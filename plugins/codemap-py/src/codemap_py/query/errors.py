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
_EXIT_GENERIC = 1  # generic failure (missing symbol, invalid index, feature gate)


_EXIT_BAD_INPUT = 2  # caller-supplied argument is malformed or rejected (bad regex, ReDoS, guard)


_EXIT_NOT_INDEXED = 3  # queried module is absent from the index (distinct from "no results")


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
        {"error": "module not indexed", "module": module, "suggestions": suggestions},
        _EXIT_NOT_INDEXED,
    )


def _exit_symbol_not_found(index: dict, qname: str) -> None:
    """Exit with the fn-* not-found error, hinting when *qname* is really a module.

    The 2026-07 usage audit found every bare-module ``fn-rdeps``/``fn-blast`` call
    failing with the generic "Symbol not found" — callers then retried other
    wrong shapes. Detecting the module case turns a dead end into a redirect.

    Args:
        index: parsed codemap index dict.
        qname: the symbol argument that failed to resolve.
    """
    if "::" not in qname and any(m.get("name") == qname for m in index.get("modules", [])):
        _exit_error(
            f"'{qname}' is a module, not a function qname — fn-* commands need 'module::function' "
            f"(see 'symbols {qname}' for its functions). For module-level callers use: rdeps {qname}"
        )
    _exit_error(f"Symbol '{qname}' not found. Use 'find-symbol <pattern>' to search.")
