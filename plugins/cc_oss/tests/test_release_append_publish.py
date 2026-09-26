"""Exercise staged append publication and interruption recovery."""

import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

import pytest
import release_append_marker as marker_api


_SCRIPT = Path(__file__).resolve().parents[1] / "bin/release_append_publish.py"
_SPEC = importlib.util.spec_from_file_location("release_append_publish", _SCRIPT)
assert _SPEC and _SPEC.loader
publisher = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(publisher)
_MAIN_MARKER = marker_api.state_relative("main", "marker")
_MAIN_PROVENANCE = marker_api.state_relative("main", "provenance.json")


def _bash_path(path: Path) -> str:
    """Return the fixture path in Git Bash syntax on native Windows."""
    if sys.platform != "win32":
        return str(path)
    return subprocess.check_output(["cygpath", "-u", str(path)], text=True).strip()


def _native_path(shell_path: str) -> Path:
    """Resolve a Git Bash output path for native Python assertions."""
    if sys.platform != "win32":
        return Path(shell_path)
    return Path(subprocess.check_output(["cygpath", "-w", shell_path], text=True).strip())


def _can_sync_directory() -> bool:
    """Probe whether this host can fsync an opened directory."""
    if not hasattr(os, "O_DIRECTORY"):
        return False
    try:
        descriptor = os.open(tempfile.gettempdir(), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        return False
    return True


def test_windows_file_sync_opens_writable_handle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows must pass a writable file handle to FlushFileBuffers."""
    candidate = tmp_path / "candidate.json"
    candidate.write_bytes(b"sealed\n")
    original_open = Path.open
    opened_modes: list[str] = []

    def record_open(path: Path, mode: str = "r", *args: object, **kwargs: object):
        """Record the access mode while preserving the real sync operation."""
        opened_modes.append(mode)
        return original_open(path, mode, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(publisher.sys, "platform", "win32")
        patch.setattr(Path, "open", record_open)
        publisher._sync_file(candidate)
    assert opened_modes == ["r+b"]
    assert candidate.read_bytes() == b"sealed\n"


def test_windows_begin_path_can_cross_git_bash_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stage path printed by native Python must be usable by Git Bash."""
    monkeypatch.setattr(publisher.sys, "platform", "win32")

    def convert(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        """Model Git Bash cygpath translating the stage path."""
        assert command == ["cygpath", "-u", "C:\\Users\\test\\stage"]
        return subprocess.CompletedProcess(command, 0, "/c/Users/test/stage\n", "")

    monkeypatch.setattr(publisher.subprocess, "run", convert)
    assert publisher._shell_path(Path("C:\\Users\\test\\stage")) == "/c/Users/test/stage"


_skip_directory_sync_unavailable = pytest.mark.skipif(
    not _can_sync_directory(), reason="Directory fsync is unavailable on this host"
)


def _write_state(root: Path, relative: str, content: str, encoding: str = "utf-8") -> None:
    """Seed a versioned candidate or live release-state file."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding=encoding, newline="\n")


def _init_git(tmp_path: Path) -> str:
    """Create a main-branch repository with a local test commit identity."""
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"], cwd=tmp_path, check=True, capture_output=True
    )
    (tmp_path / "source.txt").write_text("release source\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "add", "source.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "source"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()


def _stage_marker(stage: Path) -> str:
    head_sha = json.loads((stage / "candidate.json").read_text(encoding="utf-8"))["head_sha"]
    _write_state(stage, _MAIN_MARKER, head_sha + "\n", encoding="utf-8")
    publisher.seal(stage.parent.parent, "main", stage)
    return head_sha + "\n"


def _start(tmp_path: Path) -> Path:
    _init_git(tmp_path)
    (tmp_path / ".temp").mkdir()
    (tmp_path / "DRAFT.md").write_text("## Summary\nOriginal\n", encoding="utf-8", newline="\n")
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8", newline="\n")
    _write_state(tmp_path, _MAIN_PROVENANCE, "[]\n", encoding="utf-8")
    _write_state(tmp_path, _MAIN_MARKER, "old\n", encoding="utf-8")
    return publisher.begin(tmp_path, "main", "CHANGELOG.md", range_start="HEAD")


def _plain_candidate(tmp_path: Path) -> tuple[Path, str, str, tuple[str, str]]:
    """Stage a plain range ending before HEAD with both dated output destinations."""
    old_marker = _init_git(tmp_path)
    subprocess.run(["git", "branch", "-M", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "represented"], cwd=tmp_path, check=True)
    marker_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "later"], cwd=tmp_path, check=True)
    (tmp_path / "DRAFT.md").write_bytes(b"ORIGINAL\r\n")
    (tmp_path / "CHANGELOG.md").write_bytes(b"# Changelog\r\n")
    _write_state(tmp_path, _MAIN_MARKER, old_marker + "\n")
    outputs = (
        ".temp/output-release-summary-main-2026-09-26.md",
        ".temp/output-release-migration-main-2026-09-26.md",
    )
    _write_state(tmp_path, outputs[0], "Prior summary\n")
    _write_state(tmp_path, outputs[1], "Prior migration\n")
    stage = publisher.begin(
        tmp_path, "main", "CHANGELOG.md", marker_sha=marker_sha, extra_outputs=outputs, range_start=old_marker
    )
    (stage / "DRAFT.md").write_bytes(b"COMPLETED\n")
    (stage / "CHANGELOG.md").write_bytes(b"# Changelog\nCompleted\n")
    _write_state(stage, outputs[0], "New summary\n")
    _write_state(stage, outputs[1], "New migration\n")
    _write_state(stage, _MAIN_MARKER, marker_sha + "\n")
    publisher.seal(tmp_path, "main", stage)
    return stage, old_marker, marker_sha, outputs


def test_truth_abort_leaves_all_live_artifacts_unchanged(tmp_path: Path) -> None:
    """An unapproved candidate cannot change files or advance the marker."""
    stage = _start(tmp_path)
    (stage / "DRAFT.md").write_text(
        "## Summary\nOriginal\n### Since last draft\nIncrement\n", encoding="utf-8", newline="\n"
    )
    (stage / "CHANGELOG.md").write_text("# Changelog\nIncrement\n", encoding="utf-8", newline="\n")
    _write_state(stage, _MAIN_PROVENANCE, '[{"patch_id":"new"}]\n', encoding="utf-8")
    assert (tmp_path / "DRAFT.md").read_text(encoding="utf-8") == "## Summary\nOriginal\n"
    assert (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8") == "# Changelog\n"
    assert (tmp_path / _MAIN_MARKER).read_text(encoding="utf-8") == "old\n"
    assert (tmp_path / _MAIN_PROVENANCE).read_text(encoding="utf-8") == "[]\n"
    assert not publisher.journal_path(tmp_path, "main").exists()


@_skip_directory_sync_unavailable
def test_publication_syncs_journal_before_live_replace_and_marker_before_journal_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Directory barriers order journal persistence before live promotion."""
    stage = _start(tmp_path)
    (stage / "DRAFT.md").write_text("completed notes\n", encoding="utf-8", newline="\n")
    _stage_marker(stage)
    journal = publisher.journal_path(tmp_path, "main")
    events: list[tuple[str, Path | None]] = []
    original_fsync = os.fsync
    original_replace = os.replace
    original_unlink = Path.unlink

    def record_fsync(fd: int) -> None:
        """Record directory barriers while retaining actual filesystem sync behavior."""
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            events.append(("dir-sync", None))
        original_fsync(fd)

    def record_replace(source: str | Path, target: str | Path) -> None:
        """Record each visible rename in publication order."""
        events.append(("replace", Path(target)))
        original_replace(source, target)

    def record_unlink(path: Path, *args: object, **kwargs: object) -> None:
        """Record the journal removal that ends crash recovery."""
        if path == journal:
            events.append(("journal-unlink", path))
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "fsync", record_fsync)
    monkeypatch.setattr(os, "replace", record_replace)
    monkeypatch.setattr(Path, "unlink", record_unlink)
    publisher.publish(tmp_path, "main", stage)

    journal_replace = events.index(("replace", journal))
    first_live_replace = next(
        index for index, event in enumerate(events) if event[0] == "replace" and event[1] == tmp_path / "DRAFT.md"
    )
    marker_replace = events.index(("replace", tmp_path / _MAIN_MARKER))
    journal_unlink = events.index(("journal-unlink", journal))
    assert any(event[0] == "dir-sync" for event in events[journal_replace + 1 : first_live_replace])
    assert any(event[0] == "dir-sync" for event in events[marker_replace + 1 : journal_unlink])


@_skip_directory_sync_unavailable
def test_journal_directory_sync_failure_prevents_live_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed journal durability barrier stops promotion before touching user files."""
    stage = _start(tmp_path)
    (stage / "DRAFT.md").write_text("completed notes\n", encoding="utf-8", newline="\n")
    _stage_marker(stage)
    original_fsync = os.fsync

    def fail_directory_sync(fd: int) -> None:
        """Simulate a filesystem refusing the required directory barrier."""
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("directory sync unavailable")
        original_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_directory_sync)
    with pytest.raises(OSError, match="directory sync unavailable"):
        publisher.publish(tmp_path, "main", stage)

    assert (tmp_path / "DRAFT.md").read_text(encoding="utf-8") == "## Summary\nOriginal\n"
    assert (tmp_path / _MAIN_MARKER).read_text(encoding="utf-8") == "old\n"


@_skip_directory_sync_unavailable
def test_artifact_directory_sync_failure_keeps_journal_and_stops_promotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed live rename barrier leaves the journal and blocks the next artifact."""
    stage = _start(tmp_path)
    (stage / "DRAFT.md").write_text("completed notes\n", encoding="utf-8", newline="\n")
    _stage_marker(stage)
    original_fsync = os.fsync
    original_replace = os.replace
    draft_replaced = False

    def record_replace(source: str | Path, target: str | Path) -> None:
        """Begin failing directory barriers after the first live rename."""
        nonlocal draft_replaced
        original_replace(source, target)
        if Path(target) == tmp_path / "DRAFT.md":
            draft_replaced = True

    def fail_after_draft(fd: int) -> None:
        """Model a filesystem sync error after the draft rename."""
        if draft_replaced and stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("artifact directory sync failed")
        original_fsync(fd)

    monkeypatch.setattr(os, "replace", record_replace)
    monkeypatch.setattr(os, "fsync", fail_after_draft)
    with pytest.raises(OSError, match="artifact directory sync failed"):
        publisher.publish(tmp_path, "main", stage)

    assert (tmp_path / "DRAFT.md").read_text(encoding="utf-8") == "completed notes\n"
    assert (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8") == "# Changelog\n"
    assert (tmp_path / _MAIN_MARKER).read_text(encoding="utf-8") == "old\n"
    assert publisher.journal_path(tmp_path, "main").exists()


@_skip_directory_sync_unavailable
@pytest.mark.parametrize("foreign_edit", [False, True])
def test_recovered_final_journal_unlink_preserves_completed_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, foreign_edit: bool
) -> None:
    """A stale journal cannot roll back complete notes after source advances."""
    stage = _start(tmp_path)
    (stage / "DRAFT.md").write_bytes(b"COMPLETED\r\n")
    marker_bytes = _stage_marker(stage).encode()
    journal = publisher.journal_path(tmp_path, "main")
    original_unlink = Path.unlink
    original_fsync = os.fsync
    removed = False
    journal_bytes = b""

    def record_unlink(path: Path, *args: object, **kwargs: object) -> None:
        """Capture the journal bytes that may reappear after a failed unlink sync."""
        nonlocal removed, journal_bytes
        if path == journal:
            journal_bytes = path.read_bytes()
            removed = True
        original_unlink(path, *args, **kwargs)

    def fail_after_unlink(fd: int) -> None:
        """Model failure to persist only the final journal removal."""
        if removed and stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("final journal directory sync failed")
        original_fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", record_unlink)
        patch.setattr(os, "fsync", fail_after_unlink)
        with pytest.raises(OSError, match="final journal directory sync failed"):
            publisher.publish(tmp_path, "main", stage)

    assert journal_bytes
    assert not journal.exists()
    assert (tmp_path / "DRAFT.md").read_bytes() == b"COMPLETED\r\n"
    assert (tmp_path / _MAIN_MARKER).read_bytes() == marker_bytes
    journal.write_bytes(journal_bytes)
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "later source"], cwd=tmp_path, check=True)

    if foreign_edit:
        (tmp_path / "DRAFT.md").write_bytes(b"USER EDIT\r\n")
        with pytest.raises(ValueError, match="artifact changed outside append publication"):
            publisher.recover(tmp_path, "main")
        assert (tmp_path / "DRAFT.md").read_bytes() == b"USER EDIT\r\n"
        assert journal.exists()
    else:
        publisher.recover(tmp_path, "main")
        assert (tmp_path / "DRAFT.md").read_bytes() == b"COMPLETED\r\n"
        assert not journal.exists()
    assert (tmp_path / _MAIN_MARKER).read_bytes() == marker_bytes


