"""Regression checks for review recommendation, reviewer-assessment, and action-table integrity.

The shipped code-review validator re-derives the merge recommendation, every reviewer rating, and each action-table row
from retained evidence. These checks load the validator by file path and drive its focused entry points with small hand-
built metadata, so each failure names the single contract that stopped being enforced.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import NamedTuple

import pytest
from _skill_text import read_with_references

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
VALIDATOR_PATH = PLUGIN_ROOT / "skills" / "code-review" / "validate_artifacts.py"
_PASSES = contextlib.nullcontext()
_ATTRIBUTED_HEADER = "| Finding / area | Author | Required change | Evidence | Status |"
_ATTRIBUTED_DIVIDER = "| --- | --- | --- | --- | --- |"
_ATTRIBUTED_ROW = "| R1 | Software engineer, QA specialist | Add a guard | config.py:12 | Required |"
_SUBSTITUTE_LABEL = "Software engineer (parent substitute)"


def _raises(code: str) -> contextlib.AbstractContextManager[object]:
    """Return the expectation that the validator exits with a message containing ``code``."""
    return pytest.raises(SystemExit, match=code)


@pytest.fixture(name="validator")
def _validator() -> ModuleType:
    """Load the standalone review validator without package installation."""
    specification = importlib.util.spec_from_file_location("codex_rig_review_validator", VALIDATOR_PATH)
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _result(
    *, critical: int = 0, high: int = 0, medium: int = 0, low: int = 0, status: str = "pass", checks_failed=None
) -> dict[str, object]:
    """Return a review result with one explicit severity-count vector and gate outcome.

    Example:
        >>> _result(high=1)["findings"]["high"]
        1
    """
    return {
        "status": status,
        "checks_failed": list(checks_failed or []),
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


def _pass(role: str = "qa-specialist", mode: str = "inspection", output: str = "qa.md") -> dict[str, str]:
    """Return one specialist pass record pointing at its retained output file."""
    return {"role": role, "mode": mode, "output_path": output}


def _rating(role: str, rating: int, evidence: str) -> dict[str, object]:
    """Return one reviewer assessment record in the shape the review metadata stores."""
    return {"role": role, "rating": rating, "evidence": evidence}


def _write_assessment(path: Path, rating: int, rationale: str, heading: str = "Reviewer Assessment") -> None:
    """Write a retained reviewer output stating one rating and its rationale."""
    path.write_text(f"## {heading}\n\nRating: {rating}\nRationale: {rationale}\n", encoding="utf-8")


def _review_finding(**overrides: object) -> dict[str, object]:
    """Return one canonical finding record, with any field overridden."""
    finding: dict[str, object] = {
        "id": "R1",
        "severity": "high",
        "title": "Empty input fails",
        "summary": "Input is unguarded.",
        "required_change": "Add a guard",
        "evidence": ["config.py:12"],
        "closure_evidence": "Empty input passes.",
    }
    return {**finding, **overrides}


class TestReviewDecision:
    """The merge recommendation must agree with finding severities, quality gates, and substitute disclosure."""

    @pytest.mark.parametrize(
        ("recommendation", "result", "error"),
        [
            pytest.param("accept-as-is", _result(high=1), "review-accept-with-findings", id="accept-with-high"),
            pytest.param("minor-changes", _result(high=1), "review-minor-with-blocking-findings", id="minor-with-high"),
            pytest.param(
                "minor-changes", _result(critical=1), "review-minor-with-blocking-findings", id="minor-with-critical"
            ),
        ],
    )
    def test_rejects_approval_contradicting_findings(
        self, validator: ModuleType, recommendation: str, result: dict[str, object], error: str
    ) -> None:
        """Approval or minor-change recommendations are rejected when blocking findings exist.

        A result with a high or critical finding cannot be accepted as is, and cannot be called a minor change.
        """
        with pytest.raises(SystemExit, match=error):
            validator._validate_review_decision(_metadata(recommendation), result)

    @pytest.mark.parametrize("recommendation", ["accept-as-is", "minor-changes"])
    @pytest.mark.parametrize(
        ("status", "checks_failed"),
        [
            pytest.param("fail", ["tests"], id="failed-check"),
            pytest.param("timeout", ["tests"], id="timeout"),
            pytest.param("fail", [], id="failed-process"),
        ],
    )
    def test_rejects_approval_with_failed_gates(
        self, validator: ModuleType, recommendation: str, status: str, checks_failed: list[str]
    ) -> None:
        """Failed or incomplete checks require a non-approval decision even with zero findings.

        The finding counts are all zero, so only the gate outcome can forbid the approving recommendation.
        """
        result = _result(status=status, checks_failed=checks_failed)

        with pytest.raises(SystemExit, match="review-approval-with-failed-gates"):
            validator._validate_review_decision(_metadata(recommendation), result)

    @pytest.mark.parametrize(
        ("recommendation", "status", "checks_failed"),
        [
            pytest.param("accept-as-is", "pass", [], id="accept-with-passing-gates"),
            pytest.param("minor-changes", "pass", [], id="minor-with-passing-gates"),
            pytest.param("needs-more-work", "fail", ["tests"], id="needs-work-with-failed-gate"),
        ],
    )
    def test_accepts_passing_and_non_approval_decisions(
        self, validator: ModuleType, recommendation: str, status: str, checks_failed: list[str]
    ) -> None:
        """Honest failed reviews stay reportable while contradictory approvals are still rejected.

        The gate binding must not turn a legitimate `needs-more-work` verdict on failed checks into an error.
        """
        result = _result(status=status, checks_failed=checks_failed)

        validator._validate_review_decision(_metadata(recommendation), result)

    @pytest.mark.parametrize(
        ("role", "expectation"),
        [
            pytest.param("Software engineer", _raises("review-substitute-attribution-missing"), id="undisclosed"),
            pytest.param(_SUBSTITUTE_LABEL, _PASSES, id="disclosed"),
        ],
    )
    def test_substitute_assessment_discloses_parent_origin(
        self, validator: ModuleType, role: str, expectation: contextlib.AbstractContextManager[object]
    ) -> None:
        """A parent judgment cannot appear as an independent specialist rating.

        The software-engineer pass was substituted by the parent, so its rating must carry the parent-substitute label.
        """
        metadata = _metadata("accept-as-is")
        metadata["specialist_passes"] = [_pass("sw-engineer", "substituted", "sw.md")]
        metadata["reviewer_assessments"] = [_rating(role, 3, "parent.md")]

        with expectation:
            validator._validate_review_decision(metadata, _result())

    @pytest.mark.parametrize(
        ("role", "expectation"),
        [
            pytest.param(_SUBSTITUTE_LABEL, _raises("review-substitute-role-mismatch:qa-specialist"), id="other-role"),
            pytest.param("QA specialist (parent substitute)", _PASSES, id="matching-role"),
            pytest.param("QA Specialist (parent substitute)", _PASSES, id="matching-role-any-case"),
        ],
    )
    def test_substitute_assessment_matches_covered_role(
        self, validator: ModuleType, role: str, expectation: contextlib.AbstractContextManager[object]
    ) -> None:
        """A substitute rating must be attributed to the specialist role it covers, ignoring label case.

        The QA pass was substituted, so a rating labelled as the software engineer's substitute covers the wrong role.
        """
        metadata = _metadata("accept-as-is")
        metadata["specialist_passes"] = [_pass("qa-specialist", "substituted", "qa.md")]
        metadata["reviewer_assessments"] = [_rating(role, 3, "parent.md")]

        with expectation:
            validator._validate_review_decision(metadata, _result())


class _Attributed(NamedTuple):
    """One attributed review: its notes file and body, result, and metadata."""

    notes: Path
    body: str
    result: dict[str, object]
    metadata: dict[str, object]


def _attributed_table(header: str = _ATTRIBUTED_HEADER, divider: str = _ATTRIBUTED_DIVIDER) -> str:
    """Return review notes holding one attributed finding row under the given header and divider lines."""
    return f"## Review Findings and Merge Blocks\n\n{header}\n{divider}\n{_ATTRIBUTED_ROW}\n"


def _attributed_review() -> tuple[dict[str, object], dict[str, object]]:
    """Return the result and metadata of a review whose one high finding is authored by two reviewers."""
    result = {**_result(high=1), "schema_version": 2}
    metadata = _metadata("needs-more-work")
    metadata.update(
        finding_records_version=1,
        reviewer_assessments=[_rating("Software engineer", 3, "sw.md"), _rating("QA specialist", 2, "qa.md")],
        review_findings=[_review_finding(authors=["Software engineer", "QA specialist"])],
    )
    return result, metadata


@pytest.fixture(
    name="accepted_attributed",
    params=[
        pytest.param("valid", id="valid-attributed-table"),
        pytest.param("published-skill", id="published-skill-table-passes-consumer"),
        pytest.param("parent-substitute", id="role-only-author-matches-retained-provenance"),
    ],
)
def _accepted_attributed(request: pytest.FixtureRequest, tmp_path: Path) -> _Attributed:
    """Build one attributed review the validator must accept, in each form it is expected to read."""
    result, metadata = _attributed_review()
    body = _attributed_table()
    if request.param == "published-skill":
        skill = read_with_references(PLUGIN_ROOT / "skills" / "code-review" / "SKILL.md", "findings-and-completion.md")
        lines = skill.splitlines()
        start = next(index for index, line in enumerate(lines) if line.startswith(_ATTRIBUTED_HEADER))
        body = _attributed_table(header=lines[start], divider=lines[start + 1])
    if request.param == "parent-substitute":
        metadata["reviewer_assessments"][0]["role"] = _SUBSTITUTE_LABEL
        metadata["specialist_passes"] = [_pass("sw-engineer", "substituted", "sw.md")]
        metadata["review_findings"][0]["authors"][0] = _SUBSTITUTE_LABEL
    notes = tmp_path / "review-notes.md"
    notes.write_text(body, encoding="utf-8")
    return _Attributed(notes, body, result, metadata)


class TestActionTable:
    """The rendered action table must bind rows and authors to the canonical finding records."""

    def test_rejects_duplicate_finding_identity(self, validator: ModuleType, tmp_path: Path) -> None:
        """Two actions cannot silently claim the same finding or operational area.

        Both rows are labelled R1, so the table would hide that one finding appears to be handled twice.
        """
        notes = tmp_path / "review-notes.md"
        notes.write_text(
            "## Review Findings and Merge Blocks\n\n"
            "| Finding / area | Required change | Evidence | Status |\n"
            "| --- | --- | --- | --- |\n"
            "| R1 | Add a guard | tests | Required |\n"
            "| R1 | Add tests | tests | Required verification |\n",
            encoding="utf-8",
        )

        with pytest.raises(SystemExit, match="review-findings-action-table-identity-duplicate:R1"):
            validator._validate_action_table(notes, _result(high=2), _metadata("needs-more-work"), "pr")

    def test_accepts_attributed_table(self, validator: ModuleType, accepted_attributed: _Attributed) -> None:
        """A table whose authors match the retained provenance passes, whether hand-built or taken from the skill.

        The published skill template is validated against its own consumer so the documented table and the validator
        cannot drift apart; a parent-substitute label is accepted when the metadata records the same label.
        """
        notes, _, result, metadata = accepted_attributed

        validator._validate_review_decision(metadata, result)
        validator._validate_action_table(notes, result, metadata, "pr")

    @pytest.mark.parametrize(
        ("table_kwargs", "error"),
        [
            pytest.param(
                {"divider": "| --- | -- | --- | --- | --- |"},
                "review-findings-action-table-divider-invalid",
                id="invalid-author-divider",
            ),
            pytest.param(
                {"divider": "| -- | --- | --- | --- | --- |"},
                "review-findings-action-table-divider-invalid",
                id="invalid-other-divider",
            ),
            pytest.param(
                {"header": "| Author | Finding / area | Required change | Evidence | Status |"},
                "review-findings-action-table-header-mismatch",
                id="changed-header",
            ),
        ],
    )
    def test_rejects_malformed_attributed_table(
        self, validator: ModuleType, tmp_path: Path, table_kwargs: dict[str, str], error: str
    ) -> None:
        """A broken divider is reported as a divider fault, and a reordered header as a header mismatch.

        Both defects leave the row text intact, so only the table frame tells the reader which part is malformed.
        """
        result, metadata = _attributed_review()
        notes = tmp_path / "review-notes.md"
        notes.write_text(_attributed_table(**table_kwargs), encoding="utf-8")

        with pytest.raises(SystemExit, match=error):
            validator._validate_action_table(notes, result, metadata, "pr")

    def test_rejects_table_authors_not_matching_finding(
        self, validator: ModuleType, accepted_attributed: _Attributed
    ) -> None:
        """A row naming fewer authors than the canonical finding is rejected.

        Dropping one reviewer from the rendered Author cell would erase their attribution from the report.
        """
        notes, body, result, metadata = accepted_attributed
        notes.write_text(body.replace("Software engineer, QA specialist", "QA specialist"), encoding="utf-8")

        with pytest.raises(SystemExit, match="review-findings-action-table-authors-mismatch"):
            validator._validate_action_table(notes, result, metadata, "pr")

    def test_rejects_author_without_retained_assessment(
        self, validator: ModuleType, accepted_attributed: _Attributed
    ) -> None:
        """A finding author must be a reviewer with a retained assessment.

        `Skipped reviewer` never produced a rating, so naming them as an author is unsupported attribution.
        """
        _, _, result, metadata = accepted_attributed
        metadata["review_findings"][0]["authors"] = ["Skipped reviewer"]

        with pytest.raises(SystemExit, match="review-finding-authors-invalid"):
            validator._validate_review_decision(metadata, result)

    def test_rejects_author_column_without_attribution(self, validator: ModuleType, tmp_path: Path) -> None:
        """A rendered Author column that no retained assessment can bind is refused.

        The finding record has no authors, so a table that adds an (empty) Author column asserts attribution that the
        review never recorded.
        """
        result = {**_result(high=1), "schema_version": 2}
        metadata = _metadata("needs-more-work")
        metadata.update(finding_records_version=1, review_findings=[_review_finding()])
        notes = tmp_path / "review-notes.md"
        notes.write_text(
            "## Review Findings and Merge Blocks\n\n"
            f"{_ATTRIBUTED_HEADER}\n{_ATTRIBUTED_DIVIDER}\n"
            "| R1 |  | Add a guard | config.py:12 | Required |\n",
            encoding="utf-8",
        )

        with pytest.raises(SystemExit, match="review-findings-action-table-authors-unbound"):
            validator._validate_action_table(notes, result, metadata, "pr")

    def test_accepts_identifier_only_operational_blocker(self, validator: ModuleType, tmp_path: Path) -> None:
        """An ID-only operational blocker stays representable once reviewer attribution is required.

        The blocker has an identifier and an author but no finding detail, and its table row must still validate.
        """
        result, metadata = _blocker_review({"id": "OB1", "authors": ["QA specialist"]})
        notes = tmp_path / "review-notes.md"
        notes.write_text(
            "## Review Findings and Merge Blocks\n\n"
            f"{_ATTRIBUTED_HEADER}\n{_ATTRIBUTED_DIVIDER}\n"
            "| OB1 | QA specialist | Restore the runner | ci.log:4 | Required |\n",
            encoding="utf-8",
        )

        validator._validate_review_decision(metadata, result)
        validator._validate_action_table(notes, result, metadata, "pr")

    def test_rejects_operational_blocker_without_authors(self, validator: ModuleType) -> None:
        """An operational blocker with no authors is rejected when attribution is required.

        The identifier-only shape is valid only with its authors, so dropping them must fail.
        """
        result, metadata = _blocker_review({"id": "OB1"})

        with pytest.raises(SystemExit, match="review-finding-authors-invalid"):
            validator._validate_review_decision(metadata, result)

    def test_undecidable_review_needs_no_synthetic_action(self, validator: ModuleType, tmp_path: Path) -> None:
        """A limited source judgment needs a disclosure, not an invented review-request row.

        The recommendation is undecidable (`None`) with no findings, so the notes carry only the verification-limit
        sentence and the table check must not demand an action row.
        """
        notes = tmp_path / "review-notes.md"
        notes.write_text("Verification limited: independent coverage unavailable.\n", encoding="utf-8")
        metadata = {
            "review_decision": {"recommendation": None},
            "finding_records_version": 2,
            "review_findings": [],
            "operational_blockers": [],
        }
        result = {"findings": dict.fromkeys(("critical", "high", "medium", "low"), 0)}

        validator._validate_action_table(notes, result, metadata, "pr")


def _blocker_review(blocker: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    """Return the result and metadata of an attributed review with no findings and one operational blocker."""
    result = {**_result(), "schema_version": 2}
    metadata = _metadata("needs-more-work")
    metadata.update(
        finding_records_version=1,
        review_findings=[],
        reviewer_assessments=[_rating("QA specialist", 3, "qa.md")],
        operational_blockers=[blocker],
    )
    return result, metadata


class TestReviewerAssessments:
    """Each reviewer rating must bind to a real role and to that reviewer's retained output within the run."""

    def test_covers_each_validated_reviewer(self, validator: ModuleType, tmp_path: Path) -> None:
        """A completed QA pass cannot disappear from the reviewer ratings.

        The manifest records QA and challenger passes but the ratings list only the challenger.
        """
        (tmp_path / "qa.md").write_text("QA assessment", encoding="utf-8")
        _write_assessment(tmp_path / "challenger.md", 3, "A blocking gap remains.")
        passes = {"qa-specialist": _pass(), "challenger": _pass("challenger", output="challenger.md")}
        metadata = {"reviewer_assessments": [_rating("Challenger", 3, "challenger.md")]}

        with pytest.raises(SystemExit, match="review-assessment-role-missing:qa-specialist"):
            validator._validate_reviewer_assessments(tmp_path, metadata, passes)

    @pytest.mark.parametrize(
        ("role", "evidence", "error"),
        [
            pytest.param("Invented reviewer", "qa.md", "review-assessment-role-unbound", id="invented-reviewer"),
            pytest.param("QA specialist", "missing.md", "review-assessment-evidence-invalid", id="missing-pointer"),
            pytest.param("QA specialist", "../outside.md", "review-assessment-evidence-invalid", id="outside-pointer"),
            pytest.param(
                "QA specialist", "other.md", "review-assessment-evidence-mismatch", id="wrong-reviewer-output"
            ),
        ],
    )
    def test_binds_role_and_retained_output(
        self, validator: ModuleType, tmp_path: Path, role: str, evidence: str, error: str
    ) -> None:
        """A rating needs its actual reviewer's retained output, within the run.

        Each row breaks one link: an unknown reviewer, evidence that is missing or escapes the run, or evidence that
        belongs to a different reviewer's output file.
        """
        (tmp_path / "qa.md").write_text("QA assessment", encoding="utf-8")
        (tmp_path / "other.md").write_text("Other assessment", encoding="utf-8")
        metadata = {"reviewer_assessments": [_rating(role, 3, evidence)]}

        with pytest.raises(SystemExit, match=error):
            validator._validate_reviewer_assessments(tmp_path, metadata, {"qa-specialist": _pass()})

    def test_reports_canonical_labels_for_unbound_role(self, validator: ModuleType, tmp_path: Path) -> None:
        """An invalid display label names the assessment labels needed for recovery.

        The rejection message lists the expected labels (with substitute disclosure) and the optional main reviewer.
        """
        passes = {
            "cicd-steward": _pass("cicd-steward", output="ci.md"),
            "qa-specialist": _pass(mode="substituted"),
        }
        metadata = {"reviewer_assessments": [_rating("CI/CD steward", 3, "ci.md")]}

        with pytest.raises(SystemExit) as exc:
            validator._validate_reviewer_assessments(tmp_path, metadata, passes)

        assert str(exc.value) == (
            "review-assessment-role-unbound:CI/CD steward; expected: CICD steward, "
            "QA specialist (parent substitute); optional: Main reviewer"
        )

    @pytest.mark.parametrize(
        ("qa_evidence", "main_evidence", "expectation"),
        [
            pytest.param("qa.md", "review-notes.md", _PASSES, id="distinct-evidence"),
            pytest.param("qa.md:1", "review-notes.md", _PASSES, id="specialist-evidence-with-line"),
            pytest.param(
                "qa.md:1", "qa.md", _raises("review-assessment-main-evidence-reused"), id="main-reuses-specialist-file"
            ),
        ],
    )
    def test_allows_one_explicit_main_reviewer(
        self,
        validator: ModuleType,
        tmp_path: Path,
        qa_evidence: str,
        main_evidence: str,
        expectation: contextlib.AbstractContextManager[object],
    ) -> None:
        """The parent may retain its own assessment alongside a specialist pass, but not reuse the specialist's file.

        The main reviewer's rating needs its own retained notes; pointing it at the specialist output would credit the
        parent with the specialist's judgment.
        """
        _write_assessment(tmp_path / "qa.md", 2, "Minor changes remain.")
        _write_assessment(tmp_path / "review-notes.md", 3, "More work is needed.", heading="Main Reviewer Assessment")
        metadata = {
            "reviewer_assessments": [
                _rating("QA specialist", 2, qa_evidence),
                _rating("Main reviewer", 3, main_evidence),
            ]
        }

        with expectation:
            validator._validate_reviewer_assessments(tmp_path, metadata, {"qa-specialist": _pass()})

    @pytest.mark.parametrize(
        ("roles", "expectation"),
        [
            pytest.param(
                ("QA specialist", "Software engineer"), _raises("review-primary-software-engineer-order"), id="qa-first"
            ),
            pytest.param(("Software engineer", "QA specialist"), _PASSES, id="software-engineer-first"),
        ],
    )
    def test_python_source_assessment_starts_with_software_engineer(
        self,
        validator: ModuleType,
        tmp_path: Path,
        roles: tuple[str, str],
        expectation: contextlib.AbstractContextManager[object],
    ) -> None:
        """The primary Python reviewer is listed first and its rating binds to a real pass.

        Both specialists ran with valid evidence, so only the order of the ratings decides whether the check passes.
        """
        _write_assessment(tmp_path / "sw.md", 2, "A Python behavior gap remains.")
        _write_assessment(tmp_path / "qa.md", 2, "A regression test is missing.")
        passes = {"sw-engineer": _pass("sw-engineer", output="sw.md"), "qa-specialist": _pass()}
        evidence = {"Software engineer": "sw.md", "QA specialist": "qa.md"}
        metadata = {"reviewer_assessments": [_rating(role, 2, evidence[role]) for role in roles]}

        with expectation:
            validator._validate_reviewer_assessments(tmp_path, metadata, passes)

    def test_keeps_parent_substitute_role_bound(self, validator: ModuleType, tmp_path: Path) -> None:
        """A substituted pass retains its role label and own output pointer.

        The output starts with a `role_id` line naming the covered specialist, which ties the substitute rating to it.
        """
        (tmp_path / "qa.md").write_text(
            "role_id: qa-specialist\n\n## Reviewer Assessment\n\nRating: 3\nRationale: More work is needed.\n",
            encoding="utf-8",
        )
        passes = {"qa-specialist": _pass(mode="substituted")}
        metadata = {"reviewer_assessments": [_rating("QA specialist (parent substitute)", 3, "qa.md")]}

        validator._validate_reviewer_assessments(tmp_path, metadata, passes)

    @pytest.mark.parametrize(
        ("assessment", "rating", "error"),
        [
            pytest.param(
                "## Reviewer Assessment\n\nRating: 2\nRationale: Coverage is incomplete.\n",
                1,
                "review-assessment-rating-mismatch:qa-specialist",
                id="rating-not-stated-by-reviewer",
            ),
            pytest.param(
                "## Reviewer Assessment\n\nRating: 3\n",
                3,
                "review-assessment-content-invalid:qa-specialist",
                id="reviewer-without-rationale",
            ),
        ],
    )
    def test_rejects_unsupported_rating(
        self, validator: ModuleType, tmp_path: Path, assessment: str, rating: int, error: str
    ) -> None:
        """A metadata rating must match the retained scoped reviewer judgment, and a bare rating is never supported.

        The retained reviewer output either states a different rating than the metadata claims, or states a rating with
        no rationale; both leave the recorded assessment unsupported by its evidence file.
        """
        (tmp_path / "qa.md").write_text(assessment, encoding="utf-8")
        metadata = {"reviewer_assessments": [_rating("QA specialist", rating, "qa.md")]}

        with pytest.raises(SystemExit, match=error):
            validator._validate_reviewer_assessments(tmp_path, metadata, {"qa-specialist": _pass()})

    def test_rejects_main_reviewer_evidence_from_unrelated_file(self, validator: ModuleType, tmp_path: Path) -> None:
        """A parent rating must point to the retained parent assessment.

        The notes hold a valid main assessment, but the rating cites `diff.patch`, which proves nothing about it.
        """
        (tmp_path / "diff.patch").write_text("Unrelated diff evidence.\n", encoding="utf-8")
        _write_assessment(tmp_path / "review-notes.md", 3, "More work is needed.", heading="Main Reviewer Assessment")
        metadata = {"reviewer_assessments": [_rating("Main reviewer", 3, "diff.patch")]}

        with pytest.raises(SystemExit, match="review-assessment-main-evidence-mismatch"):
            validator._validate_reviewer_assessments(tmp_path, metadata, {})

    @pytest.mark.parametrize(
        ("rating", "expectation"),
        [
            pytest.param(1, _raises("review-assessment-rating-mismatch:qa-specialist"), id="rating-differs"),
            pytest.param(2, _PASSES, id="rating-matches"),
        ],
    )
    def test_binds_local_reviewer_wave_rating_to_structured_output(
        self,
        validator: ModuleType,
        tmp_path: Path,
        rating: int,
        expectation: contextlib.AbstractContextManager[object],
    ) -> None:
        """A clean structured reviewer response supplies its own scoped rating.

        The local reviewer wave stores JSON instead of a Markdown heading, and the metadata rating must equal the
        `assessment.rating` field inside it.
        """
        (tmp_path / "qa.md").write_text(
            json.dumps({"assessment": {"rating": 2, "rationale": "A minor change remains."}, "findings": []}),
            encoding="utf-8",
        )
        passes = {"qa-specialist": _pass(mode="app-server")}
        metadata = {"reviewer_assessments": [_rating("QA specialist", rating, "qa.md")]}

        with expectation:
            validator._validate_reviewer_assessments(tmp_path, metadata, passes)


