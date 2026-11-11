"""Focused contract tests for privacy-minimized parallel telemetry.

Covers token extraction from runtime rows, per-attempt and per-wave aggregation with HMAC identifiers, the compact
retained-evidence record, and the 30-day diagnostic expiry enforcement with its durable audit trail.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).parents[2]))

from shared import parallel_telemetry
from shared.parallel_telemetry import (
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


_skip_without_symlinks = pytest.mark.skipif(not _symlinks_supported(), reason="symlink capability unavailable")


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


def _attempt(attempt_id: str = "child-1", rows=None, role: str = "qa-specialist", **extra) -> dict[str, object]:
    """Collect one passing attempt; `rows` defaults to a small terminal-closed run."""
    return collect_attempt_telemetry(
        _row(_usage(2, 1), terminal=True) if rows is None else rows,
        attempt_id=attempt_id,
        role=role,
        outcome="pass",
        hmac_secret=SECRET,
        **extra,
    )


def _aggregate(
    attempts: list[dict[str, object]], *, workload_key: str = "same", join_ms: int = 10
) -> dict[str, object]:
    """Aggregate attempts into one parallel wave with a fixed id."""
    return aggregate_wave_telemetry(
        attempts, wave_id="wave-1", mode="parallel", workload_key=workload_key, dispatch_to_final_join_ms=join_ms
    )


def _parallel_wave() -> dict[str, object]:
    """Return one valid compact wave for retention-policy checks.

    Example:
        >>> _parallel_wave()["wall_time_ms"]
        180
    """
    attempts = [
        _attempt(attempt_id, rows=_row(_usage(40, 20), terminal=True), role=role)
        for attempt_id, role in (("child-1", "qa-specialist"), ("child-2", "challenger"))
    ]
    return _aggregate(attempts, workload_key="workload-a", join_ms=180)


def _retained_record(wave: dict[str, object] | None = None, **overrides) -> dict[str, object]:
    """Return a retained record with defaults for a passed wave observed 2026-09-02.

    Example:
        >>> _retained_record(status="failed")["status"]
        'failed'
    """
    arguments = {
        "hmac_secret": SECRET,
        "proof_sha256": "a" * 64,
        "status": "passed",
        "observed_at": "2026-09-02T12:00:00Z",
        "resolved_at": None,
        "budget_ceiling_tokens": 200,
        "budget_reserved_tokens": 150,
        **overrides,
    }
    return build_retained_wave_evidence(_parallel_wave() if wave is None else wave, **arguments)


@pytest.fixture
def diagnostics(tmp_path: Path) -> Path:
    """Empty dedicated diagnostics directory."""
    directory = tmp_path / "diagnostics"
    directory.mkdir()
    return directory


def _write_artifact(directory: Path, record: dict[str, object], text: str = "sanitized\n") -> Path:
    """Write a historical diagnostic with its recorded observation time as the actual modification time."""
    artifact = directory / f"{record['wave_id_hmac']}.diagnostic.json"
    artifact.write_text(text, encoding="utf-8", newline="\n")
    observed = datetime.fromisoformat(record["observed_at"]).timestamp()
    os.utime(artifact, (observed, observed))
    return artifact


def _expiry_audit_rows(directory: Path) -> list[dict[str, object]]:
    """Read durable expiry audit rows from the dedicated diagnostics directory."""
    return [json.loads(line) for line in (directory / "expiry-audit.jsonl").read_text(encoding="utf-8").splitlines()]


class TestExtractTokenUsage:
    """Token extraction from runtime rows: terminal totals, delta reconstruction, schema drift."""

    @pytest.mark.parametrize(
        ("rows", "expected_usage", "expected_source", "expected_reset"),
        [
            pytest.param(
                _row(_usage(10, 4, cached=6))
                + _row(_usage(15, 5, cached=5), total_usage=_usage(25, 9, cached=11), terminal=True),
                {"total_tokens": 34, "input_tokens": 25},
                "terminal_total",
                False,
                id="terminal-total-is-authoritative",
            ),
            pytest.param(
                _row(_usage(10, 2)) + _row(_usage(15, 3), total_usage=_usage(25, 5)) + _row(_usage(4, 1)),
                {"total_tokens": 35},
                "delta_reconstruction",
                True,
                id="missing-terminal-marks-counter-reset",
            ),
            pytest.param(
                _row(_usage(10, 2))
                + _row(_usage(10, 2), total_usage=_usage(20, 4))
                + _row(_usage(10, 2), total_usage=_usage(20, 4)),
                {"total_tokens": 24},
                "delta_reconstruction",
                False,
                id="duplicate-cumulative-samples-not-double-counted",
            ),
        ],
    )
    def test_usage_source_and_reset(self, rows, expected_usage, expected_source, expected_reset) -> None:
        """Usage totals come from the terminal event when present, else from reconstructed deltas.

        A terminal total is authoritative and its sub-counters are never added on top. Without one, deltas are rebuilt
        from cumulative samples: repeated samples count once and a shrinking counter is flagged as a reset.
        """
        usage, source, reset = extract_token_usage(rows)

        assert {key: usage[key] for key in expected_usage} == expected_usage
        assert source == expected_source
        assert reset is expected_reset

    @pytest.mark.parametrize(
        ("rows", "match"),
        [
            pytest.param(
                _row(_usage(10, 2), terminal=True) + _row(_usage(12, 3)),
                "token-sample-after-terminal",
                id="sample-after-terminal",
            ),
            pytest.param(
                _row({**_usage(2, 1), "total_tokens": 99}, terminal=True),
                "token-total-mismatch",
                id="inconsistent-total",
            ),
        ],
    )
    def test_rejects_invalid_rows(self, rows, match: str) -> None:
        """Rows that break the counter schema fail closed with a named reason.

        A token sample after the terminal event signals schema-order drift, and a total that is not input plus output
        signals corrupted counters; accepting either would silently skew the reported usage.
        """
        with pytest.raises(TelemetryError, match=match):
            extract_token_usage(rows)


class TestCollectAttemptTelemetry:
    """Per-attempt telemetry carries HMAC identifiers and never the raw ones."""

    def test_hmac_ids_without_raw_identifier(self) -> None:
        """Attempt output holds HMAC ids and omits the raw attempt identifier.

        Accepts an iterator of rows so streamed runtime logs work; the terminal event supplies the task duration.
        """
        attempt = _attempt(
            rows=iter(_row(_usage(10, 4), terminal=True)), parent_attempt_id="parent-1", attempt_id="child-1"
        )

        assert attempt["attempt_id_hmac"] == hmac_identifier(SECRET, "child-1")
        assert attempt["parent_attempt_id_hmac"] == hmac_identifier(SECRET, "parent-1")
        assert "child-1" not in str(attempt)
        assert attempt["task_duration_ms"] == 100


class TestAggregateWave:
    """Wave aggregation: explicit join duration, privacy of the workload key, fail-closed inputs."""

    @pytest.fixture
    def wave(self) -> dict[str, object]:
        """Two-attempt parallel wave with a padded, mixed-case workload key and a 180 ms join."""
        attempts = [
            _attempt("child-1", rows=_row(_usage(10, 4), terminal=True)),
            _attempt("child-2", rows=_row(_usage(12, 5), terminal=True), role="challenger"),
        ]
        return _aggregate(attempts, workload_key=" Workload-A ", join_ms=180)

    def test_wall_time_uses_explicit_join_duration(self, wave) -> None:
        """Wall time is the dispatch-to-join duration; the longest child stays a diagnostic proxy only.

        Two children of 100 ms each run in parallel and the parent joined at 180 ms, so the wave reports 180 ms and
        keeps the child duration as a separate proxy rather than substituting it for the wall time.
        """
        assert wave["wall_time_ms"] == 180
        assert wave["wall_time_source"] == "dispatch_to_final_join"
        assert wave["child_duration_proxy_ms"] == 100

    def test_workload_key_stored_only_as_hash(self, wave) -> None:
        """The wave records a workload hash and never the raw workload key."""
        assert "workload_key" not in wave
        assert "workload-a" not in str(wave)

    def test_compare_to_serial_with_matched_baseline(self, wave) -> None:
        """A serial baseline with the same workload hash yields savings, speedup and a token multiplier."""
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

    def test_compare_flags_mutated_workload_hash(self, wave) -> None:
        """A wave whose workload hash was altered after aggregation no longer matches its serial baseline."""
        serial = {
            "wall_time_ms": 300,
            "wall_time_source": "dispatch_to_final_join",
            "total_tokens": 100,
            "workload_key_sha256": wave["workload_key_sha256"],
        }

        comparison = compare_parallel_to_serial({**wave, "workload_key_sha256": "0" * 64}, serial)

        assert comparison["baseline_matched"] is False

    def test_rejects_duplicate_attempt_ids(self) -> None:
        """The same attempt cannot be counted twice in one wave.

        Passing one collected attempt twice would double its tokens and make a wave look larger than it was.
        """
        attempt = _attempt()

        with pytest.raises(TelemetryError, match="duplicate-attempt-id"):
            _aggregate([attempt, attempt])

    def test_parallel_wave_needs_two_attempts(self) -> None:
        """A parallel claim backed by a single attempt is rejected."""
        with pytest.raises(TelemetryError, match="parallel-wave-needs-two-attempts"):
            _aggregate([_attempt()])

    def test_comparison_without_workload_hash_is_unmatched(self) -> None:
        """Without matching workload hashes no speedup claim is derived.

        The comparison marks the baseline unmatched and lists the derived fields as unavailable instead of computing
        savings from two runs that may not describe the same work.
        """
        comparison = compare_parallel_to_serial(
            {"wall_time_ms": 100, "total_tokens": 10}, {"wall_time_ms": 200, "total_tokens": 20}
        )

        assert comparison["baseline_matched"] is False
        assert comparison["unavailable_fields"] == ["wall_saved_ms", "speedup", "token_multiplier"]


class TestNormalizeWorkloadKey:
    """Workload keys are single lowercase tokens so raw prompt text cannot leak in."""

    def test_trims_and_lowercases(self) -> None:
        """Surrounding whitespace and letter case are normalized away."""
        assert normalize_workload_key("  Workload-A  ") == "workload-a"

    def test_rejects_multi_token_text(self) -> None:
        """A key with spaces looks like free text and is rejected."""
        with pytest.raises(TelemetryError, match="workload-key-must-be-single-token"):
            normalize_workload_key("raw prompt text")


class TestBuildRetainedWaveEvidence:
    """The retained record is compact, private, and carries a fixed retention policy."""

    @pytest.mark.installed_plugin
    def test_compact_private_record_with_budget_overrun(self) -> None:
        """Durable proof stays useful without raw runtime identity or diagnostic payloads.

        A wave of 120 tokens against a 100-token ceiling is recorded as a 20-token overrun, the wave id appears only as
        an HMAC, and the record carries exactly the documented field set.
        """
        record = _retained_record(
            proof_sha256="f" * 64,
            observed_at="2026-09-02T12:00:00+00:00",
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

    @pytest.mark.parametrize(
        ("resolved_at", "expected_retention", "expected_expiry"),
        [
            pytest.param(None, "retain-until-resolution", None, id="unresolved-failure"),
            pytest.param(
                "2026-09-05T12:00:00Z",
                "expire-30d-after-success-or-resolution",
                "2026-10-05T12:00:00+00:00",
                id="resolved-failure",
            ),
        ],
    )
    def test_failure_waits_for_resolution_then_expires_in_thirty_days(
        self, resolved_at, expected_retention, expected_expiry
    ) -> None:
        """A failed wave keeps its diagnostic until resolved, then expires 30 days after the resolution.

        Failure diagnostics must stay available while the failure is open; once resolved the lifetime is bounded.
        """
        record = _retained_record(status="failed", resolved_at=resolved_at)

        assert record["diagnostic_retention"] == expected_retention
        assert record["diagnostic_expires_at"] == expected_expiry

    @pytest.mark.parametrize(
        ("wave_update", "overrides", "match"),
        [
            pytest.param(
                {"prompt": "must-not-be-retained"}, {}, "retained-wave-fields-invalid", id="unknown-raw-field"
            ),
            pytest.param(
                {},
                {"status": "failed", "resolved_at": "2026-09-01T12:00:00Z"},
                "retention-resolution-before-observation",
                id="resolution-before-observation",
            ),
            pytest.param(
                {"unavailable_fields": ["raw child path or message"]},
                {"status": "failed"},
                "retained-wave-contract-invalid",
                id="unavailable-fields-carry-raw-text",
            ),
        ],
    )
    def test_rejects_field_drift_and_invalid_lifecycle(self, wave_update, overrides, match: str) -> None:
        """Raw-field drift, bad contract values or impossible chronology fail closed with a named reason."""
        with pytest.raises(TelemetryError, match=match):
            _retained_record({**_parallel_wave(), **wave_update}, **overrides)


@pytest.fixture
def bystanders(diagnostics: Path) -> dict[Path, str]:
    """Files next to the target diagnostic that expiry must never touch, mapped to their content."""
    contents = {
        diagnostics / "unrelated-retained-evidence.json": '{"keep":true}\n',
        diagnostics / f"{'f' * 64}.diagnostic.json": '{"other":true}\n',
    }
    for path, text in contents.items():
        path.write_text(text, encoding="utf-8", newline="\n")
    return contents


def _read_all(contents: dict[Path, str]) -> dict[Path, str]:
    """Read the current text of every path in `contents`."""
    return {path: path.read_text(encoding="utf-8") for path in contents}


class TestEnforceDiagnosticExpiry:
    """Expiry deletes exactly one sanitized diagnostic after a durable audit intent, or leaves it alone."""

    @pytest.fixture(autouse=True)
    def _system_clock(self, monkeypatch) -> None:
        """Keep the trusted system clock after the historical fixture retention boundaries."""
        clock = mock.Mock(wraps=datetime)
        clock.now.return_value = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
        monkeypatch.setattr(parallel_telemetry, "datetime", clock)

    def test_deletes_only_the_fixed_diagnostic(self, diagnostics, bystanders, monkeypatch) -> None:
        """The expired record's own diagnostic is deleted and audited; neighbouring files survive.

        The wave's diagnostic sits next to an unrelated evidence file and another wave's diagnostic. Only the fixed-name
        target may go, and the audit trail shows the intent row before the deleted row.
        """
        record = _retained_record()
        artifact = _write_artifact(diagnostics, record)
        clock = mock.Mock(wraps=datetime)
        clock.now.return_value = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
        monkeypatch.setattr(parallel_telemetry, "datetime", clock)

        deleted = enforce_diagnostic_expiry(record, diagnostics_directory=diagnostics, now="2026-10-02T12:00:00Z")

        assert not artifact.exists()
        assert _read_all(bystanders) == bystanders
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

    def test_repeat_run_reports_already_missing(self, diagnostics, bystanders) -> None:
        """A second enforcement after deletion is idempotent and audits that the target is already gone.

        Expiry may be re-run by retries or schedulers; the repeat must not fail or delete anything else.
        """
        record = _retained_record()
        _write_artifact(diagnostics, record)
        enforce_diagnostic_expiry(record, diagnostics_directory=diagnostics, now="2026-10-02T12:00:00Z")

        repeated = enforce_diagnostic_expiry(record, diagnostics_directory=diagnostics, now="2026-10-03T12:00:00Z")

        assert repeated["action"] == "already-missing"
        assert repeated["deleted"] is False
        assert _expiry_audit_rows(diagnostics)[-1] == repeated
        assert _read_all(bystanders) == bystanders

    @pytest.mark.parametrize("status", ["failed", "cancelled", "conflicted"])
    def test_keeps_unresolved_failure_diagnostics(self, diagnostics, status: str) -> None:
        """Failed, cancelled and conflicted records stay untouched until explicitly resolved.

        Even far past any 30-day window, an unresolved failure has no expiry anchor, so its diagnostic is retained.
        """
        record = _retained_record(status=status)
        artifact = _write_artifact(diagnostics, record)

        audit = enforce_diagnostic_expiry(record, diagnostics_directory=diagnostics, now="2030-01-01T00:00:00Z")

        assert audit["action"] == "retained-unresolved"
        assert audit["deleted"] is False
        assert artifact.is_file()
        assert _expiry_audit_rows(diagnostics)[-1] == audit

    def test_deletes_expired_resolved_failure(self, diagnostics) -> None:
        """An explicit resolution is the 30-day expiry anchor for a failed record."""
        record = _retained_record(status="failed", resolved_at="2026-09-05T12:00:00Z")
        artifact = _write_artifact(diagnostics, record)

        audit = enforce_diagnostic_expiry(record, diagnostics_directory=diagnostics, now="2026-10-05T12:00:00Z")

        assert audit["action"] == "deleted"
        assert audit["diagnostic_expires_at"] == "2026-10-05T12:00:00+00:00"
        assert not artifact.exists()
        assert _expiry_audit_rows(diagnostics)[-1] == audit

    def test_requires_durable_audit_before_deletion(self, diagnostics) -> None:
        """The diagnostic is kept when the fixed audit target cannot accept evidence.

        With a directory squatting on the audit file name no intent row can be written, so deleting would leave no
        record of the action.
        """
        record = _retained_record()
        artifact = _write_artifact(diagnostics, record)
        (diagnostics / "expiry-audit.jsonl").mkdir()

        with pytest.raises(TelemetryError, match="diagnostic-audit-target-not-regular-file"):
            enforce_diagnostic_expiry(record, diagnostics_directory=diagnostics, now="2026-10-02T12:00:00Z")

        assert artifact.read_text(encoding="utf-8") == "sanitized\n"

    @mock.patch("shared.parallel_telemetry.os.fsync", autospec=True, side_effect=OSError("full"))
    def test_keeps_diagnostic_when_intent_fsync_fails(self, mock_fsync, diagnostics) -> None:
        """Deletion is prevented when the intent row cannot reach the audit durability boundary.

        Storage fails on the first fsync, before anything is deleted, so the diagnostic must still be on disk.
        """
        record = _retained_record()
        artifact = _write_artifact(diagnostics, record)

        with pytest.raises(TelemetryError, match="diagnostic-audit-write-failed"):
            enforce_diagnostic_expiry(record, diagnostics_directory=diagnostics, now="2026-10-02T12:00:00Z")

        assert mock_fsync.call_count == 1
        assert artifact.read_text(encoding="utf-8") == "sanitized\n"

    @mock.patch("shared.parallel_telemetry.os.fsync", autospec=True, side_effect=[None, OSError("full")])
    def test_retains_intent_when_outcome_fsync_fails(self, mock_fsync, diagnostics) -> None:
        """A recoverable intent trail remains when deletion succeeds but its outcome fsync fails.

        The first fsync (intent) succeeds and the second (outcome) fails after the file is gone; the intent row must
        remain so the deletion can be reconstructed.
        """
        record = _retained_record()
        artifact = _write_artifact(diagnostics, record)

        with pytest.raises(TelemetryError, match="diagnostic-audit-write-failed"):
            enforce_diagnostic_expiry(record, diagnostics_directory=diagnostics, now="2026-10-02T12:00:00Z")

        assert mock_fsync.call_count == 2
        assert not artifact.exists()
        assert _expiry_audit_rows(diagnostics)[0]["action"] == "delete-intent"

    @pytest.mark.parametrize(
        ("record_update", "now", "match"),
        [
            pytest.param({}, "2026-10-02T12:00:00", "retention-now-invalid", id="naive-now"),
            pytest.param(
                {"diagnostic_expires_at": "2026-10-03T12:00:00+00:00"},
                "2026-10-03T12:00:00Z",
                "retained-record-policy-drift",
                id="expiry-differs-from-policy",
            ),
        ],
    )
    def test_rejects_ambiguous_time_and_policy_drift(self, diagnostics, record_update, now: str, match: str) -> None:
        """Ambiguous time or a record that drifted from the 30-day policy fails before any deletion."""
        record = {**_retained_record(), **record_update}

        with pytest.raises(TelemetryError, match=match):
            enforce_diagnostic_expiry(record, diagnostics_directory=diagnostics, now=now)

    def test_rejects_directory_as_target(self, diagnostics) -> None:
        """A directory squatting on the diagnostic file name is rejected instead of removed."""
        record = _retained_record()
        (diagnostics / f"{record['wave_id_hmac']}.diagnostic.json").mkdir()

        with pytest.raises(TelemetryError, match="diagnostic-target-not-regular-file"):
            enforce_diagnostic_expiry(record, diagnostics_directory=diagnostics, now="2026-10-03T12:00:00Z")

    @_skip_without_symlinks
    def test_rejects_symlinked_directory(self, tmp_path, diagnostics) -> None:
        """A symlink pointing at the diagnostics directory is refused so expiry cannot be redirected."""
        link = tmp_path / "diagnostics-link"
        link.symlink_to(diagnostics, target_is_directory=True)

        with pytest.raises(TelemetryError, match="diagnostic-directory-symlink-invalid"):
            enforce_diagnostic_expiry(_retained_record(), diagnostics_directory=link, now="2026-10-03T12:00:00Z")

    @_skip_without_symlinks
    def test_rejects_symlinked_parent_escape(self, tmp_path) -> None:
        """A symlinked parent of the diagnostics directory is refused as a path escape."""
        parent = tmp_path / "parent"
        (parent / "diagnostics").mkdir(parents=True)
        parent_link = tmp_path / "parent-link"
        parent_link.symlink_to(parent, target_is_directory=True)

        with pytest.raises(TelemetryError, match="diagnostic-directory-path-escape"):
            enforce_diagnostic_expiry(
                _retained_record(), diagnostics_directory=parent_link / "diagnostics", now="2026-10-03T12:00:00Z"
            )

    @_skip_without_symlinks
    def test_rejects_symlinked_target_and_keeps_outside_file(self, tmp_path, diagnostics) -> None:
        """A symlinked diagnostic is refused and the file it points at is left intact.

        The link leads outside the diagnostics directory; following it would delete evidence the policy never owned.
        """
        record = _retained_record()
        outside = tmp_path / "outside-diagnostic.json"
        outside.write_text("keep\n", encoding="utf-8", newline="\n")
        (diagnostics / f"{record['wave_id_hmac']}.diagnostic.json").symlink_to(outside)

        with pytest.raises(TelemetryError, match="diagnostic-target-symlink-invalid"):
            enforce_diagnostic_expiry(record, diagnostics_directory=diagnostics, now="2026-10-03T12:00:00Z")

        assert outside.read_text(encoding="utf-8") == "keep\n"


class _FrozenClock(datetime):
    """Keep expiry decisions at one trusted instant for deterministic tests."""

    @classmethod
    def now(cls, tz=None) -> datetime:
        """Return the fixed local decision time, preserving the requested timezone."""
        return datetime(2026, 11, 11, tzinfo=timezone.utc).astimezone(tz)


def test_claimed_future_now_does_not_expire_recent_diagnostic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A far-future argument cannot advance the trusted expiry clock."""
    monkeypatch.setattr(parallel_telemetry, "datetime", _FrozenClock)
    record = _retained_record(observed_at="2026-11-10T12:00:00Z")
    target = tmp_path / f"{record['wave_id_hmac']}.diagnostic.json"
    target.write_text("sanitized", encoding="utf-8")
    written = datetime(2026, 11, 10, tzinfo=timezone.utc).timestamp()
    os.utime(target, (written, written))

    result = parallel_telemetry.enforce_diagnostic_expiry(
        record, diagnostics_directory=tmp_path, now="2030-01-01T00:00:00Z"
    )

    assert target.is_file()
    assert result["action"] == "not-expired"
    assert result["checked_at"] == "2026-11-11T00:00:00+00:00"


