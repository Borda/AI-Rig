"""A promoted Codex review or remediation run records its coverage gaps in the shared gap ledger.

The records come from the run's confidence-gap lines, gate rows that did not pass with an environment or process cause
and its single source refusal. Host facts are never recorded. An identical-head rerun carries the earlier run's gaps
instead of restating them, a listed hardware limit is recorded without a deduction, and every failure is reported in the
returned summary instead of raising, so the delivered result is never affected.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from _modules import load_module_scoped
from _platform import FILE_SYMLINKS_AVAILABLE

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = PLUGIN_ROOT / "shared" / "run_gap_records.py"
VERSION = json.loads((PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]
HEAD = "a" * 40
OTHER_HEAD = "b" * 40
PROCESS_GAP = "(-0.03) Parent-only review has no independent specialist assessments."
CUDA_GAP = "(-0.02) CUDA kernels were not exercised: this host has no NVIDIA GPU."
HOST_FACT = "(-0.00) One macOS host ran every check."
#: Per-command author identity, so a runner without a global Git identity can commit; global config stays untouched.
GIT_IDENTITY = (
    "-c",
    "user.name=Gap Fixture",
    "-c",
    "user.email=gap-fixture@example.invalid",
    "-c",
    "commit.gpgsign=false",
)


@pytest.fixture
def records(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Load the shipped gap-record module the way the finalizer imports it."""
    return load_module_scoped("run_gap_records", MODULE_PATH, monkeypatch)


def _write_json(path: Path, payload: object) -> None:
    """Write one JSON fixture file, creating its directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _pr_run(
    checkout: Path,
    run_name: str,
    *,
    head: str = HEAD,
    gaps: list[str] | None = None,
    gates: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> Path:
    """Write a promoted PR review run for ``octo/demo`` pull request 7 under the checkout's artifact root."""
    run = checkout / ".reports" / "codex" / "code-review" / "pr-7" / run_name
    _write_json(run / "pr-routing.json", {"base_repo": "octo/demo", "pr_number": 7, "head_oid": head})
    result_metadata = {
        "confidence_gaps": [PROCESS_GAP, CUDA_GAP, HOST_FACT] if gaps is None else gaps,
        "confidence_gap_closures": [],
        "execution_mode": "parallel",
        "fanout_substituted": False,
        "reviewer_assessments": [{"role": "Main reviewer"}],
        "reviewed_commit_sha": head,
        **(metadata or {}),
    }
    _write_json(run / "result.json", {"status": "pass", "confidence": 0.95, "metadata": result_metadata})
    _write_json(run / "gates.json", gates or {"checks": []})
    _write_json(run / "direct-plan.json", {})
    return run


def _ledger(records: ModuleType, run: Path) -> list[dict[str, Any]]:
    """Load the run's ledger through the schema owner and require it to be valid."""
    ledger = records.gap_ledger.load_ledger(run / "gaps.jsonl")
    assert ledger.errors == []
    return ledger.records


def _git(root: Path, *arguments: str) -> str:
    """Run real Git in ``root`` and return its trimmed output."""
    return subprocess.run(
        ["git", *GIT_IDENTITY, "-C", str(root), *arguments], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.mark.parametrize(
    ("metadata", "marker", "expected"),
    [
        pytest.param({"execution_mode": "parallel"}, None, "direct", id="parallel-direct"),
        pytest.param({"execution_mode": "independent-spawned"}, None, "direct", id="spawned-direct"),
        pytest.param({"execution_mode": "serial"}, None, "direct", id="one-spawned-reviewer-direct"),
        pytest.param(
            {"execution_mode": "serial-fallback", "fanout_substituted": True},
            None,
            "serial-fallback",
            id="parent-only-stays-serial-fallback",
        ),
        pytest.param(
            {"execution_mode": "parallel", "reviewer_assessments": [{"role": "QA specialist (parent substitute)"}]},
            None,
            "parent-substitute",
            id="direct-with-parent-covered-role",
        ),
        pytest.param({"execution_mode": "parallel"}, "batch-inventory.json", "paged", id="batched-paged"),
        pytest.param({"execution_mode": "independent-spawned"}, "inspection-plan.json", "paged", id="historical-paged"),
        pytest.param({"review_status": "unavailable"}, None, "none", id="unavailable-none"),
        pytest.param({"review_status": "closed"}, None, "none", id="closed-none"),
    ],
)
def test_execution_mode_maps_to_ledger_route(
    records: ModuleType, tmp_path: Path, metadata: dict[str, Any], marker: str | None, expected: str
) -> None:
    """Each recorded execution mode maps to one ledger route; paged needs a positive dispatch marker on disk."""
    if marker is not None:
        _write_json(tmp_path / marker, {})

    assert records.review_route(tmp_path, metadata) == expected


def test_unknown_execution_mode_records_nothing(records: ModuleType, tmp_path: Path) -> None:
    """An execution mode the table does not know is reported as skipped and never guessed into a route."""
    run = _pr_run(tmp_path, "run-001", metadata={"execution_mode": "telepathic"})

    summary = records.record_promoted_run(run, "code-review")

    assert summary == {"status": "skipped", "code": "route-unknown:telepathic"}
    assert not (run / "gaps.jsonl").exists()


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        pytest.param("TensorRT engine build untested here.", ("process", None), id="tensorrt-unverified"),
        pytest.param("Multi-GPU sharding never ran.", ("process", None), id="multi-gpu-unverified"),
        pytest.param("ROCm kernels unverified.", ("process", None), id="rocm-unverified"),
        pytest.param("One macOS host ran every check.", (None, None), id="host-os-fact"),
        pytest.param("Native Windows timeout cleanup was not executed here.", (None, None), id="other-os-fact"),
        pytest.param("Python 3.10 only; a single Python version ran.", (None, None), id="python-version-fact"),
        pytest.param("arm64 wheels untested.", (None, None), id="architecture-fact"),
        pytest.param("Local OpenCV cases were skipped.", ("environment-provisionable", None), id="package"),
        pytest.param("Fork code policy kept tests unrun.", ("contribution", None), id="contribution"),
        pytest.param("The user declined the push.", ("human-decision", None), id="human-decision"),
        pytest.param("Hosted CI checks were queued at collection.", ("project", None), id="project"),
        pytest.param("The windows of the sliding iterator overlap.", ("process", None), id="os-word-not-a-host"),
        pytest.param(
            "CUDA tests skipped: torch CPU-only wheel installed, CUDA not available.",
            ("environment-provisionable", None),
            id="cpu-only-wheel-names-cuda",
        ),
        pytest.param(
            "cuda-absent: torch CPU-only wheel installed.",
            ("environment-provisionable", None),
            id="hardware-label-with-provisioning-cause",
        ),
        pytest.param(
            "No CUDA-enabled torch build could be provisioned.",
            ("environment-provisionable", None),
            id="build-not-provisioned",
        ),
        pytest.param("No NVIDIA GPU installed on this runner.", ("hardware-absent", "cuda"), id="gpu-not-installed"),
        pytest.param("CUDA toolkit not installed.", ("environment-provisionable", None), id="toolkit"),
        pytest.param("No CUDA 12 toolkit was installed.", ("process", None), id="versioned-toolkit"),
        pytest.param("torch wheel built without CUDA", ("environment-provisionable", None), id="wheel-build"),
        pytest.param("driver missing", ("environment-provisionable", None), id="driver"),
        pytest.param("No CUDA runtime here.", ("process", None), id="runtime"),
        pytest.param("No ROCm library was found.", ("process", None), id="library"),
        pytest.param("No XPU package was available.", ("process", None), id="accelerator-package"),
        pytest.param("TPU image unavailable.", ("environment-provisionable", None), id="image"),
        pytest.param("No CUDA-enabled torch.", ("process", None), id="torch"),
        pytest.param("No hardware regression on the CUDA path.", ("process", None), id="regression"),
        pytest.param(
            "No GPU. Consider the CUDA runtime separately.",
            ("hardware-absent", "multi-gpu"),
            id="independent-generic-device-absence",
        ),
        pytest.param("cuda-absent: CUDA tests skipped on this host.", ("process", None), id="label-without-absence"),
        pytest.param(
            "cuda-absent: Only TensorRT runners are available.", ("process", None), id="label-with-other-device"
        ),
        pytest.param("`tpu-absent`: TPU tests skipped.", ("process", None), id="quoted-label-without-absence"),
        pytest.param("No NVIDIA GPU checks were run.", ("process", None), id="device-test-scope"),
        pytest.param("CUDA kernels were not exercised on this host.", ("process", None), id="coverage-only"),
        pytest.param("NVIDIA GPU not detected by the shipped runtime.", ("process", None), id="runtime-detection"),
    ],
)
def test_gap_line_classification(records: ModuleType, line: str, expected: tuple[str | None, str | None]) -> None:
    """Gap lines map to a ledger class through the closed tables; host facts map to no class at all."""
    kind = records.classify_gap_text(line)

    assert (kind.gap_class, kind.hardware_class) == expected


