"""Pin every user ask the research skills had at HEAD before the turn-budget pass, so no question is silently removed.

The pass touched only task-tracking lines and agent-wait wording in these skills. Each row is the first ~90 characters
(cut at a word boundary) of one HEAD line naming ``AskUserQuestion`` — the question's own text or its gate. A removed
or reworded question deletes its marker and fails here; an intentional rewording updates the row together with the
question.

fortify, judge and retro asked nothing at HEAD (their only mention is ``allowed-tools``), so they have no rows.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_PLUGIN = Path(__file__).resolve().parents[1]

#: (skill file relative to the plugin root, HEAD ask-line marker)
_ASKS = [
    pytest.param(
        "skills/plan/SKILL.md",
        "**Fallback path** — ONLY when `PROFILE_AVAILABLE=false`: skip bottleneck selection menu.",
        id="plan-1",
    ),
    pytest.param(
        "skills/plan/SKILL.md",
        'Invoke `AskUserQuestion` — "What would you like to optimize?", options: (a) Overall',
        id="plan-2",
    ),
    pytest.param(
        "skills/plan/SKILL.md",
        "**Scope guard (first action)**: Before scanning, check `<goal>` is optimization goal.",
        id="plan-3",
    ),
    pytest.param(
        "skills/plan/SKILL.md",
        "Dry-run both commands before presenting (add `# timeout: 60000` to timed bash calls —",
        id="plan-4",
    ),
    pytest.param(
        "skills/plan/SKILL.md",
        "Any agent returns `ok: false` → invoke `AskUserQuestion` with the advisor suggestions",
        id="plan-5",
    ),
    pytest.param(
        "skills/plan/SKILL.md",
        "All advisors return `ok: true` AND `OUTPUT_EXISTS`: invoke `AskUserQuestion` — (a)",
        id="plan-6",
    ),
    pytest.param(
        "skills/run/SKILL.md",
        "Print next-step suggestions as plain text — do NOT call `AskUserQuestion`: both",
        id="run-1",
    ),
    pytest.param(
        "skills/topic/SKILL.md",
        "- This task exists because a sibling skill (oss:review) had an incident: report written",
        id="topic-1",
    ),
    pytest.param(
        "skills/topic/SKILL.md",
        "**Mandatory termination gate**: after `modes/team.md` returns (consolidation complete,",
        id="topic-2",
    ),
    pytest.param(
        "skills/topic/SKILL.md",
        "**Mandatory termination gate**: after `modes/plan.md` returns (phased plan emitted,",
        id="topic-3",
    ),
    pytest.param(
        "skills/topic/SKILL.md",
        '**Hard gate**: check "Print report header" task status before anything else here. Not',
        id="topic-4",
    ),
    pytest.param(
        "skills/topic/SKILL.md",
        "Call `AskUserQuestion` tool — do NOT write options as plain text first. Map options",
        id="topic-5",
    ),
    pytest.param(
        "skills/verify/SKILL.md",
        "**Stage the partial report to `$RUN_DIR/partial-report.md`, never to `$OUT`** — a",
        id="verify-1",
    ),
    pytest.param(
        "skills/verify/SKILL.md",
        'python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/write_skill_contract.py"',
        id="verify-2",
    ),
    pytest.param(
        "skills/verify/SKILL.md",
        "The `/compact` hint rides in the question text, never as reply prose before the call",
        id="verify-3",
    ),
    pytest.param(
        "skills/verify/SKILL.md", "Invoke `AskUserQuestion` — do NOT write options as plain text.", id="verify-4"
    ),
    pytest.param(
        "skills/verify/SKILL.md",
        "**On (a)**: (a) branch runs after an `AskUserQuestion` turn boundary, i.e. fresh Bash",
        id="verify-5",
    ),
    pytest.param(
        "skills/verify/SKILL.md",
        "Call `AskUserQuestion` tool after V6 output — do NOT write options as plain text. Before",
        id="verify-6",
    ),
    pytest.param(
        "skills/kaggle/SKILL.md",
        "**Flag mutual-exclusion check** — if `EDA_ONLY` and `INFERENCE_ONLY` are both `true`",
        id="kaggle-1",
    ),
    pytest.param(
        "skills/kaggle/SKILL.md",
        "**Unsupported flag check** — scan `$ARGUMENTS` for remaining `--<token>` tokens after",
        id="kaggle-2",
    ),
    pytest.param(
        "skills/kaggle/SKILL.md",
        "| `absent` | Offer install — `AskUserQuestion`: (a) skip, ground from URL/user facts ·",
        id="kaggle-3",
    ),
    pytest.param(
        "skills/kaggle/SKILL.md",
        "| `unauthorized` | `AskUserQuestion` with the credential instructions below as the",
        id="kaggle-4",
    ),
    pytest.param(
        "skills/kaggle/SKILL.md",
        "**Full-data gate** — never pull the whole archive unprompted; competition data reaches",
        id="kaggle-5",
    ),
    pytest.param(
        "skills/kaggle/SKILL.md",
        "Invoke `AskUserQuestion` with up to 4 questions covering all unknown required facts.",
        id="kaggle-6",
    ),
    pytest.param("skills/kaggle/SKILL.md", "Invoke `AskUserQuestion` as follow-up gate:", id="kaggle-7"),
    pytest.param("skills/kaggle/SKILL.md", "Invoke `AskUserQuestion`:", id="kaggle-8"),
]


@pytest.mark.parametrize(("relative", "marker"), _ASKS)
def test_baseline_ask_line_still_present(relative: str, marker: str) -> None:
    """Every ask line the skill carried at HEAD is still present verbatim."""
    assert marker in (_PLUGIN / relative).read_text(encoding="utf-8")
