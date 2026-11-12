"""Tests for ``bin/find_review_report.py``.

Report discovery and gate parsing run against real files under ``tmp_path``; the head-SHA lookup replaces
``subprocess.run`` with an autospecced fake so no real ``gh`` invocation occurs.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

import find_review_report as frr
import pytest


def _can_symlink() -> bool:
    """Return whether this host permits creating symbolic links."""
    with tempfile.TemporaryDirectory() as directory:
        try:
            Path(directory, "link").symlink_to(Path(directory, "target"))
        except (OSError, NotImplementedError):
            return False
    return True


@pytest.mark.skipif(not _can_symlink(), reason="host cannot create symlinks")
def test_path_out_replaces_symlink_without_overwriting_target(tmp_path: Path) -> None:
    """Publishing a report path replaces a planted sentinel link and leaves its target intact."""
    victim = tmp_path / "victim"
    victim.write_text("precious", encoding="utf-8")
    sentinel = tmp_path / "sentinel"
    sentinel.symlink_to(victim)
    report = tmp_path / "report.md"
    assert frr._write_path_out(str(sentinel), report)
    assert not sentinel.is_symlink()
    assert sentinel.read_text(encoding="utf-8") == report.as_posix() + "\n"
    assert victim.read_text(encoding="utf-8") == "precious"


def test_legacy_lookup_ignores_body_pr_mentions(tmp_path: Path) -> None:
    """A newer report quoting another PR cannot hide that PR's standing rejection."""
    older = tmp_path / ".reports/review/older/review-report.md"
    older.parent.mkdir(parents=True)
    older.write_text(
        "---\nTitle: Review\nPR: #42\nGate: REJECT_SCOPE @a1b2c3d\n"
        "Outcome: N/A — rejected at gate\nSummary: Out of scope.\n---\n",
        encoding="utf-8",
    )
    os.utime(older, (1, 1))
    newer = tmp_path / ".reports/review/newer/review-report.md"
    newer.parent.mkdir()
    newer.write_text(
        "---\nTitle: Review\nPR: #7\nGate: PASS\nOutcome: APPROVE\nSummary: Ready.\n---\n```\nPR: #42\n```\n",
        encoding="utf-8",
    )
    os.utime(newer, (2, 2))
    assert frr.newest_report_for_pr("42", tmp_path) == older
    assert frr.gate_line(older) == "Gate: REJECT_SCOPE @a1b2c3d"


def _write_report(
    root: Path, run_id: str, pr: str, gate: str = "", mtime: int | None = None, outcome: str | None = None
) -> Path:
    """Create a review report for ``pr`` under its ``run_id`` run directory, with an optional ``Gate:`` line."""
    report = root / ".reports/review" / f"pr-{pr}" / run_id / "review-report.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    body = f"---\nTitle: Review\nPR: #{pr}\n"
    if gate:
        body += f"Gate: {gate}\n"
    if outcome is None:
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
    report.write_text(f"---\nTitle: Review\nPR: #{pr}\n---\n", encoding="utf-8")
    if mtime is not None:
        os.utime(report, (mtime, mtime))
    return report


def _fake_head_sha(monkeypatch: pytest.MonkeyPatch, sha: str, returncode: int = 0) -> None:
    """Answer the ``gh pr view`` head-SHA lookup with a canned value."""
    fake_run = mock.create_autospec(
        subprocess.run, return_value=subprocess.CompletedProcess([], returncode, stdout=sha)
    )
    monkeypatch.setattr(frr.subprocess, "run", fake_run)


def test_finding_row_problem_refuses_list_merged_into() -> None:
    """The producer's shared row gate refuses a merged target the consumer cannot use as a lookup key.

    ``merged_into`` must name the surviving row's candidate id as text; a list cannot be looked up, so the row is
    reported as a problem instead of silently dropping the merge.
    """
    finding = {
        "id": "architecture-quality-1",
        "section": "### Architecture & Quality",
        "severity": "high",
        "title": "Capacity leak",
        "change": "code",
        "author": "reviewer",
        "merged_into": ["cand-survivor"],
    }

    assert frr.finding_row_problem(finding, 1) == (
        "finding 1 field merged_into is list, not text — it must name the surviving row's candidate_id"
    )


