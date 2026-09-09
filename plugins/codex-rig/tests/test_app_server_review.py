"""Exercise the bounded App Server review evidence adapter."""

from __future__ import annotations

import importlib.util
import hashlib
import io
import json
import os
from pathlib import Path
import sys
from types import ModuleType

import pytest
from _platform import SYMLINKS_AVAILABLE


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_ROLES = PLUGIN_ROOT / "roles"
_ADAPTER: ModuleType | None = None


def _adapter() -> ModuleType:
    """Load the production adapter without treating the plugin root as a package."""
    global _ADAPTER
    if _ADAPTER is not None:
        return _ADAPTER
    path = PLUGIN_ROOT / "shared" / "app_server_review.py"
    spec = importlib.util.spec_from_file_location("app_server_review", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _ADAPTER = module
    return _ADAPTER


def test_adapter_exports_evidence_validator() -> None:
    """Expose the evidence validator required by code-review integration."""
    assert callable(_adapter().validate_evidence)


def test_invocation_controls_disable_each_discovered_simple_mcp_server() -> None:
    """Keep empty-map merging harmless by also disabling every discovered server directly."""
    command = _adapter()._server_command(Path("codex"), ["one", "two"])

    assert command[:3] == ["codex", "app-server", "--stdio"]
    assert "mcp_servers={}" in command
    assert "mcp_servers.one.enabled=false" in command
    assert "mcp_servers.two.enabled=false" in command


def test_thread_controls_reject_requested_without_observed_network_denial() -> None:
    """Require the host response rather than accepting the requested read-only mode."""
    node = {"model": "gpt-5.6-terra", "reasoning_effort": "high"}
    observed = {
        "sandbox": {"type": "readOnly", "networkAccess": True},
        "approvalPolicy": "never",
        "model": "gpt-5.6-terra",
        "reasoningEffort": "high",
    }

    with pytest.raises(_adapter().ReviewRouteError, match="app-server-thread-controls-mismatch"):
        _adapter()._thread_controls(observed, node)


class _FakeProcess:
    """Provide one deterministic JSON-RPC stdio process without a host or model."""

    def __init__(self, frames: list[dict[str, object]]) -> None:
        """Create writable stdin and precomputed JSON Lines stdout for one process launch."""
        self.stdin = io.StringIO()
        self.stdout = io.StringIO("".join(json.dumps(frame) + "\n" for frame in frames))
        self.pid = 4312
        self.returncode = 0

    def poll(self) -> int:
        """Report that the local fake parent may already have exited."""
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        """Return the completed fake process status without using a platform process."""
        del timeout
        return self.returncode


class _PopenFactory:
    """Return prepared fake processes and retain their client JSON-RPC writes."""

    def __init__(self, launches: list[list[dict[str, object]]]) -> None:
        """Store the exact process response plans in launch order."""
        self._launches = launches
        self.processes: list[_FakeProcess] = []
        self.commands: list[list[str]] = []

    def __call__(self, *args: object, **kwargs: object) -> _FakeProcess:
        """Create the next planned local process without inspecting command arguments."""
        del kwargs
        command = args[0]
        assert isinstance(command, list) and all(isinstance(part, str) for part in command)
        self.commands.append(command)
        process = _FakeProcess(self._launches[len(self.processes)])
        self.processes.append(process)
        return process


def _response(request_id: int, result: dict[str, object]) -> dict[str, object]:
    """Build one compact successful JSON-RPC response frame."""
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _effective_config(*, disabled: bool) -> dict[str, object]:
    """Build only the configuration shape the adapter is allowed to retain."""
    features = {
        "hooks": False,
        "plugins": False,
        "apps": False,
        "remote_plugin": False,
        "browser_use": False,
        "browser_use_external": False,
        "in_app_browser": False,
        "computer_use": False,
        "image_generation": False,
        "tool_suggest": False,
        "skill_mcp_dependency_install": False,
        "shell_snapshot": False,
        "multi_agent": False,
    }
    server = {"enabled": False} if disabled else {"enabled": True}
    return {
        "config": {
            "mcp_servers": {"local": server},
            "features": features,
            "notify": [],
            "web_search": "disabled",
            "allow_login_shell": False,
        }
    }


def _thread_result(role_index: int, model: str) -> dict[str, object]:
    """Return the observed no-turn thread control response for one role."""
    return {
        "thread": {"id": f"thread-{role_index}"},
        "sandbox": {"type": "readOnly", "networkAccess": False},
        "approvalPolicy": "never",
        "model": model,
        "reasoningEffort": "high",
    }


def _launches_for_plan(
    plan_path: Path, *, echo_input: bool = False, fail_second: bool = False, first_final: str | None = None
) -> list[list[dict[str, object]]]:
    """Return discovery and reviewer-host frames for a two-role public adapter call."""
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    first = [_response(1, {}), _response(2, _effective_config(disabled=False))]
    second = [_response(1, {}), _response(2, _effective_config(disabled=True))]
    input_completions: list[dict[str, object]] = []
    # App Server may notify lifecycle state while the matching request is pending.
    second.append({"jsonrpc": "2.0", "method": "thread/started", "params": {}})
    for index, node in enumerate(plan["nodes"], start=3):
        second.append(_response(index, _thread_result(index - 3, node["model"])))
    for index, node in enumerate(plan["nodes"], start=5):
        if echo_input:
            context_path = plan_path.parent / node["context_path"]
            item = {
                "id": f"input-{index - 5}",
                "type": "userMessage",
                "content": [{"type": "text", "text": context_path.read_text(encoding="utf-8")}],
            }
            second.append(
                {
                    "jsonrpc": "2.0",
                    "method": "item/started",
                    "params": {
                        "threadId": f"thread-{index - 5}",
                        "turnId": f"turn-{index - 5}",
                        "item": item,
                    },
                }
            )
            input_completions.append(
                {
                    "jsonrpc": "2.0",
                    "method": "item/completed",
                    "params": {
                        "threadId": f"thread-{index - 5}",
                        "turnId": f"turn-{index - 5}",
                        "item": {
                            "id": f"input-{index - 5}",
                            "type": "userMessage",
                            "content": [{"type": "text", "text": context_path.read_text(encoding="utf-8")}],
                        },
                    },
                }
            )
        second.append(_response(index, {"turn": {"id": f"turn-{index - 5}"}}))
    second.extend(input_completions)
    first_role = plan["nodes"][0]["role_id"]
    second.extend(
        [
            {
                "jsonrpc": "2.0",
                "method": "item/completed",
                "params": {
                    "threadId": "thread-0",
                    "turnId": "turn-0",
                    "item": {
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": first_final if first_final is not None else f"{first_role} final\n",
                    },
                },
            },
            {
                "jsonrpc": "2.0",
                "method": "turn/completed",
                "params": {"threadId": "thread-0", "turn": {"id": "turn-0", "status": "completed"}},
            },
        ]
    )
    if fail_second:
        second.append({"jsonrpc": "2.0", "method": "item/commandExecution/requestApproval", "params": {}})
    else:
        second.extend(
            [
                {
                    "jsonrpc": "2.0",
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "item": {"type": "agentMessage", "phase": "final_answer", "text": "second final\n"},
                    },
                },
                {
                    "jsonrpc": "2.0",
                    "method": "turn/completed",
                    "params": {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}},
                },
            ]
        )
    return [first, second]


