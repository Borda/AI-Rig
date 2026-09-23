#!/usr/bin/env python3
"""heal_git_artifacts.py — reclaim stale skill locks and orphaned git worktrees.

Long-running skills (``oss:resolve``, ``oss:review``, ``research:fortify``, the
``develop`` ``--worktree`` family) leave two kinds of durable artifact behind:

* **Advisory lock files** in the git *common* directory, so the lock is shared
  across every linked worktree of the same repository.
* **Git worktrees** under ``.claude/worktrees/`` (or a skill-specific root).

Both are written on acquisition and removed on the happy path only. A crashed,
interrupted or killed run leaks them. A leaked lock on an abandoned branch is
never revisited by the skill that made it, so it survives indefinitely (observed
in the wild: 27 days). This module is the self-healing counterpart — it decides
which leaked artifacts are provably reclaimable and, on request, reclaims them.

Design notes that are easy to get wrong:

* **Liveness comes from the parent PID, not ``$$``.** Every Bash tool call runs
  in a fresh shell, so ``$$`` names a process that is dead moments after the
  lock is written; ``kill -0`` on it would report *every* lock stale, including
  a healthy run's. The session-stable value is ``$PPID`` (the ``claude``
  process itself), which is also what the repository already uses as its
  ``CSID`` fallback. Lock writers must therefore store ``$PPID``.
* **There is no release-on-exit.** A shell ``trap`` is per-process and cannot
  outlive the Bash call that registers it, so a trap in the acquiring block
  would delete the lock seconds after acquisition and destroy mutual exclusion.
  Leak-then-steal is the intended model; liveness detection is what makes it
  safe.
* **Age remains a secondary guard.** PID reuse, and lock files copied between
  machines, both defeat liveness alone.

Worktree handling is deliberately asymmetric: a worktree holding uncommitted
work is *reported*, never removed, no matter how old it is, and worktrees whose
name does not match a managed prefix are left alone entirely.

``--root`` must resolve strictly under the git toplevel of the invoking working
directory (the "repository" for this purpose — inside a linked worktree, that
is the worktree's own toplevel, not the main tree). This bounds the argument
itself against an absolute path pointing entirely outside the repo; it does
not, on its own, bound what ``--managed-prefix '*'`` can still reach *inside*
that boundary — an in-repo ``--root`` paired with the wildcard still removes
every sufficiently-aged, unregistered child directory it finds there, since an
unregistered directory's dirty-file check is skipped by design (there is no
git state to check `git status` against). Safe use of the wildcard therefore
still depends on ``--root`` pointing at a directory this tool alone populates
(e.g. a skill-private variant dir), not on the containment check alone.

Usage:
    heal_git_artifacts.py locks --pattern '<glob>' [--max-age-min N] [--apply] [--quiet]
    heal_git_artifacts.py worktrees [--root DIR] [--min-age-days N] [--managed-prefix CSV] [--apply] [--quiet]

Both subcommands are **report-only by default**; ``--apply`` performs removal.

Exit codes:
    0 — success (nothing reclaimable, or reclaimed when ``--apply`` given)
    1 — at least one artifact is reclaimable and ``--apply`` was not given
    2 — usage or environment error (not a git repository, bad arguments,
        ``--root`` unresolvable or outside the repository)
"""

from __future__ import annotations

import argparse
import ctypes
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

#: Minutes after which a lock is reclaimable on age alone, regardless of holder
#: liveness. Matches the threshold the oss:resolve lock has always used.
DEFAULT_LOCK_MAX_AGE_MIN = 30

#: Days a worktree must be untouched before it is even considered for removal.
DEFAULT_WORKTREE_MIN_AGE_DAYS = 14

#: Path-basename prefixes this tool is allowed to remove. Anything else is a
#: worktree a human made by hand and must never be touched. ``dev-`` is
#: excluded on purpose: worktree-isolation.md contracts those as user-reviewed
#: deliverables that are never auto-removed.
DEFAULT_MANAGED_PREFIXES: tuple[str, ...] = ("agent-", "oss-")

