#!/usr/bin/env python3
"""Run the canonical five Codex Rig quality gates without Bash dependencies.

## Purpose

Execute configured lint, format, type, test, and review checks while preserving per-gate evidence and timeout
classification. An optional expected Git head binds each executable check to a clean source observation before and after
its command. An optional absolute worktree path binds those observations and gate commands to one checkout. The gate
runner turns each command into a named record that result validation can reconcile with the workflow verdict.

## Scope

Each gate has a command or an explicit skip reason and runs independently with a per-gate timeout.
One writer captures and verifies a private candidate while the previous current attempt remains readable. It publishes
the complete previous attempt under ``gate-attempts/<NNN>``, installs the current evidence under a publication guard,
and exposes the new ``gates.json`` last. A retry can finish an interrupted installation from its retained candidate.
An earlier failed applicable check must run again. Unauthenticated or displaced evidence blocks replacement.

## Usage

Run ``python run_gates.py --out <directory>`` with explicit commands or documented skip reasons for each applicable
gate. Add ``--expected-head <full-lowercase-sha>`` to require a matching clean Git checkout around executable checks.
Add ``--worktree <absolute-path>`` with the expected head to select the exact checkout used for source inspection and
commands, while retaining artifacts under ``--out``.
For Python project tests, add ``--pytest-python <absolute-executable>``, one or more ``--pytest-import <module>``
values, and optional ``--pytest-args-json <array>``. This opt-in mode calls pytest and inspects imported modules in
each executing process, including xdist workers, and verifies that selected test files are tracked in the worktree;
it cannot be combined with a free-form ``--tests`` command. Use ``--single-process-pytest`` only when that exact
selection and its project configuration, plugins, fixtures and tests support execution without subprocesses or network.
On supported macOS linked worktrees the runner confines eligible native Ruff checks and declared pytest. Each executed
gate records its actual route; a standard route names why confinement did not apply. Declining confinement changes only
the launcher, never the caller's contributor-execution decision. A failed sandboxed command is never repeated on the
standard path.
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
files. Clean import-bound pytest uses a fresh private directory under ``checks/`` for each attempt. The parent publishes
its validated import receipt. Other execution modes keep their pytest cache and coverage data under ``checks/``.
Worktree-bound runs record the selected checkout at the top level and in each executable source receipt. Guarded
executable records include their expected head plus before/after source receipts. Records distinguish
pass, fail, timeout, missing command, and ``not-applicable`` states, while captured output is bounded to protect
artifact size.
Import-bound test records contain the interpreter environment and resolved origins for imported modules and selected
tests. For clean sources, the parent passes checkout- and head-bound Git index membership to pytest and checks it
again after execution. Verified local mirrors bind regular-file snapshot membership and bytes, including added files.
Local gate snapshots retain unchanged gitlink identity without traversing or importing submodule contents.
Parallel runs retain each worker's source proof and fail if a started worker has no valid receipt.

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
import configparser
import hashlib
import importlib
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, NamedTuple

# A source file cannot contain a bytecode cache directory.
sys.pycache_prefix = str(Path(__file__).resolve())

import collect_diff  # noqa: E402
import worktree_sandbox  # noqa: E402

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 can use the optional backport from the project environment.
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        tomllib = None
from gate_test_approval import prepare_approval, retain_approval  # noqa: E402
from run_targeted_pytest import protected_pytest_environment  # noqa: E402

#: Verification gate identifiers, in the order the gates run and are reported.
GATE_IDS = ("lint", "format", "types", "tests", "review")
#: Per-gate timeout, in seconds, used when `GATE_TIMEOUT_SECONDS` is not set.
DEFAULT_TIMEOUT_SECONDS = 7200
#: Name of the output subdirectory that holds per-check proof files.
CHECKS_DIRNAME = "checks"
#: Timeout, in seconds, for the quick git and source-inspection commands run alongside the gates.
SOURCE_INSPECTION_TIMEOUT_SECONDS = 30
#: Disable repository-configured executables during parent source observations.
SOURCE_GIT = ("git", "--no-pager", "-c", "core.fsmonitor=false", "-c", f"core.hooksPath={os.devnull}")
#: Pattern for a dotted Python module name accepted as a pytest import-origin argument.
PYTHON_MODULE_PATTERN = re.compile(r"[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)*")
GIT_OBJECT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


class PythonTestSpec(NamedTuple):
    """Describe one in-process pytest invocation with import origins to verify."""

    interpreter: Path
    modules: tuple[str, ...]
    arguments: tuple[str, ...]
    single_process: bool = False


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
    if GIT_OBJECT_ID.fullmatch(value) is None:
        raise argparse.ArgumentTypeError(f"invalid-expected-head:{value}")
    return value


def skip_reason(value: str) -> str:
    """Admit one explicit, nonblank, single-line reason before any gate output changes."""
    if "\n" in value or "\r" in value:
        raise argparse.ArgumentTypeError("invalid-skip-reason:newline")
    if not value.strip():
        raise argparse.ArgumentTypeError("invalid-skip-reason:blank")
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
    parser.add_argument("--test-skip-approval", type=Path, help="Retained user decision authorizing --skip-tests.")
    for gate_id in GATE_IDS:
        parser.add_argument(
            f"--{gate_id}",
            default=os.environ.get(f"{gate_id.upper()}_CMD", ""),
            help=f"{gate_id.capitalize()} command.",
        )
        parser.add_argument(
            f"--skip-{gate_id}",
            type=skip_reason,
            help=f"Explicit reason the {gate_id} gate is not applicable.",
        )
    parser.add_argument(
        "--timeout-seconds",
        default=os.environ.get("GATE_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS)),
        type=positive_integer,
        help="Per-gate timeout; defaults to 7200 or GATE_TIMEOUT_SECONDS.",
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
    parser.add_argument(
        "--single-process-pytest",
        action="store_true",
        help="Declare that this exact import-bound selection, config, plugins and tests support confinement without "
        "child processes or network. This permits automatic use of a supported profile and does not require isolation.",
    )
    arguments = parser.parse_args()
    arguments.local_snapshot = None
    if arguments.single_process_pytest and (not arguments.pytest_python or not arguments.expected_head):
        parser.error("--single-process-pytest requires clean import-bound pytest with --expected-head")
    if arguments.worktree is not None:
        if not arguments.worktree.is_absolute() or not arguments.worktree.is_dir():
            parser.error("invalid-worktree: expected an existing absolute directory")
        arguments.worktree = arguments.worktree.resolve()
        if arguments.expected_head is None:
            if not (arguments.out / "local-source/review-worktree.json").is_file():
                parser.error("invalid-worktree: --expected-head is required")
            try:
                arguments.local_snapshot = validate_local_gate_source(arguments.out, arguments.worktree)
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
            arguments.pytest_python.absolute(),
            tuple(dict.fromkeys(arguments.pytest_import)),
            tuple(pytest_arguments),
            arguments.single_process_pytest,
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
    return ["bash", "-c", command]


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
            "review": "",
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
        "review": "",
    }


def terminate_process(process: subprocess.Popen[str], platform: str) -> None:
    """Terminate a timed-out command and its descendants on the current platform."""
    if platform == "win32":
        try:
            completed = subprocess.run(  # noqa: S603 - argv list, no shell
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],  # noqa: S607 - native system tool
                capture_output=True,
                timeout=2,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(
                "windows-process-tree-cleanup-unproven: "
                f"returncode=unavailable timeout={error.timeout} "
                f"stdout={(error.stdout or b'')[:256]!r} stderr={(error.stderr or b'')[:256]!r}"
            ) from error
        if completed.returncode != 0:
            raise RuntimeError(
                "windows-process-tree-cleanup-unproven: "
                f"returncode={completed.returncode} "
                f"stdout={(completed.stdout or b'')[:256]!r} stderr={(completed.stderr or b'')[:256]!r}"
            )
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        return

    # Keep the leader unreaped while signaling its group, preventing its PID from being reused.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait()
        return
    time.sleep(0.2)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        # Some hosts reject signals to a group containing only an exited leader.
        # Reap it, then accept the denial only if the group no longer exists.
        process.wait(timeout=2)
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return
        raise RuntimeError("process-group-cleanup-unproven") from None
    process.wait()


def gate_environment(environment: dict[str, str] | None = None) -> dict[str, str]:
    """Retain runtime and tool configuration without passing parent credentials to gate code."""
    allowed = {
        "PATH",
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "SYSTEMROOT",
        "COMSPEC",
        "PATHEXT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "TERM",
        "VIRTUAL_ENV",
        "UV_PROJECT_ENVIRONMENT",
        "UV_CACHE_DIR",
        "XDG_CACHE_HOME",
        "PYTHONPATH",
        "PYTHONDONTWRITEBYTECODE",
        "PYTEST_ADDOPTS",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "ALL_PROXY",
        "CI",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "USER",
        "LOGNAME",
        "USERNAME",
        "LD_LIBRARY_PATH",
        "PYTHONUTF8",
        "PYTHONIOENCODING",
        "WINDIR",
        "PROGRAMFILES",
        "PROGRAMDATA",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
    }
    source = os.environ if environment is None else environment
    return {
        key: value
        for key, value in source.items()
        if (key.upper() in allowed or key.upper().startswith(("UV_", "PIP_")))
        and not key.upper().endswith(("_PASSWORD", "_TOKEN", "_KEY", "_SECRET"))
        and not re.search(r"(?:[A-Za-z][A-Za-z0-9+.-]*:)?//[^/\s]*@", value)
    }


def _child_environment(
    checks: Path,
    worktree: Path | None,
    environment: dict[str, str] | None,
    run_directory: Path | None,
    child_scratch: Path | None,
) -> dict[str, str]:
    """Bind tool output and the pytest provenance plugin to the selected gate's artifact paths."""
    # Free-form gates can launch pytest through project scripts, including xdist workers.
    checks = child_scratch or checks.resolve()
    child_environment = protected_pytest_environment(
        gate_environment(environment), checks, (child_scratch or run_directory or checks).resolve()
    )
    if child_scratch is not None:
        child_environment.update(TMPDIR=str(checks), TMP=str(checks), TEMP=str(checks))
    if worktree is not None:
        child_environment.update(
            MYPY_CACHE_DIR=str(checks / "mypy-cache"),
            RUFF_CACHE_DIR=str(checks / "ruff-cache"),
        )
    return child_environment


class _GateLaunchFailure(NamedTuple):
    """Preserve the two-value command result while distinguishing an OS refusal from an actual child exit."""

    exit_code: int
    duration: float


