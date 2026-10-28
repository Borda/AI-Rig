"""Cross-platform acceptance checks for shared workflow helpers."""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
COLLECT_DIFF = PLUGIN_ROOT / "shared" / "collect_diff.py"
CHALLENGE_VALIDATOR = PLUGIN_ROOT / "skills" / "challenge-resolve" / "validate_evidence.py"
RUN_GATES = PLUGIN_ROOT / "shared" / "run_gates.py"
SHARED_VALIDATOR = PLUGIN_ROOT / "shared" / "validate-artifacts.py"
REVIEW_VALIDATOR = PLUGIN_ROOT / "skills" / "code-review" / "validate_artifacts.py"
GATE_IDS = ("lint", "format", "types", "tests", "review")


def _load_module(path: Path, name: str) -> Any:
    """Load one standalone helper without requiring package imports."""
    assert path.is_file(), path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
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
        capture_output=True,
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


def test_source_snapshot_records_staged_deletion(tmp_path: Path) -> None:
    """Retain a missing source record when HEAD has a file removed from the index."""
    repository = tmp_path / "repository"
    repository.mkdir()
    _initialize_repository(repository)
    subprocess.run(["git", "-C", str(repository), "rm", "tracked.txt"], check=True, capture_output=True)
    patch = subprocess.run(
        ["git", "-C", str(repository), "diff", "HEAD", "--", "tracked.txt"],
        check=True,
        capture_output=True,
    ).stdout
    assert b"deleted file mode" in patch

    module = _load_module(COLLECT_DIFF, "codex_rig_staged_deletion_collect_diff")
    snapshot = module.capture_source_snapshot(repository, ["tracked.txt"])

    assert snapshot["scope_paths"] == ["tracked.txt"]
    assert snapshot["files"] == [
        {
            "path": "tracked.txt",
            "kind": "missing",
            "sha256": None,
            "executable": False,
            "encoding": "utf-8",
            "content": "",
        }
    ]
    validator = _load_module(CHALLENGE_VALIDATOR, "codex_rig_staged_deletion_validator")
    validator._require_supporting_source_records(snapshot, ["tracked.txt"])


