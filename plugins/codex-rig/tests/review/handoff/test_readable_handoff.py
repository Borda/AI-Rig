"""Readable current handoffs preserve machine evidence and historical bytes.

Covers `render_handoff` in the standalone finalizer: the status table of terminal outcomes, the standard and remediation
tables, readable action and confidence blocks, byte-stable historical presentations, and exact caller contracts.
"""

import copy
import hashlib
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest
from _final_handoff_support import handoff_payload

FINALIZER_PATH = Path(__file__).resolve().parents[3] / "shared" / "final_handoff.py"
REMEDIATION_TABLE_HEADER = "| ID | Severity | Finding | Selected | Outcome | How resolved |"
EMPTY_SOURCE_COVERAGE = {
    "source_records_total": 0,
    "represented_source_records_total": 0,
    "omitted_source_records_total": 0,
}
# (status, outcome label, reason, selected) for one remediation row each, in table order.
DISPOSITIONS = [
    ("implemented", "Implemented", "Added boundary checks: equality and neighboring values.", "Yes"),
    ("resolved", "Verified without code changes", "Confirmed the existing guard rejects empty input.", "Closed"),
    ("rejected", "Rejected", "The caller already validates the value.", "Yes"),
    ("unresolved", "Deferred", "User deferred the migration until the next release.", "Yes"),
    ("unresolved", "Blocked", "The required runner is unavailable.", "Yes"),
    ("needs-clarification", "Needs clarification", "The accepted input range is unspecified.", "Yes"),
    ("unresolved", "Not selected", "The user selected the other items.", "No"),
]


@pytest.fixture
def standard_payload(finalizer: ModuleType) -> Callable[..., dict]:
    """Return a factory for a standard-workflow result with an explicitly owned outstanding action."""

    def build(skill: str, version: int = 10) -> dict:
        payload = handoff_payload()
        payload.update(skill=skill, presentation_version=version)
        payload["tables"] = [
            {
                "heading": "Results",
                "columns": list(finalizer.STANDARD_COLUMNS[skill]),
                "rows": [
                    {
                        "id": "CR-2",
                        "cells": ["Parser", "Fixed", "Passed", "External CI"],
                        "source_ids": ["report:CR-1", "report:CR-2"],
                    }
                ],
            }
        ]
        return payload

    return build


@pytest.fixture
def concise_remediation() -> dict:
    """Remediation result in the concise v10 layout with every row selected."""
    return _concise_remediation_payload(10)


def _concise_remediation_payload(version: int) -> dict:
    """Build a concise-layout remediation result of the given presentation version with every row selected."""
    payload = handoff_payload()
    payload["presentation_version"] = version
    payload["tables"][0].update(layout="concise", overview_only=True)
    for row in payload["tables"][0]["rows"]:
        row["selected"] = "Yes"
    return payload


def _update_checks(payload: dict, update: dict) -> None:
    """Apply one update to every verification check of a handoff payload."""
    for check in payload["verification"]:
        check.update(update)


def _remediation_payload(dispositions: list[tuple[str, str, str, str]]) -> dict:
    """Build a v10 remediation result with one concise row, detail pair and source record per disposition."""
    payload = handoff_payload()
    payload["presentation_version"] = 10
    table = payload["tables"][0]
    table.update(layout="concise", overview_only=True, rows=[], details=[])
    payload.update(source_records=[], remaining=[], next_steps=[])
    for index, (status, label, reason, selected) in enumerate(dispositions, 1):
        identity = f"F{index}"
        table["rows"].append(
            {
                "id": identity,
                "selected": selected,
                "cells": [
                    identity,
                    "medium",
                    f"Concern {index}",
                    f"report [{identity}]",
                    f"{status} — [O{index}]",
                    f"[E{index}] — owner/status: maintainer",
                ],
                "source_ids": [f"report:{identity}"],
            }
        )
        table["details"].extend(
            [{"id": f"O{index}", "text": f"{label}: {reason}"}, {"id": f"E{index}", "text": "Retained check evidence."}]
        )
        payload["source_records"].append({"id": f"report:{identity}", "evidence": "action-items.md"})
        if status in {"unresolved", "needs-clarification"}:
            payload["remaining"].append(
                {
                    "row_id": identity,
                    "item": reason,
                    "owner": "maintainer",
                    "next_action": "Resolve the recorded obligation.",
                }
            )
            payload["next_steps"].append(identity)
    payload["source_coverage"].update(
        source_records_total=len(dispositions), represented_source_records_total=len(dispositions)
    )
    return payload


