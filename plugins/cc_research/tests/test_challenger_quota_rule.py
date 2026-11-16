"""Check the review-loop rule that reacts to a challenger whose Codex cross-check stopped on quota."""

from pathlib import Path

import pytest

_PLUGIN = Path(__file__).resolve().parents[1]
_RULES = [
    pytest.param(_PLUGIN / "rules/quality-gates.md", id="full-rule"),
    pytest.param(_PLUGIN / "rules/quality-gates-delta.md", id="delta-rule"),
]


def _quota_lead(path: Path) -> tuple[list[str], int]:
    """Return the rule file's lines and the index of the lead line naming the challenger quota field."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return lines, next(
        i
        for i, line in enumerate(lines)
        if line.startswith("Codex quota stop: a challenger reply whose envelope carries")
    )


def _quota_bullets(path: Path) -> list[str]:
    """Return the contiguous bullets under the challenger quota lead line, skipping the blank line before them."""
    lines, lead = _quota_lead(path)
    bullets: list[str] = []
    for line in lines[lead + 2 :]:
        if not line.startswith("- "):
            break
        bullets.append(line)
    return bullets


@pytest.mark.parametrize("path", _RULES)
class TestChallengerQuotaRule:
    """A challenger quota signal keeps every later review round of the loop off Codex."""

    def test_lead_keys_on_the_envelope_key(self, path: Path) -> None:
        """The rule fires on the challenger's ``codex_blocked`` envelope key, which it sets from the incident fault.

        The challenger decides quota from the bridge incident file, never from verdict text; the envelope key is its
        primary carrier, so the lead names it together with the fault class and the loop-wide scope.
        """
        lines, lead = _quota_lead(path)
        assert '`"codex_blocked": "quota"`' in lines[lead]
        assert "`quota-exhausted`" in lines[lead]
        assert "rest of that loop" in lines[lead]

    def test_report_line_counts_only_without_an_envelope(self, path: Path) -> None:
        """The fixed Cross-Check line is the second carrier, valid only when no envelope was demanded.

        A dispatch that demands only a fixed closing line gets no envelope key, so the line opening ``## Codex Cross-
        Check`` is the only signal; a token quoted elsewhere in the report never counts.
        """
        item = next(item for item in _quota_bullets(path) if "`⚠ codex-blocked:quota`" in item)
        assert "only when the dispatch demanded no envelope" in item
        assert "first non-blank line under `## Codex Cross-Check`" in item
        assert "other report text never counts" in item

    def test_later_rounds_skip_codex(self, path: Path) -> None:
        """Every later challenger round runs with ``--no-codex`` and the loop never moves to a Codex route.

        A second Codex call after a quota stop cannot succeed and only spends a call; ``bridge:review`` is the same
        peer, so switching route would repeat the doomed call.
        """
        bullets = _quota_bullets(path)
        assert any("`--no-codex`" in item and "every later `foundry:challenger` dispatch" in item for item in bullets)
        assert any("never switch the loop to a `bridge:review` route" in item for item in bullets)

    def test_quota_blocked_round_is_not_redispatched(self, path: Path) -> None:
        """A quota-blocked envelope is exempt from the re-dispatch-once rule.

        The general rule re-dispatches a ``blocked`` round with a brief that removes its cause; no brief removes a quota
        stop, so a re-dispatch would only repeat the doomed call.
        """
        bullets = _quota_bullets(path)
        assert any("not re-dispatched" in item and "no brief removes a quota stop" in item for item in bullets)

    def test_security_filter_review_is_not_dispatched(self, path: Path) -> None:
        """After a quota stop no security-filter review is dispatched; it is reported blocked with the quota cause.

        The challenger ignores ``--no-codex`` for a filter review, so dispatching one would spend a Codex call that
        cannot succeed. The review is reported ``blocked`` with the cause and the user's options, and never as PASS.
        """
        item = next(item for item in _quota_bullets(path) if "security-filter review" in item)
        assert "Dispatch no later security-filter review" in item
        assert "`blocked` with the quota cause" in item
        assert "wait for the quota reset" in item
        assert "the user decides" in item
        assert "never PASS" in item

    def test_quota_stop_is_recorded_in_the_loop_report(self, path: Path) -> None:
        """The quota stop is written to the loop's existing report, so a compaction cannot forget it."""
        assert any("`loop-report.md`" in item for item in _quota_bullets(path))

    def test_absent_carriers_keep_todays_rules(self, path: Path) -> None:
        """Without the envelope key and without the Cross-Check line, the existing loop rules apply unchanged.

        An older challenger never emits either carrier, and a rate-limit or capacity stop never sets one, so those
        rounds keep the existing re-dispatch and filter-review behaviour.
        """
        assert any(
            "Field absent and no such line" in item and "loop rules apply unchanged" in item
            for item in _quota_bullets(path)
        )
