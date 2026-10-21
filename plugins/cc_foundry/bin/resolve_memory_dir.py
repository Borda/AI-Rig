#!/usr/bin/env python
"""resolve_memory_dir.py — resolve project-scoped Claude memory directory.

Single slug derivation for every foundry consumer that has to locate ``~/.claude/projects/<slug>/memory``. The slug
mirrors how Claude Code names its per-project directory (read from the installed Claude Code 2.1.304 bundle, confirmed
against the directory names under ``~/.claude/projects/``):

1. every UTF-16 code unit outside ``[A-Za-z0-9]`` becomes one ``-`` (no case folding, no run collapsing, no stripping),
2. a slug longer than 200 characters is cut to 200 and gets ``-<hash>`` appended, where ``<hash>`` is the absolute value
   of Java's 32-bit ``String.hashCode`` of the original path (UTF-16 code units), written in base 36.

Not modelled: a ``CLAUDE_CONFIG_DIR`` relocation of the Claude home, an ``autoMemoryDirectory`` setting, and Claude
Code keying a linked git worktree by its canonical repository root. Callers needing those pass the project root
explicitly or prefer the absolute path Claude Code injects into the session prompt.

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
import struct
import subprocess
import sys
from pathlib import Path

#: Matches each single character outside ``A-Za-z0-9``; Claude Code replaces every one with its own hyphen.
_NON_ALNUM_RE = re.compile(r"[^A-Za-z0-9]")

#: Longest slug kept verbatim; longer ones are truncated to this length and get a hash suffix.
_MAX_SLUG_LEN = 200

_BASE36_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"


def _utf16_code_units(text: str) -> tuple[int, ...]:
    """Return the UTF-16 code units of ``text``, the unit JavaScript string indexing counts in.

    Examples:
        >>> _utf16_code_units("a\\u00e9")
        (97, 233)
        >>> _utf16_code_units("\\U0001f600")
        (55357, 56832)
    """
    raw = text.encode("utf-16-le", errors="surrogatepass")
    return struct.unpack(f"<{len(raw) // 2}H", raw)


def _path_hash(project_path: str) -> str:
    """Hash suffix for over-long slugs: ``abs(int32 hashCode)`` in base 36, as Claude Code computes it.

    Examples:
        >>> _path_hash("")
        '0'
        >>> _path_hash("a")
        '2p'
    """
    value = 0
    for unit in _utf16_code_units(project_path):
        value = (value * 31 + unit) & 0xFFFFFFFF
    if value >= 0x80000000:
        value -= 0x100000000
    number = abs(value)
    digits = ""
    while number:
        number, remainder = divmod(number, 36)
        digits = _BASE36_DIGITS[remainder] + digits
    return digits or "0"


def slugify(project_path: str) -> str:
    """Convert a project root path to the directory name Claude Code uses under ``~/.claude/projects``.

    Args:
        project_path: Absolute project root, as Claude Code sees it (POSIX or Windows form).

    Returns:
        Slug with each non-alphanumeric UTF-16 code unit replaced by ``-``; slugs over 200 characters are cut to 200
        and suffixed with ``-<hash>``.

    Examples:
        >>> slugify("/home/user/MyProject")
        '-home-user-MyProject'
        >>> slugify("/a b/c.d/e_f")
        '-a-b-c-d-e-f'
        >>> slugify("/repo/.claude/worktrees/x")
        '-repo--claude-worktrees-x'
        >>> slugify("C:\\\\Users\\\\me\\\\proj")
        'C--Users-me-proj'
        >>> slugify("/srv/\\U0001f600")
        '-srv---'
        >>> slugify("")
        ''
    """
    # a character outside the BMP is two UTF-16 units, and Claude Code replaces each unit
    slug = _NON_ALNUM_RE.sub(lambda match: "-" * len(_utf16_code_units(match.group())), project_path)
    if len(slug) <= _MAX_SLUG_LEN:
        return slug
    return f"{slug[:_MAX_SLUG_LEN]}-{_path_hash(project_path)}"


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
