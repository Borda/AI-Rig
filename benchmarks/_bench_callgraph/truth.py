"""Build the call-edge truth of one module set in the caller vocabulary that ``fn-rdeps`` reports.

Truth rules:

- A truth site is an ``ast.Call`` whose ``func`` is an ``ast.Name`` or an ``ast.Attribute``; the reference backend
  resolves the terminal identifier at that position to its definition. Imports, callback values, base-class and
  annotation uses are never ``ast.Call`` function positions, so they never enter the truth.
- Targets are public functions and methods: top-level ``def`` statements and ``def`` statements directly in the body
  of a top-level class, with no qualified-name component starting with ``_``.
- The caller of a site is the codemap-py symbol that encloses it: a top-level function (nested definitions, its
  decorators and defaults included), ``Class.method`` for a method of a top-level class (its decorators included), or
  ``Class`` for a class decorator and any other class-body statement. Module-scope sites, class bases and keywords
  have no enclosing symbol; they are kept in a separate unattributed bucket, never scored.
- Edges are deduplicated per (target, caller); every call site of an edge is kept for classification.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

_TEXT_LIMIT = 120
_FunctionNode = (ast.FunctionDef, ast.AsyncFunctionDef)


@dataclass(frozen=True, slots=True)
class Definition:
    """A function or method that can be an ``fn-rdeps`` target.

    Attributes:
        module: Module name as the index records it.
        qualified_name: ``name`` or ``Class.name``.
        path: Repository-relative POSIX path of the defining file.
        line: Line of the ``def`` keyword (1-based).
        is_method: Whether the definition sits directly in a top-level class body.
        decorated: Whether the definition carries any decorator.
    """

    module: str
    qualified_name: str
    path: str
    line: int
    is_method: bool
    decorated: bool

    @property
    def qname(self) -> str:
        """Return the ``module::qualified_name`` target spelling.

        Examples:
            >>> Definition("pkg.mod", "Cls.run", "pkg/mod.py", 3, True, False).qname
            'pkg.mod::Cls.run'
        """
        return f"{self.module}::{self.qualified_name}"

    @property
    def public(self) -> bool:
        """Return whether no qualified-name component starts with an underscore.

        Examples:
            >>> Definition("m", "Cls._hidden", "m.py", 1, True, False).public
            False
        """
        return not any(part.startswith("_") for part in self.qualified_name.split("."))


@dataclass(frozen=True, slots=True)
class CallSite:
    """One ``ast.Call`` in a module.

    Attributes:
        path: Repository-relative POSIX path of the file.
        caller: Enclosing symbol as ``module::qualified_name``, or ``None`` at module scope.
        line: Line of the callee's terminal identifier (1-based).
        column: UTF-8 byte offset of the callee's terminal identifier in its line, as ``ast`` reports it.
        callee_name: Terminal identifier of a ``Name`` or ``Attribute`` callee, ``None`` for any other callee.
        callee_text: Source text of the callee, truncated.
        base_text: Source text of an ``Attribute`` callee's base, ``None`` otherwise.
    """

    path: str
    caller: str | None
    line: int
    column: int
    callee_name: str | None
    callee_text: str
    base_text: str | None

    def to_dict(self) -> dict[str, object]:
        """Serialize for the JSON result.

        Examples:
            >>> CallSite("m.py", None, 1, 0, "f", "f", None).to_dict()["callee_text"]
            'f'
        """
        return {
            "path": self.path,
            "caller": self.caller,
            "line": self.line,
            "column": self.column,
            "callee_name": self.callee_name,
            "callee_text": self.callee_text,
            "base_text": self.base_text,
        }


@dataclass(frozen=True, slots=True)
class DefLocation:
    """Where the reference backend says a name is defined.

    Attributes:
        path: Repository-relative POSIX path, or ``None`` outside the repository.
        line: Line of the definition's name (1-based).
    """

    path: str | None
    line: int


@dataclass(slots=True)
class ModuleFacts:
    """Definitions, call sites and import bindings of one parsed module.

    Attributes:
        module: Module name as the index records it.
        path: Repository-relative POSIX path.
        definitions: Every function and method in the codemap-py symbol vocabulary, public or not.
        sites: Every call site in the module.
        imports: Local name to the absolute dotted name it refers to (``from M import x`` gives ``M.x``,
            ``import a.b as m`` gives ``a.b``).
    """

    module: str
    path: str
    definitions: list[Definition] = field(default_factory=list)
    sites: list[CallSite] = field(default_factory=list)
    imports: dict[str, str] = field(default_factory=dict)


class ReferenceBackend(Protocol):
    """Resolve call-site positions in one file to their definitions."""

    def resolve(self, path: str, positions: Sequence[tuple[int, int]]) -> list[tuple[DefLocation, ...]]:
        """Return the definitions of the name at each ``(line, column)`` of *path*, in order.

        An empty tuple means the backend found no definition for that position.
        """
        ...


def _text(node: ast.AST) -> str:
    """Return the source text of *node*, truncated to a fixed length."""
    return ast.unparse(node)[:_TEXT_LIMIT]


def _site(node: ast.Call, path: str, caller: str | None) -> CallSite:
    """Describe one call, locating the callee's terminal identifier."""
    func = node.func
    if isinstance(func, ast.Name):
        return CallSite(path, caller, func.lineno, func.col_offset, func.id, _text(func), None)
    if isinstance(func, ast.Attribute) and func.end_lineno is not None and func.end_col_offset is not None:
        column = func.end_col_offset - len(func.attr)
        return CallSite(path, caller, func.end_lineno, column, func.attr, _text(func), _text(func.value))
    return CallSite(path, caller, func.lineno, func.col_offset, None, _text(func), None)


