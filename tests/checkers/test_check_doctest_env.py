"""Contract: the ``check-doctest-env`` hook flags doctests that assign ``os.environ`` without restoring it.

A doctest shares its pytest worker with every later test, so an unrestored assignment is inherited by whichever test
runs next; the hook reports it at the docstring that made the assignment.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from _repo_checker_run import run_checker

ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = ROOT / "scripts" / "check_doctest_env.py"

_LEAK = 'def f():\n    """Doc.\n\n    >>> os.environ["TMPDIR"] = "/tmp"\n    """\n'
_POPPED = 'def f():\n    """Doc.\n\n    >>> os.environ["A"] = "1"\n    >>> os.environ.pop("A")\n    """\n'
_SETDEFAULT = (
    'def f():\n    """Doc.\n\n    >>> os.environ["A"] = "1"\n    >>> os.environ.setdefault("A", "0")\n    """\n'
)
_OTHER_KEY_POPPED = 'def f():\n    """Doc.\n\n    >>> os.environ["A"] = "1"\n    >>> os.environ.pop("B")\n    """\n'
_NO_ENV = 'def f():\n    """Doc.\n\n    >>> 1 + 1\n    2\n    """\n'
_NEWER_SYNTAX = "def broken(:\n"


def _run(root: Path, relative: str, source: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Write ``source`` at ``root / relative`` and run the checker under ``root`` as the hook does."""
    module = root / relative
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text(source, encoding="utf-8", newline="\n")
    return run_checker(_SCRIPT, "--root", str(root), *args)


@pytest.mark.parametrize(
    ("source", "expected_rc"),
    [
        pytest.param(_LEAK, 1, id="assignment-never-restored"),
        pytest.param(_OTHER_KEY_POPPED, 1, id="other-key-popped"),
        pytest.param(_POPPED, 0, id="assignment-popped"),
        pytest.param(_SETDEFAULT, 0, id="assignment-setdefault"),
        pytest.param(_NO_ENV, 0, id="no-environment-access"),
        pytest.param(_NEWER_SYNTAX, 0, id="unparsable-source"),
    ],
)
def test_checker_flags_only_unrestored_assignments(tmp_path: Path, source: str, expected_rc: int) -> None:
    """Only an assignment without a matching ``pop``/``setdefault`` of the same key fails the scan.

    Each source is one module under ``plugins/`` found by the default scan, run through the real command line.
    """
    result = _run(tmp_path, "plugins/demo/mod.py", source)

    assert result.returncode == expected_rc, result.stdout + result.stderr


def test_finding_names_file_line_and_key(tmp_path: Path) -> None:
    """A finding reads ``path:line: ...`` and names the leaked key so the fix is obvious."""
    result = _run(tmp_path, "tests/test_demo.py", _LEAK)

    assert result.returncode == 1
    assert result.stdout.startswith("tests/test_demo.py:1: doctest assigns os.environ['TMPDIR']"), result.stdout


@pytest.mark.parametrize(
    "relative",
    [
        pytest.param("plugins/demo/results/mod.py", id="results-tree"),
        pytest.param("plugins/demo/.venv/mod.py", id="virtualenv"),
        pytest.param("plugins/demo/node_modules/mod.py", id="node-modules"),
        pytest.param("other/mod.py", id="unscanned-root"),
    ],
)
def test_vendored_and_unscanned_trees_are_skipped(tmp_path: Path, relative: str) -> None:
    """Sources outside the executed doctest trees never fail the scan, whatever they assign."""
    result = _run(tmp_path, relative, _LEAK)

    assert result.returncode == 0, result.stdout + result.stderr


def test_explicit_file_is_checked_instead_of_the_whole_tree(tmp_path: Path) -> None:
    """Files named on the command line, as pre-commit passes them, are the only ones scanned."""
    (tmp_path / "plugins/demo").mkdir(parents=True)
    (tmp_path / "plugins/demo/leaky.py").write_text(_LEAK, encoding="utf-8", newline="\n")

    result = _run(tmp_path, "plugins/demo/clean.py", _NO_ENV, "plugins/demo/clean.py")

    assert result.returncode == 0, result.stdout + result.stderr
