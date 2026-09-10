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
artifacts required for the selected skill.

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
import json
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any

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
CODE_REMEDIATE_SOURCE_KINDS = {"report", "online"}
CODE_REMEDIATE_SOURCE_STRING_FIELDS = {"source_id", "location", "body", "evidence"}
CODE_REMEDIATE_REPORT_SOURCE_ID = re.compile(r"(?:.+:[1-9]\d*|.+\.json#[^#\r\n]+)", re.IGNORECASE)
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
    "assess": {"files": {}},
    # Historical reports retain their original skill identity and artifact paths.
    "change-analysis": {"files": {}},
    "audit": {
        "files": {
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
    if presentation in {2, 3} and tables[0].get("layout") != {2: "grouped", 3: "concise"}[presentation]:
        raise SystemExit("code-remediate-final-handoff-grouped-layout-required")
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


def _validate_code_review_final_handoff(result: dict[str, Any], handoff: dict[str, Any]) -> None:
    """Require the final review branch and tables to match its terminal or assessed result."""
    if handoff.get("branch") == "caller-contract":
        return
    metadata = result.get("metadata")
    if not isinstance(metadata, dict):
        raise SystemExit("code-review-final-handoff-metadata-invalid")
    review_status = metadata.get("review_status")
    expected_branch = review_status if review_status in {"unavailable", "closed"} else "assessed"
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
    if expected_branch in {"unavailable", "closed"} and tables:
        raise SystemExit("code-review-terminal-final-handoff-table-forbidden")
    if expected_branch == "assessed" and metadata.get("scope") == "pr" and "PR Snapshot" not in headings:
        raise SystemExit("code-review-final-handoff-pr-snapshot-missing")
    if expected_branch == "assessed" and metadata.get("scope") == "pr":
        snapshot = tables_by_heading["PR Snapshot"]
        rows = snapshot.get("rows")
        expected_fields = ("PR", "Author", "CI", "Type", "Suggestion")
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
            raise SystemExit("code-review-final-handoff-pr-snapshot-fields-mismatch")
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
            raise SystemExit("code-review-final-handoff-pr-snapshot-suggestion-mismatch")
    finding_total = sum(result["findings"].values())
    if expected_branch == "assessed" and finding_total and "Review Findings and Merge Blocks" not in headings:
        raise SystemExit("code-review-final-handoff-findings-table-missing")
    if expected_branch == "assessed" and result.get("schema_version") == 2:
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
                if "required_change" in record and row["cells"][1:3] != [
                    record["required_change"],
                    "; ".join(record["evidence"]),
                ]:
                    raise SystemExit("code-review-final-handoff-finding-content-mismatch")


def _validate_final_handoff(result: dict[str, Any], skill: str, out_dir: Path, gates: dict[str, Any]) -> None:
    """Validate schema-v2 final-response artifacts while retaining schema-v1 readability."""
    schema_version = result.get("schema_version", 1)
    if schema_version == 1:
        return
    if schema_version != RESULT_SCHEMA_VERSION:
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
        _validate_code_remediate_final_handoff(result, handoff)
    elif skill == "code-review":
        _validate_code_review_final_handoff(result, handoff)


def _require_file_sections(path: Path, sections: list[str]) -> None:
    if not path.exists():
        raise SystemExit(f"missing-artifact:{path}")
    text = path.read_text(encoding="utf-8")
    for section in sections:
        if section.lower() not in text.lower():
            raise SystemExit(f"missing-artifact-section:{path.name}:{section}")


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


def _validate_code_remediate_report_intake(result: dict[str, Any], out_dir: Path) -> None:
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
    for key in (
        "report_items_total",
        "review_gate_items_total",
        "review_gate_items_selectable",
        "report_items_marked_out_of_scope",
    ):
        value = intake.get(key)
        if not isinstance(value, int) or value < 0:
            raise SystemExit(f"code-remediate-invalid-review-report-intake:{key}")

    grouped_scope = metadata.get("resolution_scope", {}).get("presentation_version") in {2, 3}
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

    if resolution_scope.get("presentation_version") in {2, 3}:
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
        source_refs = re.findall(r"(?:report|online) \[[^\]\r\n]+\]", source_cell)
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


def _validate_code_remediate_workplan(metadata: dict[str, Any], out_dir: Path) -> None:
    """Validate bounded work buckets, ownership, and parallel approval metadata."""
    resolution_scope = metadata.get("resolution_scope")
    if not isinstance(resolution_scope, dict):
        raise SystemExit("code-remediate-missing-resolution-scope-metadata")
    selected_indexes = resolution_scope.get("selected_indexes")
    if not isinstance(selected_indexes, list) or not all(isinstance(item, int) for item in selected_indexes):
        raise SystemExit("code-remediate-invalid-selected-indexes")

    workplan = metadata.get("resolution_workplan")
    if not isinstance(workplan, dict):
        raise SystemExit("code-remediate-missing-resolution-workplan")

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
    execution_mode = workplan.get("execution_mode")
    if execution_mode not in {"parent-owned", "sequential-specialists", "parallel-specialists"}:
        raise SystemExit("code-remediate-invalid-resolution-workplan:execution_mode")
    for key in ("parallel_eligible", "parallel_approval_required", "parallel_prompt_presented"):
        if not isinstance(workplan.get(key), bool):
            raise SystemExit(f"code-remediate-invalid-resolution-workplan:{key}")
    approval_status = workplan.get("parallel_approval_status")
    if approval_status not in {"not-required", "approved", "parent-only"}:
        raise SystemExit("code-remediate-invalid-resolution-workplan:parallel_approval_status")
    approval_source = workplan.get("parallel_approval_source")
    if approval_source not in {"not-required", "explicit-input", "user-prompt"}:
        raise SystemExit("code-remediate-invalid-resolution-workplan:parallel_approval_source")

    work_buckets = workplan.get("work_buckets")
    if not isinstance(work_buckets, list):
        raise SystemExit("code-remediate-invalid-resolution-workplan:work_buckets")

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

    if len(selected_indexes) != len(set(selected_indexes)):
        raise SystemExit("code-remediate-selected-indexes-not-unique")

    if not selected_indexes:
        if work_buckets or workplan["groups_total"] != 0:
            raise SystemExit("code-remediate-empty-selection-has-work-buckets")
        return

    bucket_plan_path = out_dir / "work-bucket-plan.json"
    approval_path = out_dir / "parallel-approval.json"
    bucket_plan = _load_json(bucket_plan_path)
    approval = _load_json(approval_path)
    if bucket_plan.get("work_buckets") != work_buckets:
        raise SystemExit("code-remediate-work-bucket-plan-content-mismatch")
    if bucket_plan.get("schema_version") not in {1, 2}:
        raise SystemExit("code-remediate-work-bucket-plan-content-mismatch")
    observed_plan_sha256 = hashlib.sha256(bucket_plan_path.read_bytes()).hexdigest()
    if bucket_plan_sha256 != observed_plan_sha256:
        raise SystemExit("code-remediate-work-bucket-plan-digest-mismatch")
    expected_approval = {
        "plan_sha256": bucket_plan_sha256,
        "prompt_presented": workplan["parallel_prompt_presented"],
        "response": approval_response,
        "source": approval_source,
    }
    if approval != expected_approval:
        raise SystemExit("code-remediate-parallel-approval-evidence-mismatch")

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

    observed_indexes: list[int] = []
    observed_parent_groups = 0
    observed_specialist_groups = 0
    singleton_specialist_groups = 0
    observed_verifier_groups = 0
    parallel_owned_paths: set[str] = set()
    parallel_bucket_count = 0
    bucket_ids: set[str] = set()
    for position, bucket in enumerate(work_buckets):
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
        observed_indexes.extend(bucket_indexes)

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
            context_path = Path(bucket["context_pack_path"])
            resolved_context_path = context_path if context_path.is_absolute() else out_dir / context_path
            try:
                resolved_context_path.resolve().relative_to(out_dir.resolve())
            except ValueError as error:
                raise SystemExit("code-remediate-specialist-context-outside-run-directory") from error
            if not resolved_context_path.is_file():
                raise SystemExit("code-remediate-specialist-context-pack-missing")
        if bucket["verifier"] != "none":
            observed_verifier_groups += 1
        if bucket_mode == "parallel":
            parallel_bucket_count += 1
            bucket_owned_paths: set[str] = set()
            for path in owned_paths:
                raw_path = path.replace("\\", "/").strip()
                if raw_path.startswith("/") or any(part == ".." for part in raw_path.split("/")):
                    raise SystemExit("code-remediate-parallel-owned-path-invalid")
                if any(character in raw_path for character in "*?[]"):
                    raise SystemExit("code-remediate-parallel-owned-path-pattern-forbidden")
                normalized_path = PurePosixPath(raw_path).as_posix().removeprefix("./").rstrip("/").casefold()
                if not normalized_path or normalized_path == "." or normalized_path in bucket_owned_paths:
                    raise SystemExit("code-remediate-parallel-owned-path-invalid")
                bucket_owned_paths.add(normalized_path)
                if any(
                    normalized_path == existing
                    or normalized_path.startswith(f"{existing}/")
                    or existing.startswith(f"{normalized_path}/")
                    for existing in parallel_owned_paths
                ):
                    raise SystemExit("code-remediate-parallel-ownership-overlap")
                parallel_owned_paths.add(normalized_path)

    if sorted(observed_indexes) != sorted(selected_indexes) or len(observed_indexes) != len(set(observed_indexes)):
        raise SystemExit("code-remediate-work-bucket-coverage-mismatch")
    if len(selected_indexes) <= 5 and len(work_buckets) != 1:
        raise SystemExit("code-remediate-low-volume-fanout")
    if (
        len(selected_indexes) > 1
        and len(work_buckets) == len(selected_indexes)
        and singleton_specialist_groups == len(work_buckets)
    ):
        raise SystemExit("code-remediate-one-specialist-per-finding")
    if observed_parent_groups != workplan["parent_owned_groups"]:
        raise SystemExit("code-remediate-workplan-parent-count-mismatch")
    if observed_specialist_groups != workplan["specialist_owned_groups"]:
        raise SystemExit("code-remediate-workplan-specialist-count-mismatch")
    if observed_verifier_groups != workplan["verifier_groups"]:
        raise SystemExit("code-remediate-workplan-verifier-count-mismatch")

    if execution_mode == "parallel-specialists":
        if parallel_bucket_count < 2 or not workplan["parallel_eligible"]:
            raise SystemExit("code-remediate-parallel-plan-not-eligible")
        if not workplan["parallel_approval_required"]:
            raise SystemExit("code-remediate-parallel-approval-not-required")
        if approval_status != "approved":
            raise SystemExit("code-remediate-parallel-approval-missing")
        if approval_source not in {"explicit-input", "user-prompt"}:
            raise SystemExit("code-remediate-parallel-approval-source-missing")
        if approval_source == "user-prompt" and not workplan["parallel_prompt_presented"]:
            raise SystemExit("code-remediate-parallel-prompt-not-presented")
        if approval_response != "approve" or approved_plan_sha256 != bucket_plan_sha256:
            raise SystemExit("code-remediate-parallel-approved-plan-not-bound")
    elif parallel_bucket_count:
        raise SystemExit("code-remediate-parallel-bucket-mode-mismatch")
    elif workplan["parallel_eligible"]:
        if not workplan["parallel_approval_required"] or approval_status != "parent-only":
            raise SystemExit("code-remediate-eligible-fanout-approval-not-recorded")
        if approval_source not in {"explicit-input", "user-prompt"}:
            raise SystemExit("code-remediate-eligible-fanout-approval-source-missing")
        if approval_source == "user-prompt" and not workplan["parallel_prompt_presented"]:
            raise SystemExit("code-remediate-eligible-fanout-prompt-not-presented")
        if approval_response != "parent-only" or approved_plan_sha256 is not None:
            raise SystemExit("code-remediate-parent-only-response-invalid")
    else:
        if workplan["parallel_approval_required"] or approval_status != "not-required":
            raise SystemExit("code-remediate-unneeded-parallel-approval")
        if approval_source != "not-required" or workplan["parallel_prompt_presented"]:
            raise SystemExit("code-remediate-unneeded-parallel-approval-source")
        if approval_response != "not-required" or approved_plan_sha256 is not None:
            raise SystemExit("code-remediate-unneeded-parallel-approval-response")

    if execution_mode == "parent-owned" and observed_specialist_groups:
        raise SystemExit("code-remediate-parent-owned-plan-has-specialists")
    if execution_mode == "sequential-specialists" and observed_specialist_groups == 0:
        raise SystemExit("code-remediate-sequential-plan-has-no-specialists")

    workplan_path = out_dir / "resolution-workplan.md"
    declared_path = Path(workplan_path_value)
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
    if bucket_plan_sha256 not in workplan_text or approval_response not in workplan_text:
        raise SystemExit("code-remediate-workplan-approval-binding-missing")
    for bucket_id in bucket_ids:
        if bucket_id.casefold() not in workplan_text:
            raise SystemExit("code-remediate-workplan-bucket-id-missing")
    if execution_mode == "parallel-specialists":
        if bucket_plan.get("schema_version") != 2 or bucket_plan.get("consumer") != "code-remediate":
            raise SystemExit("code-remediate-production-lifecycle-required")
        _validate_code_remediate_production_lifecycle(workplan, bucket_plan, out_dir, bucket_plan_sha256)


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


def _validate_code_remediate_merge_resolution(
    metadata: dict[str, Any], pr_dir: Path, target_branch: dict[str, Any]
) -> None:
    """Require intent-first target-merge completion before PR finding remediation."""
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


def validate(skill: str, out_dir: Path, result_path: Path) -> None:
    result = _load_json(result_path)
    _require_result_shape(result)
    _validate_confidence_gaps(result, skill)
    gates = _validate_gates(out_dir)
    _reconcile_result_with_gates(result, gates)
    _validate_final_handoff(result, skill, out_dir, gates)

    requirement = SKILL_REQUIREMENTS.get(skill)
    if requirement is None:
        raise SystemExit(f"unsupported-skill:{skill}")
    files = requirement.get("files", {})
    if not isinstance(files, dict):
        raise SystemExit(f"invalid-requirement:{skill}")
    for filename, sections in files.items():
        if not isinstance(sections, list):
            raise SystemExit(f"invalid-sections:{skill}:{filename}")
        _require_file_sections(out_dir / str(filename), [str(section) for section in sections])
    jsonl_files = requirement.get("jsonl", [])
    if not isinstance(jsonl_files, list):
        raise SystemExit(f"invalid-jsonl-requirement:{skill}")
    for filename in jsonl_files:
        _validate_jsonl(out_dir / str(filename))
    _validate_confidence_recovery(result, skill)
    if skill == "code-remediate":
        metadata = result.get("metadata", {})
        if not isinstance(metadata, dict):
            raise SystemExit("code-remediate-missing-metadata")
        resolution_scope = metadata.get("resolution_scope")
        if not isinstance(resolution_scope, dict):
            raise SystemExit("code-remediate-missing-resolution-scope-metadata")
        _validate_code_remediate_scope_selection(metadata, out_dir)
        _validate_code_remediate_workplan(metadata, out_dir)
        _validate_code_remediate_out_of_scope_confirmation(metadata, out_dir)
        _validate_code_remediate_report_intake(result, out_dir)
        _validate_code_remediate_final_resolution_table(metadata, out_dir)
        _validate_code_remediate_pr_relevance(metadata, out_dir)
        _validate_code_remediate_unresolved_summary(metadata, out_dir)
        scope_text = (out_dir / "resolution-scope.md").read_text(encoding="utf-8").lower()
        for required_text in ("selectable", "selected", "deferred"):
            if required_text not in scope_text:
                raise SystemExit(f"code-remediate-scope-missing-{required_text}")
        pr_dir = out_dir / "pr"
        if metadata.get("mode") == "pr" or pr_dir.exists():
            for filename in (
                "pr.json",
                "pr-routing.json",
                "remote-selection.json",
                "target-branch.json",
                "pr-head-fetch.json",
                "local-checkout.json",
                "comments.json",
                "reviews.json",
                "review-threads.json",
                "unresolved-review-threads.json",
                "online-review-summary.json",
                "merge-base.txt",
                "merge-tree.txt",
                "merge-resolution.json",
            ):
                if not (pr_dir / filename).exists():
                    raise SystemExit(f"missing-code-remediate-pr-artifact:{filename}")
            routing = _load_json(pr_dir / "pr-routing.json")
            pr_payload = _load_json(pr_dir / "pr.json")
            remote_selection = _load_json(pr_dir / "remote-selection.json")
            target_branch = _load_json(pr_dir / "target-branch.json")
            checkout = _load_json(pr_dir / "local-checkout.json")
            online_summary = _load_json(pr_dir / "online-review-summary.json")
            _validate_pr_fallback_confidence(online_summary, result, metadata)
            if not isinstance(pr_payload.get("body"), str):
                raise SystemExit("code-remediate-pr-description-missing")
            if routing.get("local_checkout_required") is not True:
                raise SystemExit("code-remediate-pr-routing-local-checkout-not-required")
            if "--force" in str(routing.get("local_checkout_command", "")):
                raise SystemExit("code-remediate-pr-routing-force-checkout-forbidden")
            if "force_policy" not in routing:
                raise SystemExit("code-remediate-pr-routing-force-policy-missing")
            expected_checkout = f"gh pr checkout {routing.get('pr_number')}"
            if routing.get("pr_metadata_transport") == "public-https-fallback":
                expected_checkout = (
                    f"git checkout --detach refs/remotes/{remote_selection.get('remote')}/pull/"
                    f"{routing.get('pr_number')}/head"
                )
            if routing.get("local_checkout_command") != expected_checkout:
                raise SystemExit("code-remediate-pr-routing-checkout-command-invalid")
            if target_branch.get("status") != "fetched":
                raise SystemExit("code-remediate-pr-target-branch-not-fetched")
            if checkout.get("status") != "checked-out":
                raise SystemExit("code-remediate-pr-local-checkout-not-checked-out")
            if "--force" in str(checkout.get("command", "")):
                raise SystemExit("code-remediate-pr-local-checkout-force-forbidden")
            if "force_policy" not in checkout:
                raise SystemExit("code-remediate-pr-local-checkout-force-policy-missing")
            if checkout.get("head_matches_pr") is not True:
                raise SystemExit("code-remediate-pr-local-checkout-head-mismatch")
            _validate_code_remediate_pr_identity(routing, remote_selection, target_branch, checkout)
            thread_status = online_summary.get("review_threads_status")
            thread_error = online_summary.get("review_threads_error")
            if thread_status == "available":
                if thread_error is not None or (pr_dir / "review-threads-error.txt").exists():
                    raise SystemExit("code-remediate-pr-review-thread-status-contradiction")
            elif thread_status == "unavailable":
                error_path = pr_dir / "review-threads-error.txt"
                if not isinstance(thread_error, str) or not error_path.is_file():
                    raise SystemExit("code-remediate-pr-review-thread-error-missing")
                if error_path.read_text(encoding="utf-8").strip() != thread_error:
                    raise SystemExit("code-remediate-pr-review-thread-error-mismatch")
                if _load_json_list(pr_dir / "review-threads.json") or _load_json_list(
                    pr_dir / "unresolved-review-threads.json"
                ):
                    raise SystemExit("code-remediate-pr-review-thread-unavailable-must-be-empty")
                confidence_gaps = metadata.get("confidence_gaps")
                if not isinstance(confidence_gaps, list) or PR_THREAD_CONFIDENCE_GAP not in confidence_gaps:
                    raise SystemExit("code-remediate-pr-review-thread-confidence-gap-missing")
                action_items = (out_dir / "action-items.md").read_text(encoding="utf-8").casefold()
                if "review-thread" not in action_items or "unavailable" not in action_items:
                    raise SystemExit("code-remediate-pr-review-thread-triage-gap-missing")
            else:
                raise SystemExit("code-remediate-pr-review-thread-status-invalid")
            _validate_code_remediate_merge_resolution(metadata, pr_dir, target_branch)
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
