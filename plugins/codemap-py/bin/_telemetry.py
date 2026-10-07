#!/usr/bin/env python3
"""Compatibility shim forwarding the legacy telemetry import to :mod:`codemap_py.telemetry`.

``scan-index``/``scan-query`` import this bare module name after inserting ``bin/`` onto their own ``sys.path``; this
shim prepends ``<plugin-root>/src`` to the process import path, then replaces its own entry in ``sys.modules`` with the
real package module so every attribute access — including the module-global ``_PLUGIN_VERSION`` cache a test
monkeypatches — reaches the one authoritative implementation.

consumers: bin/scan-index, bin/scan-query — imported as bare ``_telemetry``; not a standalone executable
"""

from __future__ import annotations

import sys
from pathlib import Path

#: Plugin ``src`` directory, put on ``sys.path`` so the shim can import the ``codemap_py`` package.
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from codemap_py import telemetry as _impl  # noqa: E402  (needs the sys.path insert above)

sys.modules[__name__] = _impl
