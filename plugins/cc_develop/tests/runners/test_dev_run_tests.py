"""Exercise the sentinel test runner with real child processes and hostile state.

``dev_run_tests.py`` reads the test command from a sentinel, runs it without a shell and maps the outcome onto a status
the reproduction gates trust. Pure status-mapping cases stub the child run (``run_tail``); the cases that prove inert
argv handling and closed-pipe behaviour launch real children.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from unittest import mock

import dev_run_dir
import dev_run_tests
import pytest

_ESC = chr(27)
_NO_RUN = dev_run_tests.EXIT_NO_RUN


@pytest.fixture
def session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate both runner sentinels and any child side effects."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "tests-runner")
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def ready_runner(session: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Record ``pytest`` as the runner sentinel and resolve every executable name to the current interpreter."""
    dev_run_dir.write_sentinel("dev-pytest-cmd", "pytest")
    monkeypatch.setattr(dev_run_tests, "which", lambda name: sys.executable)
    return session


class TestCommandValidation:
    """The sentinel command is checked against the allowlist and the host before anything launches."""

    @pytest.mark.parametrize(
        ("mode", "sentinel", "command"),
        [
            pytest.param("pytest", "dev-pytest-cmd", "pytest; touch pwned", id="pytest-injection"),
            pytest.param("suite", "dev-test-cmd", "make test; touch pwned", id="suite-injection"),
            pytest.param("suite", "dev-test-cmd", "make test ", id="suite-not-exact"),
        ],
    )
    def test_rejects_unsafe_command(self, session, capsys, mode, sentinel, command):
        """A command outside the exact allowlist is rejected before anything executes.

        The sentinel is plain text in a shared temp dir, so a shell-shaped or padded value must never reach a
        subprocess; the rejection names the sentinel and the offending text on stderr.
        """
        dev_run_dir.write_sentinel(sentinel, command)
        assert dev_run_tests.main([mode]) == _NO_RUN
        assert capsys.readouterr().err == f"dev_run_tests: rejected unsafe {sentinel}: {command}\n"
        assert not (session / "pwned").exists()

    @pytest.mark.parametrize("mode", ["pytest", "suite"])
    def test_missing_sentinel_is_unresolved(self, session, capsys, mode):
        """A mode with no recorded command never launches and returns the no-run status, not shell exit 127.

        Runner detection may not have run yet; 127 or any status a test runner returns would read as a test result.
        """
        assert dev_run_tests.main([mode]) == _NO_RUN
        assert "unresolved" in capsys.readouterr().out

    @pytest.mark.parametrize(
        ("mode", "sentinel"),
        [pytest.param("pytest", "dev-pytest-cmd", id="pytest"), pytest.param("suite", "dev-test-cmd", id="suite")],
    )
    def test_empty_sentinel_is_unresolved(self, session, capsys, mode, sentinel):
        """An empty recorded command never launches and returns the no-run status.

        Detection that found nothing leaves an empty value; running it would exit 127 and look like a failing test.
        """
        dev_run_dir.write_sentinel(sentinel, "")
        assert dev_run_tests.main([mode]) == _NO_RUN
        assert "unresolved" in capsys.readouterr().out

    @pytest.mark.parametrize(
        "args",
        [
            pytest.param(["suite", "--", "tests/test_a.py"], id="suite-extra-target"),
            pytest.param(["suite", "-q"], id="suite-extra-option"),
            pytest.param(["pytest", "--tail", "-1"], id="negative-tail"),
            pytest.param(["suite", "--ta", "3"], id="no-option-abbreviation"),
            pytest.param(["bogus"], id="unknown-mode"),
        ],
    )
    def test_usage_errors_return_no_run(self, session, args):
        """Invalid wrapper arguments fail before a sentinel command is launched."""
        assert dev_run_tests.main(args) == _NO_RUN

    @mock.patch("dev_run_tests.which", autospec=True, return_value=None)
    def test_missing_executable_is_a_launch_error(self, mock_which, ready_runner, capsys):
        """Missing tools produce an actionable error and the no-run status rather than a shell exit 127."""
        assert dev_run_tests.main(["pytest"]) == _NO_RUN
        assert "not found on PATH" in capsys.readouterr().err

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX numeric ownership check")
    @mock.patch("dev_run_dir._owned_by_caller", autospec=True, return_value=False)
    def test_foreign_owned_sentinel_is_refused(self, mock_owned, ready_runner, capsys):
        """The existing sentinel reader refuses another user's command, with a status no reproduction gate accepts."""
        assert dev_run_tests.main(["pytest"]) == _NO_RUN
        assert "unresolved" in capsys.readouterr().out

    @pytest.mark.parametrize("skill", ["fix", "feature", "refactor"])
    def test_skill_calls_portable_runner_without_eval(self, skill):
        """Shipped test fences use the same portable Python launcher as other develop helpers.

        The skill source must call the wrapper through the plugin launcher for both modes and never fall back to
        ``eval`` of a sentinel value.
        """
        source = (Path(__file__).parents[2] / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
        runner = 'python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_run_tests.py"'
        assert ('eval "$PYTEST_CMD' in source, 'eval "$TEST_CMD' in source, f"{runner} pytest" in source) == (
            False,
            False,
            True,
        )
        assert f"{runner} suite" in source


class TestLaunch:
    """What the wrapper actually executes, and how output and exit status come back."""

    @pytest.mark.integration
    @pytest.mark.parametrize(
        ("tail", "streamed"),
        [pytest.param([], "1 failed in 0.01s\nfirst\n", id="stream"), pytest.param(["--tail", "2"], "", id="tail")],
    )
    def test_passes_argv_inertly_and_preserves_child_exit(self, session, monkeypatch, capsys, tail, streamed):
        """Quoted paths and shell syntax arrive as literal argv; the failing child stays failing.

        A real child prints its own argv as JSON, so the check proves nothing was expanded or split by a shell.
        """
        script = session / "fake runner.py"
        script.write_text(
            "import json, sys\nprint('1 failed in 0.01s')\nprint('first')\nprint(json.dumps(sys.argv[1:]))\n"
            "sys.stdout.flush()\nprint('last', file=sys.stderr)\nraise SystemExit(1)\n",
            encoding="utf-8",
        )
        command = "python " + shlex.quote(script.as_posix())
        monkeypatch.setattr(dev_run_tests, "_PYTEST_ALLOWLIST", frozenset({command}))
        monkeypatch.setattr(dev_run_tests, "which", lambda name: sys.executable)
        dev_run_dir.write_sentinel("dev-pytest-cmd", command)
        args = ["space path::test_case", "$(touch pwned)", "-v"]
        assert dev_run_tests.main(["pytest", *tail, "--", *args]) == 1
        assert capsys.readouterr().out == streamed + json.dumps(args) + "\nlast\n"
        assert not (session / "pwned").exists()

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("tox", id="tox"),
            pytest.param("tox -q", id="quiet-tox"),
            pytest.param("make test", id="make"),
            pytest.param("npm test", id="npm"),
            pytest.param("cargo test", id="cargo"),
            pytest.param("go test ./...", id="go"),
            pytest.param("python -m pytest", id="pytest-suite"),
        ],
    )
    @mock.patch("dev_run_tests.run_tail", autospec=True, return_value=(5, ["done"]))
    def test_suite_uses_exact_command_argv(self, mock_run_tail, session, monkeypatch, capsys, command):
        """Each closed suite command resolves its executable and keeps its fixed arguments.

        The child run is stubbed: the contract here is the argv built from the allowlisted command, plus passing the
        child's exit status and tail straight back.
        """
        dev_run_dir.write_sentinel("dev-test-cmd", command)
        resolved = (session / "launcher.cmd").as_posix()
        monkeypatch.setattr(dev_run_tests, "which", lambda name: resolved)
        assert dev_run_tests.main(["suite", "--tail", "1"]) == 5
        assert mock_run_tail.call_args.args[0] == [resolved, *shlex.split(command)[1:]]
        assert capsys.readouterr().out == "done\n"

    @pytest.mark.integration
    @pytest.mark.skipif(sys.platform == "win32", reason="closed-pipe semantics differ on native Windows")
    def test_closed_output_pipe_keeps_the_test_status(self, session):
        """A reader that closes stdout early does not turn a failing test run into a launch failure.

        The wrapper runs real pytest against a failing test while its stdout reader is already gone; the status of that
        run must still arrive as 1, so a gate downstream of a pipe sees a failed test rather than a missing runner.
        """
        (session / "test_fail.py").write_text("def test_fail():\n    assert False\n", encoding="utf-8")
        dev_run_dir.write_sentinel("dev-pytest-cmd", "python -m pytest")
        script = Path(dev_run_tests.__file__)
        env = {**os.environ, "TMPDIR": str(session), "CSID": "tests-runner", "PYTHONPATH": str(script.parent)}
        proc = subprocess.Popen(
            [sys.executable, str(script), "pytest", "--", "-p", "no:cacheprovider", "test_fail.py"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=session,
            env=env,
        )
        proc.stdout.close()
        assert proc.wait(timeout=120) == 1
        proc.stderr.close()


class TestWrapperFaults:
    """A crash or reserved status inside the wrapper never reads as a failing test."""

    @mock.patch("dev_run_tests.run_tail", autospec=True, side_effect=RuntimeError("boom"))
    def test_crash_reports_no_run(self, mock_run_tail, ready_runner, capsys):
        """A crash inside the wrapper reports the no-run status, never the status 1 of a failing test."""
        assert dev_run_tests.main(["pytest", "--tail", "1"]) == _NO_RUN
        assert "wrapper error" in capsys.readouterr().err

    @mock.patch("dev_run_tests.run_tail", autospec=True, return_value=(_NO_RUN, []))
    def test_child_exit_matching_no_run_status_is_an_error(self, mock_run_tail, ready_runner, capsys):
        """A child exiting with the reserved no-run status is reported as an error, not as that status.

        A test command that happens to exit 64 is neither a pass nor a red demo, so 64 only ever means nothing ran.
        """
        assert dev_run_tests.main(["pytest", "--tail", "1"]) == dev_run_tests.EXIT_CHILD_RESERVED
        assert "reporting 2" in capsys.readouterr().err

    def test_fault_before_command_starts_reports_no_run(self, session, monkeypatch, capsys):
        """A crash while the wrapper sets itself up (here a closed stdout) reports the no-run status, not 1."""
        closed = mock.Mock(side_effect=ValueError("closed"))
        monkeypatch.setattr(dev_run_tests.sys.stdout, "reconfigure", closed, raising=False)
        assert dev_run_tests.main(["pytest"]) == _NO_RUN
        assert "wrapper error" in capsys.readouterr().err


class TestFailureSummaryStatus:
    """Exit 1 only stands as a failing test when the output proves a test ran."""

    @mock.patch("dev_run_tests.run_tail", autospec=True, return_value=(1, ["/usr/bin/python3: No module named pytest"]))
    def test_missing_pytest_module_is_not_a_failing_test(self, mock_run_tail, ready_runner, capsys):
        """A runner whose Python lacks pytest prints a module error and exits 1; that must read as no test ran.

        The reproduction gates accept exit 1 as a reproduced failure, so an uninstalled pytest would otherwise unlock
        the implementation step although nothing executed.
        """
        dev_run_dir.write_sentinel("dev-pytest-cmd", "python -m pytest")
        assert dev_run_tests.main(["pytest", "--tail", "5"]) == _NO_RUN
        assert "without a pytest failure summary" in capsys.readouterr().err

    @pytest.mark.parametrize(
        ("lines", "expected"),
        [
            # a genuine failing run keeps the child's status
            pytest.param(["FAILED test_a.py::test_a - assert 0", "1 failed in 0.1s"], 1, id="ordinary-failure"),
            # the summary proves it ran even when the message mentions a missing module
            pytest.param(
                ["E   AssertionError: No module named pytest", "FAILED test_a.py::test_a - AssertionError", "1 failed"],
                1,
                id="failure-message-mentions-missing-module",
            ),
            # with -qq pytest omits the "N failed" line but still prints FAILED <node id>
            pytest.param(
                ["=== short test summary info ===", "FAILED test_a.py::test_a - assert 0"],
                1,
                id="quiet-summary-without-count-line",
            ),
            # forced colour wraps both FAILED and the stats line in ANSI codes
            pytest.param(
                [
                    f"{_ESC}[31mFAILED{_ESC}[0m test_a.py::{_ESC}[1mtest_a{_ESC}[0m - assert 0",
                    f"{_ESC}[31m{_ESC}[1m1 failed{_ESC}[0m{_ESC}[31m in 0.02s{_ESC}[0m",
                ],
                1,
                id="coloured-summary",
            ),
            # startup hooks printing failure-like text name neither a node id nor the pytest stats line
            pytest.param(["startup compiler: 1 error generated."], _NO_RUN, id="unrelated-error-count"),
            pytest.param(
                ["FAILED preflight: required test environment is unavailable"],
                _NO_RUN,
                id="failed-line-without-node-id",
            ),
        ],
    )
    @mock.patch("dev_run_tests.run_tail", autospec=True)
    def test_exit_one_status_follows_failure_summary(self, mock_run_tail, ready_runner, lines, expected):
        """A pytest exit 1 keeps status 1 only when the output carries a real failure summary; otherwise nothing ran.

        Red gates treat status 1 as a reproduced failure, so startup noise and module errors must degrade to the no-run
        status while ANSI-coloured and quiet summaries must still count.
        """
        mock_run_tail.return_value = (1, lines)
        assert dev_run_tests.main(["pytest", "--tail", "5"]) == expected


class TestVerdictSentinel:
    """The ``dev-tests-verdict`` sentinel records a summarized failure for the red gates."""

    @pytest.mark.parametrize(
        ("code", "lines", "verdict"),
        [
            pytest.param(1, ["FAILED t.py::t - assert 0", "1 failed in 0.01s"], "failed", id="summarized-failure"),
            pytest.param(1, ["No module named pytest"], "none", id="exit-1-without-summary"),
            pytest.param(0, ["1 passed in 0.01s"], "none", id="pass"),
            pytest.param(5, ["no tests ran in 0.01s"], "none", id="nothing-collected"),
        ],
    )
    @mock.patch("dev_run_tests.run_tail", autospec=True)
    def test_names_only_a_summarized_failure(self, mock_run_tail, ready_runner, code, lines, verdict):
        """``failed`` is written only for a pytest exit 1 with a summary; every other outcome leaves ``none``.

        Red gates accept exit 1 only beside ``failed``, because an interpreter that dies before this wrapper runs also
        exits 1 and writes nothing.
        """
        dev_run_dir.write_sentinel(dev_run_tests.VERDICT_SENTINEL, "failed")
        mock_run_tail.return_value = (code, lines)
        dev_run_tests.main(["pytest", "--tail", "5"])
        assert dev_run_dir.read_sentinel(dev_run_tests.VERDICT_SENTINEL) == verdict

    @mock.patch("dev_run_tests.run_tail", autospec=True, return_value=(1, ["1 failed in 0.01s"]))
    def test_failed_verdict_carries_gate_nonce(self, mock_run_tail, ready_runner, monkeypatch):
        """The gate's per-run nonce is echoed in the ``failed`` verdict, so a stale or planted verdict never matches."""
        monkeypatch.setenv(dev_run_tests.VERDICT_NONCE_ENV, "123.45.67")
        dev_run_tests.main(["pytest", "--tail", "5"])
        assert dev_run_dir.read_sentinel(dev_run_tests.VERDICT_SENTINEL) == "failed 123.45.67"

    @mock.patch("dev_run_tests.run_tail", autospec=True, return_value=(1, ["1 failed in 0.01s"]))
    def test_non_canonical_session_id_records_no_verdict(self, mock_run_tail, session, monkeypatch):
        """A session id that Python would strip writes no verdict, so the gate (which uses the raw id) fails closed.

        ``review.a`` names ``dev-tests-verdict-review.a`` in the shell but ``…-reviewa`` in Python, a file another
        session (``reviewa``) also owns; writing nothing keeps the two from ever reading each other's verdict.
        """
        monkeypatch.setenv("CSID", "review.a")
        dev_run_dir.write_sentinel("dev-pytest-cmd", "pytest")
        monkeypatch.setattr(dev_run_tests, "which", lambda name: sys.executable)
        dev_run_tests.main(["pytest", "--tail", "5"])
        assert sorted(p.name for p in session.glob("dev-tests-verdict-*")) == []
