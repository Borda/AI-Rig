"""Function-level verbs over the call graph, including test impact and mocks."""

from __future__ import annotations

import json
import sys
from collections import deque
from pathlib import Path

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
from codemap_py.schema import MOCK_PATCHES_MIN_VER, VALID_CALL_RESOLUTIONS  # noqa: E402

from .coverage import _CALL_GRAPH_NOT_COVERED, _cmd_coverage  # noqa: E402
from .errors import _exit_error, _exit_symbol_not_found  # noqa: E402
from .index_io import (  # noqa: E402
    _get_rev_graph,
    _get_rev_import_graph,
    _get_symbol_map,
    _require_call_graph,
    _require_feature,
    _resolve_symbol_alias,
    build_module_map,
)
from .output import _print  # noqa: E402


def cmd_fn_deps(index: dict, qname: str) -> None:
    """List the functions called by a qualified function name.

    Args:
        index: parsed codemap index dict (must be v3 with call graph).
        qname: fully qualified symbol name (``module::symbol``).
    """
    _require_call_graph(index)
    sym_map = _get_symbol_map(index)
    if qname not in sym_map:
        _exit_symbol_not_found(index, qname)
    _module_entry, sym = sym_map[qname]
    calls = [
        {"target": edge["target"], "resolution": edge.get("resolution", "")}
        for edge in sym.get("calls", [])
        if edge.get("resolution") in VALID_CALL_RESOLUTIONS
    ]
    _print(
        json.dumps(
            {
                "qname": qname,
                "calls": calls,
                "count": len(calls),
                "index": _cmd_coverage(
                    index, method="static-ast", not_covered=["dynamic-dispatch", "runtime-injection"]
                ),
            }
        )
    )


def cmd_fn_rdeps(index: dict, qname: str, exclude_tests: bool = False) -> None:
    """List the functions that call a qualified function name.

    The caller list is deduplicated: each calling symbol appears at most once even
    when it calls *qname* from several call sites. ``count`` and its explicit alias
    ``unique_caller_count`` therefore both report the number of *distinct* callers,
    not the number of individual call-site edges.

    Args:
        index: parsed codemap index dict (must be v3 with call graph).
        qname: fully qualified symbol name (``module::symbol``).
    """
    _require_call_graph(index)
    sym_map = _get_symbol_map(index)
    resolved_qname = _resolve_symbol_alias(index, qname)
    if resolved_qname is None or (qname not in sym_map and qname not in (index.get("symbol_aliases") or {})):
        _exit_symbol_not_found(index, qname)
    rev_graph = _get_rev_graph(index)
    callers = []
    for caller_qname in sorted(set(rev_graph.get(resolved_qname, []))):
        entry = sym_map.get(caller_qname)
        m_entry = entry[0] if entry else {}
        callers.append(
            {
                "caller": caller_qname,
                "module": m_entry.get("name", ""),
                "path": m_entry.get("path", ""),
            }
        )
    if exclude_tests:
        callers = [c for c in callers if not sym_map.get(c["caller"], ({}, {}))[0].get("is_test")]
    fn_name = qname.split("::")[-1] if "::" in qname else qname
    _print(
        json.dumps(
            {
                "qname": qname,
                "resolved_qname": resolved_qname,
                "called_by": callers,
                "count": len(callers),
                "unique_caller_count": len(callers),
                "index": _cmd_coverage(
                    index,
                    query_target=qname,
                    method="static-ast",
                    not_covered=_CALL_GRAPH_NOT_COVERED,
                    hint=f'grep -rn "{fn_name}" to find hook-registered callers not in static AST',
                ),
            }
        )
    )


def cmd_fn_central(index: dict, top: int, exclude_tests: bool = False) -> None:
    """Most-called functions globally (by incoming call-edge count).

    Args:
        index: parsed codemap index dict (must be v3 with call graph).
        top: number of top-ranked functions to return.
    """
    _require_call_graph(index)
    sym_map = _get_symbol_map(index)
    # Count how many times each target appears across all forward call edges
    counts: dict[str, int] = {}
    for m in index.get("modules", []):
        if m.get("status") == "degraded":
            continue
        if exclude_tests and m.get("is_test"):
            continue
        for sym in m.get("symbols", []):
            for edge in sym.get("calls", []):
                if edge.get("resolution") in VALID_CALL_RESOLUTIONS:
                    target = _resolve_symbol_alias(index, edge["target"])
                    if target is None:
                        continue
                    counts[target] = counts.get(target, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:top]
    result = []
    for qname, call_count in ranked:
        entry = sym_map.get(qname)
        m_entry = entry[0] if entry else {}
        result.append(
            {
                "qname": qname,
                "call_count": call_count,
                "module": m_entry.get("name", ""),
                "path": m_entry.get("path", ""),
            }
        )
    _print(
        json.dumps(
            {"fn_central": result, "index": _cmd_coverage(index, method="static-ast", scope="call-graph-centrality")}
        )
    )


