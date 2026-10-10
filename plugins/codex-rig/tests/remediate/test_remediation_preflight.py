"""Prevent audited remediation contract failures before prompting or editing source."""

import json
from functools import partialmethod
from pathlib import Path

import pytest
from test_code_remediate_work_bucket_validation import VALIDATOR, _parallel_metadata, _write_workplan
from test_finding_presentation import _selection
from test_remediation_finalize import HELPER, _stale_metadata
from test_remediation_finalize import valid_run as _shared_valid_run

_valid_run = pytest.fixture(name="valid_run")(_shared_valid_run.__wrapped__)


@pytest.fixture
def legacy_text_encoding(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise UTF-8 artifacts with the legacy Windows text default on every host."""
    monkeypatch.setattr(Path, "read_text", partialmethod(Path.read_text, encoding="cp1252"))
    monkeypatch.setattr(Path, "write_text", partialmethod(Path.write_text, encoding="cp1252"))


def _small_sequential_plan(run: Path) -> dict:
    """Write two dependent role buckets with truthful capacity and dispatch evidence."""
    metadata = _parallel_metadata()
    metadata["resolution_scope"]["selected_indexes"] = [1, 2]
    plan = metadata["resolution_workplan"]
    plan.update(
        execution_mode="sequential-specialists",
        parallel_eligible=False,
        parallel_approval_required=False,
        parallel_approval_source="workflow-default",
        parallel_approval_status="parent-only",
    )
    for i, bucket in enumerate(plan["work_buckets"], 1):
        bucket.update(
            selected_indexes=[i],
            execution_mode="sequential",
            singleton_rationale="Distinct capable role and closure evidence.",
            grouping_rationale=f"Own {bucket['owned_paths'][0]} closure.",
            expected_closure=f"Verify {bucket['owned_paths'][0]}.",
            dependencies=[] if i == 1 else ["B1"],
        )
    _write_workplan(metadata, run)
    doc = run / "resolution-workplan.md"
    doc.write_text(
        doc.read_text(encoding="utf-8").replace(
            "## Parallel Approval\n",
            "## Parallel Approval\n\nIneligibility reason: Documentation depends on the source fix.\n",
        ),
        encoding="utf-8",
        newline="\n",
    )
    return metadata


def test_small_sequential_distinct_roles_pass_before_edit(tmp_path: Path) -> None:
    """Accept real dependency order instead of requiring dishonest parallel ownership."""
    metadata = _small_sequential_plan(tmp_path)
    VALIDATOR._validate_code_remediate_workplan(metadata, tmp_path)


@pytest.mark.parametrize("damage", ["same-owner", "forward-dependency", "missing-index"])
def test_small_plan_rejects_artificial_or_invalid_split(tmp_path: Path, damage: str) -> None:
    """Keep concrete ownership, dependency, and selection coverage guards."""
    metadata = _small_sequential_plan(tmp_path)
    plan = metadata["resolution_workplan"]
    if damage == "same-owner":
        plan["work_buckets"][1]["owner"] = plan["work_buckets"][0]["owner"]
    elif damage == "forward-dependency":
        plan["work_buckets"][0]["dependencies"] = ["B2"]
    else:
        metadata["resolution_scope"]["selected_indexes"].append(3)
    observed = VALIDATOR._validate_work_buckets(plan["work_buckets"], tmp_path)
    with pytest.raises(SystemExit, match="code-remediate-"):
        VALIDATOR._validate_workplan_coverage_counts(
            plan, plan["work_buckets"], metadata["resolution_scope"]["selected_indexes"], observed
        )


@pytest.mark.parametrize("counts", ["stale", "missing"])
def test_finalizer_derives_counts_from_actual_rows(valid_run: tuple[Path, dict], counts: str) -> None:
    """Ignore hand-entered counts while preserving independently validated outcomes."""
    run, result = valid_run
    draft = _stale_metadata(result)
    table = draft["final_resolution_table"]
    for field in ("triage_status_counts", "resolution_status_counts"):
        if counts == "missing":
            table.pop(field)
        else:
            table[field] = {key: 99 for key in table[field]}
    derived = HELPER.derive_metadata(run, draft)
    for field in ("triage_status_counts", "resolution_status_counts"):
        assert derived["final_resolution_table"][field] == result["metadata"]["final_resolution_table"][field]
    derived["final_resolution_table"]["triage_status_counts"]["valid"] += 1
    with pytest.raises(SystemExit, match="code-remediate-triage-status-counts-total-mismatch"):
        VALIDATOR._validate_code_remediate_final_resolution_table(derived, run)


def test_report_occurrences_preserved_before_scope(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Require each exact field/index occurrence even when the same text appears twice."""
    report = {
        "schema_version": 3,
        "artifact_path": ".reports/codex/code-review/pr-1/run-001/result.json",
        "checks_failed": ["types"],
        "follow_up": ["Add mixed-input regression.", "Add mixed-input regression."],
        "metadata": {
            "review_status": "assessed",
            "confidence_gaps": ["Independent coverage open."],
            "review_decision": {"required_next_work": ["Run the type gate."]},
            "confidence_recovery": {"remaining_limits": ["Missing stubs."]},
        },
    }
    (tmp_path / "findings-input.txt").write_text(json.dumps(report), encoding="utf-8", newline="\n")
    assert HELPER.main(["obligations", "--run", str(tmp_path)]) == 0
    capsys.readouterr()
    sources = json.loads((tmp_path / "report-obligations.json").read_text(encoding="utf-8"))["sources"]
    assert len(sources) == 6
    assert sources[1]["source_id"].endswith("#/follow_up/0")
    assert sources[2]["source_id"].endswith("#/follow_up/1")
    assert sources[1]["body"] == sources[2]["body"] == report["follow_up"][0]
    inventory = _selection()
    inventory["items"][0]["sources"].extend(sources[0:2] + sources[3:])
    selection = tmp_path / "selection.json"
    selection.write_text(json.dumps(inventory), encoding="utf-8", newline="\n")
    argv = ["preflight", "--run", str(tmp_path), "--stage", "selection", "--report"]
    assert HELPER.main(argv) == 1
    assert "report-obligation-omitted" in capsys.readouterr().out
    assert not (tmp_path / "resolution-scope.md").exists()
    inventory["items"][0]["sources"].append(sources[2])
    selection.write_text(json.dumps(inventory), encoding="utf-8", newline="\n")
    assert HELPER.main(argv) == 0
    assert inventory["selected_indexes"] is None
    assert HELPER._report_finding_ids(inventory["items"][0]) == ["F7"]
    report["follow_up"][1] = "Changed producer obligation."
    (tmp_path / "findings-input.txt").write_text(json.dumps(report), encoding="utf-8", newline="\n")
    assert HELPER.main(argv) == 1
    assert "report-obligation" in capsys.readouterr().out


def test_public_plan_checkpoint_needs_no_completed_outcomes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Check the real plan route before edits, not a final validator requiring finished runtime evidence."""
    _small_sequential_plan(tmp_path)
    inventory = _selection()
    inventory["selected_indexes"] = [1, 2]
    (tmp_path / "selection.json").write_text(json.dumps(inventory), encoding="utf-8", newline="\n")
    draft = tmp_path / "planning.json"
    draft.write_text(
        json.dumps({"resolution_workplan": {"parallel_eligible": False, "parallel_approval_required": False}}),
        encoding="utf-8",
        newline="\n",
    )
    assert (
        HELPER.main(
            [
                "workplan",
                "--run",
                str(tmp_path),
                "--metadata",
                str(draft),
                "--ineligibility-reason",
                "Documentation depends on source closure.",
            ]
        )
        == 0
    )
    argv = ["preflight", "--run", str(tmp_path), "--stage", "plan", "--metadata", str(draft)]
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert HELPER.main(argv) == 0
    assert {p: p.read_bytes() for p in before} == before
    inventory["selected_indexes"] = [1]
    (tmp_path / "selection.json").write_text(json.dumps(inventory), encoding="utf-8", newline="\n")
    assert HELPER.main(argv) == 1
    assert "work-bucket-coverage-mismatch" in capsys.readouterr().out


def test_counts_follow_latest_status_event(valid_run: tuple[Path, dict]) -> None:
    """Count folded outcomes rather than the earlier valid/implemented draft."""
    run, result = valid_run
    (run / "resolution-events.jsonl.rec").write_text(
        json.dumps(
            {"kind": "item", "id": "R1", "triage_status": "already-applied", "resolution_status": "already-applied"}
        ),
        encoding="utf-8",
        newline="\n",
    )
    HELPER.append_record(run, "resolution-events.jsonl")
    table = HELPER.derive_metadata(run, result["metadata"])["final_resolution_table"]
    assert table["triage_status_counts"]["valid"] == 0
    assert table["triage_status_counts"]["already-applied"] == 1
    assert table["resolution_status_counts"]["implemented"] == 0
    assert table["resolution_status_counts"]["already-applied"] == 1


@pytest.mark.parametrize("bad_status", [None, "", "invented"])
def test_missing_or_invalid_outcome_never_gets_default_counts(
    valid_run: tuple[Path, dict], bad_status: str | None
) -> None:
    """Reject incomplete final judgment rather than silently counting it as valid or resolved."""
    run, result = valid_run
    result["metadata"]["final_resolution_table"]["items"][0]["triage_status"] = bad_status
    with pytest.raises(HELPER.DeriveError, match="item-triage_status-invalid"):
        HELPER.derive_metadata(run, result["metadata"])


@pytest.mark.usefixtures("legacy_text_encoding")
def test_preflight_contract_precedes_question_and_edits() -> None:
    """Keep executable early gates at the skill boundaries that consume their evidence."""
    skill = (HELPER.SHARED_DIRECTORY.parent / "skills/code-remediate/SKILL.md").read_text(encoding="utf-8")
    assert skill.index("--stage selection") < skill.index("### Terminal Scope Context Contract")
    assert skill.index("--stage plan") < skill.index("### 07:")
    assert "Final status counts derive" in (HELPER.SHARED_DIRECTORY.parent / "README.md").read_text(encoding="utf-8")


@pytest.mark.integration
@pytest.mark.usefixtures("legacy_text_encoding")
def test_sequential_report_followup_reaches_truthful_failed_result(
    valid_run: tuple[Path, dict], capsys: pytest.CaptureFixture[str]
) -> None:
    """Exercise intake, planning, derived counts and promotion with an unresolved type prerequisite."""
    run, result = valid_run
    metadata = result["metadata"]
    producer = Path(metadata["review_report_intake"]["admission_evidence"]["producer_result_path"])
    report = json.loads(producer.read_bytes())
    report["follow_up"] = ["Retain the mixed-input closure regression."]
    producer.write_text(json.dumps(report), encoding="utf-8", newline="\n")
    (run / "findings-input.txt").write_bytes(producer.read_bytes())
    assert HELPER.main(["obligations", "--run", str(run)]) == 0
    sources = json.loads((run / "report-obligations.json").read_text(encoding="utf-8"))["sources"]
    table = metadata["final_resolution_table"]
    table["items"][0]["sources"].append(next(source for source in sources if "#/follow_up/" in source["source_id"]))
    table["items"][1].update(
        selectable=True,
        item_type="confidence-gap",
        triage_status="valid",
        resolution_status="unresolved",
        owner_status="unresolved",
        resolved_how="Missing dependency stubs; environment owner must supply them.",
        evidence="checks/types.stderr.txt",
    )
    table.update(selectable_rows_total=2, nonselectable_rows_total=0)
    total = sum(len(item["sources"]) for item in table["items"])
    table.update(source_records_total=total, represented_source_records_total=total)
    selection = json.loads((run / "selection.json").read_text(encoding="utf-8"))
    for item, original in zip(selection["items"], table["items"]):
        for field in HELPER.IDENTITY_FIELDS:
            item[field] = original[field]
    selection["selected_indexes"] = None
    (run / "selection.json").write_text(json.dumps(selection), encoding="utf-8", newline="\n")
    assert HELPER.main(["preflight", "--run", str(run), "--stage", "selection", "--report"]) == 0
    selection["selected_indexes"] = [1, 2]
    (run / "selection.json").write_text(json.dumps(selection), encoding="utf-8", newline="\n")
    (run / "resolution-scope.md").write_text(
        HELPER.final_handoff.render_selection(selection), encoding="utf-8", newline="\n"
    )
    metadata["resolution_scope"].update(selected_indexes=[1, 2], deferred_indexes=[])
    metadata["review_report_intake"].update(review_gate_items_total=1, review_gate_items_selectable=1)
    (run / "specialists").rmdir()
    plan = _small_sequential_plan(run)
    metadata["resolution_workplan"] = plan["resolution_workplan"]
    draft = run / "draft.json"
    draft.write_text(json.dumps(metadata), encoding="utf-8", newline="\n")
    assert HELPER.main(["preflight", "--run", str(run), "--stage", "plan", "--metadata", str(draft)]) == 0
    metadata["unresolved_summary"].update(
        selected_items_total=2,
        selected_items_resolved=1,
        selected_items_unresolved=1,
        environment_blocked_items=1,
        unresolved_reason_groups=[
            {
                "reason": "environment-blocked",
                "count": 1,
                "owner": "environment",
                "next_action": "Supply dependency stubs and rerun types.",
                "evidence_path": "checks/types.stderr.txt",
            }
        ],
    )
    (run / "unresolved.txt").write_text(
        "## Unresolved Work Summary\nClosure class: environment-blocked\n"
        "## Why Selected Items Remain Unresolved\nAttempted evidence: checks/types.stderr.txt\n"
        "## Next Action\nNext owner: environment. Supply dependency stubs and rerun types.\n",
        encoding="utf-8",
        newline="\n",
    )
    gates = json.loads((run / "gates.json").read_text(encoding="utf-8"))
    gates.update(status="fail", checks_failed=["types"], failed_count=1)
    gates["checks_not_applicable"] = [name for name in gates.get("checks_not_applicable", []) if name != "types"]
    gate = next(check for check in gates["checks"] if check["id"] == "types")
    gate.update(status="fail", exit_code=1)
    gate.pop("reason", None)
    (run / gate["stderr"]).write_text("Missing dependency stubs.\n", encoding="utf-8", newline="\n")
    (run / "gates.json").write_text(json.dumps(gates), encoding="utf-8", newline="\n")
    handoff = json.loads((run / "final-handoff.json").read_text(encoding="utf-8"))
    handoff.update(
        outcome={"title": "fail", "summary": "Source fix verified; type prerequisite unresolved."},
        remaining=[
            {
                "row_id": "R2",
                "item": "Missing stubs",
                "owner": "environment",
                "next_action": "Supply dependency stubs and rerun types.",
            }
        ],
        next_steps=["R2"],
    )
    handoff["commit_disposition"] = {
        "status": "blocked",
        "reason": "Type gate failed.",
        "evidence": "checks/types.stderr.txt",
    }
    (run / "handoff-draft.json").write_text(json.dumps(handoff), encoding="utf-8", newline="\n")
    HELPER.render_ledger(run, metadata)
    draft.write_text(json.dumps(metadata), encoding="utf-8", newline="\n")
    assert (
        HELPER.main(
            [
                "finalize",
                "--run",
                str(run),
                "--metadata",
                str(draft),
                "--handoff",
                str(run / "handoff-draft.json"),
                "--status",
                "fail",
                "--confidence",
                str(result["confidence"]),
                "--artifact-path",
                str(run / "result.json"),
                "--promote",
            ]
        )
        == 0
    ), capsys.readouterr().out
    promoted = json.loads((run / "result.json").read_text(encoding="utf-8"))
    assert promoted["status"] == "fail"
    assert promoted["checks_failed"] == ["types"]
    assert promoted["metadata"]["final_resolution_table"]["triage_status_counts"]["valid"] == 2
    assert promoted["metadata"]["unresolved_summary"]["environment_blocked_items"] == 1
    assert "Supply dependency stubs and rerun types." in (run / "final.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("generation", ["historical-promoted", "new-candidate", "new-promoted"])
def test_occurrence_contract_preserves_historical_intake(valid_run: tuple[Path, dict], generation: str) -> None:
    """Keep old body-backed provenance while new candidates and promoted outputs require occurrences."""
    run, result = valid_run
    intake = result["metadata"]["review_report_intake"]
    for item in result["metadata"]["final_resolution_table"]["items"]:
        item["sources"] = [source for source in item["sources"] if "#/" not in source["source_id"]]
    intake.pop("obligation_records_version", None)
    before = {path: path.read_bytes() for path in run.rglob("*") if path.is_file()}
    if generation == "historical-promoted":
        VALIDATOR._validate_code_remediate_report_intake(result, run, current_contract=True)
    else:
        if generation == "new-promoted":
            intake["obligation_records_version"] = 1
        with pytest.raises(SystemExit, match="code-remediate-report-obligation"):
            VALIDATOR._validate_code_remediate_report_intake(
                result, run, current_contract=False, candidate=generation == "new-candidate"
            )
    assert {path: path.read_bytes() for path in before} == before
