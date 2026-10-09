"""Regression checks for session-local review-report remediation."""

from __future__ import annotations

from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
CODE_REMEDIATE_SKILL = PLUGIN_ROOT / "skills" / "code-remediate" / "SKILL.md"


def test_report_discussion_stays_before_source_mutation_gates() -> None:
    """Keep accepted report discussion useful without pretending source remediation completed."""
    skill = CODE_REMEDIATE_SKILL.read_text(encoding="utf-8")
    discussion = skill.split("### Report discussion after deferred source recovery", 1)[1].split("### 03:", 1)[0]
    for boundary in (
        "An admitted assessed report plus the user's accepted source deferral",
        "Preserve `mode=pr`",
        "diagnosed collection-failure artifacts",
        "original ID",
        "missing observable closure evidence",
        "Do not require attached checkout, target integration, merge authorization, or a merge commit",
        "Do not create or promote a canonical remediation result",
        "does not authorize a fresh review",
        "execute steps 03–12 with incomplete source receipts",
        "edit source, merge, commit, claim findings fixed, or close missing independent coverage",
        "Producer-owned validation and independence remain mandatory",
        "first unmet checkpoint",
    ):
        assert boundary in discussion
    assert "For terminal remediation finalization" in skill
    assert "For source remediation in `mode=pr`, required before `action-items.md`, `resolution-scope.md`" in skill


def test_session_review_shortcut_reuses_local_review_without_pr_refresh() -> None:
    """Keep session-local remediation independent from fresh PR collection.

    A user who has just completed code review may deliberately remediate its artifact before checking online comments
    again. The shortcut must therefore select that assessed artifact in report mode and state its fail-closed boundary.
    """
    skill = CODE_REMEDIATE_SKILL.read_text(encoding="utf-8")
    assert "$code-remediate review" in skill
    assert "`mode=report`" in skill
    assert "latest assessed `code-review` result created in the current session" in skill
    assert "Do not collect PR evidence or fetch online review comments." in skill
    assert "For `mode=report`, normalize only the review report" in skill
    assert "If no assessed current-session review result is available, fail" in skill
    assert "Reject `review_status=unavailable` and `review_status=closed`" in skill


def test_final_summary_includes_all_ingested_items_with_outcomes() -> None:
    """Keep the final chat recap usable without reopening its artifact.

    A user needs to see the disposition of every review item, including rows skipped by their selection rather than only
    the unresolved work.
    """
    skill = CODE_REMEDIATE_SKILL.read_text(encoding="utf-8")
    assert "Final Outcome Table" in skill
    assert "every ingested item" in skill
    assert "Implemented:" in skill
    assert "Rejected:" in skill
    assert "Deferred:" in skill
    assert "Verified without code changes:" in skill
    assert "Blocked:" in skill
    assert "Needs clarification:" in skill
    assert "Never render bare `unresolved`" in skill


def test_visible_tables_use_compact_sources_without_dropping_details() -> None:
    """Keep selection and outcome tables readable without weakening the full ledger."""
    skill = CODE_REMEDIATE_SKILL.read_text(encoding="utf-8")
    assert "Item | Severity | Finding | Sources | Outcome | Evidence / next action" in skill
    assert "Every source has one owning item" in skill
    assert "complete body" in skill
    assert (
        "`report [<report-file>:<line>]`, `report [<report-json>#<finding-id>]`, or `online [<comment|thread|review-id>]`"
        in skill
    )
    assert "Keep full source records in metadata and expanded item records" in skill
    assert "layout=concise" in skill
    assert "# | Severity | Finding | Resolution proposal | Sources" in skill
    assert "report ×1; online ×2" in skill
    assert "Failure blocks the prompt and edits" in skill
    assert "omitted_source_records_total" in skill


