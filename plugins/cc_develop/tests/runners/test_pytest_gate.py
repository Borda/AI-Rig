"""Tests for ``bin/pytest_gate.py``.

The script validates ``PYTEST_CMD`` against an allowlist (``pytest``, ``uv run pytest``, ``python -m pytest``), then
execs pytest with ``--tb=short -v``. ``subprocess.run`` is mocked so no real test runs; ``which`` is mocked so
``_resolve`` succeeds without the binary present.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

import pytest
import pytest_gate  # type: ignore[import-not-found]

_RUNNER_DETECTION = Path(__file__).parents[2] / "skills" / "_shared" / "runner-detection.md"


def _runner_detection_pytest_cmds() -> list[str]:
    """Extract every pytest-invoking runner ``runner-detection.md`` can emit.

    Returns:
        Deduplicated list of ``TEST_CMD``/``PYTEST_CMD`` string values that contain
        ``pytest`` — these are exactly the commands that reach the gate allowlist.

    Examples:
        >>> "python -m pytest" in _runner_detection_pytest_cmds()
        True
    """
    text = _RUNNER_DETECTION.read_text(encoding="utf-8")
    values = re.findall(r'(?:TEST_CMD|PYTEST_CMD)="([^"]+)"', text)
    return sorted({v for v in values if "pytest" in v})


@pytest.fixture
def pytest_run() -> Iterator[mock.MagicMock]:
    """Mock executable lookup and ``subprocess.run`` inside the script; the fake pytest exits 0 unless told
    otherwise."""
    with (
        mock.patch("pytest_gate.which", autospec=True, side_effect=lambda name: f"/fake/bin/{name}"),
        mock.patch("pytest_gate.subprocess.run", autospec=True) as run,
    ):
        run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        yield run


@pytest.fixture
def project_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run from ``<tmp>/project``; the sibling ``<tmp>/outside`` directory exists and lies outside the cwd."""
    project = tmp_path / "project"
    project.mkdir()
    (tmp_path / "outside").mkdir()
    monkeypatch.chdir(project)
    return project


def _delegated_argv(pytest_run: mock.MagicMock) -> list[list[str]]:
    """Return the argv of every pytest run the gate started."""
    return [call.args[0] for call in pytest_run.call_args_list]


def test_runner_detection_outputs_are_allowlisted() -> None:
    """Every pytest runner ``runner-detection.md`` emits must pass the gate allowlist.

    The gate and the detector are maintained separately; a new detected runner that the allowlist lacks would make the
    gate reject the project's own test command.
    """
    emitted = _runner_detection_pytest_cmds()
    assert emitted, "runner-detection.md yielded no pytest commands — parse regression"
    missing = [cmd for cmd in emitted if cmd not in pytest_gate._PYTEST_ALLOWLIST]
    assert not missing, f"runner-detection emits commands not in allowlist: {missing}"


