"""Regression checks for bare-PR online-only remediation intake.

A bare PR target must be remediable from its current online review evidence without a prior review report, while an
explicitly requested review report that is missing still stops at one bound user decision. The contracts live in
instruction Markdown, so every check reads it line- and heading-wise and compares whitespace-collapsed text: re-wrapping
a paragraph must never break an assertion.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _skill_text import flat, found_phrases, missing_phrases, read_reference_body, read_with_references

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
CODE_REMEDIATE_SKILL = PLUGIN_ROOT / "skills" / "code-remediate" / "SKILL.md"
#: Section moved out of SKILL.md and loaded only when a requested PR review report is missing.
MISSING_REVIEW_DECISION = CODE_REMEDIATE_SKILL.parent / "references" / "missing-review-decision.md"
REMEDIATE_REFERENCES = ("intake.md", "selection.md", "implementation.md", "completion.md")
DECISION_TITLE = "Missing requested PR review decision"


def _section(text: str, start: str, end: str | None = None) -> str:
    """Return the whitespace-collapsed text after the first `start` marker, up to the next `end` marker if given.

    Example:
        >>> _section("# A\\nbody\\n## B\\nmore", "# A", "\\n## ")
        'body'
    """
    assert start in text, f"section marker {start!r} not found"
    body = text.split(start, 1)[1]
    return flat(body.split(end, 1)[0] if end else body)


@pytest.fixture
def skill() -> str:
    """Return the code-remediate SKILL.md joined with its always-loaded reference files."""
    return read_with_references(CODE_REMEDIATE_SKILL, *REMEDIATE_REFERENCES)


@pytest.fixture
def decision() -> str:
    """Return the missing-requested-review decision reference without its title line."""
    return read_reference_body(MISSING_REVIEW_DECISION, DECISION_TITLE)


class TestWorktreeDelivery:
    """Isolated remediation must return its fixes to the user's original checkout."""

    @pytest.mark.installed_plugin
    def test_delivers_to_original_checkout_after_source_checks(self, skill: str) -> None:
        """Isolated remediation does not finish with fixes stranded outside the user's checkout.

        The shared worktree-lifecycle policy and the skill's return-to-checkout step must both pin the destination, the
        ordering around source checks, the non-destructive transfer and the incomplete-delivery outcome.
        """
        contract = (PLUGIN_ROOT / "shared" / "native-skill-contract.md").read_text(encoding="utf-8")
        policy = _section(contract, "## Worktree lifecycle", "\n## ")
        delivery = _section(skill, "### Return remediation to the invoking checkout")

        assert (
            missing_phrases(
                policy,
                (
                    "original invoking checkout",
                    "before collection or source edits",
                    "explicit worktree-only",
                    "Review-only",
                    "never switch or deliver changes to the invoking checkout",
                    "staged, unstaged and untracked",
                    "Do not merge or cherry-pick",
                    "delivery remains incomplete",
                    "historical source receipt",
                    "Select the destination route before any transfer",
                    "verify in place without detaching, copying or requiring a clean source index",
                    "PR identity, tracking and ancestry-from-receipt checks apply only to PR mode",
                ),
            ),
            missing_phrases(
                delivery,
                ("before final promotion and output", "no-commit and leave-unstaged", "worktree-delivery.md"),
            ),
            found_phrases(skill, ("local fixes and optional commits remain there",)),
        ) == ([], [], [])

    @pytest.mark.installed_plugin
    def test_result_template_binds_every_artifact_path_to_the_run_directory(self) -> None:
        """PR remediation result links point into the promoted directory, never a stale timestamp.

        The result template is the starting point of every run; a literal timestamp in it would leave links pointing at
        the allocation-time directory after the run is promoted.
        """
        template = (CODE_REMEDIATE_SKILL.parent / "result-template.json").read_text(encoding="utf-8")
        result = json.loads(template)
        handoff = result["metadata"]["final_handoff"]

        assert (
            result["artifact_path"],
            handoff["handoff_path"],
            handoff["rendered_path"],
            handoff["validation_path"],
            ".reports/codex/code-remediate/<timestamp>" in template,
        ) == (
            "<run-directory>/result.json",
            "<run-directory>/final-handoff.json",
            "<run-directory>/final.md",
            "<run-directory>/final-handoff.validation.json",
            False,
        )


@pytest.mark.parametrize(
    ("skill_name", "heading", "references"),
    [
        pytest.param("code-review", "#### Embedded review findings", ("source-and-scope.md",), id="code-review"),
        pytest.param("code-remediate", "### Embedded review findings", REMEDIATE_REFERENCES, id="code-remediate"),
    ],
)
def test_embedded_review_findings_are_individually_inventoried(
    skill_name: str, heading: str, references: tuple[str, ...]
) -> None:
    """A collected bot review cannot mask its independent nested findings.

    Both the review and the remediation skill must inventory each nested finding of a parent comment before
    deduplication, never collapsing them by file or line alone.
    """
    skill = read_with_references(PLUGIN_ROOT / "skills" / skill_name / "SKILL.md", *references)

    assert (
        missing_phrases(
            skill,
            (
                heading,
                "<parent-id>#finding-<ordinal>",
                "before deduplication",
                "Comments generated",
                "same file or line alone",
                "complete parent body",
                "every nested finding",
            ),
        )
        == []
    )


