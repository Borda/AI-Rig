"""Hold the reviewer-attribution policy identical across the copies that state it.

The same paragraph is restated in four places — ``skills/review/SKILL.md`` and ``skills/review/templates/consolidator-
prompt.md`` in both ``cc_oss`` and ``cc_develop``. That the copies read the same, and that the rating legend keeps one
wording per version, is held by the ``check-policy-parity`` hook (``scripts/check_policy_parity.py``): a drifted copy is
bookkeeping, not broken behaviour. What stays here is what each copy must say, because an instruction that agrees with
its twin can still instruct the wrong thing.

Current reports refine five source judgments; historical reports retain their versioned wording. Preserving historical
rendering must not leave an active review skill on it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGINS_DIR = REPO_ROOT / "plugins"

REVIEW_PLUGINS = ("cc_oss", "cc_develop")
SKILL_MARKER = "> Review presentation is additive:"
CONSOLIDATOR_MARKER = "**Reviewer attribution (additive):**"

HISTORICAL_LEGEND = (
    "Legend: 1 = Approve · 2 = Minor changes · 3 = Changes required · 4 = Insufficient evidence · 5 = Block / Reject."
)
CURRENT_LEGEND = "Legend: 1 = Approve · 2 = Minor changes · 3 = Major revision · 4 = Insufficient, but a good initiative · 5 = Block."


_LIST_ITEM = re.compile(r"^(?:>\s*)?-\s")


def _paragraph(path: Path, marker: str) -> str:
    """Return the policy block of ``path`` that starts with ``marker``.

    The policy is a lead-in line followed by a bullet list (optionally inside a blockquote). Comparing only the
    lead-in line would leave every bullet unguarded, so the block runs from the marker line through the end of the
    list that follows it.

    Args:
        path: File expected to state the policy exactly once.
        marker: Leading text identifying the block.

    Returns:
        The block's lines joined by newlines, each stripped of trailing whitespace.
    """
    lines = [line.rstrip() for line in path.read_text(encoding="utf-8").splitlines()]
    starts = [i for i, line in enumerate(lines) if line.startswith(marker)]
    assert len(starts) == 1, f"{path.relative_to(REPO_ROOT)}: expected exactly one {marker!r} line, found {len(starts)}"
    block = [lines[starts[0]]]
    rest = lines[starts[0] + 1 :]
    while rest and rest[0] in {"", ">"}:
        rest = rest[1:]
    while rest and _LIST_ITEM.match(rest[0]):
        block.append(rest.pop(0))
    return "\n".join(block)


#: Shared policy blocks: the file each plugin restates and the line that opens the block.
_POLICY_BLOCKS = (
    pytest.param("skills/review/SKILL.md", SKILL_MARKER, id="skill"),
    pytest.param("skills/review/templates/consolidator-prompt.md", CONSOLIDATOR_MARKER, id="consolidator"),
)


class TestAttributionParagraph:
    """The reviewer-attribution paragraph reads the same in every plugin that states it."""

    @pytest.mark.parametrize("plugin", REVIEW_PLUGINS)
    @pytest.mark.parametrize(("relative", "marker"), _POLICY_BLOCKS)
    def test_states_role_names_only(self, plugin: str, relative: str, marker: str) -> None:
        """Each copy labels reviewers as ``Role [rating]`` by role name only, never by a parent substitute.

        Both copies being identical would still be wrong if they agreed on a policy that attributes reviews to the
        parent agent instead of the reviewing role.
        """
        text = _paragraph(PLUGINS_DIR / plugin / relative, marker)

        assert ("Role [rating]" in text, "role names only" in text, "label parent substitutes" in text.lower()) == (
            True,
            True,
            False,
        )


class TestRatingLegend:
    """The active review skills quote the current five-stage rating legend."""

    @pytest.mark.parametrize("plugin", REVIEW_PLUGINS)
    def test_active_review_skill_uses_current_legend(self, plugin: str) -> None:
        """An archived legend must not become the instruction for a newly produced review.

        The review skill is what a reviewer follows when producing a rating, so it must quote only the current legend.
        """
        text = (PLUGINS_DIR / plugin / "skills/review/SKILL.md").read_text(encoding="utf-8")

        assert (CURRENT_LEGEND in text, HISTORICAL_LEGEND in text) == (True, False)
