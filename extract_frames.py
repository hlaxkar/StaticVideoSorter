#!/usr/bin/env python3
"""
extract_frames.py — Extract the best frame from every video in a folder.

Usage:
    python extract_frames.py /path/to/folder [options]

    --output-dir PATH      Where to save frames (default: <folder>/extracted_frames)
    --quality 1-100        JPG quality (default: 95)
    --workers N            Parallel threads (default: auto)
    --skip-existing        Skip videos whose frame already exists in output dir
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import cv2
import numpy as np

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(iterable, **kwargs):
        total = kwargs.get("total", "?")
        desc = kwargs.get("desc", "")
        for i, item in enumerate(iterable):
            print(f"\r{desc}: {i+1}/{total}", end="", flush=True)
            yield item
        print()

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".webm",
    ".flv", ".m4v", ".3gp", ".ts", ".wmv",
}


# ─────────────────────────────────────────────
# FRAME EXTRACTION (reused from detect_static)
# ─────────────────────────────────────────────

def get_duration(path: Path) -> float:
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_format", str(path)
    ]
    try:
        import json
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        data = json.loads(result.stdout)
        return float(data.get("format", {}).get("duration", 0))
    except Exception:
        return 60.0


def extract_frames_png(path: Path, timestamps: list) -> list:
    frames = []
    with tempfile.TemporaryDirectory() as tmpdir:
        for i, ts in enumerate(timestamps):
            out = os.path.join(tmpdir, f"frame_{i:04d}.png")
            cmd = [
                "ffmpeg", "-ss", str(ts), "-i", str(path),
                "-frames:v", "1", "-q:v", "1",
                out, "-loglevel", "error", "-y"
            ]
            try:
                subprocess.run(cmd, capture_output=True, timeout=15)
                frame = cv2.imread(out)
                if frame is not None:
                    frames.append(frame)
            except Exception:
                continue
    return frames


def pick_best_frame(frames: list) -> int:
    if len(frames) == 1:
        return 0

    motion_scores = np.zeros(len(frames))
    for i in range(1, len(frames) - 1):
        g_prev = cv2.cvtColor(frames[i-1], cv2.COLOR_BGR2GRAY).astype(float)
        g_curr = cv2.cvtColor(frames[i],   cv2.COLOR_BGR2GRAY).astype(float)
        g_next = cv2.cvtColor(frames[i+1], cv2.COLOR_BGR2GRAY).astype(float)
        motion_scores[i] = (np.mean(np.abs(g_curr - g_prev)) +
                            np.mean(np.abs(g_curr - g_next))) / 2

    sharpness = np.zeros(len(frames))
    for i, frame in enumerate(frames):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        sharpness[i] = cv2.Laplacian(gray, cv2.CV_64F).var()

    def norm(arr):
        mn, mx = arr.min(), arr.max()
        if mx == mn:
            return np.zeros_like(arr)
        return (arr - mn) / (mx - mn)

    combined = (1.0 - norm(motion_scores)) * 0.6 + norm(sharpness) * 0.4
    combined[0]  = -1
    combined[-1] = -1
    return int(np.argmax(combined))


def process_video(video_path: Path, output_dir: Path,
                  quality: int, skip_existing: bool) -> dict:
    out_path = output_dir / (video_path.stem + ".jpg")

    if skip_existing and out_path.exists():
        return {"file": video_path.name, "status": "skipped", "output": str(out_path)}

    duration = get_duration(video_path)
    if duration <= 0:
        duration = 60.0

    n = min(40, max(10, int(duration * 2)))
    margin = max(0.5, duration * 0.05)
    timestamps = np.linspace(margin, duration - margin, n).tolist()

    frames = extract_frames_png(video_path, timestamps)
    if not frames:
        return {"file": video_path.name, "status": "error: no frames extracted", "output": ""}

    best = pick_best_frame(frames)
    cv2.imwrite(str(out_path), frames[best], [cv2.IMWRITE_JPEG_QUALITY, quality])

    return {"file": video_path.name, "status": "ok", "output": str(out_path)}


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Extract best frame from every video in a folder."
    )
    p.add_argument("folder", help="Folder containing videos")
    p.add_argument("--output-dir", default=None,
                   help="Where to save frames (default: <folder>/extracted_frames)")
    p.add_argument("--quality", type=int, default=95,
                   help="JPG quality 1-100 (default: 95)")
    p.add_argument("--workers", type=int, default=None,
                   help="Parallel threads (default: auto)")
    p.add_argument("--skip-existing", action="store_true",
                   help="Skip videos whose frame already exists in output dir")
    return p.parse_args()


def main():
    args = parse_args()

    folder = Path(args.folder).resolve()
    if not folder.is_dir():
        print(f"❌ Not a directory: {folder}")
        sys.exit(1)

    output_dir = Path(args.output_dir).resolve() if args.output_dir else folder / "extracted_frames"
    output_dir.mkdir(parents=True, exist_ok=True)

    video_files = [
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
    ]

    if not video_files:
        print("No video files found.")
        sys.exit(0)

    print(f"\n🎬 Found {len(video_files)} video(s)")
    print(f"   Output dir : {output_dir}")
    print(f"   JPG quality: {args.quality}")
    print(f"   Workers    : {args.workers or 'auto'}\n")

    workers = args.workers or os.cpu_count() or 4
    results = []

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                process_video, vp, output_dir, args.quality, args.skip_existing
            ): vp
            for vp in video_files
        }
        with tqdm(total=len(futures), desc="Extracting", unit="video") as pbar:
            for future in as_completed(futures):
                try:
                    row = future.result()
                except Exception as e:
                    vp = futures[future]
                    row = {"file": vp.name, "status": f"error: {e}", "output": ""}
                results.append(row)
                pbar.update(1)
                pbar.set_postfix({"last": row["file"][:30]})

    ok      = [r for r in results if r["status"] == "ok"]
    skipped = [r for r in results if r["status"] == "skipped"]
    errors  = [r for r in results if r["status"].startswith("error")]

    print(f"\n── Summary ──────────────────────────────")
    print(f"  ✅  Extracted : {len(ok)}")
    print(f"  ⏭   Skipped   : {len(skipped)}")
    print(f"  ❌  Errors    : {len(errors)}")
    if errors:
        for e in errors:
            print(f"       • {e['file']}: {e['status']}")
    print(f"\n🖼  Frames saved to: {output_dir}\n")


if __name__ == "__main__":
    main()
