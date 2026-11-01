"""Regression checks for ownership-scoped remediation commit choices and the dependency-preparation contract."""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from pathlib import Path

import pytest
from _contract_text import absent_phrases
from _skill_text import read_with_references

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
CODE_REMEDIATE_SKILL = PLUGIN_ROOT / "skills" / "code-remediate" / "SKILL.md"
_REFERENCES = ("intake.md", "selection.md", "implementation.md", "completion.md")
_COMMIT_STEP = "### 12: Offer An Opt-In Commit After Verified Remediation"
_RESULT_STEP = "### 11: Write And Validate Result Artifact"


def _after(text: str, marker: str) -> str:
    """Return everything after the first occurrence of `marker`."""
    return text.split(marker, 1)[1]


def _between(text: str, start: str, end: str) -> str:
    """Return the text from just after `start` up to the next `end`."""
    return _after(text, start).split(end, 1)[0]


def _paragraph(text: str, marker: str, index: int = 0) -> str:
    """Return paragraph `index` (blank-line separated) counted from the line holding `marker`."""
    return _after(text, marker).split("\n\n", index + 1)[index]


def _present(text: str, *phrases: str) -> list[str]:
    """Return the forbidden phrases found in `text`."""
    return [phrase for phrase in phrases if phrase in text]


def _read(*parts: str) -> str:
    """Read a plugin file given as path parts below the plugin root."""
    return PLUGIN_ROOT.joinpath(*parts).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def skill() -> str:
    """Return the Code Remediate skill with its intake, selection, implementation, and completion references."""
    return read_with_references(CODE_REMEDIATE_SKILL, *_REFERENCES)


@pytest.fixture(scope="module")
def skill_with_recovery() -> str:
    """Return the Code Remediate skill including the source-recovery reference."""
    return read_with_references(CODE_REMEDIATE_SKILL, *_REFERENCES, "source-recovery.md")


@pytest.fixture(scope="module")
def commit_step(skill: str) -> str:
    """Return the skill text from the opt-in commit step to the end."""
    return _after(skill, _COMMIT_STEP)


@pytest.fixture(scope="module")
def result_step(skill: str) -> str:
    """Return the result-artifact step that precedes the commit step."""
    return _between(skill, _RESULT_STEP, "### 12:")


@pytest.fixture(scope="module")
def handoff() -> str:
    """Return the final-handoff contract documents joined, as the commit steps cite them together."""
    return "\n\n".join(
        _read("shared", name) for name in ("final-handoff-contract.md", "final-handoff-code-remediate.md")
    )


@pytest.fixture(scope="module")
def native_contract() -> str:
    """Return the shared native skill contract."""
    return _read("shared", "native-skill-contract.md")


@pytest.fixture(scope="module")
def preparation(native_contract: str) -> str:
    """Return the shared Project Dependency Preparation section."""
    return _between(native_contract, "## Project Dependency Preparation", "\n## ")


@pytest.fixture(scope="module")
def baseline() -> str:
    """Return the packaged global baseline instructions."""
    return _read("assets", "AGENTS.md")


def test_late_baseline_recovers_from_retained_pre_edit_evidence_only(skill: str) -> None:
    """Prevent agent-created dirtiness from being called user work or silently cleared.

    When the commit baseline was captured late, only retained pre-edit status and source snapshots may rebuild it;
    current dirtiness proves neither user edits nor agent ownership, and an empty baseline is never invented.
    """
    assert (
        absent_phrases(
            skill,
            "Late baseline capture is an agent process error",
            "commit-ownership-recovery.md",
            "retained pre-edit status and source snapshots",
            "Current dirtiness alone proves neither pre-existing user edits nor agent ownership",
            "never fabricate an empty baseline",
        )
        == []
    )


def test_confidence_disclaimers_stay_actionable_and_backend_evidence_is_prepared(skill: str) -> None:
    """Keep real coverage actionable without making statistical disclaimers impossible chores.

    A missing backend is prepared before it can close an item as environment-blocked, and a statistical-confidence
    disclaimer never forces an invented calibration experiment while an explicitly required one stays actionable.
    """
    assert (
        absent_phrases(
            skill,
            "Project Dependency Preparation",
            "before closing a missing-backend item as environment-blocked",
            "A statistical-confidence disclaimer requires no invented calibration experiment",
            "An explicitly required calibration experiment remains actionable",
            "resume ledger, workplan, finalization, validation and promotion",
        )
        == []
    )


