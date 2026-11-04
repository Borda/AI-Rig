"""Verify the PR number extracted from a saved oss:review report for resolve's auto-selection."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "bin/extract_review_pr.py"


def _extract(report: Path) -> subprocess.CompletedProcess[str]:
    """Run the script on ``report`` and capture its streams."""
    return subprocess.run([sys.executable, str(_SCRIPT), str(report)], capture_output=True, text=True, check=False)


def _write_report(tmp_path: Path, directory: str, text: str) -> Path:
    """Write ``text`` as a review report under ``directory`` inside ``tmp_path``."""
    report = tmp_path / directory / "review-report.md"
    report.parent.mkdir(parents=True)
    report.write_text(text, encoding="utf-8")
    return report


@pytest.mark.integration
@pytest.mark.parametrize(
    ("directory", "text", "expected"),
    [
        pytest.param("pr-42/run-003", "---\nPR: #7\n---\n", "42", id="path-wins-over-header"),
        pytest.param("misc", "---\nPR: #7\n---\n", "7", id="yaml-pr-line"),
        pytest.param("misc", "PR: 12 (draft)\n", "12", id="yaml-pr-without-hash"),
        pytest.param("misc", "## Code Review: PR #19 parser\n", "19", id="heading-with-pr-word"),
        pytest.param("misc", "## Code Review: #5\n", "5", id="heading-bare-hash"),
        pytest.param("misc", "Title: v2 parser\nPR: #30\n", "30", id="ignores-digits-in-title"),
    ],
)
def test_prints_pr_number(tmp_path: Path, directory: str, text: str, expected: str) -> None:
    """The PR comes from the run path first, then a ``PR:`` line, then a ``## Code Review:`` heading.

    Resolve selects the newest review report automatically; a wrong number would check out and fix the wrong PR, so only
    the three named sources count and a number elsewhere in the text, such as a title, never does.
    """
    result = _extract(_write_report(tmp_path, directory, text))
    assert (result.returncode, result.stdout, result.stderr) == (0, f"{expected}\n", "")


@pytest.mark.integration
@pytest.mark.parametrize(
    "text",
    [
        pytest.param("Title: v2 parser\n", id="digit-only-in-title"),
        pytest.param("PR: n/a\n", id="pr-not-a-number"),
        pytest.param("PR: #0\n", id="zero-is-not-a-pr"),
    ],
)
def test_exits_one_without_a_pr(tmp_path: Path, text: str) -> None:
    """A report that names no PR yields no output and exit 1, so the caller can ask for a number.

    Guessing from stray digits would silently pick a PR; exiting empty keeps the resolve prompt in charge.
    """
    result = _extract(_write_report(tmp_path, "misc", text))
    assert (result.returncode, result.stdout) == (1, "")


@pytest.mark.integration
def test_exits_one_for_missing_report(tmp_path: Path) -> None:
    """An unreadable report path exits 1 with no output instead of raising a traceback."""
    result = _extract(tmp_path / "pr-x" / "review-report.md")
    assert (result.returncode, result.stdout) == (1, "")
