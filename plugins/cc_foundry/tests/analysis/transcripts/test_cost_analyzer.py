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

    def test_overlong_model_version_prices_as_unknown(self):
        """A model id with an absurd version run prices at the unknown rate instead of overflowing int conversion.

        The digits sit inside a JSON string, so the decoder's integer digit limit never applies to them.
        """
        model = "claude-opus-" + "9" * 5_000
        assert (ca.tier(model), ca.cost({"input_tokens": 100}, model) > 0) == (ca.UNKNOWN_TIER, True)

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
    """``_project_label()`` home-prefix stripping, with no hardcoded user path."""

    @pytest.mark.parametrize(
        ("dirname", "expected"),
        [
            pytest.param("{home}-Workspace-demo", "Workspace-demo", id="strips-home"),
            pytest.param("-some-other-slug", "-some-other-slug", id="non-matching-prefix-unchanged"),
        ],
    )
    def test_strips_home_prefix_only(self, dirname: str, expected: str) -> None:
        """The real ``Path.home()`` slug is stripped to a short portable label; any other directory name passes through.

        The home slug is computed when the test runs, from the home the function itself reads, so the test carries no
        user-specific path and stays correct when a fixture changes ``HOME`` after collection.
        """
        home_slug = str(Path.home()).replace("/", "-").replace("\\", "-")
        assert ca._project_label(dirname.format(home=home_slug)) == expected


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


@pytest.fixture
def session_tree(tmp_path: Path) -> Path:
    """Return a main transcript that has one subagent transcript with sidecar metadata."""
    project = tmp_path / "projects" / "-slug-a"
    main = _write_jsonl(project / "sid1.jsonl", [_usage_row("main1", output_tokens=100)])
    _write_jsonl(
        project / "sid1" / "subagents" / "agent-x1.jsonl", [_usage_row("sub1", sidechain=True, output_tokens=50)]
    )
    meta = project / "sid1" / "subagents" / "agent-x1.meta.json"
    meta.write_text(json.dumps({"agentType": "foundry:sw-engineer", "description": "fix bug"}), encoding="utf-8")
    return main


class TestLoadSession:
    """``load_session()`` merging subagent transcripts into one Session."""

    def test_merges_main_and_subagent_calls(self, session_tree: Path):
        """Session.calls includes both the main-loop call and the subagent's call."""
        session = ca.load_session(session_tree)
        assert len(session.calls) == 2
        assert {c.output for c in session.calls} == {100, 50}

    def test_agent_roster_carries_meta_type_and_description(self, session_tree: Path):
        """The agent roster reads agentType/description from the sibling meta.json."""
        session = ca.load_session(session_tree)
        assert len(session.agent_spends) == 1
        spend = session.agent_spends[0]
        assert spend.agent_type == "foundry:sw-engineer"
        assert spend.description == "fix bug"
        assert spend.calls == 1

    @pytest.mark.parametrize(
        "meta",
        [pytest.param([], id="list-meta"), pytest.param({"agentType": ["x"], "description": 3}, id="wrong-types")],
    )
    def test_malformed_meta_keeps_placeholders(self, session_tree: Path, meta: object):
        """Malformed sidecar metadata keeps the roster placeholders instead of aborting the session load."""
        (session_tree.parent / "sid1" / "subagents" / "agent-x1.meta.json").write_text(
            json.dumps(meta), encoding="utf-8"
        )
        spend = ca.load_session(session_tree).agent_spends[0]
        assert (spend.agent_type, spend.description) == ("?", "")

    def test_non_string_tool_name_row_is_skipped(self, tmp_path: Path):
        """A tool_use block whose name is not a string is rejected whole, keeping later valid usage."""
        bad = _usage_row("bad", input_tokens=1)
        bad["message"]["content"] = [{"type": "tool_use", "id": "t", "name": {}}]
        path = _write_jsonl(tmp_path / "s.jsonl", [bad, _usage_row("good", input_tokens=1)])
        calls, _, _, _ = ca._parse_rows(path)
        assert list(calls) == ["good"]

    def test_session_with_no_subagents_dir(self, tmp_path: Path):
        """A session with no subagents/ directory loads cleanly with an empty roster."""
        main = _write_jsonl(tmp_path / "projects" / "-slug-a" / "sid2.jsonl", [_usage_row("m1", output_tokens=1)])
        session = ca.load_session(main)
        assert len(session.calls) == 1
        assert session.agent_spends == []


