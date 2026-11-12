#!/usr/bin/env python
"""Resolve and validate a plugin's current install root independently of cwd.

Purpose: Select the plugin root used to execute shipped helpers.
Scope: Authoritative install records, strict-SemVer cache fallback, then the
executing resolver's own source checkout when cwd remains inside that checkout.
Cache roots must stay under their marketplace/plugin directory and carry a
matching manifest; orphaned and incomplete roots miss. Generic third-party
plugins need not ship this resolver.

Registry lookup delegates to :mod:`get_plugin_install_path` semantics
(``installed_plugins.json`` → latest ``installedAt``); this module adds the
cache-scan fallback and validation gates so callers get a single
trusted path on stdout, or a non-zero exit and a diagnostic on stderr.
Source fallback is available only inside the checkout containing this executing
resolver; environment variables and copied cwd manifests confer no authority.

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name foundry
    python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name foundry --marketplace borda-ai-rig

Exit codes:
    0   validated PLUGIN_ROOT printed to stdout
    1   not found (registry, cache and trusted source all miss)
    2   argument error, or candidate failed a security gate

Outputs: One absolute forward-slash root on stdout, with diagnostics on stderr.
Failure: Escaping paths and mismatched identities fail closed without stdout.
Used by: Skill bootstrap prologues, document renderers and shared-doc resolvers.

<!-- file: resolve_plugin_root.py — consumers: foundry skills/setup/SKILL.md -->
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

#: Allowed shape of a plugin or marketplace name argument: letters, digits, underscore and hyphen only.
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]+")
_SEMVER_RE = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
_VersionKey = tuple[int, int, int, bool, tuple[tuple[bool, int | str], ...]]


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
    if not _TOKEN_RE.fullmatch(value):
        raise ValueError(f"{label} {value!r} contains disallowed characters (allowed: [A-Za-z0-9_-])")


def _registry_path(home: Path) -> Path:
    """Return the path to Claude Code's ``installed_plugins.json``."""
    return home / ".claude" / "plugins" / "installed_plugins.json"


def _installed_at(value: object) -> datetime:
    """Return an actual installation timestamp, sorting malformed values last."""
    if not isinstance(value, str):
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc)


