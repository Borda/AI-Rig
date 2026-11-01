"""Acceptance checks for contained POSIX shim mutation primitives.

Every primitive works on held directory descriptors and must refuse symlinks, unexpected identities, unsafe modes, and
hash mismatches while leaving all unrelated bytes untouched. Race scenarios are injected by wrapping one internal
boundary at a time; the filesystem operations themselves always run for real inside ``tmp_path``.
"""

from __future__ import annotations

import errno
import os
import stat
import tempfile
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
from _modules import load_module_registered
from _shim_support import sha256_hex

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = PLUGIN_ROOT / "scripts" / "_agent_shim_posix.py"
LOCK_NAME = ".codex-rig-shims.lock"


def _mode_is_retainable(mode: int) -> bool:
    """Return whether the host filesystem preserves one requested permission mode."""
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "mode-probe"
        path.mkdir(mode=0o700)
        path.chmod(mode)
        return stat.S_IMODE(path.stat().st_mode) == mode


def _retainable_mode_case(mode: int, case_id: str) -> object:
    """Build one mode case that is skipped at collection when the filesystem drops the mode."""
    skip = pytest.mark.skipif(not _mode_is_retainable(mode), reason=f"filesystem does not retain mode {mode:04o}")
    return pytest.param(mode, id=case_id, marks=skip)


def _private_file(path: Path, payload: bytes = b"") -> Path:
    """Write one owner-only fixture file and return its path."""
    path.write_bytes(payload)
    path.chmod(0o600)
    return path


def _put(dir_fd: int, name: str, payload: bytes, flags: int = os.O_CREAT | os.O_EXCL) -> None:
    """Write one owner-only file through a directory descriptor, bypassing the primitives under test."""
    fd = os.open(name, os.O_WRONLY | flags, 0o600, dir_fd=dir_fd)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, payload)
    finally:
        os.close(fd)


def _lock_identity(path: Path) -> tuple[int, int]:
    """Return the (device, inode) pair that pins one lock file."""
    metadata = path.stat()
    return metadata.st_dev, metadata.st_ino


def _load_module() -> ModuleType:
    """Load the internal primitive module without package installation."""
    return load_module_registered("codex_rig_agent_shim_posix", MODULE_PATH)


@pytest.fixture(name="module")
def _module() -> ModuleType:
    """Load a fresh copy of the primitive module for one test."""
    return _load_module()


@pytest.fixture(name="root_fd")
def _root_fd(tmp_path: Path) -> Iterator[int]:
    """Hold an open directory descriptor for ``tmp_path`` and close it afterwards."""
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    yield fd
    os.close(fd)


@pytest.fixture(name="roots")
def _roots(tmp_path: Path) -> Iterator[tuple[int, int, Path, Path]]:
    """Open isolated private source and target roots for descriptor operations."""
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir(mode=0o700)
    target.mkdir(mode=0o700)
    source_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY)
    target_fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY)
    yield source_fd, target_fd, source, target
    os.close(source_fd)
    os.close(target_fd)


@pytest.fixture(name="restore_tmp_mode")
def _restore_tmp_mode(tmp_path: Path) -> Iterator[None]:
    """Return ``tmp_path`` to owner-only mode after a test made it group-writable, so pytest can clean it."""
    yield
    tmp_path.chmod(0o700)


