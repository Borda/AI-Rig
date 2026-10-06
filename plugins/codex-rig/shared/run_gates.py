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
values, and optional ``--pytest-args-json <array>``. This opt-in mode calls pytest and inspects imported modules in
each executing process, including xdist workers, and verifies that selected test files are tracked in the worktree;
it cannot be combined with a free-form ``--tests`` command.
Use ``--project-env <absolute-venv>`` to expose tools from a separately provisioned project environment to commands
running in an isolated review worktree. It never creates an environment or installs a checker.
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
Import-bound test records contain the interpreter environment and resolved origins for imported modules and selected
tests. Clean sources require Git tracking; verified local mirrors instead bind regular-file snapshot membership and
bytes, truthfully retaining untracked added files. Local gate snapshots retain unchanged gitlink identity without
traversing or importing submodule contents. Parallel runs retain each worker's source proof and fail if a started worker
has no valid receipt.

## Failure

A missing command, non-zero command result, timeout, source mismatch or dirtiness, Git inspection failure, malformed
requested gate, or unwritable artifact directory is retained as explicit gate evidence. The CLI returns ``1`` for failed
gates, ``124`` for a timeout, and ``2`` for invalid input such as a newline in a skip reason, allowing callers to
classify the run without parsing prose.
An imported module or selected test file outside the worktree, absent from its required clean Git index or mismatched
with its admitted local snapshot fails the test gate; a module
not loaded in any executing pytest process, one without a file origin, or no selected tests leaves proof inconclusive
and fails closed. A crashed worker or missing worker receipt also fails the source proof.
"""

from __future__ import annotations

import argparse
import hashlib
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

import collect_diff


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


def _capture_local_gate_snapshot(worktree: Path) -> dict[str, Any]:
    """Freeze root runtime files and Git identities without traversing unsupported gitlinks."""

    def observe() -> dict[str, Any]:
        """Read one complete observation with gitlinks kept separate from file records."""
        inventory = collect_diff._source_inventory(worktree, ["."])
        index = collect_diff._git_output(worktree, ("ls-files", "--stage", "-z"))
        head = collect_diff._git_output(worktree, ("ls-tree", "-r", "-z", "HEAD"))
        gitlinks: dict[str, dict[str, Any]] = {}
        for row in head.split(b"\0"):
            if row:
                identity, name = row.split(b"\t", 1)
                mode, _, oid = identity.split()
                if mode == b"160000":
                    gitlinks[os.fsdecode(name)] = {"head_oid": oid.decode("ascii"), "index_entries": []}
        entries = []
        for row in index.split(b"\0"):
            if row:
                identity, name = row.split(b"\t", 1)
                mode, oid, stage = identity.split()
                path = os.fsdecode(name)
                entries.append((path, {"mode": mode.decode("ascii"), "oid": oid.decode("ascii"), "stage": int(stage)}))
                if mode == b"160000":
                    gitlinks.setdefault(path, {"head_oid": None, "index_entries": []})
        for path, entry in entries:
            if path in gitlinks:
                gitlinks[path]["index_entries"].append(entry)
        for path, identity in gitlinks.items():
            if identity["index_entries"] != [{"mode": "160000", "oid": identity["head_oid"], "stage": 0}]:
                raise RuntimeError(f"local-gate-gitlink-identity-changed:{path}")
        status = collect_diff._git_output(
            worktree, ("status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=none")
        )
        if any(os.fsdecode(row[3:]) in gitlinks for row in status.split(b"\0") if row):
            raise RuntimeError("local-gate-gitlink-dirty")
        paths = [
            path
            for path in collect_diff._inventory_paths(inventory)
            if not any(path == link or path.startswith(link + "/") for link in gitlinks)
        ]
        return {
            "repository": worktree.as_posix(),
            "revision": collect_diff._git_output(worktree, ("rev-parse", "--verify", "HEAD")).strip().decode("ascii"),
            "index_sha256": hashlib.sha256(index).hexdigest(),
            "inventory_sha256": hashlib.sha256(inventory).hexdigest(),
            "status_sha256": hashlib.sha256(status).hexdigest(),
            "files": [collect_diff._source_record(worktree, path) for path in paths],
            "gitlinks": [{"path": path, **identity} for path, identity in sorted(gitlinks.items())],
        }

    before = observe()
    if before != observe():
        raise RuntimeError("local-gate-source-changed-during-capture")
    return before


def _local_snapshot_bytes(snapshot: dict[str, Any]) -> bytes:
    """Encode the runtime snapshot for identical producer and consumer digests."""
    return (json.dumps(snapshot, indent=2, sort_keys=True) + "\n").encode("utf-8")


def validate_local_gate_source(out: Path, worktree: Path) -> dict[str, Any]:
    """Bind one existing local mirror receipt to fresh runtime bytes and contained artifacts."""
    out = out.resolve()
    if out.is_relative_to(worktree) or worktree.is_relative_to(out / "checks"):
        raise ValueError("local-gate-artifacts-overlap-source")
    for name in ("local-source/review-worktree.json", "local-source/source-snapshot.json", "checks"):
        path = out / name
        if path.is_symlink() or not path.resolve().is_relative_to(out):
            raise ValueError("local-gate-artifact-escape")
    if any((out / name).exists() for name in ("pr-routing.json", "local-checkout.json", "pr/local-checkout.json")):
        raise ValueError("local-gate-source-receipt-ambiguous")
    receipt = json.loads((out / "local-source/review-worktree.json").read_bytes())
    if receipt["review_worktree"] != worktree.as_posix():
        raise ValueError("local-gate-worktree-mismatch")
    collect_diff.verify_review_worktree(out / "local-source")
    return _capture_local_gate_snapshot(worktree)


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
        help="Absolute checkout root; requires --expected-head or the existing output local-source mirror receipt.",
    )
    parser.add_argument(
        "--project-env", type=Path, help="Absolute existing project virtual environment for gate tools."
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
        arguments.worktree = arguments.worktree.resolve()
        if arguments.expected_head is None:
            if not (arguments.out / "local-source/review-worktree.json").is_file():
                parser.error("invalid-worktree: --expected-head is required")
            try:
                validate_local_gate_source(arguments.out, arguments.worktree)
            except (OSError, ValueError, RuntimeError, KeyError) as error:
                parser.error(f"invalid-worktree: --expected-head or valid local-source receipt required: {error}")
    if arguments.project_env is not None:
        if arguments.worktree is None:
            parser.error("invalid-project-env: --worktree is required")
        scripts = arguments.project_env / ("Scripts" if sys.platform == "win32" else "bin")
        python = scripts / ("python.exe" if sys.platform == "win32" else "python")
        if (
            not arguments.project_env.is_absolute()
            or not (arguments.project_env / "pyvenv.cfg").is_file()
            or not scripts.is_dir()
            or not python.is_file()
        ):
            parser.error("invalid-project-env: expected an absolute existing virtual environment")
        arguments.project_env = arguments.project_env.resolve()
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
    command: str | list[str],
    timeout: int,
    stdout_path: Path,
    stderr_path: Path,
    worktree: Path | None = None,
    environment: dict[str, str] | None = None,
) -> tuple[int, float]:
    """Execute one command with bounded runtime and captured output."""
    started = time.monotonic()
    platform = sys.platform
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if platform == "win32" else 0
    argv = command_argv(command, platform) if isinstance(command, str) else command
    if environment is not None and platform != "win32" and isinstance(command, str):
        # A login shell may replace PATH and hide the explicitly selected project environment.
        argv = ["bash", "-c", command]
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
        try:
            process = subprocess.Popen(
                argv,
                stdout=stdout,
                stderr=stderr,
                text=True,
                cwd=worktree,
                env=environment,
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


def _local_origin_membership(origin: Path, worktree: Path, snapshot: dict[str, Any]) -> dict[str, Any]:
    """Accept an actual regular-file origin only when its admitted snapshot bytes still match."""
    relative = origin.relative_to(worktree).as_posix()
    record = next((item for item in snapshot["files"] if item["path"] == relative), None)
    if (
        record is None
        or record["kind"] != "file"
        or not origin.is_file()
        or hashlib.sha256(origin.read_bytes()).hexdigest() != record["sha256"]
    ):
        return {"status": "fail", "reason": "origin-not-current-snapshot-member"}
    return {
        "status": "pass",
        "reason": None,
        "snapshot_member": {key: record[key] for key in ("path", "kind", "sha256")},
    }


def inspect_imported_module(name: str, worktree: Path, local_snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    """Check an actual imported origin against clean tracking or admitted local snapshot bytes."""
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
        **(_local_origin_membership(origin, worktree, local_snapshot) if local_snapshot is not None else {}),
    }


def inspect_collected_test(path: Path, worktree: Path, local_snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    """Verify an actual collected test against clean tracking or admitted local snapshot bytes."""
    origin = path.resolve()
    try:
        relative = origin.relative_to(worktree).as_posix()
    except ValueError:
        return {"origin": origin.as_posix(), "tracked": False, "status": "fail", "reason": "test-outside-worktree"}
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
        "reason": None if tracked else "test-not-tracked",
        **(_local_origin_membership(origin, worktree, local_snapshot) if local_snapshot is not None else {}),
    }


def _aggregate_worker_proofs(
    workers: dict[str, dict[str, Any]], expected: set[str], modules: list[str]
) -> dict[str, Any]:
    """Combine observed worker origins without hiding failures or missing receipts."""
    errors = [f"worker-proof-missing:{worker}" for worker in sorted(expected - workers.keys())]
    if not expected:
        errors.append("worker-proof-no-nodes")
    origins: dict[str, dict[str, Any]] = {}
    tests: dict[str, dict[str, Any]] = {}
    for name in modules:
        observed = [worker["modules"][name] for worker in workers.values()]
        loaded = [entry for entry in observed if entry["reason"] != "module-not-imported"]
        failing = [entry for entry in loaded if entry["status"] == "fail"]
        paths = {entry["origin"] for entry in loaded if entry["status"] == "pass"}
        if failing:
            origins[name] = failing[0]
        elif len(paths) > 1:
            origins[name] = {
                "origin": None,
                "tracked": False,
                "status": "fail",
                "reason": "conflicting-worker-origins",
            }
        elif loaded:
            origins[name] = loaded[0]
        else:
            origins[name] = {
                "origin": None,
                "tracked": False,
                "status": "inconclusive",
                "reason": "module-not-imported",
            }
    for worker in workers.values():
        for path, record in worker["tests"].items():
            if path not in tests or record["status"] == "fail":
                tests[path] = record
    statuses = {entry["status"] for entry in (*origins.values(), *tests.values())}
    if errors or "fail" in statuses:
        status = "fail"
    elif not tests or "inconclusive" in statuses:
        status = "inconclusive"
    else:
        status = "pass"
    return {"status": status, "modules": origins, "tests": tests, "workers": workers, "errors": errors}


def pytest_configure(config: Any) -> None:
    """Attach source proof to pytest controllers and workers in opt-in gate mode."""
    raw = os.environ.get("CODEX_RIG_PYTEST_SOURCE_PROOF")
    if raw is None:
        return
    # Pytest remains optional for all other gate modes and normal module imports.
    import pytest

    task = json.loads(raw)
    worktree = Path(task["worktree"])
    modules = task["modules"]
    local_source = task.get("local_source")
    local_snapshot = None
    if local_source is not None:
        content = Path(local_source["snapshot_path"]).read_bytes()
        if hashlib.sha256(content).hexdigest() != local_source["snapshot_sha256"]:
            raise ValueError("local-python-snapshot-changed")
        local_snapshot = json.loads(content)
        if local_snapshot["repository"] != worktree.as_posix():
            raise ValueError("local-python-snapshot-worktree-mismatch")

    class SourceProof:
        """Retain each pytest process's selected tests and actual imported module origins."""

        def __init__(self) -> None:
            """Start source collection with no assumed workers or test selections."""
            self.paths: set[Path] = set()
            self.expected: set[str] = set()
            self.workers: dict[str, dict[str, Any]] = {}
            self.errors: list[str] = []

        def pytest_collection_finish(self, session: Any) -> None:
            """Record test source paths after pytest applies its selection options."""
            self.paths = {Path(item.path) for item in session.items}

        @pytest.hookimpl(optionalhook=True)
        def pytest_configure_node(self, node: Any) -> None:
            """Require proof from every xdist worker the controller starts."""
            self.expected.add(node.workerinput["workerid"])

        @pytest.hookimpl(optionalhook=True)
        def pytest_testnodedown(self, node: Any, error: object) -> None:
            """Retain each worker receipt and classify crashes as proof failures."""
            worker = node.workerinput["workerid"]
            output = getattr(node, "workeroutput", None)
            proof = output.get("codex_rig_source_proof") if isinstance(output, dict) else None
            if error is not None:
                self.errors.append(f"worker-failed:{worker}:{error}")
            if not isinstance(proof, dict):
                self.errors.append(f"worker-proof-missing:{worker}")
                return
            if (
                proof.get("worktree") != worktree.as_posix()
                or not isinstance(proof.get("modules"), dict)
                or set(proof["modules"]) != set(modules)
                or any(
                    not isinstance(entry, dict) or entry.get("status") not in {"pass", "fail", "inconclusive"}
                    for entry in proof["modules"].values()
                )
                or not isinstance(proof.get("tests"), dict)
                or not proof["tests"]
                or any(
                    not isinstance(entry, dict) or entry.get("status") not in {"pass", "fail", "inconclusive"}
                    for entry in proof["tests"].values()
                )
            ):
                self.errors.append(f"worker-proof-invalid:{worker}")
                return
            if local_source is not None and proof.get("local_source") != local_source:
                self.errors.append(f"worker-local-snapshot-mismatch:{worker}")
                return
            self.workers[worker] = proof

        def pytest_sessionfinish(self, session: Any, exitstatus: int) -> None:
            """Publish serial or aggregated worker proof before pytest returns."""
            origins = {name: inspect_imported_module(name, worktree, local_snapshot) for name in modules}
            tests = {
                path.resolve().as_posix(): inspect_collected_test(path, worktree, local_snapshot) for path in self.paths
            }
            statuses = {entry["status"] for entry in (*origins.values(), *tests.values())}
            status = (
                "fail" if "fail" in statuses else "inconclusive" if not tests or "inconclusive" in statuses else "pass"
            )
            process_proof = {
                "status": status,
                "runtime_interpreter": Path(sys.executable).absolute().as_posix(),
                "sys_prefix": Path(sys.prefix).absolute().as_posix(),
                "worktree": worktree.as_posix(),
                "modules": origins,
                "tests": tests,
                **({"local_source": local_source} if local_source is not None else {}),
            }
            if hasattr(config, "workeroutput"):
                config.workeroutput["codex_rig_source_proof"] = process_proof
                return
            if self.expected:
                combined = _aggregate_worker_proofs(self.workers, self.expected, modules)
                combined["errors"].extend(self.errors)
                if self.errors:
                    combined["status"] = "fail"
            else:
                combined = {**process_proof, "workers": {}, "errors": self.errors}
            proof = {
                **combined,
                "mode": "in-process-pytest",
                "invoked_interpreter": task["invoked_interpreter"],
                "runtime_interpreter": process_proof["runtime_interpreter"],
                "sys_prefix": process_proof["sys_prefix"],
                "worktree": worktree.as_posix(),
                **({"local_source": local_source} if local_source is not None else {}),
            }
            Path(task["proof_path"]).write_text(json.dumps(proof, indent=2) + "\n", encoding="utf-8", newline="\n")

    config.pluginmanager.register(SourceProof(), "codex-rig-source-proof")


