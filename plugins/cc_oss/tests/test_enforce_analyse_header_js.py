"""Subprocess tests for ``hooks/enforce-analyse-header.js``.

The hook is a ``PreToolUse`` gate on ``AskUserQuestion``. Its contract:

* **Scoped to in-flight analyses** — it acts only when
  ``${TMPDIR:-/tmp}/analyse-report-file-<CSID>`` names a report under
  ``.reports/analyse/``; without it every ``AskUserQuestion`` passes through
  untouched (empty stdout, exit 0).
* **Denies a missing report** — sentinel present but the report it names absent
  or empty means the mode file never wrote its report, so SKILL.md Step 6a's
  follow-up question is denied with an actionable reason.
* **Covers all three modes** — thread, vitality and ecosystem each rewrite the
  same sentinel with their own path just before their report Write.
* **Fails open** — stale sentinel, empty sentinel (SKILL.md Step 1, mode not yet
  reached), implausible sentinel content, unparsable payload: every can't-tell
  case allows the call.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "enforce-analyse-header.js"

CSID = "test-session-1234"
SENTINEL_NAME = f"analyse-report-file-{CSID}"
TWO_HOURS_S = 2 * 60 * 60

# One report path per sub-mode, as the mode files build them.
MODE_REPORTS = {
    "thread": ".reports/analyse/thread/output-analyse-thread-42-2026-08-04.md",
    "vitality": ".reports/analyse/vitality/output-analyse-vitality-owner-repo-2026-08-04T10-00-00Z.md",
    "ecosystem": ".reports/analyse/ecosystem/output-analyse-ecosystem-2026-08-04.md",
}

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


@pytest.fixture(name="repo")
def _repo(tmp_path: Path) -> Path:
    """Create the working directory an analyse run reports against."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    return repo_dir


@pytest.fixture(name="analyse_run")
def _analyse_run(tmp_path: Path, repo: Path) -> Path:
    """Stage a thread-mode run that reached its sentinel write but not its report write."""
    sentinel = tmp_path / SENTINEL_NAME
    sentinel.write_text(f"{MODE_REPORTS['thread']}\n", encoding="utf-8")
    return sentinel


# ── Gate fires only for an in-flight analyse missing its report ───────────────


_skip_node_unavailable = pytest.mark.skipif(
    NODE_UNAVAILABLE,
    reason="requires node to execute the hook",
)


@_skip_node_unavailable
@pytest.mark.parametrize("mode", [pytest.param(name, id=name) for name in MODE_REPORTS])
def test_missing_report_is_denied_for_every_mode(tmp_path: Path, repo: Path, mode: str) -> None:
    """Each sub-mode's report path, unwritten → deny, naming the file and the tool."""
    (tmp_path / SENTINEL_NAME).write_text(f"{MODE_REPORTS[mode]}\n", encoding="utf-8")

    reason = _denial_reason(_run(tmp_path, _ask_payload(cwd=str(repo))))

    assert reason is not None, "AskUserQuestion must be denied while the report is missing"
    assert str(repo / MODE_REPORTS[mode]) in reason
    assert "AskUserQuestion only after" in reason


@_skip_node_unavailable
def test_empty_report_is_denied(tmp_path: Path, repo: Path, analyse_run: Path) -> None:
    """A zero-byte report counts as not written → deny."""
    report = repo / MODE_REPORTS["thread"]
    report.parent.mkdir(parents=True)
    report.touch()

    assert _denial_reason(_run(tmp_path, _ask_payload(cwd=str(repo)))) is not None


@_skip_node_unavailable
def test_written_report_passes_through(tmp_path: Path, repo: Path, analyse_run: Path) -> None:
    """Report on disk → hook stays silent and the follow-up question proceeds."""
    report = repo / MODE_REPORTS["thread"]
    report.parent.mkdir(parents=True)
    report.write_text("---\nTitle: oss:analyse — thread\n---\n", encoding="utf-8")

    assert _run(tmp_path, _ask_payload(cwd=str(repo))) == {}


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
def test_report_written_with_table_in_reply_has_no_reminder(tmp_path: Path, repo: Path, analyse_run: Path) -> None:
    """Table already printed this turn → allow with no additionalContext nudge."""
    report = repo / MODE_REPORTS["thread"]
    report.parent.mkdir(parents=True)
    report.write_text("---\nTitle: oss:analyse — thread\n---\n", encoding="utf-8")
    transcript = _write_transcript(
        tmp_path, "| Field | Value |\n| --- | --- |\n| Title | x |\n| Date | y |\n| Scope | z |\n"
    )

    assert _run(tmp_path, _ask_payload(cwd=str(repo), transcript_path=str(transcript))) == {}


@_skip_node_unavailable
def test_report_written_without_table_in_reply_gets_reminder(tmp_path: Path, repo: Path, analyse_run: Path) -> None:
    """Raw YAML fields printed instead of a table → nudge naming oss:analyse."""
    report = repo / MODE_REPORTS["thread"]
    report.parent.mkdir(parents=True)
    report.write_text("---\nTitle: oss:analyse — thread\n---\n", encoding="utf-8")
    transcript = _write_transcript(tmp_path, "Title: oss:analyse — thread\nDate: 2026-08-08\n")

    result = _run(tmp_path, _ask_payload(cwd=str(repo), transcript_path=str(transcript)))

    hook_output = result.get("hookSpecificOutput", {})
    assert hook_output.get("permissionDecision") == "allow"
    assert "oss:analyse" in hook_output.get("additionalContext", "")


