"""Regression checks for review-candidate validation and remediation recovery.

The skill, reference and shared-contract Markdown files are the unit under test. Every check reads them as prose:
sections are cut out by marker or line prefix, whitespace is normalized, and the assertions are plain substring checks,
so reflowing a paragraph never breaks an unrelated expectation.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

import pytest
from _skill_text import flat, read_reference_body, read_with_references

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
CODE_REVIEW_SKILL = PLUGIN_ROOT / "skills" / "code-review" / "SKILL.md"
CODE_REMEDIATE_SKILL = PLUGIN_ROOT / "skills" / "code-remediate" / "SKILL.md"
#: Sections moved out of SKILL.md and loaded only when their recovery trigger applies.
REVIEW_VALIDATION_RECOVERY = CODE_REVIEW_SKILL.parent / "references" / "validation-recovery.md"
REVIEW_REPORT_RECOVERY = CODE_REVIEW_SKILL.parent / "references" / "report-recovery.md"
EXISTING_CONFLICT_RECOVERY = CODE_REMEDIATE_SKILL.parent / "references" / "existing-conflict.md"
SOURCE_RECOVERY = CODE_REMEDIATE_SKILL.parent / "references" / "source-recovery.md"
MISSING_REVIEW_DECISION = CODE_REMEDIATE_SKILL.parent / "references" / "missing-review-decision.md"
FINAL_HANDOFF_CONTRACT = PLUGIN_ROOT / "shared" / "final-handoff-contract.md"
NATIVE_SKILL_CONTRACT = PLUGIN_ROOT / "shared" / "native-skill-contract.md"
REMEDIATION_REFERENCES = ("intake.md", "selection.md", "implementation.md", "completion.md")
VALIDATION_RECOVERY_LINK = "[Reviewer validation recovery](references/validation-recovery.md)"
ENTRY_HEADING = "### Entry: resume or previous completed review"


def _read(path: Path) -> str:
    """Return one UTF-8 file as text."""
    return path.read_text(encoding="utf-8")


def _after(text: str, marker: str) -> str:
    """Return the text following the first occurrence of ``marker``."""
    return text.split(marker, 1)[1]


def _before(text: str, marker: str) -> str:
    """Return the text preceding the first occurrence of ``marker``."""
    return text.split(marker, 1)[0]


def _until_line(text: str, prefix: str) -> str:
    """Return ``text`` up to, excluding, the first later line that starts with ``prefix``."""
    first, *rest = text.split("\n")
    kept = [first]
    for line in rest:
        if line.startswith(prefix):
            break
        kept.append(line)
    return "\n".join(kept)


def _first_paragraph(text: str) -> str:
    """Return the first non-empty block of consecutive lines."""
    return text.lstrip("\n").split("\n\n", 1)[0]


def _section(text: str, heading: str, next_prefix: str = "## ") -> str:
    """Return the body after a heading marker up to the next line starting with ``next_prefix``."""
    return _until_line(_after(text, heading), next_prefix)


def _remediation(*extra: str) -> str:
    """Return the remediation skill with its standard references and any extra ones."""
    return read_with_references(CODE_REMEDIATE_SKILL, *REMEDIATION_REFERENCES, *extra)


def _review(*references: str) -> str:
    """Return the review skill with the named references."""
    return read_with_references(CODE_REVIEW_SKILL, *references)


def _review_validation_recovery() -> str:
    """Return the validation-recovery body after confirming the review skill links to it."""
    assert VALIDATION_RECOVERY_LINK in _read(CODE_REVIEW_SKILL)
    return flat(read_reference_body(REVIEW_VALIDATION_RECOVERY, "Reviewer validation recovery"))


def _nested_review_delivery() -> str:
    """Return the final-handoff contract's nested review delivery section."""
    return flat(_section(_read(FINAL_HANDOFF_CONTRACT), "## Nested review delivery"))


def _assert_contains_all(text: str, requirements: Iterable[str]) -> None:
    """Assert every requirement occurs in ``text`` and name all the missing ones at once."""
    missing = [requirement for requirement in requirements if requirement not in text]
    assert not missing, f"missing from the checked text: {missing}"


