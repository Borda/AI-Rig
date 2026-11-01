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

Every test runs the real hook under ``node`` (end to end), so the classes are skipped together when ``node`` is absent.
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

HOOK = Path(__file__).resolve().parents[2] / "hooks" / "enforce-analyse-header.js"
_ask_payload = functools.partial(ask_payload, "oss-analyse")
_call_export = functools.partial(call_export, HOOK)

CSID = "test-session-1234"
SENTINEL_NAME = f"analyse-report-file-{CSID}"
# One report path per sub-mode, as the mode files build them.
MODE_REPORTS = {
    "thread": ".reports/analyse/thread/output-analyse-thread-42-2026-08-04.md",
    "vitality": ".reports/analyse/vitality/output-analyse-vitality-owner-repo-2026-08-04T10-00-00Z.md",
    "ecosystem": ".reports/analyse/ecosystem/output-analyse-ecosystem-2026-08-04.md",
}

#: A report front matter whose delivery proof is the table below.
WRITTEN_REPORT = "---\nTitle: x\nPR: #1\nDate: y\n---\n"
_skip_node_unavailable = pytest.mark.skipif(shutil.which("node") is None, reason="requires node to execute the hook")


def _summary_payload(cwd: Path, summary: str | None) -> dict:
    """Build a follow-up with fixed plain summary lines and no previews."""
    report = cwd / MODE_REPORTS["thread"]
    options = [{"label": label, "description": "next step"} for label in ("/develop:fix", "skip")]
    text = f"{summary}\n→ {report}\nWhat next?" if summary else "What next?"
    questions = [{"question": text, "header": "oss-analyse", "options": options}]
    return _ask_payload(cwd=str(cwd), tool_input={"questions": questions}, transcript_path=None)


def _invoke(
    tmp_path: Path, stdin: str, *, session_id_env: str | None = CSID, tmpdir_suffix: str = ""
) -> subprocess.CompletedProcess[str]:
    """Run the hook with raw `stdin` under an isolated TMPDIR and return the finished process."""
    env = {**os.environ, "TMPDIR": f"{tmp_path}{tmpdir_suffix}"}
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    if session_id_env is not None:
        env["CLAUDE_CODE_SESSION_ID"] = session_id_env
    return subprocess.run(["node", str(HOOK)], input=stdin, capture_output=True, text=True, timeout=10, env=env)


def _run(tmp_path: Path, payload: dict, *, session_id_env: str | None = CSID, tmpdir_suffix: str = "") -> dict:
    """Invoke the hook with `payload` on stdin and return parsed stdout (or {})."""
    proc = _invoke(tmp_path, json.dumps(payload), session_id_env=session_id_env, tmpdir_suffix=tmpdir_suffix)
    assert proc.returncode == 0, f"hook exited {proc.returncode}: {proc.stderr}"
    out = proc.stdout.strip()
    return json.loads(out) if out else {}


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


@pytest.fixture(name="report")
def _report(repo: Path) -> Path:
    """Return the thread-mode report path with its directory created and no file written yet."""
    path = repo / MODE_REPORTS["thread"]
    path.parent.mkdir(parents=True)
    return path


@pytest.fixture(name="written_run")
def _written_run(report: Path, analyse_run: Path) -> None:
    """Stage a thread-mode run whose report DELIVERED renders is written."""
    report.write_text(WRITTEN_REPORT, encoding="utf-8")


