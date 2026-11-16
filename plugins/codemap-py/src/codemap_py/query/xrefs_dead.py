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
      * Symbol is **not** the target of a root ``pyproject.toml`` entry point (``[project.scripts]``,
        ``[project.gui-scripts]``, ``[project.entry-points.*]``), nor a class enclosing that target

    A finding in a top-level module or package ``__init__.py`` that declares no ``__all__`` carries
    ``possibly_public: true``: outside code may import it, and the payload then carries one fixed
    ``possibly_public_check`` text. The finding stays listed.

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
    payload = {
        "dead": findings,
        "total": len(findings),
        "skipped_star_import": skipped_star,
        "index": _cmd_coverage(index, method="static-ast", scope="dead-symbols"),
    }
    _add_possibly_public_check(payload, findings)
    _print(json.dumps(payload))


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


#: Fixed, identity-free text telling the reader how to confirm a ``possibly_public`` finding; emitted once per payload.
_POSSIBLY_PUBLIC_CHECK = (
    "possibly_public findings sit where outside code can import them (a top-level module, a package __init__.py, "
    "or a module that declares __all__); the index sees only this project, so confirm no downstream project "
    "or documented API uses them before deleting"
)


def _is_package_init(m: dict) -> bool:
    """Return True when the module record comes from a package ``__init__.py`` file.

    Examples:
        >>> _is_package_init({"path": "src/pkg/__init__.py"})
        True
        >>> _is_package_init({"path": "src/pkg/mod.py"})
        False
        >>> _is_package_init({})
        False
    """
    path = m.get("path")
    return isinstance(path, str) and path.rsplit("/", 1)[-1] == "__init__.py"


def _is_top_level_or_init(m: dict) -> bool:
    """Return True for a top-level module (no dotted name) or any package ``__init__.py``.

    Examples:
        >>> _is_top_level_or_init({"name": "lib"})
        True
        >>> _is_top_level_or_init({"name": "lib.sub", "path": "lib/sub/__init__.py"})
        True
        >>> _is_top_level_or_init({"name": "lib.sub", "path": "lib/sub.py"})
        False
    """
    return "." not in str(m.get("name", "")) or _is_package_init(m)


def _symbol_possibly_public(m: dict) -> bool:
    """Return True when a dead-symbol finding in this module may be a public API of the project.

    Only symbols of a top-level module or package ``__init__.py`` carry the hint, and only when the module declares no
    ``__all__`` (a declared ``__all__`` already settles which names are public, and dead-symbols skips those). The
    other cues cannot fire here: a module with importers is never eligible, an entry-point module is skipped, and an
    entry-point target is removed from the findings.

    Examples:
        >>> _symbol_possibly_public({"name": "lib"})
        True
        >>> _symbol_possibly_public({"name": "lib", "exports": ["a"]})
        False
        >>> _symbol_possibly_public({"name": "lib.deep.mod", "path": "lib/deep/mod.py"})
        False
    """
    return m.get("exports") is None and _is_top_level_or_init(m)


def _module_possibly_public(m: dict) -> bool:
    """Return True when a dead-module finding may be a public module of the project.

    Cues: the module declares ``__all__``, is a top-level module, or is a package ``__init__.py``. The entry-point cue
    is not needed: an entry-point module never reaches the findings.

    Examples:
        >>> _module_possibly_public({"name": "lib.deep.mod", "path": "lib/deep/mod.py", "exports": []})
        True
        >>> _module_possibly_public({"name": "lib.deep.mod", "path": "lib/deep/mod.py"})
        False
    """
    return m.get("exports") is not None or _is_top_level_or_init(m)


def _add_possibly_public_check(payload: dict, findings: list[dict]) -> None:
    """Add the fixed ``possibly_public_check`` text to *payload* when any finding carries the hint.

    Examples:
        >>> p = {}; _add_possibly_public_check(p, [{"possibly_public": True}]); sorted(p)
        ['possibly_public_check']
        >>> p = {}; _add_possibly_public_check(p, [{}]); p
        {}
    """
    if any(f.get("possibly_public") for f in findings):
        payload["possibly_public_check"] = _POSSIBLY_PUBLIC_CHECK


