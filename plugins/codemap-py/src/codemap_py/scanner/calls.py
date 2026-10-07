"""Extract a module's symbols and resolve the call edges between them."""

from __future__ import annotations

import ast
import builtins

from codemap_py.schema import Resolution, SymbolType

from .models import CallEdge, Symbol, _docstring_fields

#: Names of Python builtins, used to classify a call's first component as a builtin.
BUILTINS = frozenset(dir(builtins))


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


def _qualified_call_edge(qualified: str, chain: str) -> CallEdge:
    """Build an import edge from a fully qualified dotted name.

    A qualified name without a dot names no module member, so the call stays
    unresolved under its original *chain*.

    Examples:
        >>> _qualified_call_edge("pkg.db.fetch_user", "fetch_user").target
        'pkg.db::fetch_user'
        >>> _qualified_call_edge("os", "os").resolution == Resolution.UNRESOLVED
        True
    """
    if "." in qualified:
        mod, sym = qualified.rsplit(".", 1)
        return CallEdge(target=f"{mod}::{sym}", resolution=Resolution.IMPORT)
    return CallEdge(target=chain, resolution=Resolution.UNRESOLVED)


def _resolve_imported_call(
    chain: str,
    first_component: str,
    name_map: dict[str, str],
    module_map: dict[str, str],
) -> CallEdge | None:
    """Resolve a call chain through the module's import scope.

    Returns ``None`` when no import binds the chain or its first component, so the caller falls through to local, self,
    star, and builtin resolution.
    """
    # Exact match in name_map (handles `from pkg.db import fetch_user; fetch_user()`)
    if chain in name_map:
        return _qualified_call_edge(name_map[chain], chain)

    # Dotted chains where the first component is in name_map
    if first_component in name_map and first_component != chain:
        rest = chain[len(first_component) + 1 :]
        return _qualified_call_edge(f"{name_map[first_component]}.{rest}", chain)

    # Prefix match in module_map (handles `import pkg.db; pkg.db.fetch_user()`)
    if first_component in module_map:
        full_module = module_map[first_component]
        rest = chain[len(first_component) + 1 :] if len(chain) > len(first_component) else ""
        return _qualified_call_edge(f"{full_module}.{rest}" if rest else full_module, chain)
    return None


def _resolve_self_call(chain: str, current_class: str, current_module: str) -> CallEdge | None:
    """Resolve a ``self.`` or enclosing-class call chain, or return ``None``.

    Examples:
        >>> _resolve_self_call("self.save", "Repo", "pkg.db").target
        'pkg.db::Repo.save'
        >>> _resolve_self_call("helper", "Repo", "pkg.db") is None
        True
    """
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

    # 1-3. Import scope: exact name, dotted name prefix, or imported module prefix
    edge = _resolve_imported_call(chain, first_component, name_map, module_map)
    if edge is not None:
        return edge

    # 3. Local names (functions/classes defined in the same file)
    if first_component in local_names:
        return CallEdge(target=f"{current_module}::{chain}", resolution=Resolution.LOCAL)

    # 4. Self / class reference
    edge = _resolve_self_call(chain, current_class, current_module)
    if edge is not None:
        return edge

    # 5. Star import — name may come from a star-imported module
    if star_imports:
        return CallEdge(target=chain, resolution=Resolution.STAR)

    # 6. Builtins
    if first_component in BUILTINS:
        return CallEdge(target=chain, resolution=Resolution.BUILTIN)

    # 7. Unresolved
    return CallEdge(target=chain, resolution=Resolution.UNRESOLVED)


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
