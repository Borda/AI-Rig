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
  modules (visualization, plotting, examples, docs, notebooks), or is private with
  no caller outside them. Test configuration and build/release entrypoints always receive ``FULL``.

Fail-safe: unreadable or oversized input (over ``MAX_INPUT_CHARS``), no changed modules,
unmapped ``.py`` files, a missing ``codemap-py`` binary, a failed ``fn-blast`` query, or an
index the diff-impact answer marks ``stale`` or ``root_mismatch`` all yield ``FULL`` — unknown
impact is never trimmed. Exit code is always 0 so the calling skill block never aborts on it.

Unknown-impact causes: every ``FULL · impact unknown: <cause phrase>[ <detail>]`` line starts
its reason with one fixed phrase from ``ImpactCause`` (``codemap unavailable``, ``codemap not
installed``, ``index stale``, ``index root mismatch``, ``diff-impact query failed``,
``unmapped``, ...), so the report names why impact was unknown and ``cause_of`` maps any such
line back to its slug. ``--unknown <slug>`` prints the line for a cause the calling block
detected itself (``codemap-not-installed``) without reading stdin.

Structural-context record: ``--context-record <file>`` reads the record ``codemap-py context`` wrote for the reviewed
head instead of stdin. An ``available`` or ``degraded`` record is tiered from its ``diff_impact`` answer, and every
``fn-blast`` query runs against that record's own index (``--index``/``--root``), never the invoking checkout's. A
``degraded`` record never yields ``LIGHT``. An ``unavailable`` record prints the unknown-impact line for its closed-set
``cause`` (the provider's slugs equal ``ImpactCause`` values); its text-search ``fallback`` never sets the tier. A
``not-applicable`` record (no Python file in the diff) stays ``FULL`` without an unknown-impact cause.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from collections.abc import Callable
from enum import Enum
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
        "notebook",
        "notebooks",
    }
)

#: Importer count from which a changed main-path module counts as widely used on its own.
IMPORTER_THRESHOLD = 5

#: Upper bound on fn-blast queries per run; symbols past the cap fail safe to FULL.
#: Matches the review's own fn-rdeps/fn-blast context loop cap, which bounds block wall time.
SYMBOL_CAP = 12

#: Largest diff-impact JSON read from stdin, in characters; matches the 10 MiB guard sibling scripts put on their
#: inputs. The read stops one character past it, so a runaway or hostile payload is never held whole in memory, and
#: oversized input is reported as unknown impact (FULL) rather than parsed.
MAX_INPUT_CHARS = 10 * 1024 * 1024

#: Verdict printed when the change reaches the main path or its impact cannot be determined.
FULL = "FULL"
#: Verdict printed when every change stays in tests, leaf modules or private code without main-path callers.
LIGHT = "LIGHT"

BlastFn = Callable[[str], "dict | None"]


class ImpactCause(str, Enum):
    """Why impact is unknown; the value is the slug, ``CAUSE_PHRASES`` holds the words the reason line carries."""

    CODEMAP_UNAVAILABLE = "codemap-unavailable"
    CODEMAP_NOT_INSTALLED = "codemap-not-installed"
    TARGET_NOT_WORKTREE = "target-not-worktree"
    INDEX_STALE = "index-stale"
    INDEX_ROOT_MISMATCH = "index-root-mismatch"
    QUERY_FAILED = "query-failed"
    UNREADABLE_OUTPUT = "unreadable-output"
    INPUT_TOO_LARGE = "input-too-large"
    NO_MAPPED_MODULES = "no-mapped-modules"
    UNMAPPED_FILE = "unmapped-file"
    MODULE_WITHOUT_PATH = "module-without-path"
    BAD_ENTRY = "bad-entry"
    FN_BLAST_FAILED = "fn-blast-failed"
    SYMBOL_CAP = "symbol-cap"
    CODEMAP_INTERPRETER_UNSUPPORTED = "codemap-interpreter-unsupported"
    CODEMAP_TIMEOUT = "codemap-timeout"
    INDEX_MISSING = "index-missing"
    INDEX_BUSY = "index-busy"
    INDEX_BUILD_FAILED = "index-build-failed"
    HEAD_MISMATCH = "head-mismatch"
    SOURCE_UNAVAILABLE = "source-unavailable"
    DIFF_UNAVAILABLE = "diff-unavailable"
    CONTEXT_REQUEST_INVALID = "context-request-invalid"


