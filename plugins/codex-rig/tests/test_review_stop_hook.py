"""Exercise current-turn review and remediation continuation without changing host permissions."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("review_stop", PLUGIN_ROOT / "hooks" / "review_stop.py")
assert SPEC
assert SPEC.loader
HOOK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HOOK)


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
        command_record(
            '"/plugin/bin/python" "/plugin/shared/create_run.py" --skill code-review',
            ".reports/codex/code-review/run-a\n",
        ),
    ]
    return payload, records


@pytest.mark.parametrize("gate_exit", [0, 1])
def test_unfinished_review_requests_one_authorized_continuation(review_state, gate_exit: int) -> None:
    """Passing tests and missing pytest both leave independent review work active."""
    payload, records = review_state
    records.append(
        command_record("python -m pytest", "2997 passed" if gate_exit == 0 else "No module named pytest", gate_exit)
    )
    response = HOOK.evaluate(payload, records, "/plugin")
    assert response["decision"] == "block"
    assert "not new authorization" in response["reason"]
    assert "first unmet checkpoint" in response["reason"]
    assert "denial" in response["reason"]


def test_remediation_before_nested_review_still_requires_continuation(review_state) -> None:
    """A successful checkout cannot strand an authorized joined workflow before review starts.

    The real failing turn contained the host's effort update, a created and promoted remediation run, and no nested
    review invocation. Replaying those lifecycle facts must request the next authorized action, not merely describe it.
    """
    payload, records = review_state
    payload["last_assistant_message"] = "Remediation remains incomplete; zero findings were implemented."
    records[2] = {"type": "response_item", "payload": {"type": "configuration_update", "reasoning": {"effort": "high"}}}
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
    """A host effort update cannot disable continuation before the first actual workflow command."""
    payload, records = review_state
    records[-1] = command_record(
        f'"/plugin/bin/python" "/plugin/shared/create_run.py" --skill {skill}',
        f".reports/codex/{skill}/run-a\n",
    )
    records.insert(
        2, {"type": "response_item", "payload": {"type": "configuration_update", "reasoning": {"effort": "high"}}}
    )
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
    """Nested review completion and preliminary checks never close the waiting remediation parent."""
    payload, records = review_state
    records.append(
        command_record(
            '"/plugin/bin/python" "/plugin/shared/create_run.py" --skill code-remediate',
            ".reports/codex/code-remediate/run-a\n",
        )
    )
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
        pytest.param(
            {"type": "configuration_update", "reasoning": {"effort": "high"}, "approval": "changed"}, id="extra-control"
        ),
        pytest.param({"type": "configuration_update", "reasoning": {"effort": []}}, id="malformed-effort"),
        pytest.param({"type": "configuration_update", "reasoning": {"effort": "unknown"}}, id="unknown-effort"),
    ],
)
def test_unknown_configuration_changes_preserve_stop(review_state, change: dict) -> None:
    """Recognizing a known effort update cannot hide a new permission or lifecycle change."""
    payload, records = review_state
    records.append({"type": "response_item", "payload": change})
    assert HOOK.evaluate(payload, records, "/plugin") == {}


@pytest.mark.parametrize(
    "change",
    ["already-continued", "other-session", "other-turn", "interrupt", "late-user", "question", "unknown-control"],
)
@pytest.mark.parametrize("skill", ["code-review", "code-remediate"])
def test_control_states_do_not_request_continuation(review_state, change: str, skill: str) -> None:
    """Neither workflow's receipt can override a host control state or identity mismatch."""
    payload, records = review_state
    records[-1] = command_record(
        f'"/plugin/bin/python" "/plugin/shared/create_run.py" --skill {skill}',
        f".reports/codex/{skill}/run-a\n",
    )
    if change == "already-continued":
        payload["stop_hook_active"] = True
    elif change == "other-session":
        payload["session_id"] = "other"
    elif change == "other-turn":
        payload["turn_id"] = "other"
    else:
        kinds = {
            "interrupt": "turn_aborted",
            "late-user": "user_message",
            "question": "request_user_input",
            "unknown-control": "new_control_state",
        }
        records.append({"type": "event_msg", "payload": {"type": kinds[change], "turn_id": "turn"}})
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
    """Conservative pause recognition trades guard coverage for fewer unwanted turns."""
    payload, records = review_state
    payload["last_assistant_message"] = message
    assert "decision" not in HOOK.evaluate(payload, records, "/plugin")


@pytest.mark.parametrize("same_run", [True, False])
def test_completion_must_name_the_active_review(review_state, same_run: bool) -> None:
    """Another review's successful completion cannot close this turn's review."""
    payload, records = review_state
    run = "run-a" if same_run else "run-b"
    records.append(
        command_record(
            f'"/plugin/bin/python" "/plugin/shared/find-review-report.py" --complete-run '
            f'"/workspace/.reports/codex/code-review/{run}"',
            "Validated final bytes",
        )
    )
    assert ("decision" in HOOK.evaluate(payload, records, "/plugin")) is not same_run


