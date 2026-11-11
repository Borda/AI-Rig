"""Exercise native review scheduling, active capacity, and bound refusal evidence.

The review wave runs a roster of specialist children inside a bounded pool. These tests replay real parent and child
session histories against ``assemble-wave`` and the artifact validator to prove that admission order, slot refill, retry
accounting and capacity refusals are accepted only when the recorded history supports them.
"""

from __future__ import annotations

import copy
import functools
import json
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import pytest
from _artifact_files import read_jsonl
from _modules import load_module_registered
from test_review_prepare import (
    _CONTEXT_READER,
    HELPER,
    PLUGIN_ROOT,
    SKILL,
    _assemble,
    _assembly_evidence,
    _five_role_review_inputs,
    _prepare,
    _record_native_schedule,
    _restore_tree,
    _review_inputs,
    _tree_state,
    _write_jsonl,
)

_EPOCH = datetime(2026, 1, 1, 10, tzinfo=timezone.utc).timestamp()
_PARENT_SEQUENCE_SCENARIOS = [
    "proper-refill",
    "blocked-wait-coalesces-joins",
    "wait-for-all-before-refill",
    "spawn-all-five",
    "reversed-largest-order",
    "wait-again-with-free-slot",
    "default-wait-again-with-free-slot",
    "30s-wait-again-with-free-slot",
    "completed-wait-again-with-free-slot",
    "join-before-terminal",
]
_PARENT_SEQUENCE_ACCEPTED = {"proper-refill", "blocked-wait-coalesces-joins", "wait-for-all-before-refill"}
_PARENT_SEQUENCE_REJECTIONS = {
    "spawn-all-five": "review-inspection-active-capacity-exceeded",
    "reversed-largest-order": "review-inspection-dispatch-order-mismatch",
    "wait-again-with-free-slot": "review-inspection-refill-opportunity-missed",
    "default-wait-again-with-free-slot": "review-inspection-refill-opportunity-missed",
    "30s-wait-again-with-free-slot": "review-inspection-refill-opportunity-missed",
    "completed-wait-again-with-free-slot": "review-inspection-refill-opportunity-missed",
    "join-before-terminal": "review-inspection-active-capacity-exceeded",
}
_OPAQUE_REFUSAL_DAMAGES = [
    "none",
    "empty-message",
    "javascript-block",
    "changed-model",
    "changed-effort",
    "changed-fork",
    "changed-agent-type",
    "extra-argument",
    "missing-argument",
    "different-refusal",
    "child-created",
    "historical-schema",
    "historical-reader",
]
_SHARED_RELEASE_DAMAGES = [
    "none",
    "two-releases",
    "unrelated",
    "metadata-conflict",
    "inactive-at-refusal",
    "old-terminal",
    "post-retry-join",
    "join-payload",
    "notification",
    "missing-activity",
    "future-activity",
    "timed-out",
    "reused-release",
]
_THREAD_LIMIT_OUTPUT = "collab spawn failed: agent thread limit reached"


def _five_role_wave(tmp_path: Path, **evidence) -> tuple[Path, Path, dict[str, Path]]:
    """Prepare the five-role roster and record its native children for the requested evidence options."""
    run = _five_role_review_inputs(tmp_path)
    prepared = _prepare(run)
    assert prepared.returncode == 0, prepared.stderr
    return _assembly_evidence(tmp_path, prepared_run=run, **evidence)


def _manifest_only_check(run: Path, home: Path) -> subprocess.CompletedProcess[str]:
    """Run the ordinary preflight that re-validates an assembled manifest against the native history."""
    argv = [sys.executable, str(SKILL / "validate_artifacts.py"), "--out", str(run), "--manifest-only"]
    return subprocess.run(
        [*argv, "--codex-home", str(home), "--parent-thread-id", "parent"],
        capture_output=True,
        text=True,
        check=False,
    )


def _role_of(task_name: str) -> str:
    """Return the role id encoded in a spawned task name."""
    return task_name.removeprefix("review_").rsplit("_", 2)[0].replace("_", "-")


def _dispatched_roles(dispatch: dict) -> list[str]:
    """Return the roles of a dispatch in the order the calls are queued."""
    return [_role_of(call["arguments"]["task_name"]) for call in dispatch["calls"]]


def _terminal_message(child: Path) -> str:
    """Return the final agent message of a child session."""
    return read_jsonl(child)[-1]["payload"]["last_agent_message"]


def _replay_scenarios(
    subtests: pytest.Subtests, root: Path, state: dict, scenarios: list[dict], check: Callable[..., None]
) -> None:
    """Run each scenario as an independent subtest from a byte-exact restore of one expensive build."""
    for scenario in scenarios:
        with subtests.test(**scenario):
            _restore_tree(root, state)
            check(**scenario)


