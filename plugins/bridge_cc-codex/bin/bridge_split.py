"""Run up to three narrow Codex reviews in parallel and print one merged compact envelope.

Purpose:
    One wide review does not fit a synchronous caller's 230 second budget: Codex spends it on its own probes and the
    hard cutoff returns no findings. A caller that can name two or three independent narrow scopes instead gets one
    bounded review per scope, run side by side, so the wall time stays that of a single call. Scope: the Claude to
    Codex ``review`` verb only. Each scope is an ordinary ``bridge_call.py review`` child, so consent, workspace, depth,
    network, sandbox and incident handling are exactly those of a single call; this script adds no authority. Usage:
    ``bridge_split.py --plan-file PATH [--workspace PATH] [--effort LEVEL] [--timeout-seconds N] [--run-id ID]
    [--untrusted-task]``. PATH is a JSON array of one to three non-empty review task strings. Outputs: exactly one JSON
    object on stdout, with ``status`` ``complete`` when every scope completed, ``partial`` when some did, otherwise
    the shared child status (or ``blocked`` when they differ), one ``splits`` row per scope, and the scopes' findings
    and blockers merged with the scope index. A scope that times out, is blocked or fails is a gap, never a verdict:
    this script never re-sends it. Failure: an invalid plan or option prints ``{"status": "error", ...}`` and exits
    2. Used by: the Claude Bridge review skill (``--split-file``).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bridge_task_path import SESSION_PATTERN

#: Most scopes one split may run; Codex quota and the host's concurrency are shared, so the fan-out stays small.
MAX_SPLITS = 3
#: Largest plan file read, in bytes; each scope's own size is bounded again by the child's task limit.
MAX_PLAN_BYTES = 65536
#: Soft budget per scope when the caller names none, matching a synchronous caller's limit.
DEFAULT_SOFT_SECONDS = 230.0
#: Child hard cutoff multiple and per-attempt termination overhead, as documented for one bridge call.
HARD_CUTOFF_MULTIPLIER = 1.2
ATTEMPT_OVERHEAD_SECONDS = 9.0
#: Margin added to one scope's worst case. The split's own limit must stay below the 595 second Bash timeout that the
#: review skill gives a synchronous caller, so the backstop fires first and the finished scopes still print.
SCOPE_BACKSTOP_SLACK_SECONDS = 5.0
#: Directory, relative to the workspace, that holds the bridge's incident records.
INCIDENT_STORE = Path(".temp") / "bridge" / "incidents"
#: Envelope fields kept per scope; the full envelope and transcript stay with the child's own artifacts.
KEPT_FIELDS = ("status", "findings", "blockers", "remaining", "incident", "duration_seconds", "effort", "run_id")


def _read_anchored(path: Path) -> str:
    """Read a plan through one opened descriptor, so a file swapped in after the path checks is never read.

    The open never follows a link and never waits for a FIFO writer; the opened inode must be a regular single-link file
    owned by the current user, within the size limit, and the very file the path check located.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise ValueError("--plan-file must be a regular file")
        located = path.stat()
        if (opened.st_dev, opened.st_ino) != (located.st_dev, located.st_ino):
            raise ValueError("--plan-file changed while it was opened")
        if hasattr(os, "geteuid") and opened.st_uid != os.geteuid():
            raise ValueError("--plan-file must be owned by the current user")
        if opened.st_size > MAX_PLAN_BYTES:
            raise ValueError(f"--plan-file is larger than {MAX_PLAN_BYTES} bytes")
        return stream.read(MAX_PLAN_BYTES + 1).decode("utf-8")


