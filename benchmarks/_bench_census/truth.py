"""Static import-edge truth: in-repository file-to-file edges, built without executing repository code.

Rules:
    - Universe: every ``.py`` file under the repository root, except files below a build, dist, virtualenv,
      site-packages, cache or VCS directory, and except files below any directory holding a ``pyvenv.cfg``.
    - Search paths: the repository root, plus ``src/`` when it exists; a file outside every package (its directory has
      no ``__init__.py``) also searches its own directory first.
    - Edge target: the module the statement names. ``from a import b`` names ``a.b`` when that is an in-repository
      module and ``a`` otherwise; ``from a import *`` names ``a``.
    - Imports inside functions, ``try`` blocks and ``TYPE_CHECKING`` blocks count; every import node in the file does.
    - Parent packages Python executes on the way (``a/__init__.py`` for ``import a.b``) are not edges.
    - Self-edges are dropped; relative imports in a file outside every package are ignored.

Resolution walks one name component at a time with :class:`importlib.machinery.PathFinder` over explicit search paths,
so no module is imported and ``sys.modules`` is never consulted.
"""

from __future__ import annotations

import ast
import warnings
from dataclasses import dataclass, field
from importlib.machinery import ModuleSpec, PathFinder
from pathlib import Path

__all__ = ["SKIPPED_DIRECTORIES", "TruthEdges", "build_truth", "find_module_spec", "package_parts", "source_universe"]

#: Directory names whose contents are never part of the source universe.
SKIPPED_DIRECTORIES = frozenset(
    {
        ".eggs",
        ".git",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "env",
        "node_modules",
        "site-packages",
        "venv",
    }
)
_VIRTUALENV_MARKER = "pyvenv.cfg"
_PACKAGE_MARKER = "__init__.py"
_NON_FILE_ORIGINS = frozenset({"namespace", "built-in", "frozen"})


@dataclass(frozen=True)
class TruthEdges:
    """Truth edge set of one repository.

    Attributes:
        sources: Repository-relative POSIX paths of every file in the source universe, sorted.
        edges: ``(source, target)`` pairs of repository-relative POSIX paths.
        parse_failures: Sources that could not be parsed; they contribute no edge.
        script_directory_only: Edges of a file outside every package that resolve only with that file's own directory
            on the search path; a subset of ``edges``, used to class misses.
    """

    sources: tuple[str, ...]
    edges: frozenset[tuple[str, str]]
    parse_failures: tuple[str, ...] = field(default=())
    script_directory_only: frozenset[tuple[str, str]] = field(default=frozenset())


def source_universe(root: Path) -> list[Path]:
    """List the ``.py`` files that form the truth universe of *root*.

    Args:
        root: Resolved repository root.

    Returns:
        Sorted file paths below *root*, excluding skipped directories and virtualenv trees.

    Examples:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as tmp:
        ...     root = Path(tmp).resolve()
        ...     for rel in ("pkg/__init__.py", "build/gen.py", "env1/pyvenv.cfg", "env1/lib/x.py"):
        ...         (root / rel).parent.mkdir(parents=True, exist_ok=True)
        ...         _ = (root / rel).write_text("")
        ...     [p.relative_to(root).as_posix() for p in source_universe(root)]
        ['pkg/__init__.py']
    """
    sources: list[Path] = []
    for path in sorted(root.rglob("*.py")):
        parents = path.relative_to(root).parts[:-1]
        if any(part in SKIPPED_DIRECTORIES for part in parents):
            continue
        if any((root.joinpath(*parents[:depth]) / _VIRTUALENV_MARKER).exists() for depth in range(1, len(parents) + 1)):
            continue
        if path.is_file():
            sources.append(path)
    return sources


def find_module_spec(parts: list[str], search_path: list[str]) -> ModuleSpec | None:
    """Resolve dotted *parts* one component at a time over *search_path*, never importing anything.

    Args:
        parts: Module name components, for example ``["pkg", "mod"]``.
        search_path: Directories searched for the first component.

    Returns:
        The spec of the last component, or ``None`` when any component is missing, when a non-package component has
        children, or when *parts* is empty.

    Examples:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as tmp:
        ...     (Path(tmp) / "pkg").mkdir()
        ...     _ = (Path(tmp) / "pkg" / "__init__.py").write_text("")
        ...     _ = (Path(tmp) / "pkg" / "mod.py").write_text("")
        ...     Path(find_module_spec(["pkg", "mod"], [tmp]).origin).name, find_module_spec(["pkg", "nope"], [tmp])
        ('mod.py', None)
    """
    path: list[str] | None = list(search_path)
    spec: ModuleSpec | None = None
    for position, part in enumerate(parts):
        # The leaf name only: a dotted name makes a namespace spec look its parent up in sys.modules.
        spec = PathFinder.find_spec(part, path)
        if spec is None:
            return None
        locations = spec.submodule_search_locations
        # A namespace path recomputes itself from sys.modules, which this walk never populates; read its fixed list.
        path = None if locations is None else list(getattr(locations, "_path", locations))
        if path is None and position < len(parts) - 1:
            return None
    return spec


