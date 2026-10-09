#!/usr/bin/env python3
"""Validate code-review artifacts for multi-axis evidence and merge-decision integrity.

## Purpose

ensure a review recommendation is traceable to scope, specialist routing, gates, findings, and the required action
table. It gives the code-review workflow a mechanical final check that connects the decision to the evidence files and
specialist outputs it claims to use.

## Scope

reads a completed local review artifact and rejects contract violations; it neither collects GitHub data nor performs a
source-code review itself. Validation covers normal reviewed results, explicitly unavailable-review results, and
proposal-level close results, including path containment and provenance checks for referenced files.

## Usage

run this validator from the code-review workflow after all evidence and draft result files have been written. Provide
the review output directory and candidate ``result.json`` through the CLI. ``--project-root`` remains accepted for
command-line compatibility. Code Review preflight and candidates bind installed role cards to retained run-local copies;
promoted schema-three results validate those retained bytes. The challenge-resolve owner may run ``--manifest-only
--challenge-only`` to validate exactly one independent challenger without Code Review's other specialist routing; this
mode still checks the same reviewer execution, role card, source, and output evidence.

## Used by

the ``code-review`` skill's terminal validation gate and review-artifact contract tests. Maintainers can also run it
while diagnosing an incomplete artifact, but it is not a replacement for collecting the diff, remote review data, or
specialist analysis.

## Outputs

accepts a coherent review artifact or emits an explicit contract failure for missing routing, source evidence, close
evidence, decision rationale, or action-table cells. Successful validation returns a zero exit status, while failures
identify the violated contract so the workflow can stop before presenting a merge recommendation. ``--all-errors``
with ``--result`` runs every review check once and prints JSON listing each failed check, plus checks not run because a
prerequisite failed; any listed failure still exits nonzero, and the default fail-fast output is unchanged.

## Failure

untriaged specialist output, unsupported recommendation, inconsistent PR evidence, an invalid close disposition, or a
non-accept decision without merge blocks exits non-zero. It also rejects artifacts that reference files outside the
review output directory or claim a terminal result while retaining forbidden detailed-review artifacts. A fresh
candidate for a Python diff also fails without a valid, non-skipped Codemap probe artifact or with a specialist
follow-up query outside its documented routes and per-specialist limit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, NamedTuple

#: Keep the installed skill helper importable when pytest loads this validator by file path.
SKILL_DIRECTORY = Path(__file__).resolve().parent
#: Root of the Codex Rig plugin, from which the shared directory is located.
PLUGIN_ROOT = SKILL_DIRECTORY.parents[1]
if str(SKILL_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SKILL_DIRECTORY))
#: Plugin shared directory placed on the import path for shared helper modules.
SHARED_DIRECTORY = PLUGIN_ROOT / "shared"
if str(SHARED_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SHARED_DIRECTORY))

from codemap_adapter import ARTIFACT_SCHEMA_VERSION as CODEMAP_ARTIFACT_SCHEMA_VERSION  # noqa: E402
from codemap_adapter import FOLLOW_UP_ELIGIBLE_STATUSES, FOLLOW_UP_QUERY_KINDS, FOLLOW_UP_QUERY_LIMIT  # noqa: E402
from codemap_adapter import FOLLOW_UP_SUBCOMMANDS as CODEMAP_FOLLOW_UP_SUBCOMMANDS  # noqa: E402
from codemap_adapter import PROTOCOL_VERSION as CODEMAP_PROTOCOL_VERSION  # noqa: E402
from codemap_adapter import STATUS_AVAILABLE as CODEMAP_STATUS_AVAILABLE  # noqa: E402
from codemap_adapter import STATUS_SKIPPED as CODEMAP_STATUS_SKIPPED  # noqa: E402
from codemap_adapter import STATUSES as CODEMAP_STATUSES  # noqa: E402
from codemap_adapter import QueryOutcome as CodemapQueryOutcome  # noqa: E402
from codemap_adapter import change_set_gap as codemap_change_set_gap  # noqa: E402
from codemap_adapter import gap_reasons as codemap_gap_reasons  # noqa: E402
from codemap_adapter import provider_rejected_input as codemap_provider_rejected_input  # noqa: E402
from codemap_adapter import read_diff_text as codemap_read_diff_text  # noqa: E402
from codemap_adapter import reduce_status as codemap_reduce_status  # noqa: E402
from codemap_adapter import unquote_git_path as codemap_unquote_git_path  # noqa: E402
from collect_pr import dirty_path_record_is_consistent  # noqa: E402
from local_reviewer_wave import ReviewRouteError as ReviewRouteError  # noqa: E402
from parallel_execution import _SECRET_PATTERNS as _SECRET_PATTERNS  # noqa: E402
from parallel_execution import validate_inspection_contexts as validate_inspection_contexts  # noqa: E402
from parallel_execution import validate_read_only_runtime as validate_read_only_runtime  # noqa: E402
from review_context import context_pages as context_pages  # noqa: E402
from review_context import dispatch_message as dispatch_message  # noqa: E402
from review_context import render_read_call as render_read_call  # noqa: E402
from review_context import render_read_output as render_read_output  # noqa: E402
from review_execution_validation import FINDING_SEVERITIES as FINDING_SEVERITIES  # noqa: E402
from review_execution_validation import LEGACY_ALL_PAGE_READER_SHA256 as LEGACY_ALL_PAGE_READER_SHA256  # noqa: E402
from review_execution_validation import (  # noqa: E402
    LEGACY_PROTOCOL_V6_READER_SHA256 as LEGACY_PROTOCOL_V6_READER_SHA256,
)
from review_execution_validation import (  # noqa: E402
    LEGACY_SINGLE_CALL_READER_SHA256 as LEGACY_SINGLE_CALL_READER_SHA256,
)
from review_execution_validation import LEGACY_WORKDIR_READER_SHA256S as LEGACY_WORKDIR_READER_SHA256S  # noqa: E402
from review_execution_validation import REQUIRED_ROLES as REQUIRED_ROLES  # noqa: E402
from review_execution_validation import TRANSIENT_RETRY_ERRORS as TRANSIENT_RETRY_ERRORS  # noqa: E402
from review_execution_validation import _assessment_format_repair as _assessment_format_repair  # noqa: E402
from review_execution_validation import _batch_reviewer_findings as _batch_reviewer_findings  # noqa: E402
from review_execution_validation import _binary_source_diagnostic as _binary_source_diagnostic  # noqa: E402
from review_execution_validation import _child_controls as _child_controls  # noqa: E402
from review_execution_validation import _closure_shape_repair as _closure_shape_repair  # noqa: E402
from review_execution_validation import _event_payloads as _event_payloads  # noqa: E402
from review_execution_validation import _find_rollout as _find_rollout  # noqa: E402
from review_execution_validation import _finding_id_namespace_repair as _finding_id_namespace_repair  # noqa: E402
from review_execution_validation import _inspection_child_called_tool as _inspection_child_called_tool  # noqa: E402
from review_execution_validation import _joined_terminal_result as _joined_terminal_result  # noqa: E402
from review_execution_validation import _joined_terminal_timestamp as _joined_terminal_timestamp  # noqa: E402
from review_execution_validation import (  # noqa: E402
    _literal_duplicated_plan_command as _literal_duplicated_plan_command,
)
from review_execution_validation import _load_json as _load_json  # noqa: E402
from review_execution_validation import _load_role_card as _load_role_card  # noqa: E402
from review_execution_validation import _manifest_passes as _manifest_passes  # noqa: E402
from review_execution_validation import _missing_reader_error_path as _missing_reader_error_path  # noqa: E402
from review_execution_validation import _native_dispatch_message as _native_dispatch_message  # noqa: E402
from review_execution_validation import _native_independent_wave as _native_independent_wave  # noqa: E402
from review_execution_validation import _native_read_frame as _native_read_frame  # noqa: E402
from review_execution_validation import _native_recipe_plan as _native_recipe_plan  # noqa: E402
from review_execution_validation import _original_dispatch_message as _original_dispatch_message  # noqa: E402
from review_execution_validation import _paged_native_manifest as _paged_native_manifest  # noqa: E402
from review_execution_validation import _parent_timestamp as _parent_timestamp  # noqa: E402
from review_execution_validation import _read_jsonl as _read_jsonl  # noqa: E402
from review_execution_validation import _reader_command_matches as _reader_command_matches  # noqa: E402
from review_execution_validation import _receipt_binds_child as _receipt_binds_child  # noqa: E402
from review_execution_validation import _recovery_arguments as _recovery_arguments  # noqa: E402
from review_execution_validation import _related_capacity_release as _related_capacity_release  # noqa: E402
from review_execution_validation import _resolve_path as _resolve_path  # noqa: E402
from review_execution_validation import _retained_reviewer_rating as _retained_reviewer_rating  # noqa: E402
from review_execution_validation import _sha256 as _sha256  # noqa: E402
from review_execution_validation import _text_reviewer_assessment as _text_reviewer_assessment  # noqa: E402
from review_execution_validation import _validate_context_read as _validate_context_read  # noqa: E402
from review_execution_validation import _validate_inspection_plan as _validate_inspection_plan  # noqa: E402
from review_execution_validation import (  # noqa: E402
    _validate_instruction_bounded_review as _validate_instruction_bounded_review,
)
from review_execution_validation import _validate_local_reviewer_wave as _validate_local_reviewer_wave  # noqa: E402
from review_execution_validation import _validate_native_schedule as _validate_native_schedule  # noqa: E402
from review_execution_validation import _validate_spawn_attempts as _validate_spawn_attempts  # noqa: E402
from review_execution_validation import (  # noqa: E402
    validate_local_reviewer_evidence as validate_local_reviewer_evidence,
)
from review_routing import ROUTING_SIGNALS, derive_mechanical_risk  # noqa: E402
from run_gates import (  # noqa: E402
    _aggregate_worker_proofs,
    _local_snapshot_bytes,
    inspect_collected_test,
    validate_local_gate_source,
)

#: Section headings a completed review report must contain.
REQUIRED_SECTIONS = (
    "Decision Summary",
    "Scope",
    "Risk Tier",
    "Files Inspected",
    "Specialist Passes",
    "Specialist Manifest",
    "Findings",
    "No-Finding Residual Risks",
    "Confidence Gaps",
    "Confidence Calibration",
)
#: Recommendation values a review verdict may use.
VALID_RECOMMENDATIONS = {"accept-as-is", "minor-changes", "needs-more-work", "reject", "not-aligned"}
#: Reason codes that justify closing a PR at the close gate.
CLOSE_CODES = {
    "FALSE_GOAL",
    "BREAKING_CONDUCT",
    "WRONG_SCOPE",
    "WRONG_PROVENANCE",
    "DUPLICATE",
    "UNADDRESSED_REVERT",
    "SPAM",
    "ARCHITECTURE_VIOLATION",
}
#: Heading of the report section that holds the findings and merge-blocks action table.
ACTION_TABLE_SECTION = "Review Findings and Merge Blocks"
#: Column headers of the action table; PR reports insert an Author column after the first.
ACTION_TABLE_HEADERS = ("Finding / area", "Required change", "Evidence", "Status")
#: Every role identifier that a specialist manifest may name.
ALL_MANIFEST_ROLES = {
    "sw-engineer",
    "qa-specialist",
    "challenger",
    "solution-architect",
    "security-auditor",
    "data-steward",
    "cicd-steward",
    "linting-expert",
    "doc-scribe",
    "oss-shepherd",
    "squeezer",
    "scientist",
    "web-explorer",
}
#: Risk tiers that require an independent review pass.
INDEPENDENT_PASS_TIERS = {"BROAD", "HIGH_RISK"}
#: Execution modes a specialist pass may record.
VALID_MODES = {"spawned", "substituted", "app-server", "inspection"}
#: Roles that need a recorded explicit user selection of the Sol model when routed.
SOL_ROLES = {"solution-architect", "security-auditor"}
#: Three lines of the note written when PR review is unavailable, stating that nothing was assessed or decided.
UNAVAILABLE_NOTE_LINES = (
    "PR Review Availability: unavailable",
    "Source findings: not assessed",
    "Merge decision: not made",
)
#: Top-level result keys allowed in an unavailable-review result.
UNAVAILABLE_RESULT_KEYS = {
    "schema_version",
    "status",
    "checks_run",
    "checks_failed",
    "findings",
    "confidence",
    "artifact_path",
    "metadata",
}
#: Metadata keys allowed in an unavailable-review result.
UNAVAILABLE_METADATA_KEYS = {
    "scope",
    "risk_tier",
    "review_status",
    "collection_failure",
    "confidence_gaps",
    "confidence_gap_closures",
    "confidence_recovery",
    "final_handoff",
}
#: Artifact names that must not exist when review is unavailable, as source review never ran.
UNAVAILABLE_FORBIDDEN_ARTIFACTS = {"local-checkout.json", "specialist-manifest.json"}
#: The only confidence gap text an unavailable-review result may report.
UNAVAILABLE_CONFIDENCE_GAP = (
    "Core PR source verification did not complete; no source review or merge decision was made."
)
#: Confidence gap text a closed-PR result must report for the skipped source review.
CLOSED_CONFIDENCE_GAP = "Detailed source review was intentionally skipped after the close gate."
#: Top-level result keys allowed in a closed-PR result, the same set as for unavailable reviews.
CLOSED_RESULT_KEYS = UNAVAILABLE_RESULT_KEYS
#: Metadata keys allowed in a closed-PR result, which carries a close decision.
CLOSED_METADATA_KEYS = {
    "scope",
    "risk_tier",
    "review_status",
    "close_decision",
    "confidence_gaps",
    "confidence_gap_closures",
    "confidence_recovery",
    "final_handoff",
}
#: PR artifact files that must be present for a closed-PR review.
CLOSED_REQUIRED_PR_ARTIFACTS = {
    "pr.json",
    "pr-routing.json",
    "remote-selection.json",
    "target-branch.json",
    "local-checkout.json",
    "comments.json",
    "reviews.json",
    "review-threads.json",
    "unresolved-review-threads.json",
    "online-review-summary.json",
    "diff.patch",
}
#: Artifacts that must not exist for a closed-PR review because detailed source review is skipped.
CLOSED_FORBIDDEN_ARTIFACTS = {"codemap-context.json", "review-routing.json", "specialist-manifest.json", "specialists"}
#: Changed-file suffix the provider maps from a diff file to modules; a diff touching one requires the probe artifact.
CODEMAP_PYTHON_SUFFIX = ".py"
#: Run artifact the required structural probe persists once for an assessed review.
CODEMAP_CONTEXT_ARTIFACT = "codemap-context.json"
#: Run directory holding each specialist's bounded follow-up query artifacts.
CODEMAP_FOLLOW_UP_DIRECTORY = "codemap-followups"
#: Follow-up artifact name: lowercase role id, then a two-digit sequence checked against the per-specialist limit.
CODEMAP_FOLLOW_UP_NAME = re.compile(r"(?P<role>[a-z][a-z0-9-]*)-(?P<sequence>[0-9]{2})\.json\Z")
#: Artifact schemas written before the review batch read a diff file; readable only for a recovered historical run.
CODEMAP_RECOVERED_ARTIFACT_SCHEMAS = frozenset({3})
#: Result metadata value that records a recovered pre-rule run as exempt from the required probe.
CODEMAP_HISTORICAL_EXEMPTION = "not-required-historical"
#: File only the native-provenance recovery helper writes; its inputs are manifests current preparation never emits.
CODEMAP_RECOVERY_MARKER = Path("native-recovery") / "inspection-summary.json"
#: Candidate manifest the recovery helper writes beside its marker, and the fixed identity fields it always carries.
CODEMAP_RECOVERY_CANDIDATE = "specialist-manifest.native-recovery.candidate.json"
CODEMAP_RECOVERY_IDENTITY = {
    "manifest_kind": "native-wave",
    "dispatch_protocol": "paged-context-v6",
}
#: Native-wave manifest schema versions the recovery candidate may carry. A reader of the existing native-wave family
#: that ``review_prepare.py`` produces, never a producer, so it is expressed as a membership set like the other
#: native-wave readers rather than as a new versioned literal.
CODEMAP_RECOVERY_SCHEMA_VERSIONS = frozenset({8})
#: Confidence gap text required when PR review-thread resolution status could not be retrieved.
PR_THREAD_CONFIDENCE_GAP = "PR review-thread resolution status was unavailable; online review triage may be incomplete."
#: Highest confidence score a review that fell back to public PR data may report.
PR_PUBLIC_FALLBACK_MAX_CONFIDENCE = 0.89
#: Recovery advice shown for each unavailable-review action class: network, retry, auth, install, identity, report.
UNAVAILABLE_RECOVERY_ACTIONS = {
    "network": (
        "Check effective runtime access; if required access is missing or unknown and requests are allowed, "
        "request runtime approval for the complete collector. Respect an explicit denial or non-overridable "
        "restriction; retry only after approval or an evidenced state change."
    ),
    "retry": "Diagnose the transport failure or rate limit; retry only after an evidenced state change.",
    "auth": "Repair local gh access privately, verify repository access, then retry.",
    "install": "Install or repair gh locally, then retry.",
    "identity": "Confirm the canonical PR URL and repository identity, then retry.",
    "report": "Stop and report this Codex Rig collector failure with sanitized artifacts.",
}
#: Sentence appended to recovery advice when a local checkout had already started.
CHECKOUT_STATE_RECOVERY_SUFFIX = " Inspect the local checkout state before retrying."
#: Pattern for failure class, label, and reason identifiers: lowercase letters, digits, and hyphens.
SAFE_DIAGNOSTIC_IDENTIFIER = re.compile(r"[a-z][a-z0-9-]*\Z")
#: Pattern for the only gh pr checkout command, naming a GitHub PR URL, that a diagnostic may recommend.
SAFE_GH_CHECKOUT_COMMAND = re.compile(
    r"gh pr checkout https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[1-9][0-9]*\Z"
)
#: Plain-language summary for each collector failure label, stating that the review has not started.
UNAVAILABLE_HUMAN_SUMMARIES = {
    "gh-pr-view": "I could not retrieve the PR metadata, so the review has not started.",
    "local-pr-checkout": "I could not check out the latest PR commit, so the review has not started.",
    "checkout-paths": "I could not compare your local files with the latest PR commit, so the review has not started.",
    "checkout-branch": "I could not verify the local PR checkout, so the review has not started.",
    "checkout-head": "I could not verify the local PR checkout, so the review has not started.",
    "local-pr-diff": "I could not compare the checked-out PR files, so the review has not started.",
    "dirty-tracked-worktree-overlap-before-pr-checkout": (
        "I stopped before checkout because it could overwrite your local files, so the review has not started."
    ),
    "dirty-pr-worktree-before-pr-checkout": (
        "I stopped before checkout because local changes overlap PR files, so the review has not started."
    ),
    "dirty-pr-worktree-after-pr-checkout": (
        "I stopped after checkout because local changes overlap PR files, so the review has not started."
    ),
    "unresolved-index-before-pr-checkout": (
        "I stopped before checkout because the Git index has unresolved entries, so the review has not started."
    ),
    "unresolved-index-after-pr-checkout": (
        "I stopped after checkout because the Git index has unresolved entries, so the review has not started."
    ),
    "target-branch-fetch": "I could not refresh the target branch before verification, so the review has not started.",
    "pr-head-fetch": "I could not refresh the PR source before verification, so the review has not started.",
    "public-pr-head-fetch": "I could not refresh the PR source before verification, so the review has not started.",
    "historical-pr-head-fetch": "I could not refresh the PR source before verification, so the review has not started.",
}
#: Summary used when a collector failure label has no specific entry in the human summaries.
UNAVAILABLE_GENERIC_SUMMARY = "Source collection stopped before I could verify which PR revision to review."
#: Ordered gate check ids that an unavailable-review result must list.
UNAVAILABLE_GATE_IDS = ("lint", "format", "types", "tests", "review")
#: Reason classifications a PR fetch failure diagnostic may report.
PR_HEAD_FETCH_FAILURE_REASONS = {
    "ref-update-rejected",
    "remote-ref-not-found",
    "transport",
    "permission",
    "repository-unavailable",
    "unknown",
}
#: Collector failure labels that come from fetching the target branch or PR head.
FETCH_FAILURE_LABELS = {
    "target-branch-fetch",
    "pr-head-fetch",
    "public-pr-head-fetch",
    "historical-pr-head-fetch",
}
#: Recovery advice for each classified fetch failure reason.
PR_HEAD_FETCH_RECOVERY_ACTIONS = {
    "ref-update-rejected": "Resolve the local Git reference rejection, then start a fresh collector run.",
    "remote-ref-not-found": "Refresh the PR metadata and confirm a current PR head exists, then start a fresh collector run.",
    "transport": "Restore transport access, then start a fresh collector run.",
    "permission": "Restore permitted repository access privately, then start a fresh collector run.",
    "repository-unavailable": "Confirm the canonical repository identity and availability after a state change, then start a fresh collector run.",
    "unknown": "Inspect the classified collector failure before choosing a permitted recovery.",
}
#: Recovery advice for each dirty-worktree or unresolved-index failure label.
WORKTREE_FAILURE_RECOVERY_ACTIONS = {
    "dirty-tracked-worktree-overlap-before-pr-checkout": (
        "Preserve or move the local changes that overlap checkout paths, then start a fresh collector run."
    ),
    "dirty-pr-worktree-before-pr-checkout": (
        "Preserve or move the local changes that overlap PR files, then start a fresh collector run."
    ),
    "dirty-pr-worktree-after-pr-checkout": (
        "Preserve or move the local changes that overlap PR files, then start a fresh collector run."
    ),
    "unresolved-index-before-pr-checkout": "Resolve the Git index entries, then start a fresh collector run.",
    "unresolved-index-after-pr-checkout": "Resolve the Git index entries, then start a fresh collector run.",
}


def _validate_sol_selections(payload: dict[str, Any], roles: set[str], *, label: str) -> dict[str, dict[str, str]]:
    """Validate immutable explicit-user-selection records for every routed Sol role."""
    selected_roles = SOL_ROLES & roles
    raw_selections = payload.get("sol_selection")
    if not selected_roles:
        if raw_selections not in (None, {}):
            raise SystemExit(f"{label}-unexpected")
        return {}
    if not isinstance(raw_selections, dict):
        missing = sorted(selected_roles)[0]
        raise SystemExit(f"{label}-missing:{missing}")
    if set(raw_selections) != selected_roles:
        missing = sorted(selected_roles - set(raw_selections))
        if missing:
            raise SystemExit(f"{label}-missing:{missing[0]}")
        raise SystemExit(f"{label}-unexpected")
    selections: dict[str, dict[str, str]] = {}
    for role in sorted(selected_roles):
        selection = raw_selections.get(role)
        if (
            not isinstance(selection, dict)
            or selection.get("source") != "explicit-user-selection"
            or not isinstance(selection.get("parent_event_id"), str)
            or not selection["parent_event_id"].strip()
            or not isinstance(selection.get("selection_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", selection["selection_sha256"]) is None
            or set(selection) != {"source", "parent_event_id", "selection_sha256"}
        ):
            raise SystemExit(f"{label}-invalid:{role}")
        selections[role] = selection
    return selections


#: Maps each conditionally routed specialist role to the routing signal that triggers it.
CONDITIONAL_SIGNALS = {
    "solution-architect": "axis_solution_architect",
    "security-auditor": "axis_security_auditor",
    "data-steward": "axis_data_steward",
    "cicd-steward": "axis_cicd_steward",
    "linting-expert": "axis_linting_expert",
    "doc-scribe": "axis_doc_scribe",
    "oss-shepherd": "axis_oss_shepherd",
    "squeezer": "axis_squeezer",
    "scientist": "axis_scientist",
    "web-explorer": "axis_web_explorer",
}


def _load_json_list(path: Path) -> list[Any]:
    """Load one required JSON array artifact."""
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, list):
        raise SystemExit(f"expected JSON array: {path}")
    return payload


def _validate_routing(out_dir: Path, risk_tier: str) -> set[str]:
    """Derive triggered specialist roles from explicit review-risk signals."""
    routing = _load_json(out_dir / "review-routing.json")
    if routing.get("schema_version") != 1:
        raise SystemExit("review-routing-schema-version")
    if routing.get("risk_tier") != risk_tier:
        raise SystemExit("review-routing-risk-tier-mismatch")
    mechanical_tier, mechanical_evidence, mandatory_signals = derive_mechanical_risk(out_dir)
    tier_rank = {"TRIVIAL": 0, "LOCAL": 1, "BROAD": 2, "HIGH_RISK": 3}
    if tier_rank[risk_tier] < tier_rank[mechanical_tier]:
        raise SystemExit(f"review-routing-tier-underclassified:{mechanical_tier}:{risk_tier}")
    if routing.get("mechanical_risk_tier") != mechanical_tier:
        raise SystemExit("review-routing-mechanical-tier-mismatch")
    if routing.get("mechanical_risk_evidence") != mechanical_evidence:
        raise SystemExit("review-routing-mechanical-evidence-mismatch")
    signals = routing.get("signals")
    if not isinstance(signals, dict) or set(signals) != ROUTING_SIGNALS:
        raise SystemExit("review-routing-signal-set-mismatch")
    if not all(isinstance(value, bool) for value in signals.values()):
        raise SystemExit("review-routing-signals-not-boolean")
    signal_evidence = routing.get("signal_evidence")
    if not isinstance(signal_evidence, dict) or set(signal_evidence) != ROUTING_SIGNALS:
        raise SystemExit("review-routing-signal-evidence-set-mismatch")
    if not all(
        isinstance(value, list) and value and all(isinstance(item, str) and item.strip() for item in value)
        for value in signal_evidence.values()
    ):
        raise SystemExit("review-routing-signal-evidence-empty")
    missing_mandatory = sorted(signal for signal in mandatory_signals if not signals[signal])
    if missing_mandatory:
        raise SystemExit("review-routing-mechanical-signals-false:" + ",".join(missing_mandatory))

    triggered: set[str] = set()
    # Source paths select the primary code reviewer; test modules remain QA's review surface.
    for filename in ("files.txt", "untracked.txt"):
        changed_file = out_dir / filename
        if not changed_file.exists():
            continue
        for raw_path in changed_file.read_text(encoding="utf-8").splitlines():
            parts = raw_path.strip().replace("\\", "/").lower().split("/")
            name = parts[-1]
            if (
                name.endswith((".py", ".pyi"))
                and "tests" not in parts[:-1]
                and not name.startswith(("test_", "conftest."))
            ):
                triggered.add("sw-engineer")
                break
        if "sw-engineer" in triggered:
            break
    if risk_tier in INDEPENDENT_PASS_TIERS:
        triggered.update(REQUIRED_ROLES)
    if risk_tier in {"TRIVIAL", "LOCAL"} and any(
        signals[name] for name in ("behavior_change", "bug_fix", "test_or_error_path", "data_tensor_boundary")
    ):
        triggered.add("qa-specialist")
    if risk_tier in {"TRIVIAL", "LOCAL"} and any(
        signals[name]
        for name in (
            "high_candidate",
            "unresolved_material_assumption",
            "material_no_finding",
            "explicit_adversarial",
        )
    ):
        triggered.add("challenger")
    triggered.update(role for role, signal in CONDITIONAL_SIGNALS.items() if signals[signal])

    _validate_sol_selections(routing, triggered, label="review-routing-sol-selection")

    declared = routing.get("triggered_roles")
    if not isinstance(declared, list) or declared != sorted(triggered):
        raise SystemExit("review-routing-triggered-role-mismatch")
    reasons = routing.get("trigger_reasons")
    if not isinstance(reasons, dict) or set(reasons) != triggered:
        raise SystemExit("review-routing-trigger-reason-mismatch")
    if not all(
        isinstance(value, list) and value and all(isinstance(item, str) and item.strip() for item in value)
        for value in reasons.values()
    ):
        raise SystemExit("review-routing-trigger-reason-values-invalid")
    return triggered


def _require_notes_sections(notes_path: Path) -> None:
    text = notes_path.read_text(encoding="utf-8")
    missing = []
    for section in REQUIRED_SECTIONS:
        if f"## {section}" not in text and f"# {section}" not in text:
            missing.append(section)
    if missing:
        raise SystemExit("missing-review-note-sections:" + ",".join(missing))


def _review_finding_identities(metadata: dict[str, Any], result: dict[str, Any]) -> set[str] | None:
    """Validate schema-v2 finding records and return their stable identities.

    Schema-v1 results retain their historical severity-count-only shape and are exempt from this check entirely.
    Schema-v2 assessed results must declare ``finding_records_version=1`` and supply the complete canonical records that
    make those counts actionable; omitting the marker no longer falls back to the bare id/severity shape for a new
    candidate.
    """
    schema_version = result.get("schema_version", 1)
    if schema_version == 1:
        return None
    if schema_version not in {2, 3}:
        raise SystemExit("unsupported-result-schema-version")

    records = metadata.get("review_findings")
    records_version = metadata.get("finding_records_version")
    if records_version is None:
        raise SystemExit("review-finding-records-version-missing")
    if type(records_version) is not int or records_version != 1:
        raise SystemExit("review-finding-records-version-invalid")
    if not isinstance(records, list):
        raise SystemExit("review-findings-records-missing")
    counts = {severity: 0 for severity in FINDING_SEVERITIES}
    identities: set[str] = set()
    for index, record in enumerate(records, start=1):
        detail_fields = {"title", "summary", "required_change", "evidence", "closure_evidence"}
        if not isinstance(record, dict) or set(record) not in (
            {"id", "severity"},
            {"id", "severity"} | detail_fields,
            {"id", "severity", "authors"} | detail_fields,
        ):
            raise SystemExit(f"review-finding-record-invalid:{index}")
        if "title" in record:
            for field in detail_fields - {"evidence"}:
                if not isinstance(record[field], str) or not record[field].strip():
                    raise SystemExit(f"review-finding-{field}-invalid:{index}")
            evidence = record["evidence"]
            if (
                not isinstance(evidence, list)
                or not evidence
                or any(not isinstance(entry, str) or not entry.strip() for entry in evidence)
            ):
                raise SystemExit(f"review-finding-evidence-invalid:{index}")
        identity = record["id"]
        if records_version == 1 and "title" not in record:
            raise SystemExit(f"review-finding-canonical-details-missing:{index}")
        severity = record["severity"]
        if not isinstance(identity, str) or not identity.strip():
            raise SystemExit(f"review-finding-id-invalid:{index}")
        if not isinstance(severity, str) or severity not in counts:
            raise SystemExit(f"review-finding-severity-invalid:{index}")
        if identity in identities:
            raise SystemExit(f"review-finding-id-duplicate:{identity}")
        identities.add(identity)
        counts[severity] += 1

    findings = result["findings"]
    for severity in FINDING_SEVERITIES:
        if counts[severity] != findings[severity]:
            raise SystemExit(f"review-findings-severity-count-mismatch:{severity}")
    return identities


def _operational_blocker_identities(metadata: dict[str, Any], finding_ids: set[str]) -> set[str]:
    """Validate optional non-finding action identities kept separate from review findings."""
    blockers = metadata.get("operational_blockers", [])
    if not isinstance(blockers, list):
        raise SystemExit("review-operational-blockers-invalid")
    identities: set[str] = set()
    for index, blocker in enumerate(blockers, start=1):
        if not isinstance(blocker, dict) or set(blocker) not in (
            {"id"},
            # An attributed review requires `authors` on every blocker, including a historical
            # ID-only one; without this shape such a blocker could satisfy neither rule.
            {"id", "authors"},
            {"id", "title", "required_change", "evidence"},
            {"id", "title", "required_change", "evidence", "authors"},
        ):
            raise SystemExit(f"review-operational-blocker-invalid:{index}")
        if "title" in blocker:
            for field in ("title", "required_change"):
                if not isinstance(blocker[field], str) or not blocker[field].strip():
                    raise SystemExit(f"review-operational-blocker-{field}-invalid:{index}")
            evidence = blocker["evidence"]
            if (
                not isinstance(evidence, list)
                or not evidence
                or any(not isinstance(entry, str) or not entry.strip() for entry in evidence)
            ):
                raise SystemExit(f"review-operational-blocker-evidence-invalid:{index}")
        identity = blocker["id"]
        if not isinstance(identity, str) or not identity.strip():
            raise SystemExit(f"review-operational-blocker-id-invalid:{index}")
        if identity in finding_ids or identity in identities:
            raise SystemExit(f"review-operational-blocker-id-duplicate:{identity}")
        identities.add(identity)
    return identities


def _readable_review_role(role_id: str) -> str:
    """Name a manifest role as it appears in a reviewer assessment."""
    parts = role_id.split("-")
    first = {"qa": "QA", "oss": "OSS", "cicd": "CICD", "sw": "Software"}.get(parts[0], parts[0].capitalize())
    return " ".join([first, *parts[1:]])


def _validate_reviewer_assessments(
    out_dir: Path,
    metadata: dict[str, Any],
    passes_by_role: dict[str, dict[str, Any]],
    *,
    batch_response: bool = False,
) -> None:
    """Bind each supplied assessment to a validated reviewer and retained output."""
    assessments = metadata.get("reviewer_assessments")
    if not isinstance(assessments, list) or not assessments:
        raise SystemExit("review-assessments-invalid")
    if "sw-engineer" in passes_by_role:
        primary = "Software engineer"
        if passes_by_role["sw-engineer"]["mode"] == "substituted":
            primary += " (parent substitute)"
        if not isinstance(assessments[0], dict) or assessments[0].get("role", "").casefold() != primary.casefold():
            raise SystemExit("review-primary-software-engineer-order")
    expected = {
        (_readable_review_role(role) + (" (parent substitute)" if item["mode"] == "substituted" else "")).casefold(): (
            role,
            item,
        )
        for role, item in passes_by_role.items()
    }
    specialist_outputs = {_resolve_path(out_dir, item["output_path"]) for item in passes_by_role.values()}
    seen: set[str] = set()
    for assessment in assessments:
        if not isinstance(assessment, dict) or not isinstance(assessment.get("role"), str):
            raise SystemExit("review-assessment-invalid")
        label = assessment["role"].casefold()
        if label in seen:
            raise SystemExit("review-assessment-role-duplicate")
        seen.add(label)
        if label not in expected and label != "main reviewer":
            raise SystemExit(f"review-assessment-role-unbound:{assessment['role']}")
        pointer = assessment.get("evidence")
        if not isinstance(pointer, str):
            raise SystemExit(f"review-assessment-evidence-invalid:{assessment['role']}")
        locator = re.search(r"(?::|#L)[1-9][0-9]*$", pointer)
        try:
            evidence_path = _resolve_path(out_dir, pointer[: locator.start()] if locator else pointer)
        except SystemExit:
            raise SystemExit(f"review-assessment-evidence-invalid:{assessment['role']}") from None
        if not evidence_path.is_file():
            raise SystemExit(f"review-assessment-evidence-invalid:{assessment['role']}")
        if label in expected:
            role, item = expected[label]
            if evidence_path != _resolve_path(out_dir, item["output_path"]):
                raise SystemExit(f"review-assessment-evidence-mismatch:{role}")
            local_reviewer_wave = item["mode"] == "app-server"
        else:
            role = "main reviewer"
            if evidence_path in specialist_outputs:
                raise SystemExit("review-assessment-main-evidence-reused")
            if evidence_path != _resolve_path(out_dir, "review-notes.md"):
                raise SystemExit("review-assessment-main-evidence-mismatch")
            local_reviewer_wave = False
        rating = _retained_reviewer_rating(
            evidence_path,
            local_reviewer_wave=local_reviewer_wave,
            main=label == "main reviewer",
            role=role,
            batch_response=batch_response and label in expected and "reviewer_findings" in expected[label][1],
        )
        if rating != assessment.get("rating"):
            raise SystemExit(f"review-assessment-rating-mismatch:{role}")
    for label, (role, _) in expected.items():
        if label not in seen:
            raise SystemExit(f"review-assessment-role-missing:{role}")


def _validate_review_decision(metadata: dict[str, Any], result: dict[str, Any]) -> None:
    """Bind an assessed review recommendation to finding severities and quality-gate status."""
    decision = metadata.get("review_decision")
    if not isinstance(decision, dict):
        raise SystemExit("result-missing-review-decision")
    recommendation = decision.get("recommendation")
    if recommendation not in VALID_RECOMMENDATIONS:
        raise SystemExit(f"invalid-review-recommendation:{recommendation!r}")
    for key in ("summary", "rationale"):
        value = decision.get(key)
        if not isinstance(value, str) or not value.strip():
            raise SystemExit(f"review-decision-missing-{key}")
    findings = result.get("findings")
    if (
        not isinstance(findings, dict)
        or set(findings) != set(FINDING_SEVERITIES)
        or any(not isinstance(findings[level], int) or findings[level] < 0 for level in FINDING_SEVERITIES)
    ):
        raise SystemExit("review-findings-invalid")
    finding_ids = _review_finding_identities(metadata, result)
    if finding_ids is not None:
        _operational_blocker_identities(metadata, finding_ids)
    assessments = metadata.get("reviewer_assessments")
    if assessments is not None:
        if not isinstance(assessments, list) or not assessments:
            raise SystemExit("review-assessments-invalid")
        roles: set[str] = set()
        for assessment in assessments:
            if (
                not isinstance(assessment, dict)
                or set(assessment) != {"role", "rating", "evidence"}
                or any(
                    not isinstance(assessment[key], str) or not assessment[key].strip() for key in ("role", "evidence")
                )
                or type(assessment["rating"]) is not int
                or assessment["rating"] not in range(1, 6)
                or assessment["role"] in roles
            ):
                raise SystemExit("review-assessment-invalid")
            roles.add(assessment["role"])
        passes = metadata.get("specialist_passes")
        if isinstance(passes, list):
            normalized_roles = {role.casefold() for role in roles}
            expected_substitutes: set[str] = set()
            for item in passes:
                if not isinstance(item, dict) or item.get("mode") != "substituted":
                    continue
                role_id = item.get("role")
                if not isinstance(role_id, str) or role_id not in ALL_MANIFEST_ROLES:
                    raise SystemExit("review-substitute-role-invalid")
                label = f"{_readable_review_role(role_id)} (parent substitute)"
                expected_substitutes.add(label.casefold())
                if label.casefold() not in normalized_roles:
                    if not any(role.endswith(" (parent substitute)") for role in roles):
                        raise SystemExit("review-substitute-attribution-missing")
                    raise SystemExit(f"review-substitute-role-mismatch:{role_id}")
            disclosed = {role.casefold() for role in roles if role.endswith(" (parent substitute)")}
            if disclosed != expected_substitutes:
                raise SystemExit("review-substitute-attribution-unbound")
        for record in metadata.get("review_findings", []) + metadata.get("operational_blockers", []):
            authors = record.get("authors")
            if (
                not isinstance(authors, list)
                or not authors
                or any(not isinstance(author, str) or author not in roles for author in authors)
                or len(set(authors)) != len(authors)
            ):
                raise SystemExit("review-finding-authors-invalid")
    if recommendation == "accept-as-is" and sum(findings.values()) != 0:
        raise SystemExit("review-accept-with-findings")
    if recommendation == "minor-changes" and (findings["critical"] or findings["high"]):
        raise SystemExit("review-minor-with-blocking-findings")
    if recommendation in {"accept-as-is", "minor-changes"} and (
        result.get("status") != "pass" or result.get("checks_failed")
    ):
        raise SystemExit("review-approval-with-failed-gates")


def _table_cells(line: str) -> list[str] | None:
    """Return one complete Markdown table row, preserving its cell content."""
    normalized = line.strip()
    if not normalized.startswith("|") or not normalized.endswith("|"):
        return None
    return [cell.replace(r"\|", "|").strip() for cell in re.split(r"(?<!\\)\|", normalized[1:-1])]


def _action_table_rows(notes_text: str) -> list[list[str]]:
    """Extract the canonical review findings and merge blocks table rows."""
    section = re.search(
        rf"^## {re.escape(ACTION_TABLE_SECTION)}\s*$\n(?P<body>.*?)(?=^## |\Z)",
        notes_text,
        re.MULTILINE | re.DOTALL,
    )
    if section is None:
        raise SystemExit("review-missing-findings-action-table")
    rows = [_table_cells(line) for line in section.group("body").splitlines() if line.strip().startswith("|")]
    width = len(rows[0]) if rows and rows[0] is not None else 0
    if len(rows) < 3 or width not in {4, 5} or any(row is None or len(row) != width for row in rows):
        raise SystemExit("review-invalid-findings-action-table")
    return [row for row in rows if row is not None]


def _validate_action_table(notes_path: Path, result: dict[str, Any], metadata: dict[str, Any], scope: str) -> None:
    """Require actionable, evidence-backed rows for non-approval review outcomes."""
    decision = metadata["review_decision"]
    recommendation = decision["recommendation"]
    canonical_actions = metadata.get("finding_records_version") == 1 and bool(
        metadata.get("review_findings") or metadata.get("operational_blockers")
    )
    if not canonical_actions and (
        recommendation == "accept-as-is" or (scope != "pr" and recommendation != "needs-more-work")
    ):
        return

    rows = _action_table_rows(notes_path.read_text(encoding="utf-8"))
    attributed = metadata.get("reviewer_assessments") is not None
    if attributed and len(rows[0]) != 5:
        raise SystemExit("review-findings-action-table-authors-missing")
    # Without retained assessments no author cell can be bound to a record, so an Author column
    # here would render attribution that nothing backs.
    if not attributed and len(rows[0]) == 5:
        raise SystemExit("review-findings-action-table-authors-unbound")
    if attributed:
        expected_headers = (ACTION_TABLE_HEADERS[0], "Author", *ACTION_TABLE_HEADERS[1:])
        if tuple(rows[0]) != expected_headers:
            raise SystemExit("review-findings-action-table-header-mismatch")
        if not re.fullmatch(r":?-{3,}:?", rows[1][1]):
            raise SystemExit("review-findings-action-table-divider-invalid")
        records = {
            record["id"]: record
            for record in metadata.get("review_findings", []) + metadata.get("operational_blockers", [])
        }
        for row in rows[2:]:
            if row[1] != ", ".join(records.get(row[0], {}).get("authors", [])):
                raise SystemExit("review-findings-action-table-authors-mismatch")
        rows = [[row[0], *row[2:]] for row in rows]
    if tuple(rows[0]) != ACTION_TABLE_HEADERS:
        raise SystemExit("review-findings-action-table-header-mismatch")
    if not all(re.fullmatch(r":?-{3,}:?", cell) for cell in rows[1]):
        raise SystemExit("review-findings-action-table-divider-invalid")
    action_rows = rows[2:]
    if not action_rows:
        raise SystemExit("review-findings-action-table-empty")
    finding_ids = _review_finding_identities(metadata, result)
    blocker_ids = _operational_blocker_identities(metadata, finding_ids or set()) if finding_ids is not None else set()
    records_by_id = (
        {record["id"]: record for record in metadata["review_findings"] + metadata.get("operational_blockers", [])}
        if finding_ids is not None
        else {}
    )
    action_identities: set[str] = set()
    for index, row in enumerate(action_rows, start=1):
        if not all(row):
            raise SystemExit(f"review-findings-action-table-cell-empty:{index}")
        identity = row[0]
        if identity in action_identities:
            raise SystemExit(f"review-findings-action-table-identity-duplicate:{identity}")
        action_identities.add(identity)
        if row[3].casefold() == "implemented":
            raise SystemExit(f"review-findings-action-table-status-closed:{index}")
        if finding_ids is not None and identity not in (finding_ids | blocker_ids):
            raise SystemExit(f"review-findings-action-table-identity-unbound:{identity}")
        record = records_by_id.get(identity)
        if (
            record is not None
            and "required_change" in record
            and row[1:3]
            != [
                record["required_change"].replace("\r\n", "\n").replace("\n", "<br>"),
                "; ".join(record["evidence"]).replace("\r\n", "\n").replace("\n", "<br>"),
            ]
        ):
            raise SystemExit(f"review-findings-action-table-content-mismatch:{identity}")
    if finding_ids is not None:
        missing = sorted((finding_ids | blocker_ids) - action_identities)
        if missing:
            raise SystemExit("review-findings-action-table-identity-coverage-mismatch:" + ",".join(missing))
    findings = result.get("findings")
    if isinstance(findings, dict):
        reported_count = sum(value for value in findings.values() if isinstance(value, int) and value >= 0)
        if len(action_rows) < reported_count:
            raise SystemExit("review-findings-action-table-incomplete")


def _validate_unavailable_result(
    out_dir: Path, result: dict[str, Any], metadata: dict[str, Any], scope: str, *, require_deductions: bool = False
) -> None:
    """Validate terminal collection failure with complete historical or deduction text.

    Keep canonical gap identity and process evidence exact; admit the new accounting profile only when its closure and
    overlapping limit carry consistent deductions. Fresh candidates require that profile; canonical historical results
    remain readable.
    """
    if scope != "pr":
        raise SystemExit("unavailable-review-non-pr-scope")
    if result.get("status") != "fail":
        raise SystemExit("unavailable-review-status-must-fail")
    if "review_decision" in metadata:
        raise SystemExit("unavailable-review-must-not-have-decision")
    unexpected_result_keys = sorted(set(result) - UNAVAILABLE_RESULT_KEYS)
    if unexpected_result_keys:
        raise SystemExit("unavailable-review-unexpected-result-fields:" + ",".join(unexpected_result_keys))
    unexpected_metadata_keys = sorted(set(metadata) - UNAVAILABLE_METADATA_KEYS)
    if unexpected_metadata_keys:
        raise SystemExit("unavailable-review-unexpected-metadata-fields:" + ",".join(unexpected_metadata_keys))
    forbidden = sorted(
        path.name
        for path in out_dir.iterdir()
        if path.name in UNAVAILABLE_FORBIDDEN_ARTIFACTS or path.name.startswith("specialist-")
    )
    if forbidden:
        raise SystemExit("unavailable-review-has-source-evidence:" + ",".join(forbidden))
    target_path = out_dir / "pr-target.txt"
    target = target_path.read_text(encoding="utf-8").strip() if target_path.is_file() else ""
    if not target or any(character.isspace() for character in target):
        raise SystemExit("unavailable-review-missing-pr-target")
    checkout_state = _unavailable_checkout_state(out_dir)
    findings = result.get("findings")
    expected_finding_levels = {"critical", "high", "medium", "low"}
    if (
        not isinstance(findings, dict)
        or set(findings) != expected_finding_levels
        or any(findings[level] != 0 for level in expected_finding_levels)
    ):
        raise SystemExit("unavailable-review-must-not-have-findings")

    failure = metadata.get("collection_failure")
    if not isinstance(failure, dict):
        raise SystemExit("unavailable-review-missing-collection-failure")
    code = failure.get("code")
    artifact = failure.get("artifact")
    if (
        not isinstance(code, str)
        or not re.fullmatch(r"[a-z][a-z0-9-]*(?::[A-Za-z0-9._-]+){0,2}", code)
        or artifact != "pr-error.txt"
    ):
        raise SystemExit("unavailable-review-invalid-collection-failure")
    error_path = out_dir / artifact
    if not error_path.is_file() or error_path.read_text(encoding="utf-8").strip() != code:
        raise SystemExit("unavailable-review-failure-artifact-mismatch")

    notes_path = out_dir / "review-notes.md"
    notes = notes_path.read_text(encoding="utf-8")
    command_record = _unavailable_command_diagnostic(out_dir, code)
    command_reason = command_record[1] if command_record is not None else None
    recovery_action = _unavailable_recovery_action(code, checkout_state is not None, command_reason)
    if any(line.strip().startswith("|") for line in notes.splitlines()):
        raise SystemExit("unavailable-review-process-table-forbidden")
    expected_notes = (
        f"# {UNAVAILABLE_NOTE_LINES[0]}\n\n"
        f"{UNAVAILABLE_NOTE_LINES[1]}\n\n"
        f"{UNAVAILABLE_NOTE_LINES[2]}\n\n"
        f"Process diagnostic: `{code}`. This is a workflow/integration failure, not a PR finding or merge block.\n\n"
        f"Recovery: {recovery_action}\n\n"
        "Evidence: `pr-error.txt`."
    )
    if notes.strip() != expected_notes:
        raise SystemExit("unavailable-review-notes-must-be-operational-only")
    if metadata.get("confidence_gaps") != [UNAVAILABLE_CONFIDENCE_GAP]:
        raise SystemExit("unavailable-review-confidence-gaps-must-be-canonical")
    expected_closure_rationale = (
        "A local checkout command may have changed state, but no verified source bundle was produced."
        if checkout_state is not None
        else "Core source verification did not complete; retained collection artifacts may be partial and were not assessed."
    )
    expected_closure = {
        "gap": UNAVAILABLE_CONFIDENCE_GAP,
        "status": "unresolved",
        "rationale": expected_closure_rationale,
    }
    deduction_closure = expected_closure | {"rationale": f"(-0.10) {expected_closure_rationale}"}
    closures = metadata.get("confidence_gap_closures")
    if closures == [expected_closure]:
        has_deductions = False
    elif closures == [deduction_closure]:
        has_deductions = True
    else:
        raise SystemExit("unavailable-review-confidence-closures-must-be-canonical")
    if require_deductions and not has_deductions:
        raise SystemExit("unavailable-review-candidate-confidence-deductions-required")
    expected_recovery = {
        "initial_confidence": 0.9,
        "final_confidence": 0.9,
        "status": "fair",
        "evidence": [
            "The classified collection failure and conservative checkout-state evidence were retained."
            if checkout_state is not None
            else "The classified collection failure and any current-attempt collector artifacts were retained."
        ],
        "recovery_actions": ["Stopped before source review."],
        "remaining_limits": [
            "PR correctness was not assessed; inspect local checkout state before retrying."
            if checkout_state is not None
            else "PR correctness was not assessed."
        ],
    }
    # Select the complete text profile together so partial migrations cannot hide a shortfall.
    if has_deductions:
        expected_recovery["remaining_limits"] = [
            f"(-0.00) {expected_recovery['remaining_limits'][0]} No additional deduction; "
            "the canonical source-verification gap accounts for this limitation."
        ]
    if metadata.get("confidence_recovery") != expected_recovery:
        raise SystemExit("unavailable-review-confidence-recovery-must-be-canonical")
    _validate_unavailable_gates(out_dir, result)
    _validate_unavailable_final_handoff(out_dir, metadata)


def _validate_unavailable_gates(out_dir: Path, result: dict[str, Any]) -> None:
    """Require explicit skipped PR gates for new terminal unavailable results."""
    if result.get("schema_version") not in {2, 3}:
        return
    gates_path = out_dir / "gates.json"
    if not gates_path.is_file():
        raise SystemExit("unavailable-review-gates-must-be-not-applicable")
    gates = _load_json(gates_path)
    checks = gates.get("checks")
    if (
        gates.get("status") != "pass"
        or gates.get("checks_failed") != []
        or not isinstance(checks, list)
        or tuple(check.get("id") if isinstance(check, dict) else None for check in checks) != UNAVAILABLE_GATE_IDS
        or any(
            not isinstance(check, dict)
            or check.get("status") != "not-applicable"
            or check.get("exit_code") != 0
            or not isinstance(check.get("reason"), str)
            or not check["reason"].strip()
            for check in checks
        )
    ):
        raise SystemExit("unavailable-review-gates-must-be-not-applicable")


def _validate_unavailable_final_handoff(out_dir: Path, metadata: dict[str, Any]) -> None:
    """Bind versioned unavailable-review explanations to classified, non-secret local diagnostics."""
    binding = metadata.get("final_handoff")
    if not isinstance(binding, dict) or not isinstance(binding.get("handoff_path"), str):
        return
    handoff_path = _resolve_path(out_dir, binding["handoff_path"])
    if not handoff_path.is_file():
        return
    handoff = _load_json(handoff_path)
    if handoff.get("presentation_version") not in {2, 3}:
        return
    failure = metadata["collection_failure"]
    code = failure["code"]
    if handoff.get("branch") != "unavailable" or handoff.get("tables") != []:
        raise SystemExit("unavailable-review-final-handoff-branch-mismatch")
    outcome = handoff.get("outcome")
    if not isinstance(outcome, dict) or outcome.get("title") != "PR Review Availability":
        raise SystemExit("unavailable-review-final-handoff-outcome-invalid")
    artifacts = handoff.get("artifacts")
    if not isinstance(artifacts, list):
        raise SystemExit("unavailable-review-final-handoff-artifacts-invalid")
    artifact_paths = {
        _resolve_path(out_dir, artifact.get("path"))
        for artifact in artifacts
        if isinstance(artifact, dict) and isinstance(artifact.get("path"), str)
    }
    required_paths = {out_dir / "pr-error.txt"}
    command_record = _unavailable_command_diagnostic(out_dir, code)
    command_diagnostic = command_record[0] if command_record is not None else None
    command_reason = command_record[1] if command_record is not None else None
    if command_diagnostic is not None:
        required_paths.add(out_dir / "command-failure.json")
    checkout_diagnostic = _unavailable_checkout_diagnostic(out_dir)
    if checkout_diagnostic is not None:
        required_paths.add(out_dir / "checkout-state.json")
    preflight_diagnostic = _unavailable_preflight_diagnostic(out_dir)
    if preflight_diagnostic is not None:
        required_paths.add(out_dir / "worktree-preflight.json")
    if {path.resolve() for path in required_paths} - artifact_paths:
        raise SystemExit("unavailable-review-final-handoff-artifact-binding-mismatch")
    label = code.rsplit(":", maxsplit=1)[-1]
    human_summary = UNAVAILABLE_HUMAN_SUMMARIES.get(label, UNAVAILABLE_GENERIC_SUMMARY)
    expected_summary = f"{human_summary} Reason: `{code}`."
    if command_diagnostic is not None:
        expected_summary += f" {command_diagnostic}"
    if checkout_diagnostic is not None:
        expected_summary += f" {checkout_diagnostic}"
    if preflight_diagnostic is not None:
        expected_summary += f" {preflight_diagnostic}"
    if command_reason in {None, "unclassified"}:
        expected_summary += " The collector did not retain a more specific cause."
    if outcome.get("summary") != expected_summary:
        raise SystemExit("unavailable-review-final-handoff-summary-mismatch")
    remaining = handoff.get("remaining")
    if not isinstance(remaining, list) or len(remaining) != 1 or not isinstance(remaining[0], dict):
        raise SystemExit("unavailable-review-final-handoff-recovery-mismatch")
    recovery = remaining[0]
    row_id = recovery.get("row_id")
    next_action = recovery.get("next_action")
    resume_condition = "Resume only after a fresh collector run produces and validates the PR source bundle."
    if (
        not isinstance(row_id, str)
        or recovery.get("owner") != "code-review"
        or recovery.get("item") != f"PR collection stopped at `{code}`."
        or not isinstance(next_action, str)
        or f"`{label}`" not in next_action
        or resume_condition not in next_action
        or handoff.get("next_steps") != [row_id]
    ):
        raise SystemExit("unavailable-review-final-handoff-recovery-mismatch")


def _unavailable_recovery_action(code: str, checkout_started: bool, command_reason: str | None = None) -> str:
    """Return the canonical safe recovery for one classified collection failure."""
    label = code.rsplit(":", maxsplit=1)[-1]
    if label in FETCH_FAILURE_LABELS and command_reason in PR_HEAD_FETCH_RECOVERY_ACTIONS:
        recovery_action = PR_HEAD_FETCH_RECOVERY_ACTIONS[command_reason]
        return recovery_action + (CHECKOUT_STATE_RECOVERY_SUFFIX if checkout_started else "")
    if label in WORKTREE_FAILURE_RECOVERY_ACTIONS:
        recovery_action = WORKTREE_FAILURE_RECOVERY_ACTIONS[label]
        return recovery_action + (CHECKOUT_STATE_RECOVERY_SUFFIX if checkout_started else "")
    category = code.split(":", maxsplit=1)[0]
    action_key = (
        "network"
        if category == "github-network"
        else "retry"
        if category in {"github-rate-limit", "command-timeout"}
        else "auth"
        if category in {"github-auth", "github-permission"}
        else "install"
        if code == "missing-command:gh"
        else "identity"
        if category == "github-not-found"
        else "report"
    )
    recovery_action = UNAVAILABLE_RECOVERY_ACTIONS[action_key]
    return recovery_action + (CHECKOUT_STATE_RECOVERY_SUFFIX if checkout_started else "")


def _unavailable_command_diagnostic(out_dir: Path, code: str) -> tuple[str, str | None] | None:
    """Render only fixed-shape collector diagnostics that cannot contain command output or credentials."""
    path = out_dir / "command-failure.json"
    if not path.is_file():
        return None
    diagnostic = _load_json(path)
    allowed = {"exit_code", "failure_class", "failure_reason", "label"}
    if set(diagnostic) - allowed or not {"exit_code", "failure_class", "label"} <= set(diagnostic):
        raise SystemExit("unavailable-review-command-diagnostic-invalid")
    exit_code = diagnostic["exit_code"]
    failure_class = diagnostic["failure_class"]
    label = diagnostic["label"]
    reason = diagnostic.get("failure_reason")
    if (
        type(exit_code) is not int
        or not isinstance(failure_class, str)
        or not SAFE_DIAGNOSTIC_IDENTIFIER.fullmatch(failure_class)
        or not isinstance(label, str)
        or not SAFE_DIAGNOSTIC_IDENTIFIER.fullmatch(label)
        or (reason is not None and (not isinstance(reason, str) or not SAFE_DIAGNOSTIC_IDENTIFIER.fullmatch(reason)))
    ):
        raise SystemExit("unavailable-review-command-diagnostic-invalid")
    if label != code.rsplit(":", maxsplit=1)[-1]:
        raise SystemExit("unavailable-review-command-diagnostic-code-mismatch")
    if label in FETCH_FAILURE_LABELS and reason is not None and reason not in PR_HEAD_FETCH_FAILURE_REASONS:
        raise SystemExit("unavailable-review-command-diagnostic-invalid")
    reason_detail = f"; reason `{reason}`" if reason is not None else ""
    return f"Command diagnostic: `{label}` exited {exit_code} (`{failure_class}`{reason_detail}).", reason


def _unavailable_checkout_state(out_dir: Path) -> dict[str, object] | None:
    """Load one bounded collector checkout-state record while preserving legacy evidence."""
    path = out_dir / "checkout-state.json"
    if not path.is_file():
        return None
    state = _load_json(path)
    status = state.get("status")
    if state.get("local_state") != "changed-or-unknown":
        raise SystemExit("unavailable-review-invalid-checkout-state")
    if status == "checkout-command-started":
        if set(state) != {"status", "local_state"}:
            raise SystemExit("unavailable-review-invalid-checkout-state")
        return state
    if status == "checkout-command-succeeded-unverified":
        expected_fields = {"status", "local_state", "gh_checkout_failure"}
        if set(state) == {"status", "local_state"}:
            return state
        if set(state) != expected_fields:
            raise SystemExit("unavailable-review-invalid-checkout-state")
        failure = state["gh_checkout_failure"]
        if failure is not None:
            _validate_unavailable_gh_checkout_failure(failure)
        return state
    if status == "gh-checkout-failed-recovery-assessment-started":
        if set(state) != {"status", "local_state", "gh_checkout_failure"}:
            raise SystemExit("unavailable-review-invalid-checkout-state")
        _validate_unavailable_gh_checkout_failure(state["gh_checkout_failure"])
        return state
    raise SystemExit("unavailable-review-invalid-checkout-state")


def _validate_unavailable_gh_checkout_failure(failure: object) -> None:
    """Require credential-opaque fields from a failed local ``gh pr checkout`` command."""
    if not isinstance(failure, dict) or set(failure) != {"command", "code", "diagnostics"}:
        raise SystemExit("unavailable-review-invalid-checkout-state")
    command = failure["command"]
    code = failure["code"]
    diagnostics = failure["diagnostics"]
    if (
        not isinstance(command, str)
        or not SAFE_GH_CHECKOUT_COMMAND.fullmatch(command)
        or not isinstance(code, str)
        or not re.fullmatch(r"[a-z][a-z0-9-]*(?::[A-Za-z0-9._-]+){1,2}", code)
    ):
        raise SystemExit("unavailable-review-invalid-checkout-state")
    if diagnostics is None:
        return
    allowed_fields = {"exit_code", "failure_class", "failure_reason", "label"}
    if not isinstance(diagnostics, dict) or not {"failure_class", "label"} <= set(diagnostics):
        raise SystemExit("unavailable-review-invalid-checkout-state")
    if set(diagnostics) - allowed_fields:
        raise SystemExit("unavailable-review-invalid-checkout-state")
    exit_code = diagnostics.get("exit_code")
    failure_class = diagnostics["failure_class"]
    failure_reason = diagnostics.get("failure_reason")
    label = diagnostics["label"]
    if (
        (exit_code is not None and type(exit_code) is not int)
        or not isinstance(failure_class, str)
        or not SAFE_DIAGNOSTIC_IDENTIFIER.fullmatch(failure_class)
        or (
            failure_reason is not None
            and (not isinstance(failure_reason, str) or not SAFE_DIAGNOSTIC_IDENTIFIER.fullmatch(failure_reason))
        )
        or label != "local-pr-checkout"
    ):
        raise SystemExit("unavailable-review-invalid-checkout-state")


def _unavailable_checkout_diagnostic(out_dir: Path) -> str | None:
    """Render only the validated fixed checkout status, never command or diagnostic payloads."""
    state = _unavailable_checkout_state(out_dir)
    if state is None:
        return None
    return f"Checkout diagnostic: local worktree state is changed or unknown after `{state['status']}`."


def _unavailable_preflight_diagnostic(out_dir: Path) -> str | None:
    """Render only the collector's fixed worktree head identifiers when that preflight exists."""
    path = out_dir / "worktree-preflight.json"
    if not path.is_file():
        return None
    preflight = _load_json(path)
    path_keys = {
        "dirty_paths",
        "checkout_paths",
        "overlapping_paths",
        "pr_paths",
        "overlapping_pr_paths",
        "unmerged_paths",
    }
    expected_keys = path_keys | {"status", "current_head", "expected_head", "phase"}
    if "schema_version" in preflight:
        # Versioned records list only overlapping dirty paths plus a bounded sample and count the rest.
        expected_keys |= {"schema_version", "dirty_paths_omitted", "dirty_path_counts"}
    head_pattern = re.compile(r"[0-9a-f]{7,64}\Z")
    if (
        set(preflight) != expected_keys
        or not dirty_path_record_is_consistent(preflight)
        or preflight.get("status")
        not in {
            "already-at-pr-head",
            "blocked-overlapping-dirty-paths",
            "blocked-pr-dirty-paths",
            "blocked-unmerged-index",
            "clean",
            "safe-unrelated-dirty-paths",
        }
        or preflight.get("phase") not in {"before-checkout", "after-checkout"}
        or not isinstance(preflight.get("current_head"), str)
        or not head_pattern.fullmatch(preflight["current_head"])
        or not isinstance(preflight.get("expected_head"), str)
        or not head_pattern.fullmatch(preflight["expected_head"])
        or any(
            not isinstance(preflight.get(key), list) or not all(isinstance(item, str) for item in preflight[key])
            for key in path_keys
        )
    ):
        raise SystemExit("unavailable-review-worktree-preflight-invalid")
    dirty_paths = set(preflight["dirty_paths"])
    # Filesystem aliases can add collisions beyond lexical ancestry; do not re-read mutable caller state here.
    for overlaps_field, changed_field in (
        ("overlapping_paths", "checkout_paths"),
        ("overlapping_pr_paths", "pr_paths"),
    ):
        recorded = set(preflight[overlaps_field])
        lexical = {
            dirty
            for dirty in dirty_paths
            if any(
                dirty == changed or dirty.startswith(f"{changed}/") or changed.startswith(f"{dirty}/")
                for changed in preflight[changed_field]
            )
        }
        if not lexical <= recorded <= dirty_paths:
            raise SystemExit("unavailable-review-worktree-preflight-invalid")
    if preflight["unmerged_paths"]:
        expected_status = "blocked-unmerged-index"
    elif preflight["overlapping_pr_paths"]:
        expected_status = "blocked-pr-dirty-paths"
    elif preflight["overlapping_paths"]:
        expected_status = "blocked-overlapping-dirty-paths"
    elif dirty_paths:
        expected_status = "safe-unrelated-dirty-paths"
    elif preflight["current_head"] == preflight["expected_head"]:
        expected_status = "already-at-pr-head"
    else:
        expected_status = "clean"
    if preflight["status"] != expected_status:
        raise SystemExit("unavailable-review-worktree-preflight-invalid")
    return f"Worktree preflight: local head `{preflight['current_head']}`; expected PR head `{preflight['expected_head']}`."


