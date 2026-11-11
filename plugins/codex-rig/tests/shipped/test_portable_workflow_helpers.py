"""Cross-platform acceptance checks for shared workflow helpers."""

from __future__ import annotations

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
from _modules import load_module

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
COLLECT_DIFF = PLUGIN_ROOT / "shared" / "collect_diff.py"
CHALLENGE_VALIDATOR = PLUGIN_ROOT / "skills" / "challenge-resolve" / "validate_evidence.py"
RUN_GATES = PLUGIN_ROOT / "shared" / "run_gates.py"
SHARED_VALIDATOR = PLUGIN_ROOT / "shared" / "validate-artifacts.py"
REVIEW_VALIDATOR = PLUGIN_ROOT / "skills" / "code-review" / "validate_artifacts.py"
GATE_IDS = ("lint", "format", "types", "tests", "review")


def _load_module(path: Path, name: str) -> Any:
    """Load one standalone helper without requiring package imports."""
    assert path.is_file(), path
    sys.path.insert(0, str(path.parent))
    try:
        return load_module(name, path)
    finally:
        sys.path.pop(0)


def _initialize_repository(root: Path) -> None:
    """Create one committed Git repository for diff collection tests."""
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "tracked.txt").write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "tracked.txt"], check=True)
    environment = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Codex Rig Test",
        "GIT_AUTHOR_EMAIL": "codex-rig@example.invalid",
        "GIT_COMMITTER_NAME": "Codex Rig Test",
        "GIT_COMMITTER_EMAIL": "codex-rig@example.invalid",
    }
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "fixture"], check=True, env=environment)


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


def _native_command(*arguments: str) -> str:
    """Quote the running interpreter plus arguments as a gate command for the host's native shell.

    Example:
        >>> _native_command("-c", "pass").endswith(("-c pass", '-c "pass"'))
        True
    """
    if sys.platform == "win32":
        quoted = (argument if argument.startswith("-") else f'"{argument}"' for argument in arguments)
        return f'& "{sys.executable}" ' + " ".join(quoted)
    return shlex.join((sys.executable, *arguments))


def _run_gates_in(workspace: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run the gate runner from a workspace directory and capture its text output."""
    return subprocess.run(
        [sys.executable, str(RUN_GATES), *arguments], cwd=workspace, capture_output=True, text=True, check=False
    )


@pytest.fixture
def gates_run(tmp_path: Path) -> tuple[Path, Path]:
    """Produce a finished all-skipped gate run at `workspace/run` and an unrelated `elsewhere` directory."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "elsewhere").mkdir()
    completed = _run_gates_in(workspace, "--out", "run", *_skipped_gate_args())
    assert completed.returncode == 0, completed.stderr
    return workspace, workspace / "run"


def _rewrite_first_command_path(output: Path, command_path: str) -> None:
    """Replace the first recorded gate command-log path in a finished run's gates.json."""
    gates_path = output / "gates.json"
    payload = json.loads(gates_path.read_text(encoding="utf-8"))
    payload["checks"][0]["command_path"] = command_path
    gates_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def test_collect_diff_runs_natively_and_writes_complete_artifacts(tmp_path: Path) -> None:
    """Collect tracked and untracked evidence without invoking Bash.

    A committed file is modified and a new file is added in a real repository; the helper runs as a script and must
    leave exactly the six evidence files, with the patch showing both sides of the edit.
    """
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

    patch = (output / "diff.patch").read_text(encoding="utf-8")
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
    assert "-before" in patch
    assert "+after" in patch


def test_source_snapshot_records_staged_deletion(tmp_path: Path) -> None:
    """Retain a missing source record when HEAD has a file removed from the index.

    A tracked file is removed with `git rm`; the snapshot must keep an explicit `missing` record for it rather than
    dropping the path, and the challenge validator must accept that record as supporting source evidence.
    """
    repository = tmp_path / "repository"
    repository.mkdir()
    _initialize_repository(repository)
    subprocess.run(["git", "-C", str(repository), "rm", "tracked.txt"], check=True, capture_output=True)
    patch = subprocess.run(
        ["git", "-C", str(repository), "diff", "HEAD", "--", "tracked.txt"], check=True, capture_output=True
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
    """Keep collection shell-free and bind every subprocess to Git argv.

    The real Git commands still run in a real repository; only their argument vectors are recorded, so a regression to a
    shell string or an extra, reordered or reconfigured Git call changes the recorded sequence.
    """
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
        ["git", "-c", "core.fsmonitor=false", "status", "--short"],
        *[
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.quotePath=true",
                "diff",
                option,
                "HEAD",
                *canonical_options,
            ]
            for option in ("--binary", "--name-only", "--stat", "--numstat")
        ],
        [
            "git",
            "-c",
            "core.fsmonitor=false",
            "-C",
            str(repository.resolve()),
            "--literal-pathspecs",
            "rev-parse",
            "--show-toplevel",
        ],
        [
            "git",
            "-c",
            "core.fsmonitor=false",
            "-C",
            str(repository.resolve()),
            "ls-files",
            "--others",
            "--exclude-standard",
        ],
    ]


