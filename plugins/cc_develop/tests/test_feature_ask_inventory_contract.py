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
        "- **Blockers found** → STOP. Invoke `AskUserQuestion` with the blocker findings",
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


@pytest.mark.parametrize(("relative", "marker"), _ASKS)
def test_baseline_ask_line_still_present(relative: str, marker: str) -> None:
    """Every ask line the skill carried at HEAD is still present verbatim."""
    assert marker in (_PLUGIN / relative).read_text(encoding="utf-8")
