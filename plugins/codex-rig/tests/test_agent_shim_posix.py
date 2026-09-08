"""Acceptance checks for contained POSIX shim mutation primitives."""

from __future__ import annotations

import hashlib
import importlib.util
import errno
import os
import stat
import sys
import tempfile
from pathlib import Path
from types import ModuleType

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PLUGIN_ROOT / "scripts" / "_agent_shim_posix.py"


def _mode_is_retainable(mode: int) -> bool:
    """Return whether the host filesystem preserves one requested permission mode."""
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "mode-probe"
        path.mkdir(mode=0o700)
        path.chmod(mode)
        return stat.S_IMODE(path.stat().st_mode) == mode


def _load_module() -> ModuleType:
    """Load the internal primitive module without package installation."""
    specification = importlib.util.spec_from_file_location("codex_rig_agent_shim_posix", MODULE_PATH)
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


@pytest.fixture(name="roots")
def _roots(tmp_path: Path) -> tuple[int, int, Path, Path]:
    """Open isolated private source and target roots for descriptor operations."""
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir(mode=0o700)
    target.mkdir(mode=0o700)
    source_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY)
    target_fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY)
    try:
        yield source_fd, target_fd, source, target
    finally:
        os.close(source_fd)
        os.close(target_fd)


def test_private_path_creation_is_contained_and_idempotent(tmp_path: Path) -> None:
    """Create only explicit private components and reopen the same identity."""
    module = _load_module()
    root_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        first_fd, created = module.create_private_path(root_fd, ("codex-rig", "shims"))
        first = module.directory_identity(first_fd)
        os.close(first_fd)
        second_fd, repeated = module.create_private_path(root_fd, ("codex-rig", "shims"))
        second = module.directory_identity(second_fd)
        os.close(second_fd)
    finally:
        os.close(root_fd)

    assert created == ("codex-rig", "shims")
    assert repeated == ()
    assert first == second
    assert stat.S_IMODE((tmp_path / "codex-rig" / "shims").stat().st_mode) == 0o700


def test_private_directory_mode_is_independent_of_umask(tmp_path: Path) -> None:
    """Establish exact private permissions even under a restrictive umask."""
    module = _load_module()
    root_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    previous_umask = os.umask(0o777)
    try:
        child_fd, created = module.create_directory_at(root_fd, "child")
        os.close(child_fd)
    finally:
        os.umask(previous_umask)
        os.close(root_fd)

    assert created is True
    assert stat.S_IMODE((tmp_path / "child").stat().st_mode) == 0o700


def test_existing_protected_directory_is_opened_without_permission_change(tmp_path: Path) -> None:
    """Open an owned protected namespace while preserving its public read mode."""
    module = _load_module()
    child = tmp_path / "agents"
    child.mkdir(mode=0o700)
    child.chmod(0o755)
    root_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        child_fd, created = module.create_directory_at(root_fd, "agents", private=False)
        identity = module.directory_identity(child_fd, private=False)
        os.close(child_fd)
    finally:
        os.close(root_fd)

    assert created is False
    assert identity.mode == "0755"
    assert stat.S_IMODE(child.stat().st_mode) == 0o755


def test_protected_target_primitives_preserve_mode(roots: tuple[int, int, Path, Path]) -> None:
    """Publish, detach, and restore through an existing protected target."""
    module = _load_module()
    source_fd, target_fd, source, target = roots
    target.chmod(0o755)
    payload = b"generated-shim\n"
    digest = hashlib.sha256(payload).hexdigest()
    module.write_exclusive_at(source_fd, "staged.toml", payload)

    module.publish_noclobber(source_fd, "staged.toml", target_fd, "role.toml", expected_hash=digest)
    module.unlink_verified_at(
        source_fd,
        "staged.toml",
        expected_hash=digest,
        expected_mode=0o600,
        expected_links=2,
    )
    module.detach_verified(
        target_fd,
        "role.toml",
        source_fd,
        "quarantine.toml",
        expected_hash=digest,
    )
    module.restore_quarantine_at(
        source_fd,
        "quarantine.toml",
        target_fd,
        "role.toml",
        expected_hash=digest,
    )

    assert (target / "role.toml").read_bytes() == payload
    assert not (source / "quarantine.toml").exists()
    assert stat.S_IMODE(target.stat().st_mode) == 0o755


