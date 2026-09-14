"""Run, supervise, and normalize one bridge request.

Purpose: Provide the single portable process boundary used by bridge skills and the MCP server. It turns an implement,
advise, or review request into a Codex or Claude child command, captures the child output, and returns one compact JSON
object rather than a transcript. Scope: This module owns argument construction, bounded foreground and detached
execution, JSONL parsing, contract validation, job records, transcript and incident files, and health logging. It
deliberately does not discover models, edit manifests, or implement a provider registry. Usage: Run ``bridge_call.py``
with the ``implement`` action and ``--task`` for a foreground request, or add ``--background`` and later use ``status``,
``result``, or ``cancel`` with the returned job id. ``bridge_mcp.py`` imports ``run_request`` for the reverse direction.
Outputs: Every command writes exactly one JSON object to stdout; artifacts live beneath ``<workspace>/.temp/bridge``.
Failure: Invalid requests, invalid model output, unavailable executables, timeouts, and structured child faults become a
validated result envelope and an incident when appropriate. Used by: Claude-facing bridge skills, the Codex-facing stdio
MCP server, and bridge diagnostics. The implementation supports Python 3.10+, uses Python's standard library, and uses
native ``taskkill`` process-tree control on Windows. Every termination path performs a bounded leader wait to collect
its actual exit status; unavailable status is diagnosed without inventing a return code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field as dataclass_field
from typing import Any, Iterable, Mapping


DEFAULT_MODEL: str | None = None
DEFAULT_EFFORT = "medium"
DEFAULT_TIMEOUTS = {"implement": 600.0, "advise": 120.0, "review": 300.0}
CORE_FIELDS = ("status", "verdict", "findings", "files_touched", "remaining", "blockers")
PEER_FIELDS = (*CORE_FIELDS, "details")
CORE_STATUSES = {"complete", "partial", "blocked"}
FINAL_STATUSES = CORE_STATUSES | {"timeout", "refused"}
EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max")
DEPTH_ENVIRONMENT_VARIABLE = "CC_CODEX_BRIDGE_DEPTH"
CHILD_TIMEOUT_MULTIPLIER = 1.2
MAX_VERDICT_CHARS = 500
MAX_SUMMARY_ITEMS = 8
MAX_SUMMARY_ITEM_CHARS = 500
MAX_DETAILS_ITEMS = 32
MAX_DETAILS_ITEM_CHARS = 2_000
# This leaves substantial headroom below the smallest audited process argument
# limit after the Bridge prompt, schema, environment, and executable arguments.
MAX_TASK_UTF8_BYTES = 16 * 1024
MAX_PROMPT_UTF8_BYTES = 20 * 1024
MAX_WINDOWS_COMMAND_UTF16_UNITS = 32_767
MAX_WINDOWS_BATCH_COMMAND_UTF16_UNITS = 8_000
MAX_CHILD_RECORD_BYTES = 256 * 1024
MAX_CHILD_TRANSCRIPT_BYTES = 256 * 1024
MAX_CHILD_OUTPUT_BYTES = MAX_CHILD_TRANSCRIPT_BYTES - len(b"stdout:\n\n\nstderr:\n\n")

# Environment passed to a bridge child, as an allowlist. Inheriting the parent
# environment wholesale hands every API key, cloud credential, and token in the
# session to the child; a bridge child needs its own provider auth and little
# else. Anything absent from these three sets is dropped.
#
# Base process needs, portable across hosts.
CHILD_ENV_BASE_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR", "USER", "SHELL")
# Windows requires these or a child Python aborts during interpreter start-up
# with `_Py_HashRandomization_Init: failed to get random numbers`. SystemRoot in
# both spellings because the OS is case-insensitive and callers differ.
CHILD_ENV_WINDOWS_KEYS = ("SystemRoot", "SYSTEMROOT", "COMSPEC", "PATHEXT", "TEMP", "TMP", "USERPROFILE")
# Provider auth and config discovery for the two supported children. Without
# these the child cannot authenticate and every call fails closed.
CHILD_ENV_PROVIDER_KEYS = (
    "CODEX_HOME",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_CODE_OAUTH_TOKEN",
)
# Operator escape hatch: a comma-separated list of additional variable names to
# let through. The fixed sets above assume a direct-API-key login on a host with
# open egress; a corporate proxy (`HTTPS_PROXY`, `NO_PROXY`, `SSL_CERT_FILE`) or
# an enterprise auth path (`CLAUDE_CODE_USE_BEDROCK`, `AWS_PROFILE`,
# `GOOGLE_APPLICATION_CREDENTIALS`) needs names no fixed list can anticipate.
# Without this, such a host cannot use the bridge at all without editing plugin
# source, and the failure surfaces as an opaque provider error inside the child.
# Only the parent process can set this, so it widens nothing a caller controls.
CHILD_ENV_EXTRA_VARIABLE = "BRIDGE_CHILD_ENV_EXTRA"
_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ArtifactBoundaryError(ValueError):
    """Report a rejected workspace artifact member without exposing its resolved destination."""


@dataclass(frozen=True)
class BridgePaths:
    """Contain paths for one workspace-local bridge artifact store."""

    workspace: Path
    artifact_root: Path = dataclass_field(init=False)

    def __post_init__(self) -> None:
        """Resolve and contain the artifact root before any bridge artifact operation."""
        workspace = self.workspace.resolve()
        artifact_root = (workspace / ".temp" / "bridge").resolve()
        try:
            artifact_root.relative_to(workspace)
        except ValueError as error:
            raise ValueError("bridge artifact root escapes the trusted workspace") from error
        object.__setattr__(self, "workspace", workspace)
        object.__setattr__(self, "artifact_root", artifact_root)

    @property
    def root(self) -> Path:
        """Return the workspace-local bridge artifact directory."""
        return self.artifact_root

    @property
    def jobs(self) -> Path:
        """Return the detached-job directory."""
        return self.root / "jobs"

    @property
    def incidents(self) -> Path:
        """Return the incident-record directory."""
        return self.root / "incidents"

    def prepare(self) -> None:
        """Create all bridge artifact directories for a request."""
        for directory in (self.root, self.jobs, self.incidents):
            directory.mkdir(parents=True, exist_ok=True)

    def relative(self, path: Path) -> str:
        """Return a stable workspace-relative artifact path when possible."""
        try:
            return path.relative_to(self.workspace).as_posix()
        except ValueError:
            return path.as_posix()


@dataclass(frozen=True)
class Request:
    """Represent one normalized bridge request before child dispatch."""

    verb: str
    task: str
    model: str | None
    effort: str
    timeout_seconds: float
    depth: int
    run_id: str
    workspace: Path
    direction: str
    background: bool = False
    session_id: str | None = None
    origin_workspace: Path | None = None
    supported_efforts: tuple[str, ...] = ()


def validate_request_transport_budget(request: Request) -> None:
    """Reject task text whose encoded prompt cannot safely fit the local CLI transport."""
    if len(request.task.encode("utf-8")) > MAX_TASK_UTF8_BYTES:
        raise ValueError("task exceeds Bridge's encoded transport budget")
    if len(_prompt_with_budget(request).encode("utf-8")) > MAX_PROMPT_UTF8_BYTES:
        raise ValueError("task exceeds Bridge's encoded transport budget")
    schema_path = Path(__file__).resolve().parents[1] / "schemas" / "envelope.schema.json"
    command = (
        build_claude_argv(request, schema_path)
        if request.direction == "codex_to_claude"
        else build_codex_argv(request, schema_path)
    )
    resolved_command = _resolved_command(command)
    units = _windows_command_utf16_units(resolved_command)
    if units > MAX_WINDOWS_COMMAND_UTF16_UNITS or (
        _is_windows_batch_shim(resolved_command[0]) and units > MAX_WINDOWS_BATCH_COMMAND_UTF16_UNITS
    ):
        raise ValueError("task exceeds Bridge's encoded transport budget")


def _windows_command_utf16_units(command: list[str]) -> int:
    """Measure the Windows command line generated from one argv list, including its terminator."""
    return len(subprocess.list2cmdline(command).encode("utf-16-le")) // 2 + 1


def _is_windows_batch_shim(executable: str) -> bool:
    """Identify a resolved Windows command shim whose Cmd.exe limit is lower than CreateProcess."""
    return Path(executable).suffix.lower() in {".bat", ".cmd"}


def validate_model_core(value: Any) -> dict[str, Any]:
    """Validate and return the strict peer result before public compaction.

    Peer results include bounded supporting ``details`` for transcript storage. The supervisor removes that field before
    it validates and returns the public envelope, keeping the model boundary compact without discarding evidence.
    """
    if not isinstance(value, dict) or set(value) != set(PEER_FIELDS):
        raise ValueError("model result must contain exactly the seven peer fields")
    if value["status"] not in CORE_STATUSES:
        raise ValueError("model result has an unsupported status")
    _validate_result_summary(value)
    _validate_string_list(value["details"], "details", MAX_DETAILS_ITEMS, MAX_DETAILS_ITEM_CHARS)
    return value


def _public_core(value: dict[str, Any]) -> dict[str, Any]:
    """Remove peer-only detail before the compact public-envelope boundary."""
    return {field: value[field] for field in CORE_FIELDS}


def _validate_result_summary(value: dict[str, Any]) -> None:
    """Enforce compact public-result bounds shared by peer and envelope validation."""
    verdict = value["verdict"]
    if not isinstance(verdict, str) or not verdict.strip() or len(verdict) > MAX_VERDICT_CHARS:
        raise ValueError("model result verdict must be a bounded non-empty string")
    for field in CORE_FIELDS[2:]:
        _validate_string_list(value[field], field, MAX_SUMMARY_ITEMS, MAX_SUMMARY_ITEM_CHARS)


def _validate_string_list(value: Any, field: str, maximum_items: int, maximum_item_chars: int) -> None:
    """Reject unbounded or non-string model-authored list fields."""
    if not isinstance(value, list) or len(value) > maximum_items:
        raise ValueError(f"model result {field} must be an array within its item limit")
    if not all(isinstance(item, str) and len(item) <= maximum_item_chars for item in value):
        raise ValueError(f"model result {field} must contain bounded strings")


def validate_envelope(value: Any) -> dict[str, Any]:
    """Validate and return the public harness-enriched result envelope."""
    required = {
        *CORE_FIELDS,
        "model",
        "effort",
        "effort_substituted",
        "cost",
        "tokens",
        "duration_seconds",
        "depth",
        "run_id",
        "incident",
        "session_id",
        "transcript_path",
        "verb",
        "direction",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("public envelope has missing or unknown fields")
    if value["status"] not in FINAL_STATUSES:
        raise ValueError("public envelope has an unsupported status")
    if value["verb"] not in DEFAULT_TIMEOUTS or value["direction"] not in {
        "claude_to_codex",
        "codex_to_claude",
    }:
        raise ValueError("public envelope has unsupported routing metadata")
    if isinstance(value["depth"], bool) or not isinstance(value["depth"], int) or value["depth"] < 0:
        raise ValueError("public envelope depth must be a non-negative integer")
    if (
        isinstance(value["duration_seconds"], bool)
        or not isinstance(value["duration_seconds"], (int, float))
        or value["duration_seconds"] < 0
    ):
        raise ValueError("public envelope duration_seconds must be non-negative")
    if value["cost"] is not None and (
        isinstance(value["cost"], bool) or not isinstance(value["cost"], (int, float)) or value["cost"] < 0
    ):
        raise ValueError("public envelope cost must be null or non-negative")
    if not isinstance(value["tokens"], dict) or not all(
        not isinstance(token, bool) and isinstance(token, (int, float)) and token >= 0
        for token in value["tokens"].values()
    ):
        raise ValueError("public envelope tokens must be a mapping of non-negative numbers")
    for field in ("model", "effort", "run_id", "transcript_path"):
        if not isinstance(value[field], str) or not value[field]:
            raise ValueError(f"public envelope {field} must be a non-empty string")
    for field in ("incident", "session_id"):
        if value[field] is not None and not isinstance(value[field], str):
            raise ValueError(f"public envelope {field} must be a string or null")
    substitution = value["effort_substituted"]
    if substitution is not None and (
        not isinstance(substitution, dict)
        or set(substitution) != {"requested", "applied", "reason"}
        or not all(isinstance(item, str) and item for item in substitution.values())
    ):
        raise ValueError("effort_substituted must be null or a complete substitution record")
    core = {field: value[field] for field in CORE_FIELDS}
    if value["status"] in CORE_STATUSES:
        _validate_result_summary(core)
    elif not isinstance(value["verdict"], str) or not value["verdict"]:
        raise ValueError("harness terminal result needs a verdict")
    return value


def build_codex_argv(request: Request, schema_path: Path) -> list[str]:
    """Build a verified local Codex CLI command for a bridge request."""
    quoted_effort = json.dumps(request.effort)
    prompt = _prompt_with_budget(request)
    common = [
        "-c",
        f"model_reasoning_effort={quoted_effort}",
        "-c",
        'approval_policy="never"',
        # Egress is denied explicitly rather than inherited. `workspace-write`
        # decides network access from `sandbox_workspace_write.network_access`,
        # so leaving it unset makes the bridge's egress posture a property of
        # whatever Codex defaults to in the installed version. Pinning it keeps
        # the child unable to send workspace contents anywhere, which is the
        # cheapest capability to remove: it still reads code and writes files.
        "-c",
        "sandbox_workspace_write.network_access=false",
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--json",
        "--output-schema",
        str(schema_path),
    ]
    if request.model:
        common[0:0] = ["-m", request.model]
    if request.session_id:
        if not _is_write_verb(request.verb):
            raise ValueError("only implement may resume a Codex session")
        if request.origin_workspace is None or request.origin_workspace.resolve() != request.workspace.resolve():
            raise ValueError("resumed implement must use the originating workspace")
        return [
            "codex",
            "exec",
            "resume",
            request.session_id,
            prompt,
            *common,
            "-c",
            f"sandbox_mode={json.dumps('workspace-write')}",
        ]
    sandbox = "workspace-write" if _is_write_verb(request.verb) else "read-only"
    command = ["codex", "exec", *common, "-s", sandbox]
    if not _is_write_verb(request.verb):
        command.append("--ephemeral")
    return [*command, prompt]


def build_claude_argv(request: Request, schema_path: Path) -> list[str]:
    """Build the Claude print-mode command with its compatible peer schema."""
    effort, _ = normalize_effort(request.effort, "claude", request.supported_efforts)
    permission = "acceptEdits" if _is_write_verb(request.verb) else "plan"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema.pop("$schema", None)
    command = [
        "claude",
        "--print",
        _prompt_with_budget(request),
        "--effort",
        effort,
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(schema, sort_keys=True),
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--permission-mode",
        permission,
    ]
    if request.model:
        command[2:2] = ["--model", request.model]
    if not _is_write_verb(request.verb):
        command.extend(["--disallowed-tools", "Edit", "Write"])
    return command


def normalize_effort(
    requested: str, host: str, supported_efforts: Iterable[str] = ()
) -> tuple[str, dict[str, str] | None]:
    """Normalize a requested effort level and record one deterministic downgrade."""
    aliases = {"trivial": "minimal", "none": "minimal"}
    requested = aliases.get(requested, requested)
    if requested not in EFFORTS:
        raise ValueError(f"unsupported effort level: {requested}")
    host_value = "low" if host == "claude" and requested == "minimal" else requested
    supported = tuple(supported_efforts)
    if not supported or host_value in supported:
        return host_value, None
    positions = {level: index for index, level in enumerate(EFFORTS)}
    viable = [level for level in supported if level in positions and positions[level] <= positions.get(host_value, 0)]
    if not viable:
        raise ValueError(f"no supported effort at or below {host_value}")
    applied = max(viable, key=positions.__getitem__)
    return applied, {"requested": requested, "applied": applied, "reason": "target model capability"}


def _is_write_verb(verb: str) -> bool:
    """Identify the write-capable implement verb."""
    return verb == "implement"


def run_request(
    request: Request,
    *,
    host: str = "codex",
    _attempt: int = 0,
    _recovery: dict[str, str] | None = None,
    _prior_incident: str | None = None,
    _job_path: Path | None = None,
) -> dict[str, Any]:
    """Run one foreground request and return a fully validated public envelope."""
    if not math.isfinite(request.timeout_seconds) or request.timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be a finite positive number")
    validate_request_transport_budget(request)
    paths = BridgePaths(request.workspace.resolve())
    paths.prepare()
    try:
        request = _request_with_trusted_depth(request)
    except ValueError as error:
        # A corrupted depth variable or negative caller depth must surface as
        # a structured result, not as an opaque transport error; the envelope
        # itself needs a valid depth, so the rejected value is clamped there.
        reportable = Request(**{**request.__dict__, "depth": max(request.depth, 0)})
        return _terminal_envelope(reportable, paths, "blocked", str(error), None, {}, None, 0.0)
    if request.depth >= 1:
        return _terminal_envelope(request, paths, "refused", "recursion-depth", None, {}, None, 0.0)
    try:
        effective_effort, substitution = normalize_effort(request.effort, host, request.supported_efforts)
    except ValueError as error:
        return _terminal_envelope(request, paths, "blocked", str(error), None, {}, None, 0.0)
    effective_request = Request(**{**request.__dict__, "effort": effective_effort})
    schema_path = Path(__file__).resolve().parents[1] / "schemas" / "envelope.schema.json"
    command = (
        build_codex_argv(effective_request, schema_path)
        if host == "codex"
        else build_claude_argv(effective_request, schema_path)
    )
    before_delta = _workspace_state(effective_request.workspace) if _is_write_verb(effective_request.verb) else []
    started = time.monotonic()
    if _job_path is None:
        outcome = _run_child(
            command, effective_request.workspace, effective_request.timeout_seconds * CHILD_TIMEOUT_MULTIPLIER
        )
    else:
        if _job_cancel_requested(_job_path):
            return _terminal_envelope(
                effective_request, paths, "blocked", "cancelled by job owner", None, {}, None, 0.0
            )
        outcome = _run_child(
            command,
            effective_request.workspace,
            effective_request.timeout_seconds * CHILD_TIMEOUT_MULTIPLIER,
            _job_path,
        )
    duration = time.monotonic() - started
    transcript = _write_transcript(paths, outcome.stdout, outcome.stderr)
    event_info = _parse_output(outcome.stdout, host)
    if outcome.output_limited:
        return _terminal_envelope(
            effective_request,
            paths,
            "blocked",
            outcome.error or "child output exceeded the Bridge capture limit",
            transcript,
            event_info.tokens,
            event_info.session_id,
            duration,
            _recovery or substitution,
            fault="output-limit",
            workspace_delta=(
                _workspace_delta(before_delta, effective_request.workspace)
                if _is_write_verb(effective_request.verb)
                else None
            ),
            prior_incident=_prior_incident,
        )
    if outcome.timed_out:
        retry_effort = _lower_effort(effective_request.effort, effective_request.supported_efforts)
        if _attempt == 0 and effective_request.verb in {"advise", "review"} and retry_effort is not None:
            recovery = {"requested": effective_request.effort, "applied": retry_effort, "reason": "timeout retry"}
            incident = _write_incident(
                paths, effective_request, "timeout", "hard cutoff reached; retrying once", transcript, None
            )
            retry = Request(**{**effective_request.__dict__, "effort": retry_effort})
            return run_request(
                retry, host=host, _attempt=1, _recovery=recovery, _prior_incident=incident, _job_path=_job_path
            )
        return _terminal_envelope(
            effective_request,
            paths,
            "timeout",
            "hard cutoff reached",
            transcript,
            event_info.tokens,
            event_info.session_id,
            duration,
            _recovery or substitution,
            workspace_delta=(
                _workspace_delta(before_delta, effective_request.workspace)
                if _is_write_verb(effective_request.verb)
                else None
            ),
            prior_incident=_prior_incident,
        )
    if outcome.error:
        return _terminal_envelope(
            effective_request,
            paths,
            "blocked",
            outcome.error,
            transcript,
            event_info.tokens,
            event_info.session_id,
            duration,
            _recovery or substitution,
            workspace_delta=(
                _workspace_delta(before_delta, effective_request.workspace)
                if _is_write_verb(effective_request.verb)
                else None
            ),
            prior_incident=_prior_incident,
        )
    if outcome.returncode != 0:
        reason = event_info.error or f"child exited with code {outcome.returncode}"
        retry_effort = _lower_effort(effective_request.effort, effective_request.supported_efforts)
        # The stale-capability retry stays read-only: a failed write-capable
        # child may already have landed edits, so rerunning it can duplicate
        # or conflict with work that reached the worktree.
        if (
            _attempt == 0
            and not _is_write_verb(effective_request.verb)
            and _is_effort_failure(reason)
            and retry_effort is not None
        ):
            recovery = {
                "requested": effective_request.effort,
                "applied": retry_effort,
                "reason": "structured unsupported effort",
            }
            incident = _write_incident(paths, effective_request, "unsupported-effort", reason, transcript, None)
            retry = Request(**{**effective_request.__dict__, "effort": retry_effort})
            return run_request(
                retry, host=host, _attempt=1, _recovery=recovery, _prior_incident=incident, _job_path=_job_path
            )
        return _terminal_envelope(
            effective_request,
            paths,
            "blocked",
            reason,
            transcript,
            event_info.tokens,
            event_info.session_id,
            duration,
            _recovery or substitution,
            prior_incident=_prior_incident,
        )
    if event_info.core is None and event_info.error:
        # A zero exit can still carry a structured provider failure; surface
        # that cause instead of a generic invalid-model-result message.
        return _terminal_envelope(
            effective_request,
            paths,
            "blocked",
            event_info.error,
            transcript,
            event_info.tokens,
            event_info.session_id,
            duration,
            _recovery or substitution,
            prior_incident=_prior_incident,
        )
    try:
        core = _public_core(validate_model_core(event_info.core))
    except ValueError as error:
        return _terminal_envelope(
            effective_request,
            paths,
            "blocked",
            f"invalid model result: {error}",
            transcript,
            event_info.tokens,
            event_info.session_id,
            duration,
            _recovery or substitution,
            prior_incident=_prior_incident,
        )
    envelope = _make_envelope(
        effective_request,
        core,
        transcript,
        event_info.tokens,
        event_info.cost,
        event_info.session_id if _is_write_verb(effective_request.verb) else None,
        duration,
        _recovery or substitution,
        _prior_incident,
    )
    try:
        _append_health(paths, envelope)
    except ArtifactBoundaryError as error:
        return _terminal_envelope(
            effective_request,
            paths,
            "blocked",
            str(error),
            transcript,
            event_info.tokens,
            event_info.session_id,
            duration,
            _recovery or substitution,
            record_health=False,
        )
    return envelope


def _lower_effort(current: str, supported_efforts: tuple[str, ...]) -> str | None:
    """Return one bounded lower effort level for a timeout or unsupported-effort retry.

    A retry keeps the original soft budget, so the only recovery with a real chance of finishing inside that budget is a
    cheaper attempt; escalating effort after a timeout would spend longer thinking against the same clock.
    """
    allowed = tuple(level for level in EFFORTS if not supported_efforts or level in supported_efforts)
    try:
        index = allowed.index(current)
    except ValueError:
        return None
    return allowed[index - 1] if index else None


def _is_effort_failure(reason: str) -> bool:
    """Recognize only structured effort failures eligible for one recovery attempt."""
    lowered = reason.lower()
    return "reasoning.effort" in lowered or "unsupported_value" in lowered or "invalid_enum_value" in lowered


@dataclass(frozen=True)
class ChildOutcome:
    """Capture child completion data without exposing a process object."""

    stdout: str
    stderr: str
    returncode: int | None
    timed_out: bool
    error: str | None
    output_limited: bool = False


@dataclass(frozen=True)
class ParsedOutput:
    """Contain model core, observed usage, session id, and structured error."""

    core: Any
    tokens: dict[str, float]
    cost: float | None
    session_id: str | None
    error: str | None


@dataclass
class _ChildOutputBuffer:
    """Retain bounded child output while compacting completed Codex command events."""

    stdout_parts: list[bytes] = dataclass_field(default_factory=list)
    stderr_parts: list[bytes] = dataclass_field(default_factory=list)
    pending_stdout: bytearray = dataclass_field(default_factory=bytearray)
    captured_bytes: int = 0
    output_limited: threading.Event = dataclass_field(default_factory=threading.Event)
    finalized: threading.Event = dataclass_field(default_factory=threading.Event)
    lock: threading.Lock = dataclass_field(default_factory=threading.Lock)

    def append(self, stream: str, chunk: bytes) -> None:
        """Compact complete stdout records and enforce the retained-output budget."""
        with self.lock:
            if self.finalized.is_set():
                return
            if stream == "stderr":
                self._retain(stream, chunk)
            else:
                self.pending_stdout.extend(chunk)
                while (newline := self.pending_stdout.find(b"\n")) >= 0:
                    line = bytes(self.pending_stdout[: newline + 1])
                    del self.pending_stdout[: newline + 1]
                    # Enforce the raw per-record ceiling before a compactable
                    # command payload can disappear from retained output.
                    if len(line) > MAX_CHILD_RECORD_BYTES:
                        self.output_limited.set()
                        self._retain("stdout", line)
                        return
                    self._retain("stdout", _compact_command_event(line))
                    if self.output_limited.is_set():
                        return
            # One oversized record remains terminal; parsing it would remove
            # the per-record memory bound even when its tool output is expendable.
            if len(self.pending_stdout) > MAX_CHILD_RECORD_BYTES:
                self.output_limited.set()
            # Object fields can arrive in any order. Candidate records use the
            # independent raw-record budget until complete validation or final flush.
            candidate = self.pending_stdout.lstrip().startswith(b"{")
            if not candidate and self.captured_bytes + len(self.pending_stdout) > MAX_CHILD_OUTPUT_BYTES:
                self.output_limited.set()

    def _retain(self, stream: str, chunk: bytes) -> None:
        """Keep at most the shared byte budget and signal when a chunk exceeds it."""
        remaining = MAX_CHILD_OUTPUT_BYTES - self.captured_bytes
        accepted = chunk[: max(remaining, 0)]
        if accepted:
            target = self.stdout_parts if stream == "stdout" else self.stderr_parts
            target.append(accepted)
            self.captured_bytes += len(accepted)
        if len(chunk) > len(accepted):
            self.output_limited.set()

    def text(self) -> tuple[str, str]:
        """Flush a final unterminated stdout record and decode bounded output."""
        with self.lock:
            # An inherited pipe can outlive its cleanup grace. Freeze capture
            # before taking the snapshot so a late reader cannot alter its fault.
            self.finalized.set()
            if self.pending_stdout:
                self._retain("stdout", _compact_command_event(bytes(self.pending_stdout)))
                self.pending_stdout.clear()
            decoded = []
            remaining = MAX_CHILD_OUTPUT_BYTES
            for parts in (self.stdout_parts, self.stderr_parts):
                encoded = b"".join(parts).decode("utf-8", errors="replace").encode("utf-8")
                if len(encoded) > remaining:
                    self.output_limited.set()
                # Replacement characters can expand malformed bytes; count the
                # actual transcript encoding and never cut through a code point.
                value = encoded[:remaining].decode("utf-8", errors="ignore")
                decoded.append(value)
                remaining -= len(value.encode("utf-8"))
            return decoded[0], decoded[1]


def _compact_command_event(line: bytes) -> bytes:
    """Replace completed Codex command output with its UTF-8 size and content digest."""
    if b'"aggregated_output"' not in line:
        return line
    try:
        record = json.loads(line)
    except (ValueError, RecursionError):
        return line
    if not isinstance(record, dict) or record.get("type") != "item.completed":
        return line
    item = record.get("item")
    if not isinstance(item, dict) or item.get("type") != "command_execution":
        return line
    command_output = item.get("aggregated_output")
    if not isinstance(command_output, str):
        return line
    try:
        output_bytes = command_output.encode("utf-8")
    except UnicodeEncodeError:
        return line
    item = {
        **item,
        "aggregated_output_bytes": len(output_bytes),
        "aggregated_output_sha256": hashlib.sha256(output_bytes).hexdigest(),
    }
    del item["aggregated_output"]
    newline = "\n" if line.endswith(b"\n") else ""
    try:
        return (json.dumps({**record, "item": item}, separators=(",", ":")) + newline).encode("utf-8")
    except RecursionError:
        return line


def build_child_environment(source: Mapping[str, str] | None = None, platform: str | None = None) -> dict[str, str]:
    """Build the child environment from an allowlist instead of inheriting the parent's.

    Only keys named in :data:`CHILD_ENV_BASE_KEYS`, :data:`CHILD_ENV_PROVIDER_KEYS`, on Windows
    :data:`CHILD_ENV_WINDOWS_KEYS`, and any the operator names in
    :data:`CHILD_ENV_EXTRA_VARIABLE` survive. Keys absent from ``source`` are omitted rather than
    set empty, so the child cannot tell a dropped variable from an unset one.

    Args:
        source: Environment to filter. Defaults to :data:`os.environ`.
        platform: Platform name to decide the Windows key set. Defaults to :data:`sys.platform`.

    Returns:
        Filtered environment mapping, without the bridge depth marker (the caller adds it).

    Examples:
        >>> build_child_environment({"PATH": "/bin", "AWS_SECRET_ACCESS_KEY": "s"}, platform="linux")
        {'PATH': '/bin'}
        >>> build_child_environment({"OPENAI_API_KEY": "k"}, platform="linux")
        {'OPENAI_API_KEY': 'k'}
        >>> sorted(build_child_environment({"COMSPEC": "c", "PATH": "p"}, platform="win32"))
        ['COMSPEC', 'PATH']
        >>> sorted(build_child_environment({"COMSPEC": "c", "PATH": "p"}, platform="linux"))
        ['PATH']
        >>> extra = {"BRIDGE_CHILD_ENV_EXTRA": "HTTPS_PROXY, NO_PROXY", "HTTPS_PROXY": "p", "OTHER": "x"}
        >>> sorted(build_child_environment(extra, platform="linux"))
        ['HTTPS_PROXY']
    """
    env = os.environ if source is None else source
    active = sys.platform if platform is None else platform
    allowed = [*CHILD_ENV_BASE_KEYS, *CHILD_ENV_PROVIDER_KEYS]
    if active == "win32":
        allowed.extend(CHILD_ENV_WINDOWS_KEYS)
    allowed.extend(_extra_child_environment_keys(env))
    return {key: env[key] for key in allowed if key in env}


def _extra_child_environment_keys(env: Mapping[str, str]) -> list[str]:
    """Parse the operator's additional-variable list into names.

    Malformed entries are skipped rather than raising: the list is host configuration read on every
    call, and one typo must not take the bridge down. The marker variable itself is never forwarded,
    so the child cannot read or re-widen the policy that produced its own environment.

    Args:
        env: Environment to read :data:`CHILD_ENV_EXTRA_VARIABLE` from.

    Returns:
        Valid variable names named by the operator, in the order given.

    Examples:
        >>> _extra_child_environment_keys({"BRIDGE_CHILD_ENV_EXTRA": "HTTPS_PROXY,NO_PROXY"})
        ['HTTPS_PROXY', 'NO_PROXY']
        >>> _extra_child_environment_keys({"BRIDGE_CHILD_ENV_EXTRA": " AWS_PROFILE , bad name ,"})
        ['AWS_PROFILE']
        >>> _extra_child_environment_keys({"BRIDGE_CHILD_ENV_EXTRA": "BRIDGE_CHILD_ENV_EXTRA"})
        []
        >>> _extra_child_environment_keys({})
        []
    """
    raw = env.get(CHILD_ENV_EXTRA_VARIABLE, "")
    names = (name.strip() for name in raw.split(","))
    return [name for name in names if name != CHILD_ENV_EXTRA_VARIABLE and _ENVIRONMENT_NAME.match(name)]


def _run_child(command: list[str], workspace: Path, timeout: float, job_path: Path | None = None) -> ChildOutcome:
    """Run a child in its own process group with stdin closed from birth."""
    environment = build_child_environment()
    environment[DEPTH_ENVIRONMENT_VARIABLE] = str(_next_bridge_depth())
    kwargs: dict[str, Any] = {
        "cwd": workspace,
        "env": environment,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }
    # Every peer owns a separate group so its supervisor can terminate the
    # complete peer tree without signalling the persistent supervisor itself.
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    try:
        process = subprocess.Popen(_resolved_command(command), **kwargs)  # noqa: S603 - fixed executables, explicit argv.
    except OSError as error:
        return ChildOutcome("", "", None, False, str(error))
    if getattr(process, "stdout", None) is None or getattr(process, "stderr", None) is None:
        # Test doubles and unusual subprocess adapters can omit pipe objects;
        # production Popen always supplies them because both streams are PIPE.
        stdout, stderr = process.communicate(timeout=timeout)
        return ChildOutcome(stdout, stderr, process.returncode, False, None)
    output = _ChildOutputBuffer()
    readers = _start_output_readers(process, output)
    deadline = time.monotonic() + timeout
    timed_out = False
    error = None
    while True:
        if output.output_limited.is_set():
            _terminate_process_group(process)
            drained = _finish_output_readers(readers)
            break
        if job_path is not None and _job_cancel_requested(job_path):
            _terminate_process_group(process)
            drained = _finish_output_readers(readers)
            error = "cancelled by job owner"
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _terminate_process_group(process)
            drained = _finish_output_readers(readers)
            timed_out = True
            break
        if process.poll() is not None:
            drained = _finish_output_readers(readers)
            if not drained:
                # A completed leader is not enough to close inherited pipes:
                # terminate the remaining group before returning its bounded output.
                _terminate_process_group(process)
                _finish_output_readers(readers)
            break
        time.sleep(min(0.05, remaining))
    # Final flush can reveal that an incomplete record was not compactable.
    # Overflow wins on every exit path so timeout recovery cannot replay it.
    stdout, stderr = output.text()
    if output.output_limited.is_set():
        _terminate_process_group(process)
        return ChildOutcome(
            stdout,
            stderr,
            process.returncode,
            False,
            f"child output exceeded {MAX_CHILD_OUTPUT_BYTES} byte capture limit",
            True,
        )
    if not drained:
        return ChildOutcome(
            stdout,
            stderr,
            process.returncode,
            False,
            "child output drain exceeded the Bridge cleanup grace",
        )
    return ChildOutcome(stdout, stderr, process.returncode, timed_out, error)


def _start_output_readers(process: subprocess.Popen[bytes], output: _ChildOutputBuffer) -> list[threading.Thread]:
    """Read both binary child streams concurrently so either pipe cannot deadlock the peer."""
    readers: list[threading.Thread] = []
    for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
        assert stream is not None
        reader = threading.Thread(target=_read_child_stream, args=(name, stream, output), daemon=True)
        reader.start()
        readers.append(reader)
    return readers


def _read_child_stream(stream_name: str, stream: Any, output: _ChildOutputBuffer) -> None:
    """Feed one child stream in bounded chunks until EOF or an output limit."""
    while not output.output_limited.is_set() and not output.finalized.is_set():
        chunk = stream.read(8 * 1024)
        if not chunk:
            return
        output.append(stream_name, chunk)


def _finish_output_readers(readers: list[threading.Thread]) -> bool:
    """Join output readers after child exit without letting inherited pipes extend the supervisor lifetime."""
    deadline = time.monotonic() + 2.0
    for reader in readers:
        reader.join(timeout=max(0.0, deadline - time.monotonic()))
    # Closing a BufferedReader while another thread is blocked in read() can
    # wait on its internal lock forever. Callers terminate the owning group
    # instead, then make one further bounded join attempt.
    return not any(reader.is_alive() for reader in readers)


def _resolved_command(command: list[str]) -> list[str]:
    """Resolve the executable through PATH so Windows npm shims (.cmd) launch."""
    resolved = shutil.which(command[0])
    return [resolved, *command[1:]] if resolved else command


def _drain_terminated_child(process: subprocess.Popen[str]) -> tuple[str, str]:
    """Collect buffered output after a group kill without blocking forever.

    A surviving grandchild can hold the inherited pipe write ends open past the group kill; an unbounded
    ``communicate()`` would then outlive every documented cutoff, so the drain itself is bounded.
    """
    try:
        return process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
    try:
        return process.communicate(timeout=2)
    except (subprocess.TimeoutExpired, OSError):
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        return "", ""


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    """Terminate the child tree and collect its leader's exit status within a bounded wait."""
    try:
        if os.name == "nt":
            if not _terminate_windows_process_tree(process.pid):
                process.kill()
            return
        kill_process_group = getattr(os, "killpg", None)
        if kill_process_group is None:
            process.kill()
            return
        try:
            kill_process_group(process.pid, signal.SIGTERM)
        except OSError:
            process.kill()
            return
        _wait_for_cleanup_grace(process)
        try:
            # The leader can exit during the grace period while descendants retain
            # inherited pipes, so leader completion is never proof that its group ended.
            kill_process_group(process.pid, signal.SIGKILL)
        except OSError:
            pass
    finally:
        # External tree kills and Popen.kill() do not refresh Popen.returncode.
        # Reap every termination path without letting failed cleanup hang a request.
        try:
            process.wait(timeout=2.0)
        except (OSError, subprocess.TimeoutExpired):
            print("Bridge child exit status unavailable after bounded cleanup wait", file=sys.stderr)


