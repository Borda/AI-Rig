#!/usr/bin/env python
"""Commit resolved changes with an explicit repository-hook approval boundary.

Purpose:
    Keep contributor-supplied hooks disabled unless the user approves their displayed contents for this run. Scope: The
    resolve commit helpers share this boundary; it does not sandbox approved hooks or Git filters. Usage: Run ``python
    resolve_commit_hooks.py`` to display JSON containing the hook commands, configuration and SHA256.     After user
    approval only, pass that digest in ``RESOLVE_APPROVED_HOOKS`` to the commit helpers.     ``--commit-merge`` finishes
    an active merge with the same hook boundary and bounded formatter recovery. Outputs: The inspector prints the active
    hooks, conventional hook configuration and their content fingerprint.     Commits without approval use a temporary
    empty hooks directory, without changing repository configuration. Failure: Changed approval contents refuse
    execution. A rejecting formatter may restage selected paths and retry once;     unrelated edits, untracked additions
    outside selection, unchanged files, and a second rejection stop recovery. Used by: commit_action_item.py,
    commit_all_items.py, commit_lint_fixes.py and the resolve dispatch modes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_PLUMBING_TIMEOUT = 30


def _git_output(git: str, *args: str) -> bytes:
    """Read bounded Git plumbing output without executing external diff commands."""
    return subprocess.run(  # noqa: S603
        [git, *args], check=True, capture_output=True, timeout=_PLUMBING_TIMEOUT
    ).stdout


def _file_record(path: Path, label: str) -> dict[str, str | int]:
    """Describe exact file bytes, link target and mode without losing undecodable content."""
    content = path.read_bytes()
    return {
        "path": label,
        "mode": path.stat().st_mode,
        "link": os.readlink(path) if path.is_symlink() else "",
        "content": content.decode("utf-8", errors="backslashreplace"),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def inspect_repository_hooks(git: str) -> dict[str, object]:
    """Display and fingerprint active hooks and conventional repository hook configuration.

    The fingerprint includes contents, executable modes and configured hook location. It is portable between resolve
    worktrees whose relative hook paths and contents agree; an absolute hook location remains absolute in the report.
    The displayed hook scripts define executable commands, including any dynamic dispatch they perform.
    """
    hooks_path = os.fsdecode(_git_output(git, "rev-parse", "--git-path", "hooks")).strip()
    config = subprocess.run(  # noqa: S603
        [git, "config", "--show-origin", "--get-all", "core.hooksPath"],
        check=False,
        capture_output=True,
        timeout=_PLUMBING_TIMEOUT,
    )
    if config.returncode not in (0, 1):
        raise RuntimeError("cannot inspect core.hooksPath configuration")
    root = Path(os.fsdecode(_git_output(git, "rev-parse", "--show-toplevel")).strip())
    hook_directory = Path(hooks_path)
    files: dict[str, dict[str, str | int]] = {}
    locations = [(hook_directory, "active-hooks"), (root / ".husky", ".husky"), (root / ".githooks", ".githooks")]
    for directory, label in locations:
        if directory.is_dir():
            for path in sorted(directory.rglob("*")):
                if path.is_file():
                    key = f"{label}/{path.relative_to(directory).as_posix()}"
                    files[key] = _file_record(path, key)
    for name in (".pre-commit-config.yaml", ".pre-commit-config.yml", "package.json", ".huskyrc", ".huskyrc.json"):
        path = root / name
        if path.is_file():
            files[name] = _file_record(path, name)
    # Default .git paths differ between linked worktrees; their contents carry the approval identity instead.
    report: dict[str, object] = {
        "core.hooksPath": config.stdout.decode(errors="backslashreplace"),
        "files": list(files.values()),
    }
    report["sha256"] = hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest()
    report["active_hooks_directory"] = hooks_path
    return report


def _working_files(git: str) -> dict[str, tuple[int, str] | None]:
    """Snapshot dirty tracked and nonignored untracked files, including deletions and symlinks."""
    raw = _git_output(git, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-only", "-z", "HEAD")
    raw += _git_output(git, "ls-files", "--others", "--exclude-standard", "-z")
    state: dict[str, tuple[int, str] | None] = {}
    for name in raw.split(b"\0"):
        if not name:
            continue
        path = Path(os.fsdecode(name))
        key = path.as_posix()
        if path.is_symlink():
            state[key] = (path.lstat().st_mode, os.readlink(path))
        elif path.is_file():
            state[key] = (path.stat().st_mode, hashlib.sha256(path.read_bytes()).hexdigest())
        else:
            state[key] = None
    return state


def commit_with_hooks(git: str, args: list[str], files: list[str] | None, timeout: int) -> int:
    """Run a commit with hooks disabled, or approved hooks with one scoped formatter recovery."""
    approval = os.environ.get("RESOLVE_APPROVED_HOOKS", "")
    if not approval:
        with tempfile.TemporaryDirectory(prefix="resolve-no-hooks-") as empty:
            return subprocess.run(  # noqa: S603
                [git, "-c", f"core.hooksPath={empty}", *args], check=False, timeout=timeout
            ).returncode
    try:
        return _commit_approved(git, args, files, timeout, approval)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"resolve hooks: {exc} — no further commit attempted", file=sys.stderr)
        return 1


def _index_entries(git: str) -> dict[str, bytes]:
    """Capture staged object identities so hooks cannot widen a recovery commit through index-only changes."""
    return {
        os.fsdecode(entry.split(b"\t", 1)[1]): entry.split(b"\t", 1)[0]
        for entry in _git_output(git, "ls-files", "--stage", "-z").split(b"\0")
        if entry
    }


def _commit_approved(git: str, args: list[str], files: list[str] | None, timeout: int, approval: str) -> int:
    """Validate the displayed approval and recover only a formatter's selected-file edits."""
    if inspect_repository_hooks(git)["sha256"] != approval:
        raise RuntimeError("hook commands/configuration changed or approval is invalid; show them and ask again")
    selected = files
    if selected is None:
        raw = _git_output(git, "diff", "--cached", "--name-only", "--no-renames", "-z")
        selected = [os.fsdecode(name) for name in raw.split(b"\0") if name]
    before = _working_files(git)
    staged_before = _index_entries(git)
    result = subprocess.run([git, *args], check=False, timeout=timeout)  # noqa: S603
    if result.returncode == 0:
        return 0
    after = _working_files(git)
    staged_after = _index_entries(git)
    index_changes = {
        name for name in staged_before.keys() | staged_after.keys() if staged_before.get(name) != staged_after.get(name)
    }
    if index_changes - {Path(name).as_posix() for name in selected}:
        raise RuntimeError("hook staged paths outside the selected files; inspect the index before continuing")
    clean = object()
    changed = {name for name in before.keys() | after.keys() if before.get(name, clean) != after.get(name, clean)}
    if not changed:
        return result.returncode
    if changed - {Path(name).as_posix() for name in selected}:
        raise RuntimeError("hook changed paths outside the selected files; inspect the tree before continuing")
    if inspect_repository_hooks(git)["sha256"] != approval:
        raise RuntimeError("hook commands/configuration changed during execution; show them and ask again")
    subprocess.run(  # noqa: S603
        [git, "--literal-pathspecs", "add", "--", *sorted(changed)], check=True, timeout=_PLUMBING_TIMEOUT
    )
    print("resolve hooks: restaged hook-modified selected files; retrying commit once", file=sys.stderr)
    retry = subprocess.run([git, *args], check=False, timeout=timeout)  # noqa: S603
    if retry.returncode:
        print("resolve hooks: second commit attempt failed; inspect hook output and working tree", file=sys.stderr)
    return retry.returncode


def main(argv: list[str] | None = None) -> int:
    """Inspect hooks for parent approval, or finish an active merge through the approved boundary."""
    parser = argparse.ArgumentParser(description="Display repository hook commands/configuration for per-run approval.")
    parser.add_argument("--commit-merge", action="store_true", help="Finish an active merge using the hook boundary.")
    args = parser.parse_args(argv)
    try:
        if args.commit_merge:
            _git_output("git", "rev-parse", "--verify", "MERGE_HEAD")
            return commit_with_hooks("git", ["commit", "--no-edit"], None, 600)
        print(json.dumps(inspect_repository_hooks("git"), ensure_ascii=True, indent=2))
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"resolve hooks: {'merge commit' if args.commit_merge else 'inspection'} failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