class TestPrStatePreflight:
    """PR state is checked before any workflow intake in both caller skills."""

    @pytest.mark.installed_plugin
    @pytest.mark.parametrize("skill_name", ["code-review", "code-remediate"])
    def test_precedes_workflow_intake(self, skill_name: str) -> None:
        """Closed PRs stop before work is allocated or a redundant review workflow is offered.

        In both skills the preflight anchor must come before the first numbered step and before any prior-report lookup,
        and the text before step 01 must say it also covers resumed PR work.
        """
        skill_path = PLUGIN_ROOT / "skills" / skill_name / "SKILL.md"
        workflow = (
            _after(read_with_references(skill_path, "source-and-scope.md"), "## Workflow (Exact Commands)")
            if skill_name == "code-review"
            else _read(skill_path.parent / "references/intake.md")
        )
        preflight = workflow.index("#pr-state-preflight")

        assert preflight < workflow.index("### 01:")
        assert preflight < workflow.index("find-review-report.py")
        assert "including resumed PR work" in flat(_before(workflow, "### 01:"))

    @pytest.mark.installed_plugin
    def test_covers_identity_terminal_states_and_unknown_state(self) -> None:
        """PR identity, current state and early exit stay coupled across both caller skills.

        The shared native-skill contract section must name how identity is resolved, which states stop the run and what
        is withheld until the state is known.
        """
        entry = flat(_section(_read(NATIVE_SKILL_CONTRACT), "## PR State Preflight"))

        _assert_contains_all(
            entry,
            (
                "--json url,state",
                "--canonical-pr-url",
                "current-branch",
                "returned URL",
                "`CLOSED` or `MERGED`",
                "`OPEN`",
                "missing or unknown state",
                "prior-report lookup",
                "run allocation",
                "fresh-review question",
                "checkout",
                "specialist dispatch",
                "Local report-only",
                "not proof of current PR state",
            ),
        )


