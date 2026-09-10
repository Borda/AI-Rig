"""Focused contract tests for privacy-minimized parallel telemetry."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from shared.parallel_telemetry import (  # noqa: E402
    TelemetryError,
    aggregate_wave_telemetry,
    build_retained_wave_evidence,
    collect_attempt_telemetry,
    compare_parallel_to_serial,
    enforce_diagnostic_expiry,
    extract_token_usage,
    hmac_identifier,
    normalize_workload_key,
)
from shared import parallel_telemetry  # noqa: E402


SECRET = b"telemetry-test-secret"


def _symlinks_supported() -> bool:
    """Return whether this host permits the symlink safety contract to run."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        target = root / "target"
        link = root / "link"
        target.write_text("target", encoding="utf-8", newline="\n")
        try:
            link.symlink_to(target)
        except OSError:
            return False
        return link.is_symlink()


SYMLINKS_SUPPORTED = _symlinks_supported()


def _row(
    usage: dict[str, int],
    *,
    total_usage: dict[str, int] | None = None,
    terminal: bool = False,
) -> list[dict[str, object]]:
    """Build one token-count row and optionally append its terminal event.

    Example:
        >>> len(_row(_usage(2, 1), terminal=True))
        2
    """
    total_usage = total_usage or usage
    payload: dict[str, object] = {
        "type": "token_count",
        "info": {"last_token_usage": usage, "total_token_usage": total_usage},
    }
    rows: list[dict[str, object]] = [{"type": "event_msg", "payload": payload}]
    if terminal:
        rows.append(
            {
                "type": "event_msg",
                "payload": {"type": "task_complete", "duration_ms": 100, "time_to_first_token_ms": 20},
            }
        )
    return rows


def _usage(input_tokens: int, output_tokens: int, *, cached: int = 0) -> dict[str, int]:
    """Build internally consistent token counters for telemetry fixtures.

    Example:
        >>> _usage(2, 1, cached=1)["total_tokens"]
        3
    """
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": min(output_tokens, 3),
        "cached_input_tokens": cached,
        "cache_write_input_tokens": 0,
        "total_tokens": input_tokens + output_tokens,
    }


def test_terminal_usage_is_authoritative_and_subclasses_are_not_added() -> None:
    first = _usage(10, 4, cached=6)
    final_call = _usage(15, 5, cached=5)
    cumulative = _usage(25, 9, cached=11)
    rows = _row(first, total_usage=first) + _row(final_call, total_usage=cumulative, terminal=True)
    usage, source, reset = extract_token_usage(rows)
    assert usage["total_tokens"] == 34
    assert usage["input_tokens"] == 25
    assert source == "terminal_total"
    assert reset is False


def test_missing_terminal_reconstructs_deltas_and_marks_resets() -> None:
    first = _usage(10, 2)
    second = _usage(15, 3)
    reset = _usage(4, 1)
    rows = _row(first) + _row(second, total_usage=_usage(25, 5)) + _row(reset, total_usage=_usage(4, 1))
    usage, source, reset = extract_token_usage(rows)
    assert usage["total_tokens"] == 35
    assert source == "delta_reconstruction"
    assert reset is True


def test_nonterminal_duplicate_cumulative_samples_are_not_double_counted() -> None:
    cumulative = _usage(20, 4)
    rows = _row(_usage(10, 2), total_usage=_usage(10, 2))
    rows += _row(_usage(10, 2), total_usage=cumulative)
    rows += _row(_usage(10, 2), total_usage=cumulative)
    usage, source, reset = extract_token_usage(rows)
    assert usage["total_tokens"] == 24
    assert source == "delta_reconstruction"
    assert reset is False


def test_token_sample_after_terminal_is_schema_order_drift() -> None:
    rows = _row(_usage(10, 2), terminal=True) + _row(_usage(12, 3))
    with pytest.raises(TelemetryError, match="token-sample-after-terminal"):
        extract_token_usage(rows)


