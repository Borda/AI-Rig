"""Run and validate a deliberately narrow, opt-in App Server review route.

## Purpose

Start independent, ephemeral read-only App Server threads only after a parent has frozen a small code-review plan, then
retain a bounded evidence record that binds role cards, contexts, and final responses. The route exists because the
native launcher cannot presently attest the mandatory reviewer controls; it is not a general agent runner, write
adapter, scheduler, or provenance replacement.

## Scope

This module accepts schema-version-one plans for one to four canonical Terra or Luna roles. It validates all local
inputs before process launch, discovers configured MCP server identifiers without retaining configuration content,
restarts with every simple identifier disabled, and rejects any control, event, output, path, hash, or cleanup
deviation. It never changes global configuration, home directories, credentials, plugin state, or a parent result
artifact.

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
duplicate finals, timeout, and unproven cleanup fail closed. Raw configuration, stderr, reasoning, tool payloads, and
credentials are neither logged nor included in evidence.

## Used by

The opt-in code-review integration freezes the plan and validates the resulting evidence before it can treat reviewers
as independently executed. Rig runtime maintainers own this small route and may remove it once native host controls and
provenance are sufficient.
"""

from __future__ import annotations

import argparse
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
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Mapping, TextIO


SCHEMA_VERSION = 1
MAX_FILE_BYTES = 256 * 1024
# JSON escaping can expand a one-byte control character to six ASCII bytes. The
# shared frame bound therefore covers any accepted context plus its small RPC envelope.
MAX_EVENT_BYTES = MAX_FILE_BYTES * 6 + 16 * 1024
MAX_EVENTS = 512
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


def _validated_plan(plan_path: Path, roles_dir: Path) -> tuple[dict[str, object], list[dict[str, object]]]:
    """Validate the frozen plan and return its nodes with resolved local bindings."""
    plan = _json_object(plan_path, "plan")
    if plan.get("schema_version") != SCHEMA_VERSION:
        raise ReviewRouteError("plan-schema-version-invalid")
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
        context = _read_bytes(context_path, "plan-context")
        if _contains_secret(context) or not context.startswith(role_bytes):
            raise ReviewRouteError("plan-context-secret-or-role-prefix-invalid")
        if _digest(node.get("context_sha256"), "plan-context-sha256") != _sha256_bytes(context):
            raise ReviewRouteError("plan-context-sha256-mismatch")
        node["_context_file"] = context_path
        node["_context_bytes"] = context
        node["_role_file"] = role_path
        node["_role_bytes"] = role_bytes
        resolved_nodes.append(node)
    return plan, resolved_nodes


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


def validate_evidence(plan_path: Path, evidence_path: Path, roles_dir: Path) -> dict[str, object]:
    """Bind completed App Server evidence to its frozen plan and installed role cards.

    The returned summary is intentionally conservative: it never claims native lineage, credential isolation, or write
    eligibility. ``parallel`` is returned only when the adapter retained overlapping substantive node intervals.
    """
    plan, plan_nodes = _validated_plan(plan_path, roles_dir)
    evidence = _json_object(evidence_path, "evidence")
    if evidence.get("schema_version") != SCHEMA_VERSION or evidence.get("route") != "app-server":
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
        self._messages: queue.Queue[str | None] = queue.Queue(maxsize=MAX_EVENTS)
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
                if len(self._pending) >= MAX_EVENTS:
                    raise ReviewRouteError("app-server-pending-events-overflow")
                self._pending.append(message)
                continue
            if "error" in message:
                raise ReviewRouteError(f"app-server-request-failed:{method}")
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


