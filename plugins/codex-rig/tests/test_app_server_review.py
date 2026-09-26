"""Exercise the bounded App Server review evidence adapter."""

from __future__ import annotations

import importlib.util
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType

import pytest
from _platform import DIRECTORY_SYMLINKS_AVAILABLE


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


def test_code_review_clean_output_requires_scoped_assessment() -> None:
    """An empty findings list must still carry the reviewer's rating and rationale."""
    adapter = _adapter()
    plan = {"consumer_id": "code-review", "source_sha256": "a" * 64, "review_input_sha256": "b" * 64}
    response = {
        "source_sha256": plan["source_sha256"],
        "diff_sha256": plan["review_input_sha256"],
        "findings": [],
        "assessment": {"rating": 1, "rationale": "The inspected scope has no open findings."},
    }

    assert "assessment" in adapter._review_output_schema(plan)["required"]
    adapter._validate_review_output(json.dumps(response), plan)
    del response["assessment"]
    with pytest.raises(adapter.ReviewRouteError, match="app-server-review-output-schema-mismatch"):
        adapter._validate_review_output(json.dumps(response), plan)


@pytest.mark.parametrize(
    ("assessment", "error"),
    [
        pytest.param({"rating": True, "rationale": "Evidence."}, "assessment-invalid", id="boolean-rating"),
        pytest.param({"rating": 2, "rationale": "  "}, "assessment-invalid", id="blank-rationale"),
        pytest.param({"rating": 6, "rationale": "Evidence."}, "assessment-invalid", id="out-of-range"),
    ],
)
def test_code_review_rejects_invalid_scoped_assessment(assessment: dict[str, object], error: str) -> None:
    """A present but unsupported judgment must fail before output retention."""
    adapter = _adapter()
    plan = {"consumer_id": "code-review", "source_sha256": "a" * 64, "review_input_sha256": "b" * 64}
    response = {
        "source_sha256": plan["source_sha256"],
        "diff_sha256": plan["review_input_sha256"],
        "findings": [],
        "assessment": assessment,
    }

    with pytest.raises(adapter.ReviewRouteError, match=error):
        adapter._validate_review_output(json.dumps(response), plan)


def test_other_review_consumer_retains_findings_only_output() -> None:
    """The Code Review assessment field does not change other review routes."""
    adapter = _adapter()
    plan = {"consumer_id": "challenge-resolve", "source_sha256": "a" * 64, "review_input_sha256": "b" * 64}
    response = {"source_sha256": plan["source_sha256"], "diff_sha256": plan["review_input_sha256"], "findings": []}

    assert adapter._review_output_schema(plan)["required"] == ["source_sha256", "diff_sha256", "findings"]
    adapter._validate_review_output(json.dumps(response), plan)


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


def test_active_review_has_independent_lifecycle_defaults_and_rejects_typos() -> None:
    """Keep the private fixed-shape reviewer state explicit and independently mutable."""
    adapter = _adapter()
    first = adapter._ActiveReview(node={}, thread_id="thread-1", controls={})
    second = adapter._ActiveReview(node={}, thread_id="thread-2", controls={})

    assert first.turn_id is None
    assert first.started_at_ms is None
    assert first.finished_at_ms is None
    assert first.final is None
    assert first.input_echo_item_id is None
    assert first.input_echo_completed is False
    assert first.output_path is None
    first.turn_id = "turn-1"
    first.input_echo_completed = True
    assert second.turn_id is None
    assert second.input_echo_completed is False
    with pytest.raises(AttributeError):
        first.input_echo_compeleted = True
    assert first.input_echo_completed is True
    with pytest.raises(TypeError):
        adapter._ActiveReview({}, "thread-3", {})


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


def _thread_result(role_index: int, model: str, effort: str) -> dict[str, object]:
    """Return the observed no-turn thread control response for one role."""
    return {
        "thread": {"id": f"thread-{role_index}"},
        "sandbox": {"type": "readOnly", "networkAccess": False},
        "approvalPolicy": "never",
        "model": model,
        "reasoningEffort": effort,
    }


def _review_answer(plan_path: Path) -> str:
    """Build a raw no-findings review bound to the fixture's exact source and diff."""
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    return json.dumps(
        {
            "source_sha256": plan["source_sha256"],
            "diff_sha256": plan["review_input_sha256"],
            "findings": [],
            "assessment": {"rating": 1, "rationale": "No open findings in the inspected scope."},
        }
    )


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
        second.append(_response(index, _thread_result(index - 3, node["model"], node["reasoning_effort"])))
    request_id = 3 + len(plan["nodes"])
    for node in plan["nodes"]:
        context = (plan_path.parent / node["context_path"]).read_text(encoding="utf-8")
        if len(context) > 1_048_576:
            second.append(_response(request_id, {}))
            request_id += 1
    for index, node in enumerate(plan["nodes"]):
        context_path = plan_path.parent / node["context_path"]
        context = context_path.read_text(encoding="utf-8")
        if len(context) > 1_048_576:
            context = (
                "Review the complete frozen context in the preceding user message. Follow its role and output contract."
            )
        if echo_input:
            item = {
                "id": f"input-{index}",
                "type": "userMessage",
                "content": [{"type": "text", "text": context}],
            }
            second.append(
                {
                    "jsonrpc": "2.0",
                    "method": "item/started",
                    "params": {
                        "threadId": f"thread-{index}",
                        "turnId": f"turn-{index}",
                        "item": item,
                    },
                }
            )
            input_completions.append(
                {
                    "jsonrpc": "2.0",
                    "method": "item/completed",
                    "params": {
                        "threadId": f"thread-{index}",
                        "turnId": f"turn-{index}",
                        "item": {
                            "id": f"input-{index}",
                            "type": "userMessage",
                            "content": [{"type": "text", "text": context}],
                        },
                    },
                }
            )
        second.append(_response(request_id, {"turn": {"id": f"turn-{index}"}}))
        request_id += 1
    second.extend(input_completions)
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
                        "text": first_final if first_final is not None else _review_answer(plan_path),
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
                        "item": {"type": "agentMessage", "phase": "final_answer", "text": _review_answer(plan_path)},
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
    # Protocol tests supply synthetic source records; source-binding tests restore
    # the real checkout boundary and exercise it against a local Git repository.
    monkeypatch.setattr(adapter, "_live_source_unchanged", lambda *args: None)
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


def _fake_host_with_git(monkeypatch: pytest.MonkeyPatch, launches: list[list[dict[str, object]]]) -> _PopenFactory:
    """Mock only Codex host calls while keeping live Git source recapture executable."""
    real_run = subprocess.run
    real_popen = subprocess.Popen
    live_check = _adapter()._live_source_unchanged
    factory = _fake_public_processes(monkeypatch, launches)
    monkeypatch.setattr(_adapter(), "_live_source_unchanged", live_check)
    codex_run = subprocess.run

    def run(command: object, *args: object, **kwargs: object) -> subprocess.CompletedProcess[object]:
        """Keep Git as a real local source boundary and stub the Codex CLI version call."""
        if isinstance(command, list) and command and command[0] == "git":
            return real_run(command, *args, **kwargs)
        return codex_run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)

    def popen(command: object, *args: object, **kwargs: object) -> subprocess.Popen[object] | _FakeProcess:
        """Use the real process boundary for local Git and the host fake for Codex."""
        if isinstance(command, list) and command and command[0] == "git":
            return real_popen(command, *args, **kwargs)
        return factory(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", popen)
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
    assert all(
        request["params"]["config"] == {"model_reasoning_effort": "high"}
        for request in request_payloads
        if request["method"] == "thread/start"
    )


def test_context_frame_queue_preserves_the_prior_aggregate_resource_envelope() -> None:
    """Scale both decoded-frame buffers down when enlarged contexts raise the per-frame ceiling."""
    adapter = _adapter()

    assert adapter.MAX_EVENTS == 512
    assert adapter.MAX_BUFFERED_EVENTS < adapter.MAX_EVENTS
    assert adapter.MAX_BUFFERED_EVENTS * adapter.MAX_EVENT_BYTES <= adapter.MAX_BUFFERED_EVENT_BYTES
    assert adapter._JsonRpcStdio(_FakeProcess([]), 1.0)._messages.maxsize == adapter.MAX_BUFFERED_EVENTS


def test_request_rejects_pending_frames_past_scaled_buffer_capacity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail closed before unmatched responses can retain the enlarged frame ceiling indefinitely."""
    adapter = _adapter()
    client = adapter._JsonRpcStdio(_FakeProcess([]), 1.0)
    frames = iter(_response(2, {}) for _ in range(adapter.MAX_BUFFERED_EVENTS + 1))
    monkeypatch.setattr(client, "_read", lambda: next(frames))

    with pytest.raises(adapter.ReviewRouteError, match="app-server-pending-events-overflow"):
        client.request("initialize", {})

    assert len(client._pending) == adapter.MAX_BUFFERED_EVENTS


def test_check_host_accepts_exact_two_mebibyte_unicode_control_context_before_launching_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Accept a full context above the metadata limit and preserve every sent character exactly."""
    adapter = _adapter()
    plan_path, _ = review_evidence_files(tmp_path)
    expected = _replace_context_to_size(plan_path, 0, adapter.MAX_CONTEXT_BYTES)
    check_launches = _launches_for_plan(plan_path)
    check_launches[1] = check_launches[1][:6]
    _fake_public_processes(monkeypatch, check_launches)

    assert adapter.check_host(plan_path, Path("codex"), 10)["model_invoked"] is False

    factory = _fake_public_processes(monkeypatch, _launches_for_plan(plan_path, echo_input=True))

    adapter.run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)

    assert len(expected.encode("utf-8")) == adapter.MAX_CONTEXT_BYTES
    assert len(expected.encode("utf-8")) > adapter.MAX_FILE_BYTES
    assert "😀" in expected and "\x00" in expected
    requests = [json.loads(line) for line in factory.processes[1].stdin.getvalue().splitlines()]
    first_turn = next(request for request in requests if request["method"] == "turn/start")
    preload = next(request for request in requests if request["method"] == "thread/inject_items")
    assert preload["params"]["items"] == [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": expected}]}
    ]
    assert first_turn["params"]["input"] == [{"type": "text", "text": adapter.HISTORY_REVIEW_PROMPT}]