class TestNewestReportForPr:
    """``newest_report_for_pr`` locates the report of the latest review run for one PR."""

    @pytest.mark.parametrize(
        ("older", "newer"),
        [
            pytest.param("run-001", "run-002", id="highest-run-number-wins"),
            pytest.param("run-999", "run-1000", id="runs-ordered-numerically-not-lexically"),
        ],
    )
    def test_picks_most_recent(self, tmp_path: Path, older: str, newer: str) -> None:
        """With two runs for the same PR, the highest run number wins.

        ``run-1000`` outranks ``run-999`` — a lexical sort would pick the wrong one. Regression guard: string-sorting
        run directory names ("run-1000" < "run-999") would make the reject gate read a stale run once a PR passes 999
        review runs.
        """
        _write_report(tmp_path, older, "42")
        newest = _write_report(tmp_path, newer, "42")

        assert frr.newest_report_for_pr("42", tmp_path) == newest

    @pytest.mark.parametrize(
        "other_pr",
        [
            pytest.param("7", id="different-pr-is-not-a-match"),
            pytest.param("420", id="pr-420-does-not-satisfy-lookup-for-42"),
        ],
    )
    def test_ignores_other_prs(self, tmp_path: Path, other_pr: str) -> None:
        """A report for a different PR is not a match, and ``pr-420`` must not satisfy a lookup for PR 42.

        Matching by directory-name prefix would hand PR 42's review gate the report of PR 420.
        """
        _write_report(tmp_path, "run-001", other_pr)

        assert frr.newest_report_for_pr("42", tmp_path) is None

    def test_falls_back_to_legacy_flat_layout(self, tmp_path: Path) -> None:
        """A pre-rename flat-layout report is still found when no ``pr-<N>`` directory exists yet.

        Regression guard: the reject gate must not fail open on a report written before the pr-<N>/run-<NNN> rename
        just because its directory shape predates it.
        """
        legacy = _write_legacy_report(tmp_path, "2026-08-04T10-00-00Z", "42")

        assert frr.newest_report_for_pr("42", tmp_path) == legacy

    def test_prefers_pr_scoped_over_legacy(self, tmp_path: Path) -> None:
        """A pr-<N>/run-<NNN> report takes priority over any legacy flat-layout report for the same PR.

        Both layouts exist after an upgrade mid-review; the new layout holds the current decision.
        """
        _write_legacy_report(tmp_path, "2026-08-04T10-00-00Z", "42")
        scoped = _write_report(tmp_path, "run-001", "42")

        assert frr.newest_report_for_pr("42", tmp_path) == scoped


class TestGateLine:
    """``gate_line`` reads a report's ``Gate:`` field, and only for a report that carries a published verdict."""

    @pytest.mark.parametrize(
        ("gate", "outcome", "expected"),
        [
            pytest.param("", "NEEDS_WORK", "", id="pre-gate-report-without-gate-field"),
            pytest.param("PASS", "⚠ REQUEST_CHANGES", "Gate: PASS", id="pass-gate-warning-symbol"),
            pytest.param("PASS", "✓ APPROVE", "Gate: PASS", id="pass-gate-check-symbol"),
            pytest.param(
                "REJECT_SCOPE @a1b2c3d",
                "✗ N/A — rejected at gate",
                "Gate: REJECT_SCOPE @a1b2c3d",
                id="reject-gate-cross-symbol",
            ),
            pytest.param(
                "PASS",
                "✓ APPROVE — minor changes suggested; 0 critical, 0 high, 2 medium, 11 low",
                "Gate: PASS",
                id="pass-gate-trailing-detail",
            ),
            pytest.param("PASS", "⚠ Needs work; 2 medium", "Gate: PASS", id="pass-gate-mixed-case-space"),
            pytest.param("BLOCK", "request-changes", "Gate: BLOCK", id="block-gate-lowercase-hyphen"),
            pytest.param("PASS", "[review outcome]", "", id="placeholder-outcome-incomplete"),
            pytest.param("PASS", "APPROVED", "", id="glued-suffix-outcome-incomplete"),
            pytest.param("PASS", "✓ LGTM", "", id="free-form-verdict-incomplete"),
            pytest.param("PASS", "NEEDS_WORK_LATER", "", id="token-with-glued-suffix-incomplete"),
            pytest.param("PASS", "pending approval", "", id="token-buried-mid-sentence-incomplete"),
        ],
    )
    def test_requires_verdict_led_outcome(self, tmp_path: Path, gate: str, outcome: str, expected: str) -> None:
        """A report counts as complete only when ``Outcome`` is led by a canonical verdict token.

        A verdict symbol, spelling variant, or trailing detail on ``Outcome`` does not make the report look incomplete:
        ``oss:review`` writes ``Outcome: ⚠ REQUEST_CHANGES`` per the shared verdict-symbol convention, and a
        consolidator may vary case/word separator or append severity counts after the token; a parser that only matches
        the bare word would wrongly block a complete, real report as ``incomplete-review-report``. Tolerating case,
        separators, and trailing detail must not also admit an unfilled template placeholder, a glued suffix, a free-
        form verdict, or a token buried mid-sentence — each means no decision was published and the gate line stays
        empty. A pre-gate report with no ``Gate:`` field also yields an empty line.
        """
        report = _write_report(tmp_path, "run-001", "42", gate=gate, outcome=outcome)

        assert frr.gate_line(report) == expected


