"""Tests for ``bin/_sentinel_io.py``, the private sentinel writer shared by the oss ``bin/`` scripts.

Sentinels live under a predictable name in a possibly machine-wide ``TMPDIR``. These tests pin that the writer replaces
whatever sits at the path instead of writing through it, creates the file private to its owner, and refuses — rather
than silently reusing — an entry it cannot remove.
"""

from __future__ import annotations

import os
import stat
import tempfile
from collections.abc import Iterator
from pathlib import Path

import _sentinel_io as sio
import pytest


def _can_symlink() -> bool:
    """Report whether this host lets the current user create a symlink (Windows needs a privilege for it)."""
    with tempfile.TemporaryDirectory() as directory:
        try:
            os.symlink(Path(directory) / "target", Path(directory) / "link")
        except (OSError, NotImplementedError):
            return False
    return True


_skip_without_symlinks = pytest.mark.skipif(not _can_symlink(), reason="host cannot create symlinks")


@pytest.fixture
def permissive_umask() -> Iterator[None]:
    """Clear the process umask for one test, so only the writer's own mode decides the file permissions."""
    previous = os.umask(0)
    yield
    os.umask(previous)


class TestWritePrivateText:
    """Covers ``write_private_text``: fresh private file, never a write through a planted entry."""

    def test_writes_text_with_lf_endings(self, tmp_path: Path) -> None:
        """The value lands as UTF-8 with LF endings, so ``IFS= read -r`` sees no stray carriage return.

        Readers fill shell variables from these files; a CRLF written on Windows would leave ``\\r`` inside the value.
        """
        target = tmp_path / "resolve-head-ref-shared"
        sio.write_private_text(target, "feature/ü-1\n")
        assert target.read_bytes() == "feature/ü-1\n".encode()

    def test_replaces_existing_file(self, tmp_path: Path) -> None:
        """A second write replaces the first value instead of appending or failing on the exclusive create."""
        target = tmp_path / "analyse-mode-shared"
        sio.write_private_text(target, "old\n")
        sio.write_private_text(target, "new\n")
        assert target.read_text(encoding="utf-8") == "new\n"

    @pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX permission bits")
    @pytest.mark.usefixtures("permissive_umask")
    def test_creates_owner_only_file(self, tmp_path: Path) -> None:
        """The sentinel is readable and writable by its owner only, even under a permissive umask.

        A group- or world-writable sentinel in a shared ``/tmp`` lets another user swap the value between the write and
        the step that reads it back.
        """
        target = tmp_path / "release-sentinel-shared"
        sio.write_private_text(target, "x\n")
        assert stat.S_IMODE(target.stat().st_mode) == 0o600

    @_skip_without_symlinks
    def test_removes_planted_symlink_instead_of_following_it(self, tmp_path: Path) -> None:
        """A symlink planted at the sentinel name is replaced; the file it points at keeps its content.

        ``Path.write_text`` would follow the link and overwrite the victim file with the sentinel value.
        """
        victim = tmp_path / "victim.txt"
        victim.write_text("precious\n", encoding="utf-8")
        target = tmp_path / "resolve-base-ref-shared"
        target.symlink_to(victim)
        sio.write_private_text(target, "main\n")
        assert (target.is_symlink(), target.read_text(encoding="utf-8"), victim.read_text(encoding="utf-8")) == (
            False,
            "main\n",
            "precious\n",
        )

    def test_refuses_directory_at_path(self, tmp_path: Path) -> None:
        """A directory sitting at the sentinel name is refused with an actionable ``PermissionError``."""
        target = tmp_path / "oss-detect-type-shared"
        target.mkdir()
        with pytest.raises(PermissionError, match="point TMPDIR at a directory only you can write"):
            sio.write_private_text(target, "issue")

    def test_refuses_entry_it_cannot_remove(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """An entry owned by another user in a sticky ``/tmp`` stops the write and stays untouched.

        Removal fails with ``EPERM`` there; the exclusive create must then never fall back to writing into that entry.
        The removal failure is simulated, since producing a second user's file needs root.
        """
        target = tmp_path / "resolve-preflight-GH_OK-shared"
        target.write_text("planted\n", encoding="utf-8")

        def deny_unlink(self: Path, missing_ok: bool = False) -> None:
            raise PermissionError(1, "Operation not permitted", str(self))

        monkeypatch.setattr(Path, "unlink", deny_unlink)
        with pytest.raises(PermissionError, match="refusing to write sentinel"):
            sio.write_private_text(target, "true\n")
        assert target.read_text(encoding="utf-8") == "planted\n"