def _registry_entries(registry: Path, marketplace: str, plugin_name: str) -> list[dict]:
    """Read timestamp-ordered records without accepting malformed registry shapes."""
    try:
        data = json.loads(registry.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    plugins = data.get("plugins") if isinstance(data, dict) else None
    entries = plugins.get(f"{plugin_name}@{marketplace}") if isinstance(plugins, dict) else None
    if not isinstance(entries, list):
        return []
    candidates = [
        e for e in entries if isinstance(e, dict) and isinstance(e.get("installPath"), str) and e["installPath"]
    ]
    return sorted(candidates, key=lambda entry: _installed_at(entry.get("installedAt")), reverse=True)


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
    candidates = _registry_entries(registry, marketplace, plugin_name)
    return candidates[0]["installPath"] if candidates else None


def _semver_key(path: Path) -> _VersionKey | None:
    """Return strict SemVer precedence, ignoring build metadata."""
    match = _SEMVER_RE.fullmatch(path.name)
    if match is None:
        return None
    major, minor, patch, prerelease = match.groups()
    identifiers = prerelease.split(".") if prerelease else []
    if any(part.isdigit() and len(part) > 1 and part.startswith("0") for part in identifiers):
        return None
    parts = tuple((not part.isdigit(), int(part) if part.isdigit() else part) for part in identifiers)
    return int(major), int(minor), int(patch), prerelease is None, parts


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
        ...     _ = (base / "0.1.0" / "bin").mkdir()
        ...     _ = (base / "0.2.0" / "bin").mkdir()
        ...     _ = (base / "0.1.0" / "bin" / "resolve_plugin_root.py").write_text("# resolver")
        ...     _ = (base / "0.2.0" / "bin" / "resolve_plugin_root.py").write_text("# resolver")
        ...     scan_cache(Path(d), "borda-ai-rig", "foundry").endswith("0.2.0")
        True
    """
    plugin_dir = cache_root / marketplace / plugin_name
    if not plugin_dir.is_dir():
        return None
    versions = [(key, d) for d in plugin_dir.iterdir() if d.is_dir() and (key := _semver_key(d)) is not None]
    for _, candidate in sorted(versions, reverse=True):
        if (candidate / ".orphaned_at").exists():
            continue
        validate_root(str(candidate), cache_root, plugin_name, marketplace=marketplace)
        if _usable_root(candidate):
            return candidate.resolve().as_posix()
    return None


def _manifest_name(root: Path) -> str | None:
    """Read a plugin identity without trusting malformed manifests."""
    try:
        manifest = json.loads((root / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return manifest.get("name") if isinstance(manifest, dict) else None


def _usable_root(root: Path) -> bool:
    """Require a live manifest-bearing root and contain any shipped bootstrap resolver."""
    bootstrap = root / "bin/resolve_plugin_root.py"
    if (bootstrap.exists() or bootstrap.is_symlink()) and not bootstrap.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"resolver escapes PLUGIN_ROOT {root}")
    return root.is_dir() and not (root / ".orphaned_at").exists() and (root / ".claude-plugin/plugin.json").is_file()


def validate_root(root: str, cache_root: Path, plugin_name: str, *, marketplace: str | None = None) -> None:
    """Enforce the two setup security gates on a resolved root.

    Args:
        root: Candidate PLUGIN_ROOT.
        cache_root: Expected ``~/.claude/plugins/cache`` ancestor.
        plugin_name: Expected ``plugin.json`` ``name`` value.

    Raises:
        ValueError: When ``root`` is outside ``cache_root`` or its
            ``plugin.json`` name does not match ``plugin_name``.
    """
    candidate = Path(root)
    if not candidate.is_absolute():
        raise ValueError(f"PLUGIN_ROOT {root!r} must be absolute")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(cache_root.resolve()):
        raise ValueError(f"PLUGIN_ROOT {resolved} is outside expected cache dir {cache_root}")
    relative = resolved.relative_to(cache_root.resolve())
    if len(relative.parts) != 3 or relative.parts[1] != plugin_name:
        raise ValueError(f"PLUGIN_ROOT {resolved} has the wrong cache plugin identity")
    if marketplace is not None and relative.parts[0] != marketplace:
        raise ValueError(f"PLUGIN_ROOT {resolved} has the wrong marketplace identity")
    manifest = resolved / ".claude-plugin/plugin.json"
    if not manifest.exists():
        return
    if not manifest.resolve().is_relative_to(resolved):
        raise ValueError(f"plugin.json escapes PLUGIN_ROOT {resolved}")
    name = _manifest_name(resolved)
    if name != plugin_name:
        raise ValueError(f"plugin.json name {name!r} != {plugin_name!r}")


def source_root(plugin_name: str, *, resolver_file: Path | None = None, cwd: Path | None = None) -> Path | None:
    """Accept only a source root in the executing resolver's own checkout."""
    executing = (resolver_file or Path(__file__)).resolve()
    if not executing.is_file():
        return None
    own_root = executing.parent.parent
    collection = own_root.parent
    checkout = collection.parent
    if executing.name != "resolve_plugin_root.py" or executing.parent.name != "bin":
        return None
    if collection.name != "plugins" or not (cwd or Path.cwd()).resolve().is_relative_to(checkout):
        return None
    own_name = _manifest_name(own_root)
    if not isinstance(own_name, str) or own_root.name not in {own_name, f"cc_{own_name}"}:
        return None
    if not (own_root / ".claude-plugin/plugin.json").resolve().is_relative_to(own_root):
        raise ValueError("executing resolver's plugin manifest escapes its root")
    for dirname in (f"cc_{plugin_name}", plugin_name):
        candidate = collection / dirname
        if candidate.resolve().parent != collection or _manifest_name(candidate) != plugin_name:
            continue
        if not (candidate / ".claude-plugin/plugin.json").resolve().is_relative_to(candidate.resolve()):
            raise ValueError("source plugin manifest escapes its root")
        if _usable_root(candidate) and executing.is_relative_to(collection):
            return candidate.resolve()
    return None


def _source_marketplace(root: Path) -> str | None:
    """Read the marketplace identity belonging to the anchored source checkout."""
    checkout = root.parent.parent
    manifest = checkout / ".claude-plugin/marketplace.json"
    if not manifest.resolve().is_relative_to(checkout):
        raise ValueError("source marketplace manifest escapes its checkout")
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    name = data.get("name") if isinstance(data, dict) else None
    return name if isinstance(name, str) else None


def resolve(home: Path, marketplace: str, plugin_name: str, *, resolver_file: Path | None = None) -> str | None:
    """Resolve a validated root through registry, cache, then the own checkout.

    Args:
        home: User home directory.
        marketplace: Marketplace short-name.
        plugin_name: Plugin short-name.

    Returns:
        A validated install-root path, or ``None`` when all resolution paths
        miss. Validation failures raise ``ValueError`` rather than returning.
    """
    cache_root = home / ".claude" / "plugins" / "cache"
    _validate_token(marketplace, "marketplace")
    _validate_token(plugin_name, "plugin")
    for entry in _registry_entries(_registry_path(home), marketplace, plugin_name):
        candidate = Path(entry["installPath"])
        validate_root(str(candidate), cache_root, plugin_name, marketplace=marketplace)
        if _installed_at(entry.get("installedAt")) == datetime.min.replace(tzinfo=timezone.utc):
            continue
        if (candidate / ".orphaned_at").exists():
            print(f"resolve_plugin_root: skipping orphaned registry install {candidate}", file=sys.stderr)
            continue
        if _usable_root(candidate) and _manifest_name(candidate) == plugin_name:
            return candidate.resolve().as_posix()
    root = scan_cache(cache_root, marketplace, plugin_name)
    if root is not None:
        return root
    source = source_root(plugin_name, resolver_file=resolver_file)
    return source.as_posix() if source is not None and _source_marketplace(source) == marketplace else None


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

    sys.stdout.buffer.write((root + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
