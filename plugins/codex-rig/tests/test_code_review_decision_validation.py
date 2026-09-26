"""Regression checks for review recommendation and action-table integrity."""

from __future__ import annotations

import importlib.util
import json
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


def test_parent_substitute_assessment_must_disclose_parent_origin() -> None:
    """A parent judgment cannot appear as an independent specialist rating."""
    validator = _load_validator()
    metadata = _metadata("accept-as-is")
    metadata["specialist_passes"] = [{"role": "sw-engineer", "mode": "substituted"}]
    metadata["reviewer_assessments"] = [{"role": "Software engineer", "rating": 3, "evidence": "parent.md"}]

    with pytest.raises(SystemExit, match="review-substitute-attribution-missing"):
        validator._validate_review_decision(metadata, _result())

    metadata["reviewer_assessments"][0]["role"] = "Software engineer (parent substitute)"
    validator._validate_review_decision(metadata, _result())


def test_parent_substitute_assessment_must_match_covered_role() -> None:
    """Reject a substitute rating attributed to a different specialist role."""
    validator = _load_validator()
    metadata = _metadata("accept-as-is")
    metadata["specialist_passes"] = [{"role": "qa-specialist", "mode": "substituted"}]
    metadata["reviewer_assessments"] = [
        {"role": "Software engineer (parent substitute)", "rating": 3, "evidence": "parent.md"}
    ]

    with pytest.raises(SystemExit, match="review-substitute-role-mismatch:qa-specialist"):
        validator._validate_review_decision(metadata, _result())

    metadata["reviewer_assessments"][0]["role"] = "QA specialist (parent substitute)"
    validator._validate_review_decision(metadata, _result())
    metadata["reviewer_assessments"][0]["role"] = "QA Specialist (parent substitute)"
    validator._validate_review_decision(metadata, _result())


def test_candidate_assessments_cover_each_validated_reviewer(tmp_path: Path) -> None:
    """A completed QA pass cannot disappear from the reviewer ratings."""
    (tmp_path / "qa.md").write_text("QA assessment", encoding="utf-8")
    (tmp_path / "challenger.md").write_text(
        "## Reviewer Assessment\n\nRating: 3\nRationale: A blocking gap remains.\n", encoding="utf-8"
    )
    passes = {
        "qa-specialist": {"role": "qa-specialist", "mode": "inspection", "output_path": "qa.md"},
        "challenger": {"role": "challenger", "mode": "inspection", "output_path": "challenger.md"},
    }
    metadata = {"reviewer_assessments": [{"role": "Challenger", "rating": 3, "evidence": "challenger.md"}]}

    with pytest.raises(SystemExit, match="review-assessment-role-missing:qa-specialist"):
        _load_validator()._validate_reviewer_assessments(tmp_path, metadata, passes)


@pytest.mark.parametrize(
    ("role", "evidence", "error"),
    [
        pytest.param("Invented reviewer", "qa.md", "review-assessment-role-unbound", id="invented-reviewer"),
        pytest.param("QA specialist", "missing.md", "review-assessment-evidence-invalid", id="missing-pointer"),
        pytest.param("QA specialist", "../outside.md", "review-assessment-evidence-invalid", id="outside-pointer"),
        pytest.param("QA specialist", "other.md", "review-assessment-evidence-mismatch", id="wrong-reviewer-output"),
    ],
)
def test_candidate_assessments_bind_role_and_retained_output(
    tmp_path: Path, role: str, evidence: str, error: str
) -> None:
    """A rating needs its actual reviewer's retained output, within the run."""
    (tmp_path / "qa.md").write_text("QA assessment", encoding="utf-8")
    (tmp_path / "other.md").write_text("Other assessment", encoding="utf-8")
    passes = {"qa-specialist": {"role": "qa-specialist", "mode": "inspection", "output_path": "qa.md"}}
    metadata = {"reviewer_assessments": [{"role": role, "rating": 3, "evidence": evidence}]}

    with pytest.raises(SystemExit, match=error):
        _load_validator()._validate_reviewer_assessments(tmp_path, metadata, passes)


