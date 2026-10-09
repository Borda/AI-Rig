"""Verbs over the subprocess and pytest-fixture graphs."""

from __future__ import annotations

import json
import sys
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
from codemap_py.schema import FIXTURE_GRAPH_MIN_VER, SUBPROCESS_CALLS_MIN_VER  # noqa: E402

from .coverage import _cmd_coverage  # noqa: E402
from .errors import _die_module_not_indexed, _exit_target_not_found  # noqa: E402
from .index_io import _require_feature, _require_subprocess_rdep_count, build_module_map  # noqa: E402
from .output import _print  # noqa: E402


def cmd_subprocess_deps(index: dict, module: str) -> None:
    """Print every subprocess call made by *module* (forward edges).

    Lists ``{target_module, file, line}`` entries recorded by ``scan-index``
    for ``subprocess.run``, ``subprocess.Popen``, and ``os.system`` invocations
    that resolve to an indexed module.

    Args:
        index: parsed codemap index dict (must be v5.2+ with subprocess_calls).
        module: dotted module name whose forward subprocess edges are queried.
    """
    _require_feature(index, SUBPROCESS_CALLS_MIN_VER, "subprocess-deps")
    modules = build_module_map(index)
    entry = modules.get(module)
    if entry is None:
        _die_module_not_indexed(index, module)
    calls = entry.get("subprocess_calls", []) or []
    _print(
        json.dumps(
            {
                "module": module,
                "calls": calls,
                "count": len(calls),
                "index": _cmd_coverage(index, method="ast-flags", scope="subprocess-call-strings"),
            }
        )
    )


def cmd_subprocess_rdeps(index: dict, module: str) -> None:
    """Print every module that spawns *module* as a subprocess (reverse edges).

    Walks every indexed module's ``subprocess_calls`` list and collects entries
    whose ``target_module`` equals *module*. Fail-closed via
    :func:`_require_subprocess_rdep_count` — a missing reverse table aborts
    rather than silently reporting zero callers.

    Args:
        index: parsed codemap index dict (must be v5.2+ with subprocess_rdep_count).
        module: dotted module name whose reverse subprocess edges are queried.
    """
    _require_feature(index, SUBPROCESS_CALLS_MIN_VER, "subprocess-rdeps")
    _require_subprocess_rdep_count(index)
    callers: list[dict] = []
    for m in index.get("modules", []):
        for call in m.get("subprocess_calls", []) or []:
            if call.get("target_module") == module:
                callers.append(
                    {
                        "caller": m.get("name", ""),
                        "file": call.get("file", ""),
                        "line": call.get("line", 0),
                    }
                )
    callers.sort(key=lambda c: (c["caller"], c["file"], c["line"]))
    _print(
        json.dumps(
            {
                "module": module,
                "callers": callers,
                "count": len(callers),
                "index": _cmd_coverage(index, method="ast-flags", scope="subprocess-call-strings"),
            }
        )
    )


def _collect_fixture_definitions(index: dict) -> dict[str, dict]:
    """Build a global ``fixture_name -> {scope, defined_in, depends_on}`` lookup.

    Aggregates fixture definitions from every module's ``fixture_exports`` block.
    Each fixture's own parameter list (``params`` field, populated by
    :func:`extract_fixtures`) becomes its ``depends_on`` set — the fixture's
    own argument names are the fixtures it requests.

    Deeper-conftest-wins semantics are approximated by depth-sorting the conftest
    list (deeper paths overwrite shallower ones). Test-file fixtures override
    conftest fixtures of the same name.

    Args:
        index: parsed codemap index dict.

    Returns:
        Mapping ``fixture_name -> {"scope", "defined_in", "depends_on"}``.
    """

    def _depth(path: str) -> int:
        parts = Path(path).parts
        return max(len(parts) - 1, 0)

    conftests: list[dict] = []
    test_files: list[dict] = []
    for m in index.get("modules", []):
        if m.get("status") != "ok":
            continue
        path = m.get("path", "")
        basename = Path(path).name
        if basename == "conftest.py":
            conftests.append(m)
        elif m.get("is_test"):
            test_files.append(m)
    conftests.sort(key=lambda m: _depth(m.get("path", "")))

    aggregated: dict[str, dict] = {}
    for m in (*conftests, *test_files):
        for fix in m.get("fixture_exports", []) or []:
            aggregated[fix["name"]] = {
                "scope": fix.get("scope", "function"),
                "defined_in": m.get("name", ""),
                "depends_on": list(fix.get("params", []) or []),
            }
    return aggregated