def _validate_verified_pr_source(
    out_dir: Path, routing: dict[str, Any], target_branch: dict[str, Any], checkout: dict[str, Any]
) -> None:
    """Bind reviewed local source to immutable target and PR-head OIDs."""
    pr_payload = _load_json(out_dir / "pr.json")
    head_fetch = _load_json(out_dir / "pr-head-fetch.json")
    preflight = _load_json(out_dir / "worktree-preflight.json")
    head_oid = routing.get("head_oid")
    base_oid = routing.get("base_oid")
    recorded_oids = (
        pr_payload.get("baseRefOid"),
        pr_payload.get("headRefOid"),
        base_oid,
        head_oid,
        target_branch.get("remote_ref"),
        target_branch.get("local_head"),
        target_branch.get("expected_base_oid"),
        head_fetch.get("local_head"),
        head_fetch.get("expected_head_oid"),
        checkout.get("expected_head"),
        checkout.get("local_head"),
        checkout.get("diff_base_oid"),
        checkout.get("diff_head_oid"),
        preflight.get("current_head"),
        preflight.get("expected_head"),
    )
    if (
        any(not isinstance(oid, str) or re.fullmatch(r"[0-9a-f]{40}", oid) is None for oid in recorded_oids)
        or pr_payload.get("baseRefOid") != base_oid
        or pr_payload.get("headRefOid") != head_oid
        or target_branch.get("remote_ref") != target_branch.get("local_head")
        or target_branch.get("expected_base_oid") != base_oid
        or head_fetch.get("remote_ref") != "FETCH_HEAD"
        or head_fetch.get("local_head") != head_fetch.get("expected_head_oid")
        or head_fetch.get("expected_head_oid") != head_oid
        or head_fetch.get("head_matches_pr_metadata") is not True
        or checkout.get("expected_head") != head_oid
        or checkout.get("local_head") != head_oid
    ):
        raise SystemExit("pr-source-oid-provenance-invalid")
    path_fields = (
        "dirty_paths",
        "unmerged_paths",
        "pr_paths",
        "checkout_paths",
        "overlapping_paths",
        "overlapping_pr_paths",
    )
    if (
        preflight.get("phase") != "after-checkout"
        or preflight.get("status") not in {"clean", "already-at-pr-head", "safe-unrelated-dirty-paths"}
        or preflight.get("expected_head") != head_oid
        or preflight.get("current_head") != head_oid
        or any(
            not isinstance(preflight.get(field), list) or not all(isinstance(path, str) for path in preflight[field])
            for field in path_fields
        )
        or preflight.get("unmerged_paths")
        or set(preflight["overlapping_paths"])
        != set(preflight["dirty_paths"]).intersection(preflight["checkout_paths"])
        or set(preflight["overlapping_pr_paths"]) != set(preflight["dirty_paths"]).intersection(preflight["pr_paths"])
        or preflight["overlapping_paths"]
        or preflight["overlapping_pr_paths"]
        or (preflight.get("status") in {"clean", "already-at-pr-head"} and preflight.get("dirty_paths"))
        or (preflight.get("status") == "safe-unrelated-dirty-paths" and not preflight.get("dirty_paths"))
        or not dirty_path_record_is_consistent(preflight)
    ):
        raise SystemExit("pr-source-worktree-preflight-invalid")
    if routing.get("checkout_method") == "git-detached-review-worktree":
        _validate_review_worktree_source(out_dir, base_oid, head_oid, checkout, preflight)


