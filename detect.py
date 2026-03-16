#!/usr/bin/env python3
"""
detect.py — Classify videos as static, dynamic, or review.

Works on Linux and Android Termux. Termux mode is auto-detected.

Usage:
    python detect.py /path/to/folder [options]

Options:
    --move                  Move videos into static/ dynamic/ review/ subfolders
    --sensitivity LEVEL     low | medium | high  (default: medium)
    --workers N             Parallel workers (default: auto)
    --fresh                 Ignore checkpoint, re-detect everything
    --report                Print per-video breakdown after run

Workflow:
    # Step 1: detect only, inspect log
    python detect.py /folder

    # Step 2: move once happy with decisions
    python detect.py /folder --move

    # Then extract frames from whichever folder you want:
    python extract.py /folder/static
"""

import argparse
import csv
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import termios
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import atexit

import cv2
import numpy as np

try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False

# ─────────────────────────────────────────────
# TERMINAL STATE PROTECTION
# ─────────────────────────────────────────────
# tqdm and subprocess can modify terminal attributes (e.g. disable echo).
# If the script exits uncleanly, those changes persist and the terminal
# stops echoing typed characters.  We save the original state at startup
# and guarantee it is restored on *any* exit path.

_saved_term_attrs = None

def _save_terminal_state():
    """Save current terminal attributes so we can restore them later."""
    global _saved_term_attrs
    try:
        _saved_term_attrs = termios.tcgetattr(sys.stdin.fileno())
    except (termios.error, ValueError, OSError):
        # Not a real terminal (piped stdin, CI, etc.) — nothing to save.
        pass

def _restore_terminal_state():
    """Restore terminal attributes saved at startup."""
    if _saved_term_attrs is not None:
        try:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN,
                              _saved_term_attrs)
        except (termios.error, ValueError, OSError):
            pass

_save_terminal_state()
atexit.register(_restore_terminal_state)

# ─────────────────────────────────────────────
# ENVIRONMENT DETECTION
# ─────────────────────────────────────────────

def detect_environment() -> dict:
    """Detect runtime environment and return adjusted defaults."""
    is_termux = (
        os.environ.get("TERMUX_VERSION") is not None
        or Path("/data/data/com.termux").exists()
        or "com.termux" in os.environ.get("PREFIX", "")
    )
    cpu_count = os.cpu_count() or 2
    return {
        "is_termux":      is_termux,
        "max_workers":    2 if is_termux else min(cpu_count, 8),
        "ffmpeg_timeout": 90 if is_termux else 60,
        "probe_timeout":  20 if is_termux else 10,
    }

ENV = detect_environment()

# ─────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────

SENSITIVITY_PRESETS = {
    "low": {
        "global_motion_static":  3.0,
        "global_motion_review":  8.0,
        "active_zone_ratio":     0.30,
        "confidence_static":     0.75,
        "confidence_review":     0.45,
    },
    "medium": {
        "global_motion_static":  4.5,
        "global_motion_review":  12.0,
        "active_zone_ratio":     0.25,
        "confidence_static":     0.65,
        "confidence_review":     0.40,
    },
    "high": {
        "global_motion_static":  7.0,
        "global_motion_review":  18.0,
        "active_zone_ratio":     0.35,
        "confidence_static":     0.55,
        "confidence_review":     0.35,
    },
}

def sample_count(duration: float) -> int:
    """Return how many frames to sample.  For very short clips (<10 s) we
    use fewer frames so they are spread out in time and produce meaningful
    inter-frame differences instead of near-identical consecutive frames."""
    if duration < 3:   return 2      # absolute minimum for motion calc
    if duration < 5:   return 3
    if duration < 10:  return 4
    if duration < 30:  return 5
    if duration < 60:  return 8
    return 16

ANALYSIS_WIDTH     = 320
GRID_ROWS          = 6
GRID_COLS          = 6
ZONE_MOTION_THRESH = 5.0

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".webm",
    ".flv", ".m4v", ".3gp", ".ts", ".wmv",
}

LOG_FILENAME        = "detection_log.csv"
CHECKPOINT_FILENAME = "checkpoint.json"

