"""Exercise native question elicitation through a real subprocess stdio client."""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

SERVER = Path(__file__).resolve().parents[1] / "shared" / "user_questions_mcp.py"
ARGUMENTS = {"decision_id": "decision-1", "scope_digest": "a" * 64, "question": "Choose an answer."}


class Client:
    """Exchange newline-delimited JSON with an actual server process with bounded waits."""

    def __init__(self, server: Path = SERVER) -> None:
        """Start the selected local provider and drain stdout into a portable message queue."""
        self.process = subprocess.Popen(
            [sys.executable, str(server), "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        self.messages: queue.Queue[str] = queue.Queue()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        """Drain stdout without platform-specific file descriptor polling."""
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.messages.put(line)

    def send(self, message: dict[str, Any] | str) -> None:
        """Send one client message or a deliberately malformed raw JSON line."""
        assert self.process.stdin is not None
        self.process.stdin.write((message if isinstance(message, str) else json.dumps(message)) + "\n")
        self.process.stdin.flush()

    def receive(self) -> dict[str, Any]:
        """Read the next protocol message or fail with a bounded timeout."""
        return json.loads(self.messages.get(timeout=5))

    def request(self, request_id: str | int, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a client JSON-RPC request with an explicit correlation identifier."""
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})

    def initialize(self, protocol: str = "2025-11-25", capability: Any = None) -> dict[str, Any]:
        """Negotiate the protocol and client form support, then notify initialization."""
        self.request(
            1,
            "initialize",
            {"protocolVersion": protocol, "capabilities": {"elicitation": {} if capability is None else capability}},
        )
        result = self.receive()
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return result

    def call(self, request_id: int = 3, arguments: dict[str, Any] | None = None) -> None:
        """Invoke the public tool with visible decision arguments."""
        self.request(request_id, "tools/call", {"name": "ask_user", "arguments": arguments or ARGUMENTS})

    def answer(self, request_id: Any, result: Any) -> None:
        """Return a client elicitation result to a server request identifier."""
        self.send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def close(self) -> None:
        """Close input, collect process exit, and release pipe handles even on failure."""
        assert self.process.stdin is not None
        if not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.reader.join(timeout=5)
        for stream in (self.process.stdout, self.process.stderr):
            if stream is not None:
                stream.close()


@contextmanager
def client(server: Path = SERVER) -> Iterator[Client]:
    """Own the server process lifetime for one independent protocol scenario."""
    instance = Client(server)
    try:
        yield instance
    finally:
        instance.close()


@pytest.mark.parametrize(
    ("protocol", "capability", "options", "answer"),
    [
        pytest.param("2025-06-18", {}, None, "Approve", id="legacy-free-text"),
        pytest.param(
            "2025-11-25",
            {"form": {}},
            ["Approve (Recommended)", "Decline"],
            "Approve (Recommended)",
            id="form-closed-choice",
        ),
    ],
)
def test_handshake_discovery_and_explicit_acceptance(
    protocol: str, capability: dict[str, Any], options: list[str] | None, answer: str
) -> None:
    """Require a matched client acceptance and preserve its exact answer and decision scope."""
    with client() as connection:
        assert connection.initialize(protocol, capability)["result"]["protocolVersion"] == protocol
        connection.request(2, "tools/list")
        tools = connection.receive()["result"]["tools"]
        assert [tool["name"] for tool in tools] == ["ask_user"]
        assert "runtime permission" in tools[0]["description"]
        arguments = dict(ARGUMENTS)
        if options is not None:
            arguments["options"] = options
        connection.call(arguments=arguments)
        form = connection.receive()
        assert form["method"] == "elicitation/create"
        assert form["params"]["message"] == arguments["question"]
        schema = form["params"]["requestedSchema"]
        assert schema["required"] == ["answer"]
        assert schema["properties"]["answer"]["type"] == "string"
        assert schema["properties"]["answer"].get("enum") == options
        assert "default" not in schema["properties"]["answer"]
        assert form["params"].get("mode") == ("form" if protocol == "2025-11-25" else None)
        connection.answer(form["id"], {"action": "accept", "content": {"answer": answer}})
        result = connection.receive()
        assert result["id"] == 3
        receipt = result["result"]["structuredContent"]
        assert receipt == {
            "decision_id": "decision-1",
            "scope_digest": "a" * 64,
            "status": "answered",
            "answer": answer,
        }
        assert json.loads(result["result"]["content"][0]["text"]) == receipt
        assert "question" not in receipt and "options" not in receipt


