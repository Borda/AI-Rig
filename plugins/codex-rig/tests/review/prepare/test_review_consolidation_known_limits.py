"""Verify repository and host binding of consolidated known-limit confidence gaps."""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from pathlib import Path

import pytest
from test_review_batches import _batch_output, _load_validator

_REPO = "octo/demo"
_REASON = "The test host lacks a local CUDA GPU."
_ACCOUNTING_ERROR = "review-consolidation-confidence-accounting-invalid"
_GAP_RECORDS_HELPER = Path(__file__).resolve().parents[3] / "shared" / "run_gap_records.py"


@pytest.fixture
def review_run(tmp_path: Path) -> Path:
    """Record a routed review with a separate invoking checkout."""
    run = tmp_path / "run"
    run.mkdir()
    checkout = tmp_path / "invoking"
    checkout.mkdir()
    (run / "review-entry.json").write_text(json.dumps({"repository": str(checkout)}), encoding="utf-8")
    (run / "pr-routing.json").write_text(json.dumps({"base_repo": _REPO}), encoding="utf-8")
    (run / "gates.json").write_text("{}", encoding="utf-8")
    return run


def _write_limits(run: Path, *, repo: str = _REPO, os_name: str | None = None, listed: bool = True) -> Path:
    """Write the known-limits file into the recorded invoking checkout."""
    path = run.parent / "invoking" / ".notes" / "known-limits.json"
    path.parent.mkdir()
    machine = platform.machine().casefold()
    limit = {
        "id": "cuda-absent",
        "class": "hardware-absent",
        "hardware_class": "cuda",
        "reason": _REASON,
        "host_class": {
            "os": os_name or platform.system().casefold(),
            "architecture": {"amd64": "x86_64", "aarch64": "arm64"}.get(machine, machine),
        },
    }
    path.write_text(
        json.dumps({"schema": "known-limits", "schema_version": 1, "repo": repo, "limits": [limit] if listed else []}),
        encoding="utf-8",
    )
    return path


def _capture_scoring_limits(run: Path) -> None:
    """Capture the registry through the shipped scoring preparation command."""
    subprocess.run(
        [sys.executable, str(_GAP_RECORDS_HELPER), "prepare", "--run", str(run)],
        capture_output=True,
        check=True,
        timeout=30,
    )


def _validate_limit(
    run: Path,
    *,
    gap_id: str = "cuda-absent",
    reason: str = _REASON,
    marker: str = "(known limit)",
    historical: int = 0,
    confidence: float = 0.99,
    source_only: bool = False,
) -> None:
    """Validate a scoped waiver alongside one admitted cause that still contributes a deduction."""
    gap = f"{gap_id}: {reason}"
    score = 1 - (historical + 3) / 100
    scoped = [
        {"gap": gap, "status": "unresolved", "rationale": f"(-{historical / 100:.2f}) Observed limitation."},
        {"gap": "Offline receipt uncertainty", "status": "unresolved", "rationale": "(-0.03) Receipts were partial."},
    ]
    profile = {"score": score, "scope": "Source inspection", "gaps": scoped}
    output = run / "qa-output.md"
    output_bytes = _batch_output([], 1, confidence=profile).encode("utf-8")
    output.write_bytes(output_bytes)
    wave = run / "wave.json"
    selected = {"attempt": 1, "output_sha256": hashlib.sha256(output_bytes).hexdigest()}
    wave_bytes = json.dumps({"passes": [{"selected_attempt": 1, "attempts": [selected], "output_path": output.name}]})
    wave.write_bytes(wave_bytes.encode("utf-8"))
    part = {"output_path": output.name, "confidence": profile}
    entry = {"role": "qa-specialist", "confidence": score, "source_parts" if source_only else "final_parts": [part]}
    manifest = {
        "review_topology": "source-only" if source_only else "report-review",
        "passes": [entry],
        "batch_execution": {
            "waves": [{"manifest_path": wave.name, "manifest_sha256": hashlib.sha256(wave.read_bytes()).hexdigest()}]
        },
    }
    labels = [f"qa-specialist {output.name}: {item['gap']} (unresolved) - {item['rationale']}" for item in scoped]
    closures = [
        {"gap": labels[0], "status": "unresolved", "rationale": f"(-0.00) {gap} {marker}"},
        {
            "gap": labels[1],
            "status": "unresolved",
            "rationale": "(-0.01) The additional receipt reduced the uncertainty.",
            "evidence_path": output.name,
        },
    ]
    metadata = {
        "confidence_gaps": labels,
        "confidence_gap_closures": closures,
        "confidence_recovery": {
            "initial_confidence": score,
            "final_confidence": confidence,
            "status": "fair",
            "evidence": [output.name],
            "recovery_actions": ["Admitted the additional receipt."],
            "remaining_limits": [closures[0]["rationale"], closures[1]["rationale"]],
        },
    }
    validator = _load_validator("consolidation_known_limits_validator")
    result = {"confidence": confidence, "checks_failed": []}
    validator._validate_consolidation_confidence(run, manifest, result, metadata)


