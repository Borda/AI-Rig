"""Readable current handoffs preserve machine evidence and historical bytes."""

import copy
import hashlib
from pathlib import Path

import pytest
from test_final_handoff import _handoff_payload, _load_finalizer


def _standard_payload(skill: str, version: int = 10) -> dict:
    """Build one standard result with an explicitly owned outstanding action."""
    payload = _handoff_payload()
    finalizer = _load_finalizer()
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


@pytest.mark.parametrize("skill", ["implement", "investigate"])
def test_current_standard_handoff_has_readable_actions_and_confidence(skill: str) -> None:
    """Keep status, ownership, action and closure evidence in distinct Markdown blocks."""
    payload = _standard_payload(skill)
    payload["remaining"][0]["item"] = "External CI — waiting; preserve this entire status."
    payload["remaining"][0]["next_action"] = "Rerun CI; retain output. Then inspect the result."
    payload["confidence"]["limits"].extend(["Separate runtime limit.", "External CI was not run."])
    original = copy.deepcopy(payload)

    rendered = _load_finalizer().render_handoff(payload)

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


def test_historical_v8_standard_bytes_stay_unchanged() -> None:
    """Retained hashes must not change when the current presentation advances."""
    rendered = _load_finalizer().render_handoff(_standard_payload("implement", 8))
    assert (
        hashlib.sha256(rendered.encode()).hexdigest()
        == "be426f1240bf2912e86e308d08d91d578c47c0bf86892d0299e8757506f8ad36"
    )


def test_historical_v9_remediation_bytes_stay_unchanged() -> None:
    """Keep saved remediation hashes valid when current rows gain resolution explanations."""
    payload = _handoff_payload()
    payload["presentation_version"] = 9
    payload["tables"][0].update(layout="concise", overview_only=True)
    for row in payload["tables"][0]["rows"]:
        row["selected"] = "Yes"
    rendered = _load_finalizer().render_handoff(payload)
    assert hashlib.sha256(rendered.encode()).hexdigest() == (
        "7ed520059578950cff54594b6705fdc1c5e0d733bdef3c7cf0ccb8bde07ad54b"
    )


@pytest.mark.parametrize("skill", ["implement", "investigate"])
def test_new_presentation_keeps_other_workflow_bytes(skill: str) -> None:
    """A remediation table change must not alter standard workflow output."""
    finalizer = _load_finalizer()
    assert finalizer.render_handoff(_standard_payload(skill, 10)) == finalizer.render_handoff(
        _standard_payload(skill, 9)
    )


@pytest.mark.parametrize("item_count", [1, 7])
def test_remediation_table_explains_each_disposition_even_for_one_item(item_count: int) -> None:
    """Show what and how for every item without conflating code changes and other closures."""
    payload = _handoff_payload()
    payload["presentation_version"] = 10
    dispositions = [
        ("implemented", "Implemented", "Added boundary checks: equality and neighboring values.", "Yes"),
        ("resolved", "Verified without code changes", "Confirmed the existing guard rejects empty input.", "Closed"),
        ("rejected", "Rejected", "The caller already validates the value.", "Yes"),
        ("unresolved", "Deferred", "User deferred the migration until the next release.", "Yes"),
        ("unresolved", "Blocked", "The required runner is unavailable.", "Yes"),
        ("needs-clarification", "Needs clarification", "The accepted input range is unspecified.", "Yes"),
        ("unresolved", "Not selected", "The user selected the other items.", "No"),
    ][:item_count]
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
    payload["source_coverage"].update(source_records_total=item_count, represented_source_records_total=item_count)
    original = copy.deepcopy(payload)

    rendered = _load_finalizer().render_handoff(payload)

    assert "| ID | Severity | Finding | Selected | Outcome | How resolved |" in rendered
    for index, (_, label, reason, selected) in enumerate(dispositions, 1):
        assert f"| F{index} | medium | Concern {index} | {selected} | {label} | {reason} |" in rendered
    assert len([line for line in rendered.splitlines() if line.startswith("| F")]) == item_count
    assert payload == original


def test_resolution_reason_preserves_prose_and_externalizes_paths() -> None:
    """Retain the change explanation, escaping delimiters and moving only its path below the table."""
    payload = _handoff_payload()
    payload["presentation_version"] = 10
    table = payload["tables"][0]
    table.update(layout="concise", overview_only=True)
    for row in table["rows"]:
        row["selected"] = "Yes"
    table["rows"][0]["cells"][4] = "implemented — [O1]"
    table["details"] = [
        {"id": "O1", "text": "Implemented: Added left | right checks\nand coverage in `tests/test_parser.py`."}
    ]

    rendered = _load_finalizer().render_handoff(payload)

    table_lines = "\n".join(line for line in rendered.splitlines() if line.startswith("|"))
    assert "Added left \\| right checks<br>and coverage in" in table_lines
    assert "tests/test_parser.py" not in table_lines
    assert "- CR-1: tests/test_parser.py" in rendered


