"""Exercise lossless review assessment and finding namespace recovery."""

from __future__ import annotations

import json
import runpy
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timezone

import pytest

from test_review_prepare import HELPER, SKILL, _assembly_evidence, _finding_id_recovery_evidence, _write_jsonl


@pytest.mark.parametrize(
    "damage",
    [
        "none",
        "opaque-value",
        "canonical",
        "empty",
        "rating",
        "multiline",
        "duplicate",
        "duplicate-heading",
        "ascii-dash",
        "bare-prose",
        "closure-shape",
        "confidence",
    ],
)
def test_assessment_format_repair_changes_only_proved_missing_label(damage: str) -> None:
    """Lossless correction inserts one label while other grammar, inventory, and confidence defects reject."""
    import test_review_batches as batches

    record = batches._batch_finding("A", "medium", "Retain — this exact claim.")
    original = batches._batch_output([record], 3)
    expected = original
    malformed = original.replace("Rating: 3\nRationale: ", "Rating: 3 — ")
    if damage == "opaque-value":
        malformed += " The label Rating: remains prose."
        expected += " The label Rating: remains prose."
    elif damage == "canonical":
        malformed = original
    elif damage == "empty":
        malformed = malformed.replace("Assessment accounts for every declared obligation.", "")
    elif damage == "rating":
        malformed = malformed.replace("Rating: 3", "Rating: 5/5")
    elif damage == "multiline":
        malformed += "\nAdditional explanation."
    elif damage == "duplicate":
        malformed += "\nRating: 2"
    elif damage == "duplicate-heading":
        malformed += "\n## Reviewer Assessment\nRating: 3 — Another explanation."
    elif damage == "ascii-dash":
        malformed = malformed.replace("Rating: 3 — ", "Rating: 3 - ")
    elif damage == "bare-prose":
        malformed = malformed.replace("Rating: 3 — ", "")
    elif damage == "closure-shape":
        malformed = malformed.replace(json.dumps(record["closure_evidence"]), '["Other shape defect."]')
    elif damage == "confidence":
        malformed = malformed.replace('"score": 0.95', '"score": 2.0')
    snapshot = {"files": [{"path": "widget.py", "kind": "text", "content": "value = 2\n", "sha256": "a" * 64}]}
    validator = runpy.run_path(str(SKILL / "validate_artifacts.py"))
    if damage in {"none", "opaque-value"}:
        assert validator["_assessment_format_repair"](malformed, snapshot, "challenger") == expected
    else:
        with pytest.raises(
            SystemExit, match="review-(?:repair-ineligible-assessment|batch-individual-findings-.*):challenger"
        ):
            validator["_assessment_format_repair"](malformed, snapshot, "challenger")