def _tool_row(message_id: str, tool_ids: list[str], *, model: str = "claude-opus-5", **usage) -> list[dict]:
    """Build one message split into one row per tool_use block, as Claude Code writes it.

    Examples:
        >>> [r["message"]["content"][0]["id"] for r in _tool_row("m", ["a", "b"], output_tokens=1)]
        ['a', 'b']
    """
    return [
        {
            "timestamp": "2030-01-01T00:00:00Z",
            "isSidechain": True,
            "message": {
                "id": message_id,
                "model": model,
                "usage": usage,
                "content": [{"type": "tool_use", "id": tool_id, "name": "Bash", "input": {}}],
            },
        }
        for tool_id in tool_ids
    ]


def _agent_tree(tmp_path: Path, agents: dict[str, dict]) -> Path:
    """Write a main transcript plus one subagent transcript and sidecar per entry of *agents*.

    Each value carries ``meta`` (sidecar dict) and ``tools`` (tool_use ids the agent's single message holds).
    """
    project = tmp_path / "projects" / "-slug-a"
    main = _write_jsonl(project / "sid1.jsonl", [_usage_row("main1", output_tokens=100)])
    for key, spec in agents.items():
        _write_jsonl(
            project / "sid1" / "subagents" / f"agent-{key}.jsonl",
            _tool_row(f"msg-{key}", spec["tools"], output_tokens=1_000_000),
        )
        (project / "sid1" / "subagents" / f"agent-{key}.meta.json").write_text(json.dumps(spec["meta"]), "utf-8")
    return main


