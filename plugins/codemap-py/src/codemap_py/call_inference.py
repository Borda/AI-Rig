"""Add call edges to methods whose receiver class is known, resolved against the whole index.

Per-file scanning cannot tell whether an imported name is a class, where a dotted name splits into module and symbol,
or which base class defines an inherited method. This pass sees every indexed module, so it can:

- turn each persisted ``typed_calls`` record (receiver class known from a constructor, an annotation, an instance
  attribute or ``super()``) into an edge to the method of that class or of the indexed base that defines it;
- repair a recorded class-member edge that names no indexed symbol: ``Engine.build()`` through an imported class is
  recorded as ``pkg.engine.Engine::build`` and an inherited ``self.save()`` as ``pkg.m::Child.save``; both become the
  defining method (``pkg.engine::Engine.build``, ``pkg.base::Base.save``).

Every edge added here carries an ``inferred`` key naming its rule, marking it lower-confidence than a recorded edge.
The pass never guesses: the receiver must be an indexed class, the method must be found in its linearised hierarchy
before any base the index does not hold, and an inconsistent hierarchy yields nothing. Existing edges stay untouched;
inferred edges are removed and recomputed on every run, so an incremental scan never carries a stale one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from codemap_py.schema import Resolution, SymbolType

__all__ = ["INFERRED_KEY", "InferenceStats", "infer_call_edges"]

#: Edge key marking an inferred edge; its value names the rule that produced it.
INFERRED_KEY = "inferred"
#: Rule name for a class-qualified call repaired through the class hierarchy (``Class.method()``).
_RULE_CLASS = "class"
#: Rule name for an inherited ``self.method()`` repaired through indexed bases.
_RULE_BASE = "base"
#: Rule name persisted by the scanner for ``super().method()`` records.
_RULE_SUPER = "super"
#: Bases that define no method a project call can name; skipping them keeps a hierarchy fully indexed.
_TRANSPARENT_BASES = frozenset(
    {
        "object",
        "typing.Generic",
        "typing.Protocol",
        "typing_extensions.Protocol",
        "abc.ABC",
        "typing.NamedTuple",
    }
)
#: Upper bound on one class's linearisation; a deeper hierarchy yields no inferred edge.
_MAX_MRO = 64
#: Linearisation entry standing for a base the index does not hold.
_OPAQUE_PREFIX = "?"
#: Call resolutions whose recorded target the repair step reads.
_REPAIRABLE = frozenset({Resolution.IMPORT, Resolution.LOCAL, Resolution.SELF})


@dataclass
class InferenceStats:
    """Counts of one pass, for diagnostics and tests.

    Attributes:
        added: inferred edges written, by rule.
        unresolved: typed-call records and dangling class-member edges that stayed without an edge.
    """

    added: dict[str, int] = field(default_factory=dict)
    unresolved: int = 0


@dataclass
class _ClassTable:
    """Indexed classes with their methods and bases, plus the alias map used to name them."""

    methods: dict[str, set[str]]
    bases: dict[str, list[str]]
    aliases: dict[str, str]
    mro_cache: dict[str, list[str] | None] = field(default_factory=dict)

    def resolve(self, dotted: str) -> str | None:
        """Return the indexed ``module::Class`` a dotted class name denotes, following symbol aliases.

        Examples:
            >>> table = _ClassTable({"pkg.text::Text": set()}, {}, {"pkg::Text": "pkg.text::Text"})
            >>> [table.resolve(name) for name in ("pkg.text.Text", "pkg.Text", "pkg.text.Missing", "Text")]
            ['pkg.text::Text', 'pkg.text::Text', None, None]
        """
        module, dot, name = dotted.rpartition(".")
        if not dot:
            return None
        qname = f"{module}::{name}"
        qname = self.aliases.get(qname, qname)
        return qname if qname in self.methods else None

    def mro(self, qname: str) -> list[str] | None:
        """Return the C3 linearisation of an indexed class; unindexed bases appear as ``?<name>`` leaves.

        Examples:
            >>> table = _ClassTable({"m::A": set(), "m::B": set(), "m::C": set()}, {"m::B": ["m.A"],
            ...     "m::C": ["m.B", "ext.Mixin"]}, {})
            >>> table.mro("m::C")
            ['m::C', 'm::B', 'm::A', '?ext.Mixin']
        """
        if qname in self.mro_cache:
            return self.mro_cache[qname]
        self.mro_cache[qname] = None  # cycle guard: a class reached again while linearising yields nothing
        parents: list[list[str]] = []
        direct: list[str] = []
        for base in self.bases.get(qname, []):
            if base in _TRANSPARENT_BASES:
                continue
            resolved = self.resolve(base)
            if resolved is None:
                parents.append([f"{_OPAQUE_PREFIX}{base}"])
                direct.append(f"{_OPAQUE_PREFIX}{base}")
                continue
            linear = self.mro(resolved)
            if linear is None:
                return None
            parents.append(linear)
            direct.append(resolved)
        merged = _c3_merge([*parents, direct])
        result = None if merged is None or len(merged) >= _MAX_MRO else [qname, *merged]
        self.mro_cache[qname] = result
        return result

    def find(self, qname: str, method: str, *, skip_self: bool = False) -> str | None:
        """Return ``module::Class.method`` of the first class in the hierarchy defining *method*, else ``None``.

        Examples:
            >>> table = _ClassTable({"m::A": {"run"}, "m::B": set()}, {"m::B": ["m.A"]}, {})
            >>> table.find("m::B", "run"), table.find("m::A", "run", skip_self=True)
            ('m::A.run', None)
        """
        linear = self.mro(qname)
        if linear is None:
            return None
        for entry in linear[1:] if skip_self else linear:
            if entry.startswith(_OPAQUE_PREFIX):
                return None
            if method in self.methods[entry]:
                return f"{entry}.{method}"
        return None


def _c3_merge(sequences: list[list[str]]) -> list[str] | None:
    """Merge linearisations by the C3 rule; ``None`` when no consistent order exists.

    Examples:
        >>> _c3_merge([["B", "A"], ["C", "A"], ["B", "C"]])
        ['B', 'C', 'A']
        >>> _c3_merge([["A", "B"], ["B", "A"], ["A", "B"]]) is None
        True
    """
    pending = [list(seq) for seq in sequences if seq]
    merged: list[str] = []
    while pending:
        head = next((seq[0] for seq in pending if not any(seq[0] in other[1:] for other in pending)), None)
        if head is None:
            return None
        merged.append(head)
        pending = [[item for item in seq if item != head] for seq in pending]
        pending = [seq for seq in pending if seq]
    return merged


def _class_table(modules: list[dict], aliases: dict[str, str]) -> _ClassTable:
    """Collect every indexed top-level class with its method names and recorded bases."""
    methods: dict[str, set[str]] = {}
    bases: dict[str, list[str]] = {}
    for module in modules:
        if module.get("status") != "ok":
            continue
        name = module["name"]
        recorded = module.get("class_bases") or {}
        for symbol in module.get("symbols", []):
            if symbol.get("type") == SymbolType.CLASS:
                qname = f"{name}::{symbol['qualified_name']}"
                methods.setdefault(qname, set())
                bases[qname] = list(recorded.get(symbol["qualified_name"], []))
        for symbol in module.get("symbols", []):
            owner, dot, method = symbol["qualified_name"].partition(".")
            if dot and symbol.get("type") == SymbolType.METHOD and f"{name}::{owner}" in methods:
                methods[f"{name}::{owner}"].add(method)
    return _ClassTable(methods=methods, bases=bases, aliases=aliases)


def _strip_inferred(modules: list[dict]) -> None:
    """Remove edges a previous pass inferred, so the pass recomputes them from current facts."""
    for module in modules:
        for symbol in module.get("symbols", []):
            calls = symbol.get("calls")
            if calls and any(INFERRED_KEY in edge for edge in calls):
                symbol["calls"] = [edge for edge in calls if INFERRED_KEY not in edge]


def _dangling_member(edge: dict, known: set[str], aliases: dict[str, str]) -> tuple[str, str] | None:
    """Split a recorded edge naming no indexed symbol into ``(dotted class, method)``, else ``None``.

    Examples:
        >>> _dangling_member({"target": "pkg.text.Text::from_ansi", "resolution": "import"}, set(), {})
        ('pkg.text.Text', 'from_ansi')
        >>> _dangling_member({"target": "pkg.m::Child.save", "resolution": "self"}, set(), {})
        ('pkg.m.Child', 'save')
        >>> _dangling_member({"target": "pkg.m::f", "resolution": "local"}, {"pkg.m::f"}, {}) is None
        True
    """
    if edge.get("resolution") not in _REPAIRABLE:
        return None
    target = edge.get("target", "")
    module, sep, symbol = target.partition("::")
    if not sep or aliases.get(target, target) in known or symbol.count(".") > 1:
        return None
    owner, dot, method = symbol.rpartition(".")
    if dot:
        return f"{module}.{owner}", method
    return (module, symbol) if "." in module else None


def _edge(caller_module: str, target: str, rule: str) -> dict:
    """Build an inferred edge; same-module targets are ``local``, others ``import``, as for recorded edges."""
    resolution = Resolution.LOCAL if target.partition("::")[0] == caller_module else Resolution.IMPORT
    return {"target": target, "resolution": resolution.value, INFERRED_KEY: rule}


def _module_edges(module: dict, table: _ClassTable, known: set[str], stats: InferenceStats) -> dict[str, list[dict]]:
    """Compute the inferred edges of one module, keyed by caller qualified name."""
    name = module["name"]
    found: dict[str, list[dict]] = {}
    for symbol in module.get("symbols", []):
        for edge in symbol.get("calls", []):
            member = _dangling_member(edge, known, table.aliases)
            if member is None:
                continue
            owner = table.resolve(member[0])
            target = table.find(owner, member[1]) if owner else None
            rule = _RULE_BASE if edge.get("resolution") == Resolution.SELF else _RULE_CLASS
            _collect(found, symbol["qualified_name"], name, target, rule, stats)
    for record in module.get("typed_calls") or []:
        owner = table.resolve(record["cls"])
        skip = record["rule"] == _RULE_SUPER
        target = table.find(owner, record["method"], skip_self=skip) if owner else None
        _collect(found, record["caller"], name, target, record["rule"], stats)
    return found


def _collect(
    found: dict[str, list[dict]], caller: str, module: str, target: str | None, rule: str, stats: InferenceStats
) -> None:
    """Record one inferred edge for *caller*, or count the miss."""
    if target is None:
        stats.unresolved += 1
        return
    found.setdefault(caller, []).append(_edge(module, target, rule))


def infer_call_edges(modules: list[dict], symbol_aliases: dict[str, str]) -> InferenceStats:
    """Append inferred method-call edges to every ok module's symbols, in place.

    Args:
        modules: module entries after alias canonicalization; read for classes, mutated only in ``calls``.
        symbol_aliases: canonical ``alias_qname -> qname`` map from the alias pass.

    Returns:
        Counts of edges added per rule and of receivers left unresolved.

    Examples:
        >>> mods = [
        ...     {"name": "p.base", "status": "ok", "symbols": [
        ...         {"qualified_name": "Base", "type": "class", "calls": []},
        ...         {"qualified_name": "Base.save", "type": "method", "calls": []}]},
        ...     {"name": "p.use", "status": "ok", "typed_calls": [
        ...         {"caller": "run", "cls": "p.base.Base", "method": "save", "rule": "annotation"}],
        ...      "symbols": [{"qualified_name": "run", "type": "function", "calls": []}]}]
        >>> infer_call_edges(mods, {}).added, mods[1]["symbols"][0]["calls"]
        ({'annotation': 1}, [{'target': 'p.base::Base.save', 'resolution': 'import', 'inferred': 'annotation'}])
    """
    _strip_inferred(modules)
    ok = [module for module in modules if module.get("status") == "ok"]
    table = _class_table(ok, symbol_aliases)
    known = {f"{module['name']}::{symbol['qualified_name']}" for module in ok for symbol in module.get("symbols", [])}
    stats = InferenceStats()
    for module in ok:
        found = _module_edges(module, table, known, stats)
        for symbol in module.get("symbols", []):
            additions = found.get(symbol["qualified_name"])
            if not additions:
                continue
            present = {table.aliases.get(edge["target"], edge["target"]) for edge in symbol.get("calls", [])}
            for edge in additions:
                if edge["target"] in present:
                    continue
                present.add(edge["target"])
                symbol.setdefault("calls", []).append(edge)
                stats.added[edge[INFERRED_KEY]] = stats.added.get(edge[INFERRED_KEY], 0) + 1
    return stats
