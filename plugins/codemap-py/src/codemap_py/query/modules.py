"""Module-level verbs: dependencies, centrality, coupling, paths and packages."""

from __future__ import annotations

import json
import sys
from collections import deque
from collections.abc import Sequence
from pathlib import Path

# Transitional seam: exclusion rules live in codemap_py.scanner, but this
# module still reaches them through the old bare-name ``_exclusions`` import
# (bin/_exclusions.py, itself a shim onto codemap_py.scanner) via a
# bin/-relative sys.path insert, the same route bin/scan-index used to take.
# Every other import below is a direct package-internal import.
# parents[3] not [2]: this file sits one level deeper than the pre-split query.py
_BIN = Path(__file__).resolve().parents[3] / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))
from codemap_py.schema import (
    IMPORT_GROUPS_MIN_VER,
    EntityType,
)

from .coverage import _IMPORT_GRAPH_NOT_COVERED, _cmd_coverage
from .errors import _die_module_not_indexed
from .index_io import _require_feature, build_module_map
from .output import _print


def cmd_deps(
    index: dict,
    module: str,
    stdlib_only: bool = False,
    third_party_only: bool = False,
    internal_only: bool = False,
) -> None:
    """Print direct imports of a module as JSON, optionally filtered by import group.

    When any of *stdlib_only*, *third_party_only*, *internal_only* is True the
    returned ``direct_imports`` list is restricted to the selected group(s) using
    the per-module ``import_groups`` field (requires v4.3+ index). When no
    filter flag is set, all imports are returned and no feature check fires
    (backward-compatible with v4.0–v4.2 indexes).

    Args:
        index: parsed codemap index dict.
        module: dotted module name to look up.
        stdlib_only: if True, restrict result to imports classified as stdlib.
        third_party_only: if True, restrict result to imports classified as third-party.
        internal_only: if True, restrict result to imports classified as internal.
    """
    modules = build_module_map(index)
    entry = modules.get(module)
    if entry is None:
        _die_module_not_indexed(index, module)
    direct = entry.get("direct_imports", [])
    if stdlib_only or third_party_only or internal_only:
        _require_feature(index, IMPORT_GROUPS_MIN_VER, "import_groups")
        groups = entry.get("import_groups", {"stdlib": [], "third_party": [], "internal": []})
        selected: set[str] = set()
        if stdlib_only:
            selected.update(groups.get("stdlib", []))
        if third_party_only:
            selected.update(groups.get("third_party", []))
        if internal_only:
            selected.update(groups.get("internal", []))
        # Preserve original order from direct_imports while restricting to the selected set.
        direct = [imp for imp in direct if imp in selected]
    _print(
        json.dumps(
            {
                "module": module,
                "direct_imports": direct,
                "index": _cmd_coverage(
                    index,
                    method="import-graph",
                    module_status=entry.get("status"),
                    module_name=module,
                    not_covered=_IMPORT_GRAPH_NOT_COVERED,
                ),
            }
        )
    )


def cmd_import_types(index: dict, module: str) -> None:
    """Print all three import groups (stdlib, third_party, internal) for a module.

    Requires v4.3+ index (``import_groups`` field). Each group preserves the
    original import strings as recorded by scan-index — no normalisation.

    Args:
        index: parsed codemap index dict (must be v4.3+ with import_groups).
        module: dotted module name to look up.
    """
    _require_feature(index, IMPORT_GROUPS_MIN_VER, "import_groups")
    modules = build_module_map(index)
    entry = modules.get(module)
    if entry is None:
        _die_module_not_indexed(index, module)
    groups = entry.get("import_groups", {"stdlib": [], "third_party": [], "internal": []})
    _print(
        json.dumps(
            {
                "module": module,
                "stdlib": groups.get("stdlib", []),
                "third_party": groups.get("third_party", []),
                "internal": groups.get("internal", []),
                "index": _cmd_coverage(index, method="import-graph", not_covered=_IMPORT_GRAPH_NOT_COVERED),
            }
        )
    )


