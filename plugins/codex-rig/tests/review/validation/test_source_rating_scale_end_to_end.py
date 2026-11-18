"""Exercise source-5-v2 review artifacts through the shared validator and final handoff."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from test_review_completion_gate import (
    VALIDATE_SHARED,
    _assessed_pr,
    _complete_run,
    _load_json,
    _rerender,
    _save_json,
)

APPROVAL_VOCABULARIES = [
    pytest.param(2, "APPROVE", "accept-as-is", id="v2-with-historical-verdict"),
    pytest.param(1, "accept-as-is", "APPROVE", id="v1-with-unified-verdict"),
]


@pytest.fixture(name="review_run")
def _review_run(tmp_path: Path) -> Path:
    """Reuse the complete assessed PR bundle, including its gate and collector receipts."""
    return _assessed_pr.__wrapped__(tmp_path)


def _write_approval(run: Path, version: int, verdict: str) -> None:
    """Bind the chosen version to both result and snapshot and regenerate the final evidence."""
    result_path = run / "result.json"
    result = _load_json(result_path)
    handoff = _load_json(run / "final-handoff.json")
    result["metadata"]["review_validation"].update(source_rating_version=version, source_verdict=verdict)
    result["metadata"]["review_decision"]["recommendation"] = verdict
    handoff["tables"][0]["source_rating_version"] = version
    handoff["outcome"]["summary"] = f"Recommendation: {verdict}."
    _rerender(run, handoff, result)
    _save_json(result_path, result)


@pytest.fixture(name="v2_review")
def _v2_review(review_run: Path) -> Path:
    """Write a complete version 2 approval with a matching snapshot."""
    _write_approval(review_run, 2, "APPROVE")
    return review_run


def _validate_shared(run: Path) -> subprocess.CompletedProcess[str]:
    """Invoke the shared validator CLI for the promoted review artifact."""
    return subprocess.run(
        [
            sys.executable,
            str(VALIDATE_SHARED),
            "--skill",
            "code-review",
            "--out",
            str(run),
            "--result",
            str(run / "result.json"),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=30,
    )


@pytest.mark.integration
class TestReviewArtifactValidation:
    """Validate source-rated review artifacts through the shared and completion CLIs."""

    def test_accepts_version_two_approval(self, v2_review: Path) -> None:
        """A promoted v2 approval passes shared validation.

        The bound PR snapshot has the same scale version and presents the token as an 'approve' Suggestion.
        """
        completed = _validate_shared(v2_review)

        assert completed.returncode == 0, completed.stderr

    def test_rejects_wrong_suggestion(self, v2_review: Path) -> None:
        """The shared validator refuses a raw verdict token in Suggestion.

        Regenerate the render and receipt so the failure identifies the cell rather than stale evidence.
        """
        result_path = v2_review / "result.json"
        result = _load_json(result_path)
        handoff = _load_json(v2_review / "final-handoff.json")
        handoff["tables"][0]["rows"][-1]["cells"][1] = "APPROVE"
        _rerender(v2_review, handoff, result)
        _save_json(result_path, result)

        completed = _validate_shared(v2_review)

        assert completed.returncode == 1, completed.stderr
        assert completed.stderr.strip() == "code-review-final-handoff-pr-snapshot-suggestion-mismatch"

    def test_rejects_snapshot_version_mismatch(self, v2_review: Path) -> None:
        """The shared validator refuses a snapshot version that differs from the result.

        Keep the v2 handoff intact so its own vocabulary check passes and the cross-artifact check runs.
        """
        result_path = v2_review / "result.json"
        result = _load_json(result_path)
        result["metadata"]["review_validation"]["source_rating_version"] = 1
        _save_json(result_path, result)

        completed = _validate_shared(v2_review)

        assert completed.returncode == 1, completed.stderr
        assert completed.stderr.strip() == "code-review-final-handoff-source-rating-version-mismatch"

    @pytest.mark.parametrize(("version", "valid_verdict", "foreign_verdict"), APPROVAL_VOCABULARIES)
    def test_rejects_foreign_vocabulary(
        self, review_run: Path, version: int, valid_verdict: str, foreign_verdict: str
    ) -> None:
        """The shared CLI reports the handoff vocabulary error in either direction.

        Both metadata verdicts and the outcome agree on the spelling from the other version.
        """
        _write_approval(review_run, version, valid_verdict)
        result_path = review_run / "result.json"
        result = _load_json(result_path)
        result["metadata"]["review_decision"]["recommendation"] = foreign_verdict
        result["metadata"]["review_validation"]["source_verdict"] = foreign_verdict
        _save_json(result_path, result)
        handoff_path = review_run / "final-handoff.json"
        handoff = _load_json(handoff_path)
        # Retain the valid render and receipt because the finalizer refuses to render this mixed input.
        handoff["outcome"]["summary"] = f"Recommendation: {foreign_verdict}."
        _save_json(handoff_path, handoff)

        completed = _validate_shared(review_run)

        assert completed.returncode == 1, completed.stderr
        assert completed.stderr.strip() == (
            "final-handoff-validation-failed:final-handoff-error:review-outcome-source-rating-version-mismatch"
        )

    @pytest.mark.parametrize(("version", "valid_verdict", "foreign_verdict"), APPROVAL_VOCABULARIES)
    def test_completion_rejects_foreign_source_verdict(
        self, review_run: Path, version: int, valid_verdict: str, foreign_verdict: str
    ) -> None:
        """Completion refuses a foreign spelling in the source verdict field.

        The bound handoff and the result recommendation retain their original spelling.
        """
        _write_approval(review_run, version, valid_verdict)
        result_path = review_run / "result.json"
        result = _load_json(result_path)
        result["metadata"]["review_validation"]["source_verdict"] = foreign_verdict
        _save_json(result_path, result)

        completed = _complete_run(review_run)

        assert completed.returncode == 1, completed.stderr
        assert completed.stderr.partition("\n")[0].endswith(
            f":review-source-verdict-version-mismatch:{foreign_verdict}:source_rating_version={version}"
        ), completed.stderr


@pytest.mark.integration
class TestSourceRatingHandoff:
    """Validate and render the assessed review through the public final handoff entry points."""

    def test_renders_five_level_legend(self, v2_review: Path, finalizer: ModuleType) -> None:
        """The validated v2 snapshot renders the five source levels in order.

        Use the complete compact handoff, including its reviewer attribution and report path.
        """
        handoff = _load_json(v2_review / "final-handoff.json")

        rendered = finalizer.render_handoff(handoff, report_root=v2_review)

        assert (
            "Legend: 1 = Approve · 2 = Minor changes · 3 = Major revision · "
            "4 = Insufficient, but a good initiative · 5 = Block."
        ) in rendered.splitlines()

    def test_rejects_historical_outcome_for_version_two_snapshot(self, v2_review: Path, finalizer: ModuleType) -> None:
        """A v2 snapshot refuses an otherwise equivalent historical spelling in the outcome.

        Only the outcome changes, leaving the versioned table and reviewer rating intact.
        """
        handoff = _load_json(v2_review / "final-handoff.json")
        handoff["outcome"]["summary"] = "Recommendation: accept-as-is."

        with pytest.raises(finalizer.HandoffError, match=r"^review-outcome-source-rating-version-mismatch$"):
            finalizer.validate_handoff(handoff)