@pytest.mark.integration
@pytest.mark.parametrize("opaque", [False, True])
@pytest.mark.parametrize("repair_kind", ["closure-evidence-shape", "assessment-format"])
@pytest.mark.parametrize(
    "damage",
    [
        "none",
        "instruction",
        "model",
        "claim",
        "rating",
        "rationale",
        "confidence",
        "tool",
        "original-source",
        "missing-receipt",
        "missing-release",
        "child-created",
    ],
)
def test_closure_correction_refusal_uses_generated_recovery_arguments(
    tmp_path: Path, opaque: bool, repair_kind: str, damage: str
) -> None:
    """Compose a proved no-child refusal with an exact claim-preserving correction allocation."""
    # Batch fixtures import this module; defer the reciprocal fixture dependency until collection completes.
    import test_review_batches as batches

    root = batches._batch_inputs(tmp_path)
    assert batches._batch_command(root, "prepare").returncode == 0
    run = root / "batches/source-001"
    record = batches._batch_finding("A", "medium", "Preserve every original obligation.")
    if repair_kind == "closure-evidence-shape":
        record["closure_evidence"] = ["First invariant.", "Second invariant."]
    original = batches._batch_output([record], 3)
    if repair_kind == "assessment-format":
        original = original.replace("Rating: 3\nRationale: ", "Rating: 3 — ")
    _, home, children = _assembly_evidence(
        tmp_path, prepared_run=run, findings={"challenger": original}, blocking_counts={"challenger": 1}
    )
    prepared = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "prepare-repair",
            "--out",
            str(run),
            "--codex-home",
            str(home),
            "--role",
            "challenger",
            "--kind",
            repair_kind,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert prepared.returncode == 0, prepared.stderr
    arguments = json.loads(prepared.stdout)["arguments"]
    corrected = arguments["message"].split("Return exactly this validated correction:\n", 1)[1]
    assert "```json" in arguments["message"]
    if damage == "claim":
        corrected = corrected.replace("Preserve every original obligation.", "No obligation remains.")
    elif damage == "rating":
        corrected = corrected.replace("Rating: 3", "Rating: 2")
    elif damage == "rationale":
        corrected = corrected.replace(
            "Assessment accounts for every declared obligation.", "The review is now complete."
        )
    elif damage == "confidence":
        corrected = corrected.replace('"score": 0.95', '"score": 0.94')
    rows = [json.loads(line) for line in children["challenger"].read_text().splitlines()]
    old_path, old_thread = rows[0]["payload"]["agent_path"], rows[0]["payload"]["id"]
    new_path, new_thread = old_path.removesuffix("_a1") + "_a2", "closure-capacity-replacement"
    rows = json.loads(json.dumps(rows).replace(old_path, new_path).replace(old_thread, new_thread))
    rows = [row for row in rows if row["type"] != "response_item"]
    rows.insert(
        2,
        {
            "type": "response_item",
            "payload": {
                "type": "agent_message",
                "content": [{"type": "encrypted_content", "encrypted_content": arguments["message"]}],
            },
        },
    )
    rows[0]["payload"]["timestamp"] = "2026-01-01T10:00:31.005Z"
    rows[-1]["payload"].update(started_at=1767261631.02, completed_at=1767261632.0, last_agent_message=corrected)
    if damage == "tool":
        rows.insert(
            -1,
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "call_id": "forbidden-tool",
                    "input": "text('extra inspection');",
                },
            },
        )
    replacement = home / f"sessions/rollout-{new_thread}.jsonl"
    _write_jsonl(replacement, rows)
    parent_path = home / "sessions/rollout-parent.jsonl"
    parent = [json.loads(line) for line in parent_path.read_text().splitlines()]
    # Another original child is active during refusal and its joined terminal releases capacity.
    sibling = children["qa-specialist"]
    sibling_rows = [json.loads(line) for line in sibling.read_text().splitlines()]
    sibling_path = sibling_rows[0]["payload"]["agent_path"]
    sibling_rows[-1]["payload"]["completed_at"] = 1767261630.0
    _write_jsonl(sibling, sibling_rows)
    for row in parent:
        if row.get("payload", {}).get("author") == sibling_path:
            row["timestamp"] = "2026-01-01T10:00:30.050Z"
    refused = dict(arguments)
    if opaque:
        refused["message"] = "opaque-refused-closure-correction"
        arguments = {**arguments, "message": "opaque-delivered-closure-correction"}
        rows[2]["payload"]["content"][0]["encrypted_content"] = arguments["message"]
        _write_jsonl(replacement, rows)
    if damage == "instruction":
        refused["message"] += "\nChange an additional claim.```"
    elif damage == "model":
        refused["model"] = "unexpected-model"
    refusal = {
        "timestamp": "2026-01-01T10:00:20.000Z",
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": "spawn_agent",
            "call_id": "closure-refused",
            "arguments": json.dumps(refused),
        },
    }
    refusal_receipt = {
        "timestamp": "2026-01-01T10:00:20.010Z",
        "type": "response_item",
        "payload": {
            "type": "function_call_output",
            "call_id": "closure-refused",
            "output": "collab spawn failed: agent thread limit reached",
        },
    }
    parent.extend(
        [
            refusal,
            refusal_receipt,
            {
                "timestamp": "2026-01-01T10:00:31.000Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "spawn_agent",
                    "call_id": "closure-retry",
                    "arguments": json.dumps(arguments),
                },
            },
            {
                "timestamp": "2026-01-01T10:00:31.010Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "closure-retry",
                    "output": json.dumps({"task_name": new_path}),
                },
            },
            {
                "timestamp": "2026-01-01T10:00:32.050Z",
                "type": "response_item",
                "payload": {
                    "type": "agent_message",
                    "author": new_path,
                    "content": [{"type": "input_text", "text": f"Message Type: FINAL_ANSWER\nPayload:\n{corrected}"}],
                },
            },
        ]
    )
    if damage == "missing-receipt":
        parent.remove(refusal_receipt)
    elif damage == "missing-release":
        sibling_rows[-1]["payload"]["completed_at"] = 1767261635.0
        _write_jsonl(sibling, sibling_rows)
        next(row for row in parent if row.get("payload", {}).get("author") == sibling_path)["timestamp"] = (
            "2026-01-01T10:00:35.050Z"
        )
    elif damage == "child-created":
        ghost = json.loads(json.dumps(rows[:1]))
        ghost[0]["payload"].update(id="refused-ghost", timestamp="2026-01-01T10:00:20.005Z")
        _write_jsonl(home / "sessions/rollout-refused-ghost.jsonl", ghost)
    elif damage == "original-source":
        original_rows = [json.loads(line) for line in children["challenger"].read_text().splitlines()]
        original_read = next(
            row["payload"] for row in original_rows if row.get("payload", {}).get("type") == "custom_tool_call"
        )
        original_read["input"] += "\nUnauthorized execution."
        _write_jsonl(children["challenger"], original_rows)
    parent.sort(key=lambda row: row.get("timestamp", ""))
    _write_jsonl(parent_path, parent)
    retained = {
        path: path.read_bytes()
        for path in [*children.values(), replacement, parent_path, run / "inspection-plan.json", run / "dispatch.json"]
    }
    assembled = batches._batch_command(run, "assemble-wave", home)
    if damage != "none":
        assert assembled.returncode != 0
        expected = {
            "instruction": "review-inspection-capacity-refusal-invalid:challenger",
            "model": "review-inspection-capacity-refusal-invalid:challenger",
            "claim": "review-repair-claims-changed:challenger",
            "rating": "review-repair-claims-changed:challenger",
            "rationale": "review-repair-claims-changed:challenger",
            "confidence": "review-repair-claims-changed:challenger",
            "tool": "review-repair-formatting-tool-use:challenger",
            "original-source": "review-inspection-context-read-call-mismatch:challenger",
            "missing-receipt": "review-inspection-capacity-refusal-invalid:challenger",
            "missing-release": "review-inspection-capacity-refusal-no-state-change:challenger",
            "child-created": "review-child-session-not-unique",
        }[damage]
        assert expected in assembled.stderr
    else:
        assert assembled.returncode == 0, assembled.stderr
        manifest = json.loads((run / "specialist-manifest.json").read_bytes())
        item = next(item for item in manifest["passes"] if item["role"] == "challenger")
        assert item["selected_attempt"] == 2 and len(item["attempts"]) == 2
        assert json.loads((run / "inspection-summary.json").read_text())["actual_mode"] == "parallel"
        assert (run / item["attempts"][0]["raw_output_path"]).read_text(encoding="utf-8") == original
        assert item["reviewer_findings"][0]["summary"] == record["summary"]
        assert item["reviewer_findings"][0]["closure_evidence"] == (
            "First invariant.\nSecond invariant."
            if repair_kind == "closure-evidence-shape"
            else record["closure_evidence"]
        )
        validator = runpy.run_path(str(SKILL / "validate_artifacts.py"))
        invalid = json.loads(json.dumps(manifest))
        invalid_item = next(entry for entry in invalid["passes"] if entry["role"] == "challenger")
        invalid_item["attempts"].append({"attempt": 3})
        with pytest.raises(SystemExit, match="manifest-invalid-attempt-count:challenger"):
            validator["_validate_manifest_entries"](
                run, invalid, invalid["passes"], set(children), home, "parent", tmp_path
            )
        historical = {**manifest, "schema_version": 7}
        with pytest.raises(SystemExit, match="manifest-invalid-internal-recovery:challenger"):
            validator["_validate_manifest_entries"](
                run, historical, manifest["passes"], set(children), home, "parent", tmp_path
            )
    assert all(path.read_bytes() == content for path, content in retained.items())


