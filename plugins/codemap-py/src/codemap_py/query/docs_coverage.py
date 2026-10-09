"""Verbs reporting docstring and line-coverage gaps."""

from __future__ import annotations

import argparse
import json
import sys
from enum import Enum
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
from codemap_py.schema import COVERAGE_MIN_VER, DOCSTRING_MIN_VER, UNCOVERED_MIN_VER  # noqa: E402

from .coverage import _cmd_coverage  # noqa: E402
from .errors import _die_module_not_indexed, _exit_error, _exit_target_not_found  # noqa: E402
from .index_io import _require_feature  # noqa: E402
from .output import _print  # noqa: E402


def _is_public_symbol(name: str) -> bool:
    """Return True when *name* is a public identifier (no leading underscore).

    Dunder names like ``__init__`` start with an underscore and are excluded —
    this matches the simplest rule consistent with "no leading ``_``" filtering.

    Examples:
        >>> _is_public_symbol("foo")
        True
        >>> _is_public_symbol("_helper")
        False
        >>> _is_public_symbol("__init__")
        False
        >>> _is_public_symbol("MyClass.method")
        True
        >>> _is_public_symbol("MyClass._priv")
        False
    """
    if not name:
        return False
    for part in name.split("."):
        if not part or part.startswith("_"):
            return False
    return True


def _symbol_loc(sym: dict) -> int:
    """Return the (end_line − start_line) span of a symbol; 0 when either is missing.

    Examples:
        >>> _symbol_loc({"start_line": 10, "end_line": 25})
        15
        >>> _symbol_loc({"start_line": 5})
        0
        >>> _symbol_loc({})
        0
    """
    start = sym.get("start_line")
    end = sym.get("end_line")
    if not isinstance(start, int) or not isinstance(end, int):
        return 0
    return end - start


def cmd_undocumented(index: dict, module: str | None, all_modules: bool) -> None:
    """List public symbols missing a docstring, sorted by LOC descending.

    Public symbol = no component of ``qualified_name`` starts with ``_`` —
    excludes dunders (``__init__``), private helpers (``_compute``), and private
    class names (``_Cache``). Test modules (``is_test=True``) are always skipped.

    Args:
        index: parsed codemap index dict (must be v4.4+ with ``has_docstring``).
        module: when set, restrict scan to this dotted module name only.
        all_modules: when True, scan every non-test module in the index.

    Examples:
        See ``tests/test_scan_query.py::TestDocstringCoverage`` for end-to-end coverage.
    """
    _require_feature(index, DOCSTRING_MIN_VER, "has_docstring")
    modules = index.get("modules", [])
    if module is not None:
        modules = [m for m in modules if m.get("name") == module]
    else:
        modules = [m for m in modules if not m.get("is_test")]
    # all_modules is the default and a no-op flag; preserved for explicit CLI clarity.
    _ = all_modules

    findings: list[dict] = []
    for m in modules:
        if m.get("status") == "degraded":
            continue
        mod_name = m.get("name", "")
        for sym in m.get("symbols", []):
            if sym.get("has_docstring", False):
                continue
            if not _is_public_symbol(sym.get("qualified_name", "")):
                continue
            findings.append(
                {
                    "name": sym.get("name", ""),
                    "qualified_name": sym.get("qualified_name", ""),
                    "module": mod_name,
                    "type": sym.get("type", ""),
                    "loc": _symbol_loc(sym),
                    "start_line": sym.get("start_line", 0),
                    "end_line": sym.get("end_line", 0),
                    "docstring_first_line": sym.get("docstring_first_line"),
                }
            )

    findings.sort(key=lambda f: (-f["loc"], f["module"], f["qualified_name"]))
    unique_qualified_names = sorted({finding["qualified_name"] for finding in findings})
    payload: dict = {
        "undocumented": findings,
        "total": len(findings),
        "unique_total": len(unique_qualified_names),
        "unique_qualified_names": unique_qualified_names,
        "count_semantics": {
            "total": "Undocumented public symbol declarations. Multiple declarations may share one qualified name.",
            "unique_total": "Unique qualified names among undocumented public symbol declarations.",
        },
        "index": _cmd_coverage(
            index, method="ast-flags", scope="public-api-only", excludes=["private", "dunder", "test-modules"]
        ),
    }
    if module is not None:
        payload["module"] = module
    _print(json.dumps(payload))


