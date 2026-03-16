#!/usr/bin/env python3
"""
detect_static.py — Detect static-image videos and extract their best frame.

Usage:
    python detect_static.py /path/to/folder [options]

    --dry-run                        Detect only, move nothing (saves checkpoint)
    --workers N                      Parallel threads (default: min(CPU, 8))
    --sensitivity low|medium|high    Tune thresholds (default: medium)
    --output-format png|jpg          Extracted frame format (default: png)
    --frame-quality 1-100            JPG quality (default: 95)
    --fresh                          Ignore existing checkpoint, start from scratch

Typical workflow:
    1. python detect_static.py /folder --dry-run      # inspect decisions safely
    2. python detect_static.py /folder                # reuses dry-run, moves + extracts
"""

import argparse
import csv
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(iterable, **kwargs):
        total = kwargs.get("total", "?")
        desc  = kwargs.get("desc", "")
        for i, item in enumerate(iterable):
            print(f"\r{desc}: {i+1}/{total}", end="", flush=True)
            yield item
        print()


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
    """Dynamic sample count based on video duration."""
    if duration < 30:
        return 5
    if duration < 60:
        return 8
    return 16

ANALYSIS_WIDTH     = 320    # downscale width for motion math only
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
    "heuristic_score", "final_confidence", "decision", "extracted_frame_path",
]


# ─────────────────────────────────────────────
# CHECKPOINT
# ─────────────────────────────────────────────

class Checkpoint:
    """Thread-safe per-video checkpoint flushed to disk after every completion."""

    def __init__(self, path: Path):
        self.path   = path
        self._lock  = threading.Lock()
        self._data: dict = {"completed": {}, "meta": {}}
        if path.exists():
            try:
                self._data = json.loads(path.read_text())
            except Exception:
                pass  # corrupt → start fresh

    def save_meta(self, **kwargs):
        with self._lock:
            self._data["meta"].update(kwargs)
            self._flush()

    def record(self, filename: str, row: dict):
        with self._lock:
            self._data["completed"][filename] = row
            self._flush()

    def _flush(self):
        """Atomic write via temp file."""
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2))
        tmp.replace(self.path)

    def is_done(self, filename: str) -> bool:
        return filename in self._data["completed"]

    def is_error(self, filename: str) -> bool:
        row = self._data["completed"].get(filename, {})
        return str(row.get("decision", "")).startswith("error")

    def get(self, filename: str) -> dict | None:
        return self._data["completed"].get(filename)

    def all_rows(self) -> list[dict]:
        return list(self._data["completed"].values())

    def was_dry_run(self) -> bool:
        return bool(self._data.get("meta", {}).get("dry_run", False))

    def count(self) -> int:
        return len(self._data["completed"])

    def clear(self):
        with self._lock:
            self._data = {"completed": {}, "meta": {}}
            self._flush()


# ─────────────────────────────────────────────
# INTERRUPT HANDLING
# ─────────────────────────────────────────────

_interrupt_event = threading.Event()
_hard_stop_event = threading.Event()
_interrupt_count = 0
_interrupt_lock  = threading.Lock()


def _handle_sigint(sig, frame):
    global _interrupt_count
    with _interrupt_lock:
        _interrupt_count += 1
        if _interrupt_count == 1:
            print(
                "\n\n⚠️  Interrupt received — finishing current videos then saving...\n"
                "   Press Ctrl+C again to force quit immediately.\n"
            )
            _interrupt_event.set()
        else:
            print("\n🛑 Force quit.\n")
            _hard_stop_event.set()
            sys.exit(1)


signal.signal(signal.SIGINT, _handle_sigint)


# ─────────────────────────────────────────────
# HARDWARE ACCELERATION
# ─────────────────────────────────────────────

def detect_hwaccel() -> list[str]:
    """
    Test whether ffmpeg can actually decode a real file with VAAPI and
    write a software JPEG output. VAAPI is listed as available on many
    Linux systems but often fails when the output is a software image file
    (it requires GPU→CPU readback which isn't set up by default).
    We do a real probe rather than trusting the hwaccels list.
    Returns hwaccel flags only if they genuinely work end-to-end.
    """
    # We skip hardware acceleration entirely for frame extraction.
    # VAAPI/NVDEC etc. are only useful when decoding to GPU memory for
    # GPU-side processing. Since we write JPEG/PNG files (software output),
    # hwaccel adds complexity and commonly causes "cannot transfer frame"
    # errors without any speed benefit for short videos on SSD.
    return []


