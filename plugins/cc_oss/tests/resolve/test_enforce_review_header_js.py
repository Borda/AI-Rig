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
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[2] / "hooks" / "enforce-review-header.js"

CSID = "test-session-1234"
SENTINEL_NAME = f"oss-review-report-dir-{CSID}"
TWO_HOURS_S = 2 * 60 * 60

NODE_UNAVAILABLE = shutil.which("node") is None


def _ask_payload(**overrides: object) -> dict:
    """Build a PreToolUse AskUserQuestion payload, applying `overrides`."""
    payload: dict = {
        "hook_event_name": "PreToolUse",
        "tool_name": "AskUserQuestion",
        "tool_input": {"questions": [{"question": "What next?", "header": "oss-review"}]},
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


@pytest.fixture(name="review_run")
def _review_run(tmp_path: Path) -> tuple[Path, Path]:
    """Stage a review that reached Step 2: report directory on disk plus its sentinel."""
    report_dir = tmp_path / "repo" / ".reports" / "review" / "2026-08-04T10-00-00Z"
    report_dir.mkdir(parents=True)
    sentinel = tmp_path / SENTINEL_NAME
    sentinel.write_text(f"{report_dir}\n", encoding="utf-8")
    return report_dir, sentinel


# ── Gate fires only for an in-flight review missing its report ────────────────


_skip_node_unavailable = pytest.mark.skipif(
    NODE_UNAVAILABLE,
    reason="requires node to execute the hook",
)


@_skip_node_unavailable
def test_missing_report_is_denied(tmp_path: Path, review_run: tuple[Path, Path]) -> None:
    """Sentinel present without review-report.md → deny, naming the step to redo."""
    report_dir, _ = review_run

    reason = _denial_reason(_run(tmp_path, _ask_payload()))

    assert reason is not None, "AskUserQuestion must be denied while the report is missing"
    assert str(report_dir / "review-report.md") in reason
    assert "Step 5b" in reason


@_skip_node_unavailable
def test_empty_report_is_denied(tmp_path: Path, review_run: tuple[Path, Path]) -> None:
    """A zero-byte review-report.md counts as not written → deny."""
    report_dir, _ = review_run
    (report_dir / "review-report.md").touch()

    assert _denial_reason(_run(tmp_path, _ask_payload())) is not None


@_skip_node_unavailable
def test_written_report_without_delivery_is_denied(tmp_path: Path, review_run: tuple[Path, Path]) -> None:
    """Unverified report delivery must block only the follow-up transition."""
    report_dir, _ = review_run
    (report_dir / "review-report.md").write_text("---\nTitle: oss-review\n---\n", encoding="utf-8")

    assert _denial_reason(_run(tmp_path, _ask_payload())) is not None


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
def test_table_only_in_reply_text_is_denied_and_summary_passes(tmp_path: Path, review_run: tuple[Path, Path]) -> None:
    """A table printed as reply text before the question is denied; a plain question summary passes.

    Reply text written before a tool call can come back as an empty progress update the user never sees, so only the
    call's own fields count at question time; the denial says so and the call corrected to it passes.
    """
    report_dir, _ = review_run
    (report_dir / "review-report.md").write_text("---\nTitle: x\nPR: #1\nDate: y\n---\n", encoding="utf-8")
    transcript = _write_transcript(tmp_path, DELIVERED)

    reason = _denial_reason(_run(tmp_path, _ask_payload(transcript_path=str(transcript))))
    retried = _run(tmp_path, _summary_payload(report_dir / "review-report.md", "Title: x"))

    assert reason is not None
    assert "reply text before the call does not count" in reason
    assert retried == {}


@_skip_node_unavailable
@pytest.mark.parametrize("complete", [False, True])
def test_reviewer_header_row_must_be_delivered_in_final_reply(
    tmp_path: Path, review_run: tuple[Path, Path], complete: bool
) -> None:
    """Stop requires the reviewer row even though the follow-up uses a shorter summary."""
    report_dir, _ = review_run
    report = report_dir / "review-report.md"
    report.write_text(
        "---\nTitle: review\nDate: 2026-09-22\nReviewers: Software engineer (3), QA specialist (2).\n---\n",
        encoding="utf-8",
    )
    assert _run(tmp_path, _summary_payload(report, "Title: review")) == {}
    table = "| Field | Value |\n| --- | --- |\n| Title | review |\n| Date | 2026-09-22 |\n"
    if complete:
        table += "| Reviewers | Software engineer (3), QA specialist (2). |\n"
    result = _run(tmp_path, _stop_payload(last_assistant_message=table))
    assert (result.get("decision") != "block") is complete


def _write_shipped_report(report_dir: Path) -> tuple[Path, str]:
    """Write the shipped template's `---` header as the run's report; return its path and full header table."""
    template = HOOK.parent.parent / "skills" / "review" / "templates" / "review-report.md"
    header = template.read_text(encoding="utf-8").split("---", 2)[1].strip()
    report = report_dir / "review-report.md"
    report.write_text(f"---\n{header}\n---\n", encoding="utf-8")
    rows = [line.partition(":") for line in header.splitlines()]
    table = "| Field | Value |\n| --- | --- |\n" + "".join(
        "| {} | {} |\n".format(key.strip(), value.strip().replace("|", r"\|")) for key, _, value in rows
    )
    return report, table


@_skip_node_unavailable
def test_shipped_review_header_is_accepted_as_question_summary(tmp_path: Path, review_run: tuple[Path, Path]) -> None:
    """The actual shipped header supplies a fixed summary without requiring its large table in previews."""
    report, _ = _write_shipped_report(review_run[0])
    header = report.read_text(encoding="utf-8").split("---", 2)[1]
    fields = dict(line.partition(":")[::2] for line in header.splitlines() if ":" in line)
    summary = f"Title: {fields['Title'].strip()}\nOutcome: {fields['Outcome'].strip()}"
    assert _run(tmp_path, _summary_payload(report, summary)) == {}


@_skip_node_unavailable
def test_shipped_review_header_optional_preview_is_over_the_cap(tmp_path: Path, review_run: tuple[Path, Path]) -> None:
    """An optional full-table preview remains capped even beside a valid question summary."""
    report, table = _write_shipped_report(review_run[0])
    header = report.read_text(encoding="utf-8").split("---", 2)[1]
    fields = dict(line.partition(":")[::2] for line in header.splitlines() if ":" in line)
    summary = f"Title: {fields['Title'].strip()}\nOutcome: {fields['Outcome'].strip()}"
    payload = _summary_payload(report, summary)
    payload["tool_input"]["questions"][0]["options"][0]["preview"] = table
    reason = _denial_reason(_run(tmp_path, payload))
    assert reason is not None
    assert "exceed the preview cap (question 1, option 1:" in reason
    assert "incomplete" not in reason
    del payload["tool_input"]["questions"][0]["options"][0]["preview"]
    assert _run(tmp_path, payload) == {}


@_skip_node_unavailable
def test_report_written_without_table_is_denied(tmp_path: Path, review_run: tuple[Path, Path]) -> None:
    """Unverified report delivery must block only the follow-up transition."""
    report_dir, _ = review_run
    (report_dir / "review-report.md").write_text("---\nTitle: oss-review\n---\n", encoding="utf-8")
    transcript = _write_transcript(tmp_path, "Title: oss-review\nPR: #1303\nDate: 2026-08-08\n")

    result = _run(tmp_path, _ask_payload(transcript_path=str(transcript)))

    hook_output = result.get("hookSpecificOutput", {})
    assert hook_output.get("permissionDecision") == "deny"
    assert "report" in hook_output.get("permissionDecisionReason", "")


@_skip_node_unavailable
def test_report_written_unreadable_transcript_is_denied(tmp_path: Path, review_run: tuple[Path, Path]) -> None:
    """Unverified report delivery must block only the follow-up transition."""
    report_dir, _ = review_run
    (report_dir / "review-report.md").write_text("---\nTitle: oss-review\n---\n", encoding="utf-8")

    result = _run(tmp_path, _ask_payload(transcript_path=str(tmp_path / "missing.jsonl")))

    assert _denial_reason(result) is not None


@_skip_node_unavailable
def test_trailing_slash_tmpdir_resolves_sentinel(tmp_path: Path, review_run: tuple[Path, Path]) -> None:
    """MacOS exports TMPDIR with a trailing slash — the sentinel must still resolve."""
    assert _denial_reason(_run(tmp_path, _ask_payload(), tmpdir_suffix="/")) is not None


@_skip_node_unavailable
def test_sentinel_resolved_from_payload_session_id(tmp_path: Path, review_run: tuple[Path, Path]) -> None:
    """CSID falls back to the payload's session_id when the env var is unset."""
    result = _run(tmp_path, _ask_payload(session_id=CSID), session_id_env=None)

    assert _denial_reason(result) is not None


@_skip_node_unavailable
def test_simulated_windows_report_dir_validation_accepts_contained_paths_and_rejects_traversal() -> None:
    """Recognise Windows separators and casing without trusting escaped sentinel paths."""
    assert _call_export("isReviewReportDir", r"C:\Repo\.REPORTS\REVIEW\run-1") is True
    assert _call_export("isReviewReportDir", r"C:\Repo\.reports\review\..\private") is False


# ── Everything outside an in-flight review passes through ────────────────────


@_skip_node_unavailable
def test_no_sentinel_passes_through(tmp_path: Path) -> None:
    """No oss:review run reached Step 2 → unrelated questions are never gated."""
    assert _run(tmp_path, _ask_payload()) == {}


@_skip_node_unavailable
@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(_ask_payload(tool_name="Bash"), id="other-tool"),
        pytest.param(_ask_payload(hook_event_name="PostToolUse"), id="other-event"),
    ],
)
def test_non_matching_payloads_pass_through(tmp_path: Path, review_run: tuple[Path, Path], payload: dict) -> None:
    """Only PreToolUse AskUserQuestion is inspected; anything else is untouched."""
    assert _run(tmp_path, payload) == {}


