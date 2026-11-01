"""Subprocess tests for ``hooks/enforce-topic-header.js``.

The hook is a ``PreToolUse`` gate on ``AskUserQuestion``. Its contract:

* **Scoped to in-flight topic runs** — it acts only when the topic-report sentinel exists
  (``${TMPDIR:-/tmp}/research-topic-report-file-<CSID>``); without it every ``AskUserQuestion`` passes through
  untouched (empty stdout, exit 0).
* **Denies a missing report** — sentinel present but the report file it names absent or empty means the report step
  (Step 3 / team.md consolidation / plan.md P3) never produced it, so the call is denied with an actionable reason.
* **Fails open** — stale sentinel, implausible sentinel content, vanished ``.reports/research/`` dir, unparsable
  payload: every can't-tell case allows the call.

Every test runs the real hook under ``node`` — the hook's end-to-end behaviour is the contract, so nothing is mocked.
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

DELIVERED = "| Field | Value |\n| --- | --- |\n| Title | x |\n| PR | #1 |\n| Date | y |\n"
_REPORT_WITH_HEADER = "---\nTitle: x\nPR: #1\nDate: y\n---\n"
_REPORT_PLAIN = "---\nResearch — efficient transformers\n---\n"

_skip_node_unavailable = pytest.mark.skipif(shutil.which("node") is None, reason="requires node to execute the hook")


def _ask_payload(**overrides: object) -> dict:
    """Build a PreToolUse AskUserQuestion payload, applying `overrides`."""
    payload: dict = {
        "hook_event_name": "PreToolUse",
        "tool_name": "AskUserQuestion",
        "tool_input": {"questions": [{"question": "What next?", "header": "topic"}]},
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
    options = [{"label": label, "description": "next step"} for label in ("/research:plan", "skip")]
    text = f"{summary}\n→ {report}\nWhat next?" if summary else "What next?"
    questions = [{"question": text, "header": "topic", "options": options}]
    return _ask_payload(tool_input={"questions": questions}, transcript_path=None)


def _preview_only_payload(report: Path) -> dict:
    """Build a follow-up whose summary appears only in every option's preview, never in the question text."""
    payload = _summary_payload(report, "Title: x")
    question = payload["tool_input"]["questions"][0]
    question["options"] = [{**option, "preview": question["question"]} for option in question["options"]]
    question["question"] = "What next?"
    return payload


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


@pytest.fixture(name="topic_run")
def _topic_run(tmp_path: Path) -> tuple[Path, Path]:
    """Stage a topic run that resolved its report path: output file plus sentinel."""
    report_file = tmp_path / "repo" / ".reports" / "research" / "topic-main-2026-08-04.md"
    report_file.parent.mkdir(parents=True)
    sentinel = tmp_path / SENTINEL_NAME
    sentinel.write_text(f"{report_file}\n", encoding="utf-8")
    return report_file, sentinel


@pytest.fixture(name="written_run")
def _written_run(topic_run: tuple[Path, Path]) -> Path:
    """Stage a topic run whose report (with the header fields DELIVERED renders) is already written."""
    report_file, _ = topic_run
    report_file.write_text(_REPORT_WITH_HEADER, encoding="utf-8")
    return report_file


