"""Recover a real interrupted read prefix without inventing a completed reviewer."""

from __future__ import annotations

import copy
import importlib.util
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import test_review_prepare as preparation


def _interrupted_case(
    tmp_path: Path, *, opaque: bool = False, aggregate_only: bool = False
) -> tuple[Path, Path, dict[str, Path], list[dict]]:
    """Retain real reader outputs and a parent interruption before the final page."""
    root = preparation._review_inputs(tmp_path)
    evidence = root / "challenger-evidence.md"
    evidence.write_bytes(evidence.read_bytes() + b"Declared challenger source evidence.\n" * 100)
    source = json.loads((root / "local-source/review-worktree.json").read_bytes())["review_worktree"]
    prepared = subprocess.run(
        [
            sys.executable,
            str(preparation.HELPER),
            "prepare",
            "--out",
            str(root),
            "--run-id",
            "bounded-review",
            "--parent-thread-id",
            "parent",
            "--source-root",
            source,
            "--batches",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert prepared.returncode == 0, prepared.stderr
    run = root / "batches/source-001"
    _, home, children = preparation._assembly_evidence(tmp_path, prepared_run=run, compact_reader=True)
    dispatch = json.loads((run / "dispatch.json").read_bytes())
    assert dispatch["calls"][0]["role"] == "challenger"
    child = children["challenger"]
    complete = [json.loads(line) for line in child.read_text(encoding="utf-8").splitlines()]
    terminal = complete[-1]["payload"]
    started = terminal["started_at"]
    calls = [r["payload"] for r in complete if r.get("payload", {}).get("type") == "custom_tool_call"]
    prefix = len(calls) - 1
    assert prefix > 0
    ids = {c["call_id"] for c in calls[:prefix]}
    rows = []
    commands = 0
    for row in complete:
        payload = row["payload"]
        if payload.get("type") == "task_complete":
            continue
        if payload.get("type") in {"custom_tool_call", "custom_tool_call_output"} and payload["call_id"] not in ids:
            continue
        if payload.get("item", {}).get("type") == "CommandExecution":
            commands += 1
            if commands > prefix:
                continue
        rows.append(copy.deepcopy(row))

    def stamp(value: float) -> str:
        """Retain exact UTC ordering of native task and parent receipts."""
        return datetime.fromtimestamp(value, timezone.utc).isoformat()

    rows.insert(
        1,
        {
            "timestamp": stamp(started),
            "type": "event_msg",
            "payload": {
                "type": "task_started",
                "turn_id": terminal["turn_id"],
                "started_at": started,
            },
        },
    )
    for row in rows:
        if row["payload"].get("type") in {"custom_tool_call", "custom_tool_call_output", "item_completed"}:
            row["timestamp"] = stamp(started + 0.1)
    rows.append(
        {
            "timestamp": stamp(started + 1),
            "type": "event_msg",
            "payload": {
                "type": "turn_aborted",
                "turn_id": terminal["turn_id"],
                "started_at": started,
                "completed_at": started + 1,
                "reason": "interrupted",
            },
        }
    )
    if aggregate_only:
        _aggregate_command_outputs(rows)
        _aggregate_command_outputs(complete)
    preparation._write_jsonl(child, rows)
    parent_path = home / "sessions/rollout-parent.jsonl"
    parent = [json.loads(line) for line in parent_path.read_text(encoding="utf-8").splitlines()]
    agent = complete[0]["payload"]["agent_path"]
    parent = [r for r in parent if r.get("payload", {}).get("author") != agent]
    parent.extend(
        [
            {
                "timestamp": stamp(started + 0.9),
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "interrupt_agent",
                    "call_id": "interrupt-reader",
                    "arguments": json.dumps({"target": agent}),
                },
            },
            {
                "timestamp": stamp(started + 1.1),
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "interrupt-reader",
                    "output": json.dumps({"previous_status": "running"}),
                },
            },
        ]
    )
    sibling_path = children["qa-specialist"]
    sibling = [json.loads(line) for line in sibling_path.read_text(encoding="utf-8").splitlines()]
    sibling_agent = sibling[0]["payload"]["agent_path"]
    sibling[0]["payload"]["timestamp"] = stamp(started + 1.205)
    sibling[-1]["payload"].update(started_at=started + 1.22, completed_at=started + 10)
    preparation._write_jsonl(sibling_path, sibling)
    sibling_call_id = next(
        row["payload"]["call_id"]
        for row in parent
        if row["payload"].get("name") == "spawn_agent"
        and json.loads(row["payload"]["arguments"])["task_name"] == sibling_agent.rsplit("/", 1)[1]
    )
    for row in parent:
        payload = row["payload"]
        if payload.get("call_id") == sibling_call_id:
            row["timestamp"] = stamp(started + (1.2 if payload["type"] == "function_call" else 1.21))
        elif payload.get("author") == sibling_agent:
            row["timestamp"] = stamp(started + 10.05)
    if opaque:
        delivery = next(row["payload"] for row in rows if row["payload"].get("type") == "agent_message")
        delivery["content"][0]["encrypted_content"] = "opaque-host-delivery:interrupted-reader"
        for row in parent:
            payload = row["payload"]
            if payload.get("type") == "function_call" and payload.get("name") == "spawn_agent":
                arguments = json.loads(payload["arguments"])
                if arguments["task_name"] == agent.rsplit("/", 1)[1]:
                    arguments["message"] = delivery["content"][0]["encrypted_content"]
                    payload["arguments"] = json.dumps(arguments)
        preparation._write_jsonl(child, rows)
    parent.sort(
        key=lambda row: (
            datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")).timestamp()
            if "timestamp" in row
            else float("-inf")
        )
    )
    preparation._write_jsonl(parent_path, parent)
    return run, home, children, complete


