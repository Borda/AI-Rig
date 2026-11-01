"""Guard the sweep turn budget: two run-level tasks whose updates ride with real calls, never a per-step task set.

Measured sweeps spent up to 5 of ~15 turns on task calls alone — five step-tasks created up front, then a ``deleted``
call for every skipped step. These tests pin the instruction that replaced that shape. Sentences are matched after
whitespace is normalised, so reflowing sweep's prose cannot break them.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _research_text import flat

_SWEEP = Path(__file__).resolve().parents[2] / "skills" / "sweep" / "SKILL.md"


def _sweep_text() -> str:
    """Return sweep's SKILL.md with every whitespace run collapsed to one space."""
    return flat(_SWEEP.read_text(encoding="utf-8"))


def _tracking() -> str:
    """Return sweep's task-tracking block, from its marker up to the S1 step heading."""
    text = _sweep_text()
    start = text.index("**Task tracking**")
    return text[start : text.index("### Step S1", start)]


class TestTaskTracking:
    """Sweep's task-tracking block keeps the turn budget."""

    @pytest.mark.parametrize(
        "sentence",
        [
            pytest.param(
                "Create tasks for S1–S5 at start, all in the same response as the first S1 call.",
                id="created-with-first-call",
            ),
            pytest.param("Every later `TaskUpdate` rides with the next real tool call.", id="updates-ride-along"),
            pytest.param(
                "The only standalone call is the final `completed` right before the R6 summary.",
                id="one-standalone-call",
            ),
            pytest.param("one response, not one per task", id="early-stop-closes-in-one-response"),
        ],
    )
    def test_block_states(self, sentence: str) -> None:
        """The tracking block keeps the sentence that holds sweep to run-level, piggybacked task calls.

        The S1-S5 stage tasks stay visible, but are created with the first real call and updated alongside real calls. A
        stop before S5 closes and deletes the remaining stage tasks in one response, not one turn per task. Losing any
        sentence brings back the turns spent on task bookkeeping alone.
        """
        assert sentence in _tracking()

    def test_s1_batching_waits_for_overwrite_guard(self) -> None:
        """S1 blocks batch only after the existing-program.md guard is answered, so that question is never moved later.

        The overwrite guard is a user question; batching S1 calls ahead of it would run work before the user decides
        whether an existing program.md may be replaced.
        """
        assert "after the existing-program.md guard is answered" in _sweep_text()
