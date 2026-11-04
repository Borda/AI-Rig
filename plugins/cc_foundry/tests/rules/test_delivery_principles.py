"""Hold the always-loaded Claude instruction file to the four delivery principles.

``CLAUDE.src.md`` is copied to ``~/.claude/CLAUDE.md`` by ``/foundry:setup``, so every line of it is re-read on every
turn of every session. The four delivery principles state how every change is made, and they are the wording the Codex
global template and the OpenCode package copy verbatim, so a divergence here means the hosts no longer say the same
thing. The canonical text lives in the Codex Rig plugin and is pinned byte-identical by the repository's policy-parity
hook; this test keeps the Claude copy readable on its own.
"""

from __future__ import annotations

from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
CLAUDE_SRC = PLUGIN_ROOT / "CLAUDE.src.md"
CORE_PRINCIPLES_HEADING = "## Core Principles"


def _text(path: Path) -> str:
    """Return one shipped Markdown file as text."""
    return path.read_text(encoding="utf-8")


def _core_principles(text: str) -> str:
    """Return the body of the ``## Core Principles`` section, read line by line so prose reflow cannot matter."""
    lines = text.splitlines()
    assert CORE_PRINCIPLES_HEADING in lines, f"{CLAUDE_SRC.name} has no {CORE_PRINCIPLES_HEADING} section"
    body = []
    for line in lines[lines.index(CORE_PRINCIPLES_HEADING) + 1 :]:
        if line.startswith("## "):
            break
        body.append(line)
    return "\n".join(body)


@pytest.mark.parametrize(
    "principle",
    [
        pytest.param("- **Consistent, simple delivery.**", id="consistent-simple-delivery"),
        pytest.param("- **Root cause, not symptom.**", id="root-cause-not-symptom"),
        pytest.param("- **Automatic correction and recovery.**", id="automatic-correction-and-recovery"),
        pytest.param("- **Honest stops, real handoffs.**", id="honest-stops-real-handoffs"),
    ],
)
def test_core_principles_states_each_delivery_principle(principle: str) -> None:
    """The always-loaded file states each delivery principle, so a session without a skill still applies it.

    Args:
        principle: The bullet stating one delivery principle.
    """
    assert principle in _core_principles(_text(CLAUDE_SRC)), f"{CLAUDE_SRC.name} does not state {principle}"


def test_core_principles_states_the_five_principles_as_one_block() -> None:
    """The five principles stay one contiguous block, matching the single statement the other hosts ship."""
    body = _core_principles(_text(CLAUDE_SRC))
    labels = [line.split("**")[1] for line in body.splitlines() if line.startswith("- **")]

    assert "Consistent, simple delivery." in labels, (
        "the delivery principles are not stated in the Core Principles section"
    )
    start = labels.index("Consistent, simple delivery.")
    assert labels[start : start + 5] == [
        "Consistent, simple delivery.",
        "Root cause, not symptom.",
        "Automatic correction and recovery.",
        "Honest stops, real handoffs.",
        "Never revert another owner's work.",
    ], "the five delivery principles are no longer five consecutive bullets"
