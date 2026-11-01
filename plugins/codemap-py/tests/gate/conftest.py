"""Fixtures shared by the gate tests."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(name="root")
def _root(tmp_path: Path) -> Path:
    """Create an empty project directory named ``proj``."""
    root = tmp_path / "proj"
    root.mkdir()
    return root
