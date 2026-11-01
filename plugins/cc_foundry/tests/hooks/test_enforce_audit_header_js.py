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

Every case runs the real hook through ``node``: the hook's behaviour end to end is the contract, so nothing is mocked.
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
from _hook_io import CSID, call_export, denial_reason, run_hook_json, stop_payload, write_transcript

HOOK = Path(__file__).resolve().parent.parent.parent / "hooks" / "enforce-audit-header.js"

STATE_DIR_NAME = f"audit-state-{CSID}"
RUN_DIR_REL = ".reports/audit/2026-08-04T10-00-00Z"
FOUR_HOURS_S = 4 * 60 * 60
#: The Step 7 report as the final message delivers it.
DELIVERED = "## Audit Report\nTotal: 1\n- broken ref\n"

_skip_node_unavailable = pytest.mark.skipif(shutil.which("node") is None, reason="requires node to execute the hook")
_run = functools.partial(run_hook_json, HOOK)
_call_export = functools.partial(call_export, HOOK)


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


def _single_option_input(label: str, description: str) -> dict:
    """Build the ``tool_input`` of a non-gate audit question (breaking ack, unknown flag) with one option.

    Examples:
        >>> _single_option_input("Skip", "report only")["questions"][0]["options"][0]["label"]
        'Skip'
    """
    return {"questions": [{"question": "Acknowledge?", "options": [{"label": label, "description": description}]}]}


def _summary_payload(cwd: str, summary: str | None) -> dict:
    """Build the audit follow-up with plain question text and no previews."""
    payload = _gate_payload(cwd=cwd, transcript_path=None)
    report = Path(cwd) / RUN_DIR_REL / "summary.jsonl"
    payload["tool_input"]["questions"][0]["question"] = (
        f"{summary}\n→ {report}\nWhat next?" if summary else "What next?"
    )
    return payload


def _move_summary_to_previews(payload: dict) -> dict:
    """Move the question text into every option's preview, leaving a bare ``What next?`` question."""
    first = payload["tool_input"]["questions"][0]
    for option in first["options"]:
        option["preview"] = first["question"]
    first["question"] = "What next?"
    return payload


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