class TestTerminalHandoff:
    """Terminal review outcomes render as a status table without source judgment."""

    @pytest.mark.parametrize(
        ("branch", "check_update", "detailed_review_skipped"),
        [
            pytest.param(
                "unavailable",
                {"status": "not-applicable", "evidence": "Source collection did not complete."},
                False,
                id="unavailable",
            ),
            pytest.param("closed", {}, True, id="closed"),
        ],
    )
    def test_keeps_status_table_without_source_judgment(
        self, finalizer: ModuleType, branch: str, check_update: dict, detailed_review_skipped: bool
    ) -> None:
        """A stopped review stays scannable without inventing assessed findings or approval.

        The review ended before any source assessment. The handoff must still show the outcome in a field table, say
        that findings were not assessed and no merge decision was made, and leave the payload unchanged.
        """
        payload = handoff_payload()
        payload.update(skill="code-review", branch=branch, presentation_version=11, tables=[], source_records=[])
        payload.update(source_coverage=dict(EMPTY_SOURCE_COVERAGE), remaining=[], next_steps=[])
        payload["outcome"] = {"title": "Review stopped", "summary": "The review ended before source assessment."}
        _update_checks(payload, check_update)
        original = copy.deepcopy(payload)

        rendered = finalizer.render_handoff(payload)

        assert "| Field | Value |" in rendered
        assert f"| Status | {branch} |" in rendered
        assert "| Source findings | Not assessed |" in rendered
        assert "| Merge decision | Not made |" in rendered
        assert ("| Detailed review | Skipped |" in rendered) == detailed_review_skipped
        assert "**Verification**" in rendered
        assert "**Confidence**" in rendered
        assert payload == original


