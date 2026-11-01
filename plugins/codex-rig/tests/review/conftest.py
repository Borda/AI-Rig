"""Fixtures shared by the review suites."""

from __future__ import annotations

from types import ModuleType

import pytest
from _final_handoff_support import load_finalizer


@pytest.fixture
def finalizer() -> ModuleType:
    """Fresh copy of the standalone finalizer module."""
    return load_finalizer()
