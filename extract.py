#!/usr/bin/env python3
"""
extract.py — Legacy CLI wrapper for StaticVideoSorter frame extraction.
Forwards arguments to `static-sorter extract`.
"""
import sys
from static_sorter.cli.parser import main

if __name__ == "__main__":
    sys.exit(main(["extract"] + sys.argv[1:]))