def test_partial_publish_recovers_without_replaying_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Recovery finishes the validated candidate after an interrupted replacement."""
    stage = _start(tmp_path)
    candidate = "## Summary\nOriginal\n### Since last draft\nIncrement\n"
    (stage / "DRAFT.md").write_text(candidate, encoding="utf-8", newline="\n")
    (stage / "CHANGELOG.md").write_text("# Changelog\nIncrement\n", encoding="utf-8", newline="\n")
    expected_marker = _stage_marker(stage)
    original_replace = publisher._replace_candidate
    calls = 0

    def interrupt(root: Path, staged: Path, relative: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("interrupted")
        original_replace(root, staged, relative)

    monkeypatch.setattr(publisher, "_replace_candidate", interrupt)
    with pytest.raises(OSError, match="interrupted"):
        publisher.publish(tmp_path, "main", stage)
    assert publisher.journal_path(tmp_path, "main").exists()
    monkeypatch.setattr(publisher, "_replace_candidate", original_replace)
    publisher.recover(tmp_path, "main")
    assert (tmp_path / "DRAFT.md").read_text(encoding="utf-8") == candidate
    assert (tmp_path / "DRAFT.md").read_text(encoding="utf-8").count("Increment") == 1
    assert (tmp_path / _MAIN_MARKER).read_text(encoding="utf-8") == expected_marker
    assert not publisher.journal_path(tmp_path, "main").exists()


def test_unknown_user_edit_blocks_recovery_without_overwrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A user edit made after staging must never be replaced by the candidate."""
    stage = _start(tmp_path)
    (stage / "DRAFT.md").write_text("candidate\n", encoding="utf-8", newline="\n")
    _stage_marker(stage)
    monkeypatch.setattr(publisher, "_replace_candidate", lambda *_: (_ for _ in ()).throw(OSError("stop")))
    with pytest.raises(OSError, match="stop"):
        publisher.publish(tmp_path, "main", stage)
    (tmp_path / "DRAFT.md").write_text("user edit\n", encoding="utf-8", newline="\n")
    with pytest.raises(ValueError, match="changed outside"):
        publisher.recover(tmp_path, "main")
    assert (tmp_path / "DRAFT.md").read_text(encoding="utf-8") == "user edit\n"
    assert (tmp_path / _MAIN_MARKER).read_text(encoding="utf-8") == "old\n"


def test_later_unknown_edit_blocks_all_remaining_recovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Recovery preflights every remaining target before replacing any of them."""
    stage = _start(tmp_path)
    (stage / "DRAFT.md").write_text("candidate draft\n", encoding="utf-8", newline="\n")
    (stage / "CHANGELOG.md").write_text("candidate changelog\n", encoding="utf-8", newline="\n")
    _stage_marker(stage)
    original_replace = publisher._replace_candidate
    calls = 0

    def interrupt(root: Path, staged: Path, relative: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("stop")
        original_replace(root, staged, relative)

    monkeypatch.setattr(publisher, "_replace_candidate", interrupt)
    with pytest.raises(OSError, match="stop"):
        publisher.publish(tmp_path, "main", stage)
    (tmp_path / "DRAFT.md").write_text("user edit\n", encoding="utf-8", newline="\n")
    monkeypatch.setattr(publisher, "_replace_candidate", original_replace)
    with pytest.raises(ValueError, match="changed outside"):
        publisher.recover(tmp_path, "main")
    assert (tmp_path / "DRAFT.md").read_text(encoding="utf-8") == "user edit\n"
    assert (tmp_path / _MAIN_MARKER).read_text(encoding="utf-8") == "old\n"


def test_successful_publish_advances_marker_and_blocks_same_candidate_replay(tmp_path: Path) -> None:
    """A completed append publishes each artifact once and rejects stale replay."""
    stage = _start(tmp_path)
    (stage / "DRAFT.md").write_text(
        "## Summary\nOriginal\n### Since last draft\nIncrement\n", encoding="utf-8", newline="\n"
    )
    expected_marker = _stage_marker(stage)
    publisher.publish(tmp_path, "main", stage)
    assert (tmp_path / "DRAFT.md").read_text(encoding="utf-8").count("Increment") == 1
    assert (tmp_path / _MAIN_MARKER).read_text(encoding="utf-8") == expected_marker
    with pytest.raises(ValueError, match="saved marker changed"):
        publisher.publish(tmp_path, "main", stage)


def test_recovery_accepts_marker_already_published(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A crash after marker replacement can clear its journal without replaying."""
    stage = _start(tmp_path)
    (stage / "DRAFT.md").write_text("candidate\n", encoding="utf-8", newline="\n")
    expected_marker = _stage_marker(stage)
    original_replace = publisher._replace_candidate

    def interrupt_after_marker(root: Path, staged: Path, relative: str) -> None:
        original_replace(root, staged, relative)
        if relative == _MAIN_MARKER:
            raise OSError("after marker")

    monkeypatch.setattr(publisher, "_replace_candidate", interrupt_after_marker)
    with pytest.raises(OSError, match="after marker"):
        publisher.publish(tmp_path, "main", stage)
    assert publisher.journal_path(tmp_path, "main").exists()
    monkeypatch.setattr(publisher, "_replace_candidate", original_replace)
    publisher.recover(tmp_path, "main")
    assert (tmp_path / "DRAFT.md").read_text(encoding="utf-8") == "candidate\n"
    assert (tmp_path / _MAIN_MARKER).read_text(encoding="utf-8") == expected_marker
    assert not publisher.journal_path(tmp_path, "main").exists()


@pytest.mark.parametrize("drift", ["none", "branch", "ancestor-tag"])
def test_recovery_checks_source_when_marker_was_advanced_externally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    """An advanced marker cannot bypass source checks before recovering an old draft."""
    ancestor = _init_git(tmp_path)
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "release"], cwd=tmp_path, check=True, capture_output=True)
    frozen_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    assert ancestor != frozen_head
    (tmp_path / ".temp").mkdir()
    draft = tmp_path / "DRAFT.md"
    draft.write_text("original\n", encoding="utf-8", newline="\n")
    (tmp_path / "CHANGELOG.md").write_text("changelog\n", encoding="utf-8", newline="\n")
    marker = tmp_path / _MAIN_MARKER
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("old\n", encoding="utf-8", newline="\n")
    stage = publisher.begin(tmp_path, "main", "CHANGELOG.md", range_start=ancestor)
    draft_candidate = "candidate\n"
    (stage / "DRAFT.md").write_text(draft_candidate, encoding="utf-8", newline="\n")
    marker_candidate = _stage_marker(stage)
    original_replace = publisher._replace_candidate
    monkeypatch.setattr(publisher, "_replace_candidate", lambda *_: (_ for _ in ()).throw(OSError("stop")))
    with pytest.raises(OSError, match="stop"):
        publisher.publish(tmp_path, "main", stage)
    monkeypatch.setattr(publisher, "_replace_candidate", original_replace)
    journal = publisher.journal_path(tmp_path, "main")
    assert journal.exists()
    marker.write_text(marker_candidate, encoding="utf-8", newline="\n")

    if drift == "branch":
        subprocess.run(["git", "switch", "-q", "-c", "other"], cwd=tmp_path, check=True, capture_output=True)
    elif drift == "ancestor-tag":
        subprocess.run(["git", "tag", "v2", ancestor], cwd=tmp_path, check=True, capture_output=True)
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip() == frozen_head

    if drift == "none":
        publisher.recover(tmp_path, "main")
        assert draft.read_text(encoding="utf-8") == draft_candidate
        assert not journal.exists()
    else:
        with pytest.raises(ValueError, match="branch changed|baseline tags changed"):
            publisher.recover(tmp_path, "main")
        assert draft.read_text(encoding="utf-8") == "original\n"
        assert journal.exists() == (drift == "branch")
    assert marker.read_text(encoding="utf-8") == (marker_candidate if drift in ("none", "branch") else "old\n")


@pytest.mark.parametrize("extra_path", ["SECRET.md", "DRAFT.md"])
def test_malformed_journal_cannot_promote_unlisted_or_duplicate_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra_path: str
) -> None:
    """Recovery rejects extra or duplicate targets before touching remaining files."""
    stage = _start(tmp_path)
    (stage / "DRAFT.md").write_text("candidate\n", encoding="utf-8", newline="\n")
    _stage_marker(stage)
    (tmp_path / "SECRET.md").write_text("user bytes\n", encoding="utf-8", newline="\n")
    monkeypatch.setattr(publisher, "_replace_candidate", lambda *_: (_ for _ in ()).throw(OSError("stop")))
    with pytest.raises(OSError, match="stop"):
        publisher.publish(tmp_path, "main", stage)
    journal_file = publisher.journal_path(tmp_path, "main")
    journal = json.loads(journal_file.read_text(encoding="utf-8"))
    journal["entries"].insert(0, {"path": extra_path, "old": None, "new": "fake"})
    journal_file.write_text(json.dumps(journal), encoding="utf-8", newline="\n")
    with pytest.raises(ValueError, match="duplicate or unexpected"):
        publisher.recover(tmp_path, "main")
    assert (tmp_path / "SECRET.md").read_text(encoding="utf-8") == "user bytes\n"
    assert (tmp_path / "DRAFT.md").read_text(encoding="utf-8") == "## Summary\nOriginal\n"
    assert (tmp_path / _MAIN_MARKER).read_text(encoding="utf-8") == "old\n"


@pytest.mark.parametrize("initial_summary", [None, "existing\n"])
def test_publish_rejects_user_change_to_unchanged_candidate(tmp_path: Path, initial_summary: str | None) -> None:
    """A changed or newly created live optional file cannot bypass candidate validation."""
    (tmp_path / ".temp").mkdir()
    (tmp_path / "DRAFT.md").write_text("original\n", encoding="utf-8", newline="\n")
    (tmp_path / "CHANGELOG.md").write_text("changelog\n", encoding="utf-8", newline="\n")
    _write_state(tmp_path, _MAIN_MARKER, "old\n", encoding="utf-8")
    if initial_summary is not None:
        (tmp_path / "SUMMARY.md").write_text(initial_summary, encoding="utf-8", newline="\n")
    _init_git(tmp_path)
    stage = publisher.begin(tmp_path, "main", "CHANGELOG.md", range_start="HEAD")
    (stage / "DRAFT.md").write_text("candidate\n", encoding="utf-8", newline="\n")
    _stage_marker(stage)
    (tmp_path / "SUMMARY.md").write_text("user edit\n", encoding="utf-8", newline="\n")
    with pytest.raises(ValueError, match="changed outside"):
        publisher.publish(tmp_path, "main", stage)
    assert (tmp_path / "DRAFT.md").read_text(encoding="utf-8") == "original\n"
    assert (tmp_path / _MAIN_MARKER).read_text(encoding="utf-8") == "old\n"


def test_recovery_rejects_change_to_unchanged_candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Recovery also inventories paths omitted from the changed-file journal."""
    stage = _start(tmp_path)
    (stage / "DRAFT.md").write_text("candidate\n", encoding="utf-8", newline="\n")
    _stage_marker(stage)
    original_replace = publisher._replace_candidate
    monkeypatch.setattr(publisher, "_replace_candidate", lambda *_: (_ for _ in ()).throw(OSError("stop")))
    with pytest.raises(OSError, match="stop"):
        publisher.publish(tmp_path, "main", stage)
    monkeypatch.setattr(publisher, "_replace_candidate", original_replace)
    (tmp_path / "SUMMARY.md").write_text("user edit\n", encoding="utf-8", newline="\n")
    with pytest.raises(ValueError, match="changed outside"):
        publisher.recover(tmp_path, "main")
    assert (tmp_path / _MAIN_MARKER).read_text(encoding="utf-8") == "old\n"


def test_recovery_rejects_journal_missing_changed_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A shortened journal cannot mark an unpromoted draft as processed."""
    stage = _start(tmp_path)
    (stage / "DRAFT.md").write_text("candidate\n", encoding="utf-8", newline="\n")
    _stage_marker(stage)
    original_replace = publisher._replace_candidate
    monkeypatch.setattr(publisher, "_replace_candidate", lambda *_: (_ for _ in ()).throw(OSError("stop")))
    with pytest.raises(OSError, match="stop"):
        publisher.publish(tmp_path, "main", stage)
    monkeypatch.setattr(publisher, "_replace_candidate", original_replace)
    journal_file = publisher.journal_path(tmp_path, "main")
    journal = json.loads(journal_file.read_text(encoding="utf-8"))
    journal["entries"] = [entry for entry in journal["entries"] if entry["path"] != "DRAFT.md"]
    journal_file.write_text(json.dumps(journal), encoding="utf-8", newline="\n")
    with pytest.raises(ValueError, match="journal.*candidate"):
        publisher.recover(tmp_path, "main")
    assert (tmp_path / "DRAFT.md").read_text(encoding="utf-8") == "## Summary\nOriginal\n"
    assert (tmp_path / _MAIN_MARKER).read_text(encoding="utf-8") == "old\n"


