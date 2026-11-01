"""Check phrases and sections of instruction text that contract tests read from SKILL.md and reference files."""

from __future__ import annotations

from _skill_text import flat


def absent_phrases(text: str, *phrases: str) -> list[str]:
    """Return the phrases missing from ``text``, so one assertion names every lost requirement at once.

    Matching is verbatim: collapse whitespace first (``_skill_text.flat``) when the source wraps its prose.
    """
    return [phrase for phrase in phrases if phrase not in text]


def h2_section(text: str, heading: str) -> str:
    """Return the whitespace-collapsed body of the ``## heading`` section, up to the next ``##`` heading."""
    lines = text.splitlines()
    body = []
    for line in lines[lines.index(f"## {heading}") + 1 :]:
        if line.startswith("## "):
            break
        body.append(line)
    return flat("\n".join(body))