def read_plan(path: Path, workspace: Path) -> list[str]:
    """Return the validated scope tasks from one plan file inside the workspace or the temporary directory.

    Args:
        path: The JSON plan file, a regular unlinked file no larger than :data:`MAX_PLAN_BYTES`.
        workspace: The selected workspace; a plan outside it must be a direct child of the temporary directory.

    Returns:
        One to :data:`MAX_SPLITS` non-empty task strings.

    Raises:
        ValueError: The file is unsafe, oversized, not a JSON array of strings, or has the wrong number of scopes.
    """
    try:
        expected = path.lstat()
        if stat.S_ISLNK(expected.st_mode) or not stat.S_ISREG(expected.st_mode) or expected.st_nlink != 1:
            raise ValueError("--plan-file must be a regular file")
        canonical = path.resolve(strict=True)
        temporary = Path(os.environ.get("TMPDIR") or tempfile.gettempdir()).resolve(strict=True)
        if not canonical.is_relative_to(workspace) and canonical.parent != temporary:
            raise ValueError("--plan-file must be inside the workspace or directly in the temporary directory")
        text = _read_anchored(canonical)
    except OSError as error:
        raise ValueError(f"--plan-file cannot be read: {error.strerror or error}") from error
    except UnicodeDecodeError as error:
        raise ValueError("--plan-file must be UTF-8 text") from error
    try:
        tasks = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"--plan-file is not valid JSON: {error.msg}") from error
    if not isinstance(tasks, list) or not tasks or not all(isinstance(task, str) and task.strip() for task in tasks):
        raise ValueError("--plan-file must be a JSON array of non-empty task strings")
    if len(tasks) > MAX_SPLITS:
        raise ValueError(f"--plan-file lists {len(tasks)} scopes; at most {MAX_SPLITS} run in one split")
    return tasks