@_skip_node_unavailable
def test_stale_sentinel_passes_through(tmp_path: Path, review_run: tuple[Path, Path]) -> None:
    """Sentinel older than the enforcement window is treated as a crashed run."""
    _, sentinel = review_run
    stale = time.time() - (TWO_HOURS_S + 600)
    os.utime(sentinel, (stale, stale))

    assert _run(tmp_path, _ask_payload()) == {}


@_skip_node_unavailable
def test_missing_report_dir_passes_through(tmp_path: Path, review_run: tuple[Path, Path]) -> None:
    """Report dir gone (worktree removed, TTL cleanup) → hook cannot judge, allows."""
    report_dir, _ = review_run
    report_dir.rmdir()

    assert _run(tmp_path, _ask_payload()) == {}


@_skip_node_unavailable
@pytest.mark.parametrize("content", ["", "   \n", "repo/.reports/review/2026-08-04T10-00-00Z\n", "/etc\n"])
def test_implausible_sentinel_content_passes_through(tmp_path: Path, content: str) -> None:
    """Sentinel not holding an absolute .reports/review/ path is ignored."""
    (tmp_path / SENTINEL_NAME).write_text(content, encoding="utf-8")

    assert _run(tmp_path, _ask_payload()) == {}


@_skip_node_unavailable
@pytest.mark.parametrize("session_id", ["../../etc/passwd", "has space", ""])
def test_unsafe_csid_passes_through(tmp_path: Path, review_run: tuple[Path, Path], session_id: str) -> None:
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


