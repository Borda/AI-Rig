#!/usr/bin/env python3
"""Run the canonical five Codex Rig quality gates without Bash dependencies.

## Purpose

Execute configured lint, format, type, test, and review checks while preserving per-gate evidence and timeout
classification. The gate runner turns each command into a named record that result validation can reconcile with the
workflow verdict.

## Scope

It runs local commands supplied by a workflow and writes gate records; it does not decide release readiness or hide
failed output. Each gate must have either a command or an explicit skip reason, and commands are executed independently
with a per-gate timeout.

## Usage

Run ``python run_gates.py --out <directory>`` with explicit commands or documented skip reasons for each applicable
gate. Commands can come from the gate flags or matching environment variables such as ``LINT_CMD``. Each gate applies
the value supplied through ``--timeout-seconds`` separately.

## Used by

Implement and related artifact workflows, result validation, and portable-gate acceptance tests use this runner. The
five canonical IDs are fixed as ``lint``, ``format``, ``types``, ``tests``, and ``review``, so consumers can compare
records without interpreting arbitrary gate names.

## Outputs

It writes ``gates.json``, ``gates.txt``, ``failed.txt``, ``gates.checks.jsonl``, and per-gate command/stdout/stderr
files. Records distinguish pass, fail, timeout, missing command, and ``not-applicable`` states, while captured output is
bounded to protect artifact size.

## Failure

A missing command, non-zero command result, timeout, malformed requested gate, or unwritable artifact directory is
retained as explicit gate evidence. The CLI returns ``1`` for failed gates, ``124`` for a timeout, and ``2`` for invalid
input such as a newline in a skip reason, allowing callers to classify the run without parsing prose.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


GATE_IDS = ("lint", "format", "types", "tests", "review")
DEFAULT_TIMEOUT_SECONDS = 900
CHECKS_DIRNAME = "checks"


def positive_integer(value: str) -> int:
    """Accept one strictly positive timeout value."""
    if not value.isdigit() or int(value) < 1:
        raise argparse.ArgumentTypeError(f"invalid-timeout-seconds:{value}")
    return int(value)


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
    return parser.parse_args()


def command_argv(command: str, platform: str | None = None) -> list[str]:
    """Return the native shell argv for one configured command string."""
    host = sys.platform if platform is None else platform
    if host == "win32":
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


def execute_command(command: str, timeout: int, stdout_path: Path, stderr_path: Path) -> tuple[int, float]:
    """Execute one command with bounded runtime and captured output."""
    started = time.monotonic()
    platform = sys.platform
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if platform == "win32" else 0
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
        try:
            process = subprocess.Popen(
                command_argv(command, platform),
                stdout=stdout,
                stderr=stderr,
                text=True,
                start_new_session=platform != "win32",
                creationflags=creationflags,
            )
        except FileNotFoundError:
            stderr.write(f"missing-command:{command_argv(command, platform)[0]}\n")
            return 127, time.monotonic() - started
        try:
            return process.wait(timeout=timeout), time.monotonic() - started
        except subprocess.TimeoutExpired:
            terminate_process(process, platform)
            stderr.write(f"timeout: exceeded {timeout} seconds\n")
            return 124, time.monotonic() - started


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


def run_check(gate_id: str, command: str, skip_reason: str, timeout: int, out_dir: Path) -> dict[str, Any]:
    """Run one gate or record its explicit not-applicable status."""
    paths, recorded = check_paths(gate_id, out_dir)
    if skip_reason:
        return skipped_check(gate_id, skip_reason, paths, recorded)
    if not command:
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

    paths["command"].write_text(f"{command}\n", encoding="utf-8")
    exit_code, duration = execute_command(command, timeout, paths["stdout"], paths["stderr"])
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
    if status == "timeout":
        result["reason"] = f"timeout after {timeout} seconds"
    elif status == "missing-command":
        first_line = paths["stderr"].read_text(encoding="utf-8").splitlines()
        result["reason"] = first_line[0] if first_line else "command exited 127"
    return result


def main() -> int:
    """Run all five gates and write their canonical aggregate artifacts."""
    arguments = parse_args()
    skip_reasons = {gate_id: getattr(arguments, f"skip_{gate_id}") for gate_id in GATE_IDS}
    if any("\n" in reason or "\r" in reason for reason in skip_reasons.values()):
        print("invalid-skip-reason:newline", file=sys.stderr)
        return 2

    output: Path = arguments.out
    checks_dir = output / CHECKS_DIRNAME
    checks_dir.mkdir(parents=True, exist_ok=True)
    commands = {gate_id: getattr(arguments, gate_id) for gate_id in GATE_IDS}
    defaults = default_commands(sys.platform)
    for gate_id in GATE_IDS:
        if not commands[gate_id]:
            commands[gate_id] = defaults[gate_id]
    if not Path("src").is_dir() and not getattr(arguments, "types"):
        commands["types"] = "$null" if sys.platform == "win32" else ":"
        skip_reasons["types"] = skip_reasons["types"] or "no src directory or typed package target"

    checks = [
        run_check(gate_id, commands[gate_id], skip_reasons[gate_id], arguments.timeout_seconds, output)
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
    result_path = output / "gates.json"
    result_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(result_path)
    return 124 if status == "timeout" else 1 if status == "fail" else 0


if __name__ == "__main__":
    sys.exit(main())