@pytest.mark.parametrize("overage", [0, 1])
def test_context_delivery_respects_character_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overage: int
) -> None:
    """Preserve direct input at the CLI ceiling and preload the complete context above it."""
    plan_path, _ = review_evidence_files(tmp_path)
    plan = json.loads(plan_path.read_bytes())
    prefix = (plan_path.parent / plan["nodes"][0]["context_path"]).read_text(encoding="utf-8")
    _replace_context_bytes(plan_path, 0, b"x" * (1_048_576 + overage - len(prefix)))
    plan = json.loads(plan_path.read_bytes())
    expected = (plan_path.parent / plan["nodes"][0]["context_path"]).read_text(encoding="utf-8")
    assert len(expected) == 1_048_576 + overage
    factory = _fake_public_processes(monkeypatch, _launches_for_plan(plan_path, echo_input=True))

    evidence_path = _adapter().run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)

    requests = [json.loads(line) for line in factory.processes[1].stdin.getvalue().splitlines()]
    turns = [request for request in requests if request["method"] == "turn/start"]
    preloads = [request for request in requests if request["method"] == "thread/inject_items"]
    evidence = json.loads(evidence_path.read_bytes())
    if overage:
        assert len(preloads) == 1
        assert preloads[0]["params"] == {
            "threadId": "thread-0",
            "items": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": expected}]}],
        }
        assert requests.index(preloads[0]) < requests.index(turns[0])
        assert evidence["nodes"][0]["context_delivery"] == {
            "method": "thread/inject_items",
            "context_sha256": plan["nodes"][0]["context_sha256"],
            "acknowledged": True,
        }
        assert len(turns[0]["params"]["input"][0]["text"]) < 1_048_576
    else:
        assert preloads == []
        assert turns[0]["params"]["input"] == [{"type": "text", "text": expected}]
        assert "context_delivery" not in evidence["nodes"][0]


def test_failed_later_preload_prevents_every_turn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject unsupported history loading for any reviewer before paid turns can begin."""
    plan_path, _ = review_evidence_files(tmp_path)
    _replace_context_bytes(plan_path, 1, b"x" * 1_048_576)
    launches = _launches_for_plan(plan_path)
    launches[1] = launches[1][:5] + [{"id": 5, "error": {"code": -32601, "message": "Method not found"}}]
    factory = _fake_public_processes(monkeypatch, launches)

    with pytest.raises(_adapter().ReviewRouteError, match="app-server-request-failed:thread/inject_items"):
        _adapter().run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)

    requests = [json.loads(line)["method"] for line in factory.processes[1].stdin.getvalue().splitlines()]
    assert "turn/start" not in requests
    evidence = json.loads((tmp_path / "review-output/evidence.json").read_bytes())
    assert evidence["turn_dispatch"] == {"attempted": 0, "acknowledged": 0}
    assert evidence["failure_phase"] == "context-preload"
    assert evidence["failure_diagnostic"]["reason"] == "method-not-supported"


@pytest.mark.parametrize("tamper", ["missing", "digest", "method", "false", "integer", "extra"])
def test_large_context_evidence_requires_exact_preload_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    """Reject large-context evidence lacking a precise acknowledged same-context delivery record."""
    plan_path, _ = review_evidence_files(tmp_path)
    _replace_context_bytes(plan_path, 0, b"x" * 1_048_576)
    _fake_public_processes(monkeypatch, _launches_for_plan(plan_path, echo_input=True))
    evidence_path = _adapter().run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)
    evidence = json.loads(evidence_path.read_bytes())
    delivery = evidence["nodes"][0]["context_delivery"]
    if tamper == "missing":
        del evidence["nodes"][0]["context_delivery"]
    elif tamper == "digest":
        delivery["context_sha256"] = "0" * 64
    elif tamper == "method":
        delivery["method"] = "turn/start"
    elif tamper == "false":
        delivery["acknowledged"] = False
    elif tamper == "integer":
        delivery["acknowledged"] = 1
    else:
        delivery["unverified"] = True
    _write_json(evidence_path, evidence)

    with pytest.raises(_adapter().ReviewRouteError, match="evidence-context-delivery-mismatch"):
        _adapter().validate_evidence(plan_path, evidence_path, CANONICAL_ROLES)


def test_check_host_rejects_context_larger_than_two_mebibytes_before_process_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject a one-byte context overage before spawning an App Server process."""
    adapter = _adapter()
    plan_path, _ = review_evidence_files(tmp_path)
    _replace_context_to_size(plan_path, 0, adapter.MAX_CONTEXT_BYTES + 1)
    factory = _fake_public_processes(monkeypatch, _launches_for_plan(plan_path))

    with pytest.raises(adapter.ReviewRouteError, match="plan-context-too-large"):
        adapter.check_host(plan_path, Path("codex"), 10)

    assert factory.processes == []


