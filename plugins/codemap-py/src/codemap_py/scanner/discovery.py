"""Find the files a scan covers and the roots and hashes that identify them."""

from __future__ import annotations

import fnmatch
import functools
import os
import re
import subprocess
import sys
from pathlib import Path

from codemap_py.schema import EntityType

from .docs_xrefs import _iter_doc_files
from .exclusions import INDEXED_PATHSPEC, SKIP_DIRS, Exclusions, _load_exclusions, _match_exclusion

_TEST_PATH_RE = re.compile(r"(^|/)tests?/|/test_[^/]+\.py$|/[^/]+_test\.py$|/conftest\.py$")


_DOCS_PATH_RE = re.compile(r"(^|/)docs?/")


_EXAMPLES_PATH_RE = re.compile(r"(^|/)examples?/")


def _is_python_source(filename: str) -> bool:
    """Check whether a filename identifies a discoverable Python source.

    ``.pyi`` type stubs join discovery; ``.pyx``/``.pyc`` and other
    ``.py``-prefixed names are excluded because ``str.endswith`` matches the full suffix.

    Examples:
        >>> _is_python_source("mod.py"), _is_python_source("mod.pyi")
        (True, True)
        >>> _is_python_source("mod.pyx"), _is_python_source("mod.pyc")
        (False, False)
    """
    return filename.endswith((".py", ".pyi"))


def _iter_python_files(root: Path, exclusions: Exclusions | None = None) -> tuple[list[Path], dict[str, int]]:
    """Walk root with directory pruning, returning non-symlink .py/.pyi files and exclusion counts.

    Args:
        root: project root to walk.
        exclusions: extra dir-name/glob exclusions layered on :data:`SKIP_DIRS`. When
            ``None``, only the built-in ``SKIP_DIRS`` apply.

    Returns:
        Tuple of ``(files, counts)`` where ``counts`` maps each exclusion entry that
        pruned at least one path to the number of ``.py`` files it removed.
    """
    exclusions = exclusions or Exclusions(frozenset(), (), {})
    skip_dirs = SKIP_DIRS | exclusions.dirs
    counts: dict[str, int] = {}
    result: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        pruned = [d for d in dirnames if d in exclusions.dirs]
        for d in pruned:
            counts[d] = counts.get(d, 0) + _count_py_files(Path(dirpath) / d)
        # Dot-dirs prune generically (mirrors _exclusions.is_excluded): never part of
        # the import space, and can hold vendored checkouts that dominate the index.
        dirnames[:] = [d for d in dirnames if d not in skip_dirs and not d.startswith(".")]
        for fn in filenames:
            if not _is_python_source(fn):
                continue
            fp = Path(dirpath) / fn
            if fp.is_symlink():
                continue
            rel = fp.relative_to(root).as_posix()
            matched = next((g for g in exclusions.globs if fnmatch.fnmatch(rel, g)), None)
            if matched is not None:
                counts[matched] = counts.get(matched, 0) + 1
                continue
            result.append(fp)
    return result, counts


def _count_py_files(directory: Path) -> int:
    """Count non-symlink ``.py``/``.pyi`` files beneath *directory* (for pruned-dir exclusion stats).

    Args:
        directory: directory being pruned from the walk.

    Returns:
        Number of Python source files under it, or 0 if it cannot be traversed.
    """
    total = 0
    for _dirpath, _dirnames, filenames in os.walk(directory):
        total += sum(1 for fn in filenames if _is_python_source(fn))
    return total