@pytest.mark.parametrize(
    "mode",
    [
        pytest.param(
            mode,
            id=case_id,
            marks=pytest.mark.skipif(
                not _mode_is_retainable(mode), reason=f"filesystem does not retain mode {mode:04o}"
            ),
        )
        for mode, case_id in (
            (0o775, "group-writable"),
            (0o4700, "setuid"),
            (0o2700, "setgid"),
            (0o1700, "sticky"),
        )
    ],
)
def test_protected_directory_rejects_group_writable_or_special_modes(tmp_path: Path, mode: int) -> None:
    """Reject target namespaces that another principal could mutate or redirect."""
    module = _load_module()
    child = tmp_path / "agents"
    child.mkdir(mode=0o700)
    child.chmod(mode)
    observed_mode = stat.S_IMODE(child.stat().st_mode)
    assert observed_mode == mode
    root_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(
            module.PosixPrimitiveError,
            match=(
                "unsafe protected directory mode: expected no group/world write or special bits, "
                rf"observed {mode:04o}"
            ),
        ):
            module.create_directory_at(root_fd, "agents", private=False)
    finally:
        os.close(root_fd)


def test_private_path_rejects_symlink_component(tmp_path: Path) -> None:
    """Refuse a link where a held private child directory is required."""
    module = _load_module()
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    (tmp_path / "codex-rig").symlink_to(outside, target_is_directory=True)
    root_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(module.PosixPrimitiveError):
            module.create_private_path(root_fd, ("codex-rig", "shims"))
    finally:
        os.close(root_fd)
    assert not (outside / "shims").exists()


def test_private_path_rejects_group_writable_parent(tmp_path: Path) -> None:
    """Refuse path-following permission repair in a substitutable namespace."""
    module = _load_module()
    tmp_path.chmod(0o770)
    root_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(module.PosixPrimitiveError, match="unsafe protected directory mode"):
            module.create_private_path(root_fd, ("codex-rig", "shims"))
    finally:
        os.close(root_fd)
        tmp_path.chmod(0o700)
    assert not (tmp_path / "codex-rig").exists()


def test_created_directory_descriptor_closes_when_fsync_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not leak a new directory capability after durability failure."""
    module = _load_module()
    root_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    opened: list[int] = []
    original_open = module.open_directory_at
    original_fsync = module.os.fsync

    def _capture_open(*args: object, **kwargs: object) -> int:
        """Record descriptors opened while testing cleanup after durability failure."""
        fd = original_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def _fail_child_fsync(fd: int) -> None:
        """Fail the first child-directory sync and delegate later syncs."""
        if opened and fd == opened[0]:
            raise OSError(errno.EIO, "injected directory fsync failure")
        original_fsync(fd)

    monkeypatch.setattr(module, "open_directory_at", _capture_open)
    monkeypatch.setattr(module.os, "fsync", _fail_child_fsync)
    try:
        with pytest.raises(OSError, match="injected"):
            module.create_directory_at(root_fd, "child")
    finally:
        os.close(root_fd)

    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])


def test_regular_read_rejects_same_size_concurrent_rewrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject a torn read even when a concurrent writer preserves file size."""
    module = _load_module()
    original = b"a" * 131_072
    replacement = b"b" * len(original)
    path = tmp_path / "role.toml"
    path.write_bytes(original)
    path.chmod(0o600)
    root_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    original_read = module.os.read
    calls = 0

    def _rewrite_after_first_chunk(fd: int, size: int) -> bytes:
        """Rewrite the file after the first read chunk to simulate a concurrent writer."""
        nonlocal calls
        chunk = original_read(fd, size)
        calls += 1
        if calls == 1:
            path.write_bytes(replacement)
            path.chmod(0o600)
        return chunk

    monkeypatch.setattr(module.os, "read", _rewrite_after_first_chunk)
    try:
        with pytest.raises(module.PosixPrimitiveError, match="changed during read"):
            module.read_regular_at(root_fd, "role.toml", expected_mode=0o600)
    finally:
        os.close(root_fd)


def test_regular_read_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    """Fail closed on a FIFO rather than waiting for a writer."""
    module = _load_module()
    os.mkfifo(tmp_path / "role.toml", mode=0o600)
    root_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(module.PosixPrimitiveError, match="unsafe regular"):
            module.read_regular_at(root_fd, "role.toml")
    finally:
        os.close(root_fd)


