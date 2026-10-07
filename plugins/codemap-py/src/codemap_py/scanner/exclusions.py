"""Decide which paths a scan walks, from built-in, config and ignore-file rules."""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path

# Built-in directory names pruned from every scan. Never project source, but can hold
# worktree copies of the whole repo (.claude/, .codex/) that would otherwise inflate the
# index and create qualname collisions, plus the usual build/cache/venv dirs.
#: Directory names always pruned from a scan, such as VCS, virtualenv, build and cache directories.
SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".tox",
    "dist",
    "build",
    ".eggs",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "htmlcov",
    ".claude",
    ".codex",
    ".experiments",
    ".temp",
    ".developments",
    ".cache",
    ".plans",
    ".reports",
    ".notes",
    ".reference",
    "site",
    "_site",
}


# Glob metacharacters — an exclusion entry containing any of these (or a "/") is
# treated as a path glob matched against the posix relpath; otherwise it is a bare
# directory name pruned during the walk (like SKIP_DIRS).
#: Matches glob metacharacters or a slash, marking an exclusion entry as a path glob rather than a directory name.
_GLOB_META_RE = re.compile(r"[*?\[\]/]")


@dataclass(frozen=True)
class Exclusions:
    """User-configurable index exclusions layered on top of :data:`SKIP_DIRS`.

    ``dirs`` are bare directory names pruned during ``os.walk`` (like ``SKIP_DIRS``).
    ``globs`` are ``fnmatch`` patterns tested against each file's posix path relative
    to the project root. ``sources`` records where each entry came from for meta output.

    Args:
        dirs: extra directory names to prune.
        globs: glob patterns to skip individual files.
        sources: mapping of raw entry → origin label (``"pyproject.toml"`` / ``".codemapignore"``).
    """

    dirs: frozenset[str]
    globs: tuple[str, ...]
    sources: dict[str, str]


def _parse_codemap_exclude_toml(text: str) -> list[str]:
    """Extract the ``[tool.codemap] exclude = [...]`` string array from pyproject text.

    Uses a targeted regex rather than a TOML parser: ``tomllib`` is 3.11+ and this
    project runs on 3.10, and the existing ``detect_src_root`` config reader already
    relies on regex TOML matching. Handles single-line and multi-line array forms.

    Args:
        text: full contents of a ``pyproject.toml`` file.

    Returns:
        List of raw exclude entries (empty if the section or key is absent).

    Examples:
        >>> _parse_codemap_exclude_toml('[tool.codemap]\\nexclude = ["a", "b/*"]\\n')
        ['a', 'b/*']
        >>> _parse_codemap_exclude_toml('[tool.other]\\nexclude = ["x"]\\n')
        []
    """
    section = re.search(r"^\[tool\.codemap\](.*?)(?=^\[|\Z)", text, re.MULTILINE | re.DOTALL)
    if not section:
        return []
    m = re.search(r"exclude\s*=\s*\[(.*?)\]", section.group(1), re.DOTALL)
    if not m:
        return []
    return re.findall(r'["\']([^"\']+)["\']', m.group(1))


def _parse_codemap_src_roots_toml(text: str) -> list[str]:
    """Extract the ``[tool.codemap] src_roots = [...]`` string array from pyproject text.

    Uses the same targeted-regex strategy as :func:`_parse_codemap_exclude_toml`
    (``tomllib`` is 3.11+; this project runs on 3.10). Handles single-line and
    multi-line array forms. Declaration order is preserved — the array order is the
    source-root priority applied by module naming and collision resolution.

    Args:
        text: full contents of a ``pyproject.toml`` file.

    Returns:
        List of raw source-root entries in declaration order (empty if the section
        or key is absent).

    Examples:
        >>> _parse_codemap_src_roots_toml('[tool.codemap]\\nsrc_roots = ["a/src", "b/src"]\\n')
        ['a/src', 'b/src']
        >>> _parse_codemap_src_roots_toml('[tool.other]\\nsrc_roots = ["x"]\\n')
        []
    """
    section = re.search(r"^\[tool\.codemap\](.*?)(?=^\[|\Z)", text, re.MULTILINE | re.DOTALL)
    if not section:
        return []
    m = re.search(r"src_roots\s*=\s*\[(.*?)\]", section.group(1), re.DOTALL)
    if not m:
        return []
    return re.findall(r'["\']([^"\']+)["\']', m.group(1))


def load_src_roots(root: Path) -> list[Path]:
    """Load explicit source roots from ``pyproject.toml`` ``[tool.codemap] src_roots``.

    Joins each declared entry to *root* (without ``resolve()`` — kept in the same
    unresolved path space as the ``os.walk`` file paths so relative-path derivation
    never trips over ``/var`` → ``/private/var`` symlink canonicalisation on macOS),
    keeping only entries that exist as directories, in declaration order — which is the
    priority order applied by module naming and collision resolution (earlier entries
    win). Duplicate roots (by relative posix form) are dropped, preserving the first
    occurrence. Returns an empty list when the key is absent, so callers fall back to
    single-root ``detect_src_root`` detection with no behaviour change.

    Args:
        root: project root to read ``pyproject.toml`` from.

    Returns:
        List of existing source-root directories under *root*, in priority order.
    """
    pyproject = root / "pyproject.toml"
    if not pyproject.exists():
        return []
    entries = _parse_codemap_src_roots_toml(pyproject.read_text(errors="replace"))
    roots: list[Path] = []
    seen: set[str] = set()
    for entry in entries:
        candidate = root / entry
        try:
            rel = candidate.relative_to(root).as_posix()
        except ValueError:
            continue  # entry escapes the project root (e.g. "../x") — ignore
        if rel and rel not in seen and candidate.is_dir():
            seen.add(rel)
            roots.append(candidate)
    return roots


