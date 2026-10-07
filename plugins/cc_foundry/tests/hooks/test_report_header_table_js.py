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
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parent.parent.parent / "hooks" / "report-header-table.js"

NODE_UNAVAILABLE = shutil.which("node") is None


@pytest.fixture(autouse=True)
def _no_flush_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the PreToolUse transcript-lag wait so tests expecting a denial stay fast; wait tests set their own."""
    monkeypatch.setenv("CLAUDE_GATE_FLUSH_WAIT_MS", "0")


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
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(
            "| Field | Value |\n| --- | --- |\n| Title | x |\n| PR | #1 |\n| Date | 2026-08-08 |\n",
            True,
            id="pipe-table-with-enough-rows",
        ),
        pytest.param("Title: oss-review\nPR: #1303\nDate: 2026-08-08\n", False, id="raw-yaml-fields-without-pipes"),
        pytest.param("| Field | Value |\n| --- | --- |\n| Title | x |\n", False, id="table-below-min-rows"),
        pytest.param("verdict: APPROVE · findings: 3 · file: review-report.md", True, id="fallback-dot-separated-line"),
        pytest.param("", False, id="empty-text"),
    ],
)
def test_has_header_table_detection(text: str, expected: bool) -> None:
    """A rendered header table, or the documented one-line fallback, is detected; anything else is not.

    Scenario: a `| Field | Value |` table with >= MIN_TABLE_ROWS data rows counts; SKILL.md's `·`-separated one-line
    fallback (used when the report read fails) also counts; raw fields printed one per line with no table (the exact
    failure this module guards against), fewer than MIN_TABLE_ROWS data rows (stray prose pipes) and empty text (an
    unreadable transcript) are never mistaken for a printed table.
    """
    assert _call("hasHeaderTable", text) is expected


@_skip_node_unavailable
@pytest.mark.parametrize(
    ("report_text", "text"),
    [
        pytest.param(
            "---\nTitle: Current review\nOutcome: PASS\nSummary: Verified result\n---\n", "", id="absent-table"
        ),
        pytest.param(
            "---\nTitle: Current review\nOutcome: PASS\nSummary: Verified result\n---\n",
            "| Field | Value |\n| --- | --- |\n| Title | unrelated |\n| Outcome | PASS |\n| Summary | old |",
            id="unrelated-table",
        ),
        pytest.param(
            "---\nTitle: Current\nOutcome: PASS\nSummary: Verified\n---\n",
            "Title Current Outcome PASS Summary Verified\n| Field | Value |\n| --- | --- |\n| Title | Other |\n| Outcome | PASS |\n| Summary | Stale |",
            id="raw-fields-plus-unrelated-table",
        ),
    ],
)
def test_delivery_rejects_absent_or_unrelated_table(tmp_path: Path, report_text: str, text: str) -> None:
    """File presence and another report's table cannot establish this report's delivery.

    Scenario: an absent table, a table of another report, and header fields that occur as prose beside an unrelated
    table all fail, because header fields must occur as rows in one matching table.
    """
    report = tmp_path / "report.md"
    report.write_text(report_text, encoding="utf-8")
    transcript = _write_transcript(
        tmp_path, [{"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}]
    )
    assert _call("deliveryProblem", str(report), str(transcript)) is not None


@_skip_node_unavailable
def test_delivery_problem_reports_zero_visible_chars_for_thinking_only_report(tmp_path: Path) -> None:
    """A header table written only in thinking is rejected, and the reason reports 0 visible reply chars.

    Observed in a resolve run: the model "printed" its table inside reasoning, got denied four times, and blamed the
    hook. The char count plus the thinking note tells it the problem is its own reply, not the gate.
    """
    report = tmp_path / "report.md"
    report.write_text("---\nTitle: Current review\nOutcome: PASS\nSummary: Verified result\n---\n", encoding="utf-8")
    table = "| Field | Value |\n| --- | --- |\n| Title | Current review |\n| Outcome | PASS |\n| Summary | Verified result |"
    transcript = _write_transcript(
        tmp_path, [{"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": table}]}}]
    )
    problem = _call("deliveryProblem", str(report), str(transcript))
    assert isinstance(problem, str)
    assert "0 chars" in problem
    assert "thinking" in problem


_HEADER_REPORT = "---\nTitle: Current review\nOutcome: PASS\nSummary: Verified result\n---\n"
_HEADER_TABLE = (
    "| Field | Value |\n| --- | --- |\n| Title | Current review |\n| Outcome | PASS |\n| Summary | Verified result |"
)


def _delivery_in_subprocess(report: Path, transcript: Path, *extra: object, wait_ms: str) -> subprocess.Popen:
    """Start deliveryProblem in Node with the given flush wait; the caller writes the transcript while it polls."""
    return subprocess.Popen(
        [
            "node",
            "-e",
            "const mod = require(process.argv[1]); process.stdout.write(JSON.stringify(mod.deliveryProblem(...JSON.parse(process.argv[2]))));",
            str(MODULE),
            json.dumps([str(report), str(transcript), *extra]),
        ],
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, "CLAUDE_GATE_FLUSH_WAIT_MS": wait_ms},
    )


@_skip_node_unavailable
def test_delivery_waits_for_table_written_after_the_hook_starts(tmp_path: Path) -> None:
    """A table that reaches the transcript shortly after the PreToolUse hook starts is still found.

    The picker or follow-up question is issued in the same assistant message as the table, and that message's text is
    flushed to the transcript a moment later. Two real runs were denied once despite a full visible table and passed on
    an identical retry with no new text.
    """
    report = tmp_path / "report.md"
    report.write_text(_HEADER_REPORT, encoding="utf-8")
    transcript = _write_transcript(tmp_path, [{"type": "user", "message": {"content": "review it"}}])
    proc = _delivery_in_subprocess(report, transcript, wait_ms="5000")
    time.sleep(0.6)
    row = {"type": "assistant", "message": {"content": [{"type": "text", "text": _HEADER_TABLE}]}}
    with transcript.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(row) + "\n")
    out, _ = proc.communicate(timeout=10)
    assert json.loads(out) is None


@_skip_node_unavailable
def test_delivery_does_not_wait_when_stop_payload_carries_the_message(tmp_path: Path) -> None:
    """A Stop payload already holds the final message, so a missing table there is denied without polling."""
    report = tmp_path / "report.md"
    report.write_text(_HEADER_REPORT, encoding="utf-8")
    transcript = _write_transcript(tmp_path, [{"type": "user", "message": {"content": "review it"}}])
    started = time.monotonic()
    proc = _delivery_in_subprocess(report, transcript, "no table here", wait_ms="5000")
    out, _ = proc.communicate(timeout=10)
    assert isinstance(json.loads(out), str)
    assert time.monotonic() - started < 3


@_skip_node_unavailable
def test_delivery_gives_up_after_the_flush_wait(tmp_path: Path) -> None:
    """A table that never arrives is still denied once the wait elapses, so the gate cannot be waited out."""
    report = tmp_path / "report.md"
    report.write_text(_HEADER_REPORT, encoding="utf-8")
    transcript = _write_transcript(tmp_path, [{"type": "user", "message": {"content": "review it"}}])
    proc = _delivery_in_subprocess(report, transcript, wait_ms="400")
    out, _ = proc.communicate(timeout=10)
    problem = json.loads(out)
    assert isinstance(problem, str)
    assert "0 chars" in problem


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
        assert isinstance(problem, str)
        assert problem.startswith(
            "current report header was not delivered; print every header field as a table before following up"
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
        assert isinstance(problem, str)
        assert problem.startswith("current audit findings were not delivered; emit Step 7 before following up")


@_skip_node_unavailable
@pytest.mark.parametrize(
    "skill", ["oss:review", "oss:analyse", "develop:review", "research:topic", "foundry:profile", "foundry:audit"]
)
@pytest.mark.parametrize(
    "question",
    [
        pytest.param(
            {
                "questions": [
                    {
                        "question": "The producer failed. Retry or stop?",
                        "options": [{"label": "Retry"}, {"label": "Stop"}],
                    }
                ]
            },
            id="recovery-question",
        ),
        pytest.param(
            {
                "questions": [
                    {"question": "What next?", "header": "Recovery", "options": [{"label": "Retry"}, {"label": "Stop"}]}
                ]
            },
            id="unrelated-what-next",
        ),
    ],
)
def test_question_is_not_a_workflow_follow_up(question: dict, skill: str) -> None:
    """Neither a recovery question nor a generic one activates a workflow's report gate.

    Scenario: missing report delivery must not prevent asking how to recover the producer, and a generic "What next?"
    cannot activate a different workflow's report gate.
    """
    assert _call("isWorkflowFollowUp", question, skill) is False


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
@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        pytest.param(
            [
                {"type": "user", "message": {"content": [{"type": "text", "text": "go"}]}},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "hello"}]}},
            ],
            "hello",
            id="current-turn-assistant-text",
        ),
        pytest.param(
            [
                {"type": "user", "message": {"content": [{"type": "text", "text": "go"}]}},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "before"}]}},
                {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read"}]}},
                {"type": "user", "message": {"content": [{"type": "tool_result", "content": "file contents"}]}},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "after"}]}},
            ],
            "before\nafter",
            id="tool-result-row-is-not-a-turn-boundary",
        ),
        pytest.param(
            [
                {"type": "user", "message": {"content": [{"type": "text", "text": "go"}]}},
                {"type": "queue-operation", "operation": "noop"},
                {"type": "attachment", "attachment": {}},
                {"type": "mode"},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "hello"}]}},
            ],
            "hello",
            id="non-turn-rows-are-skipped",
        ),
        pytest.param(
            [
                {"type": "user", "message": {"content": [{"type": "text", "text": "go"}]}},
                {
                    "type": "assistant",
                    "isSidechain": True,
                    "message": {"content": [{"type": "text", "text": "sub-agent text"}]},
                },
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "orchestrator text"}]}},
            ],
            "orchestrator text",
            id="sidechain-assistant-rows-are-excluded",
        ),
    ],
)
def test_assistant_text_since_last_human_turn(tmp_path: Path, rows: list[dict], expected: str) -> None:
    """The text of the orchestrator's own assistant rows since the last human turn is returned.

    Scenario: the current turn's assistant row is returned; a `user` row holding only a tool_result is the previous tool
    call's return value, not a new human turn; queue-operation / attachment / mode rows are not user or assistant rows
    and are not mistaken for a boundary; subagent output (isSidechain: true) is not the orchestrator's own reply and
    does not count.
    """
    transcript = _write_transcript(tmp_path, rows)

    assert _call("assistantTextSinceLastUserTurn", str(transcript)) == expected


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


# ── stopBlockReason ────────────────────────────────────────────────────────

_HEADER = "---\nTitle: Current review\nOutcome: PASS\nPath: .reports/x/report.md\n---\n"
_TABLE = (
    "| Field | Value |\n| --- | --- |\n| Title | Current review |\n| Outcome | PASS |\n| Path | .reports/x/report.md |"
)


def _stop_payload(**overrides: object) -> dict:
    """Build a Stop payload with no transcript and an empty final message, applying `overrides`."""
    payload: dict = {"hook_event_name": "Stop", "stop_hook_active": False, "last_assistant_message": ""}
    payload.update(overrides)
    return payload


@pytest.fixture(name="stop_run")
def _stop_run(tmp_path: Path) -> tuple[Path, Path]:
    """Stage a workflow sentinel and its written report."""
    sentinel = tmp_path / "wf-report-dir-test-session-1234"
    sentinel.write_text("x\n", encoding="utf-8")
    report = tmp_path / "report.md"
    report.write_text(_HEADER, encoding="utf-8")
    return sentinel, report


@_skip_node_unavailable
def test_stop_blocks_undelivered_report_once(stop_run: tuple[Path, Path]) -> None:
    """A turn ending without the header is kept going once; the same report never re-blocks."""
    sentinel, report = stop_run

    reason = _call("stopBlockReason", str(sentinel), str(report), _stop_payload(), "oss:review")

    assert isinstance(reason, str)
    assert "oss:review" in reason
    assert "Path" in reason
    assert _call("stopBlockReason", str(sentinel), str(report), _stop_payload(), "oss:review") is None


@_skip_node_unavailable
def test_stop_rechecks_rewritten_report(stop_run: tuple[Path, Path]) -> None:
    """A delivered report that is later rewritten must be delivered again."""
    sentinel, report = stop_run
    assert (
        _call("stopBlockReason", str(sentinel), str(report), _stop_payload(last_assistant_message=_TABLE), "x") is None
    )

    report.write_text(_HEADER.replace("PASS", "NEEDS_WORK"), encoding="utf-8")

    assert _call("stopBlockReason", str(sentinel), str(report), _stop_payload(), "x") is not None


@_skip_node_unavailable
@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"last_assistant_message": _TABLE}, id="table-in-final-message-without-transcript"),
        pytest.param({"stop_hook_active": True}, id="forced-continuation"),
    ],
)
def test_stop_does_not_block(stop_run: tuple[Path, Path], overrides: dict) -> None:
    """A turn that delivered the table, or that a Stop hook already forced on, is never blocked.

    Scenario: the Stop payload's final message counts as delivery even when the transcript lags; `stop_hook_active`
    means a Stop hook already forced this turn on, so never loop.
    """
    sentinel, report = stop_run

    assert _call("stopBlockReason", str(sentinel), str(report), _stop_payload(**overrides), "x") is None


@_skip_node_unavailable
@pytest.mark.parametrize("content", [None, ""])
def test_stop_ignores_unwritten_report(stop_run: tuple[Path, Path], content: str | None) -> None:
    """Missing or empty report means the producer has not finished — nothing to deliver yet."""
    sentinel, report = stop_run
    report.unlink()
    if content is not None:
        report.write_text(content, encoding="utf-8")

    assert _call("stopBlockReason", str(sentinel), str(report), _stop_payload(), "x") is None


@_skip_node_unavailable
def test_stop_audit_reason_names_findings_not_header(tmp_path: Path) -> None:
    """Audit delivers a findings aggregate, so its Stop reason must not ask for a header table."""
    sentinel = tmp_path / "run-dir"
    sentinel.write_text("x\n", encoding="utf-8")
    summary = tmp_path / "summary.jsonl"
    summary.write_text(json.dumps({"sev": "high", "one_line": "broken ref"}) + "\n", encoding="utf-8")

    reason = _call("stopBlockReason", str(sentinel), str(summary), _stop_payload(), "foundry:audit")

    assert "Audit Report" in reason
    assert "Field | Value" not in reason


@_skip_node_unavailable
def test_delivered_marker_keeps_session_token_terminal(tmp_path: Path) -> None:
    """The marker name ends with the sentinel's own CSID-suffixed name, beside it."""
    marker = Path(_call("deliveredMarkerPath", str(tmp_path / "oss-review-report-dir-abc")))

    assert marker.parent == tmp_path
    assert marker.name == "report-delivered-oss-review-report-dir-abc"
