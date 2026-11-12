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
        """The skill passes ``$ARGUMENTS`` once; every documented flag is recognised from it.

        One ``--arguments=`` string carries ``--since``, ``--session``, ``--anonymize`` and a quoted ``--output`` value
        with a space; all four must land in ``DebriefOptions``.
        """
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
        """Malformed input is refused rather than silently widening the report to all history.

        A typo'd flag or unparsable date that fell through to defaults would aggregate every record without warning.
        """
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
        """Index refresh latency never lands in the query percentiles.

        The fixture holds one 900 ms ``index`` record next to 10 ms queries; each must report only its own maximum, and
        the stale-index count must still be derived from the query records.
        """
        cohort = _run(capsys)["cohorts"]["0.40.0"]["cli"]

        assert cohort["timing_ms"]["index"]["max"] == 900
        assert cohort["timing_ms"]["query"]["max"] == 10
        assert cohort["stale"] == {"stale": 1, "queries_with_index": 1}

    def test_session_and_since_filters_apply_before_aggregation(
        self, project: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``--session`` and ``--since`` narrow the dataset; a layer without the session is reported, not an error.

        Asking for a session that only has May records, with a June cutoff, leaves nothing in any layer; the report
        names those layers instead of failing.
        """
        stats = _run(capsys, "--since", "2026-06-01", "--session", "other")

        assert stats["records"] == {"cli": 0, "skills": 0, "tools": 0}
        assert stats["session_not_found_in"] == ["cli", "skills", "tools"]

    def test_default_report_path_never_overwrites(self, tmp_path: Path) -> None:
        """A same-day re-run gets a counter suffix instead of replacing the earlier report.

        With ``debrief-2026-06-20.md`` already on disk, the default path for that day must become ``-2``.
        """
        first = tmp_path / ".reports" / "codemap" / "debrief-2026-06-20.md"
        first.parent.mkdir(parents=True)
        first.write_text("old")

        assert report_path(tmp_path, DebriefOptions(), date(2026, 6, 20)).name == "debrief-2026-06-20-2.md"


class TestAnonymize:
    """``--anonymize`` runs the real anonymizer into a run-scoped export directory."""

    def test_reads_only_fresh_export(self, project: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Shards left by an earlier export never leak into this run's report.

        A stale ``export/`` tree used to satisfy the "non-empty export" test and pull every earlier session in.
        """
        stale = project / ".cache" / "codemap" / "export" / "claude"
        stale.mkdir(parents=True)
        (stale / "cli_old-anon.jsonl").write_text(json.dumps(_cli("2026-06-20T11:00:00Z", "fn-rdeps")) + "\n")

        stats = _run(capsys, "--anonymize", "--session", _SESSION)

        assert stats["dataset"] == "anonymized", stats["anonymize"]
        assert (project / stats["anonymize"]["export_dir"]).parent == project / ".cache" / "codemap" / "export"
        assert "fn-rdeps" not in stats["cohorts"]["0.40.0"]["cli"]["subcommands"]
        assert stats["filters"]["session"] != _SESSION


class TestFailureExits:
    """Documented non-zero exits: a hung sibling helper (3) and an empty log root (1)."""

    def test_anonymizer_failure_stops_without_report_path(
        self, project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A real failed anonymizer must never produce statistics from raw fallback data."""
        fake_bin = project / "failed-bin"
        fake_bin.mkdir()
        (fake_bin / "anonymize.py").write_text("raise SystemExit(1)\n", encoding="utf-8")
        monkeypatch.setattr(debrief_stats, "_BIN_DIR", fake_bin)
        result = debrief_stats.main(["--anonymize", "--session", _SESSION, "--output", "private/report-anon.md"])
        captured = capsys.readouterr()
        assert result == 4
        assert captured.out == ""
        assert _SESSION not in captured.err
        assert "anonymization failed" in captured.err
        assert not (project / "private").exists()

    def test_anonymized_stats_omit_absolute_log_and_export_paths(
        self, project: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Successful anonymization keeps local log and export locations out of the report data."""
        stats = _run(capsys, "--anonymize")
        assert not Path(stats["log_root"]).is_absolute()
        assert not Path(stats["anonymize"]["export_dir"]).is_absolute()
        assert str(project) not in json.dumps({key: value for key, value in stats.items() if key != "report_path"})

    def test_hung_helper_exits_three_and_removes_scratch(
        self, project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A sibling helper that never returns is killed at the cap; the run exits 3 and the raw scratch copy is gone.

        Without a cap the skill's 120 s Bash kill cut the run off instead, skipping the ``TemporaryDirectory`` cleanup
        and leaving the filtered raw telemetry copy behind. A real sleeping ``join_avoidance.py`` stands in for the
        hang, so the real subprocess kill is exercised, with the cap shortened to keep the test quick.
        """
        fake_bin = project / "fake_bin"
        fake_bin.mkdir()
        (fake_bin / "join_avoidance.py").write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
        scratch_root = project / "scratch-root"
        scratch_root.mkdir()
        monkeypatch.setattr(debrief_stats, "_BIN_DIR", fake_bin)
        monkeypatch.setattr(debrief_stats, "HELPER_TIMEOUT_SECONDS", 0.2)
        monkeypatch.setattr("tempfile.tempdir", str(scratch_root))

        exit_code = debrief_stats.main([])

        assert exit_code == debrief_stats.EXIT_HELPER_TIMEOUT == 3
        assert "! join_avoidance.py did not finish within 0.2 s" in capsys.readouterr().err
        assert list(scratch_root.iterdir()) == []

    def test_missing_telemetry_exits_one(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """An empty log root stops with exit 1 instead of printing an all-zero report.

        A project that never logged anything must not produce a plausible-looking but empty debrief.
        """
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("CODEMAP_LOG_DIR", raising=False)

        assert debrief_stats.main([]) == 1
