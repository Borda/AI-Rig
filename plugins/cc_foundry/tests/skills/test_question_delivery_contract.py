"""Keep foundry decisions readable before choosing and preserve report follow-up delivery after the answer.

Each test reads skill Markdown as text and checks single-line phrases, so reflowing a paragraph never breaks an
assertion.
"""

from pathlib import Path

import pytest
from _markdown_text import found, missing

_SKILLS = Path(__file__).resolve().parents[2] / "skills"

#: Rendering limits every plain-question gate states: size caps and the ban on tables, fences and previews.
_QUESTION_LIMITS = (
    "6 plain lines",
    "600 characters",
    "1,500",
    "no table",
    "no pipe layout",
    "no code fence",
    "no `preview`",
)
#: Delivery wording of a saved-and-printed gate: printed once as normal Markdown, last reply text before the question.
_PRINT_ONCE = ("printed once", "normal Markdown", "last reply text before")
#: Wording that told the model to route rich content through option previews before the plain-question rewrite.
_OBSOLETE_PREVIEW_ROUTING = (
    "`preview` of every",
    "`preview` of both",
    "every option's `preview`",
    "option previews",
    "Preview cap",
)


def _question_rule(text: str, section: str) -> str:
    """Resolve the named shared rule when a gate references it instead of repeating its limits."""
    if "§Tree summary format — Question text rule" not in section:
        return section
    if "### Tree summary format" not in text.splitlines():
        text = (_SKILLS / "brainstorm/SKILL.md").read_text(encoding="utf-8")
    lines = text.splitlines()
    start = next(index for index, line in enumerate(lines) if line == "### Tree summary format")
    end = next(index for index in range(start + 1, len(lines)) if lines[index].startswith("### "))
    return next(line for line in lines[start:end] if line.startswith("**Question text rule**:"))