class TestDependencyPreparation:
    """Missing declared dependencies are prepared and canonical gates rerun before a workflow may stop."""

    def test_prepares_declared_dependencies_and_reruns_canonical_gates(self, preparation: str) -> None:
        """Prevent missing test packages from ending remediation before permitted environment recovery.

        Preparation uses the project's own bootstrap, asks for the one missing permission, and reruns the same gate
        bundle against the same recorded revision rather than weakening tests.
        """
        assert (
            absent_phrases(
                preparation,
                "project's declared bootstrap command",
                "request the specific missing permission",
                "rerun the required source-bound canonical gate bundle",
                "same recorded source revision",
                "Never skip tests, weaken assertions",
                "Code Review retains its read-only",
                "missing package alone is not a terminal blocker",
            )
            == []
        )

    def test_reaches_other_workflows_without_expanding_authority(self, preparation: str) -> None:
        """Require shared preparation ownership without giving read-only reviewers installation authority.

        The shared preparation section is read as written. It must reach every workflow allowed to execute project code,
        including investigation, optimization and delegated checks, while stating that no write authority is added.
        """
        assert (
            absent_phrases(
                preparation,
                "any workflow authorized to execute project code",
                "investigation, optimization, and delegated test/check work",
                "does not expand the workflow's write or execution authority",
            )
            == []
        )

    @pytest.mark.parametrize("name", ["quality-gates.md", "specialist-orchestration.md"])
    def test_consumers_keep_preparation_with_the_parent_for_read_only_agents(self, name: str) -> None:
        """Delegated read-only agents hand preparation back to their parent instead of installing anything itself.

        The quality-gates and specialist-orchestration shared docs are the consumers a delegated agent reads. Each must
        cite the preparation section and keep it with the parent for read-only agents, or a reviewer might install.
        """
        consumer = _read("shared", name)

        assert absent_phrases(consumer, "Project Dependency Preparation", "parent", "read-only") == []

    def test_global_baseline_points_at_the_shared_section(self, baseline: str) -> None:
        """Early execution paths that skip the skill body still find the shared preparation rules.

        The global baseline is what every session loads first. It must point at the named native-contract section, so a
        run that never opens a skill body still learns where dependency preparation is defined.
        """
        assert 'shared/native-skill-contract.md` §"Project Dependency Preparation"' in baseline

    @pytest.mark.parametrize(
        ("skill_name", "start", "end"),
        [
            pytest.param("implement", "## Lightweight Local Work", "## Execution Route Selection", id="implement"),
            pytest.param("optimize", "### 02:", "### 03:", id="optimize"),
            pytest.param("investigate", "### 06:", "### 07:", id="investigate"),
        ],
    )
    def test_early_execution_paths_load_preparation_before_closeout(
        self, skill_name: str, start: str, end: str
    ) -> None:
        """Prevent early skill paths from bypassing shared preparation rules they have not loaded.

        Implement, optimize and investigate each have an early step that can close out before the shared rules load. The
        text of that step must itself name the preparation section and reference its file.
        """
        route = _between(_read("skills", skill_name, "SKILL.md"), start, end)

        assert absent_phrases(route, "Project Dependency Preparation", "project-dependency-preparation") == []

    def test_optimization_rebaselines_both_commands_after_preparation(self) -> None:
        """Prevent environment preparation from appearing as a source-level optimization improvement.

        Preparing dependencies changes the measuring environment, so both baseline commands run again and earlier
        numbers are void; otherwise the setup itself would read as an improvement.
        """
        baseline = _between(_read("skills", "optimize", "SKILL.md"), "### 02:", "### 03:")

        assert (
            absent_phrases(
                baseline,
                "rerun both baseline commands in the resulting environment",
                "invalidate earlier measurements",
                "iteration or resume",
                "no improvement claim",
            )
            == []
        )


