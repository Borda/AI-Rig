"""Check that implementation routing preserves proportional independent review."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_small_work_plans_challenges_accepts_then_consolidates() -> None:
    """Keep preparation proportional without dropping preimplementation reasoning."""
    policy = (ROOT / "shared/native-skill-contract.md").read_text(encoding="utf-8")
    section = policy.split("## Proportional Execution", 1)[1].split("Medium/large", 1)[0]
    markers = (
        "concise plan",
        "parent self-challenge",
        "accept the challenged plan",
        "assign execution",
        "make the authorized change",
        "consolidate",
        "one bounded independent challenge",
        "verify",
    )
    positions = [section.index(marker) for marker in markers]
    assert positions == sorted(positions)
    assert "not independent" in section
    assert "actual runtime permissions" in section
    assert "core acceptance" in section


def test_medium_work_challenges_the_plan_and_implemented_result_in_order() -> None:
    """Require a plan challenge before implementation and an outcome challenge afterward."""
    policy = (ROOT / "shared/native-skill-contract.md").read_text(encoding="utf-8")
    section = policy.split("Medium/large", 1)[1].split("For Implement and Manage", 1)[0].lower()
    markers = (
        "one scoped plan",
        "independently challenge the plan",
        "higher-capability advice",
        "approve the challenged plan",
        "assign bounded",
        "implement the accepted plan",
        "independently challenge the implemented result",
        "derive the outcome",
    )
    positions = [section.index(marker) for marker in markers]
    assert positions == sorted(positions)
    assert "do not recursively" in section
    assert "only when explicitly requested" in section


def test_remediation_review_is_one_pass_without_implying_a_loop() -> None:
    """A required closure challenge does not silently create repeated review cycles."""
    skill = (ROOT / "skills/code-remediate/SKILL.md").read_text(encoding="utf-8")
    closure = (ROOT / "skills/code-remediate/references/implementation.md").read_text(encoding="utf-8")
    assert "one bounded independent challenge" in skill
    assert "one bounded independent challenge" in closure
    assert "explicitly requests" in skill
    assert "including plan challenges" not in skill
    assert "do not dispatch another reviewer by default" in closure
