"""Guard the adversarial loop's reviewer route and Codex quota stop in the oss quality-gate rules.

A ``foundry:challenger`` round runs its own Codex cross-check. When that call stops on a quota fault, the challenger's
reply says so, and every later Codex call of the loop would fail the same way. Both oss rule files, the complete one and
the delta beside foundry's shared body, must tell the loop to stop calling Codex and never to report a security filter
review it could not run as PASS.
"""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest

_RULES = Path(__file__).resolve().parents[1] / "rules"


@pytest.mark.parametrize("name", ["quality-gates.md", "quality-gates-delta.md"])
def test_loop_rounds_require_the_dual_engine_reviewer_with_foundry(name: str) -> None:
    """Require full challenger coverage each round and reserve direct bridge review for standalone installs."""
    lines = (_RULES / name).read_text(encoding="utf-8").splitlines()
    route = next(line for line in lines if line.startswith("For permitted independent review"))

    assert "`foundry:challenger --codex-full` for every loop round" in route
    assert "subject to the Codex quota stop below" in route
    assert "`bridge:review` only on installs without `foundry`" in route


def _quota_rule(name: str) -> list[str]:
    """Return the loop rule's Codex quota stop: its lead line and the bullet lines that follow it."""
    lines = (_RULES / name).read_text(encoding="utf-8").splitlines()
    lead = next(index for index, line in enumerate(lines) if line.startswith("Codex quota stop:"))
    rest = itertools.dropwhile(lambda line: not line.strip(), lines[lead + 1 :])
    return [lines[lead], *itertools.takewhile(lambda line: line.startswith("- "), rest)]


@pytest.mark.parametrize("name", ["quality-gates.md", "quality-gates-delta.md"])
@pytest.mark.parametrize(
    "needles",
    [
        pytest.param(('"codex_blocked": "quota"', "`quota-exhausted`"), id="trigger"),
        pytest.param(
            ("`--no-codex`", "every later `foundry:challenger` dispatch", "never switch the loop to a `bridge:review`"),
            id="route",
        ),
        pytest.param(("no later security-filter review", "never PASS", "the user decides"), id="filter"),
        pytest.param(("is not re-dispatched", "no brief removes a quota stop"), id="no-redispatch"),
        pytest.param(("reviewer route limits", "`loop-report.md`"), id="route-limits"),
        pytest.param(
            ("counts only when the dispatch demanded no envelope", "first non-blank line under `## Codex Cross-Check`"),
            id="report-line",
        ),
        pytest.param(("Field absent and no such line",), id="absent-unchanged"),
    ],
)
def test_loop_rule_stops_codex_after_a_challenger_quota_signal(name: str, needles: tuple[str, ...]) -> None:
    """Each obligation of the quota stop sits on one line of the rule's lead sentence or its bullets.

    The rule sits right after the loop paragraph, so a loop round reads it before dispatching the next challenger.
    """
    rule = _quota_rule(name)

    assert any(all(needle in line for needle in needles) for line in rule)
