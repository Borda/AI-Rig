"""Reject sensitive contexts and unsafe lifecycle Git configuration before use."""

from __future__ import annotations

import io
import json
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from _modules import load_module, load_module_registered

SHARED = Path(__file__).resolve().parents[2] / "shared"


@pytest.mark.parametrize(
    "token",
    [
        pytest.param("AKIA" + "A" * 16, id="aws"),
        pytest.param("prefix_ghp_" + "a" * 24, id="github-after-underscore"),
        pytest.param("xoxb-" + "a" * 24, id="slack"),
        pytest.param("AIza" + "a" * 35, id="google"),
        pytest.param("npm_" + "a" * 36, id="npm"),
        pytest.param("eyJ" + "a" * 20 + ".eyJ" + "b" * 20 + "." + "c" * 30, id="jwt"),
    ],
)
def test_both_scanners_reject_provider_tokens(token: str) -> None:
    """Prevent a clean context attestation for provider credential shapes."""
    execution = load_module("execution_input_safety", SHARED / "parallel_execution.py")
    reviewer = load_module_registered("reviewer_input_safety", SHARED / "local_reviewer_wave.py")
    assert execution.review_input_has_sensitive_material(token, [])
    assert reviewer._contains_secret(token.encode())


@pytest.mark.parametrize("binary", [pytest.param(False, id="text"), pytest.param(True, id="bytes")])
def test_git_read_disables_command_hooks_and_diff_transforms(tmp_path: Path, binary: bool) -> None:
    """Prevent parent reads from invoking child-configured commands."""
    lifecycle = load_module("worktree_input_safety", SHARED / "parallel_worktrees.py")
    result = subprocess.CompletedProcess([], 0, stdout=b"" if binary else "")
    with mock.patch.object(lifecycle.subprocess, "run", autospec=True, return_value=result) as run:
        reader = lifecycle._git_bytes if binary else lifecycle._git
        reader(tmp_path, "diff", "--binary", "HEAD", "--", "owned.txt")
    argv = run.call_args.args[0]
    assert "core.fsmonitor=false" in argv
    assert "core.hooksPath=" + lifecycle.os.devnull in argv
    assert "--no-ext-diff" in argv
    assert "--no-textconv" in argv


def test_child_gitfile_cannot_redirect_to_other_repository(tmp_path: Path) -> None:
    """Reject a rewritten child gitfile before a parent Git subprocess starts."""
    lifecycle = load_module("worktree_gitfile_safety", SHARED / "parallel_worktrees.py")
    source = tmp_path / "source"
    source.mkdir()
    (source / ".git").mkdir()
    child = tmp_path / "child"
    child.mkdir()
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (child / ".git").write_text("gitdir: " + str(foreign), encoding="utf-8")
    state = {"repository": "source", "baseline_head": "a" * 40}
    node = {"node_id": "n1", "worktree_path": "child", "owned_paths": ["owned.txt"]}
    with mock.patch.object(lifecycle.subprocess, "run", autospec=True) as run:
        with pytest.raises(lifecycle.PilotError, match="worktree-gitfile"):
            lifecycle._child_patch(state, tmp_path, node)
    run.assert_not_called()


def test_send_returns_at_shared_deadline_when_pipe_blocks() -> None:
    """Keep a nonreading reviewer from blocking the parent indefinitely."""
    reviewer = load_module_registered("reviewer_send_safety", SHARED / "local_reviewer_wave.py")
    release = threading.Event()
    stream = mock.Mock(spec=io.TextIOBase)
    stream.write.side_effect = lambda frame: release.wait(2)
    process = SimpleNamespace(stdin=stream, stdout=io.StringIO())
    client = reviewer._JsonRpcStdio(process, time.monotonic() + 0.04)
    start = time.monotonic()
    try:
        with pytest.raises(reviewer.ReviewRouteError, match="app-server-timeout"):
            client.send({"method": "initialize"})
        assert time.monotonic() - start < 0.5
    finally:
        release.set()


def test_event_queue_retains_at_most_two_maximum_frames() -> None:
    """Bound buffered reviewer events independently of the former 800 MiB budget."""
    reviewer = load_module_registered("reviewer_queue_safety", SHARED / "local_reviewer_wave.py")
    assert reviewer.MAX_BUFFERED_EVENTS <= 2
    assert reviewer.MAX_BUFFERED_EVENT_BYTES <= 26 * 1024 * 1024


def test_mcp_discovery_reads_configuration_without_starting_app_server(tmp_path: Path) -> None:
    """Discover all configured names before launching a host with explicit denials."""
    reviewer = load_module_registered("reviewer_discovery_safety", SHARED / "local_reviewer_wave.py")
    result = subprocess.CompletedProcess([], 0, stdout=json.dumps([{"name": "local", "enabled": True}]))
    with mock.patch.object(reviewer.subprocess, "run", autospec=True, return_value=result) as run:
        names = reviewer._discover_mcp_servers(Path("codex"), tmp_path, time.monotonic() + 1)
    assert names == ["local"]
    assert run.call_args.args[0][:4] == ["codex", "mcp", "list", "--json"]
    assert run.call_args.kwargs["timeout"] <= 1


def test_pending_events_obey_aggregate_byte_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bound unmatched decoded events by bytes while permitting normal small lifecycle notifications."""
    reviewer = load_module_registered("reviewer_pending_safety", SHARED / "local_reviewer_wave.py")
    client = reviewer._JsonRpcStdio(SimpleNamespace(stdin=io.StringIO(), stdout=io.StringIO()), time.monotonic() + 1)
    monkeypatch.setattr(reviewer, "MAX_BUFFERED_EVENT_BYTES", 100)
    monkeypatch.setattr(client, "_read", lambda: {"id": 2, "result": {"text": "a" * 75}})
    with pytest.raises(reviewer.ReviewRouteError, match="pending-events-overflow"):
        client.request("initialize", {})
    assert client._pending == []
