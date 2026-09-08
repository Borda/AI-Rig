"""Subprocess tests for ``hooks/enforce-topic-header.js``.

The hook is a ``PreToolUse`` gate on ``AskUserQuestion``. Its contract:

* **Scoped to in-flight topic runs** — it acts only when the sentinel
  ``${TMPDIR:-/tmp}/research-topic-report-file-<CSID>`` exists; without it every
  ``AskUserQuestion`` passes through untouched (empty stdout, exit 0).
* **Denies a missing report** — sentinel present but the report file it names
  absent or empty means the report step (Step 3 / team.md consolidation /
  plan.md P3) never produced it, so the call is denied with an actionable
  reason.
* **Fails open** — stale sentinel, implausible sentinel content, vanished
  ``.reports/research/`` dir, unparsable payload: every can't-tell case allows
  the call.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "enforce-topic-header.js"

CSID = "test-session-1234"
SENTINEL_NAME = f"research-topic-report-file-{CSID}"
TWO_HOURS_S = 2 * 60 * 60

NODE_UNAVAILABLE = shutil.which("node") is None


def _ask_payload(**overrides: object) -> dict:
    """Build a PreToolUse AskUserQuestion payload, applying `overrides`."""
    payload: dict = {
        "hook_event_name": "PreToolUse",
        "tool_name": "AskUserQuestion",
        "tool_input": {"questions": [{"question": "What next?"}]},
    }
    payload.update(overrides)
    return payload


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
    """Denial reason emitted by the hook, or None when it did not deny."""
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


@pytest.fixture(name="topic_run")
def _topic_run(tmp_path: Path) -> tuple[Path, Path]:
    """Stage a topic run that resolved its report path: output file plus sentinel."""
    report_file = tmp_path / "repo" / ".reports" / "research" / "topic-main-2026-08-04.md"
    report_file.parent.mkdir(parents=True)
    sentinel = tmp_path / SENTINEL_NAME
    sentinel.write_text(f"{report_file}\n", encoding="utf-8")
    return report_file, sentinel


# ── Gate fires only for an in-flight run missing its report ──────────────────


_skip_node_unavailable = pytest.mark.skipif(
    NODE_UNAVAILABLE,
    reason="requires node to execute the hook",
)


@_skip_node_unavailable
def test_missing_report_is_denied(tmp_path: Path, topic_run: tuple[Path, Path]) -> None:
    """Sentinel present without the report file → deny, naming the path to write."""
    report_file, _ = topic_run

    reason = _denial_reason(_run(tmp_path, _ask_payload()))

    assert reason is not None, "AskUserQuestion must be denied while the report is missing"
    assert str(report_file) in reason
    assert "Print report header" in reason


@_skip_node_unavailable
def test_empty_report_is_denied(tmp_path: Path, topic_run: tuple[Path, Path]) -> None:
    """A zero-byte report counts as not written → deny."""
    report_file, _ = topic_run
    report_file.touch()

    assert _denial_reason(_run(tmp_path, _ask_payload())) is not None


@_skip_node_unavailable
def test_written_report_passes_through(tmp_path: Path, topic_run: tuple[Path, Path]) -> None:
    """Report present → hook stays silent and the call proceeds."""
    report_file, _ = topic_run
    report_file.write_text("---\nResearch — efficient transformers\n---\n", encoding="utf-8")

    assert _run(tmp_path, _ask_payload()) == {}


def _write_transcript(tmp_path: Path, assistant_text: str) -> Path:
    """Write a minimal two-row JSONL transcript: a user turn then one assistant text block.

    Examples:
        >>> tmp_path = getfixture("tmp_path")
        >>> path = _write_transcript(tmp_path, "done")
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


@_skip_node_unavailable
def test_report_written_with_table_in_reply_has_no_reminder(tmp_path: Path, topic_run: tuple[Path, Path]) -> None:
    """Table already printed this turn → allow with no additionalContext nudge."""
    report_file, _ = topic_run
    report_file.write_text("---\nResearch — efficient transformers\n---\n", encoding="utf-8")
    transcript = _write_transcript(
        tmp_path, "| Field | Value |\n| --- | --- |\n| Title | x |\n| Date | y |\n| Method | z |\n"
    )

    assert _run(tmp_path, _ask_payload(transcript_path=str(transcript))) == {}


@_skip_node_unavailable
def test_report_written_without_table_in_reply_gets_reminder(tmp_path: Path, topic_run: tuple[Path, Path]) -> None:
    """Raw YAML fields printed instead of a table → nudge naming research:topic."""
    report_file, _ = topic_run
    report_file.write_text("---\nResearch — efficient transformers\n---\n", encoding="utf-8")
    transcript = _write_transcript(tmp_path, "Title: efficient transformers\nDate: 2026-08-08\n")

    result = _run(tmp_path, _ask_payload(transcript_path=str(transcript)))

    hook_output = result.get("hookSpecificOutput", {})
    assert hook_output.get("permissionDecision") == "allow"
    assert "research:topic" in hook_output.get("additionalContext", "")