@pytest.mark.parametrize(
    "line",
    [
        "0 NVIDIA GPUs on this runner.",
        "CUDA tests skipped: zero GPUs.",
        "Not one NVIDIA GPU is provisioned.",
        "Not one of the NVIDIA GPUs is attached.",
        "0 NVIDIA GPUs in CI.",
        "Not any NVIDIA GPUs on this VM.",
        "0 GPUs and the CUDA tests were skipped.",
        "Not any NVIDIA GPUs are attached.",
        "None of the NVIDIA GPUs are present.",
        "NVIDIA GPUs: 0.",
    ],
)
def test_absent_device_quantity_keeps_deduction(records: ModuleType, line: str) -> None:
    """A zero quantity modifies the device and retains the confidence deduction."""
    drafts, omitted = records.confidence_gap_drafts({"confidence_gaps": [f"(-0.03) {line}"]})

    assert omitted == []
    assert [(draft.id, draft.deduction) for draft in drafts] == [("cuda-absent", 0.03)]


def test_generic_zero_gpu_inventory_limits_multi_gpu_capacity(records: ModuleType) -> None:
    """An explicitly empty GPU inventory proves insufficient capacity without identifying a vendor."""
    kind = records.classify_gap_text("Zero GPUs.")

    assert (kind.gap_class, kind.hardware_class) == ("hardware-absent", "multi-gpu")


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        pytest.param(
            "NVIDIA GPU unavailable because the CUDA driver is not installed.",
            ("environment-provisionable", None),
            id="driver-cause",
        ),
        pytest.param(
            "NVIDIA GPU is not available due to a CPU-only build.",
            ("environment-provisionable", None),
            id="build-cause",
        ),
        pytest.param(
            "NVIDIA GPU unavailable: the driver is missing.", ("environment-provisionable", None), id="colon-cause"
        ),
        pytest.param(
            "NVIDIA GPU unavailable (because the driver is missing).",
            ("environment-provisionable", None),
            id="parenthetical-cause",
        ),
        pytest.param(
            "NVIDIA GPU unavailable here, because the driver is missing.",
            ("environment-provisionable", None),
            id="location-before-cause",
        ),
        pytest.param(
            "NVIDIA GPU unavailable in this environment because the CUDA driver is not installed.",
            ("environment-provisionable", None),
            id="environment-before-cause",
        ),
        pytest.param(
            "NVIDIA GPU unavailable because the driver is missing, and the host has no NVIDIA GPU.",
            ("hardware-absent", "cuda"),
            id="independent-absence",
        ),
        pytest.param(
            "No NVIDIA GPU; the CUDA driver is not installed.",
            ("hardware-absent", "cuda"),
            id="independent-provisioning",
        ),
        pytest.param(
            "ROCm tests skipped: host has no AMD GPU (ROCm not installed).",
            ("environment-provisionable", None),
            id="parenthetical-software-qualification",
        ),
        pytest.param(
            "The VM lacks any ROCm-capable GPU because the runtime is not installed.",
            ("environment-provisionable", None),
            id="cause-qualifies-host-inventory",
        ),
    ],
)
def test_software_cause_binds_to_its_device_report(
    records: ModuleType, line: str, expected: tuple[str, str | None]
) -> None:
    """Only a device report explained by a software cause loses its hardware classification."""
    kind = records.classify_gap_text(line)

    assert (kind.gap_class, kind.hardware_class) == expected


