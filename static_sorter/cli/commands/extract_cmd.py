"""
Handler for the `extract` subcommand.
"""
import atexit
import sys
import time
from pathlib import Path
from typing import Any

from static_sorter.cli.ui import (
    Colors,
    print_banner,
    SimpleProgressBar,
    HAS_TQDM,
)
from static_sorter.core.config import (
    DEFAULT_MAX_WORKERS,
    DEFAULT_IMAGE_FORMAT,
    DEFAULT_IMAGE_QUALITY,
)
from static_sorter.core.models import ExtractionResult
from static_sorter.core.pipeline import PipelineOrchestrator
from static_sorter.utils.system import (
    TerminalStateGuard,
    GracefulInterruptHandler,
    check_system_dependencies,
)

if HAS_TQDM:
    from tqdm import tqdm


def execute(args: Any) -> int:
    """Execute frame extraction command."""
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

    output_dir = Path(args.output_dir).resolve() if getattr(args, "output_dir", None) else None

    if not getattr(args, "quiet", False):
        print_banner(
            "StaticVideoSorter — Best Frame Extraction",
            f"Folder: {folder} | Format: {args.format} | Quality: {args.quality}",
        )

    pbar = None

    def on_progress(res: ExtractionResult, current: int, total: int):
        nonlocal pbar
        if getattr(args, "quiet", False):
            return

        if HAS_TQDM:
            if pbar is None:
                pbar = tqdm(total=total, desc="Extracting frames", unit="img")
            pbar.update(1)
        else:
            if pbar is None:
                pbar = SimpleProgressBar(total=total, desc="Extracting frames")
            pbar.update(1)

    tags_list = None
    if getattr(args, "tags", None):
        tags_list = [t.strip() for t in str(args.tags).split(",") if t.strip()]

    orchestrator = PipelineOrchestrator(interrupt_handler=interrupt_handler)
    summary = orchestrator.run_extraction(
        folder=folder,
        recursive=getattr(args, "recursive", False),
        output_dir=output_dir,
        fmt=getattr(args, "format", DEFAULT_IMAGE_FORMAT),
        quality=int(getattr(args, "quality", DEFAULT_IMAGE_QUALITY)),
        workers=args.workers or DEFAULT_MAX_WORKERS,
        skip_existing=getattr(args, "skip_existing", False),
        fresh=getattr(args, "fresh", False),
        tags=tags_list,
        on_progress=on_progress,
    )

    if pbar is not None:
        pbar.close()

    elapsed = time.time() - start_time

    if getattr(args, "json", False):
        import json
        out_data = {
            "folder": str(folder),
            "output_dir": str(summary["output_dir"]),
            "summary": {
                "total": summary["total"],
                "processed": summary["processed"],
                "success": summary["success"],
                "errors": summary["errors"],
                "elapsed_seconds": round(elapsed, 2),
            },
            "results": [
                {
                    "video": r.video.rel_str,
                    "output": str(r.output_path) if r.output_path else "",
                    "status": r.status,
                    "error": r.error or "",
                }
                for r in summary["results"]
            ],
        }
        print(json.dumps(out_data, indent=2))
        return 0

    if not getattr(args, "quiet", False):
        print(f"\n{c.BOLD}Extraction Summary ({elapsed:.1f}s):{c.RESET}")
        print(f"  • Videos Processed : {summary['processed']} / {summary['total']}")
        print(f"  • Frames Extracted : {c.GREEN}{summary['success']}{c.RESET}")
        if summary["errors"] > 0:
            print(f"  • Extraction Errors: {c.RED}{summary['errors']}{c.RESET}")
        print(f"  • Output Directory : {summary['output_dir']}\n")

    return 0