def package_parts(path: Path, root: Path) -> list[str] | None:
    """Return the dotted package components of the directory holding *path*, or ``None`` outside every package.

    Args:
        path: Source file below *root*.
        root: Resolved repository root.

    Returns:
        Package names from the outermost regular package down to the file's directory; ``None`` when the file's own
        directory has no ``__init__.py``.

    Examples:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as tmp:
        ...     root = Path(tmp).resolve()
        ...     (root / "a" / "b").mkdir(parents=True)
        ...     for rel in ("a/__init__.py", "a/b/__init__.py", "a/b/m.py", "script.py"):
        ...         _ = (root / rel).write_text("")
        ...     package_parts(root / "a" / "b" / "m.py", root), package_parts(root / "script.py", root)
        (['a', 'b'], None)
    """
    if not (path.parent / _PACKAGE_MARKER).exists():
        return None
    parts: list[str] = []
    directory = path.parent
    while directory != root.parent and (directory / _PACKAGE_MARKER).exists():
        parts.append(directory.name)
        if directory == root:
            break
        directory = directory.parent
    return list(reversed(parts))


def _in_repo_file(spec: ModuleSpec | None, files: frozenset[Path]) -> Path | None:
    """Return the resolved in-repository ``.py`` origin of *spec*, else ``None``."""
    if spec is None or not spec.origin or spec.origin in _NON_FILE_ORIGINS:
        return None
    origin = Path(spec.origin)
    if origin.suffix != ".py":
        return None
    try:
        origin = origin.resolve()
    except OSError:
        return None
    return origin if origin in files else None


@dataclass(frozen=True)
class _SourceContext:
    """Resolution inputs shared by every import statement of one source file."""

    path: Path
    package: list[str] | None
    search_path: list[str]


def _relative_base(node: ast.ImportFrom, context: _SourceContext) -> tuple[list[str], list[str]] | None:
    """Return ``(base parts, search path)`` of a relative ``from`` import, or ``None`` when it cannot resolve."""
    if context.package is None:
        return None
    keep = len(context.package) - (node.level - 1)
    if keep < 0:
        return None
    base = context.package[:keep] + (node.module.split(".") if node.module else [])
    top = context.path.parent
    for _ in context.package:
        top = top.parent
    return base, [str(top)]


def _import_from_specs(
    node: ast.ImportFrom, context: _SourceContext, files: frozenset[Path]
) -> list[ModuleSpec | None]:
    """Return the spec each name of a ``from`` import resolves to (submodule first, then the named module)."""
    if node.level:
        resolved = _relative_base(node, context)
        if resolved is None:
            return []
        base, search_path = resolved
    else:
        base, search_path = (node.module.split(".") if node.module else []), context.search_path
    if not base:
        return []
    module_spec = find_module_spec(base, search_path)
    specs: list[ModuleSpec | None] = []
    for alias in node.names:
        if alias.name == "*":
            specs.append(module_spec)
            continue
        submodule_spec = find_module_spec([*base, alias.name], search_path)
        specs.append(submodule_spec if _in_repo_file(submodule_spec, files) is not None else module_spec)
    return specs


def _statement_specs(tree: ast.AST, context: _SourceContext, files: frozenset[Path]) -> list[ModuleSpec | None]:
    """Return the spec of every module named by every import statement in *tree*."""
    specs: list[ModuleSpec | None] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            specs.extend(find_module_spec(alias.name.split("."), context.search_path) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            specs.extend(_import_from_specs(node, context, files))
    return specs


def _parse(path: Path) -> ast.AST | None:
    """Parse *path* quietly; return ``None`` when the source is not valid Python."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            return ast.parse(path.read_bytes(), filename=str(path))
        except (SyntaxError, ValueError, RecursionError):
            return None


def build_truth(root: Path) -> TruthEdges:
    """Build the static truth edge set of the repository at *root*.

    Args:
        root: Repository root; resolved before use.

    Returns:
        The universe, the edge set, and the sources that failed to parse.

    Examples:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as tmp:
        ...     root = Path(tmp)
        ...     (root / "pkg").mkdir()
        ...     _ = (root / "pkg" / "__init__.py").write_text("")
        ...     _ = (root / "pkg" / "a.py").write_text("from . import b")
        ...     _ = (root / "pkg" / "b.py").write_text("")
        ...     sorted(build_truth(root).edges)
        [('pkg/a.py', 'pkg/b.py')]
    """
    root = root.resolve()
    sources = source_universe(root)
    files = frozenset(path.resolve() for path in sources)
    base_search = [str(root)] + ([str(root / "src")] if (root / "src").is_dir() else [])
    edges: set[tuple[str, str]] = set()
    script_only: set[tuple[str, str]] = set()
    failures: list[str] = []
    for path in sources:
        tree = _parse(path)
        if tree is None:
            failures.append(path.relative_to(root).as_posix())
            continue
        package = package_parts(path, root)
        search_path = base_search if package is not None else [str(path.parent), *base_search]
        found = _edges_from(tree, _SourceContext(path=path, package=package, search_path=search_path), files, root)
        edges |= found
        if package is None:
            # Edges the file still reaches without its own directory on the search path are not script-only.
            base_context = _SourceContext(path=path, package=package, search_path=base_search)
            script_only |= found - _edges_from(tree, base_context, files, root)
    return TruthEdges(
        sources=tuple(path.relative_to(root).as_posix() for path in sources),
        edges=frozenset(edges),
        parse_failures=tuple(failures),
        script_directory_only=frozenset(script_only),
    )


def _edges_from(tree: ast.AST, context: _SourceContext, files: frozenset[Path], root: Path) -> set[tuple[str, str]]:
    """Return the in-repository edges of one parsed source under one search path."""
    relative_source = context.path.relative_to(root).as_posix()
    resolved_self = context.path.resolve()
    edges: set[tuple[str, str]] = set()
    for spec in _statement_specs(tree, context, files):
        target = _in_repo_file(spec, files)
        if target is None or target == resolved_self:
            continue
        try:
            edges.add((relative_source, target.relative_to(root).as_posix()))
        except ValueError:
            continue
    return edges
