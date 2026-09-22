"""Regression checks for review recommendation and action-table integrity."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
VALIDATOR_PATH = PLUGIN_ROOT / "skills" / "code-review" / "validate_artifacts.py"


def _load_validator() -> ModuleType:
    """Load the standalone review validator without package installation."""
    specification = importlib.util.spec_from_file_location("codex_rig_review_validator", VALIDATOR_PATH)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _result(*, critical: int = 0, high: int = 0, medium: int = 0, low: int = 0) -> dict[str, object]:
    """Return a review result with one explicit severity-count vector.

    Example:
        >>> _result(high=1)["findings"]["high"]
        1
    """
    return {
        "status": "pass",
        "checks_failed": [],
        "findings": {"critical": critical, "high": high, "medium": medium, "low": low},
    }


def _metadata(recommendation: str) -> dict[str, object]:
    """Return a complete structured review decision for one recommendation.

    Example:
        >>> _metadata("accept-as-is")["review_decision"]["recommendation"]
        'accept-as-is'
    """
    return {
        "review_decision": {
            "recommendation": recommendation,
            "summary": "The evidence supports this decision.",
            "rationale": "Finding severities determine the merge recommendation.",
        }
    }


@pytest.mark.parametrize(
    ("recommendation", "result", "error"),
    [
        pytest.param("accept-as-is", _result(high=1), "review-accept-with-findings", id="accept-as-is"),
        pytest.param(
            "minor-changes", _result(high=1), "review-minor-with-blocking-findings", id="minor-changes-_result-high-1"
        ),
        pytest.param(
            "minor-changes",
            _result(critical=1),
            "review-minor-with-blocking-findings",
            id="minor-changes-_result-critical-1",
        ),
    ],
)
def test_review_recommendation_is_bound_to_finding_severity(
    recommendation: str, result: dict[str, object], error: str
) -> None:
    """Reject approval or minor recommendations that contradict blocking findings."""
    with pytest.raises(SystemExit, match=error):
        _load_validator()._validate_review_decision(_metadata(recommendation), result)


def test_review_action_table_rejects_duplicate_finding_identity(tmp_path: Path) -> None:
    """Prevent two actions from silently claiming the same finding or operational area."""
    notes = tmp_path / "review-notes.md"
    notes.write_text(
        "## Review Findings and Merge Blocks\n\n"
        "| Finding / area | Required change | Evidence | Status |\n"
        "| --- | --- | --- | --- |\n"
        "| R1 | Add a guard | tests | Required |\n"
        "| R1 | Add tests | tests | Required verification |\n",
        encoding="utf-8",
    )
    metadata = _metadata("needs-more-work")

    with pytest.raises(SystemExit, match="review-findings-action-table-identity-duplicate:R1"):
        _load_validator()._validate_action_table(notes, _result(high=2), metadata, "pr")


def test_attributed_review_notes_bind_authors_to_canonical_findings(tmp_path: Path) -> None:
    """Preserve all contributors and reject a changed author in saved review notes."""
    validator = _load_validator()
    result = {**_result(high=1), "schema_version": 2}
    metadata = _metadata("needs-more-work")
    metadata.update(
        finding_records_version=1,
        reviewer_assessments=[
            {"role": "Software engineer", "rating": 3, "evidence": "sw.md"},
            {"role": "QA specialist", "rating": 2, "evidence": "qa.md"},
        ],
        review_findings=[
            {
                "id": "R1",
                "severity": "high",
                "title": "Empty input fails",
                "summary": "Input is unguarded.",
                "required_change": "Add a guard",
                "evidence": ["config.py:12"],
                "closure_evidence": "Empty input passes.",
                "authors": ["Software engineer", "QA specialist"],
            }
        ],
    )
    body = (
        "## Review Findings and Merge Blocks\n\n"
        "| Finding / area | Author | Required change | Evidence | Status |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| R1 | Software engineer, QA specialist | Add a guard | config.py:12 | Required |\n"
    )
    notes = tmp_path / "review-notes.md"
    notes.write_text(body, encoding="utf-8")
    validator._validate_review_decision(metadata, result)
    validator._validate_action_table(notes, result, metadata, "pr")
    notes.write_text(body.replace("Software engineer, QA specialist", "QA specialist"), encoding="utf-8")
    with pytest.raises(SystemExit, match="review-findings-action-table-authors-mismatch"):
        validator._validate_action_table(notes, result, metadata, "pr")
    metadata["review_findings"][0]["authors"] = ["Skipped reviewer"]
    with pytest.raises(SystemExit, match="review-finding-authors-invalid"):
        validator._validate_review_decision(metadata, result)


def test_attributed_review_accepts_an_identifier_only_operational_blocker(tmp_path: Path) -> None:
    """Keep an ID-only blocker representable once reviewer attribution is required."""
    validator = _load_validator()
    result = {**_result(), "schema_version": 2}
    metadata = _metadata("needs-more-work")
    metadata.update(
        finding_records_version=1,
        review_findings=[],
        reviewer_assessments=[{"role": "QA specialist", "rating": 3, "evidence": "qa.md"}],
        operational_blockers=[{"id": "OB1", "authors": ["QA specialist"]}],
    )
    body = (
        "## Review Findings and Merge Blocks\n\n"
        "| Finding / area | Author | Required change | Evidence | Status |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| OB1 | QA specialist | Restore the runner | ci.log:4 | Required |\n"
    )
    notes = tmp_path / "review-notes.md"
    notes.write_text(body, encoding="utf-8")
    validator._validate_review_decision(metadata, result)
    validator._validate_action_table(notes, result, metadata, "pr")
    del metadata["operational_blockers"][0]["authors"]
    with pytest.raises(SystemExit, match="review-finding-authors-invalid"):
        validator._validate_review_decision(metadata, result)


def test_unattributed_review_rejects_an_author_column(tmp_path: Path) -> None:
    """Refuse a rendered Author column that no retained assessment can bind."""
    validator = _load_validator()
    result = {**_result(high=1), "schema_version": 2}
    metadata = _metadata("needs-more-work")
    metadata.update(
        finding_records_version=1,
        review_findings=[
            {
                "id": "R1",
                "severity": "high",
                "title": "Empty input fails",
                "summary": "Input is unguarded.",
                "required_change": "Add a guard",
                "evidence": ["config.py:12"],
                "closure_evidence": "Empty input passes.",
            }
        ],
    )
    notes = tmp_path / "review-notes.md"
    notes.write_text(
        "## Review Findings and Merge Blocks\n\n"
        "| Finding / area | Author | Required change | Evidence | Status |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| R1 |  | Add a guard | config.py:12 | Required |\n",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="review-findings-action-table-authors-unbound"):
        validator._validate_action_table(notes, result, metadata, "pr")


@pytest.mark.parametrize("recommendation", ["accept-as-is", "minor-changes"])
@pytest.mark.parametrize(
    "status,checks_failed",
    [
        pytest.param("fail", ["tests"], id="failed-check"),
        pytest.param("timeout", ["tests"], id="timeout"),
        pytest.param("fail", [], id="failed-process"),
    ],
)
def test_approving_recommendation_requires_passing_quality_gates(
    recommendation: str, status: str, checks_failed: list[str]
) -> None:
    """Failed or incomplete checks require a non-approval decision even with zero findings."""
    result = _result()
    result.update(status=status, checks_failed=checks_failed)

    with pytest.raises(SystemExit, match="review-approval-with-failed-gates"):
        _load_validator()._validate_review_decision(_metadata(recommendation), result)


@pytest.mark.parametrize("recommendation", ["accept-as-is", "minor-changes", "needs-more-work"])
def test_gate_binding_preserves_passing_and_non_approval_decisions(recommendation: str) -> None:
    """Reject contradictory approvals without making honest failed reviews unreportable."""
    result = _result()
    if recommendation == "needs-more-work":
        result.update(status="fail", checks_failed=["tests"])

    _load_validator()._validate_review_decision(_metadata(recommendation), result)
