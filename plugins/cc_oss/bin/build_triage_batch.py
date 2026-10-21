#!/usr/bin/env python3
"""build_triage_batch.py — turn thread-extracted identifiers into a codemap-py query batch spec.

consumers: analyse/modes/codemap-signals.md

Reads a candidate file (one identifier per line, written by oss:analyse thread
triage) and emits a JSON array of codemap-py query batch queries: dotted module-shaped
identifiers map to ``rdeps`` (with ``--exclude-tests``); bare symbol names map to
an anchored ``find-symbol`` lookup. The resulting file is fed to
``codemap-py query batch`` for a one-process existence check that flags stale symbols.
Prints the number of queries written to stdout so the caller can skip the
``codemap-py query`` call when the batch is empty — no second inline interpreter call.

Usage:
    N=$(python build_triage_batch.py CANDIDATE_FILE OUT_FILE)

Exit codes:
    0 — on success (OUT_FILE written, even when empty → ``[]``)
    1 — missing argument or I/O error
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

#: Candidate cap matching the skill's 8-candidate extraction rule; bounds work on hostile thread text.
_MAX_CANDIDATES = 8
#: Longest identifier accepted; anything longer is not a real module or symbol name.
_MAX_IDENT_LEN = 200


def build_queries(identifiers: list[str]) -> list[dict[str, object]]:
    """Map extracted identifiers to codemap-py query batch entries.

    A dotted identifier whose first segment is a valid Python name is treated as a
    module and existence-checked with ``rdeps``; anything else is treated as a bare
    symbol and checked with an anchored ``find-symbol`` regex.

    Args:
        identifiers: Candidate module/symbol names extracted from a thread.

    Returns:
        List of ``{"cmd", "args"}`` dicts suitable for ``codemap-py query batch``.

    Examples:
        >>> queries = build_queries(["a.b.c", "MyClass"])
        >>> queries[0]
        {'cmd': 'rdeps', 'args': ['a.b.c', '--exclude-tests']}
        >>> queries[1]
        {'cmd': 'find-symbol', 'args': ['^MyClass$', '--limit', '1']}
        >>> build_queries(["  ", "x"])
        [{'cmd': 'find-symbol', 'args': ['^x$', '--limit', '1']}]
        >>> build_queries(["--help", "x"])
        [{'cmd': 'find-symbol', 'args': ['^x$', '--limit', '1']}]
    """
    queries: list[dict[str, object]] = []
    for raw in identifiers:
        ident = raw.strip()
        # identifiers come from untrusted issue/PR text: skip oversized or option-like tokens, escape regex syntax
        if not ident or len(ident) > _MAX_IDENT_LEN or ident.startswith("-"):
            continue
        if "." in ident and all(part.isidentifier() for part in ident.split(".")):
            queries.append({"cmd": "rdeps", "args": [ident, "--exclude-tests"]})
        else:
            queries.append({"cmd": "find-symbol", "args": [f"^{re.escape(ident)}$", "--limit", "1"]})
        if len(queries) >= _MAX_CANDIDATES:
            break
    return queries


def main(argv: list[str] | None = None) -> int:
    """Entry point.

    Args:
        argv: Argument list (defaults to sys.argv[1:]).

    Returns:
        Exit code: 0 on success, 1 on wrong count/I/O error; argparse exits 2 on bad ``-h``/unknown flag.
    """
    parser = argparse.ArgumentParser(
        prog="build_triage_batch.py",
        description="Turn thread-extracted identifiers into a codemap-py query batch spec.",
    )
    # nargs="*" keeps the legacy exit-1-on-wrong-count contract (vs argparse's exit 2).
    parser.add_argument("paths", nargs="*", help="CANDIDATE_FILE OUT_FILE (2 paths).")
    args = parser.parse_args(argv)

    if len(args.paths) != 2:
        print("Usage: build_triage_batch.py CANDIDATE_FILE OUT_FILE", file=sys.stderr)
        return 1
    cand_path, out_path = Path(args.paths[0]), Path(args.paths[1])
    try:
        lines = cand_path.read_text().splitlines()
    except OSError as exc:
        print(f"! build_triage_batch: cannot read {cand_path}: {exc}", file=sys.stderr)
        return 1
    queries = build_queries(lines)
    try:
        out_path.write_text(json.dumps(queries))
    except OSError as exc:
        print(f"! build_triage_batch: cannot write {out_path}: {exc}", file=sys.stderr)
        return 1
    print(len(queries))
    return 0


if __name__ == "__main__":
    sys.exit(main())
