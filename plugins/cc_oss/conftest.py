"""Make release script siblings importable during plugin doctest collection.

Pytest's importlib mode does not add ``bin/`` to ``sys.path`` when collecting scripts directly. The plugin's
``tests/conftest.py`` applies only to tests, so release scripts need this plugin-scoped collection setup.
"""

from __future__ import annotations

import sys
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent / "bin"
if str(_BIN_DIR) not in sys.path:
    sys.path.insert(0, str(_BIN_DIR))