def _stamp(instant: float) -> str:
    """Format an instant the way the host stamps session events."""
    return datetime.fromtimestamp(instant, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _pad_evidence(measured: Path, padded: Path, plan: dict, target: int) -> None:
    """Grow every role's evidence in ``padded`` so its context reaches ``target`` bytes, sized from ``measured``."""
    for entry in plan["contexts"]:
        evidence = padded / f"{entry['role_id']}-evidence.md"
        evidence.write_bytes(
            evidence.read_bytes() + b"x" * (target - (measured / entry["context_path"]).stat().st_size)
        )


def _refusal_rows(parent: list[dict]) -> tuple[dict, dict]:
    """Return the capacity-refusal output row and the spawn call it answers."""
    output = next(row for row in parent if row.get("payload", {}).get("output") == _THREAD_LIMIT_OUTPUT)
    call = next(
        row
        for row in parent
        if row.get("payload", {}).get("call_id") == output["payload"]["call_id"]
        and row["payload"]["type"] == "function_call"
    )
    return output, call


def _retried_sw_engineer_wave(
    tmp_path: Path, *, sequential: bool = False, unproven: bool = False, missing_timing: bool = False
):
    """Assemble the five-role wave, then record a failed first attempt and a selected second attempt for one role.

    ``sequential`` refills joined slots so only the failed attempt overlaps, ``unproven`` strips the failed attempt's
    final message, and ``missing_timing`` drops its completion event.
    """
    run, home, children = _five_role_wave(tmp_path, active_limit=4, final_header="missing")
    assembled = _assemble(run, home)
    assert assembled.returncode == 0, assembled.stderr
    manifest = json.loads((run / "specialist-manifest.json").read_text(encoding="utf-8"))
    item = next(entry for entry in manifest["passes"] if entry["role"] == "sw-engineer")
    failed = {**item["attempts"][0], "status": "failed", "error_type": "transport_error"}
    selected = {
        **item["attempts"][0],
        "attempt": 2,
        "agent_thread_id": "child-retried",
        "agent_path": item["attempts"][0]["agent_path"].removesuffix("_a1") + "_a2",
        "turn_id": "turn-retried",
        "spawn_call_id": "spawn-retried",
    }
    item.update(attempts=[failed, selected], selected_attempt=2)
    new_rows = json.loads(
        json.dumps(read_jsonl(children["sw-engineer"]))
        .replace(failed["agent_thread_id"], selected["agent_thread_id"])
        .replace(failed["agent_path"], selected["agent_path"])
        .replace(failed["turn_id"], selected["turn_id"])
    )
    page = 0
    for row in new_rows:
        payload = row.get("payload", {})
        if (
            payload.get("type") == "agent_message"
            and payload.get("content", [{}])[0].get("type") == "encrypted_content"
        ):
            content = payload["content"][0]
            content["encrypted_content"] = content["encrypted_content"].replace("--attempt 1", "--attempt 2")
        elif payload.get("type") == "custom_tool_call":
            payload["input"] = payload["input"].replace("--attempt 1", "--attempt 2")
        elif payload.get("type") == "custom_tool_call_output":
            page += 1
            payload["output"][1]["text"] = _CONTEXT_READER(run / "inspection-plan.json", "sw-engineer", 2, page)
    selected_path = home / "sessions/rollout-child-retried.jsonl"
    _write_jsonl(selected_path, new_rows)
    parent_path = home / "sessions/rollout-parent.jsonl"
    parent = read_jsonl(parent_path)
    original_call = next(
        row
        for row in parent
        if row.get("payload", {}).get("call_id") == failed["spawn_call_id"]
        and row["payload"].get("type") == "function_call"
    )
    call = copy.deepcopy(original_call)
    call["payload"]["call_id"] = selected["spawn_call_id"]
    arguments = json.loads(call["payload"]["arguments"])
    arguments["task_name"] = arguments["task_name"].removesuffix("_a1") + "_a2"
    arguments["message"] = arguments["message"].replace("--attempt 1", "--attempt 2")
    call["payload"]["arguments"] = json.dumps(arguments)
    original_receipt = next(
        row
        for row in parent
        if row.get("payload", {}).get("call_id") == failed["spawn_call_id"]
        and row["payload"].get("type") == "function_call_output"
    )
    receipt = json.loads(json.dumps(original_receipt).replace(failed["agent_path"], selected["agent_path"]))
    receipt["payload"]["call_id"] = selected["spawn_call_id"]
    original_join = next(row for row in parent if row.get("payload", {}).get("author") == failed["agent_path"])
    joined = json.loads(json.dumps(original_join).replace(failed["agent_path"], selected["agent_path"]))
    parent.extend([call, receipt, joined])
    _write_jsonl(parent_path, parent)
    selected_starts = {"qa-specialist": 1, "doc-scribe": 3, "data-steward": 5, "challenger": 7, "sw-engineer": 9}
    for index, entry in enumerate(manifest["passes"]):
        attempt = entry["attempts"][entry["selected_attempt"] - 1]
        path = selected_path if entry["role"] == "sw-engineer" else children[entry["role"]]
        rows = read_jsonl(path)
        completed = next(row["payload"] for row in rows if row.get("payload", {}).get("type") == "task_complete")
        start = _EPOCH + (selected_starts[entry["role"]] if sequential else (100 + index if index < 4 else 130))
        completed.update(started_at=start, completed_at=start + (1 if sequential else 10))
        assert completed["turn_id"] == attempt["turn_id"]
        _write_jsonl(path, rows)
    failed_rows = read_jsonl(children["sw-engineer"])
    failed_completion = next(
        row["payload"] for row in failed_rows if row.get("payload", {}).get("type") == "task_complete"
    )
    failed_completion.update(
        started_at=_EPOCH + (0.2 if sequential else 105), completed_at=_EPOCH + (1.5 if sequential else 110)
    )
    failed_completion["last_agent_message"] = "" if unproven else failed_completion["last_agent_message"]
    failed_rows = (
        [row for row in failed_rows if row.get("payload", {}).get("type") != "task_complete"]
        if missing_timing
        else failed_rows
    )
    _write_jsonl(children["sw-engineer"], failed_rows)
    if not missing_timing:
        # Keep every launch and join coherent with its selected or failed task.
        # The negative retry overlaps four live children; the sequential selected
        # case instead refills joined slots while failed work alone overlaps.
        launch_offsets = {
            "qa-specialist": 0.28,
            "doc-scribe": 0.38,
            "data-steward": 0.48,
            "challenger": 1.8 if sequential else 5.28,
            "sw-engineer": 4.2 if sequential else 5,
        }
        path_times = {}
        for role, path in {**children, "sw-engineer": selected_path}.items():
            rows = read_jsonl(path)
            agent_path = rows[0]["payload"]["agent_path"]
            launch = _EPOCH + launch_offsets[role]
            terminal = next(row["payload"] for row in rows if row.get("payload", {}).get("type") == "task_complete")
            rows[0]["payload"]["timestamp"] = _stamp(launch + 0.005)
            _write_jsonl(path, rows)
            path_times[agent_path] = (launch, terminal["completed_at"] + 0.05)
        path_times[failed["agent_path"]] = (_EPOCH + 0.18, failed_completion["completed_at"] + 0.05)
        parent = read_jsonl(parent_path)
        call_times = {}
        for row in parent:
            payload = row.get("payload", {})
            if payload.get("type") == "function_call" and payload.get("name") == "spawn_agent":
                task_name = json.loads(payload["arguments"])["task_name"]
                path = next(path for path in path_times if path.rsplit("/", 1)[-1] == task_name)
                call_times[payload["call_id"]] = path_times[path][0]
                value = call_times[payload["call_id"]]
            elif payload.get("type") == "function_call_output":
                value = call_times[payload["call_id"]] + 0.01
            elif payload.get("type") == "agent_message":
                value = path_times[payload["author"]][1]
            else:
                continue
            row["timestamp"] = _stamp(value)
        parent.sort(key=lambda row: row.get("timestamp", ""))
        _write_jsonl(parent_path, parent)
    validator = load_module_registered("retry_capacity_validator", SKILL / "validate_artifacts.py")
    return run, home, children, manifest, failed, validator


def _check_parent_sequence(run: Path, home: Path, children: dict[str, Path], scenario: str) -> None:
    """Replay one parent scheduling history and require assembly and preflight to agree on admission."""
    _record_native_schedule(run, home, children, scenario=scenario)
    assembled = _assemble(run, home)
    checked = _manifest_only_check(run, home)
    if scenario in _PARENT_SEQUENCE_ACCEPTED:
        assert assembled.returncode == checked.returncode == 0, assembled.stderr + checked.stderr
        manifest = json.loads((run / "specialist-manifest.json").read_text(encoding="utf-8"))
        assert {item["role"] for item in manifest["passes"]} == set(children)
    else:
        assert assembled.returncode != 0, f"invalid {scenario} scheduling admitted"
        assert checked.returncode != 0, f"ordinary preflight admitted {scenario}"
        expected = _PARENT_SEQUENCE_REJECTIONS[scenario]
        assert expected in assembled.stderr
        assert expected in checked.stderr


def _fast_native_schedule(tmp_path: Path, *, batched: bool) -> tuple[Path, Path, dict[str, Path]]:
    """Prepare an independence-required wave and record its children on one active slot."""
    root = _review_inputs(tmp_path)
    routing_path = root / "review-routing.json"
    routing = json.loads(routing_path.read_bytes())
    routing.update(
        independent_review_required=True,
        independence_requirement_evidence="The request requires independent source inspection.",
    )
    routing_path.write_text(json.dumps(routing), encoding="utf-8", newline="\n")
    prepared = _prepare(root, batches=batched)
    assert prepared.returncode == 0, prepared.stderr
    run = root / "batches/source-001" if batched else root
    _, home, children = _assembly_evidence(tmp_path, prepared_run=run)
    _record_native_schedule(run, home, children, pool_size=1)
    return run, home, children


def _check_fast_native_interruption(
    run: Path, home: Path, children: dict[str, Path], *, batched: bool, interruption: str
) -> None:
    """Insert one parent interruption before the pending launch and check wave admission."""
    parent_path = home / "sessions/rollout-parent.jsonl"
    rows = read_jsonl(parent_path)
    launches = [
        row
        for row in rows
        if row.get("payload", {}).get("type") == "function_call" and row["payload"].get("name") == "spawn_agent"
    ]
    assert set(children) == {"challenger", "qa-specialist", "sw-engineer"}
    assert len(launches) == len(children)
    if interruption != "none":
        next_launch = datetime.fromisoformat(launches[1]["timestamp"].replace("Z", "+00:00")).timestamp()
        call_type = "function_call" if interruption == "wait" else "custom_tool_call"
        call = {
            "type": "response_item",
            "timestamp": datetime.fromtimestamp(next_launch - 0.1, timezone.utc).isoformat(),
            "payload": {
                "type": call_type,
                "name": "wait_agent" if interruption == "wait" else "exec",
                "call_id": "interrupted-dispatch",
                "arguments": json.dumps({"timeout_ms": 10000}),
                "input": "text('unrelated parent work')",
            },
        }
        receipt = {
            "type": "response_item",
            "timestamp": datetime.fromtimestamp(next_launch - 0.05, timezone.utc).isoformat(),
            "payload": {
                "type": "function_call_output" if interruption == "wait" else "custom_tool_call_output",
                "call_id": "interrupted-dispatch",
                "output": json.dumps({"timed_out": False}),
            },
        }
        index = rows.index(launches[1])
        rows[index:index] = [call, receipt]
        _write_jsonl(parent_path, rows)
    original = {path: path.read_bytes() for path in [parent_path, *children.values()]}
    assembled = (
        subprocess.run(
            [sys.executable, str(HELPER), "assemble-wave", "--out", str(run), "--codex-home", str(home)],
            capture_output=True,
            text=True,
            check=False,
        )
        if batched
        else _assemble(run, home)
    )
    if interruption != "none":
        assert assembled.returncode != 0
        assert (
            "review-inspection-refill-opportunity-missed"
            if interruption == "wait"
            else "review-inspection-dispatch-interrupted"
        ) in assembled.stderr
        assert not (run / "specialist-manifest.json").exists()
    else:
        assert assembled.returncode == 0, assembled.stderr
        summary = json.loads((run / "inspection-summary.json").read_bytes())
        assert summary["actual_mode"] == "independent-spawned"
        assert summary["capacity_limited"] is False
        assert summary["independence_required"] is True
        assert summary["independence_satisfied"] is True
    assert all(path.read_bytes() == content for path, content in original.items())


def _check_opaque_capacity_refusal(
    run: Path, home: Path, children: dict[str, Path], canonical_capacity_limited: bool, damage: str
) -> None:
    """Make the recorded capacity refusal opaque, apply one damage, and check schedule admission."""
    prepare = load_module_registered("opaque_refusal_prepare", HELPER)
    parent_path = home / "sessions/rollout-parent.jsonl"
    parent = read_jsonl(parent_path)
    refusal_output, refusal_call = _refusal_rows(parent)
    refusal_id = refusal_output["payload"]["call_id"]
    args = json.loads(refusal_call["payload"]["arguments"])
    name = args["task_name"]
    role = _role_of(name)
    args["message"] = "opaque-refused-transport"
    successful = next(
        row
        for row in parent
        if row.get("payload", {}).get("type") == "function_call"
        and row["payload"].get("name") == "spawn_agent"
        and row["payload"]["call_id"] != refusal_id
        and json.loads(row["payload"]["arguments"])["task_name"] == name
    )
    successful_args = json.loads(successful["payload"]["arguments"])
    successful_args["message"] = "different-opaque-successful-transport"
    successful["payload"]["arguments"] = json.dumps(successful_args)
    child = children[role]
    rows = read_jsonl(child)
    next(row["payload"] for row in rows if row.get("payload", {}).get("type") == "agent_message")["content"][0][
        "encrypted_content"
    ] = successful_args["message"]
    if damage == "empty-message":
        args["message"] = ""
    elif damage == "javascript-block":
        args["message"] = "```javascript\nunrelated execution\n```"
    elif damage == "changed-model":
        args["model"] = "unselected-model"
    elif damage == "changed-effort":
        args["reasoning_effort"] = "unselected-effort"
    elif damage == "changed-fork":
        args["fork_turns"] = "all"
    elif damage == "changed-agent-type":
        args["agent_type"] = "custom"
    elif damage == "extra-argument":
        args["unexpected"] = True
    elif damage == "missing-argument":
        del args["fork_turns"]
    elif damage == "different-refusal":
        refusal_output["payload"]["output"] = "collab spawn failed: other error"
    elif damage == "child-created":
        extra = copy.deepcopy(rows[0])
        extra["payload"]["id"] = "refused-child"
        extra["payload"]["timestamp"] = refusal_call["timestamp"]
        _write_jsonl(home / "sessions/rollout-refused-child.jsonl", [extra])
    refusal_call["payload"]["arguments"] = json.dumps(args)
    _write_jsonl(child, rows)
    _write_jsonl(parent_path, parent)
    manifest = json.loads((run / "specialist-manifest.json").read_bytes())
    if damage == "historical-schema":
        manifest["schema_version"] = 7
    if damage == "historical-reader":
        manifest["context_reader_sha256"] = prepare.validator.LEGACY_SINGLE_CALL_READER_SHA256
    contexts = {
        entry["role_id"]: run / entry["context_path"]
        for entry in json.loads((run / "inspection-plan.json").read_bytes())["contexts"]
    }
    protected = {
        path: path.read_bytes()
        for path in [
            parent_path,
            *children.values(),
            run / "inspection-plan.json",
            run / "dispatch.json",
            run / "specialist-manifest.json",
        ]
    }
    if damage == "none":
        assert (
            prepare.validator._validate_native_schedule(
                run, manifest, manifest["passes"], parent, contexts, home, "parent", PLUGIN_ROOT / "roles"
            )
            is canonical_capacity_limited
        )
        result = _assemble(run, home)
        assert result.returncode == 0, result.stderr
    else:
        expected = (
            "review-inspection-launch-arguments-invalid"
            if damage == "child-created"
            else "review-inspection-capacity-refusal-invalid"
        )
        with pytest.raises(SystemExit, match=expected):
            prepare.validator._validate_native_schedule(
                run, manifest, manifest["passes"], parent, contexts, home, "parent", PLUGIN_ROOT / "roles"
            )
    assert all(path.read_bytes() == content for path, content in protected.items())


def _queued_refusal_retry(tmp_path: Path, retry_offset: float, state_change: bool) -> tuple[Path, Path, dict, str]:
    """Record a second same-task capacity refusal ``retry_offset`` seconds after the first, with or without a release.

    With ``state_change`` the first join precedes the retry, which frees one slot for the successful launch.
    """
    run, home, children = _five_role_wave(tmp_path, active_limit=4)
    initial = _assemble(run, home) if not state_change else None
    assert initial is None or initial.returncode == 0, initial and initial.stderr
    _record_native_schedule(run, home, children, scenario="capacity-rejected")
    parent_path = home / "sessions/rollout-parent.jsonl"
    parent = read_jsonl(parent_path)
    refusal_output, refusal_call = _refusal_rows(parent)
    refusal_id = refusal_output["payload"]["call_id"]
    second_call = copy.deepcopy(refusal_call)
    second_output = copy.deepcopy(refusal_output)
    second_at = _EPOCH + 1 + retry_offset
    for row, instant in [(second_call, second_at), (second_output, second_at + 0.01)]:
        row["payload"]["call_id"] = refusal_id + "-retry"
        row["timestamp"] = _stamp(instant)
    parent.extend([second_call, second_output])
    arguments = json.loads(refusal_call["payload"]["arguments"])
    role = _role_of(arguments["task_name"])
    if state_change:
        # First refusal precedes the first join; the second refusal follows it.
        # Only the next joined child frees capacity for the successful same task.
        child_rows = read_jsonl(children[role])
        agent_path = child_rows[0]["payload"]["agent_path"]
        child_rows[0]["payload"]["timestamp"] = "2026-01-01T10:00:10.285Z"
        terminal = next(row["payload"] for row in child_rows if row.get("payload", {}).get("type") == "task_complete")
        terminal.update(started_at=_EPOCH + 10.3, completed_at=_EPOCH + 20.3)
        _write_jsonl(children[role], child_rows)
        successful = next(
            row
            for row in parent
            if row.get("payload", {}).get("type") == "function_call"
            and row["payload"].get("name") == "spawn_agent"
            and row["payload"].get("call_id") not in {refusal_id, refusal_id + "-retry"}
            and json.loads(row["payload"]["arguments"])["task_name"] == arguments["task_name"]
        )
        successful["timestamp"] = "2026-01-01T10:00:10.280Z"
        success_id = successful["payload"]["call_id"]
        for row in parent:
            payload = row.get("payload", {})
            if payload.get("type") == "function_call_output" and payload.get("call_id") == success_id:
                row["timestamp"] = "2026-01-01T10:00:10.290Z"
            elif payload.get("type") == "agent_message" and payload.get("author") == agent_path:
                row["timestamp"] = "2026-01-01T10:00:20.350Z"
    parent.sort(key=lambda row: row.get("timestamp", ""))
    _write_jsonl(parent_path, parent)
    return run, home, children, role


def _check_shared_parent_release(run: Path, home: Path, children: dict[str, Path], damage: str) -> None:
    """Record one related-turn slot release around a capacity refusal and check assembly and preflight."""
    prepare = load_module_registered("shared_capacity_prepare", HELPER)
    parent_path = home / "sessions/rollout-parent.jsonl"
    parent = read_jsonl(parent_path)

    def stamp(offset: float) -> str:
        """Match the host's precise event timestamp format."""
        return _stamp(_EPOCH + offset)

    operator_path, sibling_path = "/root/operator", "/root/slot_owner"
    parent[0]["payload"].update(
        parent_thread_id="ancestor",
        agent_path=operator_path,
        source={"subagent": {"thread_spawn": {"parent_thread_id": "ancestor", "agent_path": operator_path}}},
    )
    launches = [r for r in parent if r.get("payload", {}).get("name") == "spawn_agent"]
    dispatch = json.loads((run / "dispatch.json").read_text(encoding="utf-8"))
    queued = dispatch["calls"][0]["arguments"]["task_name"]
    launch = next(r for r in launches if json.loads(r["payload"]["arguments"])["task_name"] == queued)
    refusal = copy.deepcopy(launch)
    refusal["timestamp"] = stamp(-2)
    refusal["payload"]["call_id"] = "global-refusal"
    notification = "opaque-slot-notification"
    parent.extend(
        [
            refusal,
            {
                "timestamp": stamp(-1.99),
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "global-refusal",
                    "output": _THREAD_LIMIT_OUTPUT,
                },
            },
            {
                "timestamp": stamp(-1.5),
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "wait_agent",
                    "call_id": "global-wait",
                    "arguments": '{"timeout_ms": 10000}',
                },
            },
            {
                "timestamp": stamp(-0.2),
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "global-wait",
                    "output": '{"message": "Wait completed.", "timed_out": false}',
                },
            },
            {
                "timestamp": stamp(-0.199),
                "type": "response_item",
                "payload": {
                    "type": "agent_message",
                    "author": "/root",
                    "recipient": operator_path,
                    "content": [
                        {
                            "type": "input_text",
                            "text": f"Message Type: MESSAGE\nTask name: {operator_path}\nSender: /root\nPayload:\n",
                        },
                        {"type": "encrypted_content", "encrypted_content": notification},
                    ],
                },
            },
        ]
    )
    ancestor = [
        {"type": "session_meta", "payload": {"id": "ancestor"}},
        {
            "timestamp": stamp(-0.4),
            "type": "response_item",
            "payload": {
                "type": "agent_message",
                "author": sibling_path,
                "content": [{"type": "input_text", "text": "Message Type: FINAL_ANSWER\nPayload:\nSlot finished."}],
            },
        },
        {
            "timestamp": stamp(-0.3),
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "send_message",
                "call_id": "notify",
                "arguments": json.dumps({"target": "operator", "message": notification}),
            },
        },
        {
            "timestamp": stamp(-0.29),
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "SubAgentActivity",
                    "id": "notify",
                    "kind": "interacted",
                    "agent_thread_id": "parent",
                    "agent_path": operator_path,
                },
                "started_at_ms": (_EPOCH - 0.295) * 1000,
                "completed_at_ms": (_EPOCH - 0.29) * 1000,
            },
        },
        {
            "timestamp": stamp(-0.28),
            "type": "response_item",
            "payload": {"type": "function_call_output", "call_id": "notify", "output": ""},
        },
    ]
    sibling = [
        {
            "type": "session_meta",
            "payload": {
                "id": "pool-sibling",
                "parent_thread_id": "ancestor",
                "agent_path": sibling_path,
                "source": {"subagent": {"thread_spawn": {"parent_thread_id": "ancestor", "agent_path": sibling_path}}},
            },
        },
        {
            "timestamp": stamp(-3),
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "released-turn", "started_at": _EPOCH - 3},
        },
        {
            "timestamp": stamp(-0.5),
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "released-turn",
                "started_at": _EPOCH - 3,
                "completed_at": _EPOCH - 0.5,
                "last_agent_message": "Slot finished.",
            },
        },
    ]
    if damage == "two-releases":
        second = copy.deepcopy(sibling)
        second[0]["payload"]["id"] = "pool-second"
        second[0]["payload"]["agent_path"] = "/root/second_slot_owner"
        second[0]["payload"]["source"]["subagent"]["thread_spawn"]["agent_path"] = "/root/second_slot_owner"
        ancestor.append(
            {
                "timestamp": stamp(-0.39),
                "type": "response_item",
                "payload": {
                    "type": "agent_message",
                    "author": "/root/second_slot_owner",
                    "content": [{"type": "input_text", "text": "Message Type: FINAL_ANSWER\nPayload:\nSlot finished."}],
                },
            }
        )
        _write_jsonl(home / "sessions/rollout-pool-second.jsonl", second)
    if damage == "unrelated":
        sibling[0]["payload"]["source"]["subagent"]["thread_spawn"]["parent_thread_id"] = "unrelated-root"
    elif damage == "metadata-conflict":
        sibling[0]["payload"]["parent_thread_id"] = "unrelated-root"
    elif damage == "inactive-at-refusal":
        sibling[1]["timestamp"] = stamp(-1.8)
    elif damage == "old-terminal":
        sibling[2]["timestamp"] = stamp(-2.1)
    elif damage == "post-retry-join":
        ancestor[1]["timestamp"] = stamp(0.1)
    elif damage == "join-payload":
        ancestor[1]["payload"]["content"][0]["text"] += " Altered."
    elif damage == "notification":
        parent[-1]["payload"]["content"][1]["encrypted_content"] = "unmatched-notification"
    elif damage == "missing-activity":
        ancestor.pop(3)
    elif damage == "future-activity":
        ancestor[3]["payload"]["completed_at_ms"] = (_EPOCH + 1) * 1000
    elif damage == "timed-out":
        parent[-2]["payload"]["output"] = '{"message": "Wait timed out.", "timed_out": true}'
    elif damage == "reused-release":
        repeated = copy.deepcopy(refusal)
        repeated["timestamp"] = stamp(-0.1)
        repeated["payload"]["call_id"] = "global-refusal-repeat"
        parent.extend(
            [
                repeated,
                {
                    "timestamp": stamp(-0.09),
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "global-refusal-repeat",
                        "output": _THREAD_LIMIT_OUTPUT,
                    },
                },
            ]
        )
    parent.sort(key=lambda row: row.get("timestamp", ""))
    _write_jsonl(parent_path, parent)
    _write_jsonl(home / "sessions/rollout-ancestor.jsonl", ancestor)
    _write_jsonl(home / "sessions/rollout-pool-sibling.jsonl", sibling)
    protected = {
        path: path.read_bytes()
        for path in [
            parent_path,
            home / "sessions/rollout-ancestor.jsonl",
            home / "sessions/rollout-pool-sibling.jsonl",
            *children.values(),
        ]
    }
    if damage in {"none", "two-releases"}:
        summary = prepare.assemble(run, home)
        assert summary["capacity_limited"] is True
        assert summary["actual_mode"] == "parallel"
        manifest = json.loads((run / "specialist-manifest.json").read_bytes())
        assert {p["role"] for p in manifest["passes"]} == set(children)
        if damage == "two-releases":
            execution = prepare.review_execution_validation
            refusal_record = {
                "called_at": execution._parent_timestamp(refusal),
                "returned_at": datetime.fromtimestamp(_EPOCH - 1.99, timezone.utc),
            }
            next_at = execution._parent_timestamp(launch)
            first = execution._related_capacity_release(home, "parent", parent, refusal_record, next_at, set())
            second = execution._related_capacity_release(home, "parent", parent, refusal_record, next_at, {first})
            assert first is not None
            assert second is not None
            assert first != second
            assert (
                execution._related_capacity_release(home, "parent", parent, refusal_record, next_at, {first, second})
                is None
            )
    else:
        with pytest.raises(SystemExit, match="review-inspection-capacity-refusal-no-state-change"):
            prepare.assemble(run, home)
        assert not (run / "specialist-manifest.json").exists()
    assert all(path.read_bytes() == content for path, content in protected.items())


