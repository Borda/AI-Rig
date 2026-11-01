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

Every case runs the real hook through ``node``: the hook's behaviour end to end is the contract, so nothing is mocked.
The analyzer case also runs the real ``timing_analyzer.py`` to produce the report the hook inspects.
"""

from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from _hook_io import CSID, call_export, denial_reason, run_hook_json, stop_payload, write_transcript

HOOK = Path(__file__).resolve().parent.parent.parent / "hooks" / "enforce-profile-header.js"
ANALYZER = HOOK.parent.parent / "bin" / "timing_analyzer.py"

SENTINEL_NAME = f"foundry-profile-state-{CSID}"
REPORT_DIR_REL = ".reports/profile/2026-08-04T10-00-00Z"
TWO_HOURS_S = 2 * 60 * 60
#: Front matter of a report the analyzer wrote, and the header table that renders it.
WRITTEN_REPORT = "---\nTitle: x\nPR: #1\nDate: y\n---\n"
DELIVERED = "| Field | Value |\n| --- | --- |\n| Title | x |\n| PR | #1 |\n| Date | y |\n"

_skip_node_unavailable = pytest.mark.skipif(shutil.which("node") is None, reason="requires node to execute the hook")
_run = functools.partial(run_hook_json, HOOK)
_call_export = functools.partial(call_export, HOOK)


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
        "tool_input": {"questions": [{"question": "What next?", "header": "profile"}]},
    }
    payload.update(overrides)
    return payload


def _summary_payload(cwd: str, summary: str | None) -> dict:
    """Build a follow-up with fixed plain summary lines and no previews."""
    report = Path(cwd) / REPORT_DIR_REL / "report.md"
    options = [{"label": label, "description": "next step"} for label in ("Drill into slowest session", "Skip")]
    text = f"{summary}\n→ {report}\nWhat next?" if summary else "What next?"
    questions = [{"question": text, "header": "profile", "options": options}]
    return _ask_payload(cwd=str(cwd), tool_input={"questions": questions}, transcript_path=None)


def _move_summary_to_previews(payload: dict, count: int) -> dict:
    """Move the question text into the first `count` options' previews, leaving a bare ``What next?`` question."""
    first = payload["tool_input"]["questions"][0]
    for option in first["options"][:count]:
        option["preview"] = first["question"]
    first["question"] = "What next?"
    return payload


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


@pytest.fixture(name="written_run")
def _written_run(profile_run: tuple[Path, Path, str]) -> str:
    """Stage a profile run whose analyzer wrote the report DELIVERED renders; returns the project cwd."""
    report_dir, _, cwd = profile_run
    (report_dir / "report.md").write_text(WRITTEN_REPORT, encoding="utf-8")
    return cwd


@pytest.fixture(name="analyzed_run")
def _analyzed_run(tmp_path: Path, profile_run: tuple[Path, Path, str]) -> tuple[str, str]:
    """Run the real ``timing_analyzer.py`` into the staged report dir; return the project cwd and the header table.

    The analyzer runs from the project dir with the relative ``--output "$REPORT_DIR/report.md"`` the profile skill
    passes (its ``REPORT_DIR`` is relative), so the header's ``Path`` value has its real width. The table renders every
    ``---`` header field of the written report in file order, as Step 4 of the profile skill builds it.
    """
    report_dir, _, cwd = profile_run
    timings = tmp_path / "timings.jsonl"
    row = {"ts": "2030-01-01T00:00:01Z", "tool": "Bash", "duration_ms": 200, "session_id": "s1", "args": "command=ls"}
    timings.write_text(json.dumps(row) + "\n", encoding="utf-8")
    invocations = tmp_path / "invocations.jsonl"
    invocations.write_text("\n", encoding="utf-8")
    report = report_dir / "report.md"
    command = [sys.executable, str(ANALYZER), "--timings", str(timings), "--invocations", str(invocations)]
    command += ["--since", "30d", "--output", f"{REPORT_DIR_REL}/report.md"]
    subprocess.run(command, check=True, capture_output=True, text=True, timeout=30, cwd=cwd)
    header = report.read_text(encoding="utf-8").split("---\n", 2)[1]
    rows = [line.partition(":") for line in header.splitlines()]
    table = "| Field | Value |\n| --- | --- |\n" + "".join(
        f"| {key.strip()} | {value.strip()} |\n" for key, _, value in rows
    )
    return cwd, table


