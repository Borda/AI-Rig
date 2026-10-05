#!/usr/bin/env python3
"""Compatibility shim forwarding the legacy ``bin._rwgate`` import to :mod:`codemap_py.rwgate`.

Every prior consumer, including cross-process workers that add this directory to ``sys.path`` and import the shim,
reaches the one authoritative implementation: this shim prepends ``<plugin-root>/src`` to the process import path, then
replaces its own entry in ``sys.modules`` with the real package module, so private-internal monkeypatches
(``_RELEASE_TIMEOUT``, ``_registry_for``, ...) mutate the real gate state, not a divergent copy.

consumers: tests — imported as bare ``_rwgate``; not a standalone executable
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from codemap_py import rwgate as _impl

sys.modules[__name__] = _impl