# ── Stop: delivery still checked when the follow-up question is skipped ──────


def _stop_payload(**overrides: object) -> dict:
    """Build a Stop payload whose final message lacks the header, applying `overrides`."""
    payload: dict = {"hook_event_name": "Stop", "stop_hook_active": False, "last_assistant_message": "done"}
    payload.update(overrides)
    return payload


@_skip_node_unavailable
def test_stop_blocks_undelivered_report_once(tmp_path: Path, review_run: tuple[Path, Path]) -> None:
    """A turn ending without the report delivery is kept going once; the same report never re-blocks."""
    report_dir, _ = review_run
    (report_dir / "review-report.md").write_text("---\nTitle: x\nPR: #1\nDate: y\n---\n", encoding="utf-8")
    result = _run(tmp_path, _stop_payload())

    assert result.get("decision") == "block"
    assert "oss:review" in result["reason"]
    assert _run(tmp_path, _stop_payload()) == {}


@_skip_node_unavailable
def test_stop_passes_delivery_in_final_message(tmp_path: Path, review_run: tuple[Path, Path]) -> None:
    """The Stop payload's final message proves delivery even without a transcript."""
    report_dir, _ = review_run
    (report_dir / "review-report.md").write_text("---\nTitle: x\nPR: #1\nDate: y\n---\n", encoding="utf-8")
    assert _run(tmp_path, _stop_payload(last_assistant_message=DELIVERED)) == {}


