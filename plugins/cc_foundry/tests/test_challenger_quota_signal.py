"""Pin foundry:challenger's Codex quota signal, which tells its caller the peer is out of credits for the whole run.

The challenger runs its own Codex cross-check through ``bridge:review``. When that call stopped on a harness-classified
``quota-exhausted`` fault, the stop stayed inside the challenger as free text, and the caller kept sending Codex calls
that could not succeed. The challenger now decides from the bridge incident file's ``fault`` alone and reports the stop
in two fixed places: a top-level ``codex_blocked`` key in any envelope its caller demands, and one fixed line that opens
its Codex Cross-Check section (the only channel when a caller demands a fixed closing line instead of an envelope).

These assertions read the agent definition line by line, so a reflow of surrounding prose never breaks one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_CHALLENGER = Path(__file__).resolve().parents[1] / "agents" / "challenger.md"
_QUOTA_LINE = (
    "⚠ codex-blocked:quota — Codex out of credits (incident fault `quota-exhausted`); cross-check not retried."
    " Report above is Claude-only."
)


def _section(start: str, end: str) -> list[str]:
    """Return the challenger lines from the one equal to ``start`` up to, not including, the one equal to ``end``."""
    lines = _CHALLENGER.read_text(encoding="utf-8").splitlines()
    first = lines.index(start)
    return lines[first : lines.index(end, first)]


def _workflow() -> list[str]:
    """Return the ``<workflow>`` block, where step 6 collects the Codex result."""
    return _section("<workflow>", "</workflow>")


def _delivery() -> list[str]:
    """Return the ``<delivery>`` block, which owns envelope shape and the security-filter blocked envelope."""
    return _section("<delivery>", "</delivery>")


def test_quota_signal_is_decided_from_the_incident_fault() -> None:
    """A blocked or timed-out cross-check sets the quota signal only from the incident file's ``fault``.

    The bridge classifies a provider stop in the incident it links from the envelope; a classified fault can also arrive
    under ``status=timeout``, so both statuses must lead to the incident read.
    """
    rule = next(line for line in _workflow() if "CODEX_BLOCKED=quota" in line and "`incident`" in line)
    assert "`blocked` or `timeout`" in rule
    assert "`fault`" in rule
    assert "`quota-exhausted`" in rule


def test_other_evidence_never_sets_the_quota_signal() -> None:
    """A null incident, an unreadable file, another fault or verdict text all leave the signal unset.

    A peer model can write a ``blocked`` result with the harness's own wording and a null ``incident``; deciding on that
    text would let the peer switch Codex off for the caller's whole run.
    """
    rule = next(line for line in _workflow() if "leave `CODEX_BLOCKED` unset" in line)
    assert "null `incident`" in rule
    assert "unreadable" in rule
    assert "any other `fault`" in rule
    assert "verdict or blockers text" in rule


def test_quota_stop_is_never_retried() -> None:
    """After a quota fault the challenger makes no second bridge call in the same run.

    The bridge already made its one attempt and a peer out of credits fails every later call, so a retry only spends
    another doomed call.
    """
    assert any("CODEX_BLOCKED=quota" in line and "no second `bridge:*` call" in line for line in _workflow())


def test_cross_check_section_states_the_quota_stop_in_one_fixed_line() -> None:
    """The report's Codex Cross-Check section carries one fixed quota line that a caller can match exactly.

    Callers that demand a fixed closing line (calibration's ``Wrote: <id>``) never receive an envelope, so the report
    file's opening Cross-Check line is the only signal they can read.
    """
    lines = _section("<output-format>", "</output-format>")
    marker = next(index for index, line in enumerate(lines) if "CODEX_BLOCKED=quota" in line and "<!--" in line)
    assert "opens the section" in lines[marker]
    assert lines[marker + 1] == _QUOTA_LINE


def test_successful_cross_check_opens_with_a_fixed_line_before_codex_output() -> None:
    """A successful cross-check opens its section with a fixed challenger line, and Codex output follows it.

    Callers read the quota signal from the section's first non-blank line. When that line was Codex's own verbatim
    output, a peer could open its text with the quota line and switch Codex off for the caller's whole run; a fixed
    challenger-authored first line keeps that position out of peer reach.
    """
    lines = _section("<output-format>", "</output-format>")
    marker = next(index for index, line in enumerate(lines) if "When Codex succeeded" in line and "<!--" in line)
    body = [line for line in lines[marker + 1 :] if line.strip()]
    assert body[:2] == ["Codex cross-check completed.", "[CODEX_OUTPUT verbatim]"]


def test_section_first_line_is_always_challenger_authored() -> None:
    """The delivery rule keeps Codex output out of the section's first line in every branch.

    The quota line is the only signal a fixed-closing-line caller gets, so the first line must never be peer text.
    """
    rule = next(line for line in _delivery() if line.startswith("- **Codex quota signal**"))
    assert "Codex output never opens that section" in rule
    assert "`Codex cross-check completed.`" in rule


@pytest.mark.parametrize(
    "clause",
    [
        pytest.param('"codex_blocked": "quota"', id="key-and-value"),
        pytest.param("top-level key", id="top-level"),
        pytest.param("even when its schema omits it", id="added-to-caller-schemas"),
        pytest.param("absent otherwise", id="absent-without-quota"),
    ],
)
def test_every_demanded_envelope_carries_the_quota_key(clause: str) -> None:
    """Each envelope a caller demands gains the quota key, whatever shape the caller asked for.

    Audit gates, the residual scan and develop review each dictate their own ``Return ONLY`` shape; the key must ride on
    all of them, and its absence must mean no quota stop.
    """
    rule = next(line for line in _delivery() if line.startswith("- **Codex quota signal**"))
    assert clause in rule


def test_fixed_closing_line_leaves_the_report_line_as_the_signal() -> None:
    """A demanded fixed closing line stays unchanged; the Cross-Check line then carries the stop alone."""
    rule = next(line for line in _delivery() if line.startswith("- **Codex quota signal**"))
    assert "fixed closing line" in rule
    assert "Cross-Check" in rule


def test_filter_review_blocked_envelope_names_a_quota_cause() -> None:
    """A security-filter review whose Codex search hit the quota keeps its blocked reason and adds the quota key.

    The filter path already returns ``codex-unavailable`` when the search cannot run; the caller still needs to know
    that every later Codex call in its run would fail the same way.
    """
    rule = next(line for line in _delivery() if '"reason": "codex-unavailable"' in line)
    assert '"codex_blocked": "quota"' in rule
