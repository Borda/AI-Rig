"""Symbol lookup verbs: source retrieval, listing and regex search."""

from __future__ import annotations

import argparse
import ast
import json
import multiprocessing
import re
import sys
from collections.abc import Callable
from multiprocessing.connection import Connection
from pathlib import Path

from codemap_py.scanner import discovery

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
from codemap_py.schema import Symbol  # noqa: E402

# Reached through the module, not a bound name: tests patch these on the defining
# module (monkeypatch.setattr(query.index_io, ...)), which a `from .index_io import`
# binding here would not see.
from . import index_io  # noqa: E402
from .coverage import _cmd_coverage  # noqa: E402
from .errors import (  # noqa: E402
    _EXIT_BAD_INPUT,
    _die_json,
    _die_module_not_indexed,
    _exit_error,
    _exit_target_not_found,
)
from .index_io import build_module_map  # noqa: E402
from .output import _print  # noqa: E402

# coarse classification of a stale symbol coordinate. The fine-grained
# ``stale_reason`` is kept for diagnostics; ``stale_category`` gives agents the one
# bit that changes their next action — the symbol is GONE (``symbol_deleted``: its
# file or definition no longer exists → stop looking) versus MOVED (``coords_stale``:
# it still exists but the indexed line range is wrong → re-scan / Read the file).
#: Maps each stale_reason to symbol_deleted (gone) or coords_stale (moved) for the stale_category field.
_STALE_CATEGORY = {
    "no path": "symbol_deleted",
    "file deleted": "symbol_deleted",
    "line range past EOF": "coords_stale",
    "symbol name not in slice header": "coords_stale",
}


def _scan_symbols(index: dict, exclude_tests: bool, predicate: Callable[[Symbol], bool]) -> list[tuple[dict, Symbol]]:
    """Return every ``(module, symbol)`` pair across non-degraded modules where *predicate* holds.

    Args:
        index: parsed codemap index dict.
        exclude_tests: if True, skip test modules during the scan.
        predicate: called with each symbol dict; included when it returns True.
    """
    matches: list[tuple[dict, Symbol]] = []
    for m in index.get("modules", []):
        if m.get("status") == "degraded":
            continue
        if exclude_tests and m.get("is_test"):
            continue
        for sym in m.get("symbols", []):
            if predicate(sym):
                matches.append((m, sym))
    return matches


def _find_symbol_matches(index: dict, name: str, exclude_tests: bool) -> list[tuple[dict, Symbol]]:
    """Find symbols by exact name/qualified_name match, falling back to a substring search.

    Args:
        index: parsed codemap index dict.
        name: symbol name or qualified name to search for.
        exclude_tests: if True, skip test modules during search.
    """
    matches = _scan_symbols(index, exclude_tests, lambda sym: sym["name"] == name or sym["qualified_name"] == name)
    if matches:
        return matches
    # Fallback: case-insensitive substring on qualified_name
    name_lower = name.lower()
    return _scan_symbols(index, exclude_tests, lambda sym: name_lower in sym["qualified_name"].lower())


def _extract_import_block(lines: list[str], file_path: Path | None) -> str:
    """Return the module-level import statements from *lines*, or ``""`` on any parse failure.

    Args:
        lines: the file's source, split into lines (empty when the file is unreadable).
        file_path: the file's path, used only as the AST parse filename for error messages.
    """
    if not lines:
        return ""
    try:
        tree = ast.parse("\n".join(lines), filename=str(file_path or ""))
        collected: list[str] = []
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                end = node.end_lineno or node.lineno
                collected.extend(lines[node.lineno - 1 : end])
        return "\n".join(collected)
    except (SyntaxError, ValueError):  # ValueError on null bytes
        return ""


def _symbol_source_and_staleness(
    sym: Symbol, lines: list[str], rel_path: str, file_path: Path | None
) -> tuple[str, bool, str | None]:
    """Slice *sym*'s source out of *lines* and detect whether that slice is stale.

    A stale slice is blanked before returning: never emit source that may
    point at a different function because the file shrank or moved since indexing. Instead,
    callers fall back to ``Read(path)`` and see ``stale_reason`` for diagnostics.

    Args:
        sym: the symbol dict (``start_line``, ``end_line``, ``name``).
        lines: the owning file's source lines (empty when the file is unreadable).
        rel_path: the module's indexed path (empty string when the index has none).
        file_path: resolved absolute path to the file, or None when *rel_path* is empty.
    """
    source = "\n".join(lines[sym["start_line"] - 1 : sym["end_line"]]) if lines else ""
    stale = False
    stale_reason: str | None = None
    if not rel_path:
        stale, stale_reason = True, "no path"
    elif file_path is None or not file_path.exists():
        stale, stale_reason = True, "file deleted"
    elif sym["end_line"] > len(lines):
        stale, stale_reason = True, "line range past EOF"
    elif source:
        # Identifier-boundary check: name must follow def/class keyword exactly.
        # scan-index records start_line at the def/class line (not decorator), so
        # first non-blank line is always the signature. Regex prevents foo matching foo_bar.
        first_nonblank = next((ln for ln in source.split("\n") if ln.strip()), "")
        name_pattern = re.compile(rf"\b(?:def|async\s+def|class)\s+{re.escape(sym['name'])}\s*[:(]")
        if not name_pattern.search(first_nonblank):
            stale, stale_reason = True, "symbol name not in slice header"
    if stale:
        source = ""
    return source, stale, stale_reason


