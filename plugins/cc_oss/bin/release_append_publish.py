"""Publish validated release notes candidates without replaying partial writes.

Purpose: Stage release artifacts and finish an interrupted multi-file promotion. Scope: Local plain and append notes
artifacts in one repository working tree. Usage: ``release_append_publish.py check|begin|seal|publish|recover`` from the
repository root. Outputs: Candidate directory, live release files, and a temporary recovery journal. Failure: Refuses
changed source, live bytes, candidates, symlinks, or pending promotion. Used by: ``skills/release/SKILL.md`` and
``modes/release-draft-template.md``.

The journal records exact old and candidate digests before the first replacement. The stage retains exact original bytes
for interrupted promotion recovery. Source drift switches the journal to rollback; recovery resumes that decision after
a crash. Unknown live edits stop restoration. The marker publishes last so an interrupted pass cannot make an incomplete
release look processed. Plain notes may select dated output paths and a range endpoint before the gathered HEAD; both
are sealed with the same candidate inventory and recovered together. On POSIX, staged bytes and directory entries are
synced before the journal, and each live replacement is synced before promotion continues. Native Windows lacks portable
directory fsync, so crash durability there is not guaranteed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PureWindowsPath
from urllib.parse import quote

from release_append_marker import refuse_legacy, state_relative, unique_root_commit


ARTIFACTS = ("DRAFT.md", "SUMMARY.md", "MIGRATION.md")


class SourceDriftError(ValueError):
    """Identify a changed gathered source that requires transaction rollback."""


def _safe_branch(branch: str) -> str:
    """Reject a branch name that could escape the journal filename."""
    if (
        not branch
        or ".." in branch
        or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.+%" for char in branch)
    ):
        raise ValueError("branch must be a nonempty filename-safe slug")
    return branch


def _expected_paths(branch_ref: str, changelog: str, extra_outputs: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Enumerate a candidate's validated live release destinations."""
    _validate_relative(changelog)
    if Path(changelog).name != "CHANGELOG.md" and not Path(changelog).name.startswith("CHANGELOG"):
        raise ValueError("changelog path must name a CHANGELOG file")
    for output in extra_outputs:
        _validate_relative(output)
        if not output.startswith(".temp/output-release-") or not output.endswith(".md"):
            raise ValueError(f"unexpected plain notes output: {output}")
    paths = (
        *ARTIFACTS,
        changelog,
        *extra_outputs,
        state_relative(branch_ref, "provenance.json"),
        state_relative(branch_ref, "marker"),
    )
    if len(set(paths)) != len(paths):
        raise ValueError("duplicate release artifact destination")
    return paths


def _candidate_paths(metadata: dict[str, object]) -> tuple[str, ...]:
    """Reconstruct and validate the sealed destination inventory on recovery."""
    branch_ref = metadata.get("branch_ref")
    changelog = metadata.get("changelog")
    outputs = metadata.get("extra_outputs", [])
    if not isinstance(branch_ref, str) or not isinstance(changelog, str) or not isinstance(outputs, list):
        raise ValueError("invalid candidate artifact inventory")
    if any(not isinstance(output, str) for output in outputs):
        raise ValueError("invalid candidate artifact inventory")
    return _expected_paths(branch_ref, changelog, tuple(outputs))


def _candidate_marker_sha(metadata: dict[str, object]) -> str:
    """Read the completed range endpoint, including older append candidates."""
    marker_sha = metadata.get("marker_sha", metadata.get("head_sha"))
    if not isinstance(marker_sha, str) or not marker_sha:
        raise ValueError("invalid candidate marker endpoint")
    return marker_sha