@_skip_node_unavailable
@pytest.mark.usefixtures("analyse_run")
class TestMissingOrUndeliveredReport:
    """The gate fires for an in-flight analyse whose report is missing, empty, or not delivered."""

    @pytest.mark.parametrize("mode", list(MODE_REPORTS))
    def test_missing_report_denied_for_every_mode(self, tmp_path: Path, repo: Path, mode: str) -> None:
        """Each sub-mode's report path, unwritten → deny, naming the file and the tool.

        All three modes rewrite the same sentinel with their own path, so the gate must resolve each of them.
        """
        (tmp_path / SENTINEL_NAME).write_text(f"{MODE_REPORTS[mode]}\n", encoding="utf-8")

        reason = denial_reason(_run(tmp_path, _ask_payload(cwd=str(repo))))

        assert reason is not None, "AskUserQuestion must be denied while the report is missing"
        assert str(repo / MODE_REPORTS[mode]) in reason
        assert "AskUserQuestion only after" in reason

    @pytest.mark.parametrize(
        "content",
        [
            pytest.param("", id="zero-byte-report"),
            pytest.param("---\nTitle: oss:analyse — thread\n---\n", id="written-report-without-delivery"),
        ],
    )
    def test_incomplete_report_denied(self, tmp_path: Path, repo: Path, report: Path, content: str) -> None:
        """A zero-byte report counts as not written, and a written but undelivered one blocks the follow-up.

        Unverified report delivery must block only the follow-up transition, never the run itself.
        """
        report.write_text(content, encoding="utf-8")

        assert denial_reason(_run(tmp_path, _ask_payload(cwd=str(repo)))) is not None

    def test_table_only_in_reply_text_denied_and_summary_passes(self, tmp_path: Path, repo: Path, report: Path) -> None:
        """A table printed as reply text before the question is denied; a plain question summary passes.

        Reply text written before a tool call can come back as an empty progress update the user never sees, so only the
        call's own fields count at question time; the denial says so and the call corrected to it passes.
        """
        report.write_text("---\nTitle: x\nDate: y\nScope: z\n---\n", encoding="utf-8")
        table = "| Field | Value |\n| --- | --- |\n| Title | x |\n| Date | y |\n| Scope | z |\n"
        transcript = write_transcript(tmp_path, table)

        reason = denial_reason(_run(tmp_path, _ask_payload(cwd=str(repo), transcript_path=str(transcript))))
        retried = _run(tmp_path, _summary_payload(repo, "Title: x"))

        assert reason is not None
        assert "reply text before the call does not count" in reason
        assert retried == {}

    def test_report_without_table_in_transcript_denied(self, tmp_path: Path, repo: Path, report: Path) -> None:
        """A written report whose transcript shows no header table still blocks the follow-up question.

        The assistant printed plain ``Key: value`` lines instead of the table, which is not proof of delivery.
        """
        report.write_text("---\nTitle: oss:analyse — thread\n---\n", encoding="utf-8")
        transcript = write_transcript(tmp_path, "Title: oss:analyse — thread\nDate: 2026-08-08\n")

        result = _run(tmp_path, _ask_payload(cwd=str(repo), transcript_path=str(transcript)))

        hook_output = result.get("hookSpecificOutput", {})
        assert hook_output.get("permissionDecision") == "deny"
        assert "report" in hook_output.get("permissionDecisionReason", "")

    def test_report_with_unreadable_transcript_denied(self, tmp_path: Path, repo: Path, report: Path) -> None:
        """A written report whose transcript cannot be read still blocks the follow-up question.

        Delivery cannot be verified without the transcript, so the gate treats it as undelivered rather than passing.
        """
        report.write_text("---\nTitle: oss:analyse — thread\n---\n", encoding="utf-8")

        result = _run(tmp_path, _ask_payload(cwd=str(repo), transcript_path=str(tmp_path / "missing.jsonl")))

        assert denial_reason(result) is not None

    def test_trailing_slash_tmpdir_resolves_sentinel(self, tmp_path: Path, repo: Path) -> None:
        """MacOS exports TMPDIR with a trailing slash — the sentinel must still resolve.

        Joining ``TMPDIR`` naively would produce a double slash and miss the sentinel, silently disabling the gate.
        """
        assert denial_reason(_run(tmp_path, _ask_payload(cwd=str(repo)), tmpdir_suffix="/")) is not None

    def test_session_id_taken_from_payload(self, tmp_path: Path, repo: Path) -> None:
        """CSID falls back to the payload's session_id when the env var is unset.

        The hook may run without ``CLAUDE_CODE_SESSION_ID``; the payload still carries the session to look up.
        """
        result = _run(tmp_path, _ask_payload(cwd=str(repo), session_id=CSID), session_id_env=None)

        assert denial_reason(result) is not None