@pytest.mark.parametrize(
    ("action", "status"),
    [
        pytest.param("decline", "declined", id="explicit-decline"),
        pytest.param("cancel", "cancelled", id="explicit-cancel"),
    ],
)
def test_nonacceptance_never_returns_answer(action: str, status: str) -> None:
    """Decline and cancellation cannot become consent even with supplied answer content."""
    with client() as connection:
        connection.initialize()
        connection.call()
        form = connection.receive()
        connection.answer(form["id"], {"action": action, "content": {"answer": "Approve"}})
        receipt = connection.receive()["result"]["structuredContent"]
        assert receipt["status"] == status
        assert "answer" not in receipt


@pytest.mark.parametrize("ending", ["eof", "notification"])
def test_pending_cancellation_and_end_of_input(ending: str) -> None:
    """A lost or cancelled client call yields a cancelled receipt with no fabricated answer."""
    with client() as connection:
        connection.initialize()
        connection.call()
        form = connection.receive()
        if ending == "eof":
            assert connection.process.stdin is not None
            connection.process.stdin.close()
        else:
            connection.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 3}})
        receipt = connection.receive()["result"]["structuredContent"]
        assert receipt["status"] == "cancelled"
        assert "answer" not in receipt
        if ending == "notification":
            assert connection.receive()["params"]["requestId"] == form["id"]


@pytest.mark.parametrize(
    "capability", [False, pytest.param({"url": {}}, id="url-only"), pytest.param({"form": False}, id="invalid-form")]
)
def test_unsupported_elicitation_fails_before_form(capability: Any) -> None:
    """Only advertised native form support permits a client UI request."""
    with client() as connection:
        connection.initialize(capability=capability)
        connection.call()
        assert connection.receive()["error"]["code"] == -32002
        connection.request(4, "ping")
        assert connection.receive() == {"jsonrpc": "2.0", "id": 4, "result": {}}


def test_mismatched_response_ping_and_completed_replay() -> None:
    """An unmatched answer cannot resolve a decision and completed replay cannot open another form."""
    with client() as connection:
        connection.initialize()
        connection.call()
        form = connection.receive()
        connection.answer("unmatched", {"action": "accept", "content": {"answer": "wrong"}})
        connection.request(4, "ping")
        assert connection.receive()["id"] == 4
        connection.answer(form["id"], {"action": "accept", "content": {"answer": "Approve"}})
        original = connection.receive()["result"]
        connection.answer(form["id"], {"action": "accept", "content": {"answer": "changed"}})
        connection.call(5)
        replay = connection.receive()
        assert replay["id"] == 5
        assert replay["result"] == original
        connection.request(6, "unknown")
        assert connection.receive()["error"]["code"] == -32601


def test_pending_duplicate_and_scope_reuse_fail_closed() -> None:
    """Duplicate pending calls and altered identities cannot request a second form."""
    with client() as connection:
        connection.initialize()
        connection.call()
        form = connection.receive()
        connection.call(4)
        assert connection.receive()["error"]["code"] == -32001
        connection.call(5, {**ARGUMENTS, "scope_digest": "b" * 64})
        assert connection.receive()["error"]["code"] == -32602
        connection.answer(form["id"], {"action": "accept", "content": {"answer": "Approve"}})
        assert connection.receive()["id"] == 3
        connection.call(6, {**ARGUMENTS, "question": "Changed question"})
        assert connection.receive()["error"]["code"] == -32602


