"""Deterministic public-behavior tests for the portable bridge runtime."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from _bridge_link_probes import directory_symlinks_supported, hardlinks_supported

BIN_ROOT = Path(__file__).resolve().parents[1] / "bin"
if str(BIN_ROOT) not in sys.path:
    sys.path.insert(0, str(BIN_ROOT))

import bridge_call  # noqa: E402  (loaded from the installed-plugin-equivalent bin directory)
import bridge_diagnose  # noqa: E402  (shares bridge_call's local import seam)
import bridge_mcp  # noqa: E402  (shares bridge_call's local import seam)

DIRECTORY_SYMLINKS_SUPPORTED = directory_symlinks_supported()
HARDLINKS_SUPPORTED = hardlinks_supported()


def _request(tmp_path: Path, **overrides: Any) -> bridge_call.Request:
    """Build one small local request with explicit, deterministic routing values.

    Examples:
        >>> request = _request(getfixture("tmp_path"))
        >>> request.verb, request.direction
        ('advise', 'claude_to_codex')
    """
    values: dict[str, Any] = {
        "verb": "advise",
        "task": "Report completion.",
        "model": "test-model",
        "effort": "medium",
        "timeout_seconds": 1.0,
        "depth": 0,
        "run_id": "run-fixed",
        "workspace": tmp_path,
        "direction": "claude_to_codex",
    }
    values.update(overrides)
    return bridge_call.Request(**values)


def _core(status: str = "complete", *, details: list[str] | None = None, **overrides: object) -> dict[str, object]:
    """Return a valid peer result fixture with optional transcript-only detail.

    Examples:
        >>> _core("failed", details=["timeout"])["status"]
        'failed'
        >>> _core()["findings"]
        []
    """
    core: dict[str, object] = {
        "status": status,
        "verdict": "Finished deterministically.",
        "findings": [],
        "files_touched": [],
        "remaining": [],
        "blockers": [],
        "details": details or [],
    }
    core.update(overrides)
    return core


def _outcome(
    *, core: dict[str, object] | None = None, error: str | None = None, timed_out: bool = False
) -> bridge_call.ChildOutcome:
    """Create a fake child result using the real JSONL parser's event shapes.

    Examples:
        >>> result = _outcome(error="unsupported effort")
        >>> result.returncode, result.timed_out
        (1, False)
    """
    records: list[dict[str, object]] = [{"type": "thread.started", "thread_id": "session-fixed"}]
    if core is not None:
        records.append({"type": "item.completed", "item": {"text": json.dumps(core)}})
        records.append({"type": "turn.completed", "usage": {"input": 3, "output": 2, "cost": 0.01}})
    if error is not None:
        records.append(
            {
                "type": "turn.failed",
                "error": {"code": "unsupported_value", "param": "reasoning.effort", "message": error},
            }
        )
    return bridge_call.ChildOutcome(
        "\n".join(json.dumps(item) for item in records), "", 0 if core else 1, timed_out, None
    )


def _claude_outcome(core: dict[str, object]) -> bridge_call.ChildOutcome:
    """Create one Claude print-mode response at the provider process boundary.

    Examples:
        >>> result = _claude_outcome({"status": "complete"})
        >>> json.loads(result.stdout)["structured_output"]["status"]
        'complete'
    """
    return bridge_call.ChildOutcome(
        json.dumps({"structured_output": core, "usage": {"input": 3, "output": 2}}), "", 0, False, None
    )


def _process_exists(pid: int) -> bool:
    """Report process liveness without using Windows os.kill termination semantics."""
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        return result.returncode == 0 and f'"{pid}"' in result.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _feed_chunks(
    output: bridge_call._ChildOutputBuffer, data: bytes, chunk_size: int, *, stop_when_limited: bool = False
) -> list[tuple[int, int, bool]]:
    """Append data to stdout in chunks; return (pending bytes, captured bytes, output limited) after each chunk."""
    states: list[tuple[int, int, bool]] = []
    for offset in range(0, len(data), chunk_size):
        output.append("stdout", data[offset : offset + chunk_size])
        states.append((len(output.pending_stdout), output.captured_bytes, output.output_limited.is_set()))
        if stop_when_limited and states[-1][2]:
            break
    return states


def _append_events(output: bridge_call._ChildOutputBuffer, *events: dict[str, Any]) -> None:
    """Append each event as its own newline-terminated JSONL chunk on stdout."""
    for event in events:
        output.append("stdout", (json.dumps(event) + "\n").encode())


def _final_result_event() -> dict[str, Any]:
    """Codex agent-message event carrying the canonical complete core result."""
    return {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(_core())}}


_PRIVATE_MODES = (None, None) if os.name == "nt" else (0o600, 0o700)


def _private_mode(path: Path) -> int | None:
    """Return the permission bits where POSIX modes are meaningful, None on Windows."""
    return None if os.name == "nt" else stat.S_IMODE(path.stat().st_mode)


def _wait_for(condition: Callable[[], bool], timeout: float, interval: float = 0.02) -> bool:
    """Poll a real-process condition until it holds or the timeout passes; return its final value."""
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        time.sleep(interval)
    return condition()


def _force_kill(pid: int) -> None:
    """Force-kill a leaked fixture process (its whole tree on Windows) when it still exists."""
    if not _process_exists(pid):
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        os.kill(pid, bridge_call.signal.SIGKILL)


@pytest.fixture
def pid_files() -> Iterator[list[Path]]:
    """Register pid files whose recorded processes are force-killed at teardown, pass or fail."""
    registered: list[Path] = []
    yield registered
    for path in registered:
        if path.is_file():
            _force_kill(int(path.read_text(encoding="utf-8")))


@pytest.fixture
def started_jobs(tmp_path: Path) -> Iterator[list[str]]:
    """Register background job ids cancelled at teardown so a failed test never leaves a supervisor running."""
    registered: list[str] = []
    yield registered
    for job_id in registered:
        bridge_call.cancel_job(tmp_path, job_id)


@pytest.fixture
def launched_groups() -> Iterator[list[subprocess.Popen[bytes]]]:
    """Register real processes whose process groups are killed and whose pipes are closed at teardown."""
    registered: list[subprocess.Popen[bytes]] = []
    yield registered
    for process in registered:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, bridge_call.signal.SIGKILL)
        process.wait(timeout=3.0)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()


class _LaunchRecorder:
    """Record the real child processes and reader threads that ``bridge_call._run_child`` starts."""

    def __init__(self) -> None:
        self.processes: list[subprocess.Popen[bytes]] = []
        self.readers: list[threading.Thread] = []
        self._spawn = bridge_call._spawn_child
        self._thread_start = threading.Thread.start

    def spawn(self, command: list[str], workspace: Path, timeout: float) -> Any:
        """Launch the genuine subprocess and keep it for exit-state assertions."""
        process = self._spawn(command, workspace, timeout)
        assert not isinstance(process, bridge_call.ChildOutcome)
        self.processes.append(process)
        return process

    def start_reader(self, reader: threading.Thread) -> None:
        """Start an actual reader thread without replacing its work."""
        self._thread_start(reader)
        self.readers.append(reader)

    def reap(self) -> None:
        """Kill surviving children, join readers and close their pipes so no test leaks a process, thread or file."""
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
        for reader in self.readers:
            reader.join(timeout=5)
        # Closing a pipe under a reader still blocked in read() can hang, so only close once every reader has finished.
        if not any(reader.is_alive() for reader in self.readers):
            for process in self.processes:
                for stream in (process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()


@pytest.fixture
def launches(monkeypatch: pytest.MonkeyPatch) -> Iterator[_LaunchRecorder]:
    """Record real child launches and reader starts in ``_run_child``; reap them at teardown."""
    recorder = _LaunchRecorder()
    monkeypatch.setattr(bridge_call, "_spawn_child", recorder.spawn)
    monkeypatch.setattr(threading.Thread, "start", lambda reader: recorder.start_reader(reader))
    yield recorder
    recorder.reap()


class _CompletedProcess:
    """Popen stand-in for a child that already finished with empty output."""

    returncode = 0

    def communicate(self, timeout: float) -> tuple[str, str]:
        """Return empty streams for a child that produced no output."""
        return "", ""


@pytest.fixture
def fake_popen_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], dict[str, Any]]]:
    """Replace Popen with a recorder that returns a finished child; yields each (command, kwargs) launch."""
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def _fake_popen(command: list[str], **kwargs: Any) -> _CompletedProcess:
        """Record the launch and return the protocol-compatible finished process."""
        calls.append((command, kwargs))
        return _CompletedProcess()

    monkeypatch.setattr(bridge_call.subprocess, "Popen", _fake_popen)
    return calls


def _run_mcp_stdio(tmp_path: Path, messages: str, **kwargs: Any) -> list[dict[str, Any]]:
    """Run the real bridge_mcp --stdio server on the messages and return its parsed JSON response lines."""
    result = subprocess.run(
        [sys.executable, str(BIN_ROOT / "bridge_mcp.py"), "--stdio"],
        input=messages + "\n",
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
        **kwargs,
    )
    assert result.returncode == 0, result.stderr
    return [json.loads(line) for line in result.stdout.splitlines()]


def _write_fake_codex_launcher(fake_bin: Path, peer_script: Path) -> None:
    """Install a ``codex`` launcher in fake_bin that runs peer_script with this interpreter."""
    if os.name == "nt":
        (fake_bin / "codex.cmd").write_text(f'@"{sys.executable}" "{peer_script}" %*\r\n', encoding="utf-8", newline="")
        return
    fake_codex = fake_bin / "codex"
    fake_codex.write_text(
        "#!" + sys.executable + "\n" + peer_script.read_text(encoding="utf-8"), encoding="utf-8", newline="\n"
    )
    fake_codex.chmod(0o755)


class TestBuildChildEnvironment:
    """Child environment is an allowlist, so session credentials never reach a peer process."""

    def test_drops_unrelated_secrets(self) -> None:
        """Credentials the child has no use for are removed.

        A bridge call runs inside whatever session the operator is already using, so the parent environment routinely
        holds cloud and service credentials for unrelated work. Inheriting it wholesale hands all of them to a peer CLI
        running model-authored code.
        """
        source = {
            "PATH": "/usr/bin",
            "AWS_SECRET_ACCESS_KEY": "aws-secret",
            "KAGGLE_KEY": "kaggle-secret",
            "GITHUB_TOKEN": "gh-secret",
            "MY_APP_DB_PASSWORD": "db-secret",
        }

        result = bridge_call.build_child_environment(source, platform="linux")

        assert result == {"PATH": "/usr/bin"}

    def test_keeps_provider_auth(self) -> None:
        """Provider authentication survives, or every bridge call would fail closed.

        The source environment holds both vendors' keys, a Codex home and an unrelated variable. The default allowlist
        keeps the provider variables and drops the unrelated one.
        """
        source = {"OPENAI_API_KEY": "k", "ANTHROPIC_API_KEY": "a", "CODEX_HOME": "/c", "SOME_OTHER": "x"}

        result = bridge_call.build_child_environment(source, platform="linux")

        assert result == {"OPENAI_API_KEY": "k", "ANTHROPIC_API_KEY": "a", "CODEX_HOME": "/c"}

    def test_keeps_windows_startup_keys_on_win32(self) -> None:
        """On Windows the interpreter start-up keys must survive the filter.

        A child Python launched without ``SystemRoot``/``COMSPEC`` aborts during start-up with
        ``_Py_HashRandomization_Init: failed to get random numbers``, so filtering them out would break every bridge
        call on that host while passing on POSIX.
        """
        source = {"PATH": "p", "SystemRoot": "C:\\Windows", "COMSPEC": "cmd.exe", "PATHEXT": ".EXE", "NOPE": "x"}

        result = bridge_call.build_child_environment(source, platform="win32")

        assert result == {"PATH": "p", "SystemRoot": "C:\\Windows", "COMSPEC": "cmd.exe", "PATHEXT": ".EXE"}

    def test_windows_keys_dropped_on_posix(self) -> None:
        """The Windows-only keys are not carried onto POSIX hosts, where nothing reads them.

        The source holds PATH and the Windows-only COMSPEC. For a Linux platform only PATH is carried over.
        """
        result = bridge_call.build_child_environment({"PATH": "p", "COMSPEC": "cmd.exe"}, platform="linux")

        assert result == {"PATH": "p"}

    def test_operator_extra_keys_are_allowed_through(self) -> None:
        """Variables named in BRIDGE_CHILD_ENV_EXTRA reach the child.

        A host behind a proxy or on an enterprise provider path needs variables the fixed sets cannot anticipate.
        Without this the bridge is unusable there and the failure surfaces as an opaque provider error in the child.
        """
        source = {
            "PATH": "/usr/bin",
            "BRIDGE_CHILD_ENV_EXTRA": "HTTPS_PROXY, NO_PROXY",
            "HTTPS_PROXY": "http://proxy:3128",
            "NO_PROXY": "localhost",
            "AWS_SECRET_ACCESS_KEY": "still-dropped",
        }

        result = bridge_call.build_child_environment(source, platform="linux")

        assert result == {"PATH": "/usr/bin", "HTTPS_PROXY": "http://proxy:3128", "NO_PROXY": "localhost"}

    def test_operator_extra_marker_is_not_forwarded(self) -> None:
        """The marker variable itself never reaches the child.

        Forwarding it would tell a peer process which policy produced its own environment, and a nested bridge call
        would inherit the widening rather than re-deriving it from the operator's host configuration.
        """
        source = {"PATH": "/usr/bin", "BRIDGE_CHILD_ENV_EXTRA": "BRIDGE_CHILD_ENV_EXTRA,HTTPS_PROXY"}

        result = bridge_call.build_child_environment(source, platform="linux")

        assert "BRIDGE_CHILD_ENV_EXTRA" not in result

    def test_malformed_extra_entry_is_skipped_not_fatal(self) -> None:
        """A typo in the operator's list drops that entry and keeps the rest.

        The list is host configuration re-read on every call; raising on one bad name would take the bridge down for
        every request rather than losing one variable.
        """
        source = {"PATH": "/usr/bin", "BRIDGE_CHILD_ENV_EXTRA": "bad name,,9INVALID,NO_PROXY", "NO_PROXY": "localhost"}

        result = bridge_call.build_child_environment(source, platform="linux")

        assert result == {"PATH": "/usr/bin", "NO_PROXY": "localhost"}

    def test_absent_key_is_omitted_not_blanked(self) -> None:
        """An allowlisted key missing from the source is left out rather than set empty.

        Setting it to "" would make an unset provider key look configured-but-empty to the child, turning a clear auth
        failure into a confusing one.
        """
        result = bridge_call.build_child_environment({"PATH": "p"}, platform="linux")

        assert "OPENAI_API_KEY" not in result

    @pytest.mark.parametrize(
        ("host", "expected"),
        [
            pytest.param("codex", {"OPENAI_API_KEY": "k", "CODEX_HOME": "/c"}, id="codex-gets-openai-only"),
            pytest.param(
                "claude", {"ANTHROPIC_API_KEY": "a", "CLAUDE_CONFIG_DIR": "/d"}, id="claude-gets-anthropic-only"
            ),
        ],
    )
    def test_host_receives_only_its_own_provider_keys(self, host: str, expected: dict[str, str]) -> None:
        """A named child host receives its own vendor's credentials and never the other vendor's.

        A model-directed command inside a Codex child could otherwise read the Anthropic token, and a Claude child the
        OpenAI key, though neither child authenticates with them.
        """
        source = {"OPENAI_API_KEY": "k", "CODEX_HOME": "/c", "ANTHROPIC_API_KEY": "a", "CLAUDE_CONFIG_DIR": "/d"}

        result = bridge_call.build_child_environment(source, platform="linux", host=host)

        assert result == expected


def test_spawned_codex_child_does_not_receive_anthropic_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_popen_calls: list[tuple[list[str], dict[str, Any]]]
) -> None:
    """The host name in the child command selects the provider key set at spawn time.

    The spawn seam is where the narrowing must hold: a Codex child launched with the session's Anthropic token in its
    environment could hand it to a model-directed shell command.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    monkeypatch.setattr(bridge_call, "_resolved_command", lambda command: command)

    bridge_call._run_child(["codex", "exec"], tmp_path, timeout=1.0)

    environment = fake_popen_calls[0][1]["env"]
    assert environment["OPENAI_API_KEY"] == "openai-key"
    assert "ANTHROPIC_API_KEY" not in environment


def test_background_supervisor_argv_omits_task_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The detached supervisor's command line carries no task text.

    Other local users can read any process's argv; the task, which may quote repository content, stays in the job record
    the launcher writes first.
    """
    commands: list[list[str]] = []
    monkeypatch.setattr(
        bridge_call.subprocess, "Popen", lambda command, **kwargs: commands.append(command) or SimpleNamespace(pid=1)
    )
    request = _request(tmp_path, verb="implement", background=True, task="Secret-bearing task text.")

    bridge_call.start_background(request)

    assert "--task" not in commands[0]
    assert "Secret-bearing task text." not in commands[0]


def test_supervisor_without_task_flag_loads_task_from_job_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A supervisor launched without --task runs the task its launcher stored in the job record.

    This is the launch path start_background now uses; failing to load the stored task would leave every background job
    erroring with "one of --task or --task-file is required".
    """
    monkeypatch.setattr(bridge_call.subprocess, "Popen", lambda command, **kwargs: SimpleNamespace(pid=1))
    started = bridge_call.start_background(
        _request(tmp_path, verb="implement", background=True, task="Stored task text.")
    )
    seen: list[str] = []

    def _fake_run_request(request: bridge_call.Request, **kwargs: object) -> dict[str, Any]:
        """Capture the task the supervisor resolved and return a finished envelope."""
        seen.append(request.task)
        return _finished_envelope()

    monkeypatch.setattr(bridge_call, "run_request", _fake_run_request)

    exit_code = bridge_call.main(
        ["implement", "--job-id", started["job_id"], "--supervisor", "--workspace", str(tmp_path.resolve())]
    )

    assert exit_code == 0
    assert seen == ["Stored task text."]


def test_job_record_and_artifact_directories_are_private(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Job records and their directories are readable only by the current user.

    The record holds the full task text and later the result; a umask-default record on a shared host exposes both.
    """
    monkeypatch.setattr(bridge_call.subprocess, "Popen", lambda command, **kwargs: SimpleNamespace(pid=1))

    started = bridge_call.start_background(_request(tmp_path, verb="implement", background=True))

    record = tmp_path.resolve() / ".temp" / "bridge" / "jobs" / f"{started['job_id']}.json"
    assert record.is_file()
    assert (_private_mode(record), _private_mode(record.parent)) == _PRIVATE_MODES


def test_background_launch_reaps_its_supervisor_once_it_exits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Collect the detached supervisor's exit status so the launcher leaves no unreaped child behind.

    Popen starts a real child that exits at once in place of the supervisor. The test only reads its return code and
    never waits on it, so the status can be collected only by the launcher itself; an unreaped child stays a zombie and
    warns "subprocess N is still running" when its Popen object is discarded.
    """
    launched: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen

    def _short_lived_popen(command: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        """Start a real child that exits immediately in place of the supervisor."""
        process = real_popen([sys.executable, "-c", "pass"], **kwargs)
        launched.append(process)
        return process

    monkeypatch.setattr(bridge_call.subprocess, "Popen", _short_lived_popen)

    bridge_call.start_background(_request(tmp_path, verb="implement", background=True))

    assert _wait_for(lambda: launched[0].returncode is not None, 10.0)


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("git") is None, reason="requires git on PATH")
def test_workspace_state_does_not_run_repository_fsmonitor_hook(tmp_path: Path) -> None:
    """The supervisor's git status never runs a core.fsmonitor command planted in the repository config.

    A write-capable child can edit .git/config; without the override, the supervisor would execute that command outside
    the child's sandbox when it snapshots the workspace.
    """
    marker = tmp_path / "fsmonitor-ran"
    hook = tmp_path / "hook.sh"
    hook.write_text(f'#!/bin/sh\ntouch "{marker.as_posix()}"\nexit 1\n', encoding="utf-8", newline="\n")
    hook.chmod(0o755)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "core.fsmonitor", hook.as_posix()], check=True)

    bridge_call._workspace_state(tmp_path)

    assert not marker.exists()


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        pytest.param("--model", "-c", id="model-option-shaped"),
        pytest.param("--session-id", "--last", id="session-id-option-shaped"),
        pytest.param("--run-id", "run id with spaces", id="run-id-free-text"),
    ],
)
def test_caller_identifiers_reject_option_and_free_text_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], flag: str, value: str
) -> None:
    """Model, session and run identifiers are refused unless they look like identifiers.

    A value starting with a dash would be parsed as a child CLI option, and free text reaches the child prompt and logs.
    """
    exit_code = bridge_call.main(["implement", "--task", "Do it.", f"{flag}={value}", "--workspace", str(tmp_path)])
    response = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert response["error"].startswith(f"{flag} must match")


