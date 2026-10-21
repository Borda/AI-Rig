#!/usr/bin/env python
"""derive_fork_remote.py — make sure the contributor's fork remote exists, then report the pending push scope.

Adds the fork remote when it is missing, mirroring the transport of ``origin``: an SSH origin gets an SSH fork URL and
an HTTPS origin gets an HTTPS one. Hardcoding either form breaks the push silently on the other — an SSH-only checkout
has no HTTPS credentials to fall back on.

``oss:resolve`` Step 10 prints this summary before pushing on the authorization recorded at Step 3d (or quotes it in
the lost-answer recovery question). A failed scope computation exits 1 so Step 10 records ``not-attempted`` and never
pushes a scope it cannot describe.

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/derive_fork_remote.py" --fork-remote "$FORK_REMOTE" --head-ref "$HEAD_REF" \
        --base-ref "$BASE_REF" --fork-repo "$FORK_REPO"

``FORK_REPO`` comes from the ``resolve-head-repo-name`` sentinel ``resolve_pr_refs.py`` writes (the PR's
``headRepository.name``); empty keeps origin's repository name.

Exit codes:
    0 — push scope computed and printed
    1 — the fork remote or head ref is unresolved, the push scope could not be computed, one of
        --fork-remote/--head-ref/--base-ref starts with '-' (argv-injection guard), or the remote or a ref
        is not a valid git name (e.g. ``a..b``, ``@{-1}``, whitespace)
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from typing import Final

#: Prefix identifying an SSH-style origin URL, so the derived fork remote URL uses the same transport.
_SSH_ORIGIN_PREFIX: Final = "git@"
#: Fork remote name, which doubles as the fork owner in the derived URL: a GitHub login shape, never leading ``-``.
_REMOTE_NAME_RE: Final = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*")
#: GitHub repository name shape for ``--fork-repo``; it lands inside the fork URL, so no ``/``, whitespace or
#: leading ``-``. A leading ``.`` stays legal (``org/.github`` takes pull requests); bare ``.``/``..`` are refused in
#: ``main``.
_REPO_NAME_RE: Final = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9._-]*")
#: Characters ``git check-ref-format`` rejects anywhere in a ref name.
_INVALID_REF_CHARS_RE: Final = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")


def is_valid_ref_name(name: str) -> bool:
    """Apply the core ``git check-ref-format`` rules to a branch name without spawning git.

    Args:
        name: Branch name as it would follow ``refs/heads/``.

    Returns:
        ``True`` when git would accept the name and it cannot be read as an option or revision expression.

    Examples:
        >>> is_valid_ref_name("feature/x-1")
        True
        >>> [is_valid_ref_name(n) for n in ("a..b", "a b", "@{-1}", "x.lock", "a/", "-x", "a/.b", "")]
        [False, False, False, False, False, False, False, False]
    """
    if not name or name == "@" or _INVALID_REF_CHARS_RE.search(name):
        return False
    if ".." in name or "@{" in name or "//" in name or name.startswith(("/", "-")) or name.endswith(("/", ".")):
        return False
    return not any(part.startswith(".") or part.endswith(".lock") for part in name.split("/"))


def _git(args: list[str], timeout: int) -> tuple[int, str]:
    """Run a git command and return its exit code and stripped stdout.

    Args:
        args: Git arguments, without the leading ``git``.
        timeout: Maximum wait in seconds.

    Returns:
        ``(returncode, stdout)``; the code is 1 and stdout empty when git cannot be run at all.
    """
    try:
        proc = subprocess.run(["git", *args], capture_output=True, text=True, timeout=timeout, check=False)  # noqa: S603, S607 - argv list, no shell; git resolved via PATH on purpose
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


def ensure_fork_remote(fork_remote: str, timeout: int, fork_repo: str = "") -> None:
    """Add the fork remote when git does not already know it.

    Args:
        fork_remote: Remote name, which doubles as the fork owner.
        timeout: Maximum wait in seconds per git call.
        fork_repo: Repository name of the fork when it differs from ``origin``'s (a renamed fork); empty assumes the
            fork keeps the upstream name.
    """
    if _git(["remote", "get-url", fork_remote], timeout)[0] == 0:
        return
    _, origin_url = _git(["remote", "get-url", "origin"], timeout)
    url = fork_url(origin_url, fork_remote, fork_repo or repo_name_from_origin(origin_url))
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
    parser.add_argument(
        "--fork-repo",
        default="",
        help="Fork repository name when it differs from origin's (headRepository.name); default: origin's name.",
    )
    parser.add_argument("--timeout", type=int, default=10, help="Max subprocess wait in seconds (default: 10).")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]

    if not args.fork_remote or not args.head_ref:
        print("⛔ Step 10: FORK_REMOTE/HEAD_REF unresolved — refusing to push to an unresolved target")
        return 1

    for flag, value in (
        ("--fork-remote", args.fork_remote),
        ("--head-ref", args.head_ref),
        ("--base-ref", args.base_ref),
    ):
        if value.startswith("-"):
            print(
                f"⛔ Step 10: {flag} must not start with '-': {value!r} — refusing to pass it to git argv",
                file=sys.stderr,
            )
            return 1

    # a name git rejects or reads as a revision expression (`@{-1}`, `a..b`) must not reach the push scope
    if not _REMOTE_NAME_RE.fullmatch(args.fork_remote) or ".." in args.fork_remote:
        print(f"⛔ Step 10: --fork-remote is not a valid remote name: {args.fork_remote!r}", file=sys.stderr)
        return 1
    for flag, value in (("--head-ref", args.head_ref), ("--base-ref", args.base_ref)):
        if value and not is_valid_ref_name(value):
            print(f"⛔ Step 10: {flag} is not a valid branch name: {value!r}", file=sys.stderr)
            return 1
    if args.fork_repo and (not _REPO_NAME_RE.fullmatch(args.fork_repo) or args.fork_repo in {".", ".."}):
        print(f"⛔ Step 10: --fork-repo is not a valid repository name: {args.fork_repo!r}", file=sys.stderr)
        return 1

    ensure_fork_remote(args.fork_remote, args.timeout, args.fork_repo)
    _git(["branch", f"--set-upstream-to={args.fork_remote}/{args.head_ref}"], args.timeout)
    count, stat = push_scope(args.fork_remote, args.head_ref, args.base_ref, args.timeout)
    _, last_subject = _git(["log", "-1", "--format=%s"], args.timeout)

    if not count:
        print("⛔ Step 10: push scope could not be computed — refusing to push without a diff stat or commit count")
        return 1
    print(
        f'→ {count} commits ready to push to {args.fork_remote}/{args.head_ref} ({stat}); last commit: "{last_subject}"'
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
