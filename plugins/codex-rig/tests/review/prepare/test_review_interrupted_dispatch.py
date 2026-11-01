"""Recover a real interrupted read prefix without inventing a completed reviewer.

Every scenario starts from a real batched review whose challenger reader was interrupted by its parent after reading
only part of the frozen context. The expensive build is shared per module and the tree is restored before each test.
"""

from __future__ import annotations

import copy
import functools
import importlib.util
import json
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest
import test_review_prepare as preparation
from _artifact_files import read_jsonl

_ROLES = {"challenger", "qa-specialist", "sw-engineer"}
_PREFIX_TYPES = {"custom_tool_call", "custom_tool_call_output", "item_completed"}
_REPAIR_DISPATCH = "repair-dispatch.challenger.json"


def _run_helper(*args: object) -> subprocess.CompletedProcess[str]:
    """Run the shipped review preparation helper with the given arguments."""
    return subprocess.run(
        [sys.executable, str(preparation.HELPER), *map(str, args)], capture_output=True, text=True, check=False
    )


def _interrupted_case(
    tmp_path: Path, *, opaque: bool = False, aggregate_only: bool = False
) -> tuple[Path, Path, dict[str, Path], list[dict]]:
    """Retain real reader outputs and a parent interruption before the final page."""
    root = preparation._review_inputs(tmp_path)
    evidence = root / "challenger-evidence.md"
    evidence.write_bytes(evidence.read_bytes() + b"Declared challenger source evidence.\n" * 100)
    source = json.loads((root / "local-source/review-worktree.json").read_bytes())["review_worktree"]
    prepared = _run_helper(
        "prepare",
        "--out",
        root,
        "--run-id",
        "bounded-review",
        "--parent-thread-id",
        "parent",
        "--source-root",
        source,
        "--batches",
    )
    assert prepared.returncode == 0, prepared.stderr
    run = root / "batches/source-001"
    _, home, children = preparation._assembly_evidence(tmp_path, prepared_run=run, compact_reader=True)
    dispatch = json.loads((run / "dispatch.json").read_bytes())
    assert dispatch["calls"][0]["role"] == "challenger"
    child = children["challenger"]
    complete = read_jsonl(child)
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
            "payload": {"type": "task_started", "turn_id": terminal["turn_id"], "started_at": started},
        },
    )
    for row in rows:
        if row["payload"].get("type") in _PREFIX_TYPES:
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
    parent = read_jsonl(parent_path)
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
    sibling = read_jsonl(sibling_path)
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
    parent.sort(key=_row_time)
    preparation._write_jsonl(parent_path, parent)
    return run, home, children, complete


def _row_time(row: dict) -> float:
    """Order rollout rows by timestamp, keeping rows without one first."""
    if "timestamp" not in row:
        return float("-inf")
    return datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")).timestamp()


def _prepare_interrupted(run: Path, home: Path) -> subprocess.CompletedProcess[str]:
    """Exercise the shipped recovery command against retained runtime receipts."""
    return _run_helper(
        "prepare-repair",
        "--out",
        run,
        "--codex-home",
        home,
        "--role",
        "challenger",
        "--kind",
        "interrupted-dispatch",
    )


def _assemble_wave(run: Path, home: Path) -> subprocess.CompletedProcess[str]:
    """Assemble the wave manifest for one prepared batch run."""
    return _run_helper("assemble-wave", "--out", run, "--codex-home", home)


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
    parent = read_jsonl(parent_path)
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


@dataclass(frozen=True)
class _Case:
    """One built interrupted review plus the captured tree every test starts from."""

    base: Path
    run: Path
    home: Path
    children: dict[str, Path]
    complete: list[dict]
    tree: dict
    replacement_path: Path


def _build_case(base: Path, *, opaque: bool, aggregate_only: bool = False, repaired: bool = False) -> _Case:
    """Build an interrupted review; optionally complete the replacement attempt and capture the tree."""
    run, home, children, complete = _interrupted_case(base, opaque=opaque, aggregate_only=aggregate_only)
    prepared = _prepare_interrupted(run, home)
    assert prepared.returncode == 0, prepared.stderr
    if repaired:
        _complete_replacement(run, home, complete, json.loads(prepared.stdout)["arguments"])
    (run / _REPAIR_DISPATCH).unlink()
    tree = preparation._tree_state(base)
    return _Case(base, run, home, children, complete, tree, home / "sessions/rollout-interrupted-replacement.jsonl")