def test_task_file_over_budget_is_refused_before_full_read(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A task file larger than the transport budget is refused from a bounded read.

    Reading the whole file first would let a huge file or an endless device exhaust memory before the budget check.
    """
    task_file = tmp_path / "task.txt"
    task_file.write_bytes(b"x" * (bridge_call.MAX_TASK_UTF8_BYTES + 1))

    exit_code = bridge_call.main(["advise", "--task-file", str(task_file), "--workspace", str(tmp_path)])
    response = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert response["error"] == "task exceeds Bridge's encoded transport budget"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires FIFO creation capability")
def test_task_file_refuses_a_fifo_without_blocking(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A FIFO passed as --task-file is refused at once instead of waiting for a writer.

    The task file path is a named pipe with no writer. The CLI exits 2 with a regular-file error instead of blocking on
    open, so a hostile or mistaken path cannot hang the launcher.
    """
    fifo = tmp_path / "task.fifo"
    os.mkfifo(fifo)

    exit_code = bridge_call.main(["advise", "--task-file", str(fifo), "--workspace", str(tmp_path)])
    response = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert response["error"] == "--task-file must be a regular file"


def test_fresh_review_and_resume_argv_preserve_routing_contract(tmp_path: Path) -> None:
    """Prevent subtle Codex argv drift that changes sandbox, review, or resume behavior.

    Codex argv is built for advise, review, implement and a resumed implement, plus two invalid resume requests. Sandbox
    mode, network flag, ephemeral and git-check switches, resume session id and the refusal reasons are pinned together
    because any drift changes what the peer may touch.
    """
    schema_path = tmp_path / "core-schema.json"
    schema_path.write_text("{}", encoding="utf-8")

    fresh = bridge_call.build_codex_argv(_request(tmp_path, verb="advise"), schema_path)
    review = bridge_call.build_codex_argv(_request(tmp_path, verb="review"), schema_path)
    implement = bridge_call.build_codex_argv(_request(tmp_path, verb="implement"), schema_path)
    resumed = bridge_call.build_codex_argv(
        _request(tmp_path, verb="implement", session_id="session-fixed", origin_workspace=tmp_path), schema_path
    )

    assert fresh[:2] == ["codex", "exec"]
    assert ["-s", "read-only"] == fresh[fresh.index("-s") : fresh.index("-s") + 2]
    assert "--ephemeral" in fresh
    assert "--ignore-user-config" in fresh
    assert all(
        "sandbox_workspace_write.network_access=false" in command for command in (fresh, review, implement, resumed)
    )
    assert 'model_reasoning_effort="medium"' in fresh
    assert all("--skip-git-repo-check" in command for command in (fresh, review, implement))
    assert review[:2] == ["codex", "exec"]
    assert review[:3] != ["codex", "exec", "review"]
    assert ["-s", "read-only"] == review[review.index("-s") : review.index("-s") + 2]
    assert "--ephemeral" in review
    assert "adversarial" in review[-1].lower()
    assert "review" in review[-1].lower()
    assert resumed[:4] == ["codex", "exec", "resume", "session-fixed"]
    assert "--last" not in resumed
    assert "--skip-git-repo-check" in resumed
    assert 'sandbox_mode="workspace-write"' in resumed
    with pytest.raises(ValueError, match="only implement"):
        bridge_call.build_codex_argv(
            _request(tmp_path, session_id="session-fixed", origin_workspace=tmp_path), schema_path
        )
    with pytest.raises(ValueError, match="originating workspace"):
        bridge_call.build_codex_argv(
            _request(tmp_path, verb="implement", session_id="session-fixed", origin_workspace=tmp_path / "other"),
            schema_path,
        )


class TestGithubReadNetwork:
    """The opt-in ``github-read`` posture swaps the egress pin for a method- and domain-scoped permission profile."""

    @pytest.fixture(autouse=True)
    def _verified_platform(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Run as on the one host where enforcement was probed; the refusal elsewhere has its own tests."""
        monkeypatch.setattr(bridge_call.sys, "platform", "darwin")

    @pytest.fixture
    def schema_path(self, tmp_path: Path) -> Path:
        """Minimal schema file the argv builder only needs to reference."""
        path = tmp_path / "core-schema.json"
        path.write_text("{}", encoding="utf-8")
        return path

    @staticmethod
    def _profile(command: list[str]) -> str:
        """Return the profile name the argv selects with ``default_permissions``."""
        selected = next(part for part in command if part.startswith("default_permissions="))
        return json.loads(selected.split("=", 1)[1])

    @pytest.mark.parametrize(
        ("verb", "base"),
        [
            pytest.param("advise", ":read-only", id="advise"),
            pytest.param("review", ":read-only", id="review"),
            pytest.param("implement", ":workspace", id="implement"),
        ],
    )
    def test_profile_replaces_pin_and_states_every_key(
        self, tmp_path: Path, schema_path: Path, verb: str, base: str
    ) -> None:
        """Opting in drops the pin and `-s`, and states base, protected paths, method limit, domain and proxy feature.

        `codex exec` enforces a domain allowlist only through a profile with the `network_proxy` feature on; a profile
        without `network.mode="limited"` lets an authenticated POST through; and a profile extending `:workspace` loses
        the built-in write protection of `.git`, `.codex` and `.agents`. Each is a failure a live probe showed.
        """
        command = bridge_call.build_codex_argv(_request(tmp_path, verb=verb, network="github-read"), schema_path)
        profile = f"permissions.{self._profile(command)}"

        assert "sandbox_workspace_write.network_access=false" not in command
        assert "-s" not in command
        assert f'{profile}.extends="{base}"' in command
        assert (
            f'{profile}.filesystem={{":workspace_roots"={{".git"="read",".codex"="read",".agents"="read"}}}}' in command
        )
        assert f'{profile}.network.mode="limited"' in command
        assert f'{profile}.network.domains={{"api.github.com"="allow"}}' in command
        assert command[command.index("--enable") + 1] == "network_proxy"
        assert "--ignore-user-config" in command

    def test_profile_name_is_random_per_build_and_used_consistently(self, tmp_path: Path, schema_path: Path) -> None:
        """A random name leaves no configuration layer a profile to predefine, even for a caller-chosen run id.

        A trusted project config that declares a profile of the same name adds its domains to ours, so the name must not
        be derivable from anything a caller or a repository can know in advance.
        """
        first = bridge_call.build_codex_argv(_request(tmp_path, run_id="run-a", network="github-read"), schema_path)
        again = bridge_call.build_codex_argv(_request(tmp_path, run_id="run-a", network="github-read"), schema_path)
        used = {part.split("=", 1)[0].split(".")[1] for part in first if part.startswith("permissions.")}

        assert self._profile(first) != self._profile(again)
        assert used == {self._profile(first)}

    def test_default_keeps_the_egress_pin(self, tmp_path: Path, schema_path: Path) -> None:
        """Without the opt-in the argv is unchanged: pinned off, legacy sandbox flag, no profile.

        A default advise request builds argv with no `network` posture. Existing callers must see exactly the old egress
        pin, with no proxy feature or `permissions.*` override leaking in from the new opt-in path.
        """
        command = bridge_call.build_codex_argv(_request(tmp_path), schema_path)

        assert "sandbox_workspace_write.network_access=false" in command
        assert "--enable" not in command
        assert not any(part.startswith("permissions.") for part in command)

    def test_resumed_implement_uses_the_profile_not_sandbox_mode(self, tmp_path: Path, schema_path: Path) -> None:
        """A resumed write session keeps the same posture as a fresh one and does not fall back to the pin.

        A resumed implement request opts in to `github-read`. The resume branch builds its own sandbox arguments, so it
        must select the generated profile instead of `sandbox_mode="workspace-write"` with the egress pin.
        """
        command = bridge_call.build_codex_argv(
            _request(tmp_path, verb="implement", session_id="s-1", origin_workspace=tmp_path, network="github-read"),
            schema_path,
        )

        assert 'sandbox_mode="workspace-write"' not in command
        assert "sandbox_workspace_write.network_access=false" not in command
        assert command[command.index("--enable") + 1] == "network_proxy"
        assert self._profile(command).startswith("bridge-github-")

    def test_unknown_mode_is_refused(self, tmp_path: Path, schema_path: Path) -> None:
        """An unrecognised posture never falls back to a wider one.

        A request carries `network="open"`, a value outside the two documented postures. Argv construction must raise
        instead of guessing, because a silent fallback could grant network access nobody asked for.
        """
        with pytest.raises(ValueError, match="network must be one of"):
            bridge_call.build_codex_argv(_request(tmp_path, network="open"), schema_path)

    def test_claude_peer_has_no_equivalent(self, tmp_path: Path, schema_path: Path) -> None:
        """The opt-in is Codex-side only; a reverse-direction request carrying it is refused.

        A `codex_to_claude` request asks for `github-read`. The profile mechanism exists only for Codex, so the Claude
        argv builder must refuse rather than pretend the limit applies to its peer.
        """
        request = _request(tmp_path, direction="codex_to_claude", network="github-read")

        with pytest.raises(ValueError, match="no equivalent for the Claude peer"):
            bridge_call.build_claude_argv(request, schema_path)

    def test_cli_flag_reaches_the_request(self) -> None:
        """`--network github-read` on the command line becomes the request's posture, and omitting it keeps `none`.

        The real parser is run twice, with and without the flag. The posture must travel from the command line into the
        request object, since everything downstream keys off that field.
        """
        parser = bridge_call._parser()
        chosen = parser.parse_args(["advise", "--task", "t", "--network", "github-read"])
        default = parser.parse_args(["advise", "--task", "t"])

        assert bridge_call._request_from_args(chosen).network == "github-read"
        assert bridge_call._request_from_args(default).network == "none"

    def test_bad_cli_value_is_a_json_error_not_an_argparse_exit(self) -> None:
        """A bad value parses, then fails in request validation, so it can reach the caller as a JSON envelope.

        The parser accepts `--network open` without a `choices` restriction. Validation then raises `ValueError`, which
        the CLI turns into a JSON error; an argparse exit would print usage text that callers cannot parse.
        """
        args = bridge_call._parser().parse_args(["advise", "--task", "t", "--network", "open"])

        with pytest.raises(ValueError, match="--network must be one of"):
            bridge_call._request_from_args(args)

    @pytest.mark.parametrize(
        ("network", "expected"),
        [pytest.param("github-read", True, id="opted-in"), pytest.param("none", False, id="default")],
    )
    def test_background_supervisor_argv_carries_the_posture(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, network: str, expected: bool
    ) -> None:
        """The supervisor start command carries `--network github-read` only for an opted-in request.

        The detached supervisor rebuilds its request from argv. With `Popen` stubbed, the recorded command must hold the
        flag for the opted-in case and omit it by default, or the background run would lose its posture.
        """
        commands: list[list[object]] = []

        def _fake_popen(command: list[object], **kwargs: object) -> SimpleNamespace:
            """Record the supervisor argv and return a process with a stable PID."""
            commands.append(command)
            return SimpleNamespace(pid=123)

        monkeypatch.setattr(bridge_call.subprocess, "Popen", _fake_popen)
        bridge_call.start_background(_request(tmp_path, verb="implement", background=True, network=network))

        argv = [str(part) for part in commands[0]]
        assert ("--network" in argv and argv[argv.index("--network") + 1] == "github-read") is expected

    def test_background_refuses_an_unknown_posture_before_a_job_exists(self, tmp_path: Path) -> None:
        """A bad value is rejected before any job record is written or supervisor spawned.

        A background implement request carries `network="open"`. Validation must run before the job is persisted, so no
        orphan `.json` record is left under `.temp` for a request that could never start.
        """
        with pytest.raises(ValueError, match="network must be one of"):
            bridge_call.start_background(_request(tmp_path, verb="implement", background=True, network="open"))

        assert not list((tmp_path / ".temp").rglob("*.json"))

    def test_incident_records_the_posture(self, tmp_path: Path) -> None:
        """An incident file shows whether egress was open, so a failed opted-in run is distinguishable afterwards.

        An incident is written for an opted-in timeout. Its `network` field must read `github-read`, because a reader
        triaging the failure otherwise cannot tell whether GitHub access was open during the run.
        """
        paths = bridge_call.BridgePaths(tmp_path.resolve())
        paths.prepare()

        relative = bridge_call._write_incident(
            paths, _request(tmp_path, network="github-read"), "timeout", "budget", "t.txt", None
        )

        assert json.loads((tmp_path / relative).read_text(encoding="utf-8"))["network"] == "github-read"

    @pytest.mark.parametrize(
        ("network", "expected"),
        [pytest.param("github-read", "github-read", id="opted-in"), pytest.param("none", "none", id="default")],
    )
    def test_health_record_carries_the_posture(self, tmp_path: Path, network: str, expected: str) -> None:
        """A successful run leaves a health line that says whether egress was open, so an audit can tell afterwards.

        A health line is appended for a complete envelope under each posture. The last line of `health.jsonl` must carry
        the posture verbatim, for the opted-in case and for the default `none`.
        """
        paths = bridge_call.BridgePaths(tmp_path.resolve())
        paths.prepare()
        envelope = {
            "run_id": "r",
            "verb": "advise",
            "direction": "claude_to_codex",
            "model": "m",
            "effort": "low",
            "cost": None,
            "tokens": {},
            "duration_seconds": 0.1,
            "status": "complete",
            "depth": 0,
        }

        bridge_call._append_health(paths, envelope, network)

        line = (paths.root / "health.jsonl").read_text(encoding="utf-8").splitlines()[-1]
        assert json.loads(line)["network"] == expected

    def test_detached_job_record_carries_the_posture(self, tmp_path: Path) -> None:
        """The job record keeps the posture for status readers; the supervisor itself restarts from argv.

        The serialized request for an opted-in call must include `network`, so a status or audit reader looking at the
        job file sees the same posture the supervisor was started with.
        """
        record = bridge_call._request_json(_request(tmp_path, network="github-read"))

        assert record["network"] == "github-read"

    def test_prompt_tells_the_peer_about_the_limit(self, tmp_path: Path) -> None:
        """An opted-in prompt says GitHub access is read-only; a default prompt does not mention GitHub.

        The peer is told about the limit it runs under so it does not attempt writes that the proxy will refuse. A
        default call must stay silent about GitHub, since no such access exists for it.
        """
        opted = bridge_call._prompt_with_budget(_request(tmp_path, network="github-read"))
        default = bridge_call._prompt_with_budget(_request(tmp_path))

        assert "read requests to api.github.com" in opted
        assert "api.github.com" not in default


class _ChildReachedError(Exception):
    """Raised by a stubbed child launcher to prove a request got that far."""


def _stop_at_child(*args: object) -> None:
    """Stand in for ``_run_child``: stop the request at the point where a real child would start."""
    raise _ChildReachedError


#: Preflight replies that all hold, in probe order: control, .git write, GET, POST, example.com.
_HOLDING_PROBES: list[tuple[int, str]] = [
    (0, ""),
    (1, ""),
    (0, "200 000"),
    (0, "403 000 blocked-by-method-policy"),
    (0, "000 403"),
]


class TestGithubReadGuards:
    """Platform refusal and the per-call preflight that proves the profile still enforces its limits."""

    @pytest.fixture(autouse=True)
    def _verified_platform(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Run as on the one host where enforcement was probed, so the suite passes on every CI runner.

        Tests about the refusal elsewhere set their own platform after this fixture.
        """
        monkeypatch.setattr(bridge_call.sys, "platform", "darwin")

    @pytest.fixture
    def schema_path(self, tmp_path: Path) -> Path:
        """Minimal schema file the argv builder only needs to reference."""
        path = tmp_path / "core-schema.json"
        path.write_text("{}", encoding="utf-8")
        return path

    @pytest.mark.parametrize("platform", [pytest.param("linux", id="linux"), pytest.param("win32", id="windows")])
    def test_unverified_platform_is_refused(
        self, tmp_path: Path, schema_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
    ) -> None:
        """Another host than macOS refuses the opt-in instead of trusting unverified enforcement.

        The platform is faked as linux and as win32 for an opted-in request. Enforcement was probed on macOS only, so
        argv construction must raise rather than hand out an unproven network limit.
        """
        monkeypatch.setattr(bridge_call.sys, "platform", platform)

        with pytest.raises(ValueError, match="verified on darwin only"):
            bridge_call.build_codex_argv(_request(tmp_path, network="github-read"), schema_path)

    def test_default_posture_is_unaffected_by_platform(
        self, tmp_path: Path, schema_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The platform gate applies only to the opt-in; the default pinned posture builds everywhere.

        The platform is faked as linux and a default request builds argv. The egress pin must still be present, so the
        new gate cannot break existing callers on hosts where the opt-in is refused.
        """
        monkeypatch.setattr(bridge_call.sys, "platform", "linux")

        assert "sandbox_workspace_write.network_access=false" in bridge_call.build_codex_argv(
            _request(tmp_path), schema_path
        )

    @pytest.mark.parametrize(
        ("verb", "replies", "expected"),
        [
            pytest.param("advise", _HOLDING_PROBES, None, id="all-hold"),
            pytest.param(
                "advise",
                [*_HOLDING_PROBES[:4], (0, "403 000 blocked-by-allowlist")],
                None,
                id="example-refused-by-allowlist-header",
            ),
            pytest.param("advise", [(2, ""), *_HOLDING_PROBES[1:]], "sandbox start", id="sandbox-never-started"),
            pytest.param("implement", _HOLDING_PROBES, "workspace write", id="workspace-write-did-not-land"),
            pytest.param(
                "advise", [_HOLDING_PROBES[0], (0, ""), *_HOLDING_PROBES[2:]], ".git write refusal", id="git-writable"
            ),
            pytest.param(
                "advise",
                [*_HOLDING_PROBES[:2], (0, "403 000"), *_HOLDING_PROBES[3:]],
                "GET api.github.com",
                id="github-limited",
            ),
            pytest.param(
                "advise",
                [*_HOLDING_PROBES[:3], (0, "403 000"), _HOLDING_PROBES[4]],
                "POST refusal by the proxy",
                id="github-own-403-not-the-proxy",
            ),
            pytest.param(
                "advise",
                [*_HOLDING_PROBES[:3], (0, "400 200"), _HOLDING_PROBES[4]],
                "POST refusal by the proxy",
                id="method-limit-dropped",
            ),
            pytest.param(
                "advise", [*_HOLDING_PROBES[:4], (0, "301 200")], "example.com refusal", id="example-reachable"
            ),
            pytest.param(
                "advise",
                [_HOLDING_PROBES[0], (-1, ""), *_HOLDING_PROBES[2:]],
                "the .git write refusal probe",
                id="git-probe-never-ran",
            ),
        ],
    )
    def test_preflight_reports_the_first_broken_guarantee(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        verb: str,
        replies: list[tuple[int, str]],
        expected: str | None,
    ) -> None:
        """Each guarantee the opt-in documents is probed; any that does not hold blocks the call with its name.

        A Codex release that renames a profile key drops that restriction silently; these are the observable effects.
        A status alone is not trusted: GitHub's own 403 on a POST, or a redirect from example.com, does not pass.
        The control probe first proves the sandbox ran, so a sandbox that never started cannot pass the later probes.
        """
        answers = iter(replies)
        monkeypatch.setattr(bridge_call, "_sandbox_probe", lambda *args, **kwargs: next(answers))
        request = _request(tmp_path, verb=verb, network="github-read", timeout_seconds=60.0)

        result = bridge_call._github_read_preflight(request)

        assert (result.split(" did not")[0] if result else None) == expected

    def test_preflight_never_touches_the_callers_workspace_or_codex_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Probes run in a throwaway workspace with an empty Codex home, never the caller's or the user's.

        The Codex child runs with ``--ignore-user-config``; an empty home keeps the user's config layer out of the probe
        the same way, and keeps the proxy's certificate and logs out of the real home.
        """
        seen: list[tuple[Path, Path, bool]] = []

        def probe(
            overrides: list[str], workspace: Path, command: list[str], *, codex_home: Path, timeout: float
        ) -> tuple[int, str]:
            seen.append((workspace, codex_home, not any(codex_home.iterdir())))
            return (1, "")

        monkeypatch.setattr(bridge_call, "_sandbox_probe", probe)
        bridge_call._github_read_preflight(_request(tmp_path, network="github-read", timeout_seconds=60.0))

        assert seen
        assert all(tmp_path not in [path, *path.parents] for workspace, home, _ in seen for path in (workspace, home))
        assert all(workspace != home and home_empty for workspace, home, home_empty in seen)

    def test_preflight_stays_inside_the_request_budget(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Each probe's timeout is capped by what remains of the request's budget, and a spent budget refuses.

        Clock reads: deadline at 0, first probe at 0 (5 s left), second at 0.5 (4.5 s left), third at 9 (spent).
        """
        timeouts: list[float] = []
        clock = iter([0.0, 0.0, 0.5, 9.0])

        def probe(
            overrides: list[str], workspace: Path, command: list[str], *, codex_home: Path, timeout: float
        ) -> tuple[int, str]:
            timeouts.append(timeout)
            return _HOLDING_PROBES[len(timeouts) - 1]

        monkeypatch.setattr(bridge_call.time, "monotonic", lambda: next(clock))
        monkeypatch.setattr(bridge_call, "_sandbox_probe", probe)

        result = bridge_call._github_read_preflight(_request(tmp_path, network="github-read", timeout_seconds=5.0))

        assert timeouts == [5.0, 4.5]
        assert result == "the request's time budget ran out before the GET api.github.com probe"

    def test_failed_preflight_refuses_before_any_child(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A failed preflight returns a structured refusal and never starts the Codex child.

        The preflight is stubbed to report a broken guarantee and the child launcher fails the test if reached. The
        result must be a `refused` envelope plus an incident naming the `network-preflight` fault.
        """
        monkeypatch.setattr(bridge_call.sys, "platform", "darwin")
        monkeypatch.setattr(bridge_call, "_github_read_preflight", lambda request: "a write to .git was allowed")
        monkeypatch.setattr(bridge_call, "_run_child", lambda *args: pytest.fail("refused request spawned a child"))

        result = bridge_call.run_request(_request(tmp_path, network="github-read"))

        incident = json.loads((tmp_path / result["incident"]).read_text(encoding="utf-8"))
        assert result["status"] == "refused"
        assert "github-read preflight failed: a write to .git was allowed" in result["verdict"]
        assert (incident["fault"], incident["network"]) == ("network-preflight", "github-read")

    @pytest.mark.parametrize(
        "failure",
        [
            pytest.param({"timed_out": True}, id="timeout-retry"),
            pytest.param({"error": "unsupported effort"}, id="unsupported-effort-retry"),
        ],
    )
    def test_retry_keeps_the_budget_the_preflight_left(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: dict[str, Any]
    ) -> None:
        """A retry is told and held to the same remaining budget as the first child, never the full one.

        Both retry paths skip the preflight, so without carrying the reduced budget they would regain its time.
        """
        clock = [100.0]
        seen: list[tuple[float, bool]] = []

        def preflight(request: bridge_call.Request) -> None:
            clock[0] += 4.0

        def child(command: list[str], workspace: Path, timeout: float, job_path: Path | None = None):
            seen.append((timeout, any("soft budget of 6 seconds" in part for part in command)))
            return _outcome(**failure)

        monkeypatch.setattr(bridge_call.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(bridge_call, "_github_read_preflight", preflight)
        monkeypatch.setattr(bridge_call, "_run_child", child)

        bridge_call.run_request(_request(tmp_path, network="github-read", effort="high", timeout_seconds=10.0))

        cutoff = pytest.approx(6.0 * bridge_call.CHILD_TIMEOUT_MULTIPLIER)
        incidents = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in bridge_call.BridgePaths(tmp_path.resolve()).incidents.glob("*.json")
        ]
        assert seen == [(cutoff, True), (cutoff, True)]
        assert len(incidents) == 2
        assert {incident["duration_budget_seconds"] for incident in incidents} == {6.0}

    @pytest.mark.parametrize(
        ("preflight_result", "status"),
        [
            pytest.param(None, "complete", id="opted-in-success"),
            pytest.param("a probe failed", "refused", id="refused-preflight"),
        ],
    )
    def test_run_records_the_posture_in_health(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, preflight_result: str | None, status: str
    ) -> None:
        """A whole opted-in run, finished or refused, leaves a health line saying egress was open to GitHub.

        The clock is frozen so the preflight spends nothing and the success case really reaches the child.
        """
        monkeypatch.setattr(bridge_call.time, "monotonic", lambda: 100.0)
        monkeypatch.setattr(bridge_call, "_github_read_preflight", lambda request: preflight_result)
        monkeypatch.setattr(bridge_call, "_run_child", lambda *args: _outcome(core=_core()))

        bridge_call.run_request(_request(tmp_path, network="github-read", timeout_seconds=10.0))

        health = (tmp_path / ".temp" / "bridge" / "health.jsonl").read_text(encoding="utf-8").splitlines()
        record = json.loads(health[-1])
        assert (record["status"], record["network"]) == (status, "github-read")

    def test_remaining_budget_that_overflows_the_transport_cap_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A remaining budget whose rendering lengthens the prompt past the cap refuses, never raises mid-call.

        The preflight spends 2.3 s of a huge budget and the transport check rejects any prompt rendered with the reduced
        budget. The call must end `refused` with an incident carrying the applied effort and budget.
        """
        clock = [100.0]

        def preflight(request: bridge_call.Request) -> None:
            clock[0] += 2.3

        def too_large(request: bridge_call.Request) -> None:
            if request.timeout_seconds != 1e6:
                raise ValueError("task exceeds Bridge's encoded transport budget")

        monkeypatch.setattr(bridge_call.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(bridge_call, "_github_read_preflight", preflight)
        monkeypatch.setattr(bridge_call, "validate_request_transport_budget", too_large)
        monkeypatch.setattr(bridge_call, "_run_child", _stop_at_child)

        result = bridge_call.run_request(
            _request(tmp_path, network="github-read", effort="trivial", timeout_seconds=1e6)
        )

        incident = json.loads((tmp_path / result["incident"]).read_text(encoding="utf-8"))
        assert result["status"] == "refused"
        assert "the remaining budget makes the request too large" in result["verdict"]
        assert (incident["fault"], incident["network"]) == ("network-preflight", "github-read")
        assert (incident["duration_budget_seconds"], incident["effort"]) == (999997.0, "minimal")

    def test_probe_runs_codex_sandbox_with_an_empty_home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The probe runner passes the overrides, the throwaway workspace and the empty Codex home to the sandbox.

        Every other preflight test stubs the runner, so this pins the wiring that keeps the user's config layer out.
        """
        calls: list[dict[str, Any]] = []

        def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            calls.append({"argv": argv, **kwargs})
            return subprocess.CompletedProcess(argv, 0, " 200 000 \n", "")

        monkeypatch.setattr(bridge_call.subprocess, "run", run)
        monkeypatch.setattr(bridge_call, "_resolve_executable", lambda name: name)
        home, workspace = tmp_path / "home", tmp_path / "ws"

        result = bridge_call._sandbox_probe(["-c", "x=1"], workspace, ["curl", "u"], codex_home=home, timeout=7.0)

        assert result == (0, "200 000")
        assert calls[0]["argv"][1:] == ["sandbox", "-c", "x=1", "--", "curl", "u"]
        assert (calls[0]["cwd"], calls[0]["env"]["CODEX_HOME"], calls[0]["timeout"]) == (workspace, str(home), 7.0)

    def test_git_probe_fails_when_the_write_landed_despite_an_error_exit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-zero exit is not enough: if the sentinel file exists, the .git write happened and the call is refused.

        The stubbed `.git` probe exits as if refused but leaves its sentinel file behind. The preflight must trust the
        disk over the exit status and name the `.git write refusal` guarantee as broken.
        """
        answers = iter(_HOLDING_PROBES)

        def probe(
            overrides: list[str], workspace: Path, command: list[str], *, codex_home: Path, timeout: float
        ) -> tuple[int, str]:
            if "preflight" in command[-1]:
                (workspace / ".git" / "preflight").write_text("x", encoding="utf-8")
            return next(answers)

        monkeypatch.setattr(bridge_call, "_sandbox_probe", probe)

        result = bridge_call._github_read_preflight(_request(tmp_path, network="github-read", timeout_seconds=60.0))

        assert result is not None
        assert result.startswith(".git write refusal did not hold")

    def test_default_posture_skips_the_preflight(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A valid call without the opt-in reaches the child without running the preflight.

        A default request runs with a preflight that fails the test if called and a child launcher that stops the run.
        Reaching the stop proves the sandbox probes cost nothing for callers who never opted in.
        """
        monkeypatch.setattr(bridge_call, "_github_read_preflight", lambda request: pytest.fail("preflight ran"))
        monkeypatch.setattr(bridge_call, "_run_child", _stop_at_child)

        with pytest.raises(_ChildReachedError):
            bridge_call.run_request(_request(tmp_path))

    def test_preflight_time_comes_out_of_the_child_budget(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The child is told, and held to, the budget the preflight left, so the whole call stays bounded.

        The prompt's soft budget and the hard cutoff both come from what remains, never from the full budget.
        """
        clock = [100.0]
        cutoffs: list[float] = []
        commands: list[list[str]] = []

        def preflight(request: bridge_call.Request) -> None:
            clock[0] += 0.4

        def child(command: list[str], workspace: Path, timeout: float, job_path: Path | None = None) -> None:
            cutoffs.append(timeout)
            commands.append(command)
            raise _ChildReachedError

        monkeypatch.setattr(bridge_call.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(bridge_call, "_github_read_preflight", preflight)
        monkeypatch.setattr(bridge_call, "_run_child", child)

        with pytest.raises(_ChildReachedError):
            bridge_call.run_request(_request(tmp_path, network="github-read", timeout_seconds=10.0))

        assert cutoffs == [pytest.approx(9.0 * bridge_call.CHILD_TIMEOUT_MULTIPLIER)]
        assert any("soft budget of 9 seconds" in part for part in commands[0])

    def test_refusal_reports_the_applied_effort(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A refused call's envelope carries the effort the call would have used, like every other early exit.

        The `trivial` alias is requested and the preflight refuses. The context built for the refusal must hold the
        applied effort `minimal`, so refusals report the same effort field as the other early exits.
        """
        monkeypatch.setattr(bridge_call, "_github_read_preflight", lambda request: "a probe failed")
        captured: list[bridge_call.Request] = []
        real = bridge_call._unstarted_context

        def unstarted(request: bridge_call.Request, paths: object) -> object:
            captured.append(request)
            return real(request, paths)

        monkeypatch.setattr(bridge_call, "_unstarted_context", unstarted)

        # the "trivial" alias makes the requested and applied efforts differ, so the assertion can tell them apart
        bridge_call.run_request(_request(tmp_path, network="github-read", effort="trivial"))

        assert [seen.effort for seen in captured] == ["minimal"]

    def test_preflight_that_spends_the_whole_budget_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A preflight that leaves no time for the child refuses the call instead of starting a child it must kill.

        The fake clock advances 9.5 s of a 10 s budget during the preflight. Less than a second remains, so the run must
        end `refused` with the budget verdict before any child is launched.
        """
        clock = [100.0]

        def preflight(request: bridge_call.Request) -> None:
            clock[0] += 9.5

        monkeypatch.setattr(bridge_call.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(bridge_call, "_github_read_preflight", preflight)
        monkeypatch.setattr(bridge_call, "_run_child", _stop_at_child)

        result = bridge_call.run_request(_request(tmp_path, network="github-read", timeout_seconds=10.0))

        assert result["status"] == "refused"
        assert "github-read preflight failed: it left less than one second" in result["verdict"]

    def test_invalid_opted_in_request_never_reaches_github(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Effort validation runs before the preflight, so a request that cannot run sends no GitHub request.

        An opted-in request carries an unsupported effort. It must end `blocked` without the preflight being called,
        since the preflight makes real network probes that a doomed request has no reason to spend.
        """
        monkeypatch.setattr(bridge_call, "_github_read_preflight", lambda request: pytest.fail("preflight ran"))

        result = bridge_call.run_request(_request(tmp_path, effort="bogus", network="github-read"))

        assert result["status"] == "blocked"

    def test_cancelled_job_skips_the_preflight(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A detached job cancelled before it starts is reported cancelled without probing GitHub first.

        An opted-in request runs as a job whose owner already requested cancellation. The run must end `blocked` with
        the cancel verdict and never reach the preflight, so a cancelled job makes no network probes.
        """
        monkeypatch.setattr(bridge_call, "_github_read_preflight", lambda request: pytest.fail("preflight ran"))
        monkeypatch.setattr(bridge_call, "_job_cancel_requested", lambda *args, **kwargs: True)

        result = bridge_call.run_request(_request(tmp_path, network="github-read"), _job_path=tmp_path / "job.json")

        assert (result["status"], result["verdict"]) == ("blocked", "cancelled by job owner")


def test_invalid_effort_is_rejected_before_a_child_can_spawn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent an unknown caller effort from silently consuming default-tier tokens.

    A request carries an effort value outside the supported levels while any child launch would fail the test. The run
    must end blocked with a clear verdict and still write its health record, so a typo cannot quietly bill default-tier
    tokens.
    """
    monkeypatch.setattr(bridge_call, "_run_child", lambda *args: pytest.fail("invalid effort spawned a child"))

    result = bridge_call.run_request(_request(tmp_path, effort="bogus"))

    assert result["status"] == "blocked"
    assert "unsupported effort level" in result["verdict"]
    assert (tmp_path / ".temp" / "bridge" / "health.jsonl").is_file()


@pytest.mark.skipif(not DIRECTORY_SYMLINKS_SUPPORTED, reason="requires directory symlink creation capability")
def test_artifact_store_rejects_a_preexisting_temp_symlink_before_any_write(tmp_path: Path) -> None:
    """Prevent bridge artifacts from escaping the selected workspace through a hostile ancestor link.

    The workspace already holds a .temp entry that is a symlink to a directory outside it. The run must raise before
    creating anything, so an attacker-prepared ancestor link cannot redirect transcripts or job records outside the
    workspace.
    """
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (tmp_path / ".temp").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match=r"bridge artifact root escapes the trusted workspace"):
        bridge_call.run_request(_request(tmp_path, effort="bogus"))

    assert not (outside / "bridge").exists()


def _symlink_to(target: Path, link: Path) -> None:
    """Create link as a symlink to target."""
    link.symlink_to(target)


@pytest.mark.parametrize(
    "make_link",
    [
        pytest.param(
            _symlink_to,
            marks=pytest.mark.skipif(
                not DIRECTORY_SYMLINKS_SUPPORTED, reason="requires file symlink creation capability"
            ),
            id="symlink",
        ),
        pytest.param(
            os.link,
            marks=pytest.mark.skipif(
                not HARDLINKS_SUPPORTED, reason="requires same-filesystem hard-link creation capability"
            ),
            id="hard-link",
        ),
    ],
)
def test_health_link_preserves_result_without_writing_its_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_link: Callable[[Path, Path], None]
) -> None:
    """Prevent a predictable health artifact from writing through a workspace-prepared symlink or hard link.

    The workspace pre-creates health.jsonl as a link to a file outside it. The run still completes and reports a health-
    telemetry warning, but the link target keeps its original bytes, so a hostile workspace cannot use telemetry to
    append to arbitrary files.
    """
    paths = bridge_call.BridgePaths(tmp_path)
    paths.prepare()
    outside = tmp_path.parent / f"{tmp_path.name}-health-target.jsonl"
    outside.write_text("unchanged\n", encoding="utf-8")
    make_link(outside, paths.root / "health.jsonl")
    monkeypatch.setattr(bridge_call, "_run_child", lambda *args: _outcome(core=_core()))

    result = bridge_call.run_request(_request(tmp_path))

    assert result["status"] == "complete"
    assert "health telemetry warning" in result["findings"][-1]
    assert outside.read_text(encoding="utf-8") == "unchanged\n"


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="requires symlink creation capability")
def test_diagnose_health_summary_ignores_a_symlinked_health_log(tmp_path: Path) -> None:
    """Keep diagnosis from folding another file's records into the workspace health summary through a planted link.

    The workspace health.jsonl is a symlink to a file elsewhere holding a valid record. The diagnostic summary must come
    back empty, so a planted link cannot import foreign records into the reported fault counts.
    """
    paths = bridge_call.BridgePaths(tmp_path)
    paths.prepare()
    outside = tmp_path.parent / f"{tmp_path.name}-diagnose-health-target.jsonl"
    outside.write_text(json.dumps({"status": "blocked", "ts": 1.0, "cost": 2.5}) + "\n", encoding="utf-8")
    (paths.root / "health.jsonl").symlink_to(outside)

    summary = bridge_diagnose._health_summary(tmp_path)

    assert summary == {"fault_counts": {}, "latest_fault": {}, "cost_rollup": {}}


def test_diagnose_health_summary_reads_a_regular_health_log(tmp_path: Path) -> None:
    """Keep the regular workspace health log summarized after the no-follow descriptor checks.

    A regular health.jsonl holds one valid record followed by an undecodable line. The summary counts the valid record
    and skips the bad bytes, proving the no-follow open did not break the ordinary read path.
    """
    paths = bridge_call.BridgePaths(tmp_path)
    paths.prepare()
    record = {"status": "blocked", "ts": 1.0, "cost": 2.5, "direction": "d", "verb": "v", "model": "m"}
    (paths.root / "health.jsonl").write_bytes(json.dumps(record).encode() + b"\n\xff\n")

    summary = bridge_diagnose._health_summary(tmp_path)

    assert summary == {"fault_counts": {"blocked": 1}, "latest_fault": {"blocked": 1.0}, "cost_rollup": {"d/v/m": 2.5}}


def test_regular_health_member_appends_after_descriptor_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep ordinary health telemetry writable after link-count containment checks.

    A normal workspace runs a request whose child result is stubbed. The health log must be created and hold the run's
    record, so the link-count and no-follow checks cannot silently disable telemetry for honest workspaces.
    """
    monkeypatch.setattr(bridge_call, "_run_child", lambda *args: _outcome(core=_core()))

    result = bridge_call.run_request(_request(tmp_path))
    health = tmp_path / ".temp" / "bridge" / "health.jsonl"

    assert result["status"] == "complete"
    assert json.loads(health.read_text(encoding="utf-8"))["run_id"] == "run-fixed"


def test_windows_health_descriptor_rejects_hard_link_after_nofollow_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prevent Windows no-follow opening from bypassing common descriptor link-count validation.

    Windows ctypes, msvcrt and os are replaced by doubles where CreateFile succeeds but the descriptor reports two
    links. Opening for append must raise and close the descriptor exactly once, so the Windows path shares the POSIX
    link-count guard.
    """
    health = tmp_path / "health.jsonl"
    health.write_text("unchanged\n", encoding="utf-8")
    descriptor = 91
    closed: list[int] = []
    create_file_calls: list[tuple[str, int]] = []

    def _create_file(
        path: str, access: int, share: int, security: object, disposition: int, flags: int, template: object
    ) -> int:
        """Capture the Windows no-follow open and return its owned descriptor handle."""
        create_file_calls.append((path, flags))
        return descriptor

    def _fstat(opened: int) -> SimpleNamespace:
        """Prove common validation inspects the exact descriptor opened by CreateFile."""
        assert opened == descriptor
        return SimpleNamespace(st_mode=stat.S_IFREG, st_nlink=2)

    def _close_handle(handle: int) -> None:
        """Fail if the external API closes a handle already owned by the descriptor layer."""
        pytest.fail(f"unexpected handle close: {handle}")

    fake_ctypes = SimpleNamespace(
        c_void_p=lambda value: SimpleNamespace(value=value),
        windll=SimpleNamespace(kernel32=SimpleNamespace(CreateFileW=_create_file, CloseHandle=_close_handle)),
    )
    fake_msvcrt = SimpleNamespace(open_osfhandle=lambda handle, flags: descriptor)
    fake_os = SimpleNamespace(
        name="nt",
        O_WRONLY=os.O_WRONLY,
        O_APPEND=os.O_APPEND,
        fstat=_fstat,
        close=closed.append,
        fdopen=lambda *args, **kwargs: pytest.fail("hard-linked descriptor reached fdopen"),
    )
    monkeypatch.setitem(sys.modules, "ctypes", fake_ctypes)
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)
    monkeypatch.setattr(bridge_call, "os", fake_os)

    with pytest.raises(bridge_call.ArtifactBoundaryError, match="must be a regular file"):
        bridge_call._open_regular_append(health)

    assert create_file_calls == [(str(health), 0x00000080 | 0x00200000)]
    assert closed == [descriptor]


def test_reparse_point_health_member_is_rejected_before_the_windows_open_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent Windows reparse metadata from being treated as a regular health file.

    A regular-file stat carrying the Windows reparse attribute is classified as a link. Reparse points (junctions, cloud
    placeholders) therefore never count as regular health files, even though their mode bits look regular.
    """
    monkeypatch.setattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400, raising=False)

    class _Metadata:
        """Represent a regular file carrying the Windows reparse flag."""

        st_mode = stat.S_IFREG
        st_file_attributes = 0x400

    assert bridge_call._is_link_or_reparse_point(_Metadata()) is True


@pytest.mark.skipif(not DIRECTORY_SYMLINKS_SUPPORTED, reason="requires directory symlink creation capability")
def test_mcp_containment_rejection_returns_a_generic_error_without_provider_dispatch(tmp_path: Path) -> None:
    """Keep explicit embedding-host authority subject to artifact containment before provider work.

    An embedding host passes a trusted workspace whose .temp is a symlink outside it, then sends a tool call and a
    tools/list. The call returns a generic internal error that leaks no path, nothing is written outside, and the next
    request is still served.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path.parent / f"{tmp_path.name}-mcp-outside"
    outside.mkdir()
    (workspace / ".temp").symlink_to(outside, target_is_directory=True)
    messages = "\n".join(
        json.dumps(message)
        for message in (
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "bridge_advise", "arguments": {"task": "Reject the escaped artifact root."}},
            },
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
    )
    responses = [
        bridge_mcp.handle_message(json.loads(line), trusted_workspace=workspace) for line in messages.splitlines()
    ]
    assert [response["id"] for response in responses] == [1, 2]
    assert responses[0]["error"] == {"code": -32603, "message": "bridge execution failed"}
    assert str(outside) not in responses[0]["error"]["message"]
    assert not (outside / "bridge").exists()
    assert set(tool["name"] for tool in responses[1]["result"]["tools"]) == set(bridge_mcp.EXPECTED_TOOL_INVENTORY)


@pytest.mark.parametrize("timeout", ["nan", "inf", "-inf"])
def test_cli_rejects_nonfinite_timeout_before_provider_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], timeout: str
) -> None:
    """Prevent a non-finite CLI deadline from reaching a provider child process.

    The CLI receives nan, inf or -inf as the timeout while the child runner fails the test if called. The command exits
    2 with an error envelope, so a non-finite deadline can never disable the hard timeout.
    """
    monkeypatch.setattr(
        bridge_call, "_run_child", lambda *args: pytest.fail("non-finite timeout dispatched a provider")
    )

    exit_code = bridge_call.main(
        [
            "advise",
            "--task",
            "Reject this malformed deadline.",
            f"--timeout-seconds={timeout}",
            "--workspace",
            str(tmp_path),
        ]
    )

    response = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert response["status"] == "error"


