#!/usr/bin/env python3
"""parse_audit_json.py — summarize pip-audit JSON output as deps/vulns counts.

Usage:
    pip-audit --format=json | python "${CLAUDE_PLUGIN_ROOT}/bin/parse_audit_json.py"

Reads a pip-audit JSON document from stdin and prints a single-line summary of
the form ``N deps, M vulns``.  With ``--introduced a,b`` (dependency names whose
declaration or lock entry changed inside the release range) the line gains the
scope split ``| new: K [pkgs] | pre-existing: P [pkgs]``.  Dependencies pip-audit could not audit (URL or
local requirements) are left out of ``N`` and listed as ``| skipped: K [pkgs]``.

The module also reads the shipped dependency set from ``pyproject.toml`` for the release CVE scan
(:func:`shipped_extra_groups`, :func:`shipped_requirements`).

Extracted from run_audit_checks.sh inline ``python -c`` block (as noted in the security audit)
to satisfy the project Check 23e policy prohibiting inline ``python -c`` snippets.

Exit codes:
    0 — on success
    1 — stdin read error or JSON parse error
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


def _skipped(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the dependency entries pip-audit reported as not audited (they carry a ``skip_reason``)."""
    return [dep for dep in payload.get("dependencies", []) if dep.get("skip_reason")]


def _skipped_suffix(payload: dict[str, Any]) -> str:
    """Render ``| skipped: K [pkgs]`` for unaudited dependencies, or an empty string when none were skipped.

    Examples:
        >>> _skipped_suffix({"dependencies": [{"name": "local-pkg", "skip_reason": "not on PyPI"}]})
        ' | skipped: 1 [local-pkg]'
        >>> _skipped_suffix({"dependencies": [{"name": "a", "vulns": []}]})
        ''
    """
    skipped = _skipped(payload)
    if not skipped:
        return ""
    return f" | skipped: {len(skipped)} [{', '.join(str(dep.get('name', '?')) for dep in skipped)}]"


def summarize(payload: dict[str, Any]) -> str:
    """Render a pip-audit JSON payload as a ``N deps, M vulns`` summary line.

    Args:
        payload: Parsed pip-audit JSON; expects a top-level ``dependencies``
            list, each entry containing a ``vulns`` list. Entries with a ``skip_reason`` were not audited and do
            not count as dependencies.

    Returns:
        Single-line summary string.

    Examples:
        >>> summarize({"dependencies": []})
        '0 deps, 0 vulns'
        >>> summarize({"dependencies": [{"vulns": []}, {"vulns": [{}, {}]}]})
        '2 deps, 2 vulns'
        >>> summarize({"dependencies": [{"vulns": [{}]}]})
        '1 deps, 1 vulns'
        >>> summarize({"dependencies": [{"vulns": []}, {"name": "x", "skip_reason": "not on PyPI"}]})
        '1 deps, 0 vulns'
    """
    deps = [dep for dep in payload.get("dependencies", []) if not dep.get("skip_reason")]
    n_vulns = sum(len(dep.get("vulns", [])) for dep in deps)
    return f"{len(deps)} deps, {n_vulns} vulns"


