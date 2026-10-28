"""Guard that what a resolve question is about reaches the user inside that question.

Q1 lists items as plain lines. The full saved table is the last reply text before each selection call, printed once.
Hints stay in question text so they remain visible when the host hides pre-call progress.
"""

import re
from pathlib import Path

import pytest

_SKILL = Path(__file__).resolve().parents[2] / "skills/resolve/SKILL.md"
_RESOLVE_DIR = _SKILL.parent

_DISPATCH_PARTS = ("prelude", "phase1", "phase2", "phase3")


def _dispatch_text() -> str:
    """Join Step 8's dispatch files in load order: prelude, Phase 1, Phase 2, Phase 3."""
    return "\n".join(
        (_RESOLVE_DIR / f"modes/dispatch-{part}.md").read_text(encoding="utf-8") for part in _DISPATCH_PARTS
    )


_PR_INTELLIGENCE = _SKILL.parent / "modes/pr-intelligence.md"
_REPORT_INTELLIGENCE = _SKILL.parent / "modes/report-intelligence.md"

#: An instruction to place content in reply text, or right before a question, instead of in the question call.
_REPLY_BEFORE_ASK = re.compile(
    r"\b(?:[Pp]rint|[Pp]ut|[Rr]eport)\b(?P<what>[^\n]{0,200}?)"
    r"(?:\bin (?:a|an|the) (?:\*\*)?(?:assistant )?(?:user-facing )?reply\b|\bas reply text in the message\b"
    r"|\bas the last text of the (?:reply )?message\b|\bright before the picker\b|\bbefore Call 1\b|\bbefore the call\b"
    r"|;\s*`AskUserQuestion`)"
)
#: Objects that only point at a payload quoted right after the instruction ("print this line in the reply: `...`").
_POINTERS = ("this line", "one line")
#: Reply-text payloads that may stay, each with the reason; every other match is must-see content that can vanish.
#: Tables have an explicit pre-picker exception below; all notices still belong in question text.
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
            # The table alone has a pre-picker exception; hints and arbitrary reply output still fail.
            line = text[match.start() :].split("\n", 1)[0]
            if "table" in match.group("what").lower() and "as the last text of the reply message that issues" in line:
                continue
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
    def test_every_selection_route_saves_the_table_and_prints_before_the_picker(
        self, source: Path, start: str, end: str
    ) -> None:
        """All four source routes retain the full table and expose items in Q1 before choosing."""
        route = _route(source.read_text(encoding="utf-8"), start, end)
        assert "action-items-table.md" in route
        assert "as the last text of the reply message that issues" in route
        assert "before the call" in route or "before that call" in route
        assert "plain lines" in route or "Q1’s question text listing every pending item" in route
        assert "never in an option’s `preview`" in route
        assert "Bash/tool stdout" in route

    def test_table_is_shown_once_per_selection_call(self) -> None:
        """Each picker prints its saved table first, once, and a denied retry does not repeat it."""
        skill = _SKILL.read_text(encoding="utf-8")
        picker = skill[skill.index("## Step 3d: User item selection") : skill.index("**Cap mechanics")]
        assert "**Show the table exactly once per selection call — before the picker**" in picker
        assert "before the call" in picker
        assert "refresh it for every selection call" in picker
        assert "as a normal Markdown table, outside code fences" in picker
        assert "Do not print it again after the answer" in picker
        assert "denied call re-issues without a table reprint" in picker
        assert "as the last text of the reply message that issues" in picker
        assert "never in an option’s `preview`" in picker
        assert "re-issue the identical call once" not in skill
        assert "visible reply chars" not in skill

    def test_every_selection_call_lists_items_in_q1_without_previews(self) -> None:
        """Call 1, Call 2, closed-only and large runs share the same question-text contract."""
        skill = _SKILL.read_text(encoding="utf-8")
        picker = skill[skill.index("## Step 3d: User item selection") : skill.index("**Cap mechanics")]
        bulk = skill[skill.index("**Bulk action — hard rule**") : skill.index("\n**Bulk-action resolution**")]
        call_two = skill[skill.index("- **Call 2** = ") : skill.index("- A Call 1 bulk answer that resolves to")]
        large = skill[
            skill.index("**≥19 pending items — context-budget mode**") : skill.index(
                "<!-- branch: main-path — commit-mode"
            )
        ]
        assert "**Items in the question text — MANDATORY, the gate's source of truth**" in picker
        assert "Call 1, Call 2, closed-only and ≥19" in picker
        assert "#<id> [<type>] <summary up to 50 chars>" in picker
        assert "most severe first" in picker
        assert "every pending row plus closed rows" in picker
        assert "**no `preview` on any option**" in picker
        assert "a one-line description with the applicable item count, and no `preview` field" in bulk
        assert "recommended option second" in bulk
        assert "pending-item list in Q1’s question text, with no option previews" in call_two
        assert "options carry no preview" in large
        assert "pending counts by type past the budget" in large

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
        assert step.index("Merge summary — heads the saved table and the pre-picker reply:") < step.index(
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
        section = text[idx : text.index("<!-- branch: main-path — commit-mode", idx)]
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
                "Merge summary — heads the saved table",
                "head `$IMPL_DIR/action-items-table.md`",
                id="merged-sources-before-picker",
            ),
            pytest.param(
                "modes/pr-intelligence.md",
                "Read `$IMPL_DIR/pr-intelligence.md`",
                "Later steps read per-item",
                "Sources block + motivation at the top of `$IMPL_DIR/action-items-table.md`",
                id="pr-sources-before-picker",
            ),
            pytest.param(
                "modes/report-intelligence.md",
                "### Report header state",
                "<!-- loads: review-section-taxonomy.md -->",
                "Sources block — it heads `$IMPL_DIR/action-items-table.md`",
                id="report-sources-before-picker",
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
                "modes/dispatch-phase2.md",
                "**Group-preview gate",
                "Derive `<group_tag>`",
                "2. The question text closes with this line",
                id="custom-gate-compact-hint-in-question-text",
            ),
            pytest.param(
                "modes/dispatch-phase1.md",
                "**Challenge double-timeout gate**",
                "Drop block",
                '\nLong wait? `/compact` now — state persisted in <IMPL_DIR>, resume lossless."',
                id="timeout-gate-compact-hint-in-question-text",
            ),
            pytest.param(
                "modes/dispatch-phase2.md",
                "**Group-preview gate",
                "Derive `<group_tag>`",
                "with the step 1 group lines in the question text and no option `preview` fields",
                id="custom-groups-in-question-text",
            ),
            pytest.param(
                "modes/dispatch-phase1.md",
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
        """Each gate keeps its context in question text; tables also precede the picker."""
        text = (_RESOLVE_DIR / relative).read_text(encoding="utf-8")
        section = text[text.index(start) : text.index(end, text.index(start))]
        assert marker in section


def test_bulk_text_has_only_the_ordered_fixed_lines() -> None:
    """The fixed template excludes improvised lines and budgets the complete question."""
    text = _SKILL.read_text(encoding="utf-8")
    section = _route(text, "\n**Bulk question text**", "**ESSENTIAL — exactly these 4 options")
    markers = [
        "Pending item lines",
        "1. `→ To pick items yourself",
        "2. `→ N pending items",
        "3. `→ N resolved/addressed",
        "4. `→ N pending items",
        "5. `` Long wait?",
        "6. `→ Full item table:",
    ]
    offsets = [section.index(marker) for marker in markers]
    assert offsets == sorted(offsets)
    assert "the model adds no other lines" in section
    assert "at most one short sentence" in section
    assert "→ N/A item picking" not in text
    assert "line 1 is always `Or choose a bulk action:`" not in text
    assert "Reserve those fixed lines first" in text
    assert "**1,500 characters**" in text
    assert "always retain at least the top item and the table path" in text
    assert "Closed items are named by count only unless their lines fit" in text


def test_group_gate_lists_plain_groups_without_previews() -> None:
    """Custom dispatch saves every group and shows plain lines before the width decision."""
    text = _dispatch_text()
    gate = _route(text, "**Group-preview gate", "Derive `<group_tag>`")
    for marker in (
        "phase2-groups-table.md",
        "about 6 lines",
        "1,500-character whole-question budget",
        "top groups that fit",
        "total group count and file path",
        "no preview",
        "as the last text of the reply message that issues",
        "recommended option stays second",
    ):
        assert marker in gate


@pytest.mark.parametrize(
    ("instruction", "flagged"),
    [
        pytest.param(
            "Print the table once as the last text of the reply message that issues the picker.",
            False,
            id="table-before-picker",
        ),
        pytest.param(
            "Print the hint as the last text of the reply message that issues the picker.",
            True,
            id="hint-needs-question-text",
        ),
        pytest.param("Print the table once in the reply after the answer.", True, id="wrong-timing"),
        pytest.param("Print the table in the reply before asking.", True, id="before-answer"),
        pytest.param("Print the table in the reply.", True, id="unspecified-timing"),
    ],
)
def test_reply_scanner_allows_only_explicit_pre_picker_table_timing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, instruction: str, flagged: bool
) -> None:
    """Only the explicitly authorized table placement escapes the pre-question-output scan."""
    (tmp_path / "SKILL.md").write_text(instruction, encoding="utf-8")
    monkeypatch.setattr(__import__(__name__, fromlist=["_RESOLVE_DIR"]), "_RESOLVE_DIR", tmp_path)
    assert bool(_unlisted_reply_instructions()) is flagged