def check_host(plan_path: Path, codex: Path, timeout_seconds: float) -> dict[str, object]:
    """Verify the adapter's no-turn host controls before an operator authorizes model work."""
    if (
        not isinstance(timeout_seconds, (int, float))
        or isinstance(timeout_seconds, bool)
        or not 0 < timeout_seconds <= 900
    ):
        raise ReviewRouteError("timeout-seconds-invalid")
    roles_dir = Path(__file__).resolve().parents[1] / "roles"
    plan, nodes = _validated_plan(plan_path, roles_dir)
    cwd = Path(str(plan["cwd"]))
    cli_version = _codex_version(codex, cwd)
    process: subprocess.Popen[str] | None = None
    client: _JsonRpcStdio | None = None
    primary_error = False
    try:
        process, client, capabilities = _prepared_host(codex, cwd, time.monotonic() + timeout_seconds)
        controls: list[dict[str, object]] = []
        for node in nodes:
            if _read_bytes(Path(str(node["_context_file"])), "plan-context") != node["_context_bytes"]:
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
                    "config": {"model_reasoning_effort": node["reasoning_effort"]},
                },
            )
            controls.append({"role_id": node["role_id"], "observed_controls": _thread_controls(result, node)})
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
    plan, nodes = _validated_plan(plan_path, roles_dir)
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
    failure_diagnostic: dict[str, str] | None = None
    turns_attempted = 0
    turns_acknowledged = 0
    cleanup = "not-started"
    try:
        cli_version = _codex_version(codex, Path(str(plan["cwd"])))
        process, client, capabilities = _prepared_host(codex, Path(str(plan["cwd"])), deadline)
        failure_phase = "thread-setup"
        for node in nodes:
            context_path = Path(str(node["_context_file"]))
            if _read_bytes(context_path, "plan-context") != node["_context_bytes"]:
                raise ReviewRouteError("context-mutated-before-turn")
        active: dict[str, dict[str, object]] = {}
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
                    "config": {"model_reasoning_effort": node["reasoning_effort"]},
                },
            )
            thread = _mapping(result.get("thread"), "app-server-thread")
            thread_id = _text(thread.get("id"), "app-server-thread-id")
            active[str(node["role_id"])] = {
                "node": node,
                "thread_id": thread_id,
                "controls": _thread_controls(result, node),
            }
        for role_id, state in active.items():
            node = _mapping(state["node"], "active-node")
            if _read_bytes(plan_path, "plan") != frozen_plan:
                raise ReviewRouteError("plan-mutated-before-turn")
            role_path = Path(str(node["_role_file"]))
            role_bytes = node["_role_bytes"]
            if not isinstance(role_bytes, bytes):
                raise ReviewRouteError("role-card-bytes-invalid")
            if _read_bytes(role_path, "role-card") != role_bytes:
                raise ReviewRouteError("role-card-mutated-before-turn")
            context_path = Path(str(node["_context_file"]))
            if _read_bytes(context_path, "plan-context") != node["_context_bytes"]:
                raise ReviewRouteError("context-mutated-before-turn")
            context = node["_context_bytes"]
            if not isinstance(context, bytes):
                raise ReviewRouteError("plan-context-bytes-invalid")
            failure_phase = "turn-dispatch"
            turns_attempted += 1
            turn = client.request(
                "turn/start",
                {
                    "threadId": state["thread_id"],
                    "cwd": plan["cwd"],
                    "approvalPolicy": "never",
                    "model": node["model"],
                    "effort": node["reasoning_effort"],
                    "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
                    "input": [{"type": "text", "text": context.decode("utf-8")}],
                },
            )
            turns_acknowledged += 1
            turn_data = _mapping(turn.get("turn"), "app-server-turn")
            state["turn_id"] = _text(turn_data.get("id"), "app-server-turn-id")
            state["started_at_ms"] = None
            state["finished_at_ms"] = None
            state["final"] = None
            state["input_echo_item_id"] = None
            state["input_echo_completed"] = False
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
            matching = [
                (role_id, state)
                for role_id, state in active.items()
                if params.get("threadId") == state["thread_id"]
                and (
                    params.get("turnId") == state.get("turn_id")
                    or _mapping(params.get("turn", {}), "event-turn").get("id") == state.get("turn_id")
                )
            ]
            if not matching:
                raise ReviewRouteError("app-server-thread-or-turn-mismatch")
            role_id, state = matching[0]
            if method in PLAN_NOTIFICATION_METHODS:
                _validate_plan_notification(method, params)
                continue
            item = params.get("item")
            if isinstance(item, Mapping):
                item_type = item.get("type")
                if item_type == "userMessage":
                    node = _mapping(state["node"], "active-node")
                    context = node["_context_bytes"]
                    content = item.get("content")
                    item_id = item.get("id")
                    input_matches = (
                        isinstance(content, list)
                        and len(content) == 1
                        and isinstance(content[0], Mapping)
                        and set(content[0]) <= {"type", "text", "text_elements"}
                        and content[0].get("type") == "text"
                        and isinstance(context, bytes)
                        and isinstance(item_id, str)
                        and bool(item_id)
                        and content[0].get("text") == context.decode("utf-8")
                        and content[0].get("text_elements", []) == []
                    )
                    if method == "item/started" and state["input_echo_item_id"] is None and input_matches:
                        state["input_echo_item_id"] = item_id
                        continue
                    if (
                        method == "item/completed"
                        and state["input_echo_item_id"] == item_id
                        and state["input_echo_completed"] is False
                        and input_matches
                    ):
                        state["input_echo_completed"] = True
                        continue
                    raise ReviewRouteError("app-server-user-message-lifecycle-invalid")
                if item_type == "plan" and (
                    not isinstance(item.get("id"), str) or not isinstance(item.get("text"), str)
                ):
                    raise ReviewRouteError("app-server-plan-item-invalid")
                if item_type not in ALLOWED_ITEM_TYPES:
                    raise ReviewRouteError("app-server-item-type-rejected")
                state["started_at_ms"] = state["started_at_ms"] or int(time.monotonic() * 1000)
                if method == "item/completed" and item_type == "agentMessage" and item.get("phase") == "final_answer":
                    final = item.get("text")
                    if (
                        not isinstance(final, str)
                        or not final.strip()
                        or len(final.encode("utf-8")) > MAX_OUTPUT_BYTES
                        or state["final"] is not None
                    ):
                        raise ReviewRouteError("app-server-final-output-invalid-or-duplicate")
                    if _contains_secret(final.encode("utf-8")):
                        raise ReviewRouteError("app-server-final-output-secret")
                    state["final"] = final
            if message.get("method") == "turn/completed":
                turn = _mapping(params.get("turn"), "app-server-completed-turn")
                if turn.get("status") != "completed":
                    raise ReviewRouteError("app-server-turn-status-invalid")
                if state["final"] is None:
                    raise ReviewRouteError("app-server-turn-final-missing")
                if state["input_echo_item_id"] is not None and state["input_echo_completed"] is not True:
                    raise ReviewRouteError("app-server-user-message-lifecycle-incomplete")
                state["finished_at_ms"] = int(time.monotonic() * 1000)
                output = output_root / f"{role_id}.md"
                final = _text(state["final"], "app-server-final-output")
                output.write_bytes(final.encode("utf-8"))
                state["output_path"] = output
                pending.remove(role_id)
        if _read_bytes(plan_path, "plan") != frozen_plan:
            raise ReviewRouteError("plan-mutated-during-review")
        for node in nodes:
            role_path = Path(str(node["_role_file"]))
            role_bytes = node["_role_bytes"]
            if not isinstance(role_bytes, bytes):
                raise ReviewRouteError("role-card-bytes-invalid")
            if _read_bytes(role_path, "role-card") != role_bytes:
                raise ReviewRouteError("role-card-mutated-during-review")
            context_path = Path(str(node["_context_file"]))
            if _read_bytes(context_path, "plan-context") != node["_context_bytes"]:
                raise ReviewRouteError("context-mutated-during-review")
        evidence_nodes: list[dict[str, object]] = []
        if cli_version is None:
            raise ReviewRouteError("codex-version-unavailable")
        for role_id, state in active.items():
            node = _mapping(state["node"], "active-node")
            output = Path(str(state["output_path"]))
            output_name = output.relative_to(output_root).as_posix()
            final = _text(state["final"], "app-server-final-output")
            evidence_nodes.append(
                {
                    "role_id": role_id,
                    "role_card_sha256": node["role_card_sha256"],
                    "context_path": node["context_path"],
                    "context_sha256": node["context_sha256"],
                    "output_path": output_name,
                    "output_sha256": _sha256_bytes(final.encode("utf-8")),
                    "thread_id": state["thread_id"],
                    "turn_id": state["turn_id"],
                    "observed_controls": state["controls"],
                    "terminal_status": "completed",
                    "started_at_ms": state["started_at_ms"],
                    "finished_at_ms": state["finished_at_ms"],
                }
            )
        evidence = {
            "schema_version": SCHEMA_VERSION,
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
    if failure is not None:
        failure_evidence: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
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
            output_root / "evidence.json",
            failure_evidence,
        )
        if isinstance(failure, ReviewRouteError):
            raise failure
        raise ReviewRouteError("app-server-review-failed") from failure
    assert evidence is not None
    evidence_path = output_root / "evidence.json"
    _atomic_json(evidence_path, evidence)
    validate_evidence(plan_path, evidence_path, roles_dir)
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
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
