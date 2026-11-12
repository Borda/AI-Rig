"""Keep extracted specialist contracts reachable before their dependent workflow actions."""

from pathlib import Path

import pytest

_PLUGIN = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("agent", "reference", "trigger", "obligation"),
    [
        pytest.param(
            "qa-specialist",
            "test-validation.md",
            "Before applying the validation levels",
            "Every test must pass three levels, in sequence",
            id="qa-validation",
        ),
        pytest.param(
            "qa-specialist",
            "test-patterns.md",
            "Before writing, reviewing or changing tests",
            "Assert the exception type **and**",
            id="qa-patterns",
        ),
        pytest.param(
            "qa-specialist",
            "review-assertions.md",
            "Before reading target code or reporting findings",
            "Never claim a pattern exists without confirming",
            id="qa-evidence",
        ),
        pytest.param(
            "qa-specialist",
            "teammate-protocol.md",
            "When operating as an Agent Teams teammate",
            "review proposed API BEFORE implementation starts",
            id="qa-team",
        ),
        pytest.param(
            "qa-specialist",
            "test-antipatterns.md",
            "Before reporting findings",
            "call order as primary",
            id="qa-antipatterns",
        ),
        pytest.param(
            "solution-architect",
            "finding-methodology.md",
            "Before architectural analysis",
            "**Never promote secondary to primary**",
            id="architect-findings",
        ),
        pytest.param(
            "solution-architect",
            "boundary-checks.md",
            "Before assessing cohesion",
            "side-effect-safe only",
            id="architect-boundaries",
        ),
        pytest.param(
            "solution-architect",
            "design-antipatterns.md",
            "Before reporting architectural findings",
            "Destructive migration before consumer cutover",
            id="architect-antipatterns",
        ),
    ],
)
def test_specialist_loads_full_contract_before_dependent_action(
    agent: str,
    reference: str,
    trigger: str,
    obligation: str,
) -> None:
    """Require an installed-path Bash read and retain the moved section's binding obligation."""
    lines = (_PLUGIN / "agents" / f"{agent}.md").read_text(encoding="utf-8").splitlines()
    load = next(line for line in lines if f"references/{agent}/{reference}" in line)
    assert trigger in load
    assert f'cat "${{CLAUDE_PLUGIN_ROOT}}/references/{agent}/{reference}"' in load
    assert "Bash tool" in load
    assert "follow its complete contract" in load
    detail = (_PLUGIN / "references" / agent / reference).read_text(encoding="utf-8").splitlines()
    assert any(obligation in line for line in detail)


def test_consolidation_spine_loads_pricing_detail_before_settling() -> None:
    """Keep pricing internals in the guard contract and its operational spine at four bullets."""
    lines = (_PLUGIN / "skills/audit/modes/consolidate.md").read_text(encoding="utf-8").splitlines()
    first = lines.index("After the wave, settle deterministically. The script — not the judge — applies the rules:")
    stop = lines.index("## Phase 5 — Independent review (`foundry:challenger`, always)")
    phase = lines[first:stop]
    assert len([line for line in phase if line.startswith("- ")]) == 4
    assert not any(line.startswith("Pricing:") for line in phase)
    load = next(index for index, line in enumerate(phase) if "load_mode.py" in line)
    settle = next(index for index, line in enumerate(phase) if line.startswith("python ") and " settle " in line)
    assert load < settle
    assert "consolidate-guards.md" in phase[load]
    guards = (_PLUGIN / "skills/audit/modes/consolidate-guards.md").read_text(encoding="utf-8").splitlines()
    pricing = next(line for line in guards if line.startswith("Pricing:"))
    assert "lazy savings are never summed with always-loaded ones" in pricing
    assert "every merge-prune" in pricing
    assert "Obligation files of a merge that no longer stands are removed" in pricing
