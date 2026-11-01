"""Fixtures shared by the packaging tests that install the GitHub read setup into an isolated Codex home."""

from __future__ import annotations

from pathlib import Path

import pytest
from _github_read_helpers import installed_plugin


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """Isolated Codex home directory path (not yet created)."""
    return tmp_path / "home"


@pytest.fixture
def plugin_root(home: Path) -> Path:
    """Installed package inside the isolated Codex home's versioned cache."""
    return installed_plugin(home)
