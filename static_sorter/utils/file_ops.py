"""
File discovery, directory preservation, safe moves, and storage estimations.
"""
import os
import shutil
import time
from pathlib import Path
from typing import List, Set, Optional

from static_sorter.core.config import VIDEO_EXTENSIONS
from static_sorter.core.models import VideoItem

DEFAULT_EXCLUDED_FOLDERS = {"static", "dynamic", "review", "extracted_frames"}


def discover_videos(
    root_dir: Path,
    recursive: bool = False,
    exclude_folders: Optional[Set[str]] = None,
) -> List[VideoItem]:
    """
    Discover all candidate video files in root_dir.
    Calculates relative paths and filters out hidden files and excluded category directories.
    """
    root_dir = root_dir.resolve()
    if not root_dir.is_dir():
        return []

    if exclude_folders is None:
        exclude_folders = DEFAULT_EXCLUDED_FOLDERS

    if recursive:
        candidates = root_dir.rglob("*")
    else:
        candidates = root_dir.iterdir()

    discovered: List[VideoItem] = []

    for p in candidates:
        if not p.is_file():
            continue

        if p.suffix.lower() not in VIDEO_EXTENSIONS:
            continue

        try:
            rel = p.relative_to(root_dir)
        except ValueError:
            continue

        # Skip any dotfile or hidden directory
        if any(part.startswith(".") for part in rel.parts):
            continue

        # Skip categorized output folders at the root level
        if rel.parts and rel.parts[0].lower() in exclude_folders:
            continue

        discovered.append(VideoItem(path=p, root_dir=root_dir, rel_path=rel))

    # Sort deterministically by relative path string
    return sorted(discovered, key=lambda item: item.rel_str)


def safe_move_relative(
    src_path: Path,
    src_root: Path,
    dst_category_root: Path,
) -> Path:
    """
    Move src_path into dst_category_root while maintaining its relative directory hierarchy.
    Handles filename collisions cleanly by appending a timestamp.
    
    Example:
      src: /data/videos/holidays/2024/beach.mp4
      src_root: /data/videos
      dst_category_root: /data/videos/static
      -> moves to /data/videos/static/holidays/2024/beach.mp4
    """
    src_path = src_path.resolve()
    src_root = src_root.resolve()
    dst_category_root = dst_category_root.resolve()

    if not src_path.exists() or not src_path.is_file():
        raise FileNotFoundError(f"Source file does not exist: {src_path}")

    try:
        rel = src_path.relative_to(src_root)
    except ValueError:
        rel = Path(src_path.name)

    target_path = dst_category_root / rel
    target_path.parent.mkdir(parents=True, exist_ok=True)

    if target_path.exists():
        timestamp = time.time_ns()
        collision_name = f"{target_path.stem}_dup{timestamp}{target_path.suffix}"
        target_path = target_path.parent / collision_name

    shutil.move(str(src_path), str(target_path))
    return target_path


def resolve_extracted_frame_path(
    src_path: Path,
    src_root: Path,
    output_root: Path,
    fmt: str = "jpg",
) -> Path:
    """
    Compute destination image path preserving the relative directory hierarchy.
    """
    src_path = src_path.resolve()
    src_root = src_root.resolve()
    output_root = output_root.resolve()

    clean_fmt = fmt.lstrip(".").lower()
    try:
        rel = src_path.relative_to(src_root)
    except ValueError:
        rel = Path(src_path.name)

    rel_image = rel.with_suffix(f".{clean_fmt}")
    return output_root / rel_image


def estimate_space_savings(video_items: List[VideoItem], avg_frame_bytes: int = 300 * 1024) -> int:
    """
    Estimate bytes saved if videos are replaced by extracted still frames.
    """
    total_video_bytes = 0
    for v in video_items:
        try:
            total_video_bytes += v.path.stat().st_size
        except Exception:
            pass

    estimated_frames_bytes = len(video_items) * avg_frame_bytes
    return max(0, total_video_bytes - estimated_frames_bytes)
