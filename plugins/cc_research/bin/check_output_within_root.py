#!/usr/bin/env python
"""check_output_within_root.py — verify a candidate output path stays within the project root.

Two modes:

* Two positional paths — pure containment check, no I/O (CWE-22 guard).
* ``--parse-out`` — parse ``--out <path>`` out of a skill's ``$ARGUMENTS``, reject traversal and
  escapes, and persist the resolved value to a sentinel. Python rather than a shell regex on
  purpose: the inline twin used ``[[ =~ ]]`` + ``${BASH_REMATCH[1]}``, and zsh — the harness
  shell — populates ``$match`` instead, so every ``--out`` silently resolved to empty and the
  validation it feeds never fired.

``--parse-out=…`` must use the attached spelling: ``$ARGUMENTS`` routinely starts with a flag,
which argparse would otherwise read as a missing option value.

Usage:
    check_output_within_root.py <candidate-path> <root-path>
    check_output_within_root.py --parse-out="$ARGUMENTS" --sentinel SLUG [--default-out PATH]

Exit codes:
    0 — candidate is within root (or equal to root); or --out parsed and persisted
    1 — candidate is outside root
    2 — bad/missing argument, --out contains traversal, --out escapes the project root,
        or the sentinel write failed
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

#: Unquoted single token, mirroring the `--out <path>` spelling the sweep skill documents.
_OUT_RE = re.compile(r"--out[ \t\r\f\v]+(\S+)")


def is_within_root(candidate: str, root: str) -> bool:
    """Return True if candidate path is within or equal to root.

    Args:
        candidate: Candidate output path to check.
        root: Project root the candidate must stay within.

    Returns:
        True if the resolved candidate equals or descends from the resolved root.

    Examples:
        >>> import tempfile, os
        >>> td = tempfile.mkdtemp()
        >>> is_within_root(os.path.join(td, 'sub'), td)
        True
        >>> is_within_root(td, td)
        True
        >>> is_within_root('/opt/evil', td)
        False
    """
    p = os.path.realpath(candidate)
    b = os.path.realpath(root)
    return p == b or p.startswith(b + os.sep)


def parse_out_flag(arguments: str) -> str:
    """Return the ``--out`` value from a raw argument string, or an empty string.

    Args:
        arguments: Raw ``$ARGUMENTS`` string.

    Returns:
        The path token following ``--out``.

    Examples:
        >>> parse_out_flag("goal text --out docs/program.md --team")
        'docs/program.md'
        >>> parse_out_flag("goal text")
        ''
    """
    match = _OUT_RE.search(arguments)
    return match.group(1) if match else ""


def project_root(timeout: int = 5) -> str:
    """Return the git top level, falling back to the working directory.

    Args:
        timeout: Maximum seconds to wait for git.

    Returns:
        Absolute project-root path.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return os.getcwd()
    root = result.stdout.strip()
    return root if result.returncode == 0 and root else os.getcwd()


def _sentinel_dir() -> Path:
    """Return the session temp directory (never a hardcoded ``/tmp`` — absent on native Windows)."""
    return Path(os.environ.get("TMPDIR") or tempfile.gettempdir())


def _session_token() -> str:
    """Return the sentinel session suffix; ``os.getppid()`` would name the calling shell, not the session."""
    return os.environ.get("CSID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "shared"


def _run_parse_out(args: argparse.Namespace) -> int:
    """Validate and persist the ``--out`` path pulled from a skill's arguments.

    Args:
        args: Parsed CLI namespace carrying ``parse_out``, ``sentinel``, ``label``, ``default_out``.

    Returns:
        ``0`` when persisted, ``2`` on traversal, escape, or write failure.
    """
    label = args.label or args.sentinel.split("-")[0]
    out = parse_out_flag(args.parse_out)
    if out and ".." in out:
        print(f"{label}: invalid --out path (path traversal not allowed): {out}", file=sys.stderr)
        return 2
    if out and not is_within_root(out, project_root(args.timeout)):
        print(f"{label}: --out path escapes project root: {out}", file=sys.stderr)
        return 2
    sentinel = _sentinel_dir() / f"{args.sentinel}-{_session_token()}"
    try:
        # Trailing newline is the `IFS= read -r` reload contract.
        sentinel.write_text((out or args.default_out) + "\n", encoding="utf-8", newline="\n")
    except OSError as exc:
        print(f"{label}: cannot write {sentinel}: {exc}", file=sys.stderr)
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    """Check whether an output path stays within its allowed root.

    No doctest — argv-dependent; covered by pytest.

    Args:
        argv: Optional argv override (defaults to ``sys.argv[1:]``).

    Returns:
        ``0`` if candidate is within root, ``1`` if outside; ``2`` on bad args or a rejected ``--out``.
    """
    parser = argparse.ArgumentParser(
        prog="check_output_within_root.py",
        description="Verify a candidate output path stays within the project root.",
    )
    parser.add_argument("candidate_path", nargs="?", help="Candidate output path to check.")
    parser.add_argument("root_path", nargs="?", help="Project root the candidate must stay within.")
    parser.add_argument("--parse-out", help='Raw "$ARGUMENTS" string to pull --out from (attached form).')
    parser.add_argument("--sentinel", help="Sentinel slug to persist the resolved --out under.")
    parser.add_argument("--label", default="", help="Message prefix; defaults to the sentinel slug's first segment.")
    parser.add_argument("--default-out", default="program.md", help="Value persisted when --out is absent.")
    parser.add_argument("--timeout", type=int, default=5, help="Max seconds to wait for git (default: 5).")
    args = parser.parse_args(argv)

    if args.parse_out is not None:
        if not args.sentinel:
            parser.error("--parse-out requires --sentinel")
        return _run_parse_out(args)
    if not args.candidate_path or not args.root_path:
        parser.error("candidate_path and root_path are required without --parse-out")
    return 0 if is_within_root(args.candidate_path, args.root_path) else 1


if __name__ == "__main__":
    sys.exit(main())
