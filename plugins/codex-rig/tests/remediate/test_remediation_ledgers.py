"""Regression checks for append-only remediation ledgers and the tables rendered from them."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from test_finding_presentation import VALIDATOR, _write_remediation_candidate
from test_remediation_finalize import HELPER


@pytest.fixture
def selected_run(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    """Write a selected remediation run with one parent-owned bucket that passes every validator step."""
    result_path = _write_remediation_candidate(tmp_path, "code", ("implemented", "Added the guard."))
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["metadata"]["unresolved_summary"].update(selected_items_total=1, selected_items_resolved=1)
    result_path.write_text(json.dumps(result), encoding="utf-8")
    return tmp_path, result


def _stage(run: Path, ledger: str, payload: object) -> Path:
    """Stage one record exactly as the model writes it with its file tool before the append step."""
    staged = run / f"{ledger}.rec"
    text = payload if isinstance(payload, str) else json.dumps(payload)
    staged.write_text(text, encoding="utf-8")
    return staged


class TestStatusEventAppend:
    """Protect the validated, append-only status-event ledger."""

    def test_append_help_exposes_executable_event_examples(
        self, selected_run: tuple[Path, dict], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Let a caller discover and append the documented schema without inventing fields."""
        run, _ = selected_run
        with pytest.raises(SystemExit) as exit_info:
            HELPER.parse_args(["append", "--help"])
        assert exit_info.value.code == 0
        help_text = capsys.readouterr().out
        assert "one JSON object or a JSON array; not JSONL" in help_text
        assert "Unknown fields are rejected" in help_text
        examples = [json.loads(line) for line in help_text.splitlines() if line.startswith('{"kind":')]
        assert {example["kind"] for example in examples} == {"item", "bucket"}
        assert "resolved_how" in next(example for example in examples if example["kind"] == "item")
        _stage(run, "resolution-events.jsonl", examples)
        HELPER.append_record(run, "resolution-events.jsonl")
        assert HELPER.fold_events(HELPER.read_events(run))[0] == {"B1": "verified"}

    def test_appends_valid_events_and_removes_the_staged_record(self, selected_run: tuple[Path, dict]) -> None:
        """A valid item and bucket event pair lands as two JSON lines and the staged file disappears.

        The staged ``.rec`` file is the model's only write; leaving it behind would let a resumed run append it twice.
        """
        run, _ = selected_run
        staged = _stage(
            run,
            "resolution-events.jsonl",
            [{"kind": "item", "id": "R1", "owner_status": "fixed"}, {"kind": "bucket", "id": "B1", "status": "fixed"}],
        )

        summary = HELPER.append_record(run, "resolution-events.jsonl")

        lines = (run / "resolution-events.jsonl").read_text(encoding="utf-8").splitlines()
        assert (summary["appended"], len(lines), staged.exists()) == (2, 2, False)
        assert json.loads(lines[0]) == {"schema_version": 1, "kind": "item", "id": "R1", "owner_status": "fixed"}

    def test_later_append_keeps_earlier_bytes_unchanged(self, selected_run: tuple[Path, dict]) -> None:
        """A second event is added after the first one without rewriting it.

        Re-emitting earlier records through a rewrite is the failure the ledger exists to prevent.
        """
        run, _ = selected_run
        _stage(run, "resolution-events.jsonl", {"kind": "bucket", "id": "B1", "status": "in-progress"})
        HELPER.append_record(run, "resolution-events.jsonl")
        first = (run / "resolution-events.jsonl").read_bytes()
        _stage(run, "resolution-events.jsonl", {"kind": "bucket", "id": "B1", "status": "verified"})

        HELPER.append_record(run, "resolution-events.jsonl")

        assert (run / "resolution-events.jsonl").read_bytes().startswith(first)

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param(
                '{"kind":"bucket","id":"B1","status":"verified"}\n{"kind":"item","id":"R1","owner_status":"resolved"}',
                id="jsonl-staging",
            ),
            pytest.param(
                [
                    {"kind": "bucket", "id": "B1", "status": "verified"},
                    {"kind": "item", "id": "R1", "owner_status": "resolved", "review_status": "clean"},
                ],
                id="unsupported-review-field-in-batch",
            ),
        ],
    )
    def test_rejected_review_update_can_recover_without_losing_history(
        self, selected_run: tuple[Path, dict], payload: object
    ) -> None:
        """Keep the previous ledger intact through malformed closure attempts and a valid repair."""
        run, _ = selected_run
        _stage(run, "resolution-events.jsonl", {"kind": "bucket", "id": "B1", "status": "in-progress"})
        HELPER.append_record(run, "resolution-events.jsonl")
        before = (run / "resolution-events.jsonl").read_bytes()
        staged = _stage(run, "resolution-events.jsonl", payload)
        rejected = staged.read_bytes()
        with pytest.raises(HELPER.DeriveError):
            HELPER.append_record(run, "resolution-events.jsonl")
        assert (run / "resolution-events.jsonl").read_bytes() == before
        assert staged.read_bytes() == rejected
        _stage(run, "resolution-events.jsonl", {"kind": "bucket", "id": "B1", "status": "verified"})
        HELPER.append_record(run, "resolution-events.jsonl")
        assert (run / "resolution-events.jsonl").read_bytes().startswith(before)
        assert not staged.exists()
        assert HELPER.fold_events(HELPER.read_events(run))[0] == {"B1": "verified"}

    @pytest.mark.parametrize(
        ("event", "code"),
        [
            pytest.param(
                {"kind": "item", "id": "R9", "owner_status": "fixed"}, "event-unknown-item", id="unknown-item"
            ),
            pytest.param(
                {"kind": "bucket", "id": "B9", "status": "fixed"}, "event-unknown-bucket", id="unknown-bucket"
            ),
            pytest.param(
                {"kind": "item", "id": "R1", "triage_status": "stale"}, "event-item-triage_status-invalid", id="stale"
            ),
            pytest.param({"kind": "item", "id": "R1", "evidence": " "}, "event-evidence-blank", id="blank-evidence"),
            pytest.param(
                {"kind": "item", "id": "R1", "owner_status": "fixed", "severity": "low"},
                "event-field-unknown",
                id="identity-field",
            ),
            pytest.param({"kind": "item", "id": "R1"}, "event-item-empty", id="empty-item"),
            pytest.param({"kind": "bucket", "id": "B1", "status": "done"}, "event-bucket-status-invalid", id="status"),
        ],
    )
    def test_rejects_invalid_event_without_appending(
        self, selected_run: tuple[Path, dict], event: dict[str, object], code: str
    ) -> None:
        """An invalid event appends nothing and keeps the staged record for repair.

        Identity fields stay owned by the frozen selection, and retired or misspelled statuses cannot enter the ledger.
        """
        run, _ = selected_run
        staged = _stage(run, "resolution-events.jsonl", event)

        with pytest.raises(HELPER.DeriveError, match=code):
            HELPER.append_record(run, "resolution-events.jsonl")

        assert (staged.exists(), (run / "resolution-events.jsonl").exists()) == (True, False)

    def test_rejects_unsupported_ledger_name(self, selected_run: tuple[Path, dict]) -> None:
        """Only the named growing ledgers accept appended records; rendered documents stay helper-owned."""
        run, _ = selected_run

        with pytest.raises(HELPER.DeriveError, match=r"append-ledger-unsupported:action-items.md"):
            HELPER.append_record(run, "action-items.md")

    @pytest.mark.parametrize(
        "line",
        [
            pytest.param('{"kind": "bucket", "id": "B1"}', id="bucket-without-status"),
            pytest.param('{"kind": "item", "id": "R9", "owner_status": "fixed"}', id="unknown-item"),
            pytest.param("not json", id="not-json"),
        ],
    )
    def test_hand_edited_ledger_line_fails_with_a_code(self, selected_run: tuple[Path, dict], line: str) -> None:
        """A malformed ledger line stops derivation with its line number instead of a traceback or partial fold."""
        run, result = selected_run
        (run / "resolution-events.jsonl").write_text(line + "\n", encoding="utf-8")

        with pytest.raises(HELPER.DeriveError, match="events-ledger-invalid:1"):
            HELPER.derive_metadata(run, copy.deepcopy(result["metadata"]))


