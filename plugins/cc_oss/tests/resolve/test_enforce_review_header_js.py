"""Subprocess tests for ``hooks/enforce-review-header.js``.

The hook is a ``PreToolUse`` gate on ``AskUserQuestion``. Its contract:

* **Scoped to in-flight reviews** — it acts only when the review-state sentinel
  ``${TMPDIR:-/tmp}/oss-review-report-dir-<CSID>`` exists; without it every
  ``AskUserQuestion`` passes through untouched (empty stdout, exit 0).
* **Denies a missing report** — sentinel present but
  ``$REPORT_DIR/review-report.md`` absent or empty means the findings were not
  consolidated and the report header was not printed, so the call is denied
  with an actionable reason.
* **Fails open** — stale sentinel, implausible sentinel content, vanished report
  dir, unparsable payload: every can't-tell case allows the call.

Every test runs the real hook under ``node``: the hook's end-to-end behaviour is the contract, so nothing is mocked.
"""

from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from _hook_payloads import (
    DELIVERED,
    TWO_HOURS_S,
    ask_payload,
    call_export,
    denial_reason,
    stop_payload,
    write_transcript,
)

HOOK = Path(__file__).resolve().parents[2] / "hooks" / "enforce-review-header.js"
_ask_payload = functools.partial(ask_payload, "oss-review")
_call_export = functools.partial(call_export, HOOK)

CSID = "test-session-1234"
SENTINEL_NAME = f"oss-review-report-dir-{CSID}"
NODE_UNAVAILABLE = shutil.which("node") is None
_skip_node_unavailable = pytest.mark.skipif(NODE_UNAVAILABLE, reason="requires node to execute the hook")

#: Minimal report header a consolidator writes, and the table the final reply must show for it.
_REPORT_HEADER = "---\nTitle: x\nPR: #1\nDate: y\n---\n"


def _summary_payload(report: Path, summary: str | None) -> dict:
    """Build the follow-up with a plain question summary and no previews."""
    options = [{"label": label, "description": "next step"} for label in ("/oss:resolve 1", "skip")]
    text = f"{summary}\n→ {report}\nWhat next?" if summary else "What next?"
    questions = [{"question": text, "header": "oss-review", "options": options}]
    return _ask_payload(tool_input={"questions": questions}, transcript_path=None)


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


def _write_shipped_report(report_dir: Path) -> tuple[Path, str, str]:
    """Write the shipped template's `---` header as the report; return its path, full header table and summary lines."""
    template = HOOK.parent.parent / "skills" / "review" / "templates" / "review-report.md"
    header = template.read_text(encoding="utf-8").split("---", 2)[1].strip()
    report = report_dir / "review-report.md"
    report.write_text(f"---\n{header}\n---\n", encoding="utf-8")
    rows = [line.partition(":") for line in header.splitlines()]
    table = "| Field | Value |\n| --- | --- |\n" + "".join(
        "| {} | {} |\n".format(key.strip(), value.strip().replace("|", r"\|")) for key, _, value in rows
    )
    fields = dict(line.partition(":")[::2] for line in header.splitlines() if ":" in line)
    summary = f"Title: {fields['Title'].strip()}\nOutcome: {fields['Outcome'].strip()}"
    return report, table, summary


@pytest.fixture(name="review_run")
def _review_run(tmp_path: Path) -> tuple[Path, Path]:
    """Stage a review that reached Step 2: report directory on disk plus its sentinel."""
    report_dir = tmp_path / "repo" / ".reports" / "review" / "2026-08-04T10-00-00Z"
    report_dir.mkdir(parents=True)
    sentinel = tmp_path / SENTINEL_NAME
    sentinel.write_text(f"{report_dir}\n", encoding="utf-8")
    return report_dir, sentinel


