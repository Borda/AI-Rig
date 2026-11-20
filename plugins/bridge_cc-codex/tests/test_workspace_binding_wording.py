"""Pin the reviewer-facing wording of ``bridge_bind_workspace`` and the rejection recovery in the Codex skills.

Codex's automatic reviewer once refused the bind call for lacking a native confirmation that only the call itself can
raise. These contract tests keep the description, every skill that calls the tool, the shared consent rule and the
troubleshooting docs from drifting back to a confirm-before-calling reading or to a recovery that drops the peer step.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
if str(PLUGIN / "bin") not in sys.path:
    sys.path.insert(0, str(PLUGIN / "bin"))
import bridge_mcp  # noqa: E402

#: Every skill that tells Codex to call the bind tool, each carrying the same rejection recovery.
BINDING_SKILLS = ("advise", "review", "implement", "setup")
#: Phrases one physical line of each binding skill must carry together.
RECOVERY_PHRASES = (
    "call it alone",
    "host reviewer rejects it",
    "report the reason",
    "retry once",
    "second rejection stops",
    "workspace binding outcomes",
)
#: Skills that dispatch to the peer and so must never drop its step silently.
DISPATCH_SKILLS = ("advise", "review", "implement")
#: Phrase every dispatch skill, in both host trees, carries on the line that defines the peer task context.
CONTEXT_PHRASES = ("give the peer the context it lacks", "relative to the bound workspace", "prompted")
#: Phrases the shared consent rule needs for the recovery rule and the bind outcome table.
CONSENT_RULE_PHRASES = (
    "once the user explicitly approves in the current turn",
    "a user-approved retry is not a bypass",
    "never silently drop the peer step",
    "## workspace binding outcomes",
    "error `-32602`",
    "error `-32002`",
    "error `-32001`",
    "error `-32003`",
)


def _bind_description() -> str:
    """Return the description the Bridge server advertises for the bind tool."""
    tools = {tool["name"]: tool for tool in bridge_mcp.tool_definitions()}
    return tools[bridge_mcp.BIND_TOOL_NAME]["description"]


def _lines(relative: str) -> list[str]:
    """Return the lowercased physical lines of one shipped file."""
    return (PLUGIN / relative).read_text(encoding="utf-8").lower().splitlines()


class TestBindDescription:
    """The advertised description says what the call does, not what must already have happened."""

    def test_states_that_the_call_opens_the_form_and_sends_nothing(self) -> None:
        """A reviewer reading the description sees a form-raising call with no provider transfer."""
        text = _bind_description().lower()

        assert "open a native form" in text
        assert "before any confirmation exists" in text
        assert "sends nothing to a provider" in text
        assert "grants nothing" in text

    @pytest.mark.parametrize("stale", ["only after the user confirms", "bind this process only after"])
    def test_has_no_confirm_before_calling_precondition(self, stale: str) -> None:
        """The wording that made confirmation read as a precondition of the call stays gone."""
        assert stale not in _bind_description().lower()

    def test_ties_the_call_to_an_authorized_task(self) -> None:
        """The description does not invite binding folders when no Bridge task needs one."""
        assert "authorized bridge task needs a workspace binding" in _bind_description().lower()


class TestRejectionRecovery:
    """Each skill that binds a workspace tells Codex how to recover from a host-reviewer rejection."""

    @pytest.mark.parametrize("skill", BINDING_SKILLS)
    def test_skill_carries_the_recovery_on_one_line(self, skill: str) -> None:
        """One line names alone-call, reviewer rejection, report, a single retry, the stop and the outcome table."""
        lines = _lines(f"codex-skills/{skill}/SKILL.md")

        assert any(all(phrase in line for phrase in RECOVERY_PHRASES) for line in lines)

    @pytest.mark.parametrize("skill", DISPATCH_SKILLS)
    def test_dispatch_skill_never_drops_the_peer_step_silently(self, skill: str) -> None:
        """A rejected bind in a dispatching skill is reported, never turned into a silent skip."""
        assert any("never silently drop the claude step" in line for line in _lines(f"codex-skills/{skill}/SKILL.md"))

    def test_consent_rule_defines_one_recovery_and_the_bind_outcomes(self) -> None:
        """The shared rule covers all five tools' rejection recovery and each server-side bind outcome."""
        text = "\n".join(_lines("rules/provider-consent.md"))

        assert [phrase for phrase in CONSENT_RULE_PHRASES if phrase not in text] == []


class TestPeerTaskContext:
    """Every dispatching skill, in both host trees, tells the caller to give its peer a self-contained task."""

    @pytest.mark.parametrize("tree", ["codex-skills", "claude-skills"])
    @pytest.mark.parametrize("skill", DISPATCH_SKILLS)
    def test_skill_states_the_context_rule(self, tree: str, skill: str) -> None:
        """One line carries the context rule with its base path, so no copy drifts from the others."""
        lines = _lines(f"{tree}/{skill}/SKILL.md")

        assert any(all(phrase in line for phrase in CONTEXT_PHRASES) for line in lines)

    def test_split_review_repeats_the_rule_for_every_scope(self) -> None:
        """A split review gives each scope its own context, since every scope is an independent peer call."""
        assert any("`--split-file`, repeat this for every scope" in line for line in _lines("claude-skills/review/SKILL.md"))


class TestShippedDocs:
    """The troubleshooting row and the tool table describe the same call behaviour as the server."""

    def test_operations_row_does_not_promise_a_diagnosis_the_check_cannot_give(self) -> None:
        """The rejection row says the consent check cannot show what the reviewer sees."""
        rows = [line for line in _lines("docs/operations.md") if line.startswith("| auto-review rejects")]

        assert len(rows) == 1
        assert "cannot show what the reviewer sees" in rows[0]
        assert "retry the bind once, alone" in rows[0]

    def test_readme_tool_row_says_the_call_raises_the_form(self) -> None:
        """The tool table tells readers the bind runs before any confirmation exists."""
        rows = [line for line in _lines("README.md") if line.startswith("| `bridge_bind_workspace`")]

        assert len(rows) == 1
        assert "raises that form" in rows[0]
        assert "sends nothing to a provider" in rows[0]


class TestShippedConsentPolicy:
    """The standing consent a standalone user installs names the zero-provider calls and keeps v1 recognisable."""

    def test_current_block_covers_bind_and_status_before_any_binding(self) -> None:
        """The v2 block tells the reviewer the bind opens a form and needs no earlier binding."""
        text = "\n".join(_lines("rules/claude-transfer-policy.txt"))

        assert "bridge:claude-transfer-consent:v2 begin" in text
        assert "bridge:claude-transfer-consent:v2 end" in text
        assert "bridge_status and bridge_bind_workspace are also covered and need no earlier binding" in text
        assert "sends nothing to a provider" in text
        assert "a retry the user explicitly approved in the current turn is not a bypass" in text

    def test_superseded_block_stays_exactly_v1(self) -> None:
        """The legacy file keeps the v1 markers and never mentions the bind tool, so it matches installed grants."""
        text = "\n".join(_lines("rules/claude-transfer-policy-v1.txt"))

        assert "bridge:claude-transfer-consent:v1 begin" in text
        assert "bridge_bind_workspace" not in text
