"""codemap_py.graph — cross-module graph construction, coverage, and scan orchestration.

Combines the flat per-file module entries produced by :mod:`codemap_py.scanner` into the full codemap index: metrics for
reverse dependencies and call graphs, import classification, fixture dependency graphs, coverage annotation, doc-xref
reverse indexes, subprocess call reverse indexes, name-collision dedup across source roots, and the top-level
``scan()``/``incremental_scan()`` pipeline that ``scan-index`` runs (full or incremental) before writing the index JSON
atomically.

``bin/scan-index`` is a thin launcher that calls :func:`main` below over the same argv contract and exit codes.

consumers: bin/scan-index (thin launcher calling main()), codemap_py.cli (via subprocess)
"""

from __future__ import annotations

import argparse
import ast
import errno
import json
import multiprocessing
import os
import signal
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from codemap_py import index_paths, rwgate
from codemap_py.schema import SCAN_VERSION
from codemap_py.scanner import (
    _GLOB_META_RE,
    _STDLIB_MODULES,
    Exclusions,
    _collect_module_aliases,
    _effective_src_root,
    _iter_doc_files,
    _iter_python_files,
    _load_exclusions,
    _parse_file,
    _parse_file_star,
    detect_src_root,
    extract_fixture_uses,
    extract_fixtures,
    extract_subprocess_calls,
    find_root,
    get_file_hashes,
    get_git_sha,
    load_src_roots,
    scan_config_refs,
    scan_mkdocs_xrefs,
    scan_rst_xrefs,
)
from codemap_py.telemetry import CliInvocation

_WINDOWS_REPLACE_RETRIES = 8
_WINDOWS_REPLACE_DELAY_SECONDS = 0.025
_REFRESH_TRIGGERS = {
    "missing_index_explicit",
    "claude_prompt_background",
    "codex_prompt_background",
    "query_self_heal",
    "explicit_scan",
    "direct_cli",
}


def _conftest_depth(path: str) -> int:
    """Return the directory depth of a conftest path; deeper conftests shadow shallower ones.

    A bare ``conftest.py`` at the project root has depth 0; ``tests/conftest.py``
    has depth 1, and so on. Used to sort the conftest list so deeper conftests
    are processed last and win on collision via dict-update semantics.

    Examples:
        >>> _conftest_depth("conftest.py")
        0
        >>> _conftest_depth("tests/conftest.py")
        1
        >>> _conftest_depth("tests/unit/conftest.py")
        2
    """
    parts = Path(path).parts
    return max(len(parts) - 1, 0)


def _collect_conftest_fixture_exports(modules: list[dict]) -> dict[str, dict]:
    """Aggregate every conftest's fixture exports into a single ``name -> info`` map.

    Processes conftest modules sorted by directory depth ascending so deeper
    conftests overwrite shallower ones on name collision — matching pytest's
    "closer conftest wins" scoping rule approximately.

    Args:
        modules: list of module entry dicts (must have ``fixture_exports``
            already attached to conftest entries by the earlier scan pass).

    Returns:
        Mapping ``fixture_name -> {"scope": str, "defined_in": str}``.
    """
    conftests = [m for m in modules if Path(m.get("path", "")).name == "conftest.py"]
    conftests.sort(key=lambda m: _conftest_depth(m.get("path", "")))
    aggregated: dict[str, dict] = {}
    for m in conftests:
        for fix in m.get("fixture_exports", []) or []:
            aggregated[fix["name"]] = {
                "scope": fix.get("scope", "function"),
                "defined_in": m.get("name", ""),
            }
    return aggregated


def _attach_fixture_graph(modules: list[dict], root: Path) -> None:
    """Walk every parseable module and attach fixture exports/uses in two passes.

    Pass 1 — re-parse each ``status == "ok"`` module to extract any
    ``@pytest.fixture`` definitions; conftest and test files alike may define
    fixtures locally. Non-test, non-conftest modules get an empty
    ``fixture_exports`` list to keep the schema uniform.

    Pass 2 — for every test module (``is_test=True``), reparse and resolve each
    test function's parameter list against (a) fixtures defined in the same
    module and (b) the aggregated conftest exports from pass 1.

    Args:
        modules: module entry list (mutated in-place).
        root: project root used to resolve relative file paths.
    """
    # Pass 1: extract fixture_exports for every parseable module.
    for m in modules:
        if m.get("status") != "ok":
            continue
        path = m.get("path", "")
        if not path:
            m["fixture_exports"] = []
            continue
        filepath = root / path
        try:
            source = filepath.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(filepath))
        except (OSError, SyntaxError):
            m["fixture_exports"] = []
            continue
        m["fixture_exports"] = extract_fixtures(tree, filepath)

    # Aggregate conftest exports once (deep-conftest wins).
    all_conftest_exports = _collect_conftest_fixture_exports(modules)

    # Pass 2: for test modules, extract fixture_uses.
    for m in modules:
        if m.get("status") != "ok" or not m.get("is_test"):
            continue
        path = m.get("path", "")
        if not path:
            m["fixture_uses"] = []
            continue
        filepath = root / path
        try:
            source = filepath.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(filepath))
        except (OSError, SyntaxError):
            m["fixture_uses"] = []
            continue
        defined_local = {f["name"]: f for f in m.get("fixture_exports", []) or []}
        m["fixture_uses"] = extract_fixture_uses(tree, defined_local, all_conftest_exports)


def _build_fixture_rdep_count(modules: list[dict]) -> dict[str, int]:
    """Count how many distinct test modules use each fixture (reverse index).

    Args:
        modules: module entry list with ``fixture_uses`` already attached.

    Returns:
        Mapping ``fixture_name -> count of test modules referencing it``.
    """
    counts: dict[str, int] = {}
    for m in modules:
        for fix in m.get("fixture_uses", []) or []:
            name = fix.get("name")
            if not name:
                continue
            counts[name] = counts.get(name, 0) + 1
    return counts


# ── v5.4 coverage.py integration ───────────────────────────────────────────────

# Minimum coverage library version required for the CoverageData public API used
# below (read(), measured_files(), lines(), contexts_by_lineno()). 7.4 was the
# first release to stabilise the SQLite schema and the contexts_by_lineno API
# shape we depend on; lower versions raise/return unexpected types.
_COVERAGE_MIN_LIB_VERSION: tuple[int, int] = (7, 4)


def _parse_coverage_version(version: str) -> tuple[int, int] | None:
    """Parse the first two components of a dotted version string into ``(major, minor)``.

    Returns ``None`` when the first two components are not integers — keeps the
    caller's error path simple (any unparsable version is treated as too old).

    Examples:
        >>> _parse_coverage_version("7.4.1")
        (7, 4)
        >>> _parse_coverage_version("7.10")
        (7, 10)
        >>> _parse_coverage_version("garbage") is None
        True
    """
    parts = version.split(".")
    if len(parts) < 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def _read_coverage_data(coverage_path: Path) -> dict[str, dict] | None:
    """Read a ``.coverage`` SQLite file and return per-file coverage information.

    Returns ``None`` (with a stderr warning) when any of the following holds:

      * ``coverage`` is not importable;
      * the installed library is older than :data:`_COVERAGE_MIN_LIB_VERSION`;
      * ``coverage_path`` does not exist on disk;
      * the file is corrupted or unreadable by ``CoverageData.read()``.

    On success, each measured source file is mapped to a dict with two keys:

      * ``lines`` — frozenset of measured line numbers (line-coverage mode);
      * ``contexts`` — ``{lineno: [context_str, ...]}`` mapping (empty when
        ``--cov-context`` was not enabled at collection time).

    Args:
        coverage_path: filesystem path to the coverage SQLite database.

    Returns:
        Mapping ``absolute_file_path -> {"lines": frozenset[int], "contexts": dict}``,
        or ``None`` on any failure mode listed above.
    """
    try:
        import coverage as _coverage  # local import — optional dependency
    except ImportError:
        print("⚠ coverage not installed — skipping coverage integration", file=sys.stderr)
        return None

    version = getattr(_coverage, "__version__", "0.0")
    parsed = _parse_coverage_version(version)
    if parsed is None or parsed < _COVERAGE_MIN_LIB_VERSION:
        min_str = ".".join(str(p) for p in _COVERAGE_MIN_LIB_VERSION)
        print(f"⚠ coverage {min_str}+ required, found {version} — skipping", file=sys.stderr)
        return None

    if not coverage_path.exists():
        print(f"⚠ .coverage file not found at {coverage_path} — skipping", file=sys.stderr)
        return None

    try:
        data = _coverage.CoverageData(basename=str(coverage_path))
        data.read()
    except Exception as exc:
        print(f"⚠ failed to read coverage data at {coverage_path}: {exc} — skipping", file=sys.stderr)
        return None

    result: dict[str, dict] = {}
    for measured_file in data.measured_files():
        lines = data.lines(measured_file)
        if lines is None:
            # File was tracked but no line data (arc-only mode without ``--branch`` line coverage).
            measured_lines: frozenset[int] = frozenset()
        else:
            measured_lines = frozenset(lines)
        try:
            contexts = data.contexts_by_lineno(measured_file)
        except Exception:
            # contexts_by_lineno may raise for arc-only files; treat as no contexts.
            contexts = {}
        result[measured_file] = {
            "lines": measured_lines,
            "contexts": {int(ln): list(ctxs) for ln, ctxs in contexts.items()},
        }
    return result


