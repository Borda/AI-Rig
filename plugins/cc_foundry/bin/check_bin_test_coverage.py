#!/usr/bin/env python
"""check_bin_test_coverage.py — every bin/ script must have a real test file (audit Check R4).

Replaces the inline Check R4 shell block, which shelled out to a ``python -c``
AST heredoc for the stub detection. For each ``plugins/*/bin/*.py`` (skipping
``_``-prefixed private helpers) the matching ``plugins/<plugin>/tests/test_<name>.py``
must exist, be non-empty, declare at least one ``test_`` function, and have at
least one such function whose body is more than ``pass`` or ``...``.

A script that is a MANIFEST **copy** (``propagate_shared.py``) of an already-tested
canonical is exempt: ``propagate_shared.py --apply`` plus pre-commit guarantee it stays
byte-identical to its canonical, so it can never carry a test gap its canonical lacks.
The canonical itself is never exempt — only copies are.

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
import importlib.util
import subprocess
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


def _find_nested_test(test_file: Path) -> Path | None:
    """Search one level of subdirectories under ``tests/`` for a script's test file.

    Some plugins group tests by category (``tests/scanner/test_foo.py``) instead of the flat
    ``tests/test_foo.py`` layout ``expected_test`` assumes. Only used as a fallback when the flat
    path is missing, so plugins using the flat layout see no behavior change.

    Args:
        test_file: The flat ``tests/test_<name>.py`` path that was not found.

    Returns:
        The first matching nested test file, or ``None`` if there isn't one.
    """
    tests_dir = test_file.parent
    if not tests_dir.is_dir():
        return None
    matches = sorted(tests_dir.glob(f"*/{test_file.name}"))
    return matches[0] if matches else None


def check_script(script: Path) -> str | None:
    """Return a finding line for one script, or None when it is covered."""
    test_file = expected_test(script)
    if not test_file.is_file():
        nested = _find_nested_test(test_file)
        if nested is None:
            return f"R4-FAIL (no test file): {script.as_posix()} → expected {test_file.as_posix()}"
        test_file = nested
    source = test_file.read_text(encoding="utf-8")
    if not source.strip():
        return f"R4-FAIL (empty test file): {test_file.as_posix()}"
    total, real = test_functions(source)
    if total == 0:
        return f"R4-FAIL (no test functions): {test_file.as_posix()} — has no def test_ functions"
    if real == 0:
        return f"R4-FAIL (stub tests only): {test_file.as_posix()} — all {total} test function(s) are pass/... stubs"
    return None


def repo_root() -> Path:
    """Return the git repository root for the current working directory."""
    output = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return Path(output).resolve()


def manifest_copies(root: Path) -> set[str]:
    """Repo-relative paths that are byte-identical copies of a tested canonical.

    Canonicals are deliberately excluded from this set — an untested canonical must still fail
    R4. Only copies (guaranteed byte-identical to their canonical by ``propagate_shared.py
    --apply`` + pre-commit) are safe to skip. Both the MANIFEST module and every canonical's
    existence check resolve against ``root``, never the process cwd, so this agrees with the
    same-basis comparison the R4 loop performs when matching a scanned script's path.

    Args:
        root: Repository root; ``propagate_shared.py`` is expected at
            ``root/plugins/cc_foundry/bin/propagate_shared.py``.

    Returns:
        Repo-relative POSIX path strings safe to skip in the R4 loop; empty when
        ``propagate_shared.py`` is missing or fails to load.
    """
    manifest_file = root / "plugins" / "cc_foundry" / "bin" / "propagate_shared.py"
    if not manifest_file.is_file():
        return set()
    spec = importlib.util.spec_from_file_location("_propagate_shared", manifest_file)
    if spec is None or spec.loader is None:
        return set()
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    copies: set[str] = set()
    for entry in module.MANIFEST:
        canonical = root / str(entry["canonical"])
        if not canonical.is_file():
            continue  # canonical missing/moved — do not skip its copies; let them fail R4
        copies.update(str(c) for c in entry["copies"])
    return copies


def _manifest_copies_for_repo() -> tuple[Path | None, set[str]]:
    """Return (resolved repo root, MANIFEST copy paths), degrading to (None, empty) offline.

    A missing git binary or a checkout with no ``.git`` must not crash Check R4 — the file's own contract is exit 0
    always, report-only. Losing the exemption is an acceptable degradation; aborting is not.
    """
    try:
        root = repo_root()
    except (subprocess.CalledProcessError, OSError):
        return None, set()
    return root, manifest_copies(root)


def _relative_to_root(script: Path, root: Path | None) -> str | None:
    """Return script's repo-relative POSIX path, or None when outside root or root unknown."""
    if root is None:
        return None
    try:
        return script.resolve().relative_to(root).as_posix()
    except ValueError:
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

    root, copies = _manifest_copies_for_repo()
    findings: list[str] = []
    deferred = 0
    for script in bin_scripts(Path(args.scan_dir)):
        rel = _relative_to_root(script, root)
        if rel is not None and rel in copies:
            deferred += 1
            continue
        finding = check_script(script)
        if finding:
            findings.append(finding)

    for finding in findings:
        print(finding)
    if not findings:
        print("✓: all bin/ Python scripts have non-empty test files with real assertions")
    if deferred:
        print(f"({deferred} MANIFEST copies deferred to their canonical)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
