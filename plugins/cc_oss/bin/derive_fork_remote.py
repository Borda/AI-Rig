#!/usr/bin/env python
"""derive_fork_remote.py — make sure the contributor's fork remote exists, then report the pending push scope.

Adds the fork remote when it is missing, mirroring the transport of ``origin``: an SSH origin gets an SSH fork URL and
an HTTPS origin gets an HTTPS one. Hardcoding either form breaks the push silently on the other — an SSH-only checkout
has no HTTPS credentials to fall back on.

The printed summary feeds the push-authorization question in ``oss:resolve`` Step 10, so the run stops rather than
present an authorization prompt whose scope could not be computed.

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/derive_fork_remote.py" --fork-remote "$FORK_REMOTE" --head-ref "$HEAD_REF" \
        --base-ref "$BASE_REF"

Exit codes:
    0 — push scope computed and printed
    1 — the fork remote or head ref is unresolved, or the push scope could not be computed
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from typing import Final

_SSH_ORIGIN_PREFIX: Final = "git@"


def _git(args: list[str], timeout: int) -> tuple[int, str]:
    """Run a git command and return its exit code and stripped stdout.

    Args:
        args: Git arguments, without the leading ``git``.
        timeout: Maximum wait in seconds.

    Returns:
        ``(returncode, stdout)``; the code is 1 and stdout empty when git cannot be run at all.
    """
    try:
        proc = subprocess.run(["git", *args], capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return proc.returncode, proc.stdout.strip()


def repo_name_from_origin(origin_url: str) -> str:
    """Extract the bare repository name from an origin URL.

    Args:
        origin_url: SSH or HTTPS remote URL.

    Returns:
        Repository name without the ``.git`` suffix.

    Examples:
        >>> repo_name_from_origin("git@github.com:owner/repo.git")
        'repo'
        >>> repo_name_from_origin("https://github.com/owner/repo")
        'repo'
    """
    return re.sub(r"\.git$", "", origin_url.rsplit("/", 1)[-1])


def fork_url(origin_url: str, fork_remote: str, repo_name: str) -> str:
    """Build the fork URL using the same transport as ``origin``.

    Args:
        origin_url: The origin remote URL, used only to pick SSH vs HTTPS.
        fork_remote: Remote name, which doubles as the fork owner.
        repo_name: Bare repository name.

    Returns:
        Fork URL in the matching transport.

    Examples:
        >>> fork_url("git@github.com:upstream/repo.git", "contributor", "repo")
        'git@github.com:contributor/repo.git'
        >>> fork_url("https://github.com/upstream/repo.git", "contributor", "repo")
        'https://github.com/contributor/repo.git'
    """
    if origin_url.startswith(_SSH_ORIGIN_PREFIX):
        return f"{_SSH_ORIGIN_PREFIX}github.com:{fork_remote}/{repo_name}.git"
    return f"https://github.com/{fork_remote}/{repo_name}.git"


def ensure_fork_remote(fork_remote: str, timeout: int) -> None:
    """Add the fork remote when git does not already know it.

    Args:
        fork_remote: Remote name, which doubles as the fork owner.
        timeout: Maximum wait in seconds per git call.
    """
    if _git(["remote", "get-url", fork_remote], timeout)[0] == 0:
        return
    _, origin_url = _git(["remote", "get-url", "origin"], timeout)
    url = fork_url(origin_url, fork_remote, repo_name_from_origin(origin_url))
    _git(["remote", "add", fork_remote, url], timeout)
    print(f"→ Added remote {fork_remote} → {url}")


def push_scope(fork_remote: str, head_ref: str, base_ref: str, timeout: int) -> tuple[str, str]:
    """Compute the commit count and diff stat of everything not yet on the fork branch.

    Falls back to the base branch when the fork branch does not exist yet — the first push of a new branch has nothing
    to compare against on the remote.

    Args:
        fork_remote: Remote name.
        head_ref: Branch being pushed.
        base_ref: Base branch on ``origin``, used as the fallback range.
        timeout: Maximum wait in seconds per git call.

    Returns:
        ``(count, stat)`` — the stat is the final line of ``git diff --stat``; either may be empty on failure.
    """
    primary = f"{fork_remote}/{head_ref}..HEAD"
    fallback = f"origin/{base_ref}..HEAD"
    code, count = _git(["rev-list", primary, "--count"], timeout)
    if code != 0:
        _, count = _git(["rev-list", fallback, "--count"], timeout)
    code, diff = _git(["diff", primary, "--stat"], timeout)
    if code != 0:
        _, diff = _git(["diff", fallback, "--stat"], timeout)
    # `git diff --stat` indents its summary line; strip it so the printed scope
    # reads "(3 files changed, ...)" rather than "( 3 files changed, ...)".
    stat = diff.splitlines()[-1].strip() if diff.strip() else ""
    return count, stat


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Argument list override for testing. Defaults to ``sys.argv[1:]``.

    Returns:
        Exit code — 0 when the push scope was printed, 1 on an unresolved ref or uncomputable scope.
    """
    parser = argparse.ArgumentParser(
        prog="derive_fork_remote.py",
        description="Ensure the fork remote exists and report the pending push scope.",
    )
    parser.add_argument("--fork-remote", default="", help="Fork remote name (also the fork owner).")
    parser.add_argument("--head-ref", default="", help="Branch being pushed.")
    parser.add_argument("--base-ref", default="", help="Base branch on origin, used as the fallback range.")
    parser.add_argument("--timeout", type=int, default=10, help="Max subprocess wait in seconds (default: 10).")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]

    if not args.fork_remote or not args.head_ref:
        print("⛔ Step 10: FORK_REMOTE/HEAD_REF unresolved — refusing to present an empty push-authorization prompt")
        return 1

    ensure_fork_remote(args.fork_remote, args.timeout)
    _git(["branch", f"--set-upstream-to={args.fork_remote}/{args.head_ref}"], args.timeout)
    count, stat = push_scope(args.fork_remote, args.head_ref, args.base_ref, args.timeout)
    _, last_subject = _git(["log", "-1", "--format=%s"], args.timeout)

    if not count:
        print(
            "⛔ Step 10: push scope could not be computed — refusing to present an authorization prompt with no diff "
            "stat or commit count"
        )
        return 1
    print(
        f'→ {count} commits ready to push to {args.fork_remote}/{args.head_ref} ({stat}); last commit: "{last_subject}"'
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
