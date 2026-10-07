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

    @pytest.mark.parametrize(
        ("statistic", "expected"),
        [
            pytest.param(0.0, -1.0, id="zero-statistic-is-minus-one"),
            pytest.param(36.0, 1.0, id="max-statistic-is-plus-one"),
            pytest.param(18.0, 0.0, id="midpoint-statistic-is-zero"),
        ],
    )
    def test_statistic_maps_to_effect_size(self, statistic: float, expected: float) -> None:
        """Signed-rank statistics at n=8 map onto the effect-size range [-1, 1].

        W=0 is the all-negative lower bound, W=36 (the maximum for n=8) is the all-positive upper bound, and the
        midpoint W=18 is neutral (r=0).
        """
        assert ces.rank_biserial_r(statistic, 8) == expected

    @pytest.mark.parametrize("n", [0, -3])
    def test_non_positive_n_raises(self, n: int) -> None:
        """N must be positive — zero and negative n raise ValueError.

        The formula divides by ``n*(n+1)``, so a non-positive sample size has no meaningful effect size; both boundaries
        (n=0, n=-3) are rejected with the same message.
        """
        with pytest.raises(ValueError, match="n must be positive"):
            ces.rank_biserial_r(5.0, n)


# ---------- Payload glue: compute_from_payload ----------


class TestComputeFromPayload:
    """JSON payload to printed-line glue — preserves original inline-block behavior."""

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            pytest.param({"n": 8, "statistic": None}, "", id="none-statistic-is-empty"),
            pytest.param({"n": 8}, "", id="missing-statistic-key-is-empty"),
            pytest.param({"n": 8, "statistic": 36.0}, "1.0", id="numeric-statistic-is-str-of-float"),
            pytest.param(
                {"n": 8, "statistic": 36.0, "p_value": 0.01, "significant": True}, "1.0", id="extra-keys-ignored"
            ),
        ],
    )
    def test_payload_renders_printed_line(self, payload: dict[str, object], expected: str) -> None:
        """A valid payload renders the printed line exactly as the original inline block did.

        Insufficient data (statistic null or absent) → empty line. A numeric statistic → ``str(r)`` — the same shape as
        Python ``print`` (n=8, statistic=36.0 → r=1.0 → ``"1.0"``). Extra keys retro_analyze emits ('p_value',
        'significant', 'reason') must not interfere.
        """
        assert ces.compute_from_payload(payload) == expected

    @pytest.mark.parametrize(
        ("payload", "message"),
        [
            pytest.param({"statistic": 5.0}, "missing required key 'n'", id="missing-n"),
            pytest.param({"n": 8.0, "statistic": 5.0}, "'n' must be int", id="float-n-rejected"),
            pytest.param({"n": True, "statistic": 5.0}, "'n' must be int", id="bool-n-rejected"),
            pytest.param({"n": 8, "statistic": "0.5"}, "'statistic' must be numeric or null", id="string-statistic"),
        ],
    )
    def test_invalid_payload_raises(self, payload: dict[str, object], message: str) -> None:
        """A malformed payload raises ValueError naming the offending field.

        Required key 'n' missing; 'n' must be int — a float would corrupt the formula, and a Python bool is technically
        an int but is explicitly rejected to avoid silent corruption; the statistic must be numeric or null, so strings
        are rejected.
        """
        with pytest.raises(ValueError, match=message):
            ces.compute_from_payload(payload)


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

    @pytest.mark.parametrize(
        ("payload", "expected_out"),
        [
            pytest.param(
                {"n": 8, "statistic": 36.0, "p_value": 0.01, "significant": True},
                "1.0\n",
                id="valid-payload-prints-r",
            ),
            pytest.param(
                {"n": 3, "statistic": None, "reason": "insufficient data"}, "\n", id="none-statistic-prints-empty-line"
            ),
        ],
    )
    def test_valid_payload_exits_zero(
        self,
        payload: dict[str, object],
        expected_out: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Well-formed retro_analyze JSON → exit 0, single line of effect size.

        Insufficient data (statistic=null) still exits 0 and prints an empty stdout line.
        """
        monkeypatch.setattr("sys.stdin", _StdinStub(json.dumps(payload)))
        exit_code = ces.main([])
        captured = capsys.readouterr()
        assert exit_code == 0
        assert captured.out == expected_out
        assert captured.err == ""

    @pytest.mark.parametrize(
        ("stdin_text", "message"),
        [
            pytest.param('{"n": 8, "statistic":', "malformed JSON", id="malformed-json"),
            pytest.param("[1, 2, 3]", "expected JSON object", id="non-object-json"),
            pytest.param('{"statistic": 5.0}', "missing required key 'n'", id="missing-n"),
        ],
    )
    def test_invalid_stdin_exits_two(
        self,
        stdin_text: str,
        message: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Broken JSON, a non-object JSON value, or a payload missing 'n' → exit 2 with a descriptive stderr error.

        Each case feeds a different invalid stdin shape — truncated JSON, a JSON array instead of an object, and an
        object without the required 'n' key — and expects the same exit code with an error naming the problem.
        """
        monkeypatch.setattr("sys.stdin", _StdinStub(stdin_text))
        exit_code = ces.main([])
        captured = capsys.readouterr()
        assert exit_code == 2
        assert message in captured.err


class _StdinStub:
    """Minimal stdin replacement supporting ``.read()`` for monkeypatch."""

    def __init__(self, content: str) -> None:
        """Store parser input that ``read`` will return."""
        self._content = content

    def read(self) -> str:
        """Return the configured stdin text to the parser under test."""
        return self._content