def _wait_for_cleanup_grace(process: subprocess.Popen[str]) -> None:
    """Allow cooperative child-tree exit for a bounded interval before force termination."""
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        try:
            process.wait(timeout=min(0.1, deadline - time.monotonic()))
        except (OSError, subprocess.TimeoutExpired):
            pass
        time.sleep(0.01)


def _terminate_windows_process_tree(pid: int) -> bool:
    """Force-terminate a Windows process and descendants with the native taskkill utility."""
    try:
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return False
    return result.returncode == 0


def _parse_output(stdout: str, host: str) -> ParsedOutput:
    """Parse Codex JSONL or Claude print JSON into normalized observed data."""
    records = _json_records(stdout)
    if host == "claude":
        # The host decides the parse shape; inferring it from the record count
        # would silently drop cost, usage, and session data whenever a second
        # parseable line appears on stdout.
        record = _claude_record(records)
        # An is_error result never carries a model core: decoding its string
        # payload (for example "429") would displace the structured error.
        if not isinstance(record, dict):
            core = record
        elif record.get("is_error") is True:
            core = None
        else:
            core = _decode_possible_json(record.get("structured_output", record.get("result", record)))
        usage = record.get("usage", record.get("modelUsage", {})) if isinstance(record, dict) else {}
        aggregate_cost = _cost_from(record) if isinstance(record, dict) else None
        return ParsedOutput(
            core,
            _number_fields(usage),
            aggregate_cost if aggregate_cost is not None else _cost_from(usage),
            record.get("session_id") if isinstance(record, dict) else None,
            _error_from(record) if isinstance(record, dict) else None,
        )
    core: Any = None
    tokens: dict[str, float] = {}
    cost: float | None = None
    session_id: str | None = None
    error: str | None = None
    for record in records:
        if not isinstance(record, dict):
            continue
        record_type = str(record.get("type", ""))
        if record_type == "thread.started":
            session_id = _first_string(record, ("thread_id", "threadId", "id")) or session_id
        if record_type == "turn.completed":
            usage = record.get("usage", {})
            tokens.update(_number_fields(usage))
            cost = _cost_from(usage) if _cost_from(usage) is not None else cost
        if record_type in {"turn.failed", "error"}:
            error = _error_from(record) or error
        candidate = _model_candidate(record)
        if candidate is not None:
            core = candidate
    return ParsedOutput(core, tokens, cost, session_id, error)


