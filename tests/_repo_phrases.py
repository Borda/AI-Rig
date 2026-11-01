"""Required-phrase lookup shared by the repository tests that pin policy wording."""

from __future__ import annotations


def missing_phrases(text: str, phrases: tuple[str, ...]) -> list[str]:
    """Return the phrases that ``text`` lacks, in the order given."""
    return [phrase for phrase in phrases if phrase not in text]
