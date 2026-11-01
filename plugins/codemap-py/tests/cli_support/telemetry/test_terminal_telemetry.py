"""Public CLI attempts retain terminal outcomes across host runtimes.

Every case launches the real ``scan-query`` / ``scan-index`` scripts in a subprocess: the exit code, the single final
telemetry record and the shard location are the contract, so none of the boundaries are mocked.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from codemap_py import rwgate

_RUNTIMES = ["claude", "codex", "direct"]
_skip_no_sigalrm = pytest.mark.skipif(
    not hasattr(signal, "SIGALRM"), reason="Engine timeout uses the optional SIGALRM capability."
)


def _logging_env(**overrides: str) -> dict[str, str]:
    """Return the environment with telemetry on, inherited log/index overrides dropped, then *overrides* applied."""
    inherited = {k: v for k, v in os.environ.items() if k not in {"CODEMAP_LOG_DIR", "CODEMAP_INDEX_DIR"}}
    return {**inherited, "CODEMAP_LOGGING": "true", **overrides}


def _run_cli(script: Path, args: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run one CLI script under *cwd* with *env* and capture its text output."""
    return subprocess.run([sys.executable, str(script), *args], cwd=cwd, env=env, capture_output=True, text=True)


def _records(logs: Path, pattern: str = "*.jsonl") -> list[dict]:
    """Return every telemetry record found beneath *logs*."""
    return [json.loads(line) for path in logs.rglob(pattern) for line in path.read_text().splitlines()]


def _explicit_target(tmp_path: Path, runtime: str, relative_logs: bool) -> tuple[Path, Path, dict[str, str], str]:
    """Create a caller and a one-module target project; return ``(caller, target, env, log_relative)``.

    The caller's legacy Claude session marker names a wrong session, the target's names the right one, so a record
    logged against the wrong project is detectable by its session. ``relative_logs`` switches the log directory to a
    relative path resolved against the target.
    """
    caller, target = tmp_path / "caller", tmp_path / "target"
    caller.mkdir()
    target.mkdir()
    (target / "module.py").write_text("def function():\n    return 1\n")
    (tmp_path / "codemap-caller-session").write_text("wrong-session")
    (tmp_path / "codemap-target-session").write_text("target-session")
    env = _logging_env(
        CODEMAP_RUNTIME=runtime,
        CODEX_THREAD_ID="target-session",
        CODEMAP_TELEMETRY_SESSION="target-session",
        TMPDIR=str(tmp_path),
        **({"CODEMAP_LOG_DIR": "relative-logs"} if relative_logs else {}),
    )
    return caller, target, env, ("relative-logs" if relative_logs else ".cache/codemap/logs")


def _assert_single_target_record(caller: Path, target: Path, log_relative: str, runtime: str, exit_code: int) -> None:
    """Assert exactly one record landed in the target's log directory and none in the caller's."""
    records = _records(target / log_relative)
    assert len(records) == 1
    assert records[0]["project"] == target.resolve().as_posix()
    assert records[0]["runtime"] == runtime
    assert records[0]["session"] == "target-session"
    assert records[0]["exit_code"] == exit_code
    assert not (caller / log_relative).exists()


@pytest.mark.integration
@pytest.mark.parametrize("runtime", _RUNTIMES)
@pytest.mark.parametrize("relative_logs", [False, True])
class TestExplicitTargetOwnsTelemetry:
    """Cross-project calls log against their target, including Claude's legacy session marker."""

    @pytest.mark.parametrize(
        ("arguments", "exit_code"),
        [
            pytest.param(["central"], 0, id="success"),
            pytest.param(["symbol", "missing_symbol"], 1, id="lookup-failure"),
            pytest.param(["central", "--invalid-option"], 2, id="parse-failure"),
            pytest.param(["central", "--root"], 2, id="repeated-root-failure"),
        ],
    )
    def test_query(self, runtime, relative_logs, arguments, exit_code, scan_query, scan_index, tmp_path):
        """A ``scan-query`` run from another directory logs one record against its ``--root`` target.

        The index is built first with logging off; then the query runs from the caller directory, so the record's
        project, runtime and session must come from the target rather than the caller.
        """
        caller, target, env, log_relative = _explicit_target(tmp_path, runtime, relative_logs)
        setup = _run_cli(scan_index, ["--root", str(target)], cwd=caller, env={**env, "CODEMAP_LOGGING": "false"})
        assert setup.returncode == 0, setup.stderr
        index = target / ".cache" / "codemap" / "target.json"

        result = _run_cli(
            scan_query, ["--root", str(target), "--index", str(index), "--no-heal", *arguments], cwd=caller, env=env
        )

        assert result.returncode == exit_code, result.stderr
        _assert_single_target_record(caller, target, log_relative, runtime, exit_code)

    @pytest.mark.parametrize(
        ("arguments", "exit_code"),
        [
            pytest.param([], 0, id="success"),
            pytest.param(["--invalid-option"], 2, id="parse-failure"),
            pytest.param(["--root"], 2, id="repeated-root-failure"),
        ],
    )
    def test_index(self, runtime, relative_logs, arguments, exit_code, scan_index, tmp_path):
        """A ``scan-index`` run from another directory logs one record against its ``--root`` target.

        Parse failures (an unknown option, a dangling ``--root``) must still produce the one final record.
        """
        caller, target, env, log_relative = _explicit_target(tmp_path, runtime, relative_logs)

        result = _run_cli(scan_index, ["--root", str(target), *arguments], cwd=caller, env=env)

        assert result.returncode == exit_code, result.stderr
        _assert_single_target_record(caller, target, log_relative, runtime, exit_code)


