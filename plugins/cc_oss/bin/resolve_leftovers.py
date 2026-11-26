#!/usr/bin/env python
"""List the recovery branches, worktrees and stashes an ``oss:resolve`` run left behind, with cleanup commands.

Purpose: Implementation agents work in their own worktrees, and the merge-back keeps a specialist's branch whenever it
cannot prove every commit landed or the staged diff is not yet committed. Runs used to mention those branches in
passing, so repositories collected stale ``worktree-agent-*`` branches with no record of where they came from. This
helper turns the run's own records into one list for the final report, each entry with the command that removes it.

Scope: Read-only. It reads the records the run wrote (``recovery-branches.txt`` and ``worktrees-unreported.txt`` in
the run's working directory, and the stash list saved when the run started) and checks each entry still exists. It
never deletes anything: the user decides, using the printed commands.

Usage:
    resolve_leftovers.py --impl-dir DIR [--stash-before FILE]

Landed proof: a branch qualifies for force deletion when its tip is reachable from HEAD or every path it changed
since its unique merge base has identical content in HEAD. The reason the merge-back recorded is shown, never trusted:
a branch kept while the staged diff was uncommitted still holds the only committed copy of work when the run never
committed it (stage mode, a failed group commit).

Outputs: ``LEFTOVERS=none``, or one ``LEFTOVER_BRANCH=``, ``LEFTOVER_WORKTREE=`` or ``LEFTOVER_STASH=`` line per entry
followed by ``CLEANUP=`` lines: worktrees first (a branch checked out in a worktree cannot be deleted), then every
branch proven landed in one ``git branch -D``. Other branches get a read-only ``git cherry -v HEAD`` line. A worktree
gets a removal command only when a surviving ref contains its tip. Stashes are named by commit id,
because a stash index shifts whenever a stash is added or dropped and the report is read later: the cleanup lists
them with their current index and ``STASH_DROP=`` says how to drop one. A stash is only known to have appeared during
the run, not to be the run's own. ``STASH_CHECK=unavailable`` when no run-start stash list was saved.

Failure: All git calls share one budget. Unavailable checks print ``LEFTOVER_CHECK=incomplete`` and cannot prove a
branch landed. Exit 0 on every git outcome; argparse rejects bad arguments with exit 2.

Used by: ``skills/resolve/modes/final-report.md`` (Step 11).
"""

from __future__ import annotations

import argparse
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from resolve_commit_hooks import CallBudget

#: Seconds allowed for each local git call.
_GIT_TIMEOUT = 5
#: Total seconds shared by branch, worktree and stash checks. The final-report fence must allow more time.
BUDGET_SECONDS = 12
#: Record of branches the merge-back kept, one ``<branch>\t<reason>`` row each.
RECOVERY_BRANCHES = "recovery-branches.txt"
#: Record of worktrees created during the run that no implementation group reported.
UNREPORTED_WORKTREES = "worktrees-unreported.txt"
#: Content of the run-start stash record when the stash list could not be read.
_UNAVAILABLE = "unavailable"
#: How to drop a listed stash: its index is read from the listing at cleanup time, never from this report.
STASH_DROP = "match each stash id above in the listing, then run git stash drop stash@{N} with its current N"
#: Characters stripped from recorded text before it is printed.
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
#: Reasons the merge-back records, in the words the report shows.
_REASONS = {
    "uncommitted": "kept while the combined staged diff was not yet committed",
    "unconfirmed": "kept because git could not confirm every commit landed",
}
#: Label suffixes saying whether HEAD was proven to hold the branch's work.
_LANDED = "every change is already in HEAD"
_NOT_LANDED = "not proven to be in HEAD — may hold work found nowhere else"


class LeftoverKind(str, Enum):
    """Kinds of entries a run can leave behind."""

    BRANCH = "branch"
    WORKTREE = "worktree"
    STASH = "stash"


@dataclass(frozen=True)
class Leftover:
    """One entry the run left behind.

    Attributes:
        kind: What the entry is.
        label: Text shown in the report.
        target: The branch name, worktree path or stash commit id the cleanup acts on.
        inspect: Read-only command to run before deleting, when the entry may hold work found nowhere else.
    """

    kind: LeftoverKind
    label: str
    target: str
    inspect: str = ""


def _git(args: list[str], budget: CallBudget, cwd: Path | None) -> subprocess.CompletedProcess[str]:
    """Run one read-only git command and capture its text output (undecodable bytes kept as surrogates)."""
    return subprocess.run(  # noqa: S603 - argv list, no shell
        [shutil.which("git") or "git", *args],
        capture_output=True,
        text=True,
        timeout=budget.cap(_GIT_TIMEOUT),
        check=False,
        encoding="utf-8",
        errors="surrogateescape",
        cwd=cwd,
    )


