"""Distinguish fixed subprocess deadlines from optional caller override flags."""

from __future__ import annotations

from pathlib import Path

import check_skill_contracts as contracts
import pytest


@pytest.mark.parametrize(
    "source",
    [
        'subprocess.run(["tool"], timeout=30)',
        'DEADLINE = 30\nsubprocess.run(["tool"], timeout=DEADLINE)',
    ],
)
def test_fixed_enforced_deadline_needs_no_override(source: str) -> None:
    """An internal finite positive deadline is sufficient when every subprocess has one."""
    assert contracts.missing_timeout_flag(Path("helper.py"), source) is None


@pytest.mark.parametrize(
    "source",
    [
        'subprocess.run(["tool"], timeout=None)',
        'subprocess.run(["tool"], timeout=30)\nsubprocess.run(["other"])',
        'subprocess.run(["tool"], timeout=0)',
        pytest.param('subprocess.run(["tool"], timeout=' + "9" * 400 + ")", id="unrepresentable-deadline"),
    ],
)
def test_unbounded_subprocess_is_still_reported(source: str) -> None:
    """A fixed deadline on one call cannot hide an unbounded sibling call."""
    assert contracts.missing_timeout_flag(Path("helper.py"), source) is not None