def test_promoted_run_completion_closes_original_identity(review_state) -> None:
    """Promotion keeps one review identity rather than leaving a stale unfinished run."""
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
    """Only the installed helper's direct executed command can bind a review."""
    payload, records = review_state
    records[-1]["payload"]["item"]["command"][-1] = command
    assert "decision" not in HOOK.evaluate(payload, records, "/plugin")


@pytest.mark.parametrize("skill", ["code-review", "code-remediate"])
@pytest.mark.parametrize("quote", ['"', "'"])
def test_windows_execution_is_bound_without_host_path_conversion(review_state, skill: str, quote: str) -> None:
    """Both workflows' native Windows coordinates stay meaningful on every test host."""
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
    """Tool denial evidence wins even when the final assistant message omits it."""
    payload, records = review_state
    records.append({"type": "response_item", "payload": {"type": "function_call_output", "output": wording}})
    assert "decision" not in HOOK.evaluate(payload, records, "/plugin")


def test_unknown_outer_lifecycle_preserves_stop(review_state) -> None:
    """A future outer event envelope cannot silently hide a controlling stop."""
    payload, records = review_state
    records.append({"type": "new_lifecycle", "payload": {"type": "turn_aborted"}})
    assert "decision" not in HOOK.evaluate(payload, records, "/plugin")


def test_observed_agent_mail_metadata_does_not_hide_unfinished_review(review_state) -> None:
    """A non-triggering mail receipt accompanies completed specialist messages."""
    payload, records = review_state
    records.append({"type": "inter_agent_communication_metadata", "payload": {"trigger_turn": False}})
    assert HOOK.evaluate(payload, records, "/plugin")["decision"] == "block"
    records[-1]["payload"]["trigger_turn"] = True
    assert "decision" not in HOOK.evaluate(payload, records, "/plugin")


@pytest.mark.parametrize("value", [0, 0.0])
def test_numeric_mail_state_is_not_boolean_false(review_state, value: int | float) -> None:
    """Unknown numeric metadata must not inherit Python's equality with False."""
    payload, records = review_state
    records.append({"type": "inter_agent_communication_metadata", "payload": {"trigger_turn": value}})
    assert "decision" not in HOOK.evaluate(payload, records, "/plugin")


def test_command_from_another_cwd_does_not_bind_review(review_state) -> None:
    """A relative report output is meaningful only in its actual command directory."""
    payload, records = review_state
    records[-1]["payload"]["item"]["cwd"] = "file:///other-workspace"
    assert "decision" not in HOOK.evaluate(payload, records, "/plugin")


def test_cli_missing_transcript_returns_valid_nonblocking_json(tmp_path: Path) -> None:
    """Absent host evidence must not turn a hook into an execution blocker."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, str(PLUGIN_ROOT / "hooks" / "review_stop.py")],
        input=json.dumps({"hook_event_name": "Stop", "transcript_path": str(tmp_path / "missing")}),
        text=True,
        capture_output=True,
        check=True,
    )
    assert "decision" not in json.loads(result.stdout)


@pytest.mark.parametrize("turn_in_tail", [True, False])
@pytest.mark.parametrize("windows", [False, True])
def test_cli_large_transcript_requires_complete_current_turn(
    tmp_path: Path, review_state, turn_in_tail: bool, windows: bool
) -> None:
    """A bounded tail continues only when it contains the complete current turn."""
    payload, records = review_state
    records[0]["payload"]["context"] = "x" * 65_537
    plugin_root = "C:/plugin" if windows else "/plugin"
    if windows:
        payload["cwd"] = "C:/workspace"
    launcher = "python.cmd" if windows else "python"
    records[-1] = command_record(
        f'"{plugin_root}/bin/{launcher}" "{plugin_root}/shared/create_run.py" --skill code-review',
        ".reports/codex/code-review/run-a\n",
    )
    if windows:
        item = records[-1]["payload"]["item"]
        item["command"] = ["pwsh.exe", "-Command", "& " + item["command"][2]]
        item["cwd"] = "file:///C:/workspace"
    transcript = tmp_path / "transcript.jsonl"
    payload["transcript_path"] = str(transcript)
    env = {**os.environ, "PLUGIN_ROOT": plugin_root}

    def invoke() -> dict:
        result = subprocess.run(
            [sys.executable, str(PLUGIN_ROOT / "hooks" / "review_stop.py")],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            check=True,
            env=env,
        )
        return json.loads(result.stdout)

    encoded = [json.dumps(record).encode() + b"\n" for record in records]
    transcript.write_bytes(b"".join(encoded))
    expected = invoke()
    assert expected["decision"] == "block"

    inert = (
        json.dumps({"type": "response_item", "payload": {"type": "message", "content": "x" * 1024}}).encode() + b"\n"
    )
    with transcript.open("wb") as stream:
        stream.write(encoded[0])
        if not turn_in_tail:
            stream.write(encoded[1])
        for _ in range((32 * 1024 * 1024 // len(inert)) + 2):
            stream.write(inert)
        if turn_in_tail:
            stream.write(encoded[1])
        stream.write(encoded[2])
    assert transcript.stat().st_size > 32 * 1024 * 1024
    assert invoke() == (expected if turn_in_tail else {})
    if turn_in_tail:
        with transcript.open("rb+") as stream:
            stream.truncate(transcript.stat().st_size - 1)
        assert invoke() == {}