@pytest.mark.parametrize(
    "result",
    [
        pytest.param({"action": "accept", "content": {"answer": ""}}, id="empty-answer"),
        pytest.param({"action": "accept", "content": {"answer": "Other"}}, id="outside-closed-choice"),
        pytest.param({"action": "accept", "content": {"answer": True}}, id="wrong-answer-type"),
        pytest.param({"action": "accept"}, id="missing-content"),
        pytest.param({"action": "accept", "content": {"answer": "Approve", "extra": "value"}}, id="extra-content"),
        pytest.param({"content": {"answer": "Approve"}}, id="missing-explicit-action"),
    ],
)
def test_malformed_answer_never_authorizes(result: dict[str, Any]) -> None:
    """Malformed or out-of-domain content cancels without returning a purported answer."""
    with client() as connection:
        connection.initialize()
        connection.call(arguments={**ARGUMENTS, "options": ["Approve", "Decline"]})
        form = connection.receive()
        connection.answer(form["id"], result)
        receipt = connection.receive()["result"]["structuredContent"]
        assert receipt["status"] == "cancelled"
        assert "answer" not in receipt


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param({**ARGUMENTS, "extra": True}, id="unknown-property"),
        pytest.param({**ARGUMENTS, "scope_digest": "A" * 64}, id="uppercase-scope"),
        pytest.param({**ARGUMENTS, "question": " "}, id="blank-question"),
        pytest.param({**ARGUMENTS, "decision_id": "x" * 129}, id="oversized-identity"),
        pytest.param({**ARGUMENTS, "options": []}, id="empty-options"),
        pytest.param({**ARGUMENTS, "options": ["Approve", "Approve"]}, id="duplicate-options"),
        pytest.param({**ARGUMENTS, "options": [{"label": "Approve"}]}, id="nonstring-options"),
    ],
)
def test_invalid_arguments_fail_without_form(arguments: dict[str, Any]) -> None:
    """Invalid argument shapes return a tool protocol error before eliciting any answer."""
    with client() as connection:
        connection.initialize()
        connection.call(arguments=arguments)
        assert connection.receive()["error"]["code"] == -32602
        connection.request(4, "ping")
        assert connection.receive()["id"] == 4


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        pytest.param("{", -32700, id="invalid-json"),
        pytest.param('{"jsonrpc":"2.0","id":1,"id":2,"method":"ping"}', -32700, id="duplicate-json-key"),
        pytest.param('{"jsonrpc":"2.0","id":NaN,"method":"ping"}', -32700, id="nonstandard-number"),
        pytest.param('{"jsonrpc":"2.0","id":true,"method":"ping"}', -32600, id="boolean-id"),
        pytest.param('{"jsonrpc":"2.0","id":null,"method":"ping"}', -32600, id="null-id"),
        pytest.param("[]", -32600, id="batch-not-supported"),
    ],
)
def test_invalid_transport_remains_usable(raw: str, code: int) -> None:
    """Malformed transport input cannot crash or consume the next valid request."""
    with client() as connection:
        connection.send(raw)
        assert connection.receive()["error"]["code"] == code
        connection.request(4, "ping")
        assert connection.receive()["id"] == 4


def test_utf8_question_answer_and_invalid_cancellation_identifier() -> None:
    """Preserve native UTF-8 text and reject boolean cancellation IDs before matching."""
    with client() as connection:
        connection.initialize()
        connection.call(7, {**ARGUMENTS, "question": "Jaká je odpověď?"})
        form = connection.receive()
        assert form["params"]["message"] == "Jaká je odpověď?"
        connection.request(8, "notifications/cancelled", {"requestId": True})
        assert connection.receive()["error"]["code"] == -32602
        connection.answer(form["id"], {"action": "accept", "content": {"answer": "Schváleno žluťoučké"}})
        receipt = connection.receive()["result"]["structuredContent"]
        assert receipt["status"] == "answered"
        assert receipt["answer"] == "Schváleno žluťoučké"


def test_pending_capacity_bounds_and_live_calls_are_preserved() -> None:
    """Capacity exhaustion fails locally while earlier calls remain independently answerable."""
    with client() as connection:
        connection.initialize()
        forms = []
        for index in range(16):
            connection.call(index + 10, {**ARGUMENTS, "decision_id": f"decision-{index}"})
            forms.append(connection.receive())
        connection.call(30, {**ARGUMENTS, "decision_id": "overflow"})
        assert connection.receive()["error"]["code"] == -32003
        for index, form in enumerate(forms):
            connection.answer(form["id"], {"action": "decline"})
            receipt = connection.receive()
            assert receipt["id"] == index + 10
            assert receipt["result"]["structuredContent"]["status"] == "declined"
        connection.call(31, {**ARGUMENTS, "decision_id": "overflow"})
        assert connection.receive()["method"] == "elicitation/create"