class TestBarePullRequestIntake:
    """A bare PR target is remediated from current online evidence without a prior review artifact."""

    def test_bare_targets_collect_online_evidence_without_review_artifact(self, skill: str) -> None:
        """Bare PR remediation stays usable when no assessed report exists.

        A bare number, `#number`, URL or natural-language PR target collects current online items, while an explicit
        `+review`, `+report`, alias or report path keeps its report-plus-online behavior.
        """
        assert (
            missing_phrases(
                skill,
                (
                    "bare number, `#number`, PR URL, and natural-language bare PR targets",
                    "collect current online items and verified local checkout",
                    "without a prior review report",
                    "explicit `+review`, `+report`, report aliases, and report paths retain report-plus-online behavior",
                ),
            )
            == []
        )

    def test_missing_findings_source_does_not_fail_bare_pr_route(self, skill: str) -> None:
        """The report-source fail-fast rule applies only to report aliases, never to a bare PR.

        A bare PR has current online evidence as its findings source, so it must not fail or request a code review
        merely because no assessed review artifact exists.
        """
        fail_fast = _section(skill, "## Fail-Fast Rules", "## Quality Gates")

        assert (
            missing_phrases(
                fail_fast,
                (
                    "Missing findings source in report mode, an explicit report path, or a report alias",
                    "A bare PR has current online PR evidence as its findings source",
                    "must not fail or request `code-review` merely because no assessed review artifact exists",
                ),
            ),
            missing_phrases(
                skill,
                (
                    "A bare PR target must not run this helper, scan prior review reports",
                    "For bare PR online-only intake, do not create `<run-directory>/findings-input.txt`",
                    "When `REQUESTED_REPORT=true`, no matching code-review report means the requested assessed findings are"
                    " missing",
                    "Explain that first",
                    "Inspect that run's classified error and retained checkout diagnostics",
                ),
            ),
        ) == ([], [])

    def test_preliminary_findings_keep_source_and_completion_boundaries(self, skill: str) -> None:
        """Source-confirmed hypotheses are permitted without certifying an unfinished review."""
        assert (
            missing_phrases(
                skill,
                (
                    "### Preliminary finding intake",
                    "matching exact PR identity, head, scope, and source evidence",
                    "triage each claim against the freshly verified current source",
                    "never present preliminary records as a validated review result",
                    "Selection, source checkout, target integration, and merge authorization gates still apply",
                    "do not certify unreviewed fixes as clean",
                    "Do not manufacture an assessed JSON report",
                ),
            )
            == []
        )

    def test_evidence_age_preserves_current_source_triage_and_bound_authorization(self, skill: str) -> None:
        """Older online comments require source validation, while an additional review needs its own authority.

        Comment age or fetch time never establishes review freshness; each claim is inspected against the verified
        current source, and an extra review is neither started nor offered solely because comments predate the head.
        """
        freshness = _section(skill, "### Review evidence freshness", "\n### ")
        decision = MISSING_REVIEW_DECISION.read_text(encoding="utf-8")

        assert (
            missing_phrases(
                freshness,
                (
                    "saved reports and online review comments",
                    "fetch time does not establish review freshness",
                    "For bare PR online-only remediation",
                    "not a new permission question",
                    "Inspect each claim against the verified current source",
                    "Do not start or offer an additional review solely because comments predate the head",
                    "Use existing findings",
                    "Update existing review",
                    "Create a new review",
                    "unknown",
                    "`+review`",
                    "do not ask again",
                    "current source",
                    "never edit or post GitHub comments",
                    "set `REQUESTED_REPORT=true`",
                    "retain online evidence",
                    "exact returned canonical result",
                ),
            ),
            missing_phrases(decision, ("joined `+review`", "carry that authorization into Code Review")),
        ) == ([], [])


