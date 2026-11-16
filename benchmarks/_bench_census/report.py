"""Render census results as Markdown tables padded the way the repository Markdown formatter pads them.

Every cell is padded to its column width (minimum three, the width of a ``--:`` delimiter); right-aligned columns pad on
the left and carry a ``:`` delimiter. A table rendered here is therefore left byte-identical by ``mdformat``, so a
committed record and a fresh run compare as plain text.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from _bench_census.score import Rate, Tally

__all__ = ["Align", "CensusRow", "census_table", "class_table", "format_rate", "render_table"]

_MIN_WIDTH = 3


class Align(str, Enum):
    """Column alignment of a Markdown table."""

    LEFT = "left"
    RIGHT = "right"


def render_table(headers: Sequence[str], aligns: Sequence[Align], rows: Sequence[Sequence[str]]) -> str:
    """Render a padded Markdown table.

    Args:
        headers: Header cells.
        aligns: One alignment per column.
        rows: Body rows, each with one cell per column.

    Returns:
        The table text, one line per row, without a trailing newline.

    Raises:
        ValueError: When a row or the alignment list does not match the header width.

    Examples:
        >>> print(render_table(["Name", "FN"], [Align.LEFT, Align.RIGHT], [["a", "1"]]))
        | Name |  FN |
        | ---- | --: |
        | a    |   1 |
    """
    width = len(headers)
    if len(aligns) != width or any(len(row) != width for row in rows):
        raise ValueError(f"every row and the alignment list need {width} cells, matching the header")
    widths = [max(_MIN_WIDTH, len(headers[col]), *(len(row[col]) for row in rows)) for col in range(width)]

    def line(cells: Sequence[str]) -> str:
        padded = [
            cell.rjust(widths[col]) if aligns[col] == Align.RIGHT else cell.ljust(widths[col])
            for col, cell in enumerate(cells)
        ]
        return "| " + " | ".join(padded) + " |"

    delimiter = [
        "-" * (widths[col] - 1) + ":" if aligns[col] == Align.RIGHT else "-" * widths[col] for col in range(width)
    ]
    return "\n".join([line(headers), "| " + " | ".join(delimiter) + " |", *(line(row) for row in rows)])


def format_rate(rate: Rate) -> str:
    """Format a proportion with its 95% Wilson interval to three decimals.

    Args:
        rate: The proportion.

    Returns:
        ``"0.995 [0.993, 0.996]"``-style text, or ``"n/a"`` for an empty denominator.

    Examples:
        >>> format_rate(Rate(7091, 7128))
        '0.995 [0.993, 0.996]'
        >>> format_rate(Rate(0, 0))
        'n/a'
    """
    value = rate.value
    if value is None:
        return "n/a"
    low, high = rate.interval
    return f"{value:.3f} [{low:.3f}, {high:.3f}]"


def _bold(text: str) -> str:
    """Wrap *text* in Markdown strong emphasis."""
    return f"**{text}**"


@dataclass(frozen=True)
class CensusRow:
    """One table row: a repository, or a pooled group when ``pooled`` is set.

    Attributes:
        group: Group label, for example ``held-out``.
        label: Repository label; ignored for a pooled row, which reads ``pooled``.
        tally: Edge counts.
        pooled: Whether the row pools its group.
        headline: Whether the row's rates are the headline figures (rendered bold).
        classes: Disagreement class counts, keyed by class value (used by :func:`class_table`).
    """

    group: str
    label: str
    tally: Tally
    pooled: bool = False
    headline: bool = False
    classes: tuple[tuple[str, int], ...] = ()


_CENSUS_HEADERS = (
    "Group",
    "Repository",
    "Sources",
    "Truth edges",
    "codemap-py edges",
    "FN",
    "FP",
    "Recall [95% CI]",
    "Precision [95% CI]",
)
_CENSUS_ALIGNS = (Align.LEFT, Align.LEFT, *([Align.RIGHT] * 5), Align.LEFT, Align.LEFT)


def census_table(rows: Sequence[CensusRow]) -> str:
    """Render the main census table: counts, recall and strict precision per repository and per pooled group.

    Args:
        rows: Repository and pooled rows in display order.

    Returns:
        The padded Markdown table.

    Examples:
        >>> row = CensusRow("held-out", "demo", Tally(sources=4, truth=10, scanner=10, true_positive=9))
        >>> [cell.strip() for cell in census_table([row]).splitlines()[2].split("|")[1:-1]][4:]
        ['10', '1', '1', '0.900 [0.596, 0.982]', '0.900 [0.596, 0.982]']
    """
    body = []
    for row in rows:
        recall, precision = format_rate(row.tally.recall), format_rate(row.tally.precision)
        if row.headline:
            recall, precision = _bold(recall), _bold(precision)
        body.append(
            [
                row.group,
                _bold("pooled") if row.pooled else row.label,
                str(row.tally.sources),
                str(row.tally.truth),
                str(row.tally.scanner),
                str(row.tally.false_negative),
                str(row.tally.false_positive),
                recall,
                precision,
            ]
        )
    return render_table(_CENSUS_HEADERS, _CENSUS_ALIGNS, body)


def class_table(rows: Sequence[CensusRow], class_names: Sequence[tuple[str, str]]) -> str:
    """Render disagreement classes and parent-package-adjusted precision per repository and pooled group.

    Args:
        rows: Repository and pooled rows in display order; each carries its class counts.
        class_names: ``(class value, column header)`` pairs in column order.

    Returns:
        The padded Markdown table.

    Examples:
        >>> row = CensusRow("tuned", "demo", Tally(1, 2, 3, 2, 1), classes=(("fp:parent_package_init", 1),))
        >>> print(class_table([row], [("fp:parent_package_init", "Parent-package FP")]).splitlines()[2])
        | tuned | demo       |                 1 | 1.000 [0.438, 1.000]                       |
    """
    headers = [
        "Group",
        "Repository",
        *(header for _, header in class_names),
        "Parent-package-adjusted precision [95% CI]",
    ]
    aligns = [Align.LEFT, Align.LEFT, *([Align.RIGHT] * len(class_names)), Align.LEFT]
    body = []
    for row in rows:
        counts = dict(row.classes)
        body.append(
            [
                row.group,
                _bold("pooled") if row.pooled else row.label,
                *(str(counts.get(value, 0)) for value, _ in class_names),
                format_rate(row.tally.adjusted_precision),
            ]
        )
    return render_table(headers, aligns, body)