def _checked_range_start(root: Path, start: str, endpoint: str) -> str:
    """Resolve the selected start and require it to precede the marker endpoint."""
    resolved = subprocess.run(
        ["git", "rev-parse", "--verify", "--end-of-options", f"{start}^{{commit}}"],
        cwd=root,
        capture_output=True,
        check=False,
        timeout=5,
    )
    if resolved.returncode != 0:
        raise ValueError("completed range start is unresolved")
    start_sha = resolved.stdout.decode().strip()
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", "--end-of-options", start_sha, endpoint],
        cwd=root,
        capture_output=True,
        check=False,
        timeout=5,
    )
    if ancestor.returncode != 0:
        raise ValueError("completed range start is not an ancestor of endpoint")
    return start_sha


def _current_marker_baseline(root: Path, branch_ref: str, head: str, endpoint: str, last_tag: str) -> str | None:
    """Select the saved marker or its later selected release-tag baseline."""
    marker = _checked_path(root, state_relative(branch_ref, "marker"))
    saved = marker.read_text(encoding="utf-8").strip() if marker.is_file() else ""
    try:
        saved_sha = _checked_range_start(root, saved, head) if saved else None
    except ValueError:
        saved_sha = None
    tag_sha = _checked_range_start(root, last_tag, head) if last_tag else None
    baseline = saved_sha
    if saved_sha and tag_sha:
        try:
            _checked_range_start(root, saved_sha, tag_sha)
        except ValueError:
            pass
        else:
            baseline = tag_sha
    baseline = baseline or tag_sha or unique_root_commit(root, "git")
    if baseline is not None:
        _checked_range_start(root, baseline, endpoint)
    return baseline


def journal_path(root: Path, branch_ref: str) -> Path:
    """Return the per-branch pending-promotion journal path."""
    return root / state_relative(branch_ref, "journal.json")


def _validate_relative(relative: str) -> Path:
    """Reject POSIX and Windows paths that can escape a repository root."""
    path = Path(relative)
    windows = PureWindowsPath(relative)
    if (
        path.is_absolute()
        or windows.root
        or windows.drive
        or "\\" in relative
        or not path.parts
        or any(part in (".", "..") for part in path.parts)
    ):
        raise ValueError(f"unsafe artifact path: {relative}")
    return path


def _checked_path(root: Path, relative: str) -> Path:
    """Resolve an artifact inside the repository without following symlinks."""
    path = _validate_relative(relative)
    target = root / path
    if any(part.is_symlink() for part in (root / Path(*path.parts[:index]) for index in range(1, len(path.parts) + 1))):
        raise ValueError(f"symlink artifact path: {relative}")
    return target