@pytest.mark.parametrize(
    "damage",
    [
        "none",
        "opaque-value",
        "absent-witness",
        "duplicate-witness",
        "unknown-origin",
        "duplicate-origin",
        "claim",
        "severity",
        "evidence",
        "collision",
        "second-id",
        "duplicate-key",
        "assessment",
        "closure",
        "confidence",
        "already-local",
    ],
)
def test_finding_id_namespace_correction_requires_unique_exact_origin(damage: str) -> None:
    """Correct one supplied namespace token while refusing ambiguity or any accompanying response defect."""
    import test_review_batches as batches  # Reciprocal fixture import waits until collection has completed.

    record = batches._batch_finding("LOCAL_A", "medium", "Preserve the proven original obligation.")
    if damage == "opaque-value":
        record["summary"] += ' Mention source-001.qa-specialist.LOCAL_A and "id" in opaque prose.'
    snapshot = {"files": [{"path": "widget.py", "kind": "text", "content": "value = 2\n", "sha256": "a" * 64}]}
    validator = runpy.run_path(str(SKILL / "validate_artifacts.py"))
    bound = validator["_batch_reviewer_findings"](
        Path("unused"), snapshot, "challenger", content_override=batches._batch_output([record], 3)
    )[0]
    origin = {
        "finding_id": "source-001.qa-specialist.LOCAL_A",
        "original": bound,
        "original_text": json.dumps(bound, sort_keys=True),
    }
    context = f"Source finding ID: {origin['finding_id']}\n{origin['original_text']}\n"
    origins = [origin]
    malformed = {**record, "id": origin["finding_id"]}
    records = [malformed]
    if damage == "absent-witness":
        context = "Unrelated context."
    elif damage == "duplicate-witness":
        context += context
    elif damage == "unknown-origin":
        origins = []
    elif damage == "duplicate-origin":
        origins += origins
    elif damage == "claim":
        malformed["summary"] = "Different obligation."
    elif damage == "severity":
        malformed["severity"] = "low"
    elif damage == "evidence":
        malformed["evidence"] = []
    elif damage == "collision":
        records.append(record)
    elif damage == "second-id":
        records.append({**record, "id": "unproved.other.LOCAL_B"})
    elif damage == "closure":
        malformed["closure_evidence"] = ["Wrong representation."]
    elif damage == "already-local":
        malformed["id"] = record["id"]
    raw = batches._batch_output(records, 3)
    if damage == "duplicate-key":
        raw = raw.replace('"severity":', '"id": "HIDDEN", "severity":', 1)
    elif damage == "assessment":
        raw = raw.replace("Rating: 3\nRationale: ", "Rating: 3 — ")
    elif damage == "confidence":
        raw = raw.replace('"score": 0.95', '"score": 2.0')
    if damage in {"none", "opaque-value"}:
        expected = raw.replace(json.dumps(origin["finding_id"]), json.dumps(record["id"]), 1)
        assert validator["_finding_id_namespace_repair"](raw, snapshot, "challenger", context, origins) == expected
    else:
        with pytest.raises(SystemExit, match="review-(?:repair-.*|batch-individual-findings-.*):challenger"):
            validator["_finding_id_namespace_repair"](raw, snapshot, "challenger", context, origins)


