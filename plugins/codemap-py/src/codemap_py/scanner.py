"""codemap_py.scanner — file discovery and single-file AST parsing.

Owns everything needed to turn one project root into a flat list of per-file module
entries: directory walking with exclusion rules (``[tool.codemap] exclude``/
``.codemapignore``/built-in ``SKIP_DIRS``), git/MD5 file hashing, source-root detection,
and the AST extraction that produces one module's ``symbols``/``calls``/``docstrings``/
``mock_patches``/``dynamic_imports``/``sphinx_xrefs``/``subprocess_calls``/``fixtures``
records. Cross-module aggregation (reverse-dependency counts, fixture/coverage/doc-xref
graphs, dedup, the top-level ``scan()``/``incremental_scan()`` pipeline) lives in
:mod:`codemap_py.graph`, which imports the extraction primitives defined here.

``bin/scan-index`` is a thin launcher over :func:`codemap_py.graph.main`;
``bin/_exclusions.py`` is a compatibility shim that aliases this module in
``sys.modules`` so ``scan-query`` (which imports the bare ``_exclusions`` name
from its own ``bin/`` ``sys.path`` insert) reaches this one implementation.

consumers: bin/scan-index (via codemap_py.graph), bin/_exclusions.py shim, bin/scan-query (via the shim)
"""

from __future__ import annotations

import ast
import builtins
import fnmatch
import functools
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from codemap_py.schema import EntityType, Resolution, SymbolType

_STDLIB_MODULES: frozenset[str] = frozenset(sys.stdlib_module_names)  # Python 3.10+; project requires 3.10+


# ── index exclusion rules (shared with the bin/_exclusions.py shim) ────────
#
# Shared between scan-index (writer, via this module) and scan-query (reader, via the
# bin/_exclusions.py shim) — scan-query's staleness diff must apply the SAME rules,
# otherwise a git-tracked-but-excluded .py (e.g. a vendored tree) is re-listed
# unfiltered, shows as "added" against the filtered index file_shas, and forces the
# index permanently stale. Keeping the rules in one module guarantees writer and
# reader never diverge.

# Built-in directory names pruned from every scan. Never project source, but can hold
# worktree copies of the whole repo (.claude/, .codex/) that would otherwise inflate the
# index and create qualname collisions, plus the usual build/cache/venv dirs.
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


@dataclass(frozen=True)
class CallEdge:
    """Single outgoing call edge from a function or method.

    Dataclass (not TypedDict): named construction and attribute access; converted to dict by as_dict() at the
    _parse_file boundary before JSON serialisation.
    """

    target: str  # "pkg.db::fetch_user" (module::symbol) or raw chain if unresolved
    resolution: Resolution

    def as_dict(self) -> dict:
        """Serialise to a plain dict for JSON output."""
        return {"target": self.target, "resolution": self.resolution}


@dataclass(frozen=True)
class Symbol:
    """Extracted symbol (class, function, or method) with call edges.

    Dataclass (not TypedDict): see CallEdge note above.
    """

    name: str
    qualified_name: str
    type: SymbolType
    start_line: int
    end_line: int
    calls: list[CallEdge]
    has_docstring: bool = False  # v4.4 — True when ast.get_docstring returned non-None
    docstring_first_line: str | None = None  # v4.4 — first non-empty line, stripped, ≤80 chars

    def as_dict(self) -> dict:
        """Serialise to a plain dict (including nested CallEdge list) for JSON output."""
        return {
            "name": self.name,
            "qualified_name": self.qualified_name,
            "type": self.type,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "calls": [c.as_dict() for c in self.calls],
            "has_docstring": self.has_docstring,
            "docstring_first_line": self.docstring_first_line,
        }


_DOCSTRING_FIRST_LINE_MAX = 80


def _docstring_fields(
    node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[bool, str | None]:
    """Return ``(has_docstring, docstring_first_line)`` for a class/function/async-function node.

    The first line is the first non-empty stripped line of the docstring, truncated
    to ``_DOCSTRING_FIRST_LINE_MAX`` characters. ``docstring_first_line`` is ``None``
    when the symbol has no docstring, or when every line of the docstring is blank.

    Args:
        node: AST node whose docstring to extract (must be a definition node accepted by ``ast.get_docstring``).

    Examples:
        >>> import ast
        >>> tree = ast.parse('def f():\\n    \"\"\"Hi there.\"\"\"')
        >>> _docstring_fields(tree.body[0])
        (True, 'Hi there.')
        >>> tree = ast.parse("def g():\\n    pass")
        >>> _docstring_fields(tree.body[0])
        (False, None)
    """
    raw = ast.get_docstring(node, clean=False)
    if raw is None:
        return False, None
    first = next((line.strip() for line in raw.splitlines() if line.strip()), "")
    if not first:
        return True, None
    return True, first[:_DOCSTRING_FIRST_LINE_MAX]


# Forms a mock patch can take in a test file.
_MOCK_FORM_DECORATOR = "decorator"
_MOCK_FORM_CALL = "call"
_MOCK_FORM_MOCKER = "mocker"


BUILTINS = frozenset(dir(builtins))

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
# One writer-owned contract for every tracked path that can change index content. Query
# imports this value; the prompt hook mirrors it locally because importing the scanner on
# every prompt would pull in the full scan engine. Its contract test pins that mirror.
INDEXED_PATHSPEC: tuple[str, ...] = ("*.py", "*.pyi", "*.rst", "docs/**/*.md")


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


# ── detect_src_root helpers ─────────────────────────────────────────────────────


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


def extract_imports(tree: ast.Module) -> list[str]:
    """Extract direct import module names from a pre-parsed AST.

    Args:
        tree: parsed AST of the module.
    """
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name)
            continue
        if isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return sorted(imports)


def _extract_string_sequence(value: ast.expr) -> list[str] | None:
    """Return the string elements of a ``List`` or ``Tuple`` literal of string constants.

    Returns ``None`` for any non-literal element (variable, comprehension, call),
    signalling that the sequence cannot be resolved statically.

    Examples:
        >>> import ast
        >>> _extract_string_sequence(ast.parse('["a", "b"]', mode='eval').body)
        ['a', 'b']
        >>> _extract_string_sequence(ast.parse('("x",)', mode='eval').body)
        ['x']
        >>> _extract_string_sequence(ast.parse('[a, b]', mode='eval').body) is None
        True
    """
    if not isinstance(value, (ast.List, ast.Tuple)):
        return None
    out: list[str] = []
    for elt in value.elts:
        if not (isinstance(elt, ast.Constant) and isinstance(elt.value, str)):
            return None
        out.append(elt.value)
    return out


def extract_module_exports(tree: ast.Module) -> list[str] | None:
    """Extract the static value of a module's ``__all__`` assignment.

    Returns ``None`` when:
      * no top-level ``__all__`` assignment is present (no export filter — any
        public symbol may be live)
      * ``__all__`` is computed dynamically (comprehension, function call,
        variable reference) — cannot be statically determined

    Returns ``list[str]`` when ``__all__`` is a ``List``/``Tuple`` of string
    literal constants. Detects three assignment forms:
      * ``__all__ = ["a", "b"]`` — :class:`ast.Assign`
      * ``__all__: list[str] = ["a", "b"]`` — :class:`ast.AnnAssign`
      * ``__all__ += ["a", "b"]`` — :class:`ast.AugAssign`

    Args:
        tree: parsed AST of the module.

    Examples:
        >>> import ast
        >>> extract_module_exports(ast.parse('__all__ = ["foo", "bar"]'))
        ['foo', 'bar']
        >>> extract_module_exports(ast.parse('x = 1')) is None
        True
        >>> extract_module_exports(ast.parse('__all__ = [f"x_{i}" for i in range(3)]')) is None
        True
        >>> extract_module_exports(ast.parse('__all__: list[str] = ("a",)'))
        ['a']
    """
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    result = _extract_string_sequence(node.value)
                    if result is None:
                        print(
                            "[codemap] debug: __all__ has dynamic value — exports treated as unknown",
                            file=sys.stderr,
                        )
                    return result
        elif isinstance(node, ast.AnnAssign):
            target = node.target
            if isinstance(target, ast.Name) and target.id == "__all__" and node.value is not None:
                result = _extract_string_sequence(node.value)
                if result is None:
                    print(
                        "[codemap] debug: __all__ has dynamic value — exports treated as unknown",
                        file=sys.stderr,
                    )
                return result
        elif isinstance(node, ast.AugAssign):
            target = node.target
            if isinstance(target, ast.Name) and target.id == "__all__":
                result = _extract_string_sequence(node.value)
                if result is None:
                    print(
                        "[codemap] debug: __all__ has dynamic augmented value — exports treated as unknown",
                        file=sys.stderr,
                    )
                return result
    return None


# ── build_import_scope helpers ──────────────────────────────────────────────────


def _process_ast_import(node: ast.Import, name_map: dict[str, str], module_map: dict[str, str]) -> None:
    """Populate name_map and module_map from an ``import ...`` statement.

    Args:
        node: the Import AST node to process.
        name_map: mapping from local name to fully-qualified name (mutated in-place).
        module_map: mapping from first component to full module path (mutated in-place).
    """
    for alias in node.names:
        if alias.asname:
            name_map[alias.asname] = alias.name
            continue
        first = alias.name.split(".")[0]
        name_map[first] = first
        if "." in alias.name:
            module_map[first] = alias.name


def _resolve_import_from_base(node: ast.ImportFrom, package: str) -> str:
    """Resolve the fully qualified base module for a relative import statement.

    Args:
        node: the ImportFrom AST node to resolve.
        package: dotted package name of the current module (used for relative imports).
    """
    base = node.module or ""
    if not (node.level and node.level > 0):
        return base
    parts = package.split(".") if package else []
    up = node.level - 1
    anchor = ".".join(parts[: len(parts) - up]) if up < len(parts) else ""
    if anchor and base:
        return f"{anchor}.{base}"
    return anchor or base


