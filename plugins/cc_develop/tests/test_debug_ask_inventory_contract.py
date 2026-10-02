"""Pin every user ask the debug flow had before its efficiency pass, so no question is silently removed.

Each row is one decision the user made in the committed baseline flow, mapped to where that same decision is asked now.
Batching tool calls, cutting bookkeeping turns and changing how agent waits are handled must never delete a question:
removing one, or replacing it with a silent default, deletes its marker and fails here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_SKILLS = Path(__file__).resolve().parents[1] / "skills"
_DEBUG = _SKILLS / "debug"
_SHARED = _SKILLS / "_shared"

#: (section start, section end, marker that only exists while the question is still asked there), all in SKILL.md
_TOOL_ASKS = [
    pytest.param(
        "**Language preflight gate**", "**Checkpoint**", "Non-Python project detected", id="language-preflight"
    ),
    pytest.param("**Unsupported flag check**", "**Mode selection**", "**Continue ignoring**", id="unsupported-flag"),
    pytest.param(
        "**If `TEAM_MODE=true`**",
        "## Step 1: Understand the symptom",
        "invoke `AskUserQuestion` presenting top 2 competing hypotheses",
        id="team-tie-break",
    ),
    pytest.param(
        "**If `TEAM_MODE=true`**",
        "## Step 1: Understand the symptom",
        "Only invoke `AskUserQuestion` at Step 3 if competing hypotheses remain",
        id="team-step-3-gate",
    ),
    pytest.param("**Scope gate**", "**Flaky-test branch**", '"Narrow scope (Recommended)"', id="scope-gate"),
    pytest.param(
        "## Challenger gate",
        "## Step 3",
        "Challenger raised N blocker(s) on the candidate root cause",
        id="challenger-blockers",
    ),
]

#: Asks the baseline phrased in prose, not as a tool call; kept in that form, never upgraded or dropped.
_PROSE_ASKS = [
    pytest.param(
        "## Step 3: Hypothesis and gate",
        "## Step 4",
        "present hypothesis to user, wait for confirmation or challenge",
        id="hypothesis-gate",
    ),
]

#: Shared docs whose own questions this skill reaches only by loading them.
_SHARED_ASKS = [
    pytest.param("codemap-gates.md", "Gate A always asks (`AskUserQuestion`)", id="codemap-gate-a"),
    pytest.param("premise-grounding.md", "has no verified source", id="premise-grounding"),
]


def _section(start_marker: str, end_marker: str) -> str:
    """Return the text between two unique markers of the debug skill."""
    text = (_DEBUG / "SKILL.md").read_text(encoding="utf-8")
    start = text.index(start_marker)
    return text[start : text.index(end_marker, start)]


@pytest.mark.parametrize(("section_start", "section_end", "marker"), _TOOL_ASKS)
def test_baseline_tool_ask_still_happens(section_start: str, section_end: str, marker: str) -> None:
    """Every decision asked through the AskUserQuestion tool at baseline is still asked in its section.

    The marker is the question's own text, and the section must name the tool, so a prose mention cannot stand in for a
    real ask after a section is rewritten for fewer turns.
    """
    section = _section(section_start, section_end)
    assert marker in section
    assert "AskUserQuestion" in section


@pytest.mark.parametrize(("section_start", "section_end", "marker"), _PROSE_ASKS)
def test_baseline_prose_ask_still_happens(section_start: str, section_end: str, marker: str) -> None:
    """A question the baseline put to the user in prose is still put there."""
    assert marker in _section(section_start, section_end)


@pytest.mark.parametrize(("doc", "marker"), _SHARED_ASKS)
def test_shared_doc_ask_is_still_loaded(doc: str, marker: str) -> None:
    """The skill still loads each shared doc that carries a question, and the doc still asks it."""
    skill = (_DEBUG / "SKILL.md").read_text(encoding="utf-8")
    assert f'cat "$_DEV_SHARED/{doc}"' in skill
    assert marker in (_SHARED / doc).read_text(encoding="utf-8")