@pytest.mark.integration
@pytest.mark.parametrize("engine", ["query", "index"])
@pytest.mark.parametrize(
    ("arguments", "owner", "error"),
    [
        pytest.param(["--root"], "caller", "argument --root: expected one argument", id="missing-first-root"),
        pytest.param(
            ["--root={target}", "--root"], "target", "argument --root: expected one argument", id="equals-root-retained"
        ),
        pytest.param(
            ["--root", "{caller}", "--root", "{target}", "--root"],
            "target",
            "argument --root: expected one argument",
            id="last-complete-root-retained",
        ),
    ],
)
def test_partial_root_parsing_keeps_argparse_precedence(
    engine, arguments, owner, error, scan_query, scan_index, tmp_path
):
    """Partial root parsing retains argparse precedence without printing extra diagnostics.

    A dangling ``--root`` makes argparse fail, but the telemetry record must still be owned by the last complete root
    seen (or by the caller when none was), carry the original argv, and the error text must print exactly once.
    """
    caller, target = tmp_path / "caller", tmp_path / "target"
    caller.mkdir()
    target.mkdir()
    logs = tmp_path / "logs"
    argv = [argument.format(caller=caller, target=target) for argument in arguments]
    env = _logging_env(CODEMAP_RUNTIME="direct", CODEMAP_LOG_DIR=str(logs))

    result = _run_cli(scan_query if engine == "query" else scan_index, argv, cwd=caller, env=env)

    assert result.returncode == 2
    assert not result.stdout
    assert result.stderr.count("error:") == 1
    assert error in result.stderr
    records = _records(logs)
    assert len(records) == 1
    assert records[0]["project"] == (caller if owner == "caller" else target).resolve().as_posix()
    assert records[0]["exit_code"] == 2
    assert records[0]["argv"] == argv


@pytest.mark.integration
@pytest.mark.parametrize("runtime", _RUNTIMES)
@pytest.mark.parametrize(
    ("engine", "arguments", "exit_code", "command", "result_key"),
    [
        pytest.param("query", ["central", "--invalid-option"], 2, "query", "error", id="query-parse-failure"),
        pytest.param("index", ["--invalid-option"], 2, "index", "error", id="index-parse-failure"),
        pytest.param("query", ["--no-heal", "central"], 0, "central", "central", id="query-success"),
        pytest.param("query", ["--no-heal", "symbol", "missing_symbol"], 1, "symbol", "error", id="lookup-failure"),
        pytest.param("index", ["--incremental"], 0, "index", "modules_indexed", id="index-success"),
    ],
)
def test_logs_one_terminal_record(
    runtime, engine, arguments, exit_code, command, result_key, project, scan_query, scan_index, tmp_path
):
    """Failures before output and ordinary answers each produce exactly one final record.

    The record names the runtime, project, command and argv; a failure carries a non-empty ``error`` and a success
    carries the engine's headline result (``central`` or a positive ``modules_indexed``).
    """
    root, _ = project
    logs = tmp_path / "logs"
    env = _logging_env(
        CODEMAP_LOG_DIR=str(logs), CODEMAP_RUNTIME=runtime, TMPDIR=str(tmp_path), CODEX_THREAD_ID="test-thread"
    )

    result = _run_cli(scan_query if engine == "query" else scan_index, arguments, cwd=root, env=env)

    assert result.returncode == exit_code, result.stderr
    records = _records(logs)
    assert len(records) == 1, records
    record = records[0]
    assert record["runtime"] == runtime
    assert record["project"] == root.resolve().as_posix()
    assert record["cmd"] == command
    assert record["argv"] == arguments
    assert record["exit_code"] == exit_code
    assert record["result"][result_key]


