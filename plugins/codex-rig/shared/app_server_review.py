"""Run and validate a deliberately narrow, opt-in App Server review route.

## Purpose

Start independent, ephemeral read-only App Server threads only after a parent has frozen a small code-review plan, then
retain a bounded evidence record that binds role cards, contexts, and final responses. The route exists because the
native launcher cannot presently attest the mandatory reviewer controls; it is not a general agent runner, write
adapter, scheduler, or provenance replacement.

## Scope

This module reads schema-version-one historical plans for direct evidence inspection and requires schema-version-two
plans for new dispatch to one to four canonical Terra or Luna roles. It validates all local inputs before process
launch, including complete source/diff inclusion and bounded operator capacity evidence, discovers configured MCP server
identifiers without retaining configuration content, restarts with every simple identifier disabled, and rejects any
control, event, output, path, hash, or cleanup deviation. It never changes global configuration, home directories,
credentials, plugin state, or a parent result artifact.

## Usage

An explicitly authorized operator runs ``python app_server_review.py --plan frozen-plan.json --out new-output-
directory``. Unit tests exercise the pure ``validate_evidence`` boundary and mocked protocol helpers; ordinary test
execution must never invoke a model or network operation.

## Outputs

A completed run writes immutable per-role response files and ``evidence.json`` below a new output directory contained by
the resolved plan directory. Once that directory is available, failed runs attempt a bounded failure record after
cleanup and retain completed responses. Input/output setup failures may leave no artifact; a reported failure never
proves that evidence was written. ``validate_evidence`` returns a compact, parent-consumable runtime summary; it
deliberately does not claim native child lineage, global credential isolation, or write parallel eligibility.

## Failure

Malformed, oversized, secret-bearing, mismatched, noncanonical, or unsafe input raises ``ReviewRouteError`` before model
work. During a live run, approval requests, non-read-only observed controls, unknown execution events, failed turns,
duplicate finals, timeout, and unproven cleanup fail closed. Final evidence-validation errors also record failed status
after cleanup when the output directory remains writable. Raw configuration, stderr, reasoning, tool payloads, and
credentials are neither logged nor included in evidence.

## Used by

The opt-in code-review integration freezes the plan and validates the resulting evidence before it can treat reviewers
as independently executed. Rig runtime maintainers own this small route and may remove it once native host controls and
provenance are sufficient.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Mapping, TextIO


SCHEMA_VERSION = 2
LEGACY_PLAN_SCHEMA_VERSION = 1
EVIDENCE_SCHEMA_VERSION = 1
MAX_FILE_BYTES = 256 * 1024
MAX_CONTEXT_BYTES = 2 * 1024 * 1024
# The host limits aggregate turn text by Unicode characters, independently of bytes/tokens.
MAX_TURN_INPUT_CHARACTERS = 1024 * 1024
HISTORY_REVIEW_PROMPT = (
    "Review the complete frozen context in the preceding user message. Follow its role and output contract."
)
# JSON escaping can expand a one-byte control character to six ASCII bytes. The
# frame bound therefore covers any accepted context plus its small RPC envelope.
MAX_EVENT_ENVELOPE_BYTES = 16 * 1024
MAX_EVENT_BYTES = MAX_CONTEXT_BYTES * 6 + MAX_EVENT_ENVELOPE_BYTES
MAX_EVENTS = 512
# Preserve the former per-buffer ceiling while accepting larger individual contexts.
MAX_BUFFERED_EVENT_BYTES = MAX_EVENTS * (MAX_FILE_BYTES * 6 + MAX_EVENT_ENVELOPE_BYTES)
MAX_BUFFERED_EVENTS = MAX_BUFFERED_EVENT_BYTES // MAX_EVENT_BYTES
MAX_OUTPUT_BYTES = 128 * 1024
ROLE_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]+\Z")
RUN_IDENTIFIER = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")
RPC_METHOD_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9]*/[A-Za-z][A-Za-z0-9/]{0,95}\Z")
HEX_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bgh[opsu]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
)
DISABLED_CAPABILITIES = frozenset(
    {
        "hooks",
        "plugins",
        "apps",
        "remote_plugin",
        "browser",
        "computer",
        "image_generation",
        "tool_suggestions",
        "mcp_skill_installs",
        "shell_snapshots",
        "nested_agents",
        "notifications",
        "live_web",
    }
)
ALLOWED_ITEM_TYPES = frozenset({"reasoning", "agentMessage", "commandExecution", "plan"})
HARMLESS_LIFECYCLE_METHODS = frozenset(
    {
        "thread/started",
        "turn/started",
        "thread/status/changed",
        "thread/tokenUsage/updated",
        "mcpServer/startupStatus/updated",
        "account/rateLimits/updated",
    }
)
HARMLESS_TEXT_NOTIFICATION_FIELDS = {"warning": "message", "configWarning": "summary"}
ALLOWED_STREAM_METHODS = frozenset(
    {
        "item/agentMessage/delta",
        "item/reasoning/textDelta",
        "item/reasoning/summaryTextDelta",
        "item/reasoning/summaryPartAdded",
        "item/commandExecution/outputDelta",
    }
)
PLAN_NOTIFICATION_METHODS = frozenset({"turn/plan/updated", "item/plan/delta"})
PLAN_STEP_STATUSES = frozenset({"pending", "inProgress", "completed"})
REMOTE_CONTROL_STATUS_CHANGED = "remoteControl/status/changed"


class ReviewRouteError(RuntimeError):
    """Describe evidence or protocol data that cannot prove the review boundary."""

    def __init__(self, code: str, *, diagnostic: dict[str, object] | None = None) -> None:
        """Retain a static failure code and optional allowlisted diagnostic, never a raw RPC error."""
        super().__init__(code)
        self.diagnostic = diagnostic


@dataclass(slots=True, kw_only=True)
class _ActiveReview:
    """Track the fixed lifecycle state for one active reviewer thread."""

    node: dict[str, object]
    thread_id: str
    controls: dict[str, object]
    turn_id: str | None = None
    started_at_ms: int | None = None
    finished_at_ms: int | None = None
    final: str | None = None
    input_echo_item_id: str | None = None
    input_echo_completed: bool = False
    output_path: Path | None = None
    context_delivery: dict[str, object] | None = None


def _sha256_bytes(data: bytes) -> str:
    """Return the lowercase SHA-256 digest for immutable local bytes."""
    return hashlib.sha256(data).hexdigest()


def _read_bytes(path: Path, label: str, limit: int = MAX_FILE_BYTES) -> bytes:
    """Read one bounded regular file without exposing its content in an error."""
    try:
        if not path.is_file():
            raise ReviewRouteError(f"{label}-not-file")
        data = path.read_bytes()
    except OSError as error:
        raise ReviewRouteError(f"{label}-unreadable") from error
    if len(data) > limit:
        raise ReviewRouteError(f"{label}-too-large")
    return data


def _json_object(path: Path, label: str) -> dict[str, object]:
    """Decode one bounded UTF-8 JSON object from a local file."""
    try:
        value = json.loads(_read_bytes(path, label).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReviewRouteError(f"{label}-invalid-json") from error
    if not isinstance(value, dict):
        raise ReviewRouteError(f"{label}-not-object")
    return value


def _mapping(value: object, label: str) -> Mapping[str, object]:
    """Require one JSON object-shaped protocol value."""
    if not isinstance(value, Mapping):
        raise ReviewRouteError(f"{label}-not-object")
    return value


def _text(value: object, label: str) -> str:
    """Require one nonempty text field with no surrounding whitespace change."""
    if not isinstance(value, str) or not value:
        raise ReviewRouteError(f"{label}-invalid")
    return value


def _cleanup_method_label(message: Mapping[str, object]) -> str:
    """Return a bounded protocol method label without retaining any frame payload."""
    method = message.get("method")
    if isinstance(method, str) and RPC_METHOD_IDENTIFIER.fullmatch(method):
        return method
    return "invalid"


def _json_frame(payload: Mapping[str, object]) -> str:
    """Serialize one compact JSON-RPC frame using the exact transport encoding."""
    return json.dumps(payload, separators=(",", ":")) + "\n"


def _is_harmless_lifecycle(message: Mapping[str, object]) -> bool:
    """Accept only id-less static lifecycle frames or schema-defined disabled remote control."""
    if "id" in message:
        return False
    method = message.get("method")
    if method in HARMLESS_LIFECYCLE_METHODS:
        return True
    if method in HARMLESS_TEXT_NOTIFICATION_FIELDS:
        params = message.get("params")
        return isinstance(params, Mapping) and isinstance(params.get(HARMLESS_TEXT_NOTIFICATION_FIELDS[method]), str)
    if method != REMOTE_CONTROL_STATUS_CHANGED:
        return False
    params = message.get("params")
    return isinstance(params, Mapping) and params.get("status") == "disabled"


def _validate_control_notification(message: Mapping[str, object]) -> None:
    """Reject every remote-control state except the schema-defined disabled notification."""
    if message.get("method") == REMOTE_CONTROL_STATUS_CHANGED and not _is_harmless_lifecycle(message):
        raise ReviewRouteError("app-server-remote-control-status-invalid")


def _validate_plan_notification(method: object, params: Mapping[str, object]) -> None:
    """Require the documented shape of a planning notification without retaining its text."""
    if method == "turn/plan/updated":
        plan = params.get("plan")
        if not isinstance(plan, list) or not all(
            isinstance(step, Mapping) and isinstance(step.get("step"), str) and step.get("status") in PLAN_STEP_STATUSES
            for step in plan
        ):
            raise ReviewRouteError("app-server-plan-notification-invalid")
        explanation = params.get("explanation")
        if explanation is not None and not isinstance(explanation, str):
            raise ReviewRouteError("app-server-plan-notification-invalid")
        return
    if method == "item/plan/delta" and all(isinstance(params.get(field), str) for field in ("itemId", "delta")):
        return
    raise ReviewRouteError("app-server-plan-notification-invalid")


def _digest(value: object, label: str) -> str:
    """Require one canonical SHA-256 hexadecimal digest."""
    text = _text(value, label)
    if not HEX_DIGEST.fullmatch(text):
        raise ReviewRouteError(f"{label}-invalid")
    return text


def _safe_relative(value: object, root: Path, label: str) -> Path:
    """Resolve one relative file path while rejecting traversal and symlink escape."""
    text = _text(value, label)
    candidate = Path(text)
    if (
        candidate.is_absolute()
        or PurePosixPath(text).is_absolute()
        or PureWindowsPath(text).is_absolute()
        or ".." in candidate.parts
    ):
        raise ReviewRouteError(f"{label}-not-relative")
    try:
        resolved_root = root.resolve(strict=True)
        resolved = (resolved_root / candidate).resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (OSError, ValueError) as error:
        raise ReviewRouteError(f"{label}-outside-root") from error
    return resolved


def _contains_secret(data: bytes) -> bool:
    """Return whether bounded UTF-8 text resembles a credential or private key."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return any(pattern.search(text) is not None for pattern in SECRET_PATTERNS)


