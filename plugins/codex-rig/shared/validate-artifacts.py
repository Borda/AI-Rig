#!/usr/bin/env python3
"""Validate common Codex Rig workflow artifacts and result invariants.

## Purpose

Reject incomplete, contradictory, or confidence-unsupported workflow output before it can be presented as a completed
result. Validation ties the candidate result to the gates, required notes, and skill-specific evidence that justify its
status and confidence.

## Scope

It reads local artifact files, gate records, and result JSON; it does not execute gates, review code, or mutate source
data. Requirements vary by skill, with PR remediation additionally checking review intake, scope selection, workplan,
resolution tables, identity, and merge evidence.

## Usage

Run ``python validate-artifacts.py --skill <id> --out <directory> --result <candidate.json>`` before promoting a result.
The result path may be a candidate or final JSON, but the output directory must contain the canonical gate and section
artifacts required for the selected skill. Older loop results remain readable as JSON, but cannot bypass current
validation by claiming an unverifiable archive identity.

## Used by

Implement, remediate, and review artifact workflows plus artifact-contract acceptance tests use this validator. The
validator is the final local contract check before a workflow reports completion, not a substitute for running the
checks whose records it validates.

## Outputs

It prints a passed validation confirmation or raises a precise contract error that names the missing or contradictory
evidence. Errors use stable prefixes such as ``missing-gates-json``, ``gate-check-id-set-mismatch``, and skill-specific
``code-remediate-*`` codes so callers can route recovery.

## Failure

Malformed JSON, absent required notes/gates, inconsistent confidence metadata, or outcome/gate disagreement produces a
nonzero exit and prevents promotion. A structurally valid but incomplete artifact is therefore still rejected;
validation does not silently downgrade missing evidence to a warning.
"""

from __future__ import annotations

import argparse
import hashlib
from html import escape
import json
import math
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, NamedTuple

# Preserve sibling-helper imports when callers load this executable by file path.
SHARED_DIRECTORY = Path(__file__).resolve().parent
if str(SHARED_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SHARED_DIRECTORY))

from collect_pr import _github_remote_identity, _head_repository  # noqa: E402
from release_evidence import validate_release_evidence  # noqa: E402

COMMON_RESULT_FIELDS = {
    "status",
    "checks_run",
    "checks_failed",
    "findings",
    "confidence",
    "artifact_path",
}
RESULT_SCHEMA_VERSION = 2
FINAL_HANDOFF_METADATA_FIELDS = {
    "schema_version",
    "handoff_path",
    "handoff_sha256",
    "rendered_path",
    "rendered_sha256",
    "validation_path",
    "branch",
}
FINAL_HANDOFF_FILENAMES = {
    "handoff_path": "final-handoff.json",
    "rendered_path": "final.md",
    "validation_path": "final-handoff.validation.json",
}
EXPECTED_GATE_IDS = {"lint", "format", "types", "tests", "review"}
FAILING_GATE_STATUSES = {"fail", "missing-command", "timeout"}
VALID_GATE_STATUSES = {"pass", "fail", "missing-command", "not-applicable", "timeout"}
CODE_REVIEW_UNAVAILABLE_GATE_IDS = ("lint", "format", "types", "tests", "review")

UNRESOLVED_REASON_GROUPS = {
    "local-code-or-doc",
    "process-gate",
    "independent-review",
    "environment-blocked",
    "external-ci",
    "user-deferred",
    "already-closed",
    "other",
}

UNRESOLVED_NEXT_OWNERS = {
    "codex",
    "user",
    "maintainer",
    "ci",
    "environment",
    "external-reviewer",
}

CODE_REMEDIATE_TRIAGE_STATUSES = {
    "valid",
    "resolved",
    "duplicate",
    "stale",
    "out-of-scope",
    "already-fixed",
    "already-applied",
    "needs-clarification",
}

CODE_REMEDIATE_RESOLUTION_STATUSES = {
    "implemented",
    "resolved",
    "rejected",
    "stale",
    "not-applicable",
    "duplicate",
    "already-fixed",
    "already-applied",
    "needs-clarification",
    "unresolved",
}
V3_RESOLUTION_DISPOSITIONS = {
    "implemented": {"Implemented"},
    "resolved": {"Verified without code changes"},
    "rejected": {"Rejected"},
    "not-applicable": {"Not applicable", "Rejected"},
    "duplicate": {"Duplicate", "Rejected"},
    "already-fixed": {"Verified without code changes"},
    "already-applied": {"Verified without code changes"},
    "needs-clarification": {"Needs clarification"},
    "unresolved": {"Blocked", "Deferred", "Not selected"},
}

CODE_REMEDIATE_FINAL_TABLE_REQUIRED_COLUMNS = {
    "input item",
    "item name",
    "item type",
    "sources",
    "triage status",
    "resolution",
    "owner/status",
    "resolved how",
    "evidence",
}
CODE_REMEDIATE_SOURCE_KINDS = {"report", "online", "user"}
CODE_REMEDIATE_SOURCE_STRING_FIELDS = {"source_id", "location", "body", "evidence"}
CODE_REMEDIATE_REPORT_SOURCE_ID = re.compile(r"(?:.+:[1-9]\d*|.+\.json#[^#\r\n]+)", re.IGNORECASE)
CODE_REMEDIATE_USER_SOURCE_ID = re.compile(r"user-[A-Za-z0-9_-]+#finding-[1-9]\d*")
CODE_REMEDIATE_ONLINE_SOURCE_ID = re.compile(r"(?!https?://)\S+", re.IGNORECASE)
CODE_REMEDIATE_FINAL_ITEM_STRING_FIELDS = {
    "input_item_id",
    "item_name",
    "item_type",
    "severity",
    "triage_status",
    "resolution_status",
    "owner_status",
    "resolved_how",
    "evidence",
}
CODE_REMEDIATE_WORK_BUCKET_OWNERS = {
    "parent",
    "sw-engineer",
    "qa-specialist",
    "doc-scribe",
    "cicd-steward",
    "linting-expert",
    "data-steward",
    "scientist",
    "squeezer",
    "oss-shepherd",
}
CODE_REMEDIATE_WORK_BUCKET_VERIFIERS = {
    "parent",
    "qa-specialist",
    "security-auditor",
    "linting-expert",
    "cicd-steward",
    "challenger",
    "solution-architect",
    "none",
}
PR_THREAD_CONFIDENCE_GAP = "PR review-thread resolution status was unavailable; online review triage may be incomplete."
PR_PUBLIC_FALLBACK_MAX_CONFIDENCE = 0.89

SKILL_REQUIREMENTS: dict[str, dict[str, object]] = {
    "challenge-resolve": {
        "files": {
            "loop-ledger.json": [],
            "loop-report.md": ["Scope", "Rounds", "Findings", "Verification"],
        },
    },
    "assess": {"files": {}},
    # Historical reports retain their original skill identity and artifact paths.
    "change-analysis": {"files": {}},
    "audit": {
        "files": {
            "workflow-exploration.md": ["Transitions", "Counterexamples", "Coverage"],
            "audit-ledger.md": [
                "Inventory",
                "Broken References",
                "Runtime Leaks",
                "Coverage",
                "Overlap",
                "Prompt Efficiency",
                "Recommendations",
            ],
            "prompt-efficiency.md": [
                "Measurement",
                "Cost Baseline",
                "Loaded Context",
                "Obligation Map",
                "Value Guards",
                "Adversarial Review",
                "Recommendations",
            ],
        },
    },
    "calibrate": {"files": {}},
    "research": {"files": {}},
    "code-review": {"files": {}},
    "sync": {"files": {}},
    "implement": {
        "files": {
            "development-notes.md": ["Scope", "Acceptance Criteria", "Evidence", "Specialist Policy", "Gates"],
            "confidence-calibration.md": [
                "Initial Confidence",
                "Objective Evidence",
                "Confidence Gaps",
                "Recovery Actions",
                "Recomputed Confidence",
                "Remaining Limits",
            ],
        },
    },
    "code-remediate": {
        "files": {
            "action-items.md": ["Review Item Resolution Table"],
            "resolution-scope.md": ["Resolution Scope Selection"],
            "closure-log.md": ["Closure Evidence"],
            "unresolved.txt": [],
        },
    },
    "investigate": {
        "files": {
            "symptom.md": [],
            "hypotheses.md": ["Falsification"],
            "root-cause.md": ["Evidence", "Falsification", "Rejected Alternatives", "Confidence"],
        },
    },
    "kaggle": {
        "files": {
            "profile.md": [
                "Normalized Inputs",
                "Grounded Facts",
                "Model Decision",
                "Verification",
                "Residual Limits",
            ],
        },
    },
    "manage": {
        "files": {
            "ownership.md": ["Intent", "Owned Files", "Verification", "Residual Limits"],
        },
    },
    "optimize": {
        "files": {
            "hypothesis.md": [],
            "comparison.md": ["baseline", "after", "delta", "guard", "confidence"],
            "experiments.jsonl": [],
        },
        "jsonl": ["experiments.jsonl"],
    },
    "release": {
        "files": {
            "change-table.md": [],
            "release-readiness.md": ["SemVer", "Migration", "Checks", "Blockers"],
        },
    },
}


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise SystemExit(f"expected-json-object:{path}")
    return payload


def _load_json_list(path: Path) -> list[Any]:
    """Load one required JSON array artifact."""
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, list):
        raise SystemExit(f"expected-json-array:{path}")
    return payload


def _require_result_shape(result: dict[str, Any]) -> None:
    missing = sorted(COMMON_RESULT_FIELDS - set(result))
    if missing:
        raise SystemExit("result-missing-fields:" + ",".join(missing))
    if result["status"] not in {"pass", "fail", "timeout"}:
        raise SystemExit(f"invalid-status:{result['status']!r}")
    if not isinstance(result["checks_run"], list):
        raise SystemExit("invalid-checks-run")
    if not isinstance(result["checks_failed"], list):
        raise SystemExit("invalid-checks-failed")
    findings = result["findings"]
    if not isinstance(findings, dict):
        raise SystemExit("invalid-findings")
    for key in ("critical", "high", "medium", "low"):
        if not isinstance(findings.get(key), int) or findings[key] < 0:
            raise SystemExit(f"invalid-finding-count:{key}")
    confidence = result["confidence"]
    if not isinstance(confidence, int | float) or not 0.0 <= float(confidence) <= 1.0:
        raise SystemExit("invalid-confidence")
    if result["status"] == "pass" and result["checks_failed"]:
        raise SystemExit("pass-with-failed-checks")
    if result["status"] == "pass" and findings["critical"] > 0:
        raise SystemExit("pass-with-critical-findings")


def _anchored_candidates(base: Path, declared: Path) -> list[Path]:
    """List where a declared path may live, deriving every candidate from `base`.

    A recorded path is data written by an earlier process, and the directory that process ran in is not stored with it.
    Resolving such a path against the *reader's* directory is what once let one artifact be valid in one place and
    invalid in another. Current runs record a name relative to the output directory; runs written before that convention
    recorded one relative to some ancestor of it, and those artifacts are still revalidated long afterwards, so
    ancestors are offered too. Callers keep their own containment check: widening where a name may resolve must never
    widen what is accepted as evidence.
    """
    if declared.is_absolute():
        return [declared]
    return [base / declared, *(ancestor / declared for ancestor in base.resolve().parents)]


def _resolve_final_handoff_path(out_dir: Path, raw_path: object, key: str) -> Path:
    """Resolve one declared final-handoff path inside the workflow directory."""
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise SystemExit(f"final-handoff-invalid-path:{key}")
    declared = Path(raw_path)
    candidates = _anchored_candidates(out_dir, declared)
    expected_parent = out_dir.resolve()
    expected_name = FINAL_HANDOFF_FILENAMES[key]
    matched_location = False
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.parent != expected_parent or resolved.name != expected_name:
            continue
        matched_location = True
        if not candidate.is_symlink() and resolved.is_file():
            return resolved
    if matched_location:
        raise SystemExit(f"final-handoff-missing-file:{key}")
    raise SystemExit(f"final-handoff-path-mismatch:{key}")


def _validate_result_artifact_path(out_dir: Path, raw_path: object) -> None:
    """Require schema-v2 results to bind the canonical final path before promotion."""
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise SystemExit("result-artifact-path-mismatch")
    declared = Path(raw_path)
    run_root = out_dir.resolve()
    expected = (out_dir / "result.json").resolve()
    if not expected.is_relative_to(run_root):
        raise SystemExit("result-artifact-path-mismatch")
    candidates = _anchored_candidates(out_dir, declared)
    if not any(candidate.resolve() == expected for candidate in candidates):
        raise SystemExit("result-artifact-path-mismatch")


def _validate_final_handoff_confidence(result: dict[str, Any], handoff: dict[str, Any], skill: str) -> None:
    """Reconcile rendered confidence gaps and limits with canonical result metadata."""
    confidence = handoff.get("confidence")
    metadata = result.get("metadata")
    if not isinstance(confidence, dict) or not isinstance(metadata, dict):
        raise SystemExit(f"{skill}-final-handoff-confidence-invalid")
    score = confidence.get("score")
    if not isinstance(score, int | float) or abs(float(score) - float(result["confidence"])) > 0.001:
        raise SystemExit(f"{skill}-final-handoff-confidence-mismatch")
    gap_entries = confidence.get("gaps")
    declared_gaps = metadata.get("confidence_gaps")
    closures = metadata.get("confidence_gap_closures")
    if not isinstance(gap_entries, list) or not isinstance(declared_gaps, list) or not isinstance(closures, list):
        raise SystemExit(f"{skill}-final-handoff-confidence-gaps-invalid")
    normalized_gaps = [gap.strip() for gap in declared_gaps if isinstance(gap, str)]
    if len(normalized_gaps) != len(declared_gaps) or len(normalized_gaps) != len(set(normalized_gaps)):
        raise SystemExit(f"{skill}-final-handoff-confidence-gaps-invalid")
    handoff_gaps = {entry.get("gap") for entry in gap_entries if isinstance(entry, dict)}
    if handoff_gaps != set(normalized_gaps):
        raise SystemExit(f"{skill}-final-handoff-confidence-gaps-mismatch")
    closure_by_gap = {entry.get("gap"): entry for entry in closures if isinstance(entry, dict)}
    for entry in gap_entries:
        gap = entry.get("gap")
        closure = closure_by_gap.get(gap)
        if not isinstance(closure, dict) or entry.get("status") != closure.get("status"):
            raise SystemExit(f"{skill}-final-handoff-confidence-closure-mismatch")
        detail_key = "evidence" if entry.get("status") == "closed" else "rationale"
        expected_detail = closure.get(detail_key) or closure.get("evidence_path")
        if entry.get(detail_key) != expected_detail:
            raise SystemExit(f"{skill}-final-handoff-confidence-closure-mismatch")
    recovery = metadata.get("confidence_recovery")
    if not isinstance(recovery, dict) or confidence.get("limits") != recovery.get("remaining_limits"):
        raise SystemExit(f"{skill}-final-handoff-confidence-limits-mismatch")


def _validate_final_handoff_gates(handoff: dict[str, Any], gates: dict[str, Any], skill: str) -> None:
    """Require rendered verification to preserve every canonical gate record."""
    verification = handoff.get("verification")
    checks = gates.get("checks")
    if not isinstance(verification, list) or not isinstance(checks, list):
        raise SystemExit(f"{skill}-final-handoff-verification-invalid")
    expected = [
        {"check": check.get("id"), "status": check.get("status"), "evidence": check.get("stdout")} for check in checks
    ]
    if verification != expected:
        raise SystemExit(f"{skill}-final-handoff-verification-mismatch")


def _validate_code_remediate_final_handoff(result: dict[str, Any], handoff: dict[str, Any]) -> None:
    """Bind remediation presentation rows and sources to the canonical resolution table."""
    if handoff.get("branch") == "caller-contract":
        return
    metadata = result.get("metadata")
    if not isinstance(metadata, dict):
        raise SystemExit("code-remediate-final-handoff-metadata-invalid")
    resolution_table = metadata.get("final_resolution_table")
    if not isinstance(resolution_table, dict) or not isinstance(resolution_table.get("items"), list):
        raise SystemExit("code-remediate-final-handoff-resolution-table-missing")
    tables = handoff.get("tables")
    if not isinstance(tables, list) or len(tables) != 1:
        raise SystemExit("code-remediate-final-handoff-table-invalid")
    rows = tables[0].get("rows")
    if not isinstance(rows, list):
        raise SystemExit("code-remediate-final-handoff-table-invalid")
    expected_items = resolution_table["items"]
    presentation = metadata.get("resolution_scope", {}).get("presentation_version")
    if (
        presentation in {2, 3, 4}
        and tables[0].get("layout") != {2: "grouped", 3: "concise", 4: "concise"}[presentation]
    ):
        raise SystemExit("code-remediate-final-handoff-grouped-layout-required")
    if presentation in {3, 4} and tables[0].get("overview_only") is not True:
        raise SystemExit("code-remediate-final-handoff-overview-only-required")
    expected_rows = []
    expected_details = []
    expected_source_records = []
    for position, item in enumerate(expected_items, start=1):
        if not isinstance(item, dict):
            raise SystemExit("code-remediate-final-handoff-resolution-item-invalid")
        sources = item.get("sources")
        if not isinstance(sources, list):
            raise SystemExit("code-remediate-final-handoff-resolution-sources-invalid")
        source_ids = []
        rendered_sources = []
        for source in sources:
            if not isinstance(source, dict):
                raise SystemExit("code-remediate-final-handoff-resolution-source-invalid")
            source_id = f"{source.get('kind')}:{source.get('source_id')}"
            source_ids.append(source_id)
            rendered_sources.append(f"{source.get('kind')} [{source.get('source_id')}]")
            expected_source_records.append({"id": source_id, "evidence": source.get("evidence")})
        expected_rows.append(
            {
                "id": item.get("input_item_id"),
                "cells": [
                    item.get("input_item_id"),
                    item.get("severity"),
                    item.get("item_name"),
                    "\n".join(rendered_sources),
                    f"{item.get('resolution_status')} — [O{position}]",
                    f"[E{position}] — owner/status: {item.get('owner_status')}",
                ],
                "source_ids": source_ids,
            }
        )
        expected_details.extend(
            (
                {"id": f"O{position}", "text": item.get("resolved_how")},
                {"id": f"E{position}", "text": item.get("evidence")},
            )
        )
    if rows != expected_rows or tables[0].get("details") != expected_details:
        raise SystemExit("code-remediate-final-handoff-row-coverage-mismatch")
    expected_sources = {
        f"{source.get('kind')}:{source.get('source_id')}"
        for item in expected_items
        if isinstance(item, dict)
        for source in item.get("sources", [])
        if isinstance(source, dict)
    }
    observed_sources = {
        source_id
        for row in rows
        if isinstance(row, dict)
        for source_id in row.get("source_ids", [])
        if isinstance(source_id, str)
    }
    declared_source_records = handoff.get("source_records")
    declared_sources = {source.get("id") for source in declared_source_records or [] if isinstance(source, dict)}
    if (
        observed_sources != expected_sources
        or declared_sources != expected_sources
        or declared_source_records != expected_source_records
    ):
        raise SystemExit("code-remediate-final-handoff-source-coverage-mismatch")


def _code_review_pr_ci_snapshot(rollup: object) -> str:
    """Summarize collected PR checks using failure, pending, then passing precedence."""
    if rollup is None or rollup == []:
        return "unavailable"
    if not isinstance(rollup, list):
        raise SystemExit("code-review-final-handoff-pr-snapshot-source-invalid")
    failed: list[str] = []
    pending: list[str] = []
    for check in rollup:
        if not isinstance(check, dict):
            raise SystemExit("code-review-final-handoff-pr-snapshot-source-invalid")
        if check.get("__typename") == "CheckRun":
            name = check.get("name")
            status = check.get("status")
            conclusion = check.get("conclusion")
            is_pending = status != "COMPLETED"
            is_failed = not is_pending and conclusion not in {"SUCCESS", "NEUTRAL", "SKIPPED"}
        elif check.get("__typename") == "StatusContext":
            name = check.get("context")
            state = check.get("state")
            is_pending = state not in {"SUCCESS", "FAILURE", "ERROR"}
            is_failed = state in {"FAILURE", "ERROR"}
        else:
            raise SystemExit("code-review-final-handoff-pr-snapshot-source-invalid")
        if not isinstance(name, str) or not name:
            raise SystemExit("code-review-final-handoff-pr-snapshot-source-invalid")
        if is_failed:
            failed.append(name)
        elif is_pending:
            pending.append(name)
    if failed:
        return f"failing — {', '.join(failed)}"
    if pending:
        return f"pending — {', '.join(pending)}"
    return "passing"


