"""Acceptance checks for deterministic cross-skill final handoffs.

Covers the standalone finalizer renderer and validator (`shared/final_handoff.py`), its CLI, and the schema-v2 result
validator that binds a rendered final handoff to a result by digest.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from _artifact_files import read_json
from _final_handoff_support import (
    FINALIZER,
    GATE_IDS,
    PLUGIN_ROOT,
    handoff_payload,
    load_finalizer,
    load_shared_validator,
    write_schema_v2_assess,
)
from _modules import load_module
from _platform import FILE_SYMLINKS_AVAILABLE, HARD_LINKS_AVAILABLE

RESULT_WRITER = PLUGIN_ROOT / "shared" / "write-result.py"
# Suites outside this directory load this module by path and call the fixtures under their former names.
_load_finalizer = load_finalizer
_load_shared_validator = load_shared_validator
_write_schema_v2_assess = write_schema_v2_assess
FINDINGS_HEADING = "Review Findings and Merge Blocks"
CURRENT_SUMMARY = "The reviewed change needs more work before it can be accepted."
HISTORICAL_SUMMARY = "This review needs more work before it can be accepted."
FINDING_TABLES = {
    "concise": (
        "| ID | Author | Finding | Resolution proposal | Status |",
        "| CR-1 | {author}, QA specialist | Empty input fails | Handle empty input | Required |",
    ),
    "legacy": (
        "| Finding / area | Author | Required change | Evidence | Status |",
        "| CR-1 | {author}, QA specialist | Handle empty input | config.py:12 | Required |",
    ),
}
HISTORICAL_PRESENTATIONS = [
    pytest.param(2, "legacy", id="historical-v2-legacy"),
    pytest.param(2, "concise", id="historical-v2-concise"),
    pytest.param(3, "legacy", id="historical-v3-legacy"),
    pytest.param(3, "concise", id="historical-v3-concise"),
]
V7_PRESENTATION = pytest.param(7, "concise", id="historical-v7-concise")
ATTRIBUTION_HEADINGS = pytest.mark.parametrize("heading", ["PR Snapshot", "Review Snapshot"])
ATTRIBUTION_SUFFIXES = pytest.mark.parametrize("suffix", ["", " (parent substitute)"])


def _load_result_writer() -> Any:
    """Load the standalone result writer without requiring a package import."""
    return load_module("codex_rig_result_writer", RESULT_WRITER)


@pytest.fixture
def validator() -> Any:
    """Fresh copy of the shared artifact validator module."""
    return load_shared_validator()


@pytest.fixture
def result_writer() -> Any:
    """Fresh copy of the standalone result writer module."""
    return _load_result_writer()


def _run_finalizer(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the finalizer CLI in a real subprocess and capture its text output."""
    return subprocess.run([sys.executable, str(FINALIZER), *args], capture_output=True, text=True, check=False)


def _zero_coverage() -> dict[str, int]:
    """Return source coverage for a handoff that carries no source records."""
    return {"source_records_total": 0, "represented_source_records_total": 0, "omitted_source_records_total": 0}


def _gate_verification(status: str, evidence: str) -> list[dict[str, str]]:
    """Return one verification entry per canonical gate with a shared status and evidence."""
    return [{"check": gate_id, "status": status, "evidence": evidence} for gate_id in GATE_IDS]


def _commit_disposition(
    status: str = "pending",
    reason: str = "Awaiting the user's commit choice; changes remain unstaged.",
    evidence: str = "commit-plan.md",
) -> dict[str, str]:
    """Return a commit-disposition record; the defaults describe an undecided commit."""
    return {"status": status, "reason": reason, "evidence": evidence}


def _unavailable_payload(*, status: str = "not-applicable", evidence: str = "gates.json", **updates: object) -> dict:
    """Return a code-review handoff for a collection failure: no tables, no sources, one recovery action."""
    payload = handoff_payload()
    payload.update(
        skill="code-review",
        branch="unavailable",
        outcome={"title": "PR Review Availability", "summary": "Collection stopped."},
        tables=[],
        source_records=[],
        source_coverage=_zero_coverage(),
        verification=_gate_verification(status, evidence),
        remaining=[
            {"row_id": "recovery", "item": "Collect PR", "owner": "code-review", "next_action": "Run collector."}
        ],
        next_steps=["recovery"],
        artifacts=[{"label": "Result", "path": "run/result.json"}],
    )
    payload.update(updates)
    return payload


def _assessed_review_payload(**updates: object) -> dict[str, object]:
    """Return an assessed code-review handoff with a canonical recommendation and no remaining work."""
    payload = handoff_payload()
    payload.update(
        skill="code-review",
        branch="assessed",
        outcome={"title": "Review Decision", "summary": "Recommendation: needs-more-work."},
        remaining=[],
        next_steps=[],
    )
    payload.update(updates)
    return payload


def _review_payload(heading: str, presentation: int, layout: str, suffix: str) -> dict[str, object]:
    """Return an assessed review with two rated reviewers and one finding authored by both of them."""
    return _assessed_review_payload(
        presentation_version=presentation,
        tables=[
            {
                "heading": heading,
                "summary": "Configuration needs a boundary fix.",
                "columns": ["Field", "Value"],
                "rows": [{"id": "PR", "cells": ["PR", "#42"], "source_ids": ["report:CR-2"]}],
                "reviewers": [
                    {"role": f"Software engineer{suffix}", "rating": 3, "evidence": "specialists/sw-engineer.md"},
                    {"role": "QA specialist", "rating": 2, "evidence": "specialists/qa-specialist.md"},
                ],
            },
            {
                "heading": FINDINGS_HEADING,
                "layout": layout,
                "columns": ["Finding / area", "Required change", "Evidence", "Status"],
                "rows": [
                    {
                        "id": "CR-1",
                        "title": "Empty input fails",
                        "authors": [f"Software engineer{suffix}", "QA specialist"],
                        "cells": ["CR-1", "Handle empty input", "config.py:12", "Required"],
                        "source_ids": ["report:CR-1"],
                    }
                ],
                **({"overview_only": True} if presentation >= 7 else {}),
            },
        ],
    )