def test_structured_unsupported_effort_retries_once_and_records_substitution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prevent an unsupported-effort loop from repeatedly charging or dispatching peers.

    The first attempt fails with an unsupported-effort error and the target lists the efforts it supports. Exactly one
    retry runs one level lower, and the blocked envelope records requested versus applied effort, so cost never ratchets
    silently.
    """
    attempts: list[str] = []

    def _fake_child(command: list[str], workspace: Path, timeout: float) -> bridge_call.ChildOutcome:
        """Return a child rejection so the retry policy can be inspected."""
        attempts.append(next(item for item in command if item.startswith("model_reasoning_effort=")))
        return _outcome(error="target rejected effort")

    monkeypatch.setattr(bridge_call, "_run_child", _fake_child)
    result = bridge_call.run_request(_request(tmp_path, effort="high", supported_efforts=("low", "medium", "high")))

    assert attempts == ['model_reasoning_effort="high"', 'model_reasoning_effort="medium"']
    assert result["status"] == "blocked"
    assert result["effort_substituted"] == {
        "requested": "high",
        "applied": "medium",
        "reason": "structured unsupported effort",
    }


def test_general_review_runs_read_only_exec_and_returns_a_compact_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prevent native review prose from bypassing the bridge's structured-result contract.

    A review request runs against a stubbed Codex child returning a structured result with details. The launched command
    is a read-only ephemeral exec with an adversarial review prompt, and the public envelope drops the details, keeping
    the contract compact.
    """
    commands: list[list[str]] = []

    def _fake_child(command: list[str], workspace: Path, timeout: float) -> bridge_call.ChildOutcome:
        """Return a structured review result without starting a child process."""
        commands.append(command)
        return _outcome(core=_core(details=["Reviewed the full local diff."]))

    monkeypatch.setattr(bridge_call, "_run_child", _fake_child)
    result = bridge_call.run_request(_request(tmp_path, verb="review"))

    assert commands[0][:2] == ["codex", "exec"]
    assert commands[0][:3] != ["codex", "exec", "review"]
    assert ["-s", "read-only"] == commands[0][commands[0].index("-s") : commands[0].index("-s") + 2]
    assert "--ephemeral" in commands[0]
    assert "adversarial" in commands[0][-1].lower()
    assert "review" in commands[0][-1].lower()
    assert result["status"] == "complete"
    assert result["verdict"] == "Finished deterministically."
    assert "details" not in result


def test_partial_result_keeps_remaining_work_public_and_details_transcript_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prevent incomplete work from being hidden solely in the transcript-only detail field.

    The child reports partial status with one remaining item and one long detail. The envelope exposes the remaining
    item to the caller while the detail is only in the transcript file, so unfinished work is never hidden in
    transcript-only text.
    """
    remaining = "Run the focused package validation."
    detail = "The peer explains why the package validation remains necessary."
    monkeypatch.setattr(
        bridge_call,
        "_run_child",
        lambda *args: _outcome(core=_core("partial", remaining=[remaining], details=[detail])),
    )

    envelope = bridge_call.run_request(_request(tmp_path))
    transcript = (tmp_path / envelope["transcript_path"]).read_text(encoding="utf-8")

    assert envelope["status"] == "partial"
    assert envelope["remaining"] == [remaining]
    assert "details" not in envelope
    assert detail in transcript


def test_remaining_summary_preserves_all_valid_items(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Preserve all decision-critical remaining items within the capture budget.

    The child reports nine identical remaining items. All nine reach the envelope, since remaining work is decision-
    critical and must not be trimmed to fit the capture budget.
    """
    monkeypatch.setattr(
        bridge_call,
        "_run_child",
        lambda *args: _outcome(core=_core("partial", remaining=["follow-up"] * 9)),
    )

    envelope = bridge_call.run_request(_request(tmp_path))

    assert envelope["status"] == "partial"
    assert envelope["remaining"] == ["follow-up"] * 9


@pytest.mark.parametrize(
    ("verb", "forbids_git_writes"),
    [
        pytest.param("implement", True, id="implement-forbids-git-writes"),
        pytest.param("advise", False, id="advise-read-only"),
        pytest.param("review", False, id="review-read-only"),
    ],
)
def test_write_capable_prompt_leaves_commits_to_the_caller(tmp_path: Path, verb: str, forbids_git_writes: bool) -> None:
    """The implement prompt tells the peer to stage, commit and push nothing so the calling session owns every commit.

    A Codex peer runs `workspace-write` with `approval_policy="never"` and still loads project instructions; with a
    local Git approval grant in the checkout its completion default would otherwise be to commit before the caller
    reviewed it, and a pending push token could be spent. A bridge child gains nothing from any grant or token: each is
    the calling session's authority record.
    """
    prompt = bridge_call._prompt_with_budget(_request(tmp_path, verb=verb))

    assert ("do not run git add, git commit, git push, or any other Git write" in prompt) is forbids_git_writes
    assert ("The caller reviews the diff and commits." in prompt) is forbids_git_writes


@pytest.mark.parametrize(
    "build_argv",
    [
        pytest.param(bridge_call.build_codex_argv, id="codex-peer"),
        pytest.param(bridge_call.build_claude_argv, id="claude-peer"),
    ],
)
def test_implement_argv_carries_the_git_write_ban_in_both_directions(
    tmp_path: Path, build_argv: Callable[[bridge_call.Request, Path], list[str]]
) -> None:
    """Both peer commands carry the implement prompt's Git write ban to the child.

    The Codex and Claude children are launched by separate argv builders; a builder that bypassed the shared prompt
    would leave one direction free to commit or spend a push token under the caller's grant.
    """
    schema_path = tmp_path / "core-schema.json"
    schema_path.write_text("{}", encoding="utf-8")

    argv = build_argv(_request(tmp_path, verb="implement"), schema_path)

    assert "do not run git add, git commit, git push, or any other Git write" in "\n".join(argv)


def test_budget_prompt_states_timeout_and_depth(tmp_path: Path) -> None:
    """The child prompt states the soft time budget, bridge depth and run id.

    A request with a 0.1 second timeout is rendered into the prompt the peer receives, so the peer can pace itself and a
    nested call can see its recursion depth.
    """
    prompt = bridge_call._prompt_with_budget(_request(tmp_path, timeout_seconds=0.1))

    assert "soft budget of 0.1 seconds" in prompt
    assert "Bridge depth is 0; run id is run-fixed" in prompt


def test_budget_prompt_and_timeout_terminate_held_process(tmp_path: Path) -> None:
    """Prevent an inherited stdin/process-group hang from outliving the hard deadline.

    A real Python child sleeps far beyond a 0.05 second deadline. The run must report a timeout with an exit code
    instead of hanging, so a child holding inherited stdin or a process group cannot outlive the hard deadline.
    """
    outcome = bridge_call._run_child([sys.executable, "-c", "import time; time.sleep(5)"], tmp_path, 0.05)

    assert outcome.timed_out is True
    assert outcome.returncode is not None


@pytest.mark.parametrize("verb", ["implement", "advise", "review"])
@pytest.mark.parametrize(
    ("build", "present", "absent"),
    [
        pytest.param(
            bridge_call.build_codex_argv,
            (
                "each shell command's text counts against the cap",
                "command output is reduced to size and digest",
                "never inline large scripts, source code or data lists",
                "pass small inputs as arguments and print bounded output",
                "ends with no result",
            ),
            (),
            id="codex",
        ),
        pytest.param(
            bridge_call.build_claude_argv, ("final JSON result",), ("each shell command's text",), id="claude"
        ),
    ],
)
def test_prompt_warns_about_the_derived_capture_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    verb: str,
    build: Callable[..., list[str]],
    present: tuple[str, ...],
    absent: tuple[str, ...],
) -> None:
    """Warn every verb using the actual child's capture behavior and configured cap.

    With the capture cap lowered to 32 KiB, the prompt is built through the Codex and Claude argv builders for each
    verb. Both state the 32 KiB cap, Codex adds its command-text and output-digest warnings, and only implement may
    write files.
    """
    monkeypatch.setattr(bridge_call, "MAX_CHILD_OUTPUT_BYTES", 32 * 1024)
    request = _request(tmp_path, verb=verb)
    schema = tmp_path / "schema.json"
    schema.write_text("{}", encoding="utf-8")
    command = build(request, schema)
    prompt = next(argument for argument in command if argument.startswith("You have a soft budget"))

    assert "about 32 KiB" in prompt
    assert ("Files cannot be written." in prompt) is (verb != "implement")
    assert [phrase for phrase in present if phrase not in prompt] == []
    assert [phrase for phrase in absent if phrase in prompt] == []


def test_started_command_text_is_replaced_with_size_and_digest() -> None:
    """Avoid retaining a second full copy of a command in its started event.

    A 30,000-character command arrives in an item.started event. The buffered stdout keeps every field except command,
    which becomes a byte length and SHA-256, so long commands are never stored twice (once started, once completed).
    """
    command = "python -c '" + "é" * 30_000 + "'"
    item = {"id": "one", "type": "command_execution", "command": command, "status": "in_progress"}
    line = (json.dumps({"type": "item.started", "item": item}) + "\n").encode()
    output = bridge_call._ChildOutputBuffer()
    output.append("stdout", line)
    stdout, stderr = output.text()

    assert json.loads(stdout)["item"] == {
        **{key: value for key, value in item.items() if key != "command"},
        "command_bytes": len(command.encode()),
        "command_sha256": hashlib.sha256(command.encode()).hexdigest(),
    }
    assert stdout.endswith("\n")
    assert stderr == ""
    assert not output.output_limited.is_set()


@pytest.mark.parametrize(
    ("command", "truncated"),
    [
        pytest.param("x" * 4096, False, id="exact-boundary"),
        pytest.param("x" * 4097, True, id="over-boundary"),
        pytest.param("x" * 4095 + "🙂tail", True, id="split-multibyte"),
        pytest.param("é" * 3000, True, id="multibyte-prefix"),
    ],
)
def test_completed_command_keeps_a_utf8_safe_prefix(command: str, truncated: bool) -> None:
    """Keep complete short commands and at most 4 KiB of a longer decoded command.

    Completed command events carry commands of 4096 bytes, just over it, with a multibyte character straddling the cut,
    and multibyte-only text. Short commands pass whole; longer ones keep a prefix that is valid UTF-8 plus byte count
    and digest.
    """
    event = {"type": "item.completed", "item": {"type": "command_execution", "command": command}}
    item = json.loads(bridge_call._compact_command_event(json.dumps(event).encode()))["item"]

    assert item["command"] == command.encode()[:4096].decode("utf-8", errors="ignore")
    digest = (len(command.encode()), hashlib.sha256(command.encode()).hexdigest())
    assert (item.get("command_bytes"), item.get("command_sha256")) == (digest if truncated else (None, None))


@pytest.mark.parametrize("newline", [True, False])
def test_output_limit_salvages_the_last_complete_agent_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, newline: bool
) -> None:
    """Preserve a complete message even when retaining that record triggers overflow.

    A real child emits an older message, a near-cap filler line, then a large final agent message with or without a
    trailing newline. The blocked result carries a 4 KiB partial_output cut from the last complete message and an
    output-limit incident.
    """
    message = "x" * 4095 + "🙂 last message" * 100
    emitter = (
        "import json, sys\n"
        "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'older'}}), flush=True)\n"
        f"sys.stdout.write('X' * {bridge_call.MAX_CHILD_OUTPUT_BYTES - 1024} + '\\n'); sys.stdout.flush()\n"
        "sys.stdout.write(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':sys.argv[1]}})"
        f" + {'chr(10)' if newline else repr('')}); sys.stdout.flush()\n"
    )
    resolve_command = bridge_call._resolved_command
    monkeypatch.setattr(
        bridge_call,
        "_resolved_command",
        lambda command: [sys.executable, "-c", emitter, message] if command[0] == "codex" else resolve_command(command),
    )
    result = bridge_call.run_request(_request(tmp_path, verb="review"))
    incident = json.loads((tmp_path / result["incident"]).read_text(encoding="utf-8"))

    assert result["status"] == "blocked"
    assert result["findings"] == []
    assert incident["fault"] == "output-limit"
    assert result["partial_output"] == "x" * 4095
    assert incident["partial_output"] == result["partial_output"]
    assert len(result["partial_output"].encode()) <= 4096


@pytest.mark.parametrize(
    ("host", "fragment"),
    [
        pytest.param("codex", None, id="codex-without-message"),
        pytest.param("claude", None, id="claude-without-message"),
        pytest.param("claude", "must not escape", id="claude-with-message"),
    ],
)
def test_output_limit_omits_partial_output_without_a_codex_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str, fragment: str | None
) -> None:
    """Never attach salvage when absent or when the child is Claude.

    The child outcome is output-limited with either no salvage fragment or, for a Claude host, a fragment that must not
    escape. The blocked result and incident then carry no partial_output, since salvage is a Codex-only guarantee.
    """
    monkeypatch.setattr(
        bridge_call,
        "_run_child",
        lambda *args: bridge_call.ChildOutcome("", "", -9, False, "output exceeded cap", True, fragment),
    )
    result = bridge_call.run_request(_request(tmp_path), host=host)
    incident = json.loads((tmp_path / result["incident"]).read_text(encoding="utf-8"))
    assert result["status"] == "blocked"
    assert result["findings"] == []
    assert "partial_output" not in result
    assert "partial_output" not in incident


def test_long_command_events_leave_room_for_the_final_result(tmp_path: Path) -> None:
    """Prevent repeated command text from exhausting capture despite small command output.

    A real child prints four started/completed pairs whose command text is 60,000 characters each, then the final
    result. Command text is compacted as it arrives, so capture is not exhausted and the final result still parses from
    under 20 KB of stdout.
    """
    emitter = (
        "import json, sys\n"
        "for index in range(4):\n"
        "    item = {'id':str(index),'type':'command_execution','command':'x' * 60000}\n"
        "    print(json.dumps({'type':'item.started','item':item}), flush=True)\n"
        "    item['aggregated_output'] = 'bounded output'\n"
        "    print(json.dumps({'type':'item.completed','item':item}), flush=True)\n"
        "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':sys.argv[1]}}), flush=True)\n"
    )
    outcome = bridge_call._run_child([sys.executable, "-c", emitter, json.dumps(_core())], tmp_path, 5.0)

    assert outcome.returncode == 0
    assert not outcome.output_limited
    assert outcome.partial_output is None
    assert bridge_call._parse_output(outcome.stdout, "codex").core == _core()
    assert len(outcome.stdout.encode()) < 20_000


