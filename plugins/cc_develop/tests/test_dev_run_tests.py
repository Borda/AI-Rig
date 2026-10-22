"""Exercise the sentinel test runner with real child processes and hostile state."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import dev_run_dir
import dev_run_tests
import pytest


@pytest.fixture
def session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate both runner sentinels and any child side effects."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "tests-runner")
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.mark.parametrize(
    ("mode", "sentinel", "command"),
    [
        pytest.param("pytest", "dev-pytest-cmd", "pytest; touch pwned", id="pytest-injection"),
        pytest.param("suite", "dev-test-cmd", "make test; touch pwned", id="suite-injection"),
        pytest.param("suite", "dev-test-cmd", "make test ", id="suite-not-exact"),
    ],
)
def test_rejects_unsafe_command(session, capsys, mode, sentinel, command):
    """A command outside the exact allowlist is rejected before anything executes."""
    dev_run_dir.write_sentinel(sentinel, command)
    assert dev_run_tests.main([mode]) == dev_run_tests.EXIT_NO_RUN
    assert capsys.readouterr().err == f"dev_run_tests: rejected unsafe {sentinel}: {command}\n"
    assert not (session / "pwned").exists()


@pytest.mark.parametrize(
    "mode",
    [pytest.param("pytest", id="pytest"), pytest.param("suite", id="suite")],
)
def test_missing_or_empty_runner_is_unresolved(session, capsys, mode):
    """Missing and empty state never launches a command, returns shell exit 127 or any status a test runner returns."""
    assert dev_run_tests.main([mode]) == dev_run_tests.EXIT_NO_RUN
    dev_run_dir.write_sentinel("dev-pytest-cmd" if mode == "pytest" else "dev-test-cmd", "")
    assert dev_run_tests.main([mode]) == dev_run_tests.EXIT_NO_RUN
    assert capsys.readouterr().out.count("unresolved") == 2


@pytest.mark.integration
@pytest.mark.parametrize(
    "tail",
    [pytest.param([], id="stream"), pytest.param(["--tail", "2"], id="tail")],
)
def test_passes_argv_inertly_and_preserves_child_exit(session, monkeypatch, capsys, tail):
    """Quoted paths and shell syntax arrive as literal argv; the failing child stays failing."""
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
    expected = json.dumps(args) + "\nlast\n"
    assert capsys.readouterr().out == ("1 failed in 0.01s\nfirst\n" if not tail else "") + expected
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
def test_suite_uses_exact_command_argv(session, monkeypatch, capsys, command):
    """Each closed suite command resolves its executable and keeps its fixed arguments."""
    dev_run_dir.write_sentinel("dev-test-cmd", command)
    resolved = (session / "launcher.cmd").as_posix()
    seen = []
    monkeypatch.setattr(dev_run_tests, "which", lambda name: resolved)
    monkeypatch.setattr(dev_run_tests, "run_tail", lambda argv, tail_lines: seen.append(argv) or (5, ["done"]))
    assert dev_run_tests.main(["suite", "--tail", "1"]) == 5
    assert seen == [[resolved, *shlex.split(command)[1:]]]
    assert capsys.readouterr().out == "done\n"


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
def test_usage_errors(session, args):
    """Invalid wrapper arguments fail before a sentinel command is launched."""
    assert dev_run_tests.main(args) == dev_run_tests.EXIT_NO_RUN


def test_wrapper_fault_is_not_a_failing_test(session, monkeypatch, capsys):
    """A crash inside the wrapper reports the no-run status, never the status 1 of a failing test."""
    dev_run_dir.write_sentinel("dev-pytest-cmd", "pytest")
    monkeypatch.setattr(dev_run_tests, "which", lambda name: sys.executable)

    def explode(argv, tail_lines):
        raise RuntimeError("boom")

    monkeypatch.setattr(dev_run_tests, "run_tail", explode)
    assert dev_run_tests.main(["pytest", "--tail", "1"]) == dev_run_tests.EXIT_NO_RUN
    assert "wrapper error" in capsys.readouterr().err


def test_child_exit_matching_the_no_run_status_is_reported_as_an_error(session, monkeypatch, capsys):
    """A test command that happens to exit 64 is neither a pass nor a red demo, so 64 only ever means nothing ran."""
    dev_run_dir.write_sentinel("dev-pytest-cmd", "pytest")
    monkeypatch.setattr(dev_run_tests, "which", lambda name: sys.executable)
    monkeypatch.setattr(dev_run_tests, "run_tail", lambda argv, tail_lines: (dev_run_tests.EXIT_NO_RUN, []))
    assert dev_run_tests.main(["pytest", "--tail", "1"]) == dev_run_tests.EXIT_CHILD_RESERVED
    assert "reporting 2" in capsys.readouterr().err


def test_fault_before_the_command_starts_is_not_a_failing_test(session, monkeypatch, capsys):
    """A crash while the wrapper sets itself up (here a closed stdout) reports the no-run status, not 1."""
    monkeypatch.setattr(
        dev_run_tests.sys.stdout, "reconfigure", lambda **kw: (_ for _ in ()).throw(ValueError("closed")), raising=False
    )
    assert dev_run_tests.main(["pytest"]) == dev_run_tests.EXIT_NO_RUN
    assert "wrapper error" in capsys.readouterr().err


@pytest.mark.integration
@pytest.mark.skipif(sys.platform == "win32", reason="closed-pipe semantics differ on native Windows")
def test_closed_output_pipe_keeps_the_test_status(session):
    """A reader that closes stdout early does not turn a failing test run into a launch failure.

    The wrapper runs real pytest against a failing test while its stdout reader is already gone; the status of that run
    must still arrive as 1, so a gate downstream of a pipe sees a failed test rather than a missing runner.
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


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX numeric ownership check")
def test_foreign_owned_sentinel_is_refused(session, monkeypatch, capsys):
    """The existing sentinel reader refuses another user's command, with a status no reproduction gate accepts."""
    dev_run_dir.write_sentinel("dev-pytest-cmd", "pytest")
    monkeypatch.setattr(dev_run_dir, "_owned_by_caller", lambda status: False)
    assert dev_run_tests.main(["pytest"]) == dev_run_tests.EXIT_NO_RUN
    assert "unresolved" in capsys.readouterr().out


