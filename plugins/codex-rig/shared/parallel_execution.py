"""Validate truthful staged parallel-execution manifests.

## Purpose

Admit a stable prefix of frozen child token reservations before dispatch, and turn recorded execution-manifest
evidence into one fail-closed acceptance summary. The validator makes a claimed parallel run structurally coherent only
when its manifest contains the required provenance, controls, completed outputs, parent joins, and overlap. A consuming
workflow must separately bind recorded events and controls to authoritative host observations before calling them
runtime-proven.

## Scope

Validate a positive parent-owned wave ceiling and per-node reservations, then validate schema-v1 manifests
supplied by workflow owners. It checks serial stage barriers, deterministic integration order, role and context digests,
selected attempts, output digests, bounded retries, node controls, exact frozen-plan approval for every write, and
concurrent write ownership. ``validate_read_only_runtime`` requires schema-v2 capability evidence and binds a frozen
parent-owned plan, redacted parent spawn evidence, child lineage, declared controls, terminal completion, actual child
timing, and literal portable-read-restricted host facts. Generic unbound evidence remains readable but is ineligible for
promotion; a promoted result requires an exact closed consumer policy. It does not dispatch or terminate agents, enforce
provider token usage, create worktrees, alter repository files, infer missing host events, or decide a workflow's final
user-facing result.

## Usage

Call ``admit_wave_token_budget`` with the frozen ceiling, stable node order, reservations, and completed/active
state before any spawn. Import ``validate_execution_manifest`` and pass the decoded manifest plus the run directory
holding context/output evidence and the installed roles directory holding ``<role_id>/ROLE.md``. Implement, Manage, and
Code Review use the executable ``preflight`` or ``validate-runtime`` commands so promotion is derived from a closed
consumer allowlist, the exact plan and write approval are bound before mutation, and post-join evidence carries a
mandatory consumer id. Callers must treat a nonzero exit or ``ValueError`` as a failed contract, preserve the manifest
for diagnosis, and never substitute a child response for required evidence.

## Outputs

A budget return identifies admitted, completed, active, and serial-replan nodes plus reservation totals and the
explicit non-enforced provider-cap boundary. A successful manifest return has ``acceptance_blocked``, ``actual_mode``,
and ``integration_order``. A portable runtime return adds the manifest digest, safe spawn and parent join projections,
``portable-read-restricted`` evidence level, literal ``network_mode=restricted`` and ``approval_policy=never``, and
``filesystem_credential_isolation=unverified``; it never returns raw prompts, raw spawn arguments, or child messages. No
files are written.

## Failure

Invalid ceilings, reservations, state overlaps, or already-exceeded admission state raise stable ``ValueError``
messages. Unsupported schemas, malformed identifiers or paths, DAG gaps/cycles, missing serial stage barriers,
mismatched digests, unenforced recorded controls, missing or invalid write authority, overlapping write owners or locks,
invalid retries, false parallel claims, and joins before a real terminal event also fail closed. Runtime validation
additionally rejects rollout-shape drift, ambiguous lineage, timing mismatch, sensitive output, control disagreement,
malformed consumer or capability evidence, or external-network response items. ``cancel_requested`` is not terminal.

## Used by

Codex Rig workflow runtimes and their installed-package tests use this helper before integrating specialist
outputs. Read-only pilots use the same validation path as later isolated write workflows, keeping the safety contract
local to one shipped stdlib module rather than distributing policy through registries or runtime-specific wrappers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from copy import deepcopy
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any


SCHEMA_VERSION = 1
RUNTIME_SCHEMA_VERSION = 2
_MODES = {"parallel", "independent-spawned", "serial", "serial-fallback"}
_TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
_TRANSIENT_ERRORS = {"rate_limited", "timeout", "transport_error"}
_EXTERNAL_NETWORK_PREFIXES = ("app", "browser", "connector", "mcp", "network", "search", "web")
_LOCAL_RESPONSE_ITEM_TYPES = {
    "agent_message",
    "message",
    "reasoning",
    "function_call_output",
    "custom_tool_call_output",
}
_LOCAL_RESPONSE_CALL_NAMES = {"exec"}
_PROMOTED_PORTABLE_READ_CONSUMERS = frozenset({"code-review", "implement", "manage"})
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)authorization\s*:\s*bearer\s+\S+"),
    re.compile(r"\b(?:gh[pousr]|github_pat)_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
)


def resolve_execution_mode(
    explicit: str | None,
    *,
    environment: Mapping[str, str],
    read_parallel_promoted: bool,
    write_parallel_promoted: bool,
) -> dict[str, str | bool]:
    """Resolve one execution request without granting write approval.

    Explicit ``--execution=...`` input overrides ``CODEX_RIG_EXECUTION``. The shipped default is ``auto``. Unpromoted
    explicit modes fail closed, while ``auto`` safely resolves to serial until read-only parallelism is promoted.
    """
    if explicit is not None:
        prefix = "--execution="
        if not explicit.startswith(prefix) or explicit.count("=") != 1:
            raise ValueError("execution-flag-invalid")
        requested = explicit.removeprefix(prefix)
        source = "explicit"
    elif "CODEX_RIG_EXECUTION" in environment:
        requested = environment["CODEX_RIG_EXECUTION"]
        source = "environment"
    else:
        requested = "auto"
        source = "shipped-default"
    if requested not in {"auto", "serial", "parallel-read", "parallel-write"}:
        raise ValueError("execution-mode-invalid")
    if requested == "parallel-read" and not read_parallel_promoted:
        raise ValueError("parallel-read-not-promoted")
    if requested == "parallel-write" and not write_parallel_promoted:
        raise ValueError("parallel-write-not-promoted")
    if requested == "auto":
        effective = "parallel-read" if read_parallel_promoted else "serial"
    else:
        effective = requested
    return {
        "effective_mode": effective,
        "requested_mode": requested,
        "source": source,
        "write_approval_required": effective == "parallel-write",
    }


def admit_wave_token_budget(
    *,
    ceiling_tokens: int,
    node_order: Sequence[str],
    reservations: Mapping[str, int],
    completed_node_ids: Sequence[str],
    active_node_ids: Sequence[str],
) -> dict[str, object]:
    """Admit a stable node prefix without exceeding frozen token reservations.

    The returned decision governs dispatch admission only. It preserves existing work and routes the first non-fitting
    node plus every later unstarted node to same-gate serial re-planning. Provider usage can exceed a reservation
    because the current host does not expose an enforceable per-child token cap; telemetry must report that difference
    separately.
    """
    if not isinstance(ceiling_tokens, int) or isinstance(ceiling_tokens, bool) or ceiling_tokens <= 0:
        raise ValueError("token-budget-ceiling-invalid")
    if not isinstance(node_order, Sequence) or isinstance(node_order, (str, bytes)):
        raise ValueError("token-budget-node-order-invalid")
    ordered_nodes = list(node_order)
    if (
        not ordered_nodes
        or any(not isinstance(node_id, str) or not node_id.strip() for node_id in ordered_nodes)
        or len(set(ordered_nodes)) != len(ordered_nodes)
    ):
        raise ValueError("token-budget-node-order-invalid")
    if not isinstance(reservations, Mapping):
        raise ValueError("token-reservations-invalid")
    if set(reservations) != set(ordered_nodes):
        raise ValueError("token-reservation-node-mismatch")
    for node_id in ordered_nodes:
        reservation = reservations[node_id]
        if not isinstance(reservation, int) or isinstance(reservation, bool) or reservation <= 0:
            raise ValueError(f"token-reservation-invalid:{node_id}")

    def state_nodes(values: Sequence[str], label: str) -> set[str]:
        """Validate one completed-or-active state list against the frozen order."""
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
            raise ValueError(f"token-budget-{label}-invalid")
        materialized = list(values)
        if (
            any(not isinstance(node_id, str) or not node_id.strip() for node_id in materialized)
            or len(set(materialized)) != len(materialized)
            or not set(materialized).issubset(ordered_nodes)
        ):
            raise ValueError(f"token-budget-{label}-invalid")
        return set(materialized)

    completed = state_nodes(completed_node_ids, "completed-state")
    active = state_nodes(active_node_ids, "active-state")
    overlap = completed.intersection(active)
    if overlap:
        raise ValueError(f"token-budget-state-overlap:{min(overlap)}")

    existing = completed.union(active)
    expected_existing = set(ordered_nodes[: len(existing)])
    if existing != expected_existing:
        raise ValueError("token-budget-existing-state-not-prefix")
    reserved_tokens = sum(reservations[node_id] for node_id in existing)
    if reserved_tokens > ceiling_tokens:
        raise ValueError("token-budget-already-exceeded")

    dispatch: list[str] = []
    serial_replan: list[str] = []
    exhausted = False
    for node_id in ordered_nodes:
        if node_id in existing:
            continue
        reservation = reservations[node_id]
        if exhausted or reserved_tokens + reservation > ceiling_tokens:
            exhausted = True
            serial_replan.append(node_id)
            continue
        dispatch.append(node_id)
        reserved_tokens += reservation

    return {
        "schema_version": 1,
        "enforcement_scope": "pre-dispatch-reservations",
        "ceiling_tokens": ceiling_tokens,
        "reserved_tokens": reserved_tokens,
        "remaining_tokens": ceiling_tokens - reserved_tokens,
        "dispatch_node_ids": dispatch,
        "completed_node_ids": [node_id for node_id in ordered_nodes if node_id in completed],
        "active_node_ids": [node_id for node_id in ordered_nodes if node_id in active],
        "serial_replan_node_ids": serial_replan,
        "exhausted": exhausted,
        "active_child_policy": "await-terminal-evidence",
        "completed_work_policy": "preserve",
        "unfinished_work_policy": "serial-replan-same-gates",
        "provider_usage_cap_enforced": False,
    }


def _object(value: object, label: str) -> dict[str, Any]:
    """Return one JSON object or reject a malformed manifest field."""
    if not isinstance(value, dict):
        raise ValueError(f"{label}-object-required")
    return value


def _text(value: object, label: str) -> str:
    """Return one non-empty text field without changing its recorded value."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}-required")
    return value


