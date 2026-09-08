"""Regression contracts for Codex-specific codemap hook registration."""

from __future__ import annotations

import json
from pathlib import Path


_PLUGIN_ROOT = Path(__file__).parents[2]


def _load(relative_path: str) -> dict:
    """Load one plugin JSON document from the installed-layout root."""
    return json.loads((_PLUGIN_ROOT / relative_path).read_text())


def _commands(config: dict, event: str, matcher: str | None) -> list[dict]:
    """Return command hooks registered for exactly one Codex event/matcher pair.

    >>> _commands({"hooks": {"start": [{"matcher": None, "hooks": [{"command": "x"}]}]}}, "start", None)
    [{'command': 'x'}]
    """
    return [hook for entry in config["hooks"][event] if entry.get("matcher") == matcher for hook in entry["hooks"]]


def _command(script: str) -> dict:
    """Return the cross-platform command schema for one runtime-scoped hook script.

    >>> _command("seed-session.py")["type"]
    'command'
    """
    return {
        "type": "command",
        "command": f'env CODEMAP_RUNTIME=codex python3 "$PLUGIN_ROOT/hooks/{script}"',
        "commandWindows": f"$env:CODEMAP_RUNTIME='codex'; python \"$env:PLUGIN_ROOT\\hooks\\{script}\"",
    }


def test_codex_manifest_points_to_runtime_scoped_hook_config() -> None:
    """Codex discovers its hooks through the plugin manifest, not Claude's config."""
    manifest = _load(".codex-plugin/plugin.json")

    assert manifest["hooks"] == "./hooks/codex-hooks.json"


def test_codex_hook_config_registers_runtime_scoped_session_prompt_and_tool_lifecycle() -> None:
    """Codex hooks avoid Claude-only seeding, inject prompts, and classify required tools."""
    config = _load("hooks/codex-hooks.json")

    assert "SessionStart" not in config["hooks"]
    assert _commands(config, "UserPromptSubmit", None) == [_command("inject-preamble.py")]
    assert _commands(config, "PreToolUse", "Bash") == [_command("guard-redundant-scan.py")]
    assert _commands(config, "PostToolUse", "Bash") == [_command("record-exhausted.py"), _command("log-tool-use.py")]
    assert _commands(config, "PostToolUse", "Grep|Read|Glob") == [_command("log-tool-use.py")]
    assert _commands(config, "PostToolUse", "Edit|Write|apply_patch") == [_command("record-exhausted.py")]


def test_claude_manifest_and_hook_runtime_remain_unchanged() -> None:
    """The Codex registration must not relabel the established Claude hook surface."""
    manifest = _load(".claude-plugin/plugin.json")
    claude_config = (_PLUGIN_ROOT / "hooks" / "claude-hooks.json").read_text()

    assert manifest["hooks"] == "./hooks/claude-hooks.json"
    assert "CODEMAP_RUNTIME=codex" not in claude_config
    assert "record-exhausted.py" in claude_config
