#!/usr/bin/env python3
"""Collect a scope-aware local Git diff context pack without a shell wrapper.

## Purpose

Produce stable status, patch, file-list, stat, and source-snapshot evidence for skill artifacts before review or
implementation. Keeping these views in named files lets later workflow steps cite the exact local tree state used for a
review or implementation decision.

## Scope

It invokes read-only local Git inspection for a working tree, path, commit, or explicit source snapshot; it does not
fetch, push, alter branches, or choose review findings. The ``path`` scope compares ``HEAD`` for one path, while
``commit`` compares the requested revision and deliberately leaves ``untracked.txt`` empty. Source snapshots bind
current bytes to explicit safe repository-relative scopes.

## Usage

Run ``python collect_diff.py --scope <working-tree|path|commit> --out <directory>`` with ``--target`` for ``path`` or
``commit`` scopes. To serialize source evidence, run ``python collect_diff.py --snapshot --repository <path>
--scope-path <path> --out <snapshot.json>``. The diff output directory is created when needed; snapshot output is
canonical UTF-8 JSON and cannot be an included non-ignored source path.

## Used by

Codex Rig workflow skills, implement/review artifact setup, and portable-helper acceptance tests use this collector.
Callers treat its files as local evidence and must not infer that an empty patch means the repository was clean unless
``status.txt`` agrees. Source consumers receive bytes encoded as UTF-8 text or base64; base64 preserves binary bytes but
does not make binary semantics reviewable as text.

## Outputs

It writes ``status.txt``, ``diff.patch``, ``files.txt``, ``diffstat.txt``, ``numstat.txt``, and ``untracked.txt`` under
the requested output directory. Snapshot mode writes one JSON object containing repository state plus selected source
records. Invalid diff scope or a missing required target additionally writes ``scope-error.txt`` with a stable reason
before returning exit code ``2``.

## Failure

Invalid scope/target, unsafe source scope, concurrent source-state change, or a failed local Git command exits non-zero
and leaves the caller with no claim that evidence is complete. Git output files may already exist when a later command
fails, so consumers must gate on the exit code rather than treating partial artifacts as a complete pack.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath


ARTIFACT_COMMANDS = (
    ("diff.patch", ("diff",)),
    ("files.txt", ("diff", "--name-only")),
    ("diffstat.txt", ("diff", "--stat")),
    ("numstat.txt", ("diff", "--numstat")),
)


def parse_args() -> argparse.Namespace:
    """Parse the stable collect-diff command-line contract."""
    parser = argparse.ArgumentParser(
        prog="collect_diff.py",
        description=(
            "Collect a Git diff context pack with status.txt, diff.patch, files.txt, "
            "diffstat.txt, numstat.txt, and untracked.txt."
        ),
        epilog="Exit 0 means collection succeeded; exit 2 means invalid input.",
    )
    parser.add_argument("--out", required=True, type=Path, help="Required artifact directory.")
    parser.add_argument(
        "--scope",
        default="working-tree",
        help="Collection scope: working-tree, path, or commit.",
    )
    parser.add_argument("--target", default="", help="Path or revision required by path and commit scopes.")
    parser.add_argument("--snapshot", action="store_true", help="Write a deterministic source snapshot JSON file.")
    parser.add_argument("--repository", type=Path, help="Repository root required by --snapshot.")
    parser.add_argument(
        "--scope-path",
        action="append",
        default=[],
        help="Literal repository-relative source file or directory; repeat for multiple snapshot scopes.",
    )
    return parser.parse_args()


def run_git(arguments: tuple[str, ...], output: Path) -> int:
    """Run one Git argv vector and write its stdout bytes unchanged."""
    with output.open("wb") as stream:
        completed = subprocess.run(["git", *arguments], stdout=stream, check=False)
    return completed.returncode


def collect_diff(scope: str, target: str, output: Path) -> int:
    """Collect the requested diff scope into the canonical artifact set."""
    output.mkdir(parents=True, exist_ok=True)
    status = run_git(("status", "--short"), output / "status.txt")
    if status != 0:
        return status

    if scope in {"path", "commit"} and not target:
        (output / "scope-error.txt").write_text("missing-required:--target\n", encoding="utf-8")
        return 2
    if scope not in {"working-tree", "path", "commit"}:
        (output / "scope-error.txt").write_text(f"invalid-scope:{scope}\n", encoding="utf-8")
        return 2

    if scope == "working-tree":
        revision = ("HEAD",)
        path_suffix: tuple[str, ...] = ()
    elif scope == "path":
        revision = ("HEAD",)
        path_suffix = ("--", target)
    else:
        revision = (target,)
        path_suffix = ()

    for filename, prefix in ARTIFACT_COMMANDS:
        result = run_git((*prefix, *revision, *path_suffix), output / filename)
        if result != 0:
            return result

    untracked = output / "untracked.txt"
    if scope == "commit":
        untracked.write_bytes(b"")
        return 0
    suffix = ("--", target) if scope == "path" else ()
    return run_git(("ls-files", "--others", "--exclude-standard", *suffix), untracked)


def _git_output(repository: Path, arguments: tuple[str, ...]) -> bytes:
    """Return stdout from one successful read-only Git invocation."""
    completed = subprocess.run(
        ["git", "-C", os.fspath(repository), "--literal-pathspecs", *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"Git source snapshot command failed: {' '.join(arguments)}")
    return completed.stdout


def _repository_root(repository: Path) -> Path:
    """Resolve and verify the Git top-level directory for a snapshot repository."""
    candidate = repository.resolve(strict=True)
    if not candidate.is_dir():
        raise ValueError(f"Snapshot repository is not a directory: {repository}")
    root = _git_output(candidate, ("rev-parse", "--show-toplevel")).rstrip(b"\n")
    return Path(os.fsdecode(root)).resolve(strict=True)


def _normalize_scope_paths(repository: Path, scope_paths: list[str]) -> list[str]:
    """Validate and canonically order literal source paths within one repository."""
    if not scope_paths:
        raise ValueError("Source snapshot requires at least one --scope-path")

    normalized: set[str] = set()
    for raw_path in scope_paths:
        if not raw_path or "\x00" in raw_path or raw_path.startswith(":"):
            raise ValueError(f"Unsafe source snapshot scope: {raw_path!r}")
        posix_path = PurePosixPath(raw_path)
        windows_path = PureWindowsPath(raw_path)
        if posix_path.is_absolute() or windows_path.is_absolute() or windows_path.drive:
            raise ValueError(f"Source snapshot scope must be repository-relative: {raw_path!r}")

        parts = tuple(part for part in PurePath(raw_path).parts if part not in {"", "."})
        if any(part == ".." for part in parts):
            raise ValueError(f"Source snapshot scope cannot contain '..': {raw_path!r}")
        path = PurePosixPath(*parts).as_posix() if parts else "."
        resolved = (repository / Path(*PurePosixPath(path).parts)).resolve(strict=False)
        try:
            resolved.relative_to(repository)
        except ValueError as error:
            raise ValueError(f"Source snapshot scope escapes repository: {raw_path!r}") from error
        normalized.add(path)
    return sorted(normalized)


def _source_inventory(repository: Path, scope_paths: list[str]) -> bytes:
    """Return the tracked and non-ignored file names selected by literal scopes."""
    return _git_output(
        repository,
        ("ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", *scope_paths),
    )


def _source_content(source: bytes) -> tuple[str, str]:
    """Encode source bytes as exact UTF-8 text or base64 for JSON transport."""
    try:
        return "utf-8", source.decode("utf-8")
    except UnicodeDecodeError:
        return "base64", base64.b64encode(source).decode("ascii")


def _source_record(repository: Path, relative_path: str) -> dict[str, object]:
    """Capture one selected worktree entry without following a final symlink."""
    path = repository / Path(*PurePosixPath(relative_path).parts)
    try:
        path.parent.resolve(strict=False).relative_to(repository)
    except ValueError as error:
        raise RuntimeError(f"Source inventory path escapes repository: {relative_path!r}") from error

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return {
            "path": relative_path,
            "kind": "missing",
            "sha256": None,
            "executable": False,
            "encoding": "utf-8",
            "content": "",
        }

    if stat.S_ISLNK(metadata.st_mode):
        source = os.fsencode(os.readlink(path))
        kind = "symlink"
    elif stat.S_ISREG(metadata.st_mode):
        source = path.read_bytes()
        kind = "file"
    else:
        raise RuntimeError(f"Source snapshot does not support non-file entry: {relative_path!r}")

    encoding, content = _source_content(source)
    return {
        "path": relative_path,
        "kind": kind,
        "sha256": hashlib.sha256(source).hexdigest(),
        "executable": kind == "file" and bool(metadata.st_mode & stat.S_IXUSR),
        "encoding": encoding,
        "content": content,
    }


def _inventory_paths(inventory: bytes) -> list[str]:
    """Decode and sort NUL-delimited Git file names for deterministic records."""
    return sorted({os.fsdecode(path) for path in inventory.split(b"\0") if path})


def capture_source_snapshot(repository: Path, scope_paths: list[str]) -> dict[str, object]:
    """Capture explicit current source bytes and Git state for a repository scope.

    The returned records include tracked, untracked non-ignored, and missing tracked paths. It compares the revision,
    index, and selected file inventory before and after reading source to reject observable concurrent changes.
    """
    root = _repository_root(repository)
    scopes = _normalize_scope_paths(root, scope_paths)
    revision_before = _git_output(root, ("rev-parse", "--verify", "HEAD")).rstrip(b"\n")
    index_before = _git_output(root, ("ls-files", "--stage", "-z", "--", *scopes))
    inventory_before = _source_inventory(root, scopes)
    records = [_source_record(root, path) for path in _inventory_paths(inventory_before)]
    revision_after = _git_output(root, ("rev-parse", "--verify", "HEAD")).rstrip(b"\n")
    index_after = _git_output(root, ("ls-files", "--stage", "-z", "--", *scopes))
    inventory_after = _source_inventory(root, scopes)
    if (revision_before, index_before, inventory_before) != (revision_after, index_after, inventory_after):
        raise RuntimeError("Repository changed while capturing source snapshot")

    return {
        "schema_version": 1,
        "repository": root.as_posix(),
        "scope_paths": scopes,
        "revision": revision_before.decode("ascii"),
        "index_sha256": hashlib.sha256(index_before).hexdigest(),
        "files": records,
    }


def _output_is_ignored(repository: Path, relative_path: str) -> bool:
    """Return whether Git excludes a prospective snapshot output path."""
    completed = subprocess.run(
        [
            "git",
            "-C",
            os.fspath(repository),
            "--literal-pathspecs",
            "check-ignore",
            "-q",
            "--no-index",
            "--",
            relative_path,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode not in {0, 1}:
        raise RuntimeError("Git ignored-path check failed for source snapshot output")
    return completed.returncode == 0


def _validate_snapshot_output(snapshot: dict[str, object], output: Path) -> None:
    """Reject an output that would appear in the captured source on recollection."""
    repository = Path(str(snapshot["repository"]))
    destination = output.resolve(strict=False)
    try:
        relative = destination.relative_to(repository).as_posix()
    except ValueError:
        return

    scope_paths = snapshot["scope_paths"]
    assert isinstance(scope_paths, list)
    selected = any(path == "." or relative == path or relative.startswith(f"{path}/") for path in scope_paths)
    if not selected:
        return
    files = snapshot["files"]
    assert isinstance(files, list)
    if any(record.get("path") == relative for record in files if isinstance(record, dict)):
        raise ValueError("Source snapshot output is already included in the selected source")
    if not _output_is_ignored(repository, relative):
        raise ValueError("Source snapshot output would become selected untracked source")


def _write_source_snapshot(snapshot: dict[str, object], output: Path) -> None:
    """Write one deterministic UTF-8 source snapshot JSON document."""
    _validate_snapshot_output(snapshot, output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8", newline="\n"
    )


def main() -> int:
    """Run the command-line diff collector."""
    arguments = parse_args()
    if arguments.snapshot:
        if arguments.repository is None:
            raise ValueError("--snapshot requires --repository")
        snapshot = capture_source_snapshot(arguments.repository, arguments.scope_path)
        _write_source_snapshot(snapshot, arguments.out)
        return 0
    return collect_diff(arguments.scope, arguments.target, arguments.out)


if __name__ == "__main__":
    sys.exit(main())