def _write_review_notes(root: Path, finalizer: Any, payload: dict[str, object]) -> None:
    """Write the review notes; an overview-only findings table needs the full details appended to them."""
    overview = payload["tables"][1].get("overview_only", False)
    details = "\n" + finalizer.render_review_report_details(payload) if overview else ""
    (root / "review-notes.md").write_text("Full finding evidence: config.py:12.\n" + details, encoding="utf-8")


def _set_unbound_author(payload: dict[str, object]) -> None:
    """Credit a finding to a reviewer who has no rating."""
    payload["tables"][1]["rows"][0]["authors"] = ["Skipped reviewer"]


def _drop_authors(payload: dict[str, object]) -> None:
    """Remove the authors from a finding."""
    del payload["tables"][1]["rows"][0]["authors"]


def _alias_same_path(handoff: Path, _candidate: Path) -> Path:
    """Use the handoff file itself as the output path."""
    return handoff


def _alias_symlink(handoff: Path, candidate: Path) -> Path:
    """Make the output path a symlink to the handoff file."""
    candidate.symlink_to(handoff.name)
    return candidate


def _alias_hardlink(handoff: Path, candidate: Path) -> Path:
    """Make the output path a hard link to the handoff file."""
    os.link(handoff, candidate)
    return candidate


class TestRender:
    """Deterministic Markdown rendering of validated handoffs."""

    def test_standard_markdown_is_complete_and_deterministic(self, finalizer: Any) -> None:
        """Render every required section, escaping table delimiters and newlines, identically on repeat.

        A remediation handoff holds a `|` and a newline inside table cells; both must survive as table-safe text.
        """
        rendered = finalizer.render_handoff(handoff_payload())

        assert rendered.startswith("**Outcome**\n\nRemediation Summary: One item fixed; one remains external.\n")
        assert "**Final Outcome Table**" in rendered
        assert "Preserve a \\| boundary" in rendered
        assert "Implemented — guarded<br>input" in rendered
        assert "**Remaining**\n\n- CR-2 — External CI — owner: ci — next: Rerun CI." in rendered
        assert not any(line.startswith("#") for line in rendered.splitlines())
        assert "\x1b[" not in rendered
        assert rendered.endswith("Ledger: .reports/codex/code-remediate/run/action-items.md\n")
        assert finalizer.render_handoff(handoff_payload()) == rendered

    def test_release_tables_show_readiness_beside_changes(self, finalizer: Any) -> None:
        """Show readiness independently from release changes without losing source coverage.

        Both tables come from one release handoff; each keeps its own columns and rows.
        """
        payload = handoff_payload()
        payload["skill"] = "release"
        payload["tables"] = [
            {
                "heading": "Changes",
                "columns": ["Change", "SemVer impact", "Status / blocker", "Evidence"],
                "rows": [
                    {
                        "id": "CR-1",
                        "cells": ["Boundary fix", "patch", "Drafted", "commit abc"],
                        "source_ids": ["report:CR-1"],
                    }
                ],
            },
            {
                "heading": "Readiness",
                "columns": ["Check", "Status", "Evidence", "Blocker / next action"],
                "rows": [
                    {
                        "id": "CR-2",
                        "cells": ["Verification", "unavailable", "CI unavailable", "CI owner: rerun"],
                        "source_ids": ["report:CR-2"],
                    }
                ],
            },
        ]

        rendered = finalizer.render_handoff(payload)

        assert "| Change | SemVer impact | Status / blocker | Evidence |" in rendered
        assert "**Readiness**\n\n| Check | Status | Evidence | Blocker / next action |" in rendered
        assert "| Verification | unavailable | CI unavailable | CI owner: rerun |" in rendered

    def test_v3_unavailable_leads_with_plain_english_and_row_bound_next_steps(self, finalizer: Any) -> None:
        """Keep a new unavailable report useful when only one recovery action and no executable checks exist.

        The summary opens the reply, the recovery action is printed once under Next steps, and the gate file name stays
        out of the prose.
        """
        payload = _unavailable_payload(
            presentation_version=3,
            outcome={
                "title": "PR Review Availability",
                "summary": "I could not retrieve the PR metadata, so the review has not started. "
                "Reason: `github-network:gh-pr-view`.",
            },
            remaining=[
                {
                    "row_id": "collection-recovery",
                    "item": "PR collection stopped at `github-network:gh-pr-view`.",
                    "owner": "code-review",
                    "next_action": (
                        "Code-review must inspect the classified `gh-pr-view` collector failure and record a permitted "
                        "recovery before retrying. "
                        "Resume only after a fresh collector run produces and validates the PR source bundle."
                    ),
                }
            ],
            next_steps=["collection-recovery"],
        )

        rendered = finalizer.render_handoff(payload)

        assert rendered.startswith("I could not retrieve the PR metadata, so the review has not started.\n")
        assert "**Results**" not in rendered
        assert "- Checks were not run; no executable verification applies to this branch." in rendered
        assert "gates.json" not in rendered
        assert "**Remaining**" not in rendered
        assert rendered.count("Resume only after a fresh collector run") == 1
        assert rendered.count("PR collection stopped at `github-network:gh-pr-view`.") == 1
        assert (
            "**Next steps**\n\n- collection-recovery — PR collection stopped at `github-network:gh-pr-view`. — owner: "
            "code-review" in rendered
        )

    def test_v2_table_titled_results_is_not_repeated(self, finalizer: Any) -> None:
        """Keep the enclosing v2 Results heading from repeating an identically named table title.

        A table headed `Results` would otherwise print the same bold title twice in a row.
        """
        payload = handoff_payload()
        payload["presentation_version"] = 2
        payload["tables"][0]["heading"] = "Results"

        rendered = finalizer.render_handoff(payload)

        assert rendered.count("**Results**") == 1

    def test_symbol_details_follow_their_table(self, finalizer: Any) -> None:
        """Keep long table text readable through validated under-table references.

        A cell cites `[O1]`; the matching detail text is printed directly below the table.
        """
        payload = handoff_payload()
        table = payload["tables"][0]
        table["rows"][0]["cells"][4] = "implemented — [O1]"
        table["details"] = [{"id": "O1", "text": "Guarded multiline input without changing valid requests."}]

        rendered = finalizer.render_handoff(payload)

        table_end = (
            "| CR-2 | medium | Run external CI | report [CR-2] | Unresolved — external runner unavailable | "
            "CI owner must rerun |"
        )
        assert f"{table_end}\n\n[O1] Guarded multiline input without changing valid requests." in rendered

    @pytest.mark.parametrize(
        "extra",
        [
            pytest.param({}, id="unversioned"),
            pytest.param({"presentation_version": 10}, id="v10"),
            pytest.param({"presentation_version": 11}, id="v11"),
            pytest.param({"presentation_version": 12}, id="v12"),
            pytest.param({"presentation_version": 13}, id="v13"),
        ],
    )
    def test_caller_contract_is_emitted_byte_for_byte(self, finalizer: Any, extra: dict[str, int]) -> None:
        """Keep an explicit strict output contract free of added workflow prose.

        A caller that asked for JSON only receives exactly its requested bytes in every presentation version.
        """
        payload = handoff_payload()
        payload.update(extra, branch="caller-contract", tables=[], source_records=[], source_coverage=_zero_coverage())
        payload["caller_contract"] = {
            "format": "application/json",
            "evidence": "User explicitly requested JSON only.",
            "output": '{"status":"ok"}\n',
        }

        assert finalizer.render_handoff(payload) == '{"status":"ok"}\n'


