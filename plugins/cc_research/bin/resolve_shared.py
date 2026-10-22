#!/usr/bin/env python
"""resolve_shared.py — resolve research plugin _shared/ (Windows-portable).

Pure-Python replacement for ``resolve-shared.sh``. Locates the research
plugin's ``skills/_shared`` directory via a cache scan, with a
source-tree fallback for local development.

Tier cascade
------------
* **Tier 1** — Cache semver scan under
  ``~/.claude/plugins/cache/borda-ai-rig/research/*/skills/_shared``,
  skipping versions marked ``.orphaned_at``.
* **Tier 2** — Source-tree fallback ``plugins/cc_research/skills/_shared`` relative to the working directory
  (with stderr warning). The CLI uses it only when the working directory carries
  ``plugins/cc_research/.claude-plugin/plugin.json`` whose ``name`` is ``research`` — this plugin's own source
  repository. Anywhere else that tree is the text of whatever repository the session was opened in, so the CLI exits 1
  with a ``!`` message on stderr and prints nothing, with or without flags.

Without flags it prints the resolved path and exits 0; the caller validates that the printed path exists on disk.

Flags (each skill's Agent Resolution block, which replaced a 6-line capture/guard/persist/``cat`` sequence):

* ``--persist SLUG`` writes the resolved path to ``${TMPDIR}/<SLUG>-<CSID>`` (trailing newline), the sentinel every
  later block of the same skill reads back with ``IFS= read -r`` instead of re-running this script. ``CSID`` comes from
  the environment (``CSID``, else ``CLAUDE_CODE_SESSION_ID``, else ``shared``), so callers ``export CSID`` first.
* ``--emit DOC`` (repeatable) prints ``<shared>/DOC`` to stdout, in order — the ``cat`` that followed the capture.
  ``DOC`` is a bare file name; a path separator or ``..`` is rejected.

With either flag the resolution is verified: a missing ``_shared`` directory or document prints a ``!`` message on
stderr and exits 1, so the caller's ``|| exit 1`` replaces the old empty-string guard (which could never fire, since
the source-tree fallback always yields a non-empty path). Without ``--emit`` the path is still printed; with it, only
the documents are.

Usage::

    python resolve_shared.py
    python resolve_shared.py --persist research-shared --emit agent-resolution.md --emit unsupported-flag-protocol.md

<!-- file: resolve_shared.py — consumers: resolve-shared.sh -->
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

#: Sibling import: bin/ is not a package. Putting it on the path resolves the private writer whether this file runs as
#: a script, under the test conftest, or as a doctest module.
_BIN_DIR = str(Path(__file__).resolve().parent)
if _BIN_DIR not in sys.path:
    sys.path.insert(0, _BIN_DIR)

from _sentinel_io import write_private_text  # noqa: E402

#: Marketplace directory name under the Claude plugin cache in which installed plugin versions are searched.
_MARKETPLACE = "borda-ai-rig"
#: Name of this plugin, used as its cache subdirectory and as the cc_ source directory in the fallback path.
_PLUGIN = "research"
#: Path, relative to a plugin root, of the shared-docs directory this resolver locates.
_SHARED_SUBDIR = "skills/_shared"


def _resolve(cmd: str) -> str:
    """Resolve executable basename to absolute path via :func:`shutil.which`.

    Args:
        cmd: Executable basename.

    Returns:
        Absolute path on ``PATH``.

    Raises:
        FileNotFoundError: When ``cmd`` is not on ``PATH``.

    Examples:
        >>> import os
        >>> os.path.isabs(_resolve(sys.executable))
        True
    """
    resolved = shutil.which(cmd)
    if not resolved:
        raise FileNotFoundError(f"required executable not on PATH: {cmd!r}")
    return resolved


def _version_key(name: str) -> list[int]:
    """Extract digit runs for semver-aware sort.

    Args:
        name: Directory basename.

    Returns:
        Integer list (``0.20.0`` → ``[0, 20, 0]``).

    Examples:
        >>> _version_key("0.20.0")
        [0, 20, 0]
        >>> _version_key("0.9.0") < _version_key("0.20.0")
        True
    """
    return [int(t) for t in re.findall(r"\d+", name)]


def _cache_hit(home: Path) -> str | None:
    """Return newest non-orphaned cached research ``skills/_shared`` path.

    Args:
        home: User home directory.

    Returns:
        Path string when cache hit found, else ``None``.
    """
    plugin_cache = home / ".claude" / "plugins" / "cache" / _MARKETPLACE / _PLUGIN
    if not plugin_cache.is_dir():
        return None
    candidates: list[tuple[list[int], Path]] = []
    for version_dir in plugin_cache.iterdir():
        if not version_dir.is_dir():
            continue
        if (version_dir / ".orphaned_at").exists():
            continue
        target = version_dir / _SHARED_SUBDIR
        if target.is_dir():
            candidates.append((_version_key(version_dir.name), target))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0])
    return str(candidates[-1][1])


def resolve(home: Path | None = None) -> tuple[str, bool]:
    """Resolve research plugin's ``skills/_shared`` path.

    Args:
        home: Override for ``Path.home()`` (testing).

    Returns:
        ``(path, from_cache)`` — ``from_cache`` is ``True`` when Tier 1 hit,
        ``False`` when falling back to the source tree (caller emits warning).

    Examples:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as d:
        ...     path, from_cache = resolve(home=Path(d))
        ...     (path, from_cache)
        ('plugins/cc_research/skills/_shared', False)
    """
    home = home if home is not None else Path.home()
    hit = _cache_hit(home)
    if hit is not None:
        return hit, True
    return (Path("plugins") / f"cc_{_PLUGIN}" / _SHARED_SUBDIR).as_posix(), False


def source_repo_marker_present(cwd: Path | None = None) -> bool:
    """Report whether the working directory is this plugin's own source repository.

    The source-tree tier reads ``plugins/cc_research/...`` relative to the working directory. Outside this plugin's
    source repository that tree belongs to whatever repository the session was opened in, so it is trusted only when
    the same directory also carries ``plugins/cc_research/.claude-plugin/plugin.json`` naming ``research``.
    ``load-agent-reference.py`` applies the same gate to its cwd-relative sidecar tier.

    Args:
        cwd: Directory to inspect; defaults to ``Path.cwd()``.

    Returns:
        ``True`` when the manifest exists, parses as a JSON object, and its ``name`` is exactly ``research``.

    Examples:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as d:
        ...     manifest = Path(d) / "plugins" / "cc_research" / ".claude-plugin" / "plugin.json"
        ...     before = source_repo_marker_present(Path(d))
        ...     manifest.parent.mkdir(parents=True)
        ...     _ = manifest.write_text('{"name": "research"}', encoding="utf-8")
        ...     (before, source_repo_marker_present(Path(d)))
        (False, True)
    """
    root = cwd if cwd is not None else Path.cwd()
    manifest = root / "plugins" / f"cc_{_PLUGIN}" / ".claude-plugin" / "plugin.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get("name") == _PLUGIN


def _emit_docs(shared: Path, docs: list[str]) -> int:
    """Print each document under ``shared`` to stdout in order, stopping at the first problem.

    Args:
        shared: Resolved ``skills/_shared`` directory.
        docs: Bare file names to print.

    Returns:
        ``0`` when every document was printed, ``2`` for a name containing a path separator or ``..``, ``1`` when a
        document is missing.
    """
    for doc in docs:
        if not doc or "/" in doc or "\\" in doc or doc == "..":
            print(f"resolve_shared: --emit expects a bare file name, got {doc!r}", file=sys.stderr)
            return 2
        target = shared / doc
        if not target.is_file():
            print(f"! {doc} not found under {shared} — ensure research plugin installed", file=sys.stderr)
            return 1
        sys.stdout.write(target.read_text(encoding="utf-8"))
    return 0


def _persist(slug: str, path: str) -> int:
    """Write ``path`` to ``<tmp>/<slug>-<CSID>`` and return ``0``, or ``1`` when the write fails."""
    csid = os.environ.get("CSID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "shared"
    tmp_fallback = tempfile.gettempdir() if os.name == "nt" else "/tmp"  # noqa: S108 - shell default of ${TMPDIR:-/tmp}
    sentinel = Path(os.environ.get("TMPDIR") or tmp_fallback) / f"{slug}-{csid}"
    try:
        # Never write through a pre-planted symlink at the sentinel path; the private writer replaces it.
        write_private_text(sentinel, path + "\n")
    except OSError as exc:
        print(f"resolve_shared: cannot write {sentinel}: {exc}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """Resolve the shared document path, optionally persisting it and printing documents from it."""
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    parser = argparse.ArgumentParser(
        prog="resolve_shared",
        description="Resolve the research plugin's skills/_shared path.",
    )
    parser.add_argument("--persist", metavar="SLUG", help="Write the path to ${TMPDIR}/<SLUG>-<CSID>.")
    parser.add_argument("--emit", metavar="DOC", action="append", default=[], help="Print <shared>/DOC (repeatable).")
    args = parser.parse_args(argv)
    if args.persist is not None:
        csid = os.environ.get("CSID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "shared"
        if any(not re.fullmatch(r"[A-Za-z0-9._-]+", value) or ".." in value for value in (args.persist, csid)):
            print("resolve_shared: invalid sentinel slug or session identity", file=sys.stderr)
            return 2
    path, from_cache = resolve()
    if not from_cache:
        # Callers `cat` protocol files from this directory; in a repository under analysis it is that repository's text.
        if not source_repo_marker_present():
            print(
                "! Plugin path resolution failed — research plugin not found in cache, and the working directory is "
                f"not the research plugin source repository (no plugins/cc_{_PLUGIN}/.claude-plugin/plugin.json naming "
                f"{_PLUGIN}); refusing to read {path} from it — install research@borda-ai-rig",
                file=sys.stderr,
            )
            return 1
        print(
            "resolve_shared: research plugin not found in cache — using source-tree fallback (local dev only)",
            file=sys.stderr,
        )
    if args.persist is None and not args.emit:
        print(path)
        return 0
    if not Path(path).is_dir():
        print(
            "! Plugin path resolution failed — ensure research plugin installed and CLAUDE_PLUGIN_ROOT set, "
            "or invoke from project root.",
            file=sys.stderr,
        )
        return 1
    if args.persist is not None and (status := _persist(args.persist, path)):
        return status
    if not args.emit:
        print(path)
        return 0
    return _emit_docs(Path(path), args.emit)


if __name__ == "__main__":
    sys.exit(main())
