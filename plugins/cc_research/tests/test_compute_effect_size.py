"""Tests for ``compute_effect_size.py``.

Covers:
    - ``rank_biserial_r`` pure-function contract — boundary values, validation.
    - ``compute_from_payload`` payload contract — None statistic, missing keys, type errors.
    - ``main()``: stdin parsing, exit codes, exact stdout shape vs original inline block.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import compute_effect_size as ces
import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "compute_effect_size.py"


# ---------- Pure function: rank_biserial_r ----------


class TestRankBiserialR:
    """Effect-size formula contract: r = 4*W/(n*(n+1)) - 1."""

    def test_zero_statistic_returns_minus_one(self) -> None:
        """Map an all-negative signed-rank result to the lower effect-size bound."""
        assert ces.rank_biserial_r(0.0, 8) == -1.0

    def test_max_statistic_returns_plus_one(self) -> None:
        """Map an all-positive signed-rank result to the upper effect-size bound."""
        # n=8 → max W = 36; r should be 1.0
        assert ces.rank_biserial_r(36.0, 8) == 1.0

    def test_midpoint_statistic_is_zero(self) -> None:
        """W at n*(n+1)/4 maps to r=0 — neutral."""
        # n=8 → midpoint W = 18; r should be 0.0
        assert ces.rank_biserial_r(18.0, 8) == 0.0

    def test_zero_n_raises(self) -> None:
        """N must be positive — n=0 raises ValueError."""
        with pytest.raises(ValueError, match="n must be positive"):
            ces.rank_biserial_r(5.0, 0)

    def test_negative_n_raises(self) -> None:
        """Negative n is invalid — raises ValueError."""
        with pytest.raises(ValueError, match="n must be positive"):
            ces.rank_biserial_r(5.0, -3)


# ---------- Payload glue: compute_from_payload ----------


class TestComputeFromPayload:
    """JSON payload to printed-line glue — preserves original inline-block behavior."""

    def test_none_statistic_returns_empty_string(self) -> None:
        """Insufficient data (statistic=None) → empty line, matching inline block."""
        assert ces.compute_from_payload({"n": 8, "statistic": None}) == ""

    def test_missing_statistic_key_returns_empty_string(self) -> None:
        """Treat a missing statistic key like an explicit null value."""
        assert ces.compute_from_payload({"n": 8}) == ""

    def test_numeric_statistic_produces_str_of_float(self) -> None:
        """Output is ``str(r)`` — same shape as Python ``print`` in the inline block."""
        # n=8, statistic=36.0 → r=1.0 → "1.0"
        assert ces.compute_from_payload({"n": 8, "statistic": 36.0}) == "1.0"

    def test_extra_keys_are_ignored(self) -> None:
        """retro_analyze emits 'p_value', 'significant', 'reason' — must not interfere."""
        payload = {"n": 8, "statistic": 36.0, "p_value": 0.01, "significant": True}
        assert ces.compute_from_payload(payload) == "1.0"

    def test_missing_n_raises(self) -> None:
        """Required key 'n' missing → ValueError."""
        with pytest.raises(ValueError, match="missing required key 'n'"):
            ces.compute_from_payload({"statistic": 5.0})

    def test_non_int_n_raises(self) -> None:
        """'n' must be int — float n rejected (would corrupt formula)."""
        with pytest.raises(ValueError, match="'n' must be int"):
            ces.compute_from_payload({"n": 8.0, "statistic": 5.0})

    def test_bool_n_rejected(self) -> None:
        """Python bool is technically int — explicitly reject to avoid silent corruption."""
        with pytest.raises(ValueError, match="'n' must be int"):
            ces.compute_from_payload({"n": True, "statistic": 5.0})

    def test_non_numeric_statistic_raises(self) -> None:
        """Statistic must be numeric or null — strings rejected."""
        with pytest.raises(ValueError, match="'statistic' must be numeric or null"):
            ces.compute_from_payload({"n": 8, "statistic": "0.5"})


# ---------- CLI: main() ----------


class TestArgparseCLI:
    """Argparse-surface test: ``--help`` exits 0 without touching the stdin contract."""

    def test_help_exits_zero(self) -> None:
        """Print usage and exit 0 (argparse contract); stdin is never read."""
        result = subprocess.run([sys.executable, str(_SCRIPT), "--help"], capture_output=True, text=True)
        assert result.returncode == 0
        assert "usage" in result.stdout.lower()


class TestMainCLI:
    """End-to-end stdin/stdout/exit-code contract."""

    def test_valid_payload_exits_zero_and_prints_r(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Well-formed retro_analyze JSON → exit 0, single line of effect size."""
        payload = json.dumps({"n": 8, "statistic": 36.0, "p_value": 0.01, "significant": True})
        monkeypatch.setattr("sys.stdin", _StdinStub(payload))
        exit_code = ces.main([])
        captured = capsys.readouterr()
        assert exit_code == 0
        assert captured.out == "1.0\n"
        assert captured.err == ""

    def test_none_statistic_exits_zero_with_empty_line(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Insufficient data (statistic=null) → exit 0, empty stdout line."""
        payload = json.dumps({"n": 3, "statistic": None, "reason": "insufficient data"})
        monkeypatch.setattr("sys.stdin", _StdinStub(payload))
        exit_code = ces.main([])
        captured = capsys.readouterr()
        assert exit_code == 0
        assert captured.out == "\n"

    def test_malformed_json_exits_two(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Broken JSON on stdin → exit 2 with error on stderr."""
        monkeypatch.setattr("sys.stdin", _StdinStub('{"n": 8, "statistic":'))
        exit_code = ces.main([])
        captured = capsys.readouterr()
        assert exit_code == 2
        assert "malformed JSON" in captured.err

    def test_non_object_json_exits_two(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """JSON array (not object) → exit 2."""
        monkeypatch.setattr("sys.stdin", _StdinStub("[1, 2, 3]"))
        exit_code = ces.main([])
        captured = capsys.readouterr()
        assert exit_code == 2
        assert "expected JSON object" in captured.err

    def test_missing_n_exits_two(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Payload missing 'n' → exit 2 with descriptive error."""
        monkeypatch.setattr("sys.stdin", _StdinStub('{"statistic": 5.0}'))
        exit_code = ces.main([])
        captured = capsys.readouterr()
        assert exit_code == 2
        assert "missing required key 'n'" in captured.err


class _StdinStub:
    """Minimal stdin replacement supporting ``.read()`` for monkeypatch."""

    def __init__(self, content: str) -> None:
        """Store parser input that ``read`` will return."""
        self._content = content

    def read(self) -> str:
        """Return the configured stdin text to the parser under test."""
        return self._content