class TestAllowlistedCommand:
    """An allowlisted runner is resolved to an absolute executable and run with short tracebacks."""

    @pytest.mark.parametrize(
        ("argv", "expected"),
        [
            pytest.param(
                ["poetry run pytest", "tests/"],
                ["/fake/bin/poetry", "run", "pytest", "--tb=short", "tests/", "-v"],
                id="poetry-run",
            ),
            pytest.param(
                ["pytest", "tests/foo.py::test_bar"],
                ["/fake/bin/pytest", "--tb=short", "tests/foo.py::test_bar", "-v"],
                id="golden-call-site-node-id",
            ),
            pytest.param(
                ["uv run pytest", "tests/"],
                ["/fake/bin/uv", "run", "pytest", "--tb=short", "tests/", "-v"],
                id="uv-run",
            ),
            pytest.param(
                ["python -m pytest", "tests/foo.py"],
                ["/fake/bin/python", "-m", "pytest", "--tb=short", "tests/foo.py", "-v"],
                id="python-m-pytest",
            ),
            pytest.param([], ["/fake/bin/pytest", "--tb=short", ".", "-v"], id="defaults-without-arguments"),
        ],
    )
    def test_resolves_full_argv(self, pytest_run: mock.MagicMock, argv: list[str], expected: list[str]) -> None:
        """Accept an allowlisted test command and pass its fully resolved arguments to the subprocess.

        Poetry-, uv- and module-based commands are split into executable and subcommand tokens with the executable
        resolved; the golden call site ``pytest_gate.py "$PYTEST_CMD" <node_id>`` keeps its argv shape, and no arguments
        at all run bare ``pytest`` over ``.`` with short tracebacks and verbose output.
        """
        rc = pytest_gate.main(argv)
        assert (rc, _delegated_argv(pytest_run)) == (0, [expected])

    @pytest.mark.parametrize(
        ("returncode", "argv"),
        [
            pytest.param(1, ["pytest", "tests/"], id="test-failure-exit-1"),
            pytest.param(5, ["pytest"], id="no-tests-collected-exit-5"),
        ],
    )
    def test_passes_through_exit_code(self, pytest_run: mock.MagicMock, returncode: int, argv: list[str]) -> None:
        """Pytest's own exit status (1 for failures, 5 for no tests collected) is returned by ``main`` unchanged.

        The fix flow keys its reproduction verdicts on these exact codes, so the gate must not remap them.
        """
        pytest_run.return_value = subprocess.CompletedProcess(args=[], returncode=returncode)
        assert pytest_gate.main(argv) == returncode

    def test_output_streams_are_inherited(self, pytest_run: mock.MagicMock) -> None:
        """Inherits stdout/stderr — no capture flags passed, so full pytest output streams through.

        Capturing would hide a long run's progress from the user until the whole run finished.
        """
        pytest_gate.main(["pytest"])
        kwargs = pytest_run.call_args.kwargs
        stream_args = (kwargs.get("capture_output"), kwargs.get("stdout"), kwargs.get("stderr"))
        assert stream_args in {(None, None, None), (False, None, None)}

    def test_help_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Print usage and exit 0 before any pytest run.

        ``--help`` is the only option argparse owns; it needs neither the allowlist nor an executable lookup.
        """
        with pytest.raises(SystemExit) as exc:
            pytest_gate.main(["--help"])
        assert (exc.value.code, "pytest_gate.py" in capsys.readouterr().out) == (0, True)


class TestRejection:
    """Unsafe runner commands and out-of-project targets never reach pytest."""

    @pytest.mark.parametrize(
        "command",
        [
            "rm -rf /",
            "pytest; rm -rf /",
            "pytest && echo x",
            "uv run pytest; echo x",
            "python -m pytest -q",
            pytest.param("python3 -c 'os.system(\"x\")'", id="shell-injection-payload"),
        ],
    )
    def test_non_allowlisted_command_exits_2(
        self, pytest_run: mock.MagicMock, capsys: pytest.CaptureFixture[str], command: str
    ) -> None:
        """Non-allowlisted cmd → exit 2; subprocess never invoked; stderr contains "rejected".

        Covers chained shell commands, extra pytest flags outside the allowlist and a shell-injection-style payload.
        """
        rc = pytest_gate.main([command, "."])
        assert (rc, pytest_run.called, "rejected" in capsys.readouterr().err) == (2, False, True)

    @pytest.mark.parametrize(
        "target",
        [
            pytest.param("-p", id="plugin-flag"),
            pytest.param("--rootdir=/etc", id="rootdir-option"),
            pytest.param("--basetemp=x::y", id="option-with-node-id"),
        ],
    )
    def test_option_shaped_target_exits_1(
        self, pytest_run: mock.MagicMock, capsys: pytest.CaptureFixture[str], target: str
    ) -> None:
        """A target starting with ``-`` exits 1 before pytest starts, even though it resolves under the working
        directory.

        ``Path("-p")`` resolves to ``<cwd>/-p``, so the containment check alone passed it straight into pytest's argv.
        """
        with pytest.raises(SystemExit) as exc:
            pytest_gate.main(["pytest", target])
        assert (exc.value.code, pytest_run.called, "option-shaped target" in capsys.readouterr().err) == (
            1,
            False,
            True,
        )

    @pytest.mark.parametrize(
        "target",
        [
            pytest.param("{outside}", id="absolute-path"),
            pytest.param("../outside", id="relative-parent"),
        ],
    )
    def test_target_outside_cwd_exits_1(
        self, project_dir: Path, pytest_run: mock.MagicMock, capsys: pytest.CaptureFixture[str], target: str
    ) -> None:
        """Resolved target path outside ``Path.cwd()`` → exit 1; pytest never invoked.

        Relative traversal targets are rejected after resolution, just like absolute ones.
        """
        outside = project_dir.parent / "outside"
        with pytest.raises(SystemExit) as exc:
            pytest_gate.main(["pytest", target.format(outside=outside)])
        assert (exc.value.code, pytest_run.called, "outside project directory" in capsys.readouterr().err) == (
            1,
            False,
            True,
        )

    def test_symlink_target_outside_cwd_exits_1(
        self, project_dir: Path, pytest_run: mock.MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Symlink targets are rejected based on their resolved location.

        The link sits inside the project, so only resolving it before the containment check exposes the escape.
        """
        (project_dir / "linked").symlink_to(project_dir.parent / "outside", target_is_directory=True)
        with pytest.raises(SystemExit) as exc:
            pytest_gate.main(["pytest", "linked"])
        assert (exc.value.code, pytest_run.called, "outside project directory" in capsys.readouterr().err) == (
            1,
            False,
            True,
        )
