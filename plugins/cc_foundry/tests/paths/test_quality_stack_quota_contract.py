"""Contract tests over ``quality-stack.md``'s Codex quota stop for develop runs.

A peer out of credits fails every later bridge call, and the bridge never retries it. The quality stack's Codex pre-pass
and mechanical delegation may call the bridge several times in one develop run, so the first harness-classified ``quota-
exhausted`` fault must turn the peer off for the rest of that run. These assertions read the canonical Markdown line by
line; the decision runs inside a live session, so pinning the instruction is the available regression guard.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_QUALITY_STACK = Path(__file__).resolve().parents[2] / "skills" / "_shared" / "quality-stack.md"
_QUOTA_HEADING = "## Codex Quota Stop"


def _section(heading: str) -> list[str]:
    """Return the lines of one ``## `` section, heading excluded, up to the next ``## `` heading."""
    lines = _QUALITY_STACK.read_text(encoding="utf-8").splitlines()
    start = lines.index(heading) + 1
    end = next((index for index in range(start, len(lines)) if lines[index].startswith("## ")), len(lines))
    return lines[start:end]


def test_quota_stop_decides_only_from_the_harness_incident() -> None:
    """Only the incident file's quota fault counts; verdict text and other faults keep today's path.

    A model-authored ``blocked`` result can repeat the harness wording with a null ``incident``, so the rule must name
    the incident ``fault`` as the sole evidence and send every other case back to the existing route.
    """
    lines = _section(_QUOTA_HEADING)
    assert any("incident" in line and "`fault`" in line and "`quota-exhausted`" in line for line in lines)
    assert any("null `incident`" in line and "`rate-limited`" in line and "`capacity`" in line for line in lines)
    assert any("verdict" in line and "blockers text" in line and "never decide" in line.lower() for line in lines)


def test_quota_stop_persists_in_the_run_checkpoint_ledger() -> None:
    """The stop is recorded in the develop run's existing checkpoint ledger through staged appends.

    The checkpoint survives compaction through the skill contract, so a resumed run still sees the stop. The ledger is
    append-only, so the record is staged to ``.rec`` and appended by the shipped helper, never rewritten.
    """
    record = next(line for line in _section(_QUOTA_HEADING) if "checkpoint.md.rec" in line)
    assert "codex-blocked:quota" in record
    assert "append_ledger.py" in record


def test_quota_stop_turns_the_peer_off_for_the_rest_of_the_run() -> None:
    """After the stop, no bridge call runs and later challengers skip their own Codex cross-check."""
    rule = next(line for line in _section(_QUOTA_HEADING) if "no `bridge:*` call runs" in line)
    assert "rest of the run" in rule
    assert "--no-codex" in rule
    assert "foundry:challenger" in rule


def test_quota_stop_counts_a_challenger_quota_signal() -> None:
    """A challenger whose own Codex cross-check met the quota fault triggers the same run-wide stop.

    The implementation challenge may run through ``foundry:challenger``, which calls Codex itself. An envelope decides
    alone through its key; only a reply without a demanded envelope falls back to the fixed line, read as the first non-
    blank line under the first unfenced Cross-Check heading, so Codex output or a quoted template never counts.
    """
    rule = next(line for line in _section(_QUOTA_HEADING) if "`foundry:challenger` reply" in line)
    assert '"codex_blocked": "quota"' in rule
    assert "decides alone" in rule
    assert "without a demanded envelope" in rule
    assert "first non-blank line under the reply's first `## Codex Cross-Check` heading outside any code fence" in rule
    assert "same fault" in rule


def test_quota_stop_reports_a_distinct_outcome_without_a_new_question() -> None:
    """Quota-skipped work is reported as its own outcome, never as a no-op, and the stop asks the user nothing."""
    lines = _section(_QUOTA_HEADING)
    assert any("codex-blocked:quota" in line and "no-op" in line and "No question" in line for line in lines)


@pytest.mark.parametrize(
    ("heading", "needle"),
    [
        pytest.param("## Codex Pre-pass", "§Codex Quota Stop", id="pre-pass"),
        pytest.param("## Codex Mechanical Delegation", "§Codex Quota Stop", id="delegation"),
    ],
)
def test_every_codex_section_applies_the_quota_stop(heading: str, needle: str) -> None:
    """Both bridge-calling sections hand their results to the run-wide stop."""
    assert any(needle in line for line in _section(heading))


def test_delegation_checks_the_ledger_before_each_dispatch() -> None:
    """Each delegated task re-checks the ledger, so one quota fault keeps every remaining task in-house."""
    gate = next(line for line in _section("## Codex Mechanical Delegation") if "Before each `bridge:implement`" in line)
    assert "checkpoint.md" in gate
    assert "codex-blocked:quota" in gate
    assert "in-house" in gate
