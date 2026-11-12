"""Subprocess tests for ``hooks/enforce-review-header.js``.

The hook is a ``PreToolUse`` gate on ``AskUserQuestion``. Its contract:

* **Scoped to in-flight reviews** — it acts only when the review-state sentinel
  ``${TMPDIR:-/tmp}/dev-review-report-dir-<CSID>`` exists; without it every
  ``AskUserQuestion`` passes through untouched (empty stdout, exit 0).
* **Denies a missing report** — sentinel present but
  ``$REPORT_DIR/review-report.md`` absent or empty means the findings were not
  consolidated and the report header was not printed, so the call is denied
  with an actionable reason.
* **Fails open** — stale sentinel, implausible sentinel content, vanished report
  dir, unparsable payload: every can't-tell case allows the call.

Every test runs the shipped hook under real Node: the hook's end-to-end behaviour is the contract.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[2] / "hooks" / "enforce-review-header.js"

CSID = "test-session-1234"
SENTINEL_NAME = f"dev-review-report-dir-{CSID}"
TWO_HOURS_S = 2 * 60 * 60
HEADER = "---\nTitle: x\nPR: #1\nDate: y\n---\n"
DELIVERED = "| Field | Value |\n| --- | --- |\n| Title | x |\n| PR | #1 |\n| Date | y |\n"

_skip_node_unavailable = pytest.mark.skipif(shutil.which("node") is None, reason="requires node to execute the hook")


def _ask_payload(**overrides: object) -> dict:
    """Build a PreToolUse AskUserQuestion payload, applying `overrides`."""
    payload: dict = {
        "hook_event_name": "PreToolUse",
        "tool_name": "AskUserQuestion",
        "tool_input": {"questions": [{"question": "What next?", "header": "dev-review"}]},
    }
    payload.update(overrides)
    return payload


def _stop_payload(**overrides: object) -> dict:
    """Build a Stop payload whose final message lacks the header, applying `overrides`."""
    payload: dict = {"hook_event_name": "Stop", "stop_hook_active": False, "last_assistant_message": "done"}
    payload.update(overrides)
    return payload


def _summary_payload(report: Path, summary: str | None) -> dict:
    """Build the follow-up with a plain question summary and no previews."""
    options = [{"label": label, "description": "next step"} for label in ("walk through findings", "skip")]
    text = f"{summary}\n→ {report}\nWhat next?" if summary else "What next?"
    questions = [{"question": text, "header": "dev-review", "options": options}]
    return _ask_payload(tool_input={"questions": questions}, transcript_path=None)


def _run(tmp_path: Path, payload: dict, *, session_id_env: str | None = CSID, tmpdir_suffix: str = "") -> dict:
    """Invoke the hook with `payload` on stdin and return parsed stdout (or {})."""
    env = {**os.environ, "TMPDIR": f"{tmp_path}{tmpdir_suffix}"}
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    if session_id_env is not None:
        env["CLAUDE_CODE_SESSION_ID"] = session_id_env
    proc = subprocess.run(
        ["node", str(HOOK)], input=json.dumps(payload), capture_output=True, text=True, timeout=10, env=env
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


@_skip_node_unavailable
def test_develop_report_namespace_blocks_followup_before_report_exists(tmp_path: Path) -> None:
    """A relocated develop review retains its delivery gate instead of silently falling through."""
    report_dir = tmp_path / "repo/.reports/develop-review/2026-11-12T00-00-00Z"
    report_dir.mkdir(parents=True)
    (tmp_path / SENTINEL_NAME).write_text(str(report_dir) + "\n", encoding="utf-8")
    result = _run(tmp_path, _ask_payload())
    reason = _denial_reason(result)
    assert reason is not None
    assert str(report_dir / "review-report.md") in reason


def _call_export(name: str, *args: object) -> object:
    """Call one test-only hook export in a separate Node process."""
    script = "const hook = require(process.argv[1]); process.stdout.write(JSON.stringify(hook[process.argv[2]](...JSON.parse(process.argv[3]))));"
    proc = subprocess.run(
        ["node", "-e", script, str(HOOK), name, json.dumps(args)],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    return json.loads(proc.stdout)


def _write_transcript(tmp_path: Path, assistant_text: str) -> Path:
    """Write a minimal two-row JSONL transcript: a user turn then one assistant text block."""
    rows = [
        {"type": "user", "message": {"content": [{"type": "text", "text": "go"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": assistant_text}]}},
    ]
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return transcript


@pytest.fixture(name="review_run")
def _review_run(tmp_path: Path) -> tuple[Path, Path]:
    """Stage a review that reached Step 2: report directory on disk plus its sentinel."""
    report_dir = tmp_path / "repo" / ".reports" / "review" / "2026-08-04T10-00-00Z"
    report_dir.mkdir(parents=True)
    sentinel = tmp_path / SENTINEL_NAME
    sentinel.write_text(f"{report_dir}\n", encoding="utf-8")
    return report_dir, sentinel


@pytest.fixture
def stage_report(review_run: tuple[Path, Path]) -> Callable[[str], Path]:
    """Return a function writing the run's ``review-report.md`` with the given content and returning its path."""

    def write(content: str) -> Path:
        """Write the report and return its path."""
        report = review_run[0] / "review-report.md"
        report.write_text(content, encoding="utf-8")
        return report

    return write


