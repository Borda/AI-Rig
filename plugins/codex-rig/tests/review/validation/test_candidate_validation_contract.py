"""Evidence admission and versioned source judgments, exercised at the review decision boundary."""

from __future__ import annotations

import ast
import copy
import hashlib
import importlib
import importlib.util
import json
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any, NamedTuple

import pytest
from _modules import load_module
from test_code_review_decision_validation import _metadata, _result
from test_review_batches import _batch_command, _batch_finding, _completed_batches
from test_review_completion_gate import FINDER, PLUGIN_ROOT, _assessed_pr, _module

REVIEW_VALIDATOR = PLUGIN_ROOT / "skills" / "code-review" / "validate_artifacts.py"
RECOMMENDATION_LEGEND = "4 = Insufficient, but a good initiative · 5 = Block."
OBLIGATION_NOTICE = "not a confirmed defect or automatic edit request"
CLOSED_DISPOSITIONS = ("confirmed", "refuted", "already-fixed", "optional-out-of-scope")
#: Dispositions other than ``confirmed``; the pending (``None``) concern is the open sibling of ``unverified``.
NON_CONFIRMED = ["refuted", "already-fixed", "optional-out-of-scope", pytest.param(None, id="pending"), "unverified"]
REVIEWED_DOCS = {"doc-scribe": {"reviewed": ["docs.md"], "missing": []}}
UNINSPECTED_DOCS = {"doc-scribe": {"reviewed": [], "missing": ["docs.md — source was not inspected"]}}


def _candidate(disposition: str | None = "confirmed") -> dict[str, object]:
    """Return one atomic, revision-bound candidate with an explicit performed check."""
    return {
        "id": "candidate-a",
        "severity": "low",
        "claim": "The documented option is misspelled.",
        "source_revision": "frozen-source",
        "contract": "The documented option must exist.",
        "trigger": "Use the documented invocation.",
        "confirmation_evidence": "Compare the option declaration.",
        "falsification_evidence": "An alias would settle this claim.",
        "counterevidence": [],
        "check": "Inspected declaration and aliases.",
        "result": "The option is absent.",
        "environment": "frozen source inspection",
        "limits": [],
        "disposition": disposition,
        "reason": "The published invocation cannot be used.",
        "feasible_checks_complete": True,
        "missing_prerequisite": "",
        "owner": "review owner",
        "next_check": "Inspect option aliases.",
        "closure_condition": "The documented option resolves.",
    }


def _new_metadata(disposition: str | None = "confirmed") -> dict[str, object]:
    """Build the new family independently of the historical five-value rating family."""
    metadata = _metadata("minor-changes")
    metadata["finding_records_version"] = 2
    metadata["review_validation"] = {
        "schema_version": 1,
        "source_rating_version": 1,
        "source_verdict": "minor-changes",
        "verification_readiness": "complete",
        "candidates": [_candidate(disposition)],
    }
    metadata["review_findings"] = [
        {
            "id": "candidate-a",
            "severity": "low",
            "title": "Misspelled option",
            "summary": "The option is absent.",
            "required_change": "Correct the option.",
            "evidence": ["guide.md:1"],
            "closure_evidence": "The option resolves.",
        }
    ]
    return metadata


def _assessment(
    heading: str, rating: object, rationale: str, *, readiness: str | None = None, version: int | None = 1
) -> str:
    """Render a reviewer assessment section; a ``None`` version or readiness omits that line."""
    lines = [
        f"## {heading}",
        f"Source rating version: {version}" if version else "",
        f"Rating: {rating}",
        f"Verification readiness: {readiness}" if readiness else "",
        f"Rationale: {rationale}",
    ]
    return "\n".join(line for line in lines if line) + "\n"


def _resolve_pointer(document: object, pointer: str) -> object:
    """Resolve the ``#/a/0/b`` fragment of a source id inside ``document``."""
    value = document
    for token in pointer.split("#", 1)[1].strip("/").split("/"):
        value = value[int(token)] if isinstance(value, list) else value[token]
    return value


def _complete_run(script: Path, run: Path) -> subprocess.CompletedProcess[str]:
    """Run the report finder's completion check over one review run with the real script."""
    return subprocess.run(
        [sys.executable, str(script), "--complete-run", str(run), "--parent-thread-id", "thread"],
        capture_output=True,
        text=True,
        check=False,
    )


def _archive_with_legacy_reader(archived: Path) -> Path:
    """Copy the shipped review code to ``archived`` and swap its finding reader for the legacy one."""
    for directory in ("shared", "skills/code-review", "roles"):
        shutil.copytree(PLUGIN_ROOT / directory, archived / directory)
    validator_path = archived / "skills/code-review/validate_artifacts.py"
    source = validator_path.read_text(encoding="utf-8")
    declaration = next(
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef) and node.name == "_review_finding_identities"
    )
    legacy = (Path(__file__).parent / "fixtures/legacy-finding-records-reader.txt").read_text(encoding="utf-8")
    lines = source.splitlines(keepends=True)
    validator_path.write_text(
        "".join(lines[: declaration.lineno - 1]) + legacy + "".join(lines[declaration.end_lineno :]), encoding="utf-8"
    )
    return archived


def _missing(text: str, *phrases: str) -> list[str]:
    """Return the phrases absent from the whitespace-collapsed ``text``; empty means all are stated."""
    prose = " ".join(text.split())
    return [phrase for phrase in phrases if phrase not in prose]


@pytest.fixture(scope="module")
def validator() -> ModuleType:
    """Load the standalone review validator once; its checks keep no state between calls."""
    return load_module("candidate_review_validator", REVIEW_VALIDATOR)


@pytest.fixture(scope="module")
def shared_validator() -> Any:
    """Load the shared artifact validator once."""
    return load_module("candidate_shared_validator", PLUGIN_ROOT / "shared" / "validate-artifacts.py")


@pytest.fixture(scope="module")
def finalizer() -> Any:
    """Load the standalone handoff finalizer once."""
    return load_module("candidate_final_handoff", PLUGIN_ROOT / "shared" / "final_handoff.py")


@pytest.fixture(scope="module")
def finder() -> ModuleType:
    """Load the review report finder module once."""
    return _module(FINDER)


