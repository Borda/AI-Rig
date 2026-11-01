"""Pin Claude plan and rename decision delivery independently of Codex controls.

The skills are Markdown prose, so each section is cut out of whitespace-collapsed text: an assertion matches words and
markers, never where a paragraph happens to wrap.
"""

from pathlib import Path

import pytest
from _codemap_skill_text import between, flat_file, missing

_SKILLS = Path(__file__).resolve().parents[2] / "claude-skills"
_RENAME_SKILL = _SKILLS / "rename-refs" / "SKILL.md"
_DELIVERY_NOTES = ("last reply text before", "question text", "no previews")


@pytest.mark.parametrize("mode", ["apply", "sync"])
def test_integration_approval_shows_the_saved_plan_before_question(mode: str) -> None:
    """Both mutation gates show the plan once and bind a plain question to its exact digest.

    In the integration skill's ``apply`` and ``sync`` sections the plan artifact is printed as the last reply text
    before the question, the question names its SHA-256 and obeys the 1,500-character cap, and no preview is used.
    """
    text = between(flat_file(_SKILLS / "integration" / "SKILL.md"), f"**`{mode}`**", "```")

    assert (
        missing(text, ("plan artifact", "SHA-256", "last reply text before", "question text", "1,500", "no previews"))
        == []
    )
    assert "preview cap" not in text.lower()


def test_rename_candidates_are_saved_and_shown_before_selection() -> None:
    """Candidate selection retains identity fields, a named list and the original narrowing sentinel.

    When the symbol matches several definitions, the candidate list (name/type/module/path) is shown before the question
    and the choice is recorded in ``CHOSEN_IDX``.
    """
    text = between(flat_file(_RENAME_SKILL), "- Multiple →", "Full qname:")

    assert missing(text, ("name/type/module/path", "candidate list", *_DELIVERY_NOTES, "CHOSEN_IDX")) == []


@pytest.mark.parametrize(
    "gate",
    [
        pytest.param("**`--dry-run`**:", id="dry-run"),
        pytest.param("Otherwise `AskUserQuestion`:", id="real-apply"),
    ],
)
def test_rename_confirmation_delivers_change_sites_before_choice(gate: str) -> None:
    """Dry-run and real apply choices show their complete report and carry counts plus the path.

    Both confirmation gates in ``rename-refs`` print the full change-site report as the last reply text, then ask a
    compact question without previews.
    """
    text = between(flat_file(_RENAME_SKILL), gate, "## Step 4:")

    assert missing(text, _DELIVERY_NOTES) == []


def test_rename_question_limits_and_destructive_guards_remain_explicit() -> None:
    """Compact questions retain the exhaustive zero-caller and dry-run safety boundaries.

    The question-size cap and the guards that stop a delete or an edit during ``--dry-run`` must stay spelled out in the
    skill text.
    """
    assert (
        missing(
            flat_file(_RENAME_SKILL),
            (
                "1,500",
                "at most 6 plain lines",
                "`EXHAUSTIVE=false`",
                "`RDEP_HINT` non-empty",
                "No `AskUserQuestion`, no `Edit` — dry-run means no edits, including the delete.",
            ),
        )
        == []
    )
