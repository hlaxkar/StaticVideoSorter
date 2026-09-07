"""
Configuration constants, sensitivity presets, and configuration loading.
"""
import json
import os
from pathlib import Path
from typing import Dict, Any, Optional

from static_sorter.core.models import SensitivityConfig

# Video file extensions to scan for
VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".webm",
    ".flv", ".m4v", ".3gp", ".ts", ".wmv",
}

# Standard output filenames
LOG_FILENAME = "detection_log.csv"
CHECKPOINT_FILENAME = "checkpoint.json"

LOG_FIELDS = [
    "filename", "duration_s", "width", "height", "aspect_ratio",
    "has_audio", "global_motion_score", "active_zone_ratio",
    "heuristic_score", "final_confidence", "decision", "error",
]

# Analysis parameters
ANALYSIS_WIDTH = 320
GRID_ROWS = 6
GRID_COLS = 6
ZONE_MOTION_THRESH = 5.0

# Sensitivity presets
SENSITIVITY_PRESETS: Dict[str, SensitivityConfig] = {
    "low": SensitivityConfig(
        global_motion_static=3.0,
        global_motion_review=8.0,
        active_zone_ratio=0.30,
        confidence_static=0.75,
        confidence_review=0.45,
    ),
    "medium": SensitivityConfig(
        global_motion_static=4.5,
        global_motion_review=12.0,
        active_zone_ratio=0.25,
        confidence_static=0.65,
        confidence_review=0.40,
    ),
    "high": SensitivityConfig(
        global_motion_static=7.0,
        global_motion_review=18.0,
        active_zone_ratio=0.35,
        confidence_static=0.55,
        confidence_review=0.35,
    ),
}

# Runtime execution defaults
DEFAULT_MAX_WORKERS = min(os.cpu_count() or 2, 8)
DEFAULT_FFMPEG_TIMEOUT = 60
DEFAULT_PROBE_TIMEOUT = 10
DEFAULT_EXTRACT_TIMEOUT = 90
DEFAULT_IMAGE_FORMAT = "jpg"
DEFAULT_IMAGE_QUALITY = 95


def load_config_file(config_path: Optional[Path] = None) -> Dict[str, Any]:
    """Load configuration from file or user config directory if available."""
    paths_to_check = []
    if config_path:
        paths_to_check.append(config_path)
    
    # Default user config path ~/.config/static_sorter/config.json
    home_cfg = Path.home() / ".config" / "static_sorter" / "config.json"
    paths_to_check.append(home_cfg)

    for p in paths_to_check:
        if p.is_file():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
    return {}