def _symbol_group_results(
    rel_path: str, group: list[tuple[dict, dict]], git_root: Path, with_imports: bool
) -> list[dict]:
    """Build the result entries for every ``(module, symbol)`` pair sharing one file path.

    Reads *rel_path* at most once (empty ``lines`` when unreadable), optionally extracts
    its import block, then slices and staleness-checks each symbol in *group*.

    Args:
        rel_path: the module's indexed path (may be empty for a path-less entry).
        group: the ``(module_entry, symbol)`` pairs recorded under this path.
        git_root: project root used to resolve *rel_path* to an absolute path.
        with_imports: if True, attach the module-level import block to each result.
    """
    lines: list[str] = []
    file_path: Path | None = None
    if rel_path:
        if not index_io._safe_module_path(rel_path):
            _die_json({"error": "invalid symbol path", "path": rel_path}, _EXIT_BAD_INPUT)
        try:
            root = git_root.resolve()
            file_path = (root / rel_path).resolve()
        except (OSError, RuntimeError, ValueError):
            _die_json({"error": "invalid symbol path", "path": rel_path}, _EXIT_BAD_INPUT)
        if not file_path.is_relative_to(root):
            _die_json({"error": "symbol path outside project", "path": rel_path}, _EXIT_BAD_INPUT)
        try:
            # Python 3.13 stopped raising on a symlink loop in the non-strict resolve above; strict still does.
            file_path.resolve(strict=True)
        except (FileNotFoundError, NotADirectoryError):
            pass  # missing or stale source (deleted file, parent turned into a file): exists() branch below
        except (OSError, RuntimeError):
            _die_json({"error": "invalid symbol path", "path": rel_path}, _EXIT_BAD_INPUT)
        if file_path.exists():
            if not file_path.is_file() or file_path.stat().st_size > discovery._MAX_FILE_SIZE_BYTES:
                _die_json({"error": "invalid symbol source", "path": rel_path}, _EXIT_BAD_INPUT)
            with file_path.open("rb") as source_file:
                content = source_file.read(discovery._MAX_FILE_SIZE_BYTES + 1)
            if len(content) > discovery._MAX_FILE_SIZE_BYTES:
                _die_json({"error": "invalid symbol source", "path": rel_path}, _EXIT_BAD_INPUT)
            lines = content.decode("utf-8", errors="replace").splitlines()

    import_block: str | None = _extract_import_block(lines, file_path) if with_imports else None

    results = []
    for m, sym in group:
        source, stale, stale_reason = _symbol_source_and_staleness(sym, lines, rel_path, file_path)
        results.append(
            {
                "name": sym["name"],
                "qualified_name": sym["qualified_name"],
                "type": sym.get("type", ""),
                "module": m["name"],
                "path": m.get("path", ""),
                "start_line": sym["start_line"],
                "end_line": sym["end_line"],
                "source": source,
                "stale": stale,
                "stale_reason": stale_reason,
                "stale_category": _STALE_CATEGORY.get(stale_reason) if stale_reason else None,
                "imports": import_block,
            }
        )
    return results