class TestReviewEntryAndRoutes:
    """The review skill's entry choice, routing and handoff wording."""

    @pytest.mark.installed_plugin
    def test_prior_choice_precedes_run_creation(self) -> None:
        """A reusable completed report is offered before another run supersedes it.

        The entry section must describe the reuse/fresh choice, the no-question fresh route and the unmet-checkpoint
        resume wording before any run is created or promoted.
        """
        entry = flat(_section(_review("source-and-scope.md"), ENTRY_HEADING, "### 01:"))

        _assert_contains_all(
            entry,
            (
                "--target",
                "--result",
                "Reuse completed review",
                "Run fresh review",
                "For a new review request without requested report recovery, no eligible completed report means fresh"
                " review without a question",
                "A failed requested-report recovery instead returns to the Incomplete review decision",
                "before creating or promoting another run",
                "recorded revision",
                "current-head",
                "first unmet checkpoint",
            ),
        )

    @pytest.mark.installed_plugin
    def test_shorthand_identity_resolves_before_previous_report_lookup(self) -> None:
        """Numeric and current-branch invocation share the pre-allocation reuse route.

        The canonical PR URL must be resolved (from a number or the current branch) before the target lookup runs.
        """
        entry = flat(_section(_review("source-and-scope.md"), ENTRY_HEADING, "### 01:"))

        lookup = entry.index("--target <canonical PR URL>")
        assert entry.index("--canonical-pr-url <positive PR number>") < lookup
        assert entry.index("-- gh pr view --json url,state") < lookup
        assert "temporary identity file" in entry
        assert "all PR input forms" in entry
        assert "known canonical PR identity" not in entry

    @pytest.mark.installed_plugin
    def test_review_and_remediation_checkout_routes_stay_distinct(self) -> None:
        """Calibrated checkout routes and incomplete-report continuation stay aligned with the skills.

        Review inspects a detached worktree while remediation checks out the PR branch; the calibration prompt, README
        and incomplete-handoff wording must agree and must not silently switch to online-only remediation.
        """
        review = flat(_review("source-and-scope.md"))
        remediation = flat(_remediation())
        readme = flat(_read(PLUGIN_ROOT / "README.md")).lower()
        cases = json.loads(_read(PLUGIN_ROOT / "runtime" / "calibration" / "behavioral-cases.json"))["cases"]
        prompt = flat(next(case for case in cases if case["id"] == "code-remediate-branch-continuity")["prompt"])
        incomplete_handoff = flat(_until_line(_after(_remediation(), "matching-review-incomplete:"), "- ")).lower()

        _assert_contains_all(review, ("uses `git worktree add --detach`", "It does not call `gh pr checkout`"))
        assert "Remediation first invokes `gh pr checkout <canonical PR URL>`" in remediation
        _assert_contains_all(prompt, ("uses `git worktree add --detach`", "it does not call `gh pr checkout`"))
        _assert_contains_all(
            incomplete_handoff,
            (
                "only then use current-online/user findings",
                "with the missing report limitation explicit",
                "missing requested pr review decision",
                "continuing without a completed report still requires the user's explicit choice",
            ),
        )
        assert "fresh review" in readme
        assert "requested report limitation disclosed" in readme
        assert "silently switch to online-only remediation" not in readme

    @pytest.mark.installed_plugin
    def test_native_context_files_are_owned_by_the_preparation_producer(self) -> None:
        """Parent-authored context files cannot collide with frozen producer output.

        The dispatch section, the historical generic-pass text and the README must all assign
        `specialists/<role>-context.md` to the preparation producer.
        """
        skill = _review("legacy-dispatch.md")
        readme = flat(_read(PLUGIN_ROOT / "README.md"))
        preparation = flat(_section(skill, "#### Prepare and dispatch the complete wave", "#### "))
        historical = _review("historical-routes.md")
        generic_passes = flat(_before(_after(historical, "For every triggered pass:"), "Parent owns"))

        assert "write only the focused Markdown briefs referenced by `review-briefs.json`" in preparation
        assert "`review_prepare.py prepare` owns `specialists/<role>-context.md`" in preparation
        assert "Do not create or overwrite those producer-owned context paths" in preparation
        assert "legacy or non-producer routes" in generic_passes
        _assert_contains_all(
            readme,
            ("`review_prepare.py` owns generated", "`specialists/<role>-context.md` files", "`review_batches.py`"),
        )

    def test_specialist_manifest_is_preflighted_before_candidate(self) -> None:
        """Malformed spawned-attempt bookkeeping cannot leave a candidate behind.

        The findings-and-completion reference must require a manifest-only preflight before the candidate is written.
        """
        skill = flat(_review("findings-and-completion.md")).lower()

        _assert_contains_all(
            skill,
            (
                "--manifest-only",
                "before writing `result.candidate.json`",
                "one or two sequential attempts",
                "never invent missing attempt provenance",
            ),
        )

    @pytest.mark.installed_plugin
    def test_closure_is_ordered_at_the_execution_checkpoint(self) -> None:
        """Manifest preflight or drafted output cannot become the final review step.

        Step 12 must list render, writer, both validators, promotion and completion in that order.
        """
        skill = _review("findings-and-completion.md")
        checkpoint = flat(_section(skill, "### 12:", "## Fail-fast Rules"))
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
    def test_gate_failure_keeps_artifact_closure_required(self) -> None:
        """Direct check receipts cannot bypass canonical failed-gate reconciliation.

        Step 07 must keep `gates.json` authoritative, mark the status failed and continue through step 12.
        """
        gates = flat(_section(_review("findings-and-completion.md"), "### 07:", "### 08:"))

        _assert_contains_all(
            gates,
            (
                "direct-check receipts do not replace `gates.json`",
                "status=fail",
                "continue through step 12",
                "Preserve the failed attempt",
            ),
        )

    @pytest.mark.installed_plugin
    def test_existing_merge_is_reported_and_handed_to_remediation(self) -> None:
        """A review-only run never finishes, aborts or authorizes an existing merge.

        Finish/abort/defer choices mutate the invoking worktree, which belongs to remediation; review inspects its own
        detached worktree and keeps going.
        """
        skill = _review("source-and-scope.md")
        guidance = flat(_first_paragraph(_after(skill, "For an existing merge or conflict in the invoking worktree")))

        _assert_contains_all(
            guidance,
            (
                "never finishes, aborts, or resolves that merge",
                "never asks for that authorization",
                "belongs to `code-remediate`",
                "does not block collection or source review",
                "never as a PR finding",
            ),
        )
        assert "resume the active code-review workflow" not in skill

    @pytest.mark.installed_plugin
    @pytest.mark.parametrize(
        ("reference", "section", "requirements"),
        [
            pytest.param(
                "routing.md",
                "Routing rules:",
                (
                    "actual independent children, joined results",
                    "complete required role and assigned-path coverage",
                    "Children may queue serially when capacity requires it",
                    "never claim unobserved overlap",
                    "Historical schemas 2–9 retain their issued scheduling and overlap gates",
                ),
                id="routing",
            ),
            pytest.param(
                "findings-and-completion.md",
                "## Fail-fast Rules",
                (
                    "complete required role and assigned-path coverage",
                    "Direct schema-ten runs require authentic child and join evidence",
                    "without claiming parallel overlap that did not occur",
                    "Historical overlap and scheduling gates remain unchanged",
                ),
                id="fail-fast",
            ),
        ],
    )
    def test_broad_rules_admit_complete_fast_native_coverage(
        self, reference: str, section: str, requirements: tuple[str, ...]
    ) -> None:
        """Mandatory broad-review rules do not reject already validated fast native coverage.

        The routing rules and the fail-fast rules must each admit complete native coverage, keep historical gates and
        drop the retired capacity-limited schema-eight exception.
        """
        contract = flat(_section(_review(reference), section))

        _assert_contains_all(contract, requirements)
        assert "narrowly validated capacity-limited schema-eight" not in contract

    @pytest.mark.installed_plugin
    def test_routing_reference_keeps_historical_queue_evidence(self) -> None:
        """Historical dispatch keeps its ordered queue and refill evidence for every role.

        Legacy dispatch must still accept a child finishing before the next consecutive dispatch.
        """
        historical = flat(_review("legacy-dispatch.md"))

        _assert_contains_all(
            historical,
            (
                "validated ordered queue/refill evidence cover every role",
                "even if a child finishes before the next consecutive dispatch",
            ),
        )


