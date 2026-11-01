"""Shared fixtures for the resolve tests: the shipped ``SKILL.md`` text."""

from __future__ import annotations

from pathlib import Path

import pytest

#: Directory of the shipped resolve skill.
_RESOLVE_SKILL = Path(__file__).resolve().parents[2] / "skills/resolve"


@pytest.fixture
def skill() -> str:
    """Return the resolve ``SKILL.md`` text."""
    return (_RESOLVE_SKILL / "SKILL.md").read_text(encoding="utf-8")