def _role_settings(role_bytes: bytes) -> tuple[str, str]:
    """Read the canonical model and effort from already-frozen ROLE.md bytes."""
    try:
        text = role_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ReviewRouteError("role-card-invalid-utf8") from error
    model = re.search(r"^model:\s*(\S+)\s*$", text, re.MULTILINE)
    effort = re.search(r"^model_reasoning_effort:\s*(\S+)\s*$", text, re.MULTILINE)
    if model is None or effort is None:
        raise ReviewRouteError("role-card-settings-missing")
    return model.group(1), effort.group(1)


def _source_snapshot(source_bytes: bytes) -> None:
    """Require complete explicit-file scope records from the local source collector."""
    try:
        snapshot = json.loads(source_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReviewRouteError("plan-source-snapshot-invalid") from error
    if not isinstance(snapshot, dict) or set(snapshot) != {
        "schema_version",
        "repository",
        "scope_paths",
        "revision",
        "index_sha256",
        "files",
    }:
        raise ReviewRouteError("plan-source-snapshot-invalid")
    repository = snapshot["repository"]
    scopes = snapshot["scope_paths"]
    revision = snapshot["revision"]
    index_sha256 = snapshot["index_sha256"]
    files = snapshot["files"]
    if (
        snapshot["schema_version"] != 1
        or not isinstance(repository, str)
        or not repository
        or not (PurePosixPath(repository).is_absolute() or PureWindowsPath(repository).is_absolute())
        or not isinstance(scopes, list)
        or not scopes
        or any(not isinstance(scope, str) or not scope for scope in scopes)
        or any(
            PurePosixPath(scope).is_absolute()
            or PureWindowsPath(scope).is_absolute()
            or ".." in PurePosixPath(scope).parts
            for scope in scopes
        )
        or scopes != sorted(set(scopes))
        or not isinstance(revision, str)
        or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision) is None
        or not isinstance(index_sha256, str)
        or HEX_DIGEST.fullmatch(index_sha256) is None
        or not isinstance(files, list)
    ):
        raise ReviewRouteError("plan-source-snapshot-invalid")
    paths: list[str] = []
    for record in files:
        if not isinstance(record, dict) or set(record) != {
            "path",
            "kind",
            "sha256",
            "executable",
            "encoding",
            "content",
        }:
            raise ReviewRouteError("plan-source-snapshot-invalid")
        path = record["path"]
        kind = record["kind"]
        digest = record["sha256"]
        executable = record["executable"]
        encoding = record["encoding"]
        content = record["content"]
        if (
            not isinstance(path, str)
            or not path
            or PurePosixPath(path).is_absolute()
            or PureWindowsPath(path).is_absolute()
            or ".." in PurePosixPath(path).parts
            or kind not in {"file", "symlink", "missing"}
            or not isinstance(executable, bool)
            or encoding not in {"utf-8", "base64"}
            or not isinstance(content, str)
            or (kind == "missing" and (digest is not None or executable or encoding != "utf-8" or content))
            or (kind != "missing" and (not isinstance(digest, str) or HEX_DIGEST.fullmatch(digest) is None))
        ):
            raise ReviewRouteError("plan-source-snapshot-invalid")
        if kind != "missing":
            try:
                content_bytes = (
                    content.encode("utf-8") if encoding == "utf-8" else base64.b64decode(content, validate=True)
                )
            except (UnicodeEncodeError, ValueError) as error:
                raise ReviewRouteError("plan-source-snapshot-invalid") from error
            if _sha256_bytes(content_bytes) != digest:
                raise ReviewRouteError("plan-source-snapshot-invalid")
        paths.append(path)
    if paths != sorted(set(paths)):
        raise ReviewRouteError("plan-source-snapshot-invalid")
    # Explicit leaf scopes make omitted and substituted files detectable without
    # consulting a mutable checkout when validating historical review evidence.
    if not paths or paths != scopes:
        raise ReviewRouteError("plan-source-scope-coverage-invalid")


