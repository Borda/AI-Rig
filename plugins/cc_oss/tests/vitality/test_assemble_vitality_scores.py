"""Tests for ``assemble_vitality_scores.py``.

Covers weight loading from the scoring rubric (including fallback to defaults) and the assembly of three partial axis
result files into one weighted health score, plus the five-positional CLI call site.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from assemble_vitality_scores import assemble_scores, load_weights, main

#: Axis result for an axis that could not be scored — excluded from the weighted score.
_UNAVAILABLE = {"score": None, "conf": 0.0, "label": "⚪", "signal": ""}

#: One valid scoring-table row per axis 1-9, with distinct weights 0.11 … 0.19.
_ROWS = [f"| {n} axis-{n} | 0.{10 + n} |" for n in range(1, 10)]


def _axis(score: float | None = 7.0, conf: float = 0.9, label: str = "🟢", signal: str = "ok") -> dict:
    """Build one axis result with overridable score, confidence, and status fields.

    Examples:
        >>> _axis(score=None, label="⚪")
        {'score': None, 'conf': 0.9, 'label': '⚪', 'signal': 'ok'}
    """
    return {"score": score, "conf": conf, "label": label, "signal": signal}


def _write_partial(
    tmp_path: Path, name: str, axes: dict, scored_at: str = "1700000000", extra: dict | None = None
) -> Path:
    """Write a partial vitality result to the test's temporary directory.

    Examples:
        >>> tmp_path = getfixture("tmp_path")
        >>> path = _write_partial(tmp_path, "part.json", {"1": {"score": 1.0}})
        >>> json.loads(path.read_text())["scored_at"]
        '1700000000'
    """
    p = tmp_path / name
    data: dict = {"axes": axes, "scored_at": scored_at}
    if extra:
        data.update(extra)
    p.write_text(json.dumps(data))
    return p


def _write_scoring(path: Path, weights: list[tuple[int, object]]) -> Path:
    """Write a scoring table with one ``| N axis-N | weight |`` row per ``(axis, weight)`` pair.

    Examples:
        >>> path = _write_scoring(getfixture("tmp_path") / "s.md", [(1, "0.50")])
        >>> path.read_text()
        '| 1 axis-1 | 0.50 |'
    """
    path.write_text("\n".join(f"| {n} axis-{n} | {w} |" for n, w in weights))
    return path


@pytest.fixture(name="partials")
def _partials(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create three partial score files covering complementary axes.

    Examples:
        >>> paths = getfixture("partials")
        >>> [path.name for path in paths]
        ['a.json', 'b.json', 'c.json']
    """
    pa = _write_partial(tmp_path, "a.json", {str(k): _axis() for k in [1, 2, 5, 6]})
    pb = _write_partial(tmp_path, "b.json", {str(k): _axis() for k in [4, 7, 8]})
    pc = _write_partial(tmp_path, "c.json", {str(k): _axis() for k in [3, 9]}, extra={"axis3_weeks": 52})
    return pa, pb, pc


@pytest.fixture(name="scoring_file")
def _scoring_file(tmp_path: Path) -> Path:
    """Create the scoring table consumed by vitality aggregation tests.

    Examples:
        >>> path = getfixture("scoring_file")
        >>> "axis-9" in path.read_text()
        True
    """
    weights = [(1, 0.10), (2, 0.08), (3, 0.10), (4, 0.07), (5, 0.07), (6, 0.05), (7, 0.06), (8, 0.11), (9, 0.07)]
    return _write_scoring(tmp_path / "vitality-scoring.md", weights)