@pytest.mark.parametrize(
    ("line", "hardware"),
    [
        pytest.param("The host has only one TPU.", None, id="other-device"),
        pytest.param("Only one GPU was used because the second shard was disabled.", None, id="utilization"),
        pytest.param("Only one GPU passed the benchmark.", None, id="test-outcome"),
        pytest.param("0 GPUs were used in the test.", None, id="zero-utilization"),
        pytest.param("GPUs: 0 used.", None, id="inventory-shaped-utilization"),
        pytest.param("Only one GPU is available because the driver is not installed.", None, id="software-capacity"),
        pytest.param("This has capacity for 1.0 NVIDIA GPUs.", None, id="fractional-count"),
        pytest.param("Only one GPU is installed on this host.", "multi-gpu", id="installed-capacity"),
        pytest.param("Only one NVIDIA GPU is available.", "multi-gpu", id="available-capacity"),
        pytest.param("The runner has only one GPU.", "multi-gpu", id="host-inventory"),
        pytest.param("Multi-GPU tests skipped: only one GPU.", "multi-gpu", id="capacity-fragment"),
    ],
)
def test_single_device_count_describes_gpu_capacity(records: ModuleType, line: str, hardware: str | None) -> None:
    """Only a count that describes GPU inventory or availability limits multi-GPU coverage."""
    assert records.classify_gap_text(line).hardware_class == hardware


@pytest.mark.parametrize(
    "line",
    [
        pytest.param("(-0.03) CUDA tests skipped: torch CPU-only wheel installed, CUDA not available.", id="cpu-wheel"),
        pytest.param("(-0.02) No CUDA-enabled torch build could be provisioned.", id="build-not-provisioned"),
        pytest.param("(-0.01) opencv not installed so the CUDA path not available", id="package-names-cuda"),
        pytest.param("(-0.02) torch 2.3.0+cpu installed, CUDA not available.", id="cpu-local-version"),
        pytest.param("(-0.02) CUDA not available: NVIDIA driver missing.", id="driver-missing"),
        pytest.param("(-0.02) CUDA unavailable, torch installed from the CPU index.", id="cpu-index"),
    ],
)
def test_provisioning_cause_is_omitted_never_recorded_as_hardware(records: ModuleType, line: str) -> None:
    """A package or build cause that names an accelerator is omitted as unprovisioned, never a hardware gap."""
    drafts, omitted = records.confidence_gap_drafts({"confidence_gaps": [line]})

    assert drafts == []
    assert [entry["reason"] for entry in omitted] == ["provisioning-not-attempted"]


