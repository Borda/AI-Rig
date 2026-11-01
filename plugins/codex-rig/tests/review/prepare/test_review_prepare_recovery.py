"""Exercise lossless review assessment and finding namespace recovery.

Recovery rewrites a reviewer response only when the defect is proved and claim-preserving. These tests drive the real
``prepare-repair`` and ``assemble-wave`` commands and the validator repair functions against damaged native evidence.
"""

from __future__ import annotations

import copy
import functools
import json
import runpy
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pytest
from _artifact_files import read_jsonl
from test_review_prepare import (
    HELPER,
    SKILL,
    _assembly_evidence,
    _finding_id_recovery_evidence,
    _inject_partial_dispatch_failure,
    _partial_dispatch_base,
    _restore_tree,
    _tree_state,
    _write_jsonl,
)

_SNAPSHOT = {"files": [{"path": "widget.py", "kind": "text", "content": "value = 2\n", "sha256": "a" * 64}]}
_CORRECTION_MARKER = "Return exactly this validated correction:\n"
_CLOSURE_DAMAGES = [
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
]
_CLOSURE_SCENARIOS = [{"opaque": opaque, "damage": damage} for opaque in (False, True) for damage in _CLOSURE_DAMAGES]
_NAMESPACE_RECOVERY_DAMAGES = [
    "none",
    "claim",
    "tool",
    "original-read",
    "missing-join",
    "second-repair",
    "origin-raw",
    "before-join",
]
_OPAQUE_NAMESPACE_MENTION = ' Mention source-001.qa-specialist.LOCAL_A and "id" in opaque prose.'
_OPAQUE_LABEL_PROSE = " The label Rating: remains prose."


@functools.cache
def _validator() -> dict:
    """Load the shipped artifact validator once; its repair functions are pure."""
    return runpy.run_path(str(SKILL / "validate_artifacts.py"))


def _javascript_frames(message: str) -> list[str]:
    """Collect the bodies of the fenced ``javascript`` frames in a dispatch message, line by line."""
    frames: list[str] = []
    current: list[str] | None = None
    for line in message.splitlines():
        if current is None:
            current = [] if line.startswith("```javascript") else None
        elif line.startswith("```"):
            frames.append("\n".join(current))
            current = None
        else:
            current.append(line)
    return frames


def _prepare_repair(run: Path, home: Path, role: str, kind: str) -> subprocess.CompletedProcess[str]:
    """Run the real ``prepare-repair`` command for one role and recovery kind."""
    argv = [sys.executable, str(HELPER), "prepare-repair", "--out", str(run), "--codex-home", str(home)]
    return subprocess.run([*argv, "--role", role, "--kind", kind], capture_output=True, text=True, check=False)


def _replay_scenarios(
    subtests: pytest.Subtests,
    root: Path,
    state: dict,
    scenarios: list[dict],
    check: Callable[..., None],
) -> None:
    """Run each scenario as an independent subtest from a byte-exact restore of one expensive build."""
    for scenario in scenarios:
        with subtests.test(**scenario):
            _restore_tree(root, state)
            check(**scenario)


def _spawn_row(timestamp: str, call_id: str, arguments: dict) -> dict:
    """Build a parent ``spawn_agent`` call row."""
    return {
        "timestamp": timestamp,
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": "spawn_agent",
            "call_id": call_id,
            "arguments": json.dumps(arguments),
        },
    }


def _call_output_row(timestamp: str, call_id: str, output: str) -> dict:
    """Build a parent tool-call output row."""
    return {
        "timestamp": timestamp,
        "type": "response_item",
        "payload": {"type": "function_call_output", "call_id": call_id, "output": output},
    }


def _final_answer_row(timestamp: str, author: str, payload: str) -> dict:
    """Build the parent row that joins a child's final answer."""
    return {
        "timestamp": timestamp,
        "type": "response_item",
        "payload": {
            "type": "agent_message",
            "author": author,
            "content": [{"type": "input_text", "text": f"Message Type: FINAL_ANSWER\nPayload:\n{payload}"}],
        },
    }


