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
An earlier failed, timed-out or source-refused applicable check must run again; a refused attempt is archived like any
other complete attempt once its reason is repaired. Unauthenticated or displaced evidence blocks replacement.

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
Use ``--shards N`` with explicit pytest paths after ``--`` in the JSON array. Pytest collects each selection before
the shards run. Their node IDs must be disjoint and coverage must be inactive. Run coverage as one suite so its
threshold applies once.
Each sequential shard receives the same pytest options and a fresh ``--timeout-seconds`` budget. The tests gate retains
each shard's receipt and logs. Its status reflects the worst outcome and its logs also include every code failure.
Use one shared pytest configuration and select paths so every shard collects tests after filtering. Pytest's
no-tests-collected exit remains a failure.
Ordinary gates use their configured environment. A retained contributor integrity interval enables the existing macOS
worktree profiles for eligible native Ruff and declared pytest commands, within the caller's contributor-execution
controls. Use ``--sandbox-gate <gate>`` for each command requiring confinement, or ``--sandbox`` for every gate.
For an active protected interval, pass each parent-held baseline with ``--pr-integrity SNAPSHOT SHA256``. The runner
verifies every supplied anchor on entry and immediately after each executed gate. Drift stops the remaining attempt
before further Git inspections. A mixed confinement selection on an existing interval requires an anchor.
An unsupported workload requiring confinement fails before launch. Each gate records its actual route. A failed
sandboxed command is never repeated.
Use ``--project-env <absolute-venv>`` to expose tools from a separately provisioned project environment to commands
running in an isolated review worktree. It never creates an environment or installs a checker.
Use ``--tool-cache <absolute-directory>`` outside the checkout to give pre-commit and uv a writable cache when the
default cache under the user's home is read-only, as in a sandbox. It never seeds the cache or reaches the network.
Commands can come from the gate flags or matching environment variables such as ``LINT_CMD``. Each gate applies the
value supplied through ``--timeout-seconds`` separately, and every executable gate record retains that value.

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
requested gate, or unwritable artifact directory is retained as explicit gate evidence. Every gate record that did not
pass names a ``cause`` from a fixed set, so an environment limit such as a missing package or an unreachable network
is not read as a code failure. A source-bound run observes its checkout once before any gate; when that observation
refuses execution, ``gates.json`` carries one top-level ``refusal`` with its cause and reason, every gate that would
have run is a ``refused`` row listed in ``checks_refused`` (never in ``checks_failed``), the attempt's status is
``fail``, and no gate command runs.
Ignored files under the workflows' own artifact roots (``.reports``, ``.temp``) and inside directories that a
``pyvenv.cfg`` marks as virtual environments are environment or evidence, not source, so they never count as ignored
inputs. The CLI returns ``1`` for failed
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
import errno
import hashlib
import importlib
import io
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager, redirect_stdout
from functools import partial
from itertools import pairwise
from pathlib import Path
from typing import Any, Literal, NamedTuple

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
#: Top-level directories the review and remediation workflows write their own run evidence into. Their ignored
#: contents are workflow output (probe scripts, calibration fixtures), never project source a gate imports.
TOOL_ARTIFACT_ROOTS = (".reports", ".temp")
#: File that marks its directory as a Python virtual environment (PEP 405); its ignored contents are environment state.
VIRTUAL_ENVIRONMENT_MARKER = "pyvenv.cfg"
#: Desktop file-manager metadata that no Python tool reads; an untracked copy never makes the checkout dirty.
OS_METADATA_NAMES = frozenset({".DS_Store", "Thumbs.db"})
_MISSING_MODULE = re.compile(
    r"(?:ModuleNotFoundError|ImportError): No module named '([A-Za-z_][A-Za-z0-9_.]*)'"
    r"(?:; '[A-Za-z_][A-Za-z0-9_.]*' is not a package)?"
)
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_PYTEST_REPORT = re.compile(r"={3,} (.+?) ={3,}")
_PYTEST_CASE = re.compile(r"_{3,} .+ _{3,}")
_PYTEST_CAPTURE = re.compile(r"-{3,} Captured .+ -{3,}")
_TRACEBACK_TAIL = re.compile(
    r'^Traceback \(most recent call last\):\n(?:  File "[^\n]+", line [0-9]+[^\n]*\n)(?:[ \t][^\n]*\n)*([^\n]+)'
    r"|^E[ \t]+([^\n]+)(?:\nE[ \t]+[^\n]*)*",
    re.MULTILINE,
)
#: Causes a gate record can name when it did not pass; ``command-failed`` is an ordinary non-zero exit.
GATE_FAILURE_CAUSES = (
    "timeout",
    "missing-command",
    "missing-python-package",
    "hook-cache-unwritable",
    "network-unavailable",
    "loopback-bind-blocked",
    "import-origin-unproven",
    "source-refused",
    "sandbox-refused",
    "command-failed",
)
#: Reason every refused gate row carries; the attempt's single ``refusal`` record holds the diagnostic itself.
REFUSED_ROW_REASON = "source refused before execution; see refusal"
#: Causes of a source refusal recorded once per attempt in the top-level ``refusal`` record.
SOURCE_REFUSAL_CAUSES = (
    "source-ignored-inputs",
    "source-dirty",
    "source-head-mismatch",
    "source-inspection-failed",
    "source-inventory-changed",
)
#: Ordered evidence patterns for environment causes; the first match wins, so specific causes precede generic ones.
_FAILURE_PATTERNS = (
    (
        "hook-cache-unwritable",
        re.compile(
            r"Failed to initialize cache at|PermissionError: \[Errno 1\] Operation not permitted: '[^']*"
            r"(?:\.cache|pre-commit)|PermissionError: \[Errno 13\] Permission denied: '[^']*(?:\.cache|pre-commit)"
        ),
    ),
    (
        "network-unavailable",
        re.compile(
            r"Could not resolve host|Temporary failure in name resolution|nodename nor servname provided"
            r"|Name or service not known|Network is unreachable|Failed to establish a new connection"
        ),
    ),
    ("loopback-bind-blocked", re.compile(r"(?:bind|listen)\b.{0,80}(?:Operation not permitted|Permission denied)")),
)


_SandboxPolicy = Literal["standard", "contributor", "required"]


class PythonTestSpec(NamedTuple):
    """Describe one in-process pytest invocation with import origins to verify."""

    interpreter: Path
    modules: tuple[str, ...]
    arguments: tuple[str, ...]
    single_process: bool = False
    shard: str | None = None


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