@pytest.mark.parametrize(
    ("line", "hardware"),
    [
        pytest.param(
            "CUDA tests skipped: no NVIDIA GPU on this host, so the NVIDIA driver is not installed.",
            "cuda",
            id="cuda-with-driver",
        ),
        pytest.param("TPU tests skipped: no TPU attached, libtpu missing driver stack.", "tpu", id="tpu-with-stack"),
        pytest.param("GPU not installed on this runner; CUDA tests skipped.", "cuda", id="gpu-not-installed"),
        pytest.param("No NVIDIA GPU is provisioned on this host.", "cuda", id="device-not-provisioned"),
        pytest.param("NVIDIA GPU not detected on this Linux host.", "cuda", id="device-not-detected"),
        pytest.param("No NVIDIA GPU available on this host.", "cuda", id="device-not-available"),
        pytest.param("CUDA tests skipped: no NVIDIA GPU detected.", "cuda", id="negative-detection-suffix"),
        pytest.param("CUDA tests skipped: no GPU found on this host.", "cuda", id="generic-device-found-suffix"),
        pytest.param("Missing NVIDIA drivers, no NVIDIA GPU detected.", "cuda", id="detection-with-driver-cause"),
        pytest.param("torch CPU-only wheel installed, no CUDA device available.", "cuda", id="absence-with-cpu-wheel"),
        pytest.param("CUDA reviewer note: missing GPU.", "cuda", id="missing-generic-device"),
        pytest.param("XPU code path: no devices.", "xpu", id="plural-generic-device"),
        pytest.param("The tests ran without an NVIDIA GPU.", "cuda", id="absence-inside-test-statement"),
        pytest.param("No GPU present on this host; CUDA tests skipped.", "cuda", id="device-not-present"),
        pytest.param("CUDA tests skipped: no AMD GPU on this host.", "rocm", id="named-device-decides-class"),
        pytest.param("CUDA path not exercised because there is no NVIDIA GPU.", "cuda", id="because-clause"),
        pytest.param("CUDA tests were skipped since this machine has no NVIDIA GPU.", "cuda", id="machine-clause"),
        pytest.param("Skipped CUDA tests (no NVIDIA GPU).", "cuda", id="parenthesized-absence"),
        pytest.param("CUDA kernels untested, no NVIDIA GPU available.", "cuda", id="comma-clause"),
        pytest.param("CUDA tests skipped - no NVIDIA GPU on this runner.", "cuda", id="dash-clause"),
        pytest.param("No CUDA GPU (confirmed by hardware inventory).", "cuda", id="inventory-confirmation"),
        pytest.param("CUDA checks skipped—this runner has no NVIDIA GPU.", "cuda", id="unspaced-dash"),
        pytest.param(
            "This host provides TensorRT remotely and lacks a local CUDA GPU.", "cuda", id="joined-host-observation"
        ),
        pytest.param("MPS device is absent here.", "mps", id="predicate-absence"),
        pytest.param("Only a single NVIDIA GPU on this host.", "multi-gpu", id="single-device-capacity"),
        pytest.param("NPU unavailable on this machine.", "npu", id="device-availability-predicate"),
        pytest.param("No TensorRT hardware.", "tensorrt", id="accelerator-hardware-noun"),
        pytest.param("There are no multiple GPUs on this runner.", "multi-gpu", id="existential-absence"),
        pytest.param("CUDA tests skipped: no NVIDIA GPU in CI.", "cuda", id="ci-location"),
        pytest.param("CUDA tests skipped: no NVIDIA GPU in this environment.", "cuda", id="environment-location"),
        pytest.param("CUDA tests skipped: no NVIDIA GPU on GitHub Actions.", "cuda", id="hosted-location"),
        pytest.param("CUDA tests skipped: no GPU on this VM.", "cuda", id="generic-device-on-vm"),
        pytest.param("There is no NVIDIA GPU and the CUDA tests were skipped.", "cuda", id="joined-coverage"),
        pytest.param("No NVIDIA GPU so CUDA tests were skipped.", "cuda", id="trailing-consequence"),
        pytest.param("No NVIDIA GPU available to run the CUDA tests.", "cuda", id="purpose-qualifier"),
        pytest.param("CUDA kernels not exercised: no NVIDIA GPU here or in CI.", "cuda", id="joined-locations"),
        pytest.param("CUDA tests skipped: this VM has no NVIDIA GPU.", "cuda", id="vm-subject"),
        pytest.param("CUDA tests skipped: the laptop has no NVIDIA GPU.", "cuda", id="laptop-subject"),
        pytest.param("CUDA tests skipped: this workstation has no NVIDIA GPU.", "cuda", id="workstation-subject"),
        pytest.param("CUDA tests skipped: the CI worker has no NVIDIA GPU.", "cuda", id="worker-subject"),
        pytest.param("CUDA tests skipped: the review environment has no NVIDIA GPU.", "cuda", id="environment-subject"),
        pytest.param("CUDA tests skipped because the server has no NVIDIA GPU.", "cuda", id="server-subject"),
        pytest.param("ROCm tests skipped: this node has no AMD GPU.", "rocm", id="rocm-node-subject"),
        pytest.param("Multi-GPU tests skipped: single GPU host.", "multi-gpu", id="single-gpu-host"),
        pytest.param("Multi-GPU tests skipped: a second GPU is not available.", "multi-gpu", id="second-gpu"),
        pytest.param("The CUDA GPU is physically absent.", "cuda", id="adverb-in-predicate"),
        pytest.param("The host does not have a CUDA GPU.", "cuda", id="negated-possession"),
        pytest.param("There are zero CUDA GPUs on this host.", "cuda", id="zero-count"),
        pytest.param("CUDA not available on this host.", "cuda", id="bare-accelerator"),
        pytest.param("no ROCm-capable GPU on this host", "rocm", id="hyphenated-device"),
        pytest.param("Devices here: XPU none.", "xpu", id="none-predicate"),
        pytest.param("NVIDIA GPU not found.", "cuda", id="not-found-predicate"),
        pytest.param("CUDA tests skipped: this VM has no NVIDIA GPU provisioned.", "cuda", id="vm-provisioning"),
        pytest.param("This server has no available NVIDIA GPU.", "cuda", id="availability-modifier"),
        pytest.param("There is no discrete NVIDIA GPU.", "cuda", id="device-modifier"),
        pytest.param("No CUDA hardware in the CI build job.", "cuda", id="build-job-location"),
        pytest.param(
            "NVIDIA GPU unavailable (driver installation was also skipped).", "cuda", id="parenthetical-provisioning"
        ),
        pytest.param("NVIDIA GPU: no.", "cuda", id="inventory-value"),
        pytest.param("Absent: a CUDA GPU.", "cuda", id="absence-heading"),
    ],
)
def test_stated_device_absence_keeps_hardware_deduction(records: ModuleType, line: str, hardware: str) -> None:
    """A stated device absence takes precedence over provisioning wording and host facts."""
    drafts, omitted = records.confidence_gap_drafts({"confidence_gaps": [f"(-0.02) {line}"]})

    assert [(draft.id, draft.gap_class, draft.deduction) for draft in drafts] == [
        (f"{hardware}-absent", "hardware-absent", 0.02)
    ]
    assert omitted == []


@pytest.mark.parametrize(
    "line",
    [
        "CUDA tests skipped: No NVIDIA GPU visible because PyTorch was built for CPU.",
        "NVIDIA GPU not detected with the distributed binary.",
        "cuda-absent: No CUDA hardware recognized by the selected package.",
        "No NVIDIA GPU (PyTorch was built for CPU).",
        "NVIDIA GPU not detected (the distributed binary targets the host processor).",
        "The broken runtime reports no NVIDIA GPU.",
    ],
)
def test_build_qualified_device_report_stays_a_process_gap(records: ModuleType, tmp_path: Path, line: str) -> None:
    """A package-qualified detection report keeps its deduction and never creates a hardware limit."""
    run = _pr_run(tmp_path, "run-001", gaps=[f"(-0.03) {line}"])

    summary = records.record_promoted_run(run, "code-review")

    assert (summary["status"], summary["appended"], summary["dropped"]) == ("recorded", 1, [])
    assert [(item["class"], item["deduction"]) for item in _ledger(records, run)] == [("process", 0.03)]
    assert not (tmp_path / ".notes" / "known-limits.json").exists()


def test_accelerator_named_without_absence_keeps_its_own_class(records: ModuleType) -> None:
    """A line naming CUDA without stating absent hardware is classified by its other wording, not as process."""
    drafts, omitted = records.confidence_gap_drafts({"confidence_gaps": ["(-0.02) CUDA path not covered by CI."]})

    assert [(draft.gap_class, draft.hardware_class, draft.deduction) for draft in drafts] == [("project", None, 0.02)]
    assert omitted == []


def test_provisioning_cause_is_never_registered_as_a_known_limit(records: ModuleType, tmp_path: Path) -> None:
    """A promoted review with a CPU-only wheel line records no hardware gap and writes no known limit."""
    line = "(-0.03) CUDA tests skipped: torch CPU-only wheel installed, CUDA not available."
    run = _pr_run(tmp_path, "run-001", gaps=[line])

    summary = records.record_promoted_run(run, "code-review")

    assert (summary["status"], summary["appended"]) == ("recorded", 0), summary
    assert [entry["reason"] for entry in summary["not_recorded"]] == ["provisioning-not-attempted"]
    assert "known_limits_registered" not in summary
    assert not (run / "gaps.jsonl").exists()
    assert not (tmp_path / ".notes" / "known-limits.json").exists()