def _claude_record(records: list[Any]) -> Any:
    """Select the Claude print-mode response document from parsed stdout records.

    Warning or log lines can precede the response; the response is the last record carrying a recognizable print-mode
    field, never a bare count guess.
    """
    if len(records) == 1:
        return records[0]
    response_fields = ("structured_output", "result", "usage", "modelUsage", "is_error")
    for record in reversed(records):
        if isinstance(record, dict) and any(field in record for field in response_fields):
            return record
    return None


def _json_records(stdout: str) -> list[Any]:
    """Load JSON lines while retaining a standalone JSON response as one record."""
    records: list[Any] = []
    for line in stdout.splitlines():
        try:
            records.append(json.loads(line))
        except (ValueError, RecursionError):
            continue
    if records:
        return records
    try:
        return [json.loads(stdout)]
    except (ValueError, RecursionError):
        return []


def _model_candidate(record: dict[str, Any]) -> Any | None:
    """Extract a possible structured final answer from one event without guessing fields."""
    item = record.get("item")
    if isinstance(item, dict):
        for key in ("text", "content"):
            candidate = _decode_possible_json(item.get(key))
            if candidate is not None:
                return candidate
    for key in ("text", "result"):
        candidate = _decode_possible_json(record.get(key))
        if candidate is not None:
            return candidate
    return None


