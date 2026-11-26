"""Exercise current-turn review and remediation continuation without changing host permissions."""

from __future__ import annotations

import hashlib
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
#: Direct POSIX launcher prefix for the packaged checkout-slot helper.
SLOT_HELPER = '"/plugin/bin/python" "/plugin/shared/review_checkout_slot.py"'
#: Slot locator the helper reports for the default POSIX workspace.
SLOT = "/workspace/.git/codex-review-slot"
#: Supported host receipts for a human steering message in the active turn.
USER_STEERING_RECORDS = [
    pytest.param(
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": "Stop"}},
        id="response-message",
    ),
    pytest.param(
        {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "thread_id": "session",
                "turn_id": "turn",
                "item": {"type": "UserMessage", "message": "Stop"},
            },
        },
        id="user-message-item",
    ),
]


def command_record(command: str, output: str = "", exit_code: int = 0, *, platform: dict | None = None) -> dict:
    """Represent an actual completed shell command in the active host turn."""
    platform = PLATFORMS["posix"] if platform is None else platform
    return {
        "type": "event_msg",
        "payload": {
            "type": "item_completed",
            "thread_id": "session",
            "turn_id": "turn",
            "item": {
                "type": "CommandExecution",
                "command": [*platform["shell"], command],
                "cwd": platform["item_cwd"],
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
    assert "--complete-run" in response["reason"]
    assert "remediat" not in response["reason"].lower()
    assert "+review" not in response["reason"]


@pytest.mark.parametrize("platform", PLATFORM_CASES)
@pytest.mark.parametrize("locator", ["checkout", "slot"])
@pytest.mark.parametrize(
    ("status", "holder", "expected"),
    [
        pytest.param("busy", "peer", "queue", id="peer-active"),
        pytest.param("inspection-required", "peer", "queue", id="peer-inspection"),
        pytest.param("inspection-required", "queued", "pause", id="own-inspection"),
        pytest.param("busy", None, "workflow", id="empty-slot"),
    ],
)
def test_checkout_admission_routes_first_receipt_by_holder(
    review_state, platform: dict, locator: str, status: str, holder: str | None, expected: str
) -> None:
    """Distinguish a peer's queue from an own inspection hold or an interrupted empty slot."""
    payload, records = review_state
    cwd = platform["cwd"]
    root = platform["root"]
    slot = f"{cwd}/.git/codex-review-slot"
    target = cwd if locator == "checkout" else slot
    command = platform["prefix"] + f'"{root}/bin/{platform["launcher"]}"'
    payload["cwd"] = cwd
    payload["last_assistant_message"] = "Review incomplete. The checkout slot remains busy."
    records[2:] = [
        command_record(
            f'{command} "{root}/shared/create_run.py" --skill code-review',
            ".reports/codex/code-review/run-a",
            platform=platform,
        ),
        command_record(
            f'{command} "{root}/shared/review_checkout_slot.py" acquire --{locator} "{target}" --owner queued --wait-seconds 60',
            json.dumps({"status": status, "owner": holder, "slot": slot}),
            exit_code=1,
            platform=platform,
        ),
    ]

    response = HOOK.evaluate(payload, records, root)

    assert response.get("decision") == (None if expected == "pause" else "block")
    assert ("--queue" in response.get("reason", "")) is (expected == "queue")
    assert ("--complete-run" in response.get("reason", "")) is (expected == "workflow")


@pytest.mark.parametrize(
    ("success_owner", "success_slot", "expected_queue"),
    [
        pytest.param("queued", "/workspace/.git/codex-review-slot", False, id="matched-acquisition"),
        pytest.param("other", "/workspace/.git/codex-review-slot", True, id="distinct-acquisition"),
        pytest.param("queued", "/other/.git/codex-review-slot", True, id="different-slot"),
    ],
)
def test_checkout_admission_clears_queue_only_after_matching_acquisition(
    review_state, success_owner: str, success_slot: str, expected_queue: bool
) -> None:
    """A successful matching acquire resumes normal continuation for the same owner and slot."""
    payload, records = review_state
    launcher = '"/plugin/bin/python" "/plugin/shared/review_checkout_slot.py"'
    slot = "/workspace/.git/codex-review-slot"
    records.extend(
        [
            command_record(
                f"{launcher} acquire --checkout /workspace --owner queued --wait-seconds 60",
                json.dumps({"status": "busy", "owner": "peer", "slot": slot}),
                1,
            ),
            command_record(
                f'{launcher} acquire --slot "{success_slot}" --owner {success_owner} --queue',
                "\n".join(
                    [
                        json.dumps({"status": "busy", "owner": "peer", "slot": success_slot}),
                        json.dumps({"status": "acquired", "owner": success_owner, "slot": success_slot}),
                    ]
                ),
            ),
        ]
    )

    response = HOOK.evaluate(payload, records, "/plugin")

    assert response["decision"] == "block"
    assert ("--queue" in response["reason"]) is expected_queue


@pytest.mark.parametrize(
    ("status", "holder", "expected"),
    [
        pytest.param("busy", None, "workflow", id="empty-slot"),
        pytest.param("busy", "queued", "workflow", id="same-owner-at-free-only-locator"),
        pytest.param("inspection-required", "queued", "pause", id="own-inspection-hold"),
    ],
)
def test_checkout_admission_discards_old_peer_after_a_matched_nonpeer_result(
    review_state, status: str, holder: str | None, expected: str
) -> None:
    """A newer terminal receipt supersedes an older peer claim for the same admission."""
    payload, records = review_state
    launcher = '"/plugin/bin/python" "/plugin/shared/review_checkout_slot.py"'
    slot = "/workspace/.git/codex-review-slot"
    records.extend(
        [
            command_record(
                f"{launcher} acquire --checkout /workspace --owner queued --wait-seconds 60",
                json.dumps({"status": "busy", "owner": "peer", "slot": slot}),
                1,
            ),
            command_record(
                f'{launcher} acquire --slot "{slot}" --owner queued --queue',
                json.dumps({"status": status, "owner": holder, "slot": slot}),
                1,
            ),
        ]
    )

    response = HOOK.evaluate(payload, records, "/plugin")

    assert response.get("decision") == (None if expected == "pause" else "block")
    assert ("--complete-run" in response.get("reason", "")) is (expected == "workflow")


def test_checkout_admission_preserves_matching_slot_error(review_state) -> None:
    """A malformed slot supersedes an earlier peer claim and requires inspection."""
    payload, records = review_state
    launcher = '"/plugin/bin/python" "/plugin/shared/review_checkout_slot.py"'
    slot = "/workspace/.git/codex-review-slot"
    records.extend(
        [
            command_record(
                f"{launcher} acquire --checkout /workspace --owner queued --wait-seconds 60",
                json.dumps({"status": "busy", "owner": "peer", "slot": slot}),
                1,
            ),
            command_record(
                f'{launcher} acquire --slot "{slot}" --owner queued --queue',
                json.dumps({"status": "error", "owner": None, "slot": slot, "error": "invalid review slot marker"}),
                2,
            ),
        ]
    )

    assert HOOK.evaluate(payload, records, "/plugin") == {}


def test_checkout_admission_ignores_an_unrelated_slot_receipt(review_state) -> None:
    """A mismatched returned locator cannot redirect the current workflow's continuation."""
    payload, records = review_state
    records.append(
        command_record(
            '"/plugin/bin/python" "/plugin/shared/review_checkout_slot.py" '
            "acquire --slot /workspace/.git/codex-review-slot --owner queued --wait-seconds 60",
            json.dumps({"status": "busy", "owner": "peer", "slot": "/other/.git/codex-review-slot"}),
            1,
        )
    )

    response = HOOK.evaluate(payload, records, "/plugin")

    assert response["decision"] == "block"
    assert "--complete-run" in response["reason"]


@pytest.mark.parametrize("platform", PLATFORM_CASES)
@pytest.mark.parametrize(
    ("later_event", "expected_decision"),
    [
        pytest.param([], "block", id="queue"),
        pytest.param([{"type": "message", "role": "user"}], None, id="user-steering"),
        pytest.param([{"type": "function_call", "name": HOOK.NATIVE_QUESTION}], None, id="pending-question"),
    ],
)
def test_checkout_admission_routes_peer_wait_before_run_creation(
    review_state, platform: dict, later_event: list[dict], expected_decision: str | None
) -> None:
    """Treat a peer receipt as admission evidence before a run exists and preserve later pauses."""
    payload, records = review_state
    cwd = platform["cwd"]
    root = platform["root"]
    launcher = platform["prefix"] + f'"{root}/bin/{platform["launcher"]}"'
    payload["cwd"] = cwd
    payload["last_assistant_message"] = "Checkout admission remains queued."
    records[2] = command_record(
        f'{launcher} "{root}/shared/review_checkout_slot.py" acquire --checkout "{cwd}" --owner queued --wait-seconds 60',
        json.dumps({"status": "busy", "owner": "peer", "slot": f"{cwd}/.git/codex-review-slot"}),
        exit_code=1,
        platform=platform,
    )
    records.extend({"type": "response_item", "payload": event} for event in later_event)

    response = HOOK.evaluate(payload, records, root)

    assert response.get("decision") == expected_decision
    bounded_retry = "acquire --slot <retained locator> --owner <original owner ID> --wait-seconds 60"
    assert (bounded_retry in response.get("reason", "")) is (expected_decision == "block")


@pytest.mark.parametrize("skill", ["code-review", "code-remediate"])
@pytest.mark.parametrize(
    ("arguments", "status", "holder", "exit_code", "expected"),
    [
        pytest.param("status --slot {slot}", "free", None, 0, "workflow", id="status-free"),
        pytest.param("status --slot {slot}", "busy", None, 0, "workflow", id="status-empty"),
        pytest.param("status --slot {slot}", "busy", "new-peer", 0, "queue", id="status-peer"),
        pytest.param("status --slot {slot}", "error", None, 2, "pause", id="status-error"),
        pytest.param("status --checkout /workspace", "busy", "queued", 0, "workflow", id="status-current-owner"),
        pytest.param("status --slot {slot}", "inspection-required", "queued", 0, "pause", id="status-own-hold"),
        pytest.param("hold --slot {slot} --owner queued", "inspection-required", "queued", 0, "pause", id="own-hold"),
        pytest.param("hold --slot {slot} --owner peer", "inspection-required", "peer", 0, "queue", id="peer-hold"),
        pytest.param("release --slot {slot} --owner peer", "released", None, 0, "workflow", id="peer-release"),
        pytest.param(
            "release --inspected --slot {slot} --owner peer", "released", None, 0, "workflow", id="inspected-release"
        ),
        pytest.param("release --slot {slot} --owner other", "released", None, 0, "queue", id="unrelated-release"),
        pytest.param("status --slot /other/.git/codex-review-slot", "free", None, 0, "queue", id="other-locator"),
        pytest.param("status --slot {slot}", "free", "peer", 0, "queue", id="invalid-status-owner"),
        pytest.param("hold --slot {slot} --owner queued", "inspection-required", "queued", 1, "queue", id="bad-exit"),
    ],
)
def test_checkout_admission_uses_latest_matching_observation(
    review_state, skill: str, arguments: str, status: str, holder: str | None, exit_code: int, expected: str
) -> None:
    """Apply only newer receipts the checkout helper could emit for the bound slot and owner."""
    payload, records = review_state
    records[2] = create_run_record(skill)
    launcher = '"/plugin/bin/python" "/plugin/shared/review_checkout_slot.py"'
    slot = "/workspace/.git/codex-review-slot"
    receipt = {"status": status, "owner": holder, "slot": slot}
    receipt.update({"error": "invalid review slot marker"} if exit_code == 2 else {})
    records.extend(
        [
            command_record(
                f"{launcher} acquire --checkout /workspace --owner queued --wait-seconds 60",
                json.dumps({"status": "busy", "owner": "peer", "slot": slot}),
                1,
            ),
            command_record(f"{launcher} {arguments.format(slot=slot)}", json.dumps(receipt), exit_code),
        ]
    )

    response = HOOK.evaluate(payload, records, "/plugin")

    assert response.get("decision") == (None if expected == "pause" else "block")
    assert ("--queue" in response.get("reason", "")) is (expected == "queue")
    continuation = "Continue remediation selection" if skill == "code-remediate" else "--complete-run"
    assert (continuation in response.get("reason", "")) is (expected == "workflow")


@pytest.mark.parametrize("platform", PLATFORM_CASES)
@pytest.mark.parametrize(
    ("initial_flag", "status_flag", "matching_target", "expected"),
    [
        pytest.param("--checkout", "--checkout", True, None, id="bound-checkout"),
        pytest.param("--checkout", "--checkout", False, "block", id="different-checkout"),
        pytest.param("--slot", "--checkout", True, "block", id="slot-only-admission"),
        pytest.param("--checkout", "--slot", True, "block", id="invalid-null-locator"),
    ],
)
def test_checkout_resolution_error_supersedes_only_a_bound_checkout(
    review_state, platform: dict, initial_flag: str, status_flag: str, matching_target: bool, expected: str | None
) -> None:
    """Use a slotless error only when an earlier receipt tied this checkout to the queue."""
    payload, records = review_state
    cwd = platform["cwd"]
    root = platform["root"]
    launcher = platform["prefix"] + f'"{root}/bin/{platform["launcher"]}" "{root}/shared/review_checkout_slot.py"'
    slot = f"{cwd}/.git/codex-review-slot"
    initial_target = cwd if initial_flag == "--checkout" else slot
    status_target = (cwd if matching_target else f"{cwd}/unrelated") if status_flag == "--checkout" else slot
    payload["cwd"] = cwd
    records[2:] = [
        command_record(
            f'{launcher} acquire {initial_flag} "{initial_target}" --owner queued',
            json.dumps({"status": "busy", "owner": "peer", "slot": slot}),
            1,
            platform=platform,
        ),
        command_record(
            f'{launcher} status {status_flag} "{status_target}"',
            json.dumps({"status": "error", "owner": None, "slot": None, "error": "invalid Git common directory"}),
            2,
            platform=platform,
        ),
    ]

    response = HOOK.evaluate(payload, records, root)

    assert response.get("decision") == expected
    assert ("--queue" in response.get("reason", "")) is (expected == "block")


@pytest.mark.parametrize("platform", PLATFORM_CASES)
@pytest.mark.parametrize(
    ("initial_flag", "initial_status", "holder", "initial_exit", "arguments", "status", "new_holder", "expected"),
    [
        pytest.param(
            "--checkout", "busy", "peer", 1, 'status --checkout "{cwd}"', "free", None, None, id="peer-status"
        ),
        pytest.param(
            "--checkout", "acquired", "queued", 0, 'status --checkout "{cwd}"', "free", None, None, id="acquired-status"
        ),
        pytest.param(
            "--checkout",
            "busy",
            "peer",
            1,
            'acquire --checkout "{cwd}" --owner queued',
            "acquired",
            "queued",
            None,
            id="same-requester-reacquire",
        ),
        pytest.param(
            "--checkout",
            "busy",
            "peer",
            1,
            'acquire --checkout "{cwd}" --owner other',
            "acquired",
            "other",
            "--queue",
            id="different-requester",
        ),
        pytest.param(
            "--checkout",
            "busy",
            "peer",
            1,
            'status --checkout "{cwd}/different"',
            "free",
            None,
            "--queue",
            id="different-checkout",
        ),
        pytest.param(
            "--checkout",
            "busy",
            "peer",
            1,
            'status --slot "{new_slot}"',
            "free",
            None,
            "--queue",
            id="different-locator",
        ),
        pytest.param(
            "--slot",
            "busy",
            "peer",
            1,
            'status --checkout "{cwd}"',
            "free",
            None,
            "--queue",
            id="locator-only-admission",
        ),
    ],
)
def test_checkout_slot_drift_requires_the_bound_checkout_and_requester(
    review_state,
    platform: dict,
    initial_flag: str,
    initial_status: str,
    holder: str,
    initial_exit: int,
    arguments: str,
    status: str,
    new_holder: str | None,
    expected: str | None,
) -> None:
    """A verified re-resolution of the bound checkout invalidates its earlier slot claim."""
    payload, records = review_state
    cwd = platform["cwd"]
    root = platform["root"]
    launcher = platform["prefix"] + f'"{root}/bin/{platform["launcher"]}" "{root}/shared/review_checkout_slot.py"'
    slot = f"{cwd}/.git/codex-review-slot"
    new_slot = f"{cwd}/new-common-dir/codex-review-slot"
    initial_target = cwd if initial_flag == "--checkout" else slot
    payload["cwd"] = cwd
    records[2:] = [
        command_record(
            f'{launcher} acquire {initial_flag} "{initial_target}" --owner queued',
            json.dumps({"status": initial_status, "owner": holder, "slot": slot}),
            initial_exit,
            platform=platform,
        ),
        command_record(
            f"{launcher} {arguments.format(cwd=cwd, new_slot=new_slot)}",
            json.dumps({"status": status, "owner": new_holder, "slot": new_slot}),
            platform=platform,
        ),
    ]

    response = HOOK.evaluate(payload, records, root)

    assert response.get("decision") == ("block" if expected else None)
    assert expected is None or expected in response["reason"]


@pytest.mark.parametrize("skill", ["code-review", "code-remediate"])
@pytest.mark.parametrize("observed_action", ["status", "acquire"])
@pytest.mark.parametrize(
    ("release_first", "changed_locator", "expected"),
    [
        pytest.param(False, True, None, id="drift-before-release"),
        pytest.param(True, True, None, id="release-before-drift"),
        pytest.param(True, False, "block", id="release-and-same-locator"),
    ],
)
def test_checkout_identity_survives_claim_release(
    review_state, skill: str, observed_action: str, release_first: bool, changed_locator: bool, expected: str | None
) -> None:
    """A release ends ownership but does not erase the checkout identity used to detect drift."""
    payload, records = review_state
    records[2] = create_run_record(skill)
    launcher = '"/plugin/bin/python" "/plugin/shared/review_checkout_slot.py"'
    slot = "/workspace/.git/codex-review-slot"
    observed_slot = "/workspace/new-common-dir/codex-review-slot" if changed_locator else slot
    records.append(
        command_record(
            f"{launcher} acquire --checkout /workspace --owner queued",
            json.dumps({"status": "acquired", "owner": "queued", "slot": slot}),
        )
    )
    acquiring = observed_action == "acquire"
    observation = command_record(
        f"{launcher} {observed_action} --checkout /workspace" + (" --owner queued" if acquiring else ""),
        json.dumps(
            {
                "status": "acquired" if acquiring else "free",
                "owner": "queued" if acquiring else None,
                "slot": observed_slot,
            }
        ),
    )
    release = command_record(
        f'{launcher} release --slot "{slot}" --owner queued',
        json.dumps({"status": "released", "owner": None, "slot": slot}),
    )
    records.extend((release, observation) if release_first else (observation, release))

    response = HOOK.evaluate(payload, records, "/plugin")
    assert response.get("decision") == expected
    assert "--queue" not in response.get("reason", "")


@pytest.mark.parametrize("skill", ["code-review", "code-remediate"])
@pytest.mark.parametrize(
    ("arguments", "status", "slot", "holder", "exit_code", "expected"),
    [
        pytest.param("--checkout /workspace --owner queued", "error", None, None, 2, None, id="bound-acquire-error"),
        pytest.param("--checkout /workspace --owner other", "error", None, None, 2, "block", id="unrelated-error"),
        pytest.param(
            "--checkout /workspace --owner other",
            "busy",
            "/other/codex-review-slot",
            "peer",
            1,
            "block",
            id="different-requester",
        ),
        pytest.param(
            "--slot /other/codex-review-slot --owner queued",
            "busy",
            "/other/codex-review-slot",
            "peer",
            1,
            "block",
            id="different-slot",
        ),
    ],
)
def test_checkout_release_preserves_scope_for_later_acquisition(
    review_state,
    skill: str,
    arguments: str,
    status: str,
    slot: str | None,
    holder: str | None,
    exit_code: int,
    expected: str | None,
) -> None:
    """Stop on failed re-entry of the bound checkout without adopting another invocation's queue."""
    payload, records = review_state
    records[2] = create_run_record(skill)
    launcher = '"/plugin/bin/python" "/plugin/shared/review_checkout_slot.py"'
    original = "/workspace/.git/codex-review-slot"
    receipt = {"status": status, "slot": slot, "owner": holder}
    receipt.update({"error": "Git could not resolve the checkout"} if exit_code == 2 else {})
    records.extend(
        (
            command_record(
                f"{launcher} acquire --checkout /workspace --owner queued",
                json.dumps({"status": "acquired", "owner": "queued", "slot": original}),
            ),
            command_record(
                f"{launcher} release --slot {original} --owner queued",
                json.dumps({"status": "released", "owner": None, "slot": original}),
            ),
            command_record(f"{launcher} acquire {arguments}", json.dumps(receipt), exit_code),
        )
    )

    response = HOOK.evaluate(payload, records, "/plugin")
    assert response.get("decision") == expected
    assert "--queue" not in response.get("reason", "")


@pytest.mark.parametrize("user_record", USER_STEERING_RECORDS)
def test_checkout_release_preserves_user_steering_before_run_creation(review_state, user_record: dict) -> None:
    """Honor supported human steering receipts after release and before a new claim."""
    payload, records = review_state
    launcher = '"/plugin/bin/python" "/plugin/shared/review_checkout_slot.py"'
    slot = "/workspace/.git/codex-review-slot"
    acquire = command_record(
        f"{launcher} acquire --checkout /workspace --owner queued",
        json.dumps({"status": "acquired", "owner": "queued", "slot": slot}),
    )
    release = command_record(
        f"{launcher} release --slot {slot} --owner queued",
        json.dumps({"status": "released", "owner": None, "slot": slot}),
    )
    records[2:] = [acquire, release, user_record, acquire]

    assert HOOK.evaluate(payload, records, "/plugin") == {}


@pytest.mark.parametrize(
    ("later_records", "expected"),
    [
        pytest.param([], "before an active run", id="before-run"),
        pytest.param([create_run_record("code-review")], "--complete-run", id="review-run"),
        pytest.param([create_run_record("code-remediate")], "Continue remediation selection", id="remediation-run"),
        pytest.param([{"type": "response_item", "payload": {"type": "message", "role": "user"}}], None, id="user-stop"),
        pytest.param(
            [
                command_record(
                    '"/plugin/bin/python" "/plugin/shared/review_checkout_slot.py" '
                    "release --slot /workspace/.git/codex-review-slot --owner queued",
                    '{"status":"released","owner":null,"slot":"/workspace/.git/codex-review-slot"}',
                ),
            ],
            None,
            id="released",
        ),
        pytest.param(
            [
                create_run_record("code-review"),
                command_record(
                    '"/plugin/bin/python" "/plugin/shared/find-review-report.py" --complete-run .reports/codex/code-review/run-a'
                ),
            ],
            "release --slot",
            id="completed-review",
        ),
    ],
)
def test_checkout_acquisition_recovers_before_workflow_binding(
    review_state, later_records: list[dict], expected: str | None
) -> None:
    """A confirmed acquired checkout can recover before a run exists, then yields to its lifecycle."""
    payload, records = review_state
    records[2:] = [
        command_record(
            '"/plugin/bin/python" "/plugin/shared/review_checkout_slot.py" acquire --checkout /workspace --owner queued --queue',
            '{"status":"acquired","owner":"queued","slot":"/workspace/.git/codex-review-slot"}',
        ),
        *later_records,
    ]

    response = HOOK.evaluate(payload, records, "/plugin")

    reason = response.get("reason", "")
    assert response.get("decision") == ("block" if expected else None)
    assert expected is None or expected in reason
    assert "--queue" not in reason


@pytest.mark.parametrize(
    ("arguments", "status", "exit_code", "expected"),
    [
        pytest.param("status --slot {slot}", "busy", 0, "block", id="status-confirms-owner"),
        pytest.param("acquire --slot {slot} --owner queued", "busy", 1, "block", id="free-only-acquire-sees-owner"),
        pytest.param("hold --slot {slot} --owner queued", "inspection-required", 0, None, id="own-hold"),
    ],
)
def test_checkout_acquisition_uses_newer_owner_evidence(
    review_state, arguments: str, status: str, exit_code: int, expected: str | None
) -> None:
    """Continue with a confirmed own claim and preserve a newer inspection hold."""
    payload, records = review_state
    launcher = '"/plugin/bin/python" "/plugin/shared/review_checkout_slot.py"'
    slot = "/workspace/.git/codex-review-slot"
    records[2:] = [
        command_record(
            f"{launcher} acquire --checkout /workspace --owner queued",
            json.dumps({"status": "acquired", "owner": "queued", "slot": slot}),
        ),
        command_record(
            f"{launcher} {arguments.format(slot=slot)}",
            json.dumps({"status": status, "owner": "queued", "slot": slot}),
            exit_code,
        ),
    ]

    response = HOOK.evaluate(payload, records, "/plugin")

    assert response.get("decision") == expected
    assert ("before an active run" in response.get("reason", "")) is (expected == "block")


@pytest.mark.parametrize("platform", PLATFORM_CASES)
@pytest.mark.parametrize("proof", ["matched", "different-owner", "different-slot", "unbound-receipt", "failed"])
def test_fast_forward_restart_resumes_only_matching_inspection_hold(review_state, platform: dict, proof: str) -> None:
    """Resume a held review only on a successful direct proof for the same exclusive owner and slot."""
    payload, records = review_state
    root, cwd = platform["root"], platform["cwd"]
    payload["cwd"] = cwd
    launcher = platform["prefix"] + f'"{root}/bin/{platform["launcher"]}"'
    scheduler = f'{launcher} "{root}/shared/review_checkout_slot.py"'
    slot = f"{cwd}/.git/codex-review-slot"
    held = {"owner": "original", "slot": slot}
    claim = {"owner": "other" if proof == "different-owner" else "original", "slot": slot}
    if proof == "different-slot":
        claim["slot"] = f"{cwd}/other/.git/codex-review-slot"
    digest = "a" * 64
    proof_command = (
        f'{launcher} "{root}/shared/contributor_run_integrity.py" restart-ref-fast-forward --snapshot before.json '
        f"--expect {digest} --branch main --old-oid {'a' * 40} --new-oid {'b' * 40} --out after.json "
        f'--slot "{claim["slot"]}" --owner {claim["owner"]}'
    )
    proof_output = {"status": "restarted", "ok": True, "sha256": digest, **claim}
    if platform == PLATFORMS["windows"]:
        proof_output["slot"] = claim["slot"].replace("/", "\\")
    if proof == "unbound-receipt":
        proof_output["owner"] = "other"
    records[2:] = [
        command_record(
            f'{scheduler} acquire --checkout "{cwd}" --owner original',
            json.dumps({"status": "acquired", **held}),
            platform=platform,
        ),
        command_record(
            f'{scheduler} hold --slot "{slot}" --owner original',
            json.dumps({"status": "inspection-required", **held}),
            platform=platform,
        ),
        command_record(proof_command, json.dumps(proof_output), int(proof == "failed"), platform=platform),
    ]
    response = HOOK.evaluate(payload, records, root)
    assert response.get("decision") == ("block" if proof == "matched" else None)
    assert ("before an active run" in response.get("reason", "")) is (proof == "matched")


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        pytest.param("Waiting for the checkout slot to become free.", "block", id="checkout-slot"),
        pytest.param("Waiting for another review to release the checkout slot.", "block", id="peer-release"),
        pytest.param(
            "Waiting for another review to release the checkout slot. Waiting for your approval.",
            None,
            id="peer-and-approval",
        ),
        pytest.param("Waiting for your approval.", None, id="human-approval"),
        pytest.param("Waiting for the shared checkout slot.", "block", id="shared-checkout-slot"),
        pytest.param("Review is waiting for the other checkout owner.", "block", id="checkout-owner"),
        pytest.param("Awaiting checkout admission.", "block", id="checkout-admission"),
        pytest.param("Waiting for the checkout slot. The user paused this run.", None, id="explicit-user-pause"),
        pytest.param("Waiting for checkout admission. Your approval is pending.", None, id="approval"),
        pytest.param("Waiting for the checkout slot. The retry limit is exhausted.", None, id="retry-exhausted"),
    ],
)
def test_checkout_wait_wording_preserves_evidenced_queue_recovery(
    review_state, message: str, expected: str | None
) -> None:
    """Advisory checkout progress yields to current host evidence and preserves controlling stops."""
    payload, records = review_state
    payload["last_assistant_message"] = message
    records[2] = command_record(
        '"/plugin/bin/python" "/plugin/shared/review_checkout_slot.py" acquire --checkout /workspace --owner queued --wait-seconds 60',
        '{"status":"busy","owner":"peer","slot":"/workspace/.git/codex-review-slot"}',
        1,
    )

    response = HOOK.evaluate(payload, records, "/plugin")

    assert response.get("decision") == expected
    assert ("--queue" in response.get("reason", "")) is (expected == "block")


@pytest.mark.parametrize(
    ("arguments", "status", "holder", "exit_code"),
    [
        pytest.param("acquire --checkout /workspace --owner me --queue", "acquired", "me", 0, id="acquired"),
        pytest.param("acquire --checkout /workspace --owner me --wait-seconds 60", "busy", "peer", 1, id="peer-queue"),
    ],
)
@pytest.mark.parametrize("run_bound", [pytest.param(False, id="before-run"), pytest.param(True, id="run-bound")])
@pytest.mark.parametrize(
    ("message", "question"),
    [
        pytest.param(
            "The PR head moved.\n\nShould I review the new head or the old one?", True, id="terminal-question"
        ),
        pytest.param("The PR head moved.\n\nShould I review the new head or the *old* one?**", True, id="emphasis"),
        pytest.param("Should I wait? The checkout admission remains open.", False, id="earlier-question"),
        pytest.param("The checkout admission remains open.", False, id="statement"),
    ],
)
def test_checkout_continuation_respects_a_terminal_question(
    review_state,
    arguments: str,
    status: str,
    holder: str,
    exit_code: int,
    run_bound: bool,
    message: str,
    question: bool,
) -> None:
    """A terminal question before run creation ends the turn; every continuation keeps the shared pause clause."""
    payload, records = review_state
    payload["last_assistant_message"] = message
    receipt = command_record(
        f"{SLOT_HELPER} {arguments}", json.dumps({"status": status, "owner": holder, "slot": SLOT}), exit_code
    )
    records[2:] = [receipt, *([create_run_record("code-review")] if run_bound else [])]

    response = HOOK.evaluate(payload, records, "/plugin")

    if question and not run_bound:
        assert response == {}
    else:
        assert response["decision"] == "block"
        assert "pending question" in response["reason"]
        assert "preserve that pause" in response["reason"]


@pytest.mark.parametrize("user_record", [*USER_STEERING_RECORDS, pytest.param(None, id="no-steering")])
def test_user_message_after_slot_only_receipts_suppresses_continuation(review_state, user_record: dict | None) -> None:
    """A user message after any validated slot receipt stops later peer recovery, even after a free status."""
    payload, records = review_state
    peer = command_record(
        f"{SLOT_HELPER} acquire --slot {SLOT} --owner me --wait-seconds 60",
        json.dumps({"status": "busy", "owner": "peer", "slot": SLOT}),
        1,
    )
    free = command_record(
        f"{SLOT_HELPER} status --slot {SLOT}", json.dumps({"status": "free", "owner": None, "slot": SLOT})
    )
    records[2:] = [peer, free, *([user_record] if user_record else []), peer]

    response = HOOK.evaluate(payload, records, "/plugin")

    if user_record:
        assert response == {}
    else:
        assert response["decision"] == "block"
        assert "--queue" in response["reason"]


@pytest.mark.parametrize("order", ["after-peer", "after-free", "before-admission"])
@pytest.mark.parametrize(
    ("arguments", "error_slot", "expected"),
    [
        pytest.param("status --slot {slot}", SLOT, None, id="status-error"),
        pytest.param("hold --slot {slot} --owner me", SLOT, None, id="hold-error"),
        pytest.param("release --slot {slot} --owner me", SLOT, None, id="release-error"),
        pytest.param("acquire --slot {slot} --owner other", SLOT, None, id="other-requester-error"),
        pytest.param("status --slot {slot}", "/other/.git/codex-review-slot", "block", id="unrelated-slot-error"),
    ],
)
def test_matching_slot_helper_error_ends_continuation(
    review_state, order: str, arguments: str, error_slot: str, expected: str | None
) -> None:
    """A helper error at a slot this turn uses, before or after admission, outlasts any peer observation."""
    payload, records = review_state
    peer = command_record(
        f"{SLOT_HELPER} acquire --slot {SLOT} --owner me --wait-seconds 60",
        json.dumps({"status": "busy", "owner": "peer", "slot": SLOT}),
        1,
    )
    free = command_record(
        f"{SLOT_HELPER} status --slot {SLOT}", json.dumps({"status": "free", "owner": None, "slot": SLOT})
    )
    error = command_record(
        f"{SLOT_HELPER} {arguments.format(slot=error_slot)}",
        json.dumps({"status": "error", "owner": None, "slot": error_slot, "error": "invalid review slot marker"}),
        2,
    )
    sequences = {
        "after-peer": [peer, error, peer],
        "after-free": [peer, free, error, peer],
        "before-admission": [error, peer],
    }
    records[2:] = sequences[order]

    response = HOOK.evaluate(payload, records, "/plugin")

    assert response.get("decision") == expected
    assert ("--queue" in response.get("reason", "")) is (expected == "block")


@pytest.mark.parametrize(
    ("arguments", "output", "expected"),
    [
        pytest.param(
            "acquire --checkout /workspace --owner me --wait-seconds 60",
            json.dumps({"status": "busy", "owner": "approval-run", "slot": SLOT}),
            "block",
            id="owner-approval",
        ),
        pytest.param(
            "acquire --checkout /workspace --owner me --wait-seconds 60",
            json.dumps({"status": "busy", "owner": "denied-peer", "slot": SLOT}),
            "block",
            id="owner-denied",
        ),
        pytest.param(
            "acquire --checkout /workspace --owner me --wait-seconds 60",
            json.dumps({"status": "busy", "owner": "peer", "slot": "/srv/not permitted/codex-review-slot"}),
            "block",
            id="slot-not-permitted",
        ),
        pytest.param(
            "acquire --slot /srv/approval/codex-review-slot --owner me",
            json.dumps({"status": "busy", "owner": "peer", "slot": "/srv/approval/codex-review-slot"}),
            "block",
            id="slot-approval",
        ),
        pytest.param(
            "acquire --checkout /workspace --owner me --wait-seconds 60",
            "Command approval denied by the user.",
            None,
            id="host-denial-text",
        ),
    ],
)
def test_slot_receipt_routing_uses_fields_not_identifier_words(
    review_state, arguments: str, output: str, expected: str | None
) -> None:
    """Pause words in a validated owner ID or slot path keep the peer route; host denial text still stops it."""
    payload, records = review_state
    records[2:] = [command_record(f"{SLOT_HELPER} {arguments}", output, 1)]

    response = HOOK.evaluate(payload, records, "/plugin")

    assert response.get("decision") == expected
    assert ("--queue" in response.get("reason", "")) is (expected == "block")


@pytest.mark.parametrize(
    "terminator", [pytest.param("", id="bare"), pytest.param("\n", id="newline"), pytest.param("\\n", id="escaped")]
)
@pytest.mark.parametrize(
    ("arguments", "lines", "exit_code", "expected"),
    [
        pytest.param(
            "acquire --checkout /workspace --owner me --wait-seconds 60",
            [{"status": "busy", "owner": "peer", "slot": SLOT}],
            1,
            "--queue",
            id="bounded-wait",
        ),
        pytest.param(
            "acquire --checkout /workspace --owner me --queue",
            [{"status": "busy", "owner": "peer", "slot": SLOT}, {"status": "acquired", "owner": "me", "slot": SLOT}],
            0,
            "before an active run",
            id="queue-progress",
        ),
    ],
)
def test_slot_receipt_accepts_documented_host_output_shapes(
    review_state, terminator: str, arguments: str, lines: list[dict], exit_code: int, expected: str
) -> None:
    """Slot receipts accept the same bare, newline and escaped-newline endings as run creation receipts."""
    payload, records = review_state
    output = "\n".join(json.dumps(line) for line in lines) + terminator
    records[2:] = [command_record(f"{SLOT_HELPER} {arguments}", output, exit_code)]

    response = HOOK.evaluate(payload, records, "/plugin")

    assert response["decision"] == "block"
    assert expected in response["reason"]


def test_free_slot_before_run_still_requires_admission(review_state) -> None:
    """A free observation ends the wait but never grants admission to the queued requester."""
    payload, records = review_state
    records[2:] = [
        command_record(
            f"{SLOT_HELPER} acquire --checkout /workspace --owner queued --wait-seconds 60",
            json.dumps({"status": "busy", "owner": "peer", "slot": SLOT}),
            1,
        ),
        command_record(
            f"{SLOT_HELPER} status --slot {SLOT}", json.dumps({"status": "free", "owner": None, "slot": SLOT})
        ),
    ]
    result = HOOK.evaluate(payload, records, "/plugin")
    assert result["decision"] == "block"
    assert "original owner ID" in result["reason"]
    assert "acquire" in result["reason"]


@pytest.mark.parametrize(
    ("parent", "released", "expected"),
    [
        pytest.param("code-review", False, "release --slot", id="standalone-release-pending"),
        pytest.param("code-review", True, None, id="standalone-released"),
        pytest.param("code-remediate", False, "Continue remediation selection", id="nested-release-belongs-to-parent"),
    ],
)
def test_completed_review_slot_release_respects_parent(
    review_state, parent: str, released: bool, expected: str | None
) -> None:
    """An active own claim outlives a standalone report and stays with the parent across nested completion."""
    payload, records = review_state
    records[2:] = [
        command_record(
            f"{SLOT_HELPER} acquire --checkout /workspace --owner own --queue",
            json.dumps({"status": "acquired", "owner": "own", "slot": SLOT}),
        ),
        create_run_record(parent),
    ]
    if parent == "code-remediate":
        records.append(create_run_record("code-review"))
    records.append(
        command_record(
            '"/plugin/bin/python" "/plugin/shared/find-review-report.py" --complete-run .reports/codex/code-review/run-a'
        )
    )
    if released:
        records.append(
            command_record(
                f"{SLOT_HELPER} release --slot {SLOT} --owner own",
                json.dumps({"status": "released", "owner": None, "slot": SLOT}),
            )
        )
    response = HOOK.evaluate(payload, records, "/plugin")
    if expected is None:
        assert response == {}
    else:
        assert response["decision"] == "block"
        assert expected in response["reason"]


@pytest.fixture
def standalone_receipts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, review_state) -> tuple:
    """Record the real offline report-root receipt from the invoking Git repository."""
    caller = tmp_path / "caller"
    caller.mkdir()
    target = "https://github.com/acme/widgets/pull/12"
    subprocess.run(["git", "init", "-q", str(caller)], check=True)
    subprocess.run(
        ["git", "-C", str(caller), "remote", "add", "origin", "https://github.com/acme/widgets.git"], check=True
    )
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "Application Support"))
    prepared = subprocess.run(
        [
            sys.executable,
            str(PLUGIN_ROOT / "shared/review_code_safety.py"),
            "report-root",
            "--checkout",
            str(caller),
            "--pr-url",
            target,
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    report_root = Path(prepared.stdout.strip())
    payload, records = review_state
    payload["cwd"] = str(caller)
    platform = PLATFORMS["windows" if os.name == "nt" else "posix"]
    launcher = f'{platform["prefix"]}"{platform["root"]}/bin/{platform["launcher"]}"'

    def command(cwd: Path, helper: str, arguments: str, output: str, exit_code: int = 0) -> dict:
        """Record a packaged helper under the working directory actually supplied to the host."""
        return command_record(
            f'{launcher} "{platform["root"]}/shared/{helper}.py" {arguments}',
            output,
            exit_code,
            platform={**platform, "item_cwd": cwd.as_uri()},
        )

    prepare = command(
        caller, "review_code_safety", f'report-root --checkout "{caller}" --pr-url {target}', prepared.stdout
    )
    return payload, records[:2], prepare, caller, report_root, target, command, platform["root"]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("scope", "bound"),
    [
        pytest.param("original", True, id="parent-finalization"),
        pytest.param("unrelated-root", False, id="different-report-root"),
        pytest.param("unrelated-pr", False, id="different-pr"),
        pytest.param("unrelated-checkout", False, id="different-report-caller"),
        pytest.param("missing-claim", False, id="missing-original-claim"),
    ],
)
def test_external_remediation_evidence_keeps_original_claim_and_parent(
    standalone_receipts, scope: str, bound: bool
) -> None:
    """External allocation and promotion bind only the selected PR's reports to the original parent checkout."""
    payload, start, _, caller, workspace, target, command, plugin_root = standalone_receipts
    slot = str(caller / ".git/codex-review-slot")
    claim = command(
        caller,
        "review_checkout_slot",
        f'acquire --checkout "{caller}" --owner original --queue',
        json.dumps({"status": "acquired", "owner": "original", "slot": slot}),
    )
    prepared = subprocess.run(
        [
            sys.executable,
            str(PLUGIN_ROOT / "shared/review_code_safety.py"),
            "report-root",
            "--checkout",
            str(caller),
            "--pr-url",
            target,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    report_root = Path(prepared.stdout.strip())
    assert report_root == workspace == caller / ".git/codex-rig/reports"
    root_caller = workspace if scope == "unrelated-checkout" else caller
    receipt = command(
        caller, "review_code_safety", f'report-root --checkout "{root_caller}" --pr-url {target}', str(report_root)
    )
    requested = caller / "other" if scope == "unrelated-root" else report_root
    pr_number = 14 if scope == "unrelated-pr" else 12
    staging = requested / "code-remediate/staging"
    parent_run = requested / f"code-remediate/pr-{pr_number}/run-001"
    create = command(
        caller, "create_run", f'--skill code-remediate --pr-number {pr_number} --root "{requested}"', str(staging)
    )
    promote = command(
        caller,
        "create_run",
        f'--skill code-remediate --root "{requested}" --promote-pr-run "{staging}"',
        str(parent_run),
    )
    early = [*start, *([] if scope == "missing-claim" else [claim]), receipt, create, promote]
    state = HOOK.evaluate(payload, early, plugin_root)
    assert ("Continue remediation selection" in state.get("reason", "")) is bound
    if bound:
        child = requested / "code-review/child"
        child_create = command(
            caller,
            "create_run",
            f'--root "{requested}" --skill code-review --pr-url {target} --pr-number 12',
            str(child),
        )
        child_done = command(caller, "find-review-report", f'--complete-run "{child}"', "Validated child.")
        finish = command(
            caller, "remediation_finalize", f'finalize --run "{parent_run}" --promote', "Validated parent."
        )
        nested = [*early, child_create, child_done]
        assert "Continue remediation selection" in HOOK.evaluate(payload, nested, plugin_root)["reason"]
        assert "release --slot" in HOOK.evaluate(payload, [*nested, finish], plugin_root)["reason"]
        release = command(
            caller,
            "review_checkout_slot",
            f'release --slot "{slot}" --owner original',
            json.dumps({"status": "released", "owner": None, "slot": slot}),
        )
        assert HOOK.evaluate(payload, [*nested, finish, release], plugin_root) == {}
        stopped = {"type": "response_item", "payload": {"type": "message", "role": "user", "content": "Stop"}}
        assert HOOK.evaluate(payload, [*nested, stopped], plugin_root) == {}


@pytest.mark.integration
@pytest.mark.parametrize(
    ("target_matches", "root_matches", "lookups", "recover"),
    [
        pytest.param(True, True, "single", True, id="matching-target-and-storage"),
        pytest.param(False, True, "single", False, id="same-number-in-different-repository"),
        pytest.param(True, False, "single", False, id="different-storage"),
        pytest.param(True, True, "steered", False, id="user-intervened-before-claim"),
        pytest.param(True, True, "repeated", True, id="repeated-matching-lookup"),
        pytest.param(True, True, "conflicting", False, id="conflicting-earlier-lookup"),
    ],
)
def test_remediation_binds_early_report_root_only_to_matching_target(
    standalone_receipts, target_matches: bool, root_matches: bool, lookups: str, recover: bool
) -> None:
    """An earlier storage lookup must match the later admission even when the turn releases its claim."""
    payload, start, prepare, caller, reports, target, command, root = standalone_receipts
    requested_target = target if target_matches else "https://github.com/other/widgets/pull/12"
    slot = (
        caller
        / ".git/codex-review-targets"
        / hashlib.sha256(requested_target.encode()).hexdigest()
        / "codex-review-slot"
    )
    claim = command(
        caller,
        "review_checkout_slot",
        f'acquire --checkout "{caller}" --target {requested_target} --owner own',
        json.dumps({"status": "acquired", "owner": "own", "slot": str(slot)}),
    )
    requested_root = reports if root_matches else caller / "other"
    stage = requested_root / "code-remediate/staging"
    run = requested_root / "code-remediate/pr-12/run-001"
    create = command(
        caller, "create_run", f'--skill code-remediate --pr-number 12 --root "{requested_root}"', str(stage)
    )
    promote = command(
        caller, "create_run", f'--skill code-remediate --root "{requested_root}" --promote-pr-run "{stage}"', str(run)
    )
    release = command(
        caller,
        "review_checkout_slot",
        f'release --slot "{slot}" --owner own',
        json.dumps({"status": "released", "owner": None, "slot": str(slot)}),
    )
    steering = {"type": "response_item", "payload": {"type": "message", "role": "user"}}
    other_lookup = command(
        caller, "review_code_safety", f'report-root --checkout "{caller}" --pr-url {target}', f"{caller / 'other'}\n"
    )
    early = {
        "single": [prepare],
        "steered": [prepare, steering],
        "repeated": [prepare, prepare],
        "conflicting": [other_lookup, prepare],
    }[lookups]
    response = HOOK.evaluate(payload, [*start, *early, claim, create, promote, release], root)
    assert response.get("decision") == ("block" if recover else None)
    assert ("Continue remediation selection" in response.get("reason", "")) is recover


#: Revision the retained run's original branch receipt was prepared at and is checked against.
RESUMED_HEAD = "c" * 40
#: Digest the pending selection render reports for the frozen selection inventory.
RENDERED_DIGEST = "a" * 64


@dataclass
class ResumedRemediation:
    """Receipts of an in-place resumed PR remediation turn around its branch check, selection render and answer."""

    payload: dict
    start: list[dict]
    claim: dict
    release: dict
    answer: list[dict]
    caller: Path
    reports: Path
    target: str
    command: Callable[..., dict]
    plugin_root: str

    def check(
        self, run: Path, *, extra: str = "", exit_code: int = 0, status: str = "completed", **fields: object
    ) -> dict:
        """Record the original-branch check of one retained run; ``fields`` override the returned receipt."""
        receipt = {
            "schema_version": 2,
            "status": "prepared",
            "pr_number": 12,
            "pr_url": self.target,
            "worktree": str(self.caller),
            "initial_head": RESUMED_HEAD,
            "branch": "topic",
            "head_ref": "topic",
            "head_repository": "acme/widgets",
            **fields,
        }
        record = self.command(
            self.caller,
            "remediation_branch",
            f'check --receipt "{run / "remediation-branch.json"}" --expected-head {RESUMED_HEAD}{extra}',
            json.dumps(receipt) if exit_code == 0 else "check failed: branch moved",
            exit_code,
        )
        record["payload"]["item"]["status"] = status
        return record

    def render(self, run: Path, exit_code: int = 0) -> dict:
        """Record the pending selection render of one retained run."""
        receipt = {"status": "pass", "input_sha256": RENDERED_DIGEST, "rendered_sha256": "b" * 64}
        return self.command(
            self.caller,
            "final_handoff",
            f'selection --input "{run / "selection.json"}" --out-scope "{run / "resolution-scope.md"}"',
            json.dumps(receipt) if exit_code == 0 else "selection failed: inventory changed",
            exit_code,
        )

    def evaluate(self, *receipts: dict) -> dict:
        """Run the hook over the claim, the given receipts, the release and the submitted scope answer."""
        records = [*self.start, self.claim, *receipts, self.release, *self.answer]
        return HOOK.evaluate(self.payload, records, self.plugin_root)


@pytest.fixture
def resumed_remediation(standalone_receipts, answered_question) -> ResumedRemediation:
    """Provide a resumed PR run in the invoking checkout whose scope answer carries the rendered digest."""
    payload, start, _, caller, reports, target, command, root = standalone_receipts
    slot = caller / ".git/codex-review-targets" / hashlib.sha256(target.encode()).hexdigest() / "codex-review-slot"
    claim = command(
        caller,
        "review_checkout_slot",
        f'acquire --checkout "{caller}" --target {target} --owner own',
        json.dumps({"status": "acquired", "owner": "own", "slot": str(slot)}),
    )
    release = command(
        caller,
        "review_checkout_slot",
        f'release --slot "{slot}" --owner own',
        json.dumps({"status": "released", "owner": None, "slot": str(slot)}),
    )
    item = answered_question[1]["payload"]["item"]
    item["arguments"].update(
        question="Which findings should I remediate?",
        options=["All", "Required", "Suggestions", "Custom selection"],
        scope_digest=RENDERED_DIGEST,
    )
    item["result"]["structuredContent"].update(answer="All", scope_digest=RENDERED_DIGEST)
    return ResumedRemediation(payload, start, claim, release, answered_question, caller, reports, target, command, root)


@pytest.mark.integration
@pytest.mark.parametrize(
    "variant",
    [
        "matching",
        "wrong-pr",
        "wrong-worktree",
        "wrong-head",
        "outside-storage",
        "different-render",
        "wrong-digest",
        "failed-check",
    ],
)
def test_resumed_remediation_requires_same_checkout_run_and_rendered_scope(
    resumed_remediation: ResumedRemediation, variant: str
) -> None:
    """A resumed scope answer belongs to the verified branch, admitted target, retained run and rendered inventory."""
    flow = resumed_remediation
    run = (flow.caller if variant == "outside-storage" else flow.reports) / "code-remediate/pr-12/run-003"
    fields = {
        "wrong-pr": {"pr_url": flow.target.replace("acme", "other")},
        "wrong-worktree": {"worktree": str(flow.reports)},
        "wrong-head": {"initial_head": "d" * 40},
    }.get(variant, {})
    checked = flow.check(run, exit_code=2 if variant == "failed-check" else 0, **fields)
    rendered = flow.render(run.with_name("run-004") if variant == "different-render" else run)
    if variant == "wrong-digest":
        item = flow.answer[1]["payload"]["item"]
        item["arguments"]["scope_digest"] = item["result"]["structuredContent"]["scope_digest"] = "e" * 64
    result = flow.evaluate(checked, rendered)
    assert result.get("decision") == ("block" if variant == "matching" else None)
    assert ("commit-grouping question" in result.get("reason", "")) is (variant == "matching")
    assert ("Before dependent writes" in result.get("reason", "")) is (variant == "matching")


@pytest.mark.integration
@pytest.mark.parametrize(
    ("receipts", "recover"),
    [
        pytest.param(
            lambda flow, run: [flow.check(run), flow.render(run), flow.check(run)],
            True,
            id="repeated-check-of-the-same-run",
        ),
        pytest.param(
            lambda flow, run: [flow.check(run), flow.render(run), flow.check(run.with_name("run-004"))],
            False,
            id="second-check-names-another-run",
        ),
        pytest.param(
            lambda flow, run: [flow.check(run), flow.render(run), flow.check(run, exit_code=1)],
            False,
            id="failed-recheck-after-passing-render",
        ),
        pytest.param(
            lambda flow, run: [flow.check(run), flow.render(run), flow.render(run, exit_code=1)],
            False,
            id="failed-rerender-after-passing-render",
        ),
        pytest.param(
            lambda flow, run: [flow.check(run, status="failed"), flow.render(run)],
            False,
            id="failed-check-status-with-zero-exit",
        ),
        pytest.param(
            lambda flow, run: [flow.check(run, extra=" --verbose"), flow.render(run)],
            False,
            id="check-with-unrecognized-argument",
        ),
    ],
)
def test_resumed_remediation_yields_to_failed_or_conflicting_receipts(
    resumed_remediation: ResumedRemediation,
    receipts: Callable[[ResumedRemediation, Path], list[dict]],
    recover: bool,
) -> None:
    """Only an exact completed check and render, never contradicted later in the turn, restore the resumed run."""
    flow = resumed_remediation
    result = flow.evaluate(*receipts(flow, flow.reports / "code-remediate/pr-12/run-003"))
    assert result.get("decision") == ("block" if recover else None)
    assert ("commit-grouping question" in result.get("reason", "")) is recover


@pytest.mark.integration
def test_branch_collision_ends_after_release_without_changing_repositories(standalone_receipts) -> None:
    """Report the local and remote divergence and leave the next branch decision to the user."""
    payload, start, _, caller, _, target, command, root = standalone_receipts
    slot = str(caller / ".git/codex-review-slot")
    run = caller / ".reports/codex/code-remediate/staging"
    claim = command(
        caller,
        "review_checkout_slot",
        f'acquire --checkout "{caller}" --owner own',
        json.dumps({"status": "acquired", "owner": "own", "slot": slot}),
    )
    create = command(caller, "create_run", "--skill code-remediate --pr-number 12", str(run))
    failed = command(
        caller,
        "collect_pr",
        f'--target "{target}" --out "{run}/pr" --checkout --checkout-mode remediate',
        json.dumps({"status": "remediation-fork-checkout-branch-collision", "pr_url": target}),
        2,
    )
    release = command(
        caller,
        "review_checkout_slot",
        f'release --slot "{slot}" --owner own',
        json.dumps({"status": "released", "owner": None, "slot": slot}),
    )
    active = [*start, claim, create, failed]
    reason = HOOK.evaluate(payload, active, root)["reason"]
    assert "checkout-state.json" in reason
    assert "separate repository" not in reason
    assert HOOK.evaluate(payload, [*active, release], root) == {}


@pytest.mark.integration
@pytest.mark.parametrize(
    "terminal", ["completed", "remediation-fork-checkout-stopped", "remediation-fork-checkout-branch-collision"]
)
def test_writer_release_is_required_after_terminal_remediation(standalone_receipts, terminal: str) -> None:
    """A completed run or stopped collector must release the writer even after releasing its ordinary claim."""
    payload, start, prepare, caller, reports, target, command, root = standalone_receipts
    common = caller / ".git"
    writer = str(
        common
        / "codex-review-writers"
        / hashlib.sha256(os.fsencode(os.path.normcase(str(common)))).hexdigest()
        / "codex-review-slot"
    )
    ordinary = str(
        common / "codex-review-targets" / hashlib.sha256(target.encode("ascii")).hexdigest() / "codex-review-slot"
    )
    stage = reports / "code-remediate/staging"

    def acquire(option: str, slot: str) -> dict:
        """Record the holder and checkout attested by a successful acquisition."""
        return command(
            caller,
            "review_checkout_slot",
            f'acquire --checkout "{caller}" {option} --owner own --queue',
            json.dumps({"status": "acquired", "owner": "own", "slot": slot}),
        )

    def release(slot: str, owner: str) -> dict:
        """Record the release using the specified requester and retained locator."""
        return command(
            caller,
            "review_checkout_slot",
            f'release --slot "{slot}" --owner {owner}',
            json.dumps({"status": "released", "owner": None, "slot": slot}),
        )

    create = command(caller, "create_run", f'--skill code-remediate --pr-number 12 --root "{reports}"', str(stage))
    finish = (
        command(caller, "remediation_finalize", f'finalize --run "{stage}" --promote', "Validated.")
        if terminal == "completed"
        else command(
            caller,
            "collect_pr",
            f'--target {target} --out "{stage}/pr" --checkout --checkout-mode remediate',
            json.dumps({"status": terminal, "pr_url": target}),
            2,
        )
    )
    ended = [
        *start,
        acquire("--writer", writer),
        acquire(f"--target {target}", ordinary),
        prepare,
        create,
        finish,
        release(ordinary, "own"),
    ]
    assert "writer claim" in HOOK.evaluate(payload, ended, root)["reason"]
    assert "writer claim" in HOOK.evaluate(payload, [*ended, release(writer, "other")], root)["reason"]
    other_locator = str(common / "codex-review-writers" / ("0" * 64) / "codex-review-slot")
    assert "writer claim" in HOOK.evaluate(payload, [*ended, release(other_locator, "own")], root)["reason"]
    assert HOOK.evaluate({**payload, "last_assistant_message": "Waiting for your decision."}, ended, root) == {}
    assert HOOK.evaluate(payload, [*ended, release(writer, "own")], root) == {}


@pytest.mark.integration
@pytest.mark.parametrize("released", ["both", "writer", "target"])
def test_released_remediation_claims_require_reacquisition_before_edits(standalone_receipts, released: str) -> None:
    """A premature release of remediation claims resumes through checkout recovery before using saved scope."""
    payload, start, prepare, caller, reports, target, command, root = standalone_receipts
    common = caller / ".git"
    writer = str(common / "codex-review-writers" / ("1" * 64) / "codex-review-slot")
    slot = str(common / "codex-review-targets" / hashlib.sha256(target.encode()).hexdigest() / "codex-review-slot")
    stage = reports / "code-remediate/staging"
    records = [
        *start,
        command(
            caller,
            "review_checkout_slot",
            f'acquire --checkout "{caller}" --writer --owner me',
            json.dumps({"status": "acquired", "slot": writer, "owner": "me"}),
        ),
        command(
            caller,
            "review_checkout_slot",
            f'acquire --checkout "{caller}" --target {target} --owner me',
            json.dumps({"status": "acquired", "slot": slot, "owner": "me"}),
        ),
        prepare,
        command(caller, "create_run", f'--skill code-remediate --pr-number 12 --root "{reports}"', str(stage)),
    ]
    locators = {"both": (slot, writer), "writer": (writer,), "target": (slot,)}[released]
    records.extend(
        command(
            caller,
            "review_checkout_slot",
            f'release --slot "{locator}" --owner me',
            json.dumps({"status": "released", "slot": locator, "owner": None}),
        )
        for locator in locators
    )
    response = HOOK.evaluate(payload, records, root)
    assert response["decision"] == "block"
    assert "original invocation ID" in response["reason"]
    assert "report-only recovery" in response["reason"]
    assert "PR State Preflight" in response["reason"]
    assert "writer before the PR target" in response["reason"]
    assert "reverify" in response["reason"]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("case", "recover"),
    [
        pytest.param("retained", True, id="released-original-claims"),
        pytest.param("wrong-root", False, id="unrelated-storage"),
        pytest.param("wrong-number", False, id="different-target"),
        pytest.param("wrong-owner", False, id="unrelated-writer"),
        pytest.param("rejected-root", False, id="storage-lookup-failed"),
        pytest.param("late-rejection", False, id="confirmation-failed"),
        pytest.param("steered", False, id="user-stopped"),
        pytest.param("scope-menu", False, id="scope-decision-pending"),
    ],
)
def test_remediation_allocation_without_storage_lookup_recovers_confirmation(
    standalone_receipts, case: str, recover: bool
) -> None:
    """A retained Git-admin root needs confirmation before the released remediation claims can support further work."""
    payload, start, prepare, caller, reports, target, command, root = standalone_receipts
    payload["last_assistant_message"] = (
        "All / Required / Suggestions / Custom selection" if case == "scope-menu" else payload["last_assistant_message"]
    )
    writer = str(caller / ".git/codex-review-writers" / ("1" * 64) / "codex-review-slot")
    slot = str(caller / ".git/codex-review-targets" / hashlib.sha256(target.encode()).hexdigest() / "codex-review-slot")
    writer_owner = "another" if case == "wrong-owner" else "own"
    requested_root = caller / "other" if case == "wrong-root" else reports
    pr_number = 13 if case == "wrong-number" else 12
    failed_lookup = command(
        caller, "review_code_safety", f'report-root --checkout "{caller}" --pr-url {target}', "no matching remote", 1
    )
    records = [
        *start,
        command(
            caller,
            "review_checkout_slot",
            f'acquire --checkout "{caller}" --writer --owner {writer_owner}',
            json.dumps({"status": "acquired", "slot": writer, "owner": writer_owner}),
        ),
        command(
            caller,
            "review_checkout_slot",
            f'acquire --checkout "{caller}" --target {target} --owner own',
            json.dumps({"status": "acquired", "slot": slot, "owner": "own"}),
        ),
        *([failed_lookup] if case == "rejected-root" else []),
        command(
            caller,
            "create_run",
            f'--skill code-remediate --pr-number {pr_number} --root "{requested_root}"',
            str(requested_root / "code-remediate/staging"),
        ),
        command(
            caller,
            "review_checkout_slot",
            f'release --slot "{slot}" --owner own',
            json.dumps({"status": "released", "slot": slot, "owner": None}),
        ),
        command(
            caller,
            "review_checkout_slot",
            f'release --slot "{writer}" --owner {writer_owner}',
            json.dumps({"status": "released", "slot": writer, "owner": None}),
        ),
    ]
    steering = {"type": "response_item", "payload": {"type": "message", "role": "user", "content": "Stop"}}
    records.extend({"steered": [steering], "late-rejection": [failed_lookup]}.get(case, []))
    response = HOOK.evaluate(payload, records, root)
    assert response.get("decision") == ("block" if recover else None)
    reason = response.get("reason", "")
    assert ("report-root" in reason) is recover
    assert ("original invocation ID" in reason) is recover
    assert ("explicit +review" in reason) is recover
    confirmed = HOOK.evaluate(payload, [*records, prepare], root)
    assert ("Continue remediation selection" in confirmed.get("reason", "")) is recover


@pytest.mark.integration
@pytest.mark.parametrize("use_root", [True, False])
def test_original_remediation_from_checkout_subdirectory(standalone_receipts, use_root: bool) -> None:
    """Original-checkout remediation keeps its acquired claim through finalization from either session directory."""
    payload, start, _, caller, _, _, command, root = standalone_receipts
    subdir = caller / "session"
    subdir.mkdir()
    payload = {**payload, "cwd": str(subdir)}
    workdir = caller if use_root else subdir
    slot = str(caller / ".git/codex-review-slot")
    own = command(
        workdir,
        "review_checkout_slot",
        f'acquire --checkout "{caller}" --owner own --queue',
        json.dumps({"status": "acquired", "owner": "own", "slot": slot}),
    )
    run = ".reports/codex/code-remediate/staging"
    create = command(workdir, "create_run", "--skill code-remediate --pr-number 12", run)
    finish = command(workdir, "remediation_finalize", f"finalize --run {run} --promote", "Validated")
    release = command(
        workdir,
        "review_checkout_slot",
        f'release --slot "{slot}" --owner own',
        json.dumps({"status": "released", "owner": None, "slot": slot}),
    )
    active = [*start, own, create]
    assert "Continue remediation selection" in HOOK.evaluate(payload, active, root)["reason"]
    assert "release --slot" in HOOK.evaluate(payload, [*active, finish], root)["reason"]
    assert HOOK.evaluate(payload, [*active, finish, release], root) == {}
    subprocess.run(["git", "init", "-q", str(subdir)], check=True)
    assert "release --slot" not in HOOK.evaluate(payload, [*active, finish], root).get("reason", "")


@pytest.mark.integration
def test_pr_admission_stays_bound_across_exclusivity_and_completion(standalone_receipts) -> None:
    """Only the original PR claim can resume after an exclusive phase, and final release closes the handoff."""
    payload, start, prepare, caller, reports, target, command, root = standalone_receipts
    digest = hashlib.sha256(target.lower().encode("ascii")).hexdigest()
    slot = str(caller / ".git/codex-review-targets" / digest / "codex-review-slot")
    exclusive = str(caller / ".git/codex-review-slot")
    writer = str(caller / ".git/codex-review-writers" / ("d" * 64) / "codex-review-slot")
    stage = reports / "code-review/staging"
    run = reports / "code-review/pr-12/run-001"

    def operation(action: str, args: str, slot_path: str, owner: str | None) -> dict:
        """Record the scoped helper's exit-zero receipt."""
        state = {"acquire": "acquired", "release": "released"}[action]
        return command(
            caller,
            "review_checkout_slot",
            f"{action} {args} --owner own",
            json.dumps({"status": state, "slot": slot_path, "owner": owner}),
        )

    acquire = operation("acquire", f'--checkout "{caller}" --target {target}', slot, "own")
    release = operation("release", f'--slot "{slot}"', slot, None)
    writer_acquire = operation("acquire", f'--checkout "{caller}" --writer', writer, "own")
    writer_release = operation("release", f'--slot "{writer}"', writer, None)
    create = command(
        caller, "create_run", f'--root "{reports}" --skill code-review --pr-number 12 --pr-url {target}', str(stage)
    )
    promote = command(
        caller, "create_run", f'--root "{reports}" --skill code-review --promote-pr-run "{stage}"', str(run)
    )
    complete = command(caller, "find-review-report", f'--complete-run "{run}"', "Validated review.")
    active = [*start, acquire, prepare, writer_acquire, writer_release, create]
    assert "Continue the read-only review" in HOOK.evaluate(payload, active, root)["reason"]
    early_release = HOOK.evaluate(payload, [*active, release], root)
    assert early_release["decision"] == "block"
    recovery = early_release["reason"]
    assert "reacquire" in recovery.lower()
    assert "retained locator" in recovery
    assert "original owner ID" in recovery
    assert "PR State Preflight" in recovery
    assert "reverify" in recovery
    assert "--complete-run" in recovery
    assert "stdout" in recovery
    assert "verbatim" in recovery
    assert "reacquire" not in HOOK.evaluate(payload, [*active, release, acquire], root)["reason"].lower()
    take_exclusive = operation("acquire", f'--checkout "{caller}"', exclusive, "own")
    release_exclusive = operation("release", f'--slot "{exclusive}"', exclusive, None)
    assert HOOK.evaluate(payload, [*active, take_exclusive], root) == {}
    exclusive_ended = [*active, release, take_exclusive, release_exclusive]
    assert "released PR slot" not in HOOK.evaluate(payload, exclusive_ended, root)["reason"]
    transitioned = [
        *exclusive_ended,
        operation("acquire", f'--slot "{slot}"', slot, "own"),
        promote,
        complete,
    ]
    assert "release --slot" in HOOK.evaluate(payload, transitioned, root)["reason"]
    assert HOOK.evaluate(payload, [*transitioned, release], root) == {}


@pytest.mark.integration
def test_local_review_keeps_its_ordinary_claim_until_final_release(standalone_receipts) -> None:
    """A real checkout-local admission survives the local report's completion and binds its matching release."""
    payload, start, _, caller, _, _, command, root = standalone_receipts
    args = ["acquire", "--checkout", str(caller), "--local", "--owner", "own"]
    helper = PLUGIN_ROOT / "shared/review_checkout_slot.py"
    acquired = subprocess.run([sys.executable, str(helper), *args], check=True, capture_output=True, text=True)
    claim = command(
        caller, "review_checkout_slot", f'acquire --checkout "{caller}" --local --owner own', acquired.stdout
    )
    slot = json.loads(acquired.stdout)["slot"]
    run = caller / ".reports/codex/code-review/local"
    create = command(caller, "create_run", "--skill code-review --branch main --local-scope working-tree", str(run))
    complete = command(caller, "find-review-report", f'--complete-run "{run}"', "Validated local review.")
    finished = [*start, claim, create, complete]
    assert "release --slot" in HOOK.evaluate(payload, finished, root)["reason"]
    released = subprocess.run(
        [sys.executable, str(helper), "release", "--slot", slot, "--owner", "own"],
        check=True,
        capture_output=True,
        text=True,
    )
    release = command(caller, "review_checkout_slot", f'release --slot "{slot}" --owner own', released.stdout)
    assert HOOK.evaluate(payload, [*finished, release], root) == {}


@pytest.mark.integration
def test_linked_remediation_receipt_binds_collector_in_same_common_directory(standalone_receipts) -> None:
    """The scoped locator and allocator receipt must name the same repository before routing a failed checkout."""
    payload, start, prepare, caller, reports, target, command, root = standalone_receipts
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        **dict.fromkeys(("TMPDIR", "TMP", "TEMP"), str(caller.parent)),
    }
    subprocess.run(
        [
            "git",
            "-C",
            str(caller),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "fixture",
        ],
        check=True,
        env=env,
    )
    run = reports / "code-remediate/staging"
    run.mkdir(parents=True)
    locator = run / "remediation-worktree.json"
    allocation = subprocess.run(
        [sys.executable, str(PLUGIN_ROOT / "shared/remediation_branch.py"), "worktree", "--receipt", str(locator)],
        cwd=caller,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    source = Path(json.loads(allocation.stdout)["worktree"])
    digest = hashlib.sha256(target.encode("ascii")).hexdigest()
    slot = str(caller / ".git/codex-review-targets" / digest / "codex-review-slot")
    claim = command(
        caller,
        "review_checkout_slot",
        f'acquire --checkout "{caller}" --target {target} --owner own',
        json.dumps({"status": "acquired", "slot": slot, "owner": "own"}),
    )
    create = command(caller, "create_run", f'--skill code-remediate --pr-number 12 --root "{reports}"', str(run))
    stopped = command(
        source,
        "collect_pr",
        f'--target {target} --out "{run}/pr" --checkout --checkout-mode remediate',
        json.dumps({"status": "remediation-fork-checkout-stopped", "pr_url": target}),
        2,
    )
    prefix = [*start, claim, prepare, create]
    allocated = command(caller, "remediation_branch", f'worktree --receipt "{locator}"', allocation.stdout)
    assert "stopped at fork checkout" in HOOK.evaluate(payload, [*prefix, allocated, stopped], root)["reason"]
    wrong = {**json.loads(allocation.stdout), "git_common_dir": str(caller.parent / "other")}
    unrelated = command(caller, "remediation_branch", f'worktree --receipt "{locator}"', json.dumps(wrong))
    assert "stopped at fork checkout" not in HOOK.evaluate(payload, [*prefix, unrelated, stopped], root)["reason"]


@pytest.mark.integration
@pytest.mark.parametrize("scope", ["other-target", "wrong-receipt", "writer"])
def test_foreign_claim_cannot_bind_standalone_reports(standalone_receipts, scope: str) -> None:
    """A different target, a mismatched digest or only a writer lock never admits the selected review."""
    payload, start, prepare, caller, reports, target, command, root = standalone_receipts
    other = target.replace("/12", "/13")
    claimed = target if scope == "wrong-receipt" else other
    digest = hashlib.sha256(other.encode("ascii")).hexdigest()
    namespace = "codex-review-writers" if scope == "writer" else "codex-review-targets"
    slot = caller / ".git" / namespace / digest / "codex-review-slot"
    option = "--writer" if scope == "writer" else f"--target {claimed}"
    claim = command(
        caller,
        "review_checkout_slot",
        f'acquire --checkout "{caller}" {option} --owner own',
        json.dumps({"status": "acquired", "slot": str(slot), "owner": "own"}),
    )
    stage = reports / "code-review/stage"
    create = command(
        caller, "create_run", f'--root "{reports}" --skill code-review --pr-number 12 --pr-url {target}', str(stage)
    )
    response = HOOK.evaluate(payload, [*start, claim, prepare, create], root)
    assert "Continue the read-only review" not in response.get("reason", "")


@pytest.mark.integration
@pytest.mark.parametrize("historical", [False, True])
def test_confirmed_report_resumes_from_its_registered_location(standalone_receipts, historical: bool) -> None:
    """The invoking checkout can resume a confirmed PR report in current or historical storage."""
    payload, start, _, caller, reports, target, command, root = standalone_receipts
    workspace = caller.parent / "codex-rig/review-workspaces" / ("a" * 64)
    base = workspace / ".reports/codex" if historical else reports
    run = base / "code-review/pr-12/run-001"
    selected = command(caller, "find-review-report", f'--latest-run --target {target} --expect-run "{run}"', str(run))
    complete = command(
        workspace if historical else caller, "find-review-report", f'--complete-run "{run}"', "Validated review."
    )
    assert "Continue the read-only review" in HOOK.evaluate(payload, [*start, selected], root)["reason"]
    assert HOOK.evaluate(payload, [*start, selected, complete], root) == {}


@pytest.mark.parametrize(
    ("ending", "continues"),
    [
        pytest.param("live", True, id="peer-inspection-is-live"),
        pytest.param("labeled-progress", True, id="labeled-peer-progress"),
        pytest.param("user-before-progress", False, id="user-stop-before-first-peer-receipt"),
        pytest.param("caller-interrupt", True, id="caller-interrupted-peer-wait"),
        pytest.param("host-interrupt", False, id="unattributed-interrupt"),
        pytest.param("different-process", False, id="unrelated-caller-interrupt"),
        pytest.param("user-stop", False, id="user-steering-after-progress"),
        pytest.param("question", False, id="question-after-progress"),
        pytest.param("denied", False, id="denial-after-progress"),
    ],
)
def test_live_queue_progress_and_interruption(review_state, ending: str, continues: bool) -> None:
    """Bind code-mode live peer receipts and the host's terminal process status across a queue interruption."""
    payload, records = review_state
    payload["last_assistant_message"] = "The review is blocked before collection while its peer inspects the checkout."
    del records[2:]
    command = f"{SLOT_HELPER} acquire --checkout /workspace --owner queued --queue"
    label = {"queue": "waiting"} if ending == "labeled-progress" else {}
    peer = json.dumps({"status": "busy", "owner": "peer", "slot": SLOT, **label})
    held = json.dumps({"status": "inspection-required", "owner": "peer", "slot": SLOT, **label})
    poll = 'const r = await tools.write_stdin({session_id:17,chars:"",yield_time_ms:20000}); text(r.output); text(r.session_id)'

    def code_call(source: str, call_id: str) -> dict:
        """Record a literal Code Mode invocation with the host-provided correlation ID."""
        return {
            "type": "response_item",
            "payload": {"type": "custom_tool_call", "name": "exec", "input": source, "call_id": call_id},
        }

    def code_output(value: str, call_id: str) -> dict:
        """Emit the separate stdout and process ID text blocks from the running command session."""
        blocks = [
            {"type": "input_text", "text": text}
            for text in ("Script completed\nWall time 1.0 seconds\nOutput:\n", value + "\n", "17")
        ]
        return {
            "type": "response_item",
            "payload": {"type": "custom_tool_call_output", "call_id": call_id, "output": blocks},
        }

    records.extend(
        [
            code_call(
                f"const r = await tools.exec_command({{cmd:{json.dumps(command)},yield_time_ms:1000}}); text(r.output); text(r.session_id)",
                "start",
            ),
            code_output(peer, "start"),
            code_call(poll, "poll"),
            code_output(held, "poll"),
        ]
    )
    if ending in {"caller-interrupt", "host-interrupt", "different-process"}:
        if ending != "host-interrupt":
            process = 17 if ending == "caller-interrupt" else 18
            records.append(
                code_call(
                    poll.replace("session_id:17", f"session_id:{process}")
                    .replace('chars:""', r'chars:"\u0003"')
                    .replace("text(r.session_id)", "text(r.exit_code)"),
                    "interrupt",
                )
            )
        terminal = command_record(
            command,
            f'{peer}\n{held}\nTraceback (most recent call last):\n  File "helper", line 1, in main\n    time.sleep(1)\nKeyboardInterrupt\n',
            130,
        )
        terminal["payload"]["item"].update(status="failed", process_id="17")
        records.append(terminal)
    elif ending in {"user-stop", "user-before-progress"}:
        records.insert(
            3 if ending == "user-before-progress" else len(records),
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": "Stop"}},
        )
    elif ending == "question":
        records.append({"type": "response_item", "payload": {"type": "function_call", "name": HOOK.NATIVE_QUESTION}})
    elif ending == "denied":
        records.append(
            {"type": "response_item", "payload": {"type": "function_call_output", "output": "approval denied"}}
        )
    result = HOOK.evaluate(payload, records, "/plugin")
    assert (result.get("decision") == "block") is continues
    if continues:
        assert "peer" in result["reason"]
        assert "--queue" in result["reason"]


@pytest.mark.parametrize("platform", PLATFORM_CASES)
@pytest.mark.parametrize(
    ("entry_args", "relative_run"),
    [
        pytest.param("--target https://github.com/org/repo/pull/41", "code-review/pr-41/run-002", id="pr"),
        pytest.param("--target https://github.com/org/repo/pull/41", "review/pr-41/run-002", id="legacy-pr"),
        pytest.param("--local-scope working-tree", "code-review/local-run", id="working-tree"),
        pytest.param("--local-scope path --local-target src", "code-review/local-run", id="path"),
        pytest.param("--local-scope commit --local-target HEAD^!", "code-review/local-run", id="commit"),
    ],
)
def test_chosen_resume_stays_active_through_completion(
    review_state, answered_question, platform: dict, entry_args: str, relative_run: str
) -> None:
    """A confirmed retained run needs its own completion receipt in the new turn."""
    payload, records = review_state
    payload["cwd"] = platform["cwd"]
    run = f"{platform['cwd']}/.reports/codex/{relative_run}"
    finder = f'{platform["prefix"]}"{platform["root"]}/bin/{platform["launcher"]}" "{platform["root"]}/shared/find-review-report.py"'
    selection = f"{finder} --latest-run {entry_args}"
    lookup = command_record("", run + "\n")
    item = lookup["payload"]["item"]
    item.update(command=[*platform["shell"], selection], cwd=platform["item_cwd"])
    records[2:] = [lookup]
    assert HOOK.evaluate(payload, records, platform["root"]) == {}
    answer = answered_question[1]["payload"]["item"]
    answer["arguments"]["options"] = ["Resume last", "Start fresh"]
    answer["result"]["structuredContent"]["answer"] = "Resume last"
    records[2:2] = answered_question
    item["command"][-1] += f' --expect-run "{run}"'
    assert HOOK.evaluate(payload, records, platform["root"])["decision"] == "block"
    receipt = command_record("")
    receipt["payload"]["item"].update(
        command=[*platform["shell"], f'{finder} --complete-run "{run}"'], cwd=platform["item_cwd"]
    )
    records.append(receipt)
    assert HOOK.evaluate(payload, records, platform["root"]) == {}


@pytest.mark.parametrize("skill", ["code-review", "code-remediate"])
@pytest.mark.parametrize("status", ["completed", "failed", "declined", "inProgress"])
def test_file_change_preserves_workflow_or_controlling_stop(review_state, skill: str, status: str) -> None:
    """Completed artifact edits leave work pending; unsuccessful or pending edits preserve the host stop.

    Creating a blueprint through apply_patch emits FileChange rather than CommandExecution. That ordinary receipt must
    not disable the unfinished-work diagnostic, nor may it count as workflow completion.
    """
    payload, records = review_state
    records[-1] = create_run_record(skill)
    records.append(
        {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "thread_id": "session",
                "turn_id": "turn",
                "item": {
                    "type": "FileChange",
                    "status": status,
                    "changes": {
                        f"/workspace/.reports/codex/{skill}/run-a/notes.md": {"type": "add", "content": "Notes"}
                    },
                    "stdout": "Success" if status == "completed" else "",
                    "stderr": "",
                },
            },
        }
    )

    response = HOOK.evaluate(payload, records, "/plugin")

    assert response.get("decision") == ("block" if status == "completed" else None)


@pytest.mark.parametrize("output_dir", ["run", "pr"])
def test_terminal_fork_checkout_requires_bound_collector_and_owner_release(review_state, output_dir: str) -> None:
    """A terminal checkout can be reported after its own released claim, without entering remediation selection."""
    payload, start = review_state
    run = "/workspace/.reports/codex/code-remediate/run-a"
    target = "https://github.com/acme/widgets/pull/17"
    out = run if output_dir == "run" else run + "/pr"
    acquire = command_record(
        f"{SLOT_HELPER} acquire --checkout /workspace --owner agent --queue",
        json.dumps({"status": "acquired", "owner": "agent", "slot": SLOT}),
    )
    create = command_record(
        '"/plugin/bin/python" "/plugin/shared/create_run.py" --skill code-remediate --pr-number 17', run
    )
    active = [*start[:2], acquire, create]
    invocation = f'"/plugin/bin/python" "/plugin/shared/collect_pr.py" --out "{out}" --target {target} --checkout --checkout-mode remediate'
    output = json.dumps({"status": "remediation-fork-checkout-stopped", "pr_url": target})
    failure = command_record(invocation, output, 2)
    release = command_record(
        f"{SLOT_HELPER} release --slot {SLOT} --owner agent",
        json.dumps({"status": "released", "owner": None, "slot": SLOT}),
    )
    assert "release" in HOOK.evaluate(payload, [*active, failure], "/plugin")["reason"].lower()
    assert HOOK.evaluate(payload, [*active, failure, release], "/plugin") == {}
    help_call = command_record('"/plugin/bin/python" "/plugin/shared/collect_pr.py" --help')
    assert HOOK.evaluate(payload, [*active, failure, help_call, release], "/plugin") == {}
    assert HOOK.evaluate(payload, [*active, release, failure], "/plugin")["decision"] == "block"
    assert HOOK.evaluate(payload, [*active, failure, release, acquire], "/plugin")["decision"] == "block"

    # The native failure is an intermediate step for recoverable checkouts. A newer attempt supersedes the old result.
    for other in (
        command_record(invocation, "", 2),
        command_record(invocation, "github-command-failed:local-pr-checkout", 2),
        command_record(invocation.replace(out, out + "-other"), output, 2),
        command_record(invocation.replace(target, target + "0"), output, 2),
        command_record(invocation.replace("--checkout-mode remediate", "--checkout-mode review"), output, 2),
        command_record(invocation, output, 0),
    ):
        assert HOOK.evaluate(payload, [*active, other, release], "/plugin")["decision"] == "block"
    assert (
        HOOK.evaluate(payload, [*active, failure, release, command_record(invocation)], "/plugin")["decision"]
        == "block"
    )
    wrong_release = command_record(
        f"{SLOT_HELPER} release --slot {SLOT} --owner another",
        json.dumps({"status": "released", "owner": None, "slot": SLOT}),
    )
    assert HOOK.evaluate(payload, [*active, failure, wrong_release], "/plugin")["decision"] == "block"


def test_terminal_checkout_in_verified_linked_worktree(review_state) -> None:
    """A collector run from another checkout needs its own earlier allocation in the same remediation run."""
    payload, records = review_state
    run = "/workspace/.reports/codex/code-remediate/run-a"
    target = "https://github.com/acme/widgets/pull/17"
    tree = "/private/allocated/worktree"
    acquire = command_record(
        f"{SLOT_HELPER} acquire --checkout /workspace --owner own",
        json.dumps({"status": "acquired", "owner": "own", "slot": SLOT}),
    )
    creation = command_record(
        '"/plugin/bin/python" "/plugin/shared/create_run.py" --skill code-remediate --pr-number 17', run
    )
    allocator = '"/plugin/bin/python" "/plugin/shared/remediation_branch.py" worktree --receipt'
    identity = json.dumps(
        {
            "schema_version": 1,
            "initial_head": "a" * 40,
            "invoking_checkout": "/workspace",
            "git_common_dir": "/workspace/.git",
            "worktree": tree,
        }
    )
    allocation = command_record(f'{allocator} "{run}/remediation-worktree.json"', identity)
    failure = command_record(
        f'"/plugin/bin/python" "/plugin/shared/collect_pr.py" --target {target} --out "{run}/pr" --checkout --checkout-mode remediate',
        json.dumps({"pr_url": target, "status": "remediation-fork-checkout-stopped"}),
        2,
    )
    failure["payload"]["item"]["cwd"] = tree
    release = command_record(
        f"{SLOT_HELPER} release --slot {SLOT} --owner own",
        json.dumps({"status": "released", "owner": None, "slot": SLOT}),
    )
    current = [*records[:2], acquire, creation]
    assert HOOK.evaluate(payload, [*current, failure, release], "/plugin")["decision"] == "block"
    wrong_run = command_record(f'{allocator} "{run}-other/remediation-worktree.json"', identity)
    assert HOOK.evaluate(payload, [*current, wrong_run, failure, release], "/plugin")["decision"] == "block"
    assert HOOK.evaluate(payload, [*current, allocation, failure, release], "/plugin") == {}


@pytest.mark.parametrize("platform", PLATFORM_CASES)
@pytest.mark.parametrize("status", ["remediation-fork-checkout-stopped", "remediation-fork-checkout-branch-collision"])
def test_bound_terminal_checkout_accepts_host_path_forms(review_state, platform: dict, status: str) -> None:
    """Canonical option values and GitHub owner case identify the same run across Unix and Windows hosts."""
    payload, start = review_state
    root, cwd = platform["root"], platform["cwd"]
    payload["cwd"] = cwd
    launcher = f'{platform["prefix"]}"{root}/bin/{platform["launcher"]}"'
    slot = f"{cwd}/.git/codex-review-slot"
    run = f"{cwd}/.reports/codex/code-remediate/two words"
    target = "https://github.com/Acme/Widgets/pull/17"

    def receipt(helper: str, args: str, output: str, status: int = 0) -> dict:
        return command_record(f'{launcher} "{root}/shared/{helper}.py" {args}', output, status, platform=platform)

    captured = [
        *start[:2],
        receipt(
            "review_checkout_slot",
            f'acquire --checkout "{cwd}" --owner own',
            json.dumps({"status": "acquired", "owner": "own", "slot": slot}),
        ),
        receipt("create_run", "--skill code-remediate --pr-number=17", run),
        receipt(
            "collect_pr",
            f'--checkout-mode=remediate --checkout --out="{run}/pr" --target={target}',
            json.dumps({"pr_url": target.lower(), "status": status}),
            2,
        ),
        receipt(
            "review_checkout_slot",
            f'release --slot "{slot}" --owner own',
            json.dumps({"status": "released", "owner": None, "slot": slot}),
        ),
    ]
    assert HOOK.evaluate(payload, captured, root) == {}


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
    assert "explicit +review" in response["reason"]
    assert "--complete-run" not in response["reason"]


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
    records.insert(2, create_run_record("code-remediate"))
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


@pytest.mark.parametrize("review_complete", [False, True])
def test_later_remediation_run_cannot_redirect_standalone_review(review_state, review_complete: bool) -> None:
    """Keep the original review owner even if another workflow creates a run during that review."""
    payload, records = review_state
    records.append(create_run_record("code-remediate"))
    if review_complete:
        records.append(
            command_record(
                '"/plugin/bin/python" "/plugin/shared/find-review-report.py" --complete-run .reports/codex/code-review/run-a'
            )
        )
    result = HOOK.evaluate(payload, records, "/plugin")
    if review_complete:
        assert result == {}
    else:
        assert result["decision"] == "block"
        assert "--complete-run" in result["reason"]
        assert "remediat" not in result["reason"].lower()


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
        "Waiting for your decision. Reply Approve or Deny.",
        "The retry awaits your decision.\n\n- Approve\n- Deny",
        "The retry awaits your decision.\n\nApprove / Deny",
        "Should I run the command? Reply `Approve` or `Deny`.",
        "Waiting for your decision. The retry budget is exhausted.",
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


@pytest.fixture
def async_question() -> list[dict]:
    """Build the host's direct async call, recorded question, and separately correlated acceptance."""
    questions = [{"title": "Authorize the named check?", "options": ["Approve", "Deny"]}]
    return [
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "request_user_input_async",
                "call_id": "ask-1",
                "arguments": json.dumps({"questions": questions}),
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "thread_id": "session",
                "turn_id": "turn",
                "item": {
                    "type": "AgentMessage",
                    "id": "ask-1",
                    "phase": "final_answer",
                    "delivery": "async",
                    "questions": questions,
                },
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "ask-1",
                "output": '{"accepted":true}',
            },
        },
    ]


