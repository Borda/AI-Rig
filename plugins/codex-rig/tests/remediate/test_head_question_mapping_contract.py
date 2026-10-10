"""Keep genuinely missing workflow decisions reachable without asking for local Git work that needs no authority.

Question sites remain available for unsupported scope, missing resolution intent, withheld authority or other unresolved
decisions. A task-owned target merge proceeds without a question or grant check, and a recorded local Git approval grant
answers its bounded plain-commit decision; neither is a silent default, and neither may trigger the same question again.
"""

from __future__ import annotations

from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
SKILL = "skills/code-remediate/SKILL.md"
COMMIT_TEMPLATE = "shared/commit-response-template.md"
NATIVE = "shared/native-skill-contract.md"


def _text(relative: str) -> str:
    """Return one contract file with wrapping collapsed so assertions survive reflow."""
    return " ".join((PLUGIN_ROOT / relative).read_text(encoding="utf-8").split())


@pytest.mark.parametrize(
    ("relative", "phrase"),
    [
        pytest.param(SKILL, "ask only for the missing finding evidence or selection", id="02-missing-finding-evidence"),
        pytest.param(
            SKILL,
            "ask one question with accepted answers; explain what approval and decline mean",
            id="02-collection-recovery-authorization",
        ),
        pytest.param(
            SKILL,
            "request runtime approval for the complete owning command when required capability is unavailable",
            id="02-collection-runtime-approval",
        ),
        pytest.param(
            SKILL,
            "Ask only for genuinely missing scope or resolution intent, and only that decision, never merge authorization",
            id="03-target-merge-resolution-intent",
        ),
        pytest.param(
            SKILL,
            "`Authorize finishing this existing merge and the described local commit?`",
            id="03-existing-merge-recovery",
        ),
        pytest.param(
            SKILL, "Ask only for the missing scope expansion through User Questions", id="03-repair-scope-expansion"
        ),
        pytest.param(SKILL, "ask for that specific commit decision", id="03-partial-merge-commit"),
        pytest.param(
            SKILL,
            "stop before edits and open the scope menu once using the context/control ordering above",
            id="05-scope",
        ),
        pytest.param(SKILL, "Each `out-of-scope` item needs user justification/confirmation", id="05-out-of-scope"),
        pytest.param(SKILL, "Do not ask the user to approve parent-owned or sequential execution", id="06-no-new-ask"),
        pytest.param(SKILL, "An explicit user denial still stops the denied route", id="06-explicit-route-denial"),
        pytest.param(SKILL, "Ask only for a genuinely missing route or scope decision", id="08-missing-route"),
        pytest.param(SKILL, "Do not ask for commit authorization while blocked", id="11-blocked-commit"),
        pytest.param(
            SKILL,
            "Ask exactly once through User Questions only for a missing or materially changed decision",
            id="12-commit-mode",
        ),
        pytest.param(
            SKILL,
            "Do not stage without a local Git approval grant or an explicit valid answer bound to this plan",
            id="12-no-silent-stage",
        ),
        pytest.param(
            COMMIT_TEMPLATE,
            "When conversational commit authorization is actually missing",
            id="template-commit-authorization",
        ),
        pytest.param(
            COMMIT_TEMPLATE,
            "If an actual missing capability requires runtime approval",
            id="template-commit-runtime-approval",
        ),
        pytest.param(NATIVE, "Before every intentional approval request", id="native-approval-brief"),
        pytest.param(
            NATIVE,
            "ask one concrete question through User Questions, with separate canonical options `Approve` and `Deny`",
            id="native-actionable-pause",
        ),
        pytest.param(
            "shared/adversarial-loop.md",
            "stop dependent work and ask for the exact missing decision",
            id="loop-severe-escalation",
        ),
    ],
)
def test_pre_change_ask_site_still_happens(relative: str, phrase: str) -> None:
    """Keep each pre-change question or approval present in the current contract."""
    assert phrase in _text(relative)


def test_commit_preference_reuses_a_grant_and_preserves_explicit_hold() -> None:
    """Default optional grouping under a local Git approval grant while retaining a user's explicit deferred choice."""
    skill = _text(SKILL)
    packet = skill.split("### Upfront Decision Packet", 1)[1].split("### 06:", 1)[0]
    commit = skill.split("### 12: Offer An Opt-In Commit After Verified Remediation", 1)[1]
    assert "Recommend `all at once` under a grant" in packet
    assert "an unanswered or dismissed grouping preference uses `all at once`" in packet
    assert "An explicit `decide after verification` answer still defers" in packet
    assert "Without a grant, an unanswered" in packet
    assert "a commit mode answers **Approve** for that plan and `leave unstaged` answers **Deny**" in packet
    assert "an explicit `decide after verification` answer asks here" in commit
    assert "Record the grant as the consent source" in commit
    assert "a packet `leave unstaged` answer makes the disposition `declined`" in commit


def test_finalize_never_answers_decisions_and_escalates_repeated_failures() -> None:
    """Keep one-call finalization from answering user decisions or silently stopping on a repeated failure."""
    skill = _text(SKILL)

    assert "`finalize` never answers a user question or runtime approval" in skill
    assert "`--promote` renames only a validated candidate" in skill
    assert "If the same error code remains after its repair, stop repairing that code and follow" in skill
    assert "never end silently or emit an unvalidated handoff" in skill