def _validate_review_worktree_source(
    out_dir: Path, base_oid: str, head_oid: str, checkout: dict[str, Any], preflight: dict[str, Any]
) -> None:
    """Bind isolated checkout and gate receipts to the exact reviewed PR worktree."""
    collection_run_dir = checkout.get("collection_run_dir")
    if not isinstance(collection_run_dir, str) or not Path(collection_run_dir).is_absolute():
        raise SystemExit("pr-source-review-worktree-invalid")
    original_run = Path(collection_run_dir)
    source = checkout.get("source_worktree")
    worktree = checkout.get("worktree")
    location = checkout.get("worktree_location")
    if not isinstance(worktree, str) or not Path(worktree).is_absolute() or not isinstance(source, str):
        raise SystemExit("pr-source-review-worktree-invalid")
    if location == "report-adjacent":
        expected_worktree = original_run.with_name(f"{original_run.name}-review-worktree")
        location_valid = worktree == expected_worktree.as_posix()
    elif location == "temporary-fallback":
        digest = hashlib.sha256(collection_run_dir.encode("utf-8")).hexdigest()[:16]
        expected_name = f"codex-pr-review-{digest}-review-worktree"
        location_valid = (
            Path(worktree).name == expected_name
            and Path(worktree).resolve().as_posix() == worktree
            and not Path(worktree).is_relative_to(Path(source).resolve())
        )
    else:
        location_valid = False
    checkout_state = _load_json(out_dir / "checkout-state.json")
    source_context = _load_json(out_dir / "source-worktree-context.json")
    if (
        not location_valid
        or original_run != original_run.resolve()
        or not Path(source).is_absolute()
        or source != Path(source).resolve().as_posix()
        or Path(source).resolve().as_posix() == worktree
        or checkout.get("local_branch") != ""
        or checkout.get("worktree_lifecycle") != "preserved-for-review"
        or preflight.get("worktree") != worktree
        or checkout_state.get("status") != "checkout-verified"
        or checkout_state.get("local_state") != "isolated-exact-pr-head; main-worktree-unchanged"
        or checkout_state.get("local_head") != head_oid
        or checkout_state.get("worktree") != worktree
        or source_context.get("phase") != "source-worktree-context"
        or source_context.get("expected_head") != head_oid
        or not isinstance(source_context.get("current_head"), str)
        or re.fullmatch(r"[0-9a-f]{40}", source_context["current_head"]) is None
    ):
        raise SystemExit("pr-source-review-worktree-invalid")

    gates = _load_json(out_dir / "gates.json")
    gate_source = gates.get("source")
    checks = gates.get("checks")
    if (
        not isinstance(gate_source, dict)
        or gate_source.get("worktree") != worktree
        or gate_source.get("expected_head") != head_oid
        or not isinstance(checks, list)
    ):
        raise SystemExit("pr-source-review-gates-worktree-mismatch")
    for check in checks:
        if not isinstance(check, dict):
            raise SystemExit("pr-source-review-gates-worktree-mismatch")
        if check.get("status") in {"not-applicable", "missing-command"}:
            continue
        receipt = check.get("source")
        if (
            not isinstance(receipt, dict)
            or receipt.get("worktree") != worktree
            or receipt.get("expected_head") != head_oid
        ):
            raise SystemExit("pr-source-review-gates-worktree-mismatch")
        if check.get("status") == "pass" and any(
            not isinstance(receipt.get(phase), dict)
            or receipt[phase].get("head") != head_oid
            or receipt[phase].get("status") != ""
            or receipt[phase].get("error") is not None
            for phase in ("before", "after")
        ):
            raise SystemExit("pr-source-review-gates-source-invalid")
    _validate_pr_review_gate_command(out_dir, checks, base_oid, head_oid)
    _validate_pr_tests_import_proof(checks, worktree)


