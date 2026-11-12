"""Verify bounded bridge artifact retention without deleting unresolved or linked evidence."""

from __future__ import annotations

import ctypes
import errno
import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from itertools import chain
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from _bridge_link_probes import directory_symlinks_supported, hardlinks_supported

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import bridge_call

DAY = 86400


def test_windows_retention_reads_only_while_directory_handles_are_held(
    store: bridge_call.BridgePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record read cannot follow a substituted intermediate reparse point before its final-path check."""
    record = store.root / "result-1-1234abcd.json"
    record.write_text('{"status":"complete"}', encoding="utf-8")
    with record.open("rb") as stream:
        expected = os.fstat(stream.fileno())
    held: list[Path] = []

    @contextmanager
    def hold_directory(path: Path) -> Iterator[None]:
        """Record native directory-handle lifetimes around the file read."""
        held.append(path)
        yield
        held.remove(path)

    def open_record(path: Path) -> int:
        """Require every parent handle before opening the real test record."""
        assert held == [store.workspace, store.workspace / ".temp", store.root]
        return os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))

    monkeypatch.setattr(bridge_call.os, "name", "nt")
    monkeypatch.setattr(bridge_call, "_windows_retention_directory", hold_directory)
    monkeypatch.setattr(bridge_call, "_open_windows_task_descriptor", open_record)
    assert bridge_call._read_retention_record(store, record, expected) == {"status": "complete"}
    assert held == []


@pytest.mark.parametrize(
    ("attributes", "expectation"),
    [
        pytest.param(0x10, nullcontext(), id="directory"),
        pytest.param(0x410, pytest.raises(bridge_call.ArtifactBoundaryError), id="reparse-directory"),
        pytest.param(0x80, pytest.raises(bridge_call.ArtifactBoundaryError), id="regular-file"),
    ],
)
def test_windows_retention_locks_only_opened_nonreparse_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, attributes: int, expectation: object
) -> None:
    """Windows handles prevent both rename and reparse mutation and are closed after acceptance or rejection."""

    def inspect_handle(handle: object, info_class: int, output: object, size: int) -> int:
        """Return the independently supplied FileAttributeTagInfo for the opened handle."""
        assert info_class == 9
        assert size == 8
        output[0] = attributes
        return 1

    kernel = SimpleNamespace(
        CreateFileW=Mock(return_value=42),
        GetFileInformationByHandleEx=Mock(side_effect=inspect_handle),
        CloseHandle=Mock(),
    )
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=kernel), raising=False)
    with expectation, bridge_call._windows_retention_directory(tmp_path):
        assert kernel.CreateFileW.call_args.args == (str(tmp_path), 0x80, 1, None, 3, 0x02200000, None)
        kernel.CloseHandle.assert_not_called()
    kernel.CloseHandle.assert_called_once()
    assert kernel.CloseHandle.call_args.args[0].value == 42


def test_windows_retention_refuses_an_unavailable_directory_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sharing violation skips maintenance without using or closing an invalid Windows handle."""
    kernel = SimpleNamespace(
        CreateFileW=Mock(return_value=ctypes.c_void_p(-1).value),
        GetFileInformationByHandleEx=Mock(),
        CloseHandle=Mock(),
    )
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=kernel), raising=False)
    with pytest.raises(bridge_call.ArtifactBoundaryError), bridge_call._windows_retention_directory(tmp_path):
        pytest.fail("invalid directory handle was accepted")
    kernel.GetFileInformationByHandleEx.assert_not_called()
    kernel.CloseHandle.assert_not_called()