def _forbidden_tool_row() -> dict:
    """Build a child tool call that a formatting-only correction may never make."""
    return {
        "type": "response_item",
        "payload": {
            "type": "custom_tool_call",
            "name": "exec",
            "call_id": "forbidden-tool",
            "input": "text('extra inspection');",
        },
    }


def _replacement_rows(original_rows: list[dict], thread: str, message: str) -> tuple[list[dict], str, str]:
    """Clone a child session as its second attempt, delivering ``message`` as the only parent instruction."""
    old_path, old_thread = original_rows[0]["payload"]["agent_path"], original_rows[0]["payload"]["id"]
    new_path = old_path.removesuffix("_a1") + "_a2"
    rows = json.loads(json.dumps(original_rows).replace(old_path, new_path).replace(old_thread, thread))
    rows = [row for row in rows if row["type"] != "response_item"]
    delivery = {"type": "agent_message", "content": [{"type": "encrypted_content", "encrypted_content": message}]}
    rows.insert(2, {"type": "response_item", "payload": delivery})
    return rows, old_path, new_path


@dataclass
class _NamespaceCase:
    """One reviewer response whose finding id carries a qualified namespace, plus the context proving its origin."""

    record: dict
    origin: dict
    context: str
    origins: list[dict]
    malformed: dict
    records: list[dict]
    edit_raw: Callable[[str], str] = field(default=lambda raw: raw)

    def raw(self) -> str:
        """Render the response a reviewer would have produced for the current records."""
        import test_review_batches as batches  # Reciprocal fixture import waits until collection has completed.

        return self.edit_raw(batches._batch_output(self.records, 3))


def _namespace_case(summary_suffix: str = "") -> _NamespaceCase:
    """Build a local finding, its proved qualified origin and a response using the qualified id."""
    import test_review_batches as batches  # Reciprocal fixture import waits until collection has completed.

    record = batches._batch_finding("LOCAL_A", "medium", "Preserve the proven original obligation.")
    record["summary"] += summary_suffix
    bound = _validator()["_batch_reviewer_findings"](
        Path("unused"), _SNAPSHOT, "challenger", content_override=batches._batch_output([record], 3)
    )[0]
    origin = {
        "finding_id": "source-001.qa-specialist.LOCAL_A",
        "original": bound,
        "original_text": json.dumps(bound, sort_keys=True),
    }
    context = f"Source finding ID: {origin['finding_id']}\n{origin['original_text']}\n"
    malformed = {**record, "id": origin["finding_id"]}
    return _NamespaceCase(record, origin, context, [origin], malformed, [malformed])


def _absent_witness(case: _NamespaceCase) -> None:
    """Drop the context that proves the qualified origin."""
    case.context = "Unrelated context."


def _duplicate_witness(case: _NamespaceCase) -> None:
    """Repeat the origin witness so the match is no longer unique."""
    case.context += case.context


def _unknown_origin(case: _NamespaceCase) -> None:
    """Supply no origin for the qualified id."""
    case.origins = []


def _duplicate_origin(case: _NamespaceCase) -> None:
    """Supply the same origin twice."""
    case.origins += case.origins


def _changed_claim(case: _NamespaceCase) -> None:
    """Change the summary claim alongside the id."""
    case.malformed["summary"] = "Different obligation."


def _changed_severity(case: _NamespaceCase) -> None:
    """Change the severity alongside the id."""
    case.malformed["severity"] = "low"


def _dropped_evidence(case: _NamespaceCase) -> None:
    """Drop the evidence alongside the id."""
    case.malformed["evidence"] = []


def _colliding_local_id(case: _NamespaceCase) -> None:
    """Add a second finding that already uses the local id."""
    case.records.append(case.record)


def _second_qualified_id(case: _NamespaceCase) -> None:
    """Add a second finding with an unproved qualified id."""
    case.records.append({**case.record, "id": "unproved.other.LOCAL_B"})


def _changed_closure(case: _NamespaceCase) -> None:
    """Change the closure evidence alongside the id."""
    case.malformed["closure_evidence"] = ["Wrong representation."]


def _already_local(case: _NamespaceCase) -> None:
    """Use the local id already, leaving nothing to correct."""
    case.malformed["id"] = case.record["id"]


