#!/usr/bin/env python3
"""Run the canonical five Codex Rig quality gates without Bash dependencies.

## Purpose

Execute configured lint, format, type, test, and review checks while preserving per-gate evidence and timeout
classification. An optional expected Git head binds each executable check to a clean source observation before and after
its command. An optional absolute worktree path binds those observations and gate commands to one checkout. The gate
runner turns each command into a named record that result validation can reconcile with the workflow verdict.

## Scope

It runs local commands supplied by a workflow and writes gate records; it does not decide release readiness, repair
source state, or hide failed output. Each gate must have either a command or an explicit skip reason, and commands are
executed independently with a per-gate timeout. Same-directory reruns archive previous runner-owned receipts under
``gate-attempts/<NNN>`` before writing; a failed applicable check cannot become skipped. Incomplete prior state blocks
overwrite and requires diagnosis rather than discarding evidence.

## Usage

Run ``python run_gates.py --out <directory>`` with explicit commands or documented skip reasons for each applicable
gate. Add ``--expected-head <full-lowercase-sha>`` to require a matching clean Git checkout around executable checks.
Add ``--worktree <absolute-path>`` with the expected head to select the exact checkout used for source inspection and
commands, while retaining artifacts under ``--out``.
For Python project tests, add ``--pytest-python <absolute-executable>``, one or more ``--pytest-import <module>``
values, and optional ``--pytest-args-json <array>``. This opt-in mode calls pytest and inspects imported modules in the
same Python process; it cannot be combined with a free-form ``--tests`` command.
Commands can come from the gate flags or matching environment variables such as ``LINT_CMD``. Each gate applies the
value supplied through ``--timeout-seconds`` separately.

## Used by

Implement and related artifact workflows, result validation, and portable-gate acceptance tests use this runner. The
five canonical IDs are fixed as ``lint``, ``format``, ``types``, ``tests``, and ``review``, so consumers can compare
records without interpreting arbitrary gate names.

## Outputs

It writes ``gates.json``, ``gates.txt``, ``failed.txt``, ``gates.checks.jsonl``, and per-gate command/stdout/stderr
files. Worktree-bound runs record the selected checkout at the top level and in each executable source receipt. Guarded
executable records include their expected head plus before/after source receipts. Records distinguish
pass, fail, timeout, missing command, and ``not-applicable`` states, while captured output is bounded to protect
artifact size.
Import-bound test records contain the interpreter environment and each module's resolved, Git-tracked source origin.

## Failure

A missing command, non-zero command result, timeout, source mismatch or dirtiness, Git inspection failure, malformed
requested gate, or unwritable artifact directory is retained as explicit gate evidence. The CLI returns ``1`` for failed
gates, ``124`` for a timeout, and ``2`` for invalid input such as a newline in a skip reason, allowing callers to
classify the run without parsing prose.
An imported module outside the selected worktree or absent from its Git index fails the test gate; a module not loaded
in the pytest process or without a file origin leaves import proof inconclusive and also fails closed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, NamedTuple


GATE_IDS = ("lint", "format", "types", "tests", "review")
DEFAULT_TIMEOUT_SECONDS = 900
CHECKS_DIRNAME = "checks"
SOURCE_INSPECTION_TIMEOUT_SECONDS = 30
PYTHON_MODULE_PATTERN = re.compile(r"[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)*")


class PythonTestSpec(NamedTuple):
    """Describe one in-process pytest invocation with import origins to verify."""

    interpreter: Path
    modules: tuple[str, ...]
    arguments: tuple[str, ...]


def positive_integer(value: str) -> int:
    """Accept one strictly positive timeout value."""
    if not value.isdigit() or int(value) < 1:
        raise argparse.ArgumentTypeError(f"invalid-timeout-seconds:{value}")
    return int(value)


def full_git_sha(value: str) -> str:
    """Accept a lowercase SHA-1 or SHA-256 Git object identifier."""
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value) is None:
        raise argparse.ArgumentTypeError(f"invalid-expected-head:{value}")
    return value


def parse_args() -> argparse.Namespace:
    """Parse the stable run-gates command-line contract."""
    parser = argparse.ArgumentParser(
        prog="run_gates.py",
        description=(
            "Run lint, format, types, tests, and review gates and write gates.json, "
            "gates.txt, failed.txt, gates.checks.jsonl, and per-gate logs."
        ),
        epilog=(
            "Each gate requires either a command or an explicit skip reason. Exit 0 means all applicable "
            "gates passed, 1 means a gate failed, 124 means timeout, and 2 means invalid CLI input."
        ),
    )
    parser.add_argument("--out", required=True, type=Path, help="Required artifact directory.")
    for gate_id in GATE_IDS:
        parser.add_argument(
            f"--{gate_id}",
            default=os.environ.get(f"{gate_id.upper()}_CMD", ""),
            help=f"{gate_id.capitalize()} command.",
        )
        parser.add_argument(
            f"--skip-{gate_id}",
            default="",
            help=f"Explicit reason the {gate_id} gate is not applicable.",
        )
    parser.add_argument(
        "--timeout-seconds",
        default=os.environ.get("GATE_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS)),
        type=positive_integer,
        help="Per-gate timeout; defaults to 900 or GATE_TIMEOUT_SECONDS.",
    )
    parser.add_argument(
        "--expected-head",
        type=full_git_sha,
        help="Optional clean Git HEAD required before and after every executable gate.",
    )
    parser.add_argument(
        "--worktree",
        type=Path,
        help="Absolute checkout root for all source inspections and executable gates; requires --expected-head.",
    )
    parser.add_argument("--pytest-python", type=Path, help="Absolute Python executable for import-bound pytest tests.")
    parser.add_argument(
        "--pytest-import",
        action="append",
        default=[],
        help="Module that pytest must actually import from the worktree.",
    )
    parser.add_argument("--pytest-args-json", help="JSON array of arguments passed directly to in-process pytest.")
    arguments = parser.parse_args()
    if arguments.worktree is not None:
        if not arguments.worktree.is_absolute() or not arguments.worktree.is_dir():
            parser.error("invalid-worktree: expected an existing absolute directory")
        if arguments.expected_head is None:
            parser.error("invalid-worktree: --expected-head is required")
        arguments.worktree = arguments.worktree.resolve()
    if arguments.pytest_python is not None:
        if arguments.worktree is None:
            parser.error("invalid-pytest-python: --worktree is required")
        if not arguments.pytest_python.is_absolute() or not arguments.pytest_python.is_file():
            parser.error("invalid-pytest-python: expected an existing absolute executable")
        if arguments.tests or arguments.skip_tests:
            parser.error("invalid-pytest-python: free-form or skipped tests conflict with import-bound pytest")
        if not arguments.pytest_import or any(
            PYTHON_MODULE_PATTERN.fullmatch(name) is None for name in arguments.pytest_import
        ):
            parser.error("invalid-pytest-import: at least one valid module name is required")
        try:
            pytest_arguments = json.loads(arguments.pytest_args_json or "[]")
        except json.JSONDecodeError:
            parser.error("invalid-pytest-args-json: expected an array of strings")
        if not isinstance(pytest_arguments, list) or any(not isinstance(value, str) for value in pytest_arguments):
            parser.error("invalid-pytest-args-json: expected an array of strings")
        arguments.python_test = PythonTestSpec(
            arguments.pytest_python.absolute(), tuple(dict.fromkeys(arguments.pytest_import)), tuple(pytest_arguments)
        )
    else:
        if arguments.pytest_import or arguments.pytest_args_json is not None:
            parser.error("invalid-pytest-python: --pytest-python is required")
        arguments.python_test = None
    return arguments


def command_argv(command: str, platform: str | None = None) -> list[str]:
    """Return the native shell argv for one configured command string."""
    host = sys.platform if platform is None else platform
    if host == "win32":
        # PowerShell -Command normalizes native failures to 1 unless explicitly returned.
        # Check shell success first so a recovered native failure does not override it.
        command += "\nif ($?) { exit 0 } elseif ($LASTEXITCODE) { exit $LASTEXITCODE } else { exit 1 }"
        return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command]
    return ["bash", "-lc", command]


def default_commands(platform: str) -> dict[str, str]:
    """Return platform-valid defaults for every required command gate."""
    if platform == "win32":
        return {
            "lint": (
                "if (Get-Command ruff -ErrorAction SilentlyContinue) { ruff check . } "
                "elseif (Get-Command uv -ErrorAction SilentlyContinue) { uv run --no-sync ruff check . } "
                "else { [Console]::Error.WriteLine('missing-command:ruff'); exit 127 }"
            ),
            "format": (
                "if (Get-Command ruff -ErrorAction SilentlyContinue) { ruff format --check . } "
                "elseif (Get-Command uv -ErrorAction SilentlyContinue) { uv run --no-sync ruff format --check . } "
                "else { [Console]::Error.WriteLine('missing-command:ruff'); exit 127 }"
            ),
            "types": (
                "if (Get-Command mypy -ErrorAction SilentlyContinue) { mypy src/ } "
                "elseif (Get-Command uv -ErrorAction SilentlyContinue) { uv run --no-sync mypy src/ } "
                "else { [Console]::Error.WriteLine('missing-command:mypy'); exit 127 }"
            ),
            "tests": (
                "if (Get-Command pytest -ErrorAction SilentlyContinue) { pytest -q } "
                "elseif (Get-Command uv -ErrorAction SilentlyContinue) { uv run --no-sync pytest -q } "
                "else { [Console]::Error.WriteLine('missing-command:pytest'); exit 127 }"
            ),
            "review": "git diff --check",
        }
    return {
        "lint": (
            "if command -v ruff >/dev/null 2>&1; then ruff check .; "
            "elif command -v uv >/dev/null 2>&1; then uv run --no-sync ruff check .; "
            'else echo "missing-command:ruff" >&2; exit 127; fi'
        ),
        "format": (
            "if command -v ruff >/dev/null 2>&1; then ruff format --check .; "
            "elif command -v uv >/dev/null 2>&1; then uv run --no-sync ruff format --check .; "
            'else echo "missing-command:ruff" >&2; exit 127; fi'
        ),
        "types": (
            "if command -v mypy >/dev/null 2>&1; then mypy src/; "
            "elif command -v uv >/dev/null 2>&1; then uv run --no-sync mypy src/; "
            'else echo "missing-command:mypy" >&2; exit 127; fi'
        ),
        "tests": (
            "if command -v pytest >/dev/null 2>&1; then pytest -q; "
            "elif command -v uv >/dev/null 2>&1; then uv run --no-sync pytest -q; "
            'else echo "missing-command:pytest" >&2; exit 127; fi'
        ),
        "review": "git diff --check",
    }


def terminate_process(process: subprocess.Popen[str], platform: str) -> None:
    """Terminate a timed-out command and its descendants on the current platform."""
    if process.poll() is not None:
        return
    if platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    try:
        process.wait(timeout=2)
        return
    except subprocess.TimeoutExpired:
        pass
    if platform == "win32":
        process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
    process.wait()


def execute_command(
    command: str | list[str], timeout: int, stdout_path: Path, stderr_path: Path, worktree: Path | None = None
) -> tuple[int, float]:
    """Execute one command with bounded runtime and captured output."""
    started = time.monotonic()
    platform = sys.platform
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if platform == "win32" else 0
    argv = command_argv(command, platform) if isinstance(command, str) else command
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
        try:
            process = subprocess.Popen(
                argv,
                stdout=stdout,
                stderr=stderr,
                text=True,
                cwd=worktree,
                start_new_session=platform != "win32",
                creationflags=creationflags,
            )
        except FileNotFoundError:
            stderr.write(f"missing-command:{argv[0]}\n")
            return 127, time.monotonic() - started
        try:
            return process.wait(timeout=timeout), time.monotonic() - started
        except subprocess.TimeoutExpired:
            terminate_process(process, platform)
            stderr.write(f"timeout: exceeded {timeout} seconds\n")
            return 124, time.monotonic() - started


def inspect_imported_module(name: str, worktree: Path) -> dict[str, Any]:
    """Check one module actually loaded by pytest against tracked checkout source."""
    module = sys.modules.get(name)
    if module is None:
        return {"origin": None, "tracked": False, "status": "inconclusive", "reason": "module-not-imported"}
    raw_origin = getattr(module, "__file__", None)
    if not isinstance(raw_origin, str):
        return {"origin": None, "tracked": False, "status": "inconclusive", "reason": "module-has-no-file"}
    origin = Path(raw_origin).resolve()
    try:
        relative = origin.relative_to(worktree).as_posix()
    except ValueError:
        return {"origin": origin.as_posix(), "tracked": False, "status": "fail", "reason": "origin-outside-worktree"}
    try:
        tracked = (
            subprocess.run(
                ["git", "ls-files", "--error-unmatch", "--", relative],
                cwd=worktree,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=SOURCE_INSPECTION_TIMEOUT_SECONDS,
            ).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        tracked = False
    return {
        "origin": origin.as_posix(),
        "tracked": tracked,
        "status": "pass" if tracked else "fail",
        "reason": None if tracked else "origin-not-tracked",
    }


def run_python_tests_child(arguments: list[str]) -> int:
    """Run pytest and inspect imports inside the same interpreter and process."""
    if len(arguments) != 5:
        print("invalid-python-test-child-arguments", file=sys.stderr)
        return 2
    worktree = Path(arguments[0]).resolve()
    proof_path = Path(arguments[1])
    modules = json.loads(arguments[2])
    pytest_arguments = json.loads(arguments[3])
    invoked_interpreter = arguments[4]
    # Pytest is optional for all other gate modes and belongs to the selected test environment.
    import pytest

    test_exit_code = int(pytest.main(pytest_arguments))
    origins = {name: inspect_imported_module(name, worktree) for name in modules}
    statuses = {entry["status"] for entry in origins.values()}
    status = "fail" if "fail" in statuses else "inconclusive" if "inconclusive" in statuses else "pass"
    proof = {
        "mode": "in-process-pytest",
        "status": status,
        "invoked_interpreter": invoked_interpreter,
        "runtime_interpreter": Path(sys.executable).absolute().as_posix(),
        "sys_prefix": Path(sys.prefix).absolute().as_posix(),
        "worktree": worktree.as_posix(),
        "modules": origins,
    }
    proof_path.write_text(json.dumps(proof, indent=2) + "\n", encoding="utf-8", newline="\n")
    return test_exit_code if test_exit_code else 0 if status == "pass" else 1


def check_paths(gate_id: str, out_dir: Path) -> tuple[dict[str, Path], dict[str, str]]:
    """Return the log paths to write plus the names recorded for them in gates.json.

    The recorded names are relative to the run's output directory and always use POSIX separators, so a
    reader resolves them against ``--out`` alone. A path relative to whoever happened to run this script
    is not a portable coordinate: the producer's working directory is recorded nowhere, so the same
    artifact would validate from one directory and fail from another.

    Both mappings come from one directory argument, so the file written and the name recorded for it
    cannot describe different locations.
    """
    names = ("command", "stdout", "stderr")
    checks_dir = out_dir / CHECKS_DIRNAME
    paths = {name: checks_dir / f"{gate_id}.{name}.txt" for name in names}
    recorded = {name: paths[name].relative_to(out_dir).as_posix() for name in names}
    return paths, recorded


def skipped_check(gate_id: str, reason: str, paths: dict[str, Path], recorded: dict[str, str]) -> dict[str, Any]:
    """Write and return one explicit not-applicable gate result."""
    paths["stdout"].write_text("", encoding="utf-8")
    paths["stderr"].write_text(f"not-applicable:{reason}\n", encoding="utf-8")
    paths["command"].write_text(f"not-applicable: {reason}\n", encoding="utf-8")
    return {
        "id": gate_id,
        "status": "not-applicable",
        "exit_code": 0,
        "duration_seconds": 0.0,
        "command_path": recorded["command"],
        "stdout": recorded["stdout"],
        "stderr": recorded["stderr"],
        "reason": reason,
    }


def inspect_source(worktree: Path | None = None) -> dict[str, str]:
    """Return the selected Git checkout head and status, or a failed inspection receipt."""
    empty = {"head": "", "status": ""}
    try:
        if worktree is not None:
            root = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=worktree,
                capture_output=True,
                text=True,
                check=False,
                timeout=SOURCE_INSPECTION_TIMEOUT_SECONDS,
            )
            if root.returncode != 0 or not root.stdout.strip() or Path(root.stdout.strip()).resolve() != worktree:
                return {**empty, "error": "selected-worktree-is-not-checkout-root"}
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=False,
            timeout=SOURCE_INSPECTION_TIMEOUT_SECONDS,
        )
        if head.returncode != 0:
            detail = head.stderr.strip() or f"exit {head.returncode}"
            return {**empty, "error": f"git-rev-parse-failed:{detail}"}
        # Repository ignore settings must not conceal changed release dependencies.
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none"],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=False,
            timeout=SOURCE_INSPECTION_TIMEOUT_SECONDS,
        )
        if status.returncode != 0:
            detail = status.stderr.strip() or f"exit {status.returncode}"
            return {**empty, "error": f"git-status-failed:{detail}"}
    except (OSError, subprocess.TimeoutExpired) as error:
        return {**empty, "error": f"git-inspection-failed:{error}"}
    return {"head": head.stdout.strip(), "status": status.stdout.rstrip("\r\n")}


def source_guard_error(snapshot: dict[str, str], expected_head: str) -> str | None:
    """Explain why one source observation cannot authorize a gate command."""
    if "error" in snapshot:
        return f"source-inspection-failed:{snapshot['error']}"
    if snapshot["head"] != expected_head:
        return f"source-head-mismatch:expected {expected_head}, observed {snapshot['head']}"
    if snapshot["status"]:
        return "source-dirty:git status --porcelain=v1 --untracked-files=all --ignore-submodules=none was non-empty"
    return None


def append_stderr(path: Path, message: str) -> None:
    """Append one source-guard failure to the gate's captured standard error."""
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(f"{message}\n")


