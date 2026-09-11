"""Tests for ``bin/find_review_report.py``.

Report discovery and gate parsing run against real files under ``tmp_path``; the head-SHA lookup monkeypatches
``subprocess.run`` so no real ``gh`` invocation occurs.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import find_review_report as frr


class _FakeCompleted:
    """Minimal stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        """Store the status and output consumed by the gate check."""
        self.returncode = returncode
        self.stdout = stdout


def _write_report(root: Path, run_id: str, pr: str, gate: str = "", mtime: int | None = None) -> Path:
    """Create a review report naming ``pr`` with an optional ``Gate:`` line."""
    report = root / ".reports/review" / run_id / "review-report.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    body = f"# Review\nPR: #{pr}\n"
    if gate:
        body += f"Gate: {gate}\n"
    report.write_text(body, encoding="utf-8")
    if mtime is not None:
        os.utime(report, (mtime, mtime))
    return report


def _fake_head_sha(monkeypatch: pytest.MonkeyPatch, sha: str, returncode: int = 0) -> None:
    """Answer the ``gh pr view`` head-SHA lookup with a canned value."""
    monkeypatch.setattr(
        frr.subprocess,
        "run",
        lambda *_a, **_k: _FakeCompleted(returncode=returncode, stdout=sha),
    )


# ---------------------------------------------------------------------------
# report discovery
# ---------------------------------------------------------------------------


def test_newest_report_for_pr_picks_most_recent(tmp_path: Path) -> None:
    """With two reports for the same PR, the newest by mtime wins."""
    _write_report(tmp_path, "old", "42", mtime=1_000_000)
    newest = _write_report(tmp_path, "new", "42", mtime=2_000_000)
    assert frr.newest_report_for_pr("42", tmp_path) == newest


def test_newest_report_for_pr_ignores_other_prs(tmp_path: Path) -> None:
    """A report for a different PR is not a match."""
    _write_report(tmp_path, "run1", "7")
    assert frr.newest_report_for_pr("42", tmp_path) is None


def test_newest_report_for_pr_requires_exact_number(tmp_path: Path) -> None:
    """``PR: #420`` must not satisfy a lookup for PR 42."""
    _write_report(tmp_path, "run1", "420")
    assert frr.newest_report_for_pr("42", tmp_path) is None


def test_gate_line_returns_empty_without_field(tmp_path: Path) -> None:
    """A pre-gate report with no ``Gate:`` field yields an empty line."""
    report = _write_report(tmp_path, "run1", "42")
    assert frr.gate_line(report) == ""


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        pytest.param("Gate: REJECT_SCOPE @a1b2c3d", "a1b2c3d", id="short-sha"),
        pytest.param(
            "Gate: REJECT_GOAL @0123456789abcdef0123456789abcdef01234567",
            "0123456789abcdef" * 2 + "01234567",
            id="full-sha",
        ),
        pytest.param("Gate: PASS", "", id="no-sha"),
    ],
)
def test_reject_sha(line: str, expected: str) -> None:
    """The recorded SHA is lifted out of the gate line when present."""
    assert frr.reject_sha(line) == expected


# ---------------------------------------------------------------------------
# gate enforcement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pr", ["", "n/a"])
def test_main_skips_without_pr_number(pr: str, capsys: pytest.CaptureFixture) -> None:
    """No PR number means no gate to enforce."""
    assert frr.main(["--pr", pr]) == 0
    assert "skipped" in capsys.readouterr().out


def test_main_allows_when_no_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A PR with no review report imposes no restriction."""
    monkeypatch.chdir(tmp_path)
    assert frr.main(["--pr", "42"]) == 0


@pytest.mark.parametrize("gate", ["PASS", "BLOCK"])
def test_main_allows_non_reject_gates(gate: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``PASS`` and ``BLOCK`` are ordinary findings, not premise problems."""
    monkeypatch.chdir(tmp_path)
    _write_report(tmp_path, "run1", "42", gate=gate)
    assert frr.main(["--pr", "42"]) == 0


def test_main_blocks_when_head_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A rejection still standing at the current head blocks the run."""
    monkeypatch.chdir(tmp_path)
    _write_report(tmp_path, "run1", "42", gate="REJECT_SCOPE @a1b2c3d")
    _fake_head_sha(monkeypatch, "a1b2c3d")
    assert frr.main(["--pr", "42"]) == 1
    assert "⛔ BLOCKED" in capsys.readouterr().out


def test_main_warns_when_head_moved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A rejection recorded against an older head lets the run continue with a warning."""
    monkeypatch.chdir(tmp_path)
    _write_report(tmp_path, "run1", "42", gate="REJECT_GOAL @a1b2c3d")
    _fake_head_sha(monkeypatch, "9999999")
    assert frr.main(["--pr", "42"]) == 0
    out = capsys.readouterr().out
    assert "head moved a1b2c3d→9999999" in out


def test_main_blocks_when_head_unverifiable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """An unreachable ``gh`` fails closed — the rejection stands."""
    monkeypatch.chdir(tmp_path)
    _write_report(tmp_path, "run1", "42", gate="REJECT_SPAM @a1b2c3d")
    _fake_head_sha(monkeypatch, "", returncode=1)
    assert frr.main(["--pr", "42"]) == 1
    assert "unverifiable" in capsys.readouterr().out
