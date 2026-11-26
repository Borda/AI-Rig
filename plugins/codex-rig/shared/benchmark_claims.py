#!/usr/bin/env python3
"""Turn base-versus-head benchmark samples from one host into reproducible verdicts on an author's performance claims.

## Purpose

An author's benchmark or memory claim is checked by its relative effect, not by the author's absolute numbers: the
workflow measures the review base and the reviewed head on this host with the same method, and this helper compares the
two sample sets. A claim that needs an accelerator class this host lacks, such as CUDA on a host without an NVIDIA GPU,
is recorded as ``hardware-unavailable`` with exactly that reason and nothing broader. Each record keeps the host class
(operating system, machine and native architecture, accelerators) as a fact of the run, never as a gap; a claim
requiring an operating system or architecture class this host is not gets the same verdict, with a reason starting ``not
reproducible on this host:`` and naming that class only. Nothing is emulated.

## Scope

The helper never launches project code. Measuring the head runs contributed code, so the workflow takes the samples
through its existing contributor-execution route (gate, sandbox and integrity checks) and passes them in. The host probe
reads only operating-system facts and the presence of vendor device tools, never a project import.

## Usage

``python benchmark_claims.py host`` prints the host specification. ``python benchmark_claims.py compare --claims
<claims.json> --samples <samples.json> --out <claims-result.json>`` writes one record per claim. ``claims.json`` is a
list of objects with ``id``, ``claim_text``, ``metric``, ``direction`` (``lower-is-better`` or ``higher-is-better``),
``author_hw``, optional ``requires`` (accelerator classes from ``cuda``, ``rocm``, ``mps``, ``tpu``, ``tensorrt``),
``requires_os`` (``linux``, ``macos``, ``windows``; only an operating system named on the claim line itself, while one
named elsewhere goes to ``author_os``) and ``requires_arch`` (``x86_64``, ``arm64``), optional ``comparable_hardware``
(``same-class`` or ``proportional``), ``method`` (``command``, ``repeats``, ``warmup``) and optional
``claimed_relative`` (the author's signed relative change, such as ``-0.4``). ``samples.json`` maps each claim id to
``{"base": [...], "head": [...]}``, measured alternately on this host.

## Outputs

Each record holds ``claim_text``, ``metric``, ``direction``, ``author_hw``, ``host_spec``, ``comparable_hardware``,
``method``, sample counts, ``base_median``, ``head_median``, ``relative_delta`` (head minus base over base, signed),
``spread`` (the larger interquartile range of the two sides divided by the base median), ``claimed_relative``,
``verdict`` and ``reason``, the same record the Claude review's claim helper writes. ``reproduced`` needs at least three
samples per side, an effect in the claimed direction larger than ``max(spread, 2%)``, and at least half the claimed
relative effect when the claim states one. ``not-reproduced`` means the measured effect is within noise, opposite, or
under half the claim. ``hardware-unavailable`` is only for a required host class (accelerator, operating system or
native architecture) this host lacks, ``no-benchmark`` for a claim without a benchmark method, and ``not-run`` when
samples are missing, too few, or produce non-finite evidence.

## Failure

Exit 0 after writing the records, 2 for malformed input.

## Used by

Code Review and Code Remediate workflows that check an author's benchmark or memory claim against the review base and
head on this host.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import shutil
import statistics
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

#: Fewest samples per side that any verdict on the effect needs.
MIN_SAMPLES = 3
#: Relative change below this is never called an effect, however tight the spread.
MIN_EFFECT = 0.02
#: A measured effect must reach this fraction of the claimed relative effect to count as reproduced.
CLAIM_FRACTION = 0.5
#: The Claude review's claim records name the direction by the metric's expected change.
_EXPECTED_DIRECTIONS = {"decrease": "lower-is-better", "increase": "higher-is-better"}
#: Accelerator classes and the names a reason uses for them; TensorRT runs only on the CUDA class.
ACCELERATORS = {"cuda": "CUDA", "rocm": "ROCm", "mps": "MPS", "tpu": "TPU", "tensorrt": "CUDA (TensorRT)"}
_ACCELERATOR_CLASS = {"tensorrt": "cuda"}
#: Operating-system classes a claim may require, keyed by ``platform.system()``.
OS_CLASSES = {"Linux": "linux", "Darwin": "macos", "Windows": "windows"}
_OS_LABELS = {"linux": "Linux", "macos": "macOS", "windows": "Windows"}
#: Architecture classes a claim may require, from the host's reported machine name.
ARCH_CLASSES = {"x86_64": "x86_64", "AMD64": "x86_64", "arm64": "arm64", "aarch64": "arm64", "ARM64": "arm64"}
#: Every host-class reason starts with this, so the verdict reads as a property of this run's host only.
HOST_PREFIX = "not reproducible on this host: "
#: Allowed claim directions.
DIRECTIONS = ("lower-is-better", "higher-is-better")
#: Comparable-hardware decisions a caller may record for a claim this host can run.
HARDWARE_DECISIONS = ("same-class", "proportional")
_PROBE_TIMEOUT = 10


def _command_output(argv: Sequence[str]) -> str | None:
    """Return a successful probe command's standard output, or ``None`` when it is absent or fails."""
    if shutil.which(argv[0]) is None:
        return None
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=_PROBE_TIMEOUT, check=False)  # noqa: S603
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def _native_arch(system: str, machine: str) -> str:
    """Return the hardware architecture class, seeing through Rosetta translation of an x86_64 interpreter on macOS."""
    if system == "Darwin" and (_command_output(["sysctl", "-n", "sysctl.proc_translated"]) or "").strip() == "1":
        return "arm64"
    return ARCH_CLASSES.get(machine, machine)