@pytest.fixture
def assessed_run(tmp_path: Path) -> Path:
    """Create a complete assessed PR review run on disk with real routing and gate artifacts."""
    return _assessed_pr.__wrapped__(tmp_path)


class TestReviewDecision:
    """Source judgments and candidate dispositions accepted or refused by the review decision gate."""

    def test_unverified_candidate_cannot_enter_confirmed_finding_collection(self, validator: ModuleType):
        """Unavailable evidence is never promoted into an automatic defect edit request.

        An `unverified` candidate with blocked verification still sits in the confirmed review findings, so the
        inventory of confirmed findings no longer matches the candidates.
        """
        metadata = _new_metadata("unverified")
        metadata["review_validation"]["verification_readiness"] = "blocked"
        metadata["review_validation"]["candidates"][0]["missing_prerequisite"] = "Native backend unavailable."

        with pytest.raises(SystemExit, match="review-candidate-confirmed-inventory-mismatch"):
            validator._validate_review_decision(metadata, _result(low=1))

    def test_feasible_unperformed_check_is_incomplete_not_unverified(self, validator: ModuleType):
        """An environmental `unverified` label is refused while a bounded available check remains unperformed.

        The candidate says its feasible checks are not complete, so the honest state is incomplete work, not an
        unavailable prerequisite.
        """
        metadata = _new_metadata("unverified")
        metadata["review_validation"]["candidates"][0]["feasible_checks_complete"] = False

        with pytest.raises(SystemExit, match="review-candidate-unverified-feasible-checks"):
            validator._validate_review_decision(metadata, _result(low=1))

    def test_accepts_confirmed_minor_judgment(self, validator: ModuleType):
        """Confirmed minor source changes are accepted and stay distinct from substantive defects.

        One confirmed low-severity candidate with complete verification is the baseline shape of the new family.
        """
        validator._validate_review_decision(_new_metadata(), _result(low=1))

    def test_requires_old_reader_rejection_marker(self, validator: ModuleType):
        """New semantics demand a records marker that makes older consumers reject the result.

        A `finding_records_version` of 1 on a new-family result would let an old reader silently accept semantics it
        does not understand.
        """
        metadata = _new_metadata()
        metadata["finding_records_version"] = 1

        with pytest.raises(SystemExit, match="review-validation-finding-records-version"):
            validator._validate_review_decision(metadata, _result(low=1))

    def test_unverified_requires_unavailable_prerequisite(self, validator: ModuleType):
        """An open concern must name the exact missing prerequisite.

        An `unverified` candidate with an empty prerequisite is an unexplained doubt, not an evidence obligation.
        """
        with pytest.raises(SystemExit, match="review-candidate-missing_prerequisite"):
            validator._validate_review_decision(_new_metadata("unverified"), _result(low=1))

    def test_incomplete_feasible_work_cannot_pass(self, validator: ModuleType):
        """A terminal passing result is refused while available verification remains undone.

        A pending candidate with unfinished feasible checks and no findings looks clean, but must not pass.
        """
        metadata = _new_metadata(None)
        metadata["review_validation"]["candidates"][0]["feasible_checks_complete"] = False
        metadata["review_validation"]["verification_readiness"] = "incomplete"
        metadata["review_findings"] = []

        with pytest.raises(SystemExit, match="review-incomplete-cannot-pass"):
            validator._validate_review_decision(metadata, _result())

    def test_legacy_rating_four_remains_readable(self, validator: ModuleType):
        """A historical raw rating of four is accepted without claiming the new rejection semantics.

        Old reviews recorded four as insufficient evidence; they must keep validating unchanged.
        """
        metadata = _metadata("needs-more-work")
        metadata["reviewer_assessments"] = [{"role": "Main reviewer", "rating": 4, "evidence": "review-notes.md"}]

        validator._validate_review_decision(copy.deepcopy(metadata), _result())

    def test_qualified_minor_judgment_keeps_failed_gate_status(self, validator: ModuleType):
        """A known minor source issue stays publishable while the actual required gates remain failed.

        Blocked verification plus a failed backend gate must not be cleared by the minor source judgment.
        """
        metadata = _new_metadata()
        metadata["review_validation"]["verification_readiness"] = "blocked"
        result = _result(low=1)
        result.update(status="fail", checks_failed=["native-backend-unavailable"])

        validator._validate_review_decision(metadata, result)

        assert result["status"] == "fail"

    @staticmethod
    def _approval_with(readiness: str) -> tuple[dict, dict]:
        """Build an `accept-as-is` judgment with the given readiness and a failed `types` gate."""
        metadata = _new_metadata()
        metadata["review_decision"]["recommendation"] = "accept-as-is"
        metadata["review_validation"].update(
            source_verdict="accept-as-is", verification_readiness=readiness, candidates=[]
        )
        metadata["review_findings"] = []
        result = _result()
        result.update(status="fail", checks_failed=["types"])
        return metadata, result

    def test_blocked_verification_keeps_source_approval_separate(self, validator: ModuleType):
        """A completed source approval is published without approving blocked verification or failed gates.

        The approval is accepted while the result keeps its `fail` status and failed `types` gate.
        """
        metadata, result = self._approval_with("blocked")

        validator._validate_review_decision(metadata, result)

        assert (result["status"], result["checks_failed"]) == ("fail", ["types"])

    def test_incomplete_inspection_rejects_approval_with_failed_gates(self, validator: ModuleType):
        """A source approval with incomplete inspection and failed gates is refused.

        Unlike blocked verification, incomplete inspection cannot back an approval, so the combination with a failed
        gate must be rejected.
        """
        metadata, result = self._approval_with("incomplete")

        with pytest.raises(SystemExit, match="review-approval-with-failed-gates"):
            validator._validate_review_decision(metadata, result)

    @pytest.mark.parametrize(
        ("verdict", "rating"),
        [
            pytest.param("insufficient-but-promising", 4, id="promising-incomplete-source"),
            pytest.param("reject", 5, id="documented-binding-conflict"),
        ],
    )
    def test_current_source_stages_survive_decision_renderer_and_discovery(
        self, validator: ModuleType, finalizer: Any, finder: ModuleType, tmp_path: Path, verdict: str, rating: int
    ):
        """Source implementation judgment stays separate from historical evidence insufficiency end to end.

        One current-family verdict must pass the decision gate, render with the current legend, and still be discovered
        as an assessed review result.
        """
        metadata = _new_metadata()
        metadata["review_decision"]["recommendation"] = verdict
        metadata["review_validation"]["source_verdict"] = verdict
        validator._validate_review_decision(metadata, _result(low=1))
        table = {
            "heading": "Review Snapshot",
            "columns": ["Field", "Value"],
            "rows": [],
            "source_rating_version": 1,
            "verification_readiness": "complete",
            "reviewers": [{"role": "Main reviewer", "rating": rating, "evidence": "review-notes.md"}],
        }
        metadata["scope"] = "pr"
        result = tmp_path / "result.json"
        result.write_text(json.dumps({"metadata": metadata}), encoding="utf-8")

        rendered = "\n".join(finalizer._render_table(table))

        assert f"Main reviewer ({rating})" in rendered
        assert RECOMMENDATION_LEGEND in rendered
        assert finder.review_result_kind(result) == "assessed"