def _verify_pr_integrity(anchors: Sequence[Sequence[str]], worktree: Path | None = None) -> None:
    """Authenticate every parent-held interval and stop before source reads or subsequent commands on drift."""
    if not anchors:
        return
    # The confined pytest child never needs the contributor verifier. Parent admission loads it before any workload.
    import contributor_run_integrity

    if worktree is not None:
        try:
            roots = {Path(str(contributor_run_integrity._load(path, digest)["repo"])) for path, digest in anchors}
        except contributor_run_integrity.IntegrityError as error:
            print(f"contributor-run-integrity: {error}", file=sys.stderr)
            print(contributor_run_integrity._SLOT_HOLD, file=sys.stderr)
            raise SystemExit(error.code) from error
        if worktree.resolve() not in roots:
            raise ValueError("--pr-integrity requires a snapshot for the actual execution checkout")
    for path, digest in anchors:
        # Successful verifier JSON is internal. Keep its exact failed observation and inspection advice for the parent.
        with redirect_stdout(io.StringIO()) as observation:
            status = contributor_run_integrity.main(["verify", "--snapshot", path, "--expect", digest])
        if status:
            sys.stdout.write(observation.getvalue())
            print(f"gate integrity stopped at {path}", file=sys.stderr)
            raise SystemExit(status)


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
    parser.add_argument(
        "--sandbox",
        action="store_true",
        help="Require a supported worktree sandbox for every executable gate. An unsupported workload fails before launch.",
    )
    parser.add_argument(
        "--sandbox-gate",
        action="append",
        choices=GATE_IDS,
        default=[],
        help="Require confinement for this executable gate. Repeat for other affected commands.",
    )
    parser.add_argument(
        "--pr-integrity",
        nargs=2,
        action="append",
        default=[],
        metavar=("SNAPSHOT", "SHA256"),
        help="Verify the active parent-held contributor integrity anchor around execution. Repeat for parallel drafts.",
    )
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
    parser.add_argument(
        "--tool-cache",
        type=Path,
        help="Absolute writable directory outside the checkout for the pre-commit and uv caches of gate commands.",
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
        "--shards",
        type=positive_integer,
        help="Split the disjoint pytest paths after -- in --pytest-args-json into N sequential, independently timed "
        "shards; options before -- carry attached values (--opt=value).",
    )
    parser.add_argument(
        "--single-process-pytest",
        action="store_true",
        help="Declare that this exact import-bound selection, config, plugins and tests support confinement without "
        "child processes or network. Used to select a compatible profile on protected or --sandbox runs.",
    )
    arguments = parser.parse_args()
    arguments.timeout_origin = _timeout_origin(arguments.timeout_seconds, os.environ)
    if arguments.timeout_seconds is None:
        configured = os.environ.get("GATE_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))
        try:
            arguments.timeout_seconds = positive_integer(configured)
        except (argparse.ArgumentTypeError, ValueError) as error:
            parser.error(f"argument --timeout-seconds: {error}")
    # The interval reader is parent-only. Inspect it before any Git-based source validation can run.
    from review_worktree_cleanup import has_integrity_interval

    arguments.existing_interval = has_integrity_interval(arguments.out.resolve())
    if arguments.sandbox_gate and not arguments.sandbox and arguments.existing_interval and not arguments.pr_integrity:
        parser.error("mixed gates in an active interval require the parent-held --pr-integrity SNAPSHOT SHA256")
    try:
        _verify_pr_integrity(arguments.pr_integrity, arguments.worktree or Path.cwd())
    except ValueError as error:
        parser.error(str(error))
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
    if arguments.tool_cache is not None:
        try:
            arguments.tool_cache = _tool_cache_directory(arguments.tool_cache, arguments.worktree or Path.cwd())
        except (OSError, ValueError) as error:
            parser.error(f"invalid-tool-cache: {error}")
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
    if arguments.shards is not None:
        if arguments.python_test is None:
            parser.error("--shards requires import-bound pytest with --pytest-python")
        try:
            arguments.shards = _pytest_shards(arguments.python_test.arguments, arguments.shards)
        except ValueError as error:
            parser.error(str(error))
    return arguments


def _pytest_shards(arguments: tuple[str, ...], count: int) -> list[tuple[str, ...]]:
    """Partition explicit selectors in input order; the child proves their effective selection."""
    if "--" not in arguments:
        raise ValueError("--shards requires explicit pytest paths after -- in --pytest-args-json")
    paths = arguments[arguments.index("--") + 1 :]
    if len(paths) < count:
        raise ValueError(f"--shards requires at least {count} pytest paths after --")
    if any(not path or path.startswith(("-", "@")) for path in paths):
        raise ValueError("--shards requires non-empty pytest paths after --, not options")
    size, extra = divmod(len(paths), count)
    boundaries = [index * size + min(index, extra) for index in range(count + 1)]
    return [paths[start:end] for start, end in pairwise(boundaries)]


def _timeout_origin(flag: int | None, environment: Mapping[str, str]) -> str:
    """Name where the per-gate timeout came from, so a short budget can be traced to its caller.

    Example:
        >>> _timeout_origin(900, {"GATE_TIMEOUT_SECONDS": "60"})
        'flag'
        >>> _timeout_origin(None, {"GATE_TIMEOUT_SECONDS": "900"})
        'environment'
        >>> _timeout_origin(None, {})
        'default'
    """
    if flag is not None:
        return "flag"
    if "GATE_TIMEOUT_SECONDS" in environment:
        return "environment"
    return "default"


def _tool_cache_directory(requested: Path, checkout: Path) -> Path:
    """Create the requested tool cache with its pre-commit and uv subdirectories, outside the checkout.

    A cache inside the checkout would place hook environments among the reviewed inputs, so it is refused.
    """
    if not requested.is_absolute():
        raise ValueError("expected an absolute directory")
    cache = requested.resolve()
    if cache.is_relative_to(checkout.resolve()):
        raise ValueError("the cache must be outside the checkout")
    for name in ("pre-commit", "uv"):
        (cache / name).mkdir(parents=True, exist_ok=True)
    probe = cache / f".write-probe-{secrets.token_hex(8)}"
    probe.write_bytes(b"")
    probe.unlink()
    return cache


def classify_gate_failure(check: dict[str, Any], output: str, stderr: str = "") -> str | None:
    """Return the cause for a gate record that did not pass, or ``None`` for a pass or a not-applicable gate.

    ``output`` and ``stderr`` are the gate's captured streams. Environment causes are recognised only from the
    tools' own diagnostics; anything else is an ordinary ``command-failed``.

    Example:
        >>> classify_gate_failure({"status": "pass"}, "") is None
        True
        >>> classify_gate_failure({"status": "timeout"}, "")
        'timeout'
        >>> classify_gate_failure({"status": "fail"}, "E   ModuleNotFoundError: No module named 'quoted'")
        'command-failed'
        >>> classify_gate_failure({"status": "fail"}, "error: Failed to initialize cache at `.cache/uv`")
        'hook-cache-unwritable'
        >>> classify_gate_failure({"status": "fail"}, "fatal: unable to access: Could not resolve host: github.com")
        'network-unavailable'
        >>> classify_gate_failure({"status": "fail"}, "1 failed, 20 passed")
        'command-failed'
    """
    status = check.get("status")
    if status in {"pass", "not-applicable"}:
        return None
    if status in {"timeout", "missing-command"}:
        return status
    route = check.get("execution_route")
    if isinstance(route, dict) and route.get("mode") == "not-started" and "sandbox required" in route.get("reason", ""):
        return "sandbox-refused"
    if check.get("missing_modules"):
        return "missing-python-package"
    if check.get("id") != "tests" and not check.get("id", "").startswith("tests-shard-"):
        if missing_modules(output) or missing_modules(stderr):
            return "missing-python-package"
        for cause, pattern in _FAILURE_PATTERNS:
            if pattern.search(f"{output}\n{stderr}"):
                return cause
    imports = check.get("python_imports")
    if isinstance(imports, dict) and imports.get("status") in {"fail", "inconclusive"} and check.get("exit_code") == 0:
        return "import-origin-unproven"
    return "command-failed"


def source_refusal_cause(error: str) -> str:
    """Map one source-guard diagnostic onto the refusal cause recorded once per attempt.

    Example:
        >>> source_refusal_cause("source-dirty:ignored-inputs:count=2:paths=['a.py', 'b.py']")
        'source-ignored-inputs'
        >>> source_refusal_cause("source-dirty:git status --porcelain=v1 was non-empty")
        'source-dirty'
        >>> source_refusal_cause("source-head-mismatch:expected a, observed b")
        'source-head-mismatch'
        >>> source_refusal_cause("tracked-source-inventory-changed")
        'source-inventory-changed'
    """
    if error.startswith("source-dirty:ignored-inputs"):
        return "source-ignored-inputs"
    if error.startswith("source-dirty"):
        return "source-dirty"
    if error.startswith("source-head-mismatch"):
        return "source-head-mismatch"
    if error.startswith("tracked-source-inventory"):
        return "source-inventory-changed"
    return "source-inspection-failed"


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


def _taskkill_tree(pid: int) -> subprocess.CompletedProcess[bytes]:
    """Run one bounded ``taskkill`` tree kill and refuse to claim cleanup when it cannot report."""
    try:
        return subprocess.run(  # noqa: S603 - argv list, no shell
            ["taskkill", "/PID", str(pid), "/T", "/F"],  # noqa: S607 - native system tool
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


def terminate_process(process: subprocess.Popen[str], platform: str) -> None:
    """Terminate a timed-out command and its descendants on the current platform."""
    if platform == "win32":
        completed = _taskkill_tree(process.pid)
        if completed.returncode == 255 and process.poll() is not None:
            # A tree member can exit between taskkill's process snapshot and its kill, which makes the pass report 255
            # although nothing it targeted survives. With the leader gone, a second pass over the finished tree proves
            # the cleanup when it ends the rest (0) or finds nothing left to end (128).
            retry = _taskkill_tree(process.pid)
            if retry.returncode in (0, 128):
                completed = subprocess.CompletedProcess(retry.args, 0, retry.stdout, retry.stderr)
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
        "PRE_COMMIT_HOME",
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
    pytest_report_dir: Path | None = None,
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
    if pytest_report_dir is not None:
        pytest_report_dir.mkdir(mode=0o700)
        child_environment["CODEX_RIG_PYTEST_REPORT_DIR"] = str(pytest_report_dir.resolve())
        child_environment["PYTEST_ADDOPTS"] = "-p run_gates " + child_environment["PYTEST_ADDOPTS"]
        child_environment["PYTHONPATH"] += os.pathsep + str(Path(__file__).resolve().parent)
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


def _node_digest(node_ids: Sequence[str]) -> str:
    """Bind the collected selection, including repeated IDs, without placing the list in argv."""
    return hashlib.sha256(json.dumps(sorted(node_ids)).encode()).hexdigest()


def _active_pytest_coverage(config: Any) -> bool:
    """Read activation from the configured pytest-cov plugin."""
    return config.pluginmanager.hasplugin("_cov") and not config.getoption("no_cov", default=False)


class _PytestEvidence:
    """Observe pytest's selected items, call reports and actual exceptions."""

    def __init__(self, shard: str | None) -> None:
        self.shard = shard
        self.node_ids: list[str] | None = None
        self.calls = 0
        self.missing: dict[str, None] = {}
        self.causes: set[str] = set()
        self.refusal: str | None = None

    def record_exception(self, exception: BaseException) -> None:
        """Use exception identity and structured fields for causes the runner can prove."""
        if isinstance(exception, ModuleNotFoundError) and exception.name:
            if PYTHON_MODULE_PATTERN.fullmatch(exception.name):
                self.missing[exception.name] = None
        if isinstance(exception, socket.gaierror) or (
            isinstance(exception, OSError) and exception.errno == errno.ENETUNREACH
        ):
            self.causes.add("network-unavailable")
        if isinstance(exception, PermissionError):
            traceback = exception.__traceback__
            while traceback is not None:
                frame = traceback.tb_frame
                if (
                    frame.f_globals.get("__name__") == "pytest_rerunfailures"
                    and frame.f_code.co_name == "__init__"
                    and type(frame.f_locals.get("self")).__name__ == "ServerStatusDB"
                ):
                    self.causes.add("loopback-bind-blocked")
                    break
                traceback = traceback.tb_next

    def pytest_internalerror(self, excinfo: Any) -> None:
        """Observe real tool exceptions, including failures while starting the session."""
        self.record_exception(excinfo.value)

    def pytest_runtest_logreport(self, report: Any) -> None:
        """Count calls from serial pytest or from workers reporting to their controller."""
        if report.when == "call":
            self.calls += 1

    def record_collection(self, session: Any) -> None:
        """Record node IDs using resolved paths so symlink aliases share an identity."""
        if self.shard is None:
            return
        self.node_ids = [
            f"{os.path.normcase(str(Path(item.path).resolve()))}::{item.nodeid.partition('::')[2]}"
            for item in session.items
        ]
        if self.shard is not None and _active_pytest_coverage(session.config):
            self.refusal = _SHARD_COVERAGE_REASON
        elif self.shard not in (None, "collect") and self.shard != _node_digest(self.node_ids):
            self.refusal = "--shards selection changed since pytest collection"

    def receipt(self, collected: int) -> dict[str, Any]:
        """Return the retained evidence, with the full selection only on admission probes."""
        return {
            "collected": collected,
            "calls": self.calls,
            "missing_modules": list(self.missing),
            "causes": sorted(self.causes),
            "refusal": self.refusal,
            **({"node_ids": self.node_ids} if self.shard == "collect" else {}),
        }


_SHARD_COVERAGE_REASON = "--shards cannot run coverage-enabled pytest; run one suite to apply coverage once"
_COLLECT_ONLY_REASON = "pytest collect-only cannot satisfy the tests gate"


def pytest_addoption(pluginmanager: Any) -> None:
    """Install an observer before pytest imports the initial conftests."""
    raw = os.environ.get("CODEX_RIG_PYTEST_SOURCE_PROOF")
    directory = os.environ.get("CODEX_RIG_PYTEST_REPORT_DIR")
    if raw is None and directory is None:
        return
    import pytest
    from _pytest.config import ConftestImportFailure

    class StartupEvidence:
        """Record failures that occur before pytest can produce a session receipt."""

        def publish(self, exception: BaseException) -> None:
            """Keep a fallback receipt for a startup or internal pytest failure."""
            config = pluginmanager.getplugin("pytestconfig")
            if hasattr(config, "workerinput"):
                return
            evidence = _PytestEvidence(None)
            evidence.record_exception(exception)
            if raw is not None:
                path = Path(json.loads(raw)["proof_path"]).with_suffix(".startup.json")
            else:
                path = Path(directory) / f"{os.getpid()}-{secrets.token_hex(8)}.json"
            path.write_text(json.dumps(evidence.receipt(0)), encoding="utf-8")

        @pytest.hookimpl(hookwrapper=True)
        def pytest_load_initial_conftests(self):
            """Preserve the typed cause of pytest's conftest loading error."""
            outcome = yield
            if outcome.excinfo is not None and isinstance(outcome.excinfo[1], ConftestImportFailure):
                self.publish(outcome.excinfo[1].cause)

        def pytest_internalerror(self, excinfo: Any) -> None:
            """Observe tool errors even when pytest never entered its session."""
            self.publish(excinfo.value)

    pluginmanager.register(StartupEvidence())


def _register_pytest_evidence(config: Any, shard: str | None, report_dir: str | None) -> _PytestEvidence:
    """Install effective-selection checks and transport diagnostics from pytest worker processes."""
    import pytest

    class Evidence(_PytestEvidence):
        """Bind observations to this pytest session."""

        @pytest.hookimpl(tryfirst=True)
        def pytest_collection_finish(self, session: Any) -> None:
            """Check the final selection before pytest can execute any selected item."""
            self.record_collection(session)
            if self.refusal and shard != "collect":
                pytest.exit(self.refusal, returncode=4)

        def pytest_exception_interact(self, call: Any) -> None:
            """Unwrap the typed error pytest uses to report an import failure during collection."""
            if call.excinfo is not None:
                exception = call.excinfo.value
                if isinstance(exception, pytest.Collector.CollectError) and exception.__cause__ is not None:
                    exception = exception.__cause__
                self.record_exception(exception)

        @pytest.hookimpl(optionalhook=True)
        def pytest_testnodedown(self, node: Any) -> None:
            """Merge typed failures reported by the executing worker."""
            data = (getattr(node, "workeroutput", None) or {}).get("codex_rig_pytest_evidence")
            if isinstance(data, dict):
                self.missing.update(dict.fromkeys(data["missing_modules"]))
                self.causes.update(data["causes"])
                self.refusal = self.refusal or data["refusal"]

        @pytest.hookimpl(tryfirst=True)
        def pytest_sessionfinish(self, session: Any) -> None:
            """Deliver evidence before the controller or a shell wrapper exits."""
            data = self.receipt(session.testscollected)
            if hasattr(config, "workeroutput"):
                config.workeroutput["codex_rig_pytest_evidence"] = data
            elif report_dir is not None:
                path = Path(report_dir) / f"{os.getpid()}-{secrets.token_hex(8)}.json"
                path.write_text(json.dumps(data), encoding="utf-8")

    evidence = Evidence(shard)
    config.pluginmanager.register(evidence, "codex-rig-pytest-evidence")
    if config.getoption("collectonly"):
        evidence.refusal = _COLLECT_ONLY_REASON
    elif shard is not None and _active_pytest_coverage(config):
        evidence.refusal = _SHARD_COVERAGE_REASON
    elif shard == "unavailable":
        evidence.refusal = "--shards could not admit this selection under the required sandbox"
    if shard == "collect" or evidence.refusal:
        config.option.collectonly = True
    return evidence


def pytest_configure(config: Any) -> None:
    """Attach source proof to pytest controllers and workers in opt-in gate mode."""
    raw = os.environ.get("CODEX_RIG_PYTEST_SOURCE_PROOF")
    if raw is None:
        if report_dir := os.environ.get("CODEX_RIG_PYTEST_REPORT_DIR"):
            _register_pytest_evidence(config, None, report_dir)
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
    evidence = _register_pytest_evidence(config, task.get("shard"), None)
    if task.get("shard") is not None and config.inipath and not config.inipath.resolve().is_relative_to(worktree):
        evidence.refusal = "--shards cannot use inherited pytest configuration outside the worktree"
        config.option.collectonly = True

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
                "pytest": evidence.receipt(session.testscollected),
                **({"local_source": local_source} if local_source is not None else {}),
            }
            Path(task["proof_path"]).write_text(json.dumps(proof, indent=2) + "\n", encoding="utf-8", newline="\n")

    config.pluginmanager.register(SourceProof(), "codex-rig-source-proof")


def run_python_tests_child(arguments: list[str]) -> int:
    """Run pytest and return its own exit code when the child produced proof for the parent to validate."""
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
    try:
        import pytest
    except ModuleNotFoundError as error:
        evidence = _PytestEvidence(None)
        evidence.record_exception(error)
        proof_path.with_suffix(".startup.json").write_text(json.dumps(evidence.receipt(0)), encoding="utf-8")
        raise

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
        elif "snapshot_path" in binding:
            task["local_source"] = {key: binding[key] for key in ("snapshot_path", "snapshot_sha256")}
        if "shard" in binding:
            task["shard"] = binding["shard"]
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
    # The parent fails the gate on bad proof while retaining pytest's outcome for failure classification.
    return test_exit_code


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


def _source_status(porcelain: str) -> str:
    """Drop untracked desktop metadata files from ``git status --porcelain=v1`` output; every other line remains.

    Example:
        >>> _source_status("?? .DS_Store\\n?? docs/Thumbs.db\\n M module.py")
        ' M module.py'
        >>> _source_status("?? stray.py")
        '?? stray.py'
    """
    return "\n".join(
        line
        for line in porcelain.split("\n")
        if not (line.startswith("?? ") and line[3:].rpartition("/")[2] in OS_METADATA_NAMES)
    )


def _traceback_report_lines(output: str) -> Iterator[str]:
    """Yield traceback evidence outside pytest's captured output and summaries."""
    in_failure = captured = in_summary = False
    for line in _ANSI_ESCAPE.sub("", output).splitlines():
        if report := _PYTEST_REPORT.fullmatch(line.rstrip()):
            in_failure = report[1] in {"FAILURES", "ERRORS"}
            in_summary = not in_failure
            captured = False
        elif line.startswith("ImportError while loading conftest "):
            in_failure = True
        elif in_failure and _PYTEST_CASE.fullmatch(line):
            captured = False
        elif in_failure and _PYTEST_CAPTURE.fullmatch(line):
            captured = True
        elif captured or in_summary or (line.startswith("E ") and not in_failure):
            yield ""
            continue
        yield line


def missing_modules(output: str) -> list[str]:
    """Return the module names a captured import failure reports missing, in first-seen order.

    Example:
        >>> missing_modules("=== ERRORS ===\\nE   ModuleNotFoundError: No module named 'cv2'")
        ['cv2']
    """
    report = "\n".join(_traceback_report_lines(output))
    modules = {}
    # Pytest marks every continuation of a multiline exception with E as well. Only the first line names its type.
    for native, pytest_error in _TRACEBACK_TAIL.findall(report):
        if match := _MISSING_MODULE.fullmatch((native or pytest_error).strip()):
            modules[match[1]] = None
    return list(modules)


def _first_party_submodule(module: str, root: Path) -> bool:
    """Return whether ``module`` is a submodule of a package the checkout itself provides, a source failure."""
    top, dot, _ = module.partition(".")
    if not dot:
        return False
    return any((base / top).is_dir() or (base / f"{top}.py").is_file() for base in (root, root / "src"))


def _reachable_ignored_paths(paths: Sequence[str]) -> tuple[list[str], list[str]]:
    """Split Git's ignored-file list into paths a gate can read as source and the roots excluded as non-source.

    Excluded are the workflows' own top-level artifact roots and every directory holding a ``pyvenv.cfg``, which
    marks a virtual environment as the existing ``.venv`` exclusion already treats it. pytest itself skips such
    directories during collection. A marker at the checkout root excludes nothing. Git reports POSIX separators.

    Example:
        >>> _reachable_ignored_paths(
        ...     [".reports/codex/run/probe.py", "exp/env/pyvenv.cfg", "exp/env/lib/site.py", "runtime/module.py"]
        ... )
        (['runtime/module.py'], ['.reports', 'exp/env'])
        >>> _reachable_ignored_paths(["pyvenv.cfg", "module.py"])
        (['pyvenv.cfg', 'module.py'], [])
    """
    roots = {root for root in TOOL_ARTIFACT_ROOTS if any(path.startswith(f"{root}/") for path in paths)}
    roots.update(
        path.rpartition("/")[0]
        for path in paths
        if path.rpartition("/")[2] == VIRTUAL_ENVIRONMENT_MARKER and "/" in path
    )
    prefixes = tuple(f"{root}/" for root in roots)
    return [path for path in paths if not path.startswith(prefixes)], sorted(roots)


def _ignored_source_inventory(worktree: Path | None) -> str:
    """Record ignored source paths outside tool caches, artifact roots and environments, apart from Git status."""
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
    reachable, _ = _reachable_ignored_paths([os.fsdecode(path) for path in ignored.stdout.split(b"\0") if path])
    return json.dumps(reachable)


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
        snapshot["status"] = _source_status(os.fsdecode(status.stdout).rstrip("\r\n"))
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
        if Path(path).suffix in {".py", ".pth", ".ini"} or Path(path).name in _PYTEST_CONFIG_FILES
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
_PYTEST_CONFIG_FILES = (
    "pytest.toml",
    ".pytest.toml",
    "pytest.ini",
    ".pytest.ini",
    "pyproject.toml",
    "tox.ini",
    "setup.cfg",
)


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


def _pytest_target_config_reason(worktree: Path, options: Sequence[str]) -> str | None:
    """Detect positional targets that could give pytest a different config discovery root."""
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
                if any(os.path.lexists(directory / name) for name in _PYTEST_CONFIG_FILES):
                    return "pytest target may select nested configuration"
                checked.add(directory)
                directory = directory.parent
        except (OSError, ValueError):
            return "pytest target configuration cannot be verified"
    return None


def _read_pytest_addopts(config: Path) -> tuple[bool, list[str]]:
    """Read a config candidate and report whether its format establishes pytest discovery precedence."""
    content = _read_gate_file(config, "pytest-configuration", 1024 * 1024).decode("utf-8")
    if config.suffix == ".toml":
        if tomllib is None:
            raise ValueError("TOML parser unavailable to verify pytest configuration")
        document = tomllib.loads(content)
        if config.name == "pyproject.toml":
            settings = document.get("tool", {}).get("pytest", {})
            # Older pytest accepts only the INI compatibility table as a config source.
            owns_config = "ini_options" in settings
            settings = settings.get("ini_options", settings)
        else:
            settings = document.get("pytest", {})
            owns_config = False
    else:
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(content)
        section = "tool:pytest" if config.name == "setup.cfg" else "pytest"
        owns_config = section in parser or config.name in ("pytest.ini", ".pytest.ini")
        settings = parser[section] if section in parser else {}
    addopts = settings.get("addopts", [])
    options = shlex.split(addopts) if isinstance(addopts, str) else addopts
    if not isinstance(options, list) or any(not isinstance(option, str) for option in options):
        raise ValueError("pytest addopts must contain strings")
    return owns_config, options


def _pytest_config_addopts(worktree: Path) -> list[list[str]]:
    """Collect every root config candidate's addopts without assuming the child uses this runner's pytest version."""
    owns_config = False
    candidates = []
    for name in _PYTEST_CONFIG_FILES:
        config = worktree / name
        if os.path.lexists(config):
            try:
                owns, options = _read_pytest_addopts(config)
            except (configparser.Error, AttributeError, OSError, TypeError, ValueError) as error:
                raise ValueError("pytest configuration cannot be verified") from error
            owns_config |= owns
            candidates.append(options)
    if not owns_config and any(
        os.path.lexists(path / name) for path in worktree.parents for name in _PYTEST_CONFIG_FILES
    ):
        raise ValueError("pytest may use inherited configuration outside the worktree")
    return candidates


def _pytest_configuration_reason(worktree: Path, options: list[str]) -> str | None:
    """Keep the original pytest route if hiding enclosing config or starting workers could alter collection."""
    if any((directory / "conftest.py").is_file() for directory in worktree.parents):
        return "pytest may load a conftest outside the worktree"
    if any(option.startswith(_PYTEST_LOCATION_OPTIONS) for option in options):
        return _PYTEST_LOCATION_REASON
    # Pytest expands an addopts override before normal option parsing. Its value can itself
    # introduce configuration, plugins or workers. An empty override introduces no arguments.
    if any(option.partition("addopts=")[2] for option in options if option.startswith(("-", "addopts="))):
        return "pytest addopts override needs the standard route"
    if reason := _pytest_target_config_reason(worktree, options):
        return reason
    try:
        configured = _pytest_config_addopts(worktree)
    except ValueError as error:
        return str(error)
    # Configured addopts are expanded into the same command line, so they need the command-line checks too.
    return _effective_pytest_option_reason([*options, *(option for candidate in configured for option in candidate)])


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
    *,
    sandbox_policy: _SandboxPolicy = "standard",
) -> tuple[int, float, dict[str, str]]:
    """Select the route before execution; a refused or failed launch has no replay path."""
    with ExitStack() as stack:
        try:
            prepared = "worktree sandbox not requested"
            if sandbox_policy != "standard":
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
            if sandbox_policy == "required":
                paths["stdout"].write_bytes(b"")
                reason = f"sandbox required: {prepared}"
                paths["stderr"].write_text(reason + "\n", encoding="utf-8")
                return 1, 0.0, {"mode": "not-started", "reason": reason}
            outcome = execute_command(
                command,
                timeout,
                paths["stdout"],
                paths["stderr"],
                worktree,
                environment,
                out_dir,
                scratch,
                **(
                    {"pytest_report_dir": paths["stdout"].with_suffix(".pytest")}
                    if gate_id == "tests" and isinstance(command, str)
                    else {}
                ),
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
    pr_integrity: Sequence[Sequence[str]] = (),
    sandbox_policy: _SandboxPolicy = "standard",
    refused: tuple[dict[str, str], str] | None = None,
    artifact_id: str | None = None,
) -> dict[str, Any]:
    """Run one gate and bind local execution artifacts to the observed source and child proof.

    ``refused`` carries the attempt's single refusing source observation; the gate then records that observation without
    inspecting the checkout again and without running its command. A shard keeps the canonical gate identity for routing
    and receives a distinct artifact name.
    """
    run_root = run_root or out_dir
    # The confined child needs its scratch beside the tracked input. Use their real private coordinates
    # throughout execution and replay; only the validated receipt can publish the digest-named input.
    input_checks = (out_dir if expected_head is not None else run_root) / CHECKS_DIRNAME
    paths, recorded = check_paths(artifact_id or gate_id, out_dir)
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
    proof_path = out_dir / CHECKS_DIRNAME / f"{artifact_id or 'tests'}.python-imports.json"
    scratch = None
    scratch_identity = None
    checks_identity = None
    child_proof = proof_path
    if gate_id == "tests" and python_test is not None:
        if worktree is None:
            raise RuntimeError("worktree must not be None")
        checks_identity = proof_path.parent.lstat()
        scratch = input_checks.resolve() / f"pytest-scratch-{secrets.token_hex(16)}"
        child_proof = scratch / "tests.python-imports.json"
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
            child_binding = {
                "snapshot_path": (out_dir / local_source["snapshot_path"]).resolve().as_posix(),
                "snapshot_sha256": local_source["snapshot_sha256"],
            }
        else:
            child_binding = {}
        if python_test.shard is not None:
            child_binding["shard"] = python_test.shard
        if child_binding:
            python_test_argv.append(json.dumps(child_binding))
        command = f"argv-json:{json.dumps(python_test_argv)}"
    command_bytes = f"{command}\n".encode()
    paths["command"].write_bytes(command_bytes)
    source: dict[str, Any] | None = None
    tracked_before = None
    if expected_head is not None:
        if refused is not None:
            before, error = refused
        elif checkout is None:
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
        child_binding["tracked_inventory"] = binding
        python_test_argv[9:] = [json.dumps(child_binding)]
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
        sandbox_policy=sandbox_policy,
    )
    if route["mode"] != "not-started":
        _verify_pr_integrity(pr_integrity)
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
        if python_test.shard != "collect":
            _apply_pytest_evidence(result, [_child_pytest_evidence(proof, child_proof)], paths["stderr"])
        if proof.get("status") != "pass":
            append_stderr(paths["stderr"], f"python-import-origin:{proof.get('status', 'invalid')}")
            if result["status"] == "pass":
                result["status"] = "fail"
    elif gate_id == "tests" and route["mode"] != "not-started":
        _apply_pytest_evidence(result, _read_pytest_evidence(paths["stdout"]), paths["stderr"])
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
            source["artifacts"][proof_path.relative_to(out_dir).as_posix()] = hashlib.sha256(proof_bytes).hexdigest()
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
_SHARD_STREAMS = {"command_path": "command.txt", "stdout": "stdout.txt", "stderr": "stderr.txt"}
_SHARD_PROOF = "python-imports.json"
_SHARD_MEMBER = re.compile(
    r"checks/tests-shard-[0-9]{3,}\.(?:command\.txt|stdout\.txt|stderr\.txt|python-imports\.json)"
)
_SHARD_STATUS_ORDER = {"pass": 0, "fail": 1, "missing-command": 1, "timeout": 2}
#: Prior gate statuses a retry may declare out of scope. A gate that failed, timed out, lacked its command or was
#: refused before it ran never established its result, so a retry must run it again rather than skip it.
_SKIPPABLE_PRIOR_STATUSES = frozenset({"pass", "not-applicable"})


