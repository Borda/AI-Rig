"""Regression checks for review-candidate validation and remediation recovery."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[2]
CODE_REVIEW_SKILL = PLUGIN_ROOT / "skills" / "code-review" / "SKILL.md"
CODE_REMEDIATE_SKILL = PLUGIN_ROOT / "skills" / "code-remediate" / "SKILL.md"


@pytest.mark.installed_plugin
def test_review_prior_choice_precedes_run_creation() -> None:
    """Avoid superseding a reusable completed report before offering the entry choice."""
    skill = CODE_REVIEW_SKILL.read_text(encoding="utf-8")
    entry = skill.split("### Entry: resume or previous completed review", 1)[1].split("### 01:", 1)[0]

    assert "--target" in entry and "--result" in entry
    assert "Reuse completed review" in entry and "Run fresh review" in entry
    assert "No eligible completed report means fresh review without a question" in entry
    assert "before creating or promoting another run" in entry
    assert "recorded revision" in entry and "current-head" in entry
    assert "first unmet checkpoint" in entry


@pytest.mark.installed_plugin
def test_review_shorthand_identity_resolves_before_previous_report_lookup() -> None:
    """Keep numeric and current-branch invocation on the same pre-allocation reuse route."""
    skill = CODE_REVIEW_SKILL.read_text(encoding="utf-8")
    entry = skill.split("### Entry: resume or previous completed review", 1)[1].split("### 01:", 1)[0]

    lookup = entry.index("--target <canonical PR URL>")
    assert entry.index("--canonical-pr-url <positive PR number>") < lookup
    assert entry.index("-- gh pr view --json url") < lookup
    assert "temporary identity file" in entry
    assert "all PR input forms" in entry
    assert "known canonical PR identity" not in entry


@pytest.mark.installed_plugin
def test_review_and_remediation_routes_remain_distinct_during_incomplete_handoff() -> None:
    """Keep calibrated checkout routes and incomplete-report continuation aligned with skills."""
    review = CODE_REVIEW_SKILL.read_text(encoding="utf-8")
    remediation = CODE_REMEDIATE_SKILL.read_text(encoding="utf-8")
    readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
    cases = json.loads((PLUGIN_ROOT / "runtime" / "calibration" / "behavioral-cases.json").read_text(encoding="utf-8"))[
        "cases"
    ]
    case = next(case for case in cases if case["id"] == "code-remediate-branch-continuity")
    prompt = case["prompt"]
    incomplete_handoff = remediation.split("matching-review-incomplete:", 1)[1].split("\n- ", 1)[0].lower()

    assert "uses `git worktree add --detach`" in review
    assert "It does not call `gh pr checkout`" in review
    assert "Remediation first invokes `gh pr checkout <canonical PR URL>`" in remediation
    assert "uses `git worktree add --detach`" in prompt
    assert "it does not call `gh pr checkout`" in prompt
    assert "continue current-online/user findings" in incomplete_handoff
    assert "requested report obligation open" in incomplete_handoff
    assert "missing requested pr review decision" in incomplete_handoff
    assert "only after an explicit continue choice" in incomplete_handoff
    assert "fresh review" in readme.lower()
    assert "requested report obligation open" in readme.lower()
    assert "silently switch to online-only remediation" not in readme.lower()


@pytest.mark.installed_plugin
def test_new_native_review_context_files_are_owned_by_the_preparation_producer() -> None:
    """Prevent parent-authored context files from colliding with frozen producer output."""
    skill = CODE_REVIEW_SKILL.read_text(encoding="utf-8")
    readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
    preparation = skill.split("#### Prepare and dispatch the complete wave", 1)[1].split("#### ", 1)[0]
    generic_passes = skill.split("For every triggered pass:", 1)[1].split("\n\nParent owns", 1)[0]

    assert "write only the focused Markdown briefs referenced by `review-briefs.json`" in preparation
    assert "`review_prepare.py prepare` owns `specialists/<role>-context.md`" in preparation
    assert "Do not create or overwrite those producer-owned context paths" in preparation
    assert "legacy or non-producer routes" in generic_passes
    assert "`review_prepare.py` owns generated" in readme
    assert "`specialists/<role>-context.md` files" in readme
    assert "`review_batches.py`" in readme


@pytest.mark.installed_plugin
def test_review_repair_question_runs_native_discovery_before_async() -> None:
    """Prevent generated repair approval from skipping discovery or replaying its live question."""
    skill = CODE_REVIEW_SKILL.read_text(encoding="utf-8")
    recovery = skill.split("### Reviewer validation recovery", 1)[1].split("\n### ", 1)[0]

    assert "native discovery checkpoint" in recovery
    assert "ALL_TOOLS" in recovery
    assert "Absence from the short tool list is not unavailability" in recovery
    assert "Unknown async rendering is unsuitable" in recovery
    assert "The selected control owns the question and options" in recovery
    assert "do not echo them in commentary or final output" in recovery


@pytest.mark.installed_plugin
def test_native_protocol_repair_resumes_the_retained_wave_without_reassessment() -> None:
    """Prevent an internal dispatch or representation error from abandoning valid review work."""
    skill = CODE_REVIEW_SKILL.read_text(encoding="utf-8")
    recovery = skill.split("### Reviewer validation recovery", 1)[1].split("\n### ", 1)[0]

    assert "prepare-repair" in recovery
    assert "incomplete-dispatch" in recovery
    assert "closure-evidence-shape" in recovery
    assert "same run and current wave" in recovery
    assert "one generated `_a2`" in recovery
    for invariant in (
        "Preserve every claim, severity, source coordinate, confidence and assessment",
        "Finding IDs remain unchanged except for the single proven local-ID token in `finding-id-namespace`",
        "retain the original raw ID and immutable qualified origin witness",
        "uniquely prove the original local ID and identical non-ID finding fields",
        "all other response bytes remain exact",
        "Ordinary ID validation remains strict",
    ):
        assert invariant in recovery
    assert "do not require a fresh complete PR review" in recovery
    assert "No substantive reassessment" in recovery


def test_code_review_preflights_specialist_manifest_before_candidate() -> None:
    """Prevent malformed spawned-attempt bookkeeping from leaving a candidate."""
    skill = CODE_REVIEW_SKILL.read_text(encoding="utf-8").lower()

    assert "--manifest-only" in skill
    assert "before writing `result.candidate.json`" in skill
    assert "one or two sequential attempts" in skill
    assert "never invent missing attempt provenance" in skill


@pytest.mark.installed_plugin
def test_review_closure_is_ordered_at_the_execution_checkpoint() -> None:
    """Prevent manifest preflight or drafted output from becoming the final review step."""
    skill = CODE_REVIEW_SKILL.read_text(encoding="utf-8")
    checkpoint = skill.split("### 12:", 1)[1].split("## Fail-fast Rules", 1)[0]
    actions = (
        "final_handoff.py render",
        "write-result.py",
        "review-specific validator",
        "shared validator",
        "promote",
        "find-review-report.py --complete-run",
    )
    positions = [checkpoint.index(action) for action in actions]
    assert positions == sorted(positions)
    assert "Preflight success is not review completion" in checkpoint
    assert "Emit only successful completion stdout verbatim" in checkpoint


@pytest.mark.installed_plugin
def test_review_gate_failure_keeps_artifact_closure_required() -> None:
    """Prevent direct check receipts from bypassing canonical failed-gate reconciliation."""
    skill = CODE_REVIEW_SKILL.read_text(encoding="utf-8")
    gates = skill.split("### 07:", 1)[1].split("### 08:", 1)[0]
    assert "direct-check receipts do not replace `gates.json`" in gates
    assert "status=fail" in gates
    assert "continue through step 12" in gates
    assert "Preserve the failed attempt" in gates


@pytest.mark.installed_plugin
def test_execution_failure_cannot_be_reclassified_as_inapplicable() -> None:
    """Keep failed execution visible when recovery hands a review to remediation."""
    quality = (PLUGIN_ROOT / "shared" / "quality-gates.md").read_text(encoding="utf-8")
    remediation = CODE_REMEDIATE_SKILL.read_text(encoding="utf-8")

    assert "execution failure never makes an applicable check `not-applicable`" in quality
    assert "archive runner-owned receipts under `gate-attempts/<NNN>`" in quality
    assert "reject failed-to-skipped reclassification" in quality
    assert (
        "rerun the full review without the explicit authorization below, or fall back to an older assessed report"
        in remediation
    )
    assert "continue independently authorized source-verified remediation" in remediation


def test_remediation_validates_unpromoted_candidate_read_only() -> None:
    """Diagnose an unpromoted review candidate without repairing or promoting the review run.

    The review run belongs to code-review; remediation that rewrote its manifest or promoted its candidate would certify
    a review it did not produce. A passing read-only check still sends the user back to code-review.
    """
    skill = CODE_REMEDIATE_SKILL.read_text(encoding="utf-8").lower()

    assert "matching-review-candidate-unpromoted" in skill
    assert "remediation never repairs, rewrites, rerenders, or promotes its `specialist-manifest.json`" in skill
    assert "review-specific validator, then the shared validator, read-only" in skill
    assert "re-run code-review on <review-run-directory> to finish or repair it" in skill
    assert "a passing read-only validation does not admit the candidate either" in skill
    assert "never consume `result.candidate.json` as a completed review" in skill
    assert "only its eligible finding records may be considered under preliminary finding intake" in skill
    assert "same parent thread" not in skill
    assert "promote it to `result.json`" not in skill


def test_remediation_preserves_exact_candidate_validation_failure() -> None:
    """Replace generic rerun advice with the actionable upstream validator code."""
    skill = CODE_REMEDIATE_SKILL.read_text(encoding="utf-8").lower()

    assert "review-candidate-validation.txt" in skill
    assert "manifest-invalid-attempt-count:<role>" in skill
    assert "never invent missing attempt provenance" in skill
    assert "fall back to an older assessed report" in skill
    assert "code-remediate-review-run-mutated" in skill


@pytest.mark.installed_plugin
def test_review_recovery_explains_rejection_and_declined_repair() -> None:
    """Prevent reviewer evidence rejection from becoming a repair-only dead end."""
    skill = CODE_REVIEW_SKILL.read_text(encoding="utf-8")
    assert "### Reviewer validation recovery" in skill
    recovery = skill.split("### Reviewer validation recovery", 1)[1].split("\n### ", 1)[0]

    for requirement in (
        "could not confirm",
        "encrypted",
        "not established",
        "separate canonical options `Approve` and `Deny`",
        "fresh sequential review",
        "explicitly required independent",
        "separate fallback plan",
        "normal completion checks",
    ):
        assert requirement in recovery


@pytest.mark.installed_plugin
def test_fetch_recovery_separates_unknown_cause_from_missing_source() -> None:
    """Keep download failures actionable without inventing access failure or unsafe fallback."""
    skill = CODE_REMEDIATE_SKILL.read_text(encoding="utf-8")
    assert "### Collection failure recovery" in skill
    recovery = skill.split("### Collection failure recovery", 1)[1].split("\n### ", 1)[0]

    for requirement in (
        "pr-head-fetch",
        "does not prove",
        "diagnosis",
        "Sequential execution",
        "no code edits",
        "accepts stale",
        "current PR head",
        "unchanged retry",
    ):
        assert requirement in recovery


@pytest.mark.installed_plugin
def test_existing_merge_recovery_requires_an_owned_concrete_choice() -> None:
    """Prevent unexplained cleanup demands, automatic aborts, and dirty-worktree overclaims."""
    skill = CODE_REMEDIATE_SKILL.read_text(encoding="utf-8")
    assert "### Existing merge or conflict recovery" in skill
    recovery = skill.split("### Existing merge or conflict recovery", 1)[1].split("\n### ", 1)[0]

    for requirement in (
        "MERGE_HEAD",
        "UU",
        "separate",
        "pull",
        "Finish",
        "Abort",
        "Defer",
        "explicit authorization",
        "pre-existing changes",
        "unrelated dirty files",
        "current PR head",
        "separate canonical options `Approve` and `Deny`",
        "a **Deny** selects **Defer**",
        "reprompt an action already authorized",
    ):
        assert requirement in recovery


@pytest.mark.installed_plugin
def test_readme_preserves_the_existing_rejected_evidence_review_exception() -> None:
    """Keep general role fallback wording from blocking fresh review after evidence rejection."""
    readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")

    assert "Code Review also permits its existing separate parent-sequential fallback" in readme
    assert "preserve that evidence as unaccepted and perform fresh parent inspection" in readme
    assert "Neither route permits fallback merely because a specialist disagreed" in readme


@pytest.mark.installed_plugin
def test_successful_remediation_recovery_resumes_the_active_workflow() -> None:
    """Keep verified conflict recovery from ending before the user's original remediation finishes."""
    skill = CODE_REMEDIATE_SKILL.read_text(encoding="utf-8")

    assert "Once an authorized recovery succeeds, resume the active code-remediate workflow" in skill
    assert "first unmet checkpoint" in skill
    assert "do not stop at conflict resolution or ask the user to rerun the skill" in skill
    assert "preserve and verify its recorded merge result" in skill