def test_check_host_rejects_invalid_utf8_context_before_process_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject malformed context text distinctly before any App Server process starts."""
    plan_path, _ = review_evidence_files(tmp_path)
    _replace_context_bytes(plan_path, 0, b"\xff")
    factory = _fake_public_processes(monkeypatch, [])

    with pytest.raises(_adapter().ReviewRouteError, match="plan-context-invalid-utf8"):
        _adapter().check_host(plan_path, Path("codex"), 10)

    assert factory.processes == []


@pytest.mark.parametrize(
    ("tamper", "match"),
    [
        pytest.param("missing-source", "plan-source-path-invalid", id="missing-source-binding"),
        pytest.param("partial-source", "plan-context-source-or-diff-incomplete", id="partial-source-context"),
        pytest.param("changed-source", "plan-source-sha256-mismatch", id="stale-source-snapshot"),
        pytest.param("invalid-source-snapshot", "plan-source-snapshot-invalid", id="invalid-source-snapshot"),
        pytest.param("stale-receipt", "plan-capacity-receipt-binding-invalid", id="stale-capacity-source-binding"),
        pytest.param(
            "missing-capacity-evidence", "plan-capacity-receipt-fields-invalid", id="missing-capacity-evidence"
        ),
        pytest.param("malformed-capacity-evidence", "plan-capacity-evidence-invalid", id="malformed-capacity-evidence"),
        pytest.param("boolean-receipt", "plan-capacity-receipt-types-invalid", id="boolean-capacity-value"),
        pytest.param("zero-instruction-reserve", "plan-capacity-receipt-values-invalid", id="zero-instruction-reserve"),
        pytest.param(
            "insufficient-receipt", "plan-capacity-admission-insufficient", id="insufficient-capacity-headroom"
        ),
        pytest.param("selected-window-percent", "plan-capacity-admission-insufficient", id="requested-window-percent"),
        pytest.param(
            "default-window-percent", "plan-capacity-admission-insufficient", id="observed-default-window-percent"
        ),
    ],
)
def test_check_host_rejects_unbound_source_or_capacity_receipt_before_process_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str, match: str
) -> None:
    """Reject incomplete source snapshots and unauditable capacity claims before host startup."""
    plan_path, _ = review_evidence_files(tmp_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    node = plan["nodes"][0]
    if tamper == "missing-source":
        del plan["source_path"]
    elif tamper == "partial-source":
        context_path = plan_path.parent / node["context_path"]
        source = (plan_path.parent / plan["source_path"]).read_bytes()
        context = context_path.read_bytes().replace(source, b"", 1)
        context_path.write_bytes(context)
        node["context_sha256"] = _sha256(context)
        node["capacity_receipt"]["context_sha256"] = node["context_sha256"]
    elif tamper == "changed-source":
        (plan_path.parent / plan["source_path"]).write_bytes(b'{"files":[]}')
    elif tamper == "invalid-source-snapshot":
        source_path = plan_path.parent / plan["source_path"]
        source = json.loads(source_path.read_text(encoding="utf-8"))
        source["files"][0]["content"] = "tampered source"
        _write_json(source_path, source)
        plan["source_sha256"] = _sha256(source_path.read_bytes())
        node["capacity_receipt"]["source_sha256"] = plan["source_sha256"]
    elif tamper == "stale-receipt":
        node["capacity_receipt"]["source_sha256"] = "0" * 64
    elif tamper == "missing-capacity-evidence":
        del node["capacity_receipt"]["capacity_evidence_path"]
    elif tamper == "malformed-capacity-evidence":
        capacity_path = plan_path.parent / node["capacity_receipt"]["capacity_evidence_path"]
        capacity_path.write_bytes(b"{}")
        node["capacity_receipt"]["capacity_evidence_sha256"] = _sha256(b"{}")
    elif tamper == "boolean-receipt":
        node["capacity_receipt"]["input_tokens"] = True
    elif tamper == "zero-instruction-reserve":
        node["capacity_receipt"]["instruction_reserve_tokens"] = 0
    elif tamper in {"selected-window-percent", "default-window-percent"}:
        _replace_context_to_size(plan_path, 0, 375_000 if tamper == "selected-window-percent" else 200_000)
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        node = plan["nodes"][0]
        capacity = {
            "schema_version": 1,
            "model": node["model"],
            "observed_supported_capacity_tokens": 872_000,
            "observed_default_context_window": 272_000,
        }
        capacity_path = plan_path.parent / node["capacity_receipt"]["capacity_evidence_path"]
        _write_json(capacity_path, capacity)
        node["capacity_receipt"].update(
            input_tokens=375_000 if tamper == "selected-window-percent" else 200_000,
            instruction_reserve_tokens=100_000 if tamper == "selected-window-percent" else 60_000,
            output_reserve_tokens=1_000 if tamper == "selected-window-percent" else 20_000,
            supported_capacity_tokens=872_000,
            default_context_window=272_000,
            effective_window_percent=95,
            capacity_evidence_sha256=_sha256(capacity_path.read_bytes()),
        )
        if tamper == "selected-window-percent":
            node["model_context_window"] = 500_000
    else:
        node["capacity_receipt"].update(
            supported_capacity_tokens=299,
            default_context_window=299,
            effective_window_percent=100,
        )
        capacity_path = plan_path.parent / node["capacity_receipt"]["capacity_evidence_path"]
        _write_json(
            capacity_path,
            {
                "schema_version": 1,
                "model": node["model"],
                "observed_supported_capacity_tokens": 299,
                "observed_default_context_window": 299,
            },
        )
        node["capacity_receipt"]["capacity_evidence_sha256"] = _sha256(capacity_path.read_bytes())
    _write_json(plan_path, plan)
    factory = _fake_public_processes(monkeypatch, [])

    with pytest.raises(_adapter().ReviewRouteError, match=match):
        _adapter().check_host(plan_path, Path("codex"), 10)

    assert factory.processes == []


@pytest.mark.parametrize("tamper", ["understated", "overstated", "unknown-method", "unicode-character-count"])
def test_check_host_recomputes_capacity_input_before_process_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    """Reject forged measurements even when exact context/source digests are correct."""
    plan_path, _ = review_evidence_files(tmp_path)
    _replace_context(plan_path, 0, "café 😀")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    node = plan["nodes"][0]
    context = (plan_path.parent / node["context_path"]).read_bytes()
    node["capacity_receipt"].update(tokenizer="utf8-byte-upper-bound", input_tokens=len(context))
    if tamper == "unknown-method":
        node["capacity_receipt"]["tokenizer"] = "operator-proxy"
    elif tamper == "unicode-character-count":
        node["capacity_receipt"]["input_tokens"] = len(context.decode("utf-8"))
    else:
        node["capacity_receipt"]["input_tokens"] += -1 if tamper == "understated" else 1
    _write_json(plan_path, plan)
    factory = _fake_public_processes(monkeypatch, [])

    with pytest.raises(_adapter().ReviewRouteError, match="plan-capacity-input-measurement-invalid"):
        _adapter().check_host(plan_path, Path("codex"), 10)

    assert factory.processes == []


@pytest.mark.parametrize(
    "tamper",
    [
        "empty",
        "missing-scope",
        "extra-record",
        "substituted-record",
        "directory-scope",
        "forged-directory-file",
        "forged-directory-slash",
    ],
)
def test_source_inventory_must_match_explicit_file_scopes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    """Reject incomplete inventories before host launch and when accepting retained review evidence."""
    plan_path, evidence_path = review_evidence_files(tmp_path)
    plan = json.loads(plan_path.read_bytes())
    source_path = plan_path.parent / plan["source_path"]
    source = json.loads(source_path.read_bytes())
    if tamper == "empty":
        source["files"] = []
    elif tamper == "missing-scope":
        source["scope_paths"] = ["missing.py", "widget.py"]
    elif tamper == "extra-record":
        source["files"].insert(0, dict(source["files"][0], path="extra.py"))
    elif tamper == "substituted-record":
        source["files"][0]["path"] = "other.py"
    elif tamper == "forged-directory-file":
        source["scope_paths"] = ["."]
        source["files"][0]["path"] = "."
    elif tamper == "forged-directory-slash":
        source["scope_paths"] = ["package/"]
        source["files"][0]["path"] = "package/"
    else:
        source["scope_paths"] = ["."]
    _write_json(source_path, source)
    plan["source_sha256"] = _sha256(source_path.read_bytes())
    for node in plan["nodes"]:
        node["capacity_receipt"]["source_sha256"] = plan["source_sha256"]
    _write_json(plan_path, plan)
    for index in range(len(plan["nodes"])):
        _replace_context(plan_path, index, "")
    factory = _fake_public_processes(monkeypatch, [])

    expected_error = (
        "plan-source-snapshot-invalid"
        if tamper in {"directory-scope", "forged-directory-file", "forged-directory-slash"}
        else "plan-source-scope-coverage-invalid"
    )
    with pytest.raises(_adapter().ReviewRouteError, match=expected_error):
        _adapter().check_host(plan_path, Path("codex"), 10)
    with pytest.raises(_adapter().ReviewRouteError, match=expected_error):
        _adapter().validate_evidence(plan_path, evidence_path, CANONICAL_ROLES)

    assert factory.processes == []


def test_dispatch_rejects_existing_directory_claimed_as_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A forged file record cannot turn a real directory into a leaf scope."""
    plan_path, _ = review_evidence_files(tmp_path)
    repository = tmp_path / "repository"
    (repository / "package").mkdir(parents=True)
    plan = json.loads(plan_path.read_bytes())
    source_path = plan_path.parent / plan["source_path"]
    source = json.loads(source_path.read_bytes())
    source["repository"] = repository.as_posix()
    source["scope_paths"] = ["package"]
    source["files"][0]["path"] = "package"
    _write_json(source_path, source)
    plan["source_sha256"] = _sha256(source_path.read_bytes())
    for node in plan["nodes"]:
        node["capacity_receipt"]["source_sha256"] = plan["source_sha256"]
    _write_json(plan_path, plan)
    for index in range(len(plan["nodes"])):
        _replace_context(plan_path, index, "")
    factory = _fake_public_processes(monkeypatch, [])

    with pytest.raises(_adapter().ReviewRouteError, match="plan-source-snapshot-invalid"):
        _adapter().check_host(plan_path, Path("codex"), 10)
    assert factory.processes == []


