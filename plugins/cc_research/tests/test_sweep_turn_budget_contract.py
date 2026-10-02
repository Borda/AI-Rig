"""Guard the sweep turn budget: two run-level tasks whose updates ride with real calls, never a per-step task set.

Measured sweeps spent up to 5 of ~15 turns on task calls alone — five step-tasks created up front, then a ``deleted``
call for every skipped step. These tests pin the instruction that replaced that shape.
"""

from __future__ import annotations

from pathlib import Path

_SWEEP = Path(__file__).resolve().parents[1] / "skills" / "sweep" / "SKILL.md"


def _tracking() -> str:
    """Return sweep's task-tracking block."""
    text = _SWEEP.read_text(encoding="utf-8")
    start = text.index("**Task tracking**")
    return text[start : text.index("### Step S1", start)]


def test_stage_tasks_kept_and_ride_with_real_calls() -> None:
    """S1–S5 stage tasks stay visible; they are created with the first real call and every update rides along."""
    block = _tracking()
    assert "Create tasks for S1–S5 at start, all in the same response as the first S1 call." in block
    assert "Every later `TaskUpdate` rides with the next real tool call." in block
    assert "The only standalone call is the final `completed` right before the R6 summary." in block


def test_s1_batching_waits_for_the_overwrite_guard() -> None:
    """S1 blocks batch only after the existing-program.md guard is answered, so that question is never moved later."""
    assert "after the existing-program.md guard is answered" in _SWEEP.read_text(encoding="utf-8")


def test_early_stop_closes_stages_in_one_response() -> None:
    """A stop before S5 closes and deletes the remaining stage tasks in the stop response, not one turn per task."""
    assert "one response, not one per task" in _tracking()
