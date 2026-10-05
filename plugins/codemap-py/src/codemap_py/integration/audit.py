"""Build the read-only audit report from bounded local evidence."""

from __future__ import annotations

import argparse
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path

from codemap_py import __version__, index_paths, runtime_log, scanner

# Reached through the module, not a bound name: tests patch these on the defining
# module (monkeypatch.setattr(integration.native, ...)), which a `from .native import`
# binding here would not see.
from . import native
from .managed_block import PROTOCOL_VERSION, _managed_block_status, _managed_protocol
from .native import (
    _MAX_PROVIDER_IDENTITY_BYTES,
    _MAX_PROVIDER_IDENTITY_FILES,
    _installed_version_lookup,
    _native_plugin_record,
    _provider_content_identity,
)
from .types import (
    _EXIT_OK,
    _EXIT_RUNTIME,
    CONSUMER_MANAGED_FILE,
    PROVIDER_DIR,
    PROVIDER_NAME,
    SCHEMA_VERSION,
    ConsumerTarget,
    Runtime,
    _cli_for,
    _runtimes_of,
    _targets_for_runtime,
)
from .util import _MAX_JSON_BYTES, _manifest_for, _sha256_bytes

_MAX_AUDIT_LOG_FILES = 512


_MAX_AUDIT_LOG_RECORDS = 20_000


_MAX_AUDIT_INDEX_BYTES = 8 * _MAX_JSON_BYTES


_MAX_AUDIT_DEGRADED_MODULES = 200


# Selectors that include each concrete runtime — `BOTH` is a member of both sets.
_CLAUDE_SELECTORS: tuple[Runtime, ...] = (Runtime.CLAUDE, Runtime.BOTH)


_CODEX_SELECTORS: tuple[Runtime, ...] = (Runtime.CODEX, Runtime.BOTH)


def _audit_finding(
    code: str,
    severity: str,
    status: str,
    evidence: dict,
    affected_runtime: list[str],
    remediation_kind: str,
) -> dict:
    """Build one stable audit finding record."""
    return {
        "code": code,
        "severity": severity,
        "status": status,
        "evidence": evidence,
        "affected_runtime": affected_runtime,
        "remediation_kind": remediation_kind,
    }