@pytest.fixture(scope="module")
def prefix_base(tmp_path_factory: pytest.TempPathFactory) -> _Case:
    """Build the opaque interrupted review once per module."""
    return _build_case(tmp_path_factory.mktemp("interrupted-prefix"), opaque=True)


@pytest.fixture(scope="module")
def replacement_base(tmp_path_factory: pytest.TempPathFactory) -> _Case:
    """Build an interrupted review with a completed replacement attempt once per module."""
    return _build_case(
        tmp_path_factory.mktemp("interrupted-replacement"), opaque=False, aggregate_only=True, repaired=True
    )


@pytest.fixture
def prefix_case(prefix_base: _Case) -> _Case:
    """Return the shared interrupted review restored to its pristine bytes."""
    preparation._restore_tree(prefix_base.base, prefix_base.tree)
    return prefix_base


@pytest.fixture
def replacement_case(replacement_base: _Case) -> _Case:
    """Return the shared review with replacement evidence restored to its pristine bytes."""
    preparation._restore_tree(replacement_base.base, replacement_base.tree)
    return replacement_base


@pytest.fixture(scope="module")
def validator():
    """Load the shipped artifact validator as a module."""
    spec = importlib.util.spec_from_file_location("interrupted_acceptance", preparation.SKILL / "validate_artifacts.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- full repair flow ----------------------------------------------------------------------------------------------


def _add_opening_commentary(child: Path) -> None:
    """Insert authenticated opening commentary before the first context read of the interrupted reader."""
    rows = read_jsonl(child)
    first_read = next(i for i, row in enumerate(rows) if row["payload"].get("type") == "custom_tool_call")
    started = next(row for row in rows if row["payload"].get("type") == "task_started")["timestamp"]
    text = "Opening the supplied context before reviewing it."
    rows[first_read:first_read] = [
        {
            "type": "event_msg",
            "timestamp": started,
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "AgentMessage",
                    "id": "opening-status",
                    "phase": "commentary",
                    "content": [{"type": "Text", "text": text}],
                },
            },
        },
        {
            "type": "response_item",
            "timestamp": started,
            "payload": {
                "type": "message",
                "id": "opening-status",
                "role": "assistant",
                "phase": "commentary",
                "content": [{"type": "output_text", "text": text}],
            },
        },
    ]
    preparation._write_jsonl(child, rows)


def _keep_rollout(child: Path) -> None:
    """Leave the interrupted reader rollout exactly as recorded."""


@dataclass(frozen=True)
class _FlowSpec:
    """Shape of one end-to-end repair: host delivery form, command receipt form and rollout prelude."""

    opaque: bool
    aggregate_only: bool
    prelude: Callable[[Path], None]


@dataclass(frozen=True)
class _RepairFlow:
    """Everything one prepare, replace, assemble and aggregate run produced."""

    base: Path
    run: Path
    home: Path
    result: dict
    replacement: list[dict]
    manifest: dict
    aggregate: dict
    review_root: Path
    changed_after_prepare: list[Path]
    changed_after_wave: list[Path]
    a1_after_prepare: list[Path]
    a1_after_wave: list[Path]


def _run_repair_flow(base: Path, spec: _FlowSpec) -> _RepairFlow:
    """Prepare the replacement, record its execution, assemble the wave, then aggregate the batches."""
    run, home, children, complete = _interrupted_case(base, opaque=spec.opaque, aggregate_only=spec.aggregate_only)
    spec.prelude(children["challenger"])
    original = {p: p.read_bytes() for p in [*children.values(), run / "inspection-plan.json", run / "dispatch.json"]}
    prepared = _prepare_interrupted(run, home)
    assert prepared.returncode == 0, prepared.stderr
    result = json.loads(prepared.stdout)
    changed_after_prepare = [p for p, data in original.items() if p.read_bytes() != data]
    a1_after_prepare = list((run / "specialists").glob("challenger.a1*"))
    replacement = _complete_replacement(run, home, complete, result["arguments"])
    assembled = _assemble_wave(run, home)
    assert assembled.returncode == 0, assembled.stderr
    manifest = json.loads((run / "specialist-manifest.json").read_bytes())
    changed_after_wave = [p for p, data in original.items() if p.read_bytes() != data]
    a1_after_wave = list((run / "specialists").glob("challenger.a1*"))
    review_root = run.parent.parent
    aggregated = _run_helper("assemble-batches", "--out", review_root, "--codex-home", home)
    assert aggregated.returncode == 0, aggregated.stderr
    aggregate = json.loads((review_root / "specialist-manifest.json").read_bytes())
    return _RepairFlow(
        base,
        run,
        home,
        result,
        replacement,
        manifest,
        aggregate,
        review_root,
        changed_after_prepare,
        changed_after_wave,
        a1_after_prepare,
        a1_after_wave,
    )


