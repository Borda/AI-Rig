"""Python import-graph and source-walking helpers for benchmark oracles."""

from __future__ import annotations

import ast
import os
from collections.abc import Callable, Iterator
from pathlib import Path


def resolve_relative_base(package: str, level: int, module: str | None, *, escape_to_none: bool = True) -> str | None:
    """Resolve a relative ``from`` import to its absolute base module.

    Ascends ``level - 1`` package components and appends ``module``.

    Args:
        package: Dotted package of the importing module (its ``__package__``).
        level: Number of leading dots (1 = current package, 2 = parent, ...).
        module: Text after the dots (``from ..a.b import x`` → ``"a.b"``); ``None`` for
            ``from . import x``.
        escape_to_none: When ``True`` (default), an over-ascend past the package root, or an
            empty result, returns ``None``. When ``False``, reproduce the permissive form:
            no over-ascend guard (Python negative slicing) and an empty base yields ``""``
            (or the bare ``module`` when a suffix is present).

    Returns:
        Absolute dotted base module. ``None`` on escape/empty when ``escape_to_none`` (else a
        possibly-empty ``str``).

    Examples:
        >>> resolve_relative_base("a.b", 1, "c")
        'a.b.c'
        >>> resolve_relative_base("a", 3, "x") is None
        True
        >>> resolve_relative_base("a", 3, "x", escape_to_none=False)
        'x'
    """
    base_parts = package.split(".") if package else []
    ascend = level - 1
    if escape_to_none and ascend > len(base_parts):
        return None
    kept = base_parts[: len(base_parts) - ascend] if ascend else base_parts
    base = ".".join(kept)
    if module:
        combined = f"{base}.{module}" if base else module
    else:
        combined = base
    if escape_to_none:
        return combined or None
    return combined


def _import_target_kept(name: str, keep: set[str] | None) -> bool:
    """Return True when a dotted import target survives the internal-module filter.

    Args:
        name: Dotted import target.
        keep: Allowlist of internal modules; ``None`` keeps everything.

    Returns:
        True when ``keep`` is ``None`` or ``name`` is a member of it.

    Examples:
        >>> _import_target_kept("a.b", None)
        True
        >>> _import_target_kept("a.b", {"a.b"})
        True
        >>> _import_target_kept("c.d", {"a.b"})
        False
    """
    return keep is None or name in keep


def extract_import_targets(
    tree: ast.Module,
    *,
    package: str = "",
    keep: set[str] | None = None,
    credit_submodules: bool = True,
    symbol_when_bare: bool = False,
) -> set[str]:
    """Collect the dotted import targets referenced by a module's import statements.

    Walks ``import a.b`` aliases and ``from a.b import c`` targets, resolving relative imports
    against ``package``. The flags reproduce each caller's historical contract:

    - ``keep``: when given, only targets in this set are returned (internal-module filter).
    - ``credit_submodules``: also credit ``a.b.c`` for ``from a.b import c`` (not just ``a.b``).
    - ``symbol_when_bare``: when a relative import resolves to an empty base, record the imported
      *symbol* names instead of skipping (and resolve relatives permissively, no over-ascend guard).

    Args:
        tree: Parsed module AST.
        package: Dotted package of the importing module (for relative resolution).
        keep: Optional internal-module allowlist; ``None`` keeps everything.
        credit_submodules: Credit the ``base.name`` submodule form for ``from`` imports.
        symbol_when_bare: Record symbol names when the resolved base is empty (permissive lane).

    Returns:
        Set of dotted names the module imports (filtered by ``keep`` when provided).

    Examples:
        >>> import ast
        >>> t = ast.parse("import a.b\\nfrom c.d import e\\nfrom . import f\\n")
        >>> sorted(extract_import_targets(t, keep={"a.b", "c.d"}))
        ['a.b', 'c.d']
        >>> sorted(extract_import_targets(t, symbol_when_bare=True))
        ['a.b', 'c.d', 'c.d.e', 'f']
    """
    esc = not symbol_when_bare
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _import_target_kept(alias.name, keep):
                    targets.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base = node.module or ""
            else:
                base = resolve_relative_base(package, node.level, node.module, escape_to_none=esc) or ""
            if not base:
                if symbol_when_bare:
                    targets.update(a.name for a in node.names if a.name != "*")
                continue
            if _import_target_kept(base, keep):
                targets.add(base)
            if credit_submodules:
                for alias in node.names:
                    full = f"{base}.{alias.name}"
                    if alias.name != "*" and _import_target_kept(full, keep):
                        targets.add(full)
    return targets


# Directory names pruned from a source walk by default (build/venv cruft, never source).
PY_WALK_SKIP = frozenset({"__pycache__", ".venv", "venv"})


def prune_walk_dirs(dirnames: list[str], *, skip: frozenset[str] = PY_WALK_SKIP) -> list[str]:
    """Prune hidden and excluded directory names from an in-place source walk.

    Mutates ``dirnames[:]`` so ``os.walk`` does not descend into hidden/cruft/`skip` dirs, and
    returns it for convenience. This is the shared body behind every runner's walk loop.

    Args:
        dirnames: The mutable ``dirnames`` list yielded by ``os.walk`` (edited in place).
        skip: Directory names to prune in addition to any dotfile-prefixed dir.

    Returns:
        The same ``dirnames`` list, pruned.

    Examples:
        >>> ds = ["pkg", ".git", "__pycache__", "sub"]
        >>> prune_walk_dirs(ds)
        ['pkg', 'sub']
        >>> ds
        ['pkg', 'sub']
    """
    dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in skip]
    return dirnames


