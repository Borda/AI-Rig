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
command-line compatibility, but role policy comes only from installed role cards.

## Used by

the ``code-review`` skill's terminal validation gate and review-artifact contract tests. Maintainers can also run it
while diagnosing an incomplete artifact, but it is not a replacement for collecting the diff, remote review data, or
specialist analysis.

## Outputs

accepts a coherent review artifact or emits an explicit contract failure for missing routing, source evidence, close
evidence, decision rationale, or action-table cells. Successful validation returns a zero exit status, while failures
identify the violated contract so the workflow can stop before presenting a merge recommendation.

## Failure

untriaged specialist output, unsupported recommendation, inconsistent PR evidence, an invalid close disposition, or a
non-accept decision without merge blocks exits non-zero. It also rejects artifacts that reference files outside the
review output directory or claim a terminal result while retaining forbidden detailed-review artifacts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any


# Keep the installed skill helper importable when pytest loads this validator by file path.
SKILL_DIRECTORY = Path(__file__).resolve().parent
PLUGIN_ROOT = SKILL_DIRECTORY.parents[1]
if str(SKILL_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SKILL_DIRECTORY))
SHARED_DIRECTORY = PLUGIN_ROOT / "shared"
if str(SHARED_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SHARED_DIRECTORY))

from parallel_execution import _SECRET_PATTERNS, validate_inspection_contexts, validate_read_only_runtime  # noqa: E402
from app_server_review import ReviewRouteError, validate_evidence as validate_app_server_evidence  # noqa: E402
from review_routing import derive_mechanical_risk  # noqa: E402

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
REQUIRED_ROLES = {"qa-specialist", "challenger"}
VALID_RECOMMENDATIONS = {"accept-as-is", "minor-changes", "needs-more-work", "reject", "not-aligned"}
FINDING_SEVERITIES = ("critical", "high", "medium", "low")
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
ACTION_TABLE_SECTION = "Review Findings and Merge Blocks"
ACTION_TABLE_HEADERS = ("Finding / area", "Required change", "Evidence", "Status")
ALL_MANIFEST_ROLES = {
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
INDEPENDENT_PASS_TIERS = {"BROAD", "HIGH_RISK"}
VALID_MODES = {"spawned", "substituted", "app-server", "inspection"}
TRANSIENT_RETRY_ERRORS = {"rate_limited", "timeout", "transport_error"}
SOL_ROLES = {"solution-architect", "security-auditor"}
UNAVAILABLE_NOTE_LINES = (
    "PR Review Availability: unavailable",
    "Source findings: not assessed",
    "Merge decision: not made",
)
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
UNAVAILABLE_FORBIDDEN_ARTIFACTS = {"local-checkout.json", "specialist-manifest.json"}
UNAVAILABLE_CONFIDENCE_GAP = (
    "Core PR source verification did not complete; no source review or merge decision was made."
)
CLOSED_CONFIDENCE_GAP = "Detailed source review was intentionally skipped after the close gate."
CLOSED_RESULT_KEYS = UNAVAILABLE_RESULT_KEYS
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
CLOSED_FORBIDDEN_ARTIFACTS = {"codemap-context.json", "review-routing.json", "specialist-manifest.json", "specialists"}
PR_THREAD_CONFIDENCE_GAP = "PR review-thread resolution status was unavailable; online review triage may be incomplete."
PR_PUBLIC_FALLBACK_MAX_CONFIDENCE = 0.89
UNAVAILABLE_RECOVERY_ACTIONS = {
    "retry": "Retry the unchanged collector later; no review or merge decision was made.",
    "auth": "Repair local gh access privately, verify repository access, then retry.",
    "install": "Install or repair gh locally, then retry.",
    "identity": "Confirm the canonical PR URL and repository identity, then retry.",
    "report": "Stop and report this Codex Rig collector failure with sanitized artifacts.",
}
CHECKOUT_STATE_RECOVERY_SUFFIX = " Inspect the local checkout state before retrying."
SAFE_DIAGNOSTIC_IDENTIFIER = re.compile(r"[a-z][a-z0-9-]*\Z")
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
}
UNAVAILABLE_GENERIC_SUMMARY = "Source collection stopped before I could verify which PR revision to review."


def _load_role_card(roles_dir: Path, role: str) -> dict[str, str]:
    """Load the flat installed role-card contract and bind it to its exact bytes."""
    path = roles_dir / role / "ROLE.md"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise SystemExit(f"role-card-missing:{role}") from error
    if not lines or lines[0] != "---":
        raise SystemExit(f"role-card-frontmatter-invalid:{role}")
    try:
        closing_index = lines.index("---", 1)
    except ValueError as error:
        raise SystemExit(f"role-card-frontmatter-invalid:{role}") from error
    fields: dict[str, str] = {}
    for line in lines[1:closing_index]:
        key, separator, value = line.partition(":")
        if not separator or not key or not value.strip() or key in fields:
            raise SystemExit(f"role-card-frontmatter-invalid:{role}")
        fields[key] = value.strip()
    required = ("role_id", "model", "model_reasoning_effort", "approval_policy", "sandbox_mode")
    if fields.get("role_id") != role or any(field not in fields for field in required):
        raise SystemExit(f"role-card-contract-invalid:{role}")
    return {
        "role_id": role,
        "role_card_sha256": _sha256(path),
        "model": fields["model"],
        "model_reasoning_effort": fields["model_reasoning_effort"],
        "approval_policy": fields["approval_policy"],
        "sandbox_mode": fields["sandbox_mode"],
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


ROUTING_SIGNALS = {
    "behavior_change",
    "bug_fix",
    "test_or_error_path",
    "data_tensor_boundary",
    "high_candidate",
    "unresolved_material_assumption",
    "material_no_finding",
    "explicit_adversarial",
    "axis_solution_architect",
    "axis_security_auditor",
    "axis_data_steward",
    "axis_cicd_steward",
    "axis_linting_expert",
    "axis_doc_scribe",
    "axis_oss_shepherd",
    "axis_squeezer",
    "axis_scientist",
    "axis_web_explorer",
}
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


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise SystemExit(f"expected JSON object: {path}")
    return payload


def _load_json_list(path: Path) -> list[Any]:
    """Load one required JSON array artifact."""
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, list):
        raise SystemExit(f"expected JSON array: {path}")
    return payload


