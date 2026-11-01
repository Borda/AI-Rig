"""Exercise current-turn review and remediation continuation without changing host permissions."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from _modules import load_module

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
STOP_HOOK = PLUGIN_ROOT / "hooks" / "review_stop.py"
HOOK = load_module("review_stop", STOP_HOOK)

#: Host coordinates the stop hook must accept; Windows forms are simulated, so every host runs both.
PLATFORMS = {
    "posix": {
        "root": "/plugin",
        "cwd": "/workspace",
        "item_cwd": "file:///workspace",
        "launcher": "python",
        "shell": ["/bin/bash", "-lc"],
        "prefix": "",
    },
    "windows": {
        "root": "C:/plugin",
        "cwd": "C:/workspace",
        "item_cwd": "file:///C:/workspace",
        "launcher": "python.cmd",
        "shell": ["pwsh.exe", "-Command"],
        "prefix": "& ",
    },
}
PLATFORM_CASES = [pytest.param(platform, id=name) for name, platform in PLATFORMS.items()]


def command_record(command: str, output: str = "", exit_code: int = 0) -> dict:
    """Represent an actual completed shell command in the active host turn."""
    return {
        "type": "event_msg",
        "payload": {
            "type": "item_completed",
            "thread_id": "session",
            "turn_id": "turn",
            "item": {
                "type": "CommandExecution",
                "command": ["/bin/bash", "-lc", command],
                "cwd": "file:///workspace",
                "status": "completed",
                "exit_code": exit_code,
                "aggregated_output": output,
            },
        },
    }


def create_run_record(skill: str) -> dict:
    """Represent the completed helper command that creates the first run of one workflow."""
    return command_record(
        f'"/plugin/bin/python" "/plugin/shared/create_run.py" --skill {skill}', f".reports/codex/{skill}/run-a\n"
    )


def effort_update_record(**extra: object) -> dict:
    """Represent a host reasoning-effort update, optionally carrying extra control fields."""
    return {
        "type": "response_item",
        "payload": {"type": "configuration_update", "reasoning": {"effort": "high"}, **extra},
    }


@pytest.fixture
def review_state() -> tuple[dict, list[dict]]:
    """Provide a collected review with no completion checkpoint or host stop."""
    payload = {
        "hook_event_name": "Stop",
        "session_id": "session",
        "turn_id": "turn",
        "cwd": "/workspace",
        "stop_hook_active": False,
        "last_assistant_message": "Review not complete. Required passes remain unfinished.",
    }
    records = [
        {"type": "session_meta", "payload": {"id": "session"}},
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn"}},
        create_run_record("code-review"),
    ]
    return payload, records


@pytest.mark.parametrize("gate_exit", [0, 1])
def test_unfinished_review_requests_one_authorized_continuation(review_state, gate_exit: int) -> None:
    """Passing tests and missing pytest both leave independent review work active.

    A review that collected a run but never recorded a completion checkpoint must be told once to continue, whatever the
    local test gate printed, and told that continuation is not new authorization.
    """
    payload, records = review_state
    records.append(
        command_record("python -m pytest", "2997 passed" if gate_exit == 0 else "No module named pytest", gate_exit)
    )

    response = HOOK.evaluate(payload, records, "/plugin")

    assert response["decision"] == "block"
    assert "not new authorization" in response["reason"]
    assert "first unmet checkpoint" in response["reason"]
    assert "denial" in response["reason"]


def test_remediation_before_nested_review_requires_continuation(review_state) -> None:
    """A successful checkout cannot strand an authorized joined workflow before review starts.

    The real failing turn contained the host's effort update, a created and promoted remediation run, and no nested
    review invocation. Replaying those lifecycle facts must request the next authorized action, not merely describe it.
    """
    payload, records = review_state
    payload["last_assistant_message"] = "Remediation remains incomplete; zero findings were implemented."
    records[2] = effort_update_record()
    records.extend(
        [
            command_record(
                '"/plugin/bin/python" "/plugin/shared/create_run.py" --skill code-remediate',
                ".reports/codex/code-remediate/run-a\\n",
            ),
            command_record(
                '"/plugin/bin/python" "/plugin/shared/create_run.py" --skill code-remediate '
                "--promote-pr-run .reports/codex/code-remediate/run-a",
                ".reports/codex/code-remediate/pr-7/run-001\\n",
            ),
        ]
    )

    response = HOOK.evaluate(payload, records, "/plugin")

    assert response["decision"] == "block"
    assert "remediation selection, selected fixes, verification and delivery" in response["reason"]


@pytest.mark.parametrize("skill", ["code-review", "code-remediate"])
def test_reasoning_effort_update_preserves_active_workflow(review_state, skill: str) -> None:
    """A host effort update cannot disable continuation before the first actual workflow command.

    The update lands between the turn start and the workflow's create-run command for either workflow; it is a known,
    harmless configuration change and must not hide the unfinished work behind it.
    """
    payload, records = review_state
    records[-1] = create_run_record(skill)
    records.insert(2, effort_update_record())

    assert HOOK.evaluate(payload, records, "/plugin")["decision"] == "block"


@pytest.mark.parametrize(
    ("action", "exit_code", "complete"),
    [
        pytest.param("finalize --run .reports/codex/code-remediate/run-a --promote", 0, True, id="promoted-parent"),
        pytest.param(
            "finalize --run .reports/codex/code-remediate/run-a --skill code-remediate --promote",
            0,
            True,
            id="explicit-remediation-parent",
        ),
        pytest.param("finalize --run .reports/codex/code-remediate/run-a --promote", 1, False, id="failed-promotion"),
        pytest.param("finalize --run .reports/codex/code-remediate/run-a", 0, False, id="candidate-only"),
        pytest.param("preflight --run .reports/codex/code-remediate/run-a", 0, False, id="preflight-only"),
        pytest.param("finalize --run .reports/codex/code-remediate/run-b --promote", 0, False, id="other-run"),
        pytest.param(
            "finalize --run .reports/codex/code-remediate/run-a --skill code-review --promote",
            0,
            False,
            id="review-is-not-remediation",
        ),
    ],
)
def test_only_successful_parent_finalization_completes_remediation(
    review_state, action: str, exit_code: int, complete: bool
) -> None:
    """Nested review completion and preliminary checks never close the waiting remediation parent.

    A remediation run waits on a nested review that already validated; only a successful promoted finalization of that
    same remediation run releases the stop, never a candidate, preflight, other run or review-skill finalization.
    """
    payload, records = review_state
    records.append(create_run_record("code-remediate"))
    records.append(
        command_record(
            '"/plugin/bin/python" "/plugin/shared/find-review-report.py" --complete-run '
            '"/workspace/.reports/codex/code-review/run-a"',
            "Validated child review",
        )
    )
    records.append(
        command_record(f'"/plugin/bin/python" "/plugin/shared/remediation_finalize.py" {action}', "", exit_code)
    )

    assert ("decision" not in HOOK.evaluate(payload, records, "/plugin")) is complete


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(effort_update_record(approval="changed")["payload"], id="extra-control"),
        pytest.param({"type": "configuration_update", "reasoning": {"effort": []}}, id="malformed-effort"),
        pytest.param({"type": "configuration_update", "reasoning": {"effort": "unknown"}}, id="unknown-effort"),
    ],
)
def test_unknown_configuration_changes_preserve_stop(review_state, change: dict) -> None:
    """Recognizing a known effort update cannot hide a new permission or lifecycle change.

    Anything beyond a well-formed effort update, such as an approval change or a malformed effort, may be a user
    decision in disguise, so the hook must leave the stop alone.
    """
    payload, records = review_state
    records.append({"type": "response_item", "payload": change})

    assert HOOK.evaluate(payload, records, "/plugin") == {}


@pytest.mark.parametrize(
    ("payload_changes", "event_kinds"),
    [
        pytest.param({"stop_hook_active": True}, (), id="already-continued"),
        pytest.param({"session_id": "other"}, (), id="other-session"),
        pytest.param({"turn_id": "other"}, (), id="other-turn"),
        pytest.param({}, ("turn_aborted",), id="interrupt"),
        pytest.param({}, ("user_message",), id="late-user"),
        pytest.param({}, ("request_user_input",), id="question"),
        pytest.param({}, ("new_control_state",), id="unknown-control"),
    ],
)
@pytest.mark.parametrize("skill", ["code-review", "code-remediate"])
def test_control_states_do_not_request_continuation(
    review_state, payload_changes: dict, event_kinds: tuple[str, ...], skill: str
) -> None:
    """Neither workflow's receipt can override a host control state or identity mismatch.

    A second stop in the same continuation, a different session or turn, an interrupt, a late user message, a pending
    question or an unknown control event all mean the host, not the workflow, owns what happens next.
    """
    payload, records = review_state
    records[-1] = create_run_record(skill)
    payload.update(payload_changes)
    records.extend({"type": "event_msg", "payload": {"type": kind, "turn_id": "turn"}} for kind in event_kinds)

    assert "decision" not in HOOK.evaluate(payload, records, "/plugin")


@pytest.mark.parametrize(
    "message",
    [
        "Required download denied by the user.",
        "Waiting for your decision before continuing.",
        "The retry limit is exhausted.",
        "The PR is closed.",
        "Source identity is unverified.",
    ],
)
def test_claimed_controlling_stop_is_preserved(review_state, message: str) -> None:
    """Conservative pause recognition trades guard coverage for fewer unwanted turns.

    When the final assistant message itself says a denial, pending decision, exhausted limit, closed PR or unverified
    source blocks progress, the hook takes that at its word and does not push the turn on.
    """
    payload, records = review_state
    payload["last_assistant_message"] = message

    assert "decision" not in HOOK.evaluate(payload, records, "/plugin")


@pytest.mark.parametrize("same_run", [True, False])
def test_completion_must_name_the_active_review(review_state, same_run: bool) -> None:
    """Another review's successful completion cannot close this turn's review.

    The completion command validates one run directory; it releases the stop only when it names the run this turn
    created.
    """
    payload, records = review_state
    run = "run-a" if same_run else "run-b"
    records.append(
        command_record(
            '"/plugin/bin/python" "/plugin/shared/find-review-report.py" --complete-run '
            f'"/workspace/.reports/codex/code-review/{run}"',
            "Validated final bytes",
        )
    )

    assert ("decision" in HOOK.evaluate(payload, records, "/plugin")) is not same_run


def test_promoted_run_completion_closes_original_identity(review_state) -> None:
    """Promotion keeps one review identity rather than leaving a stale unfinished run.

    A review run promoted to its PR directory and then completed there is the same review, so completing the promoted
    path releases the stop for the originally created run.
    """
    payload, records = review_state
    records.extend(
        [
            command_record(
                '"/plugin/bin/python" "/plugin/shared/create_run.py" --skill code-review '
                '--promote-pr-run "/workspace/.reports/codex/code-review/run-a"',
                ".reports/codex/code-review/pr-7/run-001\n",
            ),
            command_record(
                '"/plugin/bin/python" "/plugin/shared/find-review-report.py" --complete-run '
                '"/workspace/.reports/codex/code-review/pr-7/run-001"',
                "Validated final bytes",
            ),
        ]
    )

    assert "decision" not in HOOK.evaluate(payload, records, "/plugin")


@pytest.mark.parametrize(
    "command",
    [
        'echo "/plugin/shared/create_run.py --skill code-review"',
        '"/plugin/bin/python" "/plugin/shared/create_run.py" --skill code-review; echo extra',
        '"/plugin/bin/python" "/plugin/shared/create_run.py" --skill code-review && true',
        '"/other/bin/python" "/other/shared/create_run.py" --skill code-review',
        '"bin/python" "shared/create_run.py" --skill code-review',
    ],
)
def test_mentions_compound_commands_and_other_plugins_do_not_activate(review_state, command: str) -> None:
    """Only the installed helper's direct executed command can bind a review.

    An echoed path, a command chained with another, another plugin's helper and a relative launcher all mention the
    create-run helper without being its execution, so none of them may start a review the hook then forces on.
    """
    payload, records = review_state
    records[-1]["payload"]["item"]["command"][-1] = command

    assert "decision" not in HOOK.evaluate(payload, records, "/plugin")


@pytest.mark.parametrize("skill", ["code-review", "code-remediate"])
@pytest.mark.parametrize("quote", ['"', "'"])
def test_windows_execution_is_bound_without_host_path_conversion(review_state, skill: str, quote: str) -> None:
    """Both workflows' native Windows coordinates stay meaningful on every test host.

    A PowerShell call with backslash paths and either quote style must bind the run exactly as the POSIX form does,
    without converting through the host's own path class.
    """
    payload, records = review_state
    payload["cwd"] = "C:\\workspace"
    item = records[-1]["payload"]["item"]
    item["cwd"] = "file:///C:/workspace"
    item["command"] = [
        "pwsh.exe",
        "-Command",
        f"& {quote}C:\\plugin\\bin\\python.cmd{quote} {quote}C:\\plugin\\shared\\create_run.py{quote} --skill {skill}",
    ]
    item["aggregated_output"] = f".reports\\codex\\{skill}\\run-a\n"

    assert HOOK.evaluate(payload, records, "C:\\plugin")["decision"] == "block"


@pytest.mark.parametrize("wording", ["Error: denied by user", "Error: rejected by the user", "approval denied"])
def test_recorded_tool_denial_preserves_stop_without_summary_mention(review_state, wording: str) -> None:
    """Tool denial evidence wins even when the final assistant message omits it.

    A denied tool call recorded in the transcript stops the push to continue, however the final message words the turn's
    outcome.
    """
    payload, records = review_state
    records.append({"type": "response_item", "payload": {"type": "function_call_output", "output": wording}})

    assert "decision" not in HOOK.evaluate(payload, records, "/plugin")


def test_unknown_outer_lifecycle_preserves_stop(review_state) -> None:
    """A future outer event envelope cannot silently hide a controlling stop.

    The hook only understands the envelope types it knows; an unfamiliar one carrying an abort must be treated as a
    possible stop rather than skipped.
    """
    payload, records = review_state
    records.append({"type": "new_lifecycle", "payload": {"type": "turn_aborted"}})

    assert "decision" not in HOOK.evaluate(payload, records, "/plugin")


@pytest.mark.parametrize(
    ("trigger_turn", "continues"),
    [
        pytest.param(False, True, id="non-triggering-receipt"),
        pytest.param(True, False, id="triggering-receipt"),
        pytest.param(0, False, id="integer-zero-is-not-false"),
        pytest.param(0.0, False, id="float-zero-is-not-false"),
    ],
)
def test_agent_mail_trigger_flag_controls_continuation(review_state, trigger_turn: object, continues: bool) -> None:
    """Only a boolean false mail receipt leaves the unfinished review active; other values preserve the stop.

    A non-triggering receipt accompanies completed specialist messages and must not hide the unfinished review. A
    triggering receipt, or unknown numeric metadata that merely equals False in Python, hands control back to the host.
    """
    payload, records = review_state
    records.append({"type": "inter_agent_communication_metadata", "payload": {"trigger_turn": trigger_turn}})

    assert ("decision" in HOOK.evaluate(payload, records, "/plugin")) is continues


def test_command_from_another_cwd_does_not_bind_review(review_state) -> None:
    """A relative report output is meaningful only in its actual command directory.

    The create-run output is a relative path, so a command run in a different working directory names a different run
    and must not bind to this turn's workspace.
    """
    payload, records = review_state
    records[-1]["payload"]["item"]["cwd"] = "file:///other-workspace"

    assert "decision" not in HOOK.evaluate(payload, records, "/plugin")


def test_cli_missing_transcript_returns_valid_nonblocking_json(tmp_path: Path) -> None:
    """Absent host evidence must not turn a hook into an execution blocker.

    The hook is run as the host runs it, as a script reading its payload from standard input; a transcript path that
    does not exist must still produce a parseable, non-blocking response and a zero exit status.
    """
    result = subprocess.run(
        [sys.executable, str(STOP_HOOK)],
        input=json.dumps({"hook_event_name": "Stop", "transcript_path": str(tmp_path / "missing")}),
        text=True,
        capture_output=True,
        check=True,
    )

    assert "decision" not in json.loads(result.stdout)


@dataclass
class CliReview:
    """A review turn replayed through the hook's command line, with its small-transcript baseline response."""

    transcript: Path
    encoded: list[bytes]
    invoke: Callable[[], dict]
    expected: dict

    def write_padded(self, head_count: int) -> None:
        """Write over 32 MiB of inert records between the first `head_count` turn records and the rest."""
        inert = (
            json.dumps({"type": "response_item", "payload": {"type": "message", "content": "x" * 1024}}).encode()
            + b"\n"
        )
        padding = inert * ((32 * 1024 * 1024 // len(inert)) + 2)
        self.transcript.write_bytes(b"".join(self.encoded[:head_count]) + padding + b"".join(self.encoded[head_count:]))
        assert self.transcript.stat().st_size > 32 * 1024 * 1024


@pytest.fixture
def cli_review(tmp_path: Path, review_state) -> Callable[[dict[str, object]], CliReview]:
    """Build a factory replaying the unfinished review through the hook CLI on the chosen simulated platform."""

    def build(platform: dict[str, object]) -> CliReview:
        """Write a small oversized-context transcript for one platform and record the hook's baseline response."""
        payload, records = review_state
        root = str(platform["root"])
        payload["cwd"] = platform["cwd"]
        records[0]["payload"]["context"] = "x" * 65_537
        records[-1] = command_record(
            f'"{root}/bin/{platform["launcher"]}" "{root}/shared/create_run.py" --skill code-review',
            ".reports/codex/code-review/run-a\n",
        )
        item = records[-1]["payload"]["item"]
        item["command"] = [*platform["shell"], str(platform["prefix"]) + item["command"][2]]
        item["cwd"] = platform["item_cwd"]
        transcript = tmp_path / "transcript.jsonl"
        payload["transcript_path"] = str(transcript)
        environment = {**os.environ, "PLUGIN_ROOT": root}

        def invoke() -> dict:
            """Run the hook script with the replayed Stop payload and parse its JSON response."""
            result = subprocess.run(
                [sys.executable, str(STOP_HOOK)],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                check=True,
                env=environment,
            )
            return json.loads(result.stdout)

        encoded = [json.dumps(record).encode() + b"\n" for record in records]
        transcript.write_bytes(b"".join(encoded))
        return CliReview(transcript, encoded, invoke, invoke())

    return build


@pytest.mark.parametrize("platform", PLATFORM_CASES)
def test_cli_blocks_unfinished_review_in_small_transcript(cli_review, platform: dict[str, object]) -> None:
    """Request continuation through the real hook script when the whole transcript is available.

    This is the baseline for the bounded-tail cases below, on POSIX and simulated Windows coordinates.
    """
    assert cli_review(platform).expected["decision"] == "block"


@pytest.mark.parametrize(
    ("head_count", "keeps_turn"),
    [
        pytest.param(1, True, id="turn-in-tail"),
        pytest.param(2, False, id="turn-start-beyond-tail"),
    ],
)
@pytest.mark.parametrize("platform", PLATFORM_CASES)
def test_cli_large_transcript_requires_complete_current_turn(
    cli_review, platform: dict[str, object], head_count: int, keeps_turn: bool
) -> None:
    """A bounded tail continues only when it contains the complete current turn.

    A transcript past 32 MiB is read from its tail. When the turn's start record sits before the tail the hook cannot
    prove the turn is the current one and must not push; when the whole turn is in the tail the answer is unchanged.
    """
    review = cli_review(platform)
    review.write_padded(head_count)

    assert review.invoke() == (review.expected if keeps_turn else {})


@pytest.mark.parametrize("platform", PLATFORM_CASES)
def test_cli_truncated_final_record_in_large_transcript_is_not_continued(
    cli_review, platform: dict[str, object]
) -> None:
    """A large transcript whose last record is cut short is incomplete evidence and is never continued.

    The turn is fully inside the tail, but the final record loses its last byte, so the hook cannot trust it.
    """
    review = cli_review(platform)
    review.write_padded(1)
    with review.transcript.open("rb+") as stream:
        stream.truncate(review.transcript.stat().st_size - 1)

    assert review.invoke() == {}
