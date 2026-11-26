"""Hold the Confidence Block to scores derived from named gaps only, and the clock report to that rule.

The Confidence Block in ``rules/_full/quality-gates.md`` is what every analysis agent follows when it scores its own
work. A score that a constant stands in for, a null reviewer counted as a number, or a required control recorded as a
token deduction makes confidence flat and meaningless; these pins keep the rule saying otherwise. The clock-time profile
report is foundry's own scored output, so it must follow the same rule: its deductions depend on what the data holds,
not on a fixed block.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import timing_analyzer

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
FULL_RULE = PLUGIN_ROOT / "rules" / "_full" / "quality-gates.md"
STUB_RULE = PLUGIN_ROOT / "rules" / "quality-gates.md"
BLOCK_HEADING = "## Confidence Block (required on all analysis tasks)"
NAMED_GAPS_BULLET = "- **Score from named gaps only**"


def _section(path: Path, heading: str) -> list[str]:
    """Return the lines of one ``## `` section outside fenced code, read line by line."""
    lines = path.read_text(encoding="utf-8").splitlines()
    assert heading in lines, f"{path.name} has no {heading} section"
    body, fenced = [], False
    for line in lines[lines.index(heading) + 1 :]:
        fenced = fenced != line.lstrip().startswith("```")
        if not fenced and line.startswith("## "):
            break
        body.append(line)
    return body


class TestConfidenceBlockRule:
    """The full rule states each part of the named-gaps obligation."""

    @pytest.fixture
    def bullet(self) -> str:
        """Return the single named-gaps bullet of the full Confidence Block."""
        matches = [line for line in _section(FULL_RULE, BLOCK_HEADING) if line.startswith(NAMED_GAPS_BULLET)]
        assert len(matches) == 1, matches
        return matches[0]

    @pytest.mark.parametrize(
        "fragment",
        [
            pytest.param("1.0 minus its listed deductions", id="score-from-deductions"),
            pytest.param("never a constant or default that stands in for a reviewer", id="no-constant-reviewer"),
            pytest.param("null, failed or blocked contribution is excluded", id="null-excluded"),
            pytest.param("named once under Gaps", id="null-named"),
            pytest.param("A control the workflow requires is enforced", id="control-enforced"),
            pytest.param("never record that violation as a token deduction", id="no-token-deduction"),
            pytest.param("A closure note (gap fixed, check rerun clean) is not a gap", id="closure-note"),
        ],
    )
    def test_bullet_states_the_obligation(self, bullet: str, fragment: str) -> None:
        """Each obligation of the named-gaps rule is stated in the full Confidence Block."""
        assert fragment in bullet

    def test_stub_restates_the_rule_in_short(self) -> None:
        """The always-loaded stub carries the rule in one sentence, so it binds before the full file is read."""
        lines = _section(STUB_RULE, BLOCK_HEADING)
        assert any("Score = 1.0 minus listed deductions" in line for line in lines)


class TestClockReportConfidence:
    """The profile clock report derives its score from the gaps that apply to its data."""

    def test_window_without_spawns_carries_only_the_model_gap(self) -> None:
        """No agent time means the spawn-related gaps do not limit the report, so they are not deducted."""
        gaps = timing_analyzer.confidence_gaps(agent_ms=0)
        assert ([gap.deduction for gap in gaps], timing_analyzer.confidence_score(gaps)) == ([0.03], 0.97)

    def test_window_with_spawns_deducts_the_spawn_gaps(self) -> None:
        """Agent time brings in the invisible-subagent and background-join gaps."""
        gaps = timing_analyzer.confidence_gaps(agent_ms=1)
        assert ([gap.deduction for gap in gaps], timing_analyzer.confidence_score(gaps)) == ([0.07, 0.05, 0.03], 0.85)

    @pytest.mark.parametrize("agent_ms", [0, 5_000])
    def test_header_and_block_agree_and_deductions_sum_to_the_shortfall(self, agent_ms: int) -> None:
        """The header ``Confidence:`` score equals the block score, and the listed deductions sum to ``1.0 - Score``."""
        stats = timing_analyzer.SessionStats("s1", first_ts=0.0, last_ts=60.0, agent_ms=agent_ms)
        report = timing_analyzer.render_report({"s1": stats}, [], since_spec="24h", top_n=5, warnings=0).splitlines()
        header = next(line for line in report if line.startswith("Confidence:")).split()[1]
        block = next(line for line in report if line.startswith("**Score**:")).split()[1]
        deductions = [float(line[4:8]) for line in report if line.startswith("- (-0.")]
        assert (header, round(1.0 - sum(deductions), 2)) == (block, float(block))
