"""Guard that what a resolve question is about reaches the user inside that question.

ACTION_ITEMS tables appear exactly once, in the picker preview; no other must-see content is written as reply text right
before a question, where 5.5-family models can turn it into an empty progress update.
"""

import re
from pathlib import Path

import pytest

_SKILL = Path(__file__).resolve().parents[2] / "skills/resolve/SKILL.md"
_RESOLVE_DIR = _SKILL.parent
_PR_INTELLIGENCE = _SKILL.parent / "modes/pr-intelligence.md"
_REPORT_INTELLIGENCE = _SKILL.parent / "modes/report-intelligence.md"

#: An instruction to place content in reply text, or right before a question, instead of in the question call.
_REPLY_BEFORE_ASK = re.compile(
    r"\b(?:[Pp]rint|[Pp]ut|[Rr]eport)\b(?P<what>[^\n]{0,200}?)"
    r"(?:\bin (?:a|an|the) (?:\*\*)?(?:assistant )?(?:user-facing )?reply\b|\bas reply text in the message\b"
    r"|\bas the last text of the message\b|\bright before the picker\b|\bbefore Call 1\b|\bbefore the call\b"
    r"|;\s*`AskUserQuestion`)"
)
#: Objects that only point at a payload quoted right after the instruction ("print this line in the reply: `...`").
_POINTERS = ("this line", "one line")
#: Reply-text payloads that may stay, each with the reason; every other match is must-see content that can vanish.
#: None today: the large-selection notice rides the >=19 call's question text, and its Step 11 copy is a report line.
_ALLOWED_REPLY_LINES: dict[str, str] = {}


