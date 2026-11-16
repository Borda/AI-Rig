#!/usr/bin/env python3
"""Call-graph census: score codemap-py ``fn-rdeps`` callers against an independent static reference tool.

For every repository in ``callgraph-census-repos.json`` the census fetches the pinned commit into its own empty
directory, builds a fresh codemap-py index with the scanner checked out at ``--scanner-ref``, resolves every candidate
call site with jedi (static inference; no repository code runs), draws the pre-registered stratified sample of public
targets from the reference tool's own caller counts, and only then queries ``fn-rdeps`` and reports edge recall and
precision with 95% Wilson intervals, miss and extra classes, and a seeded adjudication draw.

The census runs no model and has no A/B/C arms. It needs network access to fetch the pinned commits and jedi
(pinned in the ``bench`` dependency group) in the interpreter that runs it.

Usage:
    python benchmarks/run-callgraph-census.py --scanner-ref HEAD --out /path/to/out --workdir /path/to/empty
    python benchmarks/run-callgraph-census.py --scanner-dir . --out /path/to/out --workdir /path/to/empty
"""

from __future__ import annotations

import json
import platform
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import fire
from _bench_callgraph.jedi_backend import JEDI_VERSION, JediBackend
from _bench_callgraph.run import load_registry, render_markdown, run_repository
from _bench_census.run import CensusError, remove_tree, scanner_checkout, scanner_directory

BENCHMARKS_DIR = Path(__file__).resolve().parent
DEFAULT_REGISTRY = BENCHMARKS_DIR / "callgraph-census-repos.json"


def _utc() -> str:
    """Return the current UTC time in ISO form with a ``Z`` suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(
    scanner_ref: str | None = None,
    out: str | None = None,
    workdir: str | None = None,
    registry: str | None = None,
    scanner_repo: str | None = None,
    workers: int = 4,
    keep_workdir: bool = False,
    scanner_dir: str | None = None,
) -> None:
    """Run the census and write ``callgraph-census.json`` and ``callgraph-census.md``.

    Args:
        scanner_ref: Revision of this repository whose codemap-py builds the indexes and answers ``fn-rdeps``;
            default ``HEAD``. Excludes ``scanner_dir``.
        out: Output directory; default ``benchmarks/results/callgraph-census-<UTC>-<sha12>/``.
        workdir: Empty or missing directory for clones, indexes and jedi's cache; default a temporary directory.
        registry: Pre-registration JSON; default ``callgraph-census-repos.json`` beside this script.
        scanner_repo: Repository holding the scanner; default the repository containing this script.
        workers: Processes resolving call sites with jedi.
        keep_workdir: Keep the work directory for inspection.
        scanner_dir: Repository root (working tree or a copy) whose codemap-py is used as it stands, uncommitted edits
            included; the payload then names it ``working-tree`` with a source digest instead of a commit SHA.

    Raises:
        CensusError: When both ``scanner_ref`` and ``scanner_dir`` are given.
    """
    if scanner_dir is not None and scanner_ref is not None:
        raise CensusError("pass either --scanner-ref or --scanner-dir, not both")
    registry_data = load_registry(Path(registry).resolve() if registry else DEFAULT_REGISTRY)
    print(f"PRE-SCORING registry={registry_data.path.name} sha256={registry_data.sha256} utc={_utc()}", flush=True)
    work = Path(workdir).resolve() if workdir else Path(tempfile.mkdtemp(prefix="callgraph-census-")).resolve()
    if work.exists() and any(work.iterdir()):
        raise CensusError(f"--workdir {work} is not empty; pass an empty or missing directory")
    work.mkdir(parents=True, exist_ok=True)
    repo_root = Path(scanner_repo).resolve() if scanner_repo else BENCHMARKS_DIR.parent
    started = _utc()
    try:
        source = (
            scanner_directory(Path(scanner_dir))
            if scanner_dir is not None
            else scanner_checkout(repo_root, scanner_ref or "HEAD", work)
        )
        with source as scanner:
            results = [
                run_repository(
                    coordinate,
                    scanner,
                    work,
                    lambda clone: JediBackend(root=clone, cache_directory=work / "jedi-cache"),
                    registry_data,
                    workers,
                )
                for coordinate in registry_data.coordinates
            ]
    finally:
        if not keep_workdir:
            remove_tree(work)
    payload = {
        "registry": {"path": registry_data.path.name, "sha256": registry_data.sha256},
        "scanner": {"ref": scanner.ref, "sha": scanner.sha, "scan_version": scanner.scan_version},
        "reference_tool": {
            "name": "jedi",
            "version": JEDI_VERSION,
            "environment": "census interpreter (sys.executable), not $VIRTUAL_ENV",
            "search_path": "src/ when present, then repository root; smart_sys_path adds the importing file's directories",
        },
        "python": platform.python_version(),
        "started": started,
        "finished": _utc(),
        "repositories": results,
    }
    target = Path(out) if out else BENCHMARKS_DIR / "results" / f"callgraph-census-{started}-{scanner.sha[:12]}"
    target.mkdir(parents=True, exist_ok=True)
    markdown = render_markdown(results)
    (target / "callgraph-census.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", "utf-8")
    (target / "callgraph-census.md").write_text(markdown, encoding="utf-8")
    sys.stdout.write(markdown)


if __name__ == "__main__":
    fire.Fire(main)
