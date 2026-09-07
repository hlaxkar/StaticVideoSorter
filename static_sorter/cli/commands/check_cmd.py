"""
Handler for the `check` subcommand.
"""
import sys
from typing import Any

from static_sorter.cli.ui import Colors, print_banner
from static_sorter.utils.system import check_system_dependencies


def execute(args: Any) -> int:
    """Run system dependency diagnostics."""
    c = Colors
    print_banner("StaticVideoSorter Environment Diagnostic")

    all_ok, details, missing = check_system_dependencies()

    print("System Dependencies Check:")
    for comp, found in details.items():
        status = f"{c.GREEN}✓ FOUND{c.RESET}" if found else f"{c.RED}✗ MISSING{c.RESET}"
        print(f"  • {comp.ljust(20)}: {status}")

    print()
    if all_ok:
        print(f"{c.GREEN}{c.BOLD}✓ All dependencies are satisfied! System is ready.{c.RESET}\n")
        return 0
    else:
        print(f"{c.RED}{c.BOLD}✗ Missing required dependencies:{c.RESET}")
        for m in missing:
            print(f"   - {m}")
        print(f"\n{c.YELLOW}Installation Instructions:{c.RESET}")
        print("  sudo apt install ffmpeg")
        print("  pip install opencv-python-headless numpy tqdm\n")
        return 1