def test_declared_expiry_deletes_recently_modified_diagnostic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A diagnostic's declared expiry remains authoritative after a recent touch or copy."""
    monkeypatch.setattr(parallel_telemetry, "datetime", _FrozenClock)
    record = _retained_record(status="failed", resolved_at="2026-09-05T12:00:00Z")
    target = tmp_path / f"{record['wave_id_hmac']}.diagnostic.json"
    target.write_text("sanitized", encoding="utf-8")
    written = datetime(2026, 11, 10, tzinfo=timezone.utc).timestamp()
    os.utime(target, (written, written))

    result = parallel_telemetry.enforce_diagnostic_expiry(
        record, diagnostics_directory=tmp_path, now="2030-01-01T00:00:00Z"
    )

    assert not target.exists()
    assert result["action"] == "deleted"
    assert result["diagnostic_expires_at"] == "2026-10-05T12:00:00+00:00"
    assert _expiry_audit_rows(tmp_path)[-1] == result


def test_expiry_overflow_has_a_validation_error() -> None:
    """A timestamp near the calendar limit yields a policy error rather than an uncaught overflow."""
    with pytest.raises(parallel_telemetry.TelemetryError, match="retention-expiry-invalid"):
        _retained_record(observed_at="9999-12-31T12:00:00Z")
