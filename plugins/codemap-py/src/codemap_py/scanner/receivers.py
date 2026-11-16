"""Record method calls whose receiver has a class known from the module's own source.

A call ``obj.method()`` names a method of whatever class ``obj`` holds, which the import map alone cannot see. This
module reads, per function, only facts written in the file: a constructor assignment ``obj = Class(...)``, an
annotation ``obj: Class`` (including ``Optional[Class]`` and ``Class | None``), an instance attribute set from either of
those, a zero-argument ``super().method()`` call, and a direct ``Class(...).method()`` call. Each fact becomes a *typed
call* record naming the receiver class as the import map spells it; the whole-index pass in
:mod:`codemap_py.call_inference` later decides whether that class is indexed and which class in its hierarchy defines
the method.

Nothing is guessed: a name bound in any other way in the same scope (another call result, a loop target, a ``with``
target, a ``match`` capture, an import, ``global``) gives no record, and neither does a constructor of a name shadowed
by a local binding or a ``super(...)`` call with arguments.
Class bases are recorded as the import map spells them, so the whole-index pass can walk indexed hierarchies.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass, field

__all__ = ["ReceiverFacts", "TypedCallRule", "extract_receiver_facts"]


class TypedCallRule:
    """Names of the rules that produce typed call records; the value is persisted as the edge's ``inferred`` marker."""

    CONSTRUCTOR = "constructor"
    ANNOTATION = "annotation"
    ATTRIBUTE = "attribute"
    SUPER = "super"


#: Marker for a binding that gives the name no single known class.
_OPAQUE = "?"
#: Last component of the typing helpers whose argument is the receiver class.
_UNWRAP_ONE = frozenset({"Optional", "Annotated", "Final", "ClassVar"})
#: Decorators after which a method's first parameter is not the instance.
_NOT_INSTANCE = frozenset({"staticmethod", "classmethod"})


@dataclass
class ReceiverFacts:
    """Per-module receiver facts, persisted as additive module fields when non-empty.

    Attributes:
        typed_calls: ``{"caller", "cls", "method", "rule"}`` records, ``cls`` a dotted class name as imported.
        class_bases: top-level class name to its base classes, dotted as imported; ``"?"`` for an unnamed base.
    """

    typed_calls: list[dict[str, str]] = field(default_factory=list)
    class_bases: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class _Context:
    """Module-wide lookup data shared by every scope walk."""

    module: str
    name_map: dict[str, str]
    module_map: dict[str, str]
    top_names: frozenset[str]


@dataclass
class _Binding:
    """Everything one scope says about one name: declared annotations and the classes plain assignments give."""

    declared: set[str] = field(default_factory=set)
    assigned: set[str] = field(default_factory=set)
    foreign: bool = False  # bound by global/nonlocal: the scope does not own the name


def _dotted(node: ast.expr) -> str | None:
    """Return ``a.b.c`` for a Name/Attribute chain, else ``None``.

    Examples:
        >>> _dotted(ast.parse("pkg.mod.Cls", mode="eval").body)
        'pkg.mod.Cls'
        >>> _dotted(ast.parse("f().x", mode="eval").body) is None
        True
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _class_ref(node: ast.expr, ctx: _Context, scopes: list[dict[str, _Binding]]) -> str | None:
    """Resolve a class reference through the import map, refusing any first name a local scope rebinds.

    Examples:
        >>> ctx = _Context("pkg.m", {"Text": "lib.text.Text"}, {"rt": "lib.text"}, frozenset({"Local"}))
        >>> [_class_ref(ast.parse(s, mode="eval").body, ctx, []) for s in ("Text", "rt.Text", "Local", "Missing")]
        ['lib.text.Text', 'lib.text.Text', 'pkg.m.Local', None]
        >>> _class_ref(ast.parse("Text", mode="eval").body, ctx, [{"Text": _Binding()}]) is None
        True
    """
    chain = _dotted(node)
    if chain is None:
        return None
    first, _, rest = chain.partition(".")
    if any(first in scope for scope in scopes):
        return None
    if first in ctx.name_map:
        base = ctx.name_map[first]
    elif first in ctx.module_map:
        base = ctx.module_map[first]
    elif first in ctx.top_names:
        base = f"{ctx.module}.{first}"
    else:
        return None
    return f"{base}.{rest}" if rest else base


def _annotation_class(node: ast.expr | None, ctx: _Context, scopes: list[dict[str, _Binding]]) -> str | None:
    """Return the one class an annotation names, unwrapping ``Optional``, ``X | None`` and string annotations.

    Examples:
        >>> ctx = _Context("m", {"C": "pkg.C", "Optional": "typing.Optional", "Union": "typing.Union"}, {}, frozenset())
        >>> [_annotation_class(ast.parse(s, mode="eval").body, ctx, []) for s in
        ...  ("C", "'C'", "Optional[C]", "C | None", "Union[C, None]", "Union[C, int]", "None")]
        ['pkg.C', 'pkg.C', 'pkg.C', 'pkg.C', 'pkg.C', None, None]
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        node = _parse_string_annotation(node.value)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _single_non_none([node.left, node.right], ctx, scopes)
    if isinstance(node, ast.Subscript):
        return _subscript_class(node, ctx, scopes)
    if isinstance(node, (ast.Name, ast.Attribute)):
        return _class_ref(node, ctx, scopes)
    return None


