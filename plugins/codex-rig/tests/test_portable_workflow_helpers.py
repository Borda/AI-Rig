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
