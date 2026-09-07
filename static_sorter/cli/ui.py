"""
Terminal formatting, ANSI styling, and progress helpers.
"""
import sys
from typing import Optional, List, Dict, Any

try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False


class Colors:
    """ANSI Color constants."""
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"

    @classmethod
    def strip_if_not_tty(cls, text: str) -> str:
        if not sys.stdout.isatty():
            # Basic escape sequence stripping
            import re
            return re.sub(r"\033\[[0-9;]*m", "", text)
        return text


def print_banner(title: str, subtitle: Optional[str] = None):
    """Print a modern styled section banner."""
    c = Colors
    line = "━" * 50
    print(f"\n{c.BOLD}{c.CYAN}{line}{c.RESET}")
    print(f"{c.BOLD}{c.WHITE}  {title}{c.RESET}")
    if subtitle:
        print(f"{c.DIM}  {subtitle}{c.RESET}")
    print(f"{c.BOLD}{c.CYAN}{line}{c.RESET}\n")


def print_decision_badge(decision: str) -> str:
    """Format a colored status badge for a decision."""
    c = Colors
    if decision == "static":
        return f"{c.GREEN}{c.BOLD}[STATIC]{c.RESET}"
    if decision == "dynamic":
        return f"{c.BLUE}{c.DIM}[DYNAMIC]{c.RESET}"
    if decision == "review":
        return f"{c.YELLOW}{c.BOLD}[REVIEW]{c.RESET}"
    return f"{c.RED}[{decision.upper()}]{c.RESET}"


def format_bytes(num_bytes: int) -> str:
    """Format bytes into human-readable size."""
    val = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < 1024.0 or unit == "TB":
            return f"{val:.1f} {unit}"
        val /= 1024.0
    return f"{num_bytes} B"


def print_table(headers: List[str], rows: List[List[Any]], align_right: Optional[List[int]] = None):
    """Print a clean ASCII table."""
    c = Colors
    if not rows:
        print("  (No items)")
        return

    align_right = align_right or []
    str_rows = [[str(cell) for cell in row] for row in rows]
    cols = len(headers)
    col_widths = [len(h) for h in headers]

    for row in str_rows:
        for i, cell in enumerate(row):
            if i < cols:
                col_widths[i] = max(col_widths[i], len(cell))

    # Header
    header_str = " | ".join(
        h.rjust(col_widths[i]) if i in align_right else h.ljust(col_widths[i])
        for i, h in enumerate(headers)
    )
    separator = "-+-".join("-" * col_widths[i] for i in range(cols))

    print(f"  {c.BOLD}{header_str}{c.RESET}")
    print(f"  {c.DIM}{separator}{c.RESET}")

    for row in str_rows:
        row_str = " | ".join(
            (row[i].rjust(col_widths[i]) if i in align_right else row[i].ljust(col_widths[i]))
            if i < len(row) else "".ljust(col_widths[i])
            for i in range(cols)
        )
        print(f"  {row_str}")
    print()


class SimpleProgressBar:
    """Clean fallback progress bar when tqdm is absent."""

    def __init__(self, total: int, desc: str = "Processing"):
        self.total = max(1, total)
        self.desc = desc
        self.current = 0

    def update(self, n: int = 1):
        self.current += n
        pct = (self.current / self.total) * 100.0
        bar_len = 30
        filled = int((self.current / self.total) * bar_len)
        bar = "█" * filled + "░" * (bar_len - filled)
        sys.stdout.write(
            f"\r{self.desc}: [{bar}] {self.current}/{self.total} ({pct:.1f}%)"
        )
        sys.stdout.flush()

    def close(self):
        sys.stdout.write("\n")
        sys.stdout.flush()
