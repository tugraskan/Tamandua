#!/usr/bin/env python3
"""Generate and install the SWAT+ source index.

Thin wrapper over :mod:`tamandua.index.cli`, which holds the real
implementation so an installed package, ``python -m``, and the git hooks all
behave identically. Run ``swatplus-build --help`` for the options.
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ is None and __name__ == "__main__":  # running as a script
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tamandua.index.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
