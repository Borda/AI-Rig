#!/usr/bin/env python
"""resolve_agent_file.py — locate the source file for a calibration target.

Replaces the inline resolution block in /foundry:calibrate. A target is either
an agent (``curator``, ``oss:shepherd``) or a skill (``/audit``, ``/oss:review``);
a leading ``/`` selects the skill namespace and a ``plugin:`` prefix selects the
plugin, defaulting to ``foundry``.

Resolution order, identical in local and installed mode — the source tree is the
only place a calibration target may come from, and the installed cache is never
a fallback:

Manifest names identify plugins and manifest ``agents``/``skills`` fields select
their source directories. ``.claude-plugin/plugin.json`` takes precedence over
``.codex-plugin/plugin.json``. Resolved files must stay inside the plugin root.
Without a manifest, legacy lookup prefers ``plugins/cc_<plugin>/<rel>``, then
``plugins/<plugin>/<rel>``, then a single match or a matching directory prefix.

stdout is two lines the caller reads back:

    agent-file=plugins/cc_foundry/agents/curator.md
    proposal-path=.reports/calibrate/<timestamp>/foundry-curator/proposal.md

``agent-file=`` is empty when nothing resolved, and the reason is printed above it.
Callers always supply the canonical safe directory with ``--run-slug``, such as
``foundry-curator`` for ``foundry:curator``. A mode-level calibration domain such as
``oss-analyse-vitality`` resolves the ``/oss:analyse`` skill file but keeps its proposal
in its own directory. Without the flag, the historical default remains the target name
without its leading ``/``, including any colon.

Usage:
    resolve_agent_file.py --name <target> [--timestamp <ts>] [--run-slug <slug>] [--local]

Exit codes:
    0   resolved
    1   no source file found for the target
    2   invalid ``--name``, ``--timestamp`` or ``--run-slug`` (path separators, dots or other characters outside
        ``[A-Za-z0-9_-]``; the run slug may also hold ``:``)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from plugin_layout import declared_dir

#: Default source-tree directory that contains the plugin folders searched for an agent file.
PLUGINS_ROOT = Path("plugins")
#: Allowed shape of a plugin, agent or skill name: no separators or dots, so it cannot leave its directory.
_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")
#: Allowed shape of the run timestamp embedded in the proposal path; empty means no timestamp.
_TIMESTAMP_RE = re.compile(r"[A-Za-z0-9_-]*")
#: Allowed shape of a run directory name: target-like, ``:`` permitted, no separators or dots.
_RUN_SLUG_RE = re.compile(r"[A-Za-z0-9_:-]+")
#: Plugin assumed when the requested agent name carries no ``plugin:`` prefix.
DEFAULT_PLUGIN = "foundry"


def split_target(name: str) -> tuple[str, str, str]:
    """Return (plugin, bare name, relative source path) for a calibration target."""
    is_skill = name.startswith("/")
    bare = name.lstrip("/")
    plugin, _, rest = bare.partition(":")
    if not rest:
        plugin, rest = DEFAULT_PLUGIN, bare
    rel = f"skills/{rest}/SKILL.md" if is_skill else f"agents/{rest}.md"
    return plugin, rest, rel


def _source_candidate(plugin_root: Path, plugin: str, namespace: str, suffix: str) -> tuple[Path | None, bool]:
    """Classify a confined candidate and preserve a known manifest identity even when its target is missing."""
    manifest_path = plugin_root / ".claude-plugin/plugin.json"
    if not manifest_path.exists():
        manifest_path = plugin_root / ".codex-plugin/plugin.json"
    directory = namespace
    identified = False
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict) or manifest.get("name") != plugin:
                return None, False
            identified = True
            directory = declared_dir(manifest, namespace, namespace)
        except (OSError, ValueError):
            return None, identified
    candidate = plugin_root / directory / suffix
    if not candidate.is_file() or not candidate.resolve().is_relative_to(plugin_root.resolve()):
        return None, identified
    return candidate, identified


def _legacy_source(matches: list[Path], root: Path, plugin: str, rel: str) -> Path | None:
    """Choose among manifest-free source candidates using the historical folder precedence."""
    for directory in (f"cc_{plugin}", plugin):
        candidate = root / directory / rel
        if candidate in matches:
            return candidate
    if len(matches) == 1:
        return matches[0]
    prefixed = [p for p in matches if p.relative_to(root).parts[0].startswith(plugin)]
    if prefixed:
        return prefixed[0]
    # `cc_`-prefixed directories are the repo convention; match them too before giving up.
    prefixed = [p for p in matches if p.relative_to(root).parts[0].startswith(f"cc_{plugin}")]
    return prefixed[0] if prefixed else None


def resolve(plugin: str, rel: str, *, root: Path = PLUGINS_ROOT) -> Path | None:
    """Resolve manifest identities and directories, retaining lookup for manifest-free source trees."""
    if not root.is_dir():
        return None
    namespace, separator, suffix = rel.partition("/")
    if not separator or namespace not in {"agents", "skills"}:
        return None
    matches = []
    manifested = []
    known_identity = False
    for plugin_root in sorted(p for p in root.iterdir() if p.is_dir()):
        if not plugin_root.resolve().is_relative_to(root.resolve()):
            continue
        candidate, identified = _source_candidate(plugin_root, plugin, namespace, suffix)
        known_identity = known_identity or identified
        if candidate is not None:
            (manifested if identified else matches).append(candidate)
    # Manifest identity outranks legacy folder guesses even when its requested target is unavailable.
    if known_identity:
        return manifested[0] if manifested else None
    return _legacy_source(matches, root, plugin, rel)


def main() -> int:
    """Locate the source file for a calibration target."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--name", required=True, help="agent name, plugin:agent, or /skill")
    parser.add_argument("--timestamp", default="", help="calibrate run timestamp for the proposal path")
    parser.add_argument(
        "--run-slug", default="", help="canonical run directory, e.g. foundry-curator (omitted: historical target name)"
    )
    parser.add_argument("--local", action="store_true", help="label failures as --local for the caller")
    args = parser.parse_args()

    plugin, rest, rel = split_target(args.name)
    slug = args.run_slug or args.name.lstrip("/")
    names_ok = _NAME_RE.fullmatch(plugin) and _NAME_RE.fullmatch(rest)
    if not (names_ok and _TIMESTAMP_RE.fullmatch(args.timestamp) and _RUN_SLUG_RE.fullmatch(slug)):
        print(
            f"! invalid --name {args.name!r}, --timestamp {args.timestamp!r} or --run-slug {args.run_slug!r} "
            "(allowed: letters, digits, '_' and '-' around one optional 'plugin:' prefix)",
            file=sys.stderr,
        )
        return 2
    resolved = resolve(plugin, rel)
    if resolved is None:
        label = "⚠ --local:" if args.local else "⚠"
        print(
            f"{label} no source file resolved for {args.name} "
            f"(tried plugins/cc_{plugin}/{rel}, plugins/{plugin}/{rel}, plugins/*/{rel}) "
            "— skipping, never falling back to installed cache"
        )
        print("agent-file=")
        return 1

    print(f"agent-file={resolved.as_posix()}")
    print(f"proposal-path=.reports/calibrate/{args.timestamp}/{slug}/proposal.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
