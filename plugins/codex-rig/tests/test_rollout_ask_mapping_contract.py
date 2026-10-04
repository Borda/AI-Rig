"""Preserve actual missing-decision and runtime boundaries without forcing historical routine questions."""

from __future__ import annotations

import re
from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
INSTRUCTION_ROOTS = ("skills", "shared", "roles", "assets")
POLLING_PATTERNS = (
    re.compile(r"(?i)\bpoll(?:ing)?\s+(?:every|until|again|for)\b"),
    re.compile(r"(?i)\bsleep\s+\d"),
    re.compile(r"(?i)\bcheck\s+again\s+(?:in|after)\b"),
    re.compile(r"(?i)\bkeep\s+(?:polling|checking)\b"),
    re.compile(r"(?i)\bloop\s+until\s+(?:the\s+)?(?:agent|child|job)\b"),
)
NEGATION = re.compile(r"(?i)\b(?:never|no|not|do not|don't|without)\b")


def _text(relative: str) -> str:
    """Return one contract file with wrapping collapsed so assertions survive reflow."""
    return " ".join((PLUGIN_ROOT / relative).read_text(encoding="utf-8").split())


@pytest.mark.parametrize(
    ("relative", "phrase"),
    [
        pytest.param(
            "skills/code-review/SKILL.md",
            "or request runtime approval for the complete owning command when required capability is unavailable",
            id="review-collection-runtime-approval",
        ),
        pytest.param(
            "skills/code-review/SKILL.md",
            "ask for canonical PR URL and repository identity",
            id="review-pr-not-found",
        ),
        pytest.param(
            "skills/code-review/SKILL.md",
            "Ask for decision only when explicit independence requirement cannot be met after completing available "
            "inspection",
            id="review-independence-decision",
        ),
        pytest.param(
            "skills/code-review/SKILL.md",
            "ask only for a concrete missing independent-route decision",
            id="review-missing-independent-route-decision",
        ),
        pytest.param(
            "skills/code-review/SKILL.md",
            "Only a genuinely missing decision about expanded scope or a sensitive/protected effect warrants a question",
            id="review-protected-repair-decision",
        ),
        pytest.param(
            "skills/code-review/SKILL.md",
            "If a required async question is accepted, yield immediately",
            id="review-pending-question",
        ),
        pytest.param("skills/implement/SKILL.md", "Before asking, read [User Questions]", id="implement-questions"),
        pytest.param(
            "skills/investigate/SKILL.md", "retain runtime approval boundaries", id="investigate-runtime-approval"
        ),
        pytest.param(
            "skills/investigate/SKILL.md", "Diagnosis does not authorize source fixes", id="investigate-scope"
        ),
        pytest.param(
            "skills/challenge-resolve/SKILL.md",
            "ask the user for direction before another affected dispatch or fix",
            id="challenge-blocker-direction",
        ),
        pytest.param(
            "skills/challenge-resolve/SKILL.md",
            "ask for a remediation or retry decision before a new paid attempt",
            id="challenge-paid-retry",
        ),
        pytest.param(
            "skills/challenge-resolve/SKILL.md",
            "ask only for the missing decision",
            id="challenge-stop-decision",
        ),
    ],
)
def test_required_decision_boundary_remains(relative: str, phrase: str) -> None:
    """Keep missing input and permissions explicit without making routine recovery a decision."""
    assert phrase in _text(relative)


def test_review_test_gate_runs_the_full_selection() -> None:
    """Keep the review test gate on the project's full selection; targeted selection is only for loops.

    The review gate is that skill's final gate, so it runs once with the repository's own settings. Fresh CI evidence
    may be cited but must never replace or skip the local gate.
    """
    review = _text("skills/code-review/SKILL.md")

    assert "pass the project's full test selection with its own settings, never a changed-file subset" in review
    assert "record the exact pytest arguments in `review-notes.md`" in review
    assert "never replace or skip the local gate" in review
    assert "pass its targets to `--pytest-args-json`" not in review


def test_loop_skills_run_targeted_tests_and_the_full_suite_once() -> None:
    """Keep loop runs on changed-file targets and the full suite at the single canonical gate."""
    native = _text("shared/native-skill-contract.md")
    remediate = _text("skills/code-remediate/SKILL.md")

    assert "PLUGIN_ROOT/bin/python PLUGIN_ROOT/shared/test_targets.py --help" in native
    assert "Never run the full suite per finding, per fix, or per loop iteration" in native
    assert "never run the full suite per finding or per group" in remediate
    assert "The full suite runs exactly once, at the step 09 gate, with the repository's own settings" in remediate


def test_agent_waits_never_poll() -> None:
    """Route every child wait through a blocking wait with a deadline instead of polling."""
    native = _text("shared/native-skill-contract.md")

    assert "Never poll: no repeated `list_agents` or status calls, `sleep`" in native
    assert "per-agent deadline of 30 minutes" in native
    assert "recorded as `timed_out` at once" in native
    assert "[Agent Waits](native-skill-contract.md#agent-waits)" in _text("shared/specialist-orchestration.md")
    assert "[Agent Waits](../../shared/native-skill-contract.md#agent-waits)" in _text("skills/code-review/SKILL.md")
    assert "[Agent Waits](../../shared/native-skill-contract.md#agent-waits)" in _text(
        "skills/challenge-resolve/SKILL.md"
    )


def test_plan_updates_ride_with_real_work() -> None:
    """Forbid plan-only bookkeeping turns."""
    assert "Never spend a turn on a plan update alone" in _text("shared/native-skill-contract.md")


@pytest.mark.parametrize(
    "path",
    sorted(
        path.relative_to(PLUGIN_ROOT).as_posix()
        for root in INSTRUCTION_ROOTS
        for path in (PLUGIN_ROOT / root).rglob("*.md")
    ),
)
def test_instructions_contain_no_polling_directive(path: str) -> None:
    """Flag any instruction that tells an agent to poll, sleep, or loop while waiting.

    Prohibitions such as "never poll" are allowed; a positive instruction to poll is the regression this audit catches.
    """
    sentences = re.split(r"(?<=[.;:])\s", _text(path))
    offending = [
        sentence
        for sentence in sentences
        if any(pattern.search(sentence) for pattern in POLLING_PATTERNS) and not NEGATION.search(sentence)
    ]

    assert offending == []


def test_review_finalizes_in_one_call_without_skipping_completion() -> None:
    """Fold review render, write, and both validators into one call while keeping the finder completion step."""
    review = _text("skills/code-review/SKILL.md")

    assert "Run steps 1–5 as one command" in review
    assert "`--skill code-review`" in review
    assert "promotes only when both pass" in review
    assert "Step 6 still runs separately." in review