class TestDispatchOrder:
    """The roster is queued by descending frozen context size, with role id as the stable tie-break."""

    def test_orders_complete_contexts_and_stable_ties(self, tmp_path: Path) -> None:
        """Queue the entire roster by descending frozen size, retaining deterministic equal-size order.

        Five roles are prepared with distinct sizes, then re-prepared with padded evidence so every context has the same
        size. Equal sizes must fall back to role order, a repeat preparation must not rewrite the frozen dispatch, and
        an assembly whose recorded launches reverse that order must be refused.
        """
        first = tmp_path / "first-a"
        second = tmp_path / "first-b"
        first.mkdir()
        second.mkdir()
        run = _five_role_review_inputs(first)
        prepared = _prepare(run)
        assert prepared.returncode == 0, prepared.stderr
        plan = json.loads((run / "inspection-plan.json").read_text(encoding="utf-8"))
        first_sizes = {entry["role_id"]: (run / entry["context_path"]).stat().st_size for entry in plan["contexts"]}
        first_dispatch = json.loads((run / "dispatch.json").read_text(encoding="utf-8"))
        assert _dispatched_roles(first_dispatch) == sorted(first_sizes, key=lambda role: (-first_sizes[role], role))
        tied = _five_role_review_inputs(second)
        _pad_evidence(run, tied, plan, max(first_sizes.values()) + 100)
        completed = _prepare(tied)
        assert completed.returncode == 0, completed.stderr
        tied_plan = json.loads((tied / "inspection-plan.json").read_text(encoding="utf-8"))
        entries = tied_plan["contexts"]
        sizes = {entry["role_id"]: (tied / entry["context_path"]).stat().st_size for entry in entries}
        assert len(set(sizes.values())) == 1, sizes
        expected = sorted(sizes, key=lambda role: (-sizes[role], role))
        assert [entry["role_id"] for entry in entries] == expected
        dispatch = json.loads((tied / "dispatch.json").read_text(encoding="utf-8"))
        assert len(dispatch["calls"]) == 5
        assert _dispatched_roles(dispatch) == expected
        frozen_dispatch = (tied / "dispatch.json").read_bytes()
        assert _prepare(tied).returncode == 0
        assert (tied / "dispatch.json").read_bytes() == frozen_dispatch
        tied, home, children = _assembly_evidence(second, prepared_run=tied, active_limit=4)
        assert _assemble(tied, home).returncode == 0
        _record_native_schedule(tied, home, children, scenario="reversed-largest-order")
        reversed_ties = _assemble(tied, home)
        assert reversed_ties.returncode != 0
        assert "review-inspection-dispatch-order-mismatch" in reversed_ties.stderr

    def test_historical_context_limit_remains_four(self, tmp_path: Path) -> None:
        """The explicit native roster extension cannot relax the shared default historical boundary.

        A five-role plan is valid for the native roster, yet the shared historical validator that other protocols still
        use must keep rejecting more than four inspection contexts.
        """
        run = _five_role_review_inputs(tmp_path)
        result = _prepare(run)
        assert result.returncode == 0, result.stderr
        plan = json.loads((run / "inspection-plan.json").read_text(encoding="utf-8"))
        module = load_module_registered("historical_context_boundary", PLUGIN_ROOT / "shared/parallel_execution.py")
        with pytest.raises(ValueError, match="review-inspection-contexts-invalid"):
            module.validate_inspection_contexts(plan, run / "inspection-plan.json")