def _process_ast_import_from(
    node: ast.ImportFrom,
    package: str,
    name_map: dict[str, str],
    star_imports: list[str],
) -> None:
    """Populate import maps from a relative import statement.

    Args:
        node: the ImportFrom AST node to process.
        package: dotted package name of the current module (used for relative imports).
        name_map: mapping from local name to fully-qualified name (mutated in-place).
        star_imports: list of modules imported via ``*`` (mutated in-place).
    """
    if not node.names:
        return
    base = _resolve_import_from_base(node, package)
    if len(node.names) == 1 and node.names[0].name == "*":
        if base:
            star_imports.append(base)
        return
    for alias in node.names:
        name_map[alias.asname or alias.name] = f"{base}.{alias.name}" if base else alias.name


def build_import_scope(tree: ast.Module, module_name: str) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """Build the import scope for a module.

    Args:
        tree: parsed AST of the module.
        module_name: fully-qualified dotted name of the module being analysed.

    Returns:
        name_map: direct name -> fully qualified name
        module_map: first component -> full module path (for ``import pkg.db`` style)
        star_imports: list of modules imported via ``from X import *``
    """
    name_map: dict[str, str] = {}
    module_map: dict[str, str] = {}
    star_imports: list[str] = []
    package = module_name.rsplit(".", 1)[0] if "." in module_name else ""

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            _process_ast_import(node, name_map, module_map)
        elif isinstance(node, ast.ImportFrom):
            _process_ast_import_from(node, package, name_map, star_imports)

    return name_map, module_map, star_imports


def _extract_imports_and_scope(
    tree: ast.Module, module_name: str, is_package: bool = False
) -> tuple[list[str], list[str], dict[str, str], dict[str, str], list[str]]:
    """Return imports, possible submodules, and the call-resolution scope.

    ``from package import name`` always imports ``package``. It can also bind an
    importable ``package.name`` submodule, but the scanner cannot decide that
    from the statement alone. The candidate list is resolved against indexed
    modules by :func:`codemap_py.graph._resolve_import_submodule_edges`.
    """
    name_map: dict[str, str] = {}
    module_map: dict[str, str] = {}
    star_imports: list[str] = []
    imports: set[str] = set()
    submodule_candidates: set[str] = set()
    package = module_name if is_package else module_name.rsplit(".", 1)[0] if "." in module_name else ""

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name)
            _process_ast_import(node, name_map, module_map)
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_import_from_base(node, package)
            if base:
                imports.add(base)
            for alias in node.names:
                if base and alias.name != "*":
                    submodule_candidates.add(f"{base}.{alias.name}")
            _process_ast_import_from(node, package, name_map, star_imports)

    _drop_top_level_rebindings(tree, name_map, module_map)
    return sorted(imports), sorted(submodule_candidates), name_map, module_map, star_imports


def _drop_top_level_rebindings(tree: ast.Module, name_map: dict[str, str], module_map: dict[str, str]) -> None:
    """Drop direct import names overwritten later in module scope.

    The general import scope intentionally includes function-local imports for call extraction. This narrow post-pass
    only corrects a module-level import that is replaced by a later module-level binding; treating that call as the
    original import would create a false reverse edge.
    """
    imported_at: dict[str, int] = {}
    rebound_at: dict[str, int] = {}
    conditional_names: set[str] = set()

    def _record_targets(target: ast.expr, position: int) -> None:
        if isinstance(target, ast.Name):
            rebound_at[target.id] = position
        elif isinstance(target, (ast.Tuple, ast.List)):
            for item in target.elts:
                _record_targets(item, position)

    def _conditional_bindings(node: ast.AST) -> set[str]:
        """Collect bindings nested under a conditional scope, excluding local bodies."""
        names: set[str] = set()
        pending = list(ast.iter_child_nodes(node))
        while pending:
            current = pending.pop()
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(current.name)
                continue
            if isinstance(current, ast.Import):
                names.update(alias.asname or alias.name.split(".")[0] for alias in current.names)
            elif isinstance(current, ast.ImportFrom):
                names.update(alias.asname or alias.name for alias in current.names if alias.name != "*")
            elif isinstance(current, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = current.targets if isinstance(current, ast.Assign) else [current.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        names.add(target.id)
            elif isinstance(current, (ast.For, ast.AsyncFor)) and isinstance(current.target, ast.Name):
                names.add(current.target.id)
            elif isinstance(current, (ast.With, ast.AsyncWith)):
                names.update(
                    item.optional_vars.id for item in current.items if isinstance(item.optional_vars, ast.Name)
                )
            elif isinstance(current, ast.ExceptHandler) and current.name:
                names.add(current.name)
            pending.extend(ast.iter_child_nodes(current))
        return names

    for position, node in enumerate(tree.body):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_at[alias.asname or alias.name.split(".")[0]] = position
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    imported_at[alias.asname or alias.name] = position
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            rebound_at[node.name] = position
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                _record_targets(target, position)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.With, ast.AsyncWith, ast.Try, ast.If, ast.Match)):
            for name in _conditional_bindings(node):
                rebound_at[name] = position
                conditional_names.add(name)

    for name, imported_position in imported_at.items():
        if rebound_at.get(name, -1) > imported_position:
            name_map.pop(name, None)
            module_map.pop(name, None)
    for name in conditional_names:
        name_map.pop(name, None)
        module_map.pop(name, None)


def _symbol_alias_provenance(
    tree: ast.Module, module_name: str, is_package_init: bool
) -> tuple[dict[str, str], list[dict[str, str]]]:
    """Return static aliases and explicit limits for rejected top-level bindings.

    Only direct module-body ``from ... import name`` statements qualify. Nested
    imports are conditional or function-local, and are deliberately excluded. A
    subsequent direct module binding removes the alias, so a rebound import never
    rewrites a call edge. The graph layer validates chains against the complete
    module set and rejects cycles or module-as-symbol ambiguity.

    Args:
        tree: parsed source module.
        module_name: dotted name assigned to the source module.
        is_package_init: whether the source is a package ``__init__.py``.

    Returns:
        Proven aliases plus rejected ``alias_qname``/``target_qname`` records.
    """
    package = module_name if is_package_init else module_name.rsplit(".", 1)[0] if "." in module_name else ""
    aliases: dict[str, str] = {}
    limitations: set[tuple[str, str, str]] = set()

    def _target_names(target: ast.expr) -> set[str]:
        if isinstance(target, ast.Name):
            return {target.id}
        if isinstance(target, ast.Starred):
            return _target_names(target.value)
        elif isinstance(target, (ast.Tuple, ast.List)):
            return {name for item in target.elts for name in _target_names(item)}
        return set()

    def _reject(names: set[str], reason: str) -> None:
        for local_name in names:
            target = aliases.pop(local_name, None)
            if target is not None:
                limitations.add((f"{module_name}::{local_name}", target, reason))

    def _conditional_bindings(node: ast.AST) -> tuple[set[str], list[tuple[str, str]]]:
        """Collect module bindings/import aliases without entering local bodies."""
        names: set[str] = set()
        imports: list[tuple[str, str]] = []
        pending = list(ast.iter_child_nodes(node))
        while pending:
            current = pending.pop()
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(current.name)
                continue
            if isinstance(current, ast.Import):
                names.update(alias.asname or alias.name.split(".")[0] for alias in current.names)
            elif isinstance(current, ast.ImportFrom):
                names.update(alias.asname or alias.name for alias in current.names if alias.name != "*")
                base = _resolve_import_from_base(current, package)
                if base:
                    imports.extend(
                        (imported.asname or imported.name, f"{base}::{imported.name}")
                        for imported in current.names
                        if imported.name != "*"
                    )
            elif isinstance(current, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = current.targets if isinstance(current, ast.Assign) else [current.target]
                names.update(name for target in targets for name in _target_names(target))
            elif isinstance(current, (ast.For, ast.AsyncFor)):
                names.update(_target_names(current.target))
            elif isinstance(current, (ast.With, ast.AsyncWith)):
                names.update(
                    name for item in current.items if item.optional_vars for name in _target_names(item.optional_vars)
                )
            elif isinstance(current, ast.ExceptHandler) and current.name:
                names.add(current.name)
            pending.extend(ast.iter_child_nodes(current))
        return names, imports

    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            base = _resolve_import_from_base(node, package)
            if not base:
                continue
            for imported in node.names:
                if imported.name != "*":
                    aliases[imported.asname or imported.name] = f"{base}::{imported.name}"
        elif isinstance(node, ast.Import):
            _reject({imported.asname or imported.name.split(".")[0] for imported in node.names}, "top_level_rebinding")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            _reject({node.name}, "top_level_rebinding")
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                _reject(_target_names(target), "top_level_rebinding")
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.With, ast.AsyncWith, ast.Try, ast.If, ast.Match)):
            # Conditional control flow can bind a module name only on some paths.
            # Excluding it is conservative: no alias provenance is better than a
            # false canonical edge.
            names, imports = _conditional_bindings(node)
            _reject(names, "conditional_binding")
            for local_name, target in imports:
                limitations.add((f"{module_name}::{local_name}", target, "conditional_import"))
    return aliases, [
        {"alias_qname": alias_qname, "target_qname": target_qname, "reason": reason}
        for alias_qname, target_qname, reason in sorted(limitations)
    ]


def extract_module_symbol_aliases(tree: ast.Module, module_name: str, is_package_init: bool) -> dict[str, str]:
    """Return statically proven top-level ``local_name -> target_qname`` aliases."""
    return _symbol_alias_provenance(tree, module_name, is_package_init)[0]


def extract_module_symbol_alias_limitations(
    tree: ast.Module, module_name: str, is_package_init: bool
) -> list[dict[str, str]]:
    """Return target-specific evidence for rejected static alias paths."""
    return _symbol_alias_provenance(tree, module_name, is_package_init)[1]


