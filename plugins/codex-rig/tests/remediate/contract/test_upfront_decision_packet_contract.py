"""Contract text that code-remediate collects predictable decisions together with the scope question.

The checks read the shipped skill Markdown line by line (heading-anchored spans, whitespace collapsed), so reflowing a
paragraph never breaks an assertion.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from _contract_text import absent_phrases
from _skill_text import read_with_references

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
CODE_REMEDIATE_SKILL = PLUGIN_ROOT / "skills" / "code-remediate" / "SKILL.md"
SCOPE_STEP = "### 05: Ask For Resolution Scope Before Editing"
PACKET = "### Upfront Decision Packet"
COMMIT_STEP = "### 12: Offer An Opt-In Commit After Verified Remediation"
COMMIT_VALUES = (
    "`all at once`",
    "`group findings by topic`",
    "`each finding as a separate commit`",
    "`leave unstaged`",
    "`decide after verification`",
)


def _span(text: str, start: str, end: str) -> str:
    """Return the collapsed lines after the first ``start`` heading line up to the next ``end`` heading line."""
    lines = text.splitlines()
    first = next((index for index, line in enumerate(lines) if line.startswith(start)), None)
    assert first is not None, f"heading {start!r} not found"
    last = next((index for index in range(first + 1, len(lines)) if lines[index].startswith(end)), None)
    assert last is not None, f"heading {end!r} not found after {start!r}"
    return " ".join(" ".join(lines[first + 1 : last]).split())


@pytest.fixture(scope="module")
def skill() -> str:
    """Return the code-remediate SKILL.md joined with its trigger-loaded reference files."""
    return read_with_references(CODE_REMEDIATE_SKILL, "intake.md", "selection.md", "implementation.md", "completion.md")


@pytest.fixture(scope="module")
def packet(skill: str) -> str:
    """Return the Upfront Decision Packet section, from its heading to step 06."""
    return _span(skill, PACKET, "### 06:")


@pytest.fixture(scope="module")
def commit_step(skill: str) -> str:
    """Step 12, the opt-in commit offer, up to the fail-fast rules."""
    return _span(skill, COMMIT_STEP, "## Fail-Fast Rules")


class TestUpfrontDecisionPacket:
    """The scope checkpoint's packet of commit and plan preferences, and the later steps that reuse its answers."""

    def test_folds_into_scope_checkpoint_keeping_rendered_scope(self, skill: str, packet: str):
        """Commit and plan preferences join step 05 without changing the frozen scope presentation.

        Measured remediation runs parked for a median 14 minutes on questions asked after the user had answered scope
        and left; the packet must not alter the helper-rendered scope bytes or presentation version.
        """
        scope_step = _span(skill, SCOPE_STEP, "### 06:")

        assert packet in scope_step
        assert not absent_phrases(
            packet,
            "steps 06–11 run without another conversational question on the normal path",
            "`How should verified remediation-owned changes be grouped into commits?`",
            *COMMIT_VALUES,
            "`proceed automatically`, `show the plan and wait for my approval`",
            "The packet adds no line to that file and does not change `presentation_version`",
            "ask the scope question first and complete any Custom follow-up",
            "optional grouping never blocks standing approval",
            "Never hide a feasible commit value behind Other",
            "Explicit `remediation_scope` input skips the scope question",
            "`## Upfront Decisions`",
            "An unanswered packet question grants nothing new",
            "Never combine scope, commit, and work-plan decisions in one question or answer field",
            "The packaged `ask_user` form has one answer field",
        )

    def test_lists_only_mid_run_questions(self, packet: str):
        """Data-dependent and recovery questions stay mid-run, and merge authorization stays before scope.

        Everything not on this list must be folded into the packet, so the list is the exhaustive set of allowed
        interruptions after the scope answer.
        """
        assert not absent_phrases(
            packet,
            "a work plan the user asked to review",
            "a scope expansion or out-of-scope confirmation",
            "missing finding evidence or reviewer route",
            "an adversarial-loop escalation or stop",
            "an existing-merge or collection recovery",
            "a commit whose packet answer no longer binds at step 12",
            "Runtime permission approvals are not conversational questions",
            "Target-merge authorization stays at step 03",
        )

    def test_has_no_foldable_question_between_scope_and_commit_steps(self, skill: str):
        """Steps 06–11 ask no commit or plan question except the plan review the user opted into.

        Exactly one User Questions sentence may remain there, tied to the packet answer `show the plan and wait for my
        approval`; any other question belongs in the packet.
        """
        middle = _span(skill, "### 06: Build And Approve The Work Bucket Plan", "### 12:")

        question_sentences = [sentence for sentence in re.split(r"(?<=[.;])\s", middle) if "User Questions" in sentence]

        assert len(question_sentences) == 1
        assert "upfront packet answer is `show the plan and wait for my approval`" in question_sentences[0]
        assert "Commit verified remediation-owned changes" not in middle
        assert "Do not ask for commit authorization while blocked" in middle

    def test_commit_step_reuses_packet_answer_and_names_material_changes(self, skill: str, commit_step: str):
        """Step 12 binds the upfront commit mode and re-asks only when its preconditions changed.

        A packet answer is a commit decision already made; the step may ask again only for a declined, deferred or
        materially changed decision, and the fail-fast rules name the foldable-question violation.
        """
        fail_fast = _span(skill, "## Fail-Fast Rules", "## Quality Gates")

        assert not (
            absent_phrases(
                commit_step,
                "An upfront packet commit mode is such an answer",
                "a packet `leave unstaged` answer makes the disposition `declined`",
                "an explicit `decide after verification` answer asks here",
                "A new exclusion, destination change, or infeasible grouping is a materially changed decision",
                "An explicit local snapshot within the recorded mode and owned scope retains that authorization",
            )
            + absent_phrases(fail_fast, "`remediation-foldable-question-after-scope`")
        )

    def test_owns_native_discovery_and_single_delivery_before_scope_prompt(self, skill: str, packet: str):
        """The earlier scope template is not asked again as part of the packet.

        The packet resolves its delivery route before any question opens, records one delivery owner per decision, and
        never resubmits a pending question to improve its look.
        """
        context = _span(skill, "### Terminal Scope Context Contract", PACKET)

        assert not (
            absent_phrases(
                context,
                "resolve the Upfront Decision Packet route before emitting context or opening any question",
                "not a separate prompt to execute before the packet",
            )
            + absent_phrases(
                packet,
                "Inspect directly exposed tools and discover the current plugin's `ask_user` in `ALL_TOOLS`",
                "Record one delivery owner and state per decision",
                "Never print the packet's live questions or options in commentary or final",
                "one distinct decision ID for scope and another for commit preference",
                "Do not submit the same pending question again to improve its appearance",
            )
        )

    def test_chooses_native_sequential_form_when_bundled_control_cannot_fit(self, packet: str):
        """Optional-only sync and text-only async delivery never replace the available native form.

        When the five commit choices cannot fit a three-choice limit, the packet falls back to the native scope form
        followed by the native commit form instead of a weaker bundled control.
        """
        assert not absent_phrases(
            packet,
            "optional-only synchronous tool cannot carry commit authorization",
            "five commit choices cannot fit a three-choice limit",
            "Text-only async delivery is unsuitable for later required questions",
            "omit the work-plan question",
            "native `ask_user` scope form, then the native commit form",
        )

    def test_asks_scope_before_commit_grouping_without_workplan_question(self, packet: str):
        """Scope is asked first, including custom scope and already-supplied answers, and no work-plan question joins.

        A cancelled or unanswered scope leaves scope pending rather than advancing to commit grouping, and only a still-
        missing commit preference is asked afterward.
        """
        assert packet.index("| Findings scope |") < packet.index("| Commit preference |")
        assert not absent_phrases(
            packet,
            "scope first, then commit grouping",
            "Finish a Custom selection follow-up before asking about commits",
            "never add it to the scope/commit packet",
            "A cancelled or unanswered required scope leaves scope pending and does not advance to commit grouping",
            "ask only the still-missing commit preference afterward",
        )

    def test_single_actionable_item_skips_grouping_without_granting_consent(self, skill: str, packet: str):
        """One selected actionable item omits the grouping question without silently authorizing its commit.

        The single-item default is neither a user answer nor commit authorization, and explicit `leave unstaged` or
        `decide after verification` preferences survive it.
        """
        row = next(line for line in skill.splitlines() if "| Commit preference |" in line)

        assert "more than one selected actionable item" in row
        assert not absent_phrases(
            packet,
            "Count selected actionable items after scope resolution",
            "one item selected from many",
            "`all` resolving to one item",
            "Custom or explicit `remediation_scope` resolving to one item",
            "exactly one, omit the commit-grouping question",
            "single-item default, not a user answer or commit authorization",
            "Preserve explicit commit preferences, including `leave unstaged` and `decide after verification`",
            "only when the resolved selection has more than one actionable item",
        )

    def test_single_item_commit_offer_collapses_equivalent_modes(self, commit_step: str):
        """The final consent offer for one item never restores the redundant three-way grouping menu.

        With exactly one selected actionable item only `all at once` and `leave unstaged` are offered, and the four-mode
        menu applies to multiple items only.
        """
        assert not absent_phrases(
            commit_step,
            "For exactly one selected actionable item, never reopen the grouping question",
            "offer only `all at once` and `leave unstaged`",
            "The single-item default supplies no authorization or explicit local snapshot exception",
            "The four-mode menu below applies only to multiple selected actionable items",
        )