def _parse_string_annotation(text: str) -> ast.expr | None:
    """Parse a quoted annotation into its expression, or ``None`` when it is not valid Python."""
    try:
        return ast.parse(text.strip(), mode="eval").body
    except SyntaxError:
        return None


def _subscript_class(node: ast.Subscript, ctx: _Context, scopes: list[dict[str, _Binding]]) -> str | None:
    """Return the class of ``Optional[C]``, ``Union[C, None]``, ``Annotated[C, ...]`` or a generic ``C[T]``."""
    head = (_dotted(node.value) or "").rpartition(".")[2]
    args = list(node.slice.elts) if isinstance(node.slice, ast.Tuple) else [node.slice]
    if head == "Union":
        return _single_non_none(args, ctx, scopes)
    if head in _UNWRAP_ONE:
        return _annotation_class(args[0], ctx, scopes)
    return _class_ref(node.value, ctx, scopes)


def _single_non_none(parts: list[ast.expr], ctx: _Context, scopes: list[dict[str, _Binding]]) -> str | None:
    """Return the class of the one non-``None`` member of a union, else ``None``."""
    rest = [part for part in parts if not (isinstance(part, ast.Constant) and part.value is None)]
    return _annotation_class(rest[0], ctx, scopes) if len(rest) == 1 else None


def _ctor_class(value: ast.expr | None, ctx: _Context, scopes: list[dict[str, _Binding]]) -> str | None:
    """Return the class a ``Class(...)`` expression constructs, ``""`` for ``None``, or ``_OPAQUE`` otherwise."""
    if value is None:
        return _OPAQUE
    if isinstance(value, ast.Constant) and value.value is None:
        return ""
    if isinstance(value, ast.Call):
        return _class_ref(value.func, ctx, scopes) or _OPAQUE
    return _OPAQUE


def _type_of(binding: _Binding) -> tuple[str, str] | None:
    """Return ``(class, rule)`` for one name's binding, or ``None`` when it names no single class.

    A declared annotation governs when every annotation agrees; otherwise every plain binding must construct the same
    class (``None`` aside).

    Examples:
        >>> _type_of(_Binding(declared={"pkg.C"}, assigned={_OPAQUE}))
        ('pkg.C', 'annotation')
        >>> _type_of(_Binding(assigned={"pkg.C", ""}))
        ('pkg.C', 'constructor')
        >>> [_type_of(b) for b in (_Binding(assigned={"pkg.C", "pkg.D"}), _Binding(assigned={"pkg.C", _OPAQUE}))]
        [None, None]
    """
    if binding.foreign:
        return None
    if binding.declared:
        return (next(iter(binding.declared)), TypedCallRule.ANNOTATION) if len(binding.declared) == 1 else None
    classes = binding.assigned - {""}
    if len(classes) == 1 and _OPAQUE not in classes:
        return next(iter(classes)), TypedCallRule.CONSTRUCTOR
    return None


