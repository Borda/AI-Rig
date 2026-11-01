"""Pytest fixtures for develop plugin bin/ tests.

Adds ``plugins/cc_develop/bin/`` to ``sys.path`` so test modules can import the script directly, and this ``tests/``
directory so they can import the ``_develop_text`` helper (also from its subdirectories).
"""

from __future__ import annotations

import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_BIN_DIR = _TESTS_DIR.parent / "bin"
for _path in (_BIN_DIR, _TESTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
