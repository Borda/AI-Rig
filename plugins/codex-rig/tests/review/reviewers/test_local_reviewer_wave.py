"""Exercise the bounded local reviewer wave evidence adapter."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType

import pytest
from _platform import DIRECTORY_SYMLINKS_AVAILABLE

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
CANONICAL_ROLES = PLUGIN_ROOT / "roles"
_ADAPTER: ModuleType | None = None


def _adapter() -> ModuleType:
    """Load the production adapter without treating the plugin root as a package."""
    global _ADAPTER
    if _ADAPTER is not None:
        return _ADAPTER
    path = PLUGIN_ROOT / "shared" / "local_reviewer_wave.py"
    spec = importlib.util.spec_from_file_location("local_reviewer_wave", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _ADAPTER = module
    return _ADAPTER


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
        assert isinstance(command, list)
        assert all(isinstance(part, str) for part in command)
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
    # local reviewer wave may notify lifecycle state while the matching request is pending.
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
    factory = _PopenFactory(launches[1:] if len(launches) > 1 else launches)
    monkeypatch.setattr(adapter, "_discover_mcp_servers", lambda *args: ["local"])
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


def _requests(factory: _PopenFactory) -> list[dict[str, object]]:
    """Return the JSON-RPC requests the adapter wrote to the single fake reviewer host."""
    return [json.loads(line) for line in factory.processes[0].stdin.getvalue().splitlines()]


def _request_methods(factory: _PopenFactory) -> list[str]:
    """Return only the method names of the requests the adapter sent to the fake reviewer host."""
    return [request["method"] for request in _requests(factory)]


def _first_index(frames: list[dict[str, object]], method: str) -> int:
    """Return the position of the first scripted host frame with the given notification method."""
    return next(index for index, frame in enumerate(frames) if frame.get("method") == method)


def _first_frame(frames: list[dict[str, object]], method: str, item_type: str | None = None) -> dict[str, object]:
    """Return the first scripted host frame with the given method and, when named, item type."""
    return next(
        frame
        for frame in frames
        if frame.get("method") == method and (item_type is None or frame["params"]["item"].get("type") == item_type)
    )


def _check_launches(plan_path: Path, host_frames: int = 5) -> list[list[dict[str, object]]]:
    """Return launches cut after the thread-start responses, as a no-turn host check consumes them."""
    launches = _launches_for_plan(plan_path)
    launches[1] = launches[1][:host_frames]
    return launches


def _check_launches_with_frame(plan_path: Path, frame: dict[str, object]) -> list[list[dict[str, object]]]:
    """Return no-turn host-check launches whose first lifecycle notification slot holds the given frame."""
    launches = _check_launches(plan_path)
    launches[1][2] = frame
    return launches


def _run_with_first_context_of_length(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, length: int
) -> tuple[dict[str, object], str, _PopenFactory, dict[str, object]]:
    """Run a review whose first reviewer context has exactly `length` characters; return plan, text, host, evidence."""
    plan_path, _ = review_evidence_files(tmp_path)
    plan = json.loads(plan_path.read_bytes())
    prefix = (plan_path.parent / plan["nodes"][0]["context_path"]).read_text(encoding="utf-8")
    _replace_context_bytes(plan_path, 0, b"x" * (length - len(prefix)))
    plan = json.loads(plan_path.read_bytes())
    context = (plan_path.parent / plan["nodes"][0]["context_path"]).read_text(encoding="utf-8")
    assert len(context) == length
    factory = _fake_public_processes(monkeypatch, _launches_for_plan(plan_path, echo_input=True))
    evidence_path = _adapter().run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)
    return plan, context, factory, json.loads(evidence_path.read_bytes())


def _on_first_node(amend: Callable[[dict[str, object], dict[str, object]], object]) -> Callable[[Path], None]:
    """Adapt an in-place `(plan, first_node)` edit into a tamper that rewrites the plan file."""

    def tamper(plan_path: Path) -> None:
        """Apply the wrapped edit to the stored plan and write it back."""
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        amend(plan, plan["nodes"][0])
        _write_json(plan_path, plan)

    return tamper


def _drop_source_from_context(plan_path: Path) -> None:
    """Remove the frozen source bytes from the first context while keeping its digests self-consistent."""
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    node = plan["nodes"][0]
    context_path = plan_path.parent / node["context_path"]
    source = (plan_path.parent / plan["source_path"]).read_bytes()
    context = context_path.read_bytes().replace(source, b"", 1)
    context_path.write_bytes(context)
    node["context_sha256"] = node["capacity_receipt"]["context_sha256"] = _sha256(context)
    _write_json(plan_path, plan)


def _overwrite_source_file(plan_path: Path) -> None:
    """Replace the stored source snapshot file without updating the plan's recorded digest."""
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    (plan_path.parent / plan["source_path"]).write_bytes(b'{"files":[]}')


def _tamper_source_content_and_rebind_digest(plan_path: Path) -> None:
    """Change a snapshot file's content but not its recorded file digest, then bind the new snapshot digest."""
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    source_path = plan_path.parent / plan["source_path"]
    source = json.loads(source_path.read_text(encoding="utf-8"))
    source["files"][0]["content"] = "tampered source"
    _write_json(source_path, source)
    plan["source_sha256"] = _sha256(source_path.read_bytes())
    plan["nodes"][0]["capacity_receipt"]["source_sha256"] = plan["source_sha256"]
    _write_json(plan_path, plan)


def _replace_capacity_evidence(plan_path: Path, evidence: bytes) -> None:
    """Overwrite the first node's capacity evidence file and bind its new digest in the receipt."""
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    receipt = plan["nodes"][0]["capacity_receipt"]
    (plan_path.parent / receipt["capacity_evidence_path"]).write_bytes(evidence)
    receipt["capacity_evidence_sha256"] = _sha256(evidence)
    _write_json(plan_path, plan)


def _claim_capacity(
    plan_path: Path,
    *,
    supported: int,
    default_window: int,
    percent: int,
    receipt: dict[str, int],
    node_fields: dict[str, int],
) -> None:
    """Rewrite the first node's capacity evidence and receipt to the stated values, signed with a fresh digest."""
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    node = plan["nodes"][0]
    capacity_path = plan_path.parent / node["capacity_receipt"]["capacity_evidence_path"]
    _write_json(
        capacity_path,
        {
            "schema_version": 1,
            "model": node["model"],
            "observed_supported_capacity_tokens": supported,
            "observed_default_context_window": default_window,
        },
    )
    node["capacity_receipt"].update(
        receipt,
        supported_capacity_tokens=supported,
        default_context_window=default_window,
        effective_window_percent=percent,
        capacity_evidence_sha256=_sha256(capacity_path.read_bytes()),
    )
    node.update(node_fields)
    _write_json(plan_path, plan)


def _claim_window_percent(plan_path: Path, *, context_size: int, reserves: tuple[int, int], extra_node: dict) -> None:
    """Resize the first context, then claim a 95 percent window over 872k supported / 272k default capacity."""
    _replace_context_to_size(plan_path, 0, context_size)
    _claim_capacity(
        plan_path,
        supported=872_000,
        default_window=272_000,
        percent=95,
        receipt={
            "input_tokens": context_size,
            "instruction_reserve_tokens": reserves[0],
            "output_reserve_tokens": reserves[1],
        },
        node_fields=extra_node,
    )


def _forge_snapshot_content(plan_path: Path, repository: Path) -> None:
    """Rewrite the frozen snapshot so it is internally consistent but disagrees with the checkout."""
    plan = json.loads(plan_path.read_bytes())
    source = json.loads((plan_path.parent / plan["source_path"]).read_bytes())
    source["files"][0]["content"] = "VALUE = 2\n"
    source["files"][0]["sha256"] = _sha256(b"VALUE = 2\n")
    _bind_source_snapshot(plan_path, source)


def _change_live_file(plan_path: Path, repository: Path) -> None:
    """Edit the tracked checkout file so the previously frozen snapshot becomes stale."""
    (repository / "widget.py").write_text("VALUE = 2\n", encoding="utf-8", newline="\n")


def _plan_with_window_headroom(plan_path: Path, suffix: str, headroom: int) -> None:
    """Size the first context to `suffix` and set its model window to measured input plus reserves plus `headroom`."""
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


def _freeze_historical_role_cards(
    tmp_path: Path, plan_path: Path, plan: dict[str, object], evidence: dict[str, object]
) -> Path:
    """Give every node a pre-migration role card with an inline `model:` line and rebind plan, evidence and context.

    Returns the directory of historical roles. Capacity receipts and outputs are left for the caller to adjust.
    """
    historical_roles = tmp_path / "historical-roles"
    for node, evidence_node in zip(plan["nodes"], evidence["nodes"], strict=True):
        role_dir = historical_roles / node["role_id"]
        role_dir.mkdir(parents=True)
        card = (CANONICAL_ROLES / node["role_id"] / "ROLE.md").read_bytes()
        historical_card = card.replace(b"---\n", f"---\nmodel: {node['model']}\n".encode(), 1)
        (role_dir / "ROLE.md").write_bytes(historical_card)
        node["role_card_sha256"] = evidence_node["role_card_sha256"] = _sha256(historical_card)
        context_path = plan_path.parent / node["context_path"]
        context = context_path.read_bytes().replace(card, historical_card, 1)
        context_path.write_bytes(context)
        node["context_sha256"] = evidence_node["context_sha256"] = _sha256(context)
    return historical_roles


def _finding_response(plan_path: Path) -> tuple[dict[str, object], dict[str, object]]:
    """Return a source-bound review response holding one valid open finding, and that finding."""
    response = json.loads(_review_answer(plan_path))
    finding = {
        "signature": "missing-check",
        "tier": "high",
        "structural": False,
        "disposition": "open",
        "evidence": ["widget.py:1 lacks input validation."],
    }
    response["findings"] = [finding]
    return response, finding


def _collide_thread_identity(frames: list[dict[str, object]]) -> None:
    """Make the second reviewer report the first reviewer's thread id in every scripted frame."""
    for frame in frames:
        thread = frame.get("result", {}).get("thread")
        if thread and thread.get("id") == "thread-1":
            thread["id"] = "thread-0"
        params = frame.get("params", {})
        if params.get("threadId") == "thread-1":
            params["threadId"] = "thread-0"


def _collide_turn_identity(frames: list[dict[str, object]]) -> None:
    """Make the second reviewer report the first reviewer's turn id in every scripted frame."""
    for frame in frames:
        turn = frame.get("result", {}).get("turn")
        if turn and turn.get("id") == "turn-1":
            turn["id"] = "turn-0"
        params = frame.get("params", {})
        if params.get("turnId") == "turn-1":
            params["turnId"] = "turn-0"
        if params.get("turn", {}).get("id") == "turn-1":
            params["turn"]["id"] = "turn-0"


def _launches_with_agent_message_phase(plan_path: Path, phase: str | None) -> list[list[dict[str, object]]]:
    """Return launches whose completed assistant-message items all carry the given `phase`."""
    launches = _launches_for_plan(plan_path)
    messages = [
        frame
        for frame in launches[1]
        if frame.get("method") == "item/completed" and frame["params"]["item"].get("type") == "agentMessage"
    ]
    for frame in messages:
        frame["params"]["item"]["phase"] = phase
    return launches


def _repeat_turn_id_in_completions(frames: list[dict[str, object]]) -> None:
    """Add a top-level `turnId` equal to `turn.id` to every scripted turn-completion frame."""
    for frame in frames:
        if frame.get("method") == "turn/completed":
            frame["params"]["turnId"] = frame["params"]["turn"]["id"]


