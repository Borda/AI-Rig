"""Judge complexity counts all declared scope entries and handles repeated phrases linearly."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from _research_text import load_module

_mod = load_module(Path(__file__).resolve().parents[1] / "bin" / "detect-complexity.py", "scope_complexity")


@pytest.mark.parametrize(
    "scope",
    [
        "scope_files:\n  - Makefile\n  - config.yaml\n",
        "scope_files:\n  - src/\n  - tests/**/*.py\n",
        "scope_files: [Makefile, config.yaml]\n",
        "scope_files:\n- docs/readme.md\n- data/input.csv\n",
        'scope_files:\n-\t"src/a.py"\n-\t"src/b.py"\n',
    ],
)
def test_multiple_scope_entries_trigger_full_review(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], scope: str
) -> None:
    """Unknown extensions, directories and globs still represent separate scope entries."""
    program = tmp_path / "program.md"
    program.write_text(scope, encoding="utf-8")
    assert _mod.main(["detect-complexity.py", str(program)]) == 0
    assert capsys.readouterr().out == "true\n"


def test_notes_bullets_do_not_inflate_scope(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Bullets outside the declared scope do not count as files."""
    program = tmp_path / "program.md"
    program.write_text("scope_files:\n  - Makefile\nnotes:\n  - first note\n  - second note\n", encoding="utf-8")
    assert _mod.main(["detect-complexity.py", str(program)]) == 0
    assert capsys.readouterr().out == "false\n"


def test_notes_scope_label_does_not_inflate_scope(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A prose scope label in Notes does not add a second declared file."""
    program = tmp_path / "program.md"
    program.write_text("scope_files:\n  - Makefile\n## Notes\nscope: keep it narrow\n", encoding="utf-8")
    _mod.main(["detect-complexity.py", str(program)])
    assert capsys.readouterr().out == "false\n"


@pytest.mark.parametrize("phrase", ["multiple", "pipeline"])
def test_repeated_keyword_without_suffix_stays_simple(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], phrase: str
) -> None:
    """Repeated prefix words without their required suffix do not trigger a cross-domain signal."""
    program = tmp_path / "program.md"
    program.write_text((phrase + " ") * 2000, encoding="utf-8")
    assert _mod.main(["detect-complexity.py", str(program)]) == 0
    assert capsys.readouterr().out == "false\n"


@pytest.mark.parametrize("phrase", ["multiple", "pipeline"])
def test_long_repeated_keyword_finishes_within_cli_budget(tmp_path: Path, phrase: str) -> None:
    """A long nonmatching phrase cannot consume the judge's command timeout through rescanning."""
    program = tmp_path / "program.md"
    program.write_text((phrase + " ") * 40000, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(Path(_mod.__file__)), str(program)],
        capture_output=True,
        text=True,
        check=False,
        timeout=2,
    )
    assert result.returncode == 0
    assert result.stdout == "false\n"
