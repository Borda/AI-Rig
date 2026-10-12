"""Keep must-see content out of option previews, and keep question text short enough for the host to show whole.

Tables, trees and specs render badly in ``AskUserQuestion`` option previews, the host shows only the focused option's
preview (so options sharing one preview carry no information), and Claude Code (2.1.294) shows a placeholder instead of
any preview longer than 2000 chars and cuts question text after 2000 chars. The contract is therefore: content the user
must read before choosing is saved to a file and printed once as the last reply text before the question, the question
text keeps a short plain summary (at most 1,500 chars, no table), and options carry a label plus a one-line description
with no preview. Report-header follow-ups carry the same plain summary and deliver the table in the final reply. The
shared hook module enforces the question-text cap and still bounds an optional preview.

These checks pin:

* no Claude skill or mode file instructs putting content in option previews (sentences that forbid it are fine);
* the shared hook module copies define the question-text cap and the optional preview cap they enforce;
* every report gate hook's missing-report reason asks for the plain question-text summary and the final-reply table;
* foundry's communication rule (always-loaded stub and full body) states the question budget and the no-preview rule.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_DIR = REPO_ROOT / "plugins"

#: Phrases that put content in option previews.
_PREVIEW_CONTENT = re.compile(
    r"`preview` of (?:every|both)|every option's `preview`|both options' `preview`|option previews|"
    r"every option: `preview`|as the `preview`",
)

#: A sentence that forbids or removes the preview carrier instead of prescribing it.
_FORBIDS = re.compile(r"\b(?:never|not|no|without|nor|do not|does not)\b|no `preview`", re.IGNORECASE)

#: Shared hook module copies, canonical first.
_HELPER_COPIES = (
    "cc_foundry/hooks/report-header-table.js",
    "cc_oss/hooks/report-header-table.js",
    "cc_develop/hooks/report-header-table.js",
    "cc_research/hooks/report-header-table.js",
)

#: Report gate hooks whose missing-report reason tells the model how to deliver the report.
_GATE_HOOKS = (
    "cc_foundry/hooks/enforce-audit-header.js",
    "cc_foundry/hooks/enforce-profile-header.js",
    "cc_oss/hooks/enforce-review-header.js",
    "cc_oss/hooks/enforce-analyse-header.js",
    "cc_develop/hooks/enforce-review-header.js",
    "cc_research/hooks/enforce-topic-header.js",
)


def _read(relative: str) -> str:
    """Return the text of a plugin file given its path relative to ``plugins/``."""
    return (PLUGINS_DIR / relative).read_text(encoding="utf-8")


def _skill_files() -> list[str]:
    """Return every Claude skill or mode file, relative to ``plugins/``.

    Examples:
        >>> "cc_oss/skills/review/SKILL.md" in _skill_files()
        True
    """
    files = sorted([*PLUGINS_DIR.glob("cc_*/skills/**/*.md"), *PLUGINS_DIR.glob("*/claude-skills/**/*.md")])
    return [path.relative_to(PLUGINS_DIR).as_posix() for path in files]


def _preview_instructions(text: str) -> list[str]:
    """Return the lines of ``text`` that prescribe option-preview content without forbidding it.

    Examples:
        >>> _preview_instructions("Put the table as the `preview` of every option.")
        ['Put the table as the `preview` of every option.']
        >>> _preview_instructions("Options carry a label, no `preview`; never in every option's `preview`.")
        []
    """
    return [line for line in text.splitlines() if _PREVIEW_CONTENT.search(line) and not _FORBIDS.search(line)]


@pytest.mark.parametrize("relative", _skill_files())
def test_no_skill_puts_content_in_option_previews(relative: str) -> None:
    """A skill or mode file never prescribes option previews as the carrier of must-see content.

    The preview shows only for the focused option and renders tables badly; the content belongs in the file printed
    before the question, with a plain summary in the question text.
    """
    hits = _preview_instructions(_read(relative))

    assert hits == [], f"{relative} still prescribes option-preview content: {hits}"


@pytest.mark.parametrize(
    "line",
    [
        pytest.param("Put the table as the `preview` of every option.", id="every-option"),
        pytest.param("Set both options' `preview` to the plan.", id="both-options"),
        pytest.param("Carried in option previews within the cap.", id="option-previews"),
    ],
)
def test_preview_prescriptions_are_detected(line: str) -> None:
    """Each wording that put content in previews is detected, so a regression cannot pass silently."""
    assert _preview_instructions(line)


@pytest.mark.parametrize("relative", _HELPER_COPIES)
def test_hook_module_defines_the_stated_caps(relative: str) -> None:
    """Each shipped copy of the shared hook module enforces the question cap and still bounds an optional preview."""
    text = _read(relative)

    assert "const QUESTION_MAX_CHARS = 1500;" in text
    assert "const PREVIEW_MAX_CHARS = 2000;" in text
    assert "const PREVIEW_MAX_LINES = 12;" in text
    assert "const PREVIEW_WRAP_COLUMNS = 86;" in text


@pytest.mark.parametrize("relative", _GATE_HOOKS)
def test_gate_hook_reason_asks_for_the_question_summary(relative: str) -> None:
    """A missing-report denial asks for the plain question-text summary and the table in the final reply."""
    text = _read(relative)

    assert "short plain summary" in text
    assert "final reply" in text
    assert "(2000 chars, 12 lines)" not in text


@pytest.mark.parametrize("relative", ["cc_foundry/rules/communication.md", "cc_foundry/rules/_full/communication.md"])
def test_communication_rule_states_the_question_budget(relative: str) -> None:
    """The always-loaded rule and its full body both state the question budget and the no-preview default."""
    text = _read(relative)

    assert "6 plain lines" in text
    assert "hard cap 1,500" in text
    assert "never table/tree in option `preview`" in text
    assert re.search(r"no preview by default|Options: label \+ one-line effect; no preview by default", text)