def _duplicate_key(case: _NamespaceCase) -> None:
    """Hide a second ``id`` key in the first record."""
    case.edit_raw = lambda raw: raw.replace('"severity":', '"id": "HIDDEN", "severity":', 1)


def _broken_assessment(case: _NamespaceCase) -> None:
    """Break the assessment label grammar."""
    case.edit_raw = lambda raw: raw.replace("\nRationale: ", " — ")


def _invalid_confidence(case: _NamespaceCase) -> None:
    """Make the confidence score invalid."""
    case.edit_raw = lambda raw: raw.replace('"score": 0.95', '"score": 2.0')


def _assessment_output(family: str) -> tuple[dict, str, str]:
    """Build a canonical batch response of the ``historical`` or ``current`` assessment family and its label."""
    import test_review_batches as batches  # Reciprocal fixture import waits until collection has completed.

    record = batches._batch_finding("A", "medium", "Retain — this exact claim.")
    original = batches._batch_output([record], 3)
    historical = original.replace("Source rating version: 1\n", "").replace("Verification readiness: complete\n", "")
    label = "Rating: 3" if family == "historical" else "Verification readiness: complete"
    return record, (historical if family == "historical" else original), label


def _closure_correction_base(
    tmp_path: Path, repair_kind: str
) -> tuple[Path, Path, dict[str, Path], dict[str, object], str, dict[str, object]]:
    """Prepare a batch whose challenger output needs one correction and generate that correction's arguments."""
    import test_review_batches as batches  # Reciprocal fixture import waits until collection has completed.

    root = batches._batch_inputs(tmp_path)
    assert batches._batch_command(root, "prepare").returncode == 0
    run = root / "batches/source-001"
    record = batches._batch_finding("A", "medium", "Preserve every original obligation.")
    record["closure_evidence"] = (
        ["First invariant.", "Second invariant."]
        if repair_kind == "closure-evidence-shape"
        else record["closure_evidence"]
    )
    original = batches._batch_output([record], 3)
    original = original.replace("\nRationale: ", " — ") if repair_kind == "assessment-format" else original
    _, home, children = _assembly_evidence(
        tmp_path, prepared_run=run, findings={"challenger": original}, blocking_counts={"challenger": 1}
    )
    prepared = _prepare_repair(run, home, "challenger", repair_kind)
    assert prepared.returncode == 0, prepared.stderr
    return run, home, children, record, original, json.loads(prepared.stdout)["arguments"]


def _closure_claim_changes() -> dict[str, tuple[str, str]]:
    """Map each claim-changing damage to the text it replaces in the validated correction."""
    return {
        "claim": ("Preserve every original obligation.", "No obligation remains."),
        "rating": ("Rating: 3", "Rating: 2"),
        "rationale": ("Assessment accounts for every declared obligation.", "The review is now complete."),
        "confidence": ('"score": 0.95', '"score": 0.94'),
    }


