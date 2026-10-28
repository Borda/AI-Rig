"""Regression checks for ownership-scoped remediation commit choices."""

from __future__ import annotations

from pathlib import Path

from _skill_text import read_with_references

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
CODE_REMEDIATE_SKILL = PLUGIN_ROOT / "skills" / "code-remediate" / "SKILL.md"


def test_remediation_recovers_late_baseline_from_retained_pre_edit_evidence_only() -> None:
    """Prevent agent-created dirtiness from being called user work or silently cleared."""
    skill = read_with_references(
        CODE_REMEDIATE_SKILL, "intake.md", "selection.md", "implementation.md", "completion.md"
    )
    assert "Late baseline capture is an agent process error" in skill
    assert "commit-ownership-recovery.md" in skill
    assert "retained pre-edit status and source snapshots" in skill
    assert "Current dirtiness alone proves neither pre-existing user edits nor agent ownership" in skill
    assert "never fabricate an empty baseline" in skill


def test_remediation_classifies_confidence_and_prepares_backend_evidence() -> None:
    """Keep real coverage actionable without making statistical disclaimers impossible chores."""
    skill = read_with_references(
        CODE_REMEDIATE_SKILL, "intake.md", "selection.md", "implementation.md", "completion.md"
    )
    assert "Project Dependency Preparation" in skill
    assert "before closing a missing-backend item as environment-blocked" in skill
    assert "A statistical-confidence disclaimer requires no invented calibration experiment" in skill
    assert "An explicitly required calibration experiment remains actionable" in skill
    assert "resume ledger, workplan, finalization, validation and promotion" in skill


def test_remediation_prepares_declared_dependencies_and_reruns_canonical_gates() -> None:
    """Prevent missing test packages from ending remediation before permitted environment recovery."""
    shared = (PLUGIN_ROOT / "shared" / "native-skill-contract.md").read_text(encoding="utf-8")
    preparation = shared.split("## Project Dependency Preparation", 1)[1].split("\n## ", 1)[0]
    assert "project's declared bootstrap command" in preparation
    assert "request the specific missing permission" in preparation
    assert "rerun the required source-bound canonical gate bundle" in preparation
    assert "same recorded source revision" in preparation
    assert "Never skip tests, weaken assertions" in preparation
    assert "Code Review retains its read-only" in preparation
    assert "missing package alone is not a terminal blocker" in preparation


def test_dependency_preparation_reaches_other_workflows_and_read_only_agents() -> None:
    """Require shared preparation ownership without giving read-only reviewers installation authority."""
    shared = (PLUGIN_ROOT / "shared" / "native-skill-contract.md").read_text(encoding="utf-8")
    preparation = shared.split("## Project Dependency Preparation", 1)[1].split("\n## ", 1)[0]
    assert "any workflow authorized to execute project code" in preparation
    assert "investigation, optimization, and delegated test/check work" in preparation
    assert "does not expand the workflow's write or execution authority" in preparation
    for name in ("quality-gates.md", "specialist-orchestration.md"):
        consumer = (PLUGIN_ROOT / "shared" / name).read_text(encoding="utf-8")
        assert "Project Dependency Preparation" in consumer
        assert "parent" in consumer
        assert "read-only" in consumer


