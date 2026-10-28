"""Pin every user ask /develop:feature had at HEAD before the turn-budget pass, so no question is silently removed.

The pass touched only task-tracking lines and agent-wait wording in these skills. Each row is the first ~90 characters
(cut at a word boundary) of one HEAD line naming ``AskUserQuestion`` — the question's own text or its gate. A removed or
reworded question deletes its marker and fails here; an intentional rewording updates the row together with the
question.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_PLUGIN = Path(__file__).resolve().parents[1]

#: (skill file relative to the plugin root, HEAD ask-line marker)
_ASKS = [
    pytest.param(
        "skills/feature/SKILL.md",
        'If `MULTI_LANG=true`: invoke `AskUserQuestion` — "Monorepo detected (Python + non-Python',
        id="feature-1",
    ),
    pytest.param(
        "skills/feature/SKILL.md",
        "**Unsupported flag check** — after ALL supported flags extracted (including `--issue`",
        id="feature-2",
    ),
    pytest.param(
        "skills/feature/SKILL.md",
        "Plan-inline skipped because `--plan` was supplied or `ACCEPT_NO_PLAN=true`, **and**",
        id="feature-3",
    ),
    pytest.param(
        "skills/feature/SKILL.md",
        "**Goal classification gate**: after sw-engineer analysis completes, scan the goal text",
        id="feature-4",
    ),
    pytest.param(
        "skills/feature/SKILL.md",
        "- **Blockers found** → STOP. Write the blocker findings",
        id="feature-5",
    ),
    pytest.param(
        "skills/feature/SKILL.md",
        "If `COLLECT_EXIT -ne 0` and `DEMO_SCRIPT` is empty (doctest form): stop — collection",
        id="feature-6",
    ),
    pytest.param(
        "skills/feature/SKILL.md",
        "After each cycle, refresh compaction contract so a mid-loop compaction resumes TDD loop",
        id="feature-7",
    ),
]


# Feasible authorized blockers no longer force a user question; protected and unresolved stops retain evidence delivery.
_EQUIVALENTS = {
    # The proportional lifecycle replaces this routine scope prompt with parent acceptance; actual decisions still stop.
    "Plan-inline skipped because `--plan` was supplied or `ACCEPT_NO_PLAN=true`, **and**": (
        "Plan-inline skipped because `--plan` was supplied or `ACCEPT_NO_PLAN=true`, and complexity classified `large`",
        "retain the medium/large plan challenge and parent acceptance.",
        "A supplied artifact or its waiver alone creates no user approval prompt.",
        "Stop only for an actual unresolved scope decision, protected decision or missing authority.",
        "For medium/large work, reuse a still-valid independent challenge of that exact plan and scope; otherwise run this gate once.",
        "Approve the challenged plan within existing authority before assigning bounded execution",
    ),
    "- **Blockers found** → STOP. Write the blocker findings": (
        "Resolve feasible authorized blockers without asking",
        "Stop for a protected decision or authority, an explicit user wait, or a blocker that remains unresolved.",
        "Write the blocker findings (one line each: claim · file:line · evidence) to `$DEV_DIR/blockers.md` first",
        "print them once as the last reply text before invoking `AskUserQuestion`",
        "The question text carries the blocker count, headline and `→ $DEV_DIR/blockers.md`",
    ),
}


@pytest.mark.parametrize(("relative", "marker"), _ASKS)
def test_baseline_ask_line_still_present(relative: str, marker: str) -> None:
    """Retain each historical ask or its authorized equivalent, including evidence delivery."""
    text = (_PLUGIN / relative).read_text(encoding="utf-8")
    assert all(item in text for item in _EQUIVALENTS.get(marker, (marker,)))