def cmd_rdeps(
    index: dict,
    module: str,
    exclude_tests: bool = False,
    entity: EntityType | None = None,
    limit: int = 0,
) -> None:
    """Print all modules that import a given module as JSON.

    Includes static importers (``imported_by``), their total before any ``limit`` truncation
    (``importer_count``), dynamic importers (``dynamic_imported_by`` — from ``importlib.import_module`` /
    ``__import__`` string literals), and config-file references (``config_refs`` — from ``pyproject.toml``,
    ``setup.cfg``, etc.). With ``exclude_tests`` the count of importers removed by that filter is reported as
    ``excluded_test_importer_count``, so production and test totals both come from a single call.

    Args:
        index: parsed codemap index dict.
        module: dotted module name whose reverse dependencies are queried.
        exclude_tests: if True, exclude test modules from static results and report how many were removed.
        entity: if set, restrict importers to this :class:`EntityType`.
        limit: maximum static importers to return; 0 keeps the exhaustive default.
    """
    modules_list = index.get("modules", [])
    if entity:
        modules_list = [m for m in modules_list if _entity_type(m) == entity]
    importers = [m for m in modules_list if module in m.get("direct_imports", [])]
    excluded_tests = sorted(m["name"] for m in importers if m.get("is_test")) if exclude_tests else []
    if exclude_tests:
        importers = [m for m in importers if not m.get("is_test")]
    result = sorted(m["name"] for m in importers)
    total_available = len(result)
    truncated = limit > 0 and total_available > limit
    if truncated:
        result = result[:limit]
    module_map = build_module_map(index)
    entry = module_map.get(module, {})
    dynamic = entry.get("dynamic_imported_by", [])
    config = entry.get("config_refs", [])
    # a module absent from the index AND imported by nothing is "not indexed",
    # not "no reverse deps" — error with suggestions instead of a misleading empty
    # ``imported_by: []``. An indexed leaf with zero importers legitimately keeps the
    # empty list (it IS in module_map); an external dep someone imports is kept too
    # (it shows up in result/dynamic/config even though it has no own module entry).
    if module not in module_map and not (result or dynamic or config):
        _die_module_not_indexed(index, module)
    coverage = _cmd_coverage(
        index,
        query_target=module,
        method="import-graph",
        not_covered=_IMPORT_GRAPH_NOT_COVERED,
        **({"confidence": "partial"} if truncated else {}),
    )
    if truncated:
        coverage["truncated"] = True
        coverage["total_available"] = total_available
    payload = {
        "module": module,
        "imported_by": result,
        "importer_count": total_available,
        "dynamic_imported_by": dynamic,
        "config_refs": config,
        "index": coverage,
    }
    if exclude_tests:
        # Both halves of the split come from one call: a caller that has to subtract the returned
        # list from a second unfiltered call to learn the test count gets the subtraction wrong.
        payload["excluded_test_importer_count"] = len(excluded_tests)
    _print(json.dumps(payload))


def _production_rdep_counts(index: dict) -> dict[str, int]:
    """Return incoming import counts after removing every test-module edge.

    ``rdep_count`` is stored during index construction and intentionally includes test importers. Using ``central`` with
    ``--exclude-tests`` needs a separate count so it describes the production import graph rather than merely hiding
    test candidates. Module aliases use the same canonicalization as graph metrics.
    """
    aliases = index.get("module_aliases", {})
    counts: dict[str, int] = {}
    for importer in index.get("modules", []):
        if importer.get("is_test"):
            continue
        for imported in importer.get("direct_imports", []):
            target = aliases.get(imported, imported)
            counts[target] = counts.get(target, 0) + 1
    return counts


