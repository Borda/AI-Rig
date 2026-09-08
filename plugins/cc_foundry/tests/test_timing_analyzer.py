"""Tests for ``bin/timing_analyzer.py`` — bucketing, joins, rendering, CLI."""

from __future__ import annotations

import json
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


def test_parse_since_units():
    assert ta.parse_since("1h") == 3600.0
    assert ta.parse_since("30m") == 1800.0
    assert ta.parse_since("7d") == 604800.0
    assert ta.parse_since("45s") == 45.0


def test_parse_since_invalid():
    with pytest.raises(ValueError):
        ta.parse_since("1week")


def test_parse_ts_round_trip():
    assert ta.parse_ts("1970-01-01T00:00:00.000Z") == 0.0
    assert ta.parse_ts("1970-01-01T00:00:10Z") == 10.0


def test_classify_bucket():
    assert ta.classify_bucket("Bash") == "local"
    assert ta.classify_bucket("Agent") == "agent"
    assert ta.classify_bucket("Task") == "agent"
    assert ta.classify_bucket("Skill") == "skill"
    assert ta.classify_bucket("AskUserQuestion") == "idle"
    assert ta.classify_bucket("UnknownTool") == "local"


def test_clip_bash_runaway():
    assert ta.clip_duration("Bash", 5_000) == 5_000
    assert ta.clip_duration("Bash", 5_000_000_000) == 3_600_000
    assert ta.clip_duration("Read", 99_999_999_999) == 99_999_999_999  # only Bash clipped
    assert ta.clip_duration("Bash", -5) == 0


def test_extract_skill_name():
    assert ta.extract_skill_name("skill=codex:rescue args=foo") == "codex:rescue"
    assert ta.extract_skill_name("skill=foundry:audit") == "foundry:audit"
    assert ta.extract_skill_name(None) is None
    assert ta.extract_skill_name("") is None


def test_extract_agent_desc():
    a, d = ta.extract_agent_desc("type=foundry:curator desc=Curator audit batch")
    assert a == "foundry:curator"
    assert d == "Curator audit batch"
    assert ta.extract_agent_desc(None) == (None, None)


def test_session_stats_wall_and_reasoning():
    s = ta.SessionStats(session_id="x", first_ts=0.0, last_ts=100.0)
    s.local_ms = 10_000
    s.agent_ms = 20_000
    s.skill_ms = 5_000
    s.idle_ms = 1_000
    assert s.wall_ms == 100_000
    assert s.reasoning_ms == 100_000 - 36_000  # 64,000


def test_session_stats_reasoning_clamped_nonneg():
    s = ta.SessionStats(session_id="x", first_ts=0.0, last_ts=1.0)
    s.local_ms = 999_999
    assert s.reasoning_ms == 0


def test_invocation_pairs_fifo(tmp_path: Path):
    rows = [
        {
            "ts": "1970-01-01T00:00:00Z",
            "tool": "Task",
            "event": "started",
            "agent": "x",
            "desc": "first",
            "project": "p",
        },
        {
            "ts": "1970-01-01T00:00:05Z",
            "tool": "Task",
            "event": "started",
            "agent": "x",
            "desc": "second",
            "project": "p",
        },
        {
            "ts": "1970-01-01T00:00:10Z",
            "tool": "Task",
            "event": "completed",
            "agent": "x",
            "project": "p",
        },
        {
            "ts": "1970-01-01T00:00:30Z",
            "tool": "Task",
            "event": "completed",
            "agent": "x",
            "project": "p",
        },
    ]
    f = _write_jsonl(tmp_path / "inv.jsonl", rows)
    pairs = ta.build_invocation_pairs(f)
    assert len(pairs) == 2
    # FIFO: first started pairs with first completed
    assert pairs[0]["desc"] == "first"
    assert pairs[0]["wall_ms"] == 10_000
    assert pairs[1]["desc"] == "second"
    assert pairs[1]["wall_ms"] == 25_000


