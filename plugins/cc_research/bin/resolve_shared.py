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
* **Tier 2** — Source-tree fallback ``plugins/cc_research/skills/_shared``
  (with stderr warning).

Always exits 0; the caller validates that the printed path exists on disk.

Usage::

    python resolve_shared.py

<!-- file: resolve_shared.py — consumers: resolve-shared.sh -->
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

_MARKETPLACE = "borda-ai-rig"
_PLUGIN = "research"
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


def main(argv: list[str] | None = None) -> int:
    """Resolve and print the shared document path."""
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    parser = argparse.ArgumentParser(
        prog="resolve_shared",
        description="Resolve the research plugin's skills/_shared path.",
    )
    parser.parse_args(argv)  # no flags; argparse handles -h/--help
    path, from_cache = resolve()
    if not from_cache:
        print(
            "resolve_shared: research plugin not found in cache — using source-tree fallback (local dev only)",
            file=sys.stderr,
        )
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
