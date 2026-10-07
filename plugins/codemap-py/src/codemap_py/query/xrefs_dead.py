"""Verbs over documentation cross-references and dead symbols or modules."""

from __future__ import annotations

import argparse
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
from codemap_py.schema import DEAD_SYMBOL_MIN_VER, SPHINX_XREFS_MIN_VER  # noqa: E402

from .coverage import _cmd_coverage  # noqa: E402
from .docs_coverage import _is_public_symbol, _symbol_loc  # noqa: E402
from .index_io import _get_rev_graph, _get_symbol_map, _require_feature, _require_sphinx_xref_count  # noqa: E402
from .output import _print  # noqa: E402


def _iter_all_xrefs(index: dict):
    """Yield every xref entry in the index — module docstrings and doc files combined.

    Args:
        index: parsed codemap index dict (v4.5+).

    Yields:
        Individual xref dicts as recorded by ``scan-index``.
    """
    for m in index.get("modules", []):
        if m.get("status") == "degraded":
            continue
        for entry in m.get("sphinx_xrefs", []) or []:
            yield entry
    for entry in index.get("doc_xrefs", []) or []:
        yield entry


# Roles whose targets resolve to ``module::name`` symbol keys (must match
# scan-index ``_SPHINX_RESOLVABLE_ROLES`` minus ``mod``/``attr``/``data``).
# ``mod`` stores bare module names; ``attr``/``data`` are best-effort and may
# legitimately point at non-symbol identifiers (instance attributes, runtime
# globals) — excluding them from the broken check avoids false positives.
#: Cross-reference roles whose targets are checked against symbol keys when detecting broken references.
_SYMBOL_ROLES: frozenset[str] = frozenset({"func", "class", "meth", "exc", "mkdocs"})


def cmd_xrefs(index: dict, query: str, broken: bool) -> None:
    """List doc cross-references targeting *query*, or surface broken refs.

    Two modes (selected by *broken*):
      * **Default** — every recorded xref whose ``target`` equals *query*. The
        returned list pools module-docstring refs and ``.rst``/``.md`` doc refs.
      * **``--broken``** — *query* names a module; every xref whose target's
        module prefix matches *query* and whose target is not present in the
        symbol index is reported. ``attr``/``data``/``mod`` refs are skipped
        because they may legitimately point at non-symbol identifiers.

    Args:
        index: parsed codemap index dict (must be v4.5+ with ``sphinx_xrefs``).
        query: symbol qname (default mode) or module name (``--broken`` mode).
        broken: when True, switch to broken-ref discovery mode.
    """
    _require_feature(index, SPHINX_XREFS_MIN_VER, "sphinx_xrefs")

    if not broken:
        refs: list[dict] = []
        for entry in _iter_all_xrefs(index):
            if entry.get("target") == query:
                refs.append(
                    {
                        "role": entry.get("role", ""),
                        "file": entry.get("file", ""),
                        "line": entry.get("line", 0),
                        "source": entry.get("source", ""),
                    }
                )
        refs.sort(key=lambda r: (r["file"], r["line"], r["role"]))
        _print(
            json.dumps(
                {
                    "target": query,
                    "refs": refs,
                    "count": len(refs),
                    "index": _cmd_coverage(index, method="ast-flags", scope="sphinx-xrefs"),
                }
            )
        )
        return

    # ``--broken`` mode: scan refs whose target module matches *query* (or all if query == "").
    sym_map = _get_symbol_map(index)
    module_prefix = f"{query}::" if query else ""
    broken_refs: list[dict] = []
    seen: set[tuple[str, str, int, str]] = set()  # (target, file, line, role) — collapse module/doc duplicates
    for entry in _iter_all_xrefs(index):
        target = entry.get("target", "")
        role = entry.get("role", "")
        if role not in _SYMBOL_ROLES:
            continue
        if module_prefix and not target.startswith(module_prefix):
            continue
        if target in sym_map:
            continue
        sig = (target, entry.get("file", ""), entry.get("line", 0), role)
        if sig in seen:
            continue
        seen.add(sig)
        broken_refs.append(
            {
                "target": target,
                "role": role,
                "file": entry.get("file", ""),
                "line": entry.get("line", 0),
                "source": entry.get("source", ""),
            }
        )
    broken_refs.sort(key=lambda r: (r["target"], r["file"], r["line"]))
    _print(
        json.dumps(
            {
                "module": query,
                "broken": broken_refs,
                "count": len(broken_refs),
                "index": _cmd_coverage(index, method="ast-flags", scope="sphinx-xrefs"),
            }
        )
    )


