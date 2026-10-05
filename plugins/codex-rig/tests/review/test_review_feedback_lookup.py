"""Regression checks for reviewer-evidence lookup and prior remediation feedback between review runs."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_find_review_report import FINDER_PATH, _load_finder
from test_finding_presentation import _load_finalizer, _selection

FINDER = _load_finder()


def _review_run(root: Path, number: int, findings: list[dict[str, object]] | None = None) -> Path:
    """Create one promoted PR review run with optional canonical findings and reviewer evidence."""
    run = root / "pr-12" / f"run-{number:03d}"
    (run / "specialists").mkdir(parents=True)
    (run / "specialists" / "qa-specialist.md").write_text("## Reviewer Assessment\n\nRating: 3\n", encoding="utf-8")
    (run / "review-notes.md").write_text("## Findings\n\nF7 detail.\n", encoding="utf-8")
    metadata = {
        "scope": "pr",
        "review_findings": findings or [],
        "reviewer_assessments": [{"role": "QA specialist", "rating": 3, "evidence": "specialists/qa-specialist.md"}],
    }
    (run / "result.json").write_text(json.dumps({"schema_version": 3, "metadata": metadata}), encoding="utf-8")
    return run


FINDING = {
    "id": "F7",
    "severity": "high",
    "title": "Guard missing",
    "authors": ["QA specialist"],
    "evidence": ["review-notes.md#Findings", "src/guard.py:12", "../../outside.md", "Caller record absent"],
}


class TestFindingEvidence:
    """Protect the read-only reviewer-evidence lookup remediation uses before asking the user."""

    def test_resolves_author_and_contained_artifacts_and_keeps_source_evidence(self, tmp_path: Path) -> None:
        """Author assessments and run-local files resolve; source coordinates, prose, and escapes stay unresolved.

        A pointer leaving the review run must never be followed, even when the target file exists.
        """
        run = _review_run(tmp_path, 1, [FINDING])
        (tmp_path / "pr-12" / "outside.md").write_text("not review evidence", encoding="utf-8")

        evidence = FINDER.finding_evidence(run / "result.json", "F7")

        assert evidence["author_evidence"] == [
            {"author": "QA specialist", "path": str((run / "specialists" / "qa-specialist.md").resolve())}
        ]
        assert evidence["artifact_evidence"] == [
            {"evidence": "review-notes.md#Findings", "path": str((run / "review-notes.md").resolve())}
        ]
        assert evidence["source_evidence"] == ["src/guard.py:12", "../../outside.md", "Caller record absent"]

    @pytest.mark.parametrize(
        ("name", "finding_id", "code"),
        [
            pytest.param("result.json", "F99", "finding-evidence-unknown-id:F99", id="unknown-id"),
            pytest.param("result.candidate.json", "F7", "finding-evidence-requires-promoted-result", id="candidate"),
        ],
    )
    def test_rejects_unknown_ids_and_unpromoted_results(
        self, tmp_path: Path, name: str, finding_id: str, code: str
    ) -> None:
        """Only a promoted result's canonical records can be looked up."""
        run = _review_run(tmp_path, 1, [FINDING])
        (run / name).write_bytes((run / "result.json").read_bytes())

        with pytest.raises(LookupError, match=code):
            FINDER.finding_evidence(run / name, finding_id)

    def test_cli_requires_a_finding_id(self, tmp_path: Path) -> None:
        """The command refuses an evidence lookup that names no finding."""
        run = _review_run(tmp_path, 1, [FINDING])

        completed = subprocess.run(
            [sys.executable, str(FINDER_PATH), "--finding-evidence", str(run / "result.json")],
            capture_output=True,
            text=True,
            check=False,
        )

        assert completed.returncode == 2
        assert "--finding-evidence requires --finding-id" in completed.stderr