@pytest.mark.parametrize("delivered_async", [False, True])
def test_status_without_actionable_decision_repairs_delivery_only(
    review_state, async_question, delivered_async
) -> None:
    """Correct a status-only final without continuing dependent work or opening duplicate controls."""
    payload, records = review_state
    final = "The check awaits your decision.\n\n| Check | Status |\n| --- | --- |\n| Tests | Blocked |"
    payload["last_assistant_message"] = final if delivered_async else "Waiting for your decision before continuing."
    if delivered_async:
        records.extend(async_question)
        records.append(
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "thread_id": "session",
                    "turn_id": "turn",
                    "item": {"type": "Extension", "id": "timer", "kind": "clock.sleep", "durationMs": 20},
                },
            }
        )
    result = HOOK.evaluate(payload, records, "/plugin")
    assert result["decision"] == "block"
    assert "question delivery only" in result["reason"]
    assert "complete actionable choices" in result["reason"]
    assert "explicit answer" in result["reason"]
    assert "first unmet checkpoint" not in result["reason"]


@pytest.mark.parametrize("status", ["waiting for the decision", "awaiting your decision"])
def test_check_inventory_cannot_substitute_for_a_pending_decision(review_state, status: str) -> None:
    """A bullet list of blocked checks still needs an actionable question."""
    payload, records = review_state
    payload["last_assistant_message"] = f"The review is {status}.\n\n- Tests blocked\n- Type check blocked"
    assert "question delivery only" in HOOK.evaluate(payload, records, "/plugin")["reason"]