def _validate_code_review_final_handoff(
    result: dict[str, Any], handoff: dict[str, Any], *, candidate: bool = False, out_dir: Path | None = None
) -> None:
    """Bind current review branches to results while retaining historical caller contracts."""
    metadata = result.get("metadata")
    if not isinstance(metadata, dict):
        raise SystemExit("code-review-final-handoff-metadata-invalid")
    review_status = metadata.get("review_status")
    expected_branch = review_status if review_status in {"unavailable", "closed"} else "assessed"
    if result.get("schema_version") == 3 and expected_branch == "assessed" and handoff.get("presentation_version") != 3:
        raise SystemExit("review-current-handoff-presentation-v3-required")
    if handoff.get("branch") == "caller-contract" and result.get("schema_version") != 3:
        return
    if handoff.get("branch") != expected_branch:
        raise SystemExit("code-review-final-handoff-branch-mismatch")
    if expected_branch == "assessed":
        decision = metadata.get("review_decision")
        recommendation = decision.get("recommendation") if isinstance(decision, dict) else None
        expected_outcome = {
            "title": "Review Decision",
            "summary": f"Recommendation: {recommendation}.",
        }
        if handoff.get("outcome") != expected_outcome:
            raise SystemExit("code-review-final-handoff-outcome-mismatch")
    tables = handoff.get("tables")
    if not isinstance(tables, list):
        raise SystemExit("code-review-final-handoff-tables-invalid")
    tables_by_heading = {
        table.get("heading"): table
        for table in tables
        if isinstance(table, dict) and isinstance(table.get("heading"), str)
    }
    headings = set(tables_by_heading)
    if result.get("schema_version") == 3 and expected_branch == "assessed":
        expected_snapshot = "PR Snapshot" if metadata.get("scope") == "pr" else "Review Snapshot"
        if headings & {"PR Snapshot", "Review Snapshot"} != {expected_snapshot}:
            raise SystemExit("code-review-final-handoff-snapshot-scope-mismatch")
    reviewer_tables = [table for table in tables if isinstance(table, dict) and "reviewers" in table]
    assessments = metadata.get("reviewer_assessments")
    if (candidate or result.get("schema_version") == 3) and expected_branch == "assessed":
        snapshot_heading = "PR Snapshot" if metadata.get("scope") == "pr" else "Review Snapshot"
        if (
            not isinstance(assessments, list)
            or not assessments
            or len(reviewer_tables) != 1
            or reviewer_tables[0].get("heading") != snapshot_heading
            or reviewer_tables[0]["reviewers"] != assessments
        ):
            raise SystemExit("code-review-final-handoff-candidate-attribution-missing")
    if assessments is not None or reviewer_tables:
        if expected_branch != "assessed" or len(reviewer_tables) != 1 or reviewer_tables[0]["reviewers"] != assessments:
            raise SystemExit("code-review-final-handoff-reviewers-mismatch")
        if reviewer_tables[0].get("summary") != metadata["review_decision"].get("summary"):
            raise SystemExit("code-review-final-handoff-review-summary-mismatch")
    if expected_branch in {"unavailable", "closed"} and tables:
        raise SystemExit("code-review-terminal-final-handoff-table-forbidden")
    if expected_branch == "assessed" and metadata.get("scope") == "pr" and "PR Snapshot" not in headings:
        raise SystemExit("code-review-final-handoff-pr-snapshot-missing")
    # `Review Snapshot` is the non-PR counterpart and carries the reviewed target and revision where the PR
    # snapshot names the pull request. The heading is new in this release, so no stored artifact predates it.
    snapshot_fields = {
        "PR Snapshot": ("PR", "Author", "CI", "Type", "Suggestion"),
        "Review Snapshot": ("Scope", "Revision", "CI", "Type", "Suggestion"),
    }
    snapshot_heading = next((heading for heading in snapshot_fields if heading in headings), None)
    if expected_branch == "assessed" and snapshot_heading:
        label = "pr-snapshot" if snapshot_heading == "PR Snapshot" else "review-snapshot"
        snapshot = tables_by_heading[snapshot_heading]
        rows = snapshot.get("rows")
        expected_fields = snapshot_fields[snapshot_heading]
        if (
            not isinstance(rows, list)
            or tuple(
                row["cells"][0]
                if isinstance(row, dict) and isinstance(row.get("cells"), list) and len(row["cells"]) == 2
                else None
                for row in rows
            )
            != expected_fields
        ):
            raise SystemExit(f"code-review-final-handoff-{label}-fields-mismatch")
        decision = metadata.get("review_decision")
        recommendation = decision.get("recommendation") if isinstance(decision, dict) else None
        suggestions = {
            "accept-as-is": "approve",
            "minor-changes": "minor changes",
            "needs-more-work": "needs work",
            "reject": "reject",
            "not-aligned": "not aligned",
        }
        suggestion_cells = rows[-1].get("cells")
        if (
            not isinstance(suggestion_cells, list)
            or len(suggestion_cells) != 2
            or suggestion_cells[1] != suggestions.get(recommendation)
        ):
            raise SystemExit(f"code-review-final-handoff-{label}-suggestion-mismatch")
        if snapshot_heading == "Review Snapshot" and result.get("schema_version") == 3:
            if rows[0]["cells"][1] != metadata.get("scope"):
                raise SystemExit("code-review-final-handoff-review-snapshot-scope-mismatch")
            if out_dir is None:
                raise SystemExit("code-review-final-handoff-review-snapshot-source-invalid")
            try:
                diff_digest = hashlib.sha256((out_dir / "diff.patch").read_bytes()).hexdigest()
            except OSError as exception:
                raise SystemExit("code-review-final-handoff-review-snapshot-source-invalid") from exception
            if metadata.get("review_input_sha256") != diff_digest:
                raise SystemExit("code-review-final-handoff-review-snapshot-source-invalid")
            if rows[1]["cells"][1] != f"diff sha256:{diff_digest}":
                raise SystemExit("code-review-final-handoff-review-snapshot-revision-mismatch")
            # A local review has no retained remote CI run. Keep its status explicit.
            if rows[2]["cells"][1] != "unavailable":
                raise SystemExit("code-review-final-handoff-review-snapshot-ci-mismatch")
            if rows[3]["cells"][1] not in {"fix", "feat", "refactor", "perf", "docs", "ci", "chore", "test", "mixed"}:
                raise SystemExit("code-review-final-handoff-review-snapshot-type-invalid")
        if candidate and snapshot_heading == "PR Snapshot" and out_dir is not None:
            pr = _load_json(out_dir / "pr.json")
            number = pr.get("number")
            title = pr.get("title")
            url = pr.get("url")
            author = pr.get("author")
            login = author.get("login") if isinstance(author, dict) else None
            if (
                type(number) is not int
                or number < 1
                or not isinstance(title, str)
                or not title.strip()
                or not isinstance(url, str)
                or not url.strip()
                or not isinstance(login, str)
                or not login.strip()
            ):
                raise SystemExit("code-review-final-handoff-pr-snapshot-source-invalid")
            expected_values = (
                f"[#{number} — {escape(title, quote=False).replace('[', '&#91;').replace(']', '&#93;')}]({url})",
                f"@{login}",
                _code_review_pr_ci_snapshot(pr.get("statusCheckRollup")),
            )
            types = {"fix", "feat", "refactor", "perf", "docs", "ci", "chore", "test", "mixed"}
            if tuple(row["cells"][1] for row in rows[:3]) != expected_values or rows[3]["cells"][1] not in types:
                raise SystemExit("code-review-final-handoff-pr-snapshot-value-mismatch")
    finding_total = sum(result["findings"].values())
    if expected_branch == "assessed" and finding_total and "Review Findings and Merge Blocks" not in headings:
        raise SystemExit("code-review-final-handoff-findings-table-missing")
    if expected_branch == "assessed" and result.get("schema_version") in {2, 3}:
        records = metadata.get("review_findings")
        blockers = metadata.get("operational_blockers", [])
        if not isinstance(records, list) or not isinstance(blockers, list):
            raise SystemExit("code-review-final-handoff-finding-records-missing")
        if metadata.get("finding_records_version") != 1:
            raise SystemExit("code-review-final-handoff-records-version-missing")
        identities = [record.get("id") if isinstance(record, dict) else None for record in records + blockers]
        if any(not isinstance(identity, str) or not identity.strip() for identity in identities):
            raise SystemExit("code-review-final-handoff-finding-records-invalid")
        table = tables_by_heading.get("Review Findings and Merge Blocks", {})
        if identities and table.get("layout") not in {"grouped", "concise"}:
            raise SystemExit("code-review-final-handoff-grouped-layout-required")
        rows = table.get("rows", [])
        row_identities = [
            row["cells"][0] if isinstance(row, dict) and isinstance(row.get("cells"), list) and row["cells"] else None
            for row in rows
        ]
        if len(row_identities) != len(identities) or set(row_identities) != set(identities):
            raise SystemExit("code-review-final-handoff-finding-identity-mismatch")
        if table.get("layout") in {"grouped", "concise"}:
            by_id = {record["id"]: record for record in records + blockers}
            for row in rows:
                record = by_id[row["cells"][0]]
                if row.get("title") != record.get("title", record["id"]):
                    raise SystemExit("code-review-final-handoff-finding-title-mismatch")
                for field in ("summary", "closure_evidence"):
                    if row.get(field) != record.get(field):
                        raise SystemExit(f"code-review-final-handoff-finding-{field}-mismatch")
                if row.get("authors") != record.get("authors"):
                    raise SystemExit("code-review-final-handoff-finding-authors-mismatch")
                if "required_change" in record and row["cells"][1:3] != [
                    record["required_change"],
                    "; ".join(record["evidence"]),
                ]:
                    raise SystemExit("code-review-final-handoff-finding-content-mismatch")


def _explicit_commit_with_external_limit(
    result: dict[str, Any],
    handoff: dict[str, Any],
    unresolved: dict[str, Any],
    open_items: list[dict[str, Any]],
) -> bool:
    """Allow an explicit commit with only disclosed external verification or review open."""
    external_counts_match = (
        unresolved["selected_items_unresolved"]
        == unresolved["environment_blocked_items"] + unresolved["process_gate_items_unresolved"]
        and unresolved["process_gate_items_unresolved"] == unresolved["external_owner_items"]
    )
    external_owners_match = bool(unresolved["unresolved_reason_groups"]) and all(
        (group["reason"] == "environment-blocked" and group["owner"] == "environment")
        or (group["reason"] == "independent-review" and group["owner"] in {"external-reviewer", "maintainer"})
        for group in unresolved["unresolved_reason_groups"]
    )
    return (
        result["status"] == "fail"
        and not result["checks_failed"]
        and result["findings"]["critical"] == 0
        and all(check["status"] in {"pass", "not-applicable"} for check in handoff["verification"])
        and bool(open_items)
        and unresolved["selected_items_unresolved"] == len(open_items)
        and unresolved["all_local_actionable_items_closed"]
        and unresolved["local_actionable_items_unresolved"] == unresolved["user_deferred_items"] == 0
        and all(
            item["item_type"] in {"review-gate", "confidence-gap"}
            and item["resolution_status"] == "unresolved"
            and item["resolved_how"].startswith("Blocked: ")
            for item in open_items
        )
        and external_counts_match
        and external_owners_match
    )


def _validate_final_handoff(
    result: dict[str, Any], skill: str, out_dir: Path, gates: dict[str, Any], *, candidate: bool = False
) -> None:
    """Validate final-response evidence and current candidates without rewriting historical results."""
    schema_version = result.get("schema_version", 1)
    if schema_version == 1:
        return
    if schema_version != RESULT_SCHEMA_VERSION and not (skill == "code-review" and schema_version == 3):
        raise SystemExit("unsupported-result-schema-version")
    _validate_result_artifact_path(out_dir, result.get("artifact_path"))
    metadata = result.get("metadata")
    if not isinstance(metadata, dict):
        raise SystemExit(f"{skill}-missing-metadata")
    binding = metadata.get("final_handoff")
    if not isinstance(binding, dict):
        raise SystemExit("missing-final-handoff-metadata")
    if set(binding) != FINAL_HANDOFF_METADATA_FIELDS or binding.get("schema_version") != 1:
        raise SystemExit("invalid-final-handoff-metadata")
    paths = {key: _resolve_final_handoff_path(out_dir, binding.get(key), key) for key in FINAL_HANDOFF_FILENAMES}
    helper = Path(__file__).with_name("final_handoff.py")
    completed = subprocess.run(
        [
            sys.executable,
            str(helper),
            "check",
            "--handoff",
            str(paths["handoff_path"]),
            "--final",
            str(paths["rendered_path"]),
            "--validation",
            str(paths["validation_path"]),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip().splitlines()[-1] if completed.stderr.strip() else "unknown"
        raise SystemExit(f"final-handoff-validation-failed:{detail}")
    handoff = _load_json(paths["handoff_path"])
    validation = _load_json(paths["validation_path"])
    if handoff.get("skill") != skill or handoff.get("branch") != binding.get("branch"):
        raise SystemExit("final-handoff-skill-or-branch-mismatch")
    if validation.get("handoff_sha256") != binding.get("handoff_sha256"):
        raise SystemExit("final-handoff-digest-mismatch")
    if validation.get("rendered_sha256") != binding.get("rendered_sha256"):
        raise SystemExit("final-handoff-rendered-digest-mismatch")
    _validate_final_handoff_gates(handoff, gates, skill)
    _validate_final_handoff_confidence(result, handoff, skill)
    artifacts = handoff.get("artifacts")
    if not isinstance(artifacts, list) or result["artifact_path"] not in {
        artifact.get("path") for artifact in artifacts if isinstance(artifact, dict)
    }:
        raise SystemExit(f"{skill}-final-handoff-result-artifact-missing")
    if skill == "code-remediate":
        disposition = handoff.get("commit_disposition")
        if candidate and disposition is None:
            raise SystemExit("remediation-commit-disposition-missing")
        if disposition is not None:
            if disposition["status"] in {"pending", "committed"}:
                if result["status"] != "pass" and not isinstance(metadata.get("unresolved_summary"), dict):
                    raise SystemExit("remediation-commit-result-blocked")
                # Explicit deferment outside the plan is not required closure; inconsistent counts still block.
                _validate_code_remediate_unresolved_summary(metadata, out_dir)
                _validate_code_remediate_scope_selection(metadata, out_dir)
                _validate_code_remediate_final_resolution_table(metadata, out_dir)
                unresolved = metadata["unresolved_summary"]
                selectable = [item for item in metadata["final_resolution_table"]["items"] if item["selectable"]]
                selected_indexes = metadata["resolution_scope"]["selected_indexes"]
                selected_items = [item for index, item in enumerate(selectable, 1) if index in selected_indexes]
                open_items = [
                    item
                    for item in selected_items
                    if item["resolution_status"].strip().casefold()
                    in {
                        "unresolved",
                        "needs-clarification",
                    }
                ]
                if len(selected_items) != len(selected_indexes) or unresolved["selected_items_total"] != len(
                    selected_items
                ):
                    raise SystemExit("remediation-commit-closure-blocked")
                external_limited = _explicit_commit_with_external_limit(result, handoff, unresolved, open_items)
                if result["status"] != "pass" and not external_limited:
                    raise SystemExit("remediation-commit-result-blocked")
                closure_blocked = (
                    unresolved["selected_items_unresolved"] != len(open_items)
                    or any(
                        item["resolution_status"].strip().casefold() != "unresolved"
                        or not item["resolved_how"].startswith("Deferred: ")
                        for item in open_items
                    )
                    or unresolved["selected_items_unresolved"] != unresolved["user_deferred_items"]
                    or not unresolved["all_local_actionable_items_closed"]
                    or any(
                        unresolved[key]
                        for key in (
                            "local_actionable_items_unresolved",
                            "process_gate_items_unresolved",
                            "environment_blocked_items",
                            "external_owner_items",
                        )
                    )
                    or any(
                        group["reason"] != "user-deferred" or group["owner"] != "user"
                        for group in unresolved["unresolved_reason_groups"]
                    )
                )
                if closure_blocked and not external_limited:
                    raise SystemExit("remediation-commit-closure-blocked")
                plan = out_dir / "commit-plan.md"
                if plan.is_symlink() or not plan.is_file():
                    raise SystemExit("remediation-commit-plan-missing")
                if external_limited:
                    _require_commit_plan_sections(plan)
                evidence = _code_remediate_run_path(
                    out_dir, disposition["evidence"], "remediation-commit-evidence-invalid"
                )
                if not evidence.is_file():
                    raise SystemExit("remediation-commit-evidence-invalid")
        _validate_code_remediate_final_handoff(result, handoff)
    elif skill == "code-review":
        _validate_code_review_final_handoff(result, handoff, candidate=candidate, out_dir=out_dir)


def _require_file_sections(path: Path, sections: list[str]) -> None:
    if not path.exists():
        raise SystemExit(f"missing-artifact:{path}")
    text = path.read_text(encoding="utf-8")
    for section in sections:
        if section.lower() not in text.lower():
            raise SystemExit(f"missing-artifact-section:{path.name}:{section}")


def _require_commit_plan_sections(path: Path) -> None:
    """Require written commit and verification context; text cannot prove user authorization."""
    text = path.read_text(encoding="utf-8")
    for heading in ("Explicit Commit Request", "Remaining Verification"):
        matches = list(re.finditer(rf"(?m)^## {re.escape(heading)}[ \t]*$", text))
        if len(matches) != 1:
            raise SystemExit(f"remediation-commit-plan-section-missing:{heading}")
        start = matches[0].end()
        following_heading = re.search(r"(?m)^## ", text[start:])
        body = text[start : start + following_heading.start() if following_heading else len(text)]
        prose = " ".join(line for line in body.splitlines() if line.strip() and not line.lstrip().startswith("#"))
        if len(re.findall(r"\b\w+\b", prose)) < 2:
            raise SystemExit(f"remediation-commit-plan-section-empty:{heading}")


def _validate_jsonl(path: Path) -> None:
    if not path.exists():
        raise SystemExit(f"missing-jsonl:{path}")
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict):
            raise SystemExit(f"jsonl-row-not-object:{path}:{index}")


def _resolve_gate_log(out_dir: Path, recorded: Path) -> Path:
    """Locate one relative gate log without consulting the caller's working directory.

    Every candidate comes from `_anchored_candidates`, and the caller's containment check still rejects anything
    resolving outside the output directory.
    """
    candidates = _anchored_candidates(out_dir, recorded)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def _validate_gates(out_dir: Path) -> dict[str, Any]:
    gates_path = out_dir / "gates.json"
    if not gates_path.exists():
        raise SystemExit("missing-gates-json")
    gates = _load_json(gates_path)
    checks = gates.get("checks")
    if not isinstance(checks, list):
        raise SystemExit("gates-missing-check-details")
    seen_ids: set[str] = set()
    failing_ids: list[str] = []
    for index, check in enumerate(checks):
        if not isinstance(check, dict):
            raise SystemExit(f"gate-check-not-object:{index}")
        for key in ("id", "status", "command_path", "stdout", "stderr", "duration_seconds"):
            if key not in check:
                raise SystemExit(f"gate-check-missing-field:{index}:{key}")
        check_id = check["id"]
        if not isinstance(check_id, str) or check_id not in EXPECTED_GATE_IDS:
            raise SystemExit(f"gate-check-invalid-id:{index}:{check_id!r}")
        if check_id in seen_ids:
            raise SystemExit(f"gate-check-duplicate-id:{check_id}")
        seen_ids.add(check_id)
        if check["status"] not in VALID_GATE_STATUSES:
            raise SystemExit(f"gate-check-invalid-status:{index}:{check['status']!r}")
        if not isinstance(check.get("exit_code"), int):
            raise SystemExit(f"gate-check-invalid-exit-code:{index}")
        if check["status"] in {"missing-command", "not-applicable", "timeout"}:
            reason = check.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                raise SystemExit(f"gate-check-missing-reason:{index}")
        if check["status"] in FAILING_GATE_STATUSES:
            failing_ids.append(check_id)
        expected_exit_codes = {"pass": 0, "missing-command": 127, "not-applicable": 0, "timeout": 124}
        expected_exit = expected_exit_codes.get(check["status"])
        if expected_exit is not None and check["exit_code"] != expected_exit:
            raise SystemExit(f"gate-check-exit-status-mismatch:{index}")
        if check["status"] == "fail" and check["exit_code"] in {0, 124, 127}:
            raise SystemExit(f"gate-check-invalid-fail-exit-code:{index}")
        for key in ("command_path", "stdout", "stderr"):
            path = Path(str(check[key]))
            if not path.is_absolute():
                path = _resolve_gate_log(out_dir, path)
            resolved = path.resolve()
            if not resolved.is_relative_to(out_dir.resolve()):
                raise SystemExit(f"gate-check-log-outside-output:{index}:{key}")
            if path.is_symlink() or not resolved.is_file():
                raise SystemExit(f"gate-check-missing-log:{index}:{key}")
    if seen_ids != EXPECTED_GATE_IDS:
        raise SystemExit("gate-check-id-set-mismatch")
    expected_status = (
        "timeout" if any(check["status"] == "timeout" for check in checks) else "fail" if failing_ids else "pass"
    )
    if gates.get("status") != expected_status:
        raise SystemExit("gates-status-mismatch")
    if gates.get("checks_failed") != failing_ids:
        raise SystemExit("gates-failed-list-mismatch")
    return gates


def _reconcile_result_with_gates(result: dict[str, Any], gates: dict[str, Any]) -> None:
    """Require result status and check fields to include gate outcomes."""
    if set(result["checks_run"]) != EXPECTED_GATE_IDS:
        raise SystemExit("result-checks-run-gate-mismatch")
    gate_failures = set(gates["checks_failed"])
    if not gate_failures.issubset(set(result["checks_failed"])):
        raise SystemExit("result-checks-failed-gate-mismatch")
    if gates["status"] == "fail" and result["status"] == "pass":
        raise SystemExit("result-pass-with-failed-gates")
    if gates["status"] == "timeout" and result["status"] != "timeout":
        raise SystemExit("result-status-timeout-mismatch")


def _validate_code_review_unavailable_gates(result: dict[str, Any], gates: dict[str, Any], skill: str) -> None:
    """Require new unavailable PR reviews to carry explicit unrun-gate records."""
    metadata = result.get("metadata")
    if (
        skill != "code-review"
        or result.get("schema_version") not in {RESULT_SCHEMA_VERSION, 3}
        or not isinstance(metadata, dict)
        or metadata.get("review_status") != "unavailable"
    ):
        return
    checks = gates.get("checks")
    if (
        gates.get("status") != "pass"
        or gates.get("checks_failed") != []
        or not isinstance(checks, list)
        or tuple(check.get("id") if isinstance(check, dict) else None for check in checks)
        != CODE_REVIEW_UNAVAILABLE_GATE_IDS
        or any(
            not isinstance(check, dict)
            or check.get("status") != "not-applicable"
            or check.get("exit_code") != 0
            or not isinstance(check.get("reason"), str)
            or not check["reason"].strip()
            for check in checks
        )
    ):
        raise SystemExit("code-review-unavailable-gates-must-be-not-applicable")


def _validate_confidence_gaps(result: dict[str, Any], skill: str) -> None:
    """Validate confidence gap metadata whenever a confidence score is reported."""
    metadata = result.get("metadata")
    if not isinstance(metadata, dict):
        raise SystemExit(f"{skill}-missing-metadata")
    confidence_gaps = metadata.get("confidence_gaps")
    if not isinstance(confidence_gaps, list) or not all(isinstance(item, str) for item in confidence_gaps):
        raise SystemExit(f"{skill}-invalid-confidence-gaps")
    if float(result["confidence"]) < 1.0 and not any(item.strip() for item in confidence_gaps):
        raise SystemExit(f"{skill}-confidence-gaps-required")
    _validate_confidence_gap_closures(metadata, confidence_gaps, skill)


def _validate_pr_fallback_confidence(
    online_summary: dict[str, Any], result: dict[str, Any], metadata: dict[str, Any]
) -> None:
    """Require explicit evidence limits and cautious confidence after public PR fallback."""
    if online_summary.get("pr_metadata_transport") != "public-https-fallback":
        return
    unavailable = online_summary.get("unavailable_evidence")
    if online_summary.get("limited_data") is not True or not isinstance(unavailable, list) or not unavailable:
        raise SystemExit("code-remediate-pr-public-fallback-limitation-missing")
    if not all(isinstance(item, str) and item for item in unavailable):
        raise SystemExit("code-remediate-pr-public-fallback-limitation-missing")
    if unavailable != sorted(unavailable):
        raise SystemExit("code-remediate-pr-public-fallback-evidence-not-sorted")
    confidence_gap = f"Public HTTPS PR metadata fallback omitted evidence: {', '.join(unavailable)}."
    confidence_gaps = metadata.get("confidence_gaps")
    if not isinstance(confidence_gaps, list) or confidence_gap not in confidence_gaps:
        raise SystemExit("code-remediate-pr-public-fallback-confidence-gap-missing")
    if float(result["confidence"]) > PR_PUBLIC_FALLBACK_MAX_CONFIDENCE:
        raise SystemExit("code-remediate-pr-public-fallback-confidence-cap-exceeded")


def _validate_confidence_gap_closures(metadata: dict[str, Any], confidence_gaps: list[str], skill: str) -> None:
    """Validate that every confidence gap is closed or explicitly carried forward."""
    active_gaps = [gap.strip() for gap in confidence_gaps]
    if any(not gap for gap in active_gaps):
        raise SystemExit(f"{skill}-invalid-confidence-gap")
    if len(active_gaps) != len(set(active_gaps)):
        raise SystemExit(f"{skill}-duplicate-confidence-gap")
    closures = metadata.get("confidence_gap_closures")
    if not active_gaps:
        if closures not in (None, []):
            raise SystemExit(f"{skill}-confidence-gap-closure-undeclared")
        return

    if not isinstance(closures, list):
        raise SystemExit(f"{skill}-missing-confidence-gap-closures")

    closed_gaps: set[str] = set()
    for index, closure in enumerate(closures):
        if not isinstance(closure, dict):
            raise SystemExit(f"{skill}-confidence-gap-closure-not-object:{index}")
        gap = closure.get("gap")
        if not isinstance(gap, str) or not gap.strip():
            raise SystemExit(f"{skill}-confidence-gap-closure-missing-gap:{index}")
        status = closure.get("status")
        if status not in {"closed", "unresolved", "deferred"}:
            raise SystemExit(f"{skill}-confidence-gap-closure-invalid-status:{index}")
        evidence = closure.get("evidence") or closure.get("evidence_path")
        rationale = closure.get("rationale")
        if status == "closed" and not (isinstance(evidence, str) and evidence.strip()):
            raise SystemExit(f"{skill}-confidence-gap-closure-missing-evidence:{index}")
        if status in {"unresolved", "deferred"} and not (isinstance(rationale, str) and rationale.strip()):
            raise SystemExit(f"{skill}-confidence-gap-closure-missing-rationale:{index}")
        normalized_gap = gap.strip()
        if normalized_gap not in active_gaps:
            raise SystemExit(f"{skill}-confidence-gap-closure-undeclared:{index}")
        if normalized_gap in closed_gaps:
            raise SystemExit(f"{skill}-confidence-gap-closure-duplicate:{normalized_gap}")
        closed_gaps.add(normalized_gap)

    missing = sorted(set(active_gaps) - closed_gaps)
    if missing:
        raise SystemExit(f"{skill}-confidence-gap-closure-missing:{','.join(missing)}")


def _require_non_empty_string_list(payload: dict[str, Any], key: str, context: str) -> list[str]:
    """Return a required non-empty list of non-blank strings from a metadata object."""
    value = payload.get(key)
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item.strip() for item in value):
        raise SystemExit(f"{context}-invalid-{key}")
    return value