@_skip_node_unavailable
class TestReportGate:
    """The gate fires only for an in-flight run whose report is missing or not shown to the user."""

    def test_missing_report_is_denied(self, tmp_path: Path, topic_run: tuple[Path, Path]) -> None:
        """Sentinel present without the report file → deny, naming the path to write.

        The sentinel proves a topic run resolved its report path; asking the follow-up before the report exists would
        strand the user without it, so the denial must tell the model exactly which file to produce.
        """
        report_file, _ = topic_run

        reason = _denial_reason(_run(tmp_path, _ask_payload()))

        assert reason is not None, "AskUserQuestion must be denied while the report is missing"
        assert str(report_file) in reason
        assert "Print report header" in reason

    @pytest.mark.parametrize(
        "report_text",
        [
            pytest.param("", id="empty-report"),
            pytest.param(_REPORT_PLAIN, id="written-without-delivery"),
        ],
    )
    def test_unverified_report_is_denied(self, tmp_path: Path, topic_run: tuple[Path, Path], report_text: str) -> None:
        """A zero-byte report, or one never shown to the user, blocks the follow-up question.

        A zero-byte file counts as not written; a written report with no delivery evidence must block only the follow-up
        transition.
        """
        report_file, _ = topic_run
        report_file.write_text(report_text, encoding="utf-8")

        assert _denial_reason(_run(tmp_path, _ask_payload())) is not None

    def test_table_only_in_reply_text_is_denied_and_summary_passes(
        self, tmp_path: Path, topic_run: tuple[Path, Path]
    ) -> None:
        """A table printed as reply text before the question is denied; a plain question summary passes.

        Reply text written before a tool call can come back as an empty progress update the user never sees, so only the
        call's own fields count at question time; the denial says so and the call corrected to it passes.
        """
        report_file, _ = topic_run
        report_file.write_text("---\nTitle: x\nDate: y\nMethod: z\n---\n", encoding="utf-8")
        table = "| Field | Value |\n| --- | --- |\n| Title | x |\n| Date | y |\n| Method | z |\n"
        transcript = _write_transcript(tmp_path, table)

        reason = _denial_reason(_run(tmp_path, _ask_payload(transcript_path=str(transcript))))
        retried = _run(tmp_path, _summary_payload(report_file, "Title: x"))

        assert reason is not None
        assert "reply text before the call does not count" in reason
        assert retried == {}

    def test_report_without_table_in_transcript_is_denied(self, tmp_path: Path, topic_run: tuple[Path, Path]) -> None:
        """A written report whose transcript shows plain lines instead of the header table is denied.

        Unverified report delivery must block only the follow-up transition, naming the report in the reason.
        """
        report_file, _ = topic_run
        report_file.write_text(_REPORT_PLAIN, encoding="utf-8")
        transcript = _write_transcript(tmp_path, "Title: efficient transformers\nDate: 2026-08-08\n")

        reason = _denial_reason(_run(tmp_path, _ask_payload(transcript_path=str(transcript))))

        assert reason is not None
        assert "report" in reason

    def test_unreadable_transcript_is_denied(self, tmp_path: Path, topic_run: tuple[Path, Path]) -> None:
        """A written report whose transcript cannot be read is treated as undelivered.

        Unverified report delivery must block only the follow-up transition.
        """
        report_file, _ = topic_run
        report_file.write_text(_REPORT_PLAIN, encoding="utf-8")

        result = _run(tmp_path, _ask_payload(transcript_path=str(tmp_path / "missing.jsonl")))

        assert _denial_reason(result) is not None

    @pytest.mark.parametrize(
        "report_name",
        [
            pytest.param("topic-plan-main-2026-08-04.md", id="plan-mode-report-name"),
            pytest.param("topic-main-2026-08-04-2.md", id="counter-suffixed-report-name"),
        ],
    )
    def test_report_name_variants_are_gated(self, tmp_path: Path, report_name: str) -> None:
        """Every report-name shape a topic run can resolve is gated the same way.

        plan.md writes `topic-plan-<branch>-<date>.md`; anti-overwrite reruns resolve to `-2.md` and the sentinel path
        is used verbatim.
        """
        report_file = tmp_path / "repo" / ".reports" / "research" / report_name
        report_file.parent.mkdir(parents=True)
        (tmp_path / SENTINEL_NAME).write_text(f"{report_file}\n", encoding="utf-8")

        assert _denial_reason(_run(tmp_path, _ask_payload())) is not None

    @pytest.mark.parametrize(
        ("payload_overrides", "run_kwargs"),
        [
            pytest.param({}, {"tmpdir_suffix": "/"}, id="trailing-slash-tmpdir"),
            pytest.param({"session_id": CSID}, {"session_id_env": None}, id="session-id-from-payload"),
        ],
    )
    def test_sentinel_resolution_variants(
        self, tmp_path: Path, topic_run: tuple[Path, Path], payload_overrides: dict, run_kwargs: dict
    ) -> None:
        """The sentinel still resolves with a trailing-slash TMPDIR or a session id taken from the payload.

        MacOS exports TMPDIR with a trailing slash, and CSID falls back to the payload's session_id when the env var is
        unset; in both cases the missing report must still be found and denied.
        """
        assert _denial_reason(_run(tmp_path, _ask_payload(**payload_overrides), **run_kwargs)) is not None

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            pytest.param(r"C:\Repo\.REPORTS\RESEARCH\TOPIC-main.md", True, id="contained-path-accepted"),
            pytest.param(r"C:\Repo\.reports\research\topic-dir\..\private.md", False, id="traversal-rejected"),
        ],
    )
    def test_simulated_windows_report_path_validation(self, path: str, expected: bool) -> None:
        """Windows separators and casing are recognised without trusting escaped sentinel paths."""
        assert _call_export("isTopicReportFile", path) is expected


