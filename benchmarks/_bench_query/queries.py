"""Scan-query invocation and per-query JSON shape validation."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from _bench_query.models import Query, ScanResult, ValidationResult
from _bench_query.sources import path_to_module

# Imported as a module, not a name: ``_run`` is the seam tests patch, and both this module
# and ``cold`` itself call it — one patch on ``cold._run`` has to reach every caller.
from _bench_query import cold


# ---- WARM QUERIES ----


# find_codemap_bin comes from codemap (shared with generate-tasks-bench).


def run_scan_query_result(scan_query_bin: Path, args: list[str], index_path: Path, repo_path: Path) -> ScanResult:
    """Run scan-query and return a :class:`ScanResult` distinguishing success from failure.

    Always passes ``--index <index_path>`` so scan-query uses the correct index
    regardless of ``cwd`` / git availability.  On failure the ``error`` field
    carries a short reason (trailing stderr line when available) so a crashed,
    timed-out, or absent-module query is never confused with an empty result.

    Args:
        scan_query_bin: Path to the scan-query Python script.
        args: Subcommand and its arguments (e.g. ``["rdeps", "foo.bar"]``).
        index_path: Path to the pre-built codemap JSON index.
        repo_path: Working directory for the subprocess (the repository root).

    Returns:
        :class:`ScanResult` with ``data`` set and ``error=None`` on success, or
        ``data=None`` and a non-empty ``error`` reason on any failure.
    """
    cmd = ["python3", str(scan_query_bin.resolve()), "--index", str(index_path.resolve())] + args
    try:
        result = cold._run(cmd, cwd=str(repo_path))
    except subprocess.TimeoutExpired:
        return ScanResult(data=None, error="timeout after 30s")
    except OSError as exc:
        return ScanResult(data=None, error=f"os error: {exc}")
    if result.returncode != 0:
        stderr_lines = (result.stderr or "").strip().splitlines()
        detail = stderr_lines[-1][:200] if stderr_lines else f"exit {result.returncode}"
        return ScanResult(data=None, error=f"exit {result.returncode}: {detail}")
    try:
        return ScanResult(data=json.loads(result.stdout), error=None)
    except json.JSONDecodeError as exc:
        return ScanResult(data=None, error=f"invalid JSON: {exc}")


def run_scan_query(scan_query_bin: Path, args: list[str], index_path: Path, repo_path: Path) -> dict | None:
    """Run scan-query and return parsed JSON, or ``None`` on any failure.

    Thin wrapper over :func:`run_scan_query_result` for call sites that only need
    the data and treat every failure (non-zero exit, timeout, bad JSON, OS error)
    as ``None``.

    Args:
        scan_query_bin: Path to the scan-query Python script.
        args: Subcommand and its arguments (e.g. ``["rdeps", "foo.bar"]``).
        index_path: Path to the pre-built codemap JSON index.
        repo_path: Working directory for the subprocess (the repository root).

    Returns:
        Parsed JSON dict from scan-query stdout, or ``None`` on failure.
    """
    return run_scan_query_result(scan_query_bin, args, index_path, repo_path).data


# ---- ACCURACY ----


def codemap_rdeps_result(
    scan_query_bin: Path, index_path: Path, repo_path: Path, module: str
) -> tuple[set[str], str | None]:
    """Retrieve a module's reverse-dependencies from the index, surfacing tool errors.

    Args:
        scan_query_bin: Path to the scan-query executable.
        index_path: Path to the pre-built codemap JSON index.
        repo_path: Working directory for the subprocess (the repository root).
        module: Dotted module name whose importers are to be retrieved.

    Returns:
        Tuple ``(importers, error)``.  ``error`` is ``None`` on success — even
        when the importer set is legitimately empty — and a short reason string
        when scan-query failed.  When ``error`` is set the returned set is empty
        and must NOT be scored as a passing result.
    """
    res = run_scan_query_result(scan_query_bin, ["rdeps", module], index_path, repo_path)
    if not res.ok:
        return set(), res.error
    return set((res.data or {}).get("imported_by", [])), None


# ---- COVERAGE GAP (real importer-set comparison) ----


def grep_importers_boundary(repo_path: Path, module: str) -> set[str]:
    """Find importers of ``module`` with a boundary-anchored, import-statement grep.

    Unlike a naive dotted-name grep, the pattern is anchored to the start of the line
    (allowing leading whitespace) and requires ``module`` to be followed by
    whitespace, a dot, or end-of-line.  This avoids substring false matches such
    as ``import pkg.target_helper`` matching a search for ``pkg.target``.  It
    still misses relative (``from . import x``) and aliased-package imports that
    do not spell the dotted name literally — those are recovered by
    :func:`verify_importer` during coverage-gap analysis.

    Args:
        repo_path: Root of the repository to search.
        module: Dotted module name whose importers are to be found.

    Returns:
        Set of dotted module names that textually import ``module``.  Excludes
        ``module`` itself.  Returns an empty set on timeout or no matches.
    """
    escaped = re.escape(module)
    # ^<ws>(from|import)<ws><module>(<ws> | . | EOL) — POSIX ERE for portability (BSD/GNU grep).
    pattern = rf"^[[:space:]]*(from|import)[[:space:]]+{escaped}([[:space:].]|$)"
    try:
        result = cold._run(
            [
                "grep",
                "-rlE",
                pattern,
                str(repo_path),
                "--include=*.py",
                "--exclude-dir=.git",
                "--exclude-dir=__pycache__",
            ]
        )
    except subprocess.TimeoutExpired:
        return set()

    modules: set[str] = set()
    repo_root = str(repo_path)
    for line in result.stdout.strip().splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        mod = path_to_module(stripped, repo_root)
        if mod and mod != module:
            modules.add(mod)
    return modules


# ---- QUERY SHAPE ----


def validate_central_json(data: dict) -> ValidationResult:
    """Validate that a scan-query ``central`` response has the required structure.

    Checks that ``data`` contains a non-empty ``central`` list and that every
    item in the list has a ``rdep_count`` field.

    Args:
        data: Parsed JSON dict from a ``scan-query central`` call.

    Returns:
        :class:`ValidationResult` with ``ok=True`` on success, or ``ok=False``
        and a reason string describing the first structural violation found.
    """
    if not isinstance(data, dict):
        return ValidationResult(ok=False, reason="response is not an object")
    if "central" not in data:
        return ValidationResult(ok=False, reason="missing 'central' key")
    central = data["central"]
    if not isinstance(central, list) or len(central) == 0:
        return ValidationResult(ok=False, reason="'central' is empty or not a list")
    for item in central:
        if not isinstance(item, dict):
            return ValidationResult(ok=False, reason="central item is not an object")
        if "rdep_count" not in item:
            return ValidationResult(ok=False, reason="central item missing 'rdep_count'")
        if not isinstance(item["rdep_count"], int):
            return ValidationResult(ok=False, reason="central item 'rdep_count' is not an int")
    return ValidationResult(ok=True, reason="")


def validate_rdeps_json(data: dict) -> ValidationResult:
    """Validate that a scan-query ``rdeps`` response has the required structure.

    Checks that ``data`` contains both ``imported_by`` and ``module`` keys.

    Args:
        data: Parsed JSON dict from a ``scan-query rdeps`` call.

    Returns:
        :class:`ValidationResult` with ``ok=True`` on success, or ``ok=False``
        and a reason string describing the missing key.
    """
    if not isinstance(data, dict):
        return ValidationResult(ok=False, reason="response is not an object")
    if "imported_by" not in data:
        return ValidationResult(ok=False, reason="missing 'imported_by' key")
    if "module" not in data:
        return ValidationResult(ok=False, reason="missing 'module' key")
    if not isinstance(data["imported_by"], list):
        return ValidationResult(ok=False, reason="'imported_by' is not a list")
    if not isinstance(data["module"], str):
        return ValidationResult(ok=False, reason="'module' is not a string")
    return ValidationResult(ok=True, reason="")


def validate_deps_json(data: dict) -> ValidationResult:
    """Validate that a scan-query ``deps`` response has the required structure.

    Checks that ``data`` contains both ``direct_imports`` and ``module`` keys.

    Args:
        data: Parsed JSON dict from a ``scan-query deps`` call.

    Returns:
        :class:`ValidationResult` with ``ok=True`` on success, or ``ok=False``
        and a reason string describing the missing key.
    """
    if not isinstance(data, dict):
        return ValidationResult(ok=False, reason="response is not an object")
    if "direct_imports" not in data:
        return ValidationResult(ok=False, reason="missing 'direct_imports' key")
    if "module" not in data:
        return ValidationResult(ok=False, reason="missing 'module' key")
    if not isinstance(data["direct_imports"], list):
        return ValidationResult(ok=False, reason="'direct_imports' is not a list")
    if not isinstance(data["module"], str):
        return ValidationResult(ok=False, reason="'module' is not a string")
    return ValidationResult(ok=True, reason="")


_QUERY_SHAPE_VALIDATORS = {"central": validate_central_json, "rdeps": validate_rdeps_json, "deps": validate_deps_json}


def run_query_shape_query(
    scan_query_bin: Path, index_path: Path, repo_path: Path, query: Query
) -> tuple[bool, bool, dict | None, str | None]:
    """Run one skill query and validate its output SHAPE (not the injection path).

    Executes ``scan-query <query.cmd> <query.args>`` via
    :func:`run_scan_query_result` and validates the returned JSON using the
    registered validator for the command type (``central``, ``rdeps``, or
    ``deps``).  Commands without a registered validator (``path``, ``coupled``)
    are considered automatically valid when they return a non-null result.  A
    scan-query failure is reported as ``present=False`` with a non-empty error
    reason so the caller can surface it rather than treat it as a bad shape.

    Args:
        scan_query_bin: Path to the ``scan-query`` executable.
        index_path: Path to the pre-built codemap index JSON file.
        repo_path: Root directory of the repository under test.
        query: :class:`Query` specifying the command and its positional arguments.

    Returns:
        A 4-tuple ``(present, valid, data, error)`` where:

        - ``present`` (``bool``): ``True`` when scan-query returned a non-null result.
        - ``valid`` (``bool``): ``True`` when the result passes the structural
          JSON validator for ``query.cmd``, or when no validator is registered.
        - ``data`` (``dict | None``): The raw parsed JSON dict, or ``None`` on failure.
        - ``error`` (``str | None``): Short scan-query failure reason, or ``None``
          on success.
    """
    res = run_scan_query_result(scan_query_bin, [query.cmd] + query.args, index_path, repo_path)
    if not res.ok:
        return False, False, None, res.error
    data = res.data
    validator = _QUERY_SHAPE_VALIDATORS.get(query.cmd)
    if validator is None:
        return True, True, data, None  # path/coupled — no structural validator needed
    v = validator(data)
    return True, v.ok, data, None
