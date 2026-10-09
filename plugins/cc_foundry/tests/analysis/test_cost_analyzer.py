"""Tests for ``bin/cost_analyzer.py`` — dedupe, bucketing, session load, CLI."""

from __future__ import annotations

import json
from pathlib import Path

import cost_analyzer as ca
import pytest


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


def _usage_row(
    message_id: str,
    *,
    ts: str = "2030-01-01T00:00:00Z",
    sidechain: bool = False,
    model: str = "claude-opus-5",
    **usage,
) -> dict:
    """Build one transcript row carrying a usage object.

    Examples:
        >>> _usage_row("m1", output_tokens=7)["message"]["usage"]
        {'output_tokens': 7}
    """
    return {
        "timestamp": ts,
        "isSidechain": sidechain,
        "message": {"id": message_id, "model": model, "usage": usage, "content": []},
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

    def test_unrecognised_model_is_unknown_tier(self):
        """A model id naming no known family reports as its own ``unknown`` tier, never folded into a known one."""
        assert ca.tier("some-future-model") == ca.UNKNOWN_TIER

    @pytest.mark.parametrize(
        ("future_id", "newest_known_id"),
        [
            pytest.param("claude-opus-6", "claude-opus-5-5", id="opus"),
            pytest.param("claude-sonnet-6-1", "claude-sonnet-5-5", id="sonnet"),
            pytest.param("claude-haiku-6", "claude-haiku-5-5", id="haiku"),
            pytest.param("claude-fable-6", "claude-fable-5-1", id="fable"),
        ],
    )
    def test_future_model_prices_as_newest_of_its_family(self, future_id: str, newest_known_id: str):
        """A release newer than the table prices exactly like its family's newest known model — never zero.

        Pricing it at the global maximum would inflate every row of a newly released model several-fold; dropping or
        zeroing it would hide real spend. The family's latest price is the closest known estimate.
        """
        usage = {"input_tokens": 1_000, "output_tokens": 1_000, "cache_read_input_tokens": 200_000}
        assert ca.tier(future_id) == ca.tier(newest_known_id)
        assert ca.cost(usage, future_id) == ca.cost(usage, newest_known_id) > 0

    @pytest.mark.parametrize(
        "retired_id",
        [
            pytest.param("claude-3-5-haiku-20241022", id="legacy-order-haiku"),
            pytest.param("claude-3-opus-20240229", id="legacy-order-opus"),
            pytest.param("claude-3-7-sonnet-20250219", id="legacy-order-sonnet"),
            pytest.param("claude-opus-4-3", id="below-family-newest"),
        ],
    )
    def test_retired_model_without_a_row_prices_at_unknown_rate(self, retired_id: str):
        """A retired id with no row of its own prices at the maximum known rates, never at the family's newest.

        Mapping it to the newest model of its family priced an older, dearer generation at the 5.5 family's much lower
        rates — the under-statement the module's overstate-never-hide principle forbids.
        """
        usage = {"input_tokens": 1_000, "output_tokens": 1_000, "cache_read_input_tokens": 1_000}
        priced = [ca.cost(usage, f"claude-{key.replace('.', '-')}") for key in ca.PRICES]
        assert ca.tier(retired_id) == ca.UNKNOWN_TIER
        assert ca.cost(usage, retired_id) >= max(priced)

    def test_retired_model_warns_maximum_known_rates(self, capsys):
        """A retired id's stderr warning names the maximum known rates, not a newest-of-family price."""
        ca._warn_unpriced([ca.Call("m1", "claude-3-5-haiku-20241022", {"output_tokens": 5})])

        assert "priced as maximum known rates" in capsys.readouterr().err

    def test_unknown_rate_is_most_expensive_on_every_field(self):
        """Unknown ids price at or above every known band on each field — overstate, don't hide.

        Taking any single model as the fallback understates whichever field another model prices higher (Fable 5 has the
        dearest cache read of the current models, the retired Opus 4.1 the dearest input and output).
        """
        rates = [p.base for p in ca.PRICES.values()] + [p.long_prompt[1] for p in ca.PRICES.values() if p.long_prompt]
        assert all(all(u >= r for u, r in zip(ca.UNKNOWN_RATE, rate)) for rate in rates)

    def test_unknown_model_costs_more_than_any_priced_model(self):
        """The same usage costs most under an unknown id, so a missing price entry surfaces as an inflated row."""
        usage = {"input_tokens": 10, "output_tokens": 10, "cache_read_input_tokens": 10}
        priced = [ca.cost(usage, f"claude-{key.replace('.', '-')}") for key in ca.PRICES]
        assert ca.cost(usage, "some-future-model") >= max(priced)

    @pytest.mark.parametrize(
        ("model", "expected"),
        [
            pytest.param("claude-opus-5-5", 25.0, id="opus-5.5-reads-at-5pct"),
            pytest.param("claude-sonnet-5-5", 25.0, id="sonnet-5.5-reads-at-5pct"),
            pytest.param("claude-opus-5", 12.5, id="opus-5-reads-at-10pct"),
            pytest.param("claude-fable-5-1", 50.0, id="fable-5.1-reads-at-2.5pct"),
            pytest.param("claude-haiku-4-5-20251001", 12.5, id="haiku-4.5-reads-at-10pct"),
        ],
    )
    def test_cache_write_to_read_ratio_per_model(self, model: str, expected: float):
        """A 5-minute cache write costs 12.5x-50x a cache read depending on the model's read multiplier.

        The flat 12.5x of the old three-tier table understated a rebuild on the 5.5 models by half, which is the figure
        that justifies never ``/clear``-ing mid-run.
        """
        w = ca.cost({"cache_creation_input_tokens": 1}, model)
        r = ca.cost({"cache_read_input_tokens": 1}, model)
        assert round(w / r, 3) == expected

    def test_one_hour_cache_write_prices_at_twice_input(self):
        """A 1-hour write prices at 2x input, a 5-minute write at 1.25x — the split comes from ``cache_creation``.

        Claude Code's main loop writes the 1-hour cache; pricing those tokens at the 5-minute rate understated every
        main-loop rebuild by 37.5%.
        """
        split = {"ephemeral_5m_input_tokens": 1_000_000, "ephemeral_1h_input_tokens": 1_000_000}
        usage = {"cache_creation_input_tokens": 2_000_000, "cache_creation": split}
        assert ca.cost(usage, "claude-opus-5-5") == 4.0 * 1.25 + 4.0 * 2.0

    @pytest.mark.parametrize(
        ("usage", "expected_input_rate"),
        [
            pytest.param({"input_tokens": 100_000}, 0.10, id="input-at-threshold-low-band"),
            pytest.param({"input_tokens": 100_001}, 0.50, id="input-over-threshold-high-band"),
            pytest.param(
                {"input_tokens": 1, "cache_read_input_tokens": 90_000, "cache_creation_input_tokens": 10_000},
                0.50,
                id="cache-tokens-count-toward-prompt",
            ),
        ],
    )
    def test_haiku_5_5_band_follows_prompt_size(self, usage: dict, expected_input_rate: float):
        """Haiku 5.5 bands on input + cache read + cache write; over 100,000 prompt tokens is the higher band.

        Counting only ``input_tokens`` would put nearly every cached Claude Code request (a few input tokens, the rest
        cache) in the cheaper band — subagent spawns alone carry ~120K tokens of fixed context.
        """
        assert ca.rate_for("claude-haiku-5-5", ca.prompt_tokens(usage)).input == expected_input_rate


class TestBucket:
    """Covers bucket() splitting main/sidechain x tier."""

    def test_main_and_sidechain_separated(self):
        """A main-loop opus call and a sidechain haiku call land in distinct buckets."""
        calls = [
            ca.Call("m1", "claude-opus-5", {"output_tokens": 100}),
            ca.Call("m2", "claude-haiku-4-5", {"output_tokens": 7}, sidechain=True),
        ]
        got = ca.bucket(calls)
        assert got[("main", "opus")].output == 100
        assert got[("sidechain", "haiku")].output == 7

    def test_cost_summed_per_call_across_haiku_5_5_bands(self):
        """A bucket mixing short and long Haiku 5.5 prompts carries the sum of per-call costs.

        Re-pricing the bucket's summed tokens would put both requests in the >100K band and overstate the short one
        five-fold — the reason buckets accumulate cost per call.
        """
        short = ca.Call("m1", "claude-haiku-5-5", {"input_tokens": 60_000})
        long = ca.Call("m2", "claude-haiku-5-5", {"input_tokens": 60_000, "cache_read_input_tokens": 60_000})
        got = ca.bucket([short, long])[("main", "haiku")]
        assert got.cost_usd == ca.cost(short.usage, short.model) + ca.cost(long.usage, long.model)
        assert round(got.cost_usd, 6) == round((60_000 * 0.10 + 60_000 * 0.50 + 60_000 * 0.05) / 1_000_000, 6)

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
        assert "sid1" in body
        assert "sid2" in body

    def test_window_mode_empty_returns_1(self, tmp_path: Path, capsys):
        """No sessions with usage in the projects root exits 1 with a stderr message."""
        root = self._projects_root(tmp_path)
        root.mkdir(parents=True)
        out = tmp_path / "cost.md"
        rc = ca.main(["--projects-root", str(root), "--since", "1h", "--output", str(out)])
        assert rc == 1
        assert "no sessions" in capsys.readouterr().err

    def test_unpriced_model_warns_once_and_stays_in_report(self, tmp_path: Path, capsys):
        """A future model id prints one stderr warning and still lands in its family's row with a nonzero cost.

        Two calls of the same unpriced id must not repeat the warning; the report must not lose the spend.
        """
        root = self._projects_root(tmp_path)
        rows = [_usage_row(m, model="claude-opus-6", output_tokens=1_000) for m in ("m1", "m2")]
        _write_jsonl(root / "-slug-a" / "sid1.jsonl", rows)
        out = tmp_path / "cost.md"
        rc = ca.main(["--projects-root", str(root), "--session-id", "sid1", "--output", str(out)])
        err = capsys.readouterr().err
        assert rc == 0
        assert err.count("warning: no list price for model 'claude-opus-6'") == 1
        assert "newest known opus (opus-5.5)" in err
        assert "| main | opus | 0 | 2,000 |" in out.read_text(encoding="utf-8")