def cmd_central(
    index: dict,
    top: int | None,
    exclude_tests: bool = False,
    entity: EntityType | None = None,
    among: Sequence[str] | None = None,
) -> None:
    """Print the most-imported modules ranked by reverse-dependency count.

    Ranks the whole repository by default. With *among* it ranks only the named modules, which answers
    "order *these* modules by their own in-degree" — the question left over after a ``rdeps`` call — without
    the caller intersecting a repository-wide ranking against its candidate list by hand.

    Args:
        index: parsed codemap index dict.
        top: number of top-ranked modules to return; ``None`` means 10 repository-wide, or every candidate
            when *among* is given, so a scoped ranking is never silently cut short.
        exclude_tests: if True, exclude test modules and their importer edges.
        entity: if set, restrict to this :class:`EntityType`.
        among: if set, rank only these dotted module names. Names that match no ranked candidate are
            reported back as ``unmatched`` rather than dropped, so a typo or an excluded module is visible.
    """
    candidates = [m for m in index.get("modules", []) if m.get("status") != "degraded"]
    if exclude_tests:
        candidates = [m for m in candidates if not m.get("is_test")]
    if entity:
        candidates = [m for m in candidates if _entity_type(m) == entity]
    requested = list(dict.fromkeys(among)) if among else []
    if among is not None:
        wanted = set(requested)
        candidates = [m for m in candidates if m["name"] in wanted]
    if top is None:
        top = len(candidates) if among is not None else 10
    if exclude_tests:
        rdep_counts = _production_rdep_counts(index)
        ranked = sorted(candidates, key=lambda m: (-rdep_counts.get(m["name"], 0), m["name"]))[:top]
    else:
        rdep_counts = {}
        ranked = sorted(candidates, key=lambda m: (-m.get("rdep_count", 0), m["name"]))[:top]
    payload = {
        "central": [
            {
                "name": m["name"],
                "rdep_count": rdep_counts.get(m["name"], m.get("rdep_count", 0)),
                "path": m.get("path", ""),
            }
            for m in ranked
        ],
        "index": _cmd_coverage(
            index,
            method="import-graph",
            scope="import-centrality",
            not_covered=_IMPORT_GRAPH_NOT_COVERED,
        ),
    }
    if among is not None:
        matched = {m["name"] for m in candidates}
        payload["candidate_count"] = len(candidates)
        payload["unmatched"] = [name for name in requested if name not in matched]
    _print(json.dumps(payload))


def cmd_coupled(index: dict, top: int, exclude_tests: bool = False, entity: EntityType | None = None) -> None:
    """Print the top N most-coupled modules ranked by internal import count.

    Args:
        index: parsed codemap index dict.
        top: number of top-ranked modules to return.
        exclude_tests: if True, exclude test modules from results.
        entity: if set, restrict to this :class:`EntityType`.
    """
    candidates = [m for m in index.get("modules", []) if m.get("status") != "degraded"]
    if exclude_tests:
        candidates = [m for m in candidates if not m.get("is_test")]
    if entity:
        candidates = [m for m in candidates if _entity_type(m) == entity]
    all_module_names = {m["name"] for m in index.get("modules", []) if m.get("status") == "ok"}
    # compute internal_count locally — never mutate the loaded index dict
    internal_counts = {
        m["name"]: sum(1 for i in m.get("direct_imports", []) if i in all_module_names) for m in candidates
    }
    ranked = sorted(candidates, key=lambda m: (-internal_counts.get(m["name"], 0), m["name"]))[:top]
    _print(
        json.dumps(
            {
                "coupled": [
                    {
                        "name": m["name"],
                        "dep_count": m.get("dep_count", 0),
                        "internal_dep_count": internal_counts.get(m["name"], 0),
                        "path": m.get("path", ""),
                    }
                    for m in ranked
                ],
                "index": _cmd_coverage(
                    index,
                    method="import-graph",
                    scope="coupling-score",
                    not_covered=_IMPORT_GRAPH_NOT_COVERED,
                ),
            }
        )
    )


