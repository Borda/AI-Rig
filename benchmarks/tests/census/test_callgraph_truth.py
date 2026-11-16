"""Offline tests of the call-graph census truth builder, sampler and edge classes, with a stub reference backend."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

import pytest
from _bench_callgraph.run import RepoFacts, resolve_sites, truth_from
from _bench_callgraph.sample import stratified_sample, stratum_of
from _bench_callgraph.score import MissClass, edge_class, score_sample
from _bench_callgraph.truth import CallSite, Definition, DefLocation, module_facts

_CORE = """\
import functools
from pkg.util import helper as aliased


def target(x):
    return x


def deco(fn):
    return fn


@functools.lru_cache(target(7))
def decorated():
    return None


class Widget(make_base(target)):
    label = target(2)

    def render(self):
        return target(1)

    @deco
    def refresh(self):
        self.render()


@deco
class Panel:
    pass


def caller():
    def inner():
        target(3)

    aliased()
    callback = target
    register(target)
    annotated: target = None
    target(4)
    target(5)


target(6)
"""

_UTIL = """\
def helper():
    return 1
"""


@dataclass(frozen=True)
class NameBackend:
    """Resolve the identifier at a position by looking its spelling up in a fixed table, like a name-only tool."""

    root: Path
    table: dict[str, DefLocation]

    def resolve(self, path: str, positions: list[tuple[int, int]]) -> list[tuple[DefLocation, ...]]:
        """Return the table entry of the identifier starting at each position, or nothing."""
        lines = (self.root / path).read_text(encoding="utf-8").splitlines()
        answers = []
        for line, column in positions:
            match = re.match(r"\w+", lines[line - 1][column:])
            found = self.table.get(match.group(0)) if match else None
            answers.append((found,) if found else ())
        return answers


def _facts(root: Path) -> RepoFacts:
    """Write the fixture package and parse it the way the census does."""
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "core.py").write_text(_CORE, encoding="utf-8")
    (root / "pkg" / "util.py").write_text(_UTIL, encoding="utf-8")
    facts = RepoFacts()
    for module, path in (("pkg.core", "pkg/core.py"), ("pkg.util", "pkg/util.py")):
        facts.modules.append(module_facts(ast.parse((root / path).read_text(encoding="utf-8")), module, path))
    return facts


@pytest.fixture
def fixture_truth(tmp_path: Path):
    """Truth of the fixture package under a name-table backend."""
    table = {
        "target": DefLocation("pkg/core.py", 5),
        "render": DefLocation("pkg/core.py", 21),
        "aliased": DefLocation("pkg/util.py", 1),
    }
    facts = _facts(tmp_path)
    resolved = resolve_sites(NameBackend(tmp_path, table), facts, workers=1)
    truth, targets = truth_from(facts, resolved)
    return truth, targets


def test_truth_keeps_only_call_positions_and_dedups_per_caller(fixture_truth) -> None:
    """Only function-position calls become edges, one per caller with every site kept.

    The fixture uses ``target`` as a call in a method, a class body, a nested function and twice directly in ``caller``,
    and also as a callback value, a call argument, an annotation and a base-class argument. Only the calls may produce
    edges, and ``caller`` must appear once with its three call sites.
    """
    truth, _targets = fixture_truth
    callers = truth.edges["pkg.core::target"]
    assert sorted(callers) == ["pkg.core::Widget", "pkg.core::Widget.render", "pkg.core::caller", "pkg.core::decorated"]
    assert [site.line for site in callers["pkg.core::caller"]] == [36, 42, 43]


def test_truth_maps_enclosing_symbols_to_the_index_vocabulary(fixture_truth) -> None:
    """Callers are named ``module::qualified_name`` exactly as codemap-py symbols are.

    A nested function folds into its top-level function, a method decorator belongs to the method, a class-body
    statement belongs to the class, a decorator argument belongs to the decorated function, and a module-scope call
    or a call in a class base has no caller: it lands in the unattributed bucket and is never scored.
    """
    truth, _targets = fixture_truth
    assert sorted(truth.edges["pkg.core::Widget.render"]) == ["pkg.core::Widget.refresh"]
    assert [site.line for site in truth.unattributed["pkg.core::target"]] == [46]
    assert all(site.caller is None for site in truth.unattributed["pkg.core::target"])


def test_truth_resolves_import_aliases_to_their_definition(fixture_truth) -> None:
    """A call through ``from m import f as g`` reaches ``f`` because import-bound names are candidates.

    ``aliased`` is not a target name, so without the import-binding candidate rule the backend would never be asked
    about it and the alias edge would be silently absent from the truth.
    """
    truth, targets = fixture_truth
    assert sorted(truth.edges["pkg.util::helper"]) == ["pkg.core::caller"]
    assert "pkg.core::Widget.refresh" in targets


def test_truth_targets_exclude_private_names(fixture_truth) -> None:
    """Public targets are top-level functions and direct methods of top-level classes without underscores.

    The fixture's ``inner`` is nested and must not be a target; ``deco``, ``decorated`` and both methods must be.
    """
    _truth, targets = fixture_truth
    assert sorted(targets) == [
        "pkg.core::Widget.refresh",
        "pkg.core::Widget.render",
        "pkg.core::caller",
        "pkg.core::deco",
        "pkg.core::decorated",
        "pkg.core::target",
        "pkg.util::helper",
    ]


def test_sampler_is_deterministic_and_stratified_by_reference_count() -> None:
    """The same seed draws the same sample; every drawn target sits in the stratum of its own caller count.

    A population with uneven strata exercises the top-up rule: the 6+ stratum is short, so the deficit up to the
    minimum total is drawn from the strata in fixed order, and a different seed draws a different sample.
    """
    counts = {f"m::f{i:03d}": (0 if i < 300 else 1 if i < 380 else 3 if i < 450 else 9) for i in range(470)}
    first = stratified_sample(counts, seed=11, label="repo", per_stratum=60, min_total=200)
    again = stratified_sample(
        dict(reversed(list(counts.items()))), seed=11, label="repo", per_stratum=60, min_total=200
    )
    other = stratified_sample(counts, seed=12, label="repo", per_stratum=60, min_total=200)
    assert first == again
    assert first != other
    assert {label: len(members) for label, members in first.items()} == {"0": 60, "1": 60, "2-5": 60, "6+": 20}
    assert all(stratum_of(counts[qname]) == label for label, members in first.items() for qname in members)


def test_sampler_tops_up_short_strata_to_the_minimum_total() -> None:
    """When strata are too small for the minimum, the deficit comes from the strata in fixed order.

    With 500 zero-caller targets and only 20 in each other stratum the draw is 60 per stratum capped at 20, so 140 more
    zero-caller targets are drawn to reach 200.
    """
    counts = {f"m::z{i}": 0 for i in range(500)} | {f"m::a{i}": 1 for i in range(20)}
    counts |= {f"m::b{i}": 4 for i in range(20)} | {f"m::c{i}": 7 for i in range(20)}
    sample = stratified_sample(counts, seed=3, label="r", per_stratum=60, min_total=200)
    assert {label: len(members) for label, members in sample.items()} == {"0": 140, "1": 20, "2-5": 20, "6+": 20}


def test_edge_class_follows_the_registered_precedence() -> None:
    """An edge takes the highest-precedence class of its sites: instance method before alias before re-export.

    One missed edge holds a ``Cls.run`` call, an aliased call and a ``self.run`` call; the instance-method site must
    win. A function reached through a package re-export with no other cue is a re-export miss.
    """
    method = Definition("pkg.core", "Cls.run", "pkg/core.py", 4, True, True)
    sites = [
        CallSite("a.py", "a::f", 1, 4, "run", "Cls.run", "Cls"),
        CallSite("a.py", "a::f", 2, 0, "go", "go", None),
        CallSite("a.py", "a::f", 3, 9, "run", "self.run", "self"),
    ]
    assert edge_class(method, sites, {}) == MissClass.INSTANCE_METHOD
    assert edge_class(method, sites[:2], {}) == MissClass.ALIAS
    function = Definition("pkg.core", "helper", "pkg/core.py", 9, False, False)
    reexport = [CallSite("a.py", "a::f", 5, 4, "helper", "pkg.helper", "pkg")]
    assert edge_class(function, reexport, {"pkg": "pkg"}) == MissClass.RE_EXPORT
    assert edge_class(function, reexport, {"pkg": "pkg.core"}) == MissClass.OTHER


def test_score_counts_edges_per_stratum_and_lists_disagreements() -> None:
    """Edge recall and precision come from per-target set comparison inside each stratum.

    Two sampled targets: one whose truth caller is missed while an unrelated caller is reported, one fully matched.
    The tallies, the missed and extra lists and the Wilson-backed rates must agree.
    """
    sample = {"0": [], "1": ["m::a"], "2-5": ["m::b"], "6+": []}
    truth = {"m::a": frozenset({"m::x"}), "m::b": frozenset({"m::y", "m::z"})}
    predicted = {"m::a": frozenset({"m::w"}), "m::b": frozenset({"m::y", "m::z"})}
    scored = score_sample(sample, {"1": 10, "2-5": 5}, truth, predicted)
    assert (scored.recall().hits, scored.recall().total) == (2, 3)
    assert (scored.precision().hits, scored.precision().total) == (2, 3)
    assert scored.missed == [("m::a", "m::x")]
    assert scored.extra == [("m::a", "m::w")]
