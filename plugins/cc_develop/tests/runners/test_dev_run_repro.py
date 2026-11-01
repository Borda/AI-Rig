"""Tests for ``bin/dev_run_repro.py`` — the pytest repro step of ``/develop:debug``.

Covers:
* Verdict line and ``dev-gate-exit`` sentinel for a failing and a passing run
* The exact runner command line (``--tb=long``, optional target, ``-v``) and the no-target full-suite shape
* Refusal paths: unresolved runner, non-allowlisted runner, target outside the project, stray arguments
* ``run_tail`` against a real subprocess: exit code propagation and bounded tail
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest import mock

import dev_run_repro
import pytest
from dev_run_dir import read_sentinel


@pytest.fixture(name="session")
def _session_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate ``$TMPDIR``, the session token and the CWD, and pin the runner lookup to a fake binary path.

    >>> getfixture("session").is_dir()
    True
    """
    base = tmp_path / "sentinels"
    base.mkdir()
    monkeypatch.setenv("TMPDIR", str(base))
    monkeypatch.setenv("CSID", "repro-test")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(dev_run_repro, "which", lambda name: f"/fake/{name}")
    return base


def _seed(session: Path, runner: str | None, test_path: str | None) -> None:
    """Write the two input sentinels the way runner detection and the debug flow do."""
    if runner is not None:
        (session / "dev-pytest-cmd-repro-test").write_text(runner + "\n", encoding="utf-8")
    if test_path is not None:
        (session / "dev-debug-test-path-repro-test").write_text(test_path + "\n", encoding="utf-8")


class TestVerdict:
    """The run's exit code decides the verdict line; the code itself lands in the gate sentinel."""

    @pytest.mark.parametrize(
        ("code", "tail", "verdict"),
        [
            pytest.param(
                1, ["last line", "1 failed in 0.01s"], "Bug reproduced — tests fail. Proceed to fix.", id="failing-run"
            ),
            pytest.param(
                0,
                ["last line"],
                "Tests pass — bug may not be reproducible via pytest; check symptom directly.",
                id="passing-run",
            ),
        ],
    )
    @mock.patch("dev_run_repro.run_tail", autospec=True)
    def test_prints_tail_then_verdict_and_records_exit(
        self,
        mock_run_tail: mock.MagicMock,
        session: Path,
        capsys: pytest.CaptureFixture[str],
        code: int,
        tail: list[str],
        verdict: str,
    ) -> None:
        """Print the tail, then the verdict; record the runner's exit code for later blocks.

        Only pytest exit 1 (tests ran and failed) reads as "reproduced"; exit 0 is a pass.
        """
        _seed(session, "uv run pytest", "tests/test_a.py::test_x")
        mock_run_tail.return_value = (code, tail)
        rc = dev_run_repro.main([])
        assert (rc, capsys.readouterr().out, read_sentinel("dev-gate-exit")) == (
            0,
            "\n".join(tail) + f"\n{verdict}\n",
            str(code),
        )

    @pytest.mark.parametrize(
        "tail",
        [
            pytest.param(["/usr/bin/python3: No module named pytest"], id="pytest-missing"),
            pytest.param(["startup compiler: 1 error generated."], id="startup-noise-with-an-error-count"),
            pytest.param(
                ["FAILED preflight: required test environment is unavailable"], id="startup-failed-line-without-node-id"
            ),
        ],
    )
    @mock.patch("dev_run_repro.run_tail", autospec=True)
    def test_exit_one_without_failure_summary_is_gate_error(
        self, mock_run_tail: mock.MagicMock, session: Path, capsys: pytest.CaptureFixture[str], tail: list[str]
    ) -> None:
        """Pytest exit 1 with no failure summary means the runner never started, so the gate errors instead.

        The run printed a module error or unrelated tool noise and exited 1 before any test executed; reading that as a
        reproduced bug would unlock the fix step with nothing proven.
        """
        _seed(session, "python -m pytest", "tests/test_a.py::test_x")
        mock_run_tail.return_value = (1, tail)
        rc = dev_run_repro.main([])
        assert (rc, "GATE ERROR" in capsys.readouterr().out) == (1, True)

    @pytest.mark.parametrize(
        "code",
        [
            pytest.param(2, id="collection-error"),
            pytest.param(4, id="bad-node-id"),
            pytest.param(5, id="no-tests-collected"),
            pytest.param(127, id="runner-missing"),
        ],
    )
    @mock.patch("dev_run_repro.run_tail", autospec=True)
    def test_other_exit_codes_are_gate_error(
        self, mock_run_tail: mock.MagicMock, session: Path, capsys: pytest.CaptureFixture[str], code: int
    ) -> None:
        """Exit codes other than 0 or 1 fail closed: an error line, rc 1, and no "reproduced" verdict.

        A collection error or an unknown node id in a freshly written test is the common way a repro run goes wrong;
        reading it as a reproduced bug would send the lead into a fix for a bug nobody observed.
        """
        _seed(session, "uv run pytest", "tests/test_a.py::test_x")
        mock_run_tail.return_value = (code, ["last line"])
        rc = dev_run_repro.main([])
        out = capsys.readouterr().out
        assert (
            rc,
            "Bug reproduced" in out,
            f"! GATE ERROR: pytest exit {code}" in out,
            read_sentinel("dev-gate-exit"),
        ) == (
            1,
            False,
            True,
            str(code),
        )


