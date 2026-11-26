"""Exercise digest-bound direct task dispatch and historical native receipt replay."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from _artifact_files import read_json, read_jsonl
from test_review_direct import (
    _assemble_direct,
    _complete_direct_retry,
    _deliver_opaque,
    _direct_evidence,
    _direct_prepare,
    _partial_direct_evidence,
    _retry_direct,
)
from test_review_prepare import _write_jsonl


@pytest.fixture
def direct_run(tmp_path: Path) -> tuple[Path, Path]:
    """Prepare a fresh direct review and corresponding native receipt fixtures."""
    return _direct_evidence(tmp_path)


@pytest.mark.integration
def test_dispatch_points_to_each_frozen_task(direct_run: tuple[Path, Path]) -> None:
    """Every routed role receives its own task path and digest in an otherwise short message."""
    run, home = direct_run
    plan = read_json(run / "direct-plan.json")
    dispatch = read_json(run / "dispatch.json")
    calls = {call["role"]: call["arguments"] for call in dispatch["calls"]}
    contexts = {item["role_id"]: item for item in plan["contexts"]}

    assert plan["schema_version"] == 4
    assert calls.keys() == contexts.keys() == set(read_json(run / "review-routing.json")["triggered_roles"])
    assert len(dispatch["calls"]) == len(contexts)
    assert all(_points_to_task(run, role, arguments, contexts[role]) for role, arguments in calls.items())
    result = _assemble_direct(run, home)
    assert result.returncode == 0, result.stderr


def _points_to_task(run: Path, role: str, arguments: dict, context: dict) -> bool:
    """Check one launch against the on-disk task bytes and planned role."""
    task = run.resolve() / f"tasks/{role}.md"
    payload = task.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    message = arguments["message"]
    return (
        json.dumps(task.as_posix(), ensure_ascii=False) in message
        and digest in message
        and context["context_sha256"] == digest
        and arguments["task_name"] == f"review_{role.replace('-', '_')}_{digest[:12]}_a1"
        and arguments["fork_turns"] == "none"
        and len(message.encode()) < len(payload) // 2
        and b"## Source Coverage" in payload
    )


@pytest.mark.integration
@pytest.mark.parametrize("departure", ["wrong-digest", "full-task", "opaque"])
def test_dispatch_rejects_unbound_pointer(direct_run: tuple[Path, Path], departure: str) -> None:
    """Require the exact digest-bearing pointer in the recorded native spawn."""
    run, home = direct_run
    context = read_json(run / "direct-plan.json")["contexts"][0]
    original = read_json(run / "dispatch.json")["calls"][0]["arguments"]["message"]
    token = "opaque-host-transport"  # noqa: S105 - transport label, not a credential
    replacement = {
        "wrong-digest": original.replace(context["context_sha256"], "0" * 64),
        "full-task": (run / context["context_path"]).read_text(encoding="utf-8"),
        "opaque": token,
    }[departure]
    _deliver_opaque(home, replacement, token if departure == "opaque" else None, None)

    result = _assemble_direct(run, home)

    assert result.returncode != 0
    assert f"review-direct-spawn-pointer-mismatch:{context['role_id']}" in result.stderr
    assert not (run / "specialist-manifest.json").exists()


@pytest.mark.integration
def test_dispatch_rejects_changed_frozen_task(direct_run: tuple[Path, Path]) -> None:
    """A correct recorded pointer still fails when its task has changed on disk."""
    run, home = direct_run
    context = read_json(run / "direct-plan.json")["contexts"][0]
    path = run / context["context_path"]
    path.write_bytes(path.read_bytes() + b"\nChanged task text.\n")

    result = _assemble_direct(run, home)

    assert result.returncode != 0
    assert f"review-direct-task-changed:{context['role_id']}" in result.stderr
    assert not (run / "specialist-manifest.json").exists()


@pytest.mark.integration
def test_retry_dispatch_points_to_its_own_frozen_task(direct_run: tuple[Path, Path]) -> None:
    """A partial review's replacement binds the retry task and retains its reconciliation instructions."""
    run, home, child, final = _partial_direct_evidence(direct_run)
    role = read_json(run / "direct-plan.json")["contexts"][0]["role_id"]
    result = _retry_direct(run, home, role)
    assert result.returncode == 0, result.stderr
    retry = json.loads(result.stdout)
    task = run.resolve() / f"tasks/{role}.retry.md"
    body = task.read_bytes()
    message = retry["arguments"]["message"]
    assert json.dumps(task.as_posix(), ensure_ascii=False) in message
    assert hashlib.sha256(body).hexdigest() in message
    assert b"preserve every original finding as a reconciliation obligation" in body
    assert len(message.encode()) < len(body) // 2

    _complete_direct_retry(home, child, retry, final)
    assembled = _assemble_direct(run, home)

    assert assembled.returncode == 0, assembled.stderr