def _capacity_evidence(capacity_bytes: bytes, model: object) -> dict[str, object]:
    """Require one bounded operator record of observed capacity for its exact reviewer model."""
    try:
        evidence = json.loads(capacity_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReviewRouteError("plan-capacity-evidence-invalid") from error
    expected = {
        "schema_version",
        "model",
        "observed_supported_capacity_tokens",
        "observed_default_context_window",
    }
    if not isinstance(evidence, dict) or set(evidence) != expected or evidence.get("schema_version") != 1:
        raise ReviewRouteError("plan-capacity-evidence-invalid")
    supported = evidence.get("observed_supported_capacity_tokens")
    default_window = evidence.get("observed_default_context_window")
    if (
        evidence.get("model") != model
        or not isinstance(supported, int)
        or isinstance(supported, bool)
        or not isinstance(default_window, int)
        or isinstance(default_window, bool)
        or supported <= 0
        or not 0 < default_window <= supported
    ):
        raise ReviewRouteError("plan-capacity-evidence-invalid")
    return evidence


def _validated_plan(
    plan_path: Path, roles_dir: Path, *, require_dispatch: bool = False
) -> tuple[dict[str, object], list[dict[str, object]]]:
    """Validate frozen source, roles, and recomputed input bounds before resolving nodes."""
    plan = _json_object(plan_path, "plan")
    plan_schema = plan.get("schema_version")
    if plan_schema not in {LEGACY_PLAN_SCHEMA_VERSION, SCHEMA_VERSION}:
        raise ReviewRouteError("plan-schema-version-invalid")
    if require_dispatch and plan_schema != SCHEMA_VERSION:
        raise ReviewRouteError("plan-legacy-dispatch-forbidden")
    if plan.get("consumer_id") != "code-review" or plan.get("task_sensitivity") != "non-sensitive":
        raise ReviewRouteError("plan-consumer-or-sensitivity-invalid")
    for key in ("review_run_id", "parent_thread_id"):
        if not RUN_IDENTIFIER.fullmatch(_text(plan.get(key), f"plan-{key}")):
            raise ReviewRouteError(f"plan-{key}-invalid")
    _digest(plan.get("review_input_sha256"), "plan-review-input-sha256")
    cwd_text = _text(plan.get("cwd"), "plan-cwd")
    cwd = Path(cwd_text)
    if not cwd.is_absolute() or not cwd.is_dir():
        raise ReviewRouteError("plan-cwd-invalid")
    raw_nodes = plan.get("nodes")
    if not isinstance(raw_nodes, list) or not 1 <= len(raw_nodes) <= 4:
        raise ReviewRouteError("plan-nodes-count-invalid")
    plan_root = plan_path.parent.resolve()
    source_bytes: bytes | None = None
    diff_bytes: bytes | None = None
    if plan_schema == SCHEMA_VERSION:
        source_path = _safe_relative(plan.get("source_path"), plan_root, "plan-source-path")
        diff_path = _safe_relative(plan.get("diff_path"), plan_root, "plan-diff-path")
        source_bytes = _read_bytes(source_path, "plan-source", MAX_CONTEXT_BYTES)
        diff_bytes = _read_bytes(diff_path, "plan-diff", MAX_CONTEXT_BYTES)
        if _digest(plan.get("source_sha256"), "plan-source-sha256") != _sha256_bytes(source_bytes):
            raise ReviewRouteError("plan-source-sha256-mismatch")
        if _digest(plan.get("diff_sha256"), "plan-diff-sha256") != _sha256_bytes(diff_bytes):
            raise ReviewRouteError("plan-diff-sha256-mismatch")
        if plan["diff_sha256"] != plan["review_input_sha256"]:
            raise ReviewRouteError("plan-diff-review-input-mismatch")
        _source_snapshot(source_bytes)
        plan["_source_file"] = source_path
        plan["_source_bytes"] = source_bytes
        plan["_diff_file"] = diff_path
        plan["_diff_bytes"] = diff_bytes
    resolved_nodes: list[dict[str, object]] = []
    role_ids: set[str] = set()
    for raw_node in raw_nodes:
        node = dict(_mapping(raw_node, "plan-node"))
        role_id = _text(node.get("role_id"), "plan-role-id")
        if not ROLE_IDENTIFIER.fullmatch(role_id) or role_id in role_ids:
            raise ReviewRouteError("plan-role-id-invalid-or-duplicate")
        role_ids.add(role_id)
        role_path = roles_dir / role_id / "ROLE.md"
        role_bytes = _read_bytes(role_path, "role-card")
        model, effort = _role_settings(role_bytes)
        if model not in {"gpt-5.6-terra", "gpt-5.6-luna"}:
            raise ReviewRouteError("plan-role-model-unsupported")
        if node.get("model") != model or node.get("reasoning_effort") != effort:
            raise ReviewRouteError("plan-role-settings-mismatch")
        if _digest(node.get("role_card_sha256"), "plan-role-card-sha256") != _sha256_bytes(role_bytes):
            raise ReviewRouteError("plan-role-card-sha256-mismatch")
        context_path = _safe_relative(node.get("context_path"), plan_root, "plan-context-path")
        context = _read_bytes(context_path, "plan-context", MAX_CONTEXT_BYTES)
        try:
            context_text = context.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ReviewRouteError("plan-context-invalid-utf8") from error
        if _contains_secret(context) or not context.startswith(role_bytes):
            raise ReviewRouteError("plan-context-secret-or-role-prefix-invalid")
        if _digest(node.get("context_sha256"), "plan-context-sha256") != _sha256_bytes(context):
            raise ReviewRouteError("plan-context-sha256-mismatch")
        if source_bytes is not None and (source_bytes not in context or diff_bytes not in context):
            raise ReviewRouteError("plan-context-source-or-diff-incomplete")
        if "model_context_window" in node and (
            not isinstance(node["model_context_window"], int)
            or isinstance(node["model_context_window"], bool)
            or node["model_context_window"] <= 0
        ):
            raise ReviewRouteError("plan-model-context-window-invalid")
        if source_bytes is not None:
            receipt = _mapping(node.get("capacity_receipt"), "plan-capacity-receipt")
            receipt_fields = {
                "context_sha256",
                "model",
                "tokenizer",
                "input_tokens",
                "instruction_reserve_tokens",
                "output_reserve_tokens",
                "supported_capacity_tokens",
                "default_context_window",
                "effective_window_percent",
                "source_path",
                "source_sha256",
                "capacity_evidence_path",
                "capacity_evidence_sha256",
            }
            if set(receipt) != receipt_fields:
                raise ReviewRouteError("plan-capacity-receipt-fields-invalid")
            if (
                receipt.get("context_sha256") != node["context_sha256"]
                or receipt.get("model") != node["model"]
                or not isinstance(receipt.get("tokenizer"), str)
                or not receipt["tokenizer"]
                or receipt.get("source_path") != plan["source_path"]
                or receipt.get("source_sha256") != plan["source_sha256"]
            ):
                raise ReviewRouteError("plan-capacity-receipt-binding-invalid")
            capacity_path = _safe_relative(
                receipt.get("capacity_evidence_path"), plan_root, "plan-capacity-evidence-path"
            )
            capacity_bytes = _read_bytes(capacity_path, "plan-capacity-evidence", MAX_FILE_BYTES)
            if _digest(receipt.get("capacity_evidence_sha256"), "plan-capacity-evidence-sha256") != _sha256_bytes(
                capacity_bytes
            ):
                raise ReviewRouteError("plan-capacity-evidence-sha256-mismatch")
            capacity = _capacity_evidence(capacity_bytes, node["model"])
            numeric = (
                "input_tokens",
                "instruction_reserve_tokens",
                "output_reserve_tokens",
                "supported_capacity_tokens",
                "default_context_window",
                "effective_window_percent",
            )
            if any(not isinstance(receipt[key], int) or isinstance(receipt[key], bool) for key in numeric):
                raise ReviewRouteError("plan-capacity-receipt-types-invalid")
            if (
                receipt["input_tokens"] <= 0
                or receipt["instruction_reserve_tokens"] <= 0
                or receipt["output_reserve_tokens"] <= 0
                or receipt["supported_capacity_tokens"] <= 0
                or not 0 < receipt["default_context_window"] <= receipt["supported_capacity_tokens"]
                or not 0 < receipt["effective_window_percent"] <= 100
            ):
                raise ReviewRouteError("plan-capacity-receipt-values-invalid")
            # Bound byte-level text tokenization without trusting an operator's proxy count
            # or adding a tokenizer/download dependency. Message overhead stays reserved.
            if receipt["tokenizer"] != "utf8-byte-upper-bound" or receipt["input_tokens"] != len(context):
                raise ReviewRouteError("plan-capacity-input-measurement-invalid")
            if (
                receipt["supported_capacity_tokens"] != capacity["observed_supported_capacity_tokens"]
                or receipt["default_context_window"] != capacity["observed_default_context_window"]
            ):
                raise ReviewRouteError("plan-capacity-receipt-evidence-mismatch")
            selected_window = node.get("model_context_window", receipt["default_context_window"])
            if selected_window > receipt["supported_capacity_tokens"]:
                raise ReviewRouteError("plan-capacity-admission-insufficient")
            effective_capacity = selected_window * receipt["effective_window_percent"] // 100
            if (
                receipt["input_tokens"] + receipt["instruction_reserve_tokens"] + receipt["output_reserve_tokens"]
                > effective_capacity
            ):
                raise ReviewRouteError("plan-capacity-admission-insufficient")
            node["_capacity_evidence_file"] = capacity_path
            node["_capacity_evidence_bytes"] = capacity_bytes
        node["_context_file"] = context_path
        node["_context_bytes"] = context
        node["_context_text"] = context_text
        node["_role_file"] = role_path
        node["_role_bytes"] = role_bytes
        resolved_nodes.append(node)
    return plan, resolved_nodes


def _source_materials_unchanged(plan: Mapping[str, object], nodes: list[dict[str, object]], phase: str) -> None:
    """Reject source, diff, or any reviewer's capacity evidence drift before dispatch or acceptance."""
    if plan.get("schema_version") != SCHEMA_VERSION:
        return
    source_file = plan.get("_source_file")
    source_bytes = plan.get("_source_bytes")
    diff_file = plan.get("_diff_file")
    diff_bytes = plan.get("_diff_bytes")
    if not isinstance(source_file, Path) or not isinstance(source_bytes, bytes):
        raise ReviewRouteError("plan-source-binding-invalid")
    if not isinstance(diff_file, Path) or not isinstance(diff_bytes, bytes):
        raise ReviewRouteError("plan-diff-binding-invalid")
    if (
        _read_bytes(source_file, "plan-source", MAX_CONTEXT_BYTES) != source_bytes
        or _read_bytes(diff_file, "plan-diff", MAX_CONTEXT_BYTES) != diff_bytes
    ):
        raise ReviewRouteError(f"source-or-diff-mutated-{phase}")
    # Check every reviewer before the first paid turn, including later siblings' capacity records.
    for node in nodes:
        capacity_file = node.get("_capacity_evidence_file")
        capacity_bytes = node.get("_capacity_evidence_bytes")
        if not isinstance(capacity_file, Path) or not isinstance(capacity_bytes, bytes):
            raise ReviewRouteError("plan-capacity-binding-invalid")
        if _read_bytes(capacity_file, "plan-capacity-evidence", MAX_FILE_BYTES) != capacity_bytes:
            raise ReviewRouteError(f"capacity-evidence-mutated-{phase}")


def _validate_capabilities(value: object) -> dict[str, bool]:
    """Require a boolean-only projection that proves all unsafe features disabled."""
    capabilities = dict(_mapping(value, "evidence-capabilities"))
    if set(capabilities) != DISABLED_CAPABILITIES or any(not isinstance(item, bool) for item in capabilities.values()):
        raise ReviewRouteError("evidence-capabilities-invalid")
    if any(capabilities.values()):
        raise ReviewRouteError("evidence-capability-enabled")
    return capabilities  # type: ignore[return-value]


def _controls(value: object, node: Mapping[str, object]) -> dict[str, object]:
    """Verify the exact observed App Server controls for one independent thread."""
    controls = dict(_mapping(value, "evidence-observed-controls"))
    expected = {
        "sandbox": {"type": "readOnly", "networkAccess": False},
        "approval_policy": "never",
        "model": node["model"],
        "reasoning_effort": node["reasoning_effort"],
    }
    if controls != expected:
        raise ReviewRouteError("evidence-observed-controls-mismatch")
    return controls


def validate_evidence(
    plan_path: Path, evidence_path: Path, roles_dir: Path, *, require_dispatch: bool = False
) -> dict[str, object]:
    """Bind completed App Server evidence to its frozen plan and installed role cards.

    The returned summary is intentionally conservative: it never claims native lineage, credential isolation, or write
    eligibility. ``parallel`` is returned only when the adapter retained overlapping substantive node intervals.
    """
    plan, plan_nodes = _validated_plan(plan_path, roles_dir, require_dispatch=require_dispatch)
    evidence = _json_object(evidence_path, "evidence")
    if evidence.get("schema_version") != EVIDENCE_SCHEMA_VERSION or evidence.get("route") != "app-server":
        raise ReviewRouteError("evidence-schema-or-route-invalid")
    if evidence.get("plan_sha256") != _sha256_bytes(_read_bytes(plan_path, "plan")):
        raise ReviewRouteError("evidence-plan-sha256-mismatch")
    for key in ("consumer_id", "review_run_id", "parent_thread_id", "review_input_sha256"):
        if evidence.get(key) != plan.get(key):
            raise ReviewRouteError(f"evidence-{key}-mismatch")
    if not isinstance(evidence.get("cli_version"), str) or not evidence["cli_version"]:
        raise ReviewRouteError("evidence-cli-version-invalid")
    if evidence.get("status") != "completed" or evidence.get("cleanup") != "completed":
        raise ReviewRouteError("evidence-status-or-cleanup-invalid")
    _validate_capabilities(evidence.get("capabilities"))
    raw_nodes = evidence.get("nodes")
    if not isinstance(raw_nodes, list) or len(raw_nodes) != len(plan_nodes):
        raise ReviewRouteError("evidence-nodes-count-invalid")
    evidence_root = evidence_path.parent.resolve()
    plan_by_role = {str(node["role_id"]): node for node in plan_nodes}
    seen_roles: set[str] = set()
    output_aliases: set[str] = set()
    thread_ids: set[str] = set()
    turn_ids: set[str] = set()
    intervals: list[tuple[int, int]] = []
    for raw_node in raw_nodes:
        node = _mapping(raw_node, "evidence-node")
        role_id = _text(node.get("role_id"), "evidence-role-id")
        if role_id in seen_roles or role_id not in plan_by_role:
            raise ReviewRouteError("evidence-role-id-invalid-or-duplicate")
        seen_roles.add(role_id)
        plan_node = plan_by_role[role_id]
        for key in ("role_card_sha256", "context_path", "context_sha256"):
            if node.get(key) != plan_node.get(key):
                raise ReviewRouteError(f"evidence-{key}-mismatch")
        expected_delivery = None
        if len(str(plan_node["_context_text"])) > MAX_TURN_INPUT_CHARACTERS:
            expected_delivery = {
                "method": "thread/inject_items",
                "context_sha256": plan_node["context_sha256"],
                "acknowledged": True,
            }
        delivery = node.get("context_delivery")
        if delivery != expected_delivery or (
            expected_delivery is not None and isinstance(delivery, Mapping) and delivery.get("acknowledged") is not True
        ):
            raise ReviewRouteError("evidence-context-delivery-mismatch")
        output_value = _text(node.get("output_path"), "evidence-output-path")
        output_alias = PureWindowsPath(output_value).as_posix().casefold()
        if output_alias in output_aliases:
            raise ReviewRouteError("evidence-output-path-duplicate")
        output_aliases.add(output_alias)
        output_path = _safe_relative(output_value, evidence_root, "evidence-output-path")
        output = _read_bytes(output_path, "evidence-output", MAX_OUTPUT_BYTES)
        try:
            output_text = output.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ReviewRouteError("evidence-output-invalid-utf8") from error
        if not output_text.strip() or _contains_secret(output):
            raise ReviewRouteError("evidence-output-empty-or-secret")
        if _digest(node.get("output_sha256"), "evidence-output-sha256") != _sha256_bytes(output):
            raise ReviewRouteError("evidence-output-sha256-mismatch")
        thread_id = _text(node.get("thread_id"), "evidence-thread-id")
        turn_id = _text(node.get("turn_id"), "evidence-turn-id")
        if thread_id in thread_ids or turn_id in turn_ids:
            raise ReviewRouteError("evidence-thread-or-turn-id-duplicate")
        thread_ids.add(thread_id)
        turn_ids.add(turn_id)
        _controls(node.get("observed_controls"), plan_node)
        if node.get("terminal_status") != "completed":
            raise ReviewRouteError("evidence-terminal-status-invalid")
        started = node.get("started_at_ms")
        finished = node.get("finished_at_ms")
        if (
            any(not isinstance(value, int) or isinstance(value, bool) for value in (started, finished))
            or started < 0
            or finished < 0
            or started > finished
        ):
            raise ReviewRouteError("evidence-substantive-interval-invalid")
        intervals.append((started, finished))
    if seen_roles != set(plan_by_role):
        raise ReviewRouteError("evidence-role-set-mismatch")
    overlaps = any(
        left[0] < left[1] and right[0] < right[1] and left[0] < right[1] and right[0] < left[1]
        for index, left in enumerate(intervals)
        for right in intervals[index + 1 :]
    )
    return {
        "actual_mode": "parallel" if overlaps else "independent-spawned",
        "evidence_level": "app-server-parent-observed",
        "write_parallel_eligible": False,
        "filesystem_credential_isolation": "unverified",
        "approval_policy": "never",
        "consumer_id": "code-review",
    }


class _JsonRpcStdio:
    """Exchange bounded JSON-RPC frames without retaining server stderr or raw logs."""

    def __init__(self, process: subprocess.Popen[str], deadline: float) -> None:
        """Attach a single bounded reader to an App Server stdio process."""
        if process.stdin is None or process.stdout is None:
            raise ReviewRouteError("app-server-stdio-unavailable")
        self._process = process
        self._stdin: TextIO = process.stdin
        self._deadline = deadline
        self._next_id = 1
        self._pending: list[Mapping[str, object]] = []
        self._messages: queue.Queue[str | None] = queue.Queue(maxsize=MAX_BUFFERED_EVENTS)
        self._reader_error: str | None = None
        self._reader = threading.Thread(target=self._read_lines, args=(process.stdout,), daemon=True)
        self._reader.start()

    def _read_lines(self, stdout: TextIO) -> None:
        """Forward bounded frames without copying them to process output or evidence."""
        try:
            while line := stdout.readline(MAX_EVENT_BYTES + 1):
                if len(line.encode("utf-8")) > MAX_EVENT_BYTES:
                    self._reader_error = "app-server-frame-too-large"
                    return
                self._messages.put_nowait(line)
        except queue.Full:
            self._reader_error = "app-server-frame-buffer-full"
        finally:
            try:
                self._messages.put_nowait(None)
            except queue.Full:
                self._reader_error = "app-server-frame-buffer-full"

    def _read(self) -> Mapping[str, object]:
        """Return one decoded frame before the shared bounded deadline."""
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise ReviewRouteError("app-server-timeout")
        try:
            line = self._messages.get(timeout=remaining)
        except queue.Empty as error:
            raise ReviewRouteError("app-server-timeout") from error
        if line is None:
            raise ReviewRouteError(self._reader_error or "app-server-stdio-closed")
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise ReviewRouteError("app-server-json-invalid") from error
        return _mapping(value, "app-server-message")

    def send(self, payload: Mapping[str, object]) -> None:
        """Write one compact JSON-RPC frame and fail closed on a broken pipe."""
        frame = _json_frame(payload)
        if len(frame.encode("utf-8")) > MAX_EVENT_BYTES:
            raise ReviewRouteError("app-server-outbound-frame-too-large")
        try:
            self._stdin.write(frame)
            self._stdin.flush()
        except OSError as error:
            raise ReviewRouteError("app-server-stdin-write-failed") from error

    def request(self, method: str, params: Mapping[str, object]) -> Mapping[str, object]:
        """Send one request and return only its matching result object."""
        request_id = self._next_id
        self._next_id += 1
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)})
        while True:
            message = self._read()
            if "id" in message and "method" in message:
                raise ReviewRouteError("app-server-server-request-rejected")
            _validate_control_notification(message)
            if message.get("id") != request_id:
                if len(self._pending) >= MAX_BUFFERED_EVENTS:
                    raise ReviewRouteError("app-server-pending-events-overflow")
                self._pending.append(message)
                continue
            if "error" in message:
                raise ReviewRouteError(
                    f"app-server-request-failed:{method}", diagnostic=_rpc_diagnostic(method, message["error"])
                )
            return _mapping(message.get("result"), f"app-server-{method}-result")

    def events(self) -> Mapping[str, object]:
        """Return the next queued server event without retaining a transcript."""
        if self._pending:
            message = self._pending.pop(0)
        else:
            message = self._read()
        _validate_control_notification(message)
        return message

    def drain_after_exit(self) -> None:
        """Prove reader EOF after process cleanup and reject unprocessed trailing frames."""
        if self._process.poll() is None:
            raise ReviewRouteError("app-server-reader-cleanup-unproven")
        self._reader.join(timeout=2)
        if self._reader.is_alive() or self._reader_error is not None:
            raise ReviewRouteError("app-server-reader-cleanup-unproven")
        for message in self._pending:
            _validate_control_notification(message)
            if not _is_harmless_lifecycle(message):
                raise ReviewRouteError(f"app-server-events-after-cleanup:{_cleanup_method_label(message)}")
        self._pending.clear()
        eof = False
        while True:
            try:
                line = self._messages.get_nowait()
            except queue.Empty:
                break
            if line is None:
                eof = True
            else:
                try:
                    message = _mapping(json.loads(line), "app-server-post-cleanup-message")
                except json.JSONDecodeError as error:
                    raise ReviewRouteError("app-server-events-after-cleanup") from error
                _validate_control_notification(message)
                if not _is_harmless_lifecycle(message):
                    raise ReviewRouteError(f"app-server-events-after-cleanup:{_cleanup_method_label(message)}")
        if not eof:
            raise ReviewRouteError("app-server-reader-cleanup-unproven")


