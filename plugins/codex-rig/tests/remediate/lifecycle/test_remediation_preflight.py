"""Prevent audited remediation contract failures before prompting or editing source."""

import json
from collections.abc import Callable
from functools import partialmethod
from pathlib import Path

import pytest
from _skill_text import read_with_references
from test_code_remediate_work_bucket_validation import VALIDATOR, _parallel_metadata, _write_workplan
from test_finding_presentation import _selection
from test_remediation_finalize import HELPER, _stale_metadata

_USER_SOURCE = {"kind": "user", "source_id": "user-request#finding-1"}


def _add_proposals(inventory: dict) -> None:
    """Give every inventory item the same resolution proposal."""
    for item in inventory["items"]:
        item["resolution_proposal"] = "Verify the input contract and retain a regression."


def _current_inventory() -> dict:
    """Return a valid current-presentation (version 5) selection with proposals and a user-sourced finding."""
    inventory = _selection()
    inventory["presentation_version"] = 5
    _add_proposals(inventory)
    inventory["items"][1]["sources"][0].update(_USER_SOURCE)
    return inventory


def _missing_proposal(inventory: dict) -> None:
    """Remove a resolution proposal."""
    del inventory["items"][0]["resolution_proposal"]


def _malformed_user_source(inventory: dict) -> None:
    """Give the user source an id that is not a user finding."""
    inventory["items"][1]["sources"][0]["source_id"] = "not-a-user-finding"


def _duplicate_source_entry(inventory: dict) -> None:
    """Repeat a source inside its item."""
    sources = inventory["items"][1]["sources"]
    sources.append(dict(sources[0]))


def _wrong_source_count(inventory: dict) -> None:
    """Claim a source total that does not match the items."""
    inventory["source_records_total"] = 99


def _unknown_schema(inventory: dict) -> None:
    """Use an unknown schema version."""
    inventory["schema_version"] = 2


def _unknown_presentation(inventory: dict) -> None:
    """Use a presentation version newer than any supported."""
    inventory["presentation_version"] = 6


def _invalid_selected_index(inventory: dict) -> None:
    """Select an index outside the inventory."""
    inventory["selected_indexes"] = [3]