def resolve_call_chain(func_node: ast.expr) -> str | None:
    """Reconstruct a dotted name from an ast.Call.func node.

    Returns None for unresolvable expressions (subscripts, call-on-call, etc.).

    Args:
        func_node: the ``func`` attribute of an ast.Call node.

    Examples:
        >>> resolve_call_chain(ast.parse('pkg.mod.run()').body[0].value.func)
        'pkg.mod.run'
        >>> resolve_call_chain(ast.parse('factory()()').body[0].value.func) is None
        True
        >>> resolve_call_chain(ast.parse('items[0]()').body[0].value.func) is None
        True
    """
    if isinstance(func_node, ast.Name):
        return func_node.id
    if isinstance(func_node, ast.Attribute):
        base = resolve_call_chain(func_node.value)
        return f"{base}.{func_node.attr}" if base else None
    return None


def resolve_call(
    chain: str,
    name_map: dict[str, str],
    module_map: dict[str, str],
    local_names: set[str],
    current_class: str,
    current_module: str,
    star_imports: list[str] | None = None,
) -> CallEdge:
    """Resolve a call chain to a CallEdge with target and resolution kind.

    Resolution order:
    1. Exact match in name_map -> "import"
    2. Prefix match in name_map -> "import"
    3. Prefix match in module_map -> "import"
    4. First component in local_names -> "local"
    5. Starts with "self." or current_class prefix -> "self"
    6. First component in star_imports -> "star"
    7. First component in BUILTINS -> "builtin"
    8. Everything else -> "unresolved"

    Args:
        chain: dotted call chain string (e.g. ``"pkg.db.fetch_user"``).
        name_map: local-name -> fully-qualified-name from build_import_scope.
        module_map: first-component -> full module path from build_import_scope.
        local_names: top-level function/class names defined in the current file.
        current_class: name of the enclosing class, or empty string.
        current_module: dotted module name of the file being analysed.
    """
    first_component = chain.split(".")[0]

    # 1. Exact match in name_map (handles `from pkg.db import fetch_user; fetch_user()`)
    if chain in name_map:
        fqn = name_map[chain]
        if "." in fqn:
            mod, sym = fqn.rsplit(".", 1)
            return CallEdge(target=f"{mod}::{sym}", resolution=Resolution.IMPORT)
        return CallEdge(target=chain, resolution=Resolution.UNRESOLVED)

    # Also handle dotted chains where the first component is in name_map
    if first_component in name_map and first_component != chain:
        fqn_base = name_map[first_component]
        rest = chain[len(first_component) + 1 :]
        mod, sym = f"{fqn_base}.{rest}".rsplit(".", 1)
        return CallEdge(target=f"{mod}::{sym}", resolution=Resolution.IMPORT)

    # 2. Prefix match in module_map (handles `import pkg.db; pkg.db.fetch_user()`)
    if first_component in module_map:
        full_module = module_map[first_component]
        rest = chain[len(first_component) + 1 :] if len(chain) > len(first_component) else ""
        full_chain = f"{full_module}.{rest}" if rest else full_module
        if "." in full_chain:
            mod, sym = full_chain.rsplit(".", 1)
            return CallEdge(target=f"{mod}::{sym}", resolution=Resolution.IMPORT)
        return CallEdge(target=chain, resolution=Resolution.UNRESOLVED)

    # 3. Local names (functions/classes defined in the same file)
    if first_component in local_names:
        return CallEdge(target=f"{current_module}::{chain}", resolution=Resolution.LOCAL)

    # 4. Self / class reference
    if chain.startswith("self."):
        method_attr = chain[5:]  # strip "self."
        if current_class:
            return CallEdge(
                target=f"{current_module}::{current_class}.{method_attr}",
                resolution=Resolution.SELF,
            )
        return CallEdge(target=chain, resolution=Resolution.SELF)
    if current_class and chain.startswith(f"{current_class}."):
        return CallEdge(target=chain, resolution=Resolution.SELF)

    # 5. Star import — name may come from a star-imported module
    if star_imports:
        return CallEdge(target=chain, resolution=Resolution.STAR)

    # 6. Builtins
    if first_component in BUILTINS:
        return CallEdge(target=chain, resolution=Resolution.BUILTIN)

    # 7. Unresolved
    return CallEdge(target=chain, resolution=Resolution.UNRESOLVED)


# ── extract_symbols helpers ─────────────────────────────────────────────────────


def _walk_calls(
    ast_node: ast.AST,
    name_map: dict[str, str],
    module_map: dict[str, str],
    local_names: set[str],
    current_class: str,
    module_name: str,
    star_imports: list[str] | None = None,
) -> list[CallEdge]:
    """Walk an AST node and return all non-builtin call edges.

    Args:
        ast_node: root node to walk (function body, decorator, class statement, etc.).
        name_map: local-name -> fully-qualified-name from build_import_scope.
        module_map: first-component -> full module path from build_import_scope.
        local_names: top-level names defined in the current file.
        current_class: enclosing class name for self-resolution, or empty string.
        module_name: dotted module name of the file being analysed.
        star_imports: modules imported via ``from X import *``, or None.
    """
    calls: list[CallEdge] = []
    for child in ast.walk(ast_node):
        if not isinstance(child, ast.Call):
            continue
        chain = resolve_call_chain(child.func)
        if not chain:
            continue
        edge = resolve_call(chain, name_map, module_map, local_names, current_class, module_name, star_imports)
        if edge.resolution != Resolution.BUILTIN:
            calls.append(edge)
    return calls


def _extract_class_symbol(
    node: ast.ClassDef,
    name_map: dict[str, str],
    module_map: dict[str, str],
    local_names: set[str],
    module_name: str,
    star_imports: list[str] | None = None,
) -> list[Symbol]:
    """Return the class Symbol plus one Symbol per method from a ClassDef node.

    Args:
        node: the ClassDef AST node to extract symbols from.
        name_map: local-name -> fully-qualified-name from build_import_scope.
        module_map: first-component -> full module path from build_import_scope.
        local_names: top-level names defined in the current file.
        module_name: dotted module name of the file being analysed.
        star_imports: modules imported via ``from X import *``, or None.
    """
    class_calls: list[CallEdge] = []
    for decorator in node.decorator_list:
        class_calls.extend(
            _walk_calls(decorator, name_map, module_map, local_names, node.name, module_name, star_imports)
        )
    for child in node.body:
        if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            class_calls.extend(
                _walk_calls(child, name_map, module_map, local_names, node.name, module_name, star_imports)
            )

    class_has_doc, class_doc_first = _docstring_fields(node)
    symbols: list[Symbol] = [
        Symbol(
            name=node.name,
            qualified_name=node.name,
            type=SymbolType.CLASS,
            start_line=node.lineno,
            end_line=node.end_lineno or node.lineno,
            calls=class_calls,
            has_docstring=class_has_doc,
            docstring_first_line=class_doc_first,
        )
    ]
    for child in node.body:
        if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        method_calls = _walk_calls(child, name_map, module_map, local_names, node.name, module_name, star_imports)
        method_has_doc, method_doc_first = _docstring_fields(child)
        symbols.append(
            Symbol(
                name=child.name,
                qualified_name=f"{node.name}.{child.name}",
                type=SymbolType.METHOD,
                start_line=child.lineno,
                end_line=child.end_lineno or child.lineno,
                calls=method_calls,
                has_docstring=method_has_doc,
                docstring_first_line=method_doc_first,
            )
        )
    return symbols


def extract_symbols(
    tree: ast.Module,
    module_name: str = "",
    name_map: dict[str, str] | None = None,
    module_map: dict[str, str] | None = None,
    star_imports: list[str] | None = None,
) -> list[Symbol]:
    """Extract top-level classes, functions, and class methods with line ranges and call edges.

    Args:
        tree: parsed AST of the module.
        module_name: dotted module name used for local-resolution targets.
        name_map: pre-built import name map; computed fresh if omitted.
        module_map: pre-built module map; computed fresh if omitted.
        star_imports: modules imported via ``from X import *``, or None.
    """
    nm = name_map or {}
    mm = module_map or {}
    local_names: set[str] = {
        node.name for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }

    symbols: list[Symbol] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            symbols.extend(_extract_class_symbol(node, nm, mm, local_names, module_name, star_imports))
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func_calls = _walk_calls(node, nm, mm, local_names, "", module_name, star_imports)
            func_has_doc, func_doc_first = _docstring_fields(node)
            symbols.append(
                Symbol(
                    name=node.name,
                    qualified_name=node.name,
                    type=SymbolType.FUNCTION,
                    start_line=node.lineno,
                    end_line=node.end_lineno or node.lineno,
                    calls=func_calls,
                    has_docstring=func_has_doc,
                    docstring_first_line=func_doc_first,
                )
            )
    return symbols


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


def extract_dynamic_imports(tree: ast.Module) -> list[dict]:
    """Extract string-literal dynamic import paths from AST.

    Covers ``importlib.import_module("X")``, ``pkgutil.import_module("X")``,
    and ``__import__("X")``. Only string-constant first arguments are captured —
    non-literal expressions (e.g. ``importlib.import_module(name)``) are skipped.

    Args:
        tree: parsed AST of a Python module.

    Returns:
        List of ``{"literal": str, "line": int}`` dicts, one per match.

    Examples:
        >>> import ast
        >>> src = 'import importlib\\nimportlib.import_module("my.pkg")'
        >>> extract_dynamic_imports(ast.parse(src))
        [{'literal': 'my.pkg', 'line': 2}]
        >>> extract_dynamic_imports(ast.parse("__import__('os.path')"))
        [{'literal': 'os.path', 'line': 1}]
        >>> extract_dynamic_imports(ast.parse("importlib.import_module(name)"))
        []
    """
    results: list[dict] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        arg0 = node.args[0] if node.args else None
        if arg0 is None or not (isinstance(arg0, ast.Constant) and isinstance(arg0.value, str)):
            continue
        # importlib.import_module("X") or pkgutil.import_module("X")
        if isinstance(func, ast.Attribute) and func.attr == "import_module":
            results.append({"literal": arg0.value, "line": node.lineno})
        # __import__("X")
        elif isinstance(func, ast.Name) and func.id == "__import__":
            results.append({"literal": arg0.value, "line": node.lineno})
    return results