def _parse_audit_timestamp(value: object) -> datetime | None:
    """Return a UTC timestamp for one telemetry value, or ``None`` when it is unusable."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _record_is_in_window(record: dict, since: date | None) -> bool:
    """Return whether one telemetry record belongs to the requested audit window."""
    if since is None:
        return True
    timestamp = _parse_audit_timestamp(record.get("ts"))
    return timestamp is not None and timestamp.date() >= since


def _read_audit_records(directory: Path, *, recursive: bool, since: date | None) -> tuple[list[dict], bool]:
    """Read bounded JSONL evidence from one legacy or runtime-scoped directory."""
    records: list[dict] = []
    truncated = False
    try:
        paths = directory.rglob("*.jsonl") if recursive else directory.glob("*.jsonl")
        for file_count, path in enumerate(paths, start=1):
            if file_count > _MAX_AUDIT_LOG_FILES:
                truncated = True
                break
            try:
                with path.open(encoding="utf-8") as handle:
                    for line in handle:
                        if len(records) >= _MAX_AUDIT_LOG_RECORDS:
                            return records, True
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(record, dict) and _record_is_in_window(record, since):
                            records.append({"record": record, "path": str(path)})
            except (OSError, UnicodeError):
                continue
    except OSError:
        return records, truncated
    return records, truncated


def _query_guidance_evidence(plugin_root: Path, relative: str) -> dict:
    """Hash bounded consumer guidance and report static skill references, never activation.

    Installed identity metadata is not executable wiring. Only a consumer-owned skill reference establishes that the
    guidance is reachable in the package; runtime loading and instruction compliance remain unobservable here.
    """
    guidance = plugin_root / relative
    try:
        if not guidance.is_file():
            return {"state": "missing", "path": relative}
        skills = sorted((plugin_root / "skills").glob("*/SKILL.md"))
        if len(skills) > _MAX_PROVIDER_IDENTITY_FILES:
            return {"state": "unknown", "reason": "guidance_file_limit_exceeded"}
        contents: dict[str, bytes] = {}
        bytes_read = 0
        for path in [guidance, *skills]:
            if path.is_symlink() or not path.resolve().is_relative_to(plugin_root.resolve()):
                return {"state": "unknown", "reason": "guidance_path_outside_plugin"}
            with path.open("rb") as handle:
                data = handle.read(_MAX_PROVIDER_IDENTITY_BYTES - bytes_read + 1)
            bytes_read += len(data)
            if bytes_read > _MAX_PROVIDER_IDENTITY_BYTES:
                return {"state": "unknown", "reason": "guidance_byte_limit_exceeded"}
            contents[path.relative_to(plugin_root).as_posix()] = data
        references = [
            name for name, data in contents.items() if name != relative and guidance.name in data.decode("utf-8")
        ]
        return {
            "state": "observed" if references else "unreferenced",
            "path": relative,
            "sha256": _sha256_bytes(contents[relative]),
            "referenced_by": references,
            "activation": "unobservable",
        }
    except (OSError, UnicodeError, ValueError):
        return {"state": "unknown", "reason": "guidance_unreadable"}


def _consumer_audit(target: ConsumerTarget, root: Path, installed: object, source_root: Path | None = None) -> dict:
    """Return read-only source, installed, and managed-block evidence for one consumer."""
    source_root = source_root or root / target.plugin_dir
    manifest = _manifest_for(target, root)
    managed_relative = CONSUMER_MANAGED_FILE.get(target.consumer)
    managed_path = root / target.plugin_dir / managed_relative if managed_relative else None
    try:
        managed_content = managed_path.read_text(encoding="utf-8") if managed_path and managed_path.is_file() else ""
    except (OSError, UnicodeError):
        managed_content = ""
    managed_status = _managed_block_status(managed_content)
    native_plugin = _native_plugin_record(target.runtime, installed, target.consumer)
    native_path = native_plugin.get("source_path")
    guidance_relative = "shared/codemap-contract.md" if target.consumer == "codex-rig" else managed_relative
    source_version = manifest.get("version") if isinstance(manifest, dict) else None
    installed_version = _installed_version_lookup(target.runtime)(target.consumer, installed)
    compare_content = (
        source_version is not None and source_version == installed_version and isinstance(native_path, str)
    )
    return {
        "manifest_present": manifest is not None,
        "name_matches": isinstance(manifest, dict) and manifest.get("name") == target.consumer,
        "source_version": source_version,
        "installed_version": installed_version,
        "native_plugin": native_plugin,
        "query_guidance": {
            "source": (
                _query_guidance_evidence(source_root, guidance_relative)
                if guidance_relative and manifest is not None
                else {"state": "not_applicable"}
            ),
            "native": (
                _query_guidance_evidence(Path(native_path), guidance_relative)
                if guidance_relative and isinstance(native_path, str)
                else {"state": "unknown", "reason": "native_consumer_root_not_observed"}
            ),
        },
        "source_content": (
            _provider_content_identity(source_root, unreadable_reason="source_plugin_root_unreadable")
            if compare_content
            else {"state": "unknown", "reason": "content_comparison_not_applicable"}
        ),
        "native_content": (
            _provider_content_identity(Path(native_path), unreadable_reason="native_plugin_root_unreadable")
            if compare_content
            else {
                "state": "unknown",
                "reason": "native_plugin_root_not_observed"
                if not isinstance(native_path, str)
                else "content_comparison_not_applicable",
            }
        ),
        "managed_block": {
            "path": str(managed_path) if managed_path else None,
            "status": managed_status,
            "protocol": _managed_protocol(managed_content) if managed_status == "authenticated" else None,
        },
    }


def _runtime_audit(targets: tuple[ConsumerTarget, ...], root: Path, runtime: Runtime) -> dict:
    """Collect installed/source evidence for one selected runtime without mutating it."""
    installed = native._native_json_probe([_cli_for(runtime), "plugin", "list", "--json"])
    provider = _consumer_audit(ConsumerTarget(runtime, PROVIDER_NAME, PROVIDER_DIR), root, installed)
    return {
        "probe_available": installed is not None,
        "provider": provider,
        "consumers": {target.consumer: _consumer_audit(target, root, installed) for target in targets},
        "session_catalog": {
            "state": "unobservable",
            "reason": "native_plugin_list_has_no_session_catalog_provenance",
        },
    }


def _audit_flat_logs(flat: list[dict], runtimes: tuple[Runtime, ...]) -> tuple[list[dict], list[dict]]:
    """Classify flat telemetry as active isolation violations or legacy compatibility evidence."""
    active = [item for item in flat if item["record"].get("v") == __version__]
    legacy = [item for item in flat if item["record"].get("v") != __version__]
    affected_runtime = [runtime.value for runtime in runtimes]
    findings: list[dict] = []
    if active:
        findings.append(
            _audit_finding(
                "runtime_log_isolation_bypassed",
                "high",
                "fail",
                {"record_count": len(active), "paths": sorted({item["path"] for item in active})},
                affected_runtime,
                "provider_release_required",
            )
        )
        missing_identity = [item for item in active if not isinstance(item["record"].get("runtime"), str)]
        if missing_identity:
            findings.append(
                _audit_finding(
                    "runtime_identity_missing",
                    "high",
                    "fail",
                    {
                        "record_count": len(missing_identity),
                        "paths": sorted({item["path"] for item in missing_identity}),
                    },
                    affected_runtime,
                    "provider_release_required",
                )
            )
    if legacy:
        findings.append(
            _audit_finding(
                "legacy_flat_logs_present",
                "low",
                "warn",
                {"record_count": len(legacy), "paths": sorted({item["path"] for item in legacy})},
                affected_runtime,
                "none",
            )
        )
    return active, findings


def _audit_runtime_logs(
    root: Path, runtimes: tuple[Runtime, ...], since: date | None
) -> tuple[dict, list[dict], list[datetime], list[dict]]:
    """Inspect selected telemetry plus direct observation without treating direct as selected health."""
    log_root = runtime_log.log_root(root=root)
    flat, flat_truncated = _read_audit_records(log_root, recursive=False, since=since)
    findings: list[dict] = []
    timestamps: list[datetime] = []
    observed_records = [{**item, "observed_runtime": "flat"} for item in flat]
    selected = {
        runtime.value: {"files": 0, "records": 0, "current_records": 0, "state": "not_observed"} for runtime in runtimes
    }

    for item in flat:
        timestamp = _parse_audit_timestamp(item["record"].get("ts"))
        if timestamp is not None:
            timestamps.append(timestamp)
    active_flat, flat_findings = _audit_flat_logs(flat, runtimes)
    findings.extend(flat_findings)

    for runtime in runtimes:
        records, truncated = _read_audit_records(log_root / runtime.value, recursive=True, since=since)
        observed_records.extend({**item, "observed_runtime": runtime.value} for item in records)
        runtime_evidence = selected[runtime.value]
        runtime_evidence["files"] = len({item["path"] for item in records})
        runtime_evidence["records"] = len(records)
        active = [item for item in records if item["record"].get("v") == __version__]
        runtime_evidence["current_records"] = len(active)
        runtime_evidence["state"] = "observed" if records else "not_observed"
        if truncated:
            runtime_evidence["truncated"] = True
        for item in records:
            timestamp = _parse_audit_timestamp(item["record"].get("ts"))
            if timestamp is not None:
                timestamps.append(timestamp)
        invalid_identity = [item for item in active if item["record"].get("runtime") != runtime.value]
        if invalid_identity:
            findings.append(
                _audit_finding(
                    "runtime_identity_missing",
                    "high",
                    "fail",
                    {
                        "record_count": len(invalid_identity),
                        "paths": sorted({item["path"] for item in invalid_identity}),
                        "expected_runtime": runtime.value,
                    },
                    [runtime.value],
                    "provider_release_required",
                )
            )
        if not records:
            findings.append(
                _audit_finding(
                    "runtime_logs_not_observed",
                    "low",
                    "warn",
                    {"record_count": 0},
                    [runtime.value],
                    "observe_next_session",
                )
            )

    direct_records, direct_truncated = _read_audit_records(
        log_root / runtime_log.DEFAULT_RUNTIME, recursive=True, since=since
    )
    observed_records.extend({**item, "observed_runtime": runtime_log.DEFAULT_RUNTIME} for item in direct_records)
    for item in direct_records:
        timestamp = _parse_audit_timestamp(item["record"].get("ts"))
        if timestamp is not None:
            timestamps.append(timestamp)
    return (
        {
            "log_root": str(log_root),
            "flat": {"records": len(flat), "current_records": len(active_flat), "truncated": flat_truncated},
            "selected": selected,
            "direct": {
                "files": len({item["path"] for item in direct_records}),
                "records": len(direct_records),
                "current_records": sum(item["record"].get("v") == __version__ for item in direct_records),
                "state": "observed" if direct_records else "not_observed",
                "truncated": direct_truncated,
            },
        },
        findings,
        timestamps,
        observed_records,
    )


def _record_index_path(record: dict) -> str | None:
    """Return a telemetry-reported loaded index path without resolving a desired path."""
    result = record.get("result")
    if not isinstance(result, dict):
        return None
    path = result.get("index_path")
    if not isinstance(path, str):
        index = result.get("index")
        path = index.get("index_path") if isinstance(index, dict) else None
    return path if isinstance(path, str) else None


def _audit_index_evidence(
    identity: index_paths.IndexIdentity, runtimes: tuple[Runtime, ...], records: list[dict]
) -> tuple[dict, list[dict]]:
    """Read the current index bytes and observed telemetry identities without invoking query or refresh."""
    observed_paths: dict[str, list[str]] = {}
    for runtime in (*runtimes,):
        paths = {
            path
            for item in records
            if item["observed_runtime"] == runtime.value and (path := _record_index_path(item["record"])) is not None
        }
        observed_paths[runtime.value] = sorted(paths)
    direct_paths = {
        path
        for item in records
        if item["observed_runtime"] == runtime_log.DEFAULT_RUNTIME
        and (path := _record_index_path(item["record"])) is not None
    }
    if direct_paths:
        observed_paths[runtime_log.DEFAULT_RUNTIME] = sorted(direct_paths)
    evidence = {
        "project": identity.project,
        "root": str(identity.root),
        "root_key": identity.root_key,
        "index_path": str(identity.index_path),
        "override": identity.override,
        "exists": identity.index_path.is_file(),
        "diagnostics": [{"code": item.code, "detail": item.detail} for item in identity.diagnostics],
        "observed_runtime_paths": observed_paths,
    }
    findings: list[dict] = []
    selected_paths = {path for runtime in runtimes for path in observed_paths.get(runtime.value, [])}
    observed_runtime_count = sum(bool(observed_paths.get(runtime.value)) for runtime in runtimes)
    if observed_runtime_count > 1 and len(selected_paths) > 1:
        findings.append(
            _audit_finding(
                "split_index_roots",
                "high",
                "fail",
                {
                    "observed_runtime_paths": {
                        runtime.value: observed_paths.get(runtime.value, []) for runtime in runtimes
                    }
                },
                [runtime.value for runtime in runtimes],
                "scan_codebase",
            )
        )
    if not identity.index_path.is_file():
        findings.append(
            _audit_finding(
                "index_stale_or_unknown",
                "medium",
                "warn",
                {"index_path": str(identity.index_path), "state": "missing"},
                [runtime.value for runtime in runtimes],
                "scan_codebase",
            )
        )
        return evidence, findings
    try:
        if identity.index_path.stat().st_size > _MAX_AUDIT_INDEX_BYTES:
            raise ValueError("index exceeds bounded audit read")
        index = json.loads(identity.index_path.read_bytes())
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        findings.append(
            _audit_finding(
                "index_stale_or_unknown",
                "medium",
                "warn",
                {"index_path": str(identity.index_path), "state": "unknown", "detail": str(exc)},
                [runtime.value for runtime in runtimes],
                "scan_codebase",
            )
        )
        return evidence, findings
    modules = index.get("modules") if isinstance(index, dict) else None
    if not isinstance(modules, list):
        findings.append(
            _audit_finding(
                "index_stale_or_unknown",
                "medium",
                "warn",
                {"index_path": str(identity.index_path), "state": "unknown", "detail": "modules are unavailable"},
                [runtime.value for runtime in runtimes],
                "scan_codebase",
            )
        )
        return evidence, findings
    indexed_sha = index.get("git_sha") if isinstance(index, dict) else None
    current_sha = scanner.get_git_sha(identity.root)
    evidence["git_sha"] = {"indexed": indexed_sha if isinstance(indexed_sha, str) else None, "current": current_sha}
    if not isinstance(indexed_sha, str) or current_sha is None or indexed_sha != current_sha:
        state = "stale" if isinstance(indexed_sha, str) and current_sha is not None else "unknown"
        findings.append(
            _audit_finding(
                "index_stale_or_unknown",
                "medium",
                "warn",
                {"index_path": str(identity.index_path), "state": state, "git_sha": evidence["git_sha"]},
                [runtime.value for runtime in runtimes],
                "scan_codebase",
            )
        )
    degraded = [
        {"path": item.get("path"), "reason": item.get("reason")}
        for item in modules
        if isinstance(item, dict) and item.get("status") == "degraded"
    ]
    evidence["module_count"] = len(modules)
    evidence["degraded_module_count"] = len(degraded)
    if degraded:
        findings.append(
            _audit_finding(
                "index_degraded",
                "medium",
                "warn",
                {"count": len(degraded), "modules": degraded[:_MAX_AUDIT_DEGRADED_MODULES]},
                [runtime.value for runtime in runtimes],
                "scan_codebase",
            )
        )
    return evidence, findings


def _complete_query_module(record: dict) -> str | None:
    """Return a module only when a telemetry result proves its query completed exhaustively."""
    result = record.get("result")
    if not isinstance(result, dict):
        return None
    complete = any(
        isinstance(block, dict) and (block.get("query_complete") or block.get("exhaustive"))
        for block in (result.get("index"), result)
    )
    module = result.get("module")
    return module if complete and isinstance(module, str) and module else None


def _target_names_module(module: str, target: str) -> bool:
    """Return whether a tool target names a module on identifier boundaries in dotted or slash form."""
    segments = re.split(r"[./]", module)
    if not module or not target or any(not segment for segment in segments):
        return False
    pattern = "[./]".join(re.escape(segment) for segment in segments)
    return re.search(rf"(^|[^A-Za-z0-9_]){pattern}([^A-Za-z0-9_]|$)", target) is not None


def _usage_session_evidence(
    cli_records: list[dict],
) -> tuple[
    dict[tuple[str, str], list[datetime]],
    dict[tuple[str, str], list[tuple[datetime, str]]],
    list[tuple[tuple[str, str], datetime]],
]:
    """Collect timestamped runtime/session CLI evidence used by usage findings."""
    queries_by_session: dict[tuple[str, str], list[datetime]] = {}
    answers_by_session: dict[tuple[str, str], list[tuple[datetime, str]]] = {}
    refreshes: list[tuple[tuple[str, str], datetime]] = []
    for item in cli_records:
        record = item["record"]
        session = record.get("session")
        timestamp = _parse_audit_timestamp(record.get("ts"))
        if not isinstance(session, str) or timestamp is None:
            continue
        session_key = (item["observed_runtime"], session)
        module = _complete_query_module(record)
        if module is not None:
            queries_by_session.setdefault(session_key, []).append(timestamp)
            answers_by_session.setdefault(session_key, []).append((timestamp, module))
        result = record.get("result")
        if record.get("cmd") == "index" and isinstance(result, dict) and isinstance(result.get("trigger"), str):
            refreshes.append((session_key, timestamp))
    return queries_by_session, answers_by_session, refreshes


def _usage_avoidance_by_runtime(
    records: list[dict], answers_by_session: dict[tuple[str, str], list[tuple[datetime, str]]]
) -> dict[str, int]:
    """Count runtime/session-local tool reads naming a completed-query module within ten minutes."""
    avoidance_by_runtime: dict[str, int] = {}
    for item in records:
        record = item["record"]
        if record.get("layer") != "tool" or record.get("tool") not in {"Grep", "Read", "Glob"}:
            continue
        session = record.get("session")
        target = record.get("target")
        timestamp = _parse_audit_timestamp(record.get("ts"))
        if not isinstance(session, str) or not isinstance(target, str) or timestamp is None:
            continue
        runtime_name = item["observed_runtime"]
        answers = answers_by_session.get((runtime_name, session), [])
        if any(
            0 <= (timestamp - answered_at).total_seconds() <= 600 and _target_names_module(module, target)
            for answered_at, module in answers
        ):
            avoidance_by_runtime[runtime_name] = avoidance_by_runtime.get(runtime_name, 0) + 1
    return avoidance_by_runtime


def _usage_summary(records: list[dict]) -> dict:
    """Reduce selected telemetry into privacy-safe runtime, timing, tool, and skill counts."""
    activity_by_runtime: dict[str, dict] = {}
    cli_timing_samples: dict[str, list[int]] = {}
    tool_counts: dict[str, dict[str, int]] = {}
    skill_counts: dict[str, dict[str, int]] = {}
    for item in records:
        runtime_name = item["observed_runtime"]
        record = item["record"]
        layer = record.get("layer")
        if not isinstance(layer, str):
            continue
        activity = activity_by_runtime.setdefault(runtime_name, {"records": 0, "layers": {}})
        activity["records"] += 1
        layers = activity["layers"]
        layers[layer] = layers.get(layer, 0) + 1
        timing_ms = record.get("timing_ms")
        if layer == "cli" and isinstance(timing_ms, int) and not isinstance(timing_ms, bool) and timing_ms >= 0:
            cli_timing_samples.setdefault(runtime_name, []).append(timing_ms)
        tool = record.get("tool")
        if layer == "tool" and isinstance(tool, str):
            counts = tool_counts.setdefault(runtime_name, {})
            counts[tool] = counts.get(tool, 0) + 1
        skill = record.get("skill")
        if layer == "skill" and record.get("event") == "start" and isinstance(skill, str):
            counts = skill_counts.setdefault(runtime_name, {})
            counts[skill] = counts.get(skill, 0) + 1

    cli_timing_by_runtime: dict[str, dict] = {}
    for runtime_name, samples in cli_timing_samples.items():
        ordered = sorted(samples)
        cli_timing_by_runtime[runtime_name] = {
            "count": len(ordered),
            "total_ms": sum(ordered),
            "median_ms": ordered[(len(ordered) - 1) // 2],
            "p95_ms": ordered[(95 * len(ordered) + 99) // 100 - 1],
        }
    return {
        "activity_by_runtime": activity_by_runtime,
        "cli_timing_by_runtime": cli_timing_by_runtime,
        "tool_counts_by_runtime": tool_counts,
        "skill_counts_by_runtime": skill_counts,
        "token_measurement": {"status": "unavailable", "reason": "host_hook_contract_has_no_token_usage"},
    }


def _usage_findings(records: list[dict], runtimes: tuple[Runtime, ...]) -> tuple[dict, list[dict]]:
    """Derive usage findings only from selected runtime records carrying their needed evidence."""
    selected_names = {runtime.value for runtime in runtimes}
    selected_records = [item for item in records if item["observed_runtime"] in selected_names]
    cli_records = [item for item in selected_records if item["record"].get("layer") == "cli"]
    skill_records = [
        item
        for item in selected_records
        if item["record"].get("layer") == "skill" and item["record"].get("event") == "start"
    ]
    claude_cli_records = [item for item in cli_records if item["observed_runtime"] == Runtime.CLAUDE.value]
    claude_skill_records = [item for item in skill_records if item["observed_runtime"] == Runtime.CLAUDE.value]
    findings: list[dict] = []
    if claude_cli_records and not claude_skill_records:
        findings.append(
            _audit_finding(
                "skill_telemetry_missing",
                "medium",
                "warn",
                {"cli_record_count": len(claude_cli_records), "skill_start_count": 0},
                [Runtime.CLAUDE.value],
                "observe_next_session",
            )
        )
    queries_by_session, answers_by_session, refreshes = _usage_session_evidence(cli_records)
    refresh_without_query = [
        session_key
        for session_key, refreshed_at in refreshes
        if not any(queried_at > refreshed_at for queried_at in queries_by_session.get(session_key, []))
    ]
    if refresh_without_query:
        findings.append(
            _audit_finding(
                "refresh_without_query",
                "low",
                "info",
                {"refresh_count": len(refresh_without_query), "session_count": len(set(refresh_without_query))},
                [runtime.value for runtime in runtimes],
                "none",
            )
        )
    avoidance_by_runtime = _usage_avoidance_by_runtime(selected_records, answers_by_session)
    if avoidance_by_runtime:
        findings.append(
            _audit_finding(
                "avoidance_after_complete_query",
                "medium",
                "warn",
                {
                    "event_count": sum(avoidance_by_runtime.values()),
                    "per_runtime": avoidance_by_runtime,
                    "window_seconds": 600,
                },
                sorted(avoidance_by_runtime),
                "none",
            )
        )
    return {
        "telemetry_records": len(selected_records),
        "cli_records": len(cli_records),
        "skill_start_records": len(skill_records),
        **_usage_summary(selected_records),
    }, findings


def _audit_state_findings(runtime_state: dict, runtimes: tuple[Runtime, ...]) -> list[dict]:
    """Derive version and managed-block findings from already observed runtime state."""
    findings: list[dict] = []
    for runtime in runtimes:
        block = runtime_state[runtime.value]
        provider = block["provider"]
        source_provider_is_active = provider["source_version"] == __version__
        if source_provider_is_active and provider["installed_version"] not in {None, provider["source_version"]}:
            findings.append(
                _audit_finding(
                    "provider_version_drift",
                    "high",
                    "fail",
                    {"source_version": provider["source_version"], "installed_version": provider["installed_version"]},
                    [runtime.value],
                    "plan_sync",
                )
            )
        source_content = provider["source_content"]
        native_content = provider["native_content"]
        if (
            provider["source_version"] == provider["installed_version"] == __version__
            and source_content["state"] == native_content["state"] == "observed"
            and source_content["sha256"] != native_content["sha256"]
        ):
            findings.append(
                _audit_finding(
                    "provider_same_version_content_drift",
                    "high",
                    "fail",
                    {
                        "source_version": provider["source_version"],
                        "installed_version": provider["installed_version"],
                        "source_sha256": source_content["sha256"],
                        "native_sha256": native_content["sha256"],
                    },
                    [runtime.value],
                    "plan_sync",
                )
            )
        for consumer, evidence in block["consumers"].items():
            if (
                source_provider_is_active
                and evidence["source_version"]
                and evidence["installed_version"] not in {None, evidence["source_version"]}
            ):
                findings.append(
                    _audit_finding(
                        "consumer_version_drift",
                        "medium",
                        "warn",
                        {
                            "consumer": consumer,
                            "source_version": evidence["source_version"],
                            "installed_version": evidence["installed_version"],
                        },
                        [runtime.value],
                        "plan_sync",
                    )
                )
            consumer_source = evidence["source_content"]
            consumer_native = evidence["native_content"]
            if (
                source_provider_is_active
                and evidence["source_version"] is not None
                and evidence["source_version"] == evidence["installed_version"]
                and consumer_source["state"] == consumer_native["state"] == "observed"
                and consumer_source["sha256"] != consumer_native["sha256"]
            ):
                findings.append(
                    _audit_finding(
                        "consumer_same_version_content_drift",
                        "medium",
                        "warn",
                        {
                            "consumer": consumer,
                            "source_version": evidence["source_version"],
                            "installed_version": evidence["installed_version"],
                            "source_sha256": consumer_source["sha256"],
                            "native_sha256": consumer_native["sha256"],
                        },
                        [runtime.value],
                        "plan_sync",
                    )
                )
            managed = evidence["managed_block"]
            if managed["status"] != "absent" and (
                managed["status"] != "authenticated" or managed["protocol"] != PROTOCOL_VERSION
            ):
                findings.append(
                    _audit_finding(
                        "managed_block_invalid",
                        "high",
                        "fail",
                        {"consumer": consumer, **managed},
                        [runtime.value],
                        "plan_apply",
                    )
                )
    return findings


def _query_guidance_findings(runtime_state: dict, runtimes: tuple[Runtime, ...]) -> list[dict]:
    """Warn on observed missing, unreferenced, or source-divergent consumer guidance."""
    findings: list[dict] = []
    for runtime in runtimes:
        for consumer, evidence in runtime_state[runtime.value]["consumers"].items():
            guidance = evidence["query_guidance"]
            source, native = guidance["source"], guidance["native"]
            states = {source["state"], native["state"]}
            code = "missing" if "missing" in states else "unreachable" if "unreferenced" in states else None
            if source["state"] == native["state"] == "observed" and native["sha256"] != source["sha256"]:
                code = "drift"
            if code is None:
                continue
            findings.append(
                _audit_finding(
                    f"consumer_query_guidance_{code}",
                    "medium",
                    "warn",
                    {"consumer": consumer, **guidance},
                    [runtime.value],
                    "plan_sync" if code == "drift" else "source_maintenance",
                )
            )
    return findings


def _audit_remediation(findings: list[dict]) -> list[dict]:
    """Return de-duplicated, non-executable remediation records for audit findings."""
    remediation: list[dict] = []
    seen: set[tuple[str, tuple[str, ...], str]] = set()
    for finding in findings:
        key = (finding["code"], tuple(finding["affected_runtime"]), finding["remediation_kind"])
        if key in seen:
            continue
        seen.add(key)
        remediation.append(
            {
                "finding": finding["code"],
                "kind": finding["remediation_kind"],
                "affected_runtime": finding["affected_runtime"],
            }
        )
    return remediation


def _audit_status(findings: list[dict]) -> str:
    """Reduce stable finding statuses into the audit's top-level status."""
    if any(finding["status"] == "fail" for finding in findings):
        return "fail"
    if any(finding["status"] == "warn" for finding in findings):
        return "warn"
    return "pass"