@pytest.fixture(name="written_run")
def _written_run(review_run: tuple[Path, Path]) -> Path:
    """Stage a review whose consolidator wrote the report that ``DELIVERED`` renders; return the report path."""
    report_dir, _ = review_run
    report = report_dir / "review-report.md"
    report.write_text(_REPORT_HEADER, encoding="utf-8")
    return report


@_skip_node_unavailable
class TestAskGate:
    """An ``AskUserQuestion`` during an in-flight review is denied until the report is written and shown."""

    def test_missing_report_is_denied(self, tmp_path: Path, review_run: tuple[Path, Path]) -> None:
        """Sentinel present without review-report.md → deny, naming the step to redo.

        The review reached Step 2 (sentinel written) but the consolidator never produced a report, so asking the user
        what to do next would skip the report entirely.
        """
        report_dir, _ = review_run

        reason = denial_reason(_run(tmp_path, _ask_payload()))

        assert reason is not None, "AskUserQuestion must be denied while the report is missing"
        assert str(report_dir / "review-report.md") in reason
        assert "Step 5b" in reason

    @pytest.mark.parametrize(
        ("report_text", "extra"),
        [
            pytest.param("", {}, id="empty-report"),
            pytest.param("---\nTitle: oss-review\n---\n", {}, id="written-report-without-transcript"),
            pytest.param(
                "---\nTitle: oss-review\n---\n",
                {"transcript_path": "/nonexistent/transcript.jsonl"},
                id="written-report-with-unreadable-transcript",
            ),
        ],
    )
    def test_unverified_report_delivery_is_denied(
        self, tmp_path: Path, review_run: tuple[Path, Path], report_text: str, extra: dict
    ) -> None:
        """A zero-byte report, or one whose delivery cannot be verified, blocks only the follow-up transition.

        An empty file counts as not written; a written report with no transcript, or a transcript that cannot be read,
        gives the hook nothing proving the header table was shown, so it cannot let the follow-up through.
        """
        report_dir, _ = review_run
        (report_dir / "review-report.md").write_text(report_text, encoding="utf-8")

        reason = denial_reason(_run(tmp_path, _ask_payload(**extra)))

        assert reason is not None

    def test_report_without_table_in_transcript_is_denied(self, tmp_path: Path, written_run: Path) -> None:
        """A report on disk whose header table never appears in the transcript is denied, naming the report.

        The assistant text holds plain ``Title: ...`` lines instead of the table, which does not count as delivery.
        """
        transcript = write_transcript(tmp_path, "Title: oss-review\nPR: #1303\nDate: 2026-08-08\n")

        result = _run(tmp_path, _ask_payload(transcript_path=str(transcript)))

        hook_output = result.get("hookSpecificOutput", {})
        assert hook_output.get("permissionDecision") == "deny"
        assert "report" in hook_output.get("permissionDecisionReason", "")

    def test_table_only_in_reply_text_is_denied_and_summary_passes(self, tmp_path: Path, written_run: Path) -> None:
        """A table printed as reply text before the question is denied; a plain question summary passes.

        Reply text written before a tool call can come back as an empty progress update the user never sees, so only the
        call's own fields count at question time; the denial says so and the call corrected to it passes.
        """
        transcript = write_transcript(tmp_path, DELIVERED)

        reason = denial_reason(_run(tmp_path, _ask_payload(transcript_path=str(transcript))))
        retried = _run(tmp_path, _summary_payload(written_run, "Title: x"))

        assert reason is not None
        assert "reply text before the call does not count" in reason
        assert retried == {}

    @pytest.mark.parametrize(
        ("reviewers_row", "blocked"),
        [
            pytest.param("", True, id="reviewer-row-missing-blocks"),
            pytest.param(
                "| Reviewers | Software engineer (3), QA specialist (2). |\n", False, id="reviewer-row-present-passes"
            ),
        ],
    )
    def test_reviewer_header_row_must_be_delivered_in_final_reply(
        self, tmp_path: Path, review_run: tuple[Path, Path], reviewers_row: str, blocked: bool
    ) -> None:
        """Stop requires the reviewer row even though the follow-up uses a shorter summary.

        The report header lists the reviewers; the follow-up question shows only a short summary and passes, so the
        final reply is the only place the full table (including that row) must appear before Stop lets the turn end.
        """
        report_dir, _ = review_run
        report = report_dir / "review-report.md"
        report.write_text(
            "---\nTitle: review\nDate: 2026-09-22\nReviewers: Software engineer (3), QA specialist (2).\n---\n",
            encoding="utf-8",
        )
        assert _run(tmp_path, _summary_payload(report, "Title: review")) == {}
        table = "| Field | Value |\n| --- | --- |\n| Title | review |\n| Date | 2026-09-22 |\n" + reviewers_row

        result = _run(tmp_path, stop_payload(last_assistant_message=table))

        assert (result.get("decision") == "block") is blocked

    @pytest.mark.parametrize(
        ("payload_extra", "run_kwargs"),
        [
            pytest.param({}, {"tmpdir_suffix": "/"}, id="trailing-slash-tmpdir"),
            pytest.param({"session_id": CSID}, {"session_id_env": None}, id="session-id-from-payload"),
        ],
    )
    def test_sentinel_resolves_despite_environment_variation(
        self, tmp_path: Path, review_run: tuple[Path, Path], payload_extra: dict, run_kwargs: dict
    ) -> None:
        """The sentinel is found when TMPDIR ends in a slash or the session id arrives only in the payload.

        macOS exports TMPDIR with a trailing slash, and the CSID falls back to the payload's ``session_id`` when the env
        var is unset; in both cases the review is still recognised and the missing report is denied.
        """
        reason = denial_reason(_run(tmp_path, _ask_payload(**payload_extra), **run_kwargs))

        assert reason is not None