def _classify_entity(rel_path: Path, name: str) -> tuple[EntityType, str]:
    """Return ``(entity_type, package)`` for a module.

    ``package`` is the top-level component of the dotted module name (first segment).

    Args:
        rel_path: module path relative to the project root.
        name: fully-qualified dotted module name (e.g. ``"mypackage.sub.mod"``).

    Examples:
        >>> from pathlib import Path
        >>> _classify_entity(Path("tests/test_foo.py"), "tests.test_foo") == (EntityType.TEST, "tests")
        True
        >>> _classify_entity(Path("docs/conf.py"), "docs.conf") == (EntityType.DOCS, "docs")
        True
        >>> _classify_entity(Path("examples/demo.py"), "examples.demo") == (EntityType.EXAMPLE, "examples")
        True
        >>> _classify_entity(Path("mypackage/core.py"), "mypackage.core") == (EntityType.PKG, "mypackage")
        True
        >>> _classify_entity(Path("src/mypackage/core.py"), "src.mypackage.core") == (EntityType.PKG, "mypackage")
        True
    """
    posix = rel_path.as_posix()
    if _TEST_PATH_RE.search(posix):
        entity_type = EntityType.TEST
    elif _DOCS_PATH_RE.search(posix):
        entity_type = EntityType.DOCS
    elif _EXAMPLES_PATH_RE.search(posix):
        entity_type = EntityType.EXAMPLE
    else:
        entity_type = EntityType.PKG
    parts = name.split(".")
    # Strip "src" layout prefix that leaks when detect_src_root falls back to root —
    # mirrors _build_internal_prefix_set which applies the same strip for import matching.
    pkg = parts[1] if parts[0] == "src" and len(parts) > 1 else parts[0]
    return entity_type, pkg


_GIT_TIMEOUT_S = 10  # max seconds to wait for any git subprocess (H78: hung process guard)


_MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB per file — guard against auto-generated files causing OOM


def find_root() -> Path:
    """Return the git repository root, or cwd if not inside a git repo."""
    try:
        root = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=_GIT_TIMEOUT_S,
        ).strip()
        return Path(root)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return Path.cwd()


def get_git_sha(root: Path) -> str | None:
    """Return the current HEAD commit SHA, or None if git is unavailable.

    Args:
        root: project root path used as cwd for the git command.
    """
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
            cwd=str(root),
            timeout=_GIT_TIMEOUT_S,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None


def _git_file_hashes(root: Path, exclusions: Exclusions) -> dict[str, str]:
    """Return git blob SHAs for all tracked ``.py``/``.pyi``/``.rst``/``docs/**/*.md`` files.

    Hashes Python sources (implementation ``.py`` and ``.pyi`` stubs) plus documentation
    files that participate in v4.5 xref scanning. ``.pyi`` joins the hash set so a stub
    edit invalidates the index like any source change. Markdown files outside
    ``docs/`` are excluded — README.md and other top-level notes do not feed mkdocstrings
    autorefs, so adding them would cause spurious incremental rebuilds.

    Args:
        root: repository root used as cwd.
        exclusions: paths matching these are dropped so git-tracked-but-excluded files
            (e.g. a vendored copy named in ``.codemapignore``) never enter the index.
    """
    output = subprocess.check_output(
        ["git", "ls-files", "-s", "-z", "--", *INDEXED_PATHSPEC],
        cwd=str(root),
        stderr=subprocess.DEVNULL,
        timeout=_GIT_TIMEOUT_S,
    )
    hashes: dict[str, str] = {}
    for entry in output.split(b"\0"):
        line = entry.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        tab_idx = line.index("\t")
        rel_path = line[tab_idx + 1 :]
        if _match_exclusion(rel_path, exclusions) is not None:
            continue
        sha = line.split()[1]
        hashes[rel_path] = sha
    return hashes


def _md5_file_hashes(root: Path, exclusions: Exclusions) -> dict[str, str]:
    """Return MD5 digests for ``.py``/``.rst``/``docs/**/*.md`` files (non-git fallback).

    Args:
        root: directory to search recursively.
        exclusions: dir-name/glob exclusions applied to both ``.py`` and doc files.
    """
    import hashlib

    hashes: dict[str, str] = {}
    py_files, _counts = _iter_python_files(root, exclusions)
    for py_file in py_files:
        try:
            hashes[py_file.relative_to(root).as_posix()] = hashlib.md5(py_file.read_bytes()).hexdigest()
        except OSError as exc:
            print(f"[codemap] ⚠ could not hash {py_file}: {exc} — treating as unchanged", file=sys.stderr)
    rst_files, md_files = _iter_doc_files(root)
    for doc_file in rst_files + md_files:
        rel = doc_file.relative_to(root).as_posix()
        if _match_exclusion(rel, exclusions) is not None:
            continue
        try:
            hashes[rel] = hashlib.md5(doc_file.read_bytes()).hexdigest()
        except OSError as exc:
            print(f"[codemap] ⚠ could not hash {doc_file}: {exc} — treating as unchanged", file=sys.stderr)
    return hashes