def _decode_possible_json(value: Any) -> Any | None:
    """Decode a JSON object embedded in a text result when present."""
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    try:
        return json.loads(value)
    except (ValueError, RecursionError):
        return None


def _number_fields(value: Any) -> dict[str, float]:
    """Keep immediate numeric usage fields as portable token counters."""
    if not isinstance(value, dict):
        return {}
    cost_fields = {"cost", "cost_usd", "total_cost_usd"}
    return {
        str(key): float(item)
        for key, item in value.items()
        if key not in cost_fields and not isinstance(item, bool) and isinstance(item, (int, float)) and item >= 0
    }


def _cost_from(value: Any) -> float | None:
    """Return a reported aggregate cost from a host record or usage object."""
    if not isinstance(value, dict):
        return None
    for key in ("cost", "cost_usd", "total_cost_usd"):
        candidate = value.get(key)
        if not isinstance(candidate, bool) and isinstance(candidate, (int, float)) and candidate >= 0:
            return float(candidate)
    return None


def _error_from(value: Any) -> str | None:
    """Render a structured host error into one stable incident message."""
    if not isinstance(value, dict):
        return None
    if value.get("is_error") is True:
        terminal_reason = value.get("terminal_reason")
        api_status = value.get("api_error_status")
        provider_message = value.get("result")
        parts = [
            str(item)
            for item in (
                terminal_reason if isinstance(terminal_reason, str) else None,
                f"HTTP {api_status}" if isinstance(api_status, int) else None,
                provider_message if isinstance(provider_message, str) else None,
            )
            if item
        ]
        rendered = ": ".join(parts)
        return rendered[:MAX_VERDICT_CHARS] if rendered else "provider request failed"
    error = value.get("error", value)
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    parameter = error.get("param")
    message = error.get("message")
    parts = [str(item) for item in (code, parameter, message) if item]
    rendered = ": ".join(parts)
    return rendered[:MAX_VERDICT_CHARS] if rendered else None