@_skip_node_unavailable
def test_report_written_unreadable_transcript_has_no_reminder(tmp_path: Path, repo: Path, analyse_run: Path) -> None:
    """transcript_path pointing at a nonexistent file can't be read → fail open, no false nudge."""
    report = repo / MODE_REPORTS["thread"]
    report.parent.mkdir(parents=True)
    report.write_text("---\nTitle: oss:analyse — thread\n---\n", encoding="utf-8")

    result = _run(tmp_path, _ask_payload(cwd=str(repo), transcript_path=str(tmp_path / "missing.jsonl")))

    assert result == {}


@_skip_node_unavailable
def test_absolute_sentinel_path_ignores_cwd(tmp_path: Path, repo: Path) -> None:
    """An absolute report path is honoured as-is, independent of the payload cwd."""
    absolute = repo / MODE_REPORTS["vitality"]
    (tmp_path / SENTINEL_NAME).write_text(f"{absolute}\n", encoding="utf-8")

    assert _denial_reason(_run(tmp_path, _ask_payload(cwd="/nonexistent"))) is not None


@_skip_node_unavailable
def test_simulated_windows_report_file_resolution_is_canonical_and_contained() -> None:
    """Resolve Windows report paths case-insensitively and reject traversal outside analyse."""
    resolved = _call_export("resolveReportFile", r".REPORTS\ANALYSE\THREAD\output-analyse-thread-1.md", r"C:\Repo")

    assert isinstance(resolved, str)
    assert resolved.casefold() == r"c:\repo\.reports\analyse\thread\output-analyse-thread-1.md".casefold()
    assert _call_export("resolveReportFile", r".reports\analyse\thread\..\..\private.md", r"C:\Repo") is None


@_skip_node_unavailable
def test_trailing_slash_tmpdir_resolves_sentinel(tmp_path: Path, repo: Path, analyse_run: Path) -> None:
    """MacOS exports TMPDIR with a trailing slash — the sentinel must still resolve."""
    assert _denial_reason(_run(tmp_path, _ask_payload(cwd=str(repo)), tmpdir_suffix="/")) is not None


@_skip_node_unavailable
def test_sentinel_resolved_from_payload_session_id(tmp_path: Path, repo: Path, analyse_run: Path) -> None:
    """CSID falls back to the payload's session_id when the env var is unset."""
    result = _run(tmp_path, _ask_payload(cwd=str(repo), session_id=CSID), session_id_env=None)

    assert _denial_reason(result) is not None


# ── Everything outside an in-flight analyse passes through ───────────────────


@_skip_node_unavailable
def test_no_sentinel_passes_through(tmp_path: Path, repo: Path) -> None:
    """No oss:analyse run started → unrelated questions are never gated."""
    assert _run(tmp_path, _ask_payload(cwd=str(repo))) == {}


@_skip_node_unavailable
@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(_ask_payload(tool_name="Bash"), id="other-tool"),
        pytest.param(_ask_payload(hook_event_name="PostToolUse"), id="other-event"),
    ],
)
def test_non_matching_payloads_pass_through(tmp_path: Path, repo: Path, analyse_run: Path, payload: dict) -> None:
    """Only PreToolUse AskUserQuestion is inspected; anything else is untouched."""
    assert _run(tmp_path, {**payload, "cwd": str(repo)}) == {}


@_skip_node_unavailable
def test_stale_sentinel_passes_through(tmp_path: Path, repo: Path, analyse_run: Path) -> None:
    """Sentinel older than the enforcement window is treated as a crashed run."""
    stale = time.time() - (TWO_HOURS_S + 600)
    os.utime(analyse_run, (stale, stale))

    assert _run(tmp_path, _ask_payload(cwd=str(repo))) == {}


@_skip_node_unavailable
@pytest.mark.parametrize(
    "content",
    ["", "   \n", "docs/handover.md\n", ".reports/review/2026-08-04T10-00-00Z/review-report.md\n"],
)
def test_implausible_sentinel_content_passes_through(tmp_path: Path, repo: Path, content: str) -> None:
    """Sentinel not naming a .reports/analyse/ report is ignored."""
    (tmp_path / SENTINEL_NAME).write_text(content, encoding="utf-8")

    assert _run(tmp_path, _ask_payload(cwd=str(repo))) == {}


@_skip_node_unavailable
def test_missing_cwd_falls_back_to_process_cwd(tmp_path: Path) -> None:
    """No cwd in the payload → relative path resolves against the hook's own cwd."""
    # tmp_path's name keeps the relative path unique, so it cannot collide with a
    # real report in the working directory the test process happens to run from.
    relative = f".reports/analyse/thread/output-analyse-thread-{tmp_path.name}.md"
    (tmp_path / SENTINEL_NAME).write_text(f"{relative}\n", encoding="utf-8")

    assert _denial_reason(_run(tmp_path, _ask_payload())) is not None


@_skip_node_unavailable
@pytest.mark.parametrize("session_id", ["../../etc/passwd", "has space", ""])
def test_unsafe_csid_passes_through(tmp_path: Path, repo: Path, analyse_run: Path, session_id: str) -> None:
    """A CSID that cannot name a sentinel file is discarded, never path-joined."""
    assert _run(tmp_path, _ask_payload(cwd=str(repo)), session_id_env=session_id) == {}


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