@pytest.fixture(name="summarized_run")
def _summarized_run(audit_run: tuple[Path, Path, str]) -> str:
    """Stage an audit whose summary.jsonl holds one finding; returns the project cwd."""
    run_dir, _, cwd = audit_run
    row = {"sev": "high", "one_line": "broken ref"}
    (run_dir / "summary.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    return cwd


@pytest.fixture(name="aggregated_run")
def _aggregated_run(audit_run: tuple[Path, Path, str], summarized_run: str) -> str:
    """Stage an audit whose Step 5 aggregate holds one finding and its consolidation note; returns the project cwd."""
    run_dir, _, _ = audit_run
    (run_dir / "aggregate.md").write_text("Consolidation complete.\n", encoding="utf-8")
    return summarized_run


@_skip_node_unavailable
class TestFollowUpGate:
    """The follow-up question is denied while an in-flight audit has no aggregate or no proven report delivery."""

    def test_denies_missing_aggregate(self, tmp_path: Path, audit_run: tuple[Path, Path, str]) -> None:
        """A sentinel without summary.jsonl denies the follow-up and names the step to redo.

        Scenario: the audit reached Step 3 but findings were never aggregated, so asking what to fix would be premature.
        """
        run_dir, _, cwd = audit_run

        reason = denial_reason(_run(tmp_path, _gate_payload(cwd=cwd)))

        assert reason is not None, "the follow-up gate must be denied while the aggregate is missing"
        assert str(run_dir / "summary.jsonl") in reason
        assert "Step 5" in reason

    @pytest.mark.parametrize(
        ("aggregate", "transcript"),
        [
            pytest.param("", "", id="zero-byte-aggregate"),
            pytest.param('{"file":"a.md","sev":"high"}\n', "", id="written-aggregate-without-delivery"),
            pytest.param('{"file":"a.md","sev":"high"}\n', "missing.jsonl", id="unreadable-transcript"),
        ],
    )
    def test_denies_unproven_delivery(
        self, tmp_path: Path, audit_run: tuple[Path, Path, str], aggregate: str, transcript: str
    ) -> None:
        """An empty aggregate, or one whose delivery cannot be verified, denies the follow-up.

        Scenario: a zero-byte summary.jsonl counts as not written, consolidator output alone does not establish
        delivery to the user, and an unreadable transcript leaves delivery unverifiable (fail closed).
        """
        run_dir, _, cwd = audit_run
        (run_dir / "summary.jsonl").write_text(aggregate, encoding="utf-8")
        extra = {"transcript_path": str(tmp_path / transcript)} if transcript else {}

        assert denial_reason(_run(tmp_path, _gate_payload(cwd=cwd, **extra))) is not None

    @pytest.mark.parametrize(
        "finding",
        [
            pytest.param("", id="no-findings"),
            pytest.param('{"file": "a.md", "sev": "high", "one_line": "Missing reference"}\n', id="one-finding"),
        ],
    )
    def test_allows_current_aggregate_summary(
        self, tmp_path: Path, audit_run: tuple[Path, Path, str], finding: str
    ) -> None:
        """An empty or populated audit advances with the aggregate path and existing headline in question text.

        Scenario: the consolidated aggregate exists and the question itself carries the summary, so it passes.
        """
        run_dir, _, cwd = audit_run
        (run_dir / "summary.jsonl").write_text(finding, encoding="utf-8")
        (run_dir / "aggregate.md").write_text("Consolidation complete.\n", encoding="utf-8")

        assert _run(tmp_path, _summary_payload(cwd, "Audit Report")) == {}

    def test_denies_reply_text_report_then_allows_question_summary(self, tmp_path: Path, aggregated_run: str) -> None:
        """The Step 7 report printed as reply text before the question is denied; a plain question summary passes.

        Reply text written before a tool call can come back as an empty progress update the user never sees, so only the
        call's own fields count at question time; the denial says so and the call corrected to it passes.
        """
        transcript = write_transcript(tmp_path, "## Audit Report\nTotal: 1\nMissing reference")

        reason = denial_reason(_run(tmp_path, _gate_payload(cwd=aggregated_run, transcript_path=str(transcript))))
        retried = _run(tmp_path, _summary_payload(aggregated_run, "Audit Report"))

        assert reason is not None
        assert "reply text before the call does not count" in reason
        assert retried == {}

    def test_tagged_follow_up_requires_aggregate(self, tmp_path: Path, audit_run: tuple[Path, Path, str]) -> None:
        """The explicit workflow header must not bypass consolidation for a zero-finding summary.

        Scenario: a question tagged ``header: audit`` carries none of the legacy gate labels, yet an empty aggregate
        still denies it with the Step 5 pointer.
        """
        run_dir, _, cwd = audit_run
        (run_dir / "summary.jsonl").touch()
        transcript = write_transcript(tmp_path, "## Audit Report\nTotal: 0\nNo findings.")
        tool_input = {"questions": [{"header": "audit", "question": "What next?"}]}

        reason = denial_reason(
            _run(tmp_path, _gate_payload(cwd=cwd, transcript_path=str(transcript), tool_input=tool_input))
        )

        assert reason is not None
        assert "Step 5" in reason

    def test_denies_aggregate_without_findings_in_reply(
        self, tmp_path: Path, audit_run: tuple[Path, Path, str]
    ) -> None:
        """An unrelated summary cannot authorize the audit follow-up.

        Scenario: the aggregate holds a finding but the transcript's reply shows only a title and outcome, so the
        report gate denies instead of treating any prose as delivery.
        """
        run_dir, _, cwd = audit_run
        (run_dir / "summary.jsonl").write_text('{"file":"a.md","sev":"high"}\n', encoding="utf-8")
        transcript = write_transcript(tmp_path, "Title: audit\nOutcome: NEEDS_ATTENTION\n")

        reason = denial_reason(_run(tmp_path, _gate_payload(cwd=cwd, transcript_path=str(transcript))))

        assert reason is not None
        assert "report gate" in reason


@_skip_node_unavailable
class TestSentinelResolution:
    """The state sentinel and the run dir it names resolve across path spellings, TMPDIR shapes and session-id
    sources."""

    def test_resolves_absolute_run_dir(self, tmp_path: Path, audit_run: tuple[Path, Path, str]) -> None:
        """An absolute run-dir value is honoured as-is, independent of the payload cwd."""
        run_dir, sentinel, _ = audit_run
        sentinel.write_text(f"{run_dir}\n", encoding="utf-8")

        assert denial_reason(_run(tmp_path, _gate_payload(cwd="/nonexistent"))) is not None

    def test_resolves_windows_run_dir_canonically_and_contained(self) -> None:
        """Windows report paths resolve case-insensitively and traversal outside the audit dir is rejected.

        Scenario: the test-only ``resolveRunDir`` export is fed Windows-style paths so the simulated branch runs on
        every host, not just Windows.
        """
        resolved = _call_export("resolveRunDir", r".REPORTS\AUDIT\run-1", r"C:\Repo")

        assert isinstance(resolved, str)
        assert resolved.casefold() == r"c:\repo\.reports\audit\run-1".casefold()
        assert _call_export("resolveRunDir", r".reports\audit\..\private", r"C:\Repo") is None

    def test_resolves_sentinel_under_trailing_slash_tmpdir(
        self, tmp_path: Path, audit_run: tuple[Path, Path, str]
    ) -> None:
        """MacOS exports TMPDIR with a trailing slash — the sentinel must still resolve."""
        _, _, cwd = audit_run

        assert denial_reason(_run(tmp_path, _gate_payload(cwd=cwd), tmpdir_suffix="/")) is not None

    def test_takes_session_id_from_payload(self, tmp_path: Path, audit_run: tuple[Path, Path, str]) -> None:
        """CSID falls back to the payload's session_id when the env var is unset."""
        _, _, cwd = audit_run

        result = _run(tmp_path, _gate_payload(cwd=cwd, session_id=CSID), session_id_env=None)

        assert denial_reason(result) is not None


@_skip_node_unavailable
class TestPassThrough:
    """Everything outside an in-flight audit's follow-up gate passes untouched: empty stdout, exit 0."""

    @pytest.mark.parametrize(
        "overrides",
        [
            pytest.param(
                {"tool_input": _single_option_input("Acknowledge", "hook event name invalid")}, id="breaking-ack"
            ),
            pytest.param(
                {"tool_input": _single_option_input("Continue ignoring", "skip unknown flags")}, id="unknown-flag"
            ),
            pytest.param({"tool_input": _single_option_input("Abort", "stop and re-invoke")}, id="abort"),
            pytest.param(
                {"tool_input": _single_option_input("Acknowledge", "you may fix all of these later")},
                id="gate-wording-only-in-description",
            ),
            pytest.param({"tool_name": "Bash"}, id="other-tool"),
            pytest.param({"hook_event_name": "PostToolUse"}, id="other-event"),
            pytest.param({"tool_input": {}}, id="no-questions"),
            pytest.param({"tool_input": {"questions": "not-a-list"}}, id="questions-not-a-list"),
            pytest.param({"tool_input": {"questions": [{"options": [{"label": 42}]}]}}, id="label-not-a-string"),
        ],
    )
    def test_passes_payload_that_is_not_the_gate(
        self, tmp_path: Path, audit_run: tuple[Path, Path, str], overrides: dict
    ) -> None:
        """Only PreToolUse AskUserQuestion carrying the gate labels is inspected; anything else is untouched.

        Scenario: breaking acks and flag prompts fire before Step 5 by design, gate wording inside a description must
        not turn another question into the gate (the label field alone is matched), and an unrecognised payload shape
        reads as 'not the gate' — fail open.
        """
        _, _, cwd = audit_run

        assert _run(tmp_path, _gate_payload(cwd=cwd, **overrides)) == {}

    def test_passes_without_sentinel(self, tmp_path: Path) -> None:
        """No foundry:audit run reached Step 3 → unrelated questions are never gated."""
        assert _run(tmp_path, _gate_payload()) == {}

    def test_passes_stale_sentinel(self, tmp_path: Path, audit_run: tuple[Path, Path, str]) -> None:
        """A sentinel older than the enforcement window is treated as a crashed run.

        Scenario: the sentinel's mtime is set past the four-hour window, so a leftover from a dead session cannot
        gate a later, unrelated question.
        """
        _, sentinel, cwd = audit_run
        stale = time.time() - (FOUR_HOURS_S + 600)
        os.utime(sentinel, (stale, stale))

        assert _run(tmp_path, _gate_payload(cwd=cwd)) == {}

    def test_passes_vanished_run_dir(self, tmp_path: Path, audit_run: tuple[Path, Path, str]) -> None:
        """Run dir gone (worktree removed, TTL cleanup) → hook cannot judge, allows."""
        run_dir, _, cwd = audit_run
        run_dir.rmdir()

        assert _run(tmp_path, _gate_payload(cwd=cwd)) == {}

    @pytest.mark.parametrize("content", ["", "   \n", ".reports/review/2026-08-04T10-00-00Z\n", "/etc\n"])
    def test_passes_implausible_sentinel_content(self, tmp_path: Path, content: str) -> None:
        """A sentinel not holding a path under .reports/audit/ is ignored."""
        state_dir = tmp_path / STATE_DIR_NAME
        state_dir.mkdir()
        (state_dir / "run-dir").write_text(content, encoding="utf-8")

        assert _run(tmp_path, _gate_payload(cwd=str(tmp_path))) == {}

    @pytest.mark.parametrize("session_id", ["../../etc/passwd", "has space", ""])
    def test_passes_unsafe_csid(self, tmp_path: Path, audit_run: tuple[Path, Path, str], session_id: str) -> None:
        """A CSID that cannot name a sentinel file is discarded, never path-joined."""
        _, _, cwd = audit_run

        assert _run(tmp_path, _gate_payload(cwd=cwd), session_id_env=session_id) == {}

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

    def test_blocks_undelivered_report_once(self, tmp_path: Path, summarized_run: str) -> None:
        """A turn ending without the report delivery is kept going once; the same report never re-blocks.

        Scenario: the aggregate is written but the final message is just ``done``; the first Stop blocks, the second
        for the same report must let the session end instead of looping.
        """
        first = _run(tmp_path, stop_payload(cwd=summarized_run))
        second = _run(tmp_path, stop_payload(cwd=summarized_run))

        assert first.get("decision") == "block"
        assert "foundry:audit" in first["reason"]
        assert second == {}

    def test_passes_delivery_in_final_message(self, tmp_path: Path, summarized_run: str) -> None:
        """The Stop payload's final message proves delivery even without a transcript."""
        assert _run(tmp_path, stop_payload(cwd=summarized_run, last_assistant_message=DELIVERED)) == {}


@_skip_node_unavailable
class TestQuestionSummaryDelivery:
    """Follow-ups show summaries in question text and leave full delivery to Stop."""

    def test_plain_summary_allows_follow_up(self, tmp_path: Path, aggregated_run: str) -> None:
        """The matching headline and resolved report path pass without any preview."""
        payload = _summary_payload(aggregated_run, "Audit Report")

        assert all("preview" not in option for option in payload["tool_input"]["questions"][0]["options"])
        assert _run(tmp_path, payload) == {}

    def test_missing_summary_denial_names_question_fix(self, tmp_path: Path, aggregated_run: str) -> None:
        """Missing summary evidence names the first-question remedy.

        Scenario: the question text carries no headline at all, so the denial must tell the model where to put it.
        """
        reason = denial_reason(_run(tmp_path, _summary_payload(aggregated_run, None)))

        assert reason is not None
        assert "first question's `question` text" in reason
        assert "fixed lines" in reason

    def test_shown_summary_keeps_stop_armed_once(self, tmp_path: Path, aggregated_run: str) -> None:
        """A shown summary leaves one Stop continuation for the full report.

        Scenario: question allowed, PostToolUse recorded, then Stop blocks once for the full report, passes once the
        report is delivered, and never blocks again.
        """
        payload = _summary_payload(aggregated_run, "Audit Report")
        assert _run(tmp_path, payload) == {}
        assert _run(tmp_path, {**payload, "hook_event_name": "PostToolUse"}) == {}

        blocked = _run(tmp_path, stop_payload(cwd=aggregated_run))
        delivered = _run(tmp_path, stop_payload(cwd=aggregated_run, last_assistant_message=DELIVERED))
        again = _run(tmp_path, stop_payload(cwd=aggregated_run))

        assert (blocked.get("decision"), delivered, again) == ("block", {}, {})

    def test_allowed_but_unshown_follow_up_leaves_stop_armed(self, tmp_path: Path, aggregated_run: str) -> None:
        """An allowed but unshown question also leaves final delivery armed."""
        allowed = _run(tmp_path, _summary_payload(aggregated_run, "Audit Report"))
        stopped = _run(tmp_path, stop_payload(cwd=aggregated_run))

        assert (allowed, stopped.get("decision")) == ({}, "block")

    def test_summary_only_in_previews_is_denied(self, tmp_path: Path, aggregated_run: str) -> None:
        """Even every option's preview cannot replace the question's plain summary."""
        payload = _move_summary_to_previews(_summary_payload(aggregated_run, "Audit Report"))

        assert denial_reason(_run(tmp_path, payload)) is not None
