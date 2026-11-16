"""Caller-count strata and the seeded stratified sample of targets.

Strata are defined by the reference tool's own distinct-caller count of each target (``0``, ``1``, ``2-5``, ``6+``),
never by the ``fn-rdeps`` answer being scored. Each stratum draws ``min(per_stratum, size)`` targets; when the total
falls short of ``min_total``, the deficit is drawn from the strata in their fixed order up to each stratum's size.
"""

from __future__ import annotations

import random
from collections.abc import Mapping

#: Stratum labels in their fixed order, with inclusive lower and upper caller counts (``None`` = unbounded).
STRATA: tuple[tuple[str, int, int | None], ...] = (("0", 0, 0), ("1", 1, 1), ("2-5", 2, 5), ("6+", 6, None))


def stratum_of(caller_count: int) -> str:
    """Return the stratum label of a distinct-caller count.

    Args:
        caller_count: Distinct callers, non-negative.

    Returns:
        One of ``0``, ``1``, ``2-5``, ``6+``.

    Raises:
        ValueError: For a negative count.

    Examples:
        >>> [stratum_of(count) for count in (0, 1, 2, 5, 6, 40)]
        ['0', '1', '2-5', '2-5', '6+', '6+']
    """
    if caller_count < 0:
        raise ValueError(f"caller count {caller_count} is negative")
    for label, low, high in STRATA:
        if caller_count >= low and (high is None or caller_count <= high):
            return label
    raise AssertionError("unreachable: the last stratum is unbounded")


def allocate(sizes: Mapping[str, int], per_stratum: int, min_total: int) -> dict[str, int]:
    """Return how many targets to draw from each stratum.

    Args:
        sizes: Population size per stratum label.
        per_stratum: Draw per stratum before any top-up.
        min_total: Smallest total sample; the deficit is drawn from the strata in fixed order.

    Returns:
        Draw per stratum label, never above the stratum's size.

    Examples:
        >>> allocate({"0": 500, "1": 80, "2-5": 70, "6+": 20}, 60, 220)
        {'0': 80, '1': 60, '2-5': 60, '6+': 20}
    """
    draws = {label: min(per_stratum, sizes.get(label, 0)) for label, _low, _high in STRATA}
    deficit = max(0, min_total - sum(draws.values()))
    for label, _low, _high in STRATA:
        extra = min(deficit, sizes.get(label, 0) - draws[label])
        draws[label] += extra
        deficit -= extra
    return draws


def stratified_sample(
    caller_counts: Mapping[str, int], *, seed: int, label: str, per_stratum: int, min_total: int
) -> dict[str, list[str]]:
    """Draw the seeded stratified sample of targets.

    The draw is a pure function of the arguments: members are sorted before sampling and each stratum uses its own
    ``random.Random`` seeded with the string ``"<seed>:<label>:<stratum>"`` (string seeds hash with SHA-512, so the
    draw does not depend on ``PYTHONHASHSEED``).

    Args:
        caller_counts: Distinct-caller count per target qname, from the reference tool.
        seed: Registered seed.
        label: Repository label, so two repositories draw independently.
        per_stratum: Draw per stratum before any top-up.
        min_total: Smallest total sample.

    Returns:
        Sorted sampled qnames per stratum label, every label present.

    Examples:
        >>> counts = {f"m::f{i}": i % 8 for i in range(40)}
        >>> sample = stratified_sample(counts, seed=7, label="demo", per_stratum=3, min_total=12)
        >>> {label: len(members) for label, members in sample.items()}
        {'0': 3, '1': 3, '2-5': 3, '6+': 3}
        >>> sample == stratified_sample(counts, seed=7, label="demo", per_stratum=3, min_total=12)
        True
    """
    members: dict[str, list[str]] = {name: [] for name, _low, _high in STRATA}
    for qname in sorted(caller_counts):
        members[stratum_of(caller_counts[qname])].append(qname)
    draws = allocate({name: len(group) for name, group in members.items()}, per_stratum, min_total)
    return {
        name: sorted(random.Random(f"{seed}:{label}:{name}").sample(group, draws[name]))  # noqa: S311 - not crypto
        for name, group in members.items()
    }
