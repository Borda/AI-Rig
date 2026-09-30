"""Hold the reviewer-attribution policy identical across the copies that state it.

The same paragraph is restated in four unguarded places — ``skills/review/SKILL.md`` and
``skills/review/templates/consolidator-prompt.md`` in both ``cc_oss`` and ``cc_develop``. None of them is a
``propagate_shared.py`` entry, because the surrounding files differ wholly and only this paragraph is shared, so a
change to one copy leaves the others stating the old policy with nothing to detect it.

That gap is not theoretical: the ``cc_oss`` copy of this very paragraph shipped inside the ``Agent Resolution`` shell
fence while the ``cc_develop`` copy sat correctly outside it. ``test_skill_shell_fence_prose.py`` catches that
structural mistake; this file catches the other half — the two copies drifting in wording.

The rating legend has a wider blast radius still: it is rendered by ``final_handoff.py`` and written into report
templates, so a reader comparing a Codex handoff against a Claude review report must see one sentence, not two near-
identical ones.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_DIR = REPO_ROOT / "plugins"

REVIEW_PLUGINS = ("cc_oss", "cc_develop")
SKILL_MARKER = "> Review presentation is additive:"
CONSOLIDATOR_MARKER = "**Reviewer attribution (additive):**"

# The legend is quoted inside Python string literals and test assertions as well as Markdown prose, so the
# sentence is matched up to its terminating period rather than to end of line.
_LEGEND = re.compile(r"Legend: 1 = Approve[^\n]*?Reject\.")
CANONICAL_LEGEND = (
    "Legend: 1 = Approve · 2 = Minor changes · 3 = Changes required · 4 = Insufficient evidence · 5 = Block / Reject."
)


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


@pytest.mark.parametrize(
    ("relative", "marker"),
    [
        pytest.param("skills/review/SKILL.md", SKILL_MARKER, id="skill"),
        pytest.param("skills/review/templates/consolidator-prompt.md", CONSOLIDATOR_MARKER, id="consolidator"),
    ],
)
def test_reviewer_attribution_paragraph_is_identical_across_plugins(relative: str, marker: str) -> None:
    """Fail when one plugin's copy of the shared policy states something the other does not."""
    variants = {plugin: _paragraph(PLUGINS_DIR / plugin / relative, marker) for plugin in REVIEW_PLUGINS}
    assert len(set(variants.values())) == 1, f"copies drifted: {variants}"


def test_rating_legend_has_one_wording_everywhere() -> None:
    """Keep one legend sentence across skills, templates, renderer and tests."""
    found: dict[str, list[str]] = {}
    for path in sorted(PLUGINS_DIR.rglob("*")):
        if path.suffix not in {".md", ".py", ".json"} or not path.is_file():
            continue
        for sentence in _LEGEND.findall(path.read_text(encoding="utf-8")):
            found.setdefault(sentence, []).append(str(path.relative_to(REPO_ROOT)))
    assert found, "the rating legend is stated nowhere — the feature it belongs to was removed or renamed"
    assert set(found) == {CANONICAL_LEGEND}, f"legend wordings diverged: {sorted(found)}"