def test_question_in_a_finding_cannot_suppress_normal_recovery(review_state) -> None:
    """Questions quoted in a report do not automatically become live user decisions."""
    payload, records = review_state
    payload["last_assistant_message"] = 'The log records "Retry?" when a reply times out.'
    assert "first unmet checkpoint" in HOOK.evaluate(payload, records, "/plugin")["reason"]


def test_combined_selection_and_decision_status_preserves_pending_decision_delivery(review_state) -> None:
    """A generic pending decision retains its own eligibility checks when the findings also lack a selection."""
    payload, records = review_state
    payload["last_assistant_message"] = "Findings are awaiting selection. Waiting for your decision."
    records[2] = create_run_record("code-remediate")
    result = HOOK.evaluate(payload, records, "/plugin")
    assert "question delivery only" in result.get("reason", "")
    assert "Runtime permission remains with its dedicated host control." in result["reason"]


@pytest.mark.parametrize(
    ("skill", "ending", "native_pending", "recover"),
    [
        pytest.param("code-remediate", "The inventory still needs reconciliation.", False, True, id="status-only"),
        pytest.param("code-remediate", "Which findings should I remediate?", False, False, id="typed-question"),
        pytest.param(
            "code-remediate", "All / Required / Suggestions / Custom selection", False, False, id="scope-menu"
        ),
        pytest.param("code-remediate", "The inventory still needs reconciliation.", True, False, id="native-question"),
        pytest.param("code-remediate", "The user denied access.", False, False, id="denial"),
        pytest.param("code-review", "The inventory still needs reconciliation.", False, False, id="other-workflow"),
    ],
)
def test_awaiting_selection_status_recovers_only_the_missing_remediation_question(
    review_state, answered_question, skill: str, ending: str, native_pending: bool, recover: bool
) -> None:
    """Selection status can recover the inventory and question without selecting or implementing any finding."""
    payload, records = review_state
    payload["last_assistant_message"] = f"The findings are awaiting selection.\n\n{ending}"
    records[2:] = [create_run_record(skill), *(answered_question[:1] if native_pending else [])]
    response = HOOK.evaluate(payload, records, "/plugin")
    assert response.get("decision") == ("block" if recover else None)
    assert ("question delivery only" in response.get("reason", "")) is recover
    assert ("inventory" in response.get("reason", "")) is recover
    assert "selected fixes" not in response.get("reason", "")