def test_incomplete_or_invalid_agent_events_cannot_replace_salvage() -> None:
    """Ignore started, malformed, oversized and stderr messages while freezing a complete fragment.

    A complete message is followed by a started message, a message with a non-string text, a stderr copy and an
    unterminated stdout record. Only the complete stdout message is kept as the salvage candidate, even after the
    unterminated record is closed.
    """
    output = bridge_call._ChildOutputBuffer()
    _append_events(
        output,
        {"type": "item.completed", "item": {"type": "agent_message", "text": "complete"}},
        {"type": "item.started", "item": {"type": "agent_message", "text": "started"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": 42}},
    )
    output.append("stderr", b'{"type":"item.completed","item":{"type":"agent_message","text":"stderr"}}\n')
    output.append("stdout", b'{"type":"item.completed","item":{"type":"agent_message","text":"unfinished')
    output.text()
    output.append("stdout", b'"}}\n')
    assert output.last_agent_message == "complete"


@pytest.mark.parametrize("event_type", ["item.started", "item.completed"])
@pytest.mark.parametrize("newline", [True, False])
def test_oversized_command_text_is_not_compacted_before_the_raw_ceiling(event_type: str, newline: bool) -> None:
    """Reject oversized command records both during capture and final flush.

    A started or completed command record whose command alone fills the record ceiling is appended with and without a
    trailing newline. It must trip the output limit and pass through verbatim up to the cap with no digest, as it is not
    provably a command-output record.
    """
    event = {
        "type": event_type,
        "item": {"type": "command_execution", "command": "X" * bridge_call.MAX_CHILD_RECORD_BYTES},
    }
    line = json.dumps(event).encode() + (b"\n" if newline else b"")
    output = bridge_call._ChildOutputBuffer()
    output.append("stdout", line)
    stdout, _ = output.text()
    assert output.output_limited.is_set()
    assert stdout.encode() == line[: bridge_call.MAX_CHILD_OUTPUT_BYTES]
    assert "command_sha256" not in stdout


@pytest.mark.parametrize(
    "emitter",
    [
        "import sys, time; sys.stdout.buffer.write(b'x' * 400000); sys.stdout.flush(); time.sleep(30)",
        "import sys, time; sys.stderr.buffer.write(b'x' * 400000); sys.stderr.flush(); time.sleep(30)",
        "import sys, time; sys.stdout.buffer.write(b'x' * 200000); sys.stderr.buffer.write(b'y' * 200000); sys.stdout.flush(); sys.stderr.flush(); time.sleep(30)",
    ],
)
def test_child_output_limit_stops_a_noisy_peer_and_caps_its_transcript(tmp_path: Path, emitter: str) -> None:
    """Prevent a verbose peer from growing supervisor memory or transcript storage without a bound.

    A real child writes 400 KB to stdout, to stderr, or split across both, then sleeps. The supervisor stops it at the
    capture cap, reports output_limited, and the transcript written from the outcome stays under its own byte bound.
    """
    outcome = bridge_call._run_child([sys.executable, "-c", emitter], tmp_path, timeout=5.0)
    paths = bridge_call.BridgePaths(tmp_path)
    paths.prepare()
    transcript = bridge_call._write_transcript(paths, outcome.stdout, outcome.stderr)
    transcript_bytes = (tmp_path / transcript).read_bytes()

    assert outcome.output_limited is True
    assert outcome.returncode is not None
    assert (
        len(outcome.stdout.encode("utf-8")) + len(outcome.stderr.encode("utf-8")) <= bridge_call.MAX_CHILD_OUTPUT_BYTES
    )
    assert len(transcript_bytes) <= bridge_call.MAX_CHILD_TRANSCRIPT_BYTES


@pytest.mark.parametrize("initial_stdout_encoding", ["utf-8", "cp1252"])
def test_codex_tool_events_do_not_consume_the_final_result_budget(tmp_path: Path, initial_stdout_encoding: str) -> None:
    """Keep a valid final review after repeated moderate command outputs exceed the raw capture budget.

    A real child emits twenty 40 KB command outputs, a final message and a usage event under a UTF-8 or cp1252 starting
    stdout encoding. Each tool event is compacted to size and digest, so the raw total exceeds the cap yet the final
    result survives.
    """
    emitter = (
        "import json, sys\n"
        "sys.stdout.reconfigure(encoding=sys.argv[2])\n"
        "sys.stdout.reconfigure(encoding='utf-8', newline='\\n')\n"
        "for index in range(20):\n"
        "    print(json.dumps({'type': 'item.completed', 'item': {'id': str(index), "
        "'type': 'command_execution', 'status': 'completed', 'exit_code': 0, "
        "'aggregated_output': 'é' * 20000}}, ensure_ascii=False), flush=True)\n"
        "print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', "
        "'text': sys.argv[1]}}), flush=True)\n"
        "print(json.dumps({'type': 'turn.completed', 'usage': {'input': 3, 'output': 2}}), flush=True)\n"
    )
    outcome = bridge_call._run_child(
        [sys.executable, "-c", emitter, json.dumps(_core()), initial_stdout_encoding], tmp_path, timeout=5.0
    )
    assert outcome.returncode == 0
    assert outcome.output_limited is False

    parsed = bridge_call._parse_output(outcome.stdout, "codex")
    records = [json.loads(line) for line in outcome.stdout.splitlines()]
    tools = [record["item"] for record in records if record.get("item", {}).get("type") == "command_execution"]
    paths = bridge_call.BridgePaths(tmp_path)
    paths.prepare()
    transcript = bridge_call._write_transcript(paths, outcome.stdout, outcome.stderr)

    assert parsed.core == _core()
    assert len(tools) == 20
    assert all("aggregated_output" not in item and item["aggregated_output_bytes"] == 40_000 for item in tools)
    assert all(
        item["aggregated_output_sha256"] == hashlib.sha256(("é" * 20_000).encode()).hexdigest() for item in tools
    )
    assert len(outcome.stdout.encode("utf-8")) <= bridge_call.MAX_CHILD_OUTPUT_BYTES
    assert len((tmp_path / transcript).read_bytes()) <= bridge_call.MAX_CHILD_TRANSCRIPT_BYTES


def test_reordered_subprocess_event_preserves_final_result_under_stderr_pressure(tmp_path: Path) -> None:
    """Keep field-order recovery covered through real concurrent subprocess pipes.

    A real child writes 100 KB to stderr and then a command event whose fields are in non-canonical order and carry 180
    KB, followed by the final result. The event compacts despite the order, stderr is intact and the final result
    parses.
    """
    emitter = (
        "import json, sys\n"
        "sys.stderr.buffer.write(b'E' * 100000); sys.stderr.flush()\n"
        "print(json.dumps({'item': {'aggregated_output': 'R' * 180000, 'type': 'command_execution'}, 'type': 'item.completed'}), flush=True)\n"
        "print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': sys.argv[1]}}), flush=True)\n"
    )
    outcome = bridge_call._run_child([sys.executable, "-c", emitter, json.dumps(_core())], tmp_path, 5.0)
    assert outcome.output_limited is False
    assert outcome.returncode == 0
    assert outcome.stderr == "E" * 100_000
    assert bridge_call._parse_output(outcome.stdout, "codex").core == _core()
    item = json.loads(outcome.stdout.splitlines()[0])["item"]
    assert "aggregated_output" not in item
    assert item["aggregated_output_bytes"] == 180_000
    assert item["aggregated_output_sha256"] == hashlib.sha256(b"R" * 180_000).hexdigest()


def test_oversized_codex_command_output_preserves_final_result(tmp_path: Path) -> None:
    """Compact a real child's 600 KiB output without terminating its final review.

    A real child emits one 600 KiB command output containing ANSI escapes and multibyte text, then the final review. The
    output is replaced by its exact byte length and digest, the child exits 0 and total capture stays within the cap.
    """
    emitter = (
        "import json, sys\n"
        "sys.stdout.reconfigure(encoding='utf-8')\n"  # Windows pipes default to the ANSI code page; Codex emits UTF-8
        "print(json.dumps({'type': 'item.completed', 'item': {'type': 'command_execution', "
        "'aggregated_output': '\\x1b[32mé.\\x1b[0m' * 51200}}, ensure_ascii=False), flush=True)\n"
        "print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': sys.argv[1]}}), flush=True)\n"
    )

    outcome = bridge_call._run_child([sys.executable, "-c", emitter, json.dumps(_core())], tmp_path, timeout=5.0)

    assert outcome.output_limited is False
    assert outcome.returncode == 0
    assert outcome.error is None
    item = json.loads(outcome.stdout.splitlines()[0])["item"]
    payload = ("\x1b[32mé.\x1b[0m" * 51200).encode()
    assert len(payload) == 600 * 1024
    assert "aggregated_output" not in item
    assert item["aggregated_output_bytes"] == len(payload)
    assert item["aggregated_output_sha256"] == hashlib.sha256(payload).hexdigest()
    assert bridge_call._parse_output(outcome.stdout, "codex").core == _core()
    assert (
        len(outcome.stdout.encode("utf-8")) + len(outcome.stderr.encode("utf-8")) <= bridge_call.MAX_CHILD_OUTPUT_BYTES
    )


@pytest.mark.parametrize("newline", [False, True])
def test_oversized_command_with_duplicate_started_type_remains_terminal(newline: bool) -> None:
    """Reject a completed prefix when the full record overrides it with a started type.

    A completed command record is padded so a later duplicate type key says item.started, which a prefix scan would
    miss. Fed in 8 KiB chunks it must end the run as output-limited instead of being compacted as completed output.
    """
    record = {
        "type": "item.completed",
        "item": {"type": "command_execution", "command": "C" * 8192, "aggregated_output": ""},
    }
    line = (
        json.dumps(record).encode()[:-1]
        + b" " * bridge_call.MAX_CHILD_RECORD_BYTES
        + b', "type": "item.started"}'
        + (b"\n" if newline else b"")
    )
    assert bridge_call._has_command_output_prefix(line[: bridge_call.MAX_CHILD_RECORD_BYTES])
    assert json.loads(line)["type"] == "item.started"
    output = bridge_call._ChildOutputBuffer()

    _feed_chunks(output, line, 8192)
    stdout, _ = output.text()

    assert output.output_limited.is_set()
    assert len(stdout.encode()) <= bridge_call.MAX_CHILD_OUTPUT_BYTES


def test_completed_record_one_byte_over_limit_compacts() -> None:
    """Admit a proven command-output record at the first byte over the ordinary ceiling.

    A completed command-output record is sized exactly one byte over the ordinary record ceiling. It is still accepted
    and compacted, proving the ceiling is not applied to provably compactable command-output records.
    """
    record = {"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": ""}}
    base = (json.dumps(record) + "\n").encode("utf-8")
    record["item"]["aggregated_output"] = "X" * (bridge_call.MAX_CHILD_RECORD_BYTES + 1 - len(base))
    line = (json.dumps(record) + "\n").encode("utf-8")
    output = bridge_call._ChildOutputBuffer()

    output.append("stdout", line)

    assert len(line) == bridge_call.MAX_CHILD_RECORD_BYTES + 1
    assert output.output_limited.is_set() is False
    item = json.loads(output.text()[0])["item"]
    assert item["aggregated_output_bytes"] == len(record["item"]["aggregated_output"])


@pytest.mark.parametrize("chunk_size", [8192, 1024 * 1024])
@pytest.mark.parametrize("newline", [True, False])
def test_large_command_record_has_bounded_capture_and_exact_digest(chunk_size: int, newline: bool) -> None:
    """Bound pending memory and retained records across chunk boundaries and EOF.

    A multibyte command and a very large output are fed in 8 KiB or 1 MiB chunks, with and without a final newline.
    Pending bytes and captured bytes stay bounded at every step, and the retained record carries exact byte counts and
    digests for both fields.
    """
    payload = 'é😀\\"\n' * 60_000
    command = "é" * 5000
    event = {
        "type": "item.completed",
        "item": {"type": "command_execution", "command": command, "aggregated_output": payload},
    }
    line = json.dumps(event).encode() + (b"\n" if newline else b"")
    output = bridge_call._ChildOutputBuffer()

    states = _feed_chunks(output, line, chunk_size)
    stdout, stderr = output.text()

    item = json.loads(stdout)["item"]
    assert max(pending for pending, _, _ in states) <= bridge_call.MAX_CHILD_COMMAND_RECORD_BYTES
    assert max(captured for _, captured, _ in states) <= bridge_call.MAX_CHILD_RECORD_BYTES
    assert not any(limited for _, _, limited in states)
    assert not output.output_limited.is_set()
    assert not output.pending_stdout
    assert stderr == ""
    assert len(stdout.encode()) <= bridge_call.MAX_CHILD_RECORD_BYTES
    assert "aggregated_output" not in item
    assert item["aggregated_output_bytes"] == len(payload.encode())
    assert item["aggregated_output_sha256"] == hashlib.sha256(payload.encode()).hexdigest()
    assert item["command"] == command.encode()[: bridge_call.MAX_CHILD_FRAGMENT_BYTES].decode()
    assert item["command_bytes"] == len(command.encode())
    assert item["command_sha256"] == hashlib.sha256(command.encode()).hexdigest()


def test_large_command_record_stops_at_its_bounded_raw_ceiling() -> None:
    """Stop an unfinished command-output string without retaining beyond 8 MiB.

    A command-output record starts but its string never closes while 8 KiB chunks keep arriving. Pending bytes never
    exceed the raw ceiling, the output limit trips at exactly that ceiling, and the retained text stays within the
    capture cap.
    """
    output = bridge_call._ChildOutputBuffer()
    output.append("stdout", b'{"type":"item.completed","item":{"type":"command_execution","aggregated_output":"')

    states = _feed_chunks(
        output, b"X" * (8192 * (bridge_call.MAX_CHILD_COMMAND_RECORD_BYTES // 8192 + 1)), 8192, stop_when_limited=True
    )

    assert max(pending for pending, _, _ in states) <= bridge_call.MAX_CHILD_COMMAND_RECORD_BYTES
    assert output.output_limited.is_set()
    assert len(output.pending_stdout) == bridge_call.MAX_CHILD_COMMAND_RECORD_BYTES
    assert len(output.text()[0].encode()) <= bridge_call.MAX_CHILD_OUTPUT_BYTES
    assert not output.pending_stdout


_OVERSIZED = "X" * (600 * 1024)


def _command_item(**overrides: str) -> dict[str, str]:
    """Oversized command_execution item; overrides replace or append fields in place."""
    return {"type": "command_execution", "aggregated_output": _OVERSIZED, **overrides}


def _completed_line(item: dict[str, str]) -> bytes:
    """Serialize a completed item event without a trailing newline."""
    return json.dumps({"type": "item.completed", "item": item}).encode()


@pytest.mark.parametrize(
    "line",
    [
        pytest.param(_completed_line({"type": "agent_message", "text": _OVERSIZED}), id="agent"),
        pytest.param(_completed_line(_command_item(type="unknown")), id="unknown"),
        pytest.param(_completed_line(_command_item())[:-1], id="malformed"),
        pytest.param(
            json.dumps(
                {"item": {"aggregated_output": _OVERSIZED, "type": "command_execution"}, "type": "item.completed"}
            ).encode(),
            id="late-type",
        ),
        pytest.param(_completed_line(_command_item())[:-2] + b', "type": "agent_message"}}', id="changed-type"),
        pytest.param(
            _completed_line(_command_item(aggregated_output="small", metadata=_OVERSIZED)), id="large-metadata"
        ),
        pytest.param(_completed_line(_command_item(aggregated_output="small", command=_OVERSIZED)), id="large-command"),
        pytest.param(
            json.dumps(
                {
                    "type": "unknown",
                    "nested": {"type": "item.completed", "item": _command_item()},
                    "text": _OVERSIZED,
                }
            ).encode(),
            id="nested-decoy",
        ),
    ],
)
def test_other_oversized_records_remain_terminal(line: bytes) -> None:
    """Never exempt agent text, unknown types, malformed JSON, or large non-output fields.

    Eight 600 KiB records that look compactable but are not provably command output: agent text, unknown type, truncated
    or late-typed JSON, large metadata or command fields, and a nested decoy. Each must trip the output limit and none
    may become salvage.
    """
    output = bridge_call._ChildOutputBuffer()

    states = _feed_chunks(output, line, 8192)
    output.append("stdout", b"\n")

    assert max(pending for pending, _, _ in states) <= bridge_call.MAX_CHILD_COMMAND_RECORD_BYTES
    assert output.output_limited.is_set()
    assert len(output.text()[0].encode()) <= bridge_call.MAX_CHILD_OUTPUT_BYTES
    assert output.last_agent_message is None


def test_whitespace_prefixed_command_event_compacts_before_shared_budget() -> None:
    """Keep a valid chunked JSONL event from blocking when stderr already uses the budget.

    A leading-whitespace command-output event of 180,000 bytes arrives after 100,000 bytes of stderr, so raw bytes would
    pass the shared cap while the compacted record fits. The event must compact instead of tripping the limit.
    """
    record = {
        "type": "item.completed",
        "item": {"type": "command_execution", "aggregated_output": "X" * 180_000},
    }
    line = b"  " + (json.dumps(record) + "\n").encode("utf-8")
    output = bridge_call._ChildOutputBuffer()
    output.append("stderr", b"E" * 100_000)

    _feed_chunks(output, line, 8192)

    assert len(line) < bridge_call.MAX_CHILD_OUTPUT_BYTES
    assert len(line) + 100_000 > bridge_call.MAX_CHILD_OUTPUT_BYTES
    assert output.output_limited.is_set() is False
    assert json.loads(output.text()[0])["item"]["aggregated_output_bytes"] == 180_000


@pytest.mark.parametrize("chunk_size", [1, 8192])
def test_reordered_event_survives_shared_capture_pressure(chunk_size: int) -> None:
    """Accept valid event fields in any order before deciding whether output can compact.

    A command-output event lists its type last, fed byte by byte or in 8 KiB chunks while stderr nearly fills the shared
    budget. The event must be accepted without tripping the limit, and a following final result must still parse.
    """
    payload_size = 2048 if chunk_size == 1 else 180_000
    stderr_size = bridge_call.MAX_CHILD_OUTPUT_BYTES - 1024 if chunk_size == 1 else 100_000
    line = (
        json.dumps(
            {"item": {"aggregated_output": "X" * payload_size, "type": "command_execution"}, "type": "item.completed"}
        )
        + "\n"
    ).encode()
    output = bridge_call._ChildOutputBuffer()
    output.append("stderr", b"E" * stderr_size)

    states = _feed_chunks(output, line, chunk_size)
    _append_events(output, _final_result_event())

    assert not any(limited for _, _, limited in states)
    assert bridge_call._parse_output(output.text()[0], "codex").core == _core()


@pytest.mark.parametrize(
    ("cancels", "lingers"),
    [
        pytest.param(False, True, id="timeout"),
        pytest.param(True, True, id="cancel"),
        pytest.param(False, False, id="exit"),
    ],
)
def test_pending_overflow_takes_precedence_after_final_drain(tmp_path: Path, cancels: bool, lingers: bool) -> None:
    """Never retry an overflow merely because the final pending record flushed during shutdown.

    A real child fills stderr to the cap and leaves an unfinished command record on stdout, then times out, is cancelled
    via its marker, or exits. All three end as output-limited, never timed-out, so overflow is never retried as an
    ordinary failure.
    """
    job_path = tmp_path / "synthetic-job.json"
    marker = job_path.with_name("synthetic-job.cancel.json")
    emitter = (
        "import sys, time\nfrom pathlib import Path\n"
        f"sys.stderr.buffer.write(b'E' * {bridge_call.MAX_CHILD_OUTPUT_BYTES}); sys.stderr.flush()\n"
        'sys.stdout.buffer.write(b\'{"type":"item.completed","item":{"type":"command_execution","aggregated_output":"\' + b\'X\' * 100000); sys.stdout.flush()\n'
    )
    emitter += f"Path({str(marker)!r}).write_text('{{\"job_id\":\"synthetic-job\"}}', encoding='utf-8')\n" * cancels
    emitter += "time.sleep(30)\n" * lingers
    outcome = bridge_call._run_child([sys.executable, "-c", emitter], tmp_path, 0.8, job_path)
    assert outcome.output_limited is True
    assert outcome.timed_out is False
    assert outcome.error == f"child output exceeded {bridge_call.MAX_CHILD_OUTPUT_BYTES} byte capture limit"
    assert outcome.returncode is not None


@pytest.mark.parametrize("payload", ["", "same", "diff", "é😀"])
def test_compacted_output_has_verifiable_content_digest(payload: str) -> None:
    """Distinguish equal-length omitted outputs and hash decoded UTF-8 rather than JSON escaping.

    Command events with empty, equal-length but different, and multibyte payloads are compacted. The item keeps only
    identity fields plus UTF-8 byte length and SHA-256 of the decoded text, so two omitted outputs of equal size remain
    distinguishable.
    """
    event = {
        "type": "item.completed",
        "item": {"type": "command_execution", "id": "one", "exit_code": 7, "aggregated_output": payload},
    }
    result = json.loads(bridge_call._compact_command_event(json.dumps(event).encode()))
    assert result["item"] == {
        "type": "command_execution",
        "id": "one",
        "exit_code": 7,
        "aggregated_output_bytes": len(payload.encode()),
        "aggregated_output_sha256": hashlib.sha256(payload.encode()).hexdigest(),
    }


def test_transcript_preserves_capture_tail_at_budget(tmp_path: Path) -> None:
    """Reserve transcript framing bytes so accepted final output is never silently sliced off.

    stdout fills the capture budget to within four bytes and stderr is the word TAIL. The transcript must contain both
    streams whole with their framing and stay within the transcript bound, so the end of the output is never cut off.
    """
    stdout = "X" * (bridge_call.MAX_CHILD_OUTPUT_BYTES - 4)
    stderr = "TAIL"
    paths = bridge_call.BridgePaths(tmp_path)
    paths.prepare()
    transcript = tmp_path / bridge_call._write_transcript(paths, stdout, stderr)
    assert transcript.read_bytes() == f"stdout:\n{stdout}\n\nstderr:\n{stderr}\n".encode()
    assert transcript.stat().st_size <= bridge_call.MAX_CHILD_TRANSCRIPT_BYTES


def test_invalid_utf8_cannot_expand_retained_transcript_budget() -> None:
    """Account for replacement characters expanding malformed bytes during final decoding.

    A full cap of undecodable 0xFF bytes arrives on stderr. Each byte decodes to a longer replacement character, yet the
    decoded output must not exceed the cap, and the limit is flagged.
    """
    output = bridge_call._ChildOutputBuffer()
    output.append("stderr", b"\xff" * bridge_call.MAX_CHILD_OUTPUT_BYTES)
    stdout, stderr = output.text()
    assert output.output_limited.is_set()
    assert len(stdout.encode()) + len(stderr.encode()) <= bridge_call.MAX_CHILD_OUTPUT_BYTES


@pytest.fixture
def pure_python_json_scanner(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make json.loads use the recursive pure-Python scanner so deep nesting raises RecursionError.

    The accelerated decoder can accept very deep nesting on newer Python; the stdlib recursive scanner cannot.
    """
    decoder = json.JSONDecoder()
    decoder.scan_once = json.scanner.py_make_scanner(decoder)
    monkeypatch.setattr(json, "_default_decoder", decoder)


def _assert_unparseable_event_keeps_final_result(value: str, error: type[Exception]) -> None:
    """Feed an event whose extra field raises ``error`` in json.loads, then a valid final result."""
    with pytest.raises(error):
        json.loads(value)
    line = (
        '{"type":"item.completed","item":{"type":"command_execution","aggregated_output":"x","extra":' + value + "}}\n"
    )
    output = bridge_call._ChildOutputBuffer()
    output.append("stdout", line.encode())
    _append_events(output, _final_result_event())
    assert bridge_call._parse_output(output.text()[0], "codex").core == _core()
    assert bridge_call._decode_possible_json(value) is None


@pytest.mark.usefixtures("pure_python_json_scanner")
def test_deeply_nested_event_does_not_hide_later_final_result() -> None:
    """Keep a parser RecursionError from crashing normalization after bounded capture.

    An event carries an extra field nested 10,000 levels deep, followed by a valid final result. Normalization must skip
    the unparsable event and still return the final result.
    """
    _assert_unparseable_event_keeps_final_result("[" * 10_000 + "0" + "]" * 10_000, RecursionError)


def test_oversized_integer_event_does_not_hide_later_final_result() -> None:
    """Keep a parser ValueError for an overlong integer from crashing normalization after bounded capture.

    An event carries an extra field that is a 10,000-digit integer, which json.loads rejects with ValueError.
    Normalization skips it and the following valid final result is still returned.
    """
    _assert_unparseable_event_keeps_final_result("1" * 10_000, ValueError)


def test_output_limit_incident_records_real_partial_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Report edits from a real overflowing child without automatically replaying its write task.

    A real git workspace and a stubbed Codex child that writes landed.txt and then floods stdout past the cap. The run
    is blocked with an output-limit incident listing the new file, and the file holds a single write because the write
    task was not replayed.
    """
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    emitter = "from pathlib import Path\nimport sys\np = Path('landed.txt')\np.write_text(p.read_text() + 'once' if p.exists() else 'once')\nsys.stdout.buffer.write(b'X' * 400000)\nsys.stdout.flush()\n"
    resolve_command = bridge_call._resolved_command
    monkeypatch.setattr(
        bridge_call,
        "_resolved_command",
        lambda command: [sys.executable, "-c", emitter] if command[0] == "codex" else resolve_command(command),
    )
    result = bridge_call.run_request(_request(tmp_path, verb="implement"))
    incident = json.loads((tmp_path / result["incident"]).read_text())
    assert result["status"] == "blocked"
    assert incident["fault"] == "output-limit"
    assert "?? landed.txt" in incident["workspace_delta"]
    assert (tmp_path / "landed.txt").read_text() == "once"


@pytest.mark.usefixtures("pure_python_json_scanner")
def test_deeply_nested_command_event_does_not_crash_reader() -> None:
    """Retain bounded raw JSONL when parser recursion limits prevent compaction.

    A command-output event carries a field nested 10,000 levels deep under the recursive scanner. Compaction cannot
    parse it, so the raw line is retained unchanged and the reader thread does not die with RecursionError.
    """
    line = (
        '{"type":"item.completed","item":{"type":"command_execution","aggregated_output":"x","extra":'
        + "[" * 10_000
        + "0"
        + "]" * 10_000
        + "}}\n"
    ).encode("utf-8")
    output = bridge_call._ChildOutputBuffer()

    with pytest.raises(RecursionError):
        json.loads(line.decode("utf-8"))

    output.append("stdout", line)

    assert output.output_limited.is_set() is False
    assert output.text()[0].encode("utf-8") == line


@pytest.mark.parametrize(
    "line",
    [
        pytest.param(
            b'{"type":"item.completed","item":{"type":"command_execution","aggregated_output":"\\ud800"}}\n',
            id="unpaired-surrogate",
        ),
        pytest.param(
            b'{"type":"item.completed","item":{"type":"command_execution","aggregated_output":"bad"\n',
            id="invalid-json",
        ),
    ],
)
def test_unparseable_command_event_remains_bounded_and_unchanged(line: bytes) -> None:
    """Keep malformed host text from crashing a reader thread during compaction.

    A command-output line holds an unpaired surrogate escape or invalid JSON. The reader keeps the line byte for byte
    without tripping the limit, so malformed host text cannot kill the reader thread mid-run.
    """
    output = bridge_call._ChildOutputBuffer()

    output.append("stdout", line)

    assert output.text()[0].encode("utf-8") == line
    assert output.output_limited.is_set() is False


def test_output_limit_returns_a_terminal_envelope_and_classified_incident(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prevent an output-cap kill from degrading into an unclassified subprocess failure.

    The child outcome reports a kill at the capture cap with partial output. The envelope is blocked with the cap
    message as its blocker and the incident is classified output-limit, not a generic child failure.
    """
    monkeypatch.setattr(
        bridge_call,
        "_run_child",
        lambda *args: bridge_call.ChildOutcome(
            "partial output", "", -9, False, "child output exceeded 262144 byte capture limit", True
        ),
    )

    result = bridge_call.run_request(_request(tmp_path))
    incident = json.loads((tmp_path / result["incident"]).read_text(encoding="utf-8"))

    assert result["status"] == "blocked"
    assert result["blockers"] == ["child output exceeded 262144 byte capture limit"]
    assert incident["fault"] == "output-limit"


def _inherited_pipe_fixture(
    tmp_path: Path, startup_delay: float, publication_failure: bool = False
) -> tuple[Path, Path, str]:
    """Construct real parent/descendant sources with bounded PID-publication readiness."""
    child_pid_path = tmp_path / "inherited-pipe-child.pid"
    result_path = tmp_path / "inherited-pipe-result.txt"
    # The pid path is embedded in the child source rather than passed through the
    # environment: _run_child filters the child environment down to an allowlist, so a
    # test-only variable would not survive to be read here.
    parent = (
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        '\'import sys, time; time.sleep(0.1); sys.stdout.buffer.write(b\\"x\\" * 400000); '
        "sys.stdout.flush(); time.sleep(60)'])\n"
        f"path = Path({str(child_pid_path)!r})\n"
        "pending = path.with_suffix('.tmp')\n"
        "try:\n"
        "    pending.write_text(str(child.pid), encoding='utf-8')\n"
        f"    time.sleep({startup_delay})\n"
        f"    if {publication_failure}:\n"
        "        raise OSError('injected PID publication failure')\n"
        "    pending.replace(path)\n"
        "except BaseException:\n"
        "    child.kill()\n"
        "    child.wait(timeout=3.0)\n"
        "    raise\n"
    )
    runner = (
        "import sys, time\n"
        f"sys.path.insert(0, {str(BIN_ROOT)!r})\n"
        "import bridge_call\n"
        "from pathlib import Path\n"
        "real_popen = bridge_call.subprocess.Popen\n"
        "def start_ready_process(command, **kwargs):\n"
        "    '''Keep real fixture startup outside the supervision deadline under test.'''\n"
        "    process = real_popen(command, **kwargs)\n"
        f"    if command[-1] != {parent!r}:\n"
        "        return process\n"
        "    deadline = time.monotonic() + 10.0\n"
        "    try:\n"
        f"        while not Path({str(child_pid_path)!r}).is_file():\n"
        "            if process.poll() is not None or time.monotonic() >= deadline:\n"
        "                raise RuntimeError('fixture leader exited or PID publication timed out')\n"
        "            time.sleep(0.01)\n"
        "        process.wait(timeout=max(0.01, deadline - time.monotonic()))\n"
        "        return process\n"
        "    except BaseException:\n"
        "        bridge_call._terminate_process_group(process)\n"
        "        for stream in (process.stdout, process.stderr):\n"
        "            if stream is not None:\n"
        "                stream.close()\n"
        "        raise\n"
        "bridge_call.subprocess.Popen = start_ready_process\n"
        f"outcome = bridge_call._run_child([sys.executable, '-c', {parent!r}], Path({str(tmp_path)!r}), 1.0)\n"
        f"Path({str(result_path)!r}).write_text(str(outcome.output_limited), encoding='utf-8')\n"
    )
    return child_pid_path, result_path, runner


@pytest.mark.integration
@pytest.mark.parametrize("startup_delay", [0.0, 1.2])
def test_normal_leader_exit_with_an_inherited_pipe_cannot_block_output_drain(
    tmp_path: Path, startup_delay: float, pid_files: list[Path]
) -> None:
    """Prevent completed-leader output draining from racing fixture PID publication.

    A real supervised leader exits normally while a descendant keeps the inherited pipe open and floods it, with the
    descendant's PID published immediately or after 1.2 seconds. Draining must still end as output-limited rather than
    hanging.
    """
    child_pid_path, result_path, runner = _inherited_pipe_fixture(tmp_path, startup_delay)
    pid_files.extend([child_pid_path, child_pid_path.with_suffix(".tmp")])

    # Fixture readiness has its own 10s bound; the original 20s margin still
    # covers native supervision, tree termination, and inherited-pipe cleanup.
    completed = subprocess.run(
        [sys.executable, "-c", runner],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    assert result_path.read_text(encoding="utf-8") == "True"


@pytest.mark.integration
def test_inherited_pipe_fixture_reclaims_descendant_when_pid_publication_fails(
    tmp_path: Path, pid_files: list[Path]
) -> None:
    """Prevent fixture startup errors from orphaning an unpublished descendant.

    The fixture is told to fail while publishing the descendant's PID. The fixture runner must exit non-zero with its
    readiness message, publish no PID file, write no result, and leave no live descendant, so a broken fixture never
    leaks processes.
    """
    child_pid_path, result_path, runner = _inherited_pipe_fixture(tmp_path, 0.0, publication_failure=True)
    staged_pid_path = child_pid_path.with_suffix(".tmp")
    pid_files.extend([child_pid_path, staged_pid_path])

    completed = subprocess.run(
        [sys.executable, "-c", runner],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )

    assert completed.returncode != 0
    assert b"fixture leader exited or PID publication timed out" in completed.stderr
    assert not child_pid_path.exists()
    assert not result_path.exists()
    assert not _process_exists(int(staged_pid_path.read_text(encoding="utf-8")))


def test_output_overflow_detected_while_draining_a_completed_leader_is_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prevent a late reader overflow from being returned as a successful child completion.

    The leader has already exited and the reader-join step is stubbed to flag the output limit while draining. The
    outcome must be output-limited with the cap message and the process group terminated once, not a successful
    completion.
    """
    captured: list[bridge_call._ChildOutputBuffer] = []
    terminated: list[bool] = []

    class _Process:
        """Expose the minimal completed-process surface used by the drain path."""

        stdout = object()
        stderr = object()
        returncode = 0

        def poll(self) -> int:
            """Report that the fake process has already exited."""
            return 0

    monkeypatch.setattr(bridge_call.subprocess, "Popen", lambda *args, **kwargs: _Process())
    monkeypatch.setattr(
        bridge_call,
        "_start_output_readers",
        lambda process, output, readers: captured.append(output),
    )
    monkeypatch.setattr(
        bridge_call,
        "_finish_output_readers",
        lambda readers: captured[0].output_limited.set() or True,
    )
    monkeypatch.setattr(bridge_call, "_terminate_process_group", lambda process: terminated.append(True))

    outcome = bridge_call._run_child([sys.executable], tmp_path, timeout=1.0)

    assert outcome.output_limited is True
    assert outcome.error == f"child output exceeded {bridge_call.MAX_CHILD_OUTPUT_BYTES} byte capture limit"
    assert terminated == [True]


def test_completed_child_leaves_no_capture_pipe_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Close both capture pipes once a supervised child exits and its readers finish.

    A real child prints one line and exits. The outcome carries that line, and both pipe file objects of the real
    process are closed, so a long-lived host does not keep two descriptors per call open until garbage collection.
    """
    launched: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen

    def _recording_popen(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        """Launch the real child and keep it for pipe-state assertions."""
        process = real_popen(*args, **kwargs)
        launched.append(process)
        return process

    monkeypatch.setattr(bridge_call.subprocess, "Popen", _recording_popen)

    outcome = bridge_call._run_child([sys.executable, "-c", "print('done')"], tmp_path, timeout=10.0)

    assert outcome.stdout.strip() == "done"
    assert (launched[0].stdout.closed, launched[0].stderr.closed) == (True, True)


def test_finalized_capture_rejects_late_reader_mutation() -> None:
    """Freeze the reported snapshot even if an inherited pipe later resumes writing.

    After text() freezes a snapshot, an inherited pipe delivers more than a full cap on stderr. The snapshot, captured
    byte count and limit flag stay unchanged, so late bytes cannot rewrite or overflow an already reported result.
    """
    output = bridge_call._ChildOutputBuffer()
    output.append("stdout", b"accepted\n")
    snapshot = output.text()
    output.append("stderr", b"X" * (bridge_call.MAX_CHILD_OUTPUT_BYTES + 1))
    assert output.text() == snapshot
    assert output.captured_bytes == len(b"accepted\n")
    assert not output.output_limited.is_set()


@pytest.fixture
def held_pipe_release() -> Iterator[threading.Event]:
    """Event that lets blocked fake-pipe readers finish at teardown so no reader thread outlives the test."""
    release = threading.Event()
    yield release
    release.set()


@pytest.mark.parametrize("termination", ["timeout", "cancel", "exit"])
def test_undrained_peer_blocks_without_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, termination: str, held_pipe_release: threading.Event
) -> None:
    """Prevent a timeout replay when real reader threads outlive bounded cleanup joins.

    Fake pipes stay open after the leader is reaped, so real reader threads outlive the 2 second cleanup join, for a
    timeout, a cancel marker or a normal exit. The run is blocked on the drain grace with a non-timeout fault and
    exactly one launch.
    """
    release = held_pipe_release
    job_path = tmp_path / "held-job.json"
    launches = []

    class HeldPipe:
        """Model an external pipe remaining open after its leader has been reaped."""

        def __init__(self) -> None:
            self.remaining = 400_000

        def read(self, size: int) -> bytes:
            """Release bounded chunks only after the supervisor has returned."""
            if not release.wait(timeout=15):
                return b""
            count = min(size, self.remaining)
            self.remaining -= count
            return b"X" * count

    class Process:
        """Expose only external Popen state; bridge readers and joins remain real."""

        pid = 123456

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.stdout = HeldPipe()
            self.stderr = HeldPipe()
            self.returncode = 0 if termination == "exit" else None
            launches.append(self)
            if termination == "cancel":
                job_path.with_name("held-job.cancel.json").write_text('{"job_id":"held-job"}', encoding="utf-8")

        def poll(self) -> int | None:
            """Return the observed leader state."""
            return self.returncode

        def kill(self) -> None:
            """Reap the leader without pretending its inherited pipes closed."""
            self.returncode = 0

        def wait(self, timeout: float) -> int | None:
            """Expose the reaped leader while reader threads remain alive."""
            return self.returncode

    def missing_group(*args: Any) -> None:
        """Simulate an absent process group so no real PID is ever signaled."""
        raise ProcessLookupError

    monkeypatch.setattr(bridge_call.subprocess, "Popen", Process)
    monkeypatch.setattr(bridge_call.os, "killpg", missing_group, raising=False)
    monkeypatch.setattr(bridge_call, "_resolve_executable", lambda name: name)
    monkeypatch.setattr(bridge_call.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1))

    result = bridge_call.run_request(_request(tmp_path, timeout_seconds=0.05), _job_path=job_path)

    incident = json.loads((tmp_path / result["incident"]).read_text())
    assert result["status"] == "blocked"
    assert result["blockers"] == ["child output drain exceeded the Bridge cleanup grace"]
    assert incident["fault"] != "timeout"
    assert len(launches) == 1


def test_timeout_retries_read_only_once_at_a_lower_tier_but_never_implement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prevent a timeout retry from escalating cost, duplicating implement edits, or looping on advise.

    Every attempt times out for advise at medium effort, advise already at the lowest effort, and implement at medium.
    Only the first retries once at low; the floor and the write verb run once and record no effort substitution.
    """
    efforts: list[str] = []

    def _timeout_child(command: list[str], workspace: Path, timeout: float) -> bridge_call.ChildOutcome:
        """Return a timeout result while recording each retry effort."""
        efforts.append(next(item for item in command if item.startswith("model_reasoning_effort=")))
        return _outcome(timed_out=True)

    monkeypatch.setattr(bridge_call, "_run_child", _timeout_child)
    advise = bridge_call.run_request(_request(tmp_path, effort="medium", supported_efforts=("low", "medium")))
    floor = bridge_call.run_request(_request(tmp_path, effort="low", supported_efforts=("low", "medium")))
    implement = bridge_call.run_request(
        _request(tmp_path, verb="implement", effort="medium", supported_efforts=("low", "medium"))
    )

    assert efforts == [
        'model_reasoning_effort="medium"',
        'model_reasoning_effort="low"',
        'model_reasoning_effort="low"',
        'model_reasoning_effort="medium"',
    ]
    assert advise["status"] == "timeout"
    assert advise["effort_substituted"] == {"requested": "medium", "applied": "low", "reason": "timeout retry"}
    assert floor["status"] == "timeout"
    assert floor["effort_substituted"] is None
    assert implement["status"] == "timeout"
    assert implement["effort_substituted"] is None


@pytest.mark.parametrize(
    ("failure", "status"),
    [
        pytest.param({"timed_out": True}, "timeout", id="timeout-retry"),
        pytest.param({"error": "unsupported effort"}, "blocked", id="unsupported-effort-retry"),
    ],
)
def test_retry_that_would_not_fit_the_transport_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: dict[str, Any], status: str
) -> None:
    """A lower-effort retry whose prompt no longer fits is skipped, so the first result returns instead of an error.

    A longer tier name (``high`` to ``medium``) can push a task near the limit past it; before this guard the retry re-
    entered the request runner and raised, leaving an orphan incident and no envelope.
    """
    launches: list[list[str]] = []

    def lowered_does_not_fit(request: bridge_call.Request) -> None:
        if request.effort == "low":
            raise ValueError("task exceeds Bridge's encoded transport budget")

    def child(command: list[str], workspace: Path, timeout: float) -> bridge_call.ChildOutcome:
        launches.append(command)
        return _outcome(**failure)

    monkeypatch.setattr(bridge_call, "validate_request_transport_budget", lowered_does_not_fit)
    monkeypatch.setattr(bridge_call, "_run_child", child)

    result = bridge_call.run_request(_request(tmp_path, effort="medium", supported_efforts=("low", "medium")))

    assert len(launches) == 1
    assert result["status"] == status
    assert result["effort_substituted"] is None


def test_parse_output_preserves_session_usage_and_cost() -> None:
    """Parsing a child's stdout keeps the session id, token usage, and cost beside the review core.

    A future refactor that renames or drops one of these fields would otherwise silently lose the evidence a caller
    needs to attribute cost and continue the same session.
    """
    parsed = bridge_call._parse_output(_outcome(core=_core()).stdout, "codex")

    assert parsed.core == _core()
    assert parsed.session_id == "session-fixed"
    assert parsed.tokens == {"input": 3.0, "output": 2.0}
    assert parsed.cost == 0.01


def test_timeout_incident_records_workspace_delta_and_health_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child timeout still records the workspace delta in the incident file and the health log.

    Losing this evidence after a cutoff would leave the operator unable to tell whether the peer's partial edits landed
    on disk before the child was terminated.
    """
    snapshots = iter([[], [" M landed.py"]])
    monkeypatch.setattr(bridge_call, "_workspace_state", lambda workspace: next(snapshots))
    monkeypatch.setattr(bridge_call, "_run_child", lambda *args: _outcome(timed_out=True))

    result = bridge_call.run_request(_request(tmp_path, verb="implement"))

    incident = json.loads((tmp_path / result["incident"]).read_text(encoding="utf-8"))
    health = [json.loads(line) for line in (tmp_path / ".temp" / "bridge" / "health.jsonl").read_text().splitlines()]
    assert result["status"] == "timeout"
    assert incident["workspace_delta"] == [" M landed.py"]
    assert health[-1]["run_id"] == "run-fixed"
    assert health[-1]["status"] == "timeout"


def test_claude_parser_preserves_top_level_aggregate_cost() -> None:
    """Prevent a valid Claude result from losing cost stored beside nested usage counters.

    A Claude result carries the cost at top level while token counters sit in a nested usage object. Parsing keeps the
    structured core, float token counts, the aggregate cost and the session id.
    """
    stdout = json.dumps(
        {
            "structured_output": _core(),
            "usage": {"input_tokens": 41, "output_tokens": 7},
            "total_cost_usd": 0.451996,
            "session_id": "claude-session-fixed",
        }
    )

    parsed = bridge_call._parse_output(stdout, "claude")

    assert parsed.core == _core()
    assert parsed.tokens == {"input_tokens": 41.0, "output_tokens": 7.0}
    assert parsed.cost == 0.451996
    assert parsed.session_id == "claude-session-fixed"


def test_claude_aggregate_cost_reaches_public_envelope_and_health_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prevent correct Claude parsing from being dropped by envelope or accounting wiring.

    A stubbed Claude child returns a result with usage and total_cost_usd for a codex_to_claude run. The public envelope
    and the latest health record both carry the cost, tokens and direction.
    """
    provider_result = {
        "structured_output": _core(),
        "usage": {"input_tokens": 41, "output_tokens": 7},
        "total_cost_usd": 0.451996,
    }
    monkeypatch.setattr(
        bridge_call,
        "_run_child",
        lambda *args: bridge_call.ChildOutcome(json.dumps(provider_result), "", 0, False, None),
    )

    result = bridge_call.run_request(_request(tmp_path, direction="codex_to_claude"), host="claude")

    health = [json.loads(line) for line in (tmp_path / ".temp" / "bridge" / "health.jsonl").read_text().splitlines()]
    assert result["cost"] == 0.451996
    assert result["tokens"] == {"input_tokens": 41.0, "output_tokens": 7.0}
    assert health[-1]["cost"] == 0.451996
    assert health[-1]["direction"] == "codex_to_claude"


def test_claude_provider_error_preserves_bounded_api_context_and_zero_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prevent Claude rate-limit failures from degrading to a generic child-exit verdict.

    A Claude child exits 1 with an api_error result for HTTP 429 and a long message. The blocked verdict names the
    status and message, zero usage is kept, the incident records the reason, and transcript-only details stay out of the
    verdict.
    """
    provider_message = "Spend limit reached; " + "x" * 1000
    provider_result = {
        "is_error": True,
        "terminal_reason": "api_error",
        "api_error_status": 429,
        "result": provider_message,
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "details": ["Transcript-only provider diagnostic."],
    }
    monkeypatch.setattr(
        bridge_call,
        "_run_child",
        lambda *args: bridge_call.ChildOutcome(json.dumps(provider_result), "", 1, False, None),
    )

    result = bridge_call.run_request(_request(tmp_path, direction="codex_to_claude"), host="claude")

    incident = json.loads((tmp_path / result["incident"]).read_text(encoding="utf-8"))
    transcript = (tmp_path / result["transcript_path"]).read_text(encoding="utf-8")
    assert result["status"] == "blocked"
    assert result["verdict"].startswith("api_error: HTTP 429: Spend limit reached; ")
    assert result["verdict"] == "api_error: HTTP 429: " + provider_message
    assert result["tokens"] == {"input_tokens": 0.0, "output_tokens": 0.0}
    assert "details" not in result
    assert incident["fault"] == "blocked"
    assert incident["reason"] == result["verdict"]
    assert provider_result["details"][0] in transcript
    assert provider_result["details"][0] not in result["verdict"]


def test_reverse_read_only_maximum_budgets_both_hard_cutoff_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prevent the reverse retry path from outliving the MCP host deadline.

    A reverse advise request with the maximum MCP timeout times out twice at medium then low effort. Both attempts get
    the hard cutoff, and their sum plus the response margin stays under the MCP host deadline.
    """
    hard_cutoffs: list[float] = []

    def _timeout_child(command: list[str], workspace: Path, timeout: float) -> bridge_call.ChildOutcome:
        """Return timeout outcomes while recording each bounded retry deadline."""
        hard_cutoffs.append(timeout)
        return _outcome(timed_out=True)

    monkeypatch.setattr(bridge_call, "_run_child", _timeout_child)
    maximum = bridge_mcp.MAX_MCP_TIMEOUT_SECONDS_BY_VERB["advise"]
    result = bridge_call.run_request(
        _request(
            tmp_path,
            direction="codex_to_claude",
            timeout_seconds=maximum,
            effort="medium",
            supported_efforts=("low", "medium"),
        ),
        host="claude",
    )

    assert result["status"] == "timeout"
    assert hard_cutoffs == [maximum * bridge_call.CHILD_TIMEOUT_MULTIPLIER] * 2
    assert sum(hard_cutoffs) + bridge_mcp.MCP_RESPONSE_MARGIN_SECONDS < bridge_mcp.MCP_HOST_DEADLINE_SECONDS


def test_job_lifecycle_uses_workspace_local_record_and_missing_signal_doubles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prevent status/cancel from depending on host process signalling or home-state records.

    A running job record with a PID exists in the workspace store while os.kill and os.killpg fail the test. An unknown
    job reads as missing, and cancel only flips the record to cancel_requested without signalling any process.
    """
    paths = bridge_call.BridgePaths(tmp_path)
    paths.prepare()
    job_id = "11111111-1111-4111-8111-111111111111"
    job_path = paths.jobs / f"{job_id}.json"
    job_path.write_text(json.dumps({"job_id": job_id, "status": "running", "pid": 42}), encoding="utf-8")
    # Liveness probing has its own tests; pin it alive so this test observes
    # only cancel semantics, deterministically on every host.
    monkeypatch.setattr(bridge_call, "_process_exists", lambda pid: True)
    monkeypatch.setattr(bridge_call.os, "kill", lambda *args: pytest.fail("cancellation signalled a persisted PID"))
    monkeypatch.setattr(
        bridge_call.os, "killpg", lambda *args: pytest.fail("cancellation signalled a process group"), raising=False
    )

    assert bridge_call.job_status(tmp_path, "22222222-2222-4222-8222-222222222222")["status"] == "missing"
    cancelled = bridge_call.cancel_job(tmp_path, job_id)

    assert cancelled["status"] == "cancel_requested"
    assert bridge_call.job_status(tmp_path, job_id)["status"] == "cancel_requested"


@pytest.mark.parametrize("command", ["status", "result", "cancel"])
def test_lifecycle_cli_rejects_job_identifier_traversal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    """Prevent lifecycle commands from reading or rewriting records outside the job store.

    status, result and cancel each receive the job id ../escaped while a real record sits at that escaped path. All exit
    2 with a job-related error and the escaped record is untouched.
    """
    paths = bridge_call.BridgePaths(tmp_path)
    paths.prepare()
    escaped_record = paths.root / "escaped.json"
    original = {"job_id": "escaped", "status": "finished", "pid": 42}
    escaped_record.write_text(json.dumps(original), encoding="utf-8")

    exit_code = bridge_call.main([command, "--job-id", "../escaped", "--workspace", str(tmp_path)])
    response = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert response["status"] == "error"
    assert "job" in response["error"].lower()
    assert json.loads(escaped_record.read_text(encoding="utf-8")) == original


def test_supervisor_rejects_job_identifier_traversal_before_writing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prevent a detached supervisor from creating or overwriting an escaped job record.

    A supervisor invocation is given the job id ../escaped with run_request wired to fail the test. It exits 2 with a
    job error before dispatching or writing, and the pre-existing escaped record is unchanged.
    """
    paths = bridge_call.BridgePaths(tmp_path)
    paths.prepare()
    escaped_record = paths.root / "escaped.json"
    original = {"job_id": "escaped", "status": "finished", "pid": 42}
    escaped_record.write_text(json.dumps(original), encoding="utf-8")
    monkeypatch.setattr(
        bridge_call, "run_request", lambda request, **kwargs: pytest.fail("invalid supervisor dispatched")
    )

    exit_code = bridge_call.main(
        [
            "advise",
            "--task",
            "Keep the record contained.",
            "--job-id",
            "../escaped",
            "--supervisor",
            "--workspace",
            str(tmp_path),
        ]
    )
    response = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert response["status"] == "error"
    assert "job" in response["error"].lower()
    assert json.loads(escaped_record.read_text(encoding="utf-8")) == original


@pytest.mark.parametrize(
    ("denials", "outcome", "expected"),
    [
        pytest.param(0, contextlib.nullcontext(), {"job_id": "fresh"}, id="no-contention"),
        pytest.param(3, contextlib.nullcontext(), {"job_id": "fresh"}, id="transient-sharing-violation"),
        pytest.param(
            bridge_call._REPLACE_ATTEMPTS,
            pytest.raises(PermissionError),
            {"job_id": "stale"},
            id="held-open-past-the-retry-budget",
        ),
    ],
)
def test_write_json_retries_a_rename_blocked_by_a_concurrent_reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    denials: int,
    outcome: contextlib.AbstractContextManager[Any],
    expected: dict[str, str],
) -> None:
    """A rename that a concurrent reader blocks is retried; only a persistent block raises.

    On Windows renaming over a file another process has open fails with ``PermissionError`` (a sharing violation). The
    supervisor polls the cancel marker and job record while the caller rewrites them, so a second ``cancel_job`` or a
    record update lands in that window routinely. The retry must absorb a short block, still raise when the file stays
    held past the budget, and never leave the temporary file behind in either case.
    """
    target = tmp_path / "job.cancel.json"
    target.write_text('{"job_id": "stale"}\n', encoding="utf-8")
    real_replace = Path.replace
    attempts: list[int] = []

    def blocked_replace(self: Path, other: Path) -> Path:
        attempts.append(len(attempts))
        if len(attempts) <= denials:
            raise PermissionError(5, "Access is denied", str(other))
        return real_replace(self, other)

    monkeypatch.setattr(Path, "replace", blocked_replace)
    monkeypatch.setattr(bridge_call, "_REPLACE_RETRY_SECONDS", 0.0)

    with outcome:
        bridge_call._write_json(target, {"job_id": "fresh"})

    assert json.loads(target.read_text(encoding="utf-8")) == expected
    assert len(attempts) == min(denials + 1, bridge_call._REPLACE_ATTEMPTS)
    assert [p.name for p in tmp_path.iterdir()] == ["job.cancel.json"]


def test_background_dispatch_leaves_pid_ownership_to_the_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prevent the launcher from racing its supervisor's record writes or losing the result.

    A background launch with a stubbed Popen writes a queued record whose pid is still null. After the record is
    rewritten as finished with a PID and result, status and result read back that data, so the launcher never owns the
    PID and cannot race the supervisor's writes.
    """

    class _Process:
        """Expose only a stable PID for background-launch assertions."""

        pid = 123

    monkeypatch.setattr(bridge_call.subprocess, "Popen", lambda *args, **kwargs: _Process())
    started = bridge_call.start_background(_request(tmp_path, background=True))
    record_path = tmp_path.resolve() / ".temp" / "bridge" / "jobs" / f"{started['job_id']}.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))

    assert started["status"] == "queued"
    assert record["status"] == "queued"
    assert record["pid"] is None
    assert isinstance(record["started_ts"], float)
    assert record["request"]["run_id"] == "run-fixed"

    record.update({"status": "finished", "pid": 123, "result": _finished_envelope()})
    bridge_call._write_json(record_path, record)

    assert bridge_call.job_status(tmp_path.resolve(), started["job_id"]) == {
        "job_id": started["job_id"],
        "status": "finished",
        "pid": 123,
    }
    assert bridge_call.job_result(tmp_path.resolve(), started["job_id"]) == _finished_envelope()


def _finished_envelope() -> dict[str, object]:
    """Return a complete valid public envelope for stored-job-result fixtures.

    Examples:
        >>> envelope = _finished_envelope()
        >>> envelope["status"], envelope["direction"]
        ('complete', 'claude_to_codex')
    """
    return {
        "status": "complete",
        "verdict": "done",
        "findings": [],
        "files_touched": [],
        "remaining": [],
        "blockers": [],
        "model": "test-model",
        "effort": "medium",
        "effort_substituted": None,
        "cost": None,
        "tokens": {},
        "duration_seconds": 0.0,
        "depth": 0,
        "run_id": "run-fixed",
        "incident": None,
        "session_id": None,
        "transcript_path": ".temp/bridge/raw-fixed.txt",
        "verb": "implement",
        "direction": "claude_to_codex",
    }


def test_job_result_rejects_a_tampered_stored_envelope(tmp_path: Path) -> None:
    """Prevent a hand-edited job record from masquerading as a validated envelope.

    A finished job record holds an envelope with an invalid status, first alone and then beside a cancel marker. Reading
    the result raises both times, so a hand-edited record or marker cannot pass as a validated envelope.
    """
    paths = bridge_call.BridgePaths(tmp_path)
    paths.prepare()
    job_id = "88888888-8888-4888-8888-888888888888"
    tampered = {**_finished_envelope(), "status": "totally-fine"}
    bridge_call._write_json(
        paths.jobs / f"{job_id}.json",
        {"job_id": job_id, "status": "finished", "pid": None, "result": tampered},
    )

    with pytest.raises(ValueError, match="unsupported status"):
        bridge_call.job_result(tmp_path, job_id)

    bridge_call._write_json(paths.jobs / f"{job_id}.cancel.json", {"job_id": job_id})

    with pytest.raises(ValueError, match="unsupported status"):
        bridge_call.job_result(tmp_path, job_id)


def test_help_token_matching_requires_word_boundaries() -> None:
    """Prevent a removed CLI subcommand from passing preflight via a prose substring.

    Help-text tokens such as exec and --json are matched against realistic and misleading strings. Only whole tokens
    count, so a prose word like execute or an option like --json-schema cannot satisfy a removed subcommand.
    """
    assert bridge_diagnose._token_present("exec", "codex exec --json") is True
    assert bridge_diagnose._token_present("exec", "commands execute quickly") is False
    assert bridge_diagnose._token_present("--json", "supports --json output") is True
    assert bridge_diagnose._token_present("--json", "supports --json-schema output") is False


def test_background_dispatch_omits_unselected_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent a host-default background request from passing a null subprocess argument.

    A background request has no model selected while Popen is stubbed to capture argv. The supervisor command omits the
    model flag entirely and holds no None entries, which would otherwise crash process launch.
    """

    class _Process:
        """Provide the child API needed to inspect inherited environment state."""

        pid = 123

    commands: list[list[object]] = []

    def _fake_popen(command: list[object], **kwargs: object) -> _Process:
        """Record background argv and return a process with a stable PID."""
        commands.append(command)
        return _Process()

    monkeypatch.setattr(bridge_call.subprocess, "Popen", _fake_popen)
    bridge_call.start_background(_request(tmp_path, background=True, model=None))

    assert commands
    assert "--model" not in commands[0]
    assert all(argument is not None for argument in commands[0])


def test_cancel_does_not_signal_a_stale_persisted_pid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent a stale job record from targeting an unrelated recycled process.

    A running record names PID 42 while liveness is forced true and os.kill, os.killpg and subprocess.run fail the test.
    Cancel only writes the marker and reports cancel_requested, since a recorded PID may now belong to an unrelated
    process.
    """
    paths = bridge_call.BridgePaths(tmp_path.resolve())
    paths.prepare()
    job_id = "33333333-3333-4333-8333-333333333333"
    job_path = paths.jobs / f"{job_id}.json"
    job_path.write_text(json.dumps({"job_id": job_id, "status": "running", "pid": 42}), encoding="utf-8")
    monkeypatch.setattr(bridge_call, "_process_exists", lambda pid: True)
    monkeypatch.setattr(bridge_call.os, "kill", lambda *args: pytest.fail("stale PID was signalled"))
    monkeypatch.setattr(
        bridge_call.os, "killpg", lambda *args: pytest.fail("stale process group was signalled"), raising=False
    )
    monkeypatch.setattr(
        bridge_call.subprocess, "run", lambda *args, **kwargs: pytest.fail("stale PID reached taskkill")
    )

    cancelled = bridge_call.cancel_job(tmp_path, job_id)

    assert cancelled["status"] == "cancel_requested"
    assert bridge_call.job_status(tmp_path, job_id)["status"] == "cancel_requested"


@pytest.fixture
def exited_pid() -> int:
    """PID of a real process that has already exited and been reaped."""
    exited = subprocess.Popen([sys.executable, "-c", "pass"], stdin=subprocess.DEVNULL)
    exited.wait()
    return exited.pid


def test_dead_supervisor_running_record_is_reported_stalled(tmp_path: Path, exited_pid: int) -> None:
    """Prevent a killed supervisor from leaving a job that reports running forever.

    Two running records exist: one names the PID of a real exited process, the other the live test process. Status and
    result report the first as stalled and the second as running.
    """
    paths = bridge_call.BridgePaths(tmp_path)
    paths.prepare()
    stalled_id = "44444444-4444-4444-8444-444444444444"
    live_id = "55555555-5555-4555-8555-555555555555"
    (paths.jobs / f"{stalled_id}.json").write_text(
        json.dumps({"job_id": stalled_id, "status": "running", "pid": exited_pid}), encoding="utf-8"
    )
    (paths.jobs / f"{live_id}.json").write_text(
        json.dumps({"job_id": live_id, "status": "running", "pid": os.getpid()}), encoding="utf-8"
    )

    assert bridge_call.job_status(tmp_path, stalled_id)["status"] == "stalled"
    assert bridge_call.job_result(tmp_path, stalled_id)["status"] == "stalled"
    assert bridge_call.job_status(tmp_path, live_id)["status"] == "running"


def test_simulated_windows_stalled_probe_uses_tasklist_and_never_signals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prevent the Windows liveness probe from using os.kill termination semantics.

    With os.name simulated as nt, a running record is probed through a fake tasklist.EXE on PATH that reports no tasks
    while os.kill fails the test. Status is stalled and the first spawned command is the resolved tasklist path.
    """
    paths = bridge_call.BridgePaths(tmp_path)
    paths.prepare()
    job_id = "77777777-7777-4777-8777-777777777777"
    (paths.jobs / f"{job_id}.json").write_text(
        json.dumps({"job_id": job_id, "status": "running", "pid": 4242}), encoding="utf-8"
    )
    probes: list[list[str]] = []
    tasklist = tmp_path / "tasklist.EXE"
    tasklist.touch()
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("PATHEXT", ".EXE")

    def _fake_tasklist(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        """Return tasklist's no-process response for the Windows probe."""
        probes.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="INFO: No tasks are running.", stderr="")

    monkeypatch.setattr(bridge_call.os, "name", "nt")
    monkeypatch.setattr(bridge_call.os, "kill", lambda *args: pytest.fail("liveness probe signalled a PID"))
    monkeypatch.setattr(bridge_call.subprocess, "run", _fake_tasklist)

    assert bridge_call.job_status(tmp_path, job_id)["status"] == "stalled"
    assert probes
    assert probes[0][0] == str(tasklist)


def test_cancelling_a_stalled_job_keeps_the_terminal_stalled_signal(tmp_path: Path, exited_pid: int) -> None:
    """Prevent a cancel on a dead supervisor from masking stalled behind cancel_requested forever.

    A running record names a dead supervisor PID. Cancelling returns stalled, writes no cancel marker, and later status
    and result remain stalled instead of being masked by cancel_requested.
    """
    paths = bridge_call.BridgePaths(tmp_path)
    paths.prepare()
    job_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    (paths.jobs / f"{job_id}.json").write_text(
        json.dumps({"job_id": job_id, "status": "running", "pid": exited_pid}), encoding="utf-8"
    )

    cancelled = bridge_call.cancel_job(tmp_path, job_id)

    assert cancelled["status"] == "stalled"
    assert not (paths.jobs / f"{job_id}.cancel.json").exists()
    assert bridge_call.job_status(tmp_path, job_id)["status"] == "stalled"
    assert bridge_call.job_result(tmp_path, job_id)["status"] == "stalled"


def test_expired_queued_record_without_a_supervisor_is_reported_stalled(tmp_path: Path) -> None:
    """Prevent a supervisor that died before its first record write from leaving queued forever.

    A queued record older than the queue stall limit and a fresh queued record are read. The old one reports stalled,
    since its supervisor never wrote anything, and the fresh one is still queued.
    """
    paths = bridge_call.BridgePaths(tmp_path)
    paths.prepare()
    job_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    stale = time.time() - bridge_call.QUEUED_STALL_SECONDS - 1
    (paths.jobs / f"{job_id}.json").write_text(
        json.dumps({"job_id": job_id, "status": "queued", "pid": None, "started_ts": stale}), encoding="utf-8"
    )
    fresh_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    (paths.jobs / f"{fresh_id}.json").write_text(
        json.dumps({"job_id": fresh_id, "status": "queued", "pid": None, "started_ts": time.time()}),
        encoding="utf-8",
    )

    assert bridge_call.job_status(tmp_path, job_id)["status"] == "stalled"
    assert bridge_call.job_status(tmp_path, fresh_id)["status"] == "queued"


def test_cancel_marker_arriving_before_a_retry_stops_the_second_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prevent a retry recursion from dropping the job path and ignoring a cancellation.

    The first attempt times out and writes the job's cancel marker while returning. The run must not retry at a lower
    effort and ends blocked with cancelled by job owner after a single attempt.
    """
    paths = bridge_call.BridgePaths(tmp_path)
    paths.prepare()
    job_id = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    job_path = paths.jobs / f"{job_id}.json"
    attempts: list[str] = []

    def _timeout_then_cancelled(
        command: list[str], workspace: Path, timeout: float, job: Path | None = None
    ) -> bridge_call.ChildOutcome:
        """Write the cancellation marker while returning a timeout."""
        attempts.append(command[-1])
        bridge_call._write_json(job_path.with_name(f"{job_id}.cancel.json"), {"job_id": job_id})
        return _outcome(timed_out=True)

    monkeypatch.setattr(bridge_call, "_run_child", _timeout_then_cancelled)
    result = bridge_call.run_request(
        _request(tmp_path, effort="medium", supported_efforts=("low", "medium")), _job_path=job_path
    )

    assert len(attempts) == 1
    assert result["status"] == "blocked"
    assert result["blockers"] == ["cancelled by job owner"]


def test_supervisor_internal_failure_writes_a_terminal_failed_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Prevent a supervisor exception from leaving a running record no caller can resolve.

    run_request raises inside a supervisor started against a queued record. The supervisor exits 2 and rewrites the
    record as failed with the error text, so status callers do not wait on a job that can never finish.
    """
    paths = bridge_call.BridgePaths(tmp_path)
    paths.prepare()
    job_id = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
    bridge_call._write_json(
        paths.jobs / f"{job_id}.json", {"job_id": job_id, "status": "queued", "pid": None, "result": None}
    )

    def _exploding_run_request(*args: object, **kwargs: object) -> dict[str, object]:
        """Raise the supervisor error whose terminal record is asserted."""
        raise ValueError("artifact store exploded")

    monkeypatch.setattr(bridge_call, "run_request", _exploding_run_request)
    exit_code = bridge_call.main(
        ["implement", "--task", "Fail internally.", "--job-id", job_id, "--supervisor", "--workspace", str(tmp_path)]
    )
    capsys.readouterr()

    assert exit_code == 2
    assert bridge_call.job_status(tmp_path, job_id)["status"] == "failed"
    record = json.loads((paths.jobs / f"{job_id}.json").read_text(encoding="utf-8"))
    assert record["error"] == "artifact store exploded"


def test_corrupt_cancel_marker_is_ignored_instead_of_killing_the_supervisor(tmp_path: Path) -> None:
    """Prevent a malformed cancellation marker from crashing the poll loop over a live child.

    The cancel marker holds truncated JSON, then valid JSON naming a different job. Both read as not cancelled, so a
    corrupt or foreign marker never aborts a supervisor with a live child.
    """
    paths = bridge_call.BridgePaths(tmp_path)
    paths.prepare()
    job_id = "99999999-9999-4999-8999-999999999999"
    job_path = paths.jobs / f"{job_id}.json"
    (paths.jobs / f"{job_id}.cancel.json").write_text("{truncated", encoding="utf-8")

    assert bridge_call._job_cancel_requested(job_path) is False

    (paths.jobs / f"{job_id}.cancel.json").write_text(json.dumps({"job_id": "someone-else"}), encoding="utf-8")

    assert bridge_call._job_cancel_requested(job_path) is False


@pytest.mark.skipif(
    not DIRECTORY_SYMLINKS_SUPPORTED or not hasattr(os, "O_NOFOLLOW"), reason="needs symlinks and O_NOFOLLOW"
)
def test_cancel_marker_swapped_for_a_symlink_is_not_followed(tmp_path: Path) -> None:
    """A cancellation marker replaced by a link after the leaf check is refused at open, never read through.

    The containment check inspects the leaf with lstat first; a name swapped between that check and the read must not
    let a link elsewhere decide whether the supervisor kills its child.
    """
    paths = bridge_call.BridgePaths(tmp_path)
    paths.prepare()
    job_id = "abababab-abab-4bab-8bab-abababababab"
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps({"job_id": job_id}), encoding="utf-8")
    (paths.jobs / f"{job_id}.cancel.json").symlink_to(elsewhere)

    assert bridge_call._job_cancel_requested(paths.jobs / f"{job_id}.json") is False


class TestProtectedPathGate:
    """A write-capable run names protected workspace paths the peer changed, whether or not it reported them."""

    @pytest.mark.parametrize(
        ("written", "expected"),
        [
            pytest.param(
                {".claude/settings.json": '{"hooks": {}}', ".mcp.json": "{}"},
                ".claude, .claude/settings.json, .mcp.json",
                id="claude-settings-and-mcp-servers",
            ),
            pytest.param({".git/hooks/pre-commit": "#!/bin/sh\n"}, ".git/hooks/pre-commit", id="git-hook"),
        ],
    )
    def test_changed_protected_paths_are_named(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, written: dict[str, str], expected: str
    ) -> None:
        """A planted host setting or Git hook is reported even though the peer listed no file it touched.

        Those files run code in a later host session or Git command; only the child's own report would otherwise reveal
        them, and a hostile child would not report them.
        """
        (tmp_path / ".git" / "hooks").mkdir(parents=True)

        def _planting_child(command: list[str], workspace: Path, timeout: float) -> bridge_call.ChildOutcome:
            """Write the protected files, then report a clean result with no touched files."""
            for relative, text in written.items():
                (workspace / relative).parent.mkdir(parents=True, exist_ok=True)
                (workspace / relative).write_text(text, encoding="utf-8")
            return _outcome(core=_core())

        monkeypatch.setattr(bridge_call, "_run_child", _planting_child)

        envelope = bridge_call.run_request(_request(tmp_path, verb="implement"))

        assert envelope["findings"] == [
            f"Protected workspace path changed during this write-capable peer run: {expected}. Review it before any "
            "host session or Git command trusts this workspace."
        ]

    def test_ordinary_edits_add_no_finding(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Source edits outside the protected set leave findings exactly as the peer reported them.

        A write-capable child edits one ordinary source file and reports no findings. The envelope stays free of
        protected-path findings.
        """

        def _editing_child(command: list[str], workspace: Path, timeout: float) -> bridge_call.ChildOutcome:
            """Edit one ordinary source file."""
            (workspace / "module.py").write_text("x = 1\n", encoding="utf-8")
            return _outcome(core=_core())

        monkeypatch.setattr(bridge_call, "_run_child", _editing_child)

        envelope = bridge_call.run_request(_request(tmp_path, verb="implement"))

        assert envelope["findings"] == []


def test_job_record_over_the_size_bound_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A job record larger than its bound is refused from a bounded read instead of being loaded whole.

    A job record padded past a lowered size bound is read through the bounded reader. It raises a size-bound error
    rather than loading the whole file, which protects the supervisor from oversized or hostile records.
    """
    paths = bridge_call.BridgePaths(tmp_path)
    paths.prepare()
    job_id = "cdcdcdcd-cdcd-4dcd-8dcd-cdcdcdcdcdcd"
    bridge_call._write_json(paths.jobs / f"{job_id}.json", {"job_id": job_id, "status": "queued", "pad": "x" * 256})
    monkeypatch.setattr(bridge_call, "MAX_JOB_RECORD_BYTES", 64)

    with pytest.raises(ValueError, match="exceeds its size bound"):
        bridge_call._read_job(tmp_path, job_id)


class TestSupervisorAlive:
    """A recorded supervisor PID counts as alive only while it still names the same process."""

    @pytest.mark.parametrize(
        ("identity", "age_seconds", "alive"),
        [
            pytest.param((1000, 3600.0), 3590.0, True, id="same-process"),
            pytest.param((0, 3600.0), 3590.0, False, id="pid-reused-by-another-user"),
            pytest.param((1000, 10.0), 3590.0, False, id="pid-reused-by-a-younger-process"),
            pytest.param(None, 3590.0, True, id="ps-unavailable-keeps-existence-verdict"),
        ],
    )
    def test_identity_decides_a_posix_pid(
        self,
        monkeypatch: pytest.MonkeyPatch,
        identity: tuple[int, float] | None,
        age_seconds: float,
        alive: bool,
    ) -> None:
        """Owner and elapsed run time from ps tell the supervisor from a later process that reused its PID.

        Only a positive mismatch counts as dead, so a host without a usable ps keeps the plain existence probe.
        """
        monkeypatch.setattr(bridge_call.os, "name", "posix")
        monkeypatch.setattr(bridge_call.os, "getuid", lambda: 1000, raising=False)
        monkeypatch.setattr(bridge_call, "_process_exists", lambda pid: True)
        monkeypatch.setattr(bridge_call, "_posix_process_identity", lambda pid: identity)

        assert bridge_call._supervisor_alive(4242, time.time() - age_seconds) is alive

    def test_record_without_a_start_timestamp_skips_ps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Records written before the timestamp existed keep the existence probe and never run ps.

        A record written before start timestamps existed passes None as its start time on a simulated POSIX host. Only
        the existence probe runs, the process-identity probe fails the test if called, and the supervisor counts as
        alive.
        """
        monkeypatch.setattr(bridge_call.os, "name", "posix")
        monkeypatch.setattr(bridge_call, "_process_exists", lambda pid: True)
        monkeypatch.setattr(bridge_call, "_posix_process_identity", lambda pid: pytest.fail("ps probed"))

        assert bridge_call._supervisor_alive(4242, None) is True

    def test_reused_pid_reports_the_job_stalled(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A running record whose PID now belongs to another user's process is reported stalled, ending the poll.

        A running record's PID exists but now belongs to uid 0 with an uptime far beyond the record's start time. As the
        owner is a different user and the process is older than the job, status reports stalled.
        """
        paths = bridge_call.BridgePaths(tmp_path)
        paths.prepare()
        job_id = "efefefef-efef-4fef-8fef-efefefefefef"
        record = {"job_id": job_id, "status": "running", "pid": 4242, "pid_started_ts": time.time() - 60}
        bridge_call._write_json(paths.jobs / f"{job_id}.json", record)
        monkeypatch.setattr(bridge_call.os, "name", "posix")
        monkeypatch.setattr(bridge_call.os, "getuid", lambda: 1000, raising=False)
        monkeypatch.setattr(bridge_call, "_process_exists", lambda pid: True)
        monkeypatch.setattr(bridge_call, "_posix_process_identity", lambda pid: (0, 7200.0))

        assert bridge_call.job_status(tmp_path, job_id)["status"] == "stalled"


def test_write_verb_effort_failure_is_reported_without_a_second_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prevent a failed write-capable child from being rerun against an already-edited worktree.

    An implement request at high effort gets a child rejection while lower tiers are available. Exactly one attempt runs
    and no effort substitution is recorded, since replaying a write task could double-apply edits already made.
    """
    attempts: list[list[str]] = []

    def _failing_child(command: list[str], workspace: Path, timeout: float) -> bridge_call.ChildOutcome:
        """Return a child rejection without permitting a write retry."""
        attempts.append(command)
        return _outcome(error="target rejected effort")

    monkeypatch.setattr(bridge_call, "_run_child", _failing_child)
    result = bridge_call.run_request(
        _request(tmp_path, verb="implement", effort="high", supported_efforts=("low", "medium", "high"))
    )

    assert len(attempts) == 1
    assert result["status"] == "blocked"
    assert result["effort_substituted"] is None


def test_workspace_state_degrades_when_git_is_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent a machine without Git from crashing write-capable dispatch instead of degrading.

    Starting git raises FileNotFoundError, as on a host without Git. The workspace snapshot degrades to an empty list
    instead of raising, so write-capable dispatch still works without Git.
    """

    def _missing_git(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        """Model a host where the Git executable cannot be started."""
        raise FileNotFoundError("git")

    monkeypatch.setattr(bridge_call.subprocess, "run", _missing_git)

    assert bridge_call._workspace_state(tmp_path) == []


def test_supervisor_preserves_a_cancellation_requested_during_its_final_record_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prevent a completed supervisor update from overwriting a concurrent cancellation request.

    A cancel is injected just before the supervisor writes its finished record. The job ends cancelled and its blocked
    result still carries the files, findings and remaining work the child reported.
    """
    paths = bridge_call.BridgePaths(tmp_path)
    paths.prepare()
    job_id = "66666666-6666-4666-8666-666666666666"
    record_path = paths.jobs / f"{job_id}.json"
    bridge_call._write_json(record_path, {"job_id": job_id, "status": "queued", "pid": None, "result": None})
    original_write_json = bridge_call._write_json
    cancellation_injected = False

    def _fake_run_request(request: bridge_call.Request, **kwargs: object) -> dict[str, object]:
        """Return a completed result for the supervisor race scenario."""
        return {
            "status": "complete",
            "verdict": "The child completed before the owner cancelled.",
            "findings": ["Adjusted the bounded module."],
            "files_touched": ["landed.py"],
            "remaining": ["Run the focused test."],
            "blockers": [],
            "model": "test-model",
            "effort": "medium",
            "effort_substituted": None,
            "cost": None,
            "tokens": {},
            "duration_seconds": 0.0,
            "depth": 0,
            "run_id": "run-fixed",
            "incident": None,
            "session_id": None,
            "transcript_path": ".temp/bridge/raw-fixed.txt",
            "verb": "implement",
            "direction": "claude_to_codex",
        }

    def _write_with_racing_cancellation(
        path: Path, value: dict[str, object], *, paths: bridge_call.BridgePaths | None = None
    ) -> None:
        """Inject cancellation immediately before the final record write."""
        nonlocal cancellation_injected
        if path == record_path and value.get("result") is not None and not cancellation_injected:
            cancellation_injected = True
            bridge_call.cancel_job(tmp_path, job_id)
        original_write_json(path, value, paths=paths)

    monkeypatch.setattr(bridge_call, "run_request", _fake_run_request)
    monkeypatch.setattr(bridge_call, "_write_json", _write_with_racing_cancellation)

    exit_code = bridge_call.main(
        [
            "implement",
            "--task",
            "Finish a bounded task.",
            "--job-id",
            job_id,
            "--supervisor",
            "--workspace",
            str(tmp_path),
        ]
    )

    assert exit_code == 0
    assert cancellation_injected is True
    assert bridge_call.job_status(tmp_path, job_id)["status"] == "cancelled"
    result = bridge_call.job_result(tmp_path, job_id)
    assert result["status"] == "blocked"
    assert result["blockers"] == ["cancelled by job owner"]
    assert result["files_touched"] == ["landed.py"]
    assert result["findings"] == ["Adjusted the bounded module."]
    assert result["remaining"] == ["Run the focused test."]


def test_diagnostics_detect_missing_help_flag_without_live_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prevent static setup from reporting success when a required CLI option disappeared.

    A fake help probe for Codex lists some options but not --output-schema, in static mode. The diagnosis is not ok,
    names the missing option, and sends no live request.
    """

    def _fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        """Return a diagnostic payload missing the required option."""
        return subprocess.CompletedProcess(command, 0, stdout="exec resume review --json", stderr="")

    monkeypatch.setattr(bridge_diagnose.subprocess, "run", _fake_run)
    result = bridge_diagnose.diagnose("codex", tmp_path, live=False)

    assert result["live"] is False
    assert result["ok"] is False
    assert "--output-schema" in result["findings"][0]["missing"]


def test_static_diagnosis_binds_itself_to_one_complete_installed_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prevent sync from accepting a doctor detached from its manifests, setup core, or schemas.

    Static diagnosis for Claude runs with a help probe echoing every baseline flag. It succeeds and reports the plugin
    name, the version from plugin.json, a complete payload and a 64-character fingerprint.
    """
    baseline = bridge_diagnose._load_baseline()
    monkeypatch.setattr(bridge_call, "_resolve_executable", lambda name: name)

    def _fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        """Return the baseline flags for the selected host executable."""
        target = "codex" if command[0] == "codex" else "claude"
        return subprocess.CompletedProcess(command, 0, stdout=" ".join(baseline[target]["required"]), stderr="")

    monkeypatch.setattr(bridge_diagnose.subprocess, "run", _fake_run)
    result = bridge_diagnose.diagnose("claude", tmp_path, live=False)
    expected_version = json.loads(
        (Path(__file__).resolve().parents[1] / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )["version"]

    assert result["ok"] is True
    assert result["payload"]["name"] == "bridge"
    assert result["payload"]["version"] == expected_version
    assert result["payload"]["complete"] is True
    assert len(result["payload"]["fingerprint"]) == 64


def test_incomplete_payload_is_reported_structurally_before_the_baseline_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prevent a truncated install from dying on the baseline read instead of naming missing payload files.

    An install missing rules/cli-baseline.json is exactly the incomplete-payload case the payload block diagnoses;
    loading the baseline first turned it into a bare error object with no missing list, and no help probe should run at
    all.
    """
    monkeypatch.setattr(
        bridge_diagnose,
        "_regular_payload_file",
        lambda path: path.name != "cli-baseline.json",
    )
    monkeypatch.setattr(
        bridge_diagnose,
        "_load_baseline",
        lambda: pytest.fail("baseline must not load while the payload is incomplete"),
    )
    monkeypatch.setattr(
        bridge_diagnose.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("no help probe may run for an incomplete payload"),
    )

    result = bridge_diagnose.diagnose("claude", tmp_path, live=False)

    assert result["ok"] is False
    assert result["findings"] == []
    assert result["payload"]["complete"] is False
    assert result["payload"]["missing"] == ["rules/cli-baseline.json"]


def test_diagnostics_report_a_malformed_baseline_without_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Prevent a corrupted shipped baseline from crashing setup instead of reporting a finding.

    The payload identity is forced complete while the baseline JSON parses to a structure missing required keys. The
    diagnostic CLI exits 2 with a JSON error naming the baseline, not a Python traceback.
    """
    with monkeypatch.context() as patched:
        # The payload gate runs before the baseline loads, so it must pass here
        # for the corrupted baseline content to be the failure under test.
        patched.setattr(
            bridge_diagnose,
            "_installed_payload_identity",
            lambda: {"name": "bridge", "version": "0.0.0", "complete": True, "fingerprint": "0" * 64, "missing": []},
        )
        patched.setattr(
            bridge_diagnose.json,
            "loads",
            lambda text: {"codex": {"required": []}, "claude": {"required": ["--print"]}},
        )
        exit_code = bridge_diagnose.main(["--direction", "codex", "--workspace", str(tmp_path)])
    report = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert report["ok"] is False
    assert "baseline" in report["error"]


def test_claude_diagnostic_baseline_covers_every_runtime_argv_flag(tmp_path: Path) -> None:
    """Prevent static preflight from missing a Claude switch the bridge dispatches at runtime.

    Claude argv is built for implement, advise and review. Every long option it contains must be listed in the shipped
    CLI baseline, so static preflight would notice a host that dropped one.
    """
    schema_path = tmp_path / "core-schema.json"
    schema_path.write_text("{}", encoding="utf-8")
    baseline = json.loads(
        (Path(__file__).resolve().parents[1] / "rules" / "cli-baseline.json").read_text(encoding="utf-8")
    )
    runtime_flags = {
        item
        for verb in ("implement", "advise", "review")
        for item in bridge_call.build_claude_argv(_request(tmp_path, verb=verb), schema_path)
        if item.startswith("--")
    }

    assert runtime_flags <= set(baseline["claude"]["required"])


def test_codex_diagnostic_baseline_covers_every_runtime_argv_flag(tmp_path: Path) -> None:
    """Prevent static preflight from missing a Codex switch the bridge dispatches at runtime.

    Codex argv is built for advise, review, implement and a resumed implement. Every long option it contains must be
    listed in the shipped CLI baseline, so static preflight would notice a host that dropped one.
    """
    schema_path = tmp_path / "core-schema.json"
    schema_path.write_text("{}", encoding="utf-8")
    baseline = json.loads(
        (Path(__file__).resolve().parents[1] / "rules" / "cli-baseline.json").read_text(encoding="utf-8")
    )
    fresh_commands = [
        bridge_call.build_codex_argv(_request(tmp_path, verb=verb), schema_path)
        for verb in ("advise", "review", "implement")
    ]
    resumed_command = bridge_call.build_codex_argv(
        _request(tmp_path, verb="implement", session_id="session-fixed", origin_workspace=tmp_path), schema_path
    )
    runtime_flags = {
        item for command in [*fresh_commands, resumed_command] for item in command if item.startswith("--")
    }

    assert runtime_flags <= set(baseline["codex"]["required"])


@pytest.mark.parametrize(
    ("verb", "expected_sandbox"),
    [
        pytest.param("implement", "workspace-write", id="implement-write"),
        pytest.param("advise", "read-only", id="advise-read-only"),
        pytest.param("review", "read-only", id="review-read-only"),
    ],
)
def test_cli_implement_is_the_only_write_capable_verb(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    verb: str,
    expected_sandbox: str,
) -> None:
    """Prevent implement from becoming read-only or a read-only verb from gaining write access.

    The CLI runs implement, advise and review with a stubbed child, then an unknown verb. Only implement launches with
    the workspace-write sandbox, the others read-only, and the retired delegate verb exits with a usage error.
    """
    commands: dict[str, list[str]] = {}

    def _fake_child(command: list[str], *args: object) -> bridge_call.ChildOutcome:
        """Return a completed child result for each CLI verb."""
        commands[verb] = command
        return _outcome(core=_core())

    monkeypatch.setattr(bridge_call, "_run_child", _fake_child)

    exit_code = bridge_call.main([verb, "--task", "Make the bounded change.", "--workspace", str(tmp_path)])
    envelope = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert envelope["status"] == "complete"
    assert envelope["verb"] == verb

    sandbox = commands[verb][commands[verb].index("-s") + 1]
    assert sandbox == expected_sandbox
    with pytest.raises(SystemExit):
        bridge_call.main(["delegate", "--task", "Rejected.", "--workspace", str(tmp_path)])


def _capturing_run_request(monkeypatch: pytest.MonkeyPatch) -> list[bridge_call.Request]:
    """Patch run_request to record every dispatched request instead of calling out to a peer."""
    captured: list[bridge_call.Request] = []

    def _fake_run_request(request: bridge_call.Request, *, host: str) -> dict[str, object]:
        """Return a compact success while recording the selected host."""
        captured.append(request)
        assert host == "claude"
        return {"status": "complete"}

    monkeypatch.setattr(bridge_mcp, "run_request", _fake_run_request)
    return captured


def test_implement_tool_is_listed_and_dispatches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The reverse bridge keeps exposing the implement tool and routes calls through to it.

    A prior regression could drop bridge_implement from the tool list while retiring bridge_delegate, which would remove
    the only write-capable path from Codex to Claude.
    """
    (tmp_path / ".git").mkdir()  # project marker: implement refuses a folder without one
    captured = _capturing_run_request(monkeypatch)
    listed = {tool["name"] for tool in bridge_mcp.tool_definitions()}

    response = bridge_mcp.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "bridge_implement", "arguments": {"task": "Make the bounded change."}},
        },
        trusted_workspace=tmp_path,
    )

    assert response["result"]["isError"] is False
    assert "bridge_implement" in listed
    assert [request.verb for request in captured] == ["implement"]


def test_delegate_tool_is_retired_and_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The retired bridge_delegate tool name is refused rather than resurrected as a second write path.

    A prior rename could leave a delegate-named tool reachable again; this pins that name permanently rejected before it
    ever reaches provider dispatch.
    """
    captured = _capturing_run_request(monkeypatch)
    listed = {tool["name"] for tool in bridge_mcp.tool_definitions()}

    retired = bridge_mcp.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "bridge_delegate", "arguments": {"task": "Make the bounded change."}},
        },
        trusted_workspace=tmp_path,
    )

    assert "error" in retired
    assert "bridge_delegate" not in listed
    assert captured == []


def test_mcp_handshake_tools_call_and_recursion_guard_preserve_run_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prevent MCP transport drift, unknown fields, and a second cross-host hop.

    An MCP client initializes, lists tools, then calls advise with depth 1 and a run id. The tool inventory and schemas
    match the contract, and the call is refused for recursion depth with the caller's run id echoed back.
    """
    initialized = bridge_mcp.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    tools = bridge_mcp.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert initialized["result"]["capabilities"] == {"tools": {}}
    listed = {item["name"]: item for item in tools["result"]["tools"]}
    assert set(listed) == {
        "bridge_implement",
        "bridge_advise",
        "bridge_review",
        "bridge_status",
        "bridge_bind_workspace",
    }
    assert "background" not in listed["bridge_implement"]["inputSchema"]["properties"]
    assert "workspace" not in listed["bridge_advise"]["inputSchema"]["properties"]

    result = bridge_mcp.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "bridge_advise",
                "arguments": {"task": "No peer call.", "depth": 1, "run_id": "tree-fixed"},
            },
        },
        trusted_workspace=tmp_path,
    )
    envelope = json.loads(result["result"]["content"][0]["text"])
    assert envelope["status"] == "refused"
    assert envelope["blockers"] == ["recursion-depth"]
    assert envelope["run_id"] == "tree-fixed"