@pytest.mark.installed_plugin
def test_review_reports_existing_merge_and_hands_it_to_remediation() -> None:
    """Keep a review-only run from finishing, aborting, or authorizing an existing merge.

    Finish/abort/defer choices mutate the invoking worktree, which belongs to remediation; review inspects its own
    detached worktree and keeps going.
    """
    skill = CODE_REVIEW_SKILL.read_text(encoding="utf-8")
    guidance = skill.split("For an existing merge or conflict in the invoking worktree", 1)[1].split("\n\n", 1)[0]

    assert "never finishes, aborts, or resolves that merge" in guidance
    assert "never asks for that authorization" in guidance
    assert "belongs to `code-remediate`" in guidance
    assert "does not block collection or source review" in guidance
    assert "never as a PR finding" in guidance
    assert "resume the active code-review workflow" not in skill


@pytest.mark.installed_plugin
def test_remediation_distinguishes_patches_from_evidence_only_closure() -> None:
    """Prevent a target merge or green checks from being reported as implemented fixes."""
    skill = CODE_REMEDIATE_SKILL.read_text(encoding="utf-8")

    assert "No review findings were fixed by a new code change" in skill
    assert "target integration is not finding implementation" in skill
    assert "Passing gates do not close selected items" in skill
    assert "Verified without code changes:" in skill
    assert "Blocked:" in skill
    assert "Never render bare `unresolved`" in skill


