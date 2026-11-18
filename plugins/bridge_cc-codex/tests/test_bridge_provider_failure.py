"""Provider-failure classification, terminal-versus-advisory parsing, attempt count and write-delta behavior.

Fixtures copy the real ``codex exec --json`` record shapes seen in bridge transcripts (message-only errors, a nested
JSON message with an inner code, reconnect notices) and vary only the message text, so the parser is exercised on the
wire shape rather than on an invented structured one.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

BIN_ROOT = Path(__file__).resolve().parents[1] / "bin"
if str(BIN_ROOT) not in sys.path:
    sys.path.insert(0, str(BIN_ROOT))

import bridge_call  # noqa: E402  (loaded from the installed-plugin-equivalent bin directory)

_skip_git_unavailable = pytest.mark.skipif(shutil.which("git") is None, reason="git executable not available")

CAPACITY = "Selected model is at capacity. Please try a different model."
CONTENT_FLAG = (
    "This content was flagged for possible cybersecurity risk. If this seems wrong, try rephrasing your request."
)
NESTED_EFFORT = json.dumps(
    {
        "type": "error",
        "error": {
            "message": "Unsupported value: 'minimal' is not supported with the 'gpt-6.2-sol' model.",
            "type": "invalid_request_error",
            "param": "reasoning.effort",
            "code": "unsupported_value",
        },
        "status": 400,
    }
)


def _nested(message: str, *, code: str, kind: str = "invalid_request_error", status: int = 429) -> str:
    """Wrap a provider message in the JSON document Codex embeds in a failed turn's message."""
    return json.dumps({"type": "error", "error": {"message": message, "type": kind, "code": code}, "status": status})