class TestRejectSha:
    """``reject_sha`` lifts the head SHA a rejection was recorded against out of a gate line."""

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
    def test_extracts_recorded_sha(self, line: str, expected: str) -> None:
        """The recorded SHA is lifted out of the gate line when present, and empty when the line carries none.

        Both the 7-character abbreviation and the full 40-character SHA appear in real reports.
        """
        assert frr.reject_sha(line) == expected


class TestMain:
    """The CLI gates a PR's intake on its newest review report and publishes the resolved report path."""

    @pytest.mark.parametrize("pr", ["", "n/a"])
    def test_skips_without_pr_number(self, pr: str, capsys: pytest.CaptureFixture) -> None:
        """No PR number means no gate to enforce.

        Runs without a PR (local branch review) pass straight through and say that the check was skipped.
        """
        code = frr.main(["--pr", pr])

        assert code == 0
        assert "skipped" in capsys.readouterr().out

    @pytest.mark.parametrize(
        "pr",
        [
            pytest.param("1/../../etc", id="path-traversal"),
            pytest.param("abc", id="non-numeric"),
            pytest.param("-x", id="flag-like"),
        ],
    )
    def test_rejects_non_numeric_pr(self, pr: str, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """A ``--pr`` value that is not bare digits is rejected before any report lookup or ``gh`` call.

        Regression guard: an unvalidated ``--pr`` reaches ``newest_report_for_pr`` (path-traversal into
        ``.reports/review/pr-<N>``) and ``current_head_sha`` (raw ``gh pr view <pr_number>`` argv). The ``--path-out``
        sentinel must also come back empty, not stale, so a caller reading it never mistakes a rejected value for a
        previously resolved PR's report.
        """
        sentinel = tmp_path / "sentinel"
        sentinel.write_text("/stale/path\n", encoding="utf-8")

        code = frr.main([f"--pr={pr}", "--path-out", str(sentinel)])

        assert code == 1
        assert "must be digits only" in capsys.readouterr().err
        assert sentinel.read_text(encoding="utf-8") == ""

    def test_allows_when_no_report(self, workdir: Path) -> None:
        """A PR with no review report imposes no restriction.

        A first-time review has nothing to be rejected by, so intake continues.
        """
        assert frr.main(["--pr", "42"]) == 0

    @pytest.mark.parametrize("gate", ["", "PENDING", "PASSING"])
    def test_incomplete_latest_report_cannot_clear_prior_rejection(
        self, workdir: Path, capsys: pytest.CaptureFixture, gate: str
    ) -> None:
        """Stop intake before an unfinished publication can replace the standing decision.

        Run 1 rejected the PR; run 2 has no valid gate (missing, unknown or look-alike token). Reading run 2 as "no
        restriction" would let a half-published review undo the earlier rejection, so intake blocks and clears the path
        sentinel.
        """
        _write_report(workdir, "run-001", "42", gate="REJECT_SCOPE @a1b2c3d")
        _write_report(workdir, "run-002", "42", gate=gate)
        selected = workdir / "selected.txt"
        selected.write_text("stale report path", encoding="utf-8")

        code = frr.main(["--pr", "42", "--path-out", str(selected)])

        assert code == 1
        assert "incomplete-review-report" in capsys.readouterr().out
        assert selected.read_text(encoding="utf-8") == ""

    @pytest.mark.parametrize("gate", ["PASS", "BLOCK"])
    def test_allows_non_reject_gates(self, gate: str, workdir: Path) -> None:
        """``PASS`` and ``BLOCK`` are ordinary findings, not premise problems.

        Only a ``REJECT_*`` gate stops intake; a blocking review still lets the contributor's follow-up proceed.
        """
        _write_report(workdir, "run-001", "42", gate=gate)

        assert frr.main(["--pr", "42"]) == 0

    @pytest.mark.parametrize(
        ("gate", "current_sha", "gh_returncode", "expected_rc", "expected_output"),
        [
            pytest.param(
                "REJECT_SCOPE @a1b2c3d", "a1b2c3d", 0, 1, "⛔ BLOCKED", id="rejection-standing-at-current-head-blocks"
            ),
            pytest.param(
                "REJECT_SCOPE @a1b2c3d",
                "a1b2c3d4e5f6789012345678901234567890abcd",
                0,
                1,
                "⛔ BLOCKED",
                id="short-recorded-sha-prefixing-full-current-sha-is-unchanged",
            ),
            pytest.param(
                "REJECT_GOAL @a1b2c3d",
                "9999999",
                0,
                0,
                "head moved a1b2c3d→9999999",
                id="head-moved-warns-and-continues",
            ),
            pytest.param("REJECT_SPAM @a1b2c3d", "", 1, 1, "unverifiable", id="unreachable-gh-fails-closed"),
        ],
    )
    def test_compares_rejection_head_with_current_head(
        self,
        workdir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
        gate: str,
        current_sha: str,
        gh_returncode: int,
        expected_rc: int,
        expected_output: str,
    ) -> None:
        """A rejection still standing at the current head blocks the run; one recorded against an older head only warns.

        An abbreviated recorded SHA that prefixes the full current SHA is treated as unchanged, not moved. ``_SHA_RE``
        accepts 7-40 char SHAs, so a ``Gate:`` line commonly records a short SHA while ``current_head_sha`` always
        returns the full one from ``gh pr view --json headRefOid``. A string-equality comparison (``recorded !=
        current``) would always see these as different, failing the reject gate open on the exact case that matters
        most: the head genuinely has not moved. Regression guard for the fix to ``main()``'s SHA comparison — the
        correct check is a prefix match. An unreachable ``gh`` fails closed — the rejection stands.
        """
        _write_report(workdir, "run-001", "42", gate=gate)
        _fake_head_sha(monkeypatch, current_sha, returncode=gh_returncode)

        code = frr.main(["--pr", "42"])

        assert code == expected_rc
        assert expected_output in capsys.readouterr().out

    @pytest.mark.parametrize(
        ("gate", "expected_rc"),
        [
            pytest.param("PASS", 0, id="passing-report-path-published"),
            pytest.param("REJECT_SCOPE @a1b2c3d", 1, id="blocking-rejection-still-publishes-path"),
        ],
    )
    def test_path_out_publishes_resolved_report(
        self, workdir: Path, monkeypatch: pytest.MonkeyPatch, gate: str, expected_rc: int
    ) -> None:
        """The resolved report path is written verbatim for the caller to reuse.

        A blocking rejection still publishes the path — the caller needs it to show the user.
        """
        report = _write_report(workdir, "run-001", "42", gate=gate)
        _fake_head_sha(monkeypatch, "a1b2c3d")
        sentinel = workdir / "sentinel"

        code = frr.main(["--pr", "42", "--path-out", str(sentinel)])

        assert code == expected_rc
        assert sentinel.read_text(encoding="utf-8").strip() == report.as_posix()

    @pytest.mark.parametrize(
        "pr",
        [
            pytest.param("", id="empty-pr-number"),
            pytest.param("n/a", id="not-applicable-pr-number"),
            pytest.param("42", id="pr-without-report"),
        ],
    )
    def test_path_out_empty_without_pr_number_or_report(self, pr: str, workdir: Path) -> None:
        """The no-PR early return, or a PR with no report, still clears the sentinel — never a stale path.

        A caller that reads the sentinel afterwards must see "no report", not the path of an earlier run.
        """
        sentinel = workdir / "sentinel"
        sentinel.write_text("/stale/path\n", encoding="utf-8")

        code = frr.main(["--pr", pr, "--path-out", str(sentinel)])

        assert code == 0
        assert sentinel.read_text(encoding="utf-8") == ""

    def test_unwritable_path_out_blocks_stale_consumption(self, workdir: Path, capsys: pytest.CaptureFixture) -> None:
        """Failed publication must stop a consumer from trusting an earlier run's sentinel.

        The sentinel sits in a directory that does not exist, so the write fails; the run must report that and not claim
        "no restriction".
        """
        _write_report(workdir, "run-001", "42", gate="PASS")
        unwritable = workdir / "missing-dir" / "sentinel"

        code = frr.main(["--pr", "42", "--path-out", str(unwritable)])

        captured = capsys.readouterr()
        assert code == 1
        assert "no restriction" not in captured.out
        assert "could not write --path-out" in captured.err

    def test_explicit_incomplete_report_blocks(self, tmp_path: Path) -> None:
        """Report-only intake must not bypass the same publication check as PR intake.

        ``--report`` points straight at a report whose gate was never published; it is refused like the PR route.
        """
        report = _write_report(tmp_path, "run-001", "42")

        assert frr.main(["--report", str(report)]) == 1

    def test_newest_unpublished_run_blocks(self, workdir: Path) -> None:
        """An allocated but unfinished newest run cannot resurrect an older completed decision.

        Run 2 exists as an empty directory (allocated, report not yet written) next to a published run 1; intake must
        stop rather than fall back to run 1.
        """
        _write_report(workdir, "run-001", "42", gate="PASS")
        (workdir / ".reports/review/pr-42/run-002").mkdir()

        assert frr.main(["--pr", "42"]) == 1
