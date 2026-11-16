"""Check that a Codex quota stop in judge's adversarial review ends Codex calls for the judge and sweep run."""

from pathlib import Path

import pytest
from _research_text import list_after as _list_after

_PLUGIN = Path(__file__).resolve().parents[2]
_JUDGE = _PLUGIN / "skills/judge/SKILL.md"
_SWEEP = _PLUGIN / "skills/sweep/SKILL.md"


def _lines(path: Path) -> list[str]:
    """Return the Markdown file's lines without their trailing newlines."""
    return path.read_text(encoding="utf-8").splitlines()


def _section(path: Path, start: str, end: str) -> list[str]:
    """Return the lines from the heading starting with ``start`` up to the next heading starting with ``end``."""
    lines = _lines(path)
    first = next(i for i, line in enumerate(lines) if line.startswith(start))
    last = next(i for i in range(first + 1, len(lines)) if lines[i].startswith(end))
    return lines[first:last]


def _j5a() -> list[str]:
    """Return judge Step J5a, the Codex adversarial review."""
    return _section(_JUDGE, "## Step J5a:", "## Step J5b:")


def _j5a_quota_index() -> int:
    """Return the index, within J5a, of the lead line classifying a harness quota stop."""
    return next(i for i, line in enumerate(_j5a()) if line.startswith("Codex stopped on a **harness-classified quota"))


def _s3() -> list[str]:
    """Return sweep Step S3, the judge and refinement loop."""
    return _section(_SWEEP, "### Step S3:", "### Step S4:")


def _s5() -> list[str]:
    """Return sweep Step S5, the run, up to the closing workflow tag."""
    lines = _lines(_SWEEP)
    first = next(i for i, line in enumerate(lines) if line.startswith("### Step S5:"))
    last = next(i for i in range(first + 1, len(lines)) if lines[i].startswith("</workflow>"))
    return lines[first:last]


class TestJudgeQuotaStop:
    """Judge's single ``bridge:review`` call reads a quota stop from the incident and finishes Claude-only."""

    def test_decides_on_the_incident_fault(self) -> None:
        """The quota outcome is keyed on a non-null incident whose file names ``quota-exhausted``.

        Synthetic harness envelope case: ``status`` blocked or timeout and an ``incident`` linking a file with
        ``fault: quota-exhausted``. The rule must name every part of that provenance check.
        """
        lead = _j5a()[_j5a_quota_index()]
        assert "`bridge:review` result `status` is `blocked` or `timeout`" in lead
        assert "`incident` is non-null" in lead
        assert "`fault` equal to `quota-exhausted`" in lead

    def test_records_marker_without_retry(self) -> None:
        """A quota stop is recorded as ``codex-blocked:quota``, never as a finding, and the call is not repeated."""
        bullets = _list_after(_j5a(), _j5a_quota_index())
        record = next(item for item in bullets if "`codex-blocked:quota`" in item)
        assert "never as a Codex finding" in record
        assert "no retry" in record
        assert "no further `bridge:review` call in this run" in record
        assert any("continue to J5b Claude-only" in item for item in bullets)

    @pytest.mark.parametrize(
        "case",
        [
            pytest.param("`incident: null`", id="model-authored-blocked"),
            pytest.param("unreadable incident file", id="unreadable-incident"),
            pytest.param("`rate-limited`", id="rate-limited"),
            pytest.param("`capacity`", id="capacity"),
            pytest.param("`blocked`", id="old-bridge-fault"),
        ],
    )
    def test_unclassified_stops_keep_todays_handling(self, case: str) -> None:
        """Every stop that is not a harness quota fault keeps today's J5a handling, and prose never decides.

        Covers a model-authored ``blocked`` result with the matching verdict wording (``incident: null``), an unreadable
        incident, the transient ``rate-limited`` and ``capacity`` faults, and an old bridge whose fault is ``blocked``.
        """
        rule = next(line for line in _j5a() if line.startswith("Not classified"))
        assert "verdict or `blockers` text never decides" in rule
        assert case in rule
        assert "keeps today's handling" in rule

    def test_report_and_summary_name_the_quota_stop(self) -> None:
        """The written report and the terminal summary both show the quota stop, so sweep can read it back."""
        lines = _lines(_JUDGE)
        assert any(line.startswith("(Stopped — `codex-blocked:quota`") for line in lines)
        summary = next(line for line in lines if line.startswith("Codex:        "))
        assert "codex-blocked:quota" in summary

    def test_report_header_carries_the_codex_field(self) -> None:
        """The report file's header has a fixed ``Codex:`` field with the summary's closed value set.

        Sweep reads the stop back from the report file after a compaction, never from the terminal summary; a fixed
        header field is the one place judged text or Codex output cannot reach.
        """
        lines = _lines(_JUDGE)
        start = next(i for i, line in enumerate(lines) if line.startswith("Title:         Judge —"))
        end = next(i for i in range(start, len(lines)) if lines[i] == "---")
        assert "Codex:         reviewed | skipped | codex-blocked:quota" in lines[start:end]

    def test_quota_stop_is_recorded_in_the_header_field(self) -> None:
        """J5a writes the quota marker into the report's ``Codex:`` header field."""
        record = next(item for item in _list_after(_j5a(), _j5a_quota_index()) if "`codex-blocked:quota`" in item)
        assert "`Codex:` header field" in record