def _prepare_interrupted(run: Path, home: Path) -> subprocess.CompletedProcess[str]:
    """Exercise the shipped recovery command against retained runtime receipts."""
    return subprocess.run(
        [
            sys.executable,
            str(preparation.HELPER),
            "prepare-repair",
            "--out",
            str(run),
            "--codex-home",
            str(home),
            "--role",
            "challenger",
            "--kind",
            "interrupted-dispatch",
        ],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.integration
@pytest.mark.parametrize("opaque", [False, True])
@pytest.mark.parametrize("aggregate_only", [False, True])
def test_interrupted_prefix_prepares_real_replacement(tmp_path: Path, opaque: bool, aggregate_only: bool) -> None:
    """A parent interrupt must not force all routed roles into parent substitutes."""
    run, home, children, complete = _interrupted_case(tmp_path, opaque=opaque, aggregate_only=aggregate_only)
    original = {p: p.read_bytes() for p in [*children.values(), run / "inspection-plan.json", run / "dispatch.json"]}
    prepared = _prepare_interrupted(run, home)
    assert prepared.returncode == 0, prepared.stderr
    result = json.loads(prepared.stdout)
    assert result["kind"] == "interrupted-dispatch"
    assert result["arguments"]["task_name"].endswith("_a2")
    blocks = re.findall(r"```javascript\n(.*?)\n```", result["arguments"]["message"], re.S)
    assert len(blocks) > 1
    assert "--attempt 2" in blocks[0]
    assert all(path.read_bytes() == data for path, data in original.items())
    assert not list((run / "specialists").glob("challenger.a1*"))

    replacement = _complete_replacement(run, home, complete, result["arguments"])
    assembled = subprocess.run(
        [sys.executable, str(preparation.HELPER), "assemble-wave", "--out", str(run), "--codex-home", str(home)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert assembled.returncode == 0, assembled.stderr
    manifest = json.loads((run / "specialist-manifest.json").read_bytes())
    repaired = next(item for item in manifest["passes"] if item["role"] == "challenger")
    sibling = next(item for item in manifest["passes"] if item["role"] == "qa-specialist")
    assert manifest["schema_version"] == 8
    assert repaired["selected_attempt"] == 2
    assert len(repaired["attempts"]) == 2
    assert repaired["attempts"][0]["status"] == "interrupted"
    assert "output_path" not in repaired["attempts"][0]
    assert "raw_output_path" not in repaired["attempts"][0]
    assert repaired["attempts"][1]["agent_thread_id"] == replacement[0]["payload"]["id"]
    assert sibling["selected_attempt"] == 1
    assert len(sibling["attempts"]) == 1
    assert all(path.read_bytes() == data for path, data in original.items())
    assert not list((run / "specialists").glob("challenger.a1*"))
    root = run.parent.parent
    aggregated = subprocess.run(
        [sys.executable, str(preparation.HELPER), "assemble-batches", "--out", str(root), "--codex-home", str(home)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert aggregated.returncode == 0, aggregated.stderr
    aggregate = json.loads((root / "specialist-manifest.json").read_bytes())
    assert aggregate["schema_version"] == 9
    assert aggregate["review_topology"] == "source-only"
    assert aggregate["manifest_kind"] == "batched-review"
    spec = importlib.util.spec_from_file_location("interrupted_acceptance", preparation.SKILL / "validate_artifacts.py")
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    for retained in (False, True):
        by_role = validator._validate_manifest_entries(
            root,
            aggregate,
            aggregate["passes"],
            {"challenger", "qa-specialist"},
            home,
            "parent",
            tmp_path,
            retained_role_cards=retained,
            require_role_card_receipts=True,
        )
        assert set(by_role) == {"challenger", "qa-specialist"}
    runtime = validator._validate_review_runtime(root, aggregate, aggregate["passes"], home, "parent")
    assert runtime["actual_mode"] == "independent-spawned"
    assert runtime["independence_satisfied"] is True
    if not opaque and not aggregate_only:
        for field in ("output_path", "raw_output_path", "output_sha256", "raw_output_sha256"):
            forged = copy.deepcopy(manifest)
            original_attempt = next(item for item in forged["passes"] if item["role"] == "challenger")["attempts"][0]
            original_attempt[field] = repaired["attempts"][1].get(field, "fabricated")
            with pytest.raises(SystemExit, match="review-interrupted-attempt-invalid"):
                validator._validate_manifest_entries(
                    run,
                    forged,
                    forged["passes"],
                    {"challenger", "qa-specialist"},
                    home,
                    "parent",
                    tmp_path,
                    require_role_card_receipts=True,
                )


def _complete_replacement(run: Path, home: Path, complete: list[dict], arguments: dict) -> list[dict]:
    """Record every freshly executed attempt-two page and its ordinary completed turn."""
    old_path = complete[0]["payload"]["agent_path"]
    new_path = "/root/" + arguments["task_name"]
    rows = copy.deepcopy(complete)
    rows[0]["payload"].update(id="interrupted-replacement", agent_path=new_path, timestamp="2026-01-01T10:01:00.050Z")
    rows[0]["payload"]["source"]["subagent"]["thread_spawn"]["agent_path"] = new_path
    turn = next(row["payload"] for row in rows if row["type"] == "turn_context")
    turn["turn_id"] = "replacement-turn"
    delivery = next(row["payload"] for row in rows if row["payload"].get("type") == "agent_message")
    delivery["content"][0]["encrypted_content"] = arguments["message"]
    blocks = re.findall(r"```javascript\n(.*?)\n```", arguments["message"], re.S)
    calls = [row["payload"] for row in rows if row["payload"].get("type") == "custom_tool_call"]
    commands = [
        row["payload"]["item"] for row in rows if row["payload"].get("item", {}).get("type") == "CommandExecution"
    ]
    assert len(calls) == len(commands) == len(blocks)
    for page, (call, command, block) in enumerate(zip(calls, commands, blocks, strict=True), 1):
        call["input"] = block
        literal = preparation._CONTEXT_READ_CALL(run / "inspection-plan.json", "challenger", 2, sys.executable, page)
        expanded = json.loads(literal.split("tools.exec_command(", 1)[1].split("); text", 1)[0])
        output = preparation._CONTEXT_READER(run / "inspection-plan.json", "challenger", 2, page)
        command["command"] = [expanded["cmd"]]
        command["aggregated_output" if "aggregated_output" in command else "stdout"] = output
        receipt = next(
            row["payload"]
            for row in rows
            if row["payload"].get("type") == "custom_tool_call_output" and row["payload"]["call_id"] == call["call_id"]
        )
        receipt["output"][1]["text"] = output
    terminal = rows[-1]["payload"]
    terminal.update(turn_id=turn["turn_id"], started_at=1767261660.2, completed_at=1767261661.0)
    preparation._write_jsonl(home / "sessions/rollout-interrupted-replacement.jsonl", rows)
    parent_path = home / "sessions/rollout-parent.jsonl"
    parent = [json.loads(line) for line in parent_path.read_text(encoding="utf-8").splitlines()]
    assert old_path != new_path
    parent.extend(
        [
            {
                "timestamp": "2026-01-01T10:01:00.000Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "spawn_agent",
                    "call_id": "interrupted-repair-spawn",
                    "arguments": json.dumps(arguments),
                },
            },
            {
                "timestamp": "2026-01-01T10:01:00.100Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "interrupted-repair-spawn",
                    "output": json.dumps({"task_name": new_path}),
                },
            },
            {
                "timestamp": "2026-01-01T10:01:01.100Z",
                "type": "response_item",
                "payload": {
                    "type": "agent_message",
                    "author": new_path,
                    "content": [
                        {
                            "type": "input_text",
                            "text": f"Message Type: FINAL_ANSWER\nPayload:\n{terminal['last_agent_message']}",
                        }
                    ],
                },
            },
        ]
    )
    preparation._write_jsonl(parent_path, parent)
    return rows


def _aggregate_command_outputs(rows: list[dict]) -> None:
    """Use the native host's completed aggregated-output receipt without invented stream fields."""
    for row in rows:
        item = row["payload"].get("item", {})
        if item.get("type") == "CommandExecution":
            item["aggregated_output"] = item.pop("stdout")
            assert item.pop("stderr") == ""
            item["status"] = "completed"


@pytest.mark.integration
def test_interrupted_repair_rejects_unproved_prefixes(tmp_path: Path, subtests: pytest.Subtests) -> None:
    """Every retained interruption boundary must be proved before replacement arguments are issued."""
    run, home, children, complete = _interrupted_case(tmp_path, opaque=True)
    baseline = _prepare_interrupted(run, home)
    assert baseline.returncode == 0, baseline.stderr
    (run / "repair-dispatch.challenger.json").unlink()
    original = preparation._tree_state(tmp_path)
    child_path = children["challenger"]
    parent_path = home / "sessions/rollout-parent.jsonl"
    for damage in [
        "empty-prefix",
        "full-prefix",
        "wrong-turn",
        "wrong-parent-target",
        "parent-was-complete",
        "missing-parent-result",
        "parent-result-before-abort",
        "interrupt-before-read",
        "read-after-abort",
        "extra-tool",
        "assistant-assessment",
        "missing-command",
        "missing-abort",
        "repeated-abort",
        "missing-start",
        "repeated-start",
        "wrong-model",
        "wrong-effort",
        "wrong-context",
        "wrong-source",
        "missing-delivery",
        "different-delivery",
        "pending-original",
    ]:
        with subtests.test(damage=damage):
            preparation._restore_tree(tmp_path, original)
            child = [json.loads(line) for line in child_path.read_text(encoding="utf-8").splitlines()]
            parent = [json.loads(line) for line in parent_path.read_text(encoding="utf-8").splitlines()]
            abort = next(row for row in child if row["payload"].get("type") == "turn_aborted")
            start = next(row for row in child if row["payload"].get("type") == "task_started")
            interrupt = next(row for row in parent if row["payload"].get("name") == "interrupt_agent")
            result = next(
                row
                for row in parent
                if row["payload"].get("call_id") == "interrupt-reader"
                and row["payload"]["type"] == "function_call_output"
            )
            calls = [row for row in child if row["payload"].get("type") == "custom_tool_call"]
            if damage == "empty-prefix":
                child = [
                    row
                    for row in child
                    if row["payload"].get("type")
                    not in {"custom_tool_call", "custom_tool_call_output", "item_completed"}
                ]
            elif damage == "full-prefix":
                child = [
                    row
                    for row in child
                    if row["payload"].get("type")
                    not in {"custom_tool_call", "custom_tool_call_output", "item_completed"}
                ]
                full = [
                    copy.deepcopy(row)
                    for row in complete
                    if row["payload"].get("type") in {"custom_tool_call", "custom_tool_call_output", "item_completed"}
                ]
                for row in full:
                    row["timestamp"] = calls[0]["timestamp"]
                child[-1:-1] = full
            elif damage == "wrong-turn":
                abort["payload"]["turn_id"] = "different-turn"
            elif damage == "wrong-parent-target":
                interrupt["payload"]["arguments"] = json.dumps({"target": "/root/unrelated"})
            elif damage == "parent-was-complete":
                result["payload"]["output"] = json.dumps({"previous_status": "completed"})
            elif damage == "missing-parent-result":
                parent.remove(result)
            elif damage == "parent-result-before-abort":
                result["timestamp"] = start["timestamp"]
            elif damage == "interrupt-before-read":
                interrupt["timestamp"] = start["timestamp"]
            elif damage == "read-after-abort":
                calls[-1]["timestamp"] = result["timestamp"]
            elif damage == "extra-tool":
                child.insert(
                    -1,
                    {
                        "type": "response_item",
                        "timestamp": calls[0]["timestamp"],
                        "payload": {
                            "type": "function_call",
                            "name": "exec_command",
                            "call_id": "unrelated-read",
                            "arguments": json.dumps({"cmd": "echo unrelated"}),
                        },
                    },
                )
            elif damage == "assistant-assessment":
                child.insert(
                    -1,
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {"type": "output_text", "text": "## Reviewer Assessment\nRating: 1\nRationale: Clean."}
                            ],
                        },
                    },
                )
            elif damage == "missing-command":
                child.remove(
                    next(row for row in child if row["payload"].get("item", {}).get("type") == "CommandExecution")
                )
            elif damage == "missing-abort":
                child.remove(abort)
            elif damage == "repeated-abort":
                child.append(copy.deepcopy(abort))
            elif damage == "missing-start":
                child.remove(start)
            elif damage == "repeated-start":
                child.append(copy.deepcopy(start))
            elif damage in {"wrong-model", "wrong-effort"}:
                context = next(row["payload"] for row in child if row["type"] == "turn_context")
                context["model" if damage == "wrong-model" else "effort"] = "unselected"
            elif damage in {"wrong-context", "wrong-source"}:
                receipt = next(
                    row["payload"] for row in child if row["payload"].get("type") == "custom_tool_call_output"
                )
                output = receipt["output"][1]["text"]
                plan = json.loads((run / "inspection-plan.json").read_bytes())
                if damage == "wrong-source":
                    assert plan["review_input_sha256"] in output
                    receipt["output"][1]["text"] = output.replace(plan["review_input_sha256"], "0" * 64)
                else:
                    receipt["output"][1]["text"] += "\nChanged frozen context."
            elif damage in {"missing-delivery", "different-delivery"}:
                delivery = next(row for row in child if row["payload"].get("type") == "agent_message")
                if damage == "missing-delivery":
                    child.remove(delivery)
                else:
                    delivery["payload"]["content"][0]["encrypted_content"] = "different-opaque-delivery"
            elif damage == "pending-original":
                sibling = json.loads(children["qa-specialist"].read_text(encoding="utf-8").splitlines()[0])["payload"][
                    "agent_path"
                ]
                spawn = next(
                    row
                    for row in parent
                    if row["payload"].get("name") == "spawn_agent"
                    and json.loads(row["payload"]["arguments"])["task_name"] == sibling.rsplit("/", 1)[1]
                )
                parent = [
                    row
                    for row in parent
                    if row["payload"].get("call_id") != spawn["payload"]["call_id"]
                    and row["payload"].get("author") != sibling
                ]
                children["qa-specialist"].unlink()
            preparation._write_jsonl(child_path, child)
            preparation._write_jsonl(parent_path, parent)
            before = preparation._tree_state(tmp_path)
            rejected = _prepare_interrupted(run, home)
            assert rejected.returncode != 0, damage
            assert rejected.stderr.strip(), damage
            assert preparation._tree_state(tmp_path) == before