def _shard_members(gates: dict[str, Any]) -> set[str]:
    """Admit shard artifacts when the tests receipt agrees with the shared shard ordering."""
    tests = next(check for check in gates["checks"] if check["id"] == "tests")
    if "shards" not in tests:
        return set()
    shards = tests["shards"]
    if not isinstance(shards, list) or not shards:
        raise ValueError("invalid-pytest-shards")
    members = set()
    statuses = set()
    for number, shard in enumerate(shards, 1):
        shard_id = f"tests-shard-{number:03d}"
        if not isinstance(shard, dict) or shard.get("id") != shard_id:
            raise ValueError("invalid-pytest-shard-id")
        status = shard.get("status")
        paths = shard.get("selected_paths")
        if (
            not isinstance(status, str)
            or status not in _SHARD_STATUS_ORDER
            or (status != "pass" and shard.get("cause") not in GATE_FAILURE_CAUSES)
            or not isinstance(paths, list)
            or not paths
            or not all(isinstance(path, str) and path for path in paths)
            or not isinstance(shard.get("exit_code"), int)
            or not isinstance(shard.get("duration_seconds"), (int, float))
        ):
            raise ValueError("invalid-pytest-shard-result")
        statuses.add(status)
        for key, suffix in _SHARD_STREAMS.items():
            relative = f"checks/{shard_id}.{suffix}"
            if shard.get(key) != relative:
                raise ValueError("invalid-pytest-shard-log")
            members.add(relative)
        if "python_imports" in shard:
            members.add(f"checks/{shard_id}.{_SHARD_PROOF}")
    worst = _representative_shard(shards)["status"]
    if tests["status"] != worst and not (tests["status"] == "refused" and statuses == {"fail"}):
        raise ValueError("invalid-pytest-shard-aggregate")
    return members


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
    shard_members = _shard_members(previous)
    if {name for name, state in plan["before"].items() if state is not None and _SHARD_MEMBER.fullmatch(name)} != (
        shard_members
    ):
        raise ValueError("gate-publication-archive-changed")
    logs = _receipt_logs(archived, previous, output)
    if set(plan["legacy_logs"]) != logs.difference(_GATE_REPORTS):
        raise ValueError("gate-publication-archive-changed")
    for name, original in plan["before"].items():
        # New immutable inputs might already exist publicly, but did not belong to the prior attempt.
        if name not in _GATE_REPORTS and name not in plan["damaged"] and name not in shard_members:
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
    report_members = set(_GATE_REPORTS) | {name for name in after if _SHARD_MEMBER.fullmatch(name)}
    if plan["archive"] is None and any(before.get(name) is not None for name in report_members):
        raise ValueError("gate-publication-invalid-plan")
    if (output / CHECKS_DIRNAME).is_symlink() or (stage / CHECKS_DIRNAME).is_symlink():
        raise ValueError("gate-checks-directory-redirected")
    candidate = _validate_staged_gates(stage, output)
    candidate_shards = _shard_members(candidate)
    if {name for name, digest in after.items() if digest is not None and _SHARD_MEMBER.fullmatch(name)} != (
        candidate_shards
    ):
        raise ValueError("gate-publication-unowned-shard")
    inventories = _receipt_inventories(candidate)
    # Journals can outlive their writer. Recompute every inventory installation or authorized retirement
    # from the receipts, including when an older journal previously admitted a name found in scratch.
    expected_inputs = {**dict.fromkeys(plan["damaged"]), **inventories}
    if {key: value for key, value in after.items() if key not in report_members} != expected_inputs or (
        plan["damaged"] and plan["archive"] is None
    ):
        raise ValueError("gate-publication-unowned-inventory")
    for name in after:
        if name not in report_members and not re.fullmatch(r"checks/tracked-source-[0-9a-f]{64}\.bin", name):
            raise ValueError("gate-publication-invalid-member")
        prior = before[name]
        if prior is not None and (
            not isinstance(prior, list)
            or len(prior) != 5
            or not all(isinstance(number, int) for number in prior[:4])
            or not re.fullmatch("[0-9a-f]{64}", str(prior[-1]))
        ):
            raise ValueError("gate-publication-invalid-member")
        state = (
            _file_state(stage / name)
            if name in _GATE_REPORTS or name in inventories or name in candidate_shards
            else None
        )
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
) -> tuple[dict[str, str | None], dict[str, str], dict[str, str], set[str]]:
    """Archive existing gate evidence before rerun, refusing failed-to-skipped recovery.

    Re-executing a failed check can establish success; changing its applicability cannot. A pinned-source retry can
    retain a receipt-bound damaged capture as diagnostic evidence. Other unreadable state stops publication.
    """
    manifest = output / "gates.json"
    if not manifest.exists() and not manifest.is_symlink():
        if any((output / name).exists() or (output / name).is_symlink() for name in _GATE_REPORTS):
            raise ValueError("invalid-previous-gates")
        return {}, {}, {}, set()
    prior = json.loads(_read_gate_file(manifest, "gate-previous-manifest"))
    checks = prior.get("checks") if isinstance(prior, dict) else None
    if not isinstance(checks, list) or len(checks) != len(GATE_IDS):
        raise ValueError("invalid-previous-gates")
    if any(not isinstance(check, dict) or not isinstance(check.get("id"), str) for check in checks):
        raise ValueError("invalid-previous-gates")
    if {check["id"] for check in checks} != set(GATE_IDS):
        raise ValueError("invalid-previous-gates")
    # Every status the published reader accepts, including a refused gate that never ran, is a complete prior receipt.
    statuses = importlib.import_module("validate-artifacts").VALID_GATE_STATUSES
    for check in checks:
        status = check.get("status")
        if not isinstance(status, str) or status not in statuses:
            raise ValueError("invalid-previous-gates")
        if skip_reasons[check["id"]] and check["status"] not in _SKIPPABLE_PRIOR_STATUSES:
            raise ValueError(f"cannot-skip-failed-gate:{check['id']}; rerun its command or diagnose a new scoped run")

    # Never follow an inventory redirect. Record damaged regular inputs alongside the original archived
    # receipts so the next run can regenerate its own index without repairing the historical evidence.
    checks_dir = output / CHECKS_DIRNAME
    if checks_dir.is_symlink():
        raise ValueError("gate-checks-directory-redirected")
    logs = _receipt_logs(output, prior, output, archive)
    shard_members = _shard_members(prior)
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
    for relative in sorted(shard_members):
        (archive / relative).write_bytes(_read_gate_file(output / relative, "prior-pytest-shard"))
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
    return damaged, damaged_logs, legacy_logs, shard_members


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