def _flow_id(spec: _FlowSpec) -> str:
    """Name one flow shape from its three axes."""
    return "-".join(
        [
            "opaque" if spec.opaque else "plain",
            "aggregate" if spec.aggregate_only else "streams",
            "commentary" if spec.prelude is _add_opening_commentary else "no-commentary",
        ]
    )


_FLOW_SPECS = [
    _FlowSpec(opaque, aggregate_only, prelude)
    for opaque in (False, True)
    for aggregate_only in (False, True)
    for prelude in (_keep_rollout, _add_opening_commentary)
]
_FLOW_CASES = [pytest.param(spec, id=_flow_id(spec)) for spec in _FLOW_SPECS]
#: Shapes whose manifest also carries host-visible output fields an interrupted attempt must never inherit.
_PLAIN_FLOW_CASES = [
    pytest.param(spec, id=_flow_id(spec)) for spec in _FLOW_SPECS if not spec.opaque and not spec.aggregate_only
]


@pytest.fixture(scope="module")
def repair_flow(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> _RepairFlow:
    """Run one full repair flow per distinct spec and share it across the tests that assert on it."""
    return _run_repair_flow(tmp_path_factory.mktemp("repair-flow"), request.param)


@pytest.mark.integration
@pytest.mark.parametrize("repair_flow", _FLOW_CASES, indirect=True)
class TestInterruptedPrefixRepair:
    """A parent interrupt must not force all routed roles into parent substitutes.

    Each case interrupts the challenger reader after a real partial read, prepares an attempt-two replacement, records
    its fresh execution, and checks the assembled manifests and validators accept exactly that history.
    """

    def test_prepares_attempt_two_replacement(self, repair_flow: _RepairFlow) -> None:
        """The repair command issues a fresh attempt-two dispatch and leaves retained evidence untouched.

        The retained interrupted rollout, inspection plan and dispatch must be byte-identical afterwards, and no
        attempt-one specialist artifact may be fabricated.
        """
        result = repair_flow.result
        blocks = re.findall(r"```javascript\n(.*?)\n```", result["arguments"]["message"], re.S)
        assert result["kind"] == "interrupted-dispatch"
        assert result["arguments"]["task_name"].endswith("_a2")
        assert len(blocks) > 1
        assert "--attempt 2" in blocks[0]
        assert repair_flow.changed_after_prepare == []
        assert repair_flow.a1_after_prepare == []

    def test_wave_manifest_records_interrupted_attempt(self, repair_flow: _RepairFlow) -> None:
        """The wave manifest keeps the interrupted attempt without output and selects the completed replacement.

        The sibling role stays on its single original attempt, and assembling must not rewrite retained evidence.
        """
        manifest = repair_flow.manifest
        repaired = next(item for item in manifest["passes"] if item["role"] == "challenger")
        sibling = next(item for item in manifest["passes"] if item["role"] == "qa-specialist")
        first, second = repaired["attempts"]
        assert manifest["schema_version"] == 8
        assert repaired["selected_attempt"] == 2
        assert first["status"] == "interrupted"
        assert "output_path" not in first
        assert "raw_output_path" not in first
        assert second["agent_thread_id"] == repair_flow.replacement[0]["payload"]["id"]
        assert sibling["selected_attempt"] == 1
        assert len(sibling["attempts"]) == 1
        assert repair_flow.changed_after_wave == []
        assert repair_flow.a1_after_wave == []

    def test_aggregate_manifest_declares_batched_source_review(self, repair_flow: _RepairFlow) -> None:
        """Aggregating the batch run yields a batched source-only manifest."""
        aggregate = repair_flow.aggregate
        assert aggregate["schema_version"] == 9
        assert aggregate["review_topology"] == "source-only"
        assert aggregate["manifest_kind"] == "batched-review"

    @pytest.mark.parametrize("retained_role_cards", [False, True])
    def test_aggregate_manifest_validates_every_role(
        self, validator, repair_flow: _RepairFlow, retained_role_cards: bool
    ) -> None:
        """The shipped validator accepts the repaired aggregate for every role with or without retained role cards."""
        by_role = validator._validate_manifest_entries(
            repair_flow.review_root,
            repair_flow.aggregate,
            repair_flow.aggregate["passes"],
            _ROLES,
            repair_flow.home,
            "parent",
            repair_flow.base,
            retained_role_cards=retained_role_cards,
            require_role_card_receipts=True,
        )
        assert set(by_role) == _ROLES

    def test_review_runtime_is_independent_and_parallel(self, validator, repair_flow: _RepairFlow) -> None:
        """Runtime validation still reports independent parallel review after a repaired interruption."""
        runtime = validator._validate_review_runtime(
            repair_flow.review_root, repair_flow.aggregate, repair_flow.aggregate["passes"], repair_flow.home, "parent"
        )
        assert runtime["actual_mode"] == "parallel"
        assert runtime["independence_satisfied"] is True


@pytest.mark.integration
@pytest.mark.parametrize("repair_flow", _PLAIN_FLOW_CASES, indirect=True)
@pytest.mark.parametrize("field", ["output_path", "raw_output_path", "output_sha256", "raw_output_sha256"])
def test_forged_output_on_interrupted_attempt_is_rejected(validator, repair_flow: _RepairFlow, field: str) -> None:
    """An interrupted attempt that claims output copied from its replacement is rejected by the validator.

    Starting from a valid repaired manifest, the interrupted attempt receives one output field of the completed
    replacement; an interrupted turn produced no output, so the validator must refuse the forged history.
    """
    forged = copy.deepcopy(repair_flow.manifest)
    attempts = next(item for item in forged["passes"] if item["role"] == "challenger")["attempts"]
    attempts[0][field] = attempts[1].get(field, "fabricated")
    with pytest.raises(SystemExit, match="review-interrupted-attempt-invalid"):
        validator._validate_manifest_entries(
            repair_flow.run,
            forged,
            forged["passes"],
            _ROLES,
            repair_flow.home,
            "parent",
            repair_flow.base,
            require_role_card_receipts=True,
        )


# --- commentary before the interrupted prefix ----------------------------------------------------------------------


def _identity(message: dict) -> dict:
    """Return the commentary message unchanged as the row payload."""
    return message


def _item_completed(message: dict) -> dict:
    """Wrap the commentary message as a completed native item."""
    return {"type": "item_completed", "item": message}


def _at_task_start(start: dict, first_read: dict) -> str:
    """Timestamp equal to the task start."""
    return start["timestamp"]


def _at_first_read(start: dict, first_read: dict) -> str:
    """Timestamp equal to the first context read."""
    return first_read["timestamp"]


def _before_task_start(start: dict, first_read: dict) -> str:
    """Timestamp one second before the task started."""
    return datetime.fromtimestamp(start["payload"]["started_at"] - 1, timezone.utc).isoformat()


def _inject_commentary(
    child: Path,
    *,
    message_type: str,
    row_type: str,
    wrap: Callable[[dict], dict],
    phase_fields: dict,
    stamp: Callable[[dict, dict], str],
    offset: int,
) -> None:
    """Insert one assistant commentary row relative to the first context read of the interrupted reader."""
    rows = read_jsonl(child)
    first = next(i for i, row in enumerate(rows) if row["payload"].get("type") == "custom_tool_call")
    start = next(row for row in rows if row["payload"].get("type") == "task_started")
    message = {
        "type": message_type,
        "role": "assistant",
        **phase_fields,
        "content": [{"type": "output_text", "text": "Opening the supplied review context."}],
    }
    row = {"type": row_type, "timestamp": stamp(start, rows[first]), "payload": wrap(message)}
    rows.insert(first + offset, row)
    preparation._write_jsonl(child, rows)


_COMMENTARY_SHAPES = [
    pytest.param("message", "response_item", _identity, id="message"),
    pytest.param("AgentMessage", "event_msg", _item_completed, id="item"),
    pytest.param("agent_message", "event_msg", _identity, id="event"),
    pytest.param("agent_message", "response_item", _identity, id="agent-message"),
]
_COMMENTARY = {"phase": "commentary"}


@pytest.mark.integration
@pytest.mark.parametrize(("message_type", "row_type", "wrap"), _COMMENTARY_SHAPES)
class TestInterruptedCommentary:
    """Only authenticated opening commentary may precede an interrupted reader prefix.

    The interrupted challenger rollout gets one extra commentary row in each native shape; recovery must accept it only
    when it is commentary-phase and sits between the task start and the first context read.
    """

    def test_accepts_opening_commentary_before_first_read(
        self, prefix_case: _Case, message_type: str, row_type: str, wrap: Callable[[dict], dict]
    ) -> None:
        """Commentary stamped at task start and placed before the first read still yields a replacement dispatch."""
        _inject_commentary(
            prefix_case.children["challenger"],
            message_type=message_type,
            row_type=row_type,
            wrap=wrap,
            phase_fields=_COMMENTARY,
            stamp=_at_task_start,
            offset=0,
        )
        prepared = _prepare_interrupted(prefix_case.run, prefix_case.home)
        assert prepared.returncode == 0, prepared.stderr
        assert (prefix_case.run / _REPAIR_DISPATCH).is_file()

    @pytest.mark.parametrize(
        ("phase_fields", "stamp", "offset"),
        [
            pytest.param({"phase": "final_answer"}, _at_task_start, 0, id="final-phase"),
            pytest.param({}, _at_task_start, 0, id="missing-phase"),
            pytest.param(_COMMENTARY, _at_task_start, 2, id="after-read"),
            pytest.param(_COMMENTARY, _at_first_read, 0, id="late-timestamp"),
            pytest.param(_COMMENTARY, _before_task_start, 0, id="before-task"),
        ],
    )
    def test_rejects_commentary_outside_pre_read_window(
        self,
        prefix_case: _Case,
        message_type: str,
        row_type: str,
        wrap: Callable[[dict], dict],
        phase_fields: dict,
        stamp: Callable[[dict, dict], str],
        offset: int,
    ) -> None:
        """Final-phase, phase-less, post-read, late or pre-task commentary invalidates the interrupted turn."""
        _inject_commentary(
            prefix_case.children["challenger"],
            message_type=message_type,
            row_type=row_type,
            wrap=wrap,
            phase_fields=phase_fields,
            stamp=stamp,
            offset=offset,
        )
        prepared = _prepare_interrupted(prefix_case.run, prefix_case.home)
        assert prepared.returncode != 0
        assert "review-interrupted-turn-invalid:challenger" in prepared.stderr
        assert not (prefix_case.run / _REPAIR_DISPATCH).exists()


# --- unproved interruption prefixes --------------------------------------------------------------------------------


@dataclass
class _Rollouts:
    """Mutable child and parent rollout rows plus the landmark rows damage functions edit in place."""

    case: _Case
    child: list[dict]
    parent: list[dict]
    abort: dict
    start: dict
    interrupt: dict
    result: dict
    calls: list[dict]

    def write(self) -> None:
        """Persist the edited child and parent rollouts."""
        preparation._write_jsonl(self.case.children["challenger"], self.child)
        preparation._write_jsonl(self.case.home / "sessions/rollout-parent.jsonl", self.parent)


def _load_rollouts(case: _Case) -> _Rollouts:
    """Read the retained rollouts and locate the rows damage functions edit."""
    child = read_jsonl(case.children["challenger"])
    parent = read_jsonl(case.home / "sessions/rollout-parent.jsonl")
    return _Rollouts(
        case,
        child,
        parent,
        abort=next(row for row in child if row["payload"].get("type") == "turn_aborted"),
        start=next(row for row in child if row["payload"].get("type") == "task_started"),
        interrupt=next(row for row in parent if row["payload"].get("name") == "interrupt_agent"),
        result=next(
            row
            for row in parent
            if row["payload"].get("call_id") == "interrupt-reader" and row["payload"]["type"] == "function_call_output"
        ),
        calls=[row for row in child if row["payload"].get("type") == "custom_tool_call"],
    )


def _without_prefix(rows: list[dict]) -> list[dict]:
    """Drop every read call, its receipt and its completed command from the rows."""
    return [row for row in rows if row["payload"].get("type") not in _PREFIX_TYPES]


def _empty_prefix(r: _Rollouts) -> None:
    """Remove every retained read so nothing proves the reader started."""
    r.child = _without_prefix(r.child)


def _full_prefix(r: _Rollouts) -> None:
    """Replace the partial prefix with every page, as if the reader had finished reading."""
    r.child = _without_prefix(r.child)
    full = [copy.deepcopy(row) for row in r.case.complete if row["payload"].get("type") in _PREFIX_TYPES]
    for row in full:
        row["timestamp"] = r.calls[0]["timestamp"]
    r.child[-1:-1] = full


def _wrong_turn(r: _Rollouts) -> None:
    """Abort a different turn than the one that started."""
    r.abort["payload"]["turn_id"] = "different-turn"


def _wrong_parent_target(r: _Rollouts) -> None:
    """Make the parent interrupt a different agent."""
    r.interrupt["payload"]["arguments"] = json.dumps({"target": "/root/unrelated"})


def _parent_was_complete(r: _Rollouts) -> None:
    """Report the interrupted child as already completed in the parent result."""
    r.result["payload"]["output"] = json.dumps({"previous_status": "completed"})


def _missing_parent_result(r: _Rollouts) -> None:
    """Remove the parent's receipt of the interrupt."""
    r.parent.remove(r.result)


def _parent_result_before_abort(r: _Rollouts) -> None:
    """Date the parent's interrupt result at task start, before the abort."""
    r.result["timestamp"] = r.start["timestamp"]


def _interrupt_before_read(r: _Rollouts) -> None:
    """Date the parent's interrupt call at task start, before any read."""
    r.interrupt["timestamp"] = r.start["timestamp"]


def _read_after_abort(r: _Rollouts) -> None:
    """Date the last read after the parent's interrupt result."""
    r.calls[-1]["timestamp"] = r.result["timestamp"]


def _extra_tool(r: _Rollouts) -> None:
    """Add an unrelated tool call to the interrupted turn."""
    r.child.insert(
        -1,
        {
            "type": "response_item",
            "timestamp": r.calls[0]["timestamp"],
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "unrelated-read",
                "arguments": json.dumps({"cmd": "echo unrelated"}),
            },
        },
    )


