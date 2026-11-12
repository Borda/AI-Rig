"""Exercise calibration input boundaries without authenticating or invoking paid models."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from _modules import load_module_scoped
from _platform import DIRECTORY_SYMLINKS_AVAILABLE

CALIBRATION = Path(__file__).resolve().parents[2] / "runtime" / "calibration"
THRESHOLDS = (
    "min_observations",
    "min_recall",
    "min_precision",
    "max_confidence_mae",
    "max_mean_overconfidence",
    "min_negative_specificity",
)


def _process_groups_available() -> bool:
    """Probe permission to signal the current process group without delivering a signal."""
    try:
        os.killpg(os.getpgrp(), 0)
    except (AttributeError, OSError):
        return False
    return True


_requires_process_groups = pytest.mark.skipif(
    not _process_groups_available(),
    reason="real descendant termination requires process groups; Windows fallback has simulated-host coverage",
)


@pytest.fixture
def modules(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Load the three calibration entry points with their real prompt contract."""
    source = Path(os.environ.get("CALIBRATION_SAFETY_SOURCE", CALIBRATION))
    monkeypatch.syspath_prepend(str(source))
    return SimpleNamespace(
        runner=load_module_scoped("safety_calibration", source / "run.py", monkeypatch),
        live=load_module_scoped("safety_live", source / "run_live_ab.py", monkeypatch),
        score=load_module_scoped("safety_score", source / "score_behavioral.py", monkeypatch),
    )


@pytest.mark.parametrize("key", THRESHOLDS)
@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), True])
def test_case_thresholds_fail_closed(modules: SimpleNamespace, tmp_path: Path, key: str, value: object) -> None:
    """Every gate threshold must be present and finite rather than silently permissive."""
    payload = json.loads((CALIBRATION / "behavioral-cases.json").read_text(encoding="utf-8"))
    payload["thresholds"][key] = value
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="threshold"):
        modules.score._load_cases(path)


@pytest.mark.parametrize("key", THRESHOLDS)
def test_case_thresholds_are_required(modules: SimpleNamespace, tmp_path: Path, key: str) -> None:
    """Deleting any threshold makes both the roster and scorer reject the policy."""
    payload = json.loads((CALIBRATION / "behavioral-cases.json").read_text(encoding="utf-8"))
    del payload["thresholds"][key]
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="threshold"):
        modules.score._load_cases(path)
    with pytest.raises(ValueError, match="threshold"):
        modules.runner.validate_behavioral_thresholds(payload)


@pytest.mark.skipif(not DIRECTORY_SYMLINKS_AVAILABLE, reason="filesystem cannot create directory symlinks")
def test_reports_symlink_cannot_redirect_writes(modules: SimpleNamespace, tmp_path: Path) -> None:
    """Reject an escaped report ancestor before creating any run directories outside the project."""
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    (project / ".reports").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match=r"report.*escapes"):
        modules.runner.Paths.create("plugin", project)
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("component", ["../escape", "..", "/absolute", "a/b", "a\\b", "C:escape"])
def test_role_and_workspace_components_cannot_escape(modules: SimpleNamespace, tmp_path: Path, component: str) -> None:
    """Reject cross-platform path syntax before role reads or workspace creation."""
    run = SimpleNamespace(paths=SimpleNamespace(layout="plugin", roles_dir=tmp_path / "roles"))
    with pytest.raises(ValueError, match="component"):
        modules.runner.role_file(run, component)
    with pytest.raises(ValueError, match="component"):
        modules.live._prepare_workspace(tmp_path, component, {})
    with pytest.raises(ValueError, match="component"):
        modules.live._write_input_snapshot(tmp_path, {}, {}, {}, tmp_path, {component}, "plugin")
    assert not (tmp_path / "inputs").exists()


@pytest.mark.parametrize(
    "text", [pytest.param(" " * 100_000, id="whitespace"), pytest.param("a" * 300_000, id="oversize")]
)
@pytest.mark.timeout(2)
def test_oversize_workflow_text_fails_before_regex_scan(modules: SimpleNamespace, text: str) -> None:
    """Refuse hostile policy and summary lines instead of spending unbounded regex time."""
    assert modules.runner.workflow_summary_findings(text) == ["workflow-input-too-large"]
    assert modules.runner.workflow_policy_findings(text, "") == ["workflow-input-too-large"]