def _parse_codemapignore(text: str) -> list[str]:
    """Extract patterns from a ``.codemapignore`` file (one per line, ``#`` comments).

    Args:
        text: full contents of a ``.codemapignore`` file.

    Returns:
        List of non-empty, non-comment patterns with surrounding whitespace stripped.

    Examples:
        >>> _parse_codemapignore("# comment\\nvendored/\\n\\n  foo.py  \\n")
        ['vendored', 'foo.py']
    """
    entries = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip().rstrip("/")
        if line:
            entries.append(line)
    return entries


def _load_exclusions(root: Path) -> Exclusions:
    """Load extra dir-name and glob exclusions from pyproject.toml and .codemapignore.

    Bare names (no ``/`` or glob metacharacter) become pruned directory names; anything
    with a path separator or glob character becomes an ``fnmatch`` pattern.

    Args:
        root: project root to read config from.

    Returns:
        :class:`Exclusions` combining both config sources (empty when neither exists).
    """
    dirs: set[str] = set()
    globs: list[str] = []
    sources: dict[str, str] = {}

    def _ingest(entries: list[str], origin: str) -> None:
        for entry in entries:
            sources.setdefault(entry, origin)
            if _GLOB_META_RE.search(entry):
                globs.append(entry)
            else:
                dirs.add(entry)

    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        _ingest(_parse_codemap_exclude_toml(pyproject.read_text(errors="replace")), "pyproject.toml")
    ignore = root / ".codemapignore"
    if ignore.exists():
        _ingest(_parse_codemapignore(ignore.read_text(errors="replace")), ".codemapignore")

    return Exclusions(dirs=frozenset(dirs), globs=tuple(dict.fromkeys(globs)), sources=sources)


def _match_exclusion(rel_posix: str, exclusions: Exclusions) -> str | None:
    """Return the exclusion entry that excludes *rel_posix*, or ``None``.

    Matches a bare dir-name entry if it appears as any path component, or a glob entry
    via ``fnmatch``. Used to keep git-tracked hashes consistent with the walked module
    list so an excluded path never appears in the index.

    Args:
        rel_posix: file path relative to root, posix separators.
        exclusions: loaded exclusions.

    Returns:
        The matching raw entry, or ``None`` if not excluded.

    Examples:
        >>> ex = Exclusions(frozenset({"vendor"}), ("gen/*.py",), {})
        >>> _match_exclusion("a/vendor/b.py", ex)
        'vendor'
        >>> _match_exclusion("gen/x.py", ex)
        'gen/*.py'
        >>> _match_exclusion("src/app.py", ex) is None
        True
    """
    parts = set(rel_posix.split("/")[:-1])
    hit = parts & exclusions.dirs
    if hit:
        return next(iter(hit))
    return next((g for g in exclusions.globs if fnmatch.fnmatch(rel_posix, g)), None)


def is_excluded(rel_posix: str, exclusions: Exclusions) -> bool:
    """Return True if *rel_posix* is excluded by SKIP_DIRS or by *exclusions*.

    Combines the built-in directory prune list with the user config so a single call
    answers "would scan-index have dropped this tracked file?" — used by scan-query's
    staleness diff to filter git-tracked paths the same way the index was filtered.

    Args:
        rel_posix: file path relative to root, posix separators.
        exclusions: user-configured exclusions from :func:`_load_exclusions`.

    Returns:
        True when any path component is a built-in SKIP_DIR, or the path matches a
        user dir-name / glob exclusion.

    Examples:
        >>> ex = Exclusions(frozenset({"vendor"}), (), {})
        >>> is_excluded(".claude/worktrees/x/pkg/a.py", ex)
        True
        >>> is_excluded("vendor/lib.py", ex)
        True
        >>> is_excluded(".sandbox/proj/src/app.py", ex)
        True
        >>> is_excluded("src/app.py", ex)
        False
    """
    parts = rel_posix.split("/")[:-1]
    # Any dot-directory component prunes: dot-dirs are never part of a project's
    # import space, but they can hold whole vendored checkouts — the 2026-07 usage
    # audit found a `.sandbox/` tree contributing 646 of 928 indexed modules and
    # dominating centrality. Must mirror the scan-index walk prune exactly, or
    # excluded files reappear in the staleness diff as permanently "added".
    if any(part.startswith(".") for part in parts):
        return True
    if SKIP_DIRS.intersection(parts):
        return True
    return _match_exclusion(rel_posix, exclusions) is not None


# One writer-owned contract for every tracked path that can change index content. Query
# imports this value; the prompt hook mirrors it locally because importing the scanner on
# every prompt would pull in the full scan engine. Its contract test pins that mirror.
#: Git pathspec of every tracked file type that can change index content.
INDEXED_PATHSPEC: tuple[str, ...] = ("*.py", "*.pyi", "*.rst", "docs/**/*.md")
