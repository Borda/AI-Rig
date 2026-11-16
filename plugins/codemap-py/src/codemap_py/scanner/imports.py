"""Extract a module's imports, exports and symbol aliases from its AST."""

from __future__ import annotations

import ast
import keyword
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass

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


#: Scope-opening nodes whose own bindings hide a module-level name inside them.
_FUNCTION_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
_COMPREHENSION_SCOPES = (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)

#: Fully qualified name of the callable that ``from importlib import import_module`` binds.
_IMPORT_MODULE_QNAME = "importlib.import_module"


@dataclass(frozen=True)
class _RefContext:
    """Module-wide facts the dynamic-reference pass resolves names against.

    Attributes:
        module_name: dotted name of the module being analysed (what ``__name__`` evaluates to).
        package: dotted package of the module (what ``__package__`` evaluates to).
        name_map: local name -> fully qualified name from the module's import statements.
        constants: module-level names bound exactly once, unconditionally, to a string literal.
    """

    module_name: str
    package: str
    name_map: dict[str, str]
    constants: dict[str, ast.Constant]


def _is_module_path(text: str) -> bool:
    """Return True when *text* is a dotted module path, optionally relative (leading dots).

    Examples:
        >>> _is_module_path("pkg.mod"), _is_module_path("..sub"), _is_module_path("pkg.mod is deprecated")
        (True, True, False)
        >>> _is_module_path("."), _is_module_path("pkg.class")
        (False, False)
    """
    body = text.lstrip(".")
    return bool(body) and all(part.isidentifier() and not keyword.iskeyword(part) for part in body.split("."))


def _resolve_relative(path: str, anchor: str | None) -> str | None:
    """Resolve a possibly relative module *path* against the package *anchor*, as ``importlib`` does.

    Returns ``None`` when the path is relative and no anchor is known, or when it climbs above the anchor.

    Examples:
        >>> _resolve_relative("pkg.mod", None), _resolve_relative(".mod", "pkg"), _resolve_relative("..mod", "pkg.sub")
        ('pkg.mod', 'pkg.mod', 'pkg.mod')
        >>> _resolve_relative(".mod", None) is None, _resolve_relative("...mod", "pkg") is None
        (True, True)
    """
    level = len(path) - len(path.lstrip("."))
    if level == 0:
        return path
    parts = anchor.split(".") if anchor else []
    if level - 1 >= len(parts):
        return None
    base = ".".join(parts[: len(parts) - (level - 1)])
    rest = path[level:]
    return f"{base}.{rest}" if rest else base


def _single_name_assignment(stmt: ast.stmt) -> tuple[str | None, ast.expr | None]:
    """Return ``(name, value)`` for ``name = value`` or ``name: T = value``, else ``(None, None)``."""
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
        return stmt.targets[0].id, stmt.value
    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
        return stmt.target.id, stmt.value
    return None, None


