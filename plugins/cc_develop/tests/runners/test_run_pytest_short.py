"""Tests for ``bin/run_pytest_short.py``.

The script validates ``PYTEST_CMD`` against the same allowlist as ``pytest_gate``, captures combined stdout+stderr from
pytest via incremental ``Popen`` reads (byte-capped at ``_MAX_OUTPUT_BYTES``), then prints only the last ``tail_n``
lines (default 20). Bad ``tail_n`` falls back to 20. ``subprocess.Popen`` and ``shutil.which`` are replaced with
autospecced fakes so no real pytest runs: the wrapper's argv checks and tailing are under test, not pytest itself.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from shutil import which
from typing import Any, NamedTuple
from unittest import mock

import dev_run_dir
import pytest
import run_pytest_short  # type: ignore[import-not-found]


class _FakePopen:
    """Stand-in for ``subprocess.Popen`` exposing a readable ``stdout`` stream."""

    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        """Provide a return code and readable output stream for the runner."""
        self.returncode = returncode
        self.stdout = io.StringIO(stdout)

    def wait(self, timeout: float | None = None) -> int:
        """Return stored returncode; accepts optional timeout kwarg to match real Popen."""
        return self.returncode


class _Workspace(NamedTuple):
    """Scratch layout used by the path-containment tests."""

    project: Path
    scratch: Path
    outside: Path


@pytest.fixture
def fake_popen(monkeypatch: pytest.MonkeyPatch) -> Callable[..., mock.MagicMock]:
    """Return a factory that fakes ``Popen`` and ``which`` and hands back the autospecced ``Popen`` mock."""

    def configure(returncode: int = 0, stdout: str = "") -> mock.MagicMock:
        popen = mock.create_autospec(subprocess.Popen, side_effect=lambda *_, **__: _FakePopen(returncode, stdout))
        monkeypatch.setattr(run_pytest_short.subprocess, "Popen", popen)
        monkeypatch.setattr(
            run_pytest_short, "which", mock.create_autospec(which, side_effect=lambda name: f"/fake/{name}")
        )
        return popen

    return configure


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Workspace:
    """Project dir (current directory), a separate ``$TMPDIR`` scratch dir, and a sibling ``outside/leak`` dir."""
    project, scratch, outside = tmp_path / "project", tmp_path / "scratch", tmp_path / "outside"
    for directory in (project, scratch, outside / "leak"):
        directory.mkdir(parents=True)
    monkeypatch.chdir(project)
    monkeypatch.setenv("TMPDIR", str(scratch))
    return _Workspace(project, scratch, outside)


def _make_lines(n: int) -> str:
    """Build a string with ``n`` numbered lines (``line-1`` … ``line-n``).

    Examples:
        >>> _make_lines(3)
        'line-1\\nline-2\\nline-3'
    """
    return "\n".join(f"line-{i + 1}" for i in range(n))


def _line_range(first: int, last: int) -> list[str]:
    """Return the expected printed lines ``line-<first>`` … ``line-<last>``.

    Examples:
        >>> _line_range(2, 3)
        ['line-2', 'line-3']
    """
    return [f"line-{i}" for i in range(first, last + 1)]


@pytest.mark.parametrize(
    ("line_count", "extra_args", "expected"),
    [
        pytest.param(30, [], _line_range(11, 30), id="default-tail-20"),
        pytest.param(30, ["--tail-n", "5"], _line_range(26, 30), id="custom-tail-5"),
        pytest.param(25, ["--tail-n", "abc"], _line_range(6, 25), id="bad-tail-n-non-numeric-falls-back-to-20"),
        pytest.param(25, ["--tail-n", "-1"], _line_range(6, 25), id="bad-tail-n-negative-falls-back-to-20"),
        pytest.param(3, ["--tail-n", "100"], ["line-1", "line-2", "line-3"], id="tail-larger-than-output-prints-all"),
        pytest.param(3, ["--tail-n", "0"], [], id="zero-lines"),
        pytest.param(3, ["--tail-n", "1"], ["line-3"], id="last-line"),
    ],
)
def test_tail_n_selects_trailing_lines(
    fake_popen: Callable[..., mock.MagicMock],
    capsys: pytest.CaptureFixture[str],
    line_count: int,
    extra_args: list[str],
    expected: list[str],
) -> None:
    """Only the requested number of trailing output lines is printed, and the exit code is 0.

    The default tail is 20 lines, ``--tail-n`` selects another count (0 prints nothing, a count above the output length
    prints everything), and a non-numeric or negative ``--tail-n`` silently falls back to the default 20.
    """
    fake_popen(stdout=_make_lines(line_count))
    rc = run_pytest_short.main(["pytest", ".", *extra_args])
    assert rc == 0
    assert capsys.readouterr().out.splitlines() == expected


def test_passes_through_exit_code(fake_popen: Callable[..., mock.MagicMock]) -> None:
    """Pytest exits 4 → ``main`` returns 4 unchanged, so callers can tell usage errors from test failures."""
    fake_popen(returncode=4, stdout=_make_lines(5))
    assert run_pytest_short.main(["pytest"]) == 4


@pytest.mark.integration
@pytest.mark.parametrize(
    ("output_cap", "returncode"),
    [pytest.param(100, 0, id="complete-output"), pytest.param(2, 4, id="truncated-output")],
)
def test_closes_real_child_output_pipe(monkeypatch: pytest.MonkeyPatch, output_cap: int, returncode: int) -> None:
    """Close the child's pipe after reading both complete and truncated output.

    A real child supplies the pipe while only command selection is replaced. Keeping the child alive as an object makes
    its unclosed stream observable before garbage collection; the context manager cleans it up on failure.
    """
    with subprocess.Popen(
        [sys.executable, "-c", f"print('child output'); raise SystemExit({returncode})"],
        stdout=subprocess.PIPE,
        text=True,
    ) as child:
        monkeypatch.setattr(
            run_pytest_short.subprocess, "Popen", mock.create_autospec(subprocess.Popen, return_value=child)
        )
        monkeypatch.setattr(run_pytest_short, "which", mock.create_autospec(which, return_value=sys.executable))
        monkeypatch.setattr(run_pytest_short, "_MAX_OUTPUT_BYTES", output_cap)

        assert run_pytest_short.main(["python -m pytest"]) == returncode
        assert child.stdout is not None
        assert child.stdout.closed


@pytest.mark.parametrize(
    "command", ["rm -rf /", "pytest; rm -rf /", "pytest && echo x", "uv run pytest; echo x", "python -m pytest -q"]
)
def test_rejects_unsafe_cmd(
    fake_popen: Callable[..., mock.MagicMock], capsys: pytest.CaptureFixture[str], command: str
) -> None:
    """Non-allowlisted cmd → exit 2; subprocess never invoked; stderr contains "rejected".

    The command string comes from the environment, so shell metacharacters and non-pytest launchers must be refused
    before anything is spawned.
    """
    popen = fake_popen()
    rc = run_pytest_short.main([command, "."])
    assert rc == 2
    popen.assert_not_called()
    assert "rejected" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("argv", "expected_cmd"),
    [
        # ``dev_test_targets.py --run`` hands over its whole selection; one process per file would re-pay start-up
        pytest.param(
            ["pytest", "tests/test_a.py", "tests/test_b.py"],
            ["/fake/pytest", "--tb=short", "tests/test_a.py", "tests/test_b.py", "-v"],
            id="several-targets-one-process",
        ),
        # ``--node-id`` was removed (zero call sites); node ids are ordinary positional targets
        pytest.param(
            ["pytest", "tests/test_a.py::test_x"],
            ["/fake/pytest", "--tb=short", "tests/test_a.py::test_x", "-v"],
            id="node-id-is-a-positional-target",
        ),
        pytest.param(
            ["uv run pytest", "tests/"],
            ["/fake/uv", "run", "pytest", "--tb=short", "tests/", "-v"],
            id="resolves-first-token-only",
        ),
    ],
)
def test_builds_one_pytest_invocation(
    fake_popen: Callable[..., mock.MagicMock], argv: list[str], expected_cmd: list[str]
) -> None:
    """Targets reach a single pytest invocation in order, and only the executable token is resolved.

    A targeted selection must cost one pytest run, targeted node ids pass through untouched, and in a multi-token
    command such as ``uv run pytest`` only the leading executable is resolved to an absolute path.
    """
    popen = fake_popen()
    run_pytest_short.main(argv)
    popen.assert_called_once()
    assert popen.call_args.args[0] == expected_cmd


def test_combined_stdout_stderr_captured(fake_popen: Callable[..., mock.MagicMock]) -> None:
    """Subprocess invoked with ``stdout=PIPE`` and ``stderr=STDOUT`` for merged tailing."""
    popen = fake_popen(stdout=_make_lines(5))
    run_pytest_short.main(["pytest"])
    popen.assert_called_once()
    kwargs: dict[str, Any] = popen.call_args.kwargs
    assert (kwargs["stdout"], kwargs["stderr"], kwargs.get("text")) == (subprocess.PIPE, subprocess.STDOUT, True)


def test_output_byte_cap_truncates_buffer(
    fake_popen: Callable[..., mock.MagicMock], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Oversized output is capped and annotated before tailing."""
    monkeypatch.setattr(run_pytest_short, "_MAX_OUTPUT_BYTES", 10)
    fake_popen(stdout="0123456789ABCDEFGHIJ")
    rc = run_pytest_short.main(["pytest", ".", "--tail-n", "20"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "0123456789" in out
    assert "output truncated at 10 bytes" in out


class TestTargetContainment:
    """Targets must resolve inside the working directory and must not look like pytest options."""

    @pytest.mark.parametrize(
        "target",
        [
            pytest.param("-p", id="plugin-flag"),
            pytest.param("--rootdir=/etc", id="rootdir-option"),
            pytest.param("-c", id="config-flag"),
            pytest.param("--basetemp=x::y", id="option-with-node-id"),
        ],
    )
    def test_rejects_option_shaped_target(
        self, fake_popen: Callable[..., mock.MagicMock], capsys: pytest.CaptureFixture[str], target: str
    ) -> None:
        """A target starting with ``-`` exits 64 before pytest starts, though it resolves under the working directory.

        ``Path("-p")`` resolves to ``<cwd>/-p``, so the containment check alone passed it straight into pytest's argv.
        """
        popen = fake_popen()
        with pytest.raises(SystemExit) as exc:
            run_pytest_short.main(["pytest", "tests/test_a.py", target])
        assert exc.value.code == 64
        popen.assert_not_called()
        assert "option-shaped target" in capsys.readouterr().err

    def test_any_target_outside_cwd_rejects_the_run(
        self, fake_popen: Callable[..., mock.MagicMock], capsys: pytest.CaptureFixture[str], workspace: _Workspace
    ) -> None:
        """One escaping target among several still exits 64 before pytest starts."""
        popen = fake_popen()
        with pytest.raises(SystemExit) as exc:
            run_pytest_short.main(["pytest", "tests/test_a.py", str(workspace.project.parent)])
        assert exc.value.code == 64
        popen.assert_not_called()
        assert "rejected target" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "target_template",
        [pytest.param("{outside}/leak", id="absolute-path"), pytest.param("../outside", id="relative-parent")],
    )
    def test_rejects_target_resolving_outside_cwd(
        self,
        fake_popen: Callable[..., mock.MagicMock],
        capsys: pytest.CaptureFixture[str],
        workspace: _Workspace,
        target_template: str,
    ) -> None:
        """A target outside ``Path.cwd()``, absolute or via ``..``, exits 64 and never starts pytest."""
        popen = fake_popen()
        with pytest.raises(SystemExit) as exc:
            run_pytest_short.main(["pytest", target_template.format(outside=workspace.outside)])
        assert exc.value.code == 64
        popen.assert_not_called()
        assert "outside project directory" in capsys.readouterr().err

    def test_rejects_symlink_target_outside_cwd(
        self, fake_popen: Callable[..., mock.MagicMock], capsys: pytest.CaptureFixture[str], workspace: _Workspace
    ) -> None:
        """Symlink targets are rejected based on their resolved location, not the link's own path."""
        (workspace.project / "linked").symlink_to(workspace.outside, target_is_directory=True)
        popen = fake_popen()
        with pytest.raises(SystemExit) as exc:
            run_pytest_short.main(["pytest", "linked"])
        assert exc.value.code == 64
        popen.assert_not_called()
        assert "outside project directory" in capsys.readouterr().err


class TestLogFile:
    """``--log`` writes the full output only inside a declared run directory or its dedicated fallback."""

    def test_keeps_full_output_while_terminal_shows_tail(
        self, fake_popen: Callable[..., mock.MagicMock], capsys: pytest.CaptureFixture[str], workspace: _Workspace
    ) -> None:
        """``--log`` writes every output line to disk while stdout still shows only the tail.

        Targeted loop runs print a short tail to keep context small; the full output must stay inspectable afterwards.
        """
        fake_popen(stdout=_make_lines(30))
        log = workspace.project / ".developments" / "run" / "run.log"
        run_pytest_short.main(["pytest", "--tail-n", "5", "--log", str(log)])
        assert capsys.readouterr().out.splitlines() == _line_range(26, 30)
        assert log.read_text(encoding="utf-8").splitlines() == _line_range(1, 30)

    def test_rejects_log_outside_project_and_temp(
        self, fake_popen: Callable[..., mock.MagicMock], capsys: pytest.CaptureFixture[str], workspace: _Workspace
    ) -> None:
        """``--log`` outside both the working directory and ``$TMPDIR`` exits 64; nothing is created or run."""
        popen = fake_popen(stdout="x")
        elsewhere = workspace.outside / "run.log"
        assert run_pytest_short.main(["pytest", "--log", str(elsewhere)]) == 64
        popen.assert_not_called()
        assert not elsewhere.exists()
        assert "rejected --log" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "where",
        [
            pytest.param("project/.developments/run/run.log", id="project-run"),
            pytest.param("scratch/run/run.log", id="temp-run"),
        ],
    )
    def test_declared_run_directory_is_written(
        self, fake_popen: Callable[..., mock.MagicMock], workspace: _Workspace, where: str
    ) -> None:
        """A trusted run sentinel can name a dedicated run directory in either the project or temp directory."""
        fake_popen(stdout="line-1")
        log = workspace.project.parent / where
        dev_run_dir.write_sentinel("dev-feature-dev-dir", str(log.parent))
        assert run_pytest_short.main(["pytest", "--log", str(log)]) == 0
        assert log.read_text(encoding="utf-8") == "line-1"