class TestReviewAttribution:
    """Reviewer ratings and finding authors shown beside the aggregate review decision."""

    @ATTRIBUTION_HEADINGS
    @ATTRIBUTION_SUFFIXES
    @pytest.mark.parametrize(
        ("presentation", "layout", "summary"),
        [
            pytest.param(2, "legacy", HISTORICAL_SUMMARY, id="historical-v2-legacy"),
            pytest.param(2, "concise", HISTORICAL_SUMMARY, id="historical-v2-concise"),
            pytest.param(3, "legacy", HISTORICAL_SUMMARY, id="historical-v3-legacy"),
            pytest.param(3, "concise", HISTORICAL_SUMMARY, id="historical-v3-concise"),
            pytest.param(7, "concise", HISTORICAL_SUMMARY, id="historical-v7-concise"),
            pytest.param(8, "concise", HISTORICAL_SUMMARY, id="historical-v8-concise"),
            pytest.param(9, "concise", HISTORICAL_SUMMARY, id="historical-v9-concise"),
            pytest.param(10, "concise", HISTORICAL_SUMMARY, id="historical-v10-concise"),
            pytest.param(11, "concise", HISTORICAL_SUMMARY, id="historical-v11-concise"),
            pytest.param(12, "concise", CURRENT_SUMMARY, id="current-v12-concise"),
        ],
    )
    def test_summary_states_the_decision(
        self, finalizer: Any, tmp_path: Path, heading: str, presentation: int, layout: str, summary: str, suffix: str
    ) -> None:
        """The aggregate decision is stated in plain words in every presentation version.

        Each version renders the same assessed review; its wording differs only between v12 and the historical ones.
        """
        payload = _review_payload(heading, presentation, layout, suffix)
        _write_review_notes(tmp_path, finalizer, payload)

        rendered = finalizer.render_handoff(payload, report_root=tmp_path)

        assert summary in rendered

    @ATTRIBUTION_HEADINGS
    @ATTRIBUTION_SUFFIXES
    @pytest.mark.parametrize(("presentation", "layout"), HISTORICAL_PRESENTATIONS)
    def test_v2_v3_show_ratings_and_deduplicated_authors(
        self, finalizer: Any, tmp_path: Path, heading: str, presentation: int, layout: str, suffix: str
    ) -> None:
        """Show each reviewer's rating separately from the decision and list each finding's authors once.

        The v2 and v3 renderings print ratings in parentheses under a legend and keep the finding evidence inline.
        """
        payload = _review_payload(heading, presentation, layout, suffix)
        _write_review_notes(tmp_path, finalizer, payload)
        header, row = FINDING_TABLES[layout]

        rendered = finalizer.render_handoff(payload, report_root=tmp_path)

        assert "Configuration needs a boundary fix." in rendered
        assert f"| Reviewers | Software engineer{suffix} (3), QA specialist (2). |\n\nLegend: 1 = Approve" in rendered
        assert "4 = Insufficient evidence · 5 = Block / Reject." in rendered
        assert header in rendered
        assert row.format(author=f"Software engineer{suffix}") in rendered
        assert "config.py:12" in rendered
        assert payload["tables"][0]["reviewers"][0]["role"] == f"Software engineer{suffix}"

    @ATTRIBUTION_HEADINGS
    @ATTRIBUTION_SUFFIXES
    def test_v7_shows_bracketed_ratings_and_moves_evidence_to_the_report(
        self, finalizer: Any, tmp_path: Path, heading: str, suffix: str
    ) -> None:
        """Print v7 ratings in brackets with a Report row, drop any parent-substitute note, keep evidence in the report.

        The overview-only findings table omits the evidence column; the full evidence lives in the review notes.
        """
        payload = _review_payload(heading, 7, "concise", suffix)
        _write_review_notes(tmp_path, finalizer, payload)
        header, row = FINDING_TABLES["concise"]

        rendered = finalizer.render_handoff(payload, report_root=tmp_path)

        assert "Configuration needs a boundary fix." in rendered
        assert "| Reviewers | Software engineer [3], QA specialist [2]. |\n| Report |" in rendered
        assert "parent substitute" not in rendered
        assert "4 = Insufficient evidence · 5 = Block / Reject." in rendered
        assert header in rendered
        assert row.format(author="Software engineer") in rendered
        assert "config.py:12" in (tmp_path / "review-notes.md").read_text(encoding="utf-8")
        assert payload["tables"][0]["reviewers"][0]["role"] == f"Software engineer{suffix}"

    @ATTRIBUTION_HEADINGS
    @ATTRIBUTION_SUFFIXES
    @pytest.mark.parametrize(("presentation", "layout"), [*HISTORICAL_PRESENTATIONS, V7_PRESENTATION])
    @pytest.mark.parametrize(
        ("break_authors", "error"),
        [
            pytest.param(_set_unbound_author, "finding-authors-unbound", id="unrated-author"),
            pytest.param(_drop_authors, "finding-authors", id="missing-authors"),
        ],
    )
    def test_rejects_finding_authors_without_a_rating(
        self,
        finalizer: Any,
        heading: str,
        presentation: int,
        layout: str,
        suffix: str,
        break_authors: Callable[[dict[str, object]], None],
        error: str,
    ) -> None:
        """A finding must name authors, and each author must be a rated reviewer.

        Crediting a finding to a reviewer with no rating, or to nobody, would publish unattributable review claims.
        """
        payload = _review_payload(heading, presentation, layout, suffix)
        break_authors(payload)

        with pytest.raises(finalizer.HandoffError, match=error):
            finalizer.render_handoff(payload)

    @pytest.mark.parametrize("rating", [0, 6, True, "3", None])
    def test_rejects_invalid_reviewer_rating(self, finalizer: Any, rating: object) -> None:
        """Reject out-of-scale ratings and booleans rather than publishing misleading reviewer judgments.

        Valid ratings are the integers 1 to 5; booleans, strings and null are not ratings.
        """
        payload = _assessed_review_payload(
            tables=[
                {
                    "heading": "PR Snapshot",
                    "columns": ["Field", "Value"],
                    "rows": [{"id": "PR", "cells": ["PR", "#42"], "source_ids": ["report:CR-1", "report:CR-2"]}],
                    "reviewers": [{"role": "QA specialist", "rating": rating, "evidence": "qa.md"}],
                }
            ]
        )

        with pytest.raises(finalizer.HandoffError, match="reviewer-rating-invalid"):
            finalizer.render_handoff(payload)