class TestConvergenceAuthorization:
    """Review and remediation never start an extra review-and-fix loop without explicit user authorization."""

    def test_review_needs_explicit_authorization_for_a_loop(self) -> None:
        """Keep ordinary Code Review invocation from silently starting an extra review-and-fix loop.

        The code-review skill text must say a review alone grants no loop, require asking before an unapproved loop,
        allow reusing existing approval for the same scope, and defer to the shared loop contract once authorized.
        """
        review = _read("skills", "code-review", "SKILL.md")

        assert (
            absent_phrases(
                review,
                "A review alone does not authorize Challenge Resolve or a review-and-fix loop",
                "Ask before adding an unapproved loop",
                "reuse existing explicit approval for the same scope",
                "If authorized, apply `../../shared/adversarial-loop.md`",
            )
            == []
        )

    def test_remediation_challenge_resolve_is_opt_in(self) -> None:
        """Keep ordinary Code Remediate invocation from silently starting an extra review-and-fix loop.

        The code-remediate skill text must label Challenge Resolve opt-in, say the invocation alone does not authorize
        it, and keep earlier explicit approval for the same scope valid.
        """
        remediate = _read("skills", "code-remediate", "SKILL.md")

        assert (
            absent_phrases(
                remediate,
                "Challenge Resolve is opt-in",
                "invocation alone does not authorize",
                "Existing explicit approval for the same scope remains valid",
            )
            == []
        )

    def test_fork_pr_recovery_needs_separate_approval(self, skill_with_recovery: str) -> None:
        """A fork PR's adversarial recovery loop needs its own approval, apart from the remediation request.

        The Fork PR bullet of the recovery text is isolated. Approving remediation of a fork must not imply approval of
        the loop, so that bullet has to require a separate explicit approval.
        """
        fork = _between(skill_with_recovery, "- **Fork PR:**", "####")

        assert "separate explicit approval for the adversarial recovery loop" in fork

    def test_single_pass_overrides_generic_convergence_rules(
        self, skill: str, baseline: str, native_contract: str
    ) -> None:
        """Prevent closure verification or newly found work from starting an unsolicited loop.

        The remediation workflow is one pass with one bounded independent challenge; the generic convergence score and
        loop ledger apply only when the user explicitly asks for them.
        """
        shared_remediation = _paragraph(native_contract, "Code Remediate remains a single pass:")

        assert (
            absent_phrases(
                skill,
                "single pass: collect findings, resolve the selected work, obtain one bounded independent challenge of any implemented change, verify, and report",
                "Do not propose or ask approval for an unsolicited loop",
                "No convergence score, loop ledger, or score-based stop applies",
                "Newly discovered in-scope defects do not trigger another review wave",
                "at most one closure-verification pass",
            )
            == []
        )
        assert (
            absent_phrases(
                baseline,
                "Code Remediate remains single-pass with the bounded independent implementation challenge",
                "only when the user explicitly requests it",
            )
            == []
        )
        assert (
            absent_phrases(
                shared_remediation,
                "collect findings, select authorized work before edits",
                "resolve selected work, obtain the bounded independent implementation challenge",
                "normal remediation and its closure checks never imply that request or require convergence scoring",
            )
            == []
        )
        assert "only when the user explicitly requests it" in native_contract


