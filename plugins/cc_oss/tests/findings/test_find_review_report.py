"""Tests for ``bin/find_review_report.py``.

Report discovery and gate parsing run against real files under ``tmp_path``; the head-SHA lookup replaces
``subprocess.run`` with an autospecced fake so no real ``gh`` invocation occurs.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

import find_review_report as frr
import pytest
from _text_io import write_lf
from _verdict_publication import Publication, candidate, judged, missing

_HEAD = "abcdef1234567"
_SOFTWARE_LINEUP = "sw-engineer | spawned | roles: Software engineer\n"
_DROPPED_DOCS = "- Unreviewed dimension: docs+lint — dropped by FANOUT_MAX. Rerun with `/oss:review 7 --full`."


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
        outcome = "N/A — rejected at gate" if gate.startswith("REJECT_") else "MAJOR_REVISION"
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
            pytest.param("", "MAJOR_REVISION", "", id="pre-gate-report-without-gate-field"),
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
            pytest.param("PASS", "MAJOR_REVISION_LATER", "", id="token-with-glued-suffix-incomplete"),
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


class TestMajorRevisionLevel:
    """Level 3 of the source scale is ``MAJOR_REVISION``; the archived ``NEEDS_WORK`` name is still read."""

    @pytest.mark.parametrize("outcome", ["MAJOR_REVISION", "NEEDS_WORK"], ids=["current", "archived"])
    def test_both_names_rate_three(self, outcome: str) -> None:
        """A sidecar carrying either name with rating 3 is a valid source judgment."""
        record = {"source_outcome": outcome, "source_rating": 3, "readiness": "complete"}
        assert frr._source_assessment(record) is True
        assert frr.source_rating(outcome) == 3

    @pytest.mark.parametrize("outcome", ["MAJOR_REVISION", "NEEDS_WORK"], ids=["current", "archived"])
    def test_a_wrong_rating_is_still_refused(self, outcome: str) -> None:
        """The legacy alias never loosens the rating check."""
        record = {"source_outcome": outcome, "source_rating": 2, "readiness": "complete"}
        assert frr._source_assessment(record) is False

    @pytest.mark.parametrize("outcome", ["MAJOR_REVISION", "MAJOR REVISION", "NEEDS_WORK", "NEEDS-WORK"])
    def test_a_legacy_header_accepts_either_name(self, tmp_path: Path, outcome: str) -> None:
        """Header intake matches the new level-3 name beside the archived one, with separator tolerance."""
        report = tmp_path / "review-report.md"
        report.write_text(
            f"---\nTitle: t\nPR: #1\nGate: PASS\nOutcome: {outcome}\nSummary: s\n---\n\n## Review\n", encoding="utf-8"
        )
        assert frr.gate_line(report) == "Gate: PASS"


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


def _fill_gaps_plan(directory: Path, carried: list[str]) -> None:
    """Write the fill-gaps plan copy the run allocation step places beside a fill-gaps publication."""
    plan = {
        "head_sha": _HEAD,
        "carried": {"assessments": [], "candidates": carried, "open_concerns": [], "findings": []},
    }
    write_lf(directory / "fill-gaps.json", json.dumps(plan))


def _write_producer(directory: Path, publication: Publication) -> Path:
    """Write the draft and the frozen reviewed identity required by the producer."""
    report = publication.write(directory)
    write_lf(report.with_name("head-sha.txt"), _HEAD + "\n")
    return report


@pytest.fixture
def review_run(tmp_path: Path) -> Path:
    """Allocate a numbered report path for tests comparing generated rerun commands."""
    directory = tmp_path / ".reports/review/pr-7/run-001"
    directory.mkdir(parents=True)
    return directory


def _keyless(record: dict) -> dict:
    """Return a candidate record without its ``missing_capability_kind`` key, as saved before the key existed."""
    return {key: value for key, value in record.items() if key != "missing_capability_kind"}


class TestProducerCoverageEvidence:
    """The publication gate refuses when the evidence it checks is absent, outside the fill-gaps exemption."""

    @pytest.mark.parametrize("name", ["lineup.md", "fill-gaps.json", "head-sha.txt"])
    def test_missing_parent_evidence_refusal_names_its_owner(
        self, tmp_path: Path, capsys: pytest.CaptureFixture, name: str
    ) -> None:
        """An absent launch artifact produces a labelled refusal that reaches the parent."""
        report = _write_producer(tmp_path, self._blocked([candidate("V1", "UNVERIFIED")], lineup=None))
        report.with_name(name).unlink(missing_ok=True)
        self._assert_parent_refusal(report, capsys, name)

    @pytest.mark.parametrize(
        ("name", "payload"),
        [
            pytest.param("lineup.md", b"sw-engineer | spawned-extra", id="malformed-lineup"),
            pytest.param("lineup.md", b"\xff\xfe", id="non-utf8-lineup"),
            pytest.param("lineup.md", b"docs+lint | promoted-unslotted: docs changed", id="unrecorded-lineup-roles"),
            pytest.param(
                "lineup.md",
                b"docs+lint | promoted-unslotted: docs changed | roles: Software engineer",
                id="wrong-lineup-role",
            ),
            pytest.param("fill-gaps.json", b"{", id="malformed-plan"),
            pytest.param("fill-gaps.json", b'{"head_sha":"abcdef1234567"}', id="plan-omits-carried-records"),
            pytest.param("fill-gaps.json", b"\xff", id="non-utf8-plan"),
            pytest.param("head-sha.txt", b"\xff", id="non-utf8-reviewed-head"),
        ],
    )
    def test_damaged_parent_evidence_refusal_names_its_owner(
        self, tmp_path: Path, capsys: pytest.CaptureFixture, name: str, payload: bytes
    ) -> None:
        """An unreadable or malformed launch artifact produces a labelled refusal that reaches the parent."""
        report = _write_producer(tmp_path, self._blocked([candidate("V1", "UNVERIFIED")]))
        report.with_name(name).write_bytes(payload)
        self._assert_parent_refusal(report, capsys, name)

    @staticmethod
    def _assert_parent_refusal(report: Path, capsys: pytest.CaptureFixture, name: str) -> None:
        """Assert the CLI refusal contains the exact marker Step 5 uses for parent recovery."""
        args = ["--report", str(report), "--producer"] + (["--fill-gaps"] if name == "fill-gaps.json" else [])
        assert frr.main(args) == 1
        output = capsys.readouterr().out.splitlines()
        assert "incomplete-review-report" in output[0]
        assert f"[gate] parent evidence: {name}" in output

    @pytest.mark.parametrize("name", ["review-report.md", "review-validation.json", "findings.jsonl"])
    def test_corrupt_publication_is_labelled_as_report_failure(
        self, tmp_path: Path, capsys: pytest.CaptureFixture, name: str
    ) -> None:
        """Undecodable publication inputs are refused without assigning them to the launch evidence owner."""
        report = _write_producer(tmp_path, self._blocked([candidate("V1", "UNVERIFIED")]))
        path = report.with_name(name)
        path.write_bytes(path.read_bytes() + b"\xff\n")
        assert frr.main(["--report", str(report), "--producer"]) == 1
        output = capsys.readouterr().out
        assert "incomplete-review-report" in output
        assert "[gate] parent evidence:" not in output

    @pytest.mark.parametrize(
        ("unit", "roles", "additional_assessments", "expected_missing"),
        [
            pytest.param(
                "docs+lint",
                "Documentation reviewer, Linting expert",
                [missing("Documentation reviewer")],
                "Linting expert",
                id="merged-role-omitted",
            ),
            pytest.param(
                "perf+arch",
                "Performance engineer, Solution architect",
                [missing("Performance engineer"), judged("Solution architect", "MINOR_EDITS")],
                "Solution architect",
                id="merged-role-claimed-completed",
            ),
            pytest.param("doc-scribe", "Documentation reviewer", [], "Documentation reviewer", id="standalone-docs"),
            pytest.param("sw-engineer", "Software engineer", [], "Software engineer", id="standalone-code"),
            pytest.param(
                "doc-scribe",
                "Documentation reviewer",
                [missing("Documentation reviewer")] * 2,
                "Documentation reviewer",
                id="duplicate-missing-record",
            ),
            pytest.param(
                "perf+arch",
                "Solution architect",
                [missing("Solution architect")],
                "",
                id="only-preselected-member-required",
            ),
        ],
    )
    def test_unslotted_roles_are_missing_even_when_readiness_already_incomplete(
        self, review_run: Path, unit: str, roles: str, additional_assessments: list[dict], expected_missing: str
    ) -> None:
        """An unrelated reason for incomplete readiness cannot conceal any role the launch record lists."""
        assessments = [judged("QA specialist", "MINOR_EDITS"), missing("Challenger"), *additional_assessments]
        fill = f'/oss:review 7 --fill-gaps=.reports/review/pr-7/run-001/review-report.md --roles="{roles.replace(", ", ",")}"'
        publication = Publication(
            "REVIEW_V1_PASS", "MINOR_EDITS", "incomplete", assessments,
            lineup=f"{unit} | promoted-unslotted: scope trigger | roles: {roles}\n",
            body=f"### Review Confidence\n\n- Unreviewed dimension: {unit} — scope trigger. Rerun with `/oss:review 7 --full` or `{fill}`.",
        )  # fmt: skip
        report = _write_producer(review_run, publication)
        report.with_name("review-publication.published").touch()
        assert frr.gate_verdict(report)[0] == "Gate: BLOCK"
        line, reason = frr.publication_verdict(report, producer=True)
        assert line == ("" if expected_missing else "Gate: BLOCK"), reason
        assert reason == (
            f"report requires exactly one presence: missing assessment for {unit!r} roles: {expected_missing}"
            if expected_missing
            else ""
        )

    def _blocked(self, candidates: list[dict], **extra: object) -> Publication:
        """Publish a judged review whose candidates stay UNVERIFIED, readiness blocked."""
        extra.setdefault("lineup", _SOFTWARE_LINEUP)
        return Publication("REVIEW_V1_PASS", "MINOR_EDITS", "blocked", [judged("Software engineer", "MINOR_EDITS")],
                           candidates, confidence="0.80 — no CUDA host", **extra)  # type: ignore[arg-type]  # fmt: skip

    @pytest.mark.parametrize(
        ("lineup", "fill_gaps", "fragment"),
        [
            pytest.param(None, False, "lineup.md", id="fresh-run-without-lineup"),
            pytest.param("", False, "lineup.md", id="empty-lineup"),
            pytest.param("sw-engineer | maybe\n", False, "lineup.md", id="unknown-lineup-state"),
            pytest.param(None, True, "", id="fill-gaps-run-writes-no-lineup"),
            pytest.param(_SOFTWARE_LINEUP, False, "", id="lineup-present"),
        ],
    )
    def test_lineup_record_is_required_outside_fill_gaps(
        self, tmp_path: Path, lineup: str | None, fill_gaps: bool, fragment: str
    ) -> None:
        """A fresh ranked run's lineup record is the gate's coverage evidence; its absence is a refusal, not a pass.

        Only a fill-gaps run, named by the caller's ``--fill-gaps`` flag and backed by its plan, writes no lineup.
        """
        publication = self._blocked([candidate("V1", "UNVERIFIED")], lineup=lineup)
        report = _write_producer(tmp_path, publication)
        if fill_gaps:
            _fill_gaps_plan(tmp_path, [])
        line, reason = frr.publication_verdict(report, producer=True, fill_gaps=fill_gaps)
        assert (bool(line), fragment in reason) == (not fragment, True), reason

    @pytest.mark.parametrize(
        ("readiness", "allowed"),
        [
            pytest.param("incomplete", True, id="incomplete"),
            pytest.param("blocked", False, id="blocked"),
            pytest.param("complete", False, id="complete"),
        ],
    )
    def test_unslotted_promoted_unit_forces_incomplete_readiness(
        self, review_run: Path, readiness: str, allowed: bool
    ) -> None:
        """A promoted unit without a slot is a missing required reviewer, so only ``incomplete`` readiness may stand.

        The refused publications omit the unslotted unit's roles from the assessments, so resolve intake reads them;
        only the lineup record shows the missing reviewer.
        """
        reviewers = [judged("Software engineer", "MINOR_EDITS")]
        publication = Publication(
            "REVIEW_V1_PASS", "MINOR_EDITS", readiness,
            [*reviewers, missing("Documentation reviewer")] if readiness == "incomplete" else reviewers,
            [candidate("V1", "UNVERIFIED")] if readiness == "blocked" else [],
            confidence="0.70 — docs unit unslotted",
            lineup=_SOFTWARE_LINEUP + "docs+lint | promoted-unslotted: CHANGELOG.md changed | roles: Documentation reviewer\n",
            body="### Review Confidence\n\n- Unreviewed dimension: docs+lint — CHANGELOG.md changed. Rerun with "
            '`/oss:review 7 --full` or `/oss:review 7 --fill-gaps=.reports/review/pr-7/run-001/review-report.md '
            '--roles="Documentation reviewer"`.',
        )  # fmt: skip
        report = _write_producer(review_run, publication)
        report.with_name("review-publication.published").touch()
        intake, intake_reason = frr.gate_verdict(report)
        line, reason = frr.publication_verdict(report, producer=True)
        assert (bool(intake), bool(line)) == (True, allowed), (intake_reason, reason)

    @pytest.mark.parametrize(
        ("body", "allowed"),
        [
            pytest.param(f"### Review Confidence\n\n{_DROPPED_DOCS}", True, id="visible-generated-line"),
            pytest.param(
                "### Review Confidence\n\n- Unreviewed dimension: docs+lint", False, id="token-without-command"
            ),
            pytest.param(
                "### Review Confidence\n\n" + _DROPPED_DOCS.replace("docs+lint", "docs+lint-x"),
                False,
                id="longer-unit-name",
            ),
            pytest.param(
                "### Review Confidence\n\n" + _DROPPED_DOCS.replace("docs+lint", "docs+lint+typing"),
                False,
                id="unit-is-a-prefix",
            ),
        ],
    )
    def test_unreviewed_dimension_requires_its_generated_line(self, review_run: Path, body: str, allowed: bool) -> None:
        """A dropped unit needs its whole generated disclosure in Review Confidence, including the rerun command."""
        publication = self._blocked([candidate("V1", "UNVERIFIED")], lineup="docs+lint | dropped\n", body=body)
        line, reason = frr.publication_verdict(_write_producer(review_run, publication), producer=True)
        assert bool(line) == allowed, reason

    @pytest.mark.parametrize(
        ("fill_gaps", "carried", "allowed"),
        [
            pytest.param(False, [], False, id="fresh-candidate-without-kind"),
            pytest.param(True, ["V1"], True, id="fill-gaps-carried-verbatim"),
            pytest.param(True, [], False, id="fill-gaps-new-candidate-without-kind"),
        ],
    )
    def test_every_unverified_candidate_carries_its_capability_kind(
        self, tmp_path: Path, fill_gaps: bool, carried: list[str], allowed: bool
    ) -> None:
        """Omitting ``missing_capability_kind`` cannot evade the provisionable-tooling rule on a fresh record.

        Only a candidate a fill-gaps plan carries verbatim from a run that predates the key may lack it.
        """
        publication = self._blocked(
            [_keyless(candidate("V1", "UNVERIFIED"))], lineup=None if fill_gaps else _SOFTWARE_LINEUP
        )
        path = _write_producer(tmp_path, publication)
        if fill_gaps:
            _fill_gaps_plan(tmp_path, carried)
        line, reason = frr.publication_verdict(path, producer=True, fill_gaps=fill_gaps)
        assert bool(line) == allowed, reason
        if not allowed:
            assert "missing_capability_kind" in reason

    def test_fill_gaps_flag_without_its_plan_is_refused(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """``--fill-gaps`` lifts the lineup rule only beside the run's plan; without it no exemption can be read."""
        report = _write_producer(tmp_path, self._blocked([candidate("V1", "UNVERIFIED")], lineup=None))
        assert frr.main(["--report", str(report), "--producer", "--fill-gaps"]) == 1
        assert "[gate] parent evidence: fill-gaps.json" in capsys.readouterr().out

    def test_fill_gaps_flag_needs_the_producer_gate(self, tmp_path: Path) -> None:
        """``--fill-gaps`` qualifies the producer gate only; on intake it is a usage error."""
        with pytest.raises(SystemExit) as exit_info:
            frr.main(["--report", str(tmp_path / "review-report.md"), "--fill-gaps"])
        assert exit_info.value.code == 2