#: Fixed reason phrase per cause, as printed after ``impact unknown: ``. ``codemap unavailable`` is the reviews'
#: pre-codemap default (codemap disabled for this review, or its block never ran); ``review target is not the
#: working-tree diff`` is develop:review's line for a PR or worktree target. Phrases already in use before the enum
#: existed are kept byte for byte, because skill blocks and reports quote them. The members from
#: ``codemap-interpreter-unsupported`` on mirror the ``codemap-py context`` cause slugs this enum lacked, so a record's
#: ``cause`` maps to a member by value.
CAUSE_PHRASES: dict[ImpactCause, str] = {
    ImpactCause.CODEMAP_UNAVAILABLE: "codemap unavailable",
    ImpactCause.CODEMAP_NOT_INSTALLED: "codemap not installed",
    ImpactCause.TARGET_NOT_WORKTREE: "review target is not the working-tree diff",
    ImpactCause.INDEX_STALE: "index stale",
    ImpactCause.INDEX_ROOT_MISMATCH: "index root mismatch",
    ImpactCause.QUERY_FAILED: "diff-impact query failed",
    ImpactCause.UNREADABLE_OUTPUT: "unreadable diff-impact output",
    ImpactCause.INPUT_TOO_LARGE: "diff-impact input exceeds",
    ImpactCause.NO_MAPPED_MODULES: "no changed modules mapped",
    ImpactCause.UNMAPPED_FILE: "unmapped",
    ImpactCause.MODULE_WITHOUT_PATH: "changed module without a path",
    ImpactCause.BAD_ENTRY: "bad entry",
    ImpactCause.FN_BLAST_FAILED: "fn-blast failed for",
    ImpactCause.SYMBOL_CAP: f"more than {SYMBOL_CAP} changed symbols",
    ImpactCause.CODEMAP_INTERPRETER_UNSUPPORTED: "codemap interpreter unsupported",
    ImpactCause.CODEMAP_TIMEOUT: "codemap timed out",
    ImpactCause.INDEX_MISSING: "index missing",
    ImpactCause.INDEX_BUSY: "index busy",
    ImpactCause.INDEX_BUILD_FAILED: "index build failed",
    ImpactCause.HEAD_MISMATCH: "checkout not at the reviewed head",
    ImpactCause.SOURCE_UNAVAILABLE: "source root unreadable",
    ImpactCause.DIFF_UNAVAILABLE: "diff unreadable",
    ImpactCause.CONTEXT_REQUEST_INVALID: "context request refused",
}

#: Prefix every unknown-impact reason carries; the cause phrase follows it.
UNKNOWN_PREFIX = "impact unknown: "

#: Schema id of the ``codemap-py context`` record read by ``--context-record``.
CONTEXT_SCHEMA = "codemap-py.structural-context"

#: Reason for a record whose diff names no Python file: impact is not unknown, there is no structural question.
NOT_APPLICABLE_REASON = "no Python file in the diff; structural impact not applicable"

#: Longest provider ``detail`` or degraded reason carried into the one-line impact sentinel.
_DETAIL_CHARS = 80


def unknown(cause: ImpactCause, detail: str = "") -> str:
    """Return the reason text for an unknown-impact cause, optionally followed by one detail.

    Examples:
        >>> unknown(ImpactCause.UNMAPPED_FILE, "src/pkg/y.py")
        'impact unknown: unmapped src/pkg/y.py'
        >>> unknown(ImpactCause.INDEX_STALE)
        'impact unknown: index stale'
    """
    return UNKNOWN_PREFIX + CAUSE_PHRASES[cause] + (f" {detail}" if detail else "")


