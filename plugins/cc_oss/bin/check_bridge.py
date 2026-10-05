#!/usr/bin/env python3
"""Report whether the Claude-Codex bridge plugin is installed and enabled.

Purpose:
    Keep consumer skills from dispatching to a bridge that cannot answer.
Scope:
    Reads only Claude plugin registry, cache, and enabled-plugin settings.
Usage:
    ``check_bridge.py`` prints ``true`` or ``false``; ``--status`` prints one
    of ``available``, ``disabled``, or ``absent``.
Outputs:
    A single status line on stdout and exit code 0 for valid invocations.
Failure:
    An unavailable or malformed registry permits cache fallback; unreadable settings
    supply no explicit disable flag. Argparse rejects unsupported flags with exit 2.
Used by:
    Foundry's challenger and consumer skill availability checks. Every consumer
    plugin ships a byte-identical copy so none has to assume Foundry is installed
    (see plugins/cc_foundry/bin/propagate_shared.py).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

TARGET_PLUGIN = "bridge"
#: Marketplace the bridge is installed from. Overridable so a fork published under
#: another marketplace name can use this detector unmodified — the selector and the
#: cache layout both key off it, and neither is discoverable from the plugin alone.
MARKETPLACE = os.environ.get("AI_RIG_MARKETPLACE") or "borda-ai-rig"
TARGET_SELECTOR = f"{TARGET_PLUGIN}@{MARKETPLACE}"
_STATUS_AVAILABLE = "available"
_STATUS_DISABLED = "disabled"
_STATUS_ABSENT = "absent"


def _manifest_lookup(manifest_path: Path) -> bool | None:
    """Return the bridge's installed state per Claude's registry, or None if it cannot answer.

    A selector key alone does not mean installed — the installer itself requires an entry carrying ``installPath``
    before it will treat a plugin as present, so this applies the same rule rather than a weaker one.

    ``None`` is reserved for a registry that could not be consulted at all (absent file, malformed JSON, unexpected
    shape); it is the only case where the cache gets a vote.
    """
    if not manifest_path.is_file():
        return None
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("plugins"), dict):
        return None
    entries = data["plugins"].get(TARGET_SELECTOR)
    if not isinstance(entries, list):
        return False
    return any(isinstance(entry, dict) and entry.get("installPath") for entry in entries)


def _cache_has_target(cache_dir: Path) -> bool:
    """Return whether the marketplace cache holds any bridge version directory.

    Corroborating evidence only, never authoritative: cache directories outlive uninstall, so a hit here proves the
    bridge was installed at some point, not that it is now.
    """
    target_root = cache_dir / MARKETPLACE / TARGET_PLUGIN
    if not target_root.is_dir():
        return False
    return any(path.is_dir() for path in target_root.iterdir())


def _settings_explicitly_disabled(home: Path, project_root: Path | None = None) -> bool:
    """Return whether local-or-global settings explicitly disable the bridge."""

    def _load(path: Path) -> dict[str, object]:
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    root = project_root or Path(".")
    global_plugins = _load(home / ".claude" / "settings.json").get("enabledPlugins", {})
    local_plugins = _load(root / ".claude" / "settings.json").get("enabledPlugins", {})
    global_map = global_plugins if isinstance(global_plugins, dict) else {}
    local_map = local_plugins if isinstance(local_plugins, dict) else {}
    return {**global_map, **local_map}.get(TARGET_SELECTOR, True) is False


def bridge_status(home: Path, project_root: Path | None = None) -> str:
    """Resolve bridge availability from registry evidence, fallback cache, and settings.

    Consult the cache only when the registry cannot answer. A readable registry without the selector takes precedence
    over stale cache directories. Apply project-over-global explicit disable settings after establishing presence.
    """
    plugins_dir = home / ".claude" / "plugins"
    registered = _manifest_lookup(plugins_dir / "installed_plugins.json")
    installed = _cache_has_target(plugins_dir / "cache") if registered is None else registered
    if not installed:
        return _STATUS_ABSENT
    if _settings_explicitly_disabled(home, project_root):
        return _STATUS_DISABLED
    return _STATUS_AVAILABLE


def bridge_available(home: Path, project_root: Path | None = None) -> bool:
    """Return whether the exact installed bridge selector is enabled."""
    return bridge_status(home, project_root) == _STATUS_AVAILABLE


def main(argv: list[str] | None = None) -> int:
    """Print the bridge availability boolean or a diagnostic status."""
    parser = argparse.ArgumentParser(
        prog="check_bridge.py",
        description=f"Detect whether {TARGET_SELECTOR} is installed and enabled.",
    )
    parser.add_argument("--status", action="store_true", help="print available, disabled, or absent")
    args = parser.parse_args(argv)
    status = bridge_status(Path.home(), Path("."))
    print(status if args.status else str(status == _STATUS_AVAILABLE).lower())
    return 0


if __name__ == "__main__":
    sys.exit(main())