class TestPerAgentSpend:
    """Per-agent rows: tool-call count, dominant model, agent type, flags and phase split."""

    def test_tool_calls_count_every_block_of_a_split_message(self, tmp_path: Path):
        """A message written as one row per tool_use block counts all its blocks, not only the first row's."""
        main = _agent_tree(tmp_path, {"a": {"meta": {"agentType": "t", "name": "impl-x"}, "tools": ["t1", "t2", "t3"]}})
        spend = ca.load_session(main).agent_spends[0]
        assert (spend.calls, spend.tool_calls) == (1, 3)

    def test_repeated_tool_use_id_counted_once(self, tmp_path: Path):
        """The same tool_use id repeated across rows is one call."""
        main = _agent_tree(tmp_path, {"a": {"meta": {"agentType": "t"}, "tools": ["t1", "t1"]}})
        assert ca.load_session(main).agent_spends[0].tool_calls == 1

    def test_roster_carries_name_and_dominant_model(self, tmp_path: Path):
        """The sidecar name is kept beside the model that carried most of the agent's cost."""
        meta = {"agentType": "foundry:sw-engineer", "name": "impl-alpha", "description": "alpha fixes"}
        main = _agent_tree(tmp_path, {"a": {"meta": meta, "tools": ["t1"]}})
        spend = ca.load_session(main).agent_spends[0]
        assert (spend.name, spend.model) == ("impl-alpha", "claude-opus-5")

    @pytest.mark.parametrize(
        ("meta", "expected"),
        [
            pytest.param({"agentType": "foundry:sw-engineer"}, "foundry:sw-engineer", id="plain-spawn"),
            pytest.param(
                {"agentType": "review-lead", "spawnedAgentType": "foundry:challenger"},
                "foundry:challenger",
                id="named-teammate-reports-spawned-type",
            ),
            pytest.param({"agentType": "", "name": "x"}, "?", id="missing-type-placeholder"),
        ],
    )
    def test_agent_type_is_the_spawned_type(self, tmp_path: Path, meta: dict[str, str], expected: str):
        """The type column names the agent type, not the teammate name a named spawn records as ``agentType``."""
        main = _agent_tree(tmp_path, {"a": {"meta": meta, "tools": ["t1"]}})
        assert ca.load_session(main).agent_spends[0].agent_type == expected

    @pytest.mark.parametrize(
        ("name", "description", "tool_calls", "expected"),
        [
            pytest.param("impl-alpha", "alpha fixes", 10, [], id="plain"),
            pytest.param("impl-alpha", "alpha fixes", 61, ["long"], id="over-the-call-bound"),
            pytest.param("impl-alpha", "alpha fixes", 60, [], id="at-the-call-bound"),
            pytest.param("impl-beta", "fix dead link 404", 5, [], id="description-never-flags-link"),
            pytest.param("impl-parser-link4", "parser", 5, ["link"], id="chain-link-in-name"),
            pytest.param("challenge-alpha", "five items", 5, ["challenge"], id="challenge-chunk"),
            pytest.param("challenge-alpha-retry-x", "single item", 5, ["challenge-retry"], id="challenge-retry"),
            pytest.param("impl-beta-link3", "beta retry 9", 5, ["link"], id="impl-retry-is-not-challenge"),
            pytest.param("challenge-retry-logic", "five items", 5, ["challenge"], id="domain-slug-starting-with-retry"),
            pytest.param("challenge-net-retry-2", "single item", 5, ["challenge-retry"], id="retry-after-a-domain"),
            pytest.param("plan-review", "challenge the plan", 5, [], id="description-never-flags-challenge"),
            pytest.param("challenger-pass", "scan", 5, [], id="challenge-needs-the-dash"),
            pytest.param("unlinked", "hyperlink 2 words", 5, [], id="link-needs-terminal-suffix"),
            pytest.param(
                "challenge-link-1", "link docs chunk", 5, ["challenge"], id="domain-named-link-is-not-a-chain"
            ),
            pytest.param("impl-link-2", "link docs", 5, [], id="link-domain-group-is-not-a-chain"),
        ],
    )
    def test_agent_flags(self, name: str, description: str, tool_calls: int, expected: list[str]):
        """Flags mark long agents, chain links and challenge-phase spawns, with retries kept separate."""
        spend = ca.AgentSpend("t", description, 1, 1.0, name=name, tool_calls=tool_calls)
        assert ca.agent_flags(spend) == expected

    def test_session_detail_lists_agents_and_phase_split(self, tmp_path: Path):
        """The deep-dive prints one row per agent and a phase table that keeps challenge retries separate."""
        agents = {
            "a": {"meta": {"agentType": "foundry:sw-engineer", "name": "impl-alpha"}, "tools": ["t1"]},
            "b": {"meta": {"agentType": "foundry:sw-engineer", "name": "challenge-alpha"}, "tools": ["t2"]},
            "c": {"meta": {"agentType": "foundry:sw-engineer", "name": "challenge-alpha-retry-1"}, "tools": ["t3"]},
        }
        lines = ca.render_session_detail(ca.load_session(_agent_tree(tmp_path, agents)), top_n=3)
        text = "\n".join(lines)
        phase_rows = {
            line.split("|")[1].strip(): line for line in lines if line.startswith("| ") and "phase" not in line
        }
        assert "### Per-agent spend" in text
        assert "impl-alpha" in text
        assert "challenge-alpha-retry-1" in text
        assert "### Agent phases" in text
        assert "challenge-* retries" in phase_rows
        assert phase_rows["challenge-* retries"].split("|")[2].strip() == "1"

    def test_agent_row_cells_escape_pipes_and_line_breaks(self):
        """Sidecar free text with a pipe or newline stays inside its own table cell."""
        spend = ca.AgentSpend("foundry:x|y", "fix dead link 404 | pipes\nnext", 1, 1.0, name="", tool_calls=1)
        row = next(line for line in ca.render_agent_detail([spend]) if "pipes" in line)
        assert len(row.replace("\\|", "").split("|")) == 11
        assert "fix dead link 404 \\| pipes next" in row
        assert "`foundry:x\\|y`" in row

    def test_roster_carries_the_sidecar_effort(self, tmp_path: Path):
        """The effort a spawn recorded in its sidecar reaches the roster; a missing or non-string value stays empty."""
        agents = {
            "a": {"meta": {"agentType": "t", "name": "with", "effort": "high"}, "tools": ["t1"]},
            "b": {"meta": {"agentType": "t", "name": "without"}, "tools": ["t2"]},
            "c": {"meta": {"agentType": "t", "name": "odd", "effort": 3}, "tools": ["t3"]},
        }
        efforts = {s.name: s.effort for s in ca.load_session(_agent_tree(tmp_path, agents)).agent_spends}
        assert efforts == {"with": "high", "without": "", "odd": ""}

    def test_agent_table_shows_effort_and_a_dash_when_absent(self):
        """The effort column prints the recorded level, or a dash when the sidecar had none."""
        spends = [
            ca.AgentSpend("t", "d", 1, 2.0, name="set", effort="high"),
            ca.AgentSpend("t", "d", 1, 1.0, name="unset"),
        ]
        rows = [line.split("|") for line in ca.render_agent_detail(spends) if line.startswith(("| set", "| unset"))]
        assert {cells[1].strip(): cells[4].strip() for cells in rows} == {"set": "high", "unset": "-"}

    def test_agent_rows_beyond_the_limit_collapse_but_phases_count_all(self):
        """Rows past the limit fold into one overflow row with their count and cost; the phase table still sees them."""
        spends = [
            ca.AgentSpend("t", "d", 1, cost_usd, name=f"challenge-{key}", tool_calls=1)
            for key, cost_usd in (("a", 5.0), ("b", 3.0), ("c", 2.0))
        ]
        lines = ca.render_agent_detail(spends, limit=1)
        text = "\n".join(lines)
        header = next(line for line in lines if line.startswith("| agent"))
        assert next(line for line in lines if "more agents" in line).count("|") == header.count("|")
        assert "challenge-a" in text
        assert "challenge-b" not in text
        assert "… 2 more agents, $5.00" in text
        phase = next(line for line in text.splitlines() if line.startswith("| challenge-* spawns"))
        assert phase.split("|")[2].strip() == "3"

    def test_session_without_agents_prints_no_agent_sections(self, tmp_path: Path):
        """A session with no subagents prints neither per-agent table."""
        main = _write_jsonl(tmp_path / "projects" / "-slug-a" / "sid2.jsonl", [_usage_row("m1", output_tokens=1)])
        text = "\n".join(ca.render_session_detail(ca.load_session(main), top_n=3))
        assert "Per-agent spend" not in text
        assert "Agent phases" not in text


