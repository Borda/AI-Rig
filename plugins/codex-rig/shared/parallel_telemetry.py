"""Collect privacy-minimized telemetry for staged multi-agent execution.

## Purpose

Convert sanitized rollout event rows into compact attempt and wave telemetry that can compare parallel execution with a
matched serial baseline.

## Scope

Own schema validation, token-counter handling, HMAC identifier projection, task timing extraction, derived comparison
metrics, compact retained-proof policy, and audited expiry deletion of one fixed sanitized diagnostic artifact. The
module does not schedule agents, read prompts, persist raw rollout data, recursively delete files, estimate provider
prices, or decide whether a rollout phase passes.

## Usage

Callers pass already-parsed JSON objects to :func:`collect_attempt_telemetry`, combine attempts with
:func:`aggregate_wave_telemetry`, compare matched waves with :func:`compare_parallel_to_serial`, and project an exact
wave through :func:`build_retained_wave_evidence`. Storage consumers call :func:`enforce_diagnostic_expiry` with that
exact record, an explicit time, and a dedicated diagnostics directory.

## Outputs

Returned dictionaries contain numeric counters, bounded enums, timing fields, HMAC identifiers, aggregate comparisons,
expiry audit evidence, and explicit unavailable-field reasons only. Expiry enforcement appends compact JSON Lines
evidence to ``expiry-audit.jsonl`` before an eligible deletion and after every outcome; it may unlink only
``<wave_id_hmac>.diagnostic.json`` directly inside the supplied dedicated directory and makes no network request.

## Failure

Malformed counters, raw identifiers in output fields, negative timing, invalid modes, inconsistent token totals, record
policy drift, ambiguous timestamps, or unsafe diagnostic paths raise ``TelemetryError``. Missing optional timing is
represented as ``None`` instead of being fabricated, so incomplete observability cannot appear measured.

## Used by

Read-only runtime pilots, storage consumers, calibration, and skill consumers that need wall-time and token evidence
without retaining prompts, reasoning, tool arguments, paths, credentials, or child messages. Acceptance logic consumes
these returned records separately; this module is not an execution controller.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


class TelemetryError(ValueError):
    """Raised when rollout telemetry cannot be validated safely."""


#: Names of the token-count fields collected for each wave and included in its telemetry record.
TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "total_tokens",
)
#: Execution modes a telemetry record may report.
EXECUTION_MODES = frozenset({"parallel", "independent-spawned", "serial", "serial-fallback"})
#: Final wave statuses that may be recorded in retained evidence.
RETENTION_STATUSES = frozenset({"passed", "failed", "cancelled", "conflicted"})
#: Fields that retained evidence may declare as unavailable without invalidating the record.
RETAINED_UNAVAILABLE_FIELDS = frozenset({"task_duration_ms"})
#: Pattern for a lowercase 64-character hex digest.
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
#: Exact set of fields permitted in a wave telemetry record.
_WAVE_FIELDS = frozenset(
    {
        "schema_version",
        "wave_id",
        "mode",
        "attempt_count",
        "workload_key_sha256",
        "wall_time_ms",
        "wall_time_source",
        "child_duration_proxy_ms",
        "unavailable_fields",
        *TOKEN_FIELDS,
    }
)
#: Exact set of fields permitted in a retained wave-evidence record.
_RETAINED_WAVE_EVIDENCE_FIELDS = frozenset(
    {
        "schema_version",
        "wave_id_hmac",
        "proof_sha256",
        "status",
        "observed_at",
        "resolved_at",
        "mode",
        "attempt_count",
        "workload_key_sha256",
        "wall_time_ms",
        "wall_time_source",
        "unavailable_fields",
        *TOKEN_FIELDS,
        "budget_ceiling_tokens",
        "budget_reserved_tokens",
        "actual_over_budget_tokens",
        "proof_retention",
        "diagnostic_retention",
        "diagnostic_expires_at",
    }
)
#: File-name suffix of a sanitized diagnostic file kept for a wave.
_SANITIZED_DIAGNOSTIC_SUFFIX = ".diagnostic.json"
#: File name of the log that records when diagnostic files expired.
_DIAGNOSTIC_EXPIRY_AUDIT = "expiry-audit.jsonl"


def hmac_identifier(secret: bytes, identifier: str) -> str:
    """Project one runtime identifier to a deterministic privacy-safe digest.

    The identifier itself never appears in the result; callers should retain the secret outside telemetry artifacts.

    Example:
        >>> hmac_identifier(b"secret", "attempt-1")
        '94e2abc45e43035e8777b11a249695ca119988b816649ae44b6f58d4f91f3a72'
    """
    if not isinstance(secret, bytes) or not secret:
        raise TelemetryError("hmac-secret-required")
    if not isinstance(identifier, str) or not identifier:
        raise TelemetryError("runtime-identifier-required")
    return hmac.new(secret, identifier.encode("utf-8"), hashlib.sha256).hexdigest()


def normalize_workload_key(value: str) -> str:
    """Normalize a non-empty single-token workload identity without accepting raw prompts.

    Unicode compatibility characters are folded, surrounding whitespace is removed, and case is normalized. Internal
    whitespace, control characters, and values longer than 256 characters are rejected.

    Example:
        >>> normalize_workload_key("  Batch-Run  ")
        'batch-run'
    """
    if not isinstance(value, str):
        raise TelemetryError("workload-key-required")
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    if not normalized or len(normalized) > 256 or any(char.isspace() for char in normalized):
        raise TelemetryError("workload-key-must-be-single-token")
    if any(ord(char) < 32 or ord(char) == 127 for char in normalized):
        raise TelemetryError("workload-key-control-character")
    return normalized


def _workload_key_digest(workload_key: str) -> str:
    """Hash an already-normalized workload key for retained telemetry evidence."""
    return hashlib.sha256(workload_key.encode("utf-8")).hexdigest()


def _event_payload(row: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Return an event payload mapping when a rollout row contains one."""
    payload = row.get("payload")
    return payload if isinstance(payload, Mapping) else None