def _stub_unrelated_result_checks(
    monkeypatch: pytest.MonkeyPatch, validator: ModuleType, qa_pass: dict[str, str], input_hash: str
) -> None:
    """Replace every result check except the reviewer-assessment one, and feed a matching manifest to the loader."""
    original_load_json = validator._load_json

    def load_result_or_manifest(path: Path) -> dict[str, object]:
        """Supply the matching manifest while retaining the real result loader."""
        if path.name == "specialist-manifest.json":
            return {"schema_version": 3, "review_run_id": "current-run", "review_input_sha256": input_hash}
        if path.name == "review-routing.json":
            return {"sol_selection": None}
        return original_load_json(path)

    for name in (
        "_require_notes_sections",
        "_validate_review_decision",
        "_validate_action_table",
        "_validate_confidence_gaps",
        "_validate_confidence_recovery",
    ):
        monkeypatch.setattr(validator, name, lambda *_: None)
    monkeypatch.setattr(validator, "_validate_routing", lambda *_: set())
    monkeypatch.setattr(validator, "_load_json", load_result_or_manifest)
    monkeypatch.setattr(validator, "_manifest_passes", lambda *_: [])
    monkeypatch.setattr(validator, "_validate_manifest_entries", lambda *_, **__: {"qa-specialist": qa_pass})