def test_candidate_assessments_allow_one_explicit_main_reviewer(tmp_path: Path) -> None:
    """The parent may retain its own assessment alongside a specialist pass."""
    (tmp_path / "qa.md").write_text(
        "## Reviewer Assessment\n\nRating: 2\nRationale: Minor changes remain.\n", encoding="utf-8"
    )
    (tmp_path / "review-notes.md").write_text(
        "## Main Reviewer Assessment\n\nRating: 3\nRationale: More work is needed.\n", encoding="utf-8"
    )
    passes = {"qa-specialist": {"role": "qa-specialist", "mode": "inspection", "output_path": "qa.md"}}
    metadata = {
        "reviewer_assessments": [
            {"role": "QA specialist", "rating": 2, "evidence": "qa.md"},
            {"role": "Main reviewer", "rating": 3, "evidence": "review-notes.md"},
        ]
    }

    _load_validator()._validate_reviewer_assessments(tmp_path, metadata, passes)
    metadata["reviewer_assessments"][0]["evidence"] = "qa.md:1"
    _load_validator()._validate_reviewer_assessments(tmp_path, metadata, passes)
    metadata["reviewer_assessments"][1]["evidence"] = "qa.md"
    with pytest.raises(SystemExit, match="review-assessment-main-evidence-reused"):
        _load_validator()._validate_reviewer_assessments(tmp_path, metadata, passes)


def test_candidate_assessments_keep_parent_substitute_role_bound(tmp_path: Path) -> None:
    """A substituted pass retains its role label and own output pointer."""
    (tmp_path / "qa.md").write_text(
        "role_id: qa-specialist\n\n## Reviewer Assessment\n\nRating: 3\nRationale: More work is needed.\n",
        encoding="utf-8",
    )
    passes = {"qa-specialist": {"role": "qa-specialist", "mode": "substituted", "output_path": "qa.md"}}
    metadata = {
        "reviewer_assessments": [{"role": "QA specialist (parent substitute)", "rating": 3, "evidence": "qa.md"}]
    }

    _load_validator()._validate_reviewer_assessments(tmp_path, metadata, passes)


def test_candidate_rejects_rating_not_stated_by_reviewer(tmp_path: Path) -> None:
    """A metadata rating must match the retained scoped reviewer judgment."""
    (tmp_path / "qa.md").write_text(
        "## Reviewer Assessment\n\nRating: 2\nRationale: Coverage is incomplete.\n", encoding="utf-8"
    )
    passes = {"qa-specialist": {"role": "qa-specialist", "mode": "inspection", "output_path": "qa.md"}}
    metadata = {"reviewer_assessments": [{"role": "QA specialist", "rating": 1, "evidence": "qa.md"}]}

    with pytest.raises(SystemExit, match="review-assessment-rating-mismatch:qa-specialist"):
        _load_validator()._validate_reviewer_assessments(tmp_path, metadata, passes)


