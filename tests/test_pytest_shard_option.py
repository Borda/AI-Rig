"""Verify the root ``--shard I/N`` option splits a test selection into disjoint slices that cover it exactly.

CI runs one slow scope as several runner jobs, so a slice that overlaps or loses a test would either repeat work or
silently skip tests while every job stays green.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

_ROOT_CONFTEST = Path(__file__).resolve().parents[1] / "conftest.py"
_CASES = "".join(f"def test_case_{number}() -> None:\n    pass\n\n\n" for number in range(40))


@pytest.fixture
def selected(pytester: pytest.Pytester) -> Callable[[str | None], set[str]]:
    """Return a runner that collects an isolated project carrying the root conftest and reports the selected node
    ids."""
    pytester.makeconftest(_ROOT_CONFTEST.read_text(encoding="utf-8"))
    pytester.makepyfile(test_many=_CASES)

    def collect(shard: str | None) -> set[str]:
        """Collect the project, restricted to one shard when given, and return the selected node ids."""
        options = ["--collect-only", "-q", "-p", "no:cacheprovider", *(["--shard", shard] if shard else [])]
        return {line for line in pytester.runpytest_inprocess(*options).outlines if "::" in line}

    return collect


@pytest.mark.parametrize(
    "shards",
    [
        pytest.param(("1/2", "2/2"), id="two-way"),
        pytest.param(("1/3", "2/3", "3/3"), id="three-way"),
    ],
)
def test_shards_are_disjoint_and_cover_the_selection(
    selected: Callable[[str | None], set[str]], shards: tuple[str, ...]
) -> None:
    """Each test belongs to exactly one slice, so the slices together run the full selection once.

    The slices are collected one after another in separate runs, as CI does on separate runners, so agreement must come
    from the node id alone and not from any state shared between runs.
    """
    full = selected(None)
    slices = list(map(selected, shards))

    assert len(full) == 40
    assert set().union(*slices) == full
    assert sum(map(len, slices)) == len(full)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("0/3", id="index-below-one"),
        pytest.param("4/3", id="index-above-total"),
        pytest.param("1/0", id="zero-total"),
        pytest.param("a/b", id="not-numbers"),
        pytest.param("2", id="missing-total"),
    ],
)
def test_malformed_shard_is_a_usage_error(
    selected: Callable[[str | None], set[str]], pytester: pytest.Pytester, value: str
) -> None:
    """A mistyped slice fails the run loudly instead of selecting nothing and passing with zero tests."""
    selected(None)

    result = pytester.runpytest_inprocess("--collect-only", "-q", "--shard", value)

    assert result.ret == pytest.ExitCode.USAGE_ERROR
    assert "--shard expects I/N" in result.stderr.str()
