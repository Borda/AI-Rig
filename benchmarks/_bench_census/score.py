"""Score codemap-py import edges against the static truth: recall, precision and 95% Wilson intervals.

A codemap-py edge is a module's ``direct_imports`` entry that names another indexed module, written as the pair of their
repository-relative paths. Edges with an endpoint outside the truth universe are counted separately and left out of the
score, so both sides measure the same file set.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

__all__ = ["Rate", "RepoScore", "Tally", "pool", "read_scanner_edges", "score_repo", "wilson_interval"]

#: Normal quantile of a two-sided 95% interval.
Z_95 = 1.96


def wilson_interval(successes: int, trials: int, z: float = Z_95) -> tuple[float, float]:
    """Return the Wilson score interval of a binomial proportion.

    Args:
        successes: Number of successes, between 0 and *trials*.
        trials: Number of trials; ``0`` gives the uninformative interval ``(0.0, 1.0)``.
        z: Normal quantile; the default gives a two-sided 95% interval.

    Returns:
        ``(low, high)`` clipped to ``[0, 1]``.

    Examples:
        >>> tuple(round(x, 3) for x in wilson_interval(7091, 7128))
        (0.993, 0.996)
        >>> wilson_interval(0, 0)
        (0.0, 1.0)
    """
    if trials == 0:
        return (0.0, 1.0)
    proportion = successes / trials
    denominator = 1 + z * z / trials
    centre = (proportion + z * z / (2 * trials)) / denominator
    half_width = z * math.sqrt(proportion * (1 - proportion) / trials + z * z / (4 * trials * trials)) / denominator
    return (max(0.0, centre - half_width), min(1.0, centre + half_width))


@dataclass(frozen=True)
class Rate:
    """A proportion with its 95% Wilson interval.

    Attributes:
        hits: Numerator.
        total: Denominator.
    """

    hits: int
    total: int

    @property
    def value(self) -> float | None:
        """Return the proportion, or ``None`` for an empty denominator."""
        return self.hits / self.total if self.total else None

    @property
    def interval(self) -> tuple[float, float]:
        """Return the 95% Wilson interval."""
        return wilson_interval(self.hits, self.total)

    def to_dict(self) -> dict[str, object]:
        """Serialize for the JSON result.

        Returns:
            Plain mapping with the counts, the proportion and the interval.

        Examples:
            >>> Rate(1, 2).to_dict()["value"]
            0.5
        """
        return {"hits": self.hits, "total": self.total, "value": self.value, "interval": list(self.interval)}


@dataclass(frozen=True)
class Tally:
    """Edge counts of one repository or one pooled group.

    Attributes:
        sources: Files in the truth universe.
        truth: Truth edges.
        scanner: codemap-py edges inside the universe.
        true_positive: Edges on both sides.
        parent_package: False positives classed as an implicit parent-package edge.
    """

    sources: int
    truth: int
    scanner: int
    true_positive: int
    parent_package: int = 0

    @property
    def false_negative(self) -> int:
        """Return truth edges codemap-py misses."""
        return self.truth - self.true_positive

    @property
    def false_positive(self) -> int:
        """Return codemap-py edges absent from the truth."""
        return self.scanner - self.true_positive

    @property
    def recall(self) -> Rate:
        """Return recall over the truth edges."""
        return Rate(self.true_positive, self.truth)

    @property
    def precision(self) -> Rate:
        """Return strict precision over the codemap-py edges."""
        return Rate(self.true_positive, self.scanner)

    @property
    def adjusted_precision(self) -> Rate:
        """Return precision counting implicit parent-package edges as true."""
        return Rate(self.true_positive + self.parent_package, self.scanner)

    def to_dict(self) -> dict[str, object]:
        """Serialize for the JSON result.

        Returns:
            Plain mapping with counts and rates.

        Examples:
            >>> Tally(sources=2, truth=4, scanner=5, true_positive=4, parent_package=1).to_dict()["false_positive"]
            1
        """
        return {
            "sources": self.sources,
            "truth_edges": self.truth,
            "scanner_edges": self.scanner,
            "true_positive": self.true_positive,
            "false_negative": self.false_negative,
            "false_positive": self.false_positive,
            "parent_package_false_positive": self.parent_package,
            "recall": self.recall.to_dict(),
            "precision": self.precision.to_dict(),
            "parent_package_adjusted_precision": self.adjusted_precision.to_dict(),
        }


@dataclass(frozen=True)
class RepoScore:
    """Score of one repository.

    Attributes:
        tally: Edge counts.
        indexed: Universe files codemap-py indexed.
        out_of_universe: codemap-py edges with an endpoint outside the universe (left out of the score).
        false_negatives: Truth edges codemap-py misses, sorted.
        false_positives: codemap-py edges absent from the truth, sorted.
    """

    tally: Tally
    indexed: int
    out_of_universe: int
    false_negatives: tuple[tuple[str, str], ...]
    false_positives: tuple[tuple[str, str], ...]


def read_scanner_edges(index_path: Path) -> tuple[frozenset[tuple[str, str]], frozenset[str]]:
    """Read the internal import edges and the indexed paths of a codemap-py index.

    Args:
        index_path: Index JSON written by ``codemap-py index``.

    Returns:
        ``(edges, indexed paths)``; an edge is ``(importer path, imported path)`` for every ``direct_imports`` entry
        that names another indexed module.

    Examples:
        >>> import tempfile
        >>> modules = [
        ...     {"name": "pkg.a", "path": "pkg/a.py", "direct_imports": ["pkg.b", "os", "pkg.a"]},
        ...     {"name": "pkg.b", "path": "pkg/b.py", "direct_imports": []},
        ... ]
        >>> with tempfile.TemporaryDirectory() as tmp:
        ...     index = Path(tmp) / "index.json"
        ...     _ = index.write_text(json.dumps({"modules": modules}))
        ...     edges, indexed = read_scanner_edges(index)
        >>> sorted(edges), sorted(indexed)
        ([('pkg/a.py', 'pkg/b.py')], ['pkg/a.py', 'pkg/b.py'])
    """
    modules = json.loads(index_path.read_text(encoding="utf-8"))["modules"]
    path_by_name = {module["name"]: module["path"] for module in modules}
    edges: set[tuple[str, str]] = set()
    for module in modules:
        for imported in module["direct_imports"]:
            target = path_by_name.get(imported)
            if target and target != module["path"]:
                edges.add((module["path"], target))
    return frozenset(edges), frozenset(path_by_name.values())


def score_repo(
    sources: Iterable[str],
    truth: Iterable[tuple[str, str]],
    scanner: Iterable[tuple[str, str]],
    indexed: Iterable[str],
    parent_package: int = 0,
) -> RepoScore:
    """Score one repository's codemap-py edges against its truth edges.

    Args:
        sources: Truth universe paths.
        truth: Truth edges.
        scanner: codemap-py edges, possibly with endpoints outside the universe.
        indexed: Paths codemap-py indexed.
        parent_package: False positives already classed as implicit parent-package edges.

    Returns:
        The repository score; scanner edges with an endpoint outside *sources* are counted, not scored.

    Examples:
        >>> result = score_repo(["a.py", "b.py"], [("a.py", "b.py")], [("a.py", "b.py"), ("a.py", "x.py")], ["a.py"])
        >>> result.tally.recall.value, result.tally.precision.value, result.out_of_universe, result.indexed
        (1.0, 1.0, 1, 1)
    """
    universe = set(sources)
    truth_edges = set(truth)
    scanner_all = set(scanner)
    inside = {edge for edge in scanner_all if edge[0] in universe and edge[1] in universe}
    tally = Tally(
        sources=len(universe),
        truth=len(truth_edges),
        scanner=len(inside),
        true_positive=len(truth_edges & inside),
        parent_package=parent_package,
    )
    return RepoScore(
        tally=tally,
        indexed=len(universe & set(indexed)),
        out_of_universe=len(scanner_all - inside),
        false_negatives=tuple(sorted(truth_edges - inside)),
        false_positives=tuple(sorted(inside - truth_edges)),
    )


def pool(tallies: Iterable[Tally]) -> Tally:
    """Sum repository tallies into one pooled tally.

    Args:
        tallies: Per-repository tallies.

    Returns:
        Component-wise sum; rates are recomputed from the summed counts.

    Examples:
        >>> pool([Tally(1, 2, 2, 2), Tally(3, 4, 6, 3, 1)]).precision.hits
        5
    """
    items = list(tallies)
    return Tally(
        sources=sum(item.sources for item in items),
        truth=sum(item.truth for item in items),
        scanner=sum(item.scanner for item in items),
        true_positive=sum(item.true_positive for item in items),
        parent_package=sum(item.parent_package for item in items),
    )
