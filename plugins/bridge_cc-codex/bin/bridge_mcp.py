"""Expose reverse Bridge calls under explicitly confirmed project authority.

Purpose: Let an installed Codex Bridge hand bounded work to a locally authenticated Claude CLI through host-launched
MCP. Scope: Status, native workspace binding, implement, advise, and review share one process. It starts unbound; only a
folder typed by the user and separately confirmed in a native form creates project authority. Every executable call
names the current binding identity; folder resolution and directory identity are checked before dispatch. Binding
selects project identity and grants no runtime permission, editing approval, authentication, or paid-call consent.
Usage: Launch ``bridge_mcp.py --stdio``, initialize with form elicitation support, then invoke ``bridge_bind_workspace``
with empty arguments before executable tools. Outputs: Native forms select and confirm the exact canonical folder; tool
responses report binding/status or a compact Bridge envelope. Failure: Missing form support, unbound or stale
identities, changed folders, malformed responses, and child failures fail closed with protocol diagnostics. Rebinding
immediately clears previous authority; cancellation and EOF never bind. Used by: Codex-facing Bridge skills and setup
consumers, using the installed question provider's strict correlation transport and the shared Python Bridge supervisor.
No persistent binding, permission, settings, or credential writes occur.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from pathlib import PurePath
import sys
import uuid
from typing import Any, TextIO

# Keep sibling imports valid when repository-wide doctest collection imports this
# file without launching it as a script from its installed ``bin`` directory.
_BIN_DIRECTORY = str(Path(__file__).resolve().parent)
if _BIN_DIRECTORY not in sys.path:
    sys.path.insert(0, _BIN_DIRECTORY)

from bridge_call import (  # noqa: E402
    CHILD_TIMEOUT_MULTIPLIER,
    DEFAULT_EFFORT,
    DEFAULT_MODEL,
    DEFAULT_TIMEOUTS,
    Request,
    run_request,
    validate_request_transport_budget,
)
from user_questions_mcp import Decision, MAX_DECISIONS, Server as QuestionServer  # noqa: E402


MCP_HOST_DEADLINE_SECONDS = 900.0
MCP_RESPONSE_MARGIN_SECONDS = 30.0
# Worst-case per-attempt supervision overhead beyond the hard cutoff: the 2 s
# SIGTERM grace in _terminate_process_group plus the 5 s + 2 s bounded drain.
TERMINATION_DRAIN_SECONDS = 9.0
MAX_MCP_TIMEOUT_SECONDS_BY_VERB = {
    "implement": 700.0,
    "advise": 350.0,
    "review": 350.0,
}
_MAX_ATTEMPTS_BY_VERB = {"implement": 1, "advise": 2, "review": 2}
for _verb, _cap in MAX_MCP_TIMEOUT_SECONDS_BY_VERB.items():
    _attempts = _MAX_ATTEMPTS_BY_VERB[_verb]
    _worst_case = _attempts * (_cap * CHILD_TIMEOUT_MULTIPLIER + TERMINATION_DRAIN_SECONDS)
    if _worst_case + MCP_RESPONSE_MARGIN_SECONDS > MCP_HOST_DEADLINE_SECONDS:
        raise ValueError(f"MCP timeout cap for {_verb} cannot fit inside the host deadline")
TOOL_NAMES = {
    "bridge_implement": "implement",
    "bridge_advise": "advise",
    "bridge_review": "review",
}
STATUS_TOOL_NAME = "bridge_status"
BIND_TOOL_NAME = "bridge_bind_workspace"


def _plugin_version() -> str:
    """Read the authoritative plugin version from the installed Claude manifest.

    The one tool whose purpose is diagnosing stale installs must never report a hardcoded literal that can drift from
    the manifests on a release bump. An unreadable manifest degrades to ``unknown`` instead of crashing the stdio server
    at import time.
    """
    manifest = Path(__file__).resolve().parents[1] / ".claude-plugin" / "plugin.json"
    try:
        value = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "unknown"
    version = value.get("version") if isinstance(value, dict) else None
    return version if isinstance(version, str) and version else "unknown"


BRIDGE_VERSION = _plugin_version()
MCP_PROTOCOL_VERSION = "2025-06-18"
STATUS_SCHEMA_VERSION = "2.0"
EXPECTED_TOOL_INVENTORY = (STATUS_TOOL_NAME, BIND_TOOL_NAME, *TOOL_NAMES)


def tool_definitions() -> list[dict[str, Any]]:
    """Return MCP tool definitions for status and each executable bridge verb."""
    schema_path = Path(__file__).resolve().parents[1] / "schemas" / "mcp-tools.schema.json"
    definitions = json.loads(schema_path.read_text(encoding="utf-8"))["$defs"]
    request_tools = [
        {
            "name": name,
            "description": f"Run a bounded {verb} bridge request through Claude.",
            "inputSchema": definitions[name],
        }
        for name, verb in TOOL_NAMES.items()
    ]
    return [
        {
            "name": STATUS_TOOL_NAME,
            "description": "Report the read-only Bridge server and host-selected workspace status.",
            "inputSchema": definitions[STATUS_TOOL_NAME],
        },
        {
            "name": BIND_TOOL_NAME,
            "description": "Bind this process to a project folder selected and confirmed by the user in native forms. No model workspace argument is accepted; rebinding immediately clears the previous binding.",
            "inputSchema": definitions[BIND_TOOL_NAME],
        },
        *request_tools,
    ]


# Methods whose result depends on nothing but the server itself. Each entry stays a callable so a method that reads
# the installed schemas does that work only when it is actually called.
# A conforming client sends ``notifications/initialized`` without an id; a malformed id-bearing variant is
# acknowledged here instead of being left hanging.
_SELF_CONTAINED_METHODS: dict[str, Callable[[], dict[str, Any]]] = {
    "notifications/initialized": dict,
    "initialize": lambda: {
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "capabilities": {"tools": {}},
        "serverInfo": {"name": "bridge", "version": BRIDGE_VERSION},
    },
    "tools/list": lambda: {"tools": tool_definitions()},
}


def _invalid_request_error(message: dict[str, Any]) -> dict[str, Any] | None:
    """Return the JSON-RPC error for a malformed id-bearing request, or ``None`` when its shape is usable."""
    if message.get("jsonrpc") != "2.0":
        return _error(None, -32600, "invalid request: jsonrpc must be 2.0")
    method = message.get("method")
    if not isinstance(method, str) or not method:
        return _error(None, -32600, "invalid request: method must be a non-empty string")
    if "params" in message and not isinstance(message["params"], (dict, list)):
        return _error(None, -32600, "invalid request: params must be an object or array")
    request_id = message["id"]
    if (
        isinstance(request_id, bool)
        or not isinstance(request_id, (str, int, float, type(None)))
        or isinstance(request_id, float)
        and not math.isfinite(request_id)
    ):
        return _error(None, -32600, "invalid request: id must be a string, number, or null")
    return None


def handle_message(message: dict[str, Any], *, trusted_workspace: Path | None = None) -> dict[str, Any] | None:
    """Handle a request with optional explicit embedding-host workspace authority.

    The caller owns ``trusted_workspace``; model arguments and stdio never populate it. A missing workspace rejects
    executable calls rather than trusting process cwd. Native binding requires the stateful ``BridgeServer`` stdio
    entrypoint.
    """
    # JSON-RPC 2.0 forbids responding to a notification, so id-lessness short-circuits before any validation or
    # dispatch: an id-less message can never produce a response, valid or not.
    if "id" not in message:
        return None
    invalid = _invalid_request_error(message)
    if invalid is not None:
        return invalid
    request_id = message["id"]
    method = str(message["method"])
    self_contained = _SELF_CONTAINED_METHODS.get(method)
    if self_contained is not None:
        return _result(request_id, self_contained())
    if method == "tools/call":
        return _call_tool(request_id, message.get("params"), trusted_workspace)
    return _error(request_id, -32601, f"method not found: {method}")


def _call_tool(
    request_id: Any,
    params: Any,
    trusted_workspace: Path | None,
    *,
    binding_id: str | None = None,
    protocol: str = MCP_PROTOCOL_VERSION,
) -> dict[str, Any]:
    """Validate tool arguments and execute a request or return local server status."""
    if not isinstance(params, dict):
        return _error(request_id, -32602, "tools/call params must be an object")
    name = params.get("name")
    arguments = params.get("arguments", {})
    if not isinstance(name, str) or not name:
        return _error(request_id, -32602, "bridge tool name must be a non-empty string")
    if name == STATUS_TOOL_NAME:
        if not isinstance(arguments, dict) or arguments:
            return _error(request_id, -32602, "bridge_status accepts an empty arguments object")
        return _result(request_id, _status_result(trusted_workspace, binding_id=binding_id, protocol=protocol))
    if not isinstance(arguments, dict):
        return _error(request_id, -32602, "tool arguments must be an object")
    if name not in TOOL_NAMES:
        return _error(request_id, -32602, f"unknown bridge tool: {name}")
    if trusted_workspace is None:
        return _error(
            request_id, -32002, "workspace unbound; use bridge_bind_workspace and complete native confirmation"
        )
    try:
        request = _request_from_arguments(TOOL_NAMES[name], arguments, trusted_workspace)
    except ValueError as error:
        return _error(request_id, -32602, str(error))
    try:
        envelope = run_request(request, host="claude")
    except (OSError, ValueError):
        return _error(request_id, -32603, "bridge execution failed")
    return _result(
        request_id,
        {
            "content": [{"type": "text", "text": json.dumps(envelope, sort_keys=True)}],
            "isError": envelope["status"] in {"blocked", "timeout", "refused"},
        },
    )


def _status_result(
    trusted_workspace: Path | None, *, binding_id: str | None = None, protocol: str = MCP_PROTOCOL_VERSION
) -> dict[str, Any]:
    """Build sanitized server status without invoking a provider or changing state."""
    normalized_workspace = PurePath(trusted_workspace.resolve()).as_posix() if trusted_workspace is not None else None
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "bridge_version": BRIDGE_VERSION,
                        "expected_tool_inventory": list(EXPECTED_TOOL_INVENTORY),
                        "plugin_version": BRIDGE_VERSION,
                        "protocol_version": protocol,
                        "schema_version": STATUS_SCHEMA_VERSION,
                        "binding_status": "bound" if trusted_workspace is not None else "unbound",
                        "binding_id": binding_id,
                        "server": {"name": "bridge", "version": BRIDGE_VERSION},
                        # The setup skills cross-check this value against the
                        # setup result's canonical_workspace, so both must use
                        # the same POSIX-separator canonical form on every OS.
                        "workspace": normalized_workspace,
                        "workspace_fingerprint": (
                            hashlib.sha256(normalized_workspace.encode("utf-8")).hexdigest()
                            if normalized_workspace is not None
                            else None
                        ),
                    },
                    sort_keys=True,
                ),
            }
        ],
        "isError": False,
    }


def _request_from_arguments(verb: str, arguments: dict[str, Any], trusted_workspace: Path) -> Request:
    """Create a reverse-direction request from MCP tool arguments."""
    allowed = {"task", "model", "effort", "timeout_seconds", "depth", "run_id", "supported_efforts"}
    extra = set(arguments) - allowed
    if extra:
        raise ValueError(f"unsupported tool arguments: {', '.join(sorted(extra))}")
    task = arguments.get("task")
    if not isinstance(task, str) or not task.strip():
        raise ValueError("task must be a non-empty string")
    depth = arguments.get("depth", 0)
    if isinstance(depth, bool) or not isinstance(depth, int) or depth < 0:
        raise ValueError("depth must be a non-negative integer")
    timeout = arguments.get("timeout_seconds", DEFAULT_TIMEOUTS[verb])
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout_seconds must be a finite positive number")
    maximum_timeout = MAX_MCP_TIMEOUT_SECONDS_BY_VERB[verb]
    if timeout > maximum_timeout:
        raise ValueError(f"timeout_seconds must not exceed {maximum_timeout:g} seconds for {verb}")
    for name in ("model", "effort", "run_id"):
        value = arguments.get(name)
        if value is not None and (not isinstance(value, str) or not value):
            raise ValueError(f"{name} must be a non-empty string")
    supported = arguments.get("supported_efforts", [])
    if not isinstance(supported, list) or not all(isinstance(item, str) and item for item in supported):
        raise ValueError("supported_efforts must be an array of non-empty strings")
    if "supported_efforts" in arguments and not supported:
        raise ValueError("supported_efforts must not be empty when supplied")
    workspace = trusted_workspace.resolve()
    if verb == "implement" and workspace in _refused_write_roots(workspace):
        raise ValueError(
            "write-capable bridge calls need a project workspace; the MCP host launched this server "
            "from the user home or a filesystem root"
        )
    request = Request(
        verb,
        task,
        arguments.get("model", DEFAULT_MODEL),
        arguments.get("effort", DEFAULT_EFFORT),
        float(timeout),
        depth,
        arguments.get("run_id", str(uuid.uuid4())),
        workspace,
        "codex_to_claude",
        False,
        None,
        None,
        tuple(supported),
    )
    validate_request_transport_budget(request)
    return request


def _refused_write_roots(workspace: Path) -> set[Path]:
    """Return the launch directories too broad to root an acceptEdits run.

    ``Path.home`` can raise on hosts with no home resolution (minimal containers); an unknown home must not crash the
    server, only narrow the refusal set to the filesystem root.
    """
    roots = {Path(workspace.anchor)}
    try:
        roots.add(Path.home().resolve())
    except (OSError, RuntimeError):
        pass
    return roots


def _result(request_id: Any, value: dict[str, Any]) -> dict[str, Any]:
    """Build one JSON-RPC success response."""
    return {"jsonrpc": "2.0", "id": request_id, "result": value}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    """Build one JSON-RPC error response without leaking traceback details."""
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


@dataclass(frozen=True)
class WorkspaceBinding:
    """Retain the selected path and directory identity for later authority checks."""

    selected: Path
    canonical: Path
    identity: tuple[int, int]


def _workspace_binding(value: str) -> WorkspaceBinding:
    """Validate an existing absolute project folder outside protected installation roots."""
    selected = Path(value)
    if not selected.is_absolute():
        raise ValueError("workspace must be an absolute existing folder")
    canonical = selected.resolve(strict=True)
    if len(PurePath(canonical).as_posix()) > 3500:
        raise ValueError("canonical workspace exceeds the native confirmation limit")
    if not canonical.is_dir() or canonical in _refused_write_roots(canonical):
        raise ValueError("workspace cannot be a filesystem root, user home, or non-directory")
    plugin_root = Path(__file__).resolve().parents[1]
    if any(canonical.is_relative_to(root) for root in _protected_payload_roots(plugin_root)):
        raise ValueError("workspace cannot be inside the plugin payload or installation cache")
    identity = canonical.stat()
    return WorkspaceBinding(selected, canonical, (identity.st_dev, identity.st_ino))


def _protected_payload_roots(plugin_root: PurePath) -> list[PurePath]:
    """Recognize payload and marketplace cache ancestry using native path semantics."""
    protected = [plugin_root]
    for ancestor in plugin_root.parents:
        if ancestor.name.casefold() == "cache" and ancestor.parent.name.casefold() == "plugins":
            protected.append(ancestor)
    return protected


class BridgeServer(QuestionServer):
    """Bind workspace authority through native user forms before dispatching Bridge calls."""

    def __init__(self, output: TextIO, diagnostics: TextIO) -> None:
        """Start with no workspace authority and no pending binding operation."""
        super().__init__(output, diagnostics)
        self.workspace: WorkspaceBinding | None = None
        self.binding_id: str | None = None
        self.candidate: WorkspaceBinding | None = None
        self.binding_phase: str | None = None

    def result(self, request_id: str | int, result: dict[str, Any]) -> None:
        """Keep the inherited handshake identified as the Bridge backend."""
        if "serverInfo" in result:
            result["serverInfo"] = {"name": "bridge", "version": BRIDGE_VERSION}
        if "tools" in result:
            result = {"tools": tool_definitions()}
        super().result(request_id, result)

    def call(self, request_id: str | int, params: Any) -> None:
        """Accept only user binding or Bridge calls under current verified authority."""
        if (
            not isinstance(params, dict)
            or not {"name"} <= set(params) <= {"name", "arguments", "_meta"}
            or ("_meta" in params and params["_meta"] is not None and not isinstance(params["_meta"], dict))
        ):
            self.error(request_id, -32602, "invalid Bridge tools/call params")
            return
        name = params.get("name")
        if name == BIND_TOOL_NAME:
            self.workspace = None
            self.binding_id = None
            if not isinstance(params.get("arguments", {}), dict) or params.get("arguments", {}):
                self.error(request_id, -32602, "bridge_bind_workspace accepts an empty arguments object")
                return
            if self.binding_phase is not None:
                self.error(request_id, -32001, "workspace binding already pending")
                return
            if self.protocol is None or not self.elicitation:
                self.error(request_id, -32002, "workspace binding requires native form elicitation")
                return
            if len(self.decisions) + 2 > MAX_DECISIONS:
                self.error(request_id, -32003, "binding capacity exhausted; start a new session")
                return
            self.binding_phase = "select"
            self._ask_binding(request_id, "Enter the absolute existing project folder to use for Bridge calls.")
            return
        workspace = self._current_workspace()
        if isinstance(name, str) and name in TOOL_NAMES:
            arguments = params.get("arguments")
            if (
                workspace is None
                or not isinstance(arguments, dict)
                or self.binding_id is None
                or arguments.get("binding_id") != self.binding_id
            ):
                self.error(request_id, -32002, "workspace unbound or binding_id missing/stale; confirm current binding")
                return
            params = {**params, "arguments": {key: value for key, value in arguments.items() if key != "binding_id"}}
        response = _call_tool(
            request_id, params, workspace, binding_id=self.binding_id, protocol=self.protocol or MCP_PROTOCOL_VERSION
        )
        self.emit(response)

    def _ask_binding(self, request_id: str | int, question: str, options: list[str] | None = None) -> None:
        """Generate internal correlation and scope identities for one binding form."""
        arguments: dict[str, Any] = {
            "decision_id": str(uuid.uuid4()),
            "scope_digest": hashlib.sha256(question.encode("utf-8")).hexdigest(),
            "question": question,
        }
        if options is not None:
            arguments["options"] = options
        super().call(request_id, {"name": "ask_user", "arguments": arguments})

    def _current_workspace(self) -> Path | None:
        """Invalidate a binding if its original path or directory identity changed."""
        if self.workspace is None:
            return None
        try:
            observed = _workspace_binding(str(self.workspace.selected))
        except (OSError, RuntimeError, ValueError):
            observed = None
        if observed != self.workspace:
            print("Bridge workspace binding invalidated: folder changed", file=self.diagnostics, flush=True)
            self.workspace = None
            self.binding_id = None
            return None
        return self.workspace.canonical

    def finish(self, decision: Decision, status: str, answer: str | None = None) -> None:
        """Advance selection to exact confirmation, or complete without invented authority."""
        if status == "answered" and self.binding_phase == "select" and answer is not None:
            try:
                self.candidate = _workspace_binding(answer)
            except (OSError, RuntimeError, ValueError) as error:
                print(f"Bridge workspace selection rejected: {error}", file=self.diagnostics, flush=True)
                status = "cancelled"
            else:
                self.pending.pop(decision.server_id, None)
                decision.receipt = {"status": "selected"}
                self.binding_phase = "confirm"
                target = PurePath(self.candidate.canonical).as_posix()
                self._ask_binding(
                    decision.call_id,
                    f"Bind Bridge calls in this process to exactly this canonical project folder: {target}",
                    ["Bind this folder", "Cancel"],
                )
                return
        if status == "answered" and self.binding_phase == "confirm" and answer == "Bind this folder":
            try:
                observed = _workspace_binding(str(self.candidate.selected)) if self.candidate else None
            except (OSError, RuntimeError, ValueError):
                observed = None
            if observed is not None and observed == self.candidate:
                self.workspace = observed
                self.binding_id = str(uuid.uuid4())
                status = "bound"
            else:
                print("Bridge workspace confirmation rejected: folder changed", file=self.diagnostics, flush=True)
                status = "cancelled"
        elif status == "answered":
            status = "cancelled"
        self.pending.pop(decision.server_id, None)
        self.binding_phase = None
        self.candidate = None
        receipt = {
            "status": status,
            "binding_status": "bound" if self.workspace is not None else "unbound",
            "binding_id": self.binding_id,
            "workspace": PurePath(self.workspace.canonical).as_posix() if self.workspace is not None else None,
        }
        decision.receipt = receipt
        self.deliver(decision.call_id, receipt)


def serve() -> int:
    """Serve strict native stdio with initially unbound workspace authority."""
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    BridgeServer(sys.stdout, sys.stderr).run(sys.stdin)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the MCP server only when the explicit stdio mode is selected."""
    parser = argparse.ArgumentParser(description="Run the bridge stdio MCP server.")
    parser.add_argument("--stdio", action="store_true", required=True)
    parser.parse_args(argv)
    return serve()


if __name__ == "__main__":
    raise SystemExit(main())