def _fake_public_processes(monkeypatch: pytest.MonkeyPatch, launches: list[list[dict[str, object]]]) -> _PopenFactory:
    """Install fake process and version boundaries while keeping public adapter paths intact."""
    adapter = _adapter()
    factory = _PopenFactory(launches)
    monkeypatch.setattr(adapter.sys, "platform", "darwin")
    monkeypatch.setattr(adapter.subprocess, "Popen", factory)
    monkeypatch.setattr(
        adapter.subprocess, "run", lambda *args, **kwargs: type("Result", (), {"returncode": 0, "stdout": "0.153.4"})()
    )
    monkeypatch.setattr(adapter.os, "killpg", lambda process_id, signal: None, raising=False)
    monkeypatch.setattr(adapter.signal, "SIGTERM", 15, raising=False)
    monkeypatch.setattr(adapter.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(adapter, "_posix_group_exists", lambda process_id: False)
    return factory


def test_check_host_uses_public_preparation_without_turn_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify effective denials and observed thread controls without a paid model turn."""
    plan_path, _ = review_evidence_files(tmp_path)
    launches = _launches_for_plan(plan_path)
    launches[1] = launches[1][:5]
    factory = _fake_public_processes(monkeypatch, launches)

    summary = _adapter().check_host(plan_path, Path("codex"), 10)

    assert summary["model_invoked"] is False
    requests = [json.loads(line)["method"] for line in factory.processes[1].stdin.getvalue().splitlines()]
    assert requests.count("thread/start") == 2
    assert "turn/start" not in requests
    assert "mcp_servers={}" in factory.commands[1]
    assert "mcp_servers.local.enabled=false" in factory.commands[1]
    request_payloads = [json.loads(line) for line in factory.processes[1].stdin.getvalue().splitlines()]
    assert request_payloads[0]["params"]["capabilities"] == {"experimentalApi": True}
    assert all(
        request["params"].get("allowProviderModelFallback") is False
        for request in request_payloads
        if request["method"] == "thread/start"
    )


def test_check_host_installs_posix_cleanup_doubles_when_host_apis_are_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep public host checks portable when the native test platform lacks POSIX process APIs."""
    adapter = _adapter()
    monkeypatch.delattr(adapter.os, "killpg", raising=False)
    monkeypatch.delattr(adapter.signal, "SIGTERM", raising=False)
    monkeypatch.delattr(adapter.signal, "SIGKILL", raising=False)

    assert not hasattr(adapter.os, "killpg")
    plan_path, _ = review_evidence_files(tmp_path)
    launches = _launches_for_plan(plan_path)
    launches[1] = launches[1][:5]
    factory = _fake_public_processes(monkeypatch, launches)

    summary = adapter.check_host(plan_path, Path("codex"), 10)

    assert summary["model_invoked"] is False
    assert hasattr(adapter.os, "killpg")
    requests = [json.loads(line)["method"] for line in factory.processes[1].stdin.getvalue().splitlines()]
    assert "turn/start" not in requests


def test_check_host_reports_only_a_sanitized_trailing_notification_method(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep a rejected post-cleanup notification diagnosable without retaining its payload."""
    plan_path, _ = review_evidence_files(tmp_path)
    launches = _launches_for_plan(plan_path)
    launches[1][2] = {"jsonrpc": "2.0", "method": "thread/unknown", "params": {"secret": "discarded"}}
    launches[1] = launches[1][:5]
    _fake_public_processes(monkeypatch, launches)

    with pytest.raises(_adapter().ReviewRouteError, match="app-server-events-after-cleanup:thread/unknown"):
        _adapter().check_host(plan_path, Path("codex"), 10)


def test_check_host_accepts_only_disabled_remote_control_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Allow the schema-defined disabled control status while retaining no status payload."""
    plan_path, _ = review_evidence_files(tmp_path)
    launches = _launches_for_plan(plan_path)
    launches[1][2] = {
        "jsonrpc": "2.0",
        "method": "remoteControl/status/changed",
        "params": {"status": "disabled", "identities": [{"ignored": "identity"}]},
    }
    launches[1] = launches[1][:5]
    _fake_public_processes(monkeypatch, launches)

    summary = _adapter().check_host(plan_path, Path("codex"), 10)

    assert summary["model_invoked"] is False


@pytest.mark.parametrize(
    "params",
    [
        pytest.param({"status": "connecting"}, id="connecting"),
        pytest.param({"status": "connected"}, id="connected"),
        pytest.param({"status": "errored"}, id="errored"),
        pytest.param({}, id="missing-status"),
    ],
)
def test_check_host_rejects_enabled_or_unknown_remote_control_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, params: dict[str, str]
) -> None:
    """Stop host preparation when remote control is not authoritatively disabled."""
    plan_path, _ = review_evidence_files(tmp_path)
    launches = _launches_for_plan(plan_path)
    launches[1][2] = {"jsonrpc": "2.0", "method": "remoteControl/status/changed", "params": params}
    launches[1] = launches[1][:5]
    factory = _fake_public_processes(monkeypatch, launches)

    with pytest.raises(_adapter().ReviewRouteError, match="app-server-remote-control-status-invalid"):
        _adapter().check_host(plan_path, Path("codex"), 10)

    requests = [json.loads(line)["method"] for line in factory.processes[1].stdin.getvalue().splitlines()]
    assert "turn/start" not in requests


def test_run_review_dispatches_all_turns_and_persists_completed_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Start every reviewer turn before waiting and retain both terminal-answer responses."""
    plan_path, _ = review_evidence_files(tmp_path)
    factory = _fake_public_processes(monkeypatch, _launches_for_plan(plan_path))
    output = tmp_path / "review-output"

    evidence_path = _adapter().run_review(plan_path, output, Path("codex"), 10)

    assert evidence_path == output / "evidence.json"
    assert (output / "challenger.md").read_text(encoding="utf-8") == "challenger final\n"
    assert (output / "cicd-steward.md").read_text(encoding="utf-8") == "second final\n"
    requests = [json.loads(line)["method"] for line in factory.processes[1].stdin.getvalue().splitlines()]
    assert requests.index("turn/start") > requests.index("thread/start")
    assert requests.count("turn/start") == 2


def test_run_review_rejects_output_outside_plan_parent_before_host_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep the new output tree inside the frozen plan's parent directory."""
    plan_path, _ = review_evidence_files(tmp_path)
    output = tmp_path.parent / f"{tmp_path.name}-outside-output"
    factory = _fake_public_processes(monkeypatch, _launches_for_plan(plan_path))

    with pytest.raises(_adapter().ReviewRouteError, match="output-root-outside-plan"):
        _adapter().run_review(plan_path, output, Path("codex"), 10)

    assert not output.exists()
    assert factory.processes == []


@pytest.mark.skipif(not SYMLINKS_AVAILABLE, reason="filesystem cannot create symlinks")
def test_run_review_rejects_output_parent_symlink_escape_before_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refuse a new output beneath an in-plan symlink that resolves outside the frozen run root."""
    plan_path, _ = review_evidence_files(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (tmp_path / "linked").symlink_to(outside, target_is_directory=True)
    output = tmp_path / "linked" / "review-output"
    factory = _fake_public_processes(monkeypatch, _launches_for_plan(plan_path))

    with pytest.raises(_adapter().ReviewRouteError, match="output-root-outside-plan"):
        _adapter().run_review(plan_path, output, Path("codex"), 10)

    assert not (outside / "review-output").exists()
    assert factory.processes == []


def test_run_review_preserves_nested_output_under_plan_parent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Retain supported creation of a new nested output directory inside the run root."""
    plan_path, _ = review_evidence_files(tmp_path)
    output = tmp_path / "nested" / "review-output"
    _fake_public_processes(monkeypatch, _launches_for_plan(plan_path))

    evidence_path = _adapter().run_review(plan_path, output, Path("codex"), 10)

    assert evidence_path == output / "evidence.json"


def test_run_review_wraps_output_directory_setup_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Return a bounded route error when the new contained output directory cannot be created."""
    plan_path, _ = review_evidence_files(tmp_path)
    output = tmp_path / "review-output"
    original_mkdir = Path.mkdir

    def deny_output_directory(self: Path, *args: object, **kwargs: object) -> None:
        """Deny only the public run's output root while preserving fixture operations."""
        if self == output:
            raise OSError("denied")
        original_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", deny_output_directory)

    with pytest.raises(_adapter().ReviewRouteError, match="output-root-unavailable"):
        _adapter().run_review(plan_path, output, Path("codex"), 10)

    assert not (output / "evidence.json").exists()


def test_run_review_persists_setup_version_failure_after_output_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Write bounded failed evidence when version setup fails after the contained root exists."""
    plan_path, _ = review_evidence_files(tmp_path)
    output = tmp_path / "review-output"
    factory = _fake_public_processes(monkeypatch, _launches_for_plan(plan_path))
    monkeypatch.setattr(
        _adapter().subprocess,
        "run",
        lambda *args, **kwargs: type("Result", (), {"returncode": 1, "stdout": ""})(),
    )

    with pytest.raises(_adapter().ReviewRouteError, match="codex-version-unavailable"):
        _adapter().run_review(plan_path, output, Path("codex"), 10)

    assert factory.processes == []
    assert json.loads((output / "evidence.json").read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "route": "app-server",
        "status": "failed",
        "failure_code": "codex-version-unavailable",
        "failure_phase": "preparation",
        "turn_dispatch": {"attempted": 0, "acknowledged": 0},
        "cleanup": "not-started",
    }


def test_run_review_accepts_schema_terminal_final_answer_phase(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Accept only the generated-schema terminal assistant-message phase."""
    plan_path, _ = review_evidence_files(tmp_path)
    launches = _launches_for_plan(plan_path)
    for frame in launches[1]:
        if frame.get("method") == "item/completed" and frame["params"]["item"].get("type") == "agentMessage":
            frame["params"]["item"]["phase"] = "final_answer"
    _fake_public_processes(monkeypatch, launches)

    _adapter().run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)