def _normalize_patch_target(dotted: str) -> str | None:
    """Normalize a dotted patch string to ``module::symbol`` form.

    Heuristic: if the second-to-last component starts with an uppercase letter,
    treat it as a class (``module::ClassName.attr``); otherwise treat the last
    component as a free function or class at module level (``module::name``).

    Returns ``None`` when the input lacks any dotted structure (cannot be split
    into module + attribute).

    Examples:
        >>> _normalize_patch_target("mypackage.core.my_func")
        'mypackage.core::my_func'
        >>> _normalize_patch_target("mypackage.core.MyClass.method")
        'mypackage.core::MyClass.method'
        >>> _normalize_patch_target("mypackage.core.MyClass")
        'mypackage.core::MyClass'
        >>> _normalize_patch_target("singletoken") is None
        True
    """
    parts = dotted.split(".")
    if len(parts) < 2:
        return None
    # Detect "module.Class.method" — penultimate component starts uppercase ⇒ class.
    if len(parts) >= 3 and parts[-2][:1].isupper():
        module = ".".join(parts[:-2])
        attr = ".".join(parts[-2:])
    else:
        module = ".".join(parts[:-1])
        attr = parts[-1]
    return f"{module}::{attr}"


def _patch_string_arg(node: ast.Call) -> str | None:
    """Return the first argument's string value if it is an ``ast.Constant`` of type str."""
    if not node.args:
        return None
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return None


def _is_patch_call(call: ast.Call) -> str | None:
    """Identify ``patch(...)``, ``mock.patch(...)``, or ``mocker.patch(...)`` calls.

    Returns the form label (``"mocker"`` when the receiver is ``mocker``, ``"call"`` otherwise) when *call* invokes
    ``patch`` with a string target, or ``None`` otherwise.
    """
    func = call.func
    # mocker.patch('...') or mock.patch('...')
    if isinstance(func, ast.Attribute) and func.attr == "patch":
        if isinstance(func.value, ast.Name):
            if func.value.id == "mocker":
                return _MOCK_FORM_MOCKER
            # mock.patch('...') — fallthrough to generic call form
            return _MOCK_FORM_CALL
        return _MOCK_FORM_CALL
    # bare patch('...')
    if isinstance(func, ast.Name) and func.id == "patch":
        return _MOCK_FORM_CALL
    return None


def _is_patch_object_call(call: ast.Call) -> bool:
    """Check whether a call invokes a supported object-patching function."""
    func = call.func
    if not isinstance(func, ast.Attribute) or func.attr != "object":
        return False
    inner = func.value
    if isinstance(inner, ast.Name) and inner.id == "patch":
        return True
    if isinstance(inner, ast.Attribute) and inner.attr == "patch":
        return True
    return False


def _resolve_patch_object(call: ast.Call, name_map: dict[str, str]) -> str | None:
    """Resolve a ``patch.object(mod, 'attr')`` call into a ``module::attr`` key.

    Returns ``None`` when the first argument is not a simple ``Name`` resolvable
    via *name_map*, or when the second argument is not a string constant.
    """
    if len(call.args) < 2:
        return None
    target_mod_node = call.args[0]
    attr_node = call.args[1]
    if not (isinstance(attr_node, ast.Constant) and isinstance(attr_node.value, str)):
        return None
    if isinstance(target_mod_node, ast.Name):
        resolved = name_map.get(target_mod_node.id, target_mod_node.id)
        return f"{resolved}::{attr_node.value}"
    if isinstance(target_mod_node, ast.Attribute):
        chain = resolve_call_chain(target_mod_node)
        if not chain:
            return None
        first = chain.split(".")[0]
        if first in name_map:
            resolved_base = name_map[first]
            rest = chain[len(first) + 1 :]
            full_mod = f"{resolved_base}.{rest}" if rest else resolved_base
            return f"{full_mod}::{attr_node.value}"
    return None


def extract_mock_patches(tree: ast.Module, filepath: Path) -> list[dict]:
    """Extract every ``patch(...)`` and ``patch.object(...)`` target in a test module.

    Detects four forms (decorator-string, decorator ``patch.object``, in-body call,
    and ``mocker.patch`` pytest-mock idiom) and normalises each to
    ``{"target": "module::symbol", "file": str(filepath), "line": int, "form": str}``.

    Unresolvable strings (no dots, or ``patch.object`` with non-Name module) are
    logged to stderr and skipped — never raise.

    Args:
        tree: parsed AST of the module.
        filepath: filesystem path of the test module (recorded in each entry).

    Returns:
        List of dicts, one per detected patch site, in source order.

    Examples:
        >>> import ast, pathlib
        >>> src = "from unittest.mock import patch\\n@patch('pkg.x.fn')\\ndef test_a():\\n    pass\\n"
        >>> result = extract_mock_patches(ast.parse(src), pathlib.Path("test_a.py"))
        >>> result[0]["target"]
        'pkg.x::fn'
        >>> result[0]["form"]
        'decorator'
    """
    # Build a name_map for resolving patch.object module references.
    name_map: dict[str, str] = {}
    module_map: dict[str, str] = {}
    star_imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            _process_ast_import(node, name_map, module_map)
        elif isinstance(node, ast.ImportFrom):
            _process_ast_import_from(node, "", name_map, star_imports)

    results: list[dict] = []
    seen: set[tuple[str, int, str]] = set()  # (target, line, form) for de-dup
    file_str = str(filepath)

    def _emit(target_key: str, line: int, form: str) -> None:
        sig = (target_key, line, form)
        if sig in seen:
            return
        seen.add(sig)
        results.append({"target": target_key, "file": file_str, "line": line, "form": form})

    def _handle_string_patch(call: ast.Call, form: str) -> None:
        raw = _patch_string_arg(call)
        if raw is None:
            return
        target_key = _normalize_patch_target(raw)
        if target_key is None:
            print(
                f"[codemap] ⚠ mock patch with unresolvable target '{raw}' at {file_str}:{call.lineno} — skipped",
                file=sys.stderr,
            )
            return
        _emit(target_key, call.lineno, form)

    def _handle_patch_object(call: ast.Call, form: str) -> None:
        target_key = _resolve_patch_object(call, name_map)
        if target_key is None:
            print(
                f"[codemap] ⚠ patch.object with unresolvable module at {file_str}:{call.lineno} — skipped",
                file=sys.stderr,
            )
            return
        _emit(target_key, call.lineno, form)

    def _handle_call_node(call: ast.Call, in_decorator: bool) -> None:
        form_label = _MOCK_FORM_DECORATOR if in_decorator else None
        if _is_patch_object_call(call):
            _handle_patch_object(call, form_label or _MOCK_FORM_CALL)
            return
        call_form = _is_patch_call(call)
        if call_form is None:
            return
        # Decorator wins over call-form label; mocker.patch keeps mocker form.
        if in_decorator and call_form == _MOCK_FORM_CALL:
            effective = _MOCK_FORM_DECORATOR
        else:
            effective = call_form
        _handle_string_patch(call, effective)

    # Walk decorators of every function/async-function/class definition.
    decorator_call_ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for deco in node.decorator_list:
                # Decorator can be a Name (no args) or a Call.
                if isinstance(deco, ast.Call):
                    decorator_call_ids.add(id(deco))
                    _handle_call_node(deco, in_decorator=True)

    # Walk all remaining ast.Call nodes for in-body forms.
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and id(node) not in decorator_call_ids:
            _handle_call_node(node, in_decorator=False)

    return results


_CONFIG_SCAN_PATTERNS = ("pyproject.toml", "setup.cfg", "setup.py", "*.yml", "*.yaml")
# Match dotted names with ≥1 dot — simple module path heuristic (no single-word false positives).
_DOTTED_NAME_RE = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]*)(?:\.[a-zA-Z_][a-zA-Z0-9_]+)+\b")

# ── v4.5 Sphinx / MkDocs cross-reference scanning ───────────────────────────────

# Sphinx role markup like :func:`mypackage.fn`, :class:`~pkg.MyCls`, :meth:`pkg.Cls.m`
_SPHINX_XREF_RE = re.compile(r":(?P<role>[a-z]+):`(?P<target>[^`]+)`")

# Roles whose targets we resolve into the symbol-index ``module::name`` form.
# Stored as a frozenset for fast membership tests in the hot doc-scanning loop.
_SPHINX_RESOLVABLE_ROLES: frozenset[str] = frozenset({"func", "class", "meth", "mod", "attr", "data", "exc"})

# MkDocs autorefs: [text][identifier] — identifier is a dotted Python path.
_MKDOCS_NAMED_RE = re.compile(r"\[(?:[^\]]+)\]\[([A-Za-z_][A-Za-z0-9_.]*)\]")
# MkDocs autorefs backtick form: [`identifier`][]
_MKDOCS_BACKTICK_RE = re.compile(r"\[`([A-Za-z_][A-Za-z0-9_.]*)`\]\[\]")