def _match_coverage_file(module_path: Path, coverage_data: dict[str, dict]) -> dict | None:
    """Resolve a module's filesystem path against the coverage-data keys.

    Coverage records measured files via the path seen at collection time — that
    may be absolute, may differ from the index's relative form, and may use a
    different case-fold on the filesystem. Match in two passes:

      1. Exact absolute match (``module_path`` resolved + cast to string).
      2. Suffix match — coverage key ends with the module's relative path.

    Args:
        module_path: absolute filesystem path of the module being indexed.
        coverage_data: result of :func:`_read_coverage_data` (file → info).

    Returns:
        The matched ``{"lines", "contexts"}`` dict, or ``None`` when no entry
        matches.
    """
    try:
        resolved_abs = str(module_path.resolve())
    except OSError:
        resolved_abs = str(module_path)
    if resolved_abs in coverage_data:
        return coverage_data[resolved_abs]
    str_path = str(module_path)
    if str_path in coverage_data:
        return coverage_data[str_path]
    # Suffix match — coverage paths may carry an alternate prefix (e.g. /private + /var symlinks).
    rel_suffix = module_path.as_posix()
    for key, info in coverage_data.items():
        if key.endswith(rel_suffix) or Path(key).as_posix().endswith(rel_suffix):
            return info
    return None


def _compute_symbol_coverage(
    start_line: int,
    end_line: int,
    measured_lines: frozenset[int],
    contexts: dict[int, list[str]],
) -> tuple[float, list[str] | None]:
    """Compute ``(coverage_pct, covered_by)`` for one symbol's line range.

    ``coverage_pct`` is the fraction of the inclusive ``[start_line, end_line]``
    range that appears in *measured_lines*, rounded to 4 decimal places. The
    denominator is clamped to ``max(1, end_line - start_line + 1)`` so 1-line
    symbols still yield a valid ratio.

    ``covered_by`` is the sorted, deduplicated list of context strings attached
    to any line in the range. It is ``None`` when contexts are empty for the
    entire range (the project was indexed without ``--cov-context``) — callers
    treat ``None`` as "context data not available" rather than "no tests cover
    this".

    Args:
        start_line: 1-based inclusive start line of the symbol.
        end_line: 1-based inclusive end line of the symbol.
        measured_lines: frozenset of line numbers reported by coverage.
        contexts: ``{lineno: [ctx, ...]}`` mapping from the coverage file.

    Examples:
        >>> _compute_symbol_coverage(1, 4, frozenset([1, 2, 3, 4]), {})
        (1.0, None)
        >>> _compute_symbol_coverage(1, 4, frozenset([1, 2]), {})
        (0.5, None)
        >>> _compute_symbol_coverage(1, 1, frozenset(), {})
        (0.0, None)
        >>> pct, ctx = _compute_symbol_coverage(1, 2, frozenset([1, 2]), {1: ["test_a"], 2: ["test_b", "test_a"]})
        >>> (pct, ctx)
        (1.0, ['test_a', 'test_b'])
    """
    total = max(1, end_line - start_line + 1)
    in_range = {ln for ln in measured_lines if start_line <= ln <= end_line}
    pct = round(len(in_range) / total, 4)
    ctx_set: set[str] = set()
    for ln in in_range:
        for ctx in contexts.get(ln, []):
            # Coverage records the empty string for the "no-context" sentinel when ``--cov-context``
            # is enabled but a line is hit outside any pytest test; drop these to avoid noise.
            if ctx:
                ctx_set.add(ctx)
    covered_by = sorted(ctx_set) if ctx_set else None
    return pct, covered_by


def _attach_coverage(modules: list[dict], root: Path, coverage_data: dict[str, dict]) -> None:
    """Annotate every ``status == "ok"`` symbol with ``coverage_pct`` and ``covered_by`` in-place.

    Symbols in modules whose file path is not present in *coverage_data* are
    left untouched — callers distinguish "no field" (coverage not measured
    for this module) from ``coverage_pct == 0.0`` (measured, no lines hit).

    Args:
        modules: module entry list (mutated in-place).
        root: project root used to resolve relative module paths.
        coverage_data: result of :func:`_read_coverage_data`.
    """
    for m in modules:
        if m.get("status") != "ok":
            continue
        rel_path = m.get("path", "")
        if not rel_path:
            continue
        module_path = root / rel_path
        info = _match_coverage_file(module_path, coverage_data)
        if info is None:
            continue
        measured_lines: frozenset[int] = info["lines"]
        contexts: dict[int, list[str]] = info["contexts"]
        for sym in m.get("symbols", []):
            start = sym.get("start_line")
            end = sym.get("end_line")
            if not isinstance(start, int) or not isinstance(end, int):
                continue
            pct, covered_by = _compute_symbol_coverage(start, end, measured_lines, contexts)
            sym["coverage_pct"] = pct
            sym["covered_by"] = covered_by


def _maybe_stamp_coverage_mtime(file_shas: dict[str, str], coverage_path: Path | None) -> None:
    """Add a synthetic ``__coverage_mtime__`` entry to *file_shas* keyed by the coverage file's mtime.

    Re-running tests overwrites ``.coverage`` without changing any Python source
    file. Without this stamp, incremental scans would skip rebuilding coverage
    annotations because ``file_shas`` would compare equal to the prior run.
    The stamp invalidates the prior index when the coverage file changes; it is
    cheap and easy to detect at staleness check time.

    No-op when *coverage_path* is None or the file is unreadable.

    Args:
        file_shas: in-progress hash dictionary (mutated in-place).
        coverage_path: optional ``.coverage`` file whose mtime to stamp.
    """
    if coverage_path is None:
        return
    try:
        mtime_ns = coverage_path.stat().st_mtime_ns
    except OSError:
        return
    file_shas["__coverage_mtime__"] = str(mtime_ns)


def _build_indexed_files(modules: list[dict]) -> dict[str, str]:
    """Build the ``rel-path -> module name`` lookup used to resolve subprocess script args.

    Args:
        modules: module entry list (only ``status == "ok"`` entries are mapped).
    """
    return {m["path"]: m["name"] for m in modules if m.get("status") == "ok" and m.get("path")}


def _attach_subprocess_calls(modules: list[dict], root: Path) -> None:
    """Walk every parseable module file and attach ``subprocess_calls`` in-place.

    The full module list must already be available so that script paths can be
    matched to dotted module names. Re-parses each source file once — the cost
    is small relative to the initial parse pass and avoids threading the AST
    through ``_parse_file``.

    Args:
        modules: module entry list (mutated in-place).
        root: project root used for script-path resolution.
    """
    indexed_files = _build_indexed_files(modules)
    for m in modules:
        if m.get("status") != "ok":
            continue
        path = m.get("path", "")
        if not path:
            continue
        filepath = root / path
        try:
            source = filepath.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(filepath))
        except (OSError, SyntaxError):
            m["subprocess_calls"] = []
            continue
        m["subprocess_calls"] = extract_subprocess_calls(tree, filepath, root, indexed_files)


