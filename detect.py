#!/usr/bin/env python3
"""
detect.py — Classify videos as static, dynamic, or review.

Works on Linux and Android Termux. Termux mode is auto-detected.

Usage:
    python detect.py /path/to/folder [options]

Options:
    --move                  Move videos into static/ dynamic/ review/ subfolders
    --sensitivity LEVEL     low | medium | high  (default: medium)
    --workers N             Parallel workers (default: auto)
    --fresh                 Ignore checkpoint, re-detect everything
    --report                Print per-video breakdown after run

Workflow:
    # Step 1: detect only, inspect log
    python detect.py /folder

    # Step 2: move once happy with decisions
    python detect.py /folder --move

    # Then extract frames from whichever folder you want:
    python extract.py /folder/static
"""

import argparse
import csv
import signal
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
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
    ENV, SENSITIVITY_PRESETS, VIDEO_EXTENSIONS,
    LOG_FILENAME, CHECKPOINT_FILENAME, LOG_FIELDS,
    Checkpoint, detect_video, safe_move,
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

import threading

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
                "\n\n⚠️  Interrupt — finishing current videos, then saving checkpoint...\n"
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
# REPORT
# ─────────────────────────────────────────────

def print_report(rows: list[dict]):
    decisions = ["static", "review", "dynamic"]
    grouped   = {d: [] for d in decisions}
    errors    = []

    for r in rows:
        d = r.get("decision", "")
        if d in grouped:
            grouped[d].append(r)
        elif d.startswith("error"):
            errors.append(r)

    for d in decisions:
        group = grouped[d]
        if not group:
            continue
        group.sort(key=lambda r: float(r.get("final_confidence") or 0), reverse=True)
        label = {"static": "🖼  STATIC", "review": "🔍 REVIEW", "dynamic": "🎥 DYNAMIC"}[d]
        print(f"\n{label} ({len(group)})")
        print(f"  {'filename':<45} {'conf':>6}  {'motion':>7}  {'zones':>6}  {'dur':>6}")
        print(f"  {'─'*45} {'─'*6}  {'─'*7}  {'─'*6}  {'─'*6}")
        for r in group:
            print(
                f"  {r['filename']:<45} "
                f"{r.get('final_confidence','?'):>6}  "
                f"{r.get('global_motion_score','?'):>7}  "
                f"{r.get('active_zone_ratio','?'):>6}  "
                f"{r.get('duration_s','?'):>6}s"
            )

    if errors:
        print(f"\n❌ ERRORS ({len(errors)})")
        for r in errors:
            print(f"  {r['filename']} — {r.get('decision','?')}")