def _source_refusal(
    check: dict[str, Any], expected_head: str, checkout: _StructuredCheckout | None
) -> tuple[dict[str, str], str] | None:
    """Return the source observation and diagnostic when this gate was refused before its command could launch.

    A refused gate has no execution route and no after-observation. Other pre-launch failures, such as unusable scratch
    space, leave the source guard satisfied and are not refusals.
    """
    source = check.get("source")
    if (
        check["status"] != "fail"
        or "execution_route" in check
        or not isinstance(source, dict)
        or source.get("after") is not None
        or not isinstance(source.get("before"), dict)
    ):
        return None
    if checkout is not None and checkout.rejection is not None:
        return checkout.rejection
    error = source_guard_error(source["before"], expected_head)
    return (source["before"], error) if error else None


def _read_pytest_evidence(stdout: Path) -> list[dict[str, Any] | None]:
    """Read the typed observations of pytest sessions started by shell tests commands."""
    directory = stdout.with_suffix(".pytest")
    if not directory.exists():
        return []
    if directory.is_symlink():
        return [None]
    evidence = []
    for path in sorted(directory.glob("*.json")):
        try:
            evidence.append(json.loads(_read_gate_file(path, "pytest-evidence", 1024 * 1024)))
        except (OSError, ValueError):
            evidence.append(None)
    return evidence