@pytest.mark.integration
@pytest.mark.parametrize("runtime", _RUNTIMES)
@pytest.mark.parametrize(
    ("engine", "engine_args"),
    [pytest.param("query", ["--no-heal", "central"], id="query"), pytest.param("index", [], id="index")],
)
@pytest.mark.parametrize(
    ("gate_timeout", "cli_args", "exit_code", "error"),
    [
        pytest.param("0.05", [], 1, "index_busy", id="gate-busy"),
        pytest.param("10", ["--timeout", "1"], 2, "timeout", id="engine-timeout", marks=_skip_no_sigalrm),
    ],
)
def test_contended_gate_logs_busy_or_timeout(
    runtime, engine, engine_args, gate_timeout, cli_args, exit_code, error, project, scan_query, scan_index, tmp_path
):
    """A real competing writer produces one logged gate rejection or handled timeout.

    While the writer lease is held, a short gate timeout gives ``index_busy`` (exit 1) and a long gate timeout with
    ``--timeout 1`` gives the engine's own ``timeout`` (exit 2); each must log exactly one terminal CLI record.
    """
    root, index = project
    logs = tmp_path / "logs"
    env = _logging_env(
        CODEMAP_RUNTIME=runtime, CODEMAP_LOG_DIR=str(logs), CODEMAP_GATE_TIMEOUT=gate_timeout, TMPDIR=str(tmp_path)
    )
    script = scan_query if engine == "query" else scan_index
    outcome = []
    original = json.loads(index.read_text())

    def build_while_contended(_path):
        """Keep the real writer lease held throughout the competing CLI invocation."""
        outcome.append(
            subprocess.run(
                [sys.executable, str(script), *cli_args, *engine_args],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
            )
        )
        return original

    rwgate.write_index(index, build_while_contended)

    assert outcome[0].returncode == exit_code, outcome[0].stderr
    records = _records(logs, "cli*.jsonl")
    assert len(records) == 1
    assert records[0]["exit_code"] == outcome[0].returncode
    assert records[0]["result"]["error"] == error


@pytest.mark.integration
@pytest.mark.parametrize(
    ("arguments", "exit_code", "result_key"),
    [
        pytest.param(["central"], 0, "central", id="success"),
        pytest.param(["symbol", "missing_symbol"], 1, "error", id="failure"),
    ],
)
def test_unwritable_log_dir_keeps_query_exit(arguments, exit_code, result_key, project, scan_query, tmp_path):
    """A file where a log directory belongs neither breaks success nor masks failure.

    ``CODEMAP_LOG_DIR`` points at a regular file, so every telemetry write fails; the query must still print its JSON
    answer and exit with the code it would have used without logging.
    """
    root, _ = project
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")
    env = _logging_env(CODEMAP_LOG_DIR=str(blocked))

    result = _run_cli(scan_query, ["--no-heal", *arguments], cwd=root, env=env)

    assert result.returncode == exit_code, result.stderr
    assert json.loads(result.stdout)[result_key]


_EMBEDDED_ENGINE_SCRIPT = """\
import sys, signal
sys.path.insert(0, {src!r})
from codemap_py import {engine}
sys.argv = ['engine', *{args!r}]
{setup}
previous = signal.getsignal(signal.SIGALRM)
{engine}.main()
remaining, interval = signal.getitimer(signal.ITIMER_REAL)
signal.alarm(0)
assert {check}
assert signal.getsignal(signal.SIGALRM) == previous
"""


@pytest.mark.integration
@pytest.mark.skipif(not hasattr(signal, "getitimer"), reason="Pending SIGALRM timer inspection requires getitimer.")
@pytest.mark.parametrize(
    ("engine", "engine_args"),
    [
        pytest.param("query", ["--timeout", "5", "--no-heal", "central"], id="query"),
        pytest.param("graph", ["--timeout", "5", "--incremental"], id="graph"),
    ],
)
@pytest.mark.parametrize(
    ("setup", "check"),
    [
        pytest.param("", "remaining == interval == 0", id="no-parent-timer"),
        pytest.param(
            "signal.setitimer(signal.ITIMER_REAL, 20, 4)",
            "0 < remaining < 20 and interval == 4",
            id="parent-timer-preserved",
        ),
    ],
)
def test_embedded_engine_releases_its_timeout(engine, engine_args, setup, check, project, scan_query):
    """An engine clears its own timer but preserves the caller's remaining periodic timer.

    The engine runs embedded in a fresh interpreter; afterwards the real-time itimer and the SIGALRM handler must be
    what the caller left (cleared, or the caller's own periodic timer with its remaining time and interval).
    """
    root, _ = project
    code = _EMBEDDED_ENGINE_SCRIPT.format(
        src=str(scan_query.parent.parent / "src"), engine=engine, args=engine_args, setup=setup, check=check
    )

    result = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
