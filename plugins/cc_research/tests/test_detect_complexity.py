"""Tests for ``bin/detect-complexity.py`` — judge program complexity verdict.

Covers the three complexity signals (multiple scope files, ``agent_strategy: arch``, cross-domain keywords), the simple
verdict, and the fail-closed paths: an unreadable program or a missing argument must never read as "simple".
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

# Hyphenated script name: load by path, as the sibling bin tests do.
_SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "detect-complexity.py"
_spec = importlib.util.spec_from_file_location("research_detect_complexity", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

main = _mod.main


def _verdict(program: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    """Run ``main`` on ``program`` and return ``(exit code, stdout verdict)``."""
    code = main(["detect-complexity.py", str(program)])
    return code, capsys.readouterr().out.strip()


class TestVerdict:
    """The verdict printed for a readable program."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            pytest.param("goal: speed up\nscope:\n- src/a.py\n", "false", id="one-scope-file"),
            pytest.param("scope:\n- src/a.py\n- src/b.py\n", "true", id="two-scope-files"),
            pytest.param("scope:\n- src/a.py\nagent_strategy: arch\n", "true", id="arch-strategy"),
            pytest.param("scope:\n- src/a.py\nagent_strategy: perf\n", "false", id="other-strategy"),
            pytest.param("scope:\n- src/a.py\nA distributed training run\n", "true", id="cross-domain-keyword"),
            pytest.param("", "false", id="empty-program"),
        ],
    )
    def test_classifies_program(
        self, text: str, expected: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Each signal flips the verdict to ``true``; a plain single-file program stays ``false``.

        The judge spends architect and Codex passes only on ``true``, so every signal needs its own case.
        """
        program = tmp_path / "program.md"
        program.write_text(text, encoding="utf-8")

        code, out = _verdict(program, capsys)

        assert (code, out) == (0, expected)


class TestFailClosed:
    """Inputs that must never produce a "simple" verdict."""

    def test_missing_file_prints_true_and_exits_two(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """An unreadable program prints ``true`` and exits 2.

        Consumers capture stdout only, so a ``false`` here would skip the review passes on a program never read.
        """
        code, out = _verdict(tmp_path / "absent.md", capsys)

        assert (code, out) == (2, "true")

    def test_missing_argument_exits_two(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Calling without a program path exits 2 with a diagnostic and no verdict."""
        code = main(["detect-complexity.py"])

        captured = capsys.readouterr()
        assert code == 2
        assert captured.out == ""
        assert "missing <program-path>" in captured.err
