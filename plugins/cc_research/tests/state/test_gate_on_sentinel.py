"""Tests for ``bin/gate-on-sentinel.py`` — read-default-compare phase gate.

Covers the match path, mismatch (gate closed, message plus actual value on stderr), the per-caller default for a missing
or empty sentinel, the unterminated-line rule that mirrors ``IFS= read -r``, and argument validation.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _research_text import load_module

_mod = load_module(Path(__file__).resolve().parents[2] / "bin" / "gate-on-sentinel.py", "research_gate_on_sentinel")

main = _mod.main


class TestGate:
    """Exit status and stderr for a four-argument invocation."""

    @pytest.mark.parametrize(
        ("filename", "content", "default", "expected_code", "expected_err"),
        [
            pytest.param("verdict", b"APPROVED\n", "MISSING", 0, "", id="value-matches"),
            pytest.param(
                "verdict", b"REJECTED\n", "MISSING", 1, "gate closed\n  actual: REJECTED\n", id="value-mismatch"
            ),
            pytest.param("absent", b"", "APPROVED", 0, "", id="missing-file-takes-open-default"),
            pytest.param(
                "absent", b"", "MISSING", 1, "gate closed\n  actual: MISSING\n", id="missing-file-takes-closed-default"
            ),
            pytest.param(
                "verdict", b"\n", "MISSING", 1, "gate closed\n  actual: MISSING\n", id="empty-line-takes-default"
            ),
            pytest.param(
                "verdict",
                b"APPROVED",
                "MISSING",
                1,
                "gate closed\n  actual: MISSING\n",
                id="unterminated-line-takes-default",
            ),
        ],
    )
    def test_gate_decision(
        self,
        filename: str,
        content: bytes,
        default: str,
        expected_code: int,
        expected_err: str,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The sentinel's first terminated line, else the default, is compared with the expected value.

        An unterminated last line reads as empty because ``IFS= read -r`` exits non-zero on it and the caller's ``||
        VAR=""`` wipes the partial value; the gate must fall back to the default the same way. ``filename`` picks
        whether the sentinel ``verdict`` exists (with ``content``) or the path points at a file that was never written.
        """
        (tmp_path / "verdict").write_bytes(content)

        code = main(["gate-on-sentinel.py", str(tmp_path / filename), "APPROVED", default, "gate closed"])

        assert code == expected_code
        assert capsys.readouterr().err == expected_err


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