@pytest.mark.skipif(not DIRECTORY_SYMLINKS_AVAILABLE, reason="filesystem cannot create directory symlinks")
def test_dispatch_accepts_tracked_symlink_to_in_repository_directory(tmp_path: Path) -> None:
    """Keep a tracked symlink leaf valid when its directory target stays inside the checkout."""
    plan_path, repository = _live_source_plan(tmp_path)
    (repository / "package").mkdir()
    (repository / "directory-link").symlink_to("package", target_is_directory=True)
    subprocess.run(["git", "add", "directory-link"], cwd=repository, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "add link"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    collector_path = PLUGIN_ROOT / "shared" / "collect_diff.py"
    spec = importlib.util.spec_from_file_location("collect_diff_symlink_test", collector_path)
    assert spec is not None and spec.loader is not None
    collector = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(collector)
    source = collector.capture_source_snapshot(repository, ["directory-link"])
    assert source["files"][0]["kind"] == "symlink"
    _bind_source_snapshot(plan_path, source)

    validated, _ = _adapter()._validated_plan(plan_path, CANONICAL_ROLES, require_dispatch=True)

    assert validated["_source_bytes"] == (plan_path.parent / validated["source_path"]).read_bytes()


@pytest.mark.parametrize("tamper", ["forged-snapshot", "stale-live-file"])
def test_dispatch_rejects_snapshot_that_differs_from_live_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    """Reject internally consistent frozen bytes that do not describe the review checkout."""
    plan_path, repository = _live_source_plan(tmp_path)
    _adapter()._validated_plan(plan_path, CANONICAL_ROLES, require_dispatch=True)
    if tamper == "forged-snapshot":
        plan = json.loads(plan_path.read_bytes())
        source = json.loads((plan_path.parent / plan["source_path"]).read_bytes())
        source["files"][0]["content"] = "VALUE = 2\n"
        source["files"][0]["sha256"] = _sha256(b"VALUE = 2\n")
        _bind_source_snapshot(plan_path, source)
    else:
        (repository / "widget.py").write_text("VALUE = 2\n", encoding="utf-8", newline="\n")
    launches = _launches_for_plan(plan_path)
    launches[1] = launches[1][:5]
    factory = _fake_host_with_git(monkeypatch, launches)

    with pytest.raises(_adapter().ReviewRouteError):
        _adapter().check_host(plan_path, Path("codex"), 10)

    assert factory.processes == []


def test_dispatch_rejects_snapshot_from_another_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The snapshot repository must be the checkout selected for reviewer turns."""
    plan_path, repository = _live_source_plan(tmp_path)
    decoy = _committed_source_repository(tmp_path / "decoy")
    plan = json.loads(plan_path.read_bytes())
    source = json.loads((plan_path.parent / plan["source_path"]).read_bytes())
    source["repository"] = decoy.as_posix()
    source["revision"] = (
        subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=decoy, check=True, capture_output=True)
        .stdout.decode("ascii")
        .strip()
    )
    source["index_sha256"] = _sha256(
        subprocess.run(
            ["git", "ls-files", "--stage", "-z", "--", "widget.py"], cwd=decoy, check=True, capture_output=True
        ).stdout
    )
    _bind_source_snapshot(plan_path, source)
    assert plan["cwd"] == str(repository)
    launches = _launches_for_plan(plan_path)
    launches[1] = launches[1][:5]
    factory = _fake_host_with_git(monkeypatch, launches)

    with pytest.raises(_adapter().ReviewRouteError):
        _adapter().check_host(plan_path, Path("codex"), 10)

    assert factory.processes == []


def test_dispatch_rechecks_live_source_after_host_setup_before_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A checkout change during host setup must stop every paid reviewer turn."""
    plan_path, repository = _live_source_plan(tmp_path)
    _adapter()._validated_plan(plan_path, CANONICAL_ROLES, require_dispatch=True)
    factory = _fake_host_with_git(monkeypatch, _launches_for_plan(plan_path))
    launch = subprocess.Popen

    def change_source_on_host_launch(*args: object, **kwargs: object) -> _FakeProcess:
        """Change only the live leaf after the second external host process starts."""
        process = launch(*args, **kwargs)
        if len(factory.processes) == 2:
            (repository / "widget.py").write_text("VALUE = 2\n", encoding="utf-8", newline="\n")
        return process

    monkeypatch.setattr(subprocess, "Popen", change_source_on_host_launch)

    with pytest.raises(_adapter().ReviewRouteError):
        _adapter().run_review(plan_path, tmp_path / "live-source-review", Path("codex"), 10)

    requests = [json.loads(line)["method"] for line in factory.processes[1].stdin.getvalue().splitlines()]
    assert "turn/start" not in requests


def test_dispatch_rechecks_live_source_after_reviewer_turn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A file changed during review cannot produce accepted completion evidence."""
    plan_path, repository = _live_source_plan(tmp_path)
    factory = _fake_host_with_git(monkeypatch, _launches_for_plan(plan_path))
    launch = subprocess.Popen
    changed = False

    class SourceChangingStdin(io.StringIO):
        """Change the live checkout when the fake host receives its first reviewer turn."""

        def write(self, data: str) -> int:
            """Preserve the request transcript and change only the first turn's source."""
            nonlocal changed
            count = super().write(data)
            if not changed and '"method":"turn/start"' in data:
                (repository / "widget.py").write_text("VALUE = 2\n", encoding="utf-8", newline="\n")
                changed = True
            return count

    def launch_with_source_change(*args: object, **kwargs: object) -> _FakeProcess:
        """Attach the source-changing stdin to the reviewer host process only."""
        process = launch(*args, **kwargs)
        if len(factory.processes) == 2:
            process.stdin = SourceChangingStdin()
        return process

    monkeypatch.setattr(subprocess, "Popen", launch_with_source_change)

    with pytest.raises(_adapter().ReviewRouteError):
        _adapter().run_review(plan_path, tmp_path / "post-turn-source-review", Path("codex"), 10)

    assert changed
    requests = [json.loads(line)["method"] for line in factory.processes[1].stdin.getvalue().splitlines()]
    assert "turn/start" in requests


@pytest.mark.parametrize("suffix", ["ASCII", "café 😀"])
@pytest.mark.parametrize("headroom", [0, -1])
def test_capacity_byte_bound_obeys_exact_selected_window(tmp_path: Path, suffix: str, headroom: int) -> None:
    """Admit the recomputed byte bound plus reserves exactly, never one token beyond it."""
    plan_path, _ = review_evidence_files(tmp_path)
    _replace_context(plan_path, 0, suffix)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    node = plan["nodes"][0]
    receipt = node["capacity_receipt"]
    measured = len((plan_path.parent / node["context_path"]).read_bytes())
    receipt.update(tokenizer="utf8-byte-upper-bound", input_tokens=measured)
    node["model_context_window"] = (
        measured + receipt["instruction_reserve_tokens"] + receipt["output_reserve_tokens"] + headroom
    )
    _write_json(plan_path, plan)

    if headroom < 0:
        with pytest.raises(_adapter().ReviewRouteError, match="plan-capacity-admission-insufficient"):
            _adapter()._validated_plan(plan_path, CANONICAL_ROLES)
    else:
        _adapter()._validated_plan(plan_path, CANONICAL_ROLES)


def test_direct_evidence_validation_keeps_legacy_plan_readable_but_dispatch_rejects_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preserve historical evidence inspection while preventing legacy plans from launching new work."""
    plan_path, evidence_path = review_evidence_files(tmp_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["schema_version"] = 1
    for key in ("source_path", "source_sha256", "diff_path", "diff_sha256"):
        del plan[key]
    for node in plan["nodes"]:
        del node["capacity_receipt"]
    _write_json(plan_path, plan)
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["plan_sha256"] = _sha256(plan_path.read_bytes())
    _write_json(evidence_path, evidence)

    assert _adapter().validate_evidence(plan_path, evidence_path, CANONICAL_ROLES)["consumer_id"] == "code-review"
    factory = _fake_public_processes(monkeypatch, [])

    with pytest.raises(_adapter().ReviewRouteError, match="plan-legacy-dispatch-forbidden"):
        _adapter().check_host(plan_path, Path("codex"), 10)

    assert factory.processes == []


@pytest.mark.parametrize("value", [True, 0, -1, "500000", 1.5])
def test_check_host_rejects_invalid_optional_model_context_window_before_process_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: object
) -> None:
    """Allow only positive integer context-window requests in a frozen reviewer node."""
    plan_path, _ = review_evidence_files(tmp_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["nodes"][0]["model_context_window"] = value
    _write_json(plan_path, plan)
    factory = _fake_public_processes(monkeypatch, _launches_for_plan(plan_path))

    with pytest.raises(_adapter().ReviewRouteError, match="plan-model-context-window-invalid"):
        _adapter().check_host(plan_path, Path("codex"), 10)

    assert factory.processes == []


def test_optional_model_context_window_is_forwarded_only_in_thread_start_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Forward an optional request without changing the observed model metadata contract."""
    plan_path, _ = review_evidence_files(tmp_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["nodes"][0]["model_context_window"] = 500_000
    _write_json(plan_path, plan)
    check_launches = _launches_for_plan(plan_path)
    check_launches[1] = check_launches[1][:5]
    check_factory = _fake_public_processes(monkeypatch, check_launches)

    summary = _adapter().check_host(plan_path, Path("codex"), 10)

    check_requests = [json.loads(line) for line in check_factory.processes[1].stdin.getvalue().splitlines()]
    check_thread = next(request for request in check_requests if request["method"] == "thread/start")
    assert check_thread["params"]["config"] == {"model_reasoning_effort": "high", "model_context_window": 500_000}
    assert summary["nodes"][0]["observed_controls"]["model"] == plan["nodes"][0]["model"]

    run_factory = _fake_public_processes(monkeypatch, _launches_for_plan(plan_path))
    _adapter().run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)

    run_requests = [json.loads(line) for line in run_factory.processes[1].stdin.getvalue().splitlines()]
    run_thread = next(request for request in run_requests if request["method"] == "thread/start")
    assert run_thread["params"]["config"] == {"model_reasoning_effort": "high", "model_context_window": 500_000}
    assert (
        "model_context_window"
        not in next(request for request in run_requests if request["method"] == "turn/start")["params"]
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


def test_check_host_discards_account_update_after_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ignore a schema-defined account notification after the host exits."""
    plan_path, _ = review_evidence_files(tmp_path)
    launches = _launches_for_plan(plan_path)
    launches[1][2] = {"method": "account/updated"}
    launches[1] = launches[1][:5]
    _fake_public_processes(monkeypatch, launches)

    assert _adapter().check_host(plan_path, Path("codex"), 10)["model_invoked"] is False


def test_cleanup_rejects_account_update_with_request_id() -> None:
    """Keep server requests out of the account notification allowance."""
    assert not _adapter()._is_harmless_lifecycle({"id": 1, "method": "account/updated"})


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
    assert (output / "challenger.md").read_text(encoding="utf-8") == _review_answer(plan_path)
    assert (output / "cicd-steward.md").read_text(encoding="utf-8") == _review_answer(plan_path)
    requests = [json.loads(line)["method"] for line in factory.processes[1].stdin.getvalue().splitlines()]
    assert requests.index("turn/start") > requests.index("thread/start")
    assert requests.count("turn/start") == 2


def test_review_turns_enforce_output_schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Require generation-time structure for every reviewer, not just prompt instructions."""
    plan_path, _ = review_evidence_files(tmp_path)
    plan = json.loads(plan_path.read_text())
    factory = _fake_public_processes(monkeypatch, _launches_for_plan(plan_path))
    _adapter().run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)
    turns = [json.loads(line) for line in factory.processes[1].stdin.getvalue().splitlines()]
    for turn in (item for item in turns if item["method"] == "turn/start"):
        schema = turn["params"]["outputSchema"]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == {"source_sha256", "diff_sha256", "findings", "assessment"}
        assert schema["properties"]["source_sha256"]["enum"] == [plan["source_sha256"]]
        assert schema["properties"]["diff_sha256"]["enum"] == [plan["review_input_sha256"]]
        assert schema["properties"]["assessment"]["required"] == ["rating", "rationale"]
        finding = schema["properties"]["findings"]["items"]
        assert finding["additionalProperties"] is False
        assert set(finding["required"]) == {"signature", "tier", "structural", "disposition", "evidence"}
        assert finding["properties"]["structural"] == {"type": "boolean"}
        assert finding["properties"]["evidence"]["items"] == {"type": "string"}


def test_review_rejects_malformed_json_without_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Retain malformed output but never accept or repair it behind the reviewer's receipt."""
    plan_path, _ = review_evidence_files(tmp_path)
    malformed = '{"findings": ['
    factory = _fake_public_processes(monkeypatch, _launches_for_plan(plan_path, first_final=malformed))
    output = tmp_path / "review-output"
    with pytest.raises(_adapter().ReviewRouteError, match="app-server-review-output-invalid-json"):
        _adapter().run_review(plan_path, output, Path("codex"), 10)
    assert (output / "challenger.md").read_text() == malformed
    evidence = json.loads((output / "evidence.json").read_text())
    assert evidence["status"] == "failed"
    assert len(factory.processes) == 2


@pytest.mark.parametrize(
    "mutation",
    [
        "none",
        "wrong-source",
        "wrong-diff",
        "extra-root",
        "missing-findings",
        "null-findings",
        "extra-finding",
        "missing-tier",
        "invalid-tier",
        "invalid-disposition",
        "integer-boolean",
        "blank-signature",
        "empty-evidence",
        "blank-evidence",
        "nonstring-evidence",
        "duplicate-signature",
    ],
)
def test_review_validates_source_bound_finding_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    """Check actual returned records rather than assuming the host enforced the requested schema."""
    plan_path, _ = review_evidence_files(tmp_path)
    response = json.loads(_review_answer(plan_path))
    finding = {
        "signature": "missing-check",
        "tier": "high",
        "structural": False,
        "disposition": "open",
        "evidence": ["widget.py:1 lacks input validation."],
    }
    response["findings"] = [finding]
    if mutation == "wrong-source":
        response["source_sha256"] = "0" * 64
    elif mutation == "wrong-diff":
        response["diff_sha256"] = "0" * 64
    elif mutation == "extra-root":
        response["summary"] = "Unaccounted finding"
    elif mutation == "missing-findings":
        del response["findings"]
    elif mutation == "null-findings":
        response["findings"] = None
    elif mutation == "extra-finding":
        finding["extra"] = "untracked"
    elif mutation == "missing-tier":
        del finding["tier"]
    elif mutation == "invalid-tier":
        finding["tier"] = "unknown"
    elif mutation == "invalid-disposition":
        finding["disposition"] = "closed"
    elif mutation == "integer-boolean":
        finding["structural"] = 1
    elif mutation == "blank-signature":
        finding["signature"] = " "
    elif mutation == "empty-evidence":
        finding["evidence"] = []
    elif mutation == "blank-evidence":
        finding["evidence"] = [" "]
    elif mutation == "nonstring-evidence":
        finding["evidence"] = [False]
    elif mutation == "duplicate-signature":
        response["findings"].append(dict(finding))
    raw = json.dumps(response)
    _fake_public_processes(monkeypatch, _launches_for_plan(plan_path, first_final=raw))
    output = tmp_path / "review-output"
    if mutation == "none":
        _adapter().run_review(plan_path, output, Path("codex"), 10)
    else:
        with pytest.raises(
            _adapter().ReviewRouteError, match="app-server-review-output-(schema-mismatch|finding-invalid)"
        ):
            _adapter().run_review(plan_path, output, Path("codex"), 10)
    assert (output / "challenger.md").read_text() == raw


@pytest.mark.parametrize("identity", ["thread", "turn"])
def test_run_review_records_failed_final_identity_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, identity: str
) -> None:
    """Keep rejected final evidence from advertising completed reviewer execution."""
    plan_path, _ = review_evidence_files(tmp_path)
    launches = _launches_for_plan(plan_path)
    for frame in launches[1]:
        response_identity = frame.get("result", {}).get(identity)
        if response_identity and response_identity.get("id") == f"{identity}-1":
            response_identity["id"] = f"{identity}-0"
        params = frame.get("params", {})
        if params.get(f"{identity}Id") == f"{identity}-1":
            params[f"{identity}Id"] = f"{identity}-0"
        if identity == "turn" and params.get("turn", {}).get("id") == "turn-1":
            params["turn"]["id"] = "turn-0"
    _fake_public_processes(monkeypatch, launches)
    output = tmp_path / "review-output"

    with pytest.raises(_adapter().ReviewRouteError, match="evidence-thread-or-turn-id-duplicate"):
        _adapter().run_review(plan_path, output, Path("codex"), 10)

    evidence = json.loads((output / "evidence.json").read_text(encoding="utf-8"))
    assert evidence["status"] == "failed"
    assert evidence["failure_code"] == "evidence-thread-or-turn-id-duplicate"
    assert evidence["failure_phase"] == "evidence-validation"
    assert evidence["cleanup"] == "completed"
    assert (output / "challenger.md").read_text(encoding="utf-8") == _review_answer(plan_path)
    assert (output / "cicd-steward.md").read_text(encoding="utf-8") == _review_answer(plan_path)