class TestSelectionPreflight:
    """Protect the selection-stage gate that runs before the user is asked anything."""

    @pytest.mark.parametrize(
        ("presentation", "source_update"),
        [
            pytest.param(2, {}, id="v2"),
            pytest.param(3, {}, id="v3"),
            pytest.param(4, _USER_SOURCE, id="v4-user-source"),
            pytest.param(5, _USER_SOURCE, id="v5-user-source"),
        ],
    )
    def test_accepts_supported_presentations(
        self, tmp_path: Path, presentation: int, source_update: dict, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Admit the same valid inventory through the public preflight and renderer without migration.

        Presentations 2 to 5 are all still supported; preflight passes, the renderer shows the awaiting-selection view,
        and the inventory file is never rewritten.
        """
        inventory = _selection()
        inventory["presentation_version"] = presentation
        _add_proposals(inventory)
        inventory["items"][1]["sources"][0].update(source_update)
        selection = tmp_path / "selection.json"
        original = json.dumps(inventory).encode("utf-8")
        selection.write_bytes(original)

        assert HELPER.main(["preflight", "--run", str(tmp_path), "--stage", "selection"]) == 0

        assert json.loads(capsys.readouterr().out)["status"] == "pass"
        rendered = HELPER.final_handoff.render_selection(inventory)
        assert "Awaiting selection" in rendered
        assert "Preserve input compatibility" in rendered
        assert selection.read_bytes() == original

    @pytest.mark.parametrize(
        ("damage", "error"),
        [
            pytest.param(_missing_proposal, "selection-item-resolution_proposal", id="missing-proposal"),
            pytest.param(_malformed_user_source, "selection-user-source-invalid", id="malformed-user-source"),
            pytest.param(_duplicate_source_entry, "selection-source-duplicate", id="duplicate-source"),
            pytest.param(_wrong_source_count, "selection-count-mismatch", id="incorrect-count"),
            pytest.param(_unknown_schema, "selection-schema-invalid", id="unknown-schema"),
            pytest.param(_unknown_presentation, "selection-presentation-version-invalid", id="unknown-presentation"),
            pytest.param(_invalid_selected_index, "selection-index-invalid", id="invalid-selection"),
        ],
    )
    def test_current_presentation_keeps_evidence_guards(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], damage: Callable[[dict], None], error: str
    ) -> None:
        """Accepting the current presentation must still reject concrete invalid input before prompting.

        Each case damages one fact of an otherwise valid version-5 inventory; preflight must exit 1 with that error
        code, leave the inventory bytes alone and render no scope document.
        """
        inventory = _current_inventory()
        damage(inventory)
        selection = tmp_path / "selection.json"
        original = json.dumps(inventory).encode("utf-8")
        selection.write_bytes(original)

        assert HELPER.main(["preflight", "--run", str(tmp_path), "--stage", "selection"]) == 1

        assert error in json.loads(capsys.readouterr().out)["code"]
        assert selection.read_bytes() == original
        assert not (tmp_path / "resolution-scope.md").exists()


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


def _planning_draft(run: Path) -> Path:
    """Write the minimal planning draft that declares a sequential, non-parallel workplan."""
    draft = run / "planning.json"
    draft.write_text(
        json.dumps({"resolution_workplan": {"parallel_eligible": False, "parallel_approval_required": False}}),
        encoding="utf-8",
        newline="\n",
    )
    return draft


def _run_workplan(run: Path, draft: Path) -> int:
    """Run the public ``workplan`` command with the documented ineligibility reason and return its exit code."""
    argv = ["workplan", "--run", str(run), "--metadata", str(draft)]
    return HELPER.main([*argv, "--ineligibility-reason", "Documentation depends on source closure."])


def _copy_identity(items: list[dict], originals: list[dict]) -> None:
    """Copy each original's identity fields onto the matching selection item."""
    for item, original in zip(items, originals):
        for field in HELPER.IDENTITY_FIELDS:
            item[field] = original[field]


def _same_owner(metadata: dict) -> None:
    """Give both buckets the same owner."""
    buckets = metadata["resolution_workplan"]["work_buckets"]
    buckets[1]["owner"] = buckets[0]["owner"]


def _forward_dependency(metadata: dict) -> None:
    """Make the first bucket depend on the second."""
    metadata["resolution_workplan"]["work_buckets"][0]["dependencies"] = ["B2"]


def _unplanned_index(metadata: dict) -> None:
    """Select an index no bucket covers."""
    metadata["resolution_scope"]["selected_indexes"].append(3)


_COUNT_FIELDS = ("triage_status_counts", "resolution_status_counts")


def _drop_counts(table: dict) -> None:
    """Remove the hand-entered status counts."""
    for field in _COUNT_FIELDS:
        table.pop(field)


def _stale_counts(table: dict) -> None:
    """Replace every hand-entered status count with 99."""
    for field in _COUNT_FIELDS:
        table[field] = {key: 99 for key in table[field]}


class TestSequentialPlan:
    """Protect the plan-stage gate for small sequential workplans."""

    def test_accepts_distinct_roles_before_edit(self, tmp_path: Path) -> None:
        """Accept real dependency order instead of requiring dishonest parallel ownership.

        Two buckets with distinct roles, truthful capacity evidence and a second bucket depending on the first form a
        valid sequential plan; the workplan validator must accept it without demanding a parallel split.
        """
        metadata = _small_sequential_plan(tmp_path)
        VALIDATOR._validate_code_remediate_workplan(metadata, tmp_path)

    @pytest.mark.parametrize(
        "damage",
        [
            pytest.param(_same_owner, id="same-owner"),
            pytest.param(_forward_dependency, id="forward-dependency"),
            pytest.param(_unplanned_index, id="missing-index"),
        ],
    )
    def test_rejects_artificial_or_invalid_split(self, tmp_path: Path, damage: Callable[[dict], None]) -> None:
        """Keep concrete ownership, dependency, and selection coverage guards.

        Starting from the valid two-bucket sequential plan, one split defect is introduced; coverage validation must
        reject it with a ``code-remediate-`` error.
        """
        metadata = _small_sequential_plan(tmp_path)
        plan = metadata["resolution_workplan"]
        damage(metadata)
        observed = VALIDATOR._validate_work_buckets(plan["work_buckets"], tmp_path)
        with pytest.raises(SystemExit, match="code-remediate-"):
            VALIDATOR._validate_workplan_coverage_counts(
                plan, plan["work_buckets"], metadata["resolution_scope"]["selected_indexes"], observed
            )

    def test_checkpoint_needs_no_completed_outcomes(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Check the real plan route before edits, not a final validator requiring finished runtime evidence.

        A planned run has a rendered workplan but no completed outcomes; plan-stage preflight must pass without touching
        any run file, and must fail with a coverage mismatch once the selection drops a bucket's item.
        """
        _small_sequential_plan(tmp_path)
        inventory = _selection()
        inventory["selected_indexes"] = [1, 2]
        (tmp_path / "selection.json").write_text(json.dumps(inventory), encoding="utf-8", newline="\n")
        draft = _planning_draft(tmp_path)
        assert _run_workplan(tmp_path, draft) == 0
        argv = ["preflight", "--run", str(tmp_path), "--stage", "plan", "--metadata", str(draft)]
        before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
        assert HELPER.main(argv) == 0
        assert {p: p.read_bytes() for p in before} == before
        inventory["selected_indexes"] = [1]
        (tmp_path / "selection.json").write_text(json.dumps(inventory), encoding="utf-8", newline="\n")
        assert HELPER.main(argv) == 1
        assert "work-bucket-coverage-mismatch" in capsys.readouterr().out


class TestDerivedCounts:
    """Protect final status counts derived from the actual rows and events."""

    @pytest.mark.parametrize(
        "damage", [pytest.param(_stale_counts, id="stale"), pytest.param(_drop_counts, id="missing")]
    )
    def test_derives_counts_from_actual_rows(
        self, valid_run: tuple[Path, dict], damage: Callable[[dict], None]
    ) -> None:
        """Ignore hand-entered counts while preserving independently validated outcomes.

        The draft's status counts are stale or absent; derivation must rebuild them from the actual rows, and a
        validator run on a tampered total must still refuse.
        """
        run, result = valid_run
        draft = _stale_metadata(result)
        damage(draft["final_resolution_table"])

        derived = HELPER.derive_metadata(run, draft)

        expected = result["metadata"]["final_resolution_table"]
        assert {f: derived["final_resolution_table"][f] for f in _COUNT_FIELDS} == {
            f: expected[f] for f in _COUNT_FIELDS
        }
        derived["final_resolution_table"]["triage_status_counts"]["valid"] += 1
        with pytest.raises(SystemExit, match="code-remediate-triage-status-counts-total-mismatch"):
            VALIDATOR._validate_code_remediate_final_resolution_table(derived, run)

    def test_follow_latest_status_event(self, valid_run: tuple[Path, dict]) -> None:
        """Count folded outcomes rather than the earlier valid/implemented draft.

        The draft marks the first item valid and implemented, then a later event records it as already applied; derived
        counts must move the item from the old statuses to the new ones.
        """
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
    def test_missing_or_invalid_outcome_gets_no_default_counts(
        self, valid_run: tuple[Path, dict], bad_status: str | None
    ) -> None:
        """Reject incomplete final judgment rather than silently counting it as valid or resolved.

        An item's triage status is null, empty or invented; derivation must raise an invalid-status error instead of
        defaulting the item into a count.
        """
        run, result = valid_run
        result["metadata"]["final_resolution_table"]["items"][0]["triage_status"] = bad_status
        with pytest.raises(HELPER.DeriveError, match="item-triage_status-invalid"):
            HELPER.derive_metadata(run, result["metadata"])


@pytest.mark.usefixtures("legacy_text_encoding")
def test_preflight_contract_precedes_question_and_edits() -> None:
    """Keep executable early gates at the skill boundaries that consume their evidence.

    The skill text must run the selection preflight before the scope-context contract and the plan preflight before
    implementation, and the README must describe derived counts. Read under a legacy text encoding to prove the
    documents stay decodable on Windows defaults. This pins skill ordering, so drift means wording moved.
    """
    skill = read_with_references(
        HELPER.SHARED_DIRECTORY.parent / "skills/code-remediate/SKILL.md",
        "intake.md",
        "selection.md",
        "implementation.md",
        "completion.md",
    )
    assert skill.index("--stage selection") < skill.index("### Terminal Scope Context Contract")
    assert skill.index("--stage plan") < skill.index("### 07:")
    assert "Final status counts derive" in (HELPER.SHARED_DIRECTORY.parent / "README.md").read_text(encoding="utf-8")


@pytest.mark.integration
@pytest.mark.usefixtures("legacy_text_encoding")
def test_sequential_report_followup_reaches_truthful_failed_result(
    valid_run: tuple[Path, dict], capsys: pytest.CaptureFixture[str]
) -> None:
    """Exercise intake, planning, derived counts and promotion with an unresolved type prerequisite.

    A valid run gains a report follow-up and a second, environment-blocked item whose type gate fails; obligations,
    selection and plan preflight, and finalize must carry it through to a promoted failing result with derived counts
    and the owner's next action. Every phase feeds the next, so this one lifecycle stays a single integration test.
    """
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
    _copy_identity(selection["items"], table["items"])
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


def _historical_intake(run: Path, result: dict[str, object]) -> dict[Path, bytes]:
    """Reduce a valid run to body-backed historical intake and snapshot every file in the run."""
    for item in result["metadata"]["final_resolution_table"]["items"]:
        item["sources"] = [source for source in item["sources"] if "#/" not in source["source_id"]]
    result["metadata"]["review_report_intake"].pop("obligation_records_version", None)
    return {path: path.read_bytes() for path in run.rglob("*") if path.is_file()}


class TestOccurrenceContract:
    """Protect occurrence-backed intake for new reports and history for old ones."""

    def test_accepts_historical_promoted_intake(self, valid_run: tuple[Path, dict]) -> None:
        """Keep old body-backed provenance valid for a promoted result under the current contract.

        The report intake predates occurrence records; validating it must succeed and leave every run file unchanged.
        """
        run, result = valid_run
        before = _historical_intake(run, result)

        VALIDATOR._validate_code_remediate_report_intake(result, run, current_contract=True)

        assert {path: path.read_bytes() for path in before} == before

    @pytest.mark.parametrize(
        ("intake_update", "candidate"),
        [
            pytest.param({}, True, id="new-candidate"),
            pytest.param({"obligation_records_version": 1}, False, id="new-promoted"),
        ],
    )
    def test_requires_occurrences_for_new_intake(
        self, valid_run: tuple[Path, dict], intake_update: dict, candidate: bool
    ) -> None:
        """New candidates and promoted outputs need occurrence-backed intake, never body-only provenance.

        Without per-occurrence sources the validator must reject, and the run files must stay untouched by the check.
        """
        run, result = valid_run
        before = _historical_intake(run, result)
        result["metadata"]["review_report_intake"].update(intake_update)

        with pytest.raises(SystemExit, match="code-remediate-report-obligation"):
            VALIDATOR._validate_code_remediate_report_intake(result, run, current_contract=False, candidate=candidate)

        assert {path: path.read_bytes() for path in before} == before


def _duplicate_blocker(report: dict, record: dict) -> None:
    """Report the same record as a finding and as a blocker."""
    report["metadata"]["operational_blockers"] = [record.copy()]


def _without_producer(report: dict, record: dict) -> None:
    """Remove the producing artifact path."""
    del report["artifact_path"]


def _invalid_id(report: dict, record: dict) -> None:
    """Give the finding an identity with a space."""
    record["id"] = "invalid id"


def _empty_evidence(report: dict, record: dict) -> None:
    """Remove the finding evidence."""
    record["evidence"] = []


def _empty_title(report: dict, record: dict) -> None:
    """Blank the finding title."""
    record["title"] = ""


class TestReportObligations:
    """Protect review-report obligations carried into the remediation selection."""

    def test_occurrences_preserved_before_scope(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Require each exact field/index occurrence even when the same text appears twice.

        A review report repeats one follow-up string at two indexes; the obligations file keeps both occurrences,
        selection preflight and historical coverage reject an inventory omitting one, accept it once added, and a
        changed producer text is rejected again. The phases are one coverage lifecycle, so they stay in a single test.
        """
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
        assert HELPER.main(argv) == 0
        historical = {"final_resolution_table": {"items": inventory["items"]}}
        with pytest.raises(SystemExit, match="report-obligation-omitted"):
            VALIDATOR._validate_code_remediate_report_coverage(historical, tmp_path, occurrence_contract=True)
        assert not (tmp_path / "resolution-scope.md").exists()
        inventory["items"][0]["sources"].append(sources[2])
        selection.write_text(json.dumps(inventory), encoding="utf-8", newline="\n")
        assert HELPER.main(argv) == 0
        VALIDATOR._validate_code_remediate_report_coverage(historical, tmp_path, occurrence_contract=True)
        assert inventory["selected_indexes"] is None
        assert HELPER._report_finding_ids(inventory["items"][0]) == ["F7"]
        report["follow_up"][1] = "Changed producer obligation."
        (tmp_path / "findings-input.txt").write_text(json.dumps(report), encoding="utf-8", newline="\n")
        with pytest.raises(SystemExit, match="report-obligation"):
            VALIDATOR._validate_code_remediate_report_coverage(historical, tmp_path, occurrence_contract=True)

    def test_clean_review_yields_no_obligations(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Admit a clean current review report whose optional fields use null and empty-list forms.

        A passing review leaves no follow-up, findings or recovery limits; ``obligations`` must exit 0 and write an
        empty source list rather than failing on the null and empty shapes.
        """
        report = {
            "schema_version": 3,
            "artifact_path": ".reports/codex/code-review/pr-1/run-001/result.json",
            "checks_failed": [],
            "follow_up": [],
            "metadata": {
                "review_status": None,
                "review_decision": {"required_next_work": None},
                "review_validation": {"schema_version": 1, "candidates": []},
                "source_findings": [],
                "source_finding_mapping": [],
                "confidence_recovery": {"remaining_limits": []},
            },
        }
        (tmp_path / "findings-input.txt").write_text(json.dumps(report), encoding="utf-8", newline="\n")

        assert HELPER.main(["obligations", "--run", str(tmp_path)]) == 0
        capsys.readouterr()
        assert json.loads((tmp_path / "report-obligations.json").read_text(encoding="utf-8")) == {
            "schema_version": 1,
            "sources": [],
        }

    def test_review_context_does_not_require_action_items(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Keep repeated reviewer limitations out of selection while retaining their original evidence.

        A report carries only confidence limits and a generic follow-up; preflight passes without action items, leaves
        the input and inventory bytes alone, and never renders the follow-up. Adding a real review finding must then
        fail with an omitted-finding error so context cannot hide a defect.
        """
        report = {
            "schema_version": 3,
            "artifact_path": "review/result.json",
            "checks_failed": ["independent-coverage"],
            "follow_up": ["Consider a further review."],
            "metadata": {
                "review_status": "assessed",
                "confidence_gaps": ["Independent coverage unavailable."],
                "confidence_recovery": {"remaining_limits": ["Independent coverage unavailable."]},
            },
        }
        original = json.dumps(report).encode()
        (tmp_path / "findings-input.txt").write_bytes(original)
        inventory = _selection()
        (tmp_path / "selection.json").write_text(json.dumps(inventory), encoding="utf-8")
        assert HELPER.main(["preflight", "--run", str(tmp_path), "--stage", "selection", "--report"]) == 0
        assert (tmp_path / "findings-input.txt").read_bytes() == original
        assert json.loads((tmp_path / "selection.json").read_bytes()) == inventory
        assert "Consider a further review" not in HELPER.final_handoff.render_selection(inventory)

        # Context must not let a real source defect disappear from the same consumer.
        report["metadata"]["review_findings"] = [{"id": "BUG", "title": "Invalid input accepted"}]
        (tmp_path / "findings-input.txt").write_text(json.dumps(report), encoding="utf-8")
        assert HELPER.main(["preflight", "--run", str(tmp_path), "--stage", "selection", "--report"]) == 1
        assert "report-finding-omitted:BUG" in capsys.readouterr().out

    def test_context_intake_rejects_lost_or_changed_evidence(self, tmp_path: Path) -> None:
        """Separating context from tasks must not silently discard or rewrite the original caveats.

        Intake records a confidence gap as a context source; the coverage check accepts it unchanged and must raise a
        context-mismatch error once the recorded body is edited.
        """
        report = {
            "schema_version": 3,
            "artifact_path": "review/result.json",
            "metadata": {
                "confidence_gaps": ["Optional platform untested."],
            },
        }
        (tmp_path / "findings-input.txt").write_text(json.dumps(report), encoding="utf-8")
        sources = VALIDATOR.report_obligation_sources(report)
        metadata = {
            "final_resolution_table": {"items": []},
            "review_report_intake": {"obligation_records_version": 2, "context_sources": sources},
        }
        VALIDATOR._validate_code_remediate_report_coverage(metadata, tmp_path)
        sources[0]["body"] = "Everything tested."
        with pytest.raises(SystemExit, match="report-context-mismatch"):
            VALIDATOR._validate_code_remediate_report_coverage(metadata, tmp_path)

    def test_historical_process_finding_can_be_reclassified_as_limitation(self, tmp_path: Path) -> None:
        """Retain the historical record while removing its rejected extra-review request from selection.

        A report lists a process blocker; the inventory declares it a review limitation with a rationale, so preflight
        passes and the rendered scope omits it. Pointing the limitation at an unknown finding must fail preflight.
        """
        report = {
            "schema_version": 3,
            "artifact_path": "review/result.json",
            "metadata": {
                "operational_blockers": [{"id": "PROCESS", "title": "Reviewer unavailable"}],
            },
        }
        (tmp_path / "findings-input.txt").write_text(json.dumps(report), encoding="utf-8")
        inventory = _selection()
        inventory["review_limitations"] = [
            {
                "finding_id": "PROCESS",
                "rationale": "Reviewer coverage unavailable; user declined another pass.",
            }
        ]
        (tmp_path / "selection.json").write_text(json.dumps(inventory), encoding="utf-8")
        argv = ["preflight", "--run", str(tmp_path), "--stage", "selection", "--report"]
        assert HELPER.main(argv) == 0
        rendered = HELPER.final_handoff.render_selection(inventory)
        assert "PROCESS" not in rendered
        assert "Reviewer unavailable" not in rendered
        inventory["review_limitations"][0]["finding_id"] = "UNKNOWN"
        (tmp_path / "selection.json").write_text(json.dumps(inventory), encoding="utf-8")
        assert HELPER.main(argv) == 1

    def test_exposes_complete_finding_for_selection(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Preserve every canonical finding field when building the selection source.

        A review finding with Unicode, a multi-line summary and several evidence entries is read by ``obligations``; the
        source body must contain every field, and an inventory item built from that source must pass selection
        preflight.
        """
        record = {
            "id": "F1",
            "title": "Unicode é title",
            "summary": "First line\nSecond line",
            "required_change": "Fix it",
            "closure_evidence": "Regression passes",
            "evidence": ["a.py:1", "review.md"],
        }
        report = {
            "schema_version": 3,
            "artifact_path": "review/result.json",
            "checks_failed": [],
            "follow_up": [],
            "metadata": {"review_findings": [record], "confidence_gaps": ["Coverage limit"]},
        }
        (tmp_path / "findings-input.txt").write_text(json.dumps(report), encoding="utf-8")
        assert HELPER.main(["obligations", "--run", str(tmp_path)]) == 0
        source = json.loads(capsys.readouterr().out)["finding_sources"][0]
        assert source["source_id"] == "review/result.json#F1"
        assert all(
            value in source["body"]
            for value in [
                record["title"],
                record["summary"],
                record["required_change"],
                record["closure_evidence"],
                *record["evidence"],
            ]
        )
        assert len(json.loads((tmp_path / "report-obligations.json").read_text(encoding="utf-8"))["sources"]) == 1
        inventory = _selection()
        inventory["items"] = inventory["items"][:1]
        inventory["items"][0].update(input_item_id="F1", sources=[source])
        (tmp_path / "selection.json").write_text(json.dumps(inventory), encoding="utf-8")
        assert HELPER.main(["preflight", "--run", str(tmp_path), "--stage", "selection", "--report"]) == 0

    @pytest.mark.parametrize(
        "damage",
        [
            pytest.param(_duplicate_blocker, id="duplicate"),
            pytest.param(_invalid_id, id="invalid-id"),
            pytest.param(_empty_evidence, id="evidence"),
            pytest.param(_empty_title, id="title"),
            pytest.param(_without_producer, id="producer"),
        ],
    )
    def test_reports_unavailable_finding_sources(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], damage: Callable[[dict, dict], None]
    ) -> None:
        """Keep context available without manufacturing incomplete or ambiguous sources.

        A review finding is duplicated, mis-identified, evidence-less, untitled or lacks a producer; ``obligations``
        must still exit 0 and write its context file, but expose no finding source and name a gap instead.
        """
        record = {
            "id": "F1",
            "title": "Problem",
            "summary": "Context",
            "required_change": "Fix",
            "closure_evidence": "Pass",
            "evidence": ["source.py:1"],
        }
        report = {"schema_version": 3, "artifact_path": "review/result.json", "metadata": {"review_findings": [record]}}
        damage(report, record)
        (tmp_path / "findings-input.txt").write_text(json.dumps(report), encoding="utf-8")

        assert HELPER.main(["obligations", "--run", str(tmp_path)]) == 0

        output = json.loads(capsys.readouterr().out)
        assert output["finding_sources"] == []
        assert output["finding_source_gaps"]
        assert (tmp_path / "report-obligations.json").exists()

    def test_keeps_historical_partial_blocker_context(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Historical partial blockers remain readable without claiming a complete source.

        A report holds a blocker with only an id and title; ``obligations`` must exit 0, expose no finding source, name
        a gap, and still write the context sources.
        """
        report = {
            "schema_version": 3,
            "artifact_path": "review/result.json",
            "metadata": {
                "operational_blockers": [{"id": "B1", "title": "Missing check"}],
                "confidence_gaps": ["Coverage limit"],
            },
        }
        (tmp_path / "findings-input.txt").write_text(json.dumps(report), encoding="utf-8")
        assert HELPER.main(["obligations", "--run", str(tmp_path)]) == 0
        output = json.loads(capsys.readouterr().out)
        assert output["finding_sources"] == []
        assert output["finding_source_gaps"]
        assert len(json.loads((tmp_path / "report-obligations.json").read_text(encoding="utf-8"))["sources"]) == 1
