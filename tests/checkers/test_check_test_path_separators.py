"""Contract: the ``test-path-separator-mixture`` hook flags assertions mixing native and ``/``-joined paths.

A path rendered with ``f"{tmp_path / 'x'}"`` is ``\\``-joined on Windows; beside a ``./pkg/x.py`` literal in the same
assertion it agrees with POSIX output everywhere except on Windows CI, where the test then fails.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from _repo_checker_run import run_checker

ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = ROOT / "scripts" / "check_test_path_separators.py"


def _run(tmp_path: Path, source: str) -> subprocess.CompletedProcess[str]:
    """Write ``source`` as a test module and run the checker on it as the hook does."""
    module = tmp_path / "test_sample.py"
    module.write_text(source, encoding="utf-8", newline="\n")
    return run_checker(_SCRIPT, str(module))


@pytest.mark.parametrize(
    ("source", "expected_rc"),
    [
        pytest.param('assert hits == [f"{tmp_path / \'m.py\'}:1", "./pkg/a.py:2"]\n', 1, id="mixed-forms"),
        pytest.param('assert hits == [str(tmp_path / "m.py"), Path("a").as_posix()]\n', 1, id="str-beside-as-posix"),
        pytest.param("assert hits == [f\"{tmp_path / 'm.py'}:1:Base\"]\n", 0, id="native-only"),
        pytest.param('assert hits == ["./pkg/a.py:2"]\n', 0, id="posix-only"),
        pytest.param("assert found == tmp_path / 'pkg/a.py'\n", 0, id="path-objects"),
        pytest.param('assert hits == [f"{t / \'m\'}", "./a.py"]  # path-sep: native\n', 0, id="opted-out"),
    ],
)
def test_checker_flags_only_mixed_separator_assertions(tmp_path: Path, source: str, expected_rc: int) -> None:
    """Only an assertion carrying both forms fails; consistent forms and ``Path`` comparisons pass.

    Each source is one assertion run through the real checker command line. Consistent native rendering agrees with
    itself on every OS, so flagging it would only add noise.
    """
    result = _run(tmp_path, source)

    assert result.returncode == expected_rc, result.stdout + result.stderr