class TestActiveCapacity:
    """At most four review children may be active at once, and smaller observed pools must still complete the roster."""

    def test_admits_five_roles_in_four_child_pool(self, tmp_path: Path) -> None:
        """Accept five completed roles launched through a four-child pool.

        The fifth role waits for a joined slot, so peak activity stays at four. The assembled manifest must cover every
        role in inspection mode with the output bytes of each child's terminal message.
        """
        run, home, children = _five_role_wave(tmp_path, active_limit=4)
        assembled = _assemble(run, home)
        assert assembled.returncode == 0, assembled.stderr
        manifest = json.loads((run / "specialist-manifest.json").read_text(encoding="utf-8"))
        assert manifest["schema_version"] == 8
        assert {item["role"] for item in manifest["passes"]} == set(children)
        assert len(children) == 5
        assert all(item["mode"] == "inspection" for item in manifest["passes"])
        assert all(
            (run / item["output_path"]).read_bytes()
            == (_terminal_message(children[item["role"]]).strip() + "\n").encode()
            for item in manifest["passes"]
        )

    def test_rejects_five_overlapping_children(self, tmp_path: Path) -> None:
        """Reject assembly when five tasks were recorded as active at the same time.

        A fifth overlapping child exceeds the four-child pool, so no specialist manifest may be written.
        """
        run, home, _ = _five_role_wave(tmp_path, active_limit=5)
        assembled = _assemble(run, home)
        assert assembled.returncode != 0
        assert "review-inspection-active-capacity-exceeded" in assembled.stderr
        assert not (run / "specialist-manifest.json").exists()

    @pytest.mark.parametrize("pool_size", [1, 2, 3])
    def test_smaller_observed_pool_refills_complete_roster(self, tmp_path: Path, pool_size: int) -> None:
        """Accept a coherent smaller active pool without dropping roles or inventing available capacity.

        The recorded history shows capacity refusals at a pool of one to three children. Every role must still be
        assembled, the run must report capacity limiting, and the ordinary preflight must agree.
        """
        run, home, children = _five_role_wave(tmp_path, active_limit=pool_size)
        _record_native_schedule(
            run,
            home,
            children,
            pool_size=pool_size,
            scenario="capacity-rejected-repeat" if pool_size == 1 else "capacity-rejected",
        )
        result = _assemble(run, home)
        assert result.returncode == 0, result.stderr
        manifest = json.loads((run / "specialist-manifest.json").read_text(encoding="utf-8"))
        summary = json.loads((run / "inspection-summary.json").read_text(encoding="utf-8"))
        checked = _manifest_only_check(run, home)
        assert {item["role"] for item in manifest["passes"]} == set(children)
        assert len(manifest["passes"]) == 5
        assert summary["actual_mode"] == ("independent-spawned" if pool_size == 1 else "parallel")
        assert summary["capacity_limited"] is True
        assert checked.returncode == 0, checked.stderr

    def test_unreadable_session_prevents_no_child_capacity_proof(self, tmp_path: Path) -> None:
        """A refusal cannot prove no child was allocated when session metadata is unknown."""
        run, home, children = _five_role_wave(tmp_path, active_limit=2)
        _record_native_schedule(run, home, children, pool_size=2, scenario="capacity-rejected")
        (home / "sessions/unknown.jsonl").write_bytes(b"\xff\n")

        result = _assemble(run, home)

        assert result.returncode != 0
        assert "provenance-session-unreadable" in result.stderr
        assert not (run / "specialist-manifest.json").exists()


