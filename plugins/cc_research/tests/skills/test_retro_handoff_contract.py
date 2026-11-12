"""Protect retro's sanitized inputs, follow-up routing, and agent deadlines."""

from pathlib import Path

_SKILL = Path(__file__).resolve().parents[2] / "skills/retro/SKILL.md"


def _section(start: str, end: str) -> list[str]:
    """Read the requested heading-delimited workflow step."""
    lines = _SKILL.read_text(encoding="utf-8").splitlines()
    first = next(index for index, line in enumerate(lines) if line.startswith(start))
    last = next(index for index in range(first + 1, len(lines)) if lines[index].startswith(end))
    return lines[first:last]


def test_scientist_requires_sanitized_history_before_dispatch() -> None:
    """Missing sanitized data must stop the handoff instead of reopening the raw ledger."""
    lines = _section("### Step T5:", "### Step T6:")
    spawn = next(index for index, line in enumerate(lines) if line.startswith("Spawn `research:scientist`"))
    assert any("RETRO_JSONL" in line and "! BLOCKED" in line and "stop" in line for line in lines[:spawn])
    history = next(line for line in lines if line.startswith("- experiments-clean.jsonl"))
    assert "fall back" not in history
    assert "experiments.jsonl" not in history
    assert "! BLOCKED" in history
    assert "stop" in history


def test_follow_up_routes_hypotheses_to_run_and_kept_changes_to_fortify() -> None:
    """Fortify recommendations must describe ablations, including the no-queue fallback."""
    lines = _section("### Step T7:", "</workflow>")
    queue = next(line for line in lines if line.startswith("Next: /research:run"))
    assert "<program.md> --hypothesis <RUN_DIR>/hypotheses.jsonl" in queue
    assert 'scientist_status != "timed_out"' in queue
    fortify = [line for line in lines if "/research:fortify" in line]
    assert len(fortify) == 2
    assert all("ablate kept changes" in line for line in fortify)
    assert not any("stress-test top hypothesis" in line for line in lines)
    fallback = next(line for line in fortify if line.startswith("If `scientist_status"))
    assert "omit the `--hypothesis` Next line entirely" in fallback
    assert "no hypotheses queue generated" in fallback


def test_scientist_timeout_uses_shared_deadline_protocol() -> None:
    """Retro must use recorded deadlines rather than watching elapsed silence."""
    lines = _section("### Step T5:", "### Step T6:")
    health = next(line for line in lines if line.startswith("**Health monitoring note**"))
    assert "agent-resolution.md" in health
    assert "Agent waits" in health
    assert "agent-watch" in health
    assert "deadline" in health
    assert "nothing appears" not in health
    assert ">15 min" not in health