class TestStandardHandoff:
    """Standard-workflow handoffs keep their reading anchor, actions and confidence readable."""

    @pytest.mark.parametrize(
        ("skill", "columns"),
        [
            pytest.param("implement", "| Surface | Outcome | Verification | Remaining limit |", id="implement"),
            pytest.param("investigate", "| Hypothesis | Evidence | Disposition | Next action |", id="investigate"),
        ],
    )
    def test_single_result_retains_standard_table(
        self, finalizer: ModuleType, standard_payload: Callable[..., dict], skill: str, columns: str
    ) -> None:
        """A single clean decision still gets the workflow's normal reading anchor.

        One row with nothing remaining and no next steps must still render the skill's standard column header and the
        row itself.
        """
        payload = standard_payload(skill, version=11)
        payload["remaining"] = []
        payload["next_steps"] = []
        payload["tables"][0]["rows"][0]["cells"] = ["Parser", "Passed", "Checks passed", "None"]

        rendered = finalizer.render_handoff(payload)

        assert columns in rendered
        assert "| Parser | Passed | Checks passed | None |" in rendered

    @pytest.mark.parametrize("skill", ["implement", "investigate"])
    def test_has_readable_actions_and_confidence(
        self, finalizer: ModuleType, standard_payload: Callable[..., dict], skill: str
    ) -> None:
        """Status, ownership, action and closure evidence render in distinct Markdown blocks.

        The outstanding item has punctuation-heavy status and action text, and confidence gains extra limits. Each must
        survive verbatim, appear once, and link to the retained result and ledger.
        """
        payload = standard_payload(skill)
        payload["remaining"][0]["item"] = "External CI — waiting; preserve this entire status."
        payload["remaining"][0]["next_action"] = "Rerun CI; retain output. Then inspect the result."
        payload["confidence"]["limits"].extend(["Separate runtime limit.", "External CI was not run."])
        original = copy.deepcopy(payload)

        rendered = finalizer.render_handoff(payload)

        steps = rendered.split("**Next steps**\n\n", 1)[1].split("\n\n**Confidence**", 1)[0]
        assert steps == (
            "1. **Owner: ci**\n\n"
            "   Status: External CI — waiting; preserve this entire status.\n\n"
            "   Action: Rerun CI; retain output. Then inspect the result."
        )
        assert "CR-2" not in steps
        assert "0.95 (fair).\n\n- Limit: Separate runtime limit." in rendered
        assert rendered.count("External CI was not run.") == 1
        assert "- Gap [unresolved]: External CI was not run. — CI is external." in rendered
        assert "- [Result](<.reports/codex/code-remediate/run/result.json>)" in rendered
        assert "- [Ledger](<.reports/codex/code-remediate/run/action-items.md>)" in rendered
        assert payload == original

    def test_multiline_confidence_stays_inside_list_items(
        self, finalizer: ModuleType, standard_payload: Callable[..., dict]
    ) -> None:
        """Paragraph breaks in each limit and gap stay inside their owning bullet.

        Limits, gaps and rationales containing blank lines must be indented as continuation paragraphs of the same list
        item rather than ending the list.
        """
        payload = standard_payload("implement")
        payload["confidence"]["limits"] = ["First limit.\n\nSecond paragraph."]
        payload["confidence"]["gaps"][0].update(
            gap="First gap.\n\nSecond gap paragraph.", rationale="First reason.\n\nSecond reason paragraph."
        )

        rendered = finalizer.render_handoff(payload)

        assert "- Limit: First limit.\n  \n  Second paragraph." in rendered
        assert (
            "- Gap [unresolved]: First gap.\n  \n  Second gap paragraph. — First reason.\n  \n  Second reason paragraph."
            in rendered
        )

    def test_numbered_actions_indent_double_digit_items(
        self, finalizer: ModuleType, standard_payload: Callable[..., dict]
    ) -> None:
        """Status and action paragraphs stay inside the tenth CommonMark list item.

        With ten outstanding actions the tenth marker is wider, so its continuation paragraphs need a four-space indent.
        """
        payload = standard_payload("implement")
        first_row = payload["tables"][0]["rows"][0]
        payload["tables"][0]["rows"] = [
            {**first_row, "id": f"row-{index}", "source_ids": [f"source-{index}"]} for index in range(10)
        ]
        payload["source_records"] = [{"id": f"source-{index}", "evidence": "CI output"} for index in range(10)]
        payload["source_coverage"].update(source_records_total=10, represented_source_records_total=10)
        payload["remaining"] = [
            {"row_id": f"row-{index}", "item": "External CI", "owner": "ci", "next_action": "Rerun CI."}
            for index in range(10)
        ]
        payload["next_steps"] = [item["row_id"] for item in payload["remaining"]]

        rendered = finalizer.render_handoff(payload)

        assert "10. **Owner: ci**\n\n    Status: External CI\n\n    Action: Rerun CI." in rendered

    def test_verification_links_existing_relative_log(
        self, finalizer: ModuleType, standard_payload: Callable[..., dict], tmp_path: Path
    ) -> None:
        """An existing gate log is linked relative to its report; prose evidence is never guessed into a path.

        One check cites a log file that exists under the report root, another cites a long prose description. Only the
        file becomes a link.
        """
        payload = standard_payload("implement")
        log = tmp_path / "gate output.log"
        log.write_text("Checks passed.\n", encoding="utf-8")
        payload["verification"][0]["evidence"] = log.name
        prose = "Complete verification details: " + "representative input retained; " * 20
        payload["verification"][1]["evidence"] = prose

        rendered = finalizer.render_handoff(payload, report_root=tmp_path)

        assert f"- lint: pass — [{log.name}](<{log.as_posix()}>)" in rendered
        assert "- tests: pass — 2 passed" in rendered
        assert f"- format: pass — {prose}" in rendered

    def test_caller_contract_preserves_exact_bytes(
        self, finalizer: ModuleType, standard_payload: Callable[..., dict]
    ) -> None:
        """An exact caller contract bypasses presentation changes after validation.

        The caller supplied a literal output; the finalizer must return those bytes untouched rather than render its own
        tables.
        """
        payload = standard_payload("implement")
        payload.update(branch="caller-contract", tables=[], source_records=[], remaining=[], next_steps=[])
        payload["source_coverage"] = dict(EMPTY_SOURCE_COVERAGE)
        payload["caller_contract"] = {"format": "literal", "evidence": "Caller requirement", "output": "raw\n\nbytes"}

        assert finalizer.render_handoff(payload) == "raw\n\nbytes"