def _digest(path: Path) -> str | None:
    """Hash exact file bytes, or return None when the file is absent."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


def _head_sha(root: Path) -> str:
    """Read the exact commit whose release claims may be published."""
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD^{commit}"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise ValueError("cannot resolve release HEAD")
    return result.stdout.strip()


def _branch_ref(root: Path) -> str:
    """Read the active branch without substituting a name for detached HEAD."""
    result = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise ValueError("cannot resolve release branch")
    return result.stdout.strip()


def _tag_state(root: Path) -> bytes:
    """Read exact local tag refs so a new or retargeted tag invalidates the baseline."""
    result = subprocess.run(
        ["git", "for-each-ref", "--sort=refname", "--format=%(refname) %(objectname)", "refs/tags"],
        cwd=root,
        capture_output=True,
        check=False,
        timeout=5,
    )
    if result.returncode != 0:
        raise ValueError("cannot resolve release tag refs")
    return result.stdout


def _check_source(root: Path, branch: str, head_sha: str, branch_ref: str, tag_state_sha256: str) -> None:
    """Refuse a changed release source before staging or promoting its marker."""
    try:
        current_head = _head_sha(root)
        current_branch = _branch_ref(root)
        current_tags = _tag_state(root)
    except ValueError as error:
        raise SourceDriftError(str(error)) from error
    if current_head != head_sha:
        raise SourceDriftError("HEAD changed after release gather")
    if current_branch != branch_ref or quote(branch_ref.replace("/", "-"), safe="-_.+") != branch:
        raise SourceDriftError("branch changed after release gather")
    refuse_legacy(root, branch_ref)
    if hashlib.sha256(current_tags).hexdigest() != tag_state_sha256:
        raise SourceDriftError("release baseline tags changed after gather")


def _sync_file(path: Path) -> None:
    """Flush a closed candidate or live file before relying on its bytes after a crash."""
    # Windows FlushFileBuffers requires a writable file handle even when no bytes change.
    with path.open("r+b" if sys.platform == "win32" else "rb") as stream:
        os.fsync(stream.fileno())


def _shell_path(path: Path) -> str:
    """Print a candidate path that Git Bash can pass between release blocks."""
    if sys.platform != "win32":
        return str(path)
    converted = subprocess.run(["cygpath", "-u", str(path)], capture_output=True, text=True, check=True, timeout=5)
    return converted.stdout.strip()


def _sync_directories(paths: set[Path]) -> None:
    """Persist directory entries on POSIX where directory fsync is available."""
    if os.name == "nt":
        return
    for path in sorted(paths, key=lambda item: len(item.parts), reverse=True):
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _parent_directories(path: Path, root: Path) -> set[Path]:
    """Include every directory entry needed to find a staged or live file."""
    directories: set[Path] = set()
    parent = path.parent
    while parent != root:
        directories.add(parent)
        parent = parent.parent
    directories.add(root)
    return directories


def _sync_inventory(root: Path, files: list[Path]) -> None:
    """Persist inventoried bytes and their directory entries before a journal transition."""
    directories: set[Path] = set()
    for path in files:
        if path.exists():
            _sync_file(path)
            directories.update(_parent_directories(path, root))
    _sync_directories(directories)


def _write_json(path: Path, value: dict[str, object], root: Path | None = None) -> None:
    """Persist a journal before changing any live artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=path.parent, delete=False) as stream:
        json.dump(value, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
        temporary = Path(stream.name)
    os.replace(temporary, path)
    _sync_directories(_parent_directories(path, root or path.parent))


def begin(
    root: Path,
    branch: str,
    changelog: str,
    expected_head: str | None = None,
    expected_start: str | None = None,
    expected_branch_ref: str | None = None,
    expected_tag_state: bytes | None = None,
    marker_sha: str | None = None,
    extra_outputs: tuple[str, ...] = (),
    *,
    range_start: str,
    last_tag: str = "",
) -> Path:
    """Stage notes inputs only when gathered source identity remains current."""
    root = root.resolve()
    branch = _safe_branch(branch)
    _validate_relative(changelog)
    head_sha = _head_sha(root)
    if expected_head is not None and expected_head != head_sha:
        raise ValueError("HEAD changed after release gather")
    if marker_sha is not None:
        marker_commit = subprocess.run(
            ["git", "merge-base", "--is-ancestor", marker_sha, head_sha],
            cwd=root,
            capture_output=True,
            check=False,
            timeout=5,
        )
        if marker_commit.returncode != 0:
            raise ValueError("completed range endpoint is not on gathered HEAD")
    branch_ref = _branch_ref(root)
    if quote(branch_ref.replace("/", "-"), safe="-_.+") != branch or (
        expected_branch_ref is not None and branch_ref != expected_branch_ref
    ):
        raise ValueError("branch changed after release gather")
    refuse_legacy(root, branch_ref)
    tag_state = _tag_state(root)
    if expected_tag_state is not None and tag_state != expected_tag_state:
        raise ValueError("release baseline tags changed after gather")
    if expected_start is not None:
        marker = _checked_path(root, state_relative(branch_ref, "marker"))
        if marker.read_text(encoding="utf-8").strip() != expected_start:
            raise ValueError("saved marker changed after release gather")
        marker_continuity = subprocess.run(
            ["git", "merge-base", "--is-ancestor", expected_start, marker_sha or head_sha],
            cwd=root,
            capture_output=True,
            check=False,
            timeout=5,
        )
        if marker_continuity.returncode != 0:
            raise ValueError("completed range endpoint is before saved marker")
    range_start_sha = _checked_range_start(root, range_start, marker_sha or head_sha)
    saved_baseline = _current_marker_baseline(root, branch_ref, head_sha, marker_sha or head_sha, last_tag)
    if saved_baseline is not None:
        _checked_range_start(root, range_start_sha, saved_baseline)
    if journal_path(root, branch_ref).exists():
        raise ValueError("pending append publication; recover before starting another append")
    _checked_path(root, changelog)
    paths = _expected_paths(branch_ref, changelog, extra_outputs)
    _checked_path(root, ".temp").mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="release-append-stage-", dir=root / ".temp"))
    originals: dict[str, str | None] = {}
    for relative in paths:
        source = _checked_path(root, relative)
        originals[relative] = _digest(source)
        if source.exists():
            destination = stage / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            original = stage / "originals" / relative
            original.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, original)
            if _digest(destination) != originals[relative] or _digest(original) != originals[relative]:
                raise ValueError(f"artifact changed during append staging: {relative}")
    _write_json(
        stage / "candidate.json",
        {
            "branch": branch,
            "changelog": changelog,
            "head_sha": head_sha,
            "branch_ref": branch_ref,
            "tag_state_sha256": hashlib.sha256(tag_state).hexdigest(),
            "start_sha": expected_start,
            "range_start_sha": range_start_sha,
            "last_tag": last_tag,
            "baseline_sha": saved_baseline,
            "marker_sha": marker_sha or head_sha,
            "extra_outputs": list(extra_outputs),
            "originals": originals,
        },
    )
    return stage


