#!/usr/bin/env python
"""parse_scan_args.py — extract ``--root`` and ``--incremental`` flags from a single $ARGUMENTS string.

Usage:
    parse_scan_args.py "$ARGUMENTS" [--nul-output <file>] [--print-root]

Output (default):
    Shell-quoted argument tokens suitable for ``eval``:

        eval "SCAN_ARGS=( $(python3 parse_scan_args.py "$ARGUMENTS") )"

Output (--nul-output <file>):
    Writes each argument token NUL-delimited to <file>. No stdout.
    Caller reads safely with: while IFS= read -r -d '' arg; do ...; done < <file>

Output (--print-root):
    Prints just the resolved ``--root`` value (or '.' when absent) on stdout.

Exit codes:
    0  always (never fails on parsed input).
    1  --nul-output path validation failed.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import sys
import tempfile
from pathlib import Path

# ``--root <value>`` where value is one of:
#   - single-quoted: '...'  (no embedded single quotes)
#   - double-quoted: "..."  (no embedded double quotes)
#   - unquoted: any run of non-whitespace characters
# Matches first occurrence anywhere in the string. The three branches mirror
# the original sed pipeline's `t`-branch fallthrough semantics.
_ROOT_RE = re.compile(
    r"--root\s+(?:'([^']*)'|\"([^\"]*)\"|(\S+))",
)


def _extract_root(arguments: str) -> str | None:
    """Return the raw ``--root`` value from arguments, or None when absent.

    Args:
        arguments: Raw $ARGUMENTS string from the caller.

    Returns:
        The unquoted root path string, or ``None`` when ``--root`` is absent.

    Examples:
        >>> _extract_root("--root /abs/path")
        '/abs/path'
        >>> _extract_root("--root '/abs path/with spaces'")
        '/abs path/with spaces'
        >>> _extract_root("--incremental")
        >>> _extract_root("")
    """
    match = _ROOT_RE.search(arguments)
    if match is None:
        return None
    return next(group for group in match.groups() if group is not None)


def parse_scan_args(arguments: str) -> list[str]:
    """Parse the $ARGUMENTS string for ``--root`` and ``--incremental`` flags.

    Args:
        arguments: Raw $ARGUMENTS string from the caller.

    Returns:
        List of argument tokens (unquoted strings). Empty when neither flag is
        present. The ``--root`` value is the resolved path string; callers that
        need shell-safe output must apply :func:`shlex.quote` themselves.

    Examples:
        >>> parse_scan_args("--root /abs/path")
        ['--root', '/abs/path']
        >>> parse_scan_args("--root '/abs path/with spaces' --incremental")
        ['--root', '/abs path/with spaces', '--incremental']
        >>> parse_scan_args('--root "/abs/path"')
        ['--root', '/abs/path']
        >>> parse_scan_args("--incremental")
        ['--incremental']
        >>> parse_scan_args("")
        []
        >>> parse_scan_args("--incremental --root /opt/x")
        ['--root', '/opt/x', '--incremental']
    """
    tokens: list[str] = []

    root_val = _extract_root(arguments)
    if root_val is not None:
        tokens.extend(["--root", root_val])

    if "--incremental" in arguments:
        tokens.append("--incremental")

    return tokens


def format_scan_args(tokens: list[str]) -> str:
    """Render parsed tokens as the shell-quoted single-line form printed on stdout.

    ``--root`` and its value stay one space-separated pair so a caller can splice the
    result straight into a command line; every other token is quoted on its own.

    Args:
        tokens: Token list as returned by :func:`parse_scan_args`.

    Returns:
        Space-joined shell-quoted string. Empty string when ``tokens`` is empty.

    Examples:
        >>> format_scan_args(["--root", "/abs/path"])
        '--root /abs/path'
        >>> format_scan_args(["--root", "/abs path", "--incremental"])
        "--root '/abs path' --incremental"
        >>> format_scan_args([])
        ''
    """
    quoted_tokens: list[str] = []
    it = iter(tokens)
    for token in it:
        if token == "--root":
            root_val = next(it, "")
            quoted_tokens.append(f"--root {shlex.quote(root_val)}")
        else:
            quoted_tokens.append(shlex.quote(token))
    return " ".join(quoted_tokens)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: read $ARGUMENTS, emit arg tokens, return 0.

    Flags (after the positional $ARGUMENTS):
        --nul-output <file>  Write tokens NUL-delimited to <file>; no stdout.
        --print-root         Print only the resolved --root value (or '.').

    Args:
        argv: Optional argv override for testing. Defaults to ``sys.argv[1:]``.

    Returns:
        Exit code (0 on success, 1 when ``--nul-output`` path validation fails).

    Examples:
        >>> import os, tempfile
        >>> with tempfile.NamedTemporaryFile(delete=False) as f:
        ...     path = f.name
        >>> main(["--root /opt/x --incremental", "--nul-output", path])
        0
        >>> open(path, "rb").read() == b"--root\\x00/opt/x\\x00--incremental\\x00"
        True
        >>> os.unlink(path)
        >>> main(["--root /opt/x", "--print-root"])
        /opt/x
        0
    """
    args = sys.argv[1:] if argv is None else argv

    # arg[0] is the raw $ARGUMENTS blob (its embedded ``--root``/``--incremental`` tokens feed the
    # INNER parser below, NOT argparse). Extracted positionally BEFORE argparse so a blob
    # beginning with "--" is never handed to argparse's flag matcher — all call sites quote it
    # as one argv element. Only the genuine OUTER flags in args[1:] reach argparse.
    arguments = args[0] if args else ""
    rest = args[1:]

    parser = argparse.ArgumentParser(
        prog="parse_scan_args.py",
        description="Extract --root and --incremental flags from a single $ARGUMENTS blob.",
    )
    parser.add_argument("--nul-output", default=None, help="Write tokens NUL-delimited to <file>; no stdout.")
    parser.add_argument(
        "--print-root", action="store_true", help="Print only the resolved --root value (or '.') on stdout."
    )
    # parse_known_args: unknown outer tokens are silently absorbed (legacy "ignore" strictness — the
    # SKILL pre-rejects unsupported $ARGUMENTS flags upstream, so this never newly rejects a call site).
    parsed, _unknown = parser.parse_known_args(rest)
    nul_output: str | None = parsed.nul_output
    print_root: bool = parsed.print_root

    tokens = parse_scan_args(arguments)

    if print_root:
        root_val = _extract_root(arguments)
        print(root_val if root_val is not None else ".")
        return 0

    if nul_output is not None:
        # Validate write target is within TMPDIR to prevent arbitrary write via ``--nul-output``.
        _nul_resolved = Path(nul_output).resolve()
        _tmpdir = Path(os.environ.get("TMPDIR") or tempfile.gettempdir()).resolve()
        try:
            _nul_resolved.relative_to(_tmpdir)
        except ValueError:
            print(f"! --nul-output path outside TMPDIR ({_tmpdir}): {nul_output}", file=sys.stderr)
            return 1
        with open(nul_output, "wb") as fh:
            fh.writelines(token.encode() + b"\x00" for token in tokens)
        return 0

    # Default: print shell-quoted tokens on stdout (legacy eval-safe form).
    quoted_tokens: list[str] = []
    it = iter(tokens)
    for token in it:
        if token == "--root":
            root_val = next(it, "")
            quoted_tokens.append(f"--root {shlex.quote(root_val)}")
        else:
            quoted_tokens.append(shlex.quote(token))
    print(" ".join(quoted_tokens))
    return 0


if __name__ == "__main__":
    sys.exit(main())