def _assistant_assessment(r: _Rollouts) -> None:
    """Add a reviewer assessment, which an interrupted reader can never have produced."""
    text = "## Reviewer Assessment\nRating: 1\nRationale: Clean."
    r.child.insert(
        -1,
        {
            "type": "response_item",
            "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]},
        },
    )


def _missing_command(r: _Rollouts) -> None:
    """Remove one completed command so a read has no executed receipt."""
    r.child.remove(next(row for row in r.child if row["payload"].get("item", {}).get("type") == "CommandExecution"))


def _missing_abort(r: _Rollouts) -> None:
    """Remove the abort so the turn never ends."""
    r.child.remove(r.abort)


def _repeated_abort(r: _Rollouts) -> None:
    """Abort the turn twice."""
    r.child.append(copy.deepcopy(r.abort))


def _missing_start(r: _Rollouts) -> None:
    """Remove the task start."""
    r.child.remove(r.start)


def _repeated_start(r: _Rollouts) -> None:
    """Start the task twice."""
    r.child.append(copy.deepcopy(r.start))


def _unselected_context_field(field: str, r: _Rollouts) -> None:
    """Record a model or effort the dispatch never selected."""
    next(row["payload"] for row in r.child if row["type"] == "turn_context")[field] = "unselected"


def _first_receipt(r: _Rollouts) -> dict:
    """Return the first context-read receipt payload."""
    return next(row["payload"] for row in r.child if row["payload"].get("type") == "custom_tool_call_output")


