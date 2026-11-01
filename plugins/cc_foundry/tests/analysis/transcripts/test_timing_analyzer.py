"""Tests for ``bin/timing_analyzer.py``: bucketing, joins, rendering, CLI."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import timing_analyzer as ta


def _write_jsonl(path: Path, rows: list[dict]) -> Path:
    """Write rows as JSONL to *path* and return it.

    Examples:
        >>> from tempfile import TemporaryDirectory
        >>> with TemporaryDirectory() as directory:
        ...     _write_jsonl(Path(directory) / "rows.jsonl", [{"n": 1}]).read_text() == '{"n": 1}\\n'
        True
    """
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


def _timing(ts: str, tool: str, duration_ms: int, session_id: str, args: str = "") -> dict:
    """Build one tool-timing row."""
    return {"ts": ts, "tool": tool, "duration_ms": duration_ms, "session_id": session_id, "args": args}


def _task_event(ts: str, event: str, desc: str | None = None) -> dict:
    """Build one background-Task invocation event; ``desc`` is only present on ``started`` events."""
    row = {"ts": ts, "tool": "Task", "event": event, "agent": "x", "project": "p"}
    return row if desc is None else {**row, "desc": desc}


@pytest.fixture(name="synthetic_logs")
def _synthetic_logs(tmp_path: Path) -> tuple[Path, Path]:
    """Build deterministic timing and invocation logs spanning aggregation buckets.

    Examples:
        >>> [path.name for path in getfixture("synthetic_logs")]
        ['timings.jsonl', 'invocations.jsonl']
    """
    timings = _write_jsonl(
        tmp_path / "timings.jsonl",
        [
            # session s1: 1 Bash + 1 Read + 1 Skill + 1 AskUserQuestion + 1 Agent
            _timing("2030-01-01T00:00:01Z", "Bash", 200, "s1", "command=ls"),
            _timing("2030-01-01T00:00:02Z", "Read", 100, "s1", "file_path=x"),
            _timing("2030-01-01T00:00:30Z", "Skill", 20_000, "s1", "skill=foundry:audit"),
            _timing("2030-01-01T00:00:35Z", "AskUserQuestion", 60_000, "s1"),
            _timing("2030-01-01T00:01:00Z", "Agent", 15_000, "s1", "type=foundry:curator desc=Audit batch"),
            # session s2: only one row
            _timing("2030-01-01T00:02:00Z", "Bash", 5_000_000_000, "s2", "command=runaway"),
        ],
    )
    invocations = _write_jsonl(tmp_path / "invocations.jsonl", [])
    return timings, invocations


class TestParsing:
    """Parsing of the ``--since`` window, timestamps, and the ``args`` field."""

    @pytest.mark.parametrize(
        ("spec", "seconds"),
        [
            pytest.param("1h", 3600.0, id="hours"),
            pytest.param("30m", 1800.0, id="minutes"),
            pytest.param("7d", 604800.0, id="days"),
            pytest.param("45s", 45.0, id="seconds"),
        ],
    )
    def test_since_units(self, spec: str, seconds: float) -> None:
        """``--since`` accepts s, m, h and d suffixes and returns the window length in seconds."""
        assert ta.parse_since(spec) == seconds

    def test_since_rejects_unknown_unit(self) -> None:
        """A suffix outside s/m/h/d raises a ``ValueError`` naming the flag instead of guessing a unit."""
        with pytest.raises(ValueError, match=r"invalid --since"):
            ta.parse_since("1week")

    @pytest.mark.parametrize(
        ("stamp", "epoch"),
        [
            pytest.param("1970-01-01T00:00:00.000Z", 0.0, id="epoch-with-millis"),
            pytest.param("1970-01-01T00:00:10Z", 10.0, id="ten-seconds-after-epoch"),
        ],
    )
    def test_timestamp_to_epoch_seconds(self, stamp: str, epoch: float) -> None:
        """ISO-8601 UTC stamps, with or without fractional seconds, convert to epoch seconds."""
        assert ta.parse_ts(stamp) == epoch

    @pytest.mark.parametrize(
        ("args", "expected"),
        [
            pytest.param("skill=codex:rescue args=foo", "codex:rescue", id="skill-with-trailing-args"),
            pytest.param("skill=foundry:audit", "foundry:audit", id="skill-only"),
            pytest.param(None, None, id="none"),
            pytest.param("", None, id="empty"),
        ],
    )
    def test_skill_name_from_args(self, args: str | None, expected: str | None) -> None:
        """The skill name is the ``skill=`` value; missing or empty args yield None."""
        assert ta.extract_skill_name(args) == expected

    @pytest.mark.parametrize(
        ("args", "expected"),
        [
            pytest.param(
                "type=foundry:curator desc=Curator audit batch",
                ("foundry:curator", "Curator audit batch"),
                id="type-and-multiword-desc",
            ),
            pytest.param(None, (None, None), id="none"),
        ],
    )
    def test_agent_and_description_from_args(self, args: str | None, expected: tuple) -> None:
        """The agent type and the rest-of-line description are split out; missing args yield ``(None, None)``."""
        assert ta.extract_agent_desc(args) == expected


class TestBucketing:
    """Which bucket a tool call lands in, and how its duration is clipped."""

    @pytest.mark.parametrize(
        ("tool", "bucket"),
        [
            pytest.param("Bash", "local", id="bash"),
            pytest.param("Agent", "agent", id="agent"),
            pytest.param("Task", "agent", id="task-is-agent"),
            pytest.param("Skill", "skill", id="skill"),
            pytest.param("AskUserQuestion", "idle", id="user-question-is-idle"),
            pytest.param("UnknownTool", "local", id="unknown-defaults-to-local"),
        ],
    )
    def test_classify_bucket(self, tool: str, bucket: str) -> None:
        """Each tool maps to its bucket, and an unrecognised tool counts as local compute."""
        assert ta.classify_bucket(tool) == bucket

    @pytest.mark.parametrize(
        ("tool", "duration_ms", "expected"),
        [
            pytest.param("Bash", 5_000, 5_000, id="bash-normal"),
            pytest.param("Bash", 5_000_000_000, 3_600_000, id="bash-runaway-clipped-to-one-hour"),
            pytest.param("Read", 99_999_999_999, 99_999_999_999, id="only-bash-is-clipped"),
            pytest.param("Bash", -5, 0, id="negative-clamped-to-zero"),
        ],
    )
    def test_clip_duration(self, tool: str, duration_ms: int, expected: int) -> None:
        """A runaway Bash call is clipped to one hour and negative durations to zero; other tools are untouched."""
        assert ta.clip_duration(tool, duration_ms) == expected


class TestSessionStats:
    """Derived wall and reasoning time of one session."""

    def test_wall_and_reasoning(self) -> None:
        """Wall time spans first to last timestamp; reasoning is the wall time not spent in any bucket."""
        s = ta.SessionStats(session_id="x", first_ts=0.0, last_ts=100.0)
        s.local_ms = 10_000
        s.agent_ms = 20_000
        s.skill_ms = 5_000
        s.idle_ms = 1_000
        assert (s.wall_ms, s.reasoning_ms) == (100_000, 100_000 - 36_000)

    def test_reasoning_clamped_nonnegative(self) -> None:
        """Bucketed time above the wall span (overlapping calls) yields zero reasoning time, never a negative one."""
        s = ta.SessionStats(session_id="x", first_ts=0.0, last_ts=1.0)
        s.local_ms = 999_999
        assert s.reasoning_ms == 0


class TestBackgroundAgentTiming:
    """Joining background agent start and completion events to recover wall time."""

    def test_invocation_pairs_are_first_in_first_out(self, tmp_path: Path) -> None:
        """Two overlapping starts pair with the completions in arrival order.

        The first start (t=0) pairs with the first completion (t=10) and the second start (t=5) with the second
        completion (t=30), giving 10 s and 25 s of wall time.
        """
        rows = [
            _task_event("1970-01-01T00:00:00Z", "started", "first"),
            _task_event("1970-01-01T00:00:05Z", "started", "second"),
            _task_event("1970-01-01T00:00:10Z", "completed"),
            _task_event("1970-01-01T00:00:30Z", "completed"),
        ]
        pairs = ta.build_invocation_pairs(_write_jsonl(tmp_path / "inv.jsonl", rows))
        assert [(p["desc"], p["wall_ms"]) for p in pairs] == [("first", 10_000), ("second", 25_000)]

    @pytest.mark.parametrize(
        ("row", "pairs", "expected"),
        [
            pytest.param(
                {"ts": "1970-01-01T00:00:10Z", "duration_ms": 50_000, "args": "type=x desc=d"},
                [],
                50_000,
                id="raw-duration-above-threshold",
            ),
            pytest.param(
                {
                    "ts": "1970-01-01T00:00:30Z",
                    "duration_ms": 80,
                    "args": "type=foundry:curator desc=Curator audit batch",
                },
                [
                    {
                        "agent": "foundry:curator",
                        "desc": "Curator audit batch 1",
                        "start": 0.0,
                        "end": 30.0,
                        "wall_ms": 30_000,
                    }
                ],
                30_000,
                id="background-pair-substituted",
            ),
            pytest.param(
                {"ts": "1970-01-01T00:00:00Z", "duration_ms": 80, "args": "type=x desc=d"},
                [{"agent": "x", "desc": "d", "start": 0.0, "end": 1000.0, "wall_ms": 1_000_000}],
                80,
                id="outside-window-falls-back",
            ),
        ],
    )
    def test_resolve_agent_ms(self, row: dict, pairs: list[dict], expected: int) -> None:
        """The agent duration is the raw value when above threshold, else the matching background pair's wall time."""
        assert ta.resolve_agent_ms(row, pairs) == expected


