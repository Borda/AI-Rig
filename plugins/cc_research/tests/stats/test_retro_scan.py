"""Exercise deterministic retrospective windows and jump detection through the CLI."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "bin/retro_scan.py"


def _scan(tmp_path: Path, records: list[dict], *flags: str) -> tuple[subprocess.CompletedProcess, Path]:
    """Run the shipped scanner against a real sanitized ledger and output directory."""
    ledger = tmp_path / "experiments-clean.jsonl"
    ledger.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    output = tmp_path / "retro-output"
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--jsonl", str(ledger), "--out-dir", str(output), *flags],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result, output


def test_dead_windows_include_null_deltas_and_flush_final_window(tmp_path: Path) -> None:
    """Null measurements form churn while kept-only tiny deltas form a distinct plateau."""
    records = [
        {"iteration": 0, "status": "baseline", "metric": 1.0},
        {"iteration": 1, "status": "kept", "delta": 0.0001},
        {"iteration": 2, "status": "timeout", "delta": None},
        {"iteration": 3, "status": "no-op", "delta": None},
        {"iteration": 4, "status": "kept", "delta": 1.0},
        {"iteration": 5, "status": "kept", "delta": 0.0001},
        {"iteration": 6, "status": "kept", "delta": 0.0002},
        {"iteration": 7, "status": "kept", "delta": 0.0003},
    ]
    result, output = _scan(tmp_path, records, "--threshold", "0.001")
    assert result.returncode == 0, result.stderr
    dead = json.loads((output / "dead-iters.json").read_text(encoding="utf-8"))
    assert dead["windows"] == [
        {"start": 1, "end": 3, "count": 3, "type": "dead-churn"},
        {"start": 5, "end": 7, "count": 3, "type": "dead-plateau"},
    ]
    assert dead["total_dead"] == 6
    assert dead["total_iterations"] == 7
    assert dead["dead_pct"] == pytest.approx(600 / 7)


@pytest.mark.parametrize(
    ("direction", "delta"),
    [pytest.param("higher", 10.0, id="higher-improves"), pytest.param("lower", -10.0, id="lower-improves")],
)
def test_jump_uses_prior_five_kept_deltas_without_current_sample(tmp_path: Path, direction: str, delta: float) -> None:
    """A large improvement is detected against previous samples rather than diluting its own baseline."""
    records = [{"iteration": 0, "status": "baseline", "metric": 1.0}]
    records += [{"iteration": index, "status": "kept", "delta": 0.001} for index in range(1, 6)]
    records += [{"iteration": 6, "status": "kept", "delta": delta, "commit": "abc123", "files": ["model.py"]}]
    result, output = _scan(tmp_path, records, "--direction", direction)
    assert result.returncode == 0, result.stderr
    jumps = json.loads((output / "suspicious-jumps.json").read_text(encoding="utf-8"))
    assert len(jumps["jumps"]) == 1
    jump = jumps["jumps"][0]
    assert jump["iteration"] == 6
    assert jump["delta"] == delta
    assert jump["severity"] == "HIGH"
    assert jump["commit"] == "abc123"
    assert jump["files"] == ["model.py"]
    assert jump["label"] == "suspicious — investigate"
    assert jump["sigma"] is None


def test_non_improving_jump_and_null_samples_are_not_candidates(tmp_path: Path) -> None:
    """A large regression is not labelled as suspicious improvement and null samples do not fill the window."""
    records = [{"iteration": 0, "status": "baseline", "metric": 1.0}]
    records += [{"iteration": index, "status": "kept", "delta": 0.01} for index in range(1, 6)]
    records += [
        {"iteration": 6, "status": "kept", "delta": -100},
        {"iteration": 7, "status": "kept", "delta": None},
        {"iteration": 8, "status": "timeout", "delta": 1000},
    ]
    result, output = _scan(tmp_path, records)
    assert result.returncode == 0, result.stderr
    jumps = json.loads((output / "suspicious-jumps.json").read_text(encoding="utf-8"))
    assert jumps["jumps"] == []
    assert jumps["kept_samples"] == 6


@pytest.mark.parametrize(
    ("flags", "threshold"),
    [
        pytest.param((), 0.1, id="scale-adjusts-default"),
        pytest.param(("--threshold", "0.001"), 0.001, id="explicit-kept"),
    ],
)
def test_threshold_scale_adjusts_only_unspecified_default(
    tmp_path: Path, flags: tuple[str, ...], threshold: float
) -> None:
    """Explicit user thresholds survive scale advice; absent thresholds use the documented adjustment."""
    records = [{"iteration": 0, "status": "baseline", "metric": 1000.0}]
    records += [{"iteration": index, "status": "kept", "delta": 0.01} for index in range(1, 4)]
    result, output = _scan(tmp_path, records, *flags)
    assert result.returncode == 0, result.stderr
    dead = json.loads((output / "dead-iters.json").read_text(encoding="utf-8"))
    assert dead["threshold"] == threshold
    assert "Threshold advisory" in result.stderr


def test_insufficient_jump_samples_are_reported(tmp_path: Path) -> None:
    """Short histories carry an explicit insufficiency reason instead of an unsupported clean verdict."""
    records = [{"iteration": 0, "status": "baseline", "metric": 1.0}]
    records += [{"iteration": index, "status": "kept", "delta": 1.0} for index in range(1, 6)]
    result, output = _scan(tmp_path, records)
    assert result.returncode == 0, result.stderr
    jumps = json.loads((output / "suspicious-jumps.json").read_text(encoding="utf-8"))
    assert jumps["kept_samples"] == 5
    assert "need ≥6" in jumps["reason"]


@pytest.mark.parametrize(
    ("delta", "severity"),
    [pytest.param(7.0, "MEDIUM", id="two-sigma"), pytest.param(8.0, "HIGH", id="three-sigma")],
)
def test_jump_severity_uses_sample_standard_deviation(tmp_path: Path, delta: float, severity: str) -> None:
    """A nonconstant reference window distinguishes two-sigma and three-sigma improvements."""
    records = [{"iteration": 0, "status": "baseline", "metric": 1.0}]
    records += [{"iteration": index, "status": "kept", "delta": index} for index in range(1, 6)]
    records += [{"iteration": 6, "status": "kept", "delta": delta}]
    result, output = _scan(tmp_path, records)
    assert result.returncode == 0, result.stderr
    jump = json.loads((output / "suspicious-jumps.json").read_text(encoding="utf-8"))["jumps"][0]
    assert jump["severity"] == severity
    assert jump["sigma"] == pytest.approx((delta - 3) / (2.5**0.5))


def test_missing_iteration_breaks_dead_window(tmp_path: Path) -> None:
    """Rows separated by missing iteration numbers cannot form a consecutive three-iteration window."""
    records = [{"iteration": 0, "status": "baseline", "metric": 1.0}]
    records += [{"iteration": index, "status": "no-op", "delta": None} for index in (1, 2, 4, 5, 6)]
    result, output = _scan(tmp_path, records)
    assert result.returncode == 0, result.stderr
    dead = json.loads((output / "dead-iters.json").read_text(encoding="utf-8"))
    assert dead["windows"] == [{"start": 4, "end": 6, "count": 3, "type": "dead-churn"}]
    assert dead["total_dead"] == 3


@pytest.mark.parametrize("delta", [True, float("nan"), float("inf"), "0.1"])
def test_invalid_delta_cannot_produce_scan_summaries(tmp_path: Path, delta: object) -> None:
    """Invalid measurements fail visibly rather than silently becoming dead or suspicious evidence."""
    records = [{"iteration": 0, "status": "baseline", "metric": 1.0}]
    records += [{"iteration": 1, "status": "kept", "delta": delta}]
    result, output = _scan(tmp_path, records)
    assert result.returncode == 2
    assert "finite" in result.stderr
    assert not output.exists()


@pytest.mark.parametrize("threshold", ["0", "-1", "nan", "inf"])
def test_invalid_threshold_is_input_error(tmp_path: Path, threshold: str) -> None:
    """Invalid numeric configuration cannot silently alter dead-iteration classification."""
    result, output = _scan(tmp_path, [{"iteration": 0, "status": "baseline", "metric": 1}], "--threshold", threshold)
    assert result.returncode == 2
    assert "threshold" in result.stderr
    assert not output.exists()