@_skip_node_unavailable
class TestFollowUpGate:
    """A follow-up question is denied while an in-flight profile run has no report or no proven report delivery."""

    def test_denies_missing_report(self, tmp_path: Path, profile_run: tuple[Path, Path, str]) -> None:
        """A sentinel without report.md denies the question and names the steps to redo.

        Scenario: the run reached Step 1 but the analyzer never wrote its report, so asking what next is premature.
        """
        report_dir, _, cwd = profile_run

        reason = denial_reason(_run(tmp_path, _ask_payload(cwd=cwd)))

        assert reason is not None, "AskUserQuestion must be denied while the report is missing"
        assert str(report_dir / "report.md") in reason
        assert "Step 4" in reason

    @pytest.mark.parametrize(
        ("report", "transcript"),
        [
            pytest.param("", "", id="zero-byte-report"),
            pytest.param("---\nTitle: profile\n---\n", "", id="written-report-without-delivery"),
            pytest.param("---\nTitle: profile\n---\n", "missing.jsonl", id="unreadable-transcript"),
        ],
    )
    def test_denies_unproven_delivery(
        self, tmp_path: Path, profile_run: tuple[Path, Path, str], report: str, transcript: str
    ) -> None:
        """An empty report, or one whose delivery cannot be verified, denies the follow-up.

        Scenario: a zero-byte report.md counts as not written, a written report with no verified delivery blocks only
        the follow-up transition, and an unreadable transcript leaves delivery unverifiable (fail closed).
        """
        report_dir, _, cwd = profile_run
        (report_dir / "report.md").write_text(report, encoding="utf-8")
        extra = {"transcript_path": str(tmp_path / transcript)} if transcript else {}

        assert denial_reason(_run(tmp_path, _ask_payload(cwd=cwd, **extra))) is not None

    def test_denies_table_in_reply_text_then_allows_question_summary(
        self, tmp_path: Path, profile_run: tuple[Path, Path, str]
    ) -> None:
        """A table printed as reply text before the question is denied; a plain question summary passes.

        Reply text written before a tool call can come back as an empty progress update the user never sees, so only the
        call's own fields count at question time; the denial says so and the call corrected to it passes.
        """
        report_dir, _, cwd = profile_run
        (report_dir / "report.md").write_text("---\nTitle: x\nSince: y\nTop N: z\n---\n", encoding="utf-8")
        table = "| Field | Value |\n| --- | --- |\n| Title | x |\n| Since | y |\n| Top N | z |\n"
        transcript = write_transcript(tmp_path, table)

        reason = denial_reason(_run(tmp_path, _ask_payload(cwd=cwd, transcript_path=str(transcript))))
        retried = _run(tmp_path, _summary_payload(cwd, "Title: x"))

        assert reason is not None
        assert "reply text before the call does not count" in reason
        assert retried == {}

    def test_denies_report_without_table_in_reply(self, tmp_path: Path, profile_run: tuple[Path, Path, str]) -> None:
        """Unverified report delivery must block only the follow-up transition.

        Scenario: the report is written but the reply shows only loose ``Title``/``Since`` lines, not the header table.
        """
        report_dir, _, cwd = profile_run
        (report_dir / "report.md").write_text("---\nTitle: profile\n---\n", encoding="utf-8")
        transcript = write_transcript(tmp_path, "Title: profile\nSince: 24h\n")

        reason = denial_reason(_run(tmp_path, _ask_payload(cwd=cwd, transcript_path=str(transcript))))

        assert reason is not None
        assert "report" in reason


