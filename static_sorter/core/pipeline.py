"""
High-level pipeline orchestrator for detection, extraction, and directory organization.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Any

from static_sorter.core.checkpoint import CheckpointManager
from static_sorter.core.config import (
    DEFAULT_MAX_WORKERS,
    DEFAULT_IMAGE_FORMAT,
    DEFAULT_IMAGE_QUALITY,
    SENSITIVITY_PRESETS,
)
from static_sorter.core.detector import classify_video
from static_sorter.core.extractor import extract_best_frame
from static_sorter.core.models import (
    VideoItem,
    DetectionResult,
    ExtractionResult,
    SensitivityConfig,
)
from static_sorter.utils.file_ops import (
    discover_videos,
    safe_move_relative,
    resolve_extracted_frame_path,
)
from static_sorter.utils.system import GracefulInterruptHandler


class PipelineOrchestrator:
    """
    Coordinates multi-threaded batch operations across video sets.
    """

    def __init__(self, interrupt_handler: Optional[GracefulInterruptHandler] = None):
        self.interrupt_handler = interrupt_handler or GracefulInterruptHandler()

    def run_detection(
        self,
        folder: Path,
        recursive: bool = False,
        sensitivity: str = "medium",
        workers: int = DEFAULT_MAX_WORKERS,
        move: bool = False,
        fresh: bool = False,
        on_progress: Optional[Callable[[DetectionResult, int, int], None]] = None,
    ) -> Dict[str, Any]:
        """
        Execute video detection/classification pass on a directory.
        """
        folder = folder.resolve()
        cfg: SensitivityConfig = SENSITIVITY_PRESETS.get(
            sensitivity, SENSITIVITY_PRESETS["medium"]
        )

        ckpt = CheckpointManager(folder, enable_csv=True)
        if fresh:
            ckpt.clear()

        ckpt.save_meta(
            sensitivity=sensitivity,
            started_at=datetime.now().isoformat(),
            recursive=recursive,
        )

        all_videos = discover_videos(folder, recursive=recursive)
        if not all_videos:
            ckpt.flush_and_stop()
            return {
                "total": 0,
                "processed": 0,
                "cached": 0,
                "skipped": 0,
                "static": 0,
                "dynamic": 0,
                "review": 0,
                "errors": 0,
                "results": [],
                "moved_paths": {},
            }

        completed_records = ckpt.get_all_completed()

        results: List[DetectionResult] = []
        counts = {"static": 0, "dynamic": 0, "review": 0, "errors": 0}
        pending: List[VideoItem] = []
        cached_count = 0

        for v in all_videos:
            if not fresh and v.rel_str in completed_records:
                cached_res = DetectionResult.from_log_dict(v, completed_records[v.rel_str])
                results.append(cached_res)
                if cached_res.error:
                    counts["errors"] += 1
                counts[cached_res.decision] = counts.get(cached_res.decision, 0) + 1
                cached_count += 1
            else:
                pending.append(v)

        def process_one(v_item: VideoItem) -> DetectionResult:
            return classify_video(v_item, cfg)

        processed_count = 0
        total_pending = len(pending)

        if pending:
            with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
                future_to_video = {
                    executor.submit(process_one, v): v for v in pending
                }

                for future in as_completed(future_to_video):
                    if self.interrupt_handler.is_interrupted:
                        break

                    try:
                        res: DetectionResult = future.result()
                    except Exception as e:
                        v = future_to_video[future]
                        res = DetectionResult(
                            video=v,
                            metadata=classify_video(v, cfg).metadata,
                            decision="dynamic",
                            error=str(e),
                        )

                    results.append(res)
                    ckpt.record_detection(res)

                    if res.error:
                        counts["errors"] += 1
                    counts[res.decision] = counts.get(res.decision, 0) + 1

                    processed_count += 1
                    if on_progress:
                        on_progress(res, processed_count, total_pending)

        ckpt.flush_and_stop()

        # Handle directory-preserving moves if requested
        moved_paths: Dict[str, Path] = {}
        if move:
            category_roots = {
                "static": folder / "static",
                "dynamic": folder / "dynamic",
                "review": folder / "review",
            }
            for res in results:
                target_root = category_roots.get(res.decision)
                if target_root and res.video.path.exists() and res.video.path.is_file():
                    try:
                        dest = safe_move_relative(
                            src_path=res.video.path,
                            src_root=folder,
                            dst_category_root=target_root,
                        )
                        moved_paths[res.video.rel_str] = dest
                    except Exception as e:
                        res.error = f"Move failed: {e}"

        return {
            "total": len(all_videos),
            "processed": processed_count,
            "cached": cached_count,
            "skipped": len(all_videos) - (processed_count + cached_count),
            "static": counts["static"],
            "dynamic": counts["dynamic"],
            "review": counts["review"],
            "errors": counts["errors"],
            "results": results,
            "moved_paths": moved_paths,
        }

    def run_extraction(
        self,
        folder: Path,
        recursive: bool = False,
        output_dir: Optional[Path] = None,
        fmt: str = DEFAULT_IMAGE_FORMAT,
        quality: int = DEFAULT_IMAGE_QUALITY,
        workers: int = DEFAULT_MAX_WORKERS,
        skip_existing: bool = False,
        fresh: bool = False,
        on_progress: Optional[Callable[[ExtractionResult, int, int], None]] = None,
    ) -> Dict[str, Any]:
        """
        Extract the best frame from every video in folder, preserving relative subfolder trees.
        """
        folder = folder.resolve()
        out_root = (output_dir or (folder / "extracted_frames")).resolve()
        out_root.mkdir(parents=True, exist_ok=True)

        all_videos = discover_videos(folder, recursive=recursive)
        if not all_videos:
            return {"total": 0, "processed": 0, "success": 0, "errors": 0, "results": []}

        # Filter skip_existing
        to_process: List[VideoItem] = []
        for v in all_videos:
            dest_img = resolve_extracted_frame_path(
                src_path=v.path,
                src_root=folder,
                output_root=out_root,
                fmt=fmt,
            )
            if skip_existing and not fresh and dest_img.exists():
                continue
            to_process.append(v)

        results: List[ExtractionResult] = []
        counts = {"success": 0, "errors": 0}

        def process_one(v_item: VideoItem) -> ExtractionResult:
            if not v_item.path.exists() or not v_item.path.is_file():
                return ExtractionResult(
                    video=v_item,
                    status="error",
                    error=f"Source video file not found: {v_item.path}",
                )
            dest_img = resolve_extracted_frame_path(
                src_path=v_item.path,
                src_root=folder,
                output_root=out_root,
                fmt=fmt,
            )
            return extract_best_frame(
                video_item=v_item,
                output_path=dest_img,
                fmt=fmt,
                quality=quality,
            )

        processed_count = 0
        total_pending = len(to_process)

        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            future_to_video = {
                executor.submit(process_one, v): v for v in to_process
            }

            for future in as_completed(future_to_video):
                if self.interrupt_handler.is_interrupted:
                    break

                try:
                    res: ExtractionResult = future.result()
                except Exception as e:
                    v = future_to_video[future]
                    res = ExtractionResult(video=v, status="error", error=str(e))

                results.append(res)
                if res.status == "ok":
                    counts["success"] += 1
                else:
                    counts["errors"] += 1

                processed_count += 1
                if on_progress:
                    on_progress(res, processed_count, total_pending)

        return {
            "total": len(all_videos),
            "processed": len(results),
            "skipped": len(all_videos) - len(results),
            "success": counts["success"],
            "errors": counts["errors"],
            "results": results,
            "output_dir": out_root,
        }

    def run_unified_pipeline(
        self,
        folder: Path,
        recursive: bool = False,
        sensitivity: str = "medium",
        workers: int = DEFAULT_MAX_WORKERS,
        move: bool = True,
        action: str = "move",  # "move", "copy"
        extract_dir: Optional[Path] = None,
        fmt: str = DEFAULT_IMAGE_FORMAT,
        quality: int = DEFAULT_IMAGE_QUALITY,
        fresh: bool = False,
        on_detection_progress: Optional[Callable[[DetectionResult, int, int], None]] = None,
        on_extraction_progress: Optional[Callable[[ExtractionResult, int, int], None]] = None,
    ) -> Dict[str, Any]:
        """
        One-shot unified pipeline:
        1. Classify all videos (static / dynamic / review).
        2. Move / organize classified videos into categorized subfolders preserving tree.
        3. Extract highest quality stills for all confirmed static videos.
        """
        folder = folder.resolve()

        # Step 1: Detect
        det_summary = self.run_detection(
            folder=folder,
            recursive=recursive,
            sensitivity=sensitivity,
            workers=workers,
            move=move,
            fresh=fresh,
            on_progress=on_detection_progress,
        )

        # Step 2: Determine which items need frame extraction
        static_results = [
            r for r in det_summary["results"] if r.decision == "static" and not r.error
        ]

        if not static_results:
            return {
                "detection": det_summary,
                "extraction": {"total": 0, "processed": 0, "success": 0, "errors": 0},
            }

        # If files were moved to folder/static, the source files for extraction are now at moved_paths
        videos_to_extract: List[VideoItem] = []
        for r in static_results:
            current_path = det_summary["moved_paths"].get(r.video.rel_str, r.video.path)
            if current_path.exists() and current_path.is_file():
                videos_to_extract.append(
                    VideoItem(
                        path=current_path,
                        root_dir=folder,
                        rel_path=r.video.rel_path,
                    )
                )

        # Output folder for frames
        if extract_dir:
            out_frames_root = extract_dir.resolve()
        elif move:
            # If moved, place frames inside folder/static preserving tree
            out_frames_root = folder / "static"
        else:
            out_frames_root = folder / "extracted_frames"

        out_frames_root.mkdir(parents=True, exist_ok=True)

        # Step 3: Extract frames for static videos
        extraction_results: List[ExtractionResult] = []
        ext_counts = {"success": 0, "errors": 0}

        def extract_one(v_item: VideoItem) -> ExtractionResult:
            if not v_item.path.exists() or not v_item.path.is_file():
                return ExtractionResult(
                    video=v_item,
                    status="error",
                    error=f"Source video file not found: {v_item.path}",
                )
            dest_img = resolve_extracted_frame_path(
                src_path=v_item.path,
                src_root=folder if not move else (folder / "static"),
                output_root=out_frames_root,
                fmt=fmt,
            )
            return extract_best_frame(
                video_item=v_item,
                output_path=dest_img,
                fmt=fmt,
                quality=quality,
            )

        processed_count = 0
        total_extract = len(videos_to_extract)

        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            future_to_video = {
                executor.submit(extract_one, v): v for v in videos_to_extract
            }

            for future in as_completed(future_to_video):
                if self.interrupt_handler.is_interrupted:
                    break

                try:
                    res: ExtractionResult = future.result()
                except Exception as e:
                    v = future_to_video[future]
                    res = ExtractionResult(video=v, status="error", error=str(e))

                extraction_results.append(res)
                if res.status == "ok":
                    ext_counts["success"] += 1
                else:
                    ext_counts["errors"] += 1

                processed_count += 1
                if on_extraction_progress:
                    on_extraction_progress(res, processed_count, total_extract)

        ext_summary = {
            "total": len(videos_to_extract),
            "processed": len(extraction_results),
            "success": ext_counts["success"],
            "errors": ext_counts["errors"],
            "results": extraction_results,
            "output_dir": out_frames_root,
        }

        return {
            "detection": det_summary,
            "extraction": ext_summary,
        }