LOG_FIELDS = [
    "filename", "duration_s", "width", "height", "aspect_ratio",
    "has_audio", "global_motion_score", "active_zone_ratio",
    "heuristic_score", "final_confidence", "decision",
]

# ─────────────────────────────────────────────
# CHECKPOINT  (write-queue design — no concurrent disk writes)
# ─────────────────────────────────────────────

class Checkpoint:
    """
    Thread-safe checkpoint with a single background writer thread.
    Worker threads push results to a queue; the writer drains it
    sequentially. Safe on FAT32/FUSE (Android sdcard) and Linux.
    """

    def __init__(self, path: Path):
        self.path   = path
        self._data: dict = {"completed": {}, "meta": {}}
        self._queue: queue.Queue = queue.Queue()
        self._stop  = threading.Event()

        if path.exists():
            try:
                self._data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass

        self._writer = threading.Thread(target=self._writer_loop, daemon=True)
        self._writer.start()

    def _writer_loop(self):
        while not self._stop.is_set() or not self._queue.empty():
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            key, value = item
            if key == "__meta__":
                self._data["meta"].update(value)
            else:
                self._data["completed"][key] = value
            self._flush()
            self._queue.task_done()

    def _flush(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def save_meta(self, **kwargs):
        self._queue.put(("__meta__", kwargs))

    def record(self, filename: str, row: dict):
        self._queue.put((filename, row))

    def flush_and_stop(self):
        """Call before exit to ensure all queued writes complete."""
        self._stop.set()
        self._writer.join(timeout=10)
        # Final flush in case writer stopped early
        if not self._queue.empty():
            while not self._queue.empty():
                try:
                    key, value = self._queue.get_nowait()
                    if key == "__meta__":
                        self._data["meta"].update(value)
                    else:
                        self._data["completed"][key] = value
                except queue.Empty:
                    break
            self._flush()

    def wait_for_writes(self):
        """Block until the write queue is drained."""
        self._queue.join()

    def is_done(self, filename: str) -> bool:
        return filename in self._data["completed"]

    def is_error(self, filename: str) -> bool:
        d = self._data["completed"].get(filename, {}).get("decision", "")
        return str(d).startswith("error")

    def get(self, filename: str) -> dict | None:
        return self._data["completed"].get(filename)

    def all_rows(self) -> list[dict]:
        return list(self._data["completed"].values())

    def count(self) -> int:
        return len(self._data["completed"])

    def clear(self):
        self._queue.join()
        self._data = {"completed": {}, "meta": {}}
        self._flush()

# ─────────────────────────────────────────────
# INTERRUPT HANDLING
# ─────────────────────────────────────────────

_interrupt_event = threading.Event()
_hard_stop_event = threading.Event()
_sig_count       = 0
_sig_lock        = threading.Lock()

def _handle_sigint(sig, frame):
    global _sig_count
    with _sig_lock:
        _sig_count += 1
        if _sig_count == 1:
            print(
                "\n\n⚠️  Interrupt — finishing current videos, then saving checkpoint...\n"
                "   Ctrl+C again to force quit immediately.\n"
            )
            _interrupt_event.set()
        else:
            print("\n🛑 Force quit.\n")
            _hard_stop_event.set()
            _restore_terminal_state()
            sys.exit(1)

signal.signal(signal.SIGINT, _handle_sigint)

# ─────────────────────────────────────────────
# PROGRESS BAR  (tqdm or fallback)
# ─────────────────────────────────────────────

class FallbackBar:
    def __init__(self, total, desc=""):
        self.total = total
        self.desc  = desc
        self.n     = 0

    def update(self, n=1):
        self.n += n
        pct = int(self.n / self.total * 40) if self.total else 0
        bar = "█" * pct + "░" * (40 - pct)
        print(f"\r{self.desc}: [{bar}] {self.n}/{self.total}", end="", flush=True)

    def set_postfix(self, d):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        print()

def make_bar(total, desc):
    if HAS_TQDM:
        return tqdm(total=total, desc=desc, unit="video")
    return FallbackBar(total=total, desc=desc)

# ─────────────────────────────────────────────
# FFPROBE
# ─────────────────────────────────────────────

def probe_video(path: Path) -> dict | None:
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_streams", "-show_format", str(path)
    ]
    try:
        r    = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=ENV["probe_timeout"])
        data = json.loads(r.stdout)
    except Exception:
        return None

    info = {"has_audio": False, "width": 0, "height": 0,
            "duration": 0.0, "codec": ""}

    for s in data.get("streams", []):
        if s.get("codec_type") == "video" and info["width"] == 0:
            info["width"]  = s.get("width", 0)
            info["height"] = s.get("height", 0)
            info["codec"]  = s.get("codec_name", "")
        if s.get("codec_type") == "audio":
            info["has_audio"] = True

    info["duration"] = float(data.get("format", {}).get("duration", 0))
    return info