@pytest.mark.integration
@pytest.mark.parametrize(
    "damage", ["none", "claim", "tool", "original-read", "missing-join", "second-repair", "origin-raw", "before-join"]
)
def test_finding_id_namespace_public_recovery_preserves_original_provenance(tmp_path: Path, damage: str) -> None:
    """Require generated tool-free a2 bytes, complete original reads and one native correction attempt."""
    import test_review_batches as batches  # Batch and native fixture modules share this established boundary.

    run, home, children, original, expected = _finding_id_recovery_evidence(tmp_path)
    initial = batches._batch_command(run, "assemble-wave", home)
    assert initial.returncode != 0 and "review-batch-individual-findings-record:qa-specialist" in initial.stderr
    argv = [
        sys.executable,
        str(HELPER),
        "prepare-repair",
        "--out",
        str(run),
        "--codex-home",
        str(home),
        "--role",
        "qa-specialist",
        "--kind",
        "finding-id-namespace",
    ]
    if damage == "origin-raw":
        origin = run.parent / "source-001/specialists/challenger.raw.md"
        origin.write_bytes(
            origin.read_bytes().replace(b"Preserve the exact source obligation.", b"Different obligation.")
        )
    prepared = subprocess.run(argv, capture_output=True, text=True, check=False)
    if damage == "origin-raw":
        assert prepared.returncode != 0
        assert "provenance-raw-output-mismatch:challenger" in prepared.stderr
        return
    assert prepared.returncode == 0, prepared.stderr
    arguments = json.loads(prepared.stdout)["arguments"]
    assert arguments["message"].split("Return exactly this validated correction:\n", 1)[1] == expected
    original_bytes = children["qa-specialist"].read_bytes()
    rows = [json.loads(line) for line in original_bytes.decode().splitlines()]
    old_path, old_thread = rows[0]["payload"]["agent_path"], rows[0]["payload"]["id"]
    new_path, new_thread = old_path.removesuffix("_a1") + "_a2", "namespace-replacement"
    rows = json.loads(json.dumps(rows).replace(old_path, new_path).replace(old_thread, new_thread))
    rows = [row for row in rows if row["type"] != "response_item"]
    rows.insert(
        2,
        {
            "type": "response_item",
            "payload": {
                "type": "agent_message",
                "content": [{"type": "encrypted_content", "encrypted_content": arguments["message"]}],
            },
        },
    )
    replacement_epoch = rows[-1]["payload"]["completed_at"] + (0 if damage == "before-join" else 10)
    timestamps = {
        offset: datetime.fromtimestamp(replacement_epoch + offset, timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
        for offset in (0, 0.05, 0.1, 1.1)
    }
    rows[0]["payload"]["timestamp"] = timestamps[0.05]
    if damage == "claim":
        expected = expected.replace("Preserve the exact source obligation.", "Different obligation.")
    rows[-1]["payload"].update(
        started_at=replacement_epoch + 0.2, completed_at=replacement_epoch + 1, last_agent_message=expected
    )
    if damage == "tool":
        rows.insert(
            -1,
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "call_id": "forbidden-tool",
                    "input": "text('extra inspection');",
                },
            },
        )
    _write_jsonl(home / f"sessions/rollout-{new_thread}.jsonl", rows)
    if damage == "second-repair":
        _write_jsonl(home / f"sessions/rollout-duplicate-{new_thread}.jsonl", rows)
    parent_path = home / "sessions/rollout-parent.jsonl"
    parent = [json.loads(line) for line in parent_path.read_text().splitlines()]
    parent.extend(
        [
            {
                "timestamp": timestamps[0],
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "spawn_agent",
                    "call_id": "namespace-spawn",
                    "arguments": json.dumps(arguments),
                },
            },
            {
                "timestamp": timestamps[0.1],
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "namespace-spawn",
                    "output": json.dumps({"task_name": new_path}),
                },
            },
            {
                "timestamp": timestamps[1.1],
                "type": "response_item",
                "payload": {
                    "type": "agent_message",
                    "author": new_path,
                    "content": [{"type": "input_text", "text": f"Message Type: FINAL_ANSWER\nPayload:\n{expected}"}],
                },
            },
        ]
    )
    if damage == "missing-join":
        parent = [row for row in parent if row.get("payload", {}).get("author") != old_path]
    _write_jsonl(parent_path, parent)
    if damage == "original-read":
        retained = [json.loads(line) for line in original_bytes.decode().splitlines()]
        next(row["payload"] for row in retained if row.get("payload", {}).get("type") == "custom_tool_call")[
            "input"
        ] += "\nUnauthorized execution."
        _write_jsonl(children["qa-specialist"], retained)
    result = batches._batch_command(run, "assemble-wave", home)
    if damage != "none":
        assert result.returncode != 0
        diagnostics = {
            "claim": "review-repair-claims-changed",
            "tool": "review-repair-formatting-tool-use",
            "original-read": "review-inspection-context-read-call-mismatch",
            "missing-join": "review-repair-original-not-joined",
            "second-repair": "review-child-session-not-unique",
            "before-join": "review-repair-before-original-join",
        }
        assert diagnostics[damage] in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        manifest = json.loads((run / "specialist-manifest.json").read_bytes())
        item = next(item for item in manifest["passes"] if item["role"] == "qa-specialist")
        assert item["recovery"] == {"kind": "finding-id-namespace"}
        assert len(item["attempts"]) == 2 and item["selected_attempt"] == 2
        assert item["reviewer_findings"][0]["id"] == "LOCAL_A"
        assert (run / item["attempts"][0]["raw_output_path"]).read_bytes().decode() == original
        assert children["qa-specialist"].read_bytes() == original_bytes
        assert json.loads((run / "inspection-summary.json").read_bytes())["actual_mode"] == "parallel"


