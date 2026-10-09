"""Pin every user ask the touched foundry skills had at the committed baseline, so no question is silently removed.

The efficiency rollout batches tool calls and replaces agent polling with per-agent deadlines in ``investigate``,
``manage``, ``calibrate``, ``audit``, ``brainstorm``, ``create`` and ``distill``. None of that may remove a user
decision. Each row below is one question the committed baseline asked, located by the section that asks it and a marker
that only exists while the question is still asked there. Removing a question, or replacing it with a silent default,
deletes its marker and fails here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_SKILLS = Path(__file__).resolve().parents[2] / "skills"
_FLAG = ("**Unsupported flag check**", "On Abort: stop.", "**Continue ignoring**")

#: (skill-relative file, section start, section end, marker that only exists while the question is still asked there)
_ASKS = [
    pytest.param(
        "investigate/SKILL.md",
        "<inputs>",
        "</inputs>",
        "What exactly is failing or behaving unexpectedly?",
        id="investigate-vague-symptom",
    ),
    pytest.param("investigate/SKILL.md", *_FLAG, id="investigate-unsupported-flag"),
    pytest.param(
        "investigate/SKILL.md",
        "## Step 6",
        "</workflow>",
        "(b) Run additional investigation with narrowed hypothesis",
        id="investigate-follow-up-gate",
    ),
    pytest.param(
        "manage/SKILL.md",
        "**Update/delete mode**",
        "**Update second-argument discrimination**",
        "Multiple matches → `AskUserQuestion`: (a) agent, (b) skill, (c) rule",
        id="manage-type-ambiguity",
    ),
    pytest.param("manage/SKILL.md", *_FLAG, id="manage-unsupported-flag"),
    pytest.param(
        "manage/SKILL.md",
        "- Multiple non-empty results:",
        "**Delete confirmation gate**",
        "Multiple entities named `<name>` found. Which one?",
        id="manage-entity-ambiguity",
    ),
    pytest.param(
        "manage/SKILL.md",
        "**Delete confirmation gate**",
        "**Rename confirmation gate**",
        "This cannot be undone. (a) Confirm · (b) Abort",
        id="manage-delete-confirm",
    ),
    pytest.param(
        "manage/SKILL.md",
        "**Rename confirmation gate**",
        "Place (b) second",
        "rename the file, or apply `<second-arg>` as an edit directive?",
        id="manage-rename-confirm",
    ),
    pytest.param(
        "manage/SKILL.md",
        "## Step 2: Overlap review",
        "## Step 3",
        '"Extend existing (Recommended)" / "Proceed" / "Abort"',
        id="manage-overlap",
    ),
    pytest.param(
        "manage/SKILL.md",
        "**Challenger review gate**",
        "**Cycle guard**",
        "Run foundry:challenger to adversarially review the changes just made?",
        id="manage-challenger-gate",
    ),
    pytest.param(
        "manage/modes/rename-validation.md",
        "**Large hit set gate**",
        "Hits within limit",
        "Proceed with classification or abort?",
        id="manage-rename-large-hit-set",
    ),
    pytest.param(
        "manage/modes/rename-validation.md",
        "Collect ambiguous hits",
        "Apply user-confirmed fixes",
        "Is this a real reference to `<old-name>` that should be updated, or a false positive?",
        id="manage-rename-ambiguous-hits",
    ),
    pytest.param("calibrate/SKILL.md", *_FLAG, id="calibrate-unsupported-flag"),
    pytest.param(
        "calibrate/SKILL.md",
        "When gated (either branch)",
        "Create tasks before proceeding",
        "Proceed?",
        id="calibrate-spawn-gate",
    ),
    pytest.param(
        "calibrate/SKILL.md", "## Step 3", "## Step 4", '"Proposals ready. What next?"', id="calibrate-proposal-gate"
    ),
    pytest.param("audit/SKILL.md", *_FLAG, id="audit-unsupported-flag"),
    pytest.param(
        "audit/SKILL.md",
        "## Follow-up gate",
        "Option slot budget",
        "**Always fires** unless `--skip-gate` passed",
        id="audit-follow-up-gate",
    ),
    pytest.param(
        "audit/SKILL.md",
        "<notes>",
        "Prose acknowledgment in response body",
        "**`! BREAKING` findings require user acknowledgment",
        id="audit-breaking-ack",
    ),
    pytest.param(
        "audit/modes/fix.md",
        '**"Fix ALL" option (c)**',
        "2. **Single integrated fix pass**",
        "1. **Upfront decision collection**",
        id="audit-fix-all-decisions",
    ),
    pytest.param(
        "brainstorm/SKILL.md",
        "## Step 6: Present and gate",
        "</workflow>",
        "Then call `AskUserQuestion` tool",
        id="brainstorm-final-gate",
    ),
    pytest.param(
        "create/SKILL.md",
        "- End with `AskUserQuestion` gate, two options:",
        "\n",
        "(a) **Generate the full artifact now**",
        id="create-generate-gate",
    ),
    pytest.param(
        "distill/modes/prune.md",
        "**P-eager-2**",
        "**P-eager-3**",
        "label: `Specific items`",
        id="distill-prune-eager-pick",
    ),
    pytest.param(
        "distill/modes/prune.md",
        "**P3**",
        "label: `Skip`",
        '"Apply prune edits across all N project memory files?"',
        id="distill-prune-apply",
    ),
    pytest.param(
        "distill/modes/external.md",
        "**E13: Gate — AskUserQuestion**",
        "label: `Skip`",
        '"Apply external source candidates?"',
        id="distill-external-gate",
    ),
    pytest.param(
        "distill/modes/memory.md",
        "Call `AskUserQuestion` tool with the (annotated) proposal table",
        "label: `Skip`",
        '"Apply proposals?"',
        id="distill-memory-gate",
    ),
    pytest.param(
        "distill/modes/executables.md",
        "Then call `AskUserQuestion`",
        "label: `Prose only`",
        '"Extract candidates to bin/ scripts?"',
        id="distill-executables-gate",
    ),
]


@pytest.mark.parametrize(("relative", "section_start", "section_end", "marker"), _ASKS)
def test_baseline_ask_still_happens(relative: str, section_start: str, section_end: str, marker: str) -> None:
    """Every decision asked in the committed baseline is still asked, in its section, through the AskUserQuestion tool.

    The section must name the tool itself, so a prose mention of the question cannot stand in for a real ask.
    """
    text = (_SKILLS / relative).read_text(encoding="utf-8")
    start = text.index(section_start)
    section = text[start : text.index(section_end, start + len(section_start))]
    assert marker in section
    assert "AskUserQuestion" in section


@pytest.mark.parametrize(
    "relative",
    ["investigate/SKILL.md", "manage/SKILL.md", "calibrate/SKILL.md", "audit/SKILL.md"],
)
def test_timeouts_never_answer_a_question(relative: str) -> None:
    """Every skill that arms agent deadlines says a timed-out agent never answers, skips or defaults a user decision."""
    text = (_SKILLS / relative).read_text(encoding="utf-8")
    assert "agent_watch.py" in text
    assert "never answers" in text or "never wait further" in text


@pytest.mark.parametrize(
    "relative", ["../rules/task-lifecycle.md", "_shared/agent-spawn-protocol.md", "../CLAUDE.src.md"]
)
def test_shared_rules_ban_waiting_tools(relative: str) -> None:
    """The shared spawn rules forbid every waiting tool and no longer permit a liveness probe or a Monitor call.

    Orchestrators followed the old "one Monitor call or find -newer probe per turn" allowance into ScheduleWakeup and
    ListAgents loops; the per-agent deadline file replaces all of them.
    """
    text = (_SKILLS / relative).read_text(encoding="utf-8")
    assert all(tool in text for tool in ("ScheduleWakeup", "ListAgents", "Monitor"))
    assert "agent-watch-<batch>.tsv" in text
    assert "one bounded `Monitor` call" not in text
    assert "Optional between-turn liveness" not in text


def test_apply_flags_keep_skipping_only_the_gates_they_skipped_at_baseline() -> None:
    """``--apply`` still skips only the calibrate proposal gate, never the large fan-out spawn gate."""
    text = (_SKILLS / "calibrate/SKILL.md").read_text(encoding="utf-8")
    assert "`--apply` only skips Step 3 proposal-review gate, not this one" in text