@pytest.fixture
def projects_root(tmp_path: Path) -> Path:
    """Return the isolated projects-root path for one CLI test."""
    return tmp_path / "projects"


class TestMainCli:
    """The CLI entry point: session-id drill-down and window ranking."""

    def test_session_mode_writes_detail_report(self, tmp_path: Path, projects_root: Path, capsys):
        """Render the single-session deep-dive section."""
        _write_jsonl(projects_root / "-slug-a" / "sid1.jsonl", [_usage_row("m1", output_tokens=100)])
        out = tmp_path / "cost.md"
        rc = ca.main(["--projects-root", str(projects_root), "--session-id", "sid1", "--output", str(out)])
        assert rc == 0
        assert str(out) in capsys.readouterr().out
        body = out.read_text(encoding="utf-8")
        assert "## Tokens & cost" in body
        assert "Session `sid1`" in body

    def test_session_mode_unknown_id_returns_1(self, tmp_path: Path, projects_root: Path, capsys):
        """An unmatched ``--session-id`` exits 1 with a stderr message, no report written."""
        _write_jsonl(projects_root / "-slug-a" / "sid1.jsonl", [_usage_row("m1", output_tokens=100)])
        out = tmp_path / "cost.md"
        rc = ca.main(["--projects-root", str(projects_root), "--session-id", "does-not-exist", "--output", str(out)])
        assert rc == 1
        assert "no session matching" in capsys.readouterr().err
        assert not out.exists()

    def test_window_mode_ranks_sessions_by_cost(self, tmp_path: Path, projects_root: Path, capsys):
        """Default (no ``--session-id``) mode ranks every session in the window by cost."""
        _write_jsonl(projects_root / "-slug-a" / "sid1.jsonl", [_usage_row("m1", output_tokens=100)])
        _write_jsonl(projects_root / "-slug-b" / "sid2.jsonl", [_usage_row("m2", output_tokens=1)])
        out = tmp_path / "cost.md"
        rc = ca.main(["--projects-root", str(projects_root), "--since", "30d", "--output", str(out)])
        assert rc == 0
        assert str(out) in capsys.readouterr().out
        body = out.read_text(encoding="utf-8")
        assert "Sessions ranked by cost" in body
        assert "sid1" in body
        assert "sid2" in body

    def test_window_mode_empty_returns_1(self, tmp_path: Path, projects_root: Path, capsys):
        """No sessions with usage in the projects root exits 1 with a stderr message."""
        projects_root.mkdir(parents=True)
        out = tmp_path / "cost.md"
        rc = ca.main(["--projects-root", str(projects_root), "--since", "1h", "--output", str(out)])
        assert rc == 1
        assert "no sessions" in capsys.readouterr().err

    def test_unpriced_model_warns_once_and_stays_in_report(self, tmp_path: Path, projects_root: Path, capsys):
        """A future model id prints one stderr warning and still lands in its family's row with a nonzero cost.

        Two calls of the same unpriced id must not repeat the warning; the report must not lose the spend.
        """
        rows = [_usage_row(m, model="claude-opus-6", output_tokens=1_000) for m in ("m1", "m2")]
        _write_jsonl(projects_root / "-slug-a" / "sid1.jsonl", rows)
        out = tmp_path / "cost.md"
        rc = ca.main(["--projects-root", str(projects_root), "--session-id", "sid1", "--output", str(out)])
        err = capsys.readouterr().err
        assert rc == 0
        assert err.count("warning: no list price for model 'claude-opus-6'") == 1
        assert "newest known opus (opus-5.5)" in err
        assert "| main | opus | 0 | 2,000 |" in out.read_text(encoding="utf-8")