@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("git") is None, reason="Requires Bash and Git")
def test_explicit_append_range_must_end_at_frozen_head(tmp_path: Path) -> None:
    """A caller-supplied range ending before HEAD cannot certify skipped commits."""
    skill = (Path(__file__).resolve().parents[1] / "skills/release/SKILL.md").read_text(encoding="utf-8")
    block = next(block for block in re.findall(r"```bash\n(.*?)```", skill, re.DOTALL) if "# quote RANGE" in block)
    prefix = block[: block.index("# quote RANGE")]
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True, capture_output=True)
    for index in range(3):
        (tmp_path / "source.txt").write_text(str(index), encoding="utf-8", newline="\n")
        subprocess.run(["git", "add", "source.txt"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", f"c{index}"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )
        if index == 0:
            subprocess.run(["git", "tag", "v1"], cwd=tmp_path, check=True, capture_output=True)
        if index == 1:
            earlier = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    (tmp_path / "release-setup-test").mkdir()
    (tmp_path / "release-setup-test/LAST_TAG").write_text("v1\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-setup-test/BRANCH").write_text("main\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-setup-test/BRANCH_REF").write_text("main\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-setup-test/BRANCH_KEY").write_text(
        marker_api.branch_state_key("main") + "\n", encoding="utf-8", newline="\n"
    )
    (tmp_path / "release-do-append-test").write_text("true\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-mode-test").write_text("notes\n", encoding="utf-8", newline="\n")
    environment = os.environ.copy()
    environment.update({"TMPDIR": str(tmp_path), "CLAUDE_CODE_SESSION_ID": "test"})
    completed = subprocess.run(
        [shutil.which("bash"), "-c", f"RANGE=v1..{earlier}; " + prefix],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0
    assert "does not end at frozen HEAD" in completed.stderr
    assert not (tmp_path / "release-range-test").exists()


def test_new_commit_after_gather_blocks_publish_and_marker(tmp_path: Path) -> None:
    """Publication cannot certify a newer HEAD than the classified commit set."""
    frozen_head = _init_git(tmp_path)
    (tmp_path / ".temp").mkdir()
    (tmp_path / "DRAFT.md").write_text("original\n", encoding="utf-8", newline="\n")
    (tmp_path / "CHANGELOG.md").write_text("changelog\n", encoding="utf-8", newline="\n")
    _write_state(tmp_path, _MAIN_MARKER, "old\n", encoding="utf-8")
    stage = publisher.begin(tmp_path, "main", "CHANGELOG.md", frozen_head, range_start="HEAD")
    (stage / "DRAFT.md").write_text("candidate\n", encoding="utf-8", newline="\n")
    _stage_marker(stage)
    (tmp_path / "source.txt").write_text("later source\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "add", "source.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "later"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    with pytest.raises(ValueError, match="HEAD changed after release gather"):
        publisher.publish(tmp_path, "main", stage)
    assert (tmp_path / "DRAFT.md").read_text(encoding="utf-8") == "original\n"
    assert (tmp_path / _MAIN_MARKER).read_text(encoding="utf-8") == "old\n"


def test_new_commit_before_staging_blocks_begin(tmp_path: Path) -> None:
    """The staging input must be the same HEAD frozen during Gather."""
    frozen_head = _init_git(tmp_path)
    (tmp_path / "source.txt").write_text("later source\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "add", "source.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "later"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    with pytest.raises(ValueError, match="HEAD changed after release gather"):
        publisher.begin(tmp_path, "main", "CHANGELOG.md", frozen_head, range_start="HEAD")


@pytest.mark.parametrize("phase", ["before-stage", "before-publish"])
def test_same_head_branch_switch_blocks_append(tmp_path: Path, phase: str) -> None:
    """A checkout with the gathered commit must not publish under another branch's marker."""
    frozen_head = _init_git(tmp_path)
    subprocess.run(["git", "branch", "-M", "main"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / ".temp").mkdir()
    (tmp_path / "DRAFT.md").write_text("original\n", encoding="utf-8", newline="\n")
    (tmp_path / "CHANGELOG.md").write_text("changelog\n", encoding="utf-8", newline="\n")
    _write_state(tmp_path, _MAIN_MARKER, "old\n", encoding="utf-8")
    if phase == "before-publish":
        stage = publisher.begin(tmp_path, "main", "CHANGELOG.md", frozen_head, range_start="HEAD")
        (stage / "DRAFT.md").write_text("candidate\n", encoding="utf-8", newline="\n")
        _stage_marker(stage)
    subprocess.run(["git", "switch", "-q", "-c", "other"], cwd=tmp_path, check=True, capture_output=True)
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip() == frozen_head
    with pytest.raises(ValueError, match="branch changed after release gather"):
        if phase == "before-stage":
            publisher.begin(tmp_path, "main", "CHANGELOG.md", frozen_head, range_start="HEAD")
        else:
            publisher.publish(tmp_path, "main", stage)
    assert (tmp_path / "DRAFT.md").read_text(encoding="utf-8") == "original\n"
    assert (tmp_path / _MAIN_MARKER).read_text(encoding="utf-8") == "old\n"


@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("git") is None, reason="Requires Bash and Git")
@pytest.mark.parametrize("phase", ["before-stage", "before-publish"])
def test_new_release_tag_blocks_append_with_unchanged_head(tmp_path: Path, phase: str) -> None:
    """A tag cut after Gather invalidates the release baseline before publication."""
    baseline = _init_git(tmp_path)
    subprocess.run(["git", "branch", "-M", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "tag", "v1"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "source.txt").write_text("previous append\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "add", "source.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "marker"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    marker = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    (tmp_path / "source.txt").write_text("new release change\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "add", "source.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "release"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    frozen_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    assert baseline != marker != frozen_head
    (tmp_path / ".temp").mkdir()
    (tmp_path / "DRAFT.md").write_text("original\n", encoding="utf-8", newline="\n")
    (tmp_path / "CHANGELOG.md").write_text("changelog\n", encoding="utf-8", newline="\n")
    _write_state(tmp_path, _MAIN_MARKER, marker + "\n", encoding="utf-8")
    setup = tmp_path / "release-setup-test"
    setup.mkdir()
    (setup / "BRANCH").write_text("main\n", encoding="utf-8", newline="\n")
    (setup / "LAST_TAG").write_text("v1\n", encoding="utf-8", newline="\n")
    (setup / "BRANCH_REF").write_text("main\n", encoding="utf-8", newline="\n")
    (setup / "BRANCH_KEY").write_text(marker_api.branch_state_key("main") + "\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-do-append-test").write_text("true\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-mode-test").write_text("notes\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-append-head-test").write_text(frozen_head + "\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-append-start-test").write_text(marker + "\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-append-end-test").write_text(frozen_head + "\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-append-branch-test").write_text("main\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-append-tags-test").write_bytes(publisher._tag_state(tmp_path))
    (tmp_path / "release-marker-valid-test").write_text("true\n", encoding="utf-8", newline="\n")
    plugin_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment.update(
        {"TMPDIR": str(tmp_path), "CLAUDE_CODE_SESSION_ID": "test", "CLAUDE_PLUGIN_ROOT": str(plugin_root)}
    )
    marker_status = subprocess.run(
        [
            sys.executable,
            str(plugin_root / "bin/release_append_marker.py"),
            "is-valid",
            "--branch",
            "main",
            "--last-tag",
            "v1",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert marker_status.stdout.strip() == "true"
    if phase == "before-publish":
        stage = publisher.begin(tmp_path, "main", "CHANGELOG.md", frozen_head, marker, range_start=marker)
        (stage / "DRAFT.md").write_text("candidate\n", encoding="utf-8", newline="\n")
        _stage_marker(stage)
        (tmp_path / "release-append-stage-test").write_text(_bash_path(stage) + "\n", encoding="utf-8", newline="\n")
        document = (plugin_root / "skills/release/modes/release-draft-template.md").read_text(encoding="utf-8")
        block = next(
            block
            for block in re.findall(r"```bash\n(.*?)```", document, re.DOTALL)
            if 'release_append_publish.py" publish' in block
        )
    else:
        document = (plugin_root / "skills/release/SKILL.md").read_text(encoding="utf-8")
        block = next(
            block
            for block in re.findall(r"```bash\n(.*?)```", document, re.DOTALL)
            if 'release_append_publish.py" begin' in block
        )
    subprocess.run(["git", "tag", "v2", frozen_head], cwd=tmp_path, check=True, capture_output=True)
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip() == frozen_head
    completed = subprocess.run(
        [shutil.which("bash"), "-c", block],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert completed.returncode != 0, completed.stdout
    assert (tmp_path / "DRAFT.md").read_text(encoding="utf-8") == "original\n"
    assert (tmp_path / _MAIN_MARKER).read_text(encoding="utf-8") == marker + "\n"


@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("git") is None, reason="Requires Bash and Git")
def test_marker_refresh_rejects_new_commit_after_gather(tmp_path: Path) -> None:
    """The instruction consumer must check frozen HEAD before any marker write."""
    frozen_head = _init_git(tmp_path)
    (tmp_path / "source.txt").write_text("later source\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "add", "source.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "later"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    skill = (Path(__file__).resolve().parents[1] / "skills/release/modes/release-draft-template.md").read_text(
        encoding="utf-8"
    )
    marker_block = next(
        block
        for block in re.findall(r"```bash\n(.*?)```", skill, re.DOTALL)
        if "HEAD changed after append gather" in block
    )
    (tmp_path / "release-setup-test").mkdir()
    (tmp_path / "release-setup-test/BRANCH").write_text("main\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-do-append-test").write_text("true\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-append-head-test").write_text(frozen_head + "\n", encoding="utf-8", newline="\n")
    environment = os.environ.copy()
    environment.update({"TMPDIR": str(tmp_path), "CLAUDE_CODE_SESSION_ID": "test"})
    completed = subprocess.run(
        [shutil.which("bash"), "-c", marker_block],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0
    assert "HEAD changed after append gather" in completed.stderr
    assert not (tmp_path / _MAIN_MARKER).exists()


def test_notes_marker_refresh_uses_frozen_range_endpoint_in_candidate() -> None:
    """Both plain and append notes stage only the gathered endpoint marker."""
    skill = (Path(__file__).resolve().parents[1] / "skills/release/modes/release-draft-template.md").read_text(
        encoding="utf-8"
    )
    marker_block = next(
        block
        for block in re.findall(r"```bash\n(.*?)```", skill, re.DOTALL)
        if "HEAD changed after append gather" in block
    )
    assert '[ "$MARKER_SHA" = "$FROZEN_END" ]' in marker_block
    assert marker_block.count('--sha "$MARKER_SHA"') == 1
    assert marker_block.count('--receipt "$RECEIPT_FILE"') == 1
    assert '--marker-dir "$APPEND_STAGE/.temp"' in marker_block


@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("git") is None, reason="Requires Bash and Git")
def test_plain_notes_marker_tracks_completed_explicit_range_endpoint(tmp_path: Path) -> None:
    """An explicit notes range ending before HEAD must leave later commits for append."""
    base = _init_git(tmp_path)
    subprocess.run(["git", "branch", "-M", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "tag", "v1", base], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "represented"], cwd=tmp_path, check=True)
    represented = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    subprocess.run(["git", "tag", "v2", represented], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "later"], cwd=tmp_path, check=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    stage = publisher.begin(
        tmp_path,
        "main",
        "CHANGELOG.md",
        head,
        expected_branch_ref="main",
        marker_sha=represented,
        range_start=base,
    )
    (stage / "DRAFT.md").write_text("notes for v1..v2\n", encoding="utf-8", newline="\n")
    (stage / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8", newline="\n")
    setup = tmp_path / "release-setup-test"
    setup.mkdir()
    (setup / "BRANCH").write_text("main\n", encoding="utf-8", newline="\n")
    (setup / "BRANCH_REF").write_text("main\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-do-append-test").write_text("false\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-mode-test").write_text("notes\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-mode-test").write_text("notes\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-range-test").write_text(f"{base}..{represented}\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-append-head-test").write_text(head + "\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-append-end-test").write_text(represented + "\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-append-branch-test").write_text("main\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-append-tags-test").write_bytes(publisher._tag_state(tmp_path))
    (tmp_path / "release-append-stage-test").write_text(_bash_path(stage) + "\n", encoding="utf-8", newline="\n")
    skill = (_SCRIPT.parents[1] / "skills/release/modes/release-draft-template.md").read_text(encoding="utf-8")
    marker_block = next(
        block
        for block in re.findall(r"```bash\n(.*?)```", skill, re.DOTALL)
        if "HEAD changed after append gather" in block
    )
    environment = os.environ.copy()
    environment.update(
        {"TMPDIR": str(tmp_path), "CLAUDE_CODE_SESSION_ID": "test", "CLAUDE_PLUGIN_ROOT": str(_SCRIPT.parents[1])}
    )

    completed = subprocess.run(
        [shutil.which("bash"), "-c", marker_block],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert not (tmp_path / _MAIN_MARKER).exists()
    publish_block = next(
        block
        for block in re.findall(r"```bash\n(.*?)```", skill, re.DOTALL)
        if 'release_append_publish.py" publish' in block
    )
    published = subprocess.run(
        [shutil.which("bash"), "-c", publish_block],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert published.returncode == 0, published.stderr
    assert (tmp_path / _MAIN_MARKER).read_text(encoding="utf-8") == represented + "\n"
    resolved = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT.parents[1] / "bin/release_append_marker.py"),
            "resolve",
            "--branch",
            "main",
            "--last-tag",
            "v1",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert resolved.stdout.strip() == f"{represented}..HEAD"
    assert head != represented


@pytest.mark.skipif(shutil.which("bash") is None, reason="Release marker refresh uses Bash")
def test_plain_notes_rejects_head_change_after_gather(tmp_path: Path) -> None:
    """A symbolic HEAD range cannot certify a commit landed after notes were drafted."""
    base = _init_git(tmp_path)
    subprocess.run(["git", "branch", "-M", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "tag", "v1", base], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "represented"], cwd=tmp_path, check=True)
    frozen = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    (tmp_path / "DRAFT.md").write_text("notes through frozen HEAD\n", encoding="utf-8", newline="\n")
    setup = tmp_path / "release-setup-test"
    setup.mkdir()
    (setup / "BRANCH").write_text("main\n", encoding="utf-8", newline="\n")
    (setup / "BRANCH_REF").write_text("main\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-do-append-test").write_text("false\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-range-test").write_text("v1..HEAD\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-append-head-test").write_text(frozen + "\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "later"], cwd=tmp_path, check=True)
    skill = (_SCRIPT.parents[1] / "skills/release/modes/release-draft-template.md").read_text(encoding="utf-8")
    marker_block = next(
        block
        for block in re.findall(r"```bash\n(.*?)```", skill, re.DOTALL)
        if "HEAD changed after append gather" in block
    )
    environment = os.environ.copy()
    environment.update(
        {"TMPDIR": str(tmp_path), "CLAUDE_CODE_SESSION_ID": "test", "CLAUDE_PLUGIN_ROOT": str(_SCRIPT.parents[1])}
    )

    completed = subprocess.run(
        [shutil.which("bash"), "-c", marker_block],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert not (tmp_path / _MAIN_MARKER).exists()


@pytest.mark.skipif(shutil.which("bash") is None, reason="Release gather uses Bash")
def test_plain_notes_rejects_range_disjoint_from_saved_marker(tmp_path: Path) -> None:
    """A plain explicit range may not skip commits after a usable saved marker."""
    base = _init_git(tmp_path)
    subprocess.run(["git", "branch", "-M", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "tag", "v1", base], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "already drafted"], cwd=tmp_path, check=True)
    old_marker = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "gap"], cwd=tmp_path, check=True)
    skipped_start = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "selected"], cwd=tmp_path, check=True)
    _write_state(tmp_path, _MAIN_MARKER, old_marker + "\n")
    setup = tmp_path / "release-setup-test"
    setup.mkdir()
    (setup / "LAST_TAG").write_text("v1\n", encoding="utf-8", newline="\n")
    (setup / "BRANCH").write_text("main\n", encoding="utf-8", newline="\n")
    (setup / "BRANCH_REF").write_text("main\n", encoding="utf-8", newline="\n")
    (setup / "BRANCH_KEY").write_text(marker_api.branch_state_key("main") + "\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-do-append-test").write_text("false\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-mode-test").write_text("notes\n", encoding="utf-8", newline="\n")
    skill = (_SCRIPT.parents[1] / "skills/release/SKILL.md").read_text(encoding="utf-8")
    gather_block = next(
        block
        for block in re.findall(r"```bash\n(.*?)```", skill, re.DOTALL)
        if "append range does not start at saved marker" in block
    )
    prefix = gather_block.split("# persist (Check 41)", 1)[0]
    environment = os.environ.copy()
    environment.update(
        {"TMPDIR": str(tmp_path), "CLAUDE_CODE_SESSION_ID": "test", "CLAUDE_PLUGIN_ROOT": str(_SCRIPT.parents[1])}
    )

    completed = subprocess.run(
        [shutil.which("bash"), "-c", f'RANGE="{skipped_start}..HEAD"\n{prefix}'],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert (tmp_path / _MAIN_MARKER).read_text(encoding="utf-8") == old_marker + "\n"


@pytest.mark.skipif(shutil.which("bash") is None, reason="Release gather uses Bash")
def test_plain_notes_rejects_range_ending_before_saved_marker(tmp_path: Path) -> None:
    """A historical range must not rewind the marker and replay represented commits."""
    base = _init_git(tmp_path)
    subprocess.run(["git", "branch", "-M", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "tag", "v1", base], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "range endpoint"], cwd=tmp_path, check=True)
    stale_end = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "already drafted"], cwd=tmp_path, check=True)
    old_marker = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "later"], cwd=tmp_path, check=True)
    _write_state(tmp_path, _MAIN_MARKER, old_marker + "\n")
    setup = tmp_path / "release-setup-test"
    setup.mkdir()
    (setup / "LAST_TAG").write_text("v1\n", encoding="utf-8", newline="\n")
    (setup / "BRANCH").write_text("main\n", encoding="utf-8", newline="\n")
    (setup / "BRANCH_REF").write_text("main\n", encoding="utf-8", newline="\n")
    (setup / "BRANCH_KEY").write_text(marker_api.branch_state_key("main") + "\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-do-append-test").write_text("false\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-mode-test").write_text("notes\n", encoding="utf-8", newline="\n")
    skill = (_SCRIPT.parents[1] / "skills/release/SKILL.md").read_text(encoding="utf-8")
    gather_block = next(
        block
        for block in re.findall(r"```bash\n(.*?)```", skill, re.DOTALL)
        if "append range does not start at saved marker" in block
    )
    prefix = gather_block.split("# persist (Check 41)", 1)[0]
    environment = os.environ.copy()
    environment.update(
        {"TMPDIR": str(tmp_path), "CLAUDE_CODE_SESSION_ID": "test", "CLAUDE_PLUGIN_ROOT": str(_SCRIPT.parents[1])}
    )

    completed = subprocess.run(
        [shutil.which("bash"), "-c", f'RANGE="v1..{stale_end}"\n{prefix}'],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert (tmp_path / _MAIN_MARKER).read_text(encoding="utf-8") == old_marker + "\n"


def test_begin_rejects_completed_endpoint_before_saved_marker(tmp_path: Path) -> None:
    """Direct candidate staging must not permit a marker rewind."""
    _init_git(tmp_path)
    subprocess.run(["git", "branch", "-M", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "historical endpoint"], cwd=tmp_path, check=True)
    stale_end = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "already drafted"], cwd=tmp_path, check=True)
    old_marker = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    _write_state(tmp_path, _MAIN_MARKER, old_marker + "\n")

    with pytest.raises(ValueError, match="before saved marker"):
        publisher.begin(
            tmp_path, "main", "CHANGELOG.md", expected_start=old_marker, marker_sha=stale_end, range_start="HEAD"
        )

    assert (tmp_path / _MAIN_MARKER).read_text(encoding="utf-8") == old_marker + "\n"
    assert not publisher.journal_path(tmp_path, "main").exists()


@pytest.mark.skipif(shutil.which("bash") is None, reason="Release staging uses Bash")
def test_plain_notes_stages_before_artifact_edits(tmp_path: Path) -> None:
    """A plain draft and marker must share a recoverable candidate before edits."""
    head = _init_git(tmp_path)
    subprocess.run(["git", "branch", "-M", "main"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "DRAFT.md").write_bytes(b"ORIGINAL\n")
    (tmp_path / "CHANGELOG.md").write_bytes(b"# Changelog\n")
    summary = ".temp/output-release-summary-main-2026-09-26.md"
    migration = ".temp/output-release-migration-main-2026-09-26.md"
    _write_state(tmp_path, summary, "OLD SUMMARY\n")
    _write_state(tmp_path, migration, "OLD MIGRATION\n")
    setup = tmp_path / "release-setup-test"
    setup.mkdir()
    (setup / "BRANCH").write_text("main\n", encoding="utf-8", newline="\n")
    (setup / "BRANCH_REF").write_text("main\n", encoding="utf-8", newline="\n")
    (setup / "DATE").write_text("2026-09-26\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-do-append-test").write_text("false\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-do-summary-test").write_text("true\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-do-migration-test").write_text("true\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-append-head-test").write_text(head + "\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-mode-test").write_text("notes\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-append-start-test").write_text(head + "\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-append-end-test").write_text(head + "\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-append-branch-test").write_text("main\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-append-tags-test").write_bytes(b"")
    skill = (_SCRIPT.parents[1] / "skills/release/SKILL.md").read_text(encoding="utf-8")
    stage_block = next(
        block
        for block in re.findall(r"```bash\n(.*?)```", skill, re.DOTALL)
        if 'release_append_publish.py" begin' in block
    )
    environment = os.environ.copy()
    environment.update(
        {"TMPDIR": str(tmp_path), "CLAUDE_CODE_SESSION_ID": "test", "CLAUDE_PLUGIN_ROOT": str(_SCRIPT.parents[1])}
    )

    completed = subprocess.run(
        [shutil.which("bash"), "-c", stage_block],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    stage_text = (tmp_path / "release-append-stage-test").read_text(encoding="utf-8").strip()
    assert stage_text, (
        f"plain notes must create a candidate before editing artifacts: {completed.stdout} {completed.stderr}"
    )
    stage = _native_path(stage_text)
    assert (stage / "DRAFT.md").read_bytes() == b"ORIGINAL\n"
    assert (tmp_path / "DRAFT.md").read_bytes() == b"ORIGINAL\n"
    assert (stage / summary).read_bytes() == b"OLD SUMMARY\n"
    assert (stage / migration).read_bytes() == b"OLD MIGRATION\n"
    metadata = json.loads((stage / "candidate.json").read_text(encoding="utf-8"))
    assert metadata["extra_outputs"] == [summary, migration]


@pytest.mark.parametrize("interruption", ["draft-edit", "new-commit"])
def test_interrupted_completion_receipt_cannot_advance_marker_after_change(tmp_path: Path, interruption: str) -> None:
    """A receipt stranded between truth review and write expires on draft or source change."""
    base = _init_git(tmp_path)
    subprocess.run(["git", "branch", "-M", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "represented"], cwd=tmp_path, check=True)
    represented = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    draft = tmp_path / "DRAFT.md"
    draft.write_bytes(b"completed notes\n")
    marker = tmp_path / _MAIN_MARKER
    marker.parent.mkdir(parents=True)
    marker.write_bytes(b"OLD\n")
    completed_range = tmp_path / "completed-range"
    completed_range.write_text(f"{base}..{represented}\n", encoding="utf-8", newline="\n")
    receipt = tmp_path / "receipt.json"
    marker_script = _SCRIPT.parents[1] / "bin/release_append_marker.py"
    subprocess.run(
        [
            sys.executable,
            str(marker_script),
            "receipt",
            "--branch",
            "main",
            "--range-file",
            str(completed_range),
            "--draft",
            str(draft),
            "--output",
            str(receipt),
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    if interruption == "draft-edit":
        draft.write_bytes(b"foreign edit\n")
    else:
        subprocess.run(["git", "commit", "--allow-empty", "-qm", "later"], cwd=tmp_path, check=True)

    completed = subprocess.run(
        [
            sys.executable,
            str(marker_script),
            "write",
            "--branch",
            "main",
            "--sha",
            represented,
            "--receipt",
            str(receipt),
            "--draft",
            str(draft),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert marker.read_bytes() == b"OLD\n"
    assert receipt.exists()


def test_direct_begin_requires_a_completed_range_start(tmp_path: Path) -> None:
    """A direct publisher cannot certify an endpoint without the selected range start."""
    base = _init_git(tmp_path)
    subprocess.run(["git", "branch", "-M", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "undrafted"], cwd=tmp_path, check=True)
    late = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "drafted"], cwd=tmp_path, check=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    tags = tmp_path / "tags.txt"
    tags.write_bytes(b"")
    (tmp_path / "DRAFT.md").write_bytes(b"Notes only for drafted commit\n")
    (tmp_path / "CHANGELOG.md").write_bytes(b"old\n")
    script = _SCRIPT.parents[1] / "bin/release_append_publish.py"
    command = [
        sys.executable,
        str(script),
        "begin",
        "--branch",
        "main",
        "--changelog",
        "CHANGELOG.md",
        "--head-sha",
        head,
        "--marker-sha",
        head,
        "--branch-ref",
        "main",
        "--tag-state-file",
        str(tags),
    ]

    missing = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True)
    invalid = subprocess.run([*command, "--range-start", "f" * 40], cwd=tmp_path, capture_output=True, text=True)
    skipped = subprocess.run([*command, "--range-start", late], cwd=tmp_path, capture_output=True, text=True)
    assert missing.returncode != 0
    assert invalid.returncode == 1
    assert skipped.returncode == 1
    assert not list((tmp_path / ".temp").glob("release-append-stage-*"))
    assert (
        subprocess.check_output(["git", "rev-list", "--count", f"{base}..{head}"], cwd=tmp_path, text=True).strip()
        == "2"
    )

    valid = subprocess.run([*command, "--range-start", base], cwd=tmp_path, capture_output=True, text=True)
    assert valid.returncode == 0, valid.stderr
    stage = _native_path(valid.stdout.strip())
    candidate_path = stage / "candidate.json"
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    assert candidate["range_start_sha"] == base
    marker = marker_api.state_relative("main", "marker")
    _write_state(stage, marker, head + "\n")
    candidate["range_start_sha"] = "f" * 40
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8", newline="\n")
    seal = subprocess.run(
        [sys.executable, str(script), "seal", "--branch", "main", "--stage", str(stage)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert seal.returncode == 1
    candidate["range_start_sha"] = base
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8", newline="\n")
    seal = subprocess.run(
        [sys.executable, str(script), "seal", "--branch", "main", "--stage", str(stage)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert seal.returncode == 0, seal.stderr
    candidate["range_start_sha"] = "f" * 40
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8", newline="\n")
    publish = subprocess.run(
        [sys.executable, str(script), "publish", "--branch", "main", "--stage", str(stage)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert publish.returncode == 1
    assert not (tmp_path / marker).exists()

    subprocess.run(["git", "tag", "v2", late], cwd=tmp_path, check=True)
    tags.write_bytes(publisher._tag_state(tmp_path))
    tagged = subprocess.run(
        [*command, "--last-tag", "v2", "--range-start", late], cwd=tmp_path, capture_output=True, text=True
    )
    assert tagged.returncode == 0, tagged.stderr
    tagged_metadata = json.loads((_native_path(tagged.stdout.strip()) / "candidate.json").read_text(encoding="utf-8"))
    assert tagged_metadata["baseline_sha"] == late


def test_direct_begin_cannot_skip_a_saved_marker(tmp_path: Path) -> None:
    """An omitted saved-marker argument cannot hide pending commits from a direct caller."""
    base = _init_git(tmp_path)
    subprocess.run(["git", "branch", "-M", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "pending"], cwd=tmp_path, check=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    marker = marker_api.state_relative("main", "marker")
    _write_state(tmp_path, marker, base + "\n")
    tags = tmp_path / "tags.txt"
    tags.write_bytes(b"")
    (tmp_path / "DRAFT.md").write_bytes(b"Notes omit pending commit\n")
    (tmp_path / "CHANGELOG.md").write_bytes(b"old\n")
    script = _SCRIPT.parents[1] / "bin/release_append_publish.py"
    command = [
        sys.executable,
        str(script),
        "begin",
        "--branch",
        "main",
        "--changelog",
        "CHANGELOG.md",
        "--head-sha",
        head,
        "--marker-sha",
        head,
        "--range-start",
        head,
        "--branch-ref",
        "main",
        "--tag-state-file",
        str(tags),
    ]

    skipped = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True)
    assert skipped.returncode == 1
    assert (tmp_path / marker).read_bytes() == (base + "\n").encode()


def test_direct_begin_cannot_publish_behind_a_saved_marker(tmp_path: Path) -> None:
    """An omitted continuity argument cannot downgrade a valid live marker."""
    base = _init_git(tmp_path)
    subprocess.run(["git", "branch", "-M", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "represented"], cwd=tmp_path, check=True)
    saved = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    _write_state(tmp_path, marker_api.state_relative("main", "marker"), saved + "\n")

    with pytest.raises(ValueError, match="saved marker|endpoint"):
        publisher.begin(tmp_path, "main", "CHANGELOG.md", marker_sha=base, range_start=base)
    assert (tmp_path / marker_api.state_relative("main", "marker")).read_bytes() == (saved + "\n").encode()


def test_tag_supersession_requires_selected_tag_baseline(tmp_path: Path) -> None:
    """A tag replacing an older marker cannot leave later commits out of notes."""
    saved = _init_git(tmp_path)
    subprocess.run(["git", "branch", "-M", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "tagged"], cwd=tmp_path, check=True)
    tagged = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    subprocess.run(["git", "tag", "v2", tagged], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "omitted"], cwd=tmp_path, check=True)
    omitted = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "drafted"], cwd=tmp_path, check=True)
    endpoint = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    _write_state(tmp_path, marker_api.state_relative("main", "marker"), saved + "\n")

    with pytest.raises(ValueError, match="tag|baseline|start"):
        publisher.begin(tmp_path, "main", "CHANGELOG.md", marker_sha=endpoint, range_start=omitted, last_tag="v2")
    stage = publisher.begin(tmp_path, "main", "CHANGELOG.md", marker_sha=endpoint, range_start=tagged, last_tag="v2")
    assert json.loads((stage / "candidate.json").read_text(encoding="utf-8"))["range_start_sha"] == tagged


@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("git") is None, reason="Requires Bash and Git")
def test_skill_stages_tag_supersession_from_selected_tag(tmp_path: Path) -> None:
    """The ordinary release shell path may resume at a tag that superseded its marker."""
    saved = _init_git(tmp_path)
    subprocess.run(["git", "branch", "-M", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "tagged"], cwd=tmp_path, check=True)
    tagged = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    subprocess.run(["git", "tag", "v2", tagged], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "drafted"], cwd=tmp_path, check=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    _write_state(tmp_path, marker_api.state_relative("main", "marker"), saved + "\n")
    (tmp_path / "DRAFT.md").write_bytes(b"earlier notes\n")
    (tmp_path / "CHANGELOG.md").write_bytes(b"earlier changelog\n")
    setup = tmp_path / "release-setup-tagged"
    setup.mkdir()
    for name, value in {
        "BRANCH": "main",
        "BRANCH_REF": "main",
        "BRANCH_KEY": marker_api.branch_state_key("main"),
        "LAST_TAG": "v2",
        "DATE": "2026-09-26",
    }.items():
        (setup / name).write_text(value + "\n", encoding="utf-8", newline="\n")
    for name, value in {
        "release-do-append-tagged": "true",
        "release-mode-tagged": "notes",
        "release-append-head-tagged": head,
        "release-append-start-tagged": tagged,
        "release-append-end-tagged": head,
        "release-append-branch-tagged": "main",
    }.items():
        (tmp_path / name).write_text(value + "\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-append-tags-tagged").write_bytes(publisher._tag_state(tmp_path))
    plugin_root = Path(__file__).resolve().parents[1]
    skill = (plugin_root / "skills/release/SKILL.md").read_text(encoding="utf-8")
    block = next(
        block
        for block in re.findall(r"```bash\n(.*?)```", skill, re.DOTALL)
        if 'release_append_publish.py" begin' in block
    )
    environment = os.environ.copy()
    environment.update(
        {"TMPDIR": str(tmp_path), "CLAUDE_CODE_SESSION_ID": "tagged", "CLAUDE_PLUGIN_ROOT": str(plugin_root)}
    )

    completed = subprocess.run(
        [shutil.which("bash"), "-c", block], cwd=tmp_path, env=environment, capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
    stage = _native_path((tmp_path / "release-append-stage-tagged").read_text(encoding="utf-8").strip())
    metadata = json.loads((stage / "candidate.json").read_text(encoding="utf-8"))
    assert metadata["baseline_sha"] == tagged
    assert metadata["range_start_sha"] == tagged
    assert (tmp_path / marker_api.state_relative("main", "marker")).read_bytes() == (saved + "\n").encode()
    (tmp_path / "release-range-tagged").write_text(f"{tagged}..{head}\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-marker-valid-tagged").write_text("false\n", encoding="utf-8", newline="\n")
    template = (plugin_root / "skills/release/modes/release-draft-template.md").read_text(encoding="utf-8")
    refresh = next(
        block
        for block in re.findall(r"```bash\n(.*?)```", template, re.DOTALL)
        if 'release_append_marker.py" receipt' in block
    )
    refreshed = subprocess.run(
        [shutil.which("bash"), "-c", refresh],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert refreshed.returncode == 0, refreshed.stderr
    assert (stage / marker_api.state_relative("main", "marker")).read_bytes() == (head + "\n").encode()
    assert (tmp_path / marker_api.state_relative("main", "marker")).read_bytes() == (saved + "\n").encode()
    publisher.publish(tmp_path, "main", stage)
    assert (tmp_path / marker_api.state_relative("main", "marker")).read_bytes() == (head + "\n").encode()


@pytest.mark.parametrize("interruption", ["before-marker", "after-marker"])
def test_plain_publication_recovers_exact_artifacts_without_replaying_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interruption: str
) -> None:
    """A plain candidate publishes once even when interrupted around its marker."""
    stage, old_marker, marker_sha, outputs = _plain_candidate(tmp_path)
    original_replace = publisher._replace_candidate

    def interrupt(root: Path, staged: Path, relative: str) -> None:
        """Stop immediately before or after replacing the final marker."""
        if relative == _MAIN_MARKER and interruption == "before-marker":
            raise OSError("interrupted before marker")
        original_replace(root, staged, relative)
        if relative == _MAIN_MARKER and interruption == "after-marker":
            raise OSError("interrupted after marker")

    monkeypatch.setattr(publisher, "_replace_candidate", interrupt)
    with pytest.raises(OSError, match="interrupted"):
        publisher.publish(tmp_path, "main", stage)
    assert publisher.journal_path(tmp_path, "main").exists()
    assert (tmp_path / _MAIN_MARKER).read_text(encoding="utf-8") == (
        old_marker + "\n" if interruption == "before-marker" else marker_sha + "\n"
    )

    monkeypatch.setattr(publisher, "_replace_candidate", original_replace)
    publisher.recover(tmp_path, "main")
    assert (tmp_path / "DRAFT.md").read_bytes() == b"COMPLETED\n"
    assert (tmp_path / "CHANGELOG.md").read_bytes() == b"# Changelog\nCompleted\n"
    assert (tmp_path / outputs[0]).read_bytes() == b"New summary\n"
    assert (tmp_path / outputs[1]).read_bytes() == b"New migration\n"
    assert (tmp_path / _MAIN_MARKER).read_bytes() == (marker_sha + "\n").encode()
    assert not publisher.journal_path(tmp_path, "main").exists()
    with pytest.raises(FileNotFoundError):
        publisher.recover(tmp_path, "main")


def test_plain_recovery_preserves_foreign_output_edit_and_pending_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user edit to a dated output stops recovery without overwriting its bytes."""
    stage, old_marker, _marker_sha, outputs = _plain_candidate(tmp_path)
    original_replace = publisher._replace_candidate

    def interrupt(root: Path, staged: Path, relative: str) -> None:
        """Leave a partial publication before the marker replacement."""
        if relative == _MAIN_MARKER:
            raise OSError("interrupted")
        original_replace(root, staged, relative)

    monkeypatch.setattr(publisher, "_replace_candidate", interrupt)
    with pytest.raises(OSError, match="interrupted"):
        publisher.publish(tmp_path, "main", stage)
    foreign = b"USER CHANGE\r\n"
    (tmp_path / outputs[0]).write_bytes(foreign)
    monkeypatch.setattr(publisher, "_replace_candidate", original_replace)

    with pytest.raises(ValueError, match="artifact changed outside append publication"):
        publisher.recover(tmp_path, "main")
    assert (tmp_path / outputs[0]).read_bytes() == foreign
    assert (tmp_path / _MAIN_MARKER).read_bytes() == (old_marker + "\n").encode()
    assert publisher.journal_path(tmp_path, "main").exists()


def test_plain_revision_can_publish_with_unchanged_completed_marker(tmp_path: Path) -> None:
    """Revising already drafted prose preserves the same completed range baseline."""
    stage, _old_marker, marker_sha, outputs = _plain_candidate(tmp_path)
    publisher.publish(tmp_path, "main", stage)
    revision = publisher.begin(
        tmp_path, "main", "CHANGELOG.md", marker_sha=marker_sha, extra_outputs=outputs, range_start=marker_sha
    )
    (revision / "DRAFT.md").write_bytes(b"REVISED COMPLETED NOTES\n")
    publisher.seal(tmp_path, "main", revision)

    publisher.publish(tmp_path, "main", revision)

    assert (tmp_path / "DRAFT.md").read_bytes() == b"REVISED COMPLETED NOTES\n"
    assert (tmp_path / _MAIN_MARKER).read_bytes() == (marker_sha + "\n").encode()
    assert not publisher.journal_path(tmp_path, "main").exists()


def test_plain_source_drift_rolls_back_exact_original_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A commit during plain promotion restores originals instead of certifying stale notes."""
    stage, old_marker, _marker_sha, outputs = _plain_candidate(tmp_path)
    original_replace = publisher._replace_candidate
    changed_source = False

    def change_source(root: Path, staged: Path, relative: str) -> None:
        """Move HEAD after the first live artifact replacement."""
        nonlocal changed_source
        original_replace(root, staged, relative)
        if not changed_source:
            changed_source = True
            subprocess.run(["git", "commit", "--allow-empty", "-qm", "later still"], cwd=root, check=True)

    monkeypatch.setattr(publisher, "_replace_candidate", change_source)
    with pytest.raises(publisher.SourceDriftError):
        publisher.publish(tmp_path, "main", stage)

    assert (tmp_path / "DRAFT.md").read_bytes() == b"ORIGINAL\r\n"
    assert (tmp_path / "CHANGELOG.md").read_bytes() == b"# Changelog\r\n"
    assert (tmp_path / outputs[0]).read_bytes() == b"Prior summary\n"
    assert (tmp_path / outputs[1]).read_bytes() == b"Prior migration\n"
    assert (tmp_path / _MAIN_MARKER).read_bytes() == (old_marker + "\n").encode()
    assert not publisher.journal_path(tmp_path, "main").exists()


def test_plain_publish_refuses_foreign_dated_output_before_first_live_write(tmp_path: Path) -> None:
    """A user edit between staging and publication survives without any partial promotion."""
    stage, old_marker, _marker_sha, outputs = _plain_candidate(tmp_path)
    foreign = b"USER SUMMARY\r\n"
    (tmp_path / outputs[0]).write_bytes(foreign)

    with pytest.raises(ValueError, match="artifact changed outside append staging"):
        publisher.publish(tmp_path, "main", stage)

    assert (tmp_path / "DRAFT.md").read_bytes() == b"ORIGINAL\r\n"
    assert (tmp_path / outputs[0]).read_bytes() == foreign
    assert (tmp_path / _MAIN_MARKER).read_bytes() == (old_marker + "\n").encode()
    assert not publisher.journal_path(tmp_path, "main").exists()


@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("git") is None, reason="Requires Bash and Git")
@pytest.mark.parametrize("drift", ["none", "new-commit", "branch-switch", "new-tag"])
def test_fallback_draft_rechecks_gather_source_before_live_write(tmp_path: Path, drift: str) -> None:
    """An invalid-marker append edits only its candidate after the source check."""
    frozen_head = _init_git(tmp_path)
    (tmp_path / "DRAFT.md").write_text("user draft\n", encoding="utf-8", newline="\n")
    (tmp_path / ".temp").mkdir()
    stage = publisher.begin(
        tmp_path, "main", "CHANGELOG.md", expected_head=frozen_head, expected_branch_ref="main", range_start="HEAD"
    )
    (tmp_path / "release-append-stage-test").write_text(_bash_path(stage) + "\n", encoding="utf-8", newline="\n")
    setup = tmp_path / "release-setup-test"
    setup.mkdir()
    (setup / "BRANCH").write_text("main\n", encoding="utf-8", newline="\n")
    (setup / "BRANCH_REF").write_text("main\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-do-append-test").write_text("true\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-append-head-test").write_text(frozen_head + "\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-append-branch-test").write_text("main\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-append-tags-test").write_bytes(b"")
    if drift == "new-commit":
        subprocess.run(["git", "commit", "--allow-empty", "-qm", "later"], cwd=tmp_path, check=True)
    elif drift == "branch-switch":
        subprocess.run(["git", "switch", "-q", "-c", "other"], cwd=tmp_path, check=True)
    elif drift == "new-tag":
        subprocess.run(["git", "tag", "v2"], cwd=tmp_path, check=True)

    skill = (Path(__file__).resolve().parents[1] / "skills/release/modes/release-draft-template.md").read_text(
        encoding="utf-8"
    )
    draft_gate = next(
        block
        for block in re.findall(r"```bash\n(.*?)```", skill, re.DOTALL)
        if "Recheck source immediately before fallback draft write" in block
    )
    environment = os.environ.copy()
    environment.update(
        {
            "TMPDIR": str(tmp_path),
            "CLAUDE_CODE_SESSION_ID": "test",
            "CLAUDE_PLUGIN_ROOT": str(_SCRIPT.parents[1]),
        }
    )
    completed = subprocess.run(
        [shutil.which("bash"), "-c", draft_gate + '\nprintf "replacement\\n" > "$APPEND_STAGE/DRAFT.md"\n'],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if drift == "none":
        assert completed.returncode == 0, completed.stderr
        assert (stage / "DRAFT.md").read_text(encoding="utf-8") == "replacement\n"
    else:
        assert completed.returncode != 0, completed.stdout
        assert ("changed after release gather" if drift != "new-tag" else "changed after gather") in completed.stderr
        assert (stage / "DRAFT.md").read_text(encoding="utf-8") == "user draft\n"
    assert (tmp_path / "DRAFT.md").read_text(encoding="utf-8") == "user draft\n"


@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("git") is None, reason="Requires Bash and Git")
def test_fallback_changelog_checks_frozen_source_before_first_live_edit(tmp_path: Path) -> None:
    """A changed source must stop the invalid-marker changelog path before publication."""
    frozen_head = _init_git(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text("user changelog\n", encoding="utf-8", newline="\n")
    setup = tmp_path / "release-setup-test"
    setup.mkdir()
    (setup / "BRANCH").write_text("main\n", encoding="utf-8", newline="\n")
    (setup / "BRANCH_REF").write_text("main\n", encoding="utf-8", newline="\n")
    (setup / "LAST_TAG").write_text("\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-do-append-test").write_text("true\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-mode-test").write_text("notes\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-append-head-test").write_text(frozen_head + "\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-append-start-test").write_text(frozen_head + "\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-append-end-test").write_text(frozen_head + "\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-append-branch-test").write_text("main\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-append-tags-test").write_bytes(b"")
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "later"], cwd=tmp_path, check=True)
    skill = (Path(__file__).resolve().parents[1] / "skills/release/SKILL.md").read_text(encoding="utf-8")
    prewrite = next(
        block
        for block in re.findall(r"```bash\n(.*?)```", skill, re.DOTALL)
        if 'EDIT_CHANGELOG_FILE="$CHANGELOG_FILE"' in block
    )
    environment = os.environ.copy()
    environment.update(
        {
            "TMPDIR": str(tmp_path),
            "CLAUDE_CODE_SESSION_ID": "test",
            "CLAUDE_PLUGIN_ROOT": str(_SCRIPT.parents[1]),
            "CHANGELOG_FILE": "CHANGELOG.md",
        }
    )
    completed = subprocess.run(
        [shutil.which("bash"), "-c", prewrite + '\nprintf "replacement\\n" > CHANGELOG.md\n'],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0, completed.stdout
    assert "HEAD changed after release gather" in completed.stderr
    assert (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8") == "user changelog\n"


@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("git") is None, reason="Requires Bash and Git")
def test_missing_marker_fallback_stages_before_ordinary_changelog_write(tmp_path: Path) -> None:
    """A first append must direct the ordinary changelog edit to a candidate copy."""
    frozen_head = _init_git(tmp_path)
    (tmp_path / "DRAFT.md").write_bytes(b"user draft\r\n")
    (tmp_path / "CHANGELOG.md").write_bytes(b"user changelog\r\n")
    setup = tmp_path / "release-setup-test"
    setup.mkdir()
    (setup / "BRANCH").write_text("main\n", encoding="utf-8", newline="\n")
    (setup / "BRANCH_REF").write_text("main\n", encoding="utf-8", newline="\n")
    (setup / "LAST_TAG").write_text("\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-do-append-test").write_text("true\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-mode-test").write_text("notes\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-append-head-test").write_text(frozen_head + "\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-append-start-test").write_text(frozen_head + "\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-append-end-test").write_text(frozen_head + "\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-append-branch-test").write_text("main\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-append-tags-test").write_bytes(b"")
    skill = (Path(__file__).resolve().parents[1] / "skills/release/SKILL.md").read_text(encoding="utf-8")
    prewrite = next(
        block
        for block in re.findall(r"```bash\n(.*?)```", skill, re.DOTALL)
        if 'EDIT_CHANGELOG_FILE="$CHANGELOG_FILE"' in block
    )
    environment = os.environ.copy()
    environment.update(
        {
            "TMPDIR": str(tmp_path),
            "CLAUDE_CODE_SESSION_ID": "test",
            "CLAUDE_PLUGIN_ROOT": str(_SCRIPT.parents[1]),
            "CHANGELOG_FILE": "CHANGELOG.md",
        }
    )
    completed = subprocess.run(
        [shutil.which("bash"), "-c", prewrite + '\nprintf "candidate\\n" >> "$EDIT_CHANGELOG_FILE"\n'],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert (tmp_path / "CHANGELOG.md").read_bytes() == b"user changelog\r\n"
    staged = (tmp_path / "release-append-stage-test").read_text(encoding="utf-8").strip()
    assert staged
    stage = _native_path(staged)
    assert (stage / "CHANGELOG.md").read_bytes() == b"user changelog\r\ncandidate\n"
    (stage / "DRAFT.md").write_bytes(b"candidate draft\n")
    _write_state(stage, _MAIN_MARKER, frozen_head + "\n")
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "later"], cwd=tmp_path, check=True)
    with pytest.raises(ValueError, match="HEAD changed after release gather"):
        publisher.seal(tmp_path, "main", stage)
    assert (tmp_path / "DRAFT.md").read_bytes() == b"user draft\r\n"
    assert (tmp_path / "CHANGELOG.md").read_bytes() == b"user changelog\r\n"
    assert not (tmp_path / _MAIN_MARKER).exists()


@pytest.mark.parametrize("drift", ["none", "new-commit", "branch-switch", "new-tag", "during-promotion"])
def test_missing_marker_fallback_publishes_only_frozen_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    """Final publication preserves all user bytes if source moves after sealing."""
    frozen_head = _init_git(tmp_path)
    (tmp_path / ".temp").mkdir()
    (tmp_path / "DRAFT.md").write_bytes(b"user draft\r\n")
    (tmp_path / "CHANGELOG.md").write_bytes(b"user changelog\r\n")
    stage = publisher.begin(
        tmp_path, "main", "CHANGELOG.md", expected_head=frozen_head, expected_branch_ref="main", range_start="HEAD"
    )
    (stage / "DRAFT.md").write_bytes(b"candidate draft\n")
    (stage / "CHANGELOG.md").write_bytes(b"candidate changelog\n")
    _write_state(stage, _MAIN_MARKER, frozen_head + "\n")
    publisher.seal(tmp_path, "main", stage)
    if drift == "new-commit":
        subprocess.run(["git", "commit", "--allow-empty", "-qm", "later"], cwd=tmp_path, check=True)
    elif drift == "branch-switch":
        subprocess.run(["git", "switch", "-q", "-c", "other"], cwd=tmp_path, check=True)
    elif drift == "new-tag":
        subprocess.run(["git", "tag", "v2"], cwd=tmp_path, check=True)
    elif drift == "during-promotion":
        replace = publisher._replace_candidate

        def move_source_after_first_replace(root: Path, staged: Path, relative: str) -> None:
            """Move HEAD at the narrow boundary between two journaled replacements."""
            replace(root, staged, relative)
            if relative == "CHANGELOG.md":
                subprocess.run(["git", "commit", "--allow-empty", "-qm", "later"], cwd=root, check=True)

        monkeypatch.setattr(publisher, "_replace_candidate", move_source_after_first_replace)
    if drift == "none":
        publisher.publish(tmp_path, "main", stage)
        assert (tmp_path / "DRAFT.md").read_bytes() == b"candidate draft\n"
        assert (tmp_path / "CHANGELOG.md").read_bytes() == b"candidate changelog\n"
        assert (tmp_path / _MAIN_MARKER).read_text(encoding="utf-8") == frozen_head + "\n"
    else:
        with pytest.raises(ValueError, match="changed after (release )?gather"):
            publisher.publish(tmp_path, "main", stage)
        if drift == "during-promotion":
            assert not publisher.journal_path(tmp_path, "main").exists()
            assert (stage / "originals/DRAFT.md").read_bytes() == b"user draft\r\n"
            assert (stage / "originals/CHANGELOG.md").read_bytes() == b"user changelog\r\n"
            assert (tmp_path / "DRAFT.md").read_bytes() == b"user draft\r\n"
        else:
            assert (tmp_path / "DRAFT.md").read_bytes() == b"user draft\r\n"
        assert (tmp_path / "CHANGELOG.md").read_bytes() == b"user changelog\r\n"
        assert not (tmp_path / _MAIN_MARKER).exists()


def test_source_drift_rollback_resumes_after_interruption(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A crash during rollback keeps its decision and restores remaining original bytes on retry."""
    frozen_head = _init_git(tmp_path)
    (tmp_path / ".temp").mkdir()
    (tmp_path / "DRAFT.md").write_bytes(b"original draft\r\n")
    (tmp_path / "CHANGELOG.md").write_bytes(b"original changelog\r\n")
    stage = publisher.begin(tmp_path, "main", "CHANGELOG.md", expected_head=frozen_head, range_start="HEAD")
    (stage / "DRAFT.md").write_bytes(b"candidate draft\n")
    (stage / "CHANGELOG.md").write_bytes(b"candidate changelog\n")
    _write_state(stage, _MAIN_MARKER, frozen_head + "\n")
    publisher.seal(tmp_path, "main", stage)
    replace = publisher._replace_candidate

    def interrupt_rollback(root: Path, staged: Path, relative: str) -> None:
        """Move HEAD after two candidate writes, then crash after one restoration."""
        replace(root, staged, relative)
        if staged == stage and relative == "DRAFT.md":
            subprocess.run(["git", "commit", "--allow-empty", "-qm", "later"], cwd=root, check=True)
        if staged == stage / "originals" and relative == "DRAFT.md":
            raise OSError("rollback interrupted")

    monkeypatch.setattr(publisher, "_replace_candidate", interrupt_rollback)
    with pytest.raises(OSError, match="rollback interrupted"):
        publisher.publish(tmp_path, "main", stage)
    journal = publisher.journal_path(tmp_path, "main")
    assert json.loads(journal.read_text(encoding="utf-8"))["mode"] == "rollback"
    assert (tmp_path / "DRAFT.md").read_bytes() == b"original draft\r\n"
    assert (tmp_path / "CHANGELOG.md").read_bytes() == b"candidate changelog\n"
    assert not (tmp_path / _MAIN_MARKER).exists()
    monkeypatch.setattr(publisher, "_replace_candidate", replace)
    publisher.recover(tmp_path, "main")
    assert (tmp_path / "DRAFT.md").read_bytes() == b"original draft\r\n"
    assert (tmp_path / "CHANGELOG.md").read_bytes() == b"original changelog\r\n"
    assert not journal.exists()
    assert not (tmp_path / _MAIN_MARKER).exists()


def test_source_drift_rollback_preserves_foreign_edit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rollback refuses an artifact changed by a user after the candidate replacement."""
    frozen_head = _init_git(tmp_path)
    (tmp_path / ".temp").mkdir()
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_bytes(b"original changelog\r\n")
    stage = publisher.begin(tmp_path, "main", "CHANGELOG.md", expected_head=frozen_head, range_start="HEAD")
    (stage / "CHANGELOG.md").write_bytes(b"candidate changelog\n")
    _write_state(stage, _MAIN_MARKER, frozen_head + "\n")
    publisher.seal(tmp_path, "main", stage)
    replace = publisher._replace_candidate

    def user_edits_after_candidate(root: Path, staged: Path, relative: str) -> None:
        """Introduce source drift and an unrelated user edit at the promotion boundary."""
        replace(root, staged, relative)
        if staged == stage and relative == "CHANGELOG.md":
            subprocess.run(["git", "commit", "--allow-empty", "-qm", "later"], cwd=root, check=True)
            changelog.write_bytes(b"user edit\r\n")

    monkeypatch.setattr(publisher, "_replace_candidate", user_edits_after_candidate)
    with pytest.raises(ValueError, match="changed outside append publication"):
        publisher.publish(tmp_path, "main", stage)
    journal = publisher.journal_path(tmp_path, "main")
    assert json.loads(journal.read_text(encoding="utf-8"))["mode"] == "rollback"
    assert changelog.read_bytes() == b"user edit\r\n"
    monkeypatch.setattr(publisher, "_replace_candidate", replace)
    with pytest.raises(ValueError, match="changed outside append publication"):
        publisher.recover(tmp_path, "main")
    assert changelog.read_bytes() == b"user edit\r\n"
    assert journal.exists()
    changelog.write_bytes(b"candidate changelog\n")
    publisher.recover(tmp_path, "main")
    assert changelog.read_bytes() == b"original changelog\r\n"
    assert not journal.exists()


def test_recovery_persists_rollback_choice_before_rejecting_foreign_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Source drift commits rollback even when a user edit fails the first preflight."""
    stage = _start(tmp_path)
    (stage / "DRAFT.md").write_text("candidate\n", encoding="utf-8", newline="\n")
    _stage_marker(stage)
    replace = publisher._replace_candidate

    def stop_after_draft(root: Path, staged: Path, relative: str) -> None:
        """Leave one candidate live under a pending promotion journal."""
        replace(root, staged, relative)
        if relative == "DRAFT.md":
            raise OSError("interrupted")

    monkeypatch.setattr(publisher, "_replace_candidate", stop_after_draft)
    with pytest.raises(OSError, match="interrupted"):
        publisher.publish(tmp_path, "main", stage)
    monkeypatch.setattr(publisher, "_replace_candidate", replace)
    draft = tmp_path / "DRAFT.md"
    draft.write_bytes(b"foreign edit\r\n")
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "later"], cwd=tmp_path, check=True)
    journal = publisher.journal_path(tmp_path, "main")
    with pytest.raises(ValueError, match="changed outside append publication"):
        publisher.recover(tmp_path, "main")
    assert json.loads(journal.read_text(encoding="utf-8"))["mode"] == "rollback"
    assert draft.read_bytes() == b"foreign edit\r\n"
    assert (tmp_path / _MAIN_MARKER).read_text(encoding="utf-8") == "old\n"
    draft.write_text("candidate\n", encoding="utf-8", newline="\n")
    publisher.recover(tmp_path, "main")
    assert draft.read_text(encoding="utf-8") == "## Summary\nOriginal\n"
    assert not journal.exists()


def test_source_drift_after_marker_replacement_restores_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The final source check can undo a marker already replaced by this transaction."""
    stage = _start(tmp_path)
    (stage / "DRAFT.md").write_text("candidate\n", encoding="utf-8", newline="\n")
    _stage_marker(stage)
    replace = publisher._replace_candidate

    def move_head_after_marker(root: Path, staged: Path, relative: str) -> None:
        """Move HEAD immediately after the transaction writes its last artifact."""
        replace(root, staged, relative)
        if staged == stage and relative == _MAIN_MARKER:
            subprocess.run(["git", "commit", "--allow-empty", "-qm", "later"], cwd=root, check=True)

    monkeypatch.setattr(publisher, "_replace_candidate", move_head_after_marker)
    with pytest.raises(ValueError, match="HEAD changed after release gather"):
        publisher.publish(tmp_path, "main", stage)
    assert (tmp_path / "DRAFT.md").read_text(encoding="utf-8") == "## Summary\nOriginal\n"
    assert (tmp_path / _MAIN_MARKER).read_text(encoding="utf-8") == "old\n"
    assert not publisher.journal_path(tmp_path, "main").exists()


def test_new_commit_during_promotion_restores_originals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A moving HEAD cannot become the marker after earlier files were published."""
    stage = _start(tmp_path)
    (stage / "DRAFT.md").write_text("candidate\n", encoding="utf-8", newline="\n")
    _stage_marker(stage)
    original_replace = publisher._replace_candidate
    committed = False

    def commit_after_first_replace(root: Path, staged: Path, relative: str) -> None:
        nonlocal committed
        original_replace(root, staged, relative)
        if not committed:
            committed = True
            (root / "source.txt").write_text("later source\n", encoding="utf-8", newline="\n")
            subprocess.run(["git", "add", "source.txt"], cwd=root, check=True, capture_output=True)
            subprocess.run(
                ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "later"],
                cwd=root,
                check=True,
                capture_output=True,
            )

    monkeypatch.setattr(publisher, "_replace_candidate", commit_after_first_replace)
    with pytest.raises(ValueError, match="HEAD changed after release gather"):
        publisher.publish(tmp_path, "main", stage)
    assert not publisher.journal_path(tmp_path, "main").exists()
    assert (tmp_path / "DRAFT.md").read_text(encoding="utf-8") == "## Summary\nOriginal\n"
    assert (tmp_path / _MAIN_MARKER).read_text(encoding="utf-8") == "old\n"
    with pytest.raises(FileNotFoundError):
        publisher.recover(tmp_path, "main")
    assert (tmp_path / _MAIN_MARKER).read_text(encoding="utf-8") == "old\n"


@pytest.mark.parametrize("drift", ["branch", "tag"])
def test_source_change_during_promotion_blocks_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    """The final marker transition must recheck branch and tag refs after artifact writes."""
    stage = _start(tmp_path)
    (stage / "DRAFT.md").write_text("candidate\n", encoding="utf-8", newline="\n")
    _stage_marker(stage)
    original_replace = publisher._replace_candidate

    def change_source_after_draft(root: Path, staged: Path, relative: str) -> None:
        """Introduce a source change only after the first live artifact is written."""
        original_replace(root, staged, relative)
        if staged == stage and relative == "DRAFT.md":
            command = ["git", "switch", "-q", "-c", "other"] if drift == "branch" else ["git", "tag", "v2"]
            subprocess.run(command, cwd=root, check=True, capture_output=True)

    monkeypatch.setattr(publisher, "_replace_candidate", change_source_after_draft)
    with pytest.raises(ValueError, match="branch changed|baseline tags changed"):
        publisher.publish(tmp_path, "main", stage)
    assert (tmp_path / "DRAFT.md").read_text(encoding="utf-8") == "## Summary\nOriginal\n"
    assert (tmp_path / _MAIN_MARKER).read_text(encoding="utf-8") == "old\n"
    assert not publisher.journal_path(tmp_path, "main").exists()


@pytest.mark.parametrize("drift", ["branch", "tag"])
def test_recover_cli_rejects_source_drift_before_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    """Recovery retains a partial journal when the release source has changed."""
    stage = _start(tmp_path)
    (stage / "DRAFT.md").write_text("candidate\n", encoding="utf-8", newline="\n")
    _stage_marker(stage)
    original_replace = publisher._replace_candidate

    def interrupt_after_draft(root: Path, staged: Path, relative: str) -> None:
        """Leave the candidate draft live while the marker remains old."""
        if relative == "DRAFT.md":
            original_replace(root, staged, relative)
        else:
            raise OSError("interrupted before marker")

    monkeypatch.setattr(publisher, "_replace_candidate", interrupt_after_draft)
    with pytest.raises(OSError, match="interrupted before marker"):
        publisher.publish(tmp_path, "main", stage)
    monkeypatch.setattr(publisher, "_replace_candidate", original_replace)
    frozen_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    command = ["git", "switch", "-q", "-c", "other"] if drift == "branch" else ["git", "tag", "v2", frozen_head]
    subprocess.run(command, cwd=tmp_path, check=True, capture_output=True)
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip() == frozen_head
    completed = subprocess.run(
        [sys.executable, str(_SCRIPT), "recover", "--branch", "main"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 1
    expected_error = "branch changed" if drift == "branch" else "baseline tags changed"
    assert expected_error in completed.stderr
    assert (tmp_path / "DRAFT.md").read_text(encoding="utf-8") == (
        "candidate\n" if drift == "branch" else "## Summary\nOriginal\n"
    )
    assert (tmp_path / _MAIN_MARKER).read_text(encoding="utf-8") == "old\n"
    assert publisher.journal_path(tmp_path, "main").exists() == (drift == "branch")


@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("git") is None, reason="Requires Bash and Git")
def test_explicit_append_range_cannot_skip_valid_marker(tmp_path: Path) -> None:
    """An explicit left endpoint must include every commit after the valid marker."""
    skill = (Path(__file__).resolve().parents[1] / "skills/release/SKILL.md").read_text(encoding="utf-8")
    block = next(block for block in re.findall(r"```bash\n(.*?)```", skill, re.DOTALL) if "# quote RANGE" in block)
    prefix = block[: block.index("# quote RANGE")]
    marker = _init_git(tmp_path)
    subprocess.run(["git", "tag", "v1", marker], cwd=tmp_path, check=True, capture_output=True)
    for index in range(3):
        (tmp_path / "source.txt").write_text(str(index), encoding="utf-8", newline="\n")
        subprocess.run(["git", "add", "source.txt"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", f"c{index}"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )
        if index == 0:
            marker = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
        if index == 1:
            skipped = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    (tmp_path / ".temp").mkdir()
    _write_state(tmp_path, _MAIN_MARKER, marker + "\n", encoding="utf-8")
    (tmp_path / "release-setup-test").mkdir()
    (tmp_path / "release-setup-test/LAST_TAG").write_text("v1\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-setup-test/BRANCH").write_text("main\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-setup-test/BRANCH_REF").write_text("main\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-setup-test/BRANCH_KEY").write_text(
        marker_api.branch_state_key("main") + "\n", encoding="utf-8", newline="\n"
    )
    (tmp_path / "release-do-append-test").write_text("true\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-mode-test").write_text("notes\n", encoding="utf-8", newline="\n")
    environment = os.environ.copy()
    environment.update(
        {"TMPDIR": str(tmp_path), "CLAUDE_CODE_SESSION_ID": "test", "CLAUDE_PLUGIN_ROOT": str(_SCRIPT.parents[1])}
    )
    completed = subprocess.run(
        [shutil.which("bash"), "-c", f"RANGE={skipped}..HEAD; " + prefix],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0
    assert "does not start at saved marker" in completed.stderr
    assert not (tmp_path / "release-range-test").exists()


def test_begin_cli_accepts_release_setup_dotted_branch_slug(tmp_path: Path) -> None:
    """A normalized release branch with a dotted version must reach staging."""
    head_sha = _init_git(tmp_path)
    subprocess.run(["git", "branch", "-M", "release/v1.2"], cwd=tmp_path, check=True, capture_output=True)
    tag_state = tmp_path / "tag-state"
    tag_state.write_bytes(
        subprocess.check_output(
            ["git", "for-each-ref", "--sort=refname", "--format=%(refname) %(objectname)", "refs/tags"], cwd=tmp_path
        )
    )
    (tmp_path / ".temp").mkdir()
    dotted_marker = marker_api.state_relative("release/v1.2", "marker")
    _write_state(tmp_path, dotted_marker, head_sha + "\n")
    (tmp_path / "DRAFT.md").write_text("draft\n", encoding="utf-8", newline="\n")
    (tmp_path / "CHANGELOG.md").write_text("changelog\n", encoding="utf-8", newline="\n")
    completed = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "begin",
            "--branch",
            "release-v1.2",
            "--changelog",
            "CHANGELOG.md",
            "--head-sha",
            head_sha,
            "--start-sha",
            head_sha,
            "--range-start",
            head_sha,
            "--branch-ref",
            "release/v1.2",
            "--tag-state-file",
            str(tag_state),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    stage = _native_path(completed.stdout.strip())
    assert stage.is_dir()
    assert (stage / dotted_marker).read_text(encoding="utf-8") == head_sha + "\n"


@pytest.mark.parametrize(
    ("git_branch", "slug"),
    [
        pytest.param("feature+append", "feature+append", id="plus"),
        pytest.param("feature%append", "feature%25append", id="percent"),
    ],
)
def test_release_setup_punctuation_branch_completes_append_without_journal_collision(
    tmp_path: Path, git_branch: str, slug: str
) -> None:
    """Git-valid punctuation must survive setup, source checks, and publication."""
    start_sha = _init_git(tmp_path)
    subprocess.run(["git", "branch", "-M", git_branch], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "source.txt").write_text("next release source\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "add", "source.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "next"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    head_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    (tmp_path / ".temp").mkdir()
    marker = tmp_path / marker_api.state_relative(git_branch, "marker")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(start_sha + "\n", encoding="utf-8", newline="\n")
    (tmp_path / "DRAFT.md").write_text("original\n", encoding="utf-8", newline="\n")
    (tmp_path / "CHANGELOG.md").write_text("changelog\n", encoding="utf-8", newline="\n")

    environment = os.environ.copy()
    environment.update({"TMPDIR": str(tmp_path), "CLAUDE_CODE_SESSION_ID": "punctuation"})
    setup = subprocess.run(
        [sys.executable, str(_SCRIPT.with_name("release_setup.py"))],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    assert setup.returncode == 0
    branch = (tmp_path / "release-setup-punctuation/BRANCH").read_text(encoding="utf-8").strip()
    assert branch == slug
    assert publisher.journal_path(tmp_path, git_branch) != publisher.journal_path(tmp_path, "feature-append")
    (tmp_path / "release-do-append-punctuation").write_text("true\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-mode-punctuation").write_text("notes\n", encoding="utf-8", newline="\n")

    skill = (_SCRIPT.parents[1] / "skills/release/SKILL.md").read_text(encoding="utf-8")
    block = next(block for block in re.findall(r"```bash\n(.*?)```", skill, re.DOTALL) if "# quote RANGE" in block)
    prefix = block[: block.index("# quote RANGE")]
    environment["CLAUDE_PLUGIN_ROOT"] = str(_SCRIPT.parents[1])
    gathered = subprocess.run(
        [shutil.which("bash"), "-c", f"RANGE={start_sha}..HEAD; " + prefix],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert gathered.returncode == 0, gathered.stderr
    assert (tmp_path / "release-append-branch-punctuation").read_text(encoding="utf-8").strip() == git_branch

    stage = publisher.begin(tmp_path, branch, "CHANGELOG.md", head_sha, start_sha, git_branch, range_start=start_sha)
    (stage / "DRAFT.md").write_text("original\nnew change\n", encoding="utf-8", newline="\n")
    _write_state(stage, marker_api.state_relative(git_branch, "marker"), head_sha + "\n")
    publisher.seal(tmp_path, branch, stage)
    publisher.publish(tmp_path, branch, stage)

    assert (tmp_path / "DRAFT.md").read_text(encoding="utf-8") == "original\nnew change\n"
    assert marker.read_text(encoding="utf-8") == head_sha + "\n"
    assert not publisher.journal_path(tmp_path, git_branch).exists()


@pytest.mark.parametrize(
    "branch", ["../escape", "release..escape", "release/escape", "feature+../escape", "feature\\escape"]
)
def test_publisher_rejects_path_like_branch_slug(tmp_path: Path, branch: str) -> None:
    """The scratch slug still rejects separators and traversal tokens."""
    with pytest.raises(ValueError, match="filename-safe slug"):
        publisher._safe_branch(branch)


@pytest.mark.parametrize(
    "unsafe",
    [r"\CHANGELOG.md", r"C:CHANGELOG.md", r"C:\release\CHANGELOG.md", r"\\server\share\CHANGELOG.md"],
)
def test_windows_rooted_changelog_is_rejected_before_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsafe: str
) -> None:
    """A Windows rooted or drive-relative changelog cannot escape the repository."""
    from pathlib import PureWindowsPath

    assert PureWindowsPath(unsafe).root or PureWindowsPath(unsafe).drive
    _init_git(tmp_path)
    monkeypatch.setattr(publisher, "_head_sha", lambda *_: (_ for _ in ()).throw(AssertionError("source was read")))
    with pytest.raises(ValueError, match="unsafe artifact path"):
        publisher.begin(tmp_path, "main", unsafe, expected_start="old", range_start="HEAD")
    assert not (tmp_path / ".temp").exists()


@pytest.mark.parametrize(
    ("branch_ref", "legacy_slug"),
    [
        pytest.param("main", "main", id="ordinary"),
        pytest.param("feature/append", "feature-append", id="slash-collision"),
        pytest.param("feature%append", "feature%25append", id="prior-percent-encoding"),
    ],
)
def test_legacy_release_state_blocks_append_without_changing_old_bytes(
    tmp_path: Path, branch_ref: str, legacy_slug: str
) -> None:
    """Ambiguous legacy marker, provenance, and journal bytes require a migration decision."""
    _init_git(tmp_path)
    subprocess.run(["git", "branch", "-M", branch_ref], cwd=tmp_path, check=True, capture_output=True)
    old = tmp_path / ".temp"
    old.mkdir()
    legacy = {
        f"release-last-processed-{legacy_slug}": b"old marker\n",
        f"release-provenance-{legacy_slug}.json": b"old provenance\n",
        f"release-append-publish-{legacy_slug}.json": b"old journal\n",
    }
    for name, content in legacy.items():
        (old / name).write_bytes(content)
    (tmp_path / "DRAFT.md").write_text("original\n", encoding="utf-8", newline="\n")
    (tmp_path / "CHANGELOG.md").write_text("changelog\n", encoding="utf-8", newline="\n")

    with pytest.raises(ValueError, match="migration decision"):
        publisher.begin(tmp_path, quote(branch_ref.replace("/", "-"), safe="-_.+"), "CHANGELOG.md", range_start="HEAD")

    assert {name: (old / name).read_bytes() for name in legacy} == legacy
    assert not (old / "release-state-v2").exists()
    assert (tmp_path / "DRAFT.md").read_text(encoding="utf-8") == "original\n"


@pytest.mark.skipif(shutil.which("bash") is None, reason="Release setup uses Bash")
@pytest.mark.parametrize("legacy", [False, True])
def test_plain_notes_setup_guards_legacy_state_before_artifact_work(tmp_path: Path, legacy: bool) -> None:
    """Plain notes must refuse old branch state before treating its marker as absent."""
    _init_git(tmp_path)
    subprocess.run(["git", "branch", "-M", "main"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "release-mode-notes").write_text("notes\n", encoding="utf-8", newline="\n")
    old_marker = tmp_path / ".temp/release-last-processed-main"
    if legacy:
        old_marker.parent.mkdir(parents=True, exist_ok=True)
        old_marker.write_bytes(b"legacy marker\n")
    skill = (_SCRIPT.parents[1] / "skills/release/SKILL.md").read_text(encoding="utf-8")
    setup_block = next(
        block
        for block in re.findall(r"```bash\n(.*?)```", skill, re.DOTALL)
        if "release branch identity or mode missing" in block
    )
    environment = os.environ.copy()
    environment.update(
        {"TMPDIR": str(tmp_path), "CLAUDE_CODE_SESSION_ID": "notes", "CLAUDE_PLUGIN_ROOT": str(_SCRIPT.parents[1])}
    )
    completed = subprocess.run(
        [shutil.which("bash"), "-c", setup_block],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if legacy:
        assert completed.returncode == 1
        assert "migration decision required" in completed.stderr
        assert old_marker.read_bytes() == b"legacy marker\n"
    else:
        assert completed.returncode == 0, completed.stderr
        assert not (tmp_path / ".temp/release-state-v2").exists()
    assert (tmp_path / "release-setup-notes/BRANCH_KEY").read_text(
        encoding="utf-8"
    ).strip() == marker_api.branch_state_key("main")


@pytest.mark.skipif(shutil.which("bash") is None, reason="Release setup uses Bash")
def test_plain_notes_setup_refuses_pending_publication(tmp_path: Path) -> None:
    """Plain notes must stop before editing artifacts when append publication is pending."""
    _init_git(tmp_path)
    subprocess.run(["git", "branch", "-M", "main"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "release-mode-notes").write_text("notes\n", encoding="utf-8", newline="\n")
    (tmp_path / "DRAFT.md").write_bytes(b"ORIGINAL\n")
    marker = tmp_path / _MAIN_MARKER
    marker.parent.mkdir(parents=True)
    marker.write_bytes(b"OLD\n")
    journal = publisher.journal_path(tmp_path, "main")
    journal.write_bytes(b'{"mode":"rollback"}\n')
    skill = (_SCRIPT.parents[1] / "skills/release/SKILL.md").read_text(encoding="utf-8")
    setup_block = next(
        block
        for block in re.findall(r"```bash\n(.*?)```", skill, re.DOTALL)
        if "release branch identity or mode missing" in block
    )
    environment = os.environ.copy()
    environment.update(
        {"TMPDIR": str(tmp_path), "CLAUDE_CODE_SESSION_ID": "notes", "CLAUDE_PLUGIN_ROOT": str(_SCRIPT.parents[1])}
    )

    completed = subprocess.run(
        [shutil.which("bash"), "-c", setup_block],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert "branch changed after release gather" in completed.stderr
    assert (tmp_path / "DRAFT.md").read_bytes() == b"ORIGINAL\n"
    assert marker.read_bytes() == b"OLD\n"
    assert journal.read_bytes() == b'{"mode":"rollback"}\n'


@pytest.mark.skipif(shutil.which("bash") is None, reason="Release setup uses Bash")
def test_append_setup_recovers_pending_publication_before_marker_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Append setup reconciles its journal before the marker guard can reject it."""
    stage = _start(tmp_path)
    (stage / "DRAFT.md").write_text("## Summary\nNew\n", encoding="utf-8", newline="\n")
    expected_marker = _stage_marker(stage)
    original_replace = publisher._replace_candidate

    def interrupt(root: Path, staged: Path, relative: str) -> None:
        """Interrupt the first live replacement after its journal is durable."""
        raise OSError("interrupted")

    monkeypatch.setattr(publisher, "_replace_candidate", interrupt)
    with pytest.raises(OSError, match="interrupted"):
        publisher.publish(tmp_path, "main", stage)
    monkeypatch.setattr(publisher, "_replace_candidate", original_replace)
    journal = publisher.journal_path(tmp_path, "main")
    assert journal.exists()
    (tmp_path / "release-mode-append").write_text("notes\n", encoding="utf-8", newline="\n")
    (tmp_path / "release-do-append-append").write_text("true\n", encoding="utf-8", newline="\n")
    skill = (_SCRIPT.parents[1] / "skills/release/SKILL.md").read_text(encoding="utf-8")
    setup_block = next(
        block
        for block in re.findall(r"```bash\n(.*?)```", skill, re.DOTALL)
        if "release branch identity or mode missing" in block
    )
    environment = os.environ.copy()
    environment.update(
        {"TMPDIR": str(tmp_path), "CLAUDE_CODE_SESSION_ID": "append", "CLAUDE_PLUGIN_ROOT": str(_SCRIPT.parents[1])}
    )

    completed = subprocess.run(
        [shutil.which("bash"), "-c", setup_block],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "Reconciled interrupted append publication" in completed.stdout
    assert (tmp_path / "DRAFT.md").read_text(encoding="utf-8") == "## Summary\nNew\n"
    assert (tmp_path / _MAIN_MARKER).read_text(encoding="utf-8") == expected_marker
    assert not journal.exists()


def test_recovery_uses_raw_branch_identity_when_legacy_slugs_collide(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A colliding sibling branch cannot consume another branch's pending journal."""
    _init_git(tmp_path)
    subprocess.run(["git", "branch", "-M", "feature/append"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / ".temp").mkdir()
    (tmp_path / "DRAFT.md").write_text("original\n", encoding="utf-8", newline="\n")
    (tmp_path / "CHANGELOG.md").write_text("changelog\n", encoding="utf-8", newline="\n")
    stage = publisher.begin(tmp_path, "feature-append", "CHANGELOG.md", range_start="HEAD")
    (stage / "DRAFT.md").write_text("candidate\n", encoding="utf-8", newline="\n")
    head_sha = json.loads((stage / "candidate.json").read_text(encoding="utf-8"))["head_sha"]
    key = marker_api.branch_state_key("feature/append")
    staged_marker = stage / ".temp" / "release-state-v2" / key / "marker"
    staged_marker.parent.mkdir(parents=True, exist_ok=True)
    staged_marker.write_text(head_sha + "\n", encoding="utf-8", newline="\n")
    publisher.seal(tmp_path, "feature-append", stage)
    monkeypatch.setattr(publisher, "_replace_candidate", lambda *_: (_ for _ in ()).throw(OSError("interrupted")))
    with pytest.raises(OSError, match="interrupted"):
        publisher.publish(tmp_path, "feature-append", stage)
    journal = publisher.journal_path(tmp_path, "feature/append")
    assert journal.exists()

    subprocess.run(["git", "switch", "-q", "-c", "feature-append"], cwd=tmp_path, check=True, capture_output=True)
    with pytest.raises(FileNotFoundError):
        publisher.recover(tmp_path, "feature-append")
    sibling_journal = publisher.journal_path(tmp_path, "feature-append")
    sibling_journal.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(journal, sibling_journal)
    with pytest.raises(ValueError, match="branch changed"):
        publisher.recover(tmp_path, "feature-append")
    assert journal.exists()
    assert (tmp_path / "DRAFT.md").read_text(encoding="utf-8") == "original\n"

    subprocess.run(["git", "switch", "-q", "feature/append"], cwd=tmp_path, check=True, capture_output=True)
    monkeypatch.undo()
    publisher.recover(tmp_path, "feature-append")
    assert (tmp_path / "DRAFT.md").read_text(encoding="utf-8") == "candidate\n"
    assert not journal.exists()


def test_candidate_changed_after_truth_seal_blocks_publish(tmp_path: Path) -> None:
    """Publication must use the exact bytes accepted by the final truth check."""
    stage = _start(tmp_path)
    (stage / "DRAFT.md").write_text("checked candidate\n", encoding="utf-8", newline="\n")
    _stage_marker(stage)
    publisher.seal(tmp_path, "main", stage)
    (stage / "DRAFT.md").write_text("unchecked candidate\n", encoding="utf-8", newline="\n")
    with pytest.raises(ValueError, match="candidate changed after truth gate"):
        publisher.publish(tmp_path, "main", stage)
    assert (tmp_path / "DRAFT.md").read_text(encoding="utf-8") == "## Summary\nOriginal\n"


@pytest.mark.parametrize("after_marker", [False, True])
def test_promoted_artifact_user_edit_blocks_marker_and_journal_clear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, after_marker: bool
) -> None:
    """An edit after an earlier replacement cannot be certified or forgotten."""
    stage = _start(tmp_path)
    (stage / "DRAFT.md").write_text("candidate draft\n", encoding="utf-8", newline="\n")
    (stage / "CHANGELOG.md").write_text("candidate changelog\n", encoding="utf-8", newline="\n")
    _stage_marker(stage)
    publisher.seal(tmp_path, "main", stage)
    original_replace = publisher._replace_candidate
    replaced = False

    def edit_promoted(root: Path, staged: Path, relative: str) -> None:
        nonlocal replaced
        original_replace(root, staged, relative)
        if (relative == _MAIN_MARKER) == after_marker and not replaced:
            replaced = True
            (root / "DRAFT.md").write_text("user edit\n", encoding="utf-8", newline="\n")

    monkeypatch.setattr(publisher, "_replace_candidate", edit_promoted)
    with pytest.raises(ValueError, match="changed outside append publication"):
        publisher.publish(tmp_path, "main", stage)
    assert (tmp_path / "DRAFT.md").read_text(encoding="utf-8") == "user edit\n"
    assert publisher.journal_path(tmp_path, "main").exists()
    if not after_marker:
        assert (tmp_path / _MAIN_MARKER).read_text(encoding="utf-8") == "old\n"