@pytest.mark.parametrize(
    ("question", "options", "answer", "recover"),
    [
        pytest.param("Continue this review?", ["Continue", "Skip"], "Continue", True, id="unrelated-entry-answer"),
        pytest.param(
            "Which findings should I remediate?",
            ["All", "Required", "Suggestions", "Custom selection"],
            "Required",
            False,
            id="scope-already-selected",
        ),
    ],
)
def test_missing_selection_question_respects_what_prior_control_resolved(
    review_state, answered_question, question: str, options: list[str], answer: str, recover: bool
) -> None:
    """Distinguish a submitted selection from an unrelated completed decision."""
    payload, records = review_state
    payload["last_assistant_message"] = "The findings are awaiting selection."
    records[2] = create_run_record("code-remediate")
    item = answered_question[1]["payload"]["item"]
    item["arguments"].update(question=question, options=options)
    item["result"]["structuredContent"]["answer"] = answer
    response = HOOK.evaluate(payload, [*records, *answered_question], "/plugin")
    assert ("question delivery only" in response.get("reason", "")) is recover
    assert response.get("decision") == ("block" if recover else None)


@pytest.mark.parametrize(
    "message",
    [
        pytest.param(
            "Remediation remains incomplete; zero implemented. First unmet checkpoint: hosted PR metadata fetch timed "
            "out. 3 confirmed findings awaiting selection.",
            id="documented-blocked-handoff",
        ),
        pytest.param(
            "Stopped before selection. Remediation remains incomplete; zero implemented. "
            "3 confirmed findings awaiting selection.",
            id="documented-handoff-without-blocker-words",
        ),
        pytest.param(
            "First unmet checkpoint: PR metadata fetch. 3 confirmed findings awaiting selection.",
            id="named-unmet-checkpoint",
        ),
        pytest.param(
            "Stopped at your request before selection. 3 confirmed findings awaiting selection.",
            id="user-requested-stop",
        ),
        pytest.param(
            "Awaiting selection.\n\nWhich findings should I remediate?\n\nThe inventory is ready.",
            id="question-before-final-paragraph",
        ),
        pytest.param(
            "Awaiting selection.\n\nWhich findings should I remediate? All / Required / Suggestions / Custom selection"
            "\n\nThe inventory is ready.",
            id="inline-menu-before-final-paragraph",
        ),
        pytest.param(
            "Awaiting selection.\n\n- All\n- Required\n- Suggestions\n- Custom selection\n\nThe inventory is ready.",
            id="menu-before-final-paragraph",
        ),
    ],
)
def test_selection_status_stops_on_incomplete_handoff_or_delivered_question(review_state, message: str) -> None:
    """A mandated stop handoff or an already-delivered scope question keeps the turn stopped."""
    payload, records = review_state
    payload["last_assistant_message"] = message
    records[2] = create_run_record("code-remediate")
    assert HOOK.evaluate(payload, records, "/plugin") == {}


