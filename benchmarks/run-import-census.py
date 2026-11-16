#!/usr/bin/env python3
"""Import-edge census: score codemap-py import edges on pinned public repositories against a static truth.

For every repository in ``import-census-repos.json`` the census fetches the pinned commit into its own empty
directory, builds a fresh codemap-py index with the scanner checked out at ``--scanner-ref``, builds the static truth
edge set (stdlib ``PathFinder`` over explicit search paths; no repository code runs), and reports recall, strict
precision and parent-package-adjusted precision with 95% Wilson intervals, plus a rule class for every disagreement.

The census runs no model and has no A/B/C arms. It needs network access to fetch the pinned commits.

Usage:
    python benchmarks/run-import-census.py --scanner-ref HEAD
    python benchmarks/run-import-census.py --scanner-ref 31c2c616 --out /path/to/out --keep-workdir

Outputs ``census.json`` and ``census.md`` in ``--out`` (default ``benchmarks/results/import-census-<UTC>-<sha12>/``)
and prints the Markdown to standard output.
"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import fire
from _bench_census.run import (
    CensusError,
    load_coordinates,
    remove_tree,
    render_markdown,
    run_census,
    scanner_checkout,
    scanner_directory,
    write_results,
)

BENCHMARKS_DIR = Path(__file__).resolve().parent
DEFAULT_COORDINATES = BENCHMARKS_DIR / "import-census-repos.json"


def _empty_workdir(workdir: str | None) -> tuple[Path, bool]:
    """Return the work directory and whether the census created it as a temporary directory."""
    if workdir is None:
        return Path(tempfile.mkdtemp(prefix="import-census-")).resolve(), True
    path = Path(workdir).resolve()
    if path.exists() and any(path.iterdir()):
        raise CensusError(f"--workdir {path} is not empty; pass an empty or missing directory")
    path.mkdir(parents=True, exist_ok=True)
    return path, False


def main(
    scanner_ref: str | None = None,
    out: str | None = None,
    workdir: str | None = None,
    coordinates: str | None = None,
    scanner_repo: str | None = None,
    keep_workdir: bool = False,
    scanner_dir: str | None = None,
) -> None:
    """Run the census and write ``census.json`` and ``census.md``.

    Args:
        scanner_ref: Git revision of the scanner, for example ``HEAD`` or a commit SHA; default ``HEAD``. Quote an
            all-digit SHA prefix. Excludes ``scanner_dir``.
        out: Output directory; defaults to ``benchmarks/results/import-census-<UTC>-<sha12>/``.
        workdir: Empty work directory for clones and the scanner worktree; defaults to a new temporary directory.
        coordinates: Repository coordinates JSON; defaults to ``import-census-repos.json`` beside this script.
        scanner_repo: Git repository holding the scanner; defaults to the repository containing this script.
        keep_workdir: Keep a default temporary work directory instead of deleting it.
        scanner_dir: Repository root (working tree or a copy) whose codemap-py is used as it stands, uncommitted edits
            included; the result then names it ``working-tree`` with a source digest instead of a commit SHA.

    Raises:
        CensusError: When an argument is invalid or a census step fails.

    Examples:
        >>> main(scanner_ref=31)
        Traceback (most recent call last):
        ...
        _bench_census.run.CensusError: --scanner-ref must be a string, got int 31; quote an all-digit SHA prefix
    """
    if scanner_ref is not None and not isinstance(scanner_ref, str):
        raise CensusError(
            f"--scanner-ref must be a string, got {type(scanner_ref).__name__} {scanner_ref!r}; "
            "quote an all-digit SHA prefix"
        )
    if scanner_dir is not None and scanner_ref is not None:
        raise CensusError("pass either --scanner-ref or --scanner-dir, not both")
    pinned = load_coordinates(Path(coordinates) if coordinates else DEFAULT_COORDINATES)
    repo_root = Path(scanner_repo).resolve() if scanner_repo else BENCHMARKS_DIR.parent
    work, temporary = _empty_workdir(workdir)
    try:
        source = (
            scanner_directory(Path(scanner_dir))
            if scanner_dir is not None
            else scanner_checkout(repo_root, scanner_ref or "HEAD", work)
        )
        with source as scanner:
            result = run_census(pinned, scanner, work)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_dir = Path(out) if out else BENCHMARKS_DIR / "results" / f"import-census-{stamp}-{result.scanner_sha[:12]}"
        json_path, markdown_path = write_results(result, out_dir)
    finally:
        if temporary and not keep_workdir:
            remove_tree(work)
    sys.stdout.write(render_markdown(result))
    sys.stdout.write(f"\nWrote {json_path}\nWrote {markdown_path}\n")


if __name__ == "__main__":
    fire.Fire(main)