@pytest.fixture(name="written_run")
def _written_run(stage_report: Callable[[str], Path]) -> Path:
    """Stage a review whose consolidator wrote the report DELIVERED renders; return the report path."""
    return stage_report(HEADER)


@pytest.fixture
def shipped_report(stage_report: Callable[[str], Path]) -> tuple[Path, str, str]:
    """Write the shipped template's `---` header as the run's report; return its path, header table and summary."""
    template = HOOK.parent.parent / "skills" / "review" / "templates" / "review-report.md"
    header = template.read_text(encoding="utf-8").split("---", 2)[1].strip()
    report = stage_report(f"---\n{header}\n---\n")
    fields = dict(line.partition(":")[::2] for line in header.splitlines() if ":" in line)
    rows = [line.partition(":") for line in header.splitlines()]
    table = "| Field | Value |\n| --- | --- |\n" + "".join(
        "| {} | {} |\n".format(key.strip(), value.strip().replace("|", r"\|")) for key, _, value in rows
    )
    summary = f"Title: {fields['Title'].strip()}\nOutcome: {fields['Outcome'].strip()}"
    return report, table, summary


@_skip_node_unavailable
class TestFollowUpGate:
    """AskUserQuestion during an in-flight review is denied until the report exists and its delivery is evidenced."""

    def test_missing_report_is_denied(self, tmp_path: Path, review_run: tuple[Path, Path]) -> None:
        """Sentinel present without review-report.md → deny, naming the step to redo and the develop skill.

        The reason starts with ``develop:review report gate`` so the oss gate is not blamed for the denial.
        """
        report_dir, _ = review_run
        reason = _denial_reason(_run(tmp_path, _ask_payload()))
        assert reason is not None, "AskUserQuestion must be denied while the report is missing"
        assert str(report_dir / "review-report.md") in reason
        assert "Step 5b" in reason
        assert reason.startswith("develop:review report gate")

    @pytest.mark.parametrize(
        ("report", "transcript"),
        [
            pytest.param("", None, id="empty-report"),
            pytest.param("---\nTitle: develop-review\n---\n", None, id="written-without-transcript"),
            pytest.param(
                "---\nTitle: develop-review\n---\n",
                lambda tmp_path: _write_transcript(tmp_path, "Title: develop-review\nDate: 2026-08-08\n"),
                id="written-transcript-without-table",
            ),
            pytest.param(
                "---\nTitle: develop-review\n---\n",
                lambda tmp_path: tmp_path / "missing.jsonl",
                id="written-transcript-unreadable",
            ),
        ],
    )
    def test_report_without_delivery_evidence_is_denied(
        self,
        tmp_path: Path,
        stage_report: Callable[[str], Path],
        report: str,
        transcript: Callable[[Path], Path] | None,
    ) -> None:
        """A zero-byte report counts as not written, and a written one needs delivery evidence before the follow-up.

        Unverified delivery blocks only the follow-up transition: no transcript, a transcript without the table, and an
        unreadable transcript all deny, so the user never sees a question before the report header.
        """
        stage_report(report)
        overrides = {} if transcript is None else {"transcript_path": str(transcript(tmp_path))}
        reason = _denial_reason(_run(tmp_path, _ask_payload(**overrides)))
        assert reason is not None
        assert "report" in reason

    def test_table_only_in_reply_text_is_denied_and_summary_passes(self, tmp_path: Path, written_run: Path) -> None:
        """A table printed as reply text before the question is denied; a plain question summary passes.

        Reply text written before a tool call can come back as an empty progress update the user never sees, so only the
        call's own fields count at question time; the denial says so and the call corrected to it passes.
        """
        transcript = _write_transcript(tmp_path, DELIVERED)
        reason = _denial_reason(_run(tmp_path, _ask_payload(transcript_path=str(transcript))))
        retried = _run(tmp_path, _summary_payload(written_run, "Title: x"))
        assert reason is not None
        assert "reply text before the call does not count" in reason
        assert retried == {}

    def test_shipped_review_header_is_accepted_as_question_summary(
        self, tmp_path: Path, shipped_report: tuple[Path, str, str]
    ) -> None:
        """The actual shipped header supplies a fixed summary without requiring its large table in previews.

        Built from the shipped report template, so a template change that breaks the summary contract fails here.
        """
        report, _, summary = shipped_report
        assert _run(tmp_path, _summary_payload(report, summary)) == {}

    def test_shipped_review_header_optional_preview_is_over_the_cap(
        self, tmp_path: Path, shipped_report: tuple[Path, str, str]
    ) -> None:
        """An optional full-table preview remains capped even beside a valid question summary.

        The denial names the offending option; dropping the preview from the same payload then passes.
        """
        report, table, summary = shipped_report
        payload = _summary_payload(report, summary)
        payload["tool_input"]["questions"][0]["options"][0]["preview"] = table
        reason = _denial_reason(_run(tmp_path, payload))
        del payload["tool_input"]["questions"][0]["options"][0]["preview"]
        without_preview = _run(tmp_path, payload)
        assert reason is not None
        assert "exceed the preview cap (question 1, option 1:" in reason
        assert "incomplete" not in reason
        assert without_preview == {}

    def test_missing_report_allows_recovery_question(self, tmp_path: Path, review_run: tuple[Path, Path]) -> None:
        """An in-flight failed producer must retain its user-directed recovery path.

        A question without the review header fields (no ``header``) is the producer-failure recovery ask; it passes.
        """
        payload = _ask_payload(tool_input={"questions": [{"question": "Producer failed. Retry or stop?"}]})
        assert _run(tmp_path, payload) == {}

    def test_trailing_slash_tmpdir_resolves_sentinel(self, tmp_path: Path, review_run: tuple[Path, Path]) -> None:
        """MacOS exports TMPDIR with a trailing slash — the sentinel must still resolve."""
        assert _denial_reason(_run(tmp_path, _ask_payload(), tmpdir_suffix="/")) is not None

    def test_sentinel_resolved_from_payload_session_id(self, tmp_path: Path, review_run: tuple[Path, Path]) -> None:
        """CSID falls back to the payload's session_id when the env var is unset."""
        result = _run(tmp_path, _ask_payload(session_id=CSID), session_id_env=None)
        assert _denial_reason(result) is not None

    @pytest.mark.parametrize(
        ("report_dir", "expected"),
        [
            pytest.param(r"C:\Repo\.REPORTS\REVIEW\run-1", True, id="contained-windows-path"),
            pytest.param(r"C:\Repo\.reports\review\..\private", False, id="traversal-escapes"),
        ],
    )
    def test_simulated_windows_report_dir_validation(self, report_dir: str, expected: bool) -> None:
        """Recognise Windows separators and casing without trusting escaped sentinel paths."""
        assert _call_export("isReviewReportDir", report_dir) is expected