def test_work_buckets_default_to_parallel_with_guarded_overlap() -> None:
    """Keep parallel remediation bounded without forcing parent-owned small scopes."""
    skill = CODE_REMEDIATE_SKILL.read_text(encoding="utf-8")
    assert "Work Bucket Plan" in skill
    assert "at most five selected items" in skill
    assert "Plan parallel dispatch by default after selection" in skill
    assert "same exact repo-relative file" in skill
    assert "do not manufacture a second task" in skill
    assert "Missing parallel capacity is not a reason to stop authorized work" in skill
    assert "continue with the available parent-owned or sequential route" in skill
    assert "without an approval prompt" in skill
    assert "approved_plan_sha256" in skill


def test_parallel_specialists_require_verified_production_lifecycle() -> None:
    """Prevent approved work buckets from masquerading as completed parallel writes."""
    skill = CODE_REMEDIATE_SKILL.read_text(encoding="utf-8") + (
        CODE_REMEDIATE_SKILL.parent / "references" / "parallel-lifecycle.md"
    ).read_text(encoding="utf-8")
    assert "Production Parallel Lifecycle" in skill
    assert "`parallel-specialists` is planning-only until" in skill
    assert "`schema_version=2`" in skill
    assert "parent-authoritative operational postcondition containment" in skill
    assert "`capability_sandbox_verified=false`" in skill
    assert "parent re-derives" in skill
    assert "lexical bucket ID order" in skill
    assert "durable reverse patch" in skill
    assert "non-force cleanup" in skill
    assert "generic `write_parallel_promoted` remains `false`" in skill
    assert "`code-remediate-shared-quality-gates`" in skill
    assert "records `structurally-verified`" in skill
    assert "does not execute or claim plan-provided commands" in skill
    assert "Re-hash every context pack at preparation and each authority transition" in skill
    assert "without passing shared-gate evidence" in skill


def test_parallel_child_verification_preserves_zero_output_boundary() -> None:
    """Prevent required child checks from producing hidden worktree output."""
    skill = (CODE_REMEDIATE_SKILL.parent / "references" / "parallel-lifecycle.md").read_text(encoding="utf-8")
    assert "zero ignored or untracked output" in skill
    assert "exact no-cache or no-output verification commands" in skill
    assert "Before hashing the plan, preflight every exact child verification command" in skill
    assert "Freeze only byte-identical command text that passed preflight" in skill
    assert "requires a new plan digest and dispatch record" in skill
    assert "must not delete verification output after the command" in skill
    assert "use an available parent-owned or sequential route" in skill
    assert "Do not ask permission to use that fallback" in skill


def test_parallel_preflight_does_not_require_future_implementation() -> None:
    """Permit planning a novel fix before postimages or passing regression assertions exist."""
    reference = CODE_REMEDIATE_SKILL.parent / "references" / "parallel-lifecycle.md"
    lifecycle = (reference if reference.exists() else CODE_REMEDIATE_SKILL).read_text(encoding="utf-8")
    baseline = lifecycle.index("against the unchanged baseline")
    freeze = lifecycle.index("Freeze only byte-identical command text")
    postimage = lifecycle.index("After implementation and before handover")
    assert baseline < freeze < postimage
    assert "expected baseline regression failures" in lifecycle
    assert "Do not create planned postimages before dispatch" in lifecycle
    assert "require exit zero on every exact approved child check" in lifecycle
    assert "must not delete verification output" in lifecycle


def test_parallel_details_load_only_for_a_selected_parallel_route() -> None:
    """Keep optional production mechanics out of the parent-only instruction load."""
    skill = CODE_REMEDIATE_SKILL.read_text(encoding="utf-8")
    reference = "references/parallel-lifecycle.md"
    assert "read [parallel-lifecycle.md]" in skill
    assert reference in skill
    assert "Only when evaluating or executing `parallel-specialists`" in skill
    assert "parent-owned and sequential routes do not load it" in skill
    assert "Only the parent may apply the integrated bundle" not in skill
    lifecycle = (CODE_REMEDIATE_SKILL.parent / reference).read_text(encoding="utf-8")
    for invariant in (
        "durable reverse patch",
        "rollback-ambiguous",
        "non-force cleanup",
        "capability_sandbox_verified=false",
    ):
        assert invariant in lifecycle


