"""Manifest revision reading, task-selection contracts, and execution admission."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


from _bench_common.artifact_hashing import runner_sha256

from _bench_codex.structural.config import PACKAGE_DIR, PARITY_MANIFEST_PATH, RUNNER_PATH


def _manifest_revision(manifest: Mapping[str, Any]) -> str:
    """Return a non-empty experiment identity from an active manifest."""
    revision = manifest.get("experiment_revision")
    if not isinstance(revision, str) or not revision:
        raise ValueError("provider-parity execution manifest has no experiment revision")
    return revision


def _read_manifest_revision(manifest_path: Path = PARITY_MANIFEST_PATH) -> str:
    """Read the active experiment identity for planning fixture or real tasks."""
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        return _manifest_revision(manifest)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("provider-parity execution manifest is unavailable or malformed") from exc


def _task_selection_contract(manifest_path: Path) -> dict[str, Any]:
    """Read and validate the active manifest's unified task-selection contract."""
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        contract = manifest["task_selection"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("task selection contract is unavailable or malformed") from exc
    if not isinstance(contract, Mapping):
        raise ValueError("task selection contract is unavailable or malformed")
    execution_ids = contract.get("allowed_task_ids")
    allowed_families = contract.get("allowed_families")
    stage_order = contract.get("stage_order")
    stages = contract.get("stages")
    coordinate_timeout = contract.get("coordinate_timeout_seconds")
    if (
        not isinstance(execution_ids, list)
        or not execution_ids
        or not all(
            isinstance(task_id, str) and re.fullmatch(r"[A-Z]{2}-[0-9]{2}", task_id) for task_id in execution_ids
        )
        or len(execution_ids) != len(set(execution_ids))
        or not isinstance(allowed_families, list)
        or not all(isinstance(family, str) and re.fullmatch(r"[A-Z]{2}", family) for family in allowed_families)
        or len(allowed_families) != len(set(allowed_families))
        or allowed_families != list(dict.fromkeys(task_id.split("-", 1)[0] for task_id in execution_ids))
        or stage_order != ["structural", "readcrop", "fix-single", "fix-multi", "patch"]
        or not isinstance(stages, Mapping)
        or set(stages) != set(stage_order)
        or type(coordinate_timeout) is not int
        or coordinate_timeout < 1
    ):
        raise ValueError("task selection contract is unavailable or malformed")
    flattened: list[str] = []
    for stage_id in stage_order:
        stage = stages.get(stage_id)
        if not isinstance(stage, Mapping):
            raise ValueError("task selection contract is unavailable or malformed")
        task_ids = stage.get("allowed_task_ids")
        families = stage.get("allowed_families")
        arms = stage.get("arms")
        default_repetitions = stage.get("default_repetitions")
        selected_repetitions = stage.get("selected_repetitions")
        if (
            not isinstance(task_ids, list)
            or not task_ids
            or not all(isinstance(task_id, str) and re.fullmatch(r"[A-Z]{2}-[0-9]{2}", task_id) for task_id in task_ids)
            or not isinstance(families, list)
            or families != list(dict.fromkeys(task_id.split("-", 1)[0] for task_id in task_ids))
            or not isinstance(arms, list)
            or len(arms) != 3
            or len(arms) != len(set(arms))
            or type(default_repetitions) is not int
            or default_repetitions < 1
            or type(selected_repetitions) is not int
            or selected_repetitions < 1
        ):
            raise ValueError("task selection contract is unavailable or malformed")
        flattened.extend(task_ids)
    if flattened != execution_ids:
        raise ValueError("task selection contract is unavailable or malformed")
    return {
        "execution_task_ids": execution_ids,
        "allowed_families": allowed_families,
        "stage_order": stage_order,
        "stages": stages,
        "coordinate_timeout_seconds": coordinate_timeout,
    }


def _selector_tokens(value: str | Sequence[str]) -> list[str]:
    """Split comma-separated selectors while rejecting empty selector tokens."""
    values = [value] if isinstance(value, str) else list(value)
    if not values or not all(isinstance(item, str) for item in values):
        raise ValueError("at least one task selector is required")
    selectors: list[str] = []
    for raw_value in values:
        for token in raw_value.split(","):
            selector = token.strip().upper()
            if not selector:
                raise ValueError("task selectors cannot contain empty tokens")
            if selector not in selectors:
                selectors.append(selector)
    return selectors


def _targeted_scope_sha256(scope: Mapping[str, Any]) -> str:
    """Return the canonical identity of a resolved targeted benchmark scope."""
    payload = {
        key: scope[key]
        for key in (
            "manifest_sha256",
            "task_ids",
            "repetitions",
            "arms",
            "coordinate_timeout_seconds",
        )
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def resolve_task_selection(manifest_path: Path, selectors: str | Sequence[str] | None = None) -> dict[str, Any]:
    """Resolve one optional mixed selector into canonical stage partitions.

    Omitting selectors selects every supported task exactly once. Explicit selectors may mix family names and exact IDs;
    overlapping selections are deduplicated in the immutable global stage order.
    """
    manifest_path = Path(manifest_path)
    contract = _task_selection_contract(manifest_path)
    normalized = _selector_tokens(selectors) if selectors is not None else []
    task_ids = contract["execution_task_ids"]
    selected: set[str] = set(task_ids) if selectors is None else set()
    known_families = set(contract["allowed_families"])
    for selector in normalized:
        if selector in task_ids:
            selected.add(selector)
            continue
        if re.fullmatch(r"[A-Z]{2}", selector) and selector in known_families:
            selected.update(task_id for task_id in task_ids if task_id.startswith(f"{selector}-"))
            continue
        raise ValueError(f"unknown task selector {selector!r}")
    resolved_ids = [task_id for task_id in task_ids if task_id in selected]
    if not resolved_ids:
        raise ValueError("task selectors resolved to no executable tasks")
    selection_mode = "all" if selectors is None else "selected"
    stages: list[dict[str, Any]] = []
    for stage_id in contract["stage_order"]:
        stage_contract = contract["stages"][stage_id]
        stage_task_ids = [task_id for task_id in stage_contract["allowed_task_ids"] if task_id in selected]
        if not stage_task_ids:
            continue
        repetitions = (
            stage_contract[f"{selection_mode}_repetitions"]
            if selection_mode == "selected"
            else stage_contract["default_repetitions"]
        )
        arms = list(stage_contract["arms"])
        stages.append(
            {
                "stage_id": stage_id,
                "task_ids": stage_task_ids,
                "repetitions": repetitions,
                "arms": arms,
                "total_cells": len(stage_task_ids) * repetitions * len(arms),
            }
        )
    resolved = {
        "selectors": normalized,
        "task_ids": resolved_ids,
        "selection_mode": selection_mode,
        "stages": stages,
        "total_tasks": len(resolved_ids),
        "total_cells": sum(stage["total_cells"] for stage in stages),
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    }
    resolved["scope_sha256"] = hashlib.sha256(
        json.dumps(resolved, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return resolved


def _resolve_structural_task_selection(manifest_path: Path, selectors: str | Sequence[str]) -> dict[str, Any]:
    """Resolve a structural-only subset for the established structural loop."""
    unified = resolve_task_selection(manifest_path, selectors)
    nonstructural = [stage["stage_id"] for stage in unified["stages"] if stage["stage_id"] != "structural"]
    if nonstructural:
        raise ValueError("the structural loop accepts only structural task selectors")
    if not unified["stages"]:
        raise ValueError("task selectors resolved to no structural tasks")
    stage = unified["stages"][0]
    scope = {
        "selectors": unified["selectors"],
        "task_ids": stage["task_ids"],
        "study_mode": "targeted",
        "nonpoolable": True,
        "pooling_eligibility": "ineligible",
        "repetitions": stage["repetitions"],
        "arms": stage["arms"],
        "coordinate_timeout_seconds": _task_selection_contract(Path(manifest_path))["coordinate_timeout_seconds"],
        "manifest_sha256": unified["manifest_sha256"],
    }
    scope["scope_sha256"] = _targeted_scope_sha256(scope)
    return scope


def _validate_targeted_scope_request(
    scope: Mapping[str, Any],
    *,
    repetitions: int,
    arm: str,
    scope_sha256: str | None,
    dry_run: bool,
) -> None:
    """Require paid targeted invocations to match their reviewed scope exactly."""
    if repetitions != scope["repetitions"]:
        raise ValueError("targeted execution requires the scope repetition count")
    if arm != "all":
        raise ValueError("targeted execution requires --arm all")
    expected_sha = scope["scope_sha256"]
    if scope_sha256 is not None and scope_sha256 != expected_sha:
        raise ValueError("targeted execution scope SHA-256 does not match the resolved scope")
    if not dry_run and scope_sha256 is None:
        raise ValueError("paid targeted execution requires --scope-sha256 from --resolve-tasks")


def _validate_unscoped_paid_task_ids(
    manifest_path: Path,
    task_ids: list[str] | None,
    *,
    targeted: bool,
    dry_run: bool,
) -> None:
    """Allow unscoped paid task IDs only for the exact confirmatory sequence."""
    if dry_run or targeted or task_ids is None:
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        confirmatory_ids = manifest["preregistered_cells"]["structural_execution_task_ids"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("confirmatory task scope is unavailable or malformed") from exc
    if task_ids != confirmatory_ids:
        raise ValueError("paid task subsets require --tasks and its resolved scope SHA-256")


def _validate_execution_manifest(manifest_path: Path = PARITY_MANIFEST_PATH) -> None:
    """Require an active manifest locked to the exact runner implementation."""
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        revision = _manifest_revision(manifest)
        expected_runner_sha = manifest["implementation_contract"]["artifact_sha256"]["run_codex_structural"]
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("provider-parity execution manifest is unavailable or malformed") from exc
    # The entrypoint is a re-export shim: hashing it alone would let any change to the code that
    # actually runs pass this gate. The identity covers the shim and every module of its package,
    # and the manifest generator records the same value for this key.
    actual_runner_sha = runner_sha256(RUNNER_PATH, PACKAGE_DIR)
    if expected_runner_sha != actual_runner_sha:
        raise ValueError(
            f"paid execution requires a manifest locked to this runner; "
            f"revision {revision!r} records {expected_runner_sha!r}, found {actual_runner_sha!r}"
        )