def _first_string(value: dict[str, Any], keys: Iterable[str]) -> str | None:
    """Return the first non-empty string found under ordered candidate keys."""
    for key in keys:
        item = value.get(key)
        if isinstance(item, str) and item:
            return item
    return None


def _prompt_with_budget(request: Request) -> str:
    """Add bounded execution context and review-specific adversarial instructions."""
    preamble = (
        f"You have a soft budget of {request.timeout_seconds:g} seconds. Return a compact structured result before it expires. "
        "If unfinished, use partial with remaining work; if blocked, name blockers instead of waiting. "
        "Keep verdict, findings, files_touched, remaining, and blockers decision-critical; put verbose supporting material only "
        "in details. The harness persists details in the transcript artifact and omits them from the public envelope. "
        f"Bridge depth is {request.depth}; run id is {request.run_id}.\n\n"
    )
    if request.verb == "review":
        preamble += (
            "Perform an adversarial read-only review: identify concrete correctness, safety, and regression risks. "
            "Do not edit files or invoke write-capable tools.\n\n"
        )
    return preamble + request.task


def _trusted_inherited_depth() -> int:
    """Return the transport-owned bridge depth inherited by this process."""
    raw_depth = os.environ.get(DEPTH_ENVIRONMENT_VARIABLE, "0")
    try:
        depth = int(raw_depth)
    except ValueError as error:
        raise ValueError(f"invalid {DEPTH_ENVIRONMENT_VARIABLE}") from error
    if depth < 0:
        raise ValueError(f"invalid {DEPTH_ENVIRONMENT_VARIABLE}")
    return depth


