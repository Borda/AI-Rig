"""Keep report-header delivery to one copy: the follow-up option previews, or the final reply when nothing is asked.

On models that return text written before a tool call as a progress-update thinking block (empty under the default
``display``), a header table printed as reply text right before the follow-up ``AskUserQuestion`` never reaches the
user. The workflow skills therefore carry the table (or the audit findings report, the session Unlanded table, the
brainstorm tree summary) only as the ``preview`` of every single-select option. A path that asks no question prints it
as the opening of the turn's final reply instead, and the ``Stop`` hook enforces that fallback.

The previous wording asked for both copies — "print it as reply text and also pass it as the preview" — which shows the
table twice on every model that keeps the text visible. These checks pin the single-copy contract:

* no instruction sentence pairs a reply-text print with a preview copy of the same table;
* every edited file states the preview-only rule;
* skills with a no-question path state the final-reply fallback after the last tool call;
* skills that cite quality-gates' table-first-in-reply rule carry their Output-Routing exemption;
* the shared hook module and the six gate hooks name only the preview fix in their deny reasons;
* the six gate hooks run on ``PostToolUse(AskUserQuestion)``, where a shown follow-up settles the ``Stop`` check.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_DIR = REPO_ROOT / "plugins"

#: Instruction files whose follow-up question carries the delivery block in its option previews.
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
    "cc_foundry/skills/session/SKILL.md",
    "cc_foundry/skills/brainstorm/SKILL.md",
    "cc_foundry/skills/brainstorm/modes/breakdown.md",
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

#: Skills that can end without the follow-up question → the block must open the final reply, after the last tool call.
_FALLBACK_FILES = (
    "cc_oss/skills/review/SKILL.md",
    "cc_oss/skills/analyse/SKILL.md",
    "cc_foundry/skills/audit/modes/steps-4-5-7.md",
)

#: Skills that render a quality-gates report header (or audit's findings report) and so must exempt the preview path.
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

#: Statement that the preview is the block's only copy on the question path.
_SINGLE_COPY = re.compile(r"never also as reply text|not as reply text|not printed here|its only copy", re.IGNORECASE)


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
def test_skill_states_the_preview_is_the_only_copy(relative: str) -> None:
    """Every edited skill or mode file says the block is not also printed as reply text before the question."""
    assert _SINGLE_COPY.search(_read(relative)), f"{relative} lacks the preview-only (single copy) statement"


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


@pytest.mark.parametrize("relative", _HOOK_FILES)
def test_hook_deny_reason_names_only_the_preview_fix(relative: str) -> None:
    """Deny reasons point at every option's preview and rule out a reply-text copy before the call."""
    text = _read(relative)

    assert "`preview` of every option" in text
    assert "not as reply text" in text


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

    That step records the report version for ``Stop`` only once the question was shown; the PreToolUse pass no longer
    does, so without this registration every preview-delivered report gets one forced ``Stop`` continuation.
    """
    hooks_json = json.loads((PLUGINS_DIR / plugin / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    matchers = [
        entry.get("matcher")
        for entry in hooks_json["hooks"].get("PostToolUse", [])
        for hook in entry["hooks"]
        if f'/hooks/{script}"' in hook.get("command", "")
    ]

    assert matchers == ["AskUserQuestion"]
