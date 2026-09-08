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

from pathlib import Path
from typing import Any

import pytest

from _launcher_capability import _raw_codemap_launchers_are_runnable

REPO_ROOT = Path(__file__).parent.parent.parent
RAW_CODEMAP_LAUNCHERS = pytest.mark.skipif(
    not _raw_codemap_launchers_are_runnable(REPO_ROOT),
    reason="raw scan-index/scan-query launchers cannot execute on this host",
)

# ===========================================================================
# _Checklist accumulator
# ===========================================================================


class TestChecklist:
    """_Checklist folds named sub-checks into one conjunctive pass/fail verdict."""

    def test_empty_checklist_does_not_pass(self, script_run_cli: Any) -> None:
        """A checklist with no recorded checks is not vacuously passing."""
        cl = script_run_cli._Checklist()
        assert cl.passed is False

    def test_all_true_passes(self, script_run_cli: Any) -> None:
        """A checklist where every recorded check is True passes."""
        cl = script_run_cli._Checklist()
        cl.record("a", True)
        cl.record("b", True)
        assert cl.passed is True

    def test_one_false_fails_and_lists_failure(self, script_run_cli: Any) -> None:
        """A single failing check flips the verdict and surfaces in ``failures``."""
        cl = script_run_cli._Checklist()
        cl.record("a", True)
        cl.record("b", False)
        assert cl.passed is False
        assert cl.failures == ["b"]

    def test_record_coerces_truthy_to_bool(self, script_run_cli: Any) -> None:
        """Store a real bool so result payloads stay JSON-clean."""
        cl = script_run_cli._Checklist()
        cl.record("a", ["non-empty"])
        assert cl.checks == [("a", True)]


# ===========================================================================
# _correctness_scenario folding
# ===========================================================================


class TestCorrectnessScenario:
    """_correctness_scenario builds one ScenarioResult from a checklist or a setup error."""

    def test_passing_checklist_yields_passed_scenario(self, script_run_cli: Any) -> None:
        """A fully-passing checklist produces a passed correctness scenario."""
        cl = script_run_cli._Checklist()
        cl.record("a", True)
        scenario = script_run_cli._correctness_scenario("D_diff_impact", "diff-impact", cl)
        assert scenario.passed is True
        assert scenario.suite == "correctness"
        assert scenario.result["contract_holds"] is True

    def test_failing_checklist_reports_failed_checks(self, script_run_cli: Any) -> None:
        """A checklist with a failure names the failed check in the result payload."""
        cl = script_run_cli._Checklist()
        cl.record("a", True)
        cl.record("b", False)
        scenario = script_run_cli._correctness_scenario("B_batch", "batch", cl)
        assert scenario.passed is False
        assert scenario.result["failed_checks"] == ["b"]

    def test_setup_error_fails_outright(self, script_run_cli: Any) -> None:
        """A fixture-setup error fails the scenario and surfaces the reason, ignoring checks."""
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

    def test_correctness_suite_is_primary(self, script_run_cli: Any) -> None:
        """A failing correctness scenario can drag the primary verdict down to PARTIAL."""
        results = [
            script_run_cli.ScenarioResult("C1", "x", "calls", True, {}, {}),
            script_run_cli.ScenarioResult("D_diff_impact", "x", "correctness", False, {}, {}),
        ]
        assert script_run_cli.compute_verdict(results) == "PARTIAL"

    def test_correctness_excluded_from_self_consistency(self, script_run_cli: Any) -> None:
        """A correctness scenario never contributes to the self-consistency track."""
        results = [script_run_cli.ScenarioResult("D_diff_impact", "x", "correctness", True, {}, {})]
        assert script_run_cli.compute_self_consistency(results)["verdict"] == "SKIPPED"

    def test_envelope_counts_correctness_suite(self, script_run_cli: Any, tmp_path: Path) -> None:
        """The summary envelope tallies the correctness suite under its own key."""
        results = [script_run_cli.ScenarioResult("D_diff_impact", "x", "correctness", True, {}, {})]
        env = script_run_cli.build_summary_envelope(results, tmp_path, tmp_path / "i.json", "PASS")
        assert env["suites"]["correctness"] == {"passed": 1, "total": 1}


