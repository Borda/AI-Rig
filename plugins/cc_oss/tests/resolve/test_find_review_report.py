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
    """Create a review report for ``pr`` under its ``run_id`` run directory, with an optional ``Gate:`` line."""
    report = root / ".reports/review" / f"pr-{pr}" / run_id / "review-report.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    body = f"---\nTitle: Review\nPR: #{pr}\n"
    if gate:
        body += f"Gate: {gate}\n"
    outcome = "N/A — rejected at gate" if gate.startswith("REJECT_") else "NEEDS_WORK"
    body += f"Outcome: {outcome}\nSummary: Reviewed the change.\n---\n"
    report.write_text(body, encoding="utf-8")
    if mtime is not None:
        os.utime(report, (mtime, mtime))
    return report


def _write_legacy_report(root: Path, run_id: str, pr: str, mtime: int | None = None) -> Path:
    """Create a pre-rename flat-layout report (no ``pr-<N>`` nesting), naming ``pr`` in its header."""
    report = root / ".reports/review" / run_id / "review-report.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(f"# Review\nPR: #{pr}\n", encoding="utf-8")
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
    """With two runs for the same PR, the highest run number wins."""
    _write_report(tmp_path, "run-001", "42")
    newest = _write_report(tmp_path, "run-002", "42")
    assert frr.newest_report_for_pr("42", tmp_path) == newest


def test_newest_report_for_pr_ignores_other_prs(tmp_path: Path) -> None:
    """A report for a different PR is not a match."""
    _write_report(tmp_path, "run-001", "7")
    assert frr.newest_report_for_pr("42", tmp_path) is None


def test_newest_report_for_pr_requires_exact_number(tmp_path: Path) -> None:
    """PR directory ``pr-420`` must not satisfy a lookup for PR 42."""
    _write_report(tmp_path, "run-001", "420")
    assert frr.newest_report_for_pr("42", tmp_path) is None


def test_newest_report_for_pr_orders_runs_numerically(tmp_path: Path) -> None:
    """``run-1000`` outranks ``run-999`` — a lexical sort would pick the wrong one.

    Regression guard: string-sorting run directory names ("run-1000" < "run-999") would make the
    reject gate read a stale run once a PR passes 999 review runs.
    """
    _write_report(tmp_path, "run-999", "42")
    newest = _write_report(tmp_path, "run-1000", "42")
    assert frr.newest_report_for_pr("42", tmp_path) == newest


def test_newest_report_for_pr_falls_back_to_legacy_flat_layout(tmp_path: Path) -> None:
    """A pre-rename flat-layout report is still found when no ``pr-<N>`` directory exists yet.

    Regression guard: the reject gate must not fail open on a report written before the
    pr-<N>/run-<NNN> rename just because its directory shape predates it.
    """
    legacy = _write_legacy_report(tmp_path, "2026-08-04T10-00-00Z", "42")
    assert frr.newest_report_for_pr("42", tmp_path) == legacy


def test_newest_report_for_pr_prefers_pr_scoped_over_legacy(tmp_path: Path) -> None:
    """A pr-<N>/run-<NNN> report takes priority over any legacy flat-layout report for the same PR."""
    _write_legacy_report(tmp_path, "2026-08-04T10-00-00Z", "42")
    scoped = _write_report(tmp_path, "run-001", "42")
    assert frr.newest_report_for_pr("42", tmp_path) == scoped


def test_gate_line_returns_empty_without_field(tmp_path: Path) -> None:
    """A pre-gate report with no ``Gate:`` field yields an empty line."""
    report = _write_report(tmp_path, "run-001", "42")
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