def test_attempt_output_contains_hmac_ids_and_no_raw_identifier() -> None:
    attempt = collect_attempt_telemetry(
        iter(_row(_usage(10, 4), terminal=True)),
        attempt_id="child-1",
        parent_attempt_id="parent-1",
        role="qa-specialist",
        outcome="pass",
        hmac_secret=SECRET,
    )
    assert attempt["attempt_id_hmac"] == hmac_identifier(SECRET, "child-1")
    assert attempt["parent_attempt_id_hmac"] == hmac_identifier(SECRET, "parent-1")
    assert "child-1" not in str(attempt)
    assert attempt["task_duration_ms"] == 100


def test_wave_parallel_uses_explicit_join_duration_and_keeps_child_proxy_diagnostic() -> None:
    attempts = [
        collect_attempt_telemetry(
            _row(_usage(10, 4), terminal=True),
            attempt_id="child-1",
            role="qa-specialist",
            outcome="pass",
            hmac_secret=SECRET,
        ),
        collect_attempt_telemetry(
            _row(_usage(12, 5), terminal=True),
            attempt_id="child-2",
            role="challenger",
            outcome="pass",
            hmac_secret=SECRET,
        ),
    ]
    wave = aggregate_wave_telemetry(
        attempts,
        wave_id="wave-1",
        mode="parallel",
        workload_key=" Workload-A ",
        dispatch_to_final_join_ms=180,
    )
    assert wave["wall_time_ms"] == 180
    assert wave["wall_time_source"] == "dispatch_to_final_join"
    assert wave["child_duration_proxy_ms"] == 100
    serial = {
        "wall_time_ms": 300,
        "wall_time_source": "dispatch_to_final_join",
        "total_tokens": 100,
        "workload_key_sha256": wave["workload_key_sha256"],
    }
    comparison = compare_parallel_to_serial(wave, serial)
    assert comparison["wall_saved_ms"] == 120
    assert comparison["speedup"] == 300 / 180
    assert comparison["token_multiplier"] == wave["total_tokens"] / 100
    assert comparison["baseline_matched"] is True
    assert "workload_key" not in wave
    assert "workload-a" not in str(wave)
    mutated = {**wave, "workload_key_sha256": "0" * 64}
    assert compare_parallel_to_serial(mutated, serial)["baseline_matched"] is False


def test_invalid_counter_and_duplicate_attempt_fail_closed() -> None:
    invalid = _usage(2, 1)
    invalid["total_tokens"] = 99
    with pytest.raises(TelemetryError, match="token-total-mismatch"):
        extract_token_usage(_row(invalid, terminal=True))
    attempt = collect_attempt_telemetry(
        _row(_usage(2, 1), terminal=True),
        attempt_id="child-1",
        role="qa-specialist",
        outcome="pass",
        hmac_secret=SECRET,
    )
    with pytest.raises(TelemetryError, match="duplicate-attempt-id"):
        aggregate_wave_telemetry(
            [attempt, attempt],
            wave_id="wave-1",
            mode="parallel",
            workload_key="same",
            dispatch_to_final_join_ms=10,
        )


def test_parallel_claim_needs_two_attempts_and_matched_baseline() -> None:
    attempt = collect_attempt_telemetry(
        _row(_usage(2, 1), terminal=True),
        attempt_id="child-1",
        role="qa-specialist",
        outcome="pass",
        hmac_secret=SECRET,
    )
    with pytest.raises(TelemetryError, match="parallel-wave-needs-two-attempts"):
        aggregate_wave_telemetry(
            [attempt],
            wave_id="wave-1",
            mode="parallel",
            workload_key="same",
            dispatch_to_final_join_ms=10,
        )
    comparison = compare_parallel_to_serial(
        {"wall_time_ms": 100, "total_tokens": 10}, {"wall_time_ms": 200, "total_tokens": 20}
    )
    assert comparison["baseline_matched"] is False
    assert comparison["unavailable_fields"] == ["wall_saved_ms", "speedup", "token_multiplier"]


