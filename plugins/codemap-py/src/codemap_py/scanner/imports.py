"""Extract a module's imports, exports and symbol aliases from its AST."""

from __future__ import annotations

import ast
import sys
from collections.abc import Callable

_STDLIB_MODULES: frozenset[str] = frozenset(sys.stdlib_module_names)  # Python 3.10+; project requires 3.10+


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


#: Module-level statements that can bind a name on only some execution paths.
_CONDITIONAL_SCOPE_NODES = (ast.For, ast.AsyncFor, ast.With, ast.AsyncWith, ast.Try, ast.If, ast.Match)


def _import_bound_names(node: ast.Import | ast.ImportFrom) -> list[str]:
    """Return the local names an import statement binds, skipping star imports.

    Examples:
        >>> _import_bound_names(ast.parse("import os.path, json as j").body[0])
        ['os', 'j']
        >>> _import_bound_names(ast.parse("from pkg import a, b as c").body[0])
        ['a', 'c']
        >>> _import_bound_names(ast.parse("from pkg import *").body[0])
        []
    """
    if isinstance(node, ast.Import):
        return [alias.asname or alias.name.split(".")[0] for alias in node.names]
    return [alias.asname or alias.name for alias in node.names if alias.name != "*"]


def _assignment_targets(node: ast.Assign | ast.AnnAssign | ast.AugAssign) -> list[ast.expr]:
    """Return the target expressions of an assignment statement."""
    return node.targets if isinstance(node, ast.Assign) else [node.target]


def _simple_target_names(target: ast.expr) -> set[str]:
    """Return the bound name when *target* is a plain name, ignoring unpacking.

    Examples:
        >>> _simple_target_names(ast.parse("a = 1").body[0].targets[0])
        {'a'}
        >>> _simple_target_names(ast.parse("a, b = 1, 2").body[0].targets[0])
        set()
    """
    return {target.id} if isinstance(target, ast.Name) else set()


def _unpacked_target_names(target: ast.expr, *, include_starred: bool = True) -> set[str]:
    """Return every name bound by an assignment target, descending into tuple and list unpacking.

    Args:
        target: assignment, loop, or ``with`` target expression.
        include_starred: also bind the name under a ``*rest`` element. The rebinding pass for the import scope
            historically ignores starred elements, so it passes ``False``.

    Examples:
        >>> sorted(_unpacked_target_names(ast.parse("a, [b, *c] = x").body[0].targets[0]))
        ['a', 'b', 'c']
        >>> sorted(_unpacked_target_names(ast.parse("a, *c = x").body[0].targets[0], include_starred=False))
        ['a']
    """
    if isinstance(target, ast.Name):
        return {target.id}
    if include_starred and isinstance(target, ast.Starred):
        return _unpacked_target_names(target.value, include_starred=include_starred)
    if isinstance(target, (ast.Tuple, ast.List)):
        return {name for item in target.elts for name in _unpacked_target_names(item, include_starred=include_starred)}
    return set()


def _statement_bound_names(node: ast.AST, target_names: Callable[[ast.expr], set[str]]) -> set[str]:
    """Return the names one statement binds in its own scope, without descending into its body.

    Args:
        node: statement or exception handler to inspect.
        target_names: extracts bound names from an assignment, loop, or ``with`` target.
    """
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return set(_import_bound_names(node))
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        return {name for target in _assignment_targets(node) for name in target_names(target)}
    if isinstance(node, (ast.For, ast.AsyncFor)):
        return target_names(node.target)
    if isinstance(node, (ast.With, ast.AsyncWith)):
        return {name for item in node.items if item.optional_vars for name in target_names(item.optional_vars)}
    if isinstance(node, ast.ExceptHandler) and node.name:
        return {node.name}
    return set()


