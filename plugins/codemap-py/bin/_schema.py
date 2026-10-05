#!/usr/bin/env python3
"""Compatibility shim forwarding the legacy schema import to :mod:`codemap_py.schema`.

``scan-index``/``scan-query`` import this bare module name after inserting ``bin/`` onto their own ``sys.path``; this
shim prepends ``<plugin-root>/src`` to the process import path, then replaces its own entry in ``sys.modules`` with the
real package module so every attribute access — including private internals a test monkeypatches — reaches the one
authoritative implementation.

consumers: bin/scan-index, bin/scan-query — imported as bare ``_schema``; not a standalone executable
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from codemap_py import schema as _impl

sys.modules[__name__] = _impl
