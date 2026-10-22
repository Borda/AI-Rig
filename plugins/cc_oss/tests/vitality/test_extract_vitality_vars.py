"""Tests for extract_vitality_vars.py."""

from __future__ import annotations

import json
import shlex
import shutil
import stat
import subprocess
import sys
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
    @pytest.mark.parametrize(
        ("variables", "expected"),
        [
            pytest.param({"FOO": "bar"}, "FOO=bar", id="simple-values-unquoted"),
            pytest.param({"MSG": "hello world"}, "MSG='hello world'", id="spaces-single-quoted"),
            pytest.param({"A": "1", "B": "2"}, "A=1\nB=2", id="multiple-vars-newline-separated"),
        ],
    )
    def test_emit_formats_assignments(self, variables: dict[str, str], expected: str) -> None:
        """Plain values stay unquoted, values with spaces are single-quoted, several vars are newline-separated."""
        assert emit(variables) == expected

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

        Legacy consumers parse shell assignments, so a stray argparse banner would corrupt the output contract; every
        line must be an assignment. Session-file consumers use --keep-state instead.
        """
        f = tmp_path / "scores.json"
        f.write_text(json.dumps(_scores()))
        rc = main([str(f)])
        assert rc == 0
        lines = capsys.readouterr().out.strip().splitlines()
        assert lines
        assert all("=" in ln for ln in lines)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("$(touch pwned); 'quoted' \\backslash", id="shell-metacharacters"),
        pytest.param("", id="empty-clears-stale-value"),
    ],
)
def test_keep_state_private_values(tmp_path, monkeypatch, capsys, value):
    """State mode emits no shell source, overwrites stale values, and stores literal UTF-8 privately."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "vitality-test")
    scores = _scores({"axes": {"1": {"signal": value}}})
    path = tmp_path / "scores.json"
    path.write_text(json.dumps(scores), encoding="utf-8")
    target = tmp_path / "vars-axis1-signal-vitality-test"
    target.write_text("stale\n", encoding="utf-8")
    assert main(["--keep-state", "vars", str(path)]) == 0
    assert capsys.readouterr().out == ""
    for key, expected in extract_vars(scores).items():
        sentinel = tmp_path / f"vars-{key.lower().replace('_', '-')}-vitality-test"
        assert sentinel.read_bytes() == (expected + "\n").encode()
        if sys.platform != "win32":
            assert stat.S_IMODE(sentinel.stat().st_mode) == 0o600
    assert not (tmp_path / "pwned").exists()


def test_keep_state_folds_multiline_signal(tmp_path, monkeypatch):
    """A multi-line signal is stored on one line, because the documented `IFS= read -r` keeps only the first line.

    The agent writes axis signals as free prose; the removed `eval` path kept embedded newlines, but a one-line sentinel
    would silently truncate the report text at the first break.
    """
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "vitality-test")
    path = tmp_path / "scores.json"
    path.write_text(
        json.dumps(_scores({"axes": {"1": {"signal": "first line\nsecond line\r\nthird"}}})), encoding="utf-8"
    )
    assert main(["--keep-state", "vars", str(path)]) == 0
    assert (tmp_path / "vars-axis1-signal-vitality-test").read_text(
        encoding="utf-8"
    ) == "first line second line third\n"


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required for the documented sentinel reader")
@pytest.mark.parametrize(
    "value",
    [pytest.param("$(touch pwned); \\ literal", id="metacharacters"), pytest.param("", id="empty")],
)
def test_keep_state_bash_read_round_trip(tmp_path, monkeypatch, value):
    """The documented read command never evaluates shell syntax in a score field."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "vitality-test")
    path = tmp_path / "scores.json"
    path.write_text(json.dumps(_scores({"confidence_history": value})), encoding="utf-8")
    assert main(["--keep-state", "vars", str(path)]) == 0
    result = subprocess.run(
        [
            shutil.which("bash"),
            "-c",
            'VAR=stale; IFS= read -r VAR < "$1" 2>/dev/null || VAR=""; printf "%s" "$VAR"',
            "reader",
            str(tmp_path / "vars-confidence-history-vitality-test"),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode, result.stdout) == (0, value)
    assert not (tmp_path / "pwned").exists()
