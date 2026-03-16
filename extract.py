#!/usr/bin/env python3
"""
extract.py — Extract the best frame from every video in a folder.

Works on Linux and Android Termux. Termux mode is auto-detected.

Usage:
    python extract.py /path/to/folder [options]

Options:
    --output-dir PATH   Where to save frames (default: <folder>/extracted_frames/)
    --format png|jpg    Output format (default: jpg)
    --quality 1-100     JPG quality (default: 95, ignored for PNG)
    --workers N         Parallel workers (default: auto)
    --skip-existing     Skip videos whose frame already exists in output dir
    --fresh             Re-extract everything, ignoring skip list

Examples:
    # Extract from static folder (most common use case)
    python extract.py /videos/static

    # Extract from review folder into a custom output dir
    python extract.py /videos/review --output-dir /videos/review_frames

    # Re-extract only new additions
    python extract.py /videos/static --skip-existing
"""

import argparse
import shutil
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import atexit

try:
    import termios
except ImportError:
    termios = None

try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False

from core import (
    ENV, VIDEO_EXTENSIONS, extract_one_frame,
)

# ─────────────────────────────────────────────
# TERMINAL STATE PROTECTION
# ─────────────────────────────────────────────

_saved_term_attrs = None

def _save_terminal_state():
    global _saved_term_attrs
    if termios is None:
        return
    try:
        _saved_term_attrs = termios.tcgetattr(sys.stdin.fileno())
    except (termios.error, ValueError, OSError):
        pass

def _restore_terminal_state():
    if _saved_term_attrs is not None and termios is not None:
        try:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN,
                              _saved_term_attrs)
        except (termios.error, ValueError, OSError):
            pass

# ─────────────────────────────────────────────
# INTERRUPT HANDLING
# ─────────────────────────────────────────────

_interrupt_event = threading.Event()
_hard_stop_event = threading.Event()
_sig_count       = 0
_sig_lock        = threading.Lock()

def _handle_sigint(sig, frame):
    global _sig_count
    with _sig_lock:
        _sig_count += 1
        if _sig_count == 1:
            print(
                "\n\n⚠️  Interrupt — finishing current extractions then stopping...\n"
                "   Ctrl+C again to force quit immediately.\n"
            )
            _interrupt_event.set()
        else:
            print("\n🛑 Force quit.\n")
            _hard_stop_event.set()
            _restore_terminal_state()
            sys.exit(1)

# ─────────────────────────────────────────────
# PROGRESS BAR
# ─────────────────────────────────────────────

class FallbackBar:
    def __init__(self, total, desc=""):
        self.total = total
        self.desc  = desc
        self.n     = 0

    def update(self, n=1):
        self.n += n
        pct = int(self.n / self.total * 40) if self.total else 0
        bar = "█" * pct + "░" * (40 - pct)
        print(f"\r{self.desc}: [{bar}] {self.n}/{self.total}", end="", flush=True)

    def set_postfix(self, d):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        print()

def make_bar(total, desc):
    if HAS_TQDM:
        return tqdm(total=total, desc=desc, unit="video")
    return FallbackBar(total=total, desc=desc)

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Extract the best frame from every video in a folder.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("folder",
                   help="Folder containing videos")
    p.add_argument("--output-dir",   default=None,
                   help="Where to save frames (default: <folder>/extracted_frames/)")
    p.add_argument("--format",       choices=["png", "jpg"], default="jpg",
                   help="Output format (default: jpg)")
    p.add_argument("--quality",      type=int, default=95,
                   help="JPG quality 1-100 (default: 95)",
                   choices=range(1, 101), metavar="1-100")
    p.add_argument("--workers",      type=int, default=None,
                   help=f"Parallel workers (default: {ENV['max_workers']})")
    p.add_argument("--skip-existing", action="store_true",
                   help="Skip videos whose frame already exists")
    p.add_argument("--fresh", action="store_true",
                   help="Re-extract everything, ignoring skip list")
    return p.parse_args()