def _check_closure_correction_refusal(
    tmp_path: Path,
    run: Path,
    home: Path,
    children: dict[str, Path],
    correction: tuple[dict[str, object], str, dict[str, object], str],
    *,
    repair_kind: str,
    opaque: bool,
    damage: str,
) -> None:
    """Record one refused and one retried correction launch, apply one damage, and check wave assembly."""
    import test_review_batches as batches  # Reciprocal fixture import waits until collection has completed.

    record, original, arguments, corrected = correction
    old, new = _closure_claim_changes().get(damage, ("", ""))
    corrected = corrected.replace(old, new) if old else corrected
    rows = read_jsonl(children["challenger"])
    rows, _, new_path = _replacement_rows(rows, "closure-capacity-replacement", arguments["message"])
    rows[0]["payload"]["timestamp"] = "2026-01-01T10:00:31.005Z"
    rows[-1]["payload"].update(started_at=1767261631.02, completed_at=1767261632.0, last_agent_message=corrected)
    if damage == "tool":
        rows.insert(-1, _forbidden_tool_row())
    replacement = home / "sessions/rollout-closure-capacity-replacement.jsonl"
    _write_jsonl(replacement, rows)
    parent_path = home / "sessions/rollout-parent.jsonl"
    parent = read_jsonl(parent_path)
    # Another original child is active during refusal and its joined terminal releases capacity.
    sibling = children["qa-specialist"]
    sibling_rows = read_jsonl(sibling)
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
    refusal = _spawn_row("2026-01-01T10:00:20.000Z", "closure-refused", refused)
    refusal_receipt = _call_output_row(
        "2026-01-01T10:00:20.010Z", "closure-refused", "collab spawn failed: agent thread limit reached"
    )
    parent.extend(
        [
            refusal,
            refusal_receipt,
            _spawn_row("2026-01-01T10:00:31.000Z", "closure-retry", arguments),
            _call_output_row("2026-01-01T10:00:31.010Z", "closure-retry", json.dumps({"task_name": new_path})),
            _final_answer_row("2026-01-01T10:00:32.050Z", new_path, corrected),
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
        ghost = copy.deepcopy(rows[:1])
        ghost[0]["payload"].update(id="refused-ghost", timestamp="2026-01-01T10:00:20.005Z")
        _write_jsonl(home / "sessions/rollout-refused-ghost.jsonl", ghost)
    elif damage == "original-source":
        original_rows = read_jsonl(children["challenger"])
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
        assert item["selected_attempt"] == 2
        assert len(item["attempts"]) == 2
        assert json.loads((run / "inspection-summary.json").read_text())["actual_mode"] == "parallel"
        assert (run / item["attempts"][0]["raw_output_path"]).read_text(encoding="utf-8") == original
        assert item["reviewer_findings"][0]["summary"] == record["summary"]
        assert item["reviewer_findings"][0]["closure_evidence"] == (
            "First invariant.\nSecond invariant."
            if repair_kind == "closure-evidence-shape"
            else record["closure_evidence"]
        )
        invalid = copy.deepcopy(manifest)
        invalid_item = next(entry for entry in invalid["passes"] if entry["role"] == "challenger")
        invalid_item["attempts"].append({"attempt": 3})
        with pytest.raises(SystemExit, match="manifest-invalid-attempt-count:challenger"):
            _validator()["_validate_manifest_entries"](
                run, invalid, invalid["passes"], set(children), home, "parent", tmp_path
            )
        historical = {**manifest, "schema_version": 7}
        with pytest.raises(SystemExit, match="manifest-invalid-internal-recovery:challenger"):
            _validator()["_validate_manifest_entries"](
                run, historical, manifest["passes"], set(children), home, "parent", tmp_path
            )
    assert all(path.read_bytes() == content for path, content in retained.items())


def _check_finding_id_namespace_recovery(
    tmp_path: Path, run: Path, home: Path, children: dict[str, Path], original: str, expected: str, damage: str
) -> None:
    """Generate the namespace correction, record its replacement with one damage, and check wave assembly."""
    import test_review_batches as batches  # Reciprocal fixture import waits until collection has completed.

    if damage == "origin-raw":
        origin = run.parent / "source-001/specialists/challenger.raw.md"
        origin.write_bytes(
            origin.read_bytes().replace(b"Preserve the exact source obligation.", b"Different obligation.")
        )
    prepared = _prepare_repair(run, home, "qa-specialist", "finding-id-namespace")
    if damage == "origin-raw":
        assert prepared.returncode != 0
        assert "provenance-raw-output-mismatch:challenger" in prepared.stderr
        return
    assert prepared.returncode == 0, prepared.stderr
    arguments = json.loads(prepared.stdout)["arguments"]
    assert arguments["message"].split(_CORRECTION_MARKER, 1)[1] == expected
    original_bytes = children["qa-specialist"].read_bytes()
    rows, old_path, new_path = _replacement_rows(
        read_jsonl(children["qa-specialist"]), "namespace-replacement", arguments["message"]
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
        rows.insert(-1, _forbidden_tool_row())
    _write_jsonl(home / "sessions/rollout-namespace-replacement.jsonl", rows)
    if damage == "second-repair":
        _write_jsonl(home / "sessions/rollout-duplicate-namespace-replacement.jsonl", rows)
    parent_path = home / "sessions/rollout-parent.jsonl"
    parent = read_jsonl(parent_path)
    parent.extend(
        [
            _spawn_row(timestamps[0], "namespace-spawn", arguments),
            _call_output_row(timestamps[0.1], "namespace-spawn", json.dumps({"task_name": new_path})),
            _final_answer_row(timestamps[1.1], new_path, expected),
        ]
    )
    if damage == "missing-join":
        parent = [row for row in parent if row.get("payload", {}).get("author") != old_path]
    _write_jsonl(parent_path, parent)
    if damage == "original-read":
        retained = read_jsonl(children["qa-specialist"])
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
        assert len(item["attempts"]) == 2
        assert item["selected_attempt"] == 2
        assert item["reviewer_findings"][0]["id"] == "LOCAL_A"
        assert (run / item["attempts"][0]["raw_output_path"]).read_bytes().decode() == original
        assert children["qa-specialist"].read_bytes() == original_bytes
        assert json.loads((run / "inspection-summary.json").read_bytes())["actual_mode"] == "parallel"


class TestCompactDispatchRepair:
    """A replacement dispatch is prepared after real launch failures of a compact reader."""

    @pytest.mark.integration
    @pytest.mark.parametrize("failure", ["missing-reader", "duplicate-interpreter-first", "duplicate-interpreter-last"])
    @pytest.mark.parametrize("parent_typo", [False, True])
    def test_uses_canonical_arguments(self, tmp_path: Path, failure: str, parent_typo: bool) -> None:
        """Prepare a replacement after real launch failures without parsing compact selectors as JSON.

        A compact-reader challenger fails to launch its reader (missing file or a duplicated interpreter), with or
        without a typo in the parent call. The repair must regenerate attempt 2 frames from canonical arguments.
        """
        run, home, children = _partial_dispatch_base(tmp_path, compact_reader=True)
        _inject_partial_dispatch_failure(tmp_path, run, home, children, parent_typo=parent_typo, failure=failure)
        retained = {
            path: path.read_bytes()
            for path in [run / "inspection-plan.json", run / "dispatch.json", *children.values()]
        }
        repaired = _prepare_repair(run, home, "challenger", "incomplete-dispatch")
        assert repaired.returncode == 0, repaired.stderr
        blocks = _javascript_frames(json.loads(repaired.stdout)["arguments"]["message"])
        assert "--attempt 2" in blocks[0]
        assert 'store("review-context-' in blocks[0]
        assert len(blocks) >= 3
        assert all('load("review-context-' in block for block in blocks[1:])
        assert all(path.read_bytes() == content for path, content in retained.items())


class TestAssessmentFormatRepair:
    """Lossless correction restores a missing assessment label and refuses every other grammar defect."""

    @pytest.mark.parametrize("family", ["historical", "current"])
    @pytest.mark.parametrize(
        "suffix",
        [pytest.param("", id="none"), pytest.param(_OPAQUE_LABEL_PROSE, id="opaque-value")],
    )
    def test_inserts_only_proved_missing_label(self, family: str, suffix: str) -> None:
        """Lossless correction restores the Rationale label and leaves label-like prose in the claim untouched.

        The malformed response replaced the Rationale line break with a dash. A response already carrying label text
        inside its prose must round-trip exactly, so only the proved missing label is inserted.
        """
        _, original, _ = _assessment_output(family)
        malformed = original.replace("\nRationale: ", " — ") + suffix
        repaired = _validator()["_assessment_format_repair"](malformed, _SNAPSHOT, "challenger")
        assert repaired == original + suffix

    @pytest.mark.parametrize("family", ["historical", "current"])
    @pytest.mark.parametrize(
        "damage",
        [
            pytest.param(lambda malformed, original, label, record: original, id="canonical"),
            pytest.param(
                lambda malformed, original, label, record: malformed.replace(
                    "Assessment accounts for every declared obligation.", ""
                ),
                id="empty",
            ),
            pytest.param(
                lambda malformed, original, label, record: malformed.replace("Rating: 3", "Rating: 5/5"), id="rating"
            ),
            pytest.param(
                lambda malformed, original, label, record: malformed + "\nAdditional explanation.", id="multiline"
            ),
            pytest.param(lambda malformed, original, label, record: malformed + "\nRating: 2", id="duplicate"),
            pytest.param(
                lambda malformed, original, label, record: (
                    malformed + "\n## Reviewer Assessment\nRating: 3 — Another explanation."
                ),
                id="duplicate-heading",
            ),
            pytest.param(
                lambda malformed, original, label, record: malformed.replace(f"{label} — ", f"{label} - "),
                id="ascii-dash",
            ),
            pytest.param(
                lambda malformed, original, label, record: malformed.replace(f"{label} — ", ""), id="bare-prose"
            ),
            pytest.param(
                lambda malformed, original, label, record: malformed.replace(
                    json.dumps(record["closure_evidence"]), '["Other shape defect."]'
                ),
                id="closure-shape",
            ),
            pytest.param(
                lambda malformed, original, label, record: malformed.replace('"score": 0.95', '"score": 2.0'),
                id="confidence",
            ),
        ],
    )
    def test_rejects_other_defects(self, family: str, damage: Callable[..., str]) -> None:
        """Refuse correction when the response is canonical or carries any defect besides the missing label.

        Empty text, a malformed rating, extra or duplicated assessment prose, a different dash, bare prose, a changed
        closure shape and an invalid confidence each need a reviewer, not a mechanical repair.
        """
        record, original, label = _assessment_output(family)
        malformed = damage(original.replace("\nRationale: ", " — "), original, label, record)
        with pytest.raises(
            SystemExit, match=r"review-(?:repair-ineligible-assessment|batch-individual-findings-.*):challenger"
        ):
            _validator()["_assessment_format_repair"](malformed, _SNAPSHOT, "challenger")


class TestClosureCorrectionRefusal:
    """A proved no-child capacity refusal composes with an exact claim-preserving correction allocation."""

    @pytest.mark.integration
    @pytest.mark.parametrize("repair_kind", ["closure-evidence-shape", "assessment-format"])
    def test_uses_generated_recovery_arguments(
        self, tmp_path: Path, subtests: pytest.Subtests, repair_kind: str
    ) -> None:
        """Compose a proved no-child refusal with an exact claim-preserving correction allocation.

        Every delivery mode and damage replays a refusal against the same prepared correction, so the batch, its native
        evidence, and the generated correction are built once per repair kind and each combination runs as an
        independent subtest from a byte-exact restore of that build.
        """
        run, home, children, record, original, arguments = _closure_correction_base(tmp_path, repair_kind)
        corrected = arguments["message"].split(_CORRECTION_MARKER, 1)[1]
        assert "```json" in arguments["message"]
        check = functools.partial(
            _check_closure_correction_refusal,
            tmp_path,
            run,
            home,
            children,
            (record, original, dict(arguments), corrected),
            repair_kind=repair_kind,
        )
        _replay_scenarios(subtests, tmp_path, _tree_state(tmp_path), _CLOSURE_SCENARIOS, check)


class TestFindingIdNamespaceCorrection:
    """A qualified finding id is corrected to its local id only when its origin is unique and exact."""

    @pytest.mark.parametrize(
        "summary_suffix",
        [pytest.param("", id="none"), pytest.param(_OPAQUE_NAMESPACE_MENTION, id="opaque-value")],
    )
    def test_corrects_one_supplied_namespace_token(self, summary_suffix: str) -> None:
        """Replace exactly the proved qualified id with the local id, ignoring the same text inside opaque prose.

        The response uses the qualified id of a finding whose origin the context proves once. Only the id token may
        change, even when the claim prose itself mentions the qualified id.
        """
        case = _namespace_case(summary_suffix)
        raw = case.raw()
        expected = raw.replace(json.dumps(case.origin["finding_id"]), json.dumps(case.record["id"]), 1)
        repaired = _validator()["_finding_id_namespace_repair"](
            raw, _SNAPSHOT, "challenger", case.context, case.origins
        )
        assert repaired == expected

    @pytest.mark.parametrize(
        "damage",
        [
            pytest.param(_absent_witness, id="absent-witness"),
            pytest.param(_duplicate_witness, id="duplicate-witness"),
            pytest.param(_unknown_origin, id="unknown-origin"),
            pytest.param(_duplicate_origin, id="duplicate-origin"),
            pytest.param(_changed_claim, id="claim"),
            pytest.param(_changed_severity, id="severity"),
            pytest.param(_dropped_evidence, id="evidence"),
            pytest.param(_colliding_local_id, id="collision"),
            pytest.param(_second_qualified_id, id="second-id"),
            pytest.param(_duplicate_key, id="duplicate-key"),
            pytest.param(_broken_assessment, id="assessment"),
            pytest.param(_changed_closure, id="closure"),
            pytest.param(_invalid_confidence, id="confidence"),
            pytest.param(_already_local, id="already-local"),
        ],
    )
    def test_rejects_ambiguity_or_accompanying_defect(self, damage: Callable[[_NamespaceCase], None]) -> None:
        """Refuse correction when the origin is absent or ambiguous or the response has any other defect.

        A missing, repeated or unknown origin witness, a changed claim, severity, evidence or closure, a colliding or
        second id, a hidden duplicate key, a broken assessment or confidence, or an already local id all need review.
        """
        case = _namespace_case()
        damage(case)
        with pytest.raises(SystemExit, match=r"review-(?:repair-.*|batch-individual-findings-.*):challenger"):
            _validator()["_finding_id_namespace_repair"](
                case.raw(), _SNAPSHOT, "challenger", case.context, case.origins
            )

    @pytest.mark.integration
    def test_public_recovery_preserves_original_provenance(self, tmp_path: Path, subtests: pytest.Subtests) -> None:
        """Require generated tool-free a2 bytes, complete original reads and one native correction attempt.

        Every damage starts from the same refused interaction wave, so that wave is built once and each damage runs as
        an independent subtest from a byte-exact restore of it.
        """
        import test_review_batches as batches  # Batch and native fixture modules share this established boundary.

        run, home, children, original, expected = _finding_id_recovery_evidence(tmp_path)
        initial = batches._batch_command(run, "assemble-wave", home)
        assert initial.returncode != 0
        assert "review-batch-individual-findings-record:qa-specialist" in initial.stderr
        check = functools.partial(
            _check_finding_id_namespace_recovery, tmp_path, run, home, children, original, expected
        )
        scenarios = [{"damage": damage} for damage in _NAMESPACE_RECOVERY_DAMAGES]
        _replay_scenarios(subtests, tmp_path, _tree_state(tmp_path), scenarios, check)

    @pytest.mark.parametrize(
        ("manifest", "directory"),
        [
            pytest.param({"schema_version": 7}, Path("batches") / "interaction-001", id="historical-schema"),
            pytest.param({"schema_version": 8}, Path("ordinary-review"), id="ordinary-profile"),
        ],
    )
    def test_recovery_rejects_other_profiles(self, manifest: dict[str, object], directory: Path) -> None:
        """Keep identity correction out of historical and ordinary reviewer protocols.

        A manifest of the historical schema or a run outside the batch interaction layout must not build recovery
        arguments for a finding-id correction.
        """
        item = {"role": "challenger", "recovery": {"kind": "finding-id-namespace"}}
        with pytest.raises(SystemExit, match="review-repair-ineligible-assessment:challenger"):
            _validator()["_recovery_arguments"](directory, manifest, item, [], Path("unused"))


class TestBatchResponseInstruction:
    """The reviewer instruction states how local and qualified finding ids relate."""

    def test_identifies_local_and_qualified_id_namespaces(self) -> None:
        """Supply the exact local-ID precondition and provenance namespace before a future reviewer responds.

        The producer instruction is the only place a reviewer learns the id grammar, so it must state the local id
        pattern, when an original id is reusable, and that a qualified origin is never copied into ``id``.
        """
        producer = runpy.run_path(str(SKILL / "review_batches.py"))
        instruction = producer["BATCH_FINDINGS_INSTRUCTION"].decode()
        assert "[A-Za-z][A-Za-z0-9_-]{0,63}" in instruction
        assert "reuse its original JSON record id only when unique in the current response" in instruction
        assert "otherwise choose a valid unique local id" in instruction
        assert "Do not copy that qualified origin into id" in instruction