def _validated_usage(usage: Any, *, field_name: str) -> dict[str, int]:
    """Validate token counters and return a typed copy with internally consistent totals."""
    if not isinstance(usage, Mapping):
        raise TelemetryError(f"{field_name}-missing")
    result: dict[str, int] = {}
    for field in TOKEN_FIELDS:
        value = usage.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise TelemetryError(f"invalid-token-counter:{field}")
        result[field] = value
    if result["total_tokens"] != result["input_tokens"] + result["output_tokens"]:
        raise TelemetryError("token-total-mismatch")
    if result["cached_input_tokens"] > result["input_tokens"]:
        raise TelemetryError("cached-input-exceeds-input")
    if result["cache_write_input_tokens"] > result["input_tokens"]:
        raise TelemetryError("cache-write-input-exceeds-input")
    if result["reasoning_output_tokens"] > result["output_tokens"]:
        raise TelemetryError("reasoning-output-exceeds-output")
    return result


def _token_usage(payload: Mapping[str, Any]) -> tuple[dict[str, int], dict[str, int] | None] | None:
    """Extract cumulative and optional incremental token counters from a token-count event."""
    if payload.get("type") != "token_count":
        return None
    info = payload.get("info")
    if not isinstance(info, Mapping):
        raise TelemetryError("token-count-info-missing")
    total = _validated_usage(info.get("total_token_usage"), field_name="total-token-usage")
    last_raw = info.get("last_token_usage")
    last = _validated_usage(last_raw, field_name="last-token-usage") if last_raw is not None else None
    return total, last


def _is_terminal(payload: Mapping[str, Any]) -> bool:
    """Return whether an event payload marks completion of the current rollout turn."""
    return payload.get("type") in {"task_complete", "turn_complete", "response_complete"}


def _reconstruct_usage(samples: Sequence[dict[str, int]]) -> tuple[dict[str, int], bool]:
    """Reconstruct token totals from cumulative samples, accounting for counter resets."""
    if not samples:
        raise TelemetryError("token-usage-not-observed")
    total = dict.fromkeys(TOKEN_FIELDS, 0)
    reset_seen = False
    previous: dict[str, int] | None = None
    for sample in samples:
        if previous is not None and sample == previous:
            continue
        if previous is None:
            delta = sample
        else:
            delta = {}
            for field in TOKEN_FIELDS:
                if sample[field] < previous[field]:
                    reset_seen = True
                    delta[field] = sample[field]
                else:
                    delta[field] = sample[field] - previous[field]
        for field in TOKEN_FIELDS:
            total[field] += delta[field]
        previous = sample
    if total["total_tokens"] != total["input_tokens"] + total["output_tokens"]:
        raise TelemetryError("reconstructed-token-total-mismatch")
    return total, reset_seen