def _duplicate_user_message_completion(frames: list[dict[str, object]]) -> None:
    """Replay the user-message completion frame a second time right after the first."""
    completion = _first_frame(frames, "item/completed", "userMessage")
    frames.insert(frames.index(completion) + 1, completion)


def _append_extra_text_part_to_echo(frames: list[dict[str, object]]) -> None:
    """Add a second text part to the echoed user-message content."""
    item = _first_frame(frames, "item/started")["params"]["item"]
    item["content"] = [*item["content"], {"type": "text", "text": "extra"}]


_PRIVATE_METADATA = "private-metadata-not-for-evidence"


def _launches_with_moderation_metadata(plan_path: Path, **overrides: object) -> list[list[dict[str, object]]]:
    """Return launches with a `turn/moderationMetadata` notification before the first final answer.

    Default params are valid and bound to the first turn; a `None` override removes that key.
    """
    launches = _launches_for_plan(plan_path)
    params = {"threadId": "thread-0", "turnId": "turn-0", "metadata": {"opaque": _PRIVATE_METADATA}, **overrides}
    params = {key: value for key, value in params.items() if value is not None}
    event = {"jsonrpc": "2.0", "method": "turn/moderationMetadata", "params": params}
    launches[1].insert(_first_index(launches[1], "item/completed"), event)
    return launches


_skip_posix_group = pytest.mark.skipif(not hasattr(os, "killpg"), reason="process-group cleanup requires POSIX killpg")