def _import_from_aliases(node: ast.ImportFrom, package: str) -> list[tuple[str, str]]:
    """Return ``(local_name, "module::name")`` pairs for a non-star ``from`` import, in source order.

    Returns an empty list when the base module cannot be resolved.
    """
    base = _resolve_import_from_base(node, package)
    if not base:
        return []
    return [
        (imported.asname or imported.name, f"{base}::{imported.name}")
        for imported in node.names
        if imported.name != "*"
    ]


def _conditional_bindings(
    node: ast.AST, target_names: Callable[[ast.expr], set[str]], package: str | None = None
) -> tuple[set[str], list[tuple[str, str]]]:
    """Collect bindings nested under a conditional scope, excluding local bodies.

    A nested function or class contributes only its own name; its body is a separate scope and is not entered.

    Args:
        node: conditional module-level statement (loop, ``with``, ``try``, ``if``, ``match``).
        target_names: extracts bound names from an assignment, loop, or ``with`` target.
        package: package anchoring relative ``from`` imports; when given, nested ``from`` imports are also
            returned as ``(local_name, "module::name")`` pairs. ``None`` skips that collection.

    Returns:
        Bound names plus the nested ``from`` import aliases (empty when *package* is ``None``).
    """
    names: set[str] = set()
    imports: list[tuple[str, str]] = []
    pending = list(ast.iter_child_nodes(node))
    while pending:
        current = pending.pop()
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(current.name)
            continue
        names.update(_statement_bound_names(current, target_names))
        if package is not None and isinstance(current, ast.ImportFrom):
            imports.extend(_import_from_aliases(current, package))
        pending.extend(ast.iter_child_nodes(current))
    return names, imports


def _top_level_binding_positions(tree: ast.Module) -> tuple[dict[str, int], dict[str, int], set[str]]:
    """Record where each module-level name is imported and where it is last rebound.

    Returns:
        ``(imported_at, rebound_at, conditional_names)``: body index of each name's last import, body index of
        each name's last non-import binding, and the names bound only under conditional control flow.
    """
    imported_at: dict[str, int] = {}
    rebound_at: dict[str, int] = {}
    conditional_names: set[str] = set()
    for position, node in enumerate(tree.body):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for name in _import_bound_names(node):
                imported_at[name] = position
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            rebound_at[node.name] = position
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            for target in _assignment_targets(node):
                for name in _unpacked_target_names(target, include_starred=False):
                    rebound_at[name] = position
        elif isinstance(node, _CONDITIONAL_SCOPE_NODES):
            names, _ = _conditional_bindings(node, _simple_target_names)
            for name in names:
                rebound_at[name] = position
                conditional_names.add(name)
    return imported_at, rebound_at, conditional_names


def _drop_top_level_rebindings(tree: ast.Module, name_map: dict[str, str], module_map: dict[str, str]) -> None:
    """Drop direct import names overwritten later in module scope.

    The general import scope intentionally includes function-local imports for call extraction. This narrow post-pass
    only corrects a module-level import that is replaced by a later module-level binding; treating that call as the
    original import would create a false reverse edge.
    """
    imported_at, rebound_at, conditional_names = _top_level_binding_positions(tree)
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

    def _reject(names: set[str], reason: str) -> None:
        for local_name in names:
            target = aliases.pop(local_name, None)
            if target is not None:
                limitations.add((f"{module_name}::{local_name}", target, reason))

    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            aliases.update(_import_from_aliases(node, package))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            _reject({node.name}, "top_level_rebinding")
        elif isinstance(node, _CONDITIONAL_SCOPE_NODES):
            # Conditional control flow can bind a module name only on some paths.
            # Excluding it is conservative: no alias provenance is better than a
            # false canonical edge.
            names, imports = _conditional_bindings(node, _unpacked_target_names, package)
            _reject(names, "conditional_binding")
            limitations.update(
                (f"{module_name}::{local_name}", target, "conditional_import") for local_name, target in imports
            )
        else:
            # A plain `import` or an assignment rebinds the name unconditionally.
            _reject(_statement_bound_names(node, _unpacked_target_names), "top_level_rebinding")
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