def cmd_path(index: dict, frm: str, to: str) -> None:
    """Print the shortest import path between two modules via BFS.

    Both endpoints exist in the index (unknown modules exit ``3`` via
    :func:`_die_module_not_indexed`). When no import path connects them, this
    exits ``0`` with ``"path": null`` and ``"reason": "no-import-path"`` — a
    legitimate empty result, distinct from the ``"error"`` contract used for
    failures.

    Args:
        index: parsed codemap index dict.
        frm: source module name.
        to: target module name.
    """
    modules = build_module_map(index)
    if frm not in modules:
        _die_module_not_indexed(index, frm)
    if to not in modules:
        _die_module_not_indexed(index, to)

    queue: deque[list[str]] = deque([[frm]])
    visited: set[str] = {frm}
    while queue:
        path = queue.popleft()
        node = path[-1]
        if node == to:
            _print(
                json.dumps(
                    {
                        "from": frm,
                        "to": to,
                        "path": path,
                        "index": _cmd_coverage(
                            index,
                            method="import-graph",
                            not_covered=_IMPORT_GRAPH_NOT_COVERED,
                        ),
                    }
                )
            )
            return
        for neighbour in modules.get(node, {}).get("direct_imports", []):
            if neighbour not in visited and neighbour in modules:
                visited.add(neighbour)
                queue.append(path + [neighbour])

    _print(
        json.dumps(
            {
                "from": frm,
                "to": to,
                "path": None,
                "reason": "no-import-path",
                "index": _cmd_coverage(index, method="import-graph", not_covered=_IMPORT_GRAPH_NOT_COVERED),
            }
        )
    )


def cmd_list(index: dict, limit: int = 100) -> None:
    """Print indexed modules with their paths and status, capped at *limit*.

    diet: a large repo's full module list is the single biggest scan-query
    result. The default cap keeps the common ``list`` call small while ``total``
    and ``shown`` disclose the truncation so a caller knows to raise ``--limit``
    (or pass ``0`` for the full list) when it genuinely needs every module.

    Args:
        index: parsed codemap index dict.
        limit: max modules to emit; 0 returns all (default 100).
    """
    all_modules = [
        {"name": m["name"], "path": m.get("path", ""), "status": m.get("status", "ok")}
        for m in index.get("modules", [])
    ]
    total = len(all_modules)
    modules = all_modules if limit <= 0 else all_modules[:limit]
    _print(json.dumps({"modules": modules, "total": total, "shown": len(modules)}))


def _entity_type(m: dict) -> str:
    """Return entity_type for a module, with fallback for pre-v5.5 indexes.

    Stays a plain ``str``: the value is read back from an on-disk index that may have
    been written by an older or foreign writer, so it is not guaranteed to be an
    :class:`EntityType` member. Compare it against members with ``==``.

    Args:
        m: module entry dict from the index.
    """
    et = m.get("entity_type")
    if et:
        return et
    return EntityType.TEST.value if m.get("is_test") else EntityType.PKG.value


def _as_entity(raw: str | None) -> EntityType | None:
    """Convert an ``--entity`` CLI value to its member (argparse already gated the choices)."""
    return EntityType(raw) if raw else None


def _as_module_list(raw: str | None) -> list[str] | None:
    """Split a comma-separated ``--among`` value into dotted module names.

    An omitted flag stays ``None`` (rank the repository); a supplied but empty value yields an empty list, which
    scopes the ranking to nothing rather than silently widening it back to every module.

    Examples:
        >>> _as_module_list("a.b, c.d")
        ['a.b', 'c.d']
        >>> _as_module_list(None) is None
        True
    """
    if raw is None:
        return None
    return [name.strip() for name in raw.split(",") if name.strip()]


def cmd_packages(index: dict) -> None:
    """Print top-level packages with module counts broken down by entity_type.

    For indexes predating v5.5 (no entity_type field), entity is derived from
    is_test (test vs pkg); docs and example are not distinguished.

    Args:
        index: parsed codemap index dict.
    """
    modules = [m for m in index.get("modules", []) if m.get("status") == "ok"]
    pkg_data: dict[str, dict] = {}
    for m in modules:
        pkg = m.get("package") or m["name"].split(".")[0]
        entity = _entity_type(m)
        if pkg not in pkg_data:
            pkg_data[pkg] = {"total": 0, "by_entity": {}}
        pkg_data[pkg]["total"] += 1
        by_entity = pkg_data[pkg]["by_entity"]
        by_entity[entity] = by_entity.get(entity, 0) + 1
    sorted_pkgs = sorted(pkg_data.items(), key=lambda kv: kv[1]["total"], reverse=True)
    _print(
        json.dumps(
            {
                "packages": [
                    {"name": pkg, "total": data["total"], "by_entity": data["by_entity"]} for pkg, data in sorted_pkgs
                ],
                "index": _cmd_coverage(index, method="entity-map"),
            }
        )
    )
