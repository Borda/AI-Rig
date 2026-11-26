"""Confidence contract of the bridge result envelope: derivation, reason codes, legacy reads and split merging.

Every public envelope carries ``confidence`` (derived by the harness from the peer's named gaps, never authored by the
peer), the peer's ``confidence_gaps`` and a closed-set ``confidence_null_reason`` that is set exactly when no number
exists. The tests drive each exit path of one request through the real supervisor with a stubbed child, so the code a
consumer reads is the one the harness writes, and check it against the incident ``fault`` recorded for the same stop.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest
from _bridge_prose import read_prose

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
BIN_ROOT = PLUGIN_ROOT / "bin"
if str(BIN_ROOT) not in sys.path:
    sys.path.insert(0, str(BIN_ROOT))

import bridge_call  # noqa: E402  (loaded from the installed-plugin-equivalent bin directory)
import bridge_split  # noqa: E402  (loaded from the installed-plugin-equivalent bin directory)

REASON_CODES = {
    "quota-exhausted",
    "rate-limited",
    "capacity",
    "timeout",
    "output-limit",
    "refused",
    "peer-unavailable",
    "peer-failed",
    "empty-result",
    "incomplete",
    "cancelled",
    "invalid-request",
    "unscored",
    "not-recorded",
}
QUOTA_MESSAGE = "You have exhausted your current quota. Please add credits."
RATE_MESSAGE = "Rate limit reached for requests. Please retry after 20s."
CAPACITY_MESSAGE = "Selected model is at capacity. Please try a different model."
CONTENT_FLAG = "This content was flagged for possible cybersecurity risk."
SKILL_FILES = (
    "claude-skills/review/SKILL.md",
    "claude-skills/advise/SKILL.md",
    "claude-skills/implement/SKILL.md",
    "claude-skills/result/SKILL.md",
    "codex-skills/review/SKILL.md",
    "codex-skills/advise/SKILL.md",
    "codex-skills/implement/SKILL.md",
)


def _request(tmp_path: Path, **overrides: Any) -> bridge_call.Request:
    """Build one small deterministic review request."""
    values: dict[str, Any] = {
        "verb": "review",
        "task": "Review the change.",
        "model": "test-model",
        "effort": "medium",
        "timeout_seconds": 1.0,
        "depth": 0,
        "run_id": "run-fixed",
        "workspace": tmp_path,
        "direction": "claude_to_codex",
    }
    values.update(overrides)
    return bridge_call.Request(**values)


def _core(status: str = "complete", **overrides: object) -> dict[str, object]:
    """Return a peer answer that names two gaps worth 0.15 in total."""
    core: dict[str, object] = {
        "status": status,
        "verdict": "Reviewed the change.",
        "findings": ["src/app.py:12 unchecked None"],
        "files_touched": [],
        "remaining": [],
        "blockers": [],
        "confidence_gaps": ["(-0.10) tests not run", "(-0.05) one module unread"],
        "details": [],
    }
    core.update(overrides)
    return core


def _stdout(records: list[dict[str, Any]], *, ensure_ascii: bool = True) -> str:
    """Render records as the JSONL stream a Codex child writes."""
    return "\n".join(
        json.dumps(record, ensure_ascii=ensure_ascii)
        for record in [{"type": "thread.started", "thread_id": "t-1"}, *records]
    )


def _answer(core: dict[str, object]) -> list[dict[str, Any]]:
    """Return the records of a completed Codex turn whose final message is ``core``."""
    return [
        {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(core)}},
        {"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 2}},
    ]


def _failed(message: str) -> list[dict[str, Any]]:
    """Return the two records a failed Codex turn emits."""
    return [{"type": "error", "message": message}, {"type": "turn.failed", "error": {"message": message}}]


def _child(records: list[dict[str, Any]], *, returncode: int = 0, **fields: Any) -> bridge_call.ChildOutcome:
    """Return one fake Codex child outcome."""
    return bridge_call.ChildOutcome(_stdout(records), "", returncode, fields.pop("timed_out", False), None, **fields)


def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: bridge_call.ChildOutcome, **request: Any) -> dict:
    """Run one request through the real supervisor with every child attempt returning ``outcome``."""
    monkeypatch.setattr(bridge_call, "_run_child", lambda *args: outcome)
    return bridge_call.run_request(_request(tmp_path, **request))


def _fault(tmp_path: Path, envelope: dict[str, Any]) -> str | None:
    """Return the ``fault`` of the incident an envelope links, or ``None`` when it links none."""
    if envelope["incident"] is None:
        return None
    return json.loads((tmp_path / envelope["incident"]).read_text(encoding="utf-8"))["fault"]


def _scored_envelope() -> dict[str, Any]:
    """Return a valid complete public envelope scored from one gap line."""
    return {
        "status": "complete",
        "verdict": "done",
        "findings": [],
        "files_touched": [],
        "remaining": [],
        "blockers": [],
        "confidence": 0.8,
        "confidence_gaps": ["(-0.20) tests not run"],
        "confidence_null_reason": None,
        "model": "test-model",
        "effort": "medium",
        "effort_substituted": None,
        "cost": None,
        "tokens": {},
        "duration_seconds": 0.0,
        "depth": 0,
        "run_id": "run-fixed",
        "incident": None,
        "session_id": None,
        "transcript_path": ".temp/bridge/raw-fixed.txt",
        "verb": "review",
        "direction": "claude_to_codex",
    }


class TestDerivation:
    """The number is 1.00 minus the named deductions, exact to two decimals, or ``None`` when it cannot be derived."""

    @pytest.mark.parametrize(
        ("gaps", "expected"),
        [
            pytest.param([], None, id="empty-list-is-skipped-accounting"),
            pytest.param(["(-0.00) read-only sandbox"], 1.0, id="zero-deduction"),
            pytest.param(["(-0.10) tests not run", "(-0.05) one module unread"], 0.85, id="two-gaps"),
            pytest.param(["(-0.33) a", "(-0.33) b", "(-0.33) c"], 0.01, id="hundredths-stay-exact"),
            pytest.param(["(-1.00) nothing could be read"], 0.0, id="full-deduction"),
            pytest.param(["(-0.60) first", "(-0.50) second"], None, id="deductions-above-one"),
            pytest.param(["(-1.50) too much"], None, id="single-deduction-above-one"),
            pytest.param(["tests not run"], None, id="no-deduction"),
            pytest.param(["- (-0.10) tests not run"], None, id="list-marker"),
            pytest.param(["(-0.1) tests not run"], None, id="one-decimal"),
            pytest.param(["(-0.10)"], None, id="no-gap-text"),
            pytest.param(["(-0.10) first\nsecond"], None, id="multi-line"),
            pytest.param([3], None, id="non-string-entry"),
            pytest.param(None, None, id="absent"),
            pytest.param("(-0.10) tests not run", None, id="not-a-list"),
        ],
    )
    def test_derives_from_named_gaps_only(self, gaps: object, expected: float | None) -> None:
        """A malformed list never yields a number, so a consumer never reads a guessed score."""
        assert bridge_call.derive_confidence(gaps) == expected


class TestExitPaths:
    """Every exit path of one request names exactly one of a number or a closed-set cause."""

    def test_complete_answer_is_scored_from_its_gaps(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A complete answer carries the derived number, its gap lines, and no reason code."""
        envelope = _run(tmp_path, monkeypatch, _child(_answer(_core())))
        assert envelope["status"] == "complete"
        assert envelope["confidence"] == 0.85
        assert envelope["confidence_gaps"] == ["(-0.10) tests not run", "(-0.05) one module unread"]
        assert envelope["confidence_null_reason"] is None

    @pytest.mark.parametrize(
        "gaps",
        [
            pytest.param(["tests not run"], id="malformed-line"),
            pytest.param(["(-0.70) a", "(-0.40) b"], id="over-one"),
            pytest.param([], id="empty-list"),
        ],
    )
    def test_unusable_gaps_keep_the_answer_but_drop_the_number(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gaps: list[str]
    ) -> None:
        """A bad gap list is ``unscored``: the findings still arrive and the lines stay visible."""
        envelope = _run(tmp_path, monkeypatch, _child(_answer(_core(confidence_gaps=gaps))))
        assert envelope["status"] == "complete"
        assert envelope["findings"] == ["src/app.py:12 unchecked None"]
        assert envelope["confidence"] is None
        assert envelope["confidence_null_reason"] == "unscored"
        assert envelope["confidence_gaps"] == gaps

    def test_answer_without_a_gap_list_is_unscored(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A peer that omits only the gap list still delivers its answer, with no number."""
        core = _core()
        del core["confidence_gaps"]
        envelope = _run(tmp_path, monkeypatch, _child(_answer(core)))
        assert envelope["status"] == "complete"
        assert envelope["findings"] == ["src/app.py:12 unchecked None"]
        assert (envelope["confidence"], envelope["confidence_null_reason"]) == (None, "unscored")
        assert envelope["confidence_gaps"] == []

    @pytest.mark.parametrize("status", ["partial", "blocked"])
    def test_peer_reported_incomplete_answer_carries_no_number(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
    ) -> None:
        """A peer-authored partial or blocked answer is ``incomplete`` and keeps its named gaps."""
        envelope = _run(tmp_path, monkeypatch, _child(_answer(_core(status))))
        assert envelope["status"] == status
        assert (envelope["confidence"], envelope["confidence_null_reason"]) == (None, "incomplete")
        assert envelope["confidence_gaps"] == ["(-0.10) tests not run", "(-0.05) one module unread"]
        assert envelope["incident"] is None

    @pytest.mark.parametrize(
        ("message", "code"),
        [
            pytest.param(QUOTA_MESSAGE, "quota-exhausted", id="quota"),
            pytest.param(RATE_MESSAGE, "rate-limited", id="rate"),
            pytest.param(CAPACITY_MESSAGE, "capacity", id="capacity"),
        ],
    )
    def test_provider_stop_code_equals_the_incident_fault(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, message: str, code: str
    ) -> None:
        """A classified provider stop names the same class in the envelope and in the incident."""
        envelope = _run(tmp_path, monkeypatch, _child(_failed(message), returncode=1))
        assert envelope["status"] == "blocked"
        assert (envelope["confidence"], envelope["confidence_null_reason"]) == (None, code)
        assert _fault(tmp_path, envelope) == code

    def test_classified_stop_at_the_cutoff_keeps_the_provider_code(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A timeout that carries a classified stop reports the class, not ``timeout``."""
        envelope = _run(tmp_path, monkeypatch, _child(_failed(QUOTA_MESSAGE), returncode=None, timed_out=True))
        assert envelope["status"] == "timeout"
        assert envelope["confidence_null_reason"] == "quota-exhausted"
        assert _fault(tmp_path, envelope) == "quota-exhausted"

    @pytest.mark.parametrize(
        ("outcome", "request_overrides", "status", "code", "fault"),
        [
            pytest.param(
                _child([], returncode=None, timed_out=True),
                {"verb": "implement"},
                "timeout",
                "timeout",
                "timeout",
                id="timeout",
            ),
            pytest.param(
                _child([], returncode=None, output_limited=True),
                {},
                "blocked",
                "output-limit",
                "output-limit",
                id="output-limit",
            ),
            pytest.param(
                bridge_call.ChildOutcome("", "", None, False, "No such file or directory: 'codex'", started=False),
                {},
                "blocked",
                "peer-unavailable",
                "blocked",
                id="peer-unavailable",
            ),
            pytest.param(
                bridge_call.ChildOutcome("", "", None, False, "cancelled by job owner", cancelled=True),
                {},
                "blocked",
                "cancelled",
                "blocked",
                id="cancelled-while-running",
            ),
            pytest.param(
                bridge_call.ChildOutcome("", "", 0, False, "child output drain exceeded the Bridge cleanup grace"),
                {},
                "blocked",
                "peer-failed",
                "blocked",
                id="undrained-child",
            ),
            pytest.param(
                _child(_failed(CONTENT_FLAG), returncode=1),
                {},
                "blocked",
                "peer-failed",
                "blocked",
                id="unclassified-failure",
            ),
            pytest.param(_child([], returncode=0), {}, "blocked", "empty-result", "blocked", id="no-answer"),
            pytest.param(
                _child(_answer({"status": "complete", "verdict": "ok"})),
                {},
                "blocked",
                "empty-result",
                "blocked",
                id="invalid-answer",
            ),
            pytest.param(_child([]), {"depth": 1}, "refused", "refused", "refused", id="recursion-depth"),
            pytest.param(_child([]), {"effort": "turbo"}, "blocked", "invalid-request", "blocked", id="bad-effort"),
        ],
    )
    def test_harness_built_result_names_its_cause(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        outcome: bridge_call.ChildOutcome,
        request_overrides: dict[str, Any],
        status: str,
        code: str,
        fault: str,
    ) -> None:
        """Each harness-built result carries no number, its own code, and the incident fault recorded for it."""
        envelope = _run(tmp_path, monkeypatch, outcome, **request_overrides)
        assert envelope["status"] == status
        assert (envelope["confidence"], envelope["confidence_null_reason"]) == (None, code)
        assert envelope["confidence_gaps"] == []
        assert _fault(tmp_path, envelope) == fault

    def test_failed_github_preflight_is_refused(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A refused ``github-read`` preflight is ``refused`` with the ``network-preflight`` incident fault."""
        monkeypatch.setattr(bridge_call, "_GITHUB_READ_PLATFORMS", (sys.platform,))
        monkeypatch.setattr(bridge_call, "_github_read_preflight", lambda request: "the probe failed")
        envelope = _run(tmp_path, monkeypatch, _child([]), network="github-read")
        assert envelope["status"] == "refused"
        assert envelope["confidence_null_reason"] == "refused"
        assert _fault(tmp_path, envelope) == "network-preflight"

    def test_cancellation_after_completion_drops_the_score(self) -> None:
        """A late cancellation keeps the finished work and gaps visible but withdraws the number."""
        cancelled = bridge_call._cancelled_envelope(_scored_envelope())
        assert cancelled["status"] == "blocked"
        assert (cancelled["confidence"], cancelled["confidence_null_reason"]) == (None, "cancelled")
        assert cancelled["confidence_gaps"] == ["(-0.20) tests not run"]


class TestEnvelopeBoundary:
    """The public validator rejects an envelope whose confidence fields contradict each other or its gaps."""

    def test_accepts_a_scored_envelope(self) -> None:
        """A complete envelope whose number equals its own gap lines validates."""
        assert bridge_call.validate_envelope(_scored_envelope())["confidence"] == 0.8

    @pytest.mark.parametrize(
        "overrides",
        [
            pytest.param({"confidence": 0.9}, id="number-not-derived-from-gaps"),
            pytest.param({"confidence": 0.75, "confidence_gaps": []}, id="constant-without-gaps"),
            pytest.param({"status": "partial"}, id="number-on-partial"),
            pytest.param({"confidence": None}, id="neither-number-nor-code"),
            pytest.param({"confidence_null_reason": "timeout"}, id="both-number-and-code"),
            pytest.param({"confidence": None, "confidence_null_reason": "slow"}, id="unknown-code"),
            pytest.param({"confidence": True}, id="boolean-number"),
            pytest.param({"confidence": 1.2, "confidence_gaps": []}, id="out-of-range"),
            pytest.param({"confidence_gaps": "(-0.20) tests not run"}, id="gaps-not-a-list"),
        ],
    )
    def test_rejects_contradictory_confidence(self, overrides: dict[str, object]) -> None:
        """A stored or forwarded envelope cannot claim a number its gaps do not support."""
        with pytest.raises(ValueError, match="confidence"):
            bridge_call.validate_envelope({**_scored_envelope(), **overrides})

    @pytest.mark.parametrize(
        ("reason", "gaps", "message"),
        [
            pytest.param([], [], "confidence_null_reason", id="array-reason"),
            pytest.param({}, [], "confidence_null_reason", id="object-reason"),
            pytest.param(1, [], "confidence_null_reason", id="number-reason"),
            pytest.param(True, [], "confidence_null_reason", id="boolean-reason"),
            pytest.param("unscored", ["(-0.20) tests not run"], "confidence_gaps", id="scorable-gaps"),
            pytest.param("unscored", ["(-1.00) no coverage"], "confidence_gaps", id="zero-score"),
            pytest.param("not-recorded", ["(-0.00) no limits"], "confidence_gaps", id="scorable-legacy-claim"),
            pytest.param("not-recorded", ["unscorable gap"], "confidence_gaps", id="unscorable-legacy-claim"),
        ],
    )
    def test_stored_confidence_rejection_is_a_cli_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], reason: object, gaps: list[str], message: str
    ) -> None:
        """A finished job with malformed or contradictory confidence yields a structured result error."""
        paths = bridge_call.BridgePaths(tmp_path)
        paths.prepare()
        job_id = "88888888-8888-4888-8888-888888888888"
        result = {**_scored_envelope(), "confidence": None, "confidence_gaps": gaps, "confidence_null_reason": reason}
        record = {"job_id": job_id, "status": "finished", "result": result}
        (paths.jobs / f"{job_id}.json").write_text(json.dumps(record), encoding="utf-8")

        exit_code = bridge_call.main(["result", "--workspace", str(tmp_path), "--job-id", job_id])

        captured = capsys.readouterr()
        assert exit_code == 2
        error = json.loads(captured.out)
        assert error["status"] == "error"
        assert message in error["error"]
        assert captured.err == ""

    @pytest.mark.parametrize("status", sorted(bridge_call.FINAL_STATUSES - {"complete"}))
    def test_not_recorded_requires_empty_gaps_regardless_of_status(self, status: str) -> None:
        """Legacy envelopes never carry named confidence gaps."""
        result = {
            **_scored_envelope(),
            "status": status,
            "confidence": None,
            "confidence_null_reason": "not-recorded",
        }
        with pytest.raises(ValueError, match="confidence_gaps"):
            bridge_call.read_stored_envelope(result)

    @pytest.mark.parametrize(
        ("status", "reason"),
        [
            pytest.param("complete", "timeout", id="complete-with-timeout"),
            pytest.param("complete", "incomplete", id="complete-with-incomplete"),
            pytest.param("complete", "cancelled", id="complete-with-cancelled"),
            pytest.param("blocked", "unscored", id="blocked-with-unscored"),
            pytest.param("blocked", "refused", id="blocked-with-refused"),
            pytest.param("blocked", "timeout", id="blocked-with-timeout"),
            pytest.param("partial", "timeout", id="partial-with-timeout"),
            pytest.param("partial", "quota-exhausted", id="partial-with-provider-stop"),
            pytest.param("timeout", "output-limit", id="timeout-with-output-limit"),
            pytest.param("refused", "invalid-request", id="refused-with-invalid-request"),
        ],
    )
    def test_rejects_a_reason_its_status_cannot_produce(self, status: str, reason: str) -> None:
        """A code is valid only beside a status its cause yields, so a tampered record cannot pair them freely."""
        envelope = {**_scored_envelope(), "status": status, "confidence": None, "confidence_null_reason": reason}
        with pytest.raises(ValueError, match="cannot accompany status"):
            bridge_call.validate_envelope(envelope)

    @pytest.mark.parametrize("reason", sorted(REASON_CODES))
    def test_accepts_every_status_the_table_allows_for_a_reason(self, reason: str) -> None:
        """Exactly the statuses of the reason's table row validate; every other final status is refused."""
        for status in sorted(bridge_call.FINAL_STATUSES):
            envelope = {
                **_scored_envelope(),
                "status": status,
                "confidence": None,
                "confidence_gaps": [],
                "confidence_null_reason": reason,
            }
            if status in bridge_call.CONFIDENCE_REASON_STATUSES[reason]:
                assert bridge_call.validate_envelope(envelope)["confidence_null_reason"] == reason
            else:
                with pytest.raises(ValueError, match="cannot accompany status"):
                    bridge_call.validate_envelope(envelope)

    def test_legacy_stored_envelope_is_read_as_not_recorded(self) -> None:
        """An envelope written before the confidence fields existed is upgraded on read, never rejected."""
        legacy = {key: value for key, value in _scored_envelope().items() if key not in bridge_call.CONFIDENCE_FIELDS}
        upgraded = bridge_call.read_stored_envelope(legacy)
        assert (upgraded["confidence"], upgraded["confidence_null_reason"]) == (None, "not-recorded")
        assert upgraded["confidence_gaps"] == []

    def test_partly_upgraded_stored_envelope_is_rejected(self) -> None:
        """An envelope carrying only some confidence fields is malformed, not legacy."""
        partial = {key: value for key, value in _scored_envelope().items() if key != "confidence_null_reason"}
        with pytest.raises(ValueError, match="missing or unknown fields"):
            bridge_call.read_stored_envelope(partial)


class TestPeerContract:
    """The peer names gaps; it never authors a number or a reason code."""

    @pytest.mark.parametrize(
        "text",
        ["", "Final prose instead of an envelope", "[]", None, pytest.param({}, id="incomplete-object")],
    )
    def test_an_unusable_final_message_supersedes_a_scored_draft(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: object
    ) -> None:
        """The draft never supplies a score or verdict if the final agent message is unusable."""
        draft, completed = _answer(_core())
        final = {"type": "item.completed", "item": {"type": "agent_message", "text": text}}
        result = _run(tmp_path, monkeypatch, _child([draft, final, completed]))
        assert (result["status"], result["confidence"], result["confidence_null_reason"]) == (
            "blocked",
            None,
            "empty-result",
        )
        assert result["findings"] == []

    @pytest.mark.parametrize("kind", ["reasoning", "file_change", "command_execution", None])
    def test_non_message_events_cannot_replace_the_final_answer(self, kind: str | None) -> None:
        """Only completed agent message events supply the Codex answer."""
        core = _core()
        final, completed = _answer(core)
        distractor = {"type": "item.completed", "item": {"type": kind, "text": json.dumps(_core(verdict="Other"))}}
        parsed = bridge_call._parse_output(_stdout([final, distractor, completed]), "codex")
        assert parsed.core == core

    @pytest.mark.parametrize("status", [pytest.param([], id="array"), pytest.param({}, id="object"), None, 0, False])
    def test_bad_peer_status_returns_an_empty_result(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: object
    ) -> None:
        """Non-string statuses always follow the structured result-failure route."""
        result = _run(tmp_path, monkeypatch, _child(_answer(_core(status=status))))
        assert (result["status"], result["confidence"], result["confidence_null_reason"]) == (
            "blocked",
            None,
            "empty-result",
        )

    @pytest.mark.parametrize("separator", [pytest.param("\u2028", id="line"), pytest.param("\u2029", id="paragraph")])
    def test_jsonl_keeps_raw_unicode_separators_inside_the_final_message(self, separator: str) -> None:
        """The LF frame boundary preserves legal Unicode inside a JSON string."""
        core = _core(verdict=f"First{separator}second")
        final, completed = _answer(core)
        final["item"]["text"] = json.dumps(core, ensure_ascii=False)
        parsed = bridge_call._parse_output(_stdout([final, completed], ensure_ascii=False), "codex")
        assert parsed.core == core

    def test_core_schema_requires_gap_strings_and_excludes_harness_fields(self) -> None:
        """The child output schema asks for string gap lines and leaves the number to the harness."""
        schema = json.loads((PLUGIN_ROOT / "schemas" / "envelope.schema.json").read_text(encoding="utf-8"))
        assert "confidence_gaps" in schema["required"]
        assert schema["properties"]["confidence_gaps"] == {"items": {"type": "string"}, "type": "array"}
        assert {"confidence", "confidence_null_reason"}.isdisjoint(schema["properties"])

    @pytest.mark.parametrize("field", ["confidence", "confidence_null_reason"])
    def test_peer_cannot_author_harness_confidence_fields(self, field: str) -> None:
        """A child answer that claims a number or a code is an invalid model result."""
        with pytest.raises(ValueError, match="peer fields"):
            bridge_call.validate_model_core({**_core(), field: 0.99})

    @pytest.mark.parametrize("verb", ["advise", "review", "implement"])
    @pytest.mark.parametrize("host", ["codex", "claude"])
    def test_prompt_asks_for_named_gaps_not_a_number(self, tmp_path: Path, verb: str, host: str) -> None:
        """Every prompt asks for gap lines in the derivable form and says the harness computes the number."""
        prompt = bridge_call._prompt_with_budget(_request(tmp_path, verb=verb), host=host)
        assert "confidence_gaps" in prompt
        assert '"(-0.NN) <limit>"' in prompt
        assert "The harness derives the confidence as 1.00 minus their sum" in prompt
        assert 'say so in one "(-0.00) <reason>" entry' in prompt


class TestCodeSetParity:
    """The enum, the public schema and the consumer-facing table name the same closed set."""

    def test_enum_matches_the_documented_set(self) -> None:
        """Adding or dropping a code is a contract change every reader must see."""
        assert set(bridge_call.CONFIDENCE_NULL_REASONS) == REASON_CODES

    def test_harness_schema_enum_matches(self) -> None:
        """The public schema accepts exactly the closed set plus null."""
        schema = json.loads((PLUGIN_ROOT / "schemas" / "harness-envelope.schema.json").read_text(encoding="utf-8"))
        enum = schema["properties"]["confidence_null_reason"]["enum"]
        assert set(enum) - {None} == REASON_CODES
        assert None in enum

    @pytest.mark.parametrize(
        ("field", "condition", "keyword", "size"),
        [
            pytest.param("confidence", {"type": "number"}, "minItems", 1, id="numeric-confidence-needs-gaps"),
            pytest.param("confidence_null_reason", {"const": "not-recorded"}, "maxItems", 0, id="legacy-no-gaps"),
        ],
    )
    def test_schema_constrains_gap_counts(self, field: str, condition: dict[str, str], keyword: str, size: int) -> None:
        """The schema constrains the gap counts the shared confidence validator requires."""
        schema = json.loads((PLUGIN_ROOT / "schemas" / "harness-envelope.schema.json").read_text(encoding="utf-8"))
        matching = [
            clause["then"]["properties"].get("confidence_gaps", {}).get(keyword)
            for clause in schema["allOf"]
            if clause["if"]["properties"].get(field) == condition
        ]
        assert matching == [size]
        bad_fields = (
            {"confidence": 1.0, "confidence_gaps": []}
            if keyword == "minItems"
            else {"confidence": None, "confidence_null_reason": "not-recorded"}
        )
        with pytest.raises(ValueError, match="confidence_gaps"):
            bridge_call.validate_confidence_fields({**_scored_envelope(), **bad_fields})

    def test_envelope_rule_table_lists_every_code_once(self) -> None:
        """The consumer-facing table in the envelope rule has one row per code."""
        lines = (PLUGIN_ROOT / "rules" / "envelope.md").read_text(encoding="utf-8").splitlines()
        start = lines.index("## Confidence")
        end = next(index for index in range(start + 1, len(lines)) if lines[index].startswith("## "))
        rows = [line for line in lines[start:end] if line.startswith("| `")]
        codes = [row.split("|")[1].strip().strip("`") for row in rows]
        assert sorted(codes) == sorted(REASON_CODES)

    def test_reason_status_pairs_match_the_documented_table(self) -> None:
        """The Status column of each table row is the set of statuses the validator accepts for that code."""
        lines = (PLUGIN_ROOT / "rules" / "envelope.md").read_text(encoding="utf-8").splitlines()
        start = lines.index("## Confidence")
        end = next(index for index in range(start + 1, len(lines)) if lines[index].startswith("## "))
        documented: dict[str, set[str]] = {}
        for line in lines[start:end]:
            if not line.startswith("| `"):
                continue
            cells = [cell.strip() for cell in line.split("|")]
            code, status_cell = cells[1].strip("`"), cells[2]
            documented[code] = (
                set(bridge_call.FINAL_STATUSES) if status_cell == "any" else set(re.findall(r"`(\w+)`", status_cell))
            )
        assert documented == {code: set(statuses) for code, statuses in bridge_call.CONFIDENCE_REASON_STATUSES.items()}

    def test_harness_schema_pairs_match_the_validator(self) -> None:
        """The public schema's ``allOf`` pairs allow, for each code, the statuses the Python validator allows."""
        schema = json.loads((PLUGIN_ROOT / "schemas" / "harness-envelope.schema.json").read_text(encoding="utf-8"))
        for reason in sorted(REASON_CODES):
            allowed = set(bridge_call.FINAL_STATUSES)
            for clause in schema["allOf"]:
                condition = clause["if"]["properties"].get("confidence_null_reason")
                if condition is None:
                    continue
                members = set(condition["enum"]) if "enum" in condition else {condition["const"]}
                if reason in members and "status" in clause["then"]["properties"]:
                    status = clause["then"]["properties"]["status"]
                    allowed &= set(status["enum"]) if "enum" in status else {status["const"]}
            assert allowed == set(bridge_call.CONFIDENCE_REASON_STATUSES[reason]), reason

    @pytest.mark.parametrize(
        ("relative_path", "phrases"),
        [
            pytest.param(
                "rules/envelope.md",
                (
                    "every wave, the last included, carries `confidence: null`",
                    "The caller derives the partition score after the last wave",
                    "the lowest `splits[].confidence` across every wave's rows",
                    "only when every group of every wave has a number",
                    "never take the minimum over the scored groups alone",
                    "`(-0.NN) split N: <limit>`",
                    "`splits[].confidence_gaps`",
                    "keep deductions only from the first group with the lowest score",
                    "`(-0.00)` for every other group",
                ),
                id="envelope-rule",
            ),
            pytest.param(
                "claude-skills/review/SKILL.md",
                (
                    "no wave carries a `confidence`, the last one included",
                    "Derive the partition score yourself after the last wave",
                    "the lowest `splits[].confidence` across every wave's rows",
                    "every wave has an empty `skipped` list",
                    "report each skipped path and reason once as a gap without a deduction",
                    "Report the whole-partition score separately from per-wave confidence",
                    "never take the minimum over the scored groups alone",
                    "`(-0.NN) split N: <limit>`",
                    "`splits[].confidence_gaps`",
                    "keep deductions only from the first group with the lowest score",
                    "`(-0.00)` for every other group",
                ),
                id="review-skill",
            ),
        ],
    )
    def test_partition_score_is_derived_by_the_caller_from_every_wave(
        self, relative_path: str, phrases: tuple[str, ...]
    ) -> None:
        """The rule and the skill say no wave scores a partition and how the caller derives the score from all waves."""
        prose = read_prose(PLUGIN_ROOT / relative_path)
        assert [phrase for phrase in phrases if phrase not in prose] == []

    @pytest.mark.parametrize("relative_path", SKILL_FILES)
    def test_every_skill_reports_the_code_and_never_a_substitute(self, relative_path: str) -> None:
        """Each Claude and Codex skill tells the caller to report the code and never invent a number."""
        prose = read_prose(PLUGIN_ROOT / relative_path)
        assert "`confidence_null_reason` names the exact cause" in prose
        assert "never substitute a number" in prose


class TestSplitMerge:
    """A split review is scored only when every scope is, by its weakest scope."""

    def test_helper_codes_belong_to_the_bridge_set(self) -> None:
        """The split helper's own codes stay inside the envelope's closed set, so a consumer reads one vocabulary."""
        helper_codes = {
            bridge_split.SCOPE_NOT_RUN,
            bridge_split.SCOPE_NO_ENVELOPE,
            bridge_split.SCOPE_REFUSED_INVOCATION,
            bridge_split.SCOPE_BACKSTOP,
            bridge_split.QUOTA_REASON,
            bridge_split.INCOMPLETE_REASON,
            "not-recorded",
        }
        assert helper_codes <= bridge_call.CONFIDENCE_NULL_REASONS

    @staticmethod
    def _row(index: int, status: str, confidence: float | None, reason: str | None, **extra: Any) -> dict:
        """Build one kept scope row."""
        gaps = [] if confidence is None else [f"(-{1 - confidence:.2f}) scope limit"]
        return {
            "index": index,
            "status": status,
            "confidence": confidence,
            "confidence_gaps": extra.pop("gaps", gaps),
            "confidence_null_reason": reason,
            **extra,
        }

    def test_all_scored_scopes_give_the_lowest_score(self) -> None:
        """Only the lowest scope contributes deductions, while every gap keeps its scope number."""
        merged = bridge_split.merge(
            [
                self._row(1, "complete", 0.9, None, gaps=["(-0.10) tests not run"]),
                self._row(2, "complete", 0.8, None, gaps=["(-0.15) one file unread", "(-0.05) one check skipped"]),
            ]
        )
        bridge_call.validate_confidence_fields(merged, merged=True)
        assert (merged["confidence"], merged["confidence_null_reason"]) == (0.8, None)
        assert merged["confidence_gaps"] == [
            "(-0.00) split 1: tests not run",
            "(-0.15) split 2: one file unread",
            "(-0.05) split 2: one check skipped",
        ]
        assert merged["splits"][0]["confidence_gaps"] == ["(-0.10) tests not run"]

    def test_tied_scopes_use_only_the_first_lowest_scope_deductions(self) -> None:
        """Two equal scope scores must never count the same score shortfall twice."""
        merged = bridge_split.merge(
            [
                self._row(1, "complete", 0.9, None, gaps=["(-0.10) tests not run"]),
                self._row(2, "complete", 0.9, None, gaps=["(-0.10) one file unread"]),
            ]
        )
        bridge_call.validate_confidence_fields(merged, merged=True)
        assert merged["confidence_gaps"] == ["(-0.10) split 1: tests not run", "(-0.00) split 2: one file unread"]

    @pytest.mark.parametrize(
        ("gap", "scoped"),
        [
            pytest.param(
                "(-0.20) tests not run", "(-0.20) split 2: tests not run", id="well-formed-keeps-deduction-first"
            ),
            pytest.param("(-0.00) nothing limits it", "(-0.00) split 2: nothing limits it", id="zero-deduction"),
            pytest.param("tests not run", "split 2: tests not run", id="no-deduction-stays-malformed"),
            pytest.param("(-0.20)", "split 2: (-0.20)", id="no-limit-stays-malformed"),
            pytest.param("(-0.2) tests not run", "split 2: (-0.2) tests not run", id="one-decimal-stays-malformed"),
            pytest.param("(-0.20) a\nb", "split 2: (-0.20) a\nb", id="multi-line-stays-malformed"),
        ],
    )
    def test_gap_lines_stay_strings_that_name_their_scope(self, gap: str, scoped: str) -> None:
        """A split's ``confidence_gaps`` has the single-call type, and no gap line gains a deduction it never had."""
        merged = bridge_split.merge([self._row(2, "complete", None, "unscored", gaps=[gap])])
        assert merged["confidence_gaps"] == ["split 2: confidence unavailable (complete, unscored)", scoped]
        assert all(isinstance(line, str) for line in merged["confidence_gaps"])

    def test_scoped_gap_lines_are_read_like_single_call_lines(self) -> None:
        """A caller that parses ``(-0.NN) <limit>`` lines reads a split's lines with the same parser."""
        merged = bridge_split.merge(
            [self._row(1, "complete", 0.85, None, gaps=["(-0.10) tests not run", "(-0.05) one module unread"])]
        )
        bridge_call.validate_confidence_fields(merged, merged=True)
        assert bridge_call.derive_confidence(merged["confidence_gaps"]) == 0.85

    def test_one_unscored_scope_withholds_the_number(self) -> None:
        """A scope without a score is a gap no other scope can stand in for."""
        merged = bridge_split.merge([self._row(1, "complete", 0.9, None), self._row(2, "timeout", None, "timeout")])
        assert (merged["confidence"], merged["confidence_null_reason"]) == (None, "timeout")

    def test_unscored_split_names_each_scope_without_a_score(self) -> None:
        """Scorable gaps from one scope cannot supply a number for another scope."""
        merged = bridge_split.merge(
            [
                self._row(1, "complete", None, "unscored"),
                self._row(2, "complete", 0.9, None, gaps=["(-0.10) tests not run"]),
                self._row(3, "complete", None, "not-recorded"),
            ]
        )
        assert (merged["status"], merged["confidence"], merged["confidence_null_reason"]) == (
            "complete",
            None,
            "unscored",
        )
        assert merged["confidence_gaps"] == [
            "split 1: confidence unavailable (complete, unscored)",
            "(-0.10) split 2: tests not run",
            "split 3: confidence unavailable (complete, not-recorded)",
        ]
        assert bridge_call.derive_confidence(merged["confidence_gaps"]) is None

    @pytest.mark.parametrize("gaps", [pytest.param([], id="missing"), pytest.param(["Unrelated."], id="unrelated")])
    def test_shared_validator_rejects_a_merge_without_its_unscored_scope_gap(self, gaps: list[str]) -> None:
        """Non-scorable aggregate gaps still have to identify every unscored scope."""
        merged = bridge_split.merge([self._row(1, "complete", None, "unscored")])
        with pytest.raises(ValueError, match="unscored scope"):
            bridge_call.validate_confidence_fields({**merged, "confidence_gaps": gaps}, merged=True)

    @pytest.mark.parametrize("waves", [1, 2])
    def test_skipped_files_withhold_partition_confidence(self, waves: int) -> None:
        """Skipped files make even the last wave partial and leave its group score intact."""
        merged = bridge_split.merge([self._row(1, "complete", 0.9, None, gaps=["(-0.10) tests not run"])])
        skipped = [{"path": "image.bin", "reason": "binary"}, {"path": "deps.lock", "reason": "marked -diff"}]
        output = bridge_split.account_for_later_waves({**merged, "wave": waves, "waves": waves, "skipped": skipped})
        assert (output["status"], output["confidence"], output["confidence_null_reason"]) == (
            "partial",
            None,
            "incomplete",
        )
        assert output["confidence_gaps"][-2:] == ["skipped image.bin: binary", "skipped deps.lock: marked -diff"]
        assert bridge_call.derive_confidence(output["confidence_gaps"]) is None
        assert output["splits"][0]["confidence"] == 0.9

    def test_quota_stop_is_named_before_other_causes(self) -> None:
        """A quota stop in any scope is the merged cause, as it is for the merged incident."""
        merged = bridge_split.merge(
            [
                self._row(1, "timeout", None, "timeout"),
                self._row(2, "blocked", None, "quota-exhausted"),
            ]
        )
        assert merged["confidence_null_reason"] == "quota-exhausted"

    def test_wave_with_later_groups_withholds_the_number(self) -> None:
        """One wave of several never reports a score for the whole partition."""
        merged = bridge_split.merge([self._row(1, "complete", 0.9, None)])
        output = bridge_split.account_for_later_waves({**merged, "wave": 1, "waves": 2, "later_groups": [4]})
        assert output["status"] == "partial"
        assert (output["confidence"], output["confidence_null_reason"]) == (None, "incomplete")

    @staticmethod
    def _wave(rows: list[dict[str, Any]], wave: int, waves: int) -> dict[str, Any]:
        """Return the envelope the helper prints for one wave of a partition of ``waves`` waves."""
        extra: dict[str, Any] = {"wave": wave, "waves": waves}
        if wave < waves:
            extra["later_groups"] = [wave * 3 + 1, waves * 3]
        return bridge_split.account_for_later_waves({**bridge_split.merge(rows), **extra})

    @pytest.mark.parametrize("wave", [1, 2, 3])
    def test_every_wave_of_a_partition_withholds_the_number(self, wave: int) -> None:
        """The last wave covers only its own groups, so it never stands for a partition an earlier wave scored lower."""
        rows = [self._row(index, "complete", 0.95, None) for index in range(wave * 3 - 2, wave * 3 + 1)]
        output = self._wave(rows, wave, 3)
        assert (output["confidence"], output["confidence_null_reason"]) == (None, "incomplete")

    def test_the_last_wave_does_not_hide_a_lower_earlier_score(self) -> None:
        """The partition score a caller derives from all waves' rows is the lowest group score, not the last wave's."""
        waves = [
            self._wave([self._row(1, "complete", 0.4, None), self._row(2, "timeout", None, "timeout")], 1, 3),
            self._wave([self._row(4, "complete", 0.9, None)], 2, 3),
            self._wave([self._row(7, "complete", 0.95, None)], 3, 3),
        ]
        assert [wave["confidence"] for wave in waves] == [None, None, None]
        scores = [row["confidence"] for wave in waves for row in wave["splits"]]
        assert min(score for score in scores if score is not None) == 0.4
        assert None in scores

    def test_a_wave_names_incomplete_whatever_its_rows_were_unscored_for(self) -> None:
        """A wave before the last is ``incomplete``; a row's own code (here a timeout) stays in the row."""
        output = self._wave([self._row(1, "complete", 0.9, None), self._row(2, "timeout", None, "timeout")], 1, 2)
        assert (output["confidence"], output["confidence_null_reason"]) == (None, "incomplete")
        assert [row["confidence_null_reason"] for row in output["splits"]] == [None, "timeout"]

    @pytest.mark.parametrize("wave", [1, 2])
    def test_a_quota_stop_stays_the_cause_of_its_wave(self, wave: int) -> None:
        """A provider stop ends every later call, so it outranks ``incomplete`` on any wave that hit it."""
        output = self._wave(
            [self._row(1, "complete", 0.9, None), self._row(2, "blocked", None, "quota-exhausted")], wave, 2
        )
        assert (output["confidence"], output["confidence_null_reason"]) == (None, "quota-exhausted")

    @pytest.mark.parametrize("wave", [1, 2])
    @pytest.mark.parametrize(
        "rows",
        [
            pytest.param([("complete", 0.9, None)], id="scored"),
            pytest.param([("complete", None, "unscored")], id="complete-unscored"),
            pytest.param([("partial", None, "incomplete")], id="peer-partial"),
            pytest.param([("timeout", None, "timeout"), ("timeout", None, "timeout")], id="all-timed-out"),
            pytest.param([("blocked", None, "peer-failed"), ("timeout", None, "timeout")], id="mixed-failures"),
            pytest.param([("blocked", None, "rate-limited")], id="rate-limited"),
            pytest.param([("complete", 0.9, None), ("blocked", None, "quota-exhausted")], id="quota-stop"),
        ],
    )
    def test_a_wave_of_a_partition_carries_only_the_partition_codes(
        self, rows: list[tuple[str, float | None, str | None]], wave: int
    ) -> None:
        """Whatever its rows hold, a wave is ``incomplete`` or ``quota-exhausted`` beside any merged status."""
        built = [self._row(index, status, score, reason) for index, (status, score, reason) in enumerate(rows, start=1)]
        output = self._wave(built, wave, 2)
        quota = any(reason == "quota-exhausted" for _, _, reason in rows)
        assert output["confidence"] is None
        assert output["confidence_null_reason"] == ("quota-exhausted" if quota else "incomplete")

    def test_a_partition_of_one_wave_keeps_its_own_score(self) -> None:
        """With no other wave the one wave is the whole partition, so it is an ordinary split."""
        output = self._wave(
            [
                self._row(1, "complete", 0.9, None, gaps=["(-0.10) tests not run"]),
                self._row(2, "complete", 0.8, None, gaps=["(-0.20) one file unread"]),
            ],
            1,
            1,
        )
        bridge_call.validate_confidence_fields(output, merged=True)
        assert (output["confidence"], output["confidence_null_reason"]) == (0.8, None)

    @pytest.mark.parametrize(
        ("stdout", "error", "code"),
        [
            pytest.param("", None, "empty-result", id="no-envelope"),
            pytest.param(json.dumps({"status": "error", "error": "refused"}), None, "invalid-request", id="refused"),
            pytest.param(None, OSError("cannot launch"), "peer-unavailable", id="not-run"),
        ],
    )
    def test_helper_filled_rows_name_their_cause(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        stdout: str | None,
        error: Exception | None,
        code: str,
    ) -> None:
        """A scope row the helper fills itself carries the bridge code for why no envelope was read."""

        def _supervise(command: list[str], budget: float) -> tuple[int, str]:
            """Return the canned child output or raise the canned launch error."""
            if error is not None:
                raise error
            return 2, stdout or ""

        monkeypatch.setattr(bridge_split, "_supervise", _supervise)
        monkeypatch.setattr(bridge_split, "_scratch_path", lambda: tmp_path / "scope.txt")
        options = bridge_split._parser().parse_args(["--plan-file", str(tmp_path / "plan.json")])
        options.workspace = tmp_path
        options.run_id = "split-fixed"
        row = bridge_split._run_scope(1, "Review src/app.py.", options)
        assert (row["confidence"], row["confidence_null_reason"]) == (None, code)
