"""Run real ``git`` commands against disposable test repositories."""

from __future__ import annotations

import subprocess
from pathlib import Path


def run_git(repo: Path, *args: str) -> str:
    """Run one real ``git`` command in ``repo`` and return its stripped stdout; failure raises.

    >>> from tempfile import TemporaryDirectory
    >>> with TemporaryDirectory() as directory:
    ...     run_git(Path(directory), "--version").startswith("git version")
    True
    """
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()
