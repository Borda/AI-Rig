"""Keep review and analysis headlines in questions and complete report headers in final replies."""

from functools import cache
from pathlib import Path

import pytest

_SKILLS = Path(__file__).resolve().parents[2] / "skills"


@cache
def _read(*parts: str) -> str:
    """Read one shipped skill file by path parts below ``skills/``; the text is immutable during a run."""
    return _SKILLS.joinpath(*parts).read_text(encoding="utf-8")


def _missing(text: str, *required: str) -> list[str]:
    """Return the required phrases absent from ``text`` so a failure names every missing one."""
    return [phrase for phrase in required if phrase not in text]


def _present(text: str, *retired: str) -> list[str]:
    """Return the retired phrases still present in ``text`` so a failure names every leftover."""
    return [phrase for phrase in retired if phrase in text]


@pytest.mark.parametrize("skill", ["review", "analyse"])
def test_report_followup_uses_a_plain_summary_and_final_header(skill: str) -> None:
    """The first question carries the report facts without duplicating or clipping its header table.

    Both report-producing skills must put a plain-text summary in the question, defer the header table to the final
    reply after the answer, and no longer describe option previews or a preview cap.
    """
    text = _read(skill, "SKILL.md")
    missing = _missing(
        text,
        "first question's text",
        "`Title`, `Outcome`",
        "target 600 characters, hard cap 1,500",
        "after the answer",
        "after its last tool call",
        "Long wait? `/compact` now",
        "no `preview`",
        "Do not print the header table before the follow-up question",
        "question summary does not satisfy final delivery",
    )
    assert missing == []
    assert _present(text, "`preview` of every option", "preview cap", "option previews carry") == []


@pytest.mark.parametrize("mode", ["ecosystem", "thread", "vitality"])
def test_analysis_modes_build_the_header_for_final_delivery(mode: str) -> None:
    """All analysis modes recover a missing report into a question summary and a final header.

    A mode that rebuilds the report must still end with the complete header in its final reply, and must not fall back
    to the retired option-preview wording.
    """
    text = _read("analyse", "modes", f"{mode}.md")
    assert _missing(text, "question text", "`Title`, `Outcome`", "final reply after the answer") == []
    assert _present(text, "option previews", "preview cap") == []


def test_review_header_task_stays_open_until_final_delivery() -> None:
    """The old completed-before-question guard cannot suppress the new final-reply delivery.

    The header task must stay ``in_progress`` through the question; the retired rule that forbade firing while it was
    open would otherwise silence the final header.
    """
    text = _read("review", "SKILL.md")
    assert "keep it `in_progress` through the question" in text
    assert "must not fire while this task is `pending`/`in_progress`" not in text


def test_worktree_removal_shows_the_exact_list_before_asking() -> None:
    """Cleanup retains explicit consent with a visible list and error, rather than identical previews.

    Leftover worktrees are listed in a file and printed once before the question, so the user approves a concrete list;
    the removal never runs without that answer.
    """
    text = _read("_shared", "worktree-isolation.md")
    missing = _missing(
        text,
        "Write the full list and the `add` error to `.temp/worktree-leftovers.md` first",
        "print them once as the last reply text before",
        "question text",
        "no `preview`",
        "never run the `--apply` form without that answer",
        "retry the create once",
    )
    assert missing == []
    assert _present(text, "`preview` of both options") == []
