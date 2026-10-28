"""Regression checks for bare-PR online-only remediation intake."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _skill_text import read_reference_body, read_with_references

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
CODE_REMEDIATE_SKILL = PLUGIN_ROOT / "skills" / "code-remediate" / "SKILL.md"
#: Section moved out of SKILL.md and loaded only when a requested PR review report is missing.
MISSING_REVIEW_DECISION = CODE_REMEDIATE_SKILL.parent / "references" / "missing-review-decision.md"


@pytest.mark.installed_plugin
def test_remediation_delivers_to_original_checkout_after_source_checks() -> None:
    """Prevent isolated remediation from finishing with fixes stranded outside the user's checkout."""
    skill = read_with_references(
        CODE_REMEDIATE_SKILL, "intake.md", "selection.md", "implementation.md", "completion.md"
    )
    contract = (PLUGIN_ROOT / "shared" / "native-skill-contract.md").read_text(encoding="utf-8")
    policy = contract.split("## Worktree lifecycle", maxsplit=1)[1].split("\n## ", maxsplit=1)[0]

    assert "original invoking checkout" in policy
    assert "before collection or source edits" in policy
    assert "explicit worktree-only" in policy
    assert "Review-only" in policy
    assert "never switch or deliver changes to the invoking checkout" in policy
    assert "staged, unstaged and untracked" in policy
    assert "Do not merge or cherry-pick" in policy
    assert "delivery remains incomplete" in policy
    assert "historical source receipt" in policy
    assert "Select the destination route before any transfer" in policy
    assert "verify in place without detaching, copying or requiring a clean source index" in policy
    assert "PR identity, tracking and ancestry-from-receipt checks apply only to PR mode" in policy
    assert "### Return remediation to the invoking checkout" in skill
    delivery = skill.split("### Return remediation to the invoking checkout", maxsplit=1)[1]
    assert "before final promotion and output" in delivery
    assert "no-commit and leave-unstaged" in delivery
    assert "worktree-delivery.md" in delivery
    assert "local fixes and optional commits remain there" not in skill


@pytest.mark.installed_plugin
def test_result_template_uses_allocated_run_for_all_artifact_paths() -> None:
    """Keep PR remediation result links bound to the promoted directory instead of a stale timestamp."""
    template = (CODE_REMEDIATE_SKILL.parent / "result-template.json").read_text(encoding="utf-8")
    result = json.loads(template)
    metadata = result["metadata"]

    assert result["artifact_path"] == "<run-directory>/result.json"
    assert metadata["final_handoff"]["handoff_path"] == "<run-directory>/final-handoff.json"
    assert metadata["final_handoff"]["rendered_path"] == "<run-directory>/final.md"
    assert metadata["final_handoff"]["validation_path"] == "<run-directory>/final-handoff.validation.json"
    assert ".reports/codex/code-remediate/<timestamp>" not in template


@pytest.mark.parametrize("skill_name", ["code-review", "code-remediate"])
def test_embedded_review_findings_are_individually_inventoried(skill_name: str) -> None:
    """Prevent a collected bot review from masking its independent nested findings."""
    skill_path = PLUGIN_ROOT / "skills" / skill_name / "SKILL.md"
    skill = (
        read_with_references(skill_path, "source-and-scope.md")
        if skill_name == "code-review"
        else read_with_references(skill_path, "intake.md", "selection.md", "implementation.md", "completion.md")
    )

    assert ("#### Embedded review findings" if skill_name == "code-review" else "### Embedded review findings") in skill
    assert "<parent-id>#finding-<ordinal>" in skill
    assert "before deduplication" in skill
    assert "Comments generated" in skill
    assert "same file or line alone" in skill
    assert "complete parent body" in skill
    assert "every nested finding" in skill


def test_bare_pr_targets_collect_online_evidence_without_review_artifact() -> None:
    """Keep bare PR remediation usable when no assessed report exists."""
    skill = read_with_references(
        CODE_REMEDIATE_SKILL, "intake.md", "selection.md", "implementation.md", "completion.md"
    )

    assert "bare number, `#number`, PR URL, and natural-language bare PR targets" in skill
    assert "collect current online items and verified local checkout" in skill
    assert "without a prior review report" in skill
    assert "explicit `+review`, `+report`, report aliases, and report paths retain report-plus-online behavior" in skill