@_skip_node_unavailable
class TestSentinelPathResolution:
    """How the report path named by the sentinel is turned into a file to check."""

    def test_absolute_sentinel_path_ignores_cwd(self, tmp_path: Path, repo: Path) -> None:
        """An absolute report path is honoured as-is, independent of the payload cwd.

        The payload cwd points at a directory that does not exist; the absolute path alone must locate the report.
        """
        absolute = repo / MODE_REPORTS["vitality"]
        (tmp_path / SENTINEL_NAME).write_text(f"{absolute}\n", encoding="utf-8")

        assert denial_reason(_run(tmp_path, _ask_payload(cwd="/nonexistent"))) is not None

    def test_missing_cwd_falls_back_to_process_cwd(self, tmp_path: Path) -> None:
        """No cwd in the payload → relative path resolves against the hook's own cwd.

        The report name embeds ``tmp_path``'s name, so it is unique and cannot collide with a real report in the
        directory the test process happens to run from.
        """
        relative = f".reports/analyse/thread/output-analyse-thread-{tmp_path.name}.md"
        (tmp_path / SENTINEL_NAME).write_text(f"{relative}\n", encoding="utf-8")

        assert denial_reason(_run(tmp_path, _ask_payload())) is not None

    def test_windows_report_path_resolves_case_insensitively(self) -> None:
        """A Windows-style relative report path resolves against a Windows root, ignoring letter case.

        Runs the hook's resolver with simulated Windows inputs, so the behaviour is proven on every host.
        """
        resolved = _call_export("resolveReportFile", r".REPORTS\ANALYSE\THREAD\output-analyse-thread-1.md", r"C:\Repo")

        assert isinstance(resolved, str)
        assert resolved.casefold() == r"c:\repo\.reports\analyse\thread\output-analyse-thread-1.md".casefold()

    def test_windows_traversal_outside_analyse_rejected(self) -> None:
        """A Windows-style path that climbs out of ``.reports/analyse`` resolves to nothing.

        ``..\\..\\private.md`` would land outside the reports tree, so the resolver must refuse it.
        """
        assert _call_export("resolveReportFile", r".reports\analyse\thread\..\..\private.md", r"C:\Repo") is None


@_skip_node_unavailable
class TestPassThrough:
    """Everything outside an in-flight analyse passes through untouched."""

    def test_no_sentinel(self, tmp_path: Path, repo: Path) -> None:
        """No oss:analyse run started → unrelated questions are never gated.

        Without the sentinel the hook has no report to check and must stay silent.
        """
        assert _run(tmp_path, _ask_payload(cwd=str(repo))) == {}

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param(_ask_payload(tool_name="Bash"), id="other-tool"),
            pytest.param(_ask_payload(hook_event_name="PostToolUse"), id="other-event"),
        ],
    )
    @pytest.mark.usefixtures("analyse_run")
    def test_non_matching_payloads(self, tmp_path: Path, repo: Path, payload: dict) -> None:
        """Only PreToolUse AskUserQuestion is inspected; anything else is untouched.

        The sentinel and its missing report are in place, so only the payload shape explains the silent exit.
        """
        assert _run(tmp_path, {**payload, "cwd": str(repo)}) == {}

    def test_stale_sentinel(self, tmp_path: Path, repo: Path, analyse_run: Path) -> None:
        """Sentinel older than the enforcement window is treated as a crashed run.

        The sentinel's mtime is moved ten minutes past the two-hour window; no wall-clock wait is involved.
        """
        stale = time.time() - (TWO_HOURS_S + 600)
        os.utime(analyse_run, (stale, stale))

        assert _run(tmp_path, _ask_payload(cwd=str(repo))) == {}

    @pytest.mark.parametrize(
        "content",
        ["", "   \n", "docs/handover.md\n", ".reports/review/2026-08-04T10-00-00Z/review-report.md\n"],
    )
    def test_implausible_sentinel_content(self, tmp_path: Path, repo: Path, content: str) -> None:
        """Sentinel not naming a .reports/analyse/ report is ignored.

        An empty sentinel is the Step 1 state before any mode ran; the other values name unrelated files.
        """
        (tmp_path / SENTINEL_NAME).write_text(content, encoding="utf-8")

        assert _run(tmp_path, _ask_payload(cwd=str(repo))) == {}

    @pytest.mark.parametrize("session_id", ["../../etc/passwd", "has space", ""])
    @pytest.mark.usefixtures("analyse_run")
    def test_unsafe_csid(self, tmp_path: Path, repo: Path, session_id: str) -> None:
        """A CSID that cannot name a sentinel file is discarded, never path-joined.

        A traversal-shaped or blank session id must not be used to build a sentinel path, so the gate stays off.
        """
        assert _run(tmp_path, _ask_payload(cwd=str(repo)), session_id_env=session_id) == {}

    def test_malformed_stdin(self, tmp_path: Path) -> None:
        """A hook bug or unparsable payload must never strand the session.

        Non-JSON stdin must exit 0 with empty stdout so the tool call is allowed.
        """
        proc = _invoke(tmp_path, "not json", session_id_env=None)

        assert (proc.returncode, proc.stdout.strip()) == (0, "")


