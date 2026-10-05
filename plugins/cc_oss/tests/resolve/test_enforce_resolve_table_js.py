"""Subprocess tests for ``hooks/enforce-resolve-table.js``.

The hook is a ``PreToolUse`` gate on ``AskUserQuestion`` for the oss:resolve Step 3d item picker. The regression it
guards: conflict-resolution output in the same turn pushed the findings table out of the reply, so the user selected
items blind. Contract:

* **Scoped** — only calls carrying the bulk-action question are gated, and only while the run's
  ``resolve-impl-dir-<CSID>`` sentinel names a directory holding ``action-items.jsonl``.
* **Denies a blind picker** — any pending item id missing from every Markdown table row since the last human turn.
* **Allows** — every pending id shown in a table row; non-selection questions; no pending items.
* **Fails open** — no sentinel, missing or malformed items file, unparsable payload.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[2] / "hooks" / "enforce-resolve-table.js"
CSID = "test-session-5678"

_skip_node_unavailable = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

_BULK = {
    "question": "Or choose a bulk action:",
    "header": "Bulk action",
    "multiSelect": False,
    "options": [
        {"label": "+All [req]", "description": "Implement all required items"},
        {"label": "+All [suggest]", "description": "Implement all suggested items"},
        {"label": "ALL (req + suggest)", "description": "Implement all pending items"},
        {"label": "Skip all", "description": "Skip all items, exit"},
    ],
}
_PUSH = {
    "question": "Push after implementing?",
    "header": "Push",
    "options": [{"label": "Push"}, {"label": "Don't push"}],
}

_TABLE = (
    "| # | Type | Severity | Summary |\n"
    "| -- | -- | -- | -- |\n"
    "| 1 | [report] | medium | guard keeps view |\n"
    "| 2 | [gh][suggest] | low | add test |\n"
)


@pytest.fixture(autouse=True)
def _no_flush_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the transcript-lag wait so tests expecting a denial stay fast; the wait test sets its own."""
    monkeypatch.setenv("CLAUDE_GATE_FLUSH_WAIT_MS", "0")


def _items(tmp_path: Path, *rows: dict) -> Path:
    """Write action-items.jsonl into an impl dir and point the CSID sentinel at it."""
    impl = tmp_path / "impl"
    impl.mkdir()
    (impl / "action-items.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8", newline="\n"
    )
    (tmp_path / f"resolve-impl-dir-{CSID}").write_text(f"{impl}\n", encoding="utf-8", newline="\n")
    return impl


