"""Cross-platform acceptance checks for shared workflow helpers."""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
COLLECT_DIFF = PLUGIN_ROOT / "shared" / "collect_diff.py"
RUN_GATES = PLUGIN_ROOT / "shared" / "run_gates.py"
SHARED_VALIDATOR = PLUGIN_ROOT / "shared" / "validate-artifacts.py"
REVIEW_VALIDATOR = PLUGIN_ROOT / "skills" / "code-review" / "validate_artifacts.py"
GATE_IDS = ("lint", "format", "types", "tests", "review")


def _load_module(path: Path, name: str) -> Any:
    """Load one standalone helper without requiring package imports."""
    assert path.is_file(), path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _initialize_repository(root: Path) -> None:
    """Create one committed Git repository for diff collection tests."""
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "tracked.txt").write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "tracked.txt"], check=True)
    environment = {
        "GIT_AUTHOR_NAME": "Codex Rig Test",
        "GIT_AUTHOR_EMAIL": "codex-rig@example.invalid",
        "GIT_COMMITTER_NAME": "Codex Rig Test",
        "GIT_COMMITTER_EMAIL": "codex-rig@example.invalid",
    }
    subprocess.run(
        ["git", "-C", str(root), "commit", "-qm", "fixture"],
        check=True,
        env=environment,
    )


def _skipped_gate_args() -> list[str]:
    """Return explicit reasons for all five not-applicable gates.

    Example:
        >>> len(_skipped_gate_args())
        10
    """
    arguments: list[str] = []
    for gate_id in GATE_IDS:
        arguments.extend((f"--skip-{gate_id}", f"{gate_id} not applicable"))
    return arguments