def _build_subprocess_rdep_count(modules: list[dict]) -> dict[str, int]:
    """Build the reverse index ``target_module -> caller_count`` from subprocess_calls entries.

    Args:
        modules: module entry list with ``subprocess_calls`` already attached.
    """
    counts: dict[str, int] = {}
    for m in modules:
        for call in m.get("subprocess_calls", []) or []:
            target = call.get("target_module")
            if target:
                counts[target] = counts.get(target, 0) + 1
    return counts


def _apply_config_refs(root: Path, modules: list[dict]) -> None:
    """Attach config_refs from scan_config_refs to matching module entries in-place.

    Args:
        root: project root used for config file discovery.
        modules: module entry list (mutated in-place).
    """
    module_names = {m["name"] for m in modules if m.get("status") == "ok"}
    refs = scan_config_refs(root, module_names)
    for m in modules:
        name = m.get("name", "")
        if name in refs:
            m["config_refs"] = refs[name]


def _build_internal_prefix_set(indexed_names: set[str]) -> set[str]:
    """Return every dotted prefix of every indexed module name (plus sans-``src.`` variants).

    Used by ``classify_imports`` to decide whether an import string targets an
    internal project module. ``src``-layout packages indexed under ``src.pkg.x``
    must also match a bare ``import pkg`` — so for any name starting with
    ``src.``, also add the name with the ``src.`` prefix stripped.

    Args:
        indexed_names: set of dotted module names discovered during the scan.

    Examples:
        >>> sorted(_build_internal_prefix_set({"pkg.a", "src.lib.b"}))
        ['lib', 'lib.b', 'pkg', 'pkg.a', 'src', 'src.lib', 'src.lib.b']
    """
    prefixes: set[str] = set()
    for name in indexed_names:
        if not name:
            continue
        parts = name.split(".")
        for i in range(1, len(parts) + 1):
            prefixes.add(".".join(parts[:i]))
        if parts[0] == "src" and len(parts) > 1:
            sans_parts = parts[1:]
            for i in range(1, len(sans_parts) + 1):
                prefixes.add(".".join(sans_parts[:i]))
    return prefixes


def classify_imports(imports: list[str], internal_prefixes: set[str]) -> dict[str, list[str]]:
    """Classify each import string as stdlib, third-party, or internal.

    Classification rules (checked in order):

    1. **Relative** (starts with ``.``) → internal (same package).
    2. **stdlib** — top-level dotted component is in :data:`sys.stdlib_module_names`.
    3. **internal** — any dotted prefix of the import path appears in
       *internal_prefixes* (computed via :func:`_build_internal_prefix_set`).
    4. **third-party** — everything else.

    The original import strings are preserved verbatim in each group; no
    normalisation is applied.

    Args:
        imports: raw import strings as recorded by :func:`extract_imports`.
        internal_prefixes: prefix set returned by :func:`_build_internal_prefix_set`.

    Examples:
        >>> classify_imports(
        ...     ["os", "os.path", "numpy", "mypkg.core", ".local"],
        ...     {"mypkg", "mypkg.core"},
        ... )
        {'stdlib': ['os', 'os.path'], 'third_party': ['numpy'], 'internal': ['mypkg.core', '.local']}
        >>> classify_imports(["__future__"], set())
        {'stdlib': ['__future__'], 'third_party': [], 'internal': []}
    """
    stdlib: list[str] = []
    third_party: list[str] = []
    internal: list[str] = []
    for imp in imports:
        if imp.startswith("."):
            internal.append(imp)
            continue
        top = imp.split(".", 1)[0]
        if top in _STDLIB_MODULES:
            stdlib.append(imp)
            continue
        parts = imp.split(".")
        matched_internal = False
        for i in range(1, len(parts) + 1):
            if ".".join(parts[:i]) in internal_prefixes:
                matched_internal = True
                break
        if matched_internal:
            internal.append(imp)
        else:
            third_party.append(imp)
    return {"stdlib": stdlib, "third_party": third_party, "internal": internal}


def _canonicalize_symbol_aliases(modules: list[dict]) -> dict[str, str]:
    """Canonicalize statically proven symbol aliases for reverse-call queries.

    The scanner preserves direct top-level import provenance per module. This
    cross-module pass is intentionally strict: a terminal target must be either a
    known project symbol or an external module, and alias cycles or imports of an
    indexed module as though it were a symbol are discarded. This prevents
    package re-export spelling from splitting a reverse-call graph without using
    fuzzy suffix matching.

    Args:
        modules: parsed module entries carrying direct alias provenance.

    Returns:
        Persistable ``alias_qname -> canonical_qname`` mappings.
    """
    indexed_modules = {m["name"] for m in modules if m.get("status") == "ok"}
    symbols = {
        f"{module['name']}::{symbol['qualified_name']}"
        for module in modules
        if module.get("status") == "ok"
        for symbol in module.get("symbols", [])
    }
    direct: dict[str, str] = {}
    for module in modules:
        if module.get("status") != "ok":
            continue
        for local_name, target in (module.get("symbol_aliases") or {}).items():
            if isinstance(local_name, str) and isinstance(target, str) and "::" in target:
                direct[f"{module['name']}::{local_name}"] = target

    resolved: dict[str, str] = {}
    invalid: set[str] = set()

    def _resolve(alias: str, trail: set[str]) -> str | None:
        if alias in resolved:
            return resolved[alias]
        if alias in invalid or alias in trail:
            invalid.update(trail)
            return None
        target = direct.get(alias)
        if target is None:
            return alias
        terminal = _resolve(target, {*trail, alias}) if target in direct else target
        if terminal is None:
            invalid.add(alias)
            return None
        target_module, separator, _symbol = terminal.partition("::")
        if not separator or (terminal not in symbols and target_module in indexed_modules):
            invalid.add(alias)
            return None
        resolved[alias] = terminal
        return terminal

    for alias in sorted(direct):
        _resolve(alias, set())

    return dict(sorted(resolved.items()))


def _resolve_import_submodule_edges(modules: list[dict]) -> None:
    """Add statically proven ``from package import submodule`` edges in-place.

    The parser keeps every ``package.name`` candidate because a ``from`` import may bind either a module or an
    attribute. Only candidates matching an indexed module become import edges; this retains parent-package imports
    without inventing reverse module edges for imported symbols.
    """
    indexed_names = {module["name"] for module in modules if module.get("status") == "ok"}
    for module in modules:
        candidates = module.get("from_import_submodules", [])
        direct = set(module.get("unresolved_direct_imports", module.get("direct_imports", [])))
        direct.update(candidate for candidate in candidates if candidate in indexed_names)
        module["direct_imports"] = sorted(direct)


def _collect_symbol_alias_limitations(modules: list[dict]) -> list[dict[str, str]]:
    """Return sorted target-specific evidence for rejected alias paths.

    These records do not alter call edges. They preserve why a scanner deliberately omitted an otherwise recognizable
    alias so reverse-call queries can decline to claim completeness for that exact canonical target.
    """
    records: set[tuple[str, str, str]] = set()
    for module in modules:
        if module.get("status") != "ok":
            continue
        for record in module.get("symbol_alias_limitations", []):
            if not isinstance(record, dict):
                continue
            alias_qname = record.get("alias_qname")
            target_qname = record.get("target_qname")
            reason = record.get("reason")
            if all(isinstance(value, str) and "::" in value for value in (alias_qname, target_qname)) and isinstance(
                reason, str
            ):
                records.add((alias_qname, target_qname, reason))
    return [
        {"alias_qname": alias_qname, "target_qname": target_qname, "reason": reason}
        for alias_qname, target_qname, reason in sorted(records)
    ]