def test_run_review_records_output_validation_failure_after_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Record final output rejection without rerunning cleanup or hiding its cause."""
    plan_path, _ = review_evidence_files(tmp_path)
    _fake_public_processes(monkeypatch, _launches_for_plan(plan_path))
    output = tmp_path / "review-output"
    original_wait = _FakeProcess.wait
    cleanup_calls: list[int] = []

    def wait_and_empty_output(process: _FakeProcess, timeout: float | None = None) -> int:
        """Simulate an external output change as the fake server finishes cleanup."""
        status = original_wait(process, timeout)
        cleanup_calls.append(process.pid)
        candidate = output / "challenger.md"
        if candidate.exists():
            candidate.write_bytes(b"")
        return status

    monkeypatch.setattr(_FakeProcess, "wait", wait_and_empty_output)

    with pytest.raises(_adapter().ReviewRouteError, match="evidence-output-empty-or-secret"):
        _adapter().run_review(plan_path, output, Path("codex"), 10)

    evidence = json.loads((output / "evidence.json").read_text(encoding="utf-8"))
    assert evidence["status"] == "failed"
    assert evidence["failure_code"] == "evidence-output-empty-or-secret"
    assert evidence["failure_phase"] == "evidence-validation"
    assert evidence["cleanup"] == "completed"
    assert len(cleanup_calls) == 2
    assert (output / "cicd-steward.md").read_text(encoding="utf-8") == _review_answer(plan_path)


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


@pytest.mark.skipif(not DIRECTORY_SYMLINKS_AVAILABLE, reason="filesystem cannot create directory symlinks")
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
    monkeypatch.setattr(_adapter(), "_live_source_unchanged", lambda *args: None)
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


@pytest.mark.parametrize("wrong_identity", ["turnId", "turn.id"])
@pytest.mark.parametrize("method", ["turn/completed", "item/agentMessage/delta", "turn/plan/updated"])
def test_run_review_rejects_conflicting_turn_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wrong_identity: str, method: str
) -> None:
    """Reject contradictory turn identities instead of accepting whichever matches."""
    plan_path, _ = review_evidence_files(tmp_path)
    launches = _launches_for_plan(plan_path)
    completion = next(frame for frame in launches[1] if frame.get("method") == "turn/completed")
    if method != "turn/completed":
        event = {
            "method": method,
            "params": {**completion["params"], "turn": dict(completion["params"]["turn"])},
        }
        launches[1].insert(launches[1].index(completion), event)
        completion = event
    completion["params"]["turnId"] = "turn-0"
    if wrong_identity == "turnId":
        completion["params"]["turnId"] = "other-turn"
    else:
        completion["params"]["turn"]["id"] = "other-turn"
    _fake_public_processes(monkeypatch, launches)
    output = tmp_path / "review-output"

    with pytest.raises(_adapter().ReviewRouteError, match="app-server-thread-or-turn-mismatch"):
        _adapter().run_review(plan_path, output, Path("codex"), 10)

    evidence = json.loads((output / "evidence.json").read_text(encoding="utf-8"))
    assert evidence["status"] == "failed"
    assert evidence["failure_code"] == "app-server-thread-or-turn-mismatch"
    assert not (output / "challenger.md").exists()


def test_run_review_accepts_agreeing_completion_turn_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Accept redundant matching identities instead of rejecting every dual-ID frame."""
    plan_path, _ = review_evidence_files(tmp_path)
    launches = _launches_for_plan(plan_path)
    for frame in launches[1]:
        if frame.get("method") == "turn/completed":
            frame["params"]["turnId"] = frame["params"]["turn"]["id"]
    _fake_public_processes(monkeypatch, launches)

    evidence_path = _adapter().run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)

    assert json.loads(evidence_path.read_text(encoding="utf-8"))["status"] == "completed"