@pytest.mark.parametrize(
    "line",
    [
        "PyTorch was built for CPU, so CUDA tests skipped: no NVIDIA GPU.",
        "CUDA tests skipped because torch has no CUDA support: no NVIDIA GPU.",
        "The conda env pins pytorch-cpu; CUDA tests skipped: no NVIDIA GPU.",
        "This host has a CUDA GPU. No CUDA GPU.",
        "CUDA tests skipped: no NVIDIA GPU detected.",
        "CUDA tests skipped: no GPU found on this host.",
    ],
)
def test_promotion_keeps_deduction_when_absence_follows_free_text(
    records: ModuleType, tmp_path: Path, line: str
) -> None:
    """A trailing device statement records a deduction without qualifying the whole reason for a waiver."""
    run = _pr_run(tmp_path, "run-001", gaps=[f"(-0.03) {line}"])

    summary = records.record_promoted_run(run, "code-review")

    assert (summary["status"], summary["appended"]) == ("recorded", 1)
    [record] = _ledger(records, run)
    assert (record["id"], record["deduction"]) == ("cuda-absent", 0.03)
    assert "known_limits_registered" not in summary
    assert not (tmp_path / ".notes" / "known-limits.json").exists()


@pytest.mark.parametrize(
    "line",
    [
        pytest.param("(-0.04) implementation-challenge-not-run: the user declined the challenge.", id="literal-id"),
        pytest.param("(-0.04) Implementation challenge was not run: approval denied.", id="documented-wording"),
    ],
)
def test_unrun_implementation_challenge_is_a_deducted_process_gap(records: ModuleType, line: str) -> None:
    """The unrun challenge keeps its canonical id, class process and deduction, even when the user declined it."""
    drafts, omitted = records.confidence_gap_drafts({"confidence_gaps": [line]})

    assert [(draft.id, draft.gap_class, draft.deduction) for draft in drafts] == [
        ("implementation-challenge-not-run", "process", 0.04)
    ]
    assert omitted == []


def test_repository_spelling_is_one_identity(records: ModuleType, tmp_path: Path) -> None:
    """A PR receipt spelling ``Octo/Demo`` records ``octo/demo`` and the registry of ``octo/demo`` applies."""
    _write_json(
        tmp_path / ".notes" / "known-limits.json",
        {
            "schema": "known-limits",
            "schema_version": 1,
            "repo": "octo/demo",
            "limits": [
                {
                    "id": "cuda-absent",
                    "class": "hardware-absent",
                    "hardware_class": "cuda",
                    "reason": "No NVIDIA GPU here.",
                    "host_class": records.gap_ledger.host_class_fields(records.gap_ledger.current_host_class()),
                }
            ],
        },
    )
    run = _pr_run(tmp_path, "run-001", gaps=["(-0.00) cuda-absent: No NVIDIA GPU here. (known limit)"])
    _write_json(run / "pr-routing.json", {"base_repo": "Octo/Demo", "pr_number": 7, "head_oid": HEAD})

    summary = records.record_promoted_run(run, "code-review")

    assert summary["status"] == "recorded", summary
    written = records.gap_ledger.latest_by_id(_ledger(records, run))
    assert {record["subject"]["repo"] for record in written.values()} == {"octo/demo"}
    assert written["cuda-absent"]["known_limit"] is True


def test_promoted_pr_review_records_its_gaps(records: ModuleType, tmp_path: Path) -> None:
    """Record confidence gaps and skipped provisioning, while omitting host facts and code failures."""
    gates = {
        "checks": [
            {"id": "tests", "status": "fail", "cause": "missing-python-package", "missing_modules": ["cv2"]},
            {"id": "lint", "status": "fail", "cause": "command-failed"},
        ]
    }
    run = _pr_run(tmp_path, "run-001", gates=gates)

    summary = records.record_promoted_run(run, "code-review")

    assert summary["status"] == "recorded", summary
    assert (summary["appended"], summary["carried"]) == (3, 0)
    assert [(entry["reason"], entry["text"]) for entry in summary["not_recorded"]] == [
        ("host-fact", "One macOS host ran every check."),
    ]
    written = {record["id"]: record for record in _ledger(records, run)}
    assert set(written) == {
        records.text_gap_id("Parent-only review has no independent specialist assessments."),
        "cuda-absent",
        "provisioning-not-attempted-tests",
    }
    skipped = written["provisioning-not-attempted-tests"]
    assert (skipped["class"], skipped["owner"], skipped["evidence"]) == ("process", "workflow", "gates.json")
    identity = {key: written["cuda-absent"][key] for key in ("subject", "run")}
    assert identity == {
        "subject": {"repo": "octo/demo", "pr": 7, "head": HEAD},
        "run": {
            "run_id": "pr-7-run-001",
            "kind": "review",
            "harness": "codex",
            "plugin": "codex-rig",
            "plugin_version": VERSION,
            "route": "direct",
        },
    }
    assert (written["cuda-absent"]["deduction"], written["cuda-absent"]["hardware_class"]) == (0.02, "cuda")


def test_package_gap_is_recorded_only_after_provisioning(records: ModuleType, tmp_path: Path) -> None:
    """A package still missing after a provisioned environment served the gates is a workflow gap with its evidence."""
    gates = {
        "checks": [{"id": "tests", "status": "fail", "cause": "missing-python-package", "missing_modules": ["cv2"]}],
        "source": {"project_env": "/env"},
    }
    run = _pr_run(tmp_path, "run-001", gaps=["(-0.02) OpenCV cases were skipped: cv2 not importable."], gates=gates)

    summary = records.record_promoted_run(run, "code-review")

    assert summary["status"] == "recorded"
    assert summary["not_recorded"] == [
        {"reason": "requires-gate-evidence", "text": "OpenCV cases were skipped: cv2 not importable."}
    ]
    written = {record["id"]: record for record in _ledger(records, run)}
    assert set(written) == {"provisioning-failed-opencv-python"}
    gate = written["provisioning-failed-opencv-python"]
    assert (gate["class"], gate["owner"], gate["evidence"]) == ("environment-provisionable", "workflow", "gates.json")
    assert gate["summary"] == "The tests gate requires opencv-python, still missing after provisioning."


