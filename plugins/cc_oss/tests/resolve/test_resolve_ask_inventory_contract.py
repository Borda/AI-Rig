"""Pin every user ask the resolve flow had before its gates moved up front, so no question is silently removed.

Each row is one decision the user made in the committed baseline flow, mapped to where that same decision is asked now —
kept in place, moved to the Step 3d gate, or kept as recovery. Removing a question, or replacing it with a silent
default, deletes its marker and fails here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_RESOLVE = Path(__file__).resolve().parents[2] / "skills/resolve"
_SHARED = _RESOLVE.parent / "_shared"
_SELECTION = ("## Step 3d", "## Step 7b join")  # opens with "invoke `AskUserQuestion` tool directly"

#: (file, section start, section end, marker that only exists while the question is still asked there)
_ASKS = [
    pytest.param(
        "SKILL.md",
        "**Unsupported flag check**",
        "### Reject-gate check",
        "**Continue ignoring**",
        id="unsupported-flag-kept",
    ),
    pytest.param(
        "SKILL.md",
        "### Report source resolution",
        "### Create all workflow tasks upfront",
        "No readable review report for PR #<N>",
        id="report-missing-kept",
    ),
    pytest.param("SKILL.md", *_SELECTION, '"Or choose a bulk action:"', id="bulk-action-kept"),
    pytest.param("SKILL.md", *_SELECTION, 'header "Items to implement:"', id="item-checkboxes-kept"),
    pytest.param("SKILL.md", *_SELECTION, 'AskUserQuestion: "Commit mode for selected items:"', id="commit-mode-kept"),
    pytest.param("SKILL.md", *_SELECTION, "\"If 'By topic group' — how should items group?\"", id="topic-group-kept"),
    pytest.param(
        "SKILL.md",
        *_SELECTION,
        '"Phase 2 runs specialists in isolated worktrees. How should the work spread?"',
        id="dispatch-kept",
    ),
    pytest.param("SKILL.md", *_SELECTION, "It records **push intent only**", id="push-intent-at-3d"),
    pytest.param(
        "SKILL.md",
        *_SELECTION,
        "(a) Push (confirm at Step 10), then open the PR in the browser",
        id="post-pr-moved-into-push-intent",
    ),
    pytest.param("SKILL.md", *_SELECTION, "Assign a topic label to each implemented item", id="labels-moved-to-3d"),
    pytest.param("SKILL.md", "**Straggler gate", "## Step 9", "No confirming commit found", id="straggler-kept"),
    pytest.param(
        "SKILL.md",
        "## Step 10: Push",
        "## Step 11",
        "Diff stat: `$PUSH_STAT`",
        id="push-confirmation-with-scope-at-step-10",
    ),
    pytest.param(
        "SKILL.md",
        "## Step 10: Push",
        "## Step 11",
        "Q2 — `unset` intent only — after the final report: (a) **Open PR in browser**",
        id="post-pr-asked-at-step-10-without-intent",
    ),
    pytest.param(
        "modes/action-item-dispatch.md",
        "**`GROUP_STRATEGY=labels` only**",
        "`auto` mapping",
        "Assign a topic label to each implemented item",
        id="labels-still-asked-at-step-8-when-lost",
    ),
    pytest.param(
        "modes/action-item-dispatch.md",
        "**Group-preview gate",
        "Derive `<group_tag>`",
        '"Phase 2 groups are formed. How should they run?"',
        id="group-preview-kept",
    ),
    pytest.param(
        "modes/action-item-dispatch.md",
        "**Challenge double-timeout gate**",
        "Drop block",
        "The challenge for <N> item(s) timed out twice",
        id="missing-challenge-verdict-asks-instead-of-blocking",
    ),
    pytest.param(
        "modes/conflict-resolution.md",
        "More than 20 conflicted files",
        "## Step 6",
        "Retry with base only",
        id="too-many-conflicts-kept",
    ),
]


@pytest.mark.parametrize(("relative", "section_start", "section_end", "marker"), _ASKS)
def test_baseline_ask_still_happens(relative: str, section_start: str, section_end: str, marker: str) -> None:
    """Every decision asked in the baseline flow is still asked — in place, moved earlier, or as recovery.

    Moving a question is allowed only with the same decision and the same information; the marker is that question's own
    text, and its section must name the AskUserQuestion tool so a prose mention cannot stand in for a real ask.
    """
    text = (_RESOLVE / relative).read_text(encoding="utf-8")
    start = text.index(section_start)
    section = text[start : text.index(section_end, start)]
    assert marker in section
    assert "AskUserQuestion" in section


def test_codemap_gate_still_asks() -> None:
    """The codemap index gate is delegated to the shared contract, which must still ask before building."""
    skill = (_RESOLVE / "SKILL.md").read_text(encoding="utf-8")
    gates = (_SHARED / "codemap-gates.md").read_text(encoding="utf-8")
    assert 'cat "$_OSS_SHARED/codemap-gates.md"' in skill
    assert "Gate A always asks (`AskUserQuestion`)" in gates


@pytest.mark.parametrize(
    ("band", "questions", "follow_up"),
    [
        pytest.param(
            "0, closed items present",
            "Q1 bulk · Q2 commit-mode · Q3 topic-group · Q4 dispatch",
            "Q1 push, only when a PR number exists",
            id="closed-only-keeps-explicit-selection-and-implementation-decisions",
        ),
        pytest.param(
            "0, no closed items",
            "Q1 push, only when a PR number exists; otherwise no call",
            "None",
            id="empty-list-keeps-push-only-without-item-questions",
        ),
    ],
)
def test_zero_pending_routes_preserve_the_available_decisions(band: str, questions: str, follow_up: str) -> None:
    """Closed-only lists retain an ID-entry gate; truly empty lists retain the merge push decision.

    This checks the shipped slot contract, not whether a model follows it in a live session. A generic zero-pending
    bypass or item-checkbox slot would lose the closed-ID entry route or silently reopen history.
    """
    skill = (_RESOLVE / "SKILL.md").read_text(encoding="utf-8")
    table = skill[skill.index("| Pending | Call 1 slots |") : skill.index("Checkbox mode holds")]
    rows = {
        cells[0]: cells[1:]
        for line in table.splitlines()
        if line.startswith("|")
        for cells in [[cell.strip() for cell in line.strip("|").split("|")]]
    }
    assert rows[band] == [questions, follow_up]
    assert "\n\n- **Zero pending, closed items present**" in skill
    assert "\n- **Zero pending, no closed items**" in skill


@pytest.mark.parametrize(
    ("fallback", "forbidden"),
    [
        pytest.param(
            "Unanswered or dismissed → run **no** push block and **no** post-PR block",
            "Unanswered → `skip` for both",
            id="unanswered-push-is-asked-later-not-skipped",
        ),
        pytest.param(
            "Skipped (empty response or blank) → fall back to `each`",
            "grouping by change domain",
            id="skipped-labels-fall-back-to-each",
        ),
    ],
)
def test_no_silent_default_replaces_an_answer(fallback: str, forbidden: str) -> None:
    """A question left unanswered keeps the baseline outcome; it is never replaced by a new silent default."""
    skill = (_RESOLVE / "SKILL.md").read_text(encoding="utf-8")
    assert fallback in skill
    assert forbidden not in skill


def test_timeouts_never_answer_a_question() -> None:
    """The no-polling rule only informs: a timed-out agent never skips or defaults a user decision."""
    skill = (_RESOLVE / "SKILL.md").read_text(encoding="utf-8")
    rule = skill[skill.index("## Agent wait discipline") : skill.index("## State checks")]
    assert "A ⏱ only informs. It never answers, skips, or defaults a user question" in rule


@pytest.mark.parametrize(
    "option",
    [
        "(a) Implement unchallenged — ⏱ noted in the Challenge Log and the final report",
        "(b) Drop them — record as skipped, not implemented",
        "(c) Retry the challenge once more",
        "(d) Stop the run before implementation",
    ],
)
def test_challenge_double_timeout_leaves_the_decision_to_the_user(option: str) -> None:
    """A challenge that times out twice asks the user once per wave; no option is ever applied by default.

    The baseline stopped the run on a missing verdict, so the decision was the user's; the gate keeps it the user's.
    """
    dispatch = (_RESOLVE / "modes/action-item-dispatch.md").read_text(encoding="utf-8")
    gate = dispatch[dispatch.index("**Challenge double-timeout gate**") : dispatch.index("Drop block")]
    assert option in gate
    assert "ask **once** for all of them" in gate
    assert "(d) or unanswered → stop" in gate