def test_pr_preparation_parallelism_stops_at_verified_merge_barrier() -> None:
    """Allow only read-only preparation to overlap before merge disposition is joined."""
    skill = CODE_REMEDIATE_SKILL.read_text(encoding="utf-8")
    preparation = skill.split("After successful remediation-mode collection", maxsplit=1)[1].split(
        "Write `<run-directory>/merge-prestage.md`", maxsplit=1
    )[0]

    assert "already-collected comments, reviews, and threads" in preparation
    assert "do not fetch the same review data again" in preparation
    assert "immutable fetched target OID" in preparation
    assert "Never run their fetches concurrently because each fetch changes `FETCH_HEAD`" in preparation
    assert "never run concurrent source or Git mutations" in preparation
    assert "The online-review arm must not read the worktree while integration may change it" in preparation
    assert "Join the preparation before writing `action-items.md` or `resolution-scope.md`" in preparation
    assert "present or likely conflicts require the authorization and completed merge gates below" in preparation
    assert (
        "A conflicted checkout or dirty path that overlaps required checkout, merge, or selected-edit paths is a stop condition"
        in preparation
    )


def test_scope_selection_question_keeps_options_with_visible_context() -> None:
    """Require the complete terminal table before one native control or a complete prose fallback."""
    skill = CODE_REMEDIATE_SKILL.read_text(encoding="utf-8")
    scope_contract = skill.split("### Terminal Scope Context Contract", maxsplit=1)[1].split(
        "Record in `<run-directory>/resolution-scope.md`", maxsplit=1
    )[0]

    assert scope_contract.count("Which findings should I remediate?") == 1
    assert "Choose the delivery route before emitting the scope context" in scope_contract
    assert "only the terminal scope context defined above" in scope_contract
    assert "let the control own the question and complete accepted syntax" in scope_contract
    assert "one final response containing the context, report link, and question" in scope_contract
    assert "Do not repeat the question/options in both prose and a native control" in scope_contract
    assert "immutable item/source inventory" in scope_contract
    assert "An async return or empty sync result leaves selection pending" in scope_contract
    assert "Async acceptance does not prove a selectable form appeared" in scope_contract
    assert "never say a scope control is visible" in scope_contract
    assert "For the observed `title`/`options`-only async schema" in scope_contract
    assert "Never add `id`, `header`, or `description` under that schema" in scope_contract
    assert "inspect the active schema because other hosts may differ" in scope_contract
    assert "Never offer `Choose severity groups or indexes`" in scope_contract
    assert '`options=["All", "Required", "Suggestions", "Custom selection"]`' in scope_contract
    assert "Never derive these groups from severity alone" in scope_contract
    assert "a non-blocking medium finding belongs to Suggestions" in scope_contract
    assert "a required low-severity evidence obligation belongs to Required" in scope_contract
    assert "record each group's exact indexes and source-backed rationale" in scope_contract
    assert "Custom selection is not a confirmed remediation scope" in scope_contract
    assert "Which finding indexes should I remediate?" in scope_contract
    assert "omit `options` only for that follow-up" in scope_contract
    assert "use a new decision ID bound to the same frozen inventory" in scope_contract
    assert "After an accepted async scope question, yield immediately" in scope_contract
    assert "even an empty final message" in scope_contract
    assert "dismissed after a later assistant action" in scope_contract
    assert "recover at the same frozen selection checkpoint" in scope_contract
    assert "collapsed output" in scope_contract