def _validate_confidence_recovery(result: dict[str, Any], skill: str) -> None:
    """Validate evidence-backed confidence recovery metadata for a skill result."""
    confidence = float(result["confidence"])
    metadata = result.get("metadata")
    if not isinstance(metadata, dict):
        raise SystemExit(f"{skill}-missing-confidence-recovery-metadata")
    recovery = metadata.get("confidence_recovery")
    if not isinstance(recovery, dict):
        raise SystemExit(f"{skill}-missing-confidence-recovery-metadata")

    initial = recovery.get("initial_confidence")
    final = recovery.get("final_confidence")
    if not isinstance(initial, int | float) or not 0.0 <= float(initial) <= 1.0:
        raise SystemExit(f"{skill}-invalid-initial-confidence")
    if not isinstance(final, int | float) or not 0.0 <= float(final) <= 1.0:
        raise SystemExit(f"{skill}-invalid-final-confidence")
    if abs(float(final) - confidence) > 0.001:
        raise SystemExit(f"{skill}-confidence-recovery-final-mismatch")

    status = recovery.get("status")
    if status not in {"fair", "cautious-low", "very-questionable", "not-acceptable-failed"}:
        raise SystemExit(f"{skill}-invalid-confidence-recovery-status")

    _require_non_empty_string_list(recovery, "evidence", skill)
    recovery_actions = _require_non_empty_string_list(recovery, "recovery_actions", skill)
    remaining_limits = recovery.get("remaining_limits")
    if not isinstance(remaining_limits, list) or not all(isinstance(item, str) for item in remaining_limits):
        raise SystemExit(f"{skill}-invalid-remaining-limits")

    if confidence <= 0.8:
        if result["status"] == "pass":
            raise SystemExit(f"{skill}-pass-confidence-not-acceptable")
        if "confidence-not-acceptable" not in result["checks_failed"]:
            raise SystemExit(f"{skill}-missing-confidence-not-acceptable-check")
        if status != "not-acceptable-failed":
            raise SystemExit(f"{skill}-confidence-status-should-fail")
        if not recovery_actions or not remaining_limits:
            raise SystemExit(f"{skill}-low-confidence-recovery-missing")
    elif confidence < 0.85:
        if result["status"] == "pass":
            raise SystemExit(f"{skill}-pass-confidence-very-questionable")
        if "confidence-very-questionable" not in result["checks_failed"]:
            raise SystemExit(f"{skill}-missing-confidence-very-questionable-check")
        if status != "very-questionable":
            raise SystemExit(f"{skill}-confidence-status-should-be-very-questionable")
        if not recovery_actions or not remaining_limits:
            raise SystemExit(f"{skill}-very-questionable-confidence-evidence-missing")
    elif confidence < 0.9:
        if status != "cautious-low":
            raise SystemExit(f"{skill}-confidence-status-should-be-cautious-low")
        if not recovery_actions or not remaining_limits:
            raise SystemExit(f"{skill}-cautious-low-confidence-evidence-missing")
    elif status != "fair":
        raise SystemExit(f"{skill}-confidence-status-should-be-fair")


def _validate_code_remediate_report_intake(
    result: dict[str, Any], out_dir: Path, *, current_contract: bool = False
) -> None:
    """Validate source-aware review report intake metadata for code-remediation artifacts."""
    metadata = result.get("metadata", {})
    if not isinstance(metadata, dict):
        raise SystemExit("code-remediate-missing-metadata")
    intake = metadata.get("review_report_intake")
    if not isinstance(intake, dict):
        raise SystemExit("code-remediate-missing-review-report-intake")

    requested_report = intake.get("requested_report")
    if not isinstance(requested_report, bool):
        raise SystemExit("code-remediate-invalid-review-report-requested")
    if "admission_status" in intake and (
        type(intake.get("schema_version")) is not int or intake["schema_version"] != 1
    ):
        raise SystemExit("code-remediate-report-admission-schema-invalid")
    admission_status = intake.get("admission_status", "completed")
    if admission_status not in {"completed", "preliminary", "unavailable"}:
        raise SystemExit("code-remediate-report-admission-status-invalid")
    if not requested_report and admission_status != "completed":
        raise SystemExit("code-remediate-report-admission-request-missing")
    for key in (
        "report_items_total",
        "review_gate_items_total",
        "review_gate_items_selectable",
        "report_items_marked_out_of_scope",
    ):
        value = intake.get(key)
        if not isinstance(value, int) or value < 0:
            raise SystemExit(f"code-remediate-invalid-review-report-intake:{key}")

    grouped_scope = metadata.get("resolution_scope", {}).get("presentation_version") in {2, 3, 4}
    if grouped_scope:
        # Item classifications, unlike rendered titles, bind report gate obligations to the inventory.
        report_items = [
            item
            for item in metadata["final_resolution_table"]["items"]
            if any(source["kind"] == "report" for source in item["sources"])
        ]
        gate_items = [item for item in report_items if item["item_type"] in {"review-gate", "confidence-gap"}]
        derived = {
            "report_items_total": len(report_items),
            "review_gate_items_total": len(gate_items),
            "review_gate_items_selectable": sum(item["selectable"] for item in gate_items),
            "report_items_marked_out_of_scope": sum(
                item.get("triage_status") == "out-of-scope" for item in report_items
            ),
        }
        if any(intake[key] != value for key, value in derived.items()) or (report_items and not requested_report):
            raise SystemExit("code-remediate-review-intake-inventory-mismatch")
    if not requested_report:
        return
    if admission_status != "completed":
        _validate_code_remediate_incomplete_report_admission(result, out_dir, admission_status)
        return
    if current_contract:
        _validate_code_remediate_report_coverage(metadata, out_dir)

    report_items_total = intake["report_items_total"]
    review_gate_items_total = intake["review_gate_items_total"]
    review_gate_items_selectable = intake["review_gate_items_selectable"]
    if report_items_total < review_gate_items_total:
        raise SystemExit("code-remediate-review-gates-exceed-report-items")
    if not grouped_scope and review_gate_items_total > 0 and review_gate_items_selectable == 0:
        raise SystemExit("code-remediate-review-gates-not-selectable")

    action_text = (out_dir / "action-items.md").read_text(encoding="utf-8").lower()
    scope_text = (out_dir / "resolution-scope.md").read_text(encoding="utf-8").lower()
    if "review report intake" not in action_text:
        raise SystemExit("code-remediate-review-report-intake-section-missing")
    if (
        not grouped_scope
        and review_gate_items_total > 0
        and not any(token in action_text for token in ("checks_failed", "follow_up", "review-gate", "review gate"))
    ):
        raise SystemExit("code-remediate-review-gate-items-missing")
    if (
        not grouped_scope
        and review_gate_items_total > 0
        and "review-gate" not in scope_text
        and "review gate" not in scope_text
    ):
        raise SystemExit("code-remediate-review-gate-scope-missing")


def _validate_code_remediate_user_source(source: dict[str, Any], out_dir: Path) -> None:
    """Keep a supplied finding distinct from online/report evidence and retain its complete body."""
    if not CODE_REMEDIATE_USER_SOURCE_ID.fullmatch(source.get("source_id", "")):
        raise SystemExit("code-remediate-user-source-id-invalid")
    path = _code_remediate_run_path(out_dir, source.get("evidence"), "code-remediate-user-source-evidence-invalid")
    if not path.is_file() or not isinstance(source.get("body"), str) or not source["body"].strip():
        raise SystemExit("code-remediate-user-source-evidence-invalid")
    if " ".join(source["body"].split()) not in " ".join(path.read_text(encoding="utf-8").split()):
        raise SystemExit("code-remediate-user-source-body-mismatch")


def _validate_code_remediate_incomplete_report_admission(
    result: dict[str, Any], out_dir: Path, admission_status: str
) -> None:
    """Bind an open report obligation to selected source, selection, and retained diagnostic."""
    if result.get("status") != "fail":
        raise SystemExit("code-remediate-report-admission-requires-fail")
    metadata = result["metadata"]
    intake = metadata["review_report_intake"]
    evidence = intake.get("admission_evidence")
    if not isinstance(evidence, dict) or metadata.get("mode") not in {"pr", "report"}:
        raise SystemExit("code-remediate-report-admission-evidence-missing")
    routing = None
    if metadata["mode"] == "pr":
        routing = _load_json(out_dir / "pr" / "pr-routing.json")
        if any(
            not isinstance(routing.get(key), str) or not routing[key] for key in ("pr_url", "head_oid", "base_oid")
        ) or any(evidence.get(key) != routing[key] for key in ("pr_url", "head_oid", "base_oid")):
            raise SystemExit("code-remediate-report-admission-evidence-mismatch")
    else:
        _validate_code_remediate_local_report_admission(out_dir, evidence)
        if admission_status != "unavailable":
            raise SystemExit("code-remediate-local-preliminary-report-unsupported")
    diagnostic = _code_remediate_run_path(
        out_dir, evidence.get("diagnostic_path"), "code-remediate-report-admission-evidence-mismatch"
    )
    selection = _code_remediate_run_path(out_dir, "selection.json", "code-remediate-report-admission-evidence-mismatch")
    if (
        not diagnostic.is_file()
        or not diagnostic.read_bytes().strip()
        or hashlib.sha256(diagnostic.read_bytes()).hexdigest() != evidence.get("diagnostic_sha256")
        or not selection.is_file()
        or hashlib.sha256(selection.read_bytes()).hexdigest() != evidence.get("selection_sha256")
    ):
        raise SystemExit("code-remediate-report-admission-evidence-mismatch")
    items = metadata.get("final_resolution_table", {}).get("items", [])
    obligations = [item for item in items if item.get("input_item_id") == evidence.get("open_item_id")]
    if (
        len(obligations) != 1
        or obligations[0].get("item_type") not in {"review-gate", "confidence-gap"}
        or obligations[0].get("resolution_status") not in {"unresolved", "needs-clarification"}
        or obligations[0].get("owner_status") not in {"unresolved", "deferred", "not-selected", "todo"}
        or obligations[0].get("triage_status") not in {"valid", "needs-clarification"}
    ):
        raise SystemExit("code-remediate-report-admission-open-obligation-missing")
    # Missing requested proof stays open even when the user selected only the source fix.
    if admission_status == "unavailable":
        if any(source.get("kind") == "report" for item in items for source in item.get("sources", [])):
            raise SystemExit("code-remediate-report-admission-unavailable-report-sources")
        return
    assert routing is not None
    _validate_code_remediate_preliminary_report_source(out_dir, evidence, routing)
    _validate_code_remediate_report_coverage(metadata, out_dir)


def _validate_code_remediate_local_report_admission(out_dir: Path, evidence: dict[str, Any]) -> None:
    """Bind local intake to the existing pre-edit snapshot and diff frozen with the selection."""
    source = evidence.get("local_source")
    if not isinstance(source, dict):
        raise SystemExit("code-remediate-report-admission-local-source-missing")
    snapshot_path = _code_remediate_run_path(
        out_dir, source.get("snapshot_path"), "code-remediate-report-admission-local-source-invalid"
    )
    diff_path = _code_remediate_run_path(
        out_dir, source.get("diff_path"), "code-remediate-report-admission-local-source-invalid"
    )
    if (
        not snapshot_path.is_file()
        or not diff_path.is_file()
        or hashlib.sha256(snapshot_path.read_bytes()).hexdigest() != source.get("snapshot_sha256")
        or hashlib.sha256(diff_path.read_bytes()).hexdigest() != source.get("diff_sha256")
    ):
        raise SystemExit("code-remediate-report-admission-local-source-invalid")
    snapshot = _load_json(snapshot_path)
    selection = _load_json(out_dir / "selection.json")
    scopes = snapshot.get("scope_paths")
    repository = snapshot.get("repository")
    if (
        type(snapshot.get("schema_version")) is not int
        or snapshot["schema_version"] != 1
        or not isinstance(repository, str)
        or not (PurePosixPath(repository).is_absolute() or PureWindowsPath(repository).is_absolute())
        or re.fullmatch(r"[0-9a-f]{40,64}", str(snapshot.get("revision"))) is None
        or re.fullmatch(r"[0-9a-f]{64}", str(snapshot.get("index_sha256"))) is None
        or not isinstance(scopes, list)
        or not scopes
        or any(
            not isinstance(path, str)
            or not path
            or PurePosixPath(path).is_absolute()
            or PureWindowsPath(path).is_absolute()
            or ".." in PurePosixPath(path).parts
            for path in scopes
        )
        or not isinstance(snapshot.get("files"), list)
        or any(source.get(key) != snapshot[key] for key in ("repository", "revision", "scope_paths"))
        or selection.get("local_source") != source
    ):
        raise SystemExit("code-remediate-report-admission-local-source-invalid")


def _validate_code_remediate_preliminary_report_source(
    out_dir: Path, evidence: dict[str, Any], routing: dict[str, Any]
) -> None:
    """Check original canonical records match this PR diff without certifying producer completion."""
    original_pr = _code_remediate_run_path(
        out_dir, evidence.get("original_pr_path"), "code-remediate-report-admission-original-source-invalid"
    )
    original_input = _code_remediate_run_path(
        out_dir, "findings-input.txt", "code-remediate-report-admission-original-source-invalid"
    )
    if (
        not original_pr.is_file()
        or not original_input.is_file()
        or hashlib.sha256(original_pr.read_bytes()).hexdigest() != evidence.get("original_pr_sha256")
        or hashlib.sha256(original_input.read_bytes()).hexdigest() != evidence.get("original_input_sha256")
    ):
        raise SystemExit("code-remediate-report-admission-original-source-invalid")
    pr = _load_json(original_pr)
    original = _load_json(original_input).get("metadata", {})
    if (
        pr.get("url") != routing["pr_url"]
        or pr.get("headRefOid") != routing["head_oid"]
        or pr.get("baseRefOid") != routing["base_oid"]
        or not isinstance(original, dict)
        or original.get("scope") != "pr"
        or not original.get("review_findings")
        or original.get("review_input_sha256")
        != hashlib.sha256((out_dir / "pr" / "diff.patch").read_bytes()).hexdigest()
    ):
        raise SystemExit("code-remediate-report-admission-original-source-invalid")
    records = original["review_findings"]
    if not isinstance(records, list) or any(
        not isinstance(record, dict)
        or record.get("severity") not in {"critical", "high", "medium", "low"}
        or any(
            not isinstance(record.get(key), str) or not record[key].strip()
            for key in ("id", "title", "summary", "required_change", "closure_evidence")
        )
        or any(
            not isinstance(record.get(key), list)
            or not record[key]
            or any(not isinstance(value, str) or not value.strip() for value in record[key])
            for key in ("evidence", "authors")
        )
        for record in records
    ):
        raise SystemExit("code-remediate-report-admission-canonical-records-required")


def _validate_code_remediate_report_coverage(metadata: dict[str, Any], out_dir: Path) -> None:
    """Bind report intake to the retained original findings and evidence obligations."""
    path = out_dir / "findings-input.txt"
    if path.is_symlink() or not path.is_file():
        raise SystemExit("code-remediate-report-input-missing")
    try:
        report = _load_json(path)
    except (ValueError, UnicodeError) as error:
        raise SystemExit("code-remediate-report-input-invalid") from error
    original = report.get("metadata")
    if (
        report.get("schema_version") != 3
        or not isinstance(original, dict)
        or original.get("review_status", "assessed") != "assessed"
    ):
        raise SystemExit("code-remediate-report-input-not-assessed")
    items = metadata["final_resolution_table"]["items"]
    report_sources = [source for item in items for source in item["sources"] if source["kind"] == "report"]
    for field in ("review_findings", "operational_blockers"):
        records = original.get(field, [])
        if not isinstance(records, list):
            raise SystemExit(f"code-remediate-report-input-invalid:{field}")
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("id"), str) or not record["id"].strip():
                raise SystemExit(f"code-remediate-report-input-invalid:{field}")
            identity = record["id"]
            sources = [
                source
                for item in items
                for source in item["sources"]
                if source["kind"] == "report"
                and (
                    source.get("finding_id") == identity
                    or source["source_id"].partition("#")[2] == identity
                    or item["input_item_id"] == identity
                )
            ]
            if not sources:
                raise SystemExit(f"code-remediate-report-finding-omitted:{identity}")
            bodies = "\n".join(source["body"] for source in sources)
            # IDs alone cannot preserve the finding's original evidence and closure contract.
            for detail in ("title", "summary", "required_change", "closure_evidence", "evidence"):
                value = record.get(detail, [])
                texts = [value] if isinstance(value, str) else value
                if not isinstance(texts, list) or any(not isinstance(text, str) for text in texts):
                    raise SystemExit(f"code-remediate-report-input-invalid:{field}:{detail}")
                if any(" ".join(text.split()) not in " ".join(bodies.split()) for text in texts):
                    raise SystemExit(f"code-remediate-report-finding-detail-omitted:{identity}:{detail}")
    decision = original.get("review_decision", {})
    recovery = original.get("confidence_recovery", {})
    if not isinstance(decision, dict) or not isinstance(recovery, dict):
        raise SystemExit("code-remediate-report-input-invalid")
    obligations = {
        "checks_failed": report.get("checks_failed", []),
        "follow_up": report.get("follow_up", []),
        "confidence_gaps": original.get("confidence_gaps", []),
        "required_next_work": decision.get("required_next_work", []),
        "remaining_limits": recovery.get("remaining_limits", []),
    }
    for field, records in obligations.items():
        if not isinstance(records, list) or any(not isinstance(text, str) or not text.strip() for text in records):
            raise SystemExit(f"code-remediate-report-input-invalid:{field}")
        for text in records:
            if not any(" ".join(text.split()) in " ".join(source["body"].split()) for source in report_sources):
                raise SystemExit(f"code-remediate-report-obligation-omitted:{field}")


