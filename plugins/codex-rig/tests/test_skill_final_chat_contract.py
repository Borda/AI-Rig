"""Regression checks for concise, outcome-coupled skill handoffs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = PLUGIN_ROOT / "skills"
QUALITY_GATES = PLUGIN_ROOT / "shared" / "quality-gates.md"
ARTIFACT_SKILLS = (
    "audit",
    "calibrate",
    "assess",
    "code-remediate",
    "code-review",
    "implement",
    "investigate",
    "kaggle",
    "manage",
    "optimize",
    "release",
    "research",
    "sync",
)


def _output_contract(text: str) -> str:
    """Return the explicit Output Contract section, not an earlier prose mention.

    Example:
        >>> _output_contract("intro\\n## Output Contract\\nresult").strip()
        'result'
    """
    return text.split("\n## Output Contract\n", maxsplit=1)[1]


@pytest.mark.parametrize(
    ("skill", "result_schema"),
    [
        pytest.param("agent-shims", "Action | Outcome | Verification | Remaining limit", id="agent-shims"),
        pytest.param("audit", "Item | Severity / impact | Decision | Evidence | Next action", id="audit"),
        pytest.param("calibrate", "Check / metric | Result | Evidence | Next action", id="calibrate"),
        pytest.param("assess", "Finding | Impact | Decision | Evidence | Next action", id="assess"),
        pytest.param("implement", "Surface | Outcome | Verification | Remaining limit", id="implement"),
        pytest.param("investigate", "Hypothesis | Evidence | Disposition | Next action", id="investigate"),
        pytest.param("kaggle", "Artifact | Mode | Verification | Runtime limit", id="kaggle"),
        pytest.param("manage", "Surface | Outcome | Verification | Remaining limit", id="manage"),
        pytest.param("optimize", "Iteration | Baseline | After | Delta | Guard | Decision", id="optimize"),
        pytest.param("release", "Change | SemVer impact | Status / blocker | Evidence", id="release"),
        pytest.param("research", "Recommendation | Evidence | Decision | Caveat / next check", id="research"),
        pytest.param("sync", "Surface | Outcome | Verification | Remaining limit", id="sync"),
    ],
)
def test_skill_final_chat_names_its_primary_result(skill: str, result_schema: str) -> None:
    """Prevent artifact-complete runs whose final chat omits the actual outcome."""
    text = (SKILLS_ROOT / skill / "SKILL.md").read_text(encoding="utf-8")
    output_contract = _output_contract(text)
    assert "Final chat" in output_contract
    assert result_schema in output_contract


def test_shared_final_chat_frame_keeps_artifacts_supplemental() -> None:
    """Require the common outcome-first frame used by every native skill."""
    contract = QUALITY_GATES.read_text(encoding="utf-8")
    assert "## Final Chat Contract" in contract
    assert all(
        heading in contract
        for heading in (
            "Outcome",
            "Results",
            "Verification",
            "Remaining",
            "Recommendations / next steps",
            "Confidence",
            "Artifact",
        )
    )
    assert "never a substitute for the outcome" in contract


@pytest.mark.parametrize("skill", ARTIFACT_SKILLS)
def test_artifact_skill_final_chat_is_an_executable_schema_v2_handoff(skill: str) -> None:
    """Require every artifact workflow to bind and emit the validated final render."""
    skill_text = (SKILLS_ROOT / skill / "SKILL.md").read_text(encoding="utf-8")
    output_contract = _output_contract(skill_text)
    template = json.loads((SKILLS_ROOT / skill / "result-template.json").read_text(encoding="utf-8"))

    assert "../../shared/final-handoff-contract.md" in output_contract
    assert "emit `final.md` verbatim" in output_contract
    assert template["schema_version"] == 2
    assert template["metadata"]["final_handoff"]["schema_version"] == 1


def test_agent_shims_declares_the_non_artifact_handoff_exception() -> None:
    """Prevent the manager-only skill from claiming validation it cannot execute."""
    output_contract = _output_contract((SKILLS_ROOT / "agent-shims" / "SKILL.md").read_text(encoding="utf-8"))
    assert "explicit exception" in output_contract
    assert "not executable" in output_contract


@pytest.mark.parametrize(
    "skill",
    [
        "agent-shims",
        "audit",
        "calibrate",
        "assess",
        "code-remediate",
        "code-review",
        "implement",
        "investigate",
        "kaggle",
        "manage",
        "optimize",
        "release",
        "research",
        "sync",
    ],
)
def test_complex_final_chat_contracts_use_structure_not_dense_prose(skill: str) -> None:
    """Keep branch-heavy output instructions scannable without hard-wrapping prose."""
    text = (SKILLS_ROOT / skill / "SKILL.md").read_text(encoding="utf-8")
    output_contract = _output_contract(text)
    assert "Next steps" in output_contract
    dense_lines = [line for line in output_contract.splitlines() if len(line) > 600]
    assert dense_lines == []
