"""Manage approval-bound generated-fixture and remediation worktree lifecycles.

## Purpose

Preserve the narrow generated-fixture lifecycle and enforce the frozen code-remediate-local production-write boundary
without promoting generic parallel writes.

## Scope

Validate exact frozen plan and approval bytes, create detached managed child worktrees, verify parent-joined handovers
against observed Git changes, derive hash-bound patches, integrate them deterministically, apply one checked source
bundle, and remove only successful worktrees after durable evidence exists. The module rejects edits to approved source
paths, plan drift, child commits, untracked or undeclared paths, symlink traversal, mutable integration drift, partial
joins, and ambiguous rollback.

## Usage

Generated-fixture callers use the existing Python functions. Code-remediate invokes explicit lifecycle subcommands after
a consumer-specific plan digest is approved.

## Outputs

Private JSON lifecycle state, source-local child and integration evidence, parent-derived forward and rollback patches,
and JSON CLI results. Successful cleanup removes managed worktrees without force; failure evidence stays available for
inspection.

## Failure

``PilotError`` stops dispatch, integration, source application, cleanup, or promotion. Failed, conflicted, cancelled,
drifted, or cleanup-uncertain worktrees remain for explicit resolution. Operational path and postcondition containment
does not claim per-child capability isolation or global atomicity.

## Used by

Codex Rig's generated-fixture acceptance proof, code-remediate production boundary, artifact validation, and focused
installed-package tests. This module is not a scheduler, general worktree manager, or independent authority source.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

#: Module logger for worktree pilot lifecycle events.
LOGGER = logging.getLogger(__name__)
#: Path components every generated develop pilot path must begin with.
_GENERATED_PREFIX = (".reports", "codex", "develop")
#: Path components every code-remediate pilot path must begin with.
_CODE_REMEDIATE_PREFIX = (".reports", "codex", "code-remediate")
#: Authority a pilot plan must request exactly: serial edits plus one local pilot, no network or remote changes.
_EXPECTED_AUTHORITY = {
    "serial_repository_edits": True,
    "one_local_parallel_write_pilot": True,
    "pilot_scope": "generated-fixture-repository-only",
    "network": "denied-by-plan",
    "external_or_paid_parent_process": False,
    "remote_mutation": False,
    "user_data_deletion": False,
    "automatic_retry_count": 0,
    "general_parallel_write_enablement": False,
}
#: Scope an approval must grant exactly, matching the requested authority with network recorded as plain `False`.
_EXPECTED_APPROVAL_SCOPE = {**_EXPECTED_AUTHORITY, "network": False}


class PilotError(RuntimeError):
    """Report one fail-closed generated worktree lifecycle violation."""


def _sha256(path: Path) -> str:
    """Return the SHA-256 digest of exact file bytes."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        raise PilotError("evidence-file-unreadable") from error


def _load_json(path: Path, label: str) -> dict[str, Any]:
    """Load one bounded JSON object used as lifecycle authority or state."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, RecursionError) as error:
        raise PilotError(f"{label}-invalid") from error
    if not isinstance(payload, dict):
        raise PilotError(f"{label}-invalid")
    return payload


def _persist(path: Path, payload: dict[str, Any]) -> None:
    """Atomically persist LF-terminated private lifecycle evidence."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    if path.is_symlink() or temporary.is_symlink():
        raise PilotError("state-path-symlink-forbidden")
    if temporary.exists():
        raise PilotError("state-temporary-exists")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    temporary.replace(path)


def _git_environment() -> dict[str, str]:
    """Return the host environment without Git redirection overrides."""
    return {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}


def _git(repository: Path, *arguments: str) -> str:
    """Run Git without a shell or inherited Git redirection overrides."""
    completed = subprocess.run(  # noqa: S603 - argv list, no shell
        ["git", "-C", str(repository), *arguments],  # noqa: S607 - argv list, no shell; tool resolved via PATH on purpose
        check=False,
        text=True,
        shell=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        env=_git_environment(),
    )
    if completed.returncode != 0:
        raise PilotError(f"git-command-failed:{arguments[0] if arguments else 'unknown'}")
    return completed.stdout.strip()


def _git_bytes(repository: Path, *arguments: str) -> bytes:
    """Run Git and return exact stdout bytes for patch hashing."""
    completed = subprocess.run(  # noqa: S603 - argv list, no shell
        ["git", "-C", str(repository), *arguments],  # noqa: S607 - argv list, no shell; tool resolved via PATH on purpose
        check=False,
        text=False,
        shell=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        env=_git_environment(),
    )
    if completed.returncode != 0:
        raise PilotError(f"git-command-failed:{arguments[0] if arguments else 'unknown'}")
    return completed.stdout


def _git_batched_lines(repository: Path, *arguments: str, expected: int) -> list[str] | None:
    """Answer ``expected`` single-line read-only questions with one Git process.

    Returns ``None`` when Git fails or does not answer with exactly one line per question, so the caller can ask each
    question on its own and surface the exact result or error of the unbatched command.
    """
    try:
        output = _git(repository, *arguments)
    except PilotError:
        return None
    lines = [line.strip() for line in output.split("\n")]
    return lines if len(lines) == expected else None


def _git_head_and_tree(repository: Path) -> tuple[str, str]:
    """Return the checked-out commit and its tree identifiers from one Git process."""
    answers = _git_batched_lines(repository, "rev-parse", "HEAD", "HEAD^{tree}", expected=2)
    if answers is None:
        return _git(repository, "rev-parse", "HEAD"), _git(repository, "rev-parse", "HEAD^{tree}")
    return answers[0], answers[1]


def _git_filtered_oid(repository: Path, relative_path: str) -> str:
    """Return Git's clean-filtered object identifier for one working-tree path."""
    path = repository.joinpath(*PurePosixPath(relative_path).parts)
    object_id = _git(repository, "hash-object", f"--path={relative_path}", "--filters", str(path))
    if re.fullmatch(r"[0-9a-f]+", object_id) is None:
        raise PilotError("git-filtered-object-invalid")
    return object_id


def _portable_relative(value: object, label: str) -> str:
    """Normalize a portable repository-relative path and reject Windows aliases."""
    if not isinstance(value, str) or not value or "\x00" in value or any(character in value for character in "*?["):
        raise PilotError(f"{label}-invalid")
    windows = PureWindowsPath(value)
    normalized = value.replace("\\", "/")
    posix = PurePosixPath(normalized)
    if windows.drive or windows.root or posix.is_absolute() or any(part in {"", ".", ".."} for part in posix.parts):
        raise PilotError(f"{label}-invalid")
    if any(":" in part for part in posix.parts):
        raise PilotError(f"{label}-invalid")
    if any(part.endswith((".", " ")) for part in posix.parts):
        raise PilotError(f"{label}-nonportable")
    return posix.as_posix()


def _canonical_path(value: str) -> str:
    """Return a Windows-conservative canonical key for collision checks."""
    return "/".join(part.casefold() for part in PurePosixPath(value).parts)


