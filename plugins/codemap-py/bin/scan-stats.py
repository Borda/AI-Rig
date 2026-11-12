#!/usr/bin/env python
"""scan-stats.py — print a codemap index summary: module, symbol, and call-edge counts.

Resolves the project root (``--root`` inside the ``SCAN_ARGS`` env var → git
toplevel → cwd), loads the codemap index JSON, and prints module/symbol/call
totals plus the five most central modules by reverse-dependency count. The
``--root`` argument is read from the ``SCAN_ARGS`` environment variable, not
from argv; argparse only handles ``-h``/``--help``.

Usage:
    SCAN_ARGS="--root <dir>" scan-stats.py

Output (stdout):
    Human-readable summary lines: module counts, symbol total, optional call
    total, and a ``Most central`` ranking.

Exit codes:
    0 — summary printed (or ``No modules indexed.``)
    1 — index file missing or larger than the size cap
    2 — ``CODEMAP_INDEX_DIR`` cannot be resolved
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

#: DoS guard, held at the query engine's ceiling (``query._MAX_INDEX_SIZE_BYTES``):
#: a helper that refuses an index the engine serves reports a healthy project as broken.
MAX_INDEX_SIZE = 512 * 1024 * 1024
MAX_SCAN_ARGS = 4096  # chars — cap SCAN_ARGS before shlex.split to bound parsing cost (DoS guard)


def _resolve_root(scan_args: str, timeout: int = 15) -> str:
    """Return project root: ``--root`` arg → git toplevel → cwd.

    Explicit roots may be outside CWD, matching the scanner's root selector.

    Raises:
        ValueError: if ``scan_args`` exceeds ``MAX_SCAN_ARGS`` characters — a pathologically large value would make
            ``shlex.split`` expensive. Raised before the suppress block so it is not swallowed.
    """
    if len(scan_args) > MAX_SCAN_ARGS:
        raise ValueError(f"SCAN_ARGS too large ({len(scan_args)} chars); max {MAX_SCAN_ARGS}")
    with contextlib.suppress(Exception):
        args = shlex.split(scan_args) if scan_args else []
        i = args.index("--root")
        root = args[i + 1]
        return str(Path(root).resolve())

    try:
        return (
            subprocess.check_output(
                ["git", "rev-parse", "--show-toplevel"],  # noqa: S607 - argv list, no shell; tool resolved via PATH on purpose
                stderr=subprocess.DEVNULL,
                timeout=timeout,
            )
            .decode()
            .strip()
        )
    except Exception:
        return os.getcwd()


def _load_index(root: str) -> dict:
    """Load a codemap index from the selected cache directory.

    Respects ``CODEMAP_INDEX_DIR`` env var — when set, reads ``$CODEMAP_INDEX_DIR/<proj>.json`` instead of
    ``<root>/.cache/codemap/<proj>.json``. Any resolvable directory is accepted, matching ``resolve_proj_index.py``.
    Exit 1 when the index is missing or oversized, or 2 when the override cannot be resolved.
    """
    proj = os.path.basename(root)
    custom_dir = os.environ.get("CODEMAP_INDEX_DIR")
    if custom_dir:
        try:
            index_dir = str(Path(custom_dir).expanduser().resolve())
        except (OSError, RuntimeError):
            print(
                f"scan-stats: CODEMAP_INDEX_DIR cannot be resolved: {custom_dir!r}",
                file=sys.stderr,
            )
            sys.exit(2)
    else:
        index_dir = os.path.join(root, ".cache", "codemap")
    index_path = os.path.join(index_dir, f"{proj}.json")
    # DoS guard: refuse oversized index files before json.load to avoid memory exhaustion.
    try:
        size = os.path.getsize(index_path)
    except FileNotFoundError:
        print(f"Index not found: {index_path} — run /codemap-py:scan-codebase first")
        sys.exit(1)
    if size > MAX_INDEX_SIZE:
        print(f"Index too large ({size} bytes; max {MAX_INDEX_SIZE}): {index_path}")
        sys.exit(1)
    try:
        with open(index_path) as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"Index not found: {index_path} — run /codemap-py:scan-codebase first")
        sys.exit(1)


#: Seconds allowed for the subprocess that resolves the scan root from ``SCAN_ARGS``.
_DEFAULT_TIMEOUT = 15


def main(argv: list[str] | None = None) -> None:
    """Print codemap index summary: module count, symbols, top central modules.

    The ``--root`` selector is read from the ``SCAN_ARGS`` environment variable
    (see :func:`_resolve_root`), not from argv; argparse handles only
    ``-h``/``--help`` here. Exit codes are raised via ``sys.exit`` in the
    helpers, so this function returns ``None`` on the success path.

    Args:
        argv: Optional argv override (defaults to ``sys.argv[1:]``). Pass ``[]``
            in tests so argparse does not pick up pytest's own argv.

    No doctest — reads SCAN_ARGS env + index file and calls sys.exit; covered by
    pytest with monkeypatch/capsys.
    """
    parser = argparse.ArgumentParser(
        prog="scan-stats.py",
        description="Print a codemap index summary: module, symbol, and call-edge counts.",
    )
    # argparse exits with code 2 on bad/missing args — matches legacy bash contract.
    parser.parse_args(argv)

    root = _resolve_root(os.environ.get("SCAN_ARGS", ""), timeout=_DEFAULT_TIMEOUT)
    d = _load_index(root)

    ok = [m for m in d["modules"] if m.get("status") == "ok"]
    deg = [m for m in d["modules"] if m.get("status") == "degraded"]

    if not ok:
        print("No modules indexed.")
        sys.exit(0)

    top = sorted(ok, key=lambda m: m.get("rdep_count", 0), reverse=True)[:5]
    total_syms = sum(len(m.get("symbols", [])) for m in ok)
    total_calls = sum(len(s.get("calls", [])) for m in ok for s in m.get("symbols", []))

    print(f"Modules: {len(ok)} indexed, {len(deg)} degraded")
    print(f"Symbols: {total_syms} (functions, classes, methods)")
    if total_calls:
        print(f"Calls:   {total_calls} resolved call edges (v3 index)")
    print("Most central (by rdep_count):")
    for m in top:
        print(f"  {m.get('rdep_count', 0):>3}  {m['name']}")


if __name__ == "__main__":
    main()
