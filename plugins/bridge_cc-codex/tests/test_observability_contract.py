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
    ("skill", "command"),
    [
        pytest.param("status", 'bridge_call.py" status --job-id "<job-id>"', id="status"),
        pytest.param("result", 'bridge_call.py" result --job-id "<job-id>"', id="result"),
        pytest.param("cancel", 'bridge_call.py" cancel --job-id "<job-id>"', id="cancel"),
    ],
)
def test_detached_job_state_stays_inspectable(skill: str, command: str) -> None:
    """Replacing polling loops kept every lifecycle command, so a detached job's state is still visible on demand."""
    assert command in (_PLUGIN / "claude-skills" / skill / "SKILL.md").read_text(encoding="utf-8")


def test_detached_dispatch_still_returns_the_job_identifier() -> None:
    """A detached implement call still hands back the job identifier the lifecycle skills need."""
    text = (_PLUGIN / "claude-skills" / "implement" / "SKILL.md").read_text(encoding="utf-8")
    assert "If detached, return job identifier; direct caller to `/bridge:status`, `/bridge:result`" in text
