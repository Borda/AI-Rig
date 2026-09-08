"""Regression checks for exact assessed-review finding identity binding."""

from __future__ import annotations

from collections.abc import Callable
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import ModuleType

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
VALIDATOR_PATH = PLUGIN_ROOT / "skills" / "code-review" / "validate_artifacts.py"


def _load_validator() -> ModuleType:
    """Load the standalone review validator without package installation."""
    specification = importlib.util.spec_from_file_location("codex_rig_review_identity_validator", VALIDATOR_PATH)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _result() -> dict[str, object]:
    """Return a schema-v2 result with two explicitly identified findings.

    Example:
        >>> _result()["findings"]["high"]
        1
    """
    return {
        "schema_version": 2,
        "findings": {"critical": 0, "high": 1, "medium": 1, "low": 0},
    }


def _metadata(*, finding_severity: str = "high") -> dict[str, object]:
    """Return the assessed-review metadata for the identity-binding contract.

    Example:
        >>> _metadata()["review_decision"]["recommendation"]
        'needs-more-work'
    """
    return {
        "review_decision": {
            "recommendation": "needs-more-work",
            "summary": "The findings require changes.",
            "rationale": "The action rows map each assessed finding to a required change.",
        },
        "review_findings": [{"id": "R1", "severity": finding_severity}, {"id": "R2", "severity": "medium"}],
        "operational_blockers": [{"id": "G1"}],
    }


def _notes(path: Path, identities: list[str]) -> None:
    """Write one action-table row for each supplied stable identity."""
    rows = "".join(f"| {identity} | Resolve it | evidence | Required |\n" for identity in identities)
    path.write_text(
        "## Review Findings and Merge Blocks\n\n"
        "| Finding / area | Required change | Evidence | Status |\n"
        "| --- | --- | --- | --- |\n" + rows,
        encoding="utf-8",
    )