def run_check(
    gate_id: str,
    command: str,
    skip_reason: str,
    timeout: int,
    out_dir: Path,
    expected_head: str | None,
    worktree: Path | None = None,
    python_test: PythonTestSpec | None = None,
) -> dict[str, Any]:
    """Run one gate or record its explicit not-applicable status."""
    paths, recorded = check_paths(gate_id, out_dir)
    if skip_reason:
        return skipped_check(gate_id, skip_reason, paths, recorded)
    if not command and not (gate_id == "tests" and python_test is not None):
        paths["stdout"].write_text("", encoding="utf-8")
        paths["stderr"].write_text("missing command\n", encoding="utf-8")
        paths["command"].write_text("", encoding="utf-8")
        return {
            "id": gate_id,
            "status": "missing-command",
            "exit_code": 127,
            "duration_seconds": 0.0,
            "command_path": recorded["command"],
            "stdout": recorded["stdout"],
            "stderr": recorded["stderr"],
            "reason": "no command configured for required gate",
        }

    python_test_argv: list[str] | None = None
    proof_path = out_dir / CHECKS_DIRNAME / "tests.python-imports.json"
    if gate_id == "tests" and python_test is not None:
        assert worktree is not None
        proof_path.unlink(missing_ok=True)
        python_test_argv = [
            str(python_test.interpreter),
            str(Path(__file__).resolve()),
            "--_python-tests-child",
            worktree.as_posix(),
            str(proof_path.resolve()),
            json.dumps(python_test.modules),
            json.dumps(python_test.arguments),
            python_test.interpreter.as_posix(),
        ]
        command = f"argv-json:{json.dumps(python_test_argv)}"
    paths["command"].write_text(f"{command}\n", encoding="utf-8")
    source: dict[str, Any] | None = None
    if expected_head is not None:
        before = inspect_source(worktree)
        source = {"expected_head": expected_head, "before": before, "after": None}
        if worktree is not None:
            source["worktree"] = worktree.as_posix()
        error = source_guard_error(before, expected_head)
        if error is not None:
            paths["stdout"].write_text("", encoding="utf-8")
            append_stderr(paths["stderr"], error)
            return {
                "id": gate_id,
                "status": "fail",
                "exit_code": 1,
                "duration_seconds": 0.0,
                "command_path": recorded["command"],
                "stdout": recorded["stdout"],
                "stderr": recorded["stderr"],
                "source": source,
            }

    exit_code, duration = execute_command(
        python_test_argv or command, timeout, paths["stdout"], paths["stderr"], worktree
    )
    status = (
        "pass"
        if exit_code == 0
        else "timeout"
        if exit_code == 124
        else "missing-command"
        if exit_code == 127
        else "fail"
    )
    result: dict[str, Any] = {
        "id": gate_id,
        "status": status,
        "exit_code": exit_code,
        "duration_seconds": duration,
        "command_path": recorded["command"],
        "stdout": recorded["stdout"],
        "stderr": recorded["stderr"],
    }
    if python_test_argv is not None:
        try:
            proof = json.loads(proof_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            proof = {
                "mode": "in-process-pytest",
                "status": "inconclusive",
                "invoked_interpreter": python_test.interpreter.as_posix(),
                "runtime_interpreter": None,
                "sys_prefix": None,
                "worktree": worktree.as_posix(),
                "modules": {},
                "reason": "proof-missing-or-invalid",
            }
        result["python_imports"] = proof
        if proof.get("status") != "pass":
            append_stderr(paths["stderr"], f"python-import-origin:{proof.get('status', 'invalid')}")
            if result["status"] == "pass":
                result["status"] = "fail"
    if source is not None:
        after = inspect_source(worktree)
        source["after"] = after
        result["source"] = source
        error = source_guard_error(after, expected_head)
        if error is not None:
            append_stderr(paths["stderr"], error)
            if result["status"] == "pass":
                result["status"] = "fail"
    if status == "timeout":
        result["reason"] = f"timeout after {timeout} seconds"
    elif status == "missing-command":
        first_line = paths["stderr"].read_text(encoding="utf-8").splitlines()
        result["reason"] = first_line[0] if first_line else "command exited 127"
    return result


def preserve_previous_attempt(output: Path, skip_reasons: dict[str, str]) -> None:
    """Archive existing gate evidence before rerun, refusing failed-to-skipped recovery.

    Re-executing a failed check can establish success; changing its applicability cannot. Incomplete or unreadable
    previous state requires a separate diagnosed run rather than overwriting the only retained evidence.
    """
    artifacts = ("gates.json", "gates.txt", "failed.txt", "gates.checks.jsonl", CHECKS_DIRNAME)
    if not any((output / name).exists() for name in artifacts):
        return
    prior = json.loads((output / "gates.json").read_text(encoding="utf-8"))
    checks = prior.get("checks") if isinstance(prior, dict) else None
    if not isinstance(checks, list) or len(checks) != len(GATE_IDS):
        raise ValueError("invalid-previous-gates")
    if any(not isinstance(check, dict) or not isinstance(check.get("id"), str) for check in checks):
        raise ValueError("invalid-previous-gates")
    if {check["id"] for check in checks} != set(GATE_IDS):
        raise ValueError("invalid-previous-gates")
    for check in checks:
        if check.get("status") not in ("pass", "not-applicable", "fail", "timeout", "missing-command"):
            raise ValueError("invalid-previous-gates")
        if skip_reasons[check["id"]] and check["status"] in {"fail", "timeout", "missing-command"}:
            raise ValueError(f"cannot-skip-failed-gate:{check['id']}; rerun its command or diagnose a new scoped run")

    # Copy only runner-owned receipts; preserve originals until the full archive succeeds.
    history = output / "gate-attempts"
    history.mkdir(exist_ok=True)
    index = 1
    while True:
        archive = history / f"{index:03d}"
        try:
            archive.mkdir()
            break
        except FileExistsError:
            index += 1
    for name in artifacts:
        source = output / name
        if source.is_dir():
            shutil.copytree(source, archive / name)
        elif source.is_file():
            shutil.copy2(source, archive / name)


def main() -> int:
    """Run all five gates and write their canonical aggregate artifacts."""
    arguments = parse_args()
    skip_reasons = {gate_id: getattr(arguments, f"skip_{gate_id}") for gate_id in GATE_IDS}
    if any("\n" in reason or "\r" in reason for reason in skip_reasons.values()):
        print("invalid-skip-reason:newline", file=sys.stderr)
        return 2

    output: Path = arguments.out
    commands = {gate_id: getattr(arguments, gate_id) for gate_id in GATE_IDS}
    defaults = default_commands(sys.platform)
    for gate_id in GATE_IDS:
        if not commands[gate_id]:
            commands[gate_id] = defaults[gate_id]
    if not ((arguments.worktree or Path.cwd()) / "src").is_dir() and not getattr(arguments, "types"):
        commands["types"] = "$null" if sys.platform == "win32" else ":"
        skip_reasons["types"] = skip_reasons["types"] or "no src directory or typed package target"

    try:
        preserve_previous_attempt(output, skip_reasons)
    except (OSError, ValueError) as error:
        print(f"gate-recovery-blocked:{error}", file=sys.stderr)
        return 2
    (output / CHECKS_DIRNAME).mkdir(parents=True, exist_ok=True)

    checks = [
        run_check(
            gate_id,
            commands[gate_id],
            skip_reasons[gate_id],
            arguments.timeout_seconds,
            output,
            arguments.expected_head,
            arguments.worktree,
            arguments.python_test,
        )
        for gate_id in GATE_IDS
    ]
    failed = [check["id"] for check in checks if check["status"] in {"fail", "missing-command", "timeout"}]
    status = "timeout" if any(check["status"] == "timeout" for check in checks) else "fail" if failed else "pass"
    not_applicable = [check["id"] for check in checks if check["status"] == "not-applicable"]
    (output / "gates.txt").write_text(
        "".join(f"{check['id']}:{check['status']}\n" for check in checks), encoding="utf-8"
    )
    (output / "failed.txt").write_text("".join(f"{gate_id}\n" for gate_id in failed), encoding="utf-8")
    (output / "gates.checks.jsonl").write_text(
        "".join(json.dumps(check, sort_keys=True) + "\n" for check in checks), encoding="utf-8"
    )
    payload = {
        "status": status,
        "checks_run": list(GATE_IDS),
        "checks_failed": failed,
        "failed_count": len(failed),
        "checks_not_applicable": not_applicable,
        "checks": checks,
    }
    if arguments.worktree is not None:
        payload["source"] = {"worktree": arguments.worktree.as_posix(), "expected_head": arguments.expected_head}
    result_path = output / "gates.json"
    result_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(result_path)
    return 124 if status == "timeout" else 1 if status == "fail" else 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--_python-tests-child"]:
        sys.exit(run_python_tests_child(sys.argv[2:]))
    sys.exit(main())
