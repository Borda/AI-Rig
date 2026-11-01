"""Tests for the deterministic correctness suites (D/B/R/K/U) in run-codemap-cli.py.

These suites build self-contained fixture repos in a tmp dir with KNOWN ground truth
and assert the user-visible scan-query CLI contract as a product acceptance check.
Coverage splits into two layers:

  - Pure/unit: the ``_Checklist`` accumulator and ``_correctness_scenario`` folding
    (no subprocess, no binaries).
  - Integration: each ``run_correctness_*`` suite executed end-to-end against real
    scan-index / scan-query binaries — the same green run the benchmark relies on —
    plus the scan-index-absent skip path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from _launcher_capability import _raw_codemap_launchers_are_runnable

REPO_ROOT = Path(__file__).parent.parent.parent.parent
RAW_CODEMAP_LAUNCHERS = pytest.mark.skipif(
    not _raw_codemap_launchers_are_runnable(REPO_ROOT),
    reason="raw scan-index/scan-query launchers cannot execute on this host",
)
CORRECTNESS_SUITES = [
    "run_correctness_diff_impact",
    "run_correctness_batch",
    "run_correctness_src_roots",
    "run_correctness_self_check",
    "run_correctness_uncovered_xrefs",
]


def _checklist(script_cli: Any, records: list[tuple[str, Any]]) -> Any:
    """Build a ``_Checklist`` pre-loaded with the given ``(name, value)`` records.

    Example:
        >>> _checklist(getfixture("script_run_cli"), [("a", True), ("b", False)]).failures
        ['b']
    """
    checklist = script_cli._Checklist()
    for name, value in records:
        checklist.record(name, value)
    return checklist


# ===========================================================================
# _Checklist accumulator
# ===========================================================================


class TestChecklist:
    """_Checklist folds named sub-checks into one conjunctive pass/fail verdict."""

    @pytest.mark.parametrize(
        ("records", "expected"),
        [
            pytest.param([], False, id="empty-is-not-vacuously-passing"),
            pytest.param([("a", True), ("b", True)], True, id="all-true"),
            pytest.param([("a", True), ("b", False)], False, id="one-false"),
        ],
    )
    def test_passes_only_when_every_check_holds(self, script_run_cli: Any, records: list, expected: bool) -> None:
        """The verdict is the conjunction of the recorded checks, and false when none were recorded.

        Scenario: an empty checklist means the suite recorded nothing, which must not read as a pass.
        """
        assert _checklist(script_run_cli, records).passed is expected

    def test_failures_lists_failed_check_names(self, script_run_cli: Any) -> None:
        """A single failing check surfaces by name in ``failures``.

        Scenario: one passing and one failing sub-check; the report must name only the failing one.
        """
        cl = script_run_cli._Checklist()
        cl.record("a", True)
        cl.record("b", False)
        assert cl.failures == ["b"]

    def test_record_coerces_truthy_to_bool(self, script_run_cli: Any) -> None:
        """A truthy value is stored as a real bool.

        Scenario: a suite records a non-empty list as its condition; the stored value must be True so the result
        payload stays JSON-clean.
        """
        cl = script_run_cli._Checklist()
        cl.record("a", ["non-empty"])
        assert cl.checks == [("a", True)]


# ===========================================================================
# _correctness_scenario folding
# ===========================================================================


class TestCorrectnessScenario:
    """_correctness_scenario builds one ScenarioResult from a checklist or a setup error."""

    def test_passing_checklist_yields_passed_scenario(self, script_run_cli: Any) -> None:
        """A fully passing checklist produces a passed correctness scenario.

        Scenario: one passing check folded under the diff-impact scenario id.
        """
        cl = script_run_cli._Checklist()
        cl.record("a", True)
        scenario = script_run_cli._correctness_scenario("D_diff_impact", "diff-impact", cl)
        assert scenario.passed is True
        assert scenario.suite == "correctness"
        assert scenario.result["contract_holds"] is True

    def test_failing_checklist_reports_failed_checks(self, script_run_cli: Any) -> None:
        """A checklist with a failure names the failed check in the result payload.

        Scenario: one passing and one failing check folded under the batch scenario id.
        """
        cl = script_run_cli._Checklist()
        cl.record("a", True)
        cl.record("b", False)
        scenario = script_run_cli._correctness_scenario("B_batch", "batch", cl)
        assert scenario.passed is False
        assert scenario.result["failed_checks"] == ["b"]

    def test_setup_error_fails_outright(self, script_run_cli: Any) -> None:
        """A fixture-setup error fails the scenario and surfaces the reason, ignoring recorded checks.

        Scenario: the checklist would pass, but a crashed fixture build means nothing was really verified.
        """
        cl = script_run_cli._Checklist()
        cl.record("a", True)  # would pass, but the error must dominate
        scenario = script_run_cli._correctness_scenario("K_self_check", "self-check", cl, error="scan-index blew up")
        assert scenario.passed is False
        assert scenario.result["error"] == "scan-index blew up"
        assert scenario.result["contract_holds"] is False


# ===========================================================================
# Suite verdict integration — correctness joins the primary verdict
# ===========================================================================


class TestCorrectnessInPrimaryVerdict:
    """The correctness suite counts toward the primary verdict, not the self-consistency track."""

    def test_is_primary(self, script_run_cli: Any) -> None:
        """A failing correctness scenario can drag the primary verdict down to PARTIAL.

        Scenario: one passing calls scenario and one failing correctness scenario make exactly half pass.
        """
        results = [
            script_run_cli.ScenarioResult("C1", "x", "calls", True, {}, {}),
            script_run_cli.ScenarioResult("D_diff_impact", "x", "correctness", False, {}, {}),
        ]
        assert script_run_cli.compute_verdict(results) == "PARTIAL"

    def test_excluded_from_self_consistency(self, script_run_cli: Any) -> None:
        """A correctness scenario never contributes to the self-consistency track.

        Scenario: a run holding only a passing correctness scenario leaves the S/H/X track with nothing to grade.
        """
        results = [script_run_cli.ScenarioResult("D_diff_impact", "x", "correctness", True, {}, {})]
        assert script_run_cli.compute_self_consistency(results)["verdict"] == "SKIPPED"

    def test_envelope_counts_suite(self, script_run_cli: Any, tmp_path: Path) -> None:
        """The summary envelope tallies the correctness suite under its own key.

        Scenario: one passing correctness scenario must appear as 1 of 1 under suites.correctness.
        """
        results = [script_run_cli.ScenarioResult("D_diff_impact", "x", "correctness", True, {}, {})]
        env = script_run_cli.build_summary_envelope(results, tmp_path, tmp_path / "i.json", "PASS")
        assert env["suites"]["correctness"] == {"passed": 1, "total": 1}


# ===========================================================================
# Integration: each suite runs green against real fixtures + binaries
# ===========================================================================


@pytest.mark.integration
def test_fixture_git_does_not_spawn_automatic_maintenance(
    script_run_cli: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep automatic Git writers out of the disposable fixture's cleanup window.

    Scenario: real git with maintenance.auto enabled and a trace2 event log; the fixture helper must commit without
    starting any maintenance or gc child that could still be writing when the temp tree is deleted.
    """
    root = tmp_path / "repo"
    root.mkdir()
    script_run_cli._fixture_git(root, "init", "-q")
    script_run_cli._fixture_git(root, "config", "maintenance.auto", "true")
    script_run_cli._fixture_git(root, "config", "maintenance.autoDetach", "false")
    trace = tmp_path / "git-events.jsonl"
    monkeypatch.setenv("GIT_TRACE2_EVENT", trace.as_posix())

    script_run_cli._fixture_git(root, "commit", "--allow-empty", "-q", "-m", "fixture")

    events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    assert any(event["event"] == "exit" and event["code"] == 0 for event in events)
    maintenance = [
        event["argv"]
        for event in events
        if event["event"] == "child_start" and any(arg in {"maintenance", "gc"} for arg in event["argv"])
    ]
    assert maintenance == []


