#!/usr/bin/env python3
"""scripts/codemap_py_cli.py — compatibility shim for :mod:`codemap_py.cli`.

``scripts/codemap_py_entry.py`` imports :mod:`codemap_py.cli` directly; this shim exists only so consumers that import
the bare ``codemap_py_cli`` name after inserting ``scripts/`` onto their own
``sys.path`` (tests, an editable checkout) keep working. It prepends
``<plugin-root>/src`` to the process import path, then replaces its own entry
in ``sys.modules`` with the real package module so every attribute access —
including ``is_supported``/``candidate_interpreters``/``resolve_interpreter``
tests exercise directly — reaches the one authoritative implementation.

consumers: tests — imported as bare ``codemap_py_cli``; not a standalone executable
"""

from __future__ import annotations

import sys
from pathlib import Path

#: Plugin ``src`` directory, put on ``sys.path`` so the launcher can import the ``codemap_py`` package.
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from codemap_py import cli as _impl  # noqa: E402  (needs the sys.path insert above)

sys.modules[__name__] = _impl