def cmd_fn_blast(index: dict, qname: str) -> None:
    """Transitive reverse-call BFS from *qname* -- everything that calls X, directly or transitively.

    Args:
        index: parsed codemap index dict (must be v3 with call graph).
        qname: fully qualified symbol name (``module::symbol``) to trace callers from.
    """
    _require_call_graph(index)
    sym_map = _get_symbol_map(index)
    if qname not in sym_map:
        _exit_symbol_not_found(index, qname)
    rev_graph = _get_rev_graph(index)

    # BFS
    visited: set[str] = {qname}
    queue: deque[tuple[str, int]] = deque([(qname, 0)])
    result = []
    while queue:
        node, depth = queue.popleft()
        for caller in rev_graph.get(node, []):
            if caller not in visited:
                visited.add(caller)
                entry = sym_map.get(caller)
                m_entry = entry[0] if entry else {}
                result.append(
                    {
                        "caller": caller,
                        "depth": depth + 1,
                        "module": m_entry.get("name", ""),
                        "path": m_entry.get("path", ""),
                    }
                )
                queue.append((caller, depth + 1))

    result.sort(key=lambda x: (x["depth"], x["caller"]))
    _print(
        json.dumps(
            {
                "qname": qname,
                "blast_radius": result,
                "total_callers": len(result),
                "index": _cmd_coverage(
                    index,
                    query_target=qname,
                    method="static-ast",
                    scope="transitive-call-graph",
                    not_covered=_CALL_GRAPH_NOT_COVERED,
                ),
            }
        )
    )


def _test_impact_via_function_call(index: dict, qname: str) -> tuple[set[str], set[str]]:
    """BFS the reverse call graph from *qname*; return (test files, test modules) reached.

    Args:
        index: parsed codemap index dict (must be v3+ with call graph).
        qname: ``module::symbol`` whose callers are traced transitively.
    """
    _require_call_graph(index)
    sym_map = _get_symbol_map(index)
    if qname not in sym_map:
        _exit_symbol_not_found(index, qname)
    rev_graph = _get_rev_graph(index)
    test_files: set[str] = set()
    test_mods: set[str] = set()
    visited: set[str] = {qname}
    queue: deque[str] = deque([qname])
    while queue:
        node = queue.popleft()
        for caller in rev_graph.get(node, []):
            if caller not in visited:
                visited.add(caller)
                entry = sym_map.get(caller)
                if entry:
                    m_entry = entry[0]
                    if m_entry.get("is_test"):
                        if p := m_entry.get("path", ""):
                            test_files.add(p)
                        if n := m_entry.get("name", ""):
                            test_mods.add(n)
                queue.append(caller)
    return test_files, test_mods


def _test_impact_via_module_import(index: dict, module_map: dict, qname: str) -> tuple[set[str], set[str]]:
    """BFS the reverse import graph from *qname*; return (test files, test modules) reached.

    Args:
        index: parsed codemap index dict.
        module_map: name-keyed module lookup from :func:`build_module_map`.
        qname: bare dotted module name whose importers are traced transitively.
    """
    if qname not in module_map:
        _exit_error(f"Module '{qname}' not found in index.")
    rev_imports = _get_rev_import_graph(index)
    test_files: set[str] = set()
    test_mods: set[str] = set()
    visited: set[str] = {qname}
    queue: deque[str] = deque([qname])
    while queue:
        node = queue.popleft()
        for importer in rev_imports.get(node, []):
            if importer not in visited:
                visited.add(importer)
                m_entry = module_map.get(importer, {})
                if m_entry.get("is_test"):
                    if p := m_entry.get("path", ""):
                        test_files.add(p)
                    if n := m_entry.get("name", ""):
                        test_mods.add(n)
                queue.append(importer)
    return test_files, test_mods


def _test_impact_via_mocks(index: dict, qname: str) -> tuple[set[str], set[str]]:
    """Return (test files, test modules) that mock *qname* via ``patch()``, call/import path or not.

    Args:
        index: parsed codemap index dict.
        qname: ``module::symbol`` (exact target) or bare module (prefix match on
            every ``module::symbol`` it owns).
    """
    mock_prefix = f"{qname}::" if "::" not in qname else None
    test_files: set[str] = set()
    test_mods: set[str] = set()
    for m in index.get("modules", []):
        if not m.get("is_test"):
            continue
        for patch in m.get("mock_patches", []) or []:
            target = patch.get("target", "")
            if not target:
                continue
            hit = target == qname or (mock_prefix and target.startswith(mock_prefix))
            if hit:
                p = patch.get("file", m.get("path", ""))
                if p:
                    test_files.add(p)
                if n := m.get("name", ""):
                    test_mods.add(n)
    return test_files, test_mods


