#!/usr/bin/env python
"""index_paths.py — canonical project-root resolver and codemap index identity.

Every codemap entrypoint (launchers, skills, both runtimes) resolves the shared
project index through this module so Claude Code, Codex, and direct CLI use on the
same real project root land on one runtime-neutral index. Runtime/session identity
is never part of the resolved path — only the logging subtree is runtime-scoped
(see ``runtime_log``).

``bin/_index_identity.py`` is a compatibility shim that aliases this module in
``sys.modules``, so every public name is reachable unchanged through either
import path.

Resolution rules:

- the canonical root is the git repository root (else the current directory),
  with symlinks/case aliases collapsed so an alias of the same project resolves
  to the same identity;
- the default index is ``<canonical-root>/.cache/codemap/<project>.json`` with a
  sibling ``.index-rw/`` coordination directory;
- ``CODEMAP_INDEX_DIR`` is a product-wide base override (never a runtime override).
  Its target is the flat ``<override>/<project>.json`` — the exact path the index
  writer (:func:`codemap_py.graph.main`) has always published under that variable —
  so the leased path, the written path, the loaded path, and the ``doctor``-reported
  path are one path. A root-keyed ``<override>/<root-key>/<project>.json`` layout was
  resolved here previously while every writer stayed flat; that split meant the gate
  coordinated a file nobody ever read. The flat convention is authoritative;
- ``root_key`` is still the stable, path-free identity of the canonical root (used for
  reporting and correlation) — it is simply no longer a path component;
- the flat convention accepts that two equal-basename projects sharing one override
  directory land on the same ``<project>.json``. That collision is detected rather than
  silently served: an occupant whose stored ``scan_root`` is a different project raises
  an ``index_root_collision`` diagnostic. Give colliding projects separate override
  directories;
- ``CODEMAP_COORDINATION_DIR`` moves only the coordination directory, leaving the
  index where it was resolved. It names that directory itself, not a base holding
  one per project, so it exists for a deployment whose index directory cannot hold
  lock state — a read-only or shared mount, or a sandbox that must grant write to
  the gate without granting it beside the index. One index per override directory:
  two projects pointed at the same one share a registry mutex and serialise against
  each other for no reason;
- ``split_index_roots`` is reported when two environments resolve different index paths.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

_GIT_TIMEOUT_S = 5
INDEX_SUBDIR = Path(".cache", "codemap")
COORDINATION_DIRNAME = ".index-rw"
_CLAUDE_CACHE_GLOB = "borda-ai-rig/codemap-py/*"

INDEX_ROOT_COLLISION = "index_root_collision"
SPLIT_INDEX_ROOTS = "split_index_roots"

_UNSET = object()
COORDINATION_DIR_ENV = "CODEMAP_COORDINATION_DIR"


def coordination_root(index_dir: Path | str, *, override: object = _UNSET) -> Path:
    """Resolve the coordination directory that guards the index in *index_dir*.

    Both the path resolver and the read/write gate call this, so the directory a caller leases is always the directory
    the gate initialises.

    Args:
        index_dir: Directory holding the index whose coordination root is wanted.
        override: Explicit override directory. ``_UNSET`` (default) reads ``CODEMAP_COORDINATION_DIR``; pass ``None``
            to force the sibling layout even when the environment variable is set.

    Returns:
        The override directory when one is given, else ``<index_dir>/.index-rw``.

    Examples:
        >>> coordination_root("/tmp/idx", override=None).name
        '.index-rw'
        >>> coordination_root("/tmp/idx", override="/var/run/codemap-gate").name
        'codemap-gate'
    """
    raw = os.environ.get(COORDINATION_DIR_ENV) if override is _UNSET else override
    if raw:
        return Path(str(raw)).expanduser().resolve()
    return Path(index_dir) / COORDINATION_DIRNAME


@dataclass(frozen=True)
class Diagnostic:
    """A bounded, machine-readable resolver diagnostic.

    Attributes:
        code: Stable diagnostic code (e.g. ``index_root_collision``).
        message: Human-readable one-line summary.
        detail: Structured supporting fields; never contains secrets.
    """

    code: str
    message: str
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class IndexIdentity:
    """Resolved codemap index identity for one canonical project root.

    Attributes:
        project: Canonical-root basename.
        root: Canonical (symlink-collapsed) project root.
        root_key: Full lowercase SHA-256 of the normalized root identity.
        index_dir: Directory holding the resolved index and coordination subtree.
        index_path: Resolved ``<project>.json`` index path.
        coordination_dir: Sibling ``.index-rw/`` directory beside the index, or the ``CODEMAP_COORDINATION_DIR``
            override when one is set.
        override: ``True`` when ``CODEMAP_INDEX_DIR`` selected the base.
        diagnostics: Any diagnostics raised while resolving (e.g. collisions).
    """

    project: str
    root: Path
    root_key: str
    index_dir: Path
    index_path: Path
    coordination_dir: Path
    override: bool
    diagnostics: tuple[Diagnostic, ...]


def _real(path: Path) -> Path:
    """Return *path* with symlinks and ``..`` collapsed (best-effort)."""
    try:
        return path.resolve()
    except OSError:
        return Path(os.path.abspath(path))


def canonical_root(cwd: Path | str | None = None) -> Path:
    """Return the canonical project root for *cwd*.

    The root is the git repository top-level when *cwd* is inside a repository,
    otherwise *cwd* itself; either way the result is symlink-collapsed so an alias
    of the same project resolves to the same identity.

    Args:
        cwd: Working directory to resolve from (defaults to the process CWD).

    Returns:
        Absolute, symlink-collapsed canonical root path.
    """
    work = Path(cwd) if cwd is not None else Path.cwd()
    git = shutil.which("git")
    if git:
        try:
            out = subprocess.run(
                [git, "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                cwd=str(work),
                check=True,
                timeout=_GIT_TIMEOUT_S,
            ).stdout.strip()
            if out:
                return _real(Path(out))
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            pass
    return _real(work)


def normalize_identity(root: Path | str, *, windows: bool | None = None) -> str:
    """Return the normalized string identity used to key an index for *root*.

    On Windows the identity casefolds and normalizes separators so drive-letter
    form and case differences collapse to one identity; on POSIX the resolved
    path string is already the identity.

    Args:
        root: Canonical root path (should already be symlink-collapsed).
        windows: Force Windows normalization; defaults to the host platform.

    Returns:
        The normalized identity string.

    Examples:
        >>> normalize_identity("/Repo/Proj", windows=False)
        '/Repo/Proj'
        >>> normalize_identity("C:/Repo/Proj", windows=True)
        'c:\\\\repo\\\\proj'
    """
    if windows is None:
        windows = os.name == "nt"
    raw = str(root)
    if windows:
        raw = raw.replace("/", "\\").casefold()
    return raw


def root_key(root: Path | str, *, windows: bool | None = None) -> str:
    """Return the full lowercase SHA-256 of the normalized identity of *root*.

    Args:
        root: Canonical root path.
        windows: Force Windows normalization; defaults to the host platform.

    Returns:
        64-character lowercase hex digest, stable and free of any raw path.

    Examples:
        >>> len(root_key("/repo/proj", windows=False))
        64
        >>> root_key("/a", windows=False) == root_key("/a", windows=False)
        True
    """
    identity = normalize_identity(root, windows=windows)
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _read_scan_root(path: Path) -> str | None:
    """Return the ``scan_root`` field stored in *path*, or ``None`` if unreadable."""
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    value = data.get("scan_root") if isinstance(data, dict) else None
    return value if isinstance(value, str) else None


def _diagnose_root_collision(path: Path, root: Path, diagnostics: list[Diagnostic]) -> None:
    """Append an ``index_root_collision`` diagnostic when *path* holds another project's index.

    Under a shared ``CODEMAP_INDEX_DIR`` the flat ``<override>/<project>.json``
    convention means two equal-basename projects target one file. This is the only
    tell: an existing occupant whose stored ``scan_root`` normalizes to a different
    identity than *root* belongs to another project. The diagnostic reports it —
    resolution never rewrites the path, so the caller decides (the CLI surfaces it;
    the fix is a separate override directory per colliding project).

    An occupant with no readable ``scan_root`` (an older index, a partial write) is
    not evidence of a collision and is left alone.
    """
    if not path.is_file():
        return
    stored = _read_scan_root(path)
    if stored is None or normalize_identity(_real(Path(stored))) == normalize_identity(root):
        return
    diagnostics.append(
        Diagnostic(
            INDEX_ROOT_COLLISION,
            "index at the resolved path was built for a different project root",
            {"index_path": str(path), "stored_scan_root": stored, "canonical_root": str(root)},
        )
    )


def resolve_index(
    cwd: Path | str | None = None,
    *,
    root: Path | str | None = None,
    index_dir_override: object = _UNSET,
) -> IndexIdentity:
    """Resolve the canonical codemap index identity.

    Args:
        cwd: Working directory used to derive the canonical root (ignored when
            *root* is given).
        root: Explicit canonical root; when omitted it is derived from *cwd*.
        index_dir_override: Base override path. ``_UNSET`` (default) reads
            ``CODEMAP_INDEX_DIR``; pass ``None`` to force the default layout even
            when the environment variable is set.

    Returns:
        An :class:`IndexIdentity` describing the resolved paths and diagnostics.
    """
    base_root = _real(Path(root)) if root is not None else canonical_root(cwd)
    project = base_root.name
    rk = root_key(base_root)
    override_raw = os.environ.get("CODEMAP_INDEX_DIR") if index_dir_override is _UNSET else index_dir_override
    diagnostics: list[Diagnostic] = []

    if override_raw:
        index_dir = Path(str(override_raw)).expanduser().resolve()
        override = True
    else:
        index_dir = base_root / INDEX_SUBDIR
        override = False

    index_path = index_dir / f"{project}.json"
    if override:
        # Only a shared override directory can collect two projects' indexes under one
        # basename; the default layout is already root-scoped, so it is not probed (this
        # resolver runs on every CLI invocation — no stat/parse on the common path).
        _diagnose_root_collision(index_path, base_root, diagnostics)
    return IndexIdentity(
        project=project,
        root=base_root,
        root_key=rk,
        index_dir=index_dir,
        index_path=index_path,
        coordination_dir=coordination_root(index_dir),
        override=override,
        diagnostics=tuple(diagnostics),
    )


def diagnose_split_index_roots(path_a: Path, path_b: Path) -> Diagnostic | None:
    """Return a ``split_index_roots`` diagnostic when two paths disagree.

    ``codemap-py integrate audit`` uses this to report — never to reconcile —
    when two runtime environments resolve different index paths.

    Args:
        path_a: Index path resolved in the first environment.
        path_b: Index path resolved in the second environment.

    Returns:
        A :class:`Diagnostic` when the paths differ, else ``None``.

    Examples:
        >>> diagnose_split_index_roots(Path("/a/i.json"), Path("/a/i.json")) is None
        True
        >>> diagnose_split_index_roots(Path("/a/i.json"), Path("/b/i.json")).code
        'split_index_roots'
    """
    if path_a == path_b:
        return None
    return Diagnostic(
        SPLIT_INDEX_ROOTS,
        "runtime environments resolve different index paths; not reconciling",
        {"path_a": str(path_a), "path_b": str(path_b)},
    )


def _is_plausible_plugin_dir(path: Path) -> bool:
    """Return whether *path* is a bounded, marker-bearing plugin directory.

    The resolver rejects filesystem roots and home directories before scanning installed plugin candidates, preventing
    an untrusted explicit path from turning migration checks into a broad traversal.
    """
    if not path.is_dir():
        return False
    resolved = _real(path)
    home = _real(Path.home())
    if resolved == resolved.anchor or resolved == home:
        return False
    markers = (
        resolved / "plugin.json",
        resolved / ".claude-plugin" / "plugin.json",
        resolved / "agents",
        resolved / "skills",
    )
    return any(marker.exists() for marker in markers)


def resolve_plugin_root(explicit: str | None) -> Path | None:
    """Resolve a safe codemap-py Claude plugin root from an argument or cache.

    Args:
        explicit: Optional caller-supplied plugin root. Unsafe or implausible
            values fall through to the ordinary installed-cache lookup.

    Returns:
        The resolved newest codemap-py cache entry, or ``None`` when unavailable.
    """
    if explicit:
        candidate = _real(Path(explicit).expanduser())
        if _is_plausible_plugin_dir(candidate):
            return candidate
    cache = Path.home() / ".claude" / "plugins" / "cache"
    candidates = sorted(
        cache.glob(_CLAUDE_CACHE_GLOB),
        key=lambda candidate: candidate.stat().st_mtime if candidate.exists() else 0.0,
        reverse=True,
    )
    return candidates[0] if candidates else None


def derive_cache_root(plugin_root: Path) -> Path:
    """Return the Claude plugin cache root containing *plugin_root*'s identity.

    Args:
        plugin_root: A resolved ``.../<plugin>/<version>`` directory.

    Returns:
        The enclosing cache root, two parent levels above the plugin root.
    """
    return plugin_root.parent.parent


def detect_dual_identity(cache_base: Path | None = None) -> str | None:
    """Detect simultaneous legacy ``codemap`` and current ``codemap-py`` installs.

    Args:
        cache_base: Optional Claude plugin-cache directory. Defaults to the
            current user's cache location.

    Returns:
        ``"dual_plugin_identity"`` when both names are present, otherwise
        ``None``.
    """
    cache = cache_base or Path.home() / ".claude" / "plugins" / "cache"
    legacy_glob = _CLAUDE_CACHE_GLOB.replace("codemap-py", "codemap")
    if any(cache.glob(_CLAUDE_CACHE_GLOB)) and any(cache.glob(legacy_glob)):
        return "dual_plugin_identity"
    return None