def test_missing_findings_source_does_not_fail_bare_pr_route() -> None:
    """Keep the report-source fail-fast rule scoped to report aliases."""
    skill = read_with_references(
        CODE_REMEDIATE_SKILL, "intake.md", "selection.md", "implementation.md", "completion.md"
    )
    fail_fast = skill.split("## Fail-fast Rules", maxsplit=1)[1].split("## Quality Gates", maxsplit=1)[0]

    assert "Missing findings source in report mode, an explicit report path, or a report alias" in fail_fast
    assert "A bare PR has current online PR evidence as its findings source" in fail_fast
    assert "must not fail or request `code-review` merely because no assessed review artifact exists" in fail_fast
    assert "A bare PR target must not run this helper, scan prior review reports" in skill
    assert "For bare PR online-only intake, do not create `<run-directory>/findings-input.txt`" in skill
    assert (
        "When `REQUESTED_REPORT=true`, no matching code-review report means the requested assessed findings are missing"
        in skill
    )
    assert "Explain that first" in skill
    assert "Inspect that run's classified error and retained checkout diagnostics" in skill


def test_requested_review_does_not_replace_primary_remediation() -> None:
    """Require the missing-review decision before continuing authorized fixes."""
    skill = read_with_references(
        CODE_REMEDIATE_SKILL, "intake.md", "selection.md", "implementation.md", "completion.md"
    )
    intake = skill.split("### 02: Normalize input", maxsplit=1)[1].split("### 03:", maxsplit=1)[0]

    assert "`+review` joins review with remediation and authorizes the needed review step" in intake
    assert "`+report`, report aliases and explicit report paths request existing evidence only" in intake
    assert "Missing requested PR review decision" in intake
    assert "continue available findings only after explicit user confirmation" in intake
    assert "Disclose the requested review evidence as unavailable without manufacturing a finding" in intake
    assert "Remediation resumes only after the producer completes" not in intake
    assert "or switch to online-only intake" not in intake
    assert "complete its ordered artifact closure before intake" not in intake


def test_evidence_age_preserves_current_source_triage_and_bound_authorization() -> None:
    """Older online comments require source validation, while an additional review needs its own authority."""
    skill = read_with_references(
        CODE_REMEDIATE_SKILL, "intake.md", "selection.md", "implementation.md", "completion.md"
    )
    freshness = skill.split("### Review evidence freshness", maxsplit=1)[1].split("\n### ", maxsplit=1)[0]
    assert "saved reports and online review comments" in freshness
    assert "fetch time does not establish review freshness" in freshness
    assert "For bare PR online-only remediation" in freshness
    assert "not a new permission question" in freshness
    assert "Inspect each claim against the verified current source" in freshness
    assert "Do not start or offer an additional review solely because comments predate the head" in freshness
    assert "Use existing findings" in freshness
    assert "Update existing review" in freshness
    assert "Create a new review" in freshness
    assert "unknown" in freshness
    assert "`+review`" in freshness
    assert "do not ask again" in freshness
    assert "current source" in freshness
    assert "never edit or post GitHub comments" in freshness
    assert "set `REQUESTED_REPORT=true`" in freshness
    assert "retain online evidence" in freshness
    assert "exact returned canonical result" in freshness
    decision = MISSING_REVIEW_DECISION.read_text(encoding="utf-8")
    assert "joined `+review`" in decision
    assert "carry that authorization into Code Review" in decision


def test_preliminary_findings_keep_source_and_completion_boundaries() -> None:
    """Permit source-confirmed hypotheses without certifying an unfinished review."""
    skill = read_with_references(
        CODE_REMEDIATE_SKILL, "intake.md", "selection.md", "implementation.md", "completion.md"
    )

    assert "### Preliminary finding intake" in skill
    assert "matching exact PR identity, head, scope, and source evidence" in skill
    assert "triage each claim against the freshly verified current source" in skill
    assert "never present preliminary records as a validated review result" in skill
    assert "Selection, source checkout, target integration, and merge authorization gates still apply" in skill
    assert "do not certify unreviewed fixes as clean" in skill
    assert "Do not manufacture an assessed JSON report" in skill


def test_missing_requested_report_blocks_continuation_until_explicit_decision() -> None:
    """Prevent fail-fast guidance from silently bypassing a requested review."""
    skill = read_with_references(
        CODE_REMEDIATE_SKILL, "intake.md", "selection.md", "implementation.md", "completion.md"
    )
    fail_fast = skill.split("## Fail-fast Rules", maxsplit=1)[1].split("## Quality Gates", maxsplit=1)[0]

    assert "ask only for the missing finding evidence or selection" in skill
    assert "run `$code-review <target>` first or provide a report path" not in fail_fast
    assert "Missing requested PR review decision" in fail_fast
    assert "available findings require explicit user confirmation before continuation" in fail_fast