@pytest.mark.parametrize(
    "phase",
    ["commentary", "final", None],
)
def test_run_review_rejects_nonterminal_agent_message_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str | None
) -> None:
    """Reject commentary, obsolete final, and unknown phases as terminal answers."""
    plan_path, _ = review_evidence_files(tmp_path)
    launches = _launches_for_plan(plan_path)
    for frame in launches[1]:
        if frame.get("method") == "item/completed" and frame["params"]["item"].get("type") == "agentMessage":
            frame["params"]["item"]["phase"] = phase
    _fake_public_processes(monkeypatch, launches)

    with pytest.raises(_adapter().ReviewRouteError, match="app-server-turn-final-missing"):
        _adapter().run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)


def test_run_review_separates_noncompleted_turn_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Report a terminal status mismatch separately from final-answer absence."""
    plan_path, _ = review_evidence_files(tmp_path)
    launches = _launches_for_plan(plan_path)
    completion = next(frame for frame in launches[1] if frame.get("method") == "turn/completed")
    completion["params"]["turn"]["status"] = "failed"
    _fake_public_processes(monkeypatch, launches)

    with pytest.raises(_adapter().ReviewRouteError, match="app-server-turn-status-invalid"):
        _adapter().run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)


def test_run_review_separates_incomplete_user_message_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Report a missing echo completion separately from a missing reviewer final answer."""
    plan_path, _ = review_evidence_files(tmp_path)
    launches = _launches_for_plan(plan_path, echo_input=True)
    launches[1] = [
        frame
        for frame in launches[1]
        if not (frame.get("method") == "item/completed" and frame["params"]["item"].get("type") == "userMessage")
    ]
    _fake_public_processes(monkeypatch, launches)

    with pytest.raises(_adapter().ReviewRouteError, match="app-server-user-message-lifecycle-incomplete"):
        _adapter().run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)


