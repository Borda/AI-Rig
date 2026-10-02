#!/usr/bin/env python
"""git_state_snapshot.py — print the repository state a resolve run keeps re-checking, as one JSON object.

``oss:resolve`` used to answer "where am I" with a scatter of separate read-only git calls — ``rev-parse``,
``branch --show-current``, ``status``, ``log``, ``remote -v``, ``worktree list`` — each its own Bash call and its own
model turn. This script runs the same queries in one process and prints one JSON object, so a state check costs one
call. It is read-only: it never stages, commits, fetches, or pushes.

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/git_state_snapshot.py" [--base-ref "$BASE_REF"]

Output keys:
    toplevel, git_dir, branch, head, head_subject, detached, upstream, ahead, behind,
    base_ref, base_target, merge_base, base_ahead, base_behind,
    merge_in_progress, cherry_pick_in_progress, rebase_in_progress,
    staged, unstaged, untracked, unmerged, remotes, worktrees

Exit codes:
    0 — snapshot printed
    1 — not inside a git work tree, or ``--base-ref`` starts with '-' (argv-injection guard); a JSON object with an
        ``error`` key is printed instead
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class StatusLists:
    """Paths from ``git status --porcelain=v1 -z``, grouped the way resolve's gates read them."""

    staged: list[str] = field(default_factory=list)
    unstaged: list[str] = field(default_factory=list)
    untracked: list[str] = field(default_factory=list)
    unmerged: list[str] = field(default_factory=list)


def _git(args: list[str], cwd: Path | None, timeout: int) -> tuple[int, str]:
    """Run one read-only git command and return its exit code and stdout without the trailing newline.

    Args:
        args: Git arguments, without the leading ``git``.
        cwd: Working directory, or ``None`` for the current one.
        timeout: Maximum wait in seconds.

    Returns:
        ``(returncode, stdout)``; the code is 1 and stdout empty when git cannot be run at all.
    """
    try:
        proc = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False, encoding="utf-8"
        )
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return proc.returncode, proc.stdout.rstrip("\n")


def _is_unmerged(xy: str) -> bool:
    """Tell whether a porcelain status code marks an unresolved merge conflict.

    Args:
        xy: The two-character ``XY`` status code.

    Returns:
        ``True`` for every unmerged code git documents (either side ``U``, or ``AA`` / ``DD``).

    Examples:
        >>> _is_unmerged("UU"), _is_unmerged("AA"), _is_unmerged("M ")
        (True, True, False)
    """
    return "U" in xy or xy in {"AA", "DD"}


def parse_porcelain_z(raw: str) -> StatusLists:
    """Group the entries of ``git status --porcelain=v1 -z`` into staged, unstaged, untracked and unmerged paths.

    Args:
        raw: NUL-separated porcelain output. A rename or copy entry carries its source path as the next field.

    Returns:
        The grouped paths; a path changed in both index and work tree appears in both ``staged`` and ``unstaged``.

    Examples:
        >>> parse_porcelain_z("M  a.py\\0 M b.py\\0?? c.py\\0UU d.py\\0R  new.py\\0old.py\\0")
        StatusLists(staged=['a.py', 'new.py'], unstaged=['b.py'], untracked=['c.py'], unmerged=['d.py'])
    """
    lists = StatusLists()
    fields = raw.split("\0")
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if len(entry) < 4:
            continue
        xy, path = entry[:2], entry[3:]
        if xy[0] in "RC":
            index += 1  # skip the rename/copy source path field
        if xy == "??":
            lists.untracked.append(path)
        elif xy == "!!":
            continue
        elif _is_unmerged(xy):
            lists.unmerged.append(path)
        else:
            if xy[0] != " ":
                lists.staged.append(path)
            if xy[1] != " ":
                lists.unstaged.append(path)
    return lists


def parse_remotes(raw: str) -> dict[str, str]:
    """Map each remote name to its fetch URL from ``git remote -v`` output.

    Args:
        raw: ``git remote -v`` output.

    Returns:
        ``{name: fetch_url}``; push-only lines are ignored.

    Examples:
        >>> parse_remotes("origin\\tgit@h:o/r.git (fetch)\\norigin\\tgit@h:o/r.git (push)")
        {'origin': 'git@h:o/r.git'}
    """
    remotes: dict[str, str] = {}
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[2] == "(fetch)":
            remotes[parts[0]] = parts[1]
    return remotes


def parse_worktrees(raw: str) -> list[dict[str, object]]:
    """Turn ``git worktree list --porcelain`` output into one record per worktree.

    Args:
        raw: Porcelain output; records are separated by blank lines.

    Returns:
        Records with ``path``, ``head``, ``branch`` (short name, empty when detached), ``detached`` and ``bare``.

    Examples:
        >>> parse_worktrees("worktree /r\\nHEAD abc\\nbranch refs/heads/main\\n\\nworktree /w\\nHEAD def\\ndetached\\n")
        [{'path': '/r', 'head': 'abc', 'branch': 'main', 'detached': False, 'bare': False}, \
{'path': '/w', 'head': 'def', 'branch': '', 'detached': True, 'bare': False}]
    """
    records: list[dict[str, object]] = []
    for block in raw.strip().split("\n\n"):
        record: dict[str, object] = {"path": "", "head": "", "branch": "", "detached": False, "bare": False}
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            if key == "worktree":
                record["path"] = value
            elif key == "HEAD":
                record["head"] = value
            elif key == "branch":
                record["branch"] = value.removeprefix("refs/heads/")
            elif key in {"detached", "bare"}:
                record[key] = True
        if record["path"]:
            records.append(record)
    return records


