"""Keep must-see content within Claude Code's option-preview cap, or deliver it as a named file.

Claude Code (2.1.294) shows a placeholder instead of any ``AskUserQuestion`` option preview longer than 2000 chars and
clips a taller one to terminal rows − 26 lines, with no scroll. A skill that makes an over-cap preview the only copy
of a report, spec, plan or queue therefore asks the user to decide on content they never saw. The rule — preview cap
of 2000 chars and 12 lines; over it, the full content goes to a file first, every option preview carries a compact
summary plus that path, and the question text names the file — is stated once in foundry's communication rule and at
every skill site that carries must-see content in option previews. The workflow gate hooks enforce it
(``report-header-table.js``).

These checks pin:

* every Claude skill or mode file carrying content in option previews states the preview cap and the question-text
  file naming, except the listed files whose preview is short by construction or owned by another contract;
* the shared hook module copies define the cap the rule states (2000 chars, 12 lines, a line wrapping at 86 chars);
* every report gate hook's missing-report reason names the cap and the named-file shape;
* foundry's communication rule (always-loaded stub and full body) states the cap and the file fallback.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_DIR = REPO_ROOT / "plugins"

#: Phrases that put must-see content in option previews.
_PREVIEW_CONTENT = re.compile(
    r"`preview` of (?:every|both)|every option's `preview`|both options' `preview`|option previews|"
    r"every option: `preview`",
)

#: Files carrying preview content that need no cap statement, each with the reason (paths relative to ``plugins/``).
_EXEMPT = {
    "cc_foundry/skills/manage/modes/rename-validation.md": "file:line plus 5 context lines per hit — within the cap",
}

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


def _preview_files() -> list[str]:
    """Return every Claude skill or mode file (relative to ``plugins/``) that carries content in option previews.

    Examples:
        >>> "cc_oss/skills/review/SKILL.md" in _preview_files()
        True
    """
    files = sorted([*PLUGINS_DIR.glob("cc_*/skills/**/*.md"), *PLUGINS_DIR.glob("*/claude-skills/**/*.md")])
    return [
        path.relative_to(PLUGINS_DIR).as_posix()
        for path in files
        if _PREVIEW_CONTENT.search(path.read_text(encoding="utf-8"))
    ]


def _read(relative: str) -> str:
    """Return the text of a plugin file given its path relative to ``plugins/``."""
    return (PLUGINS_DIR / relative).read_text(encoding="utf-8")


@pytest.mark.parametrize("relative", [path for path in _preview_files() if path not in _EXEMPT])
def test_preview_site_states_the_cap_and_the_named_file(relative: str) -> None:
    """A file showing content in option previews states the preview cap and that the question text names the file.

    Without the cap a long report, spec, plan or queue becomes a withheld or clipped preview — the user decides blind;
    without the question-text naming, the over-cap file is easy to miss.
    """
    text = _read(relative)

    assert re.search(r"preview cap", text, re.IGNORECASE), f"{relative} carries preview content without the cap"
    assert re.search(r"question text", text, re.IGNORECASE), f"{relative} never names the file in the question text"


@pytest.mark.parametrize("relative", list(_EXEMPT))
def test_exempt_file_still_carries_preview_content(relative: str) -> None:
    """An exemption that no longer matches preview content is stale and must be dropped from the list."""
    assert _PREVIEW_CONTENT.search(_read(relative)), f"{relative} is exempt but carries no preview content"


@pytest.mark.parametrize("relative", _HELPER_COPIES)
def test_hook_module_defines_the_stated_cap(relative: str) -> None:
    """Each shipped copy of the shared hook module enforces exactly the cap the skills state."""
    text = _read(relative)

    assert "const PREVIEW_MAX_CHARS = 2000;" in text
    assert "const PREVIEW_MAX_LINES = 12;" in text
    assert "const PREVIEW_WRAP_COLUMNS = 86;" in text


@pytest.mark.parametrize("relative", _GATE_HOOKS)
def test_gate_hook_reason_names_the_cap_and_the_file_shape(relative: str) -> None:
    """A missing-report denial names the preview cap and the over-cap shape, so the first corrected call passes."""
    text = _read(relative)

    assert "(2000 chars, 12 lines)" in text
    assert "named in the question text" in text


@pytest.mark.parametrize("relative", ["cc_foundry/rules/communication.md", "cc_foundry/rules/_full/communication.md"])
def test_communication_rule_states_the_cap_and_the_file_fallback(relative: str) -> None:
    """The always-loaded rule and its full body both state the cap numbers, the wrap width and the named-file fallback.

    The hook counts a line over 86 chars as wrapped rows; a rule silent on that width lets a model build 12 wide lines
    that the hook then denies.
    """
    text = _read(relative)

    assert "2000 chars" in text
    assert "12 lines" in text
    assert "86 chars" in text
    assert re.search(r"name the file in the question text|question text names the file", text)