def test_run_review_accepts_large_unicode_user_message_echo_before_turn_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Accept the exact large Unicode input echo that the host emits before acknowledging a turn."""
    plan_path, _ = review_evidence_files(tmp_path)
    _replace_context(plan_path, 0, "😀" * 24_000)
    _fake_public_processes(monkeypatch, _launches_for_plan(plan_path, echo_input=True))

    _adapter().run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)


def test_run_review_accepts_user_message_echo_with_empty_text_elements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Accept the schema-defined empty UI span list without admitting altered input."""
    plan_path, _ = review_evidence_files(tmp_path)
    launches = _launches_for_plan(plan_path, echo_input=True)
    echo = next(frame for frame in launches[1] if frame.get("method") == "item/started")
    echo["params"]["item"]["content"][0]["text_elements"] = []
    _fake_public_processes(monkeypatch, launches)

    _adapter().run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)


def test_run_review_rejects_mismatched_user_message_echo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject a user-message echo whose full text is not the frozen context sent to that turn."""
    plan_path, _ = review_evidence_files(tmp_path)
    launches = _launches_for_plan(plan_path, echo_input=True)
    echo = next(frame for frame in launches[1] if frame.get("method") == "item/started")
    echo["params"]["item"]["content"][0]["text"] = "altered context"
    _fake_public_processes(monkeypatch, launches)

    with pytest.raises(_adapter().ReviewRouteError, match="app-server-user-message-lifecycle-invalid"):
        _adapter().run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)

    evidence = json.loads((tmp_path / "review-output" / "evidence.json").read_text(encoding="utf-8"))
    assert evidence == {
        "schema_version": 1,
        "route": "app-server",
        "status": "failed",
        "failure_code": "app-server-user-message-lifecycle-invalid",
        "failure_phase": "turn-events",
        "turn_dispatch": {"attempted": 2, "acknowledged": 2},
        "cleanup": "failed",
    }


def test_run_review_failure_evidence_uses_static_reason_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Omit the request method from failure evidence while preserving it in the raised route error."""
    plan_path, _ = review_evidence_files(tmp_path)
    launches = _launches_for_plan(plan_path)
    response_index = next(index for index, frame in enumerate(launches[1]) if frame.get("id") == 5)
    launches[1][response_index] = {"jsonrpc": "2.0", "id": 5, "error": {}}
    _fake_public_processes(monkeypatch, launches)

    with pytest.raises(_adapter().ReviewRouteError, match="app-server-request-failed:turn/start"):
        _adapter().run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)

    evidence = json.loads((tmp_path / "review-output" / "evidence.json").read_text(encoding="utf-8"))
    assert evidence["failure_code"] == "app-server-request-failed"


