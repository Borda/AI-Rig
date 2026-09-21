"""Unit tests for ``hooks/report-header-table.js``, the table-format detector shared by all six ``enforce-*-header.js``
hooks (see propagate_shared.py MANIFEST — canonical here, byte-identical copies in cc_oss, cc_develop, cc_research).

Covers the three exports in isolation, independent of any single hook's
sentinel/report-dir wiring:

* ``hasHeaderTable`` — pipe-table detection (header + separator + >= MIN_TABLE_ROWS
  data rows) and the documented ``·``-separated one-line fallback.
* ``assistantTextSinceLastUserTurn`` — bounded tail-read of a JSONL transcript,
  walking back to the most recent human ``user`` turn while skipping
  ``tool_result``-only rows, non-turn rows, and sidechain (subagent) output.
* ``tableReminder`` — the ``additionalContext`` string callers attach.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parent.parent / "hooks" / "report-header-table.js"

NODE_UNAVAILABLE = shutil.which("node") is None


def _call(name: str, *args: object) -> object:
    """Call one export of report-header-table.js in a separate Node process."""
    proc = subprocess.run(
        [
            "node",
            "-e",
            "const mod = require(process.argv[1]); process.stdout.write(JSON.stringify(mod[process.argv[2]](...JSON.parse(process.argv[3]))));",
            str(MODULE),
            name,
            json.dumps(args),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    return json.loads(proc.stdout)


# ── hasHeaderTable ─────────────────────────────────────────────────────────


_skip_node_unavailable = pytest.mark.skipif(
    NODE_UNAVAILABLE,
    reason="requires node to execute the module",
)


@_skip_node_unavailable
def test_pipe_table_with_enough_rows_is_detected() -> None:
    """A `| Field | Value |` table with >= MIN_TABLE_ROWS data rows counts."""
    text = "| Field | Value |\n| --- | --- |\n| Title | x |\n| PR | #1 |\n| Date | 2026-08-08 |\n"

    assert _call("hasHeaderTable", text) is True


@_skip_node_unavailable
def test_raw_yaml_fields_without_pipes_is_not_detected() -> None:
    """The exact failure this module guards against: fields printed one per line, no table."""
    text = "Title: oss-review\nPR: #1303\nDate: 2026-08-08\n"

    assert _call("hasHeaderTable", text) is False


@_skip_node_unavailable
def test_table_below_min_rows_is_not_detected() -> None:
    """Fewer than MIN_TABLE_ROWS data rows reads as stray prose pipes, not a rendered header."""
    text = "| Field | Value |\n| --- | --- |\n| Title | x |\n"

    assert _call("hasHeaderTable", text) is False


@_skip_node_unavailable
def test_fallback_dot_separated_line_is_detected() -> None:
    """SKILL.md's documented one-line fallback (used when the report read fails) also satisfies the check."""
    text = "verdict: APPROVE · findings: 3 · file: review-report.md"

    assert _call("hasHeaderTable", text) is True


@_skip_node_unavailable
def test_empty_text_is_not_detected() -> None:
    """No text at all (unreadable transcript) is never mistaken for a printed table."""
    assert _call("hasHeaderTable", "") is False


