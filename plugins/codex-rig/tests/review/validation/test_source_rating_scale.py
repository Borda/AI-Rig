"""Unified source verdict scale: version 2 tokens, the version 1 historical reader, and refusal of mixed vocabularies.

`review_validation.source_rating_version` 2 writes the shared `source-5-v2` tokens (`APPROVE` to `REJECT`); version 1
artifacts keep their lowercase names. Every gate judges the level, never the spelling, so each version 1 name and its
version 2 token must reach the same decision, render the same text, and be discovered the same way.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from _modules import load_module
from test_candidate_validation_contract import _candidate, _new_metadata
from test_code_review_decision_validation import _metadata, _result
from test_review_completion_gate import FINDER, PLUGIN_ROOT, _module

#: Level-ordered (version 1 name, version 2 token) pairs.
SCALE = (
    ("accept-as-is", "APPROVE"),
    ("minor-changes", "MINOR_EDITS"),
    ("needs-more-work", "MAJOR_REVISION"),
    ("insufficient-but-promising", "INSUFFICIENT_BUT_PROMISING"),
    ("reject", "REJECT"),
)
V1_NAMES = tuple(name for name, _ in SCALE)
V2_TOKENS = tuple(token for _, token in SCALE)
#: Every (version, spelling) combination one validated artifact may carry.
VERSIONED = [pytest.param(1, name, id=f"v1-{name}") for name in V1_NAMES] + [
    pytest.param(2, token, id=f"v2-{token}") for token in V2_TOKENS
]
#: Version and spelling pairs that mix the two vocabularies.
MIXED = [pytest.param(2, name, id=f"v2-with-{name}") for name in V1_NAMES] + [
    pytest.param(1, token, id=f"v1-with-{token}") for token in V2_TOKENS
]
#: One spelling per version for the same level, so a parity test runs on both.
PAIRED = [pytest.param(1, 0, id="v1"), pytest.param(2, 1, id="v2")]


@pytest.fixture(scope="module")
def validator() -> ModuleType:
    """Load the standalone review validator once; its checks keep no state between calls."""
    return load_module("scale_review_validator", PLUGIN_ROOT / "skills" / "code-review" / "validate_artifacts.py")


@pytest.fixture(scope="module")
def finalizer() -> Any:
    """Load the standalone handoff finalizer once."""
    return load_module("scale_final_handoff", PLUGIN_ROOT / "shared" / "final_handoff.py")


@pytest.fixture(scope="module")
def finder() -> ModuleType:
    """Load the review report finder module once."""
    return _module(FINDER)


def _scaled(version: int, verdict: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return metadata and a passing result whose shape every verdict level accepts.

    An approval carries no candidates or findings; every other level carries one confirmed low finding.

    Example:
        >>> _scaled(2, "APPROVE")[0]["review_validation"]["source_rating_version"]
        2
    """
    metadata = _new_metadata()
    metadata["review_decision"]["recommendation"] = verdict
    metadata["review_validation"].update(source_rating_version=version, source_verdict=verdict)
    if verdict in {"accept-as-is", "APPROVE"}:
        metadata["review_validation"]["candidates"] = []
        metadata["review_findings"] = []
        return metadata, _result()
    return metadata, _result(low=1)