def test_resolve_agent_ms_uses_raw_when_above_threshold():
    row = {"ts": "1970-01-01T00:00:10Z", "duration_ms": 50_000, "args": "type=x desc=d"}
    assert ta.resolve_agent_ms(row, []) == 50_000


def test_resolve_agent_ms_substitutes_background_pair():
    row = {
        "ts": "1970-01-01T00:00:30Z",
        "duration_ms": 80,
        "args": "type=foundry:curator desc=Curator audit batch",
    }
    pairs = [
        {
            "agent": "foundry:curator",
            "desc": "Curator audit batch 1",
            "start": 0.0,
            "end": 30.0,
            "wall_ms": 30_000,
        }
    ]
    assert ta.resolve_agent_ms(row, pairs) == 30_000


def test_resolve_agent_ms_outside_window_falls_back():
    row = {
        "ts": "1970-01-01T00:00:00Z",
        "duration_ms": 80,
        "args": "type=x desc=d",
    }
    pairs = [{"agent": "x", "desc": "d", "start": 0.0, "end": 1000.0, "wall_ms": 1_000_000}]
    assert ta.resolve_agent_ms(row, pairs) == 80


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
            {
                "ts": "2030-01-01T00:00:01Z",
                "tool": "Bash",
                "duration_ms": 200,
                "session_id": "s1",
                "args": "command=ls",
            },
            {
                "ts": "2030-01-01T00:00:02Z",
                "tool": "Read",
                "duration_ms": 100,
                "session_id": "s1",
                "args": "file_path=x",
            },
            {
                "ts": "2030-01-01T00:00:30Z",
                "tool": "Skill",
                "duration_ms": 20_000,
                "session_id": "s1",
                "args": "skill=foundry:audit",
            },
            {
                "ts": "2030-01-01T00:00:35Z",
                "tool": "AskUserQuestion",
                "duration_ms": 60_000,
                "session_id": "s1",
                "args": "",
            },
            {
                "ts": "2030-01-01T00:01:00Z",
                "tool": "Agent",
                "duration_ms": 15_000,
                "session_id": "s1",
                "args": "type=foundry:curator desc=Audit batch",
            },
            # session s2: only one row
            {
                "ts": "2030-01-01T00:02:00Z",
                "tool": "Bash",
                "duration_ms": 5_000_000_000,
                "session_id": "s2",
                "args": "command=runaway",
            },
        ],
    )
    invocations = _write_jsonl(tmp_path / "invocations.jsonl", [])
    return timings, invocations


def test_aggregate_local_bucket(synthetic_logs):
    timings, inv = synthetic_logs
    sessions, _, _ = ta.aggregate_sessions(timings, inv, cutoff=0.0)
    # s1 local: Bash 200 + Read 100 = 300
    assert sessions["s1"].local_ms == 300


def test_aggregate_skill_and_idle(synthetic_logs):
    timings, inv = synthetic_logs
    sessions, skill_events, _ = ta.aggregate_sessions(timings, inv, cutoff=0.0)
    assert sessions["s1"].skill_ms == 20_000
    assert sessions["s1"].idle_ms == 60_000
    assert len(skill_events) == 1
    assert skill_events[0]["skill"] == "foundry:audit"


def test_aggregate_agent_bucket(synthetic_logs):
    timings, inv = synthetic_logs
    sessions, _, _ = ta.aggregate_sessions(timings, inv, cutoff=0.0)
    # Agent duration_ms 15_000 >= 1_000 threshold → used as-is
    assert sessions["s1"].agent_ms == 15_000


def test_aggregate_bash_clip_and_warning_count(synthetic_logs):
    timings, inv = synthetic_logs
    sessions, _, warnings = ta.aggregate_sessions(timings, inv, cutoff=0.0)
    # s2: 5e9 ms Bash → clipped to 3_600_000 ms
    assert sessions["s2"].local_ms == 3_600_000
    assert warnings == 1


