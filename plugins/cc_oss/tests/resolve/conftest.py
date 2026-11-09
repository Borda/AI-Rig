"""Shared fixtures for the resolve tests: the shipped ``SKILL.md`` text."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

#: Directory of the shipped resolve skill.
_RESOLVE_SKILL = Path(__file__).resolve().parents[2] / "skills/resolve"

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _resolve_source import read_resolve_source  # noqa: E402


@pytest.fixture
def skill() -> str:
    """Return resolve's SKILL.md plus its relocated step bodies in workflow order."""
    return read_resolve_source(_RESOLVE_SKILL / "SKILL.md")