@_skip_node_unavailable
def test_report_written_unreadable_transcript_has_no_reminder(tmp_path: Path, topic_run: tuple[Path, Path]) -> None:
    """transcript_path pointing at a nonexistent file can't be read → fail open, no false nudge."""
    report_file, _ = topic_run
    report_file.write_text("---\nResearch — efficient transformers\n---\n", encoding="utf-8")

    result = _run(tmp_path, _ask_payload(transcript_path=str(tmp_path / "missing.jsonl")))

    assert result == {}


@_skip_node_unavailable
def test_plan_mode_report_name_is_gated(tmp_path: Path) -> None:
    """plan.md writes `topic-plan-<branch>-<date>.md` — same gate applies."""
    report_file = tmp_path / "repo" / ".reports" / "research" / "topic-plan-main-2026-08-04.md"
    report_file.parent.mkdir(parents=True)
    (tmp_path / SENTINEL_NAME).write_text(f"{report_file}\n", encoding="utf-8")

    assert _denial_reason(_run(tmp_path, _ask_payload())) is not None


@_skip_node_unavailable
def test_counter_suffixed_report_name_is_gated(tmp_path: Path) -> None:
    """Anti-overwrite reruns resolve to `-2.md`; the sentinel path is used verbatim."""
    report_file = tmp_path / "repo" / ".reports" / "research" / "topic-main-2026-08-04-2.md"
    report_file.parent.mkdir(parents=True)
    (tmp_path / SENTINEL_NAME).write_text(f"{report_file}\n", encoding="utf-8")

    assert _denial_reason(_run(tmp_path, _ask_payload())) is not None


@_skip_node_unavailable
def test_trailing_slash_tmpdir_resolves_sentinel(tmp_path: Path, topic_run: tuple[Path, Path]) -> None:
    """MacOS exports TMPDIR with a trailing slash — the sentinel must still resolve."""
    assert _denial_reason(_run(tmp_path, _ask_payload(), tmpdir_suffix="/")) is not None


@_skip_node_unavailable
def test_sentinel_resolved_from_payload_session_id(tmp_path: Path, topic_run: tuple[Path, Path]) -> None:
    """CSID falls back to the payload's session_id when the env var is unset."""
    result = _run(tmp_path, _ask_payload(session_id=CSID), session_id_env=None)

    assert _denial_reason(result) is not None


@_skip_node_unavailable
def test_simulated_windows_topic_report_validation_accepts_contained_paths_and_rejects_traversal() -> None:
    """Recognise Windows separators and casing without trusting escaped sentinel paths."""
    assert _call_export("isTopicReportFile", r"C:\Repo\.REPORTS\RESEARCH\TOPIC-main.md") is True
    assert _call_export("isTopicReportFile", r"C:\Repo\.reports\research\topic-dir\..\private.md") is False


# ── Everything outside an in-flight topic run passes through ─────────────────


@_skip_node_unavailable
def test_no_sentinel_passes_through(tmp_path: Path) -> None:
    """No topic run resolved a report path → unrelated questions are never gated."""
    assert _run(tmp_path, _ask_payload()) == {}


@_skip_node_unavailable
@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(_ask_payload(tool_name="Bash"), id="other-tool"),
        pytest.param(_ask_payload(hook_event_name="PostToolUse"), id="other-event"),
    ],
)
def test_non_matching_payloads_pass_through(tmp_path: Path, topic_run: tuple[Path, Path], payload: dict) -> None:
    """Only PreToolUse AskUserQuestion is inspected; anything else is untouched."""
    assert _run(tmp_path, payload) == {}


@_skip_node_unavailable
def test_stale_sentinel_passes_through(tmp_path: Path, topic_run: tuple[Path, Path]) -> None:
    """Sentinel older than the enforcement window is treated as a crashed run."""
    _, sentinel = topic_run
    stale = time.time() - (TWO_HOURS_S + 600)
    os.utime(sentinel, (stale, stale))

    assert _run(tmp_path, _ask_payload()) == {}


@_skip_node_unavailable
def test_missing_report_dir_passes_through(tmp_path: Path, topic_run: tuple[Path, Path]) -> None:
    """Allow writes when expired research evidence is no longer available."""
    report_file, _ = topic_run
    report_file.parent.rmdir()

    assert _run(tmp_path, _ask_payload()) == {}


@_skip_node_unavailable
@pytest.mark.parametrize(
    "content",
    [
        "",
        "   \n",
        "repo/.reports/research/topic-main-2026-08-04.md\n",
        "/etc/passwd\n",
        "/repo/.reports/research/topic-main-2026-08-04\n",
    ],
)
def test_implausible_sentinel_content_passes_through(tmp_path: Path, content: str) -> None:
    """Sentinel not holding an absolute .reports/research/topic-*.md path is ignored."""
    (tmp_path / SENTINEL_NAME).write_text(content, encoding="utf-8")

    assert _run(tmp_path, _ask_payload()) == {}


@_skip_node_unavailable
@pytest.mark.parametrize("session_id", ["../../etc/passwd", "has space", ""])
def test_unsafe_csid_passes_through(tmp_path: Path, topic_run: tuple[Path, Path], session_id: str) -> None:
    """A CSID that cannot name a sentinel file is discarded, never path-joined."""
    assert _run(tmp_path, _ask_payload(), session_id_env=session_id) == {}


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