def _resolve_xref_target(role: str, raw_target: str, current_module: str) -> str | None:
    """Resolve a Sphinx role target string to a ``module::name`` symbol key.

    Strips Sphinx prefix markers (``~`` shows short label, ``!`` suppresses link)
    and dispatches on *role* to derive the canonical symbol-index key.

    Resolution rules per role:
      * ``func`` / ``class`` / ``exc`` / ``attr`` / ``data`` — ``a.b.c`` →
        ``a.b::c``. Bare names (no dot) — assume current module: ``current::name``.
      * ``meth`` — ``a.b.Cls.method`` → ``a.b::Cls.method``. The class component
        is kept after the ``::`` separator.
      * ``mod`` — module-level reference; stored as bare module name (no ``::``).

    Leading ``.`` in *raw_target* signals a relative reference: it is resolved
    against the package containing *current_module*.

    Args:
        role: Sphinx role name (lowercase, e.g. ``"func"``, ``"class"``).
        raw_target: target string captured from the role markup, possibly prefixed
            with ``~`` or ``!`` and possibly relative (leading dot).
        current_module: dotted name of the module whose docstring contains the
            reference; used as the resolution anchor for bare names and relative
            references.

    Returns:
        Canonical symbol key (``module::name`` or bare module), or ``None`` when
        *role* is not in :data:`_SPHINX_RESOLVABLE_ROLES` or *raw_target* is empty.

    Examples:
        >>> _resolve_xref_target("func", "pkg.mod.fn", "other")
        'pkg.mod::fn'
        >>> _resolve_xref_target("func", "~pkg.mod.fn", "other")
        'pkg.mod::fn'
        >>> _resolve_xref_target("meth", "pkg.mod.Cls.method", "other")
        'pkg.mod::Cls.method'
        >>> _resolve_xref_target("mod", "pkg.sub", "other")
        'pkg.sub'
        >>> _resolve_xref_target("func", "local_fn", "pkg.mod")
        'pkg.mod::local_fn'
        >>> _resolve_xref_target("unknown", "x", "y") is None
        True
    """
    if role not in _SPHINX_RESOLVABLE_ROLES:
        return None
    target = raw_target.strip()
    if not target:
        return None
    # Strip Sphinx prefix markers before processing.
    if target[:1] in ("~", "!"):
        target = target[1:]
    if not target:
        return None
    # Relative references: leading "." anchors against current module's package.
    if target.startswith("."):
        stripped = target.lstrip(".")
        package = current_module.rsplit(".", 1)[0] if "." in current_module else current_module
        target = f"{package}.{stripped}" if stripped else package
        if not target:
            return None

    if role == "mod":
        return target

    if role == "meth":
        # module.ClassName.method → module::ClassName.method
        parts = target.split(".")
        if len(parts) >= 3:
            module_part = ".".join(parts[:-2])
            attr_part = ".".join(parts[-2:])
            return f"{module_part}::{attr_part}"
        if len(parts) == 2:
            # Bare ClassName.method — anchor against current module.
            return f"{current_module}::{target}" if current_module else target
        # Single component — anchor against current module.
        return f"{current_module}::{target}" if current_module else target

    # func / class / exc / attr / data — dotted path → module::name
    if "." in target:
        module_part, name_part = target.rsplit(".", 1)
        return f"{module_part}::{name_part}"
    return f"{current_module}::{target}" if current_module else target


def _docstring_nodes(tree: ast.Module) -> list[tuple[ast.AST, int]]:
    """Yield ``(node, base_line)`` for every node whose docstring should be scanned.

    Walks the AST and surfaces the module itself plus every class, function, and
    async-function node. ``base_line`` is the line at which the node's docstring
    starts (``node.body[0].lineno`` when the first statement is a constant string).

    Returns:
        List of ``(node, base_line)`` for each docstring-bearing node.
    """
    results: list[tuple[ast.AST, int]] = []
    # Module-level docstring
    if tree.body and isinstance(tree.body[0], ast.Expr) and isinstance(tree.body[0].value, ast.Constant):
        if isinstance(tree.body[0].value.value, str):
            results.append((tree, tree.body[0].lineno))
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    results.append((node, body[0].lineno))
    return results


def extract_sphinx_xrefs(tree: ast.Module, filepath: Path, root: Path, current_module: str) -> list[dict]:
    """Extract Sphinx cross-reference roles from every docstring in *tree*.

    Walks module-, class-, function-, and async-function-level docstrings and
    matches :data:`_SPHINX_XREF_RE` against their text. Each match is normalised
    via :func:`_resolve_xref_target` to a ``module::name`` symbol-index key.

    Line numbers are approximated: the matched role is reported at the docstring's
    base line (start of the triple-quoted string). Per-line offsets are not tracked
    because AST docstring nodes only expose the opening literal's ``lineno``.

    Args:
        tree: parsed AST of the module.
        filepath: filesystem path of the source file (recorded in each entry).
        root: project root used to store a portable relative file path.
        current_module: dotted name of the module (used to anchor bare names).

    Returns:
        List of ``{"role", "target", "file", "line", "source"}`` dicts in
        document order. ``source`` is always ``"sphinx"``.
    """
    results: list[dict] = []
    file_str = filepath.relative_to(root).as_posix()
    for node, base_line in _docstring_nodes(tree):
        doc = ast.get_docstring(node, clean=False)
        if not doc:
            continue
        for match in _SPHINX_XREF_RE.finditer(doc):
            role = match.group("role")
            raw_target = match.group("target")
            target = _resolve_xref_target(role, raw_target, current_module)
            if target is None:
                continue
            results.append(
                {
                    "role": role,
                    "target": target,
                    "file": file_str,
                    "line": base_line,
                    "source": "sphinx",
                }
            )
    return results


def scan_rst_xrefs(rst_path: Path, root: Path) -> list[dict]:
    """Scan a reStructuredText file for Sphinx role cross-references.

    Reads the file line by line and matches :data:`_SPHINX_XREF_RE`. The
    "current module" anchor is empty because ``.rst`` files belong to no Python
    module — bare role targets without a dotted prefix are dropped (anchor empty
    ⇒ no resolution).

    Args:
        rst_path: filesystem path to the ``.rst`` file.
        root: project root used to compute the relative path stored in each entry.

    Returns:
        List of ``{"role", "target", "file", "line", "source"}`` dicts.
        ``source`` is always ``"sphinx"``.
    """
    try:
        text = rst_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    try:
        rel = rst_path.relative_to(root).as_posix()
    except ValueError:
        rel = str(rst_path)
    results: list[dict] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for match in _SPHINX_XREF_RE.finditer(line):
            role = match.group("role")
            raw_target = match.group("target")
            target = _resolve_xref_target(role, raw_target, current_module="")
            if target is None:
                continue
            results.append(
                {
                    "role": role,
                    "target": target,
                    "file": rel,
                    "line": lineno,
                    "source": "sphinx",
                }
            )
    return results


def _resolve_mkdocs_identifier(identifier: str) -> str | None:
    """Convert a mkdocstrings identifier to a ``module::name`` symbol key.

    Identifiers without at least one dot are treated as page anchors (not Python
    paths) and discarded. For dotted identifiers, the heuristic mirrors
    :func:`_normalize_patch_target`: if the second-to-last component starts with
    an uppercase letter it is treated as a class (``module::Class.member``);
    otherwise the final component is the attribute (``module::name``).

    Returns:
        Canonical symbol key, or ``None`` for single-token identifiers.

    Examples:
        >>> _resolve_mkdocs_identifier("pkg.mod.fn")
        'pkg.mod::fn'
        >>> _resolve_mkdocs_identifier("pkg.mod.Cls.method")
        'pkg.mod::Cls.method'
        >>> _resolve_mkdocs_identifier("anchor") is None
        True
    """
    if "." not in identifier:
        return None
    parts = identifier.split(".")
    if len(parts) >= 3 and parts[-2][:1].isupper():
        module_part = ".".join(parts[:-2])
        attr_part = ".".join(parts[-2:])
    else:
        module_part = ".".join(parts[:-1])
        attr_part = parts[-1]
    return f"{module_part}::{attr_part}"


def scan_mkdocs_xrefs(md_path: Path, root: Path) -> list[dict]:
    """Scan a Markdown file for mkdocstrings autorefs cross-references.

    Matches two autorefs forms per line:
      * ``[text][identifier]`` — :data:`_MKDOCS_NAMED_RE`
      * ``[`identifier`][]`` — :data:`_MKDOCS_BACKTICK_RE`

    Identifiers without a dot are page anchors and skipped; dotted identifiers
    are resolved via :func:`_resolve_mkdocs_identifier`.

    Args:
        md_path: filesystem path to the ``.md`` file.
        root: project root used to compute the relative path stored in each entry.

    Returns:
        List of ``{"role", "target", "file", "line", "source"}`` dicts.
        ``role`` is always ``"mkdocs"``; ``source`` is always ``"mkdocs"``.
    """
    try:
        text = md_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    try:
        rel = md_path.relative_to(root).as_posix()
    except ValueError:
        rel = str(md_path)
    results: list[dict] = []
    seen: set[tuple[str, int]] = set()  # (target, line) — backtick form is also matched by named regex
    for lineno, line in enumerate(text.splitlines(), start=1):
        for match in _MKDOCS_BACKTICK_RE.finditer(line):
            identifier = match.group(1)
            target = _resolve_mkdocs_identifier(identifier)
            if target is None:
                continue
            sig = (target, lineno)
            if sig in seen:
                continue
            seen.add(sig)
            results.append(
                {
                    "role": "mkdocs",
                    "target": target,
                    "file": rel,
                    "line": lineno,
                    "source": "mkdocs",
                }
            )
        for match in _MKDOCS_NAMED_RE.finditer(line):
            identifier = match.group(1)
            target = _resolve_mkdocs_identifier(identifier)
            if target is None:
                continue
            sig = (target, lineno)
            if sig in seen:
                continue
            seen.add(sig)
            results.append(
                {
                    "role": "mkdocs",
                    "target": target,
                    "file": rel,
                    "line": lineno,
                    "source": "mkdocs",
                }
            )
    return results


def _iter_doc_files(root: Path) -> tuple[list[Path], list[Path]]:
    """Walk *root* and return ``(.rst files, docs/**/*.md files)``.

    ``.rst`` files anywhere under the tree are returned. Markdown files are
    restricted to ``docs/`` subtrees to avoid pulling in README.md, CHANGELOG.md,
    and other non-API documentation that drives mkdocstrings autorefs.

    Args:
        root: project root to walk.

    Returns:
        Tuple of ``(rst_files, md_files)`` lists.
    """
    rst_files: list[Path] = []
    md_files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        rel_dir = Path(dirpath).relative_to(root).as_posix() if Path(dirpath) != root else ""
        in_docs = rel_dir == "docs" or rel_dir.startswith("docs/")
        for fn in filenames:
            fp = Path(dirpath) / fn
            if fp.is_symlink():
                continue
            if fn.endswith(".rst"):
                rst_files.append(fp)
            elif fn.endswith(".md") and in_docs:
                md_files.append(fp)
    return rst_files, md_files


