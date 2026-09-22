#!/usr/bin/env python3
"""Validate an installed-shape bridge plugin copy without dependencies.

Purpose:
    Check the bridge's two host manifests, MCP declaration, runtime closure,
    contract artifacts, and asset paths against one install-shaped directory.

Scope:
    This is a read-only local package gate. It validates JSON and Markdown
    payload structure and rejects symlinks or private artifact directories; it
    does not build, install, authenticate, invoke a host CLI, or publish.

Usage:
    Run ``python scripts/validate_package.py [PACKAGE_ROOT]``. The default is
    the source plugin directory, while a disposable build output can be passed
    explicitly to prove installed-path closure.

Outputs:
    A concise success line naming the validated root. Failures identify the
    missing or malformed relative path that breaks package closure.

Failure:
    Missing manifests, unsupported keys, mismatched identity, unresolved MCP
    server paths, hooks, symlinks, invalid JSON, or absent contract artifacts
    return a non-zero status.

Used by:
    Bridge packaging tests and local release gates validate both the source
    checkout and the output of ``build_package.py`` with this helper.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
CLAUDE_KEYS = {
    "author",
    "description",
    "homepage",
    "keywords",
    "license",
    "name",
    "repository",
    "skills",
    "version",
}
CODEX_KEYS = {
    "author",
    "description",
    "homepage",
    "interface",
    "keywords",
    "license",
    "mcpServers",
    "name",
    "repository",
    "skills",
    "version",
}
INTERFACE_KEYS = {
    "brandColor",
    "capabilities",
    "category",
    "composerIcon",
    "defaultPrompt",
    "developerName",
    "displayName",
    "logo",
    "longDescription",
    "shortDescription",
    "websiteURL",
}
REQUIRED_FILES = {
    ".claude-plugin/plugin.json",
    ".codex-plugin/plugin.json",
    ".codex-mcp.json",
    "assets/bridge.svg",
    "CHANGELOG.md",
    "LICENSE",
    "NOTICE",
    "README.md",
    "docs/architecture.md",
    "docs/development.md",
    "docs/operations.md",
    "docs/security.md",
    "schemas/envelope.schema.json",
    "schemas/harness-envelope.schema.json",
    "schemas/mcp-tools.schema.json",
    "schemas/setup-result.schema.json",
    "rules/cli-baseline.json",
    "rules/envelope.md",
    "rules/escalation-policy.md",
    "rules/prompting.md",
    "rules/recursion-guard.md",
    "rules/self-healing.md",
    "bin/bridge_call.py",
    "bin/bridge_diagnose.py",
    "bin/bridge_mcp.py",
    "bin/bridge_setup.py",
    "claude-skills/implement/SKILL.md",
    "codex-skills/implement/SKILL.md",
}
PLUGIN_PATH_PATTERN = re.compile(r"\$\{(?:CLAUDE_)?PLUGIN_ROOT\}/bin/([A-Za-z0-9_.-]+)")
PRIVATE_ABSOLUTE_PATTERN = re.compile(rb"/(?:Users|home)/[^/\s]+|[A-Za-z]:[\\/]Users[\\/]+[^\\/\s]+")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse an optional package root."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package_root", nargs="?", type=Path, default=PACKAGE_ROOT)
    return parser.parse_args(argv)


def _load_object(path: Path) -> dict[str, Any]:
    """Read one JSON object and identify its relative path on failure."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid JSON: {path}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"JSON object required: {path}")
    return payload


def _relative_file(root: Path, value: object, *, field: str) -> Path:
    """Resolve one manifest-relative file path without traversal."""
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"{field} must be a relative POSIX path")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"{field} must stay inside the plugin")
    resolved = root / relative
    if not resolved.is_file() or resolved.is_symlink():
        raise ValueError(f"{field} does not resolve to a regular file: {value}")
    return relative


def _relative_directory(root: Path, value: object, *, field: str) -> Path:
    """Resolve one manifest-relative directory without traversal."""
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"{field} must be a relative POSIX path")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"{field} must stay inside the plugin")
    resolved = root / relative
    if not resolved.is_dir() or resolved.is_symlink():
        raise ValueError(f"{field} does not resolve to a regular directory: {value}")
    return relative


def _validate_manifests(root: Path) -> str:
    """Validate matching Claude and Codex identities and interface paths."""
    claude = _load_object(root / ".claude-plugin/plugin.json")
    codex = _load_object(root / ".codex-plugin/plugin.json")
    if set(claude) != CLAUDE_KEYS:
        raise ValueError("Claude manifest contains unsupported or missing keys")
    if set(codex) != CODEX_KEYS:
        raise ValueError("Codex manifest contains unsupported or missing keys")
    if claude.get("name") != "bridge" or codex.get("name") != "bridge":
        raise ValueError("manifest names must both be bridge")
    version = claude.get("version")
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Claude manifest version must be a MAJOR.MINOR.PATCH release")
    if codex.get("version") != version:
        raise ValueError("manifest versions must match between the two hosts")
    interface = codex.get("interface")
    if not isinstance(interface, dict) or set(interface) != INTERFACE_KEYS:
        raise ValueError("Codex interface keys are incomplete or unsupported")
    for key in ("composerIcon", "logo"):
        _relative_file(root, interface[key], field=f"interface.{key}")
    _relative_file(root, codex["mcpServers"], field="mcpServers")
    if claude["skills"] != "./claude-skills/":
        raise ValueError("Claude skills must resolve from ./claude-skills/")
    if codex["skills"] != "./codex-skills/":
        raise ValueError("Codex skills must resolve from ./codex-skills/")
    _relative_directory(root, claude["skills"], field="Claude skills")
    _relative_directory(root, codex["skills"], field="Codex skills")
    if "hooks" in claude or "hooks" in codex or (root / "hooks").exists():
        raise ValueError("hooks are not allowed in bridge")
    return version