def test_windows_retention_holds_every_parent_without_posix_apis(
    store: bridge_call.BridgePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Native Windows anchoring locks each directory through unlink and releases them in reverse order."""
    events: list[tuple[str, Path]] = []

    @contextmanager
    def hold_directory(path: Path) -> Iterator[None]:
        """Record the lifetime of each OS boundary handle."""
        events.append(("open", path))
        yield
        events.append(("close", path))

    monkeypatch.setattr(bridge_call.os, "name", "nt")
    monkeypatch.delattr(bridge_call.os, "O_DIRECTORY", raising=False)
    monkeypatch.delattr(bridge_call.os, "O_NOFOLLOW", raising=False)
    monkeypatch.setattr(bridge_call, "_windows_retention_directory", hold_directory)
    with bridge_call._retention_parent(store, store.jobs / "job.json") as descriptor:
        assert descriptor is None
        assert [path for action, path in events if action == "open"] == [
            store.workspace,
            store.workspace / ".temp",
            store.root,
            store.jobs,
        ]
        assert all(action == "open" for action, path in events)
    assert [path for action, path in events if action == "close"] == [
        store.jobs,
        store.root,
        store.workspace / ".temp",
        store.workspace,
    ]


@pytest.fixture
def store(tmp_path: Path) -> bridge_call.BridgePaths:
    """Create a private artifact directory with the production layout."""
    paths = bridge_call.BridgePaths(tmp_path)
    paths.prepare()
    return paths


def test_windows_inventory_uses_complete_file_identity(
    store: bridge_call.BridgePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows inventory uses opened-handle identity rather than incomplete directory metadata."""
    members = _completed(store, 0)
    original_scan = os.scandir
    incomplete = SimpleNamespace(st_mode=members["report"].stat().st_mode, st_nlink=0, st_dev=0, st_ino=0)

    @contextmanager
    def scan(directory: Path) -> Iterator[object]:
        """Expose the zero identity fields supplied by native Windows DirEntry.stat."""
        with original_scan(directory) as entries:
            yield [SimpleNamespace(name=entry.name, stat=Mock(return_value=incomplete)) for entry in entries]

    monkeypatch.setattr(bridge_call.os, "name", "nt")
    monkeypatch.setattr(bridge_call, "_windows_retention_directory", lambda path: nullcontext())
    monkeypatch.setattr(bridge_call, "_open_windows_task_descriptor", _open_test_member)
    monkeypatch.setattr(bridge_call.os, "scandir", scan)
    inventory = bridge_call._retention_inventory(store)
    assert set(inventory) == set(members.values())
    with members["report"].open("rb") as stream:
        assert bridge_call._same_retention_file(os.fstat(stream.fileno()), inventory[members["report"]])


def _open_test_member(path: Path) -> int:
    """Supply a real descriptor where portable simulations replace the Windows opener."""
    return os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))