@pytest.mark.parametrize("recommendation", ["minor-changes", "reject", "not-aligned"])
def test_canonical_local_findings_always_require_action_table(tmp_path: Path, recommendation: str) -> None:
    """New local review findings need the same complete notes as their final handoff."""
    metadata = _metadata(finding_severity="medium")
    metadata["finding_records_version"] = 1
    metadata["review_decision"]["recommendation"] = recommendation
    for record in metadata["review_findings"]:
        record.update(
            title="Resolve finding",
            summary="Observed issue",
            required_change="Resolve it",
            evidence=["evidence"],
            closure_evidence="Regression passes",
        )
    result = {"schema_version": 2, "findings": {"critical": 0, "high": 0, "medium": 2, "low": 0}}
    notes = tmp_path / "review-notes.md"
    notes.write_text("No action table\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="review-missing-findings-action-table"):
        _load_validator()._validate_action_table(notes, result, metadata, "working-tree")
    _notes(notes, ["R1", "R2", "G1"])
    _load_validator()._validate_action_table(notes, result, metadata, "working-tree")


def _run_cli_validation(
    tmp_path: Path, metadata: dict[str, object], identities: list[str]
) -> subprocess.CompletedProcess[str]:
    """Run the shipped validator CLI against one minimally complete assessed candidate."""
    notes = tmp_path / "review-notes.md"
    _notes(notes, identities)
    notes.write_text(
        notes.read_text(encoding="utf-8")
        + "\n".join(
            f"## {section}\n"
            for section in (
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
        ),
        encoding="utf-8",
    )
    metadata.update({"scope": "working-tree", "risk_tier": "TRIVIAL"})
    result_path = tmp_path / "result.json"
    result_path.write_text(
        json.dumps({"schema_version": 2, "status": "fail", "findings": _result()["findings"], "metadata": metadata}),
        encoding="utf-8",
    )
    return subprocess.run(
        [
            sys.executable,
            str(VALIDATOR_PATH),
            "--out",
            str(tmp_path),
            "--result",
            str(result_path),
            "--codex-home",
            str(tmp_path / "codex-home"),
            "--parent-thread-id",
            "identity-test-thread",
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_schema_v2_rejects_finding_severity_totals_that_do_not_match_stable_records() -> None:
    """Prevent a severity total from describing a different finding set than the stable records."""
    with pytest.raises(SystemExit, match="review-findings-severity-count-mismatch:high"):
        _load_validator()._validate_review_decision(_metadata(finding_severity="medium"), _result())


@pytest.mark.parametrize("severity", [pytest.param([], id="list"), pytest.param({}, id="object")])
def test_validator_cli_rejects_structured_finding_severity_without_traceback(tmp_path: Path, severity: object) -> None:
    """Malformed JSON values must preserve the validator's stable error contract."""
    metadata = _metadata()
    metadata["review_findings"][0]["severity"] = severity

    completed = _run_cli_validation(tmp_path, metadata, ["R1", "R2", "G1"])

    assert completed.returncode != 0
    assert "review-finding-severity-invalid:1" in completed.stderr
    assert "Traceback" not in completed.stderr


def test_schema_v2_action_rows_require_exact_finding_and_declared_blocker_identities(tmp_path: Path) -> None:
    """Prevent action rows from inventing or omitting assessed finding identities."""
    validator = _load_validator()
    notes = tmp_path / "review-notes.md"
    result = _result()
    metadata = _metadata()
    _notes(notes, ["R1", "R2", "G1"])

    validator._validate_action_table(notes, result, metadata, "pr")

    _notes(notes, ["R1", "R2", "unbound-area"])
    with pytest.raises(SystemExit, match="review-findings-action-table-identity-unbound:unbound-area"):
        validator._validate_action_table(notes, result, metadata, "pr")

    _notes(notes, ["R1", "G1"])
    with pytest.raises(SystemExit, match="review-findings-action-table-identity-coverage-mismatch:R2"):
        validator._validate_action_table(notes, result, metadata, "pr")


def test_schema_v1_retains_historical_count_only_action_coverage(tmp_path: Path) -> None:
    """Keep readable historical reviews valid without synthesizing stable finding records."""
    notes = tmp_path / "review-notes.md"
    _notes(notes, ["First observed issue", "Second observed issue"])
    result = {"schema_version": 1, "findings": {"critical": 0, "high": 2, "medium": 0, "low": 0}}
    metadata = _metadata()
    metadata.pop("review_findings")
    metadata.pop("operational_blockers")

    _load_validator()._validate_action_table(notes, result, metadata, "pr")


def test_schema_v1_does_not_interpret_opaque_historical_finding_metadata(tmp_path: Path) -> None:
    """Keep count-only historical summaries independent of the new record representation."""
    notes = tmp_path / "review-notes.md"
    _notes(notes, ["First issue", "Second issue"])
    metadata = _metadata()
    metadata["review_findings"] = {"historical_summary": "Two findings"}
    result = {"schema_version": 1, "findings": {"critical": 0, "high": 2, "medium": 0, "low": 0}}

    _load_validator()._validate_action_table(notes, result, metadata, "pr")


@pytest.mark.parametrize(
    ("mutate_metadata", "identities", "error"),
    [
        pytest.param(
            lambda metadata: metadata.update(
                {"review_findings": [{"id": "R1", "severity": "high"}, {"id": "R1", "severity": "medium"}]}
            ),
            ["R1", "G1"],
            "review-finding-id-duplicate:R1",
            id="duplicate-finding-id",
        ),
        pytest.param(
            lambda metadata: metadata.update(
                {"review_findings": [{"id": "R1", "severity": "medium"}, {"id": "R2", "severity": "medium"}]}
            ),
            ["R1", "R2", "G1"],
            "review-findings-severity-count-mismatch:high",
            id="wrong-severity-total",
        ),
        pytest.param(
            lambda _metadata: None,
            ["R1", "R2", "unbound-area"],
            "review-findings-action-table-identity-unbound:unbound-area",
            id="unknown-action",
        ),
        pytest.param(
            lambda _metadata: None,
            ["R1", "R2"],
            "review-findings-action-table-identity-coverage-mismatch:G1",
            id="missing-operational-blocker-action",
        ),
    ],
)
def test_validator_cli_rejects_unbound_schema_v2_finding_actions(
    tmp_path: Path,
    mutate_metadata: Callable[[dict[str, object]], None],
    identities: list[str],
    error: str,
) -> None:
    """Exercise exact finding/action binding through the shipped CLI boundary."""
    metadata = _metadata()
    mutate_metadata(metadata)

    completed = _run_cli_validation(tmp_path, metadata, identities)

    assert completed.returncode != 0
    assert error in completed.stderr