def _request(tmp_path: Path, **overrides: Any) -> bridge_call.Request:
    """Build one small deterministic request."""
    values: dict[str, Any] = {
        "verb": "advise",
        "task": "Report completion.",
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
    """Return a valid peer result fixture."""
    core: dict[str, object] = {
        "status": status,
        "verdict": "Finished deterministically.",
        "findings": [],
        "files_touched": [],
        "remaining": [],
        "blockers": [],
        "details": [],
    }
    core.update(overrides)
    return core


def _failed(message: str) -> list[dict[str, Any]]:
    """Return the two real records a failed Codex turn emits: a top-level error, then ``turn.failed``."""
    return [{"type": "error", "message": message}, {"type": "turn.failed", "error": {"message": message}}]


def _answer(core: dict[str, object]) -> dict[str, Any]:
    """Return the item record that carries the model's final structured answer."""
    return {"type": "item.completed", "item": {"text": json.dumps(core)}}


COMPLETED = {"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 2}}


def _stdout(records: list[dict[str, Any]]) -> str:
    """Render records as the JSONL stream a Codex child writes."""
    return "\n".join(json.dumps(record) for record in [{"type": "thread.started", "thread_id": "t-1"}, *records])


def _child(records: list[dict[str, Any]], *, returncode: int = 1, timed_out: bool = False) -> bridge_call.ChildOutcome:
    """Return one fake Codex child result."""
    return bridge_call.ChildOutcome(_stdout(records), "", returncode, timed_out, None)


def _install_children(monkeypatch: pytest.MonkeyPatch, *outcomes: bridge_call.ChildOutcome) -> list[tuple[Any, ...]]:
    """Replace the child runner with a queue; the last outcome repeats.

    Returns the recorded calls.
    """
    calls: list[tuple[Any, ...]] = []

    def _fake(*args: Any) -> bridge_call.ChildOutcome:
        """Record the call and return the next queued outcome."""
        calls.append(args)
        return outcomes[min(len(calls), len(outcomes)) - 1]

    monkeypatch.setattr(bridge_call, "_run_child", _fake)
    return calls


def _incident(tmp_path: Path, result: dict[str, Any]) -> dict[str, Any]:
    """Read the incident record an envelope links to."""
    return json.loads((tmp_path / result["incident"]).read_text(encoding="utf-8"))


class TestClassifyFailure:
    """The ordered classifier reads only the terminal provider failure."""

    @pytest.mark.parametrize(
        ("failure", "expected"),
        [
            pytest.param(
                bridge_call.ProviderFailure("You're out of credits. Add more to continue."),
                bridge_call.FailureClass.QUOTA,
                id="credits-wording",
            ),
            pytest.param(
                bridge_call.ProviderFailure("Usage limit reached. Try again later."),
                bridge_call.FailureClass.QUOTA,
                id="usage-limit-wording",
            ),
            pytest.param(
                bridge_call.ProviderFailure("Request blocked", inner_code="workspace_owner_credits_depleted"),
                bridge_call.FailureClass.QUOTA,
                id="inner-code-without-wording",
            ),
            pytest.param(
                bridge_call.ProviderFailure("HTTP 429: Spend limit reached", status=429),
                bridge_call.FailureClass.QUOTA,
                id="429-with-spend-limit",
            ),
            pytest.param(
                bridge_call.ProviderFailure("payment required", status=402),
                bridge_call.FailureClass.QUOTA,
                id="402-plain",
            ),
            pytest.param(
                bridge_call.ProviderFailure("slow down", status=429), bridge_call.FailureClass.RATE, id="429-plain"
            ),
            pytest.param(
                bridge_call.ProviderFailure("Rate limit exceeded for requests"),
                bridge_call.FailureClass.RATE,
                id="rate-wording-without-status",
            ),
            pytest.param(
                bridge_call.ProviderFailure(CAPACITY), bridge_call.FailureClass.CAPACITY, id="real-capacity-message"
            ),
            pytest.param(
                bridge_call.ProviderFailure(CAPACITY, status=429), None, id="429-with-capacity-is-contradictory"
            ),
            pytest.param(
                bridge_call.ProviderFailure(CAPACITY, status=402), None, id="402-with-capacity-is-contradictory"
            ),
            pytest.param(
                bridge_call.ProviderFailure("Rate limit exceeded", status=402),
                None,
                id="402-with-rate-is-contradictory",
            ),
            pytest.param(
                bridge_call.ProviderFailure("Request refused", inner_code="rate_limit_reached"),
                bridge_call.FailureClass.RATE,
                id="rate-from-inner-code-only",
            ),
            pytest.param(
                bridge_call.ProviderFailure("Request refused", inner_type="insufficient_quota"),
                bridge_call.FailureClass.QUOTA,
                id="quota-from-inner-type-only",
            ),
            pytest.param(
                bridge_call.ProviderFailure("Your credit balance is too low to access the Anthropic API."),
                bridge_call.FailureClass.QUOTA,
                id="claude-credit-balance",
            ),
            pytest.param(
                bridge_call.ProviderFailure("You're out of extra usage"),
                bridge_call.FailureClass.QUOTA,
                id="claude-extra-usage",
            ),
            pytest.param(
                bridge_call.ProviderFailure("Insufficient credits to complete this request."),
                bridge_call.FailureClass.QUOTA,
                id="insufficient-credits",
            ),
            pytest.param(bridge_call.ProviderFailure("Could not compute an accurate limit"), None, id="accurate-limit"),
            pytest.param(bridge_call.ProviderFailure("Failed to generate limits file"), None, id="generate-limits"),
            pytest.param(bridge_call.ProviderFailure(CONTENT_FLAG), None, id="real-content-flag"),
            pytest.param(bridge_call.ProviderFailure("child process crashed"), None, id="unknown-failure"),
            pytest.param(None, None, id="no-failure"),
        ],
    )
    def test_maps_failure_evidence_to_one_class(
        self, failure: bridge_call.ProviderFailure | None, expected: bridge_call.FailureClass | None
    ) -> None:
        """Quota, rate and capacity are named from structured evidence; conflicts and unknowns stay generic.

        Capacity is a separate class from quota so a transient model stop is never reported as an account problem.
        """
        assert bridge_call.classify_failure(failure) == expected

    def test_retry_after_scan_is_bounded_on_a_pathological_message(self) -> None:
        """A very long provider message cannot make the wait scan slow: the scan is capped and linear."""
        failure = bridge_call.ProviderFailure("rate limit " + "retry" * 20000, status=429)

        line = bridge_call.failure_line(failure, bridge_call.FailureClass.RATE, "claude_to_codex")

        assert line.startswith("Codex rate limited: ")

    def test_failure_line_names_peer_by_direction_and_bounds_the_message(self) -> None:
        """The harness-authored line names the peer for the direction and never copies an unbounded provider text."""
        failure = bridge_call.ProviderFailure("Usage limit reached " + "x" * 1000)

        forward = bridge_call.failure_line(failure, bridge_call.FailureClass.QUOTA, "claude_to_codex")
        reverse = bridge_call.failure_line(failure, bridge_call.FailureClass.QUOTA, "codex_to_claude")

        assert forward.startswith("Codex quota exhausted: Usage limit reached")
        assert reverse.startswith("Claude quota exhausted: ")
        assert len(forward) <= len("Codex quota exhausted: ") + 300

    @pytest.mark.parametrize(
        ("message", "suffix"),
        [
            pytest.param("Rate limit hit. Retry after 30 seconds.", " (retry after 30s)", id="seconds"),
            pytest.param("Rate limit hit. Try again in 2 minutes.", " (retry after 120s)", id="minutes"),
            pytest.param("Rate limit hit. Try again in 6.5 seconds.", " (retry after 7s)", id="decimal-rounds-up"),
            pytest.param("Rate limit hit. Retry after 2 hours.", " (retry after 7200s)", id="hours"),
            pytest.param("Rate limit hit.", "", id="absent"),
        ],
    )
    def test_rate_line_carries_retry_after_only_when_present(self, message: str, suffix: str) -> None:
        """A wait time appears in the rate line only when the provider stated one."""
        line = bridge_call.failure_line(
            bridge_call.ProviderFailure(message, status=429), bridge_call.FailureClass.RATE, "claude_to_codex"
        )

        assert line.startswith(f"Codex rate limited{suffix}: ")


class TestParseCodexOutput:
    """Terminal failure is the last ``turn.failed`` without a later ``turn.completed``; notices are context."""

    def test_message_only_turn_failure_is_terminal(self) -> None:
        """The real capacity shape yields one terminal failure and no advisory."""
        parsed = bridge_call._parse_output(_stdout(_failed(CAPACITY)), "codex")

        assert parsed.failure == bridge_call.ProviderFailure(CAPACITY)
        assert parsed.error == CAPACITY

    def test_nested_json_message_exposes_inner_code_type_and_status(self) -> None:
        """A JSON document inside the message is read one level deep for its code, type and outer status."""
        parsed = bridge_call._parse_output(_stdout(_failed(NESTED_EFFORT)), "codex")

        assert parsed.failure is not None
        assert (parsed.failure.inner_code, parsed.failure.inner_type, parsed.failure.status) == (
            "unsupported_value",
            "invalid_request_error",
            400,
        )
        assert bridge_call._is_effort_failure(parsed.failure.message)
        assert bridge_call.classify_failure(parsed.failure) is None

    def test_notice_before_completed_turn_is_not_a_failure(self) -> None:
        """A reconnect notice followed by a completed turn leaves a complete result with the notice as context."""
        records = [{"type": "error", "message": "Reconnecting... 2/5 (request timed out)"}, _answer(_core()), COMPLETED]

        parsed = bridge_call._parse_output(_stdout(records), "codex")

        assert parsed.failure is None
        assert parsed.advisory == "Reconnecting... 2/5 (request timed out)"

    def test_failed_turn_followed_by_completed_turn_is_not_terminal(self) -> None:
        """A turn that fails and then completes recovered, so the earlier failure is not final."""
        parsed = bridge_call._parse_output(_stdout([*_failed(CAPACITY), _answer(_core()), COMPLETED]), "codex")

        assert parsed.failure is None

    def test_item_typed_error_is_neither_terminal_nor_advisory(self) -> None:
        """The project-doc warning item is progress noise, not a provider failure."""
        warning = {"type": "item.completed", "item": {"type": "error", "message": "Project doc exceeds size limit"}}

        parsed = bridge_call._parse_output(_stdout([warning]), "codex")

        assert parsed.failure is None
        assert parsed.advisory is None

    def test_file_change_items_report_touched_paths_once_in_order(self) -> None:
        """Paths from ``file_change`` items are kept in first-seen order, deduplicated, from started and completed."""
        item = {"id": "i", "type": "file_change", "changes": [{"path": "/w/a.py", "kind": "add"}]}
        later = {"id": "j", "type": "file_change", "changes": [{"path": "/w/b.py", "kind": "update"}]}
        records = [
            {"type": "item.started", "item": item},
            {"type": "item.completed", "item": item},
            {"type": "item.completed", "item": later},
        ]

        parsed = bridge_call._parse_output(_stdout(records), "codex")

        assert parsed.touched == ("/w/a.py", "/w/b.py")

    def test_claude_failure_reads_the_structured_status_integer(self) -> None:
        """The Claude side takes ``api_error_status`` as an integer, not the rendered ``HTTP`` text."""
        record = {"is_error": True, "terminal_reason": "api_error", "api_error_status": 429, "result": "Slow down"}

        parsed = bridge_call._parse_output(json.dumps(record), "claude")

        assert parsed.failure is not None
        assert parsed.failure.status == 429
        assert bridge_call.classify_failure(parsed.failure) is bridge_call.FailureClass.RATE

    def test_failed_turn_without_a_message_is_still_terminal(self) -> None:
        """A ``turn.failed`` with no readable message still ends the turn and replaces an earlier classification."""
        records = [*_failed("You're out of credits."), {"type": "turn.failed", "error": {}}]

        parsed = bridge_call._parse_output(_stdout(records), "codex")

        assert parsed.failure is not None
        assert bridge_call.classify_failure(parsed.failure) is None

    def test_claude_record_with_a_valid_answer_ignores_stray_error_keys(self) -> None:
        """A non-error Claude record whose answer is valid keeps the answer, whatever extra keys it carries."""
        record = {**_core(), "message": "You're out of credits."}

        parsed = bridge_call._parse_output(json.dumps(record), "claude")

        assert parsed.failure is None
        assert parsed.core is not None


class TestClassifiedStopDispatch:
    """A classified provider stop is reported plainly after exactly one child attempt."""

    @pytest.mark.parametrize(
        ("message", "fault", "line_prefix"),
        [
            pytest.param(
                "You're out of credits.", "quota-exhausted", "Codex quota exhausted: ", id="quota-message-only"
            ),
            pytest.param(
                _nested("Usage limit reached", code="workspace_member_usage_limit_reached"),
                "quota-exhausted",
                "Codex quota exhausted: ",
                id="quota-nested-inner-code",
            ),
            pytest.param(
                "Rate limit reached for requests.", "rate-limited", "Codex rate limited", id="rate-message-only"
            ),
            pytest.param(CAPACITY, "capacity", "Codex model at capacity: ", id="real-capacity-transcript"),
            pytest.param(
                "Usage limit reached (reasoning.effort unsupported_value)",
                "quota-exhausted",
                "Codex quota exhausted: ",
                id="effort-marker-quoted-inside-quota-message",
            ),
        ],
    )
    def test_runs_one_attempt_and_names_the_cause(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, message: str, fault: str, line_prefix: str
    ) -> None:
        """Each classified stop spends one child call, gets the class as incident fault, and a harness-authored line.

        The effort-marker case proves classification outranks the unsupported-effort retry, so an exhaustion message
        that merely quotes an effort field name cannot trigger a second billed call.
        """
        calls = _install_children(monkeypatch, _child(_failed(message), returncode=1))

        result = bridge_call.run_request(_request(tmp_path), host="codex")

        assert len(calls) == 1
        assert result["status"] == "blocked"
        assert result["verdict"].startswith(line_prefix)
        assert result["blockers"] == [result["verdict"]]
        assert _incident(tmp_path, result)["fault"] == fault
        assert _incident(tmp_path, result)["returncode"] == 1

    def test_content_flag_stays_generic_blocked_with_the_provider_text(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A content-policy stop is not an account problem and keeps the provider's own wording and fault."""
        _install_children(monkeypatch, _child(_failed(CONTENT_FLAG)))

        result = bridge_call.run_request(_request(tmp_path), host="codex")

        assert result["verdict"] == CONTENT_FLAG
        assert _incident(tmp_path, result)["fault"] == "blocked"

    def test_real_nested_effort_failure_still_retries_once_at_lower_effort(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The unsupported-effort recovery is unchanged: one retry, then the retry's result."""
        calls = _install_children(
            monkeypatch, _child(_failed(NESTED_EFFORT)), _child([_answer(_core()), COMPLETED], returncode=0)
        )

        result = bridge_call.run_request(_request(tmp_path, effort="high"), host="codex")

        assert len(calls) == 2
        assert result["status"] == "complete"

    def test_unknown_failure_stays_generic_blocked(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """An unrecognized provider message keeps today's plain blocked result with its own text."""
        calls = _install_children(monkeypatch, _child(_failed("backend exploded")))

        result = bridge_call.run_request(_request(tmp_path), host="codex")

        assert len(calls) == 1
        assert result["verdict"] == "backend exploded"
        assert _incident(tmp_path, result)["fault"] == "blocked"

    @pytest.mark.parametrize(
        ("message", "fault", "line"),
        [
            pytest.param(
                "You're out of credits.",
                "quota-exhausted",
                "Codex quota exhausted: You're out of credits.",
                id="quota",
            ),
            pytest.param(
                "Rate limit reached for requests.",
                "rate-limited",
                "Codex rate limited: Rate limit reached for requests.",
                id="rate",
            ),
            pytest.param(CAPACITY, "capacity", f"Codex model at capacity: {CAPACITY}", id="capacity"),
        ],
    )
    def test_timeout_with_terminal_provider_stop_is_not_retried(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, message: str, fault: str, line: str
    ) -> None:
        """A hard cutoff after a terminal provider stop keeps status ``timeout`` but skips the lower-effort retry.

        The incident fault names the class and the classified line rides in ``blockers`` as context; the guard covers
        every class, so a regression narrowing it to quota would spend a second billed attempt on rate or capacity.
        """
        calls = _install_children(monkeypatch, _child(_failed(message), returncode=None, timed_out=True))

        result = bridge_call.run_request(_request(tmp_path, effort="high"), host="codex")

        assert len(calls) == 1
        assert result["status"] == "timeout"
        assert result["verdict"] == "hard cutoff reached"
        assert result["blockers"] == ["hard cutoff reached", line]
        assert _incident(tmp_path, result)["fault"] == fault

    def test_unclassified_cutoff_keeps_the_notice_as_blocker_context(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cutoff after a reconnect notice keeps the notice as context beside ``hard cutoff reached``."""
        notice = [{"type": "error", "message": "Reconnecting... 5/5 (request timed out)"}]
        _install_children(monkeypatch, _child(notice, returncode=None, timed_out=True))

        result = bridge_call.run_request(_request(tmp_path, effort="high"), host="codex")

        assert result["blockers"] == [
            "hard cutoff reached",
            "provider notice: Reconnecting... 5/5 (request timed out)",
        ]

    def test_notice_then_cutoff_stays_an_ordinary_timeout_with_its_retry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A reconnect notice is not a terminal failure, so the cutoff keeps its generic fault and one retry."""
        notice = [{"type": "error", "message": "Reconnecting... 2/5 (request timed out)"}]
        calls = _install_children(monkeypatch, _child(notice, returncode=None, timed_out=True))

        result = bridge_call.run_request(_request(tmp_path, effort="high"), host="codex")

        assert len(calls) == 2
        assert result["status"] == "timeout"
        assert _incident(tmp_path, result)["fault"] == "timeout"

    def test_claude_rate_limit_and_capacity_conflict_are_classified_from_the_structured_status(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reverse direction names Claude as the peer and treats a 429 with capacity wording as unclassified."""
        plain = {"is_error": True, "terminal_reason": "api_error", "api_error_status": 429, "result": "Slow down"}
        conflict = {**plain, "result": "Model is at capacity"}
        outcomes = iter([plain, conflict])
        monkeypatch.setattr(
            bridge_call,
            "_run_child",
            lambda *args: bridge_call.ChildOutcome(json.dumps(next(outcomes)), "", 1, False, None),
        )

        first = bridge_call.run_request(_request(tmp_path, direction="codex_to_claude"), host="claude")
        second = bridge_call.run_request(_request(tmp_path, direction="codex_to_claude"), host="claude")

        assert first["verdict"].startswith("Claude rate limited: ")
        assert _incident(tmp_path, first)["fault"] == "rate-limited"
        assert _incident(tmp_path, second)["fault"] == "blocked"


class TestTerminalVersusAdvisory:
    """Only a terminal failure decides the verdict; a notice is context and never a verdict."""

    def test_notice_then_unrelated_nonzero_exit_reports_the_exit_with_the_notice_as_context(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stale reconnect notice must not become the reason a later, unrelated crash is blamed on."""
        notice = [{"type": "error", "message": "Reconnecting... 2/5 (request timed out)"}]
        _install_children(monkeypatch, _child(notice, returncode=2))

        result = bridge_call.run_request(_request(tmp_path), host="codex")

        assert result["verdict"] == "child exited with code 2"
        assert result["blockers"] == [
            "child exited with code 2",
            "provider notice: Reconnecting... 2/5 (request timed out)",
        ]

    def test_notice_then_completed_turn_stays_complete(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A notice followed by a completed turn is a normal complete result."""
        notice = {"type": "error", "message": "Reconnecting... 1/5"}
        _install_children(monkeypatch, _child([notice, _answer(_core()), COMPLETED], returncode=0))

        result = bridge_call.run_request(_request(tmp_path), host="codex")

        assert result["status"] == "complete"

    def test_terminal_failure_beats_an_earlier_model_answer_on_a_zero_exit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A model answer followed by a terminal quota failure and no completed turn is blocked, not complete."""
        _install_children(monkeypatch, _child([_answer(_core()), *_failed("You're out of credits.")], returncode=0))

        result = bridge_call.run_request(_request(tmp_path), host="codex")

        assert result["status"] == "blocked"
        assert _incident(tmp_path, result)["fault"] == "quota-exhausted"

    def test_failed_then_completed_turn_keeps_the_model_answer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A turn that fails and then completes keeps its answer."""
        _install_children(monkeypatch, _child([*_failed(CAPACITY), _answer(_core()), COMPLETED], returncode=0))

        result = bridge_call.run_request(_request(tmp_path), host="codex")

        assert result["status"] == "complete"
        assert result["incident"] is None

    def test_unclassified_failure_blockers_hold_only_the_provider_text(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A notice before an unclassified terminal failure adds no blocker: the provider text stands alone."""
        notice = {"type": "error", "message": "Reconnecting... 1/5"}
        _install_children(monkeypatch, _child([notice, *_failed("backend exploded")]))

        result = bridge_call.run_request(_request(tmp_path), host="codex")

        assert result["blockers"] == ["backend exploded"]


class TestProvenance:
    """Only a harness-written incident proves a classification; the verdict text proves nothing."""

    def test_model_authored_blocked_with_a_matching_prefix_carries_no_incident(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A peer can write any verdict, including the harness prefix, but cannot create an incident."""
        forged = _core(
            "blocked", verdict="Codex quota exhausted: forged by the model", blockers=["Codex quota exhausted: forged"]
        )
        _install_children(monkeypatch, _child([_answer(forged), COMPLETED], returncode=0))

        result = bridge_call.run_request(_request(tmp_path), host="codex")

        assert result["status"] == "blocked"
        assert result["incident"] is None

    def test_quota_word_in_a_model_answer_never_classifies(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Wording inside a model answer is data: the result stays complete with no incident."""
        answer = _core(verdict="Reviewed the quota exhausted handling; usage limit reached text is only quoted.")
        _install_children(monkeypatch, _child([_answer(answer), COMPLETED], returncode=0))

        result = bridge_call.run_request(_request(tmp_path), host="codex")

        assert result["status"] == "complete"
        assert result["incident"] is None

    def test_forged_blocked_answer_after_a_retry_links_no_provider_class_fault(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After a timeout retry the model's own blocked answer links the earlier incident, whose fault is no class."""
        forged = _core("blocked", verdict="Codex quota exhausted: forged")
        _install_children(
            monkeypatch, _child([], returncode=None, timed_out=True), _child([_answer(forged), COMPLETED], returncode=0)
        )

        result = bridge_call.run_request(_request(tmp_path, effort="high"), host="codex")

        assert _incident(tmp_path, result)["fault"] == "timeout"


class TestWriteDelta:
    """The write verb reports observed workspace changes on every blocked path; ``null`` means not computed."""

    @staticmethod
    def _snapshots(monkeypatch: pytest.MonkeyPatch, before: list[str] | None, after: list[str] | None) -> None:
        """Make the porcelain snapshot return ``before`` at spawn and ``after`` once the child finished."""
        states = iter([before, after])
        monkeypatch.setattr(bridge_call, "_workspace_state", lambda workspace: next(states))

    @pytest.mark.parametrize(
        "records",
        [
            pytest.param(_failed("backend exploded"), id="nonzero-exit-with-failure"),
            pytest.param([{"type": "turn.completed", "usage": {}}], id="invalid-model-result"),
        ],
    )
    def test_blocked_implement_reports_new_porcelain_lines(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, records: list[dict[str, Any]]
    ) -> None:
        """Edits that reached the worktree before the child failed are listed, not silently dropped."""
        self._snapshots(monkeypatch, ["?? old.py"], ["?? old.py", " M changed.py"])
        returncode = 1 if records[0]["type"] != "turn.completed" else 0
        _install_children(monkeypatch, _child(records, returncode=returncode))

        result = bridge_call.run_request(_request(tmp_path, verb="implement"), host="codex")

        assert _incident(tmp_path, result)["workspace_delta"] == [" M changed.py"]

    def test_zero_exit_terminal_failure_reports_the_delta(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The zero-exit provider-failure path carries the delta like the nonzero-exit path."""
        self._snapshots(monkeypatch, [], [" M changed.py"])
        _install_children(monkeypatch, _child(_failed(CAPACITY), returncode=0))

        result = bridge_call.run_request(_request(tmp_path, verb="implement"), host="codex")

        assert _incident(tmp_path, result)["workspace_delta"] == [" M changed.py"]

    def test_transcript_file_change_paths_cover_a_file_dirty_before_the_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A file already modified before the call keeps the same porcelain line, so the transcript path fills the
        gap."""
        edit = {
            "type": "item.completed",
            "item": {"id": "i", "type": "file_change", "changes": [{"path": "/w/a.py", "kind": "update"}]},
        }
        self._snapshots(monkeypatch, [" M a.py"], [" M a.py"])
        _install_children(monkeypatch, _child([edit, *_failed(CAPACITY)]))

        result = bridge_call.run_request(_request(tmp_path, verb="implement"), host="codex")

        assert _incident(tmp_path, result)["workspace_delta"] == ["file_change /w/a.py"]

    def test_file_already_listed_by_git_is_not_repeated_from_the_transcript(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A newly edited file appears once: the Git line wins over the transcript entry for the same path."""
        edit = {
            "type": "item.completed",
            "item": {
                "id": "i",
                "type": "file_change",
                "changes": [{"path": f"{tmp_path.resolve()}/a.py", "kind": "update"}],
            },
        }
        self._snapshots(monkeypatch, [], [" M a.py"])
        _install_children(monkeypatch, _child([edit, *_failed(CAPACITY)]))

        result = bridge_call.run_request(_request(tmp_path, verb="implement"), host="codex")

        assert _incident(tmp_path, result)["workspace_delta"] == [" M a.py"]

    def test_failed_snapshot_without_reported_edits_is_null_not_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When git cannot be read the delta is unknown (``null``), which must not look like "no edits"."""
        self._snapshots(monkeypatch, None, None)
        _install_children(monkeypatch, _child(_failed(CAPACITY)))

        result = bridge_call.run_request(_request(tmp_path, verb="implement"), host="codex")

        assert _incident(tmp_path, result)["workspace_delta"] is None

    def test_read_only_verb_records_no_delta(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A read-only verb never computes a delta, so the incident records ``null``."""
        _install_children(monkeypatch, _child(_failed(CAPACITY)))

        result = bridge_call.run_request(_request(tmp_path), host="codex")

        assert _incident(tmp_path, result)["workspace_delta"] is None

    @_skip_git_unavailable
    def test_real_git_repository_with_a_file_dirty_before_and_after_the_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Against a real repository the porcelain delta is empty for a re-edited dirty file; the transcript fills
        it."""
        git = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-c", "commit.gpgsign=false"]
        (tmp_path / "a.py").write_text("one\n", encoding="utf-8")
        # The bridge's own artifact directory is ignored, as it is in any repository that follows the bridge docs.
        (tmp_path / ".gitignore").write_text(".temp/\n", encoding="utf-8")
        for command in (["init", "-q"], ["add", "a.py", ".gitignore"], ["commit", "-q", "-m", "init"]):
            subprocess.run([*git, *command], cwd=tmp_path, check=True, capture_output=True)
        (tmp_path / "a.py").write_text("two\n", encoding="utf-8")
        edit = {
            "type": "item.completed",
            "item": {"id": "i", "type": "file_change", "changes": [{"path": str(tmp_path / "a.py"), "kind": "update"}]},
        }

        def _child_edits(*args: Any) -> bridge_call.ChildOutcome:
            """Edit the already-dirty file again, then fail the turn."""
            (tmp_path / "a.py").write_text("three\n", encoding="utf-8")
            return _child([edit, *_failed(CAPACITY)])

        monkeypatch.setattr(bridge_call, "_run_child", _child_edits)

        result = bridge_call.run_request(_request(tmp_path, verb="implement"), host="codex")

        assert _incident(tmp_path, result)["workspace_delta"] == ["file_change a.py"]

    def test_workspace_that_is_not_a_repository_records_a_null_delta(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A directory outside any Git repository makes ``git status`` fail, so the delta is unknown, not empty."""
        monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
        _install_children(monkeypatch, _child(_failed(CAPACITY)))

        result = bridge_call.run_request(_request(tmp_path, verb="implement"), host="codex")

        assert _incident(tmp_path, result)["workspace_delta"] is None

    @pytest.mark.parametrize(
        ("reported", "expected"),
        [
            pytest.param("{ws}/src/a.py", "file_change src/a.py", id="inside-workspace-is-relative"),
            pytest.param("/elsewhere/b.py", "file_change /elsewhere/b.py", id="outside-workspace-stays-absolute"),
            pytest.param("/elsewhere/b.py\nx", "file_change /elsewhere/b.py?x", id="control-characters-are-replaced"),
        ],
    )
    def test_reported_paths_are_normalized_and_bounded(self, tmp_path: Path, reported: str, expected: str) -> None:
        """Peer-authored changed paths reach the incident only relative to the workspace and without control text."""
        line = bridge_call._reported_change(reported.format(ws=tmp_path.resolve().as_posix()), tmp_path)

        assert line == expected