def test_parallel_default_uses_workflow_selected_fallback_without_prompt() -> None:
    """Keep eligible parallel work default and record fallback without an approval prompt."""
    skill = CODE_REMEDIATE_SKILL.read_text(encoding="utf-8")
    workplan_contract = skill.split("### 06: Build And Approve The Work Bucket Plan", maxsplit=1)[1].split(
        "### 07: Apply Fixes In Selected Scope", maxsplit=1
    )[0]

    assert "Authorize parent-owned or sequential fallback for this selected scope?" not in workplan_contract
    assert "`source=workflow-default`" in workplan_contract
    assert "`prompt_presented=false`" in workplan_contract
    assert (
        "In `CODE_REMEDIATE_METADATA.resolution_workplan`, also record `parallel_eligible=false`" in workplan_contract
    )
    assert "`parallel_approval_required=false`" in workplan_contract
    assert "`parallel_approval_status=parent-only`" in workplan_contract
    assert "exactly one nonempty `Ineligibility reason: <reason>` line" in workplan_contract


def test_parent_only_fallback_is_recorded_without_requesting_user_approval() -> None:
    """Require a concrete, evidence-checked workflow reason for fallback without a new prompt."""
    skill = CODE_REMEDIATE_SKILL.read_text(encoding="utf-8")
    quality_gates = skill.split("## Quality Gates", maxsplit=1)[1].split("## Calibration Hooks", maxsplit=1)[0]

    assert "requires an approval question" in skill
    assert "without an affirmative user choice" not in skill
    assert "parallel-fallback-not-recorded" in skill
    assert "workflow-default parent-owned or sequential fallback is missing" in skill
    assert "code-remediate-parallel-fallback-invalid" in skill
    assert "parent-only/sequential fallback recorded with its concrete ineligibility reason" in quality_gates


def test_blocked_opening_requires_current_cause_and_recovery() -> None:
    """Prevent abstract blocked headlines despite an otherwise detailed ledger."""
    for path in (CODE_REMEDIATE_SKILL, PLUGIN_ROOT / "shared" / "final-handoff-code-remediate.md"):
        contract = path.read_text(encoding="utf-8")
        assert "Never use bare `Blocked`" in contract
        assert "Both `outcome.title` and `outcome.summary` must name the specific current blocker" in contract
        assert "cite current-run gate or unresolved-item evidence" in contract
        assert "state the next owner/action needed to proceed" in contract
        assert "do not reuse a stale intake-review outcome" in contract


def test_scope_validation_preserves_obligation_groups() -> None:
    """Keep downstream validation from reclassifying optional medium or required low items."""
    skill = CODE_REMEDIATE_SKILL.read_text(encoding="utf-8")
    validation = skill.split("Validate before edit:", 1)[1].split("Each `out-of-scope`", 1)[0]
    assert "recorded obligation-based indexes" in validation
    assert "never recompute either group from severity" in validation
    assert "severity group selects every selectable matching severity" in validation
    assert "critical/high/medium" not in validation
    assert "selectable low items" not in validation


def test_terminal_selection_omits_item_details_but_preserves_saved_evidence() -> None:
    """Keep selection compact without dropping table rows, relevance counts or durable evidence."""
    skill = CODE_REMEDIATE_SKILL.read_text(encoding="utf-8")
    context = skill.split("### Terminal Scope Context Contract", 1)[1].split("### Upfront Decision Packet", 1)[0]
    assert "Stop the terminal display after the table, then append `## PR Relevance Summary`" in context
    assert "Do not print the `###` item groups" in context
    assert "`Context`, `Done when`, `Evidence` or `Related mentions`" in context
    assert "Keep these details in the saved scope document and full report" in context
    assert "omit this section when the inventory has no PR relevance data" in context
    assert "print only the terminal scope context defined above" in context
    assert "immediately after the table and optional PR Relevance Summary" in context
    assert "use the complete `resolution-scope.md` content" not in context
    for field in (
        "connected open items total",
        "connected selectable items total",
        "connected required followup total",
        "connected items marked out of scope",
    ):
        assert field in context