def _request_with_trusted_depth(request: Request) -> Request:
    """Prevent caller input from lowering the transport-owned recursion depth."""
    if request.depth < 0:
        raise ValueError("depth must be a non-negative integer")
    return Request(**{**request.__dict__, "depth": max(request.depth, _trusted_inherited_depth())})


def _next_bridge_depth() -> int:
    """Increment the trusted depth inherited by a peer process."""
    return _trusted_inherited_depth() + 1


def _write_transcript(paths: BridgePaths, stdout: str, stderr: str) -> str:
    """Write one bounded child transcript and return its workspace-relative path."""
    path = paths.root / f"raw-{time.time_ns()}-{uuid.uuid4().hex[:8]}.txt"
    payload = f"stdout:\n{stdout}\n\nstderr:\n{stderr}\n".encode("utf-8")
    path.write_bytes(payload[:MAX_CHILD_TRANSCRIPT_BYTES])
    return paths.relative(path)


def _workspace_state(workspace: Path) -> list[str]:
    """Capture a porcelain snapshot without failing when Git or a repository is absent.

    The probe is bounded: a hung ``git status`` (stale ``index.lock``, stalled
    network filesystem) runs before any request budget is armed and must not
    block dispatch outside every documented cutoff.
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [line for line in result.stdout.splitlines() if line]


def _workspace_delta(before: list[str], workspace: Path) -> list[str]:
    """Return changed porcelain lines between spawn and a killed implementation."""
    before_set = set(before)
    return [line for line in _workspace_state(workspace) if line not in before_set]


def _make_envelope(
    request: Request,
    core: dict[str, Any],
    transcript_path: str,
    tokens: dict[str, float],
    cost: float | None,
    session_id: str | None,
    duration: float,
    substitution: dict[str, str] | None,
    incident: str | None,
) -> dict[str, Any]:
    """Merge validated core and observed values into the sole public result shape."""
    envelope = {
        **core,
        "model": request.model or "host-default",
        "effort": request.effort,
        "effort_substituted": substitution,
        "cost": cost,
        "tokens": tokens,
        "duration_seconds": duration,
        "depth": request.depth,
        "run_id": request.run_id,
        "incident": incident,
        "session_id": session_id,
        "transcript_path": transcript_path,
        "verb": request.verb,
        "direction": request.direction,
    }
    return validate_envelope(envelope)


def _terminal_envelope(
    request: Request,
    paths: BridgePaths,
    status: str,
    reason: str,
    transcript_path: str | None,
    tokens: dict[str, float],
    session_id: str | None,
    duration: float,
    substitution: dict[str, str] | None = None,
    workspace_delta: list[str] | None = None,
    prior_incident: str | None = None,
    record_health: bool = True,
    fault: str | None = None,
) -> dict[str, Any]:
    """Create, record, and return a harness-owned terminal result."""
    if transcript_path is None:
        transcript_path = _write_transcript(paths, "", reason)
    core = {
        "status": status,
        "verdict": reason,
        "findings": [],
        "files_touched": [],
        "remaining": [],
        "blockers": [reason],
    }
    incident = _write_incident(
        paths, request, fault or status, reason, transcript_path, workspace_delta, prior_incident
    )
    envelope = _make_envelope(
        request, core, transcript_path, tokens, None, session_id, duration, substitution, incident
    )
    if record_health:
        try:
            _append_health(paths, envelope)
        except ArtifactBoundaryError:
            # A blocked result must remain readable even when the health member
            # itself is hostile; no external telemetry write is an acceptable tradeoff.
            pass
    return envelope


def _write_incident(
    paths: BridgePaths,
    request: Request,
    fault: str,
    reason: str,
    transcript_path: str,
    workspace_delta: list[str] | None,
    prior_incident: str | None = None,
) -> str:
    """Persist one compact incident record without copying child environment data."""
    path = paths.incidents / f"{time.time_ns()}-{uuid.uuid4().hex[:8]}-{fault}.json"
    payload = {
        "fault": fault,
        "reason": reason,
        "model": request.model or "host-default",
        "effort": request.effort,
        "verb": request.verb,
        "duration_budget_seconds": request.timeout_seconds,
        "transcript_path": transcript_path,
        "workspace_delta": workspace_delta or [],
        "prior_incident": prior_incident,
    }
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    return paths.relative(path)


def _append_health(paths: BridgePaths, envelope: dict[str, Any]) -> None:
    """Append one cross-hop accounting record for every completed bridge call."""
    payload = {
        key: envelope[key]
        for key in (
            "run_id",
            "verb",
            "direction",
            "model",
            "effort",
            "cost",
            "tokens",
            "duration_seconds",
            "status",
            "depth",
        )
    }
    payload["ts"] = time.time()
    with _open_regular_append(paths.root / "health.jsonl") as stream:
        stream.write(json.dumps(payload, sort_keys=True) + "\n")


def _open_regular_append(path: Path) -> Any:
    """Open a predictable health file only when its opened inode has one link."""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        metadata = None
    except OSError as error:
        raise ArtifactBoundaryError("bridge health artifact could not be inspected") from error
    if metadata is not None and _is_link_or_reparse_point(metadata):
        raise ArtifactBoundaryError("bridge health artifact must be a regular file")
    if os.name == "nt":
        descriptor = _open_windows_nofollow_descriptor(path)
    else:
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags, 0o600)
        except OSError as error:
            raise ArtifactBoundaryError("bridge health artifact could not be opened safely") from error
    try:
        opened_metadata = os.fstat(descriptor)
        if not stat.S_ISREG(opened_metadata.st_mode) or opened_metadata.st_nlink != 1:
            raise ArtifactBoundaryError("bridge health artifact must be a regular file")
        return os.fdopen(descriptor, "a", encoding="utf-8", newline="\n")
    except BaseException:
        os.close(descriptor)
        raise


def _is_link_or_reparse_point(metadata: os.stat_result) -> bool:
    """Identify linked artifact members before using their predictable filename."""
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(getattr(metadata, "st_file_attributes", 0) & reparse_attribute)


def _open_windows_nofollow_descriptor(path: Path) -> int:
    """Open one Windows health member without dereferencing a reparse point."""
    import ctypes
    import msvcrt

    create_file = ctypes.windll.kernel32.CreateFileW
    create_file.restype = ctypes.c_void_p
    handle = create_file(
        str(path),
        0x40000000,
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        4,
        0x00000080 | 0x00200000,
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise ArtifactBoundaryError("bridge health artifact could not be opened safely")
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_APPEND)
    except OSError as error:
        ctypes.windll.kernel32.CloseHandle(handle)
        raise ArtifactBoundaryError("bridge health artifact could not be opened safely") from error
    return descriptor


def start_background(request: Request) -> dict[str, Any]:
    """Start a detached supervisor and return its persistent job record."""
    validate_request_transport_budget(request)
    paths = BridgePaths(request.workspace.resolve())
    paths.prepare()
    job_id = str(uuid.uuid4())
    record_path = _job_record_path(request.workspace, job_id)
    record = {
        "job_id": job_id,
        "status": "queued",
        "request": _request_json(request),
        "pid": None,
        "result": None,
        "started_ts": time.time(),
    }
    _write_json(record_path, record)
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        request.verb,
        "--task",
        request.task,
        "--effort",
        request.effort,
        "--timeout-seconds",
        str(request.timeout_seconds),
        "--depth",
        str(request.depth),
        "--run-id",
        request.run_id,
        "--workspace",
        str(request.workspace),
        "--job-id",
        job_id,
        "--supervisor",
    ]
    if request.model:
        command.extend(["--model", request.model])
    if request.session_id:
        command.extend(["--session-id", request.session_id])
    kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(command, **kwargs)  # noqa: S603 - invokes this installed script with explicit arguments.
    # The spawned supervisor records its own PID and running state; a launcher
    # write here could race a fast-failing supervisor and clobber its final
    # record with a stale queued-derived copy. The returned status matches the
    # record the caller will observe on its first poll.
    return {"job_id": job_id, "status": "queued"}


def job_status(workspace: Path, job_id: str) -> dict[str, Any]:
    """Return lifecycle state without exposing task text or the completed result."""
    record = _read_job(workspace, job_id)
    if record is None:
        return {"job_id": job_id, "status": "missing"}
    observed = _observed_status(record)
    if observed == "stalled":
        # A dead supervisor is terminal regardless of any cancellation marker:
        # nothing remains alive to consume the marker, so reporting
        # cancel_requested would hide the signal that ends polling.
        status = "stalled"
    elif _job_cancel_requested(_job_record_path(workspace, job_id)):
        status = "cancelled" if record.get("result") is not None else "cancel_requested"
    else:
        status = observed
    return {
        "job_id": job_id,
        "status": status,
        "pid": record.get("pid"),
    }


QUEUED_STALL_SECONDS = 120.0


def _observed_status(record: dict[str, Any]) -> str:
    """Downgrade a dead ``running`` or expired ``queued`` state to ``stalled``.

    A supervisor killed without the chance to write its final record would otherwise report ``running`` forever; the
    caller needs a terminal-looking signal to stop polling and inspect the transcript instead. A ``queued`` record has
    no PID to probe yet, so it stalls on age: a healthy supervisor rewrites the record to ``running`` within moments of
    its spawn.
    """
    status = str(record["status"])
    pid = record.get("pid")
    if status == "running" and isinstance(pid, int) and not isinstance(pid, bool) and not _process_exists(pid):
        return "stalled"
    started = record.get("started_ts")
    if (
        status == "queued"
        and isinstance(started, (int, float))
        and not isinstance(started, bool)
        and time.time() - float(started) > QUEUED_STALL_SECONDS
    ):
        return "stalled"
    return status


def _process_exists(pid: int) -> bool:
    """Probe process liveness without Windows ``os.kill`` termination semantics."""
    if os.name == "nt":
        try:
            probe = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            return True
        except OSError:
            return True
        return probe.returncode == 0 and f'"{pid}"' in probe.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def job_result(workspace: Path, job_id: str) -> dict[str, Any]:
    """Return a completed public envelope or the job's current lifecycle state."""
    record = _read_job(workspace, job_id)
    if record is None:
        return {"job_id": job_id, "status": "missing"}
    if record.get("result") is not None:
        # The stored result crossed a disk boundary; re-validate so a tampered
        # or truncated record cannot masquerade as a validated envelope, even
        # when a late cancellation is about to overwrite the status fields.
        stored = validate_envelope(record["result"])
        if _job_cancel_requested(_job_record_path(workspace, job_id)):
            return _cancelled_envelope(stored)
        return stored
    observed = _observed_status(record)
    if observed != "stalled" and _job_cancel_requested(_job_record_path(workspace, job_id)):
        return {"job_id": job_id, "status": "cancel_requested"}
    return {"job_id": job_id, "status": observed}


