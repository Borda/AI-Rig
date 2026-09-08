"""Tests for extract_vitality_vars.py."""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest


from extract_vitality_vars import emit, extract_vars, main


def _scores(overrides: dict | None = None) -> dict:
    """Build complete vitality-score input with optional top-level overrides.

    Examples:
        >>> _scores({"health_score_pct": 99.0})["health_score_pct"]
        99.0
        >>> len(_scores()["axes"])
        9
    """
    base: dict = {
        "analysis_now": "1700000000",
        "overall_confidence": 0.85,
        "health_score_pct": 72.3,
        "axis3_202_pending": False,
        "total_passes": 2,
        "confidence_history": "0.80:0.85",
        "axes": {str(n): {"score": float(n), "conf": 0.9, "label": "🟢", "signal": f"sig{n}"} for n in range(1, 10)},
        "weights": {str(n): 0.11 for n in range(1, 10)},
    }
    if overrides:
        base.update(overrides)
    return base


class TestExtractVars:
    def test_top_level_fields(self) -> None:
        v = extract_vars(_scores())
        assert v["ANALYSIS_NOW"] == "1700000000"
        assert v["OVERALL_CONFIDENCE"] == "0.85"
        assert v["HEALTH_SCORE_PCT"] == "72.3"
        assert v["AXIS3_202_PENDING"] == "false"
        assert v["TOTAL_PASSES"] == "2"
        assert v["CONFIDENCE_HISTORY"] == "0.80:0.85"

    def test_axis3_202_pending_true(self) -> None:
        v = extract_vars(_scores({"axis3_202_pending": True}))
        assert v["AXIS3_202_PENDING"] == "true"

    def test_per_axis_score(self) -> None:
        v = extract_vars(_scores())
        for n in range(1, 10):
            assert v[f"AXIS{n}_SCORE"] == str(float(n))

    def test_per_axis_null_score(self) -> None:
        s = _scores()
        s["axes"]["3"] = {"score": None, "conf": 0.0, "label": "⚪", "signal": ""}
        v = extract_vars(s)
        assert v["AXIS3_SCORE"] == "-1"

    def test_per_axis_status_and_signal(self) -> None:
        v = extract_vars(_scores())
        assert v["AXIS1_STATUS"] == "🟢"
        assert v["AXIS1_SIGNAL"] == "sig1"

    def test_weight_rounding(self) -> None:
        s = _scores()
        s["weights"]["1"] = 0.17
        v = extract_vars(s)
        assert v["WEIGHT_1"] == "17"

    def test_missing_axis_uses_defaults(self) -> None:
        s = _scores()
        del s["axes"]["5"]
        v = extract_vars(s)
        assert v["AXIS5_SCORE"] == "-1"
        assert v["AXIS5_STATUS"] == "⚪"

    def test_confidence_history_fallback(self) -> None:
        s = _scores()
        del s["confidence_history"]
        v = extract_vars(s)
        assert v["CONFIDENCE_HISTORY"] == "0.85"


class TestEmit:
    def test_simple_values_unquoted(self) -> None:
        out = emit({"FOO": "bar"})
        assert out == "FOO=bar"

    def test_spaces_single_quoted(self) -> None:
        out = emit({"MSG": "hello world"})
        assert out == "MSG='hello world'"

    def test_multiple_vars_newline_separated(self) -> None:
        out = emit({"A": "1", "B": "2"})
        assert out == "A=1\nB=2"

    @pytest.mark.parametrize(
        "value",
        [
            "has 'single quotes'",
            'has "double quotes"',
            "semi;colon",
            "$(touch pwned)",
            "",
            "line1\nline2",
            "🟡 review needed",
        ],
    )
    def test_shell_sensitive_values_round_trip(self, value: str) -> None:
        assignment = shlex.split(emit({"MSG": value}))[0]
        key, decoded = assignment.split("=", 1)
        assert key == "MSG"
        assert decoded == value


class TestMain:
    def test_success(self, tmp_path: Path, capsys) -> None:
        f = tmp_path / "scores.json"
        f.write_text(json.dumps(_scores()))
        rc = main([str(f)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "ANALYSIS_NOW=" in out
        assert "AXIS1_SCORE=" in out
        assert "WEIGHT_9=" in out

    def test_no_args(self, capsys) -> None:
        rc = main([])
        assert rc == 1

    def test_missing_file(self, tmp_path: Path, capsys) -> None:
        rc = main([str(tmp_path / "missing.json")])
        assert rc == 1

    def test_invalid_json(self, tmp_path: Path, capsys) -> None:
        f = tmp_path / "bad.json"
        f.write_text("not json")
        rc = main([str(f)])
        assert rc == 1

    def test_golden_invocation_stdout_is_eval_safe(self, tmp_path: Path, capsys) -> None:
        """Documented call site ``extract_vitality_vars.py SCORES_FILE`` — stdout is ONLY VAR=value lines.

        The caller runs ``eval "$(... extract_vitality_vars.py "$SCORES_FILE")"`` so any stray argparse banner on stdout
        would corrupt the eval; every line must be an assignment.
        """
        f = tmp_path / "scores.json"
        f.write_text(json.dumps(_scores()))
        rc = main([str(f)])
        assert rc == 0
        lines = capsys.readouterr().out.strip().splitlines()
        assert lines and all("=" in ln for ln in lines)
