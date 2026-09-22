#!/usr/bin/env python
"""release_setup.py — shared setup block for /oss:release modes.

Resolves skill directory (installed cache → source tree fallback), repo
root, branch slug, current UTC date, and branch-aware last-stable-tag
baseline. Writes each resolved value to its own file under
``${TMPDIR:-/tmp}/release-setup-${CSID}/`` for safe cross-block
consumption. Informational notes go to stderr only.

Output files (written to ``${TMPDIR:-/tmp}/release-setup-${CSID}/``):
    SKILL_DIR, REPO_ROOT, BRANCH, DATE, LAST_TAG,
    CHERRY_PICK_SUBJECTS (may be empty), SOURCE_TAG_REF (may be empty)

Stable-branch detection: when current branch has its own stable tag in
first-parent history, baseline is that tag; otherwise baseline is the
most recent stable tag reachable from the common ancestor between HEAD
and the source-tag commit.

Usage:
    release_setup.py
    IFS= read -r SKILL_DIR < "${TMPDIR:-/tmp}/release-setup-${CSID}/SKILL_DIR" 2>/dev/null || SKILL_DIR=""

Exit codes:
    0 — always (caller validates resolved values)
    2 — bad/missing required argument (argparse default)
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from shutil import which

_EXCLUDE_FLAGS: tuple[str, ...] = (
    "--exclude=*rc*",
    "--exclude=*dev*",
    "--exclude=*alpha*",
    "--exclude=*beta*",
)


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


def _git(git_path: str, *args: str) -> str:
    """Run a git subcommand; return stripped stdout or empty string on failure.

    Args:
        git_path: Absolute path to the git binary.
        *args: Arguments to pass after ``git``.

    Returns:
        Stripped stdout on exit-0; empty string otherwise.

    Examples:
        No doctest — subprocess-dependent; covered by pytest.
    """
    result = subprocess.run(  # noqa: S603
        [git_path, *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _resolve_skill_dir() -> str:
    """Find installed release skill directory, falling back to source tree path.

    Replicates: ``find ~/.claude/plugins -path "*/oss/*/skills/release" -type d | head -1``

    Cache layout is ``<marketplace>/oss/<version>/skills/release`` — the version
    segment sits between the plugin id and ``skills``, so the plugin-id check
    lands 4 segments back from ``release``, not 3.

    Returns:
        Absolute path to the installed ``oss/<version>/skills/release`` directory,
        or the source-tree fallback ``"plugins/cc_oss/skills/release"`` when not
        installed.

    Examples:
        >>> isinstance(_resolve_skill_dir(), str)
        True
    """
    plugin_root = Path.home() / ".claude" / "plugins"
    if plugin_root.is_dir():
        for match in sorted(plugin_root.rglob("release")):
            parts = match.parts
            if match.is_dir() and len(parts) >= 4 and parts[-4] == "oss" and parts[-2:] == ("skills", "release"):
                return str(match)
    return "plugins/cc_oss/skills/release"


def main(argv: list[str] | None = None) -> int:
    """Entry point — writes resolved vars to ${TMPDIR:-/tmp}/release-setup-${CSID}/<KEY> files.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``); no flags.

    Returns:
        Always 0 — caller validates resolved values; argparse exits 2 on bad args.

    Examples:
        No doctest — subprocess-dependent; covered by pytest.
    """
    argparse.ArgumentParser(
        prog="release_setup.py",
        description="Resolve shared setup vars (skill dir, repo root, branch, date, baseline tag) for /oss:release.",
    ).parse_args(argv)

    sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]
    git = _resolve("git")

    skill_dir = _resolve_skill_dir()
    repo_root = _git(git, "rev-parse", "--show-toplevel") or "."
    raw_branch = _git(git, "branch", "--show-current") or "main"
    branch = raw_branch.replace("/", "-")
    date = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

    branch_tag = _git(git, "describe", "--tags", "--abbrev=0", "--first-parent", *_EXCLUDE_FLAGS)

    cherry_pick_subjects = ""
    source_tag_ref = ""

    if branch_tag:
        last_tag = branch_tag
    else:
        source_tag = _git(git, "describe", "--tags", "--abbrev=0", *_EXCLUDE_FLAGS)
        if not source_tag:
            first_commit_out = _git(git, "rev-list", "--max-parents=0", "HEAD")
            source_tag = first_commit_out.splitlines()[0] if first_commit_out else ""
            print(
                "ℹ No stable tags found — using initial commit as range base"
                " (first release; range covers full history)",
                file=sys.stderr,
            )

        source_commit_raw = _git(git, "rev-list", "-n1", f"refs/tags/{source_tag}")
        source_commit = source_commit_raw or source_tag

        common_commit = _git(git, "merge-base", "HEAD", source_commit)
        if not common_commit:
            print("Warning: no common ancestor found — range may span full history", file=sys.stderr)
            initial_out = _git(git, "rev-list", "--max-parents=0", "HEAD")
            common_commit = initial_out.splitlines()[0] if initial_out else ""

        last_tag = _git(git, "describe", "--tags", "--abbrev=0", *_EXCLUDE_FLAGS, common_commit)
        if not last_tag:
            last_tag = common_commit

        cherry_pick_subjects = _git(git, "log", f"{last_tag}..{source_tag}", "--no-merges", "--format=%s")
        source_tag_ref = source_tag
        print(f"ℹ Stable-branch mode: base={last_tag}  source={source_tag}", file=sys.stderr)

    csid = os.environ.get("CSID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "shared"
    tmp = os.environ.get("TMPDIR") or tempfile.gettempdir()
    out_dir = Path(tmp) / f"release-setup-{csid}"
    out_dir.mkdir(parents=True, exist_ok=True)

    for key, val in (
        ("SKILL_DIR", skill_dir),
        ("REPO_ROOT", repo_root),
        ("BRANCH", branch),
        ("DATE", date),
        ("LAST_TAG", last_tag),
        ("CHERRY_PICK_SUBJECTS", cherry_pick_subjects),
        ("SOURCE_TAG_REF", source_tag_ref),
    ):
        (out_dir / key).write_text(f"{val}\n", encoding="utf-8")

    return 0


if __name__ == "__main__":
    sys.exit(main())
