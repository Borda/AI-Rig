"""Pin everything the user or tooling could see at HEAD in files the turn-budget pass touched — no observability
regression.

data/observability_head_markers.json holds, per touched file, every HEAD line that produces visible output: ⏱ markers,
printed progress and report lines, report headers, checkpoint and plan-file writes, task updates, run-dir artifacts.
Each marker is the line's first ~100 characters, cut at a word boundary, still present verbatim. Lines the pass rewrote
are pinned separately by their visible equivalent. Removing an output deletes its marker and fails here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_PLUGIN = Path(__file__).resolve().parents[1]
_MARKERS = json.loads((Path(__file__).parent / "data" / "observability_head_markers.json").read_text(encoding="utf-8"))
_ROWS = [
    pytest.param(
        relative, marker, id=f"{relative.split('/')[-2] if relative.endswith('SKILL.md') else relative}-{index}"
    )
    for relative, markers in sorted(_MARKERS.items())
    for index, marker in enumerate(markers, start=1)
]


@pytest.mark.parametrize(("relative", "marker"), _ROWS)
def test_head_visible_output_still_present(relative: str, marker: str) -> None:
    """Every output line the file showed at HEAD is still there verbatim."""
    assert marker in (_PLUGIN / relative).read_text(encoding="utf-8")


def test_sweep_stage_tasks_are_kept() -> None:
    """Sweep keeps its S1–S5 stage tasks, the progress the user saw at HEAD."""
    sweep = (_PLUGIN / "skills" / "sweep" / "SKILL.md").read_text(encoding="utf-8")
    assert "Create tasks for S1–S5 at start" in sweep


@pytest.mark.parametrize("relative", ["skills/topic/SKILL.md", "skills/topic/modes/plan.md"])
def test_topic_surfaces_timed_out_agents(relative: str) -> None:
    """Research:topic now shows a timed-out spawn in its report instead of dropping it silently."""
    assert "⏱ `timed_out` at once; surface it in the report" in (_PLUGIN / relative).read_text(encoding="utf-8")