@pytest.mark.parametrize("completed", [False, True])
@pytest.mark.parametrize(
    "status",
    [
        "I am waiting for your approval before running the command.",
        "Pending user decision. The dependent checks are paused.",
    ],
)
def test_concrete_missing_approval_status_reopens_only_delivery_for_active_run(
    review_state, status: str, completed: bool
):
    """Recover two explicit waits while honoring a successful completion of the same review."""
    payload, records = review_state
    payload["last_assistant_message"] = status
    if completed:
        records.append(
            command_record(
                '"/plugin/bin/python" "/plugin/shared/find-review-report.py" --complete-run .reports/codex/code-review/run-a'
            )
        )
    result = HOOK.evaluate(payload, records, "/plugin")
    if completed:
        assert result == {}
    else:
        assert result["decision"] == "block"
        assert "question delivery only" in result["reason"]
        assert "explicit answer" in result["reason"]


def test_answer_to_review_entry_does_not_resolve_later_async_question(review_state, answered_question, async_question):
    """An answered entry decision belongs to the previous control, even in the same turn."""
    payload, records = review_state
    entry = answered_question[-1]["payload"]["item"]
    entry["arguments"]["options"] = ["Resume last", "Start fresh"]
    entry["result"]["structuredContent"]["answer"] = "Start fresh"
    records[2:2] = answered_question
    records.extend(async_question)
    payload["last_assistant_message"] = "The check awaits your decision."
    assert "question delivery only" in HOOK.evaluate(payload, records, "/plugin")["reason"]


