#!/usr/bin/env python
"""Compare the PR branch on its remote with the run's local commits when the run itself did not push.

Purpose: An ``oss:resolve`` run that leaves its commits unpushed (the user chose not to push, the push was blocked or
rejected) used to report only its own push status. Reports then found the remote branch already carrying the run's
commits, pushed from somewhere else, or moved in a way the report could not describe. This helper reads the remote
branch head once at the end of the run and names what it holds relative to the run's start and its local commits.

Scope: Read-only. One ``git ls-remote`` call against the configured remote, plus local ancestry checks. It never
fetches, never pushes and never explains why the remote changed: a concurrent session, the user, or another tool can
all move a branch, and the run has no evidence to tell them apart.

States (``REMOTE_STATE``):
    unchanged          the remote branch is still at the commit the run started from
    pushed-externally  the remote branch carries some or all of this run's commits, which this run did not push
    diverged           the remote branch moved and does not carry this run's commits
    moved              the remote branch moved, and this run made no commits to compare
    absent             the remote branch does not exist
    unknown            the remote or the run's import history could not be verified

Usage:
    resolve_remote_state.py --push-status S --remote NAME --branch REF --start-head SHA
        --imports-file PATH [--local-head REV]

Outputs: ``REMOTE_STATE=<state>`` and ``REMOTE_DETAIL=<one line>``; ``REMOTE_STATE=n/a`` when the run pushed (the PR
head check covers it) or has no PR branch.

Failure: Exit 0 on every git outcome (an unreadable remote is ``unknown``); argparse rejects bad arguments with exit 2.

Used by: ``skills/resolve/modes/final-report.md`` (Step 11).
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

#: Seconds allowed for the network read of the remote branch head.
_LS_REMOTE_TIMEOUT = 30
#: Seconds allowed for each local git call.
_LOCAL_TIMEOUT = 15
#: A full SHA-1 or SHA-256 object id.
_OID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
#: A configured remote name: no option-like or path-like spelling reaches git argv.
_REMOTE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
#: Push statuses under which the run itself pushed; the PR head check already covers them.
_PUSHED_STATUSES = frozenset({"pushed"})


class RemoteState(str, Enum):
    """What the remote branch holds relative to the run."""

    UNCHANGED = "unchanged"
    PUSHED_EXTERNALLY = "pushed-externally"
    DIVERGED = "diverged"
    MOVED = "moved"
    ABSENT = "absent"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RemoteVerdict:
    """The classified state and its one-line detail."""

    state: RemoteState
    detail: str


@dataclass(frozen=True)
class Heads:
    """The three commits and import evidence the classification compares.

    Attributes:
        remote: Remote branch head, or empty when the branch does not exist.
        start: PR head the run started from, or empty when not recorded.
        local: Local ``HEAD`` at the end of the run.
        imports_file: Run ledger containing the recorded base and pull bounds.
    """

    remote: str
    start: str
    local: str
    imports_file: Path | None = None


@dataclass(frozen=True)
class ImportBound:
    """An import's pre-operation checkout head and the tip it brought in."""

    kind: str
    before: str
    tip: str


