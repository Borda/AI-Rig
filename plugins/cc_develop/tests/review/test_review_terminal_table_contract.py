"""Guard the short terminal review table: the delivery hook and the review skill name the same file-only fields."""

from __future__ import annotations

import re
from pathlib import Path

_PLUGIN = Path(__file__).resolve().parents[2]
_HOOK = _PLUGIN / "hooks" / "report-header-table.js"
_SKILL = _PLUGIN / "skills" / "review" / "SKILL.md"


def _hook_file_only_fields() -> list[str]:
    """Return the field names the hook lets a review report leave out of its terminal table."""
    source = _HOOK.read_text(encoding="utf-8")
    block = re.search(r"REVIEW_FILE_ONLY_FIELDS = new Set\(\[(.*?)\]\)", source, re.DOTALL)
    assert block is not None, "the hook no longer defines REVIEW_FILE_ONLY_FIELDS"
    return re.findall(r'"([^"]+)"', block.group(1))


def test_the_review_skill_names_every_file_only_field() -> None:
    """The instruction that tells the parent what to print lists exactly the fields the hook may leave out."""
    line = next(
        line
        for line in _SKILL.read_text(encoding="utf-8").splitlines()
        if "The terminal table is shorter than the report file" in line
    )
    assert _hook_file_only_fields()
    assert [name for name in _hook_file_only_fields() if f"`{name}`" not in line] == []
    assert "[#N](<Link value>)" in line
