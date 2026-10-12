"""Keep research decisions readable without rich content in option previews."""

from pathlib import Path

import pytest

_SKILLS = Path(__file__).resolve().parents[1] / "skills"


@pytest.mark.parametrize("relative", ["topic/SKILL.md", "topic/modes/plan.md", "topic/modes/team.md"])
def test_topic_header_follows_the_answer(relative: str) -> None:
    """Every topic path prepares a plain question summary and delivers the full header after the answer."""
    text = (_SKILLS / relative).read_text(encoding="utf-8")
    for required in ("plain summary", "`Title`", "`Outcome`", "final reply after the answer", "no previews"):
        assert required in text
    assert "preview cap" not in text.lower()
    assert "every option's `preview`" not in text
    assert "`preview` of every" not in text


@pytest.mark.parametrize(
    ("relative", "start", "end", "artifact"),
    [
        pytest.param(
            "plan/SKILL.md",
            "### Step P-P2: Present proposed config",
            "### Step P-P2b:",
            ".temp/research-plan-config-<session-id>.md",
            id="proposed-config",
        ),
        pytest.param(
            "plan/SKILL.md",
            "Any agent returns `ok: false`",
            "All advisors return",
            "$PLAN_RUN_DIR/advisor-suggestions.md",
            id="advisor-issues",
        ),
        pytest.param(
            "run/modes/team.md",
            "5. Build queue",
            "7. Write final ordered queue",
            "<RUN_DIR>/team-queue.md",
            id="implementation-queue",
        ),
        pytest.param(
            "kaggle/SKILL.md",
            "**Credential instructions**",
            "**Grounding queries**",
            ".temp/kaggle/credential-instructions.md",
            id="credential-setup",
        ),
        pytest.param(
            "kaggle/SKILL.md",
            "**Full-data gate**",
            "Data downloaded locally",
            ".temp/kaggle/<slug>-files.md",
            id="download-listing",
        ),
        pytest.param(
            "kaggle/SKILL.md",
            "Build this summary",
            "On (a):",
            ".temp/kaggle/<name>-summary.md",
            id="notebook-follow-up",
        ),
        pytest.param("sweep/SKILL.md", "### Step S4", "Follow-up hint", "<last report path>", id="unresolved-changes"),
    ],
)
def test_rich_decision_content_is_saved_and_printed_before_question(
    relative: str, start: str, end: str, artifact: str
) -> None:
    """Each rich gate names the full artifact, prints it once, and keeps a bounded plain question."""
    text = (_SKILLS / relative).read_text(encoding="utf-8").split(start, 1)[1].split(end, 1)[0]
    for required in (artifact, "last reply text before", "question text", "1,500", "no previews"):
        assert required in text
    assert "every option's `preview`" not in text
    assert "`preview` of every" not in text


def test_resumed_queue_reuses_the_complete_delivery_gate() -> None:
    """A resumed paid run must rebuild and show its queue before reusing the confirmation gate."""
    text = (_SKILLS / "run/modes/team.md").read_text(encoding="utf-8").split('`phase: "B"`', 1)[1].splitlines()[0]
    assert "write" in text
    assert "last reply text before" in text
    assert "question text" in text
    assert "no previews" in text


def test_approved_advisors_still_show_config_before_overwrite() -> None:
    """The successful-advisor overwrite gate shows the current configuration while retaining abort."""
    text = (_SKILLS / "plan/SKILL.md").read_text(encoding="utf-8").split("All advisors return", 1)[1]
    text = text.split("### Step P-P3:", 1)[0]
    for required in (
        "$PLAN_RUN_DIR/advisor-suggestions.md",
        "last reply text before",
        "question text",
        "overwrite warning",
        "no previews",
        "(a) Overwrite",
        "(b) Abort",
        "no AskUserQuestion needed",
    ):
        assert required in text
