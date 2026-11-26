"""Author benchmark claims are judged by the relative effect between base and head measured on this host.

A claim needing an accelerator the host lacks is ``hardware-unavailable`` with exactly that reason; a claim without
samples is ``no-benchmark``; otherwise the verdict depends on the claimed direction and the sample spread, never on the
author's absolute numbers. The host probe reads only operating-system facts.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from _modules import load_module_scoped

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
HELPER_PATH = PLUGIN_ROOT / "shared" / "benchmark_claims.py"
CLAIM = {
    "id": "C1",
    "claim_text": "Batch decode is 40% faster",
    "metric": "seconds per batch",
    "direction": "lower-is-better",
    "author_hw": "RTX 4090",
    "method": {"command": "python bench.py", "repeats": 5, "warmup": 1},
}


@pytest.fixture
def helper(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Load the claim helper by path, as the skill invokes it."""
    return load_module_scoped("benchmark_claims_under_test", HELPER_PATH, monkeypatch)


@pytest.fixture
def cpu_host() -> dict[str, object]:
    """Describe a CPU-only Linux host with its recorded probe evidence."""
    return {
        "os": "Linux",
        "machine": "x86_64",
        "accelerators": [],
        "accelerator_evidence": {"cuda": "nvidia-smi not on PATH", "rocm": "rocm-smi not on PATH"},
    }


@pytest.mark.parametrize(
    ("direction", "base", "head", "verdict"),
    [
        pytest.param("lower-is-better", [2.0, 2.02, 1.98, 2.01], [1.2, 1.21, 1.19, 1.2], "reproduced", id="faster"),
        pytest.param("lower-is-better", [2.0, 2.02, 1.98, 2.01], [2.4, 2.42, 2.38, 2.4], "not-reproduced", id="slower"),
        pytest.param("higher-is-better", [100, 101, 99, 100], [150, 151, 149, 150], "reproduced", id="throughput"),
        pytest.param("lower-is-better", [2.0, 2.6, 1.4, 2.0], [1.9, 2.5, 1.3, 1.9], "not-reproduced", id="noise"),
    ],
)
def test_verdict_follows_direction_and_spread(
    helper: ModuleType, cpu_host: dict, direction: str, base: list, head: list, verdict: str
) -> None:
    """The relative effect must point the claimed way and exceed the larger side's interquartile spread."""
    record = helper.assess_claim({**CLAIM, "direction": direction}, {"base": base, "head": head}, cpu_host)

    assert record["verdict"] == verdict
    assert record["base_samples_n"] == record["head_samples_n"] == 4
    assert record["relative_delta"] == pytest.approx(
        (record["head_median"] - record["base_median"]) / record["base_median"]
    )


def test_missing_accelerator_is_named_exactly(helper: ModuleType, cpu_host: dict) -> None:
    """A CUDA claim on a host without CUDA says only that, even when samples were supplied."""
    record = helper.assess_claim({**CLAIM, "requires": ["cuda"]}, {"base": [1, 1, 1], "head": [1, 1, 1]}, cpu_host)

    assert record["verdict"] == "hardware-unavailable"
    reason = "not reproducible on this host: requires CUDA; host has no CUDA device (cuda: nvidia-smi not on PATH)"
    assert record["reason"] == reason
    assert record["comparable_hardware"]["decision"] == "not-comparable"
    assert record["comparable_hardware"]["basis"] == "accelerator-absent"
    assert record["base_median"] is None


def test_runnable_claim_records_a_slug_basis_and_keeps_the_method_in_the_reason(
    helper: ModuleType, cpu_host: dict
) -> None:
    """A claim this host can run records a slug basis, as the not-comparable ones do, and keeps the method as reason."""
    record = helper.assess_claim(CLAIM, {"base": [1, 1, 1, 1], "head": [1, 1, 1, 1]}, cpu_host)

    hardware = record["comparable_hardware"]
    assert hardware["basis"] == "caller-declared"
    assert hardware["decision"] == "proportional"
    assert "base and head measured on this host with one method" in hardware["reason"]