@pytest.mark.parametrize(
    "pr",
    [
        pytest.param("1/../../etc", id="path-traversal"),
        pytest.param("abc", id="non-numeric"),
        pytest.param("-x", id="flag-like"),
    ],
)
def test_main_rejects_non_numeric_pr(pr: str, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """A ``--pr`` value that is not bare digits is rejected before any report lookup or ``gh`` call.

    Regression guard: an unvalidated ``--pr`` reaches ``newest_report_for_pr`` (path-traversal into
    ``.reports/review/pr-<N>``) and ``current_head_sha`` (raw ``gh pr view <pr_number>`` argv). The
    ``--path-out`` sentinel must also come back empty, not stale, so a caller reading it never mistakes
    a rejected value for a previously resolved PR's report.
    """
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("/stale/path\n", encoding="utf-8")
    assert frr.main([f"--pr={pr}", "--path-out", str(sentinel)]) == 1
    assert "must be digits only" in capsys.readouterr().err
    assert sentinel.read_text(encoding="utf-8") == ""


def test_main_allows_when_no_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A PR with no review report imposes no restriction."""
    monkeypatch.chdir(tmp_path)
    assert frr.main(["--pr", "42"]) == 0


@pytest.mark.parametrize("gate", ["", "PENDING", "PASSING"])
def test_incomplete_latest_report_cannot_clear_prior_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, gate: str
) -> None:
    """Stop intake before an unfinished publication can replace the standing decision."""
    monkeypatch.chdir(tmp_path)
    _write_report(tmp_path, "run-001", "42", gate="REJECT_SCOPE @a1b2c3d")
    _write_report(tmp_path, "run-002", "42", gate=gate)
    selected = tmp_path / "selected.txt"
    selected.write_text("stale report path", encoding="utf-8")

    assert frr.main(["--pr", "42", "--path-out", str(selected)]) == 1
    assert "incomplete-review-report" in capsys.readouterr().out
    assert selected.read_text(encoding="utf-8") == ""


@pytest.mark.parametrize("gate", ["PASS", "BLOCK"])
def test_main_allows_non_reject_gates(gate: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``PASS`` and ``BLOCK`` are ordinary findings, not premise problems."""
    monkeypatch.chdir(tmp_path)
    _write_report(tmp_path, "run-001", "42", gate=gate)
    assert frr.main(["--pr", "42"]) == 0


def test_main_blocks_when_head_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A rejection still standing at the current head blocks the run."""
    monkeypatch.chdir(tmp_path)
    _write_report(tmp_path, "run-001", "42", gate="REJECT_SCOPE @a1b2c3d")
    _fake_head_sha(monkeypatch, "a1b2c3d")
    assert frr.main(["--pr", "42"]) == 1
    assert "⛔ BLOCKED" in capsys.readouterr().out


def test_main_blocks_when_short_recorded_sha_matches_full_current_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """An abbreviated recorded SHA that prefixes the full current SHA is treated as unchanged, not moved.

    ``_SHA_RE`` accepts 7-40 char SHAs, so a ``Gate:`` line commonly records a short SHA while
    ``current_head_sha`` always returns the full one from ``gh pr view --json headRefOid``. A
    string-equality comparison (``recorded != current``) would always see these as different, failing
    the reject gate open on the exact case that matters most: the head genuinely has not moved.
    Regression guard for the fix to ``main()``'s SHA comparison — the correct check is a prefix match.
    """
    monkeypatch.chdir(tmp_path)
    _write_report(tmp_path, "run-001", "42", gate="REJECT_SCOPE @a1b2c3d")
    _fake_head_sha(monkeypatch, "a1b2c3d4e5f6789012345678901234567890abcd")
    assert frr.main(["--pr", "42"]) == 1
    assert "⛔ BLOCKED" in capsys.readouterr().out


def test_main_warns_when_head_moved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A rejection recorded against an older head lets the run continue with a warning."""
    monkeypatch.chdir(tmp_path)
    _write_report(tmp_path, "run-001", "42", gate="REJECT_GOAL @a1b2c3d")
    _fake_head_sha(monkeypatch, "9999999")
    assert frr.main(["--pr", "42"]) == 0
    out = capsys.readouterr().out
    assert "head moved a1b2c3d→9999999" in out


def test_main_blocks_when_head_unverifiable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """An unreachable ``gh`` fails closed — the rejection stands."""
    monkeypatch.chdir(tmp_path)
    _write_report(tmp_path, "run-001", "42", gate="REJECT_SPAM @a1b2c3d")
    _fake_head_sha(monkeypatch, "", returncode=1)
    assert frr.main(["--pr", "42"]) == 1
    assert "unverifiable" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# --path-out: publish the resolved path so callers reuse this PR-scoped lookup
# ---------------------------------------------------------------------------


def test_path_out_publishes_resolved_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The resolved report path is written verbatim for the caller to reuse."""
    monkeypatch.chdir(tmp_path)
    report = _write_report(tmp_path, "run-001", "42", gate="PASS")
    sentinel = tmp_path / "sentinel"
    assert frr.main(["--pr", "42", "--path-out", str(sentinel)]) == 0
    assert sentinel.read_text(encoding="utf-8").strip() == report.as_posix()


def test_path_out_empty_when_no_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A PR with no report yields an empty sentinel, never a stale path."""
    monkeypatch.chdir(tmp_path)
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("/stale/path\n", encoding="utf-8")
    assert frr.main(["--pr", "42", "--path-out", str(sentinel)]) == 0
    assert sentinel.read_text(encoding="utf-8") == ""


@pytest.mark.parametrize("pr", ["", "n/a"])
def test_path_out_empty_without_pr_number(pr: str, tmp_path: Path) -> None:
    """The no-PR early return still clears the sentinel."""
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("/stale/path\n", encoding="utf-8")
    assert frr.main(["--pr", pr, "--path-out", str(sentinel)]) == 0
    assert sentinel.read_text(encoding="utf-8") == ""


def test_path_out_written_before_reject_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A blocking rejection still publishes the path — the caller needs it to show the user."""
    monkeypatch.chdir(tmp_path)
    report = _write_report(tmp_path, "run-001", "42", gate="REJECT_SCOPE @a1b2c3d")
    _fake_head_sha(monkeypatch, "a1b2c3d")
    sentinel = tmp_path / "sentinel"
    assert frr.main(["--pr", "42", "--path-out", str(sentinel)]) == 1
    assert sentinel.read_text(encoding="utf-8").strip() == report.as_posix()
    capsys.readouterr()


def test_path_out_unwritable_blocks_stale_consumption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Failed publication must stop a consumer from trusting an earlier run's sentinel."""
    monkeypatch.chdir(tmp_path)
    _write_report(tmp_path, "run-001", "42", gate="PASS")
    unwritable = tmp_path / "missing-dir" / "sentinel"
    assert frr.main(["--pr", "42", "--path-out", str(unwritable)]) == 1
    captured = capsys.readouterr()
    assert "no restriction" not in captured.out
    assert "could not write --path-out" in captured.err


def test_explicit_incomplete_report_blocks(tmp_path: Path) -> None:
    """Report-only intake must not bypass the same publication check as PR intake."""
    report = _write_report(tmp_path, "run-001", "42")
    assert frr.main(["--report", str(report)]) == 1


def test_newest_unpublished_run_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An allocated but unfinished newest run cannot resurrect an older completed decision."""
    monkeypatch.chdir(tmp_path)
    _write_report(tmp_path, "run-001", "42", gate="PASS")
    (tmp_path / ".reports/review/pr-42/run-002").mkdir()
    assert frr.main(["--pr", "42"]) == 1