def _rpc_diagnostic(method: str, error: object) -> dict[str, object]:
    """Classify known RPC failures without retaining messages, data, arbitrary codes or method names."""
    codes = {
        -32700: "parse-error",
        -32600: "invalid-request",
        -32601: "method-not-supported",
        -32602: "invalid-parameters",
        -32603: "internal-error",
    }
    value = error if isinstance(error, Mapping) else {}
    code = value.get("code")
    code = code if type(code) is int and code in codes else None
    reason = codes.get(code, "unclassified")
    message = value.get("message")
    if (
        code == -32602
        and isinstance(message, str)
        and re.search(r"\bmaximum length of 1048576 characters\b", message, re.IGNORECASE)
    ):
        reason = "input-character-limit"
    methods = {"initialize", "config/read", "thread/start", "thread/inject_items", "turn/start"}
    return {
        "stage": "rpc-response",
        "reason": reason,
        "method_category": method if method in methods else "unrecognized",
        "rpc_code": code,
        "recovery": (
            "Inspect host compatibility and the frozen input without a paid retry; preserve complete evidence. "
            "Resume model execution only with a validated repair and separate authorization."
        ),
    }


def _atomic_json(path: Path, value: Mapping[str, object]) -> None:
    """Atomically write newline-normalized JSON inside an already validated output root."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _posix_group_exists(process_id: int) -> bool:
    """Return whether an owned POSIX process group still exists without treating permission as absence."""
    try:
        os.killpg(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError as error:
        raise ReviewRouteError("app-server-process-group-cleanup-unproven") from error
    except OSError as error:
        raise ReviewRouteError("app-server-process-group-cleanup-unproven") from error
    return True


def _wait_for_posix_group_exit(process: subprocess.Popen[str], deadline: float) -> bool:
    """Reap the direct child while polling its owned process group through one bounded grace period."""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return not _posix_group_exists(process.pid)
        try:
            process.wait(timeout=min(remaining, 0.05))
        except subprocess.TimeoutExpired:
            pass
        if not _posix_group_exists(process.pid):
            return True
        time.sleep(min(remaining, 0.05))


def _terminate(process: subprocess.Popen[str]) -> None:
    """Stop the owned process group or Windows tree and prove it is gone after a bounded grace period."""
    if sys.platform == "win32":
        try:
            completed = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
                check=False,
            )
        except OSError as error:
            raise ReviewRouteError("app-server-windows-tree-cleanup-unproven") from error
        if completed.returncode != 0:
            raise ReviewRouteError("app-server-windows-tree-cleanup-unproven")
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired as error:
            raise ReviewRouteError("app-server-windows-tree-cleanup-unproven") from error
        if process.poll() is None:
            raise ReviewRouteError("app-server-windows-tree-cleanup-unproven")
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError as error:
        raise ReviewRouteError("app-server-process-group-cleanup-unproven") from error
    except OSError as error:
        raise ReviewRouteError("app-server-process-group-cleanup-unproven") from error
    if not _wait_for_posix_group_exit(process, time.monotonic() + 2):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except PermissionError as error:
            raise ReviewRouteError("app-server-process-group-cleanup-unproven") from error
        except OSError as error:
            raise ReviewRouteError("app-server-process-group-cleanup-unproven") from error
        if not _wait_for_posix_group_exit(process, time.monotonic() + 2):
            raise ReviewRouteError("app-server-process-group-cleanup-unproven")
    if process.poll() is None:
        raise ReviewRouteError("app-server-process-group-cleanup-unproven")


def _server_command(codex: Path, disabled_servers: list[str]) -> list[str]:
    """Build invocation-only App Server restrictions without changing host configuration."""
    command = [str(codex), "app-server", "--stdio"]
    overrides = (
        'sandbox_mode="read-only"',
        'approval_policy="never"',
        "features.hooks=false",
        "features.plugins=false",
        "features.apps=false",
        "features.remote_plugin=false",
        "features.multi_agent=false",
        "features.shell_snapshot=false",
        "features.browser_use=false",
        "features.browser_use_external=false",
        "features.computer_use=false",
        "features.image_generation=false",
        "features.in_app_browser=false",
        "features.skill_mcp_dependency_install=false",
        "features.tool_suggest=false",
        "allow_login_shell=false",
        "notify=[]",
        'web_search="disabled"',
        "mcp_servers={}",
    )
    for override in (*overrides, *(f"mcp_servers.{name}.enabled=false" for name in disabled_servers)):
        command.extend(("-c", override))
    return command


def _codex_version(codex: Path, cwd: Path) -> str:
    """Return the bounded installed CLI version without retaining diagnostics or configuration."""
    try:
        completed = subprocess.run(
            [str(codex), "--version"],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ReviewRouteError("codex-version-unavailable") from error
    version = re.search(r"\b\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?\b", completed.stdout)
    if completed.returncode != 0 or version is None:
        raise ReviewRouteError("codex-version-unavailable")
    return version.group(0)


def _start_server(command: list[str], cwd: Path, deadline: float) -> tuple[subprocess.Popen[str], _JsonRpcStdio]:
    """Start one local App Server with inherited opaque authentication and no stderr capture."""
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if sys.platform == "win32" else 0
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=sys.platform != "win32",
        creationflags=creationflags,
    )
    try:
        client = _JsonRpcStdio(process, deadline)
        client.request(
            "initialize",
            {"clientInfo": {"name": "codex-rig-review", "version": "1"}, "capabilities": {"experimentalApi": True}},
        )
        client.send({"jsonrpc": "2.0", "method": "initialized"})
    except Exception:
        _terminate(process)
        raise
    return process, client


def _discover_mcp_servers(client: _JsonRpcStdio, cwd: Path) -> list[str]:
    """Return only simple configured MCP identifiers, never raw configuration values."""
    config = _mapping(client.request("config/read", {"cwd": str(cwd), "includeLayers": False}).get("config"), "config")
    servers = config.get("mcp_servers")
    if not isinstance(servers, Mapping):
        raise ReviewRouteError("app-server-mcp-servers-invalid")
    names = list(servers)
    if any(not isinstance(name, str) or ROLE_IDENTIFIER.fullmatch(name) is None for name in names):
        raise ReviewRouteError("app-server-mcp-server-name-unsupported")
    return sorted(names)


def _verify_mcp_disabled(client: _JsonRpcStdio, cwd: Path) -> dict[str, bool]:
    """Verify effective server denials and the required boolean-only capability projection."""
    config = _mapping(client.request("config/read", {"cwd": str(cwd), "includeLayers": False}).get("config"), "config")
    servers = config.get("mcp_servers")
    features = config.get("features")
    if not isinstance(servers, Mapping) or not isinstance(features, Mapping):
        raise ReviewRouteError("app-server-effective-config-invalid")
    if not all(isinstance(server, Mapping) and server.get("enabled") is False for server in servers.values()):
        raise ReviewRouteError("app-server-mcp-disable-unproven")
    observed = {
        "hooks": features.get("hooks") is False,
        "plugins": features.get("plugins") is False,
        "apps": features.get("apps") is False,
        "remote_plugin": features.get("remote_plugin") is False,
        "browser": (
            features.get("browser_use") is False
            and features.get("browser_use_external") is False
            and features.get("in_app_browser") is False
        ),
        "computer": features.get("computer_use") is False,
        "image_generation": features.get("image_generation") is False,
        "tool_suggestions": features.get("tool_suggest") is False,
        "mcp_skill_installs": features.get("skill_mcp_dependency_install") is False,
        "shell_snapshots": features.get("shell_snapshot") is False,
        "nested_agents": features.get("multi_agent") is False,
        "notifications": config.get("notify") == [],
        "live_web": config.get("web_search") == "disabled",
    }
    if not all(observed.values()):
        raise ReviewRouteError("app-server-capability-denial-unproven")
    if config.get("allow_login_shell") is not False:
        raise ReviewRouteError("app-server-login-shell-denial-unproven")
    return {key: False for key in DISABLED_CAPABILITIES}


def _prepared_host(
    codex: Path, cwd: Path, deadline: float
) -> tuple[subprocess.Popen[str], _JsonRpcStdio, dict[str, bool]]:
    """Discover MCP names once, then return the restarted host with effective denials proven."""
    discovery_process, discovery_client = _start_server(_server_command(codex, []), cwd, deadline)
    try:
        server_names = _discover_mcp_servers(discovery_client, cwd)
    finally:
        _terminate(discovery_process)
        discovery_client.drain_after_exit()
    process, client = _start_server(_server_command(codex, server_names), cwd, deadline)
    try:
        capabilities = _verify_mcp_disabled(client, cwd)
    except Exception:
        _terminate(process)
        client.drain_after_exit()
        raise
    return process, client, capabilities


def _thread_controls(result: Mapping[str, object], node: Mapping[str, object]) -> dict[str, object]:
    """Require the exact observed thread control projection before a model turn starts."""
    expected = {
        "sandbox": {"type": "readOnly", "networkAccess": False},
        "approval_policy": "never",
        "model": node["model"],
        "reasoning_effort": node["reasoning_effort"],
    }
    observed = {
        "sandbox": result.get("sandbox"),
        "approval_policy": result.get("approvalPolicy"),
        "model": result.get("model"),
        "reasoning_effort": result.get("reasoningEffort"),
    }
    if observed != expected:
        raise ReviewRouteError("app-server-thread-controls-mismatch")
    return expected


def _thread_config(node: Mapping[str, object]) -> dict[str, object]:
    """Build the frozen per-thread configuration without treating a request as observed capacity."""
    config: dict[str, object] = {"model_reasoning_effort": node["reasoning_effort"]}
    if "model_context_window" in node:
        config["model_context_window"] = node["model_context_window"]
    return config


def _preload_context(client: _JsonRpcStdio, thread_id: str, node: Mapping[str, object]) -> dict[str, object] | None:
    """Load oversized context into the same thread's history before any paid turn and bind its acknowledgement."""
    context = node["_context_text"]
    if not isinstance(context, str):
        raise ReviewRouteError("plan-context-text-invalid")
    if len(context) <= MAX_TURN_INPUT_CHARACTERS:
        return None
    # History loading is a no-model host operation; paths, hashes and shortened text cannot replace the context.
    result = client.request(
        "thread/inject_items",
        {
            "threadId": thread_id,
            "items": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": context}]}],
        },
    )
    if result != {}:
        raise ReviewRouteError("app-server-context-preload-result-invalid")
    return {"method": "thread/inject_items", "context_sha256": node["context_sha256"], "acknowledged": True}