def test_exclusive_write_and_hardlink_publication_never_clobber(
    roots: tuple[int, int, Path, Path],
) -> None:
    """Publish exact staged bytes once while preserving an occupied target."""
    module = _load_module()
    source_fd, target_fd, source, target = roots
    payload = b"generated-shim\n"
    digest = hashlib.sha256(payload).hexdigest()

    staged = module.write_exclusive_at(source_fd, "role.toml", payload)
    published = module.publish_noclobber(source_fd, "role.toml", target_fd, "role.toml", expected_hash=digest)

    assert (target / "role.toml").read_bytes() == payload
    assert (staged.device, staged.inode) == (published.device, published.inode)
    with pytest.raises(module.PosixPrimitiveError):
        module.publish_noclobber(source_fd, "role.toml", target_fd, "role.toml", expected_hash=digest)
    assert (target / "role.toml").read_bytes() == payload
    assert (source / "role.toml").read_bytes() == payload


def test_initial_journal_uses_recoverable_same_inode_publication(tmp_path: Path) -> None:
    """Publish journal authority and retire only its verified initial link."""
    module = _load_module()
    root_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        payload = b'{"journal":"preparing"}'
        identity = module.write_initial_journal(root_fd, payload)
    finally:
        os.close(root_fd)

    assert (tmp_path / "journal.json").read_bytes() == payload
    assert not (tmp_path / "journal.initial.json").exists()
    assert (tmp_path / "journal.json").stat().st_nlink == 1
    assert identity.sha256 == hashlib.sha256(payload).hexdigest()


def test_failed_exclusive_write_removes_only_its_partial_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Avoid unauthenticated transaction residue after a staged write failure."""
    module = _load_module()
    root_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    original_write = module.os.write
    calls = 0

    def _partial_then_fail(fd: int, payload: object) -> int:
        """Write a short prefix once, then inject the staged-write failure."""
        nonlocal calls
        calls += 1
        if calls == 1:
            return original_write(fd, bytes(payload)[:2])
        raise OSError(errno.EIO, "injected staged write failure")

    monkeypatch.setattr(module.os, "write", _partial_then_fail)
    try:
        with pytest.raises(OSError, match="injected"):
            module.write_exclusive_at(root_fd, "after.toml", b"complete-payload")
    finally:
        os.close(root_fd)

    assert not (tmp_path / "after.toml").exists()


def test_failed_write_never_unlinks_a_substituted_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preserve both partial evidence and a concurrent replacement."""
    module = _load_module()
    root_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    original_write = module.os.write
    original_cleanup = module._cleanup_created_file
    calls = 0

    def _partial_then_fail(fd: int, payload: object) -> int:
        """Write a short prefix once, then inject the staged-write failure."""
        nonlocal calls
        calls += 1
        if calls == 1:
            return original_write(fd, bytes(payload)[:2])
        raise OSError(errno.EIO, "injected staged write failure")

    def _substitute_before_cleanup(parent_fd: int, name: str, identity: object) -> None:
        """Replace the partial path before cleanup to exercise identity protection."""
        os.rename(name, "partial.evidence", src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        replacement_fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent_fd)
        original_write(replacement_fd, b"replacement")
        os.close(replacement_fd)
        original_cleanup(parent_fd, name, identity)

    monkeypatch.setattr(module.os, "write", _partial_then_fail)
    monkeypatch.setattr(module, "_cleanup_created_file", _substitute_before_cleanup)
    try:
        with pytest.raises(module.PosixPrimitiveError, match="cleanup is uncertain"):
            module.write_exclusive_at(root_fd, "after.toml", b"complete-payload")
    finally:
        os.close(root_fd)

    assert (tmp_path / "after.toml").read_bytes() == b"replacement"
    assert (tmp_path / "partial.evidence").read_bytes() == b"co"