def test_missing_requested_pr_review_has_three_choices_and_preserves_pending_decisions() -> None:
    """Keep the user's recovery choices explicit without accepting preliminary evidence."""
    skill = read_with_references(
        CODE_REMEDIATE_SKILL, "intake.md", "selection.md", "implementation.md", "completion.md"
    )
    decision = read_reference_body(MISSING_REVIEW_DECISION, "Missing requested PR review decision")

    assert "missing-review-decision.md" in skill
    assert "`mode=pr` and `REQUESTED_REPORT=true`" in decision
    assert "explicit PR report paths" in decision
    assert "one native question naming the linked PR and exactly `New review`, `Resume last one`, `Stop`" in decision
    assert "normal full `code-review` workflow" in decision
    assert "both artifact validators, promotion and `--complete-run`" in decision
    assert "resume this same remediation run" in decision
    assert "only after an eligible completed result is admitted" in decision
    assert "`requested_report=true`" in decision
    assert "A dismissed or unanswered prompt authorizes none of its options" in decision
    assert "Bare PR online-only intake does not ask this question" in decision
    assert "stop the joined workflow, retain artifacts and changes, and do not continue remediation" in decision


def test_requested_review_sibling_routes_cannot_bypass_decision() -> None:
    """Make incomplete, unavailable and candidate evidence pass the same checkpoint."""
    skill = read_with_references(
        CODE_REMEDIATE_SKILL, "intake.md", "selection.md", "implementation.md", "completion.md"
    )
    incomplete = skill.split("- `matching-review-incomplete:", maxsplit=1)[1].split("\n- ", maxsplit=1)[0]
    missing = skill.split("- When `REQUESTED_REPORT=true`", maxsplit=1)[1].split("\n- ", maxsplit=1)[0]
    candidate = skill.split("For `matching-review-candidate-unpromoted:", maxsplit=1)[1].split(
        "### Missing requested PR review decision", maxsplit=1
    )[0]

    for route in (incomplete, missing, candidate):
        assert "Missing requested PR review decision" in route
    assert "then continue available current-online/user finding intake" not in missing


@pytest.mark.installed_plugin
def test_available_findings_cannot_bypass_missing_requested_review_approval() -> None:
    """Require a bound answer even when usable findings already exist."""
    skill = read_with_references(
        CODE_REMEDIATE_SKILL, "intake.md", "selection.md", "implementation.md", "completion.md"
    )
    decision = read_reference_body(MISSING_REVIEW_DECISION, "Missing requested PR review decision")
    assert "only after compatibility assessment and feasible automatic recovery" in decision
    assert "A version mismatch or repairable derived artifact alone does not trigger this question" in decision
    assert "`New review`, `Resume last one`, `Stop`" in decision
    assert "feasibility of each route" in decision
    assert "even when usable current-online/user findings or preliminary hypotheses exist" in decision
    assert "Continuing without a completed report requires explicit user confirmation" in decision
    assert "Honor existing bound authorization instead of asking again" in decision
    assert "A previously selected fresh-review-first route remains binding" in decision
    assert "Normal source verification and scope selection still precede edits" in decision
    for bypass in (
        "The default is available-finding continuation",
        "only when no usable finding evidence exists",
        "unless an explicit completed-review prerequisite applies",
        "an explicit continue choice is needed only",
        "available findings continue by default",
        "without blocking usable findings solely on report bookkeeping",
    ):
        assert bypass not in skill


@pytest.mark.installed_plugin
def test_unavailable_pr_question_links_target_without_claiming_source_review() -> None:
    """Keep failed collection identifiable without presenting artifact closure as a review."""
    decision = read_reference_body(MISSING_REVIEW_DECISION, "Missing requested PR review decision")
    branch = decision.split("  - d. ", 1)[1].split("\n- **Explicit free-text", 1)[0]

    assert "one native question naming the linked PR" in decision
    assert "Explain the actual collection failure or newer-run identity" in branch
    assert "never call unavailable closure a completed source review" in branch
    assert "supersession" in branch
    assert "same three-option Incomplete review decision" in branch
    assert "neither a new run nor a resume choice waives them" in branch
