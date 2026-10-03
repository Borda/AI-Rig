"""Verify the agreed GPT-6 model and effort assignments in active source routing."""

import json
import importlib.util
from pathlib import Path
import re
import sys
from typing import Any
from unittest.mock import Mock

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
ROLES = REPOSITORY_ROOT / "plugins" / "codex-rig" / "roles"
EXPECTED = {
    "challenger": ("gpt-6.1-sol", "high"),
    "cicd-steward": ("gpt-6-luna", "high"),
    "curator": ("gpt-6-luna", "high"),
    "data-steward": ("gpt-6.1-sol", "high"),
    "delegation-lead": ("gpt-6-luna", "high"),
    "doc-scribe": ("gpt-6-luna", "high"),
    "linting-expert": ("gpt-6-luna", "medium"),
    "oss-shepherd": ("gpt-6-luna", "high"),
    "qa-specialist": ("gpt-6.1-sol", "medium"),
    "scientist": ("gpt-6.1-sol", "high"),
    "security-auditor": ("gpt-6.1-sol", "high"),
    "solution-architect": ("gpt-6.1-sol", "high"),
    "squeezer": ("gpt-6.1-sol", "medium"),
    "sw-engineer": ("gpt-6.1-sol", "medium"),
    "web-explorer": ("gpt-6-luna", "medium"),
}


def test_gpt6_role_model_and_effort_map() -> None:
    """Keep each specialist on its separately selected capability and effort axes."""
    assert {path.parent.name for path in ROLES.glob("*/ROLE.md")} == set(EXPECTED)
    for role, (model, effort) in EXPECTED.items():
        card = (ROLES / role / "ROLE.md").read_text(encoding="utf-8")
        assert re.search(rf"^model: {re.escape(model)}$", card, re.MULTILINE), role
        assert re.search(rf"^model_reasoning_effort: {effort}$", card, re.MULTILINE), role


def test_gpt6_parent_and_review_model_defaults() -> None:
    """Use Sol medium as the normal parent while retaining a Sol review model."""
    config = (REPOSITORY_ROOT / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert re.search(r'^model\s*=\s*"gpt-6\.1-sol"$', config, re.MULTILINE)
    assert re.search(r'^review_model\s*=\s*"gpt-6\.1-sol"$', config, re.MULTILINE)
    assert re.search(r'^model_reasoning_effort\s*=\s*"medium"$', config, re.MULTILINE)


def test_active_assignment_record_covers_roles_and_direct_routes() -> None:
    """Keep the active routing record complete without claiming live GPT-6 evidence."""
    evidence = json.loads(
        (
            REPOSITORY_ROOT / "plugins" / "codex-rig" / "runtime" / "calibration" / "accepted-route-evidence.json"
        ).read_text(encoding="utf-8")
    )
    active = evidence["active_assignments"]
    actual = {
        role: (model, effort)
        for model, efforts in active["roles"].items()
        for effort, roles in efforts.items()
        for role in roles
    }
    assert actual == EXPECTED
    assert sum(len(roles) for efforts in active["roles"].values() for roles in efforts.values()) == len(EXPECTED)
    assert active["parent"] == {"model": "gpt-6.1-sol", "reasoning_effort": "medium"}
    assert active["deep_review"] == {
        "model": "gpt-6.1-sol",
        "reasoning_effort": "high",
        "activation": "explicit-effort-override",
    }
    assert evidence["active_assignment_basis"]["gpt6_quality_cost_evidence"] == "pending-paired-evaluation"
    assert set(evidence["historical_assignments"]) == {"gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-terra"}


@pytest.fixture
def live_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Path, Path]:
    """Prepare local CLI inputs and prohibit external subprocess execution."""
    calibration = REPOSITORY_ROOT / "plugins" / "codex-rig" / "runtime" / "calibration"
    monkeypatch.syspath_prepend(str(calibration))
    spec = importlib.util.spec_from_file_location("gpt6_live_runner", calibration / "run_live_ab.py")
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)

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
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_live_ab.py",
            "--cases",
            str(cases),
            "--tasks",
            str(tasks),
            "--route-policy",
            str(policy),
            "--out",
            str(output),
            "--route",
            "selected",
        ],
    )
    return runner, policy, output


