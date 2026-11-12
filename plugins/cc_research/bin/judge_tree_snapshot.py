#!/usr/bin/env python3
"""Record working-tree evidence around approved judge validation commands.

Purpose:
    detect content and index drift without modifying project files.

Scope:
    staged, unstaged and untracked dirty paths, nested repositories, and ignored experiment state outside the current
    judge run directory.

Usage:
    judge_tree_snapshot.py RUN_DIR before|after, from the project repository.

Outputs:
    dirty-before.json and dirty-after.json in the existing run directory, plus a changed-path summary on stdout after
    comparison.

Failure:
    return non-zero for unavailable evidence or an existing before snapshot; preserve that original baseline. Drift
    itself is reported with exit zero.

Used by:
    research:judge J4, including judge iterations in research:sweep.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


def _git(root: Path, *args: str) -> bytes:
    """Read Git state without running hooks, external diff commands or index refresh writes."""
    return subprocess.check_output(["git", "--no-optional-locks", "-C", str(root), *args])  # noqa: S603,S607 — internal Git argv, no shell.


def _file_state(path: Path) -> dict[str, str] | None:
    """Hash file contents incrementally or record a symlink or nested repository."""
    if path.is_symlink():
        return {"symlink": os.readlink(path)}
    if path.is_file():
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return {"sha256": digest.hexdigest()}
    if not path.exists():
        return None
    if path.is_dir() and (path / ".git").exists():
        return {
            "head": os.fsdecode(_git(path, "rev-parse", "HEAD")).strip(),
            "status_sha256": hashlib.sha256(_git(path, "status", "--porcelain=v1", "-z")).hexdigest(),
        }
    raise RuntimeError(f"Cannot snapshot non-file dirty path: {path}")


def _snapshot(root: Path, run_dir: Path, baseline: dict | None) -> dict:
    """Collect dirty path hashes and index evidence while excluding this run's own artifacts."""
    pathspec = ["--", "."]
    if run_dir.is_relative_to(root):
        pathspec.append(f":(exclude,literal){run_dir.relative_to(root).as_posix()}")
    names = _git(root, "ls-files", "--modified", "--deleted", "--others", "--exclude-standard", "-z", *pathspec)
    names += _git(root, "diff", "--cached", "--name-only", "--no-ext-diff", "-z", *pathspec)
    paths = {os.fsdecode(name) for name in names.split(b"\0") if name}
    state_dir = root / ".experiments/state"
    # Prior-run state is ignored by Git but writable through the Docker mount.
    paths.update(
        path.relative_to(root).as_posix()
        for path in state_dir.rglob("*")
        if not path.is_relative_to(run_dir) and (path.is_symlink() or not path.is_dir())
    )
    if baseline is not None:
        paths.update(baseline["files"])
    return {
        "status": _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", *pathspec).hex(),
        "index": _git(root, "ls-files", "--stage", "-z", *pathspec).hex(),
        "files": {name: _file_state(root / name) for name in sorted(paths)},
    }


def _report_changes(baseline: dict, snapshot: dict) -> None:
    """Name newly dirty, deleted, content-modified and index-only changes against the baseline."""
    changed = {
        name
        for name, state in snapshot["files"].items()
        if name not in baseline["files"] or baseline["files"][name] != state
    }
    index_changes = set(bytes.fromhex(baseline["index"]).split(b"\0")) ^ set(
        bytes.fromhex(snapshot["index"]).split(b"\0")
    )
    changed.update(os.fsdecode(entry.split(b"\t", 1)[1]) for entry in index_changes if entry)
    if changed or baseline["status"] != snapshot["status"]:
        print(
            "⚠ J4 changed the working tree:",
            json.dumps(sorted(changed)),
            "(see dirty-before.json and dirty-after.json)",
        )
    else:
        print("J4 working tree unchanged (other ignored files are not checked)")


def main(argv: list[str] | None = None) -> int:
    """Save one fixed snapshot phase and report failures without replacing baseline evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("phase", choices=("before", "after"))
    args = parser.parse_args(argv)
    run_dir = args.run_dir.resolve()
    baseline_path = run_dir / "dirty-before.json"
    try:
        if args.phase == "before" and baseline_path.exists():
            raise FileExistsError(f"Baseline already exists: {baseline_path}")
        baseline = json.loads(baseline_path.read_text(encoding="utf-8")) if args.phase == "after" else None
        root = Path(os.fsdecode(_git(Path.cwd(), "rev-parse", "--show-toplevel")).strip()).resolve()
        snapshot = _snapshot(root, run_dir, baseline)
        mode = "x" if args.phase == "before" else "w"
        with (run_dir / f"dirty-{args.phase}.json").open(mode, encoding="utf-8", newline="\n") as stream:
            json.dump(snapshot, stream, sort_keys=True)
        if baseline is not None:
            _report_changes(baseline, snapshot)
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"J4 snapshot failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    sys.stderr.reconfigure(encoding="utf-8", newline="\n")
    raise SystemExit(main())
