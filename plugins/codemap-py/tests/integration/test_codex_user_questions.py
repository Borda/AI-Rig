"""Check independently loadable Codex question guidance and its authorization boundaries.

The guidance is Markdown prose, so every read collapses whitespace first: an assertion matches words, never where a
paragraph happens to wrap.
"""

from pathlib import Path

import pytest
from _codemap_skill_text import missing

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
QUESTION_REFERENCE = "shared/codex-user-questions.md"
GUIDE = PLUGIN_ROOT / QUESTION_REFERENCE
DETAILS = PLUGIN_ROOT / "shared/codex-user-questions-details.md"
SKILLS = sorted((PLUGIN_ROOT / "codex-skills").glob("*/SKILL.md"))


def _read(path: Path) -> str:
    """Return *path*'s text with every whitespace run collapsed to one space."""
    return " ".join(path.read_text(encoding="utf-8").split())


@pytest.mark.installed_plugin
class TestQuestionGuideLayout:
    """The shipped guide is short, points at its details file, and every Codex skill links to it."""

    def test_six_codex_skills_ship(self) -> None:
        """Six Codex skills are discovered, so the per-skill checks below cannot silently run on none."""
        assert len(SKILLS) == 6

    def test_guide_is_short_and_points_at_details(self) -> None:
        """The guide stays within 350 words, states root ownership and links an existing details file.

        A short guide is what every skill loads before asking; the long-form rules live in the details file it names.
        """
        guide = GUIDE.read_text(encoding="utf-8")

        assert GUIDE.is_file()
        assert len(guide.split()) <= 350
        assert "The root owns user questions" in " ".join(guide.split())
        assert "codex-user-questions-details.md" in guide
        assert GUIDE.with_name("codex-user-questions-details.md").is_file()

    @pytest.mark.parametrize("skill", [pytest.param(skill, id=skill.parent.name) for skill in SKILLS])
    def test_skill_loads_local_question_guidance(self, skill: Path) -> None:
        """Prevent a generated question from bypassing the independently shipped policy.

        Each Codex skill must link the guide by a relative path that resolves to the shipped file and remind the model
        to read it before asking.
        """
        text = _read(skill)

        assert f"../../{QUESTION_REFERENCE}" in text
        assert "Before asking" in text
        assert (skill.parent / "../../" / QUESTION_REFERENCE).resolve() == GUIDE

    @pytest.mark.parametrize("skill", [pytest.param(skill, id=skill.parent.name) for skill in SKILLS])
    def test_prompts_do_not_force_chat_only_or_yes_no_authorization(self, skill: Path) -> None:
        """Reject obsolete host assumptions without changing factual or exact-token answers."""
        text = _read(skill)

        assert "Codex has no `AskUserQuestion`" not in text
        assert "(yes / no)" not in text


@pytest.mark.installed_plugin
@pytest.mark.parametrize(
    "obligation",
    [
        "request_user_input",
        "request_user_input_async",
        pytest.param(
            "Tool acceptance does not prove a selectable form was rendered.", id="acceptance-not-render-proof"
        ),
        pytest.param(
            "A user-reported dismissed question is no longer a usable pending control.", id="dismissed-not-pending"
        ),
        pytest.param(
            "or later required decisions in the same host until delivery is verified to work again",
            id="no-later-decisions-until-delivery-verified",
        ),
        pytest.param("Synchronous unavailability alone never justifies plain chat.", id="sync-unavailable-not-chat"),
        pytest.param("A failed control call is not a submitted question or an answer.", id="failed-call-not-answer"),
        pytest.param("Do not replay already-delivered report context", id="no-replay-delivered-context"),
        pytest.param(
            "A higher-priority host instruction that explicitly mandates plain text remains binding.",
            id="host-plain-text-mandate-binding",
        ),
        pytest.param("Record each control's exposure, permission and input-fit evidence", id="record-control-evidence"),
        pytest.param("For each genuinely missing user decision", id="ask-per-missing-decision"),
        pytest.param("Use complete actionable values", id="complete-actionable-values"),
        pytest.param("If no independent work remains, yield", id="yield-without-independent-work"),
        pytest.param("This preference applies to optional and required questions.", id="covers-optional-and-required"),
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
        pytest.param("Completed or superseded keys cannot execute again.", id="superseded-keys-cannot-rerun"),
        pytest.param(
            "Bind the complete displayed label, including any decision key and suffix, to the unchanged canonical "
            "value before asking; never strip arbitrary text or alter exact-digest confirmation syntax.",
            id="bind-label-to-canonical-value",
        ),
        pytest.param(
            "Require a visible decision key in the answer when conversation context cannot bind it unambiguously.",
            id="visible-decision-key-required",
        ),
        pytest.param(
            "Silence, skip, timeout, preselection, an empty result, an example answer, or unrelated text "
            "grants no consent.",
            id="silence-grants-no-consent",
        ),
        pytest.param(
            "After supersession, bare `Approve`, indexes, or `all` cannot authorize the replacement; "
            "do not guess from the latest prompt.",
            id="supersession-voids-bare-approve",
        ),
    ],
)
def test_question_contract_keeps_required_safety_obligations(obligation: str) -> None:
    """Protect transport coverage and complete safety clauses, including their negations.

    Each clause must still appear (case-insensitively) somewhere in the short guide plus its details file.
    """
    text = _read(GUIDE) + " " + _read(DETAILS)

    assert " ".join(obligation.split()).lower() in text.lower()


@pytest.mark.installed_plugin
def test_async_question_uses_observed_host_schema_and_yields_without_status() -> None:
    """Prevent rejected async payloads and status text from displacing a pending decision.

    The details file must keep the observed schema shape (top-level ``questions`` array, no top-level duplicates) and
    forbid a status message after an accepted async question.
    """
    details = _read(DETAILS)

    assert (
        missing(
            details,
            (
                "top-level `questions` array",
                "do not pass either field at the top level",
                "Inspect the active schema because another host may differ",
                "Do not append a final or status message after an accepted async question",
            ),
        )
        == []
    )


@pytest.mark.installed_plugin
def test_single_unchanged_decision_accepts_conversational_reply() -> None:
    """Keep clear conversational approval usable without weakening ambiguous binding.

    One pending, unchanged decision accepts an explicit conversational ``Approve`` / ``Deny``; the old blanket refusal
    wording must stay gone while supersession and exact-token rules stay in force.
    """
    details = _read(DETAILS)

    assert (
        missing(
            details,
            (
                "Accept an explicit conversational `Approve` / `Deny`",
                "exactly one pending decision",
                "no competing question, superseded scope, or exact-token/digest requirement",
                "do not ask again solely for a missing decision key",
                "After supersession, bare `Approve`, indexes, or `all` cannot authorize the replacement",
                "generic Approve never replaces",
                "This includes an empty final message",
            ),
        )
        == []
    )
    assert "even when it is the only pending decision" not in details


@pytest.mark.installed_plugin
def test_short_guide_preserves_existing_question_and_consent_contract() -> None:
    """Keep established obligations visible when the short guide is condensed."""
    guide = _read(GUIDE)

    assert (
        missing(
            guide,
            (
                "Children return context, question, choices, custom syntax and answer mapping",
                "Preserve every required action.",
                "existing consent skips reconfirmation",
                "Dismissal makes that control unsuitable",
            ),
        )
        == []
    )
