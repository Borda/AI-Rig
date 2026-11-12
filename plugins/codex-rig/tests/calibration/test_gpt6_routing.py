"""Verify the agreed GPT-6 model and effort assignments in active source routing, and the live runner's guards."""

import io
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from _fixture_files import tree_bytes
from _modules import load_module

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
ROLES = REPOSITORY_ROOT / "plugins" / "codex-rig" / "roles"
EXPECTED = {
    "challenger": ("gpt-6.1-sol", "high"),
    "cicd-steward": ("gpt-6-luna", "high"),
    "curator": ("gpt-6-luna", "high"),
    "data-steward": ("gpt-6.1-sol", "high"),
    "delegation-lead": ("gpt-6-luna", "high"),
    "doc-scribe": ("gpt-6.1-terra", "high"),
    "linting-expert": ("gpt-6-luna", "medium"),
    "oss-shepherd": ("gpt-6-luna", "high"),
    "qa-specialist": ("gpt-6.1-sol", "medium"),
    "scientist": ("gpt-6.1-sol", "high"),
    "security-auditor": ("gpt-6.1-sol", "high"),
    "solution-architect": ("gpt-6.1-sol", "high"),
    "squeezer": ("gpt-6.1-sol", "medium"),
    "sw-engineer": ("gpt-6.1-sol", "medium"),
    "web-explorer": ("gpt-6.1-terra", "medium"),
}
PAID_RUN = ["--confirm-paid-run", "chatgpt-subscription"]
CAPTURED_EVENTS = '{"usage":{"input_tokens":3,"output_tokens":1}}\n'


def test_role_cards_cover_exactly_the_expected_roles() -> None:
    """Every expected role has a role card and no card exists for an unexpected role."""
    assert {path.parent.name for path in ROLES.glob("*/ROLE.md")} == set(EXPECTED)


@pytest.mark.parametrize(
    ("role", "effort"), [pytest.param(role, effort, id=role) for role, (_, effort) in EXPECTED.items()]
)
def test_role_card_inherits_model_and_keeps_effort(role: str, effort: str) -> None:
    """Let the host choose the role model while the role card preserves its reasoning-effort setting.

    A role card that pinned a `model:` would override the host's model choice; the effort line is the only routing value
    a card may carry.
    """
    card = (ROLES / role / "ROLE.md").read_text(encoding="utf-8")
    assert not re.search(r"^model\s*:", card, re.MULTILINE)
    assert re.search(rf"^model_reasoning_effort: {effort}$", card, re.MULTILINE)


@pytest.mark.parametrize(
    "setting",
    [
        pytest.param(r'^model\s*=\s*"gpt-6\.1-sol"$', id="parent-model"),
        pytest.param(r'^review_model\s*=\s*"gpt-6\.1-sol"$', id="review-model"),
        pytest.param(r'^model_reasoning_effort\s*=\s*"medium"$', id="parent-effort"),
    ],
)
def test_codex_config_pins_parent_and_review_defaults(setting: str) -> None:
    """Use Sol medium as the normal parent while retaining a Sol review model."""
    config = (REPOSITORY_ROOT / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert re.search(setting, config, re.MULTILINE)


@pytest.fixture
def live_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Path, Path]:
    """Prepare local CLI inputs for the live A/B runner and prohibit external subprocess execution."""
    calibration = REPOSITORY_ROOT / "plugins" / "codex-rig" / "runtime" / "calibration"
    monkeypatch.syspath_prepend(str(calibration))
    runner = load_module("gpt6_live_runner", calibration / "run_live_ab.py")

    def reject_subprocess(*args: Any, **kwargs: Any) -> None:
        """Fail if CLI validation reaches an external command."""
        pytest.fail("model validation must precede subprocess execution")

    monkeypatch.setattr(runner.subprocess, "run", reject_subprocess)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps({"cases": [{"id": "example"}]}), encoding="utf-8")
    tasks = tmp_path / "tasks.json"
    tasks.write_text(
        json.dumps({"routes": {"selected": [{"case_id": "example", "role": "sw-engineer"}]}}), encoding="utf-8"
    )
    policy = tmp_path / "policy.json"
    output = tmp_path / "output"
    output.mkdir()
    (output / "sentinel.txt").write_bytes(b"existing evidence\n")
    arguments = ["--cases", cases, "--tasks", tasks, "--route-policy", policy, "--out", output, "--route", "selected"]
    monkeypatch.setattr(sys, "argv", ["run_live_ab.py", *map(str, arguments)])
    return runner, policy, output


@pytest.fixture
def windows_host(live_cli: tuple[Any, Path, Path], monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Path]:
    """Simulate a Windows host without POSIX process-group attributes, whatever platform runs the tests."""
    runner, _, output = live_cli
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delattr(runner.os, "killpg", raising=False)
    monkeypatch.delattr(runner.signal, "SIGKILL", raising=False)
    return runner, output


