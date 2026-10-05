"""Ask a user for an explicit answer through the client's native MCP form.

## Purpose Provide a local question tool whose answer comes exclusively from a matched client elicitation response.

## Scope This standard-library server handles newline-delimited JSON-RPC over stdio, advertises one ask_user tool,
validates bounded arguments, and retains a bounded in-memory decision cache for one process. It executes no repository
commands and reads no files, credentials, or network resources.

## Usage Launch this installed module with --stdio. Initialize with a supported protocol and client elicitation
capability before calling ask_user. Runtime permission requests must use the host permission boundary instead.

## Outputs One native form presents a required string answer, optionally restricted to closed choices. Tool receipts
retain the original decision identity and scope digest; an answer appears only after explicit client acceptance.
Diagnostics use stderr; protocol messages alone use stdout.

## Failure Unsupported capabilities, malformed arguments, changed reused decisions and capacity exhaustion fail closed.
Cancellation and EOF never produce an answer. Unmatched, duplicate or malformed responses cannot authorize a decision.

## Used by Installed Codex Rig, Codemap and Bridge question workflows use their local MCP declarations. Bridge inherits
this correlation and form lifecycle. Cached receipts are process-local and confer no persisted permissions or defaults.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from typing import Any, TextIO

PROTOCOLS = ("2025-06-18", "2025-11-25")
MAX_DECISIONS = 128
MAX_PENDING = 16
MAX_LINE = 65536
ARGUMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "decision_id": {"type": "string", "minLength": 1, "maxLength": 128},
        "scope_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "question": {"type": "string", "minLength": 1, "maxLength": 4096},
        "options": {
            "type": "array",
            "minItems": 1,
            "maxItems": 32,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 256},
        },
    },
    "required": ["decision_id", "scope_digest", "question"],
    "additionalProperties": False,
}


@dataclass
class Decision:
    """Retain a validated decision and its pending request or completed receipt."""

    arguments: dict[str, Any]
    call_id: str | int
    server_id: str
    receipt: dict[str, Any] | None = None


def _valid_id(value: Any) -> bool:
    """Recognize unambiguous string or integer JSON-RPC identifiers."""
    return (isinstance(value, str) and 0 < len(value) <= 128) or (type(value) is int and abs(value) <= 2**53 - 1)


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON keys instead of silently replacing transport values."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    """Reject nonstandard JSON numeric constants."""
    raise ValueError(f"invalid JSON constant: {value}")


def _validate_arguments(arguments: Any) -> dict[str, Any]:
    """Require a bounded decision identity, scope, question, and optional closed choices."""
    if not isinstance(arguments, dict) or set(arguments) - set(ARGUMENT_SCHEMA["properties"]):
        raise ValueError("arguments must be an object with only declared properties")
    for name, maximum in (("decision_id", 128), ("question", 4096)):
        value = arguments.get(name)
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise ValueError(f"{name} must be nonempty text of at most {maximum} characters")
    digest = arguments.get("scope_digest")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("scope_digest must be 64 lowercase hexadecimal characters")
    if "options" in arguments:
        options = arguments["options"]
        if (
            not isinstance(options, list)
            or not 1 <= len(options) <= 32
            or any(not isinstance(item, str) or not item.strip() or len(item) > 256 for item in options)
            or len(set(options)) != len(options)
        ):
            raise ValueError("options must contain 1 to 32 distinct nonempty strings of at most 256 characters")
    return arguments


class Server:
    """Correlate tool requests with explicit client answers within bounded process state."""

    def __init__(self, output: TextIO, diagnostics: TextIO) -> None:
        """Initialize transport streams and an empty process-local decision cache."""
        self.output = output
        self.diagnostics = diagnostics
        self.protocol: str | None = None
        self.elicitation = False
        self.decisions: dict[str, Decision] = {}
        self.pending: dict[str, Decision] = {}
        self.sequence = 0

    def emit(self, message: dict[str, Any]) -> None:
        """Write one JSON-RPC message and flush it for the waiting client."""
        self.output.write(json.dumps(message, ensure_ascii=True, allow_nan=False) + "\n")
        self.output.flush()

    def error(self, request_id: Any, code: int, message: str) -> None:
        """Return a protocol error without echoing untrusted question contents."""
        self.emit({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})

    def result(self, request_id: str | int, result: dict[str, Any]) -> None:
        """Return a JSON-RPC result to its owning request."""
        self.emit({"jsonrpc": "2.0", "id": request_id, "result": result})

    def finish(self, decision: Decision, status: str, answer: str | None = None) -> None:
        """Complete one pending decision without inventing an answer or consent."""
        receipt = {key: decision.arguments[key] for key in ("decision_id", "scope_digest")}
        receipt["status"] = status
        if answer is not None:
            receipt["answer"] = answer
        decision.receipt = receipt
        self.pending.pop(decision.server_id, None)
        self.deliver(decision.call_id, receipt)

    def deliver(self, request_id: str | int, receipt: dict[str, Any]) -> None:
        """Return an identical structured and text receipt to a tool caller."""
        self.result(
            request_id,
            {"structuredContent": receipt, "content": [{"type": "text", "text": json.dumps(receipt)}]},
        )

    def response(self, message: dict[str, Any]) -> None:
        """Consume only a matched, well-formed elicitation response."""
        request_id = message.get("id")
        decision = self.pending.get(request_id) if isinstance(request_id, str) else None
        if decision is None:
            print("Rejected unmatched or duplicate elicitation response", file=self.diagnostics, flush=True)
            return
        result = message.get("result")
        if set(message) != {"jsonrpc", "id", "result"} or not isinstance(result, dict):
            print("Rejected malformed elicitation response", file=self.diagnostics, flush=True)
            self.finish(decision, "cancelled")
            return
        action = result.get("action")
        metadata_valid = "_meta" not in result or result["_meta"] is None or isinstance(result["_meta"], dict)
        if action in ("decline", "cancel") and set(result) <= {"action", "content", "_meta"} and metadata_valid:
            self.finish(decision, "declined" if action == "decline" else "cancelled")
            return
        content = result.get("content")
        answer = content.get("answer") if isinstance(content, dict) else None
        if (
            action != "accept"
            or not {"action", "content"} <= set(result) <= {"action", "content", "_meta"}
            or not metadata_valid
            or not isinstance(content, dict)
            or set(content) != {"answer"}
            or not isinstance(answer, str)
            or not answer.strip()
            or len(answer) > 4096
            or ("options" in decision.arguments and answer not in decision.arguments["options"])
        ):
            print("Rejected malformed elicitation answer", file=self.diagnostics, flush=True)
            self.finish(decision, "cancelled")
            return
        self.finish(decision, "answered", answer)

    def call(self, request_id: str | int, params: Any) -> None:
        """Validate an ask_user call and emit at most one form per decision."""
        if (
            not isinstance(params, dict)
            or not {"name", "arguments"} <= set(params) <= {"name", "arguments", "_meta"}
            or ("_meta" in params and params["_meta"] is not None and not isinstance(params["_meta"], dict))
            or params.get("name") != "ask_user"
        ):
            self.error(request_id, -32602, "expected ask_user with an arguments object")
            return
        try:
            arguments = _validate_arguments(params["arguments"])
        except ValueError as error:
            self.error(request_id, -32602, str(error))
            return
        existing = self.decisions.get(arguments["decision_id"])
        if existing is not None:
            if arguments != existing.arguments:
                self.error(request_id, -32602, "decision_id already bound to different arguments or scope")
            elif existing.receipt is not None:
                self.deliver(request_id, existing.receipt)
            else:
                self.error(request_id, -32001, "decision already pending; no additional form opened")
            return
        if self.protocol is None or not self.elicitation:
            self.error(request_id, -32002, "client must initialize with form elicitation capability")
            return
        if len(self.decisions) >= MAX_DECISIONS or len(self.pending) >= MAX_PENDING:
            self.error(request_id, -32003, "decision capacity exhausted; start a new session")
            return
        self.sequence += 1
        server_id = f"question-{self.sequence}"
        decision = Decision(arguments, request_id, server_id)
        self.decisions[arguments["decision_id"]] = decision
        self.pending[server_id] = decision
        answer_schema: dict[str, Any] = {"type": "string", "minLength": 1, "maxLength": 4096}
        if "options" in arguments:
            answer_schema = {"type": "string", "enum": arguments["options"]}
        elicitation = {
            "message": arguments["question"],
            "requestedSchema": {"type": "object", "properties": {"answer": answer_schema}, "required": ["answer"]},
        }
        if self.protocol == "2025-11-25":
            elicitation["mode"] = "form"
        self.emit({"jsonrpc": "2.0", "id": server_id, "method": "elicitation/create", "params": elicitation})

    def _cancel(self, request_id: str | int | None, params: dict[str, Any]) -> None:
        """Cancel a matched caller and its native form without fabricating an answer."""
        if not _valid_id(params.get("requestId")):
            if request_id is not None:
                self.error(request_id, -32602, "cancellation requires a valid requestId")
            else:
                print("Rejected invalid cancellation identifier", file=self.diagnostics, flush=True)
            return
        for decision in tuple(self.pending.values()):
            if params.get("requestId") == decision.call_id:
                self.finish(decision, "cancelled")
                self.emit(
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/cancelled",
                        "params": {"requestId": decision.server_id},
                    }
                )
        if request_id is not None:
            self.result(request_id, {})

    def _initialize(self, request_id: str | int, params: dict[str, Any]) -> None:
        """Negotiate one session's protocol and explicit client form capability."""
        if self.protocol is not None:
            self.error(request_id, -32600, "session already initialized")
            return
        capabilities = params.get("capabilities", {})
        capability = capabilities.get("elicitation") if isinstance(capabilities, dict) else None
        self.elicitation = isinstance(capability, dict) and (
            capability == {} or isinstance(capability.get("form"), dict)
        )
        requested = params.get("protocolVersion")
        self.protocol = requested if requested in PROTOCOLS else PROTOCOLS[-1]
        self.result(
            request_id,
            {
                "protocolVersion": self.protocol,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "user-questions", "version": "1.0.0"},
            },
        )

    def _request(self, method: str, request_id: str | int | None, params: dict[str, Any]) -> None:
        """Route validated requests while preserving pending caller IDs and notification acknowledgments."""
        if method == "notifications/cancelled":
            self._cancel(request_id, params)
            return
        if request_id is None:
            return
        for decision in tuple(self.pending.values()):
            if decision.call_id == request_id:
                # A second reply with this ID could resolve the original caller incorrectly.
                # Finish its one receipt and explicitly dismiss the outstanding native form.
                self.finish(decision, "cancelled")
                self.emit(
                    {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": decision.server_id}}
                )
                return
        if method == "initialize":
            self._initialize(request_id, params)
        elif method in ("ping", "notifications/initialized"):
            self.result(request_id, {})
        elif method == "tools/list":
            self.result(
                request_id,
                {
                    "tools": [
                        {
                            "name": "ask_user",
                            "description": "Ask through a native form. Use free text for arbitrary grammar; options for closed values. Never use for runtime permission requests.",
                            "inputSchema": ARGUMENT_SCHEMA,
                        }
                    ]
                },
            )
        elif method == "tools/call":
            self.call(request_id, params)
        else:
            self.error(request_id, -32601, "method not found")

    def handle(self, message: Any) -> None:
        """Validate transport envelopes before dispatching client requests or correlated responses."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            self.error(None, -32600, "invalid JSON-RPC object")
            return
        if "id" in message and not _valid_id(message["id"]):
            self.error(None, -32600, "id must be a bounded string or integer")
            return
        if "method" not in message:
            self.response(message)
            return
        method = message["method"]
        request_id = message.get("id")
        if not isinstance(method, str) or not method or set(message) - {"jsonrpc", "id", "method", "params"}:
            self.error(request_id, -32600, "invalid request")
            return
        params = message.get("params", {})
        if not isinstance(params, dict):
            if request_id is not None:
                self.error(request_id, -32602, "params must be an object")
            return
        self._request(method, request_id, params)

    def run(self, source: TextIO) -> None:
        """Read bounded JSON lines and cancel every unresolved decision at end of input."""
        while line := source.readline(MAX_LINE + 1):
            if len(line) > MAX_LINE:
                while line and not line.endswith("\n"):
                    line = source.readline(MAX_LINE + 1)
                self.error(None, -32700, "message exceeds transport limit")
                continue
            try:
                message = json.loads(line, object_pairs_hook=_object, parse_constant=_invalid_constant)
            except (ValueError, RecursionError):
                self.error(None, -32700, "invalid JSON")
                continue
            self.handle(message)
        for decision in tuple(self.pending.values()):
            self.finish(decision, "cancelled")


def main(argv: list[str] | None = None) -> int:
    """Launch only the portable stdio transport selected by the caller."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stdio", required=True, action="store_true")
    parser.parse_args(argv)
    # MCP stdio is UTF-8 even when a native Windows console uses another encoding.
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    Server(sys.stdout, sys.stderr).run(sys.stdin)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
