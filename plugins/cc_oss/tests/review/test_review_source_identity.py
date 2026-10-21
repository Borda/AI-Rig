"""Bind every PR review publication route to frozen full commit provenance."""

from pathlib import Path

_REVIEW = Path(__file__).resolve().parents[2] / "skills/review"


def test_normal_and_terminal_publications_show_frozen_sha() -> None:
    """Both publishers visibly retain the reviewed PR head, never the invoking checkout's HEAD."""
    skill = (_REVIEW / "SKILL.md").read_text(encoding="utf-8")
    template = (_REVIEW / "templates/review-report.md").read_text(encoding="utf-8")
    prompt = (_REVIEW / "templates/consolidator-prompt.md").read_text(encoding="utf-8")
    terminal = skill.split("Orchestrator writes `$REPORT_DIR/review-report.md` itself", 1)[1].split(
        "Then jump straight", 1
    )[0]
    assert "Commit SHA:" in template
    assert "Commit SHA:" in terminal
    assert "REVIEWED_COMMIT_SHA=<" in skill
    assert "`Commit SHA:`" in prompt
    assert "headRefOid" in prompt
    assert "invoking" in prompt


def test_fill_gaps_never_upgrades_abbreviated_provenance() -> None:
    """Legacy carry identity stays exact while its unavailable full SHA is explicit."""
    skill = (_REVIEW / "SKILL.md").read_text(encoding="utf-8")
    prompt = (_REVIEW / "templates/consolidator-prompt.md").read_text(encoding="utf-8")
    assert "legacy abbreviated" in skill
    assert "legacy abbreviated" in prompt
    assert "head_sha" in prompt
    assert "Preserve a supported source judgment" in prompt
    assert "independence" in prompt