def test_missing_runner_has_distinct_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing allowlisted executable is a runner error rather than a red test."""
    monkeypatch.setattr(run_pytest_short, "which", mock.create_autospec(which, return_value=None))
    assert run_pytest_short.main(["pytest"]) == 127


def test_timeout_has_distinct_exit(fake_popen: Callable[..., mock.MagicMock], monkeypatch: pytest.MonkeyPatch) -> None:
    """An expired subprocess wait cannot be reported as pytest failure."""
    fake_popen()
    monkeypatch.setattr(
        _FakePopen, "wait", mock.create_autospec(_FakePopen.wait, side_effect=subprocess.TimeoutExpired("pytest", 600))
    )
    monkeypatch.setattr(_FakePopen, "kill", lambda self: None, raising=False)
    assert run_pytest_short.main(["pytest"]) == 124


@pytest.mark.integration
def test_deadline_stops_silent_process_during_pipe_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """The runner deadline fires before a silent child produces EOF, not only during wait."""
    monkeypatch.setattr(run_pytest_short, "_TIMEOUT_SECONDS", 0.05)
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(20)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        code, output = run_pytest_short._capture(child)
        assert code == 124
        assert "runner deadline expired" in output
        assert child.poll() is not None
    finally:
        child.kill() if child.poll() is None else None
        child.wait()


@pytest.mark.integration
def test_deadline_stops_descendants_holding_pipe(monkeypatch: pytest.MonkeyPatch) -> None:
    """A launcher cannot extend the deadline by handing its stdout pipe to a child."""
    monkeypatch.setattr(run_pytest_short, "_TIMEOUT_SECONDS", 0.1)
    script = "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(2)']); time.sleep(5)"
    child = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=os.name != "nt",
    )
    started = time.monotonic()
    try:
        code, _ = run_pytest_short._capture(child)
        assert code == 124
        assert time.monotonic() - started < 1.5
    finally:
        child.kill() if child.poll() is None else None
        child.wait()
