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
from test_review_completion_gate import FINDER, _assessed_pr  # noqa: F401


PLUGIN_ROOT = Path(__file__).resolve().parents[2]
DERIVED_HANDOFF_FIELDS = ("verification", "confidence", "artifacts", "tables", "source_records", "source_coverage")


def _load_helper() -> ModuleType:
    """Load the remediation finalize helper by file path, as the skill invokes it."""
    path = PLUGIN_ROOT / "shared" / "remediation_finalize.py"
    spec = importlib.util.spec_from_file_location("codex_rig_remediation_finalize", path)
    assert spec is not None and spec.loader is not None
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
    assert "python PLUGIN_ROOT/shared/remediation_finalize.py --help" in contract
    assert "`--all-errors`" in contract


def test_review_finalize_derives_shared_fields_and_runs_both_validators(
    assessed_pr: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Finalize an assessed PR review in one call: shared handoff fields, render, write, both validators, promote.

    The review-specific tables stay agent-authored; only verification, confidence, and the result artifact are derived.
    The promoted result must still complete through the review finder, which reruns both validators.
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