class TestLoadWeights:
    """Reading per-axis weights from the scoring rubric, with defaults when the table is unusable."""

    def test_parses_all_nine(self, scoring_file: Path) -> None:
        """A complete table yields nine weights keyed by axis number.

        The fixture table is the full nine-row shape the rubric ships, so every row is read as written.
        """
        w = load_weights(scoring_file)
        assert len(w) == 9
        assert w[1] == pytest.approx(0.10)
        assert w[9] == pytest.approx(0.07)

    def test_missing_file_returns_defaults(self, tmp_path: Path) -> None:
        """An absent rubric yields the nine built-in default weights.

        The defaults cover axes 1-9 only (axes 10-13 carry the remaining 0.29 of the 13-axis rubric), so they sum to
        0.71.
        """
        w = load_weights(tmp_path / "missing.md")
        assert len(w) == 9
        assert abs(sum(w.values()) - 0.71) < 0.01

    def test_real_rubric_matches_canonical_table(self) -> None:
        """The shipped ``vitality-scoring.md`` parses to the canonical weight table.

        Reads the real rubric on disk so a table edit that breaks the parser, or moves a weight away from the values the
        scorer assumes, fails here and not in a live analysis.
        """
        real_file = Path(__file__).resolve().parents[2] / "skills" / "_shared" / "vitality-scoring.md"
        w = load_weights(real_file)
        assert set(range(1, 10)) <= set(w)
        assert w[8] == pytest.approx(0.11)
        assert w[8] == max(w[n] for n in range(1, 10))
        assert w[2] == pytest.approx(0.08)

    def test_incomplete_file_returns_defaults(self, tmp_path: Path) -> None:
        """A table with fewer than nine parsed rows falls back to the nine defaults.

        One row parses cleanly, but a partial table would skew the renormalised score, so the whole table is rejected.
        """
        p = tmp_path / "partial.md"
        p.write_text("| 1 foo | 0.17 |\n")
        assert len(load_weights(p)) == 9

    @pytest.mark.parametrize(
        "content",
        [
            pytest.param("\n".join([_ROWS[0], "| 1 duplicate-axis-1 | 0.99 |", *_ROWS[1:]]), id="duplicate-axis"),
            pytest.param("\n".join(["| 1 axis-1 | nope |", *_ROWS[1:]]), id="non-numeric-weight"),
            pytest.param("\n".join([*_ROWS[:8], "| 10 axis-10 | 0.19 |"]), id="invalid-axis"),
        ],
    )
    def test_malformed_files_return_defaults(self, tmp_path: Path, content: str) -> None:
        """A table with a duplicate axis, a non-numeric weight, or an axis outside 1-9 falls back to the defaults.

        Each case still has nine rows, so only the content check can reject them; a silent partial parse would score
        with wrong weights.
        """
        p = tmp_path / "table.md"
        p.write_text(content)
        assert load_weights(p) == load_weights(tmp_path / "missing.md")


