"""Keep bridge delegation compatible with explicitly bounded combined changes."""

from pathlib import Path

import pytest

RULES = Path(__file__).resolve().parents[2] / "rules"


@pytest.mark.parametrize("relative", ["quality-gates.md", "_full/quality-gates.md"])
def test_bridge_checklist_preserves_clean_tree_default_and_scoped_exception(relative: str) -> None:
    """An invoking skill may retain its documented combined-change flow without relaxing other checks."""
    lines = (RULES / relative).read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith("## Write-Delegation Checklist"))
    end = next((index for index in range(start + 1, len(lines)) if lines[index].startswith("## ")), len(lines))
    section = "\n".join(lines[start + 1 : end])

    assert "git tree first" in section.lower()
    assert "unless the invoking skill explicitly documents a combined-change exception" in section
    assert "bounded owned paths" in section
    assert "recorded starting snapshot" in section
    assert "combined-diff review and proof commands" in section
    assert "only that invocation may use the exception" in section
    assert "the bridge never commits" in section