def test_run_review_rejects_mismatched_user_message_completion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject a completion whose input differs from its accepted user-message start."""
    plan_path, _ = review_evidence_files(tmp_path)
    launches = _launches_for_plan(plan_path, echo_input=True)
    completion = next(
        frame
        for frame in launches[1]
        if frame.get("method") == "item/completed" and frame["params"]["item"].get("type") == "userMessage"
    )
    completion["params"]["item"]["content"][0]["text"] = "altered completion"
    _fake_public_processes(monkeypatch, launches)

    with pytest.raises(_adapter().ReviewRouteError, match="app-server-user-message-lifecycle-invalid"):
        _adapter().run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)


def test_run_review_rejects_duplicate_user_message_completion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject a second completion for the same user-message lifecycle item."""
    plan_path, _ = review_evidence_files(tmp_path)
    launches = _launches_for_plan(plan_path, echo_input=True)
    completion_index = next(
        index
        for index, frame in enumerate(launches[1])
        if frame.get("method") == "item/completed" and frame["params"]["item"].get("type") == "userMessage"
    )
    launches[1].insert(completion_index + 1, launches[1][completion_index])
    _fake_public_processes(monkeypatch, launches)

    with pytest.raises(_adapter().ReviewRouteError, match="app-server-user-message-lifecycle-invalid"):
        _adapter().run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)


def test_run_review_rejects_image_user_message_echo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject an image input instead of treating it as a bound text-context echo."""
    plan_path, _ = review_evidence_files(tmp_path)
    launches = _launches_for_plan(plan_path, echo_input=True)
    echo = next(frame for frame in launches[1] if frame.get("method") == "item/started")
    echo["params"]["item"]["content"] = [{"type": "image", "url": "ignored"}]
    _fake_public_processes(monkeypatch, launches)

    with pytest.raises(_adapter().ReviewRouteError, match="app-server-user-message-lifecycle-invalid"):
        _adapter().run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)


def test_run_review_rejects_user_message_echo_with_extra_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject a text echo carrying an additional content part beyond the frozen context."""
    plan_path, _ = review_evidence_files(tmp_path)
    launches = _launches_for_plan(plan_path, echo_input=True)
    echo = next(frame for frame in launches[1] if frame.get("method") == "item/started")
    echo["params"]["item"]["content"].append({"type": "text", "text": "extra"})
    _fake_public_processes(monkeypatch, launches)

    with pytest.raises(_adapter().ReviewRouteError, match="app-server-user-message-lifecycle-invalid"):
        _adapter().run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)


def test_run_review_rejects_oversized_user_message_frame(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject a serialized input echo beyond the transport bound without retaining its text."""
    plan_path, _ = review_evidence_files(tmp_path)
    launches = _launches_for_plan(plan_path, echo_input=True)
    echo = next(frame for frame in launches[1] if frame.get("method") == "item/started")
    echo["params"]["item"]["content"][0]["text"] = "\x00" * (_adapter().MAX_EVENT_BYTES // 6 + 100)
    _fake_public_processes(monkeypatch, launches)

    with pytest.raises(_adapter().ReviewRouteError, match="app-server-frame-too-large"):
        _adapter().run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)


def test_run_review_accepts_maximum_final_output_frame(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the documented 128-KiB final output limit reachable through its JSON frame envelope."""
    plan_path, _ = review_evidence_files(tmp_path)
    final = "\x00" * _adapter().MAX_OUTPUT_BYTES
    _fake_public_processes(monkeypatch, _launches_for_plan(plan_path, first_final=final))

    _adapter().run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)