def branch_landed(tip: str, budget: CallBudget, cwd: Path | None = None) -> bool:
    """Prove the inspected tip reachable or its net changes identical in HEAD, including merge resolutions."""
    ancestor = _git(["merge-base", "--is-ancestor", tip, "HEAD"], budget, cwd)
    if ancestor.returncode == 0:
        return True
    if ancestor.returncode != 1:
        return False
    base = _git(["merge-base", "--all", "HEAD", tip], budget, cwd)
    bases = base.stdout.splitlines()
    if base.returncode != 0 or len(bases) != 1:
        return False
    diff = ["diff", "--name-only", "-z", "--no-renames", "--no-relative", "--ignore-submodules=none"]
    changed = _git([*diff, bases[0], tip, "--"], budget, cwd)
    if changed.returncode != 0:
        return False
    paths = set(changed.stdout.split("\0")) - {""}
    if not paths:
        return True  # the branch's commits change nothing against where it left HEAD's history
    different = _git([*diff, tip, "HEAD", "--"], budget, cwd)
    return different.returncode == 0 and paths.isdisjoint(different.stdout.split("\0"))


def _lines(path: Path) -> list[str]:
    """Return the non-empty lines of a record file, or an empty list when it is missing or unreadable."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [line.rstrip("\r") for line in text.splitlines() if line.strip()]


def _incomplete(check: str, exc: OSError | subprocess.TimeoutExpired) -> None:
    """Surface skipped checks without suggesting cleanup for an unverified entry."""
    reason = "git timed out" if isinstance(exc, subprocess.TimeoutExpired) else "git could not run"
    print(f"LEFTOVER_CHECK=incomplete ({check}: {reason})")


def _recovery_branch(name: str, reason: str, budget: CallBudget, cwd: Path | None) -> Leftover | None:
    """Look up a recorded ref and qualify it for cleanup only after its tip passes the landed proof."""
    if _git(["check-ref-format", "--branch", name], budget, cwd).returncode != 0:
        return None
    tip = _git(["rev-parse", "--verify", "--quiet", f"refs/heads/{name}"], budget, cwd)
    if tip.returncode != 0:
        return None
    oid = tip.stdout.strip()
    try:
        landed = branch_landed(oid, budget, cwd)
    except (OSError, subprocess.TimeoutExpired) as exc:
        _incomplete(f"branch {name} not proven to be in HEAD", exc)
        landed = False
    why = _REASONS.get(reason, _CONTROL.sub(" ", reason).strip() or "kept by the merge-back")
    inspect = "" if landed else f"git cherry -v HEAD {shlex.quote(name)}"
    label = f"{name} at {oid[:12]} — {why}; {_LANDED if landed else _NOT_LANDED}"
    return Leftover(LeftoverKind.BRANCH, label, name, inspect)


def recovery_branches(impl_dir: Path, budget: CallBudget, cwd: Path | None = None) -> list[Leftover]:
    """List recorded recovery branches that still exist.

    Args:
        impl_dir: The run's working directory.
        cwd: Repository directory; ``None`` uses the current directory.

    Returns:
        One entry per existing branch, in record order, each listed once.
    """
    found = []
    seen = set()
    for row in _lines(impl_dir / RECOVERY_BRANCHES):
        name, _, reason = row.partition("\t")
        name = name.strip()
        if not name or name.startswith("-") or name in seen:
            continue
        seen.add(name)
        try:
            entry = _recovery_branch(name, reason.strip(), budget, cwd)
        except (OSError, subprocess.TimeoutExpired) as exc:
            _incomplete(f"branch {_CONTROL.sub(' ', name)} not proven to be in HEAD", exc)
        else:
            if entry is not None:
                found.append(entry)
    return found


def _worktree_entry(path: str, tip: str, deleting: set[str], budget: CallBudget, cwd: Path | None) -> Leftover:
    """Offer removal only when an independent ref will retain the worktree's commit."""
    try:
        refs = _git(
            ["for-each-ref", f"--contains={tip}", "--format=%(refname)", "refs/heads", "refs/remotes", "refs/tags"],
            budget,
            cwd,
        )
        if refs.returncode == 0 and set(refs.stdout.splitlines()) - deleting:
            return Leftover(LeftoverKind.WORKTREE, path, path)
    except (OSError, subprocess.TimeoutExpired) as exc:
        _incomplete(f"worktree {_CONTROL.sub(' ', path)} tip has no proven surviving ref", exc)
    return Leftover(
        LeftoverKind.WORKTREE,
        f"{path} — tip has no proven surviving ref",
        path,
        f"git -C {shlex.quote(path)} show --no-patch HEAD",
    )