def host_spec() -> dict[str, Any]:
    """Describe this host from operating-system facts and vendor device tools only."""
    system, machine = platform.system(), platform.machine()
    cpu_model, memory = platform.processor() or None, None
    if system == "Darwin":
        cpu_model = (_command_output(["sysctl", "-n", "machdep.cpu.brand_string"]) or "").strip() or cpu_model
        raw = (_command_output(["sysctl", "-n", "hw.memsize"]) or "").strip()
        memory = int(raw) if raw.isdigit() else None
    elif system == "Linux":
        try:
            info = Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace")
            cpu_model = next(
                (line.split(":", 1)[1].strip() for line in info.splitlines() if line.startswith("model name")),
                cpu_model,
            )
            total = next(line for line in Path("/proc/meminfo").read_text().splitlines() if line.startswith("MemTotal"))
            memory = int(total.split()[1]) * 1024
        except (OSError, StopIteration, ValueError, IndexError):
            pass
    evidence: dict[str, str] = {}
    listing = _command_output(["nvidia-smi", "-L"])
    if shutil.which("nvidia-smi") is None:
        evidence["cuda"] = "nvidia-smi not on PATH"
    else:
        evidence["cuda"] = "nvidia-smi lists a GPU" if "GPU " in (listing or "") else "nvidia-smi lists no GPU"
    if shutil.which("rocm-smi") is None:
        evidence["rocm"] = "rocm-smi not on PATH"
    else:
        found = _command_output(["rocm-smi", "--showid"]) is not None
        evidence["rocm"] = "rocm-smi reports a device" if found else "rocm-smi reports no device"
    evidence["mps"] = (
        "Darwin arm64" if (system, machine) == ("Darwin", "arm64") else f"{system} {machine}, not Darwin arm64"
    )
    evidence["tpu"] = "/dev/accel0 present" if Path("/dev/accel0").exists() else "/dev/accel0 absent"
    present = {"cuda": "lists a GPU", "rocm": "reports a device", "mps": "Darwin arm64", "tpu": "present"}
    accelerators = [name for name, marker in present.items() if evidence[name].endswith(marker)]
    return {
        "os": system,
        "os_release": platform.release(),
        "machine": machine,
        "cpu_model": cpu_model,
        "cpu_count": os.cpu_count(),
        "memory_bytes": memory,
        "python": platform.python_version(),
        "accelerators": accelerators,
        "accelerator_evidence": evidence,
        "host_class": {
            "os": OS_CLASSES.get(system, system.lower()),
            "machine": machine,
            "native_arch": _native_arch(system, machine),
            "accelerators": accelerators,
        },
    }


