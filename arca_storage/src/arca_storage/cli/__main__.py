#!/usr/bin/env python3
"""
Entry point for arca CLI tool.
"""

import sys

from arca_storage.cli.cli import main

if __name__ == "__main__":
    sys.exit(main())