def execute_command(
    command: str | list[str],
    timeout: int,
    stdout_path: Path,
    stderr_path: Path,
    worktree: Path | None = None,
    environment: dict[str, str] | None = None,
    run_directory: Path | None = None,
    child_scratch: Path | None = None,
    *,
    prepared_environment: dict[str, str] | None = None,
) -> tuple[int, float]:
    """Execute one command with a shared deadline for exit and bounded stream capture."""
    started = time.monotonic()
    platform = sys.platform
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if platform == "win32" else 0
    argv = command_argv(command, platform) if isinstance(command, str) else command
    child_environment = (
        prepared_environment
        if prepared_environment is not None
        else _child_environment(
            run_directory / CHECKS_DIRNAME if run_directory is not None else stdout_path.parent,
            worktree,
            environment,
            run_directory,
            child_scratch,
        )
    )
    try:
        process = subprocess.Popen(  # noqa: S603 - argv list, no shell
            argv,
            stdin=subprocess.DEVNULL if prepared_environment is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=worktree,
            env=child_environment,
            start_new_session=platform != "win32",
            creationflags=creationflags,
        )
    except OSError as error:
        if prepared_environment is not None:
            code, diagnostic = 1, f"worktree-profile-launch-failed:{error}"
        elif isinstance(error, FileNotFoundError):
            code, diagnostic = 127, f"missing-command:{argv[0]}"
        else:
            code, diagnostic = 126, f"standard-launch-failed:{argv[0]}:{error}"
        stdout_path.write_bytes(b"")
        stderr_path.write_text(f"{diagnostic}\n", encoding="utf-8")
        return _GateLaunchFailure(code, time.monotonic() - started)

    errors: list[str] = []

    def capture(stream: Any, path: Path) -> None:
        """Retain raw output up to 1 MiB, then keep the opening and final 512 KiB around an omission count."""
        limit = 1024 * 1024
        half = limit // 2
        total = 0
        tail = bytearray()
        try:
            with path.open("wb") as output, stream:
                while chunk := stream.read1(65536):
                    if total < limit:
                        output.write(chunk[: limit - total])
                    total += len(chunk)
                    if total > half:
                        tail.extend(chunk)
                        del tail[:-half]
                if total > limit:
                    # Preserve live prefix output until EOF; the final diagnostic often arrives in the last chunk.
                    output.seek(half)
                    output.write(f"\n[output truncated, omitted bytes: {total - limit}]\n".encode("ascii"))
                    output.write(tail)
        except OSError as error:
            errors.append(f"capture-failed:{path.name}:{error}")

    threads = [
        threading.Thread(target=capture, args=(stream, path), daemon=True)
        for stream, path in ((process.stdout, stdout_path), (process.stderr, stderr_path))
    ]
    for thread in threads:
        thread.start()
    deadline = started + timeout
    try:
        # Descendants may inherit the pipes after the leader exits. Keep its PID unreaped
        # until capture finishes so timeout cleanup can still identify the original group.
        for thread in threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in threads):
            raise subprocess.TimeoutExpired(argv, timeout)
        exit_code = process.wait(timeout=max(0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        terminate_process(process, platform)
        exit_code = 124
    for thread in threads:
        thread.join(timeout=2)
    if any(thread.is_alive() for thread in threads):
        errors.append("capture-incomplete:stream remained open after command exit")
    if exit_code == 124:
        append_stderr(stderr_path, f"timeout: exceeded {timeout} seconds")
    for error in errors:
        append_stderr(stderr_path, error)
    if errors and exit_code == 0:
        exit_code = 1
    return exit_code, time.monotonic() - started


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


def _tracked_source_paths(
    worktree: Path, relative: str | None = None, *, commit: str | None = None
) -> set[bytes] | None:
    """Read raw tracked paths from the live index or a locally available commit tree."""
    arguments = ["git", "ls-files", "-z", "--"]
    environment = None
    if commit is not None:
        # Staged additions or removals cannot describe the requested clean commit. Walk its real object tree
        # without replacement refs or lazy retrieval of missing promisor objects, including on older Git.
        arguments = [
            *SOURCE_GIT,
            "--no-replace-objects",
            "ls-tree",
            "--full-tree",
            "-r",
            "-z",
            "--name-only",
            f"{commit}^{{commit}}",
            "--",
        ]
        environment = {**os.environ, "GIT_NO_LAZY_FETCH": "1", "GIT_ALLOW_PROTOCOL": ""}
    if relative is not None:
        # Standalone validators inspect one origin; avoid expanding every call into a whole-index read.
        arguments.append(f":(literal){relative}")
    try:
        completed = subprocess.run(  # noqa: S603 - fixed Git argv with a literal pathspec after --
            arguments,
            cwd=worktree,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=SOURCE_INSPECTION_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return set(completed.stdout.split(b"\0")) - {b""}


# Source proof includes a row per test and per worker; keep oversized submissions out of the trusted parent.
MAX_TEST_PROOF_BYTES = 64 * 1024 * 1024


def _read_gate_file(path: Path, label: str, limit: int | None = None) -> bytes:
    """Read an ordinary single-link file, rejecting redirects, replacements and over-budget child submissions."""
    observed = path.lstat()
    if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1 or path.parent.is_symlink():
        raise ValueError(f"{label}-invalid-file")
    if limit is not None and observed.st_size > limit:
        raise ValueError(f"{label}-oversized")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        if not os.path.samestat(observed, os.fstat(stream.fileno())):
            raise ValueError(f"{label}-file-changed")
        content = stream.read() if limit is None else stream.read(limit + 1)
    if limit is not None and len(content) > limit:
        raise ValueError(f"{label}-oversized")
    return content


def _read_tracked_inventory(path: Path, digest: str | None = None) -> bytes:
    """Read a retained Git index and verify its recorded content digest when supplied."""
    content = _read_gate_file(path, "tracked-source-inventory")
    if digest is not None and hashlib.sha256(content).hexdigest() != digest:
        raise ValueError("tracked-source-inventory-changed")
    return content


def _tracked_inventory(worktree: Path, head: str, paths: set[bytes]) -> tuple[bytes, str]:
    """Encode the exact index names and source coordinates shared by preflight and the structured test."""
    content = b"\0".join((os.fsencode(worktree.as_posix()), head.encode("ascii"), *sorted(paths)))
    return content, hashlib.sha256(content).hexdigest()


def _publish_gate_proof(path: Path, content: bytes, checks_identity: os.stat_result) -> None:
    """Create the canonical proof exclusively inside the same checks directory admitted before execution."""
    if not stat.S_ISDIR(checks_identity.st_mode) or not os.path.samestat(path.parent.lstat(), checks_identity):
        raise ValueError("source-gate-checks-directory-changed")
    if os.open in os.supports_dir_fd:
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if not os.path.samestat(os.fstat(directory), checks_identity):
                raise ValueError("source-gate-checks-directory-changed")
            with os.fdopen(
                os.open(path.name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=directory), "wb"
            ) as out:
                out.write(content)
        finally:
            os.close(directory)
    else:
        with path.open("xb") as out:
            out.write(content)


def inspect_imported_module(
    name: str,
    worktree: Path,
    local_snapshot: dict[str, Any] | None = None,
    *,
    tracked_paths: set[bytes] | None = None,
) -> dict[str, Any]:
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
    if tracked_paths is None:
        tracked_paths = _tracked_source_paths(worktree, relative) or set()
    tracked = os.fsencode(relative) in tracked_paths
    return {
        "origin": origin.as_posix(),
        "tracked": tracked,
        "status": "pass" if tracked else "fail",
        "reason": None if tracked else "origin-not-tracked",
        **(_local_origin_membership(origin, worktree, local_snapshot) if local_snapshot is not None else {}),
    }


def inspect_collected_test(
    path: Path,
    worktree: Path,
    local_snapshot: dict[str, Any] | None = None,
    *,
    tracked_paths: set[bytes] | None = None,
) -> dict[str, Any]:
    """Verify an actual collected test against clean tracking or admitted local snapshot bytes."""
    origin = path.resolve()
    try:
        relative = origin.relative_to(worktree).as_posix()
    except ValueError:
        return {"origin": origin.as_posix(), "tracked": False, "status": "fail", "reason": "test-outside-worktree"}
    if tracked_paths is None:
        tracked_paths = _tracked_source_paths(worktree, relative) or set()
    tracked = os.fsencode(relative) in tracked_paths
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

    if os.environ.get("CODEX_RIG_SINGLE_PROCESS_PYTEST") == "1" and (
        config.getoption("numprocesses", default=None)
        or config.getoption("tx", default=())
        or config.getoption("dist", default="no") != "no"
    ):
        raise pytest.UsageError("single-process declaration conflicts with effective pytest distribution settings")
    task = json.loads(raw)
    worktree = Path(task["worktree"])
    modules = task["modules"]
    local_source = task.get("local_source")
    local_snapshot = None
    clean_paths = None
    if inventory := task.get("tracked_inventory"):
        content = _read_tracked_inventory(Path(inventory["path"]), inventory["sha256"])
        root, head, names = content.split(b"\0", 2)
        if root != os.fsencode(worktree.as_posix()) or head != inventory["head"].encode("ascii"):
            raise ValueError("tracked-source-inventory-identity-mismatch")
        clean_paths = set(names.split(b"\0")) - {b""}
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
            tracked_paths = clean_paths if clean_paths is not None else _tracked_source_paths(worktree) or set()
            origins = {
                name: inspect_imported_module(name, worktree, local_snapshot, tracked_paths=tracked_paths)
                for name in modules
            }
            tests = {
                path.resolve().as_posix(): inspect_collected_test(
                    path, worktree, local_snapshot, tracked_paths=tracked_paths
                )
                for path in self.paths
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
        binding = json.loads(arguments[5])
        if "tracked_inventory" in binding:
            task["tracked_inventory"] = binding["tracked_inventory"]
        else:
            task["local_source"] = binding
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
    if "tracked_inventory" in task:
        proof = json.loads(_read_gate_file(proof_path, "source-gate-proof", MAX_TEST_PROOF_BYTES))
    else:
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


def _ignored_source_inventory(worktree: Path | None) -> str:
    """Record ignored paths outside tool caches without mixing them into Git status."""
    caches = (".venv", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache")
    ignored = subprocess.run(  # noqa: S603 - fixed local Git inspection with cache exclusions
        [
            *SOURCE_GIT,
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            "-z",
            "--",
            ".",
            *(f":(exclude,glob)**/{cache}/**" for cache in caches),
        ],
        cwd=worktree,
        capture_output=True,
        check=True,
        timeout=SOURCE_INSPECTION_TIMEOUT_SECONDS,
    )
    return json.dumps([os.fsdecode(path) for path in ignored.stdout.split(b"\0") if path])


def _concealed_source_error(flags: str, worktree: Path | None) -> str | None:
    """Reject concealed content or mode changes while preserving Git's platform configuration."""
    concealed = [row for row in flags.split("\0") if row and (row[0].islower() or row[0] == "S")]
    if not concealed:
        return None
    staged = subprocess.run(  # noqa: S603 - fixed local Git inspection
        [*SOURCE_GIT, "ls-files", "-s", "-z"],
        cwd=worktree,
        capture_output=True,
        check=True,
        timeout=SOURCE_INSPECTION_TIMEOUT_SECONDS,
    )
    index = dict(os.fsdecode(row).split("\t", 1)[::-1] for row in staged.stdout.split(b"\0") if row)
    configuration = subprocess.run(  # noqa: S603 - fixed local Git inspection
        [*SOURCE_GIT, "config", "--bool", "--get-regexp", r"^core\.(filemode|symlinks)$"],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
        timeout=SOURCE_INSPECTION_TIMEOUT_SECONDS,
    )
    if configuration.returncode not in {0, 1}:
        raise subprocess.CalledProcessError(configuration.returncode, configuration.args)
    settings = dict(line.split(None, 1) for line in configuration.stdout.splitlines())
    symlinks = settings.get("core.symlinks", "true") == "true"
    filemode = settings.get("core.filemode", "true") == "true"

    def changed_on_disk(row: str) -> bool:
        """Compare one flagged entry; failed comparisons cannot establish a change."""
        path = row[2:]
        target = (worktree or Path.cwd()) / path
        if path not in index:
            raise RuntimeError(f"git-index-entry-unavailable:{path}")
        mode, expected_hash, stage = index[path].split()
        if stage != "0" or not GIT_OBJECT_ID.fullmatch(expected_hash):
            raise RuntimeError(f"git-index-entry-unavailable:{path}")
        try:
            metadata = target.lstat()
        except (FileNotFoundError, NotADirectoryError):
            # Sparse checkouts deliberately omit skip-worktree entries.
            return row[0].upper() != "S"
        if mode == "160000":
            if stat.S_ISDIR(metadata.st_mode):
                raise RuntimeError(f"git-index-gitlink-comparison-unavailable:{path}")
            return True
        link = stat.S_ISLNK(metadata.st_mode)
        regular = stat.S_ISREG(metadata.st_mode)
        # Git permits a regular-file representation of indexed symlinks on hosts without symlink support.
        kind_matches = (link or (not symlinks and regular)) if mode == "120000" else regular
        executable_matches = (
            mode not in {"100644", "100755"}
            or not filemode
            or (bool(metadata.st_mode & stat.S_IXUSR) == (mode == "100755"))
        )
        if not kind_matches or not executable_matches:
            return True
        observed = subprocess.run(  # noqa: S603 - index-derived path after the option terminator
            [*SOURCE_GIT, "hash-object", "--stdin"] if link else [*SOURCE_GIT, "hash-object", "--", path],
            cwd=worktree,
            input=os.fsencode(os.readlink(target)) if link else None,
            capture_output=True,
            check=False,
            timeout=SOURCE_INSPECTION_TIMEOUT_SECONDS,
        )
        observed.check_returncode()
        digest = observed.stdout.strip().decode("ascii")
        if len(digest) != len(expected_hash) or not re.fullmatch("[0-9a-f]+", digest):
            raise RuntimeError(f"git-index-hash-unavailable:{path}")
        return digest != expected_hash

    changed = []
    unavailable = None
    for row in concealed:
        try:
            different = changed_on_disk(row)
        except (OSError, RuntimeError, UnicodeError, subprocess.SubprocessError) as error:
            unavailable = error
            continue
        if different:
            changed.append(row[2:])
    if changed:
        return f"git-index-concealed-entries:count={len(changed)}:paths={changed[:10]!r}"
    if unavailable is not None:
        raise unavailable
    return None


def inspect_source(worktree: Path | None = None) -> dict[str, str]:
    """Return the selected Git checkout head and status, or a failed inspection receipt."""
    return _observe_source(worktree)[0]


def _observe_source(worktree: Path | None, expected_head: str | None = None) -> tuple[dict[str, str], str | None]:
    """Retain the Git observation and any proven checkout mismatch independently of later read failures."""
    snapshot = {"head": "", "status": ""}

    def unavailable(detail: str) -> tuple[dict[str, str], str | None]:
        """Preserve earlier affirmative evidence when the remaining inspection cannot finish."""
        mismatch = source_guard_error(snapshot, expected_head) if expected_head and snapshot["head"] else None
        return {**snapshot, "error": detail}, mismatch

    try:
        collect_diff.reject_executable_filters(worktree or Path.cwd())
        if worktree is not None:
            root = subprocess.run(  # noqa: S603 - argv list, no shell; tool resolved via PATH on purpose
                [*SOURCE_GIT, "rev-parse", "--show-toplevel"],
                cwd=worktree,
                capture_output=True,
                check=False,
                timeout=SOURCE_INSPECTION_TIMEOUT_SECONDS,
            )
            root_path = os.fsdecode(root.stdout.rstrip(b"\r\n"))
            if root.returncode != 0 or not root_path or not Path(root_path).is_absolute():
                return unavailable("selected-worktree-is-not-checkout-root")
            if Path(root_path).resolve() != worktree:
                snapshot["error"] = "selected-worktree-is-not-checkout-root"
                return snapshot, source_guard_error(snapshot, expected_head) if expected_head else None
        head = subprocess.run(  # noqa: S603 - argv list, no shell; tool resolved via PATH on purpose
            [*SOURCE_GIT, "rev-parse", "HEAD"],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=False,
            timeout=SOURCE_INSPECTION_TIMEOUT_SECONDS,
        )
        if head.returncode != 0:
            detail = head.stderr.strip() or f"exit {head.returncode}"
            return unavailable(f"git-rev-parse-failed:{detail}")
        revision = head.stdout.strip()
        if not GIT_OBJECT_ID.fullmatch(revision):
            return unavailable("git-rev-parse-invalid-head")
        snapshot["head"] = revision
        # Repository ignore settings must not conceal changed release dependencies.
        status = subprocess.run(  # noqa: S603 - argv list, no shell; tool resolved via PATH on purpose
            [*SOURCE_GIT, "status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none"],
            cwd=worktree,
            capture_output=True,
            check=False,
            timeout=SOURCE_INSPECTION_TIMEOUT_SECONDS,
        )
        if status.returncode != 0:
            detail = os.fsdecode(status.stderr).strip() or f"exit {status.returncode}"
            return unavailable(f"git-status-failed:{detail}")
        snapshot["status"] = os.fsdecode(status.stdout).rstrip("\r\n")
        flags = subprocess.run(  # noqa: S603 - argv list, no shell; tool resolved via PATH on purpose
            [*SOURCE_GIT, "ls-files", "-v", "-z"],
            cwd=worktree,
            capture_output=True,
            check=False,
            timeout=SOURCE_INSPECTION_TIMEOUT_SECONDS,
        )
        if flags.returncode != 0:
            return unavailable(f"git-index-flags-failed:exit {flags.returncode}")
        snapshot["ignored"] = _ignored_source_inventory(worktree)
        concealed_error = _concealed_source_error(os.fsdecode(flags.stdout), worktree)
        if concealed_error:
            snapshot["error"] = concealed_error
        return snapshot, source_guard_error(snapshot, expected_head) if expected_head else None
    except (OSError, RuntimeError, UnicodeError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as error:
        return unavailable(f"git-inspection-failed:{error}")


def source_guard_error(snapshot: dict[str, str], expected_head: str) -> str | None:
    """Explain why one source observation cannot authorize a gate command."""
    if "error" in snapshot:
        return f"source-inspection-failed:{snapshot['error']}"
    if snapshot["head"] != expected_head:
        return f"source-head-mismatch:expected {expected_head}, observed {snapshot['head']}"
    if snapshot["status"]:
        return "source-dirty:git status --porcelain=v1 --untracked-files=all --ignore-submodules=none was non-empty"
    inputs = [
        path
        for path in json.loads(snapshot.get("ignored", "[]"))
        if Path(path).suffix in {".py", ".pth", ".ini"} or Path(path).name in {"pyproject.toml", "setup.cfg"}
    ]
    if inputs:
        return f"source-dirty:ignored-inputs:count={len(inputs)}:paths={inputs[:10]!r}"
    return None


def append_stderr(path: Path, message: str) -> None:
    """Append one source-guard failure to the gate's captured standard error."""
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(f"{message}\n")


def _ruff_configuration_reason(worktree: Path, environment: dict[str, str], lint: bool, plain_lint: bool) -> str | None:
    """Check Ruff's config discovery, Git ignore sources and the effective read-only lint mode before routing."""
    # The selected tool environment may put contributor-provided executables first. Use only the host's
    # system Git for these parent observations, without inherited Git directory, config or index overrides.
    parent_env = {key: environment[key] for key in ("HOME", "XDG_CONFIG_HOME") if key in environment}
    parent_env["PATH"] = os.defpath

    def git(*arguments: str, data: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(  # noqa: S603 - fixed system Git and repository observations
            ["/usr/bin/git", *SOURCE_GIT[1:], *arguments],
            cwd=worktree,
            env=parent_env,
            input=data,
            capture_output=True,
            check=False,
            timeout=SOURCE_INSPECTION_TIMEOUT_SECONDS,
        )

    # Without --exclude-standard this also enumerates ignored source and nested submodule pointers.
    inventory = git("ls-files", "--cached", "--others", "-z")
    if inventory.returncode:
        return "Ruff input inventory unavailable"
    names = inventory.stdout.split(b"\0")[:-1]
    root_config = False
    root_requires_python = False
    all_targets_explicit = True
    config_names = {"ruff.toml", ".ruff.toml", "pyproject.toml"}
    for name in names:
        path = worktree / os.fsdecode(name)
        if (path / ".git").exists():
            return "nested repository needs separate Ruff discovery"
        if path.name not in config_names:
            continue
        if tomllib is None:
            return "TOML parser unavailable to verify Ruff configuration"
        try:
            parsed = tomllib.loads(_read_gate_file(path, "ruff-configuration", 1024 * 1024).decode("utf-8"))
            if path == worktree / "pyproject.toml":
                root_requires_python = isinstance(parsed.get("project", {}).get("requires-python"), str)
            config = parsed.get("tool", {}).get("ruff") if path.name == "pyproject.toml" else parsed
            if config is None:
                continue
            if not isinstance(config, dict) or "extend" in config:
                return "Ruff configuration inheritance needs the standard route"
            all_targets_explicit &= "target-version" in config
            if (lint and config.get("fix-only")) or (plain_lint and config.get("fix")):
                return "Ruff configuration enables fixes"
        except (AttributeError, OSError, ValueError):
            return "Ruff configuration cannot be verified"
        root_config |= path.parent == worktree

    home = Path(environment.get("HOME") or Path.home())
    xdg = Path(environment.get("XDG_CONFIG_HOME") or home / ".config")
    if not root_config and (
        any((directory / name).exists() for directory in worktree.parents for name in config_names)
        or any(
            path.exists() for path in (xdg / "ruff", home / ".config/ruff", home / "Library/Application Support/ruff")
        )
    ):
        return "Ruff may inherit external configuration"
    # Even when a root Ruff config wins, Ruff independently searches upward for requires-python.
    if (
        not root_requires_python
        and not (root_config and all_targets_explicit)
        and any((directory / "pyproject.toml").exists() for directory in worktree.parents)
    ):
        return "Ruff may infer its target Python version outside the worktree"
    if any((directory / ".ignore").exists() for directory in worktree.parents):
        return "Ruff may inherit an ancestor .ignore file"
    if not names:
        return None
    # The nearest .git stops parent .gitignore discovery; global excludes and shared info/exclude
    # still apply. Match actual tracked AND untracked inputs with Git's own ignore engine. Include
    # matching negations: their absence in the sandbox can also change the selected files.
    matches = git("check-ignore", "--no-index", "--verbose", "-z", "--stdin", data=inventory.stdout)
    if matches.returncode not in (0, 1):
        return "Git ignore selection unavailable"
    for source in matches.stdout.split(b"\0")[:-1:4]:
        location = (worktree / os.fsdecode(source)).resolve()
        if not location.is_relative_to(worktree) or location.name != ".gitignore":
            return "Ruff selection uses external Git exclusions"
    return None


#: Option prefixes naming a pytest configuration file, root directory or argument file. Each can point outside the
#: worktree, where the confined child reads an absent file and fails a test run the standard route would complete.
_PYTEST_LOCATION_OPTIONS = ("-c", "--config-file", "--rootdir", "@")
_PYTEST_LOCATION_REASON = "explicit pytest config, root or argument file needs the standard route"


def _effective_pytest_option_reason(options: list[str]) -> str | None:
    """Check the complete option list, including project ``addopts``, for options the confined child cannot honour.

    Example:
        >>> _effective_pytest_option_reason(["-q", "@args.txt"])
        'explicit pytest config, root or argument file needs the standard route'
        >>> _effective_pytest_option_reason(["-q", "-n", "2"])
        'pytest arguments or project configuration request a distribution option'
        >>> _effective_pytest_option_reason(["-q", "-k", "local"]) is None
        True
    """
    if any(option.startswith(_PYTEST_LOCATION_OPTIONS) for option in options):
        return _PYTEST_LOCATION_REASON
    if any(option.startswith(("-n", "--numprocesses", "--dist", "--tx")) for option in options):
        return "pytest arguments or project configuration request a distribution option"
    return None


def _pytest_configuration_reason(worktree: Path, options: list[str]) -> str | None:
    """Keep the original pytest route if hiding enclosing config or starting workers could alter collection."""
    files = ("pytest.toml", ".pytest.toml", "pytest.ini", ".pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg")
    if any((directory / "conftest.py").is_file() for directory in worktree.parents):
        return "pytest may load a conftest outside the worktree"
    if any(option.startswith(_PYTEST_LOCATION_OPTIONS) for option in options):
        return _PYTEST_LOCATION_REASON
    # Pytest expands an addopts override before normal option parsing. Its value can itself
    # introduce configuration, plugins or workers. An empty override introduces no arguments.
    if any(option.partition("addopts=")[2] for option in options if option.startswith(("-", "addopts="))):
        return "pytest addopts override needs the standard route"

    # Pytest can start discovery at a positional path, including the file portion of a node ID.
    # Check all plausible paths without interpreting plugin-defined flags or choosing between config formats.
    checked = {worktree}
    for option in options:
        if option.startswith("-"):
            continue
        try:
            target = Path(os.path.abspath(worktree / option.partition("::")[0]))
            if not target.exists():
                continue
            directory = target if target.is_dir() else target.parent
            if not directory.is_relative_to(worktree):
                return "pytest target may select configuration outside the worktree"
            while directory not in checked:
                if any((directory / name).is_file() for name in files):
                    return "pytest target may select nested configuration"
                checked.add(directory)
                directory = directory.parent
        except (OSError, ValueError):
            return "pytest target configuration cannot be verified"

    owns_config = False
    for name in files:
        config = worktree / name
        if not config.exists():
            continue
        try:
            content = _read_gate_file(config, "pytest-configuration", 1024 * 1024).decode("utf-8")
            if name.endswith(".toml"):
                if tomllib is None:
                    return "TOML parser unavailable to verify pytest configuration"
                document = tomllib.loads(content)
                if name == "pyproject.toml":
                    settings = document.get("tool", {}).get("pytest")
                    if settings is None:
                        continue
                    if not isinstance(settings, dict):
                        return "pytest configuration cannot be verified"
                    # Native TOML tables and pytest.toml require newer pytest. The selected
                    # environment can differ from this runner, so only portable sections prove precedence.
                    owns_config |= "ini_options" in settings
                    settings = settings.get("ini_options", settings)
                else:
                    settings = document.get("pytest", {})
            else:
                parser = configparser.ConfigParser(interpolation=None)
                parser.read_string(content)
                section = "tool:pytest" if name == "setup.cfg" else "pytest"
                if section not in parser and name in ("tox.ini", "setup.cfg"):
                    continue
                settings = parser[section] if section in parser else {}
                owns_config = True
            addopts = settings.get("addopts", [])
            configured = shlex.split(addopts) if isinstance(addopts, str) else addopts
            if not isinstance(configured, list) or any(not isinstance(option, str) for option in configured):
                return "pytest configuration cannot be verified"
            options.extend(configured)
        except (configparser.Error, KeyError, AttributeError, OSError, ValueError):
            return "pytest configuration cannot be verified"
    if not owns_config and any((directory / name).is_file() for directory in worktree.parents for name in files):
        return "pytest may inherit configuration outside the worktree"
    # Configured addopts are expanded into the same command line, so they need the command-line checks too.
    return _effective_pytest_option_reason(options)


def _prepare_gate_profile(
    gate_id: str,
    command: str | list[str],
    checks: Path,
    out_dir: Path,
    expected_head: str | None,
    worktree: Path | None,
    environment: dict[str, str] | None,
    scratch: Path | None,
    single_process: bool,
    stack: ExitStack,
) -> str | tuple[list[str], dict[str, str]]:
    """Return a known-compatible isolated command or explain why the original route applies."""
    if sys.platform != "darwin" or worktree is None or expected_head is None or not (worktree / ".git").is_file():
        return "macOS clean worktree profile not applicable"
    if isinstance(command, list):
        if not single_process or scratch is None:
            return "single-process pytest compatibility was not declared"
        child = _child_environment(checks, worktree, environment, out_dir, scratch)
        options = [*json.loads(command[7]), *shlex.split(child.get("PYTEST_ADDOPTS", ""))]
        if not worktree_sandbox.native_available():
            return "native macOS worktree sandbox unavailable before launch"
        if reason := _pytest_configuration_reason(worktree, options):
            return reason
        prepared = worktree_sandbox.prepare_pytest(command, worktree, scratch, child)
        if isinstance(prepared, tuple):
            argv, isolated = prepared
            return argv, {**isolated, "CODEX_RIG_SINGLE_PROCESS_PYTEST": "1"}
    else:
        arguments = {"format": ["format", "--check", "."], "lint": ["check", "--no-fix", "."]}.get(gate_id)
        allowed = {"lint": ("ruff check .", "ruff check --no-fix ."), "format": ("ruff format --check .",)}
        if not arguments or command not in (*allowed[gate_id], default_commands("darwin")[gate_id]):
            return "command has no matching worktree profile"
        child = _child_environment(checks, worktree, environment, out_dir, scratch)
        # Shell PATH components are relative to the child's checkout, including empty entries.
        search = os.pathsep.join(str(worktree / entry) for entry in child.get("PATH", os.defpath).split(os.pathsep))
        executable = shutil.which("ruff", path=search)
        if executable is None:
            return "standalone Ruff was not resolved; original command owns tool selection"
        lint = gate_id == "lint"
        if not worktree_sandbox.native_available():
            return "native macOS worktree sandbox unavailable before launch"
        if reason := _ruff_configuration_reason(worktree, child, lint, lint and command != "ruff check --no-fix ."):
            return reason
        cache = stack.enter_context(tempfile.TemporaryDirectory(prefix=f"{gate_id}-", dir=checks))
        prepared = worktree_sandbox.prepare_ruff(Path(executable), arguments, worktree, Path(cache).resolve(), child)
        if isinstance(prepared, tuple):
            return prepared
    return _declined_route_reason(prepared)


def _declined_route_reason(declined: str | None) -> str:
    """Name a sandbox decline in the standard route record; the profile builder's reasons carry no input text.

    A declined profile only selects the standard launcher. Contributor-execution decisions and approvals stay with
    the calling workflow and apply to every route.

    Example:
        >>> _declined_route_reason("native sandbox unavailable")
        'worktree sandbox declined: native sandbox unavailable'
        >>> _declined_route_reason(None)
        'worktree profile unavailable or incompatible before launch'
    """
    if declined:
        return f"worktree sandbox declined: {declined}"
    return "worktree profile unavailable or incompatible before launch"


def _launch_gate(
    gate_id: str,
    command: str | list[str],
    timeout: int,
    paths: dict[str, Path],
    out_dir: Path,
    expected_head: str | None,
    worktree: Path | None,
    environment: dict[str, str] | None,
    scratch: Path | None,
    single_process: bool,
) -> tuple[int, float, dict[str, str]]:
    """Select the route before execution; a refused or failed launch has no replay path."""
    with ExitStack() as stack:
        try:
            prepared = _prepare_gate_profile(
                gate_id,
                command,
                out_dir / CHECKS_DIRNAME,
                out_dir,
                expected_head,
                worktree,
                environment,
                scratch,
                single_process,
                stack,
            )
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            paths["stdout"].write_bytes(b"")
            paths["stderr"].write_text(f"worktree-profile-preparation-failed:{error}\n", encoding="utf-8")
            return 1, 0.0, {"mode": "not-started", "reason": "worktree profile preparation refused or failed"}
        if isinstance(prepared, str):
            outcome = execute_command(
                command, timeout, paths["stdout"], paths["stderr"], worktree, environment, out_dir, scratch
            )
            route = {"mode": "standard", "reason": prepared}
        else:
            argv, isolated = prepared
            outcome = execute_command(
                argv, timeout, paths["stdout"], paths["stderr"], worktree, prepared_environment=isolated
            )
            route = {
                "mode": "macos-worktree",
                "reason": "verified native Ruff" if isinstance(command, str) else "declared single-process pytest",
            }
        if isinstance(outcome, _GateLaunchFailure):
            # A standard launch follows a named decision not to confine; keep that decision beside the launch failure.
            failure = f"{prepared}; standard" if isinstance(prepared, str) else "sandbox"
            route = {"mode": "not-started", "reason": f"{failure} launcher refused or failed"}
        return outcome[0], outcome[1], route


class _StructuredCheckout:
    """Bind one pinned attempt to its committed inventory and the first observation that stops execution."""

    def __init__(self, tracked: set[bytes] | None) -> None:
        self.tracked = tracked
        self.rejection: tuple[dict[str, str], str] | None = None

    def observe(
        self, worktree: Path, head: str, *, require_confirmation: bool = False
    ) -> tuple[dict[str, str], str | None]:
        """Compare checkout and index; admission needs a proven mismatch, while active gates retain uncertainty."""
        if self.rejection is not None:
            return self.rejection
        unavailable = None
        snapshot, failure = _observe_source(worktree, head)
        if failure is None and "error" in snapshot:
            snapshot, failure = _observe_source(worktree, head)
            if failure is None and "error" in snapshot:
                unavailable = f"source-inspection-failed:{snapshot['error']}"
        if failure is None and unavailable is None:
            if self.tracked is None:
                raise ValueError("tracked-source-inventory-unavailable")
            current = _tracked_source_paths(worktree)
            if current is None:
                current = _tracked_source_paths(worktree)
            if current == self.tracked:
                return snapshot, None
            if current is None:
                unavailable = "tracked-source-inventory-unavailable"
                snapshot["error"] = unavailable
            else:
                # Both digests identify real observations. The command's exit and any child proof remain independent.
                snapshot["expected_inventory_sha256"] = _tracked_inventory(worktree, head, self.tracked)[1]
                snapshot["observed_inventory_sha256"] = _tracked_inventory(worktree, head, current)[1]
                failure = "tracked-source-inventory-changed"
        if unavailable is not None:
            if require_confirmation:
                raise ValueError(unavailable)
            failure = unavailable
        self.rejection = snapshot, failure
        return self.rejection


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
    *,
    run_root: Path | None = None,
    checkout: _StructuredCheckout | None = None,
) -> dict[str, Any]:
    """Run one gate and bind local execution artifacts to the observed source and child proof."""
    run_root = run_root or out_dir
    # The confined child needs its scratch beside the tracked input. Use their real private coordinates
    # throughout execution and replay; only the validated receipt can publish the digest-named input.
    input_checks = (out_dir if expected_head is not None else run_root) / CHECKS_DIRNAME
    paths, recorded = check_paths(gate_id, out_dir)
    source_bound = expected_head is not None or local_source is not None
    artifact_escape = "local-gate-artifact-escape" if local_source is not None else "source-gate-artifact-escape"
    if source_bound:
        for path in paths.values():
            if path.is_symlink() or path.parent.is_symlink() or not path.resolve().is_relative_to(out_dir.resolve()):
                raise ValueError(artifact_escape)
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
    scratch = None
    scratch_identity = None
    checks_identity = None
    child_proof = proof_path
    if gate_id == "tests" and python_test is not None:
        if worktree is None:
            raise RuntimeError("worktree must not be None")
        checks_identity = proof_path.parent.lstat()
        scratch = input_checks.resolve() / f"pytest-scratch-{secrets.token_hex(16)}"
        child_proof = scratch / proof_path.name
        python_test_argv = [
            str(python_test.interpreter),
            "-B",
            str(Path(__file__).resolve()),
            "--_python-tests-child",
            worktree.as_posix(),
            str(child_proof),
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
    command_bytes = f"{command}\n".encode()
    paths["command"].write_bytes(command_bytes)
    source: dict[str, Any] | None = None
    tracked_before = None
    if expected_head is not None:
        if checkout is None:
            before = inspect_source(worktree)
            error = source_guard_error(before, expected_head)
        else:
            before, error = checkout.observe(worktree, expected_head)
        source = {"expected_head": expected_head, "before": before, "after": None}
        if worktree is not None:
            source["worktree"] = worktree.as_posix()
        if error is None and python_test_argv is not None and worktree is not None:
            tracked_before = checkout.tracked if checkout is not None else _tracked_source_paths(worktree)
            if not tracked_before:
                error = "tracked-source-inventory-empty-or-unavailable"
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
            current = validate_local_gate_source(run_root, worktree)
            if (
                hashlib.sha256((run_root / local_source["receipt_path"]).read_bytes()).hexdigest()
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
    if scratch is not None:
        try:
            if scratch.is_symlink() or scratch.resolve() != input_checks.resolve() / scratch.name:
                raise ValueError("source-gate-pytest-scratch-redirected")
            scratch.mkdir(mode=0o700)
            scratch_identity = scratch.lstat()
            if not stat.S_ISDIR(scratch_identity.st_mode):
                raise ValueError("source-gate-pytest-scratch-invalid")
        except (OSError, ValueError, RuntimeError) as scratch_error:
            paths["stdout"].write_bytes(b"")
            append_stderr(paths["stderr"], str(scratch_error))
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
    inventory = None
    if tracked_before is not None:
        # Keep exact raw index names out of argv and retain the same digest-addressed input for receipt replay.
        content, digest = _tracked_inventory(worktree, expected_head, tracked_before)
        inventory = input_checks / f"tracked-source-{digest}.bin"
        try:
            with inventory.open("xb") as stream:
                stream.write(content)
        except FileExistsError:
            _read_tracked_inventory(inventory, digest)
        binding = {"path": str(inventory.resolve()), "head": expected_head, "sha256": digest}
        python_test_argv.append(json.dumps({"tracked_inventory": binding}))
        command_bytes = f"argv-json:{json.dumps(python_test_argv)}\n".encode()
        paths["command"].write_bytes(command_bytes)
    exit_code, duration, route = _launch_gate(
        gate_id,
        python_test_argv or command,
        timeout,
        paths,
        run_root,
        expected_head,
        worktree,
        environment,
        scratch if expected_head is not None else None,
        python_test.single_process if python_test is not None else False,
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
        "execution_route": route,
        "command_path": recorded["command"],
        "stdout": recorded["stdout"],
        "stderr": recorded["stderr"],
    }
    proof_bytes: bytes | None = None
    if python_test_argv is not None and route["mode"] != "not-started":
        try:
            if scratch is not None and not os.path.samestat(scratch_identity, scratch.lstat()):
                raise ValueError("source-gate-pytest-scratch-changed")
            if source_bound and (
                child_proof.is_symlink()
                or child_proof.parent.is_symlink()
                or proof_path.parent.is_symlink()
                or not child_proof.resolve().is_relative_to(input_checks.resolve())
                or not child_proof.is_file()
            ):
                raise ValueError("source-gate-proof-artifact-invalid")
            proof_bytes = _read_gate_file(child_proof, "source-gate-proof", MAX_TEST_PROOF_BYTES)
            proof = json.loads(proof_bytes.decode("utf-8"))
            if not isinstance(proof, dict):
                raise ValueError("python-import-proof-not-object")
            if (
                proof.get("mode") != "in-process-pytest"
                or proof.get("worktree") != worktree.as_posix()
                or proof.get("invoked_interpreter") != python_test.interpreter.as_posix()
                or proof.get("status") not in ("pass", "fail", "inconclusive")
                or not isinstance(proof.get("modules"), dict)
                or set(proof["modules"]) != set(python_test.modules)
                or not isinstance(proof.get("tests"), dict)
                or not isinstance(proof.get("workers"), dict)
                or not isinstance(proof.get("errors"), list)
                or (proof["status"] == "pass" and (not proof["tests"] or proof["errors"]))
            ):
                raise ValueError("source-gate-proof-identity-mismatch")
            _publish_gate_proof(proof_path, proof_bytes, checks_identity)
        except (OSError, ValueError, UnicodeError) as error:
            if not source_bound and not isinstance(error, (OSError, json.JSONDecodeError)):
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
            if source_bound:
                append_stderr(paths["stderr"], f"source-gate-proof-rejected:{error}")
                observation = (json.dumps(proof, indent=2) + "\n").encode("utf-8")
                try:
                    _publish_gate_proof(proof_path, observation, checks_identity)
                except (OSError, ValueError) as publication_error:
                    append_stderr(paths["stderr"], f"source-gate-parent-proof-unavailable:{publication_error}")
                else:
                    proof_bytes = observation
        result["python_imports"] = proof
        if proof.get("status") != "pass":
            append_stderr(paths["stderr"], f"python-import-origin:{proof.get('status', 'invalid')}")
            if result["status"] == "pass":
                result["status"] = "fail"
    if local_source is not None:
        try:
            after = validate_local_gate_source(run_root, worktree)
            if (
                hashlib.sha256((run_root / local_source["receipt_path"]).read_bytes()).hexdigest()
                != local_source["receipt_sha256"]
            ):
                raise ValueError("local-gate-receipt-changed")
            digest = hashlib.sha256(_local_snapshot_bytes(after)).hexdigest()
            source["after"] = {"snapshot_sha256": digest}
            if digest != local_source["snapshot_sha256"]:
                raise ValueError("local-gate-source-changed")
        except (OSError, ValueError, RuntimeError, KeyError) as error:
            source["guard_error"] = str(error) or type(error).__name__
            append_stderr(paths["stderr"], source["guard_error"])
            if result["status"] == "pass":
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
                source["guard_error"] = str(error) or type(error).__name__
                append_stderr(paths["stderr"], source["guard_error"])
                if result["status"] == "pass":
                    result["status"] = "fail"
        result["source"] = source
    elif source is not None:
        if checkout is None:
            after = inspect_source(worktree)
            error = source_guard_error(after, expected_head)
        else:
            after, error = checkout.observe(worktree, expected_head)
        source["after"] = after
        result["source"] = source
        if inventory is not None:
            try:
                if (
                    proof_bytes is not None
                    and _read_gate_file(proof_path, "source-gate-proof", MAX_TEST_PROOF_BYTES) != proof_bytes
                ):
                    raise ValueError("source-gate-proof-artifact-changed")
                _read_tracked_inventory(inventory, binding["sha256"])
            except (OSError, ValueError) as inventory_error:
                error = error or str(inventory_error)
                inventory = None
            else:
                if checkout is None and tracked_before != _tracked_source_paths(worktree):
                    error = error or "tracked-source-inventory-changed"
                if error == "tracked-source-inventory-changed" and result["status"] == "pass":
                    source["guard_error"] = error
                    # A child may finish stderr without a newline. The reader verifies this diagnostic as a whole line.
                    error = "\n" + error
        if error is not None:
            append_stderr(paths["stderr"], error)
            if result["status"] == "pass":
                result["status"] = "fail"
    if source is not None:
        for path in paths.values():
            if path.is_symlink() or path.parent.is_symlink() or not path.resolve().is_relative_to(out_dir.resolve()):
                raise ValueError(artifact_escape)
        source["artifacts"] = {
            recorded["command"]: hashlib.sha256(command_bytes).hexdigest(),
            recorded["stdout"]: hashlib.sha256(paths["stdout"].read_bytes()).hexdigest(),
            recorded["stderr"]: hashlib.sha256(paths["stderr"].read_bytes()).hexdigest(),
        }
        if inventory is not None:
            source["artifacts"][f"{CHECKS_DIRNAME}/{inventory.name}"] = binding["sha256"]
        if proof_bytes is not None:
            source["artifacts"]["checks/tests.python-imports.json"] = hashlib.sha256(proof_bytes).hexdigest()
    if status == "timeout":
        result["reason"] = f"timeout after {timeout} seconds"
    elif status == "missing-command":
        first_line = paths["stderr"].read_text(encoding="utf-8", errors="replace").splitlines()
        result["reason"] = first_line[0] if first_line else "command exited 127"
    return result


# Only these fixed members may be replaced. Digest-addressed Git inputs are handled separately.
_GATE_SUMMARIES = ("gates.json", "gates.checks.jsonl", "gates.txt", "failed.txt")
_GATE_LOGS = {f"checks/{gate}.{kind}.txt" for gate in GATE_IDS for kind in ("command", "stdout", "stderr")}
_GATE_REPORTS = (
    *_GATE_SUMMARIES,
    *sorted(_GATE_LOGS),
    "checks/tests-skip-approval.json",
    "checks/tests.python-imports.json",
    "checks/local-source-snapshot.json",
)
_PUBLICATION = ".gate-publication.json"
_ARCHIVE_NUMBER = re.compile(r"00[1-9]|0[1-9][0-9]|[1-9][0-9]{2,}")


def _receipt_logs(root: Path, gates: dict[str, Any], original: Path, archive: Path | None = None) -> set[str]:
    """Resolve historical log ownership and retain directories required to replay the recorded spellings."""
    resolve_log = importlib.import_module("validate-artifacts")._resolve_gate_log
    logs = set()
    for check in gates["checks"]:
        for key in ("command_path", "stdout", "stderr"):
            value = check.get(key)
            if not isinstance(value, str) or not value:
                raise ValueError("invalid-previous-gates")
            path = resolve_log(root, Path(value), original_run=original)
            if path is None or not path.is_relative_to(root):
                raise ValueError("legacy-gate-log-outside-output")
            normalized = Path(os.path.normpath(path))
            if not normalized.is_relative_to(root) or path.resolve() != normalized:
                raise ValueError("legacy-gate-log-outside-output")
            if archive is not None and ".." in path.relative_to(root).parts:
                # The ordinary reader walks the recorded name; even an empty directory before '..' must exist.
                for parent in path.parents:
                    if parent == root:
                        break
                    (archive / parent.resolve().relative_to(root)).mkdir(parents=True, exist_ok=True)
            logs.add(normalized.relative_to(root).as_posix())
    # The reader accepts different spellings of one file on case-insensitive volumes. Keep the recorded
    # spelling for journal replay, and authorize the canonical destination only when it is the same file.
    historical = logs.difference(_GATE_REPORTS)
    if historical:
        for relative in _GATE_LOGS - logs:
            canonical = root / relative
            if canonical.is_file() and any(canonical.samefile(root / name) for name in historical):
                logs.add(relative)
    return logs


def _require_undamaged_output(output: Path) -> None:
    """Refuse a reader-visible damage marker without assuming ownership of it."""
    if os.path.lexists(output / "gate-log-errors.json"):
        raise ValueError("gate-evidence-damaged-gate-log")


def _receipt_inventories(gates: dict[str, Any]) -> dict[str, str]:
    """Return the content-addressed public inputs bound to this attempt's tests receipt."""
    tests = next(check for check in gates["checks"] if check["id"] == "tests")
    source = tests.get("source")
    hashes = source.get("artifacts", {}) if isinstance(source, dict) else {}
    if not isinstance(hashes, dict):
        raise ValueError("invalid-gate-test-artifacts")
    bindings = {name: digest for name, digest in hashes.items() if name.startswith("checks/tracked-source-")}
    for name, digest in bindings.items():
        if (
            not isinstance(digest, str)
            or not re.fullmatch("[0-9a-f]{64}", digest)
            or name != f"checks/tracked-source-{digest}.bin"
        ):
            raise ValueError("tracked-source-inventory-identity-mismatch")
    return bindings


def _validate_staged_gates(
    stage: Path,
    output: Path | None,
    damaged: dict[str, str | None] | None = None,
    damaged_logs: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Read a manifest using the public validator, rebasing staged or archived evidence when needed."""
    try:
        validate = importlib.import_module("validate-artifacts")._validate_gates
        return validate(
            stage, mirror_root=output, damaged_inventories=damaged or None, damaged_logs=damaged_logs or None
        )
    except SystemExit as error:
        raise ValueError(f"gate-publication-invalid:{error}") from error


def _file_state(path: Path) -> list[int | str] | None:
    """Bind publication to a regular file's identity and bytes, including for ordinary unbound receipts."""
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    content = _read_gate_file(path, "gate-publication-artifact")
    after = path.lstat()
    if not os.path.samestat(before, after) or before.st_ctime_ns != after.st_ctime_ns:
        raise ValueError("gate-publication-artifact-changed")
    return [after.st_dev, after.st_ino, after.st_ctime_ns, after.st_mtime_ns, hashlib.sha256(content).hexdigest()]


@contextmanager
def _gate_writer(output: Path):
    """Hold one OS-released writer lock across preflight, execution, publication and interrupted recovery."""
    path = output / ".gate-writer.lock"
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("gate-writer-lock-invalid")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags, 0o600)
    try:
        observed = os.fstat(fd)
        if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1 or not os.path.samestat(observed, path.lstat()):
            raise ValueError("gate-writer-lock-invalid")
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise ValueError("gate-writer-busy-or-unavailable") from error
        yield
    finally:
        os.close(fd)


def _verify_previous_archive(archived: Path, output: Path, plan: dict[str, Any]) -> None:
    """Recheck the complete historical attempt against the files admitted before the new commands ran."""
    identity = archived.lstat()
    if not stat.S_ISDIR(identity.st_mode) or [identity.st_dev, identity.st_ino] != plan["archive_identity"]:
        raise ValueError("gate-publication-archive-changed")
    previous = _validate_staged_gates(archived, output, plan["damaged"], plan["damaged_logs"])
    logs = _receipt_logs(archived, previous, output)
    if set(plan["legacy_logs"]) != logs.difference(_GATE_REPORTS):
        raise ValueError("gate-publication-archive-changed")
    for name, original in plan["before"].items():
        # New immutable inputs might already exist publicly, but did not belong to the prior attempt.
        if name not in _GATE_REPORTS and name not in plan["damaged"]:
            continue
        if original is not None and name in _GATE_LOGS and name not in logs:
            raise ValueError(f"gate-publication-unowned-log:{name}")
        saved = _file_state(archived / name)
        if (saved[-1] if saved else None) != (original[-1] if original else None):
            raise ValueError("gate-publication-archive-changed")
    for relative, digest in plan["legacy_logs"].items():
        if hashlib.sha256(_read_gate_file(archived / relative, "legacy-gate-log")).hexdigest() != digest:
            raise ValueError("gate-publication-archive-changed")
    if not set(plan["damaged"]).issubset(_receipt_inventories(previous)):
        raise ValueError("gate-publication-unowned-inventory")


def _moved_diagnostic(history: Path, number: str, plan: dict[str, Any]) -> Path:
    """Find an interrupted relocation by its journaled directory identity, even after a slot collision."""
    candidates = (
        [path for path in history.iterdir() if _ARCHIVE_NUMBER.fullmatch(path.name)] if history.exists() else []
    )
    if plan["damaged"] and not plan["damaged_logs"]:
        # Older writers published inventory damage into complete history before installing the live report.
        candidates.append(history.parent / "gate-attempts" / number)
    for path in candidates:
        if path.parent.is_symlink():
            continue
        try:
            identity = path.lstat()
        except (FileNotFoundError, NotADirectoryError):
            continue
        if stat.S_ISDIR(identity.st_mode) and [identity.st_dev, identity.st_ino] == plan["archive_identity"]:
            return path
    raise ValueError("gate-publication-archive-changed")


def _finish_publication(output: Path) -> None:
    """Resume only the bounded, digest-identified candidate protected by the writer lock."""
    _require_undamaged_output(output)
    marker = output / _PUBLICATION
    if not marker.exists() and not marker.is_symlink():
        return
    raw_marker = _read_gate_file(marker, "gate-publication-marker")
    owner = json.loads(raw_marker)
    if not isinstance(owner, dict) or not re.fullmatch(r"\.gate-stage-[0-9a-f]{32}", str(owner.get("stage"))):
        raise ValueError("gate-publication-invalid-stage")
    stage = output / owner["stage"]
    observed = stage.lstat()
    if (
        not stat.S_ISDIR(observed.st_mode)
        or stage.resolve().parent != output.resolve()
        or [observed.st_dev, observed.st_ino] != owner.get("identity")
    ):
        raise ValueError("gate-publication-invalid-stage")
    raw = _read_gate_file(stage / "publication.json", "gate-publication-plan")
    if hashlib.sha256(raw).hexdigest() != owner.get("sha256"):
        raise ValueError("gate-publication-plan-changed")
    plan = json.loads(raw)
    if not isinstance(plan, dict) or set(plan) != {
        "before",
        "after",
        "archive",
        "archive_identity",
        "damaged",
        "damaged_logs",
        "legacy_logs",
    }:
        raise ValueError("gate-publication-invalid-plan")
    before, after = plan["before"], plan["after"]
    if (
        not isinstance(before, dict)
        or not isinstance(after, dict)
        or set(before) != set(after)
        or not set(_GATE_REPORTS).difference({"checks/tests-skip-approval.json"}).issubset(after)
        or not isinstance(plan["damaged"], dict)
        or not isinstance(plan["damaged_logs"], dict)
        or not isinstance(plan["legacy_logs"], dict)
        or (before["gates.json"] is None) != (plan["archive"] is None)
    ):
        raise ValueError("gate-publication-invalid-plan")
    if plan["archive"] is None and any(before.get(name) is not None for name in _GATE_REPORTS):
        raise ValueError("gate-publication-invalid-plan")
    if (output / CHECKS_DIRNAME).is_symlink() or (stage / CHECKS_DIRNAME).is_symlink():
        raise ValueError("gate-checks-directory-redirected")
    candidate = _validate_staged_gates(stage, output)
    inventories = _receipt_inventories(candidate)
    # Journals can outlive their writer. Recompute every inventory installation or authorized retirement
    # from the receipts, including when an older journal previously admitted a name found in scratch.
    expected_inputs = {**dict.fromkeys(plan["damaged"]), **inventories}
    if {key: value for key, value in after.items() if key not in _GATE_REPORTS} != expected_inputs or (
        plan["damaged"] and plan["archive"] is None
    ):
        raise ValueError("gate-publication-unowned-inventory")
    for name in after:
        if name not in _GATE_REPORTS and not re.fullmatch(r"checks/tracked-source-[0-9a-f]{64}\.bin", name):
            raise ValueError("gate-publication-invalid-member")
        prior = before[name]
        if prior is not None and (
            not isinstance(prior, list)
            or len(prior) != 5
            or not all(isinstance(number, int) for number in prior[:4])
            or not re.fullmatch("[0-9a-f]{64}", str(prior[-1]))
        ):
            raise ValueError("gate-publication-invalid-member")
        state = _file_state(stage / name) if name in _GATE_REPORTS or name in inventories else None
        if (state[-1] if state else None) != after[name]:
            raise ValueError("gate-publication-candidate-changed")
    number = plan["archive"]
    if number is not None:
        if not isinstance(number, str) or not _ARCHIVE_NUMBER.fullmatch(number):
            raise ValueError("gate-publication-invalid-archive")
        diagnostic = bool(plan["damaged_logs"] or plan["damaged"])
        history = output / ("gate-diagnostics" if diagnostic else "gate-attempts")
        pending_archive = stage / "previous"
        destination = history / number
        if history.is_symlink() or pending_archive.is_symlink():
            raise ValueError("gate-attempts-directory-redirected")
        archived = pending_archive
        if not archived.exists():
            archived = _moved_diagnostic(history, number, plan) if diagnostic else destination
        if archived.is_symlink():
            raise ValueError("gate-attempts-directory-redirected")
        _verify_previous_archive(archived, output, plan)
        if archived.parent != history:
            history.mkdir(exist_ok=True)
            if diagnostic:
                index = int(number)
                while os.path.lexists(destination):
                    index += 1
                    destination = history / f"{index:03d}"
            elif destination.is_symlink():
                raise ValueError("gate-attempts-directory-redirected")
            if destination.exists():
                raise ValueError("gate-publication-archive-occupied")
            archived.rename(destination)
    # During installation no completed manifest names mixed members. Its private candidate is durable for replay.
    _require_undamaged_output(output)
    manifest = output / "gates.json"
    state = _file_state(manifest)
    withdrawn = stage / "previous-manifest"
    if state is not None and state == before["gates.json"]:
        if withdrawn.exists() or withdrawn.is_symlink():
            raise ValueError("gate-publication-previous-manifest-changed")
        manifest.rename(withdrawn)
    elif state is not None and state[-1] != after["gates.json"]:
        raise ValueError("gate-publication-current-changed:gates.json")
    for name in (*sorted(set(after) - {"gates.json"}), "gates.json"):
        target = output / name
        state = _file_state(target)
        if (state[-1] if state else None) == after[name]:
            continue  # A previously interrupted installation may already have installed this exact member.
        if state != before[name] and not (name == "gates.json" and state is None):
            raise ValueError(f"gate-publication-current-changed:{name}")
        if after[name] is None:
            retained = stage / ("retired-" + target.name)
            if os.path.lexists(retained):
                raise ValueError("gate-publication-retired-member-occupied")
            target.rename(retained)
        else:
            # Copy to an exclusive private sibling, leaving the complete staged attempt available for recovery.
            with tempfile.NamedTemporaryFile(dir=stage, delete=False) as pending:
                pending.write(_read_gate_file(stage / name, "gate-publication-candidate"))
            if _file_state(target) != state:
                raise ValueError(f"gate-publication-current-changed:{name}")
            os.replace(pending.name, target)
    for name, digest in after.items():
        installed = _file_state(output / name)
        if (installed[-1] if installed else None) != digest:
            raise ValueError(f"gate-publication-installed-changed:{name}")
    collector = "local-source/review-worktree.json"
    if (stage / collector).exists():
        if _read_gate_file(stage / collector, "gate-staged-collector") != _read_gate_file(
            output / collector, "gate-live-collector"
        ):
            raise ValueError("gate-publication-collector-changed")
    if _read_gate_file(marker, "gate-publication-marker") != raw_marker:
        raise ValueError("gate-publication-marker-changed")
    _require_undamaged_output(output)
    marker.unlink()
    if _validate_staged_gates(output, None) != candidate:
        raise ValueError("gate-publication-installed-changed:gates.json")


def preserve_previous_attempt(
    output: Path, skip_reasons: dict[str, str], archive: Path, recovery_source: tuple[str, str | None]
) -> tuple[dict[str, str | None], dict[str, str], dict[str, str]]:
    """Archive existing gate evidence before rerun, refusing failed-to-skipped recovery.

    Re-executing a failed check can establish success; changing its applicability cannot. A pinned-source retry can
    retain a receipt-bound damaged capture as diagnostic evidence. Other unreadable state stops publication.
    """
    manifest = output / "gates.json"
    if not manifest.exists() and not manifest.is_symlink():
        if any((output / name).exists() or (output / name).is_symlink() for name in _GATE_REPORTS):
            raise ValueError("invalid-previous-gates")
        return {}, {}, {}
    prior = json.loads(_read_gate_file(manifest, "gate-previous-manifest"))
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

    # Never follow an inventory redirect. Record damaged regular inputs alongside the original archived
    # receipts so the next run can regenerate its own index without repairing the historical evidence.
    checks_dir = output / CHECKS_DIRNAME
    if checks_dir.is_symlink():
        raise ValueError("gate-checks-directory-redirected")
    logs = _receipt_logs(output, prior, output, archive)
    for relative in _GATE_LOGS - logs:
        if os.path.lexists(output / relative):
            raise ValueError(f"gate-publication-unowned-log:{relative}")
    legacy_logs: dict[str, str] = {}
    for relative in sorted(logs.difference(_GATE_REPORTS)):
        content = _read_gate_file(output / relative, "legacy-gate-log")
        destination = archive / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        legacy_logs[relative] = hashlib.sha256(content).hexdigest()
    prior_tests = next(check for check in checks if check["id"] == "tests")
    prior_source = prior_tests.get("source")
    hashes = prior_source.get("artifacts", {}) if isinstance(prior_source, dict) else {}
    if not isinstance(hashes, dict):
        raise ValueError("invalid-previous-gates")
    # Matching filenames can belong to earlier attempts or another owner. Only this receipt authorizes repair.
    inventories = _receipt_inventories(prior)
    damaged: dict[str, str | None] = {}
    for relative, expected in inventories.items():
        try:
            observed = hashlib.sha256(_read_tracked_inventory(output / relative)).hexdigest()
        except FileNotFoundError:
            observed = None
        if observed != expected:
            damaged[relative] = observed
    # Only named members can be retained. An absent canonical log can belong to the candidate alone.
    (archive / CHECKS_DIRNAME).mkdir(parents=True, exist_ok=True)
    for relative in (*_GATE_REPORTS, *inventories):
        if relative in _GATE_LOGS and relative not in logs:
            continue
        if relative == "checks/tests-skip-approval.json" and "approval" not in prior_tests:
            continue
        source = output / relative
        if source.exists() or source.is_symlink():
            label = (
                "local-gate-previous-snapshot"
                if relative == "checks/local-source-snapshot.json"
                else "gate-previous-evidence"
            )
            (archive / relative).write_bytes(_read_gate_file(source, label))
    # A local gate binds one collector receipt outside checks; its mirror itself is never archived.
    mirror_source = prior.get("source")
    if isinstance(mirror_source, dict) and mirror_source.get("mode") == "local-review-mirror":
        relative = "local-source/review-worktree.json"
        snapshot = "checks/local-source-snapshot.json"
        if mirror_source.get("receipt_path") != relative or mirror_source.get("snapshot_path") != snapshot:
            raise ValueError("local-gate-previous-source-path-invalid")
        receipt = _read_gate_file(output / relative, "local-gate-previous-receipt")
        if hashlib.sha256(receipt).hexdigest() != mirror_source.get("receipt_sha256"):
            raise ValueError("local-gate-previous-receipt-changed")
        live_snapshot = output / snapshot
        content = _read_gate_file(live_snapshot, "local-gate-previous-snapshot")
        archived_snapshot = _read_gate_file(archive / snapshot, "local-gate-archived-snapshot")
        if content != archived_snapshot or hashlib.sha256(content).hexdigest() != mirror_source.get("snapshot_sha256"):
            raise ValueError("local-gate-previous-snapshot-changed")
        destination = archive / relative
        destination.parent.mkdir(exist_ok=True)
        with destination.open("xb") as stream:
            stream.write(receipt)
    # The previous test receipt owns retirement, even when the next attempt changes mode.
    relative = "checks/tests.python-imports.json"
    proof_path = output / relative
    retire_proof = relative in hashes or proof_path.exists() or proof_path.is_symlink()
    if retire_proof:
        for location in (archive / relative, proof_path):
            content = _read_gate_file(location, "source-gate-proof", MAX_TEST_PROOF_BYTES)
            if hashlib.sha256(content).hexdigest() != hashes.get(relative):
                raise ValueError("source-gate-previous-proof-unowned")
    if damaged:
        (archive / "tracked-source-errors.json").write_text(json.dumps(damaged, indent=2) + "\n", encoding="utf-8")
    damaged_logs: dict[str, str] = {}
    if (
        not damaged
        and isinstance(mirror_source, dict)
        and recovery_source[1] is not None
        and (mirror_source.get("worktree"), mirror_source.get("expected_head")) == recovery_source
    ):
        for check in checks:
            source = check.get("source")
            artifacts = source.get("artifacts") if isinstance(source, dict) else None
            if not isinstance(artifacts, dict):
                continue
            for stream in ("stdout", "stderr"):
                relative = f"checks/{check['id']}.{stream}.txt"
                expected = artifacts.get(relative)
                if (
                    check.get(stream) != relative
                    or not isinstance(expected, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", expected)
                ):
                    continue
                observed = hashlib.sha256(_read_gate_file(archive / relative, "prior-gate-log")).hexdigest()
                if observed != expected:
                    damaged_logs[relative] = observed
    if damaged_logs:
        (archive / "gate-log-errors.json").write_text(json.dumps(damaged_logs, indent=2) + "\n", encoding="utf-8")
    _validate_staged_gates(archive, output, damaged, damaged_logs)
    return damaged, damaged_logs, legacy_logs


def main() -> int:
    """Run all five gates and write their canonical aggregate artifacts."""
    arguments = parse_args()
    output = arguments.out.resolve()
    output.mkdir(parents=True, exist_ok=True)
    try:
        with _gate_writer(output):
            _finish_publication(output)
            return _run_attempt(arguments, output)
    except (OSError, ValueError) as error:
        print(f"gate-recovery-blocked:{error}", file=sys.stderr)
        return 2


def _admit_tracked_inventory(
    checks: Path, worktree: Path, expected_head: str, damaged: dict[str, str | None]
) -> _StructuredCheckout:
    """Pin the expected commit membership and admit its destination before running any structured check."""
    tracked = _tracked_source_paths(worktree, commit=expected_head)
    if tracked is None:
        tracked = _tracked_source_paths(worktree, commit=expected_head)
    checkout = _StructuredCheckout(tracked)
    try:
        if tracked is None:
            raise ValueError("tracked-source-inventory-unavailable")
        if not tracked:
            return checkout
        _, digest = _tracked_inventory(worktree, expected_head, tracked)
        path = checks / f"tracked-source-{digest}.bin"
        if f"{CHECKS_DIRNAME}/{path.name}" in damaged or not os.path.lexists(path):
            return checkout
        _read_tracked_inventory(path, digest)
    except (OSError, ValueError):
        # A confirmed mismatch still yields its failed receipt, even if the requested commit is missing.
        # Ambiguous source inspection gets one retry before leaving the existing report untouched.
        _, failure = checkout.observe(worktree, expected_head, require_confirmation=True)
        if failure is None:
            raise
    return checkout


def _run_attempt(arguments: argparse.Namespace, output: Path) -> int:
    """Capture and verify a candidate while the preceding complete receipt remains independently readable."""
    skip_reasons = {gate_id: getattr(arguments, f"skip_{gate_id}") or "" for gate_id in GATE_IDS}

    commands = {gate_id: getattr(arguments, gate_id) for gate_id in GATE_IDS}
    defaults = default_commands(sys.platform)
    for gate_id in GATE_IDS:
        if not commands[gate_id]:
            commands[gate_id] = defaults[gate_id]
    if not ((arguments.worktree or Path.cwd()) / "src").is_dir() and not getattr(arguments, "types"):
        commands["types"] = "$null" if sys.platform == "win32" else ":"
        skip_reasons["types"] = skip_reasons["types"] or "no src directory or typed package target"
    environment = gate_environment()
    if arguments.project_env is not None:
        scripts = arguments.project_env / ("Scripts" if sys.platform == "win32" else "bin")
        environment["PATH"] = os.pathsep.join((str(scripts), environment.get("PATH", "")))
        environment["VIRTUAL_ENV"] = str(arguments.project_env)
        # uv project commands ignore VIRTUAL_ENV; bind nested hooks to the explicitly selected environment too.
        environment["UV_PROJECT_ENVIRONMENT"] = str(arguments.project_env)

    # Admit the entire invocation before retiring the live proof. Keep the validated decision
    # bytes even when the caller supplied a path that the subsequent attempt will update.
    decision = None
    if arguments.test_skip_approval is not None:
        if not skip_reasons["tests"]:
            raise SystemExit("test-skip-approval-without-skip")
        _, decision = prepare_approval(arguments.test_skip_approval, output, skip_reasons["tests"])

    local_source = None
    if arguments.local_snapshot is not None:
        content = _local_snapshot_bytes(arguments.local_snapshot)
        snapshot_path = output / CHECKS_DIRNAME / "local-source-snapshot.json"
        if (
            snapshot_path.is_symlink()
            or not snapshot_path.resolve().is_relative_to(output.resolve())
            or (
                snapshot_path.exists()
                and (
                    not snapshot_path.is_file()
                    or snapshot_path.stat().st_nlink != 1
                    or not os.access(snapshot_path, os.W_OK)
                )
            )
        ):
            raise ValueError("local-gate-artifact-escape")
        receipt_path = output / "local-source/review-worktree.json"
        local_source = {
            "mode": "local-review-mirror",
            "worktree": arguments.worktree.as_posix(),
            "receipt_path": "local-source/review-worktree.json",
            "receipt_sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
            "snapshot_path": "checks/local-source-snapshot.json",
            "snapshot_sha256": hashlib.sha256(content).hexdigest(),
        }
    checks_dir = output / CHECKS_DIRNAME
    if checks_dir.is_symlink():
        raise ValueError("gate-checks-directory-redirected")
    stage = output / f".gate-stage-{secrets.token_hex(16)}"
    stage.mkdir(mode=0o700)
    stage_identity = stage.lstat()
    archive = stage / "previous"
    recovery_source = ((arguments.worktree or Path.cwd()).as_posix(), arguments.expected_head)
    damaged, damaged_logs, legacy_logs = preserve_previous_attempt(output, skip_reasons, archive, recovery_source)
    archive_identity = archive.lstat() if archive.exists() else None
    history = output / ("gate-diagnostics" if damaged_logs or damaged else "gate-attempts")
    if history.is_symlink() or history.resolve() != output / history.name:
        raise ValueError("gate-attempts-directory-redirected")
    if archive_identity is not None and history.exists() and not history.is_dir():
        raise ValueError(f"gate-history-root-occupied:{history.name}")
    checks_dir.mkdir(exist_ok=True)
    (stage / CHECKS_DIRNAME).mkdir()
    approval_name = "checks/tests-skip-approval.json"
    names = set(_GATE_REPORTS) | set(damaged)
    if not (archive / approval_name).exists():
        if decision is None:
            names.discard(approval_name)
        elif os.path.lexists(output / approval_name):
            raise ValueError("gate-publication-unowned-approval")
    before = {name: _file_state(output / name) for name in sorted(names)}
    if archive.exists():
        for name, state in before.items():
            saved = _file_state(archive / name)
            if (state[-1] if state else None) != (saved[-1] if saved else None):
                raise ValueError(f"gate-publication-current-changed:{name}")
    elif any(before.values()):
        raise ValueError("invalid-previous-gates")
    checkout = None
    if arguments.python_test is not None and arguments.expected_head is not None:
        checkout = _admit_tracked_inventory(checks_dir, arguments.worktree, arguments.expected_head, damaged)
    approval = retain_approval(decision, stage, skip_reasons["tests"]) if decision is not None else None
    if local_source is not None:
        (stage / local_source["snapshot_path"]).write_bytes(content)
        collector = stage / local_source["receipt_path"]
        collector.parent.mkdir()
        collector.write_bytes(_read_gate_file(output / local_source["receipt_path"], "local-gate-receipt"))
    # Unbound children inherit the runner's working directory. Record it independently of source verification.
    execution_cwd = (
        Path.cwd().resolve().as_posix() if arguments.worktree is None and arguments.expected_head is None else None
    )
    _require_undamaged_output(output)
    checks = [
        run_check(
            gate_id,
            commands[gate_id],
            skip_reasons[gate_id],
            arguments.timeout_seconds,
            stage,
            arguments.expected_head,
            arguments.worktree,
            arguments.python_test,
            environment,
            local_source,
            run_root=output,
            checkout=checkout,
        )
        for gate_id in GATE_IDS
    ]
    failed = [check["id"] for check in checks if check["status"] in {"fail", "missing-command", "timeout"}]
    if approval is not None:
        next(check for check in checks if check["id"] == "tests")["approval"] = approval
    status = "timeout" if any(check["status"] == "timeout" for check in checks) else "fail" if failed else "pass"
    not_applicable = [check["id"] for check in checks if check["status"] == "not-applicable"]
    (stage / "gates.txt").write_text(
        "".join(f"{check['id']}:{check['status']}\n" for check in checks), encoding="utf-8"
    )
    (stage / "failed.txt").write_text("".join(f"{gate_id}\n" for gate_id in failed), encoding="utf-8")
    (stage / "gates.checks.jsonl").write_text(
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
    if execution_cwd is None:
        payload["source"] = local_source or {
            "worktree": (arguments.worktree or Path.cwd()).as_posix(),
            "expected_head": arguments.expected_head,
        }
        if arguments.project_env is not None:
            payload["source"]["project_env"] = arguments.project_env.as_posix()
    elif any("execution_route" in check for check in checks):
        payload["execution_cwd"] = execution_cwd
    result_path = stage / "gates.json"
    result_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    if not os.path.samestat(stage_identity, stage.lstat()) or stage.is_symlink():
        raise ValueError("gate-publication-invalid-stage")
    verified = _validate_staged_gates(stage, output)
    inventories = _receipt_inventories(verified)
    for name, digest in inventories.items():
        if name not in before:
            state = _file_state(output / name)
            if state and state[-1] != digest:
                raise ValueError("tracked-source-inventory-changed")
            before[name] = state
    after = {}
    for name, state in before.items():
        if _file_state(output / name) != state:
            raise ValueError(f"gate-publication-current-changed:{name}")
        candidate = _file_state(stage / name) if name in _GATE_REPORTS or name in inventories else None
        after[name] = candidate[-1] if candidate else None
        if name in inventories and after[name] != inventories[name]:
            raise ValueError("tracked-source-inventory-changed")
    number = None
    if archive_identity is not None:
        history.mkdir(exist_ok=True)
        index = 1
        while os.path.lexists(history / f"{index:03d}"):
            index += 1
        number = f"{index:03d}"
    plan = {
        "before": before,
        "after": after,
        "archive": number,
        "damaged": damaged,
        "damaged_logs": damaged_logs,
        "legacy_logs": legacy_logs,
        "archive_identity": [archive_identity.st_dev, archive_identity.st_ino] if archive_identity else None,
    }
    if number is not None:
        _verify_previous_archive(archive, output, plan)
    raw = json.dumps(plan).encode()
    (stage / "publication.json").write_bytes(raw)
    marker = stage / "marker"
    marker.write_text(
        json.dumps(
            {
                "stage": stage.name,
                "identity": [stage_identity.st_dev, stage_identity.st_ino],
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    if os.path.lexists(output / _PUBLICATION):
        raise ValueError("gate-publication-marker-occupied")
    _require_undamaged_output(output)
    marker.replace(output / _PUBLICATION)
    _finish_publication(output)
    result_path = output / "gates.json"
    print(result_path)
    if arguments.python_test is not None and "tests" in failed:
        test_errors = (output / CHECKS_DIRNAME / "tests.stderr.txt").read_text(encoding="utf-8", errors="replace")
        if "ModuleNotFoundError: No module named " in test_errors:
            print(
                "Test gate failed on an import. Check the declared dependency and follow the owning workflow's "
                "dependency preparation rules; retain the failure and continue independent review work where "
                "authorized. A missing package alone does not complete recovery.",
                file=sys.stderr,
            )
    return 124 if status == "timeout" else 1 if status == "fail" else 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--_python-tests-child"]:
        sys.exit(run_python_tests_child(sys.argv[2:]))
    sys.exit(main())