@_skip_node_unavailable
class TestPassThrough:
    """Anything outside an in-flight review, and every can't-tell case, is allowed untouched (empty stdout)."""

    def test_no_sentinel(self, tmp_path: Path) -> None:
        """No develop:review run reached Step 2 → unrelated questions are never gated."""
        assert _run(tmp_path, _ask_payload()) == {}

    def test_oss_sentinel_is_ignored(self, tmp_path: Path) -> None:
        """An in-flight /oss:review is gated by its own plugin's hook, not this one."""
        report_dir = tmp_path / "repo" / ".reports" / "review" / "2026-08-04T10-00-00Z"
        report_dir.mkdir(parents=True)
        (tmp_path / f"oss-review-report-dir-{CSID}").write_text(f"{report_dir}\n", encoding="utf-8")
        assert _run(tmp_path, _ask_payload()) == {}

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param(_ask_payload(tool_name="Bash"), id="other-tool"),
            pytest.param(_ask_payload(hook_event_name="PostToolUse"), id="other-event"),
        ],
    )
    def test_non_matching_payloads(self, tmp_path: Path, review_run: tuple[Path, Path], payload: dict) -> None:
        """Only PreToolUse AskUserQuestion is inspected; anything else is untouched."""
        assert _run(tmp_path, payload) == {}

    def test_stale_sentinel(self, tmp_path: Path, review_run: tuple[Path, Path]) -> None:
        """Sentinel older than the enforcement window is treated as a crashed run."""
        _, sentinel = review_run
        stale = time.time() - (TWO_HOURS_S + 600)
        os.utime(sentinel, (stale, stale))
        assert _run(tmp_path, _ask_payload()) == {}

    def test_missing_report_dir(self, tmp_path: Path, review_run: tuple[Path, Path]) -> None:
        """Report dir gone (worktree removed, TTL cleanup) → hook cannot judge, allows."""
        report_dir, _ = review_run
        report_dir.rmdir()
        assert _run(tmp_path, _ask_payload()) == {}

    @pytest.mark.parametrize(
        "content",
        [
            pytest.param("", id="empty"),
            pytest.param("   \n", id="blank"),
            pytest.param("repo/.reports/review/2026-08-04T10-00-00Z\n", id="relative-path"),
            pytest.param("/etc\n", id="unrelated-absolute-path"),
        ],
    )
    def test_implausible_sentinel_content(self, tmp_path: Path, content: str) -> None:
        """Sentinel not holding an absolute .reports/review/ path is ignored."""
        (tmp_path / SENTINEL_NAME).write_text(content, encoding="utf-8")
        assert _run(tmp_path, _ask_payload()) == {}

    @pytest.mark.parametrize(
        "session_id",
        [
            pytest.param("../../etc/passwd", id="traversal"),
            pytest.param("has space", id="space"),
            pytest.param("", id="empty"),
        ],
    )
    def test_unsafe_csid(self, tmp_path: Path, review_run: tuple[Path, Path], session_id: str) -> None:
        """A CSID that cannot name a sentinel file is discarded, never path-joined."""
        assert _run(tmp_path, _ask_payload(), session_id_env=session_id) == {}

    def test_malformed_stdin(self, tmp_path: Path) -> None:
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
    """Stop re-checks delivery of the full report header when the follow-up question was skipped."""

    def test_blocks_undelivered_report_once(self, tmp_path: Path, written_run: Path) -> None:
        """A turn ending without the report delivery is kept going once; the same report never re-blocks.

        Blocking forever would trap the session; one continuation is enough to make the model print the header.
        """
        first = _run(tmp_path, _stop_payload())
        second = _run(tmp_path, _stop_payload())
        assert first.get("decision") == "block"
        assert "develop:review" in first["reason"]
        assert second == {}

    def test_passes_delivery_in_final_message(self, tmp_path: Path, written_run: Path) -> None:
        """The Stop payload's final message proves delivery even without a transcript."""
        assert _run(tmp_path, _stop_payload(last_assistant_message=DELIVERED)) == {}

    @pytest.mark.parametrize(
        ("reviewers_row", "delivered"),
        [
            pytest.param("", False, id="reviewer-row-missing"),
            pytest.param(
                "| Reviewers | Software engineer (3), QA specialist (2). |\n", True, id="reviewer-row-present"
            ),
        ],
    )
    def test_reviewer_header_row_must_be_delivered(
        self, tmp_path: Path, stage_report: Callable[[str], Path], reviewers_row: str, delivered: bool
    ) -> None:
        """Stop requires the reviewer row even though the follow-up uses a shorter summary.

        The shorter question summary passes, but the final reply must still carry the full table including the
        ``Reviewers`` row, so a table without it is blocked.
        """
        report = stage_report(
            "---\nTitle: review\nDate: 2026-09-22\nReviewers: Software engineer (3), QA specialist (2).\n---\n"
        )
        table = "| Field | Value |\n| --- | --- |\n| Title | review |\n| Date | 2026-09-22 |\n" + reviewers_row
        follow_up = _run(tmp_path, _summary_payload(report, "Title: review"))
        stop = _run(tmp_path, _stop_payload(last_assistant_message=table))
        assert (follow_up, stop.get("decision") != "block") == ({}, delivered)


