"""Payload builders and a Node runner shared by the report-header hook tests.

The review and analyse header hooks are driven with the same Claude Code hook payloads (a ``PreToolUse`` question and a
``Stop`` event) and expose test-only exports that a separate Node process calls.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

#: A delivered report header: a two-column table with Title, PR and Date rows.
DELIVERED = "| Field | Value |\n| --- | --- |\n| Title | x |\n| PR | #1 |\n| Date | y |\n"

#: Report freshness window the hooks use, in seconds.
TWO_HOURS_S = 2 * 60 * 60


def ask_payload(header: str, **overrides: object) -> dict:
    """Build a ``PreToolUse`` AskUserQuestion payload with the given question ``header``, applying ``overrides``."""
    payload: dict = {
        "hook_event_name": "PreToolUse",
        "tool_name": "AskUserQuestion",
        "tool_input": {"questions": [{"question": "What next?", "header": header}]},
    }
    payload.update(overrides)
    return payload


def stop_payload(**overrides: object) -> dict:
    """Build a Stop payload whose final message lacks the header, applying ``overrides``."""
    payload: dict = {"hook_event_name": "Stop", "stop_hook_active": False, "last_assistant_message": "done"}
    payload.update(overrides)
    return payload


def denial_reason(result: dict) -> str | None:
    """Return the denial reason emitted by the hook, or None when it did not deny."""
    hook_output = result.get("hookSpecificOutput", {})
    if hook_output.get("permissionDecision") != "deny":
        return None
    return hook_output.get("permissionDecisionReason", "")


def call_export(hook: Path, name: str, *args: object) -> object:
    """Call one test-only export of the ``hook`` script in a separate Node process."""
    proc = subprocess.run(
        [
            "node",
            "-e",
            "const hook = require(process.argv[1]); process.stdout.write(JSON.stringify(hook[process.argv[2]](...JSON.parse(process.argv[3]))));",
            str(hook),
            name,
            json.dumps(args),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    return json.loads(proc.stdout)


def write_transcript(tmp_path: Path, assistant_text: str) -> Path:
    """Write a minimal two-row JSONL transcript: a user turn then one assistant text block.

    Examples:
        >>> tmp_path = getfixture("tmp_path")
        >>> path = write_transcript(tmp_path, "done")
        >>> len(path.read_text().splitlines())
        2
    """
    rows = [
        {"type": "user", "message": {"content": [{"type": "text", "text": "go"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": assistant_text}]}},
    ]
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return transcript
