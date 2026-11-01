"""Regression checks for session-local review-report remediation.

The code-remediate instructions must keep report discussion before source-mutation gates, select a just-completed review
without refreshing the PR, keep terminal tables compact, and bound parallel work buckets. The contracts live in
instruction Markdown, so each check locates headings line-wise and compares whitespace-collapsed text: re-wrapping a
paragraph must never break an assertion.
"""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path

import pytest
from _skill_text import flat, found_phrases, missing_phrases, read_with_references

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
CODE_REMEDIATE_SKILL = PLUGIN_ROOT / "skills" / "code-remediate" / "SKILL.md"
REFERENCES_DIR = CODE_REMEDIATE_SKILL.parent / "references"
REMEDIATE_REFERENCES = ("intake.md", "selection.md", "implementation.md", "completion.md")


def _section(text: str, start: str, end: str | None = None) -> str:
    """Return the whitespace-collapsed text after the first `start` marker, up to the next `end` marker if given.

    Example:
        >>> _section("# A\\nbody\\n## B\\nmore", "# A", "\\n## ")
        'body'
    """
    assert start in text, f"section marker {start!r} not found"
    body = text.split(start, 1)[1]
    return flat(body.split(end, 1)[0] if end else body)


def _out_of_order(text: str, phrases: tuple[str, ...]) -> list[str]:
    """Return all phrases unless every one is present and they appear in the given order, ignoring line wrapping.

    Example:
        >>> _out_of_order("a b c", ("a", "c"))
        []
    """
    collapsed = flat(text)
    positions = [collapsed.find(flat(phrase)) for phrase in phrases]
    in_order = positions[0] >= 0 and all(before < after for before, after in pairwise(positions))
    return [] if in_order else list(phrases)


@pytest.fixture
def skill() -> str:
    """Return the code-remediate SKILL.md joined with its always-loaded reference files."""
    return read_with_references(CODE_REMEDIATE_SKILL, *REMEDIATE_REFERENCES)


@pytest.fixture
def parallel_lifecycle() -> str:
    """Return the trigger-loaded parallel-lifecycle reference."""
    return (REFERENCES_DIR / "parallel-lifecycle.md").read_text(encoding="utf-8")


class TestReportRemediation:
    """Accepted report discussion and session-local review selection stay independent of source mutation."""

    def test_report_discussion_stays_before_source_mutation_gates(self) -> None:
        """Accepted report discussion stays useful without pretending source remediation completed.

        After a deferred source recovery the user may still discuss the admitted report; that discussion must create no
        remediation work, canonical result, merge or fix claim, and must not repeat a declined review request.
        """
        skill = read_with_references(CODE_REMEDIATE_SKILL, *REMEDIATE_REFERENCES, "source-recovery.md")
        discussion = _section(skill, "## Report discussion after deferred source recovery", "### 03:")

        assert (
            missing_phrases(
                discussion,
                (
                    "An admitted assessed report plus the user's accepted source deferral",
                    "Preserve `mode=pr`",
                    "diagnosed collection-failure artifacts",
                    "Retain finding IDs and evidence references",
                    "missing closure evidence",
                    "without creating remediation work or repeating declined review requests",
                    "Do not require attached checkout, target integration, merge authorization, or a merge commit",
                    "Do not create or promote a canonical remediation result",
                    "does not authorize a fresh review",
                    "execute steps 03–12 with incomplete source receipts",
                    "edit source, merge, commit, claim findings fixed, or close missing independent coverage",
                    "Producer-owned validation and independence remain mandatory",
                    "first unmet checkpoint",
                ),
            ),
            missing_phrases(
                skill,
                (
                    "For terminal remediation finalization",
                    "For source remediation in `mode=pr`, required before `action-items.md`, `resolution-scope.md`",
                ),
            ),
        ) == ([], [])

    def test_session_review_shortcut_reuses_local_review_without_pr_refresh(self, skill: str) -> None:
        """Session-local remediation does not depend on fresh PR collection.

        A user who has just completed code review may deliberately remediate its artifact before checking online
        comments again. The shortcut must therefore select that assessed artifact in report mode and state its fail-
        closed boundary.
        """
        assert (
            missing_phrases(
                skill,
                (
                    "$code-remediate review",
                    "`mode=report`",
                    "latest assessed `code-review` result created in the current session",
                    "Do not collect PR evidence or fetch online review comments.",
                    "For `mode=report`, normalize only the review report",
                    "If no assessed current-session review result is available, fail",
                    "Reject `review_status=unavailable` and `review_status=closed`",
                ),
            )
            == []
        )