def get_file_hashes(root: Path, exclusions: Exclusions | None = None) -> dict[str, str]:
    """Git blob SHAs for tracked source/doc files; falls back to MD5 for non-git projects.

    Includes ``.py``, ``.rst``, and ``docs/**/*.md`` so doc-only edits invalidate
    the index and trigger a re-scan of the xref tables.

    Args:
        root: project root path.
        exclusions: dir-name/glob exclusions; excluded paths are omitted so the hash
            set stays consistent with the walked module list. ``None`` = no extra exclusions.
    """
    exclusions = exclusions or Exclusions(frozenset(), (), {})
    try:
        return _git_file_hashes(root, exclusions)
    except Exception:
        return _md5_file_hashes(root, exclusions)


def _detect_src_root_from_config(root: Path) -> Path | None:
    """Strategy 1: read explicit package location from pyproject.toml / setup.cfg.

    Args:
        root: project root to search for config files.
    """
    for config in (root / "pyproject.toml", root / "setup.cfg"):
        if not config.exists():
            continue
        # Regex matches TOML/ini array syntax only; does not handle multi-line or complex TOML
        m = re.search(r"where\s*=\s*\[([^\]]+)\]", config.read_text(errors="replace"))
        if not m:
            continue
        for entry in re.findall(r'["\']([^"\']+)["\']', m.group(1)):
            candidate = root / entry
            if candidate.is_dir():
                return candidate
    return None


def _is_package_dir(directory: Path) -> bool:
    """Check whether a directory is a Python package.

    A stub-only package (``__init__.pyi`` with no ``__init__.py``) is a real package for name resolution, so both
    markers count when detecting source roots.
    """
    return (directory / "__init__.py").exists() or (directory / "__init__.pyi").exists()


_INIT_NAMES = ("__init__.py", "__init__.pyi")


def _detect_src_root_from_init(root: Path) -> Path | None:
    """Strategy 2: find top-level package directories via the __init__ chain (``.py`` or ``.pyi``).

    Prunes while descending, using the same rules as :func:`_iter_python_files`: built-in
    ``SKIP_DIRS``, any dot-directory, and the user's ``[tool.codemap] exclude`` /
    ``.codemapignore`` entries. The previous form paired two unbounded ``rglob`` sweeps with
    a post-hoc ``SKIP_DIRS`` filter, which both cost ~17s on a tree holding vendored
    checkouts and benchmark snapshots, and let an excluded subtree win the election.

    Candidate selection is order-independent: the set is sorted before the ``src`` search
    and the depth fallback breaks ties on the path itself. Iterating the raw set returned a
    different root per ``PYTHONHASHSEED``.

    Args:
        root: project root to search for ``__init__.py``/``__init__.pyi`` files.
    """
    exclusions = _load_exclusions(root)
    skip_dirs = SKIP_DIRS | exclusions.dirs
    source_roots: set[Path] = set()
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs and not d.startswith(".")]
        present = [name for name in _INIT_NAMES if name in filenames]
        if not present:
            continue
        pkg_dir = Path(dirpath)
        rel_dir = pkg_dir.relative_to(root).as_posix()
        if exclusions.globs and all(
            _match_exclusion(f"{rel_dir}/{name}" if rel_dir != "." else name, exclusions) is not None
            for name in present
        ):
            continue
        parent = pkg_dir.parent
        # Path containment, not string prefix: a sibling named like the root (``/a/roots``
        # beside ``/a/root``) passed ``str.startswith`` and entered the election.
        if parent != root and root not in parent.parents:
            continue
        if _is_package_dir(parent):
            continue
        source_roots.add(parent)

    if not source_roots:
        return None
    ordered = sorted(source_roots)
    if len(ordered) == 1:
        return ordered[0]
    srcs = [candidate for candidate in ordered if candidate.name == "src"]
    if srcs:
        # Shallowest wins. Taking the sorted-first `src` instead made the winner depend on
        # the alphabet: a vendored `a/src` beat a top-level `src`, while `zz/src` lost to it.
        return min(srcs, key=lambda p: (len(p.parts), p))
    return max(ordered, key=lambda p: (len(p.parts), p))


