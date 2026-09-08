"""Subprocess tests for ``hooks/enforce-audit-header.js``.

The hook is a ``PreToolUse`` gate on ``AskUserQuestion``. Its contract:

* **Scoped to in-flight audits** — it acts only when the audit-state sentinel
  ``${TMPDIR:-/tmp}/audit-state-<CSID>/run-dir`` exists; without it every
  ``AskUserQuestion`` passes through untouched (empty stdout, exit 0).
* **Scoped to the follow-up gate** — recognised by the verbatim fixed option
  labels SKILL.md mandates (``Fix auto-fixable (Recommended)`` / ``Fix ALL``).
  Other questions legitimately asked before findings are aggregated — the
  ``! BREAKING`` acknowledgment, the unsupported-flag prompt — pass through.
* **Denies a missing aggregate** — sentinel present but ``$RUN_DIR/summary.jsonl``
  absent or empty means the findings were not aggregated and classified, so
  the gate is denied with an actionable reason.
* **Resolves the relative run dir** — ``make_run_dir.py`` is called with the
  relative base ``.reports/audit``, so the sentinel normally holds a relative
  path that only resolves against the payload's ``cwd``.
* **Fails open** — stale sentinel, implausible sentinel content, vanished run
  dir, unparsable payload: every can't-tell case allows the call.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "enforce-audit-header.js"

CSID = "test-session-1234"
STATE_DIR_NAME = f"audit-state-{CSID}"
RUN_DIR_REL = ".reports/audit/2026-08-04T10-00-00Z"
FOUR_HOURS_S = 4 * 60 * 60

NODE_UNAVAILABLE = shutil.which("node") is None


def _gate_payload(**overrides: object) -> dict:
    """Build a PreToolUse payload for audit's follow-up gate, applying `overrides`.

    Examples:
        >>> _gate_payload(cwd="/work")["cwd"]
        '/work'
    """
    payload: dict = {
        "hook_event_name": "PreToolUse",
        "tool_name": "AskUserQuestion",
        "tool_input": {
            "questions": [
                {
                    "question": "2 critical, 4 high, 3 medium, 1 low. What next?",
                    "options": [
                        {"label": "Fix auto-fixable (Recommended)", "description": "auto-fix all severities"},
                        {"label": "Fix ALL", "description": "including systemic items"},
                        {"label": "Skip", "description": "report only"},
                    ],
                }
            ]
        },
    }
    payload.update(overrides)
    return payload


def _other_question_payload(label: str, description: str) -> dict:
    """Build a payload for a non-gate audit question (breaking ack, unknown flag).

    Examples:
        >>> _other_question_payload("Skip", "report only")["tool_input"]["questions"][0]["options"][0]["label"]
        'Skip'
    """
    return _gate_payload(
        tool_input={
            "questions": [{"question": "Acknowledge?", "options": [{"label": label, "description": description}]}]
        }
    )


def _run(tmp_path: Path, payload: dict, *, session_id_env: str | None = CSID, tmpdir_suffix: str = "") -> dict:
    """Invoke the hook with `payload` on stdin and return parsed stdout (or {})."""
    env = {**os.environ, "TMPDIR": f"{tmp_path}{tmpdir_suffix}"}
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    if session_id_env is not None:
        env["CLAUDE_CODE_SESSION_ID"] = session_id_env
    proc = subprocess.run(
        ["node", str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )
    assert proc.returncode == 0, f"hook exited {proc.returncode}: {proc.stderr}"
    out = proc.stdout.strip()
    return json.loads(out) if out else {}


def _denial_reason(result: dict) -> str | None:
    """Denial reason emitted by the hook, or None when it did not deny.

    Examples:
        >>> (_denial_reason({}), _denial_reason({"hookSpecificOutput": {"permissionDecision": "deny"}}))
        (None, '')
    """
    hook_output = result.get("hookSpecificOutput", {})
    if hook_output.get("permissionDecision") != "deny":
        return None
    return hook_output.get("permissionDecisionReason", "")


def _call_export(name: str, *args: object) -> object:
    """Call one test-only hook export in a separate Node process."""
    proc = subprocess.run(
        [
            "node",
            "-e",
            "const hook = require(process.argv[1]); process.stdout.write(JSON.stringify(hook[process.argv[2]](...JSON.parse(process.argv[3]))));",
            str(HOOK),
            name,
            json.dumps(args),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    return json.loads(proc.stdout)


@pytest.fixture(name="audit_run")
def _audit_run(tmp_path: Path) -> tuple[Path, Path, str]:
    """Stage an audit that reached Step 3: run dir on disk plus its state sentinel.

    Returns the run dir, the sentinel file, and the project cwd the relative sentinel value resolves against.
    """
    project = tmp_path / "repo"
    run_dir = project / RUN_DIR_REL
    run_dir.mkdir(parents=True)
    state_dir = tmp_path / STATE_DIR_NAME
    state_dir.mkdir()
    sentinel = state_dir / "run-dir"
    sentinel.write_text(f"{RUN_DIR_REL}\n", encoding="utf-8")
    return run_dir, sentinel, str(project)


# ── Gate fires only for an in-flight audit missing its aggregate ──────────────


_skip_node_unavailable = pytest.mark.skipif(
    NODE_UNAVAILABLE,
    reason="requires node to execute the hook",
)


@_skip_node_unavailable
def test_missing_aggregate_is_denied(tmp_path: Path, audit_run: tuple[Path, Path, str]) -> None:
    """Sentinel present without summary.jsonl → deny, naming the step to redo."""
    run_dir, _, cwd = audit_run

    reason = _denial_reason(_run(tmp_path, _gate_payload(cwd=cwd)))

    assert reason is not None, "the follow-up gate must be denied while the aggregate is missing"
    assert str(run_dir / "summary.jsonl") in reason
    assert "Step 5" in reason


@_skip_node_unavailable
def test_empty_aggregate_is_denied(tmp_path: Path, audit_run: tuple[Path, Path, str]) -> None:
    """A zero-byte summary.jsonl counts as not written → deny."""
    run_dir, _, cwd = audit_run
    (run_dir / "summary.jsonl").touch()

    assert _denial_reason(_run(tmp_path, _gate_payload(cwd=cwd))) is not None


@_skip_node_unavailable
def test_written_aggregate_passes_through(tmp_path: Path, audit_run: tuple[Path, Path, str]) -> None:
    """Consolidator output present → hook stays silent and the gate proceeds."""
    run_dir, _, cwd = audit_run
    (run_dir / "summary.jsonl").write_text('{"file":"a.md","sev":"high"}\n', encoding="utf-8")

    assert _run(tmp_path, _gate_payload(cwd=cwd)) == {}


def _write_transcript(tmp_path: Path, assistant_text: str) -> Path:
    """Write a minimal two-row JSONL transcript: a human user turn then one assistant text block."""
    rows = [
        {"type": "user", "message": {"content": [{"type": "text", "text": "go"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": assistant_text}]}},
    ]
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return transcript


@_skip_node_unavailable
def test_aggregate_written_with_table_in_reply_has_no_reminder(
    tmp_path: Path, audit_run: tuple[Path, Path, str]
) -> None:
    """Table already printed this turn → allow with no additionalContext nudge."""
    run_dir, _, cwd = audit_run
    (run_dir / "summary.jsonl").write_text('{"file":"a.md","sev":"high"}\n', encoding="utf-8")
    transcript = _write_transcript(
        tmp_path, "| Field | Value |\n| --- | --- |\n| Title | x |\n| Scope | y |\n| Outcome | z |\n"
    )

    assert _run(tmp_path, _gate_payload(cwd=cwd, transcript_path=str(transcript))) == {}


@_skip_node_unavailable
def test_aggregate_written_without_table_in_reply_gets_reminder(
    tmp_path: Path, audit_run: tuple[Path, Path, str]
) -> None:
    """Raw YAML fields printed instead of a table → nudge naming Step 11b."""
    run_dir, _, cwd = audit_run
    (run_dir / "summary.jsonl").write_text('{"file":"a.md","sev":"high"}\n', encoding="utf-8")
    transcript = _write_transcript(tmp_path, "Title: audit\nOutcome: NEEDS_ATTENTION\n")

    result = _run(tmp_path, _gate_payload(cwd=cwd, transcript_path=str(transcript)))

    hook_output = result.get("hookSpecificOutput", {})
    assert hook_output.get("permissionDecision") == "allow"
    assert "Step 11b" in hook_output.get("additionalContext", "")


@_skip_node_unavailable
def test_aggregate_written_unreadable_transcript_has_no_reminder(
    tmp_path: Path, audit_run: tuple[Path, Path, str]
) -> None:
    """transcript_path pointing at a nonexistent file can't be read → fail open, no false nudge."""
    run_dir, _, cwd = audit_run
    (run_dir / "summary.jsonl").write_text('{"file":"a.md","sev":"high"}\n', encoding="utf-8")

    result = _run(tmp_path, _gate_payload(cwd=cwd, transcript_path=str(tmp_path / "missing.jsonl")))

    assert result == {}