@pytest.mark.parametrize("paid", [False, True])
@pytest.mark.parametrize("field", ["baseline_model", "candidate_model"])
@pytest.mark.parametrize(
    "model",
    [
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-5.6-sol",
        "gpt-4",
        "gpt-60-sol",
        "gpt-6.9-sol",
        "gpt-6-terra",
        "GPT-6-sol",
        "gpt-6-sol ",
        "",
        None,
        pytest.param(["gpt-6-sol"], id="non-string-model"),
    ],
)
def test_live_cli_rejects_unsupported_models_before_side_effects(
    live_cli: tuple[Any, Path, Path], model: Any, field: str, paid: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    """Reject either invalid paired model before planning, authentication, or output writes."""
    runner, policy, output = live_cli
    route = {"baseline_model": "gpt-6.1-sol", "candidate_model": "gpt-6-luna", "min_campaigns": 1}
    route[field] = model
    policy.write_text(json.dumps({"routes": {"selected": route}}), encoding="utf-8")
    if paid:
        sys.argv.extend(["--confirm-paid-run", "chatgpt-subscription"])
    before = {path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()}

    with pytest.raises(ValueError, match=rf"unsupported live model: selected:{field}"):
        runner.main()

    assert capsys.readouterr().out == ""
    assert {path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()} == before
    assert sorted(path.name for path in output.iterdir()) == ["sentinel.txt"]


def test_live_timeout_cleans_windows_tree_without_posix_apis(
    live_cli: tuple[Any, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Return the timeout evidence after Windows tree cleanup with no POSIX-only attributes."""
    runner, _, _ = live_cli
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delattr(runner.os, "killpg", raising=False)
    monkeypatch.delattr(runner.signal, "SIGKILL", raising=False)
    process = Mock(pid=123, returncode=1)
    process.communicate.side_effect = [runner.subprocess.TimeoutExpired("example", 7), ("partial output", "detail")]
    start = Mock(return_value=process)
    kill_tree = Mock(return_value=runner.subprocess.CompletedProcess("taskkill", 0))
    monkeypatch.setattr(runner.subprocess, "Popen", start)
    monkeypatch.setattr(runner.subprocess, "run", kill_tree)

    result = runner._run_bounded_process(["example"], None, 7)

    assert result.exit_code == 124
    assert result.stdout == "partial output"
    assert result.stderr == "detail\ntimeout after 7 seconds\n"
    kill_tree.assert_called_once_with(
        ["taskkill", "/PID", "123", "/T", "/F"],
        stdin=runner.subprocess.DEVNULL,
        stdout=runner.subprocess.DEVNULL,
        stderr=runner.subprocess.DEVNULL,
        timeout=2,
        check=False,
    )
    assert [call.kwargs["timeout"] for call in process.communicate.call_args_list] == [7, 2]
    process.kill.assert_not_called()


@pytest.mark.parametrize(
    "tree_result",
    [
        pytest.param(1, id="taskkill-nonzero"),
        pytest.param(OSError("taskkill missing"), id="taskkill-unavailable"),
        pytest.param(TimeoutError("taskkill stalled"), id="taskkill-timeout"),
    ],
)
def test_live_timeout_reports_failed_windows_tree_cleanup_and_bounds_drain(
    live_cli: tuple[Any, Path, Path], monkeypatch: pytest.MonkeyPatch, tree_result: int | Exception
) -> None:
    """Keep captured bytes and exit 124 when tree cleanup and final pipe draining fail."""
    runner, _, _ = live_cli
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delattr(runner.os, "killpg", raising=False)
    monkeypatch.delattr(runner.signal, "SIGKILL", raising=False)
    process = Mock(pid=456)
    process.communicate.side_effect = [
        runner.subprocess.TimeoutExpired("example", 7, output=b"captured", stderr=b"original detail"),
        runner.subprocess.TimeoutExpired("example", 2),
        runner.subprocess.TimeoutExpired("example", 2),
    ]
    if isinstance(tree_result, TimeoutError):
        kill_tree = Mock(side_effect=runner.subprocess.TimeoutExpired("taskkill", 2))
    elif isinstance(tree_result, Exception):
        kill_tree = Mock(side_effect=tree_result)
    else:
        kill_tree = Mock(return_value=runner.subprocess.CompletedProcess("taskkill", tree_result))
    monkeypatch.setattr(runner.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(runner.subprocess, "run", kill_tree)

    result = runner._run_bounded_process(["example"], None, 7)

    assert result.exit_code == 124
    assert result.stdout == "captured"
    assert result.stderr.startswith("original detail\ntimeout after 7 seconds\n")
    assert "process-tree cleanup unproven" in result.stderr
    assert "output drain incomplete" in result.stderr
    assert [call.kwargs["timeout"] for call in process.communicate.call_args_list] == [7, 2, 2]
    process.kill.assert_called_once_with()
    process.stdout.close.assert_not_called()
    process.stderr.close.assert_not_called()
    assert kill_tree.call_args.kwargs["timeout"] == 2


@pytest.mark.parametrize("hard_kill", [False, True])
def test_live_timeout_preserves_posix_soft_then_hard_group_cleanup(
    live_cli: tuple[Any, Path, Path], monkeypatch: pytest.MonkeyPatch, hard_kill: bool
) -> None:
    """Retain group-wide POSIX escalation and bounded output collection on every host."""
    runner, _, _ = live_cli
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(runner.signal, "SIGTERM", 15, raising=False)
    monkeypatch.setattr(runner.signal, "SIGKILL", 9, raising=False)
    kill_group = Mock()
    monkeypatch.setattr(runner.os, "killpg", kill_group, raising=False)
    process = Mock(pid=789)
    responses = [runner.subprocess.TimeoutExpired("example", 7)]
    if hard_kill:
        responses.append(runner.subprocess.TimeoutExpired("example", 2))
    responses.append(("posix output", ""))
    process.communicate.side_effect = responses
    monkeypatch.setattr(runner.subprocess, "Popen", Mock(return_value=process))

    result = runner._run_bounded_process(["example"], None, 7)

    assert result.exit_code == 124
    assert result.stdout == "posix output"
    assert result.stderr == "\ntimeout after 7 seconds\n"
    assert [call.args for call in kill_group.call_args_list] == ([(789, 15), (789, 9)] if hard_kill else [(789, 15)])
    assert [call.kwargs["timeout"] for call in process.communicate.call_args_list] == (
        [7, 2, 2] if hard_kill else [7, 2]
    )


@pytest.mark.parametrize("consumer", ["model", "gate"])
def test_live_timeout_consumers_persist_windows_timeout_evidence(
    live_cli: tuple[Any, Path, Path], monkeypatch: pytest.MonkeyPatch, consumer: str
) -> None:
    """Keep model observations and executable gate logs usable after Windows cleanup."""
    runner, _, output = live_cli
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delattr(runner.os, "killpg", raising=False)
    monkeypatch.delattr(runner.signal, "SIGKILL", raising=False)
    captured = '{"usage":{"input_tokens":3,"output_tokens":1}}\n'
    process = Mock(pid=321, returncode=1)
    process.communicate.side_effect = [runner.subprocess.TimeoutExpired("example", 7), (captured, "detail")]
    monkeypatch.setattr(runner.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(runner.subprocess, "run", Mock(return_value=runner.subprocess.CompletedProcess("taskkill", 0)))

    if consumer == "model":
        response, usage, _, exit_code = runner._run_model(
            runner.ModelCallRequest(output, output, "example", 7, "prompt", "gpt-6.1-sol", "medium", "read-only")
        )
        assert response == {}
        assert usage == {"input_tokens": 3, "cached_input_tokens": 0, "output_tokens": 1}
        assert (output / "example.events.jsonl").read_text(encoding="utf-8") == captured
        evidence = (output / "example.stderr.txt").read_text(encoding="utf-8")
    else:
        exit_code = runner._run_gate(runner.GateRunRequest(output, output, "example", 7, "example"))
        evidence = (output / "example.gate.txt").read_text(encoding="utf-8")
        assert "exit_code=124" in evidence
        assert captured in evidence

    assert exit_code == 124
    assert "detail\ntimeout after 7 seconds\n" in evidence


@pytest.mark.parametrize("model", ["gpt-6-luna", "gpt-6-sol", "gpt-6.1-sol", "gpt-6-astra"])
def test_live_cli_plans_supported_selected_route_with_unselected_archive(
    live_cli: tuple[Any, Path, Path], model: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Accept supported paired models without reactivating unrelated historical routes."""
    runner, policy, output = live_cli
    policy.write_text(
        json.dumps(
            {
                "routes": {
                    "selected": {"baseline_model": model, "candidate_model": model, "min_campaigns": 2},
                    "archived": {"baseline_model": "gpt-5.6-terra", "candidate_model": "gpt-5.6-luna"},
                }
            }
        ),
        encoding="utf-8",
    )

    assert runner.main() == 0

    plan = json.loads(capsys.readouterr().out)
    assert plan["status"] == "planned"
    assert plan["routes"] == ["selected"]
    assert plan["paid_model_calls"] == 4
    assert sorted(path.name for path in output.iterdir()) == ["sentinel.txt"]