#: Win32 ``PROCESS_QUERY_LIMITED_INFORMATION`` — the least privilege that still
#: answers "does this PID exist"; available on Vista+ and succeeds for processes
#: whose full information the caller may not read.
_WIN_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

#: Win32 ``ERROR_ACCESS_DENIED``. The process exists, we simply may not open it.
_WIN_ERROR_ACCESS_DENIED = 5


class LockVerdict(str, Enum):
    """Outcome of inspecting one lock file."""

    HELD = "held"
    STALE_DEAD = "stale-dead"
    STALE_AGED = "stale-aged"
    MALFORMED = "malformed"


class WorktreeTier(str, Enum):
    """Removal eligibility of one worktree directory."""

    MAIN = "main"
    PROTECTED = "protected"
    LIVE = "live"
    DIRTY = "dirty"
    REMOVABLE = "removable"
    ORPHAN = "orphan"


#: Verdicts whose lock may be reclaimed.
RECLAIMABLE_LOCKS = frozenset({LockVerdict.STALE_DEAD, LockVerdict.STALE_AGED})

#: Tiers whose worktree may be removed. ``ORPHAN`` is an unregistered directory
#: that ``git worktree remove`` and ``git worktree prune`` both miss.
RECLAIMABLE_WORKTREES = frozenset({WorktreeTier.REMOVABLE, WorktreeTier.ORPHAN})


@dataclass(slots=True)
class LockState:
    """One inspected lock file.

    Attributes:
        path: Absolute path of the lock file.
        pid: Holder PID parsed from the file, or ``None`` when unparsable.
        age_minutes: Age of the file derived from its mtime.
        verdict: Classification produced by :func:`classify_lock`.
    """

    path: Path
    pid: int | None
    age_minutes: float
    verdict: LockVerdict


@dataclass(slots=True)
class WorktreeState:
    """One inspected worktree directory.

    Attributes:
        path: Absolute path of the worktree.
        registered: Whether git lists it in ``git worktree list``.
        dirty_files: Count of uncommitted entries; ``0`` for a clean tree.
        age_days: Days since the directory was last modified.
        tier: Classification produced by :func:`classify_worktree`.
    """

    path: Path
    registered: bool
    dirty_files: int
    age_days: float
    tier: WorktreeTier


