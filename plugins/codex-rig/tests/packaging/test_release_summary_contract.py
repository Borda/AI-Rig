"""Pin the Codex release draft Summary contract, mirrored from the Claude release skill."""

from pathlib import Path

import pytest

_RELEASE = Path(__file__).resolve().parents[2] / "skills/release"


@pytest.mark.parametrize(
    ("name", "phrase"),
    [
        pytest.param("release-draft.md", "[Release hook]", id="template-hook"),
        pytest.param("release-draft.md", "one to five win bullets", id="template-win-count"),
        pytest.param("release-draft.md", "never pad", id="template-no-padding"),
        pytest.param("release-draft.md", "New since last draft", id="template-append-pointer"),
        pytest.param("release-writing.md", "No PR refs or count dumps", id="writing-no-pr-refs"),
        pytest.param(
            "release-writing.md", "Re-rank Summary wins across the whole release (at most five)", id="append-fold"
        ),
        pytest.param("release-writing.md", "re-derive the upgrade-call line every cycle", id="append-upgrade-call"),
        pytest.param("release-writing.md", "replace it each cycle, never append", id="append-pointer-replaced"),
    ],
)
def test_summary_contract_phrase_present(name: str, phrase: str) -> None:
    """Each Summary rule stays stated in the Codex file that owns it."""
    assert phrase in (_RELEASE / name).read_text(encoding="utf-8")
