"""Tests for ``bin/debrief_stats.py`` — telemetry aggregation behind the debrief-coding skill."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import debrief_stats
import pytest
from debrief_stats import DebriefOptions, OptionsError, parse_options, report_path, tokens_from_argv

_SESSION = "11111111-2222-3333-4444-555555555555"


def _cli(ts: str, cmd: str, version: str | None = "0.40.0", session: str = _SESSION, **extra: object) -> dict:
    """Build one CLI telemetry record."""
    record = {"ts": ts, "layer": "cli", "session": session, "cmd": cmd, "exit_code": 0, "timing_ms": 10}
    if version is not None:
        record["v"] = version
    return record | extra


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a non-git project whose log root holds Claude and legacy shards plus one malformed line."""
    logs = tmp_path / ".cache" / "codemap" / "logs"
    (logs / "claude").mkdir(parents=True)
    claude_lines = [
        _cli("2026-06-20T10:00:00Z", "rdeps", result={"index": {"stale": True, "not_covered": ["lazy-loading"]}}),
        _cli("2026-06-20T10:01:00Z", "index", timing_ms=900, result={"trigger": "hook", "changed_count": 2}),
        _cli("2026-06-20T10:02:00Z", "rdeps", version="0.39.0", exit_code=1, result={"error": "Symbol nosuch"}),
        _cli("2026-06-20T10:03:00Z", "rdeps", version=None),
        _cli("2026-06-20T10:04:00Z", "rdeps", source="bench"),
        _cli("2026-05-01T10:00:00Z", "rdeps", session="other"),
    ]
    text = "\n".join(json.dumps(line) for line in claude_lines) + "\nnot json\n"
    (logs / "claude" / f"cli_{_SESSION}.jsonl").write_text(text)
    skill = {"ts": "2026-06-20T09:59:00Z", "v": "0.40.0", "session": _SESSION, "skill": "query-code", "event": "start"}
    (logs / f"skills_{_SESSION}.jsonl").write_text(json.dumps(skill) + "\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CODEMAP_LOG_DIR", raising=False)
    return tmp_path


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> dict:
    """Run the helper and return its JSON."""
    assert debrief_stats.main(list(argv)) == 0
    return json.loads(capsys.readouterr().out)


class TestParseOptions:
    """Flag parsing from ``$ARGUMENTS`` text."""

    def test_reads_all_flags_from_arguments_text(self) -> None:
        """The skill passes ``$ARGUMENTS`` once; every documented flag is recognised from it."""
        tokens = tokens_from_argv(['--arguments=--since 2026-06-15 --session abc --anonymize --output "a b.md"'])

        assert parse_options(tokens) == DebriefOptions(date(2026, 6, 15), "abc", True, "a b.md")

    @pytest.mark.parametrize(
        "tokens",
        [
            pytest.param(["--bogus"], id="unknown-flag"),
            pytest.param(["--since", "15/06/2026"], id="bad-date"),
            pytest.param(["--session"], id="missing-value"),
            pytest.param(["stray"], id="positional"),
        ],
    )
    def test_rejects_malformed_flags(self, tokens: list[str]) -> None:
        """Malformed input is refused rather than silently widening the report to all history."""
        with pytest.raises(OptionsError):
            parse_options(tokens)


class TestAggregation:
    """Aggregates computed from a real log tree."""

    def test_counts_exclusions_cohorts_and_malformed_lines(
        self, project: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Bench, empty-``v`` and malformed records are counted apart; versions never pool.

        The newest version is the primary cohort; the 0.39.0 failure stays in its own cohort.
        """
        stats = _run(capsys)

        assert stats["malformed_lines"] == 1
        assert stats["excluded_scripted"]["cli"] == 1
        assert stats["unversioned_excluded"]["cli"] == 1
        assert stats["primary_cohort"] == "0.40.0"
        assert stats["cohorts"]["0.40.0"]["cli"]["outcomes"] == {"success": 3}
        assert stats["cohorts"]["0.39.0"]["cli"]["top_errors"] == [{"prefix": "Symbol nosuch", "count": 1}]

    def test_splits_index_and_query_timing(self, project: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Index refresh latency never lands in the query percentiles."""
        cohort = _run(capsys)["cohorts"]["0.40.0"]["cli"]

        assert cohort["timing_ms"]["index"]["max"] == 900
        assert cohort["timing_ms"]["query"]["max"] == 10
        assert cohort["stale"] == {"stale": 1, "queries_with_index": 1}

    def test_session_and_since_filters_apply_before_aggregation(
        self, project: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``--session`` and ``--since`` narrow the dataset; a layer without the session is reported, not an error."""
        stats = _run(capsys, "--since", "2026-06-01", "--session", "other")

        assert stats["records"] == {"cli": 0, "skills": 0, "tools": 0}
        assert stats["session_not_found_in"] == ["cli", "skills", "tools"]

    def test_default_report_path_never_overwrites(self, tmp_path: Path) -> None:
        """A same-day re-run gets a counter suffix instead of replacing the earlier report."""
        first = tmp_path / ".reports" / "codemap" / "debrief-2026-06-20.md"
        first.parent.mkdir(parents=True)
        first.write_text("old")

        assert report_path(tmp_path, DebriefOptions(), date(2026, 6, 20)).name == "debrief-2026-06-20-2.md"


class TestAnonymize:
    """``--anonymize`` runs the real anonymizer into a run-scoped export directory."""

    def test_reads_only_the_fresh_export(self, project: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Shards left by an earlier export never leak into this run's report.

        A stale ``export/`` tree used to satisfy the "non-empty export" test and pull every earlier session in.
        """
        stale = project / ".cache" / "codemap" / "export" / "claude"
        stale.mkdir(parents=True)
        (stale / "cli_old-anon.jsonl").write_text(json.dumps(_cli("2026-06-20T11:00:00Z", "fn-rdeps")) + "\n")

        stats = _run(capsys, "--anonymize", "--session", _SESSION)

        assert stats["dataset"] == "anonymized", stats["anonymize"]
        assert Path(stats["anonymize"]["export_dir"]).parent == project / ".cache" / "codemap" / "export"
        assert "fn-rdeps" not in stats["cohorts"]["0.40.0"]["cli"]["subcommands"]
        assert stats["filters"]["session"] != _SESSION


def test_hung_helper_exits_three_and_removes_scratch(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A sibling helper that never returns is killed at the cap; the run exits 3 and the raw scratch copy is gone.

    Without a cap the skill's 120 s Bash kill cut the run off instead, skipping the ``TemporaryDirectory`` cleanup and
    leaving the filtered raw telemetry copy behind. A real sleeping ``join_avoidance.py`` stands in for the hang.
    """
    fake_bin = project / "fake_bin"
    fake_bin.mkdir()
    (fake_bin / "join_avoidance.py").write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    scratch_root = project / "scratch-root"
    scratch_root.mkdir()
    monkeypatch.setattr(debrief_stats, "_BIN_DIR", fake_bin)
    monkeypatch.setattr(debrief_stats, "HELPER_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr("tempfile.tempdir", str(scratch_root))

    exit_code = debrief_stats.main([])

    assert exit_code == debrief_stats.EXIT_HELPER_TIMEOUT == 3
    assert "! join_avoidance.py did not finish within 0.5 s" in capsys.readouterr().err
    assert list(scratch_root.iterdir()) == []


def test_missing_telemetry_exits_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty log root stops with exit 1 instead of printing an all-zero report."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CODEMAP_LOG_DIR", raising=False)

    assert debrief_stats.main([]) == 1
