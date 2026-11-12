"""Check the instruction-file filter that keeps the lint and QA gate from skipping QA on non-documentation edits."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from _skill_source import read_skill_source

_GATE = Path(__file__).resolve().parents[2] / "skills/resolve/modes/lint-qa-gate.md"
_BASH = shutil.which("bash")
_skip_no_bash = pytest.mark.skipif(_BASH is None, reason="The gate filter is a Bash command.")


def _instruction_pattern() -> str:
    """Return the extended regex the shipped DOCS_ONLY line uses to recognise instruction files."""
    line = next(line for line in read_skill_source(_GATE).splitlines() if line.startswith("DOCS_ONLY=false;"))
    match = re.search(r"grep -qiE '([^']+)'", line)
    assert match, "instruction-file grep not found in the DOCS_ONLY line"
    return match.group(1)


@_skip_no_bash
@pytest.mark.parametrize(
    ("path", "is_instruction_file"),
    [
        pytest.param("commands/foo.md", True, id="top-level-commands-dir"),
        pytest.param("plugins/x/commands/foo.md", True, id="nested-commands-dir"),
        pytest.param("skills/a/SKILL.md", True, id="skills-dir"),
        pytest.param("docs/commands.md", False, id="commands-word-in-file-name"),
        pytest.param("docs/index.md", False, id="plain-doc"),
    ],
)
def test_instruction_filter_classifies_path(path: str, is_instruction_file: bool) -> None:
    """A documentation edit under ``commands/`` counts as an instruction change, so QA is not skipped.

    The filter keeps ``commands/`` while spelling it outside the alternation of other directory names; this pins that
    the split still matches the directory and nothing that merely contains the word.
    """
    completed = subprocess.run(
        [_BASH, "-c", 'printf "%s\\n" "$1" | grep -qiE "$2"', "bash", path, _instruction_pattern()],
        check=False,
        timeout=10,
    )
    assert (completed.returncode == 0) is is_instruction_file