def _child_pytest_evidence(proof: dict[str, Any], child_proof: Path) -> Any:
    """Recover a typed startup failure when pytest never created a normal session receipt."""
    if "pytest" in proof:
        return proof["pytest"]
    startup = child_proof.with_suffix(".startup.json")
    try:
        return json.loads(_read_gate_file(startup, "pytest-evidence", 1024 * 1024))
    except (OSError, ValueError):
        return None


def _pytest_call_evidence(evidence: Any) -> bool:
    """Require a nonempty collection and at least one call-phase report."""
    return (
        isinstance(evidence, dict)
        and type(evidence.get("collected")) is int
        and evidence["collected"] > 0
        and type(evidence.get("calls")) is int
        and evidence["calls"] > 0
    )


def _apply_pytest_evidence(result: dict[str, Any], observations: list[Any], stderr: Path) -> None:
    """Enforce observed pytest execution and retain causes backed by typed exceptions."""
    for observation in observations:
        evidence = observation if isinstance(observation, dict) else {}
        modules = evidence.get("missing_modules", [])
        if (
            isinstance(modules, list)
            and all(isinstance(name, str) and PYTHON_MODULE_PATTERN.fullmatch(name) for name in modules)
            and modules
        ):
            result["missing_modules"] = list(dict.fromkeys([*result.get("missing_modules", ()), *modules]))
        if isinstance(evidence.get("causes"), list):
            for cause in ("loopback-bind-blocked", "network-unavailable"):
                if cause in evidence["causes"]:
                    result.setdefault("cause", cause)
        refusal = evidence.get("refusal")
        if not isinstance(refusal, str) and result["status"] == "pass" and not _pytest_call_evidence(evidence):
            refusal = "pytest execution unproven: expected selected tests and call-phase reports"
        if isinstance(refusal, str):
            append_stderr(stderr, refusal)
            if result["status"] == "pass":
                result.update(status="fail", exit_code=1)
        if result["status"] == "pass":
            result.pop("missing_modules", None)
            result.pop("cause", None)