def test_workload_key_is_required_and_post_mutation_is_detected() -> None:
    assert normalize_workload_key("  Workload-A  ") == "workload-a"
    with pytest.raises(TelemetryError, match="workload-key-must-be-single-token"):
        normalize_workload_key("raw prompt text")


def _parallel_wave() -> dict[str, object]:
    """Return one valid compact wave for retention-policy checks.

    Example:
        >>> _parallel_wave()["wall_time_ms"]
        180
    """
    attempts = [
        collect_attempt_telemetry(
            _row(_usage(40, 20), terminal=True),
            attempt_id=attempt_id,
            role=role,
            outcome="pass",
            hmac_secret=SECRET,
        )
        for attempt_id, role in (("child-1", "qa-specialist"), ("child-2", "challenger"))
    ]
    return aggregate_wave_telemetry(
        attempts,
        wave_id="wave-1",
        mode="parallel",
        workload_key="workload-a",
        dispatch_to_final_join_ms=180,
    )


@pytest.mark.installed_plugin
def test_retained_wave_evidence_is_compact_private_and_observes_budget_overrun() -> None:
    """Keep durable proof useful without retaining raw runtime identity or diagnostic payloads."""
    record = build_retained_wave_evidence(
        _parallel_wave(),
        hmac_secret=SECRET,
        proof_sha256="f" * 64,
        status="passed",
        observed_at="2026-09-02T12:00:00+00:00",
        resolved_at=None,
        budget_ceiling_tokens=100,
        budget_reserved_tokens=100,
    )

    assert record["wave_id_hmac"] == hmac_identifier(SECRET, "wave-1")
    assert "wave_id" not in record
    assert "wave-1" not in str(record)
    assert record["actual_over_budget_tokens"] == 20
    assert record["proof_retention"] == "durable"
    assert record["diagnostic_retention"] == "expire-30d-after-success-or-resolution"
    assert record["diagnostic_expires_at"] == "2026-10-02T12:00:00+00:00"
    assert set(record) == {
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
        "input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "total_tokens",
        "budget_ceiling_tokens",
        "budget_reserved_tokens",
        "actual_over_budget_tokens",
        "proof_retention",
        "diagnostic_retention",
        "diagnostic_expires_at",
    }


def test_failed_retention_waits_for_resolution_then_expires_in_thirty_days() -> None:
    """Retain unresolved failure diagnostics while bounding resolved diagnostic lifetime."""
    unresolved = build_retained_wave_evidence(
        _parallel_wave(),
        hmac_secret=SECRET,
        proof_sha256="a" * 64,
        status="failed",
        observed_at="2026-09-02T12:00:00Z",
        resolved_at=None,
        budget_ceiling_tokens=200,
        budget_reserved_tokens=150,
    )
    assert unresolved["diagnostic_retention"] == "retain-until-resolution"
    assert unresolved["diagnostic_expires_at"] is None

    resolved = build_retained_wave_evidence(
        _parallel_wave(),
        hmac_secret=SECRET,
        proof_sha256="a" * 64,
        status="failed",
        observed_at="2026-09-02T12:00:00Z",
        resolved_at="2026-09-05T12:00:00Z",
        budget_ceiling_tokens=200,
        budget_reserved_tokens=150,
    )
    assert resolved["diagnostic_expires_at"] == "2026-10-05T12:00:00+00:00"