def test_command_argv_selects_powershell_for_simulated_windows_and_bash_on_posix() -> None:
    """Bind command execution to the host-native shell family.

    The shell family is chosen from the platform name, so the Windows form is checked on every host without a Windows
    runner; the unspecified-platform call must follow the host actually running the tests.
    """
    module = _load_module(RUN_GATES, "codex_rig_portable_run_gates")

    assert module.command_argv("Write-Output ok", platform="win32") == [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "Write-Output ok\nif ($?) { exit 0 } elseif ($LASTEXITCODE) { exit $LASTEXITCODE } else { exit 1 }",
    ]
    assert module.command_argv("printf ok", platform="linux") == ["bash", "-c", "printf ok"]
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
    """Preserve native exit codes without masking PowerShell failures or deliberate recovery.

    Runs the wrapped command in real Windows PowerShell; the wrapper must report a native failure, a failing cmdlet and
    a deliberate recovery with the exit status a person reading the script would expect.
    """
    module = _load_module(RUN_GATES, "codex_rig_windows_gate_status")
    completed = subprocess.run(module.command_argv(command, platform="win32"), capture_output=True, check=False)
    assert completed.returncode == expected, completed.stderr


def test_terminate_process_uses_taskkill_for_simulated_windows_trees(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use the native Windows process-tree terminator before the kill fallback.

    A fake still-running process is terminated with the simulated Windows platform; the single recorded command must be
    `taskkill /T /F` on its pid, with the follow-up wait bounded to two seconds.
    """
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
    """Retain a canonical failed gate when bounded external stderr is not valid UTF-8.

    The failing command writes just under 1 MiB of stderr ending in half a UTF-8 character or an invalid byte; the
    report must still be written with the failure reason decoded lossily and the stored log kept near the bound.
    """
    script = tmp_path / "failure.py"
    script.write_text(
        f"import sys\nsys.stderr.buffer.write(b'a' * (1024 * 1024 - 1) + {suffix!r})\nsys.exit(127)\n",
        encoding="utf-8",
    )
    output = tmp_path / "gates"

    completed = subprocess.run(
        [
            sys.executable,
            str(RUN_GATES),
            "--out",
            str(output),
            "--lint",
            _native_command(str(script)),
            *_skipped_gate_args()[2:],
        ],
        capture_output=True,
        timeout=30,
        check=False,
    )

    report = json.loads((output / "gates.json").read_text(encoding="utf-8"))
    lint = report["checks"][0]
    assert completed.returncode == 1, completed.stderr
    assert report["status"] == "fail"
    assert report["checks_failed"] == ["lint"]
    assert lint["status"] == "missing-command"
    assert lint["exit_code"] == 127
    assert "�" in lint["reason"]
    assert len((output / lint["stderr"]).read_bytes()) < 1024 * 1024 + 100


def test_run_gates_writes_exact_five_gate_artifacts(tmp_path: Path) -> None:
    """Emit the canonical five-gate JSON and per-check evidence natively.

    With every gate skipped as not applicable, the runner prints the report path and writes the JSON, text, JSONL and
    per-check command, stdout and stderr files with exactly the documented keys and statuses.
    """
    output = tmp_path / "gates"

    completed = subprocess.run(
        [sys.executable, str(RUN_GATES), "--out", str(output), *_skipped_gate_args()],
        capture_output=True,
        text=True,
        check=False,
    )

    payload = json.loads((output / "gates.json").read_text(encoding="utf-8"))
    jsonl = [json.loads(line) for line in (output / "gates.checks.jsonl").read_text().splitlines()]
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == f"{output / 'gates.json'}\n"
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
    assert jsonl == payload["checks"]
    assert all(
        (output / "checks" / f"{gate_id}.{suffix}.txt").is_file()
        for gate_id in GATE_IDS
        for suffix in ("command", "stdout", "stderr")
    )


def test_run_gates_records_log_paths_relative_to_the_output_directory(gates_run: tuple[Path, Path]) -> None:
    """Record every gate log as a POSIX path under the output directory, never as a host-native one.

    The recorded string is the only coordinate a later reader gets; the producer's working directory is not stored
    anywhere, so a path that needs it cannot be resolved by anyone else.
    """
    _, output = gates_run

    payload = json.loads((output / "gates.json").read_text(encoding="utf-8"))

    recorded = [check[key] for check in payload["checks"] for key in ("command_path", "stdout", "stderr")]
    assert not any(Path(path).is_absolute() for path in recorded)
    assert not any("\\" in path for path in recorded)
    assert all(path.startswith("checks/") for path in recorded)
    assert all((output / path).is_file() for path in recorded)


@pytest.mark.parametrize(
    ("lint_arguments", "expected_status"),
    [
        pytest.param(["--skip-lint", "not applicable here"], "not-applicable", id="skipped-gate"),
        pytest.param(["--lint", _native_command("-c", "pass")], "pass", id="executed-gate"),
        pytest.param(["--lint", _native_command("-c", "raise SystemExit(3)")], "fail", id="failed-gate"),
    ],
)
def test_run_gates_records_output_relative_logs_for_every_gate_outcome(
    tmp_path: Path, lint_arguments: list[str], expected_status: str
) -> None:
    """Record output-relative logs whether a gate is skipped, runs and passes, or runs and fails.

    Each outcome returns through a different branch of run_check, so covering only one of them would let a partial
    revert restore the old host-native paths for gates that actually ran a command — that is, for every real run.
    """
    output = tmp_path / "gates"
    others = [argument for gate_id in GATE_IDS[1:] for argument in (f"--skip-{gate_id}", "n/a")]

    subprocess.run(
        [sys.executable, str(RUN_GATES), "--out", str(output), *lint_arguments, *others],
        capture_output=True,
        check=False,
    )

    lint = next(
        check
        for check in json.loads((output / "gates.json").read_text(encoding="utf-8"))["checks"]
        if check["id"] == "lint"
    )
    recorded = {key: lint[key] for key in ("command_path", "stdout", "stderr")}
    assert lint["status"] == expected_status
    assert recorded == {
        "command_path": "checks/lint.command.txt",
        "stdout": "checks/lint.stdout.txt",
        "stderr": "checks/lint.stderr.txt",
    }
    assert all((output / path).is_file() for path in recorded.values())


class TestGateValidation:
    """Validation of a finished gate run, independent of where the reader stands."""

    @pytest.mark.parametrize("reader_directory", ["workspace", "elsewhere"])
    def test_ignores_the_callers_working_directory(
        self, gates_run: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, reader_directory: str
    ) -> None:
        """Return the same verdict for one finished artifact from the producing workspace and from elsewhere."""
        workspace, output = gates_run
        module = _load_module(SHARED_VALIDATOR, "codex_rig_shared_validate_artifacts")
        monkeypatch.chdir(workspace.parent / reader_directory)

        assert module._validate_gates(output)["status"] == "pass"

    @pytest.mark.parametrize("reader_directory", ["workspace", "elsewhere"])
    def test_accepts_a_legacy_ancestor_relative_log_from_any_directory(
        self, gates_run: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, reader_directory: str
    ) -> None:
        """Accept a gate log recorded relative to an ancestor of the output directory, wherever the reader runs.

        Runs produced before logs were recorded relative to the output directory stored a path relative to whichever
        directory produced them, and `--complete-run` revalidates such a run long afterwards. Rejecting that form would
        strand finished work, so it resolves from `out_dir`'s ancestors — never from the reader's own directory, which
        is what made the verdict vary by caller.
        """
        workspace, output = gates_run
        _rewrite_first_command_path(output, "run/checks/lint.command.txt")
        module = _load_module(SHARED_VALIDATOR, "codex_rig_shared_validate_artifacts_legacy")
        monkeypatch.chdir(workspace.parent / reader_directory)

        assert module._validate_gates(output)["status"] == "pass"

    def test_rejects_a_log_resolving_outside_the_output_directory(
        self, gates_run: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reject a gate log that exists but sits outside the output directory it claims to document.

        Trying ancestors widens where a relative name may resolve, so the containment check is what keeps a same-named
        file elsewhere on disk from being accepted as this run's evidence.
        """
        workspace, output = gates_run
        (workspace / "outside.txt").write_text("not this run's evidence\n", encoding="utf-8")
        _rewrite_first_command_path(output, "outside.txt")
        module = _load_module(SHARED_VALIDATOR, "codex_rig_shared_validate_artifacts_outside")
        monkeypatch.chdir(workspace)

        with pytest.raises(SystemExit) as failure:
            module._validate_gates(output)

        assert str(failure.value) == "gate-check-log-outside-output:0:command_path"


def test_run_gates_times_out_and_terminates_native_process(tmp_path: Path) -> None:
    """Stop a timed-out command tree and retain canonical timeout evidence.

    A 30-second sleeper runs under a one-second gate timeout; the runner must exit 124 well inside the harness's own
    ten-second bound and record the timeout in the report and the gate's stderr log.
    """
    output = tmp_path / "timeout"
    sleeper = _native_command("-c", "import time; time.sleep(30)")

    completed = subprocess.run(
        [
            sys.executable,
            str(RUN_GATES),
            "--out",
            str(output),
            "--timeout-seconds",
            "1",
            "--lint",
            sleeper,
            *_skipped_gate_args()[2:],
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    payload = json.loads((output / "gates.json").read_text(encoding="utf-8"))
    assert completed.returncode == 124
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

    time.sleep(3)  # the descendant's delayed write would land inside this window if it survived
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
    """Drain both streams and mark truncation without letting logs grow without bound.

    A command writes 2 MiB to each of stdout and stderr; both logs must start with the real output, stay near the 1 MiB
    bound and say they were truncated, and the command's own exit status must survive.
    """
    module = _load_module(RUN_GATES, "bounded_output_runner")
    command = "import sys; sys.stdout.write('x' * 2097152); sys.stderr.write('y' * 2097152)"
    stdout, stderr = tmp_path / "stdout", tmp_path / "stderr"

    result, _ = module.execute_command([sys.executable, "-c", command], 10, stdout, stderr)

    logs = (stdout.read_bytes(), stderr.read_bytes())
    assert result == 0
    assert logs[0].startswith(b"x" * 1024)
    assert logs[1].startswith(b"y" * 1024)
    assert all(len(content) <= 1024 * 1024 + 256 for content in logs)
    assert all(b"truncated" in content.lower() for content in logs)


@pytest.mark.parametrize("wrapper_name", ["collect-diff.sh", "run-gates.sh"])
def test_portable_helpers_do_not_ship_shell_compatibility_wrappers(wrapper_name: str) -> None:
    """Keep the plugin helper surface Python-only across supported platforms.

    Shell wrappers do not run on native Windows, so neither retired wrapper may reappear beside the Python helpers.
    """
    assert not (PLUGIN_ROOT / "shared" / wrapper_name).exists()


def test_review_validator_help_survives_a_host_without_home_variables(tmp_path: Path) -> None:
    """Reject a home lookup on the argparse default path, which Windows resolves from USERPROFILE.

    The validator's `--help` runs with an environment stripped to the variables a bare Windows host keeps; it must still
    print usage and exit zero instead of failing while computing a default under the user's home directory.
    """
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
