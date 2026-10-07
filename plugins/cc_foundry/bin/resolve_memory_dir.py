#!/usr/bin/env python
"""resolve_memory_dir.py — resolve project-scoped Claude memory directory.

Canonical slug transform (matches global ``git-commit.md`` repo-slug algorithm):

1. lowercase
2. all non-alphanumeric runs collapsed to single ``-``
3. trailing ``-`` stripped

Prints absolute ``MEMORY_DIR`` path to stdout.

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_memory_dir.py" [<project-root>]

Arguments:
    project-root  Optional; defaults to ``git rev-parse --show-toplevel`` of cwd.

Exit codes:
    0  Success
    1  No project root resolvable (no arg, not inside git repo)
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

#: Matches each run of characters outside ``a-z0-9``, which is collapsed into a single hyphen in the slug.
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(project_path: str) -> str:
    """Convert project root path to canonical repo slug.

    Args:
        project_path: Absolute or relative filesystem path.

    Returns:
        Slug: lowercase, non-alphanumerics collapsed to ``-``, trailing ``-`` stripped.

    Examples:
        >>> slugify("/home/user/MyProject")
        '-home-user-myproject'
        >>> slugify("MyProject")
        'myproject'
        >>> slugify("foo/bar_baz!")
        'foo-bar-baz'
        >>> slugify("---abc---")
        '-abc'
        >>> slugify("")
        ''
    """
    lowered = project_path.lower()
    collapsed = _SLUG_RE.sub("-", lowered)
    return collapsed.rstrip("-")


def _git_toplevel(timeout: int = 5) -> str | None:
    """Return ``git rev-parse --show-toplevel`` of cwd, or None if not in a repo."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],  # noqa: S607 - git resolved via PATH on purpose
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    top = result.stdout.strip()
    return top or None


def resolve_memory_dir(project: str | None, timeout: int = 5) -> str | None:
    """Resolve the absolute MEMORY_DIR path for a project.

    Args:
        project: Explicit project root path; if None or empty, falls back to git.
        timeout: Subprocess timeout in seconds for git fallback (default: 5).

    Returns:
        Absolute path string, or None if no project root resolvable.
    """
    if not project:
        project = _git_toplevel(timeout=timeout)
    if not project:
        return None
    slug = slugify(project)
    home = Path(os.environ.get("HOME") or os.path.expanduser("~"))
    return str(home / ".claude" / "projects" / slug / "memory")


def main(argv: list[str] | None = None) -> int:
    """Resolve and print the selected memory directory."""
    parser = argparse.ArgumentParser(
        description="Resolve project-scoped Claude memory directory.",
        add_help=True,
    )
    parser.add_argument(
        "project_root",
        nargs="?",
        default=None,
        help="Optional project root; defaults to `git rev-parse --show-toplevel`.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=5,
        help="Subprocess timeout in seconds for git fallback (default: 5).",
    )
    args = parser.parse_args(argv)

    memory_dir = resolve_memory_dir(args.project_root, timeout=args.timeout)
    if memory_dir is None:
        return 1
    print(memory_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
