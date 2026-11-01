"""Subprocess-result builders and the git availability mark shared by the cc_oss tests."""

from __future__ import annotations

import shutil
import subprocess

import pytest

#: Skip a test that runs a real git command on a machine without the git CLI.
requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git CLI not available")


def done(returncode: int = 0, stdout: str = "") -> subprocess.CompletedProcess[str]:
    """Build the result of one finished command, as a mocked ``subprocess.run`` returns it."""
    return subprocess.CompletedProcess([], returncode, stdout=stdout)


def completed_git(returncode: int = 0, stdout: str = "") -> subprocess.CompletedProcess[str]:
    """Build the result of one git command, as a mocked ``subprocess.run`` returns it."""
    return subprocess.CompletedProcess(args=["git"], returncode=returncode, stdout=stdout)