def _recompute_metrics(
    modules: list[dict],
    module_aliases: dict[str, str] | None = None,
    symbol_aliases: dict[str, str] | None = None,
) -> None:
    """Recompute rdep_count and rcall_count in-place from the current module list.

    Also computes the v4.1 reverse indexes (``mock_rdep_count`` per module and
    per symbol, plus per-symbol ``fn_rdep_test_count``). When *module_aliases*
    is provided (v5.1+), bare import names that match an alias key are
    attributed to the full dotted target during rdep counting and import
    classification.

    Args:
        modules: list of module entry dicts (mutated in-place).
        module_aliases: optional ``bare_name -> full_dotted_name`` map from
            ``_collect_module_aliases`` (conftest.py ``sys.path`` shims).
        symbol_aliases: optional static ``alias_qname -> canonical_qname`` map.
    """
    aliases = module_aliases or {}
    canonical_symbols = symbol_aliases or {}
    rdep_counts: dict[str, int] = {}
    rcall_counts: dict[str, int] = {}
    # fn_rdep_test_count: per fully-qualified target ("module::symbol") -> count of test callers.
    fn_test_caller_counts: dict[str, int] = {}
    # mock_rdep_count: per fully-qualified target -> count of distinct test files mocking it.
    mock_per_symbol: dict[str, set[str]] = {}
    for m in modules:
        for imp in m.get("direct_imports", []):
            resolved = aliases.get(imp, imp)
            rdep_counts[resolved] = rdep_counts.get(resolved, 0) + 1
        caller_is_test = bool(m.get("is_test"))
        for sym in m.get("symbols", []):
            for edge in sym.get("calls", []):
                if edge["resolution"] != "import":
                    continue
                target = canonical_symbols.get(edge["target"], edge["target"])
                target_module = target.split("::")[0]
                rcall_counts[target_module] = rcall_counts.get(target_module, 0) + 1
                if caller_is_test:
                    fn_test_caller_counts[target] = fn_test_caller_counts.get(target, 0) + 1
        # mock_patches: only present on test modules; map each unique (target, file) once.
        for entry in m.get("mock_patches", []) or []:
            target = entry.get("target")
            file_ = entry.get("file")
            if not target or not file_:
                continue
            mock_per_symbol.setdefault(target, set()).add(file_)
    for m in modules:
        if m.get("status") == "ok":
            m["rdep_count"] = rdep_counts.get(m["name"], 0)
            m["rcall_count"] = rcall_counts.get(m["name"], 0)
            m["dep_count"] = len(m.get("direct_imports", []))
            # Per-module mock_rdep_count: total distinct test files mocking any symbol in this module.
            mod_files: set[str] = set()
            mod_prefix = f"{m['name']}::"
            for target, files in mock_per_symbol.items():
                if target.startswith(mod_prefix):
                    mod_files.update(files)
            m["mock_rdep_count"] = len(mod_files)
            # Per-symbol: stamp fn_rdep_test_count and mock_rdep_count on each symbol dict.
            for sym in m.get("symbols", []):
                full_qname = f"{m['name']}::{sym['qualified_name']}"
                sym["fn_rdep_test_count"] = fn_test_caller_counts.get(full_qname, 0)
                sym["mock_rdep_count"] = len(mock_per_symbol.get(full_qname, set()))
    # Phase 1.5: classify each module's direct imports into stdlib/third_party/internal (v4.3).
    # v5.1: bare aliases from conftest.py sys.path shims also count as internal prefixes.
    indexed_names = {m["name"] for m in modules if m.get("status") == "ok"}
    internal_prefixes = _build_internal_prefix_set(indexed_names) | set(aliases.keys())
    for m in modules:
        if m.get("status") != "ok":
            continue
        m["import_groups"] = classify_imports(m.get("direct_imports", []), internal_prefixes)

    # Phase 2: dynamic_imported_by — cross-reference dynamic_imports literals against known modules.
    all_module_names = {m["name"] for m in modules if m.get("status") == "ok"}
    dynamic_rdeps: dict[str, list[dict]] = {}
    for m in modules:
        caller_name = m.get("name", "")
        caller_path = m.get("path", "")
        for entry in m.get("dynamic_imports", []):
            literal: str = entry["literal"]
            # Match exact module name; avoid matching sub-paths of a shorter module name twice.
            if literal in all_module_names:
                dynamic_rdeps.setdefault(literal, []).append(
                    {"importer": caller_name, "path": caller_path, "line": entry["line"], "literal": literal}
                )
            else:
                # literal may be a sub-path like "pkg.sub.mod" — attribute to the longest prefix match.
                matched = next(
                    (n for n in sorted(all_module_names, key=len, reverse=True) if literal.startswith(n + ".")), None
                )
                if matched:
                    dynamic_rdeps.setdefault(matched, []).append(
                        {"importer": caller_name, "path": caller_path, "line": entry["line"], "literal": literal}
                    )
    for m in modules:
        name = m.get("name", "")
        if name in dynamic_rdeps:
            m["dynamic_imported_by"] = dynamic_rdeps[name]


def _collect_doc_xrefs(root: Path) -> list[dict]:
    """Scan ``.rst`` and ``docs/**/*.md`` files for cross-references.

    Wraps :func:`scan_rst_xrefs` and :func:`scan_mkdocs_xrefs` over every doc
    file found by :func:`_iter_doc_files`. Used as a top-level pool of refs that
    have no owning Python module entry.

    Args:
        root: project root used to discover doc files and compute relative paths.

    Returns:
        Flat list of xref dicts from all ``.rst`` and ``.md`` files.
    """
    rst_files, md_files = _iter_doc_files(root)
    refs: list[dict] = []
    for rst in rst_files:
        refs.extend(scan_rst_xrefs(rst, root))
    for md in md_files:
        refs.extend(scan_mkdocs_xrefs(md, root))
    return refs


def _build_sphinx_xref_count(modules: list[dict], doc_xrefs: list[dict]) -> dict[str, int]:
    """Compute the reverse-index ``target -> total reference count`` for v4.5.

    Aggregates every xref across module docstrings and external doc files into
    a single counter. Stored as a top-level key on the index so :func:`cmd_xrefs`
    can answer "is this symbol referenced anywhere in the docs?" in O(1).

    Args:
        modules: module entry list (each may carry ``sphinx_xrefs``).
        doc_xrefs: top-level xref list from ``.rst`` / ``.md`` files.

    Returns:
        Mapping of canonical symbol key → total reference count.
    """
    counts: dict[str, int] = {}
    for m in modules:
        for entry in m.get("sphinx_xrefs", []) or []:
            target = entry.get("target")
            if not target:
                continue
            counts[target] = counts.get(target, 0) + 1
    for entry in doc_xrefs:
        target = entry.get("target")
        if not target:
            continue
        counts[target] = counts.get(target, 0) + 1
    return counts


def _src_root_rels(src_root_rel: str | tuple[str, ...]) -> tuple[str, ...]:
    """Normalise a single source-root rel or a priority tuple into a non-empty tuple.

    Accepts the legacy single-string form (``""`` when src root == project root) or a
    tuple of source-root rels in priority order (monorepo ``src_roots``). Empty strings
    are dropped — they denote "src root is the project root", which every path is
    trivially under, so they carry no ranking signal.

    Args:
        src_root_rel: source root rel string, or tuple of rels in priority order.

    Returns:
        Tuple of non-empty source-root rels in priority order (possibly empty).

    Examples:
        >>> _src_root_rels("src")
        ('src',)
        >>> _src_root_rels("")
        ()
        >>> _src_root_rels(("libs/core/src", "services/api/src", ""))
        ('libs/core/src', 'services/api/src')
    """
    rels = (src_root_rel,) if isinstance(src_root_rel, str) else tuple(src_root_rel)
    return tuple(r for r in rels if r)


