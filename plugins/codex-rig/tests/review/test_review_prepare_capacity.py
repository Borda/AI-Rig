"""Exercise native review scheduling, active capacity, and bound refusal evidence."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timezone

import pytest

from test_review_prepare import (
    HELPER,
    PLUGIN_ROOT,
    SKILL,
    _CONTEXT_READER,
    _assemble,
    _assembly_evidence,
    _five_role_review_inputs,
    _prepare,
    _record_native_schedule,
    _review_inputs,
    _write_jsonl,
)


def test_prepare_five_roles_orders_complete_contexts_and_stable_ties(tmp_path: Path) -> None:
    """Queue the entire roster by descending frozen size, retaining deterministic equal-size order."""
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
    assert [
        call["arguments"]["task_name"].removeprefix("review_").rsplit("_", 2)[0].replace("_", "-")
        for call in first_dispatch["calls"]
    ] == sorted(first_sizes, key=lambda role: (-first_sizes[role], role))
    target = max(first_sizes.values()) + 100
    tied = _five_role_review_inputs(second)
    for entry in plan["contexts"]:
        role = entry["role_id"]
        evidence = tied / f"{role}-evidence.md"
        evidence.write_bytes(evidence.read_bytes() + b"x" * (target - (run / entry["context_path"]).stat().st_size))
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
    assert [
        call["arguments"]["task_name"].removeprefix("review_").rsplit("_", 2)[0].replace("_", "-")
        for call in dispatch["calls"]
    ] == expected
    frozen_dispatch = (tied / "dispatch.json").read_bytes()
    assert _prepare(tied).returncode == 0
    assert (tied / "dispatch.json").read_bytes() == frozen_dispatch
    tied, home, children = _assembly_evidence(second, prepared_run=tied, active_limit=4)
    assert _assemble(tied, home).returncode == 0
    _record_native_schedule(tied, home, children, scenario="reversed-largest-order")
    reversed_ties = _assemble(tied, home)
    assert reversed_ties.returncode != 0
    assert "review-inspection-dispatch-order-mismatch" in reversed_ties.stderr


@pytest.mark.parametrize("peak", [4, 5])
def test_five_role_native_admission_limits_active_children(tmp_path: Path, peak: int) -> None:
    """Accept five completed roles in a four-child pool, rejecting five overlapping tasks."""
    run = _five_role_review_inputs(tmp_path)
    completed = _prepare(run)
    assert completed.returncode == 0, completed.stderr
    run, home, children = _assembly_evidence(tmp_path, prepared_run=run, active_limit=peak)
    assembled = _assemble(run, home)
    if peak == 5:
        assert assembled.returncode != 0
        assert "review-inspection-active-capacity-exceeded" in assembled.stderr
        assert not (run / "specialist-manifest.json").exists()
        return
    assert assembled.returncode == 0, assembled.stderr
    manifest = json.loads((run / "specialist-manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 8
    assert {item["role"] for item in manifest["passes"]} == set(children)
    assert len(children) == 5
    for item in manifest["passes"]:
        assert item["mode"] == "inspection"
        terminal = json.loads(children[item["role"]].read_text(encoding="utf-8").splitlines()[-1])["payload"][
            "last_agent_message"
        ]
        assert (run / item["output_path"]).read_bytes() == (terminal.strip() + "\n").encode()


def test_default_historical_inspection_context_limit_remains_four(tmp_path: Path) -> None:
    """The explicit native roster extension cannot relax the shared default historical boundary."""
    run = _five_role_review_inputs(tmp_path)
    result = _prepare(run)
    assert result.returncode == 0, result.stderr
    plan = json.loads((run / "inspection-plan.json").read_text(encoding="utf-8"))
    spec = importlib.util.spec_from_file_location(
        "historical_context_boundary", PLUGIN_ROOT / "shared/parallel_execution.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    with pytest.raises(ValueError, match="review-inspection-contexts-invalid"):
        module.validate_inspection_contexts(plan, run / "inspection-plan.json")


@pytest.mark.parametrize("case", ["failed-peak-five", "missing-failed-timing", "sequential-selected"])
def test_native_retry_timing_cannot_evade_capacity_or_fabricate_parallelism(tmp_path: Path, case: str) -> None:
    """Count every launched attempt while deriving parallel review only from selected completions."""
    run = _five_role_review_inputs(tmp_path)
    assert _prepare(run).returncode == 0
    run, home, children = _assembly_evidence(tmp_path, prepared_run=run, active_limit=4, final_header="missing")
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
    old_rows = [json.loads(line) for line in children["sw-engineer"].read_text(encoding="utf-8").splitlines()]
    new_rows = json.loads(
        json.dumps(old_rows)
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
    parent = [json.loads(line) for line in parent_path.read_text(encoding="utf-8").splitlines()]
    original_call = next(
        row
        for row in parent
        if row.get("payload", {}).get("call_id") == failed["spawn_call_id"]
        and row["payload"].get("type") == "function_call"
    )
    call = json.loads(json.dumps(original_call))
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
    for index, entry in enumerate(manifest["passes"]):
        attempt = entry["attempts"][entry["selected_attempt"] - 1]
        path = selected_path if entry["role"] == "sw-engineer" else children[entry["role"]]
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        completed = next(row["payload"] for row in rows if row.get("payload", {}).get("type") == "task_complete")
        epoch = datetime(2026, 1, 1, 10, tzinfo=timezone.utc).timestamp()
        selected_starts = {"qa-specialist": 1, "doc-scribe": 3, "data-steward": 5, "challenger": 7, "sw-engineer": 9}
        start = (
            epoch + selected_starts[entry["role"]]
            if case == "sequential-selected"
            else epoch + (100 + index if index < 4 else 130)
        )
        completed.update(started_at=start, completed_at=start + (1 if case == "sequential-selected" else 10))
        assert completed["turn_id"] == attempt["turn_id"]
        _write_jsonl(path, rows)
    failed_rows = [json.loads(line) for line in children["sw-engineer"].read_text(encoding="utf-8").splitlines()]
    failed_completion = next(
        row["payload"] for row in failed_rows if row.get("payload", {}).get("type") == "task_complete"
    )
    failed_completion.update(
        started_at=epoch + (0.2 if case == "sequential-selected" else 105),
        completed_at=epoch + (1.5 if case == "sequential-selected" else 110),
    )
    if case == "missing-failed-timing":
        failed_rows = [row for row in failed_rows if row.get("payload", {}).get("type") != "task_complete"]
    _write_jsonl(children["sw-engineer"], failed_rows)
    if case != "missing-failed-timing":
        # Keep every launch and join coherent with its selected or failed task.
        # The negative retry overlaps four live children; the sequential selected
        # case instead refills joined slots while failed work alone overlaps.
        launch_offsets = {
            "qa-specialist": 0.28,
            "doc-scribe": 0.38,
            "data-steward": 0.48,
            "challenger": 1.8 if case == "sequential-selected" else 5.28,
            "sw-engineer": 4.2 if case == "sequential-selected" else 5,
        }
        path_times = {}
        for role, path in {**children, "sw-engineer": selected_path}.items():
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            agent_path = rows[0]["payload"]["agent_path"]
            launch = epoch + launch_offsets[role]
            terminal = next(row["payload"] for row in rows if row.get("payload", {}).get("type") == "task_complete")
            end = terminal["completed_at"] + 0.05
            rows[0]["payload"]["timestamp"] = (
                datetime.fromtimestamp(launch + 0.005, timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            )
            _write_jsonl(path, rows)
            path_times[agent_path] = (launch, end)
        path_times[failed["agent_path"]] = (epoch + 0.18, failed_completion["completed_at"] + 0.05)
        parent = [json.loads(line) for line in parent_path.read_text(encoding="utf-8").splitlines()]
        call_times = {}
        for row in parent:
            payload = row.get("payload", {})
            if payload.get("type") == "function_call" and payload.get("name") == "spawn_agent":
                arguments = json.loads(payload["arguments"])
                path = next(path for path in path_times if path.rsplit("/", 1)[-1] == arguments["task_name"])
                value = path_times[path][0]
                call_times[payload["call_id"]] = value
            elif payload.get("type") == "function_call_output":
                value = call_times[payload["call_id"]] + 0.01
            elif payload.get("type") == "agent_message":
                value = path_times[payload["author"]][1]
            else:
                continue
            row["timestamp"] = (
                datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            )
        parent.sort(key=lambda row: row.get("timestamp", ""))
        _write_jsonl(parent_path, parent)
    spec = importlib.util.spec_from_file_location("retry_capacity_validator", SKILL / "validate_artifacts.py")
    validator = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = validator
    spec.loader.exec_module(validator)
    if case != "sequential-selected":
        expected = (
            "review-inspection-active-capacity-exceeded"
            if case == "failed-peak-five"
            else "review-inspection-attempt-timing-missing:sw-engineer:1"
        )
        with pytest.raises(SystemExit, match=expected):
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
    else:
        validator._validate_manifest_entries(
            run, manifest, manifest["passes"], set(children), home, "parent", tmp_path, require_role_card_receipts=True
        )
        summary = validator._validate_review_runtime(run, manifest, manifest["passes"], home, "parent")
        assert summary["actual_mode"] == "independent-spawned"


@pytest.mark.parametrize(
    "scenario",
    [
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
    ],
)
def test_actual_native_parent_sequence_controls_allocation_order_and_refill(tmp_path: Path, scenario: str) -> None:
    """Reject invalid parent scheduling even when every role completes and work peak remains four."""
    run = _five_role_review_inputs(tmp_path)
    assert _prepare(run).returncode == 0
    run, home, children = _assembly_evidence(tmp_path, prepared_run=run, active_limit=4)
    # Retain a valid manifest first so both assembly and ordinary preflight can replay changed parent history.
    initial = _assemble(run, home)
    assert initial.returncode == 0, initial.stderr
    _record_native_schedule(run, home, children, scenario=scenario)
    assembled = _assemble(run, home)
    checked = subprocess.run(
        [
            sys.executable,
            str(SKILL / "validate_artifacts.py"),
            "--out",
            str(run),
            "--manifest-only",
            "--codex-home",
            str(home),
            "--parent-thread-id",
            "parent",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if scenario in {"proper-refill", "blocked-wait-coalesces-joins", "wait-for-all-before-refill"}:
        assert assembled.returncode == checked.returncode == 0, assembled.stderr + checked.stderr
        manifest = json.loads((run / "specialist-manifest.json").read_text(encoding="utf-8"))
        assert {item["role"] for item in manifest["passes"]} == set(children)
    else:
        assert assembled.returncode != 0, f"invalid {scenario} scheduling admitted"
        assert checked.returncode != 0, f"ordinary preflight admitted {scenario}"
        expected = {
            "spawn-all-five": "review-inspection-active-capacity-exceeded",
            "reversed-largest-order": "review-inspection-dispatch-order-mismatch",
            "wait-again-with-free-slot": "review-inspection-refill-opportunity-missed",
            "default-wait-again-with-free-slot": "review-inspection-refill-opportunity-missed",
            "30s-wait-again-with-free-slot": "review-inspection-refill-opportunity-missed",
            "completed-wait-again-with-free-slot": "review-inspection-refill-opportunity-missed",
            "join-before-terminal": "review-inspection-active-capacity-exceeded",
        }[scenario]
        assert expected in assembled.stderr
        assert expected in checked.stderr


@pytest.mark.parametrize("pool_size", [1, 2, 3])
def test_smaller_observed_native_pool_refills_complete_roster(tmp_path: Path, pool_size: int) -> None:
    """Accept a coherent smaller active pool without dropping roles or inventing available capacity."""
    run = _five_role_review_inputs(tmp_path)
    assert _prepare(run).returncode == 0
    run, home, children = _assembly_evidence(tmp_path, prepared_run=run, active_limit=pool_size)
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
    assert {item["role"] for item in manifest["passes"]} == set(children)
    assert len(manifest["passes"]) == 5
    summary = json.loads((run / "inspection-summary.json").read_text(encoding="utf-8"))
    assert summary["actual_mode"] == ("independent-spawned" if pool_size == 1 else "parallel")
    assert summary["capacity_limited"] is True
    checked = subprocess.run(
        [
            sys.executable,
            str(SKILL / "validate_artifacts.py"),
            "--out",
            str(run),
            "--manifest-only",
            "--codex-home",
            str(home),
            "--parent-thread-id",
            "parent",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert checked.returncode == 0, checked.stderr


@pytest.mark.integration
@pytest.mark.parametrize("batched", [False, True])
@pytest.mark.parametrize("interruption", ["none", "wait", "unrelated-tool"])
def test_fast_native_assembly_requires_uninterrupted_pending_dispatch(
    tmp_path: Path, batched: bool, interruption: str
) -> None:
    """Admit authentic fast children while rejecting parent work or waiting with a free pending slot."""
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
    parent_path = home / "sessions/rollout-parent.jsonl"
    rows = [json.loads(line) for line in parent_path.read_text(encoding="utf-8").splitlines()]
    launches = [
        row
        for row in rows
        if row.get("payload", {}).get("type") == "function_call" and row["payload"].get("name") == "spawn_agent"
    ]
    assert len(launches) == 2
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
        assert summary["independence_required"] is True and summary["independence_satisfied"] is True
    assert all(path.read_bytes() == content for path, content in original.items())


def test_serial_native_roster_with_parent_tool_delay_is_rejected(tmp_path: Path) -> None:
    """Reject unrelated parent work interrupting free-capacity original reviewer launches."""
    run = _five_role_review_inputs(tmp_path)
    assert _prepare(run).returncode == 0
    run, home, _ = _assembly_evidence(tmp_path, prepared_run=run, active_limit=1)
    parent = next((home / "sessions").rglob("*parent*.jsonl"))
    rows = [json.loads(line) for line in parent.read_text(encoding="utf-8").splitlines()]
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


@pytest.mark.parametrize(
    "damage",
    [
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
    ],
)
def test_native_capacity_refusal_accepts_only_bound_no_child_opaque_transport(tmp_path: Path, damage: str) -> None:
    """Opaque refused messages cannot supply source coverage or weaken spawn controls and release proof."""
    run = _five_role_review_inputs(tmp_path)
    assert _prepare(run).returncode == 0
    run, home, children = _assembly_evidence(tmp_path, prepared_run=run, active_limit=4)
    _record_native_schedule(run, home, children, scenario="capacity-rejected")
    baseline = _assemble(run, home)
    assert baseline.returncode == 0, baseline.stderr
    canonical_capacity_limited = json.loads((run / "inspection-summary.json").read_bytes())["capacity_limited"]
    spec = importlib.util.spec_from_file_location("opaque_refusal_prepare", HELPER)
    prepare = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prepare)
    parent_path = home / "sessions/rollout-parent.jsonl"
    parent = [json.loads(line) for line in parent_path.read_text(encoding="utf-8").splitlines()]
    refusal_output = next(
        row
        for row in parent
        if row.get("payload", {}).get("output") == "collab spawn failed: agent thread limit reached"
    )
    refusal_id = refusal_output["payload"]["call_id"]
    refusal_call = next(
        row
        for row in parent
        if row.get("payload", {}).get("call_id") == refusal_id and row["payload"]["type"] == "function_call"
    )
    args = json.loads(refusal_call["payload"]["arguments"])
    name = args["task_name"]
    role = name.removeprefix("review_").rsplit("_", 2)[0].replace("_", "-")
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
    rows = [json.loads(line) for line in child.read_text(encoding="utf-8").splitlines()]
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
        extra = json.loads(json.dumps(rows[0]))
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


@pytest.mark.parametrize(
    ("retry_offset", "state_change"),
    [
        pytest.param(0.2, False, id="without-release"),
        pytest.param(4.2, True, id="after-two-releases"),
        pytest.param(0.0, False, id="same-start"),
        pytest.param(0.005, False, id="overlapping-output"),
    ],
)
def test_same_queued_role_capacity_refusal_requires_each_verified_release(
    tmp_path: Path, retry_offset: float, state_change: bool
) -> None:
    """Require a genuine slot release before every same-task refusal retry."""
    run = _five_role_review_inputs(tmp_path)
    assert _prepare(run).returncode == 0
    run, home, children = _assembly_evidence(tmp_path, prepared_run=run, active_limit=4)
    if not state_change:
        initial = _assemble(run, home)
        assert initial.returncode == 0, initial.stderr
    _record_native_schedule(run, home, children, scenario="capacity-rejected")
    parent_path = home / "sessions/rollout-parent.jsonl"
    parent = [json.loads(line) for line in parent_path.read_text(encoding="utf-8").splitlines()]
    refusal_output = next(
        row
        for row in parent
        if row.get("payload", {}).get("output") == "collab spawn failed: agent thread limit reached"
    )
    refusal_id = refusal_output["payload"]["call_id"]
    refusal_call = next(
        row
        for row in parent
        if row.get("payload", {}).get("call_id") == refusal_id and row["payload"]["type"] == "function_call"
    )
    second_call = json.loads(json.dumps(refusal_call))
    second_output = json.loads(json.dumps(refusal_output))
    epoch = datetime(2026, 1, 1, 10, tzinfo=timezone.utc).timestamp()
    second_at = epoch + 1 + retry_offset
    for row, instant in [(second_call, second_at), (second_output, second_at + 0.01)]:
        row["payload"]["call_id"] = refusal_id + "-retry"
        row["timestamp"] = (
            datetime.fromtimestamp(instant, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        )
    parent.extend([second_call, second_output])
    arguments = json.loads(refusal_call["payload"]["arguments"])
    role = arguments["task_name"].removeprefix("review_").rsplit("_", 2)[0].replace("_", "-")
    if state_change:
        # First refusal precedes the first join; the second refusal follows it.
        # Only the next joined child frees capacity for the successful same task.
        child_rows = [json.loads(line) for line in children[role].read_text(encoding="utf-8").splitlines()]
        agent_path = child_rows[0]["payload"]["agent_path"]
        child_rows[0]["payload"]["timestamp"] = "2026-01-01T10:00:10.285Z"
        terminal = next(row["payload"] for row in child_rows if row.get("payload", {}).get("type") == "task_complete")
        terminal.update(started_at=epoch + 10.3, completed_at=epoch + 20.3)
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
    assembled = _assemble(run, home)
    checked = subprocess.run(
        [
            sys.executable,
            str(SKILL / "validate_artifacts.py"),
            "--out",
            str(run),
            "--manifest-only",
            "--codex-home",
            str(home),
            "--parent-thread-id",
            "parent",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if state_change:
        assert assembled.returncode == checked.returncode == 0, assembled.stderr + checked.stderr
        summary = json.loads((run / "inspection-summary.json").read_text(encoding="utf-8"))
        assert summary["capacity_limited"] is True
        manifest = json.loads((run / "specialist-manifest.json").read_text(encoding="utf-8"))
        assert {item["role"] for item in manifest["passes"]} == set(children)
    else:
        expected = f"review-inspection-capacity-refusal-no-state-change:{role}"
        assert assembled.returncode != 0
        assert checked.returncode != 0
        assert expected in assembled.stderr
        assert expected in checked.stderr


@pytest.mark.parametrize(
    "damage",
    [
        "none",
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
    ],
)
@pytest.mark.integration
def test_shared_parent_capacity_release_requires_bound_active_turn_and_notified_wait(
    tmp_path: Path, damage: str
) -> None:
    """Admit a genuine related slot release while preserving ordinary assembly and refusal prerequisites."""
    run, home, children = _assembly_evidence(tmp_path)
    spec = importlib.util.spec_from_file_location("shared_capacity_prepare", HELPER)
    prepare = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prepare)
    parent_path = home / "sessions/rollout-parent.jsonl"
    parent = [json.loads(line) for line in parent_path.read_text(encoding="utf-8").splitlines()]
    epoch = datetime(2026, 1, 1, 10, tzinfo=timezone.utc).timestamp()

    def stamp(offset: float) -> str:
        """Match the host's precise event timestamp format."""
        return (
            datetime.fromtimestamp(epoch + offset, timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )

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
    refusal = json.loads(json.dumps(launch))
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
                    "output": "collab spawn failed: agent thread limit reached",
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
                "started_at_ms": (epoch - 0.295) * 1000,
                "completed_at_ms": (epoch - 0.29) * 1000,
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
            "payload": {"type": "task_started", "turn_id": "released-turn", "started_at": epoch - 3},
        },
        {
            "timestamp": stamp(-0.5),
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "released-turn",
                "started_at": epoch - 3,
                "completed_at": epoch - 0.5,
                "last_agent_message": "Slot finished.",
            },
        },
    ]
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
        ancestor[3]["payload"]["completed_at_ms"] = (epoch + 1) * 1000
    elif damage == "timed-out":
        parent[-2]["payload"]["output"] = '{"message": "Wait timed out.", "timed_out": true}'
    elif damage == "reused-release":
        repeated = json.loads(json.dumps(refusal))
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
                        "output": "collab spawn failed: agent thread limit reached",
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
    if damage == "none":
        summary = prepare.assemble(run, home)
        assert summary["capacity_limited"] is True
        assert summary["actual_mode"] == "parallel"
        manifest = json.loads((run / "specialist-manifest.json").read_bytes())
        assert {p["role"] for p in manifest["passes"]} == set(children)
    else:
        with pytest.raises(SystemExit, match="review-inspection-capacity-refusal-no-state-change"):
            prepare.assemble(run, home)
        assert not (run / "specialist-manifest.json").exists()
    assert all(path.read_bytes() == content for path, content in protected.items())
