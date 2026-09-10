#!/usr/bin/env python
"""check_orphaned_bin.py — detect bin/ scripts not referenced in plugin .md files.

Walks plugins/*/bin/ for .py and .sh scripts, then searches ALL plugins' .md
files for the script basename. Cross-plugin callers (script in plugin A called
from plugin B's SKILL.md) are found correctly. Scripts starting with underscore
are skipped (private modules imported by other bin/ scripts, not called directly).

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/check_orphaned_bin.py" [--plugins-dir DIR]

Options:
    --plugins-dir DIR   Root dir containing plugin subdirs (default: plugins/)

Output (stdout):
    One finding line per orphan + hint line, or a single pass line.

Exit codes:
    0   all bin/ scripts are referenced
    1   one or more orphaned scripts found
    2   argument error
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# Guard against pathological inputs that would exhaust heap memory when read
# in one shot. 10 MB is well above any realistic Markdown / agent file.
_MAX_FILE_SIZE = 10 * 1024 * 1024
_SCRIPT_BOUNDARY_CHARS = r"A-Za-z0-9_.-"


@dataclass
class OrphanFinding:
    """A bin/ script not referenced by any .md file in its plugin tree."""

    script_path: str
    plugin: str
    script: str


def iter_bin_scripts(plugins_dir: Path) -> list[tuple[str, str, str]]:
    """Return (plugin_name, script_basename, full_path) for each non-private bin/ script.

    Skips files whose names start with ``_`` (private modules, not callable directly).

    Args:
        plugins_dir: Path to the directory containing plugin subdirectories.

    Returns:
        Sorted list of (plugin, script_basename, absolute_path) tuples.

    Examples:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as d:
        ...     p = Path(d)
        ...     (p / "myplugin" / "bin").mkdir(parents=True)
        ...     _ = (p / "myplugin" / "bin" / "foo.py").write_text("x")
        ...     _ = (p / "myplugin" / "bin" / "_bar.py").write_text("x")
        ...     _ = (p / "myplugin" / "bin" / "bar.sh").write_text("x")
        ...     result = iter_bin_scripts(p)
        ...     [(r[0], r[1]) for r in result]
        [('myplugin', 'bar.sh'), ('myplugin', 'foo.py')]
    """
    results: list[tuple[str, str, str]] = []
    for plugin_dir in sorted(plugins_dir.iterdir()):
        if not plugin_dir.is_dir():
            continue
        bin_dir = plugin_dir / "bin"
        if not bin_dir.is_dir():
            continue
        for script in sorted(bin_dir.iterdir()):
            if not script.is_file():
                continue
            if script.suffix not in (".py", ".sh"):
                continue
            if script.name.startswith("_"):
                continue
            results.append((plugin_dir.name, script.name, script.as_posix()))
    return results


def is_referenced(script_name: str, search_dir: Path) -> bool:
    """Return True if script_name appears in any .md file under search_dir.

    Searches recursively. Match is a plain substring check on the script basename,
    which covers the canonical caller pattern
    ``${CLAUDE_PLUGIN_ROOT}/bin/<script_name>`` as well as prose mentions.
    Pass the plugins root (not a single plugin dir) to detect cross-plugin callers.

    Args:
        script_name: Basename of the script (e.g. ``extract_code_blocks.py``).
        search_dir: Directory tree to walk (use plugins root for cross-plugin coverage).

    Returns:
        True if at least one .md file contains script_name.

    Examples:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as d:
        ...     p = Path(d)
        ...     (p / "skills").mkdir()
        ...     _ = (p / "skills" / "SKILL.md").write_text("run bin/foo.py here")
        ...     is_referenced("foo.py", p)
        True
        >>> with tempfile.TemporaryDirectory() as d:
        ...     p = Path(d)
        ...     (p / "skills").mkdir()
        ...     _ = (p / "skills" / "SKILL.md").write_text("nothing here")
        ...     is_referenced("foo.py", p)
        False
    """
    script_ref_re = re.compile(
        rf"(?<![{_SCRIPT_BOUNDARY_CHARS}]){re.escape(script_name)}(?![{_SCRIPT_BOUNDARY_CHARS}])"
    )
    for dirpath, dirs, filenames in os.walk(search_dir):
        depth = len(Path(dirpath).relative_to(search_dir).parts)
        if depth >= 10:
            dirs.clear()  # don't recurse deeper
        for fn in filenames:
            if not fn.endswith(".md"):
                continue
            md_path = Path(dirpath) / fn
            try:
                if md_path.stat().st_size > _MAX_FILE_SIZE:
                    continue
                text = md_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if script_ref_re.search(text):
                return True
    return False


def find_orphans(plugins_dir: Path) -> list[OrphanFinding]:
    """Return all bin/ scripts not referenced in any plugin's .md files.

    Searches the entire plugins tree (not just the owning plugin) so that
    cross-plugin callers are found correctly.

    Args:
        plugins_dir: Root directory containing plugin subdirectories.

    Returns:
        List of OrphanFinding objects (empty when all scripts are referenced).
    """
    orphans: list[OrphanFinding] = []
    for plugin, script, full_path in iter_bin_scripts(plugins_dir):
        if not is_referenced(script, plugins_dir):
            orphans.append(OrphanFinding(script_path=full_path, plugin=plugin, script=script))
    return orphans


def main(argv: list[str] | None = None) -> int:
    """Report orphaned executable scripts with ASCII status labels for legacy consoles."""
    parser = argparse.ArgumentParser(
        prog="check_orphaned_bin",
        description="Detect bin/ scripts not referenced in plugin .md files.",
    )
    parser.add_argument(
        "--plugins-dir",
        default="plugins",
        metavar="DIR",
        help="Root dir containing plugin subdirs (default: plugins/).",
    )
    args = parser.parse_args(argv)

    # Normalise argv path so ``..`` components cannot escape the project tree.
    plugins_dir = Path(args.plugins_dir).resolve()
    project_root = Path.cwd().resolve()
    try:
        plugins_dir.relative_to(project_root)
    except ValueError:
        print(
            f"! SECURITY: --plugins-dir must be within project root: {args.plugins_dir}",
            file=sys.stderr,
        )
        return 2
    if not plugins_dir.is_dir():
        print(f"error: {args.plugins_dir!r} is not a directory", file=sys.stderr)
        return 2

    orphans = find_orphans(plugins_dir)
    if orphans:
        for o in orphans:
            print(
                f"WARN 32d: {o.script_path}"
                f" - bin/ script not referenced in any plugins/{o.plugin}/**/*.md file"
                f"\n  hint: wire to SKILL.md caller pattern, or delete if no longer needed"
            )
        return 1

    print("OK: Check 32d - all bin/ scripts referenced in plugin .md files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
