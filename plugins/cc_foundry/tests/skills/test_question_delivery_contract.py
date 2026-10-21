"""Keep foundry decisions readable before choosing and preserve report follow-up delivery after the answer."""

from pathlib import Path

import pytest

_SKILLS = Path(__file__).resolve().parents[2] / "skills"


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
    """Each gate saves its rich content, prints it once before asking, and keeps a bounded plain question summary."""
    text = (_SKILLS / relative).read_text(encoding="utf-8")
    section = text.split(start, 1)[1].split(end, 1)[0]
    assert artifact in section
    assert "printed once" in section
    assert "normal Markdown" in section
    assert "last reply text before" in section
    assert "question text" in section
    assert "6 plain lines" in section
    assert "600 characters" in section
    assert "1,500" in section
    assert "no table" in section
    assert "no pipe layout" in section
    assert "no code fence" in section
    assert "no `preview`" in section
    assert "recommended option second" in section or "display recommended (a) second" in section


@pytest.mark.parametrize("relative", ["audit/SKILL.md", "audit/modes/steps-4-5-7.md", "profile/SKILL.md"])
def test_report_follow_ups_use_plain_question_summary_and_final_reply(relative: str) -> None:
    """Audit and profile defer the full findings/header to the final reply while question text names its source."""
    text = (_SKILLS / relative).read_text(encoding="utf-8")
    assert "first question's text" in text
    assert "6 plain lines" in text
    assert "600 characters" in text
    assert "1,500" in text
    assert "no table" in text
    assert "no pipe layout" in text
    assert "no code fence" in text
    assert "final reply after the answer" in text
    assert "last tool call" in text
    assert "no `preview`" in text
    assert "never before the question" in text
    if relative.startswith("audit/"):
        assert "$RUN_DIR/summary.jsonl" in text
        assert "Audit Report" in text
        assert "Total: N" in text
    else:
        assert "$REPORT_DIR/report.md" in text
        assert "Title" in text
        assert "Outcome" in text


def test_rename_question_carries_small_diff_and_saves_long_context() -> None:
    """Ambiguous rename decisions include a small diff inline or a saved, printed long diff before asking."""
    text = (_SKILLS / "manage/modes/rename-validation.md").read_text(encoding="utf-8")
    section = text.split("Collect ambiguous hits", 1)[1]
    assert "small renaming diff in the question text" in section
    assert ".temp/manage/rename-hit-<id>.md" in section
    assert "5-line context" in section
    assert "printed once" in section
    assert "normal Markdown" in section
    assert "last reply text before" in section
    assert "6 plain lines" in section
    assert "600 characters" in section
    assert "1,500" in section
    assert "no table" in section
    assert "no pipe layout" in section
    assert "no code fence" in section
    assert "no `preview`" in section
    assert "Batch max 4 per call" in section


def test_shared_terminal_template_defers_header_to_final_reply() -> None:
    """The shared template preserves raw file formats and delivers rendered headers after report follow-up answers."""
    text = (_SKILLS / "_shared/terminal-summaries.md").read_text(encoding="utf-8")
    assert "first question's text" in text
    assert "Title" in text
    assert "Outcome" in text
    assert "Verdict" in text
    assert "final reply after the answer" in text
    assert "never before the question" in text
    assert "no `preview`" in text
    assert "Field | Value" in text


def test_audit_final_step_delivers_full_findings_after_all_tools() -> None:
    """The actual audit terminal step keeps every finding after the follow-up answer and final cleanup calls."""
    text = (_SKILLS / "audit/SKILL.md").read_text(encoding="utf-8")
    section = text.split("**Step 11b — terminal output**", 1)[1].split("**Completion marker**", 1)[0]
    assert "final reply after the answer" in section
    assert "after the last tool call including the completion marker and cleanup" in section
    assert "full Step 7 Audit Report from `$RUN_DIR/audit-report.md`" in section
    assert "exact `Total: N`" in section
    assert "every finding's `one_line`" in section
    assert "normal Markdown" in section
    assert "printed once here, never before the question or in an option" in section


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
    """Sibling instructions in a rewritten file cannot retain the old identical-preview delivery requirement."""
    text = (_SKILLS / relative).read_text(encoding="utf-8")
    for obsolete in (
        "`preview` of every",
        "`preview` of both",
        "every option's `preview`",
        "option previews",
        "Preview cap",
    ):
        assert obsolete not in text