def _module_level_binding_counts(tree: ast.Module) -> dict[str, int]:
    """Count how often each name is bound in module scope, conditional blocks included, nested scopes excluded."""
    counts: dict[str, int] = {}
    stack: list[ast.AST] = list(tree.body)
    while stack:
        node = stack.pop()
        bound: list[str] = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            counts[node.name] = counts.get(node.name, 0) + 1
            continue
        if isinstance(node, ast.Lambda):
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            bound = [alias.asname or alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound = [node.id]
        for name in bound:
            counts[name] = counts.get(name, 0) + 1
        stack.extend(ast.iter_child_nodes(node))
    return counts


def _module_string_constants(tree: ast.Module, counts: dict[str, int]) -> dict[str, ast.Constant]:
    """Map each module-level name bound exactly once, unconditionally, to its string literal node.

    A name any function declares ``global`` can be rebound at run time, so it never counts as a constant.

    Examples:
        >>> tree = ast.parse("A = 'pkg.a'\\nB = 'x'\\nB = 'y'\\nif True:\\n    C = 'c'\\nD: str = 'pkg.d'")
        >>> sorted(_module_string_constants(tree, _module_level_binding_counts(tree)))
        ['A', 'D']
    """
    declared_global = {name for node in ast.walk(tree) if isinstance(node, ast.Global) for name in node.names}
    constants: dict[str, ast.Constant] = {}
    for stmt in tree.body:
        name, value = _single_name_assignment(stmt)
        if (
            name is not None
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
            and counts.get(name) == 1
            and name not in declared_global
        ):
            constants[name] = value
    return constants


def _scope_bindings(node: ast.AST) -> set[str]:
    """Return the names a function, lambda or comprehension binds in its own scope.

    Imports inside the scope are left out: they bind the imported module itself, which the module-wide import map
    already records. The walk also enters nested scopes, which over-approximates the hidden names — a reference is
    then dropped rather than misattributed.
    """
    names: set[str] = set()
    if isinstance(node, _FUNCTION_SCOPES):
        args = node.args
        for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
            if arg is not None:
                names.add(arg.arg)
        body: list[ast.AST] = list(node.body) if isinstance(node.body, list) else [node.body]
    else:
        body = list(getattr(node, "generators", []))
    declared: set[str] = set()
    for child in body:
        for sub in ast.walk(child):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                names.add(sub.id)
            elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(sub.name)
            elif isinstance(sub, ast.ExceptHandler) and sub.name:
                names.add(sub.name)
            elif isinstance(sub, (ast.Global, ast.Nonlocal)):
                declared.update(sub.names)
    return names - declared


def _iter_scoped_calls(tree: ast.Module) -> Iterator[tuple[ast.Call, frozenset[str], ast.AST | None]]:
    """Yield every call with the names its enclosing scopes hide and its enclosing module-level function.

    Yields:
        ``(call, hidden_names, top_function)`` — *top_function* is the module-level ``def`` the call sits in, or
        ``None`` for a call at module level.
    """
    stack: list[tuple[ast.AST, frozenset[str], ast.AST | None]] = [(tree, frozenset(), None)]
    while stack:
        node, hidden, top_function = stack.pop()
        for child in ast.iter_child_nodes(node):
            child_hidden, child_top = hidden, top_function
            if isinstance(child, (*_FUNCTION_SCOPES, *_COMPREHENSION_SCOPES)):
                child_hidden = hidden | _scope_bindings(child)
                if node is tree and isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    child_top = child
            if isinstance(child, ast.Call):
                yield child, child_hidden, child_top
            stack.append((child, child_hidden, child_top))


def _is_import_call(func: ast.expr, name_map: dict[str, str], hidden: frozenset[str]) -> bool:
    """Return True for ``<x>.import_module(...)``, ``__import__(...)`` and a bare ``import_module`` from importlib."""
    if isinstance(func, ast.Attribute):
        return func.attr == "import_module"
    if not isinstance(func, ast.Name) or func.id in hidden:
        return False
    return func.id == "__import__" or name_map.get(func.id) == _IMPORT_MODULE_QNAME


def _constant_string(expr: ast.expr, ctx: _RefContext, hidden: frozenset[str]) -> ast.Constant | None:
    """Return the string literal node *expr* evaluates to: a literal, or a visible module-level string constant."""
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return expr
    if isinstance(expr, ast.Name) and expr.id not in hidden:
        return ctx.constants.get(expr.id)
    return None


def _relative_anchor(call: ast.Call, ctx: _RefContext) -> str | None:
    """Return the package a relative ``import_module(name, package)`` call resolves against, when it is static."""
    package_arg = (
        call.args[1] if len(call.args) > 1 else next((k.value for k in call.keywords if k.arg == "package"), None)
    )
    if isinstance(package_arg, ast.Name) and package_arg.id == "__name__":
        return ctx.module_name
    if isinstance(package_arg, ast.Name) and package_arg.id == "__package__":
        return ctx.package
    if isinstance(package_arg, ast.Constant) and isinstance(package_arg.value, str):
        return package_arg.value
    return None


def _import_call_record(call: ast.Call, ctx: _RefContext, hidden: frozenset[str]) -> tuple[dict | None, bool]:
    """Classify one import call as ``(dynamic_imports entry or None, counts_as_dynamic_site)``.

    A string literal passed to ``<x>.import_module`` or ``__import__`` is already recorded by
    :func:`extract_dynamic_imports`, so it yields nothing here; the bare ``import_module`` spelling is recorded the same
    way. A module-level string constant yields an entry marked ``via: "constant"`` whose ``line`` is the constant's own
    line, where the module path text lives. Anything else is a dynamic site.
    """
    if not call.args:
        return None, False
    arg0 = call.args[0]
    if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
        if isinstance(call.func, ast.Name) and call.func.id != "__import__":
            return {"literal": arg0.value, "line": call.lineno}, False
        return None, False
    constant = _constant_string(arg0, ctx, hidden)
    resolved = _resolve_relative(constant.value, _relative_anchor(call, ctx)) if constant is not None else None
    if constant is None or resolved is None or not _is_module_path(resolved):
        return None, True
    return {"literal": resolved, "line": constant.lineno, "via": "constant"}, False


def _base_module_path(expr: ast.expr, ctx: _RefContext, hidden: frozenset[str]) -> str | None:
    """Return the import path an expression like ``mod`` or ``pkg.sub`` names, when its root is an imported name."""
    attrs: list[str] = []
    while isinstance(expr, ast.Attribute):
        attrs.append(expr.attr)
        expr = expr.value
    if not isinstance(expr, ast.Name) or expr.id in hidden:
        return None
    root = ctx.name_map.get(expr.id)
    if root is None:
        return None
    return ".".join([root, *reversed(attrs)])


def _getattr_record(call: ast.Call, ctx: _RefContext, hidden: frozenset[str]) -> dict | None:
    """Return the ``dynamic_refs`` record of a ``getattr(<imported name>, <name>[, default])`` call, or ``None``.

    Standard-library bases are dropped: they can never name an indexed module. Whether the base is a module at all is
    decided later against the indexed module names (``codemap_py.graph``).
    """
    if len(call.args) < 2:
        return None
    base = _base_module_path(call.args[0], ctx, hidden)
    if base is None or base.split(".")[0] in _STDLIB_MODULES:
        return None
    record: dict = {"kind": "getattr", "module": base, "line": call.lineno}
    attr = _constant_string(call.args[1], ctx, hidden)
    if attr is not None and attr.value.isidentifier():
        record["name"] = attr.value
    return record


def _performs_import(function: ast.AST, name_map: dict[str, str]) -> bool:
    """Return True when *function* contains an import statement or an import call."""
    for node in ast.walk(function):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            return True
        if isinstance(node, ast.Call) and _is_import_call(node.func, name_map, frozenset()):
            return True
    return False


def _lazy_map_target(key: ast.expr | None, value: ast.expr) -> ast.Constant | None:
    """Return the module-path literal of one lazy-map entry: ``"attr": "mod"`` or ``"attr": ("mod", "attr")``."""
    if not (isinstance(key, ast.Constant) and isinstance(key.value, str) and key.value.isidentifier()):
        return None
    if isinstance(value, ast.Tuple) and len(value.elts) == 2:
        if all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in value.elts):
            value = value.elts[0]
    if isinstance(value, ast.Constant) and isinstance(value.value, str) and _is_module_path(value.value):
        return value
    return None