def _own_nodes(body: list[ast.stmt]) -> Iterator[ast.AST]:
    """Yield every node of *body* that belongs to its scope: nested def/class/lambda bodies are not entered."""
    stack: list[ast.AST] = list(reversed(body))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            stack.extend(reversed([*node.decorator_list, *getattr(node, "bases", []), *_defaults(node)]))
            continue
        if isinstance(node, ast.Lambda):
            stack.extend(reversed(_defaults(node)))
            continue
        stack.extend(reversed(list(ast.iter_child_nodes(node))))


def _defaults(node: ast.AST) -> list[ast.expr]:
    """Return a def's or lambda's default expressions, which evaluate in the enclosing scope."""
    args = getattr(node, "args", None)
    if not isinstance(args, ast.arguments):
        return []
    return [*args.defaults, *(d for d in args.kw_defaults if d is not None)]


def _target_names(target: ast.expr) -> Iterator[str]:
    """Yield every plain name a (possibly nested or starred) assignment target binds."""
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            yield from _target_names(element)
    elif isinstance(target, ast.Starred):
        yield from _target_names(target.value)


def _scope_bindings(
    func: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda, ctx: _Context, outer: list[dict[str, _Binding]]
) -> dict[str, _Binding]:
    """Collect what one function scope says about each name it binds."""
    scope: dict[str, _Binding] = {}
    args = func.args
    for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
        cls = _annotation_class(arg.annotation, ctx, outer)
        binding = scope.setdefault(arg.arg, _Binding())
        (binding.declared.add(cls) if cls else binding.assigned.add(_OPAQUE))
    for arg in (args.vararg, args.kwarg):
        if arg is not None:
            scope.setdefault(arg.arg, _Binding()).assigned.add(_OPAQUE)
    body = func.body if isinstance(func.body, list) else [ast.Expr(func.body)]
    deferred: list[tuple[str, ast.expr | None, bool]] = []
    for node in _own_nodes(body):
        deferred.extend(_bindings_of(node, scope))
    chain = [*outer, scope]
    for name, value, declared in deferred:
        binding = scope.setdefault(name, _Binding())
        if declared:
            cls = _annotation_class(value, ctx, chain)
            (binding.declared.add(cls) if cls else binding.assigned.add(_OPAQUE))
        else:
            binding.assigned.add(_ctor_class(value, ctx, chain))
    return scope


def _bindings_of(node: ast.AST, scope: dict[str, _Binding]) -> Iterator[tuple[str, ast.expr | None, bool]]:
    """Yield ``(name, value_or_annotation, is_annotation)`` for each name *node* binds; ``value`` None = opaque."""
    if isinstance(node, (ast.Global, ast.Nonlocal)):
        for name in node.names:
            scope.setdefault(name, _Binding()).foreign = True
        return
    if isinstance(node, ast.Assign):
        for target in node.targets:
            value = node.value if isinstance(target, ast.Name) else None
            yield from ((name, value, False) for name in _target_names(target))
        return
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        yield node.target.id, node.annotation, True
        if node.value is not None:
            yield node.target.id, node.value, False
        return
    if isinstance(node, ast.NamedExpr):
        yield node.target.id, node.value, False
        return
    yield from ((name, None, False) for name in _opaque_names(node))


def _opaque_names(node: ast.AST) -> Iterator[str]:
    """Yield the names a binding construct other than plain or annotated assignment binds to no known class."""
    if isinstance(node, (ast.AugAssign, ast.For, ast.AsyncFor, ast.comprehension)):
        yield from _target_names(node.target)
    elif isinstance(node, (ast.With, ast.AsyncWith)):
        yield from (name for item in node.items if item.optional_vars for name in _target_names(item.optional_vars))
    elif isinstance(node, ast.Delete):
        yield from (name for target in node.targets for name in _target_names(target))
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        yield from ((alias.asname or alias.name).split(".")[0] for alias in node.names)
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        yield node.name
    elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name:
        yield node.name
    elif isinstance(node, ast.MatchMapping) and node.rest:
        yield node.rest


def _lookup(name: str, scopes: list[dict[str, _Binding]]) -> tuple[str, str] | None:
    """Return the class of *name* from the innermost scope binding it, or ``None``."""
    for scope in reversed(scopes):
        if name in scope:
            return _type_of(scope[name])
    return None


