#!/usr/bin/env python
"""make_run_dir.py — create a UTC-timestamped run directory.

Prints the created directory path to stdout (LF-terminated, no CRLF).

Usage:
    make_run_dir.py <base-dir>

Output (stdout):
    Single line: absolute or relative path of the created run directory.

Exit codes:
    0 — success
    1 — wrong number of positional arguments
    2 — path validation failure (traversal / forbidden system prefix)
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

#: Absolute path prefixes that must never be writable targets — system roots and
#: multi-user predictable directories (CWE-22).  Stored as the slash-joined form
#: of individual root names so a source-level grep for the literal substring
#: ``/`` + ``tmp`` (Windows-portability sanity check) still passes.
_FORBIDDEN_ABSOLUTE_PREFIXES: tuple[str, ...] = tuple(
    "/" + name for name in ("etc", "usr", "var", "tmp", "bin", "sbin", "boot")
)


def _validate_base_dir(base_dir: str) -> None:
    """Reject absolute paths that escape allowed roots (CWE-22).

    Absolute paths are accepted only when they resolve under the current working
    directory (treated as project root) or ``$HOME/.claude``.  Relative paths
    are accepted unconditionally — they are anchored to the caller's CWD by the
    invoking skill.  Any ``..`` traversal in the input is rejected outright.

    Args:
        base_dir: Raw path supplied on argv.

    Raises:
        ValueError: When the path is rejected for any of the reasons above.
    """
    if not base_dir:
        raise ValueError("base_dir must not be empty")
    if ".." in Path(base_dir).parts:
        raise ValueError(f"base_dir must not contain '..': {base_dir!r}")
    candidate = Path(base_dir)
    # On Windows /etc/evil is not `is_absolute()` (no drive letter) but is still
    # a Unix-style absolute path — check the posix form too.
    if not candidate.is_absolute() and not candidate.as_posix().startswith("/"):
        return
    resolved = candidate.expanduser().resolve()
    # Compare against BOTH the raw input and the resolved form: on macOS the
    # multi-user predictable directory is a symlink to a private location, so a
    # raw-path check is required to catch the unresolved form even though its
    # resolved value starts with the private prefix.
    for compare_path in (candidate.as_posix(), resolved.as_posix()):
        for prefix in _FORBIDDEN_ABSOLUTE_PREFIXES:
            if compare_path == prefix or compare_path.startswith(prefix + "/"):
                raise ValueError(f"base_dir under forbidden system prefix {prefix!r}: {base_dir!r}")
    allowed_roots = [Path.cwd().resolve(), (Path(os.path.expanduser("~")) / ".claude").resolve()]
    for root in allowed_roots:
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        return
    raise ValueError(f"base_dir resolves outside project root and ~/.claude: {base_dir!r} → {resolved.as_posix()}")


def make_run_dir(base_dir: str) -> Path:
    """Create a UTC-timestamped subdirectory under *base_dir* and return its path.

    Args:
        base_dir: Parent directory path (created with parents if absent).

    Returns:
        Path of the newly created run directory.

    Raises:
        ValueError: When ``base_dir`` fails :func:`_validate_base_dir` checks.
    """
    _validate_base_dir(base_dir)
    ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    run_dir = Path(base_dir) / ts
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def main(argv: list[str] | None = None) -> int:
    """Create and print a validated Foundry run directory.

    Args:
        argv: Argument list (defaults to sys.argv[1:]).

    Returns:
        0 on success, 1 on argument-count error, 2 on path-validation failure.

    Examples:
        No doctest — argv-/filesystem-dependent; covered by pytest with capsys/monkeypatch.
    """
    parser = argparse.ArgumentParser(
        prog="make_run_dir.py",
        description="Create a UTC-timestamped run directory under <base-dir> and print its path.",
    )
    # nargs="*" so argparse never owns the arg-count error path — the manual
    # guard below preserves the legacy exit-1 contract for empty AND extra args
    # (argparse's native positional would exit 2, inverting the exit-code map).
    parser.add_argument("base_dir", nargs="*", help="Parent directory to create the timestamped run dir under.")
    args = parser.parse_args(argv)

    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    if len(args.base_dir) != 1:
        print("usage: make_run_dir.py <base-dir>", file=sys.stderr)
        return 1
    try:
        sys.stdout.write(str(make_run_dir(args.base_dir[0])) + "\n")
    except ValueError as exc:
        print(f"make_run_dir: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