@pytest.mark.parametrize(
    "manifest, directory",
    [
        pytest.param({"schema_version": 7}, Path("batches") / "interaction-001", id="historical-schema"),
        pytest.param({"schema_version": 8}, Path("ordinary-review"), id="ordinary-profile"),
    ],
)
def test_finding_id_namespace_recovery_rejects_other_profiles(manifest: dict[str, object], directory: Path) -> None:
    """Keep identity correction out of historical and ordinary reviewer protocols."""
    validator = runpy.run_path(str(SKILL / "validate_artifacts.py"))
    item = {"role": "challenger", "recovery": {"kind": "finding-id-namespace"}}
    with pytest.raises(SystemExit, match="review-repair-ineligible-assessment:challenger"):
        validator["_recovery_arguments"](directory, manifest, item, [], Path("unused"))


def test_batch_response_identifies_local_and_qualified_id_namespaces() -> None:
    """Supply the exact local-ID precondition and provenance namespace before a future reviewer responds."""
    producer = runpy.run_path(str(SKILL / "review_batches.py"))
    instruction = producer["BATCH_FINDINGS_INSTRUCTION"].decode()
    assert "[A-Za-z][A-Za-z0-9_-]{0,63}" in instruction
    assert "reuse its original JSON record id only when unique in the current response" in instruction
    assert "otherwise choose a valid unique local id" in instruction
    assert "Do not copy that qualified origin into id" in instruction
