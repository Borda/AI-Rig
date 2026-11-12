"""Pin every user ask the fix flow had before its efficiency pass, so no question is silently removed.

Each row is one decision the user made in the committed baseline flow, mapped to where that same decision is asked now.
Batching tool calls, cutting bookkeeping turns and narrowing test runs must never delete a question: removing one, or
replacing it with a silent default, deletes its marker and fails here.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

import pytest
from _develop_text import between, flat

_SKILLS = Path(__file__).resolve().parents[2] / "skills"
_FIX = _SKILLS / "fix"
_SHARED = _SKILLS / "_shared"

#: (section start, section end, marker that only exists while the question is still asked there), all in SKILL.md
_TOOL_ASKS = [
    pytest.param("**Unsupported flag check**", "**Preflight**", "**Continue ignoring**", id="unsupported-flag"),
    pytest.param(
        "**Cannot-reproduce gate**",
        "## Challenger gate",
        "Cannot confirm root cause from available information",
        id="cannot-reproduce",
    ),
    pytest.param("**Scope gate**", "**Non-Python surface check**", '"Narrow scope (Recommended)"', id="scope-gate"),
    pytest.param(
        "**Non-Python surface check**",
        "**Complexity classification**",
        "No Python source in the identified change surface",
        id="non-python-surface",
    ),
    pytest.param(
        "## Challenger gate",
        "## Step 2",
        "Challenger raised N blocker(s) on the fix approach",
        id="challenger-blockers",
    ),
    pytest.param(
        "**Breaking change gate**",
        "Make minimal change",
        "call `AskUserQuestion` before any edit",
        id="breaking-change",
    ),
]

#: Asks the baseline phrased in prose, not as a tool call; kept in that form, never upgraded or dropped.
_PROSE_ASKS = [
    pytest.param(
        "ASSUMPTIONS I'M MAKING", "## Challenger gate", "Correct me now or I'll proceed with these", id="assumptions"
    ),
]


#: Shared docs whose own questions this skill reaches only by loading them.
_SHARED_ASKS = [
    pytest.param("runner-detection.md", "Non-Python project detected", id="language-preflight"),
    pytest.param(
        "codemap-gates.md", "`LAZY_CODEMAP` is the explicit confirm-first preference", id="codemap-gate-a-lazy-opt-in"
    ),
    pytest.param("premise-grounding.md", "has no verified source", id="premise-grounding"),
    pytest.param("foundry--quality-stack.md", "AskUserQuestion", id="quality-stack"),
]


@cache
def _flat_text(path: Path) -> str:
    """Return one file with whitespace collapsed, so a marker matches wherever the source wraps its prose."""
    return flat(path.read_text(encoding="utf-8"))


def _section(start_marker: str, end_marker: str) -> str:
    """Return the text between two unique markers of the fix skill."""
    return between(_flat_text(_FIX / "SKILL.md"), start_marker, end_marker)


def test_checkpoint_steps_do_not_authorize_cross_session_gate_skips() -> None:
    """A prose checkpoint from any skill cannot substitute for fix's session-scoped reproduction evidence."""
    lines = (_FIX / "SKILL.md").read_text(encoding="utf-8").splitlines()
    checkpoint = next(line for line in lines if line.startswith("**Checkpoint init**"))
    assert "resume" not in checkpoint
    assert "step: N — completed" in checkpoint


@pytest.mark.parametrize(("section_start", "section_end", "marker"), _TOOL_ASKS)
def test_baseline_tool_ask_still_happens(section_start: str, section_end: str, marker: str) -> None:
    """Every decision asked through the AskUserQuestion tool at baseline is still asked in its section.

    The marker is the question's own text, and the section must name the tool, so a prose mention cannot stand in for a
    real ask after a section is rewritten for fewer turns.
    """
    section = _section(section_start, section_end)
    assert (marker in section, "AskUserQuestion" in section) == (True, True)


@pytest.mark.parametrize(("section_start", "section_end", "marker"), _PROSE_ASKS)
def test_baseline_prose_ask_still_happens(section_start: str, section_end: str, marker: str) -> None:
    """A question the baseline put to the user in prose is still put there.

    These two were never tool calls; keeping them as prose (neither upgraded nor dropped) preserves the baseline flow.
    """
    assert marker in _section(section_start, section_end)


@pytest.mark.parametrize(("doc", "marker"), _SHARED_ASKS)
def test_shared_doc_ask_is_still_loaded(doc: str, marker: str) -> None:
    """The skill still loads each shared doc that carries a question, and the doc still carries it.

    The fix skill reaches these questions only by loading the doc, so a dropped load line removes the ask silently.
    """
    loads = any(
        'bin/dev_load_shared.py"' in line and doc in line.split()
        for line in (_FIX / "SKILL.md").read_text(encoding="utf-8").splitlines()
    )
    assert (loads, marker in _flat_text(_SHARED / doc)) == (True, True)


@pytest.mark.parametrize("phrase", ["Gate A prepares automatically by default", "SCAN_NO_AUTOBUILD=1"])
def test_codemap_gate_keeps_its_prompt_policy(phrase: str) -> None:
    """Codemap's opt-in prompt policy still states the automatic default and its opt-out variable.

    The confirm-first marker above is only half of the policy; without the default and opt-out lines the prompt would
    read as unconditional.
    """
    assert phrase in _flat_text(_SHARED / "codemap-gates.md")


def test_language_preflight_gate_still_applied() -> None:
    """Fix still applies the runner-detection language gate that asks before running on a non-Python repo."""
    skill = _flat_text(_FIX / "SKILL.md")
    assert "apply §Language preflight gate from `runner-detection.md`" in skill