def _annotate_checks(checks: list[dict[str, Any]], stage: Path, timeout: int, root: Path) -> None:
    """Record each executable gate's timeout and, for a gate that did not pass, the cause of that outcome.

    The records are annotated before any summary is written, so ``gates.json`` and ``gates.checks.jsonl`` agree. A
    refused gate never ran, so it records only its cause.
    """
    for check in checks:
        if check["status"] == "not-applicable":
            continue
        if check["status"] == "refused":
            check["cause"] = "source-refused"
            continue
        if "shards" in check:  # The aggregate already carries the diagnosis derived from all of its shards.
            continue
        check["timeout_seconds"] = timeout
        if check["status"] == "pass":
            continue
        stdout, stderr = (
            _read_gate_file(path, "gate-log").decode("utf-8", errors="replace") if path.is_file() else ""
            for stream in ("stdout", "stderr")
            for path in (stage / check[stream],)
        )
        structured_cause = check.get("cause")
        check["cause"] = classify_gate_failure(check, stdout, stderr)
        if check["cause"] in {"command-failed", "import-origin-unproven"} and structured_cause:
            check["cause"] = structured_cause
        if check["cause"] == "missing-python-package":
            if "missing_modules" not in check:
                check["missing_modules"] = list(dict.fromkeys([*missing_modules(stdout), *missing_modules(stderr)]))
            # A removed submodule of the project's own package is a source failure, not a missing dependency.
            if any(_first_party_submodule(name, root) for name in check["missing_modules"]):
                check["cause"] = "command-failed"