def _validate_code_remediate_scope_selection(metadata: dict[str, Any], out_dir: Path) -> None:
    """Validate user-confirmed code-remediation scope selection metadata."""
    resolution_scope = metadata.get("resolution_scope")
    if not isinstance(resolution_scope, dict):
        raise SystemExit("code-remediate-missing-resolution-scope-metadata")

    selection_source = resolution_scope.get("selection_source")
    if selection_source not in {"explicit-input", "user-prompt", "none-selectable"}:
        raise SystemExit("code-remediate-invalid-selection-source")
    prompt_presented = resolution_scope.get("prompt_presented")
    if not isinstance(prompt_presented, bool):
        raise SystemExit("code-remediate-invalid-prompt-presented")
    selection_confirmed = resolution_scope.get("selection_confirmed_by_user")
    if not isinstance(selection_confirmed, bool):
        raise SystemExit("code-remediate-invalid-selection-confirmation")

    selected_indexes = resolution_scope.get("selected_indexes")
    deferred_indexes = resolution_scope.get("deferred_indexes")
    selected_groups = resolution_scope.get("selected_severity_groups")
    if not isinstance(selected_indexes, list) or not all(isinstance(item, int) for item in selected_indexes):
        raise SystemExit("code-remediate-invalid-selected-indexes")
    if not isinstance(deferred_indexes, list) or not all(isinstance(item, int) for item in deferred_indexes):
        raise SystemExit("code-remediate-invalid-deferred-indexes")
    if not isinstance(selected_groups, list) or not all(isinstance(item, str) for item in selected_groups):
        raise SystemExit("code-remediate-invalid-selected-severity-groups")

    if resolution_scope.get("presentation_version") in {2, 3, 4}:
        _validate_grouped_selection(metadata, out_dir)
        return

    scope_text = (out_dir / "resolution-scope.md").read_text(encoding="utf-8").lower()
    has_selectable = "none-selectable" not in scope_text and "selectable: 0" not in scope_text
    if has_selectable and selection_source == "none-selectable":
        raise SystemExit("code-remediate-selection-source-incorrectly-none-selectable")
    if has_selectable and selection_source == "user-prompt" and not (prompt_presented and selection_confirmed):
        raise SystemExit("code-remediate-user-prompt-not-confirmed")
    if has_selectable and selection_source == "explicit-input" and not selection_confirmed:
        raise SystemExit("code-remediate-explicit-selection-not-confirmed")
    if has_selectable and selection_source not in {"explicit-input", "user-prompt"}:
        raise SystemExit("code-remediate-selection-required")
    if not has_selectable:
        return

    headers, rows = _parse_markdown_table(out_dir / "resolution-scope.md", "Resolution Scope Selection")
    expected_headers = [
        "index",
        "severity",
        "item id or source location",
        "source",
        "summary",
        "expected closure evidence",
    ]
    if headers != expected_headers or not rows:
        raise SystemExit("code-remediate-scope-table-invalid")
    source_index = headers.index("source")
    normalized_scope_lines = {
        re.sub(r"\s+", " ", line.replace(r"\|", "|")).strip()
        for line in (out_dir / "resolution-scope.md").read_text(encoding="utf-8").splitlines()
    }
    for row in rows:
        source_cell = row[source_index]
        source_refs = re.findall(r"(?:report|online|user) \[[^\]\r\n]+\]", source_cell)
        if not source_refs or " ".join(source_refs) != source_cell:
            raise SystemExit("code-remediate-scope-source-not-compact")
        for source_ref in source_refs:
            kind, source_id = source_ref.split(" [", maxsplit=1)
            source_id = source_id.removesuffix("]")
            if kind == "report" and not CODE_REMEDIATE_REPORT_SOURCE_ID.fullmatch(source_id):
                raise SystemExit("code-remediate-scope-report-source-id-invalid")
            if kind == "online" and not CODE_REMEDIATE_ONLINE_SOURCE_ID.fullmatch(source_id):
                raise SystemExit("code-remediate-scope-online-source-id-invalid")
        index = row[headers.index("index")]
        summary_ref = f"[S{index}]"
        closure_ref = f"[C{index}]"
        if (
            row[headers.index("summary")] != summary_ref
            or row[headers.index("expected closure evidence")] != closure_ref
        ):
            raise SystemExit("code-remediate-scope-detail-reference-invalid")
        if not any(line.startswith(f"{summary_ref} ") for line in normalized_scope_lines) or not any(
            line.startswith(f"{closure_ref} ") for line in normalized_scope_lines
        ):
            raise SystemExit("code-remediate-scope-symbol-detail-missing")


def _validate_grouped_selection(metadata: dict[str, Any], out_dir: Path) -> None:
    """Reconcile a rendered pre-edit inventory with confirmed scope and final source ownership."""
    inventory_path = out_dir / "selection.json"
    if inventory_path.is_symlink() or not inventory_path.is_file():
        raise SystemExit("code-remediate-selection-inventory-missing")
    completed = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("final_handoff.py")),
            "selection",
            "--input",
            str(inventory_path),
            "--out-scope",
            str(out_dir / "resolution-scope.md"),
            "--check",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode:
        raise SystemExit("code-remediate-selection-invalid:" + completed.stderr.strip())
    inventory = _load_json(inventory_path)
    if metadata.get("mode") == "pr" and inventory.get("pr_relevance") != metadata.get("pr_relevance"):
        raise SystemExit("code-remediate-selection-pr-relevance-mismatch")
    scope = metadata["resolution_scope"]
    if inventory.get("presentation_version", 2) != scope["presentation_version"]:
        raise SystemExit("code-remediate-selection-presentation-version-mismatch")
    selected = inventory.get("selected_indexes")
    selectable = [item for item in inventory["items"] if item["selectable"]]
    if (
        selected is None
        or selected != scope.get("selected_indexes")
        or (selectable and not scope.get("selection_confirmed_by_user"))
    ):
        raise SystemExit("code-remediate-selection-not-confirmed")
    expected_deferred = [index for index in range(1, len(selectable) + 1) if index not in selected]
    if scope.get("deferred_indexes") != expected_deferred:
        raise SystemExit("code-remediate-selection-deferred-mismatch")
    if selectable and scope.get("selection_source") == "none-selectable":
        raise SystemExit("code-remediate-selection-source-incorrectly-none-selectable")
    if scope.get("selection_source") == "user-prompt" and not scope.get("prompt_presented"):
        raise SystemExit("code-remediate-user-prompt-not-confirmed")
    final_items = metadata.get("final_resolution_table", {}).get("items", [])
    # Only outcomes may change after selection: names, IDs, severity and provenance stay bound.
    fields = ("input_item_id", "item_name", "item_type", "severity", "selectable", "sources")
    before = [{field: item.get(field) for field in fields} for item in inventory["items"]]
    after = [{field: item.get(field) for field in fields} for item in final_items]
    if before != after:
        raise SystemExit("code-remediate-selection-final-inventory-mismatch")


def _code_remediate_run_path(out_dir: Path, value: object, error_code: str) -> Path:
    """Resolve one run-relative production artifact without accepting symlink components."""
    if not isinstance(value, str) or not value or "\\" in value or value.startswith("/") or ":" in value:
        raise SystemExit(error_code)
    parts = PurePosixPath(value).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise SystemExit(error_code)
    current = out_dir
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise SystemExit(error_code)
    path = out_dir.joinpath(*parts)
    try:
        path.resolve().relative_to(out_dir.resolve())
    except (OSError, ValueError) as error:
        raise SystemExit(error_code) from error
    return path


