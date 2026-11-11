#!/usr/bin/env python
"""plugin_layout.py — print each plugin's manifest-declared skills/agents directories.

A plugin can override the default ``skills/`` and ``agents/`` directory names in its own manifest (for example
``"skills": "./claude-skills/"``), so a fixed ``*/skills/*/SKILL.md`` glob silently never matches such a plugin. The
audit skill calls this script before globbing to learn each plugin's real directory names.

Usage:
    plugin_layout.py [<plugins-dir>]

Output (stdout), one line per manifest, sorted by manifest path:
    PLUGIN_LAYOUT: <plugin-dir-name> | skills=<dir> | agents=<dir>

A manifest that is not valid JSON, or declares neither field, falls back to ``skills`` and ``agents``.

Exit codes:
    0 — success (including a plugins directory with no manifests)
    1 — a present skills or agents field declares an invalid relative directory
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

#: Manifest locations scanned, relative to a plugin directory.
_MANIFESTS: tuple[str, ...] = (".claude-plugin/plugin.json", ".codex-plugin/plugin.json")


def declared_dir(manifest: dict, key: str, default: str) -> str:
    """Normalize a declared relative directory, defaulting only when ``key`` is absent."""
    if key not in manifest:
        return default
    value = manifest[key]
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._/-]+", value):
        raise ValueError(f"manifest {key} must name a relative directory: {value!r}")
    if value.startswith("/") or ".." in value.split("/"):
        raise ValueError(f"manifest {key} must name a relative directory without '..': {value!r}")
    normalized = Path(value).as_posix()
    if normalized == ".":
        raise ValueError(f"manifest {key} must name a directory below the plugin root: {value!r}")
    return normalized


def layout_lines(plugins_dir: Path) -> list[str]:
    """Build one ``PLUGIN_LAYOUT:`` line per plugin manifest found under ``plugins_dir``."""
    lines = []
    for plugin in sorted(p for p in plugins_dir.iterdir() if p.is_dir()):
        for rel in _MANIFESTS:
            path = plugin / rel
            if not path.is_file():
                continue
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                manifest = {}
            if not isinstance(manifest, dict):
                manifest = {}
            skills = declared_dir(manifest, "skills", "skills")
            agents = declared_dir(manifest, "agents", "agents")
            lines.append(f"PLUGIN_LAYOUT: {plugin.name} | skills={skills} | agents={agents}")
    return lines


def main(argv: list[str] | None = None) -> int:
    """Print the layout lines for the given plugins directory (default ``plugins``)."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("plugins_dir", nargs="?", default="plugins", type=Path)
    args = parser.parse_args(argv)
    if args.plugins_dir.is_dir():
        try:
            lines = layout_lines(args.plugins_dir)
        except ValueError as exc:
            print(f"plugin_layout: {exc}", file=sys.stderr)
            return 1
        for line in lines:
            sys.stdout.write(line + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