def _git(args: list[str], timeout: int, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run one git command and capture its text output."""
    return subprocess.run(  # noqa: S603 - argv list, no shell
        [shutil.which("git") or "git", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        encoding="utf-8",
        cwd=cwd,
    )


def read_remote_head(remote: str, branch: str, cwd: Path | None = None) -> tuple[str | None, str]:
    """Read the remote branch head with ``git ls-remote``.

    Args:
        remote: Configured remote name.
        branch: Branch name on that remote.
        cwd: Repository directory; ``None`` uses the current directory.

    Returns:
        ``(oid, error)``: the head oid, ``""`` when the branch does not exist, or ``None`` with an error text when the
        remote cannot be read.
    """
    full_ref = f"refs/heads/{branch}"
    try:
        proc = _git(["ls-remote", "--refs", "--", remote, full_ref], _LS_REMOTE_TIMEOUT, cwd)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"git ls-remote failed: {exc}"
    if proc.returncode != 0:
        reason = (proc.stderr.strip().splitlines() or ["no error text"])[-1]
        return None, f"git ls-remote exited {proc.returncode}: {reason}"
    for line in proc.stdout.splitlines():
        oid, _, ref = line.partition("\t")
        if ref == full_ref and _OID.match(oid):
            return oid, ""
    return "", ""


def _read_import_bounds(path: Path) -> list[ImportBound]:
    """Read complete Step 5 records, pairing each pull start with its end."""
    history = path.read_text(encoding="utf-8")
    if not history.endswith("\n"):
        raise ValueError("run import bounds are empty or truncated")
    bounds: list[ImportBound] = []
    pending = ""
    for line in history.splitlines():
        bound = _parse_import_bound(line)
        if pending:
            if bound.kind != "pull-end" or bound.before != pending:
                raise ValueError("run import bounds have an unmatched pull-start")
            pending = ""
        elif bound.kind == "pull-start" and bound.before == bound.tip:
            pending = bound.before
            continue
        elif bound.kind != "base":
            raise ValueError("run import bounds have an unexpected record")
        bounds.append(bound)
    if pending or not any(bound.kind == "base" for bound in bounds):
        raise ValueError("run import bounds are missing a base or pull-end record")
    return bounds


def _parse_import_bound(line: str) -> ImportBound:
    """Require the kind and two full object IDs written by Step 5."""
    fields = line.split()
    if len(fields) != 3 or not all(_OID.fullmatch(oid) for oid in fields[1:]):
        raise ValueError("run import bounds contain a malformed record")
    return ImportBound(*fields)


def _is_ancestor(ancestor: str, descendant: str, cwd: Path | None) -> bool:
    """Tell whether Git proves the first commit is reachable from the second."""
    return _git(["merge-base", "--is-ancestor", ancestor, descendant], _LOCAL_TIMEOUT, cwd).returncode == 0


def _commit_set(head: str, excluded: tuple[str, ...], cwd: Path | None) -> set[str]:
    """List commits reachable from a tip after excluding the given histories."""
    proc = _git(["rev-list", head, "--not", *excluded, "--"], _LOCAL_TIMEOUT, cwd)
    if proc.returncode != 0:
        raise ValueError(f"could not walk commit history at {head[:12]}")
    return set(proc.stdout.splitlines())


def _run_commits(heads: Heads, cwd: Path | None) -> set[str]:
    """Subtract each proven import interval from the run's local history."""
    if heads.imports_file is None:
        raise ValueError("run import bounds are missing")
    bounds = _read_import_bounds(heads.imports_file)
    shallow = _git(["rev-parse", "--is-shallow-repository"], _LOCAL_TIMEOUT, cwd)
    if shallow.returncode != 0 or shallow.stdout.strip() != "false":
        raise ValueError("complete commit ancestry is unavailable")
    commits = _commit_set(heads.local, (heads.start,), cwd)
    previous = heads.start
    for bound in bounds:
        if not all(_local_head(oid, cwd) == oid for oid in (bound.before, bound.tip)):
            raise ValueError("run import bounds refer to an unavailable commit")
        if not _is_ancestor(previous, bound.before, cwd) or not _is_ancestor(bound.tip, heads.local, cwd):
            raise ValueError("run import bounds are outside the recorded local history")
        previous = bound.before
        if bound.kind == "pull-end":
            if not _is_ancestor(bound.before, bound.tip, cwd):
                raise ValueError("pull rewrote its starting history")
            previous = bound.tip
        # A whole-tip exclusion would also remove run commits already reachable before the operation.
        commits.difference_update(_commit_set(bound.tip, (bound.before, heads.start), cwd))
    if not _is_ancestor(previous, heads.local, cwd):
        raise ValueError("local HEAD does not preserve the recorded checkout history")
    return commits


def _count(head: str, excluded: tuple[str, ...], cwd: Path | None) -> int | None:
    """Count commits reachable from ``head`` after excluding commits reachable from the other tips."""
    proc = _git(["rev-list", "--count", head, "--not", *excluded, "--"], _LOCAL_TIMEOUT, cwd)
    count = proc.stdout.strip()
    return int(count) if proc.returncode == 0 and count.isdecimal() else None


def classify(heads: Heads, cwd: Path | None = None) -> RemoteVerdict:
    """Classify the remote branch head against the run's start and its local commits.

    Args:
        heads: Remote, start and local heads (see :class:`Heads`).
        cwd: Repository directory; ``None`` uses the current directory.

    Returns:
        The verdict; it describes the branch, never who moved it.
    """
    try:
        return _identity_verdict(heads) or _history_verdict(heads, cwd)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return RemoteVerdict(RemoteState.UNKNOWN, f"could not compare remote head {heads.remote[:12]}: {exc}")


def _identity_verdict(heads: Heads) -> RemoteVerdict | None:
    """Classify the cases decided by comparing object ids alone, or return ``None`` when history is needed."""
    short = heads.remote[:12]
    if not heads.remote:
        return RemoteVerdict(RemoteState.ABSENT, "the remote branch does not exist")
    if heads.start and heads.remote == heads.start:
        return RemoteVerdict(RemoteState.UNCHANGED, f"still at the run's starting commit {short}")
    if heads.local == heads.start:
        return RemoteVerdict(RemoteState.MOVED, f"moved to {short} since the run started; local HEAD is unchanged")
    return None


def _history_verdict(heads: Heads, cwd: Path | None) -> RemoteVerdict:
    """Classify a moved remote by checking reachability of the run's proven commits."""
    remote, start, local = heads.remote, heads.start, heads.local
    short = remote[:12]
    if _local_head(remote, cwd) != remote:
        return RemoteVerdict(RemoteState.UNKNOWN, f"remote head {short} is not in this checkout; fetch to compare")
    if not start or _local_head(start, cwd) != start:
        return RemoteVerdict(RemoteState.UNKNOWN, f"remote head {short}; the run's starting commit is not recorded")
    commits = _run_commits(heads, cwd)
    total = len(commits)
    if total == 0:
        return RemoteVerdict(RemoteState.MOVED, f"moved to {short} since the run started; this run made no commits")
    missing = len(commits & _commit_set(local, (start, remote), cwd))
    if missing == total:
        return RemoteVerdict(RemoteState.DIVERGED, f"moved to {short}, which does not carry this run's commits")
    return _delivery_verdict(heads, total, missing, cwd)


def _delivery_verdict(heads: Heads, total: int, missing: int, cwd: Path | None) -> RemoteVerdict:
    """Describe the proven intersection and any commits reachable only from the remote."""
    short = heads.remote[:12]
    detail = f"carries {total - missing} of this run's {total} commits (at {short})"
    if missing == 0:
        extra = _count(heads.remote, (heads.local,), cwd)
        detail = f"carries all {total} of this run's commits"
        if extra:
            detail += f" plus {extra} more"
        detail += f" (at {short})"
    return RemoteVerdict(RemoteState.PUSHED_EXTERNALLY, detail)


def _local_head(revision: str, cwd: Path | None) -> str:
    """Resolve the local revision to a full oid, or empty when it does not resolve."""
    proc = _git(["rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}"], _LOCAL_TIMEOUT, cwd)
    oid = proc.stdout.strip()
    return oid if proc.returncode == 0 and _OID.match(oid) else ""


def _valid_branch(branch: str) -> bool:
    """Tell whether ``branch`` is a well-formed branch name that cannot read as an option."""
    if branch.startswith("-"):
        return False
    return _git(["check-ref-format", "--branch", branch], _LOCAL_TIMEOUT).returncode == 0


def resolve_verdict(args: argparse.Namespace) -> RemoteVerdict | None:
    """Turn parsed arguments into a verdict, or ``None`` when the comparison does not apply.

    Args:
        args: Parsed command-line arguments.

    Returns:
        The verdict, or ``None`` for a run that pushed or has no PR branch.
    """
    if args.push_status in _PUSHED_STATUSES or not args.remote or not args.branch:
        return None
    if not _REMOTE_NAME.match(args.remote) or not _valid_branch(args.branch):
        return RemoteVerdict(RemoteState.UNKNOWN, "remote or branch name is not well formed")
    start = args.start_head if _OID.match(args.start_head or "") else ""
    local = _local_head(args.local_head, None)
    if not local:
        return RemoteVerdict(RemoteState.UNKNOWN, f"local {args.local_head} does not resolve")
    remote, error = read_remote_head(args.remote, args.branch)
    if remote is None:
        return RemoteVerdict(RemoteState.UNKNOWN, error)
    return classify(Heads(remote=remote, start=start, local=local, imports_file=args.imports_file))


def main(argv: list[str] | None = None) -> int:
    """Print the remote branch state for the end-of-run report.

    Args:
        argv: Command-line arguments; ``None`` reads ``sys.argv``.

    Returns:
        Always 0 once arguments parse.
    """
    parser = argparse.ArgumentParser(description="Name what the remote PR branch holds after an unpushed run.")
    parser.add_argument("--push-status", required=True, help="the run's recorded push status")
    parser.add_argument("--remote", default="", help="configured remote name of the PR branch")
    parser.add_argument("--branch", default="", help="PR branch name on that remote")
    parser.add_argument("--start-head", default="", help="PR head commit the run started from")
    parser.add_argument("--imports-file", type=Path, help="run ledger containing base and before/after pull records")
    parser.add_argument("--local-head", default="HEAD", help="local revision holding the run's commits")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]
    verdict = resolve_verdict(args)
    if verdict is None:
        print("REMOTE_STATE=n/a")
        print("REMOTE_DETAIL=the run pushed, or has no PR branch")
        return 0
    print(f"REMOTE_STATE={verdict.state.value}")
    print(f"REMOTE_DETAIL={verdict.detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
