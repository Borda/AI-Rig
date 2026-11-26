"""Contract tests for paired before/after profile measurement and the savings-claim check."""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import profile_pair
import pytest
import timing_analyzer

#: One day in seconds.
_DAY_S = 86_400


@pytest.mark.parametrize("metric", [pytest.param(metric, id=metric.value) for metric in profile_pair.Metric])
def test_session_measurement_rejects_overlapping_observations(metric: profile_pair.Metric) -> None:
    """Distinct sessions recorded in order still need disjoint observations for every metric."""
    before = {
        "label": "review",
        "side": "before",
        "recorded_at": "1",
        "report": "before",
        "scope": {"kind": "session", "session_id": "one", "clock_session": [100, 200], "cost_session": [90, 210]},
        "metrics": {metric.value: 100, "sessions": 1, "cost_sessions": 1},
    }
    after = {
        **before,
        "side": "after",
        "recorded_at": "2",
        "report": "after",
        "scope": {"kind": "session", "session_id": "two", "clock_session": [190, 300], "cost_session": [195, 310]},
    }
    assert not profile_pair.pair_status([before, after], "review", metric).measured


def test_record_rejects_a_label_with_a_trailing_newline(tmp_path: Path) -> None:
    """A label containing a newline cannot be spelled in a Markdown annotation."""
    assert profile_pair.main(["compare", "--label=x\n", "--ledger", str(tmp_path / "pairs.jsonl")]) == 2