@pytest.mark.parametrize(
    "metadata", [None, pytest.param({"answer": "Do not trust this"}, id="metadata-cannot-authorize")]
)
def test_standard_result_metadata_preserves_explicit_answer(metadata: Any) -> None:
    """Accept optional standard metadata without using it to authorize or alter the answer."""
    with client() as connection:
        connection.initialize()
        connection.call()
        form = connection.receive()
        connection.answer(form["id"], {"action": "accept", "content": {"answer": "Approve"}, "_meta": metadata})
        receipt = connection.receive()["result"]["structuredContent"]
        assert receipt["status"] == "answered"
        assert receipt["answer"] == "Approve"
        assert "_meta" not in receipt


def test_same_pending_rpc_id_cancels_original_form_once() -> None:
    """RPC ID reuse produces one cancelled result and dismisses its outstanding native form."""
    with client() as connection:
        connection.initialize()
        connection.call()
        form = connection.receive()
        connection.call()
        terminal = connection.receive()
        assert terminal["id"] == 3
        assert terminal["result"]["structuredContent"]["status"] == "cancelled"
        assert "answer" not in terminal["result"]["structuredContent"]
        cancellation = connection.receive()
        assert cancellation == {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": form["id"]},
        }
        connection.answer(form["id"], {"action": "accept", "content": {"answer": "Approve"}})
        connection.request(4, "ping")
        assert connection.receive() == {"jsonrpc": "2.0", "id": 4, "result": {}}
        connection.call(5)
        assert connection.receive()["result"] == terminal["result"]


@pytest.mark.parametrize(
    "metadata", [None, pytest.param({"progressToken": "native-progress"}, id="standard-progress-token")]
)
def test_tool_call_standard_metadata_does_not_change_decision(metadata: Any) -> None:
    """Allow standard call metadata without adding it to the frozen decision or receipt."""
    with client() as connection:
        connection.initialize()
        connection.request(3, "tools/call", {"name": "ask_user", "arguments": ARGUMENTS, "_meta": metadata})
        form = connection.receive()
        assert form["method"] == "elicitation/create"
        connection.answer(form["id"], {"action": "accept", "content": {"answer": "Approve"}})
        receipt = connection.receive()["result"]
        connection.call(4)
        assert connection.receive()["result"] == receipt
        assert "_meta" not in receipt["structuredContent"]


@pytest.mark.parametrize(
    "server",
    [
        pytest.param(SERVER, id="codex-rig"),
        pytest.param(SERVER.parents[2] / "codemap-py/shared/user_questions_mcp.py", id="codemap"),
        pytest.param(SERVER.parents[2] / "bridge_cc-codex/bin/user_questions_mcp.py", id="bridge"),
    ],
)
def test_each_provider_copy_preserves_request_dispatch_boundaries(server: Path) -> None:
    """Copied providers retain matched answers, one handshake, and id-bearing cancellation acknowledgments."""
    assert server.read_bytes() == SERVER.read_bytes()
    with client(server) as connection:
        connection.initialize()
        connection.call()
        form = connection.receive()
        connection.answer(form["id"], {"action": "accept", "content": {"answer": "Approve"}})
        receipt = connection.receive()["result"]["structuredContent"]
        assert receipt == {
            "decision_id": "decision-1",
            "scope_digest": "a" * 64,
            "status": "answered",
            "answer": "Approve",
        }
        connection.call(4, {**ARGUMENTS, "decision_id": "decision-2"})
        pending = connection.receive()
        connection.request(5, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
        assert connection.receive() == {
            "jsonrpc": "2.0",
            "id": 5,
            "error": {"code": -32600, "message": "session already initialized"},
        }
        connection.request(6, "notifications/cancelled", {"requestId": 4})
        cancelled = connection.receive()
        assert cancelled["id"] == 4 and cancelled["result"]["structuredContent"]["status"] == "cancelled"
        assert "answer" not in cancelled["result"]["structuredContent"]
        assert connection.receive() == {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": pending["id"]},
        }
        assert connection.receive() == {"jsonrpc": "2.0", "id": 6, "result": {}}
        connection.answer(pending["id"], {"action": "accept", "content": {"answer": "Approve"}})
        connection.request(7, "ping")
        assert connection.receive() == {"jsonrpc": "2.0", "id": 7, "result": {}}
