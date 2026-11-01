"""Final-handoff fixtures shared by the review, loop and optimize suites.

They load the standalone finalizer and artifact validator by file path and build a valid remediation handoff and a
schema-v2 assess result, so every consumer exercises the same documents.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from _modules import load_module

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
FINALIZER = PLUGIN_ROOT / "shared" / "final_handoff.py"
SHARED_VALIDATOR = PLUGIN_ROOT / "shared" / "validate-artifacts.py"
REMEDIATION_COLUMNS = ["Item", "Severity", "Finding", "Sources", "Outcome", "Evidence / next action"]
GATE_IDS = ("lint", "format", "types", "tests", "review")


def load_finalizer() -> Any:
    """Load the standalone finalizer without requiring a package import."""
    return load_module("codex_rig_final_handoff", FINALIZER)


def load_shared_validator() -> Any:
    """Load the shared artifact validator without requiring a package import."""
    return load_module("codex_rig_final_validator", SHARED_VALIDATOR)


def handoff_payload() -> dict[str, object]:
    """Return a valid remediation handoff covering two distinct sources.

    Example:
        >>> handoff_payload()["branch"]
        'standard'
    """
    return {
        "schema_version": 1,
        "skill": "code-remediate",
        "branch": "standard",
        "outcome": {"title": "Remediation Summary", "summary": "One item fixed; one remains external."},
        "tables": [
            {
                "heading": "Final Outcome Table",
                "columns": REMEDIATION_COLUMNS,
                "rows": [
                    {
                        "id": "CR-1",
                        "cells": [
                            "CR-1",
                            "high",
                            "Preserve a | boundary",
                            "report [CR-1]",
                            "Implemented — guarded\ninput",
                            "tests passed",
                        ],
                        "source_ids": ["report:CR-1"],
                    },
                    {
                        "id": "CR-2",
                        "cells": [
                            "CR-2",
                            "medium",
                            "Run external CI",
                            "report [CR-2]",
                            "Unresolved — external runner unavailable",
                            "CI owner must rerun",
                        ],
                        "source_ids": ["report:CR-2"],
                    },
                ],
            }
        ],
        "source_records": [
            {"id": "report:CR-1", "evidence": "action-items.md#CR-1"},
            {"id": "report:CR-2", "evidence": "action-items.md#CR-2"},
        ],
        "source_coverage": {
            "source_records_total": 2,
            "represented_source_records_total": 2,
            "omitted_source_records_total": 0,
        },
        "verification": [
            {"check": "lint", "status": "pass", "evidence": "ruff passed"},
            {"check": "format", "status": "pass", "evidence": "format passed"},
            {"check": "types", "status": "not-applicable", "evidence": "no typed target"},
            {"check": "tests", "status": "pass", "evidence": "2 passed"},
            {"check": "review", "status": "pass", "evidence": "diff check passed"},
        ],
        "remaining": [{"row_id": "CR-2", "item": "External CI", "owner": "ci", "next_action": "Rerun CI."}],
        "next_steps": ["CR-2"],
        "confidence": {
            "score": 0.95,
            "band": "fair",
            "limits": ["External CI was not run."],
            "gaps": [{"gap": "External CI was not run.", "status": "unresolved", "rationale": "CI is external."}],
        },
        "artifacts": [
            {"label": "Result", "path": ".reports/codex/code-remediate/run/result.json"},
            {"label": "Ledger", "path": ".reports/codex/code-remediate/run/action-items.md"},
        ],
        "caller_contract": None,
    }


def write_schema_v2_assess(tmp_path: Path) -> Path:
    """Write one minimal schema-v2 result with a real rendered handoff."""
    checks = []
    verification = []
    for gate_id in GATE_IDS:
        for suffix in ("command.txt", "stdout.txt", "stderr.txt"):
            (tmp_path / f"{gate_id}.{suffix}").write_text("", encoding="utf-8")
        checks.append(
            {
                "id": gate_id,
                "status": "pass",
                "exit_code": 0,
                "duration_seconds": 0.0,
                "command_path": f"{gate_id}.command.txt",
                "stdout": f"{gate_id}.stdout.txt",
                "stderr": f"{gate_id}.stderr.txt",
            }
        )
        verification.append({"check": gate_id, "status": "pass", "evidence": f"{gate_id}.stdout.txt"})
    (tmp_path / "gates.json").write_text(
        json.dumps({"status": "pass", "checks_failed": [], "checks": checks}), encoding="utf-8"
    )
    result_path = tmp_path / "result.candidate.json"
    canonical_result_path = tmp_path / "result.json"
    gap = "External production behavior was not exercised."
    handoff = {
        "schema_version": 1,
        "presentation_version": 8,
        "skill": "assess",
        "branch": "standard",
        "outcome": {"title": "Analysis", "summary": "The requested contract is defined."},
        "tables": [
            {
                "heading": "Decision Table",
                "columns": ["Finding", "Impact", "Decision", "Evidence", "Next action"],
                "rows": [
                    {
                        "id": "CA-1",
                        "cells": [
                            "Final output was prose-only",
                            "Rows could disappear",
                            "Add checkpoint",
                            "analysis.md",
                            "Implement",
                        ],
                        "source_ids": ["analysis:CA-1"],
                    }
                ],
            }
        ],
        "source_records": [{"id": "analysis:CA-1", "evidence": "analysis.md"}],
        "source_coverage": {
            "source_records_total": 1,
            "represented_source_records_total": 1,
            "omitted_source_records_total": 0,
        },
        "verification": verification,
        "remaining": [],
        "next_steps": [],
        "confidence": {
            "score": 0.95,
            "band": "fair",
            "limits": [gap],
            "gaps": [{"gap": gap, "status": "unresolved", "rationale": "No production host transcript exists."}],
        },
        "artifacts": [{"label": "Result", "path": str(canonical_result_path)}],
        "caller_contract": None,
    }
    handoff_path = tmp_path / "final-handoff.json"
    final_path = tmp_path / "final.md"
    validation_path = tmp_path / "final-handoff.validation.json"
    handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
    validation = load_finalizer().render_files(handoff_path, final_path, validation_path)
    result = {
        "schema_version": 2,
        "status": "pass",
        "checks_run": list(GATE_IDS),
        "checks_failed": [],
        "findings": {"critical": 0, "high": 0, "medium": 0, "low": 0},
        "confidence": 0.95,
        "artifact_path": str(canonical_result_path),
        "metadata": {
            "confidence_gaps": [gap],
            "confidence_gap_closures": [
                {"gap": gap, "status": "unresolved", "rationale": "No production host transcript exists."}
            ],
            "confidence_recovery": {
                "initial_confidence": 0.9,
                "final_confidence": 0.95,
                "status": "fair",
                "evidence": ["Renderer and validator passed."],
                "recovery_actions": ["Rendered and digest-bound final output."],
                "remaining_limits": [gap],
            },
            "final_handoff": {
                "schema_version": 1,
                "handoff_path": str(handoff_path),
                "handoff_sha256": validation["handoff_sha256"],
                "rendered_path": str(final_path),
                "rendered_sha256": validation["rendered_sha256"],
                "validation_path": str(validation_path),
                "branch": "standard",
            },
        },
    }
    result_path.write_text(json.dumps(result), encoding="utf-8")
    return result_path
