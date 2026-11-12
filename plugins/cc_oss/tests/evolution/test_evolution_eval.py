"""Tests for the evolution evaluator: micro precision/recall, residue, bootstrap intervals, gate and threshold tuning.

Rows and labels are hand-built; no store or git repository is needed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from evolution_eval import GOLD_FLOOR, evaluate, gate, main, parse_labels, pooled, threshold_table, tune_threshold


def _row(fix_id: str, release: str, cells: list[tuple[str, bool, float, float]]) -> dict:
    """Return a scored row from ``(column_id, binary, raw_score, soft_share)`` cell tuples."""
    return {
        "fix_id": fix_id,
        "fix_release": release,
        "line": "1",
        "cells": [
            {"column_id": cid, "binary": binary, "raw_score": raw, "soft_share": soft, "method": "blame"}
            for cid, binary, raw, soft in cells
        ],
    }


def _label(fix_id: str, column_id: str, repo: str = "B") -> dict[str, str]:
    """Return one label record."""
    return {"repo": repo, "fix_id": fix_id, "column_id": column_id}


#: Three labelled fixes: one true positive, one wrong link (FP + FN), one correctly unlinked residue row.
_LABELS = [_label("1.1/f1", "1.0/A"), _label("1.1/f2", "1.0/C"), _label("1.2/f3", "none")]
_ROWS = [
    _row("1.1/f1", "1.1", [("1.0/A", True, 0.9, 1.0)]),
    _row("1.1/f2", "1.1", [("1.0/B", True, 0.5, 0.6), ("pre-window@0.9", False, 0.4, 0.4)]),
    _row("1.2/f3", "1.2", [("unknown", False, 1.0, 1.0)]),
]


class TestEvaluate:
    """Point estimates and bootstrap intervals for one repo."""

    def test_micro_precision_recall_and_residue(self) -> None:
        """One correct link and one wrong link give precision and recall of 1/2; residue is 1 of 3 fixes.

        The ``none``-labelled fix predicts nothing and sits entirely on the ``unknown`` residue.
        """
        result = evaluate(_LABELS, _ROWS, resamples=50)
        assert (result["true_positive"], result["predicted"], result["gold"]) == (1, 2, 2)
        assert (result["precision"], result["recall"]) == (0.5, 0.5)
        assert result["residue"] == pytest.approx(1 / 3)

    def test_missing_row_counts_as_miss_and_residue(self) -> None:
        """A labelled fix with no scored row predicts nothing and counts as fully residue.

        The fix was labelled but never scored, for example because the scan scope excluded it.
        """
        result = evaluate([_label("1.3/f9", "1.0/A")], _ROWS, resamples=20)
        assert (result["recall"], result["residue"], result["missing_rows"]) == (0.0, 1.0, ["1.3/f9"])

    def test_alias_label_resolves_to_canonical_column(self) -> None:
        """A label naming a backport alias counts as the canonical column when column records are given.

        The labeller wrote the backport's ID; the store keeps the earlier 1.0 column as canonical.
        """
        columns = [{"column_id": "1.0/A", "aliases": ["0.9/A-backport"]}]
        result = evaluate([_label("1.1/f1", "0.9/A-backport")], _ROWS, columns, resamples=20)
        assert result["true_positive"] == 1

    def test_bootstrap_is_reproducible_with_a_seed(self) -> None:
        """The same seed gives identical intervals, each bracketing the point estimate.

        Two evaluations of the same labelled sample run with seed 7.
        """
        first = evaluate(_LABELS, _ROWS, seed=7, resamples=200)
        second = evaluate(_LABELS, _ROWS, seed=7, resamples=200)
        low, high = first["ci"]["precision"]
        assert first["ci"] == second["ci"]
        assert low <= first["precision"] <= high

    def test_labels_spanning_repos_need_a_repo(self) -> None:
        """Labels for several repos without ``repo=`` raise instead of mixing repos silently.

        The CSV holds the tuning repo A and the holdout repo B.
        """
        with pytest.raises(ValueError, match="span repos"):
            evaluate([*_LABELS, _label("x", "none", repo="A")], _ROWS)


class TestPooled:
    """The third-repo rule pools repos and clusters the bootstrap by (repo, fix)."""

    def test_same_fix_id_in_two_repos_counts_twice(self) -> None:
        """Identical fix IDs in repos B and C are separate clusters; labels of the tuning repo are ignored.

        Both repos contain a fix ``1.1/f1`` repairing ``1.0/A``.
        """
        labels = [_label("1.1/f1", "1.0/A", repo=repo) for repo in ("A", "B", "C")]
        result = pooled(labels, {"B": _ROWS, "C": _ROWS}, resamples=20)
        assert (result["fixes"], result["repos"], result["ignored_repos"]) == (2, ["B", "C"], ["A"])


class TestTopK:
    """The per-fix cap keeps the best-scored links, so extra weak links stop costing precision."""

    #: One fix repairs A; the scorer also linked B (0.8) and C (0.4), whose score ties are broken by column ID.
    _ROW = _row("f1", "1.1", [("1.0/A", True, 0.8, 0.4), ("1.0/B", True, 0.8, 0.4), ("1.0/C", True, 0.4, 0.2)])

    @pytest.mark.parametrize(
        ("top_k", "predicted", "precision"),
        [
            pytest.param(None, 3, 1 / 3, id="uncapped"),
            pytest.param(2, 2, 1 / 2, id="two-best-tie-broken-by-id"),
            pytest.param(1, 1, 1.0, id="single-best"),
        ],
    )
    def test_cap_keeps_the_highest_raw_scores(self, top_k: int | None, predicted: int, precision: float) -> None:
        """A cap of k keeps the k highest raw scores; the tie at 0.8 resolves to the smaller column ID, A before B."""
        result = evaluate([_label("f1", "1.0/A")], [self._ROW], resamples=5, top_k=top_k)
        assert (result["predicted"], result["precision"], result["top_k"]) == (
            predicted,
            pytest.approx(precision),
            top_k,
        )

    def test_cap_applies_to_a_threshold_sweep(self) -> None:
        """A sweep re-decides links from raw scores and then caps them, so the table matches ``evaluate``."""
        table = threshold_table([_label("f1", "1.0/A", repo="A")], [self._ROW], [0.3], top_k=1)
        assert table[0]["precision"] == 1.0


class TestGate:
    """Phase 0 bars: precision >= 0.70, recall >= 0.50, residue <= 0.35, precision CI lower bound >= 0.55."""

    @pytest.mark.parametrize(
        ("result", "pooled_run", "expected"),
        [
            pytest.param(
                {"precision": 0.8, "recall": 0.6, "residue": 0.2, "ci": {"precision": [0.6, 0.9]}},
                False,
                (True, True, False),
                id="single-pass",
            ),
            pytest.param(
                {"precision": 0.8, "recall": 0.6, "residue": 0.2, "ci": {"precision": [0.5, 0.9]}},
                False,
                (True, False, True),
                id="single-wide-interval-triggers-third-repo",
            ),
            pytest.param(
                {"precision": 0.8, "recall": 0.6, "residue": 0.2, "ci": {"precision": [0.5, 0.9]}},
                True,
                (True, False, False),
                id="pooled-wide-interval-fails",
            ),
            pytest.param(
                {"precision": 0.8, "recall": 0.6, "residue": 0.4, "ci": {"precision": [0.6, 0.9]}},
                False,
                (False, False, False),
                id="residue-over-cap",
            ),
            pytest.param(
                {"precision": None, "recall": 0.0, "residue": 1.0, "ci": {"precision": [None, None]}},
                False,
                (False, False, True),
                id="no-predictions",
            ),
        ],
    )
    def test_gate_flags(self, result: dict[str, Any], pooled_run: bool, expected: tuple[bool, bool, bool]) -> None:
        """The gate reports bars met, overall pass and whether the third-repo rule fires.

        A wide precision interval on repo B calls for repo C; on a pooled run it is a final failure.
        """
        flags = gate(result, pooled_run=pooled_run)
        assert (flags["bars_met"], flags["pass"], flags["third_repo_required"]) == expected


#: Tuning sample: f1 repairs A (raw 0.6, decoy B at 0.3); f2 repairs C (raw 0.35).
_TUNE_LABELS = [_label("f1", "1.0/A", repo="A"), _label("f2", "1.0/C", repo="A")]
_TUNE_ROWS = [
    _row("f1", "1.1", [("1.0/A", True, 0.6, 0.7), ("1.0/B", False, 0.3, 0.3)]),
    _row("f2", "1.1", [("1.0/C", True, 0.35, 1.0)]),
]


class TestGoldFloor:
    """A repo with fewer than ``GOLD_FLOOR`` labelled pairs is reported, never passed or failed."""

    @pytest.mark.parametrize(
        ("gold", "gated", "passed"),
        [
            pytest.param(GOLD_FLOOR - 1, False, None, id="below-floor-not-gated"),
            pytest.param(GOLD_FLOOR, True, True, id="at-floor-gated"),
        ],
    )
    def test_floor_decides_whether_bars_gate(self, gold: int, gated: bool, passed: bool | None) -> None:
        """Perfect scores gate only once the sample reaches the floor; below it ``pass`` is None."""
        result = {"gold": gold, "precision": 1.0, "recall": 1.0, "residue": 0.0, "ci": {"precision": [0.9, 1.0]}}
        flags = gate(result)
        assert (flags["gated"], flags["pass"], flags["bars"]["gold_floor"]) == (gated, passed, GOLD_FLOOR)


class TestTuneThreshold:
    """Threshold choice: maximise F1 subject to precision >= 0.70 on the tuning repo."""

    def test_table_redecides_binary_links_from_raw_scores(self) -> None:
        """Each candidate re-decides links from raw scores: 0.25 keeps the decoy, 0.4 loses the true C link.

        The stored binary flags are ignored; only raw scores matter here.
        """
        table = threshold_table(_TUNE_LABELS, _TUNE_ROWS, [0.25, 0.4])
        assert [(entry["precision"], entry["recall"]) for entry in table] == [
            pytest.approx((2 / 3, 1.0)),
            pytest.approx((1.0, 0.5)),
        ]

    @pytest.mark.parametrize(
        ("method", "linked"),
        [
            pytest.param("blame", True, id="blame-links"),
            pytest.param("text", True, id="text-links"),
            pytest.param("symbol", False, id="symbol-overlap-does-not-link"),
            pytest.param("file", False, id="file-overlap-does-not-link"),
            pytest.param("inherited", False, id="inherited-share-does-not-link"),
        ],
    )
    def test_threshold_links_only_blame_backed_methods(self, method: str, linked: bool) -> None:
        """A cell above the candidate threshold counts as a link only when its strongest signal is a link method.

        The scorer applies the same rule to the stored binary flag, so a sweep must not re-admit cells the scorer
        refused: on labelled repositories symbol, file and inherited cells were nearly all false links.
        """
        row = _row("f1", "1.1", [("1.0/A", True, 0.6, 1.0)])
        row["cells"][0]["method"] = method
        table = threshold_table([_label("f1", "1.0/A", repo="A")], [row], [0.3], repo="A")
        assert table[0]["recall"] == (1.0 if linked else 0.0)

    def test_threshold_sweep_excludes_non_link_cells_and_includes_link_score_boundary(self) -> None:
        """Non-link evidence stays excluded throughout a sweep, while a link includes its exact score boundary."""
        row = _row("f1", "1.1", [("1.0/A", False, 0.6, 0.5), ("1.0/B", True, 1.0, 0.5)])
        row["cells"][1]["method"] = "symbol"

        table = threshold_table([_label("f1", "1.0/A", repo="A")], [row], [0.01, 0.6, 0.61, 1.0])

        assert table == [
            {"threshold": 0.01, "precision": 1.0, "recall": 1.0, "f1": 1.0},
            {"threshold": 0.6, "precision": 1.0, "recall": 1.0, "f1": 1.0},
            {"threshold": 0.61, "precision": None, "recall": 0.0, "f1": None},
            {"threshold": 1.0, "precision": None, "recall": 0.0, "f1": None},
        ]

    def test_picks_highest_f1_meeting_precision(self) -> None:
        """Of 0.25 (precision 0.67), 0.33 (F1 1.0) and 0.4 (F1 0.67), the precision floor and F1 pick 0.33.

        The lowest candidate fails the precision bar despite full recall.
        """
        assert tune_threshold(_TUNE_LABELS, _TUNE_ROWS, [0.25, 0.33, 0.4]) == 0.33

    def test_no_feasible_threshold_raises(self) -> None:
        """When no candidate reaches precision 0.70 the tuner raises instead of returning a weak threshold.

        Only the lowest candidate is offered, and it keeps the decoy link.
        """
        with pytest.raises(ValueError, match="precision"):
            tune_threshold(_TUNE_LABELS, _TUNE_ROWS, [0.25])


class TestLabels:
    """Labels CSV parsing."""

    def test_header_is_required(self) -> None:
        """A labels file without the ``repo,fix_id,column_id`` header is rejected with the missing names.

        The file starts directly with data.
        """
        with pytest.raises(ValueError, match="header"):
            parse_labels("B,1.1/f1,1.0/A\n")


class TestCli:
    """The ``evaluate`` and ``tune`` subcommands print one line of JSON."""

    def _write(self, tmp_path: Path, labels: list[dict[str, str]], rows: list[dict]) -> tuple[str, str]:
        """Write labels CSV and rows JSONL; return their paths."""
        csv_text = "repo,fix_id,column_id\n" + "".join(f"{x['repo']},{x['fix_id']},{x['column_id']}\n" for x in labels)
        (tmp_path / "labels.csv").write_text(csv_text, encoding="utf-8")
        (tmp_path / "rows.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        return str(tmp_path / "labels.csv"), str(tmp_path / "rows.jsonl")

    def test_evaluate_prints_result_with_gate(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """``evaluate`` prints precision, recall and the gate flags as one JSON line.

        The sample's precision of 0.5 misses the 0.70 bar, but its two labelled pairs sit below the gold floor, so the
        gate reports the repo as not gated instead of failed.
        """
        labels, rows = self._write(tmp_path, _LABELS, _ROWS)
        assert main(["evaluate", "--labels", labels, "--rows", rows, "--resamples", "20"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert (payload["precision"], payload["gate"]["gated"], payload["gate"]["pass"]) == (0.5, False, None)

    def test_top_k_flag_caps_links_per_fix(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """``--top-k 1`` keeps only the best-scored link of a fix and reports the cap in the payload.

        The fix has a true link at 0.5 and a decoy at 0.9; the cap keeps the decoy, so precision falls to 0.
        """
        rows = [_row("f1", "1.1", [("1.0/A", True, 0.5, 0.5), ("1.0/B", True, 0.9, 0.5)])]
        labels, rows_path = self._write(tmp_path, [_label("f1", "1.0/A")], rows)
        assert main(["evaluate", "--labels", labels, "--rows", rows_path, "--resamples", "5", "--top-k", "1"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert (payload["top_k"], payload["predicted"], payload["precision"]) == (1, 1, 0.0)

    def test_top_k_below_one_is_rejected(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A ``--top-k`` of zero or less exits 2 with a message instead of silently predicting nothing."""
        labels, rows = self._write(tmp_path, _LABELS, _ROWS)
        assert main(["evaluate", "--labels", labels, "--rows", rows, "--top-k", "0"]) == 2
        assert "top-k" in capsys.readouterr().err

    def test_tune_prints_threshold(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """``tune`` prints the chosen threshold and the per-candidate table.

        Same tuning sample as the library test.
        """
        labels, rows = self._write(tmp_path, _TUNE_LABELS, _TUNE_ROWS)
        assert main(["tune", "--labels", labels, "--rows", rows, "--candidates", "0.25,0.33,0.4"]) == 0
        assert json.loads(capsys.readouterr().out)["threshold"] == 0.33
