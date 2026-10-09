"""Regression checks for ownership-scoped remediation commit choices."""

from __future__ import annotations

from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
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
    assert "otherwise use the packaged native `ask_user` form with all four choices" in commit_section
    assert (
        "Use plain chat only when no permitted native control is suitable; never hide a mode behind Other"
        in commit_section
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
    shared = "\n\n".join(
        (PLUGIN_ROOT / "shared" / name).read_text(encoding="utf-8")
        for name in ("final-handoff-contract.md", "final-handoff-code-remediate.md")
    )

    assert "final-handoff.json.commit_disposition" in preparation
    assert "Do not promote or emit terminal output yet: continue to step 12" in preparation
    assert "Do not ask for commit authorization while blocked" in preparation
    assert "Silence or unavailable input is not a decline" in preparation
    assert "A submitted question without an answer stays `pending`" in commit
    assert "Re-render and revalidate the candidate when disposition changes" in commit
    assert "Result validation alone must never skip this continuation" in shared


def test_explicit_commit_rechecks_external_obligation_limit_and_cites_remaining_blockers() -> None:
    """Keep a user-requested commit actionable while making every refusal auditable."""
    skill = CODE_REMEDIATE_SKILL.read_text(encoding="utf-8")
    preparation = skill.split("### 11: Write And Validate Result Artifact", maxsplit=1)[1].split("### 12:")[0]
    commit = skill.split("### 12: Offer An Opt-In Commit After Verified Remediation", maxsplit=1)[1]
    shared = "\n\n".join(
        (PLUGIN_ROOT / "shared" / name).read_text(encoding="utf-8")
        for name in ("final-handoff-contract.md", "final-handoff-code-remediate.md")
    )
    native = (PLUGIN_ROOT / "shared" / "native-skill-contract.md").read_text(encoding="utf-8")

    assert "External-obligation exception" in preparation
    assert "independent review owned by an external reviewer or maintainer" in preparation
    assert "## Explicit Commit Request" in preparation
    assert "## Remaining Verification" in preparation
    assert "controlling rule" in preparation
    assert "A later explicit `commit this` request reopens a previously blocked disposition" in commit
    assert "external-obligation exception" in commit
    assert "including when the plan is prepared in response to it; do not ask again" in commit
    assert "A failed result may permit a local commit only after an explicit user request" in shared
    assert "explicitly requested reversible local action" in native
    assert "Never invent a blocker" in native


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
    assert "stage and commit the unit with the shared template's one owning command" in commit_section
    assert "`git --no-pager show --no-renames --name-only -z --format= HEAD`" in commit_section
    assert "equal the unit's planned paths exactly as a set" in commit_section
    assert "A mismatch stops before commit" not in commit_section
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


def test_explicit_commit_sequence_override_reaches_handoff_consumer() -> None:
    """Allow a verified requested commit before report validation without bypassing source or ownership gates."""
    skill = CODE_REMEDIATE_SKILL.read_text(encoding="utf-8")
    handoff = (PLUGIN_ROOT / "shared/final-handoff-code-remediate.md").read_text(encoding="utf-8")
    commit = skill.split("### 12:", 1)[1].split("## Fail-fast Rules", 1)[0]
    assert "Do not wait solely for final report validation" in commit
    assert "failed-gate and external-obligation rules in step 11 still apply" in commit
    assert "## Remaining Report Checkpoint" in commit
    assert "preserve its hashes and `committed` disposition" in skill
    assert "before final report validation" in handoff
    assert "then return with actual hashes/disposition to complete validation and promotion" in handoff
    assert "Grouping alone does not invoke this sequencing override" in handoff
