#!/usr/bin/env python
"""resolve_agent_file.py — locate the source file for a calibration target.

Replaces the inline resolution block in /foundry:calibrate. A target is either
an agent (``curator``, ``oss:shepherd``) or a skill (``/audit``, ``/oss:review``);
a leading ``/`` selects the skill namespace and a ``plugin:`` prefix selects the
plugin, defaulting to ``foundry``.

Resolution order, identical in local and installed mode — the source tree is the
only place a calibration target may come from, and the installed cache is never
a fallback:

1. ``plugins/cc_<plugin>/<rel>``
2. ``plugins/<plugin>/<rel>``
3. a single ``plugins/*/<rel>`` match; with several, the one whose plugin
   directory name starts with the prefix

stdout is two lines the caller reads back:

    agent-file=plugins/cc_foundry/agents/curator.md
    proposal-path=.reports/calibrate/<timestamp>/curator/proposal.md

``agent-file=`` is empty when nothing resolved, and the reason is printed above it.

Usage:
    resolve_agent_file.py --name <target> [--timestamp <ts>] [--local]

Exit codes:
    0   resolved
    1   no source file found for the target
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PLUGINS_ROOT = Path("plugins")
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


def resolve(plugin: str, rel: str, *, root: Path = PLUGINS_ROOT) -> Path | None:
    """Return the source file for a plugin-relative path, or None."""
    for candidate in (root / f"cc_{plugin}" / rel, root / plugin / rel):
        if candidate.is_file():
            return candidate
    matches = sorted(p for p in root.glob(f"*/{rel}") if p.is_file())
    if len(matches) == 1:
        return matches[0]
    prefixed = [p for p in matches if p.relative_to(root).parts[0].startswith(plugin)]
    if prefixed:
        return prefixed[0]
    # `cc_`-prefixed directories are the repo convention; match them too before giving up.
    prefixed = [p for p in matches if p.relative_to(root).parts[0].startswith(f"cc_{plugin}")]
    return prefixed[0] if prefixed else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--name", required=True, help="agent name, plugin:agent, or /skill")
    parser.add_argument("--timestamp", default="", help="calibrate run timestamp for the proposal path")
    parser.add_argument("--local", action="store_true", help="label failures as --local for the caller")
    args = parser.parse_args()

    plugin, _, rel = split_target(args.name)
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
    print(f"proposal-path=.reports/calibrate/{args.timestamp}/{args.name.lstrip('/')}/proposal.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