class TestRetryTiming:
    """Failed attempts count toward capacity, while parallel review is derived only from the selected completions."""

    @pytest.mark.parametrize(
        ("options", "expected"),
        [
            pytest.param({}, "review-inspection-active-capacity-exceeded", id="failed-peak-five"),
            pytest.param(
                {"missing_timing": True},
                "review-inspection-attempt-timing-missing:sw-engineer:1",
                id="missing-failed-timing",
            ),
        ],
    )
    def test_failed_attempt_timing_cannot_evade_capacity(self, tmp_path: Path, options: dict, expected: str) -> None:
        """Count every launched attempt, and refuse a failed attempt that has no recorded timing.

        One role fails its first attempt while four other children are live, so five tasks overlap even though the
        selected attempts alone peak at four. Without the failed attempt's completion event the overlap is unprovable.
        """
        run, home, _, manifest, _, validator = _retried_sw_engineer_wave(tmp_path, **options)
        with pytest.raises(SystemExit, match=expected):
            validator._validate_review_runtime(run, manifest, manifest["passes"], home, "parent")

    @pytest.mark.parametrize("error_type", ["timeout", "transport_error", "rate_limited"])
    def test_unverified_transient_failure_is_rejected(self, tmp_path: Path, error_type: str) -> None:
        """Refuse a transient failure claim whose failed attempt produced no proving final message.

        The manifest labels the first attempt a timeout, transport error or rate limit, but the child session ends with
        an empty message, which does not prove a launch failure.
        """
        run, home, children, manifest, failed, validator = _retried_sw_engineer_wave(tmp_path, unproven=True)
        manifest["passes"].sort(key=lambda entry: entry["role"] != "sw-engineer")
        failed["error_type"] = error_type
        with pytest.raises(SystemExit, match="manifest-unverified-transient-failure:sw-engineer:1"):
            validator._validate_manifest_entries(
                run,
                manifest,
                manifest["passes"],
                set(children),
                home,
                "parent",
                tmp_path,
                require_role_card_receipts=True,
            )

    def test_completed_first_attempt_is_not_a_transport_failure(self, tmp_path: Path) -> None:
        """A completed first attempt is not a transport failure merely because its manifest says so.

        Selected attempts refill joined slots sequentially, so only the failed attempt overlaps; its recorded completion
        contradicts the claimed failure and the validator must refuse it.
        """
        run, home, children, manifest, _, validator = _retried_sw_engineer_wave(tmp_path, sequential=True)
        with pytest.raises(SystemExit, match="manifest-invalid-failed-attempt:sw-engineer:1"):
            validator._validate_manifest_entries(
                run,
                manifest,
                manifest["passes"],
                set(children),
                home,
                "parent",
                tmp_path,
                require_role_card_receipts=True,
            )