def test_detach_preserves_exact_target_in_private_quarantine(
    roots: tuple[int, int, Path, Path],
) -> None:
    """Detach approved bytes atomically and retain their exact evidence."""
    module = _load_module()
    source_fd, target_fd, source, target = roots
    payload = b"owned-before\n"
    file_fd = os.open("role.toml", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=target_fd)
    os.write(file_fd, payload)
    os.close(file_fd)
    os.chmod(target / "role.toml", 0o600)

    identity = module.detach_verified(
        target_fd,
        "role.toml",
        source_fd,
        "quarantine.toml",
        expected_hash=hashlib.sha256(payload).hexdigest(),
    )

    assert not (target / "role.toml").exists()
    assert (source / "quarantine.toml").read_bytes() == payload
    assert identity.sha256 == hashlib.sha256(payload).hexdigest()


def test_detach_mismatch_restores_observed_bytes_without_journal_substitution(
    roots: tuple[int, int, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restore the exact detached race observation when the target stays absent."""
    module = _load_module()
    source_fd, target_fd, source, target = roots
    approved = b"approved-content\n"
    raced = b"concurrent-content\n"
    fd = os.open("role.toml", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=target_fd)
    os.write(fd, approved)
    os.close(fd)

    original_rename = module._rename_noreplace

    def _replace_before_detach(*args: object, **kwargs: object) -> None:
        """Simulate a target byte race immediately before atomic detach."""
        race_fd = os.open("role.toml", os.O_WRONLY | os.O_TRUNC, dir_fd=target_fd)
        os.write(race_fd, raced)
        os.close(race_fd)
        original_rename(*args, **kwargs)

    monkeypatch.setattr(module, "_rename_noreplace", _replace_before_detach)

    with pytest.raises(module.DetachedMismatchError) as captured:
        module.detach_verified(
            target_fd,
            "role.toml",
            source_fd,
            "quarantine.toml",
            expected_hash=hashlib.sha256(approved).hexdigest(),
        )

    assert captured.value.restored is True
    assert (target / "role.toml").read_bytes() == raced
    assert (source / "quarantine.toml").read_bytes() == raced
    assert (target / "role.toml").stat().st_ino == (source / "quarantine.toml").stat().st_ino


def test_detach_never_replaces_an_occupied_quarantine(
    roots: tuple[int, int, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preserve both files when the private quarantine name is occupied."""
    module = _load_module()
    source_fd, target_fd, source, target = roots
    approved = b"approved-content\n"
    occupied = b"existing-quarantine\n"
    target_file = os.open("role.toml", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=target_fd)
    os.write(target_file, approved)
    os.close(target_file)
    original_rename = module._rename_noreplace

    def _occupy_immediately_before_rename(*args: object, **kwargs: object) -> None:
        """Occupy the quarantine name immediately before the protected rename."""
        quarantine_file = os.open("quarantine.toml", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=source_fd)
        os.write(quarantine_file, occupied)
        os.close(quarantine_file)
        original_rename(*args, **kwargs)

    monkeypatch.setattr(module, "_rename_noreplace", _occupy_immediately_before_rename)

    with pytest.raises(module.PosixPrimitiveError):
        module.detach_verified(
            target_fd,
            "role.toml",
            source_fd,
            "quarantine.toml",
            expected_hash=hashlib.sha256(approved).hexdigest(),
        )

    assert (target / "role.toml").read_bytes() == approved
    assert (source / "quarantine.toml").read_bytes() == occupied


def test_owned_replace_requires_exact_previous_hash(tmp_path: Path) -> None:
    """Replace only the exact manager-owned preimage and retain private mode."""
    module = _load_module()
    root_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        before = b"before"
        module.write_exclusive_at(root_fd, "journal.json", before)
        with pytest.raises(module.PosixPrimitiveError, match="hash mismatch"):
            module.replace_owned_at(
                root_fd,
                "journal.json",
                b"after",
                temporary="journal.next.json",
                expected_hash="a" * 64,
            )
        result = module.replace_owned_at(
            root_fd,
            "journal.json",
            b"after",
            temporary="journal.next.json",
            expected_hash=hashlib.sha256(before).hexdigest(),
        )
    finally:
        os.close(root_fd)

    assert (tmp_path / "journal.json").read_bytes() == b"after"
    assert result.mode == "0600"


def test_owned_replace_rejects_unrecognized_state_temporary(tmp_path: Path) -> None:
    """Keep state publication out of the fixed journal-successor primitive."""
    module = _load_module()
    root_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(module.PosixPrimitiveError, match="unsupported"):
            module.replace_owned_at(
                root_fd,
                "state.json",
                b"after",
                temporary="state.next.json",
                expected_hash=None,
            )
    finally:
        os.close(root_fd)


def test_state_publication_consumes_only_journal_bound_after_link(
    roots: tuple[int, int, Path, Path],
) -> None:
    """Replace exact manager state from the staged same-filesystem artifact."""
    module = _load_module()
    transaction_fd, state_fd, transaction, state_root = roots
    before = b"before-state"
    after = b"after-state"
    module.write_exclusive_at(state_fd, "state.json", before)
    module.write_exclusive_at(transaction_fd, "state.after.json", after)

    result = module.publish_state_from_transaction(
        transaction_fd,
        state_fd,
        expected_after_hash=hashlib.sha256(after).hexdigest(),
        expected_before_hash=hashlib.sha256(before).hexdigest(),
    )

    assert (state_root / "state.json").read_bytes() == after
    assert not (transaction / "state.publish.json").exists()
    assert (transaction / "state.after.json").stat().st_ino == (state_root / "state.json").stat().st_ino
    assert result.link_count == 2


def test_failed_state_replace_retains_exact_publish_evidence(
    roots: tuple[int, int, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Leave the journal-bound publish link recoverable when replace fails."""
    module = _load_module()
    transaction_fd, state_fd, transaction, state_root = roots
    before = b"before-state"
    after = b"after-state"
    module.write_exclusive_at(state_fd, "state.json", before)
    module.write_exclusive_at(transaction_fd, "state.after.json", after)

    def _fail_replace(*args: object, **kwargs: object) -> None:
        """Inject a state-replacement failure after staging completes."""
        raise OSError(errno.EIO, "injected state replace failure")

    monkeypatch.setattr(module.os, "replace", _fail_replace)
    with pytest.raises(module.PosixPrimitiveError, match="state publication failed"):
        module.publish_state_from_transaction(
            transaction_fd,
            state_fd,
            expected_after_hash=hashlib.sha256(after).hexdigest(),
            expected_before_hash=hashlib.sha256(before).hexdigest(),
        )

    assert (state_root / "state.json").read_bytes() == before
    assert (transaction / "state.publish.json").read_bytes() == after


def test_lock_path_swap_after_flock_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Close a held lock when the approved fixed pathname is replaced."""
    module = _load_module()
    lock = tmp_path / ".codex-rig-shims.lock"
    lock.write_bytes(b"")
    lock.chmod(0o600)
    approved = lock.stat()
    home_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    original_flock = module.fcntl.flock
    held: list[int] = []

    def _replace_after_lock(fd: int, operation: int) -> None:
        """Replace the lock path after acquisition to verify identity revalidation."""
        original_flock(fd, operation)
        held.append(fd)
        lock.rename(tmp_path / "old-lock")
        lock.write_bytes(b"")
        lock.chmod(0o600)

    monkeypatch.setattr(module.fcntl, "flock", _replace_after_lock)
    try:
        with pytest.raises(module.PosixPrimitiveError, match="path changed"):
            module.acquire_coordination_lock(
                home_fd,
                intent="open-existing",
                expected_identity=(approved.st_dev, approved.st_ino),
            )
    finally:
        os.close(home_fd)

    assert len(held) == 1
    with pytest.raises(OSError):
        os.fstat(held[0])


def test_coordination_lock_is_exclusive_and_shape_checked(tmp_path: Path) -> None:
    """Acquire the fixed lock once and reject contention or unsafe reuse."""
    module = _load_module()
    home_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    first = module.acquire_coordination_lock(home_fd, intent="create-if-absent", expected_identity=None)
    identity = os.fstat(first)
    try:
        with pytest.raises(module.LockBusyError):
            module.acquire_coordination_lock(
                home_fd,
                intent="open-existing",
                expected_identity=(identity.st_dev, identity.st_ino),
            )
    finally:
        os.close(first)
    second = module.acquire_coordination_lock(
        home_fd,
        intent="open-existing",
        expected_identity=(identity.st_dev, identity.st_ino),
    )
    os.close(second)
    os.close(home_fd)
    assert stat.S_IMODE((tmp_path / ".codex-rig-shims.lock").stat().st_mode) == 0o600


@pytest.mark.parametrize("intent", ["create-if-absent", "open-existing"])
def test_coordination_lock_rejects_group_writable_home(tmp_path: Path, intent: str) -> None:
    """Reject lock creation or reuse after the approved home becomes substitutable."""
    module = _load_module()
    expected_identity = None
    if intent == "open-existing":
        lock = tmp_path / ".codex-rig-shims.lock"
        lock.write_bytes(b"")
        lock.chmod(0o600)
        metadata = lock.stat()
        expected_identity = (metadata.st_dev, metadata.st_ino)
    tmp_path.chmod(0o770)
    home_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(module.PosixPrimitiveError, match="unsafe protected directory mode"):
            module.acquire_coordination_lock(
                home_fd,
                intent=intent,
                expected_identity=expected_identity,
            )
    finally:
        os.close(home_fd)
        tmp_path.chmod(0o700)

    lock = tmp_path / ".codex-rig-shims.lock"
    if intent == "create-if-absent":
        assert not lock.exists()
    else:
        metadata = lock.stat()
        assert (metadata.st_dev, metadata.st_ino) == expected_identity
        assert metadata.st_size == 0


def test_coordination_lock_never_follows_a_link(tmp_path: Path) -> None:
    """Preserve an external file when the fixed lock name is a symlink."""
    module = _load_module()
    outside = tmp_path / "outside"
    outside.write_bytes(b"")
    outside.chmod(0o600)
    (tmp_path / ".codex-rig-shims.lock").symlink_to(outside)
    home_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(module.PosixPrimitiveError):
            module.acquire_coordination_lock(home_fd, intent="open-existing", expected_identity=(1, 1))
    finally:
        os.close(home_fd)
    assert outside.read_bytes() == b""


def test_verified_unlink_is_durable_and_absence_requires_permission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Remove exact bytes durably and permit replay absence only explicitly."""
    module = _load_module()
    payload = b"published-target\n"
    path = tmp_path / "role.toml"
    path.write_bytes(payload)
    path.chmod(0o600)
    root_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    fsynced: list[int] = []
    original_fsync = module.os.fsync

    def _capture_fsync(fd: int) -> None:
        """Record fsync calls while delegating to the real implementation."""
        fsynced.append(fd)
        original_fsync(fd)

    monkeypatch.setattr(module.os, "fsync", _capture_fsync)
    try:
        removed = module.unlink_verified_at(
            root_fd,
            "role.toml",
            expected_hash=hashlib.sha256(payload).hexdigest(),
            expected_mode=0o600,
        )
        with pytest.raises(module.PosixPrimitiveError):
            module.unlink_verified_at(
                root_fd,
                "role.toml",
                expected_hash=hashlib.sha256(payload).hexdigest(),
                expected_mode=0o600,
            )
        replayed = module.unlink_verified_at(
            root_fd,
            "role.toml",
            expected_hash=hashlib.sha256(payload).hexdigest(),
            expected_mode=0o600,
            allow_absent=True,
        )
    finally:
        os.close(root_fd)

    assert removed is True
    assert replayed is False
    assert not path.exists()
    assert fsynced == [root_fd]


class TestVerifiedUnlink:
    """Verify unlink refuses every unapproved target identity or shape."""

    def test_rejects_hash_mismatch(self, tmp_path: Path) -> None:
        """Preserve a regular file whose bytes do not match the approved digest."""
        module = _load_module()
        approved = b"approved\n"
        role = tmp_path / "role.toml"
        role.write_bytes(approved)
        role.chmod(0o600)
        root_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with pytest.raises(module.PosixPrimitiveError, match="hash"):
                module.unlink_verified_at(root_fd, "role.toml", expected_hash="0" * 64, expected_mode=0o600)
        finally:
            os.close(root_fd)

        assert role.read_bytes() == approved

    def test_rejects_symlink(self, tmp_path: Path) -> None:
        """Preserve an external target when the selected name is a symlink."""
        module = _load_module()
        outside = tmp_path / "outside"
        outside.write_bytes(b"outside\n")
        outside.chmod(0o600)
        (tmp_path / "linked.toml").symlink_to(outside)
        root_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with pytest.raises(module.PosixPrimitiveError):
                module.unlink_verified_at(
                    root_fd,
                    "linked.toml",
                    expected_hash=hashlib.sha256(outside.read_bytes()).hexdigest(),
                    expected_mode=0o600,
                )
        finally:
            os.close(root_fd)

        assert outside.read_bytes() == b"outside\n"

    def test_requires_expected_hardlink_count(self, tmp_path: Path) -> None:
        """Reject an unexpected hardlink count and accept the explicitly approved count."""
        module = _load_module()
        approved = b"approved\n"
        hard = tmp_path / "hard.toml"
        hard.write_bytes(approved)
        hard.chmod(0o600)
        os.link(hard, tmp_path / "hard-copy.toml")
        root_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with pytest.raises(module.PosixPrimitiveError, match="link count"):
                module.unlink_verified_at(
                    root_fd,
                    "hard.toml",
                    expected_hash=hashlib.sha256(approved).hexdigest(),
                    expected_mode=0o600,
                )
            assert module.unlink_verified_at(
                root_fd,
                "hard.toml",
                expected_hash=hashlib.sha256(approved).hexdigest(),
                expected_mode=0o600,
                expected_links=2,
            )
        finally:
            os.close(root_fd)

        assert (tmp_path / "hard-copy.toml").read_bytes() == approved

    def test_rejects_concurrent_substitution(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Preserve both files when the approved inode is replaced before unlink."""
        module = _load_module()
        approved = b"approved\n"
        replacement = b"replacement\n"
        role = tmp_path / "role.toml"
        role.write_bytes(approved)
        role.chmod(0o600)
        original_unlink = module._unlink_same_inode

        def _substitute_before_unlink(parent_fd: int, name: str, identity: object) -> None:
            """Swap the path before unlink so cleanup must refuse the replacement."""
            os.rename(name, "approved.evidence", src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent_fd)
            os.write(fd, replacement)
            os.close(fd)
            original_unlink(parent_fd, name, identity)

        monkeypatch.setattr(module, "_unlink_same_inode", _substitute_before_unlink)
        root_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with pytest.raises(module.PosixPrimitiveError, match="identity changed"):
                module.unlink_verified_at(
                    root_fd,
                    "role.toml",
                    expected_hash=hashlib.sha256(approved).hexdigest(),
                    expected_mode=0o600,
                )
        finally:
            os.close(root_fd)

        assert role.read_bytes() == replacement
        assert (tmp_path / "approved.evidence").read_bytes() == approved


def test_quarantine_restore_is_exact_and_never_clobbers(
    roots: tuple[int, int, Path, Path],
) -> None:
    """Restore one exact quarantine file only into an absent target name."""
    module = _load_module()
    quarantine_fd, target_fd, quarantine, target = roots
    payload = b"before-image\n"
    digest = hashlib.sha256(payload).hexdigest()
    module.write_exclusive_at(quarantine_fd, "role.toml", payload)

    restored = module.restore_quarantine_at(
        quarantine_fd,
        "role.toml",
        target_fd,
        "role.toml",
        expected_hash=digest,
    )

    assert restored.sha256 == digest
    assert not (quarantine / "role.toml").exists()
    assert (target / "role.toml").read_bytes() == payload

    module.write_exclusive_at(quarantine_fd, "next.toml", payload)
    with pytest.raises(module.PosixPrimitiveError):
        module.restore_quarantine_at(
            quarantine_fd,
            "next.toml",
            target_fd,
            "role.toml",
            expected_hash=digest,
        )
    with pytest.raises(module.PosixPrimitiveError, match="hash"):
        module.restore_quarantine_at(
            quarantine_fd,
            "next.toml",
            target_fd,
            "other.toml",
            expected_hash="0" * 64,
        )
    assert (quarantine / "next.toml").read_bytes() == payload
    assert (target / "role.toml").read_bytes() == payload
    assert not (target / "other.toml").exists()


def test_state_restore_uses_bound_before_image_without_clobber(
    roots: tuple[int, int, Path, Path],
) -> None:
    """Restore prior state while retaining the detached current state evidence."""
    module = _load_module()
    transaction_fd, state_fd, transaction, state_root = roots
    before = b"before-state"
    current = b"current-state"
    module.write_exclusive_at(transaction_fd, "state.before.json", before)
    module.write_exclusive_at(transaction_fd, "state.after.json", current)
    module.write_exclusive_at(state_fd, "state.json", current)

    restored = module.restore_state_from_transaction(
        transaction_fd,
        state_fd,
        before_exists=True,
        expected_before_hash=hashlib.sha256(before).hexdigest(),
        expected_current_hash=hashlib.sha256(current).hexdigest(),
    )

    assert restored is not None
    assert restored.sha256 == hashlib.sha256(before).hexdigest()
    assert (state_root / "state.json").read_bytes() == before
    assert (transaction / "state.after.json").read_bytes() == current

    (state_root / "state.json").write_bytes(b"foreign")
    (state_root / "state.json").chmod(0o600)
    with pytest.raises(module.PosixPrimitiveError, match="hash"):
        module.restore_state_from_transaction(
            transaction_fd,
            state_fd,
            before_exists=True,
            expected_before_hash=hashlib.sha256(before).hexdigest(),
            expected_current_hash=hashlib.sha256(current).hexdigest(),
        )
    assert (state_root / "state.json").read_bytes() == b"foreign"


def test_state_prior_absence_removal_is_explicitly_idempotent(
    roots: tuple[int, int, Path, Path],
) -> None:
    """Remove exact current state for prior absence and gate replay absence."""
    module = _load_module()
    transaction_fd, state_fd, _, state_root = roots
    current = b"current-state"
    digest = hashlib.sha256(current).hexdigest()
    module.write_exclusive_at(transaction_fd, "state.after.json", current)
    module.write_exclusive_at(state_fd, "state.json", current)

    removed = module.restore_state_from_transaction(
        transaction_fd,
        state_fd,
        before_exists=False,
        expected_before_hash=None,
        expected_current_hash=digest,
    )
    with pytest.raises(module.PosixPrimitiveError):
        module.restore_state_from_transaction(
            transaction_fd,
            state_fd,
            before_exists=False,
            expected_before_hash=None,
            expected_current_hash=digest,
        )
    replayed = module.restore_state_from_transaction(
        transaction_fd,
        state_fd,
        before_exists=False,
        expected_before_hash=None,
        expected_current_hash=digest,
        allow_current_absent=True,
    )

    assert removed is None
    assert replayed is None
    assert not (state_root / "state.json").exists()


def test_transaction_cleanup_obeys_exact_nonrecursive_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Remove named artifacts then verified empty directories without recursion."""
    module = _load_module()
    artifact = tmp_path / "journal.json"
    artifact.write_bytes(b"journal")
    artifact.chmod(0o600)
    empty = tmp_path / "empty"
    empty.mkdir(mode=0o700)
    nonempty = tmp_path / "nonempty"
    nonempty.mkdir(mode=0o700)
    (nonempty / "evidence").write_bytes(b"retain")
    (nonempty / "evidence").chmod(0o600)
    root_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    fsynced: list[int] = []
    original_fsync = module.os.fsync

    def _capture_fsync(fd: int) -> None:
        """Record fsync calls while delegating to the real implementation."""
        fsynced.append(fd)
        original_fsync(fd)

    monkeypatch.setattr(module.os, "fsync", _capture_fsync)
    try:
        removed = module.remove_transaction_entries_at(
            root_fd,
            (
                ("journal.json", hashlib.sha256(b"journal").hexdigest(), 1),
                ("empty", None, None),
            ),
        )
        with pytest.raises(module.PosixPrimitiveError, match="empty"):
            module.remove_transaction_entries_at(root_fd, (("nonempty", None, None),))
        with pytest.raises(module.PosixPrimitiveError):
            module.remove_transaction_entries_at(root_fd, (("missing", None, None),))
        replayed = module.remove_transaction_entries_at(root_fd, (("missing", None, None),), allow_absent=True)
    finally:
        os.close(root_fd)

    assert removed == ("journal.json", "empty")
    assert replayed == ()
    assert fsynced == [root_fd, root_fd]
    assert not artifact.exists()
    assert not empty.exists()
    assert (nonempty / "evidence").read_bytes() == b"retain"


def test_transaction_cleanup_rejects_linked_child(tmp_path: Path) -> None:
    """Never follow a transaction child link during bounded cleanup."""
    module = _load_module()
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    (tmp_path / "linked").symlink_to(outside, target_is_directory=True)
    root_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(module.PosixPrimitiveError):
            module.remove_transaction_entries_at(root_fd, (("linked", None, None),))
    finally:
        os.close(root_fd)
    assert (tmp_path / "linked").is_symlink()
    assert outside.is_dir()
