"""Regression checks for supplied-context-only review inspection evidence."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
VALIDATOR_PATH = PLUGIN_ROOT / "skills" / "code-review" / "validate_artifacts.py"
ROLES = ("qa-specialist", "challenger")


def _module(path: Path) -> ModuleType:
    """Load one test dependency without changing the import path."""
    spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_"), path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _validator() -> ModuleType:
    """Load the shipped validator without changing the test import path."""
    return _module(VALIDATOR_PATH)


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    """Write rollout-shaped evidence with portable newlines."""
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8", newline="\n")


def _inspection_run(
    tmp_path: Path,
    *,
    independent_required: bool = False,
    overlaps: bool = True,
    run_dir: Path | None = None,
    parent_thread_id: str = "parent-thread",
) -> dict[str, object]:
    """Create a schema-five inspection run with real parent/child evidence relationships."""
    run = run_dir or tmp_path / "run"
    sessions = tmp_path / "codex-home" / "sessions"
    specialists = run / "specialists"
    specialists.mkdir(parents=True, exist_ok=True)
    sessions.mkdir(parents=True, exist_ok=True)
    review_input = b"diff --git a/widget.py b/widget.py\n"
    (run / "diff.patch").write_bytes(review_input)
    input_hash = hashlib.sha256(review_input).hexdigest()
    plan = {
        "consumer_policy": {
            "consumer_id": "code-review",
            "capability": "instruction-bounded-review",
            "promotion_status": "promoted",
            "parent_mutations": "serial",
            "canonical_gates": "serial",
        },
        "review_operation": "inspection-only",
        "write_policy": {"parent_writes": "none", "approval_requirement": "not-required"},
        "review_run_id": "inspection-run",
        "parent_thread_id": parent_thread_id,
        "review_input_sha256": input_hash,
        "source_sensitivity": "non-sensitive",
        "contexts": [],
        "independent_review_required": independent_required,
        "independence_requirement_evidence": "User explicitly required independent review."
        if independent_required
        else None,
    }
    plan_path = run / "inspection-plan.json"
    parent_rows: list[dict[str, object]] = [{"type": "session_meta", "payload": {"id": parent_thread_id}}]
    passes: list[dict[str, object]] = []
    for index, role in enumerate(ROLES, start=1):
        card_path = PLUGIN_ROOT / "roles" / role / "ROLE.md"
        card = card_path.read_text(encoding="utf-8")
        fields = _validator()._load_role_card(PLUGIN_ROOT / "roles", role)
        context = f"{card}\nReview only the supplied source."
        context_path = specialists / f"{role}-context.md"
        context_path.write_text(context, encoding="utf-8", newline="\n")
        context_hash = hashlib.sha256(context_path.read_bytes()).hexdigest()
        plan["contexts"].append(
            {
                "role_id": role,
                "context_path": context_path.relative_to(run).as_posix(),
                "context_sha256": context_hash,
            }
        )
        message = (
            f"<!-- codex-review-provenance role={role} run=inspection-run input={input_hash} "
            f"context={context_hash} attempt=1 -->\nNo finding."
        )
        output_path = specialists / f"{role}.md"
        output_path.write_text(message + "\n", encoding="utf-8", newline="\n")
        thread = f"child-{index}"
        agent_path = f"/root/review_{role.replace('-', '_')}_{context_hash[:12]}_a1"
        turn = f"turn-{index}"
        event = f"event-{index}"
        started = 100 + index if overlaps else index * 100
        completed = 110 if overlaps else started + 10
        attempt = {
            "attempt": 1,
            "status": "completed",
            "agent_thread_id": thread,
            "event_id": event,
            "spawn_call_id": f"spawn-{index}",
            "agent_path": agent_path,
            "context_path": context_path.relative_to(run).as_posix(),
            "context_sha256": context_hash,
            "turn_id": turn,
            "model": fields["model"],
            "effort": fields["model_reasoning_effort"],
            "output_path": output_path.relative_to(run).as_posix(),
            "output_sha256": hashlib.sha256(output_path.read_bytes()).hexdigest(),
        }
        passes.append(
            {
                "role": role,
                "axis": "inspection",
                "mode": "inspection",
                "trigger": "bounded source inspection",
                "confidence": 0.95,
                "blocking_findings": 0,
                "output_path": attempt["output_path"],
                "role_card_sha256": fields["role_card_sha256"],
                "attempts": [attempt],
                "selected_attempt": 1,
            }
        )
        parent_rows.extend(
            [
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "spawn_agent",
                        "call_id": attempt["spawn_call_id"],
                        "arguments": json.dumps(
                            {"message": context, "task_name": Path(agent_path).name, "fork_turns": "none"}
                        ),
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "item_completed",
                        "started_at_ms": started * 1000,
                        "completed_at_ms": started * 1000 + 1,
                        "item": {
                            "type": "SubAgentActivity",
                            "id": event,
                            "kind": "started",
                            "agent_path": agent_path,
                            "agent_thread_id": thread,
                        },
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "agent_message",
                        "author": agent_path,
                        "content": [
                            {
                                "type": "input_text",
                                "text": f"Message Type: FINAL_ANSWER\nPayload:\n{message}",
                            }
                        ],
                    },
                },
            ]
        )
        _write_jsonl(
            sessions / f"rollout-{thread}.jsonl",
            [
                {
                    "type": "session_meta",
                    "payload": {
                        "id": thread,
                        "agent_path": agent_path,
                        "agent_role": role,
                        "source": {
                            "subagent": {
                                "thread_spawn": {
                                    "parent_thread_id": parent_thread_id,
                                    "agent_path": agent_path,
                                    "agent_role": role,
                                }
                            }
                        },
                    },
                },
                {
                    "type": "turn_context",
                    "payload": {"turn_id": turn, "model": fields["model"], "effort": fields["model_reasoning_effort"]},
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": turn,
                        "started_at": started,
                        "completed_at": completed,
                        "last_agent_message": message,
                    },
                },
            ],
        )
    plan_path.write_text(json.dumps(plan), encoding="utf-8", newline="\n")
    _write_jsonl(sessions / "rollout-parent-thread.jsonl", parent_rows)
    manifest = {
        "schema_version": 5,
        "review_run_id": "inspection-run",
        "parent_thread_id": parent_thread_id,
        "review_input_sha256": input_hash,
        "passes": passes,
        "inspection_execution": {
            "plan_path": plan_path.name,
            "plan_sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        },
    }
    (run / "specialist-manifest.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
    routing = {
        "schema_version": 1,
        "risk_tier": "HIGH_RISK",
        "mechanical_risk_tier": "TRIVIAL",
        "mechanical_risk_evidence": ["files=0", "changed_lines=0", "unknown_size_rows=0"],
        "signals": {name: False for name in _validator().ROUTING_SIGNALS},
        "signal_evidence": {
            name: ["No additional axis in this supplied-context fixture."] for name in _validator().ROUTING_SIGNALS
        },
        "triggered_roles": sorted(ROLES),
        "trigger_reasons": {role: ["High-risk review axis."] for role in ROLES},
        "independent_review_required": independent_required,
        "independence_requirement_evidence": plan["independence_requirement_evidence"],
    }
    (run / "review-routing.json").write_text(json.dumps(routing), encoding="utf-8", newline="\n")
    _module(PLUGIN_ROOT / "skills/code-review/review_routing.py").synchronize_routing(run)
    return {"run": run, "sessions": sessions.parent, "manifest": manifest, "passes": passes, "plan": plan}


def _rewrite_inspection_plan(fixture: dict[str, object]) -> None:
    """Rebind one mutated frozen plan to its schema-five manifest."""
    plan_path = fixture["run"] / "inspection-plan.json"
    plan_path.write_text(json.dumps(fixture["plan"]), encoding="utf-8", newline="\n")
    fixture["manifest"]["inspection_execution"]["plan_sha256"] = hashlib.sha256(plan_path.read_bytes()).hexdigest()


def test_schema_five_accepts_inspection_without_read_only_controls(tmp_path: Path) -> None:
    """Allow source inspection while reporting absent host controls as unknown."""
    fixture = _inspection_run(tmp_path)
    validator = _validator()

    validator._validate_manifest_preflight(fixture["run"], fixture["sessions"], "parent-thread", fixture["run"])
    summary = validator._validate_review_runtime(
        fixture["run"], fixture["manifest"], fixture["passes"], fixture["sessions"], "parent-thread"
    )

    assert summary["actual_mode"] == "parallel"
    assert summary["evidence_level"] == "instruction-bounded-review"
    assert summary["write_parallel_eligible"] is False
    assert summary["observed_controls"] == {
        role: {"sandbox_mode": "unknown", "approval_policy": "unknown"} for role in ROLES
    }


@pytest.mark.parametrize(
    ("schema", "mode"),
    [pytest.param(3, "inspection", id="no-legacy-inspection"), pytest.param(5, "spawned", id="no-mixed-route")],
)
def test_inspection_cannot_relabel_strict_evidence(tmp_path: Path, schema: int, mode: str) -> None:
    """Keep supplied-context review evidence out of strict runtime acceptance."""
    fixture = _inspection_run(tmp_path)
    fixture["manifest"]["schema_version"] = schema
    fixture["passes"][0]["mode"] = mode
    (fixture["run"] / "specialist-manifest.json").write_text(
        json.dumps(fixture["manifest"]), encoding="utf-8", newline="\n"
    )
    with pytest.raises(SystemExit, match="manifest-mode-schema-mismatch:qa-specialist"):
        _validator()._validate_manifest_preflight(fixture["run"], fixture["sessions"], "parent-thread", fixture["run"])


@pytest.mark.parametrize("tamper", ["tool", "context", "output", "lineage"])
def test_schema_five_rejects_unbound_or_executing_child_evidence(tmp_path: Path, tamper: str) -> None:
    """Reject tool use and every required source/lineage binding before promotion."""
    fixture = _inspection_run(tmp_path)
    validator = _validator()
    run, sessions, passes = fixture["run"], fixture["sessions"], fixture["passes"]
    if tamper == "tool":
        child = sessions / "sessions" / "rollout-child-1.jsonl"
        rows = [json.loads(line) for line in child.read_text(encoding="utf-8").splitlines()]
        rows.append({"type": "response_item", "payload": {"type": "function_call", "name": "exec"}})
        _write_jsonl(child, rows)
        expected = "review-inspection-child-tool-use:qa-specialist"
    elif tamper == "context":
        parent = sessions / "sessions" / "rollout-parent-thread.jsonl"
        rows = [json.loads(line) for line in parent.read_text(encoding="utf-8").splitlines()]
        rows[1]["payload"]["arguments"] = json.dumps({"message": "wrong"})
        _write_jsonl(parent, rows)
        expected = "review-inspection-context-not-sent:qa-specialist"
    elif tamper == "output":
        output = run / passes[0]["attempts"][0]["output_path"]
        output.write_text("changed\n", encoding="utf-8")
        expected = "provenance-output-hash-mismatch:qa-specialist"
    else:
        child = sessions / "sessions" / "rollout-child-1.jsonl"
        rows = [json.loads(line) for line in child.read_text(encoding="utf-8").splitlines()]
        rows[0]["payload"]["source"]["subagent"]["thread_spawn"]["parent_thread_id"] = "other-parent"
        _write_jsonl(child, rows)
        expected = "provenance-child-parent-mismatch:child-1"

    with pytest.raises(SystemExit, match=expected):
        validator._validate_manifest_preflight(run, sessions, "parent-thread", run)


def test_schema_five_reports_serial_fallback_without_independence(tmp_path: Path) -> None:
    """Keep substituted inspection output admissible only as an honest non-independent fallback."""
    fixture = _inspection_run(tmp_path)
    validator = _validator()
    manifest, passes = fixture["manifest"], fixture["passes"]
    for item in passes:
        item.pop("attempts")
        item.pop("selected_attempt")
        item["mode"] = "substituted"
    fixture["plan"]["contexts"] = []
    _rewrite_inspection_plan(fixture)

    summary = validator._validate_review_runtime(fixture["run"], manifest, passes, fixture["sessions"], "parent-thread")

    assert summary["actual_mode"] == "serial-fallback"
    assert summary["independence_satisfied"] is False
    assert summary["independence_required"] is False


def test_schema_five_does_not_label_sequential_children_parallel(tmp_path: Path) -> None:
    """Derive the mode from terminal intervals rather than the dispatch shape."""
    fixture = _inspection_run(tmp_path, overlaps=False)

    summary = _validator()._validate_review_runtime(
        fixture["run"], fixture["manifest"], fixture["passes"], fixture["sessions"], "parent-thread"
    )

    assert summary["actual_mode"] == "independent-spawned"


def test_schema_five_does_not_count_missing_required_role_lineage_as_independent(tmp_path: Path) -> None:
    """Keep a substituted challenger from satisfying explicit required-role coverage."""
    fixture = _inspection_run(tmp_path, independent_required=True)
    fixture["passes"][-1].pop("attempts")
    fixture["passes"][-1].pop("selected_attempt")
    fixture["passes"][-1]["mode"] = "substituted"
    fixture["plan"]["contexts"].pop()
    _rewrite_inspection_plan(fixture)

    summary = _validator()._validate_review_runtime(
        fixture["run"], fixture["manifest"], fixture["passes"], fixture["sessions"], "parent-thread"
    )

    assert summary["independence_required"] is True
    assert summary["independence_satisfied"] is False
    assert summary["actual_mode"] == "serial"


def test_schema_five_keeps_required_independence_when_optional_axis_falls_back(tmp_path: Path) -> None:
    """Do not let a parent-only optional axis erase QA and challenger lineage."""
    fixture = _inspection_run(tmp_path)
    fixture["passes"].append({"role": "doc-scribe", "mode": "substituted"})

    summary = _validator()._validate_review_runtime(
        fixture["run"], fixture["manifest"], fixture["passes"], fixture["sessions"], "parent-thread"
    )

    assert summary["independence_satisfied"] is True


def test_schema_five_accepts_single_local_required_role_lineage(tmp_path: Path) -> None:
    """Treat a lone QA inspection as independent coverage for its local review axis."""
    fixture = _inspection_run(tmp_path)
    fixture["passes"].pop()
    fixture["plan"]["contexts"].pop()
    _rewrite_inspection_plan(fixture)

    summary = _validator()._validate_review_runtime(
        fixture["run"], fixture["manifest"], fixture["passes"], fixture["sessions"], "parent-thread"
    )

    assert summary["actual_mode"] == "serial"
    assert summary["independence_satisfied"] is True


def test_schema_five_empty_parent_only_route_is_not_independent(tmp_path: Path) -> None:
    """Keep a route with no triggered reviewer evidence outside independent coverage."""
    fixture = _inspection_run(tmp_path)
    fixture["passes"].clear()
    fixture["plan"]["contexts"].clear()
    _rewrite_inspection_plan(fixture)

    summary = _validator()._validate_review_runtime(
        fixture["run"], fixture["manifest"], fixture["passes"], fixture["sessions"], "parent-thread"
    )

    assert summary["independence_satisfied"] is False


@pytest.mark.parametrize(
    ("event", "permitted"),
    [
        pytest.param({"type": "task_started"}, True, id="task-lifecycle"),
        pytest.param({"type": "token_count"}, True, id="token-accounting"),
        pytest.param({"type": "item_completed", "item": {"type": "Reasoning"}}, True, id="reasoning-completed"),
        pytest.param({"type": "item_completed", "item": {"type": "AgentMessage"}}, True, id="message-completed"),
        pytest.param(
            {"type": "item_completed", "item": {"type": "ContextCompaction"}}, True, id="compaction-completed"
        ),
        pytest.param({"type": "item_completed", "item": {"type": "CommandExecution"}}, False, id="command-completed"),
        pytest.param({"type": "item_completed", "item": {"type": "FileChange"}}, False, id="file-change-completed"),
        pytest.param({"type": "item_completed", "item": {"type": "UnknownTool"}}, False, id="unknown-item"),
    ],
)
def test_inspection_distinguishes_lifecycle_from_executable_events(
    tmp_path: Path, event: dict[str, object], permitted: bool
) -> None:
    """Accept actual non-executing host bookkeeping while rejecting completed tool activity."""
    fixture = _inspection_run(tmp_path)
    child = fixture["sessions"] / "sessions" / "rollout-child-1.jsonl"
    rows = [json.loads(line) for line in child.read_text(encoding="utf-8").splitlines()]
    rows.insert(1, {"type": "event_msg", "payload": event})
    _write_jsonl(child, rows)
    validator = _validator()
    if permitted:
        validator._validate_manifest_preflight(fixture["run"], fixture["sessions"], "parent-thread", fixture["run"])
    else:
        with pytest.raises(SystemExit, match="review-inspection-child-tool-use:qa-specialist"):
            validator._validate_manifest_preflight(fixture["run"], fixture["sessions"], "parent-thread", fixture["run"])


def test_schema_five_result_accepts_fallback_and_rejects_explicit_independence_shortfall(tmp_path: Path) -> None:
    """Exercise final-result acceptance separately from an explicit independence requirement."""
    completion = _module(Path(__file__).with_name("test_review_completion_gate.py"))
    assessed = completion._assessed_pr.__wrapped__(tmp_path)
    fixture = _inspection_run(tmp_path, run_dir=assessed, parent_thread_id="thread")
    validator = _validator()
    result_path = assessed / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    metadata = result["metadata"]
    routing = json.loads((assessed / "review-routing.json").read_text(encoding="utf-8"))
    metadata.update(
        risk_tier="HIGH_RISK",
        specialist_manifest=str(assessed / "specialist-manifest.json"),
        specialist_passes=fixture["passes"],
        review_run_id="inspection-run",
        review_input_sha256=fixture["manifest"]["review_input_sha256"],
        fanout_substituted=False,
        independence_required=False,
        independence_requirement_evidence=None,
        independence_satisfied=True,
        execution_mode="parallel",
        execution_evidence_level="instruction-bounded-review",
        write_parallel_eligible=False,
        execution_observed_controls={role: {"sandbox_mode": "unknown", "approval_policy": "unknown"} for role in ROLES},
    )
    result_path.write_text(json.dumps(result), encoding="utf-8", newline="\n")

    validator._validate_result(assessed, result_path, fixture["sessions"], "thread", assessed)

    for item in fixture["passes"]:
        item.pop("attempts")
        item.pop("selected_attempt")
        item["mode"] = "substituted"
    fixture["plan"].update(
        independent_review_required=False,
        independence_requirement_evidence=None,
        contexts=[],
    )
    _rewrite_inspection_plan(fixture)
    (assessed / "specialist-manifest.json").write_text(json.dumps(fixture["manifest"]), encoding="utf-8", newline="\n")
    metadata.update(
        specialist_passes=fixture["passes"],
        fanout_substituted=True,
        independence_required=False,
        independence_requirement_evidence=None,
        independence_satisfied=False,
        execution_mode="serial-fallback",
        execution_observed_controls={},
    )
    result_path.write_text(json.dumps(result), encoding="utf-8", newline="\n")

    validator._validate_result(assessed, result_path, fixture["sessions"], "thread", assessed)

    fixture["plan"].update(
        independent_review_required=True,
        independence_requirement_evidence="User explicitly required independent review.",
    )
    _rewrite_inspection_plan(fixture)
    (assessed / "specialist-manifest.json").write_text(json.dumps(fixture["manifest"]), encoding="utf-8", newline="\n")
    routing.update(
        independent_review_required=True,
        independence_requirement_evidence="User explicitly required independent review.",
    )
    (assessed / "review-routing.json").write_text(json.dumps(routing), encoding="utf-8", newline="\n")
    metadata.update(
        specialist_passes=fixture["passes"],
        fanout_substituted=True,
        independence_required=True,
        independence_requirement_evidence="User explicitly required independent review.",
        independence_satisfied=False,
        execution_mode="serial-fallback",
        execution_observed_controls={},
    )
    result_path.write_text(json.dumps(result), encoding="utf-8", newline="\n")

    with pytest.raises(SystemExit, match="independent-review-required-for-pass:challenger,qa-specialist"):
        validator._validate_result(assessed, result_path, fixture["sessions"], "thread", assessed)