def cmd_symbol(
    index: dict,
    name: str,
    limit: int = 20,
    exclude_tests: bool = False,
    with_imports: bool = False,
    project_root: Path | None = None,
) -> None:
    """Find a symbol by name (exact match, then qualified_name substring) and return its source.

    Args:
        index: parsed codemap index dict.
        name: symbol name or qualified name to search for.
        limit: max results to return (0 = unlimited).
        exclude_tests: if True, skip test modules during search.
        with_imports: if True, attach the module-level import block to each symbol result.
        project_root: resolved project root for file-path lookups; falls back to the
            cached git root, then CWD, when omitted.
    """
    matches = _find_symbol_matches(index, name, exclude_tests)
    if not matches:
        _exit_target_not_found(
            f"Symbol '{name}' not found. Try /codemap-py:query-code find-symbol <pattern> to search.", name
        )

    total_matches = len(matches)
    truncated = limit > 0 and total_matches > limit
    if truncated:
        matches = matches[:limit]

    # Group by file path to read each file at most once
    from collections import defaultdict

    by_path: dict[str, list[tuple[dict, dict]]] = defaultdict(list)
    for m, sym in matches:
        by_path[m.get("path", "")].append((m, sym))

    git_root = project_root if project_root is not None else (index_io._get_git_root_cached() or Path.cwd())
    results = [
        r for rel_path, group in by_path.items() for r in _symbol_group_results(rel_path, group, git_root, with_imports)
    ]
    any_stale = any(r["stale"] for r in results)
    confidence = "exact" if not truncated and not any_stale else "partial"
    coverage = _cmd_coverage(index, method="index-lookup", confidence=confidence)
    if truncated:
        coverage["truncated"] = True
        coverage["total_available"] = total_matches
    _print(
        json.dumps(
            {
                "symbols": results,
                "count": len(results),
                "index": coverage,
            }
        )
    )


def cmd_symbols(index: dict, module: str) -> None:
    """List all symbols in a module (no file I/O -- index only).

    Args:
        index: parsed codemap index dict.
        module: dotted module name whose symbols are listed.
    """
    modules = build_module_map(index)
    entry = modules.get(module)
    if entry is None:
        _die_module_not_indexed(index, module)
    syms: list[Symbol] = entry.get("symbols", [])
    _print(
        json.dumps(
            {
                "module": module,
                "path": entry.get("path", ""),
                "symbols": [
                    {
                        "name": s["name"],
                        "qualified_name": s["qualified_name"],
                        "type": s.get("type", ""),
                        "start_line": s["start_line"],
                        "end_line": s["end_line"],
                    }
                    for s in syms
                ],
                "count": len(syms),
                "index": _cmd_coverage(
                    index,
                    method="index-lookup",
                    module_status=entry.get("status"),
                    module_name=module,
                    confidence="exact",
                ),
            }
        )
    )


#: Verbose regex flagging adjacent or nested quantifiers that risk catastrophic backtracking in user patterns.
_DANGEROUS_PATTERN = re.compile(
    (
        "\n"
        r"    # adjacent quantifiers: a++, a**, a*+, a+{2,}"
        "\n"
        r"    (?:\+|\*|\{[0-9]+,?\})\s*(?:\+|\*|\{[0-9]+,?\})"
        "\n"
        r"    |"
        "\n"
        r"    # group with inner quantifier then outer quantifier: (a+)+, (a+)*, (.+){2,}"
        "\n"
        r"    \([^)]*(?:\+|\*|\?|\{[0-9])[^)]*\)\s*(?:\+|\*|\?|\{[0-9])"
        "\n    "
    ),
    re.VERBOSE,
)


# Alternation-based catastrophic backtracking that _DANGEROUS_PATTERN misses:
# any single-level alternation group followed by an outer quantifier — e.g.
# (a|aa)+, (a*|b*)+, (foo|foo)*. Overlapping or quantified branches under an outer
# +/*/{ are the classic exponential-backtracking shape. [^()] keeps this to a flat
# (non-nested) group so the match stays anchored to one alternation.
#: Regex flagging a flat alternation group followed by an outer quantifier, a ReDoS shape.
_ALT_REDOS_RE = re.compile(r"\([^()]*\|[^()]*\)[+*{]")

#: Default deadline for regex matching, including direct and batch command dispatch.
FIND_SYMBOL_TIMEOUT_SECONDS = 5


def _is_dangerous_regex(pattern: str) -> bool:
    """Return True if *pattern* exhibits a known catastrophic-backtracking shape.

    Combines the adjacent/nested-quantifier heuristic (:data:`_DANGEROUS_PATTERN`)
    with alternation-based ReDoS detection (:data:`_ALT_REDOS_RE`). Conservative
    by design — a false positive only rejects a query, never executes a hang.

    Examples:
        >>> _is_dangerous_regex("(a+)+")
        True
        >>> _is_dangerous_regex("(a|aa)+")
        True
        >>> _is_dangerous_regex("(a*|b*)+")
        True
        >>> _is_dangerous_regex("^Auth.*Handler$")
        False
    """
    if len(pattern) > 4096:
        return True
    normalized = re.sub(r"\\.|\[(?:\\.|[^\]\\])*\]", "a", pattern)
    return bool(
        re.search(r"(?:\.[*+]){4}", normalized)
        or _DANGEROUS_PATTERN.search(normalized)
        or _ALT_REDOS_RE.search(normalized)
    )