@pytest.mark.parametrize(
    ("requirement", "reason"),
    [
        pytest.param(
            {"requires": ["tensorrt"]},
            "requires CUDA (TensorRT); host has no CUDA device (cuda: nvidia-smi not on PATH)",
            id="tensorrt",
        ),
        pytest.param({"requires_os": ["windows"]}, "requires Windows; host is Linux", id="os"),
        pytest.param({"requires_arch": ["arm64"]}, "requires arm64; host is x86_64", id="arch"),
    ],
)
def test_other_host_class_names_exactly_that_class(
    helper: ModuleType, cpu_host: dict, requirement: dict, reason: str
) -> None:
    """A claim for another OS, architecture or accelerator class is not reproducible here, and says only why."""
    record = helper.assess_claim({**CLAIM, **requirement}, {"base": [1.0] * 3, "head": [0.5] * 3}, cpu_host)

    assert (record["verdict"], record["reason"]) == ("hardware-unavailable", f"not reproducible on this host: {reason}")


@pytest.mark.parametrize(
    ("required", "verdict"),
    [
        pytest.param("x86_64", "hardware-unavailable", id="x86-claim-on-translated-arm-host"),
        pytest.param("arm64", "reproduced", id="arm-claim-on-translated-arm-host"),
    ],
)
def test_architecture_is_the_native_class_under_translation(helper: ModuleType, required: str, verdict: str) -> None:
    """An x86_64 interpreter translated on Apple silicon is an arm64 host, as the Claude claim helper decides.

    Comparing the interpreter's machine name instead would measure an x86_64 claim under emulation and refuse an arm64
    one; both runtimes must reach the same decision for the same claim and host.
    """
    host = {"os": "Darwin", "machine": "x86_64", "accelerators": [],
            "host_class": {"os": "macos", "machine": "x86_64", "native_arch": "arm64", "accelerators": []}}  # fmt: skip
    record = helper.assess_claim({**CLAIM, "requires_arch": [required]}, {"base": [2.0] * 3, "head": [1.0] * 3}, host)

    assert record["verdict"] == verdict
    if verdict == "hardware-unavailable":
        assert record["reason"] == "not reproducible on this host: requires x86_64; host is arm64"


def test_matching_host_class_records_no_limitation(helper: ModuleType, cpu_host: dict) -> None:
    """A claim whose required classes this host has is measured normally."""
    claim = {**CLAIM, "requires_os": ["linux"], "requires_arch": ["x86_64"], "author_os": "windows"}
    record = helper.assess_claim(claim, {"base": [2.0] * 3, "head": [1.0] * 3}, cpu_host)

    assert (record["verdict"], record["author_os"]) == ("reproduced", "windows")
    assert "host" not in record["reason"]


def test_claim_without_benchmark_or_samples(helper: ModuleType, cpu_host: dict) -> None:
    """No benchmark method is ``no-benchmark``; a method without both sides measured is ``not-run``."""
    assert helper.assess_claim({**CLAIM, "method": None}, None, cpu_host)["verdict"] == "no-benchmark"
    assert helper.assess_claim(CLAIM, {"base": [1.0, 1.0, 1.0]}, cpu_host)["verdict"] == "not-run"


def test_too_few_samples_draw_no_verdict(helper: ModuleType, cpu_host: dict) -> None:
    """Two samples per side cannot establish an effect, however large, so the claim is not run rather than refuted."""
    record = helper.assess_claim(CLAIM, {"base": [2.0, 2.0], "head": [1.0, 1.0]}, cpu_host)

    assert (record["verdict"], record["reason"]) == ("not-run", "fewer than 3 samples per side; no verdict drawn")


@pytest.mark.parametrize(
    ("base", "head"),
    [
        pytest.param([1.0] * 3, [float("nan")] * 3, id="nan-head"),
        pytest.param([1.0, 1.0, float("inf")], [2.0] * 3, id="infinite-base-outlier"),
        pytest.param([1.0] * 3, [float("-inf"), 2.0, 2.0], id="infinite-head-outlier"),
        pytest.param([1.0] * 3, [10**309] * 3, id="sample-exceeds-float-range"),
        pytest.param([0.1] * 3, [1e308] * 3, id="relative-effect-overflow"),
        pytest.param([1e-307] * 3, [0.0, 1.0, 100.0], id="relative-spread-overflow"),
        pytest.param([1e308] * 4, [1.0] * 4, id="median-overflow"),
    ],
)
def test_nonfinite_measurements_draw_no_verdict(
    helper: ModuleType, cpu_host: dict, base: list[float], head: list[float]
) -> None:
    """A non-finite sample or derived statistic cannot establish a measured effect."""
    claim = {**CLAIM, "direction": "higher-is-better"}
    record = helper.assess_claim(claim, {"base": base, "head": head}, cpu_host)

    assert record["verdict"] == "not-run"
    assert "finite" in record["reason"]
    assert (record["base_samples_n"], record["head_samples_n"]) == (len(base), len(head))
    assert (record["base_median"], record["head_median"], record["relative_delta"], record["spread"]) == (None,) * 4


