"""Unit checks for public-PR fallback confidence limits in code-review artifacts."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
REVIEW_VALIDATOR_PATH = PLUGIN_ROOT / "skills" / "code-review" / "validate_artifacts.py"
SHARED_VALIDATOR_PATH = PLUGIN_ROOT / "shared" / "validate-artifacts.py"
UNAVAILABLE_EVIDENCE = ["github_provided_file_list", "mergeability", "review_decision", "reviews", "top_level_comments"]
FALLBACK_CONFIDENCE_GAP = (
    "Public HTTPS PR metadata fallback omitted evidence: "
    "github_provided_file_list, mergeability, review_decision, reviews, top_level_comments."
)


def _load_validator() -> ModuleType:
    """Load the shipped standalone code-review validator without installation."""
    assert REVIEW_VALIDATOR_PATH.is_file(), REVIEW_VALIDATOR_PATH
    specification = importlib.util.spec_from_file_location("code_review_pr_fallback_validator", REVIEW_VALIDATOR_PATH)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _load_shared_validator() -> ModuleType:
    """Load the shared validator used by PR remediation without installation."""
    specification = importlib.util.spec_from_file_location("shared_pr_fallback_validator", SHARED_VALIDATOR_PATH)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


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


def test_validate_pr_fallback_confidence_accepts_exact_gap_at_capped_confidence() -> None:
    """Allow limited public PR metadata only when its listed evidence gap caps confidence at 0.89."""
    validator = _load_validator()

    validator._validate_pr_fallback_confidence(
        _fallback_summary(),
        _review_result(0.89),
        _review_metadata([FALLBACK_CONFIDENCE_GAP]),
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
            _fallback_summary(),
            [],
            0.89,
            "pr-public-fallback-confidence-gap-missing",
            id="confidence-gap-missing",
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
def test_validate_pr_fallback_confidence_rejects_incomplete_or_overconfident_artifacts(
    summary: dict[str, object], confidence_gaps: list[str], confidence: float, error: str
) -> None:
    """Prevent missing limitations, altered gap text, or confidence that overstates public-only evidence."""
    validator = _load_validator()

    with pytest.raises(SystemExit, match=error):
        validator._validate_pr_fallback_confidence(
            summary, _review_result(confidence), _review_metadata(confidence_gaps)
        )


def test_validate_pr_fallback_confidence_does_not_cap_normal_gh_metadata() -> None:
    """Keep the public-fallback cap out of fully collected GitHub CLI review artifacts."""
    validator = _load_validator()
    summary = {
        "pr_metadata_transport": "gh",
        "limited_data": False,
        "unavailable_evidence": [],
    }

    validator._validate_pr_fallback_confidence(summary, _review_result(0.95), _review_metadata([]))


def test_shared_remediation_validator_enforces_the_same_public_fallback_cap() -> None:
    """Keep PR remediation from overstating the same limited public metadata evidence."""
    validator = _load_shared_validator()

    validator._validate_pr_fallback_confidence(
        _fallback_summary(),
        _review_result(0.89),
        _review_metadata([FALLBACK_CONFIDENCE_GAP]),
    )
    with pytest.raises(SystemExit, match="code-remediate-pr-public-fallback-confidence-cap-exceeded"):
        validator._validate_pr_fallback_confidence(
            _fallback_summary(),
            _review_result(0.90),
            _review_metadata([FALLBACK_CONFIDENCE_GAP]),
        )