@pytest.mark.parametrize("unsupported_kind", ["workspace", "background", "session"])
def test_mcp_rejects_model_supplied_workspace_and_reverse_session(tmp_path: Path, unsupported_kind: str) -> None:
    """Prevent a write-capable MCP call from widening host authority or faking resume.

    A bridge_implement call adds a workspace, a background flag or a session id on top of its task. Each is rejected
    with an invalid-params error, since the host fixes the workspace and reverse calls cannot resume sessions.
    """
    unsupported = {
        "workspace": {"workspace": str(tmp_path.parent)},
        "background": {"background": True},
        "session": {"session_id": "session-fixed"},
    }[unsupported_kind]
    result = bridge_mcp.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "bridge_implement", "arguments": {"task": "No write.", **unsupported}},
        },
        trusted_workspace=tmp_path,
    )

    assert result["error"]["code"] == -32602
    assert "unsupported tool arguments" in result["error"]["message"]


@pytest.mark.parametrize(
    ("request_id", "timeout"),
    [
        pytest.param(1, float("nan"), id="nan"),
        pytest.param(2, float("inf"), id="positive-infinity"),
        pytest.param(3, float("-inf"), id="negative-infinity"),
    ],
)
def test_mcp_rejects_nonfinite_timeout_before_provider_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request_id: int, timeout: float
) -> None:
    """Prevent non-finite MCP deadlines from bypassing the provider dispatch boundary.

    bridge_advise receives a NaN or infinite timeout while run_request fails the test if called. The response is an
    invalid-params error, so the deadline guard cannot be bypassed through MCP.
    """
    monkeypatch.setattr(
        bridge_mcp, "run_request", lambda *args, **kwargs: pytest.fail("non-finite timeout dispatched a provider")
    )

    response = bridge_mcp.handle_message(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {
                "name": "bridge_advise",
                "arguments": {"task": "Reject this malformed deadline.", "timeout_seconds": timeout},
            },
        },
        trusted_workspace=tmp_path,
    )

    assert response["error"]["code"] == -32602


