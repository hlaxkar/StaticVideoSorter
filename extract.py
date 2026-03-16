#!/usr/bin/env python3
"""
extract.py — Extract the best frame from every video in a folder.

Works on Linux and Android Termux. Termux mode is auto-detected.

Usage:
    python extract.py /path/to/folder [options]

Options:
    --output-dir PATH   Where to save frames (default: <folder>/extracted_frames/)
    --format png|jpg    Output format (default: jpg)
    --quality 1-100     JPG quality (default: 95, ignored for PNG)
    --workers N         Parallel workers (default: auto)
    --skip-existing     Skip videos whose frame already exists in output dir
    --fresh             Re-extract everything, ignoring skip list

Examples:
    # Extract from static folder (most common use case)
    python extract.py /videos/static

    # Extract from review folder into a custom output dir
    python extract.py /videos/review --output-dir /videos/review_frames

    # Re-extract only new additions
    python extract.py /videos/static --skip-existing
"""

import argparse
import os
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import cv2
import numpy as np

try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False

# ─────────────────────────────────────────────
# ENVIRONMENT DETECTION
# ─────────────────────────────────────────────

def detect_environment() -> dict:
    is_termux = (
        os.environ.get("TERMUX_VERSION") is not None
        or Path("/data/data/com.termux").exists()
        or "com.termux" in os.environ.get("PREFIX", "")
    )
    cpu_count = os.cpu_count() or 2
    return {
        "is_termux":      is_termux,
        "max_workers":    2 if is_termux else min(cpu_count, 8),
        "ffmpeg_timeout": 120 if is_termux else 90,
        "probe_timeout":  20  if is_termux else 10,
    }

ENV = detect_environment()

# ─────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".webm",
    ".flv", ".m4v", ".3gp", ".ts", ".wmv",
}

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
                "\n\n⚠️  Interrupt — finishing current extractions then stopping...\n"
                "   Ctrl+C again to force quit immediately.\n"
            )
            _interrupt_event.set()
        else:
            print("\n🛑 Force quit.\n")
            _hard_stop_event.set()
            sys.exit(1)

signal.signal(signal.SIGINT, _handle_sigint)

# ─────────────────────────────────────────────
# PROGRESS BAR
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

def get_duration(path: Path) -> float:
    import json
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
# FRAME EXTRACTION  (single ffmpeg call, full resolution)
# ─────────────────────────────────────────────

def extract_full_res_frames(path: Path, duration: float) -> list[np.ndarray]:
    """
    Extract frames at FULL original resolution in a single ffmpeg call.
    More frames for short videos, proportionally fewer for long ones.
    PNG used internally to avoid double-compression artefacts.
    """
    if duration <= 0:
        duration = 60.0

    n       = min(40, max(8, int(duration * 1.5)))
    margin  = max(0.5, duration * 0.05)
    t_start = margin
    t_end   = max(t_start + 1.0, duration - margin)
    fps     = n / max(t_end - t_start, 1.0)

    with tempfile.TemporaryDirectory() as tmpdir:
        out = os.path.join(tmpdir, "f%04d.png")
        cmd = [
            "ffmpeg",
            "-ss", f"{t_start:.3f}",
            "-to", f"{t_end:.3f}",
            "-i",  str(path),
            "-vf", f"fps={fps:.5f}",
            "-compression_level", "0",   # fastest PNG write, no quality loss
            "-loglevel", "error",
            "-y", out,
        ]
        try:
            subprocess.run(cmd, capture_output=True,
                           timeout=ENV["ffmpeg_timeout"])
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
    """
    Return index of the calmest (lowest motion to neighbours) +
    sharpest (highest Laplacian variance) frame.
    First and last frames are excluded (fade-in / fade-out).
    """
    if len(frames) == 1:
        return 0
    if len(frames) == 2:
        return 0

    n      = len(frames)
    motion = np.zeros(n, dtype=np.float64)
    sharp  = np.zeros(n, dtype=np.float64)

    for i in range(n):
        gray       = cv2.cvtColor(frames[i], cv2.COLOR_BGR2GRAY)
        sharp[i]   = cv2.Laplacian(gray.astype(np.float64), cv2.CV_64F).var()

    for i in range(1, n - 1):
        gp = cv2.cvtColor(frames[i-1], cv2.COLOR_BGR2GRAY).astype(np.float64)
        gc = cv2.cvtColor(frames[i],   cv2.COLOR_BGR2GRAY).astype(np.float64)
        gn = cv2.cvtColor(frames[i+1], cv2.COLOR_BGR2GRAY).astype(np.float64)
        motion[i] = (np.mean(np.abs(gc - gp)) + np.mean(np.abs(gc - gn))) / 2.0

    def norm(a: np.ndarray) -> np.ndarray:
        mn, mx = a.min(), a.max()
        return np.zeros_like(a) if mx == mn else (a - mn) / (mx - mn)

    score       = (1.0 - norm(motion)) * 0.6 + norm(sharp) * 0.4
    score[0]    = -1.0   # exclude first frame
    score[-1]   = -1.0   # exclude last frame
    return int(np.argmax(score))

