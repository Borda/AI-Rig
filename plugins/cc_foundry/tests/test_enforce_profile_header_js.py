"""Subprocess tests for ``hooks/enforce-profile-header.js``.

The hook is a ``PreToolUse`` gate on ``AskUserQuestion``. Its contract:

* **Scoped to in-flight profile runs** — it acts only when the profile-state sentinel
  ``${TMPDIR:-/tmp}/foundry-profile-state-<CSID>`` exists; without it every
  ``AskUserQuestion`` passes through untouched (empty stdout, exit 0).
* **Parses a shell fragment** — unlike the flat one-path sentinels other skills
  write, that file holds ``KEY=VALUE`` lines (``REPORT_DIR``, ``SINCE``,
  ``SESSION_ID``, ``TOP_N``) that later analysis commands re-source with ``.``.
  The hook reads
  ``REPORT_DIR`` line-wise, mirroring what ``source`` would bind: leading
  whitespace allowed, no whitespace around ``=``, last assignment wins,
  surrounding quotes peeled.
* **Denies a missing report** — sentinel present but ``$REPORT_DIR/report.md``
  absent or empty means the analyzer did not run or emit terminal output, so
  the call is denied with an actionable reason.
* **Resolves the relative report dir** — profile setup assigns the relative
  ``.reports/profile/$STAMP``, so the sentinel normally holds a relative path
  that only resolves against the payload's ``cwd``.
* **Fails open** — stale sentinel, absent or malformed ``REPORT_DIR`` line,
  implausible path, vanished report dir, unparsable payload: every can't-tell
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

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "enforce-profile-header.js"

CSID = "test-session-1234"
SENTINEL_NAME = f"foundry-profile-state-{CSID}"
REPORT_DIR_REL = ".reports/profile/2026-08-04T10-00-00Z"
TWO_HOURS_S = 2 * 60 * 60

NODE_UNAVAILABLE = shutil.which("node") is None


def _state_file(report_dir_line: str) -> str:
    """Build the initial profile-state fragment with the given ``REPORT_DIR`` line.

    Examples:
        >>> _state_file("REPORT_DIR=x").splitlines()[:2]
        ['REPORT_DIR=x', 'SINCE=24h']
    """
    return f"{report_dir_line}\nSINCE=24h\nSESSION_ID=\nTOP_N=5\n"


def _ask_payload(**overrides: object) -> dict:
    """Build a PreToolUse AskUserQuestion payload, applying `overrides`.

    Examples:
        >>> _ask_payload(cwd="/work")["cwd"]
        '/work'
    """
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
    """Denial reason emitted by the hook, or None when it did not deny.

    Examples:
        >>> _denial_reason({"hookSpecificOutput": {"permissionDecision": "allow"}}) is None
        True
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


@pytest.fixture(name="profile_run")
def _profile_run(tmp_path: Path) -> tuple[Path, Path, str]:
    """Stage a profile run that reached Step 1: report dir on disk plus its state file.

    Returns the report dir, the sentinel file, and the project cwd the relative sentinel value resolves against.
    """
    project = tmp_path / "repo"
    report_dir = project / REPORT_DIR_REL
    report_dir.mkdir(parents=True)
    sentinel = tmp_path / SENTINEL_NAME
    sentinel.write_text(_state_file(f"REPORT_DIR={REPORT_DIR_REL}"), encoding="utf-8")
    return report_dir, sentinel, str(project)


# ── Gate fires only for an in-flight run missing its report ──────────────────


_skip_node_unavailable = pytest.mark.skipif(
    NODE_UNAVAILABLE,
    reason="requires node to execute the hook",
)


@_skip_node_unavailable
def test_missing_report_is_denied(tmp_path: Path, profile_run: tuple[Path, Path, str]) -> None:
    """Sentinel present without report.md → deny, naming the steps to redo."""
    report_dir, _, cwd = profile_run

    reason = _denial_reason(_run(tmp_path, _ask_payload(cwd=cwd)))

    assert reason is not None, "AskUserQuestion must be denied while the report is missing"
    assert str(report_dir / "report.md") in reason
    assert "Step 4" in reason


@_skip_node_unavailable
def test_empty_report_is_denied(tmp_path: Path, profile_run: tuple[Path, Path, str]) -> None:
    """A zero-byte report.md counts as not written → deny."""
    report_dir, _, cwd = profile_run
    (report_dir / "report.md").touch()

    assert _denial_reason(_run(tmp_path, _ask_payload(cwd=cwd))) is not None