@pytest.mark.integration
def test_interrupted_replacement_requires_fresh_complete_evidence(tmp_path: Path, subtests: pytest.Subtests) -> None:
    """Replacement admission rejects incomplete rereads and conflicting native command receipts."""
    run, home, children, complete = _interrupted_case(tmp_path, aggregate_only=True)
    prepared = _prepare_interrupted(run, home)
    assert prepared.returncode == 0, prepared.stderr
    _complete_replacement(run, home, complete, json.loads(prepared.stdout)["arguments"])
    original = preparation._tree_state(tmp_path)
    replacement_path = home / "sessions/rollout-interrupted-replacement.jsonl"
    for damage in [
        "missing-page",
        "aborted-replacement",
        "third-attempt",
        "original-tampered-output",
        "replacement-tampered-output",
        "original-mixed-output",
        "replacement-mixed-output",
        "incomplete-command",
        "unrelated-queue-work",
    ]:
        with subtests.test(damage=damage):
            preparation._restore_tree(tmp_path, original)
            path = children["challenger"] if damage.startswith("original-") else replacement_path
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            commands = [
                row["payload"]["item"]
                for row in rows
                if row["payload"].get("item", {}).get("type") == "CommandExecution"
            ]
            if damage == "missing-page":
                last = [row["payload"] for row in rows if row["payload"].get("type") == "custom_tool_call"][-1]
                rows = [row for row in rows if row["payload"].get("call_id") != last["call_id"]]
                rows.remove(next(row for row in rows if row["payload"].get("item") is commands[-1]))
            elif damage == "aborted-replacement":
                rows[-1]["payload"].update(type="turn_aborted", reason="interrupted")
                rows[-1]["payload"].pop("last_agent_message")
            elif damage == "third-attempt":
                third = json.loads(
                    json.dumps(rows).replace("_a2", "_a3").replace("interrupted-replacement", "third-replacement")
                )
                preparation._write_jsonl(home / "sessions/rollout-third-replacement.jsonl", third)
                parent_path = home / "sessions/rollout-parent.jsonl"
                parent = [json.loads(line) for line in parent_path.read_text(encoding="utf-8").splitlines()]
                extra = [
                    copy.deepcopy(row) for row in parent if row["payload"].get("call_id") == "interrupted-repair-spawn"
                ]
                extra = json.loads(
                    json.dumps(extra)
                    .replace("_a2", "_a3")
                    .replace("interrupted-repair-spawn", "third-repair-spawn")
                    .replace("10:01:00", "10:02:00")
                )
                parent.extend(extra)
                preparation._write_jsonl(parent_path, parent)
            elif damage == "unrelated-queue-work":
                parent_path = home / "sessions/rollout-parent.jsonl"
                parent = [json.loads(line) for line in parent_path.read_text(encoding="utf-8").splitlines()]
                interrupted = [
                    json.loads(line) for line in children["challenger"].read_text(encoding="utf-8").splitlines()
                ]
                read_at = next(
                    row["timestamp"] for row in interrupted if row["payload"].get("type") == "custom_tool_call"
                )
                parent.append(
                    {
                        "timestamp": read_at,
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "name": "exec_command",
                            "call_id": "unrelated-parent-work",
                            "arguments": json.dumps({"cmd": "echo unrelated"}),
                        },
                    }
                )
                parent.sort(
                    key=lambda row: (
                        datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")).timestamp()
                        if "timestamp" in row
                        else float("-inf")
                    )
                )
                preparation._write_jsonl(parent_path, parent)
            elif damage.endswith("tampered-output"):
                commands[0]["aggregated_output"] += "\nUnbound source text."
            elif damage.endswith("mixed-output"):
                commands[0].update(stdout="Conflicting output", stderr="")
            else:
                commands[0]["status"] = "in_progress"
            preparation._write_jsonl(path, rows)
            result = subprocess.run(
                [
                    sys.executable,
                    str(preparation.HELPER),
                    "assemble-wave",
                    "--out",
                    str(run),
                    "--codex-home",
                    str(home),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            assert result.returncode != 0, damage
            assert result.stderr.strip(), damage
            assert not (run / "specialist-manifest.json").exists()
