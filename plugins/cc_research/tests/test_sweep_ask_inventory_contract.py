"""Pin every user ask the sweep pipeline had before its efficiency pass, so no question is silently removed.

Sweep is non-interactive by design, so it asks only at recovery points. Each row is one of those decisions in the
committed baseline flow; cutting bookkeeping turns must never delete one, and removing a question deletes its marker and
fails here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_SKILLS = Path(__file__).resolve().parents[1] / "skills"
_SWEEP = _SKILLS / "sweep"
_SHARED = _SKILLS / "_shared"

#: (section start, section end, marker that only exists while the question is still asked there), all in SKILL.md
_TOOL_ASKS = [
    pytest.param("### Step S1", "### Step S2", "Overwrite and re-sweep", id="existing-program-overwrite"),
    pytest.param(
        "### Step S3", "### Step S4", "(a) `proceed to run anyway` · (b) `abort`", id="malformed-judge-report"
    ),
    pytest.param("### Step S4", "### Step S5", "Unresolved — how to proceed?", id="unresolved-judge"),
]


def _section(start_marker: str, end_marker: str) -> str:
    """Return the text between two unique markers of the sweep skill."""
    text = (_SWEEP / "SKILL.md").read_text(encoding="utf-8")
    start = text.index(start_marker)
    return text[start : text.index(end_marker, start)]


@pytest.mark.parametrize(("section_start", "section_end", "marker"), _TOOL_ASKS)
def test_baseline_tool_ask_still_happens(section_start: str, section_end: str, marker: str) -> None:
    """Every decision asked through the AskUserQuestion tool at baseline is still asked in its section."""
    section = _section(section_start, section_end)
    assert marker in section
    assert "AskUserQuestion" in section


def test_unsupported_flag_protocol_still_asks() -> None:
    """The unknown-flag question is delegated to the shared protocol, which must still ask."""
    skill = (_SWEEP / "SKILL.md").read_text(encoding="utf-8")
    assert 'cat "$_RESEARCH_SHARED/unsupported-flag-protocol.md"' in skill
    assert "AskUserQuestion" in (_SHARED / "unsupported-flag-protocol.md").read_text(encoding="utf-8")


def test_team_confirmation_gate_still_pauses_sweep() -> None:
    """With --team, the run step's own confirmation gate still pauses sweep instead of being bypassed."""
    skill = (_SWEEP / "SKILL.md").read_text(encoding="utf-8")
    assert "Gate cannot be bypassed from sweep context; sweep pauses and waits." in skill