def test_source_layout_help_declares_code_execution(
    modules: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Make the source-root execution trust requirement visible before running calibration."""
    monkeypatch.setattr(sys, "argv", ["run.py", "--help"])
    with pytest.raises(SystemExit) as stopped:
        modules.runner.main()
    assert stopped.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "trusted" in help_text
    assert "executes" in help_text
    assert "--root" in help_text


def test_api_override_is_refused_before_login(modules: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    """A noninteractive API override cannot bypass the subscription-only guard."""
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("CODEX_API_KEY", "test-not-a-credential")

    def login_status(*args, **kwargs):
        """Supply subscription status without inspecting any real account."""
        return subprocess.CompletedProcess([], 0, "Logged in using ChatGPT", "")

    monkeypatch.setattr(modules.live.subprocess, "run", login_status)
    with pytest.raises(SystemExit, match="api-key-auth-disallowed"):
        modules.live._require_local_subscription_run()


def test_fixture_paths_cannot_escape_selftest(modules: SimpleNamespace, tmp_path: Path) -> None:
    """Selftest fixture writes apply the same containment boundary as live workspaces."""
    fixture_dir = tmp_path / "fixtures"
    with pytest.raises(ValueError, match="escapes"):
        modules.runner.write_fixture_files(fixture_dir, {"../escape": "content"})
    assert not (tmp_path / "escape").exists()


def test_gate_drops_inherited_secrets(
    modules: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real gate child receives runtime variables without inherited credentials or login homes."""
    monkeypatch.setenv("CALIBRATION_TEST_SECRET", "must-not-reach-gate")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-gate")
    script = tmp_path / "gate.py"
    script.write_text(
        "import os\nassert 'CALIBRATION_TEST_SECRET' not in os.environ\n"
        "assert 'OPENAI_API_KEY' not in os.environ\nassert not os.path.exists(os.path.join(os.environ['HOME'], 'auth.json'))\n",
        encoding="utf-8",
    )
    command = shlex.join([sys.executable, str(script)])
    result = modules.live._run_gate(modules.live.GateRunRequest(tmp_path, tmp_path, "gate", 5, command))
    assert result == 0


def test_malformed_benchmark_regex_records_advisory(modules: SimpleNamespace, tmp_path: Path) -> None:
    """A malformed optional pattern does not abort artifact creation."""
    benchmarks = tmp_path / "benchmarks.json"
    benchmarks.write_text(json.dumps({"skills": {"example": ["["]}}), encoding="utf-8")
    run = modules.runner.CalibrationRun(paths=modules.runner.Paths.create("plugin", tmp_path))
    run.paths.benchmarks = benchmarks
    modules.runner.run_benchmark_pattern_checks(run)
    assert "benchmark-skill-miss:example:[" in run.paths.checks.read_text(encoding="utf-8")


@pytest.fixture
def live_row(modules: SimpleNamespace) -> tuple[dict, dict, dict, dict]:
    """Build one valid live row using the shipped task, role card, and prompt hashes."""
    policy = json.loads((CALIBRATION / "live-route-policy.json").read_text(encoding="utf-8"))
    tasks = json.loads((CALIBRATION / "live-ab-tasks.json").read_text(encoding="utf-8"))["routes"]
    cases = {
        case["id"]: case
        for case in json.loads((CALIBRATION / "behavioral-cases.json").read_text(encoding="utf-8"))["cases"]
    }
    route_id = next(iter(policy["routes"]))
    task = tasks[route_id][0]
    role = task["role"]
    prompt = modules.live.build_prompt(
        cases[task["case_id"]],
        modules.live.candidate_findings(task["case_id"], cases),
        task,
        modules.live.role_context(CALIBRATION.parents[1], role, "plugin"),
    )
    signature = (task["case_id"], role, task["task_type"], task.get("evidence_scope", "classification"))
    record = {
        "case_id": task["case_id"],
        "role": role,
        "task_type": task["task_type"],
        "route_id": route_id,
        "evidence_scope": signature[3],
        "pair_role": "baseline",
        "campaign_id": "campaign",
        "pair_id": "pair",
        "run_id": "pair",
        "pricing_ref": modules.score.PRICING_REF,
        "outcome": "pass",
        "model": policy["routes"][route_id]["baseline_model"],
        "reasoning_effort": policy["routes"][route_id]["effort"],
        "input_tokens": 10,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "latency_ms": 1,
        "tool_failure_count": 0,
        "check_failure_count": 0,
        "estimated_cost_units": 10.0,
        "prompt_sha256": modules.live.prompt_sha256(prompt),
        "task_contract_sha256": modules.live.task_contract_sha256(task),
    }
    return record, policy, {route_id: {signature: task}}, cases


@pytest.mark.parametrize(
    ("key", "value"),
    [
        pytest.param("estimated_cost_units", float("nan"), id="nan-cost"),
        pytest.param("estimated_cost_units", float("inf"), id="infinite-cost"),
        pytest.param("input_tokens", True, id="boolean-token-count"),
        pytest.param("check_failure_count", False, id="boolean-failure-count"),
        pytest.param("pair_role", [], id="list-pair-role"),
        pytest.param("outcome", [], id="list-outcome"),
        pytest.param("evidence_scope", [], id="list-scope"),
        pytest.param("task_type", "unknown-task", id="unknown-signature"),
    ],
)
def test_live_row_rejects_invalid_metadata(modules: SimpleNamespace, live_row: tuple, key: str, value: object) -> None:
    """Malformed live evidence must raise a validation error instead of passing or crashing with TypeError."""
    record, policy, tasks, cases = live_row
    record["estimated_cost_units"] = 1.0 if key == "input_tokens" and value is True else record["estimated_cost_units"]
    record[key] = value
    with pytest.raises(ValueError, match="live"):
        modules.score._validate_live_metadata(record, policy, tasks, cases, CALIBRATION.parents[1], "plugin")


def test_valid_live_row_retains_hash_verification(modules: SimpleNamespace, live_row: tuple) -> None:
    """Accept the canonical row but reject a mismatched prompt on the same known signature."""
    record, policy, tasks, cases = live_row
    modules.score._validate_live_metadata(record, policy, tasks, cases, CALIBRATION.parents[1], "plugin")
    record["prompt_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="prompt hash mismatch"):
        modules.score._validate_live_metadata(record, policy, tasks, cases, CALIBRATION.parents[1], "plugin")


@pytest.mark.parametrize(
    "events", ['{"input_tokens": null, "output_tokens": 1}', pytest.param("[" * 2000, id="deep-json")]
)
def test_model_usage_errors_return_failed_call(
    modules: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, events: str
) -> None:
    """Malformed paid-call usage preserves failure evidence for the campaign row."""

    def completed_call(*args, **kwargs):
        """Supply one already completed external call without invoking Codex."""
        return modules.live.BoundedProcessResult(events, "", 0, 10)

    monkeypatch.setattr(modules.live, "_run_bounded_process", completed_call)
    (tmp_path / "call.response.json").write_text('{"reported_findings": [], "confidence": 0.9}', encoding="utf-8")
    response, usage, latency, exit_code = modules.live._run_model(
        modules.live.ModelCallRequest(tmp_path, tmp_path, "call", 5, "prompt", "example", "medium", "read-only")
    )
    assert response == {"reported_findings": [], "confidence": 0.9}
    assert usage == {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
    assert (latency, exit_code) == (10, 1)
    assert (tmp_path / "call.stderr.txt").read_text(encoding="utf-8").strip()


def test_paid_guard_selftest_environment_has_only_offline_codex(modules: SimpleNamespace, tmp_path: Path) -> None:
    """Even a misplaced login check reaches a nonauthenticating stub and an isolated login home."""
    env = modules.runner.offline_live_environment(tmp_path)
    stub = Path(env["PATH"]) / ("codex.cmd" if sys.platform == "win32" else "codex")
    call = subprocess.run([str(stub), "login", "status"], env=env, capture_output=True, check=False)
    assert call.returncode == 97
    assert call.stdout == b""
    assert call.stderr == b""
    assert Path(env["CODEX_HOME"]).is_relative_to(tmp_path)
    assert "OPENAI_API_KEY" not in env
    assert "CODEX_API_KEY" not in env


@pytest.mark.parametrize("fixture_text", ["{", "[]", "null"])
def test_malformed_selftest_json_is_recorded(modules: SimpleNamespace, tmp_path: Path, fixture_text: str) -> None:
    """A broken helper fixture cannot abort the calibration before its final result is written."""
    gates = tmp_path / "fake-gates.py"
    gates.write_text(
        "import pathlib, sys\nout = pathlib.Path(sys.argv[sys.argv.index('--out') + 1])\n"
        f"out.mkdir(parents=True, exist_ok=True)\n(out / 'gates.json').write_text({fixture_text!r})\n"
        "raise SystemExit(1 if 'exit 1' in sys.argv else 0)\n",
        encoding="utf-8",
    )
    gates.chmod(0o700)
    run = modules.runner.CalibrationRun(paths=modules.runner.Paths.create("plugin", tmp_path))
    run.paths = replace(run.paths, run_gates=gates)
    modules.runner.run_selftests(run)
    assert run.fails == 1
    assert "selftest-input-invalid" in run.paths.leaks.read_text(encoding="utf-8")


def test_subprocess_output_is_capped(modules: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A noisy child fails with bounded captured bytes instead of exhausting runner memory."""
    monkeypatch.setattr(modules.live, "MAX_OUTPUT_BYTES", 4096, raising=False)
    script = tmp_path / "flood.py"
    script.write_text("import os\nos.write(1, b'x' * 2000000)\nos.write(2, b'y' * 2000000)\n", encoding="utf-8")
    result = modules.live._run_bounded_process([sys.executable, str(script)], tmp_path, 5)
    assert result.exit_code == 125
    assert len(result.stdout.encode()) <= 4096
    assert len(result.stderr.encode()) <= 4608
    assert "output limit exceeded" in result.stderr


@_requires_process_groups
def test_timeout_covers_pipes_after_parent_exit(modules: SimpleNamespace, tmp_path: Path) -> None:
    """A descendant retaining inherited pipes cannot turn timeout cleanup into a passing gate."""
    marker = tmp_path / "descendant-finished"
    child = f"import time, pathlib; time.sleep(2); pathlib.Path({str(marker)!r}).touch()"
    parent = tmp_path / "parent.py"
    parent.write_text(
        f"import subprocess, sys\nsubprocess.Popen([sys.executable, '-c', {child!r}])\n", encoding="utf-8"
    )
    result = modules.live._run_bounded_process([sys.executable, str(parent)], tmp_path, 1)
    assert result.exit_code == 124
    assert "timeout after 1 seconds" in result.stderr
    assert not marker.exists()


@_requires_process_groups
def test_output_cap_terminates_descendant_after_parent_exit(
    modules: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An overflow still terminates the owned tree after its direct parent and capture readers exit."""
    monkeypatch.setattr(modules.live, "MAX_OUTPUT_BYTES", 4096)
    marker = tmp_path / "descendant-survived"
    child = (
        "import os, time, pathlib\ntime.sleep(0.2)\ntry:\n os.write(1, b'x' * 2000000)\n"
        "except OSError:\n pass\nos.close(1)\nos.close(2)\ntime.sleep(0.6)\n"
        f"pathlib.Path({str(marker)!r}).touch()\n"
    )
    parent = tmp_path / "parent.py"
    parent.write_text(
        f"import subprocess, sys\nsubprocess.Popen([sys.executable, '-c', {child!r}])\n", encoding="utf-8"
    )
    result = modules.live._run_bounded_process([sys.executable, str(parent)], tmp_path, 2)
    time.sleep(0.8)
    assert result.exit_code == 125
    assert "output limit exceeded" in result.stderr
    assert not marker.exists()


@pytest.fixture
def campaign_args(tmp_path: Path) -> list[str]:
    """Create a one-task campaign with no real model dependency or project content in its prompt."""
    task = {
        "case_id": "case",
        "role": "sw-engineer",
        "task_type": "inspection",
        "evidence_scope": "classification",
        "task_prompt": "Inspect the provided finding.",
    }
    cases = {
        "cases": [
            {"id": "case", "target": "implement", "prompt": "Example", "expected_findings": ["one"]},
            {"id": "other", "target": "implement", "prompt": "Other", "expected_findings": ["two"]},
        ]
    }
    policy = {
        "routes": {
            "selected": {
                "baseline_model": "baseline",
                "candidate_model": "candidate",
                "effort": "medium",
                "min_campaigns": 1,
            }
        }
    }
    (tmp_path / "cases.json").write_text(json.dumps(cases), encoding="utf-8")
    (tmp_path / "tasks.json").write_text(json.dumps({"routes": {"selected": [task]}}), encoding="utf-8")
    (tmp_path / "policy.json").write_text(json.dumps(policy), encoding="utf-8")
    (tmp_path / ".env").write_text("PRIVATE_SENTINEL", encoding="utf-8")
    return [
        "run_live_ab.py",
        "--cases",
        str(tmp_path / "cases.json"),
        "--tasks",
        str(tmp_path / "tasks.json"),
        "--route-policy",
        str(tmp_path / "policy.json"),
        "--out",
        str(tmp_path / "output"),
        "--root",
        str(tmp_path),
        "--layout",
        "plugin",
    ]


def test_classification_model_receives_empty_workspace(
    modules: SimpleNamespace, campaign_args: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Classification sends only the constructed prompt, without exposing the consuming checkout as its cwd."""

    def login_status(*args, **kwargs):
        """Supply subscription status without accessing stored authentication."""
        return subprocess.CompletedProcess([], 0, "Logged in using ChatGPT", "")

    def model_call(argv, cwd, timeout_seconds, **kwargs):
        """Validate external CLI inputs and supply a completed model response without a paid call."""
        workspace = Path(argv[argv.index("--cd") + 1])
        assert workspace != tmp_path
        assert not workspace.is_relative_to(tmp_path)
        assert list(workspace.iterdir()) == []
        assert argv[argv.index("--sandbox") + 1] == "read-only"
        response = Path(argv[argv.index("--output-last-message") + 1])
        response.write_text('{"reported_findings": [], "confidence": 0.9}', encoding="utf-8")
        return modules.live.BoundedProcessResult('{"input_tokens": 10, "output_tokens": 1}', "", 0, 1)

    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.setattr(modules.live.subprocess, "run", login_status)
    monkeypatch.setattr(modules.live, "_run_bounded_process", model_call)
    monkeypatch.setattr(sys, "argv", [*campaign_args, "--confirm-paid-run", "chatgpt-subscription"])
    assert modules.live.main() == 0
    assert len((tmp_path / "output" / "observations.jsonl").read_text(encoding="utf-8").splitlines()) == 2


@pytest.mark.parametrize("field", ["route", "role"])
def test_cli_rejects_path_identifiers_before_context_reads(
    modules: SimpleNamespace, campaign_args: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    """Unsafe policy routes and task roles fail before snapshots, authentication, or role-file reads."""
    tasks_path = tmp_path / "tasks.json"
    tasks = json.loads(tasks_path.read_text(encoding="utf-8"))
    policy_path = tmp_path / "policy.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    route_id = "../escape" if field == "route" else "selected"
    task = tasks["routes"]["selected"][0]
    task["role"] = "../escape" if field == "role" else "sw-engineer"
    tasks_path.write_text(json.dumps({"routes": {route_id: [task]}}), encoding="utf-8")
    policy_path.write_text(json.dumps({"routes": {route_id: policy["routes"]["selected"]}}), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", campaign_args)
    with pytest.raises(ValueError, match="component"):
        modules.live.main()
    assert not (tmp_path / "output").exists()
