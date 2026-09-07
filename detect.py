#!/usr/bin/env python3
"""
detect.py — Legacy CLI wrapper for StaticVideoSorter detection.
Forwards arguments to `static-sorter detect`.
"""
import sys
from static_sorter.cli.parser import main

if __name__ == "__main__":
    sys.exit(main(["detect"] + sys.argv[1:]))