def print_summary(rows: list[dict], moved: bool, interrupted: bool,
                  out_dirs: dict, total_time: float = 0.0):
    counts: dict[str, int] = {}
    for r in rows:
        d = r.get("decision", "unknown")
        counts[d] = counts.get(d, 0) + 1

    print("\n── Summary ──────────────────────────────────")
    labels = {
        "static":  "🖼  static ",
        "review":  "🔍 review ",
        "dynamic": "🎥 dynamic",
    }
    for d in ("static", "review", "dynamic"):
        if d in counts:
            print(f"  {labels[d]}   {counts[d]:>5}")
    errors = sum(v for k, v in counts.items() if k.startswith("error"))
    if errors:
        print(f"  ❌ errors     {errors:>5}")
    print(f"  {'─'*30}")
    print(f"  total         {len(rows):>5}")

    def fmt_time(seconds: float) -> str:
        m, s = divmod(seconds, 60)
        h, m = divmod(m, 60)
        if h > 0: return f"{int(h)}h {int(m)}m {s:.1f}s"
        if m > 0: return f"{int(m)}m {s:.1f}s"
        return f"{s:.2f}s"

    print(f"  {'─'*30}")
    print(f"  total time    {fmt_time(total_time):>5}")

    if interrupted:
        print("\n⚠️  Interrupted — checkpoint saved. Re-run to resume.")
    elif not moved:
        print("\n  Videos not moved. Re-run with --move to sort into folders.")
    else:
        for d, path in out_dirs.items():
            if counts.get(d, 0) > 0:
                print(f"\n  {labels.get(d, d)}  →  {path}")
    print()

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Classify videos as static / dynamic / review.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("folder",
                   help="Folder containing videos to analyse")
    p.add_argument("--move",         action="store_true",
                   help="Move classified videos into subfolders")
    p.add_argument("--sensitivity",  choices=["low", "medium", "high"],
                   default="medium")
    p.add_argument("--workers",      type=int, default=None,
                   help=f"Parallel workers (default: {ENV['max_workers']})")
    p.add_argument("--fresh",        action="store_true",
                   help="Ignore checkpoint, re-detect everything")
    p.add_argument("--report",       action="store_true",
                   help="Print per-video breakdown after run")
    p.add_argument("--debug",        action="store_true",
                   help="Print ffmpeg stderr for any video that fails frame extraction")
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
    args   = parse_args()
    _save_terminal_state()
    atexit.register(_restore_terminal_state)
    signal.signal(signal.SIGINT, _handle_sigint)

    check_dependencies()

    folder = Path(args.folder).resolve()
    if not folder.is_dir():
        print(f"❌ Not a directory: {folder}")
        sys.exit(1)

    if ENV["is_termux"]:
        print("📱 Termux mode — workers capped at 2, no hardware acceleration")

    thresholds = SENSITIVITY_PRESETS[args.sensitivity]
    requested  = args.workers or ENV["max_workers"]
    workers    = min(requested, ENV["max_workers"])
    if args.workers is not None and args.workers > workers:
        print(f"⚠️  --workers {args.workers} exceeds platform cap; using {workers}")

    out_static  = folder / "static"
    out_dynamic = folder / "dynamic"
    out_review  = folder / "review"
    out_dirs    = {"static": out_static, "dynamic": out_dynamic, "review": out_review}
    log_path    = folder / LOG_FILENAME
    ckpt_path   = folder / CHECKPOINT_FILENAME

    ckpt = Checkpoint(ckpt_path)
    if args.fresh:
        ckpt.clear()
        print("🔄 Checkpoint cleared — starting fresh.\n")

    ckpt.save_meta(
        sensitivity = args.sensitivity,
        started_at  = datetime.now().isoformat(),
        is_termux   = ENV["is_termux"],
    )

    all_videos = sorted([
        p for p in folder.iterdir()
        if p.is_file()
        and p.suffix.lower() in VIDEO_EXTENSIONS
    ])

    if not all_videos:
        print("No video files found.")
        ckpt.flush_and_stop()
        sys.exit(0)

    to_detect = [
        vp for vp in all_videos
        if not ckpt.is_done(vp.name) or ckpt.is_error(vp.name)
    ]
    already_done = len(all_videos) - len(to_detect)

    print(f"🎬 Found {len(all_videos)} video(s)")
    if already_done:
        print(f"   ✅ Already classified : {already_done}  (use --fresh to redo)")
    print(f"   🔍 To detect          : {len(to_detect)}")
    print(f"   Sensitivity           : {args.sensitivity}")
    print(f"   Workers               : {workers}")
    print(f"   Move after detection  : {'yes' if args.move else 'no (use --move)'}")
    print()

    interrupted = False

    if to_detect:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {}
            for vp in to_detect:
                if _interrupt_event.is_set():
                    break
                futures[executor.submit(detect_video, vp, thresholds, args.debug)] = vp

            with make_bar(len(futures), "Detecting") as pbar:
                for future in as_completed(futures):
                    if _hard_stop_event.is_set():
                        break

                    vp = futures[future]
                    try:
                        row = future.result()
                    except Exception as e:
                        row = {f: "" for f in LOG_FIELDS}
                        row["filename"] = vp.name
                        row["decision"] = f"error: {e}"

                    ckpt.record(vp.name, row)
                    pbar.update(1)
                    pbar.set_postfix({
                        "file":     vp.name[:22],
                        "result":   row.get("decision", "?"),
                    })

                    if _interrupt_event.is_set():
                        interrupted = True
                        break

        ckpt.wait_for_writes()

    if args.move and not interrupted:
        all_rows     = ckpt.all_rows()
        needs_moving = [
            r for r in all_rows
            if r.get("decision") in ("static", "dynamic", "review")
            and (folder / r["filename"]).exists()
        ]

        if needs_moving:
            for d in (out_static, out_dynamic, out_review):
                d.mkdir(exist_ok=True)

            with make_bar(len(needs_moving), "Moving") as pbar:
                for r in needs_moving:
                    if _interrupt_event.is_set():
                        interrupted = True
                        break
                    vp       = folder / r["filename"]
                    decision = r["decision"]
                    dest_dir = out_dirs[decision]
                    try:
                        safe_move(vp, dest_dir)
                    except Exception as e:
                        print(f"\n⚠️  Could not move {vp.name}: {e}")
                    pbar.update(1)

    ckpt.flush_and_stop()
    all_rows = ckpt.all_rows()

    with open(log_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_rows)

    script_end_time = time.time()
    total_time_taken = script_end_time - script_start_time

    print_summary(all_rows, moved=args.move, interrupted=interrupted,
                  out_dirs=out_dirs, total_time=total_time_taken)

    if args.report:
        print_report(all_rows)

    print(f"📄 Log        : {log_path}")
    print(f"💾 Checkpoint : {ckpt_path}")
    print()


if __name__ == "__main__":
    main()
