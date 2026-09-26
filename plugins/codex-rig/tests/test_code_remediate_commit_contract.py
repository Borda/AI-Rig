"""Regression checks for ownership-scoped remediation commit choices."""

from __future__ import annotations

from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
CODE_REMEDIATE_SKILL = PLUGIN_ROOT / "skills" / "code-remediate" / "SKILL.md"


def test_remediation_offers_post_gate_commit_modes() -> None:
    """Keep the commit decision after validated remediation work."""
    skill = CODE_REMEDIATE_SKILL.read_text(encoding="utf-8")
    commit_section = skill.split("### 12: Offer An Opt-In Commit After Verified Remediation", maxsplit=1)[1]

    assert "shared quality gates and result validation finish" in commit_section
    assert "- all at once" in commit_section
    assert "- group findings by topic" in commit_section
    assert "- each finding as a separate commit" in commit_section
    assert "- leave unstaged" in commit_section
    assert "able to represent all four feasible modes" in commit_section
    assert "otherwise invoke permitted async with all four choices" in commit_section
    assert (
        "Use plain chat only when neither native control is suitable; never hide a mode behind Other" in commit_section
    )
    assert "never omit a feasible mode to fit a menu limit" in commit_section
    assert "If an earlier explicit answer already supplies the mode" in commit_section
    assert "omit the question and reuse that authorization" in commit_section
    assert "Do not stage without an explicit valid answer bound to this plan" in commit_section
    assert "If authorization is missing and runtime cannot ask" in commit_section
    assert "Do not stage before this question" not in commit_section
    assert "silence, preselection, stale or duplicate replies cannot authorize staging" in commit_section


def test_remediation_finalization_cannot_skip_commit_disposition() -> None:
    """Keep failed and successful closeouts on the explicit commit checkpoint."""
    skill = CODE_REMEDIATE_SKILL.read_text(encoding="utf-8")
    preparation = skill.split("### 11: Write And Validate Result Artifact", maxsplit=1)[1].split("### 12:")[0]
    commit = skill.split("### 12: Offer An Opt-In Commit After Verified Remediation", maxsplit=1)[1]
    shared = (PLUGIN_ROOT / "shared" / "final-handoff-contract.md").read_text(encoding="utf-8")

    assert "final-handoff.json.commit_disposition" in preparation
    assert "Do not promote or emit terminal output yet: continue to step 12" in preparation
    assert "Do not ask for commit authorization while blocked" in preparation
    assert "Silence or unavailable input is not a decline" in preparation
    assert "A submitted question without an answer stays `pending`" in commit
    assert "Re-render and revalidate the candidate when disposition changes" in commit
    assert "Result validation alone must never skip this continuation" in shared


def test_explicit_commit_rechecks_environment_only_limit_and_cites_remaining_blockers() -> None:
    """Keep a user-requested commit actionable while making every refusal auditable."""
    skill = CODE_REMEDIATE_SKILL.read_text(encoding="utf-8")
    preparation = skill.split("### 11: Write And Validate Result Artifact", maxsplit=1)[1].split("### 12:")[0]
    commit = skill.split("### 12: Offer An Opt-In Commit After Verified Remediation", maxsplit=1)[1]
    shared = (PLUGIN_ROOT / "shared" / "final-handoff-contract.md").read_text(encoding="utf-8")

    assert "Environment-only exception" in preparation
    assert "## Explicit Commit Request" in preparation
    assert "## Remaining Verification" in preparation
    assert "controlling rule" in preparation
    assert "A later explicit `commit this` request reopens a previously blocked disposition" in commit
    assert "A failed result may permit a local commit only after an explicit user request" in shared


def test_remediation_commit_stages_only_proven_owned_paths() -> None:
    """Reject commits that could absorb user or overlapping worktree changes."""
    skill = CODE_REMEDIATE_SKILL.read_text(encoding="utf-8")
    commit_section = skill.split("### 12: Offer An Opt-In Commit After Verified Remediation", maxsplit=1)[1]
    fail_fast = skill.split("## Fail-fast Rules", maxsplit=1)[1].split("## Quality Gates", maxsplit=1)[0]

    assert "commit-baseline.json" in skill
    assert "including a lockfile such as `uv.lock`" in skill
    assert "git diff --cached --quiet" in commit_section
    assert "git add -- <paths>" in commit_section
    assert "Never use `git add .`, `git add -A`, a glob" in commit_section
    assert "partial-hunk staging" in commit_section
    assert "code-remediate-commit-scope-unsafe" in fail_fast
    assert "code-remediate-commit-grouping-unsafe" in fail_fast


def test_pr_remediation_establishes_destination_before_merge_and_rechecks_before_commit() -> None:
    """Keep source collection separate from mutation authority and preserve resumed work."""
    skill = CODE_REMEDIATE_SKILL.read_text(encoding="utf-8")
    prepare = skill.index("run its `prepare` action")
    merge = skill.index("git merge --no-commit --no-ff")
    commit = skill.split("### 12: Offer An Opt-In Commit After Verified Remediation", maxsplit=1)[1]
    assert prepare < merge
    assert "remediation_branch.py check" in commit
    assert "verify afterward that the recorded branch contains the new commit" in commit
    assert "never recollect with checkout merely to replace local remediation commits" in skill
    assert "Do not derive a replacement expected value from current HEAD" in skill


def test_legacy_resume_recovers_before_committing_without_repeating_mode_choice() -> None:
    """Keep an authorized topic commit reachable after legacy branch recovery."""
    skill = CODE_REMEDIATE_SKILL.read_text(encoding="utf-8")
    resume = skill.split("On resume, inspect", maxsplit=1)[1].split("\n\n", maxsplit=1)[0]
    commit = skill.split("### 12: Offer An Opt-In Commit After Verified Remediation", maxsplit=1)[1]
    assert "remediation-branch-recovered.json" in resume
    assert "never replace it or fall back after a failed check" in resume
    assert "run `recover`" in skill
    assert "last recorded authorized `--expected-head`" in skill
    assert "without asking the user to select a mode again" in skill
    assert "complete the legacy recovery procedure before staging" in commit
    assert "legacy generated-branch receipt is terminal" not in skill