def build_audit_report(runtime: Runtime | str, plugin_root: Path, since: date | None = None) -> dict:
    """Assemble the non-mutating v2 audit report from bounded local evidence.

    Args:
        runtime: A :class:`Runtime` member, or its plain value from the CLI.
        plugin_root: codemap-py's own resolved plugin root.
        since: Inclusive UTC date lower bound for telemetry evidence, if any.
    """
    requested = Runtime(runtime)
    runtimes = _runtimes_of(requested)
    root = index_paths.canonical_root()
    identity = index_paths.resolve_index(root=root)
    runtime_state = {
        one_runtime.value: _runtime_audit(_targets_for_runtime(one_runtime), root, one_runtime)
        for one_runtime in runtimes
    }
    runtime_logs, findings, timestamps, observed_records = _audit_runtime_logs(root, runtimes, since)
    findings.extend(_audit_state_findings(runtime_state, runtimes))
    findings.extend(_query_guidance_findings(runtime_state, runtimes))
    shared_index, index_findings = _audit_index_evidence(identity, runtimes, observed_records)
    usage, usage_findings = _usage_findings(observed_records, runtimes)
    findings.extend(index_findings)
    findings.extend(usage_findings)
    ordered_timestamps = sorted(timestamps)
    report = {
        "schema_version": SCHEMA_VERSION,
        "protocol": PROTOCOL_VERSION,
        "requested_runtime": requested.value,
        "window": {
            "since": since.isoformat() if since is not None else None,
            "first_ts": ordered_timestamps[0].isoformat().replace("+00:00", "Z") if ordered_timestamps else None,
            "last_ts": ordered_timestamps[-1].isoformat().replace("+00:00", "Z") if ordered_timestamps else None,
        },
        "provider": {
            "name": PROVIDER_NAME,
            "version": __version__,
            "root": str(plugin_root),
            "runtimes": runtime_state,
            "codex_rig_global_instructions": native.codex_rig_global_status() if Runtime.CODEX in runtimes else None,
        },
        "consumers": {one_runtime.value: runtime_state[one_runtime.value]["consumers"] for one_runtime in runtimes},
        "shared_index": shared_index,
        "runtime_logs": runtime_logs,
        "usage": usage,
        "findings": findings,
    }
    report["status"] = _audit_status(findings)
    report["remediation"] = _audit_remediation(findings)
    return report


def _print_audit_text(report: dict) -> None:
    """Print the compact human-readable form of one audit report."""
    print(f"status: {report['status']}")
    print(f"protocol: {report['protocol']}")
    print(f"requested_runtime: {report['requested_runtime']}")
    print(f"shared_index: {report['shared_index']['index_path']} exists={report['shared_index']['exists']}")
    for finding in report["findings"]:
        print(f"{finding['status']}: {finding['code']} ({','.join(finding['affected_runtime'])})")


def cmd_audit(ns: argparse.Namespace, plugin_root: Path) -> int:
    """Run ``integrate audit`` and map only completed audit status to its exit code."""
    report = build_audit_report(ns.runtime, plugin_root, ns.since)
    if ns.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_audit_text(report)
    return _EXIT_RUNTIME if report["status"] == "fail" else _EXIT_OK