class TestValidation:
    """Structural and contract checks that reject a handoff before it is rendered."""

    def test_historical_unavailable_review_keeps_executed_gate_statuses(self, finalizer: Any) -> None:
        """Historical handoffs without `presentation_version` stay readable under their existing contract.

        Before v2 an unavailable review could carry passed gates; those artifacts must keep validating unchanged.
        """
        payload = _unavailable_payload(status="pass", evidence="diagnostic command exited zero")

        assert finalizer.validate_handoff(payload) == payload

    def test_v2_unavailable_review_rejects_executed_gates(self, finalizer: Any) -> None:
        """Prevent a terminal collection failure from presenting unrun PR gates as passed.

        New unavailable reviews are created after source collection stops, before any canonical PR verification gate can
        run, so every gate must be not-applicable.
        """
        payload = _unavailable_payload(presentation_version=2, status="pass", evidence="diagnostic command exited zero")

        with pytest.raises(finalizer.HandoffError, match="unavailable-review-verification-must-be-not-applicable"):
            finalizer.validate_handoff(payload)

    def test_rejects_non_integer_presentation_version(self, finalizer: Any) -> None:
        """Prevent JSON numeric equality from accepting a noncanonical presentation version.

        `2.0 == 2` in Python, but only the integer form is a valid version.
        """
        payload = handoff_payload()
        payload["presentation_version"] = 2.0

        with pytest.raises(finalizer.HandoffError, match="handoff-presentation-version-invalid"):
            finalizer.validate_handoff(payload)

    def test_rejects_unreferenced_table_detail(self, finalizer: Any) -> None:
        """Reject detail text that has no compact symbol in its owning table.

        A detail `O1` that no cell cites would print orphan text under the table.
        """
        payload = handoff_payload()
        payload["tables"][0]["details"] = [{"id": "O1", "text": "No table cell refers to this detail."}]

        with pytest.raises(finalizer.HandoffError, match="table-detail-unreferenced:O1"):
            finalizer.validate_handoff(payload)

    def test_rejects_omitted_source_even_when_counts_are_rewritten(self, finalizer: Any) -> None:
        """Reject a table that hides one declared source behind self-consistent aggregate counts.

        The second row is removed and the coverage totals are edited to match; the missing source record still shows.
        """
        payload = handoff_payload()
        second_row = payload["tables"][0]["rows"].pop()
        assert second_row["id"] == "CR-2"
        payload["source_coverage"].update(represented_source_records_total=1, omitted_source_records_total=1)

        with pytest.raises(finalizer.HandoffError, match="omitted-source-records"):
            finalizer.validate_handoff(payload)

    def test_closed_code_review_forbids_tables(self, finalizer: Any) -> None:
        """Preserve the review close branch's plain-prose no-table contract.

        A closed review is a terminal decision, not a findings report; a table there would imply source findings.
        """
        payload = handoff_payload()
        payload.update(skill="code-review", branch="closed")

        with pytest.raises(finalizer.HandoffError, match="terminal-branch-forbids-tables"):
            finalizer.validate_handoff(payload)

    @staticmethod
    def _assessed_findings_payload(summary: str) -> dict[str, object]:
        """Return an assessed review with one finding row and the given decision summary."""
        return _assessed_review_payload(
            outcome={"title": "Review Decision", "summary": summary},
            tables=[
                {
                    "heading": FINDINGS_HEADING,
                    "columns": ["Finding / area", "Required change", "Evidence", "Status"],
                    "rows": [
                        {
                            "id": "R1",
                            "cells": ["R1", "Add a guard", "tests", "Required"],
                            "source_ids": ["review:R1"],
                        }
                    ],
                }
            ],
            source_records=[{"id": "review:R1", "evidence": "tests"}],
            source_coverage={
                "source_records_total": 1,
                "represented_source_records_total": 1,
                "omitted_source_records_total": 0,
            },
            remaining=[{"row_id": "R1", "item": "Add a guard", "owner": "author", "next_action": "Implement it."}],
            next_steps=["R1"],
        )

    def test_assessed_review_accepts_canonical_recommendation(self, finalizer: Any) -> None:
        """An assessed review whose outcome is the machine-bound recommendation validates.

        The recommendation sentence is the only prose an assessed review outcome may carry.
        """
        payload = self._assessed_findings_payload("Recommendation: needs-more-work.")

        assert finalizer.validate_handoff(payload) == payload

    def test_assessed_review_rejects_free_text_outcome(self, finalizer: Any) -> None:
        """Keep assessed review prose limited to its machine-bound recommendation.

        A free-text verdict such as "Approve this PR." could disagree with the recorded recommendation.
        """
        payload = self._assessed_findings_payload("Approve this PR.")

        with pytest.raises(finalizer.HandoffError, match="review-outcome-not-canonical"):
            finalizer.validate_handoff(payload)