class TestReviewerAssessment:
    """Retained reviewer responses: rating family, verification readiness and nullable source judgments."""

    def test_null_rating_is_a_returned_assessment(self, validator: ModuleType, tmp_path: Path):
        """A returned null rating is read as a completed undecidable response, not as a missing one.

        The response says `Rating: null` with blocked readiness; binding that to a real response stops a fabricated
        coverage result from standing in for it.
        """
        response = tmp_path / "reviewer.md"
        response.write_text(
            _assessment(
                "Reviewer Assessment",
                "null",
                "Native platform unavailable; platform owner must run the retained check.",
                readiness="blocked",
            ),
            encoding="utf-8",
        )

        rating = validator._retained_reviewer_rating(
            response, local_reviewer_wave=False, main=False, role="qa-specialist", source_rating_version=1
        )

        assert rating is None

    def test_missing_response_is_not_a_null_rating(self, validator: ModuleType, tmp_path: Path):
        """A reviewer response file that does not exist is rejected instead of read as a null rating.

        Only an actual completed response can carry a null judgment.
        """
        with pytest.raises(SystemExit, match="review-assessment-content-invalid"):
            validator._retained_reviewer_rating(
                tmp_path / "reviewer.md",
                local_reviewer_wave=False,
                main=False,
                role="qa-specialist",
                source_rating_version=1,
            )

    @pytest.fixture
    def historical_four(self, tmp_path: Path) -> Path:
        """Write an unversioned reviewer response carrying the historical raw rating four."""
        response = tmp_path / "reviewer.md"
        response.write_text(
            _assessment("Reviewer Assessment", 4, "Insufficient evidence.", version=None), encoding="utf-8"
        )
        return response

    def test_historical_four_is_read_without_a_version(self, validator: ModuleType, historical_four: Path):
        """An unversioned response keeps its raw historical rating of four.

        Reading the old family needs no version declaration, so old reviews stay readable.
        """
        rating = validator._retained_reviewer_rating(
            historical_four, local_reviewer_wave=False, main=False, role="qa-specialist"
        )

        assert rating == 4

    def test_historical_four_cannot_be_relabelled_as_source_reject(self, validator: ModuleType, historical_four: Path):
        """The versioned five-stage legend applies only to a response that declares its own family.

        Reading the unversioned response as source rating version 1 would silently turn insufficient evidence into a
        source judgment.
        """
        with pytest.raises(SystemExit, match="review-assessment-rating-version-mismatch"):
            validator._retained_reviewer_rating(
                historical_four, local_reviewer_wave=False, main=False, role="qa-specialist", source_rating_version=1
            )

    class DirectParent(NamedTuple):
        """A direct-parent Doc scribe review: directory, metadata, pass table and response file."""

        directory: Path
        metadata: dict
        passes: dict
        response: Path

    @pytest.fixture
    def direct_parent(self, tmp_path: Path) -> DirectParent:
        """Create a direct-parent inspection response whose own readiness is `incomplete`."""
        metadata = _new_metadata()
        metadata["reviewer_assessments"] = [{"role": "Doc scribe", "rating": 2, "evidence": "doc.md"}]
        response = tmp_path / "doc.md"
        response.write_text(
            _assessment(
                "Reviewer Assessment",
                2,
                "Parent must execute examples and build docs.",
                readiness="incomplete",
            ),
            encoding="utf-8",
        )
        passes = {"doc-scribe": {"mode": "inspection", "output_path": "doc.md"}}
        return self.DirectParent(tmp_path, metadata, passes, response)

    @pytest.mark.parametrize("readiness", ["complete", "blocked", "incomplete"])
    def test_direct_parent_can_close_checks_for_inspected_source(
        self, validator: ModuleType, direct_parent: DirectParent, readiness: str
    ):
        """Any aggregate readiness is admissible when the direct parent inspected its source.

        Blocked or incomplete checks remain admissible, and validation never rewrites the retained response bytes.
        """
        directory, metadata, passes, response = direct_parent
        metadata["review_validation"]["verification_readiness"] = readiness
        original = response.read_bytes()

        validator._validate_reviewer_assessments(directory, metadata, passes, direct_coverage=REVIEWED_DOCS)

        assert response.read_bytes() == original

    @pytest.mark.parametrize("readiness", ["complete", "blocked", "incomplete"])
    def test_direct_parent_cannot_close_missing_source(
        self, validator: ModuleType, direct_parent: DirectParent, readiness: str
    ):
        """No readiness label completes source the direct parent never inspected.

        Coverage lists `docs.md` as missing, so even a `complete` aggregate must be refused.
        """
        directory, metadata, passes, _ = direct_parent
        metadata["review_validation"]["verification_readiness"] = readiness

        with pytest.raises(SystemExit, match="review-direct-missing-source-cannot-complete"):
            validator._validate_reviewer_assessments(directory, metadata, passes, direct_coverage=UNINSPECTED_DOCS)

    def test_absent_independence_flag_keeps_prepared_default(self, validator: ModuleType, tmp_path: Path):
        """Final validation uses the same absent-false routing default as direct preparation.

        With no independence flag in the evidence or the result, the check reports neither required nor satisfied.
        """
        evidence = validator._SpecialistEvidence(
            triggered_roles=set(),
            manifest={"schema_version": 10},
            routing={},
            passes=[],
            by_role={},
            runtime_summary={
                "independence_required": False,
                "independence_requirement_evidence": None,
                "independence_satisfied": False,
            },
        )
        environment = validator._ReviewEnvironment(
            codex_home=tmp_path, parent_thread_id="parent", project_root=tmp_path
        )

        outcome = validator._validate_inspection_independence(
            tmp_path, evidence, {"independence_requirement_evidence": None}, "fail", environment
        )

        assert outcome == (False, False)

    @staticmethod
    def _text_review(directory: Path, readiness: str) -> tuple[dict, dict]:
        """Write a native text review note; return its metadata and an empty pass table."""
        metadata = _new_metadata()
        metadata["reviewer_assessments"] = [{"role": "Main reviewer", "rating": 2, "evidence": "review-notes.md"}]
        (directory / "review-notes.md").write_text(
            _assessment("Main Reviewer Assessment", 2, "Verification remains open.", readiness=readiness),
            encoding="utf-8",
        )
        return metadata, {}

    @staticmethod
    def _structured_review(directory: Path, readiness: str) -> tuple[dict, dict]:
        """Write a structured app-server review; return its metadata and pass table."""
        metadata = _new_metadata()
        (directory / "qa.json").write_text(
            json.dumps(
                {
                    "assessment": {
                        "source_rating_version": 1,
                        "rating": 2,
                        "verification_readiness": readiness,
                        "rationale": "Verification remains open.",
                    }
                }
            ),
            encoding="utf-8",
        )
        metadata["reviewer_assessments"] = [{"role": "QA specialist", "rating": 2, "evidence": "qa.json"}]
        return metadata, {"qa-specialist": {"mode": "app-server", "output_path": "qa.json"}}

    @pytest.mark.parametrize("readiness", ["blocked", "incomplete"])
    @pytest.mark.parametrize(
        "build_review", [pytest.param(_text_review, id="text"), pytest.param(_structured_review, id="structured")]
    )
    def test_open_readiness_blocks_complete_aggregate(
        self, validator: ModuleType, tmp_path: Path, build_review: Callable, readiness: str
    ):
        """Neither native text nor structured reviews can silently lose unfinished verification.

        The retained review says verification is `blocked` or `incomplete` while the aggregate claims `complete`.
        """
        metadata, passes = build_review(tmp_path, readiness)

        with pytest.raises(SystemExit, match="review-assessment-readiness-mismatch"):
            validator._validate_reviewer_assessments(tmp_path, metadata, passes)

    @pytest.mark.parametrize("readiness", ["blocked", "incomplete"])
    @pytest.mark.parametrize(
        "build_review", [pytest.param(_text_review, id="text"), pytest.param(_structured_review, id="structured")]
    )
    def test_aggregate_recording_open_readiness_is_accepted(
        self, validator: ModuleType, tmp_path: Path, build_review: Callable, readiness: str
    ):
        """The same review is accepted once the aggregate records the retained readiness.

        This is the control for the rejection above: only the aggregate's readiness differs.
        """
        metadata, passes = build_review(tmp_path, readiness)
        metadata["review_validation"]["verification_readiness"] = readiness

        validator._validate_reviewer_assessments(tmp_path, metadata, passes)

    @pytest.mark.parametrize("stage", ["source-001", "interactions", "consolidation-001"])
    @pytest.mark.parametrize(
        ("response", "failure"),
        [
            pytest.param(
                _assessment("Reviewer Assessment", 1, "Native check unavailable.", readiness="blocked"),
                "review-assessment-readiness-mismatch",
                id="blocked",
            ),
            pytest.param(
                _assessment("Reviewer Assessment", 1, "Historical response.", version=None),
                "review-assessment-rating-version-mismatch",
                id="unversioned",
            ),
        ],
    )
    def test_every_batch_constituent_binds_family_and_readiness(
        self, validator: ModuleType, tmp_path: Path, stage: str, response: str, failure: str
    ):
        """Final selection cannot hide an earlier incompatible or verification-blocked batch response.

        A complete main assessment sits beside one batch wave (source, interactions or consolidation) whose response is
        blocked or lacks the rating family.
        """
        metadata = _new_metadata()
        metadata["reviewer_assessments"] = [{"role": "Main reviewer", "rating": 2, "evidence": "review-notes.md"}]
        (tmp_path / "review-notes.md").write_text(
            _assessment("Main Reviewer Assessment", 2, "Main source assessment.", readiness="complete"),
            encoding="utf-8",
        )
        wave = tmp_path / "batches" / stage
        wave.mkdir(parents=True)
        (wave / "qa.md").write_text(response, encoding="utf-8")
        (wave / "specialist-manifest.json").write_text(
            json.dumps({"passes": [{"role": "qa-specialist", "output_path": "qa.md"}]}), encoding="utf-8"
        )
        (tmp_path / "specialist-manifest.json").write_text(
            json.dumps(
                {"batch_execution": {"waves": [{"manifest_path": f"batches/{stage}/specialist-manifest.json"}]}}
            ),
            encoding="utf-8",
        )

        with pytest.raises(SystemExit, match=failure):
            validator._validate_reviewer_assessments(tmp_path, metadata, {}, batch_response=True)

    def test_optional_reviewer_recipe_requests_current_assessment_family(self):
        """The optional reviewer recipe asks for the helper's current JSON assessment schema.

        The recipe is executable instructions; if it drifts from the helper, reviewers return responses the validator
        rejects.
        """
        recipe = (PLUGIN_ROOT / "skills/code-review/local-reviewer-wave.md").read_text(encoding="utf-8")

        assert not _missing(
            recipe,
            '`consumer_id="code-review"`, `source_rating_version=1`',
            "`assessment` has exactly `source_rating_version=1`, `rating`",
            "`verification_readiness` (`complete|blocked|incomplete`)",
            "Legend: 1 = Approve · 2 = Minor changes · 3 = Major revision · 4 = Insufficient, but a good initiative · 5 = Block.",
        )

    @staticmethod
    def _write_constituents(root: Path, ratings: list[int | None]) -> list[Path]:
        """Write one batch part per rating (``None`` = undecidable, blocked) and return their directories."""
        directories = []
        for index, rating in enumerate(ratings):
            directory = root / f"part-{index}"
            directory.mkdir()
            response = (
                "## Reviewer Findings\n```json\n[]\n```\n"
                "## Reviewer Confidence\n```json\n"
                + json.dumps({"score": 0.95})
                + "\n```\n"
                + _assessment(
                    "Reviewer Assessment",
                    "null" if rating is None else rating,
                    "Scoped evidence.",
                    readiness="blocked" if rating is None else "complete",
                )
            )
            (directory / "qa.md").write_text(response, encoding="utf-8")
            manifest = {
                "passes": [{"role": "qa-specialist", "output_path": "qa.md", "attempts": [], "confidence": 0.95}]
            }
            (directory / "specialist-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            directories.append(directory)
        return directories

    @pytest.mark.parametrize(
        ("known_rating", "expected"),
        [pytest.param(3, 3, id="known-fixable-defect"), pytest.param(1, None, id="unknown-prevents-approval")],
    )
    def test_batch_aggregation_preserves_nullable_scope(
        self, validator: ModuleType, tmp_path: Path, known_rating: int, expected: int | None
    ):
        """A completed undecidable batch part neither crashes aggregation nor silently becomes approval.

        Two parts, one with a null rating and blocked readiness and one with a known rating, are aggregated; the
        retained final rating is the known defect rating, or null when the known part would approve.
        """
        batches = importlib.import_module("review_batches")
        directories = self._write_constituents(tmp_path, [None, known_rating])

        passes = batches._role_passes(tmp_path, directories, [], multipart=True)

        parts = passes[0]["final_parts"]
        assert [part["rating"] for part in parts] == [None, known_rating]
        assert [part["verification_readiness"] for part in parts] == ["blocked", "complete"]
        retained = validator._retained_reviewer_rating(
            tmp_path / passes[0]["output_path"],
            local_reviewer_wave=False,
            main=False,
            role="qa-specialist",
            source_rating_version=1,
        )
        assert retained == expected

    @pytest.fixture
    def snapshot_table(self) -> dict:
        """Build a review snapshot table with one QA reviewer rated four."""
        return {
            "heading": "Review Snapshot",
            "columns": ["Field", "Value"],
            "rows": [],
            "reviewers": [{"role": "QA specialist", "rating": 4, "evidence": "qa.md"}],
        }

    def test_renderer_uses_historical_legend_without_family(self, finalizer: Any, snapshot_table: dict):
        """A table with no explicit rating family keeps the raw historical four label.

        The legend is chosen by the declared family, so old tables render exactly as before.
        """
        rendered = "\n".join(finalizer._render_table(snapshot_table))

        assert "4 = Insufficient evidence" in rendered

    def test_renderer_uses_current_legend_for_explicit_family(self, finalizer: Any, snapshot_table: dict):
        """An explicit rating family renders the promising-incomplete legend and drops the historical label.

        Declaring `source_rating_version=1` is what switches the legend, never the rating value alone.
        """
        snapshot_table.update(source_rating_version=1, verification_readiness="blocked")

        rendered = "\n".join(finalizer._render_table(snapshot_table))

        assert RECOMMENDATION_LEGEND in rendered
        assert "Insufficient evidence" not in rendered

    def test_renderer_labels_completed_null_assessment(self, finalizer: Any, snapshot_table: dict):
        """A completed null assessment renders as a reviewer without a source judgment.

        The null rating is a returned undecidable answer and must be visible, not silently dropped.
        """
        snapshot_table.update(source_rating_version=1, verification_readiness="blocked")
        snapshot_table["reviewers"][0]["rating"] = None

        rendered = "\n".join(finalizer._render_table(snapshot_table))

        assert "QA specialist (no source judgment)" in rendered


class TestRemediationIntake:
    """Open concerns and batch originals carried from a review result into remediation obligations."""

    def test_null_source_judgment_is_discoverable_as_returned_assessment(self, finder: ModuleType, tmp_path: Path):
        """Discovery classifies an explicit undecidable assessment as assessed, not as unavailable output.

        A PR result with a null recommendation and null source verdict is a returned answer the finder must keep.
        """
        path = tmp_path / "result.json"
        metadata = _new_metadata("unverified")
        metadata["scope"] = "pr"
        metadata["review_decision"]["recommendation"] = None
        metadata["review_validation"]["source_verdict"] = None
        path.write_text(json.dumps({"metadata": metadata}), encoding="utf-8")

        assert finder.review_result_kind(path) == "assessed"

    def test_open_concern_preserves_prerequisite_owner_and_next_check(self, shared_validator: Any):
        """Remediation receives an evidence obligation, never an implicit confirmed-defect request.

        An unverified candidate yields exactly one obligation whose source id points back at the candidate and whose
        body carries its prerequisite, owner, next check and closure condition.
        """
        metadata = _new_metadata("unverified")
        candidate = metadata["review_validation"]["candidates"][0]
        candidate["missing_prerequisite"] = "Native backend unavailable."
        metadata["confidence_recovery"] = {}

        sources = shared_validator.report_obligation_sources({"metadata": metadata}, "review/result.json")

        assert len(sources) == 1
        assert sources[0]["source_id"] == "review/result.json#/metadata/review_validation/candidates/0"
        assert _resolve_pointer({"metadata": metadata}, sources[0]["source_id"]) == candidate
        fields = ("id", "missing_prerequisite", "owner", "next_check", "closure_condition")
        assert all(candidate[field] in sources[0]["body"] for field in fields)
        assert OBLIGATION_NOTICE in sources[0]["body"]

    class OpenConcernHandoff(NamedTuple):
        """A review run whose only open item is a backend-unavailable concern."""

        run: Path
        result: dict
        handoff: dict
        concern: dict

    @pytest.fixture
    def open_concern_handoff(self, assessed_run: Path) -> OpenConcernHandoff:
        """Rewrite an assessed run to carry one unverified concern and a handoff listing it as remaining work."""
        result = json.loads((assessed_run / "result.json").read_text(encoding="utf-8"))
        handoff = json.loads((assessed_run / "final-handoff.json").read_text(encoding="utf-8"))
        concern = _candidate("unverified")
        concern["missing_prerequisite"] = "Native platform unavailable."
        metadata = result["metadata"]
        metadata["review_validation"].update(
            source_verdict=None, verification_readiness="blocked", candidates=[concern]
        )
        metadata["review_decision"].update(recommendation=None, summary="No source judgment; verification blocked.")
        metadata["reviewer_assessments"][0]["rating"] = None
        result["status"] = "fail"
        handoff["outcome"]["summary"] = "Recommendation: None."
        snapshot = handoff["tables"][0]
        snapshot.update(
            verification_readiness="blocked",
            reviewers=metadata["reviewer_assessments"],
            summary=metadata["review_decision"]["summary"],
        )
        snapshot["rows"][-1]["cells"][1] = "no source judgment"
        handoff["remaining"] = [
            {
                "row_id": concern["id"],
                "item": concern["claim"] + " " + concern["missing_prerequisite"],
                "owner": concern["owner"],
                "next_action": concern["next_check"] + " " + concern["closure_condition"],
            }
        ]
        handoff["next_steps"] = [concern["id"]]
        return self.OpenConcernHandoff(assessed_run, result, handoff, concern)

    def test_unverified_concern_survives_bound_final_handoff(
        self, shared_validator: Any, finalizer: Any, open_concern_handoff: OpenConcernHandoff
    ):
        """A backend uncertainty stays visible with exact ownership and closure, outside defect actions.

        The handoff validates and its rendering carries the concern's id, prerequisite, owner, next check and closure.
        """
        run, result, handoff, concern = open_concern_handoff

        shared_validator._validate_code_review_final_handoff(result, handoff, out_dir=run)
        rendered = finalizer.render_handoff(handoff, report_root=run)

        fields = ("id", "missing_prerequisite", "owner", "next_check", "closure_condition")
        assert all(concern[field] in rendered for field in fields)

    def test_handoff_dropping_open_concern_is_rejected(
        self, shared_validator: Any, open_concern_handoff: OpenConcernHandoff
    ):
        """A final handoff that no longer lists the open concern is refused.

        Clearing `remaining` hides the unresolved backend uncertainty, so the validator must name the missing concern.
        """
        run, result, handoff, _ = open_concern_handoff
        handoff["remaining"] = []

        with pytest.raises(SystemExit, match="code-review-final-handoff-open-concern-missing:candidate-a"):
            shared_validator._validate_code_review_final_handoff(result, handoff, out_dir=run)


class BatchCase(NamedTuple):
    """One disposition applied to the shared real batch run: its result and the records it was built from."""

    run: Path
    manifest: dict
    originals: list
    candidates: list
    result: dict


@pytest.fixture(scope="module")
def batch_run(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict]:
    """Build a real assembled batch review run (two admitted originals) shared by every batch-origin case.

    Building it replays recorded context-reader bytes and runs the real `assemble-batches` command; every test only
    reads it, so one module-scoped build replaces one per case.
    """
    run, home = _completed_batches(
        tmp_path_factory.mktemp("batch-origins"),
        blocker=(1, "challenger"),
        source_records=[_batch_finding("F_ONE", "high", "Preserve the caller contract.")],
        interaction_finding=(1, "qa-specialist", 3),
        interaction_records=[_batch_finding("F_TWO", "high", "Preserve the independent empty-input contract.")],
    )
    assembled = _batch_command(run, "assemble-batches", home)
    assert assembled.returncode == 0, assembled.stderr
    return run, json.loads((run / "specialist-manifest.json").read_bytes())


@pytest.fixture
def candidate_case(validator: ModuleType, batch_run: tuple[Path, dict]) -> Callable[[str | None], BatchCase]:
    """Return a factory building a result for one disposition over private copies of the batch run's records."""
    run, shared_manifest = batch_run

    def build(disposition: str | None) -> BatchCase:
        manifest = copy.deepcopy(shared_manifest)
        originals = [item for item in manifest["source_findings"] if item["disposition"] == "unresolved"]
        assert len(originals) == 2
        metadata = _new_metadata(disposition)
        metadata["review_findings"] = []
        metadata["source_findings"] = originals
        metadata["source_finding_mapping"] = {}
        revision = "sha256:" + hashlib.sha256((run / "batch-inventory.json").read_bytes()).hexdigest()
        candidates = []
        for index, origin in enumerate(originals):
            original = origin["original"]
            candidate = _candidate(disposition)
            candidate.update(
                id=f"candidate-{index}",
                severity=original["severity"],
                claim=original["summary"],
                closure_condition=original["closure_evidence"],
                source_revision=revision,
                feasible_checks_complete=disposition is not None,
                missing_prerequisite="Native backend unavailable." if disposition == "unverified" else "",
            )
            candidates.append(candidate)
            metadata["source_finding_mapping"][origin["finding_id"]] = [candidate["id"]]
            if disposition == "confirmed":
                metadata["review_findings"].append(
                    {
                        **original,
                        "id": candidate["id"],
                        "authors": [validator._readable_review_role(origin["pass"]["role"])],
                        "evidence": [origin["manifest_path"], "widget.py:1-1"],
                    }
                )
        readiness = {None: "incomplete", "unverified": "blocked"}.get(disposition, "complete")
        metadata["review_validation"].update(
            candidates=candidates, verification_readiness=readiness, source_verdict="needs-more-work"
        )
        metadata["review_decision"]["recommendation"] = "needs-more-work"
        result = {**_result(high=2 if disposition == "confirmed" else 0), "status": "fail", "metadata": metadata}
        return BatchCase(run, manifest, originals, candidates, result)

    return build


def _intake_mismatches(sources: list[dict], case: BatchCase) -> list[str]:
    """List how obligation sources fail to link each original to its candidate; empty means all are linked."""
    problems = []
    for index, (source, origin, candidate) in enumerate(zip(sources, case.originals, case.candidates, strict=False)):
        expected_id = f"review/result.json#/metadata/review_validation/candidates/{index}"
        checks = {
            "source_id": source["source_id"] == expected_id,
            "original": json.dumps(origin, sort_keys=True) in source["body"],
            "original_pointer": f"/metadata/source_findings/{index}" in source["body"],
            "candidate": json.dumps(candidate, sort_keys=True) in source["body"],
            "notice": OBLIGATION_NOTICE in source["body"],
        }
        problems.extend(f"{index}:{name}" for name, ok in checks.items() if not ok)
    return problems


def _reconciled(case: BatchCase) -> dict:
    """Copy the case result with both originals reconciled into the first candidate's single group."""
    result = copy.deepcopy(case.result)
    metadata = result["metadata"]
    first = case.candidates[0]
    metadata["source_finding_mapping"] = {origin["finding_id"]: [first["id"]] for origin in case.originals}
    metadata["review_validation"]["candidates"] = [first]
    metadata["source_finding_reconciliation"] = {
        first["id"]: {
            "invariant": "Both caller contracts must remain explicit.",
            "rationale": "One combined contract check covers both retained original obligations.",
            "origins": {
                origin["finding_id"]: "The combined check retains this caller contract." for origin in case.originals
            },
            "evidence": [case.originals[0]["original"]["evidence"][0].copy()],
        }
    }
    return result


def _reconciliation_group(case: BatchCase, result: dict) -> dict:
    """Return the reconciliation group of a reconciled result."""
    return result["metadata"]["source_finding_reconciliation"][case.candidates[0]["id"]]


def _drop_origin(case: BatchCase, result: dict) -> None:
    """Remove one original from the reconciliation group."""
    _reconciliation_group(case, result)["origins"].pop(case.originals[0]["finding_id"])


def _weaken_severity(case: BatchCase, result: dict) -> None:
    """Lower the reconciled candidate below its originals' severity."""
    result["metadata"]["review_validation"]["candidates"][0]["severity"] = "low"


def _forge_evidence_hash(case: BatchCase, result: dict) -> None:
    """Point the reconciliation evidence at a source hash that does not exist."""
    _reconciliation_group(case, result)["evidence"][0]["source_sha256"] = "0" * 64


def _duplicated(case: BatchCase) -> tuple[dict, dict]:
    """Return a result and manifest where the second original exactly duplicates the first."""
    manifest = copy.deepcopy(case.manifest)
    manifest["source_findings"][1]["original"] = copy.deepcopy(case.originals[0]["original"])
    result = _reconciled(case)
    result["metadata"].pop("source_finding_reconciliation")
    result["metadata"]["source_findings"] = manifest["source_findings"]
    first, second = sorted(origin["finding_id"] for origin in case.originals)
    result["metadata"]["source_finding_duplicates"] = {second: first}
    return result, manifest


def _obligation_rows(open_count: int, *, include_confirmed: bool) -> list:
    """Parametrize (disposition, expected obligations): closed dispositions create none, open ones ``open_count``."""
    closed = CLOSED_DISPOSITIONS if include_confirmed else CLOSED_DISPOSITIONS[1:]
    return [
        *(pytest.param(disposition, 0, id=disposition) for disposition in closed),
        pytest.param(None, open_count, id="pending"),
        pytest.param("unverified", open_count, id="unverified"),
    ]


@pytest.mark.integration
class TestBatchCandidateOrigins:
    """Real admitted batch originals stay linked through every parent disposition and ordinary obligation intake."""

    @pytest.mark.parametrize(("disposition", "obligations"), _obligation_rows(2, include_confirmed=True))
    def test_retained_originals_reach_remediation_intake(
        self, validator: ModuleType, shared_validator: Any, candidate_case: Callable, disposition, obligations: int
    ):
        """Each disposition validates, and only open concerns become obligations carrying their originals.

        Pending and unverified candidates yield one obligation per original linking original and candidate; confirmed,
        refuted, already-fixed and out-of-scope candidates create none.
        """
        case = candidate_case(disposition)
        metadata = case.result["metadata"]

        validator._validate_review_decision(metadata, case.result)
        validator._validate_candidate_source(case.run, metadata)
        validator._validate_batch_source_findings(case.run, case.result, case.manifest)
        sources = shared_validator.report_obligation_sources(case.result, "review/result.json")

        assert len(sources) == obligations
        assert _intake_mismatches(sources, case) == []

    @pytest.mark.parametrize("disposition", NON_CONFIRMED)
    @pytest.mark.parametrize(
        ("field", "wrong"),
        [
            pytest.param("claim", "Unrelated spelling concern.", id="claim"),
            pytest.param("severity", "low", id="severity"),
            pytest.param("closure_condition", "Unrelated closure.", id="closure-condition"),
        ],
    )
    def test_rejects_candidate_diverging_from_its_origin(
        self, validator: ModuleType, candidate_case: Callable, disposition, field: str, wrong: str
    ):
        """A candidate whose claim, severity or closure no longer matches its original is refused.

        The inventory of originals must stay recoverable from the candidates, so an unrelated rewrite is dropped
        evidence.
        """
        case = candidate_case(disposition)
        case.result["metadata"]["review_validation"]["candidates"][0][field] = wrong

        with pytest.raises(SystemExit, match="review-batch-result-source-finding-inventory-dropped"):
            validator._validate_batch_source_findings(case.run, case.result, case.manifest)

    @pytest.mark.parametrize("disposition", NON_CONFIRMED)
    def test_rejects_distinct_origins_collapsed_into_one_candidate(
        self, validator: ModuleType, candidate_case: Callable, disposition
    ):
        """Two distinct originals cannot be mapped onto one candidate without a reconciliation.

        Pointing the second original at the first candidate silently merges different concerns.
        """
        case = candidate_case(disposition)
        mapping = case.result["metadata"]["source_finding_mapping"]
        mapping[case.originals[1]["finding_id"]] = [case.candidates[0]["id"]]

        with pytest.raises(
            SystemExit, match=r"review-batch-(result-source-finding-inventory-dropped|distinct-findings-collapsed)"
        ):
            validator._validate_batch_source_findings(case.run, case.result, case.manifest)

    @pytest.mark.parametrize(("disposition", "obligations"), _obligation_rows(1, include_confirmed=False))
    def test_reconciled_group_retains_all_originals(
        self, validator: ModuleType, shared_validator: Any, candidate_case: Callable, disposition, obligations: int
    ):
        """A reconciliation of both originals is accepted and one open obligation carries every original.

        Open dispositions yield a single obligation whose body holds both originals; closed ones yield none.
        """
        case = candidate_case(disposition)
        reconciled = _reconciled(case)

        validator._validate_batch_source_findings(case.run, reconciled, case.manifest)
        sources = shared_validator.report_obligation_sources(reconciled, "review/result.json")

        assert len(sources) == obligations
        assert all(
            json.dumps(origin, sort_keys=True) in source["body"] for source in sources for origin in case.originals
        )

    @pytest.mark.parametrize("disposition", NON_CONFIRMED)
    @pytest.mark.parametrize(
        "damage",
        [
            pytest.param(_drop_origin, id="missing-origin"),
            pytest.param(_weaken_severity, id="weak-severity"),
            pytest.param(_forge_evidence_hash, id="wrong-source"),
        ],
    )
    def test_rejects_damaged_reconciliation(self, validator: ModuleType, candidate_case: Callable, disposition, damage):
        """A reconciliation that loses an origin, weakens severity or cites a wrong source is refused.

        Each mutator damages one part of an otherwise valid group, which must never reduce the retained originals.
        """
        case = candidate_case(disposition)
        broken = _reconciled(case)
        damage(case, broken)

        with pytest.raises(SystemExit, match="review-batch-source-reconciliation-invalid"):
            validator._validate_batch_source_findings(case.run, broken, case.manifest)

    @pytest.mark.parametrize("disposition", NON_CONFIRMED)
    def test_accepts_exact_duplicate_accounting(self, validator: ModuleType, candidate_case: Callable, disposition):
        """An original that exactly repeats another is accepted when the duplicate is accounted for.

        The result lists the second original as a duplicate of the first, which is the sanctioned way to drop it.
        """
        case = candidate_case(disposition)
        duplicated, manifest = _duplicated(case)

        validator._validate_batch_source_findings(case.run, duplicated, manifest)

    @pytest.mark.parametrize("disposition", NON_CONFIRMED)
    def test_rejects_unaccounted_exact_duplicate(self, validator: ModuleType, candidate_case: Callable, disposition):
        """An exact duplicate original with no duplicate accounting is refused.

        Emptying the duplicate map leaves a dropped original unexplained.
        """
        case = candidate_case(disposition)
        duplicated, manifest = _duplicated(case)
        duplicated["metadata"]["source_finding_duplicates"] = {}

        with pytest.raises(SystemExit, match="review-batch-duplicate-accounting-mismatch"):
            validator._validate_batch_source_findings(case.run, duplicated, manifest)


class TestLocalMinorNotes:
    """The notes-table evidence gate for local minor-change reviews."""

    @pytest.mark.parametrize("version", [1, 2])
    @pytest.mark.parametrize(
        "damage",
        [
            pytest.param(lambda body: "## Source assessment\nMinor changes required.\n", id="missing-table"),
            pytest.param(lambda body: body.replace("guide.md:1", "unrelated.md:99"), id="altered-evidence"),
            pytest.param(lambda body: body.replace("candidate-a", "unrelated"), id="missing-action"),
        ],
    )
    def test_require_canonical_actions(
        self, validator: ModuleType, tmp_path: Path, version: int, damage: Callable[[str], str]
    ):
        """Current and historical record markers apply the same notes-table evidence gate.

        Notes with no findings table, altered evidence or a renamed action no longer match the canonical finding.
        """
        metadata = _new_metadata()
        metadata["finding_records_version"] = version
        notes = tmp_path / "review-notes.md"
        notes.write_text(
            damage(
                "## Review Findings and Merge Blocks\n\n"
                "| Finding / area | Required change | Evidence | Status |\n"
                "| --- | --- | --- | --- |\n"
                "| candidate-a | Correct the option. | guide.md:1 | Required |\n"
            ),
            encoding="utf-8",
        )

        with pytest.raises(SystemExit, match=r"review-.*findings-action-table"):
            validator._validate_action_table(notes, {**_result(low=1), "schema_version": 3}, metadata, "working-tree")


class TestOrdinaryCompletion:
    """Completion checks run by the real report finder over an assessed PR run."""

    def test_old_consumer_rejects_new_records_version(self, assessed_run: Path, tmp_path: Path):
        """The semantic cutover fails at the old records gate, not on unrelated missing provenance.

        The current finder completes the run; an archived consumer whose finding reader is swapped for the legacy one
        then rejects the same run for its records version.
        """
        current = _complete_run(FINDER, assessed_run)
        assert current.returncode == 0, current.stdout + current.stderr
        archived = _archive_with_legacy_reader(tmp_path / "old-consumer")

        old = _complete_run(archived / "shared/find-review-report.py", assessed_run)

        assert old.returncode == 1
        assert (
            f"review-validation-failed:{assessed_run / 'result.json'}:review-finding-records-version-invalid"
            in old.stderr
        )

    def test_dropped_reviewer_readiness_fails_completion(self, assessed_run: Path):
        """Passing process gates cannot override a retained reviewer's blocked verification.

        The run completes, then the review notes are changed from `complete` to `blocked` readiness and the same
        completion check must refuse it.
        """
        good = _complete_run(FINDER, assessed_run)
        assert good.returncode == 0, good.stdout + good.stderr
        notes = assessed_run / "review-notes.md"
        notes.write_text(
            notes.read_text(encoding="utf-8").replace(
                "Verification readiness: complete", "Verification readiness: blocked"
            ),
            encoding="utf-8",
        )

        blocked = _complete_run(FINDER, assessed_run)

        assert blocked.returncode == 1
        assert (
            f"review-validation-failed:{assessed_run / 'result.json'}:review-assessment-readiness-mismatch"
            in blocked.stderr
        )

    def test_unrelated_source_revision_is_rejected(self, validator: ModuleType, assessed_run: Path):
        """An unrelated revision string cannot certify a candidate against this run's source.

        The default `frozen-source` revision matches no retained head.
        """
        with pytest.raises(SystemExit, match="review-candidate-source-revision-mismatch"):
            validator._validate_candidate_source(assessed_run, _new_metadata())

    def test_candidate_revision_bound_to_retained_head_is_accepted(self, validator: ModuleType, assessed_run: Path):
        """A candidate whose revision is `head:` plus the run's retained head commit is accepted.

        This is the control for the rejection above: only the revision differs.
        """
        metadata = _new_metadata()
        routing = json.loads((assessed_run / "pr-routing.json").read_text(encoding="utf-8"))
        metadata["review_validation"]["candidates"][0]["source_revision"] = "head:" + routing["head_oid"]

        validator._validate_candidate_source(assessed_run, metadata)