def _fn_rdep_count(qname: str, rev_graph: dict[str, list[str]]) -> int:
    """Return the number of distinct function-level callers of *qname*.

    Args:
        qname: fully-qualified symbol key ``module::symbol``.
        rev_graph: reverse call graph from :func:`build_reverse_call_graph`.

    Examples:
        >>> _fn_rdep_count("m::f", {"m::f": ["m::a", "m::b"]})
        2
        >>> _fn_rdep_count("m::missing", {})
        0
    """
    return len(rev_graph.get(qname, []))


def cmd_dead_symbols(index: dict, args: argparse.Namespace) -> None:
    """Print public symbols with zero callers anywhere in the project.

    A symbol qualifies as "dead" when **every** signal is zero:

      * ``rdep_count == 0`` on the owning module — no static importer
      * ``fn_rdep_count == 0`` — no function-level caller (reverse call graph)
      * ``mock_rdep_count == 0`` — not mocked by any test
      * ``sphinx_xref_count[qname] == 0`` — not referenced in any docstring,
        ``.rst``, or mkdocstrings file
      * ``qualified_name`` is public (no ``_`` prefix on any component)
      * Owning module is **not** an entry-point (``if __name__ == "__main__"``)
      * Owning module is **not** a test file
      * Symbol name is **not** in the module's ``__all__`` list when present

    Modules with ``has_star_imports=True`` are skipped entirely — star imports
    prevent reliable call-graph tracing; a warning per skipped module is logged
    to stderr.

    The ``sphinx_xref_count`` table is required (fail-closed): a missing key
    aborts the command rather than silently treating documented symbols as dead.

    Output is sorted by LOC descending — biggest dead symbol first.

    Args:
        index: parsed codemap index dict (must be v4.6+).
        args: parsed argparse namespace exposing ``min_loc`` (int, default 5).
    """
    _require_feature(index, DEAD_SYMBOL_MIN_VER, "dead-symbol")
    _require_sphinx_xref_count(index)
    min_loc: int = int(args.min_loc)
    sphinx_counts: dict[str, int] = index.get("sphinx_xref_count", {})
    rev_graph = _get_rev_graph(index)

    eligible, skipped_star = _dead_symbol_eligible_modules(index)
    findings = [f for m in eligible for f in _module_dead_symbol_candidates(m, min_loc, rev_graph, sphinx_counts)]

    findings.sort(key=lambda f: (-f["loc"], f["module"], f["qualified_name"]))
    _print(
        json.dumps(
            {
                "dead": findings,
                "total": len(findings),
                "skipped_star_import": skipped_star,
                "index": _cmd_coverage(index, method="static-ast", scope="dead-symbols"),
            }
        )
    )


def _dead_symbol_eligible_modules(index: dict) -> tuple[list[dict], list[str]]:
    """Return modules eligible for dead-symbol scanning, plus star-import skips.

    A module is eligible when it is not degraded, not a test file, not an
    entry-point, has no star imports, and has zero external importers
    (``rdep_count == 0`` — a module still imported elsewhere can't have a
    truly dead symbol, since the import graph doesn't prove the symbol
    itself is unused). Star-import modules are skipped (call-graph tracing
    through ``from x import *`` is unreliable) with a per-module stderr
    warning; their names are also returned so the caller can report them.

    Args:
        index: parsed codemap index dict.
    """
    eligible: list[dict] = []
    skipped_star: list[str] = []
    for m in index.get("modules", []):
        if m.get("status") == "degraded":
            continue
        if m.get("is_test", False):
            continue
        if m.get("is_entry_point", False):
            continue
        if m.get("has_star_imports", False):
            mod_name = m.get("name", "")
            skipped_star.append(mod_name)
            _print(
                f"⚠ dead-symbol skipped {mod_name} — star imports prevent reliable call graph",
                file=sys.stderr,
            )
            continue
        if m.get("rdep_count", 0) != 0:
            continue
        eligible.append(m)
    return eligible, skipped_star