def cause_of(line: str) -> ImpactCause | None:
    """Map an impact line or reason back to its unknown-impact cause; ``None`` for a known-impact line.

    The longest matching phrase wins, so a phrase that extends another is never misread as the shorter one.

    Examples:
        >>> cause_of("FULL · impact unknown: codemap unavailable")
        <ImpactCause.CODEMAP_UNAVAILABLE: 'codemap-unavailable'>
        >>> cause_of("FULL · impact unknown: unmapped src/pkg/y.py")
        <ImpactCause.UNMAPPED_FILE: 'unmapped-file'>
        >>> cause_of("FULL · public main-path symbol pkg.core::run") is None
        True
    """
    _, sep, rest = line.partition(UNKNOWN_PREFIX)
    if not sep:
        return None
    for cause, phrase in sorted(CAUSE_PHRASES.items(), key=lambda item: -len(item[1])):
        if rest == phrase or rest.startswith(phrase + " "):
            return cause
    return None


def _segments(path: str) -> list[str]:
    """Split a module path or dotted module name into lowercase segments without extension.

    Examples:
        >>> _segments("src/pkg/viz/draw.py")
        ['src', 'pkg', 'viz', 'draw']
        >>> _segments("pkg.tests.test_x")
        ['pkg', 'tests', 'test_x']
    """
    posix = PurePosixPath(path.replace("\\", "/"))
    parts = list(posix.parts) if "/" in path or "\\" in path or path.endswith(".py") else path.split(".")
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


def is_execution_module(path: str) -> bool:
    """Identify test, build and release entrypoints whose execution needs full review."""
    parts = _segments(path)
    if not parts:
        return False
    return (
        parts[-1] in {"conftest", "setup", "release", "hatch_build", "build_backend"}
        or any(part in {"script", "scripts", "build", "build_hooks", "build_backend"} for part in parts)
        or (parts[-1] == "conf" and any(part in {"doc", "docs"} for part in parts[:-1]))
    )


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
    """Identify public runtime functions and executable tooling entrypoints.

    Examples:
        >>> is_main_path("pkg.core::run", "src/pkg/core.py")
        True
        >>> is_main_path("pkg.viz::draw", "src/pkg/viz.py")
        False
        >>> is_main_path("pkg.core::_prep", "src/pkg/core.py")
        False
    """
    return is_execution_module(module_path) or (
        is_public_function(qname) and not is_test_module(module_path) and not is_leaf_module(module_path)
    )


def run_fn_blast(qname: str, index_args: tuple[str, ...] = ()) -> dict | None:
    """Query codemap-py for the transitive callers of one function; None on any failure.

    ``index_args`` (``--index <file> --root <dir> --no-heal``) pins the query to one exact index, such as the one a
    ``codemap-py context`` record names; empty means the invoking checkout's own index.
    """
    exe = shutil.which("codemap-py")
    if exe is None:
        return None
    # qname derives from PR file names: a leading "-" would parse as a codemap-py option; unknown impact reads as FULL
    if qname.startswith("-") or any(ch.isspace() or ord(ch) < 32 for ch in qname):
        return None
    try:
        proc = subprocess.run(  # noqa: S603 - argv list, no shell
            [exe, "query", *index_args, "--timeout", "8", "fn-blast", qname],
            capture_output=True,
            timeout=30,
            check=False,
        )
        if proc.returncode != 0:
            return None
        # Decoded here, not by `encoding=`: on Windows the pipes are read by threads, where an undecodable stream is
        # only printed and comes back empty, which would read as a confirmed answer instead of an unknown one.
        proc.stderr.decode("utf-8")
        data = json.loads(proc.stdout.decode("utf-8"))
    except (OSError, subprocess.SubprocessError, ValueError, RecursionError):
        return None
    return data if _blast_callers(data) is not None else None


