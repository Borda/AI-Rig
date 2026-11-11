"""Probe whether the host preserves POSIX file permission bits."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path


def _file_modes_supported() -> bool:
    """Return whether chmod can distinguish group read permission on a temporary file."""
    with tempfile.TemporaryDirectory() as directory:
        candidate = Path(directory) / "mode-probe"
        candidate.touch()
        try:
            os.chmod(candidate, 0o640)
            return stat.S_IMODE(candidate.stat().st_mode) == 0o640
        except OSError:
            return False
