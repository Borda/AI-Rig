"""Regression checks for session-local review-report remediation."""

from __future__ import annotations

from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
CODE_REMEDIATE_SKILL = PLUGIN_ROOT / "skills" / "code-remediate" / "SKILL.md"


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
    assert "Implemented —" in skill
    assert "Rejected —" in skill
    assert "Skipped / unselected —" in skill
    assert "Already closed —" in skill
    assert "Unresolved —" in skill


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


def test_work_buckets_bound_parallel_remediation_overhead() -> None:
    """Keep remediation fan-out disjoint, bounded, and user-approved."""
    skill = CODE_REMEDIATE_SKILL.read_text(encoding="utf-8")
    assert "Work Bucket Plan" in skill
    assert "at most five selected items" in skill
    assert "non-overlapping" in skill
    assert "five or fewer selected items" in skill
    assert "Do not spawn one specialist per finding" in skill
    assert "user confirms that exact digest before parallel dispatch" in skill
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
    assert "requires a new plan digest and approval" in skill
    assert "must not delete verification output after the command" in skill
    assert "re-plan that bucket as parent-owned or sequential" in skill


def test_parallel_preflight_does_not_require_future_implementation() -> None:
    """Permit planning a novel fix before postimages or passing regression assertions exist."""
    reference = CODE_REMEDIATE_SKILL.parent / "references" / "parallel-lifecycle.md"
    lifecycle = (reference if reference.exists() else CODE_REMEDIATE_SKILL).read_text(encoding="utf-8")
    baseline = lifecycle.index("against the unchanged baseline")
    freeze = lifecycle.index("Freeze only byte-identical command text")
    postimage = lifecycle.index("After implementation and before handover")
    assert baseline < freeze < postimage
    assert "expected baseline regression failures" in lifecycle
    assert "Do not create planned postimages before approval" in lifecycle
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


def test_scope_selection_question_keeps_options_with_visible_context() -> None:
    """Prevent selectable context and its question splitting across UI surfaces."""
    skill = CODE_REMEDIATE_SKILL.read_text(encoding="utf-8")
    scope_contract = skill.split("### Terminal Scope Context Contract", maxsplit=1)[1].split(
        "Record in `<run-directory>/resolution-scope.md`", maxsplit=1
    )[0]

    assert scope_contract.count("Which findings should I remediate?") == 1
    assert "exactly one user-visible assistant message containing, in order" in scope_contract
    assert "the exact unabridged `resolution-scope.md` content" in scope_contract
    assert "Do not open a second scope-selection control" in scope_contract
    assert "collapsed output" in scope_contract


def test_parallel_approval_question_has_one_rendering_owner() -> None:
    """Prevent plan approval appearing in prose and its interactive control."""
    skill = CODE_REMEDIATE_SKILL.read_text(encoding="utf-8")
    workplan_contract = skill.split("### 06: Build And Approve The Work Bucket Plan", maxsplit=1)[1].split(
        "### 07: Apply Fixes In Selected Scope", maxsplit=1
    )[0]

    assert workplan_contract.count("Approve these parallel work buckets?") == 1
    assert "The approval control is the sole owner of this question and its choices." in workplan_contract
    assert "must not contain the approval question or its choices" in workplan_contract
    assert "ask once in plain text instead of opening the control" in workplan_contract