_HWACCEL: list[str] = []  # reserved for future use


# ─────────────────────────────────────────────
# FFPROBE
# ─────────────────────────────────────────────

def probe_video(path: Path) -> dict | None:
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_streams", "-show_format", str(path)
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        data   = json.loads(result.stdout)
    except Exception:
        return None

    info = {"has_audio": False, "width": 0, "height": 0,
            "duration": 0.0, "codec": "", "fps": 0.0}

    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video" and info["width"] == 0:
            info["width"]  = stream.get("width", 0)
            info["height"] = stream.get("height", 0)
            info["codec"]  = stream.get("codec_name", "")
            fps_str = stream.get("r_frame_rate", "0/1")
            try:
                n, d = fps_str.split("/")
                info["fps"] = float(n) / float(d) if float(d) else 0.0
            except Exception:
                pass
        if stream.get("codec_type") == "audio":
            info["has_audio"] = True

    info["duration"] = float(data.get("format", {}).get("duration", 0))
    return info


# ─────────────────────────────────────────────
# BATCH FRAME EXTRACTION  (single ffmpeg call)
# ─────────────────────────────────────────────

def extract_frames_batch(path: Path, duration: float, n: int,
                         scale_width: int | None = None) -> list[np.ndarray]:
    """
    Extract n frames from a video in a SINGLE ffmpeg call.
    Uses fps filter between margin timestamps — no per-frame subprocess overhead.
    Optionally downscales to scale_width for fast analysis.
    """
    margin     = max(0.5, duration * 0.05)
    t_start    = margin
    t_end      = duration - margin
    seg_dur    = max(t_end - t_start, 1.0)
    target_fps = n / seg_dur

    vf = f"fps={target_fps:.5f}"
    if scale_width:
        vf += f",scale={scale_width}:-2"

    with tempfile.TemporaryDirectory() as tmpdir:
        out_pattern = os.path.join(tmpdir, "f%04d.jpg")
        cmd = (
            ["ffmpeg", "-ss", str(t_start), "-to", str(t_end),
             "-i", str(path), "-vf", vf, "-q:v", "2",
             "-loglevel", "warning", "-y", out_pattern]
        )
        try:
            subprocess.run(cmd, capture_output=True, timeout=60)
        except Exception:
            return []
        try:
            subprocess.run(cmd, capture_output=True, timeout=60)
        except Exception:
            return []

        frames = []
        for fname in sorted(os.listdir(tmpdir)):
            if fname.endswith(".jpg"):
                frame = cv2.imread(os.path.join(tmpdir, fname))
                if frame is not None:
                    frames.append(frame)

    return frames


def extract_frames_full_res(path: Path, duration: float) -> list[np.ndarray]:
    """Extract frames at FULL resolution for best-frame selection (PNG, no scale)."""
    n      = min(40, max(10, int(duration * 2)))
    margin = max(0.5, duration * 0.05)
    t_start = margin
    t_end   = duration - margin
    target_fps = n / max(t_end - t_start, 1.0)

    with tempfile.TemporaryDirectory() as tmpdir:
        out_pattern = os.path.join(tmpdir, "f%04d.png")
        cmd = [
            "ffmpeg",
            "-ss", str(t_start), "-to", str(t_end),
            "-i", str(path), "-vf", f"fps={target_fps:.5f}",
            "-compression_level", "0",
            "-loglevel", "warning", "-y", out_pattern
        ]
        try:
            subprocess.run(cmd, capture_output=True, timeout=120)
        except Exception:
            return []

        frames = []
        for fname in sorted(os.listdir(tmpdir)):
            if fname.endswith(".png"):
                frame = cv2.imread(os.path.join(tmpdir, fname))
                if frame is not None:
                    frames.append(frame)

    return frames


# ─────────────────────────────────────────────
# BEST FRAME SELECTION
# ─────────────────────────────────────────────

