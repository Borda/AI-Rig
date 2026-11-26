"""Verify offline registration through the Claude emitter's append entrypoint."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

_EMITTER = Path(__file__).resolve().parents[2] / "bin" / "gap_emit.py"


@pytest.fixture
def run(tmp_path: Path) -> Path:
    """Create a Claude review run with a frozen gap identity."""
    run = tmp_path / "run"
    run.mkdir()
    identity = {
        "subject": {"repo": "example/project", "pr": 4, "head": "a" * 40},
        "run": {
            "run_id": "run",
            "kind": "review",
            "harness": "claude",
            "plugin": "oss",
            "plugin_version": "1.0.0",
            "route": "direct",
        },
    }
    (run / "gap-identity.json").write_text(json.dumps(identity), encoding="utf-8")
    return run


def _stage_device_gap(run: Path, hardware_class: str, reason: str, summary: str = "Accelerator tests skipped.") -> None:
    """Stage an emitter observation with an explicit class and device reason."""
    record = {
        "id": "gpu-absent",
        "class": "hardware-absent",
        "owner": "workflow",
        "hardware_class": hardware_class,
        "reason": reason,
        "summary": summary,
        "closable_by": "Rerun with the required accelerator.",
        "evidence": "review-report.md",
        "deduction": 0.05,
    }
    (run / "gaps.jsonl.rec").write_text(json.dumps(record) + "\n", encoding="utf-8")


def test_append_registers_observed_hardware_and_reports_it(run: Path) -> None:
    """Append normalizes staged hardware, registers it, and honors it on a later run."""
    limits = run.parent / ".notes" / "known-limits.json"
    _stage_device_gap(run, "cuda", "No CUDA hardware.")
    args = [sys.executable, str(_EMITTER), "append", "--dir", str(run), "--known-limits", str(limits)]
    first = subprocess.run(args, capture_output=True, text=True, check=True)
    ledger = run / "gaps.jsonl"
    initial = json.loads(ledger.read_text(encoding="utf-8"))
    assert "GAP_KNOWN_LIMIT_REGISTERED=cuda-absent" in first.stdout
    assert (initial["id"], initial["owner"], initial["deduction"]) == ("cuda-absent", "host", 0.05)
    registered = json.loads(limits.read_text(encoding="utf-8"))
    assert registered["limits"][0]["host_class"] == initial["host_class"]
    _stage_device_gap(run, "cuda", "No CUDA hardware.")
    later = subprocess.run(args, capture_output=True, text=True, check=True)
    latest = json.loads(ledger.read_text(encoding="utf-8").splitlines()[-1])
    assert "GAP_KNOWN_LIMIT_REGISTERED" not in later.stdout
    assert latest["known_limit"] is True
    assert latest["deduction"] is None


@pytest.mark.parametrize(
    ("hardware_class", "reason"),
    [
        pytest.param("cuda", "Only a single NVIDIA GPU on this host.", id="single-gpu-is-a-multi-gpu-limit"),
        pytest.param("tpu", "This host has no NVIDIA GPU.", id="cuda-is-not-tpu"),
        pytest.param("mps", "This host has no NVIDIA GPU.", id="cuda-is-not-mps"),
        pytest.param("rocm", "No CUDA GPU.", id="cuda-is-not-rocm"),
    ],
)
def test_append_refuses_evidence_of_a_different_device_class(run: Path, hardware_class: str, reason: str) -> None:
    """An explicit mismatch cannot produce a hardware event or register a waiver."""
    limits = run.parent / ".notes" / "known-limits.json"
    _stage_device_gap(run, hardware_class, reason)
    args = [sys.executable, str(_EMITTER), "append", "--dir", str(run), "--known-limits", str(limits)]

    result = subprocess.run(args, capture_output=True, text=True, check=False)

    assert result.returncode == 1
    assert "GAP_LEDGER=refused" in result.stdout
    assert (run / "gaps.jsonl.rec").exists()
    assert not (run / "gaps.jsonl").exists()
    assert not limits.exists()


def test_generic_gpu_absence_does_not_register_a_vendor_waiver(run: Path) -> None:
    """A declared CUDA check can stay deducted when the observation identifies no vendor."""
    limits = run.parent / ".notes" / "known-limits.json"
    _stage_device_gap(run, "cuda", "No GPU on this host.")
    args = [sys.executable, str(_EMITTER), "append", "--dir", str(run), "--known-limits", str(limits)]

    result = subprocess.run(args, capture_output=True, text=True, check=True)

    record = json.loads((run / "gaps.jsonl").read_text(encoding="utf-8"))
    assert (record["id"], record["deduction"]) == ("cuda-absent", 0.05)
    assert "GAP_KNOWN_LIMIT_REGISTERED" not in result.stdout
    assert not limits.exists()


@pytest.mark.parametrize(
    ("reason", "summary"),
    [
        pytest.param("No accessible NVIDIA GPU.", "Accelerator tests skipped.", id="access-reason"),
        pytest.param("NVIDIA GPU not installed.", "Accelerator tests skipped.", id="installation-reason"),
        pytest.param(
            "This host has no NVIDIA GPU.",
            "CUDA tests skipped: the container was started without --gpus.",
            id="container-configuration-summary",
        ),
        pytest.param(
            "This host has no NVIDIA GPU.",
            "The container cannot see the host's NVIDIA GPU.",
            id="container-visibility-summary",
        ),
    ],
)
def test_append_keeps_deduction_for_access_and_setup_gaps(run: Path, reason: str, summary: str) -> None:
    """Append keeps deductions and refuses registration or a waiver for software-qualified observations."""
    limits = run.parent / ".notes" / "known-limits.json"
    args = [sys.executable, str(_EMITTER), "append", "--dir", str(run), "--known-limits", str(limits)]

    _stage_device_gap(run, "cuda", reason, summary)
    first = subprocess.run(args, capture_output=True, text=True, check=True)
    ledger = run / "gaps.jsonl"
    assert json.loads(ledger.read_text(encoding="utf-8"))["deduction"] == 0.05
    assert "GAP_KNOWN_LIMIT_REGISTERED" not in first.stdout
    assert not limits.exists()

    _stage_device_gap(run, "cuda", "This host has no NVIDIA GPU.")
    seeded = subprocess.run(args, capture_output=True, text=True, check=True)
    assert "GAP_KNOWN_LIMIT_REGISTERED=cuda-absent" in seeded.stdout
    _stage_device_gap(run, "cuda", reason, summary)
    subprocess.run(args, capture_output=True, text=True, check=True)
    latest = json.loads(ledger.read_text(encoding="utf-8").splitlines()[-1])
    assert (latest["reason"], latest["summary"], latest["deduction"]) == (reason, summary, 0.05)
    assert "known_limit" not in latest