@pytest.mark.parametrize("state", ["unaccepted", "mismatched", "answered", "already-recovered", "unknown-extension"])
def test_uncertain_async_or_real_stop_never_reopens_decision(review_state, async_question, state: str) -> None:
    """A live form, unrelated receipt, reply, or unknown host state never requests delivery recovery."""
    payload, records = review_state
    payload["last_assistant_message"] = "The check awaits your decision."
    if state == "unaccepted":
        async_question.pop()
    if state == "mismatched":
        async_question[-1]["payload"]["call_id"] = "another-call"
    records.extend(async_question)
    if state == "answered":
        records.append({"type": "response_item", "payload": {"type": "message", "role": "user"}})
    if state == "already-recovered":
        payload["stop_hook_active"] = True
    if state == "unknown-extension":
        records.append(
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "thread_id": "session",
                    "turn_id": "turn",
                    "item": {"type": "Extension", "kind": "unknown"},
                },
            }
        )
    assert HOOK.evaluate(payload, records, "/plugin") == {}


@pytest.mark.parametrize("same_run", [True, False])
@pytest.mark.parametrize("exit_code", [0, 1])
@pytest.mark.parametrize(
    ("prefix", "suffix"),
    [
        pytest.param("", "", id="bare"),
        pytest.param("", " --codex-home /codex", id="codex-home"),
        pytest.param("", " --parent-thread-id parent", id="parent-thread"),
        pytest.param("", " --codex-home /codex --parent-thread-id parent", id="both-context-options"),
        pytest.param("--parent-thread-id parent --codex-home /codex ", "", id="context-before-completion"),
        pytest.param("", " --parent-thread-id=parent --codex-home=/codex", id="equals-context-options"),
    ],
)
def test_completion_must_name_the_active_review(
    review_state, same_run: bool, exit_code: int, prefix: str, suffix: str
) -> None:
    """Another review's successful completion cannot close this turn's review.

    The completion command validates one run directory; it releases the stop only when it names the run this turn
    created.
    """
    payload, records = review_state
    run = "run-a" if same_run else "run-b"
    records.append(
        command_record(
            f'"/plugin/bin/python" "/plugin/shared/find-review-report.py" {prefix}--complete-run '
            f'"/workspace/.reports/codex/code-review/{run}"{suffix}',
            "Validated final bytes",
            exit_code,
        )
    )

    assert ("decision" not in HOOK.evaluate(payload, records, "/plugin")) is (same_run and exit_code == 0)