def _blast_callers(answer: object) -> list | None:
    """Read the caller list from a successful answer, including a confirmed empty list."""
    if not isinstance(answer, dict) or "error" in answer:
        return None
    callers = answer.get("blast_radius")
    return callers if isinstance(callers, list) else None


def _entry_path(entry: dict) -> str | None:
    """Use the reported path, falling back to the module name when the path is absent or empty."""
    path = entry.get("path", "")
    if path == "":
        path = entry.get("module")
    return path if isinstance(path, str) and path.strip() else None


def _string_list(value: object) -> bool:
    """Identify a list of nonblank strings without discarding unusable entries."""
    return isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value)


def _symbol_verdict(qname: str, module_path: str, blast: BlastFn) -> str | None:
    """Return a FULL reason for one changed symbol, or None when it stays off the main path."""
    if is_main_path(qname, module_path):
        return f"public main-path symbol {qname}"
    callers = _blast_callers(blast(qname))
    if callers is None:
        return unknown(ImpactCause.FN_BLAST_FAILED, qname)
    main_caller = None
    for caller in callers:
        if not isinstance(caller, dict):
            return unknown(ImpactCause.FN_BLAST_FAILED, qname)
        name, path = caller.get("caller"), _entry_path(caller)
        if not isinstance(name, str) or not name.strip() or path is None:
            return unknown(ImpactCause.FN_BLAST_FAILED, qname)
        if main_caller is None and is_main_path(name, path):
            main_caller = name
    return f"{qname} reached from main path via {main_caller}" if main_caller is not None else None


def _module_verdict(module: dict, blast: BlastFn, budget: list[int]) -> str | None:
    """Return a FULL reason for one changed module, or None when the whole module stays LIGHT."""
    path = _entry_path(module)
    if path is None:
        return unknown(ImpactCause.MODULE_WITHOUT_PATH)
    count = module.get("rdep_count", 0)
    symbols = module.get("changed_symbols", [])
    if type(count) is not int or count < 0 or not _string_list(symbols):
        return unknown(ImpactCause.BAD_ENTRY)
    if is_execution_module(path):
        return f"executable test/build/release entrypoint {path}"
    off_path = is_test_module(path) or is_leaf_module(path)
    if not off_path and count >= IMPORTER_THRESHOLD:
        return f"{path} has {count} importers"
    if not symbols:
        return None if off_path else f"module-level change in main-path module {path}"
    return _changed_symbols_verdict(symbols, path, blast, budget)


def _changed_symbols_verdict(symbols: list[str], path: str, blast: BlastFn, budget: list[int]) -> str | None:
    """Check changed symbols against the shared caller-query budget."""
    for qname in symbols:
        if budget[0] <= 0:
            return unknown(ImpactCause.SYMBOL_CAP)
        budget[0] -= 1
        reason = _symbol_verdict(qname, path, blast)
        if reason:
            return reason
    return None


def _index_problem(diff_impact: dict) -> str | None:
    """Return the unknown-impact reason when the answer is an error or its index cannot be trusted, else None.

    A ``stale`` index no longer matches the working tree and a ``root_mismatch`` index belongs to another root, so
    the mapping of changed lines to symbols may be wrong in either direction; neither may trim the review.

    Examples:
        >>> _index_problem({"index": {"stale": True}, "changed_modules": []})
        'impact unknown: index stale'
        >>> _index_problem({"error": "index_busy"})
        'impact unknown: diff-impact query failed index_busy'
        >>> _index_problem({"index": {"stale": False, "root_mismatch": False}}) is None
        True
    """
    if "error" in diff_impact:
        return unknown(ImpactCause.QUERY_FAILED, str(diff_impact.get("error"))[:80])
    index = diff_impact.get("index", {})
    if not isinstance(index, dict) or any(
        type(index.get(flag, False)) is not bool for flag in ("stale", "root_mismatch")
    ):
        return unknown(ImpactCause.UNREADABLE_OUTPUT)
    if index.get("stale"):
        return unknown(ImpactCause.INDEX_STALE)
    if index.get("root_mismatch"):
        return unknown(ImpactCause.INDEX_ROOT_MISMATCH)
    return None