@_skip_node_unavailable
class TestShippedHeader:
    """The review report header that the shipped template defines is accepted as the question summary."""

    def test_is_accepted_as_question_summary(self, tmp_path: Path, review_run: tuple[Path, Path]) -> None:
        """The actual shipped header supplies a fixed summary without requiring its large table in previews.

        Title and Outcome from the shipped template are enough for the follow-up question; the hook must not demand the
        whole table there.
        """
        report, _, summary = _write_shipped_report(review_run[0])

        assert _run(tmp_path, _summary_payload(report, summary)) == {}

    def test_optional_preview_is_over_the_cap(self, tmp_path: Path, review_run: tuple[Path, Path]) -> None:
        """An optional full-table preview remains capped even beside a valid question summary.

        The same summary payload passes without a preview (previous test); adding the whole shipped table as an option
        preview exceeds the preview cap and is denied for that reason, not as an incomplete report.
        """
        report, table, summary = _write_shipped_report(review_run[0])
        payload = _summary_payload(report, summary)
        payload["tool_input"]["questions"][0]["options"][0]["preview"] = table

        reason = denial_reason(_run(tmp_path, payload))

        assert reason is not None
        assert "exceed the preview cap (question 1, option 1:" in reason
        assert "incomplete" not in reason


@_skip_node_unavailable
def test_simulated_windows_report_dir_validation_accepts_contained_paths_and_rejects_traversal() -> None:
    """Recognise Windows separators and casing without trusting escaped sentinel paths.

    Calls the hook's exported validator with Windows-style paths on any host: a contained path in any letter case is
    accepted and one that climbs out with ``..`` is rejected.
    """
    assert _call_export("isReviewReportDir", r"C:\Repo\.REPORTS\REVIEW\run-1") is True
    assert _call_export("isReviewReportDir", r"C:\Repo\.reports\review\..\private") is False


