"""Offline rescoring of a completed run from its frozen inputs."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import sys

from _bench_codex import runtime
from _bench_common.provider_parity_contracts import (
    load_task_suite,
    semantic_suite_hash,
    treatment_adherence,
)

from _bench_common.artifact_hashing import runner_sha256

from _bench_codex.structural.config import CODEX_STRUCTURAL_ARMS, PACKAGE_DIR, RUNNER_PATH
from _bench_codex.structural.provenance import _index_sha
from _bench_codex.structural.scoring import (
    _arm_compliance,
    _default_evaluator,
    _locked_query_conformance,
    _locked_query_fitness,
)
from _bench_codex.structural.runner import _canonical_telemetry_path, _utc_now


def _regular_file_within(path: Path, root: Path, *, description: str) -> Path:
    """Resolve one immutable rescore input while rejecting links and scope escapes."""
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"offline rescore {description} is unavailable") from exc
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or not resolved.is_relative_to(root):
        raise ValueError(f"offline rescore {description} escaped the run directory")
    return resolved


def _load_frozen_rescore_inputs(
    run_dir: Path,
) -> tuple[dict[str, Any], Path, list[dict[str, Any]], Path, Path, str]:
    """Load all hash-verified inputs needed to replay one completed run."""
    root = run_dir.resolve(strict=True)
    metadata_candidates = sorted(root.glob("*metadata.json"))
    if len(metadata_candidates) != 1:
        raise ValueError("offline rescore requires exactly one run metadata JSON file")
    metadata_path = _regular_file_within(metadata_candidates[0], root, description="metadata")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("offline rescore metadata is not valid JSON") from exc
    if not isinstance(metadata, dict) or metadata.get("schema_version") not in {
        "codex-structural-run-metadata-v1",
        "codex-structural-run-metadata-v2",
    }:
        raise ValueError("offline rescore metadata schema is unsupported")
    if metadata.get("status") != "completed":
        raise ValueError("offline rescore requires completed run metadata")
    artifacts = metadata.get("artifacts")
    inputs = metadata.get("inputs")
    if not isinstance(artifacts, Mapping) or not isinstance(inputs, Mapping):
        raise ValueError("offline rescore metadata lacks artifact or input provenance")
    telemetry_raw = artifacts.get("telemetry_jsonl")
    snapshot = inputs.get("snapshot")
    if not isinstance(telemetry_raw, str) or not isinstance(snapshot, Mapping):
        raise ValueError("offline rescore metadata lacks frozen telemetry or snapshot")
    telemetry_recorded = Path(telemetry_raw)
    if telemetry_recorded.name != "telemetry.jsonl":
        raise ValueError("offline rescore telemetry provenance has an unexpected name")
    telemetry_path = _regular_file_within(root / telemetry_recorded.name, root, description="telemetry")
    telemetry_bytes = telemetry_path.read_bytes()
    expected_telemetry_hash = artifacts.get("telemetry_sha256")
    if (
        not isinstance(expected_telemetry_hash, str)
        or hashlib.sha256(telemetry_bytes).hexdigest() != expected_telemetry_hash
    ):
        raise ValueError("offline rescore telemetry hash mismatch")
    snapshot_raw = snapshot.get("path")
    snapshot_hash = snapshot.get("sha256")
    if not isinstance(snapshot_raw, str) or not isinstance(snapshot_hash, str):
        raise ValueError("offline rescore snapshot provenance is incomplete")
    snapshot_recorded = Path(snapshot_raw)
    if snapshot_recorded.name != "input-snapshot.json":
        raise ValueError("offline rescore snapshot provenance has an unexpected name")
    snapshot_path = _regular_file_within(
        root / "inputs" / snapshot_recorded.name,
        root,
        description="input snapshot",
    )
    snapshot_bytes = snapshot_path.read_bytes()
    if hashlib.sha256(snapshot_bytes).hexdigest() != snapshot_hash:
        raise ValueError("offline rescore input snapshot hash mismatch")
    try:
        snapshot_payload = json.loads(snapshot_bytes)
    except json.JSONDecodeError as exc:
        raise ValueError("offline rescore input snapshot is not valid JSON") from exc
    if (
        not isinstance(snapshot_payload, Mapping)
        or snapshot_payload.get("schema_version") != "codex-structural-input-snapshot-v1"
    ):
        raise ValueError("offline rescore input snapshot schema is unsupported")
    files = snapshot_payload.get("files")
    if not isinstance(files, list):
        raise ValueError("offline rescore input snapshot lacks files")
    task_entry = next(
        (entry for entry in files if isinstance(entry, Mapping) and entry.get("role") == "task_suite"), None
    )
    if task_entry is None:
        raise ValueError("offline rescore input snapshot lacks frozen task suite")
    archived_path = task_entry.get("archived_path")
    expected_task_hash = task_entry.get("sha256")
    if not isinstance(archived_path, str) or not isinstance(expected_task_hash, str):
        raise ValueError("offline rescore frozen task suite provenance is incomplete")
    tasks_path = _regular_file_within(root / "inputs" / archived_path, root, description="frozen task suite")
    if hashlib.sha256(tasks_path.read_bytes()).hexdigest() != expected_task_hash:
        raise ValueError("offline rescore frozen task suite hash mismatch")
    treatments = metadata.get("treatments")
    artifact_hashes = treatments.get("artifact_sha256") if isinstance(treatments, Mapping) else None
    skill_hash = artifact_hashes.get("codemap_query_skill") if isinstance(artifact_hashes, Mapping) else None
    if not isinstance(skill_hash, str) or not skill_hash:
        raise ValueError("offline rescore metadata lacks the frozen Codemap Skill hash")
    skill_entries = [
        entry
        for entry in files
        if isinstance(entry, Mapping)
        and entry.get("role") == "C_strict:codemap-py"
        and entry.get("sha256") == skill_hash
        and str(entry.get("archived_path", "")).endswith("/codex-skills/query-code/SKILL.md")
    ]
    if len(skill_entries) != 1:
        raise ValueError("offline rescore snapshot lacks one exact frozen Codemap Skill")
    skill_archived_path = skill_entries[0].get("archived_path")
    if not isinstance(skill_archived_path, str):
        raise ValueError("offline rescore frozen Codemap Skill provenance is incomplete")
    skill_path = _regular_file_within(
        root / "inputs" / skill_archived_path,
        root,
        description="frozen Codemap Skill",
    )
    if hashlib.sha256(skill_path.read_bytes()).hexdigest() != skill_hash:
        raise ValueError("offline rescore frozen Codemap Skill hash mismatch")
    return metadata, telemetry_path, load_task_suite(tasks_path), metadata_path, skill_path, skill_hash


def rescore_results(run_dir: Path) -> Path:
    """Replay frozen raw telemetry and current evaluators into an immutable offline artifact.

    The function accepts only a completed run directory. It never invokes a provider, reads credentials, or rewrites raw
    telemetry, canonical telemetry, metadata, or frozen input snapshots.
    """
    root = Path(run_dir).resolve(strict=True)
    metadata, telemetry_path, tasks, metadata_path, skill_path, skill_sha256 = _load_frozen_rescore_inputs(root)
    task_by_id = {str(task["id"]): task for task in tasks}
    execution = metadata.get("execution")
    if not isinstance(execution, Mapping):
        raise ValueError("offline rescore metadata lacks execution scope")
    selected_ids = execution.get("selected_task_ids")
    coordinates = execution.get("coordinates")
    if not isinstance(selected_ids, list) or not all(isinstance(task_id, str) for task_id in selected_ids):
        raise ValueError("offline rescore selected task scope is invalid")
    if set(selected_ids) - task_by_id.keys() or not isinstance(coordinates, list):
        raise ValueError("offline rescore task scope disagrees with frozen suite")
    allowed_coordinates: set[tuple[str, int, str]] = set()
    for coordinate in coordinates:
        if not isinstance(coordinate, Mapping):
            raise ValueError("offline rescore coordinate scope is invalid")
        task_id = coordinate.get("task_id")
        repetition = coordinate.get("repetition")
        arm = coordinate.get("arm")
        if (
            not isinstance(task_id, str)
            or task_id not in selected_ids
            or isinstance(repetition, bool)
            or not isinstance(repetition, int)
            or repetition < 1
            or arm not in CODEX_STRUCTURAL_ARMS
        ):
            raise ValueError("offline rescore coordinate scope is invalid")
        allowed_coordinates.add((task_id, repetition, arm))
    if len(allowed_coordinates) != len(coordinates):
        raise ValueError("offline rescore coordinate scope is invalid")
    telemetry_bytes = telemetry_path.read_bytes()
    source = {
        "metadata_sha256": hashlib.sha256(metadata_path.read_bytes()).hexdigest(),
        "telemetry_sha256": hashlib.sha256(telemetry_bytes).hexdigest(),
        "frozen_suite_semantic_sha256": semantic_suite_hash(tasks),
        # Shim plus package: the entrypoint's own bytes no longer describe what ran.
        "runner_sha256": runner_sha256(RUNNER_PATH, PACKAGE_DIR),
    }
    rows: list[dict[str, Any]] = []
    seen_coordinates: set[tuple[Any, Any, Any]] = set()
    for line in telemetry_bytes.decode("utf-8").splitlines():
        try:
            raw_row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError("offline rescore telemetry is not valid JSONL") from exc
        if not isinstance(raw_row, Mapping):
            raise ValueError("offline rescore telemetry row is not an object")
        task_id = raw_row.get("task_id")
        arm = raw_row.get("arm")
        repetition = raw_row.get("repetition", 1)
        coordinate = (task_id, repetition, arm)
        if (
            not isinstance(task_id, str)
            or task_id not in task_by_id
            or coordinate not in allowed_coordinates
            or coordinate in seen_coordinates
        ):
            raise ValueError("offline rescore telemetry row is outside frozen execution scope")
        raw_events = raw_row.get("raw_events")
        if not isinstance(raw_events, list) or not all(isinstance(event, Mapping) for event in raw_events):
            raise ValueError("offline rescore telemetry row lacks replayable raw events")
        seen_coordinates.add(coordinate)
        parsed = runtime.parse_codex_jsonl(
            (json.dumps(event, sort_keys=True) for event in raw_events),
            skill_path=skill_path if arm == "C_strict" else None,
            skill_sha256=skill_sha256 if arm == "C_strict" else "",
        )
        evaluation = _default_evaluator(task_by_id[task_id], parsed.output_text)
        compliance = _arm_compliance(str(arm), parsed)
        contaminated = arm == "A_plain" and parsed.codemap_observed_calls > 0
        row = {
            "task_id": task_id,
            "repetition": repetition,
            "arm": arm,
            "output_text": parsed.output_text,
            "success": parsed.success,
            "incomplete": parsed.incomplete,
            "error": parsed.error,
            "error_type": parsed.error_type,
            "quality_score": evaluation.quality_score if evaluation.scored else None,
            "quality_components": evaluation.components,
            "correct": evaluation.correct,
            "extraction_failed": evaluation.extraction_failed,
            "compliance": compliance,
            "locked_query_conformance": _locked_query_conformance(task_by_id[task_id], str(arm), parsed),
            "contaminated": contaminated,
            "treatment_adherence": treatment_adherence(
                str(arm),
                codemap_use_compliance=compliance,
                contaminated=contaminated,
            ),
            "codemap_calls": parsed.codemap_calls,
            "codemap_observed_calls": parsed.codemap_observed_calls,
            "codemap_successful_calls": parsed.codemap_successful_calls,
            "codemap_direct_compact_successful_calls": parsed.codemap_direct_compact_successful_calls,
            "codemap_skill_compact_successful_calls": parsed.codemap_skill_compact_successful_calls,
            "skill_delivery_observed": parsed.skill_delivery_observed,
            "successful_query_arguments": parsed.successful_query_arguments,
            "raw_events_sha256": hashlib.sha256(
                json.dumps(raw_events, separators=(",", ":"), sort_keys=True).encode("utf-8")
            ).hexdigest(),
        }
        locked_query_fitness = _locked_query_fitness(task_by_id[task_id], str(arm), parsed)
        row.update(
            {
                "locked_query_fitness": locked_query_fitness.overall if locked_query_fitness is not None else None,
                "locked_query_endpoint_fitness": (
                    locked_query_fitness.endpoint if locked_query_fitness is not None else None
                ),
                "locked_query_target_fitness": locked_query_fitness.target
                if locked_query_fitness is not None
                else None,
                "locked_query_option_fitness": locked_query_fitness.options
                if locked_query_fitness is not None
                else None,
            }
        )
        rows.append(row)
    if seen_coordinates != allowed_coordinates:
        raise ValueError("offline rescore telemetry is incomplete for the frozen execution scope")
    payload = {
        "schema_version": "codex-structural-offline-rescore-v2",
        "source": source,
        "rows": rows,
    }
    serialized_without_hash = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    payload["derived_sha256"] = hashlib.sha256(serialized_without_hash).hexdigest()
    serialized = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    artifact_path = root / (f"offline-rescore-v2-{source['telemetry_sha256'][:16]}-{source['runner_sha256'][:16]}.json")
    if artifact_path.exists():
        if artifact_path.read_bytes() != serialized:
            raise ValueError("offline rescore artifact already exists with different derived content")
        return artifact_path
    artifact_path.write_bytes(serialized)
    return artifact_path


def _initial_run_metadata(
    *,
    manifest_path: Path,
    repo_path: Path,
    index_path: Path | None,
    output_path: Path,
    metadata_path: Path,
    model: str,
    reasoning_effort: str,
    repetitions: int,
    task_arms: Mapping[tuple[str, int], tuple[str, ...]],
    cell_wall_clock_seconds: float,
    auth_provisioned: bool,
    input_snapshot: Mapping[str, Any] | None = None,
    study_mode: str = "confirmatory",
    targeted_scope: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build complete non-secret provenance for one paid structural run."""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    scope = dict(targeted_scope) if targeted_scope is not None else None
    coordinates = [
        {"task_id": task_id, "repetition": repetition, "arm": arm}
        for (task_id, repetition), arms in task_arms.items()
        for arm in arms
    ]
    return {
        "schema_version": "codex-structural-run-metadata-v2",
        "status": "running",
        "started_at": _utc_now(),
        "completed_at": None,
        "persisted_cells": 0,
        "cell_outcomes": {
            "successful": 0,
            "unsuccessful": 0,
            "unscoreable": 0,
            "incomplete": 0,
            "extraction_failed": 0,
            "contaminated": 0,
            "compliance_failed": 0,
            "locked_query_nonconforming": 0,
            "targeted": 0,
            "token_accounting_inconsistent": 0,
        },
        "last_persisted_coordinate": None,
        "error": None,
        "auth_provisioned": auth_provisioned,
        "auth_source_recorded": False,
        "manifest": {
            "path": str(manifest_path.resolve()),
            "sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "experiment_id": manifest["experiment_id"],
            "experiment_revision": manifest["experiment_revision"],
        },
        "execution": {
            "study_mode": study_mode,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "repetitions": repetitions,
            "arms": list(CODEX_STRUCTURAL_ARMS),
            "planned_cells": len(coordinates),
            "selected_task_ids": list(dict.fromkeys(task_id for task_id, _ in task_arms)),
            "targeted_scope": scope,
            "targeted_scope_sha256": scope["scope_sha256"] if scope is not None else None,
            "coordinates": coordinates,
            "cell_wall_clock_seconds": cell_wall_clock_seconds,
            "python": sys.version,
            "codex_cli": {
                "reviewed": manifest["codex_cli"],
                "observed_version": os.environ.get("CODEX_CLI_OBSERVED_VERSION"),
            },
        },
        "inputs": {
            "target_path": str(repo_path.resolve()),
            "target": manifest["target_source"],
            "index_path": str(index_path.resolve()) if index_path is not None else None,
            "index_sha256": _index_sha(index_path),
            "index": manifest["index"],
            "suite_integrity": manifest["suite_integrity"],
            "snapshot": dict(input_snapshot) if input_snapshot is not None else None,
        },
        "treatments": {
            "arms": manifest["arms"],
            "package_roster": manifest["package_roster"],
            "codemap_candidate": manifest["codemap_candidate"],
            "codex_rig_candidate": manifest["codex_rig_candidate"],
            "artifact_sha256": manifest["artifact_sha256"],
            "permission_profiles": manifest["codex_permission_profiles"],
            "telemetry_admission": manifest["telemetry_admission"],
        },
        "artifacts": {
            "telemetry_jsonl": str(output_path.resolve()),
            "telemetry_canonical_jsonl": str(_canonical_telemetry_path(output_path).resolve()),
            "canonical_telemetry_status": "not_written",
            "canonical_telemetry_pooling_eligible": False,
            "canonical_telemetry_pooling_ineligibility_reasons": [],
            "run_metadata": str(metadata_path.resolve()),
        },
    }
