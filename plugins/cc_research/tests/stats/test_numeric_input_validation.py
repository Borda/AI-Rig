"""Reject invalid numerical inputs before emitting metric or significance verdicts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import retro_analyze as ra
import run_metric_stats as rms


def test_oversized_integer_is_not_a_finite_metric() -> None:
    """A JSON integer outside floating-point range is invalid rather than a conversion crash."""
    assert rms._finite(10**400) is None


@pytest.mark.parametrize("direction", ["Lower", "LOWER", "min", "minimize", "typo", "", None, False])
def test_keep_config_rejects_unknown_direction(direction: object) -> None:
    """Explicit invalid directions cannot silently invert a keep decision."""
    with pytest.raises(ValueError, match="direction"):
        rms.keep_config({"config": {"metric": {"direction": direction}}})


@pytest.mark.parametrize("alpha", [0.0, 1.0, -0.1, 5.0, float("nan"), float("inf")])
def test_wilcoxon_rejects_invalid_alpha(alpha: float) -> None:
    """The significance threshold must be a probability strictly between zero and one."""
    with pytest.raises(ValueError, match="alpha"):
        ra.run_wilcoxon([1.0] * 8, [1.1] * 8, alpha=alpha)


@pytest.mark.parametrize("alpha", ["0", "1", "5", "nan", "inf"])
def test_retro_cli_reports_invalid_alpha(tmp_path: Path, capsys: pytest.CaptureFixture[str], alpha: str) -> None:
    """Invalid alpha is an input error even when too few samples would bypass testing."""
    ledger = tmp_path / "experiments.jsonl"
    ledger.write_text('{"status":"baseline","metric":1}\n', encoding="utf-8")
    assert ra.main(["--jsonl", str(ledger), "--alpha", alpha]) == 2
    assert "alpha" in json.loads(capsys.readouterr().out)["error"]


@pytest.mark.parametrize("metric", [True, False, float("nan"), float("inf"), float("-inf")])
def test_extract_scores_rejects_invalid_baseline(metric: object) -> None:
    """A boolean or non-finite baseline cannot support significance testing."""
    with pytest.raises(ValueError, match="metric"):
        ra._extract_scores([{"status": "baseline", "metric": metric}], "baseline")


@pytest.mark.parametrize("metric", [True, False, float("nan"), float("inf"), float("-inf")])
def test_extract_scores_rejects_invalid_candidate(metric: object) -> None:
    """An invalid kept metric is an input error rather than a sample or silent omission."""
    records = [{"status": "baseline", "metric": 1.0}, {"status": "kept", "metric": metric}]
    with pytest.raises(ValueError, match="metric"):
        ra._extract_scores(records, "baseline")


@pytest.mark.parametrize("value", ["²", "٤", pytest.param("9" * 5000, id="excessive-digit-count")])
def test_iteration_rejects_non_ascii_or_unconvertible_digits(value: str) -> None:
    """Handwritten iteration values must not crash ledger parsing."""
    assert rms._as_iteration(value) is None


@pytest.mark.parametrize("command", ["status", "decide"])
def test_metric_cli_reports_skipped_lines(tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str) -> None:
    """Skipping corrupt ledger rows is visible beside the resulting measurement verdict."""
    (tmp_path / "state.json").write_text('{"best_metric":1}', encoding="utf-8")
    (tmp_path / "metric-runs.jsonl").write_text(
        'not-json\n[]\n{"iteration":"²","metric":1}\n{"iteration":1,"metric":2}\n', encoding="utf-8"
    )
    assert rms.main([command, str(tmp_path), "--iteration", "1"]) == 0
    captured = capsys.readouterr()
    assert "skipped 3" in captured.err
    assert "malformed" in captured.err
    assert "RUN_METRIC_" in captured.out


@pytest.mark.parametrize("command", ["status", "decide"])
@pytest.mark.parametrize(
    "metrics",
    [pytest.param([1e308, 1e308], id="sum-overflows"), pytest.param([1e200, -1e200], id="variance-overflows")],
)
def test_metric_cli_blocks_nonfinite_aggregates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str, metrics: list[float]
) -> None:
    """Finite samples whose aggregate overflows never produce a completed or keep verdict."""
    (tmp_path / "state.json").write_text('{"config":{"metric_repeats":2},"best_metric":1}', encoding="utf-8")
    (tmp_path / "metric-runs.jsonl").write_text(
        "".join(json.dumps({"iteration": 1, "metric": metric}) + "\n" for metric in metrics), encoding="utf-8"
    )
    assert rms.main([command, str(tmp_path), "--iteration", "1"]) == 1
    output = capsys.readouterr().out
    assert "! BLOCKED" in output
    assert "RUN_METRIC_DONE" not in output
    assert "RUN_METRIC_IMPROVED" not in output


def test_metric_cli_blocks_underflowed_percentage_scale(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A subnormal nonzero best metric cannot cause a divide-by-zero traceback."""
    (tmp_path / "state.json").write_text('{"best_metric":5e-324}', encoding="utf-8")
    (tmp_path / "metric-runs.jsonl").write_text('{"iteration":1,"metric":1}\n', encoding="utf-8")
    assert rms.main(["decide", str(tmp_path), "--iteration", "1"]) == 1
    assert "! BLOCKED" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("new_runs", "best_runs"),
    [pytest.param(0, 1, id="zero-current-runs"), pytest.param(1, 0, id="zero-best-runs")],
)
def test_decide_rejects_zero_run_counts(new_runs: int, best_runs: int) -> None:
    """A missing sample count cannot be used as a noise-model divisor."""
    with pytest.raises(ValueError, match="runs"):
        rms.decide(rms.RunStats(new_runs, 2.0, 0.0), 1.0, 0.0, best_runs, rms.keep_config({}))