@pytest.mark.parametrize("task", [pytest.param("a" * 70000, id="ascii"), pytest.param("🙂" * 20000, id="multibyte")])
def test_mcp_rejects_an_encoded_task_over_the_transport_budget_before_artifacts_or_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, task: str
) -> None:
    """Prevent an unlaunchable argv task from creating Bridge state or reaching a peer.

    A 70,000-character ASCII task or a 20,000-emoji task exceeds the encoded budget. The exact invalid-params response
    returns, no peer is called and no .temp/bridge directory is created.
    """
    monkeypatch.setattr(bridge_mcp, "run_request", lambda *args, **kwargs: pytest.fail("oversize task dispatched"))

    response = bridge_mcp.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "bridge_advise", "arguments": {"task": task}},
        },
        trusted_workspace=tmp_path,
    )

    assert response == {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32602, "message": "task exceeds Bridge's encoded transport budget"},
    }
    assert not (tmp_path / ".temp" / "bridge").exists()


@pytest.mark.parametrize(
    "task",
    [pytest.param("a" * (16 * 1024), id="ascii-boundary"), pytest.param("🙂" * (4 * 1024), id="multibyte-boundary")],
)
def test_transport_budget_accepts_all_host_safe_utf8_boundaries(tmp_path: Path, task: str) -> None:
    """Keep accepted task text below the portable byte ceiling for both single- and multibyte input.

    A task of exactly 16 KiB ASCII and one of 4 Ki four-byte characters, both at the byte ceiling, validate without
    error. Accepted tasks therefore fit every host's argv limit whatever their encoding width.
    """
    request = _request(tmp_path, task=task)

    bridge_call.validate_request_transport_budget(request)