def _string_list(value: object, label: str) -> list[str]:
    """Return a duplicate-free string list required by the manifest schema."""
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{label}-invalid")
    if len(set(value)) != len(value):
        raise ValueError(f"{label}-duplicate")
    return value


def _sha256(path: Path, label: str, node_id: str, expected: object) -> None:
    """Bind one evidence file to its declared SHA-256 digest."""
    if not isinstance(expected, str) or re.fullmatch(r"[0-9a-f]{64}", expected) is None:
        raise ValueError(f"{label}-hash-mismatch:{node_id}")
    try:
        observed = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        raise ValueError(f"{label}-missing:{node_id}") from error
    if observed != expected:
        raise ValueError(f"{label}-hash-mismatch:{node_id}")


def _relative_path(root: Path, value: object, label: str, node_id: str) -> Path:
    """Resolve a portable evidence path while rejecting traversal and absolute forms."""
    text = _text(value, f"{label}-path")
    pure_path = PurePosixPath(text.replace("\\", "/"))
    windows_path = PureWindowsPath(text)
    if pure_path.is_absolute() or windows_path.is_absolute() or ".." in pure_path.parts or ".." in windows_path.parts:
        raise ValueError(f"{label}-path-invalid:{node_id}")
    candidate = (root / pure_path).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as error:
        raise ValueError(f"{label}-path-invalid:{node_id}") from error
    return candidate


def _event(value: object, label: str, node_id: str) -> int:
    """Return one parent event sequence after validating its observable identity."""
    event = _object(value, label)
    _text(event.get("event_id"), f"{label}-id")
    sequence = event.get("sequence")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence <= 0:
        raise ValueError(f"{label}-sequence-invalid:{node_id}")
    return sequence


def _normalized_paths(value: object, label: str, node_id: str) -> list[str]:
    """Return case-stable portable paths after rejecting aliases and patterns."""
    paths = _string_list(value, label)
    normalized: list[str] = []
    for path in paths:
        windows_path = PureWindowsPath(path)
        portable = PurePosixPath(path.replace("\\", "/"))
        if portable.is_absolute() or windows_path.is_absolute() or ".." in portable.parts or ".." in windows_path.parts:
            raise ValueError(f"{label}-invalid:{node_id}")
        candidate = portable.as_posix().removeprefix("./").rstrip("/").casefold()
        if not candidate or candidate == "." or any(character in candidate for character in "*?[]"):
            raise ValueError(f"{label}-invalid:{node_id}")
        normalized.append(candidate)
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{label}-duplicate:{node_id}")
    return normalized


def _validate_controls(node: dict[str, Any], node_id: str) -> list[str]:
    """Require observed least-privilege controls and return normalized owned paths."""
    requested = _object(node.get("requested_controls"), "requested-controls")
    observed = _object(node.get("observed_controls"), "observed-controls")
    if observed.get("enforced") is not True:
        raise ValueError(f"capability-enforcement-required:{node_id}")
    sandbox_mode = requested.get("sandbox_mode")
    write_paths = _string_list(requested.get("write_paths"), "requested-write-paths")
    if sandbox_mode not in {"read-only", "workspace-write"}:
        raise ValueError(f"sandbox-mode-invalid:{node_id}")
    if requested.get("network") is not False or requested.get("credentials") is not False:
        raise ValueError(f"capability-request-invalid:{node_id}")
    if any(
        observed.get(field) != requested.get(field)
        for field in ("sandbox_mode", "write_paths", "network", "credentials")
    ):
        raise ValueError(f"capability-observation-mismatch:{node_id}")
    mutation = node.get("mutation")
    owned_paths = _normalized_paths(node.get("owned_paths"), "owned-paths", node_id)
    normalized_write_paths = _normalized_paths(write_paths, "requested-write-paths", node_id)
    if mutation == "read-only" and (sandbox_mode != "read-only" or write_paths or owned_paths):
        raise ValueError(f"read-only-controls-invalid:{node_id}")
    if mutation == "write" and (
        sandbox_mode != "workspace-write" or normalized_write_paths != owned_paths or not owned_paths
    ):
        raise ValueError(f"write-controls-invalid:{node_id}")
    if mutation not in {"read-only", "write"}:
        raise ValueError(f"mutation-invalid:{node_id}")
    return owned_paths


def _validate_resource_locks(value: object, node_id: str) -> list[str]:
    """Return normalized resource locks from the small portable vocabulary."""
    locks = _string_list(value, "resource-locks")
    normalized: list[str] = []
    for lock in locks:
        if lock == "git-index":
            normalized.append(lock)
            continue
        prefix, separator, scope = lock.partition(":")
        if not separator or not scope or prefix not in {"database", "port", "gpu", "cache", "generated", "test-env"}:
            raise ValueError(f"resource-lock-invalid:{node_id}")
        if prefix == "port":
            if not scope.isdigit() or not 1 <= int(scope) <= 65535:
                raise ValueError(f"resource-lock-invalid:{node_id}")
            normalized.append(f"port:{int(scope)}")
        elif prefix in {"cache", "generated"}:
            normalized_scope = _normalized_paths([scope], "resource-lock", node_id)[0]
            normalized.append(f"{prefix}:{normalized_scope}")
        elif re.fullmatch(r"[A-Za-z0-9_.-]+", scope) is None:
            raise ValueError(f"resource-lock-invalid:{node_id}")
        else:
            normalized.append(f"{prefix}:{scope.casefold()}")
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"resource-lock-invalid:{node_id}")
    return normalized


def _validate_attempts(node: dict[str, Any], node_id: str, run_dir: Path) -> tuple[bool, int | None, int | None]:
    """Validate attempts and return substantive completion plus its event interval."""
    attempts_value = node.get("attempts")
    if not isinstance(attempts_value, list) or not attempts_value or len(attempts_value) > 2:
        raise ValueError(f"attempts-invalid:{node_id}")
    attempts = [_object(item, "attempt") for item in attempts_value]
    for number, attempt in enumerate(attempts, start=1):
        if attempt.get("attempt") != number:
            raise ValueError(f"attempt-index-invalid:{node_id}")
    first_terminal: int | None = None
    if len(attempts) == 2:
        first = attempts[0]
        if first.get("status") != "failed" or first.get("error_type") not in _TRANSIENT_ERRORS:
            raise ValueError(f"invalid-retry:{node_id}")
        first_start = _event(first.get("start_event"), "start-event", node_id)
        first_terminal = _event(first.get("terminal_event"), "terminal-event", node_id)
        if (
            first_terminal <= first_start
            or first.get("output_path") is not None
            or first.get("output_sha256") is not None
        ):
            raise ValueError(f"invalid-retry:{node_id}")
    selected = node.get("selected_attempt")
    if selected != len(attempts):
        raise ValueError(f"selected-attempt-invalid:{node_id}")
    selected_attempt = attempts[-1]
    status = selected_attempt.get("status")
    if status not in {*_TERMINAL_STATUSES, "cancel_requested"}:
        raise ValueError(f"attempt-status-invalid:{node_id}")
    start = _event(selected_attempt.get("start_event"), "start-event", node_id)
    if first_terminal is not None and start <= first_terminal:
        raise ValueError(f"invalid-retry:{node_id}")
    terminal_value = selected_attempt.get("terminal_event")
    if status == "cancel_requested":
        if terminal_value is not None:
            raise ValueError(f"cancel-requested-terminal:{node_id}")
        return False, start, None
    terminal = _event(terminal_value, "terminal-event", node_id)
    if terminal <= start:
        raise ValueError(f"terminal-before-start:{node_id}")
    if status != "completed":
        return False, start, terminal
    output = _relative_path(run_dir, selected_attempt.get("output_path"), "output", node_id)
    try:
        if not output.is_file() or not output.read_bytes():
            raise ValueError(f"output-not-substantive:{node_id}")
    except OSError as error:
        raise ValueError(f"output-missing:{node_id}") from error
    _sha256(output, "output", node_id, selected_attempt.get("output_sha256"))
    if node.get("verifier_status") != "passed" or node.get("unresolved") != []:
        raise ValueError(f"output-verification-required:{node_id}")
    return True, start, terminal


