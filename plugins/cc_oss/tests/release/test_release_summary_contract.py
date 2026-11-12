"""Pin the release draft Summary contract: a short pitch that re-folds on every incremental cycle.

The rules live in prose across the draft template, the writing rules and the append and truth-check modes. Each file is
read with whitespace collapsed, so a reflowed paragraph cannot break a phrase assertion.
"""

from pathlib import Path

import pytest
from _skill_source import read_skill_source

_RELEASE = Path(__file__).resolve().parents[2] / "skills/release"


def _read(relative: str) -> str:
    """Return one shipped release-skill file as text with every whitespace run collapsed to a single space."""
    return " ".join(read_skill_source(_RELEASE / relative).split())


class TestSummaryContract:
    """The Summary pitch rules stay stated in the files that own them."""

    @pytest.mark.parametrize(
        ("relative", "phrase"),
        [
            pytest.param("templates/release-draft.md", "[Release hook]", id="template-hook"),
            pytest.param("templates/release-draft.md", "one to five real win bullets", id="template-win-count"),
            pytest.param("templates/release-draft.md", "never pad", id="template-no-padding"),
            pytest.param("templates/release-draft.md", "split or distill long lines", id="template-long-lines"),
            pytest.param(
                "templates/release-draft.md",
                "Put breaking or removed items in the upgrade call",
                id="template-upgrade-call",
            ),
            pytest.param("templates/release-draft.md", "New since last draft", id="template-append-pointer"),
            pytest.param("guidelines/writing-rules.md", "No PR refs", id="rules-no-pr-refs"),
            pytest.param("guidelines/writing-rules.md", "every section except Summary", id="rules-pr-ref-exception"),
            pytest.param(
                "modes/release-draft-template.md", "Edit the main list to the top 5", id="append-fold-top-five"
            ),
            pytest.param(
                "modes/release-draft-template.md",
                "replace (never append to) it on later cycles",
                id="append-pointer-replaced",
            ),
            pytest.param(
                "modes/release-draft-template.md", "re-derive it every cycle", id="append-upgrade-call-rederived"
            ),
            pytest.param(
                "modes/release-draft-template.md", "**Summary shape and duplication**", id="post-merge-summary-check"
            ),
            pytest.param(
                "modes/classify-truth-check.md",
                "Summary hook, win bullets and upgrade call carry no refs",
                id="evidence-link-summary-exempt",
            ),
            pytest.param("guidelines/numbers-reference.md", "Release Summary pitch", id="limits-documented"),
        ],
    )
    def test_rule_phrase_present(self, relative: str, phrase: str) -> None:
        """Each Summary rule stays stated in the file that owns it.

        The template, writing rules, append mode and truth-check mode each carry part of the one-to-five-bullet pitch
        contract; losing a phrase from any of them lets that file drift back to the old long Summary.
        """
        assert phrase in _read(relative)

    def test_template_drops_paragraph_placeholder(self) -> None:
        """The template no longer asks for the old multi-sentence Summary paragraph.

        A leftover ``2–4 sentence`` placeholder would make drafts grow a paragraph incremental cycles cannot re-fold.
        """
        assert "2–4 sentence" not in _read("templates/release-draft.md")