class TestRenderedBinding:
    """Digest binding between a handoff, its rendered text and its validation record."""

    def test_retained_v2_handoff_checks_original_next_steps_bytes(self, finalizer: Any, tmp_path: Path) -> None:
        """A previously bound v2 result keeps its exact rendered bytes after later presentation changes.

        The stored render predates today's wording; `check_files` must accept it because the digests still match.
        """
        payload = _unavailable_payload(presentation_version=2)
        prior_render = (
            "Collection stopped.\n\n"
            "**Outcome**\n\n"
            "PR Review Availability\n\n"
            "**Verification**\n\n"
            "- Checks were not run; no executable verification applies to this branch.\n\n"
            "**Next steps**\n\n"
            "- Collect PR — owner: code-review — next: Run collector.\n\n"
            "**Confidence**\n\n"
            "0.95 (fair).\n"
            "Limits: External CI was not run.\n"
            "Gap [unresolved]: External CI was not run. — CI is external.\n\n"
            "**Artifact**\n\n"
            "Result: run/result.json\n"
        ).encode()
        handoff_path = tmp_path / "final-handoff.json"
        final_path = tmp_path / "final.md"
        validation_path = tmp_path / "final-handoff.validation.json"
        handoff_bytes = json.dumps(payload).encode("utf-8")
        handoff_path.write_bytes(handoff_bytes)
        final_path.write_bytes(prior_render)
        binding = {
            "schema_version": 1,
            "status": "pass",
            "skill": "code-review",
            "branch": "unavailable",
            "handoff_sha256": hashlib.sha256(handoff_bytes).hexdigest(),
            "rendered_sha256": hashlib.sha256(prior_render).hexdigest(),
        }
        validation_path.write_text(json.dumps(binding, indent=2) + "\n", encoding="utf-8", newline="\n")

        assert finalizer.check_files(handoff_path, final_path, validation_path)["status"] == "pass"

    def test_cli_render_then_check_detects_drift(self, tmp_path: Path) -> None:
        """Fail closed when a validated final render is changed after generation.

        The real CLI renders and checks a handoff; appending text to the rendered file must make `check` fail with a
        digest mismatch. Runs real subprocesses because the CLI exit codes are the contract.
        """
        handoff = tmp_path / "final-handoff.json"
        final = tmp_path / "final.md"
        validation = tmp_path / "final-handoff.validation.json"
        payload = handoff_payload()
        payload["commit_disposition"] = _commit_disposition()
        handoff.write_text(json.dumps(payload), encoding="utf-8")
        check_args = ["check", "--handoff", str(handoff), "--final", str(final), "--validation", str(validation)]

        rendered = _run_finalizer(
            "render", "--handoff", str(handoff), "--out-final", str(final), "--out-validation", str(validation)
        )
        checked = _run_finalizer(*check_args)
        final.write_text(final.read_text(encoding="utf-8") + "drift\n", encoding="utf-8")
        drifted = _run_finalizer(*check_args)

        assert rendered.returncode == 0, rendered.stderr
        assert checked.returncode == 0, checked.stderr
        assert drifted.returncode != 0
        assert "rendered-final-mismatch" in drifted.stderr

    @pytest.mark.parametrize(
        "make_alias",
        [
            pytest.param(_alias_same_path, id="same-path"),
            pytest.param(
                _alias_symlink,
                marks=pytest.mark.skipif(not FILE_SYMLINKS_AVAILABLE, reason="host cannot create file symlinks"),
                id="symlink",
            ),
            pytest.param(
                _alias_hardlink,
                marks=pytest.mark.skipif(not HARD_LINKS_AVAILABLE, reason="host cannot create hard links"),
                id="hardlink",
            ),
        ],
    )
    @pytest.mark.parametrize(
        ("aliased", "other"),
        [pytest.param("final", "validation", id="final"), pytest.param("validation", "final", id="validation")],
    )
    def test_render_refuses_output_aliasing_handoff_input(
        self, finalizer: Any, tmp_path: Path, make_alias: Callable[[Path, Path], Path], aliased: str, other: str
    ) -> None:
        """A generated output must never overwrite its input through any file alias.

        Whether the final text or the validation record is aliased to the handoff by path, symlink or hard link, the
        render is refused, the input is untouched and the other output is not written.
        """
        handoff = tmp_path / "final-handoff.json"
        payload = handoff_payload()
        payload["commit_disposition"] = _commit_disposition()
        original = json.dumps(payload).encode("utf-8")
        handoff.write_bytes(original)
        outputs = {"final": tmp_path / "final.md", "validation": tmp_path / "final-handoff.validation.json"}
        outputs[aliased] = make_alias(handoff, outputs[aliased])

        with pytest.raises(finalizer.HandoffError):
            finalizer.render_files(handoff, outputs["final"], outputs["validation"])

        assert handoff.read_bytes() == original
        assert not outputs[other].exists()


