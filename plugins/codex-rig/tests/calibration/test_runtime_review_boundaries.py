"""Cover role reads and subprocess deadlines remaining in the calibration runtime audit."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from _modules import load_module_scoped

CALIBRATION = Path(__file__).resolve().parents[2] / "runtime" / "calibration"


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Load calibration entry points with their shared role contract."""
    monkeypatch.syspath_prepend(str(CALIBRATION))
    return SimpleNamespace(
        runner=load_module_scoped("runtime_review_runner", CALIBRATION / "run.py", monkeypatch),
        live=load_module_scoped("runtime_review_live", CALIBRATION / "run_live_ab.py", monkeypatch),
    )


@pytest.mark.parametrize("layout", ["source", "plugin"])
@pytest.mark.parametrize("role", ["../escape", "..", "/absolute", "a/b", "a\\b", "C:escape", ""])
def test_role_contract_rejects_paths_before_reading(
    runtime: SimpleNamespace, tmp_path: Path, layout: str, role: str
) -> None:
    """All callers reject nonportable role components before attempting any file read."""
    with pytest.raises(ValueError, match="path component"):
        runtime.runner.role_context(tmp_path, role, layout)


@pytest.mark.parametrize("layout", ["source", "plugin"])
def test_role_contract_reads_valid_instructions(runtime: SimpleNamespace, tmp_path: Path, layout: str) -> None:
    """A portable role retains both installed and source-layout instruction loading."""
    (tmp_path / "roles" / "review-owner").mkdir(parents=True)
    (tmp_path / "roles" / "review-owner" / "ROLE.md").write_text("ROLE_MARKER", encoding="utf-8")
    (tmp_path / ".codex" / "agents").mkdir(parents=True)
    (tmp_path / ".codex" / "AGENTS.md").write_text("GLOBAL_MARKER", encoding="utf-8")
    (tmp_path / ".codex" / "agents" / "review-owner.toml").write_text(
        'developer_instructions = """ROLE_MARKER"""\n', encoding="utf-8"
    )
    assert "ROLE_MARKER" in runtime.runner.role_context(tmp_path, "review-owner", layout)


def test_command_deadline_returns_failure_status(runtime: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    """A real over-deadline subprocess returns a failing result usable by existing gates."""
    monkeypatch.setattr(runtime.runner, "SUBPROCESS_TIMEOUT_SECONDS", 0.05, raising=False)
    result = runtime.runner.run_command([sys.executable, "-c", "import time; time.sleep(0.3)"])
    assert result.returncode == 124
    assert result.stderr == "calibration-command-timeout"


def test_head_lookup_timeout_is_a_recordable_io_failure(
    runtime: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tree-lookup timeouts follow the same fail-closed path as an unavailable Git executable."""

    def timeout(args: list[str], **kwargs: object) -> None:
        """Check the finite deadline at the actual subprocess boundary."""
        assert 0 < kwargs["timeout"] <= 60
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(runtime.runner.subprocess, "run", timeout)
    with pytest.raises(OSError, match="HEAD path lookup failed"):
        runtime.runner._head_path_exists(tmp_path, "fixture.json")


@pytest.mark.parametrize("operation", ["rev-parse", "ls-tree", "show"])
def test_version_git_timeouts_record_failed_gate(
    runtime: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    """Every Git step in fixture-version admission records timeout as a gate failure."""
    (tmp_path / ".git").mkdir()
    cases = tmp_path / "behavioral-cases.json"
    cases.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
    run_py = tmp_path / "run.py"
    run_py.write_text("", encoding="utf-8")
    paths = SimpleNamespace(behavioral_cases=cases, run_py=run_py, layout="plugin")
    failures = []
    run = SimpleNamespace(paths=paths, fail_and_leak=lambda *args: failures.append(args))

    def git(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        """Supply unaffected Git responses while timing out the chosen operation."""
        assert 0 < kwargs["timeout"] <= 60
        if args[1] == operation:
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])
        outputs = {"rev-parse": str(tmp_path), "ls-tree": args[-1] + "\n", "show": cases.read_text()}
        return subprocess.CompletedProcess(args, 0, outputs[args[1]], "")

    monkeypatch.setattr(runtime.runner.subprocess, "run", git)
    runtime.runner.check_behavioral_cases_version(run)
    assert len(failures) == 1
    assert failures[0][0] == "behavioral-version-policy"
    assert "failed" in failures[0][1]


def test_syntax_timeout_records_failed_gate(
    runtime: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stuck compiler cannot abort calibration without recording its failed syntax gate."""
    source = tmp_path / "fixture.py"
    source.write_text("pass\n", encoding="utf-8")
    failures = []
    run = SimpleNamespace(paths=SimpleNamespace(out_dir=tmp_path), fail_and_leak=lambda *args: failures.append(args))

    def timeout(args: list[str], **kwargs: object) -> None:
        """Enforce a finite timeout and emulate its expiration."""
        assert 0 < kwargs["timeout"] <= 60
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(runtime.runner.subprocess, "run", timeout)
    runtime.runner.check_python_syntax(run, source, "fixture")
    assert failures == [("shared-script-selftests", "shared-script-syntax:fixture")]


def test_subscription_probe_timeout_fails_before_paid_run(
    runtime: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Login status expiration yields an explicit pre-execution failure."""
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_API_KEY", raising=False)

    def timeout(args: list[str], **kwargs: object) -> None:
        """Verify the probe has its own short finite deadline."""
        assert 0 < kwargs["timeout"] <= 10
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(runtime.live.subprocess, "run", timeout)
    with pytest.raises(SystemExit, match="live-paid-run-login-status-timeout"):
        runtime.live._require_local_subscription_run()