def _wrong_source(r: _Rollouts) -> None:
    """Change the review input hash printed inside the first read receipt."""
    receipt = _first_receipt(r)
    output = receipt["output"][1]["text"]
    plan = json.loads((r.case.run / "inspection-plan.json").read_bytes())
    assert plan["review_input_sha256"] in output
    receipt["output"][1]["text"] = output.replace(plan["review_input_sha256"], "0" * 64)


def _changed_context(r: _Rollouts) -> None:
    """Append text to the first read receipt so it no longer matches the frozen context."""
    _first_receipt(r)["output"][1]["text"] += "\nChanged frozen context."


def _delivery(r: _Rollouts) -> dict:
    """Return the retained delivery row."""
    return next(row for row in r.child if row["payload"].get("type") == "agent_message")


def _missing_delivery(r: _Rollouts) -> None:
    """Remove the host delivery message."""
    r.child.remove(_delivery(r))


def _different_delivery(r: _Rollouts) -> None:
    """Replace the delivered message with one the parent never sent."""
    _delivery(r)["payload"]["content"][0]["encrypted_content"] = "different-opaque-delivery"


def _pending_original(r: _Rollouts) -> None:
    """Leave the sibling reviewer's spawn without any retained child rollout."""
    sibling_path = r.case.children["qa-specialist"]
    sibling = json.loads(sibling_path.read_text(encoding="utf-8").splitlines()[0])["payload"]["agent_path"]
    spawn = next(
        row
        for row in r.parent
        if row["payload"].get("name") == "spawn_agent"
        and json.loads(row["payload"]["arguments"])["task_name"] == sibling.rsplit("/", 1)[1]
    )
    r.parent = [
        row
        for row in r.parent
        if row["payload"].get("call_id") != spawn["payload"]["call_id"] and row["payload"].get("author") != sibling
    ]
    sibling_path.unlink()


