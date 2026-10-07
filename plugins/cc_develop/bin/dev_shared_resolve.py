#!/usr/bin/env python
"""dev_shared_resolve.py — resolve develop plugin _shared/ (Windows-portable).

Locates the develop plugin's ``skills/_shared`` directory from the
installed cache, with a source-tree fallback for local development.
Pure-Python replacement for ``dev-shared-resolve.sh``; no shell
interpolation, no ``find``/``sort`` pipelines.

Resolves **only** this plugin's own shared dir. A ``--foundry`` flag used to
emit foundry's ``skills/_shared`` on a second line; it was removed because
reading a sibling plugin's ``_shared`` made every borrowed file vanish on a
develop-only install (see ``plugins/CLAUDE.md`` §Self-Contained ``_shared``).
Files needed from another plugin are duplicated into this one via
``propagate_shared.py`` MANIFEST, never resolved out of its tree.

Output contract
---------------
* ``<develop-shared-path>\\n``
* Exits 0 always; the caller validates that the printed path exists.

Tier cascade
------------
* **Tier 1** — Cache semver scan under
  ``~/.claude/plugins/cache/borda-ai-rig/<plugin>/*/skills/_shared``,
  skipping versions marked ``.orphaned_at``.
* **Tier 2** — Source-tree fallback ``plugins/<plugin>/skills/_shared``
  (with stderr warning).

<!-- file: dev_shared_resolve.py — consumers: dev-shared-resolve.sh -->
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

#: Marketplace directory name under the Claude plugin cache in which installed plugin versions are searched.
_MARKETPLACE = "borda-ai-rig"
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
    """Extract digit runs from a version dir name for semver-aware sorting.

    Args:
        name: Directory basename.

    Returns:
        Integer list of digit runs (``0.20.0`` → ``[0, 20, 0]``).

    Examples:
        >>> _version_key("0.20.0")
        [0, 20, 0]
        >>> _version_key("0.9.0") < _version_key("0.20.0")
        True
    """
    return [int(t) for t in re.findall(r"\d+", name)]


def _cache_root(home: Path) -> Path:
    """Return marketplace-scoped plugin cache root under ``home``.

    Examples:
        >>> _cache_root(Path("/h")).as_posix()
        '/h/.claude/plugins/cache/borda-ai-rig'
    """
    return home / ".claude" / "plugins" / "cache" / _MARKETPLACE


def _resolve_plugin_shared(plugin: str, home: Path) -> str | None:
    """Cache scan for ``<plugin>/skills/_shared`` skipping orphaned dirs.

    Args:
        plugin: Plugin short-name (``develop`` or ``foundry``).
        home: User home directory.

    Returns:
        Resolved cache path string, or ``None`` if cache has no usable hit.
    """
    plugin_cache = _cache_root(home) / plugin
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


def resolve_shared_path(home: Path | None = None) -> str:
    """Resolve this plugin's own ``skills/_shared`` path.

    Tier 1: cache scan via :func:`_resolve_plugin_shared`.
    Tier 2: source-tree fallback ``plugins/cc_develop/skills/_shared``.

    Args:
        home: Override for ``Path.home()`` (testing).

    Returns:
        Path string for develop's ``skills/_shared``.

    Examples:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as d:
        ...     resolve_shared_path(home=Path(d))  # source-tree fallback
        'plugins/cc_develop/skills/_shared'
    """
    home = home if home is not None else Path.home()
    dev_path = _resolve_plugin_shared("develop", home)
    if dev_path is None:
        dev_path = (Path("plugins") / "cc_develop" / _SHARED_SUBDIR).as_posix()
    return dev_path


def main(argv: list[str] | None = None) -> int:
    """Resolve and print the shared document path."""
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    parser = argparse.ArgumentParser(
        prog="dev_shared_resolve",
        description="Resolve the develop plugin's own skills/_shared path.",
    )
    parser.parse_args(argv)
    print(resolve_shared_path())
    return 0


if __name__ == "__main__":
    sys.exit(main())
