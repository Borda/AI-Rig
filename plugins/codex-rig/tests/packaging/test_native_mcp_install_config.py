"""Check copied question providers against the native plugin working-directory contract."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_PLUGINS = Path(__file__).resolve().parents[3]


@pytest.mark.packaging
@pytest.mark.integration
@pytest.mark.parametrize(
    ("plugin", "server_name", "provider"),
    [
        pytest.param("codex-rig", "codex-rig-input", "shared/user_questions_mcp.py", id="codex-rig"),
        pytest.param("codemap-py", "codemap-input", "shared/user_questions_mcp.py", id="codemap"),
        pytest.param("bridge_cc-codex", "bridge-input", "bin/user_questions_mcp.py", id="bridge"),
    ],
)
def test_copied_native_question_config_initializes_from_plugin_root(
    tmp_path: Path, plugin: str, server_name: str, provider: str
) -> None:
    """A relocated plugin must initialize without shell expansion or session-relative paths."""
    source = _PLUGINS / plugin
    config = json.loads((source / ".codex-mcp.json").read_text(encoding="utf-8"))["mcpServers"][server_name]
    assert config["command"] == "python"
    assert config["cwd"] == "."
    assert config["args"] == [provider, "--stdio"]
    installed = tmp_path / "cache" / plugin / "installed"
    target = installed / provider
    target.parent.mkdir(parents=True)
    shutil.copyfile(source / provider, target)
    initialize = {
        "jsonrpc": "2.0",
        "id": "installed-init",
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "clientInfo": {"name": "copied-plugin-test", "version": "1"},
            "capabilities": {"elicitation": {"form": {}}},
        },
    }
    # The native plugin parser resolves configured relative cwd against the installed root.
    result = subprocess.run(
        [sys.executable, *config["args"]],
        cwd=installed / config["cwd"],
        input=json.dumps(initialize) + "\n",
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    response = json.loads(result.stdout)
    assert response["id"] == "installed-init"
    assert response["result"]["protocolVersion"] == "2025-06-18"
    assert response["result"]["serverInfo"]["name"] == "user-questions"


@pytest.mark.packaging
def test_existing_bridge_server_uses_native_plugin_working_directory() -> None:
    """Keep the existing Bridge server on the same supported relocation contract."""
    config = json.loads((_PLUGINS / "bridge_cc-codex/.codex-mcp.json").read_text(encoding="utf-8"))
    server = config["mcpServers"]["bridge"]
    assert server["cwd"] == "."
    assert server["args"] == ["bin/bridge_mcp.py", "--stdio"]
