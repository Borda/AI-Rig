"""Exercise deterministic review preparation without model-authored provenance."""

from __future__ import annotations

import importlib.util
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[2]
SKILL = PLUGIN_ROOT / "skills/code-review"
HELPER = SKILL / "review_prepare.py"


def _review_inputs(tmp_path: Path) -> Path:
    """Write semantic reviewer decisions while leaving mechanical evidence to the producer."""
    spec = importlib.util.spec_from_file_location("prepare_validator", SKILL / "validate_artifacts.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    run = tmp_path / "review"
    run.mkdir()
    (run / "diff.patch").write_text("diff --git a/widget.py b/widget.py\n", encoding="utf-8")
    roles = ["challenger", "qa-specialist"]
    routing = {
        "schema_version": 1,
        "risk_tier": "HIGH_RISK",
        "signals": dict.fromkeys(module.ROUTING_SIGNALS, False),
        "signal_evidence": {name: ["Scope checked."] for name in module.ROUTING_SIGNALS},
        "triggered_roles": roles,
        "trigger_reasons": {role: ["High-risk behavior."] for role in roles},
    }
    (run / "review-routing.json").write_text(json.dumps(routing), encoding="utf-8")
    briefs = {}
    for role in roles:
        (run / f"{role}-evidence.md").write_text(
            f"Scope: widget.py at frozen revision. Excluded: unrelated callers.\nQuestion: {role} axis.\n"
            + "Relevant source evidence.\n" * 300,
            encoding="utf-8",
        )
        briefs[role] = {"axis": role, "evidence_path": f"{role}-evidence.md"}
    (run / "review-briefs.json").write_text(json.dumps(briefs), encoding="utf-8")
    return run


def _prepare(run: Path) -> subprocess.CompletedProcess[str]:
    """Invoke the shipped producer as an installed-path command."""
    return subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "prepare",
            "--out",
            str(run),
            "--run-id",
            "bounded-review",
            "--parent-thread-id",
            "parent",
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    """Write synthetic native rollout rows with portable line endings."""
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8", newline="\n")


def _assembly_evidence(tmp_path: Path, *, malformed_continuation: bool = False) -> tuple[Path, Path, dict[str, Path]]:
    """Record dispatched calls, real context-reader output, and completed child turns."""
    run = _review_inputs(tmp_path)
    prepared = _prepare(run)
    assert prepared.returncode == 0, prepared.stderr
    plan = json.loads((run / "inspection-plan.json").read_text(encoding="utf-8"))
    dispatch = json.loads((run / "dispatch.json").read_text(encoding="utf-8"))
    if malformed_continuation:
        message = dispatch["calls"][0]["arguments"]["message"]
        dispatch["calls"][0]["arguments"]["message"] = message.replace(" --page N", " --page WRONG")
    home = tmp_path / "codex-home"
    sessions = home / "sessions"
    sessions.mkdir(parents=True)
    parent_rows: list[dict[str, object]] = [{"type": "session_meta", "payload": {"id": "parent"}}]
    children = {}
    for index, (context, call) in enumerate(zip(plan["contexts"], dispatch["calls"], strict=True), start=1):
        role = context["role_id"]
        arguments = call["arguments"]
        agent_path = f"/root/{arguments['task_name']}"
        thread = f"child-{index}"
        turn = f"turn-{index}"
        call_id = f"spawn-{index}"
        message = arguments["message"]
        page_match = re.search(r"Read all (\d+) frozen review context pages", message)
        assert page_match is not None, "compact-review-page-count-invalid"
        page_count = int(page_match.group(1))
        assert page_count > 1
        instruction = (
            f"For pages 2 through {page_count}, copy the same JavaScript source once per page in order. "
            "In each copied source, append ` --page N` to the end of the `cmd` string, replacing N with that page's "
            "actual number. Keep every other byte of the JavaScript source unchanged."
        )
        assert instruction in message, "compact-review-continuation-invalid"
        templates = re.findall(r"```javascript\n(.*?)\n```", message, flags=re.DOTALL)
        assert len(templates) == 1, "compact-review-template-count-invalid"
        first_call = templates[0]
        prefix, command_tail = first_call.split("tools.exec_command(", 1)
        args_json, suffix = command_tail.split("); text(r.output);", 1)
        command_args = json.loads(args_json)
        assert command_args["cmd"].endswith("--attempt 1"), "compact-review-template-command-invalid"
        read_calls = [first_call]
        for page in range(2, page_count + 1):
            paged_args = {**command_args, "cmd": f"{command_args['cmd']} --page {page}"}
            read_calls.append(
                f"{prefix}tools.exec_command({json.dumps(paged_args, ensure_ascii=False)}); text(r.output);{suffix}"
            )
        tool_rows = []
        header = ""
        for page, read_call in enumerate(read_calls, start=1):
            reader = subprocess.run(
                [
                    sys.executable,
                    str(SKILL / "review_context.py"),
                    "--plan",
                    str(run / "inspection-plan.json"),
                    "--role",
                    role,
                    "--attempt",
                    "1",
                    "--page",
                    str(page),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            assert reader.returncode == 0, reader.stderr
            if page == 1:
                header = reader.stdout.splitlines()[0]
            read_id = f"read-{index}-{page}"
            tool_rows.extend(
                [
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "custom_tool_call",
                            "name": "exec",
                            "call_id": read_id,
                            "input": read_call,
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "custom_tool_call_output",
                            "call_id": read_id,
                            "output": [
                                {"type": "input_text", "text": "Script completed\nWall time 0.1 seconds\nOutput:\n"},
                                {"type": "input_text", "text": reader.stdout},
                            ],
                        },
                    },
                ]
            )
        final = (
            f"{header}\nNo finding.\n\n## Reviewer Assessment\n\nRating: 1\nRationale: The inspected scope is clean."
        )
        parent_rows.extend(
            [
                {
                    "timestamp": "2026-01-01T10:00:00.000Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "spawn_agent",
                        "call_id": call_id,
                        "arguments": json.dumps(arguments),
                    },
                },
                {
                    "timestamp": "2026-01-01T10:00:00.100Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps({"task_name": agent_path}),
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "agent_message",
                        "author": agent_path,
                        "content": [{"type": "input_text", "text": f"Message Type: FINAL_ANSWER\nPayload:\n{final}"}],
                    },
                },
            ]
        )
        child = sessions / f"rollout-{thread}.jsonl"
        _write_jsonl(
            child,
            [
                {
                    "type": "session_meta",
                    "payload": {
                        "id": thread,
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
                    "payload": {"turn_id": turn, "model": arguments["model"], "effort": arguments["reasoning_effort"]},
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "agent_message",
                        "content": [{"type": "encrypted_content", "encrypted_content": arguments["message"]}],
                    },
                },
                *tool_rows,
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": turn,
                        "started_at": 100 + index,
                        "completed_at": 110 + index,
                        "last_agent_message": final,
                    },
                },
            ],
        )
        children[role] = child
    _write_jsonl(sessions / "rollout-parent.jsonl", parent_rows)
    (run / "specialist-assessments.json").write_text(
        json.dumps({role: {"confidence": 0.95, "blocking_findings": 0} for role in children}), encoding="utf-8"
    )
    return run, home, children