def _module_uncovered_candidates(m: dict) -> list[dict]:
    """Return this module's public symbols with zero test callers and zero mocks.

    Args:
        m: one module entry from the index. Degraded and test modules yield
            no candidates (the caller may also pre-filter these; the check
            is repeated here so this helper is safe to call on any module).
    """
    if m.get("status") == "degraded":
        return []
    if m.get("is_test", False):
        return []
    mod_name = m.get("name", "")
    findings: list[dict] = []
    for sym in m.get("symbols", []):
        qname = sym.get("qualified_name", "")
        if not _is_public_symbol(qname):
            continue
        if sym.get("fn_rdep_test_count", 0) != 0:
            continue
        if sym.get("mock_rdep_count", 0) != 0:
            continue
        findings.append(
            {
                "name": sym.get("name", ""),
                "module": mod_name,
                "qualified_name": qname,
                "loc": _symbol_loc(sym),
                "fn_rdep_test_count": sym.get("fn_rdep_test_count", 0),
                "mock_rdep_count": sym.get("mock_rdep_count", 0),
            }
        )
    return findings


class UncoveredSort(str, Enum):
    """Sort order for the ``uncovered`` query.

    Inherits str so CLI values map straight onto members.
    """

    LOC = "loc"
    NAME = "name"
    MODULE = "module"


def cmd_uncovered(index: dict, args: argparse.Namespace) -> None:
    """Print public symbols with no test coverage, sorted and capped to ``--top``.

    A symbol is uncovered when ALL of the following hold:
      * ``qualified_name`` is public per :func:`_is_public_symbol` (no leading
        ``_`` in any dotted component — excludes dunders, private helpers,
        private classes).
      * ``fn_rdep_test_count == 0`` — no caller in a test module reaches it.
      * ``mock_rdep_count == 0`` — no test mocks it via ``patch()``.

    Only non-test modules are scanned. ``fn_rdep_test_count`` and
    ``mock_rdep_count`` are stored fields (v4.1+); the query reads them
    directly without rebuilding the call graph.

    Args:
        index: parsed codemap index dict (must be v4.2+ with ``fn_rdep_test_count``).
        args: parsed argparse namespace exposing ``module`` (str | None),
            ``all_modules`` (bool), ``sort`` (an :class:`UncoveredSort` value),
            and ``top`` (int).

    Examples:
        See ``tests/test_scan_query.py::TestUncovered`` for end-to-end coverage.
    """
    _require_feature(index, UNCOVERED_MIN_VER, "fn_rdep_test_count")
    module: str | None = args.module
    all_modules: bool = args.all_modules
    if module is None and not all_modules:
        _exit_error("Pass a module name or --all to scan every non-test module.")
    # all_modules is the default and a no-op flag; preserved for explicit CLI clarity.
    _ = all_modules

    modules = index.get("modules", [])
    if module is not None:
        modules = [m for m in modules if m.get("name") == module]
    else:
        modules = [m for m in modules if not m.get("is_test")]

    findings = [f for m in modules for f in _module_uncovered_candidates(m)]

    sort_key = UncoveredSort(args.sort)
    if sort_key == UncoveredSort.NAME:
        findings.sort(key=lambda f: (f["qualified_name"], f["module"]))
    elif sort_key == UncoveredSort.MODULE:
        findings.sort(key=lambda f: (f["module"], f["qualified_name"]))
    else:  # UncoveredSort.LOC — default
        findings.sort(key=lambda f: (-f["loc"], f["module"], f["qualified_name"]))

    total = len(findings)
    unique_qualified_names = sorted({finding["qualified_name"] for finding in findings})
    top_n = max(0, int(args.top))
    showing = min(total, top_n)
    findings = findings[:top_n]

    payload: dict = {
        "uncovered": findings,
        "selection": {
            "scope": "exact-module" if module is not None else "all-non-test-modules",
            "matched_modules": len(modules),
            "includes_descendants": module is None,
        },
        "total": total,
        "showing": showing,
        "unique_total": len(unique_qualified_names),
        "unique_qualified_names": unique_qualified_names,
        "count_semantics": {
            "definition": "Public symbols with zero test callers and zero mocks.",
            "total": "All matching static public symbol declarations before the --top display cap.",
            "showing": "Number of matching declarations included in uncovered after the --top display cap.",
            "unique_total": "Unique qualified names among all matching static public symbol declarations.",
        },
        "index": _cmd_coverage(index, method="ast-flags", scope="public-api-only", excludes=["private", "dunder"]),
    }
    if module is not None:
        payload["module"] = module
    _print(json.dumps(payload))


