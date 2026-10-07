#!/usr/bin/env python3
"""Collect a scope-aware local Git diff context pack without a shell wrapper.

## Purpose

Produce stable status, patch, file-list, stat, and source-snapshot evidence for skill artifacts before review or
implementation. Keeping these views in named files lets later workflow steps cite the exact local tree state used for a
review or implementation decision.

## Scope

It invokes local Git inspection for a working tree, path, commit, or explicit source snapshot; it does not fetch, push,
alter branches, or choose review findings. Local review mode creates a detached worktree and copies changed source into
it without editing the invoking checkout. The ``path`` scope compares ``HEAD`` for one path, while
``commit`` compares the requested revision and deliberately leaves ``untracked.txt`` empty. Source snapshots bind
current bytes to explicit safe repository-relative scopes.

## Usage

Run ``python collect_diff.py --scope <working-tree|path|commit> --out <directory>`` with ``--target`` for ``path`` or
``commit`` scopes. To serialize source evidence, run ``python collect_diff.py --snapshot --repository <path>
--scope-path <path> --out <snapshot.json>``. The diff output directory is created when needed; snapshot output is
canonical UTF-8 JSON and cannot be an included non-ignored source path. For local review, run ``--review-worktree
--repository <path> --out <directory>`` and later ``--verify-review-worktree --out <directory>`` before acceptance.

## Used by

Codex Rig workflow skills, implement/review artifact setup, and portable-helper acceptance tests use this collector.
Callers treat its files as local evidence and must not infer that an empty patch means the repository was clean unless
``status.txt`` agrees. Source consumers receive bytes encoded as UTF-8 text or base64; base64 preserves binary bytes but
does not make binary semantics reviewable as text.

## Outputs

It writes ``status.txt``, ``diff.patch``, ``files.txt``, ``diffstat.txt``, ``numstat.txt``, and ``untracked.txt`` under
the requested output directory. Snapshot mode writes one JSON object containing repository state plus selected source
records. Local review mode writes ``source-snapshot.json``, ``status.txt``, ``diff.patch``, ``staged.patch``, and
``review-worktree.json``. Invalid diff scope or a missing required target writes ``scope-error.txt`` with a stable
reason before returning exit code ``2``.

## Failure

Invalid scope/target, unsafe source scope, concurrent source-state change, or a failed local Git command exits non-zero
and leaves the caller with no claim that evidence is complete. Git output files may already exist when a later command
fails, so consumers must gate on the exit code rather than treating partial artifacts as a complete pack. A failed
local review mirror is retained for diagnosis; no worktree is automatically deleted.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath

#: Git object name of a SHA-1 (40 hex digits) or SHA-256 (64 hex digits) repository.
_REVISION_PATTERN = re.compile(rb"[0-9a-f]{40}(?:[0-9a-f]{24})?")

#: Output file name paired with the git diff arguments that produce it, one entry per diff artifact.
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
    parser.add_argument(
        "--review-worktree", action="store_true", help="Mirror current local review source into a detached worktree."
    )
    parser.add_argument(
        "--verify-review-worktree", action="store_true", help="Verify a previously collected review worktree."
    )
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
        completed = subprocess.run(["git", *arguments], stdout=stream, check=False)  # noqa: S603, S607 - argv list, no shell; tool resolved via PATH on purpose
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
    completed = subprocess.run(  # noqa: S603 - argv list, no shell
        ["git", "-C", os.fspath(repository), "--literal-pathspecs", *arguments],  # noqa: S607 - argv list, no shell; tool resolved via PATH on purpose
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"Git source snapshot command failed: {' '.join(arguments)}")
    return completed.stdout


#: Most Git processes one batch of independent reads keeps in flight at once.
_GIT_BATCH_WORKERS = 4


def _git_attempts(commands: list[tuple[Path, tuple[str, ...]]]) -> list[bytes | RuntimeError]:
    """Run independent read-only Git invocations concurrently, returning each outcome in request order.

    Process creation dominates these reads on Windows, so overlapping the waits shortens a batch without changing what
    any single invocation reads. A failed invocation is returned instead of raised so the caller can settle outcomes in
    the order its sequential checks always ran, keeping the first reported error unchanged.
    """

    def attempt(command: tuple[Path, tuple[str, ...]]) -> bytes | RuntimeError:
        """Return one invocation's stdout, or the error it would have raised."""
        try:
            return _git_output(*command)
        except RuntimeError as error:
            return error

    if len(commands) == 1:
        return [attempt(commands[0])]
    with ThreadPoolExecutor(max_workers=min(len(commands), _GIT_BATCH_WORKERS)) as pool:
        return list(pool.map(attempt, commands))


