"""
Entrypoint for `python -m static_sorter`.
"""
import sys
from static_sorter.cli.parser import main

if __name__ == "__main__":
    sys.exit(main())