def seal(root: Path, branch: str, stage: Path) -> None:
    """Seal candidate bytes while the gathered commit, branch, and tags still match."""
    root = root.resolve()
    branch = _safe_branch(branch)
    stage = stage.resolve()
    if stage.parent != root / ".temp":
        raise ValueError("append candidate cannot be sealed")
    metadata_path = stage / "candidate.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    changelog = metadata.get("changelog")
    if metadata.get("branch") != branch or not isinstance(changelog, str):
        raise ValueError("candidate metadata does not match branch")
    _check_source(root, branch, metadata["head_sha"], metadata["branch_ref"], metadata["tag_state_sha256"])
    start_sha = metadata.get("range_start_sha")
    if (
        not isinstance(start_sha, str)
        or _checked_range_start(root, start_sha, _candidate_marker_sha(metadata)) != start_sha
    ):
        raise ValueError("candidate completed range start changed")
    baseline = _current_marker_baseline(
        root,
        metadata["branch_ref"],
        metadata["head_sha"],
        _candidate_marker_sha(metadata),
        metadata.get("last_tag", ""),
    )
    if baseline != metadata.get("baseline_sha") or (
        baseline and _checked_range_start(root, start_sha, baseline) != start_sha
    ):
        raise ValueError("candidate completed range baseline changed")
    if journal_path(root, metadata["branch_ref"]).exists():
        raise ValueError("append candidate cannot be sealed")
    expected = set(_candidate_paths(metadata))
    if set(metadata.get("originals", {})) != expected:
        raise ValueError("candidate artifact inventory changed")
    marker = state_relative(metadata["branch_ref"], "marker")
    if _digest(_checked_path(root, marker)) != metadata["originals"][marker]:
        raise ValueError("saved marker changed after release gather")
    if _checked_path(stage, marker).read_text(encoding="utf-8") != _candidate_marker_sha(metadata) + "\n":
        raise ValueError("candidate marker does not match completed range endpoint")
    metadata["sealed"] = {relative: _digest(_checked_path(stage, relative)) for relative in expected}
    _write_json(metadata_path, metadata)