@pytest.mark.integration
class TestUnprovedPrefixRejection:
    """Every retained interruption boundary must be proved before replacement arguments are issued.

    Each case damages exactly one fact of the otherwise valid interrupted history; recovery must refuse with a message
    and leave the tree untouched.
    """

    @pytest.mark.parametrize(
        "damage",
        [
            pytest.param(_empty_prefix, id="empty-prefix"),
            pytest.param(_full_prefix, id="full-prefix"),
            pytest.param(_wrong_turn, id="wrong-turn"),
            pytest.param(_wrong_parent_target, id="wrong-parent-target"),
            pytest.param(_parent_was_complete, id="parent-was-complete"),
            pytest.param(_missing_parent_result, id="missing-parent-result"),
            pytest.param(_parent_result_before_abort, id="parent-result-before-abort"),
            pytest.param(_interrupt_before_read, id="interrupt-before-read"),
            pytest.param(_read_after_abort, id="read-after-abort"),
            pytest.param(_extra_tool, id="extra-tool"),
            pytest.param(_assistant_assessment, id="assistant-assessment"),
            pytest.param(_missing_command, id="missing-command"),
            pytest.param(_missing_abort, id="missing-abort"),
            pytest.param(_repeated_abort, id="repeated-abort"),
            pytest.param(_missing_start, id="missing-start"),
            pytest.param(_repeated_start, id="repeated-start"),
            pytest.param(functools.partial(_unselected_context_field, "model"), id="wrong-model"),
            pytest.param(functools.partial(_unselected_context_field, "effort"), id="wrong-effort"),
            pytest.param(_changed_context, id="wrong-context"),
            pytest.param(_wrong_source, id="wrong-source"),
            pytest.param(_missing_delivery, id="missing-delivery"),
            pytest.param(_different_delivery, id="different-delivery"),
            pytest.param(_pending_original, id="pending-original"),
        ],
    )
    def test_rejects_damaged_history(self, prefix_case: _Case, damage: Callable[[_Rollouts], None]) -> None:
        """A single broken interruption fact is refused with a message and writes nothing."""
        rollouts = _load_rollouts(prefix_case)
        damage(rollouts)
        rollouts.write()
        before = preparation._tree_state(prefix_case.base)
        rejected = _prepare_interrupted(prefix_case.run, prefix_case.home)
        assert rejected.returncode != 0
        assert rejected.stderr.strip()
        assert preparation._tree_state(prefix_case.base) == before