class TestValidationRecovery:
    """Reviewer evidence rejection and protocol repair keep valid review work."""

    @pytest.mark.installed_plugin
    def test_repair_question_runs_native_discovery_before_async(self) -> None:
        """Generated repair approval cannot skip discovery or replay its live question.

        The recovery text must require the native discovery checkpoint and let the selected control own the question.
        """
        _assert_contains_all(
            _review_validation_recovery(),
            (
                "native discovery checkpoint",
                "ALL_TOOLS",
                "Absence from the short tool list is not unavailability",
                "Unknown async rendering is unsuitable",
                "The selected control owns the question and options",
                "do not echo them in commentary or final output",
            ),
        )

    @pytest.mark.installed_plugin
    def test_protocol_repair_resumes_the_retained_wave_without_reassessment(self) -> None:
        """An internal dispatch or representation error cannot abandon valid review work.

        Repair must reuse the same run and wave, preserve every claim and finding byte, and avoid fresh assessment.
        """
        _assert_contains_all(
            _review_validation_recovery(),
            (
                "prepare-repair",
                "incomplete-dispatch",
                "closure-evidence-shape",
                "same run and current wave",
                "one generated `_a2`",
                "Preserve every claim, severity, source coordinate, confidence and assessment",
                "Finding IDs remain unchanged except for the single proven local-ID token in `finding-id-namespace`",
                "retain the original raw ID and immutable qualified origin witness",
                "uniquely prove the original local ID and identical non-ID finding fields",
                "all other response bytes remain exact",
                "Ordinary ID validation remains strict",
                "do not require a fresh complete PR review",
                "No substantive reassessment",
            ),
        )

    @pytest.mark.installed_plugin
    def test_dispatch_order_failure_uses_existing_fallback_without_recollection(self) -> None:
        """Immutable scheduling failures stay out of reader repair and redundant PR collection.

        The dispatch-order section must preserve the rejected wave, use the parent fallback and avoid recollecting
        GitHub evidence for this failure alone.
        """
        schedule = _before(
            _after(_review_validation_recovery(), "For `review-inspection-dispatch-order-mismatch`"),
            "For `review-inspection-context-not-sent:<role>`",
        )

        _assert_contains_all(
            schedule,
            (
                "generated queue and actual first-spawn order",
                "preserve the rejected wave",
                "Historical launch order cannot be repaired",
                "not a supported `prepare-repair` kind",
                "revalidate the retained source receipts",
                "do not recollect GitHub evidence solely for this failure",
                "[Current parent fallback](legacy-dispatch.md#current-parent-fallback)",
                "independence is not required in the current user-authorized scope",
                "including an explicit coverage waiver",
                "complete its nonapproval report and normal closing gates",
                "concrete unmet independent-route decision",
                "source drift, denied capability and exhausted retry limits",
            ),
        )

    @pytest.mark.installed_plugin
    def test_rejection_and_declined_repair_are_explained(self) -> None:
        """Reviewer evidence rejection never becomes a repair-only dead end.

        The recovery text must explain the rejection, offer separate Approve/Deny options and allow a fresh sequential
        review when repair is declined.
        """
        _assert_contains_all(
            _review_validation_recovery(),
            (
                "could not confirm",
                "encrypted",
                "not established",
                "separate canonical options `Approve` and `Deny`",
                "fresh sequential review",
                "explicitly required independent",
                "separate fallback plan",
                "normal completion checks",
            ),
        )

    @pytest.mark.installed_plugin
    def test_readme_keeps_the_rejected_evidence_review_exception(self) -> None:
        """General role fallback wording cannot block fresh review after evidence rejection.

        The README must keep the parent-sequential fallback exception and still forbid fallback for mere disagreement.
        """
        readme = flat(_read(PLUGIN_ROOT / "README.md"))

        _assert_contains_all(
            readme,
            (
                "Code Review also permits its existing separate parent-sequential fallback",
                "preserve that evidence as unaccepted and perform fresh parent inspection",
                "Neither route permits fallback merely because a specialist disagreed",
            ),
        )

    @pytest.mark.installed_plugin
    def test_merge_resume_keeps_checkpoints_and_protected_path_redaction(self) -> None:
        """Redaction conflicts or partial conflict resolution cannot bypass merge gates.

        Remediation keeps both merge checkpoints and the review recovery text refuses protected path lists in the
        summary.
        """
        remediation = flat(_remediation("existing-conflict.md"))
        review = flat(_review("validation-recovery.md", "report-recovery.md", "collection-failure.md")).lower()

        _assert_contains_all(
            remediation,
            ("partial recovery, not a completed merge", "pre-merge source receipts", "post_merge_head"),
        )
        assert "do not insert protected path lists into the bound summary" in review


