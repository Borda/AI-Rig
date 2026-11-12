#!/usr/bin/env python
"""Resolve the repository CHANGELOG path used by release notes, prepare, and audit.

Purpose: One deterministic changelog lookup for every release mode, so notes, prepare, and audit never disagree on which
file is the changelog. Scope: Read-only filesystem search below one repository root. Usage: ``resolve_changelog_file.py
[--repo-root DIR] [--write-sentinel]``. Search order: ``CHANGELOG.md`` at the root, ``docs/CHANGELOG.md``, then the
alphabetically first ``CHANGELOG*`` file exactly one directory deep (skipping ``node_modules``, ``.venv``, ``vendor``
and ``.git``). Outputs: Prints the path relative to the repository root; when no changelog exists it prints
``CHANGELOG.md``, the file the release flow creates. With ``--write-sentinel`` it also writes that path to ``release-
changelog-file-<session>`` under the temp directory, where later release steps reload it. Failure: Returns 1 when the
repository root is not a directory. Used by: ``skills/release/SKILL.md`` (Audit changelog), ``modes/prepare.md``,
``modes/audit.md``.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

from _sentinel_io import write_private_text

#: Directory names never searched for a changelog (dependency and tooling trees).
_SKIP_DIRS: frozenset[str] = frozenset({"node_modules", ".venv", "vendor", ".git"})

#: Path printed when the repository has no changelog yet.
DEFAULT_CHANGELOG = "CHANGELOG.md"


def _regular_changelog(path: Path) -> bool:
    """Return whether a changelog file exists without following symlinks."""
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError(f"changelog path must not traverse a symlink: {path}")
    return path.is_file()


def resolve_changelog(repo_root: Path) -> str:
    """Return the changelog path relative to ``repo_root`` following the fixed search order.

    Args:
        repo_root: Repository root directory.

    Returns:
        POSIX-style relative path of the first match, or ``CHANGELOG.md`` when none exists.

    Raises:
        ValueError: A selected changelog or searched directory is a symlink.

    Examples:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as tmp:
        ...     resolve_changelog(Path(tmp))
        'CHANGELOG.md'
    """
    for candidate in (DEFAULT_CHANGELOG, "docs/CHANGELOG.md"):
        if _regular_changelog(repo_root / candidate):
            return candidate
    for directory in sorted(p for p in repo_root.iterdir() if p.name not in _SKIP_DIRS):
        if directory.is_symlink() or not directory.is_dir():
            continue
        matches = sorted(p for p in directory.iterdir() if p.name.startswith("CHANGELOG") and _regular_changelog(p))
        if matches:
            return matches[0].relative_to(repo_root).as_posix()
    return DEFAULT_CHANGELOG


def _sentinel_path() -> Path:
    """Return the session-scoped sentinel path later release steps reload the changelog path from."""
    session = os.environ.get("CSID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "shared"
    tmp_fallback = tempfile.gettempdir() if os.name == "nt" else "/tmp"  # noqa: S108 - shell default of ${TMPDIR:-/tmp}
    return Path(os.environ.get("TMPDIR") or tmp_fallback) / f"release-changelog-file-{session}"


def main(argv: list[str] | None = None) -> int:
    """Print the resolved changelog path; optionally persist it as a session sentinel.

    Args:
        argv: Command-line arguments; ``None`` reads ``sys.argv``.

    Returns:
        ``0`` on success, ``1`` when the repository root is not a directory or the search encounters a symlink.
    """
    parser = argparse.ArgumentParser(description="Resolve the repository CHANGELOG path (relative to the repo root).")
    parser.add_argument("--repo-root", default=".", help="repository root to search (default: current directory)")
    parser.add_argument("--write-sentinel", action="store_true", help="also write the path to the session sentinel")
    args = parser.parse_args(argv)
    root = Path(args.repo_root)
    if not root.is_dir():
        print(f"Error: repo root not a directory: {root}", file=sys.stderr)
        return 1
    try:
        resolved = resolve_changelog(root)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if args.write_sentinel:
        write_private_text(_sentinel_path(), resolved + "\n")
    print(resolved)
    return 0


if __name__ == "__main__":
    sys.exit(main())
