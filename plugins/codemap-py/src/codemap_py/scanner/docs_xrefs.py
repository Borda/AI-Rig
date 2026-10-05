"""Extract documentation cross-references from RST, MkDocs and config files."""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

from .exclusions import SKIP_DIRS

_CONFIG_SCAN_PATTERNS = ("pyproject.toml", "setup.cfg", "setup.py", "*.yml", "*.yaml")


# Match dotted names with ≥1 dot — simple module path heuristic (no single-word false positives).
_DOTTED_NAME_RE = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]*)(?:\.[a-zA-Z_][a-zA-Z0-9_]+)+\b")


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