def run_python_tests_child(arguments: list[str]) -> int:
    """Run pytest with controller and worker source proof in the selected interpreter."""
    if len(arguments) not in {5, 6}:
        print("invalid-python-test-child-arguments", file=sys.stderr)
        return 2
    worktree = Path(arguments[0]).resolve()
    proof_path = Path(arguments[1])
    modules = json.loads(arguments[2])
    pytest_arguments = json.loads(arguments[3])
    invoked_interpreter = arguments[4]
    if len(arguments) == 6:
        sys.dont_write_bytecode = True
    # Pytest is optional for all other gate modes and belongs to the selected test environment.
    import pytest

    task = {
        "worktree": worktree.as_posix(),
        "proof_path": proof_path.as_posix(),
        "modules": modules,
        "invoked_interpreter": invoked_interpreter,
    }
    if len(arguments) == 6:
        task["local_source"] = json.loads(arguments[5])
    prior_task = os.environ.get("CODEX_RIG_PYTEST_SOURCE_PROOF")
    os.environ["CODEX_RIG_PYTEST_SOURCE_PROOF"] = json.dumps(task)
    try:
        test_exit_code = int(pytest.main(["-p", "run_gates", *pytest_arguments]))
    finally:
        if prior_task is None:
            os.environ.pop("CODEX_RIG_PYTEST_SOURCE_PROOF", None)
        else:
            os.environ["CODEX_RIG_PYTEST_SOURCE_PROOF"] = prior_task
    if not proof_path.is_file():
        print("source-bound-pytest-proof-missing", file=sys.stderr)
        return test_exit_code or 1
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    return test_exit_code if test_exit_code else 0 if proof["status"] == "pass" else 1


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
    environment: dict[str, str] | None = None,
    local_source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one gate and bind local execution artifacts to the observed source and child proof."""
    paths, recorded = check_paths(gate_id, out_dir)
    if local_source is not None:
        for path in paths.values():
            if path.is_symlink() or not path.resolve().is_relative_to(out_dir.resolve()):
                raise ValueError("local-gate-artifact-escape")
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
        if local_source is not None:
            python_test_argv.append(
                json.dumps(
                    {
                        "snapshot_path": (out_dir / local_source["snapshot_path"]).resolve().as_posix(),
                        "snapshot_sha256": local_source["snapshot_sha256"],
                    }
                )
            )
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

    if local_source is not None:
        try:
            current = validate_local_gate_source(out_dir, worktree)
            if (
                hashlib.sha256((out_dir / local_source["receipt_path"]).read_bytes()).hexdigest()
                != local_source["receipt_sha256"]
            ):
                raise ValueError("local-gate-receipt-changed")
            digest = hashlib.sha256(_local_snapshot_bytes(current)).hexdigest()
            if digest != local_source["snapshot_sha256"]:
                raise ValueError("local-gate-source-changed")
            for path in paths.values():
                if path.is_symlink() or not path.resolve().is_relative_to(out_dir.resolve()):
                    raise ValueError("local-gate-artifact-escape")
            source = {**local_source, "before": {"snapshot_sha256": digest}, "after": None}
        except (OSError, ValueError, RuntimeError, KeyError) as error:
            append_stderr(paths["stderr"], str(error))
            paths["stdout"].write_text("", encoding="utf-8")
            return {
                "id": gate_id,
                "status": "fail",
                "exit_code": 1,
                "duration_seconds": 0.0,
                "command_path": recorded["command"],
                "stdout": recorded["stdout"],
                "stderr": recorded["stderr"],
            }
    exit_code, duration = execute_command(
        python_test_argv or command, timeout, paths["stdout"], paths["stderr"], worktree, environment
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
    proof_bytes: bytes | None = None
    if python_test_argv is not None:
        try:
            if local_source is not None and (
                proof_path.is_symlink()
                or not proof_path.resolve().is_relative_to(out_dir.resolve())
                or not proof_path.is_file()
            ):
                raise ValueError("local-gate-proof-artifact-invalid")
            if local_source is None:
                proof = json.loads(proof_path.read_text(encoding="utf-8"))
            else:
                proof_bytes = proof_path.read_bytes()
                proof = json.loads(proof_bytes.decode("utf-8"))
                if not isinstance(proof, dict):
                    raise ValueError("python-import-proof-not-object")
        except (OSError, ValueError, UnicodeError) as error:
            if local_source is None and not isinstance(error, (OSError, json.JSONDecodeError)):
                raise
            proof_bytes = None
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
    if local_source is not None:
        try:
            after = validate_local_gate_source(out_dir, worktree)
            if (
                hashlib.sha256((out_dir / local_source["receipt_path"]).read_bytes()).hexdigest()
                != local_source["receipt_sha256"]
            ):
                raise ValueError("local-gate-receipt-changed")
            digest = hashlib.sha256(_local_snapshot_bytes(after)).hexdigest()
            source["after"] = {"snapshot_sha256": digest}
            if digest != local_source["snapshot_sha256"]:
                raise ValueError("local-gate-source-changed")
        except (OSError, ValueError, RuntimeError, KeyError) as error:
            append_stderr(paths["stderr"], str(error))
            result["status"] = "fail"
        if python_test_argv is not None and proof_bytes is not None:
            try:
                if (
                    proof_path.is_symlink()
                    or not proof_path.resolve().is_relative_to(out_dir.resolve())
                    or not proof_path.is_file()
                    or proof_path.read_bytes() != proof_bytes
                ):
                    raise ValueError("local-gate-proof-artifact-changed")
            except (OSError, ValueError) as error:
                append_stderr(paths["stderr"], str(error))
                result["status"] = "fail"
        source["artifacts"] = {
            recorded[key]: hashlib.sha256(path.read_bytes()).hexdigest() for key, path in paths.items()
        }
        if python_test_argv is not None and proof_bytes is not None:
            source["artifacts"]["checks/tests.python-imports.json"] = hashlib.sha256(proof_bytes).hexdigest()
        result["source"] = source
    elif source is not None:
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
    environment = None
    if arguments.project_env is not None:
        scripts = arguments.project_env / ("Scripts" if sys.platform == "win32" else "bin")
        environment = os.environ.copy()
        environment["PATH"] = os.pathsep.join((str(scripts), environment.get("PATH", "")))
        environment["VIRTUAL_ENV"] = str(arguments.project_env)

    try:
        preserve_previous_attempt(output, skip_reasons)
    except (OSError, ValueError) as error:
        print(f"gate-recovery-blocked:{error}", file=sys.stderr)
        return 2
    (output / CHECKS_DIRNAME).mkdir(parents=True, exist_ok=True)

    local_source = None
    if arguments.worktree is not None and arguments.expected_head is None:
        snapshot = validate_local_gate_source(output, arguments.worktree)
        content = _local_snapshot_bytes(snapshot)
        snapshot_path = output / CHECKS_DIRNAME / "local-source-snapshot.json"
        if snapshot_path.is_symlink() or not snapshot_path.resolve().is_relative_to(output.resolve()):
            raise ValueError("local-gate-artifact-escape")
        snapshot_path.write_bytes(content)
        receipt_path = output / "local-source/review-worktree.json"
        local_source = {
            "mode": "local-review-mirror",
            "worktree": arguments.worktree.as_posix(),
            "receipt_path": "local-source/review-worktree.json",
            "receipt_sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
            "snapshot_path": "checks/local-source-snapshot.json",
            "snapshot_sha256": hashlib.sha256(content).hexdigest(),
        }
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
            environment,
            local_source,
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
        payload["source"] = local_source or {
            "worktree": arguments.worktree.as_posix(),
            "expected_head": arguments.expected_head,
        }
        if arguments.project_env is not None:
            payload["source"]["project_env"] = arguments.project_env.as_posix()
    result_path = output / "gates.json"
    result_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(result_path)
    return 124 if status == "timeout" else 1 if status == "fail" else 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--_python-tests-child"]:
        sys.exit(run_python_tests_child(sys.argv[2:]))
    sys.exit(main())