class TestTerminalPresentation:
    """Selection and outcome tables stay compact in the terminal without weakening the saved ledger."""

    def test_final_summary_includes_all_ingested_items_with_outcomes(self, skill: str) -> None:
        """The final chat recap is usable without reopening its artifact.

        A user needs to see the disposition of every review item, including rows skipped by their selection rather than
        only the unresolved work.
        """
        assert (
            missing_phrases(
                skill,
                (
                    "Final Outcome Table",
                    "every ingested item",
                    "Implemented:",
                    "Rejected:",
                    "Deferred:",
                    "Verified without code changes:",
                    "Blocked:",
                    "Needs clarification:",
                    "Never render bare `unresolved`",
                ),
            )
            == []
        )

    def test_visible_tables_use_compact_sources_without_dropping_details(self, skill: str) -> None:
        """Selection and outcome tables stay readable without weakening the full ledger."""
        assert (
            missing_phrases(
                skill,
                (
                    "Item | Severity | Finding | Sources | Outcome | Evidence / next action",
                    "Every source has one owning item",
                    "complete body",
                    "`report [<report-file>:<line>]`, `report [<report-json>#<finding-id>]`, or `online"
                    " [<comment|thread|review-id>]`",
                    "Keep full source records in metadata and expanded item records",
                    "layout=concise",
                    "# | Severity | Finding | Resolution proposal | Sources",
                    "report ×1; online ×2",
                    "Failure blocks the prompt and edits",
                    "omitted_source_records_total",
                ),
            )
            == []
        )

    def test_scope_selection_question_keeps_options_with_visible_context(self, skill: str) -> None:
        """The complete terminal table precedes one native control or a complete prose fallback.

        The question text appears once, the options follow obligation-based groups rather than severity alone, and an
        accepted async question yields immediately instead of claiming a control is visible.
        """
        scope_contract = _section(
            skill, "### Terminal Scope Context Contract", "Record in `<run-directory>/resolution-scope.md`"
        )

        assert (
            scope_contract.count("Which findings should I remediate?"),
            missing_phrases(
                scope_contract,
                (
                    "Choose the delivery route before emitting the scope context",
                    "only the terminal scope context defined above",
                    "let the control own the question and complete accepted syntax",
                    "one final response containing the context, report link, and question",
                    "Do not repeat the question/options in both prose and a native control",
                    "immutable item/source inventory",
                    "An async return or empty sync result leaves selection pending",
                    "Async acceptance does not prove a selectable form appeared",
                    "never say a scope control is visible",
                    "For the observed `title`/`options`-only async schema",
                    "Never add `id`, `header`, or `description` under that schema",
                    "inspect the active schema because other hosts may differ",
                    "Never offer `Choose severity groups or indexes`",
                    '`options=["All", "Required", "Suggestions", "Custom selection"]`',
                    "Never derive these groups from severity alone",
                    "a non-blocking medium finding belongs to Suggestions",
                    "a required low-severity evidence obligation belongs to Required",
                    "record each group's exact indexes and source-backed rationale",
                    "Custom selection is not a confirmed remediation scope",
                    "Which finding indexes should I remediate?",
                    "omit `options` only for that follow-up",
                    "use a new decision ID bound to the same frozen inventory",
                    "After an accepted async scope question, yield immediately",
                    "even an empty final message",
                    "dismissed after a later assistant action",
                    "recover at the same frozen selection checkpoint",
                    "collapsed output",
                ),
            ),
        ) == (1, [])

    def test_scope_validation_preserves_obligation_groups(self, skill: str) -> None:
        """Downstream validation does not reclassify optional medium or required low items."""
        validation = _section(skill, "Validate before edit:", "Each `out-of-scope`")

        assert (
            missing_phrases(
                validation,
                (
                    "recorded obligation-based indexes",
                    "never recompute either group from severity",
                    "severity group selects every selectable matching severity",
                ),
            ),
            found_phrases(validation, ("critical/high/medium", "selectable low items")),
        ) == ([], [])

    def test_report_context_does_not_automatically_become_selectable_work(self) -> None:
        """Review limitations stay visible without turning into unsolicited work."""
        selection = (REFERENCES_DIR / "selection.md").read_text(encoding="utf-8")
        choices = _section(selection, "Selectable items:", "### Terminal Scope Context Contract")

        assert (
            missing_phrases(
                choices,
                (
                    "confirmed source defects or genuine project-required checks",
                    "retain other report context as review limitations",
                ),
            ),
            found_phrases(
                choices, ("include report-origin failed checks, follow-ups, required next work, confidence gaps",)
            ),
            missing_phrases(
                selection,
                (
                    "Historical obligation version 1 still requires its original source-body coverage",
                    "`context_sources` directly from original bytes",
                ),
            ),
        ) == ([], [], [])

    def test_terminal_selection_omits_item_details_but_preserves_saved_evidence(self, skill: str) -> None:
        """Selection stays compact without dropping table rows, relevance counts or durable evidence."""
        context = _section(skill, "### Terminal Scope Context Contract", "### Upfront Decision Packet")

        assert (
            missing_phrases(
                context,
                (
                    "list each path as `- <item>: <file-path>`",
                    "Then append `## PR Relevance Summary`",
                    "Do not print the `###` item groups",
                    "`Context`, `Done when`, `Evidence` or `Related mentions`",
                    "Keep these details in the saved scope document and full report",
                    "omit this section when the inventory has no PR relevance data",
                    "print only the terminal scope context defined above",
                    "Place the item-labelled path list directly below the table",
                    "optional PR Relevance Summary and Full report path",
                    "connected open items total",
                    "connected selectable items total",
                    "connected required followup total",
                    "connected items marked out of scope",
                ),
            ),
            found_phrases(context, ("use the complete `resolution-scope.md` content",)),
        ) == ([], [])

    @pytest.mark.parametrize(
        ("path", "references"),
        [
            pytest.param(CODE_REMEDIATE_SKILL, ("completion.md",), id="skill"),
            pytest.param(PLUGIN_ROOT / "shared" / "final-handoff-code-remediate.md", (), id="final-handoff-contract"),
        ],
    )
    def test_blocked_opening_requires_current_cause_and_recovery(self, path: Path, references: tuple[str, ...]) -> None:
        """A blocked headline names the current cause and recovery, never an abstract `Blocked`.

        Both the skill and the final-handoff contract must demand a specific blocker in the title and summary, current
        evidence, and a next owner, so a detailed ledger cannot hide behind a bare headline.
        """
        assert (
            missing_phrases(
                read_with_references(path, *references),
                (
                    "Never use bare `Blocked`",
                    "Both `outcome.title` and `outcome.summary` must name the specific current blocker",
                    "cite current-run gate or unresolved-item evidence",
                    "state the next owner/action needed to proceed",
                    "do not reuse a stale intake-review outcome",
                ),
            )
            == []
        )


