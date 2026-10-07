#!/usr/bin/env python
"""resolve_plugin_root.py — resolve and validate a plugin's current install root.

Composes the full PLUGIN_ROOT resolution that ``/foundry:setup`` performs in
several places: registry lookup (authoritative), filesystem cache-scan fallback
(skips ``.orphaned_at`` dirs, newest by semver), and security validation
(root must live under ``~/.claude/plugins/cache/`` and its ``plugin.json`` name
must equal the requested plugin name).

Registry lookup delegates to :mod:`get_plugin_install_path` semantics
(``installed_plugins.json`` → latest ``installedAt``); this module adds the
cache-scan fallback and the two validation gates so callers get a single
trusted path on stdout, or a non-zero exit and a diagnostic on stderr.

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name foundry
    python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name foundry --marketplace borda-ai-rig

Exit codes:
    0   validated PLUGIN_ROOT printed to stdout
    1   not found (registry miss + no cache hit)
    2   argument error, or candidate failed a security gate

<!-- file: resolve_plugin_root.py — consumers: foundry skills/setup/SKILL.md -->
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

#: Allowed shape of a plugin or marketplace name argument: letters, digits, underscore and hyphen only.
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


def _registry_path(home: Path) -> Path:
    """Return the path to Claude Code's ``installed_plugins.json``."""
    return home / ".claude" / "plugins" / "installed_plugins.json"