def _turn_input_text(node: Mapping[str, object]) -> str:
    """Select exact direct context or the fixed trigger for previously acknowledged history context."""
    context = node["_context_text"]
    if not isinstance(context, str):
        raise ReviewRouteError("plan-context-text-invalid")
    return HISTORY_REVIEW_PROMPT if len(context) > MAX_TURN_INPUT_CHARACTERS else context


def check_host(plan_path: Path, codex: Path, timeout_seconds: float) -> dict[str, object]:
    """Verify the adapter's no-turn host controls before an operator authorizes model work."""
    if (
        not isinstance(timeout_seconds, (int, float))
        or isinstance(timeout_seconds, bool)
        or not 0 < timeout_seconds <= 900
    ):
        raise ReviewRouteError("timeout-seconds-invalid")
    roles_dir = Path(__file__).resolve().parents[1] / "roles"
    plan, nodes = _validated_plan(plan_path, roles_dir, require_dispatch=True)
    cwd = Path(str(plan["cwd"]))
    cli_version = _codex_version(codex, cwd)
    process: subprocess.Popen[str] | None = None
    client: _JsonRpcStdio | None = None
    primary_error = False
    try:
        process, client, capabilities = _prepared_host(codex, cwd, time.monotonic() + timeout_seconds)
        _source_materials_unchanged(plan, nodes, "before-turn")
        controls: list[dict[str, object]] = []
        thread_ids: list[str] = []
        for node in nodes:
            if (
                _read_bytes(Path(str(node["_context_file"])), "plan-context", MAX_CONTEXT_BYTES)
                != node["_context_bytes"]
            ):
                raise ReviewRouteError("context-mutated-before-turn")
            result = client.request(
                "thread/start",
                {
                    "cwd": plan["cwd"],
                    "model": node["model"],
                    "approvalPolicy": "never",
                    "sandbox": "read-only",
                    "ephemeral": True,
                    "allowProviderModelFallback": False,
                    "config": _thread_config(node),
                },
            )
            controls.append({"role_id": node["role_id"], "observed_controls": _thread_controls(result, node)})
            thread_ids.append(
                _text(_mapping(result.get("thread"), "app-server-thread").get("id"), "app-server-thread-id")
            )
        for node, thread_id, control in zip(nodes, thread_ids, controls):
            delivery = _preload_context(client, thread_id, node)
            if delivery is not None:
                control["context_delivery"] = delivery
        _source_materials_unchanged(plan, nodes, "during-host-check")
        return {"cli_version": cli_version, "capabilities": capabilities, "nodes": controls, "model_invoked": False}
    except Exception:
        primary_error = True
        raise
    finally:
        if process is not None:
            try:
                _terminate(process)
                if client is not None:
                    client.drain_after_exit()
            except Exception:
                if not primary_error:
                    raise


