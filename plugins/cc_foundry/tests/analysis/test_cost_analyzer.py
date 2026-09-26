"""Tests for ``bin/cost_analyzer.py`` — dedupe, bucketing, session load, CLI."""

from __future__ import annotations

import json
from pathlib import Path

import cost_analyzer as ca


def _write_jsonl(path: Path, rows: list[dict]) -> Path:
    """Write rows as JSONL to *path* and return it.

    Examples:
        >>> from tempfile import TemporaryDirectory
        >>> with TemporaryDirectory() as directory:
        ...     _write_jsonl(Path(directory) / "rows.jsonl", [{"n": 1}]).read_text()
        '{"n": 1}\\n'
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


def _usage_row(message_id: str, *, ts: str = "2030-01-01T00:00:00Z", sidechain: bool = False, **usage) -> dict:
    """Build one transcript row carrying a usage object.

    Examples:
        >>> _usage_row("m1", output_tokens=7)["message"]["usage"]
        {'output_tokens': 7}
    """
    return {
        "timestamp": ts,
        "isSidechain": sidechain,
        "message": {"id": message_id, "model": "claude-opus-5", "usage": usage, "content": []},
    }


class TestDedupeByMessageId:
    """Guards the 3x-inflation bug: one message repeats its usage across content-block rows."""

    def test_three_rows_same_message_id_counted_once(self, tmp_path: Path):
        """Same message_id across 3 rows (one per content block) must not triple-count."""
        path = _write_jsonl(
            tmp_path / "s1.jsonl",
            [
                _usage_row("m1", output_tokens=10),
                _usage_row("m1", output_tokens=10),
                _usage_row("m1", output_tokens=10),
            ],
        )
        calls, _, _, _ = ca._parse_rows(path)
        assert len(calls) == 1
        assert calls["m1"].output == 10

    def test_distinct_message_ids_both_counted(self, tmp_path: Path):
        """Two distinct message ids each keep their own usage."""
        path = _write_jsonl(
            tmp_path / "s1.jsonl",
            [_usage_row("m1", output_tokens=10), _usage_row("m2", output_tokens=5)],
        )
        calls, _, _, _ = ca._parse_rows(path)
        assert {c.output for c in calls.values()} == {10, 5}


class TestTierAndCost:
    """Covers tier fallback and USD pricing beyond the module doctests."""

    def test_unrecognised_model_prices_as_opus(self):
        """Unknown model ids price at the most expensive tier — overstate, don't hide."""
        assert ca.tier("some-future-model") == "opus"

    def test_cache_write_twelve_point_five_x_cache_read(self):
        """Cache writes price 12.5x cache reads — why a cold start dominates cost."""
        w = ca.cost({"cache_creation_input_tokens": 1})
        r = ca.cost({"cache_read_input_tokens": 1})
        assert round(w / r, 3) == 12.5


class TestBucket:
    """Covers bucket() splitting main/sidechain x tier."""

    def test_main_and_sidechain_separated(self):
        """A main-loop opus call and a sidechain haiku call land in distinct buckets."""
        calls = [
            ca.Call("m1", "claude-opus-5", {"output_tokens": 100}),
            ca.Call("m2", "claude-haiku-4-5", {"output_tokens": 7}, sidechain=True),
        ]
        got = ca.bucket(calls)
        assert got[("main", "opus")]["out"] == 100
        assert got[("sidechain", "haiku")]["out"] == 7

    def test_empty_calls_yields_empty_buckets(self):
        """No calls means no buckets — not a KeyError."""
        assert ca.bucket([]) == {}


class TestColdStartShare:
    """Covers cold_start_share() cache-rebuild attribution."""

    def test_no_cache_writes_returns_zero(self):
        """Division-by-zero guard: zero cache writes reports 0.0, not an error."""
        assert ca.cold_start_share([]) == 0.0

    def test_mixed_cold_and_warm_calls(self):
        """A cold start (no cache read) and a warm rebuild split proportionally."""
        cold = ca.Call("m1", "opus", {"cache_creation_input_tokens": 180_000})
        warm = ca.Call("m2", "opus", {"cache_creation_input_tokens": 20_000, "cache_read_input_tokens": 150_000})
        assert round(ca.cold_start_share([cold, warm]), 2) == 0.9


class TestAggregateCommands:
    """Covers aggregate_commands() upper-bound attribution."""

    def test_session_cost_attributed_to_every_command_it_ran(self, tmp_path: Path):
        """A session running two commands attributes its full cost to each — ranks, not sums."""
        s1 = ca.Session(tmp_path / "s1.jsonl", [], {"/oss:review": 1, "/oss:resolve": 2}, [])
        s2 = ca.Session(tmp_path / "s2.jsonl", [], {"/oss:resolve": 1}, [])
        got = ca.aggregate_commands([s1, s2])
        assert sorted(got) == ["/oss:resolve", "/oss:review"]
        assert got["/oss:resolve"]["runs"] == 3

    def test_builtin_commands_without_colon_are_dropped(self, tmp_path: Path):
        """Only `/plugin:skill` style entries are ranked — `/clear` etc. drop out."""
        s1 = ca.Session(tmp_path / "s1.jsonl", [], {"/clear": 5, "/oss:review": 1}, [])
        got = ca.aggregate_commands([s1])
        assert list(got) == ["/oss:review"]


class TestProjectLabel:
    """Covers _project_label() home-prefix stripping (no hardcoded user path)."""

    def test_strips_current_home_prefix(self):
        """Real Path.home() prefix is stripped, leaving a short portable label."""
        home_slug = str(Path.home()).replace("/", "-").replace("\\", "-")
        dirname = f"{home_slug}-Workspace-demo"
        assert ca._project_label(dirname) == "Workspace-demo"

    def test_non_matching_prefix_returned_unchanged(self):
        """A dir name that doesn't start with the home slug passes through untouched."""
        assert ca._project_label("-some-other-slug") == "-some-other-slug"


