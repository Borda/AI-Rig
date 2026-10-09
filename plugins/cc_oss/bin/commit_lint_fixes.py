#!/usr/bin/env python
"""commit_lint_fixes.py — stage all changed tracked files and commit with lint-fix message.

No-ops when there are no changed files to stage. Extracted from oss:resolve
lint-qa-gate Step 9 auto-fix commit block.

Usage:
    commit_lint_fixes.py
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from shutil import which

#: Fixed commit message, including the co-author trailer, for the lint auto-fix commit.
_COMMIT_MESSAGE = "lint: auto-fix violations after resolve cycle\n\n---\nCo-authored-by: claude[bot] <209825114+claude[bot]@users.noreply.github.com>"


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


def main(argv: list[str] | None = None) -> int:
    """Entry point — mirrors ``commit_lint_fixes.sh`` behaviour.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``); only ``-h/--help``
            is honoured, otherwise the script takes no positional arguments.

    Returns:
        Exit code: 0 if no changes or commit succeeds; git exit code otherwise.

    Examples:
        No doctest — requires subprocess; covered by pytest with monkeypatch.
    """
    # Honour only ``-h/--help`` via argparse; the script otherwise takes no arguments and
    # ignores argv entirely (legacy zero-arg contract — the sole call site passes none).
    # A broad parse_args would reject any stray token with exit 2; keep argv ignored.
    if list(sys.argv[1:] if argv is None else argv) in (["-h"], ["--help"]):
        argparse.ArgumentParser(
            prog="commit_lint_fixes.py",
            description="Stage all changed tracked files and commit with a lint-fix message.",
        ).parse_args(["-h"])

    sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]
    git = _resolve("git")
    changed_proc = subprocess.run(  # noqa: S603
        [git, "diff", "HEAD", "--name-only"],
        capture_output=True,
        text=True,
        check=False,
        timeout=3,
    )
    changed = [f for f in changed_proc.stdout.splitlines() if f.strip()]
    if not changed:
        print("[lint] no changed files to commit")
        return 0
    subprocess.run([git, "add", "--", *changed], check=True, timeout=3)  # noqa: S603
    # No timeout on the commit: it triggers user-controlled pre-commit hooks whose
    # duration is unbounded by design (ruff, mypy, custom checks routinely exceed a
    # few seconds). A short timeout would kill git mid-hook, leaving a partial commit.
    # Matches commit_action_item.py; short timeouts stay only on cheap plumbing calls.
    result = subprocess.run(  # noqa: S603
        [git, "commit", "-m", _COMMIT_MESSAGE],
        check=False,
    )
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