def _historical_direct_evidence(run: Path, home: Path, schema: int) -> None:
    """Encode the issued pre-pointer plans and plaintext launch receipts used by saved runs."""
    plan_path = run / "direct-plan.json"
    dispatch_path = run / "dispatch.json"
    plan = read_json(plan_path)
    dispatch = read_json(dispatch_path)
    plan["schema_version"] = schema
    if schema < 3:
        plan.pop("source_comparison_base")
    if schema == 1:
        plan.pop("model_selection")
    for context, call in zip(plan["contexts"], dispatch["calls"], strict=True):
        role = context["role_id"]
        assert call["role"] == role
        arguments = call["arguments"]
        arguments["message"] = (run / context["context_path"]).read_text(encoding="utf-8")
        if schema == 1:
            arguments["model"] = "test-parent-model"
            card = run / "role-cards" / role / "ROLE.md"
            card_bytes = (
                card.read_text(encoding="utf-8").replace("---\n", "---\nmodel: test-parent-model\n", 1).encode()
            )
            card.write_bytes(card_bytes)
            context["role_card_sha256"] = hashlib.sha256(card_bytes).hexdigest()
    plan_path.write_bytes((json.dumps(plan, indent=2, sort_keys=True) + "\n").encode())
    dispatch["plan_sha256"] = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    dispatch["dispatch_bytes"] = len((json.dumps(dispatch["calls"], indent=2, sort_keys=True) + "\n").encode())
    dispatch_path.write_bytes((json.dumps(dispatch, indent=2, sort_keys=True) + "\n").encode())
    parent = home / "sessions/rollout-parent.jsonl"
    rows = read_jsonl(parent)
    for row in rows:
        payload = row["payload"]
        if payload.get("type") == "function_call":
            index = int(payload["call_id"].removeprefix("spawn-"))
            payload["arguments"] = json.dumps(dispatch["calls"][index]["arguments"])
    _write_jsonl(parent, rows)


@pytest.mark.integration
@pytest.mark.parametrize("schema", [1, 2, 3])
def test_saved_plans_accept_original_message_delivery(direct_run: tuple[Path, Path], schema: int) -> None:
    """Keep the plaintext and opaque receipt behavior of pre-pointer direct runs."""
    run, home = direct_run
    _historical_direct_evidence(run, home, schema)
    plaintext = _assemble_direct(run, home)
    _deliver_opaque(home, "historical-host-delivery", "historical-host-delivery", None)
    opaque = _assemble_direct(run, home)

    assert plaintext.returncode == 0, plaintext.stderr
    assert opaque.returncode == 0, opaque.stderr


@pytest.mark.integration
def test_repreparing_saved_plan_preserves_original_dispatch(direct_run: tuple[Path, Path]) -> None:
    """Regenerating the arguments of an already frozen schema-three run preserves its bytes."""
    run, home = direct_run
    _historical_direct_evidence(run, home, 3)
    before = ((run / "direct-plan.json").read_bytes(), (run / "dispatch.json").read_bytes())

    result = _direct_prepare(run)

    assert result.returncode == 0, result.stderr
    assert ((run / "direct-plan.json").read_bytes(), (run / "dispatch.json").read_bytes()) == before
