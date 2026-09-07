"""
Media probing and frame extraction primitives utilizing ffmpeg and ffprobe.
"""
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional

import cv2
import numpy as np

from static_sorter.core.config import (
    DEFAULT_FFMPEG_TIMEOUT,
    DEFAULT_PROBE_TIMEOUT,
    DEFAULT_EXTRACT_TIMEOUT,
    ANALYSIS_WIDTH,
)
from static_sorter.core.models import VideoMetadata


def probe_video(video_path: Path, timeout: int = DEFAULT_PROBE_TIMEOUT) -> VideoMetadata:
    """
    Probe video metadata using ffprobe.
    Returns populated VideoMetadata object.
    """
    meta = VideoMetadata()
    if not video_path.exists() or not video_path.is_file():
        return meta

    try:
        meta.filesize_bytes = video_path.stat().st_size
    except Exception:
        meta.filesize_bytes = 0

    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration,size:stream=width,height,codec_name,codec_type",
        "-of", "json",
        str(video_path),
    ]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if proc.returncode != 0:
            return meta

        data = json.loads(proc.stdout)
        
        # Duration & size from format
        fmt = data.get("format", {})
        if "duration" in fmt:
            try:
                meta.duration_s = float(fmt["duration"])
            except ValueError:
                pass

        # Stream properties
        streams = data.get("streams", [])
        for s in streams:
            stype = s.get("codec_type")
            if stype == "video" and meta.width == 0:
                meta.width = int(s.get("width", 0))
                meta.height = int(s.get("height", 0))
                meta.codec = s.get("codec_name", "unknown")
                if meta.width > 0 and meta.height > 0:
                    ratio = meta.width / meta.height
                    if 0.50 <= ratio <= 0.60:
                        meta.aspect_ratio = "9:16"
                    elif 0.95 <= ratio <= 1.05:
                        meta.aspect_ratio = "1:1"
                    elif 0.75 <= ratio <= 0.85:
                        meta.aspect_ratio = "4:5"
                    elif 1.70 <= ratio <= 1.85:
                        meta.aspect_ratio = "16:9"
                    else:
                        meta.aspect_ratio = f"{meta.width}:{meta.height}"
            elif stype == "audio":
                meta.has_audio = True

    except Exception:
        pass

    return meta


def sample_count_for_duration(duration: float) -> int:
    """Determine number of frames to sample during classification."""
    if duration < 3:
        return 2
    if duration < 5:
        return 3
    if duration < 10:
        return 4
    if duration < 30:
        return 5
    if duration < 60:
        return 8
    return 16


def extract_subsampled_frames(
    video_path: Path,
    count: int,
    duration: float,
    timeout: int = DEFAULT_FFMPEG_TIMEOUT,
    target_width: int = ANALYSIS_WIDTH,
) -> List[np.ndarray]:
    """
    Extract low-resolution frames evenly spaced across video for fast motion analysis.
    """
    if not video_path.exists() or not video_path.is_file():
        return []

    if duration <= 0:
        duration = 60.0

    margin = max(0.5, duration * 0.05)
    t_start = margin
    t_end = max(t_start + 1.0, duration - margin)

    if count <= 1:
        timestamps = [(t_start + t_end) / 2.0]
    else:
        step = (t_end - t_start) / (count - 1)
        timestamps = [t_start + i * step for i in range(count)]

    frames: List[np.ndarray] = []

    with tempfile.TemporaryDirectory() as tmpdir:
        for idx, ts in enumerate(timestamps):
            out_img = os.path.join(tmpdir, f"sample_{idx:03d}.jpg")
            cmd = [
                "ffmpeg",
                "-ss", f"{ts:.3f}",
                "-i", str(video_path),
                "-frames:v", "1",
                "-vf", f"scale={target_width}:-2",
                "-loglevel", "error",
                "-y", out_img,
            ]
            try:
                proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
                if proc.returncode == 0 and os.path.exists(out_img):
                    img = cv2.imread(out_img)
                    if img is not None:
                        frames.append(img)
            except Exception:
                pass

    return frames


def extract_full_resolution_frames(
    video_path: Path,
    duration: float,
    timeout: int = DEFAULT_EXTRACT_TIMEOUT,
) -> List[np.ndarray]:
    """
    Extract full-resolution frames across the video for best frame picking.
    """
    if not video_path.exists() or not video_path.is_file():
        return []

    if duration <= 0:
        duration = 60.0

    max_frames = 20
    n = min(max_frames, max(8, int(duration * 1.5)))
    margin = max(0.5, duration * 0.05)
    t_start = margin
    t_end = max(t_start + 1.0, duration - margin)
    fps = max(n / max(t_end - t_start, 1.0), 0.5)

    frames: List[np.ndarray] = []

    with tempfile.TemporaryDirectory() as tmpdir:
        out = os.path.join(tmpdir, "f%04d.png")
        cmd = [
            "ffmpeg",
            "-i", str(video_path),
            "-ss", f"{t_start:.3f}",
            "-to", f"{t_end:.3f}",
            "-vf", f"fps={fps:.5f}",
            "-compression_level", "0",
            "-loglevel", "error",
            "-y", out,
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
            if proc.returncode == 0:
                for fname in sorted(os.listdir(tmpdir)):
                    if fname.endswith(".png"):
                        fpath = os.path.join(tmpdir, fname)
                        frame = cv2.imread(fpath)
                        if frame is not None:
                            frames.append(frame)
        except Exception:
            pass

        # Fallback if range seek failed to produce enough frames
        if len(frames) < 2:
            for f in os.listdir(tmpdir):
                try:
                    os.remove(os.path.join(tmpdir, f))
                except OSError:
                    pass
            cmd_fallback = [
                "ffmpeg",
                "-i", str(video_path),
                "-vf", f"fps={fps:.5f}",
                "-compression_level", "0",
                "-loglevel", "error",
                "-y", out,
            ]
            try:
                proc = subprocess.run(cmd_fallback, capture_output=True, timeout=timeout)
                if proc.returncode == 0:
                    for fname in sorted(os.listdir(tmpdir)):
                        if fname.endswith(".png"):
                            fpath = os.path.join(tmpdir, fname)
                            frame = cv2.imread(fpath)
                            if frame is not None:
                                frames.append(frame)
            except Exception:
                pass

    return frames