def native_arch_class(host: dict[str, Any]) -> str:
    """Return the host's hardware architecture class: the recorded native one, else the interpreter's machine name.

    An x86_64 interpreter translated by Rosetta on Apple silicon reports ``machine`` x86_64, but the hardware is arm64;
    ``host_spec`` records that as ``host_class.native_arch``, and the Claude claim helper decides on the same value.

    Examples:
        >>> native_arch_class({"machine": "x86_64", "host_class": {"native_arch": "arm64"}})
        'arm64'
        >>> native_arch_class({"machine": "AMD64"})
        'x86_64'
    """
    recorded = host.get("host_class") if isinstance(host.get("host_class"), dict) else {}
    machine = str(host.get("machine", ""))
    native = recorded.get("native_arch") or host.get("native_arch") or machine
    return ARCH_CLASSES.get(str(native), str(native))


def host_class_gap(claim: dict[str, Any], host: dict[str, Any]) -> tuple[str, str] | None:
    """Return ``(basis, reason)`` for the first required host class this host lacks, or ``None`` when it can run here.

    The reason names exactly the missing class (with the probe evidence for an accelerator), nothing broader; a host
    matching every required class records no limitation. ``requires_os`` holds only an operating system named on the
    claim line itself; an operating system named elsewhere describes the author's environment (``author_os``).

    Example:
        >>> evidence = {"cuda": "nvidia-smi not on PATH"}
        >>> host = {"os": "Darwin", "machine": "arm64", "accelerators": [], "accelerator_evidence": evidence}
        >>> host_class_gap({"requires": ["tensorrt"]}, host)
        ('accelerator-absent', 'requires CUDA (TensorRT); host has no CUDA device (cuda: nvidia-smi not on PATH)')
        >>> host_class_gap({"requires_os": ["windows"]}, host)
        ('os-absent', 'requires Windows; host is macOS')
        >>> host_class_gap({"requires_os": ["macos"], "requires_arch": ["arm64"]}, host) is None
        True
    """
    required = claim.get("requires", [])
    if any(name not in ACCELERATORS for name in required):
        raise ValueError(f"invalid-accelerator:{required!r}")
    for name in required:
        device = _ACCELERATOR_CLASS.get(name, name)
        if device not in host["accelerators"]:
            proof = host.get("accelerator_evidence", {}).get(device, "no probe evidence")
            label, device_label = ACCELERATORS[name], ACCELERATORS[device]
            return "accelerator-absent", f"requires {label}; host has no {device_label} device ({device}: {proof})"
    host_os = OS_CLASSES.get(host.get("os", ""), str(host.get("os", "")).lower())
    wanted_os = claim.get("requires_os", [])
    if any(name not in _OS_LABELS for name in wanted_os):
        raise ValueError(f"invalid-os-class:{wanted_os!r}")
    if wanted_os and host_os not in wanted_os:
        names = " or ".join(_OS_LABELS[name] for name in wanted_os)
        return "os-absent", f"requires {names}; host is {_OS_LABELS.get(host_os, host_os)}"
    host_arch = native_arch_class(host)
    wanted_arch = claim.get("requires_arch", [])
    if any(name not in {"x86_64", "arm64"} for name in wanted_arch):
        raise ValueError(f"invalid-arch-class:{wanted_arch!r}")
    if wanted_arch and host_arch not in wanted_arch:
        return "arch-absent", f"requires {' or '.join(wanted_arch)}; host is {host_arch}"
    return None


