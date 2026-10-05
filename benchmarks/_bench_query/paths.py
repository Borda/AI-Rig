"""Repository, index, and plugin-root resolution for the codemap-cli benchmark."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from _bench_common.codemap_discovery import (
    git_toplevel,
)
from _bench_common.codemap_discovery import (
    resolve_index_path as _util_resolve_index_path,
)

from _bench_query.output import log

# ---- MAIN ----


def resolve_repo_path(arg: str | None) -> Path | None:
    """Resolve the path to the pytorch-lightning clone to benchmark.

    Resolution order:
    1. ``arg`` when provided and is an existing directory.
    2. ``$PYTORCH_LIGHTNING_PATH`` environment variable.
    3. ``.sandbox/pytorch-lightning`` — the pinned in-project clone (run from project root).

    Args:
        arg: Value of the ``--repo-path`` CLI flag, or ``None``.

    Returns:
        Resolved :class:`~pathlib.Path` to the repository root, or ``None``
        when no valid directory is found (error logged to stderr).
    """
    if arg:
        p = Path(arg)
        if p.is_dir():
            return p
        log(f"ERROR: --repo-path {arg} is not a directory")
        return None
    env_path = os.environ.get("PYTORCH_LIGHTNING_PATH")
    if env_path:
        p = Path(env_path)
        if p.is_dir():
            return p
        log(f"WARN: $PYTORCH_LIGHTNING_PATH={env_path} is not a directory")
    local = Path(".sandbox/pytorch-lightning")
    if local.is_dir():
        return local
    log("ERROR: cannot find pytorch-lightning repo. Provide --repo-path or set $PYTORCH_LIGHTNING_PATH")
    return None


def resolve_index_path(arg: str | None, repo_path: Path) -> Path:
    """Resolve the path to the pre-built codemap JSON index.

    When ``arg`` is given it is used directly.  Otherwise searches
    ``<repo_path>/.cache/codemap/`` then ``.cache/scan/`` for a JSON whose stem
    matches the repo dir name (``-master`` / ``-main`` stripped), falling back to
    the first ``.json`` found, then ``<repo_path>/.cache/codemap/<bare-name>.json``.

    Args:
        arg: Value of the ``--index-path`` CLI flag, or ``None``.
        repo_path: Resolved path to the pytorch-lightning repository root.

    Returns:
        :class:`~pathlib.Path` to the index file (may not exist yet when unbuilt;
        callers must check :meth:`~pathlib.Path.exists`).
    """
    # missing="bare": return a constructed path (never raise) — this lane may run pre-build.
    return _util_resolve_index_path(repo_path, arg or None, strip_suffixes=True, missing="bare")


def _resolve_plugin_root() -> Path | None:
    """Return the git top-level directory as the plugin root, or None when unavailable."""
    return git_toplevel()


# Lowest index scan_version the self-consistency track (S/H/X) needs: the X suite's
# ``xrefs --broken`` is gated by SPHINX_XREFS_MIN_VER (5) in codemap _schema.py; an
# older index makes those suites fail cryptically, so we skip them instead.
_SELF_CONSISTENCY_MIN_VER = 5


def _index_scan_version(index_path: Path) -> int:
    """Return the ``scan_version`` recorded in *index_path*, or ``0`` if unreadable.

    Args:
        index_path: Path to the codemap index JSON.

    Returns:
        The ``scan_version`` int, or ``0`` on any read/parse error.
    """
    try:
        with index_path.open() as f:
            return int(json.load(f).get("scan_version", 0))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 0


def _ensure_index(index_path: Path, repo_path: Path, scan_index_bin: Path | None) -> Path:
    """Return an existing index path, building it once via scan-index when missing.

    Exits with a clear message when the index cannot be located or built
    (scan-index absent, build failure, or still-missing after build).

    Args:
        index_path: Resolved (possibly non-existent) candidate index path.
        repo_path: Repository root passed to scan-index ``--root``.
        scan_index_bin: Path to scan-index, or None when it could not be found.

    Returns:
        Path to an index file that exists on disk.
    """
    if index_path.exists():
        return index_path
    log(f"[index] not found at {index_path}")
    if scan_index_bin is None:
        log("ERROR: scan-index not found — cannot auto-build the index.")
        log(f"Run manually:  python3 plugins/codemap-py/bin/scan-index --root {repo_path}")
        log("Then retry, or pass --index-path <path-to-index.json>.")
        sys.exit(1)
    log(f"[index] building now via {scan_index_bin} --root {repo_path} ...")
    result = subprocess.run(
        [sys.executable, str(scan_index_bin), "--root", str(repo_path)], capture_output=True, text=True, timeout=360
    )
    if result.returncode != 0:
        log(f"ERROR: scan-index failed:\n{result.stderr}")
        sys.exit(1)
    log(result.stdout.strip())
    rebuilt = resolve_index_path(None, repo_path)
    if not rebuilt.exists():
        log(f"ERROR: index still not found at {rebuilt} after build.")
        log("Try: --index-path <path-to-index.json>")
        sys.exit(1)
    return rebuilt