@_skip_node_unavailable
class TestSentinelResolution:
    """The state sentinel and the report dir it names resolve across path spellings, TMPDIR and session sources."""

    def test_resolves_absolute_report_dir(self, tmp_path: Path, profile_run: tuple[Path, Path, str]) -> None:
        """An absolute REPORT_DIR value is honoured as-is, independent of the payload cwd."""
        report_dir, sentinel, _ = profile_run
        sentinel.write_text(_state_file(f"REPORT_DIR={report_dir}"), encoding="utf-8")

        assert denial_reason(_run(tmp_path, _ask_payload(cwd="/nonexistent"))) is not None

    def test_resolves_windows_report_dir_canonically_and_contained(self) -> None:
        """Windows report paths resolve case-insensitively and traversal outside the profile dir is rejected.

        Scenario: the test-only ``resolveReportDir`` export is fed Windows-style paths so the simulated branch runs on
        every host, not just Windows.
        """
        resolved = _call_export("resolveReportDir", r".REPORTS\PROFILE\run-1", r"C:\Repo")

        assert isinstance(resolved, str)
        assert resolved.casefold() == r"c:\repo\.reports\profile\run-1".casefold()
        assert _call_export("resolveReportDir", r".reports\profile\..\private", r"C:\Repo") is None

    def test_resolves_sentinel_under_trailing_slash_tmpdir(
        self, tmp_path: Path, profile_run: tuple[Path, Path, str]
    ) -> None:
        """MacOS exports TMPDIR with a trailing slash — the sentinel must still resolve."""
        _, _, cwd = profile_run

        assert denial_reason(_run(tmp_path, _ask_payload(cwd=cwd), tmpdir_suffix="/")) is not None

    def test_takes_session_id_from_payload(self, tmp_path: Path, profile_run: tuple[Path, Path, str]) -> None:
        """CSID falls back to the payload's session_id when the env var is unset."""
        _, _, cwd = profile_run

        result = _run(tmp_path, _ask_payload(cwd=cwd, session_id=CSID), session_id_env=None)

        assert denial_reason(result) is not None


@_skip_node_unavailable
class TestReportDirParsing:
    """REPORT_DIR is read from the state file the way ``source`` would bind it."""

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
    def test_parses_assignment_form(self, tmp_path: Path, profile_run: tuple[Path, Path, str], line: str) -> None:
        """Every form a shell would bind — quoting, indent, `export` — must not defeat the gate."""
        _, sentinel, cwd = profile_run
        # Preserve the supplied CRLF bytes instead of letting Windows text I/O add
        # an extra carriage return before the test parser receives the sentinel.
        sentinel.write_bytes(_state_file(line).encode("utf-8"))

        assert denial_reason(_run(tmp_path, _ask_payload(cwd=cwd))) is not None

    def test_last_assignment_wins(self, tmp_path: Path, profile_run: tuple[Path, Path, str]) -> None:
        """Sourcing binds the final assignment — an earlier stale one must not shadow it.

        Scenario: the state file assigns a 1999 report dir first and the real one last; the denial must name the
        real dir's report.md.
        """
        report_dir, sentinel, cwd = profile_run
        stale = ".reports/profile/1999-01-01T00-00-00Z"
        sentinel.write_text(f"REPORT_DIR={stale}\n{_state_file(f'REPORT_DIR={REPORT_DIR_REL}')}", encoding="utf-8")

        reason = denial_reason(_run(tmp_path, _ask_payload(cwd=cwd)))

        assert reason is not None
        assert str(report_dir / "report.md") in reason

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
    def test_passes_unparsable_report_dir(self, tmp_path: Path, profile_run: tuple[Path, Path, str], line: str) -> None:
        """No binding the shell would make → the hook cannot name a dir, so it allows."""
        _, sentinel, cwd = profile_run
        sentinel.write_text(_state_file(line), encoding="utf-8")

        assert _run(tmp_path, _ask_payload(cwd=cwd)) == {}