def lookup_registry(registry: Path, marketplace: str, plugin_name: str) -> str | None:
    """Return the latest ``installPath`` for ``<plugin_name>@<marketplace>``.

    Args:
        registry: Path to ``installed_plugins.json``.
        marketplace: Marketplace short-name (e.g. ``borda-ai-rig``).
        plugin_name: Plugin short-name (e.g. ``foundry``).

    Returns:
        The ``installPath`` of the entry with the maximum ``installedAt``
        timestamp, or ``None`` when the registry is missing, malformed, or has
        no matching entry with an ``installPath``.

    Examples:
        >>> import json, tempfile
        >>> with tempfile.TemporaryDirectory() as d:
        ...     reg = Path(d) / "installed_plugins.json"
        ...     _ = reg.write_text(json.dumps({"plugins": {"foundry@borda-ai-rig": [
        ...         {"installedAt": "2026-01-01T00:00:00Z", "installPath": "/old"},
        ...         {"installedAt": "2026-05-01T00:00:00Z", "installPath": "/new"},
        ...     ]}}))
        ...     lookup_registry(reg, "borda-ai-rig", "foundry")
        '/new'
        >>> lookup_registry(Path("/__nonexistent__"), "borda-ai-rig", "foundry")
    """
    if not registry.is_file():
        return None
    try:
        data = json.loads(registry.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    plugins = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(plugins, dict):
        return None
    entries = plugins.get(f"{plugin_name}@{marketplace}")
    if not isinstance(entries, list):
        return None
    candidates = [e for e in entries if isinstance(e, dict) and e.get("installPath")]
    if not candidates:
        return None
    candidates.sort(key=lambda e: e.get("installedAt", ""), reverse=True)
    return str(candidates[0]["installPath"])


def _semver_key(path: Path) -> list:
    """Return a sortable key from the version segment of a cache path."""
    parts: list = []
    for token in re.split(r"[.\-+]", path.name):
        parts.append((0, int(token)) if token.isdigit() else (1, token))
    return parts


def scan_cache(cache_root: Path, marketplace: str, plugin_name: str) -> str | None:
    """Return the newest non-orphaned cache install for the plugin.

    Walks ``<cache_root>/<marketplace>/<plugin_name>/*`` version dirs, skips any
    carrying an ``.orphaned_at`` marker, and returns the newest by semver.

    Args:
        cache_root: ``~/.claude/plugins/cache`` (or test stand-in).
        marketplace: Marketplace short-name.
        plugin_name: Plugin short-name.

    Returns:
        The newest valid version dir as a string, or ``None`` when none exist.

    Examples:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as d:
        ...     base = Path(d) / "borda-ai-rig" / "foundry"
        ...     _ = (base / "0.1.0" / ".claude-plugin").mkdir(parents=True)
        ...     _ = (base / "0.2.0" / ".claude-plugin").mkdir(parents=True)
        ...     (base / "0.2.0" / ".claude-plugin" / "plugin.json").write_text('{"name": "foundry"}') and None
        ...     (base / "0.1.0" / ".claude-plugin" / "plugin.json").write_text('{"name": "foundry"}') and None
        ...     scan_cache(Path(d), "borda-ai-rig", "foundry").endswith("0.2.0")
        True
    """
    plugin_dir = cache_root / marketplace / plugin_name
    if not plugin_dir.is_dir():
        return None
    versions = [
        d
        for d in plugin_dir.iterdir()
        if d.is_dir() and not (d / ".orphaned_at").exists() and (d / ".claude-plugin" / "plugin.json").is_file()
    ]
    if not versions:
        return None
    versions.sort(key=_semver_key, reverse=True)
    return str(versions[0])


def validate_root(root: str, cache_root: Path, plugin_name: str) -> None:
    """Enforce the two setup security gates on a resolved root.

    Args:
        root: Candidate PLUGIN_ROOT.
        cache_root: Expected ``~/.claude/plugins/cache`` ancestor.
        plugin_name: Expected ``plugin.json`` ``name`` value.

    Raises:
        ValueError: When ``root`` is outside ``cache_root`` or its
            ``plugin.json`` name does not match ``plugin_name``.
    """
    resolved = Path(root).resolve()
    if not resolved.is_relative_to(cache_root.resolve()):
        raise ValueError(f"PLUGIN_ROOT {resolved} is outside expected cache dir {cache_root}")
    manifest = resolved / ".claude-plugin" / "plugin.json"
    try:
        name = json.loads(manifest.read_text(encoding="utf-8")).get("name")
    except (OSError, json.JSONDecodeError):
        name = None
    if name != plugin_name:
        raise ValueError(f"plugin.json name {name!r} != {plugin_name!r}")


def resolve(home: Path, marketplace: str, plugin_name: str) -> str | None:
    """Resolve a validated PLUGIN_ROOT via registry then cache-scan fallback.

    Args:
        home: User home directory.
        marketplace: Marketplace short-name.
        plugin_name: Plugin short-name.

    Returns:
        A validated install-root path, or ``None`` when both resolution paths
        miss. Validation failures raise ``ValueError`` rather than returning.
    """
    cache_root = home / ".claude" / "plugins" / "cache"
    root = lookup_registry(_registry_path(home), marketplace, plugin_name)
    if root is None:
        root = scan_cache(cache_root, marketplace, plugin_name)
    if root is None:
        return None
    validate_root(root, cache_root, plugin_name)
    return root


def main(argv: list[str] | None = None) -> int:
    """Resolve and print the selected plugin root."""
    parser = argparse.ArgumentParser(
        prog="resolve_plugin_root",
        description="Resolve and validate a plugin's current install root.",
    )
    parser.add_argument("--plugin-name", required=True, help="Plugin short-name (e.g. foundry).")
    parser.add_argument("--marketplace", default="borda-ai-rig", help="Marketplace short-name.")
    args = parser.parse_args(argv)

    try:
        _validate_token(args.plugin_name, "plugin")
        _validate_token(args.marketplace, "marketplace")
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    home = Path(os.path.expanduser("~"))
    try:
        root = resolve(home, args.marketplace, args.plugin_name)
    except ValueError as exc:
        print(f"! SECURITY: {exc} — aborting", file=sys.stderr)
        return 2

    if root is None:
        print(
            f"resolve_plugin_root: {args.plugin_name}@{args.marketplace} not found (registry miss + no cache hit)",
            file=sys.stderr,
        )
        return 1

    print(root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