def test_gate_refusal_is_one_record_and_provisioning_is_named(records: ModuleType) -> None:
    """Refused gate rows add no per-gate record; the refusal is recorded once and provisioning evidence is named."""
    gates = {
        "checks": [
            {"id": "tests", "status": "fail", "cause": "missing-python-package", "missing_modules": ["cv2"]},
            {"id": "lint", "status": "refused", "cause": "source-refused"},
            {"id": "types", "status": "refused", "cause": "source-refused"},
        ],
        "checks_refused": ["lint", "types"],
        "refusal": {"cause": "source-dirty", "reason": "git status was not empty"},
        "source": {"project_env": "/env"},
    }

    drafts, omitted = records.gate_drafts(gates)

    assert [draft.id for draft in drafts] == ["provisioning-failed-opencv-python", "pr-code-not-executed"]
    assert omitted == []
    assert drafts[1].summary == "Gates refused before launch (source-dirty): lint, types. git status was not empty"


def test_identical_head_rerun_carries_instead_of_restating(records: ModuleType, tmp_path: Path) -> None:
    """A rerun on the same head and version carries every earlier gap, restates none and closes a dropped one."""
    first = _pr_run(tmp_path, "run-001")
    records.record_promoted_run(first, "code-review")
    second = _pr_run(tmp_path, "run-002", gaps=[PROCESS_GAP.upper(), HOST_FACT])

    summary = records.record_promoted_run(second, "code-review")

    assert summary["status"] == "recorded", summary
    assert (summary["carried"], summary["appended"], summary["closed"]) == (2, 0, 1)
    assert summary["carried_from"] == "pr-7-run-001"
    written = _ledger(records, second)
    carried = [record for record in written if record.get("status", "open") == "open"]
    assert {record["carried_from"] for record in carried} == {"pr-7-run-001"}
    assert {record["id"] for record in carried} == {record["id"] for record in _ledger(records, first)}
    latest = records.gap_ledger.latest_by_id(written)
    assert latest["cuda-absent"]["status"] == "closed"
    assert all(record["run"]["run_id"] == "pr-7-run-002" for record in written)


def test_changed_head_records_fresh_gaps(records: ModuleType, tmp_path: Path) -> None:
    """A rerun on a different head cannot carry: every gap is recorded fresh for the new head."""
    records.record_promoted_run(_pr_run(tmp_path, "run-001"), "code-review")
    second = _pr_run(tmp_path, "run-002", head=OTHER_HEAD)

    summary = records.record_promoted_run(second, "code-review")

    assert (summary["carried"], summary["appended"]) == (0, 2)
    assert all("carried_from" not in record for record in _ledger(records, second))


def test_second_promotion_appends_nothing(records: ModuleType, tmp_path: Path) -> None:
    """Recording the same promoted run again leaves its ledger byte-identical."""
    run = _pr_run(tmp_path, "run-001")
    records.record_promoted_run(run, "code-review")
    before = (run / "gaps.jsonl").read_bytes()

    summary = records.record_promoted_run(run, "code-review")

    assert (summary["status"], summary["appended"], summary["closed"]) == ("recorded", 0, 0)
    assert (run / "gaps.jsonl").read_bytes() == before


@pytest.mark.parametrize(
    ("reason", "eligible"),
    [
        pytest.param("No NVIDIA GPU here.", True, id="device-absence"),
        pytest.param("No NVIDIA GPU here; the CUDA driver is not installed.", False, id="provisioning-reason"),
    ],
)
def test_only_eligible_registry_reasons_replace_the_observation(
    records: ModuleType, tmp_path: Path, reason: str, eligible: bool
) -> None:
    """A valid limit clears the deduction and an invalid registry leaves the observation appendable."""
    limits = {
        "schema": "known-limits",
        "schema_version": 1,
        "repo": "octo/demo",
        "limits": [
            {
                "id": "cuda-absent",
                "class": "hardware-absent",
                "hardware_class": "cuda",
                "reason": reason,
                "host_class": records.gap_ledger.host_class_fields(records.gap_ledger.current_host_class()),
            }
        ],
    }
    _write_json(tmp_path / ".notes" / "known-limits.json", limits)
    observation = "This host has no NVIDIA GPU."
    run = _pr_run(tmp_path, "run-001", gaps=[f"(-0.00) cuda-absent: {observation} (known limit)"])

    summary = records.record_promoted_run(run, "code-review")

    assert summary["status"] == "recorded", summary
    written = records.gap_ledger.latest_by_id(_ledger(records, run))
    assert "gpu-absent" not in written
    limit = written["cuda-absent"]
    expected = (True, None, reason) if eligible else (None, 0.0, observation)
    assert (limit.get("known_limit"), limit["deduction"], limit["reason"]) == expected
    assert any(note.startswith("known-limits-ignored:") for note in summary["notes"]) is not eligible


def test_deleted_registry_entry_records_the_hardware_gap_fresh_with_its_deduction(
    records: ModuleType, tmp_path: Path
) -> None:
    """A known limit the maintainer removed is not carried; the rerun records it fresh, deducted, and registers it.

    The earlier run stored the limit without a deduction, so carrying it unflagged would lose the deduction: the rerun's
    own observation decides instead.
    """
    path = tmp_path / ".notes" / "known-limits.json"
    entry = {
        "id": "cuda-absent",
        "class": "hardware-absent",
        "hardware_class": "cuda",
        "reason": "No NVIDIA GPU here.",
        "host_class": records.gap_ledger.host_class_fields(records.gap_ledger.current_host_class()),
    }
    _write_json(path, {"schema": "known-limits", "schema_version": 1, "repo": "octo/demo", "limits": [entry]})
    first = _pr_run(tmp_path, "run-001", gaps=["(-0.00) cuda-absent: No NVIDIA GPU here. (known limit)"])
    assert records.record_promoted_run(first, "code-review")["status"] == "recorded"
    assert records.gap_ledger.latest_by_id(_ledger(records, first))["cuda-absent"]["known_limit"] is True
    _write_json(path, {"schema": "known-limits", "schema_version": 1, "repo": "octo/demo", "limits": []})
    second = _pr_run(tmp_path, "run-002")

    summary = records.record_promoted_run(second, "code-review")

    assert summary["status"] == "recorded", summary
    cuda = records.gap_ledger.latest_by_id(_ledger(records, second))["cuda-absent"]
    assert ("carried_from" in cuda, "known_limit" in cuda, cuda["deduction"]) == (False, False, 0.02)
    assert summary["known_limits_registered"] == ["cuda-absent"]