def test_current_multiline_confidence_stays_inside_list_items() -> None:
    """Keep paragraph breaks in each limit and gap inside its owning bullet."""
    payload = _standard_payload("implement")
    payload["confidence"]["limits"] = ["First limit.\n\nSecond paragraph."]
    payload["confidence"]["gaps"][0].update(
        gap="First gap.\n\nSecond gap paragraph.",
        rationale="First reason.\n\nSecond reason paragraph.",
    )
    rendered = _load_finalizer().render_handoff(payload)
    assert "- Limit: First limit.\n  \n  Second paragraph." in rendered
    assert (
        "- Gap [unresolved]: First gap.\n  \n  Second gap paragraph. — First reason.\n  \n  Second reason paragraph."
        in rendered
    )


def test_current_numbered_actions_indent_double_digit_items() -> None:
    """Keep status and action paragraphs inside the tenth CommonMark list item."""
    payload = _standard_payload("implement")
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
    rendered = _load_finalizer().render_handoff(payload)
    assert "10. **Owner: ci**\n\n    Status: External CI\n\n    Action: Rerun CI." in rendered


def test_current_remediation_preserves_overview_and_commit_semantics() -> None:
    """Readable prose must keep selected findings, failed gates and commit limits."""
    payload = _handoff_payload()
    payload["presentation_version"] = 10
    payload["tables"][0].update(layout="concise", overview_only=True)
    for row in payload["tables"][0]["rows"]:
        row["selected"] = "Yes"
    payload["verification"][3].update(status="fail", evidence="External CI failed.")
    payload["commit_disposition"] = {
        "status": "blocked",
        "reason": "CI must pass.",
        "evidence": "External runner failed.",
    }
    rendered = _load_finalizer().render_handoff(payload)
    assert "| ID | Severity | Finding | Selected | Outcome | How resolved |" in rendered
    assert "tests: fail — External CI failed." in rendered
    assert "Commit: blocked — CI must pass. Evidence: External runner failed." in rendered
    assert "1. **Owner: ci**\n\n   Status: External CI\n\n   Action: Rerun CI." in rendered
    assert rendered.count("External CI was not run.") == 1


def test_current_remediation_keeps_distinct_deductions_as_bullets() -> None:
    """Distinct limitations remain separate, with matching closure evidence retained once."""
    payload = _handoff_payload()
    payload["presentation_version"] = 10
    payload["tables"][0].update(layout="concise", overview_only=True)
    for row in payload["tables"][0]["rows"]:
        row["selected"] = "Yes"
    first = "(-0.05) Native integration parity is unverified."
    second = "(-0.01) Offline documentation build unavailable."
    third = "(-0.00) Optional index was not refreshed."
    payload["confidence"] = {
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
    rendered = _load_finalizer().render_handoff(payload)
    confidence = rendered.split("**Confidence**\n\n", 1)[1].split("\n\n**Artifact**", 1)[0]
    assert confidence == (
        f"0.94 (fair).\n\n- Limit: {third}\n"
        f"- Gap [unresolved]: {first} — Native dependency absent.\n"
        f"- Gap [deferred]: {second} — Offline environment. — Build log retained."
    )


def test_current_verification_links_existing_relative_log(tmp_path: Path) -> None:
    """Resolve an existing gate log relative to its report without guessing prose paths."""
    payload = _standard_payload("implement")
    log = tmp_path / "gate output.log"
    log.write_text("Checks passed.\n", encoding="utf-8")
    payload["verification"][0]["evidence"] = log.name
    prose = "Complete verification details: " + "representative input retained; " * 20
    payload["verification"][1]["evidence"] = prose
    rendered = _load_finalizer().render_handoff(payload, report_root=tmp_path)
    assert f"- lint: pass — [{log.name}](<{log.as_posix()}>)" in rendered
    assert "- tests: pass — 2 passed" in rendered
    assert f"- format: pass — {prose}" in rendered


def test_current_assessed_review_retains_report_binding(tmp_path: Path) -> None:
    """Review overviews retain evidence validation and the current readable sections."""
    finalizer = _load_finalizer()
    payload = _handoff_payload()
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
    rendered = finalizer.render_handoff(payload, report_root=tmp_path)
    assert "This review needs more work before it can be accepted." in rendered
    assert "1. **Owner: ci**" in rendered
    assert rendered.count("External CI was not run.") == 1
    assert "- Gap [unresolved]:" in rendered
    report.write_text("Evidence removed.\n", encoding="utf-8")
    with pytest.raises(finalizer.HandoffError, match="review-report-details-mismatch"):
        finalizer.render_handoff(payload, report_root=tmp_path)


def test_current_caller_contract_preserves_exact_bytes() -> None:
    """An exact caller contract bypasses presentation changes after validation."""
    payload = _standard_payload("implement")
    payload.update(branch="caller-contract", tables=[], source_records=[], remaining=[], next_steps=[])
    payload["source_coverage"] = {
        "source_records_total": 0,
        "represented_source_records_total": 0,
        "omitted_source_records_total": 0,
    }
    payload["caller_contract"] = {"format": "literal", "evidence": "Caller requirement", "output": "raw\n\nbytes"}
    assert _load_finalizer().render_handoff(payload) == "raw\n\nbytes"
