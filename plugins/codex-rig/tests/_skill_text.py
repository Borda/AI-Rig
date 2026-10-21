"""Read skill instruction text that spans a SKILL.md and its trigger-loaded reference files.

A skill may move a recovery or lifecycle section into ``references/<name>.md`` and load it only when its trigger
applies. A contract test then checks the union of both files, so the rule stays pinned wherever it lives.
"""

from __future__ import annotations

from pathlib import Path


def read_with_references(skill_md: Path, *references: str) -> str:
    """Return a skill's SKILL.md text followed by the named ``references/`` files.

    Args:
        skill_md: Path to the skill's ``SKILL.md``.
        *references: File names under the skill's ``references/`` directory, in the order to append them.

    Returns:
        The SKILL.md text and each named reference joined by newlines.
    """
    parts = [skill_md.read_text(encoding="utf-8")]
    parts.extend((skill_md.parent / "references" / name).read_text(encoding="utf-8") for name in references)
    return "\n".join(parts)


def read_reference_body(reference: Path, title: str) -> str:
    """Return the text after a moved section's ``# <title>`` heading, asserting the reference opens with it.

    The moved section keeps its original heading text, so its anchor and its title stay unchanged.

    Args:
        reference: Path to the reference file holding the moved section.
        title: Heading text the reference must open with.

    Returns:
        The reference text after its title line.
    """
    text = reference.read_text(encoding="utf-8")
    heading = f"# {title}\n"
    assert text.startswith(heading), f"{reference.name} must open with {heading!r}"
    return text[len(heading) :]