def _settled(outcome: bytes | RuntimeError) -> bytes:
    """Return a batched outcome's stdout, raising the error it recorded."""
    if isinstance(outcome, RuntimeError):
        raise outcome
    return outcome


def _git_outputs(repository: Path, argument_sets: list[tuple[str, ...]]) -> list[bytes]:
    """Return stdout from independent read-only Git invocations in one repository, raising the first failure in
    order."""
    return [_settled(outcome) for outcome in _git_attempts([(repository, arguments) for arguments in argument_sets])]


def _checkout_directory(repository: Path) -> Path:
    """Resolve a snapshot repository path that must exist and be a directory."""
    candidate = repository.resolve(strict=True)
    if not candidate.is_dir():
        raise ValueError(f"Snapshot repository is not a directory: {repository}")
    return candidate


def _repository_root(repository: Path) -> Path:
    """Resolve and verify the Git top-level directory for a snapshot repository."""
    candidate = _checkout_directory(repository)
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
    """Return HEAD, index, and non-ignored worktree names selected by literal scopes."""
    head, current = _git_outputs(
        repository,
        [
            ("ls-tree", "-r", "--name-only", "-z", "HEAD", "--", *scope_paths),
            ("ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", *scope_paths),
        ],
    )
    return head + current


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


def _repository_root_and_head(repository: Path) -> tuple[Path, bytes | None]:
    """Resolve the Git top-level directory and verified HEAD revision with a single Git call.

    The revision is ``None`` whenever the combined call fails or prints an unexpected shape; the top-level directory
    then comes from the separate lookup, so every failure is reported exactly as the individual calls report it.
    """
    candidate = repository.resolve(strict=True)
    if candidate.is_dir():
        with contextlib.suppress(RuntimeError):
            output = _git_output(candidate, ("rev-parse", "--show-toplevel", "--verify", "HEAD"))
            root, _, revision = output.rstrip(b"\n").rpartition(b"\n")
            if root and _REVISION_PATTERN.fullmatch(revision):
                return Path(os.fsdecode(root.rstrip(b"\n"))).resolve(strict=True), revision
    return _repository_root(repository), None


def _source_state(root: Path, scopes: list[str], revision: bytes | None) -> tuple[bytes, bytes, bytes]:
    """Read the HEAD revision, staged index, and file inventory for one snapshot pass with overlapped Git reads.

    A known ``revision`` is reused; ``None`` reads it as the first request so a failing HEAD lookup is still reported
    before any index or inventory failure.
    """
    requests = [
        ("ls-files", "--stage", "-z", "--", *scopes),
        ("ls-tree", "-r", "--name-only", "-z", "HEAD", "--", *scopes),
        ("ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", *scopes),
    ]
    if revision is None:
        requests.insert(0, ("rev-parse", "--verify", "HEAD"))
    outputs = _git_outputs(root, requests)
    if revision is None:
        revision = outputs.pop(0).rstrip(b"\n")
    index, head_names, current_names = outputs
    return revision, index, head_names + current_names