@RAW_CODEMAP_LAUNCHERS
class TestSuitesRunGreen:
    """Every fixture-based correctness suite passes end-to-end against the real binaries."""

    @pytest.mark.parametrize("suite_attr", CORRECTNESS_SUITES)
    def test_suite_passes_against_own_fixture(
        self, script_run_cli: Any, scan_query_binary: Path, scan_index_binary: Path, suite_attr: str
    ) -> None:
        """Each suite builds its fixture and reports a single passing correctness scenario.

        Scenario: real scan-index and scan-query run against the suite's own constructed repo with known ground truth.
        """
        results = getattr(script_run_cli, suite_attr)(scan_query_binary, scan_index_binary)
        assert len(results) == 1
        scenario = results[0]
        assert scenario.suite == "correctness"
        assert scenario.passed is True, scenario.result.get("failed_checks", scenario.result)

    @pytest.mark.parametrize("suite_attr", CORRECTNESS_SUITES)
    def test_suite_skips_without_scan_index(
        self, script_run_cli: Any, scan_query_binary: Path, suite_attr: str
    ) -> None:
        """With scan-index absent every suite skips (returns no scenarios) instead of crashing.

        Scenario: a host that has scan-query but no scan-index must yield an empty result list for each suite.
        """
        assert getattr(script_run_cli, suite_attr)(scan_query_binary, None) == []


@RAW_CODEMAP_LAUNCHERS
class TestDiffImpactChecks:
    """The diff-impact suite asserts the specific known-ground-truth blast radius."""

    def test_records_high_tier_and_six_importers(
        self, script_run_cli: Any, scan_query_binary: Path, scan_index_binary: Path
    ) -> None:
        """The 5-consumer + 1-test fixture yields a HIGH tier with exactly 6 importers.

        Scenario: real binaries on a fixture whose blast radius is known, plus one changed file outside the index.
        """
        results = script_run_cli.run_correctness_diff_impact(scan_query_binary, scan_index_binary)
        checks = results[0].result["checks"]
        assert checks["risk_tier_high_5plus_importers"] is True
        assert checks["importer_count_is_6"] is True
        assert checks["unmapped_file_surfaced"] is True


@RAW_CODEMAP_LAUNCHERS
class TestUncoveredXrefsChecks:
    """The uncovered/xrefs suite asserts exact counts from the constructed fixture."""

    def test_exact_undocumented_and_broken_counts(
        self, script_run_cli: Any, scan_query_binary: Path, scan_index_binary: Path
    ) -> None:
        """Exactly 2 undocumented public functions and 1 broken sphinx xref are found.

        Scenario: real binaries on a fixture built with a known number of docstring gaps and one dangling reference.
        """
        results = script_run_cli.run_correctness_uncovered_xrefs(scan_query_binary, scan_index_binary)
        checks = results[0].result["checks"]
        assert checks["undocumented_total_is_2"] is True
        assert checks["broken_xref_count_is_1"] is True
        assert checks["broken_xref_target_exact"] is True