@_skip_node_unavailable
class TestPassThrough:
    """Everything outside an in-flight review, and every case the hook cannot judge, is allowed untouched."""

    def test_no_sentinel_passes_through(self, tmp_path: Path) -> None:
        """No oss:review run reached Step 2 → unrelated questions are never gated.

        Without the sentinel the session is not in a review, so any AskUserQuestion gets empty output.
        """
        assert _run(tmp_path, _ask_payload()) == {}

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param(_ask_payload(tool_name="Bash"), id="other-tool"),
            pytest.param(_ask_payload(hook_event_name="PostToolUse"), id="other-event"),
        ],
    )
    def test_non_matching_payloads_pass_through(
        self, tmp_path: Path, review_run: tuple[Path, Path], payload: dict
    ) -> None:
        """Only PreToolUse AskUserQuestion is inspected; anything else is untouched.

        A review is in flight with no report, yet a different tool or event is never denied.
        """
        assert _run(tmp_path, payload) == {}

    def test_stale_sentinel_passes_through(self, tmp_path: Path, review_run: tuple[Path, Path]) -> None:
        """Sentinel older than the enforcement window is treated as a crashed run.

        The sentinel's mtime is set ten minutes past the two-hour window, so the missing report is no longer enforced.
        """
        _, sentinel = review_run
        stale = time.time() - (TWO_HOURS_S + 600)
        os.utime(sentinel, (stale, stale))

        assert _run(tmp_path, _ask_payload()) == {}

    def test_missing_report_dir_passes_through(self, tmp_path: Path, review_run: tuple[Path, Path]) -> None:
        """Report dir gone (worktree removed, TTL cleanup) → hook cannot judge, allows.

        The sentinel still names the directory, but it no longer exists, so there is nothing to check a report in.
        """
        report_dir, _ = review_run
        report_dir.rmdir()

        assert _run(tmp_path, _ask_payload()) == {}

    @pytest.mark.parametrize("content", ["", "   \n", "repo/.reports/review/2026-08-04T10-00-00Z\n", "/etc\n"])
    def test_implausible_sentinel_content_passes_through(self, tmp_path: Path, content: str) -> None:
        """Sentinel not holding an absolute .reports/review/ path is ignored.

        Blank, relative and unrelated absolute paths must never be treated as a report directory to inspect.
        """
        (tmp_path / SENTINEL_NAME).write_text(content, encoding="utf-8")

        assert _run(tmp_path, _ask_payload()) == {}

    @pytest.mark.parametrize("session_id", ["../../etc/passwd", "has space", ""])
    def test_unsafe_csid_passes_through(self, tmp_path: Path, review_run: tuple[Path, Path], session_id: str) -> None:
        """A CSID that cannot name a sentinel file is discarded, never path-joined.

        Path traversal, whitespace and an empty id would otherwise build a sentinel path outside the temp directory.
        """
        assert _run(tmp_path, _ask_payload(), session_id_env=session_id) == {}

    def test_malformed_stdin_passes_through(self, tmp_path: Path) -> None:
        """A hook bug or unparsable payload must never strand the session.

        Garbage on stdin exits 0 with no output, so the question goes ahead.
        """
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
class TestStopGate:
    """The Stop event checks report delivery when the follow-up question was skipped."""

    def test_blocks_undelivered_report_once(self, tmp_path: Path, written_run: Path) -> None:
        """A turn ending without the report delivery is kept going once; the same report never re-blocks.

        The first Stop with a final message lacking the header table is blocked; the identical second Stop passes, so a
        stubborn turn cannot loop forever.
        """
        result = _run(tmp_path, stop_payload())

        assert result.get("decision") == "block"
        assert "oss:review" in result["reason"]
        assert _run(tmp_path, stop_payload()) == {}

    def test_passes_delivery_in_final_message(self, tmp_path: Path, written_run: Path) -> None:
        """The Stop payload's final message proves delivery even without a transcript.

        The final message holds the header table, so the turn may end.
        """
        assert _run(tmp_path, stop_payload(last_assistant_message=DELIVERED)) == {}

    @pytest.mark.parametrize(
        ("report_name", "sentinel_template"),
        [
            pytest.param("review-report.md", "", id="publication-gate-blocked-sentinel-truncated"),
            pytest.param("review-report.refused.md", "{report_dir}\n", id="refused-report-parked-for-respawn"),
        ],
    )
    def test_ignores_report_refused_by_publication_gate(
        self, tmp_path: Path, review_run: tuple[Path, Path], report_name: str, sentinel_template: str
    ) -> None:
        """A refused report is never pushed for delivery, whether the review stopped BLOCKED or awaits a re-spawn.

        When the gate stops BLOCKED it truncates the report-dir sentinel while the written report stays on disk for
        inspection: no review is in flight. When the consolidator is re-spawned (a background spawn, so the sentinel
        stays live) the gate first parks the refused report as ``review-report.refused.md``: nothing is deliverable
        until the corrected report is written. Either way the Stop hook must not demand the header table.
        """
        report_dir, sentinel = review_run
        (report_dir / report_name).write_text(_REPORT_HEADER, encoding="utf-8")
        sentinel.write_text(sentinel_template.format(report_dir=report_dir), encoding="utf-8")

        assert _run(tmp_path, stop_payload()) == {}