class TestCommitDisposition:
    """The commit checkpoint every remediation handoff must record."""

    def test_new_render_requires_a_decision(self, finalizer: Any, tmp_path: Path) -> None:
        """Prevent a remediation summary from silently skipping the commit checkpoint.

        A fresh render without `commit_disposition` is refused and leaves no output files behind.
        """
        handoff = tmp_path / "final-handoff.json"
        handoff.write_text(json.dumps(handoff_payload()), encoding="utf-8")

        with pytest.raises(finalizer.HandoffError, match="remediation-commit-disposition-missing"):
            finalizer.render_files(handoff, tmp_path / "final.md", tmp_path / "validation.json")

        assert not (tmp_path / "final.md").exists()
        assert not (tmp_path / "validation.json").exists()

    @pytest.mark.parametrize("status", ["pending", "blocked", "not-applicable", "declined", "committed"])
    @pytest.mark.parametrize("extra", [pytest.param({}, id="v1"), pytest.param({"presentation_version": 2}, id="v2")])
    def test_status_and_evidence_are_rendered_once(self, finalizer: Any, status: str, extra: dict[str, int]) -> None:
        """Retain commit disposition and its evidence in every ordinary rendering.

        Each status appears exactly once as a Commit line carrying its reason and evidence.
        """
        reason = "The recorded commit plan explains the current decision."
        payload = handoff_payload()
        payload.update(extra, commit_disposition=_commit_disposition(status, reason))

        rendered = finalizer.render_handoff(payload)

        assert rendered.count(f"Commit: {status} — {reason} Evidence: commit-plan.md") == 1

    @pytest.mark.parametrize("status", ["pending", "committed"])
    @pytest.mark.parametrize("gate_status", ["fail", "timeout", "missing-command"])
    def test_failed_verification_stays_visible(self, finalizer: Any, status: str, gate_status: str) -> None:
        """Report a requested local snapshot without converting failed verification into readiness.

        A pending or committed snapshot is printed next to the failed tests gate and does not rewrite its status.
        """
        payload = handoff_payload()
        payload["verification"][3]["status"] = gate_status
        payload["commit_disposition"] = _commit_disposition(status, "One source fix passed.")

        rendered = finalizer.render_handoff(payload)

        assert f"Commit: {status}" in rendered
        assert f"tests: {gate_status}" in rendered
        assert payload["verification"][3]["status"] == gate_status

    def test_declined_commit_claims_no_verification_readiness(self, finalizer: Any) -> None:
        """A recorded leave-unstaged answer remains valid without authorizing any commit.

        The user declined the commit while tests failed; both facts are printed and neither implies readiness.
        """
        payload = handoff_payload()
        payload["verification"][3]["status"] = "fail"
        payload["commit_disposition"] = _commit_disposition(
            "declined", "User explicitly chose to leave changes unstaged; tests also failed.", "decision.md"
        )

        rendered = finalizer.render_handoff(payload)

        assert "Commit: declined" in rendered
        assert "tests: fail" in rendered

    def test_partial_remediation_renders_explicit_blocker(self, finalizer: Any, tmp_path: Path) -> None:
        """Keep successful local work and missing verification distinct at closeout.

        Tests could not run, so the commit is blocked with the owner's repair step while implemented work stays shown.
        """
        payload = handoff_payload()
        payload["presentation_version"] = 2
        payload["verification"][3]["status"] = "fail"
        payload["commit_disposition"] = _commit_disposition(
            "blocked",
            "Changes remain unstaged: required tests could not run; environment owner must repair dependencies.",
            "gates.json",
        )
        handoff = tmp_path / "final-handoff.json"
        handoff.write_text(json.dumps(payload), encoding="utf-8")

        finalizer.render_files(handoff, tmp_path / "final.md", tmp_path / "validation.json")

        rendered = (tmp_path / "final.md").read_text(encoding="utf-8")
        assert "Commit: blocked — Changes remain unstaged: required tests could not run" in rendered
        assert "environment owner must repair dependencies" in rendered
        assert "Implemented" in rendered

    @pytest.mark.parametrize(
        "disposition",
        [
            pytest.param(None, id="null"),
            pytest.param({}, id="empty"),
            pytest.param({"status": "done", "reason": "Done", "evidence": "plan.md"}, id="unknown-status"),
            pytest.param({"status": "blocked", "reason": " ", "evidence": "plan.md"}, id="missing-reason"),
            pytest.param({"status": "declined", "reason": "User declined", "evidence": ""}, id="missing-evidence"),
        ],
    )
    def test_rejects_incomplete_disposition(self, finalizer: Any, disposition: object) -> None:
        """Do not accept an empty or ambiguous commit decision as a completed checkpoint.

        The record needs a known status, a non-blank reason and non-empty evidence.
        """
        payload = handoff_payload()
        payload["commit_disposition"] = disposition

        with pytest.raises(finalizer.HandoffError, match="commit-disposition"):
            finalizer.render_handoff(payload)

    @staticmethod
    def _snapshot_inputs(status: str) -> tuple[dict, dict, list[dict]]:
        """Return a failed result with one open high-severity code item that an explicit snapshot may retain."""
        result = {"status": status, "checks_failed": ["tests"], "findings": {"critical": 0}}
        unresolved = {
            "selected_items_unresolved": 1,
            "all_local_actionable_items_closed": False,
            "local_actionable_items_unresolved": 1,
        }
        open_items = [{"item_type": "code", "severity": "high", "resolution_status": "unresolved"}]
        return result, unresolved, open_items

    @pytest.mark.parametrize("status", ["fail", "timeout"])
    def test_explicit_snapshot_may_retain_failed_checks_and_open_local_work(self, validator: Any, status: str) -> None:
        """A local snapshot may retain a known regression while local work stays open.

        The explicit-commit predicate is true for a failed or timed-out result with unresolved non-critical items.
        """
        result, unresolved, open_items = self._snapshot_inputs(status)

        assert validator._explicit_commit_with_incomplete_verification(result, unresolved, open_items)

    @pytest.mark.parametrize("status", ["fail", "timeout"])
    def test_explicit_snapshot_check_leaves_its_inputs_unchanged(self, validator: Any, status: str) -> None:
        """Evaluating the predicate must not close items or rewrite the failed result.

        A snapshot may never claim the regression closed, so the inputs read the same afterwards.
        """
        result, unresolved, open_items = self._snapshot_inputs(status)
        before = copy.deepcopy((result, unresolved, open_items))

        validator._explicit_commit_with_incomplete_verification(result, unresolved, open_items)

        assert (result, unresolved, open_items) == before

    @pytest.mark.parametrize("status", ["fail", "timeout"])
    @pytest.mark.parametrize(
        ("unresolved_update", "critical_findings", "severity"),
        [
            pytest.param({"all_local_actionable_items_closed": True}, 0, "high", id="local-items-closed"),
            pytest.param({"local_actionable_items_unresolved": 0}, 0, "high", id="nothing-unresolved"),
            pytest.param({}, 1, "high", id="critical-finding"),
            pytest.param({}, 0, "critical", id="critical-open-item"),
        ],
    )
    def test_explicit_snapshot_is_refused_when_work_is_closed_or_critical(
        self, validator: Any, status: str, unresolved_update: dict, critical_findings: int, severity: str
    ) -> None:
        """A snapshot never hides critical findings and has nothing to retain once local work is closed.

        Each case changes one input of the retainable snapshot and the predicate turns false.
        """
        result, unresolved, open_items = self._snapshot_inputs(status)
        unresolved.update(unresolved_update)
        result["findings"]["critical"] = critical_findings
        open_items[0]["severity"] = severity

        assert not validator._explicit_commit_with_incomplete_verification(result, unresolved, open_items)