def _scratch_path() -> Path:
    """Return a fresh task-file path directly in the temporary directory, named as the bridge accepts it."""
    temporary = Path(os.environ.get("TMPDIR") or tempfile.gettempdir()).resolve(strict=True)
    session = os.environ.get("CSID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "shared"
    if re.fullmatch(SESSION_PATTERN, session) is None:
        raise ValueError("session identifier must contain 1-128 ASCII letters, digits, underscores or hyphens")
    return temporary / f"bridge-task-{session}-{uuid4()}.md"


def _write_scratch(path: Path, task: str) -> None:
    """Create ``path`` exclusively with the task text, owner-only, never following a link."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(task.encode("utf-8"))


def _stop_group(process: subprocess.Popen[str]) -> None:
    """Stop a timed-out child and everything it started in its own group, then reap it.

    The child runs in a session of its own, so one signal reaches its helpers without touching this script. A peer
    process the child itself moved to another session is the child's own cleanup to handle, not reachable from here.
    """
    try:
        if os.name == "nt" or not hasattr(os, "killpg"):
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        process.kill()
    finally:
        process.communicate()


def _supervise(command: list[str], budget: float) -> tuple[int, str]:
    """Run one child with a backstop and return its exit code and stdout; raise ``TimeoutExpired`` past the budget."""
    kwargs: dict[str, object] = (
        {"start_new_session": True}
        if os.name != "nt"
        else {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    )
    process = subprocess.Popen(  # noqa: S603 - argv list, no shell
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kwargs
    )
    try:
        stdout, _ = process.communicate(timeout=budget)
    except subprocess.TimeoutExpired:
        _stop_group(process)
        raise
    return process.returncode, stdout


def _run_scope(index: int, task: str, options: argparse.Namespace) -> dict[str, object]:
    """Run one scope as a ``bridge_call.py review`` child and return its kept envelope fields."""
    run_id = f"{options.run_id}-s{index}"
    row: dict[str, object] = {"index": index, "run_id": run_id}
    budget = (
        2 * (HARD_CUTOFF_MULTIPLIER * options.timeout_seconds + ATTEMPT_OVERHEAD_SECONDS) + SCOPE_BACKSTOP_SLACK_SECONDS
    )
    path = _scratch_path()
    try:
        _write_scratch(path, task)
        command = [
            sys.executable,
            str(Path(__file__).resolve().with_name("bridge_call.py")),
            "review",
            "--task-file",
            str(path),
            "--workspace",
            str(options.workspace),
            "--effort",
            options.effort,
            "--timeout-seconds",
            f"{options.timeout_seconds:g}",
            "--run-id",
            run_id,
        ]
        if options.untrusted_task:
            command.append("--untrusted-task")
        returncode, stdout = _supervise(command, budget)
        envelope = json.loads(stdout.strip().splitlines()[-1]) if stdout.strip() else {}
        if not isinstance(envelope, dict) or not envelope:
            row.update(status="error", blockers=[f"scope {index} printed no envelope (exit {returncode})"])
        else:
            row.update({key: envelope[key] for key in KEPT_FIELDS if key in envelope})
            row.setdefault("status", "error")
            if envelope.get("error"):
                row["blockers"] = [str(envelope["error"])]
    except subprocess.TimeoutExpired:
        row.update(status="timeout", blockers=[f"scope {index} passed its {budget:g} second backstop and was stopped"])
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        row.update(status="error", blockers=[f"scope {index} did not run: {error}"])
    finally:
        path.unlink(missing_ok=True)
    return row


def _split_incident(rows: list[dict[str, object]], workspace: Path | None) -> str | None:
    """Return the incident path a caller should read for the whole split, preferring a quota-exhausted record.

    A caller that reads one top-level ``incident`` must still see a quota stop that hit any scope, so the first scope
    whose record names a ``quota-exhausted`` fault wins; otherwise the first recorded incident is returned.
    """
    paths = [str(row["incident"]) for row in rows if isinstance(row.get("incident"), str) and row["incident"]]
    if workspace is not None:
        store = (workspace / INCIDENT_STORE).resolve()
        for raw in paths:
            record = (workspace / raw).resolve()
            if not record.is_relative_to(store) or record.is_symlink():
                continue
            try:
                fault = json.loads(record.read_text(encoding="utf-8")).get("fault")
            except (OSError, ValueError, AttributeError):
                continue
            if fault == "quota-exhausted":
                return raw
    return paths[0] if paths else None


def merge(rows: list[dict[str, object]], workspace: Path | None = None) -> dict[str, object]:
    """Fold the per-scope rows into one envelope; a gap in any scope is visible, never absorbed into a pass."""
    statuses = [str(row.get("status")) for row in rows]
    if all(status == "complete" for status in statuses):
        status = "complete"
    elif any(status in {"complete", "partial"} for status in statuses):
        status = "partial"  # a scope that answered part of its question is completed with a gap, never hidden
    else:
        status = statuses[0] if len(set(statuses)) == 1 else "blocked"
        if status == "error":
            status = "blocked"  # a top-level error means the plan was refused; scopes that ran and failed are a gap
    findings: list[object] = []
    blockers: list[str] = []
    remaining: list[object] = []
    for row in rows:
        remaining.extend({"split": row["index"], "remaining": item} for item in row.get("remaining") or [])
        for finding in row.get("findings") or []:
            findings.append({"split": row["index"], "finding": finding})
        blockers.extend(f"split {row['index']}: {text}" for text in row.get("blockers") or [])
    return {
        "status": status,
        "splits": rows,
        "findings": findings,
        "blockers": blockers,
        "remaining": remaining,
        "incident": _split_incident(rows, workspace),
    }


def _parser() -> argparse.ArgumentParser:
    """Create the command parser."""
    parser = argparse.ArgumentParser(description="Run up to three narrow Codex reviews in parallel.")
    parser.add_argument("--plan-file", required=True, type=Path)
    parser.add_argument("--workspace", default=".", type=Path)
    parser.add_argument("--effort", default="medium")
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_SOFT_SECONDS)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--untrusted-task", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the split and emit exactly one JSON object to stdout."""
    options = _parser().parse_args(argv)
    try:
        options.workspace = options.workspace.resolve()
        if not (0 < options.timeout_seconds <= DEFAULT_SOFT_SECONDS):
            raise ValueError(f"--timeout-seconds must be above 0 and at most {DEFAULT_SOFT_SECONDS:g} for a split")
        options.run_id = options.run_id or f"split-{uuid4().hex[:12]}"
        tasks = read_plan(options.plan_file, options.workspace)
        with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
            rows = list(pool.map(lambda item: _run_scope(item[0], item[1], options), enumerate(tasks, start=1)))
        output = merge(rows, options.workspace)
        code = 0
    except (OSError, ValueError) as error:
        output = {"status": "error", "error": str(error)}
        code = 2
    sys.stdout.write(json.dumps(output, sort_keys=True) + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
