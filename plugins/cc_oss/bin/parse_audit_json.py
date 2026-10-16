#!/usr/bin/env python3
"""parse_audit_json.py — summarize pip-audit JSON output as deps/vulns counts.

Usage:
    pip-audit --format=json | python "${CLAUDE_PLUGIN_ROOT}/bin/parse_audit_json.py"

Reads a pip-audit JSON document from stdin and prints a single-line summary of
the form ``N deps, M vulns``.  With ``--introduced a,b`` (dependency names whose
declaration or lock entry changed inside the release range) the line gains the
scope split ``| new: K [pkgs] | pre-existing: P [pkgs]``.  With ``--runtime-from pyproject.toml``
only packages the built distribution ships (runtime dependencies, non-dev extras, and their transitive
closure) are counted; vulnerable test/docs/dev-only packages are listed as ``not shipped``.

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
from importlib import metadata
from pathlib import Path
from typing import Any


def summarize(payload: dict[str, Any]) -> str:
    """Render a pip-audit JSON payload as a ``N deps, M vulns`` summary line.

    Args:
        payload: Parsed pip-audit JSON; expects a top-level ``dependencies``
            list, each entry containing a ``vulns`` list.

    Returns:
        Single-line summary string.

    Examples:
        >>> summarize({"dependencies": []})
        '0 deps, 0 vulns'
        >>> summarize({"dependencies": [{"vulns": []}, {"vulns": [{}, {}]}]})
        '2 deps, 2 vulns'
        >>> summarize({"dependencies": [{"vulns": [{}]}]})
        '1 deps, 1 vulns'
    """
    deps = payload.get("dependencies", [])
    n_deps = len(deps)
    n_vulns = sum(len(dep.get("vulns", [])) for dep in deps)
    return f"{n_deps} deps, {n_vulns} vulns"


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
        ``N deps, M vulns | new: K [pkgs] | pre-existing: P [pkgs]``.

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
        f" | pre-existing: {sum(old.values())} [{', '.join(old)}]"
    )


#: Optional-dependency group names that hold tooling, never shipped behaviour.
DEV_EXTRAS = frozenset({"dev", "test", "tests", "testing", "doc", "docs", "lint", "typing", "bench", "benchmark"})
_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


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


def runtime_closure(pyproject: Path) -> set[str] | None:
    """Return normalized names of every package the built distribution ships, or ``None`` if unknowable.

    Roots are ``[project].dependencies`` plus ``[project.optional-dependencies]`` groups not named in
    :data:`DEV_EXTRAS`; ``[dependency-groups]`` (PEP 735) is never shipped and is ignored. The closure follows
    installed-package metadata, skipping ``extra ==`` requirements. ``None`` (callers then scan everything) when
    the file is missing, unparsable, lists ``dependencies`` as dynamic, or a root without an environment marker is
    not installed, because a partial closure would hide real vulnerabilities.

    Args:
        pyproject: Path to the repository ``pyproject.toml``.

    Returns:
        Normalized package names, or ``None`` when the shipped set cannot be established.

    Examples:
        >>> runtime_closure(Path("/nonexistent/pyproject.toml")) is None
        True
    """
    try:
        data = _load_toml(pyproject)
    except (OSError, ValueError):
        return None
    project = (data or {}).get("project")
    if not project or "dependencies" in project.get("dynamic", []):
        return None
    roots = list(project.get("dependencies", []))
    for group, reqs in project.get("optional-dependencies", {}).items():
        if _normalize(group) not in DEV_EXTRAS:
            roots.extend(reqs)
    seen: set[str] = set()
    queue = [(r, True) for r in roots]
    while queue:
        req, is_root = queue.pop()
        match = _REQ_NAME.match(req)
        if not match:
            continue
        name = _normalize(match.group(1))
        if name in seen:
            continue
        marker = req.partition(";")[2]
        try:
            requires = metadata.distribution(name).requires or []
        except metadata.PackageNotFoundError:
            if is_root and not marker.strip():
                return None
            continue
        seen.add(name)
        queue.extend((r, False) for r in requires if "extra" not in r.partition(";")[2])
    return seen


def summarize_shipped(payload: dict[str, Any], shipped: set[str] | None, introduced: set[str]) -> str:
    """Render the scoped summary for the packages the release ships plus the excluded remainder.

    Args:
        payload: Parsed pip-audit JSON.
        shipped: Normalized shipped names from :func:`runtime_closure`; ``None`` scans every package.
        introduced: Normalized names touched inside the release range.

    Returns:
        :func:`summarize_scoped` line for shipped packages, then ``| not shipped: P [pkgs]`` for vulnerable
        test/docs/dev-only packages, or ``| scope: unknown`` when ``shipped`` is ``None``.

    Examples:
        >>> payload = {"dependencies": [{"name": "a", "vulns": [{}]}, {"name": "pytest", "vulns": [{}]}]}
        >>> summarize_shipped(payload, {"a"}, set())
        '1 deps, 1 vulns | new: 0 [] | pre-existing: 1 [a] | not shipped: 1 [pytest]'
        >>> summarize_shipped(payload, None, set())
        '2 deps, 2 vulns | new: 0 [] | pre-existing: 2 [a, pytest] | scope: unknown'
    """
    if shipped is None:
        return f"{summarize_scoped(payload, introduced)} | scope: unknown"
    deps = payload.get("dependencies", [])
    kept = [d for d in deps if _normalize(str(d.get("name", ""))) in shipped]
    dropped = {str(d.get("name", "?")): len(d.get("vulns", [])) for d in deps if d not in kept and d.get("vulns")}
    line = summarize_scoped({"dependencies": kept}, introduced)
    return f"{line} | not shipped: {sum(dropped.values())} [{', '.join(dropped)}]"


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
    parser.add_argument(
        "--runtime-from",
        default=None,
        metavar="PYPROJECT",
        help="pyproject.toml defining what the built distribution ships; limits the count to those packages.",
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

    names = {_normalize(n) for n in (args.introduced or "").split(",") if n.strip()}
    if args.runtime_from is not None:
        print(summarize_shipped(payload, runtime_closure(Path(args.runtime_from)), names))
    elif args.introduced is not None:
        print(summarize_scoped(payload, names))
    else:
        print(summarize(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
