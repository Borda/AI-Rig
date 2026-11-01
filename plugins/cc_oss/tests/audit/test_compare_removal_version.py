"""Exercise release removal-version comparisons through the installed-style CLI."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "bin/compare_removal_version.py"


@pytest.mark.integration
@pytest.mark.parametrize(
    ("removal", "target", "expected"),
    [
        pytest.param("2.3", "2.4", "OVERDUE\n", id="past-removal"),
        pytest.param("2.4", "2.4", "OVERDUE\n", id="equal-removal"),
        pytest.param("2.5", "2.4", "FUTURE\n", id="future-removal"),
        pytest.param(' "v2.4rc1" ', "2.41", "OVERDUE\n", id="legacy-nondigit-stripping"),
        pytest.param("2.4.0", "2.4", "FUTURE\n", id="legacy-tuple-length"),
        pytest.param("next", "", "OVERDUE\n", id="empty-numeric-parts-default-zero"),
    ],
)
def test_numeric_tuple_comparison(removal: str, target: str, expected: str) -> None:
    """The script prints ``OVERDUE`` or ``FUTURE`` from the numeric-tuple comparison, non-SemVer quirks included.

    The audit consumes the exact stdout. Tokens are stripped of non-digit characters, a longer tuple outranks its
    prefix, and a token with no digits counts as zero, so these historical edge cases are pinned on purpose.
    """
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), removal, target], capture_output=True, text=True, check=False
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, expected, "")