@pytest.mark.parametrize(
    ("relative", "start", "end", "artifact"),
    [
        pytest.param(
            "brainstorm/SKILL.md",
            "3. Call a single `AskUserQuestion`",
            "### Seeding the tree",
            ".reports/brainstorm/<ts>/<slug>-branches.md",
            id="brainstorm-seeding-reaction",
        ),
        pytest.param(
            "brainstorm/SKILL.md",
            "### Tree summary format",
            "### Loop bounds",
            ".reports/brainstorm/<ts>/<slug>-tree.md",
            id="brainstorm-tree-and-seeding",
        ),
        pytest.param(
            "brainstorm/SKILL.md",
            "- After limit:",
            "- **Gate**",
            ".reports/brainstorm/<ts>/<slug>-tree.md",
            id="brainstorm-operation-limit",
        ),
        pytest.param(
            "brainstorm/SKILL.md",
            "## Step 6: Present and gate",
            "**Gate**: do not exit",
            "tree file",
            id="brainstorm-final-tree",
        ),
        pytest.param(
            "brainstorm/modes/breakdown.md",
            "#### Step D1",
            "#### Step D2",
            ".reports/brainstorm/<ts>/<slug>-tree.md",
            id="brainstorm-distillation-tree",
        ),
        pytest.param(
            "brainstorm/modes/breakdown.md",
            "#### Step D3",
            "**Sections**:",
            ".reports/brainstorm/<ts>/<slug>-spec-draft.md",
            id="brainstorm-spec",
        ),
        pytest.param(
            "brainstorm/modes/breakdown.md",
            "#### Step B3",
            "End with",
            ".reports/brainstorm/<ts>/<slug>-plan.md",
            id="brainstorm-plan-both-invocation-variants",
        ),
        pytest.param(
            "create/SKILL.md",
            "**Arc approval + voice**",
            "## Step 4",
            ".temp/create/<slug>-arc.md",
            id="create-arc-and-revision",
        ),
        pytest.param(
            "calibrate/SKILL.md",
            "Build combined benchmark report",
            "## Step 4",
            ".reports/calibrate/<TIMESTAMP>/benchmark-summary.md",
            id="calibrate-benchmark-and-proposals",
        ),
        pytest.param(
            "session/SKILL.md",
            "Build the Unlanded table",
            "When a `session-restore.js`",
            ".temp/session/unlanded.md",
            id="session-unlanded",
        ),
        pytest.param(
            "setup/SKILL.md",
            "Otherwise the report lists",
            "On (a), re-invoke",
            ".temp/setup/cache-purge.md",
            id="setup-cache-purge",
        ),
        pytest.param(
            "distill/modes/executables.md",
            "## Step E3",
            "## Step E4",
            "$RUN_DIR/candidates.md",
            id="distill-executable-candidates",
        ),
        pytest.param(
            "distill/modes/external.md", "**E13:", "**E14:", "$EXT_RUN_DIR/adoption.md", id="distill-external-adoption"
        ),
        pytest.param(
            "distill/modes/memory.md",
            "Call `AskUserQuestion` tool with the (annotated) proposal table",
            "## Step L5",
            "$RUN_DIR/proposals.md",
            id="distill-memory-proposals",
        ),
        pytest.param(
            "distill/modes/prune.md",
            "**P-eager-2**",
            "**P-eager-3**",
            ".temp/distill/prune/scored.md",
            id="distill-eager-scored-items",
        ),
        pytest.param(
            "distill/modes/prune.md",
            "**P2**",
            "Only after user picks",
            ".temp/distill/prune/proposals.md",
            id="distill-prune-proposals",
        ),
    ],
)
def test_rich_decision_content_is_saved_printed_once_and_summarized(
    relative: str, start: str, end: str, artifact: str
) -> None:
    """Each gate saves its rich content, prints it once before asking, and keeps a bounded plain question summary.

    Each section must name its own artifact and delivery. Limits and option placement must be stated locally or resolved
    through its explicit reference to the named shared rule.
    """
    text = (_SKILLS / relative).read_text(encoding="utf-8")
    section = text.split(start, 1)[1].split(end, 1)[0]
    assert missing(section, [artifact, *_PRINT_ONCE, "question text"]) == []
    rule = _question_rule(text, section)
    assert missing(rule, _QUESTION_LIMITS) == []
    assert "recommended option second" in rule or "display recommended (a) second" in rule


def test_breakdown_gates_reference_the_canonical_question_text_rule() -> None:
    """All three breakdown gates inherit the same limits and option order from the parent skill."""
    text = (_SKILLS / "brainstorm/modes/breakdown.md").read_text(encoding="utf-8")
    references = [
        line for line in text.splitlines() if "apply SKILL.md §Tree summary format — Question text rule" in line
    ]
    assert len(references) == 3
    assert missing(_question_rule(text, references[0]), _QUESTION_LIMITS) == []
    assert "6 plain lines" not in text
    assert "hard cap 1,500" not in text


def test_clarifying_question_example_places_recommendation_second() -> None:
    """The Step 2 example agrees with the rule used by actual question delivery gates."""
    lines = (_SKILLS / "brainstorm/SKILL.md").read_text(encoding="utf-8").splitlines()
    rule = next(line for line in lines if line.startswith("- Always use **multiple-choice**"))
    assert "`a) Option A`, `b) Option B ★ recommended`" in rule
    assert "§Tree summary format — Question text rule" in rule


_FOLLOW_UP_FRAGMENTS = (
    "first question's text",
    "6 plain lines",
    "600 characters",
    "1,500",
    "no table",
    "no pipe layout",
    "no code fence",
    "final reply after the answer",
    "last tool call",
    "no `preview`",
    "never before the question",
)