class TestVersionedVocabulary:
    """Each source rating version admits exactly its own five spellings plus null."""

    @pytest.mark.parametrize(("version", "verdict"), VERSIONED)
    def test_accepts_own_vocabulary(self, validator: ModuleType, version: int, verdict: str) -> None:
        """Version 2 accepts each unified token and version 1 still accepts each historical name."""
        metadata, result = _scaled(version, verdict)

        validator._validate_review_decision(metadata, result)

    @pytest.mark.parametrize(("version", "verdict"), MIXED)
    def test_refuses_mixed_vocabulary(self, validator: ModuleType, version: int, verdict: str) -> None:
        """A valid spelling from the other version is refused with a reason naming the version mismatch."""
        metadata, result = _scaled(version, verdict)

        with pytest.raises(SystemExit, match=f"review-source-verdict-version-mismatch:{verdict}"):
            validator._validate_review_decision(metadata, result)

    @pytest.mark.parametrize("token", V2_TOKENS)
    def test_unified_token_requires_source_validation(self, validator: ModuleType, token: str) -> None:
        """Without `review_validation` only the historical names are readable, so a unified token is refused."""
        with pytest.raises(SystemExit, match="invalid-review-recommendation"):
            validator._validate_review_decision(_metadata(token), _result(low=1))

    @pytest.mark.parametrize(
        "version",
        [pytest.param(3, id="next-unreleased"), pytest.param(True, id="boolean"), pytest.param("2", id="string")],
    )
    def test_refuses_unknown_version(self, validator: ModuleType, version: object) -> None:
        """Only the integers 1 and 2 name a source rating version."""
        metadata, result = _scaled(2, "MINOR_EDITS")
        metadata["review_validation"]["source_rating_version"] = version

        with pytest.raises(SystemExit, match="review-validation-version-invalid"):
            validator._validate_review_decision(metadata, result)

    @pytest.mark.parametrize("version", [1, 2])
    def test_null_judgment_unchanged(self, validator: ModuleType, version: int) -> None:
        """An explicit null judgment stays valid in both versions while verification remains blocked."""
        metadata, result = _scaled(version, None)
        candidate = _candidate("unverified")
        candidate["missing_prerequisite"] = "Native backend unavailable."
        metadata["review_validation"].update(verification_readiness="blocked", candidates=[candidate])
        metadata["review_findings"] = []
        result = _result(status="fail", checks_failed=["native-backend-unavailable"])

        validator._validate_review_decision(metadata, result)


class TestGateParity:
    """A version 1 name and its version 2 token reach the same gate decision."""

    @pytest.mark.parametrize(("version", "spelling"), PAIRED)
    def test_approval_with_findings(self, validator: ModuleType, version: int, spelling: int) -> None:
        """Approval with a confirmed finding is refused in both versions."""
        metadata, result = _scaled(version, SCALE[0][spelling])
        metadata["review_validation"]["candidates"] = [_candidate()]
        metadata["review_findings"] = copy.deepcopy(_new_metadata()["review_findings"])
        result = _result(low=1)

        with pytest.raises(SystemExit, match="review-accept-with-findings"):
            validator._validate_review_decision(metadata, result)

    @pytest.mark.parametrize(("version", "spelling"), PAIRED)
    def test_minor_with_blocking_finding(self, validator: ModuleType, version: int, spelling: int) -> None:
        """Minor changes with a confirmed high finding are refused in both versions."""
        metadata, _ = _scaled(version, SCALE[1][spelling])
        metadata["review_validation"]["candidates"][0]["severity"] = "high"
        metadata["review_findings"][0]["severity"] = "high"

        with pytest.raises(SystemExit, match="review-minor-with-blocking-findings"):
            validator._validate_review_decision(metadata, _result(high=1))

    @pytest.mark.parametrize(("version", "spelling"), PAIRED)
    def test_approval_with_failed_gates_and_incomplete_inspection(
        self, validator: ModuleType, version: int, spelling: int
    ) -> None:
        """Approval with incomplete inspection and a failed gate is refused in both versions."""
        metadata, _ = _scaled(version, SCALE[0][spelling])
        metadata["review_validation"]["verification_readiness"] = "incomplete"

        with pytest.raises(SystemExit, match="review-approval-with-failed-gates"):
            validator._validate_review_decision(metadata, _result(status="fail", checks_failed=["types"]))

    @pytest.mark.parametrize(("version", "spelling"), PAIRED)
    def test_approval_with_open_concern(self, validator: ModuleType, version: int, spelling: int) -> None:
        """Approval while a feasible check is still pending is refused in both versions."""
        metadata, _ = _scaled(version, SCALE[0][spelling])
        candidate = _candidate(None)
        candidate["feasible_checks_complete"] = False
        metadata["review_validation"].update(verification_readiness="incomplete", candidates=[candidate])

        with pytest.raises(SystemExit, match="review-unconditional-approval-with-open-concerns"):
            validator._validate_review_decision(metadata, _result(status="fail", checks_failed=["types"]))

    @pytest.mark.parametrize(("version", "spelling"), PAIRED)
    def test_non_pr_major_revision_requires_action_table(
        self, validator: ModuleType, tmp_path: Path, version: int, spelling: int
    ) -> None:
        """A non-PR major revision needs its action table in both versions; minor changes do not."""
        notes = tmp_path / "review-notes.md"
        notes.write_text("# Review\n", encoding="utf-8")

        validator._validate_action_table(notes, _result(), _metadata(SCALE[1][spelling]), "working-tree")
        with pytest.raises(SystemExit, match="review-missing-findings-action-table"):
            validator._validate_action_table(notes, _result(), _metadata(SCALE[2][spelling]), "working-tree")

    @pytest.mark.parametrize(("name", "token"), SCALE)
    def test_canonical_level(self, validator: ModuleType, name: str, token: str) -> None:
        """Both spellings normalize to the version 2 token, so every gate compares one level."""
        assert validator._canonical_recommendation(name) == validator._canonical_recommendation(token) == token