@_skip_node_unavailable
def test_absolute_sentinel_path_resolves(tmp_path: Path, audit_run: tuple[Path, Path, str]) -> None:
    """An absolute run-dir value is honoured as-is, independent of the payload cwd."""
    run_dir, sentinel, _ = audit_run
    sentinel.write_text(f"{run_dir}\n", encoding="utf-8")

    assert _denial_reason(_run(tmp_path, _gate_payload(cwd="/nonexistent"))) is not None


@_skip_node_unavailable
def test_simulated_windows_run_dir_resolution_is_canonical_and_contained() -> None:
    """Resolve Windows report paths case-insensitively and reject traversal outside audit."""
    resolved = _call_export("resolveRunDir", r".REPORTS\AUDIT\run-1", r"C:\Repo")

    assert isinstance(resolved, str)
    assert resolved.casefold() == r"c:\repo\.reports\audit\run-1".casefold()
    assert _call_export("resolveRunDir", r".reports\audit\..\private", r"C:\Repo") is None


@_skip_node_unavailable
def test_trailing_slash_tmpdir_resolves_sentinel(tmp_path: Path, audit_run: tuple[Path, Path, str]) -> None:
    """MacOS exports TMPDIR with a trailing slash — the sentinel must still resolve."""
    _, _, cwd = audit_run

    assert _denial_reason(_run(tmp_path, _gate_payload(cwd=cwd), tmpdir_suffix="/")) is not None