class TestRemediationHandoff:
    """Remediation handoffs explain each disposition and keep commit and confidence semantics."""

    @pytest.mark.parametrize("item_count", [1, 7])
    def test_table_explains_each_disposition(self, finalizer: ModuleType, item_count: int) -> None:
        """Every item shows what happened and how, without conflating code changes and other closures.

        One item and seven items (implemented, verified, rejected, deferred, blocked, unclear, not selected) must each
        render a row naming the outcome and the reason, even when there is only a single item.
        """
        dispositions = DISPOSITIONS[:item_count]
        payload = _remediation_payload(dispositions)
        original = copy.deepcopy(payload)
        expected_rows = [
            f"| F{index} | medium | Concern {index} | {selected} | {label} | {reason} |"
            for index, (_, label, reason, selected) in enumerate(dispositions, 1)
        ]

        rendered = finalizer.render_handoff(payload)

        assert REMEDIATION_TABLE_HEADER in rendered
        assert [line for line in rendered.splitlines() if line.startswith("| F")] == expected_rows
        assert payload == original

    def test_resolution_reason_preserves_prose_and_externalizes_paths(
        self, finalizer: ModuleType, concise_remediation: dict
    ) -> None:
        """The change explanation keeps its prose, escapes delimiters and moves only its path below the table.

        The reason contains a pipe, a line break and a test file path. In the table the pipe is escaped and the break
        becomes `<br>`; the path appears only in the list below the table.
        """
        table = concise_remediation["tables"][0]
        table["rows"][0]["cells"][4] = "implemented — [O1]"
        table["details"] = [
            {"id": "O1", "text": "Implemented: Added left | right checks\nand coverage in `tests/test_parser.py`."}
        ]

        rendered = finalizer.render_handoff(concise_remediation)

        table_lines = "\n".join(line for line in rendered.splitlines() if line.startswith("|"))
        assert "Added left \\| right checks<br>and coverage in" in table_lines
        assert "tests/test_parser.py" not in table_lines
        assert "- CR-1: tests/test_parser.py" in rendered

    def test_preserves_overview_and_commit_semantics(self, finalizer: ModuleType, concise_remediation: dict) -> None:
        """Readable prose keeps selected findings, failed gates and commit limits.

        A failed external test gate and a blocked commit must surface in the verification and commit lines, with the
        outstanding CI action still numbered and its gap reported once.
        """
        concise_remediation["verification"][3].update(status="fail", evidence="External CI failed.")
        concise_remediation["commit_disposition"] = {
            "status": "blocked",
            "reason": "CI must pass.",
            "evidence": "External runner failed.",
        }

        rendered = finalizer.render_handoff(concise_remediation)

        assert REMEDIATION_TABLE_HEADER in rendered
        assert "tests: fail — External CI failed." in rendered
        assert "Commit: blocked — CI must pass. Evidence: External runner failed." in rendered
        assert "1. **Owner: ci**\n\n   Status: External CI\n\n   Action: Rerun CI." in rendered
        assert rendered.count("External CI was not run.") == 1

    def test_keeps_distinct_deductions_as_bullets(self, finalizer: ModuleType, concise_remediation: dict) -> None:
        """Distinct limitations stay separate, with matching closure evidence retained once.

        Three deductions are reported, two of which have gap records. The ungapped limit and each gap render as their
        own bullet, and the deferred gap carries its closure evidence.
        """
        first = "(-0.05) Native integration parity is unverified."
        second = "(-0.01) Offline documentation build unavailable."
        third = "(-0.00) Optional index was not refreshed."
        concise_remediation["confidence"] = {
            "score": 0.94,
            "band": "fair",
            "limits": [first, second, third],
            "gaps": [
                {"gap": first, "status": "unresolved", "rationale": "Native dependency absent."},
                {
                    "gap": second,
                    "status": "deferred",
                    "rationale": "Offline environment.",
                    "evidence": "Build log retained.",
                },
            ],
        }

        rendered = finalizer.render_handoff(concise_remediation)

        confidence = rendered.split("**Confidence**\n\n", 1)[1].split("\n\n**Artifact**", 1)[0]
        assert confidence == (
            f"0.94 (fair).\n\n- Limit: {third}\n"
            f"- Gap [unresolved]: {first} — Native dependency absent.\n"
            f"- Gap [deferred]: {second} — Offline environment. — Build log retained."
        )