class TestRemediationRecovery:
    """Remediation recovery routes: source drift, fetch failures, merges and unpromoted candidates."""

    @pytest.mark.installed_plugin
    def test_source_drift_recovery_preserves_commits_and_owns_safe_refresh(self) -> None:
        """Stale source is recovered without pulling the wrong branch or overwriting local fixes.

        The recovery section must pull only the verified PR worktree fast-forward, protect local commits and keep the
        legacy receipt route pointing back at it.
        """
        skill = _remediation("source-recovery.md")
        recovery = flat(_section(skill, "## Source drift recovery"))
        legacy = flat(_first_paragraph(_after(skill, "## Legacy receipt recovery\n\n")))

        _assert_contains_all(
            recovery,
            (
                "destination branch ref before checkout",
                "last recorded authorized revision",
                "git -C <verified-pr-worktree> pull --ff-only",
                "current PR metadata",
                "never pull the unrelated current branch",
                "Do not recollect over local remediation commits",
                "git switch <recorded-original-branch>",
                "require the destination tip to equal the last recorded authorized revision",
                "fresh attempt",
                "source-dependent checks",
                "active writer",
                "first unmet checkpoint",
                "existing authorization",
            ),
        )
        assert "never automatically switch, reset" not in skill
        assert "source-recovery.md#legacy-receipt-recovery" in skill
        assert legacy.startswith("For a schema-1 legacy receipt")
        assert "[Source drift recovery](#source-drift-recovery) before `recover`" in legacy
        assert "retained PR metadata" in legacy

    @pytest.mark.installed_plugin
    def test_source_drift_pulls_verified_branch_before_remote_metadata_gate(self) -> None:
        """A GitHub metadata timeout cannot prevent an otherwise safe PR pull.

        The recovery section, the intake entry and the native preflight must all allow the guarded pull before the
        remote state read, and treat a metadata-check timeout as distinct from a pull failure.
        """
        recovery = flat(_section(_read(SOURCE_RECOVERY), "## Source drift recovery"))
        entry = flat(_before(_read(CODE_REMEDIATE_SKILL.parent / "references/intake.md"), "### 01:"))
        preflight = flat(_section(_read(NATIVE_SKILL_CONTRACT), "## PR State Preflight"))

        _assert_contains_all(
            recovery,
            (
                "git worktree list --porcelain",
                "git -C <verified-pr-worktree> pull --ff-only",
                "Do not require fresh GitHub metadata or a preliminary PR-head fetch before this pull",
                "After the pull succeeds, obtain current PR metadata",
                "If the pull fails or is denied, stop this recovery route and escalate to the user",
                "a metadata-check approval timeout is not a pull failure",
                "no active writer",
                "no local remediation commits",
            ),
        )
        assert "guarded-pull exception" in entry
        assert "may precede the remote state read in step 2" in preflight
        assert "before this preflight succeeds" in preflight

    @pytest.mark.installed_plugin
    def test_fetch_recovery_separates_unknown_cause_from_missing_source(self) -> None:
        """Download failures stay actionable without inventing access failure or unsafe fallback.

        The collection-failure section must treat a failed head fetch as unproven access loss and forbid code edits and
        unchanged retries.
        """
        assert "[source-recovery.md](source-recovery.md)" in _remediation()
        body = read_reference_body(SOURCE_RECOVERY, "Source recovery")
        recovery = flat(_section(body, "## Collection failure recovery"))

        _assert_contains_all(
            recovery,
            (
                "pr-head-fetch",
                "does not prove",
                "diagnosis",
                "Sequential execution",
                "no code edits",
                "accepts stale",
                "current PR head",
                "unchanged retry",
            ),
        )

    @pytest.mark.installed_plugin
    def test_existing_merge_recovery_requires_an_owned_concrete_choice(self) -> None:
        """Existing merge recovery avoids unexplained cleanup demands, automatic aborts and dirty-worktree overclaims.

        The reference must offer Finish/Abort/Defer under explicit authorization and map a denial to Defer.
        """
        assert "[Existing merge or conflict recovery](existing-conflict.md)" in _remediation()
        recovery = flat(read_reference_body(EXISTING_CONFLICT_RECOVERY, "Existing merge or conflict recovery"))

        _assert_contains_all(
            recovery,
            (
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
            ),
        )

    @pytest.mark.installed_plugin
    def test_successful_recovery_resumes_the_active_workflow(self) -> None:
        """Verified conflict recovery cannot end before the user's original remediation finishes.

        After an authorized recovery succeeds the workflow resumes at its first unmet checkpoint and keeps the merge
        result.
        """
        skill = flat(_remediation("existing-conflict.md"))

        _assert_contains_all(
            skill,
            (
                "Once an authorized recovery succeeds, resume the active code-remediate workflow",
                "first unmet checkpoint",
                "do not stop at conflict resolution or ask the user to rerun the skill",
                "preserve and verify its recorded merge result",
            ),
        )

    def test_unpromoted_candidate_is_validated_read_only(self) -> None:
        """Candidates are diagnosed read-only, then the producer is resumed for feasible automatic recovery.

        Remediation may never repair, promote or consume the candidate itself, nor ask the user to rerun the review.
        """
        skill = flat(_remediation()).lower()

        _assert_contains_all(
            skill,
            (
                "matching-review-candidate-unpromoted",
                "remediation never directly repairs, rewrites, rerenders, or promotes its `specialist-manifest.json`",
                "review-specific validator, then the shared validator, read-only",
                "resume that owning producer automatically",
                "do not ask the user to re-run code-review for an agent-owned bookkeeping failure",
                "a passing read-only validation does not admit the candidate either",
                "never consume `result.candidate.json` as a completed review",
                "only its eligible finding records may be considered under preliminary finding intake",
            ),
        )
        assert "same parent thread" not in skill
        assert "promote it to `result.json`" not in skill

    def test_exact_candidate_validation_failure_is_preserved(self) -> None:
        """Generic rerun advice is replaced by the actionable upstream validator code.

        Remediation must surface the recorded validation failure and the run-mutated guard, not invent attempt data.
        """
        skill = flat(_remediation()).lower()

        _assert_contains_all(
            skill,
            (
                "review-candidate-validation.txt",
                "manifest-invalid-attempt-count:<role>",
                "never invent missing attempt provenance",
                "fall back to an older assessed report",
                "code-remediate-review-run-mutated",
            ),
        )

    @pytest.mark.installed_plugin
    def test_patches_are_distinguished_from_evidence_only_closure(self) -> None:
        """A target merge or green checks are never reported as implemented fixes.

        The remediation text must state that gates and target integration do not close selected items.
        """
        _assert_contains_all(
            flat(_remediation()),
            (
                "No review findings were fixed by a new code change",
                "target integration is not finding implementation",
                "Passing gates do not close selected items",
                "Verified without code changes:",
                "Blocked:",
                "Never render bare `unresolved`",
            ),
        )

    @pytest.mark.installed_plugin
    def test_usable_context_is_prepared_and_missing_evidence_recovered(self) -> None:
        """Artifact-only text reviewers and missing coverage cannot end safe recovery.

        A no-tools reviewer must receive source inline, and missing coverage details are an investigation checkpoint.
        """
        _assert_contains_all(
            flat(_remediation()),
            (
                "no-tools reviewer must receive the relevant source, diff, and evidence inline",
                "artifact paths alone are not readable context",
                "Missing coverage details are an investigation checkpoint",
                "resume the selected finding",
            ),
        )

    @pytest.mark.installed_plugin
    def test_execution_failure_cannot_be_reclassified_as_inapplicable(self) -> None:
        """Failed execution stays visible when recovery hands a review to remediation.

        Quality gates must never turn a failed check into not-applicable, and remediation may continue without a
        completed report only after an explicit user choice.
        """
        quality = flat(_read(PLUGIN_ROOT / "shared" / "quality-gates.md"))
        remediation = flat(_remediation())

        _assert_contains_all(
            quality,
            (
                "execution failure never makes an applicable check `not-applicable`",
                "archive runner-owned receipts under `gate-attempts/<NNN>`",
                "reject failed-to-skipped reclassification",
            ),
        )
        _assert_contains_all(
            remediation,
            (
                "rerun the full review without the explicit authorization below, or fall back to an older assessed report",
                "For requested PR evidence, continue source-verified remediation with the report limitation disclosed only"
                " after the user explicitly chooses to continue without a completed report",
            ),
        )