def _validate_pr_review_gate_command(out_dir: Path, checks: list[dict[str, Any]], base_oid: str, head_oid: str) -> None:
    """Require a passing review gate to inspect the committed PR diff."""
    review_checks = [check for check in checks if check.get("id") == "review"]
    if len(review_checks) != 1:
        raise SystemExit("pr-source-review-gate-command-invalid")
    review_check = review_checks[0]
    if review_check.get("status") == "not-applicable":
        raise SystemExit("pr-source-review-gate-command-invalid")
    command_path = _resolve_path(out_dir, review_check.get("command_path"))
    if not command_path.is_file():
        raise SystemExit("pr-source-review-gate-command-invalid")
    expected_command = f"git diff --check {base_oid}...{head_oid}"
    if command_path.read_text(encoding="utf-8").strip() != expected_command:
        raise SystemExit("pr-source-review-gate-command-invalid")


def _validate_pr_tests_import_proof(
    checks: list[dict[str, Any]], worktree: str, local_snapshot: dict[str, Any] | None = None
) -> None:
    """Require actual test origins from clean tracked source or explicitly admitted local snapshot bytes."""
    tests_checks = [check for check in checks if check.get("id") == "tests"]
    if len(tests_checks) != 1:
        raise SystemExit("pr-source-review-tests-import-proof-invalid")
    tests_check = tests_checks[0]
    if tests_check.get("status") != "pass":
        return
    proof = tests_check.get("python_imports")
    if (
        not isinstance(proof, dict)
        or proof.get("mode") != "in-process-pytest"
        or proof.get("status") != "pass"
        or proof.get("worktree") != worktree
        or any(
            not isinstance(proof.get(field), str) or not Path(proof[field]).is_absolute()
            for field in ("invoked_interpreter", "runtime_interpreter", "sys_prefix")
        )
        or not isinstance(proof.get("modules"), dict)
        or not proof["modules"]
        or not isinstance(proof.get("tests"), dict)
        or not proof["tests"]
    ):
        raise SystemExit("pr-source-review-tests-import-proof-invalid")
    source_root = Path(worktree).resolve()
    for name, module in proof["modules"].items():
        if (
            not isinstance(name, str)
            or not name.strip()
            or not isinstance(module, dict)
            or module.get("status") != "pass"
            or (local_snapshot is None and module.get("tracked") is not True)
            or module.get("reason") is not None
            or not isinstance(module.get("origin"), str)
            or not Path(module["origin"]).is_absolute()
            or not Path(module["origin"]).resolve().is_relative_to(source_root)
        ):
            raise SystemExit("pr-source-review-tests-import-proof-invalid")
    for name, test in proof["tests"].items():
        if (
            not isinstance(name, str)
            or not Path(name).is_absolute()
            or not isinstance(test, dict)
            or test.get("status") != "pass"
            or (local_snapshot is None and test.get("tracked") is not True)
            or test.get("reason") is not None
            or test.get("origin") != name
            or not Path(name).resolve().is_relative_to(source_root)
        ):
            raise SystemExit("pr-source-review-tests-import-proof-invalid")
    if local_snapshot is not None:
        records = {record["path"]: record for record in local_snapshot["files"]}
        expected_transport = local_snapshot["transport"]
        workers = proof.get("workers")
        if (
            not isinstance(workers, dict)
            or proof.get("errors") != []
            or any(not isinstance(name, str) or not name for name in workers)
        ):
            raise SystemExit("local-source-tests-worker-envelope-invalid")
        processes = [proof, *workers.values()]
        for process in processes:
            if (
                not isinstance(process, dict)
                or process.get("local_source") != expected_transport
                or process.get("worktree") != worktree
                or not isinstance(process.get("modules"), dict)
                or set(process["modules"]) != set(proof["modules"])
                or not isinstance(process.get("tests"), dict)
                or not process["tests"]
                or any(
                    not isinstance(process.get(field), str) or not Path(process[field]).is_absolute()
                    for field in ("runtime_interpreter", "sys_prefix")
                )
            ):
                raise SystemExit("local-source-tests-worker-envelope-invalid")
            neutral = False
            for name, origin in process["modules"].items():
                # Only an exact unloaded-module record is neutral; selected tests always require byte membership.
                if (
                    isinstance(origin, dict)
                    and origin
                    == {"origin": None, "tracked": False, "status": "inconclusive", "reason": "module-not-imported"}
                    and origin.get("tracked") is False
                ):
                    neutral = True
                    continue
                if (
                    not isinstance(origin, dict)
                    or origin.get("status") != "pass"
                    or origin.get("reason") is not None
                    or not isinstance(origin.get("origin"), str)
                    or not Path(origin["origin"]).is_absolute()
                ):
                    raise SystemExit("local-source-tests-origin-invalid")
            expected_status = "inconclusive" if neutral else "pass"
            if process.get("status") != expected_status:
                raise SystemExit("local-source-tests-worker-status-invalid")
            for name, test in process["tests"].items():
                if (
                    not isinstance(name, str)
                    or not Path(name).is_absolute()
                    or not isinstance(test, dict)
                    or test.get("origin") != name
                    or test.get("status") != "pass"
                    or test.get("reason") is not None
                ):
                    raise SystemExit("local-source-tests-selected-test-invalid")
            loaded_modules = [
                origin for origin in process["modules"].values() if origin.get("status") != "inconclusive"
            ]
            for origin in [*loaded_modules, *process["tests"].values()]:
                path = Path(origin["origin"]).resolve()
                if not path.is_relative_to(source_root):
                    raise SystemExit("local-source-tests-origin-invalid")
                record = records.get(path.relative_to(source_root).as_posix())
                if (
                    record is None
                    or record["kind"] != "file"
                    or origin.get("status") != "pass"
                    or origin.get("reason") is not None
                    or origin.get("snapshot_member") != {key: record[key] for key in ("path", "kind", "sha256")}
                    or origin.get("tracked") is not inspect_collected_test(path, source_root, local_snapshot)["tracked"]
                    or _sha256(path) != record["sha256"]
                ):
                    raise SystemExit("local-source-tests-snapshot-member-invalid")
        if workers:
            combined = _aggregate_worker_proofs(workers, set(workers), list(proof["modules"]))
            if any(combined[key] != proof[key] for key in ("status", "modules", "tests", "errors")):
                raise SystemExit("local-source-tests-worker-union-invalid")


def _validate_closed_result(out_dir: Path, result: dict[str, Any], metadata: dict[str, Any], scope: str) -> None:
    """Validate a conclusive proposal-level PR close decision that precedes source review."""
    if scope != "pr":
        raise SystemExit("closed-review-non-pr-scope")
    if result.get("status") != "pass":
        raise SystemExit("closed-review-status-must-pass")
    unexpected_result_keys = sorted(set(result) - CLOSED_RESULT_KEYS)
    if unexpected_result_keys:
        raise SystemExit("closed-review-unexpected-result-fields:" + ",".join(unexpected_result_keys))
    unexpected_metadata_keys = sorted(set(metadata) - CLOSED_METADATA_KEYS)
    if unexpected_metadata_keys:
        raise SystemExit("closed-review-unexpected-metadata-fields:" + ",".join(unexpected_metadata_keys))

    findings = result.get("findings")
    finding_levels = {"critical", "high", "medium", "low"}
    if (
        not isinstance(findings, dict)
        or set(findings) != finding_levels
        or any(findings[level] != 0 for level in finding_levels)
    ):
        raise SystemExit("closed-review-must-not-have-findings")

    forbidden = sorted(
        path.name
        for path in out_dir.iterdir()
        if path.name in CLOSED_FORBIDDEN_ARTIFACTS or path.name.startswith("specialist-")
    )
    if forbidden:
        raise SystemExit("closed-review-has-detailed-review-artifacts:" + ",".join(forbidden))
    required_pr_artifacts = set(CLOSED_REQUIRED_PR_ARTIFACTS)
    if result.get("schema_version") in {2, 3}:
        required_pr_artifacts.update({"pr-head-fetch.json", "worktree-preflight.json"})
    for filename in sorted(required_pr_artifacts):
        if not (out_dir / filename).is_file():
            raise SystemExit(f"closed-review-missing-pr-artifact:{filename}")

    decision = metadata.get("close_decision")
    expected_decision_keys = {
        "schema_version",
        "code",
        "advisory_only",
        "head_sha",
        "summary",
        "rationale",
        "evidence",
        "counterevidence_checked",
    }
    if not isinstance(decision, dict) or set(decision) != expected_decision_keys:
        raise SystemExit("closed-review-invalid-decision-shape")
    if decision.get("schema_version") != 1 or decision.get("advisory_only") is not True:
        raise SystemExit("closed-review-invalid-decision-policy")
    code = decision.get("code")
    if code not in CLOSE_CODES:
        raise SystemExit(f"closed-review-invalid-code:{code!r}")
    head_sha = decision.get("head_sha")
    if not isinstance(head_sha, str) or re.fullmatch(r"[0-9a-f]{40}", head_sha) is None:
        raise SystemExit("closed-review-invalid-head-sha")
    for key in ("summary", "rationale"):
        value = decision.get(key)
        if not isinstance(value, str) or not value.strip():
            raise SystemExit(f"closed-review-missing-{key}")
    evidence = decision.get("evidence")
    if not isinstance(evidence, list) or len(evidence) < 2:
        raise SystemExit("closed-review-evidence-required")
    evidence_sources: set[str] = set()
    for index, item in enumerate(evidence):
        if not isinstance(item, dict) or set(item) != {"claim", "source"}:
            raise SystemExit(f"closed-review-invalid-evidence-shape:{index}")
        if not all(isinstance(item[key], str) and item[key].strip() for key in ("claim", "source")):
            raise SystemExit(f"closed-review-empty-evidence:{index}")
        evidence_sources.add(item["source"].strip())
    if len(evidence_sources) < 2:
        raise SystemExit("closed-review-distinct-evidence-required")
    counterevidence = decision.get("counterevidence_checked")
    if (
        not isinstance(counterevidence, list)
        or not counterevidence
        or not all(isinstance(item, str) and item.strip() for item in counterevidence)
    ):
        raise SystemExit("closed-review-counterevidence-required")

    pr_payload = _load_json(out_dir / "pr.json")
    routing = _load_json(out_dir / "pr-routing.json")
    target_branch = _load_json(out_dir / "target-branch.json")
    checkout = _load_json(out_dir / "local-checkout.json")
    if result.get("schema_version") in {2, 3}:
        _validate_verified_pr_source(out_dir, routing, target_branch, checkout)
    if pr_payload.get("state") != "OPEN" or routing.get("pr_state") != "OPEN":
        raise SystemExit("closed-review-pr-state-not-open")
    if not isinstance(pr_payload.get("body"), str):
        raise SystemExit("closed-review-pr-description-missing")
    if head_sha != pr_payload.get("headRefOid") or head_sha != routing.get("head_oid"):
        raise SystemExit("closed-review-head-sha-mismatch")
    base_oid = routing.get("base_oid")
    if base_oid != pr_payload.get("baseRefOid"):
        raise SystemExit("closed-review-base-sha-mismatch")
    if (
        target_branch.get("status") != "fetched"
        or target_branch.get("expected_base_oid") != base_oid
        or target_branch.get("expected_base_is_ancestor") is not True
    ):
        raise SystemExit("closed-review-target-branch-invalid")
    if (
        checkout.get("status") != "checked-out"
        or checkout.get("expected_head") != head_sha
        or checkout.get("local_head") != head_sha
        or checkout.get("head_matches_pr") is not True
        or checkout.get("diff_source") != "verified-local-checkout"
        or checkout.get("diff_base_oid") != base_oid
        or checkout.get("diff_head_oid") != head_sha
    ):
        raise SystemExit("closed-review-local-checkout-invalid")

    notes = (out_dir / "review-notes.md").read_text(encoding="utf-8")
    if any(line.strip().startswith("|") for line in notes.splitlines()):
        raise SystemExit("closed-review-table-forbidden")
    evidence_notes = "\n".join(f"- `{item['source']}`: {item['claim']}" for item in evidence)
    counterevidence_notes = "\n".join(f"- {item}" for item in counterevidence)
    expected_notes = (
        "# Review Decision: close\n\n"
        "Source findings: not assessed\n\n"
        "Detailed review: skipped\n\n"
        f"Close reason: `{code}`\n\n"
        f"Summary: {decision['summary']}\n\n"
        f"Rationale: {decision['rationale']}\n\n"
        f"Evidence:\n\n{evidence_notes}\n\n"
        f"Counterevidence checked:\n\n{counterevidence_notes}\n\n"
        "GitHub mutation: not performed."
    )
    if notes.strip() != expected_notes:
        raise SystemExit("closed-review-notes-must-be-close-only")
    confidence_gaps = metadata.get("confidence_gaps")
    if not isinstance(confidence_gaps, list) or CLOSED_CONFIDENCE_GAP not in confidence_gaps:
        raise SystemExit("closed-review-confidence-gap-missing")
    confidence = result.get("confidence")
    if not isinstance(confidence, int | float) or float(confidence) < 0.9:
        raise SystemExit("closed-review-confidence-below-threshold")
    online_summary = _load_json(out_dir / "online-review-summary.json")
    if online_summary.get("pr_metadata_transport") == "public-https-fallback":
        raise SystemExit("closed-review-public-fallback-insufficient")


