"""Regression checks that code-remediate collects predictable decisions with the scope question."""

from __future__ import annotations

import re
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
CODE_REMEDIATE_SKILL = PLUGIN_ROOT / "skills" / "code-remediate" / "SKILL.md"
COMMIT_VALUES = (
    "`all at once`",
    "`group findings by topic`",
    "`each finding as a separate commit`",
    "`leave unstaged`",
    "`decide after verification`",
)


def _span(start: str, end: str) -> str:
    """Return one skill span with wrapping collapsed so assertions survive reflow."""
    text = CODE_REMEDIATE_SKILL.read_text(encoding="utf-8").split(start, maxsplit=1)[1].split(end, maxsplit=1)[0]
    return " ".join(text.split())


def test_packet_is_part_of_scope_checkpoint_and_keeps_rendered_scope() -> None:
    """Fold commit and plan preferences into step 05 without changing the frozen scope presentation.

    Measured remediation runs parked for a median 14 minutes on questions asked after the user had answered scope and
    left; the packet must not alter the helper-rendered scope bytes or presentation version.
    """
    scope_step = _span("### 05: Ask For Resolution Scope Before Editing", "### 06:")
    packet = _span("### Upfront Decision Packet", "### 06:")

    assert "### Upfront Decision Packet" in CODE_REMEDIATE_SKILL.read_text(encoding="utf-8")
    assert packet in scope_step
    assert "steps 06–11 run without another conversational question on the normal path" in packet
    assert "`Commit verified remediation-owned changes after gates pass?`" in packet
    assert all(value in packet for value in COMMIT_VALUES)
    assert "`proceed automatically`, `show the plan and wait for my approval`" in packet
    assert "The packet adds no line to that file and does not change `presentation_version`" in packet
    assert "ask the commit preference first" in packet
    assert "the scope question last, so the final answer the user gives releases the run" in packet
    assert "Never hide a feasible commit value behind Other" in packet
    assert "Explicit `remediation_scope` input asks no packet question" in packet
    assert "`## Upfront Decisions`" in packet
    assert "An unanswered packet question grants nothing" in packet
    assert "Never combine scope, commit, and work-plan decisions in one question or answer field" in packet
    assert "The packaged `ask_user` form has one answer field" in packet


def test_packet_lists_the_only_mid_run_questions() -> None:
    """Keep data-dependent and recovery questions mid-run and leave merge authorization before scope."""
    packet = _span("### Upfront Decision Packet", "### 06:")
    for allowed in (
        "a work plan the user asked to review",
        "a scope expansion or out-of-scope confirmation",
        "missing finding evidence or reviewer route",
        "an adversarial-loop escalation or stop",
        "an existing-merge or collection recovery",
        "a commit whose packet answer no longer binds at step 12",
        "Runtime permission approvals are not conversational questions",
        "Target-merge authorization stays at step 03",
    ):
        assert allowed in packet


def test_no_foldable_question_between_scope_and_commit_steps() -> None:
    """Reject commit or plan questions in steps 06–11 except the plan review the user opted into."""
    middle = _span("### 06: Build And Approve The Work Bucket Plan", "### 12:")
    question_sentences = [sentence for sentence in re.split(r"(?<=[.;])\s", middle) if "User Questions" in sentence]

    assert len(question_sentences) == 1
    assert "upfront packet answer is `show the plan and wait for my approval`" in question_sentences[0]
    assert "Commit verified remediation-owned changes" not in middle
    assert "Do not ask for commit authorization while blocked" in middle


def test_commit_step_reuses_packet_answer_and_names_material_changes() -> None:
    """Bind the upfront commit mode at step 12 and re-ask only when its preconditions changed."""
    commit = _span("### 12: Offer An Opt-In Commit After Verified Remediation", "## Fail-fast Rules")
    fail_fast = _span("## Fail-fast Rules", "## Quality Gates")

    assert "An upfront packet commit mode is such an answer" in commit
    assert "a packet `leave unstaged` answer makes the disposition `declined`" in commit
    assert "`decide after verification`, or a preference left unanswered or dismissed, asks here" in commit
    assert "A new exclusion, destination change, infeasible grouping, or external-obligation commit" in commit
    assert "`remediation-foldable-question-after-scope`" in fail_fast