def _module_dead_symbol_candidates(
    m: dict, min_loc: int, rev_graph: dict[str, list[str]], sphinx_counts: dict[str, int]
) -> list[dict]:
    """Return this module's public symbols with zero callers/mocks/xrefs anywhere.

    Args:
        m: one module entry from the index (already filtered eligible by
            :func:`_dead_symbol_eligible_modules`).
        min_loc: skip symbols spanning fewer than this many lines.
        rev_graph: reverse call graph from :func:`_get_rev_graph`.
        sphinx_counts: the index's ``sphinx_xref_count`` table.
    """
    mod_name = m.get("name", "")
    exports: list[str] | None = m.get("exports")
    findings: list[dict] = []
    for sym in m.get("symbols", []):
        qname = sym.get("qualified_name", "")
        if not _is_public_symbol(qname):
            continue
        if exports is not None and sym.get("name", "") in exports:
            continue
        loc = _symbol_loc(sym)
        if loc < min_loc:
            continue
        full_qname = f"{mod_name}::{qname}"
        fn_rdeps = _fn_rdep_count(full_qname, rev_graph)
        if fn_rdeps != 0:
            continue
        if sym.get("mock_rdep_count", 0) != 0:
            continue
        if sphinx_counts.get(full_qname, 0) != 0:
            continue
        findings.append(
            {
                "name": sym.get("name", ""),
                "module": mod_name,
                "qualified_name": qname,
                "loc": loc,
                "rdep_count": m.get("rdep_count", 0),
                "fn_rdep_count": fn_rdeps,
            }
        )
    return findings


def cmd_dead_modules(index: dict, args: argparse.Namespace) -> None:
    """Print modules with zero external importers (likely dead modules).

    A module qualifies as dead when:

      * ``rdep_count == 0`` — no other module imports it statically
      * ``is_entry_point == False`` — not a runnable ``__main__`` script
      * ``is_test == False`` — not a test file

    Dynamic importers (``dynamic_imported_by``) and config-file references
    (``config_refs``) are deliberately ignored: a module reachable only via
    dynamic dispatch is, by this definition, structurally dead from the static
    call graph's point of view. Callers needing dynamic awareness should use
    ``rdeps <module>`` to inspect the full reverse-import surface.

    Output is sorted by LOC descending — biggest dead module first.

    Args:
        index: parsed codemap index dict (must be v4.6+).
        args: parsed argparse namespace (no flags consumed today, reserved
            for future filtering options).
    """
    _require_feature(index, DEAD_SYMBOL_MIN_VER, "dead-symbol")
    _ = args  # placeholder — kept for future filtering knobs (e.g. ``--min-loc``)

    findings: list[dict] = []
    for m in index.get("modules", []):
        if m.get("status") == "degraded":
            continue
        if m.get("is_test", False):
            continue
        if m.get("is_entry_point", False):
            continue
        if m.get("rdep_count", 0) != 0:
            continue
        findings.append(
            {
                "name": m.get("name", ""),
                "rdep_count": m.get("rdep_count", 0),
                "loc": m.get("loc", 0),
            }
        )

    findings.sort(key=lambda f: (-f["loc"], f["name"]))
    _print(
        json.dumps(
            {
                "dead_modules": findings,
                "total": len(findings),
                "index": _cmd_coverage(index, method="import-graph", scope="dead-modules"),
            }
        )
    )