def test_retained_wave_rejects_unknown_fields_and_invalid_lifecycle() -> None:
    """Fail closed on raw-field drift, invalid budgets, or impossible retention chronology."""
    wave = _parallel_wave()
    wave["prompt"] = "must-not-be-retained"
    with pytest.raises(TelemetryError, match="retained-wave-fields-invalid"):
        build_retained_wave_evidence(
            wave,
            hmac_secret=SECRET,
            proof_sha256="a" * 64,
            status="passed",
            observed_at="2026-09-02T12:00:00Z",
            resolved_at=None,
            budget_ceiling_tokens=200,
            budget_reserved_tokens=150,
        )

    with pytest.raises(TelemetryError, match="retention-resolution-before-observation"):
        build_retained_wave_evidence(
            _parallel_wave(),
            hmac_secret=SECRET,
            proof_sha256="a" * 64,
            status="failed",
            observed_at="2026-09-02T12:00:00Z",
            resolved_at="2026-09-01T12:00:00Z",
            budget_ceiling_tokens=200,
            budget_reserved_tokens=150,
        )

    wave = _parallel_wave()
    wave["unavailable_fields"] = ["raw child path or message"]
    with pytest.raises(TelemetryError, match="retained-wave-contract-invalid"):
        build_retained_wave_evidence(
            wave,
            hmac_secret=SECRET,
            proof_sha256="a" * 64,
            status="failed",
            observed_at="2026-09-02T12:00:00Z",
            resolved_at=None,
            budget_ceiling_tokens=200,
            budget_reserved_tokens=150,
        )


def _retained_record(*, status: str = "passed", resolved_at: str | None = None) -> dict[str, object]:
    """Return a compact record with a deterministic 30-day expiry policy.

    Example:
        >>> _retained_record(status="failed")["status"]
        'failed'
    """
    return build_retained_wave_evidence(
        _parallel_wave(),
        hmac_secret=SECRET,
        proof_sha256="a" * 64,
        status=status,
        observed_at="2026-09-02T12:00:00Z",
        resolved_at=resolved_at,
        budget_ceiling_tokens=200,
        budget_reserved_tokens=150,
    )


def _expiry_audit_rows(diagnostics: Path) -> list[dict[str, object]]:
    """Read durable expiry audit rows from the dedicated diagnostics directory."""
    return [json.loads(line) for line in (diagnostics / "expiry-audit.jsonl").read_text(encoding="utf-8").splitlines()]


def test_expiry_enforcement_deletes_only_the_fixed_sanitized_diagnostic_and_is_idempotent(tmp_path: Path) -> None:
    """Delete the exact expired diagnostic and report a truthful repeat audit."""
    record = _retained_record()
    diagnostics = tmp_path / "diagnostics"
    diagnostics.mkdir()
    artifact = diagnostics / f"{record['wave_id_hmac']}.diagnostic.json"
    artifact.write_text('{"sanitized":true}\n', encoding="utf-8", newline="\n")
    sibling = diagnostics / "unrelated-retained-evidence.json"
    sibling.write_text('{"keep":true}\n', encoding="utf-8", newline="\n")
    other_diagnostic = diagnostics / f"{'f' * 64}.diagnostic.json"
    other_diagnostic.write_text('{"other":true}\n', encoding="utf-8", newline="\n")

    deleted = enforce_diagnostic_expiry(record, diagnostics_directory=diagnostics, now="2026-10-02T12:00:00Z")
    assert not artifact.exists()
    assert sibling.read_text(encoding="utf-8") == '{"keep":true}\n'
    assert other_diagnostic.read_text(encoding="utf-8") == '{"other":true}\n'
    assert deleted == {
        "schema_version": 1,
        "wave_id_hmac": record["wave_id_hmac"],
        "status": "passed",
        "checked_at": "2026-10-02T12:00:00+00:00",
        "diagnostic_expires_at": "2026-10-02T12:00:00+00:00",
        "action": "deleted",
        "deleted": True,
    }
    audit_rows = _expiry_audit_rows(diagnostics)
    assert [row["action"] for row in audit_rows] == ["delete-intent", "deleted"]
    assert audit_rows[-1] == deleted

    repeated = enforce_diagnostic_expiry(record, diagnostics_directory=diagnostics, now="2026-10-03T12:00:00Z")
    assert repeated["action"] == "already-missing"
    assert repeated["deleted"] is False
    assert _expiry_audit_rows(diagnostics)[-1] == repeated
    assert sibling.read_text(encoding="utf-8") == '{"keep":true}\n'
    assert other_diagnostic.read_text(encoding="utf-8") == '{"other":true}\n'