def _normalize(name: str) -> str:
    """Return the PEP 503 normalized form of a distribution name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def summarize_scoped(payload: dict[str, Any], introduced: set[str]) -> str:
    """Render the summary line plus a new-versus-pre-existing vulnerability split.

    A vulnerable package is ``new`` when its normalized name is in ``introduced`` (added or changed inside the
    release range) and ``pre-existing`` otherwise.

    Args:
        payload: Parsed pip-audit JSON, same shape as for :func:`summarize`.
        introduced: Normalized names of dependencies touched inside the release range.

    Returns:
        ``N deps, M vulns | new: K [pkgs] | pre-existing: P [pkgs]``, followed by ``| skipped: K [pkgs]`` when
        pip-audit could not audit some dependencies.

    Examples:
        >>> payload = {"dependencies": [{"name": "Foo_Bar", "vulns": [{}]}, {"name": "baz", "vulns": [{}, {}]}]}
        >>> summarize_scoped(payload, {"foo-bar"})
        '2 deps, 3 vulns | new: 1 [Foo_Bar] | pre-existing: 2 [baz]'
        >>> summarize_scoped({"dependencies": []}, set())
        '0 deps, 0 vulns | new: 0 [] | pre-existing: 0 []'
    """
    new: dict[str, int] = {}
    old: dict[str, int] = {}
    for dep in payload.get("dependencies", []):
        count = len(dep.get("vulns", []))
        if not count:
            continue
        name = str(dep.get("name", "?"))
        bucket = new if _normalize(name) in introduced else old
        bucket[name] = count
    return (
        f"{summarize(payload)} | new: {sum(new.values())} [{', '.join(new)}]"
        f" | pre-existing: {sum(old.values())} [{', '.join(old)}]{_skipped_suffix(payload)}"
    )


#: Optional-dependency group names that hold tooling, never shipped behaviour.
DEV_EXTRAS = frozenset({"dev", "test", "tests", "testing", "doc", "docs", "lint", "typing", "bench", "benchmark"})


def _load_toml(path: Path) -> dict[str, Any] | None:
    """Parse a TOML file with ``tomllib`` or the ``tomli`` backport; ``None`` when neither is importable."""
    try:
        import tomllib
    except ModuleNotFoundError:
        try:
            import tomli as tomllib
        except ModuleNotFoundError:
            return None
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _project_table(pyproject: Path) -> dict[str, Any] | None:
    """Return the ``[project]`` table of a ``pyproject.toml``, or ``None`` when it is missing, empty or unreadable."""
    try:
        data = _load_toml(pyproject)
    except (OSError, ValueError):
        return None
    return (data or {}).get("project") or None


def shipped_extra_groups(pyproject: Path) -> list[str] | None:
    """Return the optional-dependency groups the distribution ships, or ``None`` without a readable ``[project]``.

    Groups named in :data:`DEV_EXTRAS` are development tooling and are left out. Callers use ``None`` to decide that
    no shipped dependency set exists to scan.

    Args:
        pyproject: Path to the repository ``pyproject.toml``.

    Returns:
        Sorted group names exactly as spelled in the file, or ``None`` when the file is missing, unparsable, or has no
        ``[project]`` table.

    Examples:
        >>> shipped_extra_groups(Path("/nonexistent/pyproject.toml")) is None
        True
    """
    project = _project_table(pyproject)
    if project is None:
        return None
    return sorted(g for g in project.get("optional-dependencies", {}) if _normalize(g) not in DEV_EXTRAS)


def shipped_requirements(pyproject: Path) -> list[str] | None:
    """Return the requirements the distribution declares to ship, or ``None`` when they cannot be read statically.

    The list holds ``[project].dependencies`` plus the requirements of every optional-dependency group not named in
    :data:`DEV_EXTRAS`. ``None`` means the shipped set is unknowable from the file: no readable ``[project]`` table,
    or ``dependencies`` listed as dynamic (filled in by the build backend).

    Args:
        pyproject: Path to the repository ``pyproject.toml``.

    Returns:
        Requirement strings in file order, or ``None`` when the set cannot be established.

    Examples:
        >>> shipped_requirements(Path("/nonexistent/pyproject.toml")) is None
        True
    """
    project = _project_table(pyproject)
    if project is None or "dependencies" in project.get("dynamic", []):
        return None
    reqs = list(project.get("dependencies", []))
    for group, group_reqs in project.get("optional-dependencies", {}).items():
        if _normalize(group) not in DEV_EXTRAS:
            reqs.extend(group_reqs)
    return reqs


def main(argv: list[str] | None = None) -> int:
    """CLI entry point — read JSON from stdin, print summary.

    Args:
        argv: Optional argv override. Ignored except for an explicit ``-h``/``--help``
            (preserves the legacy stdin-only, argv-ignored contract). pip-audit
            JSON arrives via stdin.

    Returns:
        Exit code: 0 on success, 1 on parse error; argparse exits 0 after printing ``-h``/``--help``.

    Examples:
        No doctest — requires stdin; covered by pytest with monkeypatch.
    """
    effective_argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(
        prog="parse_audit_json.py",
        description="Summarize pip-audit JSON output (read from stdin) as deps/vulns counts.",
    )
    parser.add_argument(
        "--introduced",
        default=None,
        help="Comma-separated dependency names changed in the release range; adds the new/pre-existing split.",
    )
    # Unknown argv stays ignored (legacy stdin-only contract); only the known options are honoured.
    args, _ = parser.parse_known_args(effective_argv)
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(f"parse_audit_json: invalid JSON on stdin: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"parse_audit_json: stdin read error: {e}", file=sys.stderr)
        return 1

    if args.introduced is not None:
        names = {_normalize(n) for n in args.introduced.split(",") if n.strip()}
        print(summarize_scoped(payload, names))
    else:
        print(summarize(payload) + _skipped_suffix(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
