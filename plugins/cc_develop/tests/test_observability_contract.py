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


@pytest.mark.parametrize(
    ("relative", "marker"),
    [
        pytest.param("skills/feature/SKILL.md", "baseline proven at cycle 0", id="feature-baseline-skip-line"),
        pytest.param(
            "skills/feature/SKILL.md", "⚠ not_covered — some changed code has no mapped test", id="feature-not-covered"
        ),
        pytest.param(
            "skills/fix/SKILL.md", "⚠ not_covered — some changed code has no mapped test", id="fix-not-covered"
        ),
        pytest.param(
            "skills/refactor/SKILL.md",
            "⚠ not_covered — some changed code has no mapped test",
            id="refactor-not-covered",
        ),
        pytest.param("skills/fix/SKILL.md", "regression test is sole verification", id="fix-no-suite-note"),
        pytest.param(
            "skills/refactor/SKILL.md", "note the message above in Final Report", id="refactor-stack-skipped-note"
        ),
        pytest.param(
            "skills/review/SKILL.md",
            "Mark agents that returned empty or error with ⏱ in final report",
            id="review-timeout-marker",
        ),
        pytest.param("skills/fix/modes/team-mode.md", "surface with ⏱; never omit", id="fix-team-timeout-marker"),
    ],
)
def test_replaced_output_has_a_visible_equivalent(relative: str, marker: str) -> None:
    """Every HEAD output this pass rewrote still has a visible equivalent — same signal, new wording."""
    assert marker in (_PLUGIN / relative).read_text(encoding="utf-8")


@pytest.mark.parametrize("skill", ["refactor", "fix", "feature"])
def test_targeted_runs_are_recorded_with_reasons_and_full_log(skill: str) -> None:
    """Targeted loop runs record which tests were picked and why, and keep the full runner log, in the run dir."""
    text = (_PLUGIN / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
    assert '--run --record-dir "${DEV_DIR:-.developments}"' in text
    assert "$DEV_DIR/test-targets.jsonl" in text


def test_agent_wait_check_prints_in_flight_progress() -> None:
    """Each wake-up prints a status line per in-flight agent from the helper's pending list and elapsed time."""
    rule = (_PLUGIN / "skills" / "_shared" / "agent-resolution.md").read_text(encoding="utf-8")
    assert "top-level `pending` list" in rule
    assert "`elapsed_s` since spawn" in rule


@pytest.mark.parametrize("skill", ["refactor", "fix", "debug"])
def test_step_level_task_progress_is_kept(skill: str) -> None:
    """Step-level task progress the user saw at HEAD stays visible: one task per step, never collapsed."""
    assert "One task per step, never a bookkeeping-only turn." in (_PLUGIN / "skills" / skill / "SKILL.md").read_text(
        encoding="utf-8"
    )