def cancel_job(workspace: Path, job_id: str) -> dict[str, Any]:
    """Cooperatively request supervisor-owned cancellation without signalling a stored PID.

    The return is the same compact projection as ``job_status``: the record also holds the task text and any completed
    result, which lifecycle calls must not leak back into the caller's context.
    """
    record = _read_job(workspace, job_id)
    if record is None:
        return {"job_id": job_id, "status": "missing"}
    observed = _observed_status(record)
    if observed not in {"queued", "running"}:
        # Includes stalled: writing a marker no live supervisor can consume
        # would replace the terminal signal with cancel_requested forever.
        return {"job_id": job_id, "status": observed, "pid": record.get("pid")}
    _write_json(_job_cancel_path(workspace, job_id), {"job_id": _canonical_job_id(job_id)})
    return {"job_id": job_id, "status": "cancel_requested", "pid": record.get("pid")}


def _read_job(workspace: Path, job_id: str) -> dict[str, Any] | None:
    """Load an identity-matched job record through its contained canonical path."""
    canonical_id = _canonical_job_id(job_id)
    path = _job_record_path(workspace, canonical_id)
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("job_id") != canonical_id:
        raise ValueError("job record identity does not match its requested identifier")
    if not isinstance(value.get("status"), str):
        raise ValueError("job record has no valid lifecycle status")
    return value