class TestCommitDisposition:
    """The commit decision follows validated remediation work and stages only paths the run provably owns."""

    def test_offers_post_gate_commit_modes(self, commit_step: str) -> None:
        """Keep the commit decision after validated remediation work.

        All four modes are offered through a control that can represent them all; an earlier explicit answer is reused
        instead of asked again, and silence or stale replies never count as authorization.
        """
        assert (
            absent_phrases(
                commit_step,
                "shared quality gates and result validation finish",
                "- all at once",
                "- group findings by topic",
                "- each finding as a separate commit",
                "- leave unstaged",
                "able to represent all four feasible modes",
                "otherwise use the packaged native `ask_user` form with all four choices",
                "Use plain chat only when no permitted native control is suitable; never hide a mode behind Other",
                "never omit a feasible mode to fit a menu limit",
                "If an earlier explicit answer already supplies the mode",
                "omit the question and reuse that authorization",
                "Do not stage without a local Git approval grant or an explicit valid answer bound to this plan",
                "If authorization is missing and runtime cannot ask",
                "silence, preselection, stale or duplicate replies cannot create authorization",
            )
            == []
        )
        assert _present(commit_step, "Do not stage before this question") == []

    def test_finalization_cannot_skip_commit_disposition(
        self, result_step: str, commit_step: str, handoff: str
    ) -> None:
        """Keep failed and successful closeouts on the explicit commit checkpoint.

        The result step, commit step and final handoff are read together. Result validation must continue into the
        commit disposition, a pending question must stay pending, and silence must never count as a decline.
        """
        assert (
            absent_phrases(
                result_step,
                "final-handoff.json.commit_disposition",
                "Do not promote or emit terminal output yet: continue to step 12",
                "Do not ask for commit authorization while blocked",
                "Silence or unavailable input is not a decline",
            )
            == []
        )
        assert (
            absent_phrases(
                commit_step,
                "A submitted question without an answer stays `pending`",
                "Re-render and revalidate the candidate when disposition changes",
            )
            == []
        )
        assert "Result validation alone must never skip this continuation" in handoff

    def test_explicit_commit_overrides_verification_readiness_and_discloses_failures(
        self, result_step: str, commit_step: str, handoff: str, native_contract: str
    ) -> None:
        """Keep a user-requested commit actionable while making every refusal auditable.

        A failed or timed-out result may still be snapshotted locally on an explicit request, but the report must say
        which checks failed and what verification remains.
        """
        assert (
            absent_phrases(
                result_step,
                "Explicit local snapshot exception",
                "failed canonical checks",
                "known regression",
                "## Explicit Commit Request",
                "## Remaining Verification",
                "controlling rule",
                "an answered `all at once`",
                "Do not downgrade a recorded commit-mode choice to a grouping preference",
            )
            == []
        )
        assert (
            absent_phrases(
                commit_step,
                "retains that authorization",
                "A later explicit `commit this` request reopens a previously blocked disposition",
                "explicit local snapshot exception",
                "including when the plan is prepared in response to it; do not ask again",
            )
            == []
        )
        assert (
            "A failed or timed-out result may permit a local snapshot after an explicit user request, including an answered commit-mode choice,"
            in handoff
        )
        assert (
            absent_phrases(native_contract, "explicitly requested reversible local action", "Never invent a blocker")
            == []
        )

    def test_stages_only_proven_owned_paths(self, skill: str, commit_step: str) -> None:
        """Reject commits that could absorb user or overlapping worktree changes.

        The skill and commit step must record a baseline, stage named paths only, forbid blanket staging, compare the
        committed paths with the plan as a set, and name the fail-fast codes for unsafe scope and grouping.
        """
        fail_fast = _between(skill, "## Fail-fast Rules", "## Quality Gates")

        assert absent_phrases(skill, "commit-baseline.json", "including a lockfile such as `uv.lock`") == []
        assert (
            absent_phrases(
                commit_step,
                "git diff --cached --quiet",
                "git add -- <paths>",
                "Never use `git add .`, `git add -A`, a glob",
                "stage and commit the unit with the shared template's one owning command",
                "`git --no-pager show --no-renames --name-only -z --format= HEAD`",
                "equal the unit's planned paths exactly as a set",
                "partial-hunk staging",
            )
            == []
        )
        assert _present(commit_step, "A mismatch stops before commit") == []
        assert (
            absent_phrases(fail_fast, "code-remediate-commit-scope-unsafe", "code-remediate-commit-grouping-unsafe")
            == []
        )

    @pytest.mark.parametrize(
        ("extract", "forbidden"),
        [
            pytest.param(
                partial(_paragraph, marker="### 12:", index=1),
                "This exception does not waive required checks",
                id="early-commit-step",
            ),
            pytest.param(
                lambda text: _between(text, "43a.", "43b."),
                "after applicable verification and shared commit checks pass",
                id="fail-fast-sequencing",
            ),
        ],
    )
    def test_snapshot_keeps_required_checks_without_requiring_their_success(
        self, extract: Callable[[str], str], forbidden: str
    ) -> None:
        """Neither the early commit step nor the fail-fast sequence may restore a passing-check prerequisite.

        An explicit local snapshot may proceed with failed verification under step 11, yet required checks still run and
        ownership, destination and runtime restrictions stay binding.
        """
        section = extract(_read("skills", "code-remediate", "references", "completion.md"))

        assert (
            absent_phrases(
                section,
                "Run and report required checks and retain their evidence",
                "Ordinary readiness requires passing checks",
                "explicit local snapshot may proceed with failed verification under step 11",
                "ownership, destination and runtime restrictions remain binding",
            )
            == []
        )
        assert _present(section, forbidden) == []

    def test_pr_destination_is_established_before_merge_and_rechecked_before_commit(
        self, skill: str, commit_step: str
    ) -> None:
        """Keep source collection separate from mutation authority and preserve resumed work.

        The skill must run its `prepare` action before any merge, recheck the branch around the commit, and never
        recollect with checkout or derive an expected value from the current HEAD, which would discard local commits.
        """
        prepare = skill.index("run its `prepare` action")
        merge = skill.index("git merge --no-commit --no-ff")

        assert prepare < merge
        assert (
            absent_phrases(
                commit_step,
                "remediation_branch.py check",
                "verify afterward that the recorded branch contains the new commit",
            )
            == []
        )
        assert (
            absent_phrases(
                skill,
                "never recollect with checkout merely to replace local remediation commits",
                "Do not derive a replacement expected value from current HEAD",
            )
            == []
        )

    def test_legacy_resume_recovers_before_committing_without_repeating_mode_choice(
        self, skill: str, skill_with_recovery: str, commit_step: str
    ) -> None:
        """Keep an authorized topic commit reachable after legacy branch recovery.

        A run resumes from an older generated-branch receipt. The resume text must recover first, reuse the last
        authorized expected head, and not ask for the commit mode again, so the earlier authorization stays usable.
        """
        resume = _paragraph(skill, "On resume, inspect")

        assert (
            absent_phrases(
                resume, "remediation-branch-recovered.json", "never replace it or fall back after a failed check"
            )
            == []
        )
        assert (
            absent_phrases(
                skill_with_recovery,
                "run `recover`",
                "last recorded authorized `--expected-head`",
                "without asking the user to select a mode again",
            )
            == []
        )
        assert "complete the legacy recovery procedure before staging" in commit_step
        assert _present(skill_with_recovery, "legacy generated-branch receipt is terminal") == []

    def test_explicit_commit_sequence_override_reaches_handoff_consumer(self, skill: str) -> None:
        """Allow a verified requested commit before report validation without bypassing source or ownership gates.

        The commit step and the final-handoff doc must agree: an explicit request may commit before report validation,
        then return with actual hashes to finish validation, while step 11 readiness rules still apply.
        """
        handoff = _read("shared", "final-handoff-code-remediate.md")
        commit = _between(skill, "### 12:", "## Fail-fast Rules")

        assert (
            absent_phrases(
                commit,
                "Do not wait solely for final report validation",
                "ordinary readiness and explicit local snapshot rules in step 11 still apply",
                "## Remaining Report Checkpoint",
            )
            == []
        )
        assert "preserve its hashes and `committed` disposition" in skill
        assert (
            absent_phrases(
                handoff,
                "before final report validation",
                "then return with actual hashes/disposition to complete validation and promotion",
                "Standing approval or grouping alone does not invoke this sequencing override",
            )
            == []
        )

    def test_selected_commit_cannot_finish_with_owned_residue(self, skill: str) -> None:
        """Keep new files, incomplete fixes and late worktree changes from escaping a selected commit.

        The selected-commit completion gate must require zero task-owned uncommitted entries across staged, unstaged and
        untracked state, leave unrelated changes alone, and fail a later drift at the final recheck.
        """
        gate = _between(skill, "### Selected commit completion gate", "### Return remediation")

        assert (
            absent_phrases(
                gate,
                "before final output",
                "staged, unstaged and all untracked entries",
                "zero task-owned uncommitted entries",
                "newly created files and deletions",
                "Unrelated pre-existing changes remain untouched",
                "After verified delivery, later drift fails the final recheck",
                "no-commit/leave-unstaged decision takes precedence",
            )
            == []
        )
        assert "all authorized owned edits, additions and deletions" in skill
