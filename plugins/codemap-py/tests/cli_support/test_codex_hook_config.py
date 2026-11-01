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
        "command": f'CODEMAP_RUNTIME=codex "$PLUGIN_ROOT/bin/codemap-py" --run-hook {script}',
        "commandWindows": f"$env:CODEMAP_RUNTIME='codex'; & \"$env:PLUGIN_ROOT\\bin\\codemap-py.cmd\" --run-hook {script}; exit $LASTEXITCODE",
    }


class TestCodexRegistration:
    """Codex loads its own runtime-scoped hook config, separate from Claude's."""

    def test_manifest_points_to_runtime_scoped_hook_config(self) -> None:
        """Codex discovers its hooks through the plugin manifest, not Claude's config.

        The ``.codex-plugin`` manifest must name ``hooks/codex-hooks.json`` so Codex never loads the Claude file.
        """
        manifest = _load(".codex-plugin/plugin.json")

        assert manifest["hooks"] == "./hooks/codex-hooks.json"

    def test_registers_session_prompt_and_tool_lifecycle(self) -> None:
        """Codex hooks avoid Claude-only seeding, inject prompts, and classify required tools.

        Reads the shipped ``codex-hooks.json`` and checks, per event and matcher, exactly which runtime-scoped commands
        run: no SessionStart seeding, prompt preamble, Bash guard, and the exhausted/telemetry recorders.
        """
        config = _load("hooks/codex-hooks.json")

        assert "SessionStart" not in config["hooks"]
        assert _commands(config, "UserPromptSubmit", None) == [_command("inject-preamble.py")]
        assert _commands(config, "PreToolUse", "Bash") == [_command("guard-redundant-scan.py")]
        assert _commands(config, "PostToolUse", "Bash") == [
            _command("record-exhausted.py"),
            _command("log-tool-use.py"),
        ]
        assert _commands(config, "PostToolUse", "Grep|Read|Glob") == [_command("log-tool-use.py")]
        assert _commands(config, "PostToolUse", "Edit|Write|apply_patch") == [_command("record-exhausted.py")]


class TestClaudeRegistration:
    """The Claude hook surface stays as established while Codex gains its own."""

    def test_manifest_and_hook_runtime_unchanged(self) -> None:
        """The Codex registration must not relabel the established Claude hook surface.

        The Claude manifest keeps pointing at ``claude-hooks.json``, which must stay free of the Codex runtime label and
        still register ``record-exhausted.py``.
        """
        manifest = _load(".claude-plugin/plugin.json")
        claude_config = (_PLUGIN_ROOT / "hooks" / "claude-hooks.json").read_text()

        assert manifest["hooks"] == "./hooks/claude-hooks.json"
        assert "CODEMAP_RUNTIME=codex" not in claude_config
        assert "record-exhausted.py" in claude_config

    def test_hooks_share_validated_launcher(self) -> None:
        """Every hook validates the scanner's runtime before importing any handler.

        Each registered command must go through the ``codemap-py`` launcher and name a ``.py`` handler; a hook that
        invokes a script directly would skip the Python-runtime validation.
        """
        config = _load("hooks/claude-hooks.json")
        commands = [
            hook["command"] for entries in config["hooks"].values() for entry in entries for hook in entry["hooks"]
        ]
        launcher = 'exec "${CLAUDE_PLUGIN_ROOT}/bin/codemap-py" --run-hook '
        offenders = [c for c in commands if not (c.startswith(launcher) and c.endswith(".py"))]

        assert commands
        assert offenders == []
