"""Tests for join_avoidance.py — the avoidance-side telemetry join.

The join flags a Grep/Read/Glob tool call as an overlap after a complete module answer within the window. This is a
name/time proxy, not evidence that the guard leaked or the source read was redundant. These tests pin the criteria:

- a grep on module X within the window AFTER a complete answer on X → exactly 1 event;
- a grep BEFORE the answer, or on an unrelated module, or after an INCOMPLETE answer → 0;
- the module-match rule is word-boundary safe (ported from guard-redundant-scan.js);
- totals, per-session, and per-skill attribution aggregate correctly.

conftest.py puts bin/ on sys.path, so the module imports directly.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import join_avoidance as ja

_EXIT_USAGE = 2  # join_avoidance.main's own bare literal (bin/join_avoidance.py:610) — no exported constant to import

_BASE = datetime(2026, 7, 10, 1, 0, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("field", ["v", "project"])
def test_join_keeps_evidence_cohorts_separate(field: str) -> None:
    """A shared session and module cannot bridge versions or project roots."""
    answer = _cli("pkg.mod", 0) | {field: "/work/first" if field == "project" else "0.39.3"}
    tool = _tool("pkg.mod", 1) | {field: "/work/second" if field == "project" else "0.39.4"}
    assert len(ja.parse_cli_records([answer])) == 1
    assert len(ja.parse_tool_records([tool])) == 1
    assert ja.find_avoidance_events(ja.parse_cli_records([answer]), ja.parse_tool_records([tool])) == []


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"session": ""}, id="empty-session"),
        pytest.param({"exit_code": 2}, id="failed-command"),
        pytest.param({"source": "bench"}, id="diagnostic-source"),
        pytest.param({"result": {"module": "pkg.mod", "index": {"query_complete": True, "stale": True}}}, id="stale"),
        pytest.param(
            {"result": {"module": "pkg.mod", "index": {"query_complete": False, "exhaustive": True}}},
            id="conflicting-legacy",
        ),
    ],
)
def test_invalid_answer_cannot_support_overlap(change: dict) -> None:
    """Rejected, stale, diagnostic, and unjoinable answers stay outside the numerator."""
    assert ja.parse_cli_records([_cli("pkg.mod", 0) | change]) == []


def test_module_identity_never_comes_from_option_value() -> None:
    """An index path is not evidence for a queried module."""
    assert ja._module_from_cli_result({"argv": ["central", "--index", "pkg/mod.json"]}) == ""
    assert ja._module_from_cli_result({"argv": ["rdeps", "pkg", "--index", "other.json"]}) == "pkg"
    assert ja._module_from_cli_result({"result": {"qname": "pkg.mod::Thing"}}) == "pkg.mod"


def test_batch_counts_successful_logical_answers_only() -> None:
    """One batch invocation can expose several answers without counting failed children."""
    record = _cli("pkg.mod", 0)
    result = record["result"]
    record.update(
        cmd="batch",
        argv=["batch"],
        result={
            "batch": [
                {"cmd": "rdeps", "ok": True, "result": result},
                {"cmd": "rdeps", "ok": False, "result": result},
            ]
        },
    )
    answers = ja.parse_cli_records([record])
    assert [answer.module for answer in answers] == ["pkg.mod"]
    summary = ja.summarize(answers, ja.parse_tool_records([_tool("pkg/mod.py", 1, tool="Read")]))
    payload = json.loads(ja.render_json(summary))
    assert payload["avoidance_count"] == 1
    assert payload["metric"] == "module_overlap_proxy_v3"
    assert payload["confirmed_misuse"] is None
    assert "not confirmed misuse" in ja.render_text(summary)


@pytest.mark.parametrize(
    ("tool", "target", "search_path", "kind"),
    [
        pytest.param("Read", "src/pkg/mod.py", "", "source_read", id="read-own-file"),
        pytest.param("Read", "src\\pkg\\mod.py", "", "source_read", id="read-own-file-windows"),
        pytest.param("Read", "src/pkg/mod.py.bak", "", "source_read", id="read-backup-copy"),
        pytest.param(
            "Bash", "grep -n 'os.replace\\|tempfile' src/pkg/mod.py | head", "", "source_read", id="grep-own-file"
        ),
        pytest.param("Bash", "grep -n 'x' src/pkg/mod.py", "", "source_read", id="grep-single-file"),
        pytest.param("Bash", "rg -n 'x' src/pkg/mod/__init__.py", "", "source_read", id="rg-own-package-init"),
        pytest.param("Bash", "rg -n 'pkg.mod' src/pkg/other.py", "", "unknown", id="rg-other-single-file"),
        pytest.param("Bash", "grep -rn 'pkg.mod' src/pkg/other.py", "", "unknown", id="grep-r-other-single-file"),
        pytest.param("Bash", "grep -rn 'pkg.mod' src tests", "", "unknown", id="grep-r-tree"),
        pytest.param("Bash", "egrep -rn 'pkg.mod' src", "", "unknown", id="egrep-r-tree"),
        pytest.param("Bash", "fgrep -r 'pkg.mod' src", "", "unknown", id="fgrep-r-tree"),
        pytest.param("Bash", "grep -n --include=*.py -r 'import pkg.mod' .", "", "unknown", id="grep-include-tree"),
        pytest.param("Bash", "rg -n 'from pkg.mod import' src", "", "unknown", id="rg-tree"),
        pytest.param("Grep", "pkg.mod", "", "unknown", id="grep-tool-unscoped"),
        pytest.param("Grep", "pkg.mod", "src", "structural_search", id="grep-tool-tree-scope"),
        pytest.param("Grep", "pkg.mod", "src/pkg/mod.py", "source_read", id="grep-tool-own-file-scope"),
        pytest.param("Grep", "pkg.mod", "src/pkg/other.py", "source_read", id="grep-tool-other-file-scope"),
        pytest.param("Glob", "**/pkg/mod*", "", "unknown", id="glob-tool"),
        pytest.param("Glob", "*.py", "src/pkg/mod/__init__.py", "source_read", id="glob-tool-own-file-scope"),
    ],
)
def test_overlap_kind_separates_source_reads_from_structural_searches(
    tool: str, target: str, search_path: str, kind: str
) -> None:
    """Directory scope is structural; recursive-looking shell spelling alone leaves scope unknown."""
    scope = {
        "src": "directory",
        "src/pkg/mod.py": "file",
        "src/pkg/other.py": "file",
        "src/pkg/mod/__init__.py": "file",
    }
    assert ja.classify_overlap("pkg.mod", tool, target, search_path, scope.get(search_path, "")) == kind


@pytest.mark.parametrize(
    ("tool", "target"),
    [
        pytest.param("Grep", "pkg.mod", id="legacy-grep"),
        pytest.param("Bash", "rg -n 'pkg.mod' src/pkg/other.py", id="unverified-shell-scope"),
    ],
)
def test_unknown_count_separates_unclassifiable_from_zero_structural(tool: str, target: str) -> None:
    """Unverified search scope is counted as unknown, overall and per runtime, never as zero structural.

    A cohort of pre-scope records and a cohort of classified records both report ``structural_search_count: 0``; only
    ``unknown_count`` tells "no tree searches" from "nothing classifiable".
    """
    answers = ja.parse_cli_records([_cli("pkg.mod", 0)])
    events = ja.parse_tool_records([_tool("src/pkg/mod.py", 1, tool="Read"), _tool(target, 2, tool=tool)])
    summary = ja.summarize(answers, events)
    payload = json.loads(ja.render_json(summary))
    assert payload["avoidance_count"] == 2
    assert payload["structural_search_count"] == 0
    assert payload["unknown_count"] == 1
    assert payload["per_runtime"]["unattributed"]["unknown_count"] == 1
    assert "unknown scope among them:       1" in ja.render_text(summary)
    assert "unknown 1" in ja.render_text(summary)


def test_structural_search_count_is_subset_of_avoidance_count() -> None:
    """A source read and a tree grep both overlap, but only the grep counts as structural.

    The legacy ``avoidance_count`` stays a plain overlap proxy; the new counter narrows it without changing it, per
    runtime and overall. A Grep-tool record scoped to the own file joins through ``search_path`` as a source read.
    """
    answers = ja.parse_cli_records([_cli("pkg.mod", 0)])
    events = ja.parse_tool_records(
        [
            _tool("src/pkg/mod.py", 1, tool="Read"),
            _tool("grep -rn 'pkg.mod' src tests", 2, tool="Bash"),
            _tool("pkg.mod", 3) | {"search_path": "src/pkg/mod.py", "search_scope": "file"},
            _tool("pkg.mod", 4) | {"search_path": "src", "search_scope": "directory"},
        ]
    )
    summary = ja.summarize(answers, events)
    payload = json.loads(ja.render_json(summary))
    assert payload["avoidance_count"] == 4
    assert payload["structural_search_count"] == 1
    assert payload["unknown_count"] == 1
    assert [event["kind"] for event in payload["events"]] == [
        "source_read",
        "unknown",
        "source_read",
        "structural_search",
    ]
    assert payload["per_runtime"]["unattributed"]["structural_search_count"] == 1
    assert "structural searches among them: 1" in ja.render_text(summary)


def _ts(offset_min: float) -> str:
    """Return an ISO-Z timestamp *offset_min* minutes after the fixture base time.

    >>> _ts(30)
    '2026-07-10T01:30:00Z'
    """
    return (_BASE + timedelta(minutes=offset_min)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cli(module: str, offset_min: float, *, complete: bool = True, session: str = "s1") -> dict:
    """Build a cli.jsonl record answering *module* at *offset_min*, complete or not.

    >>> _cli("pkg.mod", 0)["argv"]
    ['rdeps', 'pkg.mod']
    """
    return {
        "ts": _ts(offset_min),
        "layer": "cli",
        "project": "/work/project",
        "v": "0.39.4",
        "exit_code": 0,
        "cmd": "rdeps",
        "session": session,
        "argv": ["rdeps", module],
        "timing_ms": 12,
        "result": {"module": module, "index": {"query_complete": complete, "exhaustive": complete}},
    }


def _tool(target: str, offset_min: float, *, tool: str = "Grep", session: str = "s1") -> dict:
    """Build a tools.jsonl record for a *tool* call on *target* at *offset_min*.

    >>> _tool("pkg.mod", 5)["tool"]
    'Grep'
    """
    return {
        "ts": _ts(offset_min),
        "layer": "tool",
        "tool": tool,
        "session": session,
        "target": target,
        "project": "/work/project",
        "v": "0.39.4",
    }


class TestModuleMatches:
    """The ported word-boundary matcher must accept real references, reject near-misses."""

    @pytest.mark.parametrize(
        ("module", "text"),
        [
            pytest.param("pkg.auth", "grep -r 'import pkg.auth' src/", id="dotted-in-grep"),
            pytest.param("pkg.auth", "src/pkg/auth.py", id="slashed-path"),
            pytest.param("pkg.auth", "pkg.auth", id="exact"),
            pytest.param("a.b.c", "from a.b.c import x", id="three-segment"),
        ],
    )
    def test_matches_real_reference(self, module: str, text: str) -> None:
        """A word-boundary reference in either dotted or slashed form matches."""
        assert ja.module_matches(module, text) is True

    @pytest.mark.parametrize(
        ("module", "text"),
        [
            pytest.param("pkg.auth", "pkg.auth2", id="trailing-digit"),
            pytest.param("pkg.auth", "notpkg.auth", id="leading-ident"),
            pytest.param("pkg.auth", "pkg.authx", id="trailing-ident"),
            pytest.param("pkg.auth", "pkg.other", id="different-module"),
            pytest.param("", "anything", id="empty-module"),
            pytest.param("pkg.auth", "", id="empty-text"),
        ],
    )
    def test_rejects_near_miss(self, module: str, text: str) -> None:
        """A substring-only or unrelated hit must not match (would be a false avoidance)."""
        assert ja.module_matches(module, text) is False


class TestQueryComplete:
    """Completeness detection must honour both the forward and legacy field names."""

    @pytest.mark.parametrize(
        ("record", "expected"),
        [
            pytest.param({"result": {"index": {"query_complete": True}}}, True, id="forward-true"),
            pytest.param({"result": {"index": {"exhaustive": True}}}, True, id="legacy-true"),
            pytest.param({"result": {"query_complete": True}}, True, id="top-level-true"),
            pytest.param({"result": {"index": {"query_complete": False}}}, False, id="forward-false"),
            pytest.param({"result": {}}, False, id="no-index"),
            pytest.param({}, False, id="no-result"),
        ],
    )
    def test_query_complete(self, record: dict, expected: bool) -> None:
        """query_complete reads index.query_complete, index.exhaustive, then top level."""
        assert ja._query_complete(record) is expected


class TestFindAvoidanceEvents:
    """The core accept criteria: a complete answer + a later matching grep = one event."""

    def test_grep_after_complete_answer_flags_one_event(self) -> None:
        """A complete answer on X then a grep on X two minutes later flags exactly one event."""
        answers = ja.parse_cli_records([_cli("pkg.auth", 0.0)])
        events = ja.parse_tool_records([_tool("import pkg.auth", 2.0)])

        flagged = ja.find_avoidance_events(answers, events, window_min=10)

        assert len(flagged) == 1
        assert flagged[0].module == "pkg.auth"
        assert flagged[0].tool == "Grep"
        assert flagged[0].gap_seconds == pytest.approx(120.0)

    def test_grep_before_answer_flags_nothing(self) -> None:
        """A grep BEFORE the complete answer is legitimate discovery — never an avoidance."""
        answers = ja.parse_cli_records([_cli("pkg.auth", 5.0)])
        events = ja.parse_tool_records([_tool("import pkg.auth", 2.0)])

        assert ja.find_avoidance_events(answers, events, window_min=10) == []

    def test_grep_on_unrelated_module_flags_nothing(self) -> None:
        """A grep on a module the answer did not cover is not an avoidance."""
        answers = ja.parse_cli_records([_cli("pkg.auth", 0.0)])
        events = ja.parse_tool_records([_tool("import pkg.billing", 2.0)])

        assert ja.find_avoidance_events(answers, events, window_min=10) == []

    def test_grep_after_incomplete_answer_flags_nothing(self) -> None:
        """An incomplete answer cannot be avoided — re-grepping it is expected, not a leak."""
        answers = ja.parse_cli_records([_cli("pkg.auth", 0.0, complete=False)])
        events = ja.parse_tool_records([_tool("import pkg.auth", 2.0)])

        assert answers == []
        assert ja.find_avoidance_events(answers, events, window_min=10) == []

    def test_grep_outside_window_flags_nothing(self) -> None:
        """A grep past the window is too late to attribute to the earlier answer."""
        answers = ja.parse_cli_records([_cli("pkg.auth", 0.0)])
        events = ja.parse_tool_records([_tool("import pkg.auth", 12.0)])

        assert ja.find_avoidance_events(answers, events, window_min=10) == []

    def test_answer_in_other_session_flags_nothing(self) -> None:
        """The join key is the session — a grep in another session never matches."""
        answers = ja.parse_cli_records([_cli("pkg.auth", 0.0, session="s1")])
        events = ja.parse_tool_records([_tool("import pkg.auth", 2.0, session="s2")])

        assert ja.find_avoidance_events(answers, events, window_min=10) == []


class TestSummarize:
    """Totals and per-session/per-skill aggregation for the debrief report."""

    def test_totals_and_rate(self) -> None:
        """Rate is flagged events over all tool events; totals count the raw inputs."""
        answers = ja.parse_cli_records([_cli("pkg.auth", 0.0)])
        tool_records = [_tool("import pkg.auth", 2.0), _tool("import pkg.billing", 3.0)]
        events = ja.parse_tool_records(tool_records)

        summary = ja.summarize(answers, events, window_min=10)

        assert summary.total_tool_events == len(tool_records)
        assert summary.total_complete_answers == 1
        assert len(summary.avoidance_events) == 1
        assert summary.rate == pytest.approx(0.5)
        assert summary.per_session == {"s1": 1}

    def test_per_skill_attribution(self) -> None:
        """A session mapped to a skill attributes its avoidance events to that skill."""
        answers = ja.parse_cli_records([_cli("pkg.auth", 0.0, session="s1")])
        events = ja.parse_tool_records([_tool("import pkg.auth", 2.0, session="s1")])

        summary = ja.summarize(answers, events, window_min=10, session_skill={"s1": "codemap:query-code"})

        assert summary.per_skill == {"codemap:query-code": 1}

    def test_no_tool_events_yields_zero_rate(self) -> None:
        """An empty tool set scores zero rate without dividing by zero."""
        summary = ja.summarize(ja.parse_cli_records([_cli("pkg.auth", 0.0)]), [], window_min=10)

        assert summary.rate == 0.0
        assert summary.avoidance_events == []


class TestMainCli:
    """End-to-end CLI: shard resolution, window flag, JSON output, and the exit contract."""

    @staticmethod
    def _write_shards(log_dir: Path, cli_records: list[dict], tool_records: list[dict]) -> None:
        """Write cli/tools shards under *log_dir* for the ``--logs`` resolution path."""
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "cli_s1.jsonl").write_text("".join(json.dumps(r) + "\n" for r in cli_records))
        (log_dir / "tools_s1.jsonl").write_text("".join(json.dumps(r) + "\n" for r in tool_records))

    def test_logs_dir_flags_synthetic_session(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """The synthetic session fixture (answer on X, grep on X 2 min later) flags one event."""
        log_dir = tmp_path / "logs"
        self._write_shards(log_dir, [_cli("pkg.auth", 0.0)], [_tool("import pkg.auth", 2.0)])

        code = ja.main(["--logs", str(log_dir), "--json"])
        payload = json.loads(capsys.readouterr().out)

        assert code == 0
        assert payload["avoidance_count"] == 1
        assert payload["events"][0]["module"] == "pkg.auth"

    def test_grep_before_query_flags_zero(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """The negative fixture (grep before the query) reports zero via the CLI too."""
        log_dir = tmp_path / "logs"
        self._write_shards(log_dir, [_cli("pkg.auth", 5.0)], [_tool("import pkg.auth", 2.0)])

        code = ja.main(["--logs", str(log_dir), "--json"])
        payload = json.loads(capsys.readouterr().out)

        assert code == 0
        assert payload["avoidance_count"] == 0

    def test_logs_dir_recursively_reconciles_flat_and_all_runtime_subtrees(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """Recursive discovery joins one matching pair from legacy and each runtime once."""
        log_dir = tmp_path / "logs"
        for runtime, session in (("", "legacy"), ("claude", "claude"), ("codex", "codex"), ("direct", "direct")):
            shard_dir = log_dir / runtime if runtime else log_dir
            shard_dir.mkdir(parents=True, exist_ok=True)
            (shard_dir / f"cli_{session}.jsonl").write_text(json.dumps(_cli("pkg.auth", 0.0, session=session)) + "\n")
            (shard_dir / f"tools_{session}.jsonl").write_text(
                json.dumps(_tool("import pkg.auth", 2.0, session=session)) + "\n"
            )

        code = ja.main(["--logs", str(log_dir), "--json"])
        payload = json.loads(capsys.readouterr().out)

        assert code == 0
        assert payload["total_complete_answers"] == 4
        assert payload["total_tool_events"] == 4
        assert payload["avoidance_count"] == 4
        assert payload["per_runtime"]["unattributed"]["total_complete_answers"] == 1
        assert payload["per_runtime"]["unattributed"]["avoidance_count"] == 1

    def test_same_session_in_different_runtime_subtrees_does_not_cross_join(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """Runtime scope is part of the join key even when opaque session strings match."""
        log_dir = tmp_path / "logs"
        claude = log_dir / "claude"
        codex = log_dir / "codex"
        claude.mkdir(parents=True)
        codex.mkdir()
        (claude / "cli_same.jsonl").write_text(json.dumps(_cli("pkg.auth", 0.0, session="same")) + "\n")
        (codex / "tools_same.jsonl").write_text(json.dumps(_tool("import pkg.auth", 2.0, session="same")) + "\n")

        assert ja.main(["--logs", str(log_dir), "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["avoidance_count"] == 0
        assert payload["per_runtime"]["claude"]["total_complete_answers"] == 1
        assert payload["per_runtime"]["codex"]["total_tool_events"] == 1

    def test_explicit_files_and_window_flag(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """Verify command-line option behavior.

        ``--cli``/``--tools`` bypass shard resolution and ``--window-min`` tightens the join.
        """
        cli_path = tmp_path / "cli.jsonl"
        tools_path = tmp_path / "tools.jsonl"
        cli_path.write_text(json.dumps(_cli("pkg.auth", 0.0)) + "\n")
        tools_path.write_text(json.dumps(_tool("import pkg.auth", 8.0)) + "\n")

        code = ja.main(["--cli", str(cli_path), "--tools", str(tools_path), "--window-min", "5", "--json"])
        payload = json.loads(capsys.readouterr().out)

        assert code == 0
        assert payload["avoidance_count"] == 0  # 8-min gap exceeds the 5-min window

    def test_missing_inputs_exit_2(self, capsys: pytest.CaptureFixture) -> None:
        """Neither ``--logs`` nor ``--cli``/``--tools`` given is a usage error (exit 2)."""
        assert ja.main([]) == _EXIT_USAGE
        assert "give --logs" in capsys.readouterr().err

    def test_text_output_reports_rate(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """Default text output names the window, totals, and rate for a human reader."""
        log_dir = tmp_path / "logs"
        self._write_shards(log_dir, [_cli("pkg.auth", 0.0)], [_tool("import pkg.auth", 2.0)])

        code = ja.main(["--logs", str(log_dir)])
        out = capsys.readouterr().out

        assert code == 0
        assert "avoidance events: 1" in out
        assert "window 10 min" in out