def _representative_shard(shards: list[dict[str, Any]]) -> dict[str, Any]:
    """Pick the shard that determines the canonical tests result for publication and validation.

    Preserve the worst status rank. At the same rank, prefer a failed test command over host or source proof failures.
    Equal candidates keep the first shard in selection order.
    """
    return max(
        shards,
        key=lambda shard: (
            _SHARD_STATUS_ORDER[shard["status"]],
            shard.get("cause") == "command-failed" and shard.get("exit_code") != 0,
        ),
    )


def _shard_missing_modules(shards: list[dict[str, Any]]) -> list[str]:
    """Return every module any failing shard reported missing, in shard and first-seen order."""
    modules: dict[str, None] = {}
    for shard in shards:
        if shard["status"] != "pass":
            modules.update(dict.fromkeys(shard.get("missing_modules", ())))
    return list(modules)


def _publish_shard_artifact(stage: Path, canonical: str, original: str, supplemental: list[str]) -> str:
    """Write and hash one canonical artifact, including labeled additional failure logs when supplied."""
    digest = hashlib.sha256()
    with (stage / canonical).open("xb") as stream:
        for name in (original, *supplemental):
            if name != original:
                header = f"\n\n--- {name} (command-failed) ---\n".encode()
                stream.write(header)
                digest.update(header)
            content = _read_gate_file(stage / name, "pytest-shard-artifact")
            stream.write(content)
            digest.update(content)
    return digest.hexdigest()


def _collect_shard_selections(
    run: Callable[..., dict[str, Any]],
    spec: PythonTestSpec,
    partitions: list[tuple[str, ...]],
    checkout: _StructuredCheckout | None,
) -> list[str | None]:
    """Probe every partition under the gate's execution policy before admitting any test calls."""
    options = spec.arguments[: spec.arguments.index("--") + 1]
    seen: set[str] = set()
    selections: list[str | None] = []
    for number, paths in enumerate(partitions, 1):
        probe = spec._replace(arguments=(*options, *paths), shard="collect")
        check = run(python_test=probe, artifact_id=f"pytest-selection-{number:03d}")
        if checkout is not None and checkout.rejection is not None:
            return [None] * len(partitions)
        if check.get("execution_route", {}).get("mode") == "not-started":
            # Preserve each shard's refusal receipt, even if a subsequent launcher can initialize.
            return ["unavailable"] * len(partitions)
        evidence = check.get("python_imports", {}).get("pytest", {})
        if evidence.get("refusal"):
            raise ValueError(evidence["refusal"])
        node_ids = evidence.get("node_ids")
        if (
            check.get("exit_code") not in {0, 2, 5}
            or evidence.get("calls") != 0
            or not isinstance(node_ids, list)
            or any(not isinstance(node, str) for node in node_ids)
        ):
            raise ValueError(
                f"--shards could not verify pytest collection for shard {number} (exit {check['exit_code']})"
            )
        nodes = set(node_ids)
        overlap = seen & nodes
        if len(nodes) != len(node_ids) or overlap:
            example = min(overlap or nodes)
            raise ValueError(f"--shards collected overlapping tests in shard {number}: {example}")
        seen.update(nodes)
        selections.append(_node_digest(node_ids))
    return selections


