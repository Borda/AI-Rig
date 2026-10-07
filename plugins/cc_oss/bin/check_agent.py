#!/usr/bin/env python
"""check_agent.py — probe whether a plugin agent is installed.

Checks the borda-ai-rig plugin cache and the project-local .claude/agents/
directory. Prints "true" or "false" to stdout; always exits 0 on success.

Usage:
    check_agent.py <plugin-name> <agent-name>

Exit codes:
    0 — always (success or "false" result)
    2 — invalid/missing plugin or agent name (non-alphanumeric characters)
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

#: Whitelist for plugin and agent names, which become path components under the plugin cache.
_SAFE_NAME: re.Pattern[str] = re.compile(r"^[a-zA-Z0-9_-]+$")


def check_agent(plugin: str, agent: str, home: Path | None = None) -> bool:
    """Return True when the agent is installed; False otherwise.

    Checks:
    1. ``~/.claude/plugins/cache/borda-ai-rig/<plugin>/*/agents/<agent>.md``
    2. ``./.claude/agents/<agent>.md`` (project-local fallback)

    Args:
        plugin: Plugin name (e.g. ``"oss"``).
        agent: Agent name without ``.md`` extension (e.g. ``"shepherd"``).
        home: Override home directory (defaults to ``Path.home()``).

    Returns:
        True if agent file found; False otherwise.

    Raises:
        ValueError: If plugin or agent name contains non-alphanumeric/dash/underscore characters.

    Examples:
        >>> check_agent("", "x")
        Traceback (most recent call last):
            ...
        ValueError: invalid plugin name: ''
        >>> check_agent("oss", "")
        Traceback (most recent call last):
            ...
        ValueError: invalid agent name: ''
    """
    if not _SAFE_NAME.fullmatch(plugin):
        raise ValueError(f"invalid plugin name: {plugin!r}")
    if not _SAFE_NAME.fullmatch(agent):
        raise ValueError(f"invalid agent name: {agent!r}")
    root = home if home is not None else Path.home()
    cache_plugin_dir = root / ".claude" / "plugins" / "cache" / "borda-ai-rig" / plugin
    if cache_plugin_dir.is_dir():
        for version_dir in cache_plugin_dir.iterdir():
            if (version_dir / "agents" / f"{agent}.md").exists():
                return True
    if (Path(".claude") / "agents" / f"{agent}.md").exists():
        return True
    return False


def main(argv: list[str] | None = None) -> int:
    """Entry point — mirrors ``check-agent.sh`` behaviour.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Exit code: 0 always; 2 on invalid argument.

    Examples:
        No doctest — requires filesystem state; covered by pytest.
    """
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]
    parser = argparse.ArgumentParser(
        prog="check_agent.py",
        description="Probe whether a plugin agent is installed.",
    )
    # nargs="*" keeps the legacy "Usage: ..." stderr message + exit 2 on too-few args
    # (vs argparse's lowercase "usage:" auto-message).
    parser.add_argument("names", nargs="*", help="<plugin-name> <agent-name> (2 names).")
    args = parser.parse_args(argv)

    if len(args.names) < 2:
        print("Usage: check_agent.py <plugin-name> <agent-name>", file=sys.stderr)
        return 2
    plugin, agent = args.names[0], args.names[1]
    try:
        result = check_agent(plugin, agent)
    except ValueError as exc:
        print(f"check_agent: {exc}", file=sys.stderr)
        return 2
    print("true" if result else "false")
    return 0


if __name__ == "__main__":
    sys.exit(main())