class TestAggregateSessions:
    """Folding timing rows into per-session bucket totals."""

    @pytest.mark.parametrize(
        ("field", "expected"),
        [
            pytest.param("local_ms", 300, id="local-bash-plus-read"),
            pytest.param("agent_ms", 15_000, id="agent-above-threshold-used-as-is"),
        ],
    )
    def test_bucket_totals(self, synthetic_logs: tuple[Path, Path], field: str, expected: int) -> None:
        """Session s1 buckets Bash 200 + Read 100 as local, and an Agent row above the 1_000 threshold as agent."""
        timings, inv = synthetic_logs
        sessions, _, _ = ta.aggregate_sessions(timings, inv, cutoff=0.0)
        assert getattr(sessions["s1"], field) == expected

    def test_skill_and_idle(self, synthetic_logs: tuple[Path, Path]) -> None:
        """A Skill row feeds the skill bucket and a skill event; an AskUserQuestion row feeds the idle bucket."""
        timings, inv = synthetic_logs
        sessions, skill_events, _ = ta.aggregate_sessions(timings, inv, cutoff=0.0)
        assert (sessions["s1"].skill_ms, sessions["s1"].idle_ms) == (20_000, 60_000)
        assert [e["skill"] for e in skill_events] == ["foundry:audit"]

    def test_bash_runaway_is_clipped_and_counted_as_warning(self, synthetic_logs: tuple[Path, Path]) -> None:
        """Session s2 holds a 5e9 ms Bash call: it is clipped to 3_600_000 ms and reported as one warning."""
        timings, inv = synthetic_logs
        sessions, _, warnings = ta.aggregate_sessions(timings, inv, cutoff=0.0)
        assert (sessions["s2"].local_ms, warnings) == (3_600_000, 1)

    def test_idle_excluded_from_compute_total(self, synthetic_logs: tuple[Path, Path]) -> None:
        """Idle time never folds into the local, agent or skill buckets, which keep their own 35_300 ms total."""
        timings, inv = synthetic_logs
        s = ta.aggregate_sessions(timings, inv, cutoff=0.0)[0]["s1"]
        assert (s.idle_ms, s.local_ms + s.agent_ms + s.skill_ms) == (60_000, 35_300)

    def test_session_filter(self, synthetic_logs: tuple[Path, Path]) -> None:
        """``session_filter`` keeps only the named session."""
        timings, inv = synthetic_logs
        sessions, _, _ = ta.aggregate_sessions(timings, inv, cutoff=0.0, session_filter="s2")
        assert set(sessions) == {"s2"}

    def test_cutoff_keeps_boundary_after_and_future_rows(self, tmp_path: Path) -> None:
        """Rows strictly before the cutoff are dropped; the boundary row, later rows and far-future rows are kept."""
        cutoff = ta.parse_ts("2030-01-01T00:00:00Z")
        timings = _write_jsonl(
            tmp_path / "timings.jsonl",
            [
                _timing("2029-12-31T23:59:59Z", "Read", 100, "before"),
                _timing("2030-01-01T00:00:00Z", "Read", 200, "boundary"),
                _timing("2030-01-01T00:00:01Z", "Read", 300, "after"),
                _timing("2035-01-01T00:00:00Z", "Read", 400, "future"),
            ],
        )
        invocations = _write_jsonl(tmp_path / "invocations.jsonl", [])
        sessions, _, _ = ta.aggregate_sessions(timings, invocations, cutoff=cutoff)
        assert {name: s.local_ms for name, s in sessions.items()} == {"boundary": 200, "after": 300, "future": 400}

    def test_top_calls_kept_sorted_by_duration(self, synthetic_logs: tuple[Path, Path]) -> None:
        """With ``top_n=2`` session s1 keeps its two longest calls, longest first: idle (60_000) then skill (20_000)."""
        timings, inv = synthetic_logs
        sessions, _, _ = ta.aggregate_sessions(timings, inv, cutoff=0.0, top_n=2)
        assert [call[0] for call in sessions["s1"].top_calls] == [60_000, 20_000]


