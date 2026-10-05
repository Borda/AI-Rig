#!/usr/bin/env python3
"""Compatibility shim forwarding legacy index-identity imports to :mod:`codemap_py.index_paths`.

Every prior consumer imports the bare ``_index_identity`` name after inserting ``bin/`` onto its own ``sys.path``; this
shim prepends ``<plugin-root>/src`` to the process import path, then replaces its own entry in ``sys.modules`` with the
real package module so every attribute access — including private internals a test monkeypatches — reaches the one
authoritative implementation.

consumers: bin/_runtime_log.py (indirectly, via the codemap_py package), tests — imported as bare ``_index_identity``;
not a standalone executable
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from codemap_py import index_paths as _impl

sys.modules[__name__] = _impl