def _overlap(left: tuple[int, int], right: tuple[int, int]) -> bool:
    """Return whether two substantive parent-observed intervals overlap strictly."""
    return left[0] < right[1] and right[0] < left[1]


def _paths_overlap(left: str, right: str) -> bool:
    """Return whether two portable owned paths are equal or ancestor-related."""
    left_parts = PurePosixPath(left).parts
    right_parts = PurePosixPath(right).parts
    common = min(len(left_parts), len(right_parts))
    return left_parts[:common] == right_parts[:common]


def _topological_stage_ids(stages: list[dict[str, Any]]) -> list[str]:
    """Return lexical deterministic stage order after validating the complete DAG."""
    stage_ids: list[str] = []
    dependencies: dict[str, list[str]] = {}
    for stage in stages:
        stage_id = _text(stage.get("stage_id"), "stage-id")
        if stage_id in dependencies:
            raise ValueError(f"stage-id-duplicate:{stage_id}")
        stage_ids.append(stage_id)
        dependencies[stage_id] = _string_list(stage.get("depends_on"), "stage-depends-on")
    known = set(stage_ids)
    for stage_id, required in dependencies.items():
        for dependency in required:
            if dependency not in known:
                raise ValueError(f"stage-dependency-missing:{stage_id}")
    ordered: list[str] = []
    remaining = {stage_id: set(required) for stage_id, required in dependencies.items()}
    while remaining:
        ready = sorted(stage_id for stage_id, required in remaining.items() if not required)
        if not ready:
            raise ValueError("stage-dependency-cycle")
        for stage_id in ready:
            ordered.append(stage_id)
            remaining.pop(stage_id)
        completed = set(ready)
        for required in remaining.values():
            required.difference_update(completed)
    return ordered


def _validate_exact_write_approval(evidence: Mapping[str, Any], plan_sha256: object) -> None:
    """Require one exact human approval bound to frozen plan bytes."""
    if (
        set(evidence) != {"plan_sha256", "response", "source"}
        or evidence.get("plan_sha256") != plan_sha256
        or evidence.get("response") != "approve"
        or evidence.get("source") not in {"explicit-input", "user-prompt"}
    ):
        raise ValueError("write-approval-invalid")


def _validate_write_approval(manifest: dict[str, Any], has_writes: bool) -> None:
    """Require exact frozen-plan approval whenever any schema-v1 node writes."""
    approval = manifest.get("write_approval")
    if not has_writes:
        if approval is not None and not isinstance(approval, dict):
            raise ValueError("write-approval-invalid")
        return
    if approval is None:
        raise ValueError("write-approval-required")
    evidence = _object(approval, "write-approval")
    _validate_exact_write_approval(evidence, manifest.get("plan_sha256"))