# --- replacement admission -----------------------------------------------------------------------------------------


def _original_rollout(case: _Case) -> Path:
    """Path of the interrupted original rollout."""
    return case.children["challenger"]


def _replacement_rollout(case: _Case) -> Path:
    """Path of the replacement rollout."""
    return case.replacement_path


def _command_items(rows: list[dict]) -> list[dict]:
    """Return every completed command item in the rows."""
    return [row["payload"]["item"] for row in rows if row["payload"].get("item", {}).get("type") == "CommandExecution"]


def _missing_page(case: _Case, rows: list[dict]) -> list[dict]:
    """Drop the last page read and its command, as if the replacement never reread it."""
    last = [row["payload"] for row in rows if row["payload"].get("type") == "custom_tool_call"][-1]
    last_command = _command_items(rows)[-1]
    rows = [row for row in rows if row["payload"].get("call_id") != last["call_id"]]
    rows.remove(next(row for row in rows if row["payload"].get("item") is last_command))
    return rows


def _aborted_replacement(case: _Case, rows: list[dict]) -> list[dict]:
    """End the replacement turn as interrupted instead of completed."""
    rows[-1]["payload"].update(type="turn_aborted", reason="interrupted")
    rows[-1]["payload"].pop("last_agent_message")
    return rows


def _third_attempt(case: _Case, rows: list[dict]) -> list[dict]:
    """Add a third attempt of the same role, which the retry budget never allows."""
    third = json.loads(json.dumps(rows).replace("_a2", "_a3").replace("interrupted-replacement", "third-replacement"))
    preparation._write_jsonl(case.home / "sessions/rollout-third-replacement.jsonl", third)
    parent_path = case.home / "sessions/rollout-parent.jsonl"
    parent = read_jsonl(parent_path)
    extra = [copy.deepcopy(row) for row in parent if row["payload"].get("call_id") == "interrupted-repair-spawn"]
    extra = json.loads(
        json.dumps(extra)
        .replace("_a2", "_a3")
        .replace("interrupted-repair-spawn", "third-repair-spawn")
        .replace("10:01:00", "10:02:00")
    )
    preparation._write_jsonl(parent_path, parent + extra)
    return rows