def _left_right(left: str, right: str, cwd: Path | None, timeout: int) -> tuple[int | None, int | None]:
    """Count commits only on ``left`` and only on ``right``.

    Args:
        left: First revision (ahead side).
        right: Second revision (behind side).
        cwd: Working directory.
        timeout: Maximum wait in seconds.

    Returns:
        ``(ahead, behind)``, or ``(None, None)`` when either revision does not resolve.
    """
    code, out = _git(["rev-list", "--left-right", "--count", f"{left}...{right}"], cwd, timeout)
    parts = out.split()
    if code != 0 or len(parts) != 2:
        return None, None
    return int(parts[0]), int(parts[1])


def _base_section(base_ref: str, cwd: Path | None, timeout: int) -> dict[str, object]:
    """Describe HEAD against the target branch, preferring the remote-tracking ref resolve merges from.

    Args:
        base_ref: Target branch name; empty skips the section.
        cwd: Working directory.
        timeout: Maximum wait in seconds.

    Returns:
        ``base_ref``, ``base_target``, ``merge_base``, ``base_ahead`` and ``base_behind`` (``None`` when unresolved).
    """
    section: dict[str, object] = {
        "base_ref": base_ref,
        "base_target": None,
        "merge_base": None,
        "base_ahead": None,
        "base_behind": None,
    }
    if not base_ref:
        return section
    for candidate in (f"origin/{base_ref}", base_ref):
        if _git(["rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}"], cwd, timeout)[0] == 0:
            section["base_target"] = candidate
            break
    target = section["base_target"]
    if isinstance(target, str):
        code, merge_base = _git(["merge-base", "HEAD", target], cwd, timeout)
        section["merge_base"] = merge_base if code == 0 else None
        section["base_ahead"], section["base_behind"] = _left_right("HEAD", target, cwd, timeout)
    return section


def _operation_flags(git_dir: Path) -> dict[str, bool]:
    """Report which multi-step git operations are mid-flight, from their marker files.

    Args:
        git_dir: The per-worktree git directory.

    Returns:
        ``merge_in_progress``, ``cherry_pick_in_progress`` and ``rebase_in_progress``.
    """
    return {
        "merge_in_progress": (git_dir / "MERGE_HEAD").exists(),
        "cherry_pick_in_progress": (git_dir / "CHERRY_PICK_HEAD").exists(),
        "rebase_in_progress": (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists(),
    }


def build_snapshot(base_ref: str = "", cwd: Path | None = None, timeout: int = 10) -> dict[str, object] | None:
    """Collect the full read-only state of the repository at ``cwd``.

    Args:
        base_ref: Optional target branch for merge-base and ahead/behind against it.
        cwd: Working directory, or ``None`` for the current one.
        timeout: Maximum wait in seconds per git call.

    Returns:
        The snapshot dict, or ``None`` when ``cwd`` is not inside a git work tree.
    """
    code, toplevel = _git(["rev-parse", "--show-toplevel"], cwd, timeout)
    if code != 0:
        return None
    _, git_dir = _git(["rev-parse", "--absolute-git-dir"], cwd, timeout)
    _, branch = _git(["branch", "--show-current"], cwd, timeout)
    head_code, head = _git(["rev-parse", "--verify", "--quiet", "HEAD"], cwd, timeout)
    _, head_subject = _git(["log", "-1", "--format=%s"], cwd, timeout) if head_code == 0 else (1, "")
    up_code, upstream = _git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"], cwd, timeout)
    ahead, behind = _left_right("HEAD", "@{upstream}", cwd, timeout) if up_code == 0 else (None, None)
    _, status_raw = _git(["status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd, timeout)
    _, remotes_raw = _git(["remote", "-v"], cwd, timeout)
    _, worktrees_raw = _git(["worktree", "list", "--porcelain"], cwd, timeout)
    return {
        "toplevel": toplevel,
        "git_dir": git_dir,
        "branch": branch,
        "head": head if head_code == 0 else "",
        "head_subject": head_subject,
        "detached": not branch,
        "upstream": upstream if up_code == 0 else None,
        "ahead": ahead,
        "behind": behind,
        **_base_section(base_ref, cwd, timeout),
        **_operation_flags(Path(git_dir)),
        **asdict(parse_porcelain_z(status_raw)),
        "remotes": parse_remotes(remotes_raw),
        "worktrees": parse_worktrees(worktrees_raw),
    }


def main(argv: list[str] | None = None) -> int:
    """Print the snapshot as one JSON line.

    Args:
        argv: Command-line arguments; ``None`` reads ``sys.argv``.

    Returns:
        Process exit code (see module docstring).
    """
    parser = argparse.ArgumentParser(description="Print a read-only git state snapshot as JSON.")
    parser.add_argument("--base-ref", default="", help="target branch for merge-base and ahead/behind against it")
    parser.add_argument("--timeout", type=int, default=10, help="seconds per git call")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]
    if args.base_ref.startswith("-"):
        print(json.dumps({"error": f"--base-ref must not start with '-': {args.base_ref!r}"}))
        return 1
    snapshot = build_snapshot(args.base_ref, None, args.timeout)
    if snapshot is None:
        print(json.dumps({"error": "not inside a git work tree"}))
        return 1
    print(json.dumps(snapshot, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
