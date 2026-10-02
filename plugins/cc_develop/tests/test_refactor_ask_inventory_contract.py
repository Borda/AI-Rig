"""Pin every user ask the refactor flow had before its efficiency pass, so no question is silently removed.

Each row is one decision the user made in the committed baseline flow, mapped to where that same decision is asked now.
Batching tool calls, cutting bookkeeping turns and narrowing test runs must never delete a question: removing one, or
replacing it with a silent default, deletes its marker and fails here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_SKILLS = Path(__file__).resolve().parents[1] / "skills"
_REFACTOR = _SKILLS / "refactor"
_SHARED = _SKILLS / "_shared"

#: (file, section start, section end, marker that only exists while the question is still asked there)
_TOOL_ASKS = [
    pytest.param(
        "SKILL.md",
        "**Unsupported flag check**",
        "## Worktree isolation",
        "**Continue ignoring**",
        id="unsupported-flag",
    ),
    pytest.param(
        "SKILL.md",
        "**Goal classification gate**",
        "## Challenger gate",
        "Goal mixes refactoring and feature work",
        id="goal-classification",
    ),
    pytest.param("SKILL.md", "**Scope gate**", "## Challenger gate", '"Narrow scope (Recommended)"', id="scope-gate"),
    pytest.param(
        "SKILL.md",
        "## Challenger gate",
        "## Step 2",
        "Challenger raised N blocker(s) on the refactoring approach",
        id="challenger-blockers",
    ),
    pytest.param("SKILL.md", "## Step 3", "## Step 4", "Characterization test gate failed", id="characterization-gate"),
    pytest.param("modes/team-mode.md", "**Gate T1", "**Step T2", "proceed without safety net", id="team-gate-t1"),
]

#: Asks the baseline phrased in prose, not as a tool call; kept in that form, never upgraded or dropped.
_PROSE_ASKS = [
    pytest.param(
        "SKILL.md", "**Checkpoint init**", "## Flag parsing", "offer resume from last completed step", id="resume-offer"
    ),
]

#: Shared docs whose own questions this skill reaches only by loading them.
_SHARED_ASKS = [
    pytest.param("codemap-gates.md", "Gate A always asks (`AskUserQuestion`)", id="codemap-gate-a"),
    pytest.param("premise-grounding.md", "has no verified source", id="premise-grounding"),
    pytest.param("plan-inline.md", "(a) **Proceed**", id="plan-inline"),
    pytest.param("foundry--quality-stack.md", "AskUserQuestion", id="quality-stack"),
]


def _section(relative: str, start_marker: str, end_marker: str) -> str:
    """Return the text between two unique markers of one refactor skill file."""
    text = (_REFACTOR / relative).read_text(encoding="utf-8")
    start = text.index(start_marker)
    return text[start : text.index(end_marker, start)]


@pytest.mark.parametrize(("relative", "section_start", "section_end", "marker"), _TOOL_ASKS)
def test_baseline_tool_ask_still_happens(relative: str, section_start: str, section_end: str, marker: str) -> None:
    """Every decision asked through the AskUserQuestion tool at baseline is still asked in its section.

    The marker is the question's own text, and the section must name the tool, so a prose mention cannot stand in for a
    real ask after a section is rewritten for fewer turns.
    """
    section = _section(relative, section_start, section_end)
    assert marker in section
    assert "AskUserQuestion" in section


@pytest.mark.parametrize(("relative", "section_start", "section_end", "marker"), _PROSE_ASKS)
def test_baseline_prose_ask_still_happens(relative: str, section_start: str, section_end: str, marker: str) -> None:
    """A question the baseline put to the user in prose is still put there."""
    assert marker in _section(relative, section_start, section_end)


@pytest.mark.parametrize(("doc", "marker"), _SHARED_ASKS)
def test_shared_doc_ask_is_still_loaded(doc: str, marker: str) -> None:
    """The skill still loads each shared doc that carries a question, and the doc still asks it."""
    skill = (_REFACTOR / "SKILL.md").read_text(encoding="utf-8")
    assert f'cat "$_DEV_SHARED/{doc}"' in skill
    assert marker in (_SHARED / doc).read_text(encoding="utf-8")
