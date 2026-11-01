"""Keep genuinely missing workflow decisions reachable without asking for local Git work that needs no authority.

Question sites remain available for unsupported scope, missing resolution intent, withheld authority or other unresolved
decisions. A task-owned target merge proceeds without a question or grant check, and a recorded local Git approval grant
answers its bounded plain-commit decision; neither is a silent default, and neither may trigger the same question again.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

import pytest
from _skill_text import read_with_references

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
SKILL = "skills/code-remediate/SKILL.md"
COMMIT_TEMPLATE = "shared/commit-response-template.md"
#: Existing merge or conflict recovery moved out of SKILL.md; its questions are pinned in the reference.
EXISTING_CONFLICT = "skills/code-remediate/references/existing-conflict.md"
#: Collection failure recovery moved out of SKILL.md; its authorization question is pinned in the reference.
SOURCE_RECOVERY = "skills/code-remediate/references/source-recovery.md"
NATIVE = "shared/native-skill-contract.md"


@cache
def _text(relative: str) -> str:
    """Return one contract file with wrapping collapsed so assertions survive reflow."""
    return " ".join(
        (
            read_with_references(
                PLUGIN_ROOT / relative, "intake.md", "selection.md", "implementation.md", "completion.md"
            )
            if relative == "skills/code-remediate/SKILL.md"
            else (PLUGIN_ROOT / relative).read_text(encoding="utf-8")
        ).split()
    )


def _missing(text: str, phrases: tuple[str, ...]) -> list[str]:
    """Return the required phrases that do not occur in the whitespace-collapsed text."""
    return [phrase for phrase in phrases if phrase not in text]


@pytest.mark.parametrize(
    ("relative", "phrase"),
    [
        pytest.param(SKILL, "ask only for the missing finding evidence or selection", id="02-missing-finding-evidence"),
        pytest.param(
            SOURCE_RECOVERY,
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
            EXISTING_CONFLICT,
            "`Authorize finishing this existing merge and the described local commit?`",
            id="03-existing-merge-recovery",
        ),
        pytest.param(
            EXISTING_CONFLICT,
            "Ask only for the missing scope expansion through User Questions",
            id="03-repair-scope-expansion",
        ),
        pytest.param(EXISTING_CONFLICT, "ask for that specific commit decision", id="03-partial-merge-commit"),
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
def test_ask_site_still_present(relative: str, phrase: str) -> None:
    """Each pre-change question or approval site is still present in the current contract.

    A genuinely missing decision must stay reachable by the user, so the exact sentence that triggers each question
    (scope, resolution intent, recovery authority, commit mode, escalation) is pinned in the file that owns it.
    """
    assert phrase in _text(relative)


def test_commit_preference_reuses_a_grant_and_preserves_explicit_hold() -> None:
    """Optional commit grouping defaults under a local Git approval grant while an explicit deferral is retained.

    Under a grant an unanswered or dismissed grouping preference uses `all at once`, an explicit `decide after
    verification` answer still defers to the commit step, and without a grant the question stays.
    """
    skill = _text(SKILL)
    packet = skill.split("### Upfront Decision Packet", 1)[1].split("### 06:", 1)[0]
    commit = skill.split("### 12: Offer An Opt-In Commit After Verified Remediation", 1)[1]

    assert (
        _missing(
            packet,
            (
                "Recommend `all at once` under a grant",
                "an unanswered or dismissed grouping preference uses `all at once`",
                "An explicit `decide after verification` answer still defers",
                "Without a grant, an unanswered",
                "a commit mode answers **Approve** for that plan and `leave unstaged` answers **Deny**",
            ),
        ),
        _missing(
            commit,
            (
                "an explicit `decide after verification` answer asks here",
                "Record the grant as the consent source",
                "a packet `leave unstaged` answer makes the disposition `declined`",
            ),
        ),
    ) == ([], [])


def test_finalize_recovers_cosmetics_without_bypassing_validation_or_decisions() -> None:
    """Repeated diagnostics are classified and presentation is repaired while evidence and authority gates hold.

    `finalize` repairs derived output automatically and continues authorized remediation, but never answers a user
    question or runtime approval, never bypasses validation, and never repeats an unchanged failing command.
    """
    assert (
        _missing(
            _text(SKILL),
            (
                "`finalize` never answers a user question or runtime approval",
                "`--promote` renames only a validated candidate",
                "If the same error code remains after its repair, diagnose its cause",
                "repair owned derived output automatically and continue authorized remediation without a new user decision",
                "concrete unresolved structural, factual, verification or authority boundary",
                "preserve the candidate until validation passes",
                "Never repeat an unchanged failing command, bypass validation, or end silently",
            ),
        )
        == []
    )
