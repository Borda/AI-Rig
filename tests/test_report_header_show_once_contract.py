"""Keep report-header delivery to one copy: a plain summary in the follow-up question, the table in the final reply.

Tables render badly in ``AskUserQuestion`` option previews, and a header table printed as reply text right before the
follow-up question can arrive as an empty progress update on some models. The report-header workflow skills therefore
put only a short plain summary (report path, ``Title``, ``Outcome``) in the follow-up question's text and deliver the
full header table (or the audit findings report) once, in the turn's final reply after the answer. A path that asks no
question opens the final reply with it, and the ``Stop`` hook enforces that delivery for both paths.

The earlier wording asked for the table in the ``preview`` of every option, or for both a reply-text print and a
preview copy. These checks pin the single-copy contract:

* no instruction sentence pairs a reply-text print with a preview copy of the same table;
* every report-header skill file defers the table to the final reply after the answer;
* skills with a no-question path state the final-reply fallback after the last tool call;
* skills that cite quality-gates' table-first-in-reply rule carry their Output-Routing exemption;
* the shared hook module and the six gate hooks name only the question-text fix in their deny reasons;
* the six gate hooks stay registered on ``PostToolUse(AskUserQuestion)``, which records nothing for ``Stop``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_DIR = REPO_ROOT / "plugins"

#: Instruction files of the report-header workflows: a plain question-text summary, the table in the final reply.
_SKILL_FILES = (
    "cc_oss/skills/review/SKILL.md",
    "cc_oss/skills/analyse/SKILL.md",
    "cc_oss/skills/analyse/modes/thread.md",
    "cc_oss/skills/analyse/modes/vitality.md",
    "cc_oss/skills/analyse/modes/ecosystem.md",
    "cc_develop/skills/review/SKILL.md",
    "cc_foundry/skills/profile/SKILL.md",
    "cc_foundry/skills/audit/SKILL.md",
    "cc_foundry/skills/audit/modes/steps-4-5-7.md",
    "cc_research/skills/topic/SKILL.md",
    "cc_research/skills/topic/modes/team.md",
    "cc_research/skills/topic/modes/plan.md",
)

#: Hook sources whose deny reasons tell the model how to deliver the report.
_HOOK_FILES = (
    "cc_foundry/hooks/report-header-table.js",
    "cc_oss/hooks/report-header-table.js",
    "cc_develop/hooks/report-header-table.js",
    "cc_research/hooks/report-header-table.js",
    "cc_oss/hooks/enforce-review-header.js",
    "cc_oss/hooks/enforce-analyse-header.js",
    "cc_develop/hooks/enforce-review-header.js",
    "cc_research/hooks/enforce-topic-header.js",
    "cc_foundry/hooks/enforce-profile-header.js",
    "cc_foundry/hooks/enforce-audit-header.js",
)

_LIBRARY_FILES = _HOOK_FILES[:4]
_GATE_HOOK_FILES = _HOOK_FILES[4:]

#: Skills that can end without the follow-up question → the block must open the final reply, after the last tool call.
_FALLBACK_FILES = (
    "cc_oss/skills/review/SKILL.md",
    "cc_oss/skills/analyse/SKILL.md",
    "cc_foundry/skills/audit/modes/steps-4-5-7.md",
)

#: Skills that render a quality-gates report header (or audit's findings report) and so must exempt the question path.
_EXEMPTION_FILES = (
    "cc_oss/skills/review/SKILL.md",
    "cc_oss/skills/analyse/SKILL.md",
    "cc_develop/skills/review/SKILL.md",
    "cc_foundry/skills/profile/SKILL.md",
    "cc_foundry/skills/audit/modes/steps-4-5-7.md",
    "cc_research/skills/topic/SKILL.md",
)

#: Conjunctions that ask for a reply-text copy beside the preview copy — the double print this contract removes.
_DOUBLE_COPY = (
    re.compile(r"reply text and also", re.IGNORECASE),
    re.compile(r"in the reply and as the `?preview", re.IGNORECASE),
    re.compile(r"\b(?:re)?print(?:s|ed)?\b[^.;]*\band carry it in the previews", re.IGNORECASE),
    re.compile(r"\bin the reply\b[^.;]*\band (?:also )?(?:pass|carry)\b[^.;]*\bpreview", re.IGNORECASE),
)

#: Statement that the full block is delivered in the final reply after the answer, not in the question.
_FINAL_REPLY_COPY = re.compile(r"final reply after the answer|final reply, after|after the answer", re.IGNORECASE)


def _read(relative: str) -> str:
    """Return the text of a plugin file given its path relative to ``plugins/``.

    Examples:
        >>> "AskUserQuestion" in _read("cc_oss/skills/review/SKILL.md")
        True
    """
    return (PLUGINS_DIR / relative).read_text(encoding="utf-8")


@pytest.mark.parametrize("relative", [*_SKILL_FILES, *_HOOK_FILES])
def test_no_instruction_asks_for_both_copies(relative: str) -> None:
    """No file asks for the table as reply text and also as an option preview.

    The double copy shows the table twice wherever reply text stays visible and adds nothing where it does not, so any
    sentence pairing the two is a regression to the pre-show-once wording.
    """
    text = _read(relative)

    hits = [pattern.pattern for pattern in _DOUBLE_COPY if pattern.search(text)]

    assert hits == [], f"{relative} still pairs a reply-text print with a preview copy: {hits}"


@pytest.mark.parametrize("relative", _SKILL_FILES)
def test_skill_defers_the_table_to_the_final_reply(relative: str) -> None:
    """Every report-header skill or mode file delivers the block in the final reply after the answer."""
    text = _read(relative)

    assert _FINAL_REPLY_COPY.search(text), f"{relative} lacks the final-reply-after-the-answer statement"


@pytest.mark.parametrize("relative", _FALLBACK_FILES)
def test_no_question_path_opens_the_final_reply_after_the_last_tool_call(relative: str) -> None:
    """A path that skips the question prints the block in the turn's final reply, after its last tool call.

    Text printed earlier in that turn is still followed by tool calls (contract writes, Step 7/8 bash), so it can come
    back as an empty progress update just like text before the question; only the final message is reliably visible.
    """
    text = _read(relative)

    assert "final reply" in text
    assert "last tool call" in text


@pytest.mark.parametrize("relative", _EXEMPTION_FILES)
def test_skill_carries_its_output_routing_exemption(relative: str) -> None:
    """Skills whose question path drops the table-first reply carry an explicit Output-Routing exemption."""
    assert "**Output-Routing exemption**" in _read(relative)


@pytest.mark.parametrize("relative", _LIBRARY_FILES)
def test_library_deny_reason_names_only_the_question_text_fix(relative: str) -> None:
    """Deny reasons point at the first question's text and rule out reply text and option fields as the summary.

    The question carries a short plain summary; the full table is the final reply's job, so a reason that sent the
    author back to option previews would reintroduce the table-in-preview shape.
    """
    text = _read(relative)

    assert "in the first question's \\`question\\` text" in text
    assert "Option descriptions and previews do not supply the summary" in text
    assert "reply text before the call does not count" in text
    assert "`preview` of every option" not in text


@pytest.mark.parametrize("relative", _GATE_HOOK_FILES)
def test_gate_hook_deny_reason_asks_for_a_plain_summary(relative: str) -> None:
    """A gate's missing-report reason asks for a plain question-text summary and the table in the final reply."""
    text = _read(relative)

    assert "short plain summary in the" in text
    assert "the final reply" in text
    assert "`preview` of every option" not in text
    assert "as every option's preview" not in text