def scan_config_refs(root: Path, module_names: set[str]) -> dict[str, list[dict]]:
    """Scan config files for string references to known module paths.

    Targets: ``pyproject.toml``, ``setup.cfg``, ``setup.py``, ``*.yml``, ``*.yaml``
    at the project root (non-recursive). Uses line-by-line regex scan — no TOML/YAML
    parser required; produces minor false-positive risk mitigated by exact-match against
    the known module names set.

    Args:
        root: project root path.
        module_names: set of known dotted module names from the index.

    Returns:
        Dict mapping module name → list of ``{"file": str, "line": int, "context": str}``.
    """
    refs: dict[str, list[dict]] = {}
    config_files: list[Path] = []
    for pattern in _CONFIG_SCAN_PATTERNS:
        config_files.extend(root.glob(pattern))
    for cfg_path in sorted(config_files):
        rel = cfg_path.relative_to(root).as_posix()
        try:
            lines = cfg_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for lineno, line in enumerate(lines, start=1):
            for m in _DOTTED_NAME_RE.finditer(line):
                candidate = m.group()
                if candidate in module_names:
                    refs.setdefault(candidate, []).append(
                        {
                            "file": rel,
                            "line": lineno,
                            "context": line.strip()[:120],
                        }
                    )
    return refs


# ── v5.1 conftest.py sys.path awareness ────────────────────────────────────────


def _extract_path_file_parent_dir(arg: ast.expr, conftest_dir: Path) -> Path | None:
    """Resolve ``str(Path(__file__).parent / "name")`` form to an absolute directory.

    Returns ``None`` for any unsupported shape — multi-level ``.parent.parent``,
    ``.resolve()`` chains, ``os.path.join`` forms, or any non-literal RHS of the
    division operator. Only the exact 1-level ``Path(__file__).parent / "name"``
    pattern (with name as a string constant) is supported.

    Args:
        arg: AST node taken from the second positional argument of
            ``sys.path.insert(N, ...)``; expected to be a ``Call`` to ``str``.
        conftest_dir: directory containing the conftest.py being parsed —
            used as the anchor for ``Path(__file__).parent``.
    """
    if not (isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name) and arg.func.id == "str"):
        return None
    if not arg.args:
        return None
    inner = arg.args[0]
    if not (isinstance(inner, ast.BinOp) and isinstance(inner.op, ast.Div)):
        return None
    if not (isinstance(inner.right, ast.Constant) and isinstance(inner.right.value, str)):
        return None
    left = inner.left
    # Require Attribute chain: Path(__file__).parent — left.attr == "parent", left.value is Call to Path(__file__).
    if not (isinstance(left, ast.Attribute) and left.attr == "parent"):
        return None
    base = left.value
    if not (isinstance(base, ast.Call) and isinstance(base.func, ast.Name) and base.func.id == "Path"):
        return None
    if not (base.args and isinstance(base.args[0], ast.Name) and base.args[0].id == "__file__"):
        return None
    return conftest_dir / inner.right.value


def _is_syspath_insert_call(call: ast.Call) -> bool:
    """Check whether a call inserts an entry into the Python import path.

    Two positional args required; receiver must be the literal ``sys.path`` attribute chain. ``sys.path.append`` and
    slice assignment are not detected here (treated as unsupported and skipped silently at the walk level).
    """
    func = call.func
    if not (isinstance(func, ast.Attribute) and func.attr == "insert"):
        return False
    receiver = func.value
    if not (isinstance(receiver, ast.Attribute) and receiver.attr == "path"):
        return False
    inner = receiver.value
    if not (isinstance(inner, ast.Name) and inner.id == "sys"):
        return False
    return len(call.args) >= 2


def extract_conftest_syspath(conftest_path: Path, root: Path) -> list[Path]:
    """Parse a conftest.py AST and return resolved directory paths added to ``sys.path``.

    Supported patterns (static, constant-foldable):

      * ``sys.path.insert(N, "str_literal")`` — resolved relative to
        ``conftest_path.parent``.
      * ``sys.path.insert(N, str(Path(__file__).parent / "name"))`` — resolved
        to ``conftest_path.parent / "name"``.

    Unsupported patterns are skipped with a single ``⚠ conftest:`` warning to
    stderr — ``Path(__file__).parent.parent / ...``, ``os.path.join(...)``,
    variable-then-use, ``sys.path.append``, slice assignment, ``.resolve()``
    chains.

    Args:
        conftest_path: filesystem path to the conftest.py file.
        root: project root (unused today, reserved for future relative-to-root
            anchoring; kept for API stability).

    Returns:
        List of resolved absolute directory paths added to ``sys.path``.
        Empty when the file has no ``sys.path.insert`` calls or all calls use
        unsupported shapes.
    """
    _ = root  # reserved
    try:
        source = conftest_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    try:
        tree = ast.parse(source, filename=str(conftest_path))
    except SyntaxError:
        return []

    conftest_dir = conftest_path.parent
    results: list[Path] = []
    for stmt in tree.body:
        if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)):
            continue
        call = stmt.value
        if not _is_syspath_insert_call(call):
            continue
        arg = call.args[1]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            results.append(conftest_dir / arg.value)
            continue
        resolved = _extract_path_file_parent_dir(arg, conftest_dir)
        if resolved is not None:
            results.append(resolved)
            continue
        print(
            f"⚠ conftest: unsupported sys.path pattern at {conftest_path}:{call.lineno} — skipped",
            file=sys.stderr,
        )
    return results


def _collect_module_aliases(
    conftest_paths: list[Path],
    indexed_names: set[str],
    root: Path,
) -> dict[str, str]:
    """Map bare module names made importable by conftest ``sys.path`` shims to dotted names.

    For each directory resolved via :func:`extract_conftest_syspath`, list the
    Python files directly inside it (non-recursive). For every file, the bare
    name is its stem; the function searches *indexed_names* for any module
    whose final dotted component equals the bare name.

      * Unique match → record ``bare_name -> full_dotted_name``.
      * Multiple matches → ambiguous; skipped with a stderr warning.
      * No match → likely a non-indexed script; skipped silently.

    Args:
        conftest_paths: every conftest.py discovered during the scan.
        indexed_names: dotted names of all modules with ``status == "ok"``.
        root: project root (unused today; kept for API stability with future
            features that may need to resolve relative paths).

    Returns:
        Mapping of bare module name → fully-qualified dotted module name.
    """
    _ = root
    # Index modules by their final dotted component for O(1) lookup.
    by_last: dict[str, list[str]] = {}
    for name in indexed_names:
        last = name.rsplit(".", 1)[-1]
        by_last.setdefault(last, []).append(name)

    aliases: dict[str, str] = {}
    seen_dirs: set[Path] = set()
    for conftest_path in conftest_paths:
        for syspath_dir in extract_conftest_syspath(conftest_path, root):
            try:
                resolved_dir = syspath_dir.resolve()
            except OSError:
                continue
            if resolved_dir in seen_dirs:
                continue
            seen_dirs.add(resolved_dir)
            if not resolved_dir.is_dir():
                continue
            for entry in sorted(resolved_dir.iterdir()):
                if entry.is_symlink() or not entry.is_file() or entry.suffix != ".py":
                    continue
                bare = entry.stem
                if bare == "__init__":
                    continue
                if bare in aliases:
                    continue
                candidates = by_last.get(bare, [])
                if not candidates:
                    continue
                if len(candidates) > 1:
                    print(
                        f"⚠ conftest: ambiguous alias '{bare}' (candidates: {', '.join(candidates)}) — skipped",
                        file=sys.stderr,
                    )
                    continue
                aliases[bare] = candidates[0]
    return aliases


# ── v5.2 subprocess call-edge extraction ───────────────────────────────────────

# Bare interpreter tokens recognised at index 0 of the args list / os.system string.
_SUBPROCESS_PY_TOKENS: frozenset[str] = frozenset({"python", "python3"})


def _resolve_path_file_parent_script(arg: ast.expr, caller_dir: Path) -> Path | None:
    """Resolve ``str(Path(__file__).parent / "name.py")`` to an absolute file path.

    Mirrors :func:`_extract_path_file_parent_dir` but resolves against the
    calling module's directory (``caller_dir``) instead of a conftest dir.
    Returns ``None`` for any unsupported shape.

    Args:
        arg: AST node from the args list element (expected: ``Call`` to ``str``).
        caller_dir: directory containing the file whose AST is being walked —
            anchor for ``Path(__file__).parent``.
    """
    if not (isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name) and arg.func.id == "str"):
        return None
    if not arg.args:
        return None
    inner = arg.args[0]
    if not (isinstance(inner, ast.BinOp) and isinstance(inner.op, ast.Div)):
        return None
    if not (isinstance(inner.right, ast.Constant) and isinstance(inner.right.value, str)):
        return None
    left = inner.left
    if not (isinstance(left, ast.Attribute) and left.attr == "parent"):
        return None
    base = left.value
    if not (isinstance(base, ast.Call) and isinstance(base.func, ast.Name) and base.func.id == "Path"):
        return None
    if not (base.args and isinstance(base.args[0], ast.Name) and base.args[0].id == "__file__"):
        return None
    return caller_dir / inner.right.value