@_skip_node_unavailable
class TestPassThrough:
    """Everything outside an in-flight profile run's follow-up question passes untouched: empty stdout, exit 0."""

    def test_passes_without_sentinel(self, tmp_path: Path) -> None:
        """No foundry:profile run reached Step 1 → unrelated questions are never gated."""
        assert _run(tmp_path, _ask_payload()) == {}

    @pytest.mark.parametrize(
        "overrides",
        [
            pytest.param({"tool_name": "Bash"}, id="other-tool"),
            pytest.param({"hook_event_name": "PostToolUse"}, id="other-event"),
        ],
    )
    def test_passes_non_matching_payload(
        self, tmp_path: Path, profile_run: tuple[Path, Path, str], overrides: dict
    ) -> None:
        """Only PreToolUse AskUserQuestion is inspected; anything else is untouched."""
        _, _, cwd = profile_run

        assert _run(tmp_path, _ask_payload(cwd=cwd, **overrides)) == {}

    def test_passes_stale_sentinel(self, tmp_path: Path, profile_run: tuple[Path, Path, str]) -> None:
        """A sentinel older than the enforcement window is treated as a crashed run.

        Scenario: the sentinel's mtime is set past the two-hour window, so a leftover from a dead session cannot
        gate a later, unrelated question.
        """
        _, sentinel, cwd = profile_run
        stale = time.time() - (TWO_HOURS_S + 600)
        os.utime(sentinel, (stale, stale))

        assert _run(tmp_path, _ask_payload(cwd=cwd)) == {}

    def test_passes_vanished_report_dir(self, tmp_path: Path, profile_run: tuple[Path, Path, str]) -> None:
        """Report dir gone (worktree removed, TTL cleanup) → hook cannot judge, allows."""
        report_dir, _, cwd = profile_run
        report_dir.rmdir()

        assert _run(tmp_path, _ask_payload(cwd=cwd)) == {}

    @pytest.mark.parametrize(
        "content", ["", "   \n", "REPORT_DIR=.reports/audit/2026-08-04T10-00-00Z\n", "REPORT_DIR=/etc\n"]
    )
    def test_passes_implausible_sentinel_content(self, tmp_path: Path, content: str) -> None:
        """A REPORT_DIR not under .reports/profile/ is ignored."""
        (tmp_path / SENTINEL_NAME).write_text(content, encoding="utf-8")

        assert _run(tmp_path, _ask_payload(cwd=str(tmp_path))) == {}

    @pytest.mark.parametrize("session_id", ["../../etc/passwd", "has space", ""])
    def test_passes_unsafe_csid(self, tmp_path: Path, profile_run: tuple[Path, Path, str], session_id: str) -> None:
        """A CSID that cannot name a sentinel file is discarded, never path-joined."""
        _, _, cwd = profile_run

        assert _run(tmp_path, _ask_payload(cwd=cwd), session_id_env=session_id) == {}

    def test_passes_malformed_stdin(self, tmp_path: Path) -> None:
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


@_skip_node_unavailable
class TestStopDelivery:
    """The Stop hook still checks report delivery when the follow-up question is skipped."""

    def test_blocks_undelivered_report_once(self, tmp_path: Path, written_run: str) -> None:
        """A turn ending without the report delivery is kept going once; the same report never re-blocks.

        Scenario: the report is written but the final message is just ``done``; the first Stop blocks, the second
        for the same report must let the session end instead of looping.
        """
        first = _run(tmp_path, stop_payload(cwd=written_run))
        second = _run(tmp_path, stop_payload(cwd=written_run))

        assert first.get("decision") == "block"
        assert "foundry:profile" in first["reason"]
        assert second == {}

    def test_passes_delivery_in_final_message(self, tmp_path: Path, written_run: str) -> None:
        """The Stop payload's final message proves delivery even without a transcript."""
        assert _run(tmp_path, stop_payload(cwd=written_run, last_assistant_message=DELIVERED)) == {}