class TestParallelWorkBuckets:
    """Parallel remediation is the bounded default, with production mechanics loaded only when selected."""

    def test_work_buckets_default_to_parallel_with_guarded_overlap(self, skill: str) -> None:
        """Parallel remediation stays bounded without forcing parent-owned small scopes."""
        assert (
            missing_phrases(
                skill,
                (
                    "Work Bucket Plan",
                    "at most five selected items",
                    "Plan parallel dispatch by default after selection",
                    "same exact repo-relative file",
                    "do not manufacture a second task",
                    "Missing parallel capacity is not a reason to stop authorized work",
                    "continue with the available parent-owned or sequential route",
                    "without an approval prompt",
                    "approved_plan_sha256",
                ),
            )
            == []
        )

    def test_specialists_require_verified_production_lifecycle(self, skill: str, parallel_lifecycle: str) -> None:
        """Approved work buckets cannot masquerade as completed parallel writes."""
        assert (
            missing_phrases(
                skill + parallel_lifecycle,
                (
                    "Production Parallel Lifecycle",
                    "`parallel-specialists` is planning-only until",
                    "`schema_version=2`",
                    "parent-authoritative operational postcondition containment",
                    "`capability_sandbox_verified=false`",
                    "parent re-derives",
                    "lexical bucket ID order",
                    "durable reverse patch",
                    "non-force cleanup",
                    "generic `write_parallel_promoted` remains `false`",
                    "`code-remediate-shared-quality-gates`",
                    "records `structurally-verified`",
                    "does not execute or claim plan-provided commands",
                    "Re-hash every context pack at preparation and each authority transition",
                    "without passing shared-gate evidence",
                ),
            )
            == []
        )

    def test_child_verification_preserves_zero_output_boundary(self, parallel_lifecycle: str) -> None:
        """Required child checks cannot produce hidden worktree output."""
        assert (
            missing_phrases(
                parallel_lifecycle,
                (
                    "zero ignored or untracked output",
                    "exact no-cache or no-output verification commands",
                    "Before hashing the plan, preflight every exact child verification command",
                    "Freeze only byte-identical command text that passed preflight",
                    "requires a new plan digest and dispatch record",
                    "must not delete verification output after the command",
                    "use an available parent-owned or sequential route",
                    "Do not ask permission to use that fallback",
                ),
            )
            == []
        )

    def test_preflight_does_not_require_future_implementation(self, parallel_lifecycle: str) -> None:
        """A novel fix can be planned before postimages or passing regression assertions exist.

        Child checks are preflighted against the unchanged baseline, frozen, and only then required to pass after
        implementation, so the three milestones must appear in that order.
        """
        assert (
            _out_of_order(
                parallel_lifecycle,
                (
                    "against the unchanged baseline",
                    "Freeze only byte-identical command text",
                    "After implementation and before handover",
                ),
            ),
            missing_phrases(
                parallel_lifecycle,
                (
                    "expected baseline regression failures",
                    "Do not create planned postimages before dispatch",
                    "require exit zero on every exact approved child check",
                    "must not delete verification output",
                ),
            ),
        ) == ([], [])

    def test_details_load_only_for_a_selected_parallel_route(self, skill: str, parallel_lifecycle: str) -> None:
        """Optional production mechanics stay out of the parent-only instruction load.

        The skill links the lifecycle reference only for a `parallel-specialists` route, and the reference itself keeps
        the rollback, cleanup and sandbox invariants.
        """
        assert (
            missing_phrases(
                skill,
                (
                    "read [parallel-lifecycle.md]",
                    "](parallel-lifecycle.md)",
                    "Only when evaluating or executing `parallel-specialists`",
                    "parent-owned and sequential routes do not load it",
                ),
            ),
            found_phrases(skill, ("Only the parent may apply the integrated bundle",)),
            missing_phrases(
                parallel_lifecycle,
                (
                    "durable reverse patch",
                    "rollback-ambiguous",
                    "non-force cleanup",
                    "capability_sandbox_verified=false",
                ),
            ),
        ) == ([], [], [])

    def test_pr_preparation_parallelism_stops_at_verified_merge_barrier(self, skill: str) -> None:
        """Only read-only preparation may overlap before the merge disposition is joined."""
        preparation = _section(
            skill, "After successful remediation-mode collection", "Write `<run-directory>/merge-prestage.md`"
        )

        assert (
            missing_phrases(
                preparation,
                (
                    "already-collected comments, reviews, and threads",
                    "do not fetch the same review data again",
                    "immutable fetched target OID",
                    "Never run their fetches concurrently because each fetch changes `FETCH_HEAD`",
                    "never run concurrent source or Git mutations",
                    "The online-review arm must not read the worktree while integration may change it",
                    "Join the preparation before writing `action-items.md` or `resolution-scope.md`",
                    "present or likely conflicts require the authorization and completed merge gates below",
                    "A conflicted checkout or dirty path that overlaps required checkout, merge, or selected-edit paths is a stop"
                    " condition",
                ),
            )
            == []
        )

    def test_default_uses_workflow_selected_fallback_without_prompt(self, skill: str) -> None:
        """Eligible parallel work stays the default and a fallback is recorded without an approval prompt."""
        workplan_contract = _section(
            skill, "### 06: Build And Approve The Work Bucket Plan", "### 07: Apply Fixes In Selected Scope"
        )

        assert (
            missing_phrases(
                workplan_contract,
                (
                    "`source=workflow-default`",
                    "`prompt_presented=false`",
                    "In `CODE_REMEDIATE_METADATA.resolution_workplan`, also record `parallel_eligible=false`",
                    "`parallel_approval_required=false`",
                    "`parallel_approval_status=parent-only`",
                    "exactly one nonempty `Ineligibility reason: <reason>` line",
                ),
            ),
            found_phrases(
                workplan_contract, ("Authorize parent-owned or sequential fallback for this selected scope?",)
            ),
        ) == ([], [])

    def test_parent_only_fallback_is_recorded_without_requesting_user_approval(self, skill: str) -> None:
        """A concrete, evidence-checked workflow reason is required for fallback without a new prompt."""
        quality_gates = _section(skill, "## Quality Gates", "## Calibration Hooks")

        assert (
            missing_phrases(
                skill,
                (
                    "requires an approval question",
                    "parallel-fallback-not-recorded",
                    "workflow-default parent-owned or sequential fallback is missing",
                    "code-remediate-parallel-fallback-invalid",
                ),
            ),
            found_phrases(skill, ("without an affirmative user choice",)),
            missing_phrases(
                quality_gates, ("parent-only/sequential fallback recorded with its concrete ineligibility reason",)
            ),
        ) == ([], [], [])
