"""Contract tests for the frozen oss:analyse calibration fixtures on disk.

The builder tests in ``test_calibrate_real_threads.py`` run on inline data, so nothing else reads the committed ``real-
problems``, ``real-fetch`` and ``real-consolidation`` folders. A hand-edited or half-written fixture would otherwise
reach a calibration run unnoticed and skew its recall and false-positive counts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

_SET = Path(__file__).resolve().parents[2] / "calibrate" / "oss-analyse"
_PROBLEMS = sorted((_SET / "real-problems").glob("*.json"))
_CONSOLIDATION = sorted((_SET / "real-consolidation").glob("*.json"))
_STORES = sorted((_SET / "real-fetch").glob("*.json"))

#: Fields every frozen problem carries, and the fields of one fetched candidate in a store.
_PROBLEM_FIELDS = frozenset(
    {"problem_id", "difficulty", "source", "task_prompt", "input", "ground_truth", "near_misses", "provenance"}
)
_FETCH_FIELDS = frozenset({"number", "title", "state", "body", "comments"})


def _load(path: Path) -> Any:
    """Return the parsed JSON of one fixture file."""
    return json.loads(path.read_text(encoding="utf-8"))


def _by_path(paths: list[Path]) -> list[Any]:
    """Return one ``pytest.param`` per fixture file, identified by its file name."""
    return [pytest.param(path, id=path.stem) for path in paths]


class TestFolders:
    """The three fixture folders are populated and keep their files apart."""

    @pytest.mark.parametrize(
        "paths",
        [
            pytest.param(_PROBLEMS, id="real-problems"),
            pytest.param(_STORES, id="real-fetch"),
            pytest.param(_CONSOLIDATION, id="real-consolidation"),
        ],
    )
    def test_folder_is_not_empty(self, paths: list[Path]) -> None:
        """Each fixture folder holds files, so a moved or mistyped path fails here instead of testing nothing."""
        assert paths

    def test_consolidation_problems_are_not_scored_problems(self) -> None:
        """A consolidation problem never also sits in ``real-problems``, where a run would score it.

        Consolidation files keep problems whose label is a maintainer's pointer to a tracker; scoring one would count a
        topic grouping as a duplicate verdict.
        """
        assert {p.name for p in _PROBLEMS}.isdisjoint({p.name for p in _CONSOLIDATION})

    def test_every_problem_has_a_fetch_store_and_no_store_is_orphaned(self) -> None:
        """The fetch store holds one file per frozen problem: none missing, none left behind after a removal."""
        assert {p.name for p in _STORES} == {p.name for p in _PROBLEMS}


class TestFrozenProblems:
    """Every frozen problem is a complete calibration problem named after its own ID."""

    @pytest.mark.parametrize("path", _by_path([*_PROBLEMS, *_CONSOLIDATION]))
    def test_file_name_is_the_problem_id(self, path: Path) -> None:
        """The file stem equals ``problem_id``, which the run directory and fetch store reuse as a key.

        A renamed file with an unchanged ID would load under one name and be looked up under another.
        """
        assert _load(path)["problem_id"] == path.stem

    @pytest.mark.parametrize("path", _by_path([*_PROBLEMS, *_CONSOLIDATION]))
    def test_carries_the_problem_fields(self, path: Path) -> None:
        """A problem has exactly the fields the calibrate pipeline reads, with ``source`` marking it as real."""
        problem = _load(path)
        assert (set(problem) == _PROBLEM_FIELDS, problem["source"]) == (True, "real")

    @pytest.mark.parametrize("path", _by_path(_PROBLEMS))
    def test_scored_problem_has_one_expected_verdict(self, path: Path) -> None:
        """A scored problem expects exactly one verdict: the true duplicate, or ``none`` for a negative control."""
        assert len(_load(path)["ground_truth"]) == 1


class TestFetchStores:
    """Every fetch store lists candidates in the shape the solver reads."""

    @pytest.mark.parametrize("path", _by_path(_STORES))
    def test_candidates_have_the_fetch_fields(self, path: Path) -> None:
        """Each stored candidate has a number, title, state, body and comments, keyed by its own number.

        The solver reads a candidate's body and last comments in place of a live ``gh`` call; a missing field would
        silently remove the evidence a duplicate verdict rests on.
        """
        store = _load(path)
        assert (
            {frozenset(entry) for entry in store.values()},
            {key for key, entry in store.items() if str(entry["number"]) != key},
        ) == (
            {_FETCH_FIELDS},
            set(),
        )