def check_dependencies():
    missing = []
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            missing.append(tool)
    try:
        import cv2, numpy  # noqa: F401
    except ImportError as e:
        missing.append(str(e))

    if not missing:
        return

    print("❌ Missing dependencies:")
    for m in missing:
        print(f"   • {m}")
    if ENV["is_termux"]:
        print("\nInstall in Termux:")
        print("   pkg install ffmpeg")
        print("   pip install opencv-python-headless numpy tqdm")
    else:
        print("\nInstall on Linux:")
        print("   sudo apt install ffmpeg")
        print("   pip install opencv-python-headless numpy tqdm")
    sys.exit(1)


def main():
    script_start_time = time.time()
    args = parse_args()

    _save_terminal_state()
    atexit.register(_restore_terminal_state)
    signal.signal(signal.SIGINT, _handle_sigint)

    check_dependencies()

    folder = Path(args.folder).resolve()
    if not folder.is_dir():
        print(f"❌ Not a directory: {folder}")
        sys.exit(1)

    if ENV["is_termux"]:
        print("📱 Termux mode — workers capped at 2")

    output_dir = (
        Path(args.output_dir).resolve() if args.output_dir
        else folder / "extracted_frames"
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    requested = args.workers or ENV["max_workers"]
    workers   = min(requested, ENV["max_workers"])
    if args.workers is not None and args.workers > workers:
        print(f"⚠️  --workers {args.workers} exceeds platform cap; using {workers}")

    video_files = sorted([
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
    ])

    if not video_files:
        print("No video files found.")
        sys.exit(0)

    if args.skip_existing and not args.fresh:
        to_process = [
            vp for vp in video_files
            if not (output_dir / (vp.stem + f".{args.format}")).exists()
        ]
        skipped_count = len(video_files) - len(to_process)
    else:
        to_process    = video_files
        skipped_count = 0

    print(f"\n🎬 Found {len(video_files)} video(s)")
    if skipped_count:
        print(f"   ⏭  Skipping {skipped_count} already extracted")
    print(f"   🔍 To extract   : {len(to_process)}")
    print(f"   Output dir      : {output_dir}")
    print(f"   Format          : {args.format.upper()}"
          + (f"  quality={args.quality}" if args.format == "jpg" else "  (lossless)"))
    print(f"   Workers         : {workers}")
    print()

    results     = []
    interrupted = False

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {}
        for vp in to_process:
            if _interrupt_event.is_set():
                break
            out_path = output_dir / (vp.stem + f".{args.format}")
            futures[executor.submit(
                extract_one_frame, vp, out_path,
                args.format, args.quality
            )] = vp

        with make_bar(len(futures), "Extracting") as pbar:
            for future in as_completed(futures):
                if _hard_stop_event.is_set():
                    break

                vp = futures[future]
                try:
                    result = future.result()
                except Exception as e:
                    result = {"file": vp.name, "status": f"error: {e}", "output": ""}

                results.append(result)
                pbar.update(1)
                pbar.set_postfix({
                    "file":   result["file"][:22],
                    "status": result["status"],
                })

                if _interrupt_event.is_set():
                    interrupted = True
                    break

    ok      = [r for r in results if r["status"] == "ok"]
    skipped = [r for r in results if r["status"] == "skipped"]
    errors  = [r for r in results if r["status"].startswith("error")]

    print(f"\n── Summary ─────────────────────────────────")
    print(f"  ✅ Extracted  : {len(ok)}")
    print(f"  ⏭  Skipped   : {len(skipped) + skipped_count}")
    print(f"  ❌ Errors     : {len(errors)}")

    def fmt_time(seconds: float) -> str:
        m, s = divmod(seconds, 60)
        h, m = divmod(m, 60)
        if h > 0: return f"{int(h)}h {int(m)}m {s:.1f}s"
        if m > 0: return f"{int(m)}m {s:.1f}s"
        return f"{s:.2f}s"

    script_end_time = time.time()
    total_time_taken = script_end_time - script_start_time

    print(f"  {'─'*32}")
    print(f"  total time    : {fmt_time(total_time_taken)}")

    if errors:
        print("\n  Failed files:")
        for e in errors:
            print(f"    • {e['file']} — {e['status']}")

    if interrupted:
        print("\n⚠️  Interrupted — re-run with --skip-existing to continue.")
    else:
        print(f"\n🖼  Frames saved to: {output_dir}")

    print()


if __name__ == "__main__":
    main()