def extract_token_usage(rows: Iterable[Mapping[str, Any]]) -> tuple[dict[str, int], str, bool]:
    """Extract token counters and their evidence source from rollout rows."""
    samples: list[dict[str, int]] = []
    terminal_seen = False
    for row in rows:
        if not isinstance(row, Mapping):
            raise TelemetryError("rollout-row-must-be-object")
        payload = _event_payload(row)
        if payload is None:
            continue
        sample = _token_usage(payload)
        if sample is not None:
            if terminal_seen:
                raise TelemetryError("token-sample-after-terminal")
            cumulative, _last = sample
            samples.append(cumulative)
        if _is_terminal(payload):
            terminal_seen = True
    if not samples:
        raise TelemetryError("token-usage-not-observed")
    if terminal_seen:
        usage = samples[-1]
        return usage, "terminal_total", False
    usage, reset_seen = _reconstruct_usage(samples)
    return usage, "delta_reconstruction", reset_seen


def extract_task_timing(rows: Iterable[Mapping[str, Any]]) -> dict[str, int | None]:
    """Extract the latest task duration and time-to-first-token values."""
    completions: list[Mapping[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise TelemetryError("rollout-row-must-be-object")
        payload = _event_payload(row)
        if payload is not None and payload.get("type") == "task_complete":
            completions.append(payload)
    if not completions:
        return {"task_duration_ms": None, "time_to_first_token_ms": None}
    payload = completions[-1]
    duration = payload.get("duration_ms")
    ttft = payload.get("time_to_first_token_ms")
    for name, value in (("duration_ms", duration), ("time_to_first_token_ms", ttft)):
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
            raise TelemetryError(f"invalid-timing:{name}")
    return {"task_duration_ms": duration, "time_to_first_token_ms": ttft}


def collect_attempt_telemetry(
    rows: Iterable[Mapping[str, Any]],
    *,
    attempt_id: str,
    role: str,
    outcome: str,
    hmac_secret: bytes,
    parent_attempt_id: str | None = None,
) -> dict[str, Any]:
    """Build one sanitized attempt record from rollout rows."""
    if not isinstance(role, str) or not role:
        raise TelemetryError("role-required")
    if not isinstance(outcome, str) or not outcome:
        raise TelemetryError("outcome-required")
    materialized_rows = list(rows)
    usage, source, reset_seen = extract_token_usage(materialized_rows)
    timing = extract_task_timing(materialized_rows)
    result: dict[str, Any] = {
        "schema_version": 1,
        "attempt_id_hmac": hmac_identifier(hmac_secret, attempt_id),
        "parent_attempt_id_hmac": (
            hmac_identifier(hmac_secret, parent_attempt_id) if parent_attempt_id is not None else None
        ),
        "role": role,
        "outcome": outcome,
        "usage_source": source,
        "counter_reset_seen": reset_seen,
        **timing,
        **usage,
    }
    return result


def aggregate_wave_telemetry(
    attempts: Sequence[Mapping[str, Any]],
    *,
    wave_id: str,
    mode: str,
    workload_key: str,
    dispatch_to_final_join_ms: int,
) -> dict[str, Any]:
    """Aggregate unique sanitized attempts into one compact wave record."""
    if not isinstance(wave_id, str) or not wave_id:
        raise TelemetryError("wave-id-required")
    if mode not in EXECUTION_MODES:
        raise TelemetryError("invalid-execution-mode")
    if not attempts:
        raise TelemetryError("wave-attempts-required")
    if mode == "parallel" and len(attempts) < 2:
        raise TelemetryError("parallel-wave-needs-two-attempts")
    normalized_workload_key = normalize_workload_key(workload_key)
    if (
        not isinstance(dispatch_to_final_join_ms, int)
        or isinstance(dispatch_to_final_join_ms, bool)
        or dispatch_to_final_join_ms < 0
    ):
        raise TelemetryError("dispatch-to-final-join-duration-required")
    seen: set[str] = set()
    usage = dict.fromkeys(TOKEN_FIELDS, 0)
    durations: list[int] = []
    unavailable: set[str] = set()
    for attempt in attempts:
        attempt_id = attempt.get("attempt_id_hmac")
        if not isinstance(attempt_id, str) or not _HEX64.fullmatch(attempt_id):
            raise TelemetryError("attempt-id-must-be-hmac")
        if attempt_id in seen:
            raise TelemetryError("duplicate-attempt-id")
        seen.add(attempt_id)
        for field in TOKEN_FIELDS:
            value = attempt.get(field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise TelemetryError(f"invalid-attempt-counter:{field}")
            usage[field] += value
        duration = attempt.get("task_duration_ms")
        if duration is None:
            unavailable.add("task_duration_ms")
        elif isinstance(duration, int) and not isinstance(duration, bool) and duration >= 0:
            durations.append(duration)
        else:
            raise TelemetryError("invalid-attempt-duration")
    result: dict[str, Any] = {
        "schema_version": 1,
        "wave_id": wave_id,
        "mode": mode,
        "attempt_count": len(attempts),
        "workload_key_sha256": _workload_key_digest(normalized_workload_key),
        "wall_time_ms": dispatch_to_final_join_ms,
        "wall_time_source": "dispatch_to_final_join",
        "child_duration_proxy_ms": max(durations) if durations else None,
        "unavailable_fields": sorted(unavailable),
        **usage,
    }
    return result


def compare_parallel_to_serial(parallel: Mapping[str, Any], serial: Mapping[str, Any]) -> dict[str, Any]:
    """Compute matched wall-time and token multipliers without price claims."""
    parallel_wall = parallel.get("wall_time_ms")
    serial_wall = serial.get("wall_time_ms")
    parallel_total = parallel.get("total_tokens")
    serial_total = serial.get("total_tokens")
    parallel_digest = parallel.get("workload_key_sha256")
    serial_digest = serial.get("workload_key_sha256")
    valid_digests = all(
        isinstance(value, str) and _HEX64.fullmatch(value) for value in (parallel_digest, serial_digest)
    )
    baseline_matched = (
        valid_digests
        and parallel_digest == serial_digest
        and parallel.get("wall_time_source") == "dispatch_to_final_join"
        and serial.get("wall_time_source") == "dispatch_to_final_join"
    )
    valid_wall_times = all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in (parallel_wall, serial_wall)
    )
    result: dict[str, Any] = {
        "wall_saved_ms": serial_wall - parallel_wall if baseline_matched and valid_wall_times else None,
        "speedup": serial_wall / parallel_wall if baseline_matched and valid_wall_times and parallel_wall > 0 else None,
        "token_multiplier": parallel_total / serial_total
        if baseline_matched and isinstance(parallel_total, int) and isinstance(serial_total, int) and serial_total > 0
        else None,
        "baseline_matched": baseline_matched,
        "unavailable_fields": [],
    }
    for field, value in (
        ("wall_saved_ms", result["wall_saved_ms"]),
        ("speedup", result["speedup"]),
        ("token_multiplier", result["token_multiplier"]),
    ):
        if value is None:
            result["unavailable_fields"].append(field)
    return result


def _retention_timestamp(value: str, label: str) -> datetime:
    """Parse one timezone-aware retention timestamp or reject ambiguous time."""
    if not isinstance(value, str) or not value:
        raise TelemetryError(f"retention-{label}-invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise TelemetryError(f"retention-{label}-invalid") from error
    if parsed.tzinfo is None:
        raise TelemetryError(f"retention-{label}-invalid")
    return parsed


def build_retained_wave_evidence(
    wave: Mapping[str, Any],
    *,
    hmac_secret: bytes,
    proof_sha256: str,
    status: str,
    observed_at: str,
    resolved_at: str | None,
    budget_ceiling_tokens: int,
    budget_reserved_tokens: int,
) -> dict[str, Any]:
    """Build durable compact proof plus a bounded diagnostic-retention policy.

    The input must be the exact output of :func:`aggregate_wave_telemetry`; unknown fields fail closed so raw prompts,
    paths, messages, or environment data cannot silently cross the retention boundary. Durable proof remains compact,
    while separate sanitized diagnostics expire 30 days after success or resolution and unresolved terminal problems
    remain available until resolution.
    """
    if set(wave) != _WAVE_FIELDS:
        raise TelemetryError("retained-wave-fields-invalid")
    wave_id = wave.get("wave_id")
    if not isinstance(wave_id, str) or not wave_id:
        raise TelemetryError("retained-wave-id-invalid")
    mode = wave.get("mode")
    if wave.get("schema_version") != 1 or not isinstance(mode, str) or mode not in EXECUTION_MODES:
        raise TelemetryError("retained-wave-contract-invalid")
    attempt_count = wave.get("attempt_count")
    wall_time_ms = wave.get("wall_time_ms")
    unavailable_fields = wave.get("unavailable_fields")
    workload_digest = wave.get("workload_key_sha256")
    if not isinstance(attempt_count, int) or isinstance(attempt_count, bool) or attempt_count <= 0:
        raise TelemetryError("retained-wave-contract-invalid")
    if not isinstance(wall_time_ms, int) or isinstance(wall_time_ms, bool) or wall_time_ms < 0:
        raise TelemetryError("retained-wave-contract-invalid")
    if wave.get("wall_time_source") != "dispatch_to_final_join":
        raise TelemetryError("retained-wave-contract-invalid")
    if not isinstance(workload_digest, str) or _HEX64.fullmatch(workload_digest) is None:
        raise TelemetryError("retained-wave-contract-invalid")
    if (
        not isinstance(unavailable_fields, list)
        or any(not isinstance(field, str) or not field for field in unavailable_fields)
        or len(set(unavailable_fields)) != len(unavailable_fields)
        or not set(unavailable_fields).issubset(RETAINED_UNAVAILABLE_FIELDS)
    ):
        raise TelemetryError("retained-wave-contract-invalid")
    usage = _validated_usage(wave, field_name="retained-wave-usage")
    if not isinstance(proof_sha256, str) or _HEX64.fullmatch(proof_sha256) is None:
        raise TelemetryError("retention-proof-digest-invalid")
    if not isinstance(status, str) or status not in RETENTION_STATUSES:
        raise TelemetryError("retention-status-invalid")
    for label, value in (
        ("ceiling", budget_ceiling_tokens),
        ("reserved", budget_reserved_tokens),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise TelemetryError(f"retention-budget-{label}-invalid")
    if budget_reserved_tokens > budget_ceiling_tokens:
        raise TelemetryError("retention-budget-reservation-exceeds-ceiling")

    observed = _retention_timestamp(observed_at, "observed-at")
    resolved = _retention_timestamp(resolved_at, "resolved-at") if resolved_at is not None else None
    if status == "passed" and resolved is not None:
        raise TelemetryError("retention-passed-resolution-invalid")
    if resolved is not None and resolved < observed:
        raise TelemetryError("retention-resolution-before-observation")
    expiry_base = observed if status == "passed" else resolved
    diagnostic_expires_at = (expiry_base + timedelta(days=30)).isoformat() if expiry_base is not None else None
    diagnostic_retention = (
        "expire-30d-after-success-or-resolution" if diagnostic_expires_at is not None else "retain-until-resolution"
    )

    return {
        "schema_version": 1,
        "wave_id_hmac": hmac_identifier(hmac_secret, wave_id),
        "proof_sha256": proof_sha256,
        "status": status,
        "observed_at": observed.isoformat(),
        "resolved_at": resolved.isoformat() if resolved is not None else None,
        "mode": mode,
        "attempt_count": attempt_count,
        "workload_key_sha256": workload_digest,
        "wall_time_ms": wall_time_ms,
        "wall_time_source": "dispatch_to_final_join",
        "unavailable_fields": list(unavailable_fields),
        **usage,
        "budget_ceiling_tokens": budget_ceiling_tokens,
        "budget_reserved_tokens": budget_reserved_tokens,
        "actual_over_budget_tokens": max(0, usage["total_tokens"] - budget_ceiling_tokens),
        "proof_retention": "durable",
        "diagnostic_retention": diagnostic_retention,
        "diagnostic_expires_at": diagnostic_expires_at,
    }


def _validated_retained_expiry(record: Mapping[str, Any]) -> tuple[str, str, datetime | None]:
    """Validate a complete retained record and return its immutable expiry state."""
    if set(record) != _RETAINED_WAVE_EVIDENCE_FIELDS:
        raise TelemetryError("retained-record-fields-invalid")
    wave_id_hmac = record.get("wave_id_hmac")
    proof_sha256 = record.get("proof_sha256")
    status = record.get("status")
    if (
        record.get("schema_version") != 1
        or not isinstance(wave_id_hmac, str)
        or _HEX64.fullmatch(wave_id_hmac) is None
        or not isinstance(proof_sha256, str)
        or _HEX64.fullmatch(proof_sha256) is None
        or not isinstance(status, str)
        or status not in RETENTION_STATUSES
    ):
        raise TelemetryError("retained-record-contract-invalid")
    observed_at = record.get("observed_at")
    resolved_at = record.get("resolved_at")
    observed = _retention_timestamp(observed_at, "observed-at")
    resolved = _retention_timestamp(resolved_at, "resolved-at") if resolved_at is not None else None
    if observed_at != observed.isoformat() or (resolved is not None and resolved_at != resolved.isoformat()):
        raise TelemetryError("retained-record-policy-drift")
    if (status == "passed" and resolved is not None) or (resolved is not None and resolved < observed):
        raise TelemetryError("retained-record-policy-drift")
    mode = record.get("mode")
    attempt_count = record.get("attempt_count")
    wall_time_ms = record.get("wall_time_ms")
    workload_digest = record.get("workload_key_sha256")
    unavailable_fields = record.get("unavailable_fields")
    if (
        not isinstance(mode, str)
        or mode not in EXECUTION_MODES
        or not isinstance(attempt_count, int)
        or isinstance(attempt_count, bool)
        or attempt_count <= 0
        or not isinstance(wall_time_ms, int)
        or isinstance(wall_time_ms, bool)
        or wall_time_ms < 0
        or record.get("wall_time_source") != "dispatch_to_final_join"
        or not isinstance(workload_digest, str)
        or _HEX64.fullmatch(workload_digest) is None
        or not isinstance(unavailable_fields, list)
        or any(not isinstance(field, str) or not field for field in unavailable_fields)
        or len(set(unavailable_fields)) != len(unavailable_fields)
        or not set(unavailable_fields).issubset(RETAINED_UNAVAILABLE_FIELDS)
    ):
        raise TelemetryError("retained-record-contract-invalid")
    usage = _validated_usage(record, field_name="retained-record-usage")
    ceiling = record.get("budget_ceiling_tokens")
    reserved = record.get("budget_reserved_tokens")
    if (
        not isinstance(ceiling, int)
        or isinstance(ceiling, bool)
        or ceiling <= 0
        or not isinstance(reserved, int)
        or isinstance(reserved, bool)
        or reserved <= 0
        or reserved > ceiling
        or record.get("actual_over_budget_tokens") != max(0, usage["total_tokens"] - ceiling)
        or record.get("proof_retention") != "durable"
    ):
        raise TelemetryError("retained-record-contract-invalid")
    expiry_base = observed if status == "passed" else resolved
    expiry = expiry_base + timedelta(days=30) if expiry_base is not None else None
    expected_retention = "expire-30d-after-success-or-resolution" if expiry is not None else "retain-until-resolution"
    expected_expiry = expiry.isoformat() if expiry is not None else None
    if (
        record.get("diagnostic_retention") != expected_retention
        or record.get("diagnostic_expires_at") != expected_expiry
    ):
        raise TelemetryError("retained-record-policy-drift")
    return wave_id_hmac, status, expiry


def _diagnostic_directory_root(diagnostics_directory: Path) -> Path:
    """Return a resolved dedicated diagnostics directory without following a symlink."""
    if not isinstance(diagnostics_directory, Path):
        raise TelemetryError("diagnostic-directory-required")
    try:
        metadata = diagnostics_directory.lstat()
    except FileNotFoundError as error:
        raise TelemetryError("diagnostic-directory-missing") from error
    if stat.S_ISLNK(metadata.st_mode):
        raise TelemetryError("diagnostic-directory-symlink-invalid")
    if not stat.S_ISDIR(metadata.st_mode):
        raise TelemetryError("diagnostic-directory-not-directory")
    root = diagnostics_directory.resolve(strict=True)
    if diagnostics_directory.absolute() != root:
        raise TelemetryError("diagnostic-directory-path-escape")
    return root


def _diagnostic_audit_evidence(
    wave_id_hmac: str,
    status: str,
    checked_at: datetime,
    expiry: datetime | None,
    action: str,
    deleted: bool,
) -> dict[str, Any]:
    """Build compact audit evidence without retaining the diagnostic path."""
    return {
        "schema_version": 1,
        "wave_id_hmac": wave_id_hmac,
        "status": status,
        "checked_at": checked_at.isoformat(),
        "diagnostic_expires_at": expiry.isoformat() if expiry is not None else None,
        "action": action,
        "deleted": deleted,
    }


def _write_diagnostic_audit(directory: Path, evidence: Mapping[str, Any]) -> None:
    """Append one compact expiry event durably to the fixed audit file."""
    audit_path = directory / _DIAGNOSTIC_EXPIRY_AUDIT
    try:
        metadata = audit_path.lstat()
    except FileNotFoundError:
        pass
    else:
        if stat.S_ISLNK(metadata.st_mode):
            raise TelemetryError("diagnostic-audit-target-symlink-invalid")
        if not stat.S_ISREG(metadata.st_mode):
            raise TelemetryError("diagnostic-audit-target-not-regular-file")

    payload = json.dumps(dict(evidence), sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(audit_path, flags, 0o600)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise TelemetryError("diagnostic-audit-target-not-regular-file")
        with os.fdopen(descriptor, "ab") as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except TelemetryError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as error:
        if descriptor is not None:
            os.close(descriptor)
        raise TelemetryError("diagnostic-audit-write-failed") from error


def _record_diagnostic_audit(
    directory: Path,
    wave_id_hmac: str,
    status: str,
    checked_at: datetime,
    expiry: datetime | None,
    action: str,
    *,
    deleted: bool,
) -> dict[str, Any]:
    """Build, durably append, and return one compact expiry audit event."""
    evidence = _diagnostic_audit_evidence(wave_id_hmac, status, checked_at, expiry, action, deleted)
    _write_diagnostic_audit(directory, evidence)
    return evidence


def enforce_diagnostic_expiry(record: Mapping[str, Any], *, diagnostics_directory: Path, now: str) -> dict[str, Any]:
    """Audit expiry policy and delete at most one expired sanitized diagnostic.

    Only exact records returned by :func:`build_retained_wave_evidence` are accepted. Every outcome is appended to the
    fixed durable audit before return. Eligible deletion records an intent before unlinking and a completion afterward,
    so an interrupted completion retains evidence for a later idempotent check. The sole allowed deletion target is the
    direct child named ``<wave_id_hmac>.diagnostic.json`` of a diagnostics directory that is not a symlink; symlinks and
    targets that are not regular files fail closed.
    """
    wave_id_hmac, status, expiry = _validated_retained_expiry(record)
    checked_at = _retention_timestamp(now, "now")
    directory = _diagnostic_directory_root(diagnostics_directory)
    if expiry is None:
        return _record_diagnostic_audit(
            directory, wave_id_hmac, status, checked_at, expiry, "retained-unresolved", deleted=False
        )
    if checked_at < expiry:
        return _record_diagnostic_audit(
            directory, wave_id_hmac, status, checked_at, expiry, "not-expired", deleted=False
        )

    target = directory / f"{wave_id_hmac}{_SANITIZED_DIAGNOSTIC_SUFFIX}"
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        return _record_diagnostic_audit(
            directory, wave_id_hmac, status, checked_at, expiry, "already-missing", deleted=False
        )
    if stat.S_ISLNK(metadata.st_mode):
        raise TelemetryError("diagnostic-target-symlink-invalid")
    if not stat.S_ISREG(metadata.st_mode):
        raise TelemetryError("diagnostic-target-not-regular-file")

    _record_diagnostic_audit(directory, wave_id_hmac, status, checked_at, expiry, "delete-intent", deleted=False)
    try:
        target.unlink()
    except FileNotFoundError:
        return _record_diagnostic_audit(
            directory, wave_id_hmac, status, checked_at, expiry, "already-missing", deleted=False
        )
    except OSError as error:
        _record_diagnostic_audit(directory, wave_id_hmac, status, checked_at, expiry, "delete-failed", deleted=False)
        raise TelemetryError("diagnostic-delete-failed") from error
    return _record_diagnostic_audit(directory, wave_id_hmac, status, checked_at, expiry, "deleted", deleted=True)