class TestNestedReviewHandoff:
    """A review launched from remediation returns an exact completed report to the waiting caller."""

    @pytest.mark.installed_plugin
    def test_branch_drift_keeps_remediation_owner_and_incomplete_status(self) -> None:
        """A completed child review cannot become a completed no-op remediation handoff.

        The nested-delivery section and both remediation contracts must keep remediation incomplete until the first
        unmet checkpoint, and say that evidence-only closure is not implementation.
        """
        nested = _nested_review_delivery()
        skill = flat(_remediation())
        supplement = flat(_read(PLUGIN_ROOT / "shared/final-handoff-code-remediate.md"))

        _assert_contains_all(
            nested, ("#source-drift-recovery", "Remediation remains incomplete", "send another message")
        )
        assert all(
            requirement in contract
            for contract in (skill, supplement)
            for requirement in (
                "Before every status or final answer",
                "prepared branch receipt",
                "zero implemented",
                "first unmet checkpoint",
                "evidence-only closure",
            )
        )

    @pytest.mark.installed_plugin
    def test_approved_nested_review_recovers_before_returning_to_remediation(self) -> None:
        """An approved nested review failure cannot become a generic resume request.

        The missing-decision reference's New review branch must link validation recovery and GitHub read execution and
        must not require a new user message to resume authorized recovery.
        """
        assert "missing-review-decision.md" in _remediation()
        decision = read_reference_body(MISSING_REVIEW_DECISION, "Missing requested PR review decision")
        affirmative = flat(_until_line(_after(decision, "**New review:**"), "- **Explicit free-text"))

        _assert_contains_all(
            affirmative,
            (
                "[Reviewer validation recovery](../../code-review/references/validation-recovery.md)",
                "[GitHub Read Execution](../../../shared/native-skill-contract.md#github-read-execution)",
                "Do not ask for a new message merely to resume already-authorized recovery",
                "does not reset retry limits or override a runtime denial",
                "only after an eligible completed result is admitted",
            ),
        )

    @pytest.mark.installed_plugin
    def test_approved_review_returns_exact_completed_report_to_waiting_remediation(self) -> None:
        """A successful nested review cannot end the caller or select another report.

        The nested-delivery section, the missing-decision reference and the three callers must agree on returning the
        exact canonical result path to the waiting remediation run.
        """
        remediation = _remediation()
        review = _read(CODE_REVIEW_SKILL)
        helpers = _read(PLUGIN_ROOT / "shared/helper-cli-contract.md")
        decision_text = read_reference_body(MISSING_REVIEW_DECISION, "Missing requested PR review decision")

        _assert_contains_all(
            _nested_review_delivery(),
            (
                "normal full `code-review` workflow",
                "`find-review-report.py --complete-run <review-run-directory>` succeeds",
                "exact canonical `<review-run-directory>/result.json`",
                "do not end the parent turn",
                "`unavailable`, `closed`, failed or incomplete",
                "Standalone review still emits",
                "not a `caller-contract` schema override",
            ),
        )
        _assert_contains_all(
            flat(decision_text),
            (
                "Set `FINDINGS_SOURCE` to that exact returned canonical result path",
                "`find-review-report.py --result <path>`",
                "Do not repeat latest-report discovery",
                "fresh source collection",
                "same remediation run",
            ),
        )
        assert "missing-review-decision.md" in remediation
        assert all(
            "final-handoff-contract.md#nested-review-delivery" in contract
            for contract in (decision_text, review, helpers)
        )


