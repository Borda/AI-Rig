"""A remediation result over refused gates names the single source refusal, never the refused gates as failures.

Refused gate rows stay out of ``checks_failed`` in ``gates.json``; the finalizer derives ``source-refused`` into the
result's failed checks, which the result validator requires whenever the gates record refused rows.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest
from test_remediation_finalize import HELPER


def _arguments(extra: str = "") -> argparse.Namespace:
    """Return finalize arguments with only the fields the result writer reads."""
    return argparse.Namespace(
        extra_checks_failed=extra,
        status="fail",
        critical=0,
        high=0,
        medium=0,
        low=0,
        confidence=0.9,
        artifact_path="result.json",
        recommendations="",
        follow_up="",
    )


@pytest.mark.parametrize(
    ("gates", "extra", "expected"),
    [
        pytest.param({"checks_failed": [], "checks_refused": ["lint", "tests"]}, "", "source-refused", id="refused"),
        pytest.param(
            {"checks_failed": ["review"], "checks_refused": ["lint"]},
            "independent-review-coverage",
            "review,source-refused,independent-review-coverage",
            id="mixed",
        ),
        pytest.param({"checks_failed": ["tests"]}, "", "tests", id="no-refusal"),
    ],
)
def test_result_checks_failed_name_the_refusal_once(gates: dict, extra: str, expected: str, tmp_path: Path) -> None:
    """Refused gates add ``source-refused`` once; ordinary failures and caller checks keep their order."""
    derived = HELPER._result_arguments(_arguments(extra), tmp_path, gates, {})

    assert derived.checks_failed == expected
