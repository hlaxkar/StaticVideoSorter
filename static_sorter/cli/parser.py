"""
Main CLI argument parsing and subcommand dispatcher.
"""
import argparse
import sys
from typing import List, Optional

from static_sorter import __version__
from static_sorter.cli.commands import (
    check_cmd,
    detect_cmd,
    extract_cmd,
    pipeline_cmd,
    report_cmd,
)
from static_sorter.core.config import (
    DEFAULT_MAX_WORKERS,
    DEFAULT_IMAGE_FORMAT,
    DEFAULT_IMAGE_QUALITY,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="static-sorter",
        description="Industrial-grade CLI to detect static videos, organize media, and extract best frames.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-V", "--version",
        action="version",
        version=f"static-sorter {__version__}",
    )

    subparsers = parser.add_subparsers(
        title="Commands",
        dest="command",
        metavar="<command>",
        required=True,
    )

    # ─────────────────────────────────────────────
    # PIPELINE COMMAND
    # ─────────────────────────────────────────────
    p_pipeline = subparsers.add_parser(
        "pipeline",
        help="End-to-end classification, organization, and frame extraction in one pass.",
        description="Classify videos, move/organize them into categorized folders (preserving directory structure), and extract high-resolution stills for static videos.",
    )
    p_pipeline.add_argument("folder", help="Folder containing videos to process")
    p_pipeline.add_argument("-r", "--recursive", action="store_true", help="Recursively scan subdirectories")
    p_pipeline.add_argument("--no-move", dest="move", action="store_false", default=True, help="Do not move classified videos (dry-run organization)")
    p_pipeline.add_argument("--sensitivity", choices=["low", "medium", "high"], default="medium", help="Detection aggressiveness (default: medium)")
    p_pipeline.add_argument("--workers", type=int, default=DEFAULT_MAX_WORKERS, help=f"Parallel worker threads (default: {DEFAULT_MAX_WORKERS})")
    p_pipeline.add_argument("--extract-dir", help="Custom output directory for extracted stills")
    p_pipeline.add_argument("--format", choices=["jpg", "png"], default=DEFAULT_IMAGE_FORMAT, help="Image format (default: jpg)")
    p_pipeline.add_argument("--quality", type=int, default=DEFAULT_IMAGE_QUALITY, help="JPG quality 1-100 (default: 95)")
    p_pipeline.add_argument("--tags", help="Comma-separated keywords/tags to embed into extracted image EXIF/metadata (default: 'static-video,extracted-frame')")
    p_pipeline.add_argument("--fresh", action="store_true", help="Ignore checkpoint, process from scratch")
    p_pipeline.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    p_pipeline.add_argument("-q", "--quiet", action="store_true", help="Suppress progress output")

    # ─────────────────────────────────────────────
    # DETECT COMMAND
    # ─────────────────────────────────────────────
    p_detect = subparsers.add_parser(
        "detect",
        help="Classify videos as static, dynamic, or review.",
        description="Run 3-layer detection on videos in a folder and generate detection_log.csv audit log.",
    )
    p_detect.add_argument("folder", help="Folder containing videos to classify")
    p_detect.add_argument("-r", "--recursive", action="store_true", help="Recursively search subdirectories")
    p_detect.add_argument("--move", action="store_true", help="Move classified videos into static/ dynamic/ review/ (preserving structure)")
    p_detect.add_argument("--sensitivity", choices=["low", "medium", "high"], default="medium", help="Detection aggressiveness (default: medium)")
    p_detect.add_argument("--workers", type=int, default=DEFAULT_MAX_WORKERS, help=f"Parallel worker threads (default: {DEFAULT_MAX_WORKERS})")
    p_detect.add_argument("--fresh", action="store_true", help="Ignore checkpoint, re-detect everything")
    p_detect.add_argument("--report", action="store_true", help="Print per-video score table after run")
    p_detect.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    p_detect.add_argument("-q", "--quiet", action="store_true", help="Suppress progress output")

    # ─────────────────────────────────────────────
    # EXTRACT COMMAND
    # ─────────────────────────────────────────────
    p_extract = subparsers.add_parser(
        "extract",
        help="Extract the single best frame from videos in a folder.",
        description="Extract the calmest and sharpest frame from every video, saving to output directory (preserving directory structure).",
    )
    p_extract.add_argument("folder", help="Folder containing videos")
    p_extract.add_argument("-r", "--recursive", action="store_true", help="Recursively search subdirectories")
    p_extract.add_argument("--output-dir", help="Where to save frames (default: <folder>/extracted_frames/)")
    p_extract.add_argument("--format", choices=["jpg", "png"], default=DEFAULT_IMAGE_FORMAT, help="Image format (default: jpg)")
    p_extract.add_argument("--quality", type=int, default=DEFAULT_IMAGE_QUALITY, help="JPG quality 1-100 (default: 95)")
    p_extract.add_argument("--tags", help="Comma-separated keywords/tags to embed into extracted image EXIF/metadata (default: 'static-video,extracted-frame')")
    p_extract.add_argument("--workers", type=int, default=DEFAULT_MAX_WORKERS, help=f"Parallel worker threads (default: {DEFAULT_MAX_WORKERS})")
    p_extract.add_argument("--skip-existing", action="store_true", help="Skip videos whose frame already exists")
    p_extract.add_argument("--fresh", action="store_true", help="Re-extract everything ignoring existing frames")
    p_extract.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    p_extract.add_argument("-q", "--quiet", action="store_true", help="Suppress progress output")

    # ─────────────────────────────────────────────
    # REPORT COMMAND
    # ─────────────────────────────────────────────
    p_report = subparsers.add_parser(
        "report",
        help="Inspect and summarize previous detection audit logs.",
        description="Read detection_log.csv and print classification statistics and space metrics.",
    )
    p_report.add_argument("target", help="Folder or detection_log.csv file path")
    p_report.add_argument("--show-all", action="store_true", help="Display full table of all recorded files")
    p_report.add_argument("--json", action="store_true", help="Output machine-readable JSON")

    # ─────────────────────────────────────────────
    # CHECK COMMAND
    # ─────────────────────────────────────────────
    p_check = subparsers.add_parser(
        "check",
        help="Check environment health and required system dependencies.",
        description="Verify availability of ffmpeg, ffprobe, opencv, numpy, tqdm.",
    )

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    dispatch_map = {
        "pipeline": pipeline_cmd.execute,
        "detect": detect_cmd.execute,
        "extract": extract_cmd.execute,
        "report": report_cmd.execute,
        "check": check_cmd.execute,
    }

    handler = dispatch_map.get(args.command)
    if not handler:
        parser.print_help()
        return 1

    try:
        return handler(args)
    except KeyboardInterrupt:
        sys.stderr.write("\nInterrupted by user.\n")
        return 130
    except Exception as e:
        sys.stderr.write(f"\n❌ Error: {e}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