def _assemble(run: Path, home: Path) -> subprocess.CompletedProcess[str]:
    """Invoke the shipped assembly command against synthetic native sessions."""
    return subprocess.run(
        [sys.executable, str(HELPER), "assemble", "--out", str(run), "--codex-home", str(home)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_prepare_freezes_complete_wave_and_keeps_source_out_of_dispatch(tmp_path: Path) -> None:
    run = _review_inputs(tmp_path)
    result = _prepare(run)
    assert result.returncode == 0, result.stderr
    plan = json.loads((run / "inspection-plan.json").read_text())
    dispatch = json.loads((run / "dispatch.json").read_text())
    assert dispatch["routing_sha256"] == hashlib.sha256((run / "review-routing.json").read_bytes()).hexdigest()
    assert dispatch["briefs_sha256"] == hashlib.sha256((run / "review-briefs.json").read_bytes()).hexdigest()
    assert [entry["role_id"] for entry in plan["contexts"]] == ["challenger", "qa-specialist"]
    assert len(dispatch["calls"]) == 2
    for entry, call in zip(plan["contexts"], dispatch["calls"], strict=True):
        context = (run / entry["context_path"]).read_bytes()
        role = entry["role_id"]
        assert context.startswith((PLUGIN_ROOT / "roles" / role / "ROLE.md").read_bytes())
        assert "Relevant source evidence." not in call["arguments"]["message"]
        assert call["arguments"]["fork_turns"] == "none"
        assert call["arguments"]["agent_type"] == "default"
        assert entry["context_sha256"][:12] in call["arguments"]["task_name"]
        assert len(re.findall(r"```javascript\n", call["arguments"]["message"])) == 1
        assert "append ` --page N`" in call["arguments"]["message"]
    assert dispatch["context_bytes"] > dispatch["dispatch_bytes"]


def test_prepare_rejects_missing_role_before_freezing_any_context(tmp_path: Path) -> None:
    run = _review_inputs(tmp_path)
    briefs = json.loads((run / "review-briefs.json").read_text())
    briefs.pop("challenger")
    (run / "review-briefs.json").write_text(json.dumps(briefs))
    result = _prepare(run)
    assert result.returncode != 0
    assert "review-brief-role-set-mismatch" in result.stderr
    assert not (run / "inspection-plan.json").exists()


def test_prepare_rejects_oversized_context_before_freezing_wave(tmp_path: Path) -> None:
    """Keep a reviewer brief above the native read limit out of the frozen wave."""
    run = _review_inputs(tmp_path)
    (run / "challenger-evidence.md").write_text("bounded evidence\n" * 5000, encoding="utf-8")
    result = _prepare(run)
    assert result.returncode != 0
    assert "review-context-capacity-exceeded:challenger:65536-bytes" in result.stderr
    assert not (run / "inspection-plan.json").exists()
    assert not (run / "specialists").exists()


@pytest.mark.parametrize("problem", ["changed-brief", "sensitive-evidence"])
def test_prepare_never_overwrites_frozen_evidence_or_retains_secrets(tmp_path: Path, problem: str) -> None:
    run = _review_inputs(tmp_path)
    if problem == "changed-brief":
        assert _prepare(run).returncode == 0
        original = (run / "inspection-plan.json").read_bytes()
        (run / "qa-specialist-evidence.md").write_text("Different source.")
        expected = "review-frozen-artifact-conflict"
    else:
        (run / "qa-specialist-evidence.md").write_text("Authorization: Bearer do-not-retain")
        expected = "review-context-sensitive-material"
    result = _prepare(run)
    assert result.returncode != 0
    assert expected in result.stderr
    if problem == "changed-brief":
        assert (run / "inspection-plan.json").read_bytes() == original
    else:
        assert not (run / "specialists").exists()


def test_assemble_binds_native_wave_and_preserves_child_outputs(tmp_path: Path) -> None:
    """Accept a completed, overlapping wave with outputs bound to child finals."""
    run, home, children = _assembly_evidence(tmp_path)
    # Native history also contains non-spawn subagents; unrelated session shapes must not abort this wave.
    _write_jsonl(
        home / "sessions" / "unrelated.jsonl",
        [{"type": "session_meta", "payload": {"id": "unrelated", "source": {"subagent": "compact"}}}],
    )
    result = _assemble(run, home)
    assert result.returncode == 0, result.stderr
    manifest = json.loads((run / "specialist-manifest.json").read_text(encoding="utf-8"))
    summary = json.loads((run / "inspection-summary.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 6
    assert summary["actual_mode"] == "parallel"
    assert {item["role"] for item in manifest["passes"]} == set(children)
    for item in manifest["passes"]:
        role = item["role"]
        rows = [json.loads(line) for line in children[role].read_text(encoding="utf-8").splitlines()]
        final = rows[-1]["payload"]["last_agent_message"]
        assert (run / item["output_path"]).read_text(encoding="utf-8") == final + "\n"
        assert item["attempts"][0]["agent_thread_id"] == rows[0]["payload"]["id"]


def test_assembly_fixture_rejects_malformed_compact_continuation(tmp_path: Path) -> None:
    """Make synthetic child calls depend on the actual compact dispatch instruction."""
    with pytest.raises(AssertionError, match="compact-review-continuation-invalid"):
        _assembly_evidence(tmp_path, malformed_continuation=True)


def test_assemble_rejects_unparseable_rating_before_promoting_manifest(tmp_path: Path) -> None:
    """Catch a malformed model assessment before downstream result writing fails."""
    run, home, children = _assembly_evidence(tmp_path)
    child = children["challenger"]
    rows = [json.loads(line) for line in child.read_text(encoding="utf-8").splitlines()]
    rows[-1]["payload"]["last_agent_message"] = rows[-1]["payload"]["last_agent_message"].replace(
        "Rating: 1", "Rating: 5/5"
    )
    _write_jsonl(child, rows)
    result = _assemble(run, home)
    assert result.returncode != 0
    assert "review-assessment-content-invalid:challenger" in result.stderr
    assert not (run / "specialist-manifest.json").exists()


@pytest.mark.parametrize(
    ("file_name", "expected"),
    [
        pytest.param("review-briefs.json", "review-prepared-briefs-changed", id="changed-axis"),
        pytest.param("review-routing.json", "review-prepared-routing-changed", id="changed-trigger"),
    ],
)
def test_assemble_rejects_changed_preparation_semantics(tmp_path: Path, file_name: str, expected: str) -> None:
    """Do not label frozen child findings with an axis or trigger edited after dispatch."""
    run, home, _ = _assembly_evidence(tmp_path)
    path = run / file_name
    payload = json.loads(path.read_text(encoding="utf-8"))
    if file_name == "review-briefs.json":
        payload["challenger"]["axis"] = "changed axis after dispatch"
    else:
        payload["trigger_reasons"]["challenger"] = ["Changed trigger after dispatch."]
    path.write_text(json.dumps(payload), encoding="utf-8", newline="\n")

    result = _assemble(run, home)
    assert result.returncode != 0
    assert expected in result.stderr
    assert not (run / "specialist-manifest.json").exists()


@pytest.mark.parametrize(
    "problem, expected",
    [
        pytest.param("missing-child", "review-child-session-not-unique", id="missing-child"),
        pytest.param("wrong-receipt", "review-inspection-context-read-output-mismatch", id="mismatched-read-receipt"),
        pytest.param("extra-tool", "review-inspection-context-read-count-mismatch", id="extra-child-tool"),
        pytest.param("wrong-model", "provenance-role-model-policy-mismatch", id="wrong-child-model"),
        pytest.param("wrong-output", "review-inspection-parent-join-missing", id="wrong-joined-output"),
        pytest.param("no-overlap", "review-wave-not-parallel", id="nonoverlapping-wave"),
    ],
)
def test_assemble_rejects_unbound_or_incomplete_wave(tmp_path: Path, problem: str, expected: str) -> None:
    """Reject plausible rollout tampering instead of accepting a fabricated pass."""
    run, home, children = _assembly_evidence(tmp_path)
    child = children["challenger"]
    rows = [json.loads(line) for line in child.read_text(encoding="utf-8").splitlines()]
    parent = home / "sessions" / "rollout-parent.jsonl"
    parent_rows = [json.loads(line) for line in parent.read_text(encoding="utf-8").splitlines()]
    if problem == "missing-child":
        child.unlink()
    elif problem == "wrong-receipt":
        receipt = next(row for row in rows if row["payload"].get("type") == "custom_tool_call_output")
        receipt["payload"]["output"][1]["text"] = "forged context"
    elif problem == "extra-tool":
        rows.insert(
            -1,
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "call_id": "unexpected",
                    "input": "text('extra')",
                },
            },
        )
    elif problem == "wrong-model":
        rows[1]["payload"]["model"] = "unrequested-model"
    elif problem == "wrong-output":
        dispatch = json.loads((run / "dispatch.json").read_text(encoding="utf-8"))
        challenger = next(call for call in dispatch["calls"] if call["role"] == "challenger")
        joined = next(
            row
            for row in parent_rows
            if row["payload"].get("author") == f"/root/{challenger['arguments']['task_name']}"
        )
        joined["payload"]["content"][0]["text"] += "\nforged final"
    else:
        rows[-1]["payload"].update(started_at=200, completed_at=210)
    if problem != "missing-child":
        _write_jsonl(child, rows)
    _write_jsonl(parent, parent_rows)
    result = _assemble(run, home)
    assert result.returncode != 0
    assert expected in result.stderr
    assert not (run / "specialist-manifest.json").exists()
    assert not (run / "inspection-summary.json").exists()
