"""
Extraction of the highest quality / calmest frame from a video.
"""
from pathlib import Path
from typing import List, Optional

import cv2
import numpy as np

from static_sorter.core.config import (
    DEFAULT_EXTRACT_TIMEOUT,
    DEFAULT_IMAGE_FORMAT,
    DEFAULT_IMAGE_QUALITY,
)
from static_sorter.core.models import VideoItem, ExtractionResult
from static_sorter.core.media import probe_video, extract_full_resolution_frames
from static_sorter.core.exif import save_image_with_metadata


def pick_best_frame(frames: List[np.ndarray]) -> int:
    """
    Select the index of the highest quality frame:
    - High sharpness (Laplacian variance)
    - Low local motion relative to neighbors
    - Excludes first and last frames to avoid fade-in/fade-out artifacts.
    """
    n = len(frames)
    if n <= 1:
        return 0
    if n == 2:
        return 0

    motion = np.zeros(n, dtype=np.float64)
    sharp = np.zeros(n, dtype=np.float64)

    grays = [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) for f in frames]

    for i in range(n):
        sharp[i] = cv2.Laplacian(grays[i].astype(np.float64), cv2.CV_64F).var()

    for i in range(1, n - 1):
        gp = grays[i - 1].astype(np.float64)
        gc = grays[i].astype(np.float64)
        gn = grays[i + 1].astype(np.float64)
        motion[i] = (np.mean(np.abs(gc - gp)) + np.mean(np.abs(gc - gn))) / 2.0

    def norm(a: np.ndarray) -> np.ndarray:
        mn, mx = a.min(), a.max()
        return np.zeros_like(a) if mx == mn else (a - mn) / (mx - mn)

    # Composite score: 60% calmness, 40% sharpness
    score = (1.0 - norm(motion)) * 0.6 + norm(sharp) * 0.4
    score[0] = -1.0
    score[-1] = -1.0

    return int(np.argmax(score))


def extract_best_frame(
    video_item: VideoItem,
    output_path: Path,
    fmt: str = DEFAULT_IMAGE_FORMAT,
    quality: int = DEFAULT_IMAGE_QUALITY,
    timeout: int = DEFAULT_EXTRACT_TIMEOUT,
    tags: Optional[List[str]] = None,
) -> ExtractionResult:
    """
    Extract best frame from video_item and save it to output_path,
    retaining all video metadata, EXIF tags, GPS location, keywords, and timestamps.
    """
    if not video_item.path.exists() or not video_item.path.is_file():
        return ExtractionResult(
            video=video_item,
            status="error",
            error=f"Video file does not exist: {video_item.path}",
        )

    meta = probe_video(video_item.path)
    frames = extract_full_resolution_frames(
        video_item.path,
        duration=meta.duration_s,
        timeout=timeout,
    )

    if not frames:
        return ExtractionResult(
            video=video_item,
            status="error",
            error="No frames extracted by ffmpeg",
        )

    best_idx = pick_best_frame(frames)
    frame = frames[best_idx]

    try:
        ok = save_image_with_metadata(
            frame_bgr=frame,
            output_path=output_path,
            source_video_path=video_item.path,
            meta=meta,
            fmt=fmt,
            quality=quality,
            tags=tags,
        )

        if not ok:
            return ExtractionResult(
                video=video_item,
                status="error",
                error="Failed to save image with metadata",
                total_frames_evaluated=len(frames),
            )

        return ExtractionResult(
            video=video_item,
            output_path=output_path,
            status="ok",
            best_frame_idx=best_idx,
            total_frames_evaluated=len(frames),
        )
    except Exception as e:
        return ExtractionResult(
            video=video_item,
            status="error",
            error=str(e),
            total_frames_evaluated=len(frames),
        )