@pytest.fixture
def cuda_limit(records: ModuleType, tmp_path: Path) -> str:
    """List the current host's CUDA limit with a runner prefix in its reason."""
    reason = "Test runner: no NVIDIA GPU."
    entry = {
        "id": "cuda-absent",
        "class": "hardware-absent",
        "hardware_class": "cuda",
        "reason": reason,
        "host_class": records.gap_ledger.host_class_fields(records.gap_ledger.current_host_class()),
    }
    _write_json(
        tmp_path / ".notes" / "known-limits.json",
        {"schema": "known-limits", "schema_version": 1, "repo": "octo/demo", "limits": [entry]},
    )
    return reason


def test_documented_known_limit_line_preserves_waiver(records: ModuleType, tmp_path: Path, cuda_limit: str) -> None:
    """Strip the presentation ID and marker before checking the registry's full reason."""
    run = _pr_run(tmp_path, "run-001", gaps=[f"(-0.00) cuda-absent: {cuda_limit} (known limit)"])

    summary = records.record_promoted_run(run, "code-review")

    assert summary["status"] == "recorded", summary
    (record,) = _ledger(records, run)
    assert (record.get("known_limit"), record["deduction"], record["reason"]) == (True, None, cuda_limit)


def test_known_limit_marker_allows_final_punctuation(records: ModuleType, tmp_path: Path, cuda_limit: str) -> None:
    """A period after the confidence marker leaves the observed hardware reason eligible for the waiver."""
    run = _pr_run(tmp_path, "run-001", gaps=["(-0.00) cuda-absent: Test runner: no NVIDIA GPU (known limit)."])

    summary = records.record_promoted_run(run, "code-review")

    assert summary["status"] == "recorded", summary
    (record,) = _ledger(records, run)
    assert (record.get("known_limit"), record["deduction"], record["reason"]) == (True, None, cuda_limit)


def test_latest_process_event_has_the_current_deduction(records: ModuleType, tmp_path: Path) -> None:
    """A changed deduction supersedes a carried event even when the gap ID and class stay the same."""
    first = _pr_run(tmp_path, "run-001", gaps=["(-0.04) full-suite-not-run: Full suite was not run."])
    assert records.record_promoted_run(first, "code-review")["appended"] == 1
    second = _pr_run(tmp_path, "run-002", gaps=["(-0.02) full-suite-not-run: Full suite was not run."])

    summary = records.record_promoted_run(second, "code-review")

    assert (summary["status"], summary["carried"], summary["appended"]) == ("recorded", 1, 1)
    latest = records.gap_ledger.latest_by_id(_ledger(records, second))["full-suite-not-run"]
    assert latest["deduction"] == 0.02


def test_latest_hardware_event_records_a_new_waiver(records: ModuleType, tmp_path: Path, cuda_limit: str) -> None:
    """A current qualifying waiver supersedes an earlier observation that kept a deduction."""
    first = _pr_run(tmp_path, "run-001", gaps=["(-0.03) NVIDIA GPU not detected."])
    assert records.record_promoted_run(first, "code-review")["appended"] == 1
    second = _pr_run(tmp_path, "run-002", gaps=[f"(-0.00) cuda-absent: {cuda_limit} (known limit)"])

    summary = records.record_promoted_run(second, "code-review")

    assert (summary["status"], summary["carried"], summary["appended"]) == ("recorded", 1, 1)
    latest = records.gap_ledger.latest_by_id(_ledger(records, second))["cuda-absent"]
    assert (latest.get("known_limit"), latest["deduction"]) == (True, None)


def test_concurrent_limit_registration_preserves_reported_deduction(records: ModuleType, tmp_path: Path) -> None:
    """A limit registered after scoring cannot retroactively lower the run's reported deduction."""
    run = _pr_run(tmp_path, "run-001", gaps=["(-0.06) This host has no NVIDIA GPU."])
    assert records.main(["prepare", "--run", str(run)]) == 0
    concurrent = _pr_run(tmp_path, "run-002", gaps=[CUDA_GAP], head=OTHER_HEAD)
    assert records.record_promoted_run(concurrent, "code-review")["known_limits_registered"] == ["cuda-absent"]

    summary = records.record_promoted_run(run, "code-review")

    assert summary["status"] == "recorded", summary
    (record,) = _ledger(records, run)
    assert (record.get("known_limit"), record["deduction"]) == (None, 0.06)


def test_limit_removal_preserves_scored_waiver(records: ModuleType, tmp_path: Path, cuda_limit: str) -> None:
    """Finalize against the registry observed at scoring even after its listed limit is removed."""
    run = _pr_run(tmp_path, "run-001", gaps=[f"(-0.00) cuda-absent: {cuda_limit} (known limit)"])
    assert records.main(["prepare", "--run", str(run)]) == 0
    (tmp_path / records.KNOWN_LIMITS_PATH).unlink()

    summary = records.record_promoted_run(run, "code-review")

    assert summary["status"] == "recorded", summary
    (record,) = _ledger(records, run)
    assert (record.get("known_limit"), record["deduction"]) == (True, None)


def test_scoped_confidence_uses_current_limit_rationale(records: ModuleType, tmp_path: Path, cuda_limit: str) -> None:
    """For a scoped review the current deduction comes from the matching rationale, separate from its prior weight."""
    gap = f"qa qa-output.md: cuda-absent: {cuda_limit} (unresolved) - (-0.03) Earlier deduction."
    closure = {"gap": gap, "status": "unresolved", "rationale": f"(-0.00) cuda-absent: {cuda_limit} (known limit)."}
    run = _pr_run(tmp_path, "run-001", gaps=[gap], metadata={"confidence_gap_closures": [closure]})

    summary = records.record_promoted_run(run, "code-review")

    assert summary["status"] == "recorded", summary
    (record,) = _ledger(records, run)
    assert (record.get("known_limit"), record["deduction"], record["reason"]) == (True, None, cuda_limit)