@pytest.fixture
def windows_timeout_capture(windows_host: tuple[Any, Path], monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Path]:
    """Make the simulated Windows child time out once, then drain captured stdout after a successful tree kill."""
    runner, output = windows_host
    process = streaming_timeout_process(runner, monkeypatch, 321, CAPTURED_EVENTS, "detail")
    process.communicate.return_value = (None, None)
    monkeypatch.setattr(runner.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(runner.subprocess, "run", Mock(return_value=subprocess.CompletedProcess("taskkill", 0)))
    return runner, output


def streaming_timeout_process(runner: Any, monkeypatch: pytest.MonkeyPatch, pid: int, stdout: str, stderr: str) -> Mock:
    """Provide finite binary child pipes and an expired deadline without waiting seven real seconds."""
    process = Mock(pid=pid, returncode=1, stdout=io.BytesIO(stdout.encode()), stderr=io.BytesIO(stderr.encode()))
    process.poll.return_value = None
    monkeypatch.setattr(runner.time, "monotonic", Mock(side_effect=[0, 8, 9]))
    return process


@pytest.mark.parametrize(
    "extra_arguments", [pytest.param([], id="plan-only"), pytest.param(PAID_RUN, id="paid-confirmed")]
)
@pytest.mark.parametrize("field", ["baseline_model", "candidate_model"])
@pytest.mark.parametrize(
    "model",
    ["", "-option", "model name", "model\nname", None, pytest.param(["example-model"], id="non-string-model")],
)
def test_live_cli_rejects_invalid_model_identifiers_before_side_effects(
    live_cli: tuple[Any, Path, Path],
    model: Any,
    field: str,
    extra_arguments: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Reject either invalid paired model before planning, authentication, or output writes.

    Both the planning-only and the paid-confirmed invocation must stop on the bad identifier with the existing evidence
    directory byte-for-byte unchanged and nothing printed.
    """
    runner, policy, output = live_cli
    route = {"baseline_model": "gpt-6.1-sol", "candidate_model": "gpt-6-luna", "min_campaigns": 1, field: model}
    policy.write_text(json.dumps({"routes": {"selected": route}}), encoding="utf-8")
    sys.argv.extend(extra_arguments)
    before = tree_bytes(output)

    with pytest.raises(ValueError, match=rf"invalid live model identifier: selected:{field}"):
        runner.main()

    assert capsys.readouterr().out == ""
    assert tree_bytes(output) == before
    assert sorted(path.name for path in output.iterdir()) == ["sentinel.txt"]


def test_live_timeout_cleans_windows_tree_without_posix_apis(
    windows_host: tuple[Any, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Return the timeout evidence after Windows tree cleanup with no POSIX-only attributes.

    A timed-out child is killed with `taskkill /T /F`, the pipes are drained, and exit 124 carries the partial output
    even though `os.killpg` and `signal.SIGKILL` do not exist on the simulated host.
    """
    runner, _ = windows_host
    process = streaming_timeout_process(runner, monkeypatch, 123, "partial output", "detail")
    process.communicate.return_value = (None, None)
    kill_tree = Mock(return_value=subprocess.CompletedProcess("taskkill", 0))
    monkeypatch.setattr(runner.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(runner.subprocess, "run", kill_tree)

    result = runner._run_bounded_process(["example"], None, 7)

    assert result.exit_code == 124
    assert result.stdout == "partial output"
    assert result.stderr == "detail\ntimeout after 7 seconds\n"
    kill_tree.assert_called_once_with(
        ["taskkill", "/PID", "123", "/T", "/F"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=2,
        check=False,
    )
    assert [call.kwargs["timeout"] for call in process.communicate.call_args_list] == [2]
    process.kill.assert_not_called()


@pytest.mark.parametrize(
    "tree_kill",
    [
        pytest.param({"return_value": subprocess.CompletedProcess("taskkill", 1)}, id="taskkill-nonzero"),
        pytest.param({"side_effect": OSError("taskkill missing")}, id="taskkill-unavailable"),
        pytest.param({"side_effect": subprocess.TimeoutExpired("taskkill", 2)}, id="taskkill-timeout"),
    ],
)
def test_live_timeout_reports_failed_windows_tree_cleanup_and_bounds_drain(
    windows_host: tuple[Any, Path], monkeypatch: pytest.MonkeyPatch, tree_kill: dict[str, Any]
) -> None:
    """Keep captured bytes and exit 124 when tree cleanup and final pipe draining fail.

    When `taskkill` fails three different ways and the pipes still do not drain, the runner must say the cleanup is
    unproven, kill the leader directly, and never block on a pipe that cannot close.
    """
    runner, _ = windows_host
    process = streaming_timeout_process(runner, monkeypatch, 456, "captured", "original detail")
    stdout_pipe, stderr_pipe = process.stdout, process.stderr
    process.communicate.side_effect = [
        subprocess.TimeoutExpired("example", 2),
        subprocess.TimeoutExpired("example", 2),
    ]
    kill_tree = Mock(**tree_kill)
    monkeypatch.setattr(runner.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(runner.subprocess, "run", kill_tree)

    result = runner._run_bounded_process(["example"], None, 7)

    assert result.exit_code == 124
    assert result.stdout == "captured"
    assert result.stderr.startswith("original detail\ntimeout after 7 seconds\n")
    assert "process-tree cleanup unproven" in result.stderr
    assert "output drain incomplete" in result.stderr
    assert [call.kwargs["timeout"] for call in process.communicate.call_args_list] == [2, 2]
    process.kill.assert_called_once_with()
    assert stdout_pipe.closed
    assert stderr_pipe.closed
    assert kill_tree.call_args.kwargs["timeout"] == 2


@pytest.mark.parametrize(
    ("later_responses", "killed_signals", "drain_timeouts"),
    [
        pytest.param([], [(789, 15)], [2], id="soft-kill-exits-group"),
        pytest.param([subprocess.TimeoutExpired("example", 2)], [(789, 15), (789, 9)], [2, 2], id="hard-kill"),
    ],
)
def test_live_timeout_preserves_posix_soft_then_hard_group_cleanup(
    live_cli: tuple[Any, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    later_responses: list[Any],
    killed_signals: list[tuple[int, int]],
    drain_timeouts: list[int],
) -> None:
    """Retain group-wide POSIX escalation and bounded output collection on every host.

    SIGTERM goes to the whole process group first; only a group that still holds the pipes open after the bounded drain
    is escalated to SIGKILL.
    """
    runner, _, _ = live_cli
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(runner.signal, "SIGTERM", 15, raising=False)
    monkeypatch.setattr(runner.signal, "SIGKILL", 9, raising=False)
    kill_group = Mock()
    monkeypatch.setattr(runner.os, "killpg", kill_group, raising=False)
    process = streaming_timeout_process(runner, monkeypatch, 789, "posix output", "")
    process.communicate.side_effect = [*later_responses, (None, None)]
    monkeypatch.setattr(runner.subprocess, "Popen", Mock(return_value=process))

    result = runner._run_bounded_process(["example"], None, 7)

    assert result.exit_code == 124
    assert result.stdout == "posix output"
    assert result.stderr == "\ntimeout after 7 seconds\n"
    assert [call.args for call in kill_group.call_args_list] == killed_signals
    assert [call.kwargs["timeout"] for call in process.communicate.call_args_list] == drain_timeouts


class TestWindowsTimeoutEvidence:
    """Timeout evidence reaches the model and gate consumers after Windows tree cleanup."""

    def test_model_observations_survive(self, windows_timeout_capture: tuple[Any, Path]) -> None:
        """Keep model usage, event log and stderr evidence usable after a Windows timeout cleanup."""
        runner, output = windows_timeout_capture

        response, usage, _, exit_code = runner._run_model(
            runner.ModelCallRequest(output, output, "example", 7, "prompt", "gpt-6.1-sol", "medium", "read-only")
        )

        assert exit_code == 124
        assert response == {}
        assert usage == {"input_tokens": 3, "cached_input_tokens": 0, "output_tokens": 1}
        assert (output / "example.events.jsonl").read_text(encoding="utf-8") == CAPTURED_EVENTS
        assert "detail\ntimeout after 7 seconds\n" in (output / "example.stderr.txt").read_text(encoding="utf-8")

    def test_gate_log_survives(self, windows_timeout_capture: tuple[Any, Path]) -> None:
        """Keep the executable gate log with exit 124, captured stdout and the timeout line after a Windows cleanup."""
        runner, output = windows_timeout_capture

        exit_code = runner._run_gate(runner.GateRunRequest(output, output, "example", 7, "example"))

        evidence = (output / "example.gate.txt").read_text(encoding="utf-8")
        assert exit_code == 124
        assert "exit_code=124" in evidence
        assert CAPTURED_EVENTS in evidence
        assert "detail\ntimeout after 7 seconds\n" in evidence


@pytest.mark.parametrize("model", ["gpt-6-luna", "gpt-5.6-terra", "custom-model", "provider/model:latest"])
def test_live_cli_plans_valid_selected_route_with_unselected_archive(
    live_cli: tuple[Any, Path, Path], model: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Accept host-selected model identifiers without reactivating unrelated historical routes.

    Only the selected route is validated and planned; an archived route holding retired model names must neither fail
    validation nor add paid calls, and the evidence directory stays untouched.
    """
    runner, policy, output = live_cli
    archived = {"baseline_model": "gpt-5.6-terra", "candidate_model": "gpt-5.6-luna"}
    selected = {"baseline_model": model, "candidate_model": model, "min_campaigns": 2}
    policy.write_text(json.dumps({"routes": {"selected": selected, "archived": archived}}), encoding="utf-8")

    assert runner.main() == 0

    plan = json.loads(capsys.readouterr().out)
    assert plan["status"] == "planned"
    assert plan["routes"] == ["selected"]
    assert plan["paid_model_calls"] == 4
    assert sorted(path.name for path in output.iterdir()) == ["sentinel.txt"]