def _bound_handoff(result: dict) -> tuple[dict, Path, dict]:
    """Return a result's final-handoff binding, the handoff file path and the parsed handoff."""
    binding = result["metadata"]["final_handoff"]
    handoff_path = Path(binding["handoff_path"])
    return binding, handoff_path, read_json(handoff_path)


def _rebind_handoff(finalizer: Any, binding: dict, handoff_path: Path, handoff: dict) -> None:
    """Write an edited handoff, re-render it, and copy the fresh digests into the result's binding."""
    handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
    validation = finalizer.render_files(handoff_path, Path(binding["rendered_path"]), Path(binding["validation_path"]))
    binding.update(handoff_sha256=validation["handoff_sha256"], rendered_sha256=validation["rendered_sha256"])


class TestCommitCheckpointInResult:
    """The whole-result validator refuses a remediation candidate that skips or contradicts the commit checkpoint."""

    @pytest.mark.parametrize(
        ("result_update", "handoff_update", "error"),
        [
            pytest.param({}, {}, "remediation-commit-disposition-missing", id="historical-input-new-candidate"),
            pytest.param(
                {"status": "fail"},
                {
                    "commit_disposition": _commit_disposition(
                        reason="All executed gates passed, but selected obligations remain open."
                    )
                },
                "remediation-commit-result-blocked",
                id="open-obligations-despite-green-gates",
            ),
        ],
    )
    def test_candidate_cannot_bypass_commit_checkpoint(
        self,
        finalizer: Any,
        validator: Any,
        tmp_path: Path,
        result_update: dict,
        handoff_update: dict,
        error: str,
    ) -> None:
        """Reject a missing decision or false readiness through the complete result validator.

        The old handoff still passes `check_files`; copying its evidence into a new remediation candidate must not skip
        the checkpoint, and a failed result cannot carry a pending commit as if all work were closed.
        """
        result_path = write_schema_v2_assess(tmp_path)
        result = read_json(result_path)
        handoff_path = tmp_path / "final-handoff.json"
        original = read_json(handoff_path)
        handoff = handoff_payload()
        handoff.update({key: original[key] for key in ("verification", "confidence", "artifacts")})
        handoff.update(handoff_update)
        result.update(result_update)
        handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
        rendered = finalizer.render_handoff(handoff).encode("utf-8")
        (tmp_path / "final.md").write_bytes(rendered)
        binding = {
            "schema_version": 1,
            "status": "pass",
            "skill": "code-remediate",
            "branch": "standard",
            "handoff_sha256": hashlib.sha256(handoff_path.read_bytes()).hexdigest(),
            "rendered_sha256": hashlib.sha256(rendered).hexdigest(),
        }
        (tmp_path / "final-handoff.validation.json").write_text(json.dumps(binding), encoding="utf-8")
        result["metadata"]["final_handoff"].update(
            handoff_sha256=binding["handoff_sha256"], rendered_sha256=binding["rendered_sha256"]
        )
        result_path.write_text(json.dumps(result), encoding="utf-8")
        # Historical evidence stays readable; copying it into a new candidate cannot skip the checkpoint.
        finalizer.check_files(handoff_path, tmp_path / "final.md", tmp_path / "final-handoff.validation.json")

        with pytest.raises(SystemExit, match=error):
            validator.validate("code-remediate", tmp_path, result_path)