@pytest.mark.parametrize("relative", ["audit/SKILL.md", "audit/modes/steps-4-5-7.md"])
def test_audit_follow_ups_use_plain_question_summary_and_final_reply(relative: str) -> None:
    """Audit defers the full findings to the final reply while the question text names where they come from.

    The audit gates point at ``summary.jsonl`` for the question summary and keep the Audit Report headline and total.
    """
    text = (_SKILLS / relative).read_text(encoding="utf-8")
    assert missing(text, [*_FOLLOW_UP_FRAGMENTS, "$RUN_DIR/summary.jsonl", "Audit Report", "Total: N"]) == []


def test_profile_follow_up_uses_plain_question_summary_and_final_reply() -> None:
    """Profile defers the full header to the final reply while the question text names the report it comes from.

    The profile gate points at ``report.md`` for the question summary and keeps the Title and Outcome header fields.
    """
    text = (_SKILLS / "profile/SKILL.md").read_text(encoding="utf-8")
    assert missing(text, [*_FOLLOW_UP_FRAGMENTS, "$REPORT_DIR/report.md", "Title", "Outcome"]) == []


def test_rename_question_carries_small_diff_and_saves_long_context() -> None:
    """Ambiguous rename decisions include a small diff inline or a saved, printed long diff before asking.

    The rename-validation mode keeps a short diff in the question text, saves a long one with 5-line context, and
    batches at most four questions per call.
    """
    text = (_SKILLS / "manage/modes/rename-validation.md").read_text(encoding="utf-8")
    section = text.split("Collect ambiguous hits", 1)[1]
    required = [
        "small renaming diff in the question text",
        ".temp/manage/rename-hit-<id>.md",
        "5-line context",
        *_PRINT_ONCE,
        *_QUESTION_LIMITS,
        "Batch max 4 per call",
    ]
    assert missing(section, required) == []


def test_shared_terminal_template_defers_header_to_final_reply() -> None:
    """The shared template preserves raw file formats and delivers rendered headers after report follow-up answers."""
    text = (_SKILLS / "_shared/terminal-summaries.md").read_text(encoding="utf-8")
    required = [
        "first question's text",
        "Title",
        "Outcome",
        "Verdict",
        "final reply after the answer",
        "never before the question",
        "no `preview`",
        "Field | Value",
    ]
    assert missing(text, required) == []


def test_audit_final_step_delivers_full_findings_after_all_tools() -> None:
    """The actual audit terminal step keeps every finding after the follow-up answer and final cleanup calls.

    The step prints the whole Step 7 report once, as the last thing, after the completion marker and cleanup calls.
    """
    text = (_SKILLS / "audit/SKILL.md").read_text(encoding="utf-8")
    section = text.split("**Step 11b — terminal output**", 1)[1].split("**Completion marker**", 1)[0]
    required = [
        "final reply after the answer",
        "after the last tool call including the completion marker and cleanup",
        "full Step 7 Audit Report from `$RUN_DIR/audit-report.md`",
        "exact `Total: N`",
        "every finding's `one_line`",
        "normal Markdown",
        "printed once here, never before the question or in an option",
    ]
    assert missing(section, required) == []


@pytest.mark.parametrize(
    "relative",
    [
        "audit/SKILL.md",
        "audit/modes/steps-4-5-7.md",
        "brainstorm/SKILL.md",
        "brainstorm/modes/breakdown.md",
        "calibrate/SKILL.md",
        "create/SKILL.md",
        "distill/modes/executables.md",
        "distill/modes/external.md",
        "distill/modes/memory.md",
        "distill/modes/prune.md",
        "manage/modes/rename-validation.md",
        "profile/SKILL.md",
        "session/SKILL.md",
        "setup/SKILL.md",
        "_shared/terminal-summaries.md",
    ],
)
def test_no_rewritten_gate_still_routes_rich_content_to_option_previews(relative: str) -> None:
    """Sibling instructions in a rewritten file cannot retain the old identical-preview delivery requirement.

    None of the files rewritten for plain-question delivery may still contain any retired preview-routing phrase.
    """
    text = (_SKILLS / relative).read_text(encoding="utf-8")
    assert found(text, _OBSOLETE_PREVIEW_ROUTING) == []