def cmd_fixture_rdeps(index: dict, fixture_name: str) -> None:
    """Print test files that use *fixture_name* anywhere in their test functions.

    Walks every test module's ``fixture_uses`` list and collects modules whose
    test functions take *fixture_name* as a parameter. Results include the
    fixture's resolved scope and defining module when known.

    Args:
        index: parsed codemap index dict (must be v5.3+ with ``fixture_uses``).
        fixture_name: name of the fixture whose reverse-dependencies are queried.
    """
    _require_feature(index, FIXTURE_GRAPH_MIN_VER, "fixture-rdeps")
    test_files: list[dict] = []
    for m in index.get("modules", []):
        if m.get("status") != "ok" or not m.get("is_test"):
            continue
        for fix in m.get("fixture_uses", []) or []:
            if fix.get("name") != fixture_name:
                continue
            test_files.append(
                {
                    "module": m.get("name", ""),
                    "path": m.get("path", ""),
                    "scope": fix.get("scope"),
                    "defined_in": fix.get("defined_in"),
                }
            )
            break
    test_files.sort(key=lambda e: (e["module"], e["path"]))
    _print(
        json.dumps(
            {
                "fixture": fixture_name,
                "count": len(test_files),
                "test_files": test_files,
                "index": _cmd_coverage(index, method="ast-flags", scope="pytest-fixture-deps"),
            }
        )
    )


#: Recursion depth at which fixture dependency graph traversal stops.
_FIXTURE_GRAPH_MAX_DEPTH = 10


def _build_fixture_subtree(
    fixture_name: str,
    fixture_defs: dict[str, dict],
    visited: set[str],
    depth: int,
) -> dict:
    """Recursively expand the dependency tree of *fixture_name*, bounded by depth and cycle guard.

    Returns a node dict with ``name``, ``scope``, ``defined_in``, and ``depends_on``
    (a list of further node dicts). Cycle detection: a fixture already in
    *visited* is emitted with empty ``depends_on`` and a ``cycle: True`` marker.
    Depth cap: at :data:`_FIXTURE_GRAPH_MAX_DEPTH` recursion stops and the node
    is marked ``truncated: True``.

    Args:
        fixture_name: fixture to expand at this level.
        fixture_defs: global fixture lookup from :func:`_collect_fixture_definitions`.
        visited: names of fixtures already expanded on the current path (mutated).
        depth: current recursion depth.
    """
    info = fixture_defs.get(fixture_name)
    if info is None:
        return {
            "name": fixture_name,
            "scope": None,
            "defined_in": None,
            "depends_on": [],
        }
    node: dict = {
        "name": fixture_name,
        "scope": info.get("scope"),
        "defined_in": info.get("defined_in"),
    }
    if fixture_name in visited:
        node["depends_on"] = []
        node["cycle"] = True
        return node
    if depth >= _FIXTURE_GRAPH_MAX_DEPTH:
        node["depends_on"] = []
        node["truncated"] = True
        return node
    visited.add(fixture_name)
    children: list[dict] = []
    for dep in info.get("depends_on", []) or []:
        children.append(_build_fixture_subtree(dep, fixture_defs, visited, depth + 1))
    visited.remove(fixture_name)
    node["depends_on"] = children
    return node


def _find_test_module(index: dict, query: str) -> dict | None:
    """Locate a test module by dotted name or file-path suffix.

    Resolution order: exact ``name`` match, exact ``path`` match, then path
    suffix match. Only modules with ``status == "ok"`` and ``is_test == True``
    are considered.

    Args:
        index: parsed codemap index dict.
        query: dotted module name (``tests.foo``) or path (``tests/foo.py``).
    """
    for m in index.get("modules", []):
        if m.get("status") != "ok" or not m.get("is_test"):
            continue
        if m.get("name") == query or m.get("path") == query:
            return m
    for m in index.get("modules", []):
        if m.get("status") != "ok" or not m.get("is_test"):
            continue
        path = m.get("path", "")
        if path.endswith(query):
            return m
    return None


def cmd_fixture_graph(index: dict, test_file: str) -> None:
    """Print the full fixture dependency tree for *test_file*.

    Resolves *test_file* via :func:`_find_test_module`, then expands each
    fixture the test module uses into a recursive ``depends_on`` tree using
    :func:`_build_fixture_subtree`. Depth is capped at
    :data:`_FIXTURE_GRAPH_MAX_DEPTH` and cycles are surfaced via a
    ``cycle: True`` marker rather than raising.

    Args:
        index: parsed codemap index dict (must be v5.3+ with ``fixture_uses``).
        test_file: dotted module name or path identifying the test module.
    """
    _require_feature(index, FIXTURE_GRAPH_MIN_VER, "fixture-graph")
    module_entry = _find_test_module(index, test_file)
    if module_entry is None:
        _exit_target_not_found(f"Test module '{test_file}' not found in index.", test_file)
    fixture_defs = _collect_fixture_definitions(index)
    roots: list[dict] = []
    for fix in module_entry.get("fixture_uses", []) or []:
        visited: set[str] = set()
        roots.append(_build_fixture_subtree(fix["name"], fixture_defs, visited, 0))
    _print(
        json.dumps(
            {
                "test_file": module_entry.get("name", ""),
                "path": module_entry.get("path", ""),
                "fixtures": roots,
                "count": len(roots),
                "index": _cmd_coverage(index, method="ast-flags", scope="pytest-fixture-deps"),
            }
        )
    )
