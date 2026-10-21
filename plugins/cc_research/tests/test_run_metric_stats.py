"""Tests for ``bin/run_metric_stats.py`` — repeated metric runs and the noise-aware keep decision of research:run.

Covers the ``status`` cursor over ``metric-runs.jsonl`` (next run, done, failed, other iterations ignored), the
``decide`` verdict against ``best_metric`` (noise threshold, minimum-delta floor, direction, zero best), and the fail-
closed paths (invalid repeat count, incomplete runs, missing best metric, bad arguments).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import run_metric_stats as rms


def _state(state_dir: Path, *, repeats: int | str = 3, direction: str = "higher", **top: object) -> None:
    """Write a ``state.json`` with the given repeat count, direction and top-level fields."""
    config = {"metric_cmd": "python bench.py", "metric": {"direction": direction}, "metric_repeats": repeats}
    state = {"config": config, "best_metric": 100.0, "best_metric_sigma": 0.2, "best_metric_runs": 3, **top}
    (state_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")


def _runs(state_dir: Path, iteration: int, *metrics: float | None, exit_code: int = 0) -> None:
    """Append one ledger line per metric for ``iteration``."""
    with (state_dir / "metric-runs.jsonl").open("a", encoding="utf-8") as ledger:
        for metric in metrics:
            ledger.write(json.dumps({"iteration": iteration, "metric": metric, "exit": exit_code}) + "\n")


class TestStatus:
    """The per-iteration cursor Phase 5 and R4 branch on."""

    def test_reports_next_run_until_repeats_recorded(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Two of three runs recorded → the next run is the third.

        The ledger is the cursor: a compaction between runs resumes from what was staged, not from memory.
        """
        _state(tmp_path)
        _runs(tmp_path, 4, 101.0, 102.0)
        assert rms.main(["status", str(tmp_path), "--iteration", "4"]) == 0
        assert capsys.readouterr().out.startswith("RUN_METRIC_NEXT_RUN=3 — 2/3 runs recorded for iteration 4")

    def test_string_iteration_in_staged_line_still_counts(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A staged line carrying ``"iteration":"4"`` is matched to iteration 4.

        The lines are model-written; a quoted number that never matched would leave ``status`` asking for run 1 forever.
        """
        _state(tmp_path)
        (tmp_path / "metric-runs.jsonl").write_text(
            "".join(json.dumps({"iteration": "4", "metric": m, "exit": 0}) + "\n" for m in (101.0, 102.0, 103.0)),
            encoding="utf-8",
        )
        assert rms.main(["status", str(tmp_path), "--iteration", "4"]) == 0
        assert capsys.readouterr().out.strip() == "RUN_METRIC_DONE=1 runs=3 mean=102 sigma=1"

    def test_reports_mean_and_sigma_when_done(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """All runs recorded → mean and sample sigma; runs of other iterations do not count."""
        _state(tmp_path)
        _runs(tmp_path, 3, 50.0, 50.0, 50.0)
        _runs(tmp_path, 4, 101.0, 102.0, 103.0)
        assert rms.main(["status", str(tmp_path), "--iteration", "4"]) == 0
        assert capsys.readouterr().out.strip() == "RUN_METRIC_DONE=1 runs=3 mean=102 sigma=1"

    @pytest.mark.parametrize(
        ("metric", "exit_code"),
        [pytest.param(None, 0, id="no-numeric-metric"), pytest.param(101.0, 1, id="non-zero-exit")],
    )
    def test_reports_failed_run(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], metric: float | None, exit_code: int
    ) -> None:
        """A run without a numeric metric or with a non-zero exit fails the iteration instead of being averaged."""
        _state(tmp_path)
        _runs(tmp_path, 4, 101.0)
        _runs(tmp_path, 4, metric, exit_code=exit_code)
        assert rms.main(["status", str(tmp_path), "--iteration", "4"]) == 0
        assert capsys.readouterr().out.startswith("RUN_METRIC_FAILED=1 — run 2 of iteration 4")

    @pytest.mark.parametrize(
        ("rework_runs", "expected"),
        [
            pytest.param((105.0,), "RUN_METRIC_NEXT_RUN=2 — 1/3 runs recorded", id="rework-in-progress"),
            pytest.param((105.0, 106.0, 107.0), "RUN_METRIC_DONE=1 runs=3 mean=106 sigma=1", id="rework-done"),
        ],
    )
    def test_rework_measures_a_fresh_block(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], rework_runs: tuple[float, ...], expected: str
    ) -> None:
        """A guard rework re-measures the amended commit as a new block of k runs under the same iteration.

        Without blocks the first measurement's three runs would already satisfy the count and the amended commit would
        be judged on the stale numbers.
        """
        _state(tmp_path)
        _runs(tmp_path, 4, 101.0, 102.0, 103.0)
        _runs(tmp_path, 4, *rework_runs)
        assert rms.main(["status", str(tmp_path), "--iteration", "4"]) == 0
        assert capsys.readouterr().out.startswith(expected)

    def test_invalid_repeat_count_blocks(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A ``metric_repeats`` outside 1-10 fails closed rather than silently measuring once."""
        _state(tmp_path, repeats=11)
        assert rms.main(["status", str(tmp_path), "--iteration", "1"]) == 1
        assert "! BLOCKED — config.metric_repeats must be an integer from 1 to 10" in capsys.readouterr().out


class TestDecide:
    """Phase 7's improved-beyond-noise verdict."""

    @pytest.mark.parametrize(
        ("direction", "metrics", "expected"),
        [
            pytest.param("higher", (101.0, 101.2, 100.8), "RUN_METRIC_IMPROVED=1", id="clear-gain"),
            pytest.param("higher", (100.05, 100.1, 99.95), "RUN_METRIC_IMPROVED=0", id="gain-under-min-delta"),
            pytest.param("higher", (104.0, 98.0, 101.0), "RUN_METRIC_IMPROVED=0", id="gain-inside-noise"),
            pytest.param("higher", (99.0, 99.0, 99.0), "RUN_METRIC_IMPROVED=0", id="regression"),
            pytest.param("lower", (98.0, 98.2, 97.8), "RUN_METRIC_IMPROVED=1", id="lower-is-better-gain"),
        ],
    )
    def test_verdict(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        direction: str,
        metrics: tuple[float, ...],
        expected: str,
    ) -> None:
        """Only a direction-signed gain beyond ``max(min_delta_pct, 2 sigma_delta)`` counts as improved.

        Best is 100 with sigma 0.2 over 3 runs. A 1% gain with tight runs clears both bars; a 0.03% gain sits under the
        0.1% floor; a 1% mean gain whose runs spread by ±3 sits inside 2 sigma_delta; a lower-is-better metric counts a
        drop as the gain.
        """
        _state(tmp_path, direction=direction)
        _runs(tmp_path, 5, *metrics)
        assert rms.main(["decide", str(tmp_path), "--iteration", "5"]) == 0
        assert capsys.readouterr().out.startswith(expected)

    def test_zero_best_compares_in_metric_units(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """With ``best_metric`` 0 percentages are undefined, so the gain is held against 2 sigma_delta in metric
        units."""
        _state(tmp_path, best_metric=0.0, best_metric_sigma=0.0)
        _runs(tmp_path, 2, 0.5, 0.5, 0.5)
        assert rms.main(["decide", str(tmp_path), "--iteration", "2"]) == 0
        out = capsys.readouterr().out
        assert out.startswith("RUN_METRIC_IMPROVED=1")
        assert "metric units" in out

    @pytest.mark.parametrize(
        ("state_extra", "runs", "message"),
        [
            pytest.param({}, (101.0,), "has no 3 complete metric runs", id="incomplete-runs"),
            pytest.param({"best_metric": None}, (101.0, 101.0, 101.0), "best_metric unreadable", id="no-best-metric"),
        ],
    )
    def test_blocks_without_complete_inputs(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        state_extra: dict[str, object],
        runs: tuple[float, ...],
        message: str,
    ) -> None:
        """Deciding before Phase 5 finished, or with no baseline best metric, stops instead of guessing."""
        _state(tmp_path, **state_extra)
        _runs(tmp_path, 6, *runs)
        assert rms.main(["decide", str(tmp_path), "--iteration", "6"]) == 1
        assert message in capsys.readouterr().out


class TestArguments:
    """Command-line validation."""

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param(["status", "dir", "--iteration", "-1"], id="negative-iteration"),
            pytest.param(["status", "dir", "--iteration", "x"], id="non-integer-iteration"),
            pytest.param(["decide", "dir"], id="missing-iteration"),
        ],
    )
    def test_bad_arguments_exit_two(self, argv: list[str]) -> None:
        """A missing, negative or non-integer ``--iteration`` is an argument error."""
        assert rms.main(argv) == 2

    def test_unreadable_state_blocks(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """No ``state.json`` in the run directory → BLOCKED, exit 1."""
        assert rms.main(["status", str(tmp_path), "--iteration", "1"]) == 1
        assert "! BLOCKED" in capsys.readouterr().out