def test_unmarked_zero_deduction_does_not_report_a_waiver(records: ModuleType, tmp_path: Path, cuda_limit: str) -> None:
    """A zero contribution from a different accounting rule is not a known-limit claim."""
    run = _pr_run(tmp_path, "run-001", gaps=[f"(-0.00) cuda-absent: {cuda_limit}"])

    summary = records.record_promoted_run(run, "code-review")

    assert summary["status"] == "recorded", summary
    (record,) = _ledger(records, run)
    assert (record.get("known_limit"), record["deduction"]) == (None, 0)


@pytest.mark.parametrize("waiver_first", [True, False])
def test_waiver_leaves_another_observations_deduction(
    records: ModuleType, tmp_path: Path, cuda_limit: str, waiver_first: bool
) -> None:
    """A waiver leaves an independently deducted observation of the same device intact in either order."""
    deducted = "NVIDIA GPU not detected."
    lines = [f"(-0.00) cuda-absent: {cuda_limit} (known limit)", f"(-0.03) {deducted}"]
    run = _pr_run(tmp_path, "run-001", gaps=lines if waiver_first else lines[::-1])

    summary = records.record_promoted_run(run, "code-review")

    assert summary["status"] == "recorded", summary
    (record,) = _ledger(records, run)
    assert (record.get("known_limit"), record["deduction"], record["reason"]) == (None, 0.03, deducted)
    assert record["summary"] == deducted


def test_fresh_hardware_deduction_supersedes_carried_waiver(
    records: ModuleType, tmp_path: Path, cuda_limit: str
) -> None:
    """An earlier run's waived absence leaves the new run's deduction intact across carry and retry."""
    first = _pr_run(tmp_path, "run-001", gaps=[f"(-0.00) cuda-absent: {cuda_limit} (known limit)"])
    assert records.record_promoted_run(first, "code-review")["status"] == "recorded"
    assert _ledger(records, first)[0]["known_limit"] is True
    second = _pr_run(tmp_path, "run-002", gaps=["(-0.03) NVIDIA GPU not detected."])

    summary = records.record_promoted_run(second, "code-review")

    assert summary["status"] == "recorded", summary
    record = records.gap_ledger.latest_by_id(_ledger(records, second))["cuda-absent"]
    assert (record.get("known_limit"), record.get("carried_from"), record["deduction"]) == (None, None, 0.03)
    assert records.record_promoted_run(second, "code-review")["appended"] == 0


def test_unwieldy_lines_do_not_sink_the_batch(records: ModuleType, tmp_path: Path) -> None:
    """Normalize punctuated gate names and shorten long multiline descriptions to one line."""
    long_gap = "(-0.05) Evidence was lost\nacross lines " + "word " * 120
    gates = {"checks": [{"id": "Tests!", "status": "timeout", "cause": "timeout"}]}
    run = _pr_run(tmp_path, "run-001", gaps=[long_gap], gates=gates)

    summary = records.record_promoted_run(run, "code-review")

    assert summary["status"] == "recorded", summary
    assert summary["dropped"] == []
    by_id = {record["id"]: record for record in _ledger(records, run)}
    assert set(by_id) == {records.text_gap_id(long_gap.removeprefix("(-0.05) ")), "gate-tests-timeout"}
    assert by_id["gate-tests-timeout"]["evidence"] == "gates.json"
    written = by_id[records.text_gap_id(long_gap.removeprefix("(-0.05) "))]
    assert "\n" not in written["summary"]
    assert len(written["summary"]) == records.gap_ledger.MAX_TEXT
    assert written["deduction"] == 0.05


@pytest.mark.skipif(not FILE_SYMLINKS_AVAILABLE, reason="this host does not permit file symlinks")
def test_symlinked_ledger_is_reported_without_raising(records: ModuleType, tmp_path: Path) -> None:
    """A planted ledger link is refused and reported; its target is never written."""
    run = _pr_run(tmp_path, "run-001")
    target = tmp_path / "elsewhere.jsonl"
    target.write_text("", encoding="utf-8")
    (run / "gaps.jsonl").symlink_to(target)

    summary = records.record_promoted_run(run, "code-review")

    assert summary["status"] == "failed"
    assert summary["code"].startswith("ledger-io:")
    assert target.read_text(encoding="utf-8") == ""


def test_local_review_subject_comes_from_the_invoking_checkout(records: ModuleType, tmp_path: Path) -> None:
    """A local review's subject is the checkout's origin repository and the reviewed commit, with no pull request."""
    checkout = tmp_path / "demo"
    checkout.mkdir()
    _git(checkout, "init", "-q")
    _git(checkout, "remote", "add", "origin", "git@github.com:octo/demo.git")
    _git(checkout, "commit", "-q", "--allow-empty", "-m", "base")
    head = _git(checkout, "rev-parse", "HEAD")
    run = checkout / ".reports" / "codex" / "code-review" / "2026-11-25T10-00-00-000000Z-abcd1234"
    metadata = {"confidence_gaps": [PROCESS_GAP], "execution_mode": "serial-fallback", "reviewed_commit_sha": head}
    _write_json(run / "result.json", {"status": "pass", "confidence": 0.97, "metadata": metadata})

    summary = records.record_promoted_run(run, "code-review")

    assert summary["status"] == "recorded", summary
    (written,) = _ledger(records, run)
    assert written["subject"] == {"repo": "octo/demo", "pr": None, "head": head}
    assert written["run"]["route"] == "serial-fallback"


def test_unknown_subject_is_skipped(records: ModuleType, tmp_path: Path) -> None:
    """A run outside any checkout and without PR routing has no subject, so nothing is recorded."""
    _write_json(tmp_path / "result.json", {"status": "pass", "metadata": {"confidence_gaps": [PROCESS_GAP]}})

    summary = records.record_promoted_run(tmp_path, "code-remediate")

    assert summary == {"status": "skipped", "code": "subject-unknown"}
    assert not (tmp_path / "gaps.jsonl").exists()
