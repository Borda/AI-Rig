"""Exercise native direct review without prescribed source transport or scheduling."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from test_review_prepare import HELPER, _review_inputs, _write_jsonl


@pytest.mark.integration
def test_default_review_prepares_one_pointer_task_per_role(tmp_path: Path) -> None:
    """Large source must not multiply a routed role into paged serial waves."""
    run = _review_inputs(tmp_path, large_source=True)
    result = _direct_prepare(run)
    assert result.returncode == 0, result.stderr
    dispatch = json.loads((run / "dispatch.json").read_text(encoding="utf-8"))
    assert dispatch["mode"] == "direct-review"
    routing = json.loads((run / "review-routing.json").read_text(encoding="utf-8"))
    assert {call["role"] for call in dispatch["calls"]} == set(routing["triggered_roles"])
    assert len(dispatch["calls"]) == len(routing["triggered_roles"])
    assert "waves" not in dispatch
    assert not (run / "inspection-plan.json").exists()
    for call in dispatch["calls"]:
        message = call["arguments"]["message"]
        assert "Reviewed:" in message
        assert "role-cards" in message
        assert "diff.patch" in message
        assert "unchanged =" not in message
        assert "review_context.py" not in message


def _direct_prepare(run: Path) -> subprocess.CompletedProcess[str]:
    """Invoke the current default independently from historical paged test helpers."""
    receipt = json.loads((run / "local-source/review-worktree.json").read_text(encoding="utf-8"))
    return subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "prepare",
            "--out",
            str(run),
            "--run-id",
            "direct-test",
            "--parent-thread-id",
            "parent",
            "--source-root",
            receipt["review_worktree"],
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def _direct_evidence(
    tmp_path: Path, *, source_name: str = "widget.py", quoted_coverage: bool = False
) -> tuple[Path, Path]:
    """Exercise real isolated source reads and retain representative native lifecycle records."""
    run = _review_inputs(tmp_path, source_name=source_name)
    prepared = _direct_prepare(run)
    assert prepared.returncode == 0, prepared.stderr
    dispatch = json.loads((run / "dispatch.json").read_text(encoding="utf-8"))
    plan = json.loads((run / "direct-plan.json").read_text(encoding="utf-8"))
    home = tmp_path / "codex-home"
    sessions = home / "sessions"
    sessions.mkdir(parents=True)
    parent_model = "test-parent-model"
    parent = [
        {"type": "session_meta", "payload": {"id": "parent", "agent_path": "/root"}},
        {"type": "turn_context", "payload": {"turn_id": "parent-turn", "model": parent_model}},
    ]
    for index, call in enumerate(dispatch["calls"]):
        arguments = call["arguments"]
        agent_path = f"/root/{arguments['task_name']}"
        role = call["role"]
        context = next(item for item in plan["contexts"] if item["role_id"] == role)
        root = Path(plan["source_root"])
        read = subprocess.run(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; print(Path(sys.argv[1]).read_text())",
                source_name,
            ],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
        assert "value = 2" in read.stdout
        final = (
            "No finding.\n\n## Source Coverage\n"
            + "\n".join(
                f"Reviewed: {json.dumps(path) if quoted_coverage else path}" for path in context["source_paths"]
            )
            + "\n\n## Reviewer Assessment\nSource rating version: 1\nRating: 1\nVerification readiness: complete\nRationale: Inspected assigned source and comparison."
        )
        call_id = f"spawn-{index}"
        parent.extend(
            [
                {
                    "type": "response_item",
                    "timestamp": "2026-01-01T10:00:00.000Z",
                    "payload": {
                        "type": "function_call",
                        "name": "collaboration.spawn_agent",
                        "call_id": call_id,
                        "arguments": json.dumps(arguments),
                    },
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-01-01T10:00:00.100Z",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps({"task_name": agent_path}),
                    },
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-01-01T10:00:20.000Z",
                    "payload": {
                        "type": "agent_message",
                        "author": agent_path,
                        "recipient": "/root",
                        "content": [
                            {
                                "type": "input_text",
                                "text": f"Message Type: FINAL_ANSWER\nTask name: /root\nSender: {agent_path}\nPayload:\n{final}",
                            }
                        ],
                    },
                },
            ]
        )
        _write_jsonl(
            sessions / f"rollout-child-{index}.jsonl",
            [
                {
                    "type": "session_meta",
                    "payload": {
                        "id": f"child-{index}",
                        "timestamp": "2026-01-01T10:00:00.050Z",
                        "parent_thread_id": "parent",
                        "agent_path": agent_path,
                        "agent_role": "default",
                        "source": {
                            "subagent": {"thread_spawn": {"parent_thread_id": "parent", "agent_path": agent_path}}
                        },
                    },
                },
                {
                    "type": "turn_context",
                    "payload": {
                        "turn_id": f"turn-{index}",
                        "model": arguments.get("model", parent_model),
                        "effort": arguments["reasoning_effort"],
                        "sandbox_policy": {"type": "workspace-write"},
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "exec_command",
                        "arguments": json.dumps({"cmd": "read assigned source", "workdir": str(root)}),
                    },
                },
                {"type": "response_item", "payload": {"type": "function_call_output", "output": read.stdout}},
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": f"turn-{index}",
                        "started_at": 1767261601 + index,
                        "completed_at": 1767261610 + index,
                        "last_agent_message": final,
                    },
                },
            ],
        )
    _write_jsonl(sessions / "rollout-parent.jsonl", parent)
    (run / "specialist-assessments.json").write_text(
        json.dumps({call["role"]: {"confidence": 0.95, "blocking_findings": 0} for call in dispatch["calls"]}),
        encoding="utf-8",
    )
    return run, home


def _assemble_direct(run: Path, home: Path) -> subprocess.CompletedProcess[str]:
    """Ask the installed-path CLI to bind ordinary native child evidence."""
    return subprocess.run(
        [sys.executable, str(HELPER), "assemble", "--out", str(run), "--codex-home", str(home)],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.integration
def test_direct_assembly_accepts_ordinary_reads_and_is_idempotent(tmp_path: Path) -> None:
    """Native children may use ordinary tools while completion remains bound to real joins."""
    run, home = _direct_evidence(tmp_path)
    first = _assemble_direct(run, home)
    assert first.returncode == 0, first.stderr
    summary = json.loads(first.stdout)
    assert summary["source_consumption"] == "reviewer-reported"
    assert summary["actual_mode"] == "parallel"
    assert all(value["sandbox_mode"] == "workspace-write" for value in summary["observed_controls"].values())
    assert _assemble_direct(run, home).returncode == 0
    manifest = json.loads((run / "specialist-manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 10


@pytest.mark.parametrize(
    "damage", ["join", "recipient", "coverage", "path-suffix", "assessment", "parent", "source", "model"]
)
@pytest.mark.integration
def test_direct_assembly_rejects_unverified_evidence(tmp_path: Path, damage: str) -> None:
    """Independent lifecycle or source defects cannot become accepted specialist coverage."""
    run, home = _direct_evidence(tmp_path)
    parent_path = home / "sessions/rollout-parent.jsonl"
    parent = [json.loads(line) for line in parent_path.read_text(encoding="utf-8").splitlines()]
    child_path = home / "sessions/rollout-child-0.jsonl"
    child = [json.loads(line) for line in child_path.read_text(encoding="utf-8").splitlines()]
    if damage == "join":
        parent = [row for row in parent if row.get("payload", {}).get("type") != "agent_message"]
    elif damage == "recipient":
        for row in parent:
            if row.get("payload", {}).get("type") == "agent_message":
                row["payload"]["recipient"] = "/root/wrong"
    elif damage == "coverage":
        child[-1]["payload"]["last_agent_message"] = child[-1]["payload"]["last_agent_message"].replace(
            "Reviewed:", "Skipped:"
        )
    elif damage == "path-suffix":
        child[-1]["payload"]["last_agent_message"] = child[-1]["payload"]["last_agent_message"].replace(
            "Reviewed: widget.py", "Reviewed: widget.py unassigned \t"
        )
    elif damage == "assessment":
        child[-1]["payload"]["last_agent_message"] = child[-1]["payload"]["last_agent_message"].replace(
            "Verification readiness: complete\n", ""
        )
    elif damage == "parent":
        child[0]["payload"]["source"]["subagent"]["thread_spawn"]["parent_thread_id"] = "other"
    elif damage == "model":
        child[1]["payload"]["model"] = "unexpected-model"
    else:
        plan = json.loads((run / "direct-plan.json").read_text(encoding="utf-8"))
        (Path(plan["source_root"]) / "widget.py").write_text("value = 999\n", encoding="utf-8")
    if damage in {"coverage", "path-suffix", "assessment"}:
        final = child[-1]["payload"]["last_agent_message"]
        for row in parent:
            payload = row.get("payload", {})
            if payload.get("author") == child[0]["payload"]["agent_path"]:
                payload["content"][0]["text"] = (
                    payload["content"][0]["text"].split("Payload:\n", 1)[0] + "Payload:\n" + final
                )
    _write_jsonl(parent_path, parent)
    _write_jsonl(child_path, child)
    result = _assemble_direct(run, home)
    assert result.returncode != 0
    assert not (run / "specialist-manifest.json").exists()


@pytest.mark.parametrize("repeated_model", ["test-parent-model", "conflicting-model"])
@pytest.mark.integration
def test_direct_assembly_validates_repeated_parent_context(tmp_path: Path, repeated_model: str) -> None:
    """Compaction may repeat a turn context, but its inherited model must stay consistent."""
    run, home = _direct_evidence(tmp_path)
    parent_path = home / "sessions/rollout-parent.jsonl"
    parent = [json.loads(line) for line in parent_path.read_text(encoding="utf-8").splitlines()]
    parent.append({"type": "turn_context", "payload": {"turn_id": "parent-turn", "model": repeated_model}})
    _write_jsonl(parent_path, parent)

    result = _assemble_direct(run, home)

    if repeated_model == "test-parent-model":
        assert result.returncode == 0, result.stderr
        assert json.loads((run / "specialist-manifest.json").read_text(encoding="utf-8"))["schema_version"] == 10
    else:
        assert result.returncode != 0
        assert "review-parent-model-context-ambiguous" in result.stderr
        assert not (run / "specialist-manifest.json").exists()


def _retry_direct(run: Path, home: Path, role: str) -> subprocess.CompletedProcess[str]:
    """Request the bounded replacement of a recorded child failure."""
    return subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "prepare-direct-retry",
            "--out",
            str(run),
            "--codex-home",
            str(home),
            "--role",
            role,
            "--reason",
            "Original turn aborted; retry after the interruption cleared.",
        ],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.integration
def test_direct_retry_cannot_replace_completed_review(tmp_path: Path) -> None:
    """A clean or inconvenient completed result is never a genuine retry failure."""
    run, home = _direct_evidence(tmp_path)
    role = json.loads((run / "dispatch.json").read_text(encoding="utf-8"))["calls"][0]["role"]
    result = _retry_direct(run, home, role)
    assert result.returncode != 0
    assert "completed-result-replacement-forbidden" in result.stderr
    assert not (run / f"retry/{role}.json").exists()


def _complete_direct_retry(home: Path, child: list[dict], retry: dict, final: str) -> None:
    """Record the same native replacement lifecycle for aborted and source-incomplete tasks."""
    parent_path = home / "sessions/rollout-parent.jsonl"
    parent = [json.loads(line) for line in parent_path.read_text(encoding="utf-8").splitlines()]
    arguments = retry["arguments"]
    new_path = f"/root/{arguments['task_name']}"
    new_child = json.loads(json.dumps(child))
    new_child[0]["payload"].update(id="retry-child", timestamp="2026-01-01T10:01:00.050Z", agent_path=new_path)
    new_child[0]["payload"]["source"]["subagent"]["thread_spawn"]["agent_path"] = new_path
    new_child[1]["payload"]["turn_id"] = "retry-turn"
    new_child[-1]["payload"] = {
        "type": "task_complete",
        "turn_id": "retry-turn",
        "started_at": 1767261661,
        "completed_at": 1767261670,
        "last_agent_message": final,
    }
    _write_jsonl(home / "sessions/rollout-retry-child.jsonl", new_child)
    parent.extend(
        [
            {
                "type": "response_item",
                "timestamp": "2026-01-01T10:01:00.000Z",
                "payload": {
                    "type": "function_call",
                    "name": "spawn_agent",
                    "call_id": "retry-spawn",
                    "arguments": json.dumps(arguments),
                },
            },
            {
                "type": "response_item",
                "timestamp": "2026-01-01T10:01:00.100Z",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "retry-spawn",
                    "output": json.dumps({"task_name": new_path}),
                },
            },
            {
                "type": "response_item",
                "timestamp": "2026-01-01T10:01:20.000Z",
                "payload": {
                    "type": "agent_message",
                    "author": new_path,
                    "recipient": "/root",
                    "content": [
                        {
                            "type": "input_text",
                            "text": f"Message Type: FINAL_ANSWER\nTask name: /root\nSender: {new_path}\nPayload:\n{final}",
                        }
                    ],
                },
            },
        ]
    )
    _write_jsonl(parent_path, parent)


@pytest.mark.integration
def test_direct_retry_retains_aborted_original_and_reuses_completed_roles(tmp_path: Path) -> None:
    """Resume one genuinely aborted role without respawning successful role reviews."""
    run, home = _direct_evidence(tmp_path)
    original = home / "sessions/rollout-child-0.jsonl"
    child = [json.loads(line) for line in original.read_text(encoding="utf-8").splitlines()]
    original_path = child[0]["payload"]["agent_path"]
    final = child[-1]["payload"]["last_agent_message"]
    child[-1]["payload"] = {"type": "turn_aborted", "turn_id": "turn-0"}
    _write_jsonl(original, child)
    parent_path = home / "sessions/rollout-parent.jsonl"
    parent = [json.loads(line) for line in parent_path.read_text(encoding="utf-8").splitlines()]
    parent = [row for row in parent if row.get("payload", {}).get("author") != original_path]
    _write_jsonl(parent_path, parent)
    failed_assembly = _assemble_direct(run, home)
    assert failed_assembly.returncode != 0
    assert not (run / "specialist-manifest.json").exists()
    role = json.loads((run / "dispatch.json").read_text(encoding="utf-8"))["calls"][0]["role"]
    retry_result = _retry_direct(run, home, role)
    assert retry_result.returncode == 0, retry_result.stderr
    retry = json.loads(retry_result.stdout)
    _complete_direct_retry(home, child, retry, final)
    result = _assemble_direct(run, home)
    assert result.returncode == 0, result.stderr
    manifest = json.loads((run / "specialist-manifest.json").read_text(encoding="utf-8"))
    retried = next(item for item in manifest["passes"] if item["role"] == role)
    assert retried["selected_attempt"] == 2
    assert (run / retry["failure_path"]).exists()
    assert all(item["selected_attempt"] == 1 for item in manifest["passes"] if item["role"] != role)
    assert _assemble_direct(run, home).returncode == 0


@pytest.mark.integration
def test_direct_promotion_rechecks_source_after_assembly(tmp_path: Path) -> None:
    """A valid manifest cannot approve a checkout changed after child results were assembled."""
    run, home = _direct_evidence(tmp_path)
    assembled = _assemble_direct(run, home)
    assert assembled.returncode == 0, assembled.stderr
    plan = json.loads((run / "direct-plan.json").read_text(encoding="utf-8"))
    (Path(plan["source_root"]) / "widget.py").write_text("value = 999\n", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(HELPER.with_name("validate_artifacts.py")),
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
    assert result.returncode != 0
    assert "review-direct-source-invalid" in result.stderr


@pytest.mark.parametrize(
    "delivery", ["opaque", "followup", "wrong-first-token", "replayed-token", "mismatched-token", "altered-plaintext"]
)
@pytest.mark.integration
def test_direct_delivery_binds_opaque_host_message(tmp_path: Path, delivery: str) -> None:
    """Encrypted delivery must bind parent and child while plaintext remains exact."""
    run, home = _direct_evidence(tmp_path)
    parent_path = home / "sessions/rollout-parent.jsonl"
    parent = [json.loads(line) for line in parent_path.read_text(encoding="utf-8").splitlines()]
    child_path = home / "sessions/rollout-child-0.jsonl"
    child = [json.loads(line) for line in child_path.read_text(encoding="utf-8").splitlines()]
    call = next(row["payload"] for row in parent if row.get("payload", {}).get("call_id") == "spawn-0")
    arguments = json.loads(call["arguments"])
    arguments["message"] = "gAAAA-retained-host-opaque-message" if delivery != "altered-plaintext" else "Changed task"
    call["arguments"] = json.dumps(arguments)
    if delivery != "altered-plaintext":
        child.insert(
            2,
            {
                "type": "response_item",
                "payload": {
                    "type": "agent_message",
                    "author": "/root",
                    "recipient": child[0]["payload"]["agent_path"],
                    "content": [
                        {
                            "type": "encrypted_content",
                            "encrypted_content": arguments["message"]
                            if delivery in {"opaque", "followup", "replayed-token"}
                            else "gAAAA-other-child-message",
                        }
                    ],
                },
            },
        )
    if delivery in {"followup", "wrong-first-token", "replayed-token"}:
        followup = json.loads(json.dumps(child[2]))
        followup["payload"]["content"][0]["encrypted_content"] = (
            "gAAAA-later-progress-reminder" if delivery == "followup" else arguments["message"]
        )
        child.insert(-1, followup)
    _write_jsonl(parent_path, parent)
    _write_jsonl(child_path, child)
    result = _assemble_direct(run, home)
    if delivery in {"opaque", "followup"}:
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode != 0
        assert not (run / "specialist-manifest.json").exists()


@pytest.mark.parametrize(
    "coverage",
    [
        "## Instructions copied for reference\n```text\nReviewed: widget.py\n```",
        "## Source Coverage\n```text\nReviewed: widget.py\n```",
        "## Source Coverage\nReviewed: widget.py\n## Source Coverage\nReviewed: widget.py",
        "## Source Coverage\nReviewed: widget.py\nMissing: widget.py — unavailable",
        "## Source Coverage\nReviewed: widget.py\n- Reviewed: `widget.py`",
        "## Source Coverage\nReviewed: `other.py`",
        "## Source Coverage\nMissing: widget.py",
        "## Source Coverage\nMissing: widget.py —   ",
    ],
)
@pytest.mark.parametrize("source_name", ["widget.py", "alpha — beta.py"])
@pytest.mark.integration
def test_direct_coverage_requires_unique_unfenced_section_claims(
    tmp_path: Path, coverage: str, source_name: str
) -> None:
    """Examples and contradictory declarations cannot stand in for an actual coverage report."""
    run, home = _direct_evidence(tmp_path, source_name=source_name)
    parent_path = home / "sessions/rollout-parent.jsonl"
    parent = [json.loads(line) for line in parent_path.read_text(encoding="utf-8").splitlines()]
    child_path = home / "sessions/rollout-child-0.jsonl"
    child = [json.loads(line) for line in child_path.read_text(encoding="utf-8").splitlines()]
    final = child[-1]["payload"]["last_agent_message"].replace(
        f"## Source Coverage\nReviewed: {source_name}", coverage.replace("widget.py", source_name)
    )
    child[-1]["payload"]["last_agent_message"] = final
    for row in parent:
        payload = row.get("payload", {})
        if payload.get("author") == child[0]["payload"]["agent_path"]:
            payload["content"][0]["text"] = (
                payload["content"][0]["text"].split("Payload:\n", 1)[0] + "Payload:\n" + final
            )
    _write_jsonl(parent_path, parent)
    _write_jsonl(child_path, child)
    result = _assemble_direct(run, home)
    assert result.returncode != 0
    assert "coverage" in result.stderr or "review-direct-missing-reason-required:" in result.stderr
    assert not (run / "specialist-manifest.json").exists()


def _partial_direct_evidence(
    tmp_path: Path, *, source_name: str = "widget.py", coverage_path: str | None = None
) -> tuple[Path, Path, list[dict], str]:
    """Retain an authentic partial first task alongside completed unaffected roles."""
    run, home = _direct_evidence(tmp_path, source_name=source_name)
    parent_path = home / "sessions/rollout-parent.jsonl"
    parent = [json.loads(line) for line in parent_path.read_text(encoding="utf-8").splitlines()]
    child_path = home / "sessions/rollout-child-0.jsonl"
    child = [json.loads(line) for line in child_path.read_text(encoding="utf-8").splitlines()]
    original = child[-1]["payload"]["last_agent_message"]
    final = original.replace(
        f"Reviewed: {source_name}",
        f"Missing: {coverage_path if coverage_path is not None else source_name} — source inspection was interrupted",
    ).replace("Verification readiness: complete", "Verification readiness: blocked")
    child[-1]["payload"]["last_agent_message"] = final
    for row in parent:
        payload = row.get("payload", {})
        if payload.get("author") == child[0]["payload"]["agent_path"]:
            payload["content"][0]["text"] = (
                payload["content"][0]["text"].split("Payload:\n", 1)[0] + "Payload:\n" + final
            )
    _write_jsonl(parent_path, parent)
    _write_jsonl(child_path, child)
    assembled = _assemble_direct(run, home)
    assert assembled.returncode == 0, assembled.stderr
    return run, home, child, original


@pytest.mark.integration
def test_direct_assembly_retains_honest_missing_source_for_recovery(tmp_path: Path) -> None:
    """Recovery keeps completed reviewer evidence while assessed-result admission rejects missing inspection."""
    run, home, _, _ = _partial_direct_evidence(tmp_path)
    assembled = _assemble_direct(run, home)
    assert assembled.returncode == 0, assembled.stderr
    coverage = json.loads(assembled.stdout)["source_coverage"]
    assert sum(bool(item["missing"]) for item in coverage.values()) == 1
    assert any(item["missing"] == ["widget.py — source inspection was interrupted"] for item in coverage.values())
    retained = (run / "specialist-manifest.json").read_bytes()
    assert _assemble_direct(run, home).returncode == 0
    assert (run / "specialist-manifest.json").read_bytes() == retained


@pytest.mark.parametrize(
    ("source_name", "coverage_path"),
    [
        pytest.param("widget.py", "widget.py", id="ordinary-path"),
        pytest.param("alpha — beta.py", "alpha — beta.py", id="literal-delimiter"),
        pytest.param("alpha — beta.py", "`alpha — beta.py`", id="inline-code-delimiter"),
        pytest.param("alpha — beta.py", json.dumps("alpha — beta.py"), id="json-delimiter"),
        pytest.param("alpha —", "alpha —", id="overlapping-delimiters"),
    ],
)
@pytest.mark.integration
def test_direct_missing_coverage_preserves_assigned_path_and_allows_retry(
    tmp_path: Path, source_name: str, coverage_path: str
) -> None:
    """A filename delimiter belongs to the assigned path before the separate missing-source reason."""
    run, home, _, _ = _partial_direct_evidence(tmp_path, source_name=source_name, coverage_path=coverage_path)
    assembled = _assemble_direct(run, home)
    assert assembled.returncode == 0, assembled.stderr
    coverage = json.loads(assembled.stdout)["source_coverage"]
    role = json.loads((run / "dispatch.json").read_text(encoding="utf-8"))["calls"][0]["role"]
    assert coverage[role] == {
        "reviewed": [],
        "missing": [f"{source_name} — source inspection was interrupted"],
    }
    assert all(
        item["reviewed"] == [source_name] and item["missing"] == [] for key, item in coverage.items() if key != role
    )
    retry = _retry_direct(run, home, role)
    assert retry.returncode == 0, retry.stderr
    assert json.loads(retry.stdout)["role"] == role


@pytest.mark.integration
def test_direct_partial_retry_completes_only_missing_role_and_preserves_original(tmp_path: Path) -> None:
    """Complete one authenticated source gap without discarding original findings or other role evidence."""
    run, home, child, final = _partial_direct_evidence(tmp_path)
    role = json.loads((run / "dispatch.json").read_text(encoding="utf-8"))["calls"][0]["role"]
    original = (run / "specialist-manifest.json").read_bytes()
    original_summary = (run / "inspection-summary.json").read_bytes()
    original_raw = (run / f"specialists/{role}.raw.md").read_bytes()
    retry_result = _retry_direct(run, home, role)
    assert retry_result.returncode == 0, retry_result.stderr
    retry = json.loads(retry_result.stdout)
    assert "preserve every original finding as a reconciliation obligation" in retry["arguments"]["message"]
    assert (run / f"retry/{role}.manifest.json").read_bytes() == original
    assert (run / f"retry/{role}.summary.json").read_bytes() == original_summary
    _complete_direct_retry(home, child, retry, final)
    assembled = _assemble_direct(run, home)
    assert assembled.returncode == 0, assembled.stderr
    assert all(not item["missing"] for item in json.loads(assembled.stdout)["source_coverage"].values())
    current = json.loads((run / "specialist-manifest.json").read_bytes())
    previous = json.loads(original)
    assert next(item for item in current["passes"] if item["role"] == role)["selected_attempt"] == 2
    assert [item for item in current["passes"] if item["role"] != role] == [
        item for item in previous["passes"] if item["role"] != role
    ]
    assert (run / f"specialists/{role}.raw.md").read_bytes() == original_raw
    assert _assemble_direct(run, home).returncode == 0
    assert "retry-already-completed" in _retry_direct(run, home, role).stderr


@pytest.mark.parametrize("damage", ["source", "promoted", "forged-coverage"])
@pytest.mark.integration
def test_direct_partial_retry_rejects_changed_or_ineligible_origin(tmp_path: Path, damage: str) -> None:
    """Source changes, promoted results, and fabricated gap text cannot authorize replacement."""
    run, home, _, _ = _partial_direct_evidence(tmp_path)
    role = json.loads((run / "dispatch.json").read_text(encoding="utf-8"))["calls"][0]["role"]
    if damage == "source":
        plan = json.loads((run / "direct-plan.json").read_text(encoding="utf-8"))
        (Path(plan["source_root"]) / "widget.py").write_text("value = 999\n", encoding="utf-8")
    elif damage == "promoted":
        (run / "result.json").write_text("{}\n", encoding="utf-8")
    else:
        report = run / f"specialists/{role}.md"
        report.write_text(
            report.read_text(encoding="utf-8") + "\nMissing: invented.py — unavailable\n", encoding="utf-8"
        )
    rejected = _retry_direct(run, home, role)
    assert rejected.returncode != 0
    assert not (run / f"retry/{role}.json").exists()


@pytest.mark.integration
def test_direct_partial_retry_does_not_refresh_for_another_partial_result(tmp_path: Path) -> None:
    """An incomplete replacement retains failure evidence without replacing the original derived artifacts."""
    run, home, child, _ = _partial_direct_evidence(tmp_path)
    role = json.loads((run / "dispatch.json").read_text(encoding="utf-8"))["calls"][0]["role"]
    original = (run / "specialist-manifest.json").read_bytes()
    original_summary = (run / "inspection-summary.json").read_bytes()
    prepared = _retry_direct(run, home, role)
    assert prepared.returncode == 0, prepared.stderr
    _complete_direct_retry(home, child, json.loads(prepared.stdout), child[-1]["payload"]["last_agent_message"])
    rejected = _assemble_direct(run, home)
    assert rejected.returncode != 0
    assert "partial-retry-source-incomplete" in rejected.stderr
    assert (run / "specialist-manifest.json").read_bytes() == original
    assert (run / "inspection-summary.json").read_bytes() == original_summary


@pytest.mark.integration
def test_direct_assembly_keeps_selected_review_after_completed_followup(tmp_path: Path) -> None:
    """A later completed reminder turn cannot replace or invalidate the original accepted review."""
    run, home = _direct_evidence(tmp_path)
    initial = _assemble_direct(run, home)
    assert initial.returncode == 0, initial.stderr
    manifest = (run / "specialist-manifest.json").read_bytes()
    child_path = home / "sessions/rollout-child-0.jsonl"
    child = [json.loads(line) for line in child_path.read_text(encoding="utf-8").splitlines()]
    later_context = json.loads(json.dumps(child[1]))
    later_context["payload"]["turn_id"] = "later-progress-turn"
    child.extend(
        [
            later_context,
            {
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "later-progress-turn",
                    "started_at": 1767261661,
                    "completed_at": 1767261670,
                    "last_agent_message": "Acknowledged reminder.",
                },
            },
        ]
    )
    _write_jsonl(child_path, child)
    result = _assemble_direct(run, home)
    assert result.returncode == 0, result.stderr
    assert (run / "specialist-manifest.json").read_bytes() == manifest


@pytest.mark.parametrize(
    "declaration",
    [
        pytest.param("Reviewed: `widget.py`", id="inline-code"),
        pytest.param("- Reviewed: widget.py", id="dash-bullet"),
        pytest.param("* Reviewed: `widget.py`", id="star-bullet"),
        pytest.param("+ Reviewed: `widget.py`", id="plus-bullet"),
        pytest.param("Reviewed: widget.py  ", id="hard-break"),
        pytest.param("Reviewed: `widget.py`  ", id="quoted-hard-break"),
        pytest.param("- Reviewed: `widget.py`\t", id="tab-padding"),
        pytest.param("Reviewed:   widget.py ", id="colon-padding"),
    ],
)
@pytest.mark.integration
def test_direct_coverage_accepts_equivalent_markdown_paths(tmp_path: Path, declaration: str) -> None:
    """Markdown padding, including hard breaks in real reviewer output, preserves exact assigned coverage."""
    run, home = _direct_evidence(tmp_path)
    parent_path = home / "sessions/rollout-parent.jsonl"
    parent = [json.loads(line) for line in parent_path.read_text(encoding="utf-8").splitlines()]
    child_path = home / "sessions/rollout-child-0.jsonl"
    child = [json.loads(line) for line in child_path.read_text(encoding="utf-8").splitlines()]
    final = child[-1]["payload"]["last_agent_message"].replace("Reviewed: widget.py", declaration)
    child[-1]["payload"]["last_agent_message"] = final
    for row in parent:
        payload = row.get("payload", {})
        if payload.get("author") == child[0]["payload"]["agent_path"]:
            payload["content"][0]["text"] = (
                payload["content"][0]["text"].split("Payload:\n", 1)[0] + "Payload:\n" + final
            )
    _write_jsonl(parent_path, parent)
    _write_jsonl(child_path, child)
    result = _assemble_direct(run, home)
    assert result.returncode == 0, result.stderr
    manifest = json.loads((run / "specialist-manifest.json").read_text(encoding="utf-8"))
    selected = next(item for item in manifest["passes"] if item["attempts"][0]["agent_thread_id"] == "child-0")
    assert (run / selected["attempts"][0]["raw_output_path"]).read_text(encoding="utf-8") == final


@pytest.mark.parametrize(
    "source_name",
    [
        pytest.param("`widget`.py", id="literal-backticks"),
        pytest.param(" widget.py", id="leading-space"),
        pytest.param("widget file.py", id="interior-space"),
    ],
)
@pytest.mark.integration
def test_direct_coverage_preserves_literal_filenames(tmp_path: Path, source_name: str) -> None:
    """Exact assigned filenames win before Markdown whitespace and backtick normalization."""
    run, home = _direct_evidence(tmp_path, source_name=source_name)
    result = _assemble_direct(run, home)
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert all(value["reviewed"] == [source_name] for value in summary["source_coverage"].values())


def _supports_control_names() -> bool:
    """Probe whether this filesystem can create the control-character fixture."""
    with tempfile.TemporaryDirectory() as directory:
        try:
            (Path(directory) / "a\nb\tc").write_bytes(b"x")
        except OSError:
            return False
    return True


@pytest.mark.skipif(not _supports_control_names(), reason="filesystem rejects control-character names")
@pytest.mark.integration
def test_direct_coverage_accepts_json_quoted_control_character_filename(tmp_path: Path) -> None:
    """Retain exact control-character source names through real dispatch and assembly."""
    name = "a\nb\tc.py"
    run, home = _direct_evidence(tmp_path, source_name=name, quoted_coverage=True)
    result = _assemble_direct(run, home)
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert all(value["reviewed"] == [name] for value in summary["source_coverage"].values())


@pytest.mark.integration
@pytest.mark.parametrize("owner_evidence", ["complete", "missing-spawn", "missing-join"])
@pytest.mark.parametrize("forkless_subagent", [False, True])
def test_direct_assembly_binds_only_nested_owner_history(
    tmp_path: Path, owner_evidence: str, forkless_subagent: bool
) -> None:
    """Inherited duplicate receipts cannot invalidate or supply a nested parent's native review evidence."""
    run, home = _direct_evidence(tmp_path)
    rollout = home / "sessions/rollout-parent.jsonl"
    rows = [json.loads(line) for line in rollout.read_text(encoding="utf-8").splitlines()]
    owner = {
        **rows[0]["payload"],
        "multi_agent_version": "v2",
        "thread_source": "subagent",
        "history_mode": "paginated",
        "subagent_history_start_ordinal": len(rows) + 1,
        "parent_thread_id": "ancestor",
        "forked_from_id": "ancestor",
        "source": {"subagent": {"thread_spawn": {"parent_thread_id": "ancestor"}}},
    }
    own = rows[1:]
    if owner_evidence == "missing-spawn":
        own = [row for row in own if row.get("payload", {}).get("name", "").rsplit(".", 1)[-1] != "spawn_agent"]
    elif owner_evidence == "missing-join":
        own = [row for row in own if row.get("payload", {}).get("type") != "agent_message"]
    nested = [
        {"type": "session_meta", "payload": owner},
        {"type": "session_meta", "payload": {"id": "ancestor"}},
        *rows[1:],
        *own,
    ]
    if forkless_subagent:
        nested[1]["payload"].update(
            parent_thread_id="execution-parent-not-inherited",
            source={"subagent": {"thread_spawn": {"parent_thread_id": "execution-parent-not-inherited"}}},
        )
    _write_jsonl(rollout, [{**row, "ordinal": index} for index, row in enumerate(nested)])
    result = _assemble_direct(run, home)
    if owner_evidence == "complete":
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["actual_mode"] == "parallel"
    else:
        assert result.returncode != 0
        assert "review-spawn-call-not-unique" in result.stderr or "review-direct-parent-join" in result.stderr
