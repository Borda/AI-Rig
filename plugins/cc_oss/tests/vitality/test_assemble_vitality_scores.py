"""Tests for assemble_vitality_scores.py."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from assemble_vitality_scores import assemble_scores, load_weights, main


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
    pc = _write_partial(
        tmp_path,
        "c.json",
        {str(k): _axis() for k in [3, 9]},
        extra={"axis3_weeks": 52},
    )
    return pa, pb, pc


@pytest.fixture(name="scoring_file")
def _scoring_file(tmp_path: Path) -> Path:
    """Create the scoring table consumed by vitality aggregation tests.

    Examples:
        >>> path = getfixture("scoring_file")
        >>> "axis-9" in path.read_text()
        True
    """
    lines = "\n".join(
        f"| {n} axis-{n} | {w} |"
        for n, w in [(1, 0.10), (2, 0.08), (3, 0.10), (4, 0.07), (5, 0.07), (6, 0.05), (7, 0.06), (8, 0.11), (9, 0.07)]
    )
    p = tmp_path / "vitality-scoring.md"
    p.write_text(lines)
    return p


class TestLoadWeights:
    def test_parses_all_nine(self, scoring_file: Path) -> None:
        w = load_weights(scoring_file)
        assert len(w) == 9
        assert w[1] == pytest.approx(0.10)
        assert w[9] == pytest.approx(0.07)

    def test_missing_file_returns_defaults(self, tmp_path: Path) -> None:
        w = load_weights(tmp_path / "missing.md")
        assert len(w) == 9
        # axes 1-9 subtotal only — axes 10-13 carry the remaining 0.29 of the 13-axis rubric
        assert abs(sum(w.values()) - 0.71) < 0.01

    def test_loads_real_rubric_matches_canonical_table(self) -> None:
        """Real vitality-scoring.md on disk parses to the canonical 13-axis weight table."""
        real_file = Path(__file__).resolve().parents[2] / "skills" / "_shared" / "vitality-scoring.md"
        w = load_weights(real_file)
        assert set(range(1, 10)) <= set(w)
        assert w[8] == pytest.approx(0.11)
        assert w[8] == max(w[n] for n in range(1, 10))
        assert w[2] == pytest.approx(0.08)

    def test_incomplete_file_returns_defaults(self, tmp_path: Path) -> None:
        p = tmp_path / "partial.md"
        p.write_text("| 1 foo | 0.17 |\n")
        w = load_weights(p)
        assert len(w) == 9  # fallback — only 1 entry parsed

    @pytest.mark.parametrize(
        ("content", "reason"),
        [
            pytest.param(
                "\n".join(
                    [
                        "| 1 axis-1 | 0.11 |",
                        "| 1 duplicate-axis-1 | 0.99 |",
                        "| 2 axis-2 | 0.12 |",
                        "| 3 axis-3 | 0.13 |",
                        "| 4 axis-4 | 0.14 |",
                        "| 5 axis-5 | 0.15 |",
                        "| 6 axis-6 | 0.16 |",
                        "| 7 axis-7 | 0.17 |",
                        "| 8 axis-8 | 0.18 |",
                        "| 9 axis-9 | 0.19 |",
                    ]
                ),
                "duplicate axis rows",
                id="duplicate-axis",
            ),
            pytest.param(
                "\n".join(
                    [
                        "| 1 axis-1 | nope |",
                        "| 2 axis-2 | 0.12 |",
                        "| 3 axis-3 | 0.13 |",
                        "| 4 axis-4 | 0.14 |",
                        "| 5 axis-5 | 0.15 |",
                        "| 6 axis-6 | 0.16 |",
                        "| 7 axis-7 | 0.17 |",
                        "| 8 axis-8 | 0.18 |",
                        "| 9 axis-9 | 0.19 |",
                    ]
                ),
                "non-numeric weight",
                id="non-numeric-weight",
            ),
            pytest.param(
                "\n".join(
                    [
                        "| 1 axis-1 | 0.11 |",
                        "| 2 axis-2 | 0.12 |",
                        "| 3 axis-3 | 0.13 |",
                        "| 4 axis-4 | 0.14 |",
                        "| 5 axis-5 | 0.15 |",
                        "| 6 axis-6 | 0.16 |",
                        "| 7 axis-7 | 0.17 |",
                        "| 8 axis-8 | 0.18 |",
                        "| 10 axis-10 | 0.19 |",
                    ]
                ),
                "invalid axis",
                id="invalid-axis",
            ),
        ],
    )
    def test_malformed_files_return_defaults(self, tmp_path: Path, content: str, reason: str) -> None:
        p = tmp_path / f"{reason}.md"
        p.write_text(content)
        assert load_weights(p) == load_weights(tmp_path / "missing.md")


class TestAssembleScores:
    def test_basic_assembly(self, partials: tuple, scoring_file: Path, tmp_path: Path) -> None:
        pa, pb, pc = partials
        result = assemble_scores(pa, pb, pc, scoring_file)
        assert "health_score_pct" in result
        assert 0.0 <= result["health_score_pct"] <= 100.0
        assert result["overall_confidence"] > 0.0
        assert len(result["axes"]) == 9

    def test_all_axes_present(self, partials: tuple, scoring_file: Path) -> None:
        pa, pb, pc = partials
        result = assemble_scores(pa, pb, pc, scoring_file)
        for n in range(1, 10):
            assert str(n) in result["axes"]

    def test_unavailable_axis_renormalization(self, tmp_path: Path, scoring_file: Path) -> None:
        pa = _write_partial(tmp_path, "a.json", {str(k): _axis() for k in [1, 2, 5, 6]})
        pb = _write_partial(tmp_path, "b.json", {str(k): _axis() for k in [4, 7, 8]})
        # Axis 3 unavailable (⚪, no score)
        pc = _write_partial(
            tmp_path,
            "c.json",
            {
                "3": {"score": None, "conf": 0.0, "label": "⚪", "signal": "", "unavailable_reason": "stats 202"},
                "9": _axis(),
            },
            extra={"axis3_weeks": None},
        )
        result = assemble_scores(pa, pb, pc, scoring_file)
        # health_score_pct still valid — axis3 weight renormalized out
        assert result["health_score_pct"] > 0.0
        assert result["axis3_202_pending"] is True

    def test_axis3_202_pending_flag_false(self, partials: tuple, scoring_file: Path) -> None:
        pa, pb, pc = partials
        result = assemble_scores(pa, pb, pc, scoring_file)
        assert result["axis3_202_pending"] is False

    def test_scored_at_from_partial_a(self, partials: tuple, scoring_file: Path) -> None:
        pa, pb, pc = partials
        result = assemble_scores(pa, pb, pc, scoring_file)
        assert result["analysis_now"] == "1700000000"

    def test_axis3_weeks_from_partial_c(self, partials: tuple, scoring_file: Path) -> None:
        pa, pb, pc = partials
        result = assemble_scores(pa, pb, pc, scoring_file)
        assert result["axis3_weeks"] == 52

    def test_exact_weighted_score_renormalizes_available_axes(self, tmp_path: Path) -> None:
        scoring = tmp_path / "scoring.md"
        scoring.write_text(
            "\n".join(
                f"| {n} axis-{n} | {weight} |"
                for n, weight in [
                    (1, "0.20"),
                    (2, "0.20"),
                    (3, "0.10"),
                    (4, "0.10"),
                    (5, "0.10"),
                    (6, "0.10"),
                    (7, "0.10"),
                    (8, "0.05"),
                    (9, "0.05"),
                ]
            )
        )
        unavailable = {"score": None, "conf": 0.0, "label": "⚪", "signal": ""}
        pa = _write_partial(tmp_path, "a.json", {"1": _axis(score=10.0), "2": _axis(score=0.0)})
        pb = _write_partial(tmp_path, "b.json", {str(k): unavailable for k in [4, 7, 8]})
        pc = _write_partial(tmp_path, "c.json", {str(k): unavailable for k in [3, 5, 6, 9]})

        result = assemble_scores(pa, pb, pc, scoring)

        assert result["health_score_pct"] == 50.0
        assert result["overall_confidence"] == 0.9

    def test_score_none_axis_is_excluded_from_weighted_score(self, tmp_path: Path, scoring_file: Path) -> None:
        pa = _write_partial(tmp_path, "a.json", {"1": _axis(score=10.0), "2": _axis(score=None)})
        pb = _write_partial(tmp_path, "b.json", {})
        pc = _write_partial(tmp_path, "c.json", {})

        result = assemble_scores(pa, pb, pc, scoring_file)

        assert result["health_score_pct"] == 100.0

    def test_all_axes_unavailable_returns_zero_score_and_confidence(self, tmp_path: Path, scoring_file: Path) -> None:
        unavailable = {"score": None, "conf": 0.0, "label": "⚪", "signal": ""}
        pa = _write_partial(tmp_path, "a.json", {"1": unavailable})
        pb = _write_partial(tmp_path, "b.json", {})
        pc = _write_partial(tmp_path, "c.json", {})

        result = assemble_scores(pa, pb, pc, scoring_file)

        assert result["health_score_pct"] == 0.0
        assert result["overall_confidence"] == 0.0


class TestMain:
    def test_success(self, partials: tuple, scoring_file: Path, tmp_path: Path, capsys) -> None:
        pa, pb, pc = partials
        scores_file = tmp_path / "scores.json"
        rc = main([str(pa), str(pb), str(pc), str(scoring_file), str(scores_file)])
        assert rc == 0
        assert scores_file.exists()
        out = capsys.readouterr().out
        assert "[vitality] assembled:" in out

    def test_wrong_arg_count(self, capsys) -> None:
        rc = main(["a", "b"])
        assert rc == 1

    def test_missing_partial(self, tmp_path: Path, scoring_file: Path, capsys) -> None:
        scores_file = tmp_path / "scores.json"
        rc = main(["/no/such/a.json", "/no/b.json", "/no/c.json", str(scoring_file), str(scores_file)])
        assert rc == 1

    def test_golden_invocation_five_positionals(
        self, partials: tuple, scoring_file: Path, tmp_path: Path, capsys
    ) -> None:
        """Documented 5-positional call site (PARTIAL_A B C SCORING SCORES) succeeds."""
        pa, pb, pc = partials
        scores_file = tmp_path / "scores.json"
        rc = main([str(pa), str(pb), str(pc), str(scoring_file), str(scores_file)])
        assert rc == 0
        assert json.loads(scores_file.read_text())["health_score_pct"] >= 0.0
