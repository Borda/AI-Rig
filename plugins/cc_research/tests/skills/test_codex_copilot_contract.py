"""Check the co-pilot's bounded exception for reviewing one combined change set."""

from pathlib import Path

_PLUGIN = Path(__file__).resolve().parents[2]


def test_phase2c_names_the_clean_tree_checklist_exception() -> None:
    """The second pass explicitly permits Claude's pending edit for combined review."""
    lines = (_PLUGIN / "skills/run/modes/codex-copilot.md").read_text(encoding="utf-8").splitlines()
    exception = next(line for line in lines if line.startswith("> **Clean-tree exception.**"))
    assert "sanctioned exception to the clean-tree step of the Write-Delegation Checklist" in exception
    assert "`bridge:implement`" in exception
    assert "Claude's uncommitted Phase 2 edit" in exception
    assert "Claude and Codex edits are reviewed together as one combined change set" in exception


def test_clean_tree_exception_preserves_existing_safeguards() -> None:
    """The exception retains path limits, the snapshot, review, proof, and commit ownership."""
    lines = (_PLUGIN / "skills/run/modes/codex-copilot.md").read_text(encoding="utf-8").splitlines()
    exception = next(line for line in lines if line.startswith("> **Clean-tree exception.**"))
    assert "All other checklist steps remain in force" in exception
    assert "owned paths" in exception
    assert "`scope_files`" in exception
    assert "iteration-start SHA snapshot" in exception
    assert "Phase 3 scope check" in exception
    assert "read the full combined diff" in exception
    assert "run the actual proof commands" in exception
    assert "the bridge never commits" in exception


def test_readme_explains_the_combined_change_set_exception() -> None:
    """Users can discover the clean-tree exception beside the co-pilot integration flag."""
    lines = (_PLUGIN / "README.md").read_text(encoding="utf-8").splitlines()
    assert any(
        line.startswith("- `--codex` co-pilot")
        and "clean-tree step of the Write-Delegation Checklist" in line
        and "one combined change set" in line
        for line in lines
    )