def _pid_alive_posix(pid: int) -> bool:
    """Report whether *pid* names a live process, using POSIX signal 0.

    Signal 0 performs error checking without delivering a signal.
    ``PermissionError`` means the process exists but belongs to another user,
    which still answers the question affirmatively.

    Args:
        pid: Process id to probe.

    Returns:
        ``True`` when the process exists.

    Examples:
        No doctest — an executable example would have to call this function, and
        doctests are not platform-gated. On Windows ``os.kill(pid, 0)`` is not a
        probe at all: signal 0 is ``CTRL_C_EVENT``, so CPython routes it to
        ``GenerateConsoleCtrlEvent`` and delivers Ctrl+C to the whole console
        process group — under pytest that surfaces as a ``KeyboardInterrupt``
        that aborts the run with no failing test to point at. Covered by pytest
        instead, which can skip by platform.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError, ValueError):
        return False
    return True


def _pid_alive_windows(pid: int) -> bool:
    """Report whether *pid* names a live process, using the Win32 API.

    ``os.kill(pid, 0)`` is **not** a liveness probe on Windows: CPython maps
    signal 0 onto ``GenerateConsoleCtrlEvent``/``TerminateProcess`` semantics,
    so it can disturb a live process instead of merely observing it. Opening a
    handle with the least-privilege query right is the correct probe.

    A missing ``ctypes.windll`` (i.e. running on a non-Windows host) makes this
    return ``False`` rather than raising, so the caller can fall back to
    age-based reclamation.

    Args:
        pid: Process id to probe.

    Returns:
        ``True`` when the process exists.

    Examples:
        No doctest — requires the Win32 API; covered by pytest with a
        monkeypatched ``ctypes.windll`` on every host.
    """
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return False
    kernel32 = windll.kernel32
    handle = kernel32.OpenProcess(_WIN_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if handle:
        kernel32.CloseHandle(handle)
        return True
    # Existing-but-unreadable is still alive; every other error code is not.
    return bool(kernel32.GetLastError() == _WIN_ERROR_ACCESS_DENIED)


def pid_alive(pid: int | None) -> bool:
    """Report whether *pid* names a live process on the current host.

    Args:
        pid: Process id to probe, or ``None``.

    Returns:
        ``False`` for ``None`` and for non-positive ids — neither can be
        verified, so the caller falls through to the age guard.

    Examples:
        >>> pid_alive(None)
        False
        >>> pid_alive(0)
        False
        >>> pid_alive(os.getpid())
        True
    """
    if pid is None or pid <= 0:
        return False
    if os.name == "nt":
        return _pid_alive_windows(pid)
    return _pid_alive_posix(pid)


def classify_lock(pid: int | None, *, alive: bool, age_minutes: float, max_age_minutes: float) -> LockVerdict:
    """Decide whether one lock is held or reclaimable.

    A parsed PID that is provably dead reclaims the lock immediately — that is
    the fast path age alone cannot provide. Age remains a secondary guard for
    PID reuse and for lock files that travelled between machines. An
    unparsable PID on a *fresh* file is treated as held: it may have been
    written moments ago by a writer this tool does not know about.

    Args:
        pid: Holder PID parsed from the lock file, or ``None``.
        alive: Result of probing *pid* (ignored when *pid* is ``None``).
        age_minutes: Age of the lock file.
        max_age_minutes: Age at or beyond which the lock is reclaimable.

    Returns:
        The verdict for this lock.

    Examples:
        >>> classify_lock(4242, alive=False, age_minutes=1.0, max_age_minutes=30).value
        'stale-dead'
        >>> classify_lock(4242, alive=True, age_minutes=1.0, max_age_minutes=30).value
        'held'
        >>> classify_lock(4242, alive=True, age_minutes=99.0, max_age_minutes=30).value
        'stale-aged'
        >>> classify_lock(None, alive=False, age_minutes=1.0, max_age_minutes=30).value
        'malformed'
        >>> classify_lock(None, alive=False, age_minutes=99.0, max_age_minutes=30).value
        'stale-aged'
    """
    if pid is not None and not alive:
        return LockVerdict.STALE_DEAD
    if age_minutes >= max_age_minutes:
        return LockVerdict.STALE_AGED
    if pid is None:
        return LockVerdict.MALFORMED
    return LockVerdict.HELD


def parse_lock_pid(text: str) -> int | None:
    """Extract the holder PID from a lock file's contents.

    The lock format is ``<pid> <iso8601-utc>`` on the first line.

    Args:
        text: Raw file contents.

    Returns:
        The PID, or ``None`` when the first token is not a positive integer.

    Examples:
        >>> parse_lock_pid("33410 2026-08-17T18:10:04Z\\n")
        33410
        >>> parse_lock_pid("") is None
        True
        >>> parse_lock_pid("not-a-pid 2026-08-17T18:10:04Z") is None
        True
        >>> parse_lock_pid("-1 x") is None
        True
    """
    first = text.strip().split("\n", 1)[0].strip()
    token = first.split(" ", 1)[0] if first else ""
    try:
        pid = int(token)
    except ValueError:
        return None
    return pid if pid > 0 else None


def classify_worktree(
    name: str,
    *,
    is_main: bool,
    registered: bool,
    dirty_files: int,
    age_days: float,
    min_age_days: float,
    managed_prefixes: tuple[str, ...],
) -> WorktreeTier:
    """Decide the removal eligibility of one worktree directory.

    Order matters and encodes the safety policy: the main tree and unmanaged
    names are never touched; recent activity outranks everything else, so a
    worktree an agent is using right now is safe even if it looks disposable;
    uncommitted work is reported but never removed at any age.

    Args:
        name: Basename of the worktree directory.
        is_main: Whether this is the repository's main working tree.
        registered: Whether git lists it as a worktree.
        dirty_files: Count of uncommitted entries.
        age_days: Days since last modification.
        min_age_days: Minimum age before removal is considered.
        managed_prefixes: Name prefixes this tool is allowed to remove.

    Returns:
        The tier for this worktree.

    Examples:
        >>> kw = dict(is_main=False, registered=True, dirty_files=0,
        ...           min_age_days=14, managed_prefixes=("agent-",))
        >>> classify_worktree("agent-abc", age_days=99, **kw).value
        'removable'
        >>> classify_worktree("agent-abc", age_days=1, **kw).value
        'live'
        >>> classify_worktree("dev-my-feature", age_days=99, **kw).value
        'protected'
        >>> classify_worktree("agent-abc", age_days=99,
        ...                   **{**kw, "dirty_files": 4}).value
        'dirty'
        >>> classify_worktree("agent-abc", age_days=99,
        ...                   **{**kw, "registered": False}).value
        'orphan'
    """
    if is_main:
        return WorktreeTier.MAIN
    if not any(name.startswith(prefix) for prefix in managed_prefixes):
        return WorktreeTier.PROTECTED
    if age_days < min_age_days:
        return WorktreeTier.LIVE
    if dirty_files > 0:
        return WorktreeTier.DIRTY
    if not registered:
        return WorktreeTier.ORPHAN
    return WorktreeTier.REMOVABLE


def _git(args: list[str], cwd: Path | None = None) -> str:
    """Run a git command and return its stdout, or ``""`` on any failure.

    Args:
        args: Arguments after ``git``.
        cwd: Working directory for the call.

    Returns:
        Captured stdout with trailing whitespace stripped.

    Examples:
        No doctest — requires a git repository; covered by pytest.
    """
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _file_age_minutes(path: Path, now: float) -> float:
    """Return the age of *path* in minutes, or ``0.0`` when it cannot be read.

    Args:
        path: File to stat.
        now: Reference epoch seconds.

    Returns:
        Age in minutes; ``0.0`` (i.e. "fresh, do not reclaim") on stat failure.

    Examples:
        No doctest — filesystem-dependent; covered by pytest with tmp_path.
    """
    try:
        return max(0.0, (now - path.stat().st_mtime) / 60.0)
    except OSError:
        return 0.0


def sweep_locks(git_dir: Path, pattern: str, max_age_minutes: float, now: float) -> list[LockState]:
    """Inspect every lock in *git_dir* matching *pattern*.

    The glob is applied across the whole common directory rather than to a
    single branch's lock. Branch-scoped inspection is what let a leaked lock on
    an abandoned branch survive indefinitely — the skill that made it never
    runs on that branch again, so nothing ever looks at it.

    Args:
        git_dir: Git common directory holding the locks.
        pattern: Glob such as ``oss-resolve-*.lock``.
        max_age_minutes: Age at or beyond which a lock is reclaimable.
        now: Reference epoch seconds.

    Returns:
        One :class:`LockState` per matching file, sorted by path.

    Examples:
        No doctest — filesystem-dependent; covered by pytest with tmp_path.
    """
    states: list[LockState] = []
    for path in sorted(git_dir.glob(pattern)):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        pid = parse_lock_pid(text)
        age = _file_age_minutes(path, now)
        verdict = classify_lock(pid, alive=pid_alive(pid), age_minutes=age, max_age_minutes=max_age_minutes)
        states.append(LockState(path=path, pid=pid, age_minutes=age, verdict=verdict))
    return states


def _registered_worktrees(repo_root: Path) -> dict[Path, bool]:
    """Map every registered worktree path to whether it is the main tree.

    Args:
        repo_root: Any directory inside the repository.

    Returns:
        Mapping of resolved worktree path to its "is main tree" flag. The first
        entry ``git worktree list --porcelain`` emits is always the main tree.

    Examples:
        No doctest — requires a git repository; covered by pytest.
    """
    out = _git(["worktree", "list", "--porcelain"], cwd=repo_root)
    result: dict[Path, bool] = {}
    for line in out.splitlines():
        if not line.startswith("worktree "):
            continue
        path = Path(line[len("worktree ") :].strip())
        try:
            path = path.resolve()
        except OSError:
            pass
        result[path] = not result  # first listed entry is the main tree
    return result


def _dirty_count(path: Path) -> int:
    """Count uncommitted entries in the worktree at *path*.

    Args:
        path: Worktree directory.

    Returns:
        Number of ``git status --porcelain`` lines; ``0`` when git cannot
        report (an unregistered directory has no git view of its own).

    Examples:
        No doctest — requires a git repository; covered by pytest.
    """
    out = _git(["status", "--porcelain"], cwd=path)
    return len([line for line in out.splitlines() if line.strip()])


def sweep_worktrees(
    repo_root: Path,
    root: Path,
    min_age_days: float,
    managed_prefixes: tuple[str, ...],
    now: float,
) -> list[WorktreeState]:
    """Inspect every worktree directory under *root*.

    Scans the directory rather than ``git worktree list`` alone, so a directory
    that exists on disk but is not registered is still seen. ``git worktree prune``
    cannot help there: prune removes *registrations whose directory is
    gone*, which is the exact opposite case.

    Args:
        repo_root: Repository root, used to enumerate registrations.
        root: Directory holding worktrees (e.g. ``.claude/worktrees``).
        min_age_days: Minimum age before removal is considered.
        managed_prefixes: Name prefixes this tool is allowed to remove.
        now: Reference epoch seconds.

    Returns:
        One :class:`WorktreeState` per subdirectory, sorted by path.

    Examples:
        No doctest — filesystem-dependent; covered by pytest with tmp_path.
    """
    if not root.is_dir():
        return []
    registered = _registered_worktrees(repo_root)
    states: list[WorktreeState] = []
    for path in sorted(p for p in root.iterdir() if p.is_dir()):
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        is_registered = resolved in registered
        try:
            age_days = max(0.0, (now - path.stat().st_mtime) / 86400.0)
        except OSError:
            age_days = 0.0
        dirty = _dirty_count(path) if is_registered else 0
        tier = classify_worktree(
            path.name,
            is_main=registered.get(resolved, False),
            registered=is_registered,
            dirty_files=dirty,
            age_days=age_days,
            min_age_days=min_age_days,
            managed_prefixes=managed_prefixes,
        )
        states.append(
            WorktreeState(path=path, registered=is_registered, dirty_files=dirty, age_days=age_days, tier=tier)
        )
    return states


def _remove_worktree(state: WorktreeState, repo_root: Path) -> bool:
    """Remove one worktree, choosing the mechanism its tier requires.

    Args:
        state: Worktree to remove.
        repo_root: Repository root, for the ``git worktree remove`` call.

    Returns:
        ``True`` when the directory is gone afterwards.

    Examples:
        No doctest — mutates the filesystem; covered by pytest with tmp_path.
    """
    if state.tier is WorktreeTier.REMOVABLE:
        _git(["worktree", "remove", str(state.path)], cwd=repo_root)
    if state.path.exists():
        # ORPHAN has no registration to remove; a REMOVABLE that git declined
        # (e.g. a locked administrative entry) falls through to the same path.
        shutil.rmtree(state.path, ignore_errors=True)
        _git(["worktree", "prune"], cwd=repo_root)
    return not state.path.exists()


def _emit(lines: list[str], quiet: bool) -> None:
    """Print *lines* unless *quiet* is set.

    Args:
        lines: Report lines.
        quiet: Suppress output.

    Examples:
        No doctest — writes to stdout; covered by pytest with capsys.
    """
    if quiet:
        return
    for line in lines:
        print(line)


def run_locks(args: argparse.Namespace, git_dir: Path, now: float) -> int:
    """Execute the ``locks`` subcommand.

    Args:
        args: Parsed arguments.
        git_dir: Git common directory.
        now: Reference epoch seconds.

    Returns:
        Process exit code.

    Examples:
        No doctest — filesystem-dependent; covered by pytest.
    """
    states = sweep_locks(git_dir, args.pattern, args.max_age_min, now)
    reclaimable = [s for s in states if s.verdict in RECLAIMABLE_LOCKS]
    lines: list[str] = []
    for state in states:
        if state.verdict is LockVerdict.HELD:
            lines.append(f"⛔ held — {state.path.name} (pid {state.pid} alive, {state.age_minutes:.0f} min)")
        elif state.verdict is LockVerdict.MALFORMED:
            lines.append(f"⚠ unparsable but fresh — {state.path.name} ({state.age_minutes:.0f} min); left alone")
    for state in reclaimable:
        why = "holder dead" if state.verdict is LockVerdict.STALE_DEAD else f"age {state.age_minutes:.0f} min"
        verb = "reclaimed" if args.apply else "reclaimable"
        lines.append(f"⚠ {verb} — {state.path.name} (pid {state.pid}, {why})")
        if args.apply:
            state.path.unlink(missing_ok=True)
    if not states:
        lines.append(f"✓ no locks matching '{args.pattern}'")
    _emit(lines, args.quiet)
    if not reclaimable:
        return 0
    return 0 if args.apply else 1


def _validate_worktree_root(root: Path, repo_root: Path) -> None:
    """Reject a ``--root`` that escapes the invoking repository (CWE-22 guard).

    ``--managed-prefix '*'`` treats every child of ``root`` as tool-managed; without
    this check, a broad ``--root`` combined with that wildcard lets ``--apply``
    recursively ``shutil.rmtree`` every sufficiently-aged, clean-looking subdirectory
    under an arbitrary path. Bounds only the ``--root`` argument itself to the git
    toplevel of the invoking working directory — it does not bound what the wildcard
    can still reach *inside* that boundary (a separate, accepted residual risk; see
    the module-level note above ``run_worktrees``).

    Args:
        root: Raw ``--root`` value, not yet resolved.
        repo_root: Git toplevel of the invoking working directory — the only
            trusted containment boundary this check has available. Not
            necessarily the physical main repository: run from inside a linked
            worktree, this is that worktree's own toplevel.

    Raises:
        ValueError: When ``root`` cannot be resolved, or does not resolve
            strictly under ``repo_root``.

    Examples:
        No doctest — path-resolution dependent; covered by pytest.
    """
    try:
        resolved = root.resolve()
        boundary = repo_root.resolve()
    except (OSError, RuntimeError) as exc:  # RuntimeError: symlink loop
        raise ValueError(f"--root cannot be resolved: {root} ({exc})") from exc
    if boundary not in resolved.parents:
        raise ValueError(f"--root must be within the repository, got: {resolved}")


def run_worktrees(args: argparse.Namespace, repo_root: Path, now: float) -> int:
    """Execute the ``worktrees`` subcommand.

    Args:
        args: Parsed arguments.
        repo_root: Repository root.
        now: Reference epoch seconds.

    Returns:
        Process exit code.

    Examples:
        No doctest — filesystem-dependent; covered by pytest.
    """
    prefixes = tuple(p for p in (s.strip() for s in args.managed_prefix.split(",")) if p)
    # "*" means every child is tool-managed — valid only for a skill-private
    # root (e.g. fortify's variant dir) where nothing else can appear. The empty
    # prefix matches any name via str.startswith. Never spelled as an empty
    # value, so a mistyped argument cannot silently widen the blast radius.
    if "*" in prefixes:
        prefixes = ("",)
    root = Path(args.root) if args.root else repo_root / ".claude" / "worktrees"
    try:
        _validate_worktree_root(root, repo_root)
    except ValueError as exc:
        print(f"! SECURITY: {exc}", file=sys.stderr)
        return 2
    states = sweep_worktrees(repo_root, root, args.min_age_days, prefixes, now)
    reclaimable = [s for s in states if s.tier in RECLAIMABLE_WORKTREES]
    lines: list[str] = []
    for state in states:
        if state.tier is WorktreeTier.DIRTY:
            lines.append(
                f"⚠ dirty — {state.path.name}: {state.dirty_files} uncommitted, {state.age_days:.0f} d — NOT removed, review by hand"
            )
        elif state.tier is WorktreeTier.PROTECTED:
            lines.append(f"· protected — {state.path.name} (unmanaged name; never auto-removed)")
    for state in reclaimable:
        kind = "unregistered dir" if state.tier is WorktreeTier.ORPHAN else "clean worktree"
        verb = "removed" if args.apply else "removable"
        ok = _remove_worktree(state, repo_root) if args.apply else True
        mark = "⚠" if ok else "✗"
        lines.append(f"{mark} {verb} — {state.path.name} ({kind}, {state.age_days:.0f} d)")
    if not reclaimable:
        lines.append(f"✓ no reclaimable worktrees under {root}")
    _emit(lines, args.quiet)
    if not reclaimable:
        return 0
    return 0 if args.apply else 1


def _build_parser() -> argparse.ArgumentParser:
    """Construct the CLI parser.

    Returns:
        Configured parser with the ``locks`` and ``worktrees`` subcommands.

    Examples:
        >>> _build_parser().prog
        'heal_git_artifacts.py'
    """
    parser = argparse.ArgumentParser(
        prog="heal_git_artifacts.py",
        description="Reclaim stale skill locks and orphaned git worktrees. Report-only unless --apply.",
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    locks = sub.add_parser("locks", help="Sweep advisory lock files in the git common directory.")
    locks.add_argument("--pattern", required=True, help="Glob for lock files, e.g. 'oss-resolve-*.lock'.")
    locks.add_argument(
        "--max-age-min",
        type=float,
        default=DEFAULT_LOCK_MAX_AGE_MIN,
        help=f"Age in minutes at which a lock is reclaimable regardless of holder liveness (default: {DEFAULT_LOCK_MAX_AGE_MIN}).",
    )

    trees = sub.add_parser("worktrees", help="Sweep worktree directories, removing only clean aged managed ones.")
    trees.add_argument(
        "--root",
        default=None,
        help="Directory holding worktrees; must resolve inside the invoking repository "
        "(default: <repo>/.claude/worktrees).",
    )
    trees.add_argument(
        "--min-age-days",
        type=float,
        default=DEFAULT_WORKTREE_MIN_AGE_DAYS,
        help=f"Minimum days untouched before removal is considered (default: {DEFAULT_WORKTREE_MIN_AGE_DAYS}).",
    )
    trees.add_argument(
        "--managed-prefix",
        default=",".join(DEFAULT_MANAGED_PREFIXES),
        help=f"Comma-separated name prefixes eligible for removal, or '*' for every child — only safe with a skill-private --root (default: {','.join(DEFAULT_MANAGED_PREFIXES)}).",
    )

    for sp in (locks, trees):
        sp.add_argument("--apply", action="store_true", help="Perform the reclamation instead of only reporting it.")
        sp.add_argument("--quiet", action="store_true", help="Suppress the report; use the exit code only.")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Inspect or reclaim stale Git artifacts selected by the command line.

    Args:
        argv: Argument list (defaults to ``sys.argv[1:]``).

    Returns:
        0 on success, 1 when reclaimable artifacts remain unreclaimed,
        2 on usage or environment error.

    Examples:
        No doctest — argv-/filesystem-dependent; covered by pytest.
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = _build_parser().parse_args(argv)

    repo_root_raw = _git(["rev-parse", "--show-toplevel"])
    if not repo_root_raw:
        print("heal_git_artifacts: not inside a git repository", file=sys.stderr)
        return 2
    repo_root = Path(repo_root_raw)
    now = time.time()

    if args.mode == "locks":
        git_dir_raw = _git(["rev-parse", "--git-common-dir"])
        if not git_dir_raw:
            print("heal_git_artifacts: cannot resolve git common directory", file=sys.stderr)
            return 2
        git_dir = Path(git_dir_raw)
        if not git_dir.is_absolute():
            git_dir = (repo_root / git_dir).resolve()
        return run_locks(args, git_dir, now)
    return run_worktrees(args, repo_root, now)


if __name__ == "__main__":
    sys.exit(main())