@pytest.mark.parametrize(
    ("historical", "source_only"),
    [
        pytest.param(0, False, id="zero-deduction-final-part"),
        pytest.param(5, False, id="reduced-final-part"),
        pytest.param(5, True, id="reduced-source-part"),
    ],
)
def test_consolidation_accepts_listed_unresolved_limit_at_zero(
    review_run: Path, historical: int, source_only: bool
) -> None:
    """Waive the listed gap without requiring closure evidence for a historical deduction."""
    _write_limits(review_run)
    _validate_limit(review_run, historical=historical, source_only=source_only)


def test_consolidation_rejects_known_limit_without_a_file(review_run: Path) -> None:
    """The prose marker alone cannot waive either consolidation requirement."""
    with pytest.raises(SystemExit, match=f"^{_ACCOUNTING_ERROR}$"):
        _validate_limit(review_run)
    with pytest.raises(SystemExit, match=r"^review-consolidation-confidence-evidence-invalid$"):
        _validate_limit(review_run, historical=5)


def test_consolidation_rejects_an_unlisted_limit(review_run: Path) -> None:
    """A valid file with no matching entry grants no waiver."""
    _write_limits(review_run, listed=False)
    with pytest.raises(SystemExit, match=f"^{_ACCOUNTING_ERROR}$"):
        _validate_limit(review_run)


def test_consolidation_keeps_a_waiver_captured_before_registry_removal(review_run: Path) -> None:
    """A concurrent removal leaves the run's captured waiver eligible during validation."""
    live_registry = _write_limits(review_run)
    _capture_scoring_limits(review_run)
    live_registry.unlink()

    _validate_limit(review_run, historical=5, source_only=True)


def test_consolidation_rejects_a_waiver_added_after_scoring_capture(review_run: Path) -> None:
    """A concurrently added limit cannot waive an earlier run's deduction."""
    _capture_scoring_limits(review_run)
    _write_limits(review_run)

    with pytest.raises(SystemExit, match=f"^{_ACCOUNTING_ERROR}$"):
        _validate_limit(review_run)


def test_consolidation_rejects_a_limit_listed_on_another_host(review_run: Path) -> None:
    """Another host's known hardware absence does not make this host's gap free."""
    _write_limits(review_run, os_name="other-os")
    with pytest.raises(SystemExit, match=f"^{_ACCOUNTING_ERROR}$"):
        _validate_limit(review_run)


def test_consolidation_rejects_a_limit_listed_for_another_repository(review_run: Path) -> None:
    """Bind the file's repository to the routed review subject."""
    _write_limits(review_run, repo="octo/other")
    with pytest.raises(SystemExit, match=f"^{_ACCOUNTING_ERROR}$"):
        _validate_limit(review_run)


def test_consolidation_accepts_a_mixed_case_registry_repository(review_run: Path) -> None:
    """Registry and routed repository spellings resolve to the same identity."""
    _write_limits(review_run, repo="Octo/Demo")
    _validate_limit(review_run, historical=5)


@pytest.mark.parametrize("reason", ["No TPU on this host.", "No GPU on this host."])
def test_consolidation_rejects_a_reason_without_the_listed_device(review_run: Path, reason: str) -> None:
    """The reported reason must name the class the registry would waive."""
    _write_limits(review_run)
    with pytest.raises(SystemExit, match=f"^{_ACCOUNTING_ERROR}$"):
        _validate_limit(review_run, reason=reason)


def test_consolidation_rejects_a_limit_from_an_invalid_file(review_run: Path) -> None:
    """Malformed configuration leaves the ordinary accounting error in effect."""
    _write_limits(review_run).write_text("{", encoding="utf-8")
    with pytest.raises(SystemExit, match=f"^{_ACCOUNTING_ERROR}$"):
        _validate_limit(review_run)


def test_consolidation_rejects_a_mismatched_total_with_a_listed_limit(review_run: Path) -> None:
    """A waived gap contributes zero and the other deductions still have to reconcile."""
    _write_limits(review_run)
    with pytest.raises(SystemExit, match=f"^{_ACCOUNTING_ERROR}$"):
        _validate_limit(review_run, confidence=0.98)


def test_consolidation_rejects_a_listed_limit_without_the_exact_marker(review_run: Path) -> None:
    """Nearby wording does not serve as the explicit known-limit marker."""
    _write_limits(review_run)
    with pytest.raises(SystemExit, match=f"^{_ACCOUNTING_ERROR}$"):
        _validate_limit(review_run, marker="(known limits)")


def test_consolidation_rejects_a_marker_on_a_non_hardware_gap(review_run: Path) -> None:
    """A process gap stays deductible even when it quotes a listed hardware limit."""
    _write_limits(review_run)
    with pytest.raises(SystemExit, match=f"^{_ACCOUNTING_ERROR}$"):
        _validate_limit(review_run, gap_id="full-suite-not-run", marker=f"cuda-absent: {_REASON} (known limit)")
