"""
Handler for the `report` subcommand.
"""
import csv
import json
import sys
from pathlib import Path
from typing import Any, List, Dict

from static_sorter.cli.ui import (
    Colors,
    print_banner,
    print_decision_badge,
    print_table,
    format_bytes,
)
from static_sorter.core.config import LOG_FILENAME


def execute(args: Any) -> int:
    """Analyze and display detection log summary."""
    c = Colors
    target = Path(args.target).resolve()

    csv_path = target if target.is_file() else (target / LOG_FILENAME)
    if not csv_path.is_file():
        sys.stderr.write(f"❌ Detection log not found at: {csv_path}\n")
        return 1

    rows: List[Dict[str, str]] = []
    try:
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for r in reader:
                rows.append(r)
    except Exception as e:
        sys.stderr.write(f"❌ Failed to parse CSV log: {e}\n")
        return 1

    if not rows:
        print("Log file is empty.")
        return 0

    counts = {"static": 0, "dynamic": 0, "review": 0, "errors": 0}
    for r in rows:
        dec = r.get("decision", "dynamic").lower()
        counts[dec] = counts.get(dec, 0) + 1
        if r.get("error"):
            counts["errors"] += 1

    if getattr(args, "json", False):
        print(json.dumps({
            "log_file": str(csv_path),
            "total_records": len(rows),
            "counts": counts,
            "records": rows,
        }, indent=2))
        return 0

    print_banner(
        "StaticVideoSorter — Audit Log Report",
        f"Log: {csv_path} | Total Records: {len(rows)}",
    )

    print(f"{c.BOLD}Classification Breakdown:{c.RESET}")
    print(f"  • {c.GREEN}Static Videos   : {counts['static']}{c.RESET}")
    print(f"  • {c.BLUE}Dynamic Videos  : {counts['dynamic']}{c.RESET}")
    print(f"  • {c.YELLOW}Review Videos   : {counts['review']}{c.RESET}")
    if counts["errors"] > 0:
        print(f"  • {c.RED}Errors Reported : {counts['errors']}{c.RESET}")

    # Optional table list if requested or limited
    if getattr(args, "show_all", False) or len(rows) <= 25:
        print(f"\n{c.BOLD}Recorded Videos:{c.RESET}")
        headers = ["Filename", "Duration", "Motion", "Zones", "Confidence", "Decision"]
        table_rows = []
        for r in rows:
            table_rows.append([
                r.get("filename", "unknown"),
                f"{r.get('duration_s', '0')}s",
                r.get("global_motion_score", "0"),
                r.get("active_zone_ratio", "0"),
                r.get("final_confidence", "0"),
                print_decision_badge(r.get("decision", "dynamic")),
            ])
        print_table(headers, table_rows, align_right=[1, 2, 3, 4])

    print()
    return 0
