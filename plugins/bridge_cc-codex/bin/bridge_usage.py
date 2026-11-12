#!/usr/bin/env python3
"""Summarize retained local Bridge usage without contacting either provider.

Purpose:
    provide explicit-workspace offline accounting from retained health rows and directly linked evidence.

Scope:
    read only each selected workspace's .temp/bridge/health.jsonl, result reports, and incident chains; never scan home
    directories, transcripts, provider sessions, or unrelated workspace content. Usage: invoke this script with one or
    more --workspace PATH arguments, or call summarize with explicit Path values. Outputs: one JSON object with
    provider-separated raw token sums, normalized input/output totals, operational status counts, provenance, missing-
    observation counts, and row-specific diagnostics. Costs are observed amounts, with unknown spend explicit; no price
    schedule or quality ranking is inferred. Latency uses observed whole-call elapsed values only; p95 is the nearest-
    rank percentile. Final-attempt duration is separate from full-call elapsed and retry-attempt sums.

Failure:
    malformed records and missing or unsafe artifacts become nonfatal diagnostics; argument errors retain argparse's
    exit behavior. Reads and chain traversal are bounded.

Used by:
    people inspecting local Bridge accounting; this module has no import-time I/O and never writes artifacts, launches a
    subprocess, calls a provider, rotates evidence, or discovers additional workspaces.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import stat
import statistics
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

MAX_HEALTH_BYTES = 16 * 1024 * 1024
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
MAX_ROW_BYTES = 1024 * 1024
MAX_ROWS = 100_000
MAX_CHAIN = 64


def _linked(metadata: os.stat_result) -> bool:
    """Recognize aliases on POSIX and native Windows."""
    return stat.S_ISLNK(metadata.st_mode) or bool(getattr(metadata, "st_file_attributes", 0) & 0x400)


def _read(workspace: Path, reference: str, limit: int, *, inspect_only: bool = False) -> bytes:
    """Read one bounded regular single-link member with verified ancestor identities.

    POSIX uses directory descriptors and no-follow opens for every ancestor. Platforms without dir_fd support use
    ancestry inspection and verify identities again before accepting bytes, matching the local artifact boundary.
    """
    if not isinstance(reference, str):
        raise ValueError("unsafe_reference")
    relative = PurePosixPath(reference)
    if (
        not isinstance(reference, str)
        or "\\" in reference
        or relative.is_absolute()
        or PureWindowsPath(reference).is_absolute()
        or relative.parts[:2] != (".temp", "bridge")
        or ".." in relative.parts
        or len(relative.parts) < 3
    ):
        raise ValueError("unsafe_reference")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    directory_flags = flags | getattr(os, "O_DIRECTORY", 0)
    descriptors: list[int] = []
    snapshots = []
    current = workspace
    try:
        if os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"):
            descriptors.append(os.open(workspace, directory_flags))
        for part in relative.parts[:-1]:
            current = current / part
            metadata = current.lstat()
            if _linked(metadata) or not stat.S_ISDIR(metadata.st_mode):
                raise ValueError("unsafe artifact directory")
            snapshots.append((current, metadata))
            if descriptors:
                descriptors.append(os.open(part, directory_flags, dir_fd=descriptors[-1]))
        leaf = workspace.joinpath(*relative.parts)
        metadata = leaf.lstat()
        if _linked(metadata) or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("unsafe artifact file")
        descriptor = (
            os.open(relative.parts[-1], flags, dir_fd=descriptors[-1])
            if descriptors
            else os.open(leaf, flags | getattr(os, "O_BINARY", 0))
        )
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or not os.path.samestat(metadata, opened):
                raise ValueError("artifact changed")
            data = b"" if inspect_only else stream.read(limit + 1)
        for path, before in [*snapshots, (leaf, metadata)]:
            after = path.lstat()
            if _linked(after) or not os.path.samestat(before, after):
                raise ValueError("artifact changed")
        if len(data) > limit:
            raise ValueError("artifact size bound")
        return data
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _number(value: Any) -> bool:
    """Accept finite nonnegative observed numbers, excluding booleans."""
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def _diagnostic(items: list[dict], workspace: Path, row: int, code: str) -> None:
    """Keep source location without copying potentially private artifact contents."""
    items.append({"workspace": str(workspace), "row": row, "code": code})


def _artifact(workspace: Path, reference: Any, row: int, diagnostics: list[dict]) -> dict | None:
    """Validate an explicitly linked JSON object and report unavailable evidence."""
    if reference is None:
        return None
    if not isinstance(reference, str):
        _diagnostic(diagnostics, workspace, row, "unsafe_reference")
        return None
    try:
        value = json.loads(_read(workspace, reference, MAX_ARTIFACT_BYTES))
        if not isinstance(value, dict):
            raise ValueError("object required")
        version = value.get("schema_version", 1)
        if not isinstance(version, int) or isinstance(version, bool) or version != 1:
            _diagnostic(diagnostics, workspace, row, "unsupported_artifact_schema")
            return None
        return value
    except FileNotFoundError:
        code = "missing_linked_artifact"
    except (OSError, ValueError, UnicodeError, RecursionError) as error:
        code = "unsafe_reference" if str(error) == "unsafe_reference" else "invalid_linked_artifact"
    _diagnostic(diagnostics, workspace, row, code)
    return None


def _attempts(workspace: Path, row: dict, line: int, diagnostics: list[dict]) -> tuple[list[dict], dict]:
    """Prefer explicit attempt telemetry, otherwise reconstruct direct legacy incident chains."""
    report = _artifact(workspace, row.get("report_path"), line, diagnostics)
    telemetry = (report or {}).get("telemetry", row.get("telemetry"))
    if report is not None and any(report.get(key, row.get(key)) != row.get(key) for key in ("run_id", "direction")):
        _diagnostic(diagnostics, workspace, line, "linked_identity_mismatch")
        report = None
        telemetry = row.get("telemetry")
    incidents = []
    reference = (report or {}).get("incident", row.get("incident"))
    seen = set()
    while reference is not None:
        if not isinstance(reference, str) or reference in seen or len(seen) >= MAX_CHAIN:
            _diagnostic(diagnostics, workspace, line, "invalid_incident_chain")
            break
        seen.add(reference)
        incident = _artifact(workspace, reference, line, diagnostics)
        if incident is None:
            break
        if any(incident.get(key, row.get(key)) != row.get(key) for key in ("run_id", "direction")):
            _diagnostic(diagnostics, workspace, line, "linked_identity_mismatch")
            break
        incidents.append(incident)
        reference = incident.get("prior_incident")
    if isinstance(telemetry, dict) and isinstance(telemetry.get("attempts"), list):
        attempts = telemetry["attempts"]
        if len(attempts) > MAX_CHAIN or any(not isinstance(attempt, dict) for attempt in attempts):
            _diagnostic(diagnostics, workspace, line, "invalid_attempt_telemetry")
    # Linked transcript validation checks evidence availability without returning prompt or response contents.
    for evidence in [
        row,
        report or {},
        *incidents,
        *(
            (telemetry or {}).get("attempts", [])[:MAX_CHAIN]
            if isinstance(telemetry, dict) and isinstance(telemetry.get("attempts"), list)
            else []
        ),
    ]:
        if not isinstance(evidence, dict):
            continue
        reference = evidence.get("transcript_path")
        if reference is not None:
            try:
                _read(workspace, reference, 0, inspect_only=True)
            except FileNotFoundError:
                _diagnostic(diagnostics, workspace, line, "missing_transcript")
            except (OSError, ValueError, TypeError):
                _diagnostic(diagnostics, workspace, line, "invalid_transcript")
    if isinstance(telemetry, dict) and isinstance(telemetry.get("attempts"), list):
        attempts = telemetry["attempts"]
        if len(attempts) <= MAX_CHAIN and all(isinstance(attempt, dict) for attempt in attempts):
            return [
                {**attempt, "usage_complete": False} if telemetry.get("usage_complete") is False else attempt
                for attempt in attempts
            ], telemetry
    # Matching explicit transcript coordinates identify the terminal incident; timestamps never establish identity.
    transcript = row.get("transcript_path")
    attempts = [row]
    for incident in incidents:
        if isinstance(transcript, str) and incident.get("transcript_path") == transcript:
            continue
        attempts.append(incident)
    return attempts, {}


def _group(direction: str, version: str, requested: str, model: str) -> dict:
    """Create counters for one provider and provenance combination."""
    return {
        "direction": direction,
        "provider": {"claude_to_codex": "codex", "codex_to_claude": "claude"}.get(direction, "unknown"),
        "plugin_version": version,
        "requested_model": requested,
        "model": model,
        "calls": 0,
        "statuses": {},
        "partial_calls": 0,
        "attempts": 0,
        "observed_attempt_seconds": 0,
        "final_attempt_seconds": 0,
        "elapsed_seconds": 0,
        "missing_elapsed_calls": 0,
        "_elapsed_measurements": [],
        "raw_tokens": {},
        "normalized": {"input_tokens_total": 0, "output_tokens_total": 0},
        "usage_complete": True,
        "known_cost": 0,
        "missing_token_attempts": 0,
        "missing_cost_attempts": 0,
        "unknown_spend": False,
    }


def _account(group: dict, attempt: dict, workspace: Path, line: int, diagnostics: list[dict]) -> None:
    """Add raw observed fields and normalize provider-specific input cache semantics."""
    group["attempts"] += 1
    duration = attempt.get("duration_seconds")
    if _number(duration):
        group["observed_attempt_seconds"] += duration
    raw = attempt.get("tokens")
    raw = raw if isinstance(raw, dict) else {}
    tokens = {key: value for key, value in raw.items() if _number(value)}
    cost = attempt.get("cost")
    if len(tokens) != len(raw) or (cost is not None and not _number(cost)):
        _diagnostic(diagnostics, workspace, line, "invalid_metrics")
    for key, value in tokens.items():
        group["raw_tokens"][key] = group["raw_tokens"].get(key, 0) + value
    usage = attempt.get("usage")
    if isinstance(usage, dict) and _number(usage.get("input_tokens_total")):
        incoming = usage["input_tokens_total"]
    else:
        incoming = tokens.get("input_tokens")
        if incoming is not None and group["provider"] == "claude":
            incoming += tokens.get("cache_creation_input_tokens", 0) + tokens.get("cache_read_input_tokens", 0)
    outgoing = tokens.get("output_tokens")
    if isinstance(usage, dict) and _number(usage.get("output_tokens_total")):
        outgoing = usage["output_tokens_total"]
    incomplete = (
        incoming is None
        or outgoing is None
        or group["provider"] == "unknown"
        or attempt.get("usage_complete") is False
        or (isinstance(usage, dict) and usage.get("complete") is False)
        or (
            group["provider"] == "claude"
            and not isinstance(usage, dict)
            and not {"cache_creation_input_tokens", "cache_read_input_tokens"}.issubset(tokens)
        )
    )
    if incomplete:
        group["missing_token_attempts"] += 1
        group["usage_complete"] = False
    if incoming is not None and group["provider"] != "unknown":
        group["normalized"]["input_tokens_total"] += incoming
    if outgoing is not None and group["provider"] != "unknown":
        group["normalized"]["output_tokens_total"] += outgoing
    if _number(cost):
        group["known_cost"] += cost
    else:
        group["missing_cost_attempts"] += 1
        group["unknown_spend"] = True


#: Row fields a current writer always records; a legacy row lacks them and is listed, never backfilled.
_EXPECTED_ROW_FIELDS = ("elapsed_seconds", "started_at", "task_bytes", "response_bytes")
#: Fields each executed attempt records in current rows.
_EXPECTED_ATTEMPT_FIELDS = ("started_at", "ended_at", "returncode", "stdout_bytes", "stderr_bytes")


def _call_record(workspace: Path, row: dict, attempts: list[dict], telemetry: dict, observed: set[str]) -> dict:
    """Describe one deduplicated call with the expected fields it lacks, for completeness checks and replay."""
    version = telemetry.get("plugin_version")
    missing = [field for field in _EXPECTED_ROW_FIELDS if not _number(row.get(field))]
    if not isinstance(version, str):
        missing.append("plugin_version")
    unknown = []
    if attempts and not observed:
        # Codex CLI 0.167.1 emits no model name in its JSON stream, and observed names come only from provider metadata.
        (unknown if row.get("direction") == "claude_to_codex" else missing).append("observed_models")
    for index, attempt in enumerate(attempts, 1):
        missing += [f"attempt_{index}.{field}" for field in _EXPECTED_ATTEMPT_FIELDS if field not in attempt]
        if not (isinstance(attempt.get("tokens"), dict) and attempt["tokens"]):
            missing.append(f"attempt_{index}.tokens")
        if not _number(attempt.get("cost")) and "cost" not in unknown:
            unknown.append("cost")
    return {
        "workspace": str(workspace),
        "run_id": row.get("run_id"),
        "direction": row.get("direction"),
        "verb": row.get("verb"),
        "status": row.get("status"),
        "requested_model": row.get("model"),
        "observed_models": sorted(observed),
        "plugin_version": version if isinstance(version, str) else None,
        "started_at": row.get("started_at"),
        "ended_at": row.get("ts"),
        "elapsed_seconds": row.get("elapsed_seconds"),
        "task_bytes": row.get("task_bytes"),
        "response_bytes": row.get("response_bytes"),
        "job_id": row.get("job_id"),
        "report_path": row.get("report_path"),
        "transcript_path": row.get("transcript_path"),
        "attempts": [
            {
                key: attempt.get(key)
                for key in (*_EXPECTED_ATTEMPT_FIELDS, "attempt", "duration_seconds", "status", "tokens", "cost")
                if key in attempt
            }
            for attempt in attempts
        ],
        "missing": missing,
        "unknown": unknown,
    }


def summarize(workspaces: list[Path], *, include_calls: bool = False) -> dict:
    """Summarize only explicitly selected workspace ledgers without modifying them.

    Args:
        workspaces: Explicit project folders whose retained ledgers are read.
        include_calls: Also return one record per deduplicated call, listing the expected fields it lacks.

    Examples:
        >>> summarize([], include_calls=True)["calls"]
        []
    """
    selected = list(dict.fromkeys(path.resolve() for path in workspaces))
    diagnostics: list[dict] = []
    groups: dict[tuple, dict] = {}
    calls: list[dict] | None = [] if include_calls else None
    for workspace in selected:
        seen_rows: dict[str, dict] = {}
        sources = []
        for source in ("health.jsonl.1", "health.jsonl"):
            try:
                data = _read(workspace, f".temp/bridge/{source}", MAX_HEALTH_BYTES)
                sources.append((source, data))
            except FileNotFoundError:
                if source == "health.jsonl":
                    diagnostics.append(
                        {"workspace": str(workspace), "source": source, "row": 0, "code": "missing_health"}
                    )
            except (OSError, ValueError):
                diagnostics.append({"workspace": str(workspace), "source": source, "row": 0, "code": "unsafe_health"})
        if sources:
            diagnostics.append(
                {"workspace": str(workspace), "source": "health.jsonl", "row": 0, "code": "retained_history_only"}
            )
        for source, data in sources:
            for line, encoded in enumerate(data.splitlines(), 1):
                first_diagnostic = len(diagnostics)
                _summarize_row(workspace, source, line, encoded, seen_rows, groups, diagnostics, calls)
                for diagnostic in diagnostics[first_diagnostic:]:
                    diagnostic["source"] = source
    for group in groups.values():
        measurements = sorted(group.pop("_elapsed_measurements"))
        group["latency_median_seconds"] = statistics.median(measurements) if measurements else None
        group["latency_p95_seconds"] = measurements[math.ceil(len(measurements) * 0.95) - 1] if measurements else None
    result = {
        "schema_version": 1,
        "workspaces": [str(path) for path in selected],
        "groups": list(groups.values()),
        "diagnostics": diagnostics,
    }
    if calls is not None:
        result["calls"] = calls
    return result


def _summarize_row(
    workspace: Path,
    source: str,
    line: int,
    encoded: bytes,
    seen_rows: dict,
    groups: dict,
    diagnostics: list[dict],
    calls: list[dict] | None = None,
) -> None:
    """Account one source row, rejecting conflicting identities across rotated ledgers."""
    if line > MAX_ROWS:
        if line == MAX_ROWS + 1:
            _diagnostic(diagnostics, workspace, line, "row_limit")
        return
    try:
        if len(encoded) > MAX_ROW_BYTES:
            raise ValueError("row size bound")
        row = json.loads(encoded)
        if not isinstance(row, dict):
            raise ValueError("object required")
    except (ValueError, UnicodeError, RecursionError):
        _diagnostic(diagnostics, workspace, line, "malformed_row")
        return
    version = row.get("schema_version", 1)
    if not isinstance(version, int) or isinstance(version, bool) or version != 1:
        _diagnostic(diagnostics, workspace, line, "unsupported_health_schema")
        return
    identity = row.get("run_id")
    if isinstance(identity, str) and identity in seen_rows:
        code = "duplicate_row" if row == seen_rows[identity] else "conflicting_duplicate_run"
        _diagnostic(diagnostics, workspace, line, code)
        return
    if isinstance(identity, str):
        seen_rows[identity] = row
    attempts, telemetry = _attempts(workspace, row, line, diagnostics)
    observed = telemetry.get("observed_models", [])
    observed = set(observed) if isinstance(observed, list) and all(isinstance(x, str) for x in observed) else set()
    for attempt in attempts:
        models = attempt.get("observed_models", [])
        if isinstance(models, list):
            observed.update(model for model in models if isinstance(model, str))
    direction = row.get("direction") if isinstance(row.get("direction"), str) else "unknown"
    version = telemetry.get("plugin_version")
    version = version if isinstance(version, str) else "unknown"
    requested = row.get("model") if isinstance(row.get("model"), str) else "unknown"
    model = ",".join(sorted(observed)) or "unknown"
    key = direction, version, requested, model
    group = groups.setdefault(key, _group(*key))
    group["calls"] += 1
    elapsed = row.get("elapsed_seconds")
    if _number(elapsed):
        group["elapsed_seconds"] += elapsed
        group["_elapsed_measurements"].append(elapsed)
    else:
        group["missing_elapsed_calls"] += 1
    duration = row.get("duration_seconds")
    if _number(duration):
        group["final_attempt_seconds"] += duration
    status = row.get("status") if isinstance(row.get("status"), str) else "unknown"
    group["statuses"][status] = group["statuses"].get(status, 0) + 1
    group["partial_calls"] += status == "partial"
    for attempt in attempts:
        _account(group, attempt, workspace, line, diagnostics)
    if calls is not None:
        calls.append(_call_record(workspace, row, attempts, telemetry, observed))


def main(argv: list[str] | None = None) -> int:
    """Print one offline JSON summary for the required explicit workspace arguments."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workspace", type=Path, action="append", required=True)
    parser.add_argument("--calls", action="store_true", help="also list every call with the expected fields it lacks")
    arguments = parser.parse_args(argv)
    print(json.dumps(summarize(arguments.workspace, include_calls=arguments.calls), sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
