"""Pin Claude plan and rename decision delivery independently of Codex controls."""

from pathlib import Path

import pytest

_SKILLS = Path(__file__).resolve().parents[2] / "claude-skills"


@pytest.mark.parametrize("mode", ["apply", "sync"])
def test_integration_approval_shows_the_saved_plan_before_question(mode: str) -> None:
    """Both mutation gates show the plan once and bind a plain question to its exact digest."""
    text = (
        (_SKILLS / "integration/SKILL.md").read_text(encoding="utf-8").split(f"**`{mode}`**", 1)[1].split("```", 1)[0]
    )
    for required in ("plan artifact", "SHA-256", "last reply text before", "question text", "1,500", "no previews"):
        assert required in text
    assert "preview cap" not in text.lower()


def test_rename_candidates_are_saved_and_shown_before_selection() -> None:
    """Candidate selection retains identity fields, a named list and the original narrowing sentinel."""
    text = (
        (_SKILLS / "rename-refs/SKILL.md")
        .read_text(encoding="utf-8")
        .split("- Multiple →", 1)[1]
        .split("Full qname:", 1)[0]
    )
    for required in (
        "name/type/module/path",
        "candidate list",
        "last reply text before",
        "question text",
        "no previews",
        "CHOSEN_IDX",
    ):
        assert required in text


@pytest.mark.parametrize("gate", ["**`--dry-run`**:", "Otherwise `AskUserQuestion`:"])
def test_rename_confirmation_delivers_change_sites_before_choice(gate: str) -> None:
    """Dry-run and real apply choices show their complete report and carry counts plus the path."""
    text = (_SKILLS / "rename-refs/SKILL.md").read_text(encoding="utf-8").split(gate, 1)[1].split("## Step 4:", 1)[0]
    for required in ("last reply text before", "question text", "no previews"):
        assert required in text


def test_rename_question_limits_and_destructive_guards_remain_explicit() -> None:
    """Compact questions retain the exhaustive zero-caller and dry-run safety boundaries."""
    text = (_SKILLS / "rename-refs/SKILL.md").read_text(encoding="utf-8")
    assert "1,500" in text
    assert "at most 6 plain lines" in text
    assert "`EXHAUSTIVE=false`" in text
    assert "`RDEP_HINT` non-empty" in text
    assert "No `AskUserQuestion`, no `Edit` — dry-run means no edits, including the delete." in text
