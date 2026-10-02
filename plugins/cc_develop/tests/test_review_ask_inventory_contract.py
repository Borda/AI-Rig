"""Pin every user ask the develop review flow had before its efficiency pass, so no question is silently removed.

Each row is one decision the user made in the committed baseline flow, mapped to where that same decision is asked now.
Tightening how agent waits are handled must never delete a question: removing one, or replacing it with a silent
default, deletes its marker and fails here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_SKILLS = Path(__file__).resolve().parents[1] / "skills"
_REVIEW = _SKILLS / "review"
_SHARED = _SKILLS / "_shared"

#: (section start, section end, marker that only exists while the question is still asked there), all in SKILL.md
_TOOL_ASKS = [
    pytest.param(
        "`$OSS_AVAILABLE` is `true`",
        "</inputs>",
        "Did you mean to run `/oss:review $ARGUMENTS`",
        id="pr-number-with-oss",
    ),
    pytest.param("`$OSS_AVAILABLE` is `false`", "</inputs>", "Review local code instead?", id="pr-number-without-oss"),
    pytest.param("**Unsupported flag check**", "## Worktree isolation", "**Continue ignoring**", id="unsupported-flag"),
    pytest.param("**Follow-up gate (NEVER SKIP)**", "</workflow>", "`walk through findings`", id="follow-up-gate"),
]


def _section(start_marker: str, end_marker: str) -> str:
    """Return the text between two unique markers of the review skill."""
    text = (_REVIEW / "SKILL.md").read_text(encoding="utf-8")
    start = text.index(start_marker)
    return text[start : text.index(end_marker, start)]


@pytest.mark.parametrize(("section_start", "section_end", "marker"), _TOOL_ASKS)
def test_baseline_tool_ask_still_happens(section_start: str, section_end: str, marker: str) -> None:
    """Every decision asked through the AskUserQuestion tool at baseline is still asked in its section.

    The marker is the question's own text, and the section must name the tool, so a prose mention cannot stand in for a
    real ask.
    """
    section = _section(section_start, section_end)
    assert marker in section
    assert "AskUserQuestion" in section


def test_codemap_gate_still_asks() -> None:
    """The codemap index gate is delegated to the shared contract, which must still ask before building."""
    skill = (_REVIEW / "SKILL.md").read_text(encoding="utf-8")
    assert 'cat "$_DEV_SHARED/codemap-gates.md"' in skill
    assert "Gate A always asks (`AskUserQuestion`)" in (_SHARED / "codemap-gates.md").read_text(encoding="utf-8")
