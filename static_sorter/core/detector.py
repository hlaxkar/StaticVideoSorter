"""
3-Layer detection algorithms for classifying static, dynamic, or review videos.
"""
from pathlib import Path
from typing import List

import cv2
import numpy as np

from static_sorter.core.config import (
    GRID_ROWS,
    GRID_COLS,
    ZONE_MOTION_THRESH,
    DEFAULT_FFMPEG_TIMEOUT,
)
from static_sorter.core.models import (
    VideoItem,
    VideoMetadata,
    DetectionResult,
    SensitivityConfig,
)
from static_sorter.core.media import (
    probe_video,
    sample_count_for_duration,
    extract_subsampled_frames,
)


def compute_global_motion(frames: List[np.ndarray]) -> float:
    """
    Layer 1: Compute average pixel intensity difference across consecutive frames.
    """
    if len(frames) < 2:
        return 0.0

    grays = [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) for f in frames]
    diffs = []
    for i in range(len(grays) - 1):
        diff = cv2.absdiff(grays[i], grays[i + 1])
        diffs.append(float(np.mean(diff)))

    return float(np.mean(diffs)) if diffs else 0.0


def compute_spatial_zone_ratio(
    frames: List[np.ndarray],
    rows: int = GRID_ROWS,
    cols: int = GRID_COLS,
    thresh: float = ZONE_MOTION_THRESH,
) -> float:
    """
    Layer 2: Divide frame into rows x cols grid and compute fraction of active motion zones.
    """
    if len(frames) < 2:
        return 0.0

    grays = [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) for f in frames]
    h, w = grays[0].shape[:2]
    cell_h = h // rows
    cell_w = w // cols

    if cell_h == 0 or cell_w == 0:
        return 0.0

    active_zones = 0
    total_zones = rows * cols

    for r in range(rows):
        for c in range(cols):
            y1 = r * cell_h
            y2 = (r + 1) * cell_h if r < rows - 1 else h
            x1 = c * cell_w
            x2 = (c + 1) * cell_w if c < cols - 1 else w

            cell_diffs = []
            for i in range(len(grays) - 1):
                crop_prev = grays[i][y1:y2, x1:x2]
                crop_curr = grays[i + 1][y1:y2, x1:x2]
                diff = cv2.absdiff(crop_prev, crop_curr)
                cell_diffs.append(float(np.mean(diff)))

            if cell_diffs and np.mean(cell_diffs) > thresh:
                active_zones += 1

    return active_zones / total_zones if total_zones > 0 else 0.0


def compute_heuristics(meta: VideoMetadata) -> float:
    """
    Layer 3: Score likelihood of being a static repost/visualizer from metadata signals.
    """
    score = 0.5  # Neutral baseline

    # Aspect ratio signals (Instagram Reels / TikTok / Square reposts)
    if meta.aspect_ratio in ("9:16", "1:1", "4:5"):
        score += 0.20
    elif meta.aspect_ratio == "16:9":
        score -= 0.10

    # Audio presence (often static music visualizers or reposts have audio)
    if meta.has_audio:
        score += 0.15
    else:
        score -= 0.15

    # Duration signals
    if 5.0 <= meta.duration_s <= 600.0:
        score += 0.10
    elif meta.duration_s > 1800.0:  # > 30 mins
        score -= 0.20

    # Codec signals
    if meta.codec.lower() in ("h264", "hevc", "av1", "vp9"):
        score += 0.05

    return float(np.clip(score, 0.0, 1.0))


def compute_confidence(
    global_motion: float,
    zone_ratio: float,
    heuristic: float,
    cfg: SensitivityConfig,
    duration: float = 0.0,
) -> float:
    """
    Combine scores into a single confidence rating (0.0 to 1.0).
    """
    # 1. Motion score: 0 motion -> 1.0, >= review_thresh -> 0.0
    if global_motion <= 0.0:
        motion_conf = 1.0
    elif global_motion >= cfg.global_motion_review:
        motion_conf = 0.0
    else:
        motion_conf = 1.0 - (global_motion / cfg.global_motion_review)

    # 2. Spatial zone score: 0 active zones -> 1.0, >= active_zone_ratio -> 0.0
    zone_thresh = cfg.active_zone_ratio * 1.5
    if zone_ratio <= 0.0:
        zone_conf = 1.0
    elif zone_ratio >= zone_thresh:
        zone_conf = 0.0
    else:
        zone_conf = 1.0 - (zone_ratio / zone_thresh)

    # Weighted average: 50% global motion, 30% spatial zones, 20% heuristics
    conf = (motion_conf * 0.50) + (zone_conf * 0.30) + (heuristic * 0.20)

    # Penalty for very short clips (< 10s) to guard against transient freeze-frames
    if 0 < duration < 5.0:
        conf *= 0.70
    elif 0 < duration < 10.0:
        conf *= 0.85

    return float(np.clip(conf, 0.0, 1.0))


def classify_video(
    video_item: VideoItem,
    cfg: SensitivityConfig,
    timeout: int = DEFAULT_FFMPEG_TIMEOUT,
) -> DetectionResult:
    """
    Full pipeline to probe, sample, and classify a single video item.
    """
    if not video_item.path.exists() or not video_item.path.is_file():
        return DetectionResult(
            video=video_item,
            metadata=VideoMetadata(),
            decision="dynamic",
            error=f"Video file does not exist: {video_item.path}",
        )

    meta = probe_video(video_item.path, timeout=DEFAULT_FFMPEG_TIMEOUT)
    if meta.duration_s <= 0 and meta.width == 0:
        return DetectionResult(
            video=video_item,
            metadata=meta,
            decision="dynamic",
            error="Failed to probe video stream/duration",
        )

    n_samples = sample_count_for_duration(meta.duration_s)
    frames = extract_subsampled_frames(
        video_item.path,
        count=n_samples,
        duration=meta.duration_s,
        timeout=timeout,
    )

    if len(frames) < 2:
        return DetectionResult(
            video=video_item,
            metadata=meta,
            decision="dynamic",
            error="Failed to extract sufficient sample frames",
        )

    g_motion = compute_global_motion(frames)
    z_ratio = compute_spatial_zone_ratio(frames)
    heur = compute_heuristics(meta)
    conf = compute_confidence(g_motion, z_ratio, heur, cfg, duration=meta.duration_s)

    if conf >= cfg.confidence_static:
        decision = "static"
    elif conf >= cfg.confidence_review:
        decision = "review"
    else:
        decision = "dynamic"

    return DetectionResult(
        video=video_item,
        metadata=meta,
        global_motion_score=g_motion,
        active_zone_ratio=z_ratio,
        heuristic_score=heur,
        final_confidence=conf,
        decision=decision,
    )