def _regex_worker(pattern: str, names: list[str], sender: Connection) -> None:
    """Return matching positions over a pipe so an expensive regex can be terminated."""
    with sender:
        rx = re.compile(pattern, re.IGNORECASE)
        sender.send([position for position, name in enumerate(names) if rx.search(name)])


def _bounded_regex_matches(pattern: str, names: list[str], timeout: float) -> list[int]:
    """Match names in a disposable process with a wall-clock deadline on every supported host."""
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_regex_worker, args=(pattern, names, sender), daemon=True)
    try:
        process.start()
        sender.close()
        if not receiver.poll(timeout):
            _print(f"find-symbol: timed out after {timeout:g}s", file=sys.stderr)
            _die_json({"error": "timeout", "timeout_seconds": timeout}, _EXIT_BAD_INPUT)
        return receiver.recv()
    except (EOFError, OSError) as exc:
        _print(f"find-symbol: regex worker failed: {exc}", file=sys.stderr)
        _die_json({"error": "regex worker failed"}, _EXIT_BAD_INPUT)
    finally:
        receiver.close()
        sender.close()
        if process.pid is not None:
            if process.is_alive():
                process.terminate()
            process.join()


def cmd_find_symbol(
    index: dict,
    pattern: str,
    limit: int = 20,
    exclude_tests: bool = False,
    timeout: float = FIND_SYMBOL_TIMEOUT_SECONDS,
) -> None:
    """Search indexed symbol qualified names with a bounded regular expression.

    Args:
        index: parsed codemap index dict.
        pattern: Python regex pattern matched against each symbol's qualified name.
        limit: max results to return (0 = unlimited).
        exclude_tests: Omit test modules from candidate symbols.
        timeout: Maximum seconds to wait for the regex worker.
    """
    # ReDoS guard — nested/adjacent and alternation quantifiers (e.g. `(a+)+`, `(.*){2,}`,
    # `(a|aa)+`) can cause catastrophic backtracking. a bare stderr `return` left
    # stdout empty and exited 0, breaking JSON consumers — emit a parseable error object
    # and a non-zero exit so a rejection is unmistakable both to humans and to callers.
    if _is_dangerous_regex(pattern):
        _print(
            f"find-symbol: pattern '{pattern}' may cause ReDoS — use a simpler pattern",
            file=sys.stderr,
        )
        _die_json(
            {"error": "pattern rejected", "reason": "redos", "pattern": pattern},
            _EXIT_BAD_INPUT,
        )
    try:
        re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        _die_json({"error": "invalid regex", "pattern": pattern, "detail": str(exc)}, _EXIT_BAD_INPUT)

    candidates = []
    for m in index.get("modules", []):
        if m.get("status") == "degraded":
            continue
        if exclude_tests and m.get("is_test"):
            continue
        for sym in m.get("symbols", []):
            candidates.append(
                {
                    "name": sym["name"],
                    "qualified_name": sym["qualified_name"],
                    "type": sym.get("type", ""),
                    "module": m["name"],
                    "path": m.get("path", ""),
                    "start_line": sym["start_line"],
                    "end_line": sym["end_line"],
                }
            )
    matches = _bounded_regex_matches(pattern, [entry["qualified_name"] for entry in candidates], timeout)
    results = [candidates[position] for position in matches]
    total_matches = len(results)
    truncated = limit > 0 and total_matches > limit
    if truncated:
        results = results[:limit]
    confidence = "exact" if not truncated else "partial"
    coverage = _cmd_coverage(index, method="index-lookup", confidence=confidence)
    if truncated:
        coverage["truncated"] = True
        coverage["total_available"] = total_matches
    _print(
        json.dumps(
            {
                "pattern": pattern,
                "matches": results,
                "count": len(results),
                "index": coverage,
            }
        )
    )


def _reject_multiline_args(args: argparse.Namespace) -> None:
    """Exit with an actionable error when any string argument embeds newlines.

    Callers that expand an unquoted shell variable under zsh (no word splitting)
    pass a whole newline-joined name list as ONE argument — the 2026-07 usage
    audit traced ~all production CLI errors to this shape, surfacing only as the
    unhelpful "module not indexed" / "Symbol not found".

    Args:
        args: the parsed argparse namespace for this invocation.
    """
    for value in vars(args).values():
        if isinstance(value, str) and "\n" in value:
            names = [line for line in value.splitlines() if line.strip()]
            _exit_error(
                f"{len(names)} names passed as ONE argument (newline-joined) — your shell did not "
                "word-split the variable (zsh default). Call scan-query once per name, or use "
                "'batch' with one request item per name."
            )
