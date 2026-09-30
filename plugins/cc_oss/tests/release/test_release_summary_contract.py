"""Pin the release draft Summary contract: a short pitch that re-folds on every incremental cycle."""

from pathlib import Path

import pytest

_RELEASE = Path(__file__).resolve().parents[2] / "skills/release"


def _read(relative: str) -> str:
    """Return one shipped release-skill file as text."""
    return (_RELEASE / relative).read_text(encoding="utf-8")


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
        pytest.param("modes/release-draft-template.md", "Edit the main list to the top 5", id="append-fold-top-five"),
        pytest.param(
            "modes/release-draft-template.md",
            "replace (never append to) it on later cycles",
            id="append-pointer-replaced",
        ),
        pytest.param("modes/release-draft-template.md", "re-derive it every cycle", id="append-upgrade-call-rederived"),
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
def test_summary_contract_phrase_present(relative: str, phrase: str) -> None:
    """Each Summary rule stays stated in the file that owns it."""
    assert phrase in _read(relative)


def test_summary_is_not_a_paragraph_placeholder() -> None:
    """The template no longer asks for the old multi-sentence Summary paragraph."""
    assert "2–4 sentence" not in _read("templates/release-draft.md")
