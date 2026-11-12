"""Bind every PR review publication route to frozen full commit provenance."""

from functools import cache
from pathlib import Path

from _skill_source import read_skill_source

_REVIEW = Path(__file__).resolve().parents[2] / "skills/review"


@cache
def _read(relative: str) -> str:
    """Read one shipped review file; the text is immutable during a run."""
    return read_skill_source(_REVIEW / relative)


def test_normal_and_terminal_publications_show_frozen_sha() -> None:
    """Both publishers visibly retain the reviewed PR head, never the invoking checkout's HEAD.

    The normal report template and the terminal-publication path of the skill each carry a ``Commit SHA:`` line, and the
    consolidator prompt names the frozen ``headRefOid`` source, so a report cannot cite whichever checkout ran it.
    """
    skill = _read("SKILL.md") + _read("modes/acceptance-gate.md") + _read("modes/post-agent.md")
    terminal = skill.split("Orchestrator writes `$REPORT_DIR/review-report.md` itself", 1)[1].split(
        "Then jump straight", 1
    )[0]
    prompt = _read("templates/consolidator-prompt.md")
    assert "Commit SHA:" in _read("templates/review-report.md")
    assert "Commit SHA:" in terminal
    assert "REVIEWED_COMMIT_SHA=<" in skill
    assert "`Commit SHA:`" in prompt
    assert "headRefOid" in prompt
    assert "invoking" in prompt


def test_fill_gaps_never_upgrades_abbreviated_provenance() -> None:
    """Legacy carry identity stays exact while its unavailable full SHA is explicit.

    A fill-gaps run carries records bound to an abbreviated head; the skill and the consolidator prompt must both say so
    and keep source judgments and reviewer independence intact instead of inventing a full SHA.
    """
    skill = _read("SKILL.md") + _read("modes/post-agent.md")
    prompt = _read("templates/consolidator-prompt.md")
    assert "legacy abbreviated" in skill
    assert "legacy abbreviated" in prompt
    assert "head_sha" in prompt
    assert "Preserve a supported source judgment" in prompt
    assert "independence" in prompt