def test_collect_diff_uses_only_git_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep collection shell-free and bind every subprocess to Git argv."""
    repository = tmp_path / "repository"
    repository.mkdir()
    _initialize_repository(repository)
    monkeypatch.chdir(repository)
    module = _load_module(COLLECT_DIFF, "codex_rig_portable_collect_diff")
    calls: list[list[str]] = []
    run = subprocess.run

    def _record_run(arguments: list[str], **kwargs: Any) -> Any:
        """Record shell-free collection argv while executing the actual Git commands."""
        assert not kwargs.get("shell", False)
        calls.append(arguments)
        return run(arguments, **kwargs)

    monkeypatch.setattr(module.subprocess, "run", _record_run)

    assert module.collect_diff("working-tree", "", tmp_path / "artifacts") == 0
    canonical_options = [
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "--src-prefix=a/",
        "--dst-prefix=b/",
        "--unified=3",
        "--inter-hunk-context=0",
        "--diff-algorithm=myers",
        "--no-renames",
        "--no-relative",
        "--ignore-submodules=none",
    ]
    assert calls == [
        ["git", "status", "--short"],
        *[
            ["git", "-c", "core.quotePath=true", "diff", option, "HEAD", *canonical_options]
            for option in ("--binary", "--name-only", "--stat", "--numstat")
        ],
        ["git", "-C", str(repository.resolve()), "--literal-pathspecs", "rev-parse", "--show-toplevel"],
        ["git", "-C", str(repository.resolve()), "ls-files", "--others", "--exclude-standard"],
    ]


def test_run_gates_selects_powershell_for_simulated_windows_and_bash_on_posix() -> None:
    """Bind command execution to the host-native shell family."""
    module = _load_module(RUN_GATES, "codex_rig_portable_run_gates")

    assert module.command_argv("Write-Output ok", platform="win32") == [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "Write-Output ok\nif ($?) { exit 0 } elseif ($LASTEXITCODE) { exit $LASTEXITCODE } else { exit 1 }",
    ]
    assert module.command_argv("printf ok", platform="linux") == ["bash", "-lc", "printf ok"]
    assert set(module.default_commands("win32")) == set(GATE_IDS)
    assert all("command -v" not in command for command in module.default_commands("win32").values())
    expected_executable = "powershell.exe" if sys.platform == "win32" else "bash"
    assert module.command_argv("native")[0] == expected_executable


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("powershell.exe") is None, reason="Windows PowerShell is unavailable")
@pytest.mark.parametrize(
    ("command", "expected"),
    [
        pytest.param("Write-Output ok", 0, id="cmdlet-success"),
        pytest.param("Write-Error failure", 1, id="cmdlet-failure"),
        pytest.param("exit 9", 9, id="explicit-exit"),
        pytest.param("throw 'failure'", 1, id="terminating-error"),
        pytest.param("cmd /c exit 7", 7, id="native-failure"),
        pytest.param("cmd /c exit 7 # trailing comment", 7, id="native-failure-comment"),
        pytest.param("cmd /c exit 7; Write-Output recovered", 0, id="recovered-native-failure"),
        pytest.param("cmd /c exit 0; Write-Error failure", 1, id="cmdlet-failure-after-native-success"),
    ],
)
def test_windows_gate_preserves_native_and_shell_status(command: str, expected: int) -> None:
    """Preserve native exit codes without masking PowerShell failures or deliberate recovery."""
    module = _load_module(RUN_GATES, "codex_rig_windows_gate_status")
    completed = subprocess.run(module.command_argv(command, platform="win32"), capture_output=True, check=False)
    assert completed.returncode == expected, completed.stderr


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


@pytest.mark.parametrize(
    "suffix",
    [pytest.param(b"\xc3\xa9", id="truncated-valid-utf8"), pytest.param(b"\xff", id="invalid-external-byte")],
)
def test_gate_report_survives_non_utf8_failure_output(tmp_path: Path, suffix: bytes) -> None:
    """Retain a canonical failed gate when bounded external stderr is not valid UTF-8."""
    script = tmp_path / "failure.py"
    script.write_text(
        f"import sys\nsys.stderr.buffer.write(b'a' * (1024 * 1024 - 1) + {suffix!r})\nsys.exit(127)\n",
        encoding="utf-8",
    )
    command = (
        f'& "{sys.executable}" "{script}"' if sys.platform == "win32" else shlex.join((sys.executable, str(script)))
    )
    output = tmp_path / "gates"
    completed = subprocess.run(
        [sys.executable, str(RUN_GATES), "--out", str(output), "--lint", command, *_skipped_gate_args()[2:]],
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 1, completed.stderr
    report = json.loads((output / "gates.json").read_text(encoding="utf-8"))
    lint = report["checks"][0]
    assert report["status"] == "fail"
    assert report["checks_failed"] == ["lint"]
    assert lint["status"] == "missing-command"
    assert lint["exit_code"] == 127
    assert "\ufffd" in lint["reason"]
    assert len((output / lint["stderr"]).read_bytes()) < 1024 * 1024 + 100


def test_run_gates_writes_exact_five_gate_artifacts(tmp_path: Path) -> None:
    """Emit the canonical five-gate JSON and per-check evidence natively."""
    output = tmp_path / "gates"

    completed = subprocess.run(
        [sys.executable, str(RUN_GATES), "--out", str(output), *_skipped_gate_args()],
        capture_output=True,
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
        capture_output=True,
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
    ("code", "expected_status"),
    [
        pytest.param(None, "not-applicable", id="skipped-gate"),
        pytest.param("pass", "pass", id="executed-gate"),
        pytest.param("raise SystemExit(3)", "fail", id="failed-gate"),
    ],
)
def test_run_gates_records_output_relative_logs_for_every_gate_outcome(
    tmp_path: Path, code: str | None, expected_status: str
) -> None:
    """Record output-relative logs whether a gate is skipped, runs and passes, or runs and fails.

    Each outcome returns through a different branch of run_check, so covering only one of them would let a partial
    revert restore the old host-native paths for gates that actually ran a command — that is, for every real run.
    """
    output = tmp_path / "gates"
    if code is None:
        arguments = ["--skip-lint", "not applicable here"]
    else:
        command = (
            f'& "{sys.executable}" -c "{code}"' if sys.platform == "win32" else shlex.join((sys.executable, "-c", code))
        )
        arguments = ["--lint", command]
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
        capture_output=True,
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


@pytest.mark.skipif(not hasattr(os, "killpg") or not hasattr(signal, "SIGTERM"), reason="requires POSIX process groups")
@pytest.mark.parametrize("leader_exits_before_timeout", [False, True])
def test_timeout_kills_descendant_after_leader_exits(tmp_path: Path, leader_exits_before_timeout: bool) -> None:
    """Stop a signal-resistant descendant even when its leader exits before the deadline.

    Inherited output pipes keep a command unfinished after its leader exits. Waiting for the leader first used to bypass
    the timeout and leave that descendant running, so the early-exit case must retain timeout evidence and prevent its
    delayed filesystem write. The other case protects escalation after SIGTERM exits the leader.
    """
    child = tmp_path / "child.py"
    child.write_text(
        "import pathlib, signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "time.sleep(3)\n"
        "pathlib.Path(sys.argv[1]).write_text('survived')\n",
        encoding="utf-8",
    )
    marker = tmp_path / "survived"
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import subprocess, sys, time\nsubprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])\n"
        + ("" if leader_exits_before_timeout else "time.sleep(20)\n"),
        encoding="utf-8",
    )
    module = _load_module(RUN_GATES, "timeout_descendant_runner")
    result, duration = module.execute_command(
        [sys.executable, str(parent), str(child), str(marker)], 1, tmp_path / "stdout", tmp_path / "stderr"
    )
    time.sleep(3)
    assert result == 124
    assert duration < 3
    assert "timeout: exceeded 1 seconds" in (tmp_path / "stderr").read_text(encoding="utf-8")
    assert not marker.exists()


def test_gate_waits_for_inherited_output_before_the_deadline(tmp_path: Path) -> None:
    """Preserve a successful descendant's output when its leader has already exited.

    An early leader exit alone is not a timeout. The capture deadline must allow a short-lived descendant to finish,
    retain its output on both streams, and preserve the leader's successful exit status.
    """
    module = _load_module(RUN_GATES, "inherited_output_runner")
    child = "import sys,time; time.sleep(0.2); print('child stdout'); print('child stderr', file=sys.stderr)"
    leader = f"import subprocess,sys; subprocess.Popen([sys.executable, '-c', {child!r}])"
    stdout, stderr = tmp_path / "stdout", tmp_path / "stderr"

    result, _ = module.execute_command([sys.executable, "-c", leader], 10, stdout, stderr)

    assert result == 0
    assert stdout.read_text(encoding="utf-8") == "child stdout\n"
    assert stderr.read_text(encoding="utf-8") == "child stderr\n"


def test_gate_output_capture_is_bounded_on_both_streams(tmp_path: Path) -> None:
    """Drain both streams and mark truncation without letting logs grow without bound."""
    module = _load_module(RUN_GATES, "bounded_output_runner")
    command = "import sys; sys.stdout.write('x' * 2097152); sys.stderr.write('y' * 2097152)"
    stdout, stderr = tmp_path / "stdout", tmp_path / "stderr"
    result, _ = module.execute_command([sys.executable, "-c", command], 10, stdout, stderr)
    assert result == 0
    for path, prefix in ((stdout, b"x"), (stderr, b"y")):
        content = path.read_bytes()
        assert content.startswith(prefix * 1024)
        assert len(content) <= 1024 * 1024 + 256
        assert b"truncated" in content.lower()


@pytest.mark.parametrize("wrapper_name", ["collect-diff.sh", "run-gates.sh"])
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