# ─────────────────────────────────────────────
# FRAME EXTRACTION  (single ffmpeg call)
# ─────────────────────────────────────────────

def extract_analysis_frames(path: Path, duration: float,
                             n: int, debug: bool = False) -> list[np.ndarray]:
    """
    Extract n small frames from video in ONE ffmpeg call.
    Frames are downscaled to ANALYSIS_WIDTH for fast motion math.

    Falls back to decoding from t=0 if the timestamp-seek approach
    yields no frames (handles zero/unreliable duration metadata).
    """
    def _run_ffmpeg(t_start, t_end, out_pattern, tmpdir):
        seg_dur    = (t_end - t_start) if (t_start is not None and t_end is not None) else 30.0
        target_fps = max(n / max(seg_dur, 1.0), 0.5)  # min 0.5fps so we always get frames
        vf         = f"fps={target_fps:.5f},scale={ANALYSIS_WIDTH}:-2"
        # Place -ss and -to AFTER -i for accurate seeking (not keyframe-only).
        # Fast pre-seek (-ss before -i) can overshoot on short clips and produce
        # zero frames when the nearest keyframe is beyond -to.
        cmd = ["ffmpeg", "-i", str(path)]
        if t_start is not None:
            cmd += ["-ss", f"{t_start:.3f}"]
        if t_end is not None:
            cmd += ["-to", f"{t_end:.3f}"]
        cmd += ["-vf", vf, "-q:v", "5",
                "-loglevel", "error", "-y", out_pattern]
        try:
            result = subprocess.run(cmd, capture_output=True,
                                    timeout=ENV["ffmpeg_timeout"])
            if debug and result.stderr:
                stderr_str = result.stderr.decode(errors="replace").strip()
                if stderr_str:
                    print(f"\n  [debug] {path.name}: {stderr_str}")
        except subprocess.TimeoutExpired:
            if debug:
                print(f"\n  [debug] {path.name}: ffmpeg timed out")
            return []
        except Exception as e:
            if debug:
                print(f"\n  [debug] {path.name}: ffmpeg error: {e}")
            return []
        found = []
        for fname in sorted(os.listdir(tmpdir)):
            if fname.endswith(".jpg"):
                frame = cv2.imread(os.path.join(tmpdir, fname))
                if frame is not None:
                    found.append(frame)
        return found

    with tempfile.TemporaryDirectory() as tmpdir:
        out = os.path.join(tmpdir, "f%04d.jpg")
        # For short videos (<10 s) use minimal margins so we actually
        # cover a meaningful portion of the timeline.  The old fixed
        # 0.5 s margin + 5-sample default would pack frames so tightly
        # that even dynamic clips appeared static.
        if duration < 5:
            margin = 0.1                 # near the edges is fine
        elif duration < 10:
            margin = min(0.3, duration * 0.03)
        else:
            margin = max(0.5, duration * 0.05)
        t_start = margin
        t_end   = max(t_start + 0.5, duration - margin)
        frames  = _run_ffmpeg(t_start, t_end, out, tmpdir)
        # Fallback: decode from t=0 with no seek (broken duration metadata)
        if len(frames) < 2:
            if debug:
                print(f"\n  [debug] {path.name}: seek got {len(frames)} frames "
                      f"— retrying from t=0")
            for f in os.listdir(tmpdir):
                os.remove(os.path.join(tmpdir, f))
            frames = _run_ffmpeg(None, None, out, tmpdir)

    return frames

