"""Tests for ``retro_analyze.py``.

Covers:
    - ``run_wilcoxon`` pure-function contract (significance, insufficient data, validation errors).
    - ``main()``: argparse defaults, JSONL parsing, baseline/kept extraction, exit codes,
      truncated trailing line tolerance, missing-file path.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
import retro_analyze as ra


class TestRunWilcoxon:
    """Significance-test contract — direction handling, sample-size gate, validation."""

    @pytest.mark.parametrize(
        ("baseline", "candidate", "direction"),
        [
            pytest.param(
                [1.0] * 8,
                [1.5, 1.6, 1.7, 1.8, 1.9, 2.0, 2.1, 2.2],
                ra.Direction.HIGHER,
                id="higher-direction-candidates-above-baseline",
            ),
            pytest.param(
                [10.0] * 8,
                [9.0, 8.5, 8.0, 7.5, 7.0, 6.5, 6.0, 5.5],
                ra.Direction.LOWER,
                id="lower-direction-candidates-below-baseline",
            ),
        ],
    )
    def test_consistent_improvement_is_significant(
        self, baseline: list[float], candidate: list[float], direction: ra.Direction
    ) -> None:
        """Consistent improvement in the chosen direction with N >= 6 → significant at alpha=0.05.

        Higher-is-better: all candidates above baseline. Lower-is-better: all candidates below baseline.
        """
        result = ra.run_wilcoxon(baseline, candidate, alpha=0.05, direction=direction)

        assert result.n == 8
        assert result.significant is True
        assert result.p_value is not None
        assert result.p_value < 0.05

    def test_insufficient_samples_returns_reason_not_pvalue(self) -> None:
        """N below MIN_SAMPLES_FOR_TEST → significant=False, p_value=None, reason present.

        Three pairs are too few for the test to be meaningful, so the result explains why instead of reporting a
        p-value.
        """
        result = ra.run_wilcoxon([1.0] * 3, [2.0] * 3, alpha=0.05, direction=ra.Direction.HIGHER)

        assert result.significant is False
        assert result.p_value is None
        assert result.statistic is None
        assert result.n == 3
        assert "insufficient data" in result.reason

    @pytest.mark.parametrize(
        ("baseline", "candidate", "direction"),
        [
            pytest.param([1.0] * 8, [1.0] * 8, ra.Direction.HIGHER, id="identical-scores"),
            pytest.param(
                [1.0] * 8, [0.8, 1.2, 0.9, 1.1, 0.95, 1.05, 1.0, 1.0], ra.Direction.HIGHER, id="mixed-up-and-down"
            ),
            pytest.param([1.0] * 8, [0.5] * 8, ra.Direction.HIGHER, id="all-worse-when-higher-is-better"),
            pytest.param([1.0] * 8, [1.5] * 8, ra.Direction.LOWER, id="all-worse-when-lower-is-better"),
        ],
    )
    def test_adequate_sample_non_significant_cases(
        self, baseline: list[float], candidate: list[float], direction: ra.Direction
    ) -> None:
        """Adequate sample size alone does not imply significance.

        No change, noise around the baseline, and a consistent move the wrong way for the chosen direction must all stay
        non-significant even though N=8 clears the sample-size gate.
        """
        result = ra.run_wilcoxon(baseline, candidate, alpha=0.05, direction=direction)

        assert result.n == 8
        assert result.significant is False

    @pytest.mark.filterwarnings("error::RuntimeWarning")
    @pytest.mark.parametrize(
        "direction", [pytest.param(ra.Direction.HIGHER, id="higher"), pytest.param(ra.Direction.LOWER, id="lower")]
    )
    def test_identical_scores_report_no_change_without_warning(self, direction: ra.Direction) -> None:
        """Every candidate equal to the baseline → p_value 1.0, statistic 0.0, no reason, and no RuntimeWarning.

        With every paired difference zero, scipy's normal approximation divides by a zero standard error and warns
        before returning statistic 0.0 and p-value 1.0. The result must keep exactly that conclusive shape, so the CLI
        payload still carries no ``reason`` key.
        """
        result = ra.run_wilcoxon([1.0] * 8, [1.0] * 8, alpha=0.05, direction=direction)

        assert result == ra.WilcoxonResult(significant=False, p_value=1.0, statistic=0.0, n=8)

    def test_invalid_direction_raises_value_error(self) -> None:
        """Direction must be 'higher' or 'lower' — anything else raises."""
        with pytest.raises(ValueError, match="direction must be 'higher' or 'lower'"):
            ra.run_wilcoxon([1.0], [2.0], direction="sideways")

    def test_mismatched_lengths_raise_value_error(self) -> None:
        """Score arrays must be paired — different lengths raise ValueError."""
        with pytest.raises(ValueError, match="must have the same length"):
            ra.run_wilcoxon([1.0, 1.0], [2.0, 2.0, 2.0])


def _kept(count: int, start: float, step: float) -> list[dict[str, object]]:
    """Return ``count`` kept records whose metric rises by ``step`` from ``start``."""
    return [{"status": "kept", "metric": start + step * (i + 1)} for i in range(count)]


@pytest.fixture
def write_ledger(tmp_path: Path) -> Callable[[str], Path]:
    """Return a writer that stores raw JSONL ``text`` as ``experiments.jsonl`` under ``tmp_path`` and returns its
    path."""

    def _write(text: str) -> Path:
        path = tmp_path / "experiments.jsonl"
        path.write_text(text, encoding="utf-8")
        return path

    return _write


def _jsonl(records: list[dict[str, object]]) -> str:
    """Serialize records as one JSON object per line with a trailing newline."""
    return "\n".join(json.dumps(r) for r in records) + "\n"


class TestMainCLI:
    """End-to-end argparse + JSONL parsing — exit codes and output shape."""

    @pytest.fixture
    def improving_ledger(self, write_ledger: Callable[[str], Path]) -> Path:
        """Write a baseline of 1.0 followed by eight kept iterations, each 0.1 better than the last."""
        return write_ledger(_jsonl([{"status": "baseline", "metric": 1.0}, *_kept(8, 1.0, 0.1)]))

    def test_significant_result_exits_zero(self, improving_ledger: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Strong improvement → exit 0 and JSON with significant=True."""
        exit_code = ra.main(["--jsonl", str(improving_ledger), "--alpha", "0.05", "--direction", "higher"])

        out = json.loads(capsys.readouterr().out.strip())
        assert exit_code == 0
        assert out["significant"] is True
        assert out["n"] == 8

    def test_success_payload_omits_reason_key(self, improving_ledger: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A conclusive run emits exactly the four result keys, with no ``reason``.

        The module docstring documents ``reason`` as present only on the insufficient-data / no-scipy paths, and
        ``retro/SKILL.md`` consumes this line as JSON. Serializing the result unconditionally would leak ``"reason":
        null`` into every success payload and silently widen that published contract.
        """
        ra.main(["--jsonl", str(improving_ledger), "--alpha", "0.05", "--direction", "higher"])

        out = json.loads(capsys.readouterr().out.strip())
        assert set(out) == {"significant", "p_value", "statistic", "n"}

    def test_custom_baseline_label(
        self, write_ledger: Callable[[str], Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The ``--baseline`` flag selects which status string marks the baseline record.

        The ledger starts with a ``control`` record instead of ``baseline``; naming it must make the run conclusive.
        """
        ledger = write_ledger(_jsonl([{"status": "control", "metric": 5.0}, *_kept(8, 5.0, 0.3)]))

        exit_code = ra.main(["--jsonl", str(ledger), "--baseline", "control", "--direction", "higher"])

        out = json.loads(capsys.readouterr().out.strip())
        assert exit_code == 0
        assert out["significant"] is True

    def test_insufficient_data_exits_one_with_reason(
        self, write_ledger: Callable[[str], Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Few kept iterations → exit 1 (not significant) with reason field."""
        ledger = write_ledger(_jsonl([{"status": "baseline", "metric": 1.0}, {"status": "kept", "metric": 1.5}]))

        exit_code = ra.main(["--jsonl", str(ledger)])

        out = json.loads(capsys.readouterr().out.strip())
        assert exit_code == 1
        assert out["significant"] is False
        assert "insufficient data" in out["reason"]

    @pytest.mark.parametrize(
        ("filename", "text", "message"),
        [
            pytest.param("missing.jsonl", "", "missing.jsonl", id="missing-file"),
            pytest.param(
                "experiments.jsonl",
                _jsonl([{"status": "kept", "metric": 1.5}]),
                "no baseline record found",
                id="no-baseline",
            ),
            pytest.param(
                "experiments.jsonl",
                _jsonl([{"status": "baseline", "metric": 1.0}]).rstrip("\n")
                + '\n{"status": "kept", bad}\n'
                + json.dumps({"status": "kept", "metric": 1.2})
                + "\n",
                "malformed JSON",
                id="malformed-middle-line",
            ),
            pytest.param(
                "experiments.jsonl",
                _jsonl([{"status": "baseline", "metric": 1.0}]) + '{"status": "kep\n\n',
                "malformed JSON at line 2",
                id="malformed-line-before-trailing-blank",
            ),
        ],
    )
    def test_input_error_exits_two(
        self,
        write_ledger: Callable[[str], Path],
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        filename: str,
        text: str,
        message: str,
    ) -> None:
        """A missing file, absent baseline record or malformed JSON mid-file exits 2 with an ``error`` message.

        A malformed line that is not the last line (even when only a blank line follows it) is a data error, not a
        tolerated trailing truncation: the streamed reader holds a bad line back until it sees the next line, exactly as
        the earlier whole-file ``readlines`` check did. ``text`` is written to ``experiments.jsonl``; the missing-file
        case points at ``missing.jsonl`` instead.
        """
        write_ledger(text)

        exit_code = ra.main(["--jsonl", str(tmp_path / filename)])

        out = json.loads(capsys.readouterr().out.strip())
        assert exit_code == 2
        assert message in out["error"]

    def test_truncated_trailing_line_is_tolerated(
        self, write_ledger: Callable[[str], Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Trailing partial JSON line is silently dropped; preceding records still parsed.

        A run killed mid-write leaves a half line at the end of the ledger. The seven complete kept records must still
        be counted; the exit code depends on signal strength, so only parsing and the sample size are pinned.
        """
        good = _jsonl([{"status": "baseline", "metric": 1.0}, *_kept(7, 1.0, 0.05)])
        ledger = write_ledger(good + '{"status": "kep')

        exit_code = ra.main(["--jsonl", str(ledger)])

        out = json.loads(capsys.readouterr().out.strip())
        assert exit_code in (0, 1)
        assert out["n"] == 7

    def test_oversized_file_exits_two_before_reading(
        self, write_ledger: Callable[[str], Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A file over the size cap is rejected with an input error instead of being loaded into memory."""
        ledger = write_ledger(_jsonl([{"status": "baseline", "metric": 1.0}, {"status": "kept", "metric": 1.1}]))
        monkeypatch.setattr(ra, "MAX_JSONL_BYTES", 16)

        exit_code = ra.main(["--jsonl", str(ledger)])

        out = json.loads(capsys.readouterr().out.strip())
        assert exit_code == 2
        assert "not a run ledger" in out["error"]