@pytest.mark.parametrize(
    ("filename", "schema_version"),
    [
        pytest.param("result.candidate.json", 3, id="current-candidate"),
        pytest.param("result.json", 3, id="current-promoted"),
    ],
)
def test_current_assessed_result_validates_reviewer_provenance_for_both_filenames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str, schema_version: int
) -> None:
    """An assessed rating cannot bypass retained-output checks after promotion."""
    validator = _load_validator()
    result_path = tmp_path / filename
    (tmp_path / "qa.md").write_text(
        "## Reviewer Assessment\n\nRating: 2\nRationale: A minor gap remains.\n", encoding="utf-8"
    )
    (tmp_path / "specialist-manifest.json").write_text("{}", encoding="utf-8")
    qa_pass = {"role": "qa-specialist", "mode": "inspection", "output_path": "qa.md"}
    result_path.write_text(
        json.dumps(
            {
                **_result(),
                "schema_version": schema_version,
                "metadata": {
                    **_metadata("accept-as-is"),
                    "scope": "working-tree",
                    "risk_tier": "TRIVIAL",
                    "specialist_manifest": "specialist-manifest.json",
                    "specialist_passes": [qa_pass],
                    "reviewer_assessments": [{"role": "QA specialist", "rating": 1, "evidence": "qa.md"}],
                    "review_run_id": "current-run",
                    "review_input_sha256": "a" * 64,
                    "fanout_substituted": False,
                    "independence_required": False,
                    "independence_satisfied": False,
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(validator, "_require_notes_sections", lambda *_: None)
    monkeypatch.setattr(validator, "_validate_review_decision", lambda *_: None)
    monkeypatch.setattr(validator, "_validate_action_table", lambda *_: None)
    monkeypatch.setattr(validator, "_validate_confidence_gaps", lambda *_: None)
    monkeypatch.setattr(validator, "_validate_confidence_recovery", lambda *_: None)
    monkeypatch.setattr(validator, "_validate_routing", lambda *_: set())
    original_load_json = validator._load_json

    def load_result_or_manifest(path: Path) -> dict[str, object]:
        """Supply the matching manifest while retaining the real result loader."""
        if path.name == "specialist-manifest.json":
            return {"schema_version": 3, "review_run_id": "current-run", "review_input_sha256": "a" * 64}
        if path.name == "review-routing.json":
            return {"sol_selection": None}
        return original_load_json(path)

    monkeypatch.setattr(validator, "_load_json", load_result_or_manifest)
    monkeypatch.setattr(validator, "_manifest_passes", lambda *_: [])
    monkeypatch.setattr(validator, "_validate_manifest_entries", lambda *_, **__: {"qa-specialist": qa_pass})
    with pytest.raises(SystemExit, match="review-assessment-rating-mismatch:qa-specialist"):
        validator._validate_result(tmp_path, result_path, tmp_path, "thread", tmp_path)


def test_candidate_rejects_main_reviewer_evidence_from_unrelated_file(tmp_path: Path) -> None:
    """A parent rating must point to the retained parent assessment."""
    (tmp_path / "diff.patch").write_text("Unrelated diff evidence.\n", encoding="utf-8")
    (tmp_path / "review-notes.md").write_text(
        "## Main Reviewer Assessment\n\nRating: 3\nRationale: More work is needed.\n", encoding="utf-8"
    )
    metadata = {"reviewer_assessments": [{"role": "Main reviewer", "rating": 3, "evidence": "diff.patch"}]}

    with pytest.raises(SystemExit, match="review-assessment-main-evidence-mismatch"):
        _load_validator()._validate_reviewer_assessments(tmp_path, metadata, {})


def test_candidate_rejects_reviewer_without_rationale(tmp_path: Path) -> None:
    """A bare rating cannot become a supported reviewer assessment."""
    (tmp_path / "qa.md").write_text("## Reviewer Assessment\n\nRating: 3\n", encoding="utf-8")
    passes = {"qa-specialist": {"role": "qa-specialist", "mode": "inspection", "output_path": "qa.md"}}
    metadata = {"reviewer_assessments": [{"role": "QA specialist", "rating": 3, "evidence": "qa.md"}]}

    with pytest.raises(SystemExit, match="review-assessment-content-invalid:qa-specialist"):
        _load_validator()._validate_reviewer_assessments(tmp_path, metadata, passes)


def test_candidate_binds_app_server_rating_to_structured_output(tmp_path: Path) -> None:
    """A clean structured reviewer response supplies its own scoped rating."""
    (tmp_path / "qa.md").write_text(
        json.dumps({"assessment": {"rating": 2, "rationale": "A minor change remains."}, "findings": []}),
        encoding="utf-8",
    )
    passes = {"qa-specialist": {"role": "qa-specialist", "mode": "app-server", "output_path": "qa.md"}}
    metadata = {"reviewer_assessments": [{"role": "QA specialist", "rating": 1, "evidence": "qa.md"}]}

    with pytest.raises(SystemExit, match="review-assessment-rating-mismatch:qa-specialist"):
        _load_validator()._validate_reviewer_assessments(tmp_path, metadata, passes)
    metadata["reviewer_assessments"][0]["rating"] = 2
    _load_validator()._validate_reviewer_assessments(tmp_path, metadata, passes)


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