@_skip_node_unavailable
@pytest.mark.parametrize(
    "text", ["", "| Field | Value |\n| --- | --- |\n| Title | unrelated |\n| Outcome | PASS |\n| Summary | old |"]
)
def test_delivery_rejects_absent_or_unrelated_table(tmp_path: Path, text: str) -> None:
    """File presence and another report's table cannot establish this report's delivery."""
    report = tmp_path / "report.md"
    report.write_text("---\nTitle: Current review\nOutcome: PASS\nSummary: Verified result\n---\n", encoding="utf-8")
    transcript = _write_transcript(
        tmp_path, [{"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}]
    )
    assert _call("deliveryProblem", str(report), str(transcript)) is not None


@_skip_node_unavailable
def test_delivery_accepts_bound_header(tmp_path: Path) -> None:
    """The current complete header table permits the follow-up transition."""
    report = tmp_path / "report.md"
    report.write_text("---\nTitle: Current review\nOutcome: PASS\nSummary: Verified result\n---\n", encoding="utf-8")
    text = "| Field | Value |\n| --- | --- |\n| Title | Current review |\n| Outcome | PASS |\n| Summary | Verified result |"
    transcript = _write_transcript(
        tmp_path, [{"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}]
    )
    assert _call("deliveryProblem", str(report), str(transcript)) is None


@_skip_node_unavailable
@pytest.mark.parametrize(
    ("saved", "delivered", "matches"),
    [
        pytest.param(r"C:\reports\q", "C:reportsq", False, id="path-separators-missing"),
        pytest.param("A|B", "AB", False, id="literal-pipe-missing"),
        pytest.param("A*B", "AB", False, id="literal-star-missing"),
        pytest.param("A`B", "AB", False, id="literal-backtick-missing"),
        pytest.param(r"C:\reports\q", r"C:\reports\q", True, id="literal-path-delivered"),
        pytest.param("A|B", r"A\|B", True, id="table-pipe-escaped"),
    ],
)
def test_delivery_preserves_literal_header_values(tmp_path: Path, saved: str, delivered: str, matches: bool) -> None:
    """Formatting tolerance cannot equate different paths or erase literal header characters."""
    report = tmp_path / "report.md"
    report.write_text(f"---\nTitle: Current\nOutcome: PASS\nPath: {saved}\n---\n", encoding="utf-8")
    text = f"| Field | Value |\n| --- | --- |\n| Title | Current |\n| Outcome | PASS |\n| Path | {delivered} |"
    transcript = _write_transcript(
        tmp_path, [{"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}]
    )
    problem = _call("deliveryProblem", str(report), str(transcript))
    if matches:
        assert problem is None
    else:
        assert (
            problem
            == "current report header was not delivered; print every header field as a table before following up"
        )


@_skip_node_unavailable
@pytest.mark.parametrize(
    ("delivered", "matches"),
    [
        pytest.param("AB", False, id="audit-pipe-missing"),
        pytest.param("A|B", True, id="audit-literal-pipe"),
        pytest.param(r"A\|B", True, id="audit-table-pipe-escaped"),
    ],
)
def test_audit_delivery_preserves_literal_finding(tmp_path: Path, delivered: str, matches: bool) -> None:
    """Audit findings retain literal punctuation rather than accepting a different description."""
    report = tmp_path / "summary.jsonl"
    report.write_text(json.dumps({"sev": "high", "one_line": "A|B"}) + "\n", encoding="utf-8")
    text = f"## Audit Report\nTotal: 1\n{delivered}"
    transcript = _write_transcript(
        tmp_path, [{"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}]
    )
    problem = _call("deliveryProblem", str(report), str(transcript))
    if matches:
        assert problem is None
    else:
        assert problem == "current audit findings were not delivered; emit Step 7 before following up"


@_skip_node_unavailable
@pytest.mark.parametrize(
    "skill", ["oss:review", "oss:analyse", "develop:review", "research:topic", "foundry:profile", "foundry:audit"]
)
def test_recovery_question_is_not_a_follow_up(skill: str) -> None:
    """Missing report delivery must not prevent asking how to recover the producer."""
    question = {
        "questions": [
            {"question": "The producer failed. Retry or stop?", "options": [{"label": "Retry"}, {"label": "Stop"}]}
        ]
    }
    assert _call("isWorkflowFollowUp", question, skill) is False


@_skip_node_unavailable
@pytest.mark.parametrize(
    "skill", ["oss:review", "oss:analyse", "develop:review", "research:topic", "foundry:profile", "foundry:audit"]
)
def test_unrelated_what_next_is_not_a_follow_up(skill: str) -> None:
    """A generic question cannot activate a different workflow's report gate."""
    question = {
        "questions": [
            {"question": "What next?", "header": "Recovery", "options": [{"label": "Retry"}, {"label": "Stop"}]}
        ]
    }
    assert _call("isWorkflowFollowUp", question, skill) is False


@_skip_node_unavailable
def test_raw_fields_plus_unrelated_table_do_not_prove_delivery(tmp_path: Path) -> None:
    """Header fields must occur as rows in one matching table, not elsewhere in prose."""
    report = tmp_path / "report.md"
    report.write_text("---\nTitle: Current\nOutcome: PASS\nSummary: Verified\n---\n", encoding="utf-8")
    text = "Title Current Outcome PASS Summary Verified\n| Field | Value |\n| --- | --- |\n| Title | Other |\n| Outcome | PASS |\n| Summary | Stale |"
    transcript = _write_transcript(
        tmp_path, [{"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}]
    )
    assert _call("deliveryProblem", str(report), str(transcript)) is not None


# ── assistantTextSinceLastUserTurn ─────────────────────────────────────────


def _write_transcript(tmp_path: Path, rows: list[dict]) -> Path:
    """Write transcript fixture rows as JSONL and return their path.

    Examples:
        >>> from tempfile import TemporaryDirectory
        >>> with TemporaryDirectory() as directory:
        ...     _write_transcript(Path(directory), [{"type": "user"}]).read_text().endswith("\\n")
        True
    """
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return transcript


@_skip_node_unavailable
def test_collects_assistant_text_after_last_human_turn(tmp_path: Path) -> None:
    """Text from the current turn's assistant row is returned."""
    rows = [
        {"type": "user", "message": {"content": [{"type": "text", "text": "go"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "hello"}]}},
    ]
    transcript = _write_transcript(tmp_path, rows)

    assert _call("assistantTextSinceLastUserTurn", str(transcript)) == "hello"


@_skip_node_unavailable
def test_tool_result_row_is_not_a_turn_boundary(tmp_path: Path) -> None:
    """A `user` row holding only a tool_result is the previous tool call's return value, not a new human turn."""
    rows = [
        {"type": "user", "message": {"content": [{"type": "text", "text": "go"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "before"}]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read"}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "file contents"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "after"}]}},
    ]
    transcript = _write_transcript(tmp_path, rows)

    assert _call("assistantTextSinceLastUserTurn", str(transcript)) == "before\nafter"


@_skip_node_unavailable
def test_non_turn_rows_are_skipped(tmp_path: Path) -> None:
    """Queue-operation / attachment / mode rows are not user or assistant rows and must not be mistaken for a
    boundary."""
    rows = [
        {"type": "user", "message": {"content": [{"type": "text", "text": "go"}]}},
        {"type": "queue-operation", "operation": "noop"},
        {"type": "attachment", "attachment": {}},
        {"type": "mode"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "hello"}]}},
    ]
    transcript = _write_transcript(tmp_path, rows)

    assert _call("assistantTextSinceLastUserTurn", str(transcript)) == "hello"


@_skip_node_unavailable
def test_sidechain_assistant_rows_are_excluded(tmp_path: Path) -> None:
    """Subagent output (isSidechain: true) is not the orchestrator's own reply and must not count."""
    rows = [
        {"type": "user", "message": {"content": [{"type": "text", "text": "go"}]}},
        {
            "type": "assistant",
            "isSidechain": True,
            "message": {"content": [{"type": "text", "text": "sub-agent text"}]},
        },
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "orchestrator text"}]}},
    ]
    transcript = _write_transcript(tmp_path, rows)

    assert _call("assistantTextSinceLastUserTurn", str(transcript)) == "orchestrator text"


@_skip_node_unavailable
def test_missing_transcript_path_returns_empty_string() -> None:
    """No transcript_path at all — caller treats this exactly like 'no table found', never a crash."""
    assert _call("assistantTextSinceLastUserTurn", None) == ""


@_skip_node_unavailable
def test_unreadable_transcript_path_returns_empty_string(tmp_path: Path) -> None:
    """A path that doesn't resolve to a file fails open to an empty string."""
    assert _call("assistantTextSinceLastUserTurn", str(tmp_path / "does-not-exist.jsonl")) == ""


# ── tableReminder ───────────────────────────────────────────────────────────


@_skip_node_unavailable
def test_reminder_names_the_skill_and_print_step() -> None:
    """The additionalContext text must name both the skill and its print step, so the model knows what to redo."""
    reminder = _call("tableReminder", "oss:review", "Step 5b (print report header)")

    assert "oss:review" in reminder
    assert "Step 5b (print report header)" in reminder