@_skip_node_unavailable
@pytest.mark.usefixtures("written_run")
class TestStop:
    """Delivery is still checked at Stop when the follow-up question is skipped."""

    def test_blocks_undelivered_report_once(self, tmp_path: Path, repo: Path) -> None:
        """A turn ending without the report delivery is kept going once; the same report never re-blocks.

        The first Stop is blocked with an ``oss:analyse`` reason; an identical second Stop is let through so the session
        cannot loop forever.
        """
        first = _run(tmp_path, stop_payload(cwd=str(repo)))
        second = _run(tmp_path, stop_payload(cwd=str(repo)))

        assert first.get("decision") == "block"
        assert "oss:analyse" in first["reason"]
        assert second == {}

    def test_passes_delivery_in_final_message(self, tmp_path: Path, repo: Path) -> None:
        """The Stop payload's final message proves delivery even without a transcript.

        The final message holds the header table, so there is nothing left to enforce.
        """
        assert _run(tmp_path, stop_payload(cwd=str(repo), last_assistant_message=DELIVERED)) == {}


@_skip_node_unavailable
@pytest.mark.usefixtures("written_run")
class TestQuestionSummaryDelivery:
    """Follow-ups show summaries in question text and leave full delivery to Stop."""

    def test_plain_summary_allows_follow_up(self, tmp_path: Path, repo: Path) -> None:
        """The matching headline and resolved report path pass without any preview.

        The payload has no option previews at all, so the question text alone has to satisfy the gate.
        """
        payload = _summary_payload(repo, "Title: x")
        assert all("preview" not in option for option in payload["tool_input"]["questions"][0]["options"])
        assert _run(tmp_path, payload) == {}

    def test_missing_summary_denial_names_question_fix(self, tmp_path: Path, repo: Path) -> None:
        """Missing summary evidence names the first-question remedy.

        The question carries no summary lines, so the denial must tell the assistant exactly where to add them.
        """
        reason = denial_reason(_run(tmp_path, _summary_payload(repo, None)))
        assert reason is not None
        assert "first question's `question` text" in reason
        assert "fixed lines" in reason

    def test_shown_summary_keeps_stop_armed_once(self, tmp_path: Path, repo: Path) -> None:
        """A shown summary leaves one Stop continuation for the full report.

        Walks the whole sequence: question allowed, post-tool event, one blocked Stop, delivery accepted, then silence.
        """
        payload = _summary_payload(repo, "Title: x")
        assert _run(tmp_path, payload) == {}
        assert _run(tmp_path, {**payload, "hook_event_name": "PostToolUse"}) == {}
        assert _run(tmp_path, stop_payload(cwd=str(repo))).get("decision") == "block"
        assert _run(tmp_path, stop_payload(cwd=str(repo), last_assistant_message=DELIVERED)) == {}
        assert _run(tmp_path, stop_payload(cwd=str(repo))) == {}

    def test_allowed_but_unshown_follow_up_leaves_stop_armed(self, tmp_path: Path, repo: Path) -> None:
        """An allowed but unshown question also leaves final delivery armed.

        The question passed but no PostToolUse showed it, so the next Stop must still demand the report.
        """
        assert _run(tmp_path, _summary_payload(repo, "Title: x")) == {}
        assert _run(tmp_path, stop_payload(cwd=str(repo))).get("decision") == "block"

    def test_summary_only_in_previews_denied(self, tmp_path: Path, repo: Path) -> None:
        """Even every option's preview cannot replace the question's plain summary.

        The summary text is moved into each option's preview and removed from the question, which must be denied.
        """
        payload = _summary_payload(repo, "Title: x")
        first = payload["tool_input"]["questions"][0]
        first["options"] = [{**option, "preview": first["question"]} for option in first["options"]]
        first["question"] = "What next?"
        assert denial_reason(_run(tmp_path, payload)) is not None