def _validate_mcp(root: Path, relative_path: str, expected_var: str) -> None:
    """Validate one host's MCP declaration resolves its script through that host's own plugin-root variable.

    Codex expands only ``PLUGIN_ROOT``, never ``CLAUDE_PLUGIN_ROOT`` -- the Claude variable in this file would break the
    only host that reads it.
    """
    config = _load_object(root / relative_path)
    servers = config.get("mcpServers")
    if not isinstance(servers, dict) or set(servers) != {"bridge"}:
        raise ValueError(f"{relative_path} must declare only the bridge server")
    server = servers["bridge"]
    if not isinstance(server, dict) or server.get("command") not in {"python", "python3"}:
        raise ValueError(f"{relative_path} MCP server must use a portable Python command")
    args = server.get("args")
    if not isinstance(args, list) or not args or not all(isinstance(item, str) for item in args):
        raise ValueError(f"{relative_path} MCP server args must be a string list")
    matches = [PLUGIN_PATH_PATTERN.fullmatch(item) for item in args]
    script_match = next((match for match in matches if match is not None), None)
    if script_match is None:
        raise ValueError(f"{relative_path} MCP server must resolve its script through a plugin-root variable")
    if f"${{{expected_var}}}" not in args[matches.index(script_match)]:
        raise ValueError(f"{relative_path} MCP server must use ${{{expected_var}}}, not the other host's variable")
    _relative_file(root, f"bin/{script_match.group(1)}", field=f"{relative_path} args")
    if server.get("cwd") is not None:
        raise ValueError(f"{relative_path} MCP server must not depend on a source-tree cwd")


def _validate_files(root: Path) -> None:
    """Validate required contract files and reject private payload material."""
    for relative in REQUIRED_FILES:
        _relative_file(root, relative, field="required payload")
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if path.name in {".DS_Store", ".coverage"}:
            continue
        # Same exclusion order as build_package: excluded trees are skipped
        # before the symlink gate, so both tools agree on any given root.
        if any(part in {"__pycache__", ".pytest_cache", "tests"} for part in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError(f"symlink payload forbidden: {relative.as_posix()}")
        if any(part in {".plans", ".reports", ".temp"} for part in relative.parts):
            raise ValueError(f"private artifact payload present: {relative.as_posix()}")
        if path.is_file() and PRIVATE_ABSOLUTE_PATTERN.search(path.read_bytes()):
            raise ValueError(f"private absolute path in payload: {relative.as_posix()}")
        if path.is_file() and path.suffix == ".json":
            _load_object(path)


def _validate_skill_script_references(root: Path) -> None:
    """Ensure installed skill commands resolve inside this package through their own host's plugin-root variable.

    Each skill tree must use only its own host's variable -- Claude Code expands ``CLAUDE_PLUGIN_ROOT``, Codex expands
    ``PLUGIN_ROOT`` -- mirroring the check ``_validate_mcp`` runs for the Codex MCP declaration.
    """
    trees = (
        ("claude-skills/**/*.md", "CLAUDE_PLUGIN_ROOT"),
        ("codex-skills/**/*.md", "PLUGIN_ROOT"),
    )
    for pattern, expected_var in trees:
        for path in sorted(root.glob(pattern)):
            display = path.relative_to(root).as_posix()
            text = path.read_text(encoding="utf-8")
            for match in PLUGIN_PATH_PATTERN.finditer(text):
                if f"${{{expected_var}}}" not in match.group(0):
                    raise ValueError(
                        f"skill reference in {display} must use ${{{expected_var}}}, not the other host's variable"
                    )
                _relative_file(root, f"bin/{match.group(1)}", field=f"skill reference in {display}")


def validate_package(root: Path) -> str:
    """Validate one package root and return its matching version."""
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"package root is not a directory: {root}")
    version = _validate_manifests(root)
    codex = _load_object(root / ".codex-plugin" / "plugin.json")
    if (root / ".mcp.json").exists():
        raise ValueError(
            "Claude Code has no MCP surface for bridge -- a shipped .mcp.json would be auto-discovered unintentionally"
        )
    _validate_mcp(root, Path(codex["mcpServers"]).as_posix(), "PLUGIN_ROOT")
    _validate_files(root)
    _validate_skill_script_references(root)
    return version


def main(argv: list[str] | None = None) -> int:
    """Run the package gate and emit one bounded result."""
    args = _parse_args(argv)
    try:
        version = validate_package(args.package_root)
    except (OSError, UnicodeError, ValueError) as error:
        print(f"package-validation-error: {error}", file=sys.stderr)
        return 2
    print(f"Package validation passed: {args.package_root.resolve()} ({version})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