@dataclass
class _ClassFrame:
    """The enclosing top-level class of a method walk."""

    name: str
    self_name: str | None
    attributes: dict[str, _Binding]


def _receiver(
    value: ast.expr, ctx: _Context, scopes: list[dict[str, _Binding]], frame: _ClassFrame | None
) -> tuple[str, str] | None:
    """Return ``(class, rule)`` for the receiver of a method call, or ``None`` when it is not known."""
    if isinstance(value, ast.Name):
        # plain self.method(): the recorded self edge plus the base lookup of call_inference handles it
        is_self = frame is not None and value.id == frame.self_name
        return None if is_self else _lookup(value.id, scopes)
    if isinstance(value, ast.Attribute):
        return _attribute_receiver(value, frame)
    if isinstance(value, ast.Call):
        return _call_receiver(value, ctx, scopes, frame)
    return None


def _attribute_receiver(value: ast.Attribute, frame: _ClassFrame | None) -> tuple[str, str] | None:
    """Return the class of a ``self.<attr>`` receiver from the enclosing class's attribute bindings."""
    if frame is None or not (isinstance(value.value, ast.Name) and value.value.id == frame.self_name):
        return None
    binding = frame.attributes.get(value.attr)
    typed = _type_of(binding) if binding is not None else None
    return (typed[0], TypedCallRule.ATTRIBUTE) if typed else None


def _call_receiver(
    value: ast.Call, ctx: _Context, scopes: list[dict[str, _Binding]], frame: _ClassFrame | None
) -> tuple[str, str] | None:
    """Return the class of a zero-argument ``super()`` or a ``Class(...)`` receiver.

    ``super(Cls, obj)`` starts the method lookup after ``Cls`` in the MRO of ``obj``'s type, which the enclosing class
    does not determine, so a ``super`` call with any argument yields no record.
    """
    if isinstance(value.func, ast.Name) and value.func.id == "super":
        shadowed = any("super" in scope for scope in scopes)
        if frame is None or shadowed or value.args or value.keywords:
            return None
        return f"{ctx.module}.{frame.name}", TypedCallRule.SUPER
    cls = _class_ref(value.func, ctx, scopes)
    return (cls, TypedCallRule.CONSTRUCTOR) if cls else None


def _walk_function(
    func: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda,
    ctx: _Context,
    outer: list[dict[str, _Binding]],
    frame: _ClassFrame | None,
    emit: list[tuple[str, str, str]],
) -> None:
    """Emit ``(class, method, rule)`` for every typed method call in *func*, nested functions included."""
    scopes = [*outer, _scope_bindings(func, ctx, outer)]
    body = func.body if isinstance(func.body, list) else [ast.Expr(func.body)]
    for node in _own_nodes(body):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            _walk_function(node, ctx, scopes, frame, emit)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            typed = _receiver(node.func.value, ctx, scopes, frame)
            if typed is not None:
                emit.append((typed[0], node.func.attr, typed[1]))


