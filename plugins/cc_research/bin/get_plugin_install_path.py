#!/usr/bin/env python
"""get_plugin_install_path.py — resolve a plugin's current installed path from Claude Code's plugin registry.

Reads ``~/.claude/plugins/installed_plugins.json``, finds the entry for a given
``marketplace + plugin`` combination, picks the most-recently-installed entry
(by ``installedAt`` timestamp), and prints its ``installPath`` to stdout.

This is the authoritative source for "which version of a plugin is currently
active" — the cache filesystem may contain multiple orphaned versions; only
``installed_plugins.json`` records which one Claude Code dispatches to.

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/get_plugin_install_path.py" <marketplace> <plugin-name>
    python "${CLAUDE_PLUGIN_ROOT}/bin/get_plugin_install_path.py" borda-ai-rig foundry

Exit codes:
    0   installPath found and printed to stdout
    1   not found (plugin not installed or registry missing)
    2   argument error

<!-- file: get_plugin_install_path.py — consumers: resolve_shared_path.py (tier 1 registry lookup, every bin/) -->
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _validate_token(value: str, label: str) -> None:
    """Reject empty or shell-metacharacter tokens.

    Args:
        value: Token to validate.
        label: Field name used in the error message.

    Raises:
        ValueError: When ``value`` is empty or contains a character outside
            ``[A-Za-z0-9_-]``.

    Examples:
        >>> _validate_token("borda-ai-rig", "marketplace")
        >>> _validate_token("foundry", "plugin")
        >>> _validate_token("", "plugin")
        Traceback (most recent call last):
            ...
        ValueError: plugin must not be empty
        >>> _validate_token("../etc", "plugin")
        Traceback (most recent call last):
            ...
        ValueError: plugin '../etc' contains disallowed characters (allowed: [A-Za-z0-9_-])
    """
    if not value:
        raise ValueError(f"{label} must not be empty")
    if not _TOKEN_RE.match(value):
        raise ValueError(f"{label} {value!r} contains disallowed characters (allowed: [A-Za-z0-9_-])")


def pick_latest_install_path(entries: list[dict]) -> str | None:
    """Return the ``installPath`` of the entry with the latest ``installedAt``.

    Args:
        entries: List of plugin-installation records from ``installed_plugins.json``.

    Returns:
        The ``installPath`` from the entry with the maximum ``installedAt``
        timestamp, or ``None`` when no entry carries an ``installPath`` field.

    Examples:
        >>> pick_latest_install_path([])
        >>> pick_latest_install_path([{"installedAt": "2026-01-01T00:00:00Z", "installPath": "/a"}])
        '/a'
        >>> pick_latest_install_path([
        ...     {"installedAt": "2026-01-01T00:00:00Z", "installPath": "/old"},
        ...     {"installedAt": "2026-05-01T00:00:00Z", "installPath": "/new"},
        ... ])
        '/new'
        >>> pick_latest_install_path([{"installedAt": "2026-01-01T00:00:00Z"}])
    """
    if not entries:
        return None
    candidates = [e for e in entries if e.get("installPath")]
    if not candidates:
        return None
    candidates.sort(key=lambda e: e.get("installedAt", ""), reverse=True)
    return str(candidates[0]["installPath"])


def resolve_install_path(registry_path: Path, marketplace: str, plugin_name: str) -> str | None:
    """Resolve the current installPath for ``<plugin_name>@<marketplace>``.

    Args:
        registry_path: Path to Claude Code's ``installed_plugins.json``.
        marketplace: Marketplace short-name (e.g. ``borda-ai-rig``).
        plugin_name: Plugin short-name (e.g. ``foundry``).

    Returns:
        The latest ``installPath`` for the lookup key, or ``None`` when the
        registry file is missing or the key has no entries with an
        ``installPath`` field.

    Examples:
        >>> import json, tempfile
        >>> with tempfile.TemporaryDirectory() as d:
        ...     reg = Path(d) / "installed_plugins.json"
        ...     _ = reg.write_text(json.dumps({"plugins": {"foo@bar": [
        ...         {"installedAt": "2026-01-01T00:00:00Z", "installPath": "/p1"},
        ...         {"installedAt": "2026-05-01T00:00:00Z", "installPath": "/p2"},
        ...     ]}}))
        ...     resolve_install_path(reg, "bar", "foo")
        '/p2'
        >>> resolve_install_path(Path("/__nonexistent__"), "bar", "foo")
    """
    if not registry_path.is_file():
        return None
    try:
        data = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    plugins = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(plugins, dict):
        return None
    key = f"{plugin_name}@{marketplace}"
    entries = plugins.get(key)
    if not isinstance(entries, list):
        return None
    return pick_latest_install_path(entries)


def main(argv: list[str] | None = None) -> int:
    """Resolve and print an installed plugin path."""
    parser = argparse.ArgumentParser(
        prog="get_plugin_install_path",
        description="Resolve a plugin's current installPath from Claude Code's installed_plugins.json.",
    )
    parser.add_argument("marketplace", help="Marketplace short-name (e.g. borda-ai-rig).")
    parser.add_argument("plugin_name", help="Plugin short-name (e.g. foundry).")
    parser.add_argument(
        "--registry",
        default=None,
        metavar="PATH",
        help="Override registry path (default: ~/.claude/plugins/installed_plugins.json).",
    )
    args = parser.parse_args(argv)

    try:
        _validate_token(args.marketplace, "marketplace")
        _validate_token(args.plugin_name, "plugin")
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.registry:
        # Canonicalise ``--registry``: must resolve under the user's ``.claude``
        # tree, the project working directory, or the OS temp dir.  The temp-dir
        # exception covers pytest's ``tmp_path`` fixture and tooling that
        # vendors a throw-away registry copy.  Anything else is rejected (CWE-22).
        registry_path = Path(args.registry).expanduser().resolve()
        allowed_roots = [
            (Path(os.path.expanduser("~")) / ".claude").resolve(),
            Path.cwd().resolve(),
            Path(tempfile.gettempdir()).resolve(),
        ]
        if not any(registry_path.is_relative_to(root) for root in allowed_roots):
            print(
                f"error: --registry resolves outside ~/.claude, project root, and temp dir: {registry_path}",
                file=sys.stderr,
            )
            return 2
        # File existence is checked downstream by ``resolve_install_path`` which
        # gracefully returns ``None`` (→ exit 1) for missing/malformed files —
        # preserves the historical exit-code contract for those cases.
    else:
        registry_path = Path(os.path.expanduser("~")) / ".claude" / "plugins" / "installed_plugins.json"

    install_path = resolve_install_path(registry_path, args.marketplace, args.plugin_name)
    if install_path is None:
        print(
            f"get_plugin_install_path: {args.plugin_name}@{args.marketplace} not found in {registry_path}",
            file=sys.stderr,
        )
        return 1

    print(install_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
