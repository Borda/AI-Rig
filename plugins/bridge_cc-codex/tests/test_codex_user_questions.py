"""Check independently loadable Codex question guidance and its authorization boundaries."""

from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
QUESTION_REFERENCE = "rules/codex-user-questions.md"
GUIDE_PATH = PLUGIN_ROOT / QUESTION_REFERENCE
DETAILS_PATH = PLUGIN_ROOT / "rules/codex-user-questions-details.md"
SKILLS = sorted((PLUGIN_ROOT / "codex-skills").glob("*/SKILL.md"))
SKILL_CASES = [pytest.param(skill, id=skill.parent.name) for skill in SKILLS]


@pytest.fixture
def guide() -> str:
    """Text of the short question guide every Codex skill loads."""
    return GUIDE_PATH.read_text(encoding="utf-8")


@pytest.fixture
def details() -> str:
    """Text of the detailed question rules the guide points at."""
    return DETAILS_PATH.read_text(encoding="utf-8")


class TestQuestionGuidance:
    """The shipped guide is small, self-contained and wired into every Codex skill."""

    @pytest.mark.installed_plugin
    def test_guide_is_short_and_points_at_its_details(self, guide: str) -> None:
        """The always-loaded guide stays within its word budget and names an existing details file.

        Every Codex skill loads this guide, so growth costs every invocation; the long clauses live in the details file,
        and the guide must keep the root-owns-questions rule plus a working pointer to them.
        """
        assert len(SKILLS) == 4
        assert len(guide.split()) <= 350
        assert "The root owns user questions" in guide
        assert "codex-user-questions-details.md" in guide
        assert DETAILS_PATH.is_file()

    @pytest.mark.installed_plugin
    @pytest.mark.parametrize("skill", SKILL_CASES)
    def test_skill_loads_local_question_guidance(self, skill: Path) -> None:
        """Each Codex skill links the plugin-local guide and tells the model to consult it before asking.

        A skill that does not reference the independently shipped guide would let a generated question bypass the
        policy; the relative link must also resolve to the guide inside the same installed plugin.
        """
        text = skill.read_text(encoding="utf-8")

        assert f"../../{QUESTION_REFERENCE}" in text
        assert "Before asking" in text
        assert (skill.parent / "../.." / QUESTION_REFERENCE).resolve() == GUIDE_PATH

    @pytest.mark.installed_plugin
    @pytest.mark.parametrize("skill", SKILL_CASES)
    def test_skill_does_not_force_chat_only_or_yes_no_authorization(self, skill: Path) -> None:
        """Skill prompts carry no obsolete claim that Codex lacks a question tool or must ask yes/no in chat.

        Those assumptions predate the question controls; factual or exact-token answers are unaffected, so only the two
        retired phrasings are rejected.
        """
        text = skill.read_text(encoding="utf-8")

        assert "Codex has no `AskUserQuestion`" not in text
        assert "(yes / no)" not in text


class TestQuestionContract:
    """The guide and its details keep every safety clause that bounds how questions grant authority."""

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
            "Require a visible decision key in the answer when conversation context cannot bind it unambiguously.",
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
    def test_keeps_required_safety_obligations(self, obligation: str, guide: str, details: str) -> None:
        """Each required clause, including its negations, appears in the guide or its details file.

        Transport coverage and the complete safety sentences are the contract; a reworded rule that drops a "never" or
        "cannot" would silently widen what a question may authorize, so the clauses are matched whole.
        """
        assert obligation.lower() in f"{guide}\n{details}".lower()

    @pytest.mark.installed_plugin
    def test_async_contract_matches_exposed_schema_and_pending_turn(self, guide: str, details: str) -> None:
        """Async question payloads follow the exposed schema and an accepted question is never buried by a status.

        A payload with top-level ``title``/``options`` is rejected by the tool, and a final or status message after an
        accepted async question hides the unanswered control from the user.
        """
        assert "put `title` and `options` inside its question item" in details
        assert "yield without final/status, including empty final" in guide
        assert "top-level `questions` array" in details
        assert "do not pass either field at the top level" in details
        assert "Inspect the active schema because another host may differ" in details
        assert "Do not append a final or status message after an accepted async question" in details
        assert "Use the actual tool schema: async questions use `title`" not in details

    @pytest.mark.installed_plugin
    def test_single_unchanged_decision_accepts_conversational_reply(self, details: str) -> None:
        """A clear conversational Approve or Deny settles a lone, unchanged decision without weakening binding.

        Exactly one pending decision with no competing question, superseded scope or exact-token requirement may be
        answered in chat; once any of those exist, a generic answer must never replace the decision key.
        """
        assert "Accept an explicit conversational `Approve` / `Deny`" in details
        assert "exactly one pending decision" in details
        assert "no competing question, superseded scope, or exact-token/digest requirement" in details
        assert "do not ask again solely for a missing decision key" in details
        assert "even when it is the only pending decision" not in details
        assert "After supersession, bare `Approve`, indexes, or `all` cannot authorize the replacement" in details
        assert "generic Approve never replaces" in details
        assert "This includes an empty final message" in details
