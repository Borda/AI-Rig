"""Public contract tests for the zero-provider Bridge MCP status tool."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path, PurePath

import pytest

BIN_ROOT = Path(__file__).resolve().parents[1] / "bin"
if str(BIN_ROOT) not in sys.path:
    sys.path.insert(0, str(BIN_ROOT))

import bridge_mcp  # noqa: E402  (loaded from the installed-plugin-equivalent bin directory)

# The manifests are the release authority; status must report their version,
# so the expected value is read from the same source rather than pinned here.
MANIFEST_VERSION = json.loads(
    (Path(__file__).resolve().parents[1] / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
)["version"]

TOOL_INVENTORY = ["bridge_status", "bridge_bind_workspace", "bridge_implement", "bridge_advise", "bridge_review"]


def _status_call(workspace: Path, arguments: object | None = None) -> dict[str, object]:
    """Call bridge_status through its public JSON-RPC entrypoint; omitted arguments mean an empty object.

    Examples:
        >>> response = _status_call(getfixture("tmp_path"))
        >>> sorted(response["result"])
        ['content', 'isError']
    """
    response = bridge_mcp.handle_message(
        {
            "jsonrpc": "2.0",
            "id": "status-test",
            "method": "tools/call",
            "params": {"name": "bridge_status", "arguments": {} if arguments is None else arguments},
        },
        trusted_workspace=workspace,
    )
    assert response is not None
    return response


class TestBridgeStatus:
    """Status reports release identity and the host-bound workspace without ever reaching a provider."""

    def test_is_read_only_and_bound_to_the_host_workspace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Status is a read-only report bound to the host workspace, never a provider call or write path.

        The host trusts one workspace; a foreign folder next to it must never appear in the payload, and the payload
        must carry no cost, model, task or transcript fields that would imply provider activity.
        """
        selected_workspace = tmp_path / "selected"
        selected_workspace.mkdir()
        foreign_workspace = tmp_path / "foreign"
        foreign_workspace.mkdir()
        monkeypatch.setattr(
            bridge_mcp, "run_request", lambda *args, **kwargs: pytest.fail("bridge_status must not dispatch a provider")
        )
        monkeypatch.setattr(
            Path, "write_text", lambda *args, **kwargs: pytest.fail("bridge_status must not write filesystem state")
        )

        response = _status_call(selected_workspace)

        content = response["result"]["content"]
        assert isinstance(content, list)
        payload = json.loads(content[0]["text"])
        normalized_workspace = PurePath(selected_workspace.resolve()).as_posix()
        assert payload == {
            "bridge_version": MANIFEST_VERSION,
            "expected_tool_inventory": TOOL_INVENTORY,
            "plugin_version": MANIFEST_VERSION,
            "protocol_version": "2025-06-18",
            "schema_version": "2.0",
            "binding_status": "bound",
            "binding_id": None,
            "server": {"name": "bridge", "version": MANIFEST_VERSION},
            "workspace": normalized_workspace,
            "workspace_fingerprint": hashlib.sha256(normalized_workspace.encode("utf-8")).hexdigest(),
        }
        assert response["result"]["isError"] is False
        assert not {"cost", "model", "task", "token_count", "tokens", "transcript", "verb"} & payload.keys()
        assert PurePath(foreign_workspace.resolve()).as_posix() != payload["workspace"]

    @pytest.mark.parametrize(
        "arguments",
        [
            pytest.param({"workspace": "/untrusted"}, id="workspace-override"),
            pytest.param({"task": "No provider call."}, id="provider-task"),
            pytest.param([], id="non-object-arguments"),
        ],
    )
    def test_rejects_all_arguments_without_provider_execution(
        self, tmp_path: Path, arguments: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Any argument, including a non-object, is a protocol error and never reaches request execution.

        A model-controlled workspace override or task text would turn the status tool into a provider entry point, so
        invalid input must fail with the invalid-params code before ``run_request`` is touched.
        """
        monkeypatch.setattr(
            bridge_mcp, "run_request", lambda *args, **kwargs: pytest.fail("invalid input must not dispatch a provider")
        )

        response = _status_call(tmp_path, arguments)

        assert response["error"]["code"] == -32602
        assert "bridge_status" in response["error"]["message"]

    def test_schema_and_initialize_advertise_the_same_release_contract(self) -> None:
        """Discoverable MCP metadata matches the status response contract.

        A client reads ``initialize`` and ``tools/list`` before it ever calls status; both must report the manifest
        version, the full tool inventory and an input schema that accepts no properties.
        """
        initialized = bridge_mcp.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        definitions = {tool["name"]: tool for tool in bridge_mcp.tool_definitions()}

        assert initialized is not None
        assert initialized["result"]["serverInfo"] == {"name": "bridge", "version": MANIFEST_VERSION}
        assert set(definitions) == set(TOOL_INVENTORY)
        assert definitions["bridge_status"]["inputSchema"] == {
            "additionalProperties": False,
            "properties": {},
            "type": "object",
        }