def _under_root_rank(path: str, root_rels: tuple[str, ...]) -> int:
    """Return the priority rank of the first source root *path* lies under.

    Rank ``0`` is the highest-priority (first-listed) root; a path under no configured
    root gets ``len(root_rels)`` so it always sorts after any in-root path. With a single
    root this reproduces the original ``0 (under) vs 1 (outside)`` ranking exactly.

    Args:
        path: module path relative to project root, posix separators.
        root_rels: source-root rels in priority order (from :func:`_src_root_rels`).

    Returns:
        Index of the matching root (0-based priority), or ``len(root_rels)`` if none.

    Examples:
        >>> _under_root_rank("src/pkg/mod.py", ("src",))
        0
        >>> _under_root_rank("copy/pkg/mod.py", ("src",))
        1
        >>> _under_root_rank("services/api/src/pkg/m.py", ("libs/core/src", "services/api/src"))
        1
    """
    for rank, rel in enumerate(root_rels):
        if path == rel or path.startswith(rel + "/"):
            return rank
    return len(root_rels)


def _dedup_key(path: str, src_root_rel: str | tuple[str, ...]) -> tuple[int, int, str]:
    """Sort key selecting the canonical winner among modules sharing a dotted name.

    Prefers, in order: a path under a configured source root (earlier-listed roots win
    over later ones), then the shortest path (by component count), then lexicographic
    order. Fully deterministic regardless of filesystem walk order.

    Args:
        path: module path relative to project root, posix separators.
        src_root_rel: source root rel (``""`` when src root == root) or, for monorepos
            with multiple ``src_roots``, a tuple of rels in priority order.

    Returns:
        Tuple ranking this path — lower sorts first (= preferred winner).

    Examples:
        >>> _dedup_key("src/pkg/mod.py", "src")
        (0, 3, 'src/pkg/mod.py')
        >>> _dedup_key("copy/pkg/mod.py", "src")
        (1, 3, 'copy/pkg/mod.py')
        >>> _dedup_key("services/api/src/pkg/m.py", ("libs/core/src", "services/api/src"))
        (1, 5, 'services/api/src/pkg/m.py')
    """
    root_rels = _src_root_rels(src_root_rel)
    return (_under_root_rank(path, root_rels), len(path.split("/")), path)


def _dedup_modules(modules: list[dict], src_root_rel: str | tuple[str, ...]) -> tuple[list[dict], list[dict]]:
    """Drop duplicate dotted names deterministically, keeping the canonical path.

    Args:
        modules: parsed module entries (each with ``name`` and ``path``).
        src_root_rel: source root rel used by :func:`_dedup_key` — a single rel string,
            or a priority-ordered tuple of rels for monorepos with multiple ``src_roots``.

    Returns:
        Tuple ``(kept_modules, collisions)`` where ``collisions`` is a list of
        ``{"name", "kept", "dropped"}`` records (empty when no name repeated).
    """
    by_name: dict[str, list[dict]] = {}
    for m in modules:
        by_name.setdefault(m.get("name", ""), []).append(m)

    kept: list[dict] = []
    collisions: list[dict] = []
    for name in sorted(by_name):
        entries = by_name[name]
        if len(entries) == 1:
            kept.append(entries[0])
            continue
        entries.sort(key=lambda m: _dedup_key(m.get("path", ""), src_root_rel))
        winner, *losers = entries
        kept.append(winner)
        dropped = [m.get("path", "") for m in losers]
        collisions.append({"name": name, "kept": winner.get("path", ""), "dropped": dropped})
        print(
            f"[codemap] ⚠ name collision: '{name}' at {[winner.get('path', '')] + dropped} "
            f"— kept '{winner.get('path', '')}'",
            file=sys.stderr,
        )
    return kept, collisions


def _stub_slot(path: str) -> tuple[str, str]:
    """Split a module rel-path into its sibling-slot key and Python suffix.

    The slot key is the path without its ``.py``/``.pyi`` suffix, so an implementation and
    its stub share one slot: ``pkg/mod.py`` and ``pkg/mod.pyi`` both key ``pkg/mod``, and
    ``pkg/__init__.py`` and ``pkg/__init__.pyi`` both key ``pkg/__init__``.

    Examples:
        >>> _stub_slot("pkg/mod.pyi")
        ('pkg/mod', '.pyi')
        >>> _stub_slot("pkg/__init__.py")
        ('pkg/__init__', '.py')
    """
    if path.endswith(".pyi"):
        return path[:-4], ".pyi"
    if path.endswith(".py"):
        return path[:-3], ".py"
    return path, ""


def _resolve_stub_shadowing(modules: list[dict]) -> tuple[list[dict], list[str], list[dict]]:
    """Apply ``.py``/``.pyi`` sibling precedence before name dedup.

    - Case-fold collision: two distinct paths equal under ``str.casefold`` cannot be
      resolved by walk/sort order, so both entries are dropped and recorded — identically
      on every OS, never selecting by directory order (fail closed).
    - A sibling ``.py`` is authoritative: its ``.pyi`` is removed from ``modules`` and
      recorded in ``shadowed_stubs``; the surviving ``.py`` entry gains ``has_stub=True``.
    - A ``.pyi`` with no ``.py`` sibling is kept once with ``stub_only=True``.

    Args:
        modules: parsed module entries (each with a ``path`` and ``name``).

    Returns:
        ``(kept_modules, shadowed_stubs, casefold_collisions)``. ``kept_modules`` keeps the
        input order (name dedup re-sorts afterward); ``shadowed_stubs`` is sorted; each
        ``casefold_collisions`` record is ``{"paths": [...], "reason": "case_fold_collision"}``.
    """
    by_casefold: dict[str, list[dict]] = {}
    for m in modules:
        by_casefold.setdefault(m.get("path", "").casefold(), []).append(m)
    casefold_collisions: list[dict] = []
    survivors: list[dict] = []
    for group in by_casefold.values():
        distinct = sorted({m.get("path", "") for m in group})
        if len(distinct) > 1:
            casefold_collisions.append({"paths": distinct, "reason": "case_fold_collision"})
            print(
                f"[codemap] ⚠ case-fold collision: {distinct} — dropped (no directory-order winner)",
                file=sys.stderr,
            )
            continue
        survivors.extend(group)

    slots: dict[str, dict[str, dict]] = {}
    for m in survivors:
        slot, suffix = _stub_slot(m.get("path", ""))
        slots.setdefault(slot, {})[suffix] = m

    kept: list[dict] = []
    shadowed_stubs: list[str] = []
    for m in survivors:
        path = m.get("path", "")
        slot, suffix = _stub_slot(path)
        slot_entries = slots[slot]
        # Flags are recomputed from current siblings every run — never only set — so an
        # incremental scan clears a stale has_stub/stub_only after a sibling is deleted.
        if suffix == ".pyi":
            if ".py" in slot_entries:
                shadowed_stubs.append(path)
                continue
            m["stub_only"] = True
            m.pop("has_stub", None)
        else:
            m.pop("stub_only", None)
            if suffix == ".py" and ".pyi" in slot_entries:
                m["has_stub"] = True
            else:
                m.pop("has_stub", None)
        kept.append(m)
    return kept, sorted(shadowed_stubs), casefold_collisions


def _build_excluded_roots(exclusions: Exclusions, counts: dict[str, int]) -> list[dict]:
    """Assemble the ``excluded_roots`` meta list from loaded exclusions and hit counts.

    Every user-configured entry appears (even at ``count == 0``) so consumers can see
    what was requested; built-in ``SKIP_DIRS`` are omitted — they are implicit and
    constant. Entries are sorted by descending count then name for stable output.

    Args:
        exclusions: loaded user exclusions (carries source provenance).
        counts: per-entry ``.py`` files removed, keyed by the raw pattern/dir-name.

    Returns:
        List of ``{"pattern", "kind", "source", "count"}`` records.
    """
    roots = []
    for entry, source in exclusions.sources.items():
        kind = "glob" if _GLOB_META_RE.search(entry) else "dir"
        roots.append({"pattern": entry, "kind": kind, "source": source, "count": counts.get(entry, 0)})
    roots.sort(key=lambda r: (-r["count"], r["pattern"]))
    return roots


