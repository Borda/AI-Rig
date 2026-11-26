"""Guard the short terminal review table: the delivery hook and the review skill name the same file-only fields."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_PLUGIN = Path(__file__).resolve().parents[2]
_HOOK = _PLUGIN / "hooks" / "report-header-table.js"
_SKILL = _PLUGIN / "skills" / "review" / "SKILL.md"
_POST_AGENT = _PLUGIN / "skills" / "review" / "modes" / "post-agent.md"


def _hook_file_only_fields() -> list[str]:
    """Return the field names the hook lets a review report leave out of its terminal table."""
    source = _HOOK.read_text(encoding="utf-8")
    block = re.search(r"REVIEW_FILE_ONLY_FIELDS = new Set\(\[(.*?)\]\)", source, re.DOTALL)
    assert block is not None, "the hook no longer defines REVIEW_FILE_ONLY_FIELDS"
    return re.findall(r'"([^"]+)"', block.group(1))


def _line_with(path: Path, needle: str) -> str:
    """Return the first line of ``path`` that contains ``needle``."""
    return next(line for line in path.read_text(encoding="utf-8").splitlines() if needle in line)


def test_the_hook_defines_the_expected_file_only_fields() -> None:
    """The omitted set is the eight bookkeeping fields named in the user-facing rule."""
    assert _hook_file_only_fields() == [
        "Link",
        "Date",
        "Commit SHA",
        "Agents",
        "Review contract",
        "Rating scale",
        "Readiness",
        "Gate",
    ]


@pytest.mark.parametrize(
    ("path", "needle"),
    [
        pytest.param(_SKILL, "The terminal table is shorter than the report file", id="skill"),
        pytest.param(_POST_AGENT, "without the file-only rows", id="post-agent"),
    ],
)
def test_review_instructions_name_every_file_only_field(path: Path, needle: str) -> None:
    """Each instruction that tells the parent what to print lists exactly the fields the hook allows to be left out."""
    line = _line_with(path, needle)
    assert [name for name in _hook_file_only_fields() if f"`{name}`" not in line] == []
    assert "[#N](<Link value>)" in line or "`[#N](<Link value>)`" in line


def test_selected_reuse_delivers_the_normal_report_with_saved_provenance() -> None:
    """Reusing a published review enters the same final reply as a fresh review, even after a push."""
    preflight = _SKILL.parent / "modes" / "preflight.md"
    request = _line_with(preflight, "show the last completed report selects (b)")
    assert "using `PRIOR_REPORT`" in request
    assert "newer unpublished run" in request
    reuse = _line_with(preflight, "Selected (b)")
    assert "Step 5b" in reuse
    assert "Step 7b" in reuse
    assert "no-question" in reuse
    provenance = _line_with(preflight, "unreviewed")
    assert "PRIOR_SHA" in provenance
    assert "HEAD_SHA" in provenance
    assert "print its path and stop" not in _line_with(preflight, "(b) Reuse")


def test_explicit_fresh_review_collects_current_discussion_on_the_same_commit() -> None:
    """An unchanged SHA cannot divert a requested re-review of new online comments to saved evidence."""
    preflight = _SKILL.parent / "modes" / "preflight.md"
    request = _line_with(preflight, "An explicit request for a fresh full review")
    assert "selects (a) directly" in request
    assert "unchanged head" in request
    rerun = _line_with(preflight, "A fresh full PR review with no usable prior report or after selecting (a)")
    assert "conversation collection block" in rerun
    text = preflight.read_text(encoding="utf-8")
    for source in ("review-bodies", "inline-comments", "general-comments"):
        assert source in text
    assert "github-review-parsing.md" in text
    assert "conversation-coverage.txt" in text
    assert "An empty conversation file is unavailable" in text
