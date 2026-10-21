"""Tests for ``bin/require-vars.py`` — non-empty sentinel value assertion.

Covers all-present success, reporting every empty value rather than the first, and the pair-count validation.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "require-vars.py"
_spec = importlib.util.spec_from_file_location("research_require_vars", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

main = _mod.main


class TestRequireVars:
    """Exit status and stderr for value/message pairs."""

    @pytest.mark.parametrize(
        ("pairs", "expected_code", "expected_err"),
        [
            pytest.param(["run-1", "no run id"], 0, "", id="single-present"),
            pytest.param(["run-1", "no run id", "/tmp/x", "no dir"], 0, "", id="all-present"),
            pytest.param(["", "no run id"], 1, "no run id\n", id="single-empty"),
            pytest.param(
                ["", "no run id", "ok", "no dir", "", "no ledger"],
                1,
                "no run id\nno ledger\n",
                id="reports-every-empty",
            ),
        ],
    )
    def test_empty_values_are_reported(
        self, pairs: list[str], expected_code: int, expected_err: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Every empty value prints its own message, so one run surfaces all missing state.

        Stopping at the first empty value would force a fix-and-rerun cycle per missing sentinel.
        """
        code = main(["require-vars.py", *pairs])

        assert (code, capsys.readouterr().err) == (expected_code, expected_err)

    @pytest.mark.parametrize(
        "pairs",
        [
            pytest.param([], id="no-args"),
            pytest.param(["only-a-value"], id="value-without-message"),
            pytest.param(["a", "msg", "b"], id="odd-count"),
        ],
    )
    def test_malformed_pairs_exit_two(self, pairs: list[str]) -> None:
        """Fewer than one pair, or an odd argument count, exits 2."""
        assert main(["require-vars.py", *pairs]) == 2