class TestClosureLogAppend:
    """Protect per-group closure evidence appends."""

    def test_first_append_creates_the_validator_heading_once(self, tmp_path: Path) -> None:
        """Two group records share one ``Closure Evidence`` heading and keep their order.

        The shared artifact validator requires that section, so the first append must create it.
        """
        _stage(tmp_path, "closure-log.md", "### B1\n\nAdded the guard; regression passes.")
        HELPER.append_record(tmp_path, "closure-log.md")
        _stage(tmp_path, "closure-log.md", "### B2\n\nDocumented the flag.\n")

        HELPER.append_record(tmp_path, "closure-log.md")

        text = (tmp_path / "closure-log.md").read_text(encoding="utf-8")
        assert text.count("## Closure Evidence") == 1
        assert text.index("### B1") < text.index("### B2")


class TestRenderedTablesFromEvents:
    """Protect the durable tables rebuilt from appended events."""

    @pytest.mark.parametrize("completeness", ["missing", "stale"])
    def test_ledger_render_repairs_completeness_from_all_latest_outcomes(
        self, selected_run: tuple[Path, dict], completeness: str
    ) -> None:
        """Rebuild numeric closure evidence instead of requiring forbidden hand edits to derived counts.

        Counts must include unselected rows and zero-valued statuses; retaining stale counts can block an otherwise
        valid run even when its machine metadata already reflects the latest events.
        """
        run, result = selected_run
        summary = "## Final Resolution Summary\n\nThe selected guard is closed; retain this authored explanation."
        intake = "## Review Report Intake\n\nTwo report items already closed."
        stale = (
            "## Final Resolution Table Completeness\n\n- Ingested entries: 99\n"
            "- Triage status counts: valid=99\n- Resolution status counts: implemented=99\n\n"
        )
        (run / "action-items.md").write_text(
            "# Action Items\n\n" + (stale if completeness == "stale" else "") + summary + "\n\n" + intake + "\n",
            encoding="utf-8",
        )
        _stage(
            run,
            "resolution-events.jsonl",
            [
                {"kind": "item", "id": "R1", "triage_status": "resolved", "resolution_status": "resolved"},
                {
                    "kind": "item",
                    "id": "R1",
                    "triage_status": "already-applied",
                    "resolution_status": "already-applied",
                },
                {"kind": "item", "id": "R2", "triage_status": "duplicate", "resolution_status": "duplicate"},
            ],
        )
        HELPER.append_record(run, "resolution-events.jsonl")
        metadata = copy.deepcopy(result["metadata"])
        table = metadata["final_resolution_table"]
        numeric_fields = (
            "ingested_entries_total",
            "table_rows_total",
            "omitted_entries_total",
            "selectable_rows_total",
            "nonselectable_rows_total",
            "source_records_total",
            "represented_source_records_total",
            "omitted_source_records_total",
            "grouped_items_total",
        )
        if completeness == "missing":
            metadata["final_resolution_table"] = {"items": table["items"]}
        else:
            table.update({field: 99 for field in numeric_fields}, required_columns=["stale"])

        HELPER.render_ledger(run, metadata)

        text = (run / "action-items.md").read_text(encoding="utf-8")
        table = metadata["final_resolution_table"]
        expected_triage = {
            "already-applied": 1,
            "already-fixed": 0,
            "duplicate": 1,
            "needs-clarification": 0,
            "out-of-scope": 0,
            "resolved": 0,
            "stale": 0,
            "valid": 0,
        }
        expected_resolution = {
            "already-applied": 1,
            "already-fixed": 0,
            "duplicate": 1,
            "implemented": 0,
            "needs-clarification": 0,
            "not-applicable": 0,
            "rejected": 0,
            "resolved": 0,
            "stale": 0,
            "unresolved": 0,
        }
        sources = result["metadata"]["final_resolution_table"]["items"]
        source_total = sum(len(item["sources"]) for item in sources)
        grouped_total = sum(len(item["sources"]) > 1 for item in sources)
        expected = [
            "## Final Resolution Table Completeness",
            "",
            "- Ingested entries: 2",
            "- Final table rows: 2",
            "- Omitted entries: 0",
            "- Selectable rows: 1",
            "- Non-selectable rows: 1",
            f"- Source records: {source_total}",
            f"- Represented source records: {source_total}",
            "- Omitted source records: 0",
            f"- Grouped items: {grouped_total}",
            "- Triage status counts: " + ", ".join(f"{status}={count}" for status, count in expected_triage.items()),
            "- Resolution status counts: "
            + ", ".join(f"{status}={count}" for status, count in expected_resolution.items()),
        ]
        assert [table[field] for field in numeric_fields] == [
            2,
            2,
            0,
            1,
            1,
            source_total,
            source_total,
            0,
            grouped_total,
        ]
        assert table["required_columns"] == sorted(VALIDATOR.CODE_REMEDIATE_FINAL_TABLE_REQUIRED_COLUMNS)
        assert table["triage_status_counts"] == expected_triage
        assert table["resolution_status_counts"] == expected_resolution
        assert text[text.index("## Final Resolution Table Completeness") :] == "\n".join(expected) + "\n"
        assert text.count("## Final Resolution Table Completeness") == 1
        assert text.count(summary) == text.count(intake) == 1
        assert text.index(summary) < text.index("## Final Resolution Table Completeness")
        assert [(item["input_item_id"], item["selectable"]) for item in table["items"]] == [
            ("R1", True),
            ("R2", False),
        ]
        VALIDATOR._validate_code_remediate_final_resolution_table(metadata, run)
        first = (run / "action-items.md").read_bytes()

        HELPER.render_ledger(run, metadata)

        assert (run / "action-items.md").read_bytes() == first

    @pytest.mark.parametrize("defect", ["contradictory-count", "duplicate-source"])
    def test_ledger_render_rejects_invalid_frozen_inventory(self, selected_run: tuple[Path, dict], defect: str) -> None:
        """Prevent derived completeness from masking contradictory or duplicate frozen source evidence."""
        run, result = selected_run
        path = run / "selection.json"
        inventory = json.loads(path.read_text(encoding="utf-8"))
        if defect == "contradictory-count":
            inventory["source_records_total"] = 999
            error = "selection-count-mismatch:source_records_total"
        else:
            inventory["items"][1]["sources"].append(copy.deepcopy(inventory["items"][0]["sources"][0]))
            error = "selection-source-duplicate"
        path.write_text(json.dumps(inventory), encoding="utf-8")

        with pytest.raises(HELPER.final_handoff.HandoffError, match=error):
            HELPER.render_ledger(run, copy.deepcopy(result["metadata"]))

    def test_ledger_render_reflects_latest_item_event_and_passes_table_validation(
        self, selected_run: tuple[Path, dict]
    ) -> None:
        """A later item event replaces the outcome cells and the result still reconciles with metadata items.

        The final-resolution-table validator binds Markdown rows to ``final_resolution_table.items``; rendering both
        from the same events keeps that contract while removing in-place edits.
        """
        run, result = selected_run
        _stage(
            run,
            "resolution-events.jsonl",
            {"kind": "item", "id": "R1", "resolved_how": "Guard added after review.", "pr_relation": "direct-diff"},
        )
        HELPER.append_record(run, "resolution-events.jsonl")
        with (run / "action-items.md").open("a", encoding="utf-8", newline="\n") as stream:
            stream.write("\n## Expanded Item Records\n\nR1 affects `src/guard.py`.\n")
        metadata = copy.deepcopy(result["metadata"])

        HELPER.render_ledger(run, metadata)

        text = (run / "action-items.md").read_text(encoding="utf-8")
        assert metadata["final_resolution_table"]["items"][0]["resolved_how"] == "Guard added after review."
        assert "[O1] Guard added after review." in text
        assert "| direct-diff |" in text
        assert "## Review Report Intake\n\nTwo report items already closed." in text
        assert "## Expanded Item Records\n\nR1 affects `src/guard.py`." in text
        VALIDATOR._validate_code_remediate_final_resolution_table(metadata, run)

    def test_ledger_render_fails_when_an_item_has_no_outcome(self, selected_run: tuple[Path, dict]) -> None:
        """Refuse an empty outcome cell instead of rendering a row the validator would reject later."""
        run, result = selected_run
        metadata = copy.deepcopy(result["metadata"])
        metadata["final_resolution_table"]["items"][1].pop("evidence")

        with pytest.raises(HELPER.DeriveError, match="ledger-item-outcome-missing:R2:evidence"):
            HELPER.render_ledger(run, metadata)

    def test_workplan_status_comes_from_the_latest_bucket_event(self, selected_run: tuple[Path, dict]) -> None:
        """Render the newest bucket status and reuse the recorded ineligibility reason on refresh.

        Status never enters the digest-bound plan JSON, so the workplan validator still accepts the refreshed file.
        """
        run, result = selected_run
        plan_bytes = (run / "work-bucket-plan.json").read_bytes()
        for status in ("in-progress", "verified"):
            _stage(run, "resolution-events.jsonl", {"kind": "bucket", "id": "B1", "status": status})
            HELPER.append_record(run, "resolution-events.jsonl")
        metadata = copy.deepcopy(result["metadata"])

        HELPER.render_workplan(run, metadata, None)

        text = (run / "resolution-workplan.md").read_text(encoding="utf-8")
        assert "| verified |" in text
        assert "| in-progress |" not in text
        assert "Ineligibility reason: One selected closure item forms one coherent bucket." in text
        assert (run / "work-bucket-plan.json").read_bytes() == plan_bytes
        VALIDATOR._validate_code_remediate_workplan(metadata, run)


