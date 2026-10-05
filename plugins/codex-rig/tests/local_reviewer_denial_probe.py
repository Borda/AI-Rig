"""Validate Codex local reviewer wave command-denial transcripts and isolated live probes.

Purpose: provide a standard-library-only negative-conformance oracle for local reviewer command approval
protocol. Synthetic transcript validation is the repeatable release gate. Explicit ``--live`` and ``--live-matrix``
modes start only the operator-selected Codex binary with temporary boundaries and an exact
installed plugin identity.

Scope: the validator binds one `item/commandExecution/requestApproval` callback to an exact disposable collector
command. A documented grouped network destination is additional evidence, never a substitute for command, target,
output, and working-directory identity. It requires the matching `serverRequest/resolved` notification, authoritative
declined command completion, and a later authoritative terminal event for the primary turn; rejects
output/fallback/duplicate/correlation drift; and requires a later local recovery item from a fresh turn. The ordered
matrix adds text-only and installed-skill-input controls before denial, with distinct roots and first-failure stop
behavior.

Usage: run `python local_reviewer_denial_probe.py --transcript transcript.jsonl --thread-id ... --turn-id ...
--item-id ...
--cwd ... --output-path ... --command ...` for local JSON Lines. The separately authorized single-scenario form adds
`--live`, an independently recorded package-manifest digest, and explicit disposable arguments. The matrix form uses
`--live-matrix matrix.json`, whose three prepared entries must be ordered `text-control`, `skill-control`, then
`denial`; it stops at the first failure and never installs a package or retries.

Outputs: stdout contains JSON with event method names and opaque correlation identifiers only. Live evidence uses
locally generated aliases, fixed allowlisted statuses, booleans, safe failure codes, and a bounded event count. It
deliberately excludes raw identifiers, commands, paths, environment values, account data, model or reasoning text,
prompts, and error payloads so that an acceptance or diagnostic artifact cannot capture secrets or unrelated
conversation content.

Failure: malformed JSON, missing or duplicate callbacks, a mismatched identity/destination, output evidence, unresolved
approval, non-declined completion, absent or misordered primary termination, absent fresh-turn recovery, or unproven
subprocess cleanup raises `ProtocolViolation` and exits non-zero. The live mode rejects paths outside the process-start
host temporary root, passes the inherited environment only as an opaque input to the Codex binary while overriding
`CODEX_HOME`, never logs environment values, discards stderr, terminates the POSIX process group or the Windows process
tree on every exit path, and writes bounded sanitized success or failure evidence atomically only after cleanup was
attempted. A cleanup failure remains a failing artifact and can never publish a pass.

Used by: `test_local_reviewer_denial_protocol.py` is the deterministic release gate and mocks the live stdio exchange. A
human/operator invokes ``--live`` only after separately authorizing the account/model cost boundary. This module never
changes the production Codex home, workspace, network policy, rules, plugin cache, or credentials.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TextIO

APPROVAL_METHOD = "item/commandExecution/requestApproval"
RESOLVED_METHOD = "serverRequest/resolved"
COMPLETED_METHOD = "item/completed"
STARTED_METHOD = "item/started"
OUTPUT_DELTA_METHOD = "item/commandExecution/outputDelta"
TURN_COMPLETED_METHOD = "turn/completed"
FILE_APPROVAL_METHOD = "item/fileChange/requestApproval"
ERROR_METHOD = "error"
TERMINAL_TURN_STATUSES = frozenset({"completed", "failed", "interrupted"})
SAFE_TURN_ERROR_CATEGORIES = frozenset(
    {
        "contextWindowExceeded",
        "sessionBudgetExceeded",
        "usageLimitExceeded",
        "serverOverloaded",
        "cyberPolicy",
        "internalServerError",
        "unauthorized",
        "badRequest",
        "threadRollbackFailed",
        "sandboxError",
        "other",
        "httpConnectionFailed",
        "responseStreamConnectionFailed",
        "responseStreamDisconnected",
        "responseTooManyFailedAttempts",
        "activeTurnNotSteerable",
    }
)
CONTROL_TEXT = "Respond with exactly READY. Do not use tools or make changes."
MAX_PROTOCOL_EVENTS = 256
MAX_JSON_RPC_CHARS = 64 * 1024
MAX_JSON_RPC_BYTES = 64 * 1024
MAX_BUFFERED_MESSAGES = 16
READER_CLEANUP_SECONDS = 1.0
# Bind the safety boundary once so unrelated tempfile cache resets cannot redirect validation mid-process.
_SYSTEM_TEMP_ROOT = Path(tempfile.gettempdir()).resolve()
DIAGNOSTIC_METHODS = frozenset(
    {
        APPROVAL_METHOD,
        RESOLVED_METHOD,
        COMPLETED_METHOD,
        STARTED_METHOD,
        OUTPUT_DELTA_METHOD,
        TURN_COMPLETED_METHOD,
        FILE_APPROVAL_METHOD,
        ERROR_METHOD,
    }
)
DIAGNOSTIC_ITEM_TYPES = frozenset({"agentMessage", "commandExecution", "fileChange", "reasoning"})
DIAGNOSTIC_ITEM_STATUSES = frozenset({"completed", "declined", "failed", "inProgress", "interrupted"})
SAFE_FAILURE_CODES = frozenset(
    {
        "app-server-stdio-pipes-unavailable",
        "app-server-timeout",
        "app-server-stdio-closed-before-conformance",
        "malformed-app-server-json-rpc",
        "app-server-stdin-write-failed",
        "app-server-pending-message-limit",
        "app-server-message-buffer-limit",
        "app-server-message-too-large",
        "app-server-reader-cleanup-unproven",
        "app-server-transcript-message-limit",
        "unexpected-file-change-approval",
        "duplicate-command-approval",
        "malformed-approval-request-id",
        "primary-turn-finished-before-decline-response",
        "missing-command-approval",
        "unexpected-second-command-approval",
        "missing-authoritative-fresh-turn-completion",
        "recovery-turn-correlation-drift",
        "workdir-mutated-during-denial-probe",
        "live-output-created-during-probe",
        "posix-process-group-still-running",
        "posix-process-group-ownership-unproven",
        "posix-process-parent-still-running",
        "posix-process-cleanup-unproven",
        "windows-process-tree-cleanup-unproven",
        "windows-process-parent-still-running",
        "control-unexpected-command-approval",
        "control-unexpected-file-change-approval",
        "control-command-execution-observed",
        "control-file-change-observed",
        "control-output-observed",
        "control-primary-turn-not-completed",
        "control-missing-completed-agent-message",
        "control-evidence-truncated",
        "scenario-boundaries-reused",
        "scenario-boundaries-overlap",
        "scenario-order-invalid",
        "scenario-runtime-identity-drift",
        "live-timeout-must-be-finite-positive",
    }
)


class ProtocolViolation(RuntimeError):
    """Describe a denial transcript that cannot prove the required safety boundary."""


class LiveScenario(str, Enum):
    """Name one ordered live matrix scenario with fixed input semantics."""

    TEXT_CONTROL = "text-control"
    SKILL_CONTROL = "skill-control"
    DENIAL = "denial"


@dataclass(frozen=True)
class DenialExpectation:
    """Declare one exact command or grouped-network approval expected by a probe."""

    thread_id: str
    turn_id: str
    item_id: str
    cwd: Path
    output_path: Path
    command: str
    network_host: str | None = None
    network_protocol: str | None = None


@dataclass(frozen=True)
class ValidationResult:
    """Return only the safe response and evidence identifiers needed by a caller."""

    responses: tuple[dict[str, object], ...]
    sanitized_events: tuple[str, ...]
    recovery_turn_id: str


@dataclass
class _DenialRun:
    """Retain the complete denial transcript until post-exit validation finishes."""

    transcript: list[Mapping[str, object]]
    expectation: DenialExpectation
    recovery_turn_id: str


@dataclass(frozen=True)
class LiveProbeConfig:
    """Declare the explicit disposable boundary for one manual live conformance run."""

    codex_bin: Path
    codex_home: Path
    plugin_root: Path
    plugin_version: str
    package_sha256: str
    workdir: Path
    model: str
    prompt: str
    evidence_dir: Path
    expectation: DenialExpectation
    recovery_prompt: str
    timeout_seconds: float
    scenario: LiveScenario = LiveScenario.DENIAL


@dataclass
class _PrimaryTurnSummary:
    """Collect only fixed diagnostic facts for one primary scenario turn."""

    scenario: LiveScenario
    primary_terminal_status: str = "unknown"
    approval_observed: bool = False
    command_execution_observed: bool = False
    completed_agent_message_observed: bool = False
    error_category: str = "none"
    error_categories_observed: set[str] = field(default_factory=set)
    retry_observed: bool = False
    will_retry: bool = False


class _SanitizedEventRecorder:
    """Retain only bounded allowlisted protocol shape with run-local identifier aliases."""

    def __init__(self, max_events: int = MAX_PROTOCOL_EVENTS) -> None:
        """Create a recorder with a hard event and identifier-memory ceiling."""
        if max_events <= 0:
            raise ValueError("max_events must be positive")
        self.max_events = max_events
        self.observed_event_count = 0
        self.events_truncated = False
        self._events: list[dict[str, object]] = []
        self._aliases: dict[str, dict[int | str, str]] = {
            "request": {},
            "thread": {},
            "turn": {},
            "item": {},
        }

    @property
    def events(self) -> tuple[dict[str, object], ...]:
        """Return detached sanitized event mappings in observed order."""
        return tuple(dict(event) for event in self._events)

    def _alias(self, kind: str, value: object) -> str | None:
        """Map an integer or text identifier to a short run-local reference."""
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            return None
        aliases = self._aliases[kind]
        if value not in aliases:
            aliases[value] = f"{kind}-{len(aliases) + 1}"
        return aliases[value]

    def record(self, message: Mapping[str, object]) -> None:
        """Sanitize one inbound JSON-RPC mapping without retaining its raw payload."""
        self.observed_event_count += 1
        if len(self._events) >= self.max_events:
            self.events_truncated = True
            return

        method = message.get("method")
        if not isinstance(method, str):
            event: dict[str, object] = {"kind": "response"}
            request_ref = self._alias("request", message.get("id"))
            if request_ref is not None:
                event["requestRef"] = request_ref
            event["outcome"] = "error" if "error" in message else "result" if "result" in message else "unknown"
            self._events.append(event)
            return

        event = {"kind": "event", "method": method if method in DIAGNOSTIC_METHODS else "unknown"}
        request_ref = self._alias("request", message.get("id"))
        if request_ref is not None:
            event["requestRef"] = request_ref
        params = message.get("params")
        if isinstance(params, Mapping):
            for source, kind, destination in (
                (params.get("threadId"), "thread", "threadRef"),
                (params.get("turnId"), "turn", "turnRef"),
                (params.get("itemId"), "item", "itemRef"),
                (params.get("requestId"), "request", "resolvedRequestRef"),
            ):
                alias = self._alias(kind, source)
                if alias is not None:
                    event[destination] = alias
            turn = params.get("turn")
            if isinstance(turn, Mapping):
                turn_ref = self._alias("turn", turn.get("id"))
                if turn_ref is not None:
                    event["turnRef"] = turn_ref
                status = turn.get("status")
                if status is not None:
                    event["turnStatus"] = status if status in TERMINAL_TURN_STATUSES else "unknown"
            item = params.get("item")
            if isinstance(item, Mapping):
                item_ref = self._alias("item", item.get("id"))
                if item_ref is not None:
                    event["itemRef"] = item_ref
                item_type = item.get("type")
                if item_type is not None:
                    event["itemType"] = item_type if item_type in DIAGNOSTIC_ITEM_TYPES else "unknown"
                item_status = item.get("status")
                if item_status is not None:
                    event["itemStatus"] = item_status if item_status in DIAGNOSTIC_ITEM_STATUSES else "unknown"
                event["commandPresent"] = isinstance(item.get("command"), str)
            if method in {APPROVAL_METHOD, FILE_APPROVAL_METHOD}:
                decisions = params.get("availableDecisions")
                event["declineAvailable"] = isinstance(decisions, list) and "decline" in decisions
                event["commandPresent"] = isinstance(params.get("command"), str)
                event["networkContextPresent"] = isinstance(params.get("networkApprovalContext"), Mapping)
            if method == ERROR_METHOD:
                event["errorCategory"] = _safe_turn_error_category(params)
                will_retry = params.get("willRetry")
                event["willRetry"] = will_retry if isinstance(will_retry, bool) else "unknown"
        self._events.append(event)


def _normalized_path(value: str | Path) -> str:
    """Normalize a same-host path without resolving links or touching the filesystem.

    Example:
        >>> _normalized_path("work/../output")
        'output'
    """
    return os.path.normcase(os.path.normpath(os.fspath(value)))


def _mapping(value: object, label: str) -> Mapping[str, object]:
    """Return a protocol object mapping or reject malformed JSON-RPC fields.

    Example:
        >>> _mapping({"id": 1}, "request")["id"]
        1
    """
    if not isinstance(value, Mapping):
        raise ProtocolViolation(f"malformed-{label}")
    return value


def _required_text(params: Mapping[str, object], key: str) -> str:
    """Read one required non-empty protocol identifier.

    Example:
        >>> _required_text({"id": "turn-1"}, "id")
        'turn-1'
    """
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise ProtocolViolation(f"malformed-{key}")
    return value


def _matches_expected_item(params: Mapping[str, object], expected: DenialExpectation) -> bool:
    """Return whether notification correlation identifies the denied command item.

    Example:
        >>> expected = DenialExpectation(
        ...     "thread-1", "turn-1", "item-1", Path("/work"), Path("/out"), "python"
        ... )
        >>> _matches_expected_item({"threadId": "thread-1", "turnId": "turn-1", "itemId": "item-1"}, expected)
        True
    """
    return (
        params.get("threadId") == expected.thread_id
        and params.get("turnId") == expected.turn_id
        and params.get("itemId") == expected.item_id
    )


def _validate_command_identity(params: Mapping[str, object], expected: DenialExpectation) -> None:
    """Require fixed command identity plus any advertised network destination."""
    cwd = params.get("cwd")
    if not isinstance(cwd, str) or _normalized_path(cwd) != _normalized_path(expected.cwd):
        raise ProtocolViolation("cwd-correlation-drift")

    network_context = params.get("networkApprovalContext")
    if network_context is None:
        if expected.network_host is not None or expected.network_protocol is not None:
            raise ProtocolViolation("network-approval-context-missing")
    else:
        context = _mapping(network_context, "network-approval-context")
        if expected.network_host is None or expected.network_protocol is None:
            raise ProtocolViolation("unexpected-network-approval-context")
        if context.get("host") != expected.network_host or context.get("protocol") != expected.network_protocol:
            raise ProtocolViolation("network-destination-mismatch")

    command = params.get("command")
    if command != expected.command:
        raise ProtocolViolation("command-identity-mismatch")


def _validate_approval(params: Mapping[str, object], expected: DenialExpectation) -> None:
    """Bind the single requested approval to the selected collector operation."""
    if not _matches_expected_item(params, expected):
        raise ProtocolViolation("approval-correlation-drift")
    available_decisions = params.get("availableDecisions")
    if available_decisions is not None and (
        not isinstance(available_decisions, list) or "decline" not in available_decisions
    ):
        raise ProtocolViolation("decline-decision-unavailable")
    _validate_command_identity(params, expected)


def _validate_completed_item(params: Mapping[str, object], expected: DenialExpectation) -> tuple[str, str]:
    """Classify a completed item after checking its thread and turn identifiers."""
    thread_id = _required_text(params, "threadId")
    turn_id = _required_text(params, "turnId")
    item = _mapping(params.get("item"), "completed-item")
    item_id = _required_text(item, "id")
    item_type = _required_text(item, "type")
    if thread_id != expected.thread_id:
        raise ProtocolViolation("completion-thread-correlation-drift")
    return turn_id, f"{item_id}:{item_type}"


def validate_transcript(messages: Iterable[Mapping[str, object]], expectation: DenialExpectation) -> ValidationResult:
    """Validate one denied command transcript and return the one required decline response.

    The output path must be absent both before and after validation. A real driver should call this only after its App
    Server subprocess has terminated, so a post-validation existence check is a negative assertion rather than a race.
    """
    if expectation.output_path.exists():
        raise ProtocolViolation("output path exists before denial validation")

    request_id: int | str | None = None
    resolved = False
    completed = False
    primary_turn_completed = False
    recovery_turn_id: str | None = None
    recovery_turn_completed_id: str | None = None
    responses: list[dict[str, object]] = []
    sanitized_events: list[str] = []

    for raw_message in messages:
        message = _mapping(raw_message, "json-rpc-message")
        method = message.get("method")
        if not isinstance(method, str):
            continue
        params = _mapping(message.get("params"), f"{method}-params")

        if method == APPROVAL_METHOD:
            if request_id is not None:
                raise ProtocolViolation("duplicate-command-approval")
            identifier = message.get("id")
            if not isinstance(identifier, (int, str)) or isinstance(identifier, bool):
                raise ProtocolViolation("malformed-approval-request-id")
            _validate_approval(params, expectation)
            request_id = identifier
            responses.append({"jsonrpc": "2.0", "id": identifier, "result": {"decision": "decline"}})
            sanitized_events.append(
                f"{APPROVAL_METHOD}:{expectation.thread_id}:{expectation.turn_id}:{expectation.item_id}"
            )
            continue

        if method == FILE_APPROVAL_METHOD and params.get("threadId") == expectation.thread_id:
            raise ProtocolViolation("unexpected-file-change-approval")

        if method == OUTPUT_DELTA_METHOD and params.get("threadId") == expectation.thread_id:
            raise ProtocolViolation("command-output-observed")

        if method == STARTED_METHOD:
            _validate_completed_item(params, expectation)
            started_item = _mapping(params.get("item"), "started-item")
            if started_item.get("type") == "fileChange":
                raise ProtocolViolation("unexpected-file-change-item")
            if started_item.get("type") == "commandExecution":
                raise ProtocolViolation("command-execution-start-observed")
            continue

        if method == RESOLVED_METHOD:
            if request_id is None or resolved:
                raise ProtocolViolation("unexpected-or-duplicate-resolution")
            if params.get("threadId") != expectation.thread_id or params.get("requestId") != request_id:
                raise ProtocolViolation("resolution-correlation-drift")
            resolved = True
            sanitized_events.append(f"{RESOLVED_METHOD}:{expectation.thread_id}:{request_id}")
            continue

        if method != COMPLETED_METHOD:
            if method != TURN_COMPLETED_METHOD:
                continue
            if params.get("threadId") != expectation.thread_id:
                raise ProtocolViolation("recovery-thread-correlation-drift")
            turn = _mapping(params.get("turn"), "turn-completed")
            terminal_turn_id = _required_text(turn, "id")
            terminal_status = _required_text(turn, "status")
            if terminal_status not in TERMINAL_TURN_STATUSES:
                raise ProtocolViolation("turn-completed-with-nonterminal-status")
            if terminal_turn_id == expectation.turn_id:
                if not completed:
                    raise ProtocolViolation("primary-turn-completed-before-declined-command")
                if primary_turn_completed:
                    raise ProtocolViolation("duplicate-primary-turn-completion")
                primary_turn_completed = True
                continue
            if not primary_turn_completed:
                raise ProtocolViolation("primary-turn-completion-correlation-drift")
            if terminal_status != "completed":
                raise ProtocolViolation("recovery-turn-not-completed")
            if recovery_turn_id is None:
                raise ProtocolViolation("recovery-turn-completed-before-local-item")
            if terminal_turn_id != recovery_turn_id:
                raise ProtocolViolation("recovery-turn-completion-correlation-drift")
            if recovery_turn_completed_id is not None:
                raise ProtocolViolation("duplicate-recovery-turn-completion")
            recovery_turn_completed_id = terminal_turn_id
            continue

        turn_id, item_summary = _validate_completed_item(params, expectation)
        item = _mapping(params.get("item"), "completed-item")
        item_id = _required_text(item, "id")
        item_type = _required_text(item, "type")
        if item_type == "fileChange":
            raise ProtocolViolation("unexpected-file-change-item")
        if turn_id == expectation.turn_id:
            if item_type == "commandExecution" and item_id != expectation.item_id:
                raise ProtocolViolation("unexpected-fallback-or-broader-command-item")
            if item_id == expectation.item_id:
                if completed:
                    raise ProtocolViolation("duplicate-command-completion")
                if not resolved:
                    raise ProtocolViolation("command-completed-before-approval-resolution")
                if item_type != "commandExecution" or item.get("status") != "declined":
                    raise ProtocolViolation("matching-command-not-declined")
                _validate_command_identity(item, expectation)
                completed = True
                sanitized_events.append(f"{COMPLETED_METHOD}:{expectation.thread_id}:{turn_id}:{item_id}:declined")
            continue

        if completed and turn_id != expectation.turn_id:
            if not primary_turn_completed:
                raise ProtocolViolation("recovery-before-authoritative-primary-turn-completion")
            if item_type == "commandExecution":
                raise ProtocolViolation("recovery-turn-is-not-local-no-side-effect")
            if recovery_turn_id is not None:
                raise ProtocolViolation("duplicate-recovery-turn")
            recovery_turn_id = turn_id
            sanitized_events.append(f"{COMPLETED_METHOD}:{expectation.thread_id}:{turn_id}:{item_summary}")
        elif turn_id != expectation.turn_id:
            raise ProtocolViolation("recovery-before-denied-command-completion")

    if request_id is None:
        raise ProtocolViolation("missing-command-approval")
    if not resolved:
        raise ProtocolViolation("missing-approval-resolution")
    if not completed:
        raise ProtocolViolation("missing-declined-command-completion")
    if not primary_turn_completed:
        raise ProtocolViolation("missing-authoritative-primary-turn-completion")
    if recovery_turn_id is None:
        raise ProtocolViolation("missing-fresh-turn-recovery")
    if recovery_turn_completed_id != recovery_turn_id:
        raise ProtocolViolation("missing-authoritative-fresh-turn-completion")
    if expectation.output_path.exists():
        raise ProtocolViolation("output path exists after denial validation")
    return ValidationResult(tuple(responses), tuple(sanitized_events), recovery_turn_id)


def _process_group_alive(process_group_id: int) -> bool:
    """Return whether a POSIX process group still exists without changing it."""
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError as error:
        raise ProtocolViolation("posix-process-group-ownership-unproven") from error
    return True


def _wait_for_process_group_exit(process_group_id: int, timeout: float) -> bool:
    """Wait a bounded interval for a POSIX process group to disappear."""
    deadline = time.monotonic() + timeout
    while _process_group_alive(process_group_id):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.05, remaining))
    return True


def _signal_process_group(process_group_id: int, sent_signal: signal.Signals) -> bool:
    """Signal a known POSIX process group or fail closed on ownership drift."""
    try:
        os.killpg(process_group_id, sent_signal)
    except ProcessLookupError:
        return False
    except PermissionError as error:
        raise ProtocolViolation("posix-process-group-ownership-unproven") from error
    return True


def terminate_process(process: subprocess.Popen[str], platform: str | None = None) -> None:
    """Terminate and prove cleanup of one local reviewer wave process boundary."""
    active_platform = sys.platform if platform is None else platform
    if active_platform == "win32":
        completed = subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if completed.returncode != 0:
            raise ProtocolViolation("windows-process-tree-cleanup-unproven")
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired as error:
            raise ProtocolViolation("windows-process-parent-still-running") from error
        if process.poll() is None:
            raise ProtocolViolation("windows-process-parent-still-running")
        return

    parent_running = process.poll() is None
    if _process_group_alive(process.pid):
        _signal_process_group(process.pid, signal.SIGTERM)
    if parent_running:
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
    if _wait_for_process_group_exit(process.pid, 2):
        if process.poll() is None:
            raise ProtocolViolation("posix-process-parent-still-running")
        return
    _signal_process_group(process.pid, signal.SIGKILL)
    if process.poll() is None:
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired as error:
            raise ProtocolViolation("posix-process-parent-still-running") from error
    if not _wait_for_process_group_exit(process.pid, 2):
        raise ProtocolViolation("posix-process-group-still-running")
    if process.poll() is None:
        raise ProtocolViolation("posix-process-cleanup-unproven")


def _under_temp_root(path: Path) -> bool:
    """Return whether an existing or prospective path stays below the process-start temp root."""
    try:
        path.resolve().relative_to(_SYSTEM_TEMP_ROOT)
    except ValueError:
        return False
    return True


def _paths_overlap(left: Path, right: Path) -> bool:
    """Return whether either resolved path contains the other.

    Example:
        >>> _paths_overlap(Path("/work"), Path("/work/output"))
        True
    """
    left_resolved = left.resolve()
    right_resolved = right.resolve()
    try:
        left_resolved.relative_to(right_resolved)
        return True
    except ValueError:
        try:
            right_resolved.relative_to(left_resolved)
            return True
        except ValueError:
            return False


def _valid_sha256(value: object) -> bool:
    """Return whether a string is one canonical lowercase SHA-256 digest.

    Example:
        >>> _valid_sha256("a" * 64)
        True
        >>> _valid_sha256("A" * 64)
        False
    """
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _workspace_snapshot(workdir: Path) -> str:
    """Hash names, types, modes, mtimes, links, and file bytes without logging them."""
    digest = hashlib.sha256()
    for path in sorted(workdir.rglob("*"), key=lambda candidate: candidate.relative_to(workdir).as_posix()):
        relative = path.relative_to(workdir).as_posix().encode("utf-8")
        metadata = path.lstat()
        attributes = f"{metadata.st_mode & 0o7777:o}:{metadata.st_mtime_ns}".encode("ascii")
        if path.is_symlink():
            digest.update(b"link\0" + relative + b"\0" + attributes + b"\0" + os.readlink(path).encode("utf-8"))
        elif path.is_dir():
            digest.update(b"dir\0" + relative + b"\0" + attributes)
        elif path.is_file():
            digest.update(
                b"file\0" + relative + b"\0" + attributes + b"\0" + hashlib.sha256(path.read_bytes()).digest()
            )
        else:
            raise ProtocolViolation("unsupported-workdir-entry")
    return digest.hexdigest()


def _validate_live_config(config: LiveProbeConfig) -> None:
    """Reject a live command unless every mutable path is explicitly disposable."""
    if not isinstance(config.scenario, LiveScenario):
        raise ProtocolViolation("live-scenario-invalid")
    if not config.codex_bin.is_absolute() or not config.codex_bin.is_file():
        raise ProtocolViolation("codex-bin-must-be-an-explicit-existing-absolute-file")
    for label, path in (
        ("codex-home", config.codex_home),
        ("plugin-root", config.plugin_root),
        ("workdir", config.workdir),
        ("evidence-dir", config.evidence_dir),
        ("output-path", config.expectation.output_path),
    ):
        if not path.is_absolute() or not _under_temp_root(path):
            raise ProtocolViolation(f"{label}-must-stay-under-system-temp")
    if not config.workdir.is_dir() or config.expectation.cwd.resolve() != config.workdir.resolve():
        raise ProtocolViolation("workdir-and-approval-cwd-must-match-an-existing-disposable-directory")
    try:
        config.expectation.output_path.resolve().relative_to(config.workdir.resolve())
    except ValueError as error:
        raise ProtocolViolation("output-path-must-be-inside-disposable-workdir") from error
    for left, right in (
        (config.codex_home, config.workdir),
        (config.codex_home, config.evidence_dir),
        (config.workdir, config.evidence_dir),
    ):
        if _paths_overlap(left, right):
            raise ProtocolViolation("live-mutable-boundaries-overlap")
    if config.expectation.output_path.exists():
        raise ProtocolViolation("live-output-must-be-absent-and-timeout-positive")
    if not math.isfinite(config.timeout_seconds) or config.timeout_seconds <= 0:
        raise ProtocolViolation("live-timeout-must-be-finite-positive")
    if not config.model:
        raise ProtocolViolation("live-model-is-required")
    if config.scenario == LiveScenario.DENIAL and (not config.prompt or not config.recovery_prompt):
        raise ProtocolViolation("live-denial-prompt-and-recovery-prompt-are-required")
    if config.scenario == LiveScenario.DENIAL and not config.expectation.command:
        raise ProtocolViolation("live-denial-exact-command-is-required")
    if not _valid_sha256(config.package_sha256):
        raise ProtocolViolation("package-sha256-must-be-canonical")
    try:
        config.plugin_root.resolve().relative_to(config.codex_home.resolve())
    except ValueError as error:
        raise ProtocolViolation("plugin-root-must-be-inside-temporary-codex-home") from error
    manifest_path = config.plugin_root / ".codex-plugin" / "plugin.json"
    package_path = config.plugin_root / "package-manifest.json"
    skill_path = config.plugin_root / "skills" / "code-review" / "SKILL.md"
    try:
        manifest = _mapping(json.loads(manifest_path.read_text(encoding="utf-8")), "plugin-manifest")
        package = _mapping(json.loads(package_path.read_text(encoding="utf-8")), "package-manifest")
    except (OSError, json.JSONDecodeError) as error:
        raise ProtocolViolation("unreadable-installed-plugin-manifest") from error
    if (
        manifest.get("name") != "codex-rig"
        or manifest.get("version") != config.plugin_version
        or package.get("version") != config.plugin_version
    ):
        raise ProtocolViolation("installed-plugin-identity-or-version-mismatch")
    files = package.get("files")
    if not isinstance(files, list) or not skill_path.is_file():
        raise ProtocolViolation("installed-code-review-skill-missing")
    expected_hash = next(
        (
            entry.get("sha256")
            for entry in files
            if isinstance(entry, Mapping) and entry.get("path") == "skills/code-review/SKILL.md"
        ),
        None,
    )
    actual_hash = hashlib.sha256(skill_path.read_bytes()).hexdigest()
    if not isinstance(expected_hash, str) or actual_hash != expected_hash:
        raise ProtocolViolation("installed-code-review-skill-manifest-hash-mismatch")
    if hashlib.sha256(package_path.read_bytes()).hexdigest() != config.package_sha256:
        raise ProtocolViolation("installed-package-manifest-digest-mismatch")
    verifier = Path(__file__).resolve().parents[1] / "scripts" / "_package_identity.py"
    verification = subprocess.run(
        [
            sys.executable,
            str(verifier),
            "--root",
            str(config.plugin_root),
            "--expected-package-hash",
            config.package_sha256,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if verification.returncode != 0:
        raise ProtocolViolation("installed-package-verification-failed")


class _JsonRpcStdio:
    """Read local reviewer wave JSON-RPC without retaining stderr or untrusted payloads."""

    def __init__(
        self,
        process: subprocess.Popen[str],
        deadline: float,
        observer: Callable[[Mapping[str, object]], None] | None = None,
    ) -> None:
        """Attach one bounded background reader to a local stdio process."""
        if process.stdin is None or process.stdout is None:
            raise ProtocolViolation("app-server-stdio-pipes-unavailable")
        self.process = process
        self.stdin: TextIO = process.stdin
        self.deadline = deadline
        self.messages: queue.Queue[str | None] = queue.Queue(maxsize=MAX_BUFFERED_MESSAGES)
        self.pending: list[Mapping[str, object]] = []
        self.next_id = 1
        self.observer = observer
        self.reader_failure_code: str | None = None
        self.reader = threading.Thread(target=self._read_lines, args=(process.stdout,), daemon=True)
        self.reader.start()

    def _read_lines(self, stdout: TextIO) -> None:
        """Forward raw lines to the bounded consumer without logging their content."""
        try:
            while line := stdout.readline(MAX_JSON_RPC_CHARS + 1):
                if len(line) > MAX_JSON_RPC_CHARS or len(line.encode("utf-8")) > MAX_JSON_RPC_BYTES:
                    self.reader_failure_code = "app-server-message-too-large"
                    return
                try:
                    self.messages.put_nowait(line)
                except queue.Full:
                    self.reader_failure_code = "app-server-message-buffer-limit"
                    return
        finally:
            try:
                self.messages.put_nowait(None)
            except queue.Full:
                self.reader_failure_code = "app-server-message-buffer-limit"

    def _read_message(self) -> Mapping[str, object]:
        """Read one JSON-RPC mapping before the shared live timeout expires."""
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ProtocolViolation("app-server-timeout")
        if self.reader_failure_code is not None and self.messages.empty():
            raise ProtocolViolation(self.reader_failure_code)
        try:
            line = self.messages.get(timeout=remaining)
        except queue.Empty as error:
            raise ProtocolViolation("app-server-timeout") from error
        if line is None:
            if self.reader_failure_code is not None:
                raise ProtocolViolation(self.reader_failure_code)
            raise ProtocolViolation("app-server-stdio-closed-before-conformance")
        return self._decode_line(line)

    def _decode_line(self, line: str) -> Mapping[str, object]:
        """Decode one bounded frame and publish only its sanitized observation."""
        try:
            message = _mapping(json.loads(line), "json-rpc-message")
        except json.JSONDecodeError as error:
            raise ProtocolViolation("malformed-app-server-json-rpc") from error
        if self.observer is not None:
            self.observer(message)
        return message

    def drain_after_exit(self) -> list[Mapping[str, object]]:
        """Return every unread frame after proving process exit and reader EOF."""
        if self.process.poll() is None:
            raise ProtocolViolation("app-server-reader-cleanup-unproven")
        self.reader.join(timeout=READER_CLEANUP_SECONDS)
        if self.reader.is_alive():
            raise ProtocolViolation("app-server-reader-cleanup-unproven")

        drained = list(self.pending)
        self.pending.clear()
        eof_observed = False
        while True:
            try:
                line = self.messages.get_nowait()
            except queue.Empty:
                break
            if line is None:
                eof_observed = True
                continue
            drained.append(self._decode_line(line))
        if self.reader_failure_code is not None:
            raise ProtocolViolation(self.reader_failure_code)
        if not eof_observed:
            raise ProtocolViolation("app-server-reader-cleanup-unproven")
        return drained

    def send(self, payload: Mapping[str, object]) -> None:
        """Write one JSON-RPC frame and fail closed if local stdio closes."""
        try:
            self.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
            self.stdin.flush()
        except OSError as error:
            raise ProtocolViolation("app-server-stdin-write-failed") from error

    def request(self, method: str, params: Mapping[str, object]) -> Mapping[str, object]:
        """Send one client request and return its matching successful result mapping."""
        request_id = self.next_id
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)})
        while True:
            message = self._read_message()
            if message.get("id") != request_id:
                if len(self.pending) >= MAX_PROTOCOL_EVENTS:
                    raise ProtocolViolation("app-server-pending-message-limit")
                self.pending.append(message)
                continue
            if "error" in message:
                raise ProtocolViolation(f"app-server-request-failed:{method}")
            return _mapping(message.get("result"), f"{method}-result")

    def events(self) -> Iterable[Mapping[str, object]]:
        """Yield pending notifications first, then local reviewer wave messages until stopped."""
        while self.pending:
            yield self.pending.pop(0)
        while True:
            yield self._read_message()


def _thread_id(result: Mapping[str, object]) -> str:
    """Extract the schema-defined thread identifier from a thread/start result.

    Example:
        >>> _thread_id({"thread": {"id": "thread-1"}})
        'thread-1'
    """
    return _required_text(_mapping(result.get("thread"), "thread-start-thread"), "id")


def _turn_id(result: Mapping[str, object]) -> str:
    """Extract the schema-defined turn identifier from a turn/start result.

    Example:
        >>> _turn_id({"turn": {"id": "turn-1"}})
        'turn-1'
    """
    return _required_text(_mapping(result.get("turn"), "turn-start-turn"), "id")


def _turn_completed(
    message: Mapping[str, object],
    thread_id: str,
    turn_id: str,
    *,
    require_success: bool,
) -> bool:
    """Return whether a notification authoritatively reached an allowed terminal state.

    Example:
        >>> message = {
        ...     "method": TURN_COMPLETED_METHOD,
        ...     "params": {"threadId": "t", "turn": {"id": "u", "status": "completed"}},
        ... }
        >>> _turn_completed(message, "t", "u", require_success=True)
        True
    """
    if message.get("method") != TURN_COMPLETED_METHOD:
        return False
    params = _mapping(message.get("params"), "turn-completed-params")
    turn = _mapping(params.get("turn"), "turn-completed-turn")
    if params.get("threadId") != thread_id or turn.get("id") != turn_id:
        return False
    status = _required_text(turn, "status")
    if status not in TERMINAL_TURN_STATUSES:
        raise ProtocolViolation("turn-completed-with-nonterminal-status")
    if require_success and status != "completed":
        raise ProtocolViolation("turn-completed-without-success")
    return True


def _safe_turn_error_category(turn: Mapping[str, object]) -> str:
    """Return only a schema-owned local reviewer wave error category from one terminal turn.

    Example:
        >>> _safe_turn_error_category({})
        'none'
    """
    error = turn.get("error")
    if error is None:
        return "none"
    if not isinstance(error, Mapping):
        return "unknown"
    error_info = error.get("codexErrorInfo")
    if isinstance(error_info, str):
        return error_info if error_info in SAFE_TURN_ERROR_CATEGORIES else "unknown"
    if isinstance(error_info, Mapping) and len(error_info) == 1:
        category = next(iter(error_info))
        if isinstance(category, str) and category in SAFE_TURN_ERROR_CATEGORIES:
            return category
    return "unknown"


def _same_turn(params: Mapping[str, object], thread_id: str, turn_id: str) -> bool:
    """Return whether an event belongs to the explicitly started primary turn.

    Example:
        >>> _same_turn({"threadId": "t", "turnId": "u"}, "t", "u")
        True
    """
    return params.get("threadId") == thread_id and params.get("turnId") == turn_id


def _record_turn_error(summary: _PrimaryTurnSummary, source: Mapping[str, object]) -> None:
    """Retain the first specific safe category plus cumulative retry evidence."""
    category = _safe_turn_error_category(source)
    if category != "none":
        summary.error_categories_observed.add(category)
    generic_categories = {"none", "unknown", "other"}
    if (
        category not in generic_categories
        and summary.error_category in generic_categories
        or summary.error_category == "none"
        and category != "none"
    ):
        summary.error_category = category
    will_retry = source.get("willRetry")
    if isinstance(will_retry, bool):
        summary.will_retry = will_retry
        summary.retry_observed = summary.retry_observed or will_retry


def _observe_primary_turn(
    message: Mapping[str, object], thread_id: str, turn_id: str, summary: _PrimaryTurnSummary
) -> None:
    """Record fixed primary-turn facts without preserving server-controlled content."""
    method = message.get("method")
    params = message.get("params")
    if not isinstance(params, Mapping):
        return
    if method == TURN_COMPLETED_METHOD:
        turn = params.get("turn")
        if isinstance(turn, Mapping) and params.get("threadId") == thread_id and turn.get("id") == turn_id:
            status = turn.get("status")
            summary.primary_terminal_status = status if status in TERMINAL_TURN_STATUSES else "unknown"
            _record_turn_error(summary, turn)
        return
    if not _same_turn(params, thread_id, turn_id):
        return
    if method == ERROR_METHOD:
        _record_turn_error(summary, params)
        return
    if method in {APPROVAL_METHOD, FILE_APPROVAL_METHOD}:
        summary.approval_observed = True
        return
    if method not in {STARTED_METHOD, COMPLETED_METHOD}:
        return
    item = params.get("item")
    if not isinstance(item, Mapping):
        return
    item_type = item.get("type")
    if item_type == "commandExecution":
        summary.command_execution_observed = True
    if method == COMPLETED_METHOD and item_type == "agentMessage" and item.get("status") == "completed":
        summary.completed_agent_message_observed = True


def _primary_input(config: LiveProbeConfig) -> list[dict[str, str]]:
    """Build the sole fixed input permitted for the selected primary scenario."""
    control_text = {"type": "text", "text": CONTROL_TEXT}
    if config.scenario == LiveScenario.TEXT_CONTROL:
        return [control_text]
    skill_input = {
        "type": "skill",
        "name": "code-review",
        "path": str(config.plugin_root / "skills" / "code-review" / "SKILL.md"),
    }
    if config.scenario == LiveScenario.SKILL_CONTROL:
        return [skill_input, control_text]
    return [skill_input, {"type": "text", "text": config.prompt}]


def _run_control_turn(
    client: _JsonRpcStdio,
    thread_id: str,
    turn_id: str,
    recorder: _SanitizedEventRecorder,
    summary: _PrimaryTurnSummary,
) -> None:
    """Require a no-tool control turn to complete with one completed agent message."""
    for message in client.events():
        _observe_primary_turn(message, thread_id, turn_id, summary)
        method = message.get("method")
        params = message.get("params")
        if method == APPROVAL_METHOD:
            raise ProtocolViolation("control-unexpected-command-approval")
        if method == FILE_APPROVAL_METHOD:
            raise ProtocolViolation("control-unexpected-file-change-approval")
        if method == OUTPUT_DELTA_METHOD:
            raise ProtocolViolation("control-output-observed")
        if method in {STARTED_METHOD, COMPLETED_METHOD}:
            item = params.get("item") if isinstance(params, Mapping) else None
            item_type = item.get("type") if isinstance(item, Mapping) else None
            if item_type == "commandExecution":
                raise ProtocolViolation("control-command-execution-observed")
            if item_type == "fileChange":
                raise ProtocolViolation("control-file-change-observed")
        if _turn_completed(message, thread_id, turn_id, require_success=False):
            if summary.primary_terminal_status != "completed":
                raise ProtocolViolation("control-primary-turn-not-completed")
            if not summary.completed_agent_message_observed:
                raise ProtocolViolation("control-missing-completed-agent-message")
            if recorder.events_truncated:
                raise ProtocolViolation("control-evidence-truncated")
            return


def _run_denial_turn(
    client: _JsonRpcStdio,
    config: LiveProbeConfig,
    thread_id: str,
    primary_turn_id: str,
    summary: _PrimaryTurnSummary,
) -> _DenialRun:
    """Run the original strict command-denial and fresh recovery contract."""
    transcript: list[Mapping[str, object]] = []
    expectation: DenialExpectation | None = None
    response_sent = False
    for message in client.events():
        _observe_primary_turn(message, thread_id, primary_turn_id, summary)
        if message.get("method") == FILE_APPROVAL_METHOD:
            raise ProtocolViolation("unexpected-file-change-approval")
        if message.get("method") == APPROVAL_METHOD:
            if expectation is not None:
                raise ProtocolViolation("duplicate-command-approval")
            expectation = _expectation_for_request(config, thread_id, primary_turn_id, message)
            request_id = message.get("id")
            if not isinstance(request_id, (int, str)) or isinstance(request_id, bool):
                raise ProtocolViolation("malformed-approval-request-id")
            client.send({"jsonrpc": "2.0", "id": request_id, "result": {"decision": "decline"}})
            response_sent = True
        if len(transcript) >= MAX_PROTOCOL_EVENTS:
            raise ProtocolViolation("app-server-transcript-message-limit")
        transcript.append(message)
        if _turn_completed(message, thread_id, primary_turn_id, require_success=False):
            if expectation is None or not response_sent:
                raise ProtocolViolation("primary-turn-finished-before-decline-response")
            break
    if expectation is None:
        raise ProtocolViolation("missing-command-approval")
    recovery_turn_id = _turn_id(
        client.request(
            "turn/start",
            {
                "threadId": thread_id,
                "cwd": str(config.workdir),
                "input": [{"type": "text", "text": config.recovery_prompt}],
            },
        )
    )
    for message in client.events():
        if message.get("method") == FILE_APPROVAL_METHOD:
            raise ProtocolViolation("unexpected-file-change-approval")
        if message.get("method") == APPROVAL_METHOD:
            raise ProtocolViolation("unexpected-second-command-approval")
        if len(transcript) >= MAX_PROTOCOL_EVENTS:
            raise ProtocolViolation("app-server-transcript-message-limit")
        transcript.append(message)
        if _turn_completed(message, thread_id, recovery_turn_id, require_success=True):
            break
    else:
        raise ProtocolViolation("missing-authoritative-fresh-turn-completion")
    return _DenialRun(transcript, expectation, recovery_turn_id)


def _expectation_for_request(
    config: LiveProbeConfig, thread_id: str, turn_id: str, message: Mapping[str, object]
) -> DenialExpectation:
    """Bind the first approval request to the known thread/turn and fixed collector identity."""
    params = _mapping(message.get("params"), "approval-params")
    item_id = _required_text(params, "itemId")
    expectation = DenialExpectation(
        thread_id=thread_id,
        turn_id=turn_id,
        item_id=item_id,
        cwd=config.expectation.cwd,
        output_path=config.expectation.output_path,
        command=config.expectation.command,
        network_host=config.expectation.network_host,
        network_protocol=config.expectation.network_protocol,
    )
    _validate_approval(params, expectation)
    return expectation


def _write_evidence_payload(evidence_dir: Path, payload: Mapping[str, object]) -> Path:
    """Atomically publish one sanitized payload inside the disposable evidence root."""
    evidence_dir.mkdir(parents=True, exist_ok=True)
    destination = evidence_dir / "denial-evidence.json"
    temporary = evidence_dir / f".{destination.name}.{os.getpid()}.tmp"
    try:
        temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        os.replace(temporary, destination)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return destination


def _safe_failure_code(error: Exception) -> str:
    """Map an exception to a fixed diagnostic code without retaining its message.

    Example:
        >>> _safe_failure_code(ValueError("private detail"))
        'unexpected-probe-failure'
    """
    if not isinstance(error, ProtocolViolation):
        return "unexpected-probe-failure"
    code = str(error)
    if code in SAFE_FAILURE_CODES:
        return code
    if code in {
        "app-server-request-failed:initialize",
        "app-server-request-failed:thread/start",
        "app-server-request-failed:turn/start",
    }:
        return code
    return "protocol-violation"


def _workspace_changed_after_failure(workdir: Path, initial_snapshot: str | None) -> bool | str:
    """Return a safe tri-state diagnostic after cleanup without exposing filesystem names."""
    if initial_snapshot is None:
        return "unknown"
    try:
        return _workspace_snapshot(workdir) != initial_snapshot
    except (OSError, ProtocolViolation):
        return "unknown"


def _path_present_after_failure(path: Path) -> bool | str:
    """Return whether a negative-output path exists without exposing the path itself."""
    try:
        return path.exists()
    except OSError:
        return "unknown"


def _write_success_evidence(
    evidence_dir: Path,
    result: ValidationResult | None,
    workdir_snapshot: str,
    recorder: _SanitizedEventRecorder,
    summary: _PrimaryTurnSummary,
) -> Path:
    """Publish sanitized passing facts only after successful process cleanup."""
    return _write_evidence_payload(
        evidence_dir,
        {
            "schemaVersion": 1,
            "status": "pass",
            "scenario": summary.scenario.value,
            "primaryTerminalStatus": summary.primary_terminal_status,
            "approvalObserved": summary.approval_observed,
            "commandExecutionObserved": summary.command_execution_observed,
            "errorCategory": summary.error_category,
            "errorCategoriesObserved": sorted(summary.error_categories_observed),
            "retryObserved": summary.retry_observed,
            "willRetry": summary.will_retry,
            "cleanupStatus": "pass",
            "declineResponseCount": len(result.responses) if result is not None else 0,
            "events": recorder.events,
            "observedEventCount": recorder.observed_event_count,
            "eventsTruncated": recorder.events_truncated,
            "recoveryTurnObserved": bool(result and result.recovery_turn_id),
            "workdirSnapshotSha256": workdir_snapshot,
        },
    )


def _write_failure_evidence(
    config: LiveProbeConfig,
    recorder: _SanitizedEventRecorder,
    initial_snapshot: str | None,
    probe_error: Exception | None,
    cleanup_error: Exception | None,
    cleanup_started: bool,
    summary: _PrimaryTurnSummary,
) -> Path:
    """Publish bounded failure shape after the process cleanup attempt completes."""
    failure = probe_error if probe_error is not None else cleanup_error
    assert failure is not None
    return _write_evidence_payload(
        config.evidence_dir,
        {
            "schemaVersion": 1,
            "status": "fail",
            "scenario": summary.scenario.value,
            "primaryTerminalStatus": summary.primary_terminal_status,
            "approvalObserved": summary.approval_observed,
            "commandExecutionObserved": summary.command_execution_observed,
            "errorCategory": summary.error_category,
            "errorCategoriesObserved": sorted(summary.error_categories_observed),
            "retryObserved": summary.retry_observed,
            "willRetry": summary.will_retry,
            "failureCode": _safe_failure_code(failure),
            "cleanupStatus": "fail" if cleanup_error is not None else "pass" if cleanup_started else "not-started",
            "cleanupFailureCode": _safe_failure_code(cleanup_error) if cleanup_error is not None else None,
            "events": recorder.events,
            "observedEventCount": recorder.observed_event_count,
            "eventsTruncated": recorder.events_truncated,
            "outputPresent": _path_present_after_failure(config.expectation.output_path),
            "workdirChanged": _workspace_changed_after_failure(config.workdir, initial_snapshot),
        },
    )


def run_live_probe(
    config: LiveProbeConfig,
    popen: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
) -> Path:
    """Run one operator-authorized, disposable local reviewer wave denial probe over local stdio.

    This function is intentionally not called by tests or CI. Its caller supplies every mutable boundary, including an
    operator-prepared isolated `CODEX_HOME`, and bears the separate model/account authorization. Path isolation and
    candidate identity are enforced; whether that home had prior use remains an operator precondition. The function
    neither inspects credentials nor enables network policy.
    """
    _validate_live_config(config)
    recorder = _SanitizedEventRecorder()
    initial_snapshot: str | None = None
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(config.codex_home)
    if sys.platform == "win32":
        for name in ("SystemRoot", "SYSTEMROOT", "COMSPEC", "PATHEXT", "TEMP", "TMP"):
            value = os.environ.get(name)
            if value is not None:
                environment[name] = value
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if sys.platform == "win32" else 0
    process: subprocess.Popen[str] | None = None
    client: _JsonRpcStdio | None = None
    denial_run: _DenialRun | None = None
    result: ValidationResult | None = None
    summary = _PrimaryTurnSummary(config.scenario)
    probe_error: Exception | None = None
    cleanup_error: Exception | None = None
    try:
        initial_snapshot = _workspace_snapshot(config.workdir)
        process = popen(
            [str(config.codex_bin), "app-server", "--stdio"],
            cwd=config.workdir,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=sys.platform != "win32",
            creationflags=creationflags,
        )
        deadline = time.monotonic() + config.timeout_seconds
        client = _JsonRpcStdio(process, deadline, recorder.record)
        client.request(
            "initialize",
            {"clientInfo": {"name": "codex-rig-denial-probe", "version": "0.8.0"}, "capabilities": {}},
        )
        client.send({"jsonrpc": "2.0", "method": "initialized"})
        thread_result = client.request(
            "thread/start",
            {
                "cwd": str(config.workdir),
                "model": config.model,
                "approvalPolicy": "on-request",
                "sandbox": "read-only",
                "ephemeral": True,
            },
        )
        thread_id = _thread_id(thread_result)
        primary_turn_id = _turn_id(
            client.request(
                "turn/start",
                {
                    "threadId": thread_id,
                    "cwd": str(config.workdir),
                    "input": _primary_input(config),
                },
            )
        )
        if config.scenario != LiveScenario.DENIAL:
            _run_control_turn(client, thread_id, primary_turn_id, recorder, summary)
        else:
            denial_run = _run_denial_turn(client, config, thread_id, primary_turn_id, summary)
        if _workspace_snapshot(config.workdir) != initial_snapshot:
            raise ProtocolViolation("workdir-mutated-during-denial-probe")
        if config.expectation.output_path.exists():
            raise ProtocolViolation("live-output-created-during-probe")
    except Exception as error:
        probe_error = error
    finally:
        if process is not None:
            try:
                terminate_process(process)
            except Exception as error:
                cleanup_error = error

    if probe_error is None and cleanup_error is None and client is not None:
        try:
            unread_messages = client.drain_after_exit()
            if denial_run is not None:
                if len(denial_run.transcript) + len(unread_messages) > MAX_PROTOCOL_EVENTS:
                    raise ProtocolViolation("app-server-transcript-message-limit")
                denial_run.transcript.extend(unread_messages)
                result = validate_transcript(denial_run.transcript, denial_run.expectation)
                if result.recovery_turn_id != denial_run.recovery_turn_id:
                    raise ProtocolViolation("recovery-turn-correlation-drift")
            elif unread_messages:
                raise ProtocolViolation("control-event-after-terminal")
        except Exception as error:
            probe_error = error

    if probe_error is None and cleanup_error is None:
        try:
            assert initial_snapshot is not None
            _validate_live_config(config)
            if _workspace_snapshot(config.workdir) != initial_snapshot:
                raise ProtocolViolation("workdir-mutated-during-denial-probe")
            if config.expectation.output_path.exists():
                raise ProtocolViolation("live-output-created-during-probe")
        except Exception as error:
            probe_error = error

    if probe_error is not None or cleanup_error is not None:
        try:
            _write_failure_evidence(
                config,
                recorder,
                initial_snapshot,
                probe_error,
                cleanup_error,
                process is not None,
                summary,
            )
        except OSError as error:
            raise ProtocolViolation("failure-evidence-write-failed") from error
        if cleanup_error is not None:
            if probe_error is not None:
                raise ProtocolViolation("probe-and-cleanup-failed") from probe_error
            if isinstance(cleanup_error, ProtocolViolation):
                raise cleanup_error
            raise ProtocolViolation("unexpected-probe-failure") from cleanup_error
        assert probe_error is not None
        if isinstance(probe_error, ProtocolViolation):
            raise probe_error
        raise ProtocolViolation("unexpected-probe-failure") from probe_error

    assert initial_snapshot is not None
    try:
        return _write_success_evidence(config.evidence_dir, result, initial_snapshot, recorder, summary)
    except OSError as error:
        raise ProtocolViolation("success-evidence-write-failed") from error


def _distinct_matrix_boundaries(configs: Sequence[LiveProbeConfig]) -> None:
    """Reject reused mutable roots before the ordered paid matrix can start."""
    expected_order = (LiveScenario.TEXT_CONTROL, LiveScenario.SKILL_CONTROL, LiveScenario.DENIAL)
    if tuple(config.scenario for config in configs) != expected_order:
        raise ProtocolViolation("scenario-order-invalid")
    runtime_identities = {
        (
            _normalized_path(config.codex_bin),
            config.model,
            config.plugin_version,
            config.package_sha256,
            config.timeout_seconds,
        )
        for config in configs
    }
    if len(runtime_identities) != 1:
        raise ProtocolViolation("scenario-runtime-identity-drift")
    boundaries = (
        ("codex-home", lambda config: config.codex_home),
        ("workdir", lambda config: config.workdir),
        ("evidence-dir", lambda config: config.evidence_dir),
        ("output-path", lambda config: config.expectation.output_path),
    )
    for _label, get_path in boundaries:
        normalized = [_normalized_path(get_path(config).resolve()) for config in configs]
        if len(set(normalized)) != len(normalized):
            raise ProtocolViolation("scenario-boundaries-reused")
    scenario_paths = [
        tuple(Path(_normalized_path(get_path(config).resolve())) for _label, get_path in boundaries)
        for config in configs
    ]
    for index, paths in enumerate(scenario_paths):
        for later_paths in scenario_paths[index + 1 :]:
            for path in paths:
                for later_path in later_paths:
                    try:
                        path.relative_to(later_path)
                    except ValueError:
                        try:
                            later_path.relative_to(path)
                        except ValueError:
                            continue
                    raise ProtocolViolation("scenario-boundaries-overlap")


def run_live_scenarios(
    configs: Sequence[LiveProbeConfig], run_one: Callable[[LiveProbeConfig], Path] = run_live_probe
) -> tuple[Path, ...]:
    """Run one isolated A-to-B-to-C matrix, stopping before every later scenario on failure."""
    _distinct_matrix_boundaries(configs)
    evidence: list[Path] = []
    for config in configs:
        evidence.append(run_one(config))
    return tuple(evidence)


def _manifest_text(value: object) -> str:
    """Read one non-empty local matrix-manifest text field without echoing it."""
    if not isinstance(value, str) or not value:
        raise ProtocolViolation("live-matrix-manifest-invalid")
    return value


def _config_from_matrix_entry(entry: object) -> LiveProbeConfig:
    """Create one validated run configuration from a disposable local matrix manifest entry."""
    if not isinstance(entry, Mapping):
        raise ProtocolViolation("live-matrix-manifest-invalid")
    try:
        scenario = LiveScenario(_manifest_text(entry.get("scenario")))
        command = _manifest_text(entry.get("command"))
        network_host = entry.get("networkHost")
        network_protocol = entry.get("networkProtocol")
        if not all(value is None or isinstance(value, str) for value in (network_host, network_protocol)):
            raise ProtocolViolation("live-matrix-manifest-invalid")
        if (network_host is None) != (network_protocol is None):
            raise ProtocolViolation("live-matrix-manifest-invalid")
        return LiveProbeConfig(
            codex_bin=Path(_manifest_text(entry.get("codexBin"))),
            codex_home=Path(_manifest_text(entry.get("codexHome"))),
            plugin_root=Path(_manifest_text(entry.get("pluginRoot"))),
            plugin_version=_manifest_text(entry.get("pluginVersion")),
            package_sha256=_manifest_text(entry.get("packageSha256")),
            workdir=Path(_manifest_text(entry.get("workdir"))),
            model=_manifest_text(entry.get("model")),
            prompt=_manifest_text(entry.get("prompt")),
            evidence_dir=Path(_manifest_text(entry.get("evidenceDir"))),
            expectation=DenialExpectation(
                thread_id="pending",
                turn_id="pending",
                item_id="pending",
                cwd=Path(_manifest_text(entry.get("cwd"))),
                output_path=Path(_manifest_text(entry.get("outputPath"))),
                command=command,
                network_host=network_host,
                network_protocol=network_protocol,
            ),
            recovery_prompt=_manifest_text(entry.get("recoveryPrompt")),
            timeout_seconds=float(entry.get("timeoutSeconds")),
            scenario=scenario,
        )
    except (TypeError, ValueError) as error:
        raise ProtocolViolation("live-matrix-manifest-invalid") from error


def _configs_from_matrix_manifest(manifest_path: Path) -> tuple[LiveProbeConfig, ...]:
    """Load three local disposable entries without retaining their raw prompt or path data."""
    try:
        manifest = _mapping(json.loads(manifest_path.read_text(encoding="utf-8")), "live-matrix-manifest")
        entries = manifest.get("scenarios")
        if not isinstance(entries, list):
            raise ProtocolViolation("live-matrix-manifest-invalid")
        return tuple(_config_from_matrix_entry(entry) for entry in entries)
    except (OSError, json.JSONDecodeError) as error:
        raise ProtocolViolation("live-matrix-manifest-unreadable") from error


def _parse_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse either a local synthetic transcript or explicit disposable live probe."""
    parser = argparse.ArgumentParser(description="Fail-closed Codex local reviewer wave denial transcript validator")
    parser.add_argument("--live", action="store_true", help="Run the separately authorized disposable stdio probe")
    parser.add_argument(
        "--live-matrix",
        type=Path,
        help="Run one ordered A-to-B-to-C disposable matrix from a local three-scenario manifest",
    )
    parser.add_argument(
        "--scenario",
        choices=tuple(scenario.value for scenario in LiveScenario),
        default=LiveScenario.DENIAL.value,
        help="Live scenario; defaults to the existing command-denial probe",
    )
    parser.add_argument("--transcript", type=Path, help="Local JSONL protocol transcript")
    parser.add_argument("--thread-id")
    parser.add_argument("--turn-id")
    parser.add_argument("--item-id")
    parser.add_argument("--cwd", type=Path)
    parser.add_argument("--output-path", type=Path)
    parser.add_argument("--command")
    parser.add_argument("--network-host")
    parser.add_argument("--network-protocol")
    parser.add_argument("--codex-bin", type=Path)
    parser.add_argument("--codex-home", type=Path)
    parser.add_argument("--plugin-root", type=Path)
    parser.add_argument("--plugin-version")
    parser.add_argument("--package-sha256")
    parser.add_argument("--workdir", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--prompt")
    parser.add_argument("--recovery-prompt")
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the selected local transcript validator or explicitly authorized live probe."""
    arguments = _parse_arguments(argv)
    if (arguments.network_host is None) != (arguments.network_protocol is None):
        raise ProtocolViolation("network-host-and-protocol-must-be-provided-together")
    if arguments.live_matrix is not None:
        if arguments.live:
            raise ProtocolViolation("live-and-live-matrix-are-mutually-exclusive")
        evidence = run_live_scenarios(_configs_from_matrix_manifest(arguments.live_matrix))
        print(json.dumps({"status": "pass", "evidence": [path.name for path in evidence]}))
        return 0
    if arguments.live:
        required = {
            "codex-bin": arguments.codex_bin,
            "codex-home": arguments.codex_home,
            "plugin-root": arguments.plugin_root,
            "plugin-version": arguments.plugin_version,
            "package-sha256": arguments.package_sha256,
            "workdir": arguments.workdir,
            "model": arguments.model,
            "evidence-dir": arguments.evidence_dir,
            "cwd": arguments.cwd,
            "output-path": arguments.output_path,
        }
        if arguments.scenario == LiveScenario.DENIAL.value:
            required.update({"prompt": arguments.prompt, "recovery-prompt": arguments.recovery_prompt})
            required["command"] = arguments.command
        missing = [name for name, value in required.items() if value is None]
        if missing:
            raise ProtocolViolation(f"live-arguments-missing:{','.join(missing)}")
        evidence_path = run_live_probe(
            LiveProbeConfig(
                codex_bin=arguments.codex_bin,
                codex_home=arguments.codex_home,
                plugin_root=arguments.plugin_root,
                plugin_version=arguments.plugin_version,
                package_sha256=arguments.package_sha256,
                workdir=arguments.workdir,
                model=arguments.model,
                prompt=arguments.prompt or "",
                evidence_dir=arguments.evidence_dir,
                expectation=DenialExpectation(
                    thread_id="pending",
                    turn_id="pending",
                    item_id="pending",
                    cwd=arguments.cwd,
                    output_path=arguments.output_path,
                    command=arguments.command or "",
                    network_host=arguments.network_host,
                    network_protocol=arguments.network_protocol,
                ),
                recovery_prompt=arguments.recovery_prompt or "",
                timeout_seconds=arguments.timeout_seconds,
                scenario=LiveScenario(arguments.scenario),
            )
        )
        print(json.dumps({"status": "pass", "evidence": evidence_path.name}))
        return 0
    if (
        arguments.transcript is None
        or not all((arguments.thread_id, arguments.turn_id, arguments.item_id))
        or arguments.cwd is None
        or arguments.output_path is None
        or arguments.command is None
    ):
        raise ProtocolViolation("synthetic-transcript-thread-id-turn-id-and-item-id-are-required")
    try:
        messages = tuple(
            json.loads(line) for line in arguments.transcript.read_text(encoding="utf-8").splitlines() if line
        )
    except (OSError, json.JSONDecodeError) as error:
        raise ProtocolViolation("unreadable-or-malformed-local-transcript") from error
    result = validate_transcript(
        messages,
        DenialExpectation(
            thread_id=arguments.thread_id,
            turn_id=arguments.turn_id,
            item_id=arguments.item_id,
            cwd=arguments.cwd,
            output_path=arguments.output_path,
            command=arguments.command,
            network_host=arguments.network_host,
            network_protocol=arguments.network_protocol,
        ),
    )
    print(
        json.dumps(
            {
                "responses": result.responses,
                "events": result.sanitized_events,
                "recoveryTurnId": result.recovery_turn_id,
            }
        )
    )
    return 0


def _cli(argv: Sequence[str] | None = None) -> int:
    """Render only a fixed safe failure code at the executable boundary."""
    try:
        return main(argv)
    except ProtocolViolation as error:
        print(f"denial-protocol-failed:{_safe_failure_code(error)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_cli())