class TestDiscoverSessions:
    """Covers discover_sessions() depth-1 scoping."""

    def test_subagent_files_not_double_counted_as_sessions(self, tmp_path: Path):
        """A `<sid>/subagents/agent-*.jsonl` file must not appear as its own session."""
        root = tmp_path / "projects"
        project = root / "-slug-a"
        main = _write_jsonl(project / "sid1.jsonl", [_usage_row("m1", output_tokens=1)])
        _write_jsonl(project / "sid1" / "subagents" / "agent-x.jsonl", [_usage_row("m2", output_tokens=1)])
        found = ca.discover_sessions(root)
        assert found == [main]

    def test_missing_root_returns_empty(self, tmp_path: Path):
        """A nonexistent root yields an empty list, not an error."""
        assert ca.discover_sessions(tmp_path / "nope") == []


class TestLoadSession:
    """Covers load_session() merging subagent transcripts into one Session."""

    def _build_session_tree(self, tmp_path: Path) -> Path:
        """Create a main transcript with one subagent transcript and metadata."""
        project = tmp_path / "projects" / "-slug-a"
        main = _write_jsonl(project / "sid1.jsonl", [_usage_row("main1", output_tokens=100)])
        _write_jsonl(
            project / "sid1" / "subagents" / "agent-x1.jsonl",
            [_usage_row("sub1", sidechain=True, output_tokens=50)],
        )
        meta = project / "sid1" / "subagents" / "agent-x1.meta.json"
        meta.write_text(json.dumps({"agentType": "foundry:sw-engineer", "description": "fix bug"}), encoding="utf-8")
        return main

    def test_merges_main_and_subagent_calls(self, tmp_path: Path):
        """Session.calls includes both the main-loop call and the subagent's call."""
        main = self._build_session_tree(tmp_path)
        session = ca.load_session(main)
        assert len(session.calls) == 2
        assert {c.output for c in session.calls} == {100, 50}

    def test_agent_roster_carries_meta_type_and_description(self, tmp_path: Path):
        """The agent roster reads agentType/description from the sibling meta.json."""
        main = self._build_session_tree(tmp_path)
        session = ca.load_session(main)
        assert len(session.agent_spends) == 1
        spend = session.agent_spends[0]
        assert spend.agent_type == "foundry:sw-engineer"
        assert spend.description == "fix bug"
        assert spend.calls == 1

    def test_session_with_no_subagents_dir(self, tmp_path: Path):
        """A session with no subagents/ directory loads cleanly with an empty roster."""
        main = _write_jsonl(tmp_path / "projects" / "-slug-a" / "sid2.jsonl", [_usage_row("m1", output_tokens=1)])
        session = ca.load_session(main)
        assert len(session.calls) == 1
        assert session.agent_spends == []


class TestMainCli:
    """Covers the CLI entry point: session-id drill-down and window ranking."""

    def _projects_root(self, tmp_path: Path) -> Path:
        """Return the isolated projects-root path for one CLI test."""
        return tmp_path / "projects"

    def test_session_mode_writes_detail_report(self, tmp_path: Path, capsys):
        """Render the single-session deep-dive section."""
        root = self._projects_root(tmp_path)
        _write_jsonl(root / "-slug-a" / "sid1.jsonl", [_usage_row("m1", output_tokens=100)])
        out = tmp_path / "cost.md"
        rc = ca.main(["--projects-root", str(root), "--session-id", "sid1", "--output", str(out)])
        assert rc == 0
        assert str(out) in capsys.readouterr().out
        body = out.read_text(encoding="utf-8")
        assert "## Tokens & cost" in body
        assert "Session `sid1`" in body

    def test_session_mode_unknown_id_returns_1(self, tmp_path: Path, capsys):
        """An unmatched ``--session-id`` exits 1 with a stderr message, no report written."""
        root = self._projects_root(tmp_path)
        _write_jsonl(root / "-slug-a" / "sid1.jsonl", [_usage_row("m1", output_tokens=100)])
        out = tmp_path / "cost.md"
        rc = ca.main(["--projects-root", str(root), "--session-id", "does-not-exist", "--output", str(out)])
        assert rc == 1
        assert "no session matching" in capsys.readouterr().err
        assert not out.exists()

    def test_window_mode_ranks_sessions_by_cost(self, tmp_path: Path, capsys):
        """Default (no ``--session-id``) mode ranks every session in the window by cost."""
        root = self._projects_root(tmp_path)
        _write_jsonl(root / "-slug-a" / "sid1.jsonl", [_usage_row("m1", output_tokens=100)])
        _write_jsonl(root / "-slug-b" / "sid2.jsonl", [_usage_row("m2", output_tokens=1)])
        out = tmp_path / "cost.md"
        rc = ca.main(["--projects-root", str(root), "--since", "30d", "--output", str(out)])
        assert rc == 0
        assert str(out) in capsys.readouterr().out
        body = out.read_text(encoding="utf-8")
        assert "Sessions ranked by cost" in body
        assert "sid1" in body and "sid2" in body

    def test_window_mode_empty_returns_1(self, tmp_path: Path, capsys):
        """No sessions with usage in the projects root exits 1 with a stderr message."""
        root = self._projects_root(tmp_path)
        root.mkdir(parents=True)
        out = tmp_path / "cost.md"
        rc = ca.main(["--projects-root", str(root), "--since", "1h", "--output", str(out)])
        assert rc == 1
        assert "no sessions" in capsys.readouterr().err
