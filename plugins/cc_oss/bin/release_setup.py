#!/usr/bin/env python
"""release_setup.py — shared setup block for /oss:release modes.

Resolves skill directory (installed cache → source tree fallback), repo
root, portable scratch slug, branch state key, raw branch ref, current UTC date, and branch-aware last-stable-tag
baseline. Writes each resolved value to its own file under
``${TMPDIR:-/tmp}/release-setup-${CSID}/`` for safe cross-block
consumption. Informational notes go to stderr only.

Output files (written to ``${TMPDIR:-/tmp}/release-setup-${CSID}/``):
    SKILL_DIR, REPO_ROOT, BRANCH, BRANCH_REF, BRANCH_KEY, DATE, LAST_TAG,
    CHERRY_PICK_SUBJECTS (may be empty), SOURCE_TAG_REF (may be empty),
    SHIPPED_ELSEWHERE_TAGS (may be empty; space-separated, newest first)

Stable-branch detection: when current branch has its own stable tag in
first-parent history, baseline is that tag; otherwise baseline is the
most recent stable tag reachable from the common ancestor between HEAD
and the source-tag commit.

Off-ancestry tags: with a first-parent baseline, stable tags newer than it that descend from it but are not reachable
from HEAD (a maintenance branch's 1.11.1 and 1.11.2 next to HEAD's 1.11.0) are listed in SHIPPED_ELSEWHERE_TAGS.
Gather subtracts the range commits those tags already shipped (``release_shipped_elsewhere.py``).

Usage:
    release_setup.py
    IFS= read -r SKILL_DIR < "${TMPDIR:-/tmp}/release-setup-${CSID}/SKILL_DIR" 2>/dev/null || SKILL_DIR=""

Exit codes:
    0 — successful setup
    1 — detached HEAD (no branch state published), or a sentinel directory that is a symlink / owned by another user
    2 — bad/missing required argument (argparse default)
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from shutil import which
from urllib.parse import quote

from release_append_marker import branch_state_key
from resolve_shared_path import resolve as resolve_plugin_dir

#: ``git describe`` flags that skip pre-release tags (rc, dev, alpha, beta) when looking for the last release tag.
_EXCLUDE_FLAGS: tuple[str, ...] = (
    "--exclude=*rc*",
    "--exclude=*dev*",
    "--exclude=*alpha*",
    "--exclude=*beta*",
)
#: Keep baseline inputs within the same SemVer/SHA allow-list as run_audit_checks.py.
_TAG_OR_SHA_RE = re.compile(r"^(?:v?[0-9]+\.[0-9]+(?:\.[0-9]+)?(?:-[a-zA-Z0-9.]+)?|[0-9a-f]{7,40})$")


def _validate_baseline(ref: str) -> None:
    """Refuse a baseline outside the release audit's SemVer or hexadecimal SHA allow-list."""
    if ref.startswith("-") or not _TAG_OR_SHA_RE.fullmatch(ref):
        raise ValueError(f"invalid release baseline (must be SemVer or 7-40 hex SHA): {ref!r}")


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


