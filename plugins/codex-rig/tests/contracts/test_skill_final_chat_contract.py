"""Regression checks for concise, outcome-coupled skill handoffs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _skill_text import flat, missing_phrases, read_with_references

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
SKILLS_ROOT = PLUGIN_ROOT / "skills"
QUALITY_GATES = PLUGIN_ROOT / "shared" / "quality-gates.md"
#: Skills whose Output Contract lives in a trigger-loaded reference instead of SKILL.md.
_COMPLETION_REFERENCES = {"code-review": ("findings-and-completion.md",), "code-remediate": ("completion.md",)}
ARTIFACT_SKILLS = (
    "challenge-resolve",
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
#: Handoff-contract link each artifact skill must carry; the two skills with references sit one directory deeper.
_DEEP_HANDOFF_SKILLS = {"code-review", "code-remediate"}
_ARTIFACT_CASES = [
    pytest.param(
        skill,
        ("../../../" if skill in _DEEP_HANDOFF_SKILLS else "../../") + "shared/final-handoff-contract.md",
        3 if skill == "code-review" else 2,
        id=skill,
    )
    for skill in ARTIFACT_SKILLS
]


def _output_contract(text: str) -> str:
    """Return the explicit Output Contract section, not an earlier prose mention.

    Example:
        >>> _output_contract("intro\\n## Output Contract\\nresult").strip()
        'result'
    """
    return flat(text.split("\n## Output Contract\n", maxsplit=1)[1])


def _skill_text(skill: str) -> str:
    """Return a skill's SKILL.md plus the trigger-loaded references that hold its completion contract."""
    return read_with_references(SKILLS_ROOT / skill / "SKILL.md", *_COMPLETION_REFERENCES.get(skill, ()))


@pytest.mark.parametrize(
    ("skill", "result_schema"),
    [
        pytest.param("agent-shims", "Action | Outcome | Verification | Remaining limit", id="agent-shims"),
        pytest.param(
            "challenge-resolve",
            "Challenge | Security | Critical | High | Medium | Low | Nits | Weighted score | Decision | Evidence",
            id="challenge-resolve",
        ),
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
    """Prevent artifact-complete runs whose final chat omits the actual outcome.

    Each skill's Output Contract must describe the final chat and carry the result table schema that names the skill's
    primary result, so a run cannot finish with only an artifact path.
    """
    output_contract = _output_contract(_skill_text(skill))

    assert missing_phrases(output_contract, ("Final chat", result_schema)) == []


def test_shared_final_chat_frame_keeps_artifacts_supplemental() -> None:
    """Require the common outcome-first frame used by every native skill.

    The shared quality gates define the Final Chat Contract; its fixed headings and the rule that an artifact never
    replaces the outcome are what every skill's own contract builds on.
    """
    contract = QUALITY_GATES.read_text(encoding="utf-8")
    headings = (
        "Outcome",
        "Results",
        "Verification",
        "Remaining",
        "Recommendations / next steps",
        "Confidence",
        "Artifact",
    )

    assert missing_phrases(contract, ("## Final Chat Contract", *headings, "never a substitute for the outcome")) == []


@pytest.mark.parametrize(("skill", "handoff_link", "schema_version"), _ARTIFACT_CASES)
def test_artifact_skill_final_chat_is_an_executable_versioned_handoff(
    skill: str, handoff_link: str, schema_version: int
) -> None:
    """Require every artifact workflow to bind and emit the validated final render.

    The Output Contract must link the shared handoff contract and say to emit ``final.md`` verbatim, and the skill's
    result template must carry the matching result and final-handoff schema versions.
    """
    output_contract = _output_contract(_skill_text(skill))
    template = json.loads((SKILLS_ROOT / skill / "result-template.json").read_text(encoding="utf-8"))

    assert missing_phrases(output_contract, (handoff_link, "emit `final.md` verbatim")) == []
    assert template["schema_version"] == schema_version
    assert template["metadata"]["final_handoff"]["schema_version"] == 1


def test_agent_shims_declares_the_non_artifact_handoff_exception() -> None:
    """Prevent the manager-only skill from claiming validation it cannot execute.

    Agent shims produce no run artifacts, so its Output Contract must state the exception and that the handoff is not
    executable instead of copying the artifact skills' validated-render promise.
    """
    output_contract = _output_contract((SKILLS_ROOT / "agent-shims" / "SKILL.md").read_text(encoding="utf-8"))

    assert missing_phrases(output_contract, ("explicit exception", "not executable")) == []


@pytest.mark.parametrize(
    "skill",
    [
        "agent-shims",
        "challenge-resolve",
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
def test_final_chat_contracts_include_next_steps(skill: str) -> None:
    """Require the delivery section without treating paragraph length as a correctness signal."""
    assert "Next steps" in _output_contract(_skill_text(skill))


def test_confidence_deductions_are_accounted_once_in_existing_text_fields() -> None:
    """Prevent unexplained scores, hidden deductions, and duplicate gap accounting.

    The quality gates own the accounting rule; the native contract, the final handoff contract and the baseline
    ``AGENTS.md`` must point at or restate it, and the handoff contract pins how gaps and closures are rendered.
    """
    contract = QUALITY_GATES.read_text(encoding="utf-8")
    section = contract.split("## Confidence Deduction Accounting\n", maxsplit=1)[1].split("\n## ", maxsplit=1)[0]
    handoff = (PLUGIN_ROOT / "shared/final-handoff-contract.md").read_text(encoding="utf-8")
    baseline = (PLUGIN_ROOT / "assets/AGENTS.md").read_text(encoding="utf-8")
    pointer = "quality-gates.md#confidence-deduction-accounting"
    without_pointer = [
        relative
        for relative in ("shared/native-skill-contract.md", "shared/final-handoff-contract.md")
        if pointer not in (PLUGIN_ROOT / relative).read_text(encoding="utf-8")
    ]

    assert (
        missing_phrases(
            section,
            (
                "every reported confidence gap or limitation",
                "ASCII minus",
                "two decimals",
                "exactly `1.00 - score`",
                "`(-0.00)`",
                "one explicit `residual`",
                "cap, floor, or band",
                "count its deduction once",
                "transparent judgment accounting",
                "not an empirically calibrated probability",
            ),
        )
        == []
    )
    assert without_pointer == []
    assert (
        missing_phrases(
            handoff,
            (
                "existing gap/limit strings",
                "matching closure labels",
                "Preserve exact canonical gap identities",
                "existing closure evidence/rationale displayed by the renderer",
                "both gap identity and closure text are fixed",
                "editable corresponding existing limit string",
                "For presentation 12 Code Review `unavailable` output",
                "Select the numeric score as an evidence-backed operator judgment",
                "do not deduct for an unreviewed PR merely because collection stopped",
                "Record each material limitation separately",
                "Unique contributions sum to exactly `1.00 - score`",
                "(-0.00) PR correctness was not assessed.",
                "Keep the prior unavailable profile readable for earlier presentations",
                "Historical rendered bytes",
            ),
        )
        == []
    )
    assert missing_phrases(baseline, ("exactly `1.00 - score`", "`(-0.00)`")) == []