@_skip_node_unavailable
class TestQuestionSummaryDelivery:
    """Follow-ups show summaries in question text and leave full delivery to Stop."""

    def test_plain_summary_allows_follow_up(self, tmp_path: Path, written_run: str) -> None:
        """The matching headline and resolved report path pass without any preview."""
        payload = _summary_payload(written_run, "Title: x")

        assert all("preview" not in option for option in payload["tool_input"]["questions"][0]["options"])
        assert _run(tmp_path, payload) == {}

    def test_missing_summary_denial_names_question_fix(self, tmp_path: Path, written_run: str) -> None:
        """Missing summary evidence names the first-question remedy.

        Scenario: the question text carries no headline at all, so the denial must tell the model where to put it.
        """
        reason = denial_reason(_run(tmp_path, _summary_payload(written_run, None)))

        assert reason is not None
        assert "first question's `question` text" in reason
        assert "fixed lines" in reason

    def test_summary_in_one_preview_is_denied_and_question_fix_passes(self, tmp_path: Path, written_run: str) -> None:
        """A summary in one preview must move into the first question's text.

        Scenario: the headline sits only in the first option's preview; the denial names the remedy and the call
        corrected to put it in the question text passes.
        """
        partial = _move_summary_to_previews(_summary_payload(written_run, "Title: x"), count=1)

        reason = denial_reason(_run(tmp_path, partial))
        corrected = _run(tmp_path, _summary_payload(written_run, "Title: x"))

        assert reason is not None
        assert "first question's `question` text" in reason
        assert corrected == {}

    def test_shown_summary_keeps_stop_armed_once(self, tmp_path: Path, written_run: str) -> None:
        """A shown summary leaves one Stop continuation for the full report.

        Scenario: question allowed, PostToolUse recorded, then Stop blocks once for the full report, passes once the
        report is delivered, and never blocks again.
        """
        payload = _summary_payload(written_run, "Title: x")
        assert _run(tmp_path, payload) == {}
        assert _run(tmp_path, {**payload, "hook_event_name": "PostToolUse"}) == {}

        blocked = _run(tmp_path, stop_payload(cwd=written_run))
        delivered = _run(tmp_path, stop_payload(cwd=written_run, last_assistant_message=DELIVERED))
        again = _run(tmp_path, stop_payload(cwd=written_run))

        assert (blocked.get("decision"), delivered, again) == ("block", {}, {})

    def test_allowed_but_unshown_follow_up_leaves_stop_armed(self, tmp_path: Path, written_run: str) -> None:
        """An allowed but unshown question also leaves final delivery armed."""
        allowed = _run(tmp_path, _summary_payload(written_run, "Title: x"))
        stopped = _run(tmp_path, stop_payload(cwd=written_run))

        assert (allowed, stopped.get("decision")) == ({}, "block")

    def test_summary_only_in_previews_is_denied(self, tmp_path: Path, written_run: str) -> None:
        """Even every option's preview cannot replace the question's plain summary."""
        payload = _move_summary_to_previews(_summary_payload(written_run, "Title: x"), count=2)

        assert denial_reason(_run(tmp_path, payload)) is not None


@_skip_node_unavailable
def test_real_analyzer_header_in_question_summary_allows_follow_up(
    tmp_path: Path, analyzed_run: tuple[str, str]
) -> None:
    """The real timing analyzer's header passes as a plain summary, and its full table passes Stop.

    Scenario: ``timing_analyzer.py`` runs for real on one timing row, so the header field widths and paths are the
    ones production emits; the hook accepts the question summary, then the delivered table at Stop, then stays quiet.
    """
    cwd, table = analyzed_run
    summary = "Title: foundry:profile — session clock-time breakdown\nOutcome: INFORMATIONAL — data-only report"
    payload = _summary_payload(cwd, summary)
    asked = _run(tmp_path, payload)
    recorded = _run(tmp_path, {**payload, "hook_event_name": "PostToolUse"})
    delivered = _run(tmp_path, stop_payload(cwd=cwd, last_assistant_message=table))
    again = _run(tmp_path, stop_payload(cwd=cwd))

    assert (asked, recorded, delivered, again) == ({}, {}, {}, {})
