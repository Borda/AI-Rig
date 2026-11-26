"""Guard develop's Codex quota stop: one harness-classified quota fault turns the peer off for the rest of the run.

A peer out of credits fails every later bridge call, and the bridge never retries it. Develop runs reach the bridge from
the shipped quality stack (pre-pass review, mechanical delegation) and from ``/develop:review`` (co-review, then a
challenger that would otherwise run its own Codex cross-check). These tests pin the instructions in develop's own
shipped copies, so a standalone develop install keeps the stop without foundry present.

Lines are read independently, so a reflow of surrounding prose never breaks an assertion.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

import pytest

_SKILLS = Path(__file__).resolve().parents[2] / "skills"
_RULES = _SKILLS.with_name("rules")
_QUALITY_STACK = "_shared/foundry--quality-stack.md"
_REVIEW = "review/SKILL.md"


@pytest.mark.parametrize("name", ["quality-gates.md", "quality-gates-delta.md"])
def test_loop_rounds_require_the_dual_engine_reviewer_with_foundry(name: str) -> None:
    """Require full challenger coverage each round and reserve direct bridge review for standalone installs."""
    lines = (_RULES / name).read_text(encoding="utf-8").splitlines()
    route = next(line for line in lines if line.startswith("For permitted independent review"))

    assert "`foundry:challenger --codex-full` for every loop round" in route
    assert "subject to the Codex quota stop below" in route
    assert "`bridge:review` only on installs without `foundry`" in route


@cache
def _lines(relative: str) -> tuple[str, ...]:
    """Read one develop skill file as independent instruction lines."""
    return tuple((_SKILLS / relative).read_text(encoding="utf-8").splitlines())


@pytest.mark.parametrize(
    "needles",
    [
        pytest.param(("incident", "`fault`", "`quota-exhausted`"), id="incident-fault-decides"),
        pytest.param(("null `incident`", "`rate-limited`", "`capacity`"), id="other-faults-keep-today"),
        pytest.param(("verdict", "blockers text", "Never decide"), id="verdict-text-never-decides"),
        pytest.param(("checkpoint.md.rec", "codex-blocked:quota", "append_ledger.py"), id="ledger-record"),
        pytest.param(("no `bridge:*` call runs", "rest of the run", "--no-codex"), id="peer-off-for-run"),
        pytest.param(("codex-blocked:quota", "no-op", "No question"), id="distinct-outcome"),
    ],
)
def test_quality_stack_carries_the_quota_stop(needles: tuple[str, ...]) -> None:
    """The shipped quality stack decides from the incident fault, records the stop and turns the peer off.

    Each case pins one obligation of the stop on a single instruction line of develop's propagated copy.
    """
    assert any(all(needle in line for needle in needles) for line in _lines(_QUALITY_STACK))


def test_review_records_a_quota_blocked_co_review() -> None:
    """A quota-blocked co-review is recorded in the run's codex.md from the incident fault, not the verdict."""
    record = next(line for line in _lines(_REVIEW) if "first line of `codex.md`" in line)
    assert "`quota-exhausted`" in record
    assert "codex-blocked:quota" in record
    keep = next(line for line in _lines(_REVIEW) if "keeps today's record" in line)
    assert "null `incident`" in keep


def test_review_challenger_skips_codex_after_a_quota_stop() -> None:
    """The challenger launched after a quota-blocked co-review skips its own doomed Codex cross-check."""
    agent = next(line for line in _lines(_REVIEW) if line.lstrip().startswith("- Exception to the omit rule"))
    assert "--no-codex" in agent
    assert "codex-blocked:quota" in agent


_AGENT_RESOLUTION = "_shared/agent-resolution.md"
_SIGNAL_HEADING = "## Challenger Codex quota signal"


