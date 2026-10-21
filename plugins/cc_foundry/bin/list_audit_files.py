#!/usr/bin/env python
"""list_audit_files.py — inventory the config files /foundry:audit covers, with block counts.

Replaces the inline Check 17 sweep. It prints one row per file:

    FILE                                                    BLOCKS
    cc_foundry/skills/audit/SKILL.md                            12

In ``--local`` mode the sweep covers the plugin source tree — skill entrypoints,
their ``modes/`` and ``templates/``, the per-plugin ``skills/_shared/``, and the
flat ``agents/`` and ``rules/`` files. Sidecar fragments under ``references/`` and
long-form rule bodies under ``rules/_full/`` are deliberately outside the sweep,
which is why the agent and rule patterns stay one level deep.

Otherwise it covers the installed layout under ``.claude/``: skill entrypoints and
flat agent files.

BLOCKS counts fenced code blocks — opening fences divided by two, the same
approximation the shell original used.

``--entrypoints`` narrows the local sweep to skill entrypoints and flat agent files — the
installed sweep is already that narrow. ``--paths-only`` prints bare paths instead of the
table; with ``-0`` they are NUL-terminated for ``xargs -0``, so a per-file checker scans
whichever tree the audit runs against.

Usage:
    list_audit_files.py [--local] [--entrypoints] [--paths-only [-0]] [--root <dir>] [--claude-dir <dir>]

Exit codes:
    0   always — this is an inventory, not a gate
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

#: Source-tree patterns, relative to the plugins root.
LOCAL_PATTERNS = (
    "*/skills/*/SKILL.md",
    "*/skills/*/modes/*.md",
    "*/skills/_shared/*.md",
    "*/skills/*/templates/*.md",
    "*/agents/*.md",
    "*/rules/*.md",
)

#: Installed-layout patterns, relative to the .claude directory.
INSTALLED_PATTERNS = ("skills/*/SKILL.md", "agents/*.md")

#: Source-tree entrypoint patterns (skill entrypoints and flat agents), relative to the plugins root.
LOCAL_ENTRYPOINT_PATTERNS = ("*/skills/*/SKILL.md", "*/agents/*.md")


def collect(root: Path, patterns: tuple[str, ...]) -> list[Path]:
    """Return the sorted, de-duplicated files matching any pattern under root."""
    found: set[Path] = set()
    for pattern in patterns:
        found.update(p for p in root.glob(pattern) if p.is_file())
    return sorted(found)


def count_blocks(path: Path) -> int:
    """Return the number of fenced code blocks in a Markdown file."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    fences = sum(1 for line in text.splitlines() if line.startswith("```"))
    return fences // 2


def report(files: list[Path], root: Path) -> None:
    """Print the inventory table for a file list."""
    print(f"{'FILE':<55} {'BLOCKS'}")
    for path in files:
        try:
            label = path.relative_to(root).as_posix()
        except ValueError:
            label = path.as_posix()
        print(f"{label:<55} {count_blocks(path)}")


def emit_paths(files: list[Path], *, null_terminated: bool) -> None:
    """Print bare file paths, NUL- or newline-terminated, ready to pipe into ``xargs``."""
    end = "\0" if null_terminated else "\n"
    sys.stdout.write("".join(f"{path.as_posix()}{end}" for path in files))


def main() -> int:
    """Inventory the config files /foundry:audit covers, with block counts."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--local", action="store_true", help="sweep the plugin source tree")
    parser.add_argument("--entrypoints", action="store_true", help="with --local: skill entrypoints and agents only")
    parser.add_argument("--paths-only", action="store_true", help="print bare paths instead of the table")
    parser.add_argument("-0", "--null", action="store_true", help="NUL-terminate paths (needs --paths-only)")
    parser.add_argument("--root", default="plugins", help="plugins root for --local")
    parser.add_argument("--claude-dir", default=".claude", help="installed layout root")
    args = parser.parse_args()
    if args.null and not args.paths_only:
        parser.error("-0/--null requires --paths-only")

    root = Path(args.root) if args.local else Path(args.claude_dir)
    local_patterns = LOCAL_ENTRYPOINT_PATTERNS if args.entrypoints else LOCAL_PATTERNS
    files = collect(root, local_patterns if args.local else INSTALLED_PATTERNS)
    if args.paths_only:
        emit_paths(files, null_terminated=args.null)
    else:
        report(files, root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