def interquartile_range(samples: Sequence[float]) -> float:
    """Return the interquartile range in the metric's own unit.

    Example:
        >>> interquartile_range([10.0, 10.0, 10.0])
        0.0
        >>> interquartile_range([9.0, 10.0, 11.0, 12.0])
        1.5
    """
    q1, _, q3 = statistics.quantiles(samples, n=4, method="inclusive")
    return q3 - q1


def assess_claim(claim: dict[str, Any], samples: dict[str, Any] | None, host: dict[str, Any]) -> dict[str, Any]:
    """Return the verdict record for one claim from its samples on this host.

    Example:
        >>> host = {"accelerators": []}
        >>> claim = {"claim_text": "2x", "direction": "lower-is-better", "method": {"command": "python bench.py"}}
        >>> record = assess_claim(claim, {"base": [2.0, 2.1, 1.9], "head": [1.0, 1.05, 0.95]}, host)
        >>> record["verdict"], round(record["relative_delta"], 3)
        ('reproduced', -0.5)
        >>> assess_claim({**claim, "claimed_relative": -0.9}, {"base": [2.0] * 3, "head": [1.5] * 3}, host)["reason"]
        'measured -25.0% is under half the claimed -90.0%'
        >>> assess_claim(claim, {"base": [2.0, 2.0], "head": [1.0, 1.0]}, host)["verdict"]
        'not-run'
        >>> assess_claim({**claim, "requires": ["cuda"]}, None, host)["reason"]
        'not reproducible on this host: requires CUDA; host has no CUDA device (cuda: no probe evidence)'
        >>> assess_claim(claim, {"base": [1.0, 1.0, 1.0], "head": [1.0, 1.01, 0.99]}, host)["verdict"]
        'not-reproduced'
        >>> assess_claim({**claim, "method": None}, {"base": [1.0] * 3, "head": [1.0] * 3}, host)["verdict"]
        'no-benchmark'
    """
    direction = claim.get("direction") or _EXPECTED_DIRECTIONS.get(str(claim.get("expected_direction")))
    if direction not in DIRECTIONS:
        raise ValueError(f"invalid-direction:{direction!r}")
    record: dict[str, Any] = {
        "claim_text": claim.get("claim_text", ""),
        "metric": claim.get("metric", ""),
        "direction": direction,
        "author_hw": claim.get("author_hw", ""),
        "host_spec": host,
        "method": claim.get("method"),
        "base_samples_n": 0,
        "head_samples_n": 0,
        "base_median": None,
        "head_median": None,
        "relative_delta": None,
        "spread": None,
        "claimed_relative": claim.get("claimed_relative"),
        "author_os": claim.get("author_os"),
    }
    unavailable = host_class_gap(claim, host)
    if unavailable is not None:
        basis, gap = unavailable
        reason = f"{HOST_PREFIX}{gap}"
        record["comparable_hardware"] = {"decision": "not-comparable", "basis": basis, "reason": reason}
        return {**record, "verdict": "hardware-unavailable", "reason": reason}
    decision = claim.get("comparable_hardware", "proportional")
    if decision not in HARDWARE_DECISIONS:
        raise ValueError(f"invalid-comparable-hardware:{decision!r}")
    record["comparable_hardware"] = {
        "decision": decision,
        "basis": "caller-declared",
        "reason": (
            "base and head measured on this host with one method; "
            "the relative effect is compared, never the author's absolute numbers"
        ),
    }
    if not record["method"]:
        return {**record, "verdict": "no-benchmark", "reason": "no benchmark method recorded for this claim"}
    return _with_measurements(record, samples or {})