@_skip_node_unavailable
def test_written_report_passes_through(tmp_path: Path, profile_run: tuple[Path, Path, str]) -> None:
    """Analyzer output present → hook stays silent and the call proceeds."""
    report_dir, _, cwd = profile_run
    (report_dir / "report.md").write_text("---\nTitle: profile\n---\n", encoding="utf-8")

    assert _run(tmp_path, _ask_payload(cwd=cwd)) == {}


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
def test_report_written_with_table_in_reply_has_no_reminder(
    tmp_path: Path, profile_run: tuple[Path, Path, str]
) -> None:
    """Table already printed this turn → allow with no additionalContext nudge."""
    report_dir, _, cwd = profile_run
    (report_dir / "report.md").write_text("---\nTitle: profile\n---\n", encoding="utf-8")
    transcript = _write_transcript(
        tmp_path, "| Field | Value |\n| --- | --- |\n| Title | x |\n| Since | y |\n| Top N | z |\n"
    )

    assert _run(tmp_path, _ask_payload(cwd=cwd, transcript_path=str(transcript))) == {}


@_skip_node_unavailable
def test_report_written_without_table_in_reply_gets_reminder(
    tmp_path: Path, profile_run: tuple[Path, Path, str]
) -> None:
    """Raw YAML fields printed instead of a table → nudge naming Step 4b."""
    report_dir, _, cwd = profile_run
    (report_dir / "report.md").write_text("---\nTitle: profile\n---\n", encoding="utf-8")
    transcript = _write_transcript(tmp_path, "Title: profile\nSince: 24h\n")

    result = _run(tmp_path, _ask_payload(cwd=cwd, transcript_path=str(transcript)))

    hook_output = result.get("hookSpecificOutput", {})
    assert hook_output.get("permissionDecision") == "allow"
    assert "Step 4b" in hook_output.get("additionalContext", "")


@_skip_node_unavailable
def test_report_written_unreadable_transcript_has_no_reminder(
    tmp_path: Path, profile_run: tuple[Path, Path, str]
) -> None:
    """transcript_path pointing at a nonexistent file can't be read → fail open, no false nudge."""
    report_dir, _, cwd = profile_run
    (report_dir / "report.md").write_text("---\nTitle: profile\n---\n", encoding="utf-8")

    result = _run(tmp_path, _ask_payload(cwd=cwd, transcript_path=str(tmp_path / "missing.jsonl")))

    assert result == {}


@_skip_node_unavailable
def test_absolute_sentinel_path_resolves(tmp_path: Path, profile_run: tuple[Path, Path, str]) -> None:
    """An absolute REPORT_DIR value is honoured as-is, independent of the payload cwd."""
    report_dir, sentinel, _ = profile_run
    sentinel.write_text(_state_file(f"REPORT_DIR={report_dir}"), encoding="utf-8")

    assert _denial_reason(_run(tmp_path, _ask_payload(cwd="/nonexistent"))) is not None


@_skip_node_unavailable
def test_simulated_windows_report_dir_resolution_is_canonical_and_contained() -> None:
    """Resolve Windows report paths case-insensitively and reject traversal outside profile."""
    resolved = _call_export("resolveReportDir", r".REPORTS\PROFILE\run-1", r"C:\Repo")

    assert isinstance(resolved, str)
    assert resolved.casefold() == r"c:\repo\.reports\profile\run-1".casefold()
    assert _call_export("resolveReportDir", r".reports\profile\..\private", r"C:\Repo") is None


@_skip_node_unavailable
def test_trailing_slash_tmpdir_resolves_sentinel(tmp_path: Path, profile_run: tuple[Path, Path, str]) -> None:
    """MacOS exports TMPDIR with a trailing slash — the sentinel must still resolve."""
    _, _, cwd = profile_run

    assert _denial_reason(_run(tmp_path, _ask_payload(cwd=cwd), tmpdir_suffix="/")) is not None


@_skip_node_unavailable
def test_sentinel_resolved_from_payload_session_id(tmp_path: Path, profile_run: tuple[Path, Path, str]) -> None:
    """CSID falls back to the payload's session_id when the env var is unset."""
    _, _, cwd = profile_run

    result = _run(tmp_path, _ask_payload(cwd=cwd, session_id=CSID), session_id_env=None)

    assert _denial_reason(result) is not None


# ── REPORT_DIR is read the way `source` would bind it ────────────────────────


@_skip_node_unavailable
@pytest.mark.parametrize(
    "line",
    [
        pytest.param(f"REPORT_DIR={REPORT_DIR_REL}", id="plain"),
        pytest.param(f'REPORT_DIR="{REPORT_DIR_REL}"', id="double-quoted"),
        pytest.param(f"REPORT_DIR='{REPORT_DIR_REL}'", id="single-quoted"),
        pytest.param(f"REPORT_DIR={REPORT_DIR_REL}   ", id="trailing-whitespace"),
        pytest.param(f"REPORT_DIR={REPORT_DIR_REL}\r", id="crlf-line-ending"),
        pytest.param(f"\tREPORT_DIR={REPORT_DIR_REL}", id="leading-indent"),
        pytest.param(f"export REPORT_DIR={REPORT_DIR_REL}", id="export-prefix"),
    ],
)
def test_assignment_forms_are_parsed(tmp_path: Path, profile_run: tuple[Path, Path, str], line: str) -> None:
    """Every form a shell would bind — quoting, indent, `export` — must not defeat the gate."""
    _, sentinel, cwd = profile_run
    # Preserve the supplied CRLF bytes instead of letting Windows text I/O add
    # an extra carriage return before the test parser receives the sentinel.
    sentinel.write_bytes(_state_file(line).encode("utf-8"))

    assert _denial_reason(_run(tmp_path, _ask_payload(cwd=cwd))) is not None