@pytest.mark.parametrize(
    "options",
    [
        "--help",
        "--unknown value",
        "--codex-home",
        "--codex-home=",
        "--parent-thread-id --help",
        "--codex-home /first --codex-home /second",
        "--complete-run /workspace/.reports/codex/code-review/run-b",
    ],
)
def test_invalid_completion_arguments_leave_review_active(review_state, options: str) -> None:
    """A help-only or unrecognized invocation cannot stand in for report validation."""
    payload, records = review_state
    records.append(
        command_record(
            '"/plugin/bin/python" "/plugin/shared/find-review-report.py" '
            f"--complete-run /workspace/.reports/codex/code-review/run-a {options}"
        )
    )

    assert HOOK.evaluate(payload, records, "/plugin")["decision"] == "block"


@pytest.mark.parametrize("separator", [" ", "="])
@pytest.mark.parametrize("quote", ['"', "'"])
def test_windows_completion_accepts_context_options(review_state, separator: str, quote: str) -> None:
    """Native PowerShell context options retain the completed review's Windows identity."""
    payload, records = review_state
    payload["cwd"] = "C:\\workspace"
    records[-1]["payload"]["item"].update(
        cwd="file:///C:/workspace",
        command=[
            "pwsh.exe",
            "-Command",
            '& "C:\\plugin\\bin\\python.cmd" "C:\\plugin\\shared\\create_run.py" --skill code-review',
        ],
    )
    completed = command_record("")
    completed["payload"]["item"].update(
        cwd="file:///C:/workspace",
        command=[
            "pwsh.exe",
            "-Command",
            '& "C:\\plugin\\bin\\python.cmd" "C:\\plugin\\shared\\find-review-report.py" '
            '--parent-thread-id parent --complete-run "C:\\workspace\\.reports\\codex\\code-review\\run-a" '
            f"--codex-home{separator}{quote}C:\\codex home{quote}",
        ],
    )
    records.append(completed)

    assert HOOK.evaluate(payload, records, "C:\\plugin") == {}


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


@pytest.mark.parametrize(
    "identity",
    [
        "--pr-number 41",
        "--pr-number 41 --pr-url https://github.com/acme/widget/pull/41",
        '--branch "feature/topic"',
        "--pr-number=41",
    ],
)
def test_identity_creation_promotion_and_completion_are_tracked(review_state, identity: str) -> None:
    """Track identifiable creation through promotion and clear only its promoted completion."""
    payload, records = review_state
    source = ".reports/codex/code-review/2026-01-02T03-04-05-000000Z-pr-41-random"
    destination = ".reports/codex/code-review/pr-41/run-001"
    records[2] = command_record(
        f'"/plugin/bin/python" "/plugin/shared/create_run.py" --skill code-review {identity}', source + "\n"
    )
    assert HOOK.evaluate(payload, records, "/plugin")["decision"] == "block"
    records.append(
        command_record(
            f'"/plugin/bin/python" "/plugin/shared/create_run.py" --skill code-review --promote-pr-run {source}',
            destination + "\n",
        )
    )
    assert HOOK.evaluate(payload, records, "/plugin")["decision"] == "block"
    records.append(
        command_record(f'"/plugin/bin/python" "/plugin/shared/find-review-report.py" --complete-run {destination}')
    )
    assert HOOK.evaluate(payload, records, "/plugin") == {}


@pytest.mark.parametrize(
    "identity",
    [
        "--branch feature --local-scope working-tree",
        "--branch topic --local-scope path --local-target src",
        "--branch main --local-scope commit --local-target HEAD^!",
    ],
)
def test_fresh_local_entry_is_tracked_through_handoff(review_state, identity: str) -> None:
    """Recording a cross-session review identity still registers the new local run."""
    payload, records = review_state
    run = ".reports/codex/code-review/local"
    records[-1] = command_record(
        f'"/plugin/bin/python" "/plugin/shared/create_run.py" --skill code-review {identity}', run
    )
    assert HOOK.evaluate(payload, records, "/plugin")["decision"] == "block"
    records.append(command_record(f'"/plugin/bin/python" "/plugin/shared/find-review-report.py" --complete-run {run}'))
    assert HOOK.evaluate(payload, records, "/plugin") == {}


@pytest.mark.parametrize(
    "identity",
    [
        "--local-target src",
        "--local-scope path",
        "--branch main --local-scope commit",
        "--branch main --local-scope working-tree --local-target src",
        "--pr-number 0",
        "--pr-number nope",
        "--pr-number 41 --pr-number 42",
        '--branch ""',
        "--branch topic --branch other",
        "--branch topic --pr-number 41",
        "--unknown 41",
        "--pr-number 41 --promote-pr-run .reports/codex/code-review/run-a",
    ],
)
def test_invalid_identity_command_does_not_start_workflow(review_state, identity: str) -> None:
    """Ignore malformed, duplicated and mixed-mode commands even with an exit-zero host receipt."""
    payload, records = review_state
    records[2] = command_record(
        f'"/plugin/bin/python" "/plugin/shared/create_run.py" --skill code-review {identity}',
        ".reports/codex/code-review/run-a\n",
    )
    assert HOOK.evaluate(payload, records, "/plugin") == {}


@pytest.fixture
def answered_question() -> list[dict]:
    """Provide a nested native question and its host-owned, identity-bound answer receipt."""
    arguments = {
        "decision_id": "review-recovery",
        "scope_digest": "a" * 64,
        "question": "Continue this review?",
        "options": ["Continue", "Skip"],
    }
    return [
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "input": "await tools.mcp__codex_rig_input__ask_user({});",
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "thread_id": "session",
                "turn_id": "turn",
                "item": {
                    "type": "McpToolCall",
                    "id": "question-call",
                    "server": "codex-rig-input",
                    "tool": "ask_user",
                    "pluginId": "codex-rig@borda-ai-rig",
                    "arguments": arguments,
                    "status": "completed",
                    "result": {
                        "structuredContent": {
                            "decision_id": "review-recovery",
                            "scope_digest": "a" * 64,
                            "status": "answered",
                            "answer": "Continue",
                        }
                    },
                },
            },
        },
    ]


@pytest.mark.parametrize(
    "source",
    [
        "text(ALL_TOOLS.filter(x=>/ask_user/.test(x.name)));",
        'text(ALL_TOOLS.filter(x=>x.name.includes("ask_user")))',
        'const r = await tools.exec_command({cmd:"record ask_user, pending"}); text(r.output)',
        "text(42);text(ALL_TOOLS.filter(x=>/ask_user|request_user_input/.test(x.name)));",
        "const note=String.raw`The native ask_user control is available.`;text(note);",
    ],
)
def test_question_mentions_do_not_pause_review(review_state, source: str) -> None:
    """Discovery and descriptive tool mentions in a batch leave the active run eligible for recovery."""
    payload, records = review_state
    records.append({"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "input": source}})
    assert HOOK.evaluate(payload, records, "/plugin")["decision"] == "block"


@pytest.mark.parametrize(
    ("question", "options", "answer", "skill", "recover"),
    [
        pytest.param(
            "Which findings should I remediate? All selects both items.",
            ["All", "Required", "Suggestions", "Custom selection"],
            "All",
            "code-remediate",
            True,
            id="scope",
        ),
        pytest.param(
            "How should verified remediation-owned changes be grouped into commits? A standing grant exists.",
            [
                "all at once",
                "group findings by topic",
                "each finding as a separate commit",
                "decide after verification",
                "leave unstaged",
            ],
            "all at once",
            "code-remediate",
            True,
            id="commit-grouping",
        ),
        pytest.param(
            "Which findings should I remediate?",
            ["All", "Required", "Suggestions", "Custom selection"],
            "All",
            "code-review",
            False,
            id="read-only-owner",
        ),
        pytest.param(
            "Which findings should I remediate?",
            ["All", "Required", "Suggestions", "Custom selection"],
            "Custom selection",
            "code-remediate",
            False,
            id="indexes-still-needed",
        ),
        pytest.param(
            "Which findings should I remediate?",
            ["All", "Skip"],
            "All",
            "code-remediate",
            False,
            id="unknown-menu",
        ),
        pytest.param(
            "Approve all changes?",
            ["All", "Required", "Suggestions", "Custom selection"],
            "All",
            "code-remediate",
            False,
            id="different-purpose",
        ),
    ],
)
def test_remediation_answers_restore_only_their_own_workflow(
    review_state, answered_question, question: str, options: list[str], answer: str, skill: str, recover: bool
) -> None:
    """Only a completed native decision from an active remediation form permits continued work within its scope."""
    payload, records = review_state
    records[2] = create_run_record(skill)
    item = answered_question[1]["payload"]["item"]
    item["arguments"].update(question=question, options=options)
    item["result"]["structuredContent"]["answer"] = answer
    records.extend(answered_question)
    response = HOOK.evaluate(payload, records, "/plugin")
    assert response.get("decision") == ("block" if recover else None)


@pytest.mark.parametrize(
    ("text", "accepted"),
    [
        pytest.param(
            "PR acme/widgets#12 — Keep fractional positions\n\nWhich findings should I remediate?",
            True,
            id="with-title",
        ),
        pytest.param("PR acme/widgets#12\n\nWhich findings should I remediate?", True, id="before-collection"),
        pytest.param(
            "Remediate PR acme/widgets#12 — Keep labels\n\nWhich findings should I remediate?",
            True,
            id="remediation-action",
        ),
        pytest.param(
            "PR acme/widgets#12 — Which findings should I remediate?\n\nWork is pending.", False, id="question-in-title"
        ),
        pytest.param(
            "A previous question was recorded.\n\nWhich findings should I remediate?", False, id="unrelated-preamble"
        ),
        pytest.param(
            "PR acme/widgets#12\n\nPR acme/widgets#12 — Again\n\nWhich findings should I remediate?",
            False,
            id="duplicate-prefix",
        ),
        pytest.param("PR acme/widgets#12 — Title\nWhich findings should I remediate?", False, id="missing-separator"),
    ],
)
def test_native_scope_recognizes_only_one_pr_subject(
    review_state, answered_question, text: str, accepted: bool
) -> None:
    """A display subject can precede the existing question while the answer stays bound to its native receipt."""
    payload, records = review_state
    records[2] = create_run_record("code-remediate")
    item = answered_question[1]["payload"]["item"]
    item["arguments"].update(question=text, options=["All", "Required", "Suggestions", "Custom selection"])
    item["result"]["structuredContent"]["answer"] = "Required"
    assert HOOK.evaluate(payload, [*records, *answered_question], "/plugin").get("decision") == (
        "block" if accepted else None
    )


@pytest.mark.parametrize(
    ("subject", "body", "expected"),
    [
        pytest.param(
            "PR acme/widgets#12 — Awaiting your decision",
            "Implementation remains unfinished.",
            "block",
            id="title-is-data",
        ),
        pytest.param(
            "PR acme/widgets#12 — Which findings should I remediate?",
            "Implementation remains unfinished.",
            "block",
            id="question-only-in-title",
        ),
        pytest.param(
            "PR acme/widgets#12 — Save widget state", "Which findings should I remediate?", None, id="pending-question"
        ),
        pytest.param(
            "Remediate PR acme/widgets#12 — Handle denied requests",
            "Implementation remains unfinished.",
            "block",
            id="action-title-with-denial",
        ),
        pytest.param("PR acme/widgets#12", "The user denied the operation.", None, id="body-denial"),
    ],
)
def test_terminal_pr_subject_preserves_question_and_pause_semantics(
    review_state, subject: str, body: str, expected: str | None
) -> None:
    """Only the terminal message body indicates a decision or controls further remediation."""
    payload, records = review_state
    records[2] = create_run_record("code-remediate")
    payload["last_assistant_message"] = f"{subject}\n\n{body}"
    result = HOOK.evaluate(payload, records, "/plugin")
    assert result.get("decision") == expected
    assert ("question delivery only" in result.get("reason", "")) is False


@pytest.mark.parametrize(
    "menu",
    [
        pytest.param(
            "Which findings should I remediate?\n\n- All\n- Required\n- Suggestions\n- Custom selection", id="scope"
        ),
        pytest.param(
            "How should verified remediation-owned changes be grouped into commits?\n\n"
            "- all at once\n- group findings by topic\n- each finding as a separate commit\n"
            "- decide after verification\n- leave unstaged",
            id="grouping",
        ),
        pytest.param("All / Required / Suggestions / Custom selection", id="inline-scope"),
        pytest.param("Which finding indexes should I remediate?", id="custom-scope-question"),
        pytest.param(
            "Approve = this commit only.\nApprove always = standing grant.\nHold = leave unstaged.", id="plain-commit"
        ),
    ],
)
def test_new_terminal_menu_after_answer_keeps_remediation_paused(review_state, answered_question, menu: str) -> None:
    """An earlier answered native decision does not settle a later plain-chat menu."""
    payload, records = review_state
    payload["last_assistant_message"] = menu
    records[2] = create_run_record("code-remediate")
    records.extend(answered_question)
    assert HOOK.evaluate(payload, records, "/plugin") == {}


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        pytest.param("Continue this review?", "block", id="historical-question"),
        pytest.param("Review PR acme/widgets#12 — Keep labels\n\nContinue this review?", "block", id="review-action"),
        pytest.param("PR acme/widgets#12 — Triage workflow\n\nContinue this review?", "block", id="display-subject"),
        pytest.param("PR acme/widgets#12\n\nPR acme/widgets#12\n\nContinue this review?", None, id="duplicate-subject"),
    ],
)
def test_answered_continue_restores_review_recovery(
    review_state, answered_question, question: str, expected: str | None
) -> None:
    """A completed native Continue answer with a single optional PR subject allows unfinished review recovery."""
    payload, records = review_state
    answered_question[1]["payload"]["item"]["arguments"]["question"] = question
    records.extend(answered_question)
    assert HOOK.evaluate(payload, records, "/plugin").get("decision") == expected