@pytest.mark.parametrize("status", ["failed", "cancelled", "conflicted"])
def test_expiry_enforcement_never_deletes_unresolved_failure_diagnostics(tmp_path: Path, status: str) -> None:
    """Leave failed, cancelled, and conflicted records untouched until explicitly resolved."""
    diagnostics = tmp_path / "diagnostics"
    diagnostics.mkdir()
    record = _retained_record(status=status)
    artifact = diagnostics / f"{record['wave_id_hmac']}.diagnostic.json"
    artifact.write_text("sanitized\n", encoding="utf-8", newline="\n")

    audit = enforce_diagnostic_expiry(record, diagnostics_directory=diagnostics, now="2030-01-01T00:00:00Z")
    assert audit["action"] == "retained-unresolved"
    assert audit["deleted"] is False
    assert artifact.is_file()
    assert _expiry_audit_rows(diagnostics)[-1] == audit


def test_expiry_enforcement_deletes_expired_resolved_failure_diagnostic(tmp_path: Path) -> None:
    """Treat an explicit resolution as the 30-day expiry anchor for a failed record."""
    record = _retained_record(status="failed", resolved_at="2026-09-05T12:00:00Z")
    diagnostics = tmp_path / "diagnostics"
    diagnostics.mkdir()
    artifact = diagnostics / f"{record['wave_id_hmac']}.diagnostic.json"
    artifact.write_text("sanitized\n", encoding="utf-8", newline="\n")

    audit = enforce_diagnostic_expiry(record, diagnostics_directory=diagnostics, now="2026-10-05T12:00:00Z")
    assert audit["action"] == "deleted"
    assert audit["diagnostic_expires_at"] == "2026-10-05T12:00:00+00:00"
    assert not artifact.exists()
    assert _expiry_audit_rows(diagnostics)[-1] == audit


def test_expiry_enforcement_requires_durable_audit_before_deletion(tmp_path: Path) -> None:
    """Keep the diagnostic when the fixed durable audit target cannot accept evidence."""
    record = _retained_record()
    diagnostics = tmp_path / "diagnostics"
    diagnostics.mkdir()
    artifact = diagnostics / f"{record['wave_id_hmac']}.diagnostic.json"
    artifact.write_text("sanitized\n", encoding="utf-8", newline="\n")
    (diagnostics / "expiry-audit.jsonl").mkdir()

    with pytest.raises(TelemetryError, match="diagnostic-audit-target-not-regular-file"):
        enforce_diagnostic_expiry(record, diagnostics_directory=diagnostics, now="2026-10-02T12:00:00Z")

    assert artifact.read_text(encoding="utf-8") == "sanitized\n"


def test_expiry_enforcement_preserves_diagnostic_when_intent_fsync_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prevent deletion when the intent row cannot reach the audit durability boundary."""
    record = _retained_record()
    diagnostics = tmp_path / "diagnostics"
    diagnostics.mkdir()
    artifact = diagnostics / f"{record['wave_id_hmac']}.diagnostic.json"
    artifact.write_text("sanitized\n", encoding="utf-8", newline="\n")

    def _fail_fsync(_descriptor: int) -> None:
        """Simulate a storage failure at the audit durability boundary."""
        raise OSError("full")

    monkeypatch.setattr(parallel_telemetry.os, "fsync", _fail_fsync)

    with pytest.raises(TelemetryError, match="diagnostic-audit-write-failed"):
        enforce_diagnostic_expiry(record, diagnostics_directory=diagnostics, now="2026-10-02T12:00:00Z")

    assert artifact.read_text(encoding="utf-8") == "sanitized\n"


def test_expiry_enforcement_retains_intent_when_outcome_fsync_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Leave a recoverable intent trail when deletion succeeds but its outcome fsync fails."""
    record = _retained_record()
    diagnostics = tmp_path / "diagnostics"
    diagnostics.mkdir()
    artifact = diagnostics / f"{record['wave_id_hmac']}.diagnostic.json"
    artifact.write_text("sanitized\n", encoding="utf-8", newline="\n")
    real_fsync = os.fsync
    calls = 0

    def _fail_second_fsync(descriptor: int) -> None:
        """Fail only the post-deletion audit durability boundary."""
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("full")
        real_fsync(descriptor)

    monkeypatch.setattr(parallel_telemetry.os, "fsync", _fail_second_fsync)
    with pytest.raises(TelemetryError, match="diagnostic-audit-write-failed"):
        enforce_diagnostic_expiry(record, diagnostics_directory=diagnostics, now="2026-10-02T12:00:00Z")

    assert not artifact.exists()
    assert _expiry_audit_rows(diagnostics)[0]["action"] == "delete-intent"


