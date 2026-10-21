"""Tests for ``bin/gate-on-sentinel.py`` — read-default-compare phase gate.

Covers the match path, mismatch (gate closed, message plus actual value on stderr), the per-caller default for a missing
or empty sentinel, the unterminated-line rule that mirrors ``IFS= read -r``, and argument validation.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "gate-on-sentinel.py"
_spec = importlib.util.spec_from_file_location("research_gate_on_sentinel", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

main = _mod.main


class TestGate:
    """Exit status and stderr for a four-argument invocation."""

    @pytest.mark.parametrize(
        ("content", "default", "expected_code", "actual"),
        [
            pytest.param("APPROVED\n", "MISSING", 0, None, id="value-matches"),
            pytest.param("REJECTED\n", "MISSING", 1, "REJECTED", id="value-mismatch"),
            pytest.param(None, "APPROVED", 0, None, id="missing-file-takes-open-default"),
            pytest.param(None, "MISSING", 1, "MISSING", id="missing-file-takes-closed-default"),
            pytest.param("\n", "MISSING", 1, "MISSING", id="empty-line-takes-default"),
            pytest.param("APPROVED", "MISSING", 1, "MISSING", id="unterminated-line-takes-default"),
        ],
    )
    def test_gate_decision(
        self,
        content: str | None,
        default: str,
        expected_code: int,
        actual: str | None,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The sentinel's first terminated line, else the default, is compared with the expected value.

        An unterminated last line reads as empty because ``IFS= read -r`` exits non-zero on it and the caller's ``||
        VAR=""`` wipes the partial value; the gate must fall back to the default the same way.
        """
        sentinel = tmp_path / "verdict"
        if content is not None:
            sentinel.write_bytes(content.encode("utf-8"))

        code = main(["gate-on-sentinel.py", str(sentinel), "APPROVED", default, "gate closed"])

        err = capsys.readouterr().err
        assert code == expected_code
        if actual is None:
            assert err == ""
        else:
            assert err == f"gate closed\n  actual: {actual}\n"


class TestArguments:
    """Argument validation."""

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param(["gate-on-sentinel.py", "s", "e", "d"], id="three-args"),
            pytest.param(["gate-on-sentinel.py", "s", "e", "d", "m", "extra"], id="five-args"),
            pytest.param(["gate-on-sentinel.py", "", "e", "d", "m"], id="empty-sentinel-path"),
        ],
    )
    def test_bad_arguments_exit_two(self, argv: list[str]) -> None:
        """Anything but four arguments with a non-empty sentinel path exits 2."""
        assert main(argv) == 2