def _review_run(root: Path, run: Path) -> Path:
    """Create the review run whose result bytes this remediation admitted as ``findings-input.txt``."""
    review_run = root / "pr-12" / "run-003"
    review_run.mkdir(parents=True)
    (review_run / "result.json").write_bytes((run / "findings-input.txt").read_bytes())
    return review_run


def _promote_with_report_ids(run: Path, result: dict[str, object]) -> None:
    """Promote the remediation result with run-qualified report finding identities."""
    promoted = copy.deepcopy(result)
    for item in promoted["metadata"]["final_resolution_table"]["items"]:
        item["sources"][0]["finding_id"] = item["input_item_id"]
    (run / "result.json").write_text(json.dumps(promoted), encoding="utf-8")


class TestReviewResolutionFeedback:
    """Protect the remediation-to-review feedback ledger."""

    def test_process_limitation_feedback_prevents_reopening_as_a_finding(
        self, selected_run: tuple[Path, dict], tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """Persist reclassification without putting a declined extra-review request in the final item table."""
        run, result = selected_run
        result["metadata"]["final_resolution_table"]["items"].pop()
        result["metadata"]["review_report_intake"]["obligation_records_version"] = 2
        result["metadata"]["review_report_intake"]["review_limitations"] = [
            {"finding_id": "R2", "rationale": "Reviewer coverage unavailable; extra review declined."}
        ]
        _promote_with_report_ids(run, result)
        review_run = _review_run(tmp_path_factory.mktemp("reviews"), run)
        summary = HELPER.append_review_resolutions(run, review_run)
        records = [json.loads(line) for line in (review_run / "resolution.jsonl").read_text().splitlines()]
        assert summary["appended"] == 2
        assert records[-1]["finding_id"] == "R2"
        assert records[-1]["verdict"] == "rejected"
        assert records[-1]["sha"] is None
        assert records[-1]["why"] == "Reviewer coverage unavailable; extra review declined."
        original = (review_run / "resolution.jsonl").read_bytes()
        assert HELPER.append_review_resolutions(run, review_run)["appended"] == 0
        assert (review_run / "resolution.jsonl").read_bytes() == original

    def test_appends_one_verdict_per_admitted_finding(
        self, selected_run: tuple[Path, dict], tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """Each admitted finding gets one schema-1 record whose verdict follows its recorded outcome."""
        run, result = selected_run
        _promote_with_report_ids(run, result)
        review_run = _review_run(tmp_path_factory.mktemp("reviews"), run)

        summary = HELPER.append_review_resolutions(run, review_run, sha="abc1234")

        records = [json.loads(line) for line in (review_run / "resolution.jsonl").read_text().splitlines()]
        assert summary["appended"] == 2
        assert [(r["finding_id"], r["verdict"], r["sha"]) for r in records] == [
            ("R1", "fixed", "abc1234"),
            ("R2", "fixed", "abc1234"),
        ]
        assert records[0]["why"] == "Added the guard."
        assert records[0]["schema_version"] == 1

    def test_resumed_run_does_not_duplicate_identical_records(
        self, selected_run: tuple[Path, dict], tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """Repeating the same feedback appends nothing, while the earlier lines stay byte-identical."""
        run, result = selected_run
        _promote_with_report_ids(run, result)
        review_run = _review_run(tmp_path_factory.mktemp("reviews"), run)
        HELPER.append_review_resolutions(run, review_run)
        first = (review_run / "resolution.jsonl").read_bytes()

        summary = HELPER.append_review_resolutions(run, review_run)

        assert (summary["appended"], summary["already_recorded"]) == (0, 2)
        assert (review_run / "resolution.jsonl").read_bytes() == first

    def test_refuses_a_review_run_that_was_not_admitted(
        self, selected_run: tuple[Path, dict], tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """Feedback never lands on a review whose result bytes differ from this run's admitted input."""
        run, result = selected_run
        _promote_with_report_ids(run, result)
        review_run = _review_run(tmp_path_factory.mktemp("reviews"), run)
        (review_run / "result.json").write_text("{}", encoding="utf-8")

        with pytest.raises(HELPER.DeriveError, match="resolution-review-run-mismatch"):
            HELPER.append_review_resolutions(run, review_run)

        assert not (review_run / "resolution.jsonl").exists()

    def test_refuses_an_unpromoted_review_candidate(
        self, selected_run: tuple[Path, dict], tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """A review that never promoted its candidate was never admitted, so it receives no verdicts."""
        run, result = selected_run
        _promote_with_report_ids(run, result)
        review_run = _review_run(tmp_path_factory.mktemp("reviews"), run)
        (review_run / "result.json").rename(review_run / "result.candidate.json")

        with pytest.raises(HELPER.DeriveError, match="resolution-review-run-mismatch"):
            HELPER.append_review_resolutions(run, review_run)

        assert not (review_run / "resolution.jsonl").exists()

    def test_requires_a_promoted_remediation_result(
        self, selected_run: tuple[Path, dict], tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """Only a validated, promoted remediation result may feed outcomes back to the review."""
        run, _ = selected_run
        review_run = _review_run(tmp_path_factory.mktemp("reviews"), run)

        with pytest.raises(HELPER.DeriveError, match="resolution-result-not-promoted"):
            HELPER.append_review_resolutions(run, review_run)

    @pytest.mark.parametrize(
        ("outcome", "verdict"),
        [
            pytest.param({"resolution_status": "implemented", "owner_status": "fixed"}, "fixed", id="implemented"),
            pytest.param({"resolution_status": "already-applied", "owner_status": "resolved"}, "fixed", id="applied"),
            pytest.param({"resolution_status": "rejected", "owner_status": "resolved"}, "rejected", id="rejected"),
            pytest.param(
                {"resolution_status": "unresolved", "owner_status": "not-selected"}, "deferred", id="unselected"
            ),
            pytest.param({"resolution_status": "needs-clarification", "owner_status": "todo"}, "skipped", id="open"),
        ],
    )
    def test_verdict_mapping(self, outcome: dict[str, str], verdict: str) -> None:
        """Map each recorded outcome family to exactly one feedback verdict."""
        assert HELPER.resolution_verdict(outcome) == verdict


def test_finalize_derives_item_outcomes_from_events(
    selected_run: tuple[Path, dict], tmp_path_factory: pytest.TempPathFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    """Refresh a previously rendered ledger when finalization sees a later outcome event.

    Requiring another explicit ledger command leaves ordinary finalization vulnerable to stale displayed outcomes. The
    durable ledger and promoted metadata must both reflect the newest event without that repair command.
    """
    from test_remediation_finalize import _draft_handoff, _stale_metadata

    run, result = selected_run
    HELPER.render_ledger(run, copy.deepcopy(result["metadata"]))
    before = (run / "action-items.md").read_bytes()
    _stage(run, "resolution-events.jsonl", {"kind": "item", "id": "R1", "resolved_how": "Guard added after review."})
    HELPER.append_record(run, "resolution-events.jsonl")
    assert (run / "action-items.md").read_bytes() == before
    drafts = tmp_path_factory.mktemp("drafts")
    (drafts / "metadata.json").write_text(json.dumps(_stale_metadata(result)), encoding="utf-8")
    (drafts / "handoff.json").write_text(json.dumps(_draft_handoff(run)), encoding="utf-8")
    argv = ["finalize", "--run", str(run), "--metadata", str(drafts / "metadata.json")]
    argv += ["--handoff", str(drafts / "handoff.json"), "--status", result["status"]]
    argv += ["--confidence", str(result["confidence"]), "--artifact-path", result["artifact_path"], "--promote"]

    exit_code = HELPER.main(argv)

    summary = json.loads(capsys.readouterr().out)
    promoted = json.loads((run / "result.json").read_text(encoding="utf-8"))
    assert (exit_code, summary["errors"]) == (0, [])
    assert summary["promoted"] is True
    assert promoted["metadata"]["final_resolution_table"]["items"][0]["resolved_how"] == "Guard added after review."
    assert "Guard added after review." in (run / "action-items.md").read_text(encoding="utf-8")
