#!/usr/bin/env python
"""check_base_fresh.py — report whether the PR branch still contains the newest target-branch commit.

``oss:resolve`` merges the target branch into the PR branch once, at Step 5, and the run can last an hour or more
before it pushes. The target branch keeps moving in that window: a commit landing three minutes after the merge leaves
the pushed branch behind, and GitHub then reports the PR as conflicting. This helper re-fetches the target and checks
ancestry so Step 9 can re-merge before QA and Step 10 can surface drift before the push.

A failed fetch does not stop the check: the last-known ``<remote>/<base>`` is still compared, because a ref another
tool refreshed meanwhile can already show the drift without network access.

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/check_base_fresh.py" --base-ref "$BASE_REF" [--remote origin] [--no-fetch]

Output (one ``KEY=value`` per line, then up to ten ``HEAD..<remote>/<base>`` subjects prefixed ``  + ``):
    BASE_FETCH=ok|failed|skipped
    BASE_FRESH=yes|no|unknown
    BASE_TIP=<sha>          (omitted when unknown)
    BASE_BEHIND=<count>     (omitted when unknown)

Exit codes:
    0 — freshness decided (``BASE_FRESH=yes`` or ``no``)
    1 — freshness unknowable: invalid or dash-leading ref, no ``<remote>/<base>`` ref, or not inside a git repository
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from typing import Final

_MAX_SUBJECTS: Final = 10


@dataclass(frozen=True)
class Freshness:
    """Outcome of comparing ``HEAD`` against the target branch tip.

    Attributes:
        fetch: ``ok``, ``failed`` or ``skipped`` — how the remote-tracking ref was obtained.
        fresh: ``yes``, ``no`` or ``unknown``.
        tip: Target tip SHA, empty when unknown.
        behind: Number of target commits missing from ``HEAD``; ``-1`` when unknown.
        subjects: One-line subjects of the missing commits, newest first, capped at ten.
    """

    fetch: str
    fresh: str
    tip: str = ""
    behind: int = -1
    subjects: tuple[str, ...] = ()

    def render(self) -> str:
        """Format the outcome as the ``KEY=value`` lines the skill parses.

        Returns:
            Newline-joined report, without a trailing newline.

        Examples:
            >>> print(Freshness(fetch="ok", fresh="no", tip="abc", behind=1, subjects=("abc fix",)).render())
            BASE_FETCH=ok
            BASE_FRESH=no
            BASE_TIP=abc
            BASE_BEHIND=1
              + abc fix
            >>> print(Freshness(fetch="failed", fresh="unknown").render())
            BASE_FETCH=failed
            BASE_FRESH=unknown
        """
        lines = [f"BASE_FETCH={self.fetch}", f"BASE_FRESH={self.fresh}"]
        if self.fresh != "unknown":
            lines += [f"BASE_TIP={self.tip}", f"BASE_BEHIND={self.behind}"]
            lines += [f"  + {subject}" for subject in self.subjects]
        return "\n".join(lines)


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


def check(base_ref: str, remote: str, fetch: bool, timeout: int) -> Freshness:
    """Fetch the target branch (optionally) and compare it with ``HEAD``.

    Args:
        base_ref: Target branch name on ``remote``.
        remote: Remote that hosts the target branch.
        fetch: Whether to refresh ``<remote>/<base_ref>`` before comparing.
        timeout: Maximum wait in seconds per git call.

    Returns:
        The comparison outcome; ``fresh`` is ``unknown`` when the remote-tracking ref cannot be resolved.

    Examples:
        >>> check("main", "origin", fetch=False, timeout=5).fetch
        'skipped'
    """
    fetched = "skipped"
    if fetch:
        fetched = "ok" if _git(["fetch", "--no-tags", remote, base_ref], timeout)[0] == 0 else "failed"
    target = f"refs/remotes/{remote}/{base_ref}"
    code, tip = _git(["rev-parse", "--verify", "--quiet", f"{target}^{{commit}}"], timeout)
    if code != 0 or not tip:
        return Freshness(fetch=fetched, fresh="unknown")
    if _git(["merge-base", "--is-ancestor", tip, "HEAD"], timeout)[0] == 0:
        return Freshness(fetch=fetched, fresh="yes", tip=tip, behind=0)
    _, count = _git(["rev-list", "--count", f"HEAD..{tip}"], timeout)
    _, log = _git(["log", "--oneline", "--no-decorate", f"-{_MAX_SUBJECTS}", f"HEAD..{tip}"], timeout)
    behind = int(count) if count.isdigit() else -1
    return Freshness(fetch=fetched, fresh="no", tip=tip, behind=behind, subjects=tuple(log.splitlines()))


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Argument list override for testing. Defaults to ``sys.argv[1:]``.

    Returns:
        Exit code — 0 when freshness was decided, 1 when it is unknowable.
    """
    parser = argparse.ArgumentParser(
        prog="check_base_fresh.py",
        description="Report whether HEAD contains the newest commit of the target branch.",
    )
    parser.add_argument("--base-ref", required=True, help="Target branch name on the remote.")
    parser.add_argument("--remote", default="origin", help="Remote hosting the target branch (default: origin).")
    parser.add_argument("--no-fetch", action="store_true", help="Compare the last-known remote-tracking ref only.")
    parser.add_argument("--timeout", type=int, default=30, help="Max wait per git call in seconds (default: 30).")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]

    for flag, value in (("--base-ref", args.base_ref), ("--remote", args.remote)):
        if not value or value.startswith("-"):
            print(f"⛔ {flag} empty or starts with '-': {value!r} — refusing to pass it to git argv", file=sys.stderr)
            print("BASE_FRESH=unknown")
            return 1
    if _git(["check-ref-format", "--branch", args.base_ref], args.timeout)[0] != 0:
        print(f"⛔ --base-ref {args.base_ref!r} is not a valid branch name", file=sys.stderr)
        print("BASE_FRESH=unknown")
        return 1

    result = check(args.base_ref, args.remote, fetch=not args.no_fetch, timeout=args.timeout)
    print(result.render())
    return 0 if result.fresh != "unknown" else 1


if __name__ == "__main__":
    sys.exit(main())
