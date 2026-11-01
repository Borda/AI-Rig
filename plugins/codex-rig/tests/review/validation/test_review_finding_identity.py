"""Regression checks for exact assessed-review finding identity binding."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest
from _modules import load_module

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
VALIDATOR_PATH = PLUGIN_ROOT / "skills" / "code-review" / "validate_artifacts.py"


def _load_validator() -> ModuleType:
    """Load the standalone review validator without package installation."""
    return load_module("codex_rig_review_identity_validator", VALIDATOR_PATH)


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

    Records are canonical (``finding_records_version=1`` with complete detail fields) since the validator now
    requires the marker on every new schema-v2 candidate. ``required_change``/``evidence`` match what ``_notes()``
    renders so callers exercising the action table need no extra fixture work.

    Example:
        >>> _metadata()["review_decision"]["recommendation"]
        'needs-more-work'
    """
    return {
        "finding_records_version": 1,
        "review_decision": {
            "recommendation": "needs-more-work",
            "summary": "The findings require changes.",
            "rationale": "The action rows map each assessed finding to a required change.",
        },
        "review_findings": [
            {
                "id": "R1",
                "severity": finding_severity,
                "title": "Resolve finding",
                "summary": "Observed issue",
                "required_change": "Resolve it",
                "evidence": ["evidence"],
                "closure_evidence": "Regression passes",
            },
            {
                "id": "R2",
                "severity": "medium",
                "title": "Resolve finding",
                "summary": "Observed issue",
                "required_change": "Resolve it",
                "evidence": ["evidence"],
                "closure_evidence": "Regression passes",
            },
        ],
        "operational_blockers": [
            {"id": "G1", "title": "Resolve blocker", "required_change": "Resolve it", "evidence": ["evidence"]}
        ],
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


def _canonical_record(identity: str, severity: str) -> dict[str, object]:
    """Return one canonical review-finding record for the given stable identity and severity."""
    return {
        "id": identity,
        "severity": severity,
        "title": "Resolve finding",
        "summary": "Observed issue",
        "required_change": "Resolve it",
        "evidence": ["evidence"],
        "closure_evidence": "Regression passes",
    }


def _run_cli_validation(
    tmp_path: Path, metadata: dict[str, object], identities: list[str]
) -> subprocess.CompletedProcess[str]:
    """Run the shipped validator CLI against one minimally complete assessed candidate."""
    notes = tmp_path / "review-notes.md"
    _notes(notes, identities)
    sections = (
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
    with notes.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(f"## {section}\n" for section in sections))
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


@pytest.fixture(scope="module")
def validator() -> ModuleType:
    """Provide the standalone validator module, loaded once because it holds no per-test state."""
    return _load_validator()


class TestReviewDecision:
    """Protect the in-process review-decision checks that bind severity totals to stable finding records."""

    @pytest.mark.parametrize(
        ("mutate_metadata", "error"),
        [
            pytest.param(
                lambda metadata: metadata["review_findings"][0].update(severity="medium"),
                "review-findings-severity-count-mismatch:high",
                id="severity-total-mismatch",
            ),
            pytest.param(
                lambda metadata: metadata.pop("finding_records_version"),
                "review-finding-records-version-missing",
                id="canonical-marker-omitted",
            ),
        ],
    )
    def test_rejects_unbound_finding_records(
        self, validator: ModuleType, mutate_metadata: Callable[[dict[str, object]], object], error: str
    ) -> None:
        """A severity total or a missing canonical marker cannot describe a different finding set than the records.

        A severity total that disagrees with the stable records is rejected, and a new schema-v2 candidate can no longer
        fall back to the bare id/severity shape by omitting ``finding_records_version``, which would skip the canonical
        detail fields the marker exists to require.
        """
        metadata = _metadata()
        mutate_metadata(metadata)

        with pytest.raises(SystemExit, match=error):
            validator._validate_review_decision(metadata, _result())


class TestActionTable:
    """Protect how review-notes action rows bind to assessed finding and blocker identities."""

    @pytest.mark.parametrize("recommendation", ["minor-changes", "reject", "not-aligned"])
    def test_canonical_local_findings_require_the_table(
        self, validator: ModuleType, tmp_path: Path, recommendation: str
    ) -> None:
        """New local review findings fail validation when the notes carry no action table.

        Whatever the recommendation, canonical local findings need the same complete notes as their final handoff.
        """
        metadata = _metadata(finding_severity="medium")
        metadata["review_decision"]["recommendation"] = recommendation
        result = {"schema_version": 2, "findings": {"critical": 0, "high": 0, "medium": 2, "low": 0}}
        notes = tmp_path / "review-notes.md"
        notes.write_text("No action table\n", encoding="utf-8")

        with pytest.raises(SystemExit, match="review-missing-findings-action-table"):
            validator._validate_action_table(notes, result, metadata, "working-tree")

    @pytest.mark.parametrize("recommendation", ["minor-changes", "reject", "not-aligned"])
    def test_canonical_local_findings_accept_a_complete_table(
        self, validator: ModuleType, tmp_path: Path, recommendation: str
    ) -> None:
        """New local review findings validate once the notes carry one row per finding and blocker identity.

        Pairs with the missing-table case above: same metadata and recommendations, but the complete table is present.
        """
        metadata = _metadata(finding_severity="medium")
        metadata["review_decision"]["recommendation"] = recommendation
        result = {"schema_version": 2, "findings": {"critical": 0, "high": 0, "medium": 2, "low": 0}}
        notes = tmp_path / "review-notes.md"
        _notes(notes, ["R1", "R2", "G1"])

        validator._validate_action_table(notes, result, metadata, "working-tree")

    def test_accepts_exact_finding_and_blocker_identities(self, validator: ModuleType, tmp_path: Path) -> None:
        """Action rows naming exactly the assessed findings and the declared blocker validate."""
        notes = tmp_path / "review-notes.md"
        _notes(notes, ["R1", "R2", "G1"])

        validator._validate_action_table(notes, _result(), _metadata(), "pr")

    @pytest.mark.parametrize(
        ("identities", "error"),
        [
            pytest.param(
                ["R1", "R2", "unbound-area"],
                "review-findings-action-table-identity-unbound:unbound-area",
                id="invented-identity",
            ),
            pytest.param(
                ["R1", "G1"], "review-findings-action-table-identity-coverage-mismatch:R2", id="omitted-finding"
            ),
        ],
    )
    def test_rejects_invented_or_omitted_identities(
        self, validator: ModuleType, tmp_path: Path, identities: list[str], error: str
    ) -> None:
        """Action rows cannot invent an identity or omit an assessed one.

        Schema-v2 rows must match the stable finding identities and declared blocker exactly, so an area label that
        belongs to no record, or a finding with no row, is rejected with the offending identity named.
        """
        notes = tmp_path / "review-notes.md"
        _notes(notes, identities)

        with pytest.raises(SystemExit, match=error):
            validator._validate_action_table(notes, _result(), _metadata(), "pr")

    @pytest.mark.parametrize(
        "mutate_metadata",
        [
            pytest.param(lambda metadata: metadata.pop("review_findings"), id="records-absent"),
            pytest.param(
                lambda metadata: metadata.update(review_findings={"historical_summary": "Two findings"}),
                id="opaque-historical-summary",
            ),
        ],
    )
    def test_schema_v1_keeps_count_only_coverage(
        self, validator: ModuleType, tmp_path: Path, mutate_metadata: Callable[[dict[str, object]], object]
    ) -> None:
        """Historical schema-v1 reviews stay valid by action-row count, without stable finding records.

        Old reviews carry only severity totals; the validator must neither synthesize records nor interpret opaque
        historical finding metadata, whether the records are absent or replaced by a free-form summary.
        """
        notes = tmp_path / "review-notes.md"
        _notes(notes, ["First observed issue", "Second observed issue"])
        result = {"schema_version": 1, "findings": {"critical": 0, "high": 2, "medium": 0, "low": 0}}
        metadata = _metadata()
        mutate_metadata(metadata)

        validator._validate_action_table(notes, result, metadata, "pr")


class TestValidatorCli:
    """Protect the shipped validator command's stable error contract for unbound finding records and actions."""

    @pytest.mark.parametrize(
        ("mutate_metadata", "identities", "error"),
        [
            pytest.param(
                lambda metadata: metadata.pop("finding_records_version"),
                ["R1", "R2", "G1"],
                "review-finding-records-version-missing",
                id="canonical-marker-omitted",
            ),
            pytest.param(
                lambda metadata: metadata["review_findings"][0].update(severity=[]),
                ["R1", "R2", "G1"],
                "review-finding-severity-invalid:1",
                id="severity-list",
            ),
            pytest.param(
                lambda metadata: metadata["review_findings"][0].update(severity={}),
                ["R1", "R2", "G1"],
                "review-finding-severity-invalid:1",
                id="severity-object",
            ),
            pytest.param(
                lambda metadata: metadata.update(
                    review_findings=[_canonical_record("R1", "high"), _canonical_record("R1", "medium")]
                ),
                ["R1", "G1"],
                "review-finding-id-duplicate:R1",
                id="duplicate-finding-id",
            ),
            pytest.param(
                lambda metadata: metadata.update(
                    review_findings=[_canonical_record("R1", "medium"), _canonical_record("R2", "medium")]
                ),
                ["R1", "R2", "G1"],
                "review-findings-severity-count-mismatch:high",
                id="wrong-severity-total",
            ),
            pytest.param(
                lambda metadata: None,
                ["R1", "R2", "unbound-area"],
                "review-findings-action-table-identity-unbound:unbound-area",
                id="unknown-action",
            ),
            pytest.param(
                lambda metadata: None,
                ["R1", "R2"],
                "review-findings-action-table-identity-coverage-mismatch:G1",
                id="missing-operational-blocker-action",
            ),
        ],
    )
    def test_rejects_unbound_schema_v2_candidates(
        self,
        tmp_path: Path,
        mutate_metadata: Callable[[dict[str, object]], object],
        identities: list[str],
        error: str,
    ) -> None:
        """The command exits non-zero with a stable error code and no traceback for each unbound-record shape.

        Each case starts from a minimally complete assessed candidate and breaks one thing: the canonical marker, a
        malformed severity (list or object JSON values), a duplicate id, a wrong severity total, or an action row
        that invents or omits an identity. The shipped CLI boundary stays real because the exit code and stderr are
        the contract.
        """
        metadata = _metadata()
        mutate_metadata(metadata)

        completed = _run_cli_validation(tmp_path, metadata, identities)

        assert completed.returncode != 0
        assert error in completed.stderr
        assert "Traceback" not in completed.stderr