def _is_python_token(node: ast.expr) -> bool:
    """Check whether an expression identifies a supported Python executable."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value in _SUBPROCESS_PY_TOKENS
    if isinstance(node, ast.Attribute) and node.attr == "executable":
        return isinstance(node.value, ast.Name) and node.value.id == "sys"
    return False


def _subprocess_script_arg(args_list: ast.List, caller_dir: Path) -> str | None:
    """Extract the script-path string from a ``subprocess.run/Popen`` args list.

    Recognised shapes for ``args_list.elts[1]`` (after the interpreter token):

      * ``Constant(str)`` — bare script name (e.g. ``"other.py"``).
      * ``Call`` to ``str(Path(__file__).parent / "x.py")`` — 1-level Path form.

    Returns the resolved or raw script path string, or ``None`` when the shape
    is unsupported.
    """
    if len(args_list.elts) < 2:
        return None
    if not _is_python_token(args_list.elts[0]):
        return None
    script_node = args_list.elts[1]
    if isinstance(script_node, ast.Constant) and isinstance(script_node.value, str):
        return script_node.value
    resolved = _resolve_path_file_parent_script(script_node, caller_dir)
    if resolved is not None:
        return str(resolved)
    return None


def _is_subprocess_run_or_popen(call: ast.Call) -> bool:
    """Check whether a call starts a subprocess through a supported API."""
    func = call.func
    if not (isinstance(func, ast.Attribute) and func.attr in ("run", "Popen")):
        return False
    return isinstance(func.value, ast.Name) and func.value.id == "subprocess"


def _is_os_system(call: ast.Call) -> bool:
    """Check whether a call invokes a command through the operating system."""
    func = call.func
    if not (isinstance(func, ast.Attribute) and func.attr == "system"):
        return False
    return isinstance(func.value, ast.Name) and func.value.id == "os"


def _os_system_script(call: ast.Call) -> str | None:
    """Extract the script name from ``os.system("python <script>")``.

    Returns the script token directly following ``python``/``python3`` after a whitespace split, or ``None`` when the
    form is unsupported (non-constant arg, no Python invocation token, no token after the interpreter).
    """
    if not call.args:
        return None
    first = call.args[0]
    if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
        return None
    tokens = first.value.split()
    if len(tokens) < 2 or tokens[0] not in _SUBPROCESS_PY_TOKENS:
        return None
    return tokens[1]


def _resolve_script_to_module(script: str, caller_dir: Path, root: Path, indexed_files: dict[str, str]) -> str | None:
    """Match a resolved script path against indexed module file paths.

    Resolution rules:
      * Absolute path → resolved as-is.
      * Relative path → resolved against ``caller_dir``.
      * Result then matched against the indexed files map (rel-path → module
        name). The map is built by :func:`extract_subprocess_calls`'s caller.

    Args:
        script: script path string captured from the subprocess call.
        caller_dir: directory of the file whose AST is being walked.
        root: project root used to compute relative paths.
        indexed_files: map of POSIX rel-path → dotted module name for every
            ``status == "ok"`` module in the index.

    Returns:
        Dotted module name if matched, ``None`` otherwise.
    """
    script_path = Path(script)
    if not script_path.is_absolute():
        script_path = caller_dir / script_path
    try:
        resolved = script_path.resolve()
    except OSError:
        return None
    try:
        rel = resolved.relative_to(root.resolve()).as_posix()
    except ValueError:
        return None
    return indexed_files.get(rel)


def extract_subprocess_calls(
    tree: ast.Module,
    filepath: Path,
    root: Path,
    indexed_files: dict[str, str] | None = None,
) -> list[dict]:
    """Extract subprocess invocations of other Python scripts in this module.

    Scans the AST for three subprocess forms and produces one entry per call
    that resolves to an indexed module:

      * ``subprocess.run([<py>, <script>, ...])``
      * ``subprocess.Popen([<py>, <script>, ...])``
      * ``os.system("python <script> ...")`` (string form, whitespace-split)

    Recognised ``<py>`` tokens are ``"python"`` / ``"python3"`` string constants
    or ``sys.executable``. Recognised ``<script>`` shapes are bare string
    constants or ``str(Path(__file__).parent / "name.py")`` (1-level Path).

    Out of scope (documented for future expansion): ``runpy.run_path``, the
    ``sh`` library, shell strings without a ``python`` interpreter token,
    multi-level ``Path(__file__).parent.parent`` chains.

    Unresolvable scripts (no matching indexed module) emit a single stderr
    warning and are skipped — never raised.

    Args:
        tree: parsed AST of the module being scanned.
        filepath: filesystem path of the source file (recorded in each entry).
        root: project root used to resolve relative script paths.
        indexed_files: map of POSIX rel-path → dotted module name. When
            ``None``, the function returns ``[]`` because no resolution is
            possible without the module list (callers build this once per
            scan from the parsed module entries).

    Returns:
        List of ``{"target_module": str, "file": str, "line": int}`` dicts, one
        per resolved subprocess call, in source order.

    Examples:
        >>> import ast, pathlib
        >>> src = "import subprocess\\nsubprocess.run(['python', 'x.py'])\\n"
        >>> extract_subprocess_calls(ast.parse(src), pathlib.Path("/tmp/a.py"), pathlib.Path("/tmp"), {}) == []
        True
    """
    if indexed_files is None:
        return []
    caller_dir = filepath.parent
    file_str = str(filepath)
    results: list[dict] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        script: str | None = None
        if _is_subprocess_run_or_popen(node):
            if not node.args:
                continue
            first_arg = node.args[0]
            if not isinstance(first_arg, ast.List):
                continue
            script = _subprocess_script_arg(first_arg, caller_dir)
        elif _is_os_system(node):
            script = _os_system_script(node)
        if script is None:
            continue
        target = _resolve_script_to_module(script, caller_dir, root, indexed_files)
        if target is None:
            print(
                f"⚠ subprocess: unresolvable script {script!r} in {filepath}",
                file=sys.stderr,
            )
            continue
        results.append({"target_module": target, "file": file_str, "line": node.lineno})
    return results


# ── v5.3 pytest fixture dependency graph ───────────────────────────────────────

# Pytest fixtures injected at runtime — no static definition in any conftest.
# Listed so test functions taking these as parameters are not flagged unknown.
_PYTEST_BUILTIN_FIXTURES: frozenset[str] = frozenset(
    {
        "tmp_path",
        "tmp_path_factory",
        "tmpdir",
        "tmpdir_factory",
        "monkeypatch",
        "mocker",
        "caplog",
        "capsys",
        "capfd",
        "capsysbinary",
        "capfdbinary",
        "request",
        "pytestconfig",
        "fixture_union",
        "recwarn",
        "cache",
        "doctest_namespace",
        "record_property",
        "record_xml_attribute",
        "record_testsuite_property",
    }
)


def _is_pytest_fixture_decorator(decorator: ast.expr) -> tuple[bool, str | None]:
    """Identify whether *decorator* marks the function as a ``@pytest.fixture``.

    Returns ``(is_fixture, scope)`` where *scope* is the explicit ``scope=`` keyword
    value (string literal only) or ``None`` when omitted, dynamic, or the decorator
    is not a fixture decorator. ``scope`` resolution defaults to ``"function"``
    upstream in :func:`extract_fixtures` when this function returns ``None``.

    Recognised forms:
      * ``@pytest.fixture`` — ``Attribute(value=Name('pytest'), attr='fixture')``
      * ``@pytest.fixture(scope='session')`` — ``Call`` wrapping the attribute form
      * ``@fixture`` — bare ``Name('fixture')`` (assumes ``from pytest import fixture``)

    Args:
        decorator: AST decorator node from a function definition's ``decorator_list``.

    Examples:
        >>> import ast
        >>> tree = ast.parse("import pytest\\n@pytest.fixture\\ndef f(): pass")
        >>> _is_pytest_fixture_decorator(tree.body[1].decorator_list[0])
        (True, None)
        >>> tree = ast.parse("import pytest\\n@pytest.fixture(scope='session')\\ndef f(): pass")
        >>> _is_pytest_fixture_decorator(tree.body[1].decorator_list[0])
        (True, 'session')
    """
    # Bare @pytest.fixture
    if isinstance(decorator, ast.Attribute) and decorator.attr == "fixture":
        if isinstance(decorator.value, ast.Name) and decorator.value.id == "pytest":
            return True, None
        return False, None
    # Bare @fixture (assumes `from pytest import fixture`)
    if isinstance(decorator, ast.Name) and decorator.id == "fixture":
        return True, None
    # @pytest.fixture(...) or @fixture(...)
    if isinstance(decorator, ast.Call):
        func = decorator.func
        is_fixture = False
        if isinstance(func, ast.Attribute) and func.attr == "fixture":
            if isinstance(func.value, ast.Name) and func.value.id == "pytest":
                is_fixture = True
        elif isinstance(func, ast.Name) and func.id == "fixture":
            is_fixture = True
        if not is_fixture:
            return False, None
        for kw in decorator.keywords:
            if kw.arg == "scope" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                return True, kw.value.value
        return True, None
    return False, None


def _body_yields(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Return True when *node*'s body contains any ``Yield`` or ``YieldFrom`` expression.

    Nested function definitions are not descended into — only yields that belong
    directly to *node* count toward classifying the fixture as a generator.

    Examples:
        >>> import ast
        >>> _body_yields(ast.parse("def f():\\n    yield 1").body[0])
        True
        >>> _body_yields(ast.parse("def f():\\n    return 1").body[0])
        False
    """
    for child in ast.walk(node):
        if child is node:
            continue
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child is not node:
            # Skip nested function definitions; their yields are not this fixture's.
            continue
        if isinstance(child, (ast.Yield, ast.YieldFrom)):
            return True
    return False