@dataclass(frozen=True)
class _SrcRootContext:
    """Resolved source-root context shared by full and incremental scans.

    Bundles the effective source root(s) so both :func:`scan` and :func:`incremental_scan` derive module names and rank
    collisions identically. ``configured`` is the explicit monorepo ``[tool.codemap] src_roots`` list (empty when
    unconfigured); ``default`` is the single-root ``detect_src_root`` fallback used for any file not under a configured
    root — and the only root when none are configured.
    """

    configured: tuple[Path, ...]
    default: Path

    def name_root_for(self, filepath: Path) -> Path:
        """Return the source root *filepath*'s dotted name is computed relative to."""
        return _effective_src_root(filepath, self.configured, self.default)

    def dedup_rels(self, root: Path) -> tuple[str, ...]:
        """Return source-root rels (posix, relative to *root*) in dedup-priority order.

        Configured roots come first, in declaration order; the default root is appended last so an in-``src`` path still
        beats a stray copy when no explicit roots match. The project-root sentinel (``""``) is dropped by
        :func:`_src_root_rels`.
        """
        ordered = [*self.configured, self.default]
        rels: list[str] = []
        seen: set[str] = set()
        for candidate in ordered:
            rel = "" if candidate == root else candidate.relative_to(root).as_posix()
            if rel and rel not in seen:
                seen.add(rel)
                rels.append(rel)
        return tuple(rels)

    def meta(self, root: Path) -> list[str]:
        """Return the effective source roots as posix rels for the index meta.

        Configured roots first (declaration order), then the default root when it is a proper subdirectory of *root* —
        so single-``src`` layouts still record ``["src"]``. The project root itself is omitted (represented by an empty
        list).
        """
        return list(self.dedup_rels(root))


def _resolve_src_roots(root: Path) -> _SrcRootContext:
    """Build the :class:`_SrcRootContext` for *root* from config and heuristics.

    Reads explicit ``[tool.codemap] src_roots`` (monorepo multi-root layout) and always
    computes the single-root ``detect_src_root`` fallback. When no roots are configured
    the context collapses to that single root, so behaviour is byte-identical to the
    prior single-root path.

    Args:
        root: project root to resolve source roots for.
    """
    return _SrcRootContext(configured=tuple(load_src_roots(root)), default=detect_src_root(root))


def scan(root: Path, coverage_path: Path | None = None) -> dict:
    """Run a full scan of all .py files under root and return the index dict.

    Args:
        root: project root to scan.
        coverage_path: optional path to a ``.coverage`` SQLite file. When
            provided and loadable, per-symbol ``coverage_pct`` / ``covered_by``
            fields are attached (v5.4).
    """
    src_ctx = _resolve_src_roots(root)
    exclusions = _load_exclusions(root)

    filepaths, excluded_counts = _iter_python_files(root, exclusions)
    filepaths = sorted(filepaths, key=lambda p: p.relative_to(root).as_posix())
    if excluded_counts:
        total = sum(excluded_counts.values())
        print(
            f"[codemap] excluded {total} .py file(s) via {len(excluded_counts)} pattern(s): "
            f"{', '.join(sorted(excluded_counts))}",
            file=sys.stderr,
        )
    parse_args = [(fp, root, src_ctx.name_root_for(fp)) for fp in filepaths]
    if sys.platform == "win32":
        # spawn (Windows' only start method) would re-import this extension-less __main__
        # script to unpickle the worker, with no freeze_support guard. Parse serially —
        # functional parity with the Unix fork path, only slower.
        modules = [_parse_file_star(a) for a in parse_args]
    else:
        # fork keeps parallelism and avoids the __main__ re-import entirely.
        try:
            _mp_ctx = multiprocessing.get_context("fork")
        except ValueError:
            print(
                "[codemap] fork process context unavailable; parsing serially",
                file=sys.stderr,
            )
            modules = [_parse_file_star(a) for a in parse_args]
        else:
            try:
                pool = ProcessPoolExecutor(max_workers=os.cpu_count(), mp_context=_mp_ctx)
            except PermissionError as exc:
                if exc.errno != errno.EPERM:
                    raise
                print(
                    "[codemap] process pool unavailable (EPERM); parsing serially",
                    file=sys.stderr,
                )
                modules = [_parse_file_star(a) for a in parse_args]
            else:
                with pool:
                    modules = list(pool.map(_parse_file_star, parse_args))

    src_root_rels = src_ctx.dedup_rels(root)
    # .pyi precedence resolves before name dedup: shadowed stubs are removed
    # so they never reach name-collision dedup; lone stubs stay as stub_only modules.
    modules, shadowed_stubs, casefold_collisions = _resolve_stub_shadowing(modules)
    modules, collisions = _dedup_modules(modules, src_root_rels)
    excluded_roots = _build_excluded_roots(exclusions, excluded_counts)

    # v5.1: collect conftest.py sys.path aliases before metric recompute so
    # rdep_count and import_groups attribute bare imports to their full names.
    conftest_paths = [fp for fp in filepaths if fp.name == "conftest.py"]
    indexed_names = {m["name"] for m in modules if m.get("status") == "ok"}
    module_aliases = _collect_module_aliases(conftest_paths, indexed_names, root)

    _resolve_import_submodule_edges(modules)
    symbol_aliases = _canonicalize_symbol_aliases(modules)
    symbol_alias_limitations = _collect_symbol_alias_limitations(modules)
    _recompute_metrics(modules, module_aliases, symbol_aliases)
    # Phase 3: config-file refs — scan pyproject.toml, setup.cfg, *.yml, *.yaml.
    _apply_config_refs(root, modules)
    # Phase 4 (v4.5): collect xrefs from .rst and docs/**/*.md and build reverse index.
    doc_xrefs = _collect_doc_xrefs(root)
    sphinx_xref_count = _build_sphinx_xref_count(modules, doc_xrefs)
    # Phase 5 (v5.2): subprocess call edges — needs complete module list to resolve script paths.
    _attach_subprocess_calls(modules, root)
    subprocess_rdep_count = _build_subprocess_rdep_count(modules)
    # Phase 6 (v5.3): pytest fixture dependency graph — needs complete module list for
    # conftest aggregation; runs after metrics so is_test stays consistent.
    _attach_fixture_graph(modules, root)
    fixture_rdep_count = _build_fixture_rdep_count(modules)
    # Phase 7 (v5.4): optional coverage integration. Failures inside _read_coverage_data
    # produce a stderr warning and a None return — the rest of the index still builds.
    if coverage_path is not None:
        coverage_data = _read_coverage_data(coverage_path)
        if coverage_data is not None:
            _attach_coverage(modules, root, coverage_data)

    file_shas = get_file_hashes(root, exclusions)
    _maybe_stamp_coverage_mtime(file_shas, coverage_path)

    index = {
        "scan_version": SCAN_VERSION,
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "git_sha": get_git_sha(root),
        "project": root.name,
        "scan_root": str(root.resolve()),
        "src_layout": bool(src_root_rels),
        "src_roots": src_ctx.meta(root),
        "file_shas": file_shas,
        "modules": modules,
        "doc_xrefs": doc_xrefs,
        "sphinx_xref_count": sphinx_xref_count,
        "module_aliases": module_aliases,
        "symbol_aliases": symbol_aliases,
        "symbol_alias_limitations": symbol_alias_limitations,
        "subprocess_rdep_count": subprocess_rdep_count,
        "fixture_rdep_count": fixture_rdep_count,
        "excluded_roots": excluded_roots,
        "collisions": collisions,
    }
    _apply_stub_keys(index, shadowed_stubs, casefold_collisions)
    return index


def _apply_stub_keys(index: dict, shadowed_stubs: list[str], casefold_collisions: list[dict]) -> None:
    """Attach ``shadowed_stubs``/``casefold_collisions`` root keys only when non-empty.

    Omitting empty values mirrors the schema's "older indexes omit them" philosophy and keeps a tree without stubs byte
    identical to an index created before ``.pyi`` support. On an incremental re-scan a key that became empty is removed
    so a stale value never lingers under ``{**old_index, ...}``.
    """
    for key, value in (("shadowed_stubs", shadowed_stubs), ("casefold_collisions", casefold_collisions)):
        if value:
            index[key] = value
        else:
            index.pop(key, None)