def _calls_in(nodes: Iterable[ast.AST], path: str, caller: str | None) -> Iterator[CallSite]:
    """Yield every call under *nodes*, attributed to *caller*."""
    for root in nodes:
        for node in ast.walk(root):
            if isinstance(node, ast.Call):
                yield _site(node, path, caller)


def _definition(node: ast.FunctionDef | ast.AsyncFunctionDef, module: str, path: str, owner: str) -> Definition:
    """Describe one function or method definition."""
    qualified = f"{owner}.{node.name}" if owner else node.name
    return Definition(module, qualified, path, node.lineno, bool(owner), bool(node.decorator_list))


def _class_facts(node: ast.ClassDef, facts: ModuleFacts) -> None:
    """Record a top-level class's methods and the calls each class-level symbol encloses."""
    class_caller = f"{facts.module}::{node.name}"
    facts.sites.extend(_calls_in(node.decorator_list, facts.path, class_caller))
    facts.sites.extend(_calls_in([*node.bases, *node.keywords], facts.path, None))
    for child in node.body:
        if isinstance(child, _FunctionNode):
            definition = _definition(child, facts.module, facts.path, node.name)
            facts.definitions.append(definition)
            facts.sites.extend(_calls_in([child], facts.path, definition.qname))
        else:
            facts.sites.extend(_calls_in([child], facts.path, class_caller))


def _absolute_module(module: str, is_package: bool, level: int, target: str | None) -> str:
    """Resolve a possibly relative import target against the importing module.

    Examples:
        >>> _absolute_module("pkg.sub.mod", False, 1, "other")
        'pkg.sub.other'
        >>> _absolute_module("pkg.sub", True, 2, None)
        'pkg'
        >>> _absolute_module("pkg.mod", False, 0, "json")
        'json'
    """
    if level == 0:
        return target or ""
    parts = module.split(".")
    keep = len(parts) - level + (1 if is_package else 0)
    base = parts[: max(keep, 0)]
    return ".".join([*base, target] if target else base)


def _import_bindings(tree: ast.Module, module: str, is_package: bool) -> dict[str, str]:
    """Map each name an import statement binds anywhere in the module to the dotted name it refers to.

    ``from M import x as y`` binds ``y`` to ``M.x``; ``import a.b as m`` binds ``m`` to ``a.b``; ``import a.b`` binds
    ``a`` to ``a``. Star imports bind nothing.
    """
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            source = _absolute_module(module, is_package, node.level, node.module)
            for alias in node.names:
                if alias.name != "*":
                    bindings[alias.asname or alias.name] = f"{source}.{alias.name}" if source else alias.name
        elif isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                bindings[local] = alias.name if alias.asname else local
    return bindings


