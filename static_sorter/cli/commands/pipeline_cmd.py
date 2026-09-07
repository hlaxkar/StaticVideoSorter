"""
Handler for the `pipeline` (end-to-end detection + extraction) subcommand.
"""
import atexit
import sys
import time
from pathlib import Path
from typing import Any

from static_sorter.cli.ui import (
    Colors,
    print_banner,
    print_decision_badge,
    format_bytes,
    SimpleProgressBar,
    HAS_TQDM,
)
from static_sorter.core.config import (
    DEFAULT_MAX_WORKERS,
    DEFAULT_IMAGE_FORMAT,
    DEFAULT_IMAGE_QUALITY,
)
from static_sorter.core.models import DetectionResult, ExtractionResult
from static_sorter.core.pipeline import PipelineOrchestrator
from static_sorter.utils.file_ops import estimate_space_savings
from static_sorter.utils.system import (
    TerminalStateGuard,
    GracefulInterruptHandler,
    check_system_dependencies,
)

if HAS_TQDM:
    from tqdm import tqdm


def execute(args: Any) -> int:
    """Execute unified end-to-end pipeline."""
    c = Colors
    start_time = time.time()

    all_ok, _, missing = check_system_dependencies()
    if not all_ok:
        sys.stderr.write(f"❌ Missing dependencies: {', '.join(missing)}\n")
        return 1

    term_guard = TerminalStateGuard()
    term_guard.save()
    atexit.register(term_guard.restore)

    interrupt_handler = GracefulInterruptHandler()
    interrupt_handler.install()

    folder = Path(args.folder).resolve()
    if not folder.is_dir():
        sys.stderr.write(f"❌ Target path is not a directory: {folder}\n")
        return 1

    extract_dir = Path(args.extract_dir).resolve() if getattr(args, "extract_dir", None) else None

    if not getattr(args, "quiet", False):
        print_banner(
            "StaticVideoSorter — End-to-End Pipeline",
            f"Folder: {folder} | Move: {getattr(args, 'move', True)} | Sensitivity: {args.sensitivity}",
        )

    pbar_det = None
    pbar_ext = None

    def on_det_progress(res: DetectionResult, current: int, total: int):
        nonlocal pbar_det
        if getattr(args, "quiet", False):
            return

        if HAS_TQDM:
            if pbar_det is None:
                pbar_det = tqdm(total=total, desc="Step 1/2: Classifying", unit="vid")
            pbar_det.update(1)
        else:
            if pbar_det is None:
                pbar_det = SimpleProgressBar(total=total, desc="Step 1/2: Classifying")
            pbar_det.update(1)

    def on_ext_progress(res: ExtractionResult, current: int, total: int):
        nonlocal pbar_ext
        if getattr(args, "quiet", False):
            return

        if HAS_TQDM:
            if pbar_ext is None:
                pbar_ext = tqdm(total=total, desc="Step 2/2: Extracting Frames", unit="img")
            pbar_ext.update(1)
        else:
            if pbar_ext is None:
                pbar_ext = SimpleProgressBar(total=total, desc="Step 2/2: Extracting Frames")
            pbar_ext.update(1)

    tags_list = None
    if getattr(args, "tags", None):
        tags_list = [t.strip() for t in str(args.tags).split(",") if t.strip()]

    orchestrator = PipelineOrchestrator(interrupt_handler=interrupt_handler)
    pipeline_res = orchestrator.run_unified_pipeline(
        folder=folder,
        recursive=getattr(args, "recursive", False),
        sensitivity=args.sensitivity,
        workers=args.workers or DEFAULT_MAX_WORKERS,
        move=getattr(args, "move", True),
        extract_dir=extract_dir,
        fmt=getattr(args, "format", DEFAULT_IMAGE_FORMAT),
        quality=int(getattr(args, "quality", DEFAULT_IMAGE_QUALITY)),
        fresh=getattr(args, "fresh", False),
        tags=tags_list,
        on_detection_progress=on_det_progress,
        on_extraction_progress=on_ext_progress,
    )

    if pbar_det is not None:
        pbar_det.close()
    if pbar_ext is not None:
        pbar_ext.close()

    elapsed = time.time() - start_time
    det = pipeline_res["detection"]
    ext = pipeline_res["extraction"]

    # Space estimation
    static_items = [
        r.video for r in det.get("results", []) if r.decision == "static"
    ]
    space_saved = estimate_space_savings(static_items)

    if getattr(args, "json", False):
        import json
        out_data = {
            "folder": str(folder),
            "elapsed_seconds": round(elapsed, 2),
            "estimated_space_saved_bytes": space_saved,
            "detection": {
                "total": det["total"],
                "processed": det["processed"],
                "static": det["static"],
                "dynamic": det["dynamic"],
                "review": det["review"],
                "errors": det["errors"],
            },
            "extraction": {
                "total": ext["total"],
                "success": ext["success"],
                "errors": ext["errors"],
                "output_dir": str(ext.get("output_dir", "")),
            },
        }
        print(json.dumps(out_data, indent=2))
        return 0

    if not getattr(args, "quiet", False):
        print(f"\n{c.BOLD}🎉 Pipeline Completed in {elapsed:.1f}s{c.RESET}")
        print(f"{'─' * 45}")
        print(f"  • Total Videos Discovered : {det['total']}")
        print(f"  • {c.GREEN}Static Videos Converted : {ext['success']} / {det['static']}{c.RESET}")
        print(f"  • {c.BLUE}Dynamic Videos Kept     : {det['dynamic']}{c.RESET}")
        print(f"  • {c.YELLOW}Review Videos           : {det['review']}{c.RESET}")
        if det["errors"] > 0 or ext["errors"] > 0:
            print(f"  • {c.RED}Errors                  : {det['errors'] + ext['errors']}{c.RESET}")
        print(f"  • Estimated Space Saved   : {c.BOLD}{c.GREEN}{format_bytes(space_saved)}{c.RESET}")
        if ext.get("output_dir"):
            print(f"  • Frames Saved Under      : {ext['output_dir']}")
        print(f"{'─' * 45}\n")

    return 0
