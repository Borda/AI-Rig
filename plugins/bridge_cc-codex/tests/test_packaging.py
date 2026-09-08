"""Acceptance checks for the bridge's install-shaped package boundary."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PLUGIN_ROOT.parents[1]
BUILD_SCRIPT = PLUGIN_ROOT / "scripts" / "build_package.py"
VALIDATE_SCRIPT = PLUGIN_ROOT / "scripts" / "validate_package.py"

if str(PLUGIN_ROOT / "bin") not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT / "bin"))

import bridge_diagnose  # noqa: E402  (loaded from the installed-plugin-equivalent bin directory)

_VALIDATE_SPECIFICATION = importlib.util.spec_from_file_location("bridge_validate_package", VALIDATE_SCRIPT)
assert _VALIDATE_SPECIFICATION is not None and _VALIDATE_SPECIFICATION.loader is not None
validate_package = importlib.util.module_from_spec(_VALIDATE_SPECIFICATION)
_VALIDATE_SPECIFICATION.loader.exec_module(validate_package)


@pytest.mark.integration
@pytest.mark.packaging
def test_build_and_validate_use_only_the_disposable_package_copy(tmp_path: Path) -> None:
    """Prove package validation resolves payload paths without source-tree context."""
    output = tmp_path / "bridge"
    built = subprocess.run(
        [sys.executable, str(BUILD_SCRIPT), "--output", str(output)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert built.returncode == 0, built.stderr

    validated = subprocess.run(
        [sys.executable, str(VALIDATE_SCRIPT), str(output)],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert validated.returncode == 0, validated.stderr
    assert ".temp" not in {path.name for path in output.iterdir()}
    assert ".reports" not in {path.name for path in output.iterdir()}
    assert not any(path.name == ".DS_Store" for path in output.rglob("*"))
    assert not (output / "tests").exists()
    assert (output / "claude-skills" / "implement" / "SKILL.md").is_file()
    assert (output / "codex-skills" / "implement" / "SKILL.md").is_file()
    assert (output / "bin" / "bridge_setup.py").is_file()
    assert (output / "schemas" / "setup-result.schema.json").is_file()


@pytest.mark.packaging
def test_mcp_server_resolves_from_installed_plugin_root() -> None:
    """Reject source-tree-relative MCP commands that break cache installs."""
    config = json.loads((PLUGIN_ROOT / ".mcp.json").read_text(encoding="utf-8"))
    server = config["mcpServers"]["bridge"]
    assert server["command"] in {"python", "python3"}
    assert server["args"][0] == "${PLUGIN_ROOT}/bin/bridge_mcp.py"
    assert "cwd" not in server


@pytest.mark.packaging
def test_source_package_validation_ignores_non_payload_test_and_cache_files() -> None:
    """Keep the validator's documented default useful in a development checkout."""
    validated = subprocess.run(
        [sys.executable, str(VALIDATE_SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert validated.returncode == 0, validated.stderr
    assert "Package validation passed" in validated.stdout


@pytest.mark.packaging
def test_host_manifests_select_disjoint_skill_surfaces() -> None:
    """Prevent either host from discovering the other host's execution instructions."""
    claude = json.loads((PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    codex = json.loads((PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))

    assert claude["name"] == "bridge"
    assert codex["name"] == "bridge"
    # Version EQUALITY across the two host manifests is enforced repo-wide by the
    # check-plugin-version-sync pre-commit hook (check_plugin_version_sync.py) —
    # no hardcoded version literal here, which broke on every legitimate bump.
    assert codex["interface"]["displayName"] == "bridge_CC-Codex"
    assert claude["skills"] == "./claude-skills/"
    assert codex["skills"] == "./codex-skills/"
    assert {path.parent.name for path in (PLUGIN_ROOT / "claude-skills").glob("*/SKILL.md")} == {
        "advise",
        "cancel",
        "implement",
        "result",
        "review",
        "setup",
        "status",
    }
    assert {path.parent.name for path in (PLUGIN_ROOT / "codex-skills").glob("*/SKILL.md")} == {
        "advise",
        "implement",
        "review",
        "setup",
    }


@pytest.mark.packaging
def test_repository_marketplaces_advertise_both_host_installations() -> None:
    """Prevent a complete bridge package from becoming undiscoverable in either host catalog."""
    claude_marketplace = json.loads(
        (REPOSITORY_ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
    )
    codex_marketplace = json.loads(
        (REPOSITORY_ROOT / ".agents" / "plugins" / "marketplace.json").read_text(encoding="utf-8")
    )

    claude_entry = next(entry for entry in claude_marketplace["plugins"] if entry["name"] == "bridge")
    codex_entry = next(entry for entry in codex_marketplace["plugins"] if entry["name"] == "bridge")

    assert claude_entry == {
        "description": "bridge_CC-Codex: guided setup plus bounded implementation, advice, and review across Claude Code and Codex.",
        "name": "bridge",
        "source": "./plugins/bridge_cc-codex",
    }
    assert codex_entry == {
        "category": "Productivity",
        "name": "bridge",
        "policy": {"authentication": "ON_INSTALL", "installation": "AVAILABLE"},
        "source": {"path": "./plugins/bridge_cc-codex", "source": "local"},
    }


@pytest.mark.packaging
def test_disposable_package_has_no_nested_marketplace(tmp_path: Path) -> None:
    """The disposable package must not carry a redundant nested marketplace."""
    output = tmp_path / "bridge"
    built = subprocess.run(
        [sys.executable, str(BUILD_SCRIPT), "--output", str(output)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert built.returncode == 0, built.stderr
    assert not (output / ".claude-plugin" / "marketplace.json").exists()


@pytest.mark.packaging
def test_diagnose_payload_fingerprint_stays_inside_the_validated_package_manifest() -> None:
    """Keep the doctor's completeness fingerprint aligned with the validated package manifest.

    The two file lists are maintained by hand in parallel; a runtime file added to the package gate but not the payload
    list silently escapes the completeness fingerprint that sync trusts, as ``.mcp.json`` and the CLI baseline once did.
    """
    payload = set(bridge_diagnose.PAYLOAD_FILES)

    assert payload <= set(validate_package.REQUIRED_FILES)
    assert {".mcp.json", "rules/cli-baseline.json"} <= payload