def _signal_section() -> tuple[str, ...]:
    """Return the agent-resolution section that turns a challenger's quota signal into the run's checkpoint record."""
    lines = _lines(_AGENT_RESOLUTION)
    start = lines.index(_SIGNAL_HEADING) + 1
    end = next(index for index in range(start, len(lines)) if lines[index].startswith("## "))
    return lines[start:end]


@pytest.mark.parametrize(
    "needles",
    [
        pytest.param(('"codex_blocked": "quota"', "decides alone", "without a demanded envelope"), id="envelope-first"),
        pytest.param(
            ("first non-blank line under the reply's first `## Codex Cross-Check` heading outside any code fence",),
            id="first-unfenced-heading",
        ),
        pytest.param(("incident file's `fault`", "never infer it from other report text"), id="provenance"),
        pytest.param(("checkpoint.md.rec", "same Write as any pending `step:` line", "append_ledger.py"), id="record"),
        pytest.param(("§Codex Quota Stop", "--no-codex", "No question"), id="quality-stack-takes-over"),
    ],
)
def test_agent_resolution_records_a_challenger_quota_signal(needles: tuple[str, ...]) -> None:
    """The early-loaded resolution doc turns a plan-gate challenger's quota signal into the run's checkpoint record.

    The plan challenger gate runs before the quality stack loads, so the stop needs a home every feature, fix and
    refactor run reads first. The record shares the checkpoint's one staged ``.rec`` file with pending step lines, so it
    must ride in the same Write rather than overwrite them.
    """
    assert any(all(needle in line for needle in needles) for line in _signal_section())


def test_agent_resolution_scopes_the_signal_to_runs_with_later_codex_calls() -> None:
    """Feature, fix and refactor record the stop; plan and debug make no later Codex call and review keeps codex.md."""
    scope = next(line for line in _signal_section() if line.startswith("Applies to"))
    assert [name for name in ("feature", "fix", "refactor", "plan", "debug", "codex.md") if name not in scope] == []


@pytest.mark.parametrize("skill", ["feature/SKILL.md", "fix/SKILL.md", "refactor/SKILL.md"])
def test_plan_challenger_gate_points_at_the_quota_signal(skill: str) -> None:
    """Each plan challenger gate reads the quota signal where it parses the challenger's result."""
    lines = _lines(skill)
    gate = lines.index("## Challenger gate")
    end = next(index for index in range(gate + 1, len(lines)) if lines[index].startswith("## "))
    pointer = next(line for line in lines[gate:end] if line.startswith("- **Codex quota signal**"))
    assert "§Challenger Codex quota signal" in pointer


def test_quality_stack_counts_a_challenger_quota_signal() -> None:
    """The implementation challenge's quota signal triggers the quality stack's run-wide stop in develop's copy."""
    needles = (
        "`foundry:challenger` reply",
        '"codex_blocked": "quota"',
        "decides alone",
        "⚠ codex-blocked:quota",
        "outside any code fence",
        "same fault",
    )
    assert any(all(needle in line for needle in needles) for line in _lines(_QUALITY_STACK))


def test_review_records_agent_seven_quota_signal_in_codex_md() -> None:
    """Agent 7's quota key puts the stop on codex.md's first line, so a Step 4 challenger verifier skips Codex.

    A Step 4 verifier is spawned with the same agent type as the finding's author; for a challenger finding that is a
    second challenger on the same material, which would otherwise run its own doomed Codex cross-check.
    """
    rule = next(line for line in _lines(_REVIEW) if '"codex_blocked": "quota"' in line)
    assert "first line of `$RUN_DIR/codex.md`" in rule
    assert "Step 4" in rule
    assert "--no-codex" in rule


def test_review_challenger_brief_returns_the_quota_key() -> None:
    """Agent 7's envelope schema names the optional quota key, so the brief's fixed shape never drops it."""
    brief = "\n".join(_lines("review/templates/brief-challenger.md"))
    assert '"codex_blocked":"quota"' in brief
    assert "omit it otherwise" in brief
