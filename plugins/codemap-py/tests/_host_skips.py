"""Host-capability probes and the ``skipif`` markers built from them, shared across the codemap test suite.

Each marker is evaluated once at import. Importable from every test module because ``conftest.py`` puts this directory
on ``sys.path``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest


def file_symlink_is_available() -> bool:
    """Return whether this host can create and resolve a file symlink."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        target = root / "target"
        link = root / "link"
        target.write_text("target\n", encoding="utf-8")
        try:
            link.symlink_to(target)
        except OSError:
            return False
        return link.is_symlink() and link.read_text(encoding="utf-8") == "target\n"


skip_file_symlink_unavailable = pytest.mark.skipif(
    not file_symlink_is_available(), reason="file symlink creation is unavailable on this host"
)


def executable_mode_is_preserved() -> bool:
    """Return whether this host preserves an executable mode set by ``chmod``."""
    with tempfile.TemporaryDirectory() as directory:
        candidate = Path(directory) / "candidate"
        candidate.write_text("candidate\n", encoding="utf-8")
        try:
            candidate.chmod(0o755)
        except OSError:
            return False
        return os.stat(candidate).st_mode & 0o777 == 0o755


skip_executable_mode_unavailable = pytest.mark.skipif(
    not executable_mode_is_preserved(), reason="chmod cannot preserve executable mode on this host"
)


def bash_runs_posix_script() -> bool:
    """Reject launchers that exist on PATH but cannot execute a POSIX script, including WSL stubs."""
    try:
        result = subprocess.run(["bash", "-c", "printf ok"], capture_output=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout == b"ok"


needs_git_and_bash = pytest.mark.skipif(
    shutil.which("git") is None or not bash_runs_posix_script(), reason="needs git and a working POSIX bash"
)
