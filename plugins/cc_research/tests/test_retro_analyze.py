"""Tests for ``retro_analyze.py``.

Covers:
    - ``run_wilcoxon`` pure-function contract (significance, insufficient data, validation errors).
    - ``main()``: argparse defaults, JSONL parsing, baseline/kept extraction, exit codes,
      truncated trailing line tolerance, missing-file path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import retro_analyze as ra


# ---------- Pure function: run_wilcoxon ----------


class TestRunWilcoxon:
    """Significance-test contract — direction handling, sample-size gate, validation."""

    def test_higher_direction_detects_consistent_improvement(self) -> None:
        """All candidates above baseline with N >= 6 → significant at alpha=0.05."""
        baseline = [1.0] * 8
        candidate = [1.5, 1.6, 1.7, 1.8, 1.9, 2.0, 2.1, 2.2]
        result = ra.run_wilcoxon(baseline, candidate, alpha=0.05, direction=ra.Direction.HIGHER)
        assert result["n"] == 8
        assert result["significant"] is True
        assert result["p_value"] is not None and result["p_value"] < 0.05

    def test_lower_direction_detects_consistent_improvement(self) -> None:
        """Lower-is-better metric: candidates below baseline → significant."""
        baseline = [10.0] * 8
        candidate = [9.0, 8.5, 8.0, 7.5, 7.0, 6.5, 6.0, 5.5]
        result = ra.run_wilcoxon(baseline, candidate, alpha=0.05, direction=ra.Direction.LOWER)
        assert result["significant"] is True
        assert result["p_value"] is not None and result["p_value"] < 0.05

    def test_insufficient_samples_returns_reason_not_pvalue(self) -> None:
        """N below MIN_SAMPLES_FOR_TEST → significant=False, p_value=None, reason present."""
        result = ra.run_wilcoxon([1.0] * 3, [2.0] * 3, alpha=0.05, direction=ra.Direction.HIGHER)
        assert result["significant"] is False
        assert result["p_value"] is None
        assert result["statistic"] is None
        assert result["n"] == 3
        assert "insufficient data" in result["reason"]

    @pytest.mark.parametrize(
        "baseline,candidate,direction",
        [
            pytest.param([1.0] * 8, [1.0] * 8, ra.Direction.HIGHER, id="1.0-8-1.0-8"),
            pytest.param(
                [1.0] * 8,
                [0.8, 1.2, 0.9, 1.1, 0.95, 1.05, 1.0, 1.0],
                ra.Direction.HIGHER,
                id="1.0-8-0.8-1.2-0.9-1.1-0.95-1.05-1.0-1.0",
            ),
            pytest.param([1.0] * 8, [0.5] * 8, ra.Direction.HIGHER, id="1.0-8-0.5-8"),
            pytest.param([1.0] * 8, [1.5] * 8, ra.Direction.LOWER, id="1.0-8-1.5-8"),
        ],
    )
    def test_adequate_sample_non_significant_cases(
        self, baseline: list[float], candidate: list[float], direction: ra.Direction
    ) -> None:
        """Adequate sample size alone does not imply significance."""
        result = ra.run_wilcoxon(baseline, candidate, alpha=0.05, direction=direction)
        assert result["n"] == 8
        assert result["significant"] is False

    def test_invalid_direction_raises_value_error(self) -> None:
        """Direction must be 'higher' or 'lower' — anything else raises."""
        with pytest.raises(ValueError, match="direction must be 'higher' or 'lower'"):
            ra.run_wilcoxon([1.0], [2.0], direction="sideways")

    def test_mismatched_lengths_raise_value_error(self) -> None:
        """Score arrays must be paired — different lengths raise ValueError."""
        with pytest.raises(ValueError, match="must have the same length"):
            ra.run_wilcoxon([1.0, 1.0], [2.0, 2.0, 2.0])


# ---------- CLI: main() ----------


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    """Write a list of records as one JSON object per line.

    Examples:
        >>> path = getfixture("tmp_path") / "records.jsonl"
        >>> _write_jsonl(path, [{"status": "baseline"}, {"status": "candidate"}])
        >>> len(path.read_text().splitlines())
        2
    """
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


class TestMainCLI:
    """End-to-end argparse + JSONL parsing — exit codes and output shape."""

    def test_significant_result_exits_zero(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Strong improvement → exit 0 and JSON with significant=True."""
        jsonl = tmp_path / "experiments.jsonl"
        _write_jsonl(
            jsonl,
            [{"status": "baseline", "metric": 1.0}]
            + [{"status": "kept", "metric": 1.0 + 0.1 * (i + 1)} for i in range(8)],
        )
        exit_code = ra.main(["--jsonl", str(jsonl), "--alpha", "0.05", "--direction", "higher"])
        out = json.loads(capsys.readouterr().out.strip())
        assert exit_code == 0
        assert out["significant"] is True
        assert out["n"] == 8

    def test_insufficient_data_exits_one_with_reason(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Few kept iterations → exit 1 (not significant) with reason field."""
        jsonl = tmp_path / "experiments.jsonl"
        _write_jsonl(
            jsonl,
            [{"status": "baseline", "metric": 1.0}, {"status": "kept", "metric": 1.5}],
        )
        exit_code = ra.main(["--jsonl", str(jsonl)])
        out = json.loads(capsys.readouterr().out.strip())
        assert exit_code == 1
        assert out["significant"] is False
        assert "insufficient data" in out["reason"]

    def test_missing_file_exits_two_with_error(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Nonexistent JSONL path → exit 2 with error key in output."""
        exit_code = ra.main(["--jsonl", str(tmp_path / "missing.jsonl")])
        out = json.loads(capsys.readouterr().out.strip())
        assert exit_code == 2
        assert "error" in out
        assert "missing.jsonl" in out["error"]

    def test_missing_baseline_record_exits_two(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """JSONL without a baseline record → exit 2 with error message."""
        jsonl = tmp_path / "experiments.jsonl"
        _write_jsonl(jsonl, [{"status": "kept", "metric": 1.5}])
        exit_code = ra.main(["--jsonl", str(jsonl)])
        out = json.loads(capsys.readouterr().out.strip())
        assert exit_code == 2
        assert "no baseline record found" in out["error"]

    def test_truncated_trailing_line_is_tolerated(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Trailing partial JSON line is silently dropped; preceding records still parsed."""
        jsonl = tmp_path / "experiments.jsonl"
        good = [{"status": "baseline", "metric": 1.0}] + [
            {"status": "kept", "metric": 1.0 + 0.05 * (i + 1)} for i in range(7)
        ]
        jsonl.write_text(
            "\n".join(json.dumps(r) for r in good) + '\n{"status": "kep',
            encoding="utf-8",
        )
        exit_code = ra.main(["--jsonl", str(jsonl)])
        out = json.loads(capsys.readouterr().out.strip())
        # 7 kept entries — exit code may be 0 or 1 depending on signal strength; what we
        # care about here is that parsing did NOT raise and N reflects only the good lines.
        assert exit_code in (0, 1)
        assert out["n"] == 7

    def test_malformed_middle_line_exits_two(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Malformed JSON before later records is a data error, not a tolerated trailing truncation."""
        jsonl = tmp_path / "experiments.jsonl"
        jsonl.write_text(
            json.dumps({"status": "baseline", "metric": 1.0})
            + '\n{"status": "kept", bad}\n'
            + json.dumps({"status": "kept", "metric": 1.2})
            + "\n",
            encoding="utf-8",
        )
        exit_code = ra.main(["--jsonl", str(jsonl)])
        out = json.loads(capsys.readouterr().out.strip())
        assert exit_code == 2
        assert "malformed JSON" in out["error"]

    def test_custom_baseline_label(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Verify command-line option behavior.

        The ``--baseline`` flag selects which status string marks the baseline record.
        """
        jsonl = tmp_path / "experiments.jsonl"
        _write_jsonl(
            jsonl,
            [{"status": "control", "metric": 5.0}]
            + [{"status": "kept", "metric": 5.0 + (i + 1) * 0.3} for i in range(8)],
        )
        exit_code = ra.main(["--jsonl", str(jsonl), "--baseline", "control", "--direction", "higher"])
        out = json.loads(capsys.readouterr().out.strip())
        assert exit_code == 0
        assert out["significant"] is True
