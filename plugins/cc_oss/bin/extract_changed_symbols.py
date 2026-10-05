#!/usr/bin/env python
"""extract_changed_symbols.py — extract added/removed public Python symbols from diffs.

Extracts ``class`` and ``def`` names changed in ``__init__.py`` files
within a given git diff range. Output is newline-separated, sort-u'd
symbol names. Prints nothing (exit 0) for empty/invalid ranges, absent
``__init__.py`` files, or diffs with no class/def changes.

Usage:
    extract_changed_symbols.py [<git_diff_range>]

Args:
    git_diff_range: any range git diff understands (e.g. HEAD~1..HEAD,
                    origin/main..HEAD, a..b). Defaults to HEAD~1..HEAD.

Caller pattern (shepherd.md):
    CHANGED_SYMBOLS=$(extract_changed_symbols.py "$RANGE")
    [ -z "$CHANGED_SYMBOLS" ] && echo "No changed symbols — skipping"

Exit codes:
    0 — always (empty output signals no changes or invalid range)
    2 — bad/missing required argument (argparse default)
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from shutil import which

_SYMBOL_RE: re.Pattern[str] = re.compile(r"(?:class|def)\s+([A-Za-z_][A-Za-z0-9_]*)")
_MAX_INIT_FILES = 50


def _resolve(cmd: str) -> str:
    """Resolve a CLI tool to its absolute path.

    Args:
        cmd: Bare executable name (e.g. ``"git"``).

    Returns:
        Absolute path to the executable.

    Raises:
        FileNotFoundError: If ``cmd`` is not present on ``PATH``.

    Examples:
        >>> import shutil
        >>> _resolve("git") == shutil.which("git")
        True
    """
    p = which(cmd)
    if p is None:
        raise FileNotFoundError(f"executable not found on PATH: {cmd}")
    return p


def _rev_parse_ok(git: str, ref: str) -> bool:
    """Return True if git can resolve ``ref``.

    Args:
        git: Absolute path to the git binary.
        ref: Git ref to validate.

    Returns:
        ``True`` if ``git rev-parse <ref>`` exits 0.

    Examples:
        No doctest — subprocess-dependent; covered by pytest.
    """
    result = subprocess.run(
        [git, "rev-parse", ref],
        capture_output=True,
        check=False,
        timeout=3,
    )
    return result.returncode == 0


def _find_init_files() -> list[Path]:
    """Find ``__init__.py`` files under the CWD, excluding hidden dirs and node_modules.

    Caps at ``_MAX_INIT_FILES`` paths to handle pathological monorepos.

    Returns:
        List of matching ``Path`` objects.

    Examples:
        >>> isinstance(_find_init_files(), list)
        True
    """
    results: list[Path] = []
    for p in Path(".").rglob("__init__.py"):
        if any(part.startswith(".") for part in p.parts):
            continue
        if "node_modules" in p.parts:
            continue
        results.append(p)
        if len(results) >= _MAX_INIT_FILES:
            break
    return results


def _extract_from_diff(git: str, init_files: list[Path], range_arg: str) -> set[str]:
    """Run ``git diff <range>`` on each file and extract class/def symbol names.

    Args:
        git: Absolute path to the git binary.
        init_files: ``__init__.py`` paths to diff.
        range_arg: Git diff range string.

    Returns:
        Set of symbol name strings found in added/removed diff lines.

    Examples:
        No doctest — subprocess-dependent; covered by pytest.
    """
    symbols: set[str] = set()
    for path in init_files:
        result = subprocess.run(
            [git, "diff", range_arg, "--", str(path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        for line in result.stdout.splitlines():
            if not line or line[0] not in ("+", "-"):
                continue
            if len(line) > 1 and line[1] in ("+", "-"):
                continue
            match = _SYMBOL_RE.search(line)
            if match:
                symbols.add(match.group(1))
    return symbols


def main(argv: list[str] | None = None) -> int:
    """Entry point — mirrors ``extract_changed_symbols.sh`` behaviour.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Always 0 — empty output signals no changes or invalid range.

    Examples:
        No doctest — subprocess-dependent; covered by pytest.
    """
    parser = argparse.ArgumentParser(
        prog="extract_changed_symbols.py",
        description="Extract added/removed public Python symbols from an __init__.py diff range.",
    )
    # nargs="*" preserves the legacy contract: extra positional tokens are absorbed
    # (only the first is used), and a missing range falls back to HEAD~1..HEAD —
    # argparse never rejects surplus positionals the way nargs="?" would.
    parser.add_argument(
        "range_tokens",
        nargs="*",
        metavar="git_diff_range",
        help="Git diff range (e.g. HEAD~1..HEAD, a..b). Defaults to HEAD~1..HEAD.",
    )
    args = parser.parse_args(argv)

    sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]
    range_arg = args.range_tokens[0] if args.range_tokens else "HEAD~1..HEAD"

    git = _resolve("git")

    if ".." in range_arg:
        left, _, right = range_arg.partition("..")
        right = right or "HEAD"
        if not _rev_parse_ok(git, left) or not _rev_parse_ok(git, right):
            return 0
    else:
        if not _rev_parse_ok(git, range_arg):
            return 0

    init_files = _find_init_files()
    if not init_files:
        return 0

    symbols = _extract_from_diff(git, init_files, range_arg)
    for sym in sorted(symbols):
        print(sym)
    return 0


if __name__ == "__main__":
    sys.exit(main())