def extract_fixtures(tree: ast.Module, filepath: Path) -> list[dict]:
    """Extract every ``@pytest.fixture`` decorated function defined in *tree*.

    Walks the top-level module body for ``FunctionDef`` / ``AsyncFunctionDef``
    nodes carrying a fixture decorator (see :func:`_is_pytest_fixture_decorator`).
    Each match is recorded with ``name``, ``scope`` (string literal from a
    ``scope=`` kwarg, or ``"function"`` when omitted), ``loc`` (the function's
    line number), ``yields`` (whether the body contains ``yield`` / ``yield from``),
    and ``params`` (the fixture's positional / keyword argument names, ``self`` /
    ``cls`` excluded — used downstream by ``fixture-graph`` to walk per-fixture
    dependency trees).

    Non-decorator forms (functions registered via ``pytest.fixture(scope=...)(fn)``
    call syntax instead of decoration) are not recognised — they fall outside
    pytest's discoverable fixture surface in practice.

    Args:
        tree: parsed AST of the module being scanned.
        filepath: filesystem path of the source file (recorded for diagnostics
            but not embedded in the output today).

    Returns:
        List of ``{"name", "scope", "loc", "yields", "params"}`` dicts in source order.

    Examples:
        >>> import ast, pathlib
        >>> src = "import pytest\\n@pytest.fixture\\ndef f(db):\\n    yield 1\\n"
        >>> extract_fixtures(ast.parse(src), pathlib.Path("conftest.py"))
        [{'name': 'f', 'scope': 'function', 'loc': 3, 'yields': True, 'params': ['db']}]
    """
    _ = filepath  # reserved for future diagnostics
    results: list[dict] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        scope: str | None = None
        is_fixture = False
        for decorator in node.decorator_list:
            matched, explicit_scope = _is_pytest_fixture_decorator(decorator)
            if matched:
                is_fixture = True
                if explicit_scope is not None:
                    scope = explicit_scope
                break
        if not is_fixture:
            continue
        results.append(
            {
                "name": node.name,
                "scope": scope or "function",
                "loc": node.lineno,
                "yields": _body_yields(node),
                "params": _function_param_names(node),
            }
        )
    return results


def _function_param_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """Return the positional/keyword parameter names of *node*, excluding ``self`` / ``cls``.

    Skips ``*args``, ``**kwargs``, and positional-only ``/`` marker semantics — pytest
    fixtures are injected through standard positional/keyword args only.

    Examples:
        >>> import ast
        >>> _function_param_names(ast.parse("def f(self, a, b=1, *args): pass").body[0])
        ['a', 'b']
    """
    args = node.args
    names: list[str] = []
    for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs):
        if arg.arg in ("self", "cls"):
            continue
        names.append(arg.arg)
    return names


def extract_fixture_uses(
    tree: ast.Module,
    defined_fixtures: dict[str, dict],
    all_conftest_exports: dict[str, dict],
) -> list[dict]:
    """Extract fixture-name parameters consumed by ``test_*`` and fixture functions in *tree*.

    Walks top-level functions (and methods of top-level classes) and treats each
    non-``self`` / ``cls`` parameter as a candidate fixture name when the function
    is either:

      * named ``test_*`` (a pytest test); or
      * decorated with ``@pytest.fixture`` (a fixture itself, whose parameters
        are fixture dependencies — used by ``fixture-graph`` to walk
        dependency trees in conftest modules).

    Each candidate is resolved against three sources, in this order:

      1. ``defined_fixtures`` — fixtures defined in the same module (local
         conftest/test-file fixtures shadow everything else).
      2. ``all_conftest_exports`` — fixtures exported by any conftest reachable
         in the project, with the closest conftest already resolved by the caller.
      3. :data:`_PYTEST_BUILTIN_FIXTURES` — pytest's runtime-injected fixtures
         (``tmp_path``, ``monkeypatch``, ``mocker``…); emitted with
         ``scope=None`` / ``defined_in=None``.

    Unknown names (plugin fixtures, fixtures defined in non-indexed conftests)
    are emitted with ``scope=None`` / ``defined_in=None`` so the caller can still
    surface them without inventing a source.

    Each fixture name is emitted at most once per module, even when consumed by
    multiple test functions — the caller's interest is reverse-dependency, not
    per-test count.

    Args:
        tree: parsed AST of a test module.
        defined_fixtures: ``name -> {scope, ...}`` map of fixtures defined in the
            same module (typically the test file or a local conftest).
        all_conftest_exports: ``name -> {scope, defined_in}`` map of fixtures
            visible through the conftest hierarchy, deeper-conftest-wins.

    Returns:
        List of ``{"name", "scope", "defined_in"}`` dicts, deduplicated by name,
        ordered alphabetically.

    Examples:
        >>> import ast
        >>> src = "def test_a(tmp_path, my_fix):\\n    pass\\n"
        >>> tree = ast.parse(src)
        >>> uses = extract_fixture_uses(
        ...     tree, {}, {"my_fix": {"scope": "session", "defined_in": "conftest"}}
        ... )
        >>> uses[0]
        {'name': 'my_fix', 'scope': 'session', 'defined_in': 'conftest'}
        >>> uses[1]
        {'name': 'tmp_path', 'scope': None, 'defined_in': None}
    """
    seen: dict[str, dict] = {}

    def _record(name: str) -> None:
        if name in seen:
            return
        if name in defined_fixtures:
            scope = defined_fixtures[name].get("scope", "function")
            seen[name] = {"name": name, "scope": scope, "defined_in": None}
            return
        if name in all_conftest_exports:
            entry = all_conftest_exports[name]
            seen[name] = {
                "name": name,
                "scope": entry.get("scope", "function"),
                "defined_in": entry.get("defined_in"),
            }
            return
        # Builtin or unknown — emit with sentinel nulls so callers can still see usage.
        seen[name] = {"name": name, "scope": None, "defined_in": None}

    def _is_relevant(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
        if fn.name.startswith("test_"):
            return True
        for decorator in fn.decorator_list:
            is_fix, _ = _is_pytest_fixture_decorator(decorator)
            if is_fix:
                return True
        return False

    def _walk_function(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        if not _is_relevant(fn):
            return
        for param in _function_param_names(fn):
            _record(param)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _walk_function(node)
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    _walk_function(child)

    return sorted(seen.values(), key=lambda d: d["name"])


def _parse_file_star(args: tuple[Path, Path, Path]) -> dict:
    """Unpack args tuple and delegate to _parse_file (required for ProcessPoolExecutor.map)."""
    return _parse_file(*args)


def _strip_stub_call_edges(symbol_dicts: list[dict]) -> None:
    """Clear outgoing call edges from a ``.pyi`` stub's symbols.

    A stub declares signatures with ``...`` bodies, so it contributes declarations and
    imports but no executable-body call edges. Empty bodies yield none anyway; this also
    drops any module-level call in a stub (e.g. ``T = TypeVar("T")``).
    """
    for sym in symbol_dicts:
        if sym.get("calls"):
            sym["calls"] = []


def _parse_file(filepath: Path, root: Path, src_root: Path) -> dict:
    """Parse a single .py/.pyi file and return its module entry dict (ok or degraded).

    Args:
        filepath: absolute path to the .py file to parse.
        root: project root used to compute relative paths.
        src_root: source root used to derive the dotted module name.
    """
    rel_path = filepath.relative_to(root)
    try:
        name = path_to_module(filepath, src_root)
    except ValueError:
        name = path_to_module(filepath, _package_src_root(filepath, root))

    file_size = filepath.stat().st_size
    if file_size > _MAX_FILE_SIZE_BYTES:
        print(
            f"[codemap] ⚠ skipping {rel_path}: file too large ({file_size // (1024 * 1024)} MB > "
            f"{_MAX_FILE_SIZE_BYTES // (1024 * 1024)} MB limit)",
            file=sys.stderr,
        )
        return {
            "name": name,
            "path": rel_path.as_posix(),
            "status": "degraded",
            "reason": f"file too large ({file_size} bytes) — skipped to prevent OOM",
        }
    try:
        try:
            source = filepath.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            # errors="replace" would substitute U+FFFD and index corrupted source
            # silently; mark degraded so scan-query surfaces it in degraded_files.
            return {"name": name, "path": rel_path.as_posix(), "status": "degraded", "reason": f"encoding: {exc}"}
        tree = ast.parse(source, filename=str(filepath))
        try:
            imports, submodule_candidates, nm, mm, star_imports = _extract_imports_and_scope(
                tree, name, filepath.name in {"__init__.py", "__init__.pyi"}
            )
        except Exception as exc:
            print(f"[codemap] ⚠ import scope build failed for {rel_path}: {exc}", file=sys.stderr)
            imports = extract_imports(tree)
            submodule_candidates, nm, mm, star_imports = [], {}, {}, []
        symbols = extract_symbols(tree, name, nm, mm, star_imports or None)
        dynamic_imports = extract_dynamic_imports(tree)
        _loc, _is_entry = _count_loc_and_main_guard(source)
        is_test = bool(_TEST_PATH_RE.search(rel_path.as_posix()))
        entity_type, pkg = _classify_entity(rel_path, name)
        sphinx_xrefs = extract_sphinx_xrefs(tree, filepath, root, name)
        exports = extract_module_exports(tree)
        symbol_aliases, symbol_alias_limitations = _symbol_alias_provenance(
            tree, name, filepath.name in {"__init__.py", "__init__.pyi"}
        )
        symbol_dicts = [s.as_dict() for s in symbols]
        if filepath.suffix == ".pyi":
            _strip_stub_call_edges(symbol_dicts)
        entry: dict = {
            "name": name,
            "path": rel_path.as_posix(),
            "loc": _loc,
            "dep_count": len(imports),
            "direct_imports": imports,
            "unresolved_direct_imports": imports,
            "from_import_submodules": submodule_candidates,
            "symbols": symbol_dicts,
            "is_entry_point": _is_entry,
            "is_test": is_test,
            "entity_type": entity_type,
            "package": pkg,
            "has_star_imports": bool(star_imports),
            "exports": exports,
            "symbol_aliases": symbol_aliases,
            "symbol_alias_limitations": symbol_alias_limitations,
            "sphinx_xrefs": sphinx_xrefs,
            "status": "ok",
        }
        if dynamic_imports:
            entry["dynamic_imports"] = dynamic_imports
        if is_test:
            entry["mock_patches"] = extract_mock_patches(tree, filepath)
        return entry
    except SyntaxError as exc:
        return {"name": name, "path": rel_path.as_posix(), "status": "degraded", "reason": f"SyntaxError: {exc}"}
    except Exception as exc:
        return {"name": name, "path": rel_path.as_posix(), "status": "degraded", "reason": str(exc)}