@_skip_node_unavailable
def test_sentinel_resolved_from_payload_session_id(tmp_path: Path, audit_run: tuple[Path, Path, str]) -> None:
    """CSID falls back to the payload's session_id when the env var is unset."""
    _, _, cwd = audit_run

    result = _run(tmp_path, _gate_payload(cwd=cwd, session_id=CSID), session_id_env=None)

    assert _denial_reason(result) is not None


# ── Questions other than the follow-up gate are never blocked ────────────────


@_skip_node_unavailable
@pytest.mark.parametrize(
    ("label", "description"),
    [
        pytest.param("Acknowledge", "hook event name invalid — skill non-functional", id="breaking-ack"),
        pytest.param("Continue ignoring", "skip unknown flags and proceed", id="unknown-flag"),
        pytest.param("Abort", "stop and re-invoke with corrected flags", id="abort"),
    ],
)
def test_non_gate_questions_pass_through(
    tmp_path: Path, audit_run: tuple[Path, Path, str], label: str, description: str
) -> None:
    """Breaking acks and flag prompts fire before Step 5 by design — never gated."""
    _, _, cwd = audit_run
    payload = _other_question_payload(label, description)
    payload["cwd"] = cwd

    assert _run(tmp_path, payload) == {}


@_skip_node_unavailable
def test_gate_label_matched_only_in_label_field(tmp_path: Path, audit_run: tuple[Path, Path, str]) -> None:
    """Gate wording inside a description must not turn another question into the gate."""
    _, _, cwd = audit_run
    payload = _other_question_payload("Acknowledge", "you may fix all of these later")
    payload["cwd"] = cwd

    assert _run(tmp_path, payload) == {}