def _workspace_path(workspace_root: Path, value: object, label: str) -> tuple[str, Path]:
    """Resolve a generated workspace-relative path without following symlinks."""
    relative = _portable_relative(value, label)
    parts = PurePosixPath(relative).parts
    if tuple(parts[: len(_GENERATED_PREFIX)]) != _GENERATED_PREFIX:
        raise PilotError(f"{label}-outside-generated-root")
    current = workspace_root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise PilotError("managed-path-symlink-forbidden")
    candidate = workspace_root.joinpath(*parts)
    try:
        candidate.resolve(strict=False).relative_to(workspace_root.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise PilotError(f"{label}-outside-workspace") from error
    return relative, candidate


def _code_remediate_evidence_path(source_repository: Path, value: object, label: str) -> tuple[str, Path]:
    """Resolve source-local remediation evidence below its bounded run root."""
    relative = _portable_relative(value, label)
    parts = PurePosixPath(relative).parts
    if tuple(parts[: len(_CODE_REMEDIATE_PREFIX)]) != _CODE_REMEDIATE_PREFIX:
        raise PilotError(f"{label}-outside-code-remediate-root")
    current = source_repository
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise PilotError("managed-path-symlink-forbidden")
    candidate = source_repository.joinpath(*parts)
    try:
        candidate.resolve(strict=False).relative_to(source_repository.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise PilotError(f"{label}-outside-source") from error
    return relative, candidate


def _code_remediate_source_path(workspace_root: Path, value: object, label: str) -> tuple[str, Path]:
    """Resolve the authoritative source inside the workspace without symlink traversal."""
    relative = _portable_relative(value, label)
    parts = PurePosixPath(relative).parts
    current = workspace_root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise PilotError("managed-path-symlink-forbidden")
    source = workspace_root.joinpath(*parts)
    try:
        source.resolve(strict=True).relative_to(workspace_root.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise PilotError(f"{label}-outside-workspace") from error
    return relative, source


def _code_remediate_worktree_path(
    workspace_root: Path, source_relative: str, value: object, label: str
) -> tuple[str, Path]:
    """Resolve a managed sibling or ignored source-local worktree path."""
    relative = _portable_relative(value, label)
    parts = PurePosixPath(relative).parts
    source_relative, source_path = _code_remediate_source_path(workspace_root, source_relative, "source-repository")
    source = _canonical_path(source_relative)
    candidate = _canonical_path(relative)
    source_parts = PurePosixPath(source_relative).parts
    local_parts = (*source_parts, ".reports", "codex", "code-remediate-worktrees")
    sibling = parts[0] == ".codex-rig-worktrees" and candidate != source and not candidate.startswith(f"{source}/")
    source_local = parts[: len(local_parts)] == local_parts and len(parts) in {
        len(local_parts) + 1,
        len(local_parts) + 2,
    }
    if not sibling and not source_local:
        raise PilotError(f"{label}-not-managed-sibling")
    current = workspace_root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise PilotError("managed-path-symlink-forbidden")
    worktree = workspace_root.joinpath(*parts)
    try:
        resolved = worktree.resolve(strict=False)
        resolved.relative_to(workspace_root.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise PilotError(f"{label}-outside-workspace") from error
    if source_local:
        try:
            resolved.relative_to(source_path.resolve(strict=True))
        except (OSError, ValueError) as error:
            raise PilotError(f"{label}-outside-source") from error
        source_local_relative = PurePosixPath(*parts[len(source_parts) :]).as_posix()
        # Nested worktrees must stay invisible to the authoritative checkout's status and patch boundary.
        try:
            _git(source_path, "check-ignore", "--", source_local_relative)
        except PilotError as error:
            raise PilotError(f"{label}-not-ignored") from error
        return relative, worktree
    try:
        resolved.relative_to(source_path.resolve(strict=True))
    except ValueError:
        return relative, worktree
    except OSError as error:
        raise PilotError(f"{label}-outside-workspace") from error
    raise PilotError(f"{label}-not-managed-sibling")


def _relative_workspace_path(workspace_root: Path, path: Path, label: str) -> str:
    """Return a workspace-relative path without trusting its string form."""
    try:
        relative = path.resolve(strict=False).relative_to(workspace_root.resolve(strict=True)).as_posix()
    except (OSError, ValueError) as error:
        raise PilotError(f"{label}-outside-workspace") from error
    return relative


def _relative_evidence_path(workspace_root: Path, path: Path, label: str) -> str:
    """Return a generated-root workspace-relative lifecycle evidence path."""
    relative = _relative_workspace_path(workspace_root, path, label)
    _workspace_path(workspace_root, relative, label)
    return relative


def _stage_three(plan: dict[str, Any]) -> dict[str, Any]:
    """Return the unique two-node generated-fixture write stage."""
    if plan.get("schema_version") != 1 or plan.get("requested_authority") != _EXPECTED_AUTHORITY:
        raise PilotError("plan-authority-invalid")
    stages = plan.get("stages")
    if not isinstance(stages, list):
        raise PilotError("plan-stage-invalid")
    matches = [stage for stage in stages if isinstance(stage, dict) and stage.get("stage_id") == "S3"]
    if len(matches) != 1:
        raise PilotError("plan-stage-invalid")
    stage = matches[0]
    if stage.get("mode") != "parallel-write" or stage.get("configured_limit") != 2:
        raise PilotError("plan-stage-invalid")
    nodes = stage.get("nodes")
    if not isinstance(nodes, list) or len(nodes) != 2:
        raise PilotError("plan-node-invalid")
    return stage


def _planned_nodes(stage: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize the exact two disjoint frozen write buckets without Git."""
    nodes: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    seen_outputs: set[str] = set()
    seen_locks: set[str] = set()
    for raw_node in stage["nodes"]:
        if not isinstance(raw_node, dict):
            raise PilotError("plan-node-invalid")
        node_id = raw_node.get("node_id")
        if not isinstance(node_id, str) or re.fullmatch(r"[A-Z0-9-]+", node_id) is None or node_id in seen_ids:
            raise PilotError("plan-node-invalid")
        owned = raw_node.get("owned_paths")
        if not isinstance(owned, list) or not owned:
            raise PilotError(f"owned-path-invalid:{node_id}")
        normalized: list[str] = []
        for value in owned:
            path = _portable_relative(value, "owned-path")
            canonical = _canonical_path(path)
            if canonical in seen_paths:
                raise PilotError(f"owned-path-alias:{node_id}")
            if any(
                canonical.startswith(f"{existing}/") or existing.startswith(f"{canonical}/") for existing in seen_paths
            ):
                raise PilotError(f"owned-path-overlap:{node_id}")
            seen_paths.add(canonical)
            normalized.append(path)
        locks = raw_node.get("resource_locks")
        if not isinstance(locks, list) or any(not isinstance(lock, str) or not lock for lock in locks):
            raise PilotError(f"resource-lock-invalid:{node_id}")
        if seen_locks.intersection(locks):
            raise PilotError(f"resource-lock-overlap:{node_id}")
        seen_locks.update(locks)
        output = _portable_relative(raw_node.get("output"), "patch-output")
        if "/" in output or _canonical_path(output) in seen_outputs:
            raise PilotError(f"patch-output-invalid:{node_id}")
        seen_outputs.add(_canonical_path(output))
        seen_ids.add(node_id)
        nodes.append(
            {
                "node_id": node_id,
                "owned_paths": sorted(normalized),
                "resource_locks": list(locks),
                "output": output,
            }
        )
    return sorted(nodes, key=lambda item: item["node_id"])


def _validated_nodes(stage: dict[str, Any], repository: Path) -> list[dict[str, Any]]:
    """Require every frozen bucket to remain tracked regular repository content."""
    nodes = _planned_nodes(stage)
    for node in nodes:
        for path in node["owned_paths"]:
            target = repository.joinpath(*PurePosixPath(path).parts)
            if target.is_symlink():
                raise PilotError(f"owned-path-symlink-forbidden:{node['node_id']}")
            try:
                _git(repository, "ls-files", "--error-unmatch", "--", path)
            except PilotError as error:
                raise PilotError(f"owned-path-not-tracked:{node['node_id']}") from error
    return nodes


def _validate_approval(plan_path: Path, approval_path: Path) -> tuple[dict[str, Any], str, str]:
    """Bind exact plan bytes to one explicit generated-fixture approval record."""
    plan = _load_json(plan_path, "plan")
    approval = _load_json(approval_path, "approval")
    plan_sha256 = _sha256(plan_path)
    if approval.get("plan_sha256") != plan_sha256:
        raise PilotError("plan-approval-digest-mismatch")
    if approval.get("response") != "approve" or approval.get("source") not in {"explicit-input", "user-prompt"}:
        raise PilotError("write-approval-invalid")
    if approval.get("scope") != _EXPECTED_APPROVAL_SCOPE:
        raise PilotError("write-approval-scope-invalid")
    return plan, plan_sha256, _sha256(approval_path)


def _repository_unchanged(repository: Path, baseline_head: str, label: str) -> None:
    """Require an exact clean generated source baseline."""
    if _git(repository, "rev-parse", "HEAD") != baseline_head:
        raise PilotError(f"{label}-head-drift")
    if _git(repository, "status", "--porcelain=v1", "--untracked-files=all"):
        raise PilotError(f"{label}-dirty")


def _retained_worktree_fingerprint(worktree: Path) -> dict[str, str]:
    """Fingerprint one retained diagnostic worktree without modifying it."""
    if not worktree.is_dir():
        raise PilotError("prior-attempt-worktree-missing")
    return {
        "head": _git(worktree, "rev-parse", "HEAD"),
        "status_sha256": hashlib.sha256(
            _git_bytes(worktree, "status", "--porcelain=v1", "-z", "--untracked-files=all")
        ).hexdigest(),
        "diff_sha256": hashlib.sha256(_git_bytes(worktree, "diff", "--binary", "HEAD", "--")).hexdigest(),
        "untracked_sha256": hashlib.sha256(
            _git_bytes(worktree, "ls-files", "--others", "--exclude-standard", "-z")
        ).hexdigest(),
    }


def _prior_attempt_fingerprint(
    plan: dict[str, Any], stage: dict[str, Any], workspace: Path
) -> dict[str, object] | None:
    """Fingerprint a declared failed attempt so later work cannot rewrite its diagnostics."""
    prior = plan.get("prior_attempt")
    if prior is None:
        return None
    required = {"runtime_record", "status", "retry_count", "retained_worktree_root"}
    if not isinstance(prior, dict) or set(prior) != required:
        raise PilotError("prior-attempt-invalid")
    if prior.get("status") != "failed-retained" or prior.get("retry_count") != 0:
        raise PilotError("prior-attempt-invalid")
    runtime_relative, runtime = _workspace_path(workspace, prior.get("runtime_record"), "prior-runtime")
    root_relative, root = _workspace_path(workspace, prior.get("retained_worktree_root"), "prior-worktree-root")
    if not runtime.is_file() or not root.is_dir():
        raise PilotError("prior-attempt-invalid")
    worktrees: dict[str, object] = {}
    for node in _planned_nodes(stage):
        relative = f"{root_relative}/{node['node_id']}"
        _, worktree = _workspace_path(workspace, relative, "prior-worktree")
        worktrees[node["node_id"]] = {
            "path": relative,
            **_retained_worktree_fingerprint(worktree),
        }
    return {
        "runtime_record": runtime_relative,
        "runtime_sha256": _sha256(runtime),
        "retained_worktree_root": root_relative,
        "worktrees": worktrees,
    }


def prepare_write_pilot(
    *,
    plan_path: Path,
    approval_path: Path,
    workspace_root: Path,
    state_path: Path,
) -> dict[str, object]:
    """Validate authority and create two detached generated child worktrees.

    Args:
        plan_path: Frozen generated-fixture plan inside the workspace.
        approval_path: Explicit approval record bound to the plan bytes.
        workspace_root: Authorized workspace containing the generated evidence root.
        state_path: Private lifecycle JSON artifact written before dispatch.

    Returns:
        Persisted lifecycle state with frozen hashes, baseline, and child paths.

    Raises:
        PilotError: If authority, paths, baseline, or worktree creation is unsafe.
    """
    workspace_root = workspace_root.resolve(strict=True)
    plan, plan_sha256, approval_sha256 = _validate_approval(plan_path, approval_path)
    stage = _stage_three(plan)
    repository_relative, repository = _workspace_path(
        workspace_root, stage.get("fixture_repository"), "fixture-repository"
    )
    worktree_root_relative, worktree_root = _workspace_path(workspace_root, stage.get("worktree_root"), "worktree-root")
    state_relative = _relative_evidence_path(workspace_root, state_path, "state-path")
    if not repository.is_dir() or not (repository / ".git").exists():
        raise PilotError("fixture-repository-invalid")
    if _git(repository, "status", "--porcelain=v1", "--untracked-files=all"):
        raise PilotError("fixture-repository-dirty")
    baseline_head, baseline_tree = _git_head_and_tree(repository)
    nodes = _validated_nodes(stage, repository)
    prior_fingerprint = _prior_attempt_fingerprint(plan, stage, workspace_root)
    if worktree_root.exists():
        raise PilotError("worktree-root-exists")
    worktree_root.mkdir(parents=True)
    state: dict[str, Any] = {
        "schema_version": 1,
        "status": "preparing",
        "workspace_root": str(workspace_root),
        "plan_path": _relative_workspace_path(workspace_root, plan_path, "plan-path"),
        "plan_sha256": plan_sha256,
        "approval_path": _relative_workspace_path(workspace_root, approval_path, "approval-path"),
        "approval_sha256": approval_sha256,
        "state_path": state_relative,
        "repository": repository_relative,
        "worktree_root": worktree_root_relative,
        "baseline_head": baseline_head,
        "baseline_tree": baseline_tree,
        "retry_count": 0,
        "nodes": [],
        "integration_status": "not-started",
        "cleanup_status": "not-started",
    }
    if prior_fingerprint is not None:
        state["prior_attempt_fingerprint"] = prior_fingerprint
    try:
        for node in nodes:
            worktree_relative = f"{worktree_root_relative}/{node['node_id']}"
            worktree = workspace_root.joinpath(*PurePosixPath(worktree_relative).parts)
            _git(repository, "worktree", "add", "--detach", str(worktree), baseline_head)
            state["nodes"].append(
                {
                    **node,
                    "worktree_path": worktree_relative,
                    "created": True,
                    "terminal_status": "pending",
                    "joined": False,
                    "patch_status": "not-collected",
                }
            )
        state["status"] = "prepared"
        _persist(state_path, state)
    except PilotError:
        state["status"] = "prepare-failed"
        _persist(state_path, state)
        raise
    LOGGER.info("Prepared generated worktree pilot with %d nodes", len(nodes))
    return state


def _read_state(state_path: Path) -> tuple[dict[str, Any], Path]:
    """Load lifecycle state and reject a non-generated state location."""
    state = _load_json(state_path, "lifecycle-state")
    workspace_value = state.get("workspace_root")
    if not isinstance(workspace_value, str):
        raise PilotError("lifecycle-state-invalid")
    workspace = Path(workspace_value)
    if not workspace.is_absolute() or not workspace.is_dir():
        raise PilotError("lifecycle-state-invalid")
    if state.get("schema_version") == 1:
        state_relative, expected_state = _workspace_path(workspace, state.get("state_path"), "state-path")
    elif state.get("schema_version") == 2 and state.get("consumer") == "code-remediate":
        _, source = _code_remediate_source_path(workspace, state.get("source_repository"), "source-repository")
        state_relative, expected_state = _code_remediate_evidence_path(source, state.get("state_path"), "state-path")
    else:
        raise PilotError("lifecycle-state-schema-invalid")
    if state_relative != state.get("state_path") or expected_state.resolve(strict=False) != state_path.resolve(
        strict=False
    ):
        raise PilotError("lifecycle-state-path-mismatch")
    return state, workspace


def _state_nodes(state: dict[str, Any], nodes: list[dict[str, Any]], workspace: Path, state_path: Path) -> None:
    """Compare state node paths and outputs with the frozen plan-derived values."""
    persisted = state.get("nodes")
    if not isinstance(persisted, list) or len(persisted) != len(nodes):
        raise PilotError("lifecycle-state-node-drift")
    for planned, recorded in zip(nodes, persisted, strict=True):
        if not isinstance(recorded, dict):
            raise PilotError("lifecycle-state-node-drift")
        if any(recorded.get(key) != planned[key] for key in ("node_id", "owned_paths", "resource_locks", "output")):
            raise PilotError("lifecycle-state-node-drift")
        expected_worktree = f"{state['worktree_root']}/{planned['node_id']}"
        _, worktree = _workspace_path(workspace, recorded.get("worktree_path"), "child-worktree-path")
        if recorded.get("worktree_path") != expected_worktree or worktree != workspace.joinpath(
            *PurePosixPath(expected_worktree).parts
        ):
            raise PilotError("lifecycle-state-node-drift")
        if "patch_path" in recorded:
            expected_patch = _relative_evidence_path(workspace, state_path.parent / planned["output"], "patch-path")
            if recorded.get("patch_path") != expected_patch:
                raise PilotError("lifecycle-state-patch-path-drift")
            _workspace_path(workspace, recorded["patch_path"], "patch-path")


def _rederive_authority(
    state: dict[str, Any], workspace: Path, state_path: Path
) -> tuple[Path, Path, list[dict[str, Any]]]:
    """Rebuild frozen authority and managed paths before a lifecycle Git transition."""
    plan_relative = _portable_relative(state.get("plan_path"), "plan-path")
    approval_relative = _portable_relative(state.get("approval_path"), "approval-path")
    plan_path = workspace.joinpath(*PurePosixPath(plan_relative).parts)
    approval_path = workspace.joinpath(*PurePosixPath(approval_relative).parts)
    if _relative_workspace_path(workspace, plan_path, "plan-path") != plan_relative:
        raise PilotError("lifecycle-state-plan-path-drift")
    if _relative_workspace_path(workspace, approval_path, "approval-path") != approval_relative:
        raise PilotError("lifecycle-state-approval-path-drift")
    plan, plan_sha256, approval_sha256 = _validate_approval(plan_path, approval_path)
    if plan_sha256 != state.get("plan_sha256"):
        raise PilotError("plan-drift-before-transition")
    if approval_sha256 != state.get("approval_sha256"):
        raise PilotError("approval-drift-before-transition")
    stage = _stage_three(plan)
    nodes = _planned_nodes(stage)
    repository_relative, repository = _workspace_path(workspace, stage.get("fixture_repository"), "fixture-repository")
    worktree_root_relative, worktree_root = _workspace_path(workspace, stage.get("worktree_root"), "worktree-root")
    if repository_relative != state.get("repository") or worktree_root_relative != state.get("worktree_root"):
        raise PilotError("lifecycle-state-root-drift")
    if state.get("retry_count") != 0:
        raise PilotError("lifecycle-state-retry-drift")
    _state_nodes(state, nodes, workspace, state_path)
    if not isinstance(state.get("baseline_head"), str) or not isinstance(state.get("baseline_tree"), str):
        raise PilotError("lifecycle-state-baseline-invalid")
    if state.get("prior_attempt_fingerprint") != _prior_attempt_fingerprint(plan, stage, workspace):
        raise PilotError("prior-attempt-fingerprint-mismatch")
    return repository, worktree_root, nodes


def _node(state: dict[str, Any], node_id: str) -> dict[str, Any]:
    """Return one unique lifecycle node by identifier."""
    nodes = state.get("nodes")
    if not isinstance(nodes, list):
        raise PilotError("lifecycle-state-invalid")
    matches = [node for node in nodes if isinstance(node, dict) and node.get("node_id") == node_id]
    if len(matches) != 1:
        raise PilotError(f"lifecycle-node-invalid:{node_id}")
    return matches[0]


def _nul_paths(output: str) -> list[str]:
    """Return non-empty NUL-separated portable paths from Git output."""
    return [PurePosixPath(value.replace("\\", "/")).as_posix() for value in output.split("\x00") if value]


def _raw_content_updates(worktree: Path, node: dict[str, Any]) -> None:
    """Reject staged, mode, type, deletion, rename, and multi-content child changes."""
    if _git_bytes(worktree, "diff", "--cached", "--name-only", "-z"):
        raise PilotError(f"child-index-change-forbidden:{node['node_id']}")
    raw = _git_bytes(worktree, "diff", "--raw", "--no-renames", "-z", "HEAD", "--", *node["owned_paths"])
    fields = [field for field in raw.split(b"\0") if field]
    if len(fields) != len(node["owned_paths"]) * 2:
        raise PilotError(f"child-patch-shape-forbidden:{node['node_id']}")
    paths: set[str] = set()
    for metadata, raw_path in zip(fields[::2], fields[1::2], strict=True):
        parts = metadata.split()
        if len(parts) != 5 or not parts[0].startswith(b":"):
            raise PilotError(f"child-patch-shape-forbidden:{node['node_id']}")
        old_mode, new_mode = parts[0][1:], parts[1]
        old_object, new_object, status = parts[2:]
        path = _portable_relative(raw_path.decode("utf-8", errors="strict"), "changed-path")
        if old_mode != new_mode or status != b"M" or old_object == new_object:
            raise PilotError(f"child-patch-shape-forbidden:{node['node_id']}")
        paths.add(path)
    if paths != set(node["owned_paths"]):
        raise PilotError(f"child-patch-shape-forbidden:{node['node_id']}")


def _child_patch(state: dict[str, Any], workspace: Path, node: dict[str, Any]) -> tuple[list[str], bytes]:
    """Return one safe parent-observed child patch and its exact changed paths."""
    node_id = str(node["node_id"])
    worktree = workspace.joinpath(*PurePosixPath(node["worktree_path"]).parts)
    if not worktree.is_dir():
        raise PilotError(f"child-worktree-missing:{node_id}")
    if _git(worktree, "rev-parse", "HEAD") != state.get("baseline_head"):
        raise PilotError(f"child-commit-forbidden:{node_id}")
    if _git(worktree, "ls-files", "--others", "--exclude-standard", "-z"):
        raise PilotError(f"child-untracked-path-forbidden:{node_id}")
    changed_paths = sorted(_nul_paths(_git(worktree, "diff", "--name-only", "--no-renames", "-z", "HEAD", "--")))
    if set(changed_paths) != set(node["owned_paths"]):
        raise PilotError(f"child-owned-path-mismatch:{node_id}")
    _raw_content_updates(worktree, node)
    for relative in changed_paths:
        if worktree.joinpath(*PurePosixPath(relative).parts).is_symlink():
            raise PilotError(f"child-symlink-path-forbidden:{node_id}")
    patch = _git_bytes(worktree, "diff", "--binary", "--full-index", "HEAD", "--", *node["owned_paths"])
    if not patch:
        raise PilotError(f"child-patch-empty:{node_id}")
    return changed_paths, patch


def create_completed_child_handover(
    *, state_path: str | os.PathLike[str], node_id: str, summary: str
) -> dict[str, object]:
    """Create one canonical completed report from the child's frozen worktree.

    This is the policy-compatible child boundary for patch hashing: it uses the same raw Git subprocess bytes that the
    parent join later verifies, without sending ``git diff`` output through an output-rendering shell wrapper.

    Args:
        state_path: Prepared lifecycle state path owned by the parent workflow.
        node_id: Frozen node whose worktree contains the completed edit.
        summary: Concise child result summary, limited to 2,000 characters.

    Returns:
        The exact five-field completed handover accepted by ``join_child_handovers``.

    Raises:
        PilotError: If authority drifted, the lifecycle is no longer prepared, the summary is invalid, or the child
            worktree does not contain one safe exact-owned-path patch.
    """
    try:
        normalized_state_path = Path(state_path)
    except TypeError as error:
        raise PilotError("lifecycle-state-path-invalid") from error
    state, workspace = _read_state(normalized_state_path)
    if _is_code_remediate_state(state):
        return _create_code_remediate_child_handover(state, workspace, normalized_state_path, node_id, summary)
    _require_authority_unchanged(state, workspace, normalized_state_path)
    if state.get("status") != "prepared":
        raise PilotError("child-handover-after-join-forbidden")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 2_000:
        raise PilotError(f"child-handover-summary-invalid:{node_id}")
    node = _node(state, node_id)
    changed_paths, patch = _child_patch(state, workspace, node)
    return {
        "node_id": node_id,
        "status": "completed",
        "summary": summary.strip(),
        "changed_paths": changed_paths,
        "patch_sha256": hashlib.sha256(patch).hexdigest(),
    }


def _record_join_failure(
    state_path: Path,
    state: dict[str, Any],
    handovers: list[dict[str, object]],
    reason: str,
) -> None:
    """Persist bounded child-report diagnostics while retaining all worktrees."""
    planned_ids = {str(node["node_id"]) for node in state.get("nodes", []) if isinstance(node, dict)}
    reason_node = reason.rsplit(":", maxsplit=1)[-1]
    received: list[dict[str, str]] = []
    for handover in handovers:
        if not isinstance(handover, dict):
            continue
        report: dict[str, str] = {}
        node_id = handover.get("node_id")
        status = handover.get("status")
        summary = handover.get("summary")
        if isinstance(node_id, str) and node_id in planned_ids:
            report["node_id"] = node_id
        if isinstance(status, str) and status in {"completed", "failed", "cancelled"}:
            report["status"] = status
        if isinstance(summary, str) and summary.strip():
            report["summary"] = summary.strip()[:2_000]
        received.append(report)
    state["status"] = "join-failed"
    state["join_failure"] = {
        "reason": reason,
        "failed_node": reason_node if reason_node in planned_ids else None,
        "received_reports": received,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    _persist(state_path, state)
    LOGGER.warning("Child handover join failed: %s", reason)


def join_child_handovers(*, state_path: Path, handovers: list[dict[str, object]]) -> dict[str, object]:
    """Verify both completed child reports against their frozen worktrees.

    Args:
        state_path: Prepared lifecycle state owned by the parent workflow.
        handovers: Exactly one completed result per frozen node. Each result contains ``node_id``, ``status``,
            ``summary``, ``changed_paths``, and the SHA-256 of the canonical Git patch bytes.

    Returns:
        Updated lifecycle state with both verified handovers and a parent join timestamp.

    Raises:
        PilotError: If either report is missing, malformed, unsuccessful, or disagrees with the observed worktree.
    """
    state, workspace = _read_state(state_path)
    if _is_code_remediate_state(state):
        return _join_code_remediate_child_handovers(state_path, state, workspace, handovers)
    repository, _, planned_nodes = _require_authority_unchanged(state, workspace, state_path)
    if state.get("status") != "prepared":
        raise PilotError("child-handovers-incomplete")
    if any(isinstance(node, dict) and "handover" in node for node in state.get("nodes", [])):
        raise PilotError("child-handovers-already-joined")
    try:
        if len(handovers) != len(planned_nodes):
            raise PilotError("child-handovers-incomplete")
        reports: dict[str, dict[str, object]] = {}
        required = {"node_id", "status", "summary", "changed_paths", "patch_sha256"}
        for handover in handovers:
            if not isinstance(handover, dict) or set(handover) != required:
                raise PilotError("child-handover-invalid")
            node_id = handover.get("node_id")
            if not isinstance(node_id, str) or node_id in reports:
                raise PilotError("child-handover-invalid")
            reports[node_id] = handover
        if set(reports) != {str(node["node_id"]) for node in planned_nodes}:
            raise PilotError("child-handovers-incomplete")
        persisted_nodes = state.get("nodes")
        if not isinstance(persisted_nodes, list) or any(not isinstance(node, dict) for node in persisted_nodes):
            raise PilotError("lifecycle-state-invalid")
        verified: dict[str, dict[str, object]] = {}
        for node in persisted_nodes:
            node_id = str(node["node_id"])
            handover = reports[node_id]
            summary = handover.get("summary")
            changed_paths = handover.get("changed_paths")
            patch_sha256 = handover.get("patch_sha256")
            if handover.get("status") != "completed":
                raise PilotError(f"child-handover-not-completed:{node_id}")
            if not isinstance(summary, str) or not summary.strip() or len(summary) > 2_000:
                raise PilotError(f"child-handover-summary-invalid:{node_id}")
            if not isinstance(changed_paths, list) or any(not isinstance(path, str) for path in changed_paths):
                raise PilotError(f"child-handover-paths-invalid:{node_id}")
            normalized_paths = [_portable_relative(path, "child-handover-path") for path in changed_paths]
            if normalized_paths != node["owned_paths"]:
                raise PilotError(f"child-handover-paths-mismatch:{node_id}")
            if not isinstance(patch_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", patch_sha256) is None:
                raise PilotError(f"child-handover-patch-hash-invalid:{node_id}")
            observed_paths, patch = _child_patch(state, workspace, node)
            if observed_paths != normalized_paths or hashlib.sha256(patch).hexdigest() != patch_sha256:
                raise PilotError(f"child-handover-patch-mismatch:{node_id}")
            verified[node_id] = {
                "status": "completed",
                "summary": summary.strip(),
                "changed_paths": normalized_paths,
                "patch_sha256": patch_sha256,
            }
    except PilotError as error:
        _record_join_failure(state_path, state, handovers, str(error))
        raise
    for node in persisted_nodes:
        node["handover"] = verified[str(node["node_id"])]
        node["terminal_status"] = "completed"
        node["joined"] = True
    _repository_unchanged(repository, state["baseline_head"], "fixture-repository")
    state["status"] = "joined"
    state["joined_at"] = datetime.now(timezone.utc).isoformat()
    _persist(state_path, state)
    LOGGER.info("Joined and verified %d child handovers", len(persisted_nodes))
    return state


def collect_write_patch(
    *,
    state_path: Path,
    node_id: str,
) -> dict[str, object]:
    """Derive one patch after the parent verifies both child handovers.

    Args:
        state_path: Prepared lifecycle state.
        node_id: Frozen node identifier.

    Returns:
        Updated node evidence including parent-derived patch path and SHA-256.

    Raises:
        PilotError: If the child committed, failed, is unjoined, or changed unsafe paths.
    """
    state, workspace = _read_state(state_path)
    if _is_code_remediate_state(state):
        return _collect_code_remediate_patch(state_path, state, workspace, node_id)
    repository, _, _ = _rederive_authority(state, workspace, state_path)
    node = _node(state, node_id)
    if state.get("status") != "joined" or any(
        not isinstance(record, dict)
        or record.get("terminal_status") != "completed"
        or record.get("joined") is not True
        or not isinstance(record.get("handover"), dict)
        or record["handover"].get("status") != "completed"
        for record in state.get("nodes", [])
    ):
        raise PilotError("pilot-children-not-joined")
    if node.get("patch_status") != "not-collected":
        raise PilotError(f"child-patch-already-collected:{node_id}")
    _repository_unchanged(repository, state["baseline_head"], "fixture-repository")
    changed_paths, patch = _child_patch(state, workspace, node)
    handover = node["handover"]
    if (
        handover.get("changed_paths") != changed_paths
        or handover.get("patch_sha256") != hashlib.sha256(patch).hexdigest()
    ):
        raise PilotError(f"child-handover-drift:{node_id}")
    patch_relative = _relative_evidence_path(workspace, state_path.parent / node["output"], "patch-path")
    _, patch_path = _workspace_path(workspace, patch_relative, "patch-path")
    if patch_path.exists() or patch_path.is_symlink():
        raise PilotError(f"patch-output-exists:{node_id}")
    patch_path.write_bytes(patch)
    node.update(
        {
            "terminal_status": "completed",
            "joined": True,
            "changed_paths": changed_paths,
            "patch_path": patch_relative,
            "patch_sha256": hashlib.sha256(patch).hexdigest(),
            "patch_derived_at": datetime.now(timezone.utc).isoformat(),
            "patch_status": "collected",
        }
    )
    _persist(state_path, state)
    LOGGER.info("Collected parent-derived patch for %s", node_id)
    return node


def _require_authority_unchanged(
    state: dict[str, Any], workspace: Path, state_path: Path
) -> tuple[Path, Path, list[dict[str, Any]]]:
    """Rebuild frozen authority and require its source baseline before Git mutation."""
    repository, worktree_root, nodes = _rederive_authority(state, workspace, state_path)
    _repository_unchanged(repository, state["baseline_head"], "fixture-repository")
    return repository, worktree_root, nodes


def integrate_write_pilot(*, state_path: Path) -> dict[str, object]:
    """Apply every joined hash-bound patch to a fresh generated integration worktree.

    Args:
        state_path: Lifecycle state whose two child patches have been collected.

    Returns:
        Updated state with deterministic integration order and exact final hashes.

    Raises:
        PilotError: If authority drifted, a join is missing, or any patch cannot integrate.
    """
    state, workspace = _read_state(state_path)
    if _is_code_remediate_state(state):
        return _integrate_code_remediate_pilot(state_path, state, workspace)
    repository, _, _ = _require_authority_unchanged(state, workspace, state_path)
    nodes = state.get("nodes")
    if not isinstance(nodes, list) or any(
        not isinstance(node, dict)
        or node.get("terminal_status") != "completed"
        or node.get("joined") is not True
        or node.get("patch_status") != "collected"
        for node in nodes
    ):
        raise PilotError("pilot-children-not-joined")
    ordered = sorted(nodes, key=lambda item: item["node_id"])
    for node in ordered:
        patch_path = workspace.joinpath(*PurePosixPath(node["patch_path"]).parts)
        if _sha256(patch_path) != node.get("patch_sha256"):
            raise PilotError(f"patch-digest-mismatch:{node['node_id']}")
    integration_relative = f"{state['worktree_root']}/integration"
    _, integration = _workspace_path(workspace, integration_relative, "integration-worktree")
    if integration.exists():
        raise PilotError("integration-worktree-exists")
    _require_authority_unchanged(state, workspace, state_path)
    _git(repository, "worktree", "add", "--detach", str(integration), state["baseline_head"])
    state["integration_worktree"] = integration_relative
    state["integration_status"] = "applying"
    _persist(state_path, state)
    applied: list[str] = []
    try:
        for node in ordered:
            patch_path = workspace.joinpath(*PurePosixPath(node["patch_path"]).parts)
            try:
                _require_authority_unchanged(state, workspace, state_path)
                if _sha256(patch_path) != node.get("patch_sha256"):
                    raise PilotError(f"patch-digest-mismatch:{node['node_id']}")
                _git(integration, "apply", "--check", str(patch_path))
                _require_authority_unchanged(state, workspace, state_path)
                if _sha256(patch_path) != node.get("patch_sha256"):
                    raise PilotError(f"patch-digest-mismatch:{node['node_id']}")
                _git(integration, "apply", str(patch_path))
            except PilotError as error:
                state["integration_status"] = "failed"
                state["integration_failed_node"] = node["node_id"]
                _persist(state_path, state)
                raise PilotError(f"patch-integration-failed:{node['node_id']}") from error
            applied.append(node["node_id"])
        if _git(integration, "ls-files", "--others", "--exclude-standard", "-z"):
            raise PilotError("integration-untracked-path-forbidden")
        changed = set(_nul_paths(_git(integration, "diff", "--name-only", "--no-renames", "-z", "HEAD", "--")))
        expected = {path for node in ordered for path in node["owned_paths"]}
        if changed != expected:
            raise PilotError("integration-changed-path-mismatch")
        final_hashes = {path: _sha256(integration.joinpath(*PurePosixPath(path).parts)) for path in sorted(expected)}
        state.update(
            {
                "status": "integrated",
                "integration_status": "passed",
                "integration_order": applied,
                "integration_final_sha256": final_hashes,
            }
        )
        _persist(state_path, state)
    except PilotError:
        if state.get("integration_status") != "failed":
            state["integration_status"] = "failed"
            _persist(state_path, state)
        raise
    LOGGER.info("Integrated generated patches in order %s", applied)
    return state


def cleanup_write_pilot(*, state_path: Path) -> dict[str, object]:
    """Remove successful generated worktrees after preserving patch and integration evidence.

    Args:
        state_path: Persisted successful lifecycle state.

    Returns:
        Updated state with per-worktree cleanup commands and postconditions.

    Raises:
        PilotError: If integration did not pass or any non-force cleanup is uncertain.
    """
    state, workspace = _read_state(state_path)
    repository, _, _ = _require_authority_unchanged(state, workspace, state_path)
    if state.get("integration_status") != "passed" or not state.get("integration_order"):
        raise PilotError("cleanup-before-integration-forbidden")
    integration_relative = f"{state['worktree_root']}/integration"
    if state.get("integration_worktree") != integration_relative:
        raise PilotError("lifecycle-state-integration-path-drift")
    _workspace_path(workspace, integration_relative, "integration-worktree")
    records: list[dict[str, str]] = []
    cleanup_targets = [(node["node_id"], node["worktree_path"], list(node["owned_paths"])) for node in state["nodes"]]
    cleanup_targets.append(
        (
            "integration",
            state["integration_worktree"],
            sorted({path for node in state["nodes"] for path in node["owned_paths"]}),
        )
    )
    state["cleanup_status"] = "in-progress"
    state["cleanup"] = records
    _persist(state_path, state)
    for label, relative, owned_paths in cleanup_targets:
        _, worktree = _workspace_path(workspace, relative, "cleanup-worktree")
        record = {
            "worktree": relative,
            "command": "git worktree remove",
            "result": "pending",
            "postcondition": "present",
        }
        records.append(record)
        _persist(state_path, state)
        try:
            # Patches are durable; restore only the generated bucket paths so non-force removal can succeed.
            _require_authority_unchanged(state, workspace, state_path)
            _git(worktree, "restore", "--staged", "--worktree", "--source=HEAD", "--", *owned_paths)
            _git(repository, "worktree", "remove", str(worktree))
        except PilotError as error:
            record["result"] = "failed"
            state["cleanup_status"] = "failed"
            _persist(state_path, state)
            raise PilotError(f"cleanup-failed:{label}") from error
        record["result"] = "removed"
        record["postcondition"] = "absent" if not worktree.exists() else "present"
        if worktree.exists():
            state["cleanup_status"] = "failed"
            _persist(state_path, state)
            raise PilotError(f"cleanup-postcondition-failed:{label}")
        _persist(state_path, state)
    state["status"] = "complete"
    state["cleanup_status"] = "removed"
    _persist(state_path, state)
    LOGGER.info("Removed %d successful generated worktrees", len(records))
    return state


def _is_code_remediate_state(state: dict[str, Any]) -> bool:
    """Return whether state is the frozen schema-v2 remediation lifecycle."""
    return state.get("schema_version") == 2 and state.get("consumer") == "code-remediate"


#: Pending-operation marker files inside the Git directory as (marker name, label), in reporting order.
_PENDING_OPERATIONS = (
    ("MERGE_HEAD", "merging"),
    ("CHERRY_PICK_HEAD", "cherry-picking"),
    ("REVERT_HEAD", "reverting"),
)


def _git_operation(repository: Path) -> str | None:
    """Return a pending Git operation that makes source state unsafe to apply."""
    queries = [argument for name, _ in _PENDING_OPERATIONS for argument in ("--git-path", name)]
    # One process answers every marker path plus the Git directory; None sends each question to its own process.
    answers = _git_batched_lines(repository, "rev-parse", *queries, "--git-dir", expected=len(_PENDING_OPERATIONS) + 1)
    for index, (name, label) in enumerate(_PENDING_OPERATIONS):
        marker = answers[index] if answers is not None else _git(repository, "rev-parse", "--git-path", name)
        if (repository / marker).is_file():
            return label
    git_dir = repository / (answers[-1] if answers is not None else _git(repository, "rev-parse", "--git-dir"))
    if (git_dir / "rebase-apply").exists() or (git_dir / "rebase-merge").exists():
        return "rebasing"
    return None


def _require_unchanged_code_remediate_source(
    repository: Path, owned_paths: list[str], baseline_head: str | None = None
) -> None:
    """Reject source operations, HEAD drift, and edits overlapping approved paths."""
    operation = _git_operation(repository)
    if operation is not None:
        raise PilotError(f"source-repository-{operation}")
    if _git_bytes(repository, "ls-files", "--unmerged", "-z"):
        raise PilotError("source-repository-unmerged")
    if baseline_head is not None and _git(repository, "rev-parse", "HEAD") != baseline_head:
        raise PilotError("source-repository-head-drift")
    protected = {_canonical_path(path) for path in owned_paths}
    records = _git_bytes(repository, "status", "--porcelain=v1", "-z", "--untracked-files=all").split(b"\0")
    position = 0
    while position < len(records) and records[position]:
        record = records[position]
        if len(record) < 4 or record[2:3] != b" ":
            raise PilotError("source-repository-status-invalid")
        status = record[:2]
        paths = [os.fsdecode(record[3:])]
        # Porcelain -z emits the original name as a second record for renames and copies.
        if b"R" in status or b"C" in status:
            position += 1
            if position >= len(records) or not records[position]:
                raise PilotError("source-repository-status-invalid")
            paths.append(os.fsdecode(records[position]))
        for path in paths:
            candidate = _canonical_path(path)
            if any(
                candidate == owned or candidate.startswith(f"{owned}/") or owned.startswith(f"{candidate}/")
                for owned in protected
            ):
                raise PilotError("source-repository-dirty")
        position += 1


def _validate_code_remediate_approval(plan_path: Path, approval_path: Path) -> tuple[dict[str, Any], str, str]:
    """Bind the schema-v2 plan to its exact four-field approval record."""
    plan = _load_json(plan_path, "plan")
    approval = _load_json(approval_path, "approval")
    plan_sha256 = _sha256(plan_path)
    if set(approval) != {"plan_sha256", "prompt_presented", "response", "source"}:
        raise PilotError("write-approval-invalid")
    if approval.get("plan_sha256") != plan_sha256:
        raise PilotError("plan-approval-digest-mismatch")
    source = approval.get("source")
    if approval.get("response") != "approve" or source not in {
        "explicit-input",
        "user-prompt",
        "workflow-default",
    }:
        raise PilotError("write-approval-invalid")
    if not isinstance(approval.get("prompt_presented"), bool):
        raise PilotError("write-approval-invalid")
    if source == "workflow-default" and approval["prompt_presented"]:
        raise PilotError("write-approval-invalid")
    return plan, plan_sha256, _sha256(approval_path)


def _code_remediate_nodes(plan: dict[str, Any], repository: Path, evidence_root: str) -> list[dict[str, Any]]:
    """Validate the small, closed set of schema-v2 remediation write buckets."""
    buckets = plan.get("work_buckets")
    if not isinstance(buckets, list) or not 2 <= len(buckets) <= 4:
        raise PilotError("plan-node-invalid")
    nodes: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_paths: dict[str, str] = {}
    seen_outputs: set[str] = set()
    seen_contexts: set[str] = set()
    seen_context_paths: set[str] = set()
    seen_locks: set[str] = set()
    for raw in buckets:
        required_fields = {
            "bucket_id",
            "context_pack_path",
            "context_sha256",
            "execution_mode",
            "owner",
            "output",
            "owned_paths",
            "resource_locks",
            "selected_indexes",
            "verifier",
        }
        raw_fields = set(raw) if isinstance(raw, dict) else set()
        if (
            not isinstance(raw, dict)
            or not required_fields.issubset(raw_fields)
            or not raw_fields.issubset(required_fields | {"singleton_rationale"})
        ):
            raise PilotError("plan-node-invalid")
        node_id = raw.get("bucket_id")
        if not isinstance(node_id, str) or re.fullmatch(r"[A-Z0-9-]+", node_id) is None or node_id in seen_ids:
            raise PilotError("plan-node-invalid")
        selected_indexes = raw.get("selected_indexes")
        if (
            not isinstance(selected_indexes, list)
            or not selected_indexes
            or any(not isinstance(index, int) or isinstance(index, bool) for index in selected_indexes)
            or len(selected_indexes) != len(set(selected_indexes))
            or raw.get("execution_mode") != "parallel"
            or not isinstance(raw.get("owner"), str)
            or not raw["owner"]
            or not isinstance(raw.get("verifier"), str)
            or not raw["verifier"]
        ):
            raise PilotError(f"bucket-metadata-invalid:{node_id}")
        rationale = raw.get("singleton_rationale")
        if (len(selected_indexes) == 1) != isinstance(rationale, str) or (isinstance(rationale, str) and not rationale):
            raise PilotError(f"bucket-metadata-invalid:{node_id}")
        owned = raw.get("owned_paths")
        if not isinstance(owned, list) or not owned:
            raise PilotError(f"owned-path-invalid:{node_id}")
        paths: list[str] = []
        bucket_paths: set[str] = set()
        for value in owned:
            path = _portable_relative(value, "owned-path")
            canonical = _canonical_path(path)
            if (
                canonical in bucket_paths
                or (canonical in seen_paths and seen_paths[canonical] != path)
                or any(
                    canonical.startswith(f"{existing}/") or existing.startswith(f"{canonical}/")
                    for existing in seen_paths
                )
            ):
                raise PilotError(f"owned-path-overlap:{node_id}")
            target = repository.joinpath(*PurePosixPath(path).parts)
            if target.is_symlink() or not target.is_file():
                raise PilotError(f"owned-path-not-regular:{node_id}")
            try:
                _git(repository, "ls-files", "--error-unmatch", "--", path)
            except PilotError as error:
                raise PilotError(f"owned-path-not-tracked:{node_id}") from error
            seen_paths[canonical] = path
            bucket_paths.add(canonical)
            paths.append(path)
        locks = raw.get("resource_locks")
        if not isinstance(locks, list) or any(not isinstance(lock, str) or not lock for lock in locks):
            raise PilotError(f"resource-lock-invalid:{node_id}")
        if seen_locks.intersection(locks):
            raise PilotError(f"resource-lock-overlap:{node_id}")
        context = raw.get("context_sha256")
        if not isinstance(context, str) or re.fullmatch(r"[0-9a-f]{64}", context) is None or context in seen_contexts:
            raise PilotError(f"context-hash-invalid:{node_id}")
        context_relative = _portable_relative(raw.get("context_pack_path"), "context-pack-path")
        _, context_path = _code_remediate_evidence_path(
            repository, f"{evidence_root}/{context_relative}", "context-pack-path"
        )
        canonical_context = _canonical_path(context_relative)
        if canonical_context in seen_context_paths:
            raise PilotError(f"context-pack-path-duplicate:{node_id}")
        if not context_path.is_file():
            raise PilotError(f"context-pack-missing:{node_id}")
        if _sha256(context_path) != context:
            raise PilotError(f"context-pack-digest-mismatch:{node_id}")
        output = _portable_relative(raw.get("output"), "patch-output")
        if "/" in output or _canonical_path(output) in seen_outputs:
            raise PilotError(f"patch-output-invalid:{node_id}")
        seen_ids.add(node_id)
        seen_locks.update(locks)
        seen_contexts.add(context)
        seen_context_paths.add(canonical_context)
        seen_outputs.add(_canonical_path(output))
        nodes.append(
            {
                "node_id": node_id,
                "selected_indexes": list(selected_indexes),
                "owner": raw["owner"],
                "verifier": raw["verifier"],
                "owned_paths": sorted(paths),
                "resource_locks": list(locks),
                "context_pack_path": context_relative,
                "context_sha256": context,
                "output": output,
                "execution_mode": "parallel",
                **({"singleton_rationale": rationale} if rationale is not None else {}),
            }
        )
    if [node["node_id"] for node in nodes] != sorted(node["node_id"] for node in nodes):
        raise PilotError("plan-node-order-invalid")
    return nodes


def prepare_code_remediate_pilot(
    *, plan_path: Path, approval_path: Path, workspace_root: Path, state_path: Path
) -> dict[str, object]:
    """Prepare bounded remediation worktrees from an approved source baseline."""
    workspace = workspace_root.resolve(strict=True)
    plan, plan_sha256, approval_sha256 = _validate_code_remediate_approval(plan_path, approval_path)
    if (
        plan.get("schema_version") != 2
        or plan.get("consumer") != "code-remediate"
        or plan.get("write_parallel_promoted") is not False
        or plan.get("status") != "frozen-awaiting-explicit-approval"
        or plan.get("rollback_policy") != "approved-paths-if-preapply-baseline-matches"
        or plan.get("cleanup_policy") != "non-force-after-durable-source-application"
        or plan.get("verification_gate") != "code-remediate-shared-quality-gates"
    ):
        raise PilotError("plan-authority-invalid")
    source_relative, source = _code_remediate_source_path(workspace, plan.get("source_repository"), "source-repository")
    if tuple(PurePosixPath(source_relative).parts[: len(_CODE_REMEDIATE_PREFIX)]) == _CODE_REMEDIATE_PREFIX:
        raise PilotError("source-repository-invalid")
    if not source.is_dir() or not (source / ".git").exists():
        raise PilotError("source-repository-invalid")
    plan_relative, expected_plan = _code_remediate_evidence_path(
        source, _relative_workspace_path(source, plan_path, "plan-path"), "plan-path"
    )
    approval_relative, expected_approval = _code_remediate_evidence_path(
        source, _relative_workspace_path(source, approval_path, "approval-path"), "approval-path"
    )
    evidence_root = str(PurePosixPath(plan_relative).parent)
    if PurePosixPath(approval_relative).parent != PurePosixPath(evidence_root):
        raise PilotError("approval-path-outside-run")
    state_relative, expected_state = _code_remediate_evidence_path(
        source, _relative_workspace_path(source, state_path, "state-path"), "state-path"
    )
    if expected_plan != plan_path or expected_approval != approval_path or expected_state != state_path:
        raise PilotError("lifecycle-state-path-mismatch")
    planned_state = _portable_relative(plan.get("state_path"), "planned-state-path")
    if "/" in planned_state or state_relative != f"{evidence_root}/{planned_state}":
        raise PilotError("lifecycle-state-plan-mismatch")
    if state_path.exists() or state_path.is_symlink():
        raise PilotError("lifecycle-state-exists")
    state_temporary = state_path.with_name(f".{state_path.name}.tmp")
    if state_temporary.exists() or state_temporary.is_symlink():
        raise PilotError("state-temporary-exists")
    worktree_relative, worktree_root = _code_remediate_worktree_path(
        workspace, source_relative, plan.get("worktree_root"), "worktree-root"
    )
    nodes = _code_remediate_nodes(plan, source, evidence_root)
    _require_unchanged_code_remediate_source(source, [path for node in nodes for path in node["owned_paths"]])
    baseline_head, baseline_tree = _git_head_and_tree(source)
    if plan.get("baseline_head") != baseline_head or plan.get("baseline_tree") != baseline_tree:
        raise PilotError("source-repository-baseline-mismatch")
    for output in [*(node["output"] for node in nodes), "source-application.patch", "rollback.patch"]:
        _, output_path = _code_remediate_evidence_path(source, f"{evidence_root}/{output}", "lifecycle-output")
        if output_path.exists() or output_path.is_symlink():
            raise PilotError(f"lifecycle-output-exists:{output}")
    if worktree_root.exists():
        raise PilotError("worktree-root-exists")
    worktree_root.mkdir(parents=True)
    state: dict[str, Any] = {
        "schema_version": 2,
        "consumer": "code-remediate",
        "status": "preparing",
        "workspace_root": str(workspace),
        "state_path": state_relative,
        "evidence_root": evidence_root,
        "plan_path": plan_relative,
        "plan_sha256": plan_sha256,
        "approval_path": approval_relative,
        "approval_sha256": approval_sha256,
        "source_repository": source_relative,
        "worktree_root": worktree_relative,
        "baseline_head": baseline_head,
        "baseline_tree": baseline_tree,
        "source": {"baseline_head": baseline_head, "baseline_tree": baseline_tree},
        "nodes": [],
        "verification_gate": "code-remediate-shared-quality-gates",
        "integration_status": "not-started",
        "cleanup_status": "not-started",
        "containment": {"mode": "parent-authoritative-worktrees", "capability_sandbox_verified": False},
    }
    try:
        for node in nodes:
            worktree_path = f"{worktree_relative}/{node['node_id']}"
            _, worktree = _code_remediate_worktree_path(
                workspace, source_relative, worktree_path, "child-worktree-path"
            )
            _git(source, "worktree", "add", "--detach", str(worktree), baseline_head)
            state["nodes"].append(
                {
                    **node,
                    "worktree_path": worktree_path,
                    "terminal_status": "pending",
                    "joined": False,
                    "patch_status": "not-collected",
                }
            )
        state["status"] = "prepared"
        _persist(state_path, state)
    except PilotError:
        state["status"] = "prepare-failed"
        _persist(state_path, state)
        raise
    LOGGER.info("Prepared code-remediate worktree pilot with %d buckets", len(nodes))
    return state


def _code_remediate_authority(
    state: dict[str, Any], workspace: Path, state_path: Path, *, clean: bool
) -> tuple[Path, list[dict[str, Any]]]:
    """Re-derive schema-v2 authority before each parent-controlled transition."""
    if not _is_code_remediate_state(state):
        raise PilotError("lifecycle-state-schema-invalid")
    source_relative, source = _code_remediate_source_path(
        workspace, state.get("source_repository"), "source-repository"
    )
    if source_relative != state.get("source_repository") or not source.is_dir():
        raise PilotError("lifecycle-state-root-drift")
    plan_relative, plan_path = _code_remediate_evidence_path(source, state.get("plan_path"), "plan-path")
    approval_relative, approval_path = _code_remediate_evidence_path(
        source, state.get("approval_path"), "approval-path"
    )
    if plan_relative != state.get("plan_path") or approval_relative != state.get("approval_path"):
        raise PilotError("lifecycle-state-path-mismatch")
    evidence_root = str(PurePosixPath(plan_relative).parent)
    if state.get("evidence_root") != evidence_root or PurePosixPath(approval_relative).parent != PurePosixPath(
        evidence_root
    ):
        raise PilotError("lifecycle-state-root-drift")
    plan, plan_sha256, approval_sha256 = _validate_code_remediate_approval(plan_path, approval_path)
    if plan_sha256 != state.get("plan_sha256") or approval_sha256 != state.get("approval_sha256"):
        raise PilotError("plan-drift-before-transition")
    if _portable_relative(plan.get("source_repository"), "source-repository") != source_relative:
        raise PilotError("lifecycle-state-root-drift")
    planned_state = _portable_relative(plan.get("state_path"), "planned-state-path")
    if (
        "/" in planned_state
        or state.get("state_path") != f"{evidence_root}/{planned_state}"
        or plan.get("verification_gate") != "code-remediate-shared-quality-gates"
        or state.get("verification_gate") != "code-remediate-shared-quality-gates"
    ):
        raise PilotError("lifecycle-state-plan-mismatch")
    worktree_root, _ = _code_remediate_worktree_path(
        workspace, source_relative, plan.get("worktree_root"), "worktree-root"
    )
    if worktree_root != state.get("worktree_root"):
        raise PilotError("lifecycle-state-root-drift")
    nodes = _code_remediate_nodes(plan, source, evidence_root)
    recorded = state.get("nodes")
    if not isinstance(recorded, list) or len(recorded) != len(nodes):
        raise PilotError("lifecycle-state-node-drift")
    for planned, node in zip(nodes, recorded, strict=True):
        if not isinstance(node, dict) or any(node.get(key) != planned[key] for key in planned):
            raise PilotError("lifecycle-state-node-drift")
        expected = f"{state['worktree_root']}/{planned['node_id']}"
        if node.get("worktree_path") != expected:
            raise PilotError("lifecycle-state-node-drift")
        _code_remediate_worktree_path(workspace, source_relative, expected, "child-worktree-path")
    if plan.get("baseline_head") != state.get("baseline_head") or plan.get("baseline_tree") != state.get(
        "baseline_tree"
    ):
        raise PilotError("lifecycle-state-baseline-invalid")
    if clean:
        _require_unchanged_code_remediate_source(
            source, [path for node in nodes for path in node["owned_paths"]], state["baseline_head"]
        )
    elif _git_operation(source) is not None:
        raise PilotError("source-repository-operation-in-progress")
    return source, nodes


def _code_remediate_child_patch(
    state: dict[str, Any], workspace: Path, node: dict[str, Any]
) -> tuple[list[str], bytes]:
    """Observe one remediation child patch, including ignored-file violations."""
    node_id = str(node["node_id"])
    _, worktree = _code_remediate_worktree_path(
        workspace, state["source_repository"], node.get("worktree_path"), "child-worktree-path"
    )
    if not worktree.is_dir():
        raise PilotError(f"child-worktree-missing:{node_id}")
    if _git(worktree, "rev-parse", "HEAD") != state.get("baseline_head"):
        raise PilotError(f"child-commit-forbidden:{node_id}")
    if _git_bytes(worktree, "ls-files", "--others", "-z"):
        raise PilotError(f"child-untracked-path-forbidden:{node_id}")
    changed_paths = sorted(_nul_paths(_git(worktree, "diff", "--name-only", "--no-renames", "-z", "HEAD", "--")))
    if set(changed_paths) != set(node["owned_paths"]):
        raise PilotError(f"child-owned-path-mismatch:{node_id}")
    _raw_content_updates(worktree, node)
    patch = _git_bytes(worktree, "diff", "--binary", "--full-index", "HEAD", "--", *node["owned_paths"])
    if not patch:
        raise PilotError(f"child-patch-empty:{node_id}")
    return changed_paths, patch


def _create_code_remediate_child_handover(
    state: dict[str, Any], workspace: Path, state_path: Path, node_id: str, summary: str
) -> dict[str, object]:
    """Create the shared five-field handover for one schema-v2 child patch."""
    _code_remediate_authority(state, workspace, state_path, clean=True)
    if state.get("status") != "prepared" or not isinstance(summary, str) or not summary.strip() or len(summary) > 2_000:
        raise PilotError(f"child-handover-summary-invalid:{node_id}")
    node = _node(state, node_id)
    paths, patch = _code_remediate_child_patch(state, workspace, node)
    return {
        "node_id": node_id,
        "status": "completed",
        "summary": summary.strip(),
        "changed_paths": paths,
        "patch_sha256": hashlib.sha256(patch).hexdigest(),
    }


def _join_code_remediate_child_handovers(
    state_path: Path, state: dict[str, Any], workspace: Path, handovers: list[dict[str, object]]
) -> dict[str, object]:
    """Join every schema-v2 child handover against parent-observed Git bytes."""
    source, nodes = _code_remediate_authority(state, workspace, state_path, clean=True)
    if state.get("status") != "prepared" or len(handovers) != len(nodes):
        raise PilotError("child-handovers-incomplete")
    reports = {report.get("node_id"): report for report in handovers if isinstance(report, dict)}
    if len(reports) != len(nodes) or set(reports) != {node["node_id"] for node in nodes}:
        raise PilotError("child-handovers-incomplete")
    for node in state["nodes"]:
        report = reports[node["node_id"]]
        if (
            set(report) != {"node_id", "status", "summary", "changed_paths", "patch_sha256"}
            or report.get("status") != "completed"
            or not isinstance(report.get("summary"), str)
            or not report["summary"].strip()
            or len(report["summary"]) > 2_000
        ):
            raise PilotError(f"child-handover-invalid:{node['node_id']}")
        paths, patch = _code_remediate_child_patch(state, workspace, node)
        if report.get("changed_paths") != paths or report.get("patch_sha256") != hashlib.sha256(patch).hexdigest():
            raise PilotError(f"child-handover-patch-mismatch:{node['node_id']}")
        node["handover"] = {key: report[key] for key in ("status", "summary", "changed_paths", "patch_sha256")}
        node["terminal_status"] = "completed"
        node["joined"] = True
    _require_unchanged_code_remediate_source(
        source, [path for node in nodes for path in node["owned_paths"]], state["baseline_head"]
    )
    state["joined_nodes"] = [
        {
            "node_id": node["node_id"],
            "owned_paths": node["owned_paths"],
            "patch_sha256": node["handover"]["patch_sha256"],
        }
        for node in state["nodes"]
    ]
    state["status"] = "joined"
    state["joined_at"] = datetime.now(timezone.utc).isoformat()
    _persist(state_path, state)
    return state


def _collect_code_remediate_patch(
    state_path: Path, state: dict[str, Any], workspace: Path, node_id: str
) -> dict[str, object]:
    """Persist one parent-derived schema-v2 child patch after the complete join."""
    _code_remediate_authority(state, workspace, state_path, clean=True)
    node = _node(state, node_id)
    if state.get("status") != "joined" or node.get("patch_status") != "not-collected":
        raise PilotError("pilot-children-not-joined")
    paths, patch = _code_remediate_child_patch(state, workspace, node)
    if node.get("handover", {}).get("patch_sha256") != hashlib.sha256(patch).hexdigest():
        raise PilotError(f"child-handover-drift:{node_id}")
    source = workspace.joinpath(*PurePosixPath(state["source_repository"]).parts)
    patch_relative, patch_path = _code_remediate_evidence_path(
        source, _relative_workspace_path(source, state_path.parent / node["output"], "patch-path"), "patch-path"
    )
    if patch_path.exists() or patch_path.is_symlink():
        raise PilotError(f"patch-output-exists:{node_id}")
    patch_path.write_bytes(patch)
    node.update(
        {
            "changed_paths": paths,
            "patch_path": patch_relative,
            "patch_sha256": hashlib.sha256(patch).hexdigest(),
            "patch_status": "collected",
        }
    )
    _persist(state_path, state)
    return node


def _integrate_code_remediate_pilot(state_path: Path, state: dict[str, Any], workspace: Path) -> dict[str, object]:
    """Integrate schema-v2 patches, retaining shared-file conflicts for the parent."""
    source, _ = _code_remediate_authority(state, workspace, state_path, clean=True)
    nodes = state.get("nodes")
    if (
        state.get("status") != "joined"
        or not isinstance(nodes, list)
        or any(node.get("patch_status") != "collected" for node in nodes)
    ):
        raise PilotError("pilot-children-not-joined")
    ordered = sorted(nodes, key=lambda node: node["node_id"])
    integration_relative = f"{state['worktree_root']}/integration"
    _, integration = _code_remediate_worktree_path(
        workspace, state["source_repository"], integration_relative, "integration-worktree"
    )
    if integration.exists():
        raise PilotError("integration-worktree-exists")
    _git(source, "worktree", "add", "--detach", str(integration), state["baseline_head"])
    state.update({"integration_worktree": integration_relative, "integration_status": "applying"})
    _persist(state_path, state)
    applied_paths: set[str] = set()
    try:
        for node in ordered:
            _, patch_path = _code_remediate_evidence_path(source, node.get("patch_path"), "patch-path")
            if _sha256(patch_path) != node.get("patch_sha256"):
                raise PilotError(f"patch-digest-mismatch:{node['node_id']}")
            try:
                _git(integration, "apply", "--check", str(patch_path))
                _git(integration, "apply", str(patch_path))
            except PilotError as error:
                conflict_paths = sorted(applied_paths.intersection(node["owned_paths"]))
                if not conflict_paths:
                    raise
                state.update(
                    {
                        "status": "integration-needs-reconciliation",
                        "integration_status": "needs-reconciliation",
                        "integration_failed_node": node["node_id"],
                        "integration_conflict_paths": conflict_paths,
                        "integration_order": [item["node_id"] for item in ordered],
                    }
                )
                _persist(state_path, state)
                raise PilotError(f"patch-integration-needs-reconciliation:{node['node_id']}") from error
            applied_paths.update(node["owned_paths"])
        if _git_bytes(integration, "ls-files", "--others", "-z"):
            raise PilotError("integration-untracked-path-forbidden")
        expected = sorted({path for node in ordered for path in node["owned_paths"]})
        changed = sorted(_nul_paths(_git(integration, "diff", "--name-only", "--no-renames", "-z", "HEAD", "--")))
        if changed != expected:
            raise PilotError("integration-changed-path-mismatch")
        final = {path: _sha256(integration.joinpath(*PurePosixPath(path).parts)) for path in expected}
        final_filtered = {path: _git_filtered_oid(integration, path) for path in expected}
        state.update(
            {
                "status": "integrated",
                "integration_status": "structurally-verified",
                "integration_order": [node["node_id"] for node in ordered],
                "integration_final_sha256": final,
                "integration_final_filtered_oid": final_filtered,
                "integration": {
                    "status": "structurally-verified",
                    "order": [node["node_id"] for node in ordered],
                    "paths": expected,
                },
            }
        )
        _persist(state_path, state)
    except PilotError:
        if state["integration_status"] != "needs-reconciliation":
            state["integration_status"] = "failed"
            _persist(state_path, state)
        raise
    return state


def reconcile_code_remediate_integration(*, state_path: Path, record_path: Path) -> dict[str, object]:
    """Verify a parent-edited integration worktree against an exact run-local resolution record."""
    state, workspace = _read_state(state_path)
    source, _ = _code_remediate_authority(state, workspace, state_path, clean=True)
    if (
        state.get("status") != "integration-needs-reconciliation"
        or state.get("integration_status") != "needs-reconciliation"
    ):
        raise PilotError("integration-reconciliation-not-required")
    _, integration = _code_remediate_worktree_path(
        workspace, state["source_repository"], state.get("integration_worktree"), "integration-worktree"
    )
    record_file = _code_remediate_cli_artifact_path(state_path, record_path, "reconciliation-record")
    record = _load_json(record_file, "reconciliation-record")
    ordered = sorted(state["nodes"], key=lambda node: node["node_id"])
    paths = sorted({path for node in ordered for path in node["owned_paths"]})
    patch_hashes = {node["node_id"]: node["patch_sha256"] for node in ordered}
    required = {
        "baseline_head",
        "failed_node",
        "integration_order",
        "paths",
        "patch_sha256",
        "postimage_sha256",
        "summary",
    }
    if (
        set(record) != required
        or record["baseline_head"] != state["baseline_head"]
        or record["failed_node"] != state["integration_failed_node"]
        or record["integration_order"] != [node["node_id"] for node in ordered]
        or record["paths"] != paths
        or record["patch_sha256"] != patch_hashes
        or not isinstance(record["summary"], str)
        or not record["summary"].strip()
        or len(record["summary"]) > 2_000
    ):
        raise PilotError("integration-reconciliation-record-invalid")
    for node in ordered:
        _, patch_path = _code_remediate_evidence_path(source, node["patch_path"], "patch-path")
        if _sha256(patch_path) != patch_hashes[node["node_id"]]:
            raise PilotError(f"patch-digest-mismatch:{node['node_id']}")
    if _git(integration, "rev-parse", "HEAD") != state["baseline_head"]:
        raise PilotError("integration-reconciliation-worktree-drift")
    if _git_bytes(integration, "ls-files", "--others", "-z"):
        raise PilotError("integration-reconciliation-untracked-path")
    changed = sorted(_nul_paths(_git(integration, "diff", "--name-only", "--no-renames", "-z", "HEAD", "--")))
    if changed != paths:
        raise PilotError("integration-reconciliation-path-mismatch")
    for node in ordered:
        _raw_content_updates(integration, node)
    postimages = {path: _sha256(integration.joinpath(*PurePosixPath(path).parts)) for path in paths}
    if record["postimage_sha256"] != postimages:
        raise PilotError("integration-reconciliation-postimage-mismatch")
    filtered = {path: _git_filtered_oid(integration, path) for path in paths}
    record_relative = _relative_workspace_path(state_path.parent, record_file, "reconciliation-record")
    state.update(
        {
            "status": "integrated",
            "integration_status": "structurally-verified",
            "integration_final_sha256": postimages,
            "integration_final_filtered_oid": filtered,
            "integration": {
                "status": "structurally-verified",
                "order": [node["node_id"] for node in ordered],
                "paths": paths,
            },
            "reconciliation": {
                "status": "verified",
                "record_path": record_relative,
                "record_sha256": _sha256(record_file),
                "changed_paths": changed,
                "failed_node": state["integration_failed_node"],
                "postimage_sha256": postimages,
            },
        }
    )
    _persist(state_path, state)
    return state


def apply_code_remediate_source(*, state_path: Path) -> dict[str, object]:
    """Apply one verified integrated bundle to the unchanged authoritative source."""
    state, workspace = _read_state(state_path)
    source, _ = _code_remediate_authority(state, workspace, state_path, clean=True)
    if state.get("status") != "integrated" or state.get("integration_status") != "structurally-verified":
        raise PilotError("source-apply-before-integration-forbidden")
    reconciliation = state.get("reconciliation")
    if state.get("integration_failed_node") is not None and reconciliation is None:
        raise PilotError("integration-reconciliation-evidence-drift")
    if reconciliation is not None:
        if not isinstance(reconciliation, dict) or reconciliation.get("status") != "verified":
            raise PilotError("integration-reconciliation-evidence-drift")
        _, record_file = _code_remediate_evidence_path(
            source, f"{state['evidence_root']}/{reconciliation.get('record_path')}", "reconciliation-record"
        )
        if _sha256(record_file) != reconciliation.get("record_sha256"):
            raise PilotError("integration-reconciliation-evidence-drift")
    _, integration = _code_remediate_worktree_path(
        workspace, state["source_repository"], state.get("integration_worktree"), "integration-worktree"
    )
    postimages = state.get("integration_final_sha256")
    postimage_filtered_oids = state.get("integration_final_filtered_oid")
    expected_paths = sorted({path for node in state["nodes"] for path in node["owned_paths"]})
    if (
        not isinstance(postimages, dict)
        or not isinstance(postimage_filtered_oids, dict)
        or sorted(postimages) != expected_paths
        or sorted(postimage_filtered_oids) != expected_paths
    ):
        raise PilotError("integration-worktree-drift")
    paths = expected_paths

    # The retained integration worktree is mutable host state. Re-establish its exact structural evidence before any
    # authoritative source patch is written or applied.
    try:
        changed = sorted(_nul_paths(_git(integration, "diff", "--name-only", "--no-renames", "-z", "HEAD", "--")))
        observed_postimages = {path: _sha256(integration.joinpath(*PurePosixPath(path).parts)) for path in paths}
        observed_filtered_oids = {path: _git_filtered_oid(integration, path) for path in paths}
        for node in state["nodes"]:
            _raw_content_updates(integration, node)
        if (
            _git(integration, "rev-parse", "HEAD") != state["baseline_head"]
            or _git_bytes(integration, "ls-files", "--others", "-z")
            or changed != paths
            or observed_postimages != postimages
            or observed_filtered_oids != postimage_filtered_oids
        ):
            raise PilotError("integration-worktree-drift")
    except (OSError, PilotError) as error:
        raise PilotError("integration-worktree-drift") from error
    preimages = {path: _sha256(source.joinpath(*PurePosixPath(path).parts)) for path in paths}
    preimage_filtered_oids = {path: _git_filtered_oid(source, path) for path in paths}
    _, patch_path = _code_remediate_evidence_path(
        source,
        _relative_workspace_path(source, state_path.parent / "source-application.patch", "source-application-patch"),
        "source-application-patch",
    )
    _, rollback_path = _code_remediate_evidence_path(
        source,
        _relative_workspace_path(source, state_path.parent / "rollback.patch", "rollback-patch"),
        "rollback-patch",
    )
    if patch_path.exists() or rollback_path.exists():
        raise PilotError("source-application-output-exists")
    patch_path.write_bytes(_git_bytes(integration, "diff", "--binary", "--full-index", "HEAD", "--", *paths))
    rollback_path.write_bytes(_git_bytes(integration, "diff", "-R", "--binary", "--full-index", "HEAD", "--", *paths))
    if not patch_path.read_bytes() or not rollback_path.read_bytes():
        raise PilotError("source-application-patch-empty")
    application = {
        "status": "applying",
        "patch_path": _relative_workspace_path(state_path.parent, patch_path, "source-application-patch"),
        "patch_sha256": _sha256(patch_path),
        "rollback_patch_path": _relative_workspace_path(state_path.parent, rollback_path, "rollback-patch"),
        "rollback_patch_sha256": _sha256(rollback_path),
        "applied_paths": paths,
    }
    state.update({"status": "source-applying", "source_application": application})
    _persist(state_path, state)
    # Preflight failures have not changed approved paths, so they must never invoke rollback restoration.
    try:
        _require_unchanged_code_remediate_source(source, paths, state["baseline_head"])
        _git(source, "apply", "--check", str(patch_path))
    except PilotError:
        application["status"] = "failed"
        state["status"] = "failed"
        _persist(state_path, state)
        raise
    try:
        _git(source, "apply", str(patch_path))
        observed_filtered_oids = {path: _git_filtered_oid(source, path) for path in paths}
        if observed_filtered_oids != postimage_filtered_oids:
            raise PilotError("source-application-postimage-mismatch")
    except PilotError as error:
        try:
            observed = {path: _sha256(source.joinpath(*PurePosixPath(path).parts)) for path in paths}
            observed_filtered_oids = {path: _git_filtered_oid(source, path) for path in paths}
        except (OSError, PilotError):
            observed = {}
            observed_filtered_oids = {}
        restored = False
        restored_hashes: dict[str, str] = {}
        if len(observed_filtered_oids) == len(paths) and all(
            observed_filtered_oids[path] in {preimage_filtered_oids[path], postimage_filtered_oids[path]}
            for path in paths
        ):
            try:
                _git(source, "restore", "--source", state["baseline_head"], "--staged", "--worktree", "--", *paths)
                restored_hashes = {path: _sha256(source.joinpath(*PurePosixPath(path).parts)) for path in paths}
                restored_filtered_oids = {path: _git_filtered_oid(source, path) for path in paths}
                restored = restored_filtered_oids == preimage_filtered_oids
            except (OSError, PilotError):
                restored = False
        application["status"] = "failed-rolled-back" if restored else "rollback-ambiguous"
        application["rollback_observed_sha256"] = restored_hashes or observed
        application["rollback_preimages_verified"] = restored
        state["status"] = application["status"]
        _persist(state_path, state)
        if not restored:
            raise PilotError("rollback-ambiguous") from error
        raise
    application["status"] = "applied"
    state["source"] = {
        "baseline_head": state["baseline_head"],
        "baseline_tree": state["baseline_tree"],
        "applied_head": _git(source, "rev-parse", "HEAD"),
        "preimage_sha256": preimages,
        "postimage_sha256": {path: _sha256(source.joinpath(*PurePosixPath(path).parts)) for path in paths},
    }
    state["status"] = "source-applied"
    _persist(state_path, state)
    return application


def cleanup_code_remediate_pilot(*, state_path: Path) -> dict[str, object]:
    """Remove only successful schema-v2 worktrees after source evidence is durable."""
    state, workspace = _read_state(state_path)
    source, _ = _code_remediate_authority(state, workspace, state_path, clean=False)
    application = state.get("source_application")
    if (
        state.get("status") != "source-applied"
        or not isinstance(application, dict)
        or application.get("status") != "applied"
    ):
        raise PilotError("cleanup-before-source-application-forbidden")
    paths = application.get("applied_paths")
    if not isinstance(paths, list) or state.get("source", {}).get("postimage_sha256") != {
        path: _sha256(source.joinpath(*PurePosixPath(path).parts)) for path in paths
    }:
        raise PilotError("cleanup-source-postimage-mismatch")
    rollback_relative = _portable_relative(application.get("rollback_patch_path"), "rollback-patch")
    _, rollback = _code_remediate_evidence_path(
        source, f"{state['evidence_root']}/{rollback_relative}", "rollback-patch"
    )
    if _sha256(rollback) != application.get("rollback_patch_sha256"):
        raise PilotError("cleanup-rollback-evidence-mismatch")
    targets = [node["worktree_path"] for node in state["nodes"]] + [state["integration_worktree"]]
    cleanup = {"status": "in-progress", "force": False, "worktrees": []}
    state.update({"cleanup_status": "in-progress", "cleanup": cleanup})
    _persist(state_path, state)
    for relative in targets:
        _, worktree = _code_remediate_worktree_path(workspace, state["source_repository"], relative, "cleanup-worktree")
        try:
            _git(worktree, "restore", "--staged", "--worktree", "--source=HEAD", "--", *paths)
            _git(source, "worktree", "remove", str(worktree))
        except PilotError as error:
            cleanup["status"] = "failed"
            state["cleanup_status"] = "failed"
            _persist(state_path, state)
            raise PilotError("cleanup-failed") from error
        if worktree.exists():
            cleanup["status"] = "failed"
            state["cleanup_status"] = "failed"
            _persist(state_path, state)
            raise PilotError("cleanup-postcondition-failed")
        cleanup["worktrees"].append(relative)
        _persist(state_path, state)
    cleanup["status"] = "removed"
    state.update({"status": "completed", "cleanup_status": "removed"})
    _persist(state_path, state)
    return {"cleanup_status": "removed", "force": False}


def _code_remediate_cli_artifact_path(state_path: Path, artifact_path: Path, label: str) -> Path:
    """Resolve one CLI handover artifact below the exact run evidence root without symlinks."""
    state, workspace = _read_state(state_path)
    source, _ = _code_remediate_authority(state, workspace, state_path, clean=True)
    try:
        absolute = Path(os.path.abspath(artifact_path))
        source_relative = absolute.relative_to(source).as_posix()
    except (OSError, ValueError) as error:
        raise PilotError(f"{label}-outside-run") from error
    relative, candidate = _code_remediate_evidence_path(source, source_relative, label)
    root_parts = PurePosixPath(state["evidence_root"]).parts
    if PurePosixPath(relative).parts[: len(root_parts)] != root_parts or len(PurePosixPath(relative).parts) == len(
        root_parts
    ):
        raise PilotError(f"{label}-outside-run")
    return candidate


def _write_handover_json(state_path: Path, output_path: Path, handover: dict[str, object]) -> None:
    """Persist one child handover only inside its prepared remediation run directory."""
    output_path = _code_remediate_cli_artifact_path(state_path, output_path, "child-handover-output")
    if output_path.exists() or output_path.is_symlink():
        raise PilotError("child-handover-output-exists")
    output_path.write_text(json.dumps(handover, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def _cli_parser() -> argparse.ArgumentParser:
    """Build the narrow explicit command surface for the schema-v2 lifecycle."""
    parser = argparse.ArgumentParser(description="Run the frozen code-remediate parallel-write lifecycle.")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="prepare approved remediation worktrees")
    prepare.add_argument("--plan", type=Path, required=True)
    prepare.add_argument("--approval", type=Path, required=True)
    prepare.add_argument("--workspace", type=Path, required=True)
    prepare.add_argument("--state", type=Path, required=True)
    handover = commands.add_parser("create-handover", help="write one parent-verifiable child handover JSON")
    handover.add_argument("--state", type=Path, required=True)
    handover.add_argument("--node", required=True)
    handover.add_argument("--summary", required=True)
    handover.add_argument("--output", type=Path, required=True)
    join = commands.add_parser("join", help="join completed child handover JSON files")
    join.add_argument("--state", type=Path, required=True)
    join.add_argument("--handover", type=Path, action="append", required=True)
    collect = commands.add_parser("collect", help="collect one parent-derived child patch")
    collect.add_argument("--state", type=Path, required=True)
    collect.add_argument("--node", required=True)
    reconcile = commands.add_parser("reconcile", help="verify a parent-edited conflicting integration")
    reconcile.add_argument("--state", type=Path, required=True)
    reconcile.add_argument("--record", type=Path, required=True)
    for command, help_text in (
        ("integrate", "integrate all collected child patches"),
        ("apply-source", "apply the verified integrated source bundle"),
        ("cleanup", "remove successful worktrees without force"),
    ):
        subparser = commands.add_parser(command, help=help_text)
        subparser.add_argument("--state", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run one explicit code-remediate lifecycle subcommand and emit JSON evidence."""
    args = _cli_parser().parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare_code_remediate_pilot(
                plan_path=args.plan, approval_path=args.approval, workspace_root=args.workspace, state_path=args.state
            )
        elif args.command == "create-handover":
            result = create_completed_child_handover(state_path=args.state, node_id=args.node, summary=args.summary)
            _write_handover_json(args.state, args.output, result)
        elif args.command == "join":
            result = join_child_handovers(
                state_path=args.state,
                handovers=[
                    _load_json(_code_remediate_cli_artifact_path(args.state, path, "child-handover"), "child-handover")
                    for path in args.handover
                ],
            )
        elif args.command == "collect":
            result = collect_write_patch(state_path=args.state, node_id=args.node)
        elif args.command == "integrate":
            result = integrate_write_pilot(state_path=args.state)
        elif args.command == "reconcile":
            result = reconcile_code_remediate_integration(state_path=args.state, record_path=args.record)
        elif args.command == "apply-source":
            result = apply_code_remediate_source(state_path=args.state)
        else:
            result = cleanup_code_remediate_pilot(state_path=args.state)
    except PilotError as error:
        LOGGER.error("Code-remediate lifecycle failed: %s", error)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
