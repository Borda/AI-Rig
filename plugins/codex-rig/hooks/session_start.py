#!/usr/bin/env python3
"""Emit a bounded read-only Codex Rig shim-health message at session start.

## Purpose

give an installed Codex session an actionable indication of managed role-shim health before normal work begins. It turns
the packaged doctor result into a short startup message so an operator can recognize degraded setup without opening
diagnostic files first.

## Scope

parses hook input and invokes the diagnostic surface only; it never installs, repairs, removes, or otherwise changes
shims. The doctor subprocess is bounded by input, output, and time limits, and the hook validates that it is running
from the active installed plugin root.

## Usage

the plugin hook runner executes this file with its JSON event on standard input; invoke ``python session_start.py`` only
for local diagnosis. Set ``PLUGIN_ROOT`` to the installed plugin directory when reproducing the hook locally, and
provide a ``SessionStart`` event envelope on standard input.

## Used by

the optional ``SessionStart`` hook declared by Codex Rig and the session-start acceptance tests. It is the presentation
boundary between hook lifecycle events and ``manage_role_agents.py doctor``; callers should not depend on its internal
helper functions.

## Outputs

prints a bounded JSON-compatible hook response that is informative but does not reveal private filesystem or credential
details. Healthy diagnostics produce a continue response without a system warning, while degraded or blocked checks
include a sanitized reason and the safe status command.

## Failure

malformed input, unavailable plugin state, or a diagnostic error becomes a concise health warning so session startup
remains non-blocking. The handler returns a successful process status for these expected failures, allowing the host
session to continue while directing the operator to the doctor command.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


MAX_INPUT_BYTES = 65_536
MAX_OUTPUT_BYTES = 1_048_576
MAX_REASON_CHARS = 240


def _response(message: str | None = None) -> str:
    """Encode one non-blocking SessionStart response."""
    value: dict[str, object] = {"continue": True, "suppressOutput": False}
    if message:
        value["systemMessage"] = message
    return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _input() -> dict[str, object]:
    """Read and validate the bounded SessionStart envelope."""
    payload = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(payload) > MAX_INPUT_BYTES:
        raise ValueError("hook input is oversized")
    value = json.loads(payload)
    if not isinstance(value, dict) or value.get("hook_event_name") != "SessionStart":
        raise ValueError("SessionStart hook input required")
    return value


def _plugin_root() -> Path:
    """Bind the hook to the exact installed plugin root supplied by Codex."""
    configured = os.environ.get("PLUGIN_ROOT")
    if not configured:
        raise ValueError("PLUGIN_ROOT is unavailable")
    root = Path(configured).resolve(strict=True)
    script = Path(__file__).resolve(strict=True)
    if script != root / "hooks" / "session_start.py":
        raise ValueError("hook is outside the active plugin root")
    return root


def _health_message(result: dict[str, object], classification: str) -> str:
    """Render one bounded failed check without suggesting an unsafe repair."""
    checks = result.get("checks")
    reason = "details unavailable"
    if isinstance(checks, dict):
        preferred = "blocked" if classification == "blocked" else "degraded"
        for name in ("python", "platform", "filesystem", "executables", "package", "active_package"):
            value = checks.get(name)
            if isinstance(value, dict) and value.get("status") == preferred:
                detail = value.get("detail")
                if isinstance(detail, str) and detail:
                    reason = f"{name}: {detail}"
                    break
    if reason == "details unavailable":
        # Unknown-host refusal paths can carry only a top-level detail. Surface it
        # instead of the useless generic placeholder.
        top_detail = result.get("detail")
        if isinstance(top_detail, str) and top_detail:
            reason = top_detail
    reason = _bounded_reason(reason.replace("\n", " "))
    return (
        f"Codex Rig shim health: {classification} — {reason}. No files changed. "
        "Run $codex-rig:agent-shims status for all checks and safe next steps."
    )


def _bounded_reason(reason: str) -> str:
    """Keep a bounded diagnostic while retaining its final observed value."""
    if len(reason) <= MAX_REASON_CHARS:
        return reason
    prefix, separator, observed = reason.rpartition(", observed ")
    if not separator:
        return reason[:MAX_REASON_CHARS]
    suffix = f"{separator}{observed}"
    head_length = MAX_REASON_CHARS - len(suffix) - 1
    if head_length <= 0:
        return suffix[-MAX_REASON_CHARS:]
    return f"{prefix[:head_length]}…{suffix}"


def main() -> int:
    """Run the packaged doctor and surface only actionable degraded health."""
    try:
        _input()
        root = _plugin_root()
        manager = root / "scripts" / "manage_role_agents.py"
        completed = subprocess.run(
            [sys.executable, str(manager), "doctor"],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=25,
        )
        if len(completed.stdout) > MAX_OUTPUT_BYTES or len(completed.stderr) > MAX_OUTPUT_BYTES:
            raise ValueError("doctor output is oversized")
        result = json.loads(completed.stdout)
        classification = result.get("classification") if isinstance(result, dict) else None
        if completed.returncode != 0 or classification not in {"healthy", "degraded", "blocked"}:
            raise ValueError("doctor did not return a valid diagnostic")
        message = None
        if classification != "healthy":
            message = _health_message(result, classification)
        print(_response(message))
        return 0
    except (OSError, subprocess.TimeoutExpired, ValueError, json.JSONDecodeError) as error:
        print(_response(f"Codex Rig shim health check unavailable: {error}. Run $codex-rig:agent-shims doctor."))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