@_skip_node_unavailable
def test_last_assignment_wins(tmp_path: Path, profile_run: tuple[Path, Path, str]) -> None:
    """Sourcing binds the final assignment — an earlier stale one must not shadow it."""
    report_dir, sentinel, cwd = profile_run
    stale = ".reports/profile/1999-01-01T00-00-00Z"
    sentinel.write_text(
        f"REPORT_DIR={stale}\n{_state_file(f'REPORT_DIR={REPORT_DIR_REL}')}",
        encoding="utf-8",
    )

    reason = _denial_reason(_run(tmp_path, _ask_payload(cwd=cwd)))

    assert reason is not None
    assert str(report_dir / "report.md") in reason


@_skip_node_unavailable
@pytest.mark.parametrize(
    "line",
    [
        "SINCE=7d",
        "REPORT_DIR=",
        pytest.param(f"REPORT_DIR = {REPORT_DIR_REL}", id="spaces-around-equals"),
        pytest.param(f"MY_REPORT_DIR={REPORT_DIR_REL}", id="key-suffix-only"),
        pytest.param(f"REPORT_DIR:{REPORT_DIR_REL}", id="colon-not-equals"),
        pytest.param(REPORT_DIR_REL, id="bare-path-no-key"),
    ],
)
def test_unparsable_report_dir_passes_through(tmp_path: Path, profile_run: tuple[Path, Path, str], line: str) -> None:
    """No binding the shell would make → the hook cannot name a dir, so it allows."""
    _, sentinel, cwd = profile_run
    sentinel.write_text(_state_file(line), encoding="utf-8")

    assert _run(tmp_path, _ask_payload(cwd=cwd)) == {}


# ── Everything outside an in-flight profile run passes through ───────────────


@_skip_node_unavailable
def test_no_sentinel_passes_through(tmp_path: Path) -> None:
    """No foundry:profile run reached Step 1 → unrelated questions are never gated."""
    assert _run(tmp_path, _ask_payload()) == {}


@_skip_node_unavailable
@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(_ask_payload(tool_name="Bash"), id="other-tool"),
        pytest.param(_ask_payload(hook_event_name="PostToolUse"), id="other-event"),
    ],
)
def test_non_matching_payloads_pass_through(tmp_path: Path, profile_run: tuple[Path, Path, str], payload: dict) -> None:
    """Only PreToolUse AskUserQuestion is inspected; anything else is untouched."""
    _, _, cwd = profile_run

    assert _run(tmp_path, {**payload, "cwd": cwd}) == {}


@_skip_node_unavailable
def test_stale_sentinel_passes_through(tmp_path: Path, profile_run: tuple[Path, Path, str]) -> None:
    """Sentinel older than the enforcement window is treated as a crashed run."""
    _, sentinel, cwd = profile_run
    stale = time.time() - (TWO_HOURS_S + 600)
    os.utime(sentinel, (stale, stale))

    assert _run(tmp_path, _ask_payload(cwd=cwd)) == {}


@_skip_node_unavailable
def test_missing_report_dir_passes_through(tmp_path: Path, profile_run: tuple[Path, Path, str]) -> None:
    """Report dir gone (worktree removed, TTL cleanup) → hook cannot judge, allows."""
    report_dir, _, cwd = profile_run
    report_dir.rmdir()

    assert _run(tmp_path, _ask_payload(cwd=cwd)) == {}


@_skip_node_unavailable
@pytest.mark.parametrize(
    "content",
    ["", "   \n", "REPORT_DIR=.reports/audit/2026-08-04T10-00-00Z\n", "REPORT_DIR=/etc\n"],
)
def test_implausible_sentinel_content_passes_through(tmp_path: Path, content: str) -> None:
    """A REPORT_DIR not under .reports/profile/ is ignored."""
    (tmp_path / SENTINEL_NAME).write_text(content, encoding="utf-8")

    assert _run(tmp_path, _ask_payload(cwd=str(tmp_path))) == {}


@_skip_node_unavailable
@pytest.mark.parametrize("session_id", ["../../etc/passwd", "has space", ""])
def test_unsafe_csid_passes_through(tmp_path: Path, profile_run: tuple[Path, Path, str], session_id: str) -> None:
    """A CSID that cannot name a sentinel file is discarded, never path-joined."""
    _, _, cwd = profile_run

    assert _run(tmp_path, _ask_payload(cwd=cwd), session_id_env=session_id) == {}


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
