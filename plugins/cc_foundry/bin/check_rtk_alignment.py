#!/usr/bin/env python
"""check_rtk_alignment.py — check that every program the RTK rewrite hook routes is a subcommand of the installed RTK.

Replaces the inline Check 10 shell block of /foundry:audit. One direction is checked: a prefix listed in the hook's
``SAFE_PREFIXES`` or ``GUARDED_PREFIXES`` that ``rtk --help`` does not mention → INVALID, because the hook would rewrite
a command into ``rtk <prefix> …`` that RTK rejects.

The reverse direction (an RTK subcommand the hook does not route) is deliberately not reported. The hook routes only
verbs whose RTK output passed a fidelity comparison against the raw command, so an RTK subcommand absent from the hook
is a measured exclusion, never a gap; the exclusions and their reasons are listed in the hook's header comment.

The check is skipped, with exit 0, when RTK is not installed or the hook file is absent — the same fail-open behaviour
the shell block had. ``node`` is not needed: the prefix arrays are read with a regex here.

Usage:
    check_rtk_alignment.py [--hook <path>] [--timeout SECS]

Exit codes:
    0   always — findings are reported on stdout with their severity markers,
        matching the inline block this replaced. A non-zero status would read as
        "the check failed to run", not "the check found something".
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

#: Matches the start of the ``SAFE_PREFIXES = [...]`` or ``GUARDED_PREFIXES = [...]`` array in the hook source.
_PREFIX_ARRAYS = (
    re.compile(r"\bSAFE_PREFIXES\s*=\s*\["),
    re.compile(r"\bGUARDED_PREFIXES\s*=\s*\["),
)
#: Matches a single- or double-quoted string, capturing its contents.
_QUOTED = re.compile(r"""["']([^"']+)["']""")
#: The hook ships next to ``bin/`` in the plugin, so the default needs no project-local copy.
_DEFAULT_HOOK = Path(__file__).resolve().parent.parent / "hooks" / "rtk-rewrite.js"


def hook_prefixes(hook: Path) -> list[str]:
    """Return the prefixes declared in the hook's SAFE_PREFIXES and GUARDED_PREFIXES arrays."""
    try:
        source = hook.read_text(encoding="utf-8")
    except OSError:
        return []
    prefixes: list[str] = []
    for pattern in _PREFIX_ARRAYS:
        match = pattern.search(source)
        if not match:
            continue
        end = source.find("]", match.end())
        if end >= 0:
            prefixes.extend(_QUOTED.findall(source[match.end() : end]))
    return prefixes


def _word_in(needle: str, haystack: str) -> bool:
    """Report whether `needle` occurs in `haystack` as a whole word.

    Reproduces ``grep -w``: word characters are ``[0-9A-Za-z_]``, so a hyphen is a boundary and ``bar`` matches inside
    ``foo-bar``. A command named only in a prose line of ``rtk --help`` counts as valid, the same as the shell original.
    """
    pattern = rf"(?<![0-9A-Za-z_]){re.escape(needle)}(?![0-9A-Za-z_])"
    return re.search(pattern, haystack) is not None


def invalid_prefixes(prefixes: list[str], help_text: str) -> list[str]:
    """Return the hook prefixes that `rtk --help` does not name as a whole word."""
    return [p for p in prefixes if not _word_in(p, help_text)]


def resolve_rtk() -> str | None:
    """Find an absolute RTK executable outside the checkout without implicit cwd lookup."""
    root = Path.cwd().resolve()
    extensions = [""]
    if sys.platform == "win32":
        extensions.extend(ext for ext in os.environ.get("PATHEXT", "").split(os.pathsep) if ext)
    for entry in os.get_exec_path():
        directory = Path(entry or os.curdir).resolve()
        if directory.is_relative_to(root):
            continue
        for extension in extensions:
            found = shutil.which(str(directory / ("rtk" + extension)))
            if found is None:
                continue
            candidate = Path(found).resolve()
            if not candidate.is_relative_to(root):
                return str(candidate)
    return None


def main() -> int:
    """Check the RTK rewrite hook's routed prefixes against the installed RTK."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--hook", default=str(_DEFAULT_HOOK), help="hook to check (default: this plugin's hook)")
    parser.add_argument("--timeout", type=float, default=30.0, help="seconds for `rtk --help`")
    args = parser.parse_args()

    print("=== Check 10: RTK hook alignment ===")
    executable = resolve_rtk()
    if executable is None:
        print("⚠ SKIPPED: Check 10 — rtk not installed")
        return 0
    hook = Path(args.hook)
    if not hook.is_file():
        print(f"⚠ SKIPPED: Check 10 — {hook.as_posix()} not found")
        return 0

    prefixes = hook_prefixes(hook)
    if not prefixes:
        print("⚠ SKIPPED: Check 10 — could not parse SAFE_PREFIXES or GUARDED_PREFIXES from hook file")
        return 0

    try:
        result = subprocess.run(  # noqa: S603 - absolute executable resolved outside the checkout
            [executable, "--help"],
            capture_output=True,
            text=True,
            timeout=args.timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        # Includes TimeoutExpired. The shell original could not crash here, so an
        # unhandled traceback would be a regression, not a finding.
        print("⚠ SKIPPED: Check 10 — `rtk --help` failed or timed out")
        return 0

    invalid = invalid_prefixes(prefixes, result.stdout + result.stderr)
    for prefix in invalid:
        print(f"! INVALID hook prefix: '{prefix}' — not a recognized RTK subcommand")
    if not invalid:
        print("✓ OK: Check 10 — every routed hook prefix is an RTK subcommand")
    return 0


if __name__ == "__main__":
    sys.exit(main())
