#!/usr/bin/env python3
"""Reject hardcoded machine and temporary paths in benchmark source and docs.

Generated manifests, paid-run evidence, and regression fixtures are excluded: they may preserve machine paths as
immutable evidence or deliberate bad inputs.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

#: Benchmarks directory that is scanned for machine-specific paths.
BENCHMARKS = Path(__file__).resolve().parent
#: Policy seed file, scanned in addition to the benchmark sources although it lives under policy.
POLICY_SEED = BENCHMARKS / "policy" / "provider-parity-methodology.json"
#: File extensions checked for forbidden paths.
SOURCE_SUFFIXES = frozenset({".json", ".md", ".py", ".sh"})
#: Directory names skipped during the scan.
EXCLUDED_DIRS = frozenset({"manifests", "results", "tests"})
#: Escaped forward slash used to assemble the path pattern below.
_SLASH = re.escape(chr(47))
#: Matches a user home directory path or a root temp directory path that would break on another machine.
FORBIDDEN_PATH = re.compile(
    rf"{_SLASH}(?:Users|home){_SLASH}[A-Za-z0-9_-]+{_SLASH}|"
    rf"{_SLASH}(?:private{_SLASH})?tmp{_SLASH}"
)


def _python_strings(path: Path) -> list[tuple[int, str]]:
    """Return line-numbered Python string literals from one source file."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        (node.lineno + offset, line)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        for offset, line in enumerate(node.value.splitlines())
    ]


#: A line carrying this marker documents a deliberate, reviewed root-temp path — the
#: canonical parity target, whose value is locked in suites/patch-index-locks.json and so
#: cannot follow $TMPDIR. The point of the marker is that the exemption is visible and
#: justified in the source, unlike an assembled literal the gate simply could not see.
EXEMPTION_MARKER = "portable-paths: canonical-target"


def _assembled_root_temp_dirs(path: Path) -> list[tuple[int, str]]:
    """Return root-temp paths assembled from ``os.sep`` rather than written as literals.

    A literal scan cannot see these: the separator arrives from ``os.sep`` and only the
    bare segment ``"tmp"`` appears as a string, so the assembled path slipped the gate
    while still hardcoding ``/tmp`` — and resolving to the drive root on Windows.
    Covers both ``Path(os.sep) / "tmp"`` and the f-string ``f"{os.sep}tmp{os.sep}..."``.
    """
    source_lines = path.read_text(encoding="utf-8").splitlines()
    tree = ast.parse("\n".join(source_lines), filename=str(path))

    def exempt(lineno: int) -> bool:
        """Return whether the reported line carries the documented exemption marker."""
        return EXEMPTION_MARKER in source_lines[lineno - 1] if 0 < lineno <= len(source_lines) else False

    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        assembled = False
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            left = node.left
            assembled = (
                isinstance(node.right, ast.Constant)
                and node.right.value in {"tmp", "temp"}
                and isinstance(left, ast.Call)
                and isinstance(left.func, ast.Name)
                and left.func.id == "Path"
                and len(left.args) == 1
                and isinstance(left.args[0], ast.Attribute)
                and left.args[0].attr == "sep"
            )
        elif isinstance(node, ast.JoinedStr):
            parts = [
                value.value.attr
                if isinstance(value, ast.FormattedValue) and isinstance(value.value, ast.Attribute)
                else value.value
                if isinstance(value, ast.Constant)
                else None
                for value in node.values
            ]
            assembled = parts[:2] == ["sep", "tmp"]
        if assembled and not exempt(node.lineno):
            violations.append(
                (node.lineno, f"assembled root temp path; use tempfile.gettempdir() or mark '{EXEMPTION_MARKER}'")
            )
    return violations


def find_violations(path: Path) -> list[tuple[int, str]]:
    """Return forbidden absolute-path literals in one governed source file."""
    if path.suffix == ".py":
        lines = _python_strings(path)
    elif path.suffix in {".json", ".md", ".sh"}:
        lines = list(enumerate(path.read_text(encoding="utf-8").splitlines(), start=1))
    else:
        return []
    found = [
        (line_number, line.strip())
        for line_number, line in lines
        if not line.lstrip().startswith("#") and FORBIDDEN_PATH.search(line)
    ]
    if path.suffix == ".py":
        found.extend(_assembled_root_temp_dirs(path))
    return sorted(found)


def _default_sources() -> list[Path]:
    """Return executable benchmark sources governed by the portability gate."""
    sources = {
        path
        for path in BENCHMARKS.rglob("*")
        if path.is_file()
        and path.suffix in SOURCE_SUFFIXES
        and not any(part in EXCLUDED_DIRS for part in path.relative_to(BENCHMARKS).parts[:-1])
    }
    sources.add(POLICY_SEED)
    return sorted(sources)


def main(paths: list[str] | None = None) -> int:
    """Check explicit pre-commit paths or all governed benchmark sources."""
    candidates = [Path(raw) for raw in paths] if paths else _default_sources()
    violations = [
        (path, line_number, line)
        for path in candidates
        if path.is_file() and path.suffix in SOURCE_SUFFIXES
        for line_number, line in find_violations(path)
    ]
    for path, line_number, line in violations:
        print(f"{path}:{line_number}: hardcoded absolute machine or temporary path: {line}", file=sys.stderr)
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
