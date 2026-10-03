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
        "For each genuinely missing user decision",
        "Use complete actionable values",
        "If no independent work remains, yield",
        "This preference applies to optional and required questions.",
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
        ("Require a visible decision key in the answer when conversation context cannot bind it unambiguously."),
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
def test_async_question_uses_observed_host_schema_and_yields_without_status() -> None:
    """Prevent rejected async payloads and status text from displacing a pending decision."""
    details = (PLUGIN_ROOT / "shared/codex-user-questions-details.md").read_text(encoding="utf-8")
    assert "top-level `questions` array" in details
    assert "do not pass either field at the top level" in details
    assert "Inspect the active schema because another host may differ" in details
    assert "Do not append a final or status message after an accepted async question" in details


@pytest.mark.installed_plugin
def test_single_unchanged_decision_accepts_conversational_reply() -> None:
    """Keep clear conversational approval usable without weakening ambiguous binding."""
    details = (PLUGIN_ROOT / "shared/codex-user-questions-details.md").read_text(encoding="utf-8")
    assert "Accept an explicit conversational `Approve` / `Deny`" in details
    assert "exactly one pending decision" in details
    assert "no competing question, superseded scope, or exact-token/digest requirement" in details
    assert "do not ask again solely for a missing decision key" in details
    assert "even when it is the only pending decision" not in details
    assert "After supersession, bare `Approve`, indexes, or `all` cannot authorize the replacement" in details
    assert "generic Approve never replaces" in details
    assert "This includes an empty final message" in details


@pytest.mark.installed_plugin
def test_short_guide_preserves_existing_question_and_consent_contract() -> None:
    """Keep established obligations visible when the short guide is condensed."""
    guide = (PLUGIN_ROOT / QUESTION_REFERENCE).read_text(encoding="utf-8")
    assert "Children return context, question, choices, custom syntax and answer mapping" in guide
    assert "Preserve every required action." in guide
    assert "existing consent skips reconfirmation" in guide
    assert "Dismissal makes that control unsuitable" in guide
