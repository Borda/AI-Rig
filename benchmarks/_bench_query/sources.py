"""Module-name and Python-source resolution used to verify importers against the AST."""

from __future__ import annotations

import ast
import os
from pathlib import Path

from _bench_common.python_source import extract_import_targets, resolve_relative_base

# ---- HELPERS ----


def path_to_module(path: str, repo_root: str) -> str | None:
    """Convert a filesystem path to a dotted module name relative to the repo root.

    For an on-disk regular package, the name starts at the outermost package
    directory, so a non-package container such as ``tests/`` is excluded. Paths
    without a discoverable package retain the legacy repo-relative conversion.

    Args:
        path: Absolute or relative path to a ``.py`` file.
        repo_root: Absolute path to the repository root used as the base for
            ``os.path.relpath``.

    Returns:
        Dotted module name (e.g. ``"lightning.pytorch.trainer.trainer"``), or
        ``None`` if ``path`` does not end with ``.py``.

    Examples:
        >>> path_to_module("/repo/src/pkg/mod.py", "/repo")
        'pkg.mod'
        >>> path_to_module("/repo/pkg/__init__.py", "/repo")
        'pkg'
        >>> path_to_module("/repo/README.md", "/repo") is None
        True
    """
    file_path = Path(path)
    root = Path(repo_root)
    if not file_path.is_absolute():
        file_path = root / file_path
    if file_path.suffix != ".py":
        return None

    parts = [] if file_path.stem == "__init__" else [file_path.stem]
    package_dir = file_path.parent
    found_package = False
    while package_dir != root and ((package_dir / "__init__.py").exists() or (package_dir / "__init__.pyi").exists()):
        found_package = True
        parts.append(package_dir.name)
        package_dir = package_dir.parent
    if found_package:
        return ".".join(reversed(parts))

    rel = os.path.relpath(file_path, root).replace(os.sep, "/")
    if rel.startswith("src/"):
        rel = rel[4:]
    mod = rel[:-3].replace("/", ".")
    return mod[:-9] if mod.endswith(".__init__") else mod


def module_to_grep_pattern(module: str) -> str:
    """Build a grep alternation pattern that matches direct imports of a module.

    The returned pattern matches both ``from <module> import ...`` and
    ``import <module>`` forms and is suitable for ``grep -rn <pattern>``.

    Args:
        module: Dotted module name (e.g. ``"lightning.pytorch.trainer.trainer"``).

    Returns:
        A grep alternation string using ``\\|`` as the separator.

    Examples:
        >>> module_to_grep_pattern("foo.bar")
        'from foo.bar import\\\\|import foo.bar'
    """
    # For grep: match "from <module> import" or "import <module>"
    return rf"from {module} import\|import {module}"


def module_to_package(module: str) -> str | None:
    """Return the parent package of a dotted module name, or None for top-level modules.

    Args:
        module: Dotted module name (e.g. ``"lightning.pytorch.trainer.trainer"``).

    Returns:
        The parent package (everything before the last dot), or ``None`` when
        ``module`` has no dot (i.e. is already a top-level module).

    Examples:
        >>> module_to_package("foo.bar.baz")
        'foo.bar'
        >>> module_to_package("foo") is None
        True
    """
    parts = module.rsplit(".", 1)
    return parts[0] if len(parts) > 1 else None


def module_to_source_file(module: str, repo_root: Path) -> Path | None:
    """Resolve a dotted module name to its source ``.py`` file on disk.

    Checks ``src/`` layout and ``__init__.py`` package variants, mirroring the
    candidate order used by :func:`count_cold_calls_deps`. For a regular package
    below another non-package directory, falls back to the matching
    :func:`path_to_module` identity.

    Args:
        module: Dotted module name (e.g. ``"pkg.sub.mod"``).
        repo_root: Repository root to resolve the module against.

    Returns:
        The first existing candidate path, or ``None`` when no source file is
        found.

    Examples:
        >>> module_to_source_file("nope.not.here", getfixture("tmp_path")) is None
        True
    """
    parts = module.replace(".", "/")
    candidates = [
        repo_root / "src" / f"{parts}.py",
        repo_root / f"{parts}.py",
        repo_root / "src" / parts / "__init__.py",
        repo_root / parts / "__init__.py",
    ]
    direct_match = next((candidate for candidate in candidates if candidate.exists()), None)
    if direct_match is not None:
        return direct_match

    # A package may live below a non-package container (for example, tests/).
    # Match its actual dotted identity instead of inventing a directory prefix.
    filename = f"{module.rsplit('.', maxsplit=1)[-1]}.py"
    for candidate in sorted(repo_root.rglob(filename)):
        if {".git", "__pycache__"}.intersection(candidate.parts):
            continue
        if path_to_module(str(candidate), str(repo_root)) == module:
            return candidate
    for candidate in sorted(repo_root.rglob("__init__.py")):
        if {".git", "__pycache__"}.intersection(candidate.parts):
            continue
        if path_to_module(str(candidate), str(repo_root)) == module:
            return candidate
    return None


