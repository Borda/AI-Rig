"""Payload, transcript and result helpers shared by the foundry hook tests that drive one hook script through ``node``.

Deliberately a uniquely-named module rather than ``conftest``, for the reason ``_hook_env.py`` gives. Each helper takes
the hook script as its first argument; a test module binds it once with :func:`functools.partial`.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

#: Session id the header-gate hook tests run under, so the hook finds the state sentinel they plant.
CSID = "test-session-1234"


def run_hook_json(
    hook: Path, tmp_path: Path, payload: dict, *, session_id_env: str | None = CSID, tmpdir_suffix: str = ""
) -> dict:
    """Invoke ``hook`` with `payload` on stdin and return parsed stdout (or {})."""
    env = {**os.environ, "TMPDIR": f"{tmp_path}{tmpdir_suffix}"}
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    if session_id_env is not None:
        env["CLAUDE_CODE_SESSION_ID"] = session_id_env
    proc = subprocess.run(
        ["node", str(hook)], input=json.dumps(payload), capture_output=True, text=True, timeout=10, env=env
    )
    assert proc.returncode == 0, f"hook exited {proc.returncode}: {proc.stderr}"
    out = proc.stdout.strip()
    return json.loads(out) if out else {}


def call_export(hook: Path, name: str, *args: object) -> object:
    """Call one test-only export of ``hook`` in a separate Node process."""
    script = "const hook = require(process.argv[1]); process.stdout.write(JSON.stringify(hook[process.argv[2]](...JSON.parse(process.argv[3]))));"
    proc = subprocess.run(
        ["node", "-e", script, str(hook), name, json.dumps(args)],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    return json.loads(proc.stdout)


def denial_reason(result: dict) -> str | None:
    """Denial reason emitted by the hook, or None when it did not deny.

    Examples:
        >>> (denial_reason({}), denial_reason({"hookSpecificOutput": {"permissionDecision": "deny"}}))
        (None, '')
    """
    hook_output = result.get("hookSpecificOutput", {})
    if hook_output.get("permissionDecision") != "deny":
        return None
    return hook_output.get("permissionDecisionReason", "")


def is_allowed(result: dict) -> bool:
    """Check whether the hook allowed the requested operation.

    Examples:
        >>> (is_allowed({"hookSpecificOutput": {"permissionDecision": "allow"}}), is_allowed({}))
        (True, False)
    """
    try:
        return result["hookSpecificOutput"]["permissionDecision"] == "allow"
    except (KeyError, TypeError):
        return False


def stop_payload(**overrides: object) -> dict:
    """Build a Stop payload whose final message lacks the header, applying `overrides`."""
    payload: dict = {"hook_event_name": "Stop", "stop_hook_active": False, "last_assistant_message": "done"}
    payload.update(overrides)
    return payload


def write_transcript(tmp_path: Path, assistant_text: str) -> Path:
    """Write a minimal two-row JSONL transcript: a human user turn then one assistant text block."""
    rows = [
        {"type": "user", "message": {"content": [{"type": "text", "text": "go"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": assistant_text}]}},
    ]
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return transcript