def _unmapped_problem(diff_impact: dict) -> str | None:
    """Name an unmapped Python path or refuse a malformed coverage list."""
    unmapped = diff_impact.get("unmapped_files", [])
    if not _string_list(unmapped):
        return unknown(ImpactCause.UNREADABLE_OUTPUT)
    for path in unmapped:
        if path.endswith(".py"):
            return unknown(ImpactCause.UNMAPPED_FILE, _one_line(path))
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
        return FULL, unknown(ImpactCause.UNREADABLE_OUTPUT)
    problem = _index_problem(diff_impact)
    if problem:
        return FULL, problem
    modules = diff_impact.get("changed_modules")
    if not isinstance(modules, list) or not modules:
        return FULL, unknown(ImpactCause.NO_MAPPED_MODULES)
    problem = _unmapped_problem(diff_impact)
    if problem:
        return FULL, problem
    budget = [SYMBOL_CAP]
    for module in modules:
        reason = _module_verdict(module, blast, budget) if isinstance(module, dict) else unknown(ImpactCause.BAD_ENTRY)
        if reason:
            return FULL, reason
    return LIGHT, "all changes stay in tests, leaf modules or private code without main-path callers"


def _one_line(text: object) -> str:
    """Return ``text`` as one bounded line, so it can ride the single-line impact sentinel.

    Examples:
        >>> _one_line("index locked by\\npid 42")
        'index locked by pid 42'
        >>> _one_line(None)
        ''
    """
    return " ".join(str(text or "").split())[:_DETAIL_CHARS]


def _context_blast(record: dict) -> BlastFn:
    """Return a fn-blast callable pinned to the record's own index, or one that always fails when it names none.

    Querying the invoking checkout's index instead would tier a fresh head answer with callers from another index.
    """
    index = record.get("index")
    path = index.get("path") if isinstance(index, dict) else None
    root = record.get("root")
    if not isinstance(path, str) or not path or not isinstance(root, str) or not root:
        return lambda _qname: None
    index_args = ("--index", path, "--root", root, "--no-heal")
    return lambda qname: run_fn_blast(qname, index_args)


def _context_unknown(record: dict) -> str:
    """Return the unknown-impact reason for an ``unavailable`` record from its closed-set cause and detail.

    Examples:
        >>> _context_unknown({"cause": "index-busy", "detail": "lease held"})
        'impact unknown: index busy lease held'
        >>> _context_unknown({"cause": "brand-new-cause", "detail": ""})
        'impact unknown: diff-impact query failed unrecognised cause brand-new-cause'
    """
    cause, detail = record.get("cause"), record.get("detail", "")
    if not isinstance(cause, str) or not isinstance(detail, str):
        return unknown(ImpactCause.UNREADABLE_OUTPUT, "(structural context record)")
    try:
        return unknown(ImpactCause(cause), _one_line(detail))
    except ValueError:
        return unknown(ImpactCause.QUERY_FAILED, _one_line(f"unrecognised cause {cause}"))


