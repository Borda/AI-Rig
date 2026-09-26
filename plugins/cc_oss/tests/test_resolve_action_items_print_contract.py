"""Guard the resolve skill's mandate that ACTION_ITEMS tables always print."""

import re
from pathlib import Path

_SKILL = Path(__file__).resolve().parents[1] / "skills/resolve/SKILL.md"
_PR_INTELLIGENCE = _SKILL.parent / "modes/pr-intelligence.md"


class TestActionItemsPrintContract:
    def test_every_selection_route_uses_a_user_facing_reply_before_picker(self) -> None:
        """The table must reach the user rather than disappear into tool stdout."""
        skill = _SKILL.read_text(encoding="utf-8")
        intelligence = _PR_INTELLIGENCE.read_text(encoding="utf-8")
        pure_pr = intelligence[
            intelligence.index("Read `$IMPL_DIR/pr-intelligence.md`") : intelligence.index("Later steps read per-item")
        ]
        merged = skill[
            skill.index("**MANDATORY — print merged ACTION_ITEMS") : skill.index("## Step 3d: User item selection")
        ]
        large = skill[
            skill.index("**≥19 pending items — context-budget mode**") : skill.index(
                "<!-- branch: main-path — commit-mode"
            )
        ]
        for route in (pure_pr, merged, large):
            assert "assistant user-facing reply" in route
            assert "not Bash/tool stdout" in route
            assert route.index("assistant user-facing reply") < route.index("AskUserQuestion")
        picker = skill[skill.index("## Step 3d: User item selection") : skill.index("**Cap mechanics")]
        assert "latest assistant user-facing reply contains every ACTION_ITEMS row" in picker
        assert "repeat that table in the reply now" in picker

    def test_step_3c_marks_the_table_print_mandatory(self) -> None:
        """Step 3c's merged-table print is an unconditional imperative, not a suggestion.

        A prior resolve run substituted a prose pending-item count for the table and still wrote "table above" as if it
        had printed one. The print instruction must be MANDATORY and must forbid every compression style, including
        caveman, from replacing the table with prose.
        """
        text = _SKILL.read_text(encoding="utf-8")
        assert "MANDATORY — print merged ACTION_ITEMS as markdown table" in text
        assert "not a decorative table" in text
        assert "never replace it with a prose count or summary line" in text
        step = text[text.index("### Sources confirmation") : text.index("## Step 3d: User item selection")]
        assert step.index("Print merge summary before table:") < step.index("MANDATORY — print merged ACTION_ITEMS")
        assert step.index("MANDATORY — print merged ACTION_ITEMS") < step.index(
            "| # | Type | Change | Severity | Author | Status | Summary | Notes |"
        )

    def test_context_budget_branch_prints_before_asking(self) -> None:
        """The >=19-item branch must print its compressed table before the AskUserQuestion call.

        The original wording buried "print compressed table" as a clause trailing "skip per-item checkboxes", so the
        print silently dropped under context or style pressure while the picker still fired. Ordering must be explicit.
        """
        text = _SKILL.read_text(encoding="utf-8")
        idx = text.index("pending items — context-budget mode**:")
        section = text[idx : idx + 900]
        assert "print first, ask second" in section
        assert section.index("(1) print the compressed table") < section.index("(2) then issue ONE call")

    def test_table_above_phrase_is_gated_on_an_actual_table(self) -> None:
        """The skill states its own guard against claiming a table that was never printed."""
        text = _SKILL.read_text(encoding="utf-8")
        assert re.search(
            r'references "the table above" without the table .* is a defect',
            text,
        )
