#!/usr/bin/env python
"""Build the review pre-flight ``codemap-py query batch`` request for changed modules.

Derives changed modules from ``git diff HEAD --name-only`` (reusing the
``codemap_scan.py`` mapping: strip ``./``/``src/``/``.py``, ``/`` → ``.``, map
``__init__`` to its package) and writes one
JSON array with ``central --top 5`` plus the five per-module pre-flight queries.
One ``codemap-py query batch`` process then shares a single coverage block instead of
paying the per-call spawn + coverage cost 5×N times.

Extracted from an inline bash+python heredoc in the review SKILL — heredoc
python in skill bodies is banned (audit Check 23a/30e); ``bin/*.py`` is the
sanctioned home for this transform.

Usage:
    build_codemap_batch.py <out.json> [--modules "m1 m2 ..."] [--queries rdeps,uncovered]

Flags:
    ``--modules`` — space-separated dotted module names; skips git-diff
    derivation entirely (caller already knows its scope, e.g. the refactor
    skill's AFFECTED_MODULES list).
    ``--queries`` — comma-separated subset of the per-module query families
    (``rdeps``, ``mock-rdeps``, ``uncovered``, ``xrefs``, ``undocumented``).
    When given, the ``central`` baseline item is omitted too — the caller asked
    for exactly those queries. Unknown names exit 1 with a message.

Output (stdout):
    Batch request JSON written to ``<out.json>``; derived module names printed
    space-separated on stdout (empty line when no ``.py`` files changed).

Exit codes:
    0 — success (including zero changed modules: writes ``central``-only request).
    1 — missing required output-path argument.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Sibling import: resolved by Python's script-dir sys.path entry on direct execution,
# and by plugins/cc_develop/conftest.py during pytest --doctest-modules collection.
from codemap_scan import _git_diff_files, derive_modules_from_diff

FALLBACK_LIMIT = 10

# fn-rdeps/fn-blast are NOT in this set: they require `module::fn` qnames and a
# name-only diff yields bare modules — every such batch item failed "Symbol not
# found" in production (2026-07 usage audit). Function-level queries return once
# Derive qualified names from diff hunks before resolving the affected symbols.
PER_MODULE_QUERIES: tuple[tuple[str, ...], ...] = (
    ("rdeps",),  # importer count → risk tier
    ("mock-rdeps",),
    ("uncovered", "--top", "20"),
    ("xrefs", "--broken"),
    ("undocumented",),
)


def build_batch_request(modules: list[str]) -> list[dict[str, object]]:
    """Assemble the ordered batch request: one ``central`` plus five queries per module.

    Args:
        modules: Dotted module names derived from the diff (possibly empty).

    Returns:
        List of ``{"cmd": ..., "args": [...]}`` items in codemap-py query batch order.

    Examples:
        >>> req = build_batch_request(["pkg.mod"])
        >>> req[0]
        {'cmd': 'central', 'args': ['--top', '5']}
        >>> len(req)
        6
        >>> req[1]
        {'cmd': 'rdeps', 'args': ['pkg.mod']}
        >>> req[3]["args"]
        ['--top', '20', 'pkg.mod']
        >>> build_batch_request([])
        [{'cmd': 'central', 'args': ['--top', '5']}]
    """
    items: list[dict[str, object]] = [{"cmd": "central", "args": ["--top", "5"]}]
    for mod in modules:
        for cmd, *flags in PER_MODULE_QUERIES:
            args = [*flags, mod] if cmd == "uncovered" else [mod, *flags]
            items.append({"cmd": cmd, "args": args})
    return items


def build_filtered_request(modules: list[str], queries: list[str]) -> list[dict[str, object]]:
    """Assemble a batch request restricted to the named per-module query families.

    The ``central`` baseline item is deliberately omitted — a caller passing an
    explicit query list asked for exactly those queries.

    Args:
        modules: Dotted module names supplied by the caller.
        queries: Per-module query family names (``cmd`` values from
            :data:`PER_MODULE_QUERIES`).

    Returns:
        List of ``{"cmd": ..., "args": [...]}`` items in codemap-py query batch order.

    Raises:
        ValueError: If any name in ``queries`` is not a known query family.

    Examples:
        >>> build_filtered_request(["pkg.mod"], ["rdeps"])
        [{'cmd': 'rdeps', 'args': ['pkg.mod']}]
        >>> build_filtered_request([], ["rdeps"])
        []
        >>> build_filtered_request(["pkg.mod"], ["bogus"])
        Traceback (most recent call last):
        ...
        ValueError: unknown query family: bogus
    """
    known = {spec[0]: spec for spec in PER_MODULE_QUERIES}
    for name in queries:
        if name not in known:
            raise ValueError(f"unknown query family: {name}")
    items: list[dict[str, object]] = []
    for mod in modules:
        for name in queries:
            cmd, *flags = known[name]
            args = [*flags, mod] if cmd == "uncovered" else [mod, *flags]
            items.append({"cmd": cmd, "args": args})
    return items


def main(argv: list[str] | None = None) -> int:
    """Derive changed modules, write the batch request JSON, print the module list.

    Args:
        argv: Optional argv override (defaults to ``sys.argv[1:]``).

    Returns:
        ``0`` on success; ``1`` when the output-path argument is missing.

    No doctest — writes a file and shells out to git; covered by pytest with monkeypatch.
    """
    parser = argparse.ArgumentParser(
        prog="build_codemap_batch.py",
        description="Build the review pre-flight codemap-py query batch request for changed modules.",
    )
    # nargs="?" (not a plain required positional) so a missing path returns exit 1 — argparse's
    # own missing-required exit is 2, but callers and tests rely on the legacy exit-1 contract.
    parser.add_argument("out_json", nargs="?", help="Output path for the batch request JSON.")
    parser.add_argument(
        "--modules",
        help="Space-separated dotted module names; skips git-diff derivation.",
    )
    parser.add_argument(
        "--queries",
        help="Comma-separated per-module query families; omits the central baseline item.",
    )
    args = parser.parse_args(argv)
    if args.out_json is None:
        print("usage: build_codemap_batch.py <out.json> [--modules ...] [--queries ...]", file=sys.stderr)
        return 1
    if args.modules is not None:
        modules = args.modules.split()
    else:
        modules = derive_modules_from_diff(_git_diff_files(), limit=FALLBACK_LIMIT)
    if args.queries is not None:
        try:
            request = build_filtered_request(modules, args.queries.split(","))
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    else:
        request = build_batch_request(modules)
    Path(args.out_json).write_text(json.dumps(request), encoding="utf-8")
    print(" ".join(modules))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