class TestReaders:
    """Renderers and report discovery give one answer per level whatever the version."""

    @pytest.mark.parametrize(("name", "token"), SCALE)
    def test_plain_summary_matches(self, finalizer: Any, name: str, token: str) -> None:
        """The plain-English decision summary is identical for a version 1 name and its version 2 token."""
        assert finalizer.REVIEW_PLAIN_SUMMARIES[token] == finalizer.REVIEW_PLAIN_SUMMARIES[name]

    @pytest.mark.parametrize("version", [1, 2])
    def test_snapshot_uses_source_legend(self, finalizer: Any, version: int) -> None:
        """Both versions select the five-level source legend and print verification readiness."""
        table = {
            "heading": "Review Snapshot",
            "columns": ["Field", "Value"],
            "rows": [],
            "source_rating_version": version,
            "verification_readiness": "blocked",
            "reviewers": [{"role": "Main reviewer", "rating": 3, "evidence": "review-notes.md"}],
        }

        rendered = "\n".join(finalizer._render_table(table))

        assert finalizer.SOURCE_REVIEWER_LEGEND in rendered
        assert "Verification readiness: blocked." in rendered

    @pytest.mark.parametrize(
        ("version", "recommendation"),
        [pytest.param(2, name, id=f"v2-with-{name}") for name in V1_NAMES]
        + [pytest.param(1, token, id=f"v1-with-{token}") for token in V2_TOKENS],
    )
    def test_handoff_refuses_mixed_outcome(self, finalizer: Any, version: int, recommendation: str) -> None:
        """A handoff outcome spelled for the other version than its snapshot is refused."""
        with pytest.raises(finalizer.HandoffError, match="review-outcome-source-rating-version-mismatch"):
            finalizer._require_source_vocabulary(
                f"Recommendation: {recommendation}.", [{"source_rating_version": version}]
            )

    @pytest.mark.parametrize(("version", "verdict"), VERSIONED)
    def test_handoff_accepts_matching_outcome(self, finalizer: Any, version: int, verdict: str) -> None:
        """A handoff outcome spelled for its snapshot version is accepted."""
        finalizer._require_source_vocabulary(f"Recommendation: {verdict}.", [{"source_rating_version": version}])

    @pytest.mark.parametrize(("version", "verdict"), VERSIONED)
    def test_discovered_as_assessed(self, finder: ModuleType, tmp_path: Path, version: int, verdict: str) -> None:
        """Report discovery classifies each versioned spelling as an assessed review."""
        metadata, _ = _scaled(version, verdict)
        metadata["scope"] = "pr"
        result = tmp_path / "result.json"
        result.write_text(json.dumps({"metadata": metadata}), encoding="utf-8")

        assert finder.review_result_kind(result) == "assessed"

    @pytest.mark.parametrize(("version", "verdict"), MIXED)
    def test_discovery_refuses_mixed(self, finder: ModuleType, tmp_path: Path, version: int, verdict: str) -> None:
        """Report discovery never classifies a mixed-vocabulary result as assessed."""
        metadata, _ = _scaled(version, verdict)
        metadata["scope"] = "pr"
        result = tmp_path / "result.json"
        result.write_text(json.dumps({"metadata": metadata}), encoding="utf-8")

        assert finder.review_result_kind(result) == "invalid"
