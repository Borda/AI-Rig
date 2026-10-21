"""Tests for ``bin/fortify_metric_stats.py`` — fortify's repeated-run statistics and noise-aware ranking.

Covers:
* ``status``: next-run cursor from the run ledger, failed runs, done with mean and sample sigma
* full-first divergence gate against ``best_metric`` (pass, abort, sigma-widened tolerance, unreadable best)
* ``rank``: deltas, ``max(noise floor, 2 * sigma_delta)`` threshold, direction, BORDERLINE vs effect classes
* ``--runs`` bounds
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "fortify_metric_stats.py"
_spec = importlib.util.spec_from_file_location("research_fortify_metric_stats", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
sys.modules[_spec.name] = _mod  # dataclass resolution looks the defining module up by name
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

classify = _mod.classify
summarise = _mod.summarise
main = _mod.main


def _runs(fortify: Path, variant: str, metrics: list[float | None], exit_code: int = 0) -> None:
    with fortify.joinpath("metric-runs.jsonl").open("a", encoding="utf-8") as handle:
        for metric in metrics:
            handle.write(json.dumps({"variant": variant, "metric": metric, "exit": exit_code}) + "\n")


def _results(fortify: Path, rows: list[tuple[str, str]]) -> None:
    fortify.joinpath("results.jsonl").write_text(
        "".join(json.dumps({"variant": name, "status": status}) + "\n" for name, status in rows), encoding="utf-8"
    )


def _state(tmp_path: Path, best: object) -> Path:
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"best_metric": best}), encoding="utf-8")
    return state


class TestSummarise:
    def test_sample_sigma_uses_bessel_correction(self) -> None:
        """Sigma divides by n - 1, the unbiased sample estimate for a handful of runs."""
        assert summarise([2.0, 4.0, 6.0]) == _mod.RunStats(runs=3, mean=4.0, sigma=2.0)

    def test_single_run_has_undefined_sigma(self) -> None:
        """One run gives no spread estimate; sigma is None so callers fall back to the noise floor."""
        assert summarise([0.7]).sigma is None


class TestStatus:
    def test_asks_for_next_run_until_n_recorded(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """With fewer than N runs on the ledger the next 1-based run number is printed.

        The ledger, not model memory, is the inner-loop cursor, so a compaction mid-variant resumes at k + 1.
        """
        _runs(tmp_path, "variant-no-aug", [0.5])
        assert main(["status", str(tmp_path), "--variant", "variant-no-aug", "--runs", "3"]) == 0
        assert "FORTIFY_METRIC_NEXT_RUN=2 — 1/3 runs recorded" in capsys.readouterr().out

    @pytest.mark.parametrize(
        ("metric", "exit_code"),
        [pytest.param(0.5, 1, id="nonzero-exit"), pytest.param(None, 0, id="no-numeric-metric")],
    )
    def test_failed_run_marks_variant_failed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], metric: float | None, exit_code: int
    ) -> None:
        """A non-zero exit or a missing number ends the variant as metric-failed, even before N runs."""
        _runs(tmp_path, "variant-no-aug", [metric], exit_code)
        assert main(["status", str(tmp_path), "--variant", "variant-no-aug", "--runs", "3"]) == 0
        assert "FORTIFY_METRIC_FAILED=1 — run 1 of variant-no-aug" in capsys.readouterr().out

    def test_done_reports_mean_and_sigma_of_first_n(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Runs beyond N (a resumed variant re-measured) are ignored; the first N give mean and sigma."""
        _runs(tmp_path, "no-aug", [1.0, 2.0, 3.0, 100.0])
        assert main(["status", str(tmp_path), "--variant", "variant-no-aug", "--runs", "3"]) == 0
        assert "FORTIFY_METRIC_DONE=1 runs=3 mean=2 sigma=1" in capsys.readouterr().out

    @pytest.mark.parametrize(
        ("metrics", "best", "code", "marker"),
        [
            pytest.param([100.0, 100.2, 99.8], 101.0, 0, "full-first gate: PASS", id="within-fixed-tolerance"),
            pytest.param([90.0, 90.1, 89.9], 100.0, 1, "FORTIFY_FULL_DIVERGED=1", id="diverged-aborts"),
            pytest.param([84.0, 90.0, 96.0], 100.0, 0, "full-first gate: PASS", id="noisy-metric-widens-tolerance"),
            pytest.param([0.0, 0.0, 0.0], 0.0, 0, "full-first gate: PASS", id="zero-best-exact"),
        ],
    )
    def test_full_variant_divergence_gate(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        metrics: list[float],
        best: float,
        code: int,
        marker: str,
    ) -> None:
        """The full mean must reproduce best_metric within max(divergence pct, 2 sigma) or the run aborts.

        A deterministic worktree that measures 10% low is a broken environment (missing gitignored inputs); a noisy
        metric whose 2 sigma covers the gap is not.
        """
        _runs(tmp_path, "variant-full", metrics)
        state = _state(tmp_path, best)
        assert (
            main(["status", str(tmp_path), "--variant", "variant-full", "--runs", "3", "--state-json", str(state)])
            == code
        )
        assert marker in capsys.readouterr().out

    def test_unreadable_best_metric_blocks(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Without best_metric the reproduction check cannot run, so the gate fails closed."""
        _runs(tmp_path, "full", [1.0, 1.0, 1.0])
        state = _state(tmp_path, None)
        assert main(["status", str(tmp_path), "--variant", "full", "--runs", "3", "--state-json", str(state)]) == 1
        assert "! BLOCKED — best_metric unreadable" in capsys.readouterr().out


class TestClassify:
    @pytest.mark.parametrize(
        ("importance", "threshold", "expected"),
        [
            pytest.param(1.9, 2.0, "BORDERLINE", id="below-floor"),
            pytest.param(-15.0, 20.0, "BORDERLINE", id="within-two-sigma"),
            pytest.param(60.0, 2.0, "CRITICAL", id="critical"),
            pytest.param(10.0, 2.0, "SIGNIFICANT", id="significant-boundary"),
            pytest.param(4.0, 2.0, "MARGINAL", id="marginal"),
            pytest.param(12.0, 15.0, "BORDERLINE", id="noise-swallows-significant"),
            pytest.param(-4.0, 2.0, "NEUTRAL", id="neutral"),
            pytest.param(-6.0, 2.0, "HARMFUL", id="harmful"),
        ],
    )
    def test_class_against_noise_threshold(self, importance: float, threshold: float, expected: str) -> None:
        """Only an importance beyond max(noise floor, 2 sigma_delta) earns an effect class."""
        assert classify(importance, threshold) == expected


class TestRank:
    def test_writes_deltas_with_noise_threshold(self, tmp_path: Path) -> None:
        """A clear drop is SIGNIFICANT; a delta inside 2 sigma_delta is BORDERLINE, not MARGINAL.

        full = 100 ± 0; no-a = 80 (importance 20%); no-b mean 99 with sigma 3, sigma_delta = 3 / sqrt(3), so
        threshold = 2 * 1.732 = 3.46% > |1%|.
        """
        _runs(tmp_path, "variant-full", [100.0, 100.0, 100.0])
        _runs(tmp_path, "variant-no-a", [80.0, 80.0, 80.0])
        _runs(tmp_path, "variant-no-b", [96.0, 99.0, 102.0])
        _results(
            tmp_path, [("variant-full", "completed"), ("variant-no-a", "completed"), ("variant-no-b", "completed")]
        )
        assert main(["rank", str(tmp_path), "--runs", "3"]) == 0
        rows = {
            row["variant"]: row
            for row in map(json.loads, tmp_path.joinpath("results-deltas.jsonl").read_text("utf-8").splitlines())
        }
        assert (rows["no-a"]["class"], rows["no-b"]["class"]) == ("SIGNIFICANT", "BORDERLINE")
        assert rows["no-b"]["threshold_pct"] == pytest.approx(2 * 3 / 3**0.5)

    def test_lower_direction_flips_sign(self, tmp_path: Path) -> None:
        """For a lower-is-better metric, removing a component that raises the loss is helpful (positive importance)."""
        program = tmp_path / "program.md"
        program.write_text("## Metric\n```yaml\ncommand: x\ndirection: lower\n```\n\n## Guard\n", encoding="utf-8")
        _runs(tmp_path, "full", [1.0, 1.0, 1.0])
        _runs(tmp_path, "no-a", [1.5, 1.5, 1.5])
        _results(tmp_path, [("full", "completed"), ("no-a", "completed")])
        assert main(["rank", str(tmp_path), "--runs", "3", "--program-file", str(program)]) == 0
        row = json.loads(tmp_path.joinpath("results-deltas.jsonl").read_text("utf-8"))
        assert (row["importance_pct"], row["class"]) == (pytest.approx(50.0), "SIGNIFICANT")

    def test_skips_non_completed_ablations(self, tmp_path: Path) -> None:
        """Revert conflicts and failed metrics carry no measurement and stay out of the deltas file."""
        _runs(tmp_path, "full", [1.0])
        _results(tmp_path, [("full", "completed"), ("no-a", "revert-conflict")])
        assert main(["rank", str(tmp_path), "--runs", "1"]) == 0
        assert tmp_path.joinpath("results-deltas.jsonl").read_text("utf-8") == ""

    @pytest.mark.parametrize("status", ["diverged", "metric-failed"])
    def test_blocks_without_completed_full(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], status: str
    ) -> None:
        """No completed full baseline → no deltas; the skill's full-variant-failed path takes over."""
        _runs(tmp_path, "full", [1.0, 1.0, 1.0])
        _results(tmp_path, [("full", status)])
        assert main(["rank", str(tmp_path), "--runs", "3"]) == 1
        assert "! BLOCKED — full variant not completed" in capsys.readouterr().out


@pytest.mark.parametrize("runs", ["0", "11", "abc"])
def test_runs_out_of_range_is_argument_error(tmp_path: Path, runs: str) -> None:
    """``--runs`` accepts 1..10; anything else exits 2 before touching the ledger."""
    with pytest.raises(SystemExit) as excinfo:
        main(["status", str(tmp_path), "--variant", "full", "--runs", runs])
    assert excinfo.value.code == 2
