"""Exercise target and artifact boundaries before a test subprocess launches."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest import mock

import dev_run_dir
import dev_run_tests
import pytest
import run_pytest_short


def _symlinks_supported() -> bool:
    """Probe whether this process can create a symlink on the current host."""
    try:
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "link").symlink_to(Path(directory))
    except OSError:
        return False
    return True


@pytest.fixture
def runner_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate runner state and make the scratch directory the project root."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "runner-boundaries")
    dev_run_dir.write_sentinel("dev-pytest-cmd", "pytest")
    monkeypatch.setattr(dev_run_tests, "which", lambda name: sys.executable)
    return tmp_path


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param(["-p", "hostile_plugin"], id="plugin-import"),
        pytest.param(["../outside/test_other.py"], id="outside-target"),
        pytest.param(["--basetemp=../outside"], id="outside-basetemp"),
        pytest.param(["--rootdir=../outside"], id="outside-rootdir"),
        pytest.param(["--cov=../outside"], id="outside-coverage-source"),
        pytest.param(["--cov-report=html:../outside"], id="outside-coverage-report"),
        pytest.param(["@pytest-arguments.txt"], id="response-file-options"),
    ],
)
@mock.patch("dev_run_tests.run_tail", autospec=True, return_value=(0, ["1 passed"]))
def test_refuses_unsafe_pytest_arguments(mock_run_tail, runner_session: Path, extra: list[str]) -> None:
    """Unsafe plugin, option and target arguments never reach the child runner."""
    assert dev_run_tests.main(["pytest", "--tail", "2", "--", *extra]) == dev_run_tests.EXIT_NO_RUN
    mock_run_tail.assert_not_called()


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param(["--collect-only", "--doctest-modules", "src/engine.py", "-q"], id="feature-collection"),
        pytest.param(["--tb=long", "tests/test_engine.py::test_run", "-v"], id="fix-reproduction"),
        pytest.param(["--co", "-q", "--cov=."], id="refactor-collection"),
        pytest.param(["--cov=engine.core", "-q", "--cov-report=term-missing"], id="refactor-coverage"),
        pytest.param(["-p", "no:cacheprovider", "tests/test_engine.py"], id="disable-cache-plugin"),
    ],
)
@mock.patch("dev_run_tests.run_tail", autospec=True, return_value=(0, ["1 passed"]))
def test_preserves_skill_pytest_arguments(mock_run_tail, runner_session: Path, extra: list[str]) -> None:
    """Existing skill argument shapes retain their exact order and values."""
    assert dev_run_tests.main(["pytest", "--tail", "2", "--", *extra]) == 0
    assert mock_run_tail.call_args.args[0][1:] == extra


@mock.patch("run_pytest_short._execute", autospec=True, return_value=0)
def test_refuses_response_file_target(mock_execute, runner_session: Path) -> None:
    """A response file cannot hide unvalidated pytest options behind a project-local path."""
    with pytest.raises(SystemExit) as exc:
        run_pytest_short.main(["pytest", "@pytest-arguments.txt"])
    assert exc.value.code == 64
    mock_execute.assert_not_called()


@pytest.mark.parametrize("destination", ["project.py", "scratch.log"])
@mock.patch("run_pytest_short._execute", autospec=True, return_value=0)
def test_refuses_log_outside_run_directory(mock_execute, runner_session: Path, destination: str) -> None:
    """Neither project files nor arbitrary temp files are valid log destinations."""
    log = runner_session / destination
    log.write_text("keep", encoding="utf-8")
    assert run_pytest_short.main(["pytest", "--log", str(log)]) == 64
    assert log.read_text(encoding="utf-8") == "keep"
    mock_execute.assert_not_called()


@mock.patch("run_pytest_short._execute", autospec=True, return_value=0)
def test_accepts_absolute_session_run_after_cwd_change(mock_execute, runner_session: Path, monkeypatch) -> None:
    """The original run directory remains valid after entering a worktree."""
    run = runner_session / ".developments" / "run"
    run.mkdir(parents=True)
    dev_run_dir.write_sentinel("dev-feature-dev-dir", str(run))
    worktree = runner_session / "worktree"
    worktree.mkdir()
    monkeypatch.chdir(worktree)
    monkeypatch.setattr(run_pytest_short, "which", lambda name: sys.executable)
    log = run / "pytest.log"
    assert run_pytest_short.main(["pytest", "--log", str(log)]) == 0
    assert mock_execute.call_args.args[-1] == str(log)


@pytest.mark.skipif(not _symlinks_supported(), reason="symlink creation unavailable on this host")
@mock.patch("run_pytest_short._execute", autospec=True, return_value=0)
def test_refuses_log_symlink_outside_run_directory(mock_execute, runner_session: Path) -> None:
    """A log symlink cannot redirect a valid run path onto a project source file."""
    run = runner_session / ".developments" / "run"
    run.mkdir(parents=True)
    dev_run_dir.write_sentinel("dev-feature-dev-dir", str(run))
    source = runner_session / "source.py"
    source.write_text("keep", encoding="utf-8")
    log = run / "pytest.log"
    log.symlink_to(source)
    assert run_pytest_short.main(["pytest", "--log", str(log)]) == 64
    assert source.read_text(encoding="utf-8") == "keep"
    mock_execute.assert_not_called()


@mock.patch("run_pytest_short._execute", autospec=True, return_value=0)
def test_active_run_excludes_other_development_logs(mock_execute, runner_session: Path) -> None:
    """The standalone fallback does not broaden an active session's dedicated run root."""
    run = runner_session / ".developments" / "active"
    dev_run_dir.write_sentinel("dev-feature-dev-dir", str(run))
    log = runner_session / ".developments" / "unrelated" / "pytest.log"
    assert run_pytest_short.main(["pytest", "--log", str(log)]) == 64
    mock_execute.assert_not_called()


@pytest.mark.integration
def test_real_pytest_accepts_checked_skill_arguments(runner_session: Path, capsys) -> None:
    """A real project-local test still runs through the checked sentinel command and safe flags."""
    test = runner_session / "test_example.py"
    test.write_text("def test_example():\n    assert 1 + 1 == 2\n", encoding="utf-8")
    dev_run_dir.write_sentinel("dev-pytest-cmd", "python -m pytest")
    args = ["--tb=short", str(test), "-q", "-p", "no:cacheprovider", "-p", "no:xdist"]
    assert dev_run_tests.main(["pytest", "--tail", "4", "--", *args]) == 0
    assert "1 passed" in capsys.readouterr().out