def pick_best_frame(frames: list[np.ndarray]) -> int:
    """Return index of the calmest + sharpest frame."""
    if len(frames) == 1:
        return 0

    motion = np.zeros(len(frames))
    for i in range(1, len(frames) - 1):
        gp = cv2.cvtColor(frames[i-1], cv2.COLOR_BGR2GRAY).astype(float)
        gc = cv2.cvtColor(frames[i],   cv2.COLOR_BGR2GRAY).astype(float)
        gn = cv2.cvtColor(frames[i+1], cv2.COLOR_BGR2GRAY).astype(float)
        motion[i] = (np.mean(np.abs(gc - gp)) + np.mean(np.abs(gc - gn))) / 2

    sharpness = np.array([
        cv2.Laplacian(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()
        for f in frames
    ])

    def norm(a):
        mn, mx = a.min(), a.max()
        return np.zeros_like(a) if mx == mn else (a - mn) / (mx - mn)

    score         = (1.0 - norm(motion)) * 0.6 + norm(sharpness) * 0.4
    score[0]      = -1
    score[-1]     = -1
    return int(np.argmax(score))


def extract_best_frame(path: Path, output_path: Path, fmt: str, quality: int) -> bool:
    info = probe_video(path)
    if not info:
        return False

    duration = info["duration"] if info["duration"] > 0 else 60.0
    frames   = extract_frames_full_res(path, duration)
    if not frames:
        return False

    frame = frames[pick_best_frame(frames)]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "jpg":
        cv2.imwrite(str(output_path), frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    else:
        cv2.imwrite(str(output_path), frame, [cv2.IMWRITE_PNG_COMPRESSION, 0])
    return True


# ─────────────────────────────────────────────
# DETECTION LAYERS
# ─────────────────────────────────────────────

def layer1_global_motion(frames: list[np.ndarray]) -> float:
    if len(frames) < 2:
        return 0.0
    diffs = [
        np.mean(np.abs(
            cv2.cvtColor(frames[i-1], cv2.COLOR_BGR2GRAY).astype(float) -
            cv2.cvtColor(frames[i],   cv2.COLOR_BGR2GRAY).astype(float)
        ))
        for i in range(1, len(frames))
    ]
    return float(np.mean(diffs))


def layer2_spatial_zones(frames: list[np.ndarray]) -> float:
    if len(frames) < 2:
        return 0.0

    h, w        = frames[0].shape[:2]
    zone_h      = max(1, h // GRID_ROWS)
    zone_w      = max(1, w // GRID_COLS)
    zone_motion = np.zeros((GRID_ROWS, GRID_COLS))

    for i in range(1, len(frames)):
        diff = np.abs(
            cv2.cvtColor(frames[i-1], cv2.COLOR_BGR2GRAY).astype(float) -
            cv2.cvtColor(frames[i],   cv2.COLOR_BGR2GRAY).astype(float)
        )
        for r in range(GRID_ROWS):
            for c in range(GRID_COLS):
                zone_motion[r, c] += np.mean(
                    diff[r*zone_h:(r+1)*zone_h, c*zone_w:(c+1)*zone_w]
                )

    zone_motion /= (len(frames) - 1)
    return float(np.sum(zone_motion > ZONE_MOTION_THRESH) / (GRID_ROWS * GRID_COLS))


def layer3_heuristics(info: dict) -> float:
    score, votes = 0.0, 0

    if info["width"] > 0 and info["height"] > 0:
        ar = info["width"] / info["height"]
        if ar < 0.7:
            score += 0.8          # portrait / story
        elif 0.95 < ar < 1.05:
            score += 0.6          # square
        elif 1.7 < ar < 1.85:
            score += 0.4          # 16:9 lyric video
        votes += 1

    if info["has_audio"]:
        score += 0.7
        votes += 1

    if 0 < info["duration"] < 600:
        score += 0.4
        votes += 1

    if info["codec"] in ("h264", "avc", "hevc", "h265"):
        score += 0.3
        votes += 1

    return (score / votes) if votes > 0 else 0.5


def compute_confidence(global_motion: float, zone_ratio: float,
                       heuristic: float, T: dict) -> float:
    motion_conf = 1.0 - float(np.clip(
        (global_motion - T["global_motion_static"]) /
        (T["global_motion_review"] - T["global_motion_static"]),
        0.0, 1.0
    ))
    zone_conf = 1.0 - float(np.clip(zone_ratio / T["active_zone_ratio"], 0.0, 1.0))
    return float(np.clip(
        motion_conf * 0.50 + zone_conf * 0.30 + heuristic * 0.20,
        0.0, 1.0
    ))


# ─────────────────────────────────────────────
# PER-VIDEO DETECTION  (no file moves here)
# ─────────────────────────────────────────────

def detect_video(video_path: Path, thresholds: dict) -> dict:
    row = {f: "" for f in LOG_FIELDS}
    row["filename"] = video_path.name

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
    row["has_audio"]    = info["has_audio"]

    # ── Batch extract analysis frames (single ffmpeg call) ──
    n      = sample_count(duration)
    frames = extract_frames_batch(video_path, duration, n, scale_width=ANALYSIS_WIDTH)

    if len(frames) < 2:
        row["decision"] = "error_frame_extraction_failed"
        return row

    # ── Layer 1 ──
    global_motion = layer1_global_motion(frames)
    row["global_motion_score"] = f"{global_motion:.3f}"

    T = thresholds

    # ── Early exit: obviously static ──
    if global_motion < T["global_motion_static"] * 0.5:
        heuristic = layer3_heuristics(info)
        row["active_zone_ratio"] = "0.000"
        row["heuristic_score"]   = f"{heuristic:.3f}"
        confidence               = compute_confidence(global_motion, 0.0, heuristic, T)
        row["final_confidence"]  = f"{confidence:.3f}"
        row["decision"]          = "static"
        return row

    # ── Early exit: obviously dynamic ──
    if global_motion > T["global_motion_review"] * 1.5:
        row["active_zone_ratio"] = "1.000"
        row["heuristic_score"]   = "0.000"
        row["final_confidence"]  = "0.000"
        row["decision"]          = "dynamic"
        return row

    # ── Layer 2 ──
    zone_ratio = layer2_spatial_zones(frames)
    row["active_zone_ratio"] = f"{zone_ratio:.3f}"

    # ── Layer 3 ──
    heuristic = layer3_heuristics(info)
    row["heuristic_score"] = f"{heuristic:.3f}"

    # ── Final confidence ──
    confidence = compute_confidence(global_motion, zone_ratio, heuristic, T)
    row["final_confidence"] = f"{confidence:.3f}"

    if confidence >= T["confidence_static"]:
        row["decision"] = "static"
    elif confidence >= T["confidence_review"]:
        row["decision"] = "review"
    else:
        row["decision"] = "dynamic"

    return row


# ─────────────────────────────────────────────
# ACT  (move + extract frame)
# ─────────────────────────────────────────────

def act_on_video(video_path: Path, decision: str,
                 out_static: Path, out_frames: Path, out_review: Path,
                 args) -> str:
    frame_path = ""

    if decision == "static":
        dest = _safe_move(video_path, out_static / video_path.name)
        fp   = out_frames / (video_path.stem + f".{args.output_format}")
        ok   = extract_best_frame(dest, fp, args.output_format, args.frame_quality)
        frame_path = str(fp) if ok else "extraction_failed"

    elif decision == "review":
        _safe_move(video_path, out_review / video_path.name)

    return frame_path


def _safe_move(src: Path, dst: Path) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst = dst.parent / f"{dst.stem}_dup{int(time.time())}{dst.suffix}"
    shutil.move(str(src), str(dst))
    return dst


# ─────────────────────────────────────────────
# SUMMARY
# ─────────────────────────────────────────────

def print_summary(rows: list[dict], is_dry: bool,
                  out_frames: Path, out_static: Path, out_review: Path,
                  interrupted: bool):
    counts: dict[str, int] = {}
    for r in rows:
        d = r.get("decision", "unknown")
        counts[d] = counts.get(d, 0) + 1

    labels = {"static": "🖼  static", "review": "🔍 review", "dynamic": "🎥 dynamic"}

    print("\n── Summary ──────────────────────────────")
    for d, c in sorted(counts.items()):
        print(f"  {labels.get(d, '   ' + d):<30} {c:>4}")

    if interrupted:
        print("\n⚠️  Interrupted — progress saved to checkpoint.")
        print("   Re-run the same command to resume.")
    elif is_dry:
        print("\n✅ Dry run complete.")
        print("   Re-run without --dry-run to move files and extract frames.")
    print()


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Detect static-image videos, move them, extract best frame."
    )
    p.add_argument("folder")
    p.add_argument("--dry-run",       action="store_true",
                   help="Detect only, no files moved (saves checkpoint for reuse)")
    p.add_argument("--workers",       type=int, default=None,
                   help="Parallel workers (default: min(CPU count, 8))")
    p.add_argument("--sensitivity",   choices=["low", "medium", "high"], default="medium")
    p.add_argument("--output-format", choices=["png", "jpg"], default="png")
    p.add_argument("--frame-quality", type=int, default=95)
    p.add_argument("--fresh",         action="store_true",
                   help="Ignore existing checkpoint, start from scratch")
    return p.parse_args()


def check_dependencies():
    missing = []
    for tool in ["ffmpeg", "ffprobe"]:
        if shutil.which(tool) is None:
            missing.append(tool)
    try:
        import cv2, numpy  # noqa
    except ImportError as e:
        missing.append(str(e))
    if missing:
        print("❌ Missing dependencies:")
        for m in missing:
            print(f"   • {m}")
        print("\nInstall with:")
        print("   sudo apt install ffmpeg")
        print("   pip install opencv-python-headless numpy tqdm")
        sys.exit(1)


def main():
    global _HWACCEL
    args = parse_args()
    check_dependencies()

    folder = Path(args.folder).resolve()
    if not folder.is_dir():
        print(f"❌ Not a directory: {folder}")
        sys.exit(1)

    thresholds = SENSITIVITY_PRESETS[args.sensitivity]
    out_static = folder / "static_videos"
    out_frames = folder / "extracted_frames"
    out_review = folder / "review"
    log_path   = folder / LOG_FILENAME
    ckpt_path  = folder / CHECKPOINT_FILENAME

    # Hardware accel (disabled — see detect_hwaccel docstring)
    _HWACCEL = detect_hwaccel()

    # ── Checkpoint ──
    ckpt = Checkpoint(ckpt_path)

    if args.fresh:
        ckpt.clear()
        print("🔄 Fresh run — checkpoint cleared.\n")

    # ── Dry-run reuse prompt ──
    # If a finished dry-run checkpoint exists and this is a real run,
    # ask the user if they want to skip re-detection entirely.
    reuse_rows: dict[str, dict] = {}   # filename → row, from dry-run ckpt
    skip_detection: set[str]    = set()

    if not args.dry_run and ckpt.count() > 0 and ckpt.was_dry_run():
        saved    = ckpt.all_rows()
        n_static = sum(1 for r in saved if r.get("decision") == "static")
        n_review = sum(1 for r in saved if r.get("decision") == "review")
        n_dyn    = sum(1 for r in saved if r.get("decision") == "dynamic")
        n_err    = sum(1 for r in saved if str(r.get("decision","")).startswith("error"))

        print(f"\n⚡ Dry-run checkpoint found:")
        print(f"   🖼  static : {n_static}")
        print(f"   🔍 review : {n_review}")
        print(f"   🎥 dynamic: {n_dyn}")
        if n_err:
            print(f"   ❌ errors : {n_err} (will be retried)")

        ans = input("\n   Reuse these decisions and skip re-detection? [Y/n]: ").strip().lower()
        if ans in ("", "y", "yes"):
            # Keep errored ones for retry; reuse the rest
            for r in saved:
                fname = r["filename"]
                if not str(r.get("decision", "")).startswith("error"):
                    reuse_rows[fname]  = r
                    skip_detection.add(fname)
            print(f"   ✅ Reusing {len(skip_detection)} decisions. "
                  f"Errors will be retried.\n")
            # Clear checkpoint so real-run progress is tracked fresh
            ckpt.clear()
        else:
            print("   Running full detection.\n")
            ckpt.clear()

    # ── Save run metadata ──
    ckpt.save_meta(
        dry_run     = args.dry_run,
        sensitivity = args.sensitivity,
        started_at  = datetime.now().isoformat(),
    )

    if not args.dry_run:
        for d in (out_static, out_frames, out_review):
            d.mkdir(exist_ok=True)

    # ── Collect videos ──
    excluded = {out_static, out_frames, out_review}
    all_videos = sorted([
        p for p in folder.rglob("*")
        if p.suffix.lower() in VIDEO_EXTENSIONS
        and p.is_file()
        and not any(p.is_relative_to(e) for e in excluded)
    ])

    if not all_videos:
        print("No video files found.")
        sys.exit(0)

    # Partition: already checkpointed (non-error) vs needs processing
    to_detect  = []
    done_count = 0
    for vp in all_videos:
        if vp.name in skip_detection:
            done_count += 1       # reused from dry-run
        elif ckpt.is_done(vp.name) and not ckpt.is_error(vp.name):
            done_count += 1       # resumed from real-run checkpoint
        else:
            to_detect.append(vp)  # needs detection (includes error retries)

    print(f"🎬 Found {len(all_videos)} video(s)")
    print(f"   ✅ Already done : {done_count}")
    print(f"   🔍 To detect   : {len(to_detect)}")
    print(f"   Sensitivity    : {args.sensitivity}")
    print(f"   Workers        : {args.workers or 'auto (max 8)'}")
    print(f"   Output format  : {args.output_format.upper()}")
    if args.dry_run:
        print("   ⚠️  DRY RUN — no files will be moved")
    print()

    workers     = min(args.workers or os.cpu_count() or 4, 8)
    interrupted = False

    # ── Phase 1: Detection ──
    if to_detect:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(detect_video, vp, thresholds): vp
                for vp in to_detect
                if not _interrupt_event.is_set()
            }

            with tqdm(total=len(futures), desc="Detecting", unit="video") as pbar:
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

                    ckpt.record(vp.name, row)   # flush to disk immediately
                    pbar.update(1)
                    pbar.set_postfix({
                        "file":     vp.name[:25],
                        "decision": row.get("decision", "?"),
                    })

                    if _interrupt_event.is_set():
                        interrupted = True
                        break

    if interrupted:
        all_rows = ckpt.all_rows() + list(reuse_rows.values())
        print_summary(all_rows, args.dry_run, out_frames, out_static, out_review,
                      interrupted=True)
        print(f"💾 Checkpoint: {ckpt_path}\n")
        sys.exit(0)

    # ── Phase 2: Act (real run only) ──
    if not args.dry_run:
        # Combine checkpoint results with reused dry-run rows
        all_detected: dict[str, dict] = {r["filename"]: r for r in ckpt.all_rows()}
        for fname, row in reuse_rows.items():
            if fname not in all_detected:
                all_detected[fname] = row

        to_act = [
            (folder / fname, row["decision"])
            for fname, row in all_detected.items()
            if row.get("decision") in ("static", "review")
            and (folder / fname).exists()
        ]

        if to_act:
            with tqdm(total=len(to_act), desc="Moving & extracting", unit="video") as pbar:
                for vp, decision in to_act:
                    if _interrupt_event.is_set():
                        interrupted = True
                        break
                    try:
                        frame_path = act_on_video(
                            vp, decision, out_static, out_frames, out_review, args
                        )
                        existing = ckpt.get(vp.name) or reuse_rows.get(vp.name) or {}
                        existing["extracted_frame_path"] = frame_path
                        ckpt.record(vp.name, existing)
                    except Exception as e:
                        print(f"\n⚠️  Error acting on {vp.name}: {e}")
                    pbar.update(1)

    # ── Write final CSV log (always overwrite) ──
    all_rows = ckpt.all_rows()
    seen     = {r["filename"] for r in all_rows}
    for fname, row in reuse_rows.items():
        if fname not in seen:
            all_rows.append(row)

    with open(log_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_rows)

    # ── Final summary ──
    print_summary(all_rows, args.dry_run, out_frames, out_static, out_review,
                  interrupted=interrupted)

    print(f"📄 Log        : {log_path}")
    print(f"💾 Checkpoint : {ckpt_path}")

    counts: dict[str, int] = {}
    for r in all_rows:
        d = r.get("decision", "")
        counts[d] = counts.get(d, 0) + 1

    if not args.dry_run:
        if counts.get("static"):
            print(f"🖼  Frames    : {out_frames}")
            print(f"📦 Videos    : {out_static}")
        if counts.get("review"):
            print(f"🔍 Review    : {out_review}  ({counts['review']} video(s))")
    print()


if __name__ == "__main__":
    main()