def test_windows_command_measurement_uses_list2cmdline_utf16_units() -> None:
    """Prevent a Unicode task that is short in Python characters from exceeding Windows command transport.

    A command with a quote-heavy emoji task is measured the way CreateProcess sees it. The UTF-16 count exceeds the
    Windows limit although the Python string is short, so quoting and surrogate pairs are counted.
    """
    command = ["peer", 'quote " and emoji 🙂' * 8_000]

    assert bridge_call._windows_command_utf16_units(command) > bridge_call.MAX_WINDOWS_COMMAND_UTF16_UNITS


@pytest.mark.parametrize(
    ("executable", "expectation"),
    [
        pytest.param(
            "C:\\\\Bridge\\\\codex.cmd",
            lambda: pytest.raises(ValueError, match="encoded transport budget"),
            id="batch-shim",
        ),
        pytest.param("C:\\\\Bridge\\\\codex.exe", contextlib.nullcontext, id="native-executable"),
    ],
)
def test_windows_batch_shim_uses_a_stricter_resolved_command_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, executable: str, expectation: Callable[[], Any]
) -> None:
    """Prevent a safe CreateProcess argv from exceeding Cmd.exe through an npm batch shim.

    A 7,800-character task is validated with the executable resolved to a .cmd shim or a native .exe. The shim is
    rejected for exceeding the encoded budget (Cmd.exe limit) while the native executable accepts it.
    """
    monkeypatch.setattr(bridge_call, "_resolve_executable", lambda name: executable)
    request = _request(tmp_path, task="a" * 7_800)

    with expectation():
        bridge_call.validate_request_transport_budget(request)


@pytest.mark.parametrize(
    "message",
    [
        pytest.param({"jsonrpc": "1.0", "id": 1, "method": "tools/list"}, id="wrong-version"),
        pytest.param({"jsonrpc": "2.0", "id": 2}, id="missing-method"),
        pytest.param({"jsonrpc": "2.0", "id": 3, "method": ["tools/list"]}, id="method-not-string"),
    ],
)
def test_mcp_rejects_invalid_request_shapes_with_the_standard_error_code(message: dict[str, object]) -> None:
    """Prevent malformed JSON-RPC requests from being treated as unknown methods.

    Requests with a wrong jsonrpc version, no method, or a non-string method are handled. Each returns the standard
    invalid-request code -32600 instead of falling through to method-not-found.
    """
    response = bridge_mcp.handle_message(message)

    assert response["error"]["code"] == -32600


def test_mcp_notifications_never_reply_or_execute_a_provider_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prevent notification-shaped tool calls from consuming provider capacity without a response channel.

    A tools/call arrives without an id while run_request fails the test if called. No response is produced and nothing
    is dispatched, since a notification has no reply channel to report cost or results.
    """
    monkeypatch.setattr(
        bridge_mcp, "run_request", lambda *args, **kwargs: pytest.fail("notification dispatched a provider")
    )

    response = bridge_mcp.handle_message(
        {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": "bridge_advise", "arguments": {"task": "Do not run this notification."}},
        },
        trusted_workspace=tmp_path,
    )

    assert response is None


def test_unknown_mcp_notification_does_not_receive_a_method_error() -> None:
    """Prevent an unknown notification from producing an invalid response without an identifier.

    A notification with an unknown method and no id is handled. The server returns nothing, because answering would emit
    a response without an identifier.
    """
    response = bridge_mcp.handle_message({"jsonrpc": "2.0", "method": "notifications/unknown"})

    assert response is None


def test_id_bearing_initialized_receives_an_acknowledgement() -> None:
    """Prevent a malformed id-bearing initialized message from leaving its request unanswered.

    A notifications/initialized message that mistakenly carries an id is handled. It gets an empty-result
    acknowledgement with that id, so a client waiting on it is not left hanging.
    """
    response = bridge_mcp.handle_message({"jsonrpc": "2.0", "id": 7, "method": "notifications/initialized"})

    assert response == {"jsonrpc": "2.0", "id": 7, "result": {}}


def test_malformed_notification_never_receives_an_error_response() -> None:
    """Prevent validation failures on id-less messages from violating the notification contract.

    Three id-less messages are malformed: wrong version, empty method, and non-object params. All return no response,
    because the notification contract forbids error replies.
    """
    assert bridge_mcp.handle_message({"jsonrpc": "1.0", "method": "notifications/initialized"}) is None
    assert bridge_mcp.handle_message({"jsonrpc": "2.0", "method": ""}) is None
    assert bridge_mcp.handle_message({"jsonrpc": "2.0", "method": "tools/call", "params": 5}) is None


def test_mcp_refuses_write_verbs_from_a_home_or_root_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent a host that launches the server from $HOME from rooting acceptEdits runs there.

    The trusted workspace is the user's home directory. bridge_implement is refused with an invalid-params error naming
    the workspace, while bridge_advise still runs, so write access is never rooted in a broad directory.
    """
    dispatched: list[str] = []

    def _fake_run_request(request: bridge_call.Request, **kwargs: object) -> dict[str, object]:
        """Return a completed advisory result for the trusted-workspace check."""
        dispatched.append(request.verb)
        return {"status": "complete"}

    monkeypatch.setattr(bridge_mcp, "run_request", _fake_run_request)

    response = bridge_mcp.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "bridge_implement", "arguments": {"task": "Edit something."}},
        },
        trusted_workspace=Path.home(),
    )
    advisory = bridge_mcp.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "bridge_advise", "arguments": {"task": "Answer without editing."}},
        },
        trusted_workspace=Path.home(),
    )

    assert response is not None
    assert response["error"]["code"] == -32602
    assert "workspace" in response["error"]["message"]
    assert advisory is not None
    assert "error" not in advisory
    assert dispatched == ["advise"]


@pytest.mark.parametrize(
    "invalid_arguments",
    [
        pytest.param({"depth": True}, id="boolean-depth"),
        pytest.param({"timeout_seconds": True}, id="boolean-timeout"),
        pytest.param({"timeout_seconds": 360.1}, id="timeout-over-host-safe-maximum"),
        pytest.param({"supported_efforts": []}, id="empty-supported-efforts"),
    ],
)
def test_mcp_rejects_values_that_disagree_with_its_json_schema(
    tmp_path: Path, invalid_arguments: dict[str, object]
) -> None:
    """Prevent Python's bool-as-int rule or empty capability data from bypassing the MCP contract.

    bridge_advise gets boolean depth or timeout, a timeout above the host-safe maximum, or an empty supported_efforts
    list. Each is an invalid-params error, matching the published schema.
    """
    result = bridge_mcp.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "bridge_advise", "arguments": {"task": "Keep schema parity.", **invalid_arguments}},
        },
        trusted_workspace=tmp_path,
    )

    assert result["error"]["code"] == -32602


def test_mcp_uses_trusted_workspace_instead_of_model_argument(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent a reverse bridge request from escaping the MCP launch workspace.

    bridge_advise is called without a workspace argument. The dispatched request is rooted at the resolved trusted
    workspace, foreground and without a session, so the model cannot pick its own directory.
    """
    captured: list[bridge_call.Request] = []

    def _fake_run_request(request: bridge_call.Request, *, host: str) -> dict[str, object]:
        """Return a result tied to the request workspace and run identifier."""
        captured.append(request)
        assert host == "claude"
        return {"status": "complete", "run_id": request.run_id}

    monkeypatch.setattr(bridge_mcp, "run_request", _fake_run_request)
    result = bridge_mcp.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "bridge_advise", "arguments": {"task": "Stay in the launch workspace."}},
        },
        trusted_workspace=tmp_path,
    )

    assert result["result"]["isError"] is False
    assert captured[0].workspace == tmp_path.resolve()
    assert captured[0].background is False
    assert captured[0].session_id is None


def test_reverse_mcp_uses_a_claude_compatible_schema_and_keeps_peer_details_in_the_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prevent verbose peer detail from escaping the compact MCP envelope or a rejected schema from reaching Claude.

    A Claude child is stubbed to return a core with a verbose detail. The schema passed to Claude is the canonical
    envelope schema minus its $schema key, the public envelope has no details, and the detail appears only in the
    transcript.
    """
    commands: list[list[str]] = []
    peer_detail = "The provider emitted this verbose diagnostic only for the saved transcript."

    def _fake_child(command: list[str], workspace: Path, timeout: float) -> bridge_call.ChildOutcome:
        """Return Claude detail that should remain in the transcript."""
        commands.append(command)
        return _claude_outcome(_core(details=[peer_detail]))

    monkeypatch.setattr(bridge_call, "_run_child", _fake_child)
    response = bridge_mcp.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "bridge_advise", "arguments": {"task": "Return a bounded answer."}},
        },
        trusted_workspace=tmp_path,
    )

    envelope = json.loads(response["result"]["content"][0]["text"])
    schema = json.loads(commands[0][commands[0].index("--json-schema") + 1])
    canonical_schema = json.loads(
        (Path(__file__).resolve().parents[1] / "schemas" / "envelope.schema.json").read_text(encoding="utf-8")
    )
    transcript = (tmp_path / envelope["transcript_path"]).read_text(encoding="utf-8")

    assert "$schema" not in schema
    canonical_schema.pop("$schema")
    assert schema == canonical_schema
    assert set(schema["required"]) == {
        "status",
        "verdict",
        "findings",
        "files_touched",
        "remaining",
        "blockers",
        "details",
    }
    assert envelope["status"] == "complete"
    assert "details" not in envelope
    assert peer_detail in transcript
    assert peer_detail not in response["result"]["content"][0]["text"]


def test_mcp_implement_runs_real_supervisor_with_claude_write_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prevent implement from using read-only Claude flags or bypassing the real bridge envelope path.

    bridge_implement runs against a stubbed Claude child that writes a marker file. The child command uses acceptEdits
    without tool-disabling flags, and the envelope reports implement, codex_to_claude and the touched file, with details
    only in the transcript.
    """
    (tmp_path / ".git").mkdir()  # project marker: implement refuses a folder without one
    commands: list[list[str]] = []
    marker = tmp_path / "implemented-marker.txt"
    peer_detail = "The fake provider's private implementation detail belongs only in the transcript."

    def _fake_child(command: list[str], workspace: Path, timeout: float) -> bridge_call.ChildOutcome:
        """Write the marker and return Claude's bounded implementation result."""
        commands.append(command)
        assert workspace == tmp_path.resolve()
        marker.write_text("implemented by fake Claude child\n", encoding="utf-8")
        return _claude_outcome(_core(files_touched=[marker.name], details=[peer_detail]))

    monkeypatch.setattr(bridge_call, "_run_child", _fake_child)
    response = bridge_mcp.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 18,
            "method": "tools/call",
            "params": {"name": "bridge_implement", "arguments": {"task": "Create a trusted marker."}},
        },
        trusted_workspace=tmp_path,
    )

    envelope = json.loads(response["result"]["content"][0]["text"])
    transcript = (tmp_path / envelope["transcript_path"]).read_text(encoding="utf-8")
    command = commands[0]
    assert response["result"]["isError"] is False
    assert marker.read_text(encoding="utf-8") == "implemented by fake Claude child\n"
    assert command[command.index("--permission-mode") + 1] == "acceptEdits"
    assert "--disallowed-tools" not in command
    assert "Edit" not in command
    assert "Write" not in command
    assert envelope["status"] == "complete"
    assert envelope["verb"] == "implement"
    assert envelope["direction"] == "codex_to_claude"
    assert envelope["files_touched"] == [marker.name]
    assert "details" not in envelope
    assert peer_detail in transcript
    assert peer_detail not in response["result"]["content"][0]["text"]


