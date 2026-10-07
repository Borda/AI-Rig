"""Codemap binary discovery and index-path resolution for benchmark runners."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


def find_codemap_bin(name: str, plugin_root: Path | None = None) -> Path | None:
    """Locate a codemap CLI binary (scan-query / scan-index) on PATH or in the plugin dir.

    Checks ``PATH`` first via :func:`shutil.which`. Falls back to
    ``<plugin_root>/plugins/codemap-py/bin/<name>`` when ``plugin_root`` is given.

    Args:
        name: Binary name to locate (e.g. ``"scan-query"`` or ``"scan-index"``).
        plugin_root: Root of the plugin repository; checked only when the binary is not on PATH.

    Returns:
        Resolved :class:`~pathlib.Path` to the binary, or ``None`` when not found.

    Examples:
        >>> find_codemap_bin("codemap-bin-that-does-not-exist-xyz", None) is None
        True
    """
    which = shutil.which(name)
    if which:
        return Path(which)
    if plugin_root:
        candidate = plugin_root / "plugins" / "codemap-py" / "bin" / name
        if candidate.exists():
            return candidate
    return None


def codemap_bin_on_path(env: dict[str, str], plugin_root: Path) -> dict[str, str]:
    """Prepend one explicit, scope-locked Codemap plugin ``bin/`` to ``PATH``.

    Plugin ``bin/`` dirs are not reliably added to PATH in ``claude -p`` mode, so runners
    inject the benchmark fixture explicitly. Never discover a mutable user-cache version:
    doing so would let the process execute bytes absent from the paid scope hash. Mutates
    and returns the same dict for convenient chaining.

    Args:
        env: Environment mapping to augment (typically ``os.environ.copy()``).
        plugin_root: Locked ``plugins/codemap-py`` directory supplied by the runner.

    Returns:
        The same ``env`` dict with the locked ``bin/`` directory prepended.

    Raises:
        FileNotFoundError: If the locked ``codemap-py`` launcher is missing.

    Examples:
        >>> import pathlib, tempfile
        >>> root = pathlib.Path(tempfile.mkdtemp()).resolve()  # macOS TMPDIR is a symlink
        >>> (root / "bin").mkdir()
        >>> _ = (root / "bin" / "codemap-py").write_text("")
        >>> codemap_bin_on_path({"PATH": "/usr/bin"}, root)["PATH"].startswith(str(root / "bin"))
        True
    """
    plugin_root = plugin_root.resolve()
    launcher = plugin_root / "bin" / "codemap-py"
    if not launcher.is_file():
        raise FileNotFoundError(f"locked Codemap launcher not found: {launcher}")
    env["PATH"] = str(launcher.parent) + os.pathsep + env.get("PATH", "")
    return env


def resolve_index_path(
    repo_path: Path,
    explicit: str | Path | None = None,
    *,
    strip_suffixes: bool = True,
    missing: str = "bare",
    require_explicit_file: bool = False,
) -> Path:
    """Resolve the codemap index path under ``<repo>/.cache/{codemap,scan}/``.

    Search order is dir-major: ``.cache/codemap/`` then ``.cache/scan/``; within each,
    ``<repo_name>.json`` (plus ``-master``/``-main``-stripped stems when ``strip_suffixes``),
    then the lexicographically first ``*.json``. Callers pick the miss/explicit contract:

    - ``missing="bare"``: return a constructed (possibly non-existent) path and leave found
      paths unresolved (the "may not be built yet" flow).
    - ``missing="raise"``: ``.resolve()`` found paths and raise ``FileNotFoundError`` on a miss.
    - ``require_explicit_file``: when an explicit path is given, ``.resolve()`` it and raise if
      it is not an existing file.

    Args:
        repo_path: Root of the repository being benchmarked.
        explicit: Caller-supplied index path (e.g. ``--index-path``); short-circuits discovery.
        strip_suffixes: Also try ``-master``/``-main``-stripped stems (disable for exact-name only).
        missing: ``"bare"`` (return computed fallback) or ``"raise"`` (raise on miss).
        require_explicit_file: Validate that an explicit path exists as a file (raises otherwise).

    Returns:
        Path to the index file. Resolved when ``missing="raise"``; unresolved when ``"bare"``.

    Raises:
        FileNotFoundError: When ``missing="raise"`` and nothing is found, or when
            ``require_explicit_file`` is set and the explicit path is not a file.

    Examples:
        >>> import pathlib, tempfile
        >>> resolve_index_path(pathlib.Path(tempfile.gettempdir())).suffix
        '.json'
    """
    raise_mode = missing == "raise"
    if explicit is not None:
        p = Path(explicit)
        if raise_mode:
            p = p.resolve()
        if require_explicit_file and not p.is_file():
            raise FileNotFoundError(f"Explicit index not found: {p}")
        return p

    stems = [repo_path.name]
    if strip_suffixes:
        stems += [repo_path.name.replace("-master", ""), repo_path.name.replace("-main", "")]
    for cache_name in ("codemap", "scan"):
        d = repo_path / ".cache" / cache_name
        for stem in stems:
            candidate = d / f"{stem}.json"
            if candidate.exists():
                return candidate.resolve() if raise_mode else candidate
        if d.is_dir():
            jsons = sorted(d.glob("*.json"))
            if jsons:
                return jsons[0].resolve() if raise_mode else jsons[0]

    if raise_mode:
        raise FileNotFoundError(
            f"No codemap index found under {repo_path}/.cache/{{codemap,scan}}/.\n"
            "Build it first (one-time, not measured):\n"
            f"  python plugins/codemap-py/bin/scan-index --root {repo_path}"
        )
    bare = repo_path.name.replace("-master", "").replace("-main", "")
    return repo_path / ".cache" / "codemap" / f"{bare}.json"


def git_toplevel() -> Path | None:
    """Return the current git working tree's top-level directory, or ``None``.

    Used to locate the plugin repo root (for ``plugins/codemap-py/bin/`` lookups) when the
    codemap binaries are not on ``PATH``.

    Returns:
        Absolute path to the git top-level, or ``None`` when not in a repo / git unavailable.
    """
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=5)  # noqa: S607 - git/tool resolved via PATH on purpose
    except (subprocess.TimeoutExpired, OSError):
        return None
    return Path(r.stdout.strip()) if r.returncode == 0 else None
