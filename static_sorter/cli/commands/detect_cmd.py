"""
Handler for the `detect` subcommand.
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
    print_table,
    SimpleProgressBar,
    HAS_TQDM,
)
from static_sorter.core.config import DEFAULT_MAX_WORKERS
from static_sorter.core.models import DetectionResult
from static_sorter.core.pipeline import PipelineOrchestrator
from static_sorter.utils.system import (
    TerminalStateGuard,
    GracefulInterruptHandler,
    check_system_dependencies,
)

if HAS_TQDM:
    from tqdm import tqdm


def execute(args: Any) -> int:
    """Execute detection command."""
    c = Colors
    start_time = time.time()

    # 1. Dependency check
    all_ok, _, missing = check_system_dependencies()
    if not all_ok:
        sys.stderr.write(f"❌ Missing dependencies: {', '.join(missing)}\n")
        return 1

    # 2. Terminal protection & signals
    term_guard = TerminalStateGuard()
    term_guard.save()
    atexit.register(term_guard.restore)

    interrupt_handler = GracefulInterruptHandler()
    interrupt_handler.install()

    folder = Path(args.folder).resolve()
    if not folder.is_dir():
        sys.stderr.write(f"❌ Target path is not a directory: {folder}\n")
        return 1

    if not getattr(args, "quiet", False):
        print_banner(
            "StaticVideoSorter — Video Classification",
            f"Folder: {folder} | Sensitivity: {args.sensitivity} | Workers: {args.workers or DEFAULT_MAX_WORKERS}",
        )

    # 3. Setup progress bar
    progress_bar = None
    if not getattr(args, "quiet", False):
        if HAS_TQDM:
            # We'll initialize tqdm inside the callback once total is known
            pbar = None
        else:
            pbar = None

    def on_progress(res: DetectionResult, current: int, total: int):
        nonlocal pbar
        if getattr(args, "quiet", False):
            return

        if HAS_TQDM:
            if pbar is None:
                pbar = tqdm(total=total, desc="Analyzing videos", unit="vid")
            pbar.update(1)
        else:
            if pbar is None:
                pbar = SimpleProgressBar(total=total, desc="Analyzing videos")
            pbar.update(1)

    orchestrator = PipelineOrchestrator(interrupt_handler=interrupt_handler)
    summary = orchestrator.run_detection(
        folder=folder,
        recursive=getattr(args, "recursive", False),
        sensitivity=args.sensitivity,
        workers=args.workers or DEFAULT_MAX_WORKERS,
        move=getattr(args, "move", False),
        fresh=getattr(args, "fresh", False),
        on_progress=on_progress,
    )

    if pbar is not None:
        pbar.close()

    elapsed = time.time() - start_time

    if getattr(args, "json", False):
        import json
        out_data = {
            "folder": str(folder),
            "summary": {
                "total": summary["total"],
                "processed": summary["processed"],
                "static": summary["static"],
                "dynamic": summary["dynamic"],
                "review": summary["review"],
                "errors": summary["errors"],
                "elapsed_seconds": round(elapsed, 2),
            },
            "results": [r.to_log_dict() for r in summary["results"]],
        }
        print(json.dumps(out_data, indent=2))
        return 0

    if not getattr(args, "quiet", False):
        print(f"\n{c.BOLD}Detection Summary ({elapsed:.1f}s):{c.RESET}")
        print(f"  • Total Videos Discovered : {summary['total']}")
        if summary.get("cached", 0) > 0:
            print(f"  • Processed in this run   : {summary['processed']} (Cached: {summary['cached']})")
        else:
            print(f"  • Processed in this run   : {summary['processed']}")
        print(f"  • {c.GREEN}Static Videos (Target)  : {summary['static']}{c.RESET}")
        print(f"  • {c.BLUE}Dynamic Videos          : {summary['dynamic']}{c.RESET}")
        print(f"  • {c.YELLOW}Review (Borderline)     : {summary['review']}{c.RESET}")
        if summary["errors"] > 0:
            print(f"  • {c.RED}Errors                  : {summary['errors']}{c.RESET}")

        if getattr(args, "move", False) and summary["moved_paths"]:
            print(f"\n{c.GREEN}✓ Videos sorted into static/, dynamic/, and review/ subfolders (preserving directory tree).{c.RESET}")

        if getattr(args, "report", False) and summary["results"]:
            print(f"\n{c.BOLD}Per-Video Report:{c.RESET}")
            headers = ["Filename", "Duration", "Motion", "Zones", "Confidence", "Decision"]
            rows = []
            for r in summary["results"]:
                rows.append([
                    r.video.rel_str,
                    f"{r.metadata.duration_s:.1f}s",
                    f"{r.global_motion_score:.1f}",
                    f"{r.active_zone_ratio:.2f}",
                    f"{r.final_confidence:.3f}",
                    print_decision_badge(r.decision),
                ])
            print_table(headers, rows, align_right=[1, 2, 3, 4])

        print()

    return 0