def _review_output_schema(plan: Mapping[str, object]) -> dict[str, Any]:
    """Constrain every review turn to source-bound, machine-readable findings."""
    finding_properties = {
        "signature": {"type": "string"},
        "tier": {"type": "string", "enum": ["security", "critical", "high", "medium", "low", "nit"]},
        "structural": {"type": "boolean"},
        "disposition": {
            "type": "string",
            "enum": ["open", "fixed-pending-verification", "verified-fixed", "rejected"],
        },
        "evidence": {"type": "array", "items": {"type": "string"}},
    }
    return {
        "type": "object",
        "properties": {
            "source_sha256": {"type": "string", "enum": [plan["source_sha256"]]},
            "diff_sha256": {"type": "string", "enum": [plan["review_input_sha256"]]},
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": finding_properties,
                    "required": list(finding_properties),
                    "additionalProperties": False,
                },
            },
        },
        "required": ["source_sha256", "diff_sha256", "findings"],
        "additionalProperties": False,
    }


def _validate_review_output(text: str, plan: Mapping[str, object]) -> None:
    """Reject malformed or unbound results even when the host accepted the schema request."""
    try:
        response = json.loads(text)
    except (ValueError, RecursionError) as error:
        raise ReviewRouteError("app-server-review-output-invalid-json") from error
    if (
        not isinstance(response, dict)
        or set(response) != {"source_sha256", "diff_sha256", "findings"}
        or response["source_sha256"] != plan["source_sha256"]
        or response["diff_sha256"] != plan["review_input_sha256"]
        or not isinstance(response["findings"], list)
    ):
        raise ReviewRouteError("app-server-review-output-schema-mismatch")
    properties = _review_output_schema(plan)["properties"]["findings"]["items"]["properties"]
    signatures: set[str] = set()
    for finding in response["findings"]:
        if (
            not isinstance(finding, dict)
            or set(finding) != set(properties)
            or not isinstance(finding["signature"], str)
            or not finding["signature"].strip()
            or finding["signature"] in signatures
            or finding["tier"] not in properties["tier"]["enum"]
            or type(finding["structural"]) is not bool
            or finding["disposition"] not in properties["disposition"]["enum"]
            or not isinstance(finding["evidence"], list)
            or not finding["evidence"]
            or any(not isinstance(item, str) or not item.strip() for item in finding["evidence"])
        ):
            raise ReviewRouteError("app-server-review-output-finding-invalid")
        signatures.add(finding["signature"])


