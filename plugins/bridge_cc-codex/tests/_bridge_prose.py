"""Whitespace-insensitive Markdown reading shared by the Bridge prose-contract tests."""

from __future__ import annotations

import re
from pathlib import Path

#: A Markdown list item: optional indentation, then a bullet marker or an ordered-list number.
_LIST_ITEM = re.compile(r"^\s*(?:[-*]|\d+\.)\s")


def read_prose(path: Path) -> str:
    """Read a Markdown file with all whitespace runs collapsed so a reflowed paragraph cannot break a phrase check."""
    return " ".join(path.read_text(encoding="utf-8").split())


def instruction_block(lines: list[str], anchor: str) -> list[str]:
    """Return the first line containing ``anchor`` together with the list that belongs to it.

    A restructured instruction keeps one clause per physical line, so a contract that spans several clauses is checked
    against the whole block rather than one line. A lead paragraph owns the list that directly follows it (after one
    blank separator line); a list item owns its more deeply indented sub-items. Every assertion still matches within a
    single physical line, because no line break is inside a pinned phrase.

    Examples:
        >>> lines = ["Lead sentence:", "", "- first", "  - nested", "- second", "", "Next paragraph."]
        >>> instruction_block(lines, "Lead")
        ['Lead sentence:', '- first', '  - nested', '- second']
        >>> instruction_block(lines, "first")
        ['- first', '  - nested']
    """
    start = next(index for index, line in enumerate(lines) if anchor in line)
    lead = lines[start]
    rest = lines[start + 1 :]
    if _LIST_ITEM.match(lead):
        indent = len(lead) - len(lead.lstrip())
        owned = []
        for line in rest:
            if not line.strip() or len(line) - len(line.lstrip()) <= indent:
                break
            owned.append(line)
        return [lead, *owned]
    if rest[:1] == [""]:
        rest = rest[1:]
    owned = []
    for line in rest:
        if not line.strip() or not (_LIST_ITEM.match(line) or line.startswith(" ")):
            break
        owned.append(line)
    return [lead, *owned]
