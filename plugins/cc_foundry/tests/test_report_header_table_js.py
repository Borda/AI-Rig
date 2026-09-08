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
