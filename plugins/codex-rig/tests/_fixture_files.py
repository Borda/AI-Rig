"""Fingerprint fixture files and directory trees so a test can prove a command wrote nothing."""

from __future__ import annotations

import hashlib
import stat
from pathlib import Path


def file_sha256(path: Path) -> str:
    """Return the lowercase SHA-256 digest of one file.

    Example:
        >>> len(file_sha256(Path(__file__)))
        64
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_bytes(root: Path) -> dict[Path, bytes]:
    """Map every file under ``root`` to its bytes, so a before/after comparison proves zero writes."""
    return {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def tree_snapshot(root: Path) -> tuple[tuple[object, ...], ...]:
    """Capture bytes and mutation-relevant metadata of ``root`` and everything under it, excluding atime."""
    rows = []
    for path in [root, *sorted(root.rglob("*"))]:
        metadata = path.lstat()
        rows.append(
            (
                str(path.relative_to(root)),
                stat.S_IFMT(metadata.st_mode),
                stat.S_IMODE(metadata.st_mode),
                metadata.st_ino,
                metadata.st_nlink,
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
                path.read_bytes() if stat.S_ISREG(metadata.st_mode) else None,
            )
        )
    return tuple(rows)
