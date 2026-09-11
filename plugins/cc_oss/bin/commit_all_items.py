#!/usr/bin/env python
"""commit_all_items.py — create a bulk commit for all resolved review items.

Builds a commit message summarising counts of as-suggested, self-resolved, and
rejected items, then runs ``git commit -m <message>``. Extracted from
oss:resolve action-item-dispatch COMMIT_MODE=all block.

Usage:
    commit_all_items.py PR_NUMBER N_AS_SUGGESTED N_SELF_RESOLVED N_REJECTED \\
                        [SUMMARIES_FILE] [--codex]

Args:
    PR_NUMBER:      Pull request number.
    N_AS_SUGGESTED: Count of items applied as-suggested.
    N_SELF_RESOLVED: Count of items self-resolved (suggestion rejected).
    N_REJECTED:     Count of items whose evidence was rejected (skipped).
    SUMMARIES_FILE: Optional path to file with bullet-list item summaries.
    --codex:        Include OpenAI Codex co-author trailer (pass anywhere).
"""

from __future__ import annotations

import argparse
import atexit
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from shutil import which

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(text: str) -> str:
    """Slugify text to ``[a-z0-9-]`` with no trailing hyphen.

    Args:
        text: Input string.

    Returns:
        Slugified string.

    Examples:
        >>> _slug("My/Repo Name")
        'my-repo-name'
        >>> _slug("main")
        'main'
    """
    return _SLUG_RE.sub("-", text.lower()).rstrip("-")


def _sentinel_path(git: str) -> Path:
    """Return the commit-auth sentinel path for the current repo+branch.

    Mirrors the logic in ``commit_action_item.py`` and ``compute_commit_sentinel.py``.

    Args:
        git: Absolute path to the ``git`` executable.

    Returns:
        Path to sentinel file (may not yet exist).

    Examples:
        No doctest — requires live git; covered by pytest with monkeypatch.
    """
    root = subprocess.run(
        [git, "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=False
    ).stdout.strip()
    branch = subprocess.run(
        [git, "branch", "--show-current"], capture_output=True, text=True, check=False
    ).stdout.strip()
    # Prefer a per-user temp dir over a world-readable default, but only when the value is
    # absolute for this host. Windows CI inherits a POSIX-style TMPDIR that has no native
    # directory, and a drive-less path would resolve against whatever drive the process
    # happens to run on; the system temp dir is the interoperable fallback there.
    native = PureWindowsPath if sys.platform == "win32" else PurePosixPath
    candidates = (os.environ.get("TMPDIR"), os.environ.get("XDG_RUNTIME_DIR"))
    base = Path(next((c for c in candidates if c and native(c).is_absolute()), tempfile.gettempdir()))
    return base / f"claude-commit-auth-{_slug(Path(root).name)}-{_slug(branch)}"


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


def build_commit_message(
    pr_number: str,
    n_as_suggested: int,
    n_self_resolved: int,
    n_rejected: int,
    bullet_list: str,
    include_codex: bool,
) -> str:
    """Build the commit message string.

    Args:
        pr_number: Pre-formatted PR reference to embed verbatim — ``#<N>`` when the
            commit lands in the same repo as the PR, or the full PR URL when it lands
            in a different repo (e.g. pushed to a contributor's fork). Caller resolves
            which form applies (see ``PR_REF`` in ``resolve/SKILL.md`` Step 4).
        n_as_suggested: Count of items applied as-suggested.
        n_self_resolved: Count of items self-resolved.
        n_rejected: Count of items rejected.
        bullet_list: Optional bullet-list body (may be empty string).
        include_codex: Whether to add OpenAI Codex co-author trailer.

    Returns:
        Full commit message string.

    Examples:
        >>> msg = build_commit_message("#42", 3, 1, 0, "", False)
        >>> "PR #42" in msg
        True
        >>> "3 as-suggested" in msg
        True
        >>> "Co-authored-by: claude[bot]" in msg
        True
    """
    codex_trailer = "\nCo-authored-by: OpenAI Codex <codex@openai.com>" if include_codex else ""
    body_section = f"\n{bullet_list}\n" if bullet_list.strip() else "\n"
    return (
        f"Resolve review items for PR {pr_number}\n"
        f"{body_section}"
        f"Challenge log: {n_as_suggested} as-suggested, "
        f"{n_self_resolved} self-resolved, {n_rejected} rejected\n"
        f"\n---\n"
        f"Co-authored-by: claude[bot] <209825114+claude[bot]@users.noreply.github.com>"
        f"{codex_trailer}"
    )


def main(argv: list[str] | None = None) -> int:
    """Entry point — mirrors ``commit_all_items.sh`` behaviour.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Exit code: 1 on missing PR number; 2 on non-integer count; git exit code otherwise.

    Examples:
        No doctest — requires subprocess; covered by pytest with monkeypatch.
    """
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]
    args = list(sys.argv[1:] if argv is None else argv)

    # Honour only ``-h/--help`` via argparse; every other token flows through the manual
    # positional loop below, which silently ignores extra positionals and accepts
    # ``--codex`` anywhere (legacy "ignore" contract). A broad parse_args would reject
    # extras with exit 2 and reorder positional handling — keep the manual parser.
    if args in (["-h"], ["--help"]):
        argparse.ArgumentParser(
            prog="commit_all_items.py",
            description="Create a bulk commit for all resolved review items.",
        ).parse_args(["-h"])

    pr_number = ""
    raw_counts: list[str] = ["0", "0", "0"]
    summaries_file = ""
    include_codex = False
    pos = 0
    for arg in args:
        if arg == "--codex":
            include_codex = True
            continue
        if pos == 0:
            pr_number = arg
        elif pos == 1:
            raw_counts[0] = arg
        elif pos == 2:
            raw_counts[1] = arg
        elif pos == 3:
            raw_counts[2] = arg
        elif pos == 4:
            summaries_file = arg
        pos += 1
    if not pr_number:
        print(
            "Usage: commit_all_items.py PR_NUMBER N_AS N_SELF N_REJECTED [SUMMARIES_FILE] [--codex]",
            file=sys.stderr,
        )
        return 1
    int_counts: list[int] = []
    for val in raw_counts:
        if not str(val).isdigit():
            print(f"commit_all_items: expected integer, got: {val!r}", file=sys.stderr)
            return 2
        int_counts.append(int(val))
    bullet_list = ""
    if summaries_file and Path(summaries_file).is_file():
        bullet_list = Path(summaries_file).read_text(encoding="utf-8")
    msg = build_commit_message(pr_number, *int_counts, bullet_list, include_codex)
    git = _resolve("git")
    sentinel = _sentinel_path(git)
    sentinel.touch()
    atexit.register(lambda: sentinel.unlink(missing_ok=True))
    cached = subprocess.run([git, "diff", "--cached", "--quiet"], check=False)  # noqa: S603
    if cached.returncode == 0:
        print("commit_all_items: staging area empty — no commit created", file=sys.stderr)
        return 0
    # No timeout on the commit: it triggers user-controlled pre-commit hooks whose
    # duration is unbounded by design (ruff, mypy, custom checks routinely exceed a
    # few seconds). A short timeout would kill git mid-hook, leaving a partial commit.
    # Matches commit_action_item.py; short timeouts stay only on cheap plumbing calls.
    result = subprocess.run(  # noqa: S603
        [git, "commit", "-m", msg],
        check=False,
    )
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