class TestSweepQuotaStop:
    """A judge quota stop inside sweep ends every later Codex call of the sweep run, judge and run alike."""

    def test_later_judge_iterations_skip_codex(self) -> None:
        """After one judge iteration records the stop, later iterations make no ``bridge:review`` call.

        The refine loop re-runs judge up to three times; each J5a call after a quota stop would fail the same way. The
        loop itself continues Claude-only, and the marker is repeated so the latest report still carries it.
        """
        rule = next(line.strip() for line in _s3() if "`codex-blocked:quota`" in line)
        assert "no further `bridge:review` call in this sweep run" in rule
        assert "skip J5a's Codex pass" in rule
        assert "latest `JUDGE_REPORT`" in rule
        assert "continues Claude-only" in rule

    @pytest.mark.parametrize(
        "lead",
        [
            pytest.param("- The `Codex:` header field of an earlier iteration's judge report", id="s3-loop"),
            pytest.param("**Quota stop carried into the run**", id="s5-seed"),
        ],
    )
    def test_stop_is_read_from_the_header_field_only(self, lead: str) -> None:
        """Both sweep decisions read the judge report's ``Codex:`` header field, never the token elsewhere.

        A report quoting program text or Codex findings that mention ``codex-blocked:quota`` must not start a stop that
        overrides the user's forwarded ``--codex``.
        """
        rule = next(line.strip() for line in _lines(_SWEEP) if line.strip().startswith(lead))
        assert "`Codex:` header field" in rule
        assert "reads `codex-blocked:quota`" in rule
        assert "never" in rule.split("header field", 2)[-1]

    def test_run_starts_with_codex_already_off(self) -> None:
        """S5 seeds the new run's quota stop right after R1, before R2, without counting a Codex pass.

        Sweep forwards ``--codex`` to the run; without the seed, Phase 2c and R7 would each spend a Codex call that
        cannot succeed. The stored flag makes both skip, while the forwarded flag still passes R2's checks. No pass ran,
        so the seed uses ``block``, which leaves ``codex_iter`` and the run report's pass count at zero.
        """
        s5 = _s5()
        lead = next(i for i, line in enumerate(s5) if line.startswith("**Quota stop carried into the run**"))
        block = next(line for line in s5[lead:] if 'codex_state.py" block' in line)
        guard = next(line for line in s5[lead : s5.index(block)] if '[ -n "$RUN_ID" ]' in line)
        assert "after run's R1 writes the run-id sentinel" in s5[lead]
        assert "before R2" in s5[lead]
        assert "no Codex pass run" in s5[lead]
        assert 'block --state ".experiments/state/${RUN_ID}/codex-state"' in block
        assert "bump" not in block
        assert "BLOCKED" in guard

    def test_completion_summary_names_the_quota_stop(self) -> None:
        """The sweep completion text tells the user that Codex stayed off for the run because of the quota stop."""
        assert any(line.startswith("sweep: Codex off — codex-blocked:quota") for line in _s5())
