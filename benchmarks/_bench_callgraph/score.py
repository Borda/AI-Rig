"""Score ``fn-rdeps`` callers against the call-edge truth and class every disagreement.

Units: a scored item is a distinct (target, caller) edge. Recall is truth edges ``fn-rdeps`` also reports over all
truth edges; precision is ``fn-rdeps`` edges the truth also holds over all ``fn-rdeps`` edges. Both carry a 95% Wilson
interval over edges of the sample, plus a population-weighted point estimate (stratum weight ``N_h / n_h``).

Miss class of a missed edge: the highest-precedence class among the edge's call sites, precedence ``dynamic``,
``instance_method``, ``alias``, ``re_export``, ``decorator``, ``other``. A site is:

- ``dynamic`` when its callee is neither a name nor an attribute;
- ``instance_method`` when the target is a method and the callee is an attribute whose base's last component is not
  the class name (``self.m()``, ``cls.m()``, ``super().m()``, ``obj.m()``);
- ``alias`` when the callee's terminal identifier differs from the target's name;
- ``re_export`` when the callee's head name is import-bound and the dotted name it spells differs from the target's
  defining ``module.qualified_name``;
- ``decorator`` when the target carries a decorator;
- ``other`` otherwise.

Extra class of an ``fn-rdeps`` edge the truth lacks, from the caller's call sites whose terminal identifier is the
target's name: ``reference_unresolved`` when the reference tool resolved at least one of them to nothing,
``resolved_elsewhere`` when it resolved all of them to other definitions, ``no_same_name_call`` when the caller has
no such call site.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum

from _bench_census.score import Rate

from _bench_callgraph.truth import CallSite, Definition, DefLocation


class MissClass(str, Enum):
    """Why a truth edge may be missing from ``fn-rdeps``, in precedence order."""

    DYNAMIC = "dynamic"
    INSTANCE_METHOD = "instance_method"
    ALIAS = "alias"
    RE_EXPORT = "re_export"
    DECORATOR = "decorator"
    OTHER = "other"


class ExtraClass(str, Enum):
    """Why an ``fn-rdeps`` edge may be missing from the truth."""

    REFERENCE_UNRESOLVED = "reference_unresolved"
    RESOLVED_ELSEWHERE = "resolved_elsewhere"
    NO_SAME_NAME_CALL = "no_same_name_call"


_PRECEDENCE = tuple(MissClass)


def _effective_dotted(site: CallSite, imports: Mapping[str, str]) -> str | None:
    """Return the dotted name an import-bound callee spells, or ``None`` when its head is not import-bound."""
    head, _sep, rest = site.callee_text.partition(".")
    referent = imports.get(head)
    if referent is None:
        return None
    return f"{referent}.{rest}" if rest else referent


def site_class(target: Definition, site: CallSite, imports: Mapping[str, str]) -> MissClass:
    """Return the class of one call site of a missed edge.

    Args:
        target: The target definition.
        site: One call site of the edge.
        imports: Import bindings of the caller's module.

    Returns:
        The site's class.

    Examples:
        >>> target = Definition("pkg.core", "Cls.run", "pkg/core.py", 5, True, False)
        >>> site_class(target, CallSite("a.py", "a::f", 1, 9, "run", "self.run", "self"), {})
        <MissClass.INSTANCE_METHOD: 'instance_method'>
        >>> plain = Definition("pkg.core", "helper", "pkg/core.py", 9, False, False)
        >>> site_class(plain, CallSite("a.py", "a::f", 1, 0, "helper", "helper", None), {"helper": "pkg.helper"})
        <MissClass.RE_EXPORT: 're_export'>
    """
    if site.callee_name is None:
        return MissClass.DYNAMIC
    terminal = target.qualified_name.rsplit(".", 1)[-1]
    class_name = target.qualified_name.split(".")[0]
    if target.is_method and site.base_text is not None and site.base_text.rsplit(".", 1)[-1] != class_name:
        return MissClass.INSTANCE_METHOD
    if site.callee_name != terminal:
        return MissClass.ALIAS
    spelled = _effective_dotted(site, imports)
    if spelled is not None and spelled != f"{target.module}.{target.qualified_name}":
        return MissClass.RE_EXPORT
    return MissClass.DECORATOR if target.decorated else MissClass.OTHER


def edge_class(target: Definition, sites: Sequence[CallSite], imports: Mapping[str, str]) -> MissClass:
    """Return the highest-precedence class among an edge's call sites.

    Examples:
        >>> target = Definition("m", "Cls.run", "m.py", 2, True, False)
        >>> by_class = CallSite("m.py", "m::f", 9, 4, "run", "Cls.run", "Cls")
        >>> sites = [by_class, CallSite("m.py", "m::f", 10, 9, "run", "self.run", "self")]
        >>> edge_class(target, sites, {})
        <MissClass.INSTANCE_METHOD: 'instance_method'>
    """
    found = {site_class(target, site, imports) for site in sites}
    return next(cls for cls in _PRECEDENCE if cls in found)


def instance_base(site: CallSite) -> str:
    """Return the base kind of an instance-method call: ``self``, ``cls``, ``super()`` or ``other``.

    Examples:
        >>> instance_base(CallSite("m.py", None, 1, 0, "run", "super().run", "super()"))
        'super()'
    """
    base = site.base_text or ""
    return base if base in {"self", "cls", "super()"} else "other"


def extra_class(target: Definition, caller_sites: Sequence[tuple[CallSite, tuple[DefLocation, ...]]]) -> ExtraClass:
    """Return why an ``fn-rdeps`` edge has no truth counterpart.

    Args:
        target: The target definition.
        caller_sites: The caller's resolved call sites with their reference-tool answers.

    Returns:
        The edge's extra class.

    Examples:
        >>> target = Definition("m", "f", "m.py", 1, False, False)
        >>> extra_class(target, [(CallSite("n.py", "n::g", 2, 4, "f", "f", None), ())])
        <ExtraClass.REFERENCE_UNRESOLVED: 'reference_unresolved'>
        >>> extra_class(target, [])
        <ExtraClass.NO_SAME_NAME_CALL: 'no_same_name_call'>
    """
    terminal = target.qualified_name.rsplit(".", 1)[-1]
    answers = [found for site, found in caller_sites if site.callee_name == terminal]
    if not answers:
        return ExtraClass.NO_SAME_NAME_CALL
    if any(not found for found in answers):
        return ExtraClass.REFERENCE_UNRESOLVED
    return ExtraClass.RESOLVED_ELSEWHERE


@dataclass(slots=True)
class StratumTally:
    """Edge counts of one stratum of the sample.

    Attributes:
        population: Targets in the stratum (``N_h``).
        sampled: Targets drawn (``n_h``).
        truth: Truth edges of the drawn targets.
        predicted: ``fn-rdeps`` edges of the drawn targets.
        hits: Edges in both.
    """

    population: int = 0
    sampled: int = 0
    truth: int = 0
    predicted: int = 0
    hits: int = 0

    @property
    def weight(self) -> float:
        """Return the stratum weight ``N_h / n_h`` (zero for an empty draw).

        Examples:
            >>> StratumTally(population=300, sampled=60).weight
            5.0
        """
        return self.population / self.sampled if self.sampled else 0.0


@dataclass(slots=True)
class Scored:
    """Edge-level result of one repository's sample.

    Attributes:
        strata: Tally per stratum label.
        missed: Missed edges as ``(target, caller)``.
        extra: Extra edges as ``(target, caller)``.
    """

    strata: dict[str, StratumTally] = field(default_factory=dict)
    missed: list[tuple[str, str]] = field(default_factory=list)
    extra: list[tuple[str, str]] = field(default_factory=list)

    def recall(self) -> Rate:
        """Return sample recall over edges with its Wilson interval."""
        return Rate(sum(t.hits for t in self.strata.values()), sum(t.truth for t in self.strata.values()))

    def precision(self) -> Rate:
        """Return sample precision over edges with its Wilson interval."""
        return Rate(sum(t.hits for t in self.strata.values()), sum(t.predicted for t in self.strata.values()))

    def weighted(self) -> tuple[float | None, float | None]:
        """Return population-weighted (recall, precision) point estimates.

        Examples:
            >>> scored = Scored(strata={"1": StratumTally(100, 10, 10, 8, 8), "6+": StratumTally(10, 10, 80, 40, 40)})
            >>> [round(value, 3) for value in scored.weighted()]
            [0.667, 1.0]
        """
        hits = sum(t.weight * t.hits for t in self.strata.values())
        truth = sum(t.weight * t.truth for t in self.strata.values())
        predicted = sum(t.weight * t.predicted for t in self.strata.values())
        return (hits / truth if truth else None, hits / predicted if predicted else None)


def score_sample(
    sample: Mapping[str, Sequence[str]],
    population: Mapping[str, int],
    truth_callers: Mapping[str, frozenset[str]],
    predicted_callers: Mapping[str, frozenset[str]],
) -> Scored:
    """Compare truth and ``fn-rdeps`` callers for every sampled target.

    Args:
        sample: Sampled target qnames per stratum label.
        population: Population size per stratum label.
        truth_callers: Truth callers per target qname (missing = none).
        predicted_callers: ``fn-rdeps`` callers per sampled target qname.

    Returns:
        Per-stratum tallies and the disagreeing edges, sorted.

    Raises:
        KeyError: When a sampled target has no ``fn-rdeps`` answer.

    Examples:
        >>> truth, predicted = {"m::f": frozenset({"m::g"})}, {"m::f": frozenset({"m::h"})}
        >>> scored = score_sample({"1": ["m::f"]}, {"1": 1}, truth, predicted)
        >>> scored.missed, scored.extra
        ([('m::f', 'm::g')], [('m::f', 'm::h')])
    """
    scored = Scored()
    for label, targets in sample.items():
        tally = StratumTally(population=population.get(label, 0), sampled=len(targets))
        for target in targets:
            truth = truth_callers.get(target, frozenset())
            predicted = predicted_callers[target]
            tally.truth += len(truth)
            tally.predicted += len(predicted)
            tally.hits += len(truth & predicted)
            scored.missed.extend((target, caller) for caller in sorted(truth - predicted))
            scored.extra.extend((target, caller) for caller in sorted(predicted - truth))
        scored.strata[label] = tally
    return scored


def adjudication_draw(edges: Sequence[tuple[str, str]], count: int, seed: str) -> list[tuple[str, str]]:
    """Draw a seeded, sorted sample of disagreeing edges for hand adjudication.

    Examples:
        >>> edges = [("m::f", f"m::g{i}") for i in range(10)]
        >>> adjudication_draw(edges, 3, "s") == adjudication_draw(list(reversed(edges)), 3, "s")
        True
    """
    ordered = sorted(edges)
    return sorted(random.Random(seed).sample(ordered, min(count, len(ordered))))  # noqa: S311 - not crypto