class TestPriorResolutions:
    """Protect how a later review learns what remediation did with earlier findings."""

    def test_reads_latest_outcome_per_finding_from_newest_prior_ledger(self, tmp_path: Path) -> None:
        """The newest lower run with a ledger wins, a later line supersedes an earlier one, and junk is counted.

        Titles come from that prior run's canonical findings so reviewer briefs can name each finding.
        """
        older = _review_run(tmp_path, 1, [FINDING])
        (older / "resolution.jsonl").write_text('{"finding_id": "OLD", "verdict": "fixed"}\n', encoding="utf-8")
        prior = _review_run(tmp_path, 2, [FINDING])
        lines = [
            {"schema_version": 1, "finding_id": "F7", "verdict": "skipped", "sha": None, "why": "Blocked."},
            {"schema_version": 1, "finding_id": "F7", "verdict": "fixed", "sha": "abc1234", "why": "Guard added."},
        ]
        text = "".join(json.dumps(line) + "\n" for line in lines) + "not json\n"
        (prior / "resolution.jsonl").write_text(text, encoding="utf-8")
        current = _review_run(tmp_path, 3)
        (current / "resolution.jsonl").write_text('{"finding_id": "NEW", "verdict": "fixed"}\n', encoding="utf-8")

        summary = FINDER.prior_resolutions(current)

        assert summary["review_run"] == str(prior.resolve())
        assert summary["resolutions"] == [
            {"finding_id": "F7", "title": "Guard missing", "verdict": "fixed", "sha": "abc1234", "why": "Guard added."}
        ]
        assert summary["invalid_lines"] == 1

    def test_first_review_has_no_prior_resolutions(self, tmp_path: Path) -> None:
        """A run without earlier ledgers reports none, which is the normal first-review case."""
        current = _review_run(tmp_path, 1)

        assert FINDER.prior_resolutions(current) == {
            "review_run": None,
            "ledger": None,
            "resolutions": [],
            "invalid_lines": 0,
        }

    def test_requires_a_promoted_pull_request_run(self, tmp_path: Path) -> None:
        """Local and temporary runs have no run topology to compare against, so lookup refuses them."""
        local = tmp_path / "2026-10-02T00-00-00Z"
        local.mkdir()

        with pytest.raises(LookupError, match="prior-resolutions-requires-pr-run"):
            FINDER.prior_resolutions(local)


def _report_views(first: str, second: str) -> dict[str, object]:
    """Build a selection whose two items cite finding F7 through the two given report paths."""
    payload = _selection()
    payload["items"][0]["sources"][0]["source_id"] = first
    payload["items"][1]["sources"] = copy.deepcopy(payload["items"][0]["sources"])
    payload["items"][1]["sources"][0]["source_id"] = second
    payload["items"][1]["sources"][0]["finding_id"] = "F7"
    return payload


class TestReportIdentityByRun:
    """Protect run-keyed report identity for canonical finding ownership."""

    def test_two_views_of_one_review_run_share_finding_identity(self) -> None:
        """The JSON result and notes of one run are one report, so F7 cannot be owned twice."""
        payload = _report_views(
            ".reports/codex/code-review/pr-12/run-003/result.json#F7",
            ".reports/codex/code-review/pr-12/run-003/review-notes.md:62",
        )

        with pytest.raises(ValueError, match="selection-canonical-finding-duplicate"):
            _load_finalizer().render_selection(payload)

    @pytest.mark.parametrize(
        "second",
        [
            pytest.param(".reports/codex/code-review/pr-12/run-004/result.json#F7", id="later-run"),
            pytest.param(".reports/codex/review/pr-12/run-003/result.json#F7", id="other-root"),
        ],
    )
    def test_distinct_runs_may_reuse_a_finding_id(self, second: str) -> None:
        """Another run of the same PR, or the same run number under another report root, is a different report."""
        payload = _report_views(".reports/codex/code-review/pr-12/run-003/result.json#F7", second)

        rendered = _load_finalizer().render_selection(payload)

        assert f"report [{second}]" in rendered
