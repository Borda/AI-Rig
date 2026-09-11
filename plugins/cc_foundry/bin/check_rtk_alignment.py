#!/usr/bin/env python
"""check_rtk_alignment.py — compare the RTK rewrite hook's prefix list against the installed RTK.

Replaces the inline Check 10 shell block of /foundry:audit. Two directions are
checked, with the same wording the audit severity table maps onto:

- a prefix listed in the hook that ``rtk --help`` does not mention → INVALID
- a filterable RTK subcommand the hook does not list → MISSING

Meta subcommands (``gain``, ``discover``, ``proxy``, ``init``, ``version``,
``help``) are never expected in the hook and are skipped in the MISSING pass.

The check is skipped, with exit 0, when RTK is not installed or the hook file is
absent — the same fail-open behaviour the shell block had. ``node`` is no longer
needed: the prefix array is read with a regex here.

Usage:
    check_rtk_alignment.py [--hook <path>] [--timeout SECS]

Exit codes:
    0   always — findings are reported on stdout with their severity markers,
        matching the inline block this replaced. A non-zero status would read as
        "the check failed to run", not "the check found something".
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

_PREFIX_ARRAY = re.compile(r"RTK_PREFIXES\s*=\s*\[([^\]]*)\]", re.DOTALL)
_QUOTED = re.compile(r"""["']([^"']+)["']""")
# `rtk --help` lists subcommands indented two to four spaces.
_HELP_COMMAND = re.compile(r"^\s{2,4}([a-z][a-z0-9_-]+)", re.MULTILINE)

META_COMMANDS = frozenset({"gain", "discover", "proxy", "init", "version", "help"})


def hook_prefixes(hook: Path) -> list[str]:
    """Return the prefixes declared in the hook's RTK_PREFIXES array."""
    try:
        source = hook.read_text(encoding="utf-8")
    except OSError:
        return []
    match = _PREFIX_ARRAY.search(source)
    if not match:
        return []
    return _QUOTED.findall(match.group(1))


def help_commands(help_text: str) -> set[str]:
    """Return the subcommand names `rtk --help` advertises."""
    return set(_HELP_COMMAND.findall(help_text))


def _word_in(needle: str, haystack: str) -> bool:
    """Report whether `needle` occurs in `haystack` as a whole word.

    Reproduces ``grep -w``: word characters are ``[0-9A-Za-z_]``, so a hyphen is a boundary and ``bar`` matches inside
    ``foo-bar``. Membership in the parsed subcommand set is stricter than the shell original was — a command named only
    in a prose line of ``rtk --help`` would be reported INVALID, a false positive at **high** severity.
    """
    pattern = rf"(?<![0-9A-Za-z_]){re.escape(needle)}(?![0-9A-Za-z_])"
    return re.search(pattern, haystack) is not None


def compare(prefixes: list[str], help_text: str) -> tuple[list[str], list[str]]:
    """Return (invalid prefixes, missing subcommands) for one hook/RTK pair."""
    invalid = [p for p in prefixes if not _word_in(p, help_text)]
    listed = " ".join(prefixes)
    missing = sorted(c for c in help_commands(help_text) if c not in META_COMMANDS and not _word_in(c, listed))
    return invalid, missing


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--hook", default=".claude/hooks/rtk-rewrite.js")
    parser.add_argument("--timeout", type=float, default=30.0, help="seconds for `rtk --help`")
    args = parser.parse_args()

    print("=== Check 10: RTK hook alignment ===")
    if shutil.which("rtk") is None:
        print("⚠ SKIPPED: Check 10 — rtk not installed")
        return 0
    hook = Path(args.hook)
    if not hook.is_file():
        print(f"⚠ SKIPPED: Check 10 — {hook.as_posix()} not found")
        return 0

    prefixes = hook_prefixes(hook)
    if not prefixes:
        print("⚠ SKIPPED: Check 10 — could not parse RTK_PREFIXES from hook file")
        return 0

    try:
        result = subprocess.run(  # noqa: S603 — fixed argv, no user input
            ["rtk", "--help"],  # noqa: S607
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

    invalid, missing = compare(prefixes, result.stdout + result.stderr)
    for prefix in invalid:
        print(f"! INVALID hook prefix: '{prefix}' — not a recognized RTK subcommand")
    for command in missing:
        print(f"⚠ MISSING hook prefix: '{command}' — RTK supports filtering this command but hook does not list it")
    if not invalid and not missing:
        print("✓ OK: Check 10 — RTK hook prefixes aligned with installed RTK version")
    return 0


if __name__ == "__main__":
    sys.exit(main())
