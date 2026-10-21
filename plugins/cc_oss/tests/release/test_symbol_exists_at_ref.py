"""Direct tests for ``bin/symbol_exists_at_ref.py``, the release truth-check binding probe.

The release skill's behavioural cases (docstring-only names, PEP 263 sources, dynamic bindings) run through the
instruction blocks in ``test_release_truth_waiver_contract.py``. These tests cover what those cannot reach: argument
handling, the path guard, and the ``git``/parser failure paths, all of which must stay "inconclusive" (exit 2).
"""

import subprocess
from pathlib import Path
from unittest import mock

import pytest
import symbol_exists_at_ref as sear


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return an empty git repository that is the current directory, as when the skill runs from the repo root."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _commit(repo: Path, name: str, source: bytes) -> None:
    """Commit ``source`` bytes as ``name`` so ``git show HEAD:name`` returns them unchanged."""
    (repo / name).write_bytes(source)
    subprocess.run(["git", "add", name], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "c"],
        cwd=repo,
        check=True,
        capture_output=True,
    )


@pytest.mark.parametrize(
    ("source", "args", "expected"),
    [
        pytest.param(b"def Legacy():\n    pass\n", ["HEAD", "Legacy", "api.py"], 0, id="function-declared"),
        pytest.param(
            b"parser.add_argument('--legacy')\n", ["HEAD", "--legacy", "api.py"], 0, id="option-like-symbol-is-a-name"
        ),
        pytest.param(
            b"parser.add_argument('--help')\n", ["HEAD", "--help", "api.py"], 0, id="help-like-symbol-is-a-name"
        ),
        pytest.param(b"def Legacy():\n    pass\n", ["HEAD", "Other", "api.py"], 2, id="other-name-inconclusive"),
        pytest.param(b"def Legacy():\n    pass\n", ["no-such-ref", "Legacy", "api.py"], 2, id="unknown-ref"),
        pytest.param(b"def Legacy():\n    pass\n", ["HEAD", "Legacy", "missing.py"], 2, id="missing-file"),
        pytest.param(b"x = 1\x00\n", ["HEAD", "x", "api.py"], 2, id="null-byte-source-unparsable"),
        pytest.param(b"def broken(:\n", ["HEAD", "broken", "api.py"], 2, id="syntax-error"),
    ],
)
def test_main_exit_status(repo: Path, source: bytes, args: list[str], expected: int) -> None:
    """Exit 0 means a candidate binding was read; every other outcome is the inconclusive status 2.

    A committed ``api.py`` is probed through the CLI entry point. An option-shaped symbol such as ``--legacy`` must
    reach the probe as a name (the parser-flag case), and a git or parse failure must never surface as a crash or as
    proof of absence.
    """
    _commit(repo, "api.py", source)

    assert sear.main(args) == expected


@pytest.mark.parametrize("flag", [pytest.param("--help", id="long"), pytest.param("-h", id="short")])
def test_lone_help_flag_prints_usage_and_exits_zero(flag: str, capsys: pytest.CaptureFixture[str]) -> None:
    """A single ``--help``/``-h`` argument prints the usage line and exits 0 like every other ``bin/`` script.

    Only the lone flag is help: with three arguments the same text is a symbol name (see ``test_main_exit_status``).
    """
    with pytest.raises(SystemExit) as exc:
        sear.main([flag])

    assert exc.value.code == 0
    assert "usage:" in capsys.readouterr().out


@pytest.mark.parametrize(
    "args",
    [
        pytest.param([], id="no-arguments"),
        pytest.param(["HEAD", "Legacy"], id="missing-path"),
        pytest.param(["HEAD", "Legacy", "api.py", "extra"], id="extra-argument"),
    ],
)
def test_wrong_argument_count_is_inconclusive(args: list[str]) -> None:
    """A call with other than three positional arguments reports inconclusive instead of raising."""
    assert sear.main(args) == sear.EXIT_INCONCLUSIVE


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("/abs/api.py", id="absolute"),
        pytest.param("../api.py", id="parent-segment"),
        pytest.param("pkg/../../api.py", id="nested-parent-segment"),
        pytest.param("pkg/api.txt", id="not-python"),
        pytest.param("", id="empty"),
    ],
)
def test_invalid_path_is_rejected_before_git_runs(path: str) -> None:
    """A path outside the repository-relative ``.py`` shape is inconclusive without ever calling ``git``.

    The release skill passes an empty ``PUBLIC_PATH`` when the claimed module is unknown; that and any escaping path
    must not be turned into a ``git show`` of an arbitrary location.
    """
    with mock.patch.object(sear.subprocess, "run", side_effect=AssertionError("git must not run")):
        assert sear.probe("HEAD", "Legacy", path) == sear.EXIT_INCONCLUSIVE


def test_missing_git_binary_is_inconclusive() -> None:
    """A host without ``git`` on PATH reports inconclusive rather than crashing the truth check."""
    with mock.patch.object(sear.subprocess, "run", side_effect=FileNotFoundError("git")):
        assert sear.probe("HEAD", "Legacy", "api.py") == sear.EXIT_INCONCLUSIVE