@functools.lru_cache(maxsize=4)
def detect_src_root(root: Path) -> Path:
    """Return the effective Python source root.

    Resolution order:
    1. pyproject.toml / setup.cfg  [tool.setuptools.packages.find] where = [...]
    2. __init__.py chain — find directories that ARE packages whose parent is NOT
    3. src/ heuristic — fallback for repos without __init__.py-based packages

    Args:
        root: project root to inspect.
    """
    result = _detect_src_root_from_config(root)
    if result is not None:
        return result
    result = _detect_src_root_from_init(root)
    if result is not None:
        return result
    # Strategy 3: src/ heuristic
    src_dir = root / "src"
    if src_dir.is_dir() and not (src_dir / "__init__.py").exists():
        return src_dir
    return root


def path_to_module(filepath: Path, src_root: Path) -> str:
    """Convert a file path to a dotted module name relative to src_root.

    Args:
        filepath: absolute path to the .py file.
        src_root: source root to make the path relative to.
    """
    rel = filepath.relative_to(src_root)
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _package_src_root(filepath: Path, root: Path) -> Path:
    """Return the parent of the outermost regular package containing ``filepath``.

    A file outside the configured or detected source root can still belong to a regular package. Its dotted name must
    start at that package, not at an arbitrary non-package directory such as ``tests/``. Files outside a regular package
    retain the repository-root fallback used by older indexes.
    """
    package_dir = filepath.parent
    if not _is_package_dir(package_dir):
        return root
    while _is_package_dir(package_dir):
        parent = package_dir.parent
        if parent == root:
            return root
        if root not in parent.parents:
            return root
        package_dir = parent
    return package_dir


def _effective_src_root(filepath: Path, configured_roots: tuple[Path, ...], default_root: Path) -> Path:
    """Return the source root a file's dotted name should be computed relative to.

    In a monorepo, ``[tool.codemap] src_roots`` can list several package roots (e.g.
    ``libs/core/src`` and ``services/api/src``). A file's module name derives from the
    first configured root that contains it — list order is priority, so a file under an
    earlier root is named relative to that root even if a later root would also match.
    When no configured root contains the file (or none are configured), *default_root*
    is returned, preserving the single-root ``detect_src_root`` behaviour unchanged.

    Args:
        filepath: absolute path to the ``.py`` file being named.
        configured_roots: explicit source roots in priority order (may be empty).
        default_root: fallback root when no configured root contains *filepath*
            (typically the ``detect_src_root`` result).

    Returns:
        The source root *filepath*'s dotted name should be relative to.

    Examples:
        >>> from pathlib import Path
        >>> roots = (Path("/repo/libs/core/src"), Path("/repo/services/api/src"))
        >>> _effective_src_root(Path("/repo/libs/core/src/pkg_a/m.py"), roots, Path("/repo")) == Path(
        ...     "/repo/libs/core/src"
        ... )
        True
        >>> _effective_src_root(Path("/repo/other/m.py"), roots, Path("/repo")) == Path("/repo")
        True
    """
    for candidate in configured_roots:
        if filepath == candidate or candidate in filepath.parents:
            return candidate
    return default_root


def count_loc(source: str) -> int:
    """Count non-blank lines in source, including comment-only lines.

    Args:
        source: raw source text of a Python file.

    Examples:
        >>> count_loc('x = 1\\n\\n# comment\\n')
        2
        >>> count_loc('\\n  \\t')
        0
    """
    return sum(1 for line in source.splitlines() if line.strip())


def has_main_guard(source: str) -> bool:
    """Return True if source contains an ``if __name__ == '__main__'`` guard.

    Args:
        source: raw source text of a Python file.

    Examples:
        >>> has_main_guard("if __name__ == '__main__': pass")
        True
        >>> has_main_guard('main()')
        False
    """
    return bool(re.search(r'if\s+__name__\s*==\s*[\'"]__main__[\'"]', source))


def _count_loc_and_main_guard(source: str) -> tuple[int, bool]:
    """Single pass over source lines: returns (loc, has_main_guard)."""
    loc = 0
    has_guard = False
    for line in source.splitlines():
        stripped = line.strip()
        if stripped:
            loc += 1
        if not has_guard and stripped.startswith("if __name__") and "__main__" in stripped:
            has_guard = True
    return loc, has_guard