class TestAssembleScores:
    """Combining three partial result files into the weighted health score."""

    def test_basic_assembly(self, partials: tuple, scoring_file: Path) -> None:
        """Complete partials assemble into a bounded health score, positive confidence, and nine axes.

        The three fixture partials cover axes 1-9 between them, the shape a full analysis produces.
        """
        pa, pb, pc = partials
        result = assemble_scores(pa, pb, pc, scoring_file)
        assert "health_score_pct" in result
        assert 0.0 <= result["health_score_pct"] <= 100.0
        assert result["overall_confidence"] > 0.0
        assert len(result["axes"]) == 9

    def test_all_axes_present(self, partials: tuple, scoring_file: Path) -> None:
        """Every axis 1-9 appears in the assembled axes, keyed by its number as a string.

        Downstream rendering looks axes up by string key, so a missing or integer-keyed axis would drop a table row.
        """
        pa, pb, pc = partials
        result = assemble_scores(pa, pb, pc, scoring_file)
        assert set(result["axes"]) == {str(n) for n in range(1, 10)}

    def test_unavailable_axis_renormalization(self, tmp_path: Path, scoring_file: Path) -> None:
        """An unavailable axis 3 is renormalised out of the score and flagged as 202 pending.

        Axis 3 depends on contributor statistics GitHub may still be computing; its weight must not drag the score to
        zero, and the result records that the axis is pending rather than failed.
        """
        pa = _write_partial(tmp_path, "a.json", {str(k): _axis() for k in [1, 2, 5, 6]})
        pb = _write_partial(tmp_path, "b.json", {str(k): _axis() for k in [4, 7, 8]})
        axis3 = {"score": None, "conf": 0.0, "label": "⚪", "signal": "", "unavailable_reason": "stats 202"}
        pc = _write_partial(tmp_path, "c.json", {"3": axis3, "9": _axis()}, extra={"axis3_weeks": None})
        result = assemble_scores(pa, pb, pc, scoring_file)
        assert result["health_score_pct"] > 0.0
        assert result["axis3_202_pending"] is True

    @pytest.mark.parametrize(
        ("key", "expected"),
        [
            pytest.param("axis3_202_pending", False, id="axis3-202-pending-flag-false"),
            pytest.param("analysis_now", "1700000000", id="scored-at-from-partial-a"),
            pytest.param("axis3_weeks", 52, id="axis3-weeks-from-partial-c"),
        ],
    )
    def test_assembled_field_comes_from_its_partial(
        self, partials: tuple, scoring_file: Path, key: str, expected: object
    ) -> None:
        """Complete partials assemble with axis 3 not pending, the scored-at from partial A and weeks from partial C.

        Each top-level field has one owning partial; taking it from the wrong file would mis-date or mis-size the run.
        """
        pa, pb, pc = partials
        result = assemble_scores(pa, pb, pc, scoring_file)
        assert result[key] == expected

    def test_exact_weighted_score_renormalizes_available_axes(self, tmp_path: Path) -> None:
        """Only axes with a score contribute, and their weights are renormalised to sum to one.

        Axes 1 and 2 (weight 0.20 each) score 10 and 0 and every other axis is unavailable, so the score is exactly 50%
        and confidence is the mean of the available axes.
        """
        weights = [(1, "0.20"), (2, "0.20"), (3, "0.10"), (4, "0.10"), (5, "0.10"), (6, "0.10"), (7, "0.10")]
        scoring = _write_scoring(tmp_path / "scoring.md", [*weights, (8, "0.05"), (9, "0.05")])
        pa = _write_partial(tmp_path, "a.json", {"1": _axis(score=10.0), "2": _axis(score=0.0)})
        pb = _write_partial(tmp_path, "b.json", {str(k): _UNAVAILABLE for k in [4, 7, 8]})
        pc = _write_partial(tmp_path, "c.json", {str(k): _UNAVAILABLE for k in [3, 5, 6, 9]})

        result = assemble_scores(pa, pb, pc, scoring)

        assert result["health_score_pct"] == 50.0
        assert result["overall_confidence"] == 0.9

    def test_score_none_axis_is_excluded_from_weighted_score(self, tmp_path: Path, scoring_file: Path) -> None:
        """An axis present with ``score: None`` is excluded, leaving the other axis to set the whole score.

        Axis 1 scores 10 and axis 2 has no score, so the result is 100% rather than the average with a zero.
        """
        pa = _write_partial(tmp_path, "a.json", {"1": _axis(score=10.0), "2": _axis(score=None)})
        pb = _write_partial(tmp_path, "b.json", {})
        pc = _write_partial(tmp_path, "c.json", {})

        result = assemble_scores(pa, pb, pc, scoring_file)

        assert result["health_score_pct"] == 100.0

    def test_all_axes_unavailable_returns_zero_score_and_confidence(self, tmp_path: Path, scoring_file: Path) -> None:
        """When no axis has a score, health and confidence are both zero instead of a division error.

        The renormalisation divides by the available weight, which is zero here; the result must degrade, not raise.
        """
        pa = _write_partial(tmp_path, "a.json", {"1": _UNAVAILABLE})
        pb = _write_partial(tmp_path, "b.json", {})
        pc = _write_partial(tmp_path, "c.json", {})

        result = assemble_scores(pa, pb, pc, scoring_file)

        assert result["health_score_pct"] == 0.0
        assert result["overall_confidence"] == 0.0


class TestMain:
    """The command-line entry point and its documented five-positional call site."""

    def test_golden_invocation_five_positionals(
        self, partials: tuple, scoring_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The documented call (PARTIAL_A B C SCORING SCORES) writes the scores file, prints a summary and exits 0.

        This is the exact shape the skill invokes, so a changed positional order or output contract breaks it here.
        """
        pa, pb, pc = partials
        scores_file = tmp_path / "scores.json"
        rc = main([str(pa), str(pb), str(pc), str(scoring_file), str(scores_file)])
        assert rc == 0
        assert json.loads(scores_file.read_text())["health_score_pct"] >= 0.0
        assert "[vitality] assembled:" in capsys.readouterr().out

    def test_wrong_arg_count_exits_one(self) -> None:
        """Fewer than five positional arguments is a usage error.

        Two arguments is the near miss of a caller that forgot the scoring table and output path.
        """
        assert main(["a", "b"]) == 1

    def test_missing_partial_exits_one(self, tmp_path: Path, scoring_file: Path) -> None:
        """A partial file that does not exist fails the run instead of scoring from the rest.

        Silently assembling from two of three partials would report a health score missing whole axes.
        """
        absent = tmp_path / "absent"
        rc = main(
            [str(absent / "a.json"), str(absent / "b.json"), str(absent / "c.json"), str(scoring_file), "out.json"]
        )
        assert rc == 1
