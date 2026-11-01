"""Run real Git against the disposable repositories that the review and loop suites build."""

from __future__ import annotations

import subprocess
from pathlib import Path


def git_bytes(root: Path, *arguments: str) -> bytes:
    """Run real Git against ``root`` and return its raw standard output, raising when Git fails."""
    return subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True).stdout


def git_text(root: Path, *arguments: str) -> str:
    """Run real Git against ``root`` and return its standard output as text with surrounding whitespace removed."""
    return subprocess.run(
        ["git", "-C", str(root), *arguments], capture_output=True, text=True, check=True
    ).stdout.strip()