def _run_pytest_shards(
    run: Callable[..., dict[str, Any]],
    arguments: argparse.Namespace,
    stage: Path,
    checkout: _StructuredCheckout | None,
) -> dict[str, Any]:
    """Run every partition through the canonical gate and summarize it by its representative shard.

    The aggregate takes its status and cause from the shard ``_representative_shard`` picks and lists all missing
    modules. Its logs include other command failures. Gap and provisioning decisions use the individual receipts.
    """
    spec = arguments.python_test
    options = spec.arguments[: spec.arguments.index("--") + 1]
    selections = _collect_shard_selections(run, spec, arguments.shards, checkout)
    shards = []
    shard_hashes = {}
    for number, (paths, selection_digest) in enumerate(zip(arguments.shards, selections), 1):
        shard_id = f"tests-shard-{number:03d}"
        files = [f"checks/{shard_id}.{suffix}" for suffix in (*_SHARD_STREAMS.values(), _SHARD_PROOF)]
        if any(os.path.lexists(stage / name) for name in files):
            raise ValueError(f"pytest-shard-artifact-already-exists:{shard_id}")
        selection = spec._replace(arguments=(*options, *paths), shard=selection_digest)
        shard = run(python_test=selection, artifact_id=shard_id)
        shard.update(id=shard_id, selected_paths=list(paths))
        _annotate_checks([shard], stage, arguments.timeout_seconds, arguments.worktree)
        if arguments.expected_head is not None and _source_refusal(shard, arguments.expected_head, checkout):
            shard["cause"] = "source-refused"
        artifacts = (shard.get("source") or {}).get("artifacts", {})
        shard_hashes.update(artifacts)
        for name in files[: len(_SHARD_STREAMS)]:
            if name not in artifacts:
                shard_hashes[name] = hashlib.sha256(_read_gate_file(stage / name, "pytest-shard-log")).hexdigest()
        shards.append(shard)

    representative = _representative_shard(shards)
    aggregate = {
        **representative,
        "id": "tests",
        "duration_seconds": sum(shard["duration_seconds"] for shard in shards),
        "shards": shards,
    }
    del aggregate["selected_paths"]
    modules = _shard_missing_modules(shards)
    if modules:
        aggregate["missing_modules"] = modules
    # Bind the canonical files and each shard's original evidence to the same receipt.
    source = aggregate.get("source")
    if isinstance(source, dict):
        aggregate["source"] = {**source, "artifacts": shard_hashes}
    members = dict(_SHARD_STREAMS)
    if "python_imports" in representative:
        members["python_imports"] = _SHARD_PROOF
    for key, suffix in members.items():
        original = f"checks/{representative['id']}.{suffix}"
        canonical = f"checks/tests.{suffix}"
        supplemental = [
            shard[key]
            for shard in shards
            if key in {"stdout", "stderr"} and shard is not representative and shard.get("cause") == "command-failed"
        ]
        digest = _publish_shard_artifact(stage, canonical, original, supplemental)
        if key != "python_imports":
            aggregate[key] = canonical
        if original in shard_hashes:
            shard_hashes[canonical] = digest
    return aggregate


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
    if arguments.tool_cache is not None:
        environment["PRE_COMMIT_HOME"] = str(arguments.tool_cache / "pre-commit")
        environment["UV_CACHE_DIR"] = str(arguments.tool_cache / "uv")
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
    damaged, damaged_logs, legacy_logs, previous_shards = preserve_previous_attempt(
        output, skip_reasons, archive, recovery_source
    )
    archive_identity = archive.lstat() if archive.exists() else None
    history = output / ("gate-diagnostics" if damaged_logs or damaged else "gate-attempts")
    if history.is_symlink() or history.resolve() != output / history.name:
        raise ValueError("gate-attempts-directory-redirected")
    if archive_identity is not None and history.exists() and not history.is_dir():
        raise ValueError(f"gate-history-root-occupied:{history.name}")
    checks_dir.mkdir(exist_ok=True)
    (stage / CHECKS_DIRNAME).mkdir()
    approval_name = "checks/tests-skip-approval.json"
    names = set(_GATE_REPORTS) | set(damaged) | previous_shards
    if arguments.shards is not None:
        # New names must be vacant before executing the candidate, including the optional proof sidecars.
        for number in range(1, len(arguments.shards) + 1):
            for suffix in (*_SHARD_STREAMS.values(), _SHARD_PROOF):
                relative = f"checks/tests-shard-{number:03d}.{suffix}"
                if relative not in previous_shards and os.path.lexists(output / relative):
                    raise ValueError(f"gate-publication-unowned-shard:{relative}")
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
    required_gates = set(GATE_IDS if arguments.sandbox else arguments.sandbox_gate)
    default_policy: _SandboxPolicy = (
        "contributor" if arguments.existing_interval or arguments.pr_integrity else "standard"
    )
    # A refusal observed before any gate launched decides the whole attempt; later gates reuse it uninspected.
    refusal: tuple[dict[str, str], str] | None = None
    checks: list[dict[str, Any]] = []
    for gate_id in GATE_IDS:
        run = partial(
            run_check,
            gate_id,
            commands[gate_id],
            skip_reasons[gate_id],
            arguments.timeout_seconds,
            stage,
            arguments.expected_head,
            arguments.worktree,
            environment=environment,
            local_source=local_source,
            run_root=output,
            checkout=checkout,
            pr_integrity=arguments.pr_integrity,
            sandbox_policy="required" if gate_id in required_gates else default_policy,
            refused=refusal,
        )
        if gate_id == "tests" and arguments.shards is not None:
            check = _run_pytest_shards(run, arguments, stage, checkout)
        else:
            check = run(python_test=arguments.python_test)
        shard_started = any("execution_route" in shard for shard in check.get("shards", ()))
        if (
            refusal is None
            and arguments.expected_head is not None
            and not shard_started
            and not any("execution_route" in prior for prior in checks)
        ):
            refusal = _source_refusal(check, arguments.expected_head, checkout)
        if refusal is not None and check["status"] == "fail" and "execution_route" not in check:
            # The command never launched: report the gate as refused, not as one more failure.
            check.update(status="refused", exit_code=0, reason=REFUSED_ROW_REASON)
        checks.append(check)
    _annotate_checks(checks, stage, arguments.timeout_seconds, (arguments.worktree or Path.cwd()).resolve())
    failed = [check["id"] for check in checks if check["status"] in {"fail", "missing-command", "timeout"}]
    refused_ids = [check["id"] for check in checks if check["status"] == "refused"]
    if approval is not None:
        next(check for check in checks if check["id"] == "tests")["approval"] = approval
    status = (
        "timeout"
        if any(check["status"] == "timeout" for check in checks)
        else "fail"
        if failed or refused_ids
        else "pass"
    )
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
    if any(check["status"] not in {"not-applicable", "refused"} for check in checks):
        payload["timeout"] = {"seconds": arguments.timeout_seconds, "origin": arguments.timeout_origin}
    if refusal is not None:
        payload["refusal"] = {"cause": source_refusal_cause(refusal[1]), "reason": refusal[1]}
    if refused_ids:
        payload["checks_refused"] = refused_ids
    if arguments.tool_cache is not None:
        payload["tool_cache"] = arguments.tool_cache.as_posix()
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
    candidate_shards = _shard_members(verified)
    for name in candidate_shards - previous_shards:
        if os.path.lexists(output / name):
            raise ValueError(f"gate-publication-unowned-shard:{name}")
        before[name] = None
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
        admitted = name in _GATE_REPORTS or name in inventories or name in candidate_shards
        candidate = _file_state(stage / name) if admitted else None
        after[name] = candidate[-1] if candidate else None
        if name in candidate_shards and candidate is None:
            raise ValueError(f"gate-publication-missing-shard:{name}")
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
    if any(check["id"] == "tests" and check.get("cause") == "missing-python-package" for check in checks):
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