def test_aggregate_idle_excluded_from_compute_total(synthetic_logs):
    timings, inv = synthetic_logs
    sessions, _, _ = ta.aggregate_sessions(timings, inv, cutoff=0.0)
    s = sessions["s1"]
    # compute total = wall - idle; idle never folds into local/agent/skill
    assert s.idle_ms == 60_000
    assert s.local_ms + s.agent_ms + s.skill_ms == 35_300  # no idle mixed in


def test_aggregate_session_filter(synthetic_logs):
    timings, inv = synthetic_logs
    sessions, _, _ = ta.aggregate_sessions(timings, inv, cutoff=0.0, session_filter="s2")
    assert set(sessions.keys()) == {"s2"}


def test_aggregate_cutoff_includes_boundary_after_and_future_rows(tmp_path: Path):
    cutoff = ta.parse_ts("2030-01-01T00:00:00Z")
    timings = _write_jsonl(
        tmp_path / "timings.jsonl",
        [
            {
                "ts": "2029-12-31T23:59:59Z",
                "tool": "Read",
                "duration_ms": 100,
                "session_id": "before",
                "args": "",
            },
            {
                "ts": "2030-01-01T00:00:00Z",
                "tool": "Read",
                "duration_ms": 200,
                "session_id": "boundary",
                "args": "",
            },
            {
                "ts": "2030-01-01T00:00:01Z",
                "tool": "Read",
                "duration_ms": 300,
                "session_id": "after",
                "args": "",
            },
            {
                "ts": "2035-01-01T00:00:00Z",
                "tool": "Read",
                "duration_ms": 400,
                "session_id": "future",
                "args": "",
            },
        ],
    )
    invocations = _write_jsonl(tmp_path / "invocations.jsonl", [])

    sessions, _, _ = ta.aggregate_sessions(timings, invocations, cutoff=cutoff)

    assert set(sessions) == {"boundary", "after", "future"}
    assert sessions["boundary"].local_ms == 200
    assert sessions["after"].local_ms == 300
    assert sessions["future"].local_ms == 400


def test_aggregate_top_n_kept_sorted(synthetic_logs):
    timings, inv = synthetic_logs
    sessions, _, _ = ta.aggregate_sessions(timings, inv, cutoff=0.0, top_n=2)
    top = sessions["s1"].top_calls
    assert len(top) == 2
    # Idle (60_000) and Skill (20_000) are largest
    assert top[0][0] == 60_000
    assert top[1][0] == 20_000


def test_render_report_includes_all_sections(synthetic_logs):
    timings, inv = synthetic_logs
    sessions, skill_events, warnings = ta.aggregate_sessions(timings, inv, cutoff=0.0)
    out = ta.render_report(sessions, skill_events, since_spec="24h", top_n=5, warnings=warnings)
    for header in [
        "Headline split",
        "Per-session breakdown",
        "Per-skill rollup",
        "longest single calls",
        "Legend",
        "Confidence",
    ]:
        assert header in out
    assert "foundry:audit" in out


def test_main_writes_report(tmp_path: Path, synthetic_logs, capsys):
    """Synthetic rows are dated year 2030 — they sit in any sane past window."""
    timings, inv = synthetic_logs
    out = tmp_path / "report.md"
    rc = ta.main(["--timings", str(timings), "--invocations", str(inv), "--since", "30d", "--output", str(out)])
    assert rc == 0
    captured = capsys.readouterr()
    assert str(out) in captured.out
    assert out.exists()
    body = out.read_text(encoding="utf-8")
    assert "Headline split" in body
    assert "foundry:audit" in body


def test_main_returns_1_when_empty_window(tmp_path: Path, synthetic_logs, capsys):
    timings, inv = synthetic_logs
    out = tmp_path / "report.md"
    # 1s window into past — no rows match (rows are dated year 2030, time.time() << those ts)
    # but cutoff = now - 1s; rows in year 2030 have ts >> now → they DO match.
    # Use empty timings instead.
    empty = _write_jsonl(tmp_path / "empty.jsonl", [])
    rc = ta.main(["--timings", str(empty), "--invocations", str(inv), "--since", "1h", "--output", str(out)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "no sessions" in err