@_skip_node_unavailable
class TestQuestionSummaryDelivery:
    """Follow-ups show summaries in question text and leave full delivery to Stop."""

    def test_plain_summary_allows_follow_up(self, tmp_path: Path, written_run: Path) -> None:
        """The matching headline and resolved report path pass without any preview.

        The payload carries the Title line, the report path and the question, and no option has a preview.
        """
        payload = _summary_payload(written_run, "Title: x")
        assert all("preview" not in option for option in payload["tool_input"]["questions"][0]["options"])

        assert _run(tmp_path, payload) == {}

    def test_missing_summary_denial_names_question_fix(self, tmp_path: Path, written_run: Path) -> None:
        """Missing summary evidence names the first-question remedy.

        A follow-up whose question text has no summary is denied, and the reason says where the lines belong.
        """
        reason = denial_reason(_run(tmp_path, _summary_payload(written_run, None)))

        assert reason is not None
        assert "first question's `question` text" in reason
        assert "fixed lines" in reason

    def test_shown_summary_keeps_stop_armed_once(self, tmp_path: Path, written_run: Path) -> None:
        """A shown summary leaves one Stop continuation for the full report.

        After the question is allowed and shown, a Stop without the table is blocked once; delivering the table, or a
        repeat Stop, then passes.
        """
        payload = _summary_payload(written_run, "Title: x")
        assert _run(tmp_path, payload) == {}
        assert _run(tmp_path, {**payload, "hook_event_name": "PostToolUse"}) == {}

        blocked = _run(tmp_path, stop_payload())

        assert blocked.get("decision") == "block"
        assert _run(tmp_path, stop_payload(last_assistant_message=DELIVERED)) == {}
        assert _run(tmp_path, stop_payload()) == {}

    def test_allowed_but_unshown_follow_up_leaves_stop_armed(self, tmp_path: Path, written_run: Path) -> None:
        """An allowed but unshown question also leaves final delivery armed.

        The question passes but no PostToolUse follows, so the hook must still require the table at Stop.
        """
        assert _run(tmp_path, _summary_payload(written_run, "Title: x")) == {}

        assert _run(tmp_path, stop_payload()).get("decision") == "block"

    def test_summary_only_in_previews_is_denied(self, tmp_path: Path, written_run: Path) -> None:
        """Even every option's preview cannot replace the question's plain summary.

        The summary text moves from the question into each option's preview; the question is left bare, which is denied.
        """
        payload = _summary_payload(written_run, "Title: x")
        first = payload["tool_input"]["questions"][0]
        first["options"] = [{**option, "preview": first["question"]} for option in first["options"]]
        first["question"] = "What next?"

        assert denial_reason(_run(tmp_path, payload)) is not None
