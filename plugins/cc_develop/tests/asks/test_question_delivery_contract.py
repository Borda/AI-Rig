"""Keep decision content visible without putting plans or reports in option previews.

Each test reads one shipped skill or shared doc line by line and checks that a question delivers its evidence as saved
files plus one visible reply copy, with only a short summary inside the question text.
"""

from pathlib import Path

import pytest

_SKILLS = Path(__file__).resolve().parents[2] / "skills"


def _read(relative: str) -> str:
    """Return the text of one skill file or shared doc under ``skills/``."""
    return (_SKILLS / relative).read_text(encoding="utf-8")


def _line_starting(text: str, prefix: str) -> str:
    """Return the first line of ``text`` that starts with ``prefix``."""
    return next(line for line in text.splitlines() if line.startswith(prefix))


@pytest.mark.parametrize("relative", ["debug/SKILL.md", "feature/SKILL.md", "fix/SKILL.md", "refactor/SKILL.md"])
def test_challenger_blockers_are_saved_and_shown_before_the_question(relative: str) -> None:
    """Every blocker gate shows its evidence once and keeps the decision summary in the question.

    The gate line is the single line naming both the blocker count and the question tool. It must save the findings,
    print them once ahead of the question, cap the question text, and forbid previews, so a host that hides pre-call
    reply text still leaves the evidence on disk.
    """
    gate = next(line for line in _read(relative).splitlines() if "Blockers found" in line and "AskUserQuestion" in line)
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


@pytest.mark.parametrize(
    ("skill", "next_gate"),
    [
        pytest.param("debug", "**Scope gate**", id="debug"),
        pytest.param("feature", "**Complexity classification**", id="feature"),
    ],
)
def test_analysis_is_saved_and_shown_once_at_the_next_gate(skill: str, next_gate: str) -> None:
    """Analysis remains visible before choosing even when the host hides pre-call reply text.

    The ``Carry the ...`` paragraph is the delivery instruction; it must precede the skill's next gate so the analysis
    is already on screen when that gate asks.
    """
    text = _read(f"{skill}/SKILL.md")
    delivery = _line_starting(text, "Carry the ")
    assert "Write the full analysis" in delivery
    assert "print the analysis summary once as the last reply text before" in delivery
    assert "question text" in delivery
    assert "counts, headline" in delivery
    assert "target 600 characters, hard cap 1,500" in delivery
    assert "no `preview`" in delivery
    assert "no table, pipe layout or code fence" in delivery
    assert "`preview` of every option" not in delivery
    assert text.index(delivery) < text.index(next_gate)


def test_feature_team_uses_the_same_challenger_blocker_gate() -> None:
    """Team mode retains the solo gate's evidence delivery and authorized-resolution rules.

    Team mode must point at the solo blocker gate instead of restating it, so the two cannot drift apart.
    """
    team = _read("feature/modes/team-mode.md")
    assert "feature/SKILL.md §Challenger gate" in team
    assert "same blocker resolution, protected-decision and authorized plan hash refresh" in team


def test_inline_plan_reuses_authorization_and_keeps_protected_decisions() -> None:
    """Ordinary plans proceed while explicit waits and protected decisions remain gated.

    The inline plan doc must not add a routine approval question, yet still stop for a plan-only request or a protected
    architecture, security or data decision.
    """
    text = _read("_shared/plan-inline.md")
    assert "Reuse the user's existing authorization" in text
    assert "do not ask for routine plan approval" in text
    assert "plan-only, wait, or explicit plan approval" in text
    assert "genuinely missing protected decision or authority" in text
    assert "architecture, security, data" in text
    assert "plan_hash.py" in text
    assert "(a) **Proceed**" not in text


def test_worktree_removal_shows_the_exact_list_before_asking() -> None:
    """Removal requires the saved leftovers and error to be visible before the existing explicit answer.

    The worktree isolation doc must save the leftover list and the ``add`` error, show them once, and never run the
    ``--apply`` form without the user's answer.
    """
    text = _read("_shared/worktree-isolation.md")
    assert "Write the full list and the `add` error to `.temp/worktree-leftovers.md` first" in text
    assert "print them once as the last reply text before" in text
    assert "question text" in text
    assert "no `preview`" in text
    assert "never run the `--apply` form without that answer" in text
    assert "retry the create once" in text
    assert "`preview` of both options" not in text


def test_plan_handoff_shows_the_saved_brief_before_the_question() -> None:
    """The plan brief is delivered before the choice while the implementation handoff waits for Proceed.

    The plan skill must print its saved brief and unresolved items once, cap the question text, and hold the handoff
    line back until the user selects the proceed option.
    """
    text = _read("plan/SKILL.md")
    assert "print the saved brief and any unresolved items once as the last reply text before" in text
    assert "counts, headline" in text
    assert "no `preview`" in text
    assert "Don't print handoff line until user selects (a)" in text
    assert "hard cap 1,500" in text


def test_hypothesis_gate_saves_evidence_and_preserves_confirmation() -> None:
    """Root-cause confirmation retains its evidence file and one visible copy before a tool-based choice.

    The debug skill's hypothesis gate must wait for confirmation before Step 4, save the hypothesis file, and present
    the hypothesis only at that gate, after premise grounding.
    """
    text = _read("debug/SKILL.md")
    gate = _line_starting(text, "**Gate**: present hypothesis")
    assert "wait for confirmation or challenge before proceeding to Step 4" in gate
    assert ".temp/debug/<slug>-hypothesis.md" in gate
    assert "print it once as the last reply text before" in gate
    assert "question text" in gate
    assert "no `preview`" in gate
    assert "present it only at the gate below, after premise grounding" in text


def test_review_summary_precedes_answer_and_full_header_follows_it() -> None:
    """The first review question supplies the headline; the full header stays tracked until final delivery.

    The review skill must not print the header table before the follow-up question, must keep the header task open
    through the question, and must not claim the question summary satisfies final delivery.
    """
    text = _read("review/SKILL.md")
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