class TestFinalizationContracts:
    """Completion wording: failed artifact routes, failed gates and report recovery."""

    @pytest.mark.installed_plugin
    def test_finalization_failure_delivers_evidence_without_repeating_declined_review(self) -> None:
        """A failed artifact route cannot become a bare-code dead end for authorized source work.

        Supported findings are delivered as preliminary evidence, a declined review is not offered again and already-
        authorized work proceeds.
        """
        review = flat(_review("findings-and-completion.md"))
        contract = flat(_read(FINAL_HANDOFF_CONTRACT))

        _assert_contains_all(
            review,
            (
                "Deliver all supported findings as explicitly preliminary evidence",
                "Do not repeat a declined additional review in next actions",
                "already-authorized source-confirmed remediation may proceed",
            ),
        )
        _assert_contains_all(
            contract,
            (
                "including progress reports and unvalidated process failures",
                "Perform already-authorized next work instead of asking for permission again",
            ),
        )

    @pytest.mark.installed_plugin
    def test_review_completion_is_distinct_from_failed_quality_gates(self) -> None:
        """A completed nonapproval review is not reported as an unfinished workflow.

        The handoff contract must not infer lifecycle state from `status=fail`, and remediation must tell a failed
        lifecycle checkpoint from an admitted report whose quality gates failed.
        """
        contract = flat(_read(FINAL_HANDOFF_CONTRACT))
        remediation = flat(_remediation("missing-review-decision.md"))

        _assert_contains_all(
            contract,
            ("review completed even when `result.status=fail`", "never infer it from `status=fail` alone"),
        )
        _assert_contains_all(
            remediation,
            (
                "A failed or incomplete review lifecycle checkpoint",
                "An admitted report with `status=fail` from quality gates",
            ),
        )

    def test_report_recovery_preserves_evidence_and_distinguishes_substantive_gaps(self) -> None:
        """Recovery may regenerate proven bookkeeping but cannot manufacture review completion.

        The review skill, remediation skill and report-recovery reference must agree on which repairs are automatic and
        which gaps stay unverified.
        """
        skill = flat(_review("source-and-scope.md", "findings-and-completion.md", "report-recovery.md"))
        recovery = flat(_read(REVIEW_REPORT_RECOVERY))

        assert "[Report compatibility and recovery](references/report-recovery.md)" in _read(CODE_REVIEW_SKILL)
        _assert_contains_all(
            skill,
            (
                "A failed requested-report recovery instead returns to the Incomplete review decision",
                "An incompatible reader requires capability diagnosis",
            ),
        )
        assert "Do not prescribe a review rerun for the version alone" in flat(_remediation())
        _assert_contains_all(
            recovery,
            (
                "A version number selects a reader and its semantics",
                "Automatically repair deterministic derived presentation or bookkeeping",
                "Retain byte-exact pre-repair artifacts",
                "Never rewrite frozen dispatch, contexts, source receipts or raw reviewer output",
                "A schema-2 archive or another unsupported representation stays unverified",
                "Never transplant evidence between runs",
                "`Resume last one`",
                "never relabel rejected output as valid",
                "run both producer artifact validators, normal promotion and `--complete-run`",
                "Resume the waiting remediation at its first unmet checkpoint in the same turn",
            ),
        )


