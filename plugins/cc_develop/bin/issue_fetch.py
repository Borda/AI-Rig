#!/usr/bin/env python
"""issue_fetch.py — strip a leading ``#`` from an issue number and fetch the issue via ``gh``.

Forwards ``gh issue view <num> --comments [--repo <owner/repo>]`` with
stdout/stderr inherited from the caller. Validates that the stripped
argument is digits-only and exits 1 with a stderr message otherwise.
Propagates ``gh``'s exit code on success.

The first positional argument is the raw ``$ARGUMENTS`` blob and may itself contain
trailing ``--``-shaped tokens (e.g. ``"123 --repo owner/repo"``). Only the first
whitespace-separated token is used as the issue number; the rest is ignored. The blob is
kept opaque — it is sliced by this script's own token scan, never handed to argparse's
matcher, so an embedded ``--repo`` inside the blob does not confuse flag parsing. Pass
``--repo <owner/repo>`` as a separate argv element to route the request to an upstream
repository (fork workflow). argparse is present only to supply ``-h/--help``.

Usage:
    issue_fetch.py <issue-number-with-optional-hash> [--repo <owner/repo>]

Exit codes:
    0   — success (``gh`` exited 0); also argparse's ``--help`` exit.
    1   — invalid (empty or non-numeric) issue number.
    *   — any other exit code reflects ``gh``'s own exit status.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from shutil import which


def _resolve(cmd: str) -> str:
    """Resolve ``cmd`` to an absolute path using ``shutil.which``.

    Args:
        cmd: Bare executable name (e.g. ``"gh"``).

    Returns:
        Absolute path to the executable.

    Raises:
        FileNotFoundError: If ``cmd`` is not present on ``PATH``.
    """
    resolved = which(cmd)
    if resolved is None:
        raise FileNotFoundError(f"executable not found on PATH: {cmd}")
    return resolved


def main(argv: list[str] | None = None) -> int:
    """Entry point — mirrors ``issue-fetch.sh`` behaviour.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Exit code: 1 on invalid input; ``gh``'s exit code otherwise.

    No doctest — forwards to ``gh`` via subprocess and reads argv; covered by pytest.
    """
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]
    args = list(sys.argv[1:] if argv is None else argv)

    # argparse supplies only ``-h/--help``; the $ARGUMENTS blob carries ``--``-shaped tokens that
    # must stay opaque, so ``--repo`` and the blob are dispatched by the direct scan below.
    if args and args[0] in {"-h", "--help"}:
        parser = argparse.ArgumentParser(
            prog="issue_fetch.py",
            description="Strip a leading # from an issue number and fetch the issue via gh.",
        )
        parser.add_argument("arguments", nargs="?", help="Raw $ARGUMENTS blob; first token is the issue number.")
        parser.add_argument("--repo", metavar="OWNER/REPO", help="Optional upstream repo override (fork workflow).")
        parser.parse_args(args)  # exits 0 after printing help

    # Extract the ``--repo`` flag (supports fork/upstream workflow).
    repo: str | None = None
    positional: list[str] = []
    i = 0
    while i < len(args):
        if args[i] == "--repo" and i + 1 < len(args):
            repo = args[i + 1]
            i += 2
        else:
            positional.append(args[i])
            i += 1

    # The first positional arg may be the raw ``$ARGUMENTS`` string which can include
    # trailing flags (e.g. "123 --repo owner/repo"). Extract only the first token.
    raw_str = positional[0] if positional else ""
    raw = raw_str.split()[0] if raw_str.strip() else ""
    issue_num = raw.removeprefix("#")
    if not issue_num or not issue_num.isdigit():
        print(f"issue-fetch: invalid issue number: '{issue_num}'", file=sys.stderr)
        return 1
    gh = _resolve("gh")
    cmd = [gh, "issue", "view", issue_num, "--comments"]
    if repo:
        cmd += ["--repo", repo]
    # stdout/stderr inherited from caller — caller sees combined output as in bash `2>&1`.
    result = subprocess.run(cmd, check=False, timeout=30)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
