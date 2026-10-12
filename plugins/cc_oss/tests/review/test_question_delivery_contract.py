"""Keep review and analysis headlines in questions and complete report headers in final replies."""

from pathlib import Path

import pytest

_SKILLS = Path(__file__).resolve().parents[2] / "skills"


@pytest.mark.parametrize("skill", ["review", "analyse"])
def test_report_followup_uses_a_plain_summary_and_final_header(skill: str) -> None:
    """The first question carries the report facts without duplicating or clipping its header table."""
    text = (_SKILLS / skill / "SKILL.md").read_text(encoding="utf-8")
    assert "first question's text" in text
    assert "`Title`, `Outcome`" in text
    assert "target 600 characters, hard cap 1,500" in text
    assert "after the answer" in text
    assert "after its last tool call" in text
    assert "Long wait? `/compact` now" in text
    assert "no `preview`" in text
    assert "Do not print the header table before the follow-up question" in text
    assert "question summary does not satisfy final delivery" in text
    assert "`preview` of every option" not in text
    assert "preview cap" not in text
    assert "option previews carry" not in text


@pytest.mark.parametrize("mode", ["ecosystem", "thread", "vitality"])
def test_analysis_modes_build_the_header_for_final_delivery(mode: str) -> None:
    """All analysis modes recover a missing report into a question summary and a final header."""
    text = (_SKILLS / "analyse" / "modes" / f"{mode}.md").read_text(encoding="utf-8")
    assert "question text" in text
    assert "`Title`, `Outcome`" in text
    assert "final reply after the answer" in text
    assert "option previews" not in text
    assert "preview cap" not in text


def test_review_header_task_stays_open_until_final_delivery() -> None:
    """The old completed-before-question guard cannot suppress the new final-reply delivery."""
    text = (_SKILLS / "review" / "SKILL.md").read_text(encoding="utf-8")
    assert "keep it `in_progress` through the question" in text
    assert "must not fire while this task is `pending`/`in_progress`" not in text


def test_worktree_removal_shows_the_exact_list_before_asking() -> None:
    """Cleanup retains explicit consent with a visible list and error, rather than identical previews."""
    text = (_SKILLS / "_shared" / "worktree-isolation.md").read_text(encoding="utf-8")
    assert "Write the full list and the `add` error to `.temp/worktree-leftovers.md` first" in text
    assert "print them once as the last reply text before" in text
    assert "question text" in text
    assert "no `preview`" in text
    assert "never run the `--apply` form without that answer" in text
    assert "retry the create once" in text
    assert "`preview` of both options" not in text
