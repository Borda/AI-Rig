"""Fixtures for the ``/oss:analyse evolution`` tests: an isolated fixture-repository builder."""

from __future__ import annotations

from pathlib import Path

import pytest
from _evolution_repos import RepoBuilder


@pytest.fixture
def evo_repo(tmp_path: Path) -> RepoBuilder:
    """Return a builder for an empty, isolated repository on ``main`` (no commits yet)."""
    return RepoBuilder.create(tmp_path / "repo", tmp_path / "gitcfg")