def test_early_execution_paths_load_dependency_preparation_before_closeout() -> None:
    """Prevent early skill paths from bypassing shared preparation rules they have not loaded."""
    baseline = (PLUGIN_ROOT / "assets" / "AGENTS.md").read_text(encoding="utf-8")
    assert 'shared/native-skill-contract.md` §"Project Dependency Preparation"' in baseline
    routes = {
        "implement": ("## Lightweight Local Work", "## Execution Route Selection"),
        "optimize": ("### 02:", "### 03:"),
        "investigate": ("### 06:", "### 07:"),
    }
    for skill, (start, end) in routes.items():
        source = (PLUGIN_ROOT / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
        route = source.split(start, 1)[1].split(end, 1)[0]
        assert "Project Dependency Preparation" in route, skill
        assert "project-dependency-preparation" in route, skill


def test_optimization_rebaselines_both_commands_after_dependency_preparation() -> None:
    """Prevent environment preparation from appearing as a source-level optimization improvement."""
    source = (PLUGIN_ROOT / "skills" / "optimize" / "SKILL.md").read_text(encoding="utf-8")
    baseline = source.split("### 02:", 1)[1].split("### 03:", 1)[0]
    assert "rerun both baseline commands in the resulting environment" in baseline
    assert "invalidate earlier measurements" in baseline
    assert "iteration or resume" in baseline
    assert "no improvement claim" in baseline


def test_review_and_remediation_require_explicit_convergence_authorization() -> None:
    """Keep ordinary workflow invocation from silently starting an extra review-and-fix loop."""
    for name in ("code-review", "code-remediate"):
        skill = (PLUGIN_ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
        if name == "code-review":
            assert "A review alone does not authorize Challenge Resolve or a review-and-fix loop" in skill
            assert "Ask before adding an unapproved loop" in skill
            assert "reuse existing explicit approval for the same scope" in skill
            assert "If authorized, apply `../../shared/adversarial-loop.md`" in skill
        else:
            assert "Challenge Resolve is opt-in" in skill
            assert "invocation alone does not authorize" in skill
            assert "Existing explicit approval for the same scope remains valid" in skill
    remediation = read_with_references(
        CODE_REMEDIATE_SKILL, "intake.md", "selection.md", "implementation.md", "completion.md", "source-recovery.md"
    )
    fork = remediation.split("- **Fork PR:**", 1)[1].split("####", 1)[0]
    assert "separate explicit approval for the adversarial recovery loop" in fork


def test_remediation_single_pass_overrides_generic_convergence_rules() -> None:
    """Prevent closure verification or newly found work from starting an unsolicited loop."""
    skill = read_with_references(
        CODE_REMEDIATE_SKILL, "intake.md", "selection.md", "implementation.md", "completion.md"
    )
    baseline = (PLUGIN_ROOT / "assets" / "AGENTS.md").read_text(encoding="utf-8")
    shared = (PLUGIN_ROOT / "shared" / "native-skill-contract.md").read_text(encoding="utf-8")
    assert (
        "single pass: collect findings, resolve the selected work, obtain one bounded independent challenge of any implemented change, verify, and report"
        in skill
    )
    assert "Do not propose or ask approval for an unsolicited loop" in skill
    assert "No convergence score, loop ledger, or score-based stop applies" in skill
    assert "Newly discovered in-scope defects do not trigger another review wave" in skill
    assert "at most one closure-verification pass" in skill
    assert "Code Remediate remains single-pass with the bounded independent implementation challenge" in baseline
    shared_remediation = shared.split("Code Remediate remains a single pass:", 1)[1].split("\n\n", 1)[0]
    assert "collect findings, select authorized work before edits" in shared_remediation
    assert "resolve selected work, obtain the bounded independent implementation challenge" in shared_remediation
    assert (
        "normal remediation and its closure checks never imply that request or require convergence scoring"
        in shared_remediation
    )
    assert "only when the user explicitly requests it" in baseline
    assert "only when the user explicitly requests it" in shared


def test_remediation_offers_post_gate_commit_modes() -> None:
    """Keep the commit decision after validated remediation work."""
    skill = read_with_references(
        CODE_REMEDIATE_SKILL, "intake.md", "selection.md", "implementation.md", "completion.md"
    )
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
    assert (
        "Do not stage without a local Git approval grant or an explicit valid answer bound to this plan"
        in commit_section
    )
    assert "If authorization is missing and runtime cannot ask" in commit_section
    assert "Do not stage before this question" not in commit_section
    assert "silence, preselection, stale or duplicate replies cannot create authorization" in commit_section


def test_remediation_finalization_cannot_skip_commit_disposition() -> None:
    """Keep failed and successful closeouts on the explicit commit checkpoint."""
    skill = read_with_references(
        CODE_REMEDIATE_SKILL, "intake.md", "selection.md", "implementation.md", "completion.md"
    )
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


def test_explicit_commit_overrides_verification_readiness_and_discloses_failures() -> None:
    """Keep a user-requested commit actionable while making every refusal auditable."""
    skill = read_with_references(
        CODE_REMEDIATE_SKILL, "intake.md", "selection.md", "implementation.md", "completion.md"
    )
    preparation = skill.split("### 11: Write And Validate Result Artifact", maxsplit=1)[1].split("### 12:")[0]
    commit = skill.split("### 12: Offer An Opt-In Commit After Verified Remediation", maxsplit=1)[1]
    shared = "\n\n".join(
        (PLUGIN_ROOT / "shared" / name).read_text(encoding="utf-8")
        for name in ("final-handoff-contract.md", "final-handoff-code-remediate.md")
    )
    native = (PLUGIN_ROOT / "shared" / "native-skill-contract.md").read_text(encoding="utf-8")

    assert "Explicit local snapshot exception" in preparation
    assert "failed canonical checks" in preparation
    assert "known regression" in preparation
    assert "## Explicit Commit Request" in preparation
    assert "## Remaining Verification" in preparation
    assert "controlling rule" in preparation
    assert "an answered `all at once`" in preparation
    assert "Do not downgrade a recorded commit-mode choice to a grouping preference" in preparation
    assert "retains that authorization" in commit
    assert "A later explicit `commit this` request reopens a previously blocked disposition" in commit
    assert "explicit local snapshot exception" in commit
    assert "including when the plan is prepared in response to it; do not ask again" in commit
    assert (
        "A failed or timed-out result may permit a local snapshot after an explicit user request, including an answered commit-mode choice,"
        in shared
    )
    assert "explicitly requested reversible local action" in native
    assert "Never invent a blocker" in native


def test_remediation_commit_stages_only_proven_owned_paths() -> None:
    """Reject commits that could absorb user or overlapping worktree changes."""
    skill = read_with_references(
        CODE_REMEDIATE_SKILL, "intake.md", "selection.md", "implementation.md", "completion.md"
    )
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


def test_early_commit_keeps_required_checks_without_requiring_snapshot_success() -> None:
    """Prevent early commit instructions from overriding the failed-check snapshot exception."""
    completion = (PLUGIN_ROOT / "skills/code-remediate/references/completion.md").read_text(encoding="utf-8")
    early_commit = completion.split("### 12:", 1)[1].split("\n\n", 2)[1]

    assert "Run and report required checks and retain their evidence" in early_commit
    assert "Ordinary readiness requires passing checks" in early_commit
    assert "explicit local snapshot may proceed with failed verification under step 11" in early_commit
    assert "ownership, destination and runtime restrictions remain binding" in early_commit
    assert "This exception does not waive required checks" not in early_commit


def test_commit_fail_fast_preserves_explicit_failed_verification_snapshot() -> None:
    """Keep fail-fast sequencing from restoring a passing-check prerequisite for snapshots."""
    completion = (PLUGIN_ROOT / "skills/code-remediate/references/completion.md").read_text(encoding="utf-8")
    sequencing = completion.split("43a.", 1)[1].split("43b.", 1)[0]

    assert "Run and report required checks and retain their evidence" in sequencing
    assert "Ordinary readiness requires passing checks" in sequencing
    assert "explicit local snapshot may proceed with failed verification under step 11" in sequencing
    assert "ownership, destination and runtime restrictions remain binding" in sequencing
    assert "after applicable verification and shared commit checks pass" not in sequencing


def test_pr_remediation_establishes_destination_before_merge_and_rechecks_before_commit() -> None:
    """Keep source collection separate from mutation authority and preserve resumed work."""
    skill = read_with_references(
        CODE_REMEDIATE_SKILL, "intake.md", "selection.md", "implementation.md", "completion.md"
    )
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
    own = read_with_references(CODE_REMEDIATE_SKILL, "intake.md", "selection.md", "implementation.md", "completion.md")
    skill = read_with_references(
        CODE_REMEDIATE_SKILL, "intake.md", "selection.md", "implementation.md", "completion.md", "source-recovery.md"
    )
    resume = own.split("On resume, inspect", maxsplit=1)[1].split("\n\n", maxsplit=1)[0]
    commit = own.split("### 12: Offer An Opt-In Commit After Verified Remediation", maxsplit=1)[1]
    assert "remediation-branch-recovered.json" in resume
    assert "never replace it or fall back after a failed check" in resume
    assert "run `recover`" in skill
    assert "last recorded authorized `--expected-head`" in skill
    assert "without asking the user to select a mode again" in skill
    assert "complete the legacy recovery procedure before staging" in commit
    assert "legacy generated-branch receipt is terminal" not in skill


def test_explicit_commit_sequence_override_reaches_handoff_consumer() -> None:
    """Allow a verified requested commit before report validation without bypassing source or ownership gates."""
    skill = read_with_references(
        CODE_REMEDIATE_SKILL, "intake.md", "selection.md", "implementation.md", "completion.md"
    )
    handoff = (PLUGIN_ROOT / "shared/final-handoff-code-remediate.md").read_text(encoding="utf-8")
    commit = skill.split("### 12:", 1)[1].split("## Fail-fast Rules", 1)[0]
    assert "Do not wait solely for final report validation" in commit
    assert "ordinary readiness and explicit local snapshot rules in step 11 still apply" in commit
    assert "## Remaining Report Checkpoint" in commit
    assert "preserve its hashes and `committed` disposition" in skill
    assert "before final report validation" in handoff
    assert "then return with actual hashes/disposition to complete validation and promotion" in handoff
    assert "Standing approval or grouping alone does not invoke this sequencing override" in handoff


def test_selected_commit_cannot_finish_with_owned_residue() -> None:
    """Keep new files, incomplete fixes and late worktree changes from escaping a selected commit."""
    skill = read_with_references(
        CODE_REMEDIATE_SKILL, "intake.md", "selection.md", "implementation.md", "completion.md"
    )
    gate = skill.split("### Selected commit completion gate", maxsplit=1)[1].split(
        "### Return remediation", maxsplit=1
    )[0]
    assert "before final output" in gate
    assert "staged, unstaged and all untracked entries" in gate
    assert "zero task-owned uncommitted entries" in gate
    assert "newly created files and deletions" in gate
    assert "Unrelated pre-existing changes remain untouched" in gate
    assert "After verified delivery, later drift fails the final recheck" in gate
    assert "no-commit/leave-unstaged decision takes precedence" in gate
    assert "all authorized owned edits, additions and deletions" in skill
