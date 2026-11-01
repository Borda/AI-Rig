"""Subprocess runner shared by the tests that drive a repository checker script as its pre-commit hook does."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def run_checker(script: Path, *arguments: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    """Run ``script`` under the current interpreter and capture its exit code and text output."""
    return subprocess.run(
        [sys.executable, str(script), *arguments], capture_output=True, text=True, check=False, timeout=timeout
    )