def _validate_confidence_gaps(result: dict[str, Any], metadata: dict[str, Any]) -> None:
    """Validate confidence gap metadata whenever review confidence is reported."""
    confidence_gaps = metadata.get("confidence_gaps")
    if not isinstance(confidence_gaps, list) or not all(isinstance(item, str) for item in confidence_gaps):
        raise SystemExit("review-invalid-confidence-gaps")
    if float(result["confidence"]) < 1.0 and not any(item.strip() for item in confidence_gaps):
        raise SystemExit("review-confidence-gaps-required")
    _validate_confidence_gap_closures(metadata, confidence_gaps)


def _validate_pr_fallback_confidence(
    online_summary: dict[str, Any], result: dict[str, Any], metadata: dict[str, Any]
) -> str | None:
    """Require explicit evidence limits and cautious confidence after public PR fallback."""
    if online_summary.get("pr_metadata_transport") != "public-https-fallback":
        return None
    unavailable = online_summary.get("unavailable_evidence")
    if online_summary.get("limited_data") is not True or not isinstance(unavailable, list) or not unavailable:
        raise SystemExit("pr-public-fallback-limitation-missing")
    if not all(isinstance(item, str) and item for item in unavailable):
        raise SystemExit("pr-public-fallback-limitation-missing")
    if unavailable != sorted(unavailable):
        raise SystemExit("pr-public-fallback-evidence-not-sorted")
    confidence_gap = f"Public HTTPS PR metadata fallback omitted evidence: {', '.join(unavailable)}."
    confidence_gaps = metadata.get("confidence_gaps")
    if not isinstance(confidence_gaps, list) or confidence_gap not in confidence_gaps:
        raise SystemExit("pr-public-fallback-confidence-gap-missing")
    if float(result["confidence"]) > PR_PUBLIC_FALLBACK_MAX_CONFIDENCE:
        raise SystemExit("pr-public-fallback-confidence-cap-exceeded")
    return confidence_gap


def _validate_confidence_gap_closures(metadata: dict[str, Any], confidence_gaps: list[str]) -> None:
    """Validate that every review confidence gap has closure evidence or carry-forward state."""
    active_gaps = [gap.strip() for gap in confidence_gaps]
    if any(not gap for gap in active_gaps):
        raise SystemExit("review-invalid-confidence-gap")
    if len(active_gaps) != len(set(active_gaps)):
        raise SystemExit("review-duplicate-confidence-gap")
    closures = metadata.get("confidence_gap_closures")
    if not active_gaps:
        if closures not in (None, []):
            raise SystemExit("review-confidence-gap-closure-undeclared")
        return

    if not isinstance(closures, list):
        raise SystemExit("review-missing-confidence-gap-closures")

    closed_gaps: set[str] = set()
    for index, closure in enumerate(closures):
        if not isinstance(closure, dict):
            raise SystemExit(f"review-confidence-gap-closure-not-object:{index}")
        gap = closure.get("gap")
        if not isinstance(gap, str) or not gap.strip():
            raise SystemExit(f"review-confidence-gap-closure-missing-gap:{index}")
        status = closure.get("status")
        if status not in {"closed", "unresolved", "deferred"}:
            raise SystemExit(f"review-confidence-gap-closure-invalid-status:{index}")
        evidence = closure.get("evidence") or closure.get("evidence_path")
        rationale = closure.get("rationale")
        if status == "closed" and not (isinstance(evidence, str) and evidence.strip()):
            raise SystemExit(f"review-confidence-gap-closure-missing-evidence:{index}")
        if status in {"unresolved", "deferred"} and not (isinstance(rationale, str) and rationale.strip()):
            raise SystemExit(f"review-confidence-gap-closure-missing-rationale:{index}")
        normalized_gap = gap.strip()
        if normalized_gap not in active_gaps:
            raise SystemExit(f"review-confidence-gap-closure-undeclared:{index}")
        if normalized_gap in closed_gaps:
            raise SystemExit(f"review-confidence-gap-closure-duplicate:{normalized_gap}")
        closed_gaps.add(normalized_gap)

    missing = sorted(set(active_gaps) - closed_gaps)
    if missing:
        raise SystemExit(f"review-confidence-gap-closure-missing:{','.join(missing)}")


def _require_non_empty_string_list(payload: dict[str, Any], key: str, context: str) -> list[str]:
    """Return a required non-empty list of non-blank strings from a metadata object."""
    value = payload.get(key)
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item.strip() for item in value):
        raise SystemExit(f"{context}-invalid-{key}")
    return value


def _validate_confidence_recovery(result: dict[str, Any], metadata: dict[str, Any]) -> None:
    """Validate evidence-backed confidence recovery metadata for review artifacts."""
    confidence = result.get("confidence")
    if not isinstance(confidence, int | float) or not 0.0 <= float(confidence) <= 1.0:
        raise SystemExit("invalid-confidence")
    checks_failed = result.get("checks_failed")
    if not isinstance(checks_failed, list):
        raise SystemExit("invalid-checks-failed")

    recovery = metadata.get("confidence_recovery")
    if not isinstance(recovery, dict):
        raise SystemExit("review-missing-confidence-recovery-metadata")

    initial = recovery.get("initial_confidence")
    final = recovery.get("final_confidence")
    if not isinstance(initial, int | float) or not 0.0 <= float(initial) <= 1.0:
        raise SystemExit("review-invalid-initial-confidence")
    if not isinstance(final, int | float) or not 0.0 <= float(final) <= 1.0:
        raise SystemExit("review-invalid-final-confidence")
    if abs(float(final) - float(confidence)) > 0.001:
        raise SystemExit("review-confidence-recovery-final-mismatch")

    status = recovery.get("status")
    if status not in {"fair", "cautious-low", "very-questionable", "not-acceptable-failed"}:
        raise SystemExit("review-invalid-confidence-recovery-status")

    _require_non_empty_string_list(recovery, "evidence", "code-review")
    recovery_actions = _require_non_empty_string_list(recovery, "recovery_actions", "code-review")
    remaining_limits = recovery.get("remaining_limits")
    if not isinstance(remaining_limits, list) or not all(isinstance(item, str) for item in remaining_limits):
        raise SystemExit("review-invalid-remaining-limits")

    confidence_value = float(confidence)
    if confidence_value <= 0.8:
        if result["status"] == "pass":
            raise SystemExit("review-pass-confidence-not-acceptable")
        if "confidence-not-acceptable" not in checks_failed:
            raise SystemExit("review-missing-confidence-not-acceptable-check")
        if status != "not-acceptable-failed":
            raise SystemExit("review-confidence-status-should-fail")
        if not recovery_actions or not remaining_limits:
            raise SystemExit("review-low-confidence-recovery-missing")
    elif confidence_value < 0.85:
        if result["status"] == "pass":
            raise SystemExit("review-pass-confidence-very-questionable")
        if "confidence-very-questionable" not in checks_failed:
            raise SystemExit("review-missing-confidence-very-questionable-check")
        if status != "very-questionable":
            raise SystemExit("review-confidence-status-should-be-very-questionable")
        if not recovery_actions or not remaining_limits:
            raise SystemExit("review-very-questionable-confidence-evidence-missing")
    elif confidence_value < 0.9:
        if status != "cautious-low":
            raise SystemExit("review-confidence-status-should-be-cautious-low")
        if not recovery_actions or not remaining_limits:
            raise SystemExit("review-cautious-low-confidence-evidence-missing")
    elif status != "fair":
        raise SystemExit("review-confidence-status-should-be-fair")


def _consolidation_confidence_evidence(out_dir: Path, manifest: dict[str, Any]) -> set[str]:
    """Bind recovery references to admitted outputs and source-bound PR gates without judging relevance."""
    references: set[str] = set()
    for record in manifest["batch_execution"]["waves"]:
        path = _resolve_path(out_dir, record["manifest_path"])
        if _sha256(path) != record["manifest_sha256"]:
            raise SystemExit("review-consolidation-confidence-evidence-invalid")
        wave = _load_json(path)
        for item in wave["passes"]:
            selected = next(attempt for attempt in item["attempts"] if attempt["attempt"] == item["selected_attempt"])
            output = _resolve_path(path.parent, item["output_path"])
            if _sha256(output) != selected["output_sha256"]:
                raise SystemExit("review-consolidation-confidence-evidence-invalid")
            references.add(output.relative_to(out_dir).as_posix())
    # Existing PR admission binds gate execution/import receipts to the exact reviewed revision.
    routing_path = out_dir / "pr-routing.json"
    routing = _load_json(routing_path) if routing_path.is_file() else {}
    gates = _load_json(out_dir / "gates.json")
    if routing.get("checkout_method") == "git-detached-review-worktree" and gates.get("source"):
        _validate_verified_pr_source(
            out_dir, routing, _load_json(out_dir / "target-branch.json"), _load_json(out_dir / "local-checkout.json")
        )
        for check in gates["checks"]:
            if check["status"] == "pass" and check.get("source"):
                for key in ("command_path", "stdout", "stderr"):
                    path = _resolve_path(out_dir, check[key])
                    if not path.is_file():
                        raise SystemExit("review-consolidation-confidence-evidence-invalid")
                    references.add(path.relative_to(out_dir).as_posix())
    elif routing_path.exists() is False and gates.get("source", {}).get("mode") == "local-review-mirror":
        source = gates["source"]
        import review_batches

        inventory = review_batches.validate_inventory(out_dir)
        root = Path(source["worktree"])
        if inventory["source_arguments"]["source_root"] != root.as_posix():
            raise SystemExit("local-confidence-source-root-mismatch")
        current = validate_local_gate_source(out_dir, root)
        snapshot_path = _resolve_path(out_dir, source.get("snapshot_path"))
        receipt_path = _resolve_path(out_dir, source.get("receipt_path"))
        if (
            source.get("snapshot_path") != "checks/local-source-snapshot.json"
            or source.get("receipt_path") != "local-source/review-worktree.json"
            or snapshot_path.is_symlink()
            or receipt_path.is_symlink()
            or _sha256(snapshot_path) != source.get("snapshot_sha256")
            or _sha256(receipt_path) != source.get("receipt_sha256")
            or hashlib.sha256(_local_snapshot_bytes(current)).hexdigest() != source.get("snapshot_sha256")
        ):
            raise SystemExit("local-confidence-source-snapshot-invalid")
        selected = {record["path"]: record for record in current["files"]}
        if any(selected.get(record["path"]) != record for record in inventory["source_snapshot"]["files"]):
            raise SystemExit("local-confidence-source-selection-mismatch")
        tests = [check for check in gates["checks"] if check["id"] == "tests"]
        if len(tests) != 1:
            raise SystemExit("local-confidence-tests-invalid")
        check = tests[0]
        if check["status"] != "pass":
            return references
        receipt = check.get("source")
        fields = ("mode", "worktree", "receipt_path", "receipt_sha256", "snapshot_path", "snapshot_sha256")
        if (
            not isinstance(receipt, dict)
            or any(receipt.get(key) != source.get(key) for key in fields)
            or any(
                receipt.get(phase) != {"snapshot_sha256": source["snapshot_sha256"]} for phase in ("before", "after")
            )
        ):
            raise SystemExit("local-confidence-tests-source-invalid")
        proof_path = out_dir / "checks/tests.python-imports.json"
        try:
            if (
                proof_path.is_symlink()
                or not proof_path.resolve().is_relative_to(out_dir.resolve())
                or not proof_path.is_file()
            ):
                raise ValueError("missing-or-escaped-proof")
            proof_bytes = proof_path.read_bytes()
            if (
                receipt.get("artifacts", {}).get("checks/tests.python-imports.json")
                != hashlib.sha256(proof_bytes).hexdigest()
            ):
                raise ValueError("proof-hash-mismatch")
            if json.loads(proof_bytes.decode("utf-8")) != check.get("python_imports"):
                raise ValueError("proof-inline-mismatch")
        except (OSError, ValueError, UnicodeError):
            raise SystemExit("local-confidence-tests-proof-invalid") from None
        current["transport"] = {"snapshot_path": snapshot_path.as_posix(), "snapshot_sha256": source["snapshot_sha256"]}
        _validate_pr_tests_import_proof(gates["checks"], root.as_posix(), current)
        for key in ("command_path", "stdout", "stderr"):
            path = _resolve_path(out_dir, check[key])
            if path.is_symlink() or not path.is_file() or receipt.get("artifacts", {}).get(check[key]) != _sha256(path):
                raise SystemExit("local-confidence-tests-artifact-invalid")
            references.add(path.relative_to(out_dir).as_posix())
    return references


def _scoped_confidence_deduction(gap: dict[str, Any]) -> int:
    """Read one historical cause's contribution from either permitted field without double-counting duplicate labels."""
    tokens = set(re.findall(r"\([-−](0\.\d{2}|1\.00)\)", gap["gap"] + "\n" + gap["rationale"]))
    if len(tokens) > 1:
        raise SystemExit("review-consolidation-confidence-scoped-deduction-ambiguous")
    return round(float(next(iter(tokens))) * 100) if tokens else 0


def _validate_consolidation_confidence(
    out_dir: Path, manifest: dict[str, Any], result: dict[str, Any], metadata: dict[str, Any]
) -> None:
    """Require all scoped causes, admitted evidence and complete deductions before global confidence recovery."""
    minimum = min(item["confidence"] for item in manifest["passes"])
    labels: dict[str, int] = {}
    prefixes = []
    for item in manifest["passes"]:
        for part in item["source_parts" if manifest.get("review_topology") == "source-only" else "final_parts"]:
            prefixes.append(f"{item['role']} {part['output_path']}: ")
            accounted = 0
            for gap in part["confidence"]["gaps"]:
                historical = _scoped_confidence_deduction(gap) if result["confidence"] > minimum else 0
                accounted += historical
                if gap["status"] != "closed":
                    label = f"{item['role']} {part['output_path']}: {gap['gap']} ({gap['status']}) - {gap['rationale']}"
                    if label not in metadata.get("confidence_gaps", []):
                        raise SystemExit("review-consolidation-confidence-gap-dropped")
                    labels[label] = historical
            residual = round((1 - part["confidence"]["score"]) * 100) - accounted
            if result["confidence"] > minimum and residual > 0:
                label = (
                    f"{item['role']} {part['output_path']}: residual scoped confidence shortfall {residual / 100:.2f}"
                )
                if label not in metadata.get("confidence_gaps", []):
                    raise SystemExit("review-consolidation-confidence-gap-dropped")
                labels[label] = residual
    if result["confidence"] <= minimum:
        return
    recovery = metadata.get("confidence_recovery", {})
    if recovery.get("final_confidence") != result["confidence"]:
        raise SystemExit("review-consolidation-confidence-inflated")
    if recovery.get("initial_confidence") != minimum:
        raise SystemExit("review-consolidation-confidence-initial-mismatch")
    _validate_confidence_gaps(result, metadata)
    _validate_confidence_recovery(result, metadata)
    references = _consolidation_confidence_evidence(out_dir, manifest)
    if any(evidence not in references for evidence in recovery["evidence"]):
        raise SystemExit("review-consolidation-confidence-evidence-invalid")
    closures = {closure["gap"].strip(): closure for closure in metadata["confidence_gap_closures"]}
    if any(label.startswith(tuple(prefixes)) and label not in labels for label in closures):
        raise SystemExit("review-consolidation-confidence-gap-unknown")
    deductions = {}
    for label, closure in closures.items():
        # Historical labels quoted in the prose retain their own numbers; only the leading current token contributes.
        match = re.match(r"\(-0\.(\d{2})\)(?:\s|$)", closure.get("rationale", ""))
        if match is None or (closure["status"] == "closed" and match[1] != "00"):
            raise SystemExit("review-consolidation-confidence-accounting-invalid")
        deductions[label] = int(match[1])
        if closure.get("evidence") and closure.get("evidence_path") and closure["evidence"] != closure["evidence_path"]:
            raise SystemExit("review-consolidation-confidence-evidence-invalid")
        if closure["status"] == "closed":
            evidence = closure.get("evidence_path") or closure.get("evidence")
            if evidence not in references or evidence not in recovery["evidence"]:
                raise SystemExit("review-consolidation-confidence-evidence-invalid")
    for label in labels:
        closure = closures[label]
        reduced = deductions[label] < labels[label]
        shared = any(
            other != label and deduction > 0 and other in closure["rationale"]
            for other, deduction in deductions.items()
        )
        if closure["status"] != "closed" and reduced and not shared:
            evidence = closure.get("evidence_path") or closure.get("evidence")
            if evidence not in references or evidence not in recovery["evidence"]:
                raise SystemExit("review-consolidation-confidence-evidence-invalid")
        if closure["status"] != "closed" and deductions[label] == 0 and not shared:
            raise SystemExit("review-consolidation-confidence-accounting-invalid")
    if abs(sum(deductions.values()) / 100 - (1 - result["confidence"])) > 0.000001:
        raise SystemExit("review-consolidation-confidence-accounting-invalid")


def _validate_review_runtime(
    out_dir: Path,
    manifest: dict[str, Any],
    passes: list[dict[str, Any]],
    codex_home: Path,
    parent_thread_id: str,
    *,
    require_assessment: bool = True,
    roles_dir: Path = PLUGIN_ROOT / "roles",
) -> dict[str, object]:
    """Validate route-specific execution evidence before granting reviewer independence."""
    if manifest.get("schema_version") in {7, 8, 9} and manifest.get("manifest_kind") == "batched-review":
        import review_batches  # Verified circular producer/validator boundary; resolve after initialization.

        return review_batches.validate_aggregate(
            out_dir,
            manifest,
            {item["role"] for item in passes},
            codex_home,
            parent_thread_id,
            retained_role_cards=roles_dir == out_dir / "role-cards",
        )
    if manifest.get("schema_version") in {5, 6, 7, 8} or _paged_native_manifest(manifest):
        return _validate_instruction_bounded_review(out_dir, manifest, passes, codex_home, parent_thread_id, roles_dir)
    if manifest.get("schema_version") == 4:
        return _validate_local_reviewer_wave(
            out_dir, manifest, passes, require_assessment=require_assessment, roles_dir=roles_dir
        )
    spawned = [item for item in passes if item.get("mode") == "spawned"]
    if not spawned:
        if manifest.get("runtime_execution") is not None:
            raise SystemExit("review-runtime-execution-unexpected")
        return {}
    runtime = manifest.get("runtime_execution")
    if not isinstance(runtime, dict) or set(runtime) != {"manifest_path", "manifest_sha256", "plan_path"}:
        raise SystemExit("review-runtime-execution-missing")
    execution_path = _resolve_path(out_dir, runtime.get("manifest_path"))
    plan_path = _resolve_path(out_dir, runtime.get("plan_path"))
    if not execution_path.is_file() or _sha256(execution_path) != runtime.get("manifest_sha256"):
        raise SystemExit("review-runtime-execution-hash-mismatch")
    execution = _load_json(execution_path)
    stages = execution.get("stages")
    if not isinstance(stages, list):
        raise SystemExit("review-runtime-execution-role-mismatch")
    nodes = [node for stage in stages if isinstance(stage, dict) for node in stage.get("nodes", [])]
    if any(not isinstance(node, dict) for node in nodes):
        raise SystemExit("review-runtime-execution-role-mismatch")
    nodes_by_role: dict[str, dict[str, Any]] = {}
    for node in nodes:
        role = node.get("role_id")
        if not isinstance(role, str) or role in nodes_by_role:
            raise SystemExit("review-runtime-execution-role-mismatch")
        nodes_by_role[role] = node
    passes_by_role = {str(item.get("role")): item for item in spawned}
    if set(nodes_by_role) != set(passes_by_role):
        raise SystemExit("review-runtime-execution-role-mismatch")
    for role, item in passes_by_role.items():
        selected = item.get("selected_attempt")
        attempts = item.get("attempts")
        node = nodes_by_role[role]
        node_selected = node.get("selected_attempt")
        node_attempts = node.get("attempts")
        if (
            not isinstance(selected, int)
            or not isinstance(attempts, list)
            or not 1 <= selected <= len(attempts)
            or not isinstance(node_selected, int)
            or not isinstance(node_attempts, list)
            or not 1 <= node_selected <= len(node_attempts)
            or not isinstance(attempts[selected - 1], dict)
            or not isinstance(node_attempts[node_selected - 1], dict)
        ):
            raise SystemExit(f"review-runtime-execution-attempt-mismatch:{role}")
        if _resolve_path(out_dir, item.get("output_path")) != _resolve_path(
            out_dir, node_attempts[node_selected - 1].get("output_path")
        ) or _resolve_path(out_dir, attempts[selected - 1].get("context_path")) != _resolve_path(
            out_dir, node.get("context_path")
        ):
            raise SystemExit(f"review-runtime-execution-evidence-mismatch:{role}")
    try:
        summary = validate_read_only_runtime(
            execution,
            manifest_path=execution_path,
            plan_path=plan_path,
            parent_rollout=_find_rollout(codex_home, parent_thread_id),
            sessions_dir=codex_home / "sessions",
            run_dir=out_dir,
            roles_dir=roles_dir,
            expected_consumer_id="code-review",
        )
    except ValueError as error:
        raise SystemExit(f"review-runtime-execution-invalid:{error}") from error
    if (
        summary.get("evidence_level") != "portable-read-restricted"
        or summary.get("network_mode") != "restricted"
        or summary.get("approval_policy") != "never"
        or summary.get("filesystem_credential_isolation") != "unverified"
        or summary.get("runtime_promotion_eligible") is not True
        or summary.get("write_parallel_eligible") is not False
        or summary.get("consumer_id") != "code-review"
    ):
        raise SystemExit("review-runtime-execution-evidence-invalid")
    return summary