def classify_context(record: object, blast: BlastFn | None = None) -> tuple[str, str]:
    """Return ``(tier, reason)`` for a ``codemap-py context`` record.

    Args:
        record: parsed structural-context record (schema ``codemap-py.structural-context``).
        blast: fn-blast callable; default queries the record's own index.

    Returns:
        ``classify`` of the record's ``diff_impact`` for an ``available`` record; the same for ``degraded``, except
        that a ``LIGHT`` result is raised to ``FULL`` with the first degraded reason; ``FULL`` with the cause's
        unknown-impact reason for ``unavailable``; ``FULL`` with ``NOT_APPLICABLE_REASON`` for ``not-applicable``.

    Examples:
        >>> rec = {"schema": CONTEXT_SCHEMA, "status": "unavailable", "cause": "index-stale", "detail": ""}
        >>> classify_context(rec)
        ('FULL', 'impact unknown: index stale')
        >>> classify_context({"schema": CONTEXT_SCHEMA, "status": "not-applicable", "cause": "non-python-scope"})
        ('FULL', 'no Python file in the diff; structural impact not applicable')
        >>> viz = {"changed_modules": [{"path": "src/pkg/viz/draw.py", "changed_symbols": ["pkg.viz.draw::box"]}]}
        >>> rec = {"schema": CONTEXT_SCHEMA, "status": "degraded", "diff_impact": viz, "degraded_reasons": ["deleted"]}
        >>> classify_context(rec, lambda q: {"blast_radius": []})
        ('FULL', 'degraded structural answer: deleted')
    """
    if not isinstance(record, dict) or record.get("schema") != CONTEXT_SCHEMA:
        return FULL, unknown(ImpactCause.UNREADABLE_OUTPUT, "(structural context record)")
    status = record.get("status")
    if status == "not-applicable":
        return FULL, NOT_APPLICABLE_REASON
    if status == "unavailable":
        return FULL, _context_unknown(record)
    reasons = record.get("degraded_reasons", [])
    if status not in ("available", "degraded") or not _string_list(reasons):
        return FULL, unknown(ImpactCause.UNREADABLE_OUTPUT, "(structural context record)")
    tier, reason = classify(record.get("diff_impact"), blast or _context_blast(record))
    if tier == LIGHT and status == "degraded":
        return FULL, "degraded structural answer: " + (_one_line(reasons[0]) if reasons else "no reason recorded")
    return tier, reason


def _read_context_record(path: str) -> object:
    """Return the parsed record at ``path``, or ``None`` when it is unreadable, oversized or not JSON."""
    try:
        with open(path, encoding="utf-8") as handle:
            raw = handle.read(MAX_INPUT_CHARS + 1)
        return json.loads(raw) if len(raw) <= MAX_INPUT_CHARS else None
    except (OSError, ValueError, RecursionError):
        return None


def main(argv: list[str]) -> int:
    """Read diff-impact JSON from stdin and print ``TIER · reason``; always exit 0."""
    parser = argparse.ArgumentParser(
        prog="review_impact_tier.py",
        description="Print FULL or LIGHT review depth from codemap-py diff-impact JSON (stdin) plus fn-blast.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--unknown",
        choices=[c.value for c in ImpactCause],
        help="print the FULL line for an unknown-impact cause the caller detected, without reading stdin",
    )
    mode.add_argument(
        "--context-record",
        metavar="FILE",
        help="tier from a codemap-py context record (its diff_impact and its own index) instead of stdin",
    )
    args = parser.parse_args(argv)
    if args.unknown:
        print(f"{FULL} · {unknown(ImpactCause(args.unknown))}")
        return 0
    if args.context_record:
        tier, reason = classify_context(_read_context_record(args.context_record))
        print(f"{tier} · {reason}")
        return 0
    try:
        raw = sys.stdin.read(MAX_INPUT_CHARS + 1)
    except (OSError, ValueError):
        print(f"{FULL} · {unknown(ImpactCause.UNREADABLE_OUTPUT)}")
        return 0
    if len(raw) > MAX_INPUT_CHARS:
        # Exit 0, not 2: the calling block redirects stdout into the impact sentinel and must never abort on it
        print(f"{FULL} · {unknown(ImpactCause.INPUT_TOO_LARGE, f'{MAX_INPUT_CHARS} characters')}")
        return 0
    if not raw.strip():
        # codemap-py writes its errors to stderr, which the calling block discards: empty stdin is a failed query
        print(f"{FULL} · {unknown(ImpactCause.QUERY_FAILED, '(no output)')}")
        return 0
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        payload = None
    tier, reason = classify(payload)
    print(f"{tier} · {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