def _file_base_package(file_path: Path, repo_root: Path) -> str:
    """Return the dotted package that contains ``file_path`` (for relative-import resolution).

    Args:
        file_path: Path to a ``.py`` source file.
        repo_root: Repository root used as the relative base.

    Returns:
        Dotted name of the directory containing the file (empty string when the
        file sits at the repository root or under a bare ``src/`` layout root).
    """
    rel = os.path.relpath(str(file_path), str(repo_root)).replace(os.sep, "/")
    if rel.startswith("src/"):
        rel = rel[4:]
    directory = os.path.dirname(rel)
    return directory.replace("/", ".").strip(".")


def _resolve_relative(base_package: str, level: int, module: str | None) -> str:
    """Resolve a relative import target to its absolute dotted module base.

    Args:
        base_package: Dotted package containing the importing file (``level`` 1
            resolves against this package).
        level: Relative-import level (number of leading dots).
        module: The dotted suffix after the dots, or ``None`` for ``from . import x``.

    Returns:
        Absolute dotted base for the import (may be an empty string when the
        relative reference escapes the resolvable root).

    Examples:
        >>> _resolve_relative("pkg.rel", 2, "target")
        'pkg.target'
        >>> _resolve_relative("pkg.rel", 1, None)
        'pkg.rel'
    """
    # escape_to_none=False keeps this lane's permissive contract (str, no over-ascend guard).
    return resolve_relative_base(base_package, level, module, escape_to_none=False)


def _imported_names_from_source(source: str, base_package: str) -> set[str]:
    """Collect every absolute dotted name referenced by the import statements in ``source``.

    Handles ``import a.b``, ``import a.b as c``, ``from a.b import c`` (recording
    both ``a.b`` and ``a.b.c``), and relative imports resolved against
    ``base_package``.

    Args:
        source: Python source text to parse.
        base_package: Dotted package of the source file, for relative imports.

    Returns:
        Set of absolute dotted names introduced by import statements.

    Raises:
        SyntaxError: If ``source`` cannot be parsed.
    """
    # symbol_when_bare=True keeps this lane's permissive contract: record symbol names when a
    # relative import resolves to an empty base (matches the historical _resolve_relative path).
    return extract_import_targets(ast.parse(source), package=base_package, symbol_when_bare=True)


def file_imports_module(file_path: Path, target_module: str, repo_root: Path) -> bool:
    """Verify by AST that ``file_path`` truly imports ``target_module``.

    A file imports the target when any import statement resolves to the target
    module itself or to a descendant of it (importing ``a.b.c`` imports the
    package ``a.b``).  Relative and aliased imports are resolved correctly.

    Args:
        file_path: Path to the importing source file.
        target_module: Dotted module name whose import is being verified.
        repo_root: Repository root, used to resolve relative imports.

    Returns:
        ``True`` when the file imports the target module; ``False`` on read
        error, syntax error, or when no matching import is found.
    """
    try:
        source = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    try:
        names = _imported_names_from_source(source, _file_base_package(file_path, repo_root))
    except SyntaxError:
        return False
    prefix = f"{target_module}."
    return any(name == target_module or name.startswith(prefix) for name in names)


def verify_importer(candidate_module: str, target_module: str, repo_root: Path) -> bool:
    """Confirm that ``candidate_module``'s source file actually imports ``target_module``.

    Resolves the candidate to a source file and delegates to
    :func:`file_imports_module`.  Used to filter grep-missed extras down to true
    importers when computing the coverage gap.

    Args:
        candidate_module: Dotted module claimed (by the index) to import the target.
        target_module: Dotted module whose import is being verified.
        repo_root: Repository root for file resolution.

    Returns:
        ``True`` only when the candidate resolves to a file that genuinely
        imports the target module.
    """
    source_file = module_to_source_file(candidate_module, repo_root)
    if source_file is None:
        return False
    return file_imports_module(source_file, target_module, repo_root)
