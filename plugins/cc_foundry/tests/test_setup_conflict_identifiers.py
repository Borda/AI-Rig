"""Keep the setup skill's per-entry conflict approvals matchable by the step that applies them.

``/foundry:setup`` asks the user which conflicting entries to replace (Phase 3, option c) and records each approved
entry's identifier in ``APPROVED_CONFLICT_ENTRIES``. Phase 4 then links an entry only when ``_approved`` finds that
identifier verbatim. The two halves live far apart in ``skills/setup/SKILL.md`` — one in prose, one in a bash block — so
nothing but this test notices when their formats drift.

They did drift once: Phase 3 recorded the bare basename ``foundry-<name>.md`` while Phase 4 asked for
``rules/foundry-<name>.md``, so every rule the user explicitly approved was skipped as "not approved".
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parents[1] / "skills" / "setup" / "SKILL.md"


@pytest.fixture(scope="module")
def skill_text() -> str:
    """Read the setup skill source once for every contract check."""
    return SKILL.read_text(encoding="utf-8")


def _phase3_identifiers(text: str) -> str:
    """Return the Phase 3 sentence that defines what gets appended to ``APPROVED_CONFLICT_ENTRIES``."""
    lines = [line for line in text.splitlines() if "append entry's identifier" in line]
    assert len(lines) == 1, f"expected one Phase 3 identifier rule, found {len(lines)}"
    return lines[0]


def _phase4_needles(text: str) -> list[str]:
    """Return every literal argument Phase 4 passes to ``_approved``."""
    return re.findall(r'_approved "([^"]+)"', text)


class TestApprovedConflictIdentifiers:
    """Phase 3 must record approvals in the exact form Phase 4 looks up."""

    def test_rule_identifier_carries_rules_prefix(self, skill_text: str) -> None:
        """Approved rule entries are recorded as ``rules/foundry-<name>.md``, the form Phase 4 checks.

        Phase 4 builds ``base="foundry-$(basename "$src")"`` and calls ``_approved "rules/$base"``; a bare basename in
        Phase 3 would never compare equal, so an approved rule would be skipped.
        """
        assert "rules/$base" in _phase4_needles(skill_text)
        assert 'base="foundry-$(basename "$src")"' in skill_text
        assert "`rules/foundry-<name>.md` for rules" in _phase3_identifiers(skill_text)

    def test_team_protocol_identifier_matches(self, skill_text: str) -> None:
        """``TEAM_PROTOCOL.md`` is recorded and looked up under the same bare name.

        It lives at the root of ``~/.claude/``, so Phase 2 names it without a directory and both halves agree.
        """
        assert "TEAM_PROTOCOL.md" in _phase4_needles(skill_text)
        assert "`TEAM_PROTOCOL.md`" in _phase3_identifiers(skill_text)
