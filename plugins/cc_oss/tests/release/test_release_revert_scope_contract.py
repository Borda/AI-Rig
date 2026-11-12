"""Pin release-skill revert handling and the stop-after-redirect scope rule."""

import functools
from pathlib import Path

import pytest
from _skill_source import read_skill_source

_RELEASE = Path(__file__).resolve().parents[2] / "skills/release"


@functools.cache
def _read(relative: str) -> str:
    """Return one shipped release-skill file as text."""
    return read_skill_source(_RELEASE / relative)


@pytest.mark.parametrize(
    ("relative", "phrase"),
    [
        pytest.param("SKILL.md", "conventional `revert:` / `revert(<scope>):` type", id="detect-conventional-revert"),
        pytest.param("SKILL.md", "`This reverts commit <sha>` trailer first", id="detect-trailer-first"),
        pytest.param("SKILL.md", "gets the redirect only", id="scope-redirect-only"),
        pytest.param("SKILL.md", "Never do any part of that skill's work", id="scope-no-partial-work"),
        pytest.param("modes/classify-truth-check.md", "omit from DRAFT.md", id="net-state-draft-scope"),
        pytest.param("modes/classify-truth-check.md", "🔄 Reverted (CHANGELOG only)", id="category-changelog-only"),
        pytest.param("modes/release-draft-template.md", "### Reverted", id="changelog-template-reverted"),
        pytest.param("modes/release-draft-template.md", "DRAFT.md has no Reverted section", id="draft-no-reverted"),
    ],
)
def test_revert_and_scope_rule_present(relative: str, phrase: str) -> None:
    """Each revert or scope rule stays present in its shipped release-skill file.

    A reverted commit must be detected by type and by trailer, kept out of DRAFT.md, and a redirected request must stop
    after the redirect. Removing any of these phrases silently drops one of those behaviours.
    """
    assert phrase in _read(relative)