def test_effect_under_half_the_claim_is_not_reproduced(helper: ModuleType, cpu_host: dict) -> None:
    """A real but much smaller effect than the author stated does not reproduce the stated claim."""
    claim = {**CLAIM, "claimed_relative": -0.6}
    record = helper.assess_claim(claim, {"base": [2.0, 2.01, 1.99], "head": [1.6, 1.61, 1.59]}, cpu_host)

    assert record["verdict"] == "not-reproduced"
    assert record["claimed_relative"] == -0.6


def test_fast_head_spread_is_measured_against_the_base(helper: ModuleType, cpu_host: dict) -> None:
    """A much faster head's own small median never inflates the noise band and hides a real effect."""
    record = helper.assess_claim(CLAIM, {"base": [10.0, 10.1, 9.9], "head": [0.1, 0.4, 0.05]}, cpu_host)

    assert record["verdict"] == "reproduced"
    assert record["spread"] == pytest.approx(0.175 / 10.0)


def test_claude_direction_vocabulary_is_accepted(helper: ModuleType, cpu_host: dict) -> None:
    """A claim file written for the Claude review helper, using ``expected_direction``, is read the same way."""
    claim = {key: value for key, value in CLAIM.items() if key != "direction"} | {"expected_direction": "decrease"}

    assert (
        helper.assess_claim(claim, {"base": [2.0] * 3, "head": [1.0] * 3}, cpu_host)["direction"] == "lower-is-better"
    )


def test_cli_writes_one_record_per_claim(tmp_path: Path) -> None:
    """The compare action writes records carrying the claim id, host specification and verdict."""
    claims, samples, out = tmp_path / "claims.json", tmp_path / "samples.json", tmp_path / "out.json"
    claims.write_text(json.dumps([CLAIM, {**CLAIM, "id": "C2", "requires": ["rocm"]}]), encoding="utf-8")
    samples.write_text(json.dumps({"C1": {"base": [2, 2.01, 1.99], "head": [1, 1.01, 0.99]}}), encoding="utf-8")

    done = subprocess.run(
        [
            sys.executable,
            str(HELPER_PATH),
            "compare",
            "--claims",
            str(claims),
            "--samples",
            str(samples),
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    records = json.loads(out.read_text(encoding="utf-8"))

    assert done.returncode == 0, done.stderr
    assert (records[0]["id"], records[0]["verdict"]) == ("C1", "reproduced")
    assert set(records[0]["host_spec"]) == {
        "os", "os_release", "machine", "cpu_model", "cpu_count", "memory_bytes", "python", "accelerators",
        "accelerator_evidence", "host_class",
    }  # fmt: skip
    assert set(records[0]["host_spec"]["host_class"]) == {"os", "machine", "native_arch", "accelerators"}
    if "rocm" not in records[1]["host_spec"]["accelerators"]:
        evidence = records[1]["host_spec"]["accelerator_evidence"]["rocm"]
        expected = f"not reproducible on this host: requires ROCm; host has no ROCm device (rocm: {evidence})"
        assert records[1]["reason"] == expected


def test_invalid_direction_is_rejected(tmp_path: Path) -> None:
    """A claim without a known direction is malformed input, not a verdict."""
    claims, samples, out = tmp_path / "claims.json", tmp_path / "samples.json", tmp_path / "out.json"
    claims.write_text(json.dumps([{**CLAIM, "direction": "faster"}]), encoding="utf-8")
    samples.write_text("{}", encoding="utf-8")

    done = subprocess.run(
        [
            sys.executable,
            str(HELPER_PATH),
            "compare",
            "--claims",
            str(claims),
            "--samples",
            str(samples),
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode == 2
    assert "invalid-direction" in done.stderr
    assert not out.exists()