class TestRenderReport:
    """The rendered Markdown report."""

    @pytest.mark.parametrize(
        "fragment",
        [
            "Headline split",
            "Per-session breakdown",
            "Per-skill rollup",
            "longest single calls",
            "Legend",
            "Confidence",
            "foundry:audit",
        ],
    )
    def test_includes_section(self, synthetic_logs: tuple[Path, Path], fragment: str) -> None:
        """Every report section, and the skill seen in the logs, appears in the rendered output."""
        timings, inv = synthetic_logs
        sessions, skill_events, warnings = ta.aggregate_sessions(timings, inv, cutoff=0.0)
        out = ta.render_report(sessions, skill_events, since_spec="24h", top_n=5, warnings=warnings)
        assert fragment in out


class TestMain:
    """The command line entry point."""

    @staticmethod
    def _argv(timings: Path, inv: Path, out: Path, since: str) -> list[str]:
        """Build the argument list shared by every CLI test."""
        return ["--timings", str(timings), "--invocations", str(inv), "--since", since, "--output", str(out)]

    def test_writes_report(
        self, tmp_path: Path, synthetic_logs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A run over the synthetic logs writes the report, prints its path and exits 0.

        The synthetic rows are dated year 2030, so they sit inside any sane past window such as ``30d``.
        """
        timings, inv = synthetic_logs
        out = tmp_path / "report.md"
        assert ta.main(self._argv(timings, inv, out, "30d")) == 0
        assert str(out) in capsys.readouterr().out
        body = out.read_text(encoding="utf-8")
        assert "Headline split" in body
        assert "foundry:audit" in body

    def test_report_header_is_a_complete_key_value_block(
        self, tmp_path: Path, synthetic_logs: tuple[Path, Path]
    ) -> None:
        """The written report opens with a `---` header of `Key: value` lines, `Title` first.

        The profile follow-up gate (enforce-profile-header.js) reads any other header line, or a missing `Title`, as an
        unfinished report and denies the follow-up question; the former `[Profile] -` title line did that for every
        report.
        """
        timings, inv = synthetic_logs
        out = tmp_path / "report.md"
        ta.main(self._argv(timings, inv, out, "30d"))
        header = out.read_text(encoding="utf-8").split("---\n", 2)[1].splitlines()
        assert header[0].startswith("Title:")
        assert [line for line in header if not re.match(r"^[^:\n]+:[ \t]*\S", line)] == []

    def test_empty_window_returns_1(
        self, tmp_path: Path, synthetic_logs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A timings log with no rows exits 1 and reports ``no sessions`` on stderr.

        Empty timings are used because the synthetic rows are dated year 2030 and would match any past window.
        """
        _timings, inv = synthetic_logs
        empty = _write_jsonl(tmp_path / "empty.jsonl", [])
        assert ta.main(self._argv(empty, inv, tmp_path / "report.md", "1h")) == 1
        assert "no sessions" in capsys.readouterr().err