class TestParentSequence:
    """The recorded parent history, not the final work peak, controls allocation order and slot refill."""

    def test_controls_allocation_order_and_refill(self, tmp_path: Path, subtests: pytest.Subtests) -> None:
        """Reject invalid parent scheduling even when every role completes and work peak remains four.

        Every scenario replays a different parent history over the same assembled five-role wave, so that wave is built
        once and each scenario runs as an independent subtest from a byte-exact restore of it.
        """
        run, home, children = _five_role_wave(tmp_path, active_limit=4)
        # Retain a valid manifest first so both assembly and ordinary preflight can replay changed parent history.
        initial = _assemble(run, home)
        assert initial.returncode == 0, initial.stderr
        scenarios = [{"scenario": scenario} for scenario in _PARENT_SEQUENCE_SCENARIOS]
        check = functools.partial(_check_parent_sequence, run, home, children)
        _replay_scenarios(subtests, tmp_path, _tree_state(tmp_path), scenarios, check)


class TestFastNativeAssembly:
    """Children that finish fast are admitted only when the parent never stalled a pending launch."""

    @pytest.mark.integration
    @pytest.mark.parametrize("batched", [False, True])
    def test_requires_uninterrupted_pending_dispatch(
        self, tmp_path: Path, subtests: pytest.Subtests, batched: bool
    ) -> None:
        """Admit authentic fast children while rejecting parent work or waiting with a free pending slot.

        Every interruption alters the same prepared single-slot schedule, so that schedule is built once per preparation
        mode and each interruption runs as an independent subtest from a byte-exact restore of it.
        """
        run, home, children = _fast_native_schedule(tmp_path, batched=batched)
        scenarios = [{"interruption": interruption} for interruption in ["none", "wait", "unrelated-tool"]]
        check = functools.partial(_check_fast_native_interruption, run, home, children, batched=batched)
        _replay_scenarios(subtests, tmp_path, _tree_state(tmp_path), scenarios, check)

    def test_rejects_serial_roster_with_parent_tool_delay(self, tmp_path: Path) -> None:
        """Reject unrelated parent work interrupting free-capacity original reviewer launches.

        With a one-child pool the parent runs an unrelated tool between two original launches; that delay is not a
        capacity wait, so assembly must report the interrupted dispatch.
        """
        run, home, _ = _five_role_wave(tmp_path, active_limit=1)
        parent = next((home / "sessions").rglob("*parent*.jsonl"))
        rows = read_jsonl(parent)
        launches = [row for row in rows if row.get("payload", {}).get("name") == "spawn_agent"]
        rows.insert(
            rows.index(launches[1]),
            {
                "timestamp": launches[1]["timestamp"],
                "type": "response_item",
                "payload": {"type": "custom_tool_call", "name": "exec", "call_id": "delay", "input": "text('delay')"},
            },
        )
        _write_jsonl(parent, rows)
        result = _assemble(run, home)
        assert result.returncode != 0
        assert "review-inspection-dispatch-interrupted" in result.stderr