def _transcript(tmp_path: Path, assistant_text: str) -> Path:
    """Write a transcript with one human turn followed by one assistant reply."""
    path = tmp_path / "transcript.jsonl"
    rows = [
        {"type": "user", "message": {"content": "/oss:resolve 42"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": assistant_text}]}},
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8", newline="\n")
    return path


def _run(tmp_path: Path, questions: list[dict], transcript: Path | None) -> str | None:
    """Run the hook and return its denial reason, or None when it allowed the call."""
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "AskUserQuestion",
        "tool_input": {"questions": questions},
        "transcript_path": str(transcript) if transcript else None,
    }
    env = {**os.environ, "TMPDIR": str(tmp_path), "CLAUDE_CODE_SESSION_ID": CSID}
    proc = subprocess.run(
        ["node", str(HOOK)], input=json.dumps(payload), capture_output=True, text=True, timeout=10, env=env
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout.strip()
    decision = json.loads(out)["hookSpecificOutput"] if out else {}
    return decision.get("permissionDecisionReason") if decision.get("permissionDecision") == "deny" else None


_PENDING = ({"id": 1, "type": "[report]"}, {"id": 2, "type": "[gh][suggest]"}, {"id": 3, "type": "[done]"})


@_skip_node_unavailable
class TestSelectionGate:
    def test_picker_without_table_is_denied(self, tmp_path: Path) -> None:
        """A picker after conflict-resolution chatter, with no table in the reply, is denied and names the ids.

        This is the reported regression: the reply showed only the merge-commit narration before the picker.
        """
        _items(tmp_path, *_PENDING)
        reason = _run(tmp_path, [_BULK], _transcript(tmp_path, "Resolved CHANGELOG.md. Committed 8db6a761."))
        assert reason is not None and "1, 2" in reason

    def test_picker_after_full_table_is_allowed(self, tmp_path: Path) -> None:
        """Every pending id shown as a table row lets the picker open."""
        _items(tmp_path, *_PENDING)
        assert _run(tmp_path, [_BULK], _transcript(tmp_path, "Merged items:\n\n" + _TABLE)) is None

    def test_partial_table_is_denied(self, tmp_path: Path) -> None:
        """A table missing one pending row still leaves the user partly blind, so it is denied."""
        _items(tmp_path, *_PENDING, {"id": 4, "type": "[report]"})
        reason = _run(tmp_path, [_BULK], _transcript(tmp_path, _TABLE))
        assert reason is not None and "4" in reason

    def test_table_only_in_thinking_is_denied_with_zero_visible_chars(self, tmp_path: Path) -> None:
        """A table the model wrote only in thinking is denied, and the reason reports 0 visible reply chars.

        Observed failure: each picker response held thinking + tool_use and no text block, so the user never saw the
        table; without the char count the model blamed the hook and retried the picker four times.
        """
        _items(tmp_path, *_PENDING)
        transcript = tmp_path / "transcript.jsonl"
        rows = [
            {"type": "user", "message": {"content": "ask me with questions"}},
            {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": _TABLE}]}},
        ]
        transcript.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8", newline="\n")
        reason = _run(tmp_path, [_BULK], transcript)
        assert reason is not None and "0 chars" in reason and "Thinking" in reason

    def test_table_flushed_to_transcript_after_the_hook_starts_is_allowed(self, tmp_path: Path) -> None:
        """A table that reaches the transcript shortly after the hook starts still opens the picker.

        The model prints the table and issues the picker in one assistant message; the hook can fire before that
        message's text is flushed. A real run was denied once with a full 12-row table on screen and passed on an
        identical retry, so the hook re-reads the transcript for a short while before denying.
        """
        _items(tmp_path, *_PENDING)
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(
            json.dumps({"type": "user", "message": {"content": "/oss:resolve 42"}}) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": [_BULK]},
            "transcript_path": str(transcript),
        }
        env = {
            **os.environ,
            "TMPDIR": str(tmp_path),
            "CLAUDE_CODE_SESSION_ID": CSID,
            "CLAUDE_GATE_FLUSH_WAIT_MS": "5000",
        }
        proc = subprocess.Popen(["node", str(HOOK)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(payload))
        proc.stdin.close()
        time.sleep(0.6)
        row = {"type": "assistant", "message": {"content": [{"type": "text", "text": _TABLE}]}}
        with transcript.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(row) + "\n")
        assert proc.stdout is not None
        out = proc.stdout.read()
        assert proc.wait(timeout=10) == 0
        assert out.strip() == ""

    def test_ids_mentioned_only_in_prose_are_denied(self, tmp_path: Path) -> None:
        """Item numbers in prose or a count line do not replace the table rows."""
        _items(tmp_path, *_PENDING)
        assert _run(tmp_path, [_BULK], _transcript(tmp_path, "2 pending items: #1 and #2.")) is not None


@_skip_node_unavailable
class TestPassThrough:
    def test_non_selection_question_passes(self, tmp_path: Path) -> None:
        """Push and recovery questions are not item pickers and are never gated."""
        _items(tmp_path, *_PENDING)
        assert _run(tmp_path, [_PUSH], _transcript(tmp_path, "no table")) is None

    def test_no_sentinel_passes(self, tmp_path: Path) -> None:
        """Outside a resolve run (no impl-dir sentinel) the hook stays silent."""
        assert _run(tmp_path, [_BULK], _transcript(tmp_path, "no table")) is None

    def test_no_pending_items_passes(self, tmp_path: Path) -> None:
        """With only resolved, addressed, legacy done, or info items there is nothing to select, so nothing to show."""
        _items(
            tmp_path,
            {"id": 1, "type": "[done]"},
            {"id": 2, "type": "[gh][info]"},
            {"id": 3, "type": "[gh][req]", "status": "resolved"},
            {"id": 4, "type": "[gh][suggest]", "status": "addressed"},
        )
        assert _run(tmp_path, [_BULK], _transcript(tmp_path, "no table")) is None

    def test_malformed_items_file_passes(self, tmp_path: Path) -> None:
        """A malformed items file fails open; the skill's own merge checks own that failure."""
        impl = _items(tmp_path, *_PENDING)
        (impl / "action-items.jsonl").write_text("not json\n", encoding="utf-8")
        assert _run(tmp_path, [_BULK], _transcript(tmp_path, "no table")) is None
