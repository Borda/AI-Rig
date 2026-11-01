"""Check that implementation routing preserves proportional independent review."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SMALL_MARKERS = (
    "concise plan",
    "parent self-challenge",
    "accept the challenged plan",
    "assign execution",
    "make the authorized change",
    "consolidate",
    "one bounded independent challenge",
    "verify",
)
MEDIUM_MARKERS = (
    "one scoped plan",
    "independently challenge the plan",
    "higher-capability advice",
    "approve the challenged plan",
    "assign bounded",
    "implement the accepted plan",
    "independently challenge the implemented result",
    "derive the outcome",
)


def _proportional_execution() -> str:
    """Return the Proportional Execution section body as one whitespace-collapsed line."""
    lines = (ROOT / "shared/native-skill-contract.md").read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith("## Proportional Execution"))
    end = next((index for index in range(start + 1, len(lines)) if lines[index].startswith("## ")), len(lines))
    return " ".join(" ".join(lines[start + 1 : end]).split())


@pytest.fixture
def small_and_medium_work() -> tuple[str, str]:
    """Split the section into its small-work and medium/large-work halves at the `Medium/large` sentence."""
    small, remainder = _proportional_execution().split("Medium/large", 1)
    return small, remainder.split("For Implement and Manage", 1)[0].lower()


def _in_order(text: str, markers: tuple[str, ...]) -> bool:
    """Report whether every marker occurs in the text and in the listed order."""
    positions = [text.find(marker) for marker in markers]
    return -1 not in positions and positions == sorted(positions)


class TestProportionalExecution:
    """Proportional review policy in the native skill contract."""

    def test_small_work_plans_challenges_accepts_then_consolidates(
        self, small_and_medium_work: tuple[str, str]
    ) -> None:
        """Keep preparation proportional without dropping preimplementation reasoning.

        Small work states its steps in a fixed order and names the limits of its self-challenge, so a reordered or
        trimmed policy would silently skip the one independent challenge.
        """
        small, _ = small_and_medium_work
        assert _in_order(small, SMALL_MARKERS)
        assert "not independent" in small
        assert "actual runtime permissions" in small
        assert "core acceptance" in small

    def test_medium_work_challenges_the_plan_and_implemented_result_in_order(
        self, small_and_medium_work: tuple[str, str]
    ) -> None:
        """Require a plan challenge before implementation and an outcome challenge afterward.

        Medium and large work challenges twice, bracketing implementation, and must not grow recursive or unrequested
        extra review rounds.
        """
        _, medium = small_and_medium_work
        assert _in_order(medium, MEDIUM_MARKERS)
        assert "do not recursively" in medium
        assert "only when explicitly requested" in medium


def test_remediation_review_is_one_pass_without_implying_a_loop() -> None:
    """A required closure challenge does not silently create repeated review cycles.

    The remediation skill and its implementation reference both promise one bounded challenge, so neither may imply plan
    challenges, repeated reviewers or an unrequested loop.
    """
    skill = " ".join((ROOT / "skills/code-remediate/SKILL.md").read_text(encoding="utf-8").split())
    closure = " ".join(
        (ROOT / "skills/code-remediate/references/implementation.md").read_text(encoding="utf-8").split()
    )
    assert "one bounded independent challenge" in skill
    assert "one bounded independent challenge" in closure
    assert "explicitly requests" in skill
    assert "including plan challenges" not in skill
    assert "do not dispatch another reviewer by default" in closure
