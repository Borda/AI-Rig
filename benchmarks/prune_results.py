#!/usr/bin/env python3
"""Age out benchmark run artifacts under ``benchmarks/results/``.

``benchmarks/results/`` is gitignored and never committed, so nothing reclaims it: every
benchmark run adds a directory and none are removed. Left alone it reached 4.4 GB across
349 runs, holding 42160 ``.py`` files — enough that tree-walking tools in this repository
spent most of their time there rather than in the source.

The 30-day default matches the retention the artifact-lifecycle rule already states for
dot-prefixed artifact directories; this brings ``benchmarks/results/`` under the same
policy, which it was outside of only because it is not dot-prefixed.

Deletion is irreversible and nothing here is recoverable from git, so the default is a dry
run: ``--apply`` is required to remove anything.

Examples:
    List what a prune would remove, without removing it::

        $ python benchmarks/prune_results.py
        would remove 12 entr(y|ies), 340.2 MB (older than 30 days)

    Remove them::

        $ python benchmarks/prune_results.py --apply

    Keep a different window::

        $ python benchmarks/prune_results.py --days 90 --apply

A run named by tracked documentation is kept whatever its age, because the prose citing it
is the thing that makes it evidence rather than scratch.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_RETENTION_DAYS = 30
_SECONDS_PER_DAY = 86400
_BYTES_PER_MB = 1024 * 1024
_GIT_TIMEOUT_S = 30


def results_dir(repo_root: Path) -> Path:
    """Return the benchmark results directory for *repo_root*.

    Args:
        repo_root: repository root holding ``benchmarks/``.

    Examples:
        >>> results_dir(Path("/repo")).as_posix()
        '/repo/benchmarks/results'
    """
    return repo_root / "benchmarks" / "results"


class CitationsUnavailableError(RuntimeError):
    """Raised when the set of documented run names cannot be established."""


def cited_names(repo_root: Path) -> set[str]:
    """Return every results-directory name referenced by a tracked file.

    Run directories are gitignored, but the prose that interprets them is not: benchmark
    write-ups cite specific runs by name as the evidence behind a published number. Such a
    run is documentation, not scratch, however old its mtime — deleting one silently
    strands a conclusion that can never be re-verified.

    Args:
        repo_root: repository root to search.

    Returns:
        The bare directory names appearing after ``results/`` in any tracked file.

    Raises:
        CitationsUnavailableError: git is absent, or the search failed. The caller must
            treat this as "cannot prove a run is uncited" rather than as "nothing is
            cited" — an empty set from a failed search would authorise deleting
            everything.
    """
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "grep", "--no-color", "-ohI", "-E", r"results/[A-Za-z0-9._-]+"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CitationsUnavailableError(f"could not search tracked files: {exc}") from exc
    # git grep exits 1 for "no matches", which is a legitimate empty result; anything
    # higher is a real failure and must not read as "nothing is cited".
    if completed.returncode > 1:
        raise CitationsUnavailableError(completed.stderr.strip() or "git grep failed")
    return {line.split("/", 1)[1] for line in completed.stdout.splitlines() if "/" in line}


def entry_size_bytes(path: Path) -> int:
    """Return the total size of *path*, recursing when it is a directory.

    Symlinks are measured as links rather than followed, so a link into a large tree
    outside the results directory is never counted against it.

    Args:
        path: file or directory to measure.
    """
    if path.is_symlink() or path.is_file():
        return path.lstat().st_size
    return sum(child.lstat().st_size for child in path.rglob("*") if not child.is_dir())


def stale_entries(directory: Path, days: int, now: float | None = None) -> list[Path]:
    """Return the entries in *directory* last modified more than *days* ago, oldest first.

    Only immediate children are considered: a run is one directory (or one report file),
    and a run is kept or removed whole.

    Args:
        directory: the results directory to scan.
        days: retention window; entries at least this old are returned.
        now: epoch seconds to measure against; defaults to the current time.

    Returns:
        Matching paths sorted oldest first, so a caller printing them shows the most
        obviously disposable ones at the top.
    """
    if not directory.is_dir():
        return []
    cutoff = (time.time() if now is None else now) - days * _SECONDS_PER_DAY
    matches = [child for child in directory.iterdir() if child.lstat().st_mtime < cutoff]
    return sorted(matches, key=lambda p: p.lstat().st_mtime)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_RETENTION_DAYS,
        help=f"retention window in days (default: {DEFAULT_RETENTION_DAYS})",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually delete; without it the script only reports what it would delete",
    )
    parser.add_argument(
        "--ignore-citations",
        action="store_true",
        help="delete even runs cited by tracked documentation (last resort; strands those citations)",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="repository root (default: the repository containing this script)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Report or remove aged-out benchmark results; return a process exit code."""
    args = _parse_args(argv)
    if args.days < 1:
        print("prune_results: --days must be at least 1", file=sys.stderr)
        return 2

    directory = results_dir(args.root)
    victims = stale_entries(directory, args.days)
    if not victims:
        print(f"prune_results: nothing older than {args.days} days in {directory}")
        return 0

    protected: list[Path] = []
    if not args.ignore_citations:
        try:
            cited = cited_names(args.root)
        except CitationsUnavailableError as exc:
            # Fail closed. An age-based prune once removed 18 runs that benchmarks/README.md
            # cites as frozen evidence; they were gitignored, so nothing could restore them.
            # Refusing here costs a rerun, guessing costs the evidence.
            print(f"prune_results: cannot determine which runs are cited ({exc}) — refusing to delete", file=sys.stderr)
            print("prune_results: re-run with --ignore-citations to override", file=sys.stderr)
            return 2
        protected = [path for path in victims if path.name in cited]
        victims = [path for path in victims if path.name not in cited]

    if protected:
        print(f"prune_results: keeping {len(protected)} cited by tracked docs:")
        for path in protected:
            print(f"  {path.relative_to(args.root)}")
    if not victims:
        print(f"prune_results: nothing left to remove older than {args.days} days")
        return 0

    total = sum(entry_size_bytes(path) for path in victims)
    verb = "removing" if args.apply else "would remove"
    print(f"prune_results: {verb} {len(victims)} entries, {total / _BYTES_PER_MB:.1f} MB (older than {args.days} days)")
    if not args.apply:
        for path in victims[:10]:
            print(f"  {path.relative_to(args.root)}")
        if len(victims) > 10:
            print(f"  ... and {len(victims) - 10} more")
        print("prune_results: re-run with --apply to delete")
        return 0

    for path in victims:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    print(f"prune_results: freed {total / _BYTES_PER_MB:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
