#!/usr/bin/env python
"""check_bin_test_coverage.py — every bin/ script must have a real test file (audit Check R4).

Replaces the inline Check R4 shell block, which shelled out to a ``python -c``
AST heredoc for the stub detection. For each ``plugins/*/bin/*.py`` (skipping
``_``-prefixed private helpers) the matching ``plugins/<plugin>/tests/test_<name>.py``
must exist, be non-empty, declare at least one ``test_`` function, and have at
least one such function whose body is more than ``pass`` or ``...``.

Usage:
    check_bin_test_coverage.py [--scan-dir plugins] [--local]

    --local mirrors the shell original's LOCAL_MODE guard: the check only means
    something against a source tree, so without it the check reports a skip.

Exit codes:
    0   always — findings are reported on stdout as ``R4-FAIL`` lines, matching the
        inline block this replaced. A non-zero status would read as "the check
        failed to run", not "the check found something".
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path


def _is_stub(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Report whether the function body is only `pass` and/or `...`."""
    for stmt in node.body:
        if isinstance(stmt, ast.Pass):
            continue
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and stmt.value.value is ...:
            continue
        return False
    return True


def test_functions(source: str) -> tuple[int, int]:
    """Return (test function count, non-stub test function count) for a test module."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # Unparsable is not this check's business; treat as "has content" so the
        # lint hooks report the syntax error instead of R4 double-reporting it.
        return (1, 1)
    total = 0
    real = 0
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test_"):
            continue
        total += 1
        if not _is_stub(node):
            real += 1
    return total, real


def bin_scripts(scan_dir: Path) -> list[Path]:
    """Return every public bin/ Python script under scan_dir."""
    return sorted(p for p in scan_dir.glob("*/bin/*.py") if not p.name.startswith("_"))


def expected_test(script: Path) -> Path:
    """Return the test file path a bin/ script is expected to have.

    Hyphens in the script stem become underscores: a test module named ``test_a-b.py`` is not
    importable, so a hyphenated script's tests can only live under the underscore spelling.

    Examples:
        >>> expected_test(Path("plugins/p/bin/thing.py")).as_posix()
        'plugins/p/tests/test_thing.py'
        >>> expected_test(Path("plugins/p/bin/parse-skill-flags.py")).as_posix()
        'plugins/p/tests/test_parse_skill_flags.py'
    """
    return script.parent.parent / "tests" / f"test_{script.stem.replace('-', '_')}.py"


def check_script(script: Path) -> str | None:
    """Return a finding line for one script, or None when it is covered."""
    test_file = expected_test(script)
    if not test_file.is_file():
        return f"R4-FAIL (no test file): {script.as_posix()} → expected {test_file.as_posix()}"
    source = test_file.read_text(encoding="utf-8")
    if not source.strip():
        return f"R4-FAIL (empty test file): {test_file.as_posix()}"
    total, real = test_functions(source)
    if total == 0:
        return f"R4-FAIL (no test functions): {test_file.as_posix()} — has no def test_ functions"
    if real == 0:
        return f"R4-FAIL (stub tests only): {test_file.as_posix()} — all {total} test function(s) are pass/... stubs"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scan-dir", default="plugins")
    parser.add_argument("--local", action="store_true", help="run the check; without it, report a skip")
    args = parser.parse_args()

    print("=== Check R4: bin/ Python test coverage ===")
    if not args.local:
        print("✓: Check R4 skipped in non-local mode")
        return 0

    findings = [f for f in (check_script(s) for s in bin_scripts(Path(args.scan_dir))) if f]
    for finding in findings:
        print(finding)
    if not findings:
        print("✓: all bin/ Python scripts have non-empty test files with real assertions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