def _split_coverage_qname(qname: str) -> tuple[str, str | None]:
    """Split a ``module::symbol`` query into ``(module, symbol)``; bare module → ``(module, None)``.

    Examples:
        >>> _split_coverage_qname("pkg.mod::func")
        ('pkg.mod', 'func')
        >>> _split_coverage_qname("pkg.mod")
        ('pkg.mod', None)
        >>> _split_coverage_qname("pkg.mod::Cls.method")
        ('pkg.mod', 'Cls.method')
    """
    if "::" in qname:
        module, symbol = qname.split("::", 1)
        return module, symbol
    return qname, None


def _find_module(index: dict, module_name: str) -> dict | None:
    """Return the module entry whose ``name`` matches *module_name*, or ``None``."""
    for m in index.get("modules", []):
        if m.get("name") == module_name:
            return m
    return None


def cmd_coverage(index: dict, qname: str) -> None:
    """Show ``coverage_pct`` and ``covered_by`` for a specific symbol or whole module.

    Accepts two query shapes:

      * ``module::symbol`` — return one symbol's coverage fields, or an explicit
        error JSON when the symbol is not present in the index or its coverage
        fields were not populated (index built without ``--with-coverage``).
      * ``module`` — return the per-symbol coverage map for every symbol in the
        module that has coverage data attached.

    Args:
        index: parsed codemap index dict (must be v5.4+).
        qname: ``module::symbol`` query string, or a bare ``module`` name.
    """
    _require_feature(index, COVERAGE_MIN_VER, "coverage")
    module_name, symbol_name = _split_coverage_qname(qname)
    module = _find_module(index, module_name)
    if module is None:
        _die_module_not_indexed(index, module_name)
    if symbol_name is None:
        rows: list[dict] = []
        for sym in module.get("symbols", []):
            if sym.get("coverage_pct") is None:
                continue
            rows.append(
                {
                    "qualified_name": sym.get("qualified_name", ""),
                    "type": sym.get("type", ""),
                    "coverage_pct": sym.get("coverage_pct"),
                    "covered_by": sym.get("covered_by"),
                    "start_line": sym.get("start_line", 0),
                    "end_line": sym.get("end_line", 0),
                }
            )
        _print(
            json.dumps(
                {
                    "module": module_name,
                    "symbols": rows,
                    "total": len(rows),
                    "measurement": _coverage_measurement(module.get("symbols", [])),
                    "selection": {"scope": "exact-module", "matched_modules": 1, "includes_descendants": False},
                    "index": _cmd_coverage(index, method="ast-flags", scope="line-coverage"),
                }
            )
        )
        return

    for sym in module.get("symbols", []):
        if sym.get("qualified_name") != symbol_name:
            continue
        if "coverage_pct" not in sym:
            _exit_error(
                f"Symbol '{module_name}::{symbol_name}' has no coverage data — "
                "rebuild the index with `scan-index --with-coverage <path>`."
            )
        _print(
            json.dumps(
                {
                    "module": module_name,
                    "qualified_name": symbol_name,
                    "type": sym.get("type", ""),
                    "coverage_pct": sym.get("coverage_pct"),
                    "covered_by": sym.get("covered_by"),
                    "start_line": sym.get("start_line", 0),
                    "end_line": sym.get("end_line", 0),
                    "index": _cmd_coverage(index, method="ast-flags", scope="line-coverage"),
                }
            )
        )
        return
    _exit_target_not_found(
        f"Symbol '{module_name}::{symbol_name}' not found in module.", f"{module_name}::{symbol_name}"
    )