def test_expiry_enforcement_rejects_timestamp_policy_and_target_drift(tmp_path: Path) -> None:
    """Fail closed before deletion for ambiguous time, record drift, and unsafe targets."""
    record = _retained_record()
    diagnostics = tmp_path / "diagnostics"
    diagnostics.mkdir()
    artifact = diagnostics / f"{record['wave_id_hmac']}.diagnostic.json"

    with pytest.raises(TelemetryError, match="retention-now-invalid"):
        enforce_diagnostic_expiry(record, diagnostics_directory=diagnostics, now="2026-10-02T12:00:00")

    drifted = {**record, "diagnostic_expires_at": "2026-10-03T12:00:00+00:00"}
    with pytest.raises(TelemetryError, match="retained-record-policy-drift"):
        enforce_diagnostic_expiry(drifted, diagnostics_directory=diagnostics, now="2026-10-03T12:00:00Z")

    artifact.mkdir()
    with pytest.raises(TelemetryError, match="diagnostic-target-not-regular-file"):
        enforce_diagnostic_expiry(record, diagnostics_directory=diagnostics, now="2026-10-03T12:00:00Z")


@pytest.mark.skipif(not SYMLINKS_SUPPORTED, reason="symlink capability unavailable")
def test_expiry_enforcement_rejects_symlinked_directory_and_target(tmp_path: Path) -> None:
    """Reject symlink routes so expiry cannot escape the dedicated diagnostics directory."""
    record = _retained_record()
    diagnostics = tmp_path / "diagnostics"
    diagnostics.mkdir()
    directory_link = tmp_path / "diagnostics-link"
    directory_link.symlink_to(diagnostics, target_is_directory=True)
    with pytest.raises(TelemetryError, match="diagnostic-directory-symlink-invalid"):
        enforce_diagnostic_expiry(record, diagnostics_directory=directory_link, now="2026-10-03T12:00:00Z")

    parent = tmp_path / "parent"
    nested_diagnostics = parent / "diagnostics"
    nested_diagnostics.mkdir(parents=True)
    parent_link = tmp_path / "parent-link"
    parent_link.symlink_to(parent, target_is_directory=True)
    with pytest.raises(TelemetryError, match="diagnostic-directory-path-escape"):
        enforce_diagnostic_expiry(record, diagnostics_directory=parent_link / "diagnostics", now="2026-10-03T12:00:00Z")

    artifact = diagnostics / f"{record['wave_id_hmac']}.diagnostic.json"
    outside = tmp_path / "outside-diagnostic.json"
    outside.write_text("keep\n", encoding="utf-8", newline="\n")
    artifact.symlink_to(outside)
    with pytest.raises(TelemetryError, match="diagnostic-target-symlink-invalid"):
        enforce_diagnostic_expiry(record, diagnostics_directory=diagnostics, now="2026-10-03T12:00:00Z")
    assert outside.read_text(encoding="utf-8") == "keep\n"
