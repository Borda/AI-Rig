"""Scoring, disagreement classes and table rendering of the import-edge census."""

from __future__ import annotations

import pytest
from _bench_census.classify import (
    ExtraClass,
    MissClass,
    classify_false_negatives,
    classify_false_positives,
)
from _bench_census.report import Align, CensusRow, census_table, render_table
from _bench_census.score import Rate, Tally, pool, score_repo, wilson_interval


@pytest.mark.parametrize(
    ("hits", "total", "expected"),
    [
        pytest.param(7091, 7128, (0.993, 0.996), id="recall-high"),
        pytest.param(7091, 7223, (0.978, 0.985), id="precision"),
        pytest.param(3831, 3831, (0.999, 1.000), id="perfect"),
        pytest.param(0, 0, (0.0, 1.0), id="empty"),
    ],
)
def test_wilson_interval_matches_reference_values(hits: int, total: int, expected: tuple[float, float]) -> None:
    """The 95% Wilson interval rounds to independently computed reference values."""
    assert tuple(round(bound, 3) for bound in wilson_interval(hits, total)) == expected


def test_score_repo_counts_out_of_universe_edges_separately() -> None:
    """Scanner edges with an endpoint outside the universe are counted but excluded from recall and precision."""
    result = score_repo(
        sources=["a.py", "b.py", "c.py"],
        truth=[("a.py", "b.py"), ("a.py", "c.py")],
        scanner=[("a.py", "b.py"), ("b.py", "c.py"), ("a.py", "vendored.py")],
        indexed=["a.py", "b.py", "vendored.py"],
    )
    assert (result.tally.true_positive, result.tally.false_negative, result.tally.false_positive) == (1, 1, 1)
    assert result.out_of_universe == 1
    assert result.indexed == 2
    assert result.false_negatives == (("a.py", "c.py"),)
    assert result.false_positives == (("b.py", "c.py"),)


def test_adjusted_precision_counts_parent_package_edges_as_true() -> None:
    """Parent-package-adjusted precision adds the classed parent-package false positives to the hits."""
    tally = Tally(sources=10, truth=8, scanner=10, true_positive=8, parent_package=2)
    assert tally.precision == Rate(8, 10)
    assert tally.adjusted_precision == Rate(10, 10)


def test_pool_sums_counts_and_recomputes_rates() -> None:
    """Pooled rates come from summed counts, not from averaging per-repository rates."""
    pooled = pool([Tally(1, 10, 10, 9), Tally(1, 90, 100, 90, 5)])
    assert (pooled.truth, pooled.scanner, pooled.true_positive, pooled.parent_package) == (100, 110, 99, 5)
    assert pooled.recall.value == pytest.approx(0.99)


def test_false_positive_classes_follow_the_parent_package_rule() -> None:
    """An ``__init__.py`` target is a parent-package edge when the source is inside it or reaches below it."""
    truth = [("app/main.py", "lib/sub/mod.py")]
    false_positives = [
        ("pkg/sub/a.py", "pkg/__init__.py"),
        ("app/main.py", "lib/__init__.py"),
        ("app/main.py", "other/__init__.py"),
        ("app/main.py", "lib/helpers.py"),
        ("nested/x.py", "__init__.py"),
    ]
    classes = classify_false_positives(false_positives, truth)
    assert [classes[edge] for edge in false_positives] == [
        ExtraClass.PARENT_PACKAGE_INIT,
        ExtraClass.PARENT_PACKAGE_INIT,
        ExtraClass.OTHER,
        ExtraClass.OTHER,
        ExtraClass.OTHER,
    ]


def test_false_negative_classes_follow_the_resolution_rule() -> None:
    """Script-only edges, non-package paths and the rest each get their own class."""
    sources = ["run.py", "helper.py", "modules/sd.py", "modules/deep/pkg/__init__.py", "modules/deep/pkg/m.py"]
    sources += ["lib/__init__.py", "lib/a.py", "lib/b.py"]
    misses = [("run.py", "helper.py"), ("modules/sd.py", "modules/deep/pkg/m.py"), ("lib/a.py", "lib/b.py")]
    classes = classify_false_negatives(misses, sources, script_directory_only=[("run.py", "helper.py")])
    assert [classes[edge] for edge in misses] == [
        MissClass.SCRIPT_DIRECTORY,
        MissClass.NON_PACKAGE_PATH,
        MissClass.OTHER,
    ]


def test_render_table_pads_like_the_markdown_formatter() -> None:
    """Right-aligned columns pad left with a ``:`` delimiter; every column is at least three wide."""
    table = render_table(["Group", "FN"], [Align.LEFT, Align.RIGHT], [["held-out", "37"], ["tuned", "3"]])
    assert table.splitlines() == [
        "| Group    |  FN |",
        "| -------- | --: |",
        "| held-out |  37 |",
        "| tuned    |   3 |",
    ]


def test_render_table_rejects_ragged_rows() -> None:
    """A row with the wrong cell count is a caller error, not a silently shifted table."""
    with pytest.raises(ValueError, match="2 cells"):
        render_table(["a", "b"], [Align.LEFT, Align.LEFT], [["only one"]])


def test_census_table_renders_repository_and_bold_headline_pooled_rows() -> None:
    """Repository rows are plain; the headline group's pooled row bolds its label and both rates."""
    rows = [
        CensusRow("held-out", "yt-dlp", Tally(sources=1124, truth=3831, scanner=3858, true_positive=3831)),
        CensusRow("held-out", "pooled", Tally(2010, 7128, 7223, 7091), pooled=True, headline=True),
        CensusRow("tuned", "pooled", Tally(416, 1295, 1348, 1292), pooled=True),
    ]
    lines = census_table(rows).splitlines()
    assert lines[0] == (
        "| Group    | Repository | Sources | Truth edges | codemap-py edges |  FN |  FP "
        "| Recall [95% CI]          | Precision [95% CI]       |"
    )
    assert lines[2] == (
        "| held-out | yt-dlp     |    1124 |        3831 |             3858 |   0 |  27 "
        "| 1.000 [0.999, 1.000]     | 0.993 [0.990, 0.995]     |"
    )
    assert lines[3] == (
        "| held-out | **pooled** |    2010 |        7128 |             7223 |  37 | 132 "
        "| **0.995 [0.993, 0.996]** | **0.982 [0.978, 0.985]** |"
    )
    assert lines[4] == (
        "| tuned    | **pooled** |     416 |        1295 |             1348 |   3 |  56 "
        "| 0.998 [0.993, 0.999]     | 0.958 [0.946, 0.968]     |"
    )
