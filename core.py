"""
core.py — Shared detection and extraction logic for StaticSort.

Both the CLI scripts (detect.py, extract.py) and the web GUI (app.py)
import from this module.  No CLI-specific code lives here — only the
pure algorithmic / IO functions.
"""

import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import cv2
import numpy as np

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
        # extract.py uses longer ffmpeg timeouts for full-res extraction
        "ffmpeg_timeout_extract": 120 if is_termux else 90,
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

ANALYSIS_WIDTH     = 320
GRID_ROWS          = 6
GRID_COLS          = 6
ZONE_MOTION_THRESH = 5.0


def sample_count(duration: float) -> int:
    """Return how many frames to sample for detection."""
    if duration < 3:   return 2
    if duration < 5:   return 3
    if duration < 10:  return 4
    if duration < 30:  return 5
    if duration < 60:  return 8
    return 16

# ─────────────────────────────────────────────
# CHECKPOINT
# ─────────────────────────────────────────────

class Checkpoint:
    """
    Thread-safe checkpoint with a single background writer thread.
    Worker threads push results to a queue; the writer drains it
    sequentially.  Safe on FAT32/FUSE (Android sdcard) and Linux.
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

    def _flush(self, pretty: bool = False):
        tmp = self.path.with_suffix(".tmp")
        if pretty:
            payload = json.dumps(self._data, indent=2)
        else:
            payload = json.dumps(self._data, separators=(',', ':'))
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(self.path)

    def save_meta(self, **kwargs):
        self._queue.put(("__meta__", kwargs))

    def record(self, filename: str, row: dict):
        self._queue.put((filename, row))

    def flush_and_stop(self):
        self._stop.set()
        self._writer.join(timeout=10)
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
        self._flush(pretty=True)

    def wait_for_writes(self):
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
        self._stop.set()
        self._writer.join(timeout=5)
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except queue.Empty:
                break
        self._data = {"completed": {}, "meta": {}}
        self._flush(pretty=True)
        self._stop.clear()
        self._writer = threading.Thread(target=self._writer_loop, daemon=True)
        self._writer.start()

# ─────────────────────────────────────────────
# FFPROBE
# ─────────────────────────────────────────────

def probe_video(path: Path) -> dict | None:
    """Probe video metadata via ffprobe.

    Returns a dict with width, height, codec, has_audio, duration.
    Returns None on failure.
    """
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_streams", "-show_format", str(path)
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=ENV["probe_timeout"])
        if r.returncode != 0:
            return None
        data = json.loads(r.stdout)
    except subprocess.TimeoutExpired:
        return None
    except (json.JSONDecodeError, OSError):
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


def get_duration(path: Path) -> float:
    """Return the duration of a video file in seconds via ffprobe.

    Lighter probe than probe_video() — only reads format-level duration.
    """
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_format", str(path)
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=ENV["probe_timeout"])
        return float(json.loads(r.stdout).get("format", {}).get("duration", 0))
    except Exception:
        return 60.0

# ─────────────────────────────────────────────
# FRAME EXTRACTION (detection — small frames)
# ─────────────────────────────────────────────

def extract_analysis_frames(path: Path, duration: float,
                            n: int, debug: bool = False) -> list[np.ndarray]:
    """
    Extract n small frames from video in ONE ffmpeg call.
    Frames are downscaled to ANALYSIS_WIDTH for fast motion math.
    """
    def _run_ffmpeg(t_start, t_end, out_pattern, tmpdir):
        seg_dur    = (t_end - t_start) if (t_start is not None and t_end is not None) else 30.0
        target_fps = max(n / max(seg_dur, 1.0), 0.5)
        vf         = f"fps={target_fps:.5f},scale={ANALYSIS_WIDTH}:-2"
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
            return []
        except Exception:
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
        if duration < 5:
            margin = 0.1
        elif duration < 10:
            margin = min(0.3, duration * 0.03)
        else:
            margin = max(0.5, duration * 0.05)
        t_start = margin
        t_end   = max(t_start + 0.5, duration - margin)
        frames  = _run_ffmpeg(t_start, t_end, out, tmpdir)
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

def layer1_global_motion(grays: list[np.ndarray]) -> float:
    """Mean inter-frame pixel difference across all sampled frames."""
    if len(grays) < 2:
        return 0.0
    diffs = []
    for i in range(1, len(grays)):
        diffs.append(float(np.mean(np.abs(grays[i] - grays[i-1]))))
    return float(np.mean(diffs))


def layer2_spatial_zones(grays: list[np.ndarray]) -> float:
    """Fraction of grid zones with significant motion."""
    if len(grays) < 2:
        return 0.0

    h, w        = grays[0].shape[:2]
    zone_h      = max(1, h // GRID_ROWS)
    zone_w      = max(1, w // GRID_COLS)
    gh, gw      = zone_h * GRID_ROWS, zone_w * GRID_COLS
    zone_motion = np.zeros((GRID_ROWS, GRID_COLS), dtype=np.float32)

    for i in range(1, len(grays)):
        diff = np.abs(grays[i][:gh, :gw] - grays[i-1][:gh, :gw])
        zone_motion += diff.reshape(GRID_ROWS, zone_h, GRID_COLS, zone_w).mean(axis=(1, 3))

    zone_motion /= max(len(grays) - 1, 1)
    active = int(np.sum(zone_motion > ZONE_MOTION_THRESH))
    return active / (GRID_ROWS * GRID_COLS)


def layer3_heuristics(info: dict) -> float:
    """Heuristic score [0,1] based on video metadata."""
    score, votes = 0.0, 0

    if info["width"] > 0 and info["height"] > 0:
        ar = info["width"] / info["height"]
        if ar < 0.7:                  score += 0.8
        elif 0.95 < ar < 1.05:       score += 0.6
        elif 1.70 < ar < 1.85:       score += 0.4
        votes += 1

    if info["has_audio"]:
        score += 0.7;  votes += 1

    dur = info["duration"]
    if dur > 0:
        if dur < 5:
            score += 0.05;  votes += 1
        elif dur < 10:
            score += 0.15;  votes += 1
        elif dur < 600:
            score += 0.4;   votes += 1

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
    if 0 < duration < 5:
        raw *= 0.75
    elif 0 < duration < 10:
        raw *= 0.88
    return raw

# ─────────────────────────────────────────────
# PER-VIDEO DETECTION
# ─────────────────────────────────────────────

def detect_video(video_path: Path, thresholds: dict, debug: bool = False) -> dict:
    """Run full detection pipeline. Returns log row dict. Never raises."""
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
    row["has_audio"]    = str(info["has_audio"])

    n      = sample_count(duration)
    frames = extract_analysis_frames(video_path, duration, n, debug=debug)

    if len(frames) < 2:
        row["decision"] = "error_frame_extraction_failed"
        return row

    T = thresholds

    grays = [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(np.float32)
             for f in frames]

    global_motion              = layer1_global_motion(grays)
    row["global_motion_score"] = f"{global_motion:.3f}"

    if duration >= 10 and global_motion < T["global_motion_static"] * 0.5:
        h3 = layer3_heuristics(info)
        row["active_zone_ratio"] = "0.000"
        row["heuristic_score"]   = f"{h3:.3f}"
        conf                     = compute_confidence(global_motion, 0.0, h3, T,
                                                      duration=duration)
        row["final_confidence"]  = f"{conf:.3f}"
        row["decision"]          = "static"
        return row

    if global_motion > T["global_motion_review"] * 1.5:
        heuristic = layer3_heuristics(info)
        row["active_zone_ratio"] = "skipped"
        row["heuristic_score"]   = f"{heuristic:.3f}"
        row["final_confidence"]  = "0.000"
        row["decision"]          = "dynamic"
        return row

    zone_ratio               = layer2_spatial_zones(grays)
    row["active_zone_ratio"] = f"{zone_ratio:.3f}"

    heuristic              = layer3_heuristics(info)
    row["heuristic_score"] = f"{heuristic:.3f}"

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
# FRAME EXTRACTION (full resolution — for extract)
# ─────────────────────────────────────────────

def extract_full_res_frames(path: Path, duration: float) -> list[np.ndarray]:
    """
    Extract frames at FULL original resolution in a single ffmpeg call.
    """
    if duration <= 0:
        duration = 60.0

    max_frames = 12 if ENV["is_termux"] else 20
    n       = min(max_frames, max(8, int(duration * 1.5)))
    margin  = max(0.5, duration * 0.05)
    t_start = margin
    t_end   = max(t_start + 1.0, duration - margin)
    fps     = n / max(t_end - t_start, 1.0)
    fps     = max(fps, 0.5)

    timeout = ENV["ffmpeg_timeout_extract"]

    with tempfile.TemporaryDirectory() as tmpdir:
        out = os.path.join(tmpdir, "f%04d.png")
        cmd = [
            "ffmpeg",
            "-i",  str(path),
            "-ss", f"{t_start:.3f}",
            "-to", f"{t_end:.3f}",
            "-vf", f"fps={fps:.5f}",
            "-compression_level", "0",
            "-loglevel", "error",
            "-y", out,
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
            if proc.returncode != 0:
                return []
        except Exception:
            return []

        frames = []
        for fname in sorted(os.listdir(tmpdir)):
            if fname.endswith(".png"):
                frame = cv2.imread(os.path.join(tmpdir, fname))
                if frame is not None:
                    frames.append(frame)

        if len(frames) < 2:
            for f in os.listdir(tmpdir):
                os.remove(os.path.join(tmpdir, f))
            cmd_fallback = [
                "ffmpeg", "-i", str(path),
                "-vf", f"fps={fps:.5f}",
                "-compression_level", "0",
                "-loglevel", "error",
                "-y", out,
            ]
            try:
                proc = subprocess.run(cmd_fallback, capture_output=True,
                                      timeout=timeout)
                if proc.returncode != 0:
                    return frames
            except Exception:
                return frames
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
    """
    Return index of the calmest + sharpest frame.
    First and last frames are excluded (fade-in / fade-out).
    """
    if len(frames) <= 2:
        return 0

    n      = len(frames)
    motion = np.zeros(n, dtype=np.float64)
    sharp  = np.zeros(n, dtype=np.float64)

    grays = [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) for f in frames]

    for i in range(n):
        sharp[i] = cv2.Laplacian(grays[i].astype(np.float64), cv2.CV_64F).var()

    for i in range(1, n - 1):
        gp = grays[i-1].astype(np.float64)
        gc = grays[i].astype(np.float64)
        gn = grays[i+1].astype(np.float64)
        motion[i] = (np.mean(np.abs(gc - gp)) + np.mean(np.abs(gc - gn))) / 2.0

    def norm(a: np.ndarray) -> np.ndarray:
        mn, mx = a.min(), a.max()
        return np.zeros_like(a) if mx == mn else (a - mn) / (mx - mn)

    score       = (1.0 - norm(motion)) * 0.6 + norm(sharp) * 0.4
    score[0]    = -1.0
    score[-1]   = -1.0
    return int(np.argmax(score))

# ─────────────────────────────────────────────
# PER-VIDEO EXTRACTION
# ─────────────────────────────────────────────

def extract_one_frame(video_path: Path, output_path: Path,
                      fmt: str, quality: int) -> dict:
    """Extract best frame from one video. Returns status dict."""
    duration = get_duration(video_path)
    frames   = extract_full_res_frames(video_path, duration)

    if not frames:
        return {"file": video_path.name, "status": "error: no frames extracted", "output": ""}

    best  = pick_best_frame(frames)
    frame = frames[best]

    try:
        if fmt == "jpg":
            ok = cv2.imwrite(str(output_path), frame,
                             [cv2.IMWRITE_JPEG_QUALITY, quality])
        else:
            ok = cv2.imwrite(str(output_path), frame,
                             [cv2.IMWRITE_PNG_COMPRESSION, 0])
        if not ok:
            return {"file": video_path.name, "status": "error: cv2.imwrite failed",
                    "output": ""}
    except Exception as e:
        return {"file": video_path.name, "status": f"error: {e}", "output": ""}

    return {"file": video_path.name, "status": "ok", "output": str(output_path)}

# ─────────────────────────────────────────────
# FILE OPERATIONS
# ─────────────────────────────────────────────

def safe_move(src: Path, dst_dir: Path) -> Path:
    """Move src into dst_dir, appending nanosecond timestamp on collision."""
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    if dst.exists():
        dst = dst_dir / f"{src.stem}_dup{time.time_ns()}{src.suffix}"
    shutil.move(str(src), str(dst))
    return dst


def estimate_space_saved(static_video_paths: list[Path]) -> int:
    """Returns estimated bytes saved by extracting frames instead of keeping videos."""
    total = 0
    for p in static_video_paths:
        try:
            total += p.stat().st_size
        except Exception:
            pass
    avg_frame_kb = 300 * 1024
    saved = total - (len(static_video_paths) * avg_frame_kb)
    return max(0, saved)
