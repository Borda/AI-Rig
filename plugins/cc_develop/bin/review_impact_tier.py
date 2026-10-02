#!/usr/bin/env python
"""review_impact_tier.py — decide review depth from the code path a change sits on, not its size.

Usage::

    codemap-py query diff-impact --diff-file pr.diff | python review_impact_tier.py
    codemap-py query diff-impact | python review_impact_tier.py

Reads ``codemap-py query diff-impact`` JSON from stdin, runs ``codemap-py query fn-blast``
for every changed symbol, and prints one line: ``FULL · reason`` or ``LIGHT · reason``. The
tier is the first word, so a skill reads it with a plain ``IFS= read -r`` sentinel read and
strips the rest with ``${LINE%% *}``; the whole line goes into the report's ``Impact:`` field.

Why path, not size: a few-line change to a private helper on the main feature path can
break the main user story, while a larger tweak to a plotting helper reaches nobody. Line
and file counts cannot tell those apart; the transitive caller graph can.

Tiers:

- ``FULL`` — the change reaches the main path: a changed or transitively calling function
  with a public name (no leading underscore, dunders count as public) in a module that is
  neither a test nor a leaf module, or a changed main-path module with 5+ importers, or a
  module-level change with no mapped symbol in a main-path module.
- ``LIGHT`` — every changed symbol and every transitive caller lives in tests or in leaf
  modules (visualization, plotting, examples, docs, scripts, notebooks), or is private with
  no caller outside them.

Fail-safe: unreadable input, no changed modules, unmapped ``.py`` files, a missing
``codemap-py`` binary, or a failed ``fn-blast`` query all yield ``FULL`` — unknown impact is
never trimmed. Exit code is always 0 so the calling skill block never aborts on it.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import PurePosixPath

#: Path segments marking a module as a leaf: code users look at, not code the product runs on.
LEAF_SEGMENTS = frozenset(
    {
        "viz",
        "vis",
        "visual",
        "visuals",
        "visualize",
        "visualization",
        "visualizations",
        "plot",
        "plots",
        "plotting",
        "example",
        "examples",
        "doc",
        "docs",
        "script",
        "scripts",
        "notebook",
        "notebooks",
    }
)

#: Importer count from which a changed main-path module counts as widely used on its own.
IMPORTER_THRESHOLD = 5

#: Upper bound on fn-blast queries per run; symbols past the cap fail safe to FULL.
#: Matches the review's own fn-rdeps/fn-blast context loop cap, which bounds block wall time.
SYMBOL_CAP = 12

FULL = "FULL"
LIGHT = "LIGHT"

BlastFn = Callable[[str], "dict | None"]


def _segments(path: str) -> list[str]:
    """Split a module path or dotted module name into lowercase segments without extension.

    Examples:
        >>> _segments("src/pkg/viz/draw.py")
        ['src', 'pkg', 'viz', 'draw']
        >>> _segments("pkg.tests.test_x")
        ['pkg', 'tests', 'test_x']
    """
    posix = PurePosixPath(path.replace("\\", "/"))
    parts = list(posix.parts) if "/" in path or "\\" in path else path.split(".")
    if parts and parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]
    return [p.lower() for p in parts if p]


def is_test_module(path: str) -> bool:
    """Return True when the module path belongs to a test suite.

    Only a ``tests`` directory or test-file naming counts. ``testing`` and bare ``test`` are
    deliberately excluded: packages ship public ``pkg.testing`` helpers, and misreading one as
    test code would trim the review of public API — the one direction this must never err in.

    Examples:
        >>> is_test_module("tests/unit/test_core.py")
        True
        >>> is_test_module("src/pkg/conftest.py")
        True
        >>> is_test_module("src/pkg/core.py")
        False
        >>> is_test_module("src/pkg/testing/utils.py")
        False
    """
    parts = _segments(path)
    if "tests" in parts:
        return True
    name = parts[-1] if parts else ""
    return name.startswith("test_") or name.endswith("_test") or name == "conftest"


def is_leaf_module(path: str) -> bool:
    """Return True when the module path sits under a leaf segment such as ``viz`` or ``examples``.

    Examples:
        >>> is_leaf_module("src/pkg/visualization/boxes.py")
        True
        >>> is_leaf_module("src/pkg/plot.py")
        True
        >>> is_leaf_module("src/pkg/models/core.py")
        False
    """
    return any(p in LEAF_SEGMENTS for p in _segments(path))


def is_public_function(qname: str) -> bool:
    """Return True when every name after ``::`` is public; dunder names count as public.

    Examples:
        >>> is_public_function("pkg.mod::run")
        True
        >>> is_public_function("pkg.mod::Model.__call__")
        True
        >>> is_public_function("pkg.mod::_helper")
        False
        >>> is_public_function("pkg.mod::_Private.run")
        False
    """
    _, _, func = qname.partition("::")
    names = [n for n in func.split(".") if n]
    if not names:
        return False
    return all(not n.startswith("_") or (n.startswith("__") and n.endswith("__")) for n in names)


def is_main_path(qname: str, module_path: str) -> bool:
    """Return True when a function is public and lives outside tests and leaf modules.

    Examples:
        >>> is_main_path("pkg.core::run", "src/pkg/core.py")
        True
        >>> is_main_path("pkg.viz::draw", "src/pkg/viz.py")
        False
        >>> is_main_path("pkg.core::_prep", "src/pkg/core.py")
        False
    """
    return is_public_function(qname) and not is_test_module(module_path) and not is_leaf_module(module_path)


def run_fn_blast(qname: str) -> dict | None:
    """Query codemap-py for the transitive callers of one function; None on any failure."""
    exe = shutil.which("codemap-py")
    if exe is None:
        return None
    try:
        proc = subprocess.run(
            [exe, "query", "--timeout", "8", "fn-blast", qname],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        data = json.loads(proc.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or "error" in data or not isinstance(data.get("blast_radius"), list):
        return None
    return data


def _symbol_verdict(qname: str, module_path: str, blast: BlastFn) -> str | None:
    """Return a FULL reason for one changed symbol, or None when it stays off the main path."""
    if is_main_path(qname, module_path):
        return f"public main-path symbol {qname}"
    result = blast(qname)
    if result is None:
        return f"impact unknown: fn-blast failed for {qname}"
    for caller in result["blast_radius"]:
        if not isinstance(caller, dict):
            continue
        name, path = caller.get("caller", ""), caller.get("path") or caller.get("module", "")
        if isinstance(name, str) and isinstance(path, str) and is_main_path(name, path):
            return f"{qname} reached from main path via {name}"
    return None


def _module_verdict(module: dict, blast: BlastFn, budget: list[int]) -> str | None:
    """Return a FULL reason for one changed module, or None when the whole module stays LIGHT."""
    path = module.get("path") or module.get("module") or ""
    if not isinstance(path, str) or not path:
        return "impact unknown: changed module without a path"
    off_path = is_test_module(path) or is_leaf_module(path)
    if not off_path and int(module.get("rdep_count") or 0) >= IMPORTER_THRESHOLD:
        return f"{path} has {module.get('rdep_count')} importers"
    symbols = [s for s in module.get("changed_symbols") or [] if isinstance(s, str)]
    if not symbols:
        return None if off_path else f"module-level change in main-path module {path}"
    for qname in symbols:
        if budget[0] <= 0:
            return f"impact unknown: more than {SYMBOL_CAP} changed symbols"
        budget[0] -= 1
        reason = _symbol_verdict(qname, path, blast)
        if reason:
            return reason
    return None


def classify(diff_impact: object, blast: BlastFn = run_fn_blast) -> tuple[str, str]:
    """Return ``(tier, reason)`` for a parsed diff-impact payload.

    Args:
        diff_impact: parsed ``codemap-py query diff-impact`` JSON.
        blast: callable returning fn-blast JSON for a qname, or None on failure.

    Returns:
        ``("FULL", reason)`` when any change reaches the main path or impact is unknown,
        else ``("LIGHT", reason)``.

    Examples:
        >>> no_callers = lambda q: {"blast_radius": []}
        >>> classify({"changed_modules": [{"path": "src/pkg/core.py", "changed_symbols": ["pkg.core::_prep"]}]},
        ...          lambda q: {"blast_radius": [{"caller": "pkg.core::run", "path": "src/pkg/core.py"}]})
        ('FULL', 'pkg.core::_prep reached from main path via pkg.core::run')
        >>> classify({"changed_modules": [{"path": "src/pkg/viz/draw.py", "changed_symbols": ["pkg.viz.draw::box"]}]},
        ...          no_callers)
        ('LIGHT', 'all changes stay in tests, leaf modules or private code without main-path callers')
        >>> classify("not a dict")
        ('FULL', 'impact unknown: unreadable diff-impact output')
    """
    if not isinstance(diff_impact, dict):
        return FULL, "impact unknown: unreadable diff-impact output"
    modules = diff_impact.get("changed_modules")
    if not isinstance(modules, list) or not modules:
        return FULL, "impact unknown: no changed modules mapped"
    unmapped = [f for f in diff_impact.get("unmapped_files") or [] if isinstance(f, str) and f.endswith(".py")]
    if unmapped:
        return FULL, f"impact unknown: unmapped {unmapped[0]}"
    budget = [SYMBOL_CAP]
    for module in modules:
        reason = _module_verdict(module, blast, budget) if isinstance(module, dict) else "impact unknown: bad entry"
        if reason:
            return FULL, reason
    return LIGHT, "all changes stay in tests, leaf modules or private code without main-path callers"


def main(argv: list[str]) -> int:
    """Read diff-impact JSON from stdin and print ``TIER · reason``; always exit 0."""
    parser = argparse.ArgumentParser(
        prog="review_impact_tier.py",
        description="Print FULL or LIGHT review depth from codemap-py diff-impact JSON (stdin) plus fn-blast.",
    )
    parser.parse_args(argv)
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        payload = None
    tier, reason = classify(payload)
    print(f"{tier} · {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
