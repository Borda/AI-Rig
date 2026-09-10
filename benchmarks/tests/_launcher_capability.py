"""Capability checks for benchmark launchers and their isolated filesystem artifacts."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path


def _private_filesystem_available() -> bool:
    """Probe the private modes and owner identity used by isolated benchmark homes."""
    if not hasattr(os, "getuid"):
        return False
    with tempfile.TemporaryDirectory() as scratch:
        directory = Path(scratch)
        probe = directory / "private"
        probe.write_bytes(b"")
        try:
            directory.chmod(0o700)
            probe.chmod(0o600)
            return (
                directory.stat().st_mode & 0o777 == 0o700
                and probe.stat().st_mode & 0o777 == 0o600
                and probe.stat().st_uid == os.getuid()
            )
        except OSError:
            return False


def _pinned_frozen_checkout_is_available(repo_path: Path, expected_commit: str) -> bool:
    """Admit a clean ordinary or linked checkout root at the benchmark baseline."""
    if not repo_path.is_dir() or not (repo_path / ".git").exists():
        return False
    try:
        head = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
        if head.returncode != 0 or head.stdout.strip() != expected_commit:
            return False
        status = subprocess.run(
            ["git", "-C", str(repo_path), "status", "--porcelain", "--untracked-files=all"],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return status.returncode == 0 and not status.stdout


def _raw_codemap_launchers_are_runnable(repo_root: Path) -> bool:
    """Probe both raw launchers, returning false for launch errors, timeouts, or nonzero exits.

    The missing-checkout example cannot start a subprocess because neither executable exists.

    >>> from tempfile import TemporaryDirectory
    >>> with TemporaryDirectory() as directory:
    ...     _raw_codemap_launchers_are_runnable(Path(directory))
    False
    """
    for name in ("scan-index", "scan-query"):
        launcher = repo_root / "plugins" / "codemap-py" / "bin" / name
        try:
            result = subprocess.run([str(launcher), "--help"], capture_output=True, text=True, timeout=30, check=False)
        except (OSError, subprocess.SubprocessError):
            return False
        if result.returncode != 0:
            return False
    return True