def _coverage_measurement(symbols: list[dict]) -> dict:
    """Describe measurement availability without treating missing data as zero coverage."""
    measured = sum(sym.get("coverage_pct") is not None for sym in symbols)
    if not symbols:
        status = "empty"
    elif not measured:
        status = "unavailable"
    else:
        status = "available" if measured == len(symbols) else "partial"
    return {"status": status, "symbols": len(symbols), "measured_symbols": measured}


def _module_coverage_gap_candidates(m: dict, threshold: float) -> list[dict]:
    """Return this module's public symbols whose ``coverage_pct`` is strictly below *threshold*.

    Args:
        m: one module entry from the index (degraded/test modules yield nothing).
        threshold: lower bound on acceptable coverage.
    """
    if m.get("status") == "degraded":
        return []
    if m.get("is_test", False):
        return []
    mod_name = m.get("name", "")
    findings: list[dict] = []
    for sym in m.get("symbols", []):
        qname = sym.get("qualified_name", "")
        if not _is_public_symbol(qname):
            continue
        pct = sym.get("coverage_pct")
        if pct is None:
            continue
        if pct >= threshold:
            continue
        findings.append(
            {
                "module": mod_name,
                "qualified_name": qname,
                "type": sym.get("type", ""),
                "coverage_pct": pct,
                "gap": round(threshold - pct, 4),
                "start_line": sym.get("start_line", 0),
                "end_line": sym.get("end_line", 0),
            }
        )
    return findings


def cmd_coverage_gap(
    index: dict,
    module: str | None,
    all_modules: bool,
    threshold: float,
) -> None:
    """List public symbols whose ``coverage_pct`` is strictly below *threshold*.

    Findings are sorted by ``gap = threshold - coverage_pct`` descending so the
    largest-coverage holes appear first. Only ``status == "ok"`` modules are
    scanned; test modules are skipped (matching ``cmd_uncovered``). Symbols
    without ``coverage_pct`` do not contribute findings; measurement metadata keeps
    unavailable data distinct from measured coverage. Static test relationships
    reported by ``uncovered`` are a different metric, not replacement measurements.

    Args:
        index: parsed codemap index dict (must be v5.4+).
        module: when set, restrict scan to this dotted module name only.
        all_modules: when True, scan every non-test module in the index.
        threshold: lower bound on acceptable coverage (default 0.8 set in argparse).
    """
    _require_feature(index, COVERAGE_MIN_VER, "coverage-gap")
    if module is None and not all_modules:
        _exit_error("Pass a module name or --all to scan every non-test module.")
    _ = all_modules  # all_modules is the default and a no-op flag; preserved for explicit CLI clarity.

    modules = index.get("modules", [])
    if module is not None:
        modules = [m for m in modules if m.get("name") == module]
    else:
        modules = [m for m in modules if not m.get("is_test")]

    findings = [f for m in modules for f in _module_coverage_gap_candidates(m, threshold)]
    eligible_symbols = [
        sym
        for m in modules
        if m.get("status") != "degraded" and not m.get("is_test", False)
        for sym in m.get("symbols", [])
        if _is_public_symbol(sym.get("qualified_name", ""))
    ]

    findings.sort(key=lambda f: (-f["gap"], f["module"], f["qualified_name"]))
    payload: dict = {
        "coverage_gap": findings,
        "total": len(findings),
        "threshold": threshold,
        "measurement": _coverage_measurement(eligible_symbols),
        "selection": {
            "scope": "exact-module" if module is not None else "all-non-test-modules",
            "matched_modules": len(modules),
            "includes_descendants": module is None,
        },
        "index": _cmd_coverage(index, method="ast-flags", scope="line-coverage-gap"),
    }
    if module is not None:
        payload["module"] = module
    _print(json.dumps(payload))