def _git_ok(git_path: str, *args: str) -> bool:
    """Return whether a git subcommand exits zero (for yes/no queries such as ``merge-base --is-ancestor``).

    Args:
        git_path: Absolute path to the git binary.
        *args: Arguments to pass after ``git``.

    Returns:
        ``True`` on exit-0, ``False`` on any other exit.

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
    return result.returncode == 0


def _is_prerelease_tag(tag: str) -> bool:
    """Return whether ``tag`` matches one of the ``_EXCLUDE_FLAGS`` pre-release globs.

    Args:
        tag: Tag name.

    Returns:
        ``True`` when the name contains ``rc``, ``dev``, ``alpha`` or ``beta``.

    Examples:
        >>> _is_prerelease_tag("v1.2.0rc1"), _is_prerelease_tag("1.11.2")
        (True, False)
    """
    return any(marker in tag for marker in ("rc", "dev", "alpha", "beta"))


def _shipped_elsewhere_tags(git: str, last_tag: str) -> list[str]:
    """List stable tags newer than ``last_tag`` that were cut off HEAD's ancestry.

    A maintenance branch such as ``release/latest`` can carry 1.11.1 and 1.11.2 while HEAD's own newest tag is
    1.11.0. ``git describe --first-parent`` never sees them, so work backported into those tags would look new in a
    ``last_tag..HEAD`` range. A tag qualifies when it sorts after ``last_tag``, is not reachable from HEAD, and descends
    from ``last_tag`` (a tag of an unrelated line does not describe what users of this line already received).

    Args:
        git: Absolute path to the git binary.
        last_tag: Baseline tag resolved from HEAD's first-parent history.

    Returns:
        Qualifying tags, newest version first; empty when none, or when ``last_tag`` is not itself a tag.

    Examples:
        No doctest — subprocess-dependent; covered by pytest.
    """
    by_version = _git(git, "tag", "--list", "--sort=-v:refname").splitlines()
    if last_tag not in by_version:
        return []
    unmerged = set(_git(git, "tag", "--list", "--no-merged", "HEAD").split())
    newer = by_version[: by_version.index(last_tag)]
    return [
        tag
        for tag in newer
        if tag in unmerged
        and not _is_prerelease_tag(tag)
        and _git_ok(git, "merge-base", "--is-ancestor", "--end-of-options", last_tag, tag)
    ]


def _native_temp_dir() -> Path:
    """Resolve a Git Bash temporary path for the native Python process on Windows."""
    configured = os.environ.get("TMPDIR")
    if not configured:
        # the shell spells this ${TMPDIR:-/tmp}; gettempdir() would honour TEMP/TMP first and disagree on POSIX
        return Path(tempfile.gettempdir() if sys.platform == "win32" else "/tmp")  # noqa: S108 - shell default
    if sys.platform != "win32" or PureWindowsPath(configured).is_absolute():
        return Path(configured)
    converted = subprocess.run(["cygpath", "-w", configured], capture_output=True, text=True, check=True, timeout=5)  # noqa: S603, S607 - argv list, no shell; cygpath resolved via PATH on purpose
    return Path(converted.stdout.strip())


def _shell_path(path: str) -> str:
    """Emit paths in Git Bash syntax when native Python is called from that shell."""
    if sys.platform != "win32" or not PureWindowsPath(path).is_absolute():
        return path
    converted = subprocess.run(["cygpath", "-u", path], capture_output=True, text=True, check=True, timeout=5)  # noqa: S603, S607 - argv list, no shell; cygpath resolved via PATH on purpose
    return converted.stdout.strip()


def _resolve_skill_dir() -> str:
    """Find the active release skill directory, falling back to the source tree path.

    Delegates to the plugin's shared resolver: the install registry decides first, so the version Claude Code
    actually loads wins; the cache scan that follows skips orphaned installs and sorts versions numerically
    (``0.100`` after ``0.50``). The result is later read and executed as instructions, so a stale or
    unrelated directory must never win.

    Cache layout is ``<marketplace>/oss/<version>/skills/release``.

    Returns:
        Path to the active ``oss/<version>/skills/release`` directory, or its verified source-tree fallback.

    Raises:
        ValueError: No trusted release skill directory is available.

    Examples:
        >>> isinstance(_resolve_skill_dir(), str)
        True
    """
    path, tier = resolve_plugin_dir("oss", "skills/release")
    if tier == -1:
        raise ValueError("release skill directory not found in registry, cache, or source tree")
    return path


def _resolve_stable_baseline(git: str) -> tuple[str, str, str]:
    """Resolve the stable-branch baseline when the current branch has no own stable tag.

    Source-tag resolution (with its own first-commit fallback), ``merge-base``
    common-ancestor resolution (with its own fallback), ``last_tag`` resolution,
    and the cherry-pick-subjects log.

    Args:
        git: Absolute path to the git binary.

    Returns:
        ``(last_tag, cherry_pick_subjects, source_tag_ref)``.
    """
    source_tag = _git(git, "describe", "--tags", "--abbrev=0", *_EXCLUDE_FLAGS)
    if not source_tag:
        first_commit_out = _git(git, "rev-list", "--max-parents=0", "HEAD")
        source_tag = first_commit_out.splitlines()[0] if first_commit_out else ""
        print(
            "ℹ No stable tags found — using initial commit as range base (first release; range covers full history)",
            file=sys.stderr,
        )

    _validate_baseline(source_tag)
    source_commit_raw = _git(git, "rev-list", "-n1", "--end-of-options", f"refs/tags/{source_tag}")
    source_commit = source_commit_raw or source_tag

    common_commit = _git(git, "merge-base", "--end-of-options", "HEAD", source_commit)
    if not common_commit:
        print("Warning: no common ancestor found — range may span full history", file=sys.stderr)
        initial_out = _git(git, "rev-list", "--max-parents=0", "HEAD")
        common_commit = initial_out.splitlines()[0] if initial_out else ""

    last_tag = _git(git, "describe", "--tags", "--abbrev=0", *_EXCLUDE_FLAGS, "--end-of-options", common_commit)
    if not last_tag:
        last_tag = common_commit

    _validate_baseline(last_tag)
    cherry_pick_subjects = _git(
        git, "log", "--no-merges", "--format=%s", "--end-of-options", f"{last_tag}..{source_tag}"
    )
    source_tag_ref = source_tag
    print(f"ℹ Stable-branch mode: base={last_tag}  source={source_tag}", file=sys.stderr)

    return last_tag, cherry_pick_subjects, source_tag_ref


def _prepare_out_dir(out_dir: Path) -> None:
    """Create the sentinel directory private to this user, refusing one planted by someone else.

    The sentinels sit under a predictable name in a possibly shared temp dir and later steps read ``SKILL_DIR``
    back and execute the instructions found there, so a directory (or symlink) pre-created by another user
    would swap those instructions.

    Args:
        out_dir: Sentinel directory to create or verify.

    Raises:
        PermissionError: If ``out_dir`` is a symlink or is owned by another user.

    Examples:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as d:
        ...     target = Path(d) / "release-setup-doctest"
        ...     _prepare_out_dir(target)
        ...     target.is_dir()
        True
    """
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if out_dir.is_symlink():
        raise PermissionError(f"refusing symlinked sentinel directory: {out_dir}")
    if hasattr(os, "getuid"):
        if out_dir.lstat().st_uid != os.getuid():
            raise PermissionError(f"refusing sentinel directory owned by another user: {out_dir}")
        out_dir.chmod(0o700)


def _write_private(path: Path, text: str) -> None:
    """Create ``path`` fresh (mode 0600), never writing through a pre-existing file or symlink.

    Args:
        path: Sentinel file to create; an existing entry is unlinked first.
        text: Content to write, UTF-8 with LF newlines.

    Examples:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as d:
        ...     target = Path(d) / "KEY"
        ...     _write_private(target, "a\\n")
        ...     _write_private(target, "b\\n")
        ...     target.read_text(encoding="utf-8")
        'b\\n'
    """
    path.unlink(missing_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags, 0o600), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _write_setup_outputs(out_dir: Path, values: dict[str, str]) -> None:
    """Write each resolved value to its own file under ``out_dir``.

    Args:
        out_dir: Directory to write ``<KEY>`` files into (caller ensures it exists).
        values: Ordered key → value mapping; one file written per entry.
    """
    for key, val in values.items():
        _write_private(out_dir / key, f"{val}\n")


def main(argv: list[str] | None = None) -> int:
    """Resolve release setup values and publish them for later steps.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``); no flags.

    Returns:
        Zero after setup; one on detached HEAD or an unresolved skill directory; argparse exits 2 on bad args.

    Examples:
        No doctest — subprocess-dependent; covered by pytest.
    """
    argparse.ArgumentParser(
        prog="release_setup.py",
        description="Resolve shared setup vars (skill dir, repo root, branch, date, baseline tag) for /oss:release.",
    ).parse_args(argv)

    sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]
    git = _resolve("git")

    try:
        skill_dir = _resolve_skill_dir()
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    repo_root = _git(git, "rev-parse", "--show-toplevel") or "."
    raw_branch = _git(git, "branch", "--show-current")
    if not raw_branch:
        print("Error: release requires an attached Git branch", file=sys.stderr)
        return 1
    branch = quote(raw_branch.replace("/", "-"), safe="-_.+")
    date = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

    branch_tag = _git(git, "describe", "--tags", "--abbrev=0", "--first-parent", *_EXCLUDE_FLAGS)

    cherry_pick_subjects = ""
    source_tag_ref = ""
    shipped_elsewhere: list[str] = []

    try:
        if branch_tag:
            last_tag = branch_tag
            _validate_baseline(last_tag)
            shipped_elsewhere = _shipped_elsewhere_tags(git, last_tag)
            if shipped_elsewhere:
                print(
                    f"ℹ Newer stable tags off this branch's ancestry: {' '.join(shipped_elsewhere)} - "
                    "range commits they already shipped are excluded at Gather",
                    file=sys.stderr,
                )
        else:
            last_tag, cherry_pick_subjects, source_tag_ref = _resolve_stable_baseline(git)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    csid = os.environ.get("CSID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "shared"
    out_dir = _native_temp_dir() / f"release-setup-{csid}"
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        _prepare_out_dir(out_dir)
    except OSError as exc:
        print(f"Error: unsafe release sentinel directory — {exc}", file=sys.stderr)
        return 1

    values = {
        "SKILL_DIR": _shell_path(skill_dir),
        "REPO_ROOT": _shell_path(repo_root),
        "BRANCH": branch,
        "BRANCH_REF": raw_branch,
        "BRANCH_KEY": branch_state_key(raw_branch),
        "DATE": date,
        "LAST_TAG": last_tag,
        "CHERRY_PICK_SUBJECTS": cherry_pick_subjects,
        "SOURCE_TAG_REF": source_tag_ref,
        "SHIPPED_ELSEWHERE_TAGS": " ".join(shipped_elsewhere),
    }
    _write_setup_outputs(out_dir, values)

    return 0


if __name__ == "__main__":
    sys.exit(main())