def test_missing_executable_is_a_launch_error(session, monkeypatch, capsys):
    """Missing tools produce an actionable error and the no-run status rather than a shell exit 127."""
    dev_run_dir.write_sentinel("dev-pytest-cmd", "pytest")
    monkeypatch.setattr(dev_run_tests, "which", lambda name: None)
    assert dev_run_tests.main(["pytest"]) == dev_run_tests.EXIT_NO_RUN
    assert "not found on PATH" in capsys.readouterr().err


@pytest.mark.parametrize(
    "skill",
    [pytest.param("fix", id="fix"), pytest.param("feature", id="feature"), pytest.param("refactor", id="refactor")],
)
def test_skill_calls_portable_runner_without_eval(skill):
    """Shipped test fences use the same portable Python launcher as other develop helpers."""
    source = (Path(__file__).parents[1] / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
    assert 'eval "$PYTEST_CMD' not in source
    assert 'eval "$TEST_CMD' not in source
    runner = 'python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_run_tests.py"'
    assert f"{runner} pytest" in source
    assert f"{runner} suite" in source


def test_missing_pytest_module_is_not_a_failing_test(session, monkeypatch, capsys):
    """A runner whose Python lacks pytest prints a module error and exits 1; that must read as no test ran.

    The reproduction gates accept exit 1 as a reproduced failure, so an uninstalled pytest would otherwise unlock the
    implementation step although nothing executed.
    """
    dev_run_dir.write_sentinel("dev-pytest-cmd", "python -m pytest")
    monkeypatch.setattr(dev_run_tests, "which", lambda name: sys.executable)
    lines = ["/usr/bin/python3: No module named pytest"]
    monkeypatch.setattr(dev_run_tests, "run_tail", lambda argv, tail_lines: (1, lines))
    assert dev_run_tests.main(["pytest", "--tail", "5"]) == dev_run_tests.EXIT_NO_RUN
    assert "without a pytest failure summary" in capsys.readouterr().err


def test_ordinary_failure_output_keeps_the_child_status(session, monkeypatch):
    """A real failing test run, whose output does not name a missing pytest, still reports status 1."""
    dev_run_dir.write_sentinel("dev-pytest-cmd", "pytest")
    monkeypatch.setattr(dev_run_tests, "which", lambda name: sys.executable)
    monkeypatch.setattr(
        dev_run_tests,
        "run_tail",
        lambda argv, tail_lines: (1, ["FAILED test_a.py::test_a - assert 0", "1 failed in 0.1s"]),
    )
    assert dev_run_tests.main(["pytest", "--tail", "5"]) == 1


def test_failure_summary_text_inside_a_real_failure_keeps_status_one(session, monkeypatch):
    """A genuine failing test whose message mentions a missing module still reports 1: the summary proves it ran."""
    dev_run_dir.write_sentinel("dev-pytest-cmd", "pytest")
    monkeypatch.setattr(dev_run_tests, "which", lambda name: sys.executable)
    lines = [
        "E   AssertionError: No module named pytest",
        "FAILED test_a.py::test_a - AssertionError",
        "1 failed in 0.1s",
    ]
    monkeypatch.setattr(dev_run_tests, "run_tail", lambda argv, tail_lines: (1, lines))
    assert dev_run_tests.main(["pytest", "--tail", "5"]) == 1


def test_quiet_failure_summary_without_a_count_line_keeps_status_one(session, monkeypatch):
    """With ``-qq`` pytest omits the ``N failed`` line but still prints ``FAILED …``; that proves a test failed."""
    dev_run_dir.write_sentinel("dev-pytest-cmd", "pytest")
    monkeypatch.setattr(dev_run_tests, "which", lambda name: sys.executable)
    lines = ["=== short test summary info ===", "FAILED test_a.py::test_a - assert 0"]
    monkeypatch.setattr(dev_run_tests, "run_tail", lambda argv, tail_lines: (1, lines))
    assert dev_run_tests.main(["pytest", "--tail", "5"]) == 1


@pytest.mark.parametrize(
    "line",
    [
        pytest.param("startup compiler: 1 error generated.", id="unrelated-error-count"),
        pytest.param("FAILED preflight: required test environment is unavailable", id="failed-line-without-node-id"),
    ],
)
def test_startup_noise_is_not_a_failure_summary(session, monkeypatch, line):
    """A startup hook that prints failure-like text and exits 1 is a runner fault, not a failing test.

    Neither line names a test node id or the pytest stats line, so nothing proves a test ran.
    """
    dev_run_dir.write_sentinel("dev-pytest-cmd", "pytest")
    monkeypatch.setattr(dev_run_tests, "which", lambda name: sys.executable)
    monkeypatch.setattr(dev_run_tests, "run_tail", lambda argv, tail_lines: (1, [line]))
    assert dev_run_tests.main(["pytest", "--tail", "5"]) == dev_run_tests.EXIT_NO_RUN


@pytest.mark.parametrize(
    ("code", "lines", "verdict"),
    [
        pytest.param(1, ["FAILED t.py::t - assert 0", "1 failed in 0.01s"], "failed", id="summarized-failure"),
        pytest.param(1, ["No module named pytest"], "none", id="exit-1-without-summary"),
        pytest.param(0, ["1 passed in 0.01s"], "none", id="pass"),
        pytest.param(5, ["no tests ran in 0.01s"], "none", id="nothing-collected"),
    ],
)
def test_verdict_sentinel_names_only_a_summarized_failure(session, monkeypatch, code, lines, verdict):
    """``failed`` is written only for a pytest exit 1 with a summary; every other outcome leaves ``none``.

    Red gates accept exit 1 only beside ``failed``, because an interpreter that dies before this wrapper runs also exits
    1 and writes nothing.
    """
    dev_run_dir.write_sentinel("dev-pytest-cmd", "pytest")
    dev_run_dir.write_sentinel(dev_run_tests.VERDICT_SENTINEL, "failed")
    monkeypatch.setattr(dev_run_tests, "which", lambda name: sys.executable)
    monkeypatch.setattr(dev_run_tests, "run_tail", lambda argv, tail_lines: (code, lines))
    dev_run_tests.main(["pytest", "--tail", "5"])
    assert dev_run_dir.read_sentinel(dev_run_tests.VERDICT_SENTINEL) == verdict


def test_failed_verdict_carries_the_gate_nonce(session, monkeypatch):
    """The gate's per-run nonce is echoed in the ``failed`` verdict, so a stale or planted verdict never matches."""
    dev_run_dir.write_sentinel("dev-pytest-cmd", "pytest")
    monkeypatch.setenv(dev_run_tests.VERDICT_NONCE_ENV, "123.45.67")
    monkeypatch.setattr(dev_run_tests, "which", lambda name: sys.executable)
    monkeypatch.setattr(dev_run_tests, "run_tail", lambda argv, tail_lines: (1, ["1 failed in 0.01s"]))
    dev_run_tests.main(["pytest", "--tail", "5"])
    assert dev_run_dir.read_sentinel(dev_run_tests.VERDICT_SENTINEL) == "failed 123.45.67"


def test_non_canonical_session_id_records_no_verdict(session, monkeypatch, tmp_path):
    """A session id that Python would strip writes no verdict, so the gate (which uses the raw id) fails closed.

    ``review.a`` names ``dev-tests-verdict-review.a`` in the shell but ``…-reviewa`` in Python, a file another session
    (``reviewa``) also owns; writing nothing keeps the two from ever reading each other's verdict.
    """
    monkeypatch.setenv("CSID", "review.a")
    dev_run_dir.write_sentinel("dev-pytest-cmd", "pytest")
    monkeypatch.setattr(dev_run_tests, "which", lambda name: sys.executable)
    monkeypatch.setattr(dev_run_tests, "run_tail", lambda argv, tail_lines: (1, ["1 failed in 0.01s"]))
    dev_run_tests.main(["pytest", "--tail", "5"])
    assert sorted(p.name for p in tmp_path.glob("dev-tests-verdict-*")) == []


def test_coloured_failure_summary_keeps_status_one(session, monkeypatch):
    """With ``--color=yes`` pytest wraps the summary in ANSI codes; a real failure must still report 1.

    Forced colour puts escape codes inside both ``FAILED`` and the stats line, so matching the raw text would turn a
    genuine failure into "no test ran".
    """
    dev_run_dir.write_sentinel("dev-pytest-cmd", "pytest")
    monkeypatch.setattr(dev_run_tests, "which", lambda name: sys.executable)
    esc = chr(27)
    lines = [
        f"{esc}[31mFAILED{esc}[0m test_a.py::{esc}[1mtest_a{esc}[0m - assert 0",
        f"{esc}[31m{esc}[1m1 failed{esc}[0m{esc}[31m in 0.02s{esc}[0m",
    ]
    monkeypatch.setattr(dev_run_tests, "run_tail", lambda argv, tail_lines: (1, lines))
    assert dev_run_tests.main(["pytest", "--tail", "5"]) == 1