@pytest.fixture
def review_recovery_question(answered_question) -> list[dict]:
    """Supply the host receipts for the documented three-option review decision."""
    item = answered_question[1]["payload"]["item"]
    item["arguments"].update(
        question="How should we continue the review of https://github.com/acme/widgets/pull/12?",
        options=["New review", "Resume last one", "Stop"],
    )
    item["result"]["structuredContent"]["answer"] = "New review"
    return answered_question


@pytest.mark.parametrize(
    ("answer", "decision", "after_original_completion"),
    [
        pytest.param("New review", "block", "block", id="start-fresh"),
        pytest.param("Resume last one", "block", None, id="resume-retained"),
        pytest.param("Stop", None, None, id="stop-review"),
    ],
)
def test_incomplete_review_decision_respects_native_choice(
    review_state, review_recovery_question, answer: str, decision: str | None, after_original_completion: str | None
) -> None:
    """The native recovery menu reopens a review only when the user selects a continuing route."""
    payload, records = review_state
    item = review_recovery_question[1]["payload"]["item"]
    item["result"]["structuredContent"]["answer"] = answer
    active = [*records, *review_recovery_question]
    assert HOOK.evaluate(payload, active, "/plugin").get("decision") == decision
    finish_original = command_record(
        '"/plugin/bin/python" "/plugin/shared/find-review-report.py" --complete-run .reports/codex/code-review/run-a'
    )
    assert HOOK.evaluate(payload, [*active, finish_original], "/plugin").get("decision") == after_original_completion
    promote_original = command_record(
        '"/plugin/bin/python" "/plugin/shared/create_run.py" --skill code-review '
        "--promote-pr-run .reports/codex/code-review/run-a",
        ".reports/codex/code-review/pr-12/run-001",
    )
    finish_promoted = command_record(
        '"/plugin/bin/python" "/plugin/shared/find-review-report.py" --complete-run .reports/codex/code-review/pr-12/run-001'
    )
    result = HOOK.evaluate(payload, [*active, promote_original, finish_promoted], "/plugin")
    assert result.get("decision") == after_original_completion


@pytest.mark.parametrize("nested", [False, True])
def test_new_review_completes_with_replacement_run(review_state, review_recovery_question, nested: bool) -> None:
    """Choosing a new review retires the old run when its replacement is allocated and completed."""
    payload, records = review_state
    base = ".reports/codex/code-review"
    allocator = '"/plugin/bin/python" "/plugin/shared/create_run.py" --skill code-review'
    records[2] = command_record(f"{allocator} --pr-number 12", f"{base}/run-a")
    records[2:2] = [create_run_record("code-remediate")] if nested else []
    allocated = [*records, *review_recovery_question, command_record(f"{allocator} --pr-number 12", f"{base}/run-b")]
    assert HOOK.evaluate(payload, allocated, "/plugin")["decision"] == "block"
    run = f"{base}/pr-12/run-002"
    promoted = command_record(f"{allocator} --promote-pr-run {base}/run-b", run)
    finish = command_record(f'"/plugin/bin/python" "/plugin/shared/find-review-report.py" --complete-run {run}')
    completed = HOOK.evaluate(payload, [*allocated, promoted, finish], "/plugin")
    assert completed.get("decision") == ("block" if nested else None)
    assert ("Continue remediation selection" in completed.get("reason", "")) is nested


@pytest.mark.parametrize("answer", ["Skip", "Stop", "Deny", "", "continue"])
def test_other_answers_preserve_pause(review_state, answered_question, answer: str) -> None:
    """Only the exact offered continuation answer can release this advisory question pause."""
    payload, records = review_state
    answered_question[1]["payload"]["item"]["result"]["structuredContent"]["answer"] = answer
    records.extend(answered_question)
    assert HOOK.evaluate(payload, records, "/plugin") == {}


@pytest.mark.parametrize("field", ["decision_id", "scope_digest", "status"])
def test_mismatched_question_receipt_preserves_pause(review_state, answered_question, field: str) -> None:
    """An unrelated or incomplete receipt cannot clear a pending question."""
    payload, records = review_state
    answered_question[1]["payload"]["item"]["result"]["structuredContent"][field] = "unrelated"
    records.extend(answered_question)
    assert HOOK.evaluate(payload, records, "/plugin") == {}


def test_unanswered_nested_question_preserves_pause(review_state, answered_question) -> None:
    """No host completion receipt means the native question is still unresolved."""
    payload, records = review_state
    records.append(answered_question[0])
    assert HOOK.evaluate(payload, records, "/plugin") == {}


def test_one_answer_cannot_close_two_questions(review_state, answered_question) -> None:
    """One completed answer cannot stand for another still-pending invocation."""
    payload, records = review_state
    records.extend([answered_question[0], *answered_question])
    assert HOOK.evaluate(payload, records, "/plugin") == {}


def test_duplicate_answer_cannot_close_pending_question(review_state, answered_question) -> None:
    """Replayed receipt IDs must not consume a second outstanding question."""
    payload, records = review_state
    records.extend([*answered_question, *answered_question])
    assert HOOK.evaluate(payload, records, "/plugin") == {}


def test_continue_must_be_an_offered_choice(review_state, answered_question) -> None:
    """A free-text answer or changed choices cannot claim the supported Continue decision."""
    payload, records = review_state
    answered_question[1]["payload"]["item"]["arguments"]["options"] = ["Stop"]
    records.extend(answered_question)
    assert HOOK.evaluate(payload, records, "/plugin") == {}


@pytest.mark.parametrize("scope", ["invalid", "a" * 64])
def test_scope_error_is_recoverable_only_before_form_creation(review_state, answered_question, scope: str) -> None:
    """Only independently invalid scope plus its exact validation error proves no form opened."""
    payload, records = review_state
    item = answered_question[1]["payload"]["item"]
    item.update(
        status="failed",
        error={"message": "Mcp error: -32602: scope_digest must be 64 lowercase hexadecimal characters"},
    )
    item.pop("result")
    item["arguments"]["scope_digest"] = scope
    records.extend(answered_question)
    assert (HOOK.evaluate(payload, records, "/plugin").get("decision") == "block") is (scope == "invalid")
    records[2] = create_run_record("code-remediate")
    payload["last_assistant_message"] = "Findings are awaiting selection."
    assert ("question delivery only" in HOOK.evaluate(payload, records, "/plugin").get("reason", "")) is (
        scope == "invalid"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("server", "other", id="other-server"),
        pytest.param("pluginId", "other", id="other-plugin"),
        pytest.param("tool", "other", id="other-tool"),
        pytest.param("id", "", id="missing-receipt-id"),
        pytest.param("status", "pending", id="pending"),
    ],
)
def test_unknown_question_receipt_preserves_pause(review_state, answered_question, field: str, value: str) -> None:
    """Only the supported host tool and completed receipt can clear its outstanding invocation."""
    payload, records = review_state
    answered_question[1]["payload"]["item"][field] = value
    records.extend(answered_question)
    assert HOOK.evaluate(payload, records, "/plugin") == {}


@pytest.mark.parametrize(
    "source",
    [
        "const ask = tools.mcp__codex_rig_input__ask_user; ask({});",
        "await tools.mcp__other__ask_user({});",
        "await tools.request_user_input({});",
        'await tools["mcp__codex_rig_input__ask_user"]({});',
        "await tools?.mcp__codex_rig_input__ask_user({});",
        "const {mcp__codex_rig_input__ask_user: ask} = tools; await ask({});",
        "const ask = tools . mcp__codex_rig_input__ask_user; ask({});",
        "text(ALL_TOOLS.filter(x=>/ask_user/.test(x.name)));await tools.mcp__codex_rig_input__ask_user({});",
        'text(ALL_TOOLS.filter(x=>/ask_user/.test(x.name)));await tools["ask_user"]({});',
        'text(ALL_TOOLS.filter(x=>x.name.includes("ask_user")));await tools.mcp__codex_rig_input__ask_user({});',
        'const r = await tools.exec_command({cmd:"record ask_user, pending"}); text(r.output);await ask_user({});',
        "const r = await tools.exec_command({cmd:`record ${await ask_user({})}`}); text(r.output);",
        "const lookup = ask_user; await lookup({});",
        "await ask_user({});",
    ],
)
def test_unknown_nested_question_routes_preserve_pause(review_state, source: str) -> None:
    """Aliased or other question routes cannot be mistaken for a completed supported question."""
    payload, records = review_state
    records.append({"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "input": source}})
    assert HOOK.evaluate(payload, records, "/plugin") == {}


@pytest.mark.parametrize("platform", PLATFORM_CASES)
@pytest.mark.parametrize("tail_exit", [0, 1])
@pytest.mark.parametrize("git_prefix", ["git", "rtk git"])
def test_batched_creation_and_promotion_remain_active(
    review_state, platform: dict, tail_exit: int, git_prefix: str
) -> None:
    """Read-only suffix commands cannot hide an unfinished promoted remediation run."""
    payload, records = review_state
    payload["cwd"] = platform["cwd"]
    launcher = f'{platform["prefix"]}"{platform["root"]}/bin/{platform["launcher"]}"'
    helper = f'{launcher} "{platform["root"]}/shared/create_run.py" --skill code-remediate'
    creation = command_record("")
    creation["payload"]["item"].update(
        command=[
            *platform["shell"],
            helper
            + f'\n{launcher} "{platform["root"]}/shared/find-review-report.py" --result "{platform["cwd"]}/.reports/codex/code-review/pr-7/run-001/result.json"',
        ],
        cwd=platform["item_cwd"],
        aggregated_output=".reports/codex/code-remediate/run-a\n.reports/codex/code-review/pr-7/run-001/result.json\n",
    )
    promotion = command_record("")
    promotion["payload"]["item"].update(
        command=[
            *platform["shell"],
            helper
            + f" --promote-pr-run .reports/codex/code-remediate/run-a\n{git_prefix} status\n{git_prefix} status --short\n{git_prefix} rev-parse HEAD",
        ],
        cwd=platform["item_cwd"],
        aggregated_output=".reports/codex/code-remediate/pr-7/run-001\n?? untouched.txt\n" + "a" * 40 + "\n",
    )
    creation["payload"]["item"]["exit_code"] = tail_exit
    promotion["payload"]["item"]["exit_code"] = tail_exit
    records[2:] = [creation, promotion]
    assert HOOK.evaluate(payload, records, platform["root"])["decision"] == "block"
    completion = command_record("")
    completion["payload"]["item"].update(
        command=[
            *platform["shell"],
            f'{launcher} "{platform["root"]}/shared/remediation_finalize.py" finalize --run .reports/codex/code-remediate/pr-7/run-001 --promote',
        ],
        cwd=platform["item_cwd"],
    )
    records.append(completion)
    assert HOOK.evaluate(payload, records, platform["root"]) == {}


@pytest.mark.parametrize(
    "suffix",
    [
        "printf .reports/codex/code-review/run-a",
        "echo forged",
        "git status; true",
        "rtk git status; true",
        "rtk git status --short; true",
        "rtk git status --short && true",
        "rtk git rev-parse $REF",
        "rtk git status --short | cat",
    ],
)
def test_unknown_creation_suffix_cannot_activate_review(review_state, suffix: str) -> None:
    """Only the bounded literal read-only suffix vocabulary can accompany run creation."""
    payload, records = review_state
    records[-1]["payload"]["item"]["command"][-1] += "\n" + suffix
    assert HOOK.evaluate(payload, records, "/plugin") == {}


@pytest.mark.parametrize(
    "output",
    [
        "creation failed\n",
        ".reports/codex/code-review/pr-7/run-001/result.json\n",
        "?? .reports/codex/code-review/run-a\n",
        "",
    ],
)
def test_failed_creation_cannot_borrow_tail_success(review_state, output: str) -> None:
    """A successful tail command without the creator's path receipt proves no new run."""
    payload, records = review_state
    item = records[-1]["payload"]["item"]
    item["command"][-1] += (
        '\n"/plugin/bin/python" "/plugin/shared/find-review-report.py" --result .reports/codex/code-review/pr-7/run-001/result.json'
    )
    item["aggregated_output"] = output
    assert HOOK.evaluate(payload, records, "/plugin") == {}


def test_creation_after_another_command_is_not_admitted(review_state) -> None:
    """Output position cannot prove run creation when the helper is not the first command."""
    payload, records = review_state
    item = records[-1]["payload"]["item"]
    item["command"][-1] = "rtk git status --short\n" + item["command"][-1]
    assert HOOK.evaluate(payload, records, "/plugin") == {}
