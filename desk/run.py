#!/usr/bin/env python3
"""Start YM Desk. Run this file -- or `python -m hub` from this directory."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hub.__main__ import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