@_skip_node_unavailable
class TestQuestionSummaryDelivery:
    """Follow-ups show summaries in question text and leave full delivery to Stop."""

    def test_plain_summary_allows_follow_up(self, tmp_path: Path, written_run: Path) -> None:
        """The matching headline and resolved report path pass without any preview."""
        payload = _summary_payload(written_run, "Title: x")
        no_previews = all("preview" not in option for option in payload["tool_input"]["questions"][0]["options"])
        assert (no_previews, _run(tmp_path, payload)) == (True, {})

    def test_missing_summary_denial_names_question_fix(self, tmp_path: Path, written_run: Path) -> None:
        """Missing summary evidence names the first-question remedy."""
        reason = _denial_reason(_run(tmp_path, _summary_payload(written_run, None)))
        assert reason is not None
        assert "first question's `question` text" in reason
        assert "fixed lines" in reason

    def test_shown_summary_keeps_stop_armed_once(self, tmp_path: Path, written_run: Path) -> None:
        """A shown summary leaves one Stop continuation for the full report.

        The follow-up and its PostToolUse pass, the first Stop blocks, delivering the table clears it, and a later Stop
        stays quiet.
        """
        payload = _summary_payload(written_run, "Title: x")
        results = [
            _run(tmp_path, payload),
            _run(tmp_path, {**payload, "hook_event_name": "PostToolUse"}),
            _run(tmp_path, _stop_payload()).get("decision"),
            _run(tmp_path, _stop_payload(last_assistant_message=DELIVERED)),
            _run(tmp_path, _stop_payload()),
        ]
        assert results == [{}, {}, "block", {}, {}]

    def test_allowed_but_unshown_follow_up_leaves_stop_armed(self, tmp_path: Path, written_run: Path) -> None:
        """An allowed but unshown question also leaves final delivery armed."""
        follow_up = _run(tmp_path, _summary_payload(written_run, "Title: x"))
        assert (follow_up, _run(tmp_path, _stop_payload()).get("decision")) == ({}, "block")

    def test_summary_only_in_previews_is_denied(self, tmp_path: Path, written_run: Path) -> None:
        """Even every option's preview cannot replace the question's plain summary."""
        payload = _summary_payload(written_run, "Title: x")
        first = payload["tool_input"]["questions"][0]
        first["options"] = [{**option, "preview": first["question"]} for option in first["options"]]
        first["question"] = "What next?"
        assert _denial_reason(_run(tmp_path, payload)) is not None
