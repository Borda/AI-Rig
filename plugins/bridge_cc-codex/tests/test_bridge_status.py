"""Public contract tests for the zero-provider Bridge MCP status tool."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePath
import sys

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


def _status_call(workspace: Path, arguments: object = {}) -> dict[str, object]:
    """Call bridge_status through its public JSON-RPC entrypoint.

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
            "params": {"name": "bridge_status", "arguments": arguments},
        },
        trusted_workspace=workspace,
    )
    assert response is not None
    return response


def test_bridge_status_is_read_only_and_bound_to_the_host_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prevent status from becoming a provider call, write path, or model-selected workspace escape."""
    selected_workspace = tmp_path / "selected"
    selected_workspace.mkdir()
    foreign_workspace = tmp_path / "foreign"
    foreign_workspace.mkdir()

    monkeypatch.setattr(
        bridge_mcp,
        "run_request",
        lambda *args, **kwargs: pytest.fail("bridge_status must not dispatch a provider request"),
    )
    monkeypatch.setattr(
        Path,
        "write_text",
        lambda *args, **kwargs: pytest.fail("bridge_status must not write filesystem state"),
    )

    response = _status_call(selected_workspace)

    content = response["result"]["content"]
    assert isinstance(content, list)
    payload = json.loads(content[0]["text"])
    canonical_workspace = selected_workspace.resolve()
    normalized_workspace = PurePath(canonical_workspace).as_posix()
    assert payload == {
        "bridge_version": MANIFEST_VERSION,
        "expected_tool_inventory": ["bridge_status", "bridge_implement", "bridge_advise", "bridge_review"],
        "plugin_version": MANIFEST_VERSION,
        "protocol_version": "2024-11-05",
        "schema_version": "1.0",
        "server": {"name": "bridge", "version": MANIFEST_VERSION},
        "workspace": normalized_workspace,
        "workspace_fingerprint": hashlib.sha256(normalized_workspace.encode("utf-8")).hexdigest(),
    }
    assert response["result"]["isError"] is False
    assert (
        not {
            "cost",
            "model",
            "task",
            "token_count",
            "tokens",
            "transcript",
            "verb",
        }
        & payload.keys()
    )
    assert PurePath(foreign_workspace.resolve()).as_posix() != payload["workspace"]


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param({"workspace": "/untrusted"}, id="workspace-override"),
        pytest.param({"task": "No provider call."}, id="provider-task"),
        pytest.param([], id="non-object-arguments"),
    ],
)
def test_bridge_status_rejects_all_arguments_without_provider_execution(
    tmp_path: Path, arguments: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prevent status from accepting a model-controlled input or falling into request execution."""
    monkeypatch.setattr(
        bridge_mcp,
        "run_request",
        lambda *args, **kwargs: pytest.fail("invalid bridge_status input must not dispatch a provider request"),
    )

    response = _status_call(tmp_path, arguments)

    assert response["error"]["code"] == -32602
    assert "bridge_status" in response["error"]["message"]


def test_bridge_status_schema_and_initialize_advertise_the_same_release_contract() -> None:
    """Keep discoverable MCP metadata aligned with the public status response contract."""
    initialized = bridge_mcp.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    definitions = {tool["name"]: tool for tool in bridge_mcp.tool_definitions()}

    assert initialized is not None
    assert initialized["result"]["serverInfo"] == {"name": "bridge", "version": MANIFEST_VERSION}
    assert set(definitions) == {
        "bridge_status",
        "bridge_implement",
        "bridge_advise",
        "bridge_review",
    }
    assert definitions["bridge_status"]["inputSchema"] == {
        "additionalProperties": False,
        "properties": {},
        "type": "object",
    }
