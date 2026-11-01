"""Check copied question providers against the native plugin working-directory contract."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_PLUGINS = Path(__file__).resolve().parents[4]
INITIALIZE_REQUEST = {
    "jsonrpc": "2.0",
    "id": "installed-init",
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "clientInfo": {"name": "copied-plugin-test", "version": "1"},
        "capabilities": {"elicitation": {"form": {}}},
    },
}
#: Question-provider servers shipped by each plugin: (plugin, server name, provider script relative to the plugin root).
QUESTION_PROVIDERS = [
    pytest.param("codex-rig", "codex-rig-input", "shared/user_questions_mcp.py", id="codex-rig"),
    pytest.param("codemap-py", "codemap-input", "shared/user_questions_mcp.py", id="codemap"),
    pytest.param("bridge_cc-codex", "bridge-input", "bin/user_questions_mcp.py", id="bridge"),
]


def _server_config(plugin: str, server_name: str) -> dict[str, object]:
    """Return one server entry from the plugin's shipped ``.codex-mcp.json``."""
    config = json.loads((_PLUGINS / plugin / ".codex-mcp.json").read_text(encoding="utf-8"))
    return config["mcpServers"][server_name]


@pytest.mark.packaging
@pytest.mark.parametrize(
    ("plugin", "server_name", "script"),
    [
        *QUESTION_PROVIDERS,
        pytest.param("bridge_cc-codex", "bridge", "bin/bridge_mcp.py", id="bridge-server"),
    ],
)
def test_server_config_runs_script_from_plugin_root(plugin: str, server_name: str, script: str) -> None:
    """Every shipped native MCP server starts ``python <script> --stdio`` from the plugin root.

    The plugin parser resolves a relative ``cwd`` against the installed root, so ``cwd`` must be ``.`` and the script
    path must be plugin-relative. This covers the three question providers and the existing Bridge server, keeping them
    on one supported relocation contract.
    """
    config = _server_config(plugin, server_name)

    assert config["command"] == "python"
    assert config["cwd"] == "."
    assert config["args"] == [script, "--stdio"]


@pytest.mark.packaging
@pytest.mark.integration
@pytest.mark.parametrize(("plugin", "server_name", "provider"), QUESTION_PROVIDERS)
def test_copied_native_question_config_initializes_from_plugin_root(
    tmp_path: Path, plugin: str, server_name: str, provider: str
) -> None:
    """A relocated question provider answers ``initialize`` without shell expansion or session-relative paths.

    Only the provider script is copied into a fresh "installed" directory and started with the configured command line
    from the configured working directory, as the native plugin parser would; the reply must identify the server.
    """
    config = _server_config(plugin, server_name)
    installed = tmp_path / "cache" / plugin / "installed"
    target = installed / provider
    target.parent.mkdir(parents=True)
    shutil.copyfile(_PLUGINS / plugin / provider, target)

    result = subprocess.run(
        [sys.executable, *config["args"]],
        cwd=installed / config["cwd"],
        input=json.dumps(INITIALIZE_REQUEST) + "\n",
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