@_skip_node_unavailable
class TestPassThrough:
    """Everything outside an in-flight topic run passes through (empty stdout, exit 0)."""

    def test_no_sentinel(self, tmp_path: Path) -> None:
        """No topic run resolved a report path → unrelated questions are never gated."""
        assert _run(tmp_path, _ask_payload()) == {}

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param(_ask_payload(tool_name="Bash"), id="other-tool"),
            pytest.param(_ask_payload(hook_event_name="PostToolUse"), id="other-event"),
        ],
    )
    def test_non_matching_payloads(self, tmp_path: Path, topic_run: tuple[Path, Path], payload: dict) -> None:
        """Only PreToolUse AskUserQuestion is inspected; anything else is untouched."""
        assert _run(tmp_path, payload) == {}

    def test_stale_sentinel(self, tmp_path: Path, topic_run: tuple[Path, Path]) -> None:
        """Sentinel older than the enforcement window is treated as a crashed run."""
        _, sentinel = topic_run
        stale = time.time() - (TWO_HOURS_S + 600)
        os.utime(sentinel, (stale, stale))

        assert _run(tmp_path, _ask_payload()) == {}

    def test_missing_report_dir(self, tmp_path: Path, topic_run: tuple[Path, Path]) -> None:
        """Allow writes when expired research evidence is no longer available."""
        report_file, _ = topic_run
        report_file.parent.rmdir()

        assert _run(tmp_path, _ask_payload()) == {}

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
    def test_implausible_sentinel_content(self, tmp_path: Path, content: str) -> None:
        """Sentinel not holding an absolute .reports/research/topic-*.md path is ignored."""
        (tmp_path / SENTINEL_NAME).write_text(content, encoding="utf-8")

        assert _run(tmp_path, _ask_payload()) == {}

    @pytest.mark.parametrize("session_id", ["../../etc/passwd", "has space", ""])
    def test_unsafe_csid(self, tmp_path: Path, topic_run: tuple[Path, Path], session_id: str) -> None:
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
class TestStop:
    """Delivery is still checked on ``Stop`` when the follow-up question is skipped."""

    def test_blocks_undelivered_report_once(self, tmp_path: Path, written_run: Path) -> None:
        """A turn ending without the report delivery is kept going once; the same report never re-blocks.

        The first Stop blocks with a reason naming research:topic; the repeat finds the block already issued and lets
        the turn end, so a model that cannot comply is never trapped in a loop.
        """
        first = _run(tmp_path, _stop_payload())
        second = _run(tmp_path, _stop_payload())

        assert first.get("decision") == "block"
        assert "research:topic" in first["reason"]
        assert second == {}

    def test_passes_delivery_in_final_message(self, tmp_path: Path, written_run: Path) -> None:
        """The Stop payload's final message proves delivery even without a transcript."""
        assert _run(tmp_path, _stop_payload(last_assistant_message=DELIVERED)) == {}


@_skip_node_unavailable
class TestQuestionSummaryDelivery:
    """Follow-ups show summaries in question text and leave full delivery to Stop."""

    def test_plain_summary_allows_follow_up(self, tmp_path: Path, written_run: Path) -> None:
        """The matching headline and resolved report path pass without any preview."""
        payload = _summary_payload(written_run, "Title: x")

        assert all("preview" not in option for option in payload["tool_input"]["questions"][0]["options"])
        assert _run(tmp_path, payload) == {}

    def test_missing_summary_denial_names_question_fix(self, tmp_path: Path, written_run: Path) -> None:
        """Missing summary evidence names the first-question remedy."""
        reason = _denial_reason(_run(tmp_path, _summary_payload(written_run, None)))

        assert reason is not None
        assert "first question's `question` text" in reason
        assert "fixed lines" in reason

    def test_shown_summary_keeps_stop_armed_once(self, tmp_path: Path, written_run: Path) -> None:
        """A shown summary leaves one Stop continuation for the full report.

        After the follow-up passes, the first Stop still blocks until the full table is delivered; once it is, the next
        Stop is clean. The sequence is one stateful scenario across the hook's per-event invocations.
        """
        payload = _summary_payload(written_run, "Title: x")

        results = [
            _run(tmp_path, payload),
            _run(tmp_path, {**payload, "hook_event_name": "PostToolUse"}),
            _run(tmp_path, _stop_payload()),
            _run(tmp_path, _stop_payload(last_assistant_message=DELIVERED)),
            _run(tmp_path, _stop_payload()),
        ]

        assert results[:2] == [{}, {}]
        assert results[2].get("decision") == "block"
        assert results[3:] == [{}, {}]

    def test_allowed_but_unshown_follow_up_leaves_stop_armed(self, tmp_path: Path, written_run: Path) -> None:
        """An allowed but unshown question also leaves final delivery armed."""
        allowed = _run(tmp_path, _summary_payload(written_run, "Title: x"))
        stop = _run(tmp_path, _stop_payload())

        assert allowed == {}
        assert stop.get("decision") == "block"

    def test_summary_only_in_previews_is_denied(self, tmp_path: Path, written_run: Path) -> None:
        """Even every option's preview cannot replace the question's plain summary."""
        assert _denial_reason(_run(tmp_path, _preview_only_payload(written_run))) is not None