def test_run_review_consumes_disabled_remote_control_status_before_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Consume the approved disabled remote-control notification in the public event loop."""
    plan_path, _ = review_evidence_files(tmp_path)
    launches = _launches_for_plan(plan_path)
    launches[1][2] = {
        "jsonrpc": "2.0",
        "method": "remoteControl/status/changed",
        "params": {"status": "disabled"},
    }
    _fake_public_processes(monkeypatch, launches)

    _adapter().run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)


def test_run_review_discards_rate_limit_notification_before_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Discard the documented rolling rate-limit update without retaining its account data."""
    plan_path, _ = review_evidence_files(tmp_path)
    launches = _launches_for_plan(plan_path)
    launches[1][2] = {
        "jsonrpc": "2.0",
        "method": "account/rateLimits/updated",
        "params": {"rateLimits": {"primary": {"usedPercent": 1}}},
    }
    _fake_public_processes(monkeypatch, launches)

    _adapter().run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)


def test_run_review_preserves_terminal_output_when_sibling_requests_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed on an approval request without deleting a prior completed response."""
    plan_path, _ = review_evidence_files(tmp_path)
    _fake_public_processes(monkeypatch, _launches_for_plan(plan_path, fail_second=True))
    output = tmp_path / "review-output"

    with pytest.raises(_adapter().ReviewRouteError, match="app-server-approval-requested"):
        _adapter().run_review(plan_path, output, Path("codex"), 10)

    assert (output / "challenger.md").read_text(encoding="utf-8") == "challenger final\n"
    assert json.loads((output / "evidence.json").read_text(encoding="utf-8"))["status"] == "failed"


def test_check_host_cleans_up_when_initialize_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Terminate the just-started host if initialization cannot produce a result."""
    plan_path, _ = review_evidence_files(tmp_path)
    factory = _fake_public_processes(monkeypatch, [[{"jsonrpc": "2.0", "id": 1, "error": {}}]])

    with pytest.raises(_adapter().ReviewRouteError, match="app-server-request-failed:initialize"):
        _adapter().check_host(plan_path, Path("codex"), 10)

    assert len(factory.processes) == 1


def test_run_review_rejects_unknown_execution_bearing_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail closed when the host emits an event outside the narrow reviewer allowlist."""
    plan_path, _ = review_evidence_files(tmp_path)
    launches = _launches_for_plan(plan_path)
    first_event = next(index for index, frame in enumerate(launches[1]) if frame.get("method") == "item/completed")
    launches[1][first_event] = {"jsonrpc": "2.0", "method": "shellCommand/executed", "params": {}}
    _fake_public_processes(monkeypatch, launches)

    with pytest.raises(_adapter().ReviewRouteError, match="app-server-event-rejected"):
        _adapter().run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)


def test_run_review_rejects_id_bearing_server_request_before_turn_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject an unsolicited request even when its method is an allowed notification."""
    plan_path, _ = review_evidence_files(tmp_path)
    launches = _launches_for_plan(plan_path)
    first_turn_response = next(index for index, frame in enumerate(launches[1]) if frame.get("id") == 5)
    launches[1].insert(
        first_turn_response,
        {"jsonrpc": "2.0", "id": 5, "method": "account/rateLimits/updated", "params": {}},
    )
    _fake_public_processes(monkeypatch, launches)

    with pytest.raises(_adapter().ReviewRouteError, match="app-server-server-request-rejected"):
        _adapter().run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)