class TestRequestedReviewDecision:
    """A requested PR review that is missing stops at one bound decision before remediation continues."""

    def test_requested_review_does_not_replace_primary_remediation(self, skill: str) -> None:
        """The missing-review decision is required before continuing authorized fixes.

        `+review` joins review with remediation and authorizes the review step, while `+report` and explicit report
        paths request existing evidence only; none of the retired recovery wordings may remain in intake.
        """
        intake = _section(skill, "### 02: Normalize input", "### 03:")

        assert (
            missing_phrases(
                intake,
                (
                    "`+review` joins review with remediation and authorizes the needed review step",
                    "`+report`, report aliases and explicit report paths request existing evidence only",
                    DECISION_TITLE,
                    "continue available findings only after explicit user confirmation",
                    "Disclose the requested review evidence as unavailable without manufacturing a finding",
                ),
            ),
            found_phrases(
                intake,
                (
                    "Remediation resumes only after the producer completes",
                    "or switch to online-only intake",
                    "complete its ordered artifact closure before intake",
                ),
            ),
        ) == ([], [])

    def test_missing_requested_report_blocks_continuation_until_explicit_decision(self, skill: str) -> None:
        """Fail-fast guidance cannot silently bypass a requested review."""
        fail_fast = _section(skill, "## Fail-Fast Rules", "## Quality Gates")

        assert (
            missing_phrases(skill, ("ask only for the missing finding evidence or selection",)),
            missing_phrases(
                fail_fast,
                (
                    DECISION_TITLE,
                    "available findings require explicit user confirmation before continuation",
                ),
            ),
            found_phrases(fail_fast, ("run `$code-review <target>` first or provide a report path",)),
        ) == ([], [], [])

    def test_missing_pr_review_has_three_choices_and_preserves_pending_decisions(
        self, skill: str, decision: str
    ) -> None:
        """The user's recovery choices stay explicit without accepting preliminary evidence.

        The question names the linked PR and offers exactly `New review`, `Resume last one` and `Stop`; a dismissed or
        unanswered prompt authorizes none of them, and a bare PR never sees this question.
        """
        assert (
            missing_phrases(skill, ("missing-review-decision.md",)),
            missing_phrases(
                decision,
                (
                    "`mode=pr` and `REQUESTED_REPORT=true`",
                    "explicit PR report paths",
                    "one native question naming the linked PR, recovering missed ordering automatically and applying its interrupted-check `Continue`/`Skip` exception only for a concrete remaining check decision and otherwise offering exactly `New review`, `Resume last one`, `Stop`",
                    "normal full `code-review` workflow",
                    "both artifact validators, promotion and `--complete-run`",
                    "resume this same remediation run",
                    "only after an eligible completed result is admitted",
                    "`requested_report=true`",
                    "A dismissed or unanswered prompt authorizes none of its options",
                    "Bare PR online-only intake does not ask this question",
                    "stop the joined workflow, retain artifacts and changes, and do not continue remediation",
                ),
            ),
        ) == ([], [])

    def test_sibling_routes_cannot_bypass_decision(self, skill: str) -> None:
        """Incomplete, unavailable and candidate review evidence all pass the same checkpoint.

        The three sibling routes (a matching review that is incomplete, a requested report that is missing, and an
        unpromoted candidate) each name the decision, and the missing route no longer continues on its own.
        """
        incomplete = _section(skill, "- `matching-review-incomplete:", "\n- ")
        missing = _section(skill, "- When `REQUESTED_REPORT=true`", "\n- ")
        candidate = _section(skill, "For `matching-review-candidate-unpromoted:", f"### {DECISION_TITLE}")

        assert (
            [route for route in (incomplete, missing, candidate) if DECISION_TITLE not in route],
            found_phrases(missing, ("then continue available current-online/user finding intake",)),
        ) == ([], [])

    @pytest.mark.installed_plugin
    def test_available_findings_cannot_bypass_approval(self, skill: str, decision: str) -> None:
        """A bound answer is required even when usable findings already exist.

        The question fires only after compatibility assessment and feasible automatic recovery, honors any existing
        bound authorization, and none of the retired "continue by default" wordings may remain in the skill.
        """
        assert (
            missing_phrases(
                decision,
                (
                    "only after compatibility assessment and feasible automatic recovery",
                    "A version mismatch or repairable derived artifact alone does not trigger this question",
                    "`New review`, `Resume last one`, `Stop`",
                    "feasibility of each route",
                    "even when usable current-online/user findings or preliminary hypotheses exist",
                    "Continuing without a completed report requires explicit user confirmation",
                    "Honor existing bound authorization instead of asking again",
                    "A previously selected fresh-review-first route remains binding",
                    "Normal source verification and scope selection still precede edits",
                ),
            ),
            found_phrases(
                skill,
                (
                    "The default is available-finding continuation",
                    "only when no usable finding evidence exists",
                    "unless an explicit completed-review prerequisite applies",
                    "an explicit continue choice is needed only",
                    "available findings continue by default",
                    "without blocking usable findings solely on report bookkeeping",
                ),
            ),
        ) == ([], [])

    @pytest.mark.installed_plugin
    def test_unavailable_pr_question_links_target_without_claiming_source_review(self, decision: str) -> None:
        """Failed collection stays identifiable without presenting artifact closure as a review."""
        branch = _section(decision, "  - d. ", "\n- **Explicit free-text")

        assert (
            missing_phrases(decision, ("one native question naming the linked PR",)),
            missing_phrases(
                branch,
                (
                    "Explain the actual collection failure or newer-run identity",
                    "never call unavailable closure a completed source review",
                    "supersession",
                    "same three-option Incomplete review decision",
                    "neither a new run nor a resume choice waives them",
                ),
            ),
        ) == ([], [])
