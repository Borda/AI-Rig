"""Check independently loadable Codex question guidance and its authorization boundaries."""

from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[2]
QUESTION_REFERENCE = "shared/codex-user-questions.md"
SKILLS = sorted((PLUGIN_ROOT / "codex-skills").glob("*/SKILL.md"))


@pytest.mark.installed_plugin
def test_every_codex_skill_loads_local_question_guidance() -> None:
    """Prevent a generated question from bypassing the independently shipped policy."""
    assert len(SKILLS) == 6
    reference = PLUGIN_ROOT / QUESTION_REFERENCE.split("#")[0]
    assert reference.is_file()
    for skill in SKILLS:
        text = skill.read_text(encoding="utf-8")
        assert f"../../{QUESTION_REFERENCE}" in text, skill
        assert "Before asking" in text, skill
        assert (skill.parent / "../../" / QUESTION_REFERENCE.split("#")[0]).resolve() == reference


@pytest.mark.installed_plugin
@pytest.mark.parametrize(
    "obligation",
    [
        "request_user_input",
        "request_user_input_async",
        "Synchronous unavailability alone never justifies plain chat.",
        "For every user-facing choice",
        "Use complete actionable values",
        "If no independent work remains, yield",
        (
            "For an optional question, if async is unavailable or unsuitable, use sync when it is exposed, "
            "permitted for that purpose, and can represent the complete input."
        ),
        "permitted for that purpose",
        "all feasible choices",
        "(Recommended)",
        "recommendation is not consent",
        "Approve",
        "Deny",
        "immutable",
        "superseded",
        "canonical",
        "preselection",
        "runtime permission",
        "headless",
        "Do not invent",
        "Completed or superseded keys cannot execute again.",
        (
            "Bind the complete displayed label, including any decision key and suffix, to the unchanged canonical "
            "value before asking; never strip arbitrary text or alter exact-digest confirmation syntax."
        ),
        ("Otherwise require an unambiguous visible decision key with the answer and state that syntax in the control."),
        (
            "Silence, skip, timeout, preselection, an empty result, an example answer, or unrelated text "
            "grants no consent."
        ),
        (
            "After supersession, bare `Approve`, indexes, or `all` cannot authorize the replacement; "
            "do not guess from the latest prompt."
        ),
    ],
)
def test_question_contract_keeps_required_safety_obligations(obligation: str) -> None:
    """Protect transport coverage and complete safety clauses, including their negations."""
    text = (PLUGIN_ROOT / QUESTION_REFERENCE.split("#")[0]).read_text(encoding="utf-8")
    if "#user-questions" in QUESTION_REFERENCE:
        text = text.split("## User Questions\n", 1)[1].split("\n## ", 1)[0]
    assert obligation.lower() in text.lower()


@pytest.mark.installed_plugin
def test_codex_prompts_do_not_force_chat_only_or_yes_no_authorization() -> None:
    """Reject obsolete host assumptions without changing factual or exact-token answers."""
    for skill in SKILLS:
        text = skill.read_text(encoding="utf-8")
        assert "Codex has no `AskUserQuestion`" not in text, skill
        assert "(yes / no)" not in text, skill