@pytest.fixture(name="fsynced")
def _fsynced(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Record every descriptor passed to ``os.fsync`` while still delegating to the real call."""
    recorded: list[int] = []
    original_fsync = module.os.fsync

    def _capture_fsync(fd: int) -> None:
        """Record the descriptor then sync it for real."""
        recorded.append(fd)
        original_fsync(fd)

    monkeypatch.setattr(module.os, "fsync", _capture_fsync)
    return recorded


@pytest.fixture(name="partial_then_failing_write")
def _partial_then_failing_write(module: ModuleType, monkeypatch: pytest.MonkeyPatch):
    """Make ``os.write`` store a two-byte prefix once then fail; return the real ``os.write``."""
    original_write = module.os.write
    calls = 0

    def _partial_then_fail(fd: int, payload: object) -> int:
        """Write a short prefix on the first call, then inject the staged-write failure."""
        nonlocal calls
        calls += 1
        if calls == 1:
            return original_write(fd, bytes(payload)[:2])
        raise OSError(errno.EIO, "injected staged write failure")

    monkeypatch.setattr(module.os, "write", _partial_then_fail)
    return original_write


class TestDirectories:
    """Private and protected directory creation relative to a held root descriptor."""

    def test_private_path_creation_is_contained_and_idempotent(self, module: ModuleType, root_fd: int, tmp_path: Path):
        """Create only the explicit private components and reopen the same directory identity.

        A fresh root gains ``codex-rig/shims`` at mode 0700; a second call reports nothing created and resolves to the
        same device and inode, so repeated installs never churn the namespace.
        """
        first_fd, created = module.create_private_path(root_fd, ("codex-rig", "shims"))
        first = module.directory_identity(first_fd)
        os.close(first_fd)
        second_fd, repeated = module.create_private_path(root_fd, ("codex-rig", "shims"))
        second = module.directory_identity(second_fd)
        os.close(second_fd)

        assert created == ("codex-rig", "shims")
        assert repeated == ()
        assert first == second
        assert stat.S_IMODE((tmp_path / "codex-rig" / "shims").stat().st_mode) == 0o700

    def test_private_mode_is_independent_of_umask(self, module: ModuleType, root_fd: int, tmp_path: Path):
        """Create a private directory with exact 0700 permissions even under a restrictive umask.

        A umask of 0777 would strip every bit from a naive ``mkdir``; the primitive must set the mode explicitly.
        """
        previous_umask = os.umask(0o777)
        try:
            child_fd, created = module.create_directory_at(root_fd, "child")
        finally:
            os.umask(previous_umask)
        os.close(child_fd)

        assert created is True
        assert stat.S_IMODE((tmp_path / "child").stat().st_mode) == 0o700

    def test_existing_protected_directory_keeps_its_mode(self, module: ModuleType, root_fd: int, tmp_path: Path):
        """Open an owned protected namespace without changing its public read mode.

        ``~/.codex/agents`` is legitimately 0755; opening it with ``private=False`` must not tighten or recreate it.
        """
        child = tmp_path / "agents"
        child.mkdir(mode=0o700)
        child.chmod(0o755)

        child_fd, created = module.create_directory_at(root_fd, "agents", private=False)
        identity = module.directory_identity(child_fd, private=False)
        os.close(child_fd)

        assert created is False
        assert identity.mode == "0755"
        assert stat.S_IMODE(child.stat().st_mode) == 0o755

    @pytest.mark.parametrize(
        "mode",
        [
            _retainable_mode_case(0o775, "group-writable"),
            _retainable_mode_case(0o4700, "setuid"),
            _retainable_mode_case(0o2700, "setgid"),
            _retainable_mode_case(0o1700, "sticky"),
        ],
    )
    def test_protected_directory_rejects_group_writable_or_special_modes(
        self, module: ModuleType, root_fd: int, tmp_path: Path, mode: int
    ):
        """Reject target namespaces that another principal could mutate or redirect.

        Group-write and setuid/setgid/sticky bits on an existing protected directory make its entries substitutable, so
        opening it fails with the observed mode named in the message.
        """
        child = tmp_path / "agents"
        child.mkdir(mode=0o700)
        child.chmod(mode)
        message = f"unsafe protected directory mode: expected no group/world write or special bits, observed {mode:04o}"

        with pytest.raises(module.PosixPrimitiveError, match=message):
            module.create_directory_at(root_fd, "agents", private=False)

    def test_private_path_rejects_symlink_component(self, module: ModuleType, root_fd: int, tmp_path: Path):
        """Refuse a link where a held private child directory is required.

        ``codex-rig`` is a symlink to an outside directory; following it would create ``shims`` outside the root.
        """
        outside = tmp_path / "outside"
        outside.mkdir(mode=0o700)
        (tmp_path / "codex-rig").symlink_to(outside, target_is_directory=True)

        with pytest.raises(module.PosixPrimitiveError):
            module.create_private_path(root_fd, ("codex-rig", "shims"))

        assert not (outside / "shims").exists()

    @pytest.mark.usefixtures("restore_tmp_mode")
    def test_private_path_rejects_group_writable_parent(self, module: ModuleType, root_fd: int, tmp_path: Path):
        """Refuse path-following permission repair in a substitutable namespace.

        The root itself is group-writable (0770), so creating children inside it would let another principal swap them;
        the primitive fails before creating anything.
        """
        tmp_path.chmod(0o770)

        with pytest.raises(module.PosixPrimitiveError, match="unsafe protected directory mode"):
            module.create_private_path(root_fd, ("codex-rig", "shims"))

        assert not (tmp_path / "codex-rig").exists()

    def test_created_directory_descriptor_closes_when_fsync_fails(
        self, module: ModuleType, root_fd: int, monkeypatch: pytest.MonkeyPatch
    ):
        """Do not leak a new directory capability after a durability failure.

        The first sync of the freshly opened child fails with EIO; the descriptor opened for it must already be closed
        when the error reaches the caller.
        """
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

        with pytest.raises(OSError, match="injected"):
            module.create_directory_at(root_fd, "child")

        assert len(opened) == 1
        with pytest.raises(OSError, match=r"\[Errno 9\]"):
            os.fstat(opened[0])


class TestReadRegular:
    """Descriptor-relative reads of regular files."""

    def test_rejects_same_size_concurrent_rewrite(
        self, module: ModuleType, root_fd: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Reject a torn read even when a concurrent writer preserves file size.

        A 128 KiB file is rewritten with different bytes of identical length after its first chunk is read, so only a
        content or identity recheck can notice the change.
        """
        original = b"a" * 131_072
        replacement = b"b" * len(original)
        path = _private_file(tmp_path / "role.toml", original)
        original_read = module.os.read
        calls = 0

        def _rewrite_after_first_chunk(fd: int, size: int) -> bytes:
            """Rewrite the file after the first read chunk to simulate a concurrent writer."""
            nonlocal calls
            chunk = original_read(fd, size)
            calls += 1
            if calls == 1:
                _private_file(path, replacement)
            return chunk

        monkeypatch.setattr(module.os, "read", _rewrite_after_first_chunk)

        with pytest.raises(module.PosixPrimitiveError, match="changed during read"):
            module.read_regular_at(root_fd, "role.toml", expected_mode=0o600)

    def test_rejects_fifo_without_blocking(self, module: ModuleType, root_fd: int, tmp_path: Path):
        """Fail closed on a FIFO rather than waiting for a writer.

        A FIFO named like the role file would block a naive blocking open forever; the read must refuse it.
        """
        os.mkfifo(tmp_path / "role.toml", mode=0o600)

        with pytest.raises(module.PosixPrimitiveError, match="unsafe regular"):
            module.read_regular_at(root_fd, "role.toml")


class TestExclusiveWriteAndPublish:
    """Exclusive staging writes and no-clobber hardlink publication."""

    def test_publish_shares_staged_inode_and_bytes(self, module: ModuleType, roots: tuple[int, int, Path, Path]):
        """Publish exact staged bytes as a hardlink of the staged artifact.

        The target gains the payload under a new name backed by the staged inode, so journal authority can later verify
        the same file rather than a copy.
        """
        source_fd, target_fd, source, target = roots
        payload = b"generated-shim\n"
        staged = module.write_exclusive_at(source_fd, "role.toml", payload)

        published = module.publish_noclobber(
            source_fd, "role.toml", target_fd, "role.toml", expected_hash=sha256_hex(payload)
        )

        assert (target / "role.toml").read_bytes() == payload
        assert (staged.device, staged.inode) == (published.device, published.inode)
        assert (source / "role.toml").read_bytes() == payload

    def test_publish_never_clobbers_occupied_target(self, module: ModuleType, roots: tuple[int, int, Path, Path]):
        """Refuse to publish over an existing target name and leave both files unchanged."""
        source_fd, target_fd, source, target = roots
        payload = b"generated-shim\n"
        module.write_exclusive_at(source_fd, "role.toml", payload)
        module.publish_noclobber(source_fd, "role.toml", target_fd, "role.toml", expected_hash=sha256_hex(payload))

        with pytest.raises(module.PosixPrimitiveError):
            module.publish_noclobber(source_fd, "role.toml", target_fd, "role.toml", expected_hash=sha256_hex(payload))

        assert (target / "role.toml").read_bytes() == payload
        assert (source / "role.toml").read_bytes() == payload

    def test_protected_target_round_trip_preserves_mode(self, module: ModuleType, roots: tuple[int, int, Path, Path]):
        """Publish, unlink the staged link, detach, and restore through an existing 0755 protected target.

        The sequence is one logical install-then-rollback; none of the primitives may tighten the shared target.
        """
        source_fd, target_fd, source, target = roots
        target.chmod(0o755)
        payload = b"generated-shim\n"
        digest = sha256_hex(payload)
        module.write_exclusive_at(source_fd, "staged.toml", payload)

        module.publish_noclobber(source_fd, "staged.toml", target_fd, "role.toml", expected_hash=digest)
        module.unlink_verified_at(source_fd, "staged.toml", expected_hash=digest, expected_mode=0o600, expected_links=2)
        module.detach_verified(target_fd, "role.toml", source_fd, "quarantine.toml", expected_hash=digest)
        module.restore_quarantine_at(source_fd, "quarantine.toml", target_fd, "role.toml", expected_hash=digest)

        assert (target / "role.toml").read_bytes() == payload
        assert not (source / "quarantine.toml").exists()
        assert stat.S_IMODE(target.stat().st_mode) == 0o755

    def test_initial_journal_publishes_then_retires_initial_link(
        self, module: ModuleType, root_fd: int, tmp_path: Path
    ):
        """Publish journal authority and retire only its verified initial link.

        The first journal is staged as ``journal.initial.json`` and hardlinked to ``journal.json`` so a crash between
        the two steps is recoverable; afterwards only the final name remains with a single link.
        """
        payload = b'{"journal":"preparing"}'

        identity = module.write_initial_journal(root_fd, payload)

        assert (tmp_path / "journal.json").read_bytes() == payload
        assert not (tmp_path / "journal.initial.json").exists()
        assert (tmp_path / "journal.json").stat().st_nlink == 1
        assert identity.sha256 == sha256_hex(payload)

    @pytest.mark.usefixtures("partial_then_failing_write")
    def test_failed_write_removes_only_its_partial_artifact(self, module: ModuleType, root_fd: int, tmp_path: Path):
        """Leave no unauthenticated transaction residue after a staged write fails midway.

        Two bytes reach the file before the second write raises; the partial file this call created is removed.
        """
        with pytest.raises(OSError, match="injected"):
            module.write_exclusive_at(root_fd, "after.toml", b"complete-payload")

        assert not (tmp_path / "after.toml").exists()

    def test_failed_write_never_unlinks_a_substituted_path(
        self,
        module: ModuleType,
        root_fd: int,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        partial_then_failing_write,
    ):
        """Preserve both the partial evidence and a concurrent replacement.

        Before cleanup runs the partial path is renamed away and a different file takes its name; cleanup must see the
        identity change, refuse to unlink, and report that it is uncertain.
        """
        original_write = partial_then_failing_write
        original_cleanup = module._cleanup_created_file

        def _substitute_before_cleanup(parent_fd: int, name: str, identity: object) -> None:
            """Replace the partial path before cleanup to exercise identity protection."""
            os.rename(name, "partial.evidence", src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            replacement_fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent_fd)
            original_write(replacement_fd, b"replacement")
            os.close(replacement_fd)
            original_cleanup(parent_fd, name, identity)

        monkeypatch.setattr(module, "_cleanup_created_file", _substitute_before_cleanup)

        with pytest.raises(module.PosixPrimitiveError, match="cleanup is uncertain"):
            module.write_exclusive_at(root_fd, "after.toml", b"complete-payload")

        assert (tmp_path / "after.toml").read_bytes() == b"replacement"
        assert (tmp_path / "partial.evidence").read_bytes() == b"co"


class TestDetach:
    """Atomic detach of an approved target into private quarantine."""

    def test_preserves_exact_target_in_quarantine(self, module: ModuleType, roots: tuple[int, int, Path, Path]):
        """Detach approved bytes atomically and retain them as exact evidence.

        The target name disappears and the same bytes are readable under the quarantine name with a matching hash.
        """
        source_fd, target_fd, source, target = roots
        payload = b"owned-before\n"
        _put(target_fd, "role.toml", payload)

        identity = module.detach_verified(
            target_fd, "role.toml", source_fd, "quarantine.toml", expected_hash=sha256_hex(payload)
        )

        assert not (target / "role.toml").exists()
        assert (source / "quarantine.toml").read_bytes() == payload
        assert identity.sha256 == sha256_hex(payload)

    def test_mismatch_restores_observed_bytes_without_journal_substitution(
        self, module: ModuleType, roots: tuple[int, int, Path, Path], monkeypatch: pytest.MonkeyPatch
    ):
        """Restore the exact detached race observation when the target name stays absent.

        Another writer changes the target bytes right before the rename, so the detached file no longer matches the
        approved hash; the observed bytes go back unmodified and the error reports the restore.
        """
        source_fd, target_fd, source, target = roots
        approved = b"approved-content\n"
        raced = b"concurrent-content\n"
        _put(target_fd, "role.toml", approved)
        original_rename = module._rename_noreplace

        def _replace_before_detach(*args: object, **kwargs: object) -> None:
            """Simulate a target byte race immediately before atomic detach."""
            _put(target_fd, "role.toml", raced, os.O_TRUNC)
            original_rename(*args, **kwargs)

        monkeypatch.setattr(module, "_rename_noreplace", _replace_before_detach)

        with pytest.raises(module.DetachedMismatchError) as captured:
            module.detach_verified(
                target_fd, "role.toml", source_fd, "quarantine.toml", expected_hash=sha256_hex(approved)
            )

        assert captured.value.restored is True
        assert (target / "role.toml").read_bytes() == raced
        assert (source / "quarantine.toml").read_bytes() == raced
        assert (target / "role.toml").stat().st_ino == (source / "quarantine.toml").stat().st_ino

    def test_never_replaces_an_occupied_quarantine(
        self, module: ModuleType, roots: tuple[int, int, Path, Path], monkeypatch: pytest.MonkeyPatch
    ):
        """Preserve both files when the private quarantine name becomes occupied.

        The quarantine name is created by someone else immediately before the protected rename; detach fails and the
        approved target stays in place.
        """
        source_fd, target_fd, source, target = roots
        approved = b"approved-content\n"
        occupied = b"existing-quarantine\n"
        _put(target_fd, "role.toml", approved)
        original_rename = module._rename_noreplace

        def _occupy_immediately_before_rename(*args: object, **kwargs: object) -> None:
            """Occupy the quarantine name immediately before the protected rename."""
            _put(source_fd, "quarantine.toml", occupied)
            original_rename(*args, **kwargs)

        monkeypatch.setattr(module, "_rename_noreplace", _occupy_immediately_before_rename)

        with pytest.raises(module.PosixPrimitiveError):
            module.detach_verified(
                target_fd, "role.toml", source_fd, "quarantine.toml", expected_hash=sha256_hex(approved)
            )

        assert (target / "role.toml").read_bytes() == approved
        assert (source / "quarantine.toml").read_bytes() == occupied


class TestOwnedReplace:
    """Replacement of a manager-owned journal file by its fixed successor name."""

    def test_rejects_wrong_previous_hash(self, module: ModuleType, root_fd: int, tmp_path: Path):
        """Refuse to replace a file whose bytes do not match the expected preimage hash."""
        module.write_exclusive_at(root_fd, "journal.json", b"before")

        with pytest.raises(module.PosixPrimitiveError, match="hash mismatch"):
            module.replace_owned_at(
                root_fd, "journal.json", b"after", temporary="journal.next.json", expected_hash="a" * 64
            )

        assert (tmp_path / "journal.json").read_bytes() == b"before"

    def test_replaces_exact_preimage_with_private_mode(self, module: ModuleType, root_fd: int, tmp_path: Path):
        """Replace the exact manager-owned preimage and keep the result owner-only (0600)."""
        module.write_exclusive_at(root_fd, "journal.json", b"before")

        result = module.replace_owned_at(
            root_fd, "journal.json", b"after", temporary="journal.next.json", expected_hash=sha256_hex(b"before")
        )

        assert (tmp_path / "journal.json").read_bytes() == b"after"
        assert result.mode == "0600"

    def test_rejects_unrecognized_state_temporary(self, module: ModuleType, root_fd: int):
        """Keep state publication out of the fixed journal-successor primitive.

        Only the journal successor name is supported here; ``state.json`` must go through the dedicated state
        publication path, so any other temporary name is refused as unsupported.
        """
        with pytest.raises(module.PosixPrimitiveError, match="unsupported"):
            module.replace_owned_at(root_fd, "state.json", b"after", temporary="state.next.json", expected_hash=None)


class TestStatePublication:
    """Publish and restore manager state through transaction-bound artifacts."""

    def test_publish_consumes_journal_bound_after_link(self, module: ModuleType, roots: tuple[int, int, Path, Path]):
        """Replace exact manager state from the staged same-filesystem artifact.

        ``state.after.json`` is linked as ``state.publish.json`` then renamed over ``state.json``, so state and the
        staged artifact share one inode (link count 2) and no publish link remains.
        """
        transaction_fd, state_fd, transaction, state_root = roots
        before = b"before-state"
        after = b"after-state"
        module.write_exclusive_at(state_fd, "state.json", before)
        module.write_exclusive_at(transaction_fd, "state.after.json", after)

        result = module.publish_state_from_transaction(
            transaction_fd, state_fd, expected_after_hash=sha256_hex(after), expected_before_hash=sha256_hex(before)
        )

        assert (state_root / "state.json").read_bytes() == after
        assert not (transaction / "state.publish.json").exists()
        assert (transaction / "state.after.json").stat().st_ino == (state_root / "state.json").stat().st_ino
        assert result.link_count == 2

    def test_failed_replace_retains_exact_publish_evidence(
        self, module: ModuleType, roots: tuple[int, int, Path, Path], monkeypatch: pytest.MonkeyPatch
    ):
        """Leave the journal-bound publish link recoverable when the final replace fails.

        State keeps its previous bytes and ``state.publish.json`` stays behind with the new bytes, which is exactly what
        recovery needs to finish or undo the publication.
        """
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
                transaction_fd, state_fd, expected_after_hash=sha256_hex(after), expected_before_hash=sha256_hex(before)
            )

        assert (state_root / "state.json").read_bytes() == before
        assert (transaction / "state.publish.json").read_bytes() == after

    def test_restore_uses_bound_before_image_without_clobber(
        self, module: ModuleType, roots: tuple[int, int, Path, Path]
    ):
        """Restore prior state while retaining the detached current-state evidence.

        Current state matches the expected hash, so ``state.before.json`` replaces it and the current bytes stay in
        ``state.after.json`` untouched.
        """
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
            expected_before_hash=sha256_hex(before),
            expected_current_hash=sha256_hex(current),
        )

        assert restored is not None
        assert restored.sha256 == sha256_hex(before)
        assert (state_root / "state.json").read_bytes() == before
        assert (transaction / "state.after.json").read_bytes() == current

    def test_restore_rejects_foreign_current_state(self, module: ModuleType, roots: tuple[int, int, Path, Path]):
        """Refuse to restore over state bytes that match neither the expected current nor before hash."""
        transaction_fd, state_fd, _, state_root = roots
        before = b"before-state"
        current = b"current-state"
        module.write_exclusive_at(transaction_fd, "state.before.json", before)
        module.write_exclusive_at(transaction_fd, "state.after.json", current)
        _put(state_fd, "state.json", b"foreign")

        with pytest.raises(module.PosixPrimitiveError, match="hash"):
            module.restore_state_from_transaction(
                transaction_fd,
                state_fd,
                before_exists=True,
                expected_before_hash=sha256_hex(before),
                expected_current_hash=sha256_hex(current),
            )

        assert (state_root / "state.json").read_bytes() == b"foreign"

    def test_prior_absence_removes_exact_current_state(self, module: ModuleType, roots: tuple[int, int, Path, Path]):
        """Remove the exact current state when the transaction began with no state file."""
        transaction_fd, state_fd, _, state_root = roots
        current = b"current-state"
        module.write_exclusive_at(transaction_fd, "state.after.json", current)
        module.write_exclusive_at(state_fd, "state.json", current)

        removed = module.restore_state_from_transaction(
            transaction_fd,
            state_fd,
            before_exists=False,
            expected_before_hash=None,
            expected_current_hash=sha256_hex(current),
        )

        assert removed is None
        assert not (state_root / "state.json").exists()

    def test_prior_absence_rejects_missing_current_state_by_default(
        self, module: ModuleType, roots: tuple[int, int, Path, Path]
    ):
        """Treat an already-absent current state as an error unless replay is explicitly allowed."""
        transaction_fd, state_fd, _, _ = roots
        module.write_exclusive_at(transaction_fd, "state.after.json", b"current-state")

        with pytest.raises(module.PosixPrimitiveError, match=r"state\.json"):
            module.restore_state_from_transaction(
                transaction_fd,
                state_fd,
                before_exists=False,
                expected_before_hash=None,
                expected_current_hash=sha256_hex(b"current-state"),
            )

    def test_prior_absence_replay_allows_missing_current_state(
        self, module: ModuleType, roots: tuple[int, int, Path, Path]
    ):
        """Return None for a replayed removal when ``allow_current_absent`` is set and state is already gone."""
        transaction_fd, state_fd, _, state_root = roots
        module.write_exclusive_at(transaction_fd, "state.after.json", b"current-state")

        replayed = module.restore_state_from_transaction(
            transaction_fd,
            state_fd,
            before_exists=False,
            expected_before_hash=None,
            expected_current_hash=sha256_hex(b"current-state"),
            allow_current_absent=True,
        )

        assert replayed is None
        assert not (state_root / "state.json").exists()