# ─────────────────────────────────────────────
# DETECTION LAYERS
# ─────────────────────────────────────────────

def layer1_global_motion(frames: list[np.ndarray]) -> float:
    """Mean inter-frame pixel difference across all sampled frames."""
    if len(frames) < 2:
        return 0.0
    diffs = []
    for i in range(1, len(frames)):
        g1 = cv2.cvtColor(frames[i-1], cv2.COLOR_BGR2GRAY).astype(np.float32)
        g2 = cv2.cvtColor(frames[i],   cv2.COLOR_BGR2GRAY).astype(np.float32)
        diffs.append(float(np.mean(np.abs(g1 - g2))))
    return float(np.mean(diffs))


def layer2_spatial_zones(frames: list[np.ndarray]) -> float:
    """
    Fraction of grid zones with significant motion.
    Low fraction = motion concentrated in small overlay area = likely static bg.
    """
    if len(frames) < 2:
        return 0.0

    h, w        = frames[0].shape[:2]
    zone_h      = max(1, h // GRID_ROWS)
    zone_w      = max(1, w // GRID_COLS)
    zone_motion = np.zeros((GRID_ROWS, GRID_COLS), dtype=np.float32)

    for i in range(1, len(frames)):
        g1   = cv2.cvtColor(frames[i-1], cv2.COLOR_BGR2GRAY).astype(np.float32)
        g2   = cv2.cvtColor(frames[i],   cv2.COLOR_BGR2GRAY).astype(np.float32)
        diff = np.abs(g1 - g2)
        for r in range(GRID_ROWS):
            for c in range(GRID_COLS):
                zone_motion[r, c] += float(np.mean(
                    diff[r*zone_h:(r+1)*zone_h, c*zone_w:(c+1)*zone_w]
                ))

    zone_motion /= max(len(frames) - 1, 1)
    active = int(np.sum(zone_motion > ZONE_MOTION_THRESH))
    return active / (GRID_ROWS * GRID_COLS)


def layer3_heuristics(info: dict) -> float:
    """Heuristic score [0,1] based on video metadata.

    Short videos (<10 s) receive a penalty because the detection layers
    have less temporal data to work with — motion scores are noisier,
    so we lean toward "not static" to avoid false positives.
    """
    score, votes = 0.0, 0

    if info["width"] > 0 and info["height"] > 0:
        ar = info["width"] / info["height"]
        if ar < 0.7:                  score += 0.8  # portrait/story
        elif 0.95 < ar < 1.05:        score += 0.6  # square
        elif 1.70 < ar < 1.85:        score += 0.4  # 16:9 lyric video
        votes += 1

    if info["has_audio"]:
        score += 0.7;  votes += 1

    dur = info["duration"]
    if dur > 0:
        # Penalise very short clips: we have less confidence in our
        # motion analysis, so the heuristic should push toward "dynamic"
        # rather than "static".
        if dur < 5:
            score += 0.05;  votes += 1      # near-zero boost
        elif dur < 10:
            score += 0.15;  votes += 1      # modest boost
        elif dur < 600:
            score += 0.4;   votes += 1      # original normal boost

    if info["codec"] in ("h264", "avc", "hevc", "h265"):
        score += 0.3;  votes += 1

    return (score / votes) if votes > 0 else 0.5


def compute_confidence(global_motion: float, zone_ratio: float,
                       heuristic: float, T: dict,
                       duration: float = 0.0) -> float:
    motion_conf = 1.0 - float(np.clip(
        (global_motion - T["global_motion_static"]) /
        max(T["global_motion_review"] - T["global_motion_static"], 1e-6),
        0.0, 1.0
    ))
    zone_conf = 1.0 - float(np.clip(
        zone_ratio / max(T["active_zone_ratio"], 1e-6),
        0.0, 1.0
    ))
    raw = float(np.clip(
        motion_conf * 0.50 + zone_conf * 0.30 + heuristic * 0.20,
        0.0, 1.0
    ))
    # ── Short-video penalty ──
    # With fewer frames the motion/zone measurements are unreliable.
    # Apply a multiplicative penalty so a borderline video is pushed
    # toward "dynamic/review" instead of being called "static".
    if 0 < duration < 5:
        raw *= 0.75        # 25 % confidence reduction
    elif 0 < duration < 10:
        raw *= 0.88        # 12 % confidence reduction
    return raw

# ─────────────────────────────────────────────
# PER-VIDEO DETECTION
# ─────────────────────────────────────────────

def detect_video(video_path: Path, thresholds: dict, debug: bool = False) -> dict:
    """Run full detection pipeline. Returns log row dict. Never raises."""
    row = {f: "" for f in LOG_FIELDS}
    row["filename"] = video_path.name

    # ── Probe ──
    info = probe_video(video_path)
    if not info:
        row["decision"] = "error_probe_failed"
        return row

    duration = info["duration"] if info["duration"] > 0 else 60.0
    w, h     = info["width"], info["height"]

    row["duration_s"]   = f"{duration:.1f}"
    row["width"]        = w
    row["height"]       = h
    row["aspect_ratio"] = f"{w}x{h}" if w and h else "unknown"
    row["has_audio"]    = str(info["has_audio"])

    # ── Extract analysis frames (single ffmpeg call) ──
    n      = sample_count(duration)
    frames = extract_analysis_frames(video_path, duration, n, debug=debug)

    if len(frames) < 2:
        row["decision"] = "error_frame_extraction_failed"
        return row

    T = thresholds

    # ── Layer 1: global motion ──
    global_motion              = layer1_global_motion(frames)
    row["global_motion_score"] = f"{global_motion:.3f}"

    # ── Early exit: obviously static (skip layers 2 & 3) ──
    # Disabled for short videos (<10 s) because too-few / tightly-spaced
    # frames can produce artificially low motion scores.
    if duration >= 10 and global_motion < T["global_motion_static"] * 0.5:
        h3 = layer3_heuristics(info)
        row["active_zone_ratio"] = "0.000"
        row["heuristic_score"]   = f"{h3:.3f}"
        conf                     = compute_confidence(global_motion, 0.0, h3, T,
                                                      duration=duration)
        row["final_confidence"]  = f"{conf:.3f}"
        row["decision"]          = "static"
        return row

    # ── Early exit: obviously dynamic (skip layers 2 & 3) ──
    if global_motion > T["global_motion_review"] * 1.5:
        row["active_zone_ratio"] = "1.000"
        row["heuristic_score"]   = "0.000"
        row["final_confidence"]  = "0.000"
        row["decision"]          = "dynamic"
        return row

    # ── Layer 2: spatial zone analysis ──
    zone_ratio               = layer2_spatial_zones(frames)
    row["active_zone_ratio"] = f"{zone_ratio:.3f}"

    # ── Layer 3: heuristics ──
    heuristic              = layer3_heuristics(info)
    row["heuristic_score"] = f"{heuristic:.3f}"

    # ── Confidence + decision ──
    conf                    = compute_confidence(global_motion, zone_ratio, heuristic, T,
                                                 duration=duration)
    row["final_confidence"] = f"{conf:.3f}"

    if conf >= T["confidence_static"]:
        row["decision"] = "static"
    elif conf >= T["confidence_review"]:
        row["decision"] = "review"
    else:
        row["decision"] = "dynamic"

    return row

# ─────────────────────────────────────────────
# FILE OPERATIONS
# ─────────────────────────────────────────────

def safe_move(src: Path, dst_dir: Path) -> Path:
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    if dst.exists():
        dst = dst_dir / f"{src.stem}_dup{int(time.time())}{src.suffix}"
    shutil.move(str(src), str(dst))
    return dst

# ─────────────────────────────────────────────
# REPORT
# ─────────────────────────────────────────────

def print_report(rows: list[dict]):
    """Print a detailed per-video table sorted by decision then confidence."""
    decisions = ["static", "review", "dynamic"]
    grouped   = {d: [] for d in decisions}
    errors    = []

    for r in rows:
        d = r.get("decision", "")
        if d in grouped:
            grouped[d].append(r)
        elif d.startswith("error"):
            errors.append(r)

    for d in decisions:
        group = grouped[d]
        if not group:
            continue
        # Sort by confidence desc
        group.sort(key=lambda r: float(r.get("final_confidence") or 0), reverse=True)
        label = {"static": "🖼  STATIC", "review": "🔍 REVIEW", "dynamic": "🎥 DYNAMIC"}[d]
        print(f"\n{label} ({len(group)})")
        print(f"  {'filename':<45} {'conf':>6}  {'motion':>7}  {'zones':>6}  {'dur':>6}")
        print(f"  {'─'*45} {'─'*6}  {'─'*7}  {'─'*6}  {'─'*6}")
        for r in group:
            print(
                f"  {r['filename']:<45} "
                f"{r.get('final_confidence','?'):>6}  "
                f"{r.get('global_motion_score','?'):>7}  "
                f"{r.get('active_zone_ratio','?'):>6}  "
                f"{r.get('duration_s','?'):>6}s"
            )

    if errors:
        print(f"\n❌ ERRORS ({len(errors)})")
        for r in errors:
            print(f"  {r['filename']} — {r.get('decision','?')}")


def print_summary(rows: list[dict], moved: bool, interrupted: bool,
                  out_dirs: dict):
    counts: dict[str, int] = {}
    for r in rows:
        d = r.get("decision", "unknown")
        counts[d] = counts.get(d, 0) + 1

    print("\n── Summary ──────────────────────────────────")
    labels = {
        "static":  "🖼  static ",
        "review":  "🔍 review ",
        "dynamic": "🎥 dynamic",
    }
    for d in ("static", "review", "dynamic"):
        if d in counts:
            print(f"  {labels[d]}   {counts[d]:>5}")
    errors = sum(v for k, v in counts.items() if k.startswith("error"))
    if errors:
        print(f"  ❌ errors     {errors:>5}")
    print(f"  {'─'*30}")
    print(f"  total         {len(rows):>5}")

    if interrupted:
        print("\n⚠️  Interrupted — checkpoint saved. Re-run to resume.")
    elif not moved:
        print("\n  Videos not moved. Re-run with --move to sort into folders.")
    else:
        for d, path in out_dirs.items():
            if counts.get(d, 0) > 0:
                print(f"\n  {labels.get(d, d)}  →  {path}")
    print()

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Classify videos as static / dynamic / review.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("folder",
                   help="Folder containing videos to analyse")
    p.add_argument("--move",         action="store_true",
                   help="Move classified videos into subfolders")
    p.add_argument("--sensitivity",  choices=["low", "medium", "high"],
                   default="medium")
    p.add_argument("--workers",      type=int, default=None,
                   help=f"Parallel workers (default: {ENV['max_workers']})")
    p.add_argument("--fresh",        action="store_true",
                   help="Ignore checkpoint, re-detect everything")
    p.add_argument("--report",       action="store_true",
                   help="Print per-video breakdown after run")
    p.add_argument("--debug",        action="store_true",
                   help="Print ffmpeg stderr for any video that fails frame extraction")
    return p.parse_args()


def check_dependencies():
    missing = []
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            missing.append(tool)
    try:
        import cv2, numpy  # noqa: F401
    except ImportError as e:
        missing.append(str(e))

    if not missing:
        return

    print("❌ Missing dependencies:")
    for m in missing:
        print(f"   • {m}")

    if ENV["is_termux"]:
        print("\nInstall in Termux:")
        print("   pkg install ffmpeg")
        print("   pip install opencv-python-headless numpy tqdm")
    else:
        print("\nInstall on Linux:")
        print("   sudo apt install ffmpeg")
        print("   pip install opencv-python-headless numpy tqdm")
    sys.exit(1)


def main():
    args   = parse_args()
    check_dependencies()

    folder = Path(args.folder).resolve()
    if not folder.is_dir():
        print(f"❌ Not a directory: {folder}")
        sys.exit(1)

    if ENV["is_termux"]:
        print("📱 Termux mode — workers capped at 2, no hardware acceleration")

    thresholds = SENSITIVITY_PRESETS[args.sensitivity]
    workers    = min(args.workers or ENV["max_workers"], ENV["max_workers"])

    out_static  = folder / "static"
    out_dynamic = folder / "dynamic"
    out_review  = folder / "review"
    out_dirs    = {"static": out_static, "dynamic": out_dynamic, "review": out_review}
    log_path    = folder / LOG_FILENAME
    ckpt_path   = folder / CHECKPOINT_FILENAME

    # ── Checkpoint ──
    ckpt = Checkpoint(ckpt_path)
    if args.fresh:
        ckpt.clear()
        print("🔄 Checkpoint cleared — starting fresh.\n")

    ckpt.save_meta(
        sensitivity = args.sensitivity,
        started_at  = datetime.now().isoformat(),
        is_termux   = ENV["is_termux"],
    )

    # ── Collect videos ──
    # Only scan top-level folder — subfolders (including static/ dynamic/ review/)
    # are intentionally excluded so re-runs don't reprocess already-moved videos.
    all_videos = sorted([
        p for p in folder.iterdir()
        if p.is_file()
        and p.suffix.lower() in VIDEO_EXTENSIONS
    ])

    if not all_videos:
        print("No video files found.")
        ckpt.flush_and_stop()
        sys.exit(0)

    # Partition: already done (skip) vs needs detection (includes error retries)
    to_detect = [
        vp for vp in all_videos
        if not ckpt.is_done(vp.name) or ckpt.is_error(vp.name)
    ]
    already_done = len(all_videos) - len(to_detect)

    print(f"🎬 Found {len(all_videos)} video(s)")
    if already_done:
        print(f"   ✅ Already classified : {already_done}  (use --fresh to redo)")
    print(f"   🔍 To detect          : {len(to_detect)}")
    print(f"   Sensitivity           : {args.sensitivity}")
    print(f"   Workers               : {workers}")
    print(f"   Move after detection  : {'yes' if args.move else 'no (use --move)'}")
    print()

    interrupted = False

    # ── Detection phase ──
    if to_detect:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {}
            for vp in to_detect:
                if _interrupt_event.is_set():
                    break
                futures[executor.submit(detect_video, vp, thresholds, args.debug)] = vp

            with make_bar(len(futures), "Detecting") as pbar:
                for future in as_completed(futures):
                    if _hard_stop_event.is_set():
                        break

                    vp = futures[future]
                    try:
                        row = future.result()
                    except Exception as e:
                        row = {f: "" for f in LOG_FIELDS}
                        row["filename"] = vp.name
                        row["decision"] = f"error: {e}"

                    ckpt.record(vp.name, row)
                    pbar.update(1)
                    pbar.set_postfix({
                        "file":     vp.name[:22],
                        "result":   row.get("decision", "?"),
                    })

                    if _interrupt_event.is_set():
                        interrupted = True
                        break

        # Drain write queue before proceeding
        ckpt.wait_for_writes()

    # ── Move phase (optional) ──
    if args.move and not interrupted:
        all_rows     = ckpt.all_rows()
        needs_moving = [
            r for r in all_rows
            if r.get("decision") in ("static", "dynamic", "review")
            and (folder / r["filename"]).exists()  # still in root, not yet moved
        ]

        if needs_moving:
            for d in (out_static, out_dynamic, out_review):
                d.mkdir(exist_ok=True)

            with make_bar(len(needs_moving), "Moving") as pbar:
                for r in needs_moving:
                    if _interrupt_event.is_set():
                        interrupted = True
                        break
                    vp       = folder / r["filename"]
                    decision = r["decision"]
                    dest_dir = out_dirs[decision]
                    try:
                        safe_move(vp, dest_dir)
                    except Exception as e:
                        print(f"\n⚠️  Could not move {vp.name}: {e}")
                    pbar.update(1)

    # ── Write CSV log (always overwrite for clean state) ──
    ckpt.flush_and_stop()
    all_rows = ckpt.all_rows()

    with open(log_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_rows)

    # ── Output ──
    print_summary(all_rows, moved=args.move, interrupted=interrupted,
                  out_dirs=out_dirs)

    if args.report:
        print_report(all_rows)

    print(f"📄 Log        : {log_path}")
    print(f"💾 Checkpoint : {ckpt_path}")
    print()


if __name__ == "__main__":
    main()