class TestCommandLine:
    """The runner is invoked exactly as the inline blocks did, minus the shell."""

    @pytest.mark.parametrize(
        ("runner", "test_path", "expected"),
        [
            pytest.param(
                "uv run pytest",
                "tests/test_a.py::test_x",
                ["/fake/uv", "run", "pytest", "--tb=long", "tests/test_a.py::test_x", "-v"],
                id="node-id-target",
            ),
            pytest.param("pytest", "", ["/fake/pytest", "--tb=long", "-v"], id="empty-path-is-full-suite"),
            pytest.param(
                "python -m pytest", None, ["/fake/python", "-m", "pytest", "--tb=long", "-v"], id="no-path-sentinel"
            ),
        ],
    )
    @mock.patch("dev_run_repro.run_tail", autospec=True, return_value=(0, []))
    def test_builds_argv(
        self, mock_run_tail: mock.MagicMock, session: Path, runner: str, test_path: str | None, expected: list[str]
    ) -> None:
        """An empty or absent target adds no argument at all — never an empty-string one or an explicit ``.``.

        The repro step must run the same command the old inline blocks ran; a stray ``""`` or ``.`` argument would
        change what pytest collects.
        """
        _seed(session, runner, test_path)
        dev_run_repro.main([])
        mock_run_tail.assert_called_once_with(expected)


class TestRefusals:
    """Inputs that must stop the run before pytest starts."""

    @mock.patch("dev_run_repro.run_tail", autospec=True)
    def test_unresolved_runner_exits_1_without_gate_sentinel(
        self, mock_run_tail: mock.MagicMock, session: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An empty runner would exit 127 and look like a reproduced bug, so it is reported and nothing runs."""
        rc = dev_run_repro.main([])
        out = capsys.readouterr().out
        assert (rc, out.startswith("! PYTEST_CMD unresolved"), read_sentinel("dev-gate-exit")) == (1, True, "")
        mock_run_tail.assert_not_called()

    @mock.patch("dev_run_repro.run_tail", autospec=True)
    def test_non_allowlisted_runner_is_rejected(
        self, mock_run_tail: mock.MagicMock, session: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A sentinel holding an arbitrary command never reaches a subprocess."""
        _seed(session, "rm -rf /", "")
        rc = dev_run_repro.main([])
        assert (rc, "rejected unsafe PYTEST_CMD" in capsys.readouterr().err) == (2, True)
        mock_run_tail.assert_not_called()

    @mock.patch("dev_run_repro.run_tail", autospec=True)
    def test_target_outside_project_is_rejected(self, mock_run_tail: mock.MagicMock, session: Path) -> None:
        """A test path lifted from an issue body cannot point pytest outside the working directory."""
        _seed(session, "pytest", "../../elsewhere/test_x.py")
        with pytest.raises(SystemExit) as exc:
            dev_run_repro.main([])
        assert exc.value.code == 1
        mock_run_tail.assert_not_called()

    @mock.patch("dev_run_repro.which", autospec=True, return_value=None)
    def test_runner_missing_from_path_exits_2(
        self, mock_which: mock.MagicMock, session: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An allowlisted runner that is not installed is a clear error, not a traceback."""
        _seed(session, "poetry run pytest", "")
        rc = dev_run_repro.main([])
        assert (rc, "not found on PATH" in capsys.readouterr().err) == (2, True)

    def test_stray_argument_is_a_usage_error(self, session: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """The script reads sentinels only; a leftover ``--test-path``-style argument fails loudly."""
        rc = dev_run_repro.main(["--test-path", "tests/x.py"])
        assert (rc, capsys.readouterr().out.startswith("usage:")) == (2, True)


class TestRunTail:
    """``run_tail`` against a real subprocess, no mocks — the child process is the contract."""

    def test_returns_exit_code_and_only_the_last_lines(self) -> None:
        """Ten lines in, three kept, in order; the child's exit code survives."""
        code, lines = dev_run_repro.run_tail(
            [sys.executable, "-c", "print(*range(10), sep='\\n'); raise SystemExit(4)"], tail_lines=3
        )
        assert (code, lines) == (4, ["7", "8", "9"])

    def test_merges_stderr_into_the_tail(self) -> None:
        """Pytest writes failures to stderr in some runners; the tail must show them like ``2>&1 | tail``."""
        code, lines = dev_run_repro.run_tail([sys.executable, "-c", "import sys; sys.stderr.write('boom\\n')"])
        assert (code, lines) == (0, ["boom"])

    @pytest.mark.filterwarnings("error::ResourceWarning")
    @pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning")
    def test_kills_child_holding_stdout_open_at_the_deadline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The wall-clock cap covers reading the output, not only waiting after it ends.

        The child keeps its stdout open for 30 s; the cap is 0.5 s. Before the read ran on a helper thread, the cap
        started only after the child closed its output, so the run lasted the full 30 s and never raised. Once the
        killed child's pipe ends, the output stream is closed: a stream left open surfaces as an unclosed-file
        ``ResourceWarning`` when garbage-collected, which the two warning filters turn into a failure.
        """
        monkeypatch.setattr(dev_run_repro, "_RUN_TIMEOUT_S", 0.5)
        with pytest.raises(subprocess.TimeoutExpired):
            dev_run_repro.run_tail([sys.executable, "-c", "import time; print('start', flush=True); time.sleep(30)"])