def _accept_input_echo(method: object, item: Mapping[str, object], state: _ActiveReview) -> None:
    """Validate and advance the exact submitted-input echo lifecycle."""
    node = state.node
    context = _turn_input_text(node)
    content = item.get("content")
    item_id = item.get("id")
    input_matches = (
        isinstance(content, list)
        and len(content) == 1
        and isinstance(content[0], Mapping)
        and set(content[0]) <= {"type", "text", "text_elements"}
        and content[0].get("type") == "text"
        and isinstance(context, str)
        and content[0].get("text") == context
        and content[0].get("text_elements", []) == []
    )
    if not isinstance(item_id, str) or not item_id or not input_matches:
        raise ReviewRouteError("app-server-user-message-lifecycle-invalid")
    if method == "item/started" and state.input_echo_item_id is None:
        state.input_echo_item_id = item_id
        return
    if method == "item/completed" and state.input_echo_item_id == item_id and state.input_echo_completed is False:
        state.input_echo_completed = True
        return
    raise ReviewRouteError("app-server-user-message-lifecycle-invalid")


def run_review(plan_path: Path, output_root: Path, codex: Path, timeout_seconds: float) -> Path:
    """Run one explicitly authorized App Server review wave and write bounded local evidence."""
    if (
        not isinstance(timeout_seconds, (int, float))
        or isinstance(timeout_seconds, bool)
        or not 0 < timeout_seconds <= 900
    ):
        raise ReviewRouteError("timeout-seconds-invalid")
    roles_dir = Path(__file__).resolve().parents[1] / "roles"
    frozen_plan = _read_bytes(plan_path, "plan")
    plan, nodes = _validated_plan(plan_path, roles_dir, require_dispatch=True)
    try:
        plan_parent = plan_path.resolve().parent
        output_root = output_root.resolve(strict=False)
        output_root.relative_to(plan_parent)
    except (OSError, RuntimeError, ValueError) as error:
        raise ReviewRouteError("output-root-outside-plan") from error
    try:
        output_exists = output_root.exists()
    except OSError as error:
        raise ReviewRouteError("output-root-unavailable") from error
    if output_exists:
        raise ReviewRouteError("output-root-must-be-new")
    try:
        output_root.mkdir(parents=True)
    except OSError as error:
        raise ReviewRouteError("output-root-unavailable") from error
    deadline = time.monotonic() + timeout_seconds
    cli_version: str | None = None
    process: subprocess.Popen[str] | None = None
    client: _JsonRpcStdio | None = None
    evidence: dict[str, object] | None = None
    failure: Exception | None = None
    failure_phase = "preparation"
    failure_diagnostic: dict[str, object] | None = None
    turns_attempted = 0
    turns_acknowledged = 0
    cleanup = "not-started"
    try:
        cli_version = _codex_version(codex, Path(str(plan["cwd"])))
        process, client, capabilities = _prepared_host(codex, Path(str(plan["cwd"])), deadline)
        failure_phase = "thread-setup"
        _source_materials_unchanged(plan, nodes, "before-turn")
        for node in nodes:
            context_path = Path(str(node["_context_file"]))
            if _read_bytes(context_path, "plan-context", MAX_CONTEXT_BYTES) != node["_context_bytes"]:
                raise ReviewRouteError("context-mutated-before-turn")
        active: dict[str, _ActiveReview] = {}
        for node in nodes:
            result = client.request(
                "thread/start",
                {
                    "cwd": plan["cwd"],
                    "model": node["model"],
                    "approvalPolicy": "never",
                    "sandbox": "read-only",
                    "ephemeral": True,
                    "allowProviderModelFallback": False,
                    "config": _thread_config(node),
                },
            )
            thread = _mapping(result.get("thread"), "app-server-thread")
            thread_id = _text(thread.get("id"), "app-server-thread-id")
            active[str(node["role_id"])] = _ActiveReview(
                node=node,
                thread_id=thread_id,
                controls=_thread_controls(result, node),
            )
        # Complete every no-model preload first: a later incompatible reviewer must not spend an earlier turn.
        failure_phase = "context-preload"
        for state in active.values():
            state.context_delivery = _preload_context(client, state.thread_id, state.node)
        for role_id, state in active.items():
            node = state.node
            if _read_bytes(plan_path, "plan") != frozen_plan:
                raise ReviewRouteError("plan-mutated-before-turn")
            _source_materials_unchanged(plan, nodes, "before-turn")
            role_path = Path(str(node["_role_file"]))
            role_bytes = node["_role_bytes"]
            if not isinstance(role_bytes, bytes):
                raise ReviewRouteError("role-card-bytes-invalid")
            if _read_bytes(role_path, "role-card") != role_bytes:
                raise ReviewRouteError("role-card-mutated-before-turn")
            context_path = Path(str(node["_context_file"]))
            if _read_bytes(context_path, "plan-context", MAX_CONTEXT_BYTES) != node["_context_bytes"]:
                raise ReviewRouteError("context-mutated-before-turn")
            context = _turn_input_text(node)
            failure_phase = "turn-dispatch"
            turns_attempted += 1
            turn = client.request(
                "turn/start",
                {
                    "threadId": state.thread_id,
                    "cwd": plan["cwd"],
                    "approvalPolicy": "never",
                    "model": node["model"],
                    "effort": node["reasoning_effort"],
                    "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
                    "outputSchema": _review_output_schema(plan),
                    "input": [{"type": "text", "text": context}],
                },
            )
            turns_acknowledged += 1
            turn_data = _mapping(turn.get("turn"), "app-server-turn")
            state.turn_id = _text(turn_data.get("id"), "app-server-turn-id")
        pending = set(active)
        failure_phase = "turn-events"
        while pending:
            message = client.events()
            method = message.get("method")
            if "id" in message:
                raise ReviewRouteError("app-server-server-request-rejected")
            if _is_harmless_lifecycle(message):
                continue
            if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
                raise ReviewRouteError("app-server-approval-requested")
            if method not in {
                "item/started",
                "item/completed",
                "turn/completed",
                *ALLOWED_STREAM_METHODS,
                *PLAN_NOTIFICATION_METHODS,
            }:
                failure_diagnostic = {
                    "stage": "turn-events",
                    "reason": "method-not-allowlisted",
                    "method_category": "unrecognized",
                    "recovery": (
                        "Continue permitted source inspection using native instruction-bounded reviewers or disclosed "
                        "parent-serial review; resume this launcher only after protocol-maintainer triage validates a "
                        "supported event schema."
                    ),
                }
                raise ReviewRouteError("app-server-event-rejected")
            params = _mapping(message.get("params"), "app-server-event-params")
            # Every supplied identity must agree; one matching field cannot mask another turn.
            turn_ids = [params["turnId"]] if "turnId" in params else []
            if "turn" in params:
                turn_ids.append(_mapping(params["turn"], "event-turn").get("id"))
            matching = [
                (role_id, state)
                for role_id, state in active.items()
                if params.get("threadId") == state.thread_id
                and turn_ids
                and state.turn_id is not None
                and all(turn_id == state.turn_id for turn_id in turn_ids)
            ]
            if not matching:
                raise ReviewRouteError("app-server-thread-or-turn-mismatch")
            role_id, state = matching[0]
            if role_id not in pending:
                raise ReviewRouteError("app-server-event-after-turn-completed")
            if method in PLAN_NOTIFICATION_METHODS:
                _validate_plan_notification(method, params)
                continue
            item = params.get("item")
            if method in {"item/started", "item/completed"} and not isinstance(item, Mapping):
                raise ReviewRouteError("app-server-event-item-not-object")
            if isinstance(item, Mapping):
                item_type = item.get("type")
                if item_type == "userMessage":
                    _accept_input_echo(method, item, state)
                    continue
                if item_type == "plan" and (
                    not isinstance(item.get("id"), str) or not isinstance(item.get("text"), str)
                ):
                    raise ReviewRouteError("app-server-plan-item-invalid")
                if item_type not in ALLOWED_ITEM_TYPES:
                    raise ReviewRouteError("app-server-item-type-rejected")
                state.started_at_ms = state.started_at_ms or int(time.monotonic() * 1000)
                if method == "item/completed" and item_type == "agentMessage" and item.get("phase") == "final_answer":
                    final = item.get("text")
                    if (
                        not isinstance(final, str)
                        or not final.strip()
                        or len(final.encode("utf-8")) > MAX_OUTPUT_BYTES
                        or state.final is not None
                    ):
                        raise ReviewRouteError("app-server-final-output-invalid-or-duplicate")
                    if _contains_secret(final.encode("utf-8")):
                        raise ReviewRouteError("app-server-final-output-secret")
                    state.final = final
            if message.get("method") == "turn/completed":
                turn = _mapping(params.get("turn"), "app-server-completed-turn")
                if turn.get("status") != "completed":
                    raise ReviewRouteError("app-server-turn-status-invalid")
                if state.final is None:
                    raise ReviewRouteError("app-server-turn-final-missing")
                if state.input_echo_item_id is not None and state.input_echo_completed is not True:
                    raise ReviewRouteError("app-server-user-message-lifecycle-incomplete")
                state.finished_at_ms = int(time.monotonic() * 1000)
                output = output_root / f"{role_id}.md"
                final = _text(state.final, "app-server-final-output")
                output.write_bytes(final.encode("utf-8"))
                state.output_path = output
                pending.remove(role_id)
                # Preserve the original response before rejecting it; never repair or retry a paid result.
                _validate_review_output(final, plan)
        if _read_bytes(plan_path, "plan") != frozen_plan:
            raise ReviewRouteError("plan-mutated-during-review")
        _source_materials_unchanged(plan, nodes, "during-review")
        for node in nodes:
            role_path = Path(str(node["_role_file"]))
            role_bytes = node["_role_bytes"]
            if not isinstance(role_bytes, bytes):
                raise ReviewRouteError("role-card-bytes-invalid")
            if _read_bytes(role_path, "role-card") != role_bytes:
                raise ReviewRouteError("role-card-mutated-during-review")
            context_path = Path(str(node["_context_file"]))
            if _read_bytes(context_path, "plan-context", MAX_CONTEXT_BYTES) != node["_context_bytes"]:
                raise ReviewRouteError("context-mutated-during-review")
        evidence_nodes: list[dict[str, object]] = []
        if cli_version is None:
            raise ReviewRouteError("codex-version-unavailable")
        for role_id, state in active.items():
            node = state.node
            output = state.output_path
            assert output is not None
            output_name = output.relative_to(output_root).as_posix()
            final = _text(state.final, "app-server-final-output")
            evidence_nodes.append(
                {
                    "role_id": role_id,
                    "role_card_sha256": node["role_card_sha256"],
                    "context_path": node["context_path"],
                    "context_sha256": node["context_sha256"],
                    "output_path": output_name,
                    "output_sha256": _sha256_bytes(final.encode("utf-8")),
                    "thread_id": state.thread_id,
                    "turn_id": state.turn_id,
                    "observed_controls": state.controls,
                    "terminal_status": "completed",
                    "started_at_ms": state.started_at_ms,
                    "finished_at_ms": state.finished_at_ms,
                }
            )
            if state.context_delivery is not None:
                evidence_nodes[-1]["context_delivery"] = state.context_delivery
        evidence = {
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "route": "app-server",
            "plan_sha256": _sha256_bytes(frozen_plan),
            "consumer_id": plan["consumer_id"],
            "review_run_id": plan["review_run_id"],
            "parent_thread_id": plan["parent_thread_id"],
            "review_input_sha256": plan["review_input_sha256"],
            "cli_version": cli_version,
            "status": "completed",
            "cleanup": "completed",
            "capabilities": capabilities,
            "nodes": evidence_nodes,
        }
    except Exception as error:
        failure = error
        if isinstance(error, ReviewRouteError) and error.diagnostic is not None:
            failure_diagnostic = error.diagnostic
    finally:
        if process is not None:
            cleanup = "started"
            try:
                _terminate(process)
                if client is not None:
                    client.drain_after_exit()
                cleanup = "completed"
            except Exception as cleanup_error:
                cleanup = "failed"
                if failure is None:
                    failure = cleanup_error
    evidence_path = output_root / "evidence.json"
    if failure is None:
        failure_phase = "evidence-validation"
        try:
            assert evidence is not None
            _atomic_json(evidence_path, evidence)
            validate_evidence(plan_path, evidence_path, roles_dir, require_dispatch=True)
        except Exception as error:
            # A rejected final artifact must enter the same failure path as rejected events.
            failure = error
    if failure is not None:
        failure_evidence: dict[str, object] = {
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "route": "app-server",
            "status": "failed",
            "failure_code": str(failure).split(":", 1)[0]
            if isinstance(failure, ReviewRouteError)
            else "app-server-review-failed",
            "failure_phase": failure_phase,
            "turn_dispatch": {"attempted": turns_attempted, "acknowledged": turns_acknowledged},
            "cleanup": cleanup,
        }
        if failure_diagnostic is not None:
            failure_evidence["failure_diagnostic"] = failure_diagnostic
        _atomic_json(
            evidence_path,
            failure_evidence,
        )
        if isinstance(failure, ReviewRouteError):
            raise failure
        raise ReviewRouteError("app-server-review-failed") from failure
    return evidence_path


def main() -> int:
    """Parse the explicit operator CLI and run one bounded review route."""
    parser = argparse.ArgumentParser(description="Run an explicitly authorized bounded App Server code review.")
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--codex", type=Path, default=Path("codex"))
    parser.add_argument("--timeout-seconds", type=float, default=300.0)
    parser.add_argument("--check-host", action="store_true")
    args = parser.parse_args()
    try:
        if args.check_host:
            print(json.dumps(check_host(args.plan, args.codex, args.timeout_seconds), sort_keys=True))
        elif args.out is None:
            parser.error("--out is required unless --check-host is used")
        else:
            run_review(args.plan, args.out, args.codex, args.timeout_seconds)
    except ReviewRouteError as error:
        print(str(error), file=sys.stderr)
        if error.diagnostic is not None:
            print(json.dumps({"failure_diagnostic": error.diagnostic}, sort_keys=True), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