def _canonical_job_id(job_id: str) -> str:
    """Require the lower-case UUID form generated by the bridge supervisor."""
    if not isinstance(job_id, str):
        raise ValueError("job_id must be a canonical UUID")
    try:
        parsed = uuid.UUID(job_id)
    except ValueError as error:
        raise ValueError("job_id must be a canonical UUID") from error
    if str(parsed) != job_id:
        raise ValueError("job_id must be a canonical UUID")
    return job_id


def _job_record_path(workspace: Path, job_id: str) -> Path:
    """Return a path proven to remain beneath the workspace-local job store."""
    canonical_id = _canonical_job_id(job_id)
    jobs = BridgePaths(workspace.resolve()).jobs.resolve()
    path = (jobs / f"{canonical_id}.json").resolve()
    try:
        path.relative_to(jobs)
    except ValueError as error:
        raise ValueError("job record path escapes the bridge job store") from error
    return path


def _job_cancel_path(workspace: Path, job_id: str) -> Path:
    """Return the contained cooperative-cancellation marker for one canonical job."""
    canonical_id = _canonical_job_id(job_id)
    jobs = BridgePaths(workspace.resolve()).jobs.resolve()
    path = (jobs / f"{canonical_id}.cancel.json").resolve()
    try:
        path.relative_to(jobs)
    except ValueError as error:
        raise ValueError("job cancellation path escapes the bridge job store") from error
    return path


def _job_cancel_requested(job_path: Path) -> bool:
    """Read a dedicated cancellation marker that record writes cannot overwrite.

    An unreadable, malformed, or identity-mismatched marker is treated as no
    cancellation instead of raising: this poll runs while a live child is
    supervised, and an exception here would kill the supervisor and orphan the
    write-capable child. A real cancellation rewrites the marker atomically.
    """
    cancel_path = job_path.with_name(f"{job_path.stem}.cancel.json")
    try:
        value = json.loads(cancel_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(value, dict) and value.get("job_id") == job_path.stem


def _cancelled_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    """Turn a late cooperative cancellation marker into the authoritative result.

    Landed work must stay visible: ``findings``, ``files_touched``, and
    ``remaining`` from the completed child survive so a cancellation can never
    hide edits that already reached the worktree.
    """
    cancelled = {
        **envelope,
        "status": "blocked",
        "verdict": "cancelled by job owner",
        "blockers": ["cancelled by job owner"],
    }
    return validate_envelope(cancelled)


def _request_json(request: Request) -> dict[str, Any]:
    """Serialize a request into a job record without nonportable Path objects."""
    return {
        **request.__dict__,
        "workspace": str(request.workspace),
        "origin_workspace": str(request.origin_workspace) if request.origin_workspace else None,
        "supported_efforts": list(request.supported_efforts),
    }


def _write_json(path: Path, value: dict[str, Any]) -> None:
    """Atomically write a JSON artifact with byte-stable cross-platform newlines."""
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        temporary.replace(path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def _resolve_task(args: argparse.Namespace) -> str:
    """Return the task text, reading ``--task-file`` when the task is not inline.

    ``--task-file`` exists so a caller holding text it did not author — a pull-request comment, an issue body, a
    reviewer finding — never has to embed that text in the command line it composes. Quoting is then not a property of
    how carefully the caller escaped the text.
    """
    if args.task is not None and args.task_file is not None:
        raise ValueError("pass either --task or --task-file, not both")
    if args.task is not None:
        return args.task
    if args.task_file is None:
        raise ValueError("one of --task or --task-file is required")
    try:
        return Path(args.task_file).read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError(f"--task-file could not be read: {error}") from error


def _request_from_args(args: argparse.Namespace) -> Request:
    """Build a validated public request from one command-line namespace."""
    workspace = Path(args.workspace).resolve()
    task = _resolve_task(args)
    if not task.strip():
        raise ValueError("--task must not be empty")
    if args.verb not in DEFAULT_TIMEOUTS:
        raise ValueError("unknown bridge verb")
    if args.session_id and not _is_write_verb(args.verb):
        raise ValueError("--session-id is available only for implement")
    if args.background and not _is_write_verb(args.verb):
        raise ValueError("--background is available only for implement")
    if args.depth < 0:
        raise ValueError("--depth must be a non-negative integer")
    timeout = args.timeout_seconds if args.timeout_seconds is not None else DEFAULT_TIMEOUTS[args.verb]
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("--timeout-seconds must be a finite positive number")
    if args.effort is not None:
        normalize_effort(args.effort, "codex")
    request = Request(
        args.verb,
        task,
        args.model or DEFAULT_MODEL,
        args.effort or DEFAULT_EFFORT,
        float(timeout),
        args.depth,
        args.run_id or str(uuid.uuid4()),
        workspace,
        "claude_to_codex",
        args.background,
        args.session_id,
        workspace if args.session_id else None,
    )
    validate_request_transport_budget(request)
    return request


def _parser() -> argparse.ArgumentParser:
    """Create the stable public command parser for skills and lifecycle calls."""
    parser = argparse.ArgumentParser(description="Run a compact Claude/Codex bridge call.")
    commands = parser.add_subparsers(dest="command", required=True)
    for verb in DEFAULT_TIMEOUTS:
        child = commands.add_parser(verb)
        child.set_defaults(verb=verb)
        # Not argparse-mutually-exclusive: _resolve_task raises a ValueError that reaches
        # the caller inside the JSON error envelope, whereas argparse would exit 2 with a
        # bare usage string that no skill or MCP caller can parse.
        child.add_argument("--task")
        child.add_argument("--task-file", help="read the task from this file instead of the command line")
        child.add_argument("--model")
        child.add_argument("--effort")
        child.add_argument("--timeout-seconds", type=float)
        child.add_argument("--background", action="store_true")
        child.add_argument("--session-id")
        child.add_argument("--depth", type=int, default=0)
        child.add_argument("--run-id")
        child.add_argument("--workspace", default=".")
        child.add_argument("--job-id", help=argparse.SUPPRESS)
        child.add_argument("--supervisor", action="store_true", help=argparse.SUPPRESS)
    for command in ("status", "result", "cancel"):
        child = commands.add_parser(command)
        child.add_argument("--job-id", required=True)
        child.add_argument("--workspace", default=".")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the public bridge CLI and emit exactly one JSON object to stdout."""
    args = _parser().parse_args(argv)
    exit_code = 0
    try:
        if args.command in {"status", "result"}:
            output = (
                job_status(Path(args.workspace), args.job_id)
                if args.command == "status"
                else job_result(Path(args.workspace), args.job_id)
            )
        elif args.command == "cancel":
            output = cancel_job(Path(args.workspace), args.job_id)
        else:
            request = _request_from_args(args)
            if args.job_id is not None and not args.supervisor:
                raise ValueError("--job-id is valid only with --supervisor")
            if args.supervisor:
                if args.job_id is None:
                    raise ValueError("--supervisor requires --job-id")
                record_path = _job_record_path(request.workspace, args.job_id)
                record = _read_job(request.workspace, args.job_id)
                if record is None:
                    raise ValueError("supervisor job record is missing")
                if not _job_cancel_requested(record_path):
                    record["pid"] = os.getpid()
                    record["status"] = "running"
                    _write_json(record_path, record)
                try:
                    output = run_request(request, _job_path=record_path)
                except (OSError, ValueError) as error:
                    # Leave a terminal record so lifecycle callers stop polling
                    # a supervisor that died before writing its result.
                    failed = _read_job(request.workspace, args.job_id)
                    if failed is not None:
                        failed["status"] = "failed"
                        failed["error"] = str(error)
                        _write_json(record_path, failed)
                    raise
                record = _read_job(request.workspace, args.job_id)
                if record is None:
                    raise ValueError("supervisor job record disappeared")
                if _job_cancel_requested(record_path):
                    output = _cancelled_envelope(output)
                    record["status"] = "cancelled"
                else:
                    record["status"] = "finished"
                record["result"] = output
                _write_json(record_path, record)
            elif request.background:
                output = start_background(request)
            else:
                output = run_request(request)
    except (OSError, ValueError) as error:
        output = {"status": "error", "error": str(error)}
        exit_code = 2
    sys.stdout.write(json.dumps(output, sort_keys=True) + "\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