def capture_source_snapshot(repository: Path, scope_paths: list[str]) -> dict[str, object]:
    """Capture explicit current source bytes and Git state for a repository scope.

    The returned records include tracked, untracked non-ignored, and missing tracked paths. It compares the revision,
    index, selected file inventory, and selected bytes before and after reading source to reject concurrent changes.
    """
    root, head = _repository_root_and_head(repository)
    scopes = _normalize_scope_paths(root, scope_paths)
    revision_before, index_before, inventory_before = _source_state(root, scopes, head)
    records = [_source_record(root, path) for path in _inventory_paths(inventory_before)]
    revision_after, index_after, inventory_after = _source_state(root, scopes, None)
    records_after = [_source_record(root, path) for path in _inventory_paths(inventory_after)]
    if (revision_before, index_before, inventory_before, records) != (
        revision_after,
        index_after,
        inventory_after,
        records_after,
    ):
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
    completed = subprocess.run(  # noqa: S603 - argv list, no shell
        [  # noqa: S607 - argv list, no shell; tool resolved via PATH on purpose
            "git",
            "-C",
            os.fspath(repository),
            "check-ignore",
            "-q",
            "--no-index",
            "--",
            relative_path,
        ],
        capture_output=True,
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
    if not isinstance(scope_paths, list):
        raise TypeError(f"scope_paths must be list, got {type(scope_paths).__name__}")
    selected = any(path == "." or relative == path or relative.startswith(f"{path}/") for path in scope_paths)
    if not selected:
        return
    files = snapshot["files"]
    if not isinstance(files, list):
        raise TypeError(f"files must be list, got {type(files).__name__}")
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


def _require_ignored_inside_repository(repository: Path, destination: Path) -> None:
    """Keep collector artifacts from becoming new source in the invoking checkout."""
    try:
        relative = destination.resolve(strict=False).relative_to(repository).as_posix()
    except ValueError:
        return
    if not _output_is_ignored(repository, relative):
        raise ValueError(f"Review output inside repository must be ignored: {destination}")


def _materialize_source(review: Path, snapshot: dict[str, object], untracked_paths: set[str]) -> None:
    """Restore captured file bytes and untracked entries after Git applies the local patch."""
    records = snapshot["files"]
    if not isinstance(records, list):
        raise TypeError(f"records must be list, got {type(records).__name__}")
    for record in records:
        if not isinstance(record, dict):
            raise TypeError(f"record must be dict, got {type(record).__name__}")
        relative = record["path"]
        # Git may convert tracked line endings; the snapshot must retain the caller's raw bytes.
        # Tracked symlinks and deletions remain Git's responsibility and are verified afterward.
        if relative not in untracked_paths and record["kind"] != "file":
            continue
        destination = review / Path(*PurePosixPath(relative).parts)
        if not destination.parent.resolve().is_relative_to(review):
            raise RuntimeError(f"Review source path escapes worktree: {relative}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.parent.resolve().is_relative_to(review):
            raise RuntimeError(f"Review source path escapes worktree: {relative}")
        content = record["content"]
        if not isinstance(content, str):
            raise TypeError(f"content must be str, got {type(content).__name__}")
        source = content.encode("utf-8") if record["encoding"] == "utf-8" else base64.b64decode(content)
        if record["kind"] == "symlink":
            destination.symlink_to(os.fsdecode(source))
        elif record["kind"] == "file":
            if destination.is_symlink():
                raise RuntimeError(f"Review source file became a symlink: {relative}")
            destination.write_bytes(source)
            if record["executable"]:
                destination.chmod(destination.stat().st_mode | stat.S_IXUSR)
        else:
            raise RuntimeError(f"Untracked review entry is not a file: {relative}")


def collect_review_worktree(repository: Path, output: Path) -> dict[str, object]:
    """Mirror changed local source into a detached worktree and certify caller stability.

    The invoking checkout remains untouched. A failure leaves the detached worktree for diagnosis rather than deleting
    potentially modified source.
    """
    root = _repository_root(repository)
    report = output.resolve(strict=False)
    review = report.with_name(f"{report.name}-review-worktree")
    for destination in (report, review):
        _require_ignored_inside_repository(root, destination)
    if review.exists() or review.is_symlink():
        raise ValueError(f"Review worktree path is occupied: {review}")

    status_before = _git_output(root, ("status", "--porcelain", "-z", "--untracked-files=all"))
    tracked = _git_output(root, ("diff", "--name-only", "-z", "HEAD", "--"))
    staged = _git_output(root, ("diff", "--cached", "--name-only", "-z", "HEAD", "--"))
    untracked = _git_output(root, ("ls-files", "--others", "--exclude-standard", "-z"))
    paths = _inventory_paths(tracked + staged + untracked)
    if not paths:
        raise ValueError("Local review worktree requires changed source paths")
    snapshot = capture_source_snapshot(root, paths)
    patch = _git_output(root, ("diff", "--binary", "HEAD", "--"))
    staged_patch = _git_output(root, ("diff", "--cached", "--binary", "HEAD", "--"))
    if status_before != _git_output(root, ("status", "--porcelain", "-z", "--untracked-files=all")):
        raise RuntimeError("Repository changed while preparing local review worktree")
    if snapshot != capture_source_snapshot(root, paths):
        raise RuntimeError("Repository changed while preparing local review worktree")

    completed = subprocess.run(  # noqa: S603 - argv list, no shell
        ["git", "-C", os.fspath(root), "worktree", "add", "--detach", os.fspath(review), str(snapshot["revision"])],  # noqa: S607 - argv list, no shell; tool resolved via PATH on purpose
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("Git could not create detached local review worktree")
    if patch:
        completed = subprocess.run(  # noqa: S603 - argv list, no shell
            ["git", "-C", os.fspath(review), "apply", "--binary", "--"],  # noqa: S607 - argv list, no shell; tool resolved via PATH on purpose
            input=patch,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError("Git could not apply local review patch")
    _materialize_source(review, snapshot, set(_inventory_paths(untracked)))

    mirror = capture_source_snapshot(review, paths)
    if mirror["revision"] != snapshot["revision"] or mirror["files"] != snapshot["files"]:
        raise RuntimeError("Detached review worktree source differs from captured local source")
    if patch != _git_output(review, ("diff", "--binary", "HEAD", "--")):
        raise RuntimeError("Detached review worktree patch differs from captured local patch")
    if status_before != _git_output(root, ("status", "--porcelain", "-z", "--untracked-files=all")):
        raise RuntimeError("Repository changed while creating local review worktree")
    if snapshot != capture_source_snapshot(root, paths):
        raise RuntimeError("Repository changed while creating local review worktree")

    report.mkdir(parents=True, exist_ok=True)
    _write_source_snapshot(snapshot, report / "source-snapshot.json")
    (report / "status.txt").write_bytes(status_before)
    (report / "diff.patch").write_bytes(patch)
    (report / "staged.patch").write_bytes(staged_patch)
    receipt: dict[str, object] = {
        "schema_version": 1,
        "source_worktree": root.as_posix(),
        "review_worktree": review.as_posix(),
        "revision": snapshot["revision"],
        "scope_paths": paths,
        "index_sha256": snapshot["index_sha256"],
        "diff_sha256": hashlib.sha256(patch).hexdigest(),
        "status_sha256": hashlib.sha256(status_before).hexdigest(),
        "review_status_sha256": hashlib.sha256(
            _git_output(review, ("status", "--porcelain", "-z", "--untracked-files=all"))
        ).hexdigest(),
    }
    (report / "review-worktree.json").write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    return receipt


def verify_review_worktree(output: Path) -> None:
    """Reject source drift from the recorded detached local review worktree."""
    report = output.resolve(strict=True)
    receipt = json.loads((report / "review-worktree.json").read_text(encoding="utf-8"))
    if receipt.get("schema_version") != 1:
        raise ValueError("Unsupported review worktree receipt schema")
    snapshot = json.loads((report / "source-snapshot.json").read_text(encoding="utf-8"))
    review = Path(receipt["review_worktree"])
    source = Path(receipt["source_worktree"])
    if review.resolve() != report.with_name(f"{report.name}-review-worktree") or review.resolve() == source.resolve():
        raise RuntimeError("Review worktree path no longer matches receipt")
    # The four identity probes are independent; outcomes are settled in the original check order below.
    topology = _git_attempts(
        [
            (_checkout_directory(review), ("rev-parse", "--show-toplevel")),
            (source, ("rev-parse", "--git-common-dir")),
            (review, ("rev-parse", "--git-common-dir")),
            (review, ("branch", "--show-current")),
        ]
    )
    if Path(os.fsdecode(_settled(topology[0]).rstrip(b"\n"))).resolve(strict=True) != review.resolve():
        raise RuntimeError("Review worktree path is not a checkout root")
    source_common = (source / os.fsdecode(_settled(topology[1]).strip())).resolve()
    review_common = (review / os.fsdecode(_settled(topology[2]).strip())).resolve()
    if source_common != review_common:
        raise RuntimeError("Review worktree repository no longer matches source")
    if _settled(topology[3]).strip():
        raise RuntimeError("Review worktree is no longer detached")
    current = capture_source_snapshot(review, receipt["scope_paths"])
    if current["revision"] != receipt["revision"] or current["files"] != snapshot["files"]:
        raise RuntimeError("Review worktree source changed after collection")
    status, patch = _git_outputs(
        review, [("status", "--porcelain", "-z", "--untracked-files=all"), ("diff", "--binary", "HEAD", "--")]
    )
    if hashlib.sha256(status).hexdigest() != receipt["review_status_sha256"]:
        raise RuntimeError("Review worktree status changed after collection")
    if hashlib.sha256(patch).hexdigest() != receipt["diff_sha256"]:
        raise RuntimeError("Review worktree patch changed after collection")


def main() -> int:
    """Run the command-line diff collector."""
    arguments = parse_args()
    if arguments.verify_review_worktree:
        if (
            arguments.review_worktree
            or arguments.snapshot
            or arguments.repository
            or arguments.target
            or arguments.scope_path
        ):
            raise ValueError("--verify-review-worktree only accepts --out")
        verify_review_worktree(arguments.out)
        return 0
    if arguments.review_worktree:
        if (
            arguments.snapshot
            or arguments.repository is None
            or arguments.scope != "working-tree"
            or arguments.target
            or arguments.scope_path
        ):
            raise ValueError(
                "--review-worktree requires --repository and working-tree scope without --target or --snapshot"
            )
        collect_review_worktree(arguments.repository, arguments.out)
        return 0
    if arguments.snapshot:
        if arguments.repository is None:
            raise ValueError("--snapshot requires --repository")
        snapshot = capture_source_snapshot(arguments.repository, arguments.scope_path)
        _write_source_snapshot(snapshot, arguments.out)
        return 0
    return collect_diff(arguments.scope, arguments.target, arguments.out)


if __name__ == "__main__":
    sys.exit(main())