class TestSchemaV2Result:
    """Schema-v2 results bind a digest-checked final handoff and a canonical result path."""

    def test_requires_digest_bound_final_output(self, validator: Any, tmp_path: Path) -> None:
        """Accept an intact final handoff and reject post-render presentation drift.

        The validator passes on the generated trio of files, then fails once `final.md` is replaced by other text.
        """
        result_path = write_schema_v2_assess(tmp_path)
        validator.validate("assess", tmp_path, result_path)

        (tmp_path / "final.md").write_text("truncated\n", encoding="utf-8")

        with pytest.raises(SystemExit, match=r"final-handoff-validation-failed:.*rendered-final-mismatch"):
            validator.validate("assess", tmp_path, result_path)

    @pytest.mark.parametrize("status", ["fail", "timeout"])
    def test_failed_v12_handoff_requires_action(
        self, finalizer: Any, validator: Any, tmp_path: Path, status: str
    ) -> None:
        """Reject a failed result whose final handoff gives no owner or next step.

        The same v12 handoff is valid for a passing result but must name an action once the result failed.
        """
        result = read_json(write_schema_v2_assess(tmp_path))
        binding, handoff_path, handoff = _bound_handoff(result)
        handoff["presentation_version"] = 12
        _rebind_handoff(finalizer, binding, handoff_path, handoff)
        gates = read_json(tmp_path / "gates.json")
        validator._validate_final_handoff(result, "assess", tmp_path, gates, candidate=True)
        result["status"] = status

        with pytest.raises(SystemExit, match="failed-handoff-next-action-required"):
            validator._validate_final_handoff(result, "assess", tmp_path, gates, candidate=True)

    def test_candidate_binds_the_canonical_result_path_before_promotion(
        self, finalizer: Any, validator: Any, tmp_path: Path
    ) -> None:
        """Allow a candidate to bind the final result path before its atomic promotion.

        The candidate validates while named `result.candidate.json`, and the promoted `result.json` validates too.
        """
        candidate_path = write_schema_v2_assess(tmp_path)
        result = read_json(candidate_path)
        canonical_path = tmp_path / "result.json"
        result["artifact_path"] = str(canonical_path)
        binding, handoff_path, handoff = _bound_handoff(result)
        handoff["artifacts"][0]["path"] = str(canonical_path)
        _rebind_handoff(finalizer, binding, handoff_path, handoff)
        candidate_path.write_text(json.dumps(result), encoding="utf-8")

        validator.validate("assess", tmp_path, candidate_path)
        candidate_path.replace(canonical_path)
        validator.validate("assess", tmp_path, canonical_path)

    @pytest.mark.parametrize("artifact_path", ["not-the-result.json", "../result.json"])
    def test_rejects_noncanonical_result_artifact_paths(
        self, finalizer: Any, validator: Any, tmp_path: Path, artifact_path: str
    ) -> None:
        """Keep digest-bound presentation from naming an unrelated result artifact.

        Both the result and its handoff name another file or a path outside the run directory.
        """
        result_path = write_schema_v2_assess(tmp_path)
        result = read_json(result_path)
        result["artifact_path"] = artifact_path
        binding, handoff_path, handoff = _bound_handoff(result)
        handoff["artifacts"][0]["path"] = artifact_path
        _rebind_handoff(finalizer, binding, handoff_path, handoff)
        result_path.write_text(json.dumps(result), encoding="utf-8")

        with pytest.raises(SystemExit, match="result-artifact-path-mismatch"):
            validator.validate("assess", tmp_path, result_path)

    @pytest.mark.skipif(not FILE_SYMLINKS_AVAILABLE, reason="host cannot create file symlinks")
    def test_rejects_canonical_result_symlink_escaping_the_run_directory(
        self, validator: Any, result_writer: Any, tmp_path: Path
    ) -> None:
        """Prevent a canonical result name from resolving to evidence outside its run directory.

        `result.json` is a symlink to a file beside the run directory; both the writer and the validator refuse it.
        """
        result_path = write_schema_v2_assess(tmp_path)
        escaped_result = tmp_path.parent / "escaped-result.json"
        escaped_result.write_text("outside\n", encoding="utf-8")
        canonical_result = tmp_path / "result.json"
        canonical_result.symlink_to(escaped_result)

        with pytest.raises(SystemExit, match="result-artifact-path-mismatch"):
            result_writer.validate_artifact_path(result_path, str(canonical_result))
        with pytest.raises(SystemExit, match="result-artifact-path-mismatch"):
            validator.validate("assess", tmp_path, result_path)

    def test_rejects_duplicate_confidence_gaps_and_closures(self, validator: Any, tmp_path: Path) -> None:
        """Reject ambiguous provenance rather than retaining an arbitrary last closure.

        One gap listed twice with two conflicting closure rationales leaves no single answer to keep.
        """
        result_path = write_schema_v2_assess(tmp_path)
        result = read_json(result_path)
        metadata = result["metadata"]
        gap = metadata["confidence_gaps"][0]
        metadata["confidence_gaps"] = [gap, gap]
        metadata["confidence_gap_closures"] = [
            {"gap": gap, "status": "unresolved", "rationale": "First conflicting rationale."},
            {"gap": gap, "status": "unresolved", "rationale": "Second conflicting rationale."},
        ]
        result_path.write_text(json.dumps(result), encoding="utf-8")

        with pytest.raises(SystemExit, match="duplicate-confidence-gap"):
            validator.validate("assess", tmp_path, result_path)

    def test_resolves_workspace_relative_handoff_paths(
        self, validator: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Resolve template-style run paths relative to the invoking workspace.

        The binding names the handoff files relative to the run directory's parent, which is the current directory.
        """
        result_path = write_schema_v2_assess(tmp_path)
        result = read_json(result_path)
        run_path = Path(tmp_path.name)
        result["metadata"]["final_handoff"].update(
            handoff_path=str(run_path / "final-handoff.json"),
            rendered_path=str(run_path / "final.md"),
            validation_path=str(run_path / "final-handoff.validation.json"),
        )
        result_path.write_text(json.dumps(result), encoding="utf-8")
        monkeypatch.chdir(tmp_path.parent)

        validator.validate("assess", tmp_path, result_path)

    def test_retired_change_analysis_skill_stays_readable(self, finalizer: Any, validator: Any, tmp_path: Path) -> None:
        """Keep a retired skill name readable without retaining a discoverable alias.

        A historical `change-analysis` handoff validates under that name even though the skill is now `assess`.
        """
        result_path = write_schema_v2_assess(tmp_path)
        result = read_json(result_path)
        binding, handoff_path, handoff = _bound_handoff(result)
        handoff["skill"] = "change-analysis"
        _rebind_handoff(finalizer, binding, handoff_path, handoff)
        result_path.write_text(json.dumps(result), encoding="utf-8")

        validator.validate("change-analysis", tmp_path, result_path)

    def test_historical_schema_v1_result_needs_no_final_handoff(self, validator: Any, tmp_path: Path) -> None:
        """Keep pre-migration artifacts valid while schema-v2 creation fails closed.

        A result with no `schema_version` and no `final_handoff` metadata validates as a historical artifact.
        """
        result_path = write_schema_v2_assess(tmp_path)
        result = read_json(result_path)
        result.pop("schema_version")
        result["metadata"].pop("final_handoff")
        result_path.write_text(json.dumps(result), encoding="utf-8")

        validator.validate("assess", tmp_path, result_path)