def incremental_scan(root: Path, old_index: dict, coverage_path: Path | None = None) -> dict:
    """Re-parse only files whose hash changed since the last scan.

    Args:
        root: project root path.
        old_index: previously written index dict loaded from disk.
        coverage_path: optional path to a ``.coverage`` SQLite file. When
            provided, the coverage stamp is compared so re-running tests alone
            forces re-annotation even when no source file changed (v5.4).
    """
    src_ctx = _resolve_src_roots(root)
    exclusions = _load_exclusions(root)
    current_shas = get_file_hashes(root, exclusions)
    _maybe_stamp_coverage_mtime(current_shas, coverage_path)
    stored_shas: dict[str, str] = old_index.get("file_shas", {})

    # Coverage stamp differences live alongside file hashes; treat them like any other key for invalidation.
    changed = {p for p in current_shas if stored_shas.get(p) != current_shas[p]}
    deleted = {p for p in stored_shas if p not in current_shas}
    # The synthetic coverage stamp is not a source file; do not feed it into the parse loop below.
    changed.discard("__coverage_mtime__")
    deleted.discard("__coverage_mtime__")

    stored_modules = old_index.get("modules", [])
    legacy_non_python = {
        module.get("path")
        for module in stored_modules
        if isinstance(module, dict)
        and isinstance(module.get("path"), str)
        and not module["path"].endswith((".py", ".pyi"))
    }
    if not changed and not deleted and not legacy_non_python:
        print("[codemap] Index already up to date.", file=sys.stderr)
        return old_index

    # Freshness tracks documentation too, but only Python sources belong in the module graph.
    # Filtering the carried index also repairs indexes produced by the former parse-all path.
    modules_by_path: dict[str, dict] = {
        module["path"]: module
        for module in stored_modules
        if isinstance(module, dict) and isinstance(module.get("path"), str) and module["path"].endswith((".py", ".pyi"))
    }
    for path in sorted(legacy_non_python):
        print(f"[codemap]   - removed non-Python module {path}", file=sys.stderr)

    for path in sorted(deleted):
        modules_by_path.pop(path, None)
        print(f"[codemap]   - removed {path}", file=sys.stderr)

    for rel_path_str in sorted(changed):
        if not rel_path_str.endswith((".py", ".pyi")):
            print(f"[codemap]   refreshed {rel_path_str}", file=sys.stderr)
            continue
        filepath = root / rel_path_str
        if not filepath.exists():
            modules_by_path.pop(rel_path_str, None)
            continue
        entry = _parse_file(filepath, root, src_ctx.name_root_for(filepath))
        action = "updated" if rel_path_str in stored_shas else "added"
        modules_by_path[entry["path"]] = entry
        print(f"[codemap]   {action} {rel_path_str}", file=sys.stderr)

    src_root_rels = src_ctx.dedup_rels(root)
    modules, shadowed_stubs, casefold_collisions = _resolve_stub_shadowing(list(modules_by_path.values()))
    modules, collisions = _dedup_modules(modules, src_root_rels)
    excluded_roots = _build_excluded_roots(exclusions, {})
    # v5.1: re-collect conftest.py aliases from disk (cheap full sweep — every
    # conftest can affect rdep_count, so per-file invalidation is unsafe).
    conftest_paths = [root / m["path"] for m in modules if m.get("path", "").endswith("conftest.py")]
    indexed_names = {m["name"] for m in modules if m.get("status") == "ok"}
    module_aliases = _collect_module_aliases(conftest_paths, indexed_names, root)

    _resolve_import_submodule_edges(modules)
    symbol_aliases = _canonicalize_symbol_aliases(modules)
    symbol_alias_limitations = _collect_symbol_alias_limitations(modules)
    _recompute_metrics(modules, module_aliases, symbol_aliases)
    _apply_config_refs(root, modules)
    # v4.5: rebuild doc_xrefs / sphinx_xref_count — cheap full re-scan beats trying
    # to invalidate per-file (RST/MD files have no module entry to update).
    doc_xrefs = _collect_doc_xrefs(root)
    sphinx_xref_count = _build_sphinx_xref_count(modules, doc_xrefs)
    # v5.2: rebuild subprocess edges over the merged module set — caller resolution
    # depends on the complete module list, so per-file invalidation is unsafe.
    _attach_subprocess_calls(modules, root)
    subprocess_rdep_count = _build_subprocess_rdep_count(modules)
    # v5.3: rebuild fixture graph over the merged module set — conftest visibility
    # is project-wide so per-file invalidation cannot preserve the override hierarchy.
    _attach_fixture_graph(modules, root)
    fixture_rdep_count = _build_fixture_rdep_count(modules)
    # v5.4: re-annotate coverage if a coverage file is still in scope. Skip silently
    # otherwise so prior coverage annotations carried over via {**old_index, ...} survive.
    if coverage_path is not None:
        coverage_data = _read_coverage_data(coverage_path)
        if coverage_data is not None:
            _attach_coverage(modules, root, coverage_data)

    index = {
        **old_index,
        "scan_version": SCAN_VERSION,
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "git_sha": get_git_sha(root),
        "project": root.name,
        "scan_root": str(root.resolve()),
        "src_layout": bool(src_root_rels),
        "src_roots": src_ctx.meta(root),
        "file_shas": current_shas,
        "modules": modules,
        "doc_xrefs": doc_xrefs,
        "sphinx_xref_count": sphinx_xref_count,
        "module_aliases": module_aliases,
        "symbol_aliases": symbol_aliases,
        "symbol_alias_limitations": symbol_alias_limitations,
        "subprocess_rdep_count": subprocess_rdep_count,
        "fixture_rdep_count": fixture_rdep_count,
        "excluded_roots": excluded_roots,
        "collisions": collisions,
    }
    _apply_stub_keys(index, shadowed_stubs, casefold_collisions)
    return index


def _build_index(args: argparse.Namespace, root: Path, out_path: Path) -> dict:
    """Dispatch to incremental or full scan based on args and existing index state.

    Args:
        args: parsed CLI arguments (uses ``args.incremental`` and
            ``args.with_coverage``).
        root: project root path.
        out_path: path to the existing index file (may not exist yet).
    """
    coverage_path = Path(args.with_coverage) if args.with_coverage else None
    if args.incremental and out_path.exists():
        with out_path.open() as f:
            old_index = json.load(f)
        if int(old_index.get("scan_version", 0)) >= SCAN_VERSION and old_index.get("file_shas"):
            print(f"[codemap] Incremental scan {root} ...", file=sys.stderr)
            return incremental_scan(root, old_index, coverage_path=coverage_path)
        print("[codemap] pre-v13 index found — falling back to full scan ...", file=sys.stderr)
        return scan(root, coverage_path=coverage_path)
    if args.incremental:
        print("[codemap] No existing index found — running full scan ...", file=sys.stderr)
    print(f"[codemap] Scanning {root} ...", file=sys.stderr)
    return scan(root, coverage_path=coverage_path)


def _die_gate(code: str, detail: str) -> None:
    """Write one bounded structured gate error to stderr and exit 1.

    The capability contract's exit-1 row requires a bounded structured error and
    forbids a traceback, so every way the RW gate can refuse a build funnels through
    here into the same ``{"error", "detail"}`` shape the CLI dispatcher already emits.

    Args:
        code: stable machine-readable slug (e.g. ``index_busy``).
        detail: human-readable one-line cause.
    """
    sys.stderr.write(json.dumps({"error": code, "detail": detail}) + "\n")
    sys.exit(1)


