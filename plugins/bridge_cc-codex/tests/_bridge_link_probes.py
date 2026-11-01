"""Host capability probes for link creation, shared by the Bridge tests that skip when a host denies a link type."""

from __future__ import annotations

import functools
import os
import tempfile
from pathlib import Path


@functools.cache
def directory_symlinks_supported() -> bool:
    """Probe directory-symlink creation without assuming Windows privileges."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        target = root / "target"
        link = root / "link"
        target.mkdir()
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            return False
        return link.is_symlink()


@functools.cache
def file_symlinks_supported() -> bool:
    """Probe file-symlink creation on platforms that restrict links."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        target = root / "target"
        link = root / "link"
        target.write_text("probe\n", encoding="utf-8", newline="\n")
        try:
            link.symlink_to(target)
        except OSError:
            return False
        return link.is_symlink()


@functools.cache
def hardlinks_supported() -> bool:
    """Probe same-filesystem hard-link creation without assuming host privilege or filesystem support."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        target = root / "target"
        link = root / "link"
        target.write_text("probe\n", encoding="utf-8")
        try:
            os.link(target, link)
        except OSError:
            return False
        return link.stat().st_nlink == 2
