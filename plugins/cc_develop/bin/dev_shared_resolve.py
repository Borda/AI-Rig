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
* Exits 0 when a path was resolved; the caller validates that the printed path exists.
* Exits 1, printing nothing, when no develop install is cached and the working directory is not the develop plugin's
  own source repository (see Tier 2).

``--init`` is the cold start every develop skill runs once per invocation: it persists the session sentinels instead of
printing the path, so each skill replaces a four-line resolve-and-echo block with one call plus a warm read.

* ``dev-shared-<CSID>`` — the resolved path every later block warm-reads.
* ``dev-agent-watch-dir-<CSID>`` — a fresh ``<cwd>/.temp/develop/agent-watch-<UTC time>`` per run, so a prior run's
  deadline batches never report as stale (``agent-resolution.md`` §Agent waits).
* Nothing on stdout. A failed sentinel write still exits 0 (later blocks use their fallbacks); an untrusted working
  directory exits 1 before any sentinel is written.

Tier cascade
------------
* **Tier 1** — Cache semver scan under
  ``~/.claude/plugins/cache/borda-ai-rig/<plugin>/*/skills/_shared``,
  skipping versions marked ``.orphaned_at``.
* **Tier 2** — Source-tree fallback ``plugins/cc_develop/skills/_shared`` relative to the working directory (with
  stderr warning). The CLI uses it only when the working directory carries
  ``plugins/cc_develop/.claude-plugin/plugin.json`` whose ``name`` is ``develop`` — this plugin's own source
  repository. Anywhere else that tree is the text of whatever repository the session was opened in, and skills ``cat``
  protocol files from it, so the CLI fails closed instead.

<!-- file: dev_shared_resolve.py — consumers: dev-shared-resolve.sh -->
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from dev_run_dir import write_sentinel  # sibling bin/ script; atomic, link-safe sentinel writes

#: Marketplace directory name under the Claude plugin cache in which installed plugin versions are searched.
_MARKETPLACE = "borda-ai-rig"
#: Path, relative to a plugin root, of the shared-docs directory this resolver locates.
_SHARED_SUBDIR = "skills/_shared"
#: This plugin's manifest ``name``; the source-repository marker must carry exactly this value.
_PLUGIN = "develop"
#: Working-directory-relative source-tree path the resolver falls back to when no install is cached.
SOURCE_FALLBACK = (Path("plugins") / f"cc_{_PLUGIN}" / _SHARED_SUBDIR).as_posix()


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
    Tier 2: source-tree fallback ``plugins/cc_develop/skills/_shared``, returned unchecked. Only the CLI gates it on
    :func:`source_repo_marker_present`; an in-process caller reading files from the returned path must apply that
    gate itself.

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
    return _locate(home)[0]


def _locate(home: Path | None) -> tuple[str, bool]:
    """Resolve the shared path and report whether it came from the installed cache.

    Args:
        home: Override for ``Path.home()`` (testing).

    Returns:
        ``(path, from_cache)``; ``from_cache`` is ``False`` for the working-directory source fallback.
    """
    home = home if home is not None else Path.home()
    dev_path = _resolve_plugin_shared("develop", home)
    if dev_path is not None:
        return dev_path, True
    return SOURCE_FALLBACK, False


def source_repo_marker_present(cwd: Path | None = None) -> bool:
    """Report whether the working directory is this plugin's own source repository.

    The source-tree tier reads ``plugins/cc_develop/skills/_shared`` relative to the working directory. Outside this
    plugin's source repository that tree belongs to whatever repository the session was opened in, so it is trusted
    only when the same directory also carries ``plugins/cc_develop/.claude-plugin/plugin.json`` naming ``develop``.

    Args:
        cwd: Directory to inspect; defaults to ``Path.cwd()``.

    Returns:
        ``True`` when the manifest exists, parses as a JSON object, and its ``name`` is exactly ``develop``.

    Examples:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as d:
        ...     manifest = Path(d) / "plugins" / "cc_develop" / ".claude-plugin" / "plugin.json"
        ...     before = source_repo_marker_present(Path(d))
        ...     manifest.parent.mkdir(parents=True)
        ...     _ = manifest.write_text('{"name": "develop"}', encoding="utf-8")
        ...     (before, source_repo_marker_present(Path(d)))
        (False, True)
    """
    manifest = (cwd if cwd is not None else Path.cwd()) / "plugins" / f"cc_{_PLUGIN}" / ".claude-plugin" / "plugin.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get("name") == _PLUGIN


def persist_run_sentinels(shared: str, cwd: Path | None = None, now: datetime | None = None) -> bool:
    """Write the cold-start sentinels: the resolved shared path and a fresh agent-watch directory for this run.

    Args:
        shared: Resolved ``skills/_shared`` path every later block warm-reads.
        cwd: Directory the watch dir hangs under; defaults to ``Path.cwd()``.
        now: Clock override (testing); defaults to the current UTC time.

    Returns:
        ``True`` when both sentinels were written.

    Examples:
        >>> import os, tempfile
        >>> from unittest import mock
        >>> env = {"CSID": "doc"}
        >>> with tempfile.TemporaryDirectory() as scratch, mock.patch.dict(os.environ, {**env, "TMPDIR": scratch}):
        ...     stamp = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        ...     ok = persist_run_sentinels("shared", cwd=Path("/work"), now=stamp)
        ...     (ok, (Path(scratch) / "dev-agent-watch-dir-doc").read_text().strip())
        (True, '/work/.temp/develop/agent-watch-2026-01-02T03-04-05Z')
    """
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H-%M-%SZ")
    watch_dir = f"{(cwd or Path.cwd()).as_posix()}/.temp/develop/agent-watch-{stamp}"
    written = (write_sentinel("dev-shared", shared), write_sentinel("dev-agent-watch-dir", watch_dir))
    return all(path is not None for path in written)


def main(argv: list[str] | None = None) -> int:
    """Resolve the shared document path; print it, or with ``--init`` persist the run's cold-start sentinels.

    Returns 1 without output when only the working-directory tier is available and the working directory is not the
    develop source repository; 0 otherwise.
    """
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    parser = argparse.ArgumentParser(
        prog="dev_shared_resolve",
        description="Resolve the develop plugin's own skills/_shared path.",
    )
    parser.add_argument(
        "--init",
        action="store_true",
        help="persist dev-shared and a fresh dev-agent-watch-dir sentinel instead of printing the path",
    )
    args = parser.parse_args(argv)
    path, from_cache = _locate(None)
    if not from_cache:
        # Skills `cat` protocol files from this directory; in a repository under analysis it is that repository's text.
        if not source_repo_marker_present():
            print(
                f"! dev_shared_resolve: no installed develop plugin, and the working directory is not the develop "
                f"plugin source repository (no plugins/cc_{_PLUGIN}/.claude-plugin/plugin.json naming {_PLUGIN}) — "
                f"refusing to read {path} from it; install develop@borda-ai-rig",
                file=sys.stderr,
            )
            return 1
        print(
            f"dev_shared_resolve: no installed develop plugin; using {path} from the working directory, "
            "the develop plugin source repository (local dev only)",
            file=sys.stderr,
        )
    if args.init:
        if not persist_run_sentinels(path):
            print(
                "dev_shared_resolve: could not write the session sentinels; later blocks use their fallbacks",
                file=sys.stderr,
            )
        return 0
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