def _self_name(method: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    """Return the instance parameter of a method, or ``None`` for a static or class method."""
    decorators = {(_dotted(d) or "").rpartition(".")[2] for d in method.decorator_list}
    params = [*method.args.posonlyargs, *method.args.args]
    if decorators & _NOT_INSTANCE or not params:
        return None
    return params[0].arg


def _class_attributes(node: ast.ClassDef, ctx: _Context) -> dict[str, _Binding]:
    """Collect what the class body and its methods say about each instance attribute ``self.<name>``."""
    attributes: dict[str, _Binding] = {}
    for statement in node.body:
        if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
            cls = _annotation_class(statement.annotation, ctx, [])
            binding = attributes.setdefault(statement.target.id, _Binding())
            (binding.declared.add(cls) if cls else binding.assigned.add(_OPAQUE))
    for method in node.body:
        if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        self_name = _self_name(method)
        if self_name is None:
            continue
        scopes = [_scope_bindings(method, ctx, [])]
        for child in ast.walk(method):
            _record_attribute(child, self_name, attributes, ctx, scopes)
    return attributes


def _record_attribute(
    node: ast.AST, self_name: str, attributes: dict[str, _Binding], ctx: _Context, scopes: list[dict[str, _Binding]]
) -> None:
    """Fold one ``self.<name> = ...`` or ``self.<name>: T = ...`` statement into *attributes*."""
    targets: list[ast.expr] = []
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        targets = [node.target]
    for target in targets:
        if not (
            isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == self_name
        ):
            if isinstance(target, (ast.Tuple, ast.List)):
                _poison_attributes(target, self_name, attributes)
            continue
        binding = attributes.setdefault(target.attr, _Binding())
        if isinstance(node, ast.AnnAssign):
            cls = _annotation_class(node.annotation, ctx, scopes)
            (binding.declared.add(cls) if cls else binding.assigned.add(_OPAQUE))
            if node.value is not None:
                binding.assigned.add(_value_class(node.value, ctx, scopes))
        elif isinstance(node, ast.Assign):
            binding.assigned.add(_value_class(node.value, ctx, scopes))
        else:
            binding.assigned.add(_OPAQUE)


def _poison_attributes(target: ast.expr, self_name: str, attributes: dict[str, _Binding]) -> None:
    """Mark every ``self.<name>`` inside a tuple/list unpacking target as bound to no known class."""
    for element in ast.walk(target):
        if isinstance(element, ast.Attribute) and isinstance(element.value, ast.Name) and element.value.id == self_name:
            attributes.setdefault(element.attr, _Binding()).assigned.add(_OPAQUE)


def _value_class(value: ast.expr, ctx: _Context, scopes: list[dict[str, _Binding]]) -> str:
    """Return the class an attribute value holds: a constructor, ``None`` (``""``), or a typed local name."""
    if isinstance(value, ast.Name):
        typed = _lookup(value.id, scopes)
        return typed[0] if typed else _OPAQUE
    return _ctor_class(value, ctx, scopes)


def _base_names(node: ast.ClassDef, ctx: _Context) -> list[str]:
    """Return a class's bases dotted as imported; ``"?"`` for one the import map cannot name."""
    bases: list[str] = []
    for base in node.bases:
        target = base.value if isinstance(base, ast.Subscript) else base
        bases.append(_class_ref(target, ctx, []) or (_dotted(target) or _OPAQUE))
    return bases


def extract_receiver_facts(
    tree: ast.Module, module_name: str, name_map: dict[str, str], module_map: dict[str, str]
) -> ReceiverFacts:
    """Collect typed method calls and class bases from one module's top-level functions and classes.

    Args:
        tree: parsed AST of the module.
        module_name: dotted module name.
        name_map: local-name -> fully-qualified-name from the import scope.
        module_map: first-component -> full module path from the import scope.

    Returns:
        The module's receiver facts; records are sorted and unique.

    Examples:
        >>> src = "from pkg.text import Text\\ndef f(t: Text):\\n    t.render()\\n    Text().split()\\n"
        >>> facts = extract_receiver_facts(ast.parse(src), "pkg.m", {"Text": "pkg.text.Text"}, {})
        >>> [(r["caller"], r["cls"], r["method"], r["rule"]) for r in facts.typed_calls]
        [('f', 'pkg.text.Text', 'render', 'annotation'), ('f', 'pkg.text.Text', 'split', 'constructor')]
    """
    top_names = frozenset(
        node.name for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    )
    ctx = _Context(module_name, name_map, module_map, top_names)
    facts = ReceiverFacts()
    records: set[tuple[str, str, str, str]] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            emitted: list[tuple[str, str, str]] = []
            _walk_function(node, ctx, [], None, emitted)
            records.update((node.name, *item) for item in emitted)
        elif isinstance(node, ast.ClassDef):
            if node.bases:
                facts.class_bases[node.name] = _base_names(node, ctx)
            attributes = _class_attributes(node, ctx)
            for method in node.body:
                if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    emitted = []
                    frame = _ClassFrame(node.name, _self_name(method), attributes)
                    _walk_function(method, ctx, [], frame, emitted)
                    records.update((f"{node.name}.{method.name}", *item) for item in emitted)
    facts.typed_calls = [
        {"caller": caller, "cls": cls, "method": method, "rule": rule} for caller, cls, method, rule in sorted(records)
    ]
    return facts