class TestHistoricalPresentations:
    """Saved presentations keep their exact bytes or evidence checks when the current layout advances."""

    def test_v8_standard_bytes_stay_unchanged(
        self, finalizer: ModuleType, standard_payload: Callable[..., dict]
    ) -> None:
        """Retained hashes of version 8 standard output do not change when the current presentation advances."""
        rendered = finalizer.render_handoff(standard_payload("implement", 8))

        assert (
            hashlib.sha256(rendered.encode()).hexdigest()
            == "be426f1240bf2912e86e308d08d91d578c47c0bf86892d0299e8757506f8ad36"
        )

    def test_v9_remediation_bytes_stay_unchanged(self, finalizer: ModuleType) -> None:
        """Saved version 9 remediation hashes stay valid when current rows gain resolution explanations."""
        rendered = finalizer.render_handoff(_concise_remediation_payload(9))

        assert hashlib.sha256(rendered.encode()).hexdigest() == (
            "7ed520059578950cff54594b6705fdc1c5e0d733bdef3c7cf0ccb8bde07ad54b"
        )

    @pytest.mark.parametrize("skill", ["implement", "investigate"])
    def test_new_presentation_keeps_other_workflow_bytes(
        self, finalizer: ModuleType, standard_payload: Callable[..., dict], skill: str
    ) -> None:
        """A remediation table change does not alter standard workflow output.

        Version 10 and version 9 of a standard workflow must render identically, since only remediation tables changed.
        """
        assert finalizer.render_handoff(standard_payload(skill, 10)) == finalizer.render_handoff(
            standard_payload(skill, 9)
        )

    @pytest.fixture
    def historical_review(self, finalizer: ModuleType, tmp_path: Path) -> tuple[dict, Path]:
        """Historical assessed review payload and its retained report-details file under `tmp_path`."""
        payload = handoff_payload()
        payload.update(skill="code-review", branch="assessed", presentation_version=10)
        payload["outcome"] = {"title": "Review Decision", "summary": "Recommendation: needs-more-work."}
        payload["tables"] = [
            {
                "heading": "Review Snapshot",
                "columns": ["Field", "Value"],
                "rows": [{"id": "PR", "cells": ["PR", "#42"], "source_ids": ["report:CR-1"]}],
            },
            {
                "heading": "Review Findings and Merge Blocks",
                "layout": "concise",
                "overview_only": True,
                "columns": ["Finding / area", "Required change", "Evidence", "Status"],
                "rows": [
                    {
                        "id": "CR-2",
                        "title": "CI missing",
                        "cells": ["CR-2", "Run CI", "ci.log", "Required"],
                        "source_ids": ["report:CR-2"],
                    }
                ],
            },
        ]
        report = tmp_path / "review-notes.md"
        report.write_text(finalizer.render_review_report_details(payload), encoding="utf-8")
        return payload, report

    def test_assessed_review_renders_with_matching_report(
        self, finalizer: ModuleType, historical_review: tuple[dict, Path]
    ) -> None:
        """A historical review overview keeps its wording when its report details still match.

        The report-details file written from the payload is present under the report root, so rendering succeeds and
        names the outstanding CI owner and gap once.
        """
        payload, report = historical_review

        rendered = finalizer.render_handoff(payload, report_root=report.parent)

        assert "This review needs more work before it can be accepted." in rendered
        assert "1. **Owner: ci**" in rendered
        assert rendered.count("External CI was not run.") == 1
        assert "- Gap [unresolved]:" in rendered

    def test_assessed_review_rejects_changed_report(
        self, finalizer: ModuleType, historical_review: tuple[dict, Path]
    ) -> None:
        """A historical review is bound to its report: replacing the details file fails evidence validation."""
        payload, report = historical_review
        report.write_text("Evidence removed.\n", encoding="utf-8")

        with pytest.raises(finalizer.HandoffError, match="review-report-details-mismatch"):
            finalizer.render_handoff(payload, report_root=report.parent)