def iter_py_files(root: Path, *, skip: frozenset[str] = PY_WALK_SKIP) -> Iterator[Path]:
    """Yield ``*.py`` files under ``root``, pruning hidden and ``skip`` directories.

    Directories whose name starts with ``.`` (``.git``, ``.cache``) and any name in ``skip``
    are not descended into. Callers pass their own ``skip`` set (e.g. ``{"tests", "test"}`` to
    exclude test trees) — the default excludes only build/venv cruft.

    Args:
        root: Repository root to walk.
        skip: Directory names to prune (in addition to any dotfile-prefixed dir).

    Yields:
        Candidate Python paths rooted at ``root``; relative roots yield relative paths.

    Examples:
        >>> import tempfile, pathlib
        >>> with tempfile.TemporaryDirectory() as d:
        ...     r = pathlib.Path(d)
        ...     _ = (r / "a.py").write_text("")
        ...     _ = (r / "__pycache__").mkdir()
        ...     _ = (r / "__pycache__" / "b.py").write_text("")
        ...     sorted(p.name for p in iter_py_files(r))
        ['a.py']
    """
    for dirpath, dirnames, filenames in os.walk(root):
        prune_walk_dirs(dirnames, skip=skip)
        for name in filenames:
            if name.endswith(".py"):
                yield Path(dirpath) / name


def walk_py_modules(
    root: Path,
    *,
    skip: frozenset[str] = PY_WALK_SKIP,
    keep: Callable[[str], bool] | None = None,
) -> Iterator[tuple[Path, str, ast.Module]]:
    """Yield ``(path, rel_path, tree)`` for every parseable ``*.py`` file under ``root``.

    Bundles the walk+prune+``.py``-filter+``rel_path``-normalise+``ast.parse``-skip-``SyntaxError``
    scaffold shared by the AST oracle scans. ``rel_path`` is POSIX-normalised
    (``os.sep`` → ``/``) so callers can apply path regexes portably. Files that fail to parse are
    silently skipped, matching the oracle's tolerant scan.

    Args:
        root: Repository root to walk.
        skip: Directory names to prune (in addition to any dotfile-prefixed dir).
        keep: Optional predicate on the POSIX ``rel_path``; when given, only files for which it
            returns truthy are parsed and yielded (e.g. ``lambda r: not TEST_RE.search(r)`` to drop
            test modules, or ``lambda r: bool(TEST_RE.search(r))`` to keep only them). ``keep`` is
            evaluated *before* parsing, so unwanted files are never read.

    Yields:
        ``(path, rel_path, tree)`` triples: path rooted at ``root``, POSIX repo-relative path, and parsed AST.

    Examples:
        >>> import tempfile, pathlib
        >>> with tempfile.TemporaryDirectory() as d:
        ...     r = pathlib.Path(d)
        ...     _ = (r / "a.py").write_text("import os\\n")
        ...     _ = (r / "bad.py").write_text("def (\\n")  # unparsable — skipped
        ...     [rel for _p, rel, _t in walk_py_modules(r)]
        ['a.py']
    """
    for dirpath, dirnames, filenames in os.walk(root):
        prune_walk_dirs(dirnames, skip=skip)
        for name in filenames:
            if not name.endswith(".py"):
                continue
            path = Path(dirpath) / name
            rel_path = str(path.relative_to(root)).replace(os.sep, "/")
            if keep is not None and not keep(rel_path):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"), filename=str(path))
            except SyntaxError:
                continue
            yield path, rel_path, tree


def module_from_init_chain(path: Path) -> str:
    """Derive a dotted module name by walking a file's ``__init__.py`` package chain.

    Ascends from ``path`` while each parent directory is a package (contains ``__init__.py``),
    collecting package names; the walk stops at the first non-package ancestor (the ``src/`` dir
    in a src-layout repo, or the repo root in a flat layout), so no ``src.`` prefix is emitted.

    Args:
        path: Absolute path to a ``.py`` file whose parent directory is a package.

    Returns:
        Dotted module name (an ``__init__.py`` resolves to its package name); ``""`` when the
        file is not inside any package.

    Examples:
        >>> import tempfile, pathlib
        >>> with tempfile.TemporaryDirectory() as d:
        ...     r = pathlib.Path(d)
        ...     _ = (r / "pkg").mkdir()
        ...     _ = (r / "pkg" / "__init__.py").write_text("")
        ...     _ = (r / "pkg" / "mod.py").write_text("")
        ...     module_from_init_chain(r / "pkg" / "mod.py")
        'pkg.mod'
    """
    parts: list[str] = []
    if path.stem != "__init__":
        parts.append(path.stem)
    directory = path.parent
    while (directory / "__init__.py").exists() and directory != directory.parent:
        parts.append(directory.name)
        directory = directory.parent
    parts.reverse()
    return ".".join(parts)
