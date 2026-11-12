"""Verify worktree skills clear their local contract before invoking the exit tool."""

from pathlib import Path

import pytest

_SKILLS = Path(__file__).resolve().parents[2] / "skills"


@pytest.mark.parametrize("skill", ["feature", "fix", "refactor", "debug"])
def test_contract_cleanup_precedes_worktree_exit(skill: str) -> None:
    """The cleanup command must execute while the skill still occupies its worktree."""
    lines = (_SKILLS / skill / "SKILL.md").read_text(encoding="utf-8").splitlines()
    exit_line = next(index for index, line in enumerate(lines) if line.startswith("**Worktree exit**"))
    cleanup_line = next(
        index for index, line in enumerate(lines) if line.startswith("rm -f .temp/state/skill-contract.md")
    )
    assert cleanup_line < exit_line