@pytest.mark.parametrize("method", ["item/started", "item/completed"])
@pytest.mark.parametrize("item", [None, pytest.param([], id="array"), "invalid"])
def test_run_review_rejects_nonobject_lifecycle_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, item: object, method: str
) -> None:
    """Reject malformed lifecycle payloads even when valid final events follow."""
    plan_path, _ = review_evidence_files(tmp_path)
    launches = _launches_for_plan(plan_path)
    first_final = next(index for index, frame in enumerate(launches[1]) if frame.get("method") == "item/completed")
    launches[1].insert(
        first_final,
        {
            "jsonrpc": "2.0",
            "method": method,
            "params": {"threadId": "thread-0", "turnId": "turn-0", "item": item},
        },
    )
    _fake_public_processes(monkeypatch, launches)
    output = tmp_path / "review-output"

    with pytest.raises(_adapter().ReviewRouteError, match="app-server-event-item-not-object"):
        _adapter().run_review(plan_path, output, Path("codex"), 10)

    evidence = json.loads((output / "evidence.json").read_text(encoding="utf-8"))
    assert evidence["failure_code"] == "app-server-event-item-not-object"
    assert not (output / "challenger.md").exists()


@pytest.mark.parametrize("method", ["turn/plan/updated", "item/agentMessage/delta", "turn/completed"])
def test_run_review_rejects_events_for_completed_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    """Freeze a completed review while its sibling continues producing events."""
    plan_path, _ = review_evidence_files(tmp_path)
    launches = _launches_for_plan(plan_path)
    terminal = next(index for index, frame in enumerate(launches[1]) if frame.get("method") == "turn/completed")
    launches[1].insert(
        terminal + 1,
        {
            "jsonrpc": "2.0",
            "method": method,
            "params": {
                "threadId": "thread-0",
                "turnId": "turn-0",
                "turn": {"id": "turn-0", "status": "completed"},
                "plan": [],
                "itemId": "answer-0",
                "delta": "late text",
            },
        },
    )
    _fake_public_processes(monkeypatch, launches)
    output = tmp_path / "review-output"

    with pytest.raises(_adapter().ReviewRouteError, match="app-server-event-after-turn-completed"):
        _adapter().run_review(plan_path, output, Path("codex"), 10)

    evidence = json.loads((output / "evidence.json").read_text(encoding="utf-8"))
    assert evidence["status"] == "failed"
    assert evidence["failure_code"] == "app-server-event-after-turn-completed"
    assert (output / "challenger.md").read_text(encoding="utf-8") == _review_answer(plan_path)
    assert not (output / "qa-specialist.md").exists()


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


@pytest.mark.parametrize(
    ("code", "message", "reason"),
    [
        pytest.param(
            -32602, "Input exceeds the maximum length of 1048576 characters", "input-character-limit", id="input-limit"
        ),
        pytest.param(-32602, "private path /secret/account", "invalid-parameters", id="private-diagnostic"),
        pytest.param(-32601, "Method not found", "method-not-supported", id="unsupported-method"),
        pytest.param(True, "Input exceeds the maximum length of 1048576 characters", "unclassified", id="boolean-code"),
        pytest.param(12345, "sk-" + "abcdefghijklmnopqrstuvwxyz012345", "unclassified", id="unknown-secret-error"),
    ],
)
def test_rpc_failure_retains_only_allowlisted_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: object, message: str, reason: str
) -> None:
    """Keep actionable RPC categories without copying provider messages, data or arbitrary codes."""
    plan_path, _ = review_evidence_files(tmp_path)
    launches = _launches_for_plan(plan_path)
    launches[1] = launches[1][:5] + [{"id": 5, "error": {"code": code, "message": message, "data": "private-data"}}]
    _fake_public_processes(monkeypatch, launches)

    with pytest.raises(_adapter().ReviewRouteError, match="app-server-request-failed:turn/start"):
        _adapter().run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)

    evidence_text = (tmp_path / "review-output/evidence.json").read_text(encoding="utf-8")
    diagnostic = json.loads(evidence_text)["failure_diagnostic"]
    assert diagnostic["stage"] == "rpc-response"
    assert diagnostic["reason"] == reason
    assert diagnostic["method_category"] == "turn/start"
    assert diagnostic["rpc_code"] == (code if type(code) is int and code in {-32602, -32601} else None)
    assert "private-data" not in evidence_text and message not in evidence_text
    assert set(diagnostic) == {"stage", "reason", "method_category", "rpc_code", "recovery"}


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
    response = json.loads(_review_answer(plan_path))
    response["findings"] = [
        {"signature": "boundary", "tier": "low", "structural": False, "disposition": "open", "evidence": [""]}
    ]
    empty = json.dumps(response)
    response["findings"][0]["evidence"][0] = "x" * (_adapter().MAX_OUTPUT_BYTES - len(empty.encode("utf-8")))
    final = json.dumps(response)
    assert len(final.encode("utf-8")) == _adapter().MAX_OUTPUT_BYTES
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


@pytest.mark.parametrize(
    ("method", "params"),
    [
        pytest.param("warning", {"message": "Ignored warning."}, id="warning"),
        pytest.param("configWarning", {"summary": "Ignored configuration warning."}, id="config-warning"),
    ],
)
def test_run_review_discards_schema_warning_notification_before_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str, params: dict[str, str]
) -> None:
    """Discard only documented warning notifications without retaining their text."""
    plan_path, _ = review_evidence_files(tmp_path)
    launches = _launches_for_plan(plan_path)
    launches[1][2] = {"jsonrpc": "2.0", "method": method, "params": params}
    _fake_public_processes(monkeypatch, launches)

    evidence_path = _adapter().run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)

    assert params[next(iter(params))] not in evidence_path.read_text(encoding="utf-8")


def test_run_review_preserves_terminal_output_when_sibling_requests_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed on an approval request without deleting a prior completed response."""
    plan_path, _ = review_evidence_files(tmp_path)
    _fake_public_processes(monkeypatch, _launches_for_plan(plan_path, fail_second=True))
    output = tmp_path / "review-output"

    with pytest.raises(_adapter().ReviewRouteError, match="app-server-approval-requested"):
        _adapter().run_review(plan_path, output, Path("codex"), 10)

    assert (output / "challenger.md").read_text(encoding="utf-8") == _review_answer(plan_path)
    assert json.loads((output / "evidence.json").read_text(encoding="utf-8"))["status"] == "failed"


def test_check_host_cleans_up_when_initialize_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Terminate the just-started host if initialization cannot produce a result."""
    plan_path, _ = review_evidence_files(tmp_path)
    factory = _fake_public_processes(monkeypatch, [[{"jsonrpc": "2.0", "id": 1, "error": {}}]])

    with pytest.raises(_adapter().ReviewRouteError, match="app-server-request-failed:initialize"):
        _adapter().check_host(plan_path, Path("codex"), 10)

    assert len(factory.processes) == 1


