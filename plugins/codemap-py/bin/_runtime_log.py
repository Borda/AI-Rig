#!/usr/bin/env python3
"""Compatibility shim forwarding the legacy runtime-log import to :mod:`codemap_py.runtime_log`.

Every prior consumer imports the bare ``_runtime_log`` name after inserting ``bin/`` onto its own ``sys.path``; this
shim prepends ``<plugin-root>/src`` to the process import path, then replaces its own entry in ``sys.modules`` with the
real package module so every attribute access reaches the one authoritative implementation.

consumers: tests — imported as bare ``_runtime_log``; not a standalone executable
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from codemap_py import runtime_log as _impl

sys.modules[__name__] = _impl