#: Gate hooks that record a shown follow-up for the ``Stop`` check: (plugin dir, hook script).
_POST_TOOL_USE_GATES = (
    pytest.param("cc_foundry", "enforce-audit-header.js", id="foundry-audit"),
    pytest.param("cc_foundry", "enforce-profile-header.js", id="foundry-profile"),
    pytest.param("cc_oss", "enforce-review-header.js", id="oss-review"),
    pytest.param("cc_oss", "enforce-analyse-header.js", id="oss-analyse"),
    pytest.param("cc_develop", "enforce-review-header.js", id="develop-review"),
    pytest.param("cc_research", "enforce-topic-header.js", id="research-topic"),
)


@pytest.mark.parametrize(("plugin", "script"), _POST_TOOL_USE_GATES)
def test_gate_hook_runs_after_the_follow_up_was_shown(plugin: str, script: str) -> None:
    """Each gate hook is registered once on ``PostToolUse`` with the ``AskUserQuestion`` matcher.

    The step records nothing for ``Stop`` (a shown summary never settles final-reply delivery); the registration stays
    so the hook interface is unchanged.
    """
    hooks_json = json.loads((PLUGINS_DIR / plugin / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    matchers = [
        entry.get("matcher")
        for entry in hooks_json["hooks"].get("PostToolUse", [])
        for hook in entry["hooks"]
        if f'/hooks/{script}"' in hook.get("command", "")
    ]

    assert matchers == ["AskUserQuestion"]
