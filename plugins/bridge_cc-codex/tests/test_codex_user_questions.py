"""Check independently loadable Codex question guidance and its authorization boundaries."""

from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
QUESTION_REFERENCE = "rules/codex-user-questions.md"
SKILLS = sorted((PLUGIN_ROOT / "codex-skills").glob("*/SKILL.md"))


@pytest.mark.installed_plugin
def test_every_codex_skill_loads_local_question_guidance() -> None:
    """Prevent a generated question from bypassing the independently shipped policy."""
    assert len(SKILLS) == 4
    reference = PLUGIN_ROOT / QUESTION_REFERENCE.split("#")[0]
    assert reference.is_file()
    guide = reference.read_text(encoding="utf-8")
    assert len(guide.split()) <= 350
    assert "The root owns user questions" in guide
    assert "codex-user-questions-details.md" in guide
    assert reference.with_name("codex-user-questions-details.md").is_file()
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
        "Tool acceptance does not prove a selectable form was rendered.",
        "A user-reported dismissed question is no longer a usable pending control.",
        "or later required decisions in the same host until delivery is verified to work again",
        "Synchronous unavailability alone never justifies plain chat.",
        "A failed control call is not a submitted question or an answer.",
        "Do not replay already-delivered report context",
        "A higher-priority host instruction that explicitly mandates plain text remains binding.",
        "Record each control's exposure, permission and input-fit evidence",
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
    details = PLUGIN_ROOT / QUESTION_REFERENCE.split("/")[0] / "codex-user-questions-details.md"
    text += "\n" + details.read_text(encoding="utf-8")
    assert obligation.lower() in text.lower()


@pytest.mark.installed_plugin
def test_codex_prompts_do_not_force_chat_only_or_yes_no_authorization() -> None:
    """Reject obsolete host assumptions without changing factual or exact-token answers."""
    for skill in SKILLS:
        text = skill.read_text(encoding="utf-8")
        assert "Codex has no `AskUserQuestion`" not in text, skill
        assert "(yes / no)" not in text, skill


@pytest.mark.installed_plugin
def test_async_question_contract_matches_exposed_schema_and_pending_turn() -> None:
    """Prevent rejected async payloads and a status handoff that buries an unanswered question."""
    guide = (PLUGIN_ROOT / QUESTION_REFERENCE).read_text(encoding="utf-8")
    details = (PLUGIN_ROOT / "rules/codex-user-questions-details.md").read_text(encoding="utf-8")
    assert "async takes `questions`, not top-level `title`/`options`" in guide
    assert "yield without final/status if idle" in guide
    assert "top-level `questions` array" in details
    assert "do not pass either field at the top level" in details
    assert "Inspect the active schema because another host may differ" in details
    assert "Do not append a final or status message after an accepted async question" in details
    assert "Use the actual tool schema: async questions use `title`" not in details


@pytest.mark.installed_plugin
def test_keyed_approval_requires_key_even_with_one_pending_decision() -> None:
    """Prevent bare approval from authorizing a keyed action by conversational inference."""
    guide = (PLUGIN_ROOT / QUESTION_REFERENCE).read_text(encoding="utf-8")
    details = (PLUGIN_ROOT / "rules/codex-user-questions-details.md").read_text(encoding="utf-8")
    assert "Keyed approval requires the key even with one pending decision" in guide
    assert "bare `approve` or `yes` without that key is invalid" in details
    assert "even when it is the only pending decision" in details
    assert "request the complete keyed answer once" in details
    assert "Never supply a missing key from the prior question or another message" in details