def _gate_timeout_kwargs() -> dict[str, float]:
    """Return the optional bounded gate timeout.

    Absent or non-positive leaves the gate's own default bound; a positive value lets callers (and tests) shorten the
    wait before ``index_busy``. Mirrors the query engine's reader-side handling of the same variable, so one setting
    bounds both halves of the gate.
    """
    raw = os.environ.get("CODEMAP_GATE_TIMEOUT", "").strip()
    try:
        value = float(raw)
    except ValueError:
        return {}
    return {"timeout": value} if value > 0 else {}


def _build_and_publish(args: argparse.Namespace, root: Path, out_path: Path) -> dict:
    """Scan *root* and publish the index to *out_path*; the writer's exclusive phase.

    Runs as ``build_fn`` inside :func:`codemap_py.rwgate.write_index`, so it is the
    only writer touching *out_path* for its duration. Serialisation streams straight
    into the gate's temp file rather than being materialised as one payload — a real
    index reaches hundreds of megabytes, and buffering it whole would multiply peak
    memory during the write for no benefit.

    Args:
        args: parsed scan-index arguments (``incremental``, ``with_coverage``).
        root: project root to scan.
        out_path: index path to publish.

    Returns:
        The freshly built index dict (the caller reports its module counts).
    """
    index = _build_index(args, root, out_path)
    with rwgate.publish_stream(out_path) as fh:
        json.dump(index, fh, separators=(",", ":"))
    return index


def _refresh_result(args: argparse.Namespace) -> dict[str, str | int | bool | None]:
    """Return bounded refresh provenance for the successful index telemetry record.

    Background and self-heal callers set only facts they observed before spawning this process. A raw CLI invocation has
    no such probe, so its stale state and changed count remain ``None`` rather than being inferred after the fact.
    """
    changed_raw = os.environ.get("CODEMAP_REFRESH_CHANGED_COUNT", "")
    try:
        parsed_changed_count = int(changed_raw)
    except ValueError:
        changed_count = None
    else:
        changed_count = parsed_changed_count if parsed_changed_count >= 0 else None
    stale_raw = os.environ.get("CODEMAP_REFRESH_STALE_BEFORE", "").lower()
    stale_before: bool | None = {"true": True, "false": False}.get(stale_raw)
    trigger = os.environ.get("CODEMAP_REFRESH_TRIGGER") or "direct_cli"
    return {
        "trigger": trigger if trigger in _REFRESH_TRIGGERS else "direct_cli",
        "changed_count": changed_count,
        "incremental": bool(args.incremental),
        "stale_before": stale_before,
        "result_currency": "current",
    }


def _resolve_out_path(root: Path) -> Path:
    """Return the index path to publish for *root*.

    Under ``CODEMAP_INDEX_DIR`` the path comes from
    :func:`codemap_py.index_paths.resolve_index`, the single resolver the reader,
    the RW gate, and ``codemap-py doctor`` also use — so all four derive one
    identical path from one function. Previously this function derived the
    override path itself (``<override>/<root.name>.json``) while the resolver
    root-keyed it, and reader and writer each normalized the root differently;
    an alias of the same project could therefore resolve to two different
    ``<root_name>.json`` files under one override directory.

    The default (no-override) layout keeps its root-relative derivation
    unchanged: it is already root-scoped, and the reader's git-root strategy
    matches it verbatim.

    Args:
        root: the project root being scanned.

    Returns:
        Absolute path of the ``<project>.json`` index to publish.

    Examples:
        >>> import os, tempfile
        >>> d = Path(tempfile.mkdtemp()) / "proj"
        >>> d.mkdir()
        >>> _resolve_out_path(d) == d / ".cache" / "codemap" / "proj.json"
        True
    """
    identity = index_paths.resolve_index(root=root)
    if identity.override:
        return identity.index_path
    return root / ".cache" / "codemap" / f"{root.name}.json"


def main() -> None:
    """Run one scan attempt with terminal telemetry, including handled failures."""
    previous_alarm = signal.getsignal(signal.SIGALRM) if hasattr(signal, "SIGALRM") else None
    previous_timer = signal.getitimer(signal.ITIMER_REAL) if hasattr(signal, "getitimer") else (0.0, 0.0)
    timer_started = time.monotonic()
    with CliInvocation("index", sys.argv[1:]) as invocation:
        try:
            _run_scan(invocation)
        finally:
            if hasattr(signal, "SIGALRM") and signal.getsignal(signal.SIGALRM) != previous_alarm:
                signal.alarm(0)
                signal.signal(signal.SIGALRM, previous_alarm)
                if previous_timer[0] > 0:
                    # Preserve the enclosing caller's elapsed deadline and repeat interval.
                    remaining = max(1e-6, previous_timer[0] - (time.monotonic() - timer_started))
                    signal.setitimer(signal.ITIMER_REAL, remaining, previous_timer[1])


def _run_scan(invocation: CliInvocation) -> None:
    """Parse CLI arguments and run the scan."""
    parser = argparse.ArgumentParser(description="Build the codemap structural index.")
    parser.add_argument("--root", type=Path, default=None, help="Project root (default: git root or cwd)")
    parser.add_argument(
        "--incremental",
        action="store_true",
        help="Re-parse only files changed since last scan (requires an existing v3 index).",
    )
    parser.add_argument(
        "--with-coverage",
        type=str,
        default=None,
        metavar="PATH",
        help=(
            "Path to a .coverage SQLite file (v5.4). When provided, per-symbol "
            "coverage_pct and covered_by fields are attached. Requires coverage>=7.4."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=0,
        metavar="N",
        help="Hard timeout in seconds; 0 = no limit (default). Uses SIGALRM — Unix only.",
    )
    args = parser.parse_args()

    if args.timeout > 0 and hasattr(signal, "SIGALRM"):

        def _timeout_handler(signum: int, frame: object) -> None:  # noqa: ARG001
            """Retain the handled timeout outcome before unwinding the scan."""
            invocation.result = {"error": "timeout", "timeout_seconds": args.timeout}
            print(f"scan-index: timed out after {args.timeout}s", file=sys.stderr)
            sys.exit(2)

        signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(args.timeout)

    try:
        root = args.root or find_root()
        out_path = _resolve_out_path(root)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        # The exclusive writer lease is taken HERE, in the engine, not in whatever
        # launched it — so every route in (the codemap-py dispatcher, bin/scan-index,
        # the query engine's self-heal spawn, a hook's background refresh) is gated by
        # construction. A caller must NOT wrap this in a second lease: the parent's
        # intent is foreign and live to this process, so the child would wait out its
        # whole deadline and fail index_busy on every run.
        index = rwgate.write_index(
            out_path,
            lambda target: _build_and_publish(args, root, target),
            writer_version=SCAN_VERSION,
            **_gate_timeout_kwargs(),
        )

        ok = sum(1 for m in index["modules"] if m.get("status") == "ok")
        degraded = sum(1 for m in index["modules"] if m.get("status") == "degraded")

        print(f"[codemap] \u2713 {out_path}", file=sys.stderr)
        print(f"[codemap]   {ok} modules indexed, {degraded} degraded", file=sys.stderr)
        if degraded:
            for m in index["modules"]:
                if m.get("status") == "degraded":
                    print(f"[codemap]   \u26a0 {m['path']}: {m['reason']}", file=sys.stderr)
        invocation.result = {**_refresh_result(args), "modules_indexed": ok, "degraded": degraded}
    except rwgate.IndexBusy as exc:
        invocation.result = {"error": "index_busy"}
        _die_gate("index_busy", str(exc))
    except rwgate.VersionSkewRefused as exc:
        invocation.result = {"error": "index_version_skew"}
        _die_gate("index_version_skew", str(exc))
    except rwgate.IndexUnreadable as exc:
        invocation.result = {"error": "index_unreadable"}
        # Subclass of CoordinationUnavailable, so it must be caught ahead of it.
        _die_gate("index_unreadable", str(exc))
    except rwgate.CoordinationUnavailable as exc:
        invocation.result = {"error": "index_coordination_unavailable"}
        _die_gate("index_coordination_unavailable", str(exc))
    except PermissionError as exc:
        print(f"[codemap] ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