def _entry_point_refs(m: dict) -> list[dict]:
    """Return the module's well-formed ``entry_point_refs`` records.

    The index validator does not know this key, so anything other than a list of dicts reads as no reference.

    Examples:
        >>> _entry_point_refs({"entry_point_refs": [{"symbol": "main"}, "junk"]})
        [{'symbol': 'main'}]
        >>> _entry_point_refs({"entry_point_refs": "junk"})
        []
    """
    refs = m.get("entry_point_refs")
    if not isinstance(refs, list):
        return []
    return [ref for ref in refs if isinstance(ref, dict)]


def _entry_point_symbols(m: dict) -> set[str]:
    """Return the qualified names that loading the module's entry points looks up.

    Loading ``pkg.mod:Cls.method`` reads ``Cls`` from the module and then ``method`` from it, so both ``Cls`` and
    ``Cls.method`` count as referenced. A module-only target (``pkg.plugin``) names no symbol.

    Examples:
        >>> sorted(_entry_point_symbols({"entry_point_refs": [{"symbol": "Cls.method"}, {"symbol": None}]}))
        ['Cls', 'Cls.method']
        >>> _entry_point_symbols({})
        set()
    """
    names: set[str] = set()
    for ref in _entry_point_refs(m):
        symbol = ref.get("symbol")
        if isinstance(symbol, str) and symbol:
            parts = symbol.split(".")
            names.update(".".join(parts[:end]) for end in range(1, len(parts) + 1))
    return names


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
    entry_point_symbols = _entry_point_symbols(m)
    possibly_public = _symbol_possibly_public(m)
    findings: list[dict] = []
    for sym in m.get("symbols", []):
        qname = sym.get("qualified_name", "")
        if not _is_public_symbol(qname):
            continue
        if exports is not None and sym.get("name", "") in exports:
            continue
        if qname in entry_point_symbols:
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
        finding = {
            "name": sym.get("name", ""),
            "module": mod_name,
            "qualified_name": qname,
            "loc": loc,
            "rdep_count": m.get("rdep_count", 0),
            "fn_rdep_count": fn_rdeps,
        }
        if possibly_public:
            finding["possibly_public"] = True
        findings.append(finding)
    return findings


def cmd_dead_modules(index: dict, args: argparse.Namespace) -> None:
    """Print modules with zero external importers (likely dead modules).

    A module qualifies as dead when:

      * ``rdep_count == 0`` — no other module imports it statically
      * ``is_entry_point == False`` — not a runnable ``__main__`` script
      * ``is_test == False`` — not a test file
      * no ``entry_point_refs`` — not the target module of an entry point declared
        in the root ``pyproject.toml`` (packaging tools import it by name)

    A finding that is a top-level module, a package ``__init__.py`` or a module declaring ``__all__`` carries
    ``possibly_public: true`` (outside code may import it) and the payload then carries one fixed
    ``possibly_public_check`` text; the finding stays listed.

    Dynamic importers (``dynamic_imported_by``) and generic config-file
    references (``config_refs``) are deliberately ignored: a module reachable
    only via dynamic dispatch is, by this definition, structurally dead from the
    static call graph's point of view. Callers needing dynamic awareness should
    use ``rdeps <module>`` to inspect the full reverse-import surface.

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
        if _entry_point_refs(m):
            continue
        finding = {
            "name": m.get("name", ""),
            "rdep_count": m.get("rdep_count", 0),
            "loc": m.get("loc", 0),
        }
        if _module_possibly_public(m):
            finding["possibly_public"] = True
        findings.append(finding)

    findings.sort(key=lambda f: (-f["loc"], f["name"]))
    payload = {
        "dead_modules": findings,
        "total": len(findings),
        "index": _cmd_coverage(index, method="import-graph", scope="dead-modules"),
    }
    _add_possibly_public_check(payload, findings)
    _print(json.dumps(payload))