def test_review_selects_execution_before_dispatch_and_preserves_task_identity() -> None:
    """Prevent unnecessary native waves and task renaming from stalling an eligible small review."""
    lines = CODE_REVIEW_SKILL.read_text(encoding="utf-8").splitlines()
    choice = next(index for index, line in enumerate(lines) if "Choose the execution route before preparing" in line)
    dispatch = next(index for index, line in enumerate(lines) if "For a new independent review" in line)
    recovery = REVIEW_VALIDATION_RECOVERY.read_text(encoding="utf-8").splitlines()

    assert choice < dispatch
    assert "TRIVIAL" in lines[choice]
    assert "LOCAL" in lines[choice]
    assert "independence requirement" in lines[choice]
    assert any("Copy every generated argument unchanged" in line and "task_name" in line for line in lines)
    assert any("review-child-session-missing" in line and "zero" in line for line in recovery)
    assert any("Announcing a fallback is not executing it" in line for line in recovery)


def test_interrupted_review_offers_continue_or_skip_without_restart() -> None:
    """Keep a progressing gate and a missed ordering checkpoint in the retained review."""
    lines = REVIEW_REPORT_RECOVERY.read_text(encoding="utf-8").splitlines()
    section = flat(_after(_read(REVIEW_REPORT_RECOVERY), "## Interrupted checks and missed ordering"))
    assert any("7200 seconds" in line and "progress" in line for line in lines)
    assert "exactly `Continue` and `Skip`" in flat(section)
    assert "Never offer a fresh review" in flat(section)
    assert "cannot reconstruct a blind blueprint" in flat(section)
    assert "never establishes completed-review proof" in flat(section)
    completion = (
        (CODE_REVIEW_SKILL.parent / "references/findings-and-completion.md").read_text(encoding="utf-8").splitlines()
    )
    standalone = next(line for line in completion if "For a genuinely blocked standalone handoff" in line)
    assert "only when the blocker belongs to the three-option recovery branch" in standalone
    assert "never for interrupted checks or missed ordering" in standalone
