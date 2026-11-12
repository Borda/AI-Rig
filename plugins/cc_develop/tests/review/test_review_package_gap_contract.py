"""Guard how develop:review reports a claim blocked by a missing or out-of-spec Python package.

Review agents may run installed static analyzers and dependency audits without fixes, but never execute reviewed code or
install packages. The consolidator retains unavailable checks with their declaring spec and closing route.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_CONSOLIDATOR = Path(__file__).resolve().parents[2] / "skills/review/templates/consolidator-prompt.md"


@pytest.mark.parametrize(
    "marker",
    [
        pytest.param("Review agents do not execute reviewed code, examples or tests", id="read-only-reason"),
        pytest.param("state the unavailable check, package", id="gap-reason-literal"),
        pytest.param("declaring spec `file:line`", id="declaring-spec-cited"),
        pytest.param("never close it by installing anything", id="no-install"),
        pytest.param("name the targeted check that would close it under `Next steps`", id="closing-route"),
    ],
)
def test_package_gap_stays_with_read_only_reason(marker: str) -> None:
    """A package gap in a read-only review keeps a concrete reason and a closing route, never an install.

    The consolidator prompt is the only place that tells the reporting agent how to word such a gap; each marker is one
    clause of that instruction and dropping any one lets the report claim a closure the review cannot perform.
    """
    assert marker in _CONSOLIDATOR.read_text(encoding="utf-8")
