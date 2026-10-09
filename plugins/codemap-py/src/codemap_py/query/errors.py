"""Exit codes and the error emitters every command exits through."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from codemap_py import query_state as state

# Transitional seam: exclusion rules live in codemap_py.scanner, but this
# module still reaches them through the old bare-name ``_exclusions`` import
# (bin/_exclusions.py, itself a shim onto codemap_py.scanner) via a
# bin/-relative sys.path insert, the same route bin/scan-index used to take.
# Every other import below is a direct package-internal import.
# parents[3] not [2]: this file sits one level deeper than the pre-split query.py
#: Plugin bin/ directory, added to sys.path so the _exclusions shim can be imported.
_BIN = Path(__file__).resolve().parents[3] / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))


# No module-level _LOG_DIR: it was a CWD-relative constant frozen at IMPORT time, so a
# query launched from a subdirectory logged to <subdir>/.cache/codemap/logs while the
# hooks logged to <repo-root>/.cache/codemap/logs — one session split across two
# directories, and a CODEMAP_LOG_DIR exported after import was never seen at all.
# log_cli() resolves the project-anchored root itself, per call.
_builtin_print = print  # saved before print( → _print( sweep below


# Exit-code contract: every error exit prints a parseable JSON object
# to stdout — never a bare non-zero exit with empty stdout. Codes let a caller branch
# on failure class without string-matching the message.
#: Exit status for generic query failures such as a missing symbol, invalid index or disabled feature.
_EXIT_GENERIC = 1  # generic failure (missing symbol, invalid index, feature gate)


_EXIT_BAD_INPUT = 2  # caller-supplied argument is malformed or rejected (bad regex, ReDoS, guard)


_EXIT_NOT_INDEXED = 3  # queried module is absent from the index (distinct from "no results")


#: Payload key every target-resolution failure carries, holding the caller's target exactly as supplied.
#:
#: Exit codes alone cannot tell a target the index does not resolve from a provider that cannot answer: a missing or
#: ambiguous symbol exits 1, the same code as an invalid index or a disabled feature. A consumer that must keep those
#: apart — retry with one listed candidate, or stop querying an unusable provider — reads this key instead of the
#: message wording. It is additive, so every historic key and exit code stays as it was.
REJECTED_TARGET_KEY = "rejected_target"


def _die_json(payload: dict, exit_code: int = _EXIT_GENERIC) -> None:
    """Print a JSON error object to stdout and exit with *exit_code*.

    Single choke point for every error exit so all failures share one shape
    (parseable JSON on stdout, never empty) and a stable exit-code contract.
    Routing through :func:`_print` keeps the cli.jsonl telemetry record.

    Args:
        payload: JSON-serialisable error object; ``error`` key is conventional.
        exit_code: process exit status (see the ``_EXIT_*`` constants).

    Examples:
        >>> import subprocess, sys
        >>> # _die_json({"error": "boom"}, 2) prints '{"error": "boom"}' then exits 2.
    """
    # Always JSON, never routed through the formatter: an error object is not a table, so
    # under ``--format tsv`` it would be refused by a path that reports failure the same
    # way and recurse. The ``{"error": ...}`` shape on stdout is the contract callers
    # parse, independent of the format asked for.
    if state._capture is not None:
        state._capture.append(json.dumps(payload))
    else:
        _builtin_print(json.dumps(payload))
        if state._invocation is not None:
            state._invocation.result = payload
    sys.exit(exit_code)


def _exit_error(message: str) -> None:
    """Print a ``{"error": message}`` object to stdout and exit with code 1.

    Backward-compatible wrapper over :func:`_die_json` preserving the historic
    shape and exit code for the many callers that only need a plain message.

    Args:
        message: human-readable error description.
    """
    _die_json({"error": message}, _EXIT_GENERIC)


def _exit_target_not_found(message: str, target: str) -> None:
    """Print a ``{"error": message}`` object naming the unresolved *target* and exit with code 1.

    Same shape and exit code as :func:`_exit_error`, plus :data:`REJECTED_TARGET_KEY`, so a caller can tell "this
    target does not resolve" from a provider-side failure that shares exit 1.

    Args:
        message: human-readable error description.
        target: the caller-supplied target that resolved to nothing usable.
    """
    _die_json({"error": message, REJECTED_TARGET_KEY: target}, _EXIT_GENERIC)


def _die_module_not_indexed(index: dict, module: str) -> None:
    """Exit 3 with a structured "module not indexed" error plus close suggestions.

    Replaces the historic ``Module 'X' not in index.`` message: a
    caller can now distinguish "this module is absent from the index" from "this
    module exists but has no results", and gets up to three closest indexed module
    names (difflib) to recover from a typo without a second round-trip.

    Args:
        index: parsed codemap index dict (source of the candidate module names).
        module: the dotted module name that was not found in the index.
    """
    import difflib

    known = [m["name"] for m in index.get("modules", []) if "name" in m]
    suggestions = difflib.get_close_matches(module, known, n=3, cutoff=0.6)
    _die_json(
        {"error": "module not indexed", "module": module, "suggestions": suggestions, REJECTED_TARGET_KEY: module},
        _EXIT_NOT_INDEXED,
    )


#: Most candidate qnames an ambiguous-symbol error lists; ``candidate_count`` still reports the full total.
_MAX_SYMBOL_CANDIDATES = 20


def _candidates_message(qname: str, candidates: tuple[str, ...]) -> str:
    """Word the error for a target that resolved to candidates instead of one symbol.

    A lone candidate is not ambiguity, so it is never reported as "1 match" to choose from. A lone module candidate
    means the target is the last segment of exactly one indexed module: the message names that module and the two ways
    to query it. A lone ``module::Class.method`` candidate means the target is a bare method name: the resolver never
    auto-resolves one because nobody named the class, so the message names the one qname to re-run with.

    Examples:
        >>> _candidates_message("graph", ("codemap_py.graph",)).split(";")[0]
        "Symbol 'graph' is not an indexed symbol: it names module 'codemap_py.graph'"
        >>> _candidates_message("render", ("pkg.util::Report.render",)).split(";")[0]
        "Symbol 'render' is a bare method name matching only 'pkg.util::Report.render'"
        >>> "ambiguous: 2" in _candidates_message("build", ("shapes.core::build", "tools.build"))
        True
    """
    if len(candidates) == 1 and "::" not in candidates[0]:
        module = candidates[0]
        return (
            f"Symbol '{qname}' is not an indexed symbol: it names module '{module}'; use 'rdeps {module}' or "
            f"module-level 'test-impact {module}' for the module, or '{module}::<symbol>' for a function query."
        )
    if len(candidates) == 1:
        method = candidates[0]
        return (
            f"Symbol '{qname}' is a bare method name matching only '{method}'; a method is never resolved without "
            f"its class, so re-run with '{method}'."
        )
    return (
        f"Symbol '{qname}' is ambiguous: {len(candidates)} indexed symbols or modules match that name. "
        "Re-run with one candidate: a 'module::symbol' for function queries, a module name for "
        "rdeps or module-level test-impact."
    )


def _exit_symbol_not_found(index: dict, qname: str, candidates: tuple[str, ...] = ()) -> None:
    """Exit with the fn-* not-found error, hinting when *qname* is really a module or is ambiguous.

    The 2026-07 usage audit found every bare-module ``fn-rdeps``/``fn-blast`` call
    failing with the generic "Symbol not found" — callers then retried other
    wrong shapes. Detecting the module case turns a dead end into a redirect.
    A bare or class-qualified name matching several symbols, a method leaf, or the
    last segment of an indexed module lists every candidate (``module::symbol`` or
    module name) instead, so the caller picks one in the next call rather than
    searching with ``find-symbol``. Every branch carries :data:`REJECTED_TARGET_KEY`.

    Args:
        index: parsed codemap index dict.
        qname: the symbol argument that failed to resolve.
        candidates: sorted ``module::symbol`` or module-name matches when *qname* is ambiguous; empty when nothing
            matched.
    """
    if "::" not in qname and any(m.get("name") == qname for m in index.get("modules", [])):
        _exit_target_not_found(
            f"'{qname}' is a module, not a function qname — fn-* commands need 'module::function' "
            f"(see 'symbols {qname}' for its functions). For module-level callers use: rdeps {qname}",
            qname,
        )
    if candidates:
        _die_json(
            {
                "error": _candidates_message(qname, candidates),
                "candidates": list(candidates[:_MAX_SYMBOL_CANDIDATES]),
                "candidate_count": len(candidates),
                REJECTED_TARGET_KEY: qname,
            },
            _EXIT_GENERIC,
        )
    _exit_target_not_found(f"Symbol '{qname}' not found. Use 'find-symbol <pattern>' to search.", qname)
