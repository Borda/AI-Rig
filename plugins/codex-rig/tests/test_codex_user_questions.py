"""Check independently loadable Codex question guidance and its authorization boundaries."""

import json
from pathlib import Path
import re

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
QUESTION_REFERENCE = "shared/codex-user-questions.md"
SKILLS = sorted((PLUGIN_ROOT / "skills").glob("*/SKILL.md"))


@pytest.mark.installed_plugin
def test_every_codex_skill_loads_local_question_guidance() -> None:
    """Prevent a generated question from bypassing the independently shipped policy."""
    assert len(SKILLS) == 15
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
        "Do not retry the same unsuitable transport for that decision",
        "or later required decisions in the same host until delivery is verified to work again",
        "the final user-facing response of the turn",
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


def test_async_question_example_uses_the_exposed_questions_wrapper() -> None:
    """Keep the documented async call valid after a rejected top-level title call."""
    details = (PLUGIN_ROOT / "shared/codex-user-questions-details.md").read_text(encoding="utf-8")
    match = re.search(r"(?m)^([ ]{2,})```json\n\1(\{[^\n]+\})\n\1```$", details)
    assert match is not None
    payload = json.loads(match.group(2))
    assert set(payload) == {"questions"}
    assert len(payload["questions"]) == 1
    assert set(payload["questions"][0]) == {"title", "options"}
    assert payload["questions"][0]["options"] == [
        "example-action: Approve (Recommended)",
        "example-action: Deny",
    ]


def test_keyed_merge_reply_cannot_accept_bare_approval_or_displace_control() -> None:
    """Keep a typed bare approval from authorizing a pending local merge."""
    guide = (PLUGIN_ROOT / "shared/codex-user-questions-details.md").read_text(encoding="utf-8")
    merge = (PLUGIN_ROOT / "skills/code-remediate/SKILL.md").read_text(encoding="utf-8")
    assert "bare `approve` or `yes` without that key is invalid" in guide
    assert "even when it is the only pending decision" in guide
    assert "Do not append a final or status message after an accepted async question" in guide
    assert "a bare `approve` or `yes` cannot authorize the merge" in merge