def _validate_code_remediate_production_lifecycle(
    workplan: dict[str, Any], bucket_plan: dict[str, Any], out_dir: Path, bucket_plan_sha256: str
) -> None:
    """Reconcile completed parallel remediation with its schema-v2 lifecycle evidence."""
    reference = workplan.get("production_lifecycle")
    if not isinstance(reference, dict):
        raise SystemExit("code-remediate-production-lifecycle-required")
    if set(reference) != {"path", "sha256", "status"} or reference.get("status") != "completed":
        raise SystemExit("code-remediate-production-lifecycle-reference-invalid")
    value = reference.get("path")
    lifecycle_path = _code_remediate_run_path(out_dir, value, "code-remediate-production-lifecycle-path-invalid")
    if not lifecycle_path.is_file():
        raise SystemExit("code-remediate-production-lifecycle-evidence-missing")
    digest = reference.get("sha256")
    if not isinstance(digest, str) or digest != hashlib.sha256(lifecycle_path.read_bytes()).hexdigest():
        raise SystemExit("code-remediate-production-lifecycle-digest-mismatch")
    lifecycle = _load_json(lifecycle_path)
    if lifecycle.get("schema_version") != 2 or lifecycle.get("consumer") != "code-remediate":
        raise SystemExit("code-remediate-production-lifecycle-schema-invalid")
    if lifecycle.get("status") != "completed" or lifecycle.get("plan_sha256") != bucket_plan_sha256:
        raise SystemExit("code-remediate-production-lifecycle-plan-mismatch")
    if bucket_plan.get("schema_version") != 2 or bucket_plan.get("consumer") != "code-remediate":
        raise SystemExit("code-remediate-production-lifecycle-plan-schema-invalid")
    if bucket_plan.get("write_parallel_promoted") is not False:
        raise SystemExit("code-remediate-production-lifecycle-promotion-invalid")
    buckets = bucket_plan.get("work_buckets")
    if not isinstance(buckets, list) or not 2 <= len(buckets) <= 4:
        raise SystemExit("code-remediate-production-lifecycle-bucket-count-invalid")
    expected_nodes = [bucket.get("bucket_id") for bucket in buckets if isinstance(bucket, dict)]
    expected_paths = sorted({path for bucket in buckets for path in bucket["owned_paths"]})
    evidence_root = lifecycle.get("evidence_root")
    evidence_parts = PurePosixPath(evidence_root).parts if isinstance(evidence_root, str) else ()
    if (
        not isinstance(evidence_root, str)
        or not evidence_root
        or "\\" in evidence_root
        or evidence_root.startswith("/")
        or evidence_parts[:3] != (".reports", "codex", "code-remediate")
        or len(evidence_parts) != 4
        or any(part == ".." for part in evidence_parts)
        or ":" in evidence_root
    ):
        raise SystemExit("code-remediate-production-lifecycle-evidence-root-invalid")
    planned_state = bucket_plan.get("state_path")
    if (
        not isinstance(planned_state, str)
        or not planned_state
        or "/" in planned_state
        or "\\" in planned_state
        or lifecycle.get("state_path") != f"{evidence_root}/{planned_state}"
    ):
        raise SystemExit("code-remediate-production-lifecycle-state-path-mismatch")
    if (
        bucket_plan.get("verification_gate") != "code-remediate-shared-quality-gates"
        or lifecycle.get("verification_gate") != "code-remediate-shared-quality-gates"
    ):
        raise SystemExit("code-remediate-production-lifecycle-verification-gate-mismatch")
    for bucket in buckets:
        context_path = _code_remediate_run_path(
            out_dir,
            bucket.get("context_pack_path"),
            "code-remediate-production-lifecycle-context-path-invalid",
        )
        context_digest = bucket.get("context_sha256")
        if (
            not context_path.is_file()
            or not isinstance(context_digest, str)
            or context_digest != hashlib.sha256(context_path.read_bytes()).hexdigest()
        ):
            raise SystemExit("code-remediate-production-lifecycle-context-mismatch")
    recorded_nodes = lifecycle.get("nodes")
    if (
        not isinstance(recorded_nodes, list)
        or [node.get("node_id") for node in recorded_nodes if isinstance(node, dict)] != expected_nodes
    ):
        raise SystemExit("code-remediate-production-lifecycle-node-mismatch")
    for node, bucket in zip(recorded_nodes, buckets, strict=True):
        output = bucket.get("output")
        if (
            not isinstance(node, dict)
            or node.get("owned_paths") != bucket["owned_paths"]
            or not isinstance(output, str)
            or not output
            or "/" in output
            or "\\" in output
            or node.get("patch_path") != f"{evidence_root}/{output}"
        ):
            raise SystemExit("code-remediate-production-lifecycle-child-patch-path-invalid")
        patch_path = _code_remediate_run_path(
            out_dir, output, "code-remediate-production-lifecycle-child-patch-path-invalid"
        )
        if (
            patch_path.is_symlink()
            or not patch_path.is_file()
            or node.get("patch_sha256") != hashlib.sha256(patch_path.read_bytes()).hexdigest()
        ):
            raise SystemExit("code-remediate-production-lifecycle-child-patch-mismatch")
    nodes = lifecycle.get("joined_nodes")
    if (
        not isinstance(nodes, list)
        or [node.get("node_id") for node in nodes if isinstance(node, dict)] != expected_nodes
    ):
        raise SystemExit("code-remediate-production-lifecycle-node-mismatch")
    for node, recorded, bucket in zip(nodes, recorded_nodes, buckets, strict=True):
        if (
            not isinstance(node, dict)
            or node.get("owned_paths") != bucket["owned_paths"]
            or not isinstance(node.get("patch_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", node["patch_sha256"]) is None
            or node["patch_sha256"] != recorded["patch_sha256"]
        ):
            raise SystemExit("code-remediate-production-lifecycle-node-mismatch")
    integration = lifecycle.get("integration")
    if not isinstance(integration, dict) or integration != {
        "status": "structurally-verified",
        "order": expected_nodes,
        "paths": expected_paths,
    }:
        raise SystemExit("code-remediate-production-lifecycle-integration-mismatch")
    reconciliation = lifecycle.get("reconciliation")
    failed_node = lifecycle.get("integration_failed_node")
    conflict_paths = lifecycle.get("integration_conflict_paths")
    if reconciliation is None:
        if failed_node is not None or conflict_paths is not None:
            raise SystemExit("code-remediate-production-lifecycle-reconciliation-required")
    else:
        error = "code-remediate-production-lifecycle-reconciliation-mismatch"
        final_hashes = lifecycle.get("integration_final_sha256")
        if (
            not isinstance(reconciliation, dict)
            or set(reconciliation)
            != {"status", "record_path", "record_sha256", "changed_paths", "failed_node", "postimage_sha256"}
            or reconciliation.get("status") != "verified"
            or failed_node not in expected_nodes
            or not isinstance(conflict_paths, list)
            or not conflict_paths
            or any(path not in expected_paths for path in conflict_paths)
            or reconciliation.get("failed_node") != failed_node
            or reconciliation.get("changed_paths") != expected_paths
            or not isinstance(final_hashes, dict)
            or sorted(final_hashes) != expected_paths
            or any(
                not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
                for value in final_hashes.values()
            )
            or reconciliation.get("postimage_sha256") != final_hashes
        ):
            raise SystemExit(error)
        record_path = _code_remediate_run_path(out_dir, reconciliation.get("record_path"), error)
        if (
            record_path.is_symlink()
            or not record_path.is_file()
            or reconciliation.get("record_sha256") != hashlib.sha256(record_path.read_bytes()).hexdigest()
        ):
            raise SystemExit(error)
        record = _load_json(record_path)
        if (
            set(record)
            != {
                "baseline_head",
                "failed_node",
                "integration_order",
                "paths",
                "patch_sha256",
                "postimage_sha256",
                "summary",
            }
            or record.get("baseline_head") != bucket_plan.get("baseline_head")
            or record.get("failed_node") != failed_node
            or record.get("integration_order") != expected_nodes
            or record.get("paths") != expected_paths
            or record.get("patch_sha256") != {node["node_id"]: node["patch_sha256"] for node in recorded_nodes}
            or record.get("postimage_sha256") != final_hashes
            or not isinstance(record.get("summary"), str)
            or not record["summary"].strip()
            or len(record["summary"]) > 2_000
        ):
            raise SystemExit(error)
    source = lifecycle.get("source")
    if (
        not isinstance(source, dict)
        or source.get("baseline_head") != bucket_plan.get("baseline_head")
        or source.get("baseline_tree") != bucket_plan.get("baseline_tree")
        or source.get("applied_head") != source.get("baseline_head")
    ):
        raise SystemExit("code-remediate-production-lifecycle-source-mismatch")
    for key in ("preimage_sha256", "postimage_sha256"):
        hashes = source.get(key)
        if (
            not isinstance(hashes, dict)
            or sorted(hashes) != expected_paths
            or any(
                not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None for value in hashes.values()
            )
        ):
            raise SystemExit("code-remediate-production-lifecycle-source-hash-mismatch")
    application = lifecycle.get("source_application")
    if (
        not isinstance(application, dict)
        or application.get("status") != "applied"
        or application.get("applied_paths") != expected_paths
    ):
        raise SystemExit("code-remediate-production-lifecycle-source-application-mismatch")
    for path_key, digest_key, path_error, mismatch_error in (
        (
            "patch_path",
            "patch_sha256",
            "code-remediate-production-lifecycle-source-patch-path-invalid",
            "code-remediate-production-lifecycle-source-patch-mismatch",
        ),
        (
            "rollback_patch_path",
            "rollback_patch_sha256",
            "code-remediate-production-lifecycle-rollback-path-invalid",
            "code-remediate-production-lifecycle-rollback-mismatch",
        ),
    ):
        patch_path = _code_remediate_run_path(out_dir, application.get(path_key), path_error)
        if (
            patch_path.is_symlink()
            or not patch_path.is_file()
            or application.get(digest_key) != hashlib.sha256(patch_path.read_bytes()).hexdigest()
        ):
            raise SystemExit(mismatch_error)
    cleanup = lifecycle.get("cleanup")
    if not isinstance(cleanup, dict) or cleanup.get("status") != "removed" or cleanup.get("force") is not False:
        raise SystemExit("code-remediate-production-lifecycle-cleanup-mismatch")
    if lifecycle.get("containment") != {
        "mode": "parent-authoritative-worktrees",
        "capability_sandbox_verified": False,
    }:
        raise SystemExit("code-remediate-production-lifecycle-containment-mismatch")


# ``NamedTuple``, not ``@dataclass``: callers load this validator by file path without registering it in
# ``sys.modules``, and under ``from __future__ import annotations`` every field annotation reaches
# ``dataclasses._is_type``, which dereferences ``sys.modules[cls.__module__]`` and raises on the missing entry.
class _WorkplanApproval(NamedTuple):
    """Parallel-approval fields declared by the resolution workplan metadata."""

    status: str
    source: str
    response: str
    bucket_plan_sha256: str
    approved_plan_sha256: str | None


class _WorkBucketObservations(NamedTuple):
    """Counts and identifiers accumulated while validating the declared work buckets."""

    indexes: list[int]
    parent_groups: int
    specialist_groups: int
    singleton_specialist_groups: int
    verifier_groups: int
    parallel_bucket_count: int
    bucket_ids: set[str]


def _validate_workplan_scalar_fields(workplan: dict[str, Any]) -> None:
    """Check the workplan's group counters, execution mode, and approval-state scalars."""
    for key in (
        "groups_total",
        "parent_owned_groups",
        "specialist_owned_groups",
        "verifier_groups",
        "unassigned_selected_items",
    ):
        value = workplan.get(key)
        if not isinstance(value, int) or value < 0:
            raise SystemExit(f"code-remediate-invalid-resolution-workplan:{key}")

    if workplan.get("max_items_per_bucket") != 5:
        raise SystemExit("code-remediate-invalid-resolution-workplan:max_items_per_bucket")
    if workplan.get("execution_mode") not in {"parent-owned", "sequential-specialists", "parallel-specialists"}:
        raise SystemExit("code-remediate-invalid-resolution-workplan:execution_mode")
    for key in ("parallel_eligible", "parallel_approval_required", "parallel_prompt_presented"):
        if not isinstance(workplan.get(key), bool):
            raise SystemExit(f"code-remediate-invalid-resolution-workplan:{key}")
    if workplan.get("parallel_approval_status") not in {"not-required", "approved", "parent-only"}:
        raise SystemExit("code-remediate-invalid-resolution-workplan:parallel_approval_status")
    if workplan.get("parallel_approval_source") not in {
        "not-required",
        "explicit-input",
        "user-prompt",
        "workflow-default",
    }:
        raise SystemExit("code-remediate-invalid-resolution-workplan:parallel_approval_source")


def _validate_workplan_paths_and_digests(workplan: dict[str, Any]) -> _WorkplanApproval:
    """Check the declared artifact paths and digests, returning the parallel-approval fields."""
    workplan_path_value = workplan.get("workplan_path")
    if not isinstance(workplan_path_value, str) or not workplan_path_value.strip():
        raise SystemExit("code-remediate-invalid-resolution-workplan:workplan_path")

    bucket_plan_path_value = workplan.get("bucket_plan_path")
    approval_path_value = workplan.get("parallel_approval_path")
    bucket_plan_sha256 = workplan.get("bucket_plan_sha256")
    approval_response = workplan.get("parallel_approval_response")
    approved_plan_sha256 = workplan.get("approved_plan_sha256")
    if not isinstance(bucket_plan_path_value, str) or Path(bucket_plan_path_value).name != "work-bucket-plan.json":
        raise SystemExit("code-remediate-work-bucket-plan-path-invalid")
    if not isinstance(approval_path_value, str) or Path(approval_path_value).name != "parallel-approval.json":
        raise SystemExit("code-remediate-parallel-approval-path-invalid")
    if not isinstance(bucket_plan_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", bucket_plan_sha256) is None:
        raise SystemExit("code-remediate-work-bucket-plan-digest-invalid")
    if approval_response not in {"not-required", "approve", "parent-only"}:
        raise SystemExit("code-remediate-parallel-approval-response-invalid")
    if approved_plan_sha256 is not None and (
        not isinstance(approved_plan_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", approved_plan_sha256) is None
    ):
        raise SystemExit("code-remediate-approved-plan-digest-invalid")
    return _WorkplanApproval(
        status=workplan["parallel_approval_status"],
        source=workplan["parallel_approval_source"],
        response=approval_response,
        bucket_plan_sha256=bucket_plan_sha256,
        approved_plan_sha256=approved_plan_sha256,
    )


def _validate_workplan_shape(metadata: dict[str, Any]) -> tuple[dict[str, Any], list[int], _WorkplanApproval]:
    """Check resolution-scope and workplan metadata shape before any bucket is inspected."""
    resolution_scope = metadata.get("resolution_scope")
    if not isinstance(resolution_scope, dict):
        raise SystemExit("code-remediate-missing-resolution-scope-metadata")
    selected_indexes = resolution_scope.get("selected_indexes")
    if not isinstance(selected_indexes, list) or not all(isinstance(item, int) for item in selected_indexes):
        raise SystemExit("code-remediate-invalid-selected-indexes")

    workplan = metadata.get("resolution_workplan")
    if not isinstance(workplan, dict):
        raise SystemExit("code-remediate-missing-resolution-workplan")

    _validate_workplan_scalar_fields(workplan)

    if not isinstance(workplan.get("work_buckets"), list):
        raise SystemExit("code-remediate-invalid-resolution-workplan:work_buckets")

    approval = _validate_workplan_paths_and_digests(workplan)

    if len(selected_indexes) != len(set(selected_indexes)):
        raise SystemExit("code-remediate-selected-indexes-not-unique")
    return workplan, selected_indexes, approval


def _validate_workplan_evidence_files(
    out_dir: Path,
    workplan: dict[str, Any],
    approval: _WorkplanApproval,
) -> dict[str, Any]:
    """Match the on-disk bucket plan and approval record against the declared metadata."""
    bucket_plan_path = out_dir / "work-bucket-plan.json"
    approval_path = out_dir / "parallel-approval.json"
    bucket_plan = _load_json(bucket_plan_path)
    approval_record = _load_json(approval_path)
    if bucket_plan.get("work_buckets") != workplan["work_buckets"]:
        raise SystemExit("code-remediate-work-bucket-plan-content-mismatch")
    if bucket_plan.get("schema_version") not in {1, 2}:
        raise SystemExit("code-remediate-work-bucket-plan-content-mismatch")
    observed_plan_sha256 = hashlib.sha256(bucket_plan_path.read_bytes()).hexdigest()
    if approval.bucket_plan_sha256 != observed_plan_sha256:
        raise SystemExit("code-remediate-work-bucket-plan-digest-mismatch")
    expected_approval = {
        "plan_sha256": approval.bucket_plan_sha256,
        "prompt_presented": workplan["parallel_prompt_presented"],
        "response": approval.response,
        "source": approval.source,
    }
    if approval_record != expected_approval:
        raise SystemExit("code-remediate-parallel-approval-evidence-mismatch")
    return bucket_plan


def _validate_workplan_aggregate_counts(workplan: dict[str, Any], work_buckets: list[Any]) -> None:
    """Cross-check the workplan's declared group counters against the bucket list length."""
    if workplan["groups_total"] <= 0:
        raise SystemExit("code-remediate-selected-items-without-workplan-groups")
    if workplan["unassigned_selected_items"] != 0:
        raise SystemExit("code-remediate-selected-items-unassigned-in-workplan")
    if workplan["parent_owned_groups"] + workplan["specialist_owned_groups"] != workplan["groups_total"]:
        raise SystemExit("code-remediate-workplan-owner-count-mismatch")
    if workplan["verifier_groups"] > workplan["groups_total"]:
        raise SystemExit("code-remediate-workplan-verifier-count-exceeds-groups")
    if workplan["groups_total"] != len(work_buckets):
        raise SystemExit("code-remediate-work-bucket-count-mismatch")


def _validate_work_bucket_fields(bucket: Any, position: int, bucket_ids: set[str]) -> list[int]:
    """Check one bucket's scalar fields and index list, returning its selected indexes."""
    if not isinstance(bucket, dict):
        raise SystemExit(f"code-remediate-work-bucket-not-object:{position}")
    for key in ("bucket_id", "owner", "verifier", "context_pack_path", "execution_mode"):
        value = bucket.get(key)
        if not isinstance(value, str) or not value.strip():
            raise SystemExit(f"code-remediate-work-bucket-invalid-{key}:{position}")
    bucket_id = bucket["bucket_id"]
    if bucket_id in bucket_ids:
        raise SystemExit("code-remediate-work-bucket-id-duplicate")
    bucket_ids.add(bucket_id)
    if bucket["owner"] not in CODE_REMEDIATE_WORK_BUCKET_OWNERS:
        raise SystemExit(f"code-remediate-work-bucket-owner-unsupported:{position}")
    if bucket["verifier"] not in CODE_REMEDIATE_WORK_BUCKET_VERIFIERS:
        raise SystemExit(f"code-remediate-work-bucket-verifier-unsupported:{position}")
    bucket_indexes = bucket.get("selected_indexes")
    if not isinstance(bucket_indexes, list) or not all(isinstance(item, int) for item in bucket_indexes):
        raise SystemExit(f"code-remediate-work-bucket-invalid-selected-indexes:{position}")
    if not bucket_indexes:
        raise SystemExit(f"code-remediate-work-bucket-empty:{position}")
    if len(bucket_indexes) > 5:
        raise SystemExit("code-remediate-work-bucket-too-large")
    if len(bucket_indexes) != len(set(bucket_indexes)):
        raise SystemExit("code-remediate-work-bucket-duplicate-index")
    return bucket_indexes


def _validate_bucket_owned_paths_and_mode(bucket: dict[str, Any], position: int) -> tuple[list[str], str]:
    """Check one bucket's owned-path list and execution mode, returning both."""
    owned_paths = bucket.get("owned_paths")
    if (
        not isinstance(owned_paths, list)
        or not owned_paths
        or not all(isinstance(path, str) and path.strip() for path in owned_paths)
    ):
        raise SystemExit(f"code-remediate-work-bucket-invalid-owned-paths:{position}")
    bucket_mode = bucket["execution_mode"]
    if bucket_mode not in {"parent", "sequential", "parallel"}:
        raise SystemExit(f"code-remediate-work-bucket-invalid-execution-mode:{position}")
    return owned_paths, bucket_mode


def _validate_specialist_context_pack(bucket: dict[str, Any], out_dir: Path) -> None:
    """Confirm a specialist bucket's context pack exists inside the run directory."""
    context_path = Path(bucket["context_pack_path"])
    resolved_context_path = context_path if context_path.is_absolute() else out_dir / context_path
    try:
        resolved_context_path.resolve().relative_to(out_dir.resolve())
    except ValueError as error:
        raise SystemExit("code-remediate-specialist-context-outside-run-directory") from error
    if not resolved_context_path.is_file():
        raise SystemExit("code-remediate-specialist-context-pack-missing")


def _validate_parallel_bucket_paths(owned_paths: list[str], parallel_owned_paths: dict[str, str]) -> None:
    """Allow exact shared files while rejecting unsafe path aliases and ancestry."""
    bucket_owned_paths: set[str] = set()
    for path in owned_paths:
        raw_path = path.replace("\\", "/").strip()
        if raw_path.startswith("/") or any(part == ".." for part in raw_path.split("/")):
            raise SystemExit("code-remediate-parallel-owned-path-invalid")
        if any(character in raw_path for character in "*?[]"):
            raise SystemExit("code-remediate-parallel-owned-path-pattern-forbidden")
        normalized_path = PurePosixPath(raw_path).as_posix().removeprefix("./").rstrip("/")
        canonical_path = normalized_path.casefold()
        if not canonical_path or canonical_path == "." or canonical_path in bucket_owned_paths:
            raise SystemExit("code-remediate-parallel-owned-path-invalid")
        bucket_owned_paths.add(canonical_path)
        if any(
            canonical_path.startswith(f"{existing}/") or existing.startswith(f"{canonical_path}/")
            for existing in parallel_owned_paths
        ):
            raise SystemExit("code-remediate-parallel-ownership-overlap")
        if canonical_path in parallel_owned_paths and parallel_owned_paths[canonical_path] != normalized_path:
            raise SystemExit("code-remediate-parallel-ownership-overlap")
        parallel_owned_paths[canonical_path] = normalized_path


def _validate_work_buckets(work_buckets: list[Any], out_dir: Path) -> _WorkBucketObservations:
    """Validate every declared work bucket and accumulate the observed ownership counts."""
    observed_indexes: list[int] = []
    observed_parent_groups = 0
    observed_specialist_groups = 0
    singleton_specialist_groups = 0
    observed_verifier_groups = 0
    parallel_owned_paths: dict[str, str] = {}
    parallel_bucket_count = 0
    bucket_ids: set[str] = set()
    for position, bucket in enumerate(work_buckets):
        bucket_indexes = _validate_work_bucket_fields(bucket, position, bucket_ids)
        observed_indexes.extend(bucket_indexes)

        owned_paths, bucket_mode = _validate_bucket_owned_paths_and_mode(bucket, position)
        if bucket["owner"] == "parent":
            if bucket_mode != "parent":
                raise SystemExit("code-remediate-parent-work-bucket-mode-invalid")
            observed_parent_groups += 1
        else:
            if bucket_mode == "parent":
                raise SystemExit("code-remediate-specialist-work-bucket-mode-invalid")
            observed_specialist_groups += 1
            if len(bucket_indexes) == 1:
                singleton_specialist_groups += 1
                rationale = bucket.get("singleton_rationale")
                if not isinstance(rationale, str) or not rationale.strip():
                    raise SystemExit("code-remediate-singleton-specialist-rationale-missing")
            _validate_specialist_context_pack(bucket, out_dir)
        if bucket["verifier"] != "none":
            observed_verifier_groups += 1
        if bucket_mode == "parallel":
            parallel_bucket_count += 1
            _validate_parallel_bucket_paths(owned_paths, parallel_owned_paths)

    return _WorkBucketObservations(
        indexes=observed_indexes,
        parent_groups=observed_parent_groups,
        specialist_groups=observed_specialist_groups,
        singleton_specialist_groups=singleton_specialist_groups,
        verifier_groups=observed_verifier_groups,
        parallel_bucket_count=parallel_bucket_count,
        bucket_ids=bucket_ids,
    )


def _validate_workplan_coverage_counts(
    workplan: dict[str, Any],
    work_buckets: list[Any],
    selected_indexes: list[int],
    observed: _WorkBucketObservations,
) -> None:
    """Reconcile observed bucket coverage and ownership counts against the declared workplan."""
    if sorted(observed.indexes) != sorted(selected_indexes) or len(observed.indexes) != len(set(observed.indexes)):
        raise SystemExit("code-remediate-work-bucket-coverage-mismatch")
    if len(selected_indexes) <= 5 and len(work_buckets) != 1 and workplan["execution_mode"] != "parallel-specialists":
        raise SystemExit("code-remediate-low-volume-fanout")
    if (
        len(selected_indexes) > 1
        and len(work_buckets) == len(selected_indexes)
        and observed.singleton_specialist_groups == len(work_buckets)
        and len({bucket["owner"] for bucket in work_buckets}) == 1
    ):
        raise SystemExit("code-remediate-one-specialist-per-finding")
    if observed.parent_groups != workplan["parent_owned_groups"]:
        raise SystemExit("code-remediate-workplan-parent-count-mismatch")
    if observed.specialist_groups != workplan["specialist_owned_groups"]:
        raise SystemExit("code-remediate-workplan-specialist-count-mismatch")
    if observed.verifier_groups != workplan["verifier_groups"]:
        raise SystemExit("code-remediate-workplan-verifier-count-mismatch")


def _validate_parallel_specialists_approval(
    workplan: dict[str, Any],
    approval: _WorkplanApproval,
    observed: _WorkBucketObservations,
) -> None:
    """Check approval evidence for a plan that dispatches parallel specialists."""
    if observed.parallel_bucket_count < 2 or not workplan["parallel_eligible"]:
        raise SystemExit("code-remediate-parallel-plan-not-eligible")
    if not workplan["parallel_approval_required"]:
        raise SystemExit("code-remediate-parallel-approval-not-required")
    if approval.status != "approved":
        raise SystemExit("code-remediate-parallel-approval-missing")
    if approval.source not in {"explicit-input", "user-prompt", "workflow-default"}:
        raise SystemExit("code-remediate-parallel-approval-source-missing")
    if approval.source == "user-prompt" and not workplan["parallel_prompt_presented"]:
        raise SystemExit("code-remediate-parallel-prompt-not-presented")
    if approval.source == "workflow-default" and workplan["parallel_prompt_presented"]:
        raise SystemExit("code-remediate-default-parallel-prompt-invalid")
    if approval.response != "approve" or approval.approved_plan_sha256 != approval.bucket_plan_sha256:
        raise SystemExit("code-remediate-parallel-approved-plan-not-bound")


def _validate_sequential_fallback_approval(workplan: dict[str, Any], approval: _WorkplanApproval) -> None:
    """Bind an ineligible parent or sequential route to its dispatch source."""
    if approval.status != "parent-only":
        raise SystemExit("code-remediate-eligible-fanout-approval-not-recorded")
    if approval.source == "not-required":
        raise SystemExit("code-remediate-eligible-fanout-approval-not-recorded")
    if workplan["parallel_eligible"]:
        raise SystemExit("code-remediate-eligible-fanout-fallback-forbidden")
    if approval.source == "workflow-default":
        if workplan["parallel_approval_required"]:
            raise SystemExit("code-remediate-default-fallback-approval-invalid")
        if workplan["parallel_prompt_presented"]:
            raise SystemExit("code-remediate-default-fallback-prompt-invalid")
    elif approval.source in {"explicit-input", "user-prompt"}:
        if not workplan["parallel_approval_required"]:
            raise SystemExit("code-remediate-eligible-fanout-approval-not-recorded")
        if approval.source == "user-prompt" and not workplan["parallel_prompt_presented"]:
            raise SystemExit("code-remediate-eligible-fanout-prompt-not-presented")
    else:
        raise SystemExit("code-remediate-eligible-fanout-approval-source-missing")
    if approval.response != "parent-only" or approval.approved_plan_sha256 is not None:
        raise SystemExit("code-remediate-parent-only-response-invalid")


def _validate_unneeded_parallel_approval(workplan: dict[str, Any], approval: _WorkplanApproval) -> None:
    """Reject approval evidence recorded for a plan that never became fanout-eligible."""
    if workplan["parallel_approval_required"] or approval.status != "not-required":
        raise SystemExit("code-remediate-unneeded-parallel-approval")
    if approval.source != "not-required" or workplan["parallel_prompt_presented"]:
        raise SystemExit("code-remediate-unneeded-parallel-approval-source")
    if approval.response != "not-required" or approval.approved_plan_sha256 is not None:
        raise SystemExit("code-remediate-unneeded-parallel-approval-response")


def _validate_workplan_execution_mode_approval(
    workplan: dict[str, Any],
    approval: _WorkplanApproval,
    observed: _WorkBucketObservations,
) -> None:
    """Route approval validation by execution mode and check mode/ownership consistency."""
    execution_mode = workplan["execution_mode"]
    if execution_mode == "parallel-specialists":
        _validate_parallel_specialists_approval(workplan, approval, observed)
    elif observed.parallel_bucket_count:
        raise SystemExit("code-remediate-parallel-bucket-mode-mismatch")
    elif approval.status == "parent-only":
        _validate_sequential_fallback_approval(workplan, approval)
    elif workplan["parallel_eligible"]:
        raise SystemExit("code-remediate-eligible-fanout-approval-not-recorded")
    else:
        _validate_unneeded_parallel_approval(workplan, approval)

    if execution_mode == "parent-owned" and observed.specialist_groups:
        raise SystemExit("code-remediate-parent-owned-plan-has-specialists")
    if execution_mode == "sequential-specialists" and observed.specialist_groups == 0:
        raise SystemExit("code-remediate-sequential-plan-has-no-specialists")


def _validate_workplan_document(
    out_dir: Path,
    workplan: dict[str, Any],
    approval: _WorkplanApproval,
    observed: _WorkBucketObservations,
    bucket_plan: dict[str, Any],
) -> None:
    """Check the rendered resolution workplan document against the validated plan metadata."""
    workplan_path = out_dir / "resolution-workplan.md"
    declared_path = Path(workplan["workplan_path"])
    if declared_path.name != "resolution-workplan.md":
        raise SystemExit("code-remediate-workplan-path-name-invalid")
    _require_file_sections(
        workplan_path,
        ["Work Bucket Plan", "Parallel Approval", "Execution Order", "Ungrouped Items"],
    )

    workplan_text = workplan_path.read_text(encoding="utf-8").lower()
    for required_text in ("owner", "verifier", "context", "closure", "approval"):
        if required_text not in workplan_text:
            raise SystemExit(f"code-remediate-workplan-missing-{required_text}")
    if approval.bucket_plan_sha256 not in workplan_text or approval.response not in workplan_text:
        raise SystemExit("code-remediate-workplan-approval-binding-missing")
    if approval.source == "workflow-default" and approval.response == "parent-only":
        approval_section = re.search(
            r"(?ms)^## Parallel Approval[ \t]*\n(.*?)(?=^## |\Z)", workplan_text, re.IGNORECASE
        )
        reasons = re.findall(
            r"^Ineligibility reason:[ \t]*([^\n]*)$",
            approval_section.group(1) if approval_section else "",
            re.IGNORECASE | re.MULTILINE,
        )
        reason = reasons[0].strip() if len(reasons) == 1 else ""
        if not reason or reason in {"none", "n/a", "tbd", "todo", "unknown"} or reason.startswith("<"):
            raise SystemExit("code-remediate-ineligibility-reason-missing")
    for bucket_id in observed.bucket_ids:
        if bucket_id.casefold() not in workplan_text:
            raise SystemExit("code-remediate-workplan-bucket-id-missing")
    if workplan["execution_mode"] == "parallel-specialists":
        if bucket_plan.get("schema_version") != 2 or bucket_plan.get("consumer") != "code-remediate":
            raise SystemExit("code-remediate-production-lifecycle-required")
        _validate_code_remediate_production_lifecycle(workplan, bucket_plan, out_dir, approval.bucket_plan_sha256)


def _validate_code_remediate_workplan(
    metadata: dict[str, Any], out_dir: Path, *, current_contract: bool = True
) -> None:
    """Validate bounded work buckets, ownership, and parallel approval metadata."""
    workplan, selected_indexes, approval = _validate_workplan_shape(metadata)
    work_buckets = workplan["work_buckets"]

    if not selected_indexes:
        if work_buckets or workplan["groups_total"] != 0:
            raise SystemExit("code-remediate-empty-selection-has-work-buckets")
        return

    bucket_plan = _validate_workplan_evidence_files(out_dir, workplan, approval)
    _validate_workplan_aggregate_counts(workplan, work_buckets)
    observed = _validate_work_buckets(work_buckets, out_dir)
    _validate_workplan_coverage_counts(workplan, work_buckets, selected_indexes, observed)
    _validate_workplan_execution_mode_approval(workplan, approval, observed)
    _validate_workplan_document(out_dir, workplan, approval, observed, bucket_plan)
    if current_contract and approval.source == "not-required":
        raise SystemExit("code-remediate-current-fallback-dispatch-required")


def _count_out_of_scope_items(action_text: str) -> int:
    """Count concrete out-of-scope rows in a code-remediation action ledger."""
    count = 0
    for line in action_text.lower().splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and "out-of-scope" in stripped and "---" not in stripped:
            count += 1
        elif stripped.startswith("- triage status:") and "out-of-scope" in stripped:
            count += 1
    return count


def _validate_code_remediate_out_of_scope_confirmation(metadata: dict[str, Any], out_dir: Path) -> None:
    """Validate user confirmation metadata for every out-of-scope code-remediation item."""
    confirmation = metadata.get("out_of_scope_confirmation")
    if not isinstance(confirmation, dict):
        raise SystemExit("code-remediate-missing-out-of-scope-confirmation")
    count = confirmation.get("count")
    all_confirmed = confirmation.get("all_confirmed_by_user")
    items = confirmation.get("items")
    if not isinstance(count, int) or count < 0:
        raise SystemExit("code-remediate-invalid-out-of-scope-count")
    if not isinstance(all_confirmed, bool):
        raise SystemExit("code-remediate-invalid-out-of-scope-confirmed")
    if not isinstance(items, list):
        raise SystemExit("code-remediate-invalid-out-of-scope-items")
    if count != len(items):
        raise SystemExit("code-remediate-out-of-scope-count-mismatch")

    action_text = (out_dir / "action-items.md").read_text(encoding="utf-8")
    observed_count = _count_out_of_scope_items(action_text)
    if observed_count > count:
        raise SystemExit("code-remediate-out-of-scope-items-not-recorded")
    if count == 0:
        if not all_confirmed:
            raise SystemExit("code-remediate-zero-out-of-scope-not-confirmed")
        return
    if not all_confirmed:
        raise SystemExit("code-remediate-out-of-scope-not-confirmed")
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise SystemExit(f"code-remediate-out-of-scope-item-not-object:{index}")
        for key in ("item_id", "source", "rationale", "evidence_path"):
            value = item.get(key)
            if not isinstance(value, str) or not value.strip():
                raise SystemExit(f"code-remediate-out-of-scope-item-missing-{key}:{index}")
        if item.get("user_confirmed") is not True:
            raise SystemExit(f"code-remediate-out-of-scope-item-not-confirmed:{index}")


def _validate_code_remediate_pr_relevance(metadata: dict[str, Any], out_dir: Path) -> None:
    """Validate PR relevance triage for report and PR-review items."""
    relevance = metadata.get("pr_relevance")
    if not isinstance(relevance, dict):
        raise SystemExit("code-remediate-missing-pr-relevance")
    evaluated = relevance.get("evaluated")
    if not isinstance(evaluated, bool):
        raise SystemExit("code-remediate-invalid-pr-relevance-evaluated")

    for key in (
        "connected_open_items_total",
        "connected_selectable_items_total",
        "connected_required_followup_total",
        "connected_items_marked_out_of_scope",
    ):
        value = relevance.get(key)
        if not isinstance(value, int) or value < 0:
            raise SystemExit(f"code-remediate-invalid-pr-relevance:{key}")

    is_pr_mode = metadata.get("mode") == "pr" or (out_dir / "pr").exists()
    if is_pr_mode and not evaluated:
        raise SystemExit("code-remediate-pr-relevance-not-evaluated")
    if not evaluated:
        return

    connected_open = relevance["connected_open_items_total"]
    connected_selectable = relevance["connected_selectable_items_total"]
    connected_followup = relevance["connected_required_followup_total"]
    connected_out_of_scope = relevance["connected_items_marked_out_of_scope"]
    if connected_out_of_scope > 0:
        raise SystemExit("code-remediate-connected-item-marked-out-of-scope")
    if connected_open > 0 and connected_selectable + connected_followup < connected_open:
        raise SystemExit("code-remediate-connected-items-not-selectable-or-followup")

    action_text = (out_dir / "action-items.md").read_text(encoding="utf-8").lower()
    scope_text = (out_dir / "resolution-scope.md").read_text(encoding="utf-8").lower()
    if "pr relevance summary" not in action_text or "pr relevance summary" not in scope_text:
        raise SystemExit("code-remediate-pr-relevance-summary-missing")
    if connected_open > 0 and not any(
        relation in action_text for relation in ("direct-diff", "pr-intent", "adjacent", "unknown")
    ):
        raise SystemExit("code-remediate-connected-relation-missing")


def _validate_status_counts(
    counts: Any,
    allowed_statuses: set[str],
    expected_total: int,
    error_prefix: str,
) -> None:
    """Validate table status-count metadata covers every final table row."""
    if not isinstance(counts, dict):
        raise SystemExit(f"{error_prefix}-not-object")
    missing = sorted(allowed_statuses - set(counts))
    if missing:
        raise SystemExit(f"{error_prefix}-missing:" + ",".join(missing))
    unexpected = sorted(set(counts) - allowed_statuses)
    if unexpected:
        raise SystemExit(f"{error_prefix}-unexpected:" + ",".join(unexpected))

    total = 0
    for status, value in counts.items():
        if not isinstance(value, int) or value < 0:
            raise SystemExit(f"{error_prefix}-invalid:{status}")
        total += value
    if total != expected_total:
        raise SystemExit(f"{error_prefix}-total-mismatch")


def _parse_markdown_table(path: Path, heading: str) -> tuple[list[str], list[list[str]]]:
    """Parse the first pipe table under an exact level-two Markdown heading."""
    lines = path.read_text(encoding="utf-8").splitlines()
    expected_heading = f"## {heading}".casefold()
    section_start = next(
        (index + 1 for index, line in enumerate(lines) if line.strip().casefold() == expected_heading),
        None,
    )
    if section_start is None:
        raise SystemExit("code-remediate-final-table-section-missing")

    table_lines: list[str] = []
    for line in lines[section_start:]:
        stripped = line.strip()
        if stripped.startswith("## "):
            break
        if stripped.startswith("|") and stripped.endswith("|"):
            table_lines.append(stripped)
        elif table_lines:
            break
    if len(table_lines) < 2:
        raise SystemExit("code-remediate-final-table-markdown-missing")

    parsed = []
    for line in table_lines:
        parts = re.split(r"(?<!\\)\|", line)
        parsed.append([cell.replace(r"\|", "|").strip() for cell in parts[1:-1]])
    headers = [header.casefold() for header in parsed[0]]
    separator = [cell.replace(" ", "") for cell in parsed[1]]
    if len(headers) != len(separator) or not all(re.fullmatch(r":?-{3,}:?", cell) for cell in separator):
        raise SystemExit("code-remediate-final-table-markdown-separator-invalid")
    rows = parsed[2:]
    if any(len(row) != len(headers) for row in rows):
        raise SystemExit("code-remediate-final-table-markdown-row-width-invalid")
    return headers, rows


def _normalize_rendered_detail(value: str) -> str:
    """Normalize rendered Markdown so it can be compared against a stored ledger value.

    Collapses whitespace, unescapes pipe characters that the table rendering requires, and drops inline code-span
    backticks. The ledger stores plain text, so a detail line that marks up a path or a command as code renders the same
    information the ledger holds; treating that markup as a difference rejects a faithful rendering and leaves a
    complete result unpromotable.
    """
    return re.sub(r"\s+", " ", value.replace(r"\|", "|").replace("`", "")).strip()


def _validate_code_remediate_final_resolution_table(metadata: dict[str, Any], out_dir: Path) -> None:
    """Validate the final code-remediation table covers every ingested entry."""
    table = metadata.get("final_resolution_table")
    if not isinstance(table, dict):
        raise SystemExit("code-remediate-missing-final-resolution-table")

    count_keys = (
        "ingested_entries_total",
        "table_rows_total",
        "omitted_entries_total",
        "selectable_rows_total",
        "nonselectable_rows_total",
    )
    counts = {}
    for key in count_keys:
        value = table.get(key)
        if not isinstance(value, int) or value < 0:
            raise SystemExit(f"code-remediate-invalid-final-resolution-table:{key}")
        counts[key] = value

    if counts["omitted_entries_total"] != 0:
        raise SystemExit("code-remediate-final-table-omitted-entries")
    if counts["ingested_entries_total"] != counts["table_rows_total"]:
        raise SystemExit("code-remediate-final-table-row-count-mismatch")
    if counts["selectable_rows_total"] + counts["nonselectable_rows_total"] != counts["table_rows_total"]:
        raise SystemExit("code-remediate-final-table-selectable-count-mismatch")

    source_count_keys = (
        "source_records_total",
        "represented_source_records_total",
        "omitted_source_records_total",
        "grouped_items_total",
    )
    source_counts = {}
    for key in source_count_keys:
        value = table.get(key)
        if not isinstance(value, int) or value < 0:
            raise SystemExit(f"code-remediate-invalid-final-resolution-table:{key}")
        source_counts[key] = value
    if source_counts["omitted_source_records_total"] != 0:
        raise SystemExit("code-remediate-final-table-omitted-sources")
    if source_counts["source_records_total"] != source_counts["represented_source_records_total"]:
        raise SystemExit("code-remediate-final-table-source-count-mismatch")

    _validate_status_counts(
        table.get("triage_status_counts"),
        CODE_REMEDIATE_TRIAGE_STATUSES,
        counts["table_rows_total"],
        "code-remediate-triage-status-counts",
    )
    _validate_status_counts(
        table.get("resolution_status_counts"),
        CODE_REMEDIATE_RESOLUTION_STATUSES,
        counts["table_rows_total"],
        "code-remediate-resolution-status-counts",
    )

    required_columns = table.get("required_columns")
    if not isinstance(required_columns, list) or not all(isinstance(item, str) for item in required_columns):
        raise SystemExit("code-remediate-final-table-required-columns-invalid")
    normalized_columns = {item.strip().lower() for item in required_columns}
    missing_columns = sorted(CODE_REMEDIATE_FINAL_TABLE_REQUIRED_COLUMNS - normalized_columns)
    if missing_columns:
        raise SystemExit("code-remediate-final-table-required-columns-missing:" + ",".join(missing_columns))

    items = table.get("items")
    if not isinstance(items, list):
        raise SystemExit("code-remediate-final-table-items-not-list")
    if len(items) != counts["table_rows_total"]:
        raise SystemExit("code-remediate-final-table-item-count-mismatch")

    item_ids: set[str] = set()
    observed_triage_counts = {status: 0 for status in CODE_REMEDIATE_TRIAGE_STATUSES}
    observed_resolution_counts = {status: 0 for status in CODE_REMEDIATE_RESOLUTION_STATUSES}
    observed_selectable = 0
    observed_source_keys: set[tuple[str, str]] = set()
    observed_source_records = 0
    observed_grouped_items = 0
    resolution_scope = metadata.get("resolution_scope")
    presentation_version = resolution_scope.get("presentation_version") if isinstance(resolution_scope, dict) else None
    for position, item in enumerate(items):
        if not isinstance(item, dict):
            raise SystemExit(f"code-remediate-final-table-item-not-object:{position}")
        for field in CODE_REMEDIATE_FINAL_ITEM_STRING_FIELDS:
            value = item.get(field)
            if not isinstance(value, str) or not value.strip():
                raise SystemExit(f"code-remediate-final-table-item-invalid-{field}:{position}")
        if not isinstance(item.get("selectable"), bool):
            raise SystemExit(f"code-remediate-final-table-item-invalid-selectable:{position}")
        sources = item.get("sources")
        if not isinstance(sources, list) or not sources:
            raise SystemExit(f"code-remediate-final-table-item-sources-missing:{position}")
        observed_grouped_items += int(len(sources) > 1)
        for source_position, source in enumerate(sources):
            if not isinstance(source, dict):
                raise SystemExit(f"code-remediate-final-table-source-not-object:{position}:{source_position}")
            kind = source.get("kind")
            if not isinstance(kind, str) or kind not in CODE_REMEDIATE_SOURCE_KINDS:
                raise SystemExit(f"code-remediate-final-table-source-kind-invalid:{position}:{source_position}")
            for field in CODE_REMEDIATE_SOURCE_STRING_FIELDS:
                value = source.get(field)
                if not isinstance(value, str) or not value.strip():
                    raise SystemExit(f"code-remediate-final-table-source-{field}-invalid:{position}:{source_position}")
            source_id = source["source_id"].strip()
            if kind == "report" and not CODE_REMEDIATE_REPORT_SOURCE_ID.fullmatch(source_id):
                raise SystemExit("code-remediate-final-table-report-source-id-invalid")
            if kind == "online" and not CODE_REMEDIATE_ONLINE_SOURCE_ID.fullmatch(source_id):
                raise SystemExit("code-remediate-final-table-online-source-id-invalid")
            if kind == "user":
                if presentation_version != 4:
                    raise SystemExit("code-remediate-user-source-requires-v4")
                _validate_code_remediate_user_source(source, out_dir)
            source_key = (kind, source["source_id"].strip())
            if source_key in observed_source_keys:
                raise SystemExit("code-remediate-final-table-source-id-duplicate")
            observed_source_keys.add(source_key)
            observed_source_records += 1
        item_id = item["input_item_id"].strip()
        if item_id in item_ids:
            raise SystemExit("code-remediate-final-table-item-id-duplicate")
        item_ids.add(item_id)
        triage_status = item["triage_status"].strip().casefold()
        resolution_status = item["resolution_status"].strip().casefold()
        if triage_status not in CODE_REMEDIATE_TRIAGE_STATUSES:
            raise SystemExit("code-remediate-final-table-item-triage-status-invalid")
        if resolution_status not in CODE_REMEDIATE_RESOLUTION_STATUSES:
            raise SystemExit("code-remediate-final-table-item-resolution-status-invalid")
        if presentation_version in {3, 4}:
            if "stale" in {triage_status, resolution_status}:
                raise SystemExit("code-remediate-v3-stale-status-forbidden")
            disposition, separator, reason = item["resolved_how"].partition(": ")
            if not separator or not reason.strip() or disposition not in V3_RESOLUTION_DISPOSITIONS[resolution_status]:
                raise SystemExit("code-remediate-v3-resolution-disposition-invalid")
        observed_triage_counts[triage_status] += 1
        observed_resolution_counts[resolution_status] += 1
        observed_selectable += int(item["selectable"])

    if observed_triage_counts != table.get("triage_status_counts"):
        raise SystemExit("code-remediate-final-table-item-triage-counts-mismatch")
    if observed_resolution_counts != table.get("resolution_status_counts"):
        raise SystemExit("code-remediate-final-table-item-resolution-counts-mismatch")
    if observed_selectable != counts["selectable_rows_total"]:
        raise SystemExit("code-remediate-final-table-item-selectable-count-mismatch")
    if observed_source_records != source_counts["represented_source_records_total"]:
        raise SystemExit("code-remediate-final-table-represented-source-count-mismatch")
    if observed_grouped_items != source_counts["grouped_items_total"]:
        raise SystemExit("code-remediate-final-table-grouped-item-count-mismatch")

    if counts["table_rows_total"] == 0:
        return

    action_text = (out_dir / "action-items.md").read_text(encoding="utf-8").lower()
    _require_file_sections(
        out_dir / "action-items.md",
        ["Review Item Resolution Table", "Final Resolution Summary", "Final Resolution Table Completeness"],
    )
    for required_column in sorted(CODE_REMEDIATE_FINAL_TABLE_REQUIRED_COLUMNS):
        if required_column not in action_text:
            raise SystemExit(f"code-remediate-final-table-column-missing:{required_column}")
    for required_text in (
        "ingested entries",
        "table rows",
        "omitted entries",
        "triage status counts",
        "resolution status counts",
    ):
        if required_text not in action_text:
            raise SystemExit(f"code-remediate-final-table-missing-{required_text.replace(' ', '-')}")

    headers, rows = _parse_markdown_table(out_dir / "action-items.md", "Review Item Resolution Table")
    missing_headers = sorted(CODE_REMEDIATE_FINAL_TABLE_REQUIRED_COLUMNS - set(headers))
    if missing_headers:
        raise SystemExit("code-remediate-final-table-markdown-columns-missing:" + ",".join(missing_headers))
    if len(rows) != counts["table_rows_total"]:
        raise SystemExit("code-remediate-final-table-markdown-row-count-mismatch")
    header_indexes = {header: index for index, header in enumerate(headers)}
    rows_by_id: dict[str, list[str]] = {}
    for row in rows:
        row_id = row[header_indexes["input item"]]
        if row_id in rows_by_id:
            raise SystemExit("code-remediate-final-table-markdown-id-duplicate")
        rows_by_id[row_id] = row
    if set(rows_by_id) != item_ids:
        raise SystemExit("code-remediate-final-table-markdown-id-coverage-mismatch")

    field_to_column = {
        "input_item_id": "input item",
        "item_name": "item name",
        "item_type": "item type",
        "triage_status": "triage status",
        "resolution_status": "resolution",
        "owner_status": "owner/status",
    }
    normalized_action_text = _normalize_rendered_detail(action_text)
    normalized_action_lines = {
        _normalize_rendered_detail(line)
        for line in (out_dir / "action-items.md").read_text(encoding="utf-8").splitlines()
    }
    for position, item in enumerate(items, start=1):
        row = rows_by_id[item["input_item_id"].strip()]
        for field, column in field_to_column.items():
            if row[header_indexes[column]] != item[field].strip():
                raise SystemExit(f"code-remediate-final-table-markdown-{field}-mismatch")
        for detail_id, field, column in (
            (f"O{position}", "resolved_how", "resolved how"),
            (f"E{position}", "evidence", "evidence"),
        ):
            if row[header_indexes[column]] != f"[{detail_id}]":
                raise SystemExit(f"code-remediate-final-table-markdown-{field}-mismatch")
            expected_detail = _normalize_rendered_detail(f"[{detail_id}] {item[field]}")
            if expected_detail not in normalized_action_lines:
                raise SystemExit(f"code-remediate-final-table-symbol-detail-missing:{detail_id}")
        expected_source_cell = " ".join(f"{source['kind']} [{source['source_id']}]" for source in item["sources"])
        if row[header_indexes["sources"]] != expected_source_cell:
            raise SystemExit(f"code-remediate-final-table-compact-source-mismatch:{item['input_item_id']}")
        for source in item["sources"]:
            expanded_detail = (
                f"{source['kind']} [{source['source_id']}] @ {source['location']} — "
                f"{source['body']} — {source['evidence']}"
            )
            normalized_detail = _normalize_rendered_detail(expanded_detail).casefold()
            if normalized_detail not in normalized_action_text:
                raise SystemExit(
                    f"code-remediate-final-table-expanded-source-detail-missing:"
                    f"{item['input_item_id']}:{source['source_id']}"
                )


def _validate_code_remediate_unresolved_summary(metadata: dict[str, Any], out_dir: Path) -> None:
    """Validate selected unresolved work is actionable and not overclaimed."""
    summary = metadata.get("unresolved_summary")
    if not isinstance(summary, dict):
        raise SystemExit("code-remediate-missing-unresolved-summary")

    count_keys = (
        "selected_items_total",
        "selected_items_resolved",
        "selected_items_unresolved",
        "local_actionable_items_unresolved",
        "process_gate_items_unresolved",
        "environment_blocked_items",
        "external_owner_items",
        "user_deferred_items",
    )
    counts = {}
    for key in count_keys:
        value = summary.get(key)
        if not isinstance(value, int) or value < 0:
            raise SystemExit(f"code-remediate-invalid-unresolved-summary:{key}")
        counts[key] = value

    if counts["selected_items_resolved"] + counts["selected_items_unresolved"] != counts["selected_items_total"]:
        raise SystemExit("code-remediate-unresolved-summary-total-mismatch")
    scope = metadata.get("resolution_scope", {})
    if scope.get("presentation_version") in {2, 3, 4}:
        selectable = [item for item in metadata["final_resolution_table"]["items"] if item["selectable"]]
        selected = [item for index, item in enumerate(selectable, 1) if index in scope["selected_indexes"]]
        open_count = sum(item["resolution_status"] in {"unresolved", "needs-clarification"} for item in selected)
        if (
            counts["selected_items_total"] != len(selected)
            or counts["selected_items_unresolved"] != open_count
            or counts["selected_items_resolved"] != len(selected) - open_count
        ):
            raise SystemExit("code-remediate-unresolved-selected-count-mismatch")
    if not isinstance(summary.get("all_local_actionable_items_closed"), bool):
        raise SystemExit("code-remediate-invalid-local-actionable-closed")
    if summary["all_local_actionable_items_closed"] and counts["local_actionable_items_unresolved"] > 0:
        raise SystemExit("code-remediate-local-actionable-contradiction")

    reason_groups = summary.get("unresolved_reason_groups")
    if not isinstance(reason_groups, list):
        raise SystemExit("code-remediate-invalid-unresolved-reason-groups")
    if counts["selected_items_unresolved"] > 0 and not reason_groups:
        raise SystemExit("code-remediate-unresolved-reason-groups-missing")

    grouped_count = 0
    for index, group in enumerate(reason_groups):
        if not isinstance(group, dict):
            raise SystemExit(f"code-remediate-unresolved-reason-group-not-object:{index}")
        reason = group.get("reason")
        if reason not in UNRESOLVED_REASON_GROUPS:
            raise SystemExit(f"code-remediate-invalid-unresolved-reason:{index}")
        count = group.get("count")
        if not isinstance(count, int) or count <= 0:
            raise SystemExit(f"code-remediate-invalid-unresolved-reason-count:{index}")
        grouped_count += count
        owner = group.get("owner")
        if owner not in UNRESOLVED_NEXT_OWNERS:
            raise SystemExit(f"code-remediate-invalid-unresolved-owner:{index}")
        for key in ("next_action", "evidence_path"):
            value = group.get(key)
            if not isinstance(value, str) or not value.strip():
                raise SystemExit(f"code-remediate-unresolved-reason-missing-{key}:{index}")

    if grouped_count != counts["selected_items_unresolved"]:
        raise SystemExit("code-remediate-unresolved-reason-count-mismatch")
    if counts["selected_items_unresolved"] == 0:
        return

    _require_file_sections(
        out_dir / "unresolved.txt",
        ["Unresolved Work Summary", "Why Selected Items Remain Unresolved", "Next Action"],
    )
    unresolved_text = (out_dir / "unresolved.txt").read_text(encoding="utf-8").lower()
    for required_text in ("closure class", "next owner", "attempted evidence"):
        if required_text not in unresolved_text:
            raise SystemExit(f"code-remediate-unresolved-summary-missing-{required_text.replace(' ', '-')}")


def _validate_code_remediate_pr_identity(
    routing: dict[str, Any],
    remote_selection: dict[str, Any],
    target_branch: dict[str, Any],
    checkout: dict[str, Any],
) -> None:
    """Reconcile authoritative PR remote, base OID, and checkout head evidence."""
    if routing.get("base_identity_source") != "pr_url":
        raise SystemExit("code-remediate-pr-routing-base-identity-not-authoritative")
    if routing.get("pr_state") != "OPEN":
        raise SystemExit("code-remediate-pr-state-not-open")
    expected_identity = remote_selection.get("expected")
    if not isinstance(expected_identity, dict):
        raise SystemExit("code-remediate-pr-remote-selection-expected-missing")
    if expected_identity.get("host") != routing.get("base_host"):
        raise SystemExit("code-remediate-pr-remote-selection-host-mismatch")
    if expected_identity.get("repository") != routing.get("base_repo"):
        raise SystemExit("code-remediate-pr-remote-selection-repository-mismatch")
    if target_branch.get("remote") != remote_selection.get("remote"):
        raise SystemExit("code-remediate-pr-target-branch-remote-mismatch")
    if target_branch.get("remote_url") != remote_selection.get("remote_url"):
        raise SystemExit("code-remediate-pr-target-branch-remote-url-mismatch")
    expected_base = target_branch.get("expected_base_oid")
    local_base = target_branch.get("local_head")
    if not expected_base or expected_base != routing.get("base_oid"):
        raise SystemExit("code-remediate-pr-target-branch-expected-oid-missing")
    base_matches = local_base == expected_base
    base_is_ancestor = target_branch.get("expected_base_is_ancestor") is True
    expected_relation = "matches-pr-metadata" if base_matches else "advanced" if base_is_ancestor else "diverged"
    if (
        not local_base
        or target_branch.get("base_matches_pr_metadata") is not base_matches
        or target_branch.get("base_relation") != expected_relation
        or not base_is_ancestor
    ):
        raise SystemExit("code-remediate-pr-target-branch-oid-mismatch")
    if checkout.get("pr_url") != routing.get("pr_url"):
        raise SystemExit("code-remediate-pr-local-checkout-url-mismatch")
    if not checkout.get("expected_head") or checkout.get("expected_head") != routing.get("head_oid"):
        raise SystemExit("code-remediate-pr-local-checkout-expected-head-missing")
    if checkout.get("local_head") != checkout.get("expected_head"):
        raise SystemExit("code-remediate-pr-local-checkout-oid-mismatch")
    expected_diff_command = f"git diff --binary {routing.get('base_oid')}...{routing.get('head_oid')} --"
    if (
        checkout.get("diff_source") != "verified-local-checkout"
        or checkout.get("diff_base_oid") != routing.get("base_oid")
        or checkout.get("diff_head_oid") != routing.get("head_oid")
        or checkout.get("diff_command") != expected_diff_command
    ):
        raise SystemExit("code-remediate-pr-local-diff-provenance-invalid")


def _validate_code_remediate_pr_source(
    pr_dir: Path, routing: dict[str, Any], target_branch: dict[str, Any], checkout: dict[str, Any]
) -> None:
    """Validate the checkout receipt and bind remediation source to fetched commit identities."""
    pr_payload = _load_json(pr_dir / "pr.json")
    remote_selection = _load_json(pr_dir / "remote-selection.json")
    # Preserve legacy receipts while validating the collector's current attached-branch routes.
    method = routing.get("checkout_method")
    expected_checkout = None
    if method is None and routing.get("checkout_mode") is None:
        expected_checkout = f"git checkout --detach {routing.get('head_oid')}"
    elif method == "gh-pr-checkout":
        expected_checkout = f"gh pr checkout {routing.get('pr_url')}"
    elif method == "git-original-branch-fallback":
        branch = pr_payload.get("headRefName")
        remote = remote_selection.get("remote")
        url = routing.get("pr_url")
        base_repository = _github_remote_identity(url.rsplit("/pull/", 1)[0]) if isinstance(url, str) else None
        failure = checkout.get("gh_checkout_failure")
        allowed_commands = (
            f"git checkout --no-guess {branch}",
            f"git checkout --track -b {branch} {remote}/{branch}",
        )
        if (
            routing.get("same_repo") is True
            and pr_payload.get("isCrossRepository") is False
            and base_repository is not None
            and "/".join(base_repository).casefold() == _head_repository(pr_payload).casefold()
            and isinstance(remote, str)
            and remote
            and isinstance(failure, dict)
            and isinstance(failure.get("code"), str)
            and failure["code"].strip()
            and failure.get("command") == f"gh pr checkout {url}"
            and isinstance(branch, str)
            and branch
            and not branch.startswith("-")
            and checkout.get("local_branch") == branch
            and routing.get("local_checkout_command") in allowed_commands
        ):
            expected_checkout = routing["local_checkout_command"]
    if (
        expected_checkout is None
        or routing.get("local_checkout_command") != expected_checkout
        or checkout.get("command") != expected_checkout
        or checkout.get("checkout_method") != method
        or checkout.get("checkout_mode") != routing.get("checkout_mode")
        or (method is not None and routing.get("checkout_mode") != "remediate")
        or (method is not None and not checkout.get("local_branch"))
    ):
        raise SystemExit("code-remediate-pr-routing-checkout-command-invalid")
    head_fetch = _load_json(pr_dir / "pr-head-fetch.json")
    preflight = _load_json(pr_dir / "worktree-preflight.json")
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
        raise SystemExit("code-remediate-pr-source-oid-provenance-invalid")
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
        or preflight.get("current_head") != head_oid
        or preflight.get("expected_head") != head_oid
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
    ):
        raise SystemExit("code-remediate-pr-source-worktree-preflight-invalid")


def _validate_code_remediate_merge_resolution(
    metadata: dict[str, Any],
    pr_dir: Path,
    target_branch: dict[str, Any],
    expected_pre_merge_head: str | None = None,
) -> None:
    """Require target-merge completion and optionally bind it to a verified PR head."""
    path = pr_dir / "merge-resolution.json"
    resolution = _load_json(path)
    required = {
        "schema_version",
        "conflicts_detected",
        "status",
        "authorization",
        "base_remote_ref",
        "target_oid",
        "pre_merge_head",
        "post_merge_head",
        "merge_commit",
        "resolved_paths",
        "unmerged_paths",
        "evidence",
    }
    missing = sorted(required - resolution.keys())
    if missing:
        raise SystemExit("code-remediate-merge-resolution-missing:" + ",".join(missing))
    if resolution.get("schema_version") != 1:
        raise SystemExit("code-remediate-merge-resolution-schema-invalid")
    if resolution.get("target_oid") != target_branch.get("local_head"):
        raise SystemExit("code-remediate-merge-resolution-target-oid-mismatch")
    if resolution.get("unmerged_paths") != []:
        raise SystemExit("code-remediate-merge-conflicts-still-unresolved")
    if not isinstance(resolution.get("evidence"), list) or not resolution["evidence"]:
        raise SystemExit("code-remediate-merge-resolution-evidence-missing")

    conflicts = resolution.get("conflicts_detected")
    status = resolution.get("status")
    authorization = resolution.get("authorization")
    pre_head = resolution.get("pre_merge_head")
    post_head = resolution.get("post_merge_head")
    if expected_pre_merge_head is not None:
        if pre_head != expected_pre_merge_head:
            raise SystemExit("code-remediate-merge-resolution-pre-merge-head-mismatch")
        if any(not isinstance(oid, str) or re.fullmatch(r"[0-9a-f]{40}", oid) is None for oid in (pre_head, post_head)):
            raise SystemExit("code-remediate-merge-resolution-execution-head-invalid")
    if conflicts is False:
        if status != "not-needed" or authorization != "not-required":
            raise SystemExit("code-remediate-conflict-free-merge-resolution-invalid")
        if pre_head != post_head or resolution.get("merge_commit") not in (None, ""):
            raise SystemExit("code-remediate-unneeded-target-merge-recorded")
        if resolution.get("resolved_paths") != []:
            raise SystemExit("code-remediate-conflict-free-resolved-paths-invalid")
    elif conflicts is True:
        if status != "completed":
            raise SystemExit("code-remediate-target-merge-not-completed")
        if authorization not in {"explicit-input", "user-confirmed"}:
            raise SystemExit("code-remediate-target-merge-authorization-required")
        if not isinstance(pre_head, str) or not isinstance(post_head, str) or pre_head == post_head:
            raise SystemExit("code-remediate-target-merge-head-evidence-invalid")
        if resolution.get("merge_commit") != post_head:
            raise SystemExit("code-remediate-target-merge-commit-missing")
        if not isinstance(resolution.get("resolved_paths"), list) or not resolution["resolved_paths"]:
            raise SystemExit("code-remediate-target-merge-resolved-paths-missing")
    else:
        raise SystemExit("code-remediate-merge-conflict-decision-invalid")

    summary = metadata.get("merge_resolution")
    if not isinstance(summary, dict):
        raise SystemExit("code-remediate-merge-resolution-metadata-missing")
    summary_path = summary.get("artifact_path")
    if not isinstance(summary_path, str) or Path(summary_path).resolve() != path.resolve():
        raise SystemExit("code-remediate-merge-resolution-path-mismatch")
    expected_summary = {
        "authorization": authorization,
        "conflicts_detected": conflicts,
        "status": status,
    }
    if any(summary.get(key) != value for key, value in expected_summary.items()):
        raise SystemExit("code-remediate-merge-resolution-metadata-mismatch")


def _validate_adversarial_loop(
    result: dict[str, Any], out_dir: Path, gates: dict[str, Any], *, candidate: bool, current_result: bool = False
) -> None:
    """Bind the loop decision to retained snapshots, reports, and visible result rows."""
    if result.get("schema_version") != 2:
        raise SystemExit("adversarial-loop-schema-v2-required")
    table_version = result.get("metadata", {}).get("challenge_table_contract_version")
    if table_version is None:
        raise SystemExit("adversarial-loop-table-contract-required")
    if type(table_version) is not int:
        raise SystemExit("adversarial-loop-table-contract-version-invalid")
    if table_version != 1:
        raise SystemExit("adversarial-loop-table-contract-version-required")
    scope = result["metadata"].get("challenge_scope")
    if "child_runs" in _load_json(out_dir / "final-handoff.json") and not (
        isinstance(scope, dict) and scope.get("mode") == "chunked"
    ):
        raise SystemExit("adversarial-loop-child-scores-scope-invalid")
    if isinstance(scope, dict) and scope.get("mode") == "chunked":
        _validate_chunked_coordinator(result, out_dir, gates, candidate=candidate, current_result=current_result)
        return
    if candidate or "challenge_scope" in result["metadata"]:
        _validate_challenge_scope(result["metadata"], out_dir)
    ledger_path = out_dir / "loop-ledger.json"
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("adversarial_loop.py")), "--ledger", str(ledger_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode:
        raise SystemExit("adversarial-loop-invalid-ledger:" + completed.stderr.strip())
    action_contract = result.get("metadata", {}).get("action_contract_version")
    if type(action_contract) is not int or action_contract != 2:
        raise SystemExit("adversarial-loop-action-contract-required")
    actions_path = out_dir / "loop-actions.json"
    actions = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("adversarial_loop.py")),
            "--ledger",
            str(ledger_path),
            "--actions",
            str(actions_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if actions.returncode:
        raise SystemExit("adversarial-loop-invalid-actions:" + actions.stderr.strip())
    if _load_json(actions_path).get("schema_version") != action_contract:
        raise SystemExit("adversarial-loop-action-contract-required")
    summary = json.loads(completed.stdout)
    if result.get("metadata", {}).get("adversarial_loop") != summary:
        raise SystemExit("adversarial-loop-summary-mismatch")
    if result["status"] == "pass" and summary["reason"] != "clean":
        raise SystemExit("adversarial-loop-incomplete-review")
    review_gate = next(check for check in gates["checks"] if check["id"] == "review")
    if summary["reason"] != "clean" and (review_gate["status"] != "fail" or "review" not in result["checks_failed"]):
        raise SystemExit("adversarial-loop-nonclean-review-gate")
    ledger = _load_json(ledger_path)
    snapshots = [("current.diff", ledger["current_snapshot"])]
    snapshots.extend((f"round-{item['index']}.diff", item["snapshot"]) for item in ledger["rounds"])
    for filename, snapshot in snapshots:
        path = out_dir / filename
        if not path.is_file() or not path.resolve().is_relative_to(out_dir.resolve()):
            raise SystemExit(f"adversarial-loop-snapshot-missing:{filename}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != snapshot["diff_digest"]:
            raise SystemExit(f"adversarial-loop-snapshot-digest-mismatch:{filename}")
    for item in ledger["rounds"]:
        report = out_dir / item["report_path"]
        if not report.resolve().is_relative_to(out_dir.resolve()) or not report.is_file():
            raise SystemExit("adversarial-loop-report-missing-or-outside-run")
        if not report.read_text(encoding="utf-8").strip():
            raise SystemExit("adversarial-loop-report-empty")
    counts = (
        summary["rounds"][-1]["counts"]
        if summary["rounds"]
        else dict.fromkeys(("security", "critical", "high", "medium", "low", "nit"), 0)
    )
    expected_findings = {
        "critical": counts["critical"] + counts["security"],
        "high": counts["high"],
        "medium": counts["medium"],
        "low": counts["low"] + counts["nit"],
    }
    if result["findings"] != expected_findings:
        raise SystemExit("adversarial-loop-finding-count-mismatch")
    _validate_adversarial_loop_rows(out_dir, ledger, summary, table_version=table_version)
    evidence_validator = (
        Path(__file__).resolve().parent.parent / "skills" / "challenge-resolve" / "validate_evidence.py"
    )
    evidence = subprocess.run(
        [sys.executable, str(evidence_validator), "--out", str(out_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    if evidence.returncode:
        raise SystemExit("adversarial-loop-evidence-invalid:" + evidence.stderr.strip())
    loop_evidence = _load_json(out_dir / "loop-evidence.json")
    if (candidate or current_result) and (loop_evidence.get("schema_version") != 2 or "origin" not in loop_evidence):
        raise SystemExit("adversarial-loop-evidence-contract-required")
    if "origin" in loop_evidence and result["metadata"].get("challenge_origin") != loop_evidence["origin"]:
        raise SystemExit("adversarial-loop-origin-mismatch")


def _validate_challenge_scope(
    metadata: dict[str, Any], out_dir: Path, *, summary: bool = False, stopped: bool = False
) -> dict[str, Any] | None:
    """Recheck declared chunk coverage and return a clean or stopped child-backed summary."""
    scope = metadata.get("challenge_scope")
    if not isinstance(scope, dict):
        raise SystemExit("adversarial-loop-challenge-scope-required")
    chunks_dir = out_dir / "chunks"
    if scope == {"mode": "single"}:
        if chunks_dir.exists() or chunks_dir.is_symlink():
            raise SystemExit("adversarial-loop-challenge-scope-mismatch")
        return
    if scope != {
        "mode": "chunked",
        "manifest_path": "chunks/chunks.json",
        "results_path": "chunks/results.json",
    }:
        raise SystemExit("adversarial-loop-challenge-scope-invalid")
    manifest = chunks_dir / "chunks.json"
    results = chunks_dir / "results.json"
    if (
        chunks_dir.is_symlink()
        or manifest.is_symlink()
        or results.is_symlink()
        or not manifest.is_file()
        or not results.is_file()
        or not manifest.resolve().is_relative_to(out_dir.resolve())
        or not results.resolve().is_relative_to(out_dir.resolve())
    ):
        raise SystemExit("adversarial-loop-chunk-coverage-invalid")
    checker = Path(__file__).resolve().parent.parent / "skills" / "challenge-resolve" / "chunk_diff.py"
    checked = subprocess.run(
        [
            sys.executable,
            str(checker),
            "check-stopped" if stopped else "check",
            "--manifest",
            str(manifest),
            "--results",
            str(results),
        ]
        + (["--summary"] if summary else []),
        capture_output=True,
        text=True,
        check=False,
    )
    if checked.returncode:
        raise SystemExit("adversarial-loop-chunk-coverage-invalid:" + checked.stderr.strip())
    if not summary:
        return None
    try:
        return json.loads(checked.stdout)
    except json.JSONDecodeError as error:
        raise SystemExit("adversarial-loop-chunk-summary-invalid") from error


def _validate_chunked_coordinator(
    result: dict[str, Any], out_dir: Path, gates: dict[str, Any], *, candidate: bool, current_result: bool = False
) -> None:
    """Bind a clean or stopped coordinator to checked child evidence without an invented parent score."""
    recorded = result["metadata"].get("chunked_review")
    stopped = result["status"] == "fail" and isinstance(recorded, dict) and recorded.get("status") in {None, "stopped"}
    checked = _validate_challenge_scope(result["metadata"], out_dir, summary=True, stopped=stopped)
    if not isinstance(checked, dict) or not checked.get("runs"):
        raise SystemExit("adversarial-loop-chunk-summary-invalid")
    manifest_path = out_dir / "chunks" / "chunks.json"
    if manifest_path.is_file():
        manifest = _load_json(manifest_path)
        if current_result and manifest.get("schema_version") != 1:
            raise SystemExit("adversarial-loop-chunk-origin-contract-required")
        if manifest.get("schema_version") == 1 and result["metadata"].get("challenge_origin") != manifest.get("origin"):
            raise SystemExit("adversarial-loop-chunk-origin-mismatch")
    metadata = result["metadata"]
    if metadata.get("chunked_review") != checked or "adversarial_loop" in metadata:
        raise SystemExit("adversarial-loop-chunk-summary-mismatch")
    review_gate = next(check for check in gates["checks"] if check["id"] == "review")
    if stopped:
        if (
            result["status"] != "fail"
            or review_gate["status"] != "fail"
            or "review" not in result["checks_failed"]
            or any(result["findings"].values())
        ):
            raise SystemExit("adversarial-loop-chunk-coordinator-must-stop")
    elif (
        result["status"] != "pass"
        or review_gate["status"] != "pass"
        or result["checks_failed"]
        or any(result["findings"].values())
        or any(
            run.get("decision") != "clean" or not run.get("scores") or run["scores"][-1] != 0 for run in checked["runs"]
        )
    ):
        raise SystemExit("adversarial-loop-chunk-coordinator-clean-result-invalid")
    handoff = _load_json(out_dir / "final-handoff.json")
    if candidate and handoff.get("presentation_version") != 3:
        raise SystemExit("adversarial-loop-child-scores-presentation-required")
    if handoff.get("presentation_version") == 3 and handoff.get("child_runs") != checked["runs"]:
        raise SystemExit("adversarial-loop-child-scores-mismatch")
    rows = [row["cells"] for table in handoff["tables"] for row in table["rows"]]
    if rows != [["not-run", *(["N/A"] * 7), "chunk-only", "chunks/results.json"]]:
        raise SystemExit("adversarial-loop-chunk-coordinator-table-mismatch")
    if stopped and (not handoff["remaining"] or not handoff["next_steps"]):
        raise SystemExit("adversarial-loop-recovery-guidance-missing")


def _validate_adversarial_loop_rows(
    out_dir: Path, ledger: dict[str, Any], summary: dict[str, Any], *, table_version: int
) -> None:
    """Keep displayed round scores and decisions equal to their ledger evidence."""
    handoff = _load_json(out_dir / "final-handoff.json")
    rows = [row["cells"] for table in handoff["tables"] for row in table["rows"]]
    expected = []
    seen: set[str] = set()
    weights = {"security": 20, "critical": 10, "high": 6, "medium": 4, "low": 2, "nit": 1}
    for item in summary["rounds"]:
        round_record = ledger["rounds"][item["index"] - 1]
        report = round_record["report_path"]
        counts = item["counts"]
        if table_version == 1:
            new_counts = dict.fromkeys(weights, 0)
            for finding in round_record["findings"]:
                if (
                    finding["disposition"] in {"open", "fixed-pending-verification"}
                    and finding["signature"] not in seen
                ):
                    new_counts[finding["tier"]] += 1
            new_score = sum(new_counts[tier] * weight for tier, weight in weights.items())
            expected.append(
                [
                    str(item["index"]),
                    *(f"{counts[tier] - new_counts[tier]} + {new_counts[tier]}" for tier in weights),
                    f"{item['score'] - new_score} + {new_score}",
                    item["decision"],
                    report,
                ]
            )
        seen.update(finding["signature"] for finding in round_record["findings"])
    if not expected:
        expected = [["not-run", *(["N/A"] * 7), summary["reason"], "loop-report.md"]]
    if rows != expected:
        raise SystemExit("adversarial-loop-table-mismatch")
    if summary["reason"] != "clean" and (not handoff["remaining"] or not handoff["next_steps"]):
        raise SystemExit("adversarial-loop-recovery-guidance-missing")


def _validate_release_communication(result: dict[str, Any], out_dir: Path, gates: dict[str, Any]) -> None:
    """Reject incomplete new release communication while retaining historical report readability.

    The versioned contract checks output selection, evidence presence and draft structure. Semantic release membership,
    contributor identity, historical-byte preservation and executed examples remain the recorded review gate's duties.
    Failed runs retain their requested output list without being forced to manufacture unfinished deliverables.
    """
    metadata = result.get("metadata", {})
    if "release_contract_version" not in metadata:
        return
    version = metadata["release_contract_version"]
    if type(version) is not int or version != 1:
        raise SystemExit("release-contract-version")
    if result.get("schema_version") != RESULT_SCHEMA_VERSION:
        raise SystemExit("release-contract-requires-schema-v2")
    minimum = {
        "notes": {"DRAFT.md"},
        "prepare": {"DRAFT.md", "CHANGELOG.md", "SUMMARY.md", "MIGRATION.md"},
        "audit": set(),
        "demo": {"demo.py"},
    }
    mode = metadata.get("mode")
    if not isinstance(mode, str) or mode not in minimum:
        raise SystemExit("release-invalid-mode")
    requested = metadata.get("requested_artifacts")
    allowed = minimum["prepare"] | minimum["demo"]
    if (
        not isinstance(requested, list)
        or any(not isinstance(name, str) or name not in allowed for name in requested)
        or len(requested) != len(set(requested))
    ):
        raise SystemExit("release-invalid-requested-artifacts")
    if not minimum[mode].issubset(requested):
        raise SystemExit("release-required-deliverables")
    if (mode == "audit" and requested) or (mode == "demo" and set(requested) != {"demo.py"}):
        raise SystemExit("release-mode-artifact-mismatch")
    for name, sections in {
        "release-scope.md": ["Head and baseline", "Released comparison", "Candidate accounting", "Limits"],
        "contributors.md": ["Coverage", "Credits", "Exclusions", "Unresolved"],
        "changelog-audit.md": ["Scope", "Preservation", "Added and flagged", "Limits"],
        "draft-review.md": ["Claims", "Contributors", "Changelog preservation", "Examples", "Deliverables", "Limits"],
    }.items():
        _require_file_sections(out_dir / name, sections)
    _validate_release_readiness(result, out_dir)
    if result["status"] != "pass":
        return
    _validate_release_gate_source(metadata, gates)
    for name in requested:
        path = out_dir / "deliverables" / name
        if path.is_symlink() or not path.resolve().is_relative_to(out_dir.resolve()) or not path.is_file():
            raise SystemExit(f"release-missing-deliverable:{name}")
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            raise SystemExit(f"release-empty-deliverable:{name}")
        if name == "DRAFT.md":
            _validate_release_draft(text)
    validate_release_evidence(metadata, out_dir, set(requested))


def _validate_release_gate_source(metadata: dict[str, Any], gates: dict[str, Any]) -> None:
    """Require passing release checks to observe the pinned clean source before and after execution."""
    head = metadata.get("release_head")
    if not isinstance(head, str) or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head) is None:
        raise SystemExit("release-head-invalid")
    for check in gates["checks"]:
        if check["status"] == "not-applicable":
            continue
        source = check.get("source")
        if not isinstance(source, dict) or source.get("expected_head") != head:
            raise SystemExit(f"release-gate-source:{check['id']}:expected-head")
        for phase in ("before", "after"):
            observation = source.get(phase)
            if (
                not isinstance(observation, dict)
                or observation.get("head") != head
                or observation.get("status") != ""
                or "error" in observation
            ):
                raise SystemExit(f"release-gate-source:{check['id']}:{phase}")


def _validate_release_readiness(result: dict[str, Any], out_dir: Path) -> None:
    """Bind release readiness rows to the report and, when present, the rendered final handoff."""
    try:
        columns, rows = _parse_markdown_table(out_dir / "release-readiness.md", "Checks")
    except SystemExit as error:
        raise SystemExit(f"release-readiness-table-invalid:{error}") from error
    if columns != ["check", "status", "evidence", "blocker / next action"] or not rows:
        raise SystemExit("release-readiness-table-columns")
    required = {
        "semver",
        "migration",
        "release scope",
        "contributors",
        "changelog",
        "documentation",
        "verification",
        "artifacts",
    }
    names = [row[0].casefold() for row in rows]
    if len(names) != len(set(names)) or not required.issubset(names):
        raise SystemExit("release-readiness-checks-missing-or-duplicate")
    for row in rows:
        if any(not cell.strip() for cell in row) or row[1] not in {
            "pass",
            "fail",
            "warning",
            "not-applicable",
            "unavailable",
        }:
            raise SystemExit("release-readiness-row-invalid")
        if result["status"] == "pass" and row[1] in {"fail", "unavailable"}:
            raise SystemExit("release-pass-with-readiness-blocker")
    binding = result["metadata"].get("final_handoff")
    if binding is not None:
        path = _resolve_final_handoff_path(out_dir, binding.get("handoff_path"), "handoff_path")
        handoff = _load_json(path)
        if handoff.get("branch") == "caller-contract":
            return
        tables = handoff.get("tables", [])
        changes = [
            table
            for table in tables
            if table.get("columns") == ["Change", "SemVer impact", "Status / blocker", "Evidence"]
        ]
        if len(tables) != 2 or len(changes) != 1:
            raise SystemExit("release-changes-table-missing")
        readiness = [table for table in handoff.get("tables", []) if table.get("heading") == "Readiness"]
        if len(readiness) != 1 or [row["cells"] for row in readiness[0]["rows"]] != rows:
            raise SystemExit("release-readiness-handoff-mismatch")
        verdict = "release-ready"
        if result["status"] != "pass":
            verdict = "blocked"
        elif result["metadata"]["mode"] in {"notes", "demo"} or any(row[1] == "warning" for row in rows):
            verdict = "warning-only"
        if handoff.get("outcome", {}).get("title") != verdict:
            raise SystemExit("release-readiness-verdict-mismatch")


_RELEASE_DRAFT_TEMPLATE_RESIDUE = (
    "[Release hook]",
    "[User-facing win]",
    "[Upgrade guidance]",
    "[Release highlights]",
    "[Migration guidance]",
    "[Full changelog URL]",
    "> Summary guidance:",
    "> Template guidance:",
)


def _validate_release_draft(text: str) -> None:
    """Require substantive draft section bodies rather than changelog-only output or heading mentions."""
    if re.match(r"\s*#\s+changelog\b", text, re.IGNORECASE):
        raise SystemExit("release-draft-is-changelog")
    # Optional leading emojis preserve project voice without weakening the section-role check.
    for section in ("Summary", "Highlights", "Migration guide", "Notable changes", "Contributors"):
        match = re.search(
            rf"^##[^\w\r\n]*{re.escape(section)}[ \t]*\r?\n(?P<body>.*?)(?=^##[ \t]+|\Z)",
            text,
            re.IGNORECASE | re.MULTILINE | re.DOTALL,
        )
        if match is None or not match.group("body").strip():
            raise SystemExit(f"release-draft-section:{section}")
    if not re.search(r"full changelog", text, re.IGNORECASE):
        raise SystemExit("release-draft-comparison-missing")
    # Template placeholders and writing notes must never survive into a delivered draft.
    for residue in _RELEASE_DRAFT_TEMPLATE_RESIDUE:
        if residue.lower() in text.lower():
            raise SystemExit(f"release-draft-template-residue:{residue}")


def _require_code_remediate_pr_artifacts(pr_dir: Path, result: dict[str, Any]) -> None:
    """Confirm every PR artifact the remediation contract requires is present on disk."""
    required_pr_artifacts = (
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
        "merge-base.txt",
        "merge-tree.txt",
        "merge-resolution.json",
    )
    if result.get("schema_version") in {2, 3}:
        required_pr_artifacts += ("pr-head-fetch.json", "worktree-preflight.json")
    for filename in required_pr_artifacts:
        if not (pr_dir / filename).exists():
            raise SystemExit(f"missing-code-remediate-pr-artifact:{filename}")


def _validate_code_remediate_pr_routing(
    routing: dict[str, Any],
    remote_selection: dict[str, Any],
    result: dict[str, Any],
) -> None:
    """Check routing metadata forbids forced checkout and names the expected checkout command."""
    if routing.get("local_checkout_required") is not True:
        raise SystemExit("code-remediate-pr-routing-local-checkout-not-required")
    if "--force" in str(routing.get("local_checkout_command", "")):
        raise SystemExit("code-remediate-pr-routing-force-checkout-forbidden")
    if "force_policy" not in routing:
        raise SystemExit("code-remediate-pr-routing-force-policy-missing")
    if result.get("schema_version") not in {2, 3}:
        expected_checkout = f"gh pr checkout {routing.get('pr_number')}"
        if routing.get("pr_metadata_transport") == "public-https-fallback":
            expected_checkout = (
                f"git checkout --detach refs/remotes/{remote_selection.get('remote')}/pull/"
                f"{routing.get('pr_number')}/head"
            )
        if routing.get("local_checkout_command") != expected_checkout:
            raise SystemExit("code-remediate-pr-routing-checkout-command-invalid")


def _validate_code_remediate_pr_checkout_state(checkout: dict[str, Any]) -> None:
    """Check the recorded local checkout is unforced and pinned to the PR head."""
    if checkout.get("status") != "checked-out":
        raise SystemExit("code-remediate-pr-local-checkout-not-checked-out")
    if "--force" in str(checkout.get("command", "")):
        raise SystemExit("code-remediate-pr-local-checkout-force-forbidden")
    if "force_policy" not in checkout:
        raise SystemExit("code-remediate-pr-local-checkout-force-policy-missing")
    if checkout.get("head_matches_pr") is not True:
        raise SystemExit("code-remediate-pr-local-checkout-head-mismatch")


def _validate_code_remediate_unavailable_threads(
    pr_dir: Path,
    out_dir: Path,
    thread_error: Any,
    metadata: dict[str, Any],
) -> None:
    """Check the recorded evidence and documented gaps when review threads were unavailable."""
    error_path = pr_dir / "review-threads-error.txt"
    if not isinstance(thread_error, str) or not error_path.is_file():
        raise SystemExit("code-remediate-pr-review-thread-error-missing")
    if error_path.read_text(encoding="utf-8").strip() != thread_error:
        raise SystemExit("code-remediate-pr-review-thread-error-mismatch")
    if _load_json_list(pr_dir / "review-threads.json") or _load_json_list(pr_dir / "unresolved-review-threads.json"):
        raise SystemExit("code-remediate-pr-review-thread-unavailable-must-be-empty")
    confidence_gaps = metadata.get("confidence_gaps")
    if not isinstance(confidence_gaps, list) or PR_THREAD_CONFIDENCE_GAP not in confidence_gaps:
        raise SystemExit("code-remediate-pr-review-thread-confidence-gap-missing")
    action_items = (out_dir / "action-items.md").read_text(encoding="utf-8").casefold()
    if "review-thread" not in action_items or "unavailable" not in action_items:
        raise SystemExit("code-remediate-pr-review-thread-triage-gap-missing")


def _validate_code_remediate_pr_review_threads(
    pr_dir: Path,
    out_dir: Path,
    online_summary: dict[str, Any],
    metadata: dict[str, Any],
) -> None:
    """Reconcile recorded review-thread status against its error evidence and triage notes."""
    thread_status = online_summary.get("review_threads_status")
    thread_error = online_summary.get("review_threads_error")
    if thread_status == "available":
        if thread_error is not None or (pr_dir / "review-threads-error.txt").exists():
            raise SystemExit("code-remediate-pr-review-thread-status-contradiction")
    elif thread_status == "unavailable":
        _validate_code_remediate_unavailable_threads(pr_dir, out_dir, thread_error, metadata)
    else:
        raise SystemExit("code-remediate-pr-review-thread-status-invalid")


def _validate_code_remediate_pr_triage_notes(pr_dir: Path, out_dir: Path) -> None:
    """Check the merge-prestage sections and the triage statuses recorded in action items."""
    if (pr_dir / "head-files").exists():
        raise SystemExit("code-remediate-pr-raw-head-file-snapshots-forbidden")
    _require_file_sections(
        out_dir / "merge-prestage.md",
        [
            "PR And Target Refresh",
            "Clean PR Implementation Context",
            "Target Branch Context",
            "Conflict Risk",
            "Resolution Strategy",
            "Merge Execution",
        ],
    )
    action_text = (out_dir / "action-items.md").read_text(encoding="utf-8").lower()
    required = (
        "valid",
        "resolved",
        "duplicate",
        "stale",
        "out-of-scope",
        "already-fixed",
        "already-applied",
        "needs-clarification",
    )
    if not any(status in action_text for status in required):
        raise SystemExit("code-remediate-pr-triage-status-missing")


def _validate_code_remediate_pr_artifacts(result: dict[str, Any], metadata: dict[str, Any], out_dir: Path) -> None:
    """Validate the PR-mode artifact set ``collect_pr`` produces for a remediation run."""
    pr_dir = out_dir / "pr"
    if metadata.get("mode") != "pr" and not pr_dir.exists():
        return
    _require_code_remediate_pr_artifacts(pr_dir, result)
    routing = _load_json(pr_dir / "pr-routing.json")
    pr_payload = _load_json(pr_dir / "pr.json")
    remote_selection = _load_json(pr_dir / "remote-selection.json")
    target_branch = _load_json(pr_dir / "target-branch.json")
    checkout = _load_json(pr_dir / "local-checkout.json")
    online_summary = _load_json(pr_dir / "online-review-summary.json")
    _validate_pr_fallback_confidence(online_summary, result, metadata)
    if not isinstance(pr_payload.get("body"), str):
        raise SystemExit("code-remediate-pr-description-missing")
    _validate_code_remediate_pr_routing(routing, remote_selection, result)
    if target_branch.get("status") != "fetched":
        raise SystemExit("code-remediate-pr-target-branch-not-fetched")
    _validate_code_remediate_pr_checkout_state(checkout)
    _validate_code_remediate_pr_identity(routing, remote_selection, target_branch, checkout)
    if result.get("schema_version") in {2, 3}:
        _validate_code_remediate_pr_source(pr_dir, routing, target_branch, checkout)
    _validate_code_remediate_pr_review_threads(pr_dir, out_dir, online_summary, metadata)
    _validate_code_remediate_merge_resolution(
        metadata,
        pr_dir,
        target_branch,
        routing.get("head_oid") if result.get("schema_version") in {2, 3} else None,
    )
    _validate_code_remediate_pr_triage_notes(pr_dir, out_dir)


def _is_chunked_coordinator(skill: str, result: dict[str, Any]) -> bool:
    """Report whether this result is a chunked challenge-resolve coordinator run."""
    scope = result["metadata"].get("challenge_scope") if skill == "challenge-resolve" else None
    return (
        skill == "challenge-resolve"
        and result["metadata"].get("challenge_table_contract_version") == 1
        and isinstance(scope, dict)
        and scope.get("mode") == "chunked"
    )


def _validate_required_artifacts(skill: str, out_dir: Path, result: dict[str, Any]) -> None:
    """Check every report section and JSONL artifact the selected skill's contract requires."""
    requirement = SKILL_REQUIREMENTS.get(skill)
    if requirement is None:
        raise SystemExit(f"unsupported-skill:{skill}")
    files = requirement.get("files", {})
    if not isinstance(files, dict):
        raise SystemExit(f"invalid-requirement:{skill}")
    chunked_coordinator = _is_chunked_coordinator(skill, result)
    for filename, sections in files.items():
        if chunked_coordinator and filename == "loop-ledger.json":
            continue
        if not isinstance(sections, list):
            raise SystemExit(f"invalid-sections:{skill}:{filename}")
        report_path = out_dir / str(filename)
        _require_file_sections(report_path, [str(section) for section in sections])
        if skill == "challenge-resolve" and filename == "loop-report.md":
            report_text = report_path.read_text(encoding="utf-8")
            if re.search(r"(?im)^#{1,6}[ \t]+(?:remediation|recovery)[ \t]*#*[ \t]*$", report_text) is None:
                raise SystemExit("missing-artifact-section:loop-report.md:Remediation")
    jsonl_files = requirement.get("jsonl", [])
    if not isinstance(jsonl_files, list):
        raise SystemExit(f"invalid-jsonl-requirement:{skill}")
    for filename in jsonl_files:
        _validate_jsonl(out_dir / str(filename))


def _validate_code_remediate_skill(result: dict[str, Any], out_dir: Path, *, current_contract: bool = True) -> None:
    """Run the code-remediate contract checks over scope, workplan, tables, and PR evidence."""
    metadata = result.get("metadata", {})
    if not isinstance(metadata, dict):
        raise SystemExit("code-remediate-missing-metadata")
    resolution_scope = metadata.get("resolution_scope")
    if not isinstance(resolution_scope, dict):
        raise SystemExit("code-remediate-missing-resolution-scope-metadata")
    _validate_code_remediate_scope_selection(metadata, out_dir)
    _validate_code_remediate_workplan(metadata, out_dir, current_contract=current_contract)
    _validate_code_remediate_out_of_scope_confirmation(metadata, out_dir)
    _validate_code_remediate_report_intake(result, out_dir, current_contract=current_contract)
    _validate_code_remediate_final_resolution_table(metadata, out_dir)
    _validate_code_remediate_pr_relevance(metadata, out_dir)
    _validate_code_remediate_unresolved_summary(metadata, out_dir)
    if result["status"] == "pass":
        summary = metadata["unresolved_summary"]
        selectable = [item for item in metadata["final_resolution_table"]["items"] if item["selectable"]]
        selected = [item for index, item in enumerate(selectable, 1) if index in resolution_scope["selected_indexes"]]
        open_items = [item for item in selected if item["resolution_status"] in {"unresolved", "needs-clarification"}]
        if (
            summary["selected_items_unresolved"] != summary["user_deferred_items"]
            or not summary["all_local_actionable_items_closed"]
            or any(
                summary[key]
                for key in (
                    "local_actionable_items_unresolved",
                    "process_gate_items_unresolved",
                    "environment_blocked_items",
                    "external_owner_items",
                )
            )
            or any(
                item["resolution_status"] != "unresolved" or not item["resolved_how"].startswith("Deferred: ")
                for item in open_items
            )
            or any(
                group["reason"] != "user-deferred" or group["owner"] != "user"
                for group in summary["unresolved_reason_groups"]
            )
        ):
            raise SystemExit("code-remediate-pass-with-required-unresolved-work")
    scope_text = (out_dir / "resolution-scope.md").read_text(encoding="utf-8").lower()
    for required_text in ("selectable", "selected", "deferred"):
        if required_text not in scope_text:
            raise SystemExit(f"code-remediate-scope-missing-{required_text}")
    _validate_code_remediate_pr_artifacts(result, metadata, out_dir)


def _audit_cost_artifact(out_dir: Path, record: object, field: str) -> Any:
    """Read digest-bound audit evidence without accepting paths outside the run."""
    if not isinstance(record, dict) or set(record) != {"path", "sha256"}:
        raise SystemExit(f"audit-cost-invalid-evidence:{field}")
    raw_path = record["path"]
    if not isinstance(raw_path, str) or not raw_path or "\\" in raw_path:
        raise SystemExit(f"audit-cost-invalid-path:{field}")
    declared = PurePosixPath(raw_path)
    if declared.is_absolute() or PureWindowsPath(raw_path).drive or ".." in declared.parts:
        raise SystemExit(f"audit-cost-invalid-path:{field}")
    path = out_dir.resolve() / declared
    components = [out_dir.resolve().joinpath(*declared.parts[:index]) for index in range(1, len(declared.parts) + 1)]
    if any(component.is_symlink() for component in components) or not path.is_file():
        raise SystemExit(f"audit-cost-missing-evidence:{field}")
    if not path.resolve().is_relative_to(out_dir.resolve()):
        raise SystemExit(f"audit-cost-invalid-path:{field}")
    if record["sha256"] != hashlib.sha256(path.read_bytes()).hexdigest():
        raise SystemExit(f"audit-cost-evidence-digest-mismatch:{field}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as error:
        raise SystemExit(f"audit-cost-invalid-json:{field}") from error


def _audit_cost_number(value: object, field: str, *, positive: bool = False) -> float:
    """Reject boolean, nonfinite, or negative cost and failure measurements."""
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise SystemExit(f"audit-cost-invalid-number:{field}")
    if value < 0 or (positive and value == 0):
        raise SystemExit(f"audit-cost-invalid-number:{field}")
    return float(value)


def _validate_audit_evidence(result: dict[str, Any], gates: dict[str, Any], out_dir: Path) -> None:
    """Require assessed audit completion and retained proof for optimization acceptance.

    Validation establishes evidence shape, digest integrity, matching identities, and declared guard outcomes. The
    workflow owner still verifies source completeness, reviewer independence, and provider provenance.
    """
    if result["status"] == "pass" and not any(
        check["id"] == "review" and check["status"] == "pass" for check in gates["checks"]
    ):
        raise SystemExit("audit-review-gate-required")
    value = result["metadata"].get("value_per_token")
    if value is None:
        return
    if not isinstance(value, dict) or value.get("status") not in {
        "not-run",
        "insufficient-evidence",
        "rejected",
        "accepted",
    }:
        raise SystemExit("audit-cost-invalid-status")
    if value["status"] != "accepted":
        return
    if type(value.get("schema_version")) is not int or value["schema_version"] != 2:
        raise SystemExit("audit-cost-acceptance-schema-required")
    for field in ("scope_roots", "conditional_load_trace", "residual_limits"):
        if (
            not isinstance(value.get(field), list)
            or not value[field]
            or not all(isinstance(item, str) and item.strip() for item in value[field])
        ):
            raise SystemExit(f"audit-cost-missing-evidence:{field}")
    for field in ("baseline_sha256", "candidate_sha256"):
        if not isinstance(value.get(field), str) or not re.fullmatch(r"[0-9a-f]{64}", value[field]):
            raise SystemExit(f"audit-cost-invalid-digest:{field}")
    if value["baseline_sha256"] == value["candidate_sha256"]:
        raise SystemExit("audit-cost-unchanged-snapshot")
    if not isinstance(value.get("decision"), str) or not value["decision"].strip():
        raise SystemExit("audit-cost-missing-decision")
    measurements = value.get("static_measurements")
    if not isinstance(measurements, list) or not measurements:
        raise SystemExit("audit-cost-missing-measurements")
    for measurement in measurements:
        if not isinstance(measurement, dict) or measurement.get("source") not in {
            "provider-native",
            "tiktoken:o200k_base",
        }:
            raise SystemExit("audit-cost-token-source-required")
        for field in ("baseline_sha256", "candidate_sha256"):
            if measurement.get(field) != value[field]:
                raise SystemExit("audit-cost-measurement-snapshot-mismatch")
        for field in ("baseline_tokens", "candidate_tokens"):
            _audit_cost_number(measurement.get(field), field, positive=True)
    map_path = value.get("obligation_map_path")
    if not isinstance(map_path, str) or not map_path:
        raise SystemExit("audit-cost-missing-obligation-map")
    obligations = _audit_cost_artifact(
        out_dir, {"path": map_path, "sha256": value.get("obligation_map_sha256")}, "obligation-map"
    )
    if (
        not isinstance(obligations, list)
        or not obligations
        or not all(
            isinstance(item, dict)
            and item.get("preserved") is True
            and all(
                isinstance(item.get(field), str) and item[field].strip()
                for field in ("baseline", "candidate", "evidence")
            )
            for item in obligations
        )
    ):
        raise SystemExit("audit-cost-obligation-map-incomplete")
    static_gates = value.get("static_gates")
    expected = {"package", "tests", "calibration", "contract-markers", "adversarial-review"}
    if not isinstance(static_gates, list) or len(static_gates) != len(expected):
        raise SystemExit("audit-cost-static-guards-required")
    seen: set[str] = set()
    for guard in static_gates:
        if (
            not isinstance(guard, dict)
            or not isinstance(guard.get("id"), str)
            or guard.get("id") not in expected
            or guard["id"] in seen
            or guard.get("status") != "pass"
        ):
            raise SystemExit("audit-cost-static-guard-failed")
        seen.add(guard["id"])
        proof = _audit_cost_artifact(out_dir, guard.get("evidence"), guard["id"])
        if (
            not isinstance(proof, dict)
            or proof.get("status") != "pass"
            or any(proof.get(field) != value[field] for field in ("baseline_sha256", "candidate_sha256"))
        ):
            raise SystemExit("audit-cost-static-guard-failed")
    behavioral = _audit_cost_artifact(out_dir, value.get("behavioral_comparison"), "behavioral")
    live = _audit_cost_artifact(out_dir, value.get("live_comparison"), "live")
    for comparison in (behavioral, live):
        if not isinstance(comparison, dict) or any(
            comparison.get(field) != value[field] for field in ("baseline_sha256", "candidate_sha256")
        ):
            raise SystemExit("audit-cost-comparison-snapshot-mismatch")
    critical = _audit_cost_number(behavioral.get("critical_regressions"), "critical-regressions")
    baseline_failures = _audit_cost_number(behavioral.get("baseline_failures"), "baseline-failures")
    candidate_failures = _audit_cost_number(behavioral.get("candidate_failures"), "candidate-failures")
    if critical != 0 or candidate_failures > baseline_failures:
        raise SystemExit("audit-cost-behavior-regression")
    tasks = behavioral.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise SystemExit("audit-cost-paired-tasks-required")
    seen_tasks: set[str] = set()
    for task in tasks:
        if (
            not isinstance(task, dict)
            or not isinstance(task.get("id"), str)
            or not task["id"].strip()
            or task["id"] in seen_tasks
        ):
            raise SystemExit("audit-cost-invalid-task-identity")
        seen_tasks.add(task["id"])
        # Aggregate successes must not conceal worse completion or more tool/check failures on a paired task.
        assessments = []
        for side in ("baseline", "candidate"):
            assessment = task.get(side)
            if (
                not isinstance(assessment, dict)
                or not isinstance(assessment.get("evidence"), list)
                or not assessment["evidence"]
                or not all(isinstance(item, str) and item.strip() for item in assessment["evidence"])
            ):
                raise SystemExit("audit-cost-task-evidence-required")
            values = {
                field: _audit_cost_number(assessment.get(field), f"task-{side}-{field}")
                for field in ("completion_quality", "tool_failures", "check_failures")
            }
            if values["completion_quality"] > 1:
                raise SystemExit("audit-cost-invalid-completion-quality")
            assessments.append(values)
        baseline, candidate = assessments
        if (
            candidate["completion_quality"] < baseline["completion_quality"]
            or candidate["tool_failures"] > baseline["tool_failures"]
            or candidate["check_failures"] > baseline["check_failures"]
        ):
            raise SystemExit("audit-cost-task-behavior-regression")
    identity = live.get("baseline_identity")
    if (
        not isinstance(identity, dict)
        or set(identity) != {"model", "effort", "task_contract_sha256", "prompt_sha256"}
        or not all(isinstance(item, str) and item.strip() for item in identity.values())
        or live.get("candidate_identity") != identity
    ):
        raise SystemExit("audit-cost-unmatched-live-identity")
    if live.get("source") != "provider-native":
        raise SystemExit("audit-cost-native-live-evidence-required")
    for field in ("task_contract_sha256", "prompt_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", identity[field]):
            raise SystemExit("audit-cost-invalid-live-identity-digest")
    baseline_cost = _audit_cost_number(live.get("baseline_cost"), "baseline-cost", positive=True)
    candidate_cost = _audit_cost_number(live.get("candidate_cost"), "candidate-cost", positive=True)
    minimum = _audit_cost_number(live.get("min_cost_reduction"), "minimum-reduction", positive=True)
    if minimum >= 1 or (baseline_cost - candidate_cost) / baseline_cost + 1e-12 < minimum:
        raise SystemExit("audit-cost-insufficient-reduction")


def validate(skill: str, out_dir: Path, result_path: Path) -> None:
    """Validate shared workflow evidence and the selected skill's completion contract."""
    result = _load_json(result_path)
    _require_result_shape(result)
    _validate_confidence_gaps(result, skill)
    gates = _validate_gates(out_dir)
    _validate_code_review_unavailable_gates(result, gates, skill)
    _reconcile_result_with_gates(result, gates)
    if skill == "audit":
        _validate_audit_evidence(result, gates, out_dir)
    current_contract = (
        result_path.name == "result.candidate.json"
        or skill == "challenge-resolve"
        or (skill == "code-review" and result.get("schema_version") == 3)
        or (
            skill == "code-remediate"
            and result.get("metadata", {}).get("resolution_scope", {}).get("presentation_version") in {3, 4}
        )
    )
    _validate_final_handoff(result, skill, out_dir, gates, candidate=current_contract)

    _validate_required_artifacts(skill, out_dir, result)
    _validate_confidence_recovery(result, skill)
    if skill == "release":
        _validate_release_communication(result, out_dir, gates)
    if skill == "challenge-resolve":
        _validate_adversarial_loop(
            result,
            out_dir,
            gates,
            candidate=current_contract,
            current_result=True,
        )
    if skill == "code-remediate":
        _validate_code_remediate_skill(result, out_dir, current_contract=current_contract)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skill", required=True, choices=sorted(SKILL_REQUIREMENTS), help="Skill contract to validate."
    )
    parser.add_argument("--out", required=True, type=Path, help="Skill artifact directory.")
    parser.add_argument("--result", required=True, type=Path, help="Candidate result JSON to validate.")
    args = parser.parse_args()

    validate(args.skill, args.out, args.result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