@pytest.mark.parametrize(
    "invalid_core",
    [
        pytest.param(_core(details=[1]), id="non-string-detail"),
        pytest.param(_core(details="detail"), id="non-array-details"),
        pytest.param(_core(verdict=""), id="empty-verdict"),
        pytest.param(_core(verdict=1), id="non-string-verdict"),
        pytest.param(_core(findings=[1]), id="non-string-finding"),
        pytest.param(_core(remaining=None), id="non-array-remaining"),
    ],
)
def test_peer_result_types_reject_malformed_model_output(invalid_core: dict[str, object]) -> None:
    """Retain required field and type checks after removing presentation bounds.

    Model cores with non-string details, a non-array details field, an empty or non-string verdict, non-string findings
    or non-array remaining are validated. Each raises a field-specific ValueError, so malformed peer output never
    becomes an envelope.
    """
    with pytest.raises(ValueError, match=r"model result (details|findings|verdict|remaining) must"):
        bridge_call.validate_model_core(invalid_core)


def test_mcp_stdio_returns_one_response_per_request_without_peer_cli(tmp_path: Path) -> None:
    """Prevent stdio framing regressions that corrupt a Codex MCP client's request stream.

    The real server process receives initialize, a notification and tools/list on stdin. It answers the two requests in
    order, stays silent for the notification, and lists the expected tool inventory without any peer CLI.
    """
    messages = "\n".join(
        json.dumps(message)
        for message in (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
    )

    responses = _run_mcp_stdio(tmp_path, messages)

    assert [response["id"] for response in responses] == [1, 2]
    assert {item["name"] for item in responses[1]["result"]["tools"]} == set(bridge_mcp.EXPECTED_TOOL_INVENTORY)


def test_mcp_stdio_malformed_tool_name_returns_invalid_params_and_keeps_serving(tmp_path: Path) -> None:
    """Prevent a malformed tool name from terminating the server before a later valid request.

    A tools/call whose name is a list instead of a string is answered with invalid params, and the following tools/list
    on the same stdio session still gets its full answer.
    """
    messages = "\n".join(
        json.dumps(message)
        for message in (
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": [], "arguments": {}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
    )

    responses = _run_mcp_stdio(tmp_path, messages)

    assert [response["id"] for response in responses] == [1, 2]
    assert responses[0]["error"]["code"] == -32602
    assert {tool["name"] for tool in responses[1]["result"]["tools"]} == set(bridge_mcp.EXPECTED_TOOL_INVENTORY)


def test_mcp_stdio_raw_nonfinite_timeout_returns_invalid_params_without_a_provider(tmp_path: Path) -> None:
    """Prevent a raw non-finite JSON timeout from reaching a provider child before a later request.

    The wire text carries a bare NaN timeout and PATH points at an empty directory, so any provider launch would fail.
    The server answers the bad line with a parse error (null id) and still serves the next request.
    """
    messages = "\n".join(
        (
            '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"bridge_advise","arguments":{"task":"Reject this malformed deadline.","timeout_seconds":NaN}}}',
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        )
    )
    environment = {**os.environ, "PATH": str(tmp_path / "no-provider-bin")}

    responses = _run_mcp_stdio(tmp_path, messages, env=environment)

    assert [response["id"] for response in responses] == [None, 2]
    assert responses[0]["error"]["code"] == -32700
    assert {tool["name"] for tool in responses[1]["result"]["tools"]} == set(bridge_mcp.EXPECTED_TOOL_INVENTORY)


def test_cli_invalid_input_has_one_json_error_and_nonzero_exit(tmp_path: Path) -> None:
    """Prevent skills from accepting a failed request as a successful JSON result.

    The real CLI process receives an unknown effort level. It exits non-zero and prints exactly one JSON line with
    status error, which is what skills parse.
    """
    result = subprocess.run(
        [
            sys.executable,
            str(BIN_ROOT / "bridge_call.py"),
            "advise",
            "--task",
            "Report completion.",
            "--effort",
            "not-a-level",
            "--workspace",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    lines = result.stdout.splitlines()
    assert result.returncode != 0
    assert len(lines) == 1
    assert json.loads(lines[0])["status"] == "error"


@pytest.mark.parametrize(
    ("extra_args", "expected_fragment"),
    [
        pytest.param(["--task", "inline", "--task-file", "{task_file}"], "not both", id="both-sources"),
        pytest.param([], "one of --task or --task-file", id="neither-source"),
    ],
)
def test_cli_task_source_conflicts_return_a_json_error(
    tmp_path: Path, extra_args: list[str], expected_fragment: str
) -> None:
    """Ambiguous or missing task sources fail as parseable envelopes, not argparse exits.

    Every caller — skill, MCP tool, lifecycle supervisor — reads exactly one JSON object from stdout. An argparse usage
    error would print to stderr and exit 2 with nothing to parse, so the two task flags are validated in request
    construction instead.
    """
    task_file = tmp_path / "task.md"
    task_file.write_text("Summarize the change.", encoding="utf-8")
    task_args = [arg.format(task_file=task_file) for arg in extra_args]
    command = [sys.executable, str(BIN_ROOT / "bridge_call.py"), "advise", "--workspace", str(tmp_path), *task_args]

    result = subprocess.run(command, capture_output=True, text=True, check=False)

    lines = result.stdout.splitlines()
    assert result.returncode != 0
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["status"] == "error"
    assert expected_fragment in payload["error"]


def test_cli_reads_a_task_containing_shell_metacharacters_from_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task text that would be hostile on a command line survives verbatim through a file.

    Consumer skills forward text they did not author — review comments, issue bodies — and a brief carrying backticks,
    ``$(...)`` or embedded quotes is ordinary, not an attack. Reading it from a file keeps correctness independent of
    the caller's quoting.
    """
    hostile = 'Fix `rm -rf /` in "quoted" text; $(whoami) && echo done\n'
    task_file = tmp_path / "brief.md"
    task_file.write_text(hostile, encoding="utf-8", newline="\n")
    namespace = argparse.Namespace(
        verb="advise",
        task=None,
        task_file=str(task_file),
        model=None,
        effort=None,
        timeout_seconds=None,
        background=False,
        session_id=None,
        depth=0,
        network="none",
        run_id=None,
        workspace=str(tmp_path),
    )

    request = bridge_call._request_from_args(namespace)

    assert request.task == hostile


def test_cli_rejects_negative_depth_before_dispatch(tmp_path: Path) -> None:
    """Prevent a caller from bypassing recursion accounting with a negative depth.

    The real CLI process is invoked with --depth -1. It exits non-zero and prints exactly one JSON error naming the non-
    negative requirement, before any peer could start.
    """
    result = subprocess.run(
        [
            sys.executable,
            str(BIN_ROOT / "bridge_call.py"),
            "advise",
            "--task",
            "Report completion.",
            "--depth",
            "-1",
            "--workspace",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    lines = result.stdout.splitlines()
    assert result.returncode != 0
    assert len(lines) == 1
    assert json.loads(lines[0]) == {"status": "error", "error": "--depth must be a non-negative integer"}


def test_trusted_inherited_depth_cannot_be_lowered_by_caller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent a caller-provided zero from bypassing an inherited recursion refusal.

    The environment already records depth 1 while the request passes depth 0 and any child launch fails the test. The
    run is refused for recursion depth and reports the inherited depth, not the caller's claim.
    """
    monkeypatch.setenv(bridge_call.DEPTH_ENVIRONMENT_VARIABLE, "1")
    monkeypatch.setattr(bridge_call, "_run_child", lambda *args: pytest.fail("refused call dispatched a child"))

    result = bridge_call.run_request(_request(tmp_path, depth=0))

    assert result["status"] == "refused"
    assert result["depth"] == 1
    assert result["blockers"] == ["recursion-depth"]


def test_child_receives_incremented_trusted_depth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_popen_calls: list[tuple[list[str], dict[str, Any]]]
) -> None:
    """Prevent a peer process from inheriting its parent's depth and permitting another hop.

    The parent runs at depth 2 with an unrelated secret in its environment. The launched child gets depth 3 on top of
    the filtered allowlist environment, so the secret never propagates and the recursion guard keeps counting.
    """
    monkeypatch.setenv(bridge_call.DEPTH_ENVIRONMENT_VARIABLE, "2")
    monkeypatch.setenv("BRIDGE_TEST_UNRELATED_SECRET", "must-not-propagate")

    bridge_call._run_child([sys.executable], tmp_path, timeout=1.0)

    environments = [kwargs["env"] for _, kwargs in fake_popen_calls]
    assert environments == [{**bridge_call.build_child_environment(), bridge_call.DEPTH_ENVIRONMENT_VARIABLE: "3"}]
    assert "BRIDGE_TEST_UNRELATED_SECRET" not in environments[0]


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="requires POSIX process-group termination capability")
@pytest.mark.parametrize("startup_delay", [0.0, 0.15])
def test_timeout_force_terminates_a_descendant_after_its_term_ignoring_leader_exits(
    tmp_path: Path,
    startup_delay: float,
    monkeypatch: pytest.MonkeyPatch,
    launched_groups: list[subprocess.Popen[bytes]],
) -> None:
    """Prevent a timeout from returning while a TERM-ignoring descendant retains its inherited pipes.

    A real leader spawns a descendant that ignores SIGTERM and keeps the inherited pipes, with the descendant ready
    immediately or after a short delay. A 0.05 second timeout must still end with the leader killed by SIGTERM and the
    descendant gone.
    """
    child_pid_path = tmp_path / "timeout-child.pid"
    # Only the descendant can acknowledge that its TERM-ignore handler is installed.
    child = (
        "import os, signal, time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"path = Path({str(child_pid_path)!r})\n"
        "pending = path.with_suffix('.tmp')\n"
        "pending.write_text(str(os.getpid()), encoding='utf-8')\n"
        "pending.replace(path)\n"
        "time.sleep(60)\n"
    )
    parent = (
        "import subprocess, sys, time\n"
        f"time.sleep({startup_delay})\n"
        f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"
        "time.sleep(60)\n"
    )
    real_popen = subprocess.Popen

    def start_ready_process(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        """Launch a real process but keep fixture startup outside the timeout under test."""
        process = real_popen(*args, **kwargs)
        launched_groups.append(process)
        _wait_for(lambda: child_pid_path.is_file() or process.poll() is not None, 10.0, 0.01)
        assert child_pid_path.is_file(), "descendant did not install its TERM-ignore handler within 10 seconds"
        return process

    monkeypatch.setattr(bridge_call.subprocess, "Popen", start_ready_process)

    outcome = bridge_call._run_child([sys.executable, "-c", parent], tmp_path, timeout=0.05)
    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    _wait_for(lambda: not _process_exists(child_pid), 3.0)

    assert outcome.timed_out is True
    assert outcome.returncode == -bridge_call.signal.SIGTERM
    assert outcome.error is None
    assert not _process_exists(child_pid)


def test_simulated_windows_child_launch_uses_a_new_process_group_without_posix_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_popen_calls: list[tuple[list[str], dict[str, Any]]]
) -> None:
    """Prevent the portable Windows branch from passing POSIX-only process-launch options.

    With os.name simulated as nt and a fake creation-flag constant, the launch must request a new process group through
    creationflags and must not pass start_new_session, which Windows does not support.
    """
    monkeypatch.setattr(bridge_call.os, "name", "nt")
    monkeypatch.setattr(bridge_call.subprocess, "CREATE_NEW_PROCESS_GROUP", 2468, raising=False)

    outcome = bridge_call._run_child([sys.executable], tmp_path, timeout=1.0)

    launch = fake_popen_calls[0][1]
    assert outcome.returncode == 0
    assert launch["creationflags"] == 2468
    assert "start_new_session" not in launch


@pytest.mark.skipif(
    (os.name == "nt" and (shutil.which("taskkill") is None or shutil.which("tasklist") is None))
    or (os.name != "nt" and not hasattr(os, "killpg")),
    reason="requires native process-tree termination and liveness capabilities",
)
def test_cancelling_detached_supervisor_terminates_its_real_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pid_files: list[Path], started_jobs: list[str]
) -> None:
    """Prevent cancellation from leaving a detached peer child alive after its supervisor dies.

    A background implement job runs a fake codex whose own child ignores SIGTERM. After cancel_job the supervisor must
    tear down the whole tree, and the registered job and PID fixtures reclaim anything left on failure.
    """
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    child_pid_path = tmp_path / "child.pid"
    pid_files.append(child_pid_path)
    peer_script = fake_bin / "codex_peer.py"
    # Path baked into the peer script rather than read from the environment: the bridge
    # filters the child environment to an allowlist, so a test-only variable never arrives.
    peer_script.write_text(
        "from pathlib import Path\n"
        "import subprocess\n"
        "import sys\n"
        "import time\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'])\n"
        f"pid_path = Path({str(child_pid_path)!r})\n"
        "pid_tmp_path = pid_path.with_suffix('.pid.tmp')\n"
        "pid_tmp_path.write_text(str(child.pid), encoding='utf-8')\n"
        "pid_tmp_path.replace(pid_path)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
        newline="\n",
    )
    _write_fake_codex_launcher(fake_bin, peer_script)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    started = bridge_call.start_background(_request(tmp_path, verb="implement", background=True, timeout_seconds=30.0))
    started_jobs.append(started["job_id"])
    assert _wait_for(child_pid_path.is_file, 10.0, 0.01), "fake peer did not start its child within 10 seconds"
    child_pid = int(child_pid_path.read_text(encoding="utf-8"))

    bridge_call.cancel_job(tmp_path, started["job_id"])

    assert _wait_for(lambda: not _process_exists(child_pid), 3.0), (
        "detached peer child survived supervisor cancellation"
    )


@pytest.mark.parametrize("wait_result", ["exited", "timeout", "os-error"])
def test_posix_termination_falls_back_when_killpg_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    wait_result: str,
) -> None:
    """Reap direct-kill fallbacks and report bounded wait failures without inventing an exit code.

    On a simulated POSIX host without killpg, termination kills the process directly and waits with a bound. The wait
    exits, times out or raises OSError, and none of them invents an exit code or escapes as an exception.
    """
    killed: list[bool] = []
    waited: list[float] = []

    class _Process:
        """Track direct-kill fallback calls and their bounded wait result."""

        pid = 42
        returncode = None

        def wait(self, timeout: float) -> int:
            """Wait for the bounded direct-kill fallback and report its result."""
            waited.append(timeout)
            assert killed == [True]
            if wait_result == "timeout":
                raise subprocess.TimeoutExpired("fake", timeout)
            if wait_result == "os-error":
                raise OSError("wait unavailable")
            self.returncode = 1
            return self.returncode

        def kill(self) -> None:
            """Record the fallback kill request without touching a real process."""
            killed.append(True)

    monkeypatch.setattr(bridge_call.os, "name", "posix")
    monkeypatch.delattr(bridge_call.os, "killpg", raising=False)
    process = _Process()
    bridge_call._terminate_process_group(process)

    assert killed == [True]
    assert waited == [2.0]
    assert process.returncode == (1 if wait_result == "exited" else None)
    diagnostic = capsys.readouterr().err
    assert diagnostic == (
        "" if wait_result == "exited" else "Bridge child exit status unavailable after bounded cleanup wait\n"
    )


class _ReapedLeader:
    """Provide a group leader that was already reaped, leaving only its process group's liveness in question."""

    pid = 4242
    returncode = 0

    def wait(self, timeout: float) -> int:
        """Report the leader's exit status immediately, as a reaped child does."""
        return self.returncode


class _LeaderReapedDuringGrace:
    """Provide a group leader still running when termination starts and reaped by the first grace wait."""

    pid = 4242
    returncode: int | None = None

    def wait(self, timeout: float) -> int:
        """Reap the leader on the first wait, as SIGTERM followed by a prompt exit does."""
        self.returncode = 0
        return self.returncode


def _simulate_group_probe(
    monkeypatch: pytest.MonkeyPatch, grace_seconds: float, probe_error: type[OSError] | None
) -> list[tuple[int, int]]:
    """Install a fake POSIX group whose signal-0 probe raises ``probe_error`` or reports the group alive."""
    signals: list[tuple[int, int]] = []

    def fake_killpg(process_group: int, signal_number: int) -> None:
        """Record the signal; only the existence probe and the final SIGKILL see an absent or inaccessible group."""
        signals.append((process_group, signal_number))
        if probe_error is not None and signal_number in (0, 9):
            raise probe_error

    monkeypatch.setattr(bridge_call.os, "name", "posix")
    monkeypatch.setattr(bridge_call.os, "killpg", fake_killpg, raising=False)
    monkeypatch.setattr(bridge_call.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(bridge_call, "CLEANUP_GRACE_SECONDS", grace_seconds)
    return signals


def test_cleanup_grace_ends_once_the_process_group_is_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the rest of the cleanup grace when SIGTERM has already emptied the child's process group.

    A tree that exits promptly must not make every termination wait out the full grace. No force-kill follows: the group
    is empty and its leader reaped, so its id may already belong to an unrelated process group.
    """
    signals = _simulate_group_probe(monkeypatch, grace_seconds=10.0, probe_error=ProcessLookupError)

    started = time.monotonic()
    bridge_call._terminate_process_group(_LeaderReapedDuringGrace())
    elapsed = time.monotonic() - started

    assert elapsed < 5.0
    assert signals == [(4242, bridge_call.signal.SIGTERM), (4242, 0)]


def test_termination_never_signals_a_reaped_leader_whose_group_is_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Send no group signal when the leader is already reaped and its process group already empty.

    A late output-limit cleanup can reach termination after the child exited and drained. The group id is then free for
    reuse, so even SIGTERM could hit an unrelated process group; only the signal-0 existence probe may run.
    """
    signals = _simulate_group_probe(monkeypatch, grace_seconds=10.0, probe_error=ProcessLookupError)

    bridge_call._terminate_process_group(_ReapedLeader())

    assert signals == [(4242, 0)]


@pytest.mark.parametrize(
    "probe_error",
    [pytest.param(None, id="member-alive"), pytest.param(PermissionError, id="member-not-signalable")],
)
def test_cleanup_grace_is_fully_waited_while_the_process_group_has_a_member(
    monkeypatch: pytest.MonkeyPatch, probe_error: type[OSError] | None
) -> None:
    """Keep waiting the whole grace after the leader exits while a descendant still occupies its group.

    Leader exit alone is no proof the tree ended: descendants can outlive it holding inherited pipes, and a member the
    user may not signal still counts as alive.
    """
    grace_seconds = 0.3
    signals = _simulate_group_probe(monkeypatch, grace_seconds=grace_seconds, probe_error=probe_error)

    started = time.monotonic()
    bridge_call._terminate_process_group(_ReapedLeader())
    elapsed = time.monotonic() - started

    assert elapsed >= grace_seconds
    assert signals[-1] == (4242, 9)


@pytest.mark.parametrize("tree_status", [0, 1])
def test_simulated_windows_termination_uses_tree_kill_before_the_leader_can_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tree_status: int,
) -> None:
    """Kill the tree before its leader exits, then collect the leader's actual exit status.

    With `os.name` faked as nt, a stand-in `taskkill.EXE` on PATH and a fake leader, termination must run `taskkill /PID
    42 /T /F` before waiting on the leader. Only a failed tree kill (status 1) falls back to killing the leader
    directly, and no POSIX signal is ever sent.
    """
    sent: list[int] = []
    killed: list[bool] = []
    tree_kills: list[list[str]] = []
    taskkill = tmp_path / "taskkill.EXE"
    taskkill.touch()
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("PATHEXT", ".EXE")
    monkeypatch.setattr(bridge_call.os, "name", "nt")
    monkeypatch.setattr(
        bridge_call.subprocess,
        "run",
        lambda command, **kwargs: tree_kills.append(command) or subprocess.CompletedProcess(command, tree_status),
    )

    class _Process:
        """Track Windows termination calls without launching a real process."""

        pid = 42
        returncode = None

        def kill(self) -> None:
            """Record the simulated Windows child kill request."""
            killed.append(True)

        def wait(self, timeout: float) -> int:
            """Return the simulated Windows child's exit status."""
            assert tree_kills
            assert 0 < timeout <= 2
            self.returncode = 1
            return self.returncode

    process = _Process()
    bridge_call._terminate_process_group(process)

    assert tree_kills == [[str(taskkill), "/PID", "42", "/T", "/F"]]
    assert killed == ([True] if tree_status else [])
    assert sent == []
    assert process.returncode == 1


def test_simulated_windows_cancel_is_cooperative_without_posix_process_apis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prevent Windows cancellation from signalling a PID stored in a job record.

    With os.name simulated as nt and os.kill and os.killpg failing the test, cancelling a running record only requests
    cancellation. Status then reads cancel_requested, so Windows never signals a stored PID.
    """
    paths = bridge_call.BridgePaths(tmp_path.resolve())
    paths.prepare()
    job_id = "44444444-4444-4444-8444-444444444444"
    bridge_call._write_json(paths.jobs / f"{job_id}.json", {"job_id": job_id, "status": "running", "pid": 42})
    monkeypatch.setattr(bridge_call.os, "name", "nt")
    monkeypatch.setattr(bridge_call, "_process_exists", lambda pid: True)
    monkeypatch.setattr(
        bridge_call.os, "kill", lambda *args: pytest.fail("Windows cancellation signalled a PID"), raising=False
    )
    monkeypatch.setattr(
        bridge_call.os, "killpg", lambda *args: pytest.fail("Windows cancellation used killpg"), raising=False
    )

    cancelled = bridge_call.cancel_job(tmp_path, job_id)

    assert cancelled["status"] == "cancel_requested"
    assert bridge_call.job_status(tmp_path, job_id)["status"] == "cancel_requested"


def test_simulated_windows_cancel_never_uses_taskkill_for_a_persisted_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prevent a stale Windows record from escalating cooperative cancellation into tree termination.

    With os.name simulated as nt, subprocess.run answers only a liveness probe and fails if taskkill is run. Cancelling
    a record that names PID 42 stays cooperative and returns cancel_requested.
    """
    paths = bridge_call.BridgePaths(tmp_path.resolve())
    paths.prepare()
    job_id = "55555555-5555-4555-8555-555555555555"
    bridge_call._write_json(paths.jobs / f"{job_id}.json", {"job_id": job_id, "status": "running", "pid": 42})

    def _fail_on_taskkill(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        """Fail if cooperative cancellation unexpectedly invokes ``taskkill``."""
        if command[0] == "taskkill":
            pytest.fail("taskkill was run")
        return subprocess.CompletedProcess(command, 0, stdout='"codex.exe","42"', stderr="")

    monkeypatch.setattr(bridge_call.os, "name", "nt")
    monkeypatch.setattr(bridge_call.subprocess, "run", _fail_on_taskkill)

    cancelled = bridge_call.cancel_job(tmp_path, job_id)

    assert cancelled["status"] == "cancel_requested"


@pytest.mark.skipif(not DIRECTORY_SYMLINKS_SUPPORTED, reason="directory symlinks unavailable")
def test_artifact_validation_failure_reaps_child_and_drains_readers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launches: _LaunchRecorder
) -> None:
    """Reject changed artifact boundaries without abandoning a genuinely launched child.

    After a real child starts, the artifact store is renamed and replaced by a symlink outside the workspace. The run
    raises a root-escape error, and the launched child has exited with both output readers joined.
    """
    workspace = tmp_path / "project"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    paths = bridge_call.BridgePaths(workspace)
    paths.prepare()
    job_path = paths.jobs / "owned.json"

    def spawn_then_change_boundary(command: list[str], selected_workspace: Path, timeout: float) -> Any:
        """Change actual filesystem state only after the genuine subprocess starts."""
        process = launches.spawn(command, selected_workspace, timeout)
        paths.root.rename(workspace / "retained-store")
        (workspace / ".temp" / "bridge").symlink_to(outside, target_is_directory=True)
        return process

    monkeypatch.setattr(bridge_call, "_spawn_child", spawn_then_change_boundary)

    with pytest.raises(ValueError, match="root escapes"):
        bridge_call._run_child([sys.executable, "-c", "import time; time.sleep(30)"], workspace, 0.2, job_path)

    assert len(launches.processes) == 1
    assert launches.processes[0].poll() is not None, "validation rejection abandoned the launched child"
    assert len(launches.readers) == 2
    assert not any(reader.is_alive() for reader in launches.readers), "owned output readers were not drained"


def test_partial_output_reader_start_failure_reaps_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launches: _LaunchRecorder
) -> None:
    """Clean up the real child and first reader when starting the second reader fails.

    The second output reader thread fails to start after the first and the real child are running. The original error
    propagates, the child is dead and the first reader has finished.
    """
    failure = RuntimeError("second output reader could not start")

    def fail_second_start(reader: threading.Thread) -> None:
        """Model OS thread-start failure after one actual reader has started."""
        if launches.readers:
            raise failure
        launches.start_reader(reader)

    monkeypatch.setattr(threading.Thread, "start", fail_second_start)

    with pytest.raises(RuntimeError) as raised:
        bridge_call._run_child([sys.executable, "-c", "import time; time.sleep(30)"], tmp_path, 0.2)

    assert raised.value is failure
    assert len(launches.processes) == 1
    assert launches.processes[0].poll() is not None, "partial reader startup abandoned the launched child"
    assert len(launches.readers) == 1
    assert not launches.readers[0].is_alive()


def test_wait_failure_reaps_child_and_drains_readers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launches: _LaunchRecorder
) -> None:
    """Preserve an OS wait failure while cleaning up a genuinely running peer.

    The first wait call in the supervision loop raises OSError while a real child runs. The error propagates unchanged,
    the child is terminated, and both readers are drained.
    """
    sleep = time.sleep
    failure = OSError("OS wait interrupted")

    def fail_first_wait(seconds: float) -> None:
        """Inject one external wait failure; cleanup waits use the real OS function."""
        monkeypatch.setattr(bridge_call.time, "sleep", sleep)
        raise failure

    monkeypatch.setattr(bridge_call.time, "sleep", fail_first_wait)

    with pytest.raises(OSError, match=r"OS wait interrupted") as raised:
        bridge_call._run_child([sys.executable, "-c", "import time; time.sleep(30)"], tmp_path, 0.2)

    assert raised.value is failure
    assert len(launches.processes) == 1
    assert launches.processes[0].poll() is not None, "wait failure abandoned the launched child"
    assert len(launches.readers) == 2
    assert not any(reader.is_alive() for reader in launches.readers)