def _unlisted_reply_instructions() -> list[tuple[str, str]]:
    """Return ``(file, instruction)`` for each reply-text-before-question instruction whose payload is not allowed.

    The payload is the instruction's own object, or, when that object only points ("this line"), the text after the
    placement phrase on the same line.
    """
    found = []
    for path in sorted(_RESOLVE_DIR.rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        for match in _REPLY_BEFORE_ASK.finditer(text):
            what = match.group("what").strip(" *")
            payload = text[match.end() :].split("\n", 1)[0][:200] if what in _POINTERS else what
            if not any(marker in payload for marker in _ALLOWED_REPLY_LINES):
                found.append((path.relative_to(_RESOLVE_DIR).as_posix(), match.group(0)))
    return found


def _route(text: str, start: str, end: str) -> str:
    """Slice ``text`` from the first ``start`` marker up to the first ``end`` marker after it."""
    begin = text.index(start)
    return text[begin : text.index(end, begin)]


class TestActionItemsPrintContract:
    @pytest.mark.parametrize(
        ("source", "start", "end"),
        [
            pytest.param(_PR_INTELLIGENCE, "Read `$IMPL_DIR/pr-intelligence.md`", "Later steps read per-item", id="pr"),
            pytest.param(
                _REPORT_INTELLIGENCE,
                "Render ACTION_ITEMS as a markdown table",
                "PR# found in report header",
                id="report",
            ),
            pytest.param(
                _SKILL, "**MANDATORY — render merged ACTION_ITEMS", "## Step 3d: User item selection", id="merged"
            ),
            pytest.param(
                _SKILL,
                "**≥19 pending items — context-budget mode**",
                "<!-- branch: main-path — commit-mode",
                id="context-budget",
            ),
        ],
    )
    def test_every_selection_route_shows_the_table_only_in_the_picker_preview(
        self, source: Path, start: str, end: str
    ) -> None:
        """Each route puts the table in the bulk options' preview and forbids a reply-text or tool-stdout copy.

        On 5.5-family models reply text before a tool call can come back as an empty progress update, and a second copy
        beside the preview would show the table twice; tool stdout never reaches the user at all.
        """
        route = _route(source.read_text(encoding="utf-8"), start, end)
        assert "`preview` of every bulk-action option" in route
        assert "never also as reply text" in route
        assert "Bash/tool stdout" in route

    def test_table_is_shown_once_per_selection_call(self) -> None:
        """The selection table appears once per call, in the preview, and no rule asks for a reply-text copy.

        A resolve run printed the table, then reprinted it when the selection hook denied the picker; a later
        5.5-family run printed it three times while the user saw none of them. Neither a reprint nor a retry of the
        identical call may come back as guidance.
        """
        skill = _SKILL.read_text(encoding="utf-8")
        picker = skill[skill.index("## Step 3d: User item selection") : skill.index("**Cap mechanics")]
        assert "exactly once, as the `preview` of every bulk-action option" in skill
        assert (
            "**Show the table exactly once per selection call — in the picker preview, never as reply text**" in picker
        )
        assert "latest assistant user-facing reply contains every ACTION_ITEMS row" not in skill
        assert "repeat that table in the reply now" not in skill
        assert "re-issue the identical call once" not in skill
        assert "visible reply chars" not in skill

    def test_every_selection_call_carries_the_table_in_bulk_previews(self) -> None:
        """Each bulk-action option carries the full table as its preview, and a denial names that fix.

        On 5.5-family models reply text before a tool call can come back as an empty progress update: a real run printed
        a 9-row table three times and the user saw none of it. The picker's own preview is the copy that renders, so the
        rule must cover every call carrying the bulk question, including Call 2 and the >=19 call.
        """
        skill = _SKILL.read_text(encoding="utf-8")
        picker = skill[skill.index("## Step 3d: User item selection") : skill.index("**Cap mechanics")]
        bulk = skill[skill.index("**Bulk action — hard rule**") : skill.index("**Bulk-action resolution**")]
        call_two = skill[skill.index("- **Call 2** = ") : skill.index("- A Call 1 bulk answer that resolves to")]
        large = skill[
            skill.index("**≥19 pending items — context-budget mode**") : skill.index(
                "<!-- branch: main-path — commit-mode"
            )
        ]
        assert "**Table in the picker preview — MANDATORY, the gate's source of truth**" in picker
        assert "every pending row plus closed rows" in picker
        assert "empty progress update" in picker
        assert "put the table in the bulk-option preview" in picker
        assert "`preview` field on each of (a)-(d) carries the full ACTION_ITEMS table" in bulk
        assert "full-table `preview` on every option" in call_two
        assert "each Q1 option's `preview` = that compressed table" in large

    def test_table_columns_are_fixed(self) -> None:
        """The selection table forbids improvised columns such as File or Sev."""
        skill = _SKILL.read_text(encoding="utf-8")
        intelligence = _PR_INTELLIGENCE.read_text(encoding="utf-8")
        assert "never add `File`, `Sev`, `Loc` or any other column" in skill
        assert "exactly these; never add File/Sev/Loc" in intelligence

    def test_step_3c_marks_the_table_render_mandatory(self) -> None:
        """Step 3c's merged-table render is an unconditional imperative, not a suggestion.

        A prior resolve run substituted a prose pending-item count for the table and still wrote "table above" as if it
        had printed one. The render instruction must be MANDATORY and must forbid every compression style, including
        caveman, from replacing the table with prose.
        """
        text = _SKILL.read_text(encoding="utf-8")
        assert "MANDATORY — render merged ACTION_ITEMS as markdown table" in text
        assert "not a decorative table" in text
        assert "never replace it with a prose count or summary line" in text
        step = text[text.index("### Sources confirmation") : text.index("## Step 3d: User item selection")]
        assert step.index("Merge summary — heads the table in the picker preview:") < step.index(
            "MANDATORY — render merged ACTION_ITEMS"
        )
        assert step.index("MANDATORY — render merged ACTION_ITEMS") < step.index(
            "| # | Type | Change | Severity | Author | Status | Summary | Notes |"
        )

    def test_context_budget_branch_renders_before_asking(self) -> None:
        """The >=19-item branch must render its compressed table before building the AskUserQuestion call.

        The original wording buried "print compressed table" as a clause trailing "skip per-item checkboxes", so the
        table silently dropped under context or style pressure while the picker still fired. Ordering must be explicit.
        """
        text = _SKILL.read_text(encoding="utf-8")
        idx = text.index("pending items — context-budget mode**:")
        section = text[idx : idx + 900]
        assert "render first, ask second" in section
        assert section.index("(1) render the compressed table") < section.index("(2) then issue ONE call")

    def test_table_above_phrase_is_gated_on_an_actual_table(self) -> None:
        """The skill states its own guard against claiming a table that was never printed."""
        text = _SKILL.read_text(encoding="utf-8")
        assert re.search(
            r'references "the table above" without the table .* is a defect',
            text,
        )


class TestNoReplyTextBeforeAQuestion:
    def test_no_must_see_content_is_printed_as_reply_text_before_a_question(self) -> None:
        """No resolve instruction writes what a question is about as reply text right before asking it.

        On 5.5-family models reply text written before a tool call can come back as an empty progress update: a Custom
        dispatch gate showed no groups, a closed-only picker lost its only usage hint, a timeout gate asked about bare
        ids, and every ``/compact`` hint before an idle gate vanished. Only the listed lines may remain, each with its
        reason.
        """
        assert _unlisted_reply_instructions() == []

    @pytest.mark.parametrize(
        ("relative", "start", "end", "marker"),
        [
            pytest.param(
                "SKILL.md",
                "### Sources confirmation",
                "Merge summary — heads the table",
                "head the bulk-option `preview` of Step 3d's picker",
                id="merged-sources-in-preview",
            ),
            pytest.param(
                "modes/pr-intelligence.md",
                "Read `$IMPL_DIR/pr-intelligence.md`",
                "Later steps read per-item",
                "Sources block + motivation at the top of the `preview` of every bulk-action option",
                id="pr-sources-in-preview",
            ),
            pytest.param(
                "modes/report-intelligence.md",
                "### Report header state",
                "<!-- loads: review-section-taxonomy.md -->",
                "Sources block — it heads the `preview` of every bulk-action option of Step 3d's AskUserQuestion",
                id="report-sources-in-preview",
            ),
            pytest.param(
                "SKILL.md",
                "Pending items = ACTION_ITEMS",
                "- **Zero pending, closed items present**",
                "the bulk question's text says so in every selection call (**Bulk question text** below)",
                id="closed-items-hint-in-question-text",
            ),
            pytest.param(
                "SKILL.md",
                "**Bulk question text**",
                "**ESSENTIAL — exactly these 4 options",
                "3. `→ N resolved/addressed items not in bulk options — type their ids to include`",
                id="bulk-text-closed-hint",
            ),
            pytest.param(
                "SKILL.md",
                "**Bulk question text**",
                "**ESSENTIAL — exactly these 4 options",
                "2. `→ N pending items — selecting in 2 calls; a bulk choice here ends selection after this call`"
                " — Call 1 of the two-call layout",
                id="bulk-text-two-call-notice",
            ),
            pytest.param(
                "SKILL.md",
                "**Bulk question text**",
                "**ESSENTIAL — exactly these 4 options",
                "1. `→ To pick items yourself, leave this unanswered and tick them on the next tabs.",
                id="bulk-text-cherry-pick-usage",
            ),
            pytest.param(
                "SKILL.md",
                "**Bulk question text**",
                "**ESSENTIAL — exactly these 4 options",
                "5. `` Long wait? `/compact` now — state persisted in <IMPL_DIR>, resume lossless. `` — Call 1 only",
                id="bulk-text-compact-hint",
            ),
            pytest.param(
                "SKILL.md",
                "**Bulk question text**",
                "**ESSENTIAL — exactly these 4 options",
                "4. `→ N pending items — selecting more than 50 runs them all in this pass",
                id="bulk-text-long-run-notice",
            ),
            pytest.param(
                "SKILL.md",
                "Longest idle window of the run sits here",
                "**Show the table exactly once per selection call",
                "goes into the `question` text of Step 3d's first question",
                id="step-3d-compact-hint-in-question-text",
            ),
            pytest.param(
                "SKILL.md",
                "**Push confirmation — one `AskUserQuestion` call.**",
                "Options:",
                "- Last line: `` Long wait? `/compact` now — commits landed",
                id="push-compact-hint-in-question-text",
            ),
            pytest.param(
                "modes/action-item-dispatch.md",
                "**Group-preview gate",
                "Derive `<group_tag>`",
                "2. The question text closes with this line",
                id="custom-gate-compact-hint-in-question-text",
            ),
            pytest.param(
                "modes/action-item-dispatch.md",
                "**Challenge double-timeout gate**",
                "Drop block",
                '\nLong wait? `/compact` now — state persisted in <IMPL_DIR>, resume lossless."',
                id="timeout-gate-compact-hint-in-question-text",
            ),
            pytest.param(
                "modes/action-item-dispatch.md",
                "**Group-preview gate",
                "Derive `<group_tag>`",
                "every option's `preview` = the step 1 table",
                id="custom-groups-in-preview",
            ),
            pytest.param(
                "modes/action-item-dispatch.md",
                "**Challenge double-timeout gate**",
                "Drop block",
                "The items go in the question text itself",
                id="timed-out-items-in-question-text",
            ),
            pytest.param(
                "modes/conflict-resolution.md",
                "More than 20 conflicted files",
                "## Step 6",
                "`AskUserQuestion` whose question text itself carries the count and every conflicted path",
                id="conflict-list-in-question-text",
            ),
        ],
    )
    def test_must_see_content_rides_in_the_question_call(
        self, relative: str, start: str, end: str, marker: str
    ) -> None:
        """Each gate's must-see content sits in a field the question renders: its text or its options' preview."""
        text = (_RESOLVE_DIR / relative).read_text(encoding="utf-8")
        section = text[text.index(start) : text.index(end, text.index(start))]
        assert marker in section