@pytest.mark.installed_plugin
def test_remediation_prepares_usable_context_and_recovers_missing_evidence() -> None:
    """Prevent artifact-only text reviewers and missing coverage from ending safe recovery."""
    skill = CODE_REMEDIATE_SKILL.read_text(encoding="utf-8")

    assert "no-tools reviewer must receive the relevant source, diff, and evidence inline" in skill
    assert "artifact paths alone are not readable context" in skill
    assert "Missing coverage details are an investigation checkpoint" in skill
    assert "resume the selected finding" in skill


@pytest.mark.installed_plugin
def test_merge_resume_preserves_both_checkpoints_and_protected_path_redaction() -> None:
    """Prevent redaction conflicts or partial conflict resolution from bypassing merge gates."""
    remediation = CODE_REMEDIATE_SKILL.read_text(encoding="utf-8")
    review = CODE_REVIEW_SKILL.read_text(encoding="utf-8")

    assert "partial recovery, not a completed merge" in remediation
    assert "pre-merge source receipts" in remediation
    assert "post_merge_head" in remediation
    assert "do not insert protected path lists into the bound summary" in review.lower()


@pytest.mark.installed_plugin
@pytest.mark.parametrize("section", ["Routing rules:", "## Fail-fast Rules"])
def test_broad_routing_and_fail_fast_admit_complete_fast_native_coverage(section: str) -> None:
    """Prevent mandatory broad-review rules from rejecting already validated fast native coverage."""
    skill = (PLUGIN_ROOT / "skills/code-review/SKILL.md").read_text(encoding="utf-8")
    contract = skill.split(section, 1)[1].split("\n\n", 1)[1].split("\n\n", 1)[0]
    assert "validated current native all-role evidence" in contract
    assert "naturally fast uninterrupted dispatch" in contract.casefold()
    assert "narrowly validated capacity-limited schema-eight" not in contract
