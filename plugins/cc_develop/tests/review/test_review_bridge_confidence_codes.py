"""Pin how local review names an unscored Codex lane: by the bridge's own closed-set confidence code.

The bridge envelope carries ``confidence_null_reason`` whenever it has no number and ``confidence_gaps`` beside a
number. Free-text causes made the same missing lane read differently on every run; these tests hold the consolidator and
the Step 2 handoff sentence to the bridge's vocabulary.
"""

from pathlib import Path

import pytest

_REVIEW = Path(__file__).resolve().parents[2] / "skills/review"


def _line(relative: str, predicate: str) -> str:
    """Return the one line of a shipped review file containing ``predicate``."""
    lines = [line for line in (_REVIEW / relative).read_text(encoding="utf-8").splitlines() if predicate in line]
    assert len(lines) == 1, f"{relative}: expected one line containing {predicate!r}, found {len(lines)}"
    return lines[0]


def test_unscored_lane_is_named_by_the_bridge_code() -> None:
    """The observed cause is the envelope's code, and an absent bridge uses the code for a peer that never started."""
    rule = _line("templates/consolidator-prompt.md", "- Codex confidence uses only")
    assert "closed-set `confidence_null_reason` code" in rule
    assert "`not-recorded` for a stored envelope" in rule
    assert "`peer-unavailable` when the bridge was absent" in rule


def test_scored_lane_carries_its_gap_lines() -> None:
    """A scored reply's own gap lines reach the report, and the report's ceilings still apply."""
    rule = _line("templates/consolidator-prompt.md", "- Codex confidence uses only")
    assert "`confidence_gaps` lines" in rule
    assert "deduction first, as `- (-0.NN) Codex: <limit>`" in rule
    assert "this report's ceilings still apply on top" in rule


def test_step2_retains_the_confidence_fields() -> None:
    """Step 2 keeps every confidence field for the Step 4 handoff, so the consolidator has the code to name."""
    rule = _line("SKILL.md", "Retain the bridge envelope's")
    for field in ("`status`", "`confidence`", "`confidence_null_reason`", "`confidence_gaps`"):
        assert field in rule
    assert "bridge absent → `peer-unavailable`" in rule
    assert "Write tool" in rule
    assert "`$RUN_DIR/codex-review-reply.md`" in rule
    skip_link = "[Record the Codex outcome](#record-the-codex-outcome)"
    assert skip_link in _line("SKILL.md", "If Codex is unavailable")
    assert skip_link in _line("SKILL.md", "When the criteria say to skip")


@pytest.mark.parametrize(
    ("instruction", "required"),
    [
        pytest.param("Single call or single-wave partition", "original envelope", id="single-envelope"),
        pytest.param("Multi-wave partition", "every wave's original envelope", id="partition-envelopes"),
        pytest.param("Multi-wave partition", "qualified gap lines", id="partition-gaps"),
        pytest.param("Multi-wave partition", "unscored group names", id="partition-missing-groups"),
        pytest.param("Failure, skip or absence", "skip or absence reason", id="no-usable-reply"),
    ],
)
def test_bridge_reply_records_cover_each_route(instruction: str, required: str) -> None:
    """Every bridge route leaves enough evidence to recover its score or name its limitation."""
    assert required in _line("SKILL.md", f"- **{instruction}**")


@pytest.mark.parametrize(
    ("mode", "required"),
    [
        pytest.param("Working-tree diff", '`"paths"`', id="explicit-tracked-paths"),
        pytest.param("Working-tree diff", "admitted in Step 1", id="admitted-path-source"),
        pytest.param("Working-tree diff", "untracked", id="untracked-excluded"),
        pytest.param("Working-tree diff", "report them as unreviewed", id="unsupported-admitted-path"),
        pytest.param("Committed diff", '`"range": "HEAD~1..HEAD"`', id="committed-range"),
        pytest.param("Explicit file or directory", "only that target", id="explicit-target"),
    ],
)
def test_bridge_partition_preserves_target_scope(mode: str, required: str) -> None:
    """The caller chooses the partition source from the scope already admitted for review."""
    assert required in _line("SKILL.md", f"- **{mode}**")


def test_partition_lane_score_is_derived_from_every_wave() -> None:
    """No wave of a partition carries a number, so the lane score is derived from all waves, never the last one's."""
    prepass = _line("../_shared/codex-prepass.md", "**Oversized diff**")
    assert "No wave's envelope carries a `confidence`" in prepass
    assert "lowest `splits[].confidence` across every wave's rows" in prepass
    assert "only when every group has one" in prepass
    assert "`(-0.NN) split N: <limit>`" in prepass
    skill = _line("SKILL.md", "never one wave's envelope")
    assert "In a multi-wave partition no wave carries a `confidence`" in skill
    assert "a single-wave partition keeps its own envelope score" in skill.lower()
    assert "never one wave's envelope" in skill
    consolidator = _line("templates/consolidator-prompt.md", "- Codex confidence uses only")
    assert "partition of several waves has no single envelope number" in consolidator
    assert "never one wave's reply" in consolidator