def unreported_worktrees(
    impl_dir: Path, budget: CallBudget, cwd: Path | None = None, *, deleting: set[str] | None = None
) -> list[Leftover]:
    """List recorded unreported worktrees that are still registered.

    Args:
        impl_dir: The run's working directory.
        cwd: Repository directory; ``None`` uses the current directory.

    Returns:
        One entry per worktree still in ``git worktree list``.
    """
    try:
        listing = _git(["worktree", "list", "--porcelain", "-z"], budget, cwd)
    except (OSError, subprocess.TimeoutExpired) as exc:
        _incomplete("worktree listing", exc)
        return []
    if listing.returncode != 0:
        return []
    registered = {}
    for row in listing.stdout.split("\0\0"):
        fields = dict(line.split(" ", 1) for line in row.split("\0") if " " in line)
        if "worktree" in fields and re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", fields.get("HEAD", "")):
            registered[fields["worktree"]] = fields["HEAD"]
    return [
        _worktree_entry(path, registered[path], deleting or set(), budget, cwd)
        for path in dict.fromkeys(_lines(impl_dir / UNREPORTED_WORKTREES))
        if path in registered
    ]


def new_stashes(before: set[str], budget: CallBudget, cwd: Path | None = None) -> list[Leftover]:
    """List stashes whose commit was not in the run-start stash list.

    Args:
        before: Stash commit ids saved when the run started.
        cwd: Repository directory; ``None`` uses the current directory.

    Returns:
        One entry per stash that appeared during the run, newest first, as ``git stash list`` orders them.
    """
    try:
        listing = _git(["stash", "list", "--format=%gd%x09%H%x09%s"], budget, cwd)
    except (OSError, subprocess.TimeoutExpired) as exc:
        _incomplete("stash listing", exc)
        return []
    found = []
    for line in listing.stdout.splitlines():
        ref, oid, subject = [*line.split("\t", 2), "", ""][:3]
        if oid and oid not in before:
            label = f"{oid} {_CONTROL.sub(' ', subject).strip()} (now {ref}; appeared during the run, not proven to be its own)"
            found.append(Leftover(LeftoverKind.STASH, label, oid))
    return found


def cleanup_commands(leftovers: list[Leftover]) -> list[str]:
    """Build the cleanup commands, ordered so no command breaks a later one.

    Worktrees go first (a branch checked out in a worktree cannot be deleted). Branches proven landed share one
    force delete. Unverified worktrees and branches get inspection lines. Stashes get one
    listing line, since their index must be read at cleanup time (see :data:`STASH_DROP`).

    Args:
        leftovers: Entries from the three listing functions.

    Returns:
        Shell commands with every operand quoted.

    Examples:
        >>> kept = Leftover(LeftoverKind.BRANCH, "b", "worktree-agent-a1")
        >>> unsure = Leftover(LeftoverKind.BRANCH, "b", "worktree-agent-b2", "git cherry -v HEAD worktree-agent-b2")
        >>> cleanup_commands([kept, unsure])
        ['git branch -D worktree-agent-a1', 'git cherry -v HEAD worktree-agent-b2']
    """
    commands = [
        item.inspect or f"git worktree remove {shlex.quote(item.target)}"
        for item in leftovers
        if item.kind is LeftoverKind.WORKTREE
    ]
    branches = [item for item in leftovers if item.kind is LeftoverKind.BRANCH]
    landed = [shlex.quote(item.target) for item in branches if not item.inspect]
    if landed:
        commands.append("git branch -D " + " ".join(landed))
    commands.extend(item.inspect for item in branches if item.inspect)
    if any(item.kind is LeftoverKind.STASH for item in leftovers):
        commands.append("git stash list --format='%gd %H %s'")
    return commands


def main(argv: list[str] | None = None) -> int:
    """Print the run's leftovers and their cleanup commands.

    Args:
        argv: Command-line arguments; ``None`` reads ``sys.argv``.

    Returns:
        Always 0 once arguments parse.
    """
    parser = argparse.ArgumentParser(description="List branches, worktrees and stashes a resolve run left behind.")
    parser.add_argument("--impl-dir", default="", help="the run's working directory holding its records")
    parser.add_argument("--stash-before", default="", help="file holding the stash commit ids saved at run start")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", newline="\n", errors="backslashreplace")  # type: ignore[union-attr]
    leftovers: list[Leftover] = []
    budget = CallBudget.starting_now(BUDGET_SECONDS)
    if args.impl_dir:
        impl_dir = Path(args.impl_dir)
        branches = recovery_branches(impl_dir, budget)
        deleting = {f"refs/heads/{branch.target}" for branch in branches if not branch.inspect}
        leftovers += unreported_worktrees(impl_dir, budget, deleting=deleting) + branches
    before = _lines(Path(args.stash_before)) if args.stash_before else [_UNAVAILABLE]
    if _UNAVAILABLE in before or not Path(args.stash_before).is_file():
        print("STASH_CHECK=unavailable (no stash list saved at run start)")
    else:
        leftovers += new_stashes(set(before), budget)
    if not leftovers:
        print("LEFTOVERS=none")
        return 0
    for item in leftovers:
        print(f"LEFTOVER_{item.kind.value.upper()}={item.label}")
    for command in cleanup_commands(leftovers):
        print(f"CLEANUP={command}")
    if any(item.kind is LeftoverKind.STASH for item in leftovers):
        print(f"STASH_DROP={STASH_DROP}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
