"""Unit checks for public-PR fallback confidence limits in code-review artifacts.

When the PR collector falls back to public HTTPS metadata, mergeability, reviews and similar evidence are missing. The
shipped validators must then require the matching confidence gap and cap confidence at 0.89, for both a review result
and a PR remediation result.
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

import pytest
from _modules import load_module

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
REVIEW_VALIDATOR_PATH = PLUGIN_ROOT / "skills" / "code-review" / "validate_artifacts.py"
SHARED_VALIDATOR_PATH = PLUGIN_ROOT / "shared" / "validate-artifacts.py"
UNAVAILABLE_EVIDENCE = ["github_provided_file_list", "mergeability", "review_decision", "reviews", "top_level_comments"]
FALLBACK_CONFIDENCE_GAP = (
    "Public HTTPS PR metadata fallback omitted evidence: "
    "github_provided_file_list, mergeability, review_decision, reviews, top_level_comments."
)


@pytest.fixture(name="validator")
def _validator() -> ModuleType:
    """Load the shipped standalone code-review validator."""
    return load_module("code_review_pr_fallback_validator", REVIEW_VALIDATOR_PATH)


@pytest.fixture(name="shared_validator")
def _shared_validator() -> ModuleType:
    """Load the shared validator used by PR remediation."""
    return load_module("shared_pr_fallback_validator", SHARED_VALIDATOR_PATH)


def _fallback_summary(*, limited_data: bool = True, unavailable_evidence: list[str] | None = None) -> dict[str, object]:
    """Return the fallback fields persisted by the PR collector's online-review summary.

    Example:
        >>> _fallback_summary(limited_data=False)["limited_data"]
        False
    """
    return {
        "pr_metadata_transport": "public-https-fallback",
        "limited_data": limited_data,
        "unavailable_evidence": UNAVAILABLE_EVIDENCE if unavailable_evidence is None else unavailable_evidence,
    }


def _review_result(confidence: float) -> dict[str, object]:
    """Return the confidence surface consumed by the targeted validator helper.

    Example:
        >>> _review_result(0.8)["confidence"]
        0.8
    """
    return {"confidence": confidence}


def _review_metadata(confidence_gaps: list[str]) -> dict[str, object]:
    """Return the confidence-gap surface consumed by the targeted validator helper.

    Example:
        >>> _review_metadata(["missing diff"])["confidence_gaps"]
        ['missing diff']
    """
    return {"confidence_gaps": confidence_gaps}


class TestReviewValidator:
    """Public-PR fallback confidence rules in the shipped code-review validator."""

    def test_accepts_exact_gap_at_capped_confidence(self, validator: ModuleType) -> None:
        """Limited public PR metadata passes when its listed evidence gap caps confidence at 0.89.

        The summary lists every omitted evidence kind, the confidence gap names the same kinds verbatim, and confidence
        sits exactly on the cap, which is the one shape a fallback review may legitimately persist.
        """
        validator._validate_pr_fallback_confidence(
            _fallback_summary(), _review_result(0.89), _review_metadata([FALLBACK_CONFIDENCE_GAP])
        )

    @pytest.mark.parametrize(
        ("summary", "confidence_gaps", "confidence", "error"),
        [
            pytest.param(
                _fallback_summary(limited_data=False),
                [FALLBACK_CONFIDENCE_GAP],
                0.89,
                "pr-public-fallback-limitation-missing",
                id="limited-data-false",
            ),
            pytest.param(
                _fallback_summary(unavailable_evidence=[]),
                [FALLBACK_CONFIDENCE_GAP],
                0.89,
                "pr-public-fallback-limitation-missing",
                id="unavailable-evidence-missing",
            ),
            pytest.param(
                _fallback_summary(unavailable_evidence=list(reversed(UNAVAILABLE_EVIDENCE))),
                [FALLBACK_CONFIDENCE_GAP],
                0.89,
                "pr-public-fallback-evidence-not-sorted",
                id="unavailable-evidence-unsorted",
            ),
            pytest.param(
                _fallback_summary(), [], 0.89, "pr-public-fallback-confidence-gap-missing", id="confidence-gap-missing"
            ),
            pytest.param(
                _fallback_summary(),
                ["Public HTTPS PR metadata fallback omitted evidence: reviews."],
                0.89,
                "pr-public-fallback-confidence-gap-missing",
                id="confidence-gap-mismatched",
            ),
            pytest.param(
                _fallback_summary(),
                [FALLBACK_CONFIDENCE_GAP],
                0.90,
                "pr-public-fallback-confidence-cap-exceeded",
                id="confidence-cap-exceeded",
            ),
        ],
    )
    def test_rejects_incomplete_or_overconfident_artifacts(
        self,
        validator: ModuleType,
        summary: dict[str, object],
        confidence_gaps: list[str],
        confidence: float,
        error: str,
    ) -> None:
        """Missing limitations, altered gap text, or confidence above the cap are rejected with a specific code.

        Each row breaks exactly one requirement of a valid fallback artifact, so a failure names which part of the
        public-only evidence contract stopped being enforced.
        """
        with pytest.raises(SystemExit, match=error):
            validator._validate_pr_fallback_confidence(
                summary, _review_result(confidence), _review_metadata(confidence_gaps)
            )

    def test_does_not_cap_normal_gh_metadata(self, validator: ModuleType) -> None:
        """The public-fallback cap stays out of fully collected GitHub CLI review artifacts.

        Metadata fetched through `gh` is complete, so a confidence above 0.89 with no fallback gap must not be refused.
        """
        summary = {"pr_metadata_transport": "gh", "limited_data": False, "unavailable_evidence": []}

        validator._validate_pr_fallback_confidence(summary, _review_result(0.95), _review_metadata([]))


class TestSharedRemediationValidator:
    """Public-PR fallback confidence rules in the validator shared with PR remediation."""

    def test_accepts_exact_gap_at_capped_confidence(self, shared_validator: ModuleType) -> None:
        """PR remediation accepts the same capped fallback artifact that a review accepts.

        Remediation reuses review evidence, so it must not reject the one valid limited-metadata shape.
        """
        shared_validator._validate_pr_fallback_confidence(
            _fallback_summary(), _review_result(0.89), _review_metadata([FALLBACK_CONFIDENCE_GAP])
        )

    def test_rejects_confidence_above_cap(self, shared_validator: ModuleType) -> None:
        """PR remediation cannot overstate the same limited public metadata evidence.

        A fallback artifact claiming 0.90 is refused with the remediation-specific cap code.
        """
        with pytest.raises(SystemExit, match="code-remediate-pr-public-fallback-confidence-cap-exceeded"):
            shared_validator._validate_pr_fallback_confidence(
                _fallback_summary(), _review_result(0.90), _review_metadata([FALLBACK_CONFIDENCE_GAP])
            )