def module_facts(tree: ast.Module, module: str, path: str) -> ModuleFacts:
    """Collect the definitions, call sites and import bindings of one module.

    Args:
        tree: Parsed module.
        module: Module name as the index records it.
        path: Repository-relative POSIX path; ``__init__.py`` marks a package.

    Returns:
        The module's facts, call sites in the codemap-py caller vocabulary.

    Examples:
        >>> facts = module_facts(ast.parse("def f():\\n    g()\\n"), "m", "m.py")
        >>> [(site.caller, site.callee_name) for site in facts.sites]
        [('m::f', 'g')]
    """
    facts = ModuleFacts(module=module, path=path)
    facts.imports = _import_bindings(tree, module, path.endswith("__init__.py"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            _class_facts(node, facts)
        elif isinstance(node, _FunctionNode):
            definition = _definition(node, module, path, "")
            facts.definitions.append(definition)
            facts.sites.extend(_calls_in([node], path, definition.qname))
        else:
            facts.sites.extend(_calls_in([node], path, None))
    facts.sites.sort(key=lambda site: (site.line, site.column))
    return facts


def candidate_names(facts: ModuleFacts, target_names: frozenset[str]) -> frozenset[str]:
    """Return the callee identifiers worth resolving in one module.

    A site is resolved when its terminal identifier names a public target or is a name this module imported under an
    alias (``from m import f as g``), so an aliased call can still reach its definition.

    Examples:
        >>> facts = ModuleFacts("m", "m.py", imports={"g": "pkg", "h": "pkg"})
        >>> sorted(candidate_names(facts, frozenset({"f"})))
        ['f', 'g', 'h']
    """
    return target_names | frozenset(facts.imports)


@dataclass(slots=True)
class Truth:
    """Call-edge truth of one module set.

    Attributes:
        edges: Target qname to caller qname to the call sites of that edge, in source order.
        unattributed: Target qname to module-scope call sites that resolve to it (never scored).
        resolved_sites: Sites the backend resolved to at least one repository definition.
        unresolved_sites: Sites the backend returned no definition for.
        external_sites: Sites resolved only outside the repository or to a non-target definition.
    """

    edges: dict[str, dict[str, list[CallSite]]] = field(default_factory=dict)
    unattributed: dict[str, list[CallSite]] = field(default_factory=dict)
    resolved_sites: int = 0
    unresolved_sites: int = 0
    external_sites: int = 0

    def caller_count(self, target: str) -> int:
        """Return the number of distinct callers of *target*.

        Examples:
            >>> Truth().caller_count("m::f")
            0
        """
        return len(self.edges.get(target, {}))


def build_truth(
    sites: Sequence[CallSite],
    resolutions: Sequence[tuple[DefLocation, ...]],
    definitions: Mapping[tuple[str, int], Definition],
) -> Truth:
    """Turn resolved call sites into deduplicated (target, caller) edges.

    Args:
        sites: The resolved call sites.
        resolutions: Definitions per site, aligned with *sites*.
        definitions: Public target definitions keyed by ``(path, def line)``.

    Returns:
        The truth; a site that resolves to several targets adds an edge to each.

    Raises:
        ValueError: When *sites* and *resolutions* differ in length.

    Examples:
        >>> target = Definition("m", "f", "m.py", 1, False, False)
        >>> site = CallSite("n.py", "n::g", 3, 4, "f", "f", None)
        >>> truth = build_truth([site, site], [(DefLocation("m.py", 1),)] * 2, {("m.py", 1): target})
        >>> truth.caller_count("m::f"), len(truth.edges["m::f"]["n::g"])
        (1, 2)
    """
    if len(sites) != len(resolutions):
        raise ValueError(f"{len(sites)} sites but {len(resolutions)} resolutions; the backend must answer every site")
    truth = Truth()
    for site, found in zip(sites, resolutions):
        if not found:
            truth.unresolved_sites += 1
            continue
        targets = sorted(
            {definitions[key].qname for key in ((loc.path, loc.line) for loc in found) if key in definitions}
        )
        if not targets:
            truth.external_sites += 1
            continue
        truth.resolved_sites += 1
        for target in targets:
            if site.caller is None:
                truth.unattributed.setdefault(target, []).append(site)
            else:
                truth.edges.setdefault(target, {}).setdefault(site.caller, []).append(site)
    return truth