@pytest.fixture(autouse=True)
def clock_step(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Stamp each recorded row one step after the previous; a test may shorten the step before recording.

    Returns:
        One-item list holding the step in seconds.
    """
    step = [_DAY_S]
    ticks = itertools.count(1)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def _now() -> str:
        return (start + timedelta(seconds=next(ticks) * step[0])).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    monkeypatch.setattr(profile_pair, "_utc_now", _now)
    monkeypatch.setattr(profile_pair, "_revision", lambda: None)
    return step


@pytest.fixture
def profile_run(tmp_path: Path) -> Callable[..., Path]:
    """Return a factory writing a finished profile run directory rendered by the real clock analyzer."""
    ticks = itertools.count(1)

    def _make(
        name: str,
        *,
        wall_s: int,
        idle_s: int,
        since: str = "24h",
        cost: str | tuple[str, int, int | None] | None = None,
        session_id: str = "",
        sessions: int = 1,
    ) -> Path:
        run = tmp_path / "runs" / name
        run.mkdir(parents=True)
        end = 1_760_000_000.0 + next(ticks) * _DAY_S
        stats = {
            f"s{number}": timing_analyzer.SessionStats(
                f"s{number}",
                first_ts=end - wall_s,
                last_ts=end,
                idle_ms=idle_s * 1000,
                local_ms=(wall_s - idle_s) * 1000,
                observation=(end - wall_s, end) if session_id else None,
            )
            for number in range(1, sessions + 1)
        }
        start = end - timing_analyzer.parse_since(since)
        bounds = None if session_id else (start, end)
        report = timing_analyzer.render_report(stats, [], since_spec=since, top_n=5, warnings=0, window=bounds)
        if cost is not None:
            dollars, count, tokens = (cost, sessions, None) if isinstance(cost, str) else cost
            report += (
                f"\n## Tokens & cost\n\nCost window (UTC epoch seconds): {start} to {end}\n"
                f"**Window total: {dollars}** across all {count} session(s) above.\n"
            )
            if tokens is not None:
                report += f"**Total tokens: {tokens:,}**\n"
        (run / "report.md").write_text(report, encoding="utf-8")
        result = {"status": "complete", "cost_exit": 0, "since": since, "session_id": session_id, "top_n": 5}
        (run / "result.jsonl").write_text(json.dumps(result) + "\n", encoding="utf-8")
        return run

    return _make


def _record(ledger: Path, run: Path, side: str, label: str = "/oss:resolve") -> int:
    """Record one side through the command line and return its exit code."""
    return profile_pair.main(
        ["record", "--label", label, "--side", side, "--report", str(run), "--ledger", str(ledger)]
    )


def _status(ledger: Path, metric: profile_pair.Metric, label: str = "/oss:resolve") -> profile_pair.PairStatus:
    """Return the pair status of a label for one metric."""
    return profile_pair.pair_status(profile_pair.read_rows(ledger), label, metric)


def _copy_window(source: Path, target: Path, producer: str) -> None:
    """Give separately recorded reports an identical observation window for one analyzer."""
    source_lines = (source / "report.md").read_text(encoding="utf-8").splitlines()
    path = target / "report.md"
    target_lines = path.read_text(encoding="utf-8").splitlines()
    prefix = f"{producer} window (UTC epoch seconds):"
    window = next(line for line in source_lines if line.startswith(prefix))
    path.write_text("\n".join(window if line.startswith(prefix) else line for line in target_lines), encoding="utf-8")


class TestParseReport:
    """Metrics read from a report rendered by the real clock analyzer."""

    def test_reads_wall_idle_and_compute_from_the_clock_report(self, profile_run: Callable[..., Path]) -> None:
        """The reader uses the explicit duration of observed compute from the clock report."""
        run = profile_run("one", wall_s=3600, idle_s=600)
        metrics = profile_pair.parse_report((run / "report.md").read_text(encoding="utf-8"))
        assert (metrics["wall_s"], metrics["idle_s"], metrics["compute_s"], metrics["cost_usd"]) == (
            3600,
            600,
            3000,
            None,
        )

    def test_reads_the_window_cost_total(self, profile_run: Callable[..., Path]) -> None:
        """A present token/cost section contributes its window total, thousands separators included."""
        run = profile_run("one", wall_s=60, idle_s=0, cost="$1,234.50")
        assert profile_pair.parse_report((run / "report.md").read_text(encoding="utf-8"))["cost_usd"] == 1234.5

    def test_record_refuses_a_report_without_clock_lines(self, tmp_path: Path) -> None:
        """A report lacking the Scope header cannot become a pair row."""
        run = tmp_path / "broken"
        run.mkdir()
        (run / "report.md").write_text("# not a profile report\n", encoding="utf-8")
        (run / "result.jsonl").write_text("{}\n", encoding="utf-8")
        assert _record(tmp_path / "pairs.jsonl", run, "before") == 1


class TestPairStatus:
    """A pair is measured only with a comparable before and later after side."""

    def test_before_and_after_measure_compute_and_cost(self, tmp_path: Path, profile_run: Callable[..., Path]) -> None:
        """Two comparable sides with cost sections measure both metrics."""
        ledger = tmp_path / "pairs.jsonl"
        _record(ledger, profile_run("before", wall_s=4000, idle_s=0, cost="$20.00"), "before")
        _record(ledger, profile_run("after", wall_s=3000, idle_s=0, cost="$15.00"), "after")
        assert _status(ledger, profile_pair.Metric.COMPUTE_S).measured
        assert _status(ledger, profile_pair.Metric.COST_USD).measured

    def test_after_without_before_is_unmeasured(self, tmp_path: Path, profile_run: Callable[..., Path]) -> None:
        """A claim with only an after side has no baseline, so it stays unmeasured."""
        ledger = tmp_path / "pairs.jsonl"
        _record(ledger, profile_run("after", wall_s=3000, idle_s=0), "after")
        assert _status(ledger, profile_pair.Metric.COMPUTE_S).reason == "no before row recorded for this label"

    def test_different_windows_are_not_comparable(self, tmp_path: Path, profile_run: Callable[..., Path]) -> None:
        """A 24h before against a 7d after measures two different spans, not one change."""
        ledger = tmp_path / "pairs.jsonl"
        _record(ledger, profile_run("before", wall_s=4000, idle_s=0, since="24h"), "before")
        _record(ledger, profile_run("after", wall_s=3000, idle_s=0, since="7d"), "after")
        assert _status(ledger, profile_pair.Metric.COMPUTE_S).reason == "window lengths differ"

    def test_overlapping_windows_are_not_a_pair(
        self, tmp_path: Path, profile_run: Callable[..., Path], clock_step: list[int]
    ) -> None:
        """Overlapping clock windows remain incomparable even when recorded one minute apart."""
        clock_step[0] = 60
        ledger = tmp_path / "pairs.jsonl"
        before = profile_run("before", wall_s=4000, idle_s=0)
        after = profile_run("after", wall_s=3000, idle_s=0)
        _copy_window(before, after, "Clock")
        _record(ledger, before, "before")
        _record(ledger, after, "after")
        assert _status(ledger, profile_pair.Metric.COMPUTE_S).reason.startswith("the windows overlap")

    def test_delayed_recording_does_not_change_analyzer_windows(
        self, tmp_path: Path, profile_run: Callable[..., Path], clock_step: list[int]
    ) -> None:
        """Back-to-back observation windows remain a pair when registered close together and out of order."""
        clock_step[0] = 60
        ledger = tmp_path / "pairs.jsonl"
        before = profile_run("before", wall_s=4000, idle_s=0)
        after = profile_run("after", wall_s=3000, idle_s=0)
        _record(ledger, after, "after")
        _record(ledger, before, "before")
        status = _status(ledger, profile_pair.Metric.COMPUTE_S)
        assert status.measured
        assert status.before["scope"]["clock_window"][1] == status.after["scope"]["clock_window"][0]

    def test_cost_windows_are_compared_independently_of_clock_windows(
        self, tmp_path: Path, profile_run: Callable[..., Path]
    ) -> None:
        """Nonoverlapping clock windows cannot qualify overlapping cost windows."""
        ledger = tmp_path / "pairs.jsonl"
        before = profile_run("before", wall_s=4000, idle_s=0, cost=("$20.00", 1, 200))
        after = profile_run("after", wall_s=3000, idle_s=0, cost=("$15.00", 1, 100))
        _copy_window(before, after, "Cost")
        assert _record(ledger, before, "before") == _record(ledger, after, "after") == 0
        assert _status(ledger, profile_pair.Metric.WALL_S).measured
        assert _status(ledger, profile_pair.Metric.COST_USD).reason == "the windows overlap"
        assert _status(ledger, profile_pair.Metric.TOKENS).reason == "the windows overlap"

    def test_window_rows_without_bounds_remain_unmeasured(
        self, tmp_path: Path, profile_run: Callable[..., Path]
    ) -> None:
        """A version-one ledger row written before bounds were captured remains readable but cannot prove a window."""
        ledger = tmp_path / "pairs.jsonl"
        before = profile_pair.build_row(
            "legacy", profile_pair.Side.BEFORE, profile_run("before", wall_s=60, idle_s=0), None
        )
        del before["scope"]["clock_window"]
        profile_pair.append_row(ledger, before)
        assert _record(ledger, profile_run("after", wall_s=30, idle_s=0), "after", "legacy") == 0
        status = _status(ledger, profile_pair.Metric.COMPUTE_S, "legacy")
        assert status.before == before
        assert status.reason.startswith("analyzer window bounds missing")

    def test_legacy_window_wall_without_clock_allocation_is_unmeasured(
        self, tmp_path: Path, profile_run: Callable[..., Path]
    ) -> None:
        """An old wall total can include clock activity outside its recorded window."""
        ledger = tmp_path / "pairs.jsonl"
        before = profile_pair.build_row(
            "review", profile_pair.Side.BEFORE, profile_run("before", wall_s=60, idle_s=0), None
        )
        del before["scope"]["observed_compute"]
        profile_pair.append_row(ledger, before)
        assert _record(ledger, profile_run("after", wall_s=30, idle_s=0), "after", "review") == 0
        assert not _status(ledger, profile_pair.Metric.WALL_S, "review").measured

    def test_recording_a_report_twice_does_not_make_a_pair(
        self, tmp_path: Path, profile_run: Callable[..., Path]
    ) -> None:
        """Even records written a day apart need two distinct observations."""
        ledger = tmp_path / "pairs.jsonl"
        run = profile_run("same", wall_s=3000, idle_s=0)
        assert _record(ledger, run, "before") == _record(ledger, run, "after") == 0
        assert not _status(ledger, profile_pair.Metric.COMPUTE_S).measured

    @pytest.mark.parametrize(
        ("after_session", "measured"),
        [
            pytest.param("sess-before", False, id="same-session-twice"),
            pytest.param("sess-after", True, id="two-sessions"),
        ],
    )
    def test_session_sides_need_two_sessions(
        self, tmp_path: Path, profile_run: Callable[..., Path], after_session: str, measured: bool
    ) -> None:
        """Profiling the same session on both sides measures nothing; two different sessions form a pair."""
        ledger = tmp_path / "pairs.jsonl"
        _record(ledger, profile_run("before", wall_s=4000, idle_s=0, session_id="sess-before"), "before")
        _record(ledger, profile_run("after", wall_s=3000, idle_s=0, session_id=after_session), "after")
        assert _status(ledger, profile_pair.Metric.COMPUTE_S).measured is measured

    @pytest.mark.parametrize(
        ("before_sessions", "after_sessions"),
        [
            pytest.param(3, 2, id="fewer-sessions-after"),
            pytest.param(1, 4, id="more-sessions-after"),
        ],
    )
    def test_unequal_session_counts_are_not_a_pair(
        self, tmp_path: Path, profile_run: Callable[..., Path], before_sessions: int, after_sessions: int
    ) -> None:
        """Sides summing a different number of sessions hold different workloads, so their totals show no change.

        A window sums every session in it on this machine; an after window with fewer sessions would report a saving
        that only reflects less work, for compute time and cost alike.
        """
        ledger = tmp_path / "pairs.jsonl"
        before = profile_run("before", wall_s=4000, idle_s=0, cost="$20.00", sessions=before_sessions)
        _record(ledger, before, "before")
        _record(ledger, profile_run("after", wall_s=3000, idle_s=0, cost="$15.00", sessions=after_sessions), "after")
        statuses = [_status(ledger, metric) for metric in (profile_pair.Metric.COMPUTE_S, profile_pair.Metric.COST_USD)]
        assert [status.measured for status in statuses] == [False, False]
        assert all(status.reason.startswith("session counts differ") for status in statuses)

    def test_cost_missing_on_one_side_leaves_cost_unmeasured(
        self, tmp_path: Path, profile_run: Callable[..., Path]
    ) -> None:
        """Compute time can be measured while a cost claim on the same pair stays unmeasured."""
        ledger = tmp_path / "pairs.jsonl"
        _record(ledger, profile_run("before", wall_s=4000, idle_s=0), "before")
        _record(ledger, profile_run("after", wall_s=3000, idle_s=0, cost="$15.00"), "after")
        assert _status(ledger, profile_pair.Metric.COMPUTE_S).measured
        assert not _status(ledger, profile_pair.Metric.COST_USD).measured

    def test_after_window_preceding_the_baseline_is_unmeasured(
        self, tmp_path: Path, profile_run: Callable[..., Path]
    ) -> None:
        """An analysis window earlier than its baseline cannot show the effect of a later change."""
        ledger = tmp_path / "pairs.jsonl"
        after = profile_run("after", wall_s=3000, idle_s=0)
        before = profile_run("before", wall_s=4000, idle_s=0)
        _record(ledger, before, "before")
        _record(ledger, after, "after")
        assert _status(ledger, profile_pair.Metric.COMPUTE_S).reason == "the after window precedes the before window"

    def test_record_appends_the_comparison_to_the_report(
        self, tmp_path: Path, profile_run: Callable[..., Path]
    ) -> None:
        """``--append-to`` adds the paired section, with the relative change, to the run's report."""
        ledger = tmp_path / "pairs.jsonl"
        _record(ledger, profile_run("before", wall_s=4000, idle_s=0), "before")
        after = profile_run("after", wall_s=3000, idle_s=0)
        argv = ["record", "--label", "/oss:resolve", "--side", "after", "--report", str(after), "--ledger", str(ledger)]
        assert profile_pair.main([*argv, "--append-to", str(after / "report.md")]) == 0
        lines = (after / "report.md").read_text(encoding="utf-8").splitlines()
        assert "## Paired measurement" in lines
        assert "| compute time | 01:06:40 | 00:50:00 | -25.0% |" in lines


class TestCheckClaims:
    """A savings or cost claim is accepted only when measured by a complete pair or labelled unmeasured."""

    @pytest.fixture
    def changelog(self, tmp_path: Path) -> Callable[[str], Path]:
        """Return a writer placing one bullet under ``## Unreleased`` of a changelog."""

        def _write(bullet: str) -> Path:
            path = tmp_path / "CHANGELOG.md"
            path.write_text(f"# Changelog\n\n## Unreleased\n\n{bullet}\n\n## 0.1.0\n\n- Cuts cost.\n", encoding="utf-8")
            return path

        return _write

    def _check(self, path: Path, ledger: Path) -> int:
        """Run the claim check over the Unreleased section."""
        return profile_pair.main(["check-claims", str(path), "--section", "Unreleased", "--ledger", str(ledger)])

    @pytest.mark.parametrize("quantity", ["p95 latency", "median wall time", "average cost", "maximum tokens"])
    def test_totals_cannot_substantiate_statistic_claims(
        self, tmp_path: Path, changelog: Callable[[str], Path], profile_run: Callable[..., Path], quantity: str
    ) -> None:
        """Smaller totals do not substantiate distribution or rate claims."""
        ledger = tmp_path / "pairs.jsonl"
        _record(ledger, profile_run("before", wall_s=100, idle_s=0, cost=("$20.00", 1, 200)), "before")
        _record(ledger, profile_run("after", wall_s=50, idle_s=0, cost=("$10.00", 1, 100)), "after")
        assert self._check(changelog(f"- Cuts {quantity} by half (measured: /oss:resolve)."), ledger) == 1
        assert self._check(changelog(f"- Cuts {quantity} by half (unmeasured)."), ledger) == 0

    @pytest.mark.parametrize(
        "bullet",
        [
            "Cuts cost (measured: missing) and reduces token use (unmeasured).",
            "Updates the docs (measured: missing).",
            "Cuts cost (unmeasured) and tokens (measured: missing).",
            "Updates docs (measured: invalid(label)).",
            "Updates docs (measured: invalid",
        ],
    )
    def test_every_measured_annotation_must_resolve(
        self, tmp_path: Path, changelog: Callable[[str], Path], bullet: str
    ) -> None:
        """Neither an unmeasured annotation nor absent savings prose exempts a measured label."""
        assert self._check(changelog(f"- {bullet}"), tmp_path / "pairs.jsonl") == 1

    @pytest.mark.parametrize(
        "bullet",
        [
            pytest.param("- Cuts resolve token spend by 60%.", id="cuts"),
            pytest.param("- Save tokens on every resolve run.", id="save"),
            pytest.param("- Saves 40% of review tokens.", id="saves"),
            pytest.param("- Saved review cost on large diffs.", id="saved"),
            pytest.param("- Saving tokens on resolve.", id="saving"),
            pytest.param("- Token savings on cache rebuilds.", id="savings"),
            pytest.param("- Trims token spend by 30%.", id="trims"),
            pytest.param("- Spends less on cache writes.", id="less"),
            pytest.param("- Cuts review time by half.", id="time-without-units"),
            pytest.param("- Halving review time with batching.", id="time-savings-participle"),
            "- Review runs in half the time.",
            "- Reviews are 40% quicker.",
            "- Uses 30% less compute.",
            "- Drops review wall time from 10 to 5 minutes.",
            "- Changes review cost from $20 to $15.",
            "- Shortens review wall time by 20%.",
        ],
    )
    def test_rejects_a_claim_without_a_baseline(
        self, tmp_path: Path, changelog: Callable[[str], Path], bullet: str
    ) -> None:
        """A savings claim carrying neither a measured pair nor the unmeasured label is rejected, in every verb form."""
        assert self._check(changelog(bullet), tmp_path / "pairs.jsonl") == 1

    def test_section_missing_from_a_file_is_rejected(self, tmp_path: Path) -> None:
        """A missing pending section fails the check.

        A misspelt section name, or a changelog whose pending section is absent, would otherwise print OK for claims it
        never read.
        """
        path = tmp_path / "CHANGELOG.md"
        path.write_text("# Changelog\n\n## 0.2.0\n\n- Saves 40% of review tokens.\n", encoding="utf-8")
        assert self._check(path, tmp_path / "pairs.jsonl") == 1

    @pytest.mark.parametrize(
        ("section", "heading"),
        [
            pytest.param("0.53.0", "10.53.0", id="version-suffix"),
            pytest.param("Unreleased", "Previously Unreleased", id="heading-suffix"),
            pytest.param("Unreleased", "unreleased", id="heading-case"),
        ],
    )
    def test_only_exact_pending_heading_is_checked(self, tmp_path: Path, section: str, heading: str) -> None:
        """A similar heading never satisfies the explicitly selected section."""
        path = tmp_path / "CHANGELOG.md"
        path.write_text(f"## {heading}\n\n- Adds a flag.\n", encoding="utf-8")
        assert profile_pair.main(["check-claims", str(path), "--section", section]) == 1

    @pytest.mark.parametrize("fence", ["```", "~~~"])
    def test_fenced_heading_does_not_supply_pending_section(self, tmp_path: Path, fence: str) -> None:
        """Example headings inside either Markdown fence syntax cannot satisfy a section request."""
        path = tmp_path / "CHANGELOG.md"
        path.write_text(f"{fence}markdown\n## Unreleased\n{fence}\n", encoding="utf-8")
        assert self._check(path, tmp_path / "pairs.jsonl") == 1

    def test_rejects_a_measured_label_whose_pair_lacks_a_baseline(
        self, tmp_path: Path, changelog: Callable[[str], Path], profile_run: Callable[..., Path]
    ) -> None:
        """Naming a label that has only an after side does not make the claim measured."""
        ledger = tmp_path / "pairs.jsonl"
        _record(ledger, profile_run("after", wall_s=3000, idle_s=0, cost="$15.00"), "after")
        assert self._check(changelog("- Cuts resolve cost by 25% (measured: /oss:resolve)."), ledger) == 1

    def test_accepts_a_claim_labelled_unmeasured(self, tmp_path: Path, changelog: Callable[[str], Path]) -> None:
        """An honest ``(unmeasured)`` label is accepted."""
        assert self._check(changelog("- Cuts resolve token spend (unmeasured)."), tmp_path / "pairs.jsonl") == 0

    def test_accepts_a_claim_with_a_complete_pair(
        self, tmp_path: Path, changelog: Callable[[str], Path], profile_run: Callable[..., Path]
    ) -> None:
        """A cost claim naming a label with comparable before and after cost totals is accepted."""
        ledger = tmp_path / "pairs.jsonl"
        _record(ledger, profile_run("before", wall_s=4000, idle_s=0, cost="$20.00"), "before")
        _record(ledger, profile_run("after", wall_s=3000, idle_s=0, cost="$15.00"), "after")
        assert self._check(changelog("- Cuts resolve cost by 25% (measured: /oss:resolve)."), ledger) == 0

    @pytest.mark.parametrize(
        ("claim", "before_cost", "after_cost", "expected"),
        [
            pytest.param("review cost", ("$20.00", 2, None), ("$10.00", 1, None), 1, id="different-cost-workloads"),
            pytest.param("wall time", "$20.00", "$10.00", 1, id="wall-increases"),
            pytest.param("compute time", "$20.00", "$10.00", 0, id="compute-decreases"),
            pytest.param("review tokens", "$20.00", "$10.00", 1, id="token-count-missing"),
            pytest.param("review tokens", ("$20.00", 1, 10_000), ("$10.00", 1, 20_000), 1, id="tokens-increase"),
            pytest.param("review tokens", ("$20.00", 1, 20_000), ("$10.00", 1, 10_000), 0, id="tokens-decrease"),
            pytest.param("tokens and wall time", ("$20.00", 1, 20_000), ("$10.00", 1, 10_000), 1, id="both-metrics"),
        ],
    )
    def test_claim_uses_the_report_for_its_stated_quantity(
        self,
        tmp_path: Path,
        changelog: Callable[[str], Path],
        profile_run: Callable[..., Path],
        claim: str,
        before_cost: str | tuple[str, int, int | None],
        after_cost: str | tuple[str, int, int | None],
        expected: int,
    ) -> None:
        """Matching clock counts and falling dollars or compute cannot substitute for the quantity in the claim."""
        ledger = tmp_path / "pairs.jsonl"
        before = profile_run("before", wall_s=1000, idle_s=0, cost=before_cost, sessions=2)
        after = profile_run("after", wall_s=2000, idle_s=1500, cost=after_cost, sessions=2)
        assert _record(ledger, before, "before") == _record(ledger, after, "after") == 0
        assert self._check(changelog(f"- Cuts {claim} by half (measured: /oss:resolve)."), ledger) == expected

    @pytest.mark.parametrize(
        "bullet",
        [
            pytest.param("- Fewer false positives in the guard.", id="no-cost-or-time-word"),
            pytest.param("```text\nCuts cost by half\n```", id="inside-fenced-code"),
            "- Documents the `(measured: <label>)` syntax for cost claims.",
            "- Each approval token is single-use and allows up to 8 pending approvals.",
            "- The cost table shows cache writes. The second column lists requests that used 10% of context.",
            "- Adds a 30-second timeout for slow requests.",
        ],
    )
    def test_ignores_lines_that_are_not_claims(
        self, tmp_path: Path, changelog: Callable[[str], Path], bullet: str
    ) -> None:
        """Lines without a savings and a cost or time word, and fenced examples, are not claims."""
        assert self._check(changelog(bullet), tmp_path / "pairs.jsonl") == 0

    def test_released_sections_are_outside_the_check(self, tmp_path: Path, changelog: Callable[[str], Path]) -> None:
        """With ``--section Unreleased`` a claim in released history is not checked, since it cannot be edited."""
        assert self._check(changelog("- Adds a flag."), tmp_path / "pairs.jsonl") == 0