@pytest.fixture
def windows_path_metadata_disagreement(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate path metadata differing from handle metadata while keeping real file operations."""
    original_fstat = os.fstat

    def handle_stat(descriptor: int) -> object:
        """Change only the handle-query timestamp, preserving every other metadata field."""
        metadata = original_fstat(descriptor)
        fields = {name: getattr(metadata, name) for name in dir(metadata) if name.startswith("st_")}
        return SimpleNamespace(**(fields | {"st_ctime_ns": metadata.st_ctime_ns + 1}))

    monkeypatch.setattr(bridge_call, "os", SimpleNamespace(**(vars(os) | {"name": "nt", "fstat": handle_stat})))
    monkeypatch.setattr(bridge_call, "_windows_retention_directory", lambda path: nullcontext())
    monkeypatch.setattr(bridge_call, "_open_windows_task_descriptor", _open_test_member)


@pytest.mark.usefixtures("windows_path_metadata_disagreement")
def test_windows_retention_prunes_when_path_and_handle_metadata_differ(store: bridge_call.BridgePaths) -> None:
    """Expired ordinary evidence is removed despite path and handle timestamp differences."""
    members = _completed(store, 0, age=31 * DAY)
    bridge_call._prune_artifacts(store)
    assert not any(path.exists() for path in members.values())


@pytest.mark.parametrize(
    "mutation",
    [
        "replace",
        pytest.param("link", marks=pytest.mark.skipif(not hardlinks_supported(), reason="requires hard-link creation")),
    ],
)
@pytest.mark.usefixtures("windows_path_metadata_disagreement")
def test_windows_unlink_preserves_changed_member(store: bridge_call.BridgePaths, tmp_path: Path, mutation: str) -> None:
    """A stale inventory cannot authorize deleting a replacement or newly hardlinked member."""
    members = _completed(store, 0, age=31 * DAY)
    member = members["report"]
    inventory = bridge_call._retention_inventory(store)
    _change_member(member, tmp_path / "replacement.json", mutation)
    bridge_call._unlink_retention_member(store, member, inventory[member])
    assert member.is_file()


def _change_member(member: Path, other: Path, mutation: str) -> None:
    """Keep the inventoried inode alive while replacing or adding a link to its name."""
    if mutation == "replace":
        member.rename(other)
        member.write_text("replacement evidence", encoding="utf-8")
    else:
        os.link(member, other)


@pytest.mark.skipif(not hardlinks_supported(), reason="requires hard-link creation")
def test_windows_inventory_rejects_hardlinked_evidence(
    store: bridge_call.BridgePaths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fetching complete Windows metadata must still reject files shared with another directory entry."""
    target = tmp_path / "outside.txt"
    target.write_text("outside evidence", encoding="utf-8")
    member = store.root / "raw-1-1234abcd.txt"
    os.link(target, member)
    monkeypatch.setattr(bridge_call.os, "name", "nt")
    monkeypatch.setattr(bridge_call, "_windows_retention_directory", lambda path: nullcontext())
    monkeypatch.setattr(bridge_call, "_open_windows_task_descriptor", _open_test_member)
    with pytest.raises(bridge_call.ArtifactBoundaryError, match="linked bridge retention member"):
        bridge_call._retention_inventory(store)
    assert target.read_text(encoding="utf-8") == "outside evidence"
    assert member.is_file()


def _completed(paths: bridge_call.BridgePaths, index: int, *, age: float = 0, detached: bool = True) -> dict[str, Path]:
    """Persist one completion's linked evidence with deterministic file times."""
    stamp = index + 1
    members = {
        "transcript": paths.root / f"raw-{stamp}-1234abcd.txt",
        "incident": paths.incidents / f"{stamp}-1234abcd-timeout.json",
        "report": paths.root / f"result-{stamp}-1234abcd.json",
    }
    members["transcript"].write_text("peer output", encoding="utf-8")
    result = {
        "run_id": f"run-{index}",
        "status": "complete",
        "transcript_path": paths.relative(members["transcript"]),
        "incident": paths.relative(members["incident"]),
    }
    members["incident"].write_text(json.dumps({"transcript_path": result["transcript_path"]}), encoding="utf-8")
    members["report"].write_text(json.dumps(result), encoding="utf-8")
    if detached:
        job_id = str(uuid.UUID(int=stamp))
        members["job"] = paths.jobs / f"{job_id}.json"
        members["cancel"] = paths.jobs / f"{job_id}.cancel.json"
        members["job"].write_text(
            json.dumps({"job_id": job_id, "status": "finished", "result": result}), encoding="utf-8"
        )
        members["cancel"].write_text(json.dumps({"job_id": job_id}), encoding="utf-8")
    modified = time.time() - age
    for member in members.values():
        os.utime(member, (modified, modified))
    return members


def _seed_completions(paths: bridge_call.BridgePaths, *, detached: bool) -> list[dict[str, Path]]:
    """Order 201 completed calls by their actual completion times."""
    return [_completed(paths, index, age=201 - index, detached=detached) for index in range(201)]


def _remove_transcripts(completions: list[dict[str, Path]]) -> None:
    """Simulate partial evidence loss while keeping the completed records and reports intact."""
    for members in completions:
        members["transcript"].unlink()


def test_missing_transcripts_do_not_double_count_completions(store: bridge_call.BridgePaths) -> None:
    """A completion's stable transcript identity still joins its record and report after evidence loss."""
    completions = _seed_completions(store, detached=True)
    _remove_transcripts(completions)
    bridge_call._prune_artifacts(store)
    assert len(list(store.root.glob("result-*.json"))) == 200
    assert len([path for path in store.jobs.glob("*.json") if not path.name.endswith(".cancel.json")]) == 200
    assert not completions[0]["job"].exists()
    assert all(members["job"].is_file() and members["report"].is_file() for members in completions[1:])


@pytest.mark.skipif(not directory_symlinks_supported(), reason="requires symbolic-link creation")
def test_parent_swap_after_last_check_cannot_delete_outside_evidence(
    store: bridge_call.BridgePaths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deletion remains bound to the checked directory even if its pathname is replaced at the final seam."""
    members = _completed(store, 0, age=31 * DAY)
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / members["job"].name
    target.write_text("outside evidence", encoding="utf-8")
    original_metadata = bridge_call._retention_metadata
    checks: list[Path] = []

    def swap_after_check(paths: bridge_call.BridgePaths, path: Path) -> os.stat_result:
        """Replace the job directory only after inventory and immediately before deletion."""
        metadata = original_metadata(paths, path)
        if path == members["job"]:
            checks.append(path)
            if len(checks) == 1:
                (store.root / "jobs").rename(store.root / "saved-jobs")
                (store.root / "jobs").symlink_to(outside, target_is_directory=True)
        return metadata

    monkeypatch.setattr(bridge_call, "_retention_metadata", swap_after_check)
    bridge_call._prune_optional_artifacts(store)
    assert len(checks) == 1
    assert target.read_text(encoding="utf-8") == "outside evidence"


def _unresolved(paths: bridge_call.BridgePaths, status: str, transcript: Path | None = None) -> Path:
    """Write an old unresolved job, optionally referencing another call's transcript."""
    job_id = "ffffffff-ffff-4fff-8fff-ffffffffffff"
    path = paths.jobs / f"{job_id}.json"
    record = {"job_id": job_id, "status": status, "request": {"run_id": "unresolved"}}
    if transcript is not None:
        record["transcript_path"] = paths.relative(transcript)
    path.write_text(json.dumps(record), encoding="utf-8")
    old = time.time() - 60 * DAY
    os.utime(path, (old, old))
    return path


def _request(workspace: Path) -> bridge_call.Request:
    """Refuse at the recursion gate so maintenance tests never invoke a provider."""
    return bridge_call.Request(
        "advise", "No provider call", None, "medium", 1, 1, "retention", workspace, "claude_to_codex"
    )


def test_prunes_expired_completed_job_and_its_evidence(store: bridge_call.BridgePaths) -> None:
    """Age retention removes the completed record, cancellation marker, report, transcript and incident together."""
    expired = _completed(store, 0, age=31 * DAY)
    current = _completed(store, 1)
    bridge_call._prune_artifacts(store)
    assert not any(path.exists() for path in expired.values())
    assert all(path.exists() for path in current.values())


@pytest.mark.parametrize("detached", [True, False])
def test_keeps_the_newest_200_completed_calls(store: bridge_call.BridgePaths, detached: bool) -> None:
    """A background record and its result report count as one completion, just like a foreground result."""
    completions = _seed_completions(store, detached=detached)
    bridge_call._prune_artifacts(store)
    assert not any(path.exists() for path in completions[0].values())
    assert all(path.exists() for members in completions[1:] for path in members.values())
    assert len(list(store.root.glob("result-*.json"))) == 200


@pytest.mark.parametrize("status", ["queued", "running", "stalled", "cancel_requested"])
def test_preserves_unresolved_records_and_shared_transcripts(store: bridge_call.BridgePaths, status: str) -> None:
    """Neither age nor another completion's deletion may remove evidence still referenced by unresolved work."""
    expired = _completed(store, 0, age=31 * DAY)
    unresolved = _unresolved(store, status, expired["transcript"])
    bridge_call._prune_artifacts(store)
    assert unresolved.is_file()
    assert expired["transcript"].read_text(encoding="utf-8") == "peer output"
    assert not expired["job"].exists()


def test_unresolved_jobs_do_not_consume_completed_capacity(store: bridge_call.BridgePaths) -> None:
    """An unresolved reservation neither counts toward 200 completions nor loses its own artifacts."""
    completions = _seed_completions(store, detached=True)
    unresolved = _unresolved(store, "running")
    bridge_call._prune_artifacts(store)
    assert unresolved.is_file()
    assert len(list(store.root.glob("result-*.json"))) == 200
    assert completions[-1]["job"].is_file()


def test_preserves_report_written_before_supervisor_finishes(store: bridge_call.BridgePaths) -> None:
    """A result with an unresolved request's run identity cannot become an old standalone completion."""
    members = _completed(store, 0, age=31 * DAY, detached=False)
    unresolved = _unresolved(store, "running")
    record = json.loads(unresolved.read_text(encoding="utf-8"))
    record["request"]["run_id"] = "run-0"
    unresolved.write_text(json.dumps(record), encoding="utf-8")
    bridge_call._prune_artifacts(store)
    assert all(path.exists() for path in members.values())
    assert unresolved.is_file()


def test_preserves_unrelated_files_and_cleans_only_old_orphan_evidence(store: bridge_call.BridgePaths) -> None:
    """Only bridge-owned transcript and incident names qualify for orphan age pruning."""
    old = _completed(store, 0, age=31 * DAY, detached=False)
    old["report"].unlink()
    unrelated = store.root / "user-note.txt"
    unrelated.write_text("keep", encoding="utf-8")
    recent = store.root / "raw-2-1234abcd.txt"
    recent.write_text("active output", encoding="utf-8")
    bridge_call._prune_artifacts(store)
    assert not old["transcript"].exists()
    assert not old["incident"].exists()
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert recent.read_text(encoding="utf-8") == "active output"


def test_unreadable_job_aborts_optional_pruning(store: bridge_call.BridgePaths) -> None:
    """Unknown references in a broken job record must not cause evidence loss."""
    expired = _completed(store, 0, age=31 * DAY)
    broken = store.jobs / "ffffffff-ffff-4fff-8fff-ffffffffffff.json"
    broken.write_text("{", encoding="utf-8")
    bridge_call._prune_optional_artifacts(store)
    assert all(path.exists() for path in expired.values())


def test_disappearing_job_entry_aborts_the_whole_pruning_pass(
    store: bridge_call.BridgePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial directory scan cannot hide an unresolved reference and make its transcript look orphaned."""
    expired = _completed(store, 0, age=31 * DAY)
    unresolved = _unresolved(store, "running", expired["transcript"])
    original_scan = os.scandir
    jobs_inode = store.jobs.stat().st_ino

    class MissingEntry:
        """Represent a job removed between directory enumeration and its metadata probe."""

        name = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee.json"

        def stat(self, *, follow_symlinks: bool) -> os.stat_result:
            """Expose the concurrent removal at the OS boundary."""
            raise FileNotFoundError("entry disappeared")

    @contextmanager
    def scan(directory: int | Path) -> Iterator[object]:
        """Prepend the vanished entry only to the actual jobs directory enumeration."""
        metadata = os.fstat(directory) if isinstance(directory, int) else directory.stat()
        with original_scan(directory) as entries:
            yield chain([MissingEntry()], entries) if metadata.st_ino == jobs_inode else entries

    monkeypatch.setattr(bridge_call.os, "scandir", scan)
    bridge_call._prune_optional_artifacts(store)
    assert unresolved.is_file()
    assert expired["transcript"].is_file()


@pytest.mark.skipif(not directory_symlinks_supported(), reason="requires symbolic-link creation")
def test_pruning_never_follows_a_linked_jobs_directory(store: bridge_call.BridgePaths, tmp_path: Path) -> None:
    """Even an in-store directory alias is excluded from retention traversal."""
    members = _completed(store, 0, age=31 * DAY)
    real_jobs = store.root / "other-jobs"
    store.jobs.rename(real_jobs)
    (store.root / "jobs").symlink_to(real_jobs, target_is_directory=True)
    bridge_call._prune_optional_artifacts(store)
    assert (real_jobs / members["job"].name).is_file()
    assert members["transcript"].is_file()


@pytest.mark.skipif(not directory_symlinks_supported(), reason="requires symbolic-link creation")
def test_pruning_never_follows_a_linked_transcript(store: bridge_call.BridgePaths, tmp_path: Path) -> None:
    """An expired completion cannot lead retention to unlink or inspect a transcript alias's target."""
    members = _completed(store, 0, age=31 * DAY)
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    members["transcript"].unlink()
    members["transcript"].symlink_to(outside)
    bridge_call._prune_optional_artifacts(store)
    assert members["transcript"].is_symlink()
    assert outside.read_text(encoding="utf-8") == "private"


@pytest.mark.parametrize("directory", [".temp", ".temp/bridge"])
@pytest.mark.skipif(not directory_symlinks_supported(), reason="requires symbolic-link creation")
def test_pruning_rejects_an_aliased_artifact_root(
    store: bridge_call.BridgePaths, tmp_path: Path, directory: str
) -> None:
    """Resolving an in-project alias during construction must not make that alias eligible for pruning."""
    members = _completed(store, 0, age=31 * DAY)
    original = tmp_path / directory
    relocated = tmp_path / "relocated"
    original.rename(relocated)
    original.symlink_to(relocated, target_is_directory=True)
    aliased = bridge_call.BridgePaths(tmp_path)
    bridge_call._prune_optional_artifacts(aliased)
    assert members["job"].is_file()
    assert members["transcript"].is_file()


def test_malformed_job_status_does_not_fail_peer_delivery(store: bridge_call.BridgePaths) -> None:
    """A structurally invalid local record is a maintenance failure, not a failed peer result."""
    unresolved = _unresolved(store, "running")
    record = json.loads(unresolved.read_text(encoding="utf-8"))
    record["status"] = []
    unresolved.write_text(json.dumps(record), encoding="utf-8")
    result = bridge_call.run_request(_request(store.workspace))
    assert result["status"] == "refused"
    assert unresolved.is_file()


def test_completed_call_runs_pruning_once(store: bridge_call.BridgePaths, monkeypatch: pytest.MonkeyPatch) -> None:
    """A logical request has only one optional pruning pass, outside its attempt loop."""
    passes: list[Path] = []
    monkeypatch.setattr(bridge_call, "_prune_artifacts", lambda paths: passes.append(paths.workspace), raising=False)
    result = bridge_call.run_request(_request(store.workspace))
    assert result["status"] == "refused"
    assert passes == [store.workspace]


@pytest.mark.parametrize("failure", [PermissionError, AttributeError])
def test_pruning_io_failure_does_not_fail_the_call(
    store: bridge_call.BridgePaths, monkeypatch: pytest.MonkeyPatch, failure: type[Exception]
) -> None:
    """Optional maintenance failure preserves the original bridge result."""

    def deny_scan(path: Path) -> None:
        """Model an unavailable directory enumeration without altering peer delivery."""
        raise failure("cannot enumerate")

    monkeypatch.setattr(bridge_call.os, "scandir", deny_scan)
    result = bridge_call.run_request(_request(store.workspace))
    assert result["status"] == "refused"
    assert result["blockers"] == ["recursion-depth"]


def test_health_rotates_at_five_mib_and_keeps_one_previous_generation(store: bridge_call.BridgePaths) -> None:
    """A newly appended row replaces the previous generation only when the current file reaches its limit."""
    result = bridge_call.run_request(_request(store.workspace))
    health = store.root / "health.jsonl"
    previous = store.root / "health.jsonl.1"
    previous.write_text("older generation", encoding="utf-8")
    old = b"x" * (5 * 1024 * 1024)
    health.write_bytes(old)
    bridge_call._append_health(store, result)
    assert previous.read_bytes() == old
    assert json.loads(health.read_text(encoding="utf-8"))["run_id"] == "retention"
    assert not (store.root / "health.jsonl.2").exists()


_HEALTH_WRITER = r"""
import errno
import sys
import time
from pathlib import Path

sys.path.insert(0, sys.argv[1])
import bridge_call

workspace, role = Path(sys.argv[2]), sys.argv[3]
def wait(name):
    deadline = time.monotonic() + 10
    while not (workspace / name).exists():
        if time.monotonic() >= deadline:
            raise RuntimeError("writer coordination timed out")
        time.sleep(0.01)

if role == "first":
    replace = bridge_call.os.replace
    def pause_replace(*args, **kwargs):
        (workspace / "rotating").touch()
        wait("release")
        return replace(*args, **kwargs)
    bridge_call.os.replace = pause_replace
else:
    if sys.platform == "win32":
        import msvcrt as locks
        operation = "locking"
    else:
        import fcntl as locks
        operation = "flock"
    native_lock = getattr(locks, operation)
    def observe_contention(*args):
        try:
            return native_lock(*args)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                (workspace / "second-contended").touch()
            raise
    setattr(locks, operation, observe_contention)
(workspace / (role + "-ready")).touch()
wait(role + "-go")
bridge_call._append_health(bridge_call.BridgePaths(workspace), {
    "run_id": role, "verb": "review", "direction": "claude_to_codex", "model": "test",
    "effort": "low", "cost": 0, "tokens": {}, "duration_seconds": 0, "status": "complete", "depth": 0,
})
(workspace / (role + "-done")).touch()
"""


def _wait_for_health_marker(path: Path, timeout: float = 10) -> bool:
    """Wait for a real worker's coordination event under a bounded deadline."""
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    return True


def _race_health_writers(workspace: Path) -> None:
    """Pause one rotation while another process attempts to append to the same ledger."""
    command = [sys.executable, "-c", _HEALTH_WRITER, str(Path(bridge_call.__file__).parent), str(workspace)]
    first = subprocess.Popen([*command, "first"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    second = subprocess.Popen([*command, "second"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert _wait_for_health_marker(workspace / "first-ready")
        assert _wait_for_health_marker(workspace / "second-ready")
        (workspace / "first-go").touch()
        assert _wait_for_health_marker(workspace / "rotating")
        (workspace / "second-go").touch()
        _wait_for_second_health_attempt(workspace)
        (workspace / "release").touch()
        first_output = first.communicate(timeout=10)
        second_output = second.communicate(timeout=10)
        assert first.returncode == 0, first_output
        assert second.returncode == 0, second_output
    finally:
        (workspace / "release").touch()
        if first.poll() is None:
            first.kill()
        if second.poll() is None:
            second.kill()
        first.communicate()
        second.communicate()


def _wait_for_second_health_attempt(workspace: Path) -> None:
    """Require completion or observed native lock contention before releasing the first rotation."""
    deadline = time.monotonic() + 10
    while not (workspace / "second-done").exists() and not (workspace / "second-contended").exists():
        if time.monotonic() >= deadline:
            raise AssertionError("second writer never completed or encountered native lock contention")
        time.sleep(0.01)


@pytest.mark.integration
def test_concurrent_health_rotation_preserves_previous_generation_and_both_rows(store: bridge_call.BridgePaths) -> None:
    """Two real writers cannot rotate a newly created ledger over the retained generation."""
    old = b"x" * (5 * 1024 * 1024)
    (store.root / "health.jsonl").write_bytes(old)

    _race_health_writers(store.workspace)

    assert (store.root / "health.jsonl.1").read_bytes() == old
    rows = (store.root / "health.jsonl").read_text(encoding="utf-8").splitlines()
    assert {json.loads(row)["run_id"] for row in rows} == {"first", "second"}
    assert len(rows) == 2


def test_windows_health_lock_locks_existing_byte_zero_and_closes_descriptor(
    store: bridge_call.BridgePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows locking uses an existing byte on a read/write descriptor and releases it on scope exit."""
    descriptor = os.open(store.root / ".health-lock", os.O_CREAT | os.O_RDWR, 0o600)
    opened = Mock(return_value=descriptor)

    def lock_byte(observed: int, mode: int, length: int) -> None:
        """Check native byte-range locking inputs without depending on the host OS."""
        assert os.fstat(observed).st_size >= 1
        assert os.lseek(observed, 0, os.SEEK_CUR) == 0
        assert (observed, mode, length) == (descriptor, 2, 1)

    monkeypatch.setattr(bridge_call.os, "name", "nt")
    monkeypatch.setattr(bridge_call, "_open_regular_descriptor", opened)
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(LK_NBLCK=2, locking=lock_byte))

    with bridge_call._health_lock(store, None):
        assert os.fstat(descriptor).st_size >= 1

    opened.assert_called_once_with(store.root / ".health-lock", dir_fd=None, read_write=True)
    with pytest.raises(OSError, match=r".+") as closed:
        os.fstat(descriptor)
    assert closed.value.errno == errno.EBADF


def test_windows_health_lock_descriptor_requests_read_write_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Windows lock handle supports the read/write CRT byte-range locking contract."""
    create = Mock(return_value=42)
    transfer = Mock(return_value=24)
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=SimpleNamespace(CreateFileW=create)), raising=False)
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(open_osfhandle=transfer))

    assert bridge_call._open_windows_nofollow_descriptor(tmp_path / ".health-lock", read_write=True) == 24

    assert create.call_args.args[1] == 0xC0000000
    transfer.assert_called_once_with(42, os.O_RDWR | os.O_APPEND)


def test_health_lock_timeout_closes_descriptor_without_touching_ledger(
    store: bridge_call.BridgePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Persistent contention becomes an accounting boundary error without a ledger mutation."""
    descriptor = os.open(store.root / ".health-lock", os.O_CREAT | os.O_RDWR, 0o600)
    monkeypatch.setattr(bridge_call.os, "name", "nt")
    monkeypatch.setattr(bridge_call, "_open_regular_descriptor", lambda *args, **kwargs: descriptor)
    monkeypatch.setattr(bridge_call, "HEALTH_LOCK_TIMEOUT_SECONDS", 0)
    monkeypatch.setitem(
        sys.modules,
        "msvcrt",
        SimpleNamespace(LK_NBLCK=2, locking=Mock(side_effect=OSError(errno.EACCES, "busy"))),
    )

    with (
        pytest.raises(bridge_call.ArtifactBoundaryError, match="lock timed out"),
        bridge_call._health_lock(store, None),
    ):
        pytest.fail("a contended lock was acquired")

    assert not (store.root / "health.jsonl").exists()
    with pytest.raises(OSError, match=r".+") as closed:
        os.fstat(descriptor)
    assert closed.value.errno == errno.EBADF


@pytest.mark.parametrize("linked", [False, True])
def test_health_lock_rejects_hardlinks_and_nonregular_files(store: bridge_call.BridgePaths, linked: bool) -> None:
    """A planted accounting lock cannot redirect writes into an aliased or nonregular member."""
    lock = store.root / ".health-lock"
    _plant_health_lock(lock, linked)

    with pytest.raises(bridge_call.ArtifactBoundaryError), bridge_call._health_lock(store, None):
        pytest.fail("an unsafe lock was acquired")


def _plant_health_lock(lock: Path, linked: bool) -> None:
    """Create a hardlinked file or directory at the predictable lock path."""
    if linked:
        original = lock.with_name("original")
        original.write_bytes(b"untouched")
        os.link(original, lock)
    else:
        lock.mkdir()


def test_health_below_limit_retains_previous_rows(store: bridge_call.BridgePaths) -> None:
    """Ordinary accounting remains append-only until rotation is needed."""
    result = bridge_call.run_request(_request(store.workspace))
    health = store.root / "health.jsonl"
    before = health.read_bytes()
    bridge_call._append_health(store, result)
    assert health.read_bytes().startswith(before)
    assert len(health.read_text(encoding="utf-8").splitlines()) == 2
    assert not (store.root / "health.jsonl.1").exists()


@pytest.mark.skipif(not directory_symlinks_supported(), reason="requires symbolic-link creation")
def test_health_rotation_rejects_a_linked_previous_generation(store: bridge_call.BridgePaths, tmp_path: Path) -> None:
    """Rotation cannot overwrite a symlink or the file it points to, and delivery still completes."""
    health = store.root / "health.jsonl"
    health.write_bytes(b"x" * (5 * 1024 * 1024))
    target = tmp_path / "outside.txt"
    target.write_text("private", encoding="utf-8")
    previous = store.root / "health.jsonl.1"
    previous.symlink_to(target)
    result = bridge_call.run_request(_request(store.workspace))
    assert result["status"] == "refused"
    assert "Nonblocking health telemetry warning" in " ".join(result["findings"])
    assert previous.is_symlink()
    assert target.read_text(encoding="utf-8") == "private"
    assert health.stat().st_size == 5 * 1024 * 1024


@pytest.mark.skipif(not directory_symlinks_supported(), reason="requires symbolic-link creation")
def test_health_rotation_parent_swap_cannot_overwrite_outside_evidence(
    store: bridge_call.BridgePaths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory replaced after the final metadata check cannot redirect accounting rotation outside the store."""
    health = store.root / "health.jsonl"
    health.write_bytes(b"x" * (5 * 1024 * 1024))
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "health.jsonl").write_text("outside current", encoding="utf-8")
    (outside / "health.jsonl.1").write_text("outside previous", encoding="utf-8")
    original_metadata = bridge_call._retention_metadata
    checks: list[Path] = []

    def swap_after_check(paths: bridge_call.BridgePaths, path: Path) -> os.stat_result:
        """Swap only after the second health check, leaving its returned metadata unchanged."""
        metadata = original_metadata(paths, path)
        if path == health:
            checks.append(path)
            if len(checks) == 2:
                paths.artifact_root.rename(tmp_path / "saved-bridge")
                paths.artifact_root.symlink_to(outside, target_is_directory=True)
        return metadata

    monkeypatch.setattr(bridge_call, "_retention_metadata", swap_after_check)
    bridge_call.run_request(_request(store.workspace))
    assert len(checks) == 2
    assert (outside / "health.jsonl").read_text(encoding="utf-8") == "outside current"
    assert (outside / "health.jsonl.1").read_text(encoding="utf-8") == "outside previous"
