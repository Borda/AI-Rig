"""Guard how develop:review reports a claim blocked by a missing or out-of-spec Python package.

develop:review executes no code, so unlike oss:review it cannot close such a gap with an ephemeral overlay run. The
consolidator must keep the gap with that concrete reason and the declaring spec, never install anything, and name the
targeted run that would close it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_CONSOLIDATOR = Path(__file__).resolve().parents[1] / "skills/review/templates/consolidator-prompt.md"


@pytest.mark.parametrize(
    "marker",
    [
        pytest.param("develop:review executes no code", id="read-only-reason"),
        pytest.param("`not run: develop:review is read-only`", id="gap-reason-literal"),
        pytest.param("the declaring spec `file:line`", id="declaring-spec-cited"),
        pytest.param("never close it by installing anything", id="no-install"),
        pytest.param("name the targeted test run that would close it under `Next steps`", id="closing-route"),
    ],
)
def test_package_gap_stays_with_read_only_reason(marker: str) -> None:
    """A package gap in a read-only review keeps a concrete reason and a closing route, never an install."""
    assert marker in _CONSOLIDATOR.read_text(encoding="utf-8")