class TestCapacityRefusal:
    """A thread-limit refusal is evidence only when it is bound to a childless launch and a verified slot release."""

    def test_accepts_only_bound_no_child_opaque_transport(self, tmp_path: Path, subtests: pytest.Subtests) -> None:
        """Opaque refused messages cannot supply source coverage or weaken spawn controls and release proof.

        Every damage rewrites the refusal of the same assembled capacity-limited wave, so that wave is built once and
        each damage runs as an independent subtest from a byte-exact restore of it.
        """
        run, home, children = _five_role_wave(tmp_path, active_limit=4)
        _record_native_schedule(run, home, children, scenario="capacity-rejected")
        baseline = _assemble(run, home)
        assert baseline.returncode == 0, baseline.stderr
        canonical_capacity_limited = json.loads((run / "inspection-summary.json").read_bytes())["capacity_limited"]
        scenarios = [{"damage": damage} for damage in _OPAQUE_REFUSAL_DAMAGES]
        check = functools.partial(_check_opaque_capacity_refusal, run, home, children, canonical_capacity_limited)
        _replay_scenarios(subtests, tmp_path, _tree_state(tmp_path), scenarios, check)

    def test_admits_retry_after_verified_release(self, tmp_path: Path) -> None:
        """Admit a same-task refusal retry once two genuine slot releases separate the refusals.

        The second refusal follows a joined child, so the successful launch of the queued role is backed by freed
        capacity, and both assembly and preflight must accept the capacity-limited run.
        """
        run, home, children, _ = _queued_refusal_retry(tmp_path, retry_offset=4.2, state_change=True)
        assembled = _assemble(run, home)
        checked = _manifest_only_check(run, home)
        assert assembled.returncode == checked.returncode == 0, assembled.stderr + checked.stderr
        summary = json.loads((run / "inspection-summary.json").read_text(encoding="utf-8"))
        manifest = json.loads((run / "specialist-manifest.json").read_text(encoding="utf-8"))
        assert summary["capacity_limited"] is True
        assert {item["role"] for item in manifest["passes"]} == set(children)

    @pytest.mark.parametrize(
        "retry_offset",
        [
            pytest.param(0.2, id="without-release"),
            pytest.param(0.0, id="same-start"),
            pytest.param(0.005, id="overlapping-output"),
        ],
    )
    def test_rejects_retry_without_verified_release(self, tmp_path: Path, retry_offset: float) -> None:
        """Require a genuine slot release before every same-task refusal retry.

        The retry refusal arrives with no join in between (or at the same instant), so nothing proves capacity changed
        and both assembly and preflight must report the missing state change.
        """
        run, home, _, role = _queued_refusal_retry(tmp_path, retry_offset=retry_offset, state_change=False)
        assembled = _assemble(run, home)
        checked = _manifest_only_check(run, home)
        expected = f"review-inspection-capacity-refusal-no-state-change:{role}"
        assert assembled.returncode != 0
        assert checked.returncode != 0
        assert expected in assembled.stderr
        assert expected in checked.stderr

    @pytest.mark.integration
    def test_shared_parent_release_requires_bound_active_turn_and_notified_wait(
        self, tmp_path: Path, subtests: pytest.Subtests
    ) -> None:
        """Admit a genuine related slot release while preserving ordinary assembly and refusal prerequisites.

        Every damage rewrites the parent history of the same assembled native wave, so that wave is built once and each
        damage runs as an independent subtest from a byte-exact restore of it.
        """
        run, home, children = _assembly_evidence(tmp_path)
        scenarios = [{"damage": damage} for damage in _SHARED_RELEASE_DAMAGES]
        check = functools.partial(_check_shared_parent_release, run, home, children)
        _replay_scenarios(subtests, tmp_path, _tree_state(tmp_path), scenarios, check)
