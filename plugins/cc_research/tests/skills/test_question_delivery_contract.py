"""Keep research decisions readable without rich content in option previews.

Each test reads a skill or mode file, isolates the gate it guards and checks that the gate's wording carries the
delivery contract: plain question text, the full artifact printed once beforehand, and no option previews. Phrases are
matched on whitespace-collapsed text so reflowing the Markdown cannot fail them.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _research_text import flat

_SKILLS = Path(__file__).resolve().parents[2] / "skills"
_PREVIEW_FOR_EVERY_OPTION = ("every option's `preview`", "`preview` of every")


def _flat(relative: str) -> str:
    """File text with whitespace runs collapsed, so a phrase matches wherever the prose is wrapped."""
    return flat((_SKILLS / relative).read_text(encoding="utf-8"))


def _missing(text: str, required: tuple[str, ...] | list[str]) -> list[str]:
    """Return the required phrases absent from ``text``."""
    return [phrase for phrase in required if phrase not in text]


def _present(text: str, forbidden: tuple[str, ...] | list[str]) -> list[str]:
    """Return the forbidden phrases found in ``text``."""
    return [phrase for phrase in forbidden if phrase in text]


@pytest.mark.parametrize("relative", ["topic/SKILL.md", "topic/modes/plan.md", "topic/modes/team.md"])
def test_topic_header_follows_the_answer(relative: str) -> None:
    """Every topic path prepares a plain question summary and delivers the full header after the answer.

    The header table carries rich content, so it must come after the user's answer and never ride in an option preview.
    """
    text = _flat(relative)

    assert (
        _missing(text, ["plain summary", "`Title`", "`Outcome`", "final reply after the answer", "no previews"]) == []
    )
    assert _present(text.lower(), ["preview cap"]) == []
    assert _present(text, _PREVIEW_FOR_EVERY_OPTION) == []


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
def test_rich_gate_saves_and_prints_artifact_before_question(
    relative: str, start: str, end: str, artifact: str
) -> None:
    """Each rich gate names the full artifact, prints it once, and keeps a bounded plain question.

    The gate section runs from ``start`` to ``end`` in the file; inside it the artifact path, the "last reply text
    before" placement, the 1,500-character question cap and the no-previews rule must all be spelled out.
    """
    text = _flat(relative).split(start, 1)[1].split(end, 1)[0]

    assert _missing(text, [artifact, "last reply text before", "question text", "1,500", "no previews"]) == []
    assert _present(text, _PREVIEW_FOR_EVERY_OPTION) == []


def test_resumed_queue_reuses_the_complete_delivery_gate() -> None:
    """A resumed paid run must rebuild and show its queue before reusing the confirmation gate.

    The resume branch is a single line starting at the ``phase: "B"`` marker, so it is read line-wise.
    """
    text = (_SKILLS / "run/modes/team.md").read_text(encoding="utf-8").split('`phase: "B"`', 1)[1].splitlines()[0]

    assert _missing(text, ["write", "last reply text before", "question text", "no previews"]) == []


def test_approved_advisors_still_show_config_before_overwrite() -> None:
    """The successful-advisor overwrite gate shows the current configuration while retaining abort.

    The gate sits between the "All advisors return" marker and the next step heading; it must keep the overwrite
    warning, both lettered options and the note that no extra question is needed.
    """
    text = _flat("plan/SKILL.md").split("All advisors return", 1)[1].split("### Step P-P3:", 1)[0]

    required = [
        "$PLAN_RUN_DIR/advisor-suggestions.md",
        "last reply text before",
        "question text",
        "overwrite warning",
        "no previews",
        "(a) Overwrite",
        "(b) Abort",
        "no AskUserQuestion needed",
    ]
    assert _missing(text, required) == []