class TestCoordinationLock:
    """The fixed ``.codex-rig-shims.lock`` coordination lock."""

    def test_rejects_path_swap_after_flock(
        self, module: ModuleType, root_fd: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Close a held lock when the approved fixed pathname is replaced after acquisition.

        The lock file is swapped for a new inode right after ``flock`` succeeds; identity revalidation must reject it
        and close the descriptor rather than return a lock on an orphaned inode.
        """
        lock = _private_file(tmp_path / LOCK_NAME)
        approved = _lock_identity(lock)
        original_flock = module.fcntl.flock
        held: list[int] = []

        def _replace_after_lock(fd: int, operation: int) -> None:
            """Replace the lock path after acquisition to verify identity revalidation."""
            original_flock(fd, operation)
            held.append(fd)
            lock.rename(tmp_path / "old-lock")
            _private_file(lock)

        monkeypatch.setattr(module.fcntl, "flock", _replace_after_lock)

        with pytest.raises(module.PosixPrimitiveError, match="path changed"):
            module.acquire_coordination_lock(root_fd, intent="open-existing", expected_identity=approved)

        assert len(held) == 1
        with pytest.raises(OSError, match=r"\[Errno 9\]"):
            os.fstat(held[0])

    def test_second_holder_is_refused_while_lock_is_held(self, module: ModuleType, root_fd: int):
        """Reject contention with ``LockBusyError`` while another descriptor holds the lock."""
        first = module.acquire_coordination_lock(root_fd, intent="create-if-absent", expected_identity=None)
        identity = os.fstat(first)

        with pytest.raises(module.LockBusyError):
            module.acquire_coordination_lock(
                root_fd, intent="open-existing", expected_identity=(identity.st_dev, identity.st_ino)
            )

        os.close(first)

    def test_lock_can_be_reacquired_after_release_with_private_mode(
        self, module: ModuleType, root_fd: int, tmp_path: Path
    ):
        """Reopen the same lock identity once released; the lock file is owner-only (0600)."""
        first = module.acquire_coordination_lock(root_fd, intent="create-if-absent", expected_identity=None)
        identity = os.fstat(first)
        os.close(first)

        second = module.acquire_coordination_lock(
            root_fd, intent="open-existing", expected_identity=(identity.st_dev, identity.st_ino)
        )
        os.close(second)

        assert stat.S_IMODE((tmp_path / LOCK_NAME).stat().st_mode) == 0o600

    @pytest.mark.usefixtures("restore_tmp_mode")
    def test_create_rejects_group_writable_home(self, module: ModuleType, root_fd: int, tmp_path: Path):
        """Refuse to create the lock once the approved home became substitutable (0770); nothing is created."""
        tmp_path.chmod(0o770)

        with pytest.raises(module.PosixPrimitiveError, match="unsafe protected directory mode"):
            module.acquire_coordination_lock(root_fd, intent="create-if-absent", expected_identity=None)

        assert not (tmp_path / LOCK_NAME).exists()

    @pytest.mark.usefixtures("restore_tmp_mode")
    def test_open_existing_rejects_group_writable_home(self, module: ModuleType, root_fd: int, tmp_path: Path):
        """Refuse to reuse an existing lock after the home became substitutable; the lock file is untouched."""
        lock = _private_file(tmp_path / LOCK_NAME)
        approved = _lock_identity(lock)
        tmp_path.chmod(0o770)

        with pytest.raises(module.PosixPrimitiveError, match="unsafe protected directory mode"):
            module.acquire_coordination_lock(root_fd, intent="open-existing", expected_identity=approved)

        assert _lock_identity(lock) == approved
        assert lock.stat().st_size == 0

    def test_never_follows_a_link(self, module: ModuleType, root_fd: int, tmp_path: Path):
        """Preserve an external file when the fixed lock name is a symlink to it."""
        outside = _private_file(tmp_path / "outside")
        (tmp_path / LOCK_NAME).symlink_to(outside)

        with pytest.raises(module.PosixPrimitiveError):
            module.acquire_coordination_lock(root_fd, intent="open-existing", expected_identity=(1, 1))

        assert outside.read_bytes() == b""


class TestVerifiedUnlink:
    """Unlink refuses every unapproved target identity or shape."""

    def test_removes_exact_bytes_and_syncs_directory(
        self, module: ModuleType, root_fd: int, tmp_path: Path, fsynced: list[int]
    ):
        """Remove the approved file and make the removal durable by syncing the directory once."""
        payload = b"published-target\n"
        path = _private_file(tmp_path / "role.toml", payload)

        removed = module.unlink_verified_at(
            root_fd, "role.toml", expected_hash=sha256_hex(payload), expected_mode=0o600
        )

        assert removed is True
        assert not path.exists()
        assert fsynced == [root_fd]

    def test_rejects_missing_target_by_default(self, module: ModuleType, root_fd: int):
        """Treat an absent target as an error unless replay absence is explicitly permitted."""
        with pytest.raises(module.PosixPrimitiveError):
            module.unlink_verified_at(root_fd, "role.toml", expected_hash=sha256_hex(b"x"), expected_mode=0o600)

    def test_allows_missing_target_when_replaying(self, module: ModuleType, root_fd: int):
        """Return False instead of failing when ``allow_absent`` is set and the target is already gone."""
        replayed = module.unlink_verified_at(
            root_fd, "role.toml", expected_hash=sha256_hex(b"x"), expected_mode=0o600, allow_absent=True
        )

        assert replayed is False

    def test_rejects_hash_mismatch(self, module: ModuleType, root_fd: int, tmp_path: Path):
        """Preserve a regular file whose bytes do not match the approved digest."""
        role = _private_file(tmp_path / "role.toml", b"approved\n")

        with pytest.raises(module.PosixPrimitiveError, match="hash"):
            module.unlink_verified_at(root_fd, "role.toml", expected_hash="0" * 64, expected_mode=0o600)

        assert role.read_bytes() == b"approved\n"

    def test_rejects_symlink(self, module: ModuleType, root_fd: int, tmp_path: Path):
        """Preserve an external target when the selected name is a symlink."""
        outside = _private_file(tmp_path / "outside", b"outside\n")
        (tmp_path / "linked.toml").symlink_to(outside)

        with pytest.raises(module.PosixPrimitiveError):
            module.unlink_verified_at(
                root_fd, "linked.toml", expected_hash=sha256_hex(b"outside\n"), expected_mode=0o600
            )

        assert outside.read_bytes() == b"outside\n"

    def test_rejects_unexpected_hardlink_count(self, module: ModuleType, root_fd: int, tmp_path: Path):
        """Refuse to unlink a file with a second link unless the caller approved that link count."""
        approved = b"approved\n"
        hard = _private_file(tmp_path / "hard.toml", approved)
        os.link(hard, tmp_path / "hard-copy.toml")

        with pytest.raises(module.PosixPrimitiveError, match="link count"):
            module.unlink_verified_at(root_fd, "hard.toml", expected_hash=sha256_hex(approved), expected_mode=0o600)

        assert hard.exists()

    def test_accepts_explicitly_approved_hardlink_count(self, module: ModuleType, root_fd: int, tmp_path: Path):
        """Unlink one name of a two-link file when ``expected_links=2``; the other name keeps the bytes."""
        approved = b"approved\n"
        hard = _private_file(tmp_path / "hard.toml", approved)
        os.link(hard, tmp_path / "hard-copy.toml")

        removed = module.unlink_verified_at(
            root_fd, "hard.toml", expected_hash=sha256_hex(approved), expected_mode=0o600, expected_links=2
        )

        assert removed
        assert (tmp_path / "hard-copy.toml").read_bytes() == approved

    def test_rejects_concurrent_substitution(
        self, module: ModuleType, root_fd: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Preserve both files when the approved inode is replaced just before the unlink.

        The approved file is renamed aside and a different file takes its name; the same-inode check must refuse the
        unlink, leaving the replacement and the approved evidence in place.
        """
        approved = b"approved\n"
        replacement = b"replacement\n"
        role = _private_file(tmp_path / "role.toml", approved)
        original_unlink = module._unlink_same_inode

        def _substitute_before_unlink(parent_fd: int, name: str, identity: object) -> None:
            """Swap the path before unlink so cleanup must refuse the replacement."""
            os.rename(name, "approved.evidence", src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            _put(parent_fd, name, replacement)
            original_unlink(parent_fd, name, identity)

        monkeypatch.setattr(module, "_unlink_same_inode", _substitute_before_unlink)

        with pytest.raises(module.PosixPrimitiveError, match="identity changed"):
            module.unlink_verified_at(root_fd, "role.toml", expected_hash=sha256_hex(approved), expected_mode=0o600)

        assert role.read_bytes() == replacement
        assert (tmp_path / "approved.evidence").read_bytes() == approved


class TestRestoreQuarantine:
    """Restore one exact quarantined file into an absent target name."""

    def test_restores_exact_file_into_absent_target(self, module: ModuleType, roots: tuple[int, int, Path, Path]):
        """Move the quarantined bytes to the target name and drop the quarantine entry."""
        quarantine_fd, target_fd, quarantine, target = roots
        payload = b"before-image\n"
        module.write_exclusive_at(quarantine_fd, "role.toml", payload)

        restored = module.restore_quarantine_at(
            quarantine_fd, "role.toml", target_fd, "role.toml", expected_hash=sha256_hex(payload)
        )

        assert restored.sha256 == sha256_hex(payload)
        assert not (quarantine / "role.toml").exists()
        assert (target / "role.toml").read_bytes() == payload

    def test_never_clobbers_an_occupied_target(self, module: ModuleType, roots: tuple[int, int, Path, Path]):
        """Refuse to restore over an existing target name; both files keep their bytes."""
        quarantine_fd, target_fd, quarantine, target = roots
        payload = b"before-image\n"
        module.write_exclusive_at(quarantine_fd, "next.toml", payload)
        module.write_exclusive_at(target_fd, "role.toml", b"occupant\n")

        with pytest.raises(module.PosixPrimitiveError):
            module.restore_quarantine_at(
                quarantine_fd, "next.toml", target_fd, "role.toml", expected_hash=sha256_hex(payload)
            )

        assert (quarantine / "next.toml").read_bytes() == payload
        assert (target / "role.toml").read_bytes() == b"occupant\n"

    def test_rejects_hash_mismatch(self, module: ModuleType, roots: tuple[int, int, Path, Path]):
        """Refuse to restore quarantined bytes that do not match the expected hash; nothing reaches the target."""
        quarantine_fd, target_fd, quarantine, target = roots
        payload = b"before-image\n"
        module.write_exclusive_at(quarantine_fd, "next.toml", payload)

        with pytest.raises(module.PosixPrimitiveError, match="hash"):
            module.restore_quarantine_at(quarantine_fd, "next.toml", target_fd, "other.toml", expected_hash="0" * 64)

        assert (quarantine / "next.toml").read_bytes() == payload
        assert not (target / "other.toml").exists()


class TestTransactionCleanup:
    """Bounded, non-recursive removal of named transaction entries."""

    def test_removes_artifacts_then_verified_empty_directories(
        self, module: ModuleType, root_fd: int, tmp_path: Path, fsynced: list[int]
    ):
        """Remove named files then empty directories in order and report the removed names.

        ``journal.json`` (hash and link count checked) goes first, then the empty directory; each removal syncs the
        parent directory.
        """
        artifact = _private_file(tmp_path / "journal.json", b"journal")
        empty = tmp_path / "empty"
        empty.mkdir(mode=0o700)

        removed = module.remove_transaction_entries_at(
            root_fd, (("journal.json", sha256_hex(b"journal"), 1), ("empty", None, None))
        )

        assert removed == ("journal.json", "empty")
        assert not artifact.exists()
        assert not empty.exists()
        assert fsynced == [root_fd, root_fd]

    def test_rejects_non_empty_directory_without_recursing(self, module: ModuleType, root_fd: int, tmp_path: Path):
        """Refuse to remove a directory that still holds evidence; its contents survive."""
        nonempty = tmp_path / "nonempty"
        nonempty.mkdir(mode=0o700)
        evidence = _private_file(nonempty / "evidence", b"retain")

        with pytest.raises(module.PosixPrimitiveError, match="empty"):
            module.remove_transaction_entries_at(root_fd, (("nonempty", None, None),))

        assert evidence.read_bytes() == b"retain"

    def test_rejects_missing_entry_by_default(self, module: ModuleType, root_fd: int):
        """Treat an absent entry as an error unless replay absence is explicitly permitted."""
        with pytest.raises(module.PosixPrimitiveError):
            module.remove_transaction_entries_at(root_fd, (("missing", None, None),))

    def test_allows_missing_entry_when_replaying(self, module: ModuleType, root_fd: int):
        """Report nothing removed when ``allow_absent`` is set and the entry is already gone."""
        replayed = module.remove_transaction_entries_at(root_fd, (("missing", None, None),), allow_absent=True)

        assert replayed == ()

    def test_rejects_linked_child(self, module: ModuleType, root_fd: int, tmp_path: Path):
        """Never follow a transaction child symlink during bounded cleanup; link and target both survive."""
        outside = tmp_path / "outside"
        outside.mkdir(mode=0o700)
        (tmp_path / "linked").symlink_to(outside, target_is_directory=True)

        with pytest.raises(module.PosixPrimitiveError):
            module.remove_transaction_entries_at(root_fd, (("linked", None, None),))

        assert (tmp_path / "linked").is_symlink()
        assert outside.is_dir()