@pytest.fixture
def local_child() -> Iterator[subprocess.Popen[bytes]]:
    """Start a harmless sleeping Python child in its own session; kill and reap it at teardown if still alive."""
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    yield process
    if process.poll() is None:
        process.kill()
        process.wait(timeout=2)


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
    pattern = "😀\x00x".encode()
    suffix = pattern * (remaining // len(pattern)) + b"x" * (remaining % len(pattern))
    _replace_context_bytes(plan_path, role_index, suffix)
    return (prefix + suffix).decode("utf-8")


def review_evidence_files(
    tmp_path: Path, *, roles: tuple[str, ...] = ("challenger", "cicd-steward"), source_path: str = "widget.py"
) -> tuple[Path, Path]:
    """Create a valid two-role frozen plan and bounded local reviewer wave evidence fixture."""
    contexts = tmp_path / "contexts"
    outputs = tmp_path / "outputs"
    contexts.mkdir()
    outputs.mkdir()
    source = json.dumps(
        {
            "schema_version": 1,
            "repository": "/workspace/repository",
            "scope_paths": [source_path],
            "revision": "a" * 40,
            "index_sha256": "b" * 64,
            "files": [
                {
                    "path": source_path,
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
    diff = f"diff --git a/{source_path} b/{source_path}\n".encode()
    (tmp_path / "source.json").write_bytes(source)
    (tmp_path / "diff.patch").write_bytes(diff)
    nodes: list[dict[str, object]] = []
    evidence_nodes: list[dict[str, object]] = []
    for index, role_id in enumerate(roles):
        role_card = (CANONICAL_ROLES / role_id / "ROLE.md").read_bytes()
        role_text = role_card.decode("utf-8")
        model = {
            "cicd-steward": "gpt-6-luna",
            "doc-scribe": "gpt-6.1-terra",
            "web-explorer": "gpt-6.1-terra",
        }.get(role_id, "gpt-6.1-sol")
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
        "schema_version": 3,
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


def _rebind_capacity_to_contexts(plan_path: Path, plan: dict[str, object]) -> None:
    """Point every node's capacity receipt at its (rewritten) context file digest and byte length."""
    for node in plan["nodes"]:
        context = (plan_path.parent / node["context_path"]).read_bytes()
        node["capacity_receipt"]["context_sha256"] = _sha256(context)
        node["capacity_receipt"]["input_tokens"] = len(context)


def _strip_assessment_from_outputs(evidence_path: Path, evidence: dict[str, object]) -> None:
    """Rewrite every retained reviewer output without its `assessment` field and re-bind the output digests."""
    for node in evidence["nodes"]:
        output_path = evidence_path.parent / node["output_path"]
        output = json.loads(output_path.read_text(encoding="utf-8"))
        del output["assessment"]
        output_path.write_text(json.dumps(output), encoding="utf-8", newline="\n")
        node["output_sha256"] = _sha256(output_path.read_bytes())


@pytest.fixture
def historical_schema_two_evidence(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Build schema-2 plan and evidence with pre-assessment outputs; return the plan, evidence and roles paths."""
    plan_path, evidence_path = review_evidence_files(tmp_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["schema_version"] = 2
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    historical_roles = _freeze_historical_role_cards(tmp_path, plan_path, plan, evidence)
    _rebind_capacity_to_contexts(plan_path, plan)
    _strip_assessment_from_outputs(evidence_path, evidence)
    _write_json(plan_path, plan)
    evidence["plan_sha256"] = _sha256(plan_path.read_bytes())
    _write_json(evidence_path, evidence)
    return plan_path, evidence_path, historical_roles


class TestAdapterSurface:
    """Public surface and fixed-shape state of the local reviewer wave adapter."""

    def test_exports_evidence_validator(self) -> None:
        """Expose the evidence validator required by code-review integration.

        Code-review integration imports `validate_evidence` from the adapter module; losing that export would break it
        at import time instead of here.
        """
        assert callable(_adapter().validate_evidence)

    def test_active_review_has_independent_lifecycle_defaults_and_rejects_typos(self) -> None:
        """Keep the private fixed-shape reviewer state explicit and independently mutable.

        Two reviewer states built side by side must not share defaults, and a misspelled attribute must raise instead of
        silently creating a new field.
        """
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

    def test_invocation_controls_disable_each_discovered_simple_mcp_server(self) -> None:
        """Keep empty-map merging harmless by also disabling every discovered server directly.

        The host command line blanks the MCP server map and also disables each discovered server by name, so a config
        that merges maps still ends with every server off.
        """
        command = _adapter()._server_command(Path("codex"), ["one", "two"])

        assert command[:3] == ["codex", "app-server", "--stdio"]
        assert "mcp_servers={}" in command
        assert "mcp_servers.one.enabled=false" in command
        assert "mcp_servers.two.enabled=false" in command

    def test_thread_controls_reject_requested_without_observed_network_denial(self) -> None:
        """Require the host response rather than accepting the requested read-only mode.

        The host reports network access enabled although read-only was requested; the adapter trusts the observed
        response, not the request, and refuses it.
        """
        node = {"model": "gpt-5.6-terra", "reasoning_effort": "high"}
        observed = {
            "sandbox": {"type": "readOnly", "networkAccess": True},
            "approvalPolicy": "never",
            "model": "gpt-5.6-terra",
            "reasoningEffort": "high",
        }

        with pytest.raises(_adapter().ReviewRouteError, match="app-server-thread-controls-mismatch"):
            _adapter()._thread_controls(observed, node)

    def test_context_frame_queue_preserves_the_prior_aggregate_resource_envelope(self) -> None:
        """Scale both decoded-frame buffers down when enlarged contexts raise the per-frame ceiling.

        Raising the per-frame ceiling for 2 MiB contexts must not raise the total memory the decoded-frame queue may
        hold, so the queue length shrinks to compensate.
        """
        adapter = _adapter()

        assert adapter.MAX_EVENTS == 512
        assert adapter.MAX_BUFFERED_EVENTS < adapter.MAX_EVENTS
        assert adapter.MAX_BUFFERED_EVENTS * adapter.MAX_EVENT_BYTES <= adapter.MAX_BUFFERED_EVENT_BYTES
        assert adapter._JsonRpcStdio(_FakeProcess([]), 1.0)._messages.maxsize == adapter.MAX_BUFFERED_EVENTS

    def test_request_rejects_pending_frames_past_scaled_buffer_capacity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Fail closed before unmatched responses can retain the enlarged frame ceiling indefinitely.

        Unmatched responses keep arriving while a request waits; once they fill the scaled buffer the request fails
        closed instead of buffering without bound.
        """
        adapter = _adapter()
        client = adapter._JsonRpcStdio(_FakeProcess([]), time.monotonic() + 5)
        frames = iter(_response(2, {}) for _ in range(adapter.MAX_PENDING_EVENTS + 1))
        monkeypatch.setattr(client, "_read", lambda: next(frames))

        with pytest.raises(adapter.ReviewRouteError, match="app-server-pending-events-overflow"):
            client.request("initialize", {})

        assert len(client._pending) == adapter.MAX_PENDING_EVENTS


class TestReviewOutput:
    """Review answer schema, validation and retention of accepted or rejected output."""

    def test_code_review_clean_output_requires_scoped_assessment(self) -> None:
        """An empty findings list must still carry the reviewer's rating and rationale.

        A code-review plan with no findings still needs the reviewer's assessment, so the schema marks it required and a
        response without it is rejected as a schema mismatch.
        """
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
    def test_code_review_rejects_invalid_scoped_assessment(self, assessment: dict[str, object], error: str) -> None:
        """A present but unsupported judgment must fail before output retention.

        A boolean rating, a blank rationale and an out-of-range rating each fail validation, so a malformed judgment is
        never stored as review evidence.
        """
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

    def test_other_consumer_retains_findings_only_output(self) -> None:
        """The Code Review assessment field does not change other review routes.

        Other consumers such as challenge-resolve keep the three-field findings-only schema; the code-review assessment
        requirement must not leak into them.
        """
        adapter = _adapter()
        plan = {"consumer_id": "challenge-resolve", "source_sha256": "a" * 64, "review_input_sha256": "b" * 64}
        response = {"source_sha256": plan["source_sha256"], "diff_sha256": plan["review_input_sha256"], "findings": []}

        assert adapter._review_output_schema(plan)["required"] == ["source_sha256", "diff_sha256", "findings"]
        adapter._validate_review_output(json.dumps(response), plan)

    @pytest.mark.parametrize(
        "turn_index",
        [pytest.param(0, id="first-reviewer"), pytest.param(1, id="second-reviewer")],
    )
    def test_turns_enforce_output_schema(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, turn_index: int
    ) -> None:
        """Require generation-time structure for every reviewer, not just prompt instructions.

        Both reviewer turns must carry an output schema pinned to the frozen source and diff digests, with the closed
        finding record and assessment shapes the validator later checks.
        """
        plan_path, _ = review_evidence_files(tmp_path)
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        factory = _fake_public_processes(monkeypatch, _launches_for_plan(plan_path))
        _adapter().run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)
        turns = [request for request in _requests(factory) if request["method"] == "turn/start"]

        schema = turns[turn_index]["params"]["outputSchema"]
        finding = schema["properties"]["findings"]["items"]
        assert len(turns) == 2
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == {"source_sha256", "diff_sha256", "findings", "assessment"}
        assert schema["properties"]["source_sha256"]["enum"] == [plan["source_sha256"]]
        assert schema["properties"]["diff_sha256"]["enum"] == [plan["review_input_sha256"]]
        assert schema["properties"]["assessment"]["required"] == ["rating", "rationale"]
        assert finding["additionalProperties"] is False
        assert set(finding["required"]) == {"signature", "tier", "structural", "disposition", "evidence"}
        assert finding["properties"]["structural"] == {"type": "boolean"}
        assert finding["properties"]["evidence"]["items"] == {"type": "string"}

    def test_rejects_malformed_json_without_retry(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Retain malformed output but never accept or repair it behind the reviewer's receipt.

        The first reviewer returns truncated JSON; the file is kept verbatim, evidence records the failure, and exactly
        one host process ran, so nothing was retried.
        """
        plan_path, _ = review_evidence_files(tmp_path)
        malformed = '{"findings": ['
        factory = _fake_public_processes(monkeypatch, _launches_for_plan(plan_path, first_final=malformed))
        output = tmp_path / "review-output"
        with pytest.raises(_adapter().ReviewRouteError, match="app-server-review-output-invalid-json"):
            _adapter().run_review(plan_path, output, Path("codex"), 10)
        assert (output / "challenger.md").read_text(encoding="utf-8") == malformed
        evidence = json.loads((output / "evidence.json").read_text(encoding="utf-8"))
        assert evidence["status"] == "failed"
        assert len(factory.processes) == 1

    def test_retains_valid_source_bound_finding(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A schema-valid open finding bound to the frozen source and diff is accepted and retained verbatim.

        This is the unmutated baseline for the rejection cases below, so a validator that refuses everything cannot
        pass.
        """
        plan_path, _ = review_evidence_files(tmp_path)
        response, _ = _finding_response(plan_path)
        raw = json.dumps(response)
        _fake_public_processes(monkeypatch, _launches_for_plan(plan_path, first_final=raw))
        output = tmp_path / "review-output"

        _adapter().run_review(plan_path, output, Path("codex"), 10)

        assert (output / "challenger.md").read_text(encoding="utf-8") == raw

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(lambda response, finding: response.update(source_sha256="0" * 64), id="wrong-source"),
            pytest.param(lambda response, finding: response.update(diff_sha256="0" * 64), id="wrong-diff"),
            pytest.param(lambda response, finding: response.update(summary="Unaccounted finding"), id="extra-root"),
            pytest.param(lambda response, finding: response.pop("findings"), id="missing-findings"),
            pytest.param(lambda response, finding: response.update(findings=None), id="null-findings"),
            pytest.param(lambda response, finding: finding.update(extra="untracked"), id="extra-finding"),
            pytest.param(lambda response, finding: finding.pop("tier"), id="missing-tier"),
            pytest.param(lambda response, finding: finding.update(tier="unknown"), id="invalid-tier"),
            pytest.param(lambda response, finding: finding.update(disposition="closed"), id="invalid-disposition"),
            pytest.param(lambda response, finding: finding.update(structural=1), id="integer-boolean"),
            pytest.param(lambda response, finding: finding.update(signature=" "), id="blank-signature"),
            pytest.param(lambda response, finding: finding.update(evidence=[]), id="empty-evidence"),
            pytest.param(lambda response, finding: finding.update(evidence=[" "]), id="blank-evidence"),
            pytest.param(lambda response, finding: finding.update(evidence=[False]), id="nonstring-evidence"),
            pytest.param(
                lambda response, finding: response["findings"].append(dict(finding)), id="duplicate-signature"
            ),
        ],
    )
    def test_rejects_invalid_source_bound_finding_fields(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        mutate: Callable[[dict[str, object], dict[str, object]], object],
    ) -> None:
        """Check actual returned records rather than assuming the host enforced the requested schema.

        Each case damages one field of an otherwise valid finding response (wrong digests, extra or missing keys, bad
        enum values, non-boolean flags, blank or duplicate content); the answer is rejected but still retained for
        diagnosis.
        """
        plan_path, _ = review_evidence_files(tmp_path)
        response, finding = _finding_response(plan_path)
        mutate(response, finding)
        raw = json.dumps(response)
        _fake_public_processes(monkeypatch, _launches_for_plan(plan_path, first_final=raw))
        output = tmp_path / "review-output"

        with pytest.raises(
            _adapter().ReviewRouteError, match=r"app-server-review-output-(schema-mismatch|finding-invalid)"
        ):
            _adapter().run_review(plan_path, output, Path("codex"), 10)

        assert (output / "challenger.md").read_text(encoding="utf-8") == raw

    def test_accepts_maximum_final_output_frame(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Keep the documented 128-KiB final output limit reachable through its JSON frame envelope.

        A final answer whose raw size equals the output cap still fits once wrapped in the JSON frame, so the limit is
        reachable and not only documented.
        """
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


class TestCheckHost:
    """No-turn host check: effective denials, thread controls and rejection before any process launch."""

    def test_uses_public_preparation_without_turn_start(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify effective denials and observed thread controls without a paid model turn.

        Two reviewer threads are prepared against a scripted host; the contract is that MCP servers are disabled,
        provider fallback is refused, and no `turn/start` request is ever sent.
        """
        plan_path, _ = review_evidence_files(tmp_path)
        factory = _fake_public_processes(monkeypatch, _check_launches(plan_path))

        summary = _adapter().check_host(plan_path, Path("codex"), 10)

        assert summary["model_invoked"] is False
        requests = _request_methods(factory)
        assert requests.count("thread/start") == 2
        assert "turn/start" not in requests
        assert "mcp_servers={}" in factory.commands[0]
        assert "mcp_servers.local.enabled=false" in factory.commands[0]
        request_payloads = _requests(factory)
        thread_starts = [request["params"] for request in request_payloads if request["method"] == "thread/start"]
        assert request_payloads[0]["params"]["capabilities"] == {"experimentalApi": True}
        assert all(params.get("allowProviderModelFallback") is False for params in thread_starts)
        assert all(params["config"] == {"model_reasoning_effort": "high"} for params in thread_starts)

    def test_accepts_exact_two_mebibyte_unicode_control_context_before_launching_turns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Accept a full context above the metadata limit and preserve every sent character exactly.

        A context of exactly 2 MiB holding emoji and NUL characters passes the host check, is preloaded unchanged into
        history, and the turn input carries only the history prompt.
        """
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
        assert "😀" in expected
        assert "\x00" in expected
        requests = _requests(factory)
        first_turn = next(request for request in requests if request["method"] == "turn/start")
        preload = next(request for request in requests if request["method"] == "thread/inject_items")
        assert preload["params"]["items"] == [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": expected}]}
        ]
        assert first_turn["params"]["input"] == [{"type": "text", "text": adapter.HISTORY_REVIEW_PROMPT}]

    def test_rejects_context_larger_than_two_mebibytes_before_process_launch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reject a one-byte context overage before spawning a local reviewer process.

        The size limit is checked before any process spawns, so an oversized context costs nothing and leaves no orphan
        host.
        """
        adapter = _adapter()
        plan_path, _ = review_evidence_files(tmp_path)
        _replace_context_to_size(plan_path, 0, adapter.MAX_CONTEXT_BYTES + 1)
        factory = _fake_public_processes(monkeypatch, _launches_for_plan(plan_path))

        with pytest.raises(adapter.ReviewRouteError, match="plan-context-too-large"):
            adapter.check_host(plan_path, Path("codex"), 10)

        assert factory.processes == []

    def test_rejects_invalid_utf8_context_before_process_launch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reject malformed context text distinctly before any local reviewer wave process starts.

        A context file with bytes that are not UTF-8 gets its own error code, distinct from the size and secret checks,
        and no host starts.
        """
        plan_path, _ = review_evidence_files(tmp_path)
        _replace_context_bytes(plan_path, 0, b"\xff")
        factory = _fake_public_processes(monkeypatch, [])

        with pytest.raises(_adapter().ReviewRouteError, match="plan-context-invalid-utf8"):
            _adapter().check_host(plan_path, Path("codex"), 10)

        assert factory.processes == []

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param(b"Authorization: " + b"Bearer " + b"fixture", id="bearer"),
            pytest.param(b"github" + b"_pat_" + b"a_b" * 9, id="github-fine-grained"),
            pytest.param(b"gh" + b"r_" + b"a_b" * 9, id="github-refresh"),
            pytest.param(b"sk" + b"-" + b"a_b-" * 6, id="api-token-punctuation"),
        ],
    )
    def test_rejects_native_sensitive_shapes_before_process_launch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: bytes
    ) -> None:
        """Reject native credential shapes in frozen input before launching a reviewer.

        Bearer headers and GitHub, refresh and API token shapes in the frozen context are refused before launch, because
        the context is sent to a model.
        """
        plan_path, _ = review_evidence_files(tmp_path)
        _replace_context_bytes(plan_path, 0, b"\n" + payload)
        factory = _fake_public_processes(monkeypatch, _launches_for_plan(plan_path))

        with pytest.raises(_adapter().ReviewRouteError, match="plan-context-secret-or-role-prefix-invalid"):
            _adapter().check_host(plan_path, Path("codex"), 10)

        assert factory.processes == []

    @pytest.mark.parametrize(
        ("tamper", "match"),
        [
            pytest.param(
                _on_first_node(lambda plan, node: plan.pop("source_path")),
                "plan-source-path-invalid",
                id="missing-source-binding",
            ),
            pytest.param(
                _drop_source_from_context, "plan-context-source-or-diff-incomplete", id="partial-source-context"
            ),
            pytest.param(_overwrite_source_file, "plan-source-sha256-mismatch", id="stale-source-snapshot"),
            pytest.param(
                _tamper_source_content_and_rebind_digest, "plan-source-snapshot-invalid", id="invalid-source-snapshot"
            ),
            pytest.param(
                _on_first_node(lambda plan, node: node["capacity_receipt"].update(source_sha256="0" * 64)),
                "plan-capacity-receipt-binding-invalid",
                id="stale-capacity-source-binding",
            ),
            pytest.param(
                _on_first_node(lambda plan, node: node["capacity_receipt"].pop("capacity_evidence_path")),
                "plan-capacity-receipt-fields-invalid",
                id="missing-capacity-evidence",
            ),
            pytest.param(
                lambda plan_path: _replace_capacity_evidence(plan_path, b"{}"),
                "plan-capacity-evidence-invalid",
                id="malformed-capacity-evidence",
            ),
            pytest.param(
                _on_first_node(lambda plan, node: node["capacity_receipt"].update(input_tokens=True)),
                "plan-capacity-receipt-types-invalid",
                id="boolean-capacity-value",
            ),
            pytest.param(
                _on_first_node(lambda plan, node: node["capacity_receipt"].update(instruction_reserve_tokens=0)),
                "plan-capacity-receipt-values-invalid",
                id="zero-instruction-reserve",
            ),
            pytest.param(
                lambda plan_path: _claim_capacity(
                    plan_path, supported=299, default_window=299, percent=100, receipt={}, node_fields={}
                ),
                "plan-capacity-admission-insufficient",
                id="insufficient-capacity-headroom",
            ),
            pytest.param(
                lambda plan_path: _claim_window_percent(
                    plan_path,
                    context_size=375_000,
                    reserves=(100_000, 1_000),
                    extra_node={"model_context_window": 500_000},
                ),
                "plan-capacity-admission-insufficient",
                id="requested-window-percent",
            ),
            pytest.param(
                lambda plan_path: _claim_window_percent(
                    plan_path, context_size=200_000, reserves=(60_000, 20_000), extra_node={}
                ),
                "plan-capacity-admission-insufficient",
                id="observed-default-window-percent",
            ),
        ],
    )
    def test_rejects_unbound_source_or_capacity_receipt_before_process_launch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: Callable[[Path], None], match: str
    ) -> None:
        """Reject incomplete source snapshots and unauditable capacity claims before host startup.

        Each case damages one binding between plan, source snapshot, context and capacity receipt (or claims a window
        the observed capacity cannot honour); the host check must fail before any reviewer process starts.
        """
        plan_path, _ = review_evidence_files(tmp_path)
        tamper(plan_path)
        factory = _fake_public_processes(monkeypatch, [])

        with pytest.raises(_adapter().ReviewRouteError, match=match):
            _adapter().check_host(plan_path, Path("codex"), 10)

        assert factory.processes == []

    def test_installs_posix_cleanup_doubles_when_host_apis_are_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Keep public host checks portable when the native test platform lacks POSIX process APIs.

        With killpg and the signal constants removed (as on Windows) the host check still runs, because the test helper
        supplies doubles for the missing POSIX surface.
        """
        adapter = _adapter()
        monkeypatch.delattr(adapter.os, "killpg", raising=False)
        monkeypatch.delattr(adapter.signal, "SIGTERM", raising=False)
        monkeypatch.delattr(adapter.signal, "SIGKILL", raising=False)

        assert not hasattr(adapter.os, "killpg")
        plan_path, _ = review_evidence_files(tmp_path)
        launches = _check_launches(plan_path)
        factory = _fake_public_processes(monkeypatch, launches)

        summary = adapter.check_host(plan_path, Path("codex"), 10)

        assert summary["model_invoked"] is False
        assert hasattr(adapter.os, "killpg")
        requests = _request_methods(factory)
        assert "turn/start" not in requests

    def test_reports_only_a_sanitized_trailing_notification_method(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Keep a rejected post-cleanup notification diagnosable without retaining its payload.

        An unexpected notification arriving after cleanup fails the check; the error names only its method, never the
        params that carried a secret.
        """
        plan_path, _ = review_evidence_files(tmp_path)
        notification = {"jsonrpc": "2.0", "method": "thread/unknown", "params": {"secret": "discarded"}}
        _fake_public_processes(monkeypatch, _check_launches_with_frame(plan_path, notification))

        with pytest.raises(_adapter().ReviewRouteError, match="app-server-events-after-cleanup:thread/unknown"):
            _adapter().check_host(plan_path, Path("codex"), 10)

    @pytest.mark.parametrize(
        "notification",
        [
            pytest.param({"method": "account/updated"}, id="account-update"),
            pytest.param(
                {
                    "jsonrpc": "2.0",
                    "method": "remoteControl/status/changed",
                    "params": {"status": "disabled", "identities": [{"ignored": "identity"}]},
                },
                id="disabled-remote-control-status",
            ),
        ],
    )
    def test_accepts_harmless_notification_after_cleanup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, notification: dict[str, object]
    ) -> None:
        """Ignore a schema-defined account update or disabled remote-control status once the host has exited.

        Only the disabled control status is allowed, and no status payload is retained.
        """
        plan_path, _ = review_evidence_files(tmp_path)
        _fake_public_processes(monkeypatch, _check_launches_with_frame(plan_path, notification))

        assert _adapter().check_host(plan_path, Path("codex"), 10)["model_invoked"] is False

    def test_cleanup_rejects_account_update_with_request_id(self) -> None:
        """Keep server requests out of the account notification allowance.

        A frame carrying an `id` is a request the host expects an answer to, not a notification, even with a harmless
        method.
        """
        assert not _adapter()._is_harmless_lifecycle({"id": 1, "method": "account/updated"})

    @pytest.mark.parametrize(
        "params",
        [
            pytest.param({"status": "connecting"}, id="connecting"),
            pytest.param({"status": "connected"}, id="connected"),
            pytest.param({"status": "errored"}, id="errored"),
            pytest.param({}, id="missing-status"),
        ],
    )
    def test_rejects_enabled_or_unknown_remote_control_status(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, params: dict[str, str]
    ) -> None:
        """Stop host preparation when remote control is not authoritatively disabled.

        Connecting, connected, errored and a status-less notification are not proof of disabled, so none may pass and no
        turn starts.
        """
        plan_path, _ = review_evidence_files(tmp_path)
        notification = {"jsonrpc": "2.0", "method": "remoteControl/status/changed", "params": params}
        factory = _fake_public_processes(monkeypatch, _check_launches_with_frame(plan_path, notification))

        with pytest.raises(_adapter().ReviewRouteError, match="app-server-remote-control-status-invalid"):
            _adapter().check_host(plan_path, Path("codex"), 10)

        assert "turn/start" not in _request_methods(factory)

    def test_cleans_up_when_initialize_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Terminate the just-started host if initialization cannot produce a result.

        The initialize call returns an error, so exactly one process was started and it is torn down.
        """
        plan_path, _ = review_evidence_files(tmp_path)
        factory = _fake_public_processes(monkeypatch, [[{"jsonrpc": "2.0", "id": 1, "error": {}}]])

        with pytest.raises(_adapter().ReviewRouteError, match="app-server-request-failed:initialize"):
            _adapter().check_host(plan_path, Path("codex"), 10)

        assert len(factory.processes) == 1


class TestContextDelivery:
    """Delivery of large reviewer contexts: direct turn input versus acknowledged history preload."""

    def test_at_character_ceiling_is_sent_directly(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A context exactly at the 1,048,576-character CLI ceiling is delivered inside the turn input, no preload.

        The boundary matters because one extra character switches delivery to history preloading, which needs a
        different host method and an acknowledged evidence record.
        """
        _, context, factory, evidence = _run_with_first_context_of_length(tmp_path, monkeypatch, 1_048_576)

        requests = _requests(factory)
        turns = [request for request in requests if request["method"] == "turn/start"]
        assert [request for request in requests if request["method"] == "thread/inject_items"] == []
        assert turns[0]["params"]["input"] == [{"type": "text", "text": context}]
        assert "context_delivery" not in evidence["nodes"][0]

    def test_over_character_ceiling_is_preloaded_before_first_turn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A context one character over the ceiling is injected whole into history before any turn starts.

        The retained evidence must bind that delivery to the same context digest and mark it acknowledged.
        """
        plan, context, factory, evidence = _run_with_first_context_of_length(tmp_path, monkeypatch, 1_048_577)

        requests = _requests(factory)
        turns = [request for request in requests if request["method"] == "turn/start"]
        preloads = [request for request in requests if request["method"] == "thread/inject_items"]
        assert len(preloads) == 1
        assert preloads[0]["params"] == {
            "threadId": "thread-0",
            "items": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": context}]}],
        }
        assert requests.index(preloads[0]) < requests.index(turns[0])
        assert evidence["nodes"][0]["context_delivery"] == {
            "method": "thread/inject_items",
            "context_sha256": plan["nodes"][0]["context_sha256"],
            "acknowledged": True,
        }
        assert len(turns[0]["params"]["input"][0]["text"]) < 1_048_576

    def test_failed_later_preload_prevents_every_turn(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Reject unsupported history loading for any reviewer before paid turns can begin.

        Only the second reviewer's context needs preloading and the host does not support it; no turn starts for either
        reviewer and the evidence records zero dispatched turns.
        """
        plan_path, _ = review_evidence_files(tmp_path)
        _replace_context_bytes(plan_path, 1, b"x" * 1_048_576)
        launches = _launches_for_plan(plan_path)
        launches[1] = [*launches[1][:5], {"id": 5, "error": {"code": -32601, "message": "Method not found"}}]
        factory = _fake_public_processes(monkeypatch, launches)

        with pytest.raises(_adapter().ReviewRouteError, match="app-server-request-failed:thread/inject_items"):
            _adapter().run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)

        requests = _request_methods(factory)
        assert "turn/start" not in requests
        evidence = json.loads((tmp_path / "review-output/evidence.json").read_bytes())
        assert evidence["turn_dispatch"] == {"attempted": 0, "acknowledged": 0}
        assert evidence["failure_phase"] == "context-preload"
        assert evidence["failure_diagnostic"]["reason"] == "method-not-supported"

    @pytest.mark.parametrize(
        "tamper",
        [
            pytest.param(lambda node: node.pop("context_delivery"), id="missing"),
            pytest.param(lambda node: node["context_delivery"].update(context_sha256="0" * 64), id="digest"),
            pytest.param(lambda node: node["context_delivery"].update(method="turn/start"), id="method"),
            pytest.param(lambda node: node["context_delivery"].update(acknowledged=False), id="false"),
            pytest.param(lambda node: node["context_delivery"].update(acknowledged=1), id="integer"),
            pytest.param(lambda node: node["context_delivery"].update(unverified=True), id="extra"),
        ],
    )
    def test_large_context_evidence_requires_exact_preload_binding(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: Callable[[dict[str, object]], object]
    ) -> None:
        """Reject large-context evidence lacking a precise acknowledged same-context delivery record.

        A real preloaded run supplies valid evidence; each case then damages the delivery record (missing, wrong digest
        or method, non-boolean acknowledgement, extra key) and validation must refuse it.
        """
        plan_path, _ = review_evidence_files(tmp_path)
        _replace_context_bytes(plan_path, 0, b"x" * 1_048_576)
        _fake_public_processes(monkeypatch, _launches_for_plan(plan_path, echo_input=True))
        evidence_path = _adapter().run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)
        evidence = json.loads(evidence_path.read_bytes())
        tamper(evidence["nodes"][0])
        _write_json(evidence_path, evidence)

        with pytest.raises(_adapter().ReviewRouteError, match="evidence-context-delivery-mismatch"):
            _adapter().validate_evidence(plan_path, evidence_path, CANONICAL_ROLES)


class TestCapacityAdmission:
    """Capacity receipts, byte-bound admission and the optional model context window request."""

    @pytest.mark.parametrize(
        ("tokenizer", "measure"),
        [
            pytest.param("utf8-byte-upper-bound", lambda raw: len(raw) - 1, id="understated"),
            pytest.param("utf8-byte-upper-bound", lambda raw: len(raw) + 1, id="overstated"),
            pytest.param("operator-proxy", len, id="unknown-method"),
            pytest.param("utf8-byte-upper-bound", lambda raw: len(raw.decode("utf-8")), id="unicode-character-count"),
        ],
    )
    def test_recomputes_capacity_input_before_process_launch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tokenizer: str, measure: Callable[[bytes], int]
    ) -> None:
        """Reject forged measurements even when exact context/source digests are correct.

        The context holds multi-byte Unicode so a character count differs from the UTF-8 byte bound the adapter
        recomputes; under- and over-stated counts and an unrecognised tokenizer name must all be refused.
        """
        plan_path, _ = review_evidence_files(tmp_path)
        _replace_context(plan_path, 0, "café 😀")
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        node = plan["nodes"][0]
        context = (plan_path.parent / node["context_path"]).read_bytes()
        node["capacity_receipt"].update(tokenizer=tokenizer, input_tokens=measure(context))
        _write_json(plan_path, plan)
        factory = _fake_public_processes(monkeypatch, [])

        with pytest.raises(_adapter().ReviewRouteError, match="plan-capacity-input-measurement-invalid"):
            _adapter().check_host(plan_path, Path("codex"), 10)

        assert factory.processes == []

    @pytest.mark.parametrize("suffix", ["ASCII", "café 😀"])
    def test_admits_window_exactly_equal_to_byte_bound_plus_reserves(self, tmp_path: Path, suffix: str) -> None:
        """A selected window equal to the recomputed byte bound plus both reserves is admitted.

        The multi-byte suffix proves the bound counts UTF-8 bytes, not characters.
        """
        plan_path, _ = review_evidence_files(tmp_path)
        _plan_with_window_headroom(plan_path, suffix, 0)

        validated, _ = _adapter()._validated_plan(plan_path, CANONICAL_ROLES)

        assert validated["nodes"][0]["model_context_window"] > 0

    @pytest.mark.parametrize("suffix", ["ASCII", "café 😀"])
    def test_rejects_window_one_token_short_of_byte_bound_plus_reserves(self, tmp_path: Path, suffix: str) -> None:
        """A selected window one token below the recomputed byte bound plus reserves is refused.

        Paired with the exact-fit case, this pins the admission boundary to the token.
        """
        plan_path, _ = review_evidence_files(tmp_path)
        _plan_with_window_headroom(plan_path, suffix, -1)

        with pytest.raises(_adapter().ReviewRouteError, match="plan-capacity-admission-insufficient"):
            _adapter()._validated_plan(plan_path, CANONICAL_ROLES)

    @pytest.mark.parametrize("value", [True, 0, -1, "500000", 1.5])
    def test_rejects_invalid_optional_model_context_window_before_process_launch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: object
    ) -> None:
        """Allow only positive integer context-window requests in a frozen reviewer node.

        Booleans, zero, negatives, numeric strings and floats are not valid window requests; each is refused before any
        process starts.
        """
        plan_path, _ = review_evidence_files(tmp_path)
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["nodes"][0]["model_context_window"] = value
        _write_json(plan_path, plan)
        factory = _fake_public_processes(monkeypatch, _launches_for_plan(plan_path))

        with pytest.raises(_adapter().ReviewRouteError, match="plan-model-context-window-invalid"):
            _adapter().check_host(plan_path, Path("codex"), 10)

        assert factory.processes == []

    def test_optional_model_context_window_is_forwarded_only_in_thread_start_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Forward an optional request without changing the observed model metadata contract.

        A 500k window request appears in the thread-start config for both the host check and the review run, never in
        turn- start parameters, and observed model metadata is unchanged.
        """
        plan_path, _ = review_evidence_files(tmp_path)
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["nodes"][0]["model_context_window"] = 500_000
        _write_json(plan_path, plan)
        check_launches = _launches_for_plan(plan_path)
        check_launches[1] = check_launches[1][:5]
        check_factory = _fake_public_processes(monkeypatch, check_launches)

        summary = _adapter().check_host(plan_path, Path("codex"), 10)

        check_requests = _requests(check_factory)
        check_thread = next(request for request in check_requests if request["method"] == "thread/start")
        assert check_thread["params"]["config"] == {"model_reasoning_effort": "high", "model_context_window": 500_000}
        assert summary["nodes"][0]["observed_controls"]["model"] == plan["nodes"][0]["model"]

        run_factory = _fake_public_processes(monkeypatch, _launches_for_plan(plan_path))
        _adapter().run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)

        run_requests = _requests(run_factory)
        run_thread = next(request for request in run_requests if request["method"] == "thread/start")
        assert run_thread["params"]["config"] == {"model_reasoning_effort": "high", "model_context_window": 500_000}
        assert (
            "model_context_window"
            not in next(request for request in run_requests if request["method"] == "turn/start")["params"]
        )


class TestDispatchAdmission:
    """Source snapshot, role-card and legacy-plan binding enforced before and between reviewer turns."""

    @pytest.mark.parametrize(
        ("amend", "expected_error"),
        [
            pytest.param(lambda source: source.update(files=[]), "plan-source-scope-coverage-invalid", id="empty"),
            pytest.param(
                lambda source: source.update(scope_paths=["missing.py", "widget.py"]),
                "plan-source-scope-coverage-invalid",
                id="missing-scope",
            ),
            pytest.param(
                lambda source: source.update(files=[dict(source["files"][0], path="extra.py"), *source["files"]]),
                "plan-source-scope-coverage-invalid",
                id="extra-record",
            ),
            pytest.param(
                lambda source: source.update(files=[dict(source["files"][0], path="other.py")]),
                "plan-source-scope-coverage-invalid",
                id="substituted-record",
            ),
            pytest.param(
                lambda source: source.update(scope_paths=["."]), "plan-source-snapshot-invalid", id="directory-scope"
            ),
            pytest.param(
                lambda source: source.update(scope_paths=["."], files=[dict(source["files"][0], path=".")]),
                "plan-source-snapshot-invalid",
                id="forged-directory-file",
            ),
            pytest.param(
                lambda source: source.update(
                    scope_paths=["package/"], files=[dict(source["files"][0], path="package/")]
                ),
                "plan-source-snapshot-invalid",
                id="forged-directory-slash",
            ),
        ],
    )
    def test_source_inventory_must_match_explicit_file_scopes(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        amend: Callable[[dict[str, object]], object],
        expected_error: str,
    ) -> None:
        """Reject incomplete inventories before host launch and when accepting retained review evidence.

        The frozen source snapshot must list exactly the requested file scopes; each case empties, pads, substitutes or
        forges the inventory, and both the host check and evidence validation must refuse it.
        """
        plan_path, evidence_path = review_evidence_files(tmp_path)
        plan = json.loads(plan_path.read_bytes())
        source = json.loads((plan_path.parent / plan["source_path"]).read_bytes())
        amend(source)
        _bind_source_snapshot(plan_path, source)
        factory = _fake_public_processes(monkeypatch, [])

        with pytest.raises(_adapter().ReviewRouteError, match=expected_error):
            _adapter().check_host(plan_path, Path("codex"), 10)
        with pytest.raises(_adapter().ReviewRouteError, match=expected_error):
            _adapter().validate_evidence(plan_path, evidence_path, CANONICAL_ROLES)

        assert factory.processes == []

    def test_rejects_existing_directory_claimed_as_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A forged file record cannot turn a real directory into a leaf scope.

        Unlike the inventory cases, `package` really exists on disk as a directory in the named repository, so the
        adapter must reject the claim by inspecting the checkout rather than only the snapshot text.
        """
        plan_path, _ = review_evidence_files(tmp_path)
        repository = tmp_path / "repository"
        (repository / "package").mkdir(parents=True)
        plan = json.loads(plan_path.read_bytes())
        source = json.loads((plan_path.parent / plan["source_path"]).read_bytes())
        source.update(repository=repository.as_posix(), scope_paths=["package"])
        source["files"][0]["path"] = "package"
        _bind_source_snapshot(plan_path, source)
        factory = _fake_public_processes(monkeypatch, [])

        with pytest.raises(_adapter().ReviewRouteError, match="plan-source-snapshot-invalid"):
            _adapter().check_host(plan_path, Path("codex"), 10)
        assert factory.processes == []

    @pytest.mark.skipif(not DIRECTORY_SYMLINKS_AVAILABLE, reason="filesystem cannot create directory symlinks")
    def test_accepts_tracked_symlink_to_in_repository_directory(self, tmp_path: Path) -> None:
        """Keep a tracked symlink leaf valid when its directory target stays inside the checkout.

        A real Git checkout tracks a symlink to a directory inside it; the captured snapshot records a symlink leaf and
        plan validation for dispatch accepts it.
        """
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
        assert spec is not None
        assert spec.loader is not None
        collector = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(collector)
        source = collector.capture_source_snapshot(repository, ["directory-link"])
        assert source["files"][0]["kind"] == "symlink"
        _bind_source_snapshot(plan_path, source)

        validated, _ = _adapter()._validated_plan(plan_path, CANONICAL_ROLES, require_dispatch=True)

        assert validated["_source_bytes"] == (plan_path.parent / validated["source_path"]).read_bytes()

    @pytest.mark.parametrize(
        "tamper",
        [
            pytest.param(_forge_snapshot_content, id="forged-snapshot"),
            pytest.param(_change_live_file, id="stale-live-file"),
        ],
    )
    def test_rejects_snapshot_that_differs_from_live_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: Callable[[Path, Path], None]
    ) -> None:
        """Reject internally consistent frozen bytes that do not describe the review checkout.

        A real local Git checkout backs the plan; either the snapshot is forged or the live file changes, and the host
        check must refuse before any reviewer process starts.
        """
        plan_path, repository = _live_source_plan(tmp_path)
        _adapter()._validated_plan(plan_path, CANONICAL_ROLES, require_dispatch=True)
        tamper(plan_path, repository)
        factory = _fake_host_with_git(monkeypatch, _check_launches(plan_path))

        with pytest.raises(_adapter().ReviewRouteError, match="live-source-mutated-before-host"):
            _adapter().check_host(plan_path, Path("codex"), 10)

        assert factory.processes == []

    def test_rejects_snapshot_from_another_repository(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The snapshot repository must be the checkout selected for reviewer turns.

        A decoy repository with identical file content supplies a self-consistent snapshot; the plan's working directory
        is another checkout, so dispatch must refuse it.
        """
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
        factory = _fake_host_with_git(monkeypatch, _check_launches(plan_path))

        with pytest.raises(_adapter().ReviewRouteError, match="live-source-root-mismatch-before-host"):
            _adapter().check_host(plan_path, Path("codex"), 10)

        assert factory.processes == []

    def test_rechecks_live_source_after_host_setup_before_turn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A checkout change during host setup must stop every paid reviewer turn.

        The checkout file changes the moment the second host process starts, so the pre-turn source recheck must stop
        the run before any turn/start request.
        """
        plan_path, repository = _live_source_plan(tmp_path)
        _adapter()._validated_plan(plan_path, CANONICAL_ROLES, require_dispatch=True)
        factory = _fake_host_with_git(monkeypatch, _launches_for_plan(plan_path))
        launch = subprocess.Popen

        def change_source_on_host_launch(*args: object, **kwargs: object) -> _FakeProcess:
            """Change only the live leaf after the restricted host process starts."""
            process = launch(*args, **kwargs)
            if len(factory.processes) == 1:
                (repository / "widget.py").write_text("VALUE = 2\n", encoding="utf-8", newline="\n")
            return process

        monkeypatch.setattr(subprocess, "Popen", change_source_on_host_launch)

        with pytest.raises(_adapter().ReviewRouteError, match="live-source-mutated-before-turn"):
            _adapter().run_review(plan_path, tmp_path / "live-source-review", Path("codex"), 10)

        requests = _request_methods(factory)
        assert "turn/start" not in requests

    def test_rechecks_live_source_after_reviewer_turn(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A file changed during review cannot produce accepted completion evidence.

        The checkout changes right after the first turn is sent; the recheck before the next reviewer turn must reject
        the run (`live-source-mutated-before-turn`) so changed code cannot yield completion evidence.
        """
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
            if len(factory.processes) == 1:
                process.stdin = SourceChangingStdin()
            return process

        monkeypatch.setattr(subprocess, "Popen", launch_with_source_change)

        with pytest.raises(_adapter().ReviewRouteError, match="live-source-mutated-before-turn"):
            _adapter().run_review(plan_path, tmp_path / "post-turn-source-review", Path("codex"), 10)

        assert changed
        requests = _request_methods(factory)
        assert "turn/start" in requests

    def test_rechecks_frozen_role_bytes_before_turn_start(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reject role-card drift after thread setup and before a paid reviewer turn starts.

        After thread setup the role-card read returns different bytes; the run fails before turn/start so a paid turn
        never runs with drifted instructions.
        """
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

        requests = _request_methods(factory)
        assert "turn/start" not in requests

    @pytest.mark.parametrize(
        ("material", "reason"),
        [
            pytest.param("source", "source-or-diff", id="source-snapshot"),
            pytest.param("capacity", "capacity-evidence", id="sibling-capacity-evidence"),
        ],
    )
    def test_rechecks_admission_before_turn_start(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, material: str, reason: str
    ) -> None:
        """Reject actual evidence-file drift at the host boundary before any paid reviewer turn.

        The source snapshot or the sibling capacity evidence file is overwritten when thread setup is sent; the recheck
        catches it before any turn.
        """
        adapter = _adapter()
        plan_path, _ = review_evidence_files(tmp_path)
        factory = _fake_public_processes(monkeypatch, _launches_for_plan(plan_path))
        plan = json.loads(plan_path.read_bytes())
        relative = (
            plan["source_path"]
            if material == "source"
            else plan["nodes"][-1]["capacity_receipt"]["capacity_evidence_path"]
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

        requests = _request_methods(factory)
        assert "turn/start" not in requests

    def test_legacy_plan_stays_readable_as_evidence_but_dispatch_rejects_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Preserve historical evidence inspection while preventing legacy plans from launching new work.

        A schema-1 plan (no source, diff or capacity receipts) with historical role cards must still validate as
        evidence, yet `check_host` must refuse it before any process starts.
        """
        plan_path, evidence_path = review_evidence_files(tmp_path)
        legacy_dropped = {"source_path", "source_sha256", "diff_path", "diff_sha256"}
        plan = {
            key: value
            for key, value in json.loads(plan_path.read_text(encoding="utf-8")).items()
            if key not in legacy_dropped
        }
        plan["schema_version"] = 1
        plan["nodes"] = [
            {key: value for key, value in node.items() if key != "capacity_receipt"} for node in plan["nodes"]
        ]
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        historical_roles = _freeze_historical_role_cards(tmp_path, plan_path, plan, evidence)
        _write_json(plan_path, plan)
        evidence["plan_sha256"] = _sha256(plan_path.read_bytes())
        _write_json(evidence_path, evidence)

        assert _adapter().validate_evidence(plan_path, evidence_path, historical_roles)["consumer_id"] == "code-review"
        factory = _fake_public_processes(monkeypatch, [])

        with pytest.raises(_adapter().ReviewRouteError, match="plan-legacy-dispatch-forbidden"):
            _adapter().check_host(plan_path, Path("codex"), 10)

        assert factory.processes == []


class TestRunReviewLifecycle:
    """Reviewer turn dispatch, terminal answers, turn identity and failure evidence for a full review run."""

    def test_dispatches_all_turns_and_persists_completed_sibling(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Start every reviewer turn before waiting and retain both terminal-answer responses.

        Both reviewers' turn/start requests go out after thread setup, and each reviewer's final answer is saved under
        its role name.
        """
        plan_path, _ = review_evidence_files(tmp_path)
        factory = _fake_public_processes(monkeypatch, _launches_for_plan(plan_path))
        output = tmp_path / "review-output"

        evidence_path = _adapter().run_review(plan_path, output, Path("codex"), 10)

        assert evidence_path == output / "evidence.json"
        assert (output / "challenger.md").read_text(encoding="utf-8") == _review_answer(plan_path)
        assert (output / "cicd-steward.md").read_text(encoding="utf-8") == _review_answer(plan_path)
        requests = _request_methods(factory)
        assert requests.index("turn/start") > requests.index("thread/start")
        assert requests.count("turn/start") == 2

    def test_preserves_terminal_output_when_sibling_requests_approval(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail closed on an approval request without deleting a prior completed response.

        Reviewer one finishes; reviewer two asks to run a command. The run fails closed, but the finished answer file
        stays and evidence says failed.
        """
        plan_path, _ = review_evidence_files(tmp_path)
        _fake_public_processes(monkeypatch, _launches_for_plan(plan_path, fail_second=True))
        output = tmp_path / "review-output"

        with pytest.raises(_adapter().ReviewRouteError, match="app-server-approval-requested"):
            _adapter().run_review(plan_path, output, Path("codex"), 10)

        assert (output / "challenger.md").read_text(encoding="utf-8") == _review_answer(plan_path)
        assert json.loads((output / "evidence.json").read_text(encoding="utf-8"))["status"] == "failed"

    def test_accepts_schema_terminal_final_answer_phase(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Accept only the generated-schema terminal assistant-message phase.

        This is the baseline for the rejected phases below: the `final_answer` phase completes the run.
        """
        plan_path, _ = review_evidence_files(tmp_path)
        _fake_public_processes(monkeypatch, _launches_with_agent_message_phase(plan_path, "final_answer"))

        evidence_path = _adapter().run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)

        assert json.loads(evidence_path.read_text(encoding="utf-8"))["status"] == "completed"

    @pytest.mark.parametrize("phase", ["commentary", "final", None])
    def test_rejects_nonterminal_agent_message_phase(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str | None
    ) -> None:
        """Reject commentary, obsolete final, and unknown phases as terminal answers.

        Commentary, the obsolete final phase and a missing phase each leave the run without a terminal answer.
        """
        plan_path, _ = review_evidence_files(tmp_path)
        _fake_public_processes(monkeypatch, _launches_with_agent_message_phase(plan_path, phase))

        with pytest.raises(_adapter().ReviewRouteError, match="app-server-turn-final-missing"):
            _adapter().run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)

    def test_separates_noncompleted_turn_status(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Report a terminal status mismatch separately from final-answer absence.

        A turn that ends with status failed is reported as a status error, not as a missing final answer, so triage
        points at the right cause.
        """
        plan_path, _ = review_evidence_files(tmp_path)
        launches = _launches_for_plan(plan_path)
        completion = next(frame for frame in launches[1] if frame.get("method") == "turn/completed")
        completion["params"]["turn"]["status"] = "failed"
        _fake_public_processes(monkeypatch, launches)

        with pytest.raises(_adapter().ReviewRouteError, match="app-server-turn-status-invalid"):
            _adapter().run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)

    @pytest.mark.parametrize(
        "wrong_identity",
        [
            pytest.param({"turnId": "other-turn"}, id="turnId"),
            pytest.param({"turn": {"id": "other-turn", "status": "completed"}}, id="turn.id"),
        ],
    )
    @pytest.mark.parametrize("method", ["turn/completed", "item/agentMessage/delta", "turn/plan/updated"])
    def test_rejects_conflicting_turn_ids(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wrong_identity: dict[str, object], method: str
    ) -> None:
        """Reject contradictory turn identities instead of accepting whichever matches.

        An event naming the active turn in one identity field and another turn in the other field arrives just before
        the real completion; whichever field matches, the run must fail rather than accept it.
        """
        plan_path, _ = review_evidence_files(tmp_path)
        launches = _launches_for_plan(plan_path)
        completion = _first_frame(launches[1], "turn/completed")
        event = {"method": method, "params": {**completion["params"], "turnId": "turn-0", **wrong_identity}}
        launches[1].insert(launches[1].index(completion), event)
        _fake_public_processes(monkeypatch, launches)
        output = tmp_path / "review-output"

        with pytest.raises(_adapter().ReviewRouteError, match="app-server-thread-or-turn-mismatch"):
            _adapter().run_review(plan_path, output, Path("codex"), 10)

        evidence = json.loads((output / "evidence.json").read_text(encoding="utf-8"))
        assert evidence["status"] == "failed"
        assert evidence["failure_code"] == "app-server-thread-or-turn-mismatch"
        assert not (output / "challenger.md").exists()

    def test_accepts_agreeing_completion_turn_ids(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Accept redundant matching identities instead of rejecting every dual-ID frame.

        Both reviewers' completion frames name their turn twice with the same value; the run still completes.
        """
        plan_path, _ = review_evidence_files(tmp_path)
        launches = _launches_for_plan(plan_path)
        _repeat_turn_id_in_completions(launches[1])
        _fake_public_processes(monkeypatch, launches)

        evidence_path = _adapter().run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)

        assert json.loads(evidence_path.read_text(encoding="utf-8"))["status"] == "completed"

    @pytest.mark.parametrize("method", ["item/started", "item/completed"])
    @pytest.mark.parametrize("item", [None, pytest.param([], id="array"), "invalid"])
    def test_rejects_nonobject_lifecycle_item(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, item: object, method: str
    ) -> None:
        """Reject malformed lifecycle payloads even when valid final events follow.

        Item start or completion events whose item is null, an array or a string fail closed with a specific code, and
        no output is promoted.
        """
        plan_path, _ = review_evidence_files(tmp_path)
        launches = _launches_for_plan(plan_path)
        first_final = _first_index(launches[1], "item/completed")
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
    def test_rejects_events_for_completed_turn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str
    ) -> None:
        """Freeze a completed review while its sibling continues producing events.

        After the first reviewer's turn completes, a late delta, plan update or second completion for it is rejected;
        the earlier answer is kept and the second reviewer's output is never written.
        """
        plan_path, _ = review_evidence_files(tmp_path)
        launches = _launches_for_plan(plan_path)
        terminal = _first_index(launches[1], "turn/completed")
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

    @pytest.mark.parametrize(
        "collide",
        [pytest.param(_collide_thread_identity, id="thread"), pytest.param(_collide_turn_identity, id="turn")],
    )
    def test_records_failed_final_identity_validation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, collide: Callable[[list[dict[str, object]]], None]
    ) -> None:
        """Keep rejected final evidence from advertising completed reviewer execution.

        Two reviewers that share a thread id or a turn id finish their turns, so output files exist, but the final
        evidence check refuses the duplicate identity and records a failed run.
        """
        plan_path, _ = review_evidence_files(tmp_path)
        launches = _launches_for_plan(plan_path)
        collide(launches[1])
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

    def test_records_output_validation_failure_after_cleanup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Record final output rejection without rerunning cleanup or hiding its cause.

        The first output file is emptied while cleanup runs; the final evidence check then fails, cleanup stays recorded
        as completed, and it ran once per host process.
        """
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
        assert len(cleanup_calls) == 1
        assert (output / "cicd-steward.md").read_text(encoding="utf-8") == _review_answer(plan_path)


class TestRunReviewOutputRoot:
    """Containment and setup failures of the review output directory."""

    def test_rejects_output_outside_plan_parent_before_host_start(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Keep the new output tree inside the frozen plan's parent directory.

        An output directory outside the plan's folder is refused before it is created and before any process starts.
        """
        plan_path, _ = review_evidence_files(tmp_path)
        output = tmp_path.parent / f"{tmp_path.name}-outside-output"
        factory = _fake_public_processes(monkeypatch, _launches_for_plan(plan_path))

        with pytest.raises(_adapter().ReviewRouteError, match="output-root-outside-plan"):
            _adapter().run_review(plan_path, output, Path("codex"), 10)

        assert not output.exists()
        assert factory.processes == []

    @pytest.mark.skipif(not DIRECTORY_SYMLINKS_AVAILABLE, reason="filesystem cannot create directory symlinks")
    def test_rejects_output_parent_symlink_escape_before_creation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Refuse a new output beneath an in-plan symlink that resolves outside the frozen run root.

        A symlink inside the plan folder pointing outside would let a nested output escape; the resolved path is checked
        and nothing is created beyond the link.
        """
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

    def test_preserves_nested_output_under_plan_parent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Retain supported creation of a new nested output directory inside the run root.

        A new nested output path inside the run root is created on demand and holds evidence.json.
        """
        plan_path, _ = review_evidence_files(tmp_path)
        output = tmp_path / "nested" / "review-output"
        _fake_public_processes(monkeypatch, _launches_for_plan(plan_path))

        evidence_path = _adapter().run_review(plan_path, output, Path("codex"), 10)

        assert evidence_path == output / "evidence.json"

    def test_wraps_output_directory_setup_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Return a bounded route error when the new contained output directory cannot be created.

        Creating the output root is made to fail with OSError; the caller gets a bounded route error and no evidence
        file is left behind.
        """
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

    def test_persists_setup_version_failure_after_output_creation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Write bounded failed evidence when version setup fails after the contained root exists.

        The codex version probe fails after the output directory exists; failed evidence for the preparation phase with
        zero dispatched turns is written and no host starts.
        """
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


class TestRunReviewUserMessage:
    """Validation of the host's echo of the frozen user-message context."""

    def test_separates_incomplete_user_message_lifecycle(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Report a missing echo completion separately from a missing reviewer final answer.

        The input-echo completions are dropped from the script; the run reports an incomplete user-message lifecycle,
        not a missing final answer.
        """
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

    def test_accepts_large_unicode_user_message_echo_before_turn_response(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Accept the exact large Unicode input echo that the host emits before acknowledging a turn.

        A 24,000-emoji context is echoed back before the turn response arrives, as the host does for large inputs, and
        the run still completes.
        """
        plan_path, _ = review_evidence_files(tmp_path)
        _replace_context(plan_path, 0, "😀" * 24_000)
        _fake_public_processes(monkeypatch, _launches_for_plan(plan_path, echo_input=True))

        _adapter().run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)

    def test_accepts_user_message_echo_with_empty_text_elements(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Accept the schema-defined empty UI span list without admitting altered input.

        The echo item carries the schema's empty text_elements list on its text part; the text itself is unchanged, so
        it is accepted.
        """
        plan_path, _ = review_evidence_files(tmp_path)
        launches = _launches_for_plan(plan_path, echo_input=True)
        echo = next(frame for frame in launches[1] if frame.get("method") == "item/started")
        echo["params"]["item"]["content"][0]["text_elements"] = []
        _fake_public_processes(monkeypatch, launches)

        _adapter().run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)

    def test_rejects_mismatched_user_message_echo(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Reject a user-message echo whose full text is not the frozen context sent to that turn.

        The echoed text differs from what was sent; failure evidence records the turn-events phase, both turns as
        dispatched, and cleanup as failed.
        """
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

    @pytest.mark.parametrize(
        "corrupt",
        [
            pytest.param(
                lambda frames: _first_frame(frames, "item/completed", "userMessage")["params"]["item"]["content"][
                    0
                ].update(text="altered completion"),
                id="completion-text-differs-from-start",
            ),
            pytest.param(_duplicate_user_message_completion, id="duplicate-completion"),
            pytest.param(
                lambda frames: _first_frame(frames, "item/started")["params"]["item"].update(
                    content=[{"type": "image", "url": "ignored"}]
                ),
                id="image-input",
            ),
            pytest.param(_append_extra_text_part_to_echo, id="extra-content-part"),
        ],
    )
    def test_rejects_unbound_user_message_lifecycle(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corrupt: Callable[[list[dict[str, object]]], object]
    ) -> None:
        """Reject a user-message lifecycle that is not exactly one bound text-context start and completion.

        A completion that differs from its start, a replayed completion, an image echo, and a text echo with an extra
        content part all go beyond the frozen context and must fail the user-message lifecycle.
        """
        plan_path, _ = review_evidence_files(tmp_path)
        launches = _launches_for_plan(plan_path, echo_input=True)
        corrupt(launches[1])
        _fake_public_processes(monkeypatch, launches)

        with pytest.raises(_adapter().ReviewRouteError, match="app-server-user-message-lifecycle-invalid"):
            _adapter().run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)

    def test_rejects_oversized_user_message_frame(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Reject a serialized input echo beyond the transport bound without retaining its text.

        An echoed message of NUL characters exceeds the per-frame byte cap after JSON escaping and is rejected on size
        before it is parsed or retained.
        """
        plan_path, _ = review_evidence_files(tmp_path)
        launches = _launches_for_plan(plan_path, echo_input=True)
        echo = next(frame for frame in launches[1] if frame.get("method") == "item/started")
        echo["params"]["item"]["content"][0]["text"] = "\x00" * (_adapter().MAX_EVENT_BYTES // 6 + 100)
        _fake_public_processes(monkeypatch, launches)

        with pytest.raises(_adapter().ReviewRouteError, match="app-server-frame-too-large"):
            _adapter().run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)


class TestRunReviewEvents:
    """Host notifications and RPC failures: what is consumed, what fails closed, what evidence may retain."""

    def test_failure_evidence_uses_static_reason_code(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Omit the request method from failure evidence while preserving it in the raised route error.

        The turn/start response is an empty error; the route error names the method while the stored evidence carries
        only the static failure code.
        """
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
                -32602,
                "Input exceeds the maximum length of 1048576 characters",
                "input-character-limit",
                id="input-limit",
            ),
            pytest.param(-32602, "private path /secret/account", "invalid-parameters", id="private-diagnostic"),
            pytest.param(-32601, "Method not found", "method-not-supported", id="unsupported-method"),
            pytest.param(
                True, "Input exceeds the maximum length of 1048576 characters", "unclassified", id="boolean-code"
            ),
            pytest.param(12345, "sk-" + "abcdefghijklmnopqrstuvwxyz012345", "unclassified", id="unknown-secret-error"),
        ],
    )
    def test_rpc_failure_retains_only_allowlisted_diagnostics(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: object, message: str, reason: str
    ) -> None:
        """Keep actionable RPC categories without copying provider messages, data or arbitrary codes.

        Each case injects a turn/start error with private data in its data and message fields; only a fixed set of
        categories survives in evidence, and the code is kept only for two known integers.
        """
        plan_path, _ = review_evidence_files(tmp_path)
        launches = _launches_for_plan(plan_path)
        launches[1] = [*launches[1][:5], {"id": 5, "error": {"code": code, "message": message, "data": "private-data"}}]
        _fake_public_processes(monkeypatch, launches)

        with pytest.raises(_adapter().ReviewRouteError, match="app-server-request-failed:turn/start"):
            _adapter().run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)

        evidence_text = (tmp_path / "review-output/evidence.json").read_text(encoding="utf-8")
        diagnostic = json.loads(evidence_text)["failure_diagnostic"]
        assert diagnostic["stage"] == "rpc-response"
        assert diagnostic["reason"] == reason
        assert diagnostic["method_category"] == "turn/start"
        assert diagnostic["rpc_code"] == (code if type(code) is int and code in {-32602, -32601} else None)
        assert "private-data" not in evidence_text
        assert message not in evidence_text
        assert set(diagnostic) == {"stage", "reason", "method_category", "rpc_code", "recovery"}

    @pytest.mark.parametrize(
        "notification",
        [
            pytest.param(
                {"jsonrpc": "2.0", "method": "remoteControl/status/changed", "params": {"status": "disabled"}},
                id="disabled-remote-control-status",
            ),
            pytest.param(
                {
                    "jsonrpc": "2.0",
                    "method": "account/rateLimits/updated",
                    "params": {"rateLimits": {"primary": {"usedPercent": 1}}},
                },
                id="rate-limit-update",
            ),
        ],
    )
    def test_consumes_documented_notification_before_turns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, notification: dict[str, object]
    ) -> None:
        """Consume a documented host notification in the public event loop without retaining its account data.

        The approved disabled remote-control status and the rolling rate-limit update arrive before the reviewer turns
        and must not interrupt the run.
        """
        plan_path, _ = review_evidence_files(tmp_path)
        launches = _launches_for_plan(plan_path)
        launches[1][2] = notification
        _fake_public_processes(monkeypatch, launches)

        _adapter().run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)

    @pytest.mark.parametrize(
        ("method", "params"),
        [
            pytest.param("warning", {"message": "Ignored warning."}, id="warning"),
            pytest.param("configWarning", {"summary": "Ignored configuration warning."}, id="config-warning"),
        ],
    )
    def test_discards_schema_warning_notification_before_turns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str, params: dict[str, str]
    ) -> None:
        """Discard only documented warning notifications without retaining their text.

        Warning and configWarning notifications before the turns are ignored and their text never appears in evidence.
        """
        plan_path, _ = review_evidence_files(tmp_path)
        launches = _launches_for_plan(plan_path)
        launches[1][2] = {"jsonrpc": "2.0", "method": method, "params": params}
        _fake_public_processes(monkeypatch, launches)

        evidence_path = _adapter().run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)

        assert params[next(iter(params))] not in evidence_path.read_text(encoding="utf-8")

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
    def test_discards_account_update_during_turn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, notification: dict[str, object]
    ) -> None:
        """A host account notification must not interrupt a read-only review turn.

        An account update arriving mid-turn, bare or with auth details, is ignored and the auth mode never reaches
        evidence.
        """
        plan_path, _ = review_evidence_files(tmp_path)
        launches = _launches_for_plan(plan_path)
        first_event = _first_index(launches[1], "item/completed")
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
    def test_rejects_unknown_event_method(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str) -> None:
        """Fail closed without retaining an event method outside the diagnostic allowlist.

        An execution-like event and a documented prefix with an extra segment are not on the allowlist; evidence holds a
        generic diagnostic and never the method string.
        """
        plan_path, _ = review_evidence_files(tmp_path)
        launches = _launches_for_plan(plan_path)
        first_event = _first_index(launches[1], "item/completed")
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
        ("event", "category", "secret"),
        [
            pytest.param(
                {
                    "jsonrpc": "2.0",
                    "method": "model/verification",
                    "params": {"sensitive": "do-not-retain-this-payload"},
                },
                "model/verification",
                "do-not-retain-this-payload",
                id="model-notification",
            ),
            pytest.param(
                {
                    "jsonrpc": "2.0",
                    "method": "item/autoApprovalReview/started",
                    "params": {"sensitive": "do-not-retain-this-payload"},
                },
                "item/autoApprovalReview/started",
                "do-not-retain-this-payload",
                id="approval-review",
            ),
            pytest.param(
                {"jsonrpc": "2.0", "method": {"sensitive": "do-not-retain-this-method"}, "params": {}},
                "unrecognized",
                "do-not-retain-this-method",
                id="nontext-method",
            ),
        ],
    )
    def test_classifies_rejected_event_without_retaining_its_content(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, event: dict[str, object], category: str, secret: str
    ) -> None:
        """Identify a rejected event by static label while failing closed and discarding its payload or method.

        A documented event is classified by its own label, and a malformed non-text method is treated as unknown rather
        than raising a Python type error; neither the payload nor the method value is retained in the evidence.
        """
        plan_path, _ = review_evidence_files(tmp_path)
        launches = _launches_for_plan(plan_path)
        first_event = _first_index(launches[1], "item/completed")
        launches[1][first_event] = event
        _fake_public_processes(monkeypatch, launches)

        with pytest.raises(_adapter().ReviewRouteError, match="app-server-event-rejected"):
            _adapter().run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)

        evidence_text = (tmp_path / "review-output" / "evidence.json").read_text(encoding="utf-8")
        diagnostic = json.loads(evidence_text)["failure_diagnostic"]
        assert diagnostic["method_category"] == category
        assert secret not in evidence_text

    def test_accepts_turn_bound_moderation_metadata(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Ignore documented moderation metadata bound to the active turn without retaining its payload.

        A `turn/moderationMetadata` notification naming the first reviewer's thread and turn arrives before its final
        answer; the run completes and the private metadata text never reaches the evidence file. This is the baseline
        for the malformed and cross-turn rejection cases.
        """
        plan_path, _ = review_evidence_files(tmp_path)
        _fake_public_processes(monkeypatch, _launches_with_moderation_metadata(plan_path))

        evidence_path = _adapter().run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)

        assert _PRIVATE_METADATA not in evidence_path.read_text(encoding="utf-8")

    @pytest.mark.parametrize(
        ("overrides", "error"),
        [
            pytest.param({"metadata": None}, "app-server-moderation-notification-invalid", id="missing-metadata"),
            pytest.param({"turnId": "other-turn"}, "app-server-thread-or-turn-mismatch", id="other-turn"),
            pytest.param(
                {"command": _PRIVATE_METADATA}, "app-server-moderation-notification-invalid", id="extra-field"
            ),
        ],
    )
    def test_rejects_malformed_or_cross_turn_moderation_metadata(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overrides: dict[str, object], error: str
    ) -> None:
        """Fail closed on moderation metadata that is malformed or names another turn, and retain no payload.

        The documented notification must carry exactly the thread, turn and metadata fields for the active turn; a
        missing field, an extra field, or a foreign turn id stops the run without copying the private text into
        evidence.
        """
        plan_path, _ = review_evidence_files(tmp_path)
        _fake_public_processes(monkeypatch, _launches_with_moderation_metadata(plan_path, **overrides))

        with pytest.raises(_adapter().ReviewRouteError, match=error):
            _adapter().run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)

        assert _PRIVATE_METADATA not in (tmp_path / "review-output/evidence.json").read_text(encoding="utf-8")

    def test_accepts_schema_planning_events_bound_to_active_turn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Accept the documented planning notifications without retaining their text.

        A plan update, plan item start, plan delta and plan item completion for the first turn pass, and none of the
        plan text is stored.
        """
        plan_path, _ = review_evidence_files(tmp_path)
        launches = _launches_for_plan(plan_path)
        first_final = _first_index(launches[1], "item/completed")
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

    @pytest.mark.parametrize(
        ("thread_id", "status", "error"),
        [
            pytest.param("other-thread", "pending", "app-server-thread-or-turn-mismatch", id="other-thread"),
            pytest.param("thread-0", "unreviewed", "app-server-plan-notification-invalid", id="status-outside-schema"),
        ],
    )
    def test_rejects_invalid_schema_planning_event(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, thread_id: str, status: str, error: str
    ) -> None:
        """Keep planning events bound to the active reviewer turns and inside the generated schema.

        A plan update naming another thread is rejected as a thread mismatch, and a plan update whose step status is
        outside the generated schema is rejected as an invalid notification.
        """
        plan_path, _ = review_evidence_files(tmp_path)
        launches = _launches_for_plan(plan_path)
        first_final = _first_index(launches[1], "item/completed")
        launches[1].insert(
            first_final,
            {
                "jsonrpc": "2.0",
                "method": "turn/plan/updated",
                "params": {
                    "threadId": thread_id,
                    "turnId": "turn-0",
                    "plan": [{"step": "Inspect the supplied source.", "status": status}],
                },
            },
        )
        _fake_public_processes(monkeypatch, launches)

        with pytest.raises(_adapter().ReviewRouteError, match=error):
            _adapter().run_review(plan_path, tmp_path / "review-output", Path("codex"), 10)

    def test_rejects_id_bearing_server_request_before_turn_completion(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reject an unsolicited request even when its method is an allowed notification.

        A server-sent message with an id expects a reply; even with an allowed method it is rejected as a server
        request.
        """
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


class TestTerminate:
    """Process cleanup of the reviewer host on POSIX and Windows."""

    def test_kills_a_posix_group_after_parent_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Escalate from TERM to KILL using group existence, not the already-exited parent status.

        The parent has already exited, yet the process group survives TERM; the helper must send KILL based on group
        existence.
        """
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

    def test_requires_successful_windows_tree_cleanup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Reject a nonzero taskkill result rather than treating an exited parent as tree cleanup proof.

        On Windows the taskkill result is the only proof the whole process tree is gone, so a nonzero exit raises.
        """
        adapter = _adapter()
        monkeypatch.setattr(adapter.sys, "platform", "win32")
        monkeypatch.setattr(adapter.subprocess, "run", lambda *args, **kwargs: type("Result", (), {"returncode": 1})())

        with pytest.raises(adapter.ReviewRouteError, match="app-server-windows-tree-cleanup-unproven"):
            adapter._terminate(_FakeProcess([]))

    @_skip_posix_group
    def test_reaps_a_real_local_child_before_group_liveness_check(self, local_child: subprocess.Popen[bytes]) -> None:
        """Cleanly reap a harmless local child so its zombie state cannot keep the group alive.

        This stays a real process on purpose: the contract is that `_terminate` waits for the child so the process-group
        liveness check sees no zombie, which a fake process cannot show.
        """
        _adapter()._terminate(local_child)

        assert local_child.poll() is not None


class TestValidateEvidence:
    """Offline validation of retained plan, output and evidence files."""

    def test_binds_canonical_roles_and_outputs(self, tmp_path: Path) -> None:
        """Return only conservative parent-consumable execution facts for valid evidence.

        A matching two-role plan and evidence fixture validates and yields only the conservative facts a parent may rely
        on.
        """
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

    def test_historical_schema_two_output_requires_assessment_by_default(
        self,
        historical_schema_two_evidence: tuple[Path, Path, Path],
    ) -> None:
        """Current validation rejects a retained response that lacks the later `assessment` field.

        The strict default keeps new runs from silently dropping the reviewer's rating and rationale.
        """
        plan_path, evidence_path, historical_roles = historical_schema_two_evidence

        with pytest.raises(_adapter().ReviewRouteError, match="app-server-review-output-schema-mismatch"):
            _adapter().validate_evidence(plan_path, evidence_path, historical_roles)

    def test_historical_schema_two_output_revalidates_without_later_assessment(
        self,
        historical_schema_two_evidence: tuple[Path, Path, Path],
    ) -> None:
        """Retain a prior source-bound response whose original schema had three fields.

        Opting out of the assessment requirement lets historical evidence from before that field existed validate.
        """
        plan_path, evidence_path, historical_roles = historical_schema_two_evidence

        summary = _adapter().validate_evidence(plan_path, evidence_path, historical_roles, require_assessment=False)

        assert summary["consumer_id"] == "code-review"

    def test_retained_output_rejects_malformed_optional_assessment(self, tmp_path: Path) -> None:
        """An assessment present in retained evidence cannot bypass rating validation.

        A boolean rating inside a stored output is caught with its own error even though the assessment is optional in
        historical evidence.
        """
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

    @pytest.mark.parametrize("role", ["doc-scribe", "web-explorer"])
    def test_local_reviewer_accepts_terra_role_evidence(self, tmp_path: Path, role: str) -> None:
        """Admit Terra's canonical cards without dropping source or runtime bindings.

        doc-scribe and web-explorer run on the terra model; their evidence validates, but changing the plan's model to
        sol breaks the capacity receipt binding.
        """
        plan_path, evidence_path = review_evidence_files(tmp_path, roles=(role,))
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        assert plan["nodes"][0]["model"] == "gpt-6.1-terra"
        assert _adapter().validate_evidence(plan_path, evidence_path, CANONICAL_ROLES)["consumer_id"] == "code-review"

        plan["nodes"][0]["model"] = "gpt-6.1-sol"
        _write_json(plan_path, plan)
        with pytest.raises(_adapter().ReviewRouteError, match="plan-capacity-receipt-binding-invalid"):
            _adapter().validate_evidence(plan_path, evidence_path, CANONICAL_ROLES)

    def test_local_reviewer_rejects_explicit_selection_advisors(self, tmp_path: Path) -> None:
        """Keep architecture and security advisors outside automatic review dispatch.

        security-auditor is chosen only by explicit request, so automatic evidence validation reports its model as
        unsupported.
        """
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
            pytest.param(
                "output_path", "CHALLENGER.MD", "evidence-output-path-duplicate", id="cross-platform-output-alias"
            ),
        ],
    )
    def test_rejects_tampered_node_binding(self, tmp_path: Path, field: str, value: str, match: str) -> None:
        """Reject output-root escape, identity drift, incomplete reviewer results, and colliding output aliases.

        An output path that collides with another node's output after Windows separator and case normalization is a
        duplicate even though its raw spelling differs.
        """
        plan_path, evidence_path = review_evidence_files(tmp_path)
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        evidence["nodes"][1][field] = value
        _write_json(evidence_path, evidence)

        with pytest.raises(_adapter().ReviewRouteError, match=match):
            _adapter().validate_evidence(plan_path, evidence_path, CANONICAL_ROLES)

    @pytest.mark.parametrize(
        ("payload", "match"),
        [
            pytest.param(b" \n\t", "evidence-output-empty-or-secret", id="whitespace-only"),
            pytest.param(b"\xff", "evidence-output-invalid-utf8", id="invalid-utf8"),
        ],
    )
    def test_rejects_nonfinal_output_bytes(self, tmp_path: Path, payload: bytes, match: str) -> None:
        """Reject whitespace-only and non-UTF-8 final response files before promotion.

        A response file holding only whitespace or non-UTF-8 bytes is not a final answer, even when its recorded digest
        matches.
        """
        plan_path, evidence_path = review_evidence_files(tmp_path)
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        output = evidence_path.parent / evidence["nodes"][0]["output_path"]
        output.write_bytes(payload)
        evidence["nodes"][0]["output_sha256"] = _sha256(payload)
        _write_json(evidence_path, evidence)

        with pytest.raises(_adapter().ReviewRouteError, match=match):
            _adapter().validate_evidence(plan_path, evidence_path, CANONICAL_ROLES)

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param("Authorization: " + "Bearer " + "fixture", id="bearer"),
            pytest.param("github" + "_pat_" + "a_b" * 9, id="github-fine-grained"),
            pytest.param("gh" + "r_" + "a_b" * 9, id="github-refresh"),
            pytest.param("sk" + "-" + "a_b-" * 6, id="api-token-punctuation"),
        ],
    )
    def test_rejects_native_sensitive_shapes_in_valid_output(self, tmp_path: Path, payload: str) -> None:
        """Reject credential-shaped material even inside otherwise valid reviewer output.

        The rationale field of an otherwise valid answer carries a token shape; validation refuses the output as a
        secret.
        """
        plan_path, evidence_path = review_evidence_files(tmp_path)
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        output = evidence_path.parent / evidence["nodes"][0]["output_path"]
        answer = json.loads(_review_answer(plan_path))
        answer["assessment"]["rationale"] = payload
        output.write_text(json.dumps(answer), encoding="utf-8", newline="\n")
        evidence["nodes"][0]["output_sha256"] = _sha256(output.read_bytes())
        _write_json(evidence_path, evidence)

        with pytest.raises(_adapter().ReviewRouteError, match="evidence-output-empty-or-secret"):
            _adapter().validate_evidence(plan_path, evidence_path, CANONICAL_ROLES)

    def test_rejects_nonliteral_sandbox_observation(self, tmp_path: Path) -> None:
        """Require the host-observed read-only object rather than a weaker boolean shorthand.

        A shorthand readOnly boolean does not prove the host's read-only object; validation requires the literal type
        and networkAccess shape.
        """
        plan_path, evidence_path = review_evidence_files(tmp_path)
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        evidence["nodes"][0]["observed_controls"]["sandbox"] = {"readOnly": True, "networkAccess": False}
        _write_json(evidence_path, evidence)

        with pytest.raises(_adapter().ReviewRouteError, match="evidence-observed-controls-mismatch"):
            _adapter().validate_evidence(plan_path, evidence_path, CANONICAL_ROLES)

    def test_reports_parallel_only_for_overlapping_intervals(self, tmp_path: Path) -> None:
        """Derive parallelism from reviewer work intervals, never dispatch ordering.

        Reviewer two starting at 4 ms while reviewer one runs from 0 to 5 ms overlaps, so the mode is parallel and not
        independent-spawned.
        """
        plan_path, evidence_path = review_evidence_files(tmp_path)
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        evidence["nodes"][1]["started_at_ms"] = 4
        _write_json(evidence_path, evidence)

        assert _adapter().validate_evidence(plan_path, evidence_path, CANONICAL_ROLES)["actual_mode"] == "parallel"

    def test_does_not_promote_zero_duration_or_negative_intervals(self, tmp_path: Path) -> None:
        """Keep zero-duration evidence independent and reject impossible monotonic timestamps.

        A zero-length first interval overlaps nothing and stays independent-spawned; a negative start timestamp is
        refused as an impossible interval.
        """
        plan_path, evidence_path = review_evidence_files(tmp_path)
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        evidence["nodes"][0].update(started_at_ms=5, finished_at_ms=5)
        evidence["nodes"][1].update(started_at_ms=4, finished_at_ms=8)
        _write_json(evidence_path, evidence)

        assert (
            _adapter().validate_evidence(plan_path, evidence_path, CANONICAL_ROLES)["actual_mode"]
            == "independent-spawned"
        )
        evidence["nodes"][0]["started_at_ms"] = -1
        _write_json(evidence_path, evidence)
        with pytest.raises(_adapter().ReviewRouteError, match="evidence-substantive-interval-invalid"):
            _adapter().validate_evidence(plan_path, evidence_path, CANONICAL_ROLES)