def _unrelated_queue_work(case: _Case, rows: list[dict]) -> list[dict]:
    """Interleave unrelated parent work with the interrupted read."""
    parent_path = case.home / "sessions/rollout-parent.jsonl"
    parent = read_jsonl(parent_path)
    read_at = next(
        row["timestamp"]
        for row in read_jsonl(case.children["challenger"])
        if row["payload"].get("type") == "custom_tool_call"
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
    parent.sort(key=_row_time)
    preparation._write_jsonl(parent_path, parent)
    return rows


def _tampered_output(case: _Case, rows: list[dict]) -> list[dict]:
    """Append text the frozen source never contained to the first command output."""
    _command_items(rows)[0]["aggregated_output"] += "\nUnbound source text."
    return rows


def _mixed_output(case: _Case, rows: list[dict]) -> list[dict]:
    """Give the first command both aggregated and stream output that disagree."""
    _command_items(rows)[0].update(stdout="Conflicting output", stderr="")
    return rows


def _incomplete_command(case: _Case, rows: list[dict]) -> list[dict]:
    """Leave the first command unfinished."""
    _command_items(rows)[0]["status"] = "in_progress"
    return rows


@pytest.mark.integration
class TestReplacementAdmission:
    """Replacement admission rejects incomplete rereads and conflicting native command receipts.

    A fully valid replacement is damaged one way at a time; assembling the wave must fail with a message and publish no
    specialist manifest.
    """

    @pytest.mark.parametrize(
        ("target", "damage"),
        [
            pytest.param(_replacement_rollout, _missing_page, id="missing-page"),
            pytest.param(_replacement_rollout, _aborted_replacement, id="aborted-replacement"),
            pytest.param(_replacement_rollout, _third_attempt, id="third-attempt"),
            pytest.param(_original_rollout, _tampered_output, id="original-tampered-output"),
            pytest.param(_replacement_rollout, _tampered_output, id="replacement-tampered-output"),
            pytest.param(_original_rollout, _mixed_output, id="original-mixed-output"),
            pytest.param(_replacement_rollout, _mixed_output, id="replacement-mixed-output"),
            pytest.param(_replacement_rollout, _incomplete_command, id="incomplete-command"),
            pytest.param(_replacement_rollout, _unrelated_queue_work, id="unrelated-queue-work"),
        ],
    )
    def test_rejects_damaged_evidence(
        self,
        replacement_case: _Case,
        target: Callable[[_Case], Path],
        damage: Callable[[_Case, list[dict]], list[dict]],
    ) -> None:
        """Wave assembly refuses damaged replacement or original receipts and writes no manifest."""
        path = target(replacement_case)
        preparation._write_jsonl(path, damage(replacement_case, read_jsonl(path)))
        assembled = _assemble_wave(replacement_case.run, replacement_case.home)
        assert assembled.returncode != 0
        assert assembled.stderr.strip()
        assert not (replacement_case.run / "specialist-manifest.json").exists()