DELIVERED = "| Field | Value |\n| --- | --- |\n| Title | x |\n| PR | #1 |\n| Date | y |\n"


# ── Delivery via the follow-up question text ────────────────────


def _summary_payload(report: Path, summary: str | None) -> dict:
    """Build the follow-up with a plain question summary and no previews."""
    options = [{"label": label, "description": "next step"} for label in ("/oss:resolve 1", "skip")]
    text = f"{summary}\n→ {report}\nWhat next?" if summary else "What next?"
    questions = [{"question": text, "header": "oss-review", "options": options}]
    return _ask_payload(tool_input={"questions": questions}, transcript_path=None)


@pytest.fixture(name="written_run")
def _written_run(review_run: tuple[Path, Path]) -> Path:
    """Stage a review whose consolidator wrote the report DELIVERED renders."""
    report_dir, _ = review_run
    (report_dir / "review-report.md").write_text("---\nTitle: x\nPR: #1\nDate: y\n---\n", encoding="utf-8")
    return report_dir / "review-report.md"


class TestQuestionSummaryDelivery:
    """Follow-ups show summaries in question text and leave full delivery to Stop."""

    @_skip_node_unavailable
    def test_plain_summary_allows_follow_up(self, tmp_path: Path, written_run: Path) -> None:
        """The matching headline and resolved report path pass without any preview."""
        payload = _summary_payload(written_run, "Title: x")
        assert all("preview" not in option for option in payload["tool_input"]["questions"][0]["options"])
        assert _run(tmp_path, payload) == {}

    @_skip_node_unavailable
    def test_missing_summary_denial_names_question_fix(self, tmp_path: Path, written_run: Path) -> None:
        """Missing summary evidence names the first-question remedy."""
        reason = _denial_reason(_run(tmp_path, _summary_payload(written_run, None)))
        assert reason is not None
        assert "first question's `question` text" in reason
        assert "fixed lines" in reason

    @_skip_node_unavailable
    def test_shown_summary_keeps_stop_armed_once(self, tmp_path: Path, written_run: Path) -> None:
        """A shown summary leaves one Stop continuation for the full report."""
        payload = _summary_payload(written_run, "Title: x")
        assert _run(tmp_path, payload) == {}
        assert _run(tmp_path, {**payload, "hook_event_name": "PostToolUse"}) == {}
        assert _run(tmp_path, _stop_payload()).get("decision") == "block"
        assert _run(tmp_path, _stop_payload(last_assistant_message=DELIVERED)) == {}
        assert _run(tmp_path, _stop_payload()) == {}

    @_skip_node_unavailable
    def test_allowed_but_unshown_follow_up_leaves_stop_armed(self, tmp_path: Path, written_run: Path) -> None:
        """An allowed but unshown question also leaves final delivery armed."""
        assert _run(tmp_path, _summary_payload(written_run, "Title: x")) == {}
        assert _run(tmp_path, _stop_payload()).get("decision") == "block"

    @_skip_node_unavailable
    def test_summary_only_in_previews_is_denied(self, tmp_path: Path, written_run: Path) -> None:
        """Even every option's preview cannot replace the question's plain summary."""
        payload = _summary_payload(written_run, "Title: x")
        first = payload["tool_input"]["questions"][0]
        for option in first["options"]:
            option["preview"] = first["question"]
        first["question"] = "What next?"
        assert _denial_reason(_run(tmp_path, payload)) is not None
