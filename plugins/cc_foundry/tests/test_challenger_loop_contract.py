"""Keep foundry:challenger usable as the adversarial loop's reviewer: a round must fold into the loop table."""

from pathlib import Path

import pytest

_PLUGINS = Path(__file__).resolve().parents[2]
_CHALLENGER = _PLUGINS / "cc_foundry" / "agents" / "challenger.md"


@pytest.mark.parametrize(
    "clause",
    [
        pytest.param("never `run_in_background`", id="foreground-only"),
        pytest.param("Final reply = deliverable", id="inline-delivery"),
        pytest.param("Never send the report or envelope through SendMessage", id="no-sendmessage-envelope"),
        pytest.param("never author candidate bypass strings", id="generator-not-author"),
        pytest.param('"status": "blocked"', id="blocked-envelope"),
        pytest.param("Never report PASS for a search that did not run", id="no-false-pass"),
        pytest.param("the open-ended search runs in Codex, never in your own reply", id="codex-runs-filter-search"),
        pytest.param("A filter review needs Codex: ignore `--no-codex` for it", id="filter-review-keeps-codex"),
        pytest.param("tagged `source: codex` or `source: generator`", id="codex-candidates-counted"),
        pytest.param('"reason": "codex-unavailable"', id="codex-unavailable-blocks"),
        pytest.param("a skipped or failed cross-check is stated, never omitted", id="codex-section-always"),
        pytest.param("tools: Read, Write, Grep, Glob, Bash, Skill", id="skill-tool-for-bridge"),
    ],
)
def test_challenger_states_delivery_rule(clause: str) -> None:
    """Each rule that kept a challenger round from reaching the loop table stays in the agent definition.

    A challenger round failed once per missing rule: a background test run nothing resumed, a refused report-file write,
    an envelope sent as a SendMessage object, and a classifier stop while authoring bypass strings. Later rounds fed a
    generator file were still stopped once the agent searched on its own, and none reached Codex: a definition without
    the ``Skill`` tool cannot call ``bridge:review``, and the skip went unreported.
    """
    assert clause in _CHALLENGER.read_text(encoding="utf-8")


@pytest.mark.parametrize("tier", ["security", "critical", "high", "medium", "low", "nit"])
def test_challenger_maps_findings_to_loop_tier(tier: str) -> None:
    """Every adversarial-loop tier has a row in the challenger's mapping, so its counts fold into the progress table.

    The challenger's own Blocker/Concern/Nitpick scale has no weight in the loop score; without the mapping a completed
    round still cannot be scored.
    """
    assert f"| `{tier}` |" in _CHALLENGER.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "relative",
    [
        "cc_foundry/rules/_full/quality-gates.md",
        "cc_develop/rules/quality-gates.md",
        "cc_develop/rules/quality-gates-delta.md",
        "cc_oss/rules/quality-gates.md",
        "cc_oss/rules/quality-gates-delta.md",
        "cc_research/rules/quality-gates.md",
        "cc_research/rules/quality-gates-delta.md",
    ],
)
def test_dispatch_site_records_challenger_failure(relative: str) -> None:
    """Every rule that dispatches the challenger in a loop treats a round without a deliverable as an agent failure.

    A silent ``not-run`` row hides why the primary reviewer produced nothing; the rule asks for the cause and one
    corrected re-dispatch, and keeps Codex enabled so the challenger's own cross-check runs the bypass search. A reply
    cut off before its closing block is listed as a cause too, or it reads as a complete round with fewer findings.
    """
    text = (_PLUGINS / relative).read_text(encoding="utf-8")
    assert "agent failure" in text
    assert "reply truncated before its required closing block" in text
    assert "probe generator" in text
    assert "no `--no-codex`" in text


@pytest.mark.parametrize(
    "clause",
    [
        pytest.param("is an agent failure, never a silent `not-run`", id="failure-not-silent"),
        pytest.param("never append it to `loop-rounds.jsonl`", id="failure-never-a-round"),
        pytest.param(
            "Re-dispatch that reviewer once with a brief that removes the cause;", id="unconditional-redispatch"
        ),
        pytest.param("not a retry of the same one", id="redispatch-not-retry"),
        pytest.param("still needs any approval its route requires", id="redispatch-keeps-approval"),
        pytest.param("for a refused write, require the findings inline in the final reply", id="refused-write-inline"),
        pytest.param("never spend the re-dispatch on the same write under another name", id="no-rename-retry"),
        pytest.param("treat the route as failed without a re-dispatch", id="no-attempt-left-fails-route"),
        pytest.param("switch to another permitted independent route when one exists", id="second-failure-switches"),
        pytest.param("stop as `independence-unavailable` only when none remains", id="stop-only-without-route"),
        pytest.param("a reply truncated in transit", id="truncated-reply-is-failure"),
        pytest.param(
            "A truncated reply never counts as a complete round with fewer findings", id="truncated-not-short-round"
        ),
        pytest.param("keep asking while each reply adds new content", id="truncated-tail-recovery"),
        pytest.param("A reply that adds nothing new", id="truncated-tail-stalls"),
    ],
)
def test_loop_rule_records_reviewer_round_failure(clause: str) -> None:
    """The shipped loop rule treats any reviewer round without a deliverable as a recorded agent failure.

    The quality-gates files cover the challenger; the loop rule itself must say the same for every reviewer, or a caller
    reading only ``_full/adversarial-loop.md`` records the round as ``not-run`` and loses its cause.
    """
    text = (_PLUGINS / "cc_foundry/rules/_full/adversarial-loop.md").read_text(encoding="utf-8")
    assert clause in text


def test_loop_rule_redispatch_is_not_gated_on_retry_contract() -> None:
    """The corrected re-dispatch is an obligation, not an option a workflow's retry contract can veto.

    Codex Rig's specialist retry policy allows only timeout, transport and rate-limit retries; a qualifier tying the re-
    dispatch to that contract silently removed it there while every rule and changelog promised it.
    """
    text = (_PLUGINS / "cc_foundry/rules/_full/adversarial-loop.md").read_text(encoding="utf-8")
    assert "Where the owning workflow's retry contract permits that class, re-dispatch" not in text


def test_eager_foundry_rule_points_at_challenger_rounds() -> None:
    """The always-loaded foundry rule sends every challenger dispatch to the full Challenger Rounds section.

    The procedure lives in ``_full/quality-gates.md`` to keep the eager rule set small; without this pointer a caller
    would brief the challenger before ever reading it.
    """
    text = (_PLUGINS / "cc_foundry/rules/quality-gates.md").read_text(encoding="utf-8")
    assert "`_full/quality-gates.md` §Challenger Rounds" in text
