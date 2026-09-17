"""Regression checks for review-candidate validation and remediation recovery."""

from __future__ import annotations

from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
CODE_REVIEW_SKILL = PLUGIN_ROOT / "skills" / "code-review" / "SKILL.md"
CODE_REMEDIATE_SKILL = PLUGIN_ROOT / "skills" / "code-remediate" / "SKILL.md"


def test_code_review_preflights_specialist_manifest_before_candidate() -> None:
    """Prevent malformed spawned-attempt bookkeeping from leaving a candidate."""
    skill = CODE_REVIEW_SKILL.read_text(encoding="utf-8").lower()

    assert "--manifest-only" in skill
    assert "before writing `result.candidate.json`" in skill
    assert "one or two sequential attempts" in skill
    assert "never invent missing attempt provenance" in skill


def test_remediation_revalidates_same_session_candidate_before_promotion() -> None:
    """Recover a valid candidate without bypassing either review validator."""
    skill = CODE_REMEDIATE_SKILL.read_text(encoding="utf-8").lower()

    assert "matching-review-candidate-unpromoted" in skill
    assert "same parent thread" in skill
    assert "review-specific validator, then the shared validator" in skill
    assert "promote it to `result.json` only after both validators pass" in skill
    assert "never consume `result.candidate.json` directly" in skill


def test_remediation_preserves_exact_candidate_validation_failure() -> None:
    """Replace generic rerun advice with the actionable upstream validator code."""
    skill = CODE_REMEDIATE_SKILL.read_text(encoding="utf-8").lower()

    assert "review-candidate-validation.txt" in skill
    assert "manifest-invalid-attempt-count:<role>" in skill
    assert "return to the code-review manifest preflight checkpoint" in skill
    assert "one evidence-preserving repair" in skill
    assert "never invent missing attempt provenance" in skill
    assert "do not fall back to an older assessed report" in skill


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
        "(Approve / Deny)",
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
        "(Approve / Deny)",
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
@pytest.mark.parametrize("skill_name", ["code-review", "code-remediate"])
def test_successful_recovery_resumes_the_active_workflow(skill_name: str) -> None:
    """Keep verified conflict recovery from ending before the user's original task finishes."""
    skill = (PLUGIN_ROOT / "skills" / skill_name / "SKILL.md").read_text(encoding="utf-8")

    assert f"Once an authorized recovery succeeds, resume the active {skill_name} workflow" in skill
    assert "first unmet checkpoint" in skill
    assert "do not stop at conflict resolution or ask the user to rerun the skill" in skill
    if skill_name == "code-remediate":
        assert "preserve and verify its recorded merge result" in skill


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
    assert "do not insert protected path lists into the bound summary" in review