def test_collect_diff_runs_natively_and_writes_complete_artifacts(tmp_path: Path) -> None:
    """Collect tracked and untracked evidence without invoking Bash."""
    repository = tmp_path / "repository"
    repository.mkdir()
    _initialize_repository(repository)
    (repository / "tracked.txt").write_text("after\n", encoding="utf-8")
    (repository / "untracked.txt").write_text("new\n", encoding="utf-8")
    output = tmp_path / "artifacts"

    completed = subprocess.run(
        [sys.executable, str(COLLECT_DIFF), "--out", str(output)],
        cwd=repository,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert {path.name for path in output.iterdir()} == {
        "diff.patch",
        "diffstat.txt",
        "files.txt",
        "numstat.txt",
        "status.txt",
        "untracked.txt",
    }
    assert (output / "files.txt").read_text(encoding="utf-8") == "tracked.txt\n"
    assert (output / "untracked.txt").read_text(encoding="utf-8") == "untracked.txt\n"
    assert "-before" in (output / "diff.patch").read_text(encoding="utf-8")
    assert "+after" in (output / "diff.patch").read_text(encoding="utf-8")


def test_collect_diff_uses_only_git_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep collection shell-free and bind every subprocess to Git argv."""
    module = _load_module(COLLECT_DIFF, "codex_rig_portable_collect_diff")
    calls: list[list[str]] = []

    def _record_run(arguments: list[str], **_: object) -> Any:
        """Record shell-free collection argv and return a successful process stub."""
        calls.append(arguments)
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr(module.subprocess, "run", _record_run)

    assert module.collect_diff("working-tree", "", tmp_path / "artifacts") == 0
    assert calls == [
        ["git", "status", "--short"],
        ["git", "diff", "HEAD"],
        ["git", "diff", "--name-only", "HEAD"],
        ["git", "diff", "--stat", "HEAD"],
        ["git", "diff", "--numstat", "HEAD"],
        ["git", "ls-files", "--others", "--exclude-standard"],
    ]


def test_run_gates_selects_powershell_for_simulated_windows_and_bash_on_posix() -> None:
    """Bind command execution to the host-native shell family."""
    module = _load_module(RUN_GATES, "codex_rig_portable_run_gates")

    assert module.command_argv("Write-Output ok", platform="win32") == [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "Write-Output ok",
    ]
    assert module.command_argv("printf ok", platform="linux") == ["bash", "-lc", "printf ok"]
    assert set(module.default_commands("win32")) == set(GATE_IDS)
    assert all("command -v" not in command for command in module.default_commands("win32").values())
    expected_executable = "powershell.exe" if sys.platform == "win32" else "bash"
    assert module.command_argv("native")[0] == expected_executable


def test_run_gates_terminates_simulated_windows_process_trees_with_taskkill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use the native Windows process-tree terminator before the kill fallback."""
    module = _load_module(RUN_GATES, "codex_rig_portable_windows_termination")
    calls: list[list[str]] = []

    class Process:
        pid = 42

        def poll(self) -> None:
            """Report a still-running fake process to exercise termination."""
            return None

        def wait(self, timeout: int | None = None) -> int:
            """Return successful termination after checking the bounded wait timeout."""
            assert timeout == 2
            return 0

    def _record_run(arguments: list[str], **_: object) -> Any:
        """Record native Windows termination argv and return a successful process stub."""
        calls.append(arguments)
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr(module.subprocess, "run", _record_run)

    module.terminate_process(Process(), "win32")

    assert calls == [["taskkill", "/PID", "42", "/T", "/F"]]


def test_run_gates_writes_exact_five_gate_artifacts(tmp_path: Path) -> None:
    """Emit the canonical five-gate JSON and per-check evidence natively."""
    output = tmp_path / "gates"

    completed = subprocess.run(
        [sys.executable, str(RUN_GATES), "--out", str(output), *_skipped_gate_args()],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == f"{output / 'gates.json'}\n"
    payload = json.loads((output / "gates.json").read_text(encoding="utf-8"))
    assert set(payload) == {
        "status",
        "checks_run",
        "checks_failed",
        "failed_count",
        "checks_not_applicable",
        "checks",
    }
    assert payload["status"] == "pass"
    assert payload["checks_run"] == list(GATE_IDS)
    assert payload["checks_failed"] == []
    assert payload["failed_count"] == 0
    assert payload["checks_not_applicable"] == list(GATE_IDS)
    assert [check["id"] for check in payload["checks"]] == list(GATE_IDS)
    assert {check["status"] for check in payload["checks"]} == {"not-applicable"}
    assert (output / "gates.txt").read_text(encoding="utf-8").splitlines() == [
        f"{gate_id}:not-applicable" for gate_id in GATE_IDS
    ]
    assert (output / "failed.txt").read_text(encoding="utf-8") == ""
    jsonl = [json.loads(line) for line in (output / "gates.checks.jsonl").read_text().splitlines()]
    assert jsonl == payload["checks"]
    for gate_id in GATE_IDS:
        for suffix in ("command", "stdout", "stderr"):
            assert (output / "checks" / f"{gate_id}.{suffix}.txt").is_file()


def test_run_gates_records_log_paths_relative_to_the_output_directory(tmp_path: Path) -> None:
    """Record every gate log as a POSIX path under the output directory, never as a host-native one.

    The recorded string is the only coordinate a later reader gets; the producer's working directory is not stored
    anywhere, so a path that needs it cannot be resolved by anyone else.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    output = workspace / "run"

    completed = subprocess.run(
        [sys.executable, str(RUN_GATES), "--out", "run", *_skipped_gate_args()],
        cwd=workspace,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads((output / "gates.json").read_text(encoding="utf-8"))
    for check in payload["checks"]:
        for key in ("command_path", "stdout", "stderr"):
            recorded = check[key]
            assert not Path(recorded).is_absolute()
            assert "\\" not in recorded
            assert recorded.startswith("checks/")
            assert (output / recorded).is_file()


@pytest.mark.parametrize(
    ("arguments", "expected_status"),
    [
        pytest.param(["--skip-lint", "not applicable here"], "not-applicable", id="skipped-gate"),
        pytest.param(["--lint", 'python -c "pass"'], "pass", id="executed-gate"),
        pytest.param(["--lint", 'python -c "raise SystemExit(3)"'], "fail", id="failed-gate"),
    ],
)
def test_run_gates_records_output_relative_logs_for_every_gate_outcome(
    tmp_path: Path, arguments: list[str], expected_status: str
) -> None:
    """Record output-relative logs whether a gate is skipped, runs and passes, or runs and fails.

    Each outcome returns through a different branch of run_check, so covering only one of them would let a partial
    revert restore the old host-native paths for gates that actually ran a command — that is, for every real run.
    """
    output = tmp_path / "gates"
    others = [argument for gate_id in GATE_IDS if gate_id != "lint" for argument in (f"--skip-{gate_id}", "n/a")]

    subprocess.run(
        [sys.executable, str(RUN_GATES), "--out", str(output), *arguments, *others],
        capture_output=True,
        check=False,
    )

    payload = json.loads((output / "gates.json").read_text(encoding="utf-8"))
    lint = next(check for check in payload["checks"] if check["id"] == "lint")
    assert lint["status"] == expected_status
    for key in ("command_path", "stdout", "stderr"):
        assert lint[key] == f"checks/lint.{'command' if key == 'command_path' else key}.txt"
        assert (output / lint[key]).is_file()


@pytest.mark.parametrize("directory_name", ["workspace", "elsewhere"])
def test_gate_validation_ignores_the_callers_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, directory_name: str
) -> None:
    """Return the same verdict for one finished artifact from the producing workspace and from elsewhere."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    output = workspace / "run"
    subprocess.run(
        [sys.executable, str(RUN_GATES), "--out", "run", *_skipped_gate_args()],
        cwd=workspace,
        check=True,
        capture_output=True,
    )
    module = _load_module(SHARED_VALIDATOR, "codex_rig_shared_validate_artifacts")

    directory = workspace if directory_name == "workspace" else elsewhere
    monkeypatch.chdir(directory)

    assert module._validate_gates(output)["status"] == "pass"


@pytest.mark.parametrize("directory_name", ["workspace", "elsewhere"])
def test_gate_validation_accepts_a_legacy_ancestor_relative_log_from_any_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, directory_name: str
) -> None:
    """Accept a gate log recorded relative to an ancestor of the output directory, wherever the reader runs.

    Runs produced before logs were recorded relative to the output directory stored a path relative to whichever
    directory produced them, and `--complete-run` revalidates such a run long afterwards. Rejecting that form would
    strand finished work, so it resolves from `out_dir`'s ancestors — never from the reader's own directory, which is
    what made the verdict vary by caller.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    output = workspace / "run"
    subprocess.run(
        [sys.executable, str(RUN_GATES), "--out", "run", *_skipped_gate_args()],
        cwd=workspace,
        check=True,
        capture_output=True,
    )
    gates_path = output / "gates.json"
    payload = json.loads(gates_path.read_text(encoding="utf-8"))
    payload["checks"][0]["command_path"] = f"run/{payload['checks'][0]['command_path']}"
    gates_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    module = _load_module(SHARED_VALIDATOR, "codex_rig_shared_validate_artifacts_legacy")

    directory = workspace if directory_name == "workspace" else elsewhere
    monkeypatch.chdir(directory)

    assert module._validate_gates(output)["status"] == "pass"


def test_gate_validation_rejects_a_log_resolving_outside_the_output_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject a gate log that exists but sits outside the output directory it claims to document.

    Trying ancestors widens where a relative name may resolve, so the containment check is what keeps a same-named file
    elsewhere on disk from being accepted as this run's evidence.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    output = workspace / "run"
    subprocess.run(
        [sys.executable, str(RUN_GATES), "--out", "run", *_skipped_gate_args()],
        cwd=workspace,
        check=True,
        capture_output=True,
    )
    (workspace / "outside.txt").write_text("not this run's evidence\n", encoding="utf-8")
    gates_path = output / "gates.json"
    payload = json.loads(gates_path.read_text(encoding="utf-8"))
    payload["checks"][0]["command_path"] = "outside.txt"
    gates_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    module = _load_module(SHARED_VALIDATOR, "codex_rig_shared_validate_artifacts_outside")
    monkeypatch.chdir(workspace)

    with pytest.raises(SystemExit) as failure:
        module._validate_gates(output)

    assert str(failure.value) == "gate-check-log-outside-output:0:command_path"


def test_run_gates_times_out_and_terminates_native_process(tmp_path: Path) -> None:
    """Stop a timed-out command tree and retain canonical timeout evidence."""
    output = tmp_path / "timeout"
    if sys.platform == "win32":
        command = f'& "{sys.executable}" -c "import time; time.sleep(30)"'
    else:
        command = shlex.join((sys.executable, "-c", "import time; time.sleep(30)"))
    arguments = ["--lint", command]
    for gate_id in GATE_IDS[1:]:
        arguments.extend((f"--skip-{gate_id}", f"{gate_id} not applicable"))

    started = time.monotonic()
    completed = subprocess.run(
        [sys.executable, str(RUN_GATES), "--out", str(output), "--timeout-seconds", "1", *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=10,
    )

    assert completed.returncode == 124
    assert time.monotonic() - started < 8
    payload = json.loads((output / "gates.json").read_text(encoding="utf-8"))
    assert payload["status"] == "timeout"
    assert payload["checks_failed"] == ["lint"]
    assert payload["checks"][0]["status"] == "timeout"
    assert payload["checks"][0]["exit_code"] == 124
    assert payload["checks"][0]["reason"] == "timeout after 1 seconds"
    assert "timeout: exceeded 1 seconds" in (output / "checks" / "lint.stderr.txt").read_text()


@pytest.mark.parametrize("wrapper_name", ("collect-diff.sh", "run-gates.sh"))
def test_portable_helpers_do_not_ship_shell_compatibility_wrappers(wrapper_name: str) -> None:
    """Keep the plugin helper surface Python-only across supported platforms."""
    assert not (PLUGIN_ROOT / "shared" / wrapper_name).exists()


def test_review_validator_help_survives_a_host_without_home_variables(tmp_path: Path) -> None:
    """Reject a home lookup on the argparse default path, which Windows resolves from USERPROFILE."""
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in ("PATH", "SystemRoot", "SYSTEMROOT", "COMSPEC", "ComSpec", "PATHEXT", "TEMP", "TMP", "TMPDIR")
    }
    environment["CODEX_HOME"] = str(tmp_path / ".codex")

    completed = subprocess.run(
        [sys.executable, str(REVIEW_VALIDATOR), "--help"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "usage" in completed.stdout.lower()
