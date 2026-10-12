"""Keep decision content visible without putting plans or reports in option previews."""

from pathlib import Path

import pytest

_SKILLS = Path(__file__).resolve().parents[1] / "skills"


@pytest.mark.parametrize(
    "relative",
    [
        "debug/SKILL.md",
        "feature/SKILL.md",
        "feature/modes/team-mode.md",
        "fix/SKILL.md",
        "refactor/SKILL.md",
    ],
)
def test_challenger_blockers_are_saved_and_shown_before_the_question(relative: str) -> None:
    """Every blocker gate shows its evidence once and keeps the decision summary in the question."""
    text = (_SKILLS / relative).read_text(encoding="utf-8")
    gate = next(line for line in text.splitlines() if "Blockers found" in line and "AskUserQuestion" in line)
    assert "Write the blocker findings" in gate
    assert "print them once as the last reply text before" in gate
    assert "question text" in gate
    assert "blocker count, headline" in gate
    assert "target 600 characters, hard cap 1,500" in gate
    assert "one-line description" in gate
    assert "no `preview`" in gate
    assert "no table, pipe layout or code fence" in gate
    assert "`preview` of every option" not in gate
    assert "On Abort: stop" in gate


@pytest.mark.parametrize("skill", ["debug", "feature"])
def test_analysis_is_saved_and_shown_once_at_the_next_gate(skill: str) -> None:
    """Analysis remains visible before choosing even when the host hides pre-call reply text."""
    text = (_SKILLS / skill / "SKILL.md").read_text(encoding="utf-8")
    delivery = next(line for line in text.splitlines() if line.startswith("Carry the "))
    assert "Write the full analysis" in delivery
    assert "print the analysis summary once as the last reply text before" in delivery
    assert "question text" in delivery
    assert "counts, headline" in delivery
    assert "target 600 characters, hard cap 1,500" in delivery
    assert "no `preview`" in delivery
    assert "no table, pipe layout or code fence" in delivery
    assert "`preview` of every option" not in delivery
    next_gate = "**Scope gate**" if skill == "debug" else "**Complexity classification**"
    assert text.index(delivery) < text.index(next_gate)


def test_inline_plan_is_printed_once_and_the_question_carries_its_summary() -> None:
    """The saved plan is shown before Proceed, Stop or Abort without identical option previews."""
    text = (_SKILLS / "_shared" / "plan-inline.md").read_text(encoding="utf-8")
    assert "print the saved plan once as the last reply text before" in text
    assert "counts, headline" in text
    assert "target 600 characters, hard cap 1,500" in text
    assert "one-line description" in text
    assert "no `preview`" in text
    assert "On (b) or (c): stop" in text
    assert "`preview` of every option" not in text


def test_worktree_removal_shows_the_exact_list_before_asking() -> None:
    """Removal requires the saved leftovers and error to be visible before the existing explicit answer."""
    text = (_SKILLS / "_shared" / "worktree-isolation.md").read_text(encoding="utf-8")
    assert "Write the full list and the `add` error to `.temp/worktree-leftovers.md` first" in text
    assert "print them once as the last reply text before" in text
    assert "question text" in text
    assert "no `preview`" in text
    assert "never run the `--apply` form without that answer" in text
    assert "retry the create once" in text
    assert "`preview` of both options" not in text


def test_plan_handoff_shows_the_saved_brief_before_the_question() -> None:
    """The plan brief is delivered before the choice while the implementation handoff waits for Proceed."""
    text = (_SKILLS / "plan" / "SKILL.md").read_text(encoding="utf-8")
    assert "print the saved brief and any unresolved items once as the last reply text before" in text
    assert "counts, headline" in text
    assert "no `preview`" in text
    assert "Don't print handoff line until user selects (a)" in text
    assert "hard cap 1,500" in text


def test_hypothesis_gate_saves_evidence_and_preserves_confirmation() -> None:
    """Root-cause confirmation retains its evidence file and one visible copy before a tool-based choice."""
    text = (_SKILLS / "debug" / "SKILL.md").read_text(encoding="utf-8")
    gate = next(line for line in text.splitlines() if line.startswith("**Gate**: present hypothesis"))
    assert "wait for confirmation or challenge before proceeding to Step 4" in gate
    assert ".temp/debug/<slug>-hypothesis.md" in gate
    assert "print it once as the last reply text before" in gate
    assert "question text" in gate
    assert "no `preview`" in gate
    assert "present it only at the gate below, after premise grounding" in text


def test_review_summary_precedes_answer_and_full_header_follows_it() -> None:
    """The first review question supplies the headline; the full header stays tracked until final delivery."""
    text = (_SKILLS / "review" / "SKILL.md").read_text(encoding="utf-8")
    assert "first question's text" in text
    assert "`Title`, `Outcome`" in text
    assert "after the answer" in text
    assert "after its last tool call" in text
    assert "Long wait? `/compact` now" in text
    assert "no `preview`" in text
    assert "keep it `in_progress` through the question" in text
    assert "Do not print the header table before the follow-up question" in text
    assert "question summary does not satisfy final delivery" in text
    assert "option previews carry" not in text
    assert "preview cap" not in text
