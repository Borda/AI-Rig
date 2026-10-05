#!/usr/bin/env python3
"""Compatibility shim forwarding legacy exclusions imports to :mod:`codemap_py.scanner`.

``scan-query`` imports this bare module name after inserting ``bin/`` onto its own ``sys.path``; this shim prepends
``<plugin-root>/src`` to the process import path, then replaces its own entry in ``sys.modules`` with the real package
module so every attribute access — including the exclusion parsers/matchers a test monkeypatches — reaches the one
authoritative implementation. The exclusion rules (``SKIP_DIRS``, ``Exclusions``, ``_load_exclusions``,
``_match_exclusion``, ``is_excluded``, ``load_src_roots``) moved into :mod:`codemap_py.scanner` alongside the rest of
the file-discovery/parsing code that scan-index's writer side owns; the reader (scan-query) must apply the SAME rules,
so this shim keeps it pointed at the one implementation.

consumers: bin/scan-query — imported as bare ``_exclusions``; not a standalone executable
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from codemap_py import scanner as _impl

sys.modules[__name__] = _impl