def _replace_candidate(root: Path, stage: Path, relative: str) -> None:
    """Replace one verified artifact using a same-directory temporary file."""
    source = _checked_path(stage, relative)
    target = _checked_path(root, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
        temporary = Path(stream.name)
    try:
        shutil.copy2(source, temporary)
        _sync_file(temporary)
        os.replace(temporary, target)
        _sync_directories(_parent_directories(target, root))
    finally:
        temporary.unlink(missing_ok=True)


def _promote(root: Path, branch: str, journal: dict[str, object], require_head: bool = False) -> None:
    """Finish a journaled promotion or rollback without replacing unknown live bytes."""
    stage = Path(str(journal["stage"]))
    if stage.parent != root / ".temp":
        raise ValueError("candidate directory outside repository staging area")
    entries = journal["entries"]
    marker = state_relative(str(journal["branch_ref"]), "marker")
    if (
        not isinstance(entries, list)
        or not entries
        or not isinstance(entries[-1], dict)
        or (
            any(entry.get("path") == marker for entry in entries if isinstance(entry, dict))
            and entries[-1].get("path") != marker
        )
        or journal.get("mode", "promote") not in ("promote", "rollback")
    ):
        raise ValueError("invalid append journal")
    metadata = json.loads((stage / "candidate.json").read_text(encoding="utf-8"))
    changelog = metadata.get("changelog")
    head_sha = metadata.get("head_sha")
    if (
        not isinstance(changelog, str)
        or not isinstance(head_sha, str)
        or metadata.get("branch") != branch
        or journal.get("changelog") != changelog
        or journal.get("head_sha") != head_sha
        or journal.get("branch_ref") != metadata.get("branch_ref")
        or journal.get("tag_state_sha256") != metadata.get("tag_state_sha256")
        or journal.get("start_sha") != metadata.get("start_sha")
        or journal.get("marker_sha", head_sha) != metadata.get("marker_sha", head_sha)
        or journal.get("extra_outputs", []) != metadata.get("extra_outputs", [])
    ):
        raise ValueError("append journal does not match candidate metadata")
    expected = set(_candidate_paths(metadata))
    originals = metadata.get("originals")
    if not isinstance(originals, dict) or set(originals) != expected:
        raise ValueError("candidate artifact inventory changed")
    for relative, digest in originals.items():
        original = _checked_path(stage / "originals", relative)
        if _digest(original) != digest:
            raise ValueError(f"original artifact backup changed: {relative}")
    sealed = metadata.get("sealed")
    if not isinstance(sealed, dict) or set(sealed) != expected or journal.get("sealed") != sealed:
        raise ValueError("append journal does not match truth seal")
    candidates: dict[str, str | None] = {}
    for relative in expected:
        candidate = _digest(_checked_path(stage, relative))
        if candidate is None and originals[relative] is not None:
            raise ValueError(f"candidate deleted artifact: {relative}")
        candidates[relative] = candidate
        if candidate != sealed[relative]:
            raise ValueError(f"candidate changed after truth gate: {relative}")
    changed = {relative for relative in expected if candidates[relative] != originals[relative]}
    if _checked_path(stage, marker).read_text(encoding="utf-8") != _candidate_marker_sha(metadata) + "\n":
        raise ValueError("candidate marker does not match completed range endpoint")
    seen: set[str] = set()
    # Validate the journal before recognizing an already completed live inventory.
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("invalid append journal entry")
        relative = str(entry["path"])
        if relative not in changed or relative in seen or entry["old"] != originals[relative]:
            raise ValueError("journal contains duplicate or unexpected artifact")
        seen.add(relative)
        candidate = entry["new"]
        staged = _checked_path(stage, relative)
        if candidates[relative] != candidate or _digest(staged) != candidate:
            raise ValueError(f"candidate changed after validation: {relative}")
    if seen != changed:
        raise ValueError("append journal disagrees with candidate changes")
    if journal.get("mode") == "promote" and all(
        _digest(_checked_path(root, relative)) == sealed[relative] for relative in expected
    ):
        # A restored journal after its final unlink already certifies these exact live bytes.
        _sync_inventory(root, [_checked_path(root, relative) for relative in expected])
        journal_file = journal_path(root, metadata["branch_ref"])
        journal_file.unlink()
        _sync_directories(_parent_directories(journal_file, root))
        return
    if require_head:
        # Incomplete publication chooses durable rollback on drift before foreign-edit preflight.
        _check_source(root, branch, head_sha, metadata["branch_ref"], metadata["tag_state_sha256"])
    for entry in entries:
        relative = str(entry["path"])
        current = _digest(_checked_path(root, relative))
        if current not in (entry["old"], entry["new"]):
            raise ValueError(f"artifact changed outside append publication: {relative}")
    for relative in expected - changed:
        if _digest(_checked_path(root, relative)) != originals[relative]:
            raise ValueError(f"artifact changed outside append publication: {relative}")
    if journal.get("mode") == "rollback":
        # Restore only exact candidate bytes; another writer's edit must survive and keep the journal.
        for entry in reversed(entries):
            relative = str(entry["path"])
            live = _checked_path(root, relative)
            current = _digest(live)
            if current == entry["old"]:
                continue
            if current != entry["new"]:
                raise ValueError(f"artifact changed outside append publication: {relative}")
            if entry["old"] is None:
                live.unlink()
                _sync_directories(_parent_directories(live, root))
            else:
                _replace_candidate(root, stage / "originals", relative)
        for relative in expected:
            if _digest(_checked_path(root, relative)) != originals[relative]:
                raise ValueError(f"artifact changed outside append rollback: {relative}")
        _sync_inventory(root, [_checked_path(root, relative) for relative in expected])
        journal_file = journal_path(root, metadata["branch_ref"])
        journal_file.unlink()
        _sync_directories(_parent_directories(journal_file, root))
        return
    for entry in entries:
        relative = str(entry["path"])
        candidate = entry["new"]
        staged = _checked_path(stage, relative)
        live = _checked_path(root, relative)
        # The marker may already have candidate bytes, so source checks cannot depend on replacing it.
        if require_head:
            _check_source(root, branch, head_sha, metadata["branch_ref"], metadata["tag_state_sha256"])
        # A prior replacement may have been edited while later files were promoted.
        for prior in entries:
            prior_path = str(prior["path"])
            if prior_path == relative:
                break
            if _digest(_checked_path(root, prior_path)) != prior["new"]:
                raise ValueError(f"artifact changed outside append publication: {prior_path}")
        if _digest(live) == candidate:
            continue
        if _digest(live) != entry["old"]:
            raise ValueError(f"artifact changed outside append publication: {relative}")
        if relative == marker:
            for candidate_path in expected:
                if _digest(_checked_path(stage, candidate_path)) != sealed[candidate_path]:
                    raise ValueError(f"candidate changed after truth gate: {candidate_path}")
            for unchanged in expected - changed:
                if _digest(_checked_path(root, unchanged)) != originals[unchanged]:
                    raise ValueError(f"artifact changed outside append publication: {unchanged}")
            for prior in entries[:-1]:
                prior_path = str(prior["path"])
                if _digest(_checked_path(root, prior_path)) != prior["new"]:
                    raise ValueError(f"artifact changed outside append publication: {prior_path}")
        _replace_candidate(root, stage, relative)
    for relative in expected:
        if _digest(_checked_path(stage, relative)) != sealed[relative]:
            raise ValueError(f"candidate changed after truth gate: {relative}")
    for relative in expected:
        if _digest(_checked_path(root, relative)) != sealed[relative]:
            raise ValueError(f"artifact changed outside append publication: {relative}")
    if require_head:
        _check_source(root, branch, head_sha, metadata["branch_ref"], metadata["tag_state_sha256"])
    _sync_inventory(root, [_checked_path(root, relative) for relative in expected])
    journal_file = journal_path(root, metadata["branch_ref"])
    journal_file.unlink()
    _sync_directories(_parent_directories(journal_file, root))


def _finish(root: Path, branch: str, journal: dict[str, object]) -> None:
    """Switch a source-drifted promotion to durable, resumable rollback."""
    if journal.get("mode") == "rollback":
        _promote(root, branch, journal)
        print("Recovered interrupted append rollback; original artifacts restored.", file=sys.stderr)
        return
    head_sha = journal.get("head_sha")
    branch_ref = journal.get("branch_ref")
    tag_state_sha256 = journal.get("tag_state_sha256")
    if not all(isinstance(value, str) for value in (head_sha, branch_ref, tag_state_sha256)):
        raise ValueError("invalid append journal source")
    try:
        _promote(root, branch, journal, require_head=True)
    except SourceDriftError:
        journal["mode"] = "rollback"
        _write_json(journal_path(root, str(journal["branch_ref"])), journal, root)
        _promote(root, branch, journal)
        print("Append publication rolled back after source drift; original artifacts restored.", file=sys.stderr)
        raise


def publish(root: Path, branch: str, stage: Path) -> None:
    """Journal and promote a truth-checked candidate when its source still matches."""
    root = root.resolve()
    branch = _safe_branch(branch)
    stage = stage.resolve()
    if stage.parent != root / ".temp":
        raise ValueError("candidate directory outside repository staging area")
    metadata = json.loads((stage / "candidate.json").read_text(encoding="utf-8"))
    changelog = metadata.get("changelog")
    head_sha = metadata.get("head_sha")
    if (
        not isinstance(changelog, str)
        or not isinstance(head_sha, str)
        or metadata.get("branch") != branch
        or not isinstance(metadata.get("originals"), dict)
    ):
        raise ValueError("candidate metadata does not match branch")
    if journal_path(root, metadata["branch_ref"]).exists():
        raise ValueError("pending append publication; recover first")
    if set(metadata["originals"]) != set(_candidate_paths(metadata)):
        raise ValueError("candidate artifact inventory changed")
    sealed = metadata.get("sealed")
    if not isinstance(sealed, dict) or set(sealed) != set(metadata["originals"]):
        raise ValueError("append candidate has no complete truth seal")
    if metadata.get("start_sha") is not None:
        marker_file = _checked_path(root, state_relative(metadata["branch_ref"], "marker"))
        if marker_file.read_text(encoding="utf-8").strip() != metadata["start_sha"]:
            raise ValueError("saved marker changed after release gather")
    _check_source(root, branch, head_sha, metadata["branch_ref"], metadata["tag_state_sha256"])
    start_sha = metadata.get("range_start_sha")
    if (
        not isinstance(start_sha, str)
        or _checked_range_start(root, start_sha, _candidate_marker_sha(metadata)) != start_sha
    ):
        raise ValueError("candidate completed range start changed")
    baseline = _current_marker_baseline(
        root, metadata["branch_ref"], head_sha, _candidate_marker_sha(metadata), metadata.get("last_tag", "")
    )
    if baseline != metadata.get("baseline_sha") or (
        baseline and _checked_range_start(root, start_sha, baseline) != start_sha
    ):
        raise ValueError("candidate completed range baseline changed")
    marker = state_relative(metadata["branch_ref"], "marker")
    if _digest(_checked_path(root, marker)) != metadata["originals"][marker]:
        raise ValueError("saved marker changed after release gather")
    entries: list[dict[str, str | None]] = []
    for relative, original in metadata["originals"].items():
        live = _checked_path(root, relative)
        staged = _checked_path(stage, relative)
        candidate = _digest(staged)
        if candidate != sealed[relative]:
            raise ValueError(f"candidate changed after truth gate: {relative}")
        if _digest(live) != original:
            raise ValueError(f"artifact changed outside append staging: {relative}")
        if candidate == original:
            continue
        if candidate is None:
            raise ValueError(f"candidate deleted artifact: {relative}")
        entries.append({"path": relative, "old": original, "new": candidate})
    if not entries:
        return
    entries.sort(key=lambda entry: entry["path"] == marker)
    journal = {
        "mode": "promote",
        "stage": str(stage),
        "changelog": changelog,
        "head_sha": head_sha,
        "branch_ref": metadata["branch_ref"],
        "tag_state_sha256": metadata["tag_state_sha256"],
        "start_sha": metadata.get("start_sha"),
        "marker_sha": metadata.get("marker_sha", head_sha),
        "extra_outputs": metadata.get("extra_outputs", []),
        "sealed": sealed,
        "entries": entries,
    }
    stage_files = [stage / "candidate.json"]
    for relative in metadata["originals"]:
        stage_files.extend((_checked_path(stage, relative), _checked_path(stage / "originals", relative)))
    _sync_inventory(root, stage_files)
    _write_json(journal_path(root, metadata["branch_ref"]), journal, root)
    _finish(root, branch, journal)


def recover(root: Path, branch: str) -> None:
    """Resume a journaled promotion or its committed rollback decision."""
    root = root.resolve()
    branch = _safe_branch(branch)
    branch_ref = _branch_ref(root)
    if quote(branch_ref.replace("/", "-"), safe="-_.+") != branch:
        raise ValueError("branch changed after release gather")
    refuse_legacy(root, branch_ref)
    journal = json.loads(journal_path(root, branch_ref).read_text(encoding="utf-8"))
    if journal.get("branch_ref") != branch_ref:
        raise ValueError("branch changed after release gather")
    _finish(root, branch, journal)


def main(argv: list[str] | None = None) -> int:
    """Run a release source check or staged publication command."""
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("check", "begin", "seal", "publish", "recover"))
    parser.add_argument("--branch", required=True)
    parser.add_argument("--changelog")
    parser.add_argument("--stage")
    parser.add_argument("--head-sha")
    parser.add_argument("--start-sha")
    parser.add_argument("--range-start")
    parser.add_argument("--last-tag", default="")
    parser.add_argument("--marker-sha")
    parser.add_argument("--output", action="append", default=[])
    parser.add_argument("--branch-ref")
    parser.add_argument("--tag-state-file")
    args = parser.parse_args(argv)
    root = Path.cwd()
    try:
        if args.action == "check":
            if not args.head_sha or not args.branch_ref or not args.tag_state_file:
                parser.error("check requires --head-sha, --branch-ref, and --tag-state-file")
            _check_source(
                root,
                _safe_branch(args.branch),
                args.head_sha,
                args.branch_ref,
                hashlib.sha256(Path(args.tag_state_file).read_bytes()).hexdigest(),
            )
        elif args.action == "begin":
            if not args.changelog or not args.head_sha:
                parser.error("begin requires --changelog and --head-sha")
            if not args.branch_ref or not args.tag_state_file:
                parser.error("begin requires --branch-ref and --tag-state-file")
            if not args.range_start:
                parser.error("begin requires --range-start")
            stage = begin(
                root,
                args.branch,
                args.changelog,
                args.head_sha,
                args.start_sha,
                args.branch_ref,
                Path(args.tag_state_file).read_bytes(),
                args.marker_sha,
                tuple(args.output),
                range_start=args.range_start,
                last_tag=args.last_tag,
            )
            print(_shell_path(stage))
        elif args.action == "seal":
            if not args.stage:
                parser.error("seal requires --stage")
            seal(root, args.branch, Path(args.stage))
        elif args.action == "publish":
            if not args.stage:
                parser.error("publish requires --stage")
            publish(root, args.branch, Path(args.stage))
        else:
            recover(root, args.branch)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        parser.exit(1, f"Error: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
