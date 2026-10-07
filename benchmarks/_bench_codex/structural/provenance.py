"""Repository and index fingerprints recorded with every run."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path


def _repo_sha(repo_path: Path) -> str:
    """Return repository HEAD or ``unknown`` when the fixture has no Git metadata."""
    try:
        proc = subprocess.run(  # noqa: S603 - argv list, no shell
            ["git", "-C", str(repo_path), "rev-parse", "HEAD"],  # noqa: S607 - git/tool resolved via PATH on purpose
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else "unknown"


def _index_sha(index_path: Path | None) -> str:
    """Fingerprint an index file when one is configured."""
    if index_path is None or not index_path.is_file():
        return "unknown"
    try:
        return hashlib.sha256(index_path.read_bytes()).hexdigest()
    except OSError:
        return "unknown"