def _resolve_path(out_dir: Path, raw_path: object) -> Path:
    """Resolve one declared review artifact path without consulting the caller's working directory.

    The recorded path was written by an earlier process whose directory is not stored alongside it, so probing the
    reader's own directory made a finished review valid in one place and invalid in another. Candidates are derived from
    `out_dir` — the current relative form first, then its ancestors for runs written before that convention — and the
    containment check below still rejects anything landing outside the review output.
    """
    if not isinstance(raw_path, str) or not raw_path:
        raise SystemExit("missing output path")
    declared = Path(raw_path)
    path = declared if declared.is_absolute() else out_dir / declared
    if not declared.is_absolute() and not path.is_file():
        for ancestor in out_dir.resolve().parents:
            if (ancestor / declared).is_file():
                path = ancestor / declared
                break
    resolved = path.resolve()
    if not resolved.is_relative_to(out_dir.resolve()):
        raise SystemExit(f"artifact-path-outside-review-output:{raw_path}")
    return resolved


def _sha256(path: Path) -> str:
    """Return the SHA-256 digest for an evidence file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read valid object rows from a Codex rollout log."""
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _find_rollout(codex_home: Path, thread_id: str) -> Path:
    """Find the unique rollout log for a Codex thread ID."""
    matches = list((codex_home / "sessions").rglob(f"*{thread_id}*.jsonl"))
    if len(matches) != 1:
        raise SystemExit(f"provenance-rollout-count:{thread_id}:{len(matches)}")
    return matches[0]


def _event_payloads(rows: list[dict[str, Any]], event_type: str) -> list[dict[str, Any]]:
    """Select event-message payloads of one type."""
    payloads = [
        row["payload"]
        for row in rows
        if row.get("type") == "event_msg"
        and isinstance(row.get("payload"), dict)
        and row["payload"].get("type") == event_type
    ]
    if event_type != "sub_agent_activity":
        return payloads
    for row in rows:
        payload = row.get("payload")
        if row.get("type") != "event_msg" or not isinstance(payload, dict):
            continue
        item = payload.get("item")
        if payload.get("type") != "item_completed" or not isinstance(item, dict):
            continue
        if item.get("type") != "SubAgentActivity":
            continue
        payloads.append(
            {
                "event_id": item.get("id"),
                "kind": item.get("kind"),
                "agent_path": item.get("agent_path"),
                "agent_thread_id": item.get("agent_thread_id"),
                "started_at_ms": payload.get("started_at_ms"),
                "completed_at_ms": payload.get("completed_at_ms"),
            }
        )
    return payloads


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
    if schema_version != 2:
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
        if not isinstance(record, dict) or set(record) not in ({"id", "severity"}, {"id", "severity"} | detail_fields):
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
            {"id", "title", "required_change", "evidence"},
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
    if len(rows) < 3 or any(row is None or len(row) != len(ACTION_TABLE_HEADERS) for row in rows):
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


def _validate_unavailable_result(out_dir: Path, result: dict[str, Any], metadata: dict[str, Any], scope: str) -> None:
    """Validate a terminal PR-collection process failure without inventing a review outcome."""
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
    checkout_state_path = out_dir / "checkout-state.json"
    checkout_state: dict[str, object] | None = None
    if checkout_state_path.is_file():
        checkout_state = _load_json(checkout_state_path)
        if checkout_state not in (
            {"status": "checkout-command-started", "local_state": "changed-or-unknown"},
            {"status": "checkout-command-succeeded-unverified", "local_state": "changed-or-unknown"},
        ):
            raise SystemExit("unavailable-review-invalid-checkout-state")
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
    recovery_action = _unavailable_recovery_action(code, checkout_state is not None)
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
    if metadata.get("confidence_gap_closures") != [
        {
            "gap": UNAVAILABLE_CONFIDENCE_GAP,
            "status": "unresolved",
            "rationale": expected_closure_rationale,
        }
    ]:
        raise SystemExit("unavailable-review-confidence-closures-must-be-canonical")
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
    if metadata.get("confidence_recovery") != expected_recovery:
        raise SystemExit("unavailable-review-confidence-recovery-must-be-canonical")
    _validate_unavailable_final_handoff(out_dir, metadata)


def _validate_unavailable_final_handoff(out_dir: Path, metadata: dict[str, Any]) -> None:
    """Bind a v2 unavailable-review explanation to classified, non-secret local diagnostics."""
    binding = metadata.get("final_handoff")
    if not isinstance(binding, dict) or not isinstance(binding.get("handoff_path"), str):
        return
    handoff_path = _resolve_path(out_dir, binding["handoff_path"])
    if not handoff_path.is_file():
        return
    handoff = _load_json(handoff_path)
    if handoff.get("presentation_version") != 2:
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