@pytest.mark.parametrize(
    "notification",
    [
        pytest.param({"method": "account/updated"}, id="method-only"),
        pytest.param(
            {"jsonrpc": "2.0", "method": "account/updated", "params": {"authMode": "chatgpt", "planType": "plus"}},
            id="schema-payload",
        ),
    ],
)
def test_run_review_discards_account_update_during_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, notification: dict[str, object]
) -> None:
    """A host account notification must not interrupt a read-only review turn."""
    plan_path, _ = review_evidence_files(tmp_path)
    launches = _launches_for_plan(plan_path)
    first_event = next(index for index, frame in enumerate(launches[1]) if frame.get("method") == "item/completed")
    launches[1].insert(first_event, notification)
    _fake_public_processes(monkeypatch, launches)

    evidence_path = _adapter().run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)

    assert json.loads(evidence_path.read_text(encoding="utf-8"))["status"] == "completed"
    assert "authMode" not in evidence_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "method",
    [
        pytest.param("shellCommand/executed", id="execution-event"),
        pytest.param("model/verification/opaqueSuffix", id="documented-prefix-with-extra-segment"),
    ],
)
def test_run_review_rejects_unknown_event_method(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str) -> None:
    """Fail closed without retaining an event method outside the diagnostic allowlist."""
    plan_path, _ = review_evidence_files(tmp_path)
    launches = _launches_for_plan(plan_path)
    first_event = next(index for index, frame in enumerate(launches[1]) if frame.get("method") == "item/completed")
    launches[1][first_event] = {"jsonrpc": "2.0", "method": method, "params": {}}
    _fake_public_processes(monkeypatch, launches)

    with pytest.raises(_adapter().ReviewRouteError, match="app-server-event-rejected"):
        _adapter().run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)

    evidence = json.loads((tmp_path / "review-output" / "evidence.json").read_text(encoding="utf-8"))
    assert evidence["failure_diagnostic"] == {
        "stage": "turn-events",
        "reason": "method-not-allowlisted",
        "method_category": "unrecognized",
        "recovery": (
            "Continue permitted source inspection using native instruction-bounded reviewers or disclosed "
            "parent-serial review; resume this launcher only after protocol-maintainer triage validates a "
            "supported event schema."
        ),
    }
    assert method not in json.dumps(evidence)


@pytest.mark.parametrize(
    ("method", "category"),
    [
        pytest.param("model/verification", "model/verification", id="model-notification"),
        pytest.param("item/autoApprovalReview/started", "item/autoApprovalReview/started", id="approval-review"),
    ],
)
def test_run_review_classifies_documented_rejected_event_without_retaining_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str, category: str
) -> None:
    """Identify a documented event by static label while failing closed and discarding its payload."""
    plan_path, _ = review_evidence_files(tmp_path)
    launches = _launches_for_plan(plan_path)
    first_event = next(index for index, frame in enumerate(launches[1]) if frame.get("method") == "item/completed")
    launches[1][first_event] = {
        "jsonrpc": "2.0",
        "method": method,
        "params": {"sensitive": "do-not-retain-this-payload"},
    }
    _fake_public_processes(monkeypatch, launches)

    with pytest.raises(_adapter().ReviewRouteError, match="app-server-event-rejected"):
        _adapter().run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)

    evidence_text = (tmp_path / "review-output" / "evidence.json").read_text(encoding="utf-8")
    diagnostic = json.loads(evidence_text)["failure_diagnostic"]
    assert diagnostic["method_category"] == category
    assert "do-not-retain-this-payload" not in evidence_text


def test_run_review_rejects_nontext_event_method_without_retaining_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Treat malformed method values as unknown rather than raising a Python type error."""
    plan_path, _ = review_evidence_files(tmp_path)
    launches = _launches_for_plan(plan_path)
    first_event = next(index for index, frame in enumerate(launches[1]) if frame.get("method") == "item/completed")
    launches[1][first_event] = {
        "jsonrpc": "2.0",
        "method": {"sensitive": "do-not-retain-this-method"},
        "params": {},
    }
    _fake_public_processes(monkeypatch, launches)

    with pytest.raises(_adapter().ReviewRouteError, match="app-server-event-rejected"):
        _adapter().run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)

    evidence_text = (tmp_path / "review-output" / "evidence.json").read_text(encoding="utf-8")
    diagnostic = json.loads(evidence_text)["failure_diagnostic"]
    assert diagnostic["method_category"] == "unrecognized"
    assert "do-not-retain-this-method" not in evidence_text


def test_run_review_accepts_schema_planning_events_bound_to_active_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Accept the documented planning notifications without retaining their text."""
    plan_path, _ = review_evidence_files(tmp_path)
    launches = _launches_for_plan(plan_path)
    first_final = next(index for index, frame in enumerate(launches[1]) if frame.get("method") == "item/completed")
    plan_item = {"id": "plan-0", "type": "plan", "text": "Inspect the supplied source."}
    launches[1][first_final:first_final] = [
        {
            "jsonrpc": "2.0",
            "method": "turn/plan/updated",
            "params": {
                "threadId": "thread-0",
                "turnId": "turn-0",
                "plan": [{"step": "Inspect the supplied source.", "status": "inProgress"}],
            },
        },
        {
            "jsonrpc": "2.0",
            "method": "item/started",
            "params": {"threadId": "thread-0", "turnId": "turn-0", "item": plan_item},
        },
        {
            "jsonrpc": "2.0",
            "method": "item/plan/delta",
            "params": {"threadId": "thread-0", "turnId": "turn-0", "itemId": "plan-0", "delta": "Inspect"},
        },
        {
            "jsonrpc": "2.0",
            "method": "item/completed",
            "params": {"threadId": "thread-0", "turnId": "turn-0", "item": plan_item},
        },
    ]
    _fake_public_processes(monkeypatch, launches)

    _adapter().run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)

    evidence = (tmp_path / "review-output" / "evidence.json").read_text(encoding="utf-8")
    assert "Inspect the supplied source." not in evidence


def test_run_review_rejects_schema_planning_event_for_other_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep planning events bound to the active reviewer turns."""
    plan_path, _ = review_evidence_files(tmp_path)
    launches = _launches_for_plan(plan_path)
    first_final = next(index for index, frame in enumerate(launches[1]) if frame.get("method") == "item/completed")
    launches[1].insert(
        first_final,
        {
            "jsonrpc": "2.0",
            "method": "turn/plan/updated",
            "params": {
                "threadId": "other-thread",
                "turnId": "turn-0",
                "plan": [{"step": "Inspect the supplied source.", "status": "pending"}],
            },
        },
    )
    _fake_public_processes(monkeypatch, launches)

    with pytest.raises(_adapter().ReviewRouteError, match="app-server-thread-or-turn-mismatch"):
        _adapter().run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)


def test_run_review_rejects_malformed_schema_planning_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject a plan update whose step status is outside the generated schema."""
    plan_path, _ = review_evidence_files(tmp_path)
    launches = _launches_for_plan(plan_path)
    first_final = next(index for index, frame in enumerate(launches[1]) if frame.get("method") == "item/completed")
    launches[1].insert(
        first_final,
        {
            "jsonrpc": "2.0",
            "method": "turn/plan/updated",
            "params": {
                "threadId": "thread-0",
                "turnId": "turn-0",
                "plan": [{"step": "Inspect the supplied source.", "status": "unreviewed"}],
            },
        },
    )
    _fake_public_processes(monkeypatch, launches)

    with pytest.raises(_adapter().ReviewRouteError, match="app-server-plan-notification-invalid"):
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


@pytest.mark.parametrize(
    ("material", "reason"),
    [
        pytest.param("source", "source-or-diff", id="source-snapshot"),
        pytest.param("capacity", "capacity-evidence", id="sibling-capacity-evidence"),
    ],
)
def test_run_review_rechecks_admission_before_turn_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, material: str, reason: str
) -> None:
    """Reject actual evidence-file drift at the host boundary before any paid reviewer turn."""
    adapter = _adapter()
    plan_path, _ = review_evidence_files(tmp_path)
    factory = _fake_public_processes(monkeypatch, _launches_for_plan(plan_path))
    plan = json.loads(plan_path.read_bytes())
    relative = (
        plan["source_path"] if material == "source" else plan["nodes"][-1]["capacity_receipt"]["capacity_evidence_path"]
    )
    evidence_path = tmp_path / relative

    class MutatingInput(io.StringIO):
        """Simulate concurrent file mutation when the external host receives thread setup."""

        def write(self, text: str) -> int:
            """Retain RPC writes while changing the real evidence file, not the validator internals."""
            if json.loads(text).get("method") == "thread/start":
                evidence_path.write_bytes(b"changed evidence\n")
            return super().write(text)

    def launch(*args: object, **kwargs: object) -> _FakeProcess:
        """Return the existing host fake with mutation at its stdin boundary."""
        process = factory(*args, **kwargs)
        process.stdin = MutatingInput()
        return process

    monkeypatch.setattr(adapter.subprocess, "Popen", launch)

    with pytest.raises(adapter.ReviewRouteError, match=f"{reason}-mutated-before-turn"):
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
    _replace_context_bytes(plan_path, role_index, suffix.encode("utf-8"))