class TestAssessedResult:
    """A whole assessed result must run the reviewer-provenance checks under either filename."""

    @pytest.mark.parametrize("filename", ["result.candidate.json", "result.json"])
    def test_validates_reviewer_provenance_before_and_after_promotion(
        self, validator: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str
    ) -> None:
        """An assessed rating cannot bypass retained-output checks after promotion.

        The retained QA output states rating 2 while the result claims 1. Every other result check is stubbed out, so a
        mismatch error can only come from the reviewer-assessment check, for the candidate and the promoted filename.
        """
        result_path = tmp_path / filename
        (tmp_path / "diff.patch").write_bytes(b"")
        input_hash = hashlib.sha256(b"").hexdigest()
        _write_assessment(tmp_path / "qa.md", 2, "A minor gap remains.")
        (tmp_path / "specialist-manifest.json").write_text("{}", encoding="utf-8")
        qa_pass = _pass()
        metadata = {
            **_metadata("accept-as-is"),
            "scope": "working-tree",
            "risk_tier": "TRIVIAL",
            "specialist_manifest": "specialist-manifest.json",
            "specialist_passes": [qa_pass],
            "reviewer_assessments": [_rating("QA specialist", 1, "qa.md")],
            "finding_records_version": 2,
            "review_run_id": "current-run",
            "review_input_sha256": input_hash,
            "fanout_substituted": False,
            "independence_required": False,
            "independence_satisfied": False,
        }
        result_path.write_text(json.dumps({**_result(), "schema_version": 3, "metadata": metadata}), encoding="utf-8")
        _stub_unrelated_result_checks(monkeypatch, validator, qa_pass, input_hash)

        with pytest.raises(SystemExit, match="review-assessment-rating-mismatch:qa-specialist"):
            validator._validate_result(tmp_path, result_path, tmp_path, "thread", tmp_path)
