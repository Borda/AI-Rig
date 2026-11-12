"""Keep result authoring templates aligned with the shared gate outcome contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

SKILLS_ROOT = Path(__file__).resolve().parents[2] / "skills"


@pytest.mark.parametrize(
    "skill",
    [
        "assess",
        "audit",
        "calibrate",
        "challenge-resolve",
        "code-review",
        "implement",
        "investigate",
        "kaggle",
        "manage",
        "optimize",
        "profile",
        "release",
        "research",
        "sync",
    ],
)
def test_result_template_admits_every_shared_gate_outcome(skill: str) -> None:
    """Authors can report a timed-out run without substituting a pass or failure."""
    template = json.loads((SKILLS_ROOT / skill / "result-template.json").read_text(encoding="utf-8"))

    assert set(template["status"].split("|")) == {"pass", "fail", "timeout"}


def test_profile_result_template_declares_the_current_result_schema() -> None:
    """A profiling result authored from its template identifies the schema used by its writer."""
    template = json.loads((SKILLS_ROOT / "profile" / "result-template.json").read_text(encoding="utf-8"))

    assert template["schema_version"] == 2