def validate_execution_manifest(manifest: dict[str, object], *, run_dir: Path, roles_dir: Path) -> dict[str, object]:
    """Validate schema-v1 execution evidence and derive its acceptance summary."""
    payload = _object(manifest, "manifest")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported-schema-version")
    _text(payload.get("run_id"), "run-id")
    plan_sha256 = payload.get("plan_sha256")
    if not isinstance(plan_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", plan_sha256) is None:
        raise ValueError("plan-sha256-invalid")
    claimed_mode = payload.get("claimed_mode")
    if claimed_mode not in _MODES:
        raise ValueError("claimed-mode-invalid")
    limit = payload.get("configured_limit")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 4:
        raise ValueError("configured-limit-invalid")
    stages_value = payload.get("stages")
    if not isinstance(stages_value, list) or not stages_value:
        raise ValueError("stages-required")
    stages = [_object(item, "stage") for item in stages_value]
    stage_order = _topological_stage_ids(stages)
    by_stage = {_text(stage.get("stage_id"), "stage-id"): stage for stage in stages}
    for previous_stage, stage_id in zip(stage_order, stage_order[1:], strict=False):
        if previous_stage not in _string_list(by_stage[stage_id].get("depends_on"), "stage-depends-on"):
            raise ValueError(f"stage-barrier-required:{stage_id}")

    seen_nodes: set[str] = set()
    seen_waves: set[str] = set()
    seen_contexts: set[Path] = set()
    seen_outputs: set[Path] = set()
    completed_by_stage: dict[str, list[tuple[str, int, int]]] = {}
    joins_by_stage: dict[str, list[int]] = {}
    owned_paths_by_node: dict[str, list[str]] = {}
    resource_locks_by_node: dict[str, list[str]] = {}
    acceptance_blocked = False
    write_node_ids: set[str] = set()
    for stage_id in stage_order:
        stage = by_stage[stage_id]
        wave_id = _text(stage.get("wave_id"), "wave-id")
        if wave_id in seen_waves:
            raise ValueError(f"wave-id-duplicate:{wave_id}")
        seen_waves.add(wave_id)
        nodes_value = stage.get("nodes")
        if not isinstance(nodes_value, list) or not nodes_value:
            raise ValueError(f"stage-nodes-required:{stage_id}")
        if len(nodes_value) > limit:
            raise ValueError(f"configured-limit-exceeded:{stage_id}")
        nodes = [_object(item, "node") for item in nodes_value]
        node_records: list[tuple[str, dict[str, Any], bool, int | None, int | None, int | None]] = []
        for node in nodes:
            node_id = _text(node.get("node_id"), "node-id")
            if node_id in seen_nodes:
                raise ValueError(f"node-id-duplicate:{node_id}")
            seen_nodes.add(node_id)
            role_id = _text(node.get("role_id"), "role-id")
            role_path = _relative_path(roles_dir, f"{role_id}/ROLE.md", "role-card", node_id)
            _sha256(role_path, "role-card", node_id, node.get("role_card_sha256"))
            context_path = _relative_path(run_dir, node.get("context_path"), "context", node_id)
            if context_path in seen_contexts:
                raise ValueError(f"context-path-duplicate:{node_id}")
            seen_contexts.add(context_path)
            _sha256(context_path, "context", node_id, node.get("context_sha256"))
            context_text = context_path.read_text(encoding="utf-8")
            if any(pattern.search(context_text) for pattern in _SECRET_PATTERNS):
                raise ValueError(f"context-sensitive-material:{node_id}")
            owned_paths_by_node[node_id] = _validate_controls(node, node_id)
            resource_locks_by_node[node_id] = _validate_resource_locks(node.get("resource_locks"), node_id)
            if node.get("mutation") == "write":
                write_node_ids.add(node_id)
            substantive, start, terminal = _validate_attempts(node, node_id, run_dir)
            join_value = node.get("join_event")
            if terminal is None:
                acceptance_blocked = True
                if join_value is not None:
                    raise ValueError(f"cancel-requested-joined:{node_id}")
                node_records.append((node_id, node, substantive, start, terminal, None))
                continue
            if join_value is None:
                acceptance_blocked = True
                node_records.append((node_id, node, substantive, start, terminal, None))
                continue
            join = _event(join_value, "join-event", node_id)
            if join <= terminal:
                raise ValueError(f"join-before-terminal:{node_id}")
            if substantive:
                output_path = _relative_path(run_dir, node["attempts"][-1].get("output_path"), "output", node_id)
                if output_path in seen_outputs:
                    raise ValueError(f"output-path-duplicate:{node_id}")
                seen_outputs.add(output_path)
            else:
                acceptance_blocked = True
            node_records.append((node_id, node, substantive, start, terminal, join))

        for dependency in _string_list(stage.get("depends_on"), "stage-depends-on"):
            dependency_joins = joins_by_stage.get(dependency, [])
            if len(dependency_joins) != len(by_stage[dependency].get("nodes", [])):
                raise ValueError(f"stage-dependency-unjoined:{stage_id}")
            for _, _, _, start, _, _ in node_records:
                if start is not None and any(start <= join for join in dependency_joins):
                    raise ValueError(f"stage-start-before-dependency-join:{stage_id}")

        write_nodes = [node_id for node_id, node, _, _, _, _ in node_records if node.get("mutation") == "write"]
        for index, left_id in enumerate(write_nodes):
            left_paths = owned_paths_by_node[left_id]
            left_locks = set(resource_locks_by_node[left_id])
            for right_id in write_nodes[index + 1 :]:
                right_paths = owned_paths_by_node[right_id]
                if any(_paths_overlap(left, right) for left in left_paths for right in right_paths):
                    raise ValueError(f"write-ownership-overlap:{left_id}:{right_id}")
                if left_locks.intersection(resource_locks_by_node[right_id]):
                    raise ValueError(f"resource-lock-overlap:{left_id}:{right_id}")

        completed_by_stage[stage_id] = [
            (node_id, start, terminal)
            for node_id, _, substantive, start, terminal, join in node_records
            if substantive and start is not None and terminal is not None and join is not None
        ]
        joins_by_stage[stage_id] = [join for _, _, _, _, _, join in node_records if join is not None]

    overlapping_pairs = [
        (left[0], right[0])
        for stage_intervals in completed_by_stage.values()
        for index, left in enumerate(stage_intervals)
        for right in stage_intervals[index + 1 :]
        if _overlap((left[1], left[2]), (right[1], right[2]))
    ]
    substantive_intervals = [interval for stage in completed_by_stage.values() for interval in stage]
    actual_mode = (
        "parallel" if overlapping_pairs else ("independent-spawned" if len(substantive_intervals) > 1 else "serial")
    )
    _validate_write_approval(payload, bool(write_node_ids))
    if claimed_mode == "parallel" and actual_mode != "parallel":
        raise ValueError("false-parallel-claim")
    integration_order = [
        node_id
        for stage_id in stage_order
        for node_id, _, _ in sorted(completed_by_stage[stage_id], key=lambda item: item[0])
    ]
    return {
        "acceptance_blocked": acceptance_blocked,
        "actual_mode": actual_mode,
        "integration_order": integration_order,
    }


def _runtime_rows(path: Path, label: str) -> list[dict[str, Any]]:
    """Load one current rollout JSONL file, rejecting malformed record envelopes."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ValueError(f"runtime-{label}-missing") from error
    if not lines:
        raise ValueError(f"runtime-{label}-invalid")
    rows: list[dict[str, Any]] = []
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"runtime-{label}-invalid") from error
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("type"), str)
            or not isinstance(row.get("payload"), dict)
        ):
            raise ValueError(f"runtime-{label}-invalid")
        rows.append(row)
    return rows


def _runtime_integer(value: object, label: str, node_id: str) -> int:
    """Return one non-boolean integer from a host event's observed shape."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"runtime-{label}-invalid:{node_id}")
    return value


def _runtime_timestamp(value: object, label: str, node_id: str) -> datetime:
    """Return one timezone-aware host timestamp from a rollout row."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"runtime-{label}-invalid:{node_id}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"runtime-{label}-invalid:{node_id}") from error
    if parsed.tzinfo is None:
        raise ValueError(f"runtime-{label}-invalid:{node_id}")
    return parsed


def _runtime_manifest_bytes(manifest: dict[str, object], manifest_path: Path) -> str:
    """Bind the supplied decoded manifest to the exact bytes retained by the parent."""
    try:
        manifest_bytes = manifest_path.read_bytes()
        on_disk = json.loads(manifest_bytes)
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("runtime-manifest-bytes-invalid") from error
    if not isinstance(on_disk, dict) or on_disk != manifest:
        raise ValueError("runtime-manifest-bytes-mismatch")
    return hashlib.sha256(manifest_bytes).hexdigest()


def _runtime_write_node(manifest: dict[str, object]) -> str | None:
    """Find a declared write node before manifest-control validation can mask it."""
    stages = manifest.get("stages")
    if not isinstance(stages, list):
        return None
    for stage in stages:
        if not isinstance(stage, dict) or not isinstance(stage.get("nodes"), list):
            continue
        for node in stage["nodes"]:
            if isinstance(node, dict) and node.get("mutation") == "write":
                node_id = node.get("node_id")
                return node_id if isinstance(node_id, str) and node_id else "unknown"
    return None


def _runtime_capability_evidence(manifest: dict[str, object]) -> dict[str, str]:
    """Validate the complete schema-v2 portable capability-evidence contract."""
    evidence = _object(manifest.get("capability_evidence"), "capability-evidence")
    if set(evidence) != {"tier", "task_sensitivity", "network", "credentials"}:
        raise ValueError("capability-evidence-invalid")
    network = _object(evidence.get("network"), "capability-network")
    credentials = _object(evidence.get("credentials"), "capability-credentials")
    if set(network) != {"mode", "approval_policy", "external_events"} or set(credentials) != {
        "context_scan",
        "filesystem_isolation",
    }:
        raise ValueError("capability-evidence-invalid")
    if evidence.get("tier") == "host-isolated":
        raise ValueError("host-isolation-evidence-unavailable")
    if evidence.get("tier") != "portable":
        raise ValueError("capability-evidence-tier-invalid")
    if evidence.get("task_sensitivity") != "non-sensitive":
        raise ValueError("portable-sensitive-task")
    if network.get("mode") != "restricted":
        raise ValueError("portable-network-mode-invalid")
    if network.get("approval_policy") != "never":
        raise ValueError("portable-network-approval-policy-invalid")
    if network.get("external_events") != []:
        raise ValueError("portable-external-events-invalid")
    if credentials.get("context_scan") != "passed":
        raise ValueError("portable-context-scan-required")
    if credentials.get("filesystem_isolation") != "unverified":
        raise ValueError("portable-filesystem-isolation-invalid")
    return {
        "tier": "portable",
        "task_sensitivity": "non-sensitive",
        "approval_policy": "never",
        "network": "restricted",
    }


def _runtime_plan_capability_policy(plan_path: Path) -> dict[str, str]:
    """Read the frozen parent-owned portable task classification from the plan."""
    try:
        plan = json.loads(plan_path.read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("runtime-plan-capability-policy-missing") from error
    if not isinstance(plan, dict):
        raise ValueError("runtime-plan-capability-policy-missing")
    policy = plan.get("capability_policy")
    if not isinstance(policy, dict) or "task_sensitivity" not in policy:
        raise ValueError("runtime-plan-capability-policy-missing")
    if policy.get("task_sensitivity") != "non-sensitive":
        raise ValueError("runtime-plan-task-sensitivity-mismatch")
    if "tier" in policy and policy.get("tier") != "portable":
        raise ValueError("runtime-plan-capability-policy-mismatch")
    return {"task_sensitivity": "non-sensitive"}


def _validated_runtime_consumer_policy(plan: Mapping[str, Any], expected_consumer_id: str) -> dict[str, str]:
    """Bind one closed promoted consumer to its exact parent policy object."""
    if not isinstance(expected_consumer_id, str) or re.fullmatch(r"[a-z][a-z0-9-]*", expected_consumer_id) is None:
        raise ValueError("runtime-consumer-id-invalid")
    if expected_consumer_id not in _PROMOTED_PORTABLE_READ_CONSUMERS:
        raise ValueError("runtime-consumer-not-promoted")
    policy = plan.get("consumer_policy")
    if not isinstance(policy, dict):
        raise ValueError("runtime-consumer-policy-missing")
    if set(policy) != {
        "consumer_id",
        "capability",
        "promotion_status",
        "parent_mutations",
        "canonical_gates",
    }:
        raise ValueError("runtime-consumer-policy-invalid")
    if policy.get("consumer_id") != expected_consumer_id:
        raise ValueError("runtime-consumer-id-mismatch")
    capability = policy.get("capability")
    if capability == "instruction-bounded-review" and expected_consumer_id == "code-review":
        if plan.get("review_operation") != "inspection-only":
            raise ValueError("review-inspection-operation-invalid")
        if plan.get("source_sensitivity") != "non-sensitive":
            raise ValueError("review-inspection-source-sensitivity-invalid")
        if "read_host" in plan or "review_host" in plan:
            raise ValueError("review-inspection-host-claim-forbidden")
    elif capability != "portable-read-only":
        raise ValueError("runtime-consumer-capability-invalid")
    elif "review_operation" in plan:
        raise ValueError("review-inspection-capability-required")
    if policy.get("promotion_status") != "promoted":
        raise ValueError("runtime-consumer-promotion-required")
    if policy.get("parent_mutations") != "serial":
        raise ValueError("runtime-consumer-parent-mutations-invalid")
    if policy.get("canonical_gates") != "serial":
        raise ValueError("runtime-consumer-canonical-gates-invalid")
    return {"consumer_id": expected_consumer_id, "capability": capability}


def _runtime_plan_consumer_policy(plan_path: Path, expected_consumer_id: str) -> dict[str, str]:
    """Bind a promoted consumer runtime to its exact frozen parent policy."""
    try:
        plan = json.loads(plan_path.read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("runtime-consumer-policy-missing") from error
    if not isinstance(plan, dict):
        raise ValueError("runtime-consumer-policy-missing")
    policy = _validated_runtime_consumer_policy(plan, expected_consumer_id)
    # Inspection admission never certifies the strict runtime's enforced permission contract.
    if policy["capability"] != "portable-read-only":
        raise ValueError("runtime-consumer-capability-invalid")
    return policy


def _runtime_write_policy(plan: Mapping[str, Any]) -> bool:
    """Return whether the frozen consumer plan declares parent-serial writes."""
    policy = plan.get("write_policy")
    if not isinstance(policy, dict) or set(policy) != {"parent_writes", "approval_requirement"}:
        raise ValueError("runtime-write-policy-invalid")
    parent_writes = policy.get("parent_writes")
    requirement = policy.get("approval_requirement")
    if parent_writes == "planned" and requirement == "exact-plan-digest":
        return True
    if parent_writes == "none" and requirement == "not-required":
        return False
    raise ValueError("runtime-write-policy-invalid")


def validate_inspection_contexts(plan: Mapping[str, Any], plan_path: Path) -> dict[str, Path]:
    """Bind and scan review context before dispatch, returning its unique role-to-file mapping.

    Paths stay under the frozen plan directory and hashes bind the exact UTF-8 source bytes. Common-secret detection
    rejects a context without printing its contents; it is not a guarantee that arbitrary sensitive data is absent. An
    empty list represents a parent-only review with no child dispatch.
    """
    contexts = plan.get("contexts")
    if not isinstance(contexts, list) or len(contexts) > 4:
        raise ValueError("review-inspection-contexts-invalid")
    by_role: dict[str, Path] = {}
    for entry in contexts:
        if not isinstance(entry, dict) or set(entry) != {"role_id", "context_path", "context_sha256"}:
            raise ValueError("review-inspection-context-entry-invalid")
        role = entry["role_id"]
        if not isinstance(role, str) or re.fullmatch(r"[a-z][a-z0-9-]*", role) is None:
            raise ValueError("review-inspection-context-role-invalid")
        if role in by_role:
            raise ValueError("review-inspection-context-role-duplicate")
        context_path = _relative_path(plan_path.parent, entry["context_path"], "review-inspection-context", role)
        if context_path in by_role.values():
            raise ValueError("review-inspection-context-path-duplicate")
        _sha256(context_path, "review-inspection-context", role, entry["context_sha256"])
        try:
            context = context_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise ValueError(f"review-inspection-context-unreadable:{role}") from error
        if not context.strip():
            raise ValueError(f"review-inspection-context-empty:{role}")
        if any(pattern.search(context) for pattern in _SECRET_PATTERNS):
            raise ValueError(f"review-inspection-context-sensitive-material:{role}")
        by_role[role] = context_path
    return by_role


def resolve_consumer_execution_mode(
    consumer_id: str,
    explicit: str | None,
    *,
    environment: Mapping[str, str],
    plan_path: Path,
    approval_path: Path | None,
) -> dict[str, object]:
    """Admit review inspection or compatible restricted reads without certifying runtime controls.

    Host declarations are compatibility inputs transcribed from the actual launcher contract. They never replace
    authoritative post-run control validation. Explicit parallel reads fail closed; automatic mode retains serial work
    when compatible child controls are unavailable. Supplied-context code review may use an instruction-bounded
    inspection plan without permission attestation; its separate review validator checks actual child activity and
    provenance. This exception never authorizes executable probes, writes, or another consumer's dispatch.
    """
    try:
        plan_bytes = plan_path.read_bytes()
        plan = json.loads(plan_bytes)
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("runtime-consumer-plan-invalid") from error
    if not isinstance(plan, dict):
        raise ValueError("runtime-consumer-plan-invalid")
    consumer_policy = _validated_runtime_consumer_policy(plan, consumer_id)
    writes_planned = _runtime_write_policy(plan)
    inspection_only = consumer_policy["capability"] == "instruction-bounded-review"
    if inspection_only and writes_planned:
        raise ValueError("review-inspection-writes-forbidden")
    if inspection_only:
        validate_inspection_contexts(plan, plan_path)
    plan_sha256 = hashlib.sha256(plan_bytes).hexdigest()
    write_approval_validated = False
    if writes_planned:
        if approval_path is None:
            raise ValueError("write-approval-required")
        try:
            approval = json.loads(approval_path.read_bytes())
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("write-approval-invalid") from error
        if not isinstance(approval, dict):
            raise ValueError("write-approval-invalid")
        _validate_exact_write_approval(approval, plan_sha256)
        write_approval_validated = True
    elif approval_path is not None:
        raise ValueError("write-approval-unexpected")
    resolution = resolve_execution_mode(
        explicit,
        environment=environment,
        read_parallel_promoted=True,
        write_parallel_promoted=False,
    )
    if resolution["effective_mode"] == "parallel-read" and not inspection_only:
        # Retain historical review plans; a new declaration cannot be rescued by a conflicting legacy value.
        host = plan.get("read_host", plan.get("review_host") if consumer_id == "code-review" else None)
        if host != {
            "source": "runtime-tool-contract",
            "sandbox_mode": "read-only",
            "approval_policy": "never",
        }:
            if resolution["requested_mode"] != "auto":
                prefix = "review-" if consumer_id == "code-review" else ""
                raise ValueError(f"{prefix}host-controls-unavailable-before-dispatch")
            resolution["effective_mode"] = "serial"
            resolution["fallback_reason"] = "host-controls-unavailable-before-dispatch"
    return {
        **resolution,
        **consumer_policy,
        "plan_sha256": plan_sha256,
        "write_approval_required": writes_planned,
        "write_approval_validated": write_approval_validated,
    }


def _runtime_plan_token_budgets(plan_path: Path, manifest: dict[str, object]) -> list[dict[str, object]]:
    """Bind every runtime wave to a fully admitted budget in the frozen plan."""
    try:
        plan = json.loads(plan_path.read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("runtime-plan-token-budgets-missing") from error
    if not isinstance(plan, dict) or not isinstance(plan.get("token_budgets"), list) or not plan["token_budgets"]:
        raise ValueError("runtime-plan-token-budgets-missing")

    stages_raw = manifest.get("stages")
    if not isinstance(stages_raw, list) or not stages_raw:
        raise ValueError("runtime-plan-token-budgets-invalid")
    stages = [_object(stage, "stage") for stage in stages_raw]
    stages_by_id = {_text(stage.get("stage_id"), "stage-id"): stage for stage in stages}
    expected_by_wave: dict[str, list[str]] = {}
    for stage_id in _topological_stage_ids(stages):
        stage = stages_by_id[stage_id]
        wave_id = _text(stage.get("wave_id"), "wave-id")
        if wave_id in expected_by_wave or not isinstance(stage.get("nodes"), list):
            raise ValueError("runtime-plan-token-budgets-invalid")
        expected_by_wave[wave_id] = sorted(
            _text(_object(node, "node").get("node_id"), "node-id") for node in stage["nodes"]
        )

    budgets: dict[str, dict[str, Any]] = {}
    for raw_budget in plan["token_budgets"]:
        budget = _object(raw_budget, "runtime-plan-token-budget")
        if set(budget) != {"wave_id", "ceiling_tokens", "node_order", "reservations"}:
            raise ValueError("runtime-plan-token-budgets-invalid")
        wave_id = _text(budget.get("wave_id"), "runtime-plan-token-budget-wave-id")
        if wave_id in budgets:
            raise ValueError("runtime-plan-token-budgets-invalid")
        budgets[wave_id] = budget
    if set(budgets) != set(expected_by_wave):
        raise ValueError("runtime-plan-token-budget-wave-mismatch")

    admissions: list[dict[str, object]] = []
    for wave_id, expected_nodes in expected_by_wave.items():
        budget = budgets[wave_id]
        node_order = _string_list(budget.get("node_order"), "runtime-plan-token-budget-node-order")
        if node_order != expected_nodes:
            raise ValueError(f"runtime-plan-token-budget-node-mismatch:{wave_id}")
        decision = admit_wave_token_budget(
            ceiling_tokens=budget.get("ceiling_tokens"),  # type: ignore[arg-type]
            node_order=node_order,
            reservations=_object(budget.get("reservations"), "runtime-plan-token-budget-reservations"),
            completed_node_ids=[],
            active_node_ids=[],
        )
        if decision["dispatch_node_ids"] != expected_nodes or decision["serial_replan_node_ids"]:
            raise ValueError(f"runtime-token-budget-dispatch-exceeds-admission:{wave_id}")
        admissions.append({"wave_id": wave_id, **decision})
    return admissions


def _runtime_structural_manifest(manifest: dict[str, object]) -> dict[str, object]:
    """Adapt verified v2 read-only controls for the private schema-v1 structural gate."""
    structural = deepcopy(manifest)
    stages = structural.get("stages")
    if not isinstance(stages, list):
        return structural
    expected_controls = {
        "sandbox_mode": "read-only",
        "write_paths": [],
        "network": "restricted",
        "credentials": "unverified",
    }
    structural_controls = {
        "sandbox_mode": "read-only",
        "write_paths": [],
        "network": False,
        "credentials": False,
    }
    for stage in stages:
        if not isinstance(stage, dict):
            continue
        nodes = stage.get("nodes")
        if not isinstance(nodes, list):
            continue
        for node in nodes:
            if not isinstance(node, dict):
                continue
            node_id = node.get("node_id")
            safe_node_id = node_id if isinstance(node_id, str) and node_id else "unknown"
            if (
                node.get("mutation") != "read-only"
                or node.get("owned_paths") != []
                or node.get("requested_controls") != expected_controls
                or node.get("observed_controls") != {**expected_controls, "enforced": True}
            ):
                raise ValueError(f"runtime-node-capability-mismatch:{safe_node_id}")
            node["requested_controls"] = structural_controls
            node["observed_controls"] = {**structural_controls, "enforced": True}
    structural["schema_version"] = SCHEMA_VERSION
    structural["claimed_mode"] = "independent-spawned"
    return structural


def _runtime_parent_identity(rows: list[dict[str, Any]]) -> tuple[str, str]:
    """Return the unique parent thread and collaboration path from session metadata."""
    metadata = [
        row["payload"] for row in rows if row["type"] == "session_meta" and isinstance(row["payload"].get("id"), str)
    ]
    if len(metadata) != 1 or not metadata[0]["id"]:
        raise ValueError("runtime-parent-session-invalid")
    agent_path = metadata[0].get("agent_path", "/root")
    if not isinstance(agent_path, str) or (agent_path != "/root" and not agent_path.startswith("/root/")):
        raise ValueError("runtime-parent-session-invalid")
    return metadata[0]["id"], agent_path


def _runtime_spawn_call(
    rows: list[dict[str, Any]],
    *,
    call_id: str,
    role_id: str,
    agent_path: str,
    node_id: str,
) -> dict[str, str]:
    """Bind one spawn call while retaining only a hash and safe argument projection."""
    matches = [
        row["payload"]
        for row in rows
        if row["type"] == "response_item"
        and row["payload"].get("type") == "function_call"
        and row["payload"].get("name") == "spawn_agent"
        and row["payload"].get("call_id") == call_id
    ]
    if len(matches) != 1:
        raise ValueError(f"runtime-spawn-call-missing:{node_id}")
    raw_arguments = matches[0].get("arguments")
    if not isinstance(raw_arguments, str):
        raise ValueError(f"runtime-spawn-arguments-invalid:{node_id}")
    try:
        arguments = json.loads(raw_arguments)
    except json.JSONDecodeError as error:
        raise ValueError(f"runtime-spawn-arguments-invalid:{node_id}") from error
    if not isinstance(arguments, dict):
        raise ValueError(f"runtime-spawn-arguments-invalid:{node_id}")
    task_name = agent_path.rsplit("/", maxsplit=1)[-1]
    if arguments.get("agent_type") != role_id or arguments.get("task_name") != task_name:
        raise ValueError(f"runtime-spawn-lineage-mismatch:{node_id}")
    projection = {"agent_type": role_id, "task_name": task_name}
    message = arguments.get("message")
    if isinstance(message, str) and message:
        projection["message_sha256"] = hashlib.sha256(message.encode("utf-8")).hexdigest()
    fork_turns = arguments.get("fork_turns")
    if isinstance(fork_turns, str) and fork_turns:
        projection["fork_turns"] = fork_turns
    projection["arguments_sha256"] = hashlib.sha256(raw_arguments.encode("utf-8")).hexdigest()
    return projection


def _runtime_parent_start(
    rows: list[dict[str, Any]],
    *,
    parent_thread_id: str,
    activity_id: str,
    agent_path: str,
    child_thread_id: str,
    node_id: str,
) -> int:
    """Return one parent-observed child-start time from the current activity record."""
    matches: list[dict[str, Any]] = []
    for row in rows:
        payload = row["payload"]
        item = payload.get("item")
        if (
            row["type"] == "event_msg"
            and payload.get("type") == "item_completed"
            and payload.get("thread_id") == parent_thread_id
            and isinstance(item, dict)
            and item.get("type") == "SubAgentActivity"
            and item.get("kind") == "started"
            and item.get("id") == activity_id
            and item.get("agent_path") == agent_path
            and item.get("agent_thread_id") == child_thread_id
        ):
            matches.append(payload)
    if len(matches) != 1:
        raise ValueError(f"runtime-parent-start-missing:{node_id}")
    return _runtime_integer(matches[0].get("started_at_ms"), "parent-start", node_id)


def _runtime_child_session(
    sessions_dir: Path,
    *,
    parent_thread_id: str,
    child_thread_id: str,
    agent_path: str,
    role_id: str,
    node_id: str,
) -> tuple[Path, list[dict[str, Any]]]:
    """Find the one child rollout whose session metadata proves its parent lineage."""
    try:
        candidates = sorted(path for path in sessions_dir.glob(f"rollout-*{child_thread_id}*.jsonl") if path.is_file())
    except OSError as error:
        raise ValueError(f"runtime-child-session-missing:{node_id}") from error
    matches: list[tuple[Path, list[dict[str, Any]]]] = []
    for path in candidates:
        rows = _runtime_rows(path, "child-rollout")
        metadata = [
            row["payload"]
            for row in rows
            if row["type"] == "session_meta" and row["payload"].get("id") == child_thread_id
        ]
        if not metadata:
            continue
        if len(metadata) != 1:
            raise ValueError(f"runtime-child-session-invalid:{node_id}")
        source = metadata[0].get("source")
        if not isinstance(source, dict):
            raise ValueError(f"runtime-child-lineage-mismatch:{node_id}")
        subagent = source.get("subagent")
        if not isinstance(subagent, dict) or not isinstance(subagent.get("thread_spawn"), dict):
            raise ValueError(f"runtime-child-lineage-mismatch:{node_id}")
        lineage = subagent["thread_spawn"]
        if (
            metadata[0].get("agent_path") != agent_path
            or metadata[0].get("agent_role") != role_id
            or lineage.get("parent_thread_id") != parent_thread_id
            or lineage.get("agent_path") != agent_path
            or lineage.get("agent_role") != role_id
        ):
            raise ValueError(f"runtime-child-lineage-mismatch:{node_id}")
        matches.append((path, rows))
    if not matches:
        raise ValueError(f"runtime-child-session-missing:{node_id}")
    if len(matches) != 1:
        raise ValueError(f"runtime-child-session-duplicate:{node_id}")
    return matches[0]


def _runtime_has_external_network_event(rows: list[dict[str, Any]]) -> bool:
    """Return whether a child response item exceeds the portable local allowlist."""
    for row in rows:
        if row["type"] != "response_item":
            continue
        payload = row["payload"]
        item_type = payload.get("type")
        if not isinstance(item_type, str) or not item_type:
            raise ValueError("portable-external-network-event")
        normalized_type = item_type.casefold().replace("_", ".")
        if normalized_type.startswith(tuple(f"{prefix}." for prefix in _EXTERNAL_NETWORK_PREFIXES)):
            return True
        if item_type in _LOCAL_RESPONSE_ITEM_TYPES:
            continue
        if item_type not in {"function_call", "custom_tool_call"}:
            raise ValueError("portable-external-network-event")
        name = payload.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("portable-external-network-event")
        normalized = name.casefold().replace("_", ".")
        if normalized.startswith(tuple(f"{prefix}." for prefix in _EXTERNAL_NETWORK_PREFIXES)):
            return True
        if name not in _LOCAL_RESPONSE_CALL_NAMES:
            raise ValueError("portable-external-network-event")
    return False


def _runtime_terminal_and_controls(
    rows: list[dict[str, Any]],
    *,
    turn_id: str,
    node_id: str,
    expected_approval_policy: str,
    expected_network: str,
) -> tuple[int, int, dict[str, str]]:
    """Reconcile child control records and return the exact terminal work interval.

    Explicit filesystem write grants contradict this read-only route even when the sandbox label says otherwise. Absence
    of such a contradiction does not prove filesystem or credential isolation.
    """
    settings = [
        row["payload"].get("thread_settings")
        for row in rows
        if row["type"] == "event_msg" and row["payload"].get("type") == "thread_settings_applied"
    ]
    contexts = [
        row["payload"] for row in rows if row["type"] == "turn_context" and row["payload"].get("turn_id") == turn_id
    ]
    terminals = [
        row["payload"]
        for row in rows
        if row["type"] == "event_msg"
        and row["payload"].get("type") == "task_complete"
        and row["payload"].get("turn_id") == turn_id
    ]
    if len(terminals) != 1:
        raise ValueError(f"runtime-child-terminal-missing:{node_id}")
    if len(settings) != 1 or not isinstance(settings[0], dict) or len(contexts) != 1:
        raise ValueError(f"runtime-child-controls-missing:{node_id}")
    setting = settings[0]
    context = contexts[0]
    permission = setting.get("permission_profile")
    turn_permission = context.get("permission_profile")
    sandbox = context.get("sandbox_policy")
    if not isinstance(permission, dict) or not isinstance(turn_permission, dict) or not isinstance(sandbox, dict):
        raise ValueError(f"runtime-child-controls-invalid:{node_id}")
    for profile in (permission, turn_permission):
        filesystem = profile.get("file_system")
        if filesystem is None:
            continue
        if not isinstance(filesystem, dict) or filesystem.get("type") != "restricted":
            raise ValueError(f"runtime-child-filesystem-controls-invalid:{node_id}")
        entries = filesystem.get("entries")
        if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
            raise ValueError(f"runtime-child-filesystem-controls-invalid:{node_id}")
        if any(entry.get("access") == "write" for entry in entries):
            raise ValueError(f"runtime-child-filesystem-write-grant:{node_id}")
        if any(entry.get("access") != "read" for entry in entries):
            raise ValueError(f"runtime-child-filesystem-controls-invalid:{node_id}")
    model = setting.get("model")
    effort = setting.get("reasoning_effort")
    observed_approval_policy = setting.get("approval_policy")
    if (
        not isinstance(model, str)
        or not model
        or not isinstance(effort, str)
        or not effort
        or not isinstance(observed_approval_policy, str)
        or not observed_approval_policy
        or context.get("model") != model
        or context.get("effort") != effort
        or sandbox.get("type") != "read-only"
    ):
        raise ValueError(f"runtime-child-controls-invalid:{node_id}")
    if (
        observed_approval_policy != expected_approval_policy
        or context.get("approval_policy") != expected_approval_policy
    ):
        raise ValueError(f"portable-network-approval-record-mismatch:{node_id}")
    if permission.get("network") != expected_network or turn_permission.get("network") != expected_network:
        raise ValueError(f"portable-network-record-mismatch:{node_id}")
    terminal = terminals[0]
    started = _runtime_integer(terminal.get("started_at"), "child-terminal", node_id)
    completed = _runtime_integer(terminal.get("completed_at"), "child-terminal", node_id)
    duration_ms = _runtime_integer(terminal.get("duration_ms"), "child-terminal", node_id)
    endpoint_duration_ms = (completed - started) * 1000
    # Current host endpoints lose sub-second precision while elapsed duration retains it.
    if completed <= started or abs(duration_ms - endpoint_duration_ms) >= 1000:
        raise ValueError(f"runtime-child-terminal-invalid:{node_id}")
    return (
        started,
        completed,
        {
            "model": model,
            "effort": effort,
            "approval_policy": observed_approval_policy,
            "sandbox_mode": "read-only",
            "network": expected_network,
        },
    )


def _runtime_role_model_effort(roles_dir: Path, role_id: str, node_id: str) -> tuple[str, str]:
    """Read the model and effort already hash-bound by the packaged role card."""
    role_path = roles_dir / role_id / "ROLE.md"
    try:
        lines = role_path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ValueError(f"runtime-role-contract-invalid:{node_id}") from error
    if not lines or lines[0] != "---":
        raise ValueError(f"runtime-role-contract-invalid:{node_id}")
    try:
        closing_index = lines.index("---", 1)
    except ValueError as error:
        raise ValueError(f"runtime-role-contract-invalid:{node_id}") from error
    fields = dict(line.split(":", maxsplit=1) for line in lines[1:closing_index] if ":" in line)
    model = fields.get("model", "").strip()
    effort = fields.get("model_reasoning_effort", "").strip()
    if not model or not effort:
        raise ValueError(f"runtime-role-contract-invalid:{node_id}")
    return model, effort


def _runtime_output_message(node: dict[str, Any], *, run_dir: Path, terminal: dict[str, Any], node_id: str) -> str:
    """Bind the terminal message to the verified output file and return its stripped text."""
    attempt = _object(node["attempts"][-1], "attempt")
    output_path = _relative_path(run_dir, attempt.get("output_path"), "output", node_id)
    try:
        expected = output_path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise ValueError(f"runtime-child-output-missing:{node_id}") from error
    observed = terminal.get("last_agent_message")
    if not isinstance(observed, str):
        raise ValueError(f"runtime-child-output-mismatch:{node_id}")
    if any(pattern.search(text) for pattern in _SECRET_PATTERNS for text in (expected, observed)):
        raise ValueError(f"runtime-output-sensitive-material:{node_id}")
    if observed != expected:
        raise ValueError(f"runtime-child-output-mismatch:{node_id}")
    return expected


def _runtime_parent_join(
    rows: list[dict[str, Any]],
    *,
    join_event: object,
    agent_path: str,
    parent_path: str,
    output_text: str,
    child_completed: int,
    node_id: str,
) -> dict[str, str]:
    """Bind one manifest join to parent-recorded consumption of a child final answer."""
    event_id = _text(_object(join_event, "join-event").get("event_id"), "join-event-id")
    matches = [
        row
        for row in rows
        if row["type"] == "response_item"
        and row["payload"].get("type") == "agent_message"
        and row["payload"].get("id") == event_id
    ]
    if not matches:
        raise ValueError(f"runtime-parent-join-missing:{node_id}")
    if len(matches) != 1:
        raise ValueError(f"runtime-parent-join-duplicate:{node_id}")
    row = matches[0]
    payload = row["payload"]
    if payload.get("author") != agent_path:
        raise ValueError(f"runtime-parent-join-sender-mismatch:{node_id}")
    recipient = payload.get("recipient")
    if not isinstance(recipient, str) or not recipient.strip():
        raise ValueError(f"runtime-parent-join-recipient-required:{node_id}")
    if recipient != parent_path:
        raise ValueError(f"runtime-parent-join-recipient-mismatch:{node_id}")
    expected_message = "\n".join(
        (
            "Message Type: FINAL_ANSWER",
            f"Task name: {recipient}",
            f"Sender: {agent_path}",
            "Payload:",
            output_text,
        )
    )
    content = payload.get("content")
    if (
        not isinstance(content, list)
        or len(content) != 1
        or not isinstance(content[0], dict)
        or content[0].get("type") != "input_text"
        or content[0].get("text") != expected_message
    ):
        raise ValueError(f"runtime-parent-join-content-mismatch:{node_id}")
    raw_timestamp = row.get("timestamp")
    if not isinstance(raw_timestamp, str) or not raw_timestamp:
        raise ValueError(f"runtime-parent-join-timestamp-invalid:{node_id}")
    try:
        joined_at = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"runtime-parent-join-timestamp-invalid:{node_id}") from error
    if joined_at.tzinfo is None:
        raise ValueError(f"runtime-parent-join-timestamp-invalid:{node_id}")
    if joined_at.timestamp() < child_completed:
        raise ValueError(f"runtime-parent-join-before-terminal:{node_id}")
    return {
        "event_id": event_id,
        "recipient": recipient,
        "message_sha256": hashlib.sha256(expected_message.encode("utf-8")).hexdigest(),
    }


def validate_read_only_runtime(
    manifest: dict[str, object],
    *,
    manifest_path: Path,
    plan_path: Path,
    parent_rollout: Path,
    sessions_dir: Path,
    run_dir: Path,
    roles_dir: Path,
    historical_unbudgeted: bool = False,
    expected_consumer_id: str | None = None,
) -> dict[str, object]:
    """Bind schema-v2 portable-read-restricted evidence to observed rollout records.

    This validator returns literal observed host facts, never a global guarantee about network, command, filesystem
    credential, or host isolation. Pass ``expected_consumer_id`` only for a promoted consumer route; the exact frozen
    plan must then keep that consumer's capability portable-read-only and its mutations and canonical gates serial.
    ``historical_unbudgeted=True`` reads earlier schema-v2 evidence without token budgets only; its summary is
    acceptance-blocked and cannot promote a runtime route.
    """
    if not isinstance(historical_unbudgeted, bool):
        raise ValueError("runtime-historical-reader-flag-invalid")
    if manifest.get("schema_version") != RUNTIME_SCHEMA_VERSION:
        raise ValueError("runtime-schema-v2-required")
    capability_evidence = _runtime_capability_evidence(manifest)
    write_node = _runtime_write_node(manifest)
    if write_node is not None:
        raise ValueError(f"runtime-write-parallel-unsupported:{write_node}")
    manifest_sha256 = _runtime_manifest_bytes(manifest, manifest_path)
    try:
        if not isinstance(manifest.get("plan_sha256"), str) or hashlib.sha256(
            plan_path.read_bytes()
        ).hexdigest() != manifest.get("plan_sha256"):
            raise ValueError("runtime-plan-hash-mismatch")
    except OSError as error:
        raise ValueError("runtime-plan-hash-mismatch") from error
    plan_policy = _runtime_plan_capability_policy(plan_path)
    if capability_evidence["task_sensitivity"] != plan_policy["task_sensitivity"]:
        raise ValueError("runtime-plan-task-sensitivity-mismatch")
    consumer_policy = (
        _runtime_plan_consumer_policy(plan_path, expected_consumer_id) if expected_consumer_id is not None else None
    )
    legacy_unbudgeted = False
    try:
        token_budget_admissions = _runtime_plan_token_budgets(plan_path, manifest)
    except ValueError as error:
        if not historical_unbudgeted or str(error) != "runtime-plan-token-budgets-missing":
            raise
        legacy_unbudgeted = True
        token_budget_admissions = []
    # Runtime child intervals, rather than manifest sequence counters, decide the mode.
    # Adapt only an in-memory copy; the retained schema-v2 evidence keeps literal host values.
    structural_manifest = _runtime_structural_manifest(manifest)
    structural = validate_execution_manifest(structural_manifest, run_dir=run_dir, roles_dir=roles_dir)
    parent_rows = _runtime_rows(parent_rollout, "parent-rollout")
    parent_thread_id, parent_path = _runtime_parent_identity(parent_rows)
    stages = [_object(stage, "stage") for stage in manifest["stages"]]
    stage_order = _topological_stage_ids(stages)
    stages_by_id = {_text(stage.get("stage_id"), "stage-id"): stage for stage in stages}
    runtime_intervals: list[tuple[str, int, int]] = []
    runtime_nodes: list[dict[str, object]] = []
    used_child_files: set[Path] = set()
    used_child_threads: set[str] = set()
    for stage_id in stage_order:
        nodes = [_object(node, "node") for node in stages_by_id[stage_id]["nodes"]]
        for node in sorted(nodes, key=lambda item: _text(item.get("node_id"), "node-id")):
            node_id = _text(node.get("node_id"), "node-id")
            role_id = _text(node.get("role_id"), "role-id")
            attempt = _object(node["attempts"][-1], "attempt")
            agent_path = _text(attempt.get("agent_path"), "runtime-agent-path")
            child_thread_id = _text(attempt.get("agent_thread_id"), "runtime-agent-thread")
            spawn_call_id = _text(attempt.get("spawn_call_id"), "runtime-spawn-call")
            turn_id = _text(attempt.get("turn_id"), "runtime-turn")
            activity_id = _text(_object(attempt.get("start_event"), "start-event").get("event_id"), "start-event-id")
            if child_thread_id in used_child_threads:
                raise ValueError(f"runtime-child-thread-duplicate:{node_id}")
            used_child_threads.add(child_thread_id)
            spawn = _runtime_spawn_call(
                parent_rows,
                call_id=spawn_call_id,
                role_id=role_id,
                agent_path=agent_path,
                node_id=node_id,
            )
            parent_started_ms = _runtime_parent_start(
                parent_rows,
                parent_thread_id=parent_thread_id,
                activity_id=activity_id,
                agent_path=agent_path,
                child_thread_id=child_thread_id,
                node_id=node_id,
            )
            child_path, child_rows = _runtime_child_session(
                sessions_dir,
                parent_thread_id=parent_thread_id,
                child_thread_id=child_thread_id,
                agent_path=agent_path,
                role_id=role_id,
                node_id=node_id,
            )
            if child_path in used_child_files:
                raise ValueError(f"runtime-child-session-duplicate:{node_id}")
            used_child_files.add(child_path)
            if _runtime_has_external_network_event(child_rows):
                raise ValueError("portable-external-network-event")
            started, completed, controls = _runtime_terminal_and_controls(
                child_rows,
                turn_id=turn_id,
                node_id=node_id,
                expected_approval_policy=capability_evidence["approval_policy"],
                expected_network=capability_evidence["network"],
            )
            expected_model, expected_effort = _runtime_role_model_effort(roles_dir, role_id, node_id)
            if controls["model"] != expected_model or controls["effort"] != expected_effort:
                raise ValueError(f"runtime-role-model-effort-mismatch:{node_id}")
            # A parent activity may precede actual work while a serial fallback queues a child.
            # A parallel claim needs the observed launch and work start to agree within one second.
            if manifest.get("claimed_mode") == "parallel" and abs(parent_started_ms - started * 1000) > 1000:
                raise ValueError(f"runtime-start-alignment-invalid:{node_id}")
            terminals = [
                row["payload"]
                for row in child_rows
                if row["type"] == "event_msg"
                and row["payload"].get("type") == "task_complete"
                and row["payload"].get("turn_id") == turn_id
            ]
            output_text = _runtime_output_message(node, run_dir=run_dir, terminal=terminals[0], node_id=node_id)
            parent_join = _runtime_parent_join(
                parent_rows,
                join_event=node.get("join_event"),
                agent_path=agent_path,
                parent_path=parent_path,
                output_text=output_text,
                child_completed=completed,
                node_id=node_id,
            )
            runtime_intervals.append((node_id, started, completed))
            runtime_nodes.append(
                {
                    "node_id": node_id,
                    "role_id": role_id,
                    "agent_path": agent_path,
                    "agent_thread_id": child_thread_id,
                    "spawn": spawn,
                    "parent_join": parent_join,
                    "declared_controls": controls,
                    "child_rollout_sha256": hashlib.sha256(child_path.read_bytes()).hexdigest(),
                }
            )
    overlaps = [
        (left[0], right[0])
        for index, left in enumerate(runtime_intervals)
        for right in runtime_intervals[index + 1 :]
        if _overlap((left[1], left[2]), (right[1], right[2]))
    ]
    claimed_mode = manifest.get("claimed_mode")
    if overlaps:
        actual_mode = "parallel"
    elif claimed_mode == "serial-fallback":
        actual_mode = "serial-fallback"
    else:
        actual_mode = "independent-spawned" if len(runtime_intervals) > 1 else "serial"
    if claimed_mode != actual_mode:
        if claimed_mode == "parallel":
            raise ValueError("runtime-false-parallel-claim")
        if claimed_mode == "serial-fallback" and actual_mode == "parallel":
            raise ValueError("runtime-serial-fallback-overlap")
        raise ValueError(f"runtime-mode-claim-mismatch:{claimed_mode}:{actual_mode}")
    acceptance_blocked = bool(structural["acceptance_blocked"]) or legacy_unbudgeted
    return {
        **structural,
        "acceptance_blocked": acceptance_blocked,
        "actual_mode": actual_mode,
        "evidence_level": (
            "historical-portable-read-restricted-unbudgeted" if legacy_unbudgeted else "portable-read-restricted"
        ),
        "network_mode": "restricted",
        "approval_policy": "never",
        "filesystem_credential_isolation": "unverified",
        "manifest_sha256": manifest_sha256,
        "parent_rollout_sha256": hashlib.sha256(parent_rollout.read_bytes()).hexdigest(),
        "token_budget_admissions": token_budget_admissions,
        "runtime_nodes": runtime_nodes,
        "runtime_promotion_eligible": not acceptance_blocked and consumer_policy is not None,
        "write_parallel_eligible": False,
        "consumer_id": consumer_policy["consumer_id"] if consumer_policy is not None else None,
    }


def _cli_parser() -> argparse.ArgumentParser:
    """Build the installed consumer preflight and runtime-validation CLI."""
    parser = argparse.ArgumentParser(description="Resolve and validate promoted portable read-only consumers.")
    commands = parser.add_subparsers(dest="command", required=True)

    preflight = commands.add_parser("preflight", help="Bind execution mode and any parent-write approval.")
    preflight.add_argument("--consumer", required=True, choices=sorted(_PROMOTED_PORTABLE_READ_CONSUMERS))
    preflight.add_argument("--plan", required=True, type=Path)
    preflight.add_argument("--approval", type=Path)
    preflight.add_argument("--execution", choices=("auto", "serial", "parallel-read", "parallel-write"))

    runtime = commands.add_parser("validate-runtime", help="Bind a completed host runtime to one consumer.")
    runtime.add_argument("--consumer", required=True, choices=sorted(_PROMOTED_PORTABLE_READ_CONSUMERS))
    runtime.add_argument("--manifest", required=True, type=Path)
    runtime.add_argument("--plan", required=True, type=Path)
    runtime.add_argument("--parent-rollout", required=True, type=Path)
    runtime.add_argument("--sessions-dir", required=True, type=Path)
    runtime.add_argument("--run-dir", required=True, type=Path)
    runtime.add_argument("--roles-dir", type=Path, default=Path(__file__).resolve().parents[1] / "roles")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one fail-closed installed consumer boundary and print compact JSON."""
    args = _cli_parser().parse_args(argv)
    try:
        if args.command == "preflight":
            explicit = f"--execution={args.execution}" if args.execution is not None else None
            result = resolve_consumer_execution_mode(
                args.consumer,
                explicit,
                environment=os.environ,
                plan_path=args.plan,
                approval_path=args.approval,
            )
        else:
            try:
                manifest = json.loads(args.manifest.read_bytes())
            except (OSError, json.JSONDecodeError) as error:
                raise ValueError("runtime-manifest-invalid") from error
            if not isinstance(manifest, dict):
                raise ValueError("runtime-manifest-invalid")
            result = validate_read_only_runtime(
                manifest,
                manifest_path=args.manifest,
                plan_path=args.plan,
                parent_rollout=args.parent_rollout,
                sessions_dir=args.sessions_dir,
                run_dir=args.run_dir,
                roles_dir=args.roles_dir,
                expected_consumer_id=args.consumer,
            )
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