def _lazy_getattr_imports(
    tree: ast.Module, ctx: _RefContext, counts: dict[str, int]
) -> tuple[list[dict], ast.AST | None]:
    """Resolve PEP 562 lazy-import maps: module-level dicts a module-level ``__getattr__`` reads before importing.

    Returns:
        ``(entries, getattr_function)`` — one ``dynamic_imports`` entry marked ``via: "lazy_getattr"`` per map value,
        and the ``__getattr__`` definition whose import calls the map resolves (``None`` when no entry resolved).
    """
    getattr_function = next(
        (s for s in reversed(tree.body) if isinstance(s, ast.FunctionDef) and s.name == "__getattr__"), None
    )
    if getattr_function is None or not _performs_import(getattr_function, ctx.name_map):
        return [], None
    read_names = {n.id for n in ast.walk(getattr_function) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    entries: list[dict] = []
    for stmt in tree.body:
        name, value = _single_name_assignment(stmt)
        if name not in read_names or not isinstance(value, ast.Dict) or counts.get(name) != 1:
            continue
        for key, item in zip(value.keys, value.values):
            target = _lazy_map_target(key, item)
            resolved = _resolve_relative(target.value, ctx.package) if target is not None else None
            if target is not None and resolved is not None:
                entries.append({"literal": resolved, "line": target.lineno, "via": "lazy_getattr"})
    return entries, (getattr_function if entries else None)


def extract_dynamic_references(
    tree: ast.Module, module_name: str, is_package: bool, name_map: dict[str, str]
) -> tuple[list[dict], list[dict]]:
    """Resolve the static dynamic-import and ``getattr`` forms that string literals alone do not cover.

    Three forms are resolved without running code:

    * ``importlib.import_module(CONST)`` (or ``__import__``/a bare ``import_module``) whose argument is a module-level
      string constant, bound exactly once and not hidden by a local name; relative paths resolve against ``__name__``,
      ``__package__`` or a literal ``package`` argument.
    * PEP 562 lazy-import maps: a module-level dict of ``"attr": "module.path"`` (or ``("module.path", "attr")``)
      that a module-level ``__getattr__`` reads and that performs an import.
    * ``getattr(<imported name>, "attr")`` with a literal or constant attribute name.

    Every other import call, and every ``getattr`` on an imported name whose attribute is not static, is a dynamic
    site: recorded so the count of unresolvable references is never silently zero.

    Args:
        tree: parsed AST of the module.
        module_name: dotted name of the module.
        is_package: True for a package ``__init__``.
        name_map: local name -> qualified name map from :func:`_extract_imports_and_scope`.

    Returns:
        ``(imports, refs)`` — *imports* are extra ``dynamic_imports`` entries (``literal``, ``line`` and, for the
        resolved forms, ``via``); *refs* are ``dynamic_refs`` records: ``{"kind": "getattr", "module", "line"}`` plus
        ``name`` when the attribute is static, and ``{"kind": "import", "line"}`` for an unresolved import call.

    Examples:
        >>> src = "import importlib as il\\nTARGET = 'pkg.plugins'\\nil.import_module(TARGET)\\nil.import_module(x)"
        >>> extract_dynamic_references(ast.parse(src), "pkg.loader", False, {"il": "importlib"})
        ([{'literal': 'pkg.plugins', 'line': 2, 'via': 'constant'}], [{'kind': 'import', 'line': 4}])
        >>> extract_dynamic_references(ast.parse("getattr(mod, 'run')"), "pkg.a", False, {"mod": "pkg.mod"})
        ([], [{'kind': 'getattr', 'module': 'pkg.mod', 'line': 1, 'name': 'run'}])
    """
    counts = _module_level_binding_counts(tree)
    package = module_name if is_package else module_name.rsplit(".", 1)[0] if "." in module_name else ""
    ctx = _RefContext(module_name, package, name_map, _module_string_constants(tree, counts))
    imports, lazy_function = _lazy_getattr_imports(tree, ctx, counts)
    getattr_visible = "getattr" not in name_map and counts.get("getattr") is None
    refs: list[dict] = []
    for call, hidden, top_function in _iter_scoped_calls(tree):
        if _is_import_call(call.func, name_map, hidden):
            entry, is_site = _import_call_record(call, ctx, hidden)
            if entry is not None:
                imports.append(entry)
            # A lazy map resolves the non-literal import calls of the __getattr__ that reads it.
            elif is_site and not (lazy_function is not None and top_function is lazy_function):
                refs.append({"kind": "import", "line": call.lineno})
        elif (
            getattr_visible
            and isinstance(call.func, ast.Name)
            and call.func.id == "getattr"
            and "getattr" not in hidden
        ):
            record = _getattr_record(call, ctx, hidden)
            if record is not None:
                refs.append(record)
    # The scoped walk is depth-first from a stack; source order keeps the persisted lists deterministic and readable.
    imports.sort(key=lambda entry: entry["line"])
    # Two calls reading one constant point at the same constant line; one entry per (path, line, form) is enough.
    imports = list({(entry["literal"], entry["line"], entry.get("via")): entry for entry in imports}.values())
    refs.sort(key=lambda record: (record["line"], record["kind"], record.get("module", ""), record.get("name", "")))
    return imports, refs
