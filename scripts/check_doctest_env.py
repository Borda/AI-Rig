#!/usr/bin/env python3
"""Flag doctests that assign ``os.environ`` keys without undoing the assignment in the same docstring.

Doctests share the interpreter with every other test in their pytest worker, so an unrestored ``os.environ`` assignment
outlives the example that made it. With ``pytest-randomly`` deciding order, the victim and the symptom both change from
run to run, which reads as flakiness rather than as the ordering defect it is. The observed case: a doctest planted the
POSIX literal ``/tmp`` in ``TMPDIR`` (unset on Windows, so the fallback applied), and a later doctest in another plugin
resolved that value against the current drive and failed there.

An assignment paired with ``os.environ.pop(...)`` or ``os.environ.setdefault(...)`` of the same key in the same
docstring is the accepted save-and-restore idiom; an assignment alone is the defect. This is a hook rather than a test
because it inspects source text only: no product behaviour depends on it.

Usage: ``check_doctest_env.py [--root DIR] [FILE...]``. Without files, every Python source under ``plugins/``,
``benchmarks/`` and ``tests/`` is scanned. Vendored, cached and generated trees are skipped. Exit 1 on any finding.
"""

from __future__ import annotations

import argparse
import ast
import doctest
import re
import sys
from collections.abc import Iterable
from pathlib import Path

#: Directories holding vendored copies, captured benchmark evidence, or build output: their doctests are not executed by
#: this repository's pytest run, so their hygiene is not ours.
EXCLUDED_PARTS = frozenset(
    {".cache", ".git", ".sandbox", ".reports", ".temp", ".venv", "__pycache__", "node_modules", "results"}
)
#: Source trees whose doctests this repository executes.
SCANNED_ROOTS = ("plugins", "benchmarks", "tests")

_ASSIGN_RE = re.compile(r"""os\.environ\[\s*["'](?P<key>[^"']+)["']\s*\]\s*=""")
_REMOVE_RE = re.compile(r"""os\.environ\.(?:pop|setdefault)\(\s*["'](?P<key>[^"']+)["']""")


def _docstrings(source: str) -> list[tuple[int, str]]:
    """Return ``(line, text)`` for every docstring in ``source``: module, class, function and method alike.

    A file whose syntax the running interpreter rejects yields none: pytest cannot import it either, so it has no
    doctests to leak on this version (the frozen ``codemap-py`` grammar corpus carries newer syntax on purpose).

    Examples:
        >>> _docstrings("def f():\\n    'Inner.'\\n")
        [(1, 'Inner.')]
        >>> _docstrings("def broken(:\\n")
        []
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    nodes = [tree, *(n for n in ast.walk(tree) if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)))]
    return [(getattr(node, "lineno", 1), text) for node in nodes if (text := ast.get_docstring(node, clean=False))]


def unrestored_keys(docstring: str) -> set[str]:
    """Return environment keys the docstring's doctest assigns but never pops or restores.

    Examples:
        >>> unrestored_keys(">>> os.environ['A'] = '1'\\n")
        {'A'}
        >>> unrestored_keys(">>> os.environ['A'] = '1'\\n>>> os.environ.pop('A')\\n")
        set()
    """
    example_source = "".join(example.source for example in doctest.DocTestParser().get_examples(docstring))
    assigned = {m.group("key") for m in _ASSIGN_RE.finditer(example_source)}
    removed = {m.group("key") for m in _REMOVE_RE.finditer(example_source)}
    return assigned - removed


def check_source(source: str, filename: str) -> list[str]:
    """Return one ``file:line: message`` per docstring that leaks environment keys."""
    return [
        f"{filename}:{line}: doctest assigns os.environ[{key!r}] without a matching os.environ.pop/setdefault in the "
        "same docstring; restore it so later tests in the worker do not inherit it"
        for line, docstring in _docstrings(source)
        for key in sorted(unrestored_keys(docstring))
    ]


def _is_scanned(path: Path, root: Path) -> bool:
    """Return whether ``path`` is a Python source whose doctests this repository executes."""
    try:
        parts = path.resolve().relative_to(root.resolve()).parts
    except ValueError:
        parts = path.parts
    return path.suffix == ".py" and EXCLUDED_PARTS.isdisjoint(parts)


def _default_files(root: Path) -> list[Path]:
    """List every Python source below the scanned roots of ``root``."""
    return [path for scanned in SCANNED_ROOTS for path in sorted((root / scanned).rglob("*.py"))]


def check_files(paths: Iterable[Path], root: Path) -> list[str]:
    """Check every scanned path and return the findings in input order."""
    findings: list[str] = []
    for path in paths:
        if _is_scanned(path, root):
            name = path.resolve().relative_to(root.resolve()).as_posix() if path.is_absolute() else path.as_posix()
            findings.extend(check_source(path.read_text(encoding="utf-8"), name))
    return findings


def main(argv: list[str] | None = None) -> int:
    """Check the given files (or the whole repository) and print findings; exit 1 when any exist."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="repository root (default: current directory)")
    parser.add_argument("paths", nargs="*", type=Path, help="Python files to check; default: all scanned sources")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    paths = [path if path.is_absolute() else root / path for path in args.paths] or _default_files(root)
    findings = check_files(paths, root)
    for finding in findings:
        print(finding)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