def test_run_review_rechecks_frozen_role_bytes_before_turn_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject role-card drift after thread setup and before a paid reviewer turn starts."""
    adapter = _adapter()
    plan_path, _ = review_evidence_files(tmp_path)
    factory = _fake_public_processes(monkeypatch, _launches_for_plan(plan_path))
    read_bytes = adapter._read_bytes
    role_reads = 0

    def drift_role_card(path: Path, label: str, limit: int = adapter.MAX_FILE_BYTES) -> bytes:
        nonlocal role_reads
        if label == "role-card":
            role_reads += 1
            if role_reads > 2:
                return b"mutated role card"
        return read_bytes(path, label, limit)

    monkeypatch.setattr(adapter, "_read_bytes", drift_role_card)

    with pytest.raises(adapter.ReviewRouteError, match="role-card-mutated-before-turn"):
        adapter.run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)

    requests = [json.loads(line)["method"] for line in factory.processes[1].stdin.getvalue().splitlines()]
    assert "turn/start" not in requests


def test_terminate_kills_a_posix_group_after_parent_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Escalate from TERM to KILL using group existence, not the already-exited parent status."""
    adapter = _adapter()
    signals: list[int] = []
    cleanup_results = iter((False, True))
    monkeypatch.setattr(adapter.sys, "platform", "darwin")
    monkeypatch.setattr(adapter.os, "killpg", lambda process_id, signal: signals.append(signal), raising=False)
    monkeypatch.setattr(adapter.signal, "SIGTERM", 15, raising=False)
    monkeypatch.setattr(adapter.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(adapter, "_wait_for_posix_group_exit", lambda process, deadline: next(cleanup_results))

    adapter._terminate(_FakeProcess([]))

    assert signals == [adapter.signal.SIGTERM, adapter.signal.SIGKILL]


def test_terminate_requires_successful_windows_tree_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject a nonzero taskkill result rather than treating an exited parent as tree cleanup proof."""
    adapter = _adapter()
    monkeypatch.setattr(adapter.sys, "platform", "win32")
    monkeypatch.setattr(adapter.subprocess, "run", lambda *args, **kwargs: type("Result", (), {"returncode": 1})())

    with pytest.raises(adapter.ReviewRouteError, match="app-server-windows-tree-cleanup-unproven"):
        adapter._terminate(_FakeProcess([]))


_skip_posix_group = pytest.mark.skipif(not hasattr(os, "killpg"), reason="process-group cleanup requires POSIX killpg")


@_skip_posix_group
def test_terminate_reaps_a_real_local_child_before_group_liveness_check() -> None:
    """Cleanly reap a harmless local child so its zombie state cannot keep the group alive."""
    adapter = _adapter()
    process = adapter.subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=adapter.subprocess.DEVNULL,
        stdout=adapter.subprocess.DEVNULL,
        stderr=adapter.subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        adapter._terminate(process)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)

    assert process.poll() is not None


def _sha256(data: bytes) -> str:
    """Return the fixture digest using the adapter's immutable binding algorithm."""
    return hashlib.sha256(data).hexdigest()


def _write_json(path: Path, value: dict[str, object]) -> None:
    """Write fixture JSON with portable newline bytes."""
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def _replace_context(plan_path: Path, role_index: int, suffix: str) -> None:
    """Replace one frozen fixture context while preserving its canonical role-card prefix."""
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    node = plan["nodes"][role_index]
    role_card = (CANONICAL_ROLES / node["role_id"] / "ROLE.md").read_bytes()
    context = role_card + b"\n" + suffix.encode("utf-8")
    context_path = plan_path.parent / node["context_path"]
    context_path.write_bytes(context)
    node["context_sha256"] = _sha256(context)
    _write_json(plan_path, plan)


def review_evidence_files(
    tmp_path: Path, *, roles: tuple[str, ...] = ("challenger", "cicd-steward")
) -> tuple[Path, Path]:
    """Create a valid two-role frozen plan and bounded App Server evidence fixture."""
    contexts = tmp_path / "contexts"
    outputs = tmp_path / "outputs"
    contexts.mkdir()
    outputs.mkdir()
    nodes: list[dict[str, object]] = []
    evidence_nodes: list[dict[str, object]] = []
    for index, role_id in enumerate(roles):
        role_card = (CANONICAL_ROLES / role_id / "ROLE.md").read_bytes()
        role_text = role_card.decode("utf-8")
        model = next(line.split(": ", 1)[1] for line in role_text.splitlines() if line.startswith("model: "))
        effort = next(
            line.split(": ", 1)[1] for line in role_text.splitlines() if line.startswith("model_reasoning_effort: ")
        )
        context = role_card + b"\nReview the supplied bounded change only.\n"
        context_path = contexts / f"{role_id}.txt"
        context_path.write_bytes(context)
        output = f"{role_id} final response\n".encode()
        output_path = outputs / f"{role_id}.md"
        output_path.write_bytes(output)
        nodes.append(
            {
                "role_id": role_id,
                "model": model,
                "reasoning_effort": effort,
                "context_path": context_path.relative_to(tmp_path).as_posix(),
                "context_sha256": _sha256(context),
                "role_card_sha256": _sha256(role_card),
            }
        )
        evidence_nodes.append(
            {
                "role_id": role_id,
                "role_card_sha256": _sha256(role_card),
                "context_path": context_path.relative_to(tmp_path).as_posix(),
                "context_sha256": _sha256(context),
                "output_path": output_path.relative_to(outputs).as_posix(),
                "output_sha256": _sha256(output),
                "thread_id": f"thread-{index}",
                "turn_id": f"turn-{index}",
                "observed_controls": {
                    "sandbox": {"type": "readOnly", "networkAccess": False},
                    "approval_policy": "never",
                    "model": model,
                    "reasoning_effort": effort,
                },
                "terminal_status": "completed",
                "started_at_ms": index * 10,
                "finished_at_ms": index * 10 + 5,
            }
        )
    plan = {
        "schema_version": 1,
        "consumer_id": "code-review",
        "review_run_id": "review-17",
        "parent_thread_id": "parent-17",
        "review_input_sha256": "a" * 64,
        "task_sensitivity": "non-sensitive",
        "cwd": str(tmp_path),
        "nodes": nodes,
    }
    plan_path = tmp_path / "plan.json"
    _write_json(plan_path, plan)
    evidence = {
        "schema_version": 1,
        "route": "app-server",
        "plan_sha256": _sha256(plan_path.read_bytes()),
        "consumer_id": plan["consumer_id"],
        "review_run_id": plan["review_run_id"],
        "parent_thread_id": plan["parent_thread_id"],
        "review_input_sha256": plan["review_input_sha256"],
        "cli_version": "1",
        "status": "completed",
        "cleanup": "completed",
        "capabilities": {key: False for key in _adapter().DISABLED_CAPABILITIES},
        "nodes": evidence_nodes,
    }
    evidence_path = outputs / "evidence.json"
    _write_json(evidence_path, evidence)
    return plan_path, evidence_path


def test_validate_evidence_binds_canonical_roles_and_outputs(tmp_path: Path) -> None:
    """Return only conservative parent-consumable execution facts for valid evidence."""
    plan_path, evidence_path = review_evidence_files(tmp_path)

    summary = _adapter().validate_evidence(plan_path, evidence_path, CANONICAL_ROLES)

    assert summary == {
        "actual_mode": "independent-spawned",
        "evidence_level": "app-server-parent-observed",
        "write_parallel_eligible": False,
        "filesystem_credential_isolation": "unverified",
        "approval_policy": "never",
        "consumer_id": "code-review",
    }


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        pytest.param("output_path", "../escaped.md", "evidence-output-path-not-relative", id="output-traversal"),
        pytest.param(
            "output_path", "C:\\\\outside.md", "evidence-output-path-not-relative", id="windows-output-absolute"
        ),
        pytest.param("output_sha256", "0" * 64, "evidence-output-sha256-mismatch", id="output-digest"),
        pytest.param("thread_id", "thread-0", "evidence-thread-or-turn-id-duplicate", id="duplicate-thread"),
        pytest.param("terminal_status", "failed", "evidence-terminal-status-invalid", id="failed-turn"),
    ],
)
def test_validate_evidence_rejects_tampered_node_binding(tmp_path: Path, field: str, value: str, match: str) -> None:
    """Reject output-root escape, identity drift, and incomplete reviewer results."""
    plan_path, evidence_path = review_evidence_files(tmp_path)
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["nodes"][1][field] = value
    _write_json(evidence_path, evidence)

    with pytest.raises(_adapter().ReviewRouteError, match=match):
        _adapter().validate_evidence(plan_path, evidence_path, CANONICAL_ROLES)