# ─────────────────────────────────────────────
# PER-VIDEO EXTRACTION
# ─────────────────────────────────────────────

def extract_one(video_path: Path, output_dir: Path,
                fmt: str, quality: int,
                skip_existing: bool) -> dict:
    """Extract best frame from one video. Returns status dict."""
    out_path = output_dir / (video_path.stem + f".{fmt}")

    if skip_existing and out_path.exists():
        return {"file": video_path.name, "status": "skipped", "output": str(out_path)}

    duration = get_duration(video_path)
    frames   = extract_full_res_frames(video_path, duration)

    if not frames:
        return {"file": video_path.name, "status": "error: no frames extracted", "output": ""}

    best  = pick_best_frame(frames)
    frame = frames[best]

    try:
        if fmt == "jpg":
            ok = cv2.imwrite(str(out_path), frame,
                             [cv2.IMWRITE_JPEG_QUALITY, quality])
        else:
            ok = cv2.imwrite(str(out_path), frame,
                             [cv2.IMWRITE_PNG_COMPRESSION, 0])
        if not ok:
            return {"file": video_path.name, "status": "error: cv2.imwrite failed",
                    "output": ""}
    except Exception as e:
        return {"file": video_path.name, "status": f"error: {e}", "output": ""}

    return {"file": video_path.name, "status": "ok", "output": str(out_path)}

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Extract the best frame from every video in a folder.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("folder",
                   help="Folder containing videos")
    p.add_argument("--output-dir",   default=None,
                   help="Where to save frames (default: <folder>/extracted_frames/)")
    p.add_argument("--format",       choices=["png", "jpg"], default="jpg",
                   help="Output format (default: jpg)")
    p.add_argument("--quality",      type=int, default=95,
                   help="JPG quality 1-100 (default: 95)")
    p.add_argument("--workers",      type=int, default=None,
                   help=f"Parallel workers (default: {ENV['max_workers']})")
    p.add_argument("--skip-existing", action="store_true",
                   help="Skip videos whose frame already exists")
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
    args = parse_args()
    check_dependencies()

    folder = Path(args.folder).resolve()
    if not folder.is_dir():
        print(f"❌ Not a directory: {folder}")
        sys.exit(1)

    if ENV["is_termux"]:
        print("📱 Termux mode — workers capped at 2")

    output_dir = (
        Path(args.output_dir).resolve() if args.output_dir
        else folder / "extracted_frames"
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    workers = min(args.workers or ENV["max_workers"], ENV["max_workers"])

    # Collect videos (skip the output dir itself if nested)
    video_files = sorted([
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
    ])

    if not video_files:
        print("No video files found.")
        sys.exit(0)

    # Count how many will be skipped up front
    if args.skip_existing:
        to_process = [
            vp for vp in video_files
            if not (output_dir / (vp.stem + f".{args.format}")).exists()
        ]
        skipped_count = len(video_files) - len(to_process)
    else:
        to_process    = video_files
        skipped_count = 0

    print(f"\n🎬 Found {len(video_files)} video(s)")
    if skipped_count:
        print(f"   ⏭  Skipping {skipped_count} already extracted")
    print(f"   🔍 To extract   : {len(to_process)}")
    print(f"   Output dir      : {output_dir}")
    print(f"   Format          : {args.format.upper()}"
          + (f"  quality={args.quality}" if args.format == "jpg" else "  (lossless)"))
    print(f"   Workers         : {workers}")
    print()

    results     = []
    interrupted = False

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {}
        for vp in to_process:
            if _interrupt_event.is_set():
                break
            futures[executor.submit(
                extract_one, vp, output_dir,
                args.format, args.quality, args.skip_existing
            )] = vp

        with make_bar(len(futures), "Extracting") as pbar:
            for future in as_completed(futures):
                if _hard_stop_event.is_set():
                    break

                vp = futures[future]
                try:
                    result = future.result()
                except Exception as e:
                    result = {"file": vp.name, "status": f"error: {e}", "output": ""}

                results.append(result)
                pbar.update(1)
                pbar.set_postfix({
                    "file":   result["file"][:22],
                    "status": result["status"],
                })

                if _interrupt_event.is_set():
                    interrupted = True
                    break

    # ── Summary ──
    ok      = [r for r in results if r["status"] == "ok"]
    skipped = [r for r in results if r["status"] == "skipped"]
    errors  = [r for r in results if r["status"].startswith("error")]

    print(f"\n── Summary ─────────────────────────────────")
    print(f"  ✅ Extracted  : {len(ok)}")
    print(f"  ⏭  Skipped   : {len(skipped) + skipped_count}")
    print(f"  ❌ Errors     : {len(errors)}")

    if errors:
        print("\n  Failed files:")
        for e in errors:
            print(f"    • {e['file']} — {e['status']}")

    if interrupted:
        print("\n⚠️  Interrupted — re-run with --skip-existing to continue.")
    else:
        print(f"\n🖼  Frames saved to: {output_dir}")

    print()


if __name__ == "__main__":
    main()
