"""Public CLI attempts retain terminal outcomes across host runtimes."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys

import pytest

from codemap_py import rwgate


@pytest.mark.integration
@pytest.mark.parametrize("runtime", ["claude", "codex", "direct"])
@pytest.mark.parametrize(
    ("engine", "arguments", "exit_code", "command"),
    [
        pytest.param("query", ["central", "--invalid-option"], 2, "query", id="query-parse-failure"),
        pytest.param("index", ["--invalid-option"], 2, "index", id="index-parse-failure"),
        pytest.param("query", ["--no-heal", "central"], 0, "central", id="query-success"),
        pytest.param("query", ["--no-heal", "symbol", "missing_symbol"], 1, "symbol", id="lookup-failure"),
        pytest.param("index", ["--incremental"], 0, "index", id="index-success"),
    ],
)
def test_terminal_outcome(runtime, engine, arguments, exit_code, command, project, scan_query, scan_index, tmp_path):
    """Failures before output and ordinary answers each produce exactly one final record."""
    root, _ = project
    logs = tmp_path / "logs"
    env = {
        **os.environ,
        "CODEMAP_LOGGING": "true",
        "CODEMAP_LOG_DIR": str(logs),
        "CODEMAP_RUNTIME": runtime,
        "TMPDIR": str(tmp_path),
        "CODEX_THREAD_ID": "test-thread",
    }
    script = scan_query if engine == "query" else scan_index
    result = subprocess.run(
        [sys.executable, str(script), *arguments], cwd=root, env=env, capture_output=True, text=True
    )
    assert result.returncode == exit_code, result.stderr
    records = [json.loads(line) for path in logs.rglob("*.jsonl") for line in path.read_text().splitlines()]
    assert len(records) == 1, records
    record = records[0]
    assert record["runtime"] == runtime
    assert record["project"] == root.resolve().as_posix()
    assert record["cmd"] == command
    assert record["argv"] == arguments
    assert record["exit_code"] == exit_code
    if exit_code:
        assert record["result"]["error"]
    elif engine == "query":
        assert record["result"]["central"]
    else:
        assert record["result"]["modules_indexed"] > 0


@pytest.mark.integration
@pytest.mark.parametrize("runtime", ["claude", "codex", "direct"])
@pytest.mark.parametrize("engine", ["query", "index"])
@pytest.mark.parametrize(
    "timeout",
    [
        False,
        pytest.param(
            True,
            marks=pytest.mark.skipif(
                not hasattr(signal, "SIGALRM"), reason="Engine timeout uses the optional SIGALRM capability."
            ),
        ),
    ],
)
def test_busy_gate_and_timeout_are_terminal_outcomes(
    runtime, engine, timeout, project, scan_query, scan_index, tmp_path
):
    """A real competing writer produces one logged gate rejection or handled timeout."""
    root, index = project
    logs = tmp_path / "logs"
    env = {
        **os.environ,
        "CODEMAP_RUNTIME": runtime,
        "CODEMAP_LOGGING": "true",
        "CODEMAP_LOG_DIR": str(logs),
        "CODEMAP_GATE_TIMEOUT": "10" if timeout else "0.05",
        "TMPDIR": str(tmp_path),
    }
    args = ["--timeout", "1"] if timeout else []
    if engine == "query":
        args += ["--no-heal", "central"]
    command = [sys.executable, str(scan_query if engine == "query" else scan_index), *args]
    outcome = []
    original = json.loads(index.read_text())

    def build_while_contended(_path):
        """Keep the real writer lease held throughout the competing CLI invocation."""
        outcome.append(subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=15))
        return original

    rwgate.write_index(index, build_while_contended)
    assert outcome[0].returncode == (2 if timeout else 1), outcome[0].stderr
    records = [json.loads(line) for path in logs.rglob("cli*.jsonl") for line in path.read_text().splitlines()]
    assert len(records) == 1
    assert records[0]["exit_code"] == outcome[0].returncode
    assert records[0]["result"]["error"] == ("timeout" if timeout else "index_busy")


@pytest.mark.integration
@pytest.mark.parametrize("failure", [False, True])
def test_unwritable_telemetry_never_changes_query_exit(failure, project, scan_query, tmp_path):
    """A file where a log directory belongs neither breaks success nor masks failure."""
    root, _ = project
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")
    env = {**os.environ, "CODEMAP_LOGGING": "true", "CODEMAP_LOG_DIR": str(blocked)}
    args = ["symbol", "missing_symbol"] if failure else ["central"]
    result = subprocess.run(
        [sys.executable, str(scan_query), "--no-heal", *args], cwd=root, env=env, capture_output=True, text=True
    )
    assert result.returncode == int(failure), result.stderr
    assert json.loads(result.stdout).get("error") if failure else json.loads(result.stdout)["central"]


@pytest.mark.integration
@pytest.mark.skipif(not hasattr(signal, "getitimer"), reason="Pending SIGALRM timer inspection requires getitimer.")
@pytest.mark.parametrize("engine", ["query", "graph"])
@pytest.mark.parametrize("parent_timer", [False, True])
def test_embedded_engine_releases_its_timeout(engine, parent_timer, project, scan_query):
    """An engine clears its own timer but preserves the caller's remaining periodic timer."""
    root, _ = project
    src = scan_query.parent.parent / "src"
    args = ["--timeout", "5", "--no-heal", "central"] if engine == "query" else ["--timeout", "5", "--incremental"]
    setup = "signal.setitimer(signal.ITIMER_REAL,20,4);" if parent_timer else ""
    check = "0 < remaining < 20 and interval == 4" if parent_timer else "remaining == interval == 0"
    code = f"import sys,signal; sys.path.insert(0,{str(src)!r}); from codemap_py import {engine}; sys.argv=['engine',*{args!r}]; {setup} previous=signal.getsignal(signal.SIGALRM); {engine}.main(); remaining,interval=signal.getitimer(signal.ITIMER_REAL); signal.alarm(0); assert {check}; assert signal.getsignal(signal.SIGALRM) == previous"
    result = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