def _validate_manifest_entries(
    out_dir: Path,
    manifest: dict[str, Any],
    passes: list[dict[str, Any]],
    triggered_roles: set[str],
    codex_home: Path,
    parent_thread_id: str,
    project_root: Path,
    *,
    require_assessment: bool = True,
    retained_role_cards: bool = False,
    require_role_card_receipts: bool = False,
    runtime_summary: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Bind every triggered pass to its evidence and expose the native runtime summary when requested."""
    schema_version = manifest.get("schema_version")
    if schema_version not in {2, 3, 4, 5, 6, 7, 8, 9}:
        raise SystemExit("manifest-schema-version")
    if schema_version in {7, 8, 9}:
        kind = manifest.get("manifest_kind")
        if kind not in {"native-wave", "batched-review"}:
            raise SystemExit("manifest-kind-invalid")
        if schema_version == 9 and kind != "batched-review":
            raise SystemExit("manifest-schema-nine-aggregate-only")
        if kind == "batched-review":
            import review_batches  # Resolve the circular aggregate validation boundary after initialization.

            if passes != manifest.get("passes"):
                raise SystemExit("review-batch-pass-input-mismatch")
            summary = review_batches.validate_aggregate(
                out_dir,
                manifest,
                triggered_roles,
                codex_home,
                parent_thread_id,
                retained_role_cards=retained_role_cards,
            )
            if runtime_summary is not None:
                runtime_summary.update(summary)
            return {item["role"]: item for item in passes}
        if manifest.get("dispatch_protocol") not in {"paged-context-v6", "paged-context-v7", "paged-context-v8"}:
            raise SystemExit("manifest-dispatch-protocol-invalid")
        reader_path = Path(str(manifest.get("context_reader_path", "")))
        # Historical readers retain their issued dispatch recipe while exact page calls and outputs remain checked.
        current_reader = Path(__file__).with_name("review_context.py")
        if (
            not reader_path.is_absolute()
            or not reader_path.is_file()
            or reader_path.name != "review_context.py"
            or _sha256(reader_path) != manifest.get("context_reader_sha256")
            or manifest["context_reader_sha256"] not in {*LEGACY_WORKDIR_READER_SHA256S, _sha256(current_reader)}
            or (
                manifest["dispatch_protocol"] == "paged-context-v8"
                and manifest["context_reader_sha256"] != _sha256(current_reader)
            )
            or (
                manifest["dispatch_protocol"] in {"paged-context-v7", "paged-context-v8"}
                and manifest["context_reader_sha256"] == LEGACY_PROTOCOL_V6_READER_SHA256
            )
        ):
            raise SystemExit("manifest-context-reader-identity-invalid")
    if _paged_native_manifest(manifest):
        reader = manifest.get("context_reader_python")
        if (
            not isinstance(reader, str)
            or not Path(reader).is_absolute()
            or not Path(reader).is_file()
            or not Path(reader).name.lower().startswith("python")
        ):
            raise SystemExit("manifest-context-reader-python-invalid")
    for key in ("review_run_id", "parent_thread_id", "review_input_sha256"):
        if not isinstance(manifest.get(key), str) or not manifest[key]:
            raise SystemExit(f"manifest-missing-{key}")
    if manifest["parent_thread_id"] != parent_thread_id:
        raise SystemExit("manifest-parent-thread-mismatch")
    review_input = out_dir / "diff.patch"
    if not review_input.exists() or _sha256(review_input) != manifest["review_input_sha256"]:
        raise SystemExit("manifest-review-input-hash-mismatch")
    roles_dir = out_dir / "role-cards" if retained_role_cards else PLUGIN_ROOT / "roles"
    if schema_version == 4:
        _validate_local_reviewer_wave(
            out_dir, manifest, passes, require_assessment=require_assessment, roles_dir=roles_dir
        )
    elif manifest.get("app_server_execution") is not None or any(item.get("mode") == "app-server" for item in passes):
        raise SystemExit("review-app-server-schema-required")
    parent_rows: list[dict[str, Any]] | None = None
    used_threads: set[str] = set()
    used_context_paths: set[Path] = set()
    used_output_paths: set[Path] = set()
    by_role: dict[str, dict[str, Any]] = {}
    _validate_sol_selections(manifest, triggered_roles, label="manifest-sol-selection")
    for item in passes:
        role = item.get("role")
        if "recovery" in item and (schema_version != 8 or item.get("mode") != "inspection"):
            raise SystemExit(f"manifest-invalid-internal-recovery:{role}")
        axis = item.get("axis")
        mode = item.get("mode")
        trigger = item.get("trigger")
        confidence = item.get("confidence")
        blocking_findings = item.get("blocking_findings")
        if not isinstance(role, str) or role not in ALL_MANIFEST_ROLES:
            raise SystemExit(f"manifest-invalid-role:{role!r}")
        if role in by_role:
            raise SystemExit(f"manifest-duplicate-role:{role}")
        if retained_role_cards:
            _resolve_path(out_dir, str(roles_dir / role / "ROLE.md"))
        role_card = _load_role_card(roles_dir, role)
        if schema_version in {3, 4, 5, 6, 7, 8} and item.get("role_card_sha256") != role_card["role_card_sha256"]:
            raise SystemExit(f"manifest-role-card-hash-mismatch:{role}")
        if require_role_card_receipts and schema_version in {3, 4, 5, 6, 7, 8} and not retained_role_cards:
            retained_path = _resolve_path(out_dir, f"role-cards/{role}/ROLE.md")
            if not retained_path.is_file() or _sha256(retained_path) != role_card["role_card_sha256"]:
                raise SystemExit(f"manifest-retained-role-card-mismatch:{role}")
        if not isinstance(axis, str) or not axis.strip():
            raise SystemExit(f"manifest-missing-axis:{role}")
        if mode not in VALID_MODES:
            raise SystemExit(f"manifest-invalid-mode:{role}:{mode!r}")
        if (schema_version in {5, 6, 7, 8} and mode not in {"inspection", "substituted"}) or (
            schema_version not in {5, 6, 7, 8} and mode == "inspection"
        ):
            raise SystemExit(f"manifest-mode-schema-mismatch:{role}")
        if not isinstance(trigger, str) or not trigger.strip():
            raise SystemExit(f"manifest-missing-trigger:{role}")
        if not isinstance(confidence, int | float) or not 0.0 <= float(confidence) <= 1.0:
            raise SystemExit(f"manifest-invalid-confidence:{role}")
        if type(blocking_findings) is not int or blocking_findings < 0:
            raise SystemExit(f"manifest-invalid-blocking-findings:{role}")
        output_path = _resolve_path(out_dir, item.get("output_path"))
        if not output_path.exists():
            raise SystemExit(f"manifest-missing-output:{role}:{output_path}")
        if mode in {"spawned", "inspection"}:
            if parent_rows is None:
                parent_rows = _read_jsonl(_find_rollout(codex_home, parent_thread_id))
            _validate_spawn_attempts(
                out_dir,
                item,
                manifest,
                codex_home,
                parent_rows,
                used_threads,
                role_card,
                used_context_paths,
                used_output_paths,
            )
        elif mode == "app-server":
            if output_path in used_output_paths:
                raise SystemExit("manifest-reused-output-path")
            used_output_paths.add(output_path)
        elif item.get("attempts") not in (None, []):
            raise SystemExit(f"manifest-substitute-has-attempts:{role}")
        else:
            if output_path in used_output_paths:
                raise SystemExit("manifest-reused-output-path")
            used_output_paths.add(output_path)
            try:
                substitute_output = output_path.read_text(encoding="utf-8").strip()
            except OSError as error:
                raise SystemExit(f"manifest-substitute-output-not-role-bound:{role}") from error
            first_line = substitute_output.splitlines()[0].strip() if substitute_output else ""
            explicit_header = first_line == f"role_id: {role}"
            legacy_heading = first_line == f"{role}:" or re.fullmatch(rf"#{{1,6}}\s+{re.escape(role)}", first_line)
            if not explicit_header and (schema_version in {5, 6, 7, 8} or not legacy_heading):
                raise SystemExit(f"manifest-substitute-output-not-role-bound:{role}")
        by_role[role] = item

    if set(by_role) != triggered_roles:
        raise SystemExit("manifest-triggered-role-set-mismatch")
    if schema_version in {7, 8}:
        summary = _validate_review_runtime(out_dir, manifest, passes, codex_home, parent_thread_id, roles_dir=roles_dir)
        if "reviewer_findings_version" in manifest:
            import review_batches  # Reuse the existing circular batch boundary after native provenance admission.

            review_batches.validate_wave_findings(out_dir, manifest, passes)
        if runtime_summary is not None:
            runtime_summary.update(summary)
    return by_role


def _validate_manifest_preflight(
    out_dir: Path,
    codex_home: Path,
    parent_thread_id: str,
    project_root: Path,
) -> None:
    """Validate specialist routing and provenance before writing a result candidate."""
    routing = _load_json(out_dir / "review-routing.json")
    risk_tier = routing.get("risk_tier")
    if risk_tier not in {"TRIVIAL", "LOCAL", "BROAD", "HIGH_RISK"}:
        raise SystemExit(f"invalid-risk-tier:{risk_tier!r}")
    triggered_roles = _validate_routing(out_dir, risk_tier)
    manifest = _load_json(out_dir / "specialist-manifest.json")
    routing = _load_json(out_dir / "review-routing.json")
    if manifest.get("sol_selection") != routing.get("sol_selection"):
        raise SystemExit("manifest-sol-selection-routing-mismatch")
    passes = _manifest_passes(manifest)
    _validate_manifest_entries(
        out_dir,
        manifest,
        passes,
        triggered_roles,
        codex_home,
        parent_thread_id,
        project_root,
        require_role_card_receipts=True,
    )
    if manifest.get("schema_version") in {3, 4, 5, 6}:
        _validate_review_runtime(out_dir, manifest, passes, codex_home, parent_thread_id)


def _validate_challenge_manifest_preflight(
    out_dir: Path,
    codex_home: Path,
    parent_thread_id: str,
    project_root: Path,
) -> None:
    """Validate one challenge reviewer without Code Review's multi-axis routing."""
    manifest = _load_json(out_dir / "specialist-manifest.json")
    passes = _manifest_passes(manifest)
    if (
        manifest.get("schema_version") not in {4, 5, 6, 7, 8}
        or len(passes) != 1
        or passes[0].get("role") != "challenger"
        or passes[0].get("mode") not in {"app-server", "inspection"}
    ):
        raise SystemExit("manifest-triggered-role-set-mismatch")
    _validate_manifest_entries(
        out_dir,
        manifest,
        passes,
        {"challenger"},
        codex_home,
        parent_thread_id,
        project_root,
    )
    if manifest.get("schema_version") not in {7, 8}:
        _validate_review_runtime(out_dir, manifest, passes, codex_home, parent_thread_id)


# ``NamedTuple``, not ``@dataclass``: callers load this validator by file path without registering it in
# ``sys.modules``, and under ``from __future__ import annotations`` every field annotation reaches
# ``dataclasses._is_type``, which dereferences ``sys.modules[cls.__module__]`` and raises on the missing entry.
class _ReviewEnvironment(NamedTuple):
    """Ambient run locations a review validation needs to resolve recorded evidence."""

    codex_home: Path
    parent_thread_id: str
    project_root: Path


class _SpecialistEvidence(NamedTuple):
    """Validated specialist manifest state shared by the later review-decision checks."""

    triggered_roles: set[str]
    manifest: dict[str, Any]
    routing: dict[str, Any]
    passes: list[dict[str, Any]]
    by_role: dict[str, Any]
    runtime_summary: dict[str, Any]


def _require_pr_artifacts(out_dir: Path, result: dict[str, Any]) -> None:
    """Confirm every PR artifact the review contract requires is present on disk."""
    required_pr_artifacts = (
        "pr.json",
        "pr-routing.json",
        "target-branch.json",
        "local-checkout.json",
        "comments.json",
        "reviews.json",
        "review-threads.json",
        "unresolved-review-threads.json",
        "online-review-summary.json",
        "remote-selection.json",
        "diff.patch",
    )
    if result.get("schema_version") in {2, 3}:
        required_pr_artifacts += ("pr-head-fetch.json", "worktree-preflight.json")
    for filename in required_pr_artifacts:
        if not (out_dir / filename).exists():
            raise SystemExit(f"missing-pr-artifact:{filename}")


def _validate_pr_routing_identity(routing: dict[str, Any], remote_selection: dict[str, Any]) -> None:
    """Check PR routing names an authoritative open-PR base that matches the selected remote."""
    if routing.get("base_identity_source") != "pr_url":
        raise SystemExit("pr-routing-base-identity-not-authoritative")
    if routing.get("pr_state") != "OPEN":
        raise SystemExit("pr-state-not-open-for-merge-review")
    expected_identity = remote_selection.get("expected")
    if not isinstance(expected_identity, dict):
        raise SystemExit("pr-remote-selection-expected-missing")
    if expected_identity.get("host") != routing.get("base_host"):
        raise SystemExit("pr-remote-selection-host-mismatch")
    if expected_identity.get("repository") != routing.get("base_repo"):
        raise SystemExit("pr-remote-selection-repository-mismatch")
    if routing.get("local_checkout_required") is not True:
        raise SystemExit("pr-routing-local-checkout-not-required")
    if "--force" in str(routing.get("local_checkout_command", "")):
        raise SystemExit("pr-routing-force-checkout-forbidden")
    if "force_policy" not in routing:
        raise SystemExit("pr-routing-force-policy-missing")


def _validate_current_pr_checkout_receipt(routing: dict[str, Any], checkout: dict[str, Any]) -> None:
    """Match the recorded checkout receipt against the command its declared method implies."""
    # Older receipts omit the method; current collectors record the operation actually performed.
    review_worktree = Path(checkout["worktree"]) if isinstance(checkout.get("worktree"), str) else None
    checkout_commands = {
        None: f"git checkout --detach {routing.get('head_oid')}",
        "gh-pr-checkout": f"gh pr checkout {routing.get('pr_url')}",
        "git-detached-review-fallback": f"git checkout --detach {routing.get('head_oid')}",
        "git-detached-review-worktree": f"git worktree add --detach {review_worktree} {routing.get('head_oid')}",
        "already-at-head": "not-run: already at expected PR head",
    }
    method = routing.get("checkout_method")
    expected_checkout = checkout_commands.get(method) if isinstance(method, (str, type(None))) else None
    receipt_command = checkout.get("command")
    valid_receipt_command = receipt_command == expected_checkout or (
        method == "git-detached-review-worktree" and receipt_command == "not-run: existing exact clean review worktree"
    )
    if (
        expected_checkout is None
        or routing.get("local_checkout_command") != expected_checkout
        or not valid_receipt_command
        or checkout.get("checkout_method") != method
        or routing.get("checkout_mode") != (None if method is None else "review")
        or checkout.get("checkout_mode") != routing.get("checkout_mode")
    ):
        raise SystemExit("pr-routing-checkout-command-invalid")


def _validate_legacy_pr_checkout_command(routing: dict[str, Any], remote_selection: dict[str, Any]) -> None:
    """Match a pre-schema-2 routing record against the checkout command its transport implies."""
    expected_checkout = f"gh pr checkout {routing.get('pr_number')}"
    if routing.get("pr_metadata_transport") == "public-https-fallback":
        expected_checkout = (
            f"git checkout --detach refs/remotes/{remote_selection.get('remote')}/pull/{routing.get('pr_number')}/head"
        )
    if routing.get("local_checkout_command") != expected_checkout:
        raise SystemExit("pr-routing-checkout-command-invalid")


def _validate_pr_target_branch(
    target_branch: dict[str, Any],
    remote_selection: dict[str, Any],
    routing: dict[str, Any],
) -> None:
    """Check the fetched target branch matches the selected remote and the PR's base commit."""
    if target_branch.get("status") != "fetched":
        raise SystemExit("pr-target-branch-not-fetched")
    if target_branch.get("remote") != remote_selection.get("remote"):
        raise SystemExit("pr-target-branch-remote-mismatch")
    if target_branch.get("remote_url") != remote_selection.get("remote_url"):
        raise SystemExit("pr-target-branch-remote-url-mismatch")
    expected_base = target_branch.get("expected_base_oid")
    local_base = target_branch.get("local_head")
    if not expected_base or expected_base != routing.get("base_oid"):
        raise SystemExit("pr-target-branch-expected-oid-missing")
    base_matches = local_base == expected_base
    base_is_ancestor = target_branch.get("expected_base_is_ancestor") is True
    expected_relation = "matches-pr-metadata" if base_matches else "advanced" if base_is_ancestor else "diverged"
    if (
        not local_base
        or target_branch.get("base_matches_pr_metadata") is not base_matches
        or target_branch.get("base_relation") != expected_relation
        or not base_is_ancestor
    ):
        raise SystemExit("pr-target-branch-oid-mismatch")


def _validate_pr_local_checkout_state(checkout: dict[str, Any], routing: dict[str, Any]) -> None:
    """Check the local checkout receipt is unforced and pinned to the PR head commit."""
    if checkout.get("status") != "checked-out":
        raise SystemExit("pr-local-checkout-not-checked-out")
    if checkout.get("pr_url") != routing.get("pr_url"):
        raise SystemExit("pr-local-checkout-url-mismatch")
    if "--force" in str(checkout.get("command", "")):
        raise SystemExit("pr-local-checkout-force-forbidden")
    if "force_policy" not in checkout:
        raise SystemExit("pr-local-checkout-force-policy-missing")
    if checkout.get("head_matches_pr") is not True:
        raise SystemExit("pr-local-checkout-head-mismatch")
    if not checkout.get("expected_head") or checkout.get("expected_head") != routing.get("head_oid"):
        raise SystemExit("pr-local-checkout-expected-head-missing")
    if checkout.get("local_head") != checkout.get("expected_head"):
        raise SystemExit("pr-local-checkout-oid-mismatch")


def _validate_pr_diff_provenance(checkout: dict[str, Any], routing: dict[str, Any]) -> None:
    """Check the recorded diff came from the verified local checkout at the PR's own commit range."""
    if routing.get("checkout_method") == "git-detached-review-worktree":
        review_worktree = Path(checkout["worktree"]) if isinstance(checkout.get("worktree"), str) else None
        expected_diff_command = (
            f"git -C {review_worktree} diff --binary {routing.get('base_oid')}...{routing.get('head_oid')} --"
        )
    else:
        expected_diff_command = f"git diff --binary {routing.get('base_oid')}...{routing.get('head_oid')} --"
    if (
        checkout.get("diff_source") != "verified-local-checkout"
        or checkout.get("diff_base_oid") != routing.get("base_oid")
        or checkout.get("diff_head_oid") != routing.get("head_oid")
        or checkout.get("diff_command") != expected_diff_command
    ):
        raise SystemExit("pr-local-diff-provenance-invalid")


def _validate_unavailable_review_threads(
    out_dir: Path,
    thread_error: Any,
    metadata: dict[str, Any],
    notes_text: str,
) -> None:
    """Check the recorded evidence and documented gaps when review threads were unavailable."""
    error_path = out_dir / "review-threads-error.txt"
    if not isinstance(thread_error, str) or not error_path.is_file():
        raise SystemExit("pr-review-thread-error-missing")
    if error_path.read_text(encoding="utf-8").strip() != thread_error:
        raise SystemExit("pr-review-thread-error-mismatch")
    if _load_json_list(out_dir / "review-threads.json") or _load_json_list(out_dir / "unresolved-review-threads.json"):
        raise SystemExit("pr-review-thread-unavailable-must-be-empty")
    confidence_gaps = metadata.get("confidence_gaps")
    if not isinstance(confidence_gaps, list) or PR_THREAD_CONFIDENCE_GAP not in confidence_gaps:
        raise SystemExit("pr-review-thread-confidence-gap-missing")
    if "review-thread" not in notes_text.casefold() or "unavailable" not in notes_text.casefold():
        raise SystemExit("pr-review-thread-triage-gap-missing")


def _validate_pr_review_threads(
    out_dir: Path,
    online_summary: dict[str, Any],
    metadata: dict[str, Any],
    notes_text: str,
) -> None:
    """Reconcile recorded review-thread status against its error evidence and triage notes."""
    thread_status = online_summary.get("review_threads_status")
    thread_error = online_summary.get("review_threads_error")
    if thread_status == "available":
        if thread_error is not None or (out_dir / "review-threads-error.txt").exists():
            raise SystemExit("pr-review-thread-status-contradiction")
    elif thread_status == "unavailable":
        _validate_unavailable_review_threads(out_dir, thread_error, metadata, notes_text)
    else:
        raise SystemExit("pr-review-thread-status-invalid")


def _validate_pr_review_scope(
    out_dir: Path,
    result: dict[str, Any],
    metadata: dict[str, Any],
    notes_path: Path,
) -> None:
    """Validate the PR-scope artifact set behind a review: routing, checkout, diff, and threads."""
    notes_text = notes_path.read_text(encoding="utf-8")
    if "Online Review Triage" not in notes_text:
        raise SystemExit("missing-pr-online-review-triage")
    _require_pr_artifacts(out_dir, result)
    routing = _load_json(out_dir / "pr-routing.json")
    pr_payload = _load_json(out_dir / "pr.json")
    remote_selection = _load_json(out_dir / "remote-selection.json")
    target_branch = _load_json(out_dir / "target-branch.json")
    checkout = _load_json(out_dir / "local-checkout.json")
    online_summary = _load_json(out_dir / "online-review-summary.json")
    fallback_gap = _validate_pr_fallback_confidence(online_summary, result, metadata)
    if fallback_gap is not None and fallback_gap not in notes_text:
        raise SystemExit("pr-public-fallback-confidence-gap-not-documented")
    if not isinstance(pr_payload.get("body"), str):
        raise SystemExit("pr-description-missing")
    _validate_pr_routing_identity(routing, remote_selection)
    if result.get("schema_version") in {2, 3}:
        _validate_current_pr_checkout_receipt(routing, checkout)
    else:
        _validate_legacy_pr_checkout_command(routing, remote_selection)
    _validate_pr_target_branch(target_branch, remote_selection, routing)
    _validate_pr_local_checkout_state(checkout, routing)
    _validate_pr_diff_provenance(checkout, routing)
    if result.get("schema_version") in {2, 3}:
        _validate_verified_pr_source(out_dir, routing, target_branch, checkout)
    _validate_pr_review_threads(out_dir, online_summary, metadata, notes_text)
    if (out_dir / "head-files").exists():
        raise SystemExit("pr-raw-head-file-snapshots-forbidden")


def _validate_runtime_summary_metadata(
    metadata: dict[str, Any],
    manifest: dict[str, Any],
    runtime_summary: dict[str, Any],
) -> None:
    """Cross-check result metadata against the execution mode the runtime evidence recorded."""
    if runtime_summary:
        for key, expected in (
            ("execution_mode", runtime_summary.get("actual_mode")),
            ("execution_evidence_level", runtime_summary.get("evidence_level")),
            ("write_parallel_eligible", False),
        ):
            if metadata.get(key) != expected:
                raise SystemExit(f"metadata-{key.replace('_', '-')}-mismatch")
        if manifest.get("schema_version") in {5, 6, 7, 8, 9}:
            if metadata.get("execution_observed_controls") != runtime_summary.get("observed_controls"):
                raise SystemExit("metadata-execution-observed-controls-mismatch")
    if metadata.get("review_run_id") != manifest.get("review_run_id"):
        raise SystemExit("metadata-review-run-id-mismatch")
    if metadata.get("review_input_sha256") != manifest.get("review_input_sha256"):
        raise SystemExit("metadata-review-input-hash-mismatch")


def _validate_specialist_manifest(
    out_dir: Path,
    result: dict[str, Any],
    result_path: Path,
    metadata: dict[str, Any],
    risk_tier: str,
    env: _ReviewEnvironment,
) -> _SpecialistEvidence:
    """Validate routing, the specialist manifest, and the recorded review runtime evidence."""
    triggered_roles = _validate_routing(out_dir, risk_tier)
    manifest_path = _resolve_path(out_dir, metadata.get("specialist_manifest"))
    manifest = _load_json(manifest_path)
    if result.get("schema_version") == 3 and manifest.get("schema_version") == 2:
        raise SystemExit("current-review-specialist-manifest-schema")
    routing = _load_json(out_dir / "review-routing.json")
    if manifest.get("sol_selection") != routing.get("sol_selection"):
        raise SystemExit("manifest-sol-selection-routing-mismatch")
    passes = _manifest_passes(manifest)
    retained_role_cards = result.get("schema_version") == 3 and result_path.name == "result.json"
    runtime_summary: dict[str, Any] = {}
    by_role = _validate_manifest_entries(
        out_dir,
        manifest,
        passes,
        triggered_roles,
        env.codex_home,
        env.parent_thread_id,
        env.project_root,
        require_assessment=result.get("schema_version") != 2,
        retained_role_cards=retained_role_cards,
        require_role_card_receipts=result.get("schema_version") == 3,
        runtime_summary=runtime_summary,
    )
    runtime_summary = (
        runtime_summary
        if manifest.get("schema_version") in {7, 8, 9}
        else _validate_review_runtime(
            out_dir,
            manifest,
            passes,
            env.codex_home,
            env.parent_thread_id,
            require_assessment=result.get("schema_version") != 2,
            roles_dir=out_dir / "role-cards" if retained_role_cards else PLUGIN_ROOT / "roles",
        )
        if manifest.get("schema_version") in {3, 4, 5, 6, 7, 8}
        else {}
    )
    _validate_runtime_summary_metadata(metadata, manifest, runtime_summary)
    if manifest.get("schema_version") == 9:
        _validate_consolidation_confidence(out_dir, manifest, result, metadata)
    return _SpecialistEvidence(
        triggered_roles=triggered_roles,
        manifest=manifest,
        routing=routing,
        passes=passes,
        by_role=by_role,
        runtime_summary=runtime_summary,
    )


def _validate_specialist_pass_metadata(metadata: dict[str, Any], by_role: dict[str, Any]) -> None:
    """Reconcile the declared specialist passes against the manifest entries actually validated."""
    metadata_passes = metadata.get("specialist_passes")
    if not isinstance(metadata_passes, list):
        raise SystemExit("metadata-missing-specialist-passes")
    metadata_by_role = {}
    for index, item in enumerate(metadata_passes):
        if not isinstance(item, dict):
            raise SystemExit(f"metadata-specialist-pass-not-object:{index}")
        role = item.get("role")
        if not isinstance(role, str):
            raise SystemExit(f"metadata-specialist-pass-missing-role:{index}")
        if role in metadata_by_role:
            raise SystemExit(f"metadata-specialist-pass-duplicate-role:{role}")
        metadata_by_role[role] = item
    if set(metadata_by_role) != set(by_role):
        raise SystemExit("metadata-specialist-pass-role-mismatch")
    for role, item in by_role.items():
        metadata_item = metadata_by_role[role]
        for key in (
            "axis",
            "trigger",
            "mode",
            "role_card_sha256",
            "output_path",
            "confidence",
            "blocking_findings",
            "attempts",
            "selected_attempt",
        ):
            if metadata_item.get(key) != item.get(key):
                raise SystemExit(f"metadata-specialist-pass-mismatch:{role}:{key}")
        for parts_key in ("final_parts", "source_parts"):
            if parts_key in item and metadata_item.get(parts_key) != item[parts_key]:
                raise SystemExit(f"metadata-specialist-pass-mismatch:{role}:{parts_key}")


def _validate_inspection_independence(
    out_dir: Path,
    evidence: _SpecialistEvidence,
    metadata: dict[str, Any],
    status: str,
    env: _ReviewEnvironment,
) -> tuple[bool, bool]:
    """Check the schema-5 inspection plan's independence requirement against recorded evidence."""
    triggered_required = REQUIRED_ROLES & evidence.triggered_roles
    plan_dir = out_dir
    plan_manifest = evidence.manifest
    if plan_manifest.get("schema_version") == 9:
        independence_required = evidence.runtime_summary["independence_required"]
        requirement_evidence = evidence.runtime_summary["independence_requirement_evidence"]
    elif plan_manifest.get("manifest_kind") == "batched-review":
        plan_dir = out_dir / "batches" / "interactions"
        plan_manifest = _load_json(plan_dir / "specialist-manifest.json")
    if plan_manifest.get("schema_version") != 9:
        _, _, independence_required, requirement_evidence = _validate_inspection_plan(
            plan_dir, plan_manifest, env.parent_thread_id
        )
    routing_requirement = evidence.routing.get("independent_review_required")
    if routing_requirement is not independence_required:
        raise SystemExit("routing-inspection-independence-required-mismatch")
    if evidence.routing.get("independence_requirement_evidence") != requirement_evidence:
        raise SystemExit("routing-inspection-independence-evidence-mismatch")
    required_independent = bool(evidence.runtime_summary.get("independence_satisfied"))
    if metadata.get("independence_requirement_evidence") != requirement_evidence:
        raise SystemExit("metadata-independence-requirement-evidence-mismatch")
    if independence_required and not required_independent:
        boundary = "pass" if status == "pass" else "completion"
        raise SystemExit(f"independent-review-required-for-{boundary}:" + ",".join(sorted(triggered_required)))
    return independence_required, required_independent


def _validate_legacy_independence(
    evidence: _SpecialistEvidence,
    status: str,
    risk_tier: str,
) -> tuple[bool, bool]:
    """Derive the pre-schema-5 independence requirement from the triggered required roles."""
    triggered_required = REQUIRED_ROLES & evidence.triggered_roles
    substituted_roles = sorted(role for role in triggered_required if evidence.by_role[role]["mode"] == "substituted")
    independence_required = bool(triggered_required)
    required_independent = independence_required and all(
        evidence.by_role[role]["mode"] in {"spawned", "app-server"} for role in triggered_required
    )
    if risk_tier in INDEPENDENT_PASS_TIERS and status == "pass" and not required_independent:
        raise SystemExit("independent-review-required-for-pass:" + ",".join(substituted_roles))
    return independence_required, required_independent


def _validate_independence_requirement(
    out_dir: Path,
    evidence: _SpecialistEvidence,
    metadata: dict[str, Any],
    status: str,
    risk_tier: str,
    env: _ReviewEnvironment,
) -> None:
    """Check the review's independence requirement and the metadata that claims it was satisfied."""
    if evidence.manifest.get("schema_version") in {5, 6, 7, 8, 9}:
        independence_required, required_independent = _validate_inspection_independence(
            out_dir, evidence, metadata, status, env
        )
    else:
        independence_required, required_independent = _validate_legacy_independence(evidence, status, risk_tier)
    native_wave = evidence.manifest
    if evidence.manifest.get("manifest_kind") == "batched-review" and evidence.manifest.get("schema_version") != 9:
        native_wave = _load_json(out_dir / "batches/interactions/specialist-manifest.json")
    if (
        evidence.manifest.get("schema_version") in {3, 4, 5, 6, 7, 8, 9}
        and status == "pass"
        and len(evidence.triggered_roles) >= 2
        and (
            (
                evidence.runtime_summary.get("actual_mode") != "parallel"
                and not (
                    evidence.manifest.get("schema_version") in {7, 8}
                    and evidence.runtime_summary.get("actual_mode") == "independent-spawned"
                    and evidence.runtime_summary.get("capacity_limited") is True
                )
                and not _native_independent_wave(native_wave, evidence.runtime_summary)
                and not (
                    evidence.manifest.get("schema_version") == 9
                    and (
                        evidence.runtime_summary.get("final_union") is True
                        or evidence.runtime_summary.get("source_union") is True
                    )
                )
            )
            or any(item["mode"] not in {"inspection", "spawned", "app-server"} for item in evidence.passes)
        )
    ):
        raise SystemExit("parallel-review-required-for-pass:" + ",".join(sorted(evidence.triggered_roles)))

    fanout_substituted = any(item["mode"] == "substituted" for item in evidence.passes)
    if metadata.get("fanout_substituted") is not fanout_substituted:
        raise SystemExit("metadata-fanout-substituted-mismatch")

    independence_satisfied = bool(required_independent)
    if metadata.get("independence_satisfied") is not independence_satisfied:
        raise SystemExit("metadata-independence-satisfied-mismatch")
    if metadata.get("independence_required") is not independence_required:
        raise SystemExit("metadata-independence-required-mismatch")


def _batch_provenance_header(out_dir: Path, manifest: dict[str, Any], item: dict[str, Any]) -> str | None:
    """Derive an optional current native marker from frozen delivery, never from the response's claims."""
    if (
        manifest.get("schema_version") != 8
        or manifest.get("manifest_kind") != "native-wave"
        or manifest.get("reviewer_findings_version") != 1
        or manifest.get("dispatch_protocol") not in {"paged-context-v7", "paged-context-v8"}
        or manifest.get("context_reader_sha256") != _sha256(SKILL_DIRECTORY / "review_context.py")
    ):
        return None
    plan_path = _resolve_path(out_dir, manifest["inspection_execution"]["plan_path"])
    plan = _load_json(plan_path)
    attempt = item["attempts"][item["selected_attempt"] - 1]
    entries = [entry for entry in plan["contexts"] if entry["role_id"] == item["role"]]
    if (
        _sha256(plan_path) != manifest["inspection_execution"]["plan_sha256"]
        or any(plan[key] != manifest[key] for key in ("review_run_id", "review_input_sha256", "parent_thread_id"))
        or len(entries) != 1
        or attempt["attempt"] != item["selected_attempt"]
        or entries[0]["context_sha256"] != attempt["context_sha256"]
        or _resolve_path(out_dir, attempt["context_path"]) != _resolve_path(out_dir, entries[0]["context_path"])
        or _sha256(_resolve_path(out_dir, entries[0]["context_path"])) != entries[0]["context_sha256"]
    ):
        raise SystemExit(f"review-batch-provenance-identity-invalid:{item['role']}")
    return render_read_output(
        "",
        item["role"],
        plan["review_run_id"],
        plan["review_input_sha256"],
        attempt["context_sha256"],
        attempt["attempt"],
    ).splitlines()[0]


def _validate_batch_reconciliation(
    out_dir: Path,
    manifest: dict[str, Any],
    groups: dict[str, Any],
    actions: dict[str, list[dict[str, Any]]],
    records: dict[str, Any],
) -> None:
    """Bind parent-authored same-cause reasons to every origin and the admitted current source snapshot.

    This validates preservation and source identity, not semantic equivalence. Parent source judgment and independent
    verification must establish that the canonical change and closure obligation cover each retained original.
    """
    error = "review-batch-source-reconciliation-invalid"
    if set(groups) - set(actions):
        raise SystemExit(error)
    execution = manifest["batch_execution"]
    inventory_path = _resolve_path(out_dir, execution["inventory_path"])
    if _sha256(inventory_path) != execution["inventory_sha256"]:
        raise SystemExit(error)
    import review_batches  # Reuse the verified circular boundary after ordinary constituent admission.

    try:
        inventory = review_batches.validate_inventory(out_dir)
    except ValueError as exc:
        raise SystemExit(error) from exc
    sources = {item["path"]: item for item in inventory["source_snapshot"]["files"]}
    for identity, group in groups.items():
        originals = actions[identity]
        # Identical originals retain the historical exact-canonical contract; only variants need reconciliation.
        payloads = {
            json.dumps({key: value for key, value in item["original"].items() if key != "id"}, sort_keys=True)
            for item in originals
        }
        if (
            not isinstance(group, dict)
            or set(group) != {"invariant", "rationale", "origins", "evidence"}
            or any(not isinstance(group[key], str) or not group[key].strip() for key in ("invariant", "rationale"))
            or not isinstance(group["origins"], dict)
            or set(group["origins"]) != {item["finding_id"] for item in originals}
            or any(not isinstance(reason, str) or not reason.strip() for reason in group["origins"].values())
            or len(originals) < 2
            or len(payloads) < 2
            or not isinstance(group["evidence"], list)
            or not group["evidence"]
        ):
            raise SystemExit(error)
        severity = min((item["original"]["severity"] for item in originals), key=FINDING_SEVERITIES.index)
        if records[identity].get("severity") != severity:
            raise SystemExit(error)
        for witness in group["evidence"]:
            if not isinstance(witness, dict) or set(witness) != {"path", "start_line", "end_line", "source_sha256"}:
                raise SystemExit(error)
            source = sources.get(witness["path"]) if isinstance(witness["path"], str) else None
            if (
                source is None
                or source.get("encoding") != "utf-8"
                or source["sha256"] != witness["source_sha256"]
                or hashlib.sha256(source["content"].encode("utf-8")).hexdigest() != source["sha256"]
                or type(witness["start_line"]) is not int
                or type(witness["end_line"]) is not int
                or not 1 <= witness["start_line"] <= witness["end_line"] <= len(source["content"].splitlines())
            ):
                raise SystemExit(error)


def _validate_batch_source_findings(out_dir: Path, result: dict[str, Any], manifest: dict[str, Any]) -> None:
    """Retain unresolved findings from every admitted batch phase and forbid false canonical approval.

    The serialized source_findings field retains individual original obligations from source, intermediate interaction,
    and final waves. Every unresolved individual requires one canonical action and retained original obligations and
    evidence. Shared actions require exact duplicate accounting or complete parent reconciliation bound to admitted
    source. Neither path changes an original obligation or establishes closure merely because a canonical action exists.
    """
    if manifest.get("manifest_kind") != "batched-review":
        return
    ledger = manifest["source_findings"]
    metadata = result["metadata"]
    reconciliations = metadata.get("source_finding_reconciliation", {})
    if not isinstance(reconciliations, dict):
        raise SystemExit("review-batch-source-reconciliation-invalid")
    if metadata.get("source_findings") != ledger:
        raise SystemExit("review-batch-result-source-findings-mismatch")
    unresolved = [item for item in ledger if item["disposition"] == "unresolved"]
    if not unresolved:
        if reconciliations:
            raise SystemExit("review-batch-source-reconciliation-invalid")
        return
    blocking = any(item["original"]["severity"] != "low" for item in unresolved)
    if (blocking and result.get("status") == "pass") or metadata.get("review_decision", {}).get(
        "recommendation"
    ) == "accept-as-is":
        raise SystemExit("review-batch-source-findings-unresolved")
    records = {item.get("id"): item for item in metadata.get("review_findings", [])}
    mapping = metadata.get("source_finding_mapping")
    if not isinstance(mapping, dict) or set(mapping) != {item["finding_id"] for item in unresolved}:
        raise SystemExit("review-batch-result-source-finding-inventory-dropped")
    actions: dict[str, list[dict[str, Any]]] = {}
    for item in unresolved:
        identities = mapping[item["finding_id"]]
        if not isinstance(identities, list) or len(identities) != 1 or not isinstance(identities[0], str):
            raise SystemExit("review-batch-result-source-findings-dropped")
        identity = identities[0]
        finding = records.get(identity)
        original = item["original"]
        if (
            not finding
            or item["manifest_path"] not in finding.get("evidence", [])
            or _readable_review_role(item["pass"]["role"]) not in finding.get("authors", [])
            or any(
                f"{entry['path']}:{entry['start_line']}-{entry['end_line']}" not in finding.get("evidence", [])
                for entry in original["evidence"]
            )
            or (
                identity not in reconciliations
                and any(
                    finding.get(key) != original[key]
                    for key in ("severity", "title", "summary", "required_change", "closure_evidence")
                )
            )
        ):
            raise SystemExit("review-batch-result-source-finding-inventory-dropped")
        actions.setdefault(identity, []).append(item)
    if reconciliations:
        _validate_batch_reconciliation(out_dir, manifest, reconciliations, actions, records)
    duplicates = {}
    for identity, originals in actions.items():
        originals.sort(key=lambda item: item["finding_id"])
        first_by_payload = {}
        for item in originals:
            payload = json.dumps({key: value for key, value in item["original"].items() if key != "id"}, sort_keys=True)
            if first_by_payload and payload not in first_by_payload and identity not in reconciliations:
                raise SystemExit("review-batch-distinct-findings-collapsed")
            if payload in first_by_payload:
                duplicates[item["finding_id"]] = first_by_payload[payload]
            else:
                first_by_payload[payload] = item["finding_id"]
    if metadata.get("source_finding_duplicates", {}) != duplicates:
        raise SystemExit("review-batch-duplicate-accounting-mismatch")


def _python_diff_paths(out_dir: Path) -> list[str]:
    """Return the collected changed paths the provider can map from the review's diff file to Python modules.

    Only ``files.txt`` names paths that ``diff.patch`` contains: untracked files are listed separately and never reach
    the provider. The provider maps only case-sensitive ``.py`` post-image paths, so stub-only or untracked-only Python
    changes do not require a probe whose answer could only be empty. Git C-quotes a non-ASCII path in this listing
    (``"pkg/mod\\303\\251.py"``), which would otherwise end in ``"`` and silently skip the required probe, so each line
    is unquoted the way the adapter reads the diff before Windows separators are normalized.

    Example:
        >>> import tempfile
        >>> listing = 'a.pyi\\nsrc\\\\pkg\\\\mod.py\\n"pkg/mod\\\\303\\\\251.py"\\n'
        >>> with tempfile.TemporaryDirectory() as directory:
        ...     _ = (Path(directory) / "files.txt").write_text(listing, encoding="utf-8")
        ...     _python_diff_paths(Path(directory))
        ['src/pkg/mod.py', 'pkg/modé.py']
    """
    listing = out_dir / "files.txt"
    if not listing.is_file():
        return []
    paths = (
        codemap_unquote_git_path(raw_path.strip()).replace("\\", "/")
        for raw_path in listing.read_text(encoding="utf-8").splitlines()
    )
    return [path for path in paths if path.endswith(CODEMAP_PYTHON_SUFFIX)]


def _load_codemap_artifact(path: Path, code: str, *, historical: bool = False) -> dict[str, Any]:
    """Load one persisted Codemap artifact and require the fields that make its status auditable.

    Every adapter artifact names its protocol, one status from the closed vocabulary, and a non-empty probe detail. The
    detail is the recorded reason an ``absent`` or ``incompatible`` provider was accepted as a non-fatal fallback, so an
    artifact without it cannot show why the review proceeded without structural evidence. The older schema, which kept
    no answer and no gap reasons, is accepted only for a recovered historical run.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise SystemExit(f"{code}:unreadable-json") from None
    if not isinstance(payload, dict):
        raise SystemExit(f"{code}:not-json-object")
    if payload.get("protocol_version") != CODEMAP_PROTOCOL_VERSION:
        raise SystemExit(f"{code}:protocol_version")
    schema_version = payload.get("artifact_schema_version")
    if schema_version != CODEMAP_ARTIFACT_SCHEMA_VERSION and not (
        historical and schema_version in CODEMAP_RECOVERED_ARTIFACT_SCHEMAS
    ):
        raise SystemExit(f"{code}:artifact_schema_version")
    if payload.get("status") not in CODEMAP_STATUSES:
        raise SystemExit(f"{code}:status")
    probe = payload.get("probe")
    detail = probe.get("detail") if isinstance(probe, dict) else None
    if not isinstance(detail, str) or not detail.strip():
        raise SystemExit(f"{code}:probe.detail")
    reasons = payload.get("status_reasons")
    if schema_version == CODEMAP_ARTIFACT_SCHEMA_VERSION and (
        not isinstance(reasons, list) or not all(isinstance(item, str) and item.strip() for item in reasons)
    ):
        raise SystemExit(f"{code}:status_reasons")
    return payload


def _recorded_codemap_outcomes(payload: dict[str, Any], code: str) -> list[Any]:
    """Rebuild a current artifact's query records, failing closed on any malformed record."""
    queries = payload.get("queries")
    if not isinstance(queries, list):
        raise SystemExit(f"{code}:queries")
    try:
        return [CodemapQueryOutcome.from_dict(record) for record in queries]
    except ValueError:
        raise SystemExit(f"{code}:queries") from None


def _validate_codemap_recorded_status(payload: dict[str, Any], outcomes: list[Any], code: str, diff_text: str) -> None:
    """Require the recorded gap, status, and reasons to be exactly what the recorded query outcomes imply.

    The validator re-derives them with the adapter's own reduction rather than trusting the artifact, so a hand-written
    ``available`` over a mismatched, all-unmapped, or deletion-only answer fails the same way a skipped probe does.
    ``diff_text`` is the change set a ``diff-impact`` query read, from which deleted Python modules are recounted.

    Each query's ``input_rejected`` flag is re-derived first, from its exit code and recorded answer: the status
    reduction reads that flag to keep a batch with no successful query ``degraded`` instead of ``incompatible``, so a
    flag set on a provider-side failure would otherwise pass as a typo and admit follow-ups after an unusable probe.
    """
    if any(
        outcome.input_rejected != codemap_provider_rejected_input(outcome.exit_code, outcome.answer)
        for outcome in outcomes
    ):
        raise SystemExit(f"{code}:input_rejected")
    if any(
        outcome.adapter_gap != codemap_change_set_gap(outcome.subcommand, outcome.answer, diff_text)
        for outcome in outcomes
    ):
        raise SystemExit(f"{code}:answer")
    if payload["status"] != codemap_reduce_status(payload["probe"].get("status"), outcomes):
        raise SystemExit(f"{code}:status")
    if payload["status_reasons"] != list(codemap_gap_reasons(outcomes)):
        raise SystemExit(f"{code}:status_reasons")


def _codemap_run_diff_text(diff_file: object, out_dir: Path) -> str | None:
    """Return this run's ``diff.patch`` text when the recorded diff file is exactly that file, else ``None``.

    Any other file inside the run, such as ``files.txt``, parses as a diff with no Python change, so binding the probe
    to the run's own diff is what makes an empty answer mean an empty change set.
    """
    if not isinstance(diff_file, str) or not diff_file.strip() or not Path(diff_file).is_absolute():
        return None
    run_diff = out_dir / "diff.patch"
    if Path(diff_file).resolve() != run_diff.resolve() or not run_diff.is_file():
        return None
    return codemap_read_diff_text(run_diff)


def _validate_codemap_primary_review(context: dict[str, Any], out_dir: Path, python_paths: list[str]) -> None:
    """Bind a current primary artifact to one standard ``diff-impact`` query over this run's own diff file.

    A Python diff whose usable probe read zero changed Python files must also name why in ``status_reasons``: the
    provider reads only post-images, so a deleted, renamed, or hunk-free Python change otherwise looks like an empty,
    complete, low-risk answer.
    """
    code = "codemap-context-invalid"
    if context.get("query_kind") != "standard":
        raise SystemExit(f"{code}:query_kind")
    outcomes = _recorded_codemap_outcomes(context, code)
    provider_usable = context["probe"].get("status") == CODEMAP_STATUS_AVAILABLE
    if [outcome.subcommand for outcome in outcomes] != (["diff-impact"] if provider_usable else []):
        raise SystemExit(f"{code}:queries")
    diff_text = _codemap_run_diff_text(context.get("diff_file"), out_dir) if outcomes else ""
    if diff_text is None:
        # A probe a non-Python review volunteered is not required, but once run it must still read this run's diff.
        raise SystemExit(f"{code}:diff_file" if python_paths else f"{code}:diff_file:volunteered-probe")
    _validate_codemap_recorded_status(context, outcomes, code, diff_text)
    if python_paths and any(outcome.changed_files == 0 for outcome in outcomes) and not context["status_reasons"]:
        raise SystemExit(f"{code}:changed_files")


def _triggered_review_roles(out_dir: Path) -> set[str]:
    """Return the specialist roles this review's routing triggered, or none when routing is unreadable."""
    try:
        routing = json.loads((out_dir / "review-routing.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return set()
    roles = routing.get("triggered_roles") if isinstance(routing, dict) else None
    return {role for role in roles if isinstance(role, str)} if isinstance(roles, list) else set()


def _validate_codemap_follow_up(path: Path, name: str, context: dict[str, Any]) -> None:
    """Check one follow-up artifact: its route, target, category, and a status its own query outcome implies."""
    code = f"codemap-followup-invalid:{name}"
    if path.is_symlink() or not path.is_file():
        raise SystemExit(f"{code}:not-a-file")
    follow_up = _load_codemap_artifact(path, code)
    kind = follow_up.get("query_kind")
    if kind not in FOLLOW_UP_QUERY_KINDS:
        raise SystemExit(f"{code}:query_kind")
    if follow_up.get("category") != context.get("category"):
        raise SystemExit(f"{code}:category")
    target = follow_up.get("target")
    if not isinstance(target, str) or not target.strip():
        raise SystemExit(f"{code}:target")
    outcomes = _recorded_codemap_outcomes(follow_up, code)
    provider_usable = follow_up["probe"].get("status") == CODEMAP_STATUS_AVAILABLE
    if [outcome.subcommand for outcome in outcomes] != (
        [CODEMAP_FOLLOW_UP_SUBCOMMANDS[kind]] if provider_usable else []
    ):
        raise SystemExit(f"{code}:queries")
    _validate_codemap_recorded_status(follow_up, outcomes, code, "")


def _validate_codemap_follow_ups(directory: Path, context: dict[str, Any], out_dir: Path) -> None:
    """Keep every specialist follow-up within a triggered role, its per-role limit, and the provider precondition.

    A follow-up only answers a fact the persisted probe left open, so it needs a probe whose provider was usable: after
    ``absent`` or ``incompatible`` the contract forbids retrying the adapter in the same run, and ``skipped`` recorded a
    deliberate zero-query decision. Each name binds to one role this review's routing triggered, and its two-digit
    sequence bounds that role's query count, so renaming cannot buy extra queries.
    """
    if not directory.exists() and not directory.is_symlink():
        return
    if directory.is_symlink() or not directory.is_dir():
        raise SystemExit("codemap-followup-directory-invalid")
    names = sorted(path.name for path in directory.iterdir())
    if names and context["status"] not in FOLLOW_UP_ELIGIBLE_STATUSES:
        raise SystemExit(f"codemap-followup-after-unusable-probe:{context['status']}")
    roles = _triggered_review_roles(out_dir)
    for name in names:
        match = CODEMAP_FOLLOW_UP_NAME.match(name)
        if match is None or not 1 <= int(match["sequence"]) <= FOLLOW_UP_QUERY_LIMIT:
            raise SystemExit(f"codemap-followup-name-invalid:{name}")
        if match["role"] not in roles:
            raise SystemExit(f"codemap-followup-role-unknown:{name}")
        _validate_codemap_follow_up(directory / name, name, context)


def _recovered_historical_run(out_dir: Path) -> bool:
    """Report whether this run carries the native-provenance recovery helper's marker and candidate manifest."""
    try:
        marker = json.loads((out_dir / CODEMAP_RECOVERY_MARKER).read_text(encoding="utf-8"))
        candidate = json.loads((out_dir / CODEMAP_RECOVERY_CANDIDATE).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return (
        isinstance(marker, dict)
        and isinstance(candidate, dict)
        and candidate.get("schema_version") in CODEMAP_RECOVERY_SCHEMA_VERSIONS
        and all(candidate.get(key) == value for key, value in CODEMAP_RECOVERY_IDENTITY.items())
    )


def _historical_codemap_exemption(out_dir: Path, metadata: dict[str, Any], artifact_present: bool) -> bool:
    """Return whether the result validly records a recovered pre-rule run as exempt from the required probe.

    The exemption is never inferred. The result must name it explicitly, the run must carry the recovery helper's marker
    and candidate manifest, and no probe artifact may exist that the claim would contradict.
    """
    claim = metadata.get("codemap_context")
    if claim is None:
        return False
    if claim != CODEMAP_HISTORICAL_EXEMPTION:
        raise SystemExit("codemap-context-metadata-invalid")
    if artifact_present:
        raise SystemExit("codemap-context-historical-exemption-contradicted")
    if not _recovered_historical_run(out_dir):
        raise SystemExit("codemap-context-historical-exemption-unproven")
    return True


def _validate_codemap_context(out_dir: Path, metadata: dict[str, Any]) -> None:
    """Require the persisted structural probe for a Python diff and bound every specialist follow-up.

    The provider stays optional: an ``absent`` or pre-query ``incompatible`` artifact passes because it records why the
    review fell back to bounded inspection. What fails closed is a Python diff with no artifact, a ``skipped`` one, or a
    current artifact whose recorded status, reasons, query, or diff file disagree with what actually ran. A recovered
    pre-rule run may instead record the explicit historical exemption.
    """
    python_paths = _python_diff_paths(out_dir)
    artifact = out_dir / CODEMAP_CONTEXT_ARTIFACT
    follow_ups = out_dir / CODEMAP_FOLLOW_UP_DIRECTORY
    artifact_present = artifact.exists() or artifact.is_symlink()
    if _historical_codemap_exemption(out_dir, metadata, artifact_present) or (
        not python_paths and not artifact_present and not follow_ups.exists()
    ):
        if follow_ups.exists():
            raise SystemExit("codemap-followup-without-context")
        return
    if artifact.is_symlink() or not artifact.is_file():
        raise SystemExit(
            "codemap-context-missing-for-python-diff" if python_paths else "codemap-followup-without-context"
        )
    context = _load_codemap_artifact(artifact, "codemap-context-invalid", historical=_recovered_historical_run(out_dir))
    if context.get("category") != "review":
        raise SystemExit("codemap-context-invalid:category")
    if python_paths and context["status"] == CODEMAP_STATUS_SKIPPED:
        raise SystemExit("codemap-context-skipped-for-python-diff")
    if context["artifact_schema_version"] == CODEMAP_ARTIFACT_SCHEMA_VERSION:
        _validate_codemap_primary_review(context, out_dir, python_paths)
    _validate_codemap_follow_ups(follow_ups, context, out_dir)


def _is_fresh_candidate(out_dir: Path, result_path: Path) -> bool:
    """Report whether validation targets a fresh candidate rather than the run's promoted canonical result.

    Requirements added after a result was promoted apply to fresh candidates only, so historical canonical results stay
    readable while every new run must satisfy them before promotion.
    """
    return result_path.name != "result.json" or result_path.parent.resolve() != out_dir.resolve()


class _ReviewStep(NamedTuple):
    """One named review check and the earlier steps whose success it needs."""

    name: str
    check: Any
    requires: tuple[str, ...] = ()


def _result_context(result_path: Path) -> dict[str, Any]:
    """Load the candidate and check the shape fields every later review step reads."""
    result = _load_json(result_path)
    status = result.get("status")
    if status not in {"pass", "fail", "timeout"}:
        raise SystemExit(f"invalid-status:{status!r}")

    metadata = result.get("metadata")
    if not isinstance(metadata, dict):
        raise SystemExit("result-missing-metadata")

    scope = metadata.get("scope", metadata.get("review_scope"))
    if scope not in {"working-tree", "path", "commit", "pr"}:
        raise SystemExit(f"invalid-review-scope:{scope!r}")

    risk_tier = metadata.get("risk_tier")
    if risk_tier not in {"TRIVIAL", "LOCAL", "BROAD", "HIGH_RISK"}:
        raise SystemExit(f"invalid-risk-tier:{risk_tier!r}")

    review_status = metadata.get("review_status")
    if review_status not in {None, "unavailable", "closed"}:
        raise SystemExit(f"invalid-review-status:{review_status!r}")
    return {
        "result": result,
        "status": status,
        "metadata": metadata,
        "scope": scope,
        "risk_tier": risk_tier,
        "review_status": review_status,
    }


def _terminal_review_steps(out_dir: Path, result_path: Path, context: dict[str, Any]) -> list[_ReviewStep]:
    """Check terminal reviews, requiring deduction text before candidate promotion."""
    result, metadata, scope = context["result"], context["metadata"], context["scope"]

    def terminal() -> None:
        """Select candidate requirements without changing canonical historical readers."""
        if context["review_status"] == "unavailable":
            _validate_unavailable_result(
                out_dir,
                result,
                metadata,
                scope,
                require_deductions=_is_fresh_candidate(out_dir, result_path),
            )
        else:
            _validate_closed_result(out_dir, result, metadata, scope)

    return [
        _ReviewStep(str(context["review_status"]), terminal),
        _ReviewStep("confidence-gaps", lambda: _validate_confidence_gaps(result, metadata)),
        _ReviewStep("confidence-recovery", lambda: _validate_confidence_recovery(result, metadata)),
    ]


def _assessed_review_steps(
    out_dir: Path, result_path: Path, context: dict[str, Any], env: _ReviewEnvironment
) -> list[_ReviewStep]:
    """List the checks for an assessed review in fail-fast order with their dependencies."""
    result, metadata, scope = context["result"], context["metadata"], context["scope"]
    risk_tier, status = context["risk_tier"], context["status"]
    notes_path = out_dir / "review-notes.md"
    state: dict[str, Any] = {}

    def manifest() -> None:
        state["evidence"] = _validate_specialist_manifest(out_dir, result, result_path, metadata, risk_tier, env)

    def assessments() -> None:
        if result.get("schema_version") == 3 or metadata.get("reviewer_assessments") is not None:
            manifest = state["evidence"].manifest
            _validate_reviewer_assessments(
                out_dir,
                metadata,
                state["evidence"].by_role,
                batch_response=manifest.get("schema_version") in {7, 8, 9}
                and (
                    manifest.get("manifest_kind") == "batched-review" or manifest.get("reviewer_findings_version") == 1
                ),
            )

    notes, evidence = ("notes-sections",), ("specialist-manifest",)
    steps = [
        _ReviewStep("notes-sections", lambda: _require_notes_sections(notes_path)),
        _ReviewStep("review-decision", lambda: _validate_review_decision(metadata, result)),
        _ReviewStep("action-table", lambda: _validate_action_table(notes_path, result, metadata, scope), notes),
        _ReviewStep("confidence-gaps", lambda: _validate_confidence_gaps(result, metadata)),
        _ReviewStep("confidence-recovery", lambda: _validate_confidence_recovery(result, metadata)),
    ]
    if scope == "pr":
        steps.append(
            _ReviewStep("pr-scope", lambda: _validate_pr_review_scope(out_dir, result, metadata, notes_path), notes)
        )
    if _is_fresh_candidate(out_dir, result_path):
        # Promoted results predating the required probe stay readable; every new candidate must carry it.
        steps.append(_ReviewStep("codemap-context", lambda: _validate_codemap_context(out_dir, metadata)))
    steps.extend(
        (
            _ReviewStep("specialist-manifest", manifest),
            _ReviewStep(
                "specialist-passes",
                lambda: _validate_specialist_pass_metadata(metadata, state["evidence"].by_role),
                evidence,
            ),
            _ReviewStep(
                "batch-findings",
                lambda: _validate_batch_source_findings(out_dir, result, state["evidence"].manifest),
                evidence,
            ),
            _ReviewStep("reviewer-assessments", assessments, evidence),
            _ReviewStep(
                "independence",
                lambda: _validate_independence_requirement(
                    out_dir, state["evidence"], metadata, status, risk_tier, env
                ),
                evidence,
            ),
        )
    )
    return steps


def _review_steps(
    out_dir: Path, result_path: Path, context: dict[str, Any], env: _ReviewEnvironment
) -> list[_ReviewStep]:
    """Select the terminal or assessed review check list for one loaded candidate."""
    if context["review_status"] is not None:
        return _terminal_review_steps(out_dir, result_path, context)
    return _assessed_review_steps(out_dir, result_path, context, env)


def _validate_result(
    out_dir: Path,
    result_path: Path,
    codex_home: Path,
    parent_thread_id: str,
    project_root: Path,
) -> None:
    """Validate a complete review decision against routing, independent evidence, and gates."""
    env = _ReviewEnvironment(codex_home=codex_home, parent_thread_id=parent_thread_id, project_root=project_root)
    context = _result_context(result_path)
    for step in _review_steps(out_dir, result_path, context, env):
        step.check()


def collect_result_errors(
    out_dir: Path,
    result_path: Path,
    codex_home: Path,
    parent_thread_id: str,
    project_root: Path,
) -> dict[str, Any]:
    """Run every review check once and report each failure; dependent checks are reported as not run.

    An unexpected exception inside a check counts as that check's failure, because malformed input from another reported
    error can reach it. Any failure or not-run check makes the report fail.
    """
    env = _ReviewEnvironment(codex_home=codex_home, parent_thread_id=parent_thread_id, project_root=project_root)
    try:
        context = _result_context(result_path)
    except SystemExit as error:
        errors = [{"step": "result-shape", "code": str(error.code)}]
        return {
            "status": "fail",
            "errors": errors,
            "not_run": [{"step": "review-checks", "blocked_by": "result-shape"}],
        }
    errors: list[dict[str, str]] = []
    not_run: list[dict[str, str]] = []
    failed: set[str] = set()
    for step in _review_steps(out_dir, result_path, context, env):
        blocked = [name for name in step.requires if name in failed]
        if blocked:
            failed.add(step.name)
            not_run.append({"step": step.name, "blocked_by": ",".join(blocked)})
            continue
        try:
            step.check()
        except SystemExit as error:
            failed.add(step.name)
            errors.append({"step": step.name, "code": str(error.code)})
        except (KeyError, TypeError, AttributeError, ValueError, OSError, IndexError) as error:
            failed.add(step.name)
            errors.append({"step": step.name, "code": f"{step.name}-unchecked:{type(error).__name__}"})
    return {"status": "fail" if errors or not_run else "pass", "errors": errors, "not_run": not_run}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path, help="Review output directory.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--result", type=Path, help="Candidate result.json path.")
    source.add_argument(
        "--manifest-only",
        action="store_true",
        help="Validate specialist routing and manifest provenance before candidate creation.",
    )
    parser.add_argument(
        "--challenge-only",
        action="store_true",
        help="With --manifest-only, validate exactly one independent challenger without Code Review routing.",
    )
    # Resolve the fallback lazily: an argparse default is built even when CODEX_HOME is set, and
    # Path.home() raises on any host that exposes no home variable the platform recognizes.
    codex_home = os.environ.get("CODEX_HOME")
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=Path(codex_home) if codex_home else Path.home() / ".codex",
        help="Codex home containing rollout session logs.",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path.cwd(),
        help="Accepted for CLI compatibility; role policy uses installed role cards.",
    )
    parser.add_argument(
        "--parent-thread-id",
        default=os.environ.get("CODEX_THREAD_ID", ""),
        help="Current parent Codex thread ID.",
    )
    parser.add_argument(
        "--all-errors",
        action="store_true",
        help="With --result, run every review check once and print JSON listing each failure; exit 1 on any failure.",
    )
    args = parser.parse_args()

    if args.all_errors and args.manifest_only:
        parser.error("--all-errors requires --result")
    if args.challenge_only and not args.manifest_only:
        parser.error("--challenge-only requires --manifest-only")
    if not args.parent_thread_id:
        raise SystemExit("missing-parent-thread-id")
    if args.manifest_only:
        if args.challenge_only:
            _validate_challenge_manifest_preflight(args.out, args.codex_home, args.parent_thread_id, args.project_root)
        else:
            _validate_manifest_preflight(args.out, args.codex_home, args.parent_thread_id, args.project_root)
        return 0
    if args.all_errors:
        report = collect_result_errors(args.out, args.result, args.codex_home, args.parent_thread_id, args.project_root)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report["status"] == "pass" else 1
    _validate_result(args.out, args.result, args.codex_home, args.parent_thread_id, args.project_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
