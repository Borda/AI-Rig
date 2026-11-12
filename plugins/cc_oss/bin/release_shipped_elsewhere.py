#!/usr/bin/env python3
"""Purpose: Find commits of a release range that an off-branch release tag already shipped.

Usage:
    release_shipped_elsewhere.py --range <base>..<head> --tags "<tag> [<tag> ...]" --out <file> [--repo <dir>]

``--tags`` lists stable release tags that are not ancestors of ``<head>`` (written by ``release_setup.py`` as
``SHIPPED_ELSEWHERE_TAGS``), for example 1.11.1 and 1.11.2 cut from a release branch while ``<head>`` is the
development branch whose last in-branch tag is 1.11.0. Their backported work is reachable from ``<base>..<head>`` but
is not new to users.

Evidence, per range commit (merge commits excluded):

* ``shipped`` - ``git cherry <tag> <head> <base>`` marks the commit's patch as present in ``<tag>`` and no revert of it
  appears on the release side. The skill drops these from the release content.
* ``review`` - only weaker evidence: same ``(#N)`` pull request number on the release side, or a patch-equivalent
  commit that the release side also reverted. The skill keeps these and verifies them against the released tree.

Output:
    ``--out`` receives one ``<sha>`` TAB ``<category>`` TAB ``<tag>`` TAB ``<evidence>`` line per classified commit.
    stdout gets one summary line ``candidates=<n> shipped=<n> review=<n>``.

Exit codes:
    0 - classification written (possibly empty)
    1 - invalid arguments
    2 - a git command failed; never treated as "nothing shipped elsewhere"
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from shutil import which

#: Seconds allowed per git call; ``git cherry`` over a long range is the slowest.
_GIT_TIMEOUT = 60
_PR_NUMBER = re.compile(r"\(#(\d+)\)\s*$")
_REVERT_SUBJECT = re.compile(r'^revert\W+"?(?P<subject>.+?)"?$', re.IGNORECASE)


class GitError(RuntimeError):
    """A git command exited non-zero or timed out."""


def _git(repo: str, *args: str) -> str:
    """Run git in ``repo`` and return stdout; raise ``GitError`` on failure.

    Args:
        repo: Working directory for git (``-C``).
        *args: Git subcommand and arguments.

    Returns:
        Raw stdout.

    Raises:
        GitError: On non-zero exit, timeout, or missing git binary.
    """
    git = which("git")
    if git is None:
        raise GitError("git executable not found on PATH")
    try:
        proc = subprocess.run(  # noqa: S603
            [git, "-C", repo, *args], capture_output=True, text=True, check=False, timeout=_GIT_TIMEOUT
        )
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {args[0]} timed out after {_GIT_TIMEOUT} s") from exc
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def _subjects(repo: str, revs: list[str]) -> dict[str, str]:
    """Map full sha to subject for ``git log --no-merges <revs>``."""
    out = _git(repo, "log", "--no-merges", "--format=%H%x09%s", *revs)
    return {sha: subject for sha, _, subject in (line.partition("\t") for line in out.splitlines()) if sha}


def _pr_numbers(subjects: dict[str, str]) -> dict[str, str]:
    """Map PR number to sha for subjects that end in ``(#N)``."""
    found: dict[str, str] = {}
    for sha, subject in subjects.items():
        match = _PR_NUMBER.search(subject)
        if match:
            found.setdefault(match.group(1), sha)
    return found


def _reverted_subjects(released: dict[str, str]) -> set[str]:
    """Return the subjects that the release side reverted (``Revert "<subject>"``)."""
    reverted: set[str] = set()
    for subject in released.values():
        match = _REVERT_SUBJECT.match(subject.strip())
        if match:
            reverted.add(match.group("subject").strip())
    return reverted


def classify(repo: str, range_spec: str, tags: list[str]) -> list[tuple[str, str, str, str]]:
    """Classify range commits against each off-branch release tag.

    Args:
        repo: Repository directory.
        range_spec: ``<base>..<head>`` release range.
        tags: Release tags that are not ancestors of ``<head>``.

    Returns:
        ``(sha, category, tag, evidence)`` rows. A commit keeps its strongest category across tags, so ``shipped``
        beats ``review``.

    Raises:
        GitError: When any git call fails.
    """
    base, _, head = range_spec.partition("..")
    candidates = _subjects(repo, [range_spec])
    candidate_prs = _pr_numbers(candidates)
    rows: dict[str, tuple[str, str, str, str]] = {}
    for tag in tags:
        merge_base = _git(repo, "merge-base", head, tag).strip()
        released = _subjects(repo, [f"{merge_base}..{tag}"])
        reverted = _reverted_subjects(released)
        equivalent = {
            line[2:].strip() for line in _git(repo, "cherry", tag, head, base).splitlines() if line.startswith("- ")
        }
        for sha in equivalent & candidates.keys():
            if candidates[sha] in reverted:
                rows.setdefault(sha, (sha, "review", tag, "patch-equivalent in tag but reverted on release side"))
            else:
                rows[sha] = (sha, "shipped", tag, "patch-equivalent in tag (git cherry)")
        for number, released_sha in _pr_numbers(released).items():
            sha = candidate_prs.get(number)
            if sha is not None and sha not in rows:
                rows[sha] = (sha, "review", tag, f"same PR #{number} as {released_sha[:12]} in tag")
    return list(rows.values())


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser."""
    parser = argparse.ArgumentParser(description="Find release-range commits already shipped by off-branch tags.")
    parser.add_argument("--range", required=True, dest="range_spec", help="<base>..<head> release range")
    parser.add_argument("--tags", required=True, help="space-separated release tags not reachable from head")
    parser.add_argument("--out", required=True, help="file receiving the TSV classification")
    parser.add_argument("--repo", default=".", help="repository directory (default: cwd)")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Classify the range and write the TSV; return the process exit code.

    Args:
        argv: Command-line arguments; ``None`` reads ``sys.argv``.

    Returns:
        ``0`` on success, ``1`` for invalid arguments, ``2`` when git fails.
    """
    args = build_parser().parse_args(argv)
    tags = args.tags.split()
    parts = args.range_spec.split("..")
    if len(parts) != 2 or not all(parts) or "..." in args.range_spec or any(p.startswith("-") for p in (*parts, *tags)):
        print(f"release_shipped_elsewhere: invalid range or tag {args.range_spec!r} {tags!r}", file=sys.stderr)
        return 1
    try:
        total = len(_subjects(args.repo, [args.range_spec]))
        rows = classify(args.repo, args.range_spec, tags) if tags else []
    except GitError as exc:
        print(f"release_shipped_elsewhere: {exc}", file=sys.stderr)
        return 2
    Path(args.out).write_text("".join("\t".join(row) + "\n" for row in rows), encoding="utf-8", newline="\n")
    shipped = sum(1 for row in rows if row[1] == "shipped")
    print(f"candidates={total} shipped={shipped} review={len(rows) - shipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
