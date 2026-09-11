#!/usr/bin/env python3
"""compute_commit_sentinel.py — print the commit-authorization sentinel path for the current repo/branch.

Usage:
    SENTINEL=$(python "${CLAUDE_PLUGIN_ROOT}/bin/compute_commit_sentinel.py")
    touch "$SENTINEL"  # timeout: 3000
    trap 'rm -f "$SENTINEL"' EXIT INT TERM

Sentinel path format: <temp-dir>/claude-commit-auth-<repo-slug>-<branch-slug>

Slug algorithm: lowercase, runs of non-alphanumeric chars → single '-',
trailing '-' stripped — mirrors the ``tr``/``sed`` pipeline documented in
``git-commit.md``.  Extracted from the oss:resolve action-item-dispatch
setup block to enable reuse across resolve steps.

Note: sentinel path is predictable by design for cross-process coordination
with the pre-commit hook (Gate 1, see git-commit.md). On multi-user hosts,
use $XDG_RUNTIME_DIR instead of /tmp for improved isolation, as noted in
the security audit. TOCTOU risk on single-user workstations
accepted as low-severity.

Exit codes:
    0  on success
    1  if not inside a git repository (git commands fail)
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath


def to_slug(value: str) -> str:
    """Convert a string to a filesystem-safe slug.

    Lowercases, collapses runs of non-alphanumeric characters to a single
    ``-``, and strips any trailing ``-``.  Mirrors the shell pipeline::

        tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-' | tr -s '-' | sed 's/-$//'

    Args:
        value: Input string (repo name or branch name).

    Returns:
        Slug string containing only ``[a-z0-9-]`` with no trailing ``-``.

    Examples:
        >>> to_slug("MyRepo.local")
        'myrepo-local'
        >>> to_slug("feature/my-branch")
        'feature-my-branch'
        >>> to_slug("UPPER-CASE--extra-")
        'upper-case-extra'
        >>> to_slug("main")
        'main'
        >>> to_slug("")
        ''
    """
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = value.rstrip("-")
    return value


def get_sentinel_path() -> str:
    """Compute the commit-authorization sentinel file path for the current git repo and branch.

    Runs ``git rev-parse --show-toplevel`` and ``git branch --show-current``
    to derive repo name and branch, slugifies both, and returns the sentinel path.

    Args:
        None

    Returns:
        Absolute sentinel path string, e.g. ``<tmpdir>/claude-commit-auth-myrepo-main``.

    Raises:
        subprocess.CalledProcessError: if not inside a git repository.

    Examples:
        No doctest — requires live git subprocess; covered by pytest with monkeypatch.
    """
    repo_root = subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip()
    repo_name = repo_root.rsplit("/", 1)[-1]

    branch = subprocess.check_output(["git", "branch", "--show-current"], text=True).strip()

    # Prefer a per-user temp dir over a world-readable default, but only when the value is
    # absolute for this host. Windows CI inherits a POSIX-style TMPDIR that has no native
    # directory, and a drive-less path would resolve against whatever drive the process
    # happens to run on; the system temp dir is the interoperable fallback there.
    native = PureWindowsPath if sys.platform == "win32" else PurePosixPath
    candidates = (os.environ.get("TMPDIR"), os.environ.get("XDG_RUNTIME_DIR"))
    base = Path(next((c for c in candidates if c and native(c).is_absolute()), tempfile.gettempdir()))
    return str(base / f"claude-commit-auth-{to_slug(repo_name)}-{to_slug(branch)}")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Argument list override for testing. Defaults to sys.argv[1:].

    Returns:
        Exit code: 0 on success, 1 on git error.

    Examples:
        No doctest — requires live git; covered by pytest.
    """
    _ = sys.argv[1:] if argv is None else argv  # no positional args used
    try:
        print(get_sentinel_path())
        return 0
    except subprocess.CalledProcessError as e:
        print(f"git error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
