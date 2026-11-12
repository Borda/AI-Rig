"""Distinguish Bash-tool hints from portable standalone execution deadlines."""

from pathlib import Path

import check_skill_contracts as csc
import pytest


def test_bash_tool_hint_does_not_require_a_shell_wrapper() -> None:
    """A Bash-tool annotation is satisfied by the caller's tool timeout."""
    assert csc.unenforced_timeouts(Path("skill.md"), ["git status # timeout: 5000"]) == []


def test_standalone_hint_does_not_enforce_a_deadline() -> None:
    """The same comment in standalone execution still needs an enforced deadline."""
    assert len(csc.unenforced_timeouts(Path("script.md"), ["git status # timeout: 5000"], bash_tool=False)) == 1


@pytest.mark.parametrize(
    "text",
    [
        'import subprocess\nsubprocess.run(["x"], timeout=5)',
        'import subprocess\nLIMIT = 5\nsubprocess.run(["x"], timeout=LIMIT)',
    ],
)
def test_fixed_deadline_needs_no_configurable_override(text: str) -> None:
    """A positive fixed internal deadline exempts the helper from exposing --timeout."""
    assert csc.missing_timeout_flag(Path("helper.py"), text) is None


def test_standalone_python_requires_internal_deadline_evidence() -> None:
    """A Python command name alone does not prove that standalone execution is bounded."""
    assert len(csc.unenforced_timeouts(Path("script.md"), ["python task.py # timeout: 5000"], bash_tool=False)) == 1