def cmd_test_impact(index: dict, qname: str, include_mocks: bool = True) -> None:
    """Which tests are affected by changing *qname*?

    Two input modes:

    * ``module::symbol`` — BFS over static reverse call graph; filters to
      test modules. Also collects ``mock_patches`` for the symbol.
    * bare ``module`` — BFS over static reverse import graph; filters to
      test modules. Also collects ``mock_patches`` for every symbol in the
      module.

    Args:
        index: parsed codemap index dict.
        qname: ``module::symbol`` for function-level impact, or bare dotted
            module name for module-level impact.
        include_mocks: when True (default) add test files that mock *qname*
            even if they have no call/import path to it.

    Examples:
        ``mypackage.trainer::Trainer.fit`` — tests calling fit (directly or
        transitively) plus tests that mock Trainer.fit.
        ``mypackage.utils`` — tests that import utils through any chain.
    """
    if "::" in qname:
        test_files_via_call, test_mods_via_call = _test_impact_via_function_call(index, qname)
    else:
        module_map = build_module_map(index)
        test_files_via_call, test_mods_via_call = _test_impact_via_module_import(index, module_map, qname)

    test_files_via_mock: set[str] = set()
    test_mods_via_mock: set[str] = set()
    if include_mocks:
        test_files_via_mock, test_mods_via_mock = _test_impact_via_mocks(index, qname)

    all_files = sorted(test_files_via_call | test_files_via_mock)
    all_mods = sorted(test_mods_via_call | test_mods_via_mock)
    pytest_cmd = ("pytest " + " ".join(all_files)) if all_files else ""
    method = "static-ast" if "::" in qname else "import-graph"
    short_name = qname.split("::")[-1] if "::" in qname else qname.split(".")[-1]
    _print(
        json.dumps(
            {
                "qname": qname,
                "test_files": all_files,
                "test_modules": all_mods,
                "via_call": len(test_files_via_call),
                "via_mock": len(test_files_via_mock),
                "total": len(all_files),
                "pytest_cmd": pytest_cmd,
                "index": _cmd_coverage(
                    index,
                    query_target=qname,
                    method=method,
                    scope="test-impact",
                    not_covered=_CALL_GRAPH_NOT_COVERED,
                    hint=f'grep -rn "{short_name}" tests/ to find hook-based test deps not in static graph',
                ),
            }
        )
    )


def cmd_mock_rdeps(index: dict, query: str) -> None:
    """Print test files that mock *query* via ``patch``/``mocker.patch``.

    Two modes:
      * ``module::symbol`` — return callers for that one symbol
      * bare ``module`` — return every mocked symbol in the module with its callers

    Args:
        index: parsed codemap index dict (must be v4.1+ with mock_patches).
        query: either ``module::symbol`` or bare ``module`` dotted name.

    Examples:
        ``mypackage.core::MyClass.method`` — caller list for that symbol only.
        ``mypackage.core`` — every mocked symbol in ``mypackage.core``.
    """
    _require_feature(index, MOCK_PATCHES_MIN_VER, "mock_patches")
    by_symbol: dict[str, list[dict]] = {}
    for m in index.get("modules", []):
        if not m.get("is_test"):
            continue
        for entry in m.get("mock_patches", []) or []:
            target = entry.get("target")
            if not target:
                continue
            by_symbol.setdefault(target, []).append(
                {
                    "file": entry.get("file", ""),
                    "line": entry.get("line", 0),
                    "form": entry.get("form", ""),
                }
            )

    if "::" in query:
        module, symbol = query.split("::", 1)
        callers = sorted(by_symbol.get(query, []), key=lambda c: (c["file"], c["line"]))
        _print(
            json.dumps(
                {
                    "module": module,
                    "symbol": symbol,
                    "callers": callers,
                    "count": len(callers),
                    "index": _cmd_coverage(index, method="ast-flags", scope="mock-patch-strings"),
                }
            )
        )
        return

    prefix = f"{query}::"
    callers: list[dict] = []
    for target, hits in by_symbol.items():
        if not target.startswith(prefix):
            continue
        for hit in hits:
            callers.append(
                {
                    "target": target,
                    "file": hit["file"],
                    "line": hit["line"],
                    "form": hit["form"],
                }
            )
    callers.sort(key=lambda c: (c["target"], c["file"], c["line"]))
    _print(
        json.dumps(
            {
                "module": query,
                "callers": callers,
                "count": len(callers),
                "index": _cmd_coverage(index, method="ast-flags", scope="mock-patch-strings"),
            }
        )
    )
