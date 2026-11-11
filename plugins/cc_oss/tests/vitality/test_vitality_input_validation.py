"""Check repository text preservation and scoring partial validation."""

from __future__ import annotations

import json
from pathlib import Path

import assemble_vitality_data as data
import assemble_vitality_scores as scores
import pytest


@pytest.mark.parametrize("separator", ["\u0085", "\u2028", "\u2029"])
def test_jsonl_preserves_unicode_separators(tmp_path: Path, separator: str) -> None:
    """Unicode line separators inside JSON strings remain part of their record."""
    record = {"type": "readme_content", "data": f"before{separator}after"}
    source = tmp_path / "data.jsonl"
    source.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
    assert data.read_existing_records(source) == [record]


@pytest.mark.parametrize("repo", ["owner/repo\n", "owner/repo\r\n"])
def test_repo_rejects_trailing_newline(tmp_path: Path, repo: str) -> None:
    """A newline cannot be part of the validated repository identifier."""
    with pytest.raises(SystemExit) as exc:
        data.main(["--data-file", str(tmp_path / "d"), "--group1-dir", str(tmp_path), "--repo", repo])
    assert exc.value.code == 2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("score", float("nan"), id="nan-score"),
        pytest.param("score", float("inf"), id="infinite-score"),
        pytest.param("score", -1, id="negative-score"),
        pytest.param("score", 11, id="large-score"),
        pytest.param("score", True, id="boolean-score"),
        pytest.param("conf", "high", id="string-confidence"),
        pytest.param("conf", None, id="null-confidence"),
        pytest.param("conf", float("nan"), id="nan-confidence"),
        pytest.param("conf", 1.1, id="large-confidence"),
    ],
)
def test_invalid_partial_fails_without_writing(tmp_path: Path, field: str, value: object) -> None:
    """Invalid numeric fields fail the CLI without emitting an invalid or misleading score."""
    axis = {"score": 7, "conf": 0.9, "label": "green", field: value}
    partial = tmp_path / "partial.json"
    partial.write_text(json.dumps({"axes": {"1": axis}, "scored_at": "now"}))
    target = tmp_path / "scores.json"
    assert scores.main([str(partial)] * 3 + [str(tmp_path / "rubric"), str(target)]) == 1
    assert not target.exists()


def test_all_unavailable_is_unknown_health(tmp_path: Path) -> None:
    """Unavailable axes yield null health and accept an absent unavailable reason."""
    partial = tmp_path / "partial.json"
    partial.write_text(
        json.dumps(
            {"axes": {"3": {"score": None, "conf": 0, "label": "⚪", "unavailable_reason": None}}, "scored_at": "now"}
        )
    )
    result = scores.assemble_scores(partial, partial, partial, tmp_path / "rubric")
    assert result["health_score_pct"] is None
    assert result["axis3_202_pending"] is False
    json.dumps(result, allow_nan=False)