def _unavailable_recovery_action(code: str, checkout_started: bool) -> str:
    """Return the canonical safe recovery for one classified collection failure."""
    category = code.split(":", maxsplit=1)[0]
    action_key = (
        "retry"
        if category in {"github-network", "github-rate-limit", "command-timeout"}
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
    reason_detail = f"; reason `{reason}`" if reason is not None else ""
    return f"Command diagnostic: `{label}` exited {exit_code} (`{failure_class}`{reason_detail}).", reason


def _unavailable_checkout_diagnostic(out_dir: Path) -> str | None:
    """Render the existing conservative checkout-state record without inferring a failure cause."""
    path = out_dir / "checkout-state.json"
    if not path.is_file():
        return None
    state = _load_json(path)
    if state not in (
        {"status": "checkout-command-started", "local_state": "changed-or-unknown"},
        {"status": "checkout-command-succeeded-unverified", "local_state": "changed-or-unknown"},
    ):
        raise SystemExit("unavailable-review-command-diagnostic-invalid")
    return f"Checkout diagnostic: local worktree state is changed or unknown after `{state['status']}`."


def _unavailable_preflight_diagnostic(out_dir: Path) -> str | None:
    """Render only the collector's fixed worktree head identifiers when that preflight exists."""
    path = out_dir / "worktree-preflight.json"
    if not path.is_file():
        return None
    preflight = _load_json(path)
    expected_keys = {"status", "current_head", "expected_head", "dirty_paths", "checkout_paths", "overlapping_paths"}
    head_pattern = re.compile(r"[0-9a-f]{7,64}\Z")
    if (
        set(preflight) != expected_keys
        or preflight.get("status")
        not in {"already-at-pr-head", "blocked-overlapping-dirty-paths", "clean", "safe-unrelated-dirty-paths"}
        or not isinstance(preflight.get("current_head"), str)
        or not head_pattern.fullmatch(preflight["current_head"])
        or not isinstance(preflight.get("expected_head"), str)
        or not head_pattern.fullmatch(preflight["expected_head"])
        or any(
            not isinstance(preflight.get(key), list) or not all(isinstance(item, str) for item in preflight[key])
            for key in expected_keys - {"status", "current_head", "expected_head"}
        )
    ):
        raise SystemExit("unavailable-review-worktree-preflight-invalid")
    return f"Worktree preflight: local head `{preflight['current_head']}`; expected PR head `{preflight['expected_head']}`."


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
    for filename in sorted(CLOSED_REQUIRED_PR_ARTIFACTS):
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


def _manifest_passes(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    passes = manifest.get("passes", manifest.get("specialist_passes"))
    if not isinstance(passes, list):
        raise SystemExit("manifest-missing-passes")
    normalized = []
    for index, item in enumerate(passes):
        if not isinstance(item, dict):
            raise SystemExit(f"manifest-pass-not-object:{index}")
        normalized.append(item)
    return normalized


def _validate_review_runtime(
    out_dir: Path,
    manifest: dict[str, Any],
    passes: list[dict[str, Any]],
    codex_home: Path,
    parent_thread_id: str,
) -> dict[str, object]:
    """Validate route-specific execution evidence before granting reviewer independence."""
    if manifest.get("schema_version") == 5:
        return _validate_instruction_bounded_review(out_dir, manifest, passes, codex_home, parent_thread_id)
    if manifest.get("schema_version") == 4:
        return _validate_app_server_review(out_dir, manifest, passes)
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
            roles_dir=PLUGIN_ROOT / "roles",
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


def _validate_inspection_plan(
    out_dir: Path, manifest: dict[str, Any], parent_thread_id: str
) -> tuple[dict[str, Any], dict[str, Path], bool, str | None]:
    """Bind schema-five inspection evidence to its exact no-execution plan."""
    execution = manifest.get("inspection_execution")
    if not isinstance(execution, dict) or set(execution) != {"plan_path", "plan_sha256"}:
        raise SystemExit("review-inspection-plan-missing")
    plan_path = _resolve_path(out_dir, execution["plan_path"])
    if not plan_path.is_file() or _sha256(plan_path) != execution["plan_sha256"]:
        raise SystemExit("review-inspection-plan-hash-mismatch")
    plan = _load_json(plan_path)
    required_keys = {
        "consumer_policy",
        "review_operation",
        "write_policy",
        "review_run_id",
        "parent_thread_id",
        "review_input_sha256",
        "source_sensitivity",
        "contexts",
        "independent_review_required",
        "independence_requirement_evidence",
    }
    if set(plan) != required_keys:
        raise SystemExit("review-inspection-plan-shape-invalid")
    if plan["consumer_policy"] != {
        "consumer_id": "code-review",
        "capability": "instruction-bounded-review",
        "promotion_status": "promoted",
        "parent_mutations": "serial",
        "canonical_gates": "serial",
    }:
        raise SystemExit("review-inspection-plan-policy-invalid")
    if plan["review_operation"] != "inspection-only" or plan["write_policy"] != {
        "parent_writes": "none",
        "approval_requirement": "not-required",
    }:
        raise SystemExit("review-inspection-plan-operation-invalid")
    if plan["source_sensitivity"] != "non-sensitive":
        raise SystemExit("review-inspection-plan-source-sensitivity-invalid")
    for key in ("review_run_id", "parent_thread_id", "review_input_sha256"):
        if plan[key] != manifest.get(key):
            raise SystemExit(f"review-inspection-plan-identity-mismatch:{key}")
    if plan["parent_thread_id"] != parent_thread_id:
        raise SystemExit("review-inspection-plan-parent-thread-mismatch")
    independent_required = plan["independent_review_required"]
    evidence = plan["independence_requirement_evidence"]
    if type(independent_required) is not bool:
        raise SystemExit("review-inspection-plan-independence-required-invalid")
    if independent_required:
        if not isinstance(evidence, str) or not evidence.strip():
            raise SystemExit("review-inspection-plan-independence-evidence-missing")
    elif evidence is not None:
        raise SystemExit("review-inspection-plan-independence-evidence-unexpected")
    try:
        contexts = validate_inspection_contexts(plan, plan_path)
    except ValueError as error:
        raise SystemExit(f"review-inspection-contexts-invalid:{error}") from error
    return plan, contexts, independent_required, evidence


def _child_controls(child_rows: list[dict[str, Any]], turn_id: str) -> dict[str, str]:
    """Report observed child controls without treating them as an isolation guarantee."""
    contexts = [
        row["payload"]
        for row in child_rows
        if row.get("type") == "turn_context"
        and isinstance(row.get("payload"), dict)
        and row["payload"].get("turn_id") == turn_id
    ]
    if len(contexts) != 1:
        raise SystemExit("review-inspection-turn-context-missing")
    context = contexts[0]
    sandbox = context.get("sandbox_mode")
    if sandbox is None and isinstance(context.get("sandbox_policy"), dict):
        sandbox = context["sandbox_policy"].get("type")
    approval = context.get("approval_policy")
    return {
        "sandbox_mode": sandbox if isinstance(sandbox, str) and sandbox else "unknown",
        "approval_policy": approval if isinstance(approval, str) and approval else "unknown",
    }


def _inspection_child_called_tool(child_rows: list[dict[str, Any]]) -> bool:
    """Reject every child tool-call or tool-result record for supplied-context inspection."""
    for row in child_rows:
        payload = row.get("payload")
        if row.get("type") == "response_item" and isinstance(payload, dict):
            if payload.get("type") not in {"agent_message", "message", "reasoning"}:
                return True
        elif row.get("type") == "event_msg" and isinstance(payload, dict):
            if payload.get("type") == "item_completed":
                item = payload.get("item")
                if not isinstance(item, dict) or item.get("type") not in {
                    "AgentMessage",
                    "Reasoning",
                    "ContextCompaction",
                }:
                    return True
            elif payload.get("type") not in {"task_started", "task_complete", "token_count", "thread_settings_applied"}:
                return True
    return False


def _joined_terminal_result(parent_rows: list[dict[str, Any]], agent_path: str, message: str) -> bool:
    """Require one parent-visible child final message matching the bound output exactly."""
    joined = 0
    for row in parent_rows:
        payload = row.get("payload")
        if row.get("type") != "response_item" or not isinstance(payload, dict):
            continue
        if payload.get("type") != "agent_message" or payload.get("author") != agent_path:
            continue
        content = payload.get("content")
        if not isinstance(content, list):
            continue
        texts = [item.get("text") for item in content if isinstance(item, dict) and item.get("type") == "input_text"]
        if (
            len(texts) == 1
            and isinstance(texts[0], str)
            and texts[0].startswith("Message Type: FINAL_ANSWER\n")
            and texts[0].split("Payload:\n", 1)[-1].strip() == message
        ):
            joined += 1
    return joined == 1


def _validate_instruction_bounded_review(
    out_dir: Path,
    manifest: dict[str, Any],
    passes: list[dict[str, Any]],
    codex_home: Path,
    parent_thread_id: str,
) -> dict[str, object]:
    """Validate supplied-context inspection without claiming host-enforced isolation."""
    if manifest.get("runtime_execution") is not None or manifest.get("app_server_execution") is not None:
        raise SystemExit("review-inspection-runtime-evidence-forbidden")
    _, frozen_contexts, independent_required, _ = _validate_inspection_plan(out_dir, manifest, parent_thread_id)
    inspections = [item for item in passes if item.get("mode") == "inspection"]
    if {item["role"] for item in inspections} != set(frozen_contexts):
        raise SystemExit("review-inspection-context-role-set-mismatch")
    if not inspections:
        return {
            "actual_mode": "serial-fallback",
            "evidence_level": "instruction-bounded-review",
            "write_parallel_eligible": False,
            "independence_satisfied": False,
            "independence_required": independent_required,
            "observed_controls": {},
        }

    parent_rows = _read_jsonl(_find_rollout(codex_home, parent_thread_id))
    controls: dict[str, dict[str, str]] = {}
    intervals: list[tuple[int | float, int | float]] = []
    for item in inspections:
        role = item["role"]
        role_card_path = PLUGIN_ROOT / "roles" / role / "ROLE.md"
        role_card = role_card_path.read_text(encoding="utf-8")
        selected = item["selected_attempt"]
        for attempt in item["attempts"]:
            context_path = _resolve_path(out_dir, attempt["context_path"])
            if context_path != frozen_contexts[role]:
                raise SystemExit(f"review-inspection-attempt-context-mismatch:{role}")
            context = context_path.read_text(encoding="utf-8")
            if any(pattern.search(context) for pattern in _SECRET_PATTERNS):
                raise SystemExit(f"review-inspection-context-sensitive-material:{role}")
            if not context.startswith(role_card):
                raise SystemExit(f"review-inspection-role-card-context-missing:{role}")
            child_rows = _read_jsonl(_find_rollout(codex_home, attempt["agent_thread_id"]))
            if _inspection_child_called_tool(child_rows):
                raise SystemExit(f"review-inspection-child-tool-use:{role}")
        attempt = item["attempts"][selected - 1]
        context = _resolve_path(out_dir, attempt["context_path"]).read_text(encoding="utf-8")
        spawn_calls = [
            row["payload"]
            for row in parent_rows
            if row.get("type") == "response_item"
            and isinstance(row.get("payload"), dict)
            and row["payload"].get("type") == "function_call"
            and row["payload"].get("name") == "spawn_agent"
        ]
        matching_calls = []
        for call in spawn_calls:
            try:
                arguments = json.loads(call.get("arguments", ""))
            except (TypeError, json.JSONDecodeError):
                continue
            if (
                isinstance(arguments, dict)
                and call.get("call_id") == attempt.get("spawn_call_id")
                and arguments.get("message") == context
                and arguments.get("task_name") == Path(attempt["agent_path"]).name
                and arguments.get("fork_turns") == "none"
            ):
                matching_calls.append(call)
        if len(matching_calls) != 1:
            raise SystemExit(f"review-inspection-context-not-sent:{role}")
        child_rows = _read_jsonl(_find_rollout(codex_home, attempt["agent_thread_id"]))
        controls[role] = _child_controls(child_rows, attempt["turn_id"])
        completions = [
            event
            for event in _event_payloads(child_rows, "task_complete")
            if event.get("turn_id") == attempt["turn_id"]
        ]
        if len(completions) != 1:
            raise SystemExit(f"review-inspection-child-terminal-missing:{role}")
        started_at, completed_at = completions[0].get("started_at"), completions[0].get("completed_at")
        if (
            isinstance(started_at, bool)
            or isinstance(completed_at, bool)
            or not isinstance(started_at, int | float)
            or not isinstance(completed_at, int | float)
            or not math.isfinite(started_at)
            or not math.isfinite(completed_at)
            or started_at >= completed_at
        ):
            raise SystemExit(f"review-inspection-child-timing-invalid:{role}")
        message = _resolve_path(out_dir, attempt["output_path"]).read_text(encoding="utf-8").strip()
        if any(pattern.search(message) for pattern in _SECRET_PATTERNS):
            raise SystemExit(f"review-inspection-output-sensitive-material:{role}")
        if not _joined_terminal_result(parent_rows, attempt["agent_path"], message):
            raise SystemExit(f"review-inspection-parent-join-missing:{role}")
        intervals.append((started_at, completed_at))

    overlaps = any(
        max(first[0], second[0]) < min(first[1], second[1])
        for index, first in enumerate(intervals)
        for second in intervals[index + 1 :]
    )
    actual_mode = "parallel" if overlaps else "independent-spawned" if len(inspections) > 1 else "serial"
    required_roles = REQUIRED_ROLES & {item["role"] for item in passes}
    independence_satisfied = bool(required_roles) and required_roles <= {item["role"] for item in inspections}
    return {
        "actual_mode": actual_mode,
        "evidence_level": "instruction-bounded-review",
        "write_parallel_eligible": False,
        "independence_satisfied": independence_satisfied,
        "independence_required": independent_required,
        "observed_controls": controls,
    }


def _validate_app_server_review(
    out_dir: Path, manifest: dict[str, Any], passes: list[dict[str, Any]]
) -> dict[str, object]:
    """Bind an isolated review wave without manufacturing native child lineage."""
    execution = manifest.get("app_server_execution")
    if not isinstance(execution, dict) or set(execution) != {"plan_path", "evidence_path", "evidence_sha256"}:
        raise SystemExit("review-app-server-execution-missing")
    if manifest.get("runtime_execution") is not None or any(item.get("mode") == "spawned" for item in passes):
        raise SystemExit("review-app-server-native-evidence-forbidden")
    plan_path = _resolve_path(out_dir, execution["plan_path"])
    evidence_path = _resolve_path(out_dir, execution["evidence_path"])
    if not evidence_path.is_file() or _sha256(evidence_path) != execution["evidence_sha256"]:
        raise SystemExit("review-app-server-evidence-hash-mismatch")
    try:
        summary = validate_app_server_evidence(plan_path, evidence_path, PLUGIN_ROOT / "roles")
    except (ReviewRouteError, ValueError, OSError) as error:
        raise SystemExit(f"review-app-server-evidence-invalid:{error}") from error
    evidence = _load_json(evidence_path)
    for key in ("review_run_id", "parent_thread_id", "review_input_sha256"):
        if evidence.get(key) != manifest.get(key):
            raise SystemExit(f"review-app-server-identity-mismatch:{key}")
    isolated = [item for item in passes if item.get("mode") == "app-server"]
    nodes = {node["role_id"]: node for node in evidence["nodes"]}
    if not isolated or len(isolated) != len(nodes) or {item.get("role") for item in isolated} != set(nodes):
        raise SystemExit("review-app-server-role-set-mismatch")
    for item in isolated:
        node = nodes[item["role"]]
        if (
            item.get("role_card_sha256") != node["role_card_sha256"]
            or _resolve_path(out_dir, item.get("output_path")) != (evidence_path.parent / node["output_path"]).resolve()
            or item.get("attempts") not in (None, [])
            or item.get("selected_attempt") is not None
        ):
            raise SystemExit(f"review-app-server-pass-mismatch:{item['role']}")
    if (
        summary.get("evidence_level") != "app-server-parent-observed"
        or summary.get("actual_mode") not in {"parallel", "independent-spawned"}
        or summary.get("approval_policy") != "never"
        or summary.get("filesystem_credential_isolation") != "unverified"
        or summary.get("write_parallel_eligible") is not False
        or summary.get("consumer_id") != "code-review"
    ):
        raise SystemExit("review-app-server-summary-invalid")
    return summary


def _validate_spawn_attempts(
    out_dir: Path,
    item: dict[str, Any],
    manifest: dict[str, Any],
    codex_home: Path,
    parent_rows: list[dict[str, Any]],
    used_threads: set[str],
    role_card: dict[str, str],
    used_context_paths: set[Path],
    used_output_paths: set[Path],
) -> None:
    """Bind a spawned specialist output to parent and child rollout evidence."""
    role = str(item["role"])
    attempts = item.get("attempts")
    if not isinstance(attempts, list) or not 1 <= len(attempts) <= 2:
        raise SystemExit(f"manifest-invalid-attempt-count:{role}")
    if not all(isinstance(attempt, dict) for attempt in attempts):
        raise SystemExit(f"manifest-attempt-not-object:{role}")
    if [attempt.get("attempt") for attempt in attempts] != list(range(1, len(attempts) + 1)):
        raise SystemExit(f"manifest-attempt-sequence:{role}")
    if len(attempts) == 2 and (
        attempts[0].get("status") == "completed" or attempts[0].get("error_type") not in TRANSIENT_RETRY_ERRORS
    ):
        raise SystemExit(f"manifest-invalid-retry:{role}")
    selected = item.get("selected_attempt")
    if not isinstance(selected, int) or selected < 1 or selected > len(attempts):
        raise SystemExit(f"manifest-invalid-selected-attempt:{role}")

    parent_events = _event_payloads(parent_rows, "sub_agent_activity")
    role_context_paths: set[Path] = set()
    for attempt in attempts:
        thread_id = attempt.get("agent_thread_id")
        event_id = attempt.get("event_id")
        agent_path = attempt.get("agent_path")
        if not all(isinstance(value, str) and value for value in (thread_id, event_id, agent_path)):
            raise SystemExit(f"manifest-attempt-identity-missing:{role}")
        context_path = _resolve_path(out_dir, attempt.get("context_path"))
        if context_path in used_context_paths and not (
            manifest.get("schema_version") == 5 and context_path in role_context_paths
        ):
            raise SystemExit("manifest-reused-context-path")
        used_context_paths.add(context_path)
        role_context_paths.add(context_path)
        context_sha256 = attempt.get("context_sha256")
        if not context_path.exists() or _sha256(context_path) != context_sha256:
            raise SystemExit(f"provenance-context-hash-mismatch:{role}")
        expected_agent_name = f"review_{role.replace('-', '_')}_{context_sha256[:12]}_a{attempt['attempt']}"
        if Path(agent_path).name != expected_agent_name:
            raise SystemExit(f"provenance-agent-path-context-mismatch:{role}:{agent_path}")
        if thread_id in used_threads:
            raise SystemExit(f"manifest-reused-agent-thread:{thread_id}")
        used_threads.add(thread_id)
        matches = [
            event
            for event in parent_events
            if event.get("event_id") == event_id
            and event.get("agent_thread_id") == thread_id
            and event.get("agent_path") == agent_path
            and event.get("kind") == "started"
        ]
        if len(matches) != 1:
            raise SystemExit(f"provenance-parent-spawn-mismatch:{role}:{attempt['attempt']}")

        child_rows = _read_jsonl(_find_rollout(codex_home, thread_id))
        session_rows = [
            row["payload"]
            for row in child_rows
            if row.get("type") == "session_meta"
            and isinstance(row.get("payload"), dict)
            and row["payload"].get("id") == thread_id
        ]
        if len(session_rows) != 1:
            raise SystemExit(f"provenance-child-session-count:{thread_id}")
        session = session_rows[0]
        spawn = session.get("source", {}).get("subagent", {}).get("thread_spawn", {})
        if session.get("id") != thread_id or spawn.get("parent_thread_id") != manifest["parent_thread_id"]:
            raise SystemExit(f"provenance-child-parent-mismatch:{thread_id}")
        session_path = session.get("agent_path") or spawn.get("agent_path")
        if session_path != agent_path:
            raise SystemExit(f"provenance-child-path-mismatch:{role}:{session_path}")
        session_role = session.get("agent_role") or spawn.get("agent_role")
        if session_role is not None and session_role != role:
            raise SystemExit(f"provenance-child-role-mismatch:{role}:{session_role}")

        if attempt.get("status") != "completed":
            if attempt.get("error_type") not in TRANSIENT_RETRY_ERRORS or attempt["attempt"] == selected:
                raise SystemExit(f"manifest-invalid-failed-attempt:{role}:{attempt['attempt']}")
            continue

        turn_id = attempt.get("turn_id")
        contexts = [
            row["payload"]
            for row in child_rows
            if row.get("type") == "turn_context"
            and isinstance(row.get("payload"), dict)
            and row["payload"].get("turn_id") == turn_id
        ]
        if len(contexts) != 1:
            raise SystemExit(f"provenance-turn-context-mismatch:{thread_id}")
        context = contexts[0]
        if context.get("model") != attempt.get("model") or context.get("effort") != attempt.get("effort"):
            raise SystemExit(f"provenance-model-effort-mismatch:{thread_id}")
        if context.get("model") != role_card["model"]:
            raise SystemExit(f"provenance-role-model-policy-mismatch:{role}:{thread_id}")
        if context.get("effort") != role_card["model_reasoning_effort"]:
            raise SystemExit(f"provenance-role-effort-policy-mismatch:{role}:{thread_id}")
        completions = [
            event for event in _event_payloads(child_rows, "task_complete") if event.get("turn_id") == turn_id
        ]
        if len(completions) != 1 or not isinstance(completions[0].get("last_agent_message"), str):
            raise SystemExit(f"provenance-task-complete-mismatch:{thread_id}")

        output_path = _resolve_path(out_dir, attempt.get("output_path"))
        if output_path in used_output_paths:
            raise SystemExit("manifest-reused-output-path")
        used_output_paths.add(output_path)
        if not output_path.exists() or _sha256(output_path) != attempt.get("output_sha256"):
            raise SystemExit(f"provenance-output-hash-mismatch:{role}")
        message = completions[0]["last_agent_message"].strip()
        if output_path.read_text(encoding="utf-8").strip() != message:
            raise SystemExit(f"provenance-output-message-mismatch:{role}")
        expected_header = (
            f"<!-- codex-review-provenance role={role} run={manifest['review_run_id']} "
            f"input={manifest['review_input_sha256']} context={attempt['context_sha256']} "
            f"attempt={attempt['attempt']} -->"
        )
        if message.splitlines()[0] != expected_header:
            raise SystemExit(f"provenance-output-header-mismatch:{role}")

    if attempts[selected - 1].get("status") != "completed":
        raise SystemExit(f"manifest-selected-attempt-not-completed:{role}")
    canonical_output = _resolve_path(out_dir, item.get("output_path"))
    selected_output = _resolve_path(out_dir, attempts[selected - 1].get("output_path"))
    if canonical_output != selected_output:
        raise SystemExit(f"manifest-selected-output-mismatch:{role}")


def _validate_manifest_entries(
    out_dir: Path,
    manifest: dict[str, Any],
    passes: list[dict[str, Any]],
    triggered_roles: set[str],
    codex_home: Path,
    parent_thread_id: str,
    project_root: Path,
) -> dict[str, dict[str, Any]]:
    """Bind every triggered pass to unique, role-specific evidence for its declared route."""
    schema_version = manifest.get("schema_version")
    if schema_version not in {2, 3, 4, 5}:
        raise SystemExit("manifest-schema-version")
    for key in ("review_run_id", "parent_thread_id", "review_input_sha256"):
        if not isinstance(manifest.get(key), str) or not manifest[key]:
            raise SystemExit(f"manifest-missing-{key}")
    if manifest["parent_thread_id"] != parent_thread_id:
        raise SystemExit("manifest-parent-thread-mismatch")
    review_input = out_dir / "diff.patch"
    if not review_input.exists() or _sha256(review_input) != manifest["review_input_sha256"]:
        raise SystemExit("manifest-review-input-hash-mismatch")
    if schema_version == 4:
        _validate_app_server_review(out_dir, manifest, passes)
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
        axis = item.get("axis")
        mode = item.get("mode")
        trigger = item.get("trigger")
        confidence = item.get("confidence")
        blocking_findings = item.get("blocking_findings")
        if not isinstance(role, str) or role not in ALL_MANIFEST_ROLES:
            raise SystemExit(f"manifest-invalid-role:{role!r}")
        if role in by_role:
            raise SystemExit(f"manifest-duplicate-role:{role}")
        role_card = _load_role_card(PLUGIN_ROOT / "roles", role)
        if schema_version in {3, 4, 5} and item.get("role_card_sha256") != role_card["role_card_sha256"]:
            raise SystemExit(f"manifest-role-card-hash-mismatch:{role}")
        if not isinstance(axis, str) or not axis.strip():
            raise SystemExit(f"manifest-missing-axis:{role}")
        if mode not in VALID_MODES:
            raise SystemExit(f"manifest-invalid-mode:{role}:{mode!r}")
        if (schema_version == 5 and mode not in {"inspection", "substituted"}) or (
            schema_version != 5 and mode == "inspection"
        ):
            raise SystemExit(f"manifest-mode-schema-mismatch:{role}")
        if not isinstance(trigger, str) or not trigger.strip():
            raise SystemExit(f"manifest-missing-trigger:{role}")
        if not isinstance(confidence, int | float) or not 0.0 <= float(confidence) <= 1.0:
            raise SystemExit(f"manifest-invalid-confidence:{role}")
        if not isinstance(blocking_findings, int) or blocking_findings < 0:
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
            if not substitute_output or role not in substitute_output:
                raise SystemExit(f"manifest-substitute-output-not-role-bound:{role}")
        by_role[role] = item

    if set(by_role) != triggered_roles:
        raise SystemExit("manifest-triggered-role-set-mismatch")
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
    )
    if manifest.get("schema_version") in {3, 4, 5}:
        _validate_review_runtime(out_dir, manifest, passes, codex_home, parent_thread_id)


def _validate_result(
    out_dir: Path,
    result_path: Path,
    codex_home: Path,
    parent_thread_id: str,
    project_root: Path,
) -> None:
    """Validate a complete review decision against routing, independent evidence, and gates."""
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
    if review_status == "unavailable":
        _validate_unavailable_result(out_dir, result, metadata, scope)
        _validate_confidence_gaps(result, metadata)
        _validate_confidence_recovery(result, metadata)
        return
    if review_status == "closed":
        _validate_closed_result(out_dir, result, metadata, scope)
        _validate_confidence_gaps(result, metadata)
        _validate_confidence_recovery(result, metadata)
        return
    if review_status is not None:
        raise SystemExit(f"invalid-review-status:{review_status!r}")

    notes_path = out_dir / "review-notes.md"
    _require_notes_sections(notes_path)
    _validate_review_decision(metadata, result)
    _validate_action_table(notes_path, result, metadata, scope)
    _validate_confidence_gaps(result, metadata)
    _validate_confidence_recovery(result, metadata)
    if scope == "pr":
        notes_text = notes_path.read_text(encoding="utf-8")
        if "Online Review Triage" not in notes_text:
            raise SystemExit("missing-pr-online-review-triage")
        for filename in (
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
        ):
            if not (out_dir / filename).exists():
                raise SystemExit(f"missing-pr-artifact:{filename}")
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
        expected_checkout = f"gh pr checkout {routing.get('pr_number')}"
        if routing.get("pr_metadata_transport") == "public-https-fallback":
            expected_checkout = (
                f"git checkout --detach refs/remotes/{remote_selection.get('remote')}/pull/"
                f"{routing.get('pr_number')}/head"
            )
        if routing.get("local_checkout_command") != expected_checkout:
            raise SystemExit("pr-routing-checkout-command-invalid")
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
        expected_diff_command = f"git diff --binary {routing.get('base_oid')}...{routing.get('head_oid')} --"
        if (
            checkout.get("diff_source") != "verified-local-checkout"
            or checkout.get("diff_base_oid") != routing.get("base_oid")
            or checkout.get("diff_head_oid") != routing.get("head_oid")
            or checkout.get("diff_command") != expected_diff_command
        ):
            raise SystemExit("pr-local-diff-provenance-invalid")
        thread_status = online_summary.get("review_threads_status")
        thread_error = online_summary.get("review_threads_error")
        if thread_status == "available":
            if thread_error is not None or (out_dir / "review-threads-error.txt").exists():
                raise SystemExit("pr-review-thread-status-contradiction")
        elif thread_status == "unavailable":
            error_path = out_dir / "review-threads-error.txt"
            if not isinstance(thread_error, str) or not error_path.is_file():
                raise SystemExit("pr-review-thread-error-missing")
            if error_path.read_text(encoding="utf-8").strip() != thread_error:
                raise SystemExit("pr-review-thread-error-mismatch")
            if _load_json_list(out_dir / "review-threads.json") or _load_json_list(
                out_dir / "unresolved-review-threads.json"
            ):
                raise SystemExit("pr-review-thread-unavailable-must-be-empty")
            confidence_gaps = metadata.get("confidence_gaps")
            if not isinstance(confidence_gaps, list) or PR_THREAD_CONFIDENCE_GAP not in confidence_gaps:
                raise SystemExit("pr-review-thread-confidence-gap-missing")
            if "review-thread" not in notes_text.casefold() or "unavailable" not in notes_text.casefold():
                raise SystemExit("pr-review-thread-triage-gap-missing")
        else:
            raise SystemExit("pr-review-thread-status-invalid")
        if (out_dir / "head-files").exists():
            raise SystemExit("pr-raw-head-file-snapshots-forbidden")

    triggered_roles = _validate_routing(out_dir, risk_tier)
    manifest_path = _resolve_path(out_dir, metadata.get("specialist_manifest"))
    manifest = _load_json(manifest_path)
    routing = _load_json(out_dir / "review-routing.json")
    if manifest.get("sol_selection") != routing.get("sol_selection"):
        raise SystemExit("manifest-sol-selection-routing-mismatch")
    passes = _manifest_passes(manifest)
    by_role = _validate_manifest_entries(
        out_dir,
        manifest,
        passes,
        triggered_roles,
        codex_home,
        parent_thread_id,
        project_root,
    )
    runtime_summary = (
        _validate_review_runtime(out_dir, manifest, passes, codex_home, parent_thread_id)
        if manifest.get("schema_version") in {3, 4, 5}
        else {}
    )
    if runtime_summary:
        for key, expected in (
            ("execution_mode", runtime_summary.get("actual_mode")),
            ("execution_evidence_level", runtime_summary.get("evidence_level")),
            ("write_parallel_eligible", False),
        ):
            if metadata.get(key) != expected:
                raise SystemExit(f"metadata-{key.replace('_', '-')}-mismatch")
        if manifest.get("schema_version") == 5:
            if metadata.get("execution_observed_controls") != runtime_summary.get("observed_controls"):
                raise SystemExit("metadata-execution-observed-controls-mismatch")
    if metadata.get("review_run_id") != manifest.get("review_run_id"):
        raise SystemExit("metadata-review-run-id-mismatch")
    if metadata.get("review_input_sha256") != manifest.get("review_input_sha256"):
        raise SystemExit("metadata-review-input-hash-mismatch")

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

    triggered_required = REQUIRED_ROLES & triggered_roles
    substituted_roles = sorted(role for role in triggered_required if by_role[role]["mode"] == "substituted")
    if manifest.get("schema_version") == 5:
        _, _, independence_required, requirement_evidence = _validate_inspection_plan(
            out_dir, manifest, parent_thread_id
        )
        routing_requirement = routing.get("independent_review_required")
        if routing_requirement is not independence_required:
            raise SystemExit("routing-inspection-independence-required-mismatch")
        if routing.get("independence_requirement_evidence") != requirement_evidence:
            raise SystemExit("routing-inspection-independence-evidence-mismatch")
        required_independent = bool(runtime_summary.get("independence_satisfied"))
        if metadata.get("independence_requirement_evidence") != requirement_evidence:
            raise SystemExit("metadata-independence-requirement-evidence-mismatch")
        if independence_required and status == "pass" and not required_independent:
            raise SystemExit("independent-review-required-for-pass:" + ",".join(sorted(triggered_required)))
    else:
        independence_required = bool(triggered_required)
        required_independent = independence_required and all(
            by_role[role]["mode"] in {"spawned", "app-server"} for role in triggered_required
        )
        if risk_tier in INDEPENDENT_PASS_TIERS and status == "pass" and not required_independent:
            raise SystemExit("independent-review-required-for-pass:" + ",".join(substituted_roles))

    fanout_substituted = any(item["mode"] == "substituted" for item in passes)
    if metadata.get("fanout_substituted") is not fanout_substituted:
        raise SystemExit("metadata-fanout-substituted-mismatch")

    independence_satisfied = bool(required_independent)
    if metadata.get("independence_satisfied") is not independence_satisfied:
        raise SystemExit("metadata-independence-satisfied-mismatch")
    if metadata.get("independence_required") is not independence_required:
        raise SystemExit("metadata-independence-required-mismatch")


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
    args = parser.parse_args()

    if not args.parent_thread_id:
        raise SystemExit("missing-parent-thread-id")
    if args.manifest_only:
        _validate_manifest_preflight(args.out, args.codex_home, args.parent_thread_id, args.project_root)
        return 0
    _validate_result(args.out, args.result, args.codex_home, args.parent_thread_id, args.project_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