def _with_measurements(record: dict[str, Any], samples: dict[str, Any]) -> dict[str, Any]:
    """Attach evidence and judge the effect only when the samples and their statistics are finite."""
    raw_base, raw_head = samples.get("base") or [], samples.get("head") or []
    record.update(base_samples_n=len(raw_base), head_samples_n=len(raw_head))
    invalid = {**record, "verdict": "not-run", "reason": "benchmark samples are not finite measurements"}
    try:
        base = [float(value) for value in raw_base]
        head = [float(value) for value in raw_head]
    except OverflowError:
        return invalid
    if not all(math.isfinite(value) for value in (*base, *head)):
        return invalid
    base_median = statistics.median(base) if base else 0
    if not head or base_median == 0:
        return {**record, "verdict": "not-run", "reason": "no usable measurement recorded for both sides"}
    if min(len(base), len(head)) < MIN_SAMPLES:
        return {
            **record,
            "verdict": "not-run",
            "reason": f"fewer than {MIN_SAMPLES} samples per side; no verdict drawn",
        }
    head_median = statistics.median(head)
    delta = (head_median - base_median) / base_median
    # Both ranges are taken in units of the base median, the delta's denominator: a much faster head's small median
    # would otherwise inflate its own relative range and hide a real effect.
    base_iqr, head_iqr = interquartile_range(base), interquartile_range(head)
    spread = max(base_iqr, head_iqr) / base_median
    if not all(map(math.isfinite, (base_median, head_median, base_iqr, head_iqr, delta, spread))):
        return {**record, "verdict": "not-run", "reason": "benchmark statistics are not finite"}
    record.update(base_median=base_median, head_median=head_median, relative_delta=delta, spread=spread)
    return _judge_measurement(record, delta, spread)


def _judge_measurement(record: dict[str, Any], delta: float, spread: float) -> dict[str, Any]:
    """Compare a measured effect with the noise band and the stated claim."""
    noise = max(MIN_EFFECT, spread)
    claimed = record["claimed_relative"]
    if abs(delta) <= noise:
        return {
            **record,
            "verdict": "not-reproduced",
            "reason": f"measured {delta:+.1%} is within the noise band ±{noise:.1%}",
        }
    if (delta < 0) != (record["direction"] == "lower-is-better"):
        return {
            **record,
            "verdict": "not-reproduced",
            "reason": f"measured {delta:+.1%}, opposite to the claimed direction",
        }
    if isinstance(claimed, (int, float)) and abs(delta) < CLAIM_FRACTION * abs(claimed):
        return {
            **record,
            "verdict": "not-reproduced",
            "reason": f"measured {delta:+.1%} is under half the claimed {claimed:+.1%}",
        }
    return {
        **record,
        "verdict": "reproduced",
        "reason": f"measured {delta:+.1%} in the claimed direction, noise ±{noise:.1%}",
    }


def main(argv: list[str] | None = None) -> int:
    """Print the host specification or write claim verdict records."""
    parser = argparse.ArgumentParser(prog="benchmark_claims.py", description=__doc__.split("\n\n")[0])
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("host", help="Print this host's specification.")
    compare = actions.add_parser("compare", help="Write one verdict record per claim.")
    compare.add_argument("--claims", type=Path, required=True, help="Claims JSON list.")
    compare.add_argument("--samples", type=Path, required=True, help="Samples JSON keyed by claim id.")
    compare.add_argument("--out", type=Path, required=True, help="Verdict records JSON path.")
    arguments = parser.parse_args(argv)
    host = host_spec()
    if arguments.action == "host":
        print(json.dumps(host, indent=2))
        return 0
    try:
        claims = json.loads(arguments.claims.read_text(encoding="utf-8"))
        samples = json.loads(arguments.samples.read_text(encoding="utf-8"))
        if not isinstance(claims, list) or not isinstance(samples, dict):
            raise ValueError("claims must be a list and samples an object")
        records = [{"id": claim["id"], **assess_claim(claim, samples.get(claim["id"]), host)} for claim in claims]
    except (OSError, ValueError, KeyError, TypeError, statistics.StatisticsError) as error:
        print(f"benchmark-claims-invalid:{error}", file=sys.stderr)
        return 2
    arguments.out.parent.mkdir(parents=True, exist_ok=True)
    arguments.out.write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")
    print(arguments.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