def _replace_context_bytes(plan_path: Path, role_index: int, suffix: bytes) -> None:
    """Replace one frozen fixture context with exact bytes after its canonical role-card prefix."""
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    node = plan["nodes"][role_index]
    role_card = (CANONICAL_ROLES / node["role_id"] / "ROLE.md").read_bytes()
    source = (plan_path.parent / plan["source_path"]).read_bytes()
    diff = (plan_path.parent / plan["diff_path"]).read_bytes()
    context = role_card + b"\nFrozen source:\n" + source + b"\nFrozen diff:\n" + diff + suffix
    context_path = plan_path.parent / node["context_path"]
    context_path.write_bytes(context)
    node["context_sha256"] = _sha256(context)
    node["capacity_receipt"]["context_sha256"] = node["context_sha256"]
    node["capacity_receipt"]["input_tokens"] = len(context)
    _write_json(plan_path, plan)


def _replace_context_to_size(plan_path: Path, role_index: int, size: int) -> str:
    """Replace one frozen context with exact valid UTF-8 Unicode and control-character bytes."""
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    role_card = (CANONICAL_ROLES / plan["nodes"][role_index]["role_id"] / "ROLE.md").read_bytes()
    source = (plan_path.parent / plan["source_path"]).read_bytes()
    diff = (plan_path.parent / plan["diff_path"]).read_bytes()
    prefix = role_card + b"\nFrozen source:\n" + source + b"\nFrozen diff:\n" + diff
    remaining = size - len(prefix)
    assert remaining >= 0
    pattern = "😀\x00x".encode("utf-8")
    suffix = pattern * (remaining // len(pattern)) + b"x" * (remaining % len(pattern))
    _replace_context_bytes(plan_path, role_index, suffix)
    return (prefix + suffix).decode("utf-8")


def review_evidence_files(
    tmp_path: Path, *, roles: tuple[str, ...] = ("challenger", "cicd-steward")
) -> tuple[Path, Path]:
    """Create a valid two-role frozen plan and bounded App Server evidence fixture."""
    contexts = tmp_path / "contexts"
    outputs = tmp_path / "outputs"
    contexts.mkdir()
    outputs.mkdir()
    source = json.dumps(
        {
            "schema_version": 1,
            "repository": "/workspace/repository",
            "scope_paths": ["widget.py"],
            "revision": "a" * 40,
            "index_sha256": "b" * 64,
            "files": [
                {
                    "path": "widget.py",
                    "kind": "file",
                    "sha256": _sha256(b"VALUE = 1\n"),
                    "executable": False,
                    "encoding": "utf-8",
                    "content": "VALUE = 1\n",
                }
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    diff = b"diff --git a/widget.py b/widget.py\n"
    (tmp_path / "source.json").write_bytes(source)
    (tmp_path / "diff.patch").write_bytes(diff)
    nodes: list[dict[str, object]] = []
    evidence_nodes: list[dict[str, object]] = []
    for index, role_id in enumerate(roles):
        role_card = (CANONICAL_ROLES / role_id / "ROLE.md").read_bytes()
        role_text = role_card.decode("utf-8")
        model = next(line.split(": ", 1)[1] for line in role_text.splitlines() if line.startswith("model: "))
        effort = next(
            line.split(": ", 1)[1] for line in role_text.splitlines() if line.startswith("model_reasoning_effort: ")
        )
        capacity = {
            "schema_version": 1,
            "model": model,
            "observed_supported_capacity_tokens": 3_000_000,
            "observed_default_context_window": 3_000_000,
        }
        capacity_path = tmp_path / f"capacity-{role_id}.json"
        _write_json(capacity_path, capacity)
        context = role_card + b"\nFrozen source:\n" + source + b"\nFrozen diff:\n" + diff
        context_path = contexts / f"{role_id}.txt"
        context_path.write_bytes(context)
        output = json.dumps(
            {
                "source_sha256": _sha256(source),
                "diff_sha256": _sha256(diff),
                "findings": [],
                "assessment": {"rating": 1, "rationale": f"No open findings for {role_id}."},
            }
        ).encode()
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
                "capacity_receipt": {
                    "context_sha256": _sha256(context),
                    "model": model,
                    "tokenizer": "utf8-byte-upper-bound",
                    "input_tokens": len(context),
                    "instruction_reserve_tokens": 100,
                    "output_reserve_tokens": 100,
                    "supported_capacity_tokens": 3_000_000,
                    "default_context_window": 3_000_000,
                    "effective_window_percent": 100,
                    "source_path": "source.json",
                    "source_sha256": _sha256(source),
                    "capacity_evidence_path": capacity_path.relative_to(tmp_path).as_posix(),
                    "capacity_evidence_sha256": _sha256(capacity_path.read_bytes()),
                },
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
        "schema_version": 2,
        "consumer_id": "code-review",
        "review_run_id": "review-17",
        "parent_thread_id": "parent-17",
        "review_input_sha256": _sha256(diff),
        "task_sensitivity": "non-sensitive",
        "cwd": str(tmp_path),
        "source_path": "source.json",
        "source_sha256": _sha256(source),
        "diff_path": "diff.patch",
        "diff_sha256": _sha256(diff),
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


def _committed_source_repository(root: Path) -> Path:
    """Create a real local Git checkout whose leaf bytes can be compared with a plan."""
    root.mkdir()
    (root / "widget.py").write_text("VALUE = 1\n", encoding="utf-8", newline="\n")
    for arguments in (
        ("init", "-q"),
        ("add", "widget.py"),
        ("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "initial"),
    ):
        subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)
    return root


def _bind_source_snapshot(plan_path: Path, source: dict[str, object]) -> None:
    """Keep frozen plan, role contexts, and capacity receipts consistent after test tampering."""
    plan = json.loads(plan_path.read_bytes())
    source_path = plan_path.parent / plan["source_path"]
    _write_json(source_path, source)
    plan["source_sha256"] = _sha256(source_path.read_bytes())
    for node in plan["nodes"]:
        node["capacity_receipt"]["source_sha256"] = plan["source_sha256"]
    _write_json(plan_path, plan)
    for index in range(len(plan["nodes"])):
        _replace_context(plan_path, index, "")


def _live_source_plan(tmp_path: Path) -> tuple[Path, Path]:
    """Bind the two-role plan to a committed checkout with matching source bytes."""
    plan_path, _ = review_evidence_files(tmp_path)
    repository = _committed_source_repository(tmp_path / "repository")
    plan = json.loads(plan_path.read_bytes())
    plan["cwd"] = str(repository)
    _write_json(plan_path, plan)
    source_path = plan_path.parent / plan["source_path"]
    source = json.loads(source_path.read_bytes())
    source["repository"] = repository.as_posix()
    revision = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"], cwd=repository, check=True, capture_output=True
    ).stdout.rstrip(b"\n")
    index = subprocess.run(
        ["git", "ls-files", "--stage", "-z", "--", "widget.py"], cwd=repository, check=True, capture_output=True
    ).stdout
    source["revision"] = revision.decode("ascii")
    source["index_sha256"] = _sha256(index)
    _bind_source_snapshot(plan_path, source)
    return plan_path, repository


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


def test_historical_schema_two_output_revalidates_without_later_assessment(tmp_path: Path) -> None:
    """Retain a prior source-bound response whose original schema had three fields."""
    plan_path, evidence_path = review_evidence_files(tmp_path)
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    for node in evidence["nodes"]:
        output_path = evidence_path.parent / node["output_path"]
        output = json.loads(output_path.read_text(encoding="utf-8"))
        del output["assessment"]
        output_path.write_text(json.dumps(output), encoding="utf-8", newline="\n")
        node["output_sha256"] = _sha256(output_path.read_bytes())
    _write_json(evidence_path, evidence)

    with pytest.raises(_adapter().ReviewRouteError, match="app-server-review-output-schema-mismatch"):
        _adapter().validate_evidence(plan_path, evidence_path, CANONICAL_ROLES)
    assert (
        _adapter().validate_evidence(plan_path, evidence_path, CANONICAL_ROLES, require_assessment=False)["consumer_id"]
        == "code-review"
    )


def test_retained_output_rejects_malformed_optional_assessment(tmp_path: Path) -> None:
    """An assessment present in retained evidence cannot bypass rating validation."""
    plan_path, evidence_path = review_evidence_files(tmp_path)
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    node = evidence["nodes"][0]
    output_path = evidence_path.parent / node["output_path"]
    output = json.loads(output_path.read_text(encoding="utf-8"))
    output["assessment"]["rating"] = True
    output_path.write_text(json.dumps(output), encoding="utf-8", newline="\n")
    node["output_sha256"] = _sha256(output_path.read_bytes())
    _write_json(evidence_path, evidence)

    with pytest.raises(_adapter().ReviewRouteError, match="app-server-review-output-assessment-invalid"):
        _adapter().validate_evidence(plan_path, evidence_path, CANONICAL_ROLES)


def test_app_server_rejects_explicit_selection_advisors(tmp_path: Path) -> None:
    """Keep architecture and security advisors outside automatic review dispatch."""
    plan_path, evidence_path = review_evidence_files(tmp_path, roles=("security-auditor",))

    with pytest.raises(_adapter().ReviewRouteError, match="plan-role-model-unsupported"):
        _adapter().validate_evidence(plan_path, evidence_path, CANONICAL_ROLES)


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
