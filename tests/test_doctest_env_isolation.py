"""Contract: a doctest that mutates ``os.environ`` must undo the mutation in the same docstring.

Doctests share the interpreter with every other test in their pytest worker, so an unrestored ``os.environ`` assignment
outlives the example that made it. With ``pytest-randomly`` deciding order, the victim and the symptom both change from
run to run, which reads as flakiness rather than as the ordering defect it is.

The observed case: a ``cc_oss`` doctest planted the POSIX literal ``/tmp`` in ``TMPDIR`` (unset on Windows, so the
fallback applied), and a later ``codemap-py`` doctest resolved that value against the current drive, compared its own
temp file against ``D:\\tmp``, and failed.
"""

from __future__ import annotations

import ast
import doctest
import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]

#: Directories holding vendored copies, captured benchmark evidence, or build output — their
#: doctests are not executed by this repository's pytest run, so their hygiene is not ours.
_EXCLUDED_PARTS = frozenset(
    {".cache", ".git", ".sandbox", ".reports", ".temp", ".venv", "__pycache__", "node_modules", "results"}
)

#: Source trees whose doctests this repository does execute.
_SCANNED_ROOTS = ("plugins", "benchmarks", "tests")

_ASSIGN_RE = re.compile(r"""os\.environ\[\s*["'](?P<key>[^"']+)["']\s*\]\s*=""")
_REMOVE_RE = re.compile(r"""os\.environ\.(?:pop|setdefault)\(\s*["'](?P<key>[^"']+)["']""")


def _python_sources() -> list[Path]:
    """Return every Python file whose doctests run in this repository's pytest session."""
    files: list[Path] = []
    for scanned in _SCANNED_ROOTS:
        for path in sorted((ROOT / scanned).rglob("*.py")):
            if _EXCLUDED_PARTS.isdisjoint(path.parts):
                files.append(path)
    return files


def _docstrings(source: str, path: Path) -> list[str]:
    """Return every docstring in ``source`` — module, class, function, and method alike.

    A file whose syntax the running interpreter rejects is skipped rather than failed: pytest cannot import it here
    either, so it has no doctests to leak on this version. The frozen grammar corpus under ``codemap-py`` is the
    standing case — it carries post-3.11 syntax on purpose and its ``conftest.py`` keeps the collector away from it.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        pytest.skip(f"{path.relative_to(ROOT)}: not parseable on this interpreter ({exc.msg})")
    nodes = [tree, *(n for n in ast.walk(tree) if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)))]
    return [text for node in nodes if (text := ast.get_docstring(node, clean=False))]


def _unrestored_keys(docstring: str) -> set[str]:
    """Return environment keys the docstring's doctest assigns but never pops or restores."""
    example_source = "".join(example.source for example in doctest.DocTestParser().get_examples(docstring))
    assigned = {m.group("key") for m in _ASSIGN_RE.finditer(example_source)}
    removed = {m.group("key") for m in _REMOVE_RE.finditer(example_source)}
    return assigned - removed


@pytest.mark.parametrize("path", [pytest.param(p, id=str(p.relative_to(ROOT))) for p in _python_sources()])
def test_doctests_leave_the_environment_unchanged(path: Path) -> None:
    """No doctest assigns an environment variable it does not also pop or restore.

    An assignment paired with a ``pop``/``setdefault`` of the same key is the accepted save-and-restore idiom; an
    assignment alone is the defect, because the next test in the worker inherits it.
    """
    docstrings = _docstrings(path.read_text(encoding="utf-8"), path)
    leaked = {key for docstring in docstrings for key in _unrestored_keys(docstring)}
    assert not leaked, f"{path.relative_to(ROOT)}: doctest leaks os.environ {sorted(leaked)} into later tests"