def test_validate_evidence_rejects_cross_platform_duplicate_output_alias(tmp_path: Path) -> None:
    """Reject output paths that collide after Windows separator and case normalization."""
    plan_path, evidence_path = review_evidence_files(tmp_path)
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["nodes"][1]["output_path"] = "CHALLENGER.MD"
    _write_json(evidence_path, evidence)

    with pytest.raises(_adapter().ReviewRouteError, match="evidence-output-path-duplicate"):
        _adapter().validate_evidence(plan_path, evidence_path, CANONICAL_ROLES)


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        pytest.param(b" \n\t", "evidence-output-empty-or-secret", id="whitespace-only"),
        pytest.param(b"\xff", "evidence-output-invalid-utf8", id="invalid-utf8"),
    ],
)
def test_validate_evidence_rejects_nonfinal_output_bytes(tmp_path: Path, payload: bytes, match: str) -> None:
    """Reject whitespace-only and non-UTF-8 final response files before promotion."""
    plan_path, evidence_path = review_evidence_files(tmp_path)
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    output = evidence_path.parent / evidence["nodes"][0]["output_path"]
    output.write_bytes(payload)
    evidence["nodes"][0]["output_sha256"] = _sha256(payload)
    _write_json(evidence_path, evidence)

    with pytest.raises(_adapter().ReviewRouteError, match=match):
        _adapter().validate_evidence(plan_path, evidence_path, CANONICAL_ROLES)


def test_validate_evidence_rejects_nonliteral_sandbox_observation(tmp_path: Path) -> None:
    """Require the host-observed read-only object rather than a weaker boolean shorthand."""
    plan_path, evidence_path = review_evidence_files(tmp_path)
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["nodes"][0]["observed_controls"]["sandbox"] = {"readOnly": True, "networkAccess": False}
    _write_json(evidence_path, evidence)

    with pytest.raises(_adapter().ReviewRouteError, match="evidence-observed-controls-mismatch"):
        _adapter().validate_evidence(plan_path, evidence_path, CANONICAL_ROLES)


def test_validate_evidence_reports_parallel_only_for_overlapping_intervals(tmp_path: Path) -> None:
    """Derive parallelism from reviewer work intervals, never dispatch ordering."""
    plan_path, evidence_path = review_evidence_files(tmp_path)
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["nodes"][1]["started_at_ms"] = 4
    _write_json(evidence_path, evidence)

    assert _adapter().validate_evidence(plan_path, evidence_path, CANONICAL_ROLES)["actual_mode"] == "parallel"


def test_validate_evidence_does_not_promote_zero_duration_or_negative_intervals(tmp_path: Path) -> None:
    """Keep zero-duration evidence independent and reject impossible monotonic timestamps."""
    plan_path, evidence_path = review_evidence_files(tmp_path)
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["nodes"][0].update(started_at_ms=5, finished_at_ms=5)
    evidence["nodes"][1].update(started_at_ms=4, finished_at_ms=8)
    _write_json(evidence_path, evidence)

    assert (
        _adapter().validate_evidence(plan_path, evidence_path, CANONICAL_ROLES)["actual_mode"] == "independent-spawned"
    )
    evidence["nodes"][0]["started_at_ms"] = -1
    _write_json(evidence_path, evidence)
    with pytest.raises(_adapter().ReviewRouteError, match="evidence-substantive-interval-invalid"):
        _adapter().validate_evidence(plan_path, evidence_path, CANONICAL_ROLES)
