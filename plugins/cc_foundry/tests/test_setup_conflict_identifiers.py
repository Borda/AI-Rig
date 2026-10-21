"""Keep the setup skill's per-entry conflict approvals matchable by the step that applies them.

``/foundry:setup`` asks the user which conflicting entries to replace (Phase 3, option c) and appends each approved
entry's identifier to a session file. Phase 4 then links an entry only when ``_approved`` finds that identifier verbatim
in that file. The two halves live far apart in ``skills/setup/SKILL.md`` — one in prose, one in a bash block — so
nothing but this test notices when their formats drift.

They did drift once: Phase 3 recorded the bare basename ``foundry-<name>.md`` while Phase 4 asked for
``rules/foundry-<name>.md``, so every rule the user explicitly approved was skipped as "not approved".

They drifted a second way: Phase 3 wrote the file from a model-context array in a fresh Bash call, which expanded to
nothing, so the file held one blank line and every approval was silently skipped.
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
    """Return the Phase 3 sentence that defines what gets appended to the approved-entries file."""
    lines = [line for line in text.splitlines() if "append entry's identifier" in line]
    assert len(lines) == 1, f"expected one Phase 3 identifier rule, found {len(lines)}"
    return lines[0]


def _phase4_needles(text: str) -> list[str]:
    """Return every literal argument Phase 4 passes to ``_approved``."""
    return re.findall(r'_approved "([^"]+)"', text)


def _approved_file_expr(text: str) -> str:
    """Return the shell path expression Phase 4 assigns to ``_APPR`` and greps for approvals."""
    match = re.search(r'_APPR="([^"]+)"', text)
    assert match is not None, "Phase 4 no longer assigns _APPR"
    return match.group(1)


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


class TestApprovedEntriesPersistence:
    """Phase 3 approvals must reach Phase 4 through the file, never through model-context state."""

    def test_no_model_context_array_remains(self, skill_text: str) -> None:
        """The approved list is not held in a shell array that a later Bash call cannot see.

        Every Bash call starts a fresh shell, so an array named in prose and expanded in a later call is empty there;
        the file then holds one blank line and Phase 4 skips every entry the user ticked.
        """
        assert "APPROVED_CONFLICT_ENTRIES" not in skill_text

    def test_phase3_appends_to_the_file_phase4_reads(self, skill_text: str) -> None:
        """Exactly one Phase 3 writer appends to the path Phase 4 greps, without expanding an array.

        The writer and the ``_approved`` reader share only that path expression, so a rename on one side would make
        every approval invisible to the other.
        """
        writers = [line for line in skill_text.splitlines() if f'>> "{_approved_file_expr(skill_text)}"' in line]
        assert len(writers) == 1
        assert "[@]" not in writers[0]

    def test_phase3_writer_aborts_on_unsubstituted_placeholder(self, skill_text: str) -> None:
        """The writer refuses to record a ``<entry>`` placeholder the model forgot to replace.

        Recording the literal placeholder would look like success while approving nothing.
        """
        writers = [line for line in skill_text.splitlines() if f'>> "{_approved_file_expr(skill_text)}"' in line]
        assert "case \"$ENTRY\" in *'<'*'>'*|\"\") echo" in writers[0]

    def test_phase2_empties_the_file(self, skill_text: str) -> None:
        """Phase 2 truncates the approved file so a prior run's approvals never leak into this run.

        With the array gone this truncation is also what makes "no entry ticked" a valid empty list.
        """
        assert f': > "{_approved_file_expr(skill_text)}"' in skill_text
