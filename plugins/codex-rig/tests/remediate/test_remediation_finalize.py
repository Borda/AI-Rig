"""Regression checks for derived remediation artifacts, one-pass validation, and one-command finalization."""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from test_finding_presentation import VALIDATOR, _write_remediation_candidate

# Importing the fixture function registers the shared `assessed_pr` fixture in this module.
from test_review_completion_gate import (
    FINDER,
    _assessed_pr,  # noqa: F401
    _finalize_parent_fallback,
)
from test_review_finding_identity import _load_validator, _metadata, _notes, _result

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
DERIVED_HANDOFF_FIELDS = ("verification", "confidence", "artifacts", "tables", "source_records", "source_coverage")


def _load_helper() -> ModuleType:
    """Load the remediation finalize helper by file path, as the skill invokes it."""
    path = PLUGIN_ROOT / "shared" / "remediation_finalize.py"
    spec = importlib.util.spec_from_file_location("codex_rig_remediation_finalize", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HELPER = _load_helper()


@pytest.fixture
def valid_run(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    """Write a selected, implemented remediation run that passes every validator step."""
    result_path = _write_remediation_candidate(tmp_path, "code", ("implemented", "Added the guard."))
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["metadata"]["unresolved_summary"].update(selected_items_total=1, selected_items_resolved=1)
    result_path.write_text(json.dumps(result), encoding="utf-8")
    return tmp_path, result


def _stale_metadata(result: dict[str, object]) -> dict[str, object]:
    """Return the run's metadata with every derivable copy removed or corrupted, as a hand draft would drift."""
    metadata = copy.deepcopy(result["metadata"])
    metadata.pop("final_handoff")
    metadata["resolution_scope"].update(deferred_indexes=[9], presentation_version=1)
    for key in ("bucket_plan_sha256", "groups_total", "verifier_groups", "work_buckets", "parallel_approval_status"):
        metadata["resolution_workplan"].pop(key)
    for item in metadata["final_resolution_table"]["items"]:
        item["item_name"] = item["item_name"].upper()
    return metadata


def _draft_handoff(run: Path) -> dict[str, object]:
    """Return the run's handoff with every derivable field removed."""
    handoff = json.loads((run / "final-handoff.json").read_text(encoding="utf-8"))
    for field in DERIVED_HANDOFF_FIELDS:
        handoff.pop(field)
    return handoff


def test_current_handoff_marks_selection_independently_of_outcome(valid_run: tuple[Path, dict[str, object]]) -> None:
    """Keep all findings visible and count selection indexes only over selectable items."""
    run, result = valid_run
    metadata = copy.deepcopy(result["metadata"])
    metadata["resolution_scope"].update(presentation_version=4, selected_indexes=[2])
    original = metadata["final_resolution_table"]["items"][0]
    items = []
    for identity, selectable in [("CLOSED", False), ("OMITTED", True), ("CHOSEN", True)]:
        item = copy.deepcopy(original)
        item.update(
            input_item_id=identity,
            item_name=identity,
            selectable=selectable,
            resolved_how="Implemented: Added the guard.",
        )
        for source_index, source in enumerate(item["sources"]):
            source["source_id"] = f"{identity}-{source_index}"
        items.append(item)
    metadata["final_resolution_table"]["items"] = items

    handoff = HELPER.derive_handoff(run, metadata, _draft_handoff(run), result["confidence"], result["artifact_path"])

    assert handoff["presentation_version"] == 5
    assert [(row["id"], row["selected"]) for row in handoff["tables"][0]["rows"]] == [
        ("CLOSED", False),
        ("OMITTED", False),
        ("CHOSEN", True),
    ]
    rendered = HELPER.final_handoff.render_handoff(handoff)
    assert "| ID | Severity | Finding | Selected | Outcome |" in rendered
    for identity, selected in [("CLOSED", "No"), ("OMITTED", "No"), ("CHOSEN", "Yes")]:
        assert f"| {identity} | {selected} | Implemented |" in rendered
    assert "| Resolution |" not in rendered
    assert "[Action items](" in rendered
    VALIDATOR._validate_code_remediate_final_handoff({"metadata": metadata}, handoff, candidate=True)
    handoff["tables"][0]["rows"][0]["selected"] = True
    with pytest.raises(SystemExit, match="row-coverage-mismatch"):
        VALIDATOR._validate_code_remediate_final_handoff({"metadata": metadata}, handoff, candidate=True)


@pytest.mark.parametrize("selection", [None, 1, "Yes"])
def test_current_handoff_rejects_missing_or_nonboolean_selection(
    valid_run: tuple[Path, dict[str, object]], selection: object
) -> None:
    """A visible selection mark must come from an explicit boolean, never truthiness."""
    run, result = valid_run
    metadata = copy.deepcopy(result["metadata"])
    metadata["resolution_scope"]["presentation_version"] = 4
    handoff = HELPER.derive_handoff(run, metadata, _draft_handoff(run), result["confidence"], result["artifact_path"])
    row = handoff["tables"][0]["rows"][0]
    if selection is None:
        row.pop("selected", None)
    else:
        row["selected"] = selection
    with pytest.raises(ValueError, match="remediation-row-selected-invalid"):
        HELPER.final_handoff.render_handoff(handoff)


def test_current_candidate_cannot_hide_selection_using_historical_handoff(
    valid_run: tuple[Path, dict[str, object]],
) -> None:
    """Historical reports remain readable, but a fresh scope-four candidate must show selection."""
    run, result = valid_run
    metadata = copy.deepcopy(result["metadata"])
    metadata["resolution_scope"]["presentation_version"] = 4
    handoff = HELPER.derive_handoff(run, metadata, _draft_handoff(run), result["confidence"], result["artifact_path"])
    handoff["presentation_version"] = 4
    for row in handoff["tables"][0]["rows"]:
        row.pop("selected", None)
    assert "| Selected |" not in HELPER.final_handoff.render_handoff(handoff)
    VALIDATOR._validate_code_remediate_final_handoff({"metadata": metadata}, handoff)
    with pytest.raises(SystemExit, match="remediation-current-handoff-presentation-v5-required"):
        VALIDATOR._validate_code_remediate_final_handoff({"metadata": metadata}, handoff, candidate=True)


def test_public_validator_preserves_historical_handoff_but_rejects_new_candidate(tmp_path: Path) -> None:
    """Distinguish a promoted scope-four report from a fresh candidate at the normal validator entrypoint."""
    candidate = _write_remediation_candidate(tmp_path, "code")
    result = json.loads(candidate.read_text(encoding="utf-8"))
    metadata = result["metadata"]
    metadata["resolution_scope"]["presentation_version"] = 4
    inventory_path = tmp_path / "selection.json"
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    inventory["presentation_version"] = 4
    inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
    (tmp_path / "resolution-scope.md").write_bytes(HELPER.final_handoff.render_selection(inventory).encode("utf-8"))
    for item in metadata["final_resolution_table"]["items"]:
        item["resolved_how"] = "Verified without code changes: Existing regression passes."
    HELPER.render_ledger(tmp_path, metadata)
    handoff = HELPER.derive_handoff(
        tmp_path, metadata, _draft_handoff(tmp_path), result["confidence"], result["artifact_path"]
    )
    handoff["presentation_version"] = 4
    for row in handoff["tables"][0]["rows"]:
        row.pop("selected")
    (tmp_path / "final-handoff.json").write_text(json.dumps(handoff), encoding="utf-8")
    HELPER._bind_handoff(tmp_path, metadata, handoff)
    canonical = tmp_path / "result.json"
    canonical.write_text(json.dumps(result), encoding="utf-8")
    candidate.write_bytes(canonical.read_bytes())

    VALIDATOR.validate("code-remediate", tmp_path, canonical)
    with pytest.raises(SystemExit, match="remediation-current-handoff-presentation-v5-required"):
        VALIDATOR.validate("code-remediate", tmp_path, candidate)

    current = HELPER.derive_handoff(tmp_path, metadata, handoff, result["confidence"], result["artifact_path"])
    (tmp_path / "final-handoff.json").write_text(json.dumps(current), encoding="utf-8")
    HELPER._bind_handoff(tmp_path, metadata, current)
    candidate.write_text(json.dumps(result), encoding="utf-8")
    canonical.write_bytes(candidate.read_bytes())
    VALIDATOR.validate("code-remediate", tmp_path, candidate)
    VALIDATOR.validate("code-remediate", tmp_path, canonical)


def test_derived_metadata_restores_every_hand_copied_field(valid_run: tuple[Path, dict[str, object]]) -> None:
    """Rebuild scope, workplan, and item identity from machine files exactly as the passing run recorded them.

    A drifted hand copy of the plan, digest, counts, deferred indexes, or item names is the failure family this helper
    removes; the derived values must equal the ones a validator-accepted run carries.
    """
    run, result = valid_run
    expected = result["metadata"]

    derived = HELPER.derive_metadata(run, _stale_metadata(result))

    for key in ("resolution_scope", "resolution_workplan", "final_resolution_table", "pr_relevance"):
        assert derived[key] == expected[key]


def test_finalize_turns_judgement_drafts_into_a_passing_promoted_result(
    valid_run: tuple[Path, dict[str, object]],
    tmp_path_factory: pytest.TempPathFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Derive, render, write, validate, and promote in one call when only judgement fields were authored.

    The drafts omit the handoff verification, confidence, artifacts, tables, and source records, and the metadata omits
    its handoff binding and derived copies; one finalize call must still produce a validator-accepted result.
    """
    run, result = valid_run
    drafts = tmp_path_factory.mktemp("drafts")
    (drafts / "metadata.json").write_text(json.dumps(_stale_metadata(result)), encoding="utf-8")
    (drafts / "handoff.json").write_text(json.dumps(_draft_handoff(run)), encoding="utf-8")
    argv = [
        "finalize",
        "--run",
        str(run),
        "--metadata",
        str(drafts / "metadata.json"),
        "--handoff",
        str(drafts / "handoff.json"),
        "--status",
        result["status"],
        "--confidence",
        str(result["confidence"]),
        "--artifact-path",
        result["artifact_path"],
        "--promote",
    ]

    exit_code = HELPER.main(argv)
    summary = json.loads(capsys.readouterr().out)

    assert (exit_code, summary["status"], summary["errors"], summary["promoted"]) == (0, "pass", [], True)
    assert [step["step"] for step in summary["steps"]] == ["derive", "render", "write-result", "validate"]
    VALIDATOR.validate("code-remediate", run, run / "result.json")


def test_all_errors_reports_independent_failures_with_hints(valid_run: tuple[Path, dict[str, object]]) -> None:
    """Report every independent failure in one pass while the fail-fast entry point still stops at the first.

    Two unrelated defects, a stale plan copy and a missing unresolved-work note, must both appear with repair hints so
    one repair round fixes them; neither may be reported as passed.
    """
    run, result = valid_run
    result_path = run / "result.candidate.json"
    result["metadata"]["resolution_workplan"]["work_buckets"][0]["owner"] = "parent "
    result_path.write_text(json.dumps(result), encoding="utf-8")
    (run / "unresolved.txt").unlink()

    report = VALIDATOR.collect_errors("code-remediate", run, result_path)

    codes = {error["code"] for error in report["errors"]}
    assert report["status"] == "fail"
    assert "code-remediate-work-bucket-plan-content-mismatch" in codes
    assert any(code.startswith("missing-artifact:") and code.endswith("unresolved.txt") for code in codes)
    assert all(error["hint"] for error in report["errors"])
    with pytest.raises(SystemExit):
        VALIDATOR.validate("code-remediate", run, result_path)


def test_all_errors_passes_a_valid_run_and_matches_fail_fast(valid_run: tuple[Path, dict[str, object]]) -> None:
    """Accept exactly the runs the fail-fast validator accepts."""
    run, _ = valid_run

    report = VALIDATOR.collect_errors("code-remediate", run, run / "result.candidate.json")

    assert report == {"status": "pass", "errors": [], "not_run": []}
    VALIDATOR.validate("code-remediate", run, run / "result.candidate.json")


def test_all_errors_marks_dependent_steps_not_run_instead_of_passed(tmp_path: Path) -> None:
    """Never let a step whose prerequisite failed read as a pass."""
    (tmp_path / "result.candidate.json").write_text(json.dumps({"status": "pass"}), encoding="utf-8")

    report = VALIDATOR.collect_errors("code-remediate", tmp_path, tmp_path / "result.candidate.json")

    assert report["status"] == "fail"
    assert report["errors"][0]["step"] == "result"
    assert {entry["step"] for entry in report["not_run"]} >= {"gates", "final-handoff", "code-remediate-workplan"}


def test_validator_cli_prints_all_errors_json(
    valid_run: tuple[Path, dict[str, object]], capsys: pytest.CaptureFixture[str]
) -> None:
    """Expose the one-pass report through the validator command line with a failing exit status."""
    run, result = valid_run
    result["metadata"]["pr_relevance"]["evaluated"] = "yes"
    (run / "result.candidate.json").write_text(json.dumps(result), encoding="utf-8")
    argv = ["validate-artifacts.py", "--skill", "code-remediate", "--out", str(run)]
    argv += ["--result", str(run / "result.candidate.json"), "--all-errors"]

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("sys.argv", argv)
        exit_code = VALIDATOR.main()
    report = json.loads(capsys.readouterr().out)

    assert (exit_code, report["status"]) == (1, "fail")
    assert report["errors"]


def test_workplan_render_keeps_agent_sections_and_passes_document_checks(
    valid_run: tuple[Path, dict[str, object]],
) -> None:
    """Rewrite only the managed workplan sections, keep upfront decisions, and satisfy the workplan validator."""
    run, result = valid_run
    workplan = run / "resolution-workplan.md"
    workplan.write_text(
        workplan.read_text(encoding="utf-8") + "\n## Upfront Decisions\n\nCommit preference: all at once.\n",
        encoding="utf-8",
    )
    metadata = copy.deepcopy(result["metadata"])

    HELPER.render_workplan(run, metadata, "One selected item forms one coherent bucket.")

    text = workplan.read_text(encoding="utf-8")
    assert "## Upfront Decisions\n\nCommit preference: all at once." in text
    assert text.count("## Parallel Approval") == 1
    VALIDATOR._validate_code_remediate_workplan(metadata, run)


def test_workplan_render_requires_reason_for_default_parent_only_plan(
    valid_run: tuple[Path, dict[str, object]],
) -> None:
    """Refuse to render a default parent-only plan without a concrete ineligibility reason.

    A refresh may reuse the reason already rendered in the workplan, so this case removes it first: with no argument and
    no recorded reason, nothing can justify the parent-only route.
    """
    run, result = valid_run
    workplan = run / "resolution-workplan.md"
    text = workplan.read_text(encoding="utf-8")
    workplan.write_text("\n".join(line for line in text.splitlines() if not line.startswith("Ineligibility reason:")))

    with pytest.raises(HELPER.DeriveError, match="ineligibility-reason-required"):
        HELPER.render_workplan(run, copy.deepcopy(result["metadata"]), None)


def test_derivation_rejects_plan_digest_that_differs_from_approval(valid_run: tuple[Path, dict[str, object]]) -> None:
    """Stop when the dispatch record was approved for different plan bytes instead of rebinding it."""
    run, result = valid_run
    plan = run / "work-bucket-plan.json"
    plan.write_text(plan.read_text(encoding="utf-8") + " ", encoding="utf-8")

    with pytest.raises(HELPER.DeriveError, match="parallel-approval-plan-digest-mismatch"):
        HELPER.derive_metadata(run, copy.deepcopy(result["metadata"]))


def test_skill_routes_bookkeeping_through_the_derivation_helper() -> None:
    """Keep the remediation skill on derived workplan sections, one-call finalize, and one-pass validation."""
    skill = " ".join((PLUGIN_ROOT / "skills" / "code-remediate" / "SKILL.md").read_text(encoding="utf-8").split())
    contract = (PLUGIN_ROOT / "shared" / "helper-cli-contract.md").read_text(encoding="utf-8")

    assert "remediation_finalize.py workplan --help" in skill
    assert "Do not hand-write those four sections" in skill
    assert "remediation_finalize.py finalize --help" in skill
    assert "repair every listed error in one round from its hint, then rerun `finalize`" in skill
    assert "Never hand-copy a derived field." in skill
    assert "one `remediation_finalize.py finalize --promote` call" in skill
    assert "PLUGIN_ROOT/bin/python PLUGIN_ROOT/shared/remediation_finalize.py --help" in contract
    assert "`--all-errors`" in contract


def test_review_finalize_derives_shared_fields_and_runs_both_validators(
    assessed_pr: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Finalize an assessed PR review in one call: shared handoff fields, render, write, both validators, promote.

    Snapshot judgments remain agent-authored; canonical action content and shared handoff fields are derived. The
    promoted result must still complete through the review finder, which reruns both validators.
    """
    run = assessed_pr
    result_path = run / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result_path.unlink()
    drafts = tmp_path / "drafts"
    drafts.mkdir()
    metadata = {key: value for key, value in result["metadata"].items() if key != "final_handoff"}
    handoff = json.loads((run / "final-handoff.json").read_text(encoding="utf-8"))
    for field in ("verification", "confidence", "artifacts"):
        handoff.pop(field)
    (drafts / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    (drafts / "handoff.json").write_text(json.dumps(handoff), encoding="utf-8")
    argv = ["finalize", "--skill", "code-review", "--parent-thread-id", "thread", "--run", str(run)]
    argv += ["--metadata", str(drafts / "metadata.json"), "--handoff", str(drafts / "handoff.json")]
    argv += ["--status", "pass", "--confidence", "0.95", "--artifact-path", str(result_path), "--promote"]

    exit_code = HELPER.main(argv)
    summary = json.loads(capsys.readouterr().out)
    finished = subprocess.run(
        [sys.executable, str(FINDER), "--complete-run", str(run), "--parent-thread-id", "thread"], capture_output=True
    )

    assert (exit_code, summary["status"], summary["promoted"]) == (0, "pass", True), summary
    assert [step["step"] for step in summary["steps"]] == [
        "derive",
        "render",
        "write-result",
        "review-validate",
        "validate",
    ]
    assert finished.returncode == 0, finished.stderr.decode()


def test_review_finalize_stops_on_review_validator_failure(
    assessed_pr: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Never promote a review candidate the review-specific validator rejects."""
    run = assessed_pr
    result_path = run / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result_path.unlink()
    (tmp_path / "metadata.json").write_text(json.dumps(result["metadata"]), encoding="utf-8")
    argv = ["finalize", "--skill", "code-review", "--parent-thread-id", "other-thread", "--run", str(run)]
    argv += ["--metadata", str(tmp_path / "metadata.json"), "--handoff", str(run / "final-handoff.json")]
    argv += ["--status", "pass", "--confidence", "0.95", "--artifact-path", str(result_path), "--promote"]

    exit_code = HELPER.main(argv)
    summary = json.loads(capsys.readouterr().out)

    assert (exit_code, summary["status"], summary["promoted"]) == (1, "fail", False)
    assert summary["steps"][-1]["step"] == "review-validate"
    assert not result_path.exists()


@pytest.mark.integration
def test_review_finalize_repairs_copied_blocker_content_before_completion(
    assessed_pr: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Canonical blocker facts survive stale presentation through completion and report admission."""
    run = assessed_pr
    prepared = _finalize_parent_fallback(run)
    assert prepared.returncode == 0, prepared.stdout + prepared.stderr
    result_path = run / "result.json"
    result_path.unlink()
    notes = run / "review-notes.md"
    original = notes.read_text(encoding="utf-8")
    notes.write_text(
        original.replace("Run the configured mypy check in the project environment.", "Stale proposal"),
        encoding="utf-8",
    )
    draft = run / "fallback-drafts/handoff.json"
    handoff = json.loads(draft.read_bytes())
    row = handoff["tables"][-1]["rows"][0]
    row["title"] = "Stale title"
    row["cells"][1:3] = ["Stale proposal", "Stale evidence"]
    draft.write_text(json.dumps(handoff), encoding="utf-8")
    argv = ["finalize", "--skill", "code-review", "--parent-thread-id", "thread", "--run", str(run)]
    argv += ["--metadata", str(run / "fallback-drafts/metadata.json"), "--handoff", str(draft)]
    argv += ["--status", "fail", "--confidence", "0.95", "--artifact-path", str(result_path), "--promote"]

    assert HELPER.main(argv) == 0, capsys.readouterr().out
    summary = json.loads(capsys.readouterr().out)
    assert summary["promoted"] is True
    assert notes.read_text(encoding="utf-8") == original
    assert json.loads(result_path.read_bytes())["status"] == "fail"
    for action in (["--complete-run", str(run)], ["--result", str(result_path)]):
        completed = subprocess.run(
            [sys.executable, str(FINDER), *action, "--parent-thread-id", "thread"], capture_output=True, text=True
        )
        assert completed.returncode == 0, completed.stderr
    before = notes.read_bytes()
    assert HELPER.main(argv) == 0, capsys.readouterr().out
    assert notes.read_bytes() == before


def test_review_failure_retains_structured_errors_and_next_action(
    assessed_pr: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A provenance rejection retains each check and a recovery action without promotion."""
    run = assessed_pr
    result = json.loads((run / "result.json").read_bytes())
    (run / "result.json").unlink()
    metadata = tmp_path / "metadata.json"
    metadata.write_text(json.dumps(result["metadata"]), encoding="utf-8")
    argv = ["finalize", "--skill", "code-review", "--parent-thread-id", "other-thread", "--run", str(run)]
    argv += ["--metadata", str(metadata), "--handoff", str(run / "final-handoff.json")]
    argv += ["--status", "pass", "--confidence", "0.95", "--artifact-path", str(run / "result.json"), "--promote"]

    assert HELPER.main(argv) == 1
    summary = json.loads(capsys.readouterr().out)
    assert summary["errors"]
    assert all(error["step"] and error["code"] and error["hint"] for error in summary["errors"])
    assert summary["next_action"]["owner"] == "code-review"
    assert "provenance" in summary["next_action"]["action"]
    assert "validators" in summary["next_action"]["resume_condition"]
    assert summary["promoted"] is False
    assert not (run / "result.json").exists()


@pytest.mark.parametrize("attributed", [False, True])
def test_review_action_derivation_preserves_judgment_and_escaped_content(tmp_path: Path, attributed: bool) -> None:
    """Findings and blockers retain identities, status and source bindings while canonical text is escaped."""
    metadata = _metadata()
    if attributed:
        metadata["reviewer_assessments"] = []
    records = metadata["review_findings"] + metadata["operational_blockers"]
    rows = []
    for record in records:
        record["required_change"] = "Keep a | b\r\nand verify"
        record["evidence"] = ["first | proof", "second\nproof"]
        if attributed:
            record["authors"] = ["Main reviewer", "QA specialist"]
        rows.append(
            {
                "id": record["id"],
                "cells": [record["id"], "stale", "stale", "Verify"],
                "source_ids": ["source:" + record["id"]],
            }
        )
    handoff = {"tables": [{"heading": "Review Findings and Merge Blocks", "rows": rows}]}
    notes = tmp_path / "review-notes.md"
    _notes(notes, [record["id"] for record in records])
    prefix, suffix = (
        "## Decision Summary\r\n\r\nKeep this.\r\n",
        "\r\nComment after table.\r\n## Evidence\r\nUntouched.\r\n",
    )
    notes.write_bytes((prefix + notes.read_text().replace("Required |", "Verify |") + suffix).encode())
    original_metadata = copy.deepcopy(metadata)

    HELPER._derive_review_actions(tmp_path, metadata, handoff)

    _load_validator()._validate_action_table(notes, _result(), metadata, "pr")
    assert metadata == original_metadata
    assert notes.read_bytes().startswith(prefix.encode())
    assert notes.read_bytes().endswith(suffix.encode())
    for row, record in zip(rows, records, strict=True):
        assert row["id"] == record["id"]
        assert row["source_ids"] == ["source:" + record["id"]]
        assert row["cells"] == [record["id"], record["required_change"], "; ".join(record["evidence"]), "Verify"]
    once = notes.read_bytes()
    HELPER._derive_review_actions(tmp_path, metadata, handoff)
    assert notes.read_bytes() == once


@pytest.mark.parametrize(
    "defect", ["duplicate-record", "duplicate-row", "missing-row", "status", "second-table", "second-section"]
)
def test_review_action_derivation_rejects_ambiguous_authored_input(tmp_path: Path, defect: str) -> None:
    """Never repair identity/status decisions or erase an ambiguous notes table."""
    metadata = _metadata()
    records = metadata["review_findings"] + metadata["operational_blockers"]
    rows = [{"id": record["id"], "cells": [record["id"], "stale", "stale", "Required"]} for record in records]
    handoff = {"tables": [{"heading": "Review Findings and Merge Blocks", "rows": rows}]}
    notes = tmp_path / "review-notes.md"
    _notes(notes, [record["id"] for record in records])
    if defect == "duplicate-record":
        metadata["review_findings"].append(copy.deepcopy(records[0]))
    elif defect == "duplicate-row":
        rows.append(copy.deepcopy(rows[0]))
    elif defect == "missing-row":
        rows.pop()
    elif defect == "status":
        rows[0]["cells"][-1] = "Verify"
    elif defect == "second-table":
        notes.write_text(notes.read_text() + "\nOther evidence\n| Extra | Table |\n", encoding="utf-8")
    else:
        notes.write_text(notes.read_text() * 2, encoding="utf-8")
    before = notes.read_bytes()
    with pytest.raises(HELPER.DeriveError, match="review-action-"):
        HELPER._derive_review_actions(tmp_path, metadata, handoff)
    assert notes.read_bytes() == before
