#!/usr/bin/env python
"""resolve_shared_path.py — canonical plugin shared-dir resolver (Windows-portable).

Resolves a plugin's ``<subdir>`` (typically ``skills/_shared``) via tiered cascade.
Replaces the near-identical bash scripts ``resolve-shared-path.sh`` and
``find-foundry-shared.sh`` shipped per plugin, providing a single pure-Python
implementation usable on Linux, macOS, and Windows alike.

Tier cascade
------------
* **Tier 0** — ``CLAUDE_PLUGIN_ROOT`` env var set AND ``$CLAUDE_PLUGIN_ROOT/<subdir>``
  is a directory → print that path, exit 0. Fastest path: runtime is already
  inside the active plugin install.
* **Tier 1** — Locate ``get_plugin_install_path.py`` (canonical foundry helper),
  invoke it for the requested plugin, and validate that ``<installPath>/<subdir>``
  exists. Helper search order: (a) ``$CLAUDE_PLUGIN_ROOT/bin/...``,
  (b) newest cached foundry version, (c) source-tree dev fallback.
* **Tier 2** — Glob ``~/.claude/plugins/cache/borda-ai-rig/<plugin>/*/<subdir>``,
  skip version dirs carrying ``.orphaned_at``, return newest by semver. Degraded
  fallback only: it runs when the registry has no usable record (helper missing,
  registry absent or malformed, recorded ``installPath`` purged, or the recorded
  install lacks ``<subdir>``) and may then pick a cache dir newer than the one
  Claude Code actually loads.
* **Tier 3** — Source-tree fallback ``plugins/cc_<plugin>/<subdir>``, then
  ``plugins/<plugin>/<subdir>`` for un-prefixed provider dirs such as
  ``codemap-py`` (warn to stderr).

Cross-plugin use
----------------
Consumers normally resolve their **own** plugin. The one sanctioned exception is an
optional-provider contract — content that has no value unless the provider is
installed (``resolve_shared_path.py codemap-py claude-skills/_shared``): the
registry tier returns the *active* install and skips a newer orphaned cache dir;
the newest-cache tier is reached only when no usable registry record exists or the
recorded install lacks ``<subdir>``, and absence degrades to the consumer's own
fallback line. See ``plugins/CLAUDE.md``
§Self-Contained ``_shared``.

Exit codes
----------
* ``0`` — A path was emitted to stdout (any tier).
* ``1`` — All tiers failed: helper missing, no cache hit, and source-tree
  fallback also not present on disk. Plugin genuinely absent.
* ``2`` — Invalid argument (bad PLUGIN or SUBDIR token).

Usage
-----
::

    python resolve_shared_path.py <plugin-name> <subdir>
    python resolve_shared_path.py foundry skills/_shared
    python resolve_shared_path.py oss skills/_shared

<!-- file: resolve_shared_path.py — consumers: codemap-gates.md, codemap-context.md, foundry/oss SKILL.md -->
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

_PLUGIN_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_SUBDIR_RE = re.compile(r"^[a-zA-Z0-9_/-]+$")
_MARKETPLACE = "borda-ai-rig"


def _version_key(name: str) -> list[int]:
    """Extract numeric version components for semver-aware sorting.

    Pure helper replacing ``sort -V`` from bash. Lifts every contiguous
    digit run from the version dir name into an integer list, so
    ``0.20.0`` sorts above ``0.9.9`` (digit-run aware, not lexical).
    Non-numeric segments are dropped entirely.

    Args:
        name: Directory name to key on (e.g. ``"0.20.0"``).

    Returns:
        List of integers extracted from ``name`` in left-to-right order.

    Examples:
        >>> _version_key("0.20.0")
        [0, 20, 0]
        >>> _version_key("0.9.9") < _version_key("0.20.0")
        True
        >>> _version_key("1.2.3rc4")
        [1, 2, 3, 4]
        >>> _version_key("nonsense")
        []
    """
    return [int(t) for t in re.findall(r"\d+", name)]


def _validate_plugin(plugin: str) -> None:
    """Reject plugin names containing path separators or shell metacharacters.

    Args:
        plugin: Plugin short-name to validate.

    Raises:
        ValueError: When ``plugin`` does not match ``^[a-zA-Z0-9_-]+$``.

    Examples:
        >>> _validate_plugin("foundry")
        >>> _validate_plugin("borda-ai-rig")
        >>> _validate_plugin("../evil")
        Traceback (most recent call last):
            ...
        ValueError: invalid PLUGIN: '../evil'
    """
    if not _PLUGIN_RE.match(plugin):
        raise ValueError(f"invalid PLUGIN: {plugin!r}")


def _validate_subdir(subdir: str) -> None:
    """Reject subdir tokens containing ``..`` or unexpected metacharacters.

    Args:
        subdir: Subdirectory path inside the plugin install.

    Raises:
        ValueError: When ``subdir`` fails the regex check or contains ``..``.

    Examples:
        >>> _validate_subdir("skills/_shared")
        >>> _validate_subdir("../etc/passwd")
        Traceback (most recent call last):
            ...
        ValueError: invalid SUBDIR: '../etc/passwd'
        >>> _validate_subdir("skills/../etc")
        Traceback (most recent call last):
            ...
        ValueError: invalid SUBDIR: 'skills/../etc'
    """
    if not _SUBDIR_RE.match(subdir) or ".." in subdir:
        raise ValueError(f"invalid SUBDIR: {subdir!r}")


def _cache_root(home: Path) -> Path:
    """Return the marketplace-scoped cache root under ``$HOME``.

    Args:
        home: User home directory (typically ``Path.home()``).

    Returns:
        Path to ``<home>/.claude/plugins/cache/borda-ai-rig``.

    Examples:
        >>> from pathlib import Path
        >>> _cache_root(Path("/home/x")).as_posix()
        '/home/x/.claude/plugins/cache/borda-ai-rig'
    """
    return home / ".claude" / "plugins" / "cache" / _MARKETPLACE


def _locate_helper(home: Path, env_root: str | None) -> Path | None:
    """Find ``get_plugin_install_path.py`` across known locations.

    Every tier stays inside the plugin this script itself ships in — derived from
    ``__file__``, never a hardcoded sibling. A copy of this resolver living in
    another plugin therefore looks up that plugin's helper, so no plugin depends
    on a sibling being installed (``plugins/CLAUDE.md`` §Self-Contained ``_shared``).

    1. ``$CLAUDE_PLUGIN_ROOT/bin/get_plugin_install_path.py`` when env set.
    2. Newest semver under ``<cache>/<own-plugin>/*/bin/get_plugin_install_path.py``.
    3. Source-tree dev fallback ``plugins/<own-plugin-dir>/bin/…`` (repo-relative).

    Args:
        home: User home directory.
        env_root: Value of ``CLAUDE_PLUGIN_ROOT`` (may be empty/``None``).

    Returns:
        Path to the helper script when found, else ``None``.
    """
    if env_root:
        candidate = Path(env_root) / "bin" / "get_plugin_install_path.py"
        if candidate.is_file():
            return candidate
    own_plugin_dir = Path(__file__).resolve().parent.parent
    own_cache = _cache_root(home) / own_plugin_dir.name.removeprefix("cc_")
    if own_cache.is_dir():
        versions = [
            v for v in own_cache.iterdir() if v.is_dir() and (v / "bin" / "get_plugin_install_path.py").is_file()
        ]
        if versions:
            versions.sort(key=lambda p: _version_key(p.name))
            return versions[-1] / "bin" / "get_plugin_install_path.py"
    # repo-relative on purpose (matches tier-3 shared-path fallback): an absolute
    # path here would always resolve, letting the helper tier win even with no install
    source_fallback = Path("plugins") / own_plugin_dir.name / "bin" / "get_plugin_install_path.py"
    if source_fallback.is_file():
        return source_fallback
    return None


def _tier1_registry(home: Path, plugin: str, subdir: str, env_root: str | None) -> Path | None:
    """Resolve via ``installed_plugins.json`` (registry).

    Invokes ``get_plugin_install_path.py`` as a subprocess under the
    interpreter already running this script (``sys.executable``), looks up
    the plugin's authoritative install path, then validates that
    ``<install_path>/<subdir>`` is a directory. A PATH lookup for ``python``
    would silently miss on ``python3``-only hosts and on the Windows Store
    alias stub, dropping every caller to the newest-cache tier without a
    trace.

    Args:
        home: User home directory.
        plugin: Plugin short-name.
        subdir: Subdir to confirm under the resolved install path.
        env_root: Value of ``CLAUDE_PLUGIN_ROOT`` (may be empty/``None``).

    Returns:
        Resolved path if registry hit AND subdir exists, else ``None``.
    """
    helper = _locate_helper(home, env_root)
    if helper is None:
        return None
    try:
        completed = subprocess.run(  # noqa: S603 — args fully internal
            [sys.executable, str(helper), _MARKETPLACE, plugin],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    install_path = completed.stdout.strip()
    if not install_path:
        return None
    candidate = Path(install_path) / subdir
    if candidate.is_dir():
        return candidate
    return None


def _tier2_cache(home: Path, plugin: str, subdir: str) -> Path | None:
    """Resolve via cache semver scan, skipping orphaned versions.

    Walks ``<cache>/<plugin>/<version>/`` directories, filters out any
    version dir containing ``.orphaned_at``, keeps those whose
    ``<version>/<subdir>`` exists, sorts by semver, returns newest.

    Args:
        home: User home directory.
        plugin: Plugin short-name.
        subdir: Subdir that must exist under the version dir.

    Returns:
        Newest cached path matching ``<plugin>/<version>/<subdir>`` that
        is not orphaned, or ``None`` if no match.
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
        target = version_dir / subdir
        if target.is_dir():
            candidates.append((_version_key(version_dir.name), target))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0])
    return candidates[-1][1]


def resolve(plugin: str, subdir: str, *, home: Path | None = None, env_root: str | None = None) -> tuple[str, int]:
    """Resolve ``<plugin>/<subdir>`` via the four-tier cascade.

    Pure resolution routine — performs no I/O on stdout. Callers (CLI
    or other modules) handle printing and stderr warnings based on the
    returned tier indicator.

    Args:
        plugin: Plugin short-name (already validated).
        subdir: Subdir under the plugin install (already validated).
        home: Override for ``Path.home()`` (testing).
        env_root: Override for ``$CLAUDE_PLUGIN_ROOT`` (testing).

    Returns:
        Tuple ``(path, tier)`` where ``tier`` is the originating tier
        (``0``–``3``) or ``-1`` when even the source-tree fallback
        target does not exist on disk (caller should exit 1).

    Examples:
        >>> import os, tempfile
        >>> from pathlib import Path
        >>> with tempfile.TemporaryDirectory() as d:
        ...     root = Path(d) / "plugin_install"
        ...     (root / "skills" / "_shared").mkdir(parents=True)
        ...     path, tier = resolve(
        ...         "foundry", "skills/_shared",
        ...         home=Path(d), env_root=str(root),
        ...     )
        ...     tier == 0 and path == str(root / "skills" / "_shared")
        True
    """
    home = home if home is not None else Path.home()
    env_root = env_root if env_root is not None else os.environ.get("CLAUDE_PLUGIN_ROOT", "")
    if env_root:
        # Only use env_root when it belongs to the requested plugin.
        # Without this check, an OSS caller requesting 'foundry skills/_shared'
        # would get the OSS _shared dir back when CLAUDE_PLUGIN_ROOT points at OSS.
        env_root_path = Path(env_root)
        plugin_json = env_root_path / ".claude-plugin" / "plugin.json"
        env_plugin = ""
        if plugin_json.is_file():
            import json as _json

            try:
                env_plugin = _json.loads(plugin_json.read_text(encoding="utf-8")).get("name", "")
            except Exception:  # noqa: BLE001
                pass
        # Allow Tier 0 when: no plugin constraint, names match, or plugin.json absent (dev tree).
        if not plugin or not env_plugin or env_plugin.lower() == plugin.lower():
            candidate = env_root_path / subdir
            if candidate.is_dir():
                return str(candidate), 0
    hit = _tier1_registry(home, plugin, subdir, env_root)
    if hit is not None:
        return str(hit), 1
    hit = _tier2_cache(home, plugin, subdir)
    if hit is not None:
        return str(hit), 2
    # Source-tree fallback targets the on-disk folder: cc_-prefixed for the
    # Claude plugins, bare for provider plugins such as codemap-py; `plugin`
    # stays bare for the cache/registry tiers.
    source_fallback = Path("plugins") / f"cc_{plugin}" / subdir
    for candidate in (source_fallback, Path("plugins") / plugin / subdir):
        if candidate.is_dir():
            return candidate.as_posix(), 3
    return source_fallback.as_posix(), -1


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Optional argv override (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code (0 success, 1 absent, 2 argument error).
    """
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    parser = argparse.ArgumentParser(
        prog="resolve_shared_path",
        description="Resolve a plugin's <subdir> via tiered cache + registry cascade.",
    )
    parser.add_argument("plugin", help="Plugin short-name (e.g. foundry, oss).")
    parser.add_argument("subdir", help="Subdir under the plugin install (e.g. skills/_shared).")
    args = parser.parse_args(argv)

    try:
        _validate_plugin(args.plugin)
        _validate_subdir(args.subdir)
    except ValueError as exc:
        print(f"resolve_shared_path: {exc}", file=sys.stderr)
        return 2

    path, tier = resolve(args.plugin, args.subdir)
    if tier == -1:
        print(
            f"resolve_shared_path: {args.plugin} not found in registry, cache, or source tree",
            file=sys.stderr,
        )
        return 1
    if tier == 3:
        print(
            f"resolve_shared_path: {args.plugin}/{args.subdir} not in cache or registry"
            " — using source-tree fallback (local dev only)",
            file=sys.stderr,
        )
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