# ── Everything outside an in-flight audit passes through ─────────────────────


@_skip_node_unavailable
def test_no_sentinel_passes_through(tmp_path: Path) -> None:
    """No foundry:audit run reached Step 3 → unrelated questions are never gated."""
    assert _run(tmp_path, _gate_payload()) == {}


@_skip_node_unavailable
@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(_gate_payload(tool_name="Bash"), id="other-tool"),
        pytest.param(_gate_payload(hook_event_name="PostToolUse"), id="other-event"),
    ],
)
def test_non_matching_payloads_pass_through(tmp_path: Path, audit_run: tuple[Path, Path, str], payload: dict) -> None:
    """Only PreToolUse AskUserQuestion is inspected; anything else is untouched."""
    _, _, cwd = audit_run
    payload = {**payload, "cwd": cwd}

    assert _run(tmp_path, payload) == {}


@_skip_node_unavailable
def test_stale_sentinel_passes_through(tmp_path: Path, audit_run: tuple[Path, Path, str]) -> None:
    """Sentinel older than the enforcement window is treated as a crashed run."""
    _, sentinel, cwd = audit_run
    stale = time.time() - (FOUR_HOURS_S + 600)
    os.utime(sentinel, (stale, stale))

    assert _run(tmp_path, _gate_payload(cwd=cwd)) == {}


@_skip_node_unavailable
def test_missing_run_dir_passes_through(tmp_path: Path, audit_run: tuple[Path, Path, str]) -> None:
    """Run dir gone (worktree removed, TTL cleanup) → hook cannot judge, allows."""
    run_dir, _, cwd = audit_run
    run_dir.rmdir()

    assert _run(tmp_path, _gate_payload(cwd=cwd)) == {}


@_skip_node_unavailable
@pytest.mark.parametrize("content", ["", "   \n", ".reports/review/2026-08-04T10-00-00Z\n", "/etc\n"])
def test_implausible_sentinel_content_passes_through(tmp_path: Path, content: str) -> None:
    """Sentinel not holding a path under .reports/audit/ is ignored."""
    state_dir = tmp_path / STATE_DIR_NAME
    state_dir.mkdir()
    (state_dir / "run-dir").write_text(content, encoding="utf-8")

    assert _run(tmp_path, _gate_payload(cwd=str(tmp_path))) == {}


@_skip_node_unavailable
@pytest.mark.parametrize(
    "tool_input",
    [
        pytest.param({}, id="no-questions"),
        pytest.param({"questions": "not-a-list"}, id="questions-not-a-list"),
        pytest.param({"questions": [{"options": [{"label": 42}]}]}, id="label-not-a-string"),
    ],
)
def test_unexpected_tool_input_shape_passes_through(
    tmp_path: Path, audit_run: tuple[Path, Path, str], tool_input: object
) -> None:
    """An unrecognised payload shape reads as 'not the gate' — fail open."""
    _, _, cwd = audit_run

    assert _run(tmp_path, _gate_payload(cwd=cwd, tool_input=tool_input)) == {}


@_skip_node_unavailable
@pytest.mark.parametrize("session_id", ["../../etc/passwd", "has space", ""])
def test_unsafe_csid_passes_through(tmp_path: Path, audit_run: tuple[Path, Path, str], session_id: str) -> None:
    """A CSID that cannot name a sentinel file is discarded, never path-joined."""
    _, _, cwd = audit_run

    assert _run(tmp_path, _gate_payload(cwd=cwd), session_id_env=session_id) == {}


@_skip_node_unavailable
def test_malformed_stdin_passes_through(tmp_path: Path) -> None:
    """A hook bug or unparsable payload must never strand the session."""
    proc = subprocess.run(
        ["node", str(HOOK)],
        input="not json",
        capture_output=True,
        text=True,
        timeout=10,
        env={**os.environ, "TMPDIR": str(tmp_path)},
    )

    assert (proc.returncode, proc.stdout.strip()) == (0, "")