# ===========================================================================
# Integration: each suite runs green against real fixtures + binaries
# ===========================================================================


def _bins(script_run_cli: Any, scan_query_binary: Path, scan_index_binary: Path) -> tuple[Path, Path]:
    """Return the prevalidated query and index fixture paths in invocation order.

    No finder or executable is invoked here; the runner argument is retained for fixture compatibility.

    Args:
        script_run_cli: Loaded Codemap CLI module.
        scan_query_binary: scan-query path fixture (also asserts it exists).
        scan_index_binary: scan-index path fixture (also asserts it exists).

    Returns:
        Tuple of resolved binary paths.

    Example:
        >>> query, index = Path("query"), Path("index")
        >>> _bins(None, query, index) == (query, index)
        True
    """
    _ = script_run_cli  # binaries come straight from the conftest fixtures
    return scan_query_binary, scan_index_binary


@RAW_CODEMAP_LAUNCHERS
class TestSuitesRunGreen:
    """Every fixture-based correctness suite passes end-to-end against the real binaries."""

    @pytest.mark.parametrize(
        "suite_attr",
        [
            "run_correctness_diff_impact",
            "run_correctness_batch",
            "run_correctness_src_roots",
            "run_correctness_self_check",
            "run_correctness_uncovered_xrefs",
        ],
    )
    def test_suite_passes_against_own_fixture(
        self,
        script_run_cli: Any,
        scan_query_binary: Path,
        scan_index_binary: Path,
        suite_attr: str,
    ) -> None:
        """Each suite builds its fixture and reports a single passing correctness scenario.

        Args:
            script_run_cli: Loaded Codemap CLI module.
            scan_query_binary: scan-query path fixture.
            scan_index_binary: scan-index path fixture.
            suite_attr: Name of the ``run_correctness_*`` function under test.
        """
        sq, si = _bins(script_run_cli, scan_query_binary, scan_index_binary)
        results = getattr(script_run_cli, suite_attr)(sq, si)
        assert len(results) == 1
        scenario = results[0]
        assert scenario.suite == "correctness"
        assert scenario.passed is True, scenario.result.get("failed_checks", scenario.result)

    @pytest.mark.parametrize(
        "suite_attr",
        [
            "run_correctness_diff_impact",
            "run_correctness_batch",
            "run_correctness_src_roots",
            "run_correctness_self_check",
            "run_correctness_uncovered_xrefs",
        ],
    )
    def test_suite_skips_without_scan_index(
        self,
        script_run_cli: Any,
        scan_query_binary: Path,
        suite_attr: str,
    ) -> None:
        """With scan-index absent every suite skips (returns no scenarios), never crashing.

        Args:
            script_run_cli: Loaded Codemap CLI module.
            scan_query_binary: scan-query path fixture.
            suite_attr: Name of the ``run_correctness_*`` function under test.
        """
        results = getattr(script_run_cli, suite_attr)(scan_query_binary, None)
        assert results == []


@RAW_CODEMAP_LAUNCHERS
class TestDiffImpactChecks:
    """The diff-impact suite asserts the specific known-ground-truth blast radius."""

    def test_records_high_tier_and_six_importers(
        self, script_run_cli: Any, scan_query_binary: Path, scan_index_binary: Path
    ) -> None:
        """The 5-consumer + 1-test fixture yields a HIGH tier with exactly 6 importers."""
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
        """Exactly 2 undocumented public functions and 1 broken sphinx xref are found."""
        results = script_run_cli.run_correctness_uncovered_xrefs(scan_query_binary, scan_index_binary)
        checks = results[0].result["checks"]
        assert checks["undocumented_total_is_2"] is True
        assert checks["broken_xref_count_is_1"] is True
        assert checks["broken_xref_target_exact"] is True
