"""Tests for ``bin/efficiency_measured.py`` — measured cost checks over synthetic transcripts.

Every transcript here is built from small synthetic rows; no real session data is read.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path, PurePosixPath, PureWindowsPath

import cost_analyzer as ca
import efficiency_measured as em
import pytest

OPUS = "claude-opus-5-5"
SONNET = "claude-sonnet-5-5"
HAIKU = "claude-haiku-5-5"


def _row(
    message_id: str,
    *,
    ts: str,
    model: str = OPUS,
    tools: tuple[tuple[str, dict], ...] = (),
    **usage: int,
) -> list[dict]:
    """Build the rows of one assistant message: one per content block, each repeating the usage.

    Examples:
        >>> len(_row("m", ts="2030-01-01T00:00:00Z", tools=(("Bash", {}), ("Read", {}))))
        2
    """
    blocks = [{"type": "tool_use", "name": name, "input": data} for name, data in tools] or [{"type": "text"}]
    return [
        {"timestamp": ts, "message": {"id": message_id, "model": model, "usage": usage, "content": [block]}}
        for block in blocks
    ]


def _command_row(ts: str, name: str) -> list[dict]:
    """Build the user row Claude Code writes when a slash command is typed."""
    return [{"type": "user", "timestamp": ts, "message": {"content": f"<command-name>{name}</command-name>"}}]


def _write(path: Path, messages: list[list[dict]]) -> Path:
    """Write message row groups as one JSONL transcript and return its path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for rows in messages for row in rows) + "\n", encoding="utf-8")
    return path


class TestParseTranscript:
    """Covers merging of content-block rows into one call."""

    def test_tool_names_survive_the_merge(self, tmp_path: Path) -> None:
        """Tool names from every row of a message land on that one call.

        A message written as two rows (two parallel tool uses) must give one call carrying both tool names, because the
        usage dedupe of the cost analyzer keeps only the first row and would lose the second tool.
        """
        rows = _row("m1", ts="2030-01-01T00:00:00Z", tools=(("Read", {}), ("Grep", {})), output_tokens=5)
        calls = em.parse_transcript(_write(tmp_path / "s.jsonl", [rows])).calls
        assert [(c.message_id, c.tools, c.usage["output_tokens"]) for c in calls] == [("m1", ["Read", "Grep"], 5)]

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            pytest.param("gh pr view 42 --json body", "gh", id="plain"),
            pytest.param("GH_PAGER= gh pr view 42", "gh", id="leading-assignment"),
            pytest.param("cd repo && gh pr view 42", "gh", id="cd-prefix"),
            pytest.param("env X=1 git log", "git", id="env-prefix"),
            pytest.param("", "?", id="empty-command"),
        ],
    )
    def test_bash_keeps_only_the_first_real_word(self, tmp_path: Path, command: str, expected: str) -> None:
        """A Bash command is reduced to its first real word, so reports never carry command text.

        Command arguments can hold user content; the report guardrail allows metrics only, so the parser drops
        everything after the first word at read time. Environment assignments and a ``cd dir &&`` prefix are looked
        through so a fetch behind them still reads as ``gh``.
        """
        rows = _row("m1", ts="2030-01-01T00:00:00Z", tools=(("Bash", {"command": command}),), output_tokens=1)
        call = em.parse_transcript(_write(tmp_path / "s.jsonl", [rows])).calls[0]
        assert call.cli == [expected]

    def test_tool_input_that_is_not_an_object_does_not_crash(self, tmp_path: Path) -> None:
        """A ``tool_use`` block whose ``input`` is a string is read as an unknown command.

        One odd row anywhere in a transcript must never end the whole run; the call is kept with the first word ``?``.
        """
        rows = _row("m1", ts="2030-01-01T00:00:00Z", tools=(("Bash", "not-an-object"),), output_tokens=1)
        assert em.parse_transcript(_write(tmp_path / "s.jsonl", [rows])).calls[0].cli == ["?"]

    def test_message_without_usage_is_dropped(self, tmp_path: Path) -> None:
        """A message that never reports usage cannot be priced and is left out of the call list."""
        rows = [{"timestamp": "2030-01-01T00:00:00Z", "message": {"id": "m1", "model": OPUS, "content": []}}]
        assert em.parse_transcript(_write(tmp_path / "s.jsonl", [rows])).calls == []

    def test_message_without_id_is_kept(self, tmp_path: Path) -> None:
        """A priced message that lacks an id still counts, as in the cost analyzer.

        Dropping it would make this tool disagree with ``cost_analyzer`` on the totals of the same session.
        """
        rows = [{"timestamp": "2030-01-01T00:00:00Z", "message": {"model": OPUS, "usage": {"output_tokens": 7}}}]
        assert [c.usage["output_tokens"] for c in em.parse_transcript(_write(tmp_path / "s.jsonl", [rows])).calls] == [
            7
        ]

    def test_malformed_row_is_skipped(self, tmp_path: Path) -> None:
        """A row with a string where a counter belongs is skipped without raising and without partial state."""
        bad = [{"timestamp": "2030-01-01T00:00:00Z", "message": {"id": "m1", "usage": {"input_tokens": "x"}}}]
        good = _row("m2", ts="2030-01-01T00:00:01Z", output_tokens=1)
        calls = em.parse_transcript(_write(tmp_path / "s.jsonl", [bad, good])).calls
        assert [c.message_id for c in calls] == ["m2"]

    def test_commands_come_from_user_rows_only(self, tmp_path: Path) -> None:
        """A command name mentioned in assistant or tool text is not a typed command.

        Scenario: a transcript quotes ``<command-name>/oss:resolve</command-name>`` inside an assistant message while
        the user typed ``/oss:review``; only the typed one may select the session.
        """
        quote = _row("m1", ts="2030-01-01T00:00:00Z", output_tokens=1)
        quote[0]["message"]["content"] = [{"type": "text", "text": "<command-name>/oss:resolve</command-name>"}]
        transcript = em.parse_transcript(
            _write(tmp_path / "s.jsonl", [_command_row("2030-01-01T00:00:00Z", "/oss:review"), quote])
        )
        assert list(transcript.commands) == ["/oss:review"]


class TestModelSwitches:
    """Covers R1."""

    @pytest.mark.parametrize(
        ("second_model", "second_write", "expected"),
        [
            pytest.param(SONNET, 90_000, 1, id="switch-with-large-write"),
            pytest.param(SONNET, 1_000, 0, id="switch-with-small-write"),
            pytest.param(OPUS, 90_000, 0, id="large-write-same-model"),
        ],
    )
    def test_only_a_switch_with_a_large_write_counts(self, second_model: str, second_write: int, expected: int) -> None:
        """A finding needs both a model change and a prefix-sized cache write.

        Scenario: growth on the same model writes a lot without being a rewrite, and a switch with a tiny write means
        the new model found its prefix already cached; only the combination is the expensive rewrite.
        """
        first = em.MCall("a", 0.0, OPUS, {"cache_creation_input_tokens": 100})
        second = em.MCall("b", 0.0, second_model, {"cache_creation_input_tokens": second_write})
        assert len(em.model_switches([first, second])) == expected

    def test_dollars_use_the_new_models_write_rate(self) -> None:
        """The reported cost is the cache write priced at the model that wrote it.

        1M tokens written on Opus 5.5 with no duration split price at the 5-minute rate, 1.25 × $4.
        """
        first = em.MCall("a", 0.0, SONNET, {})
        second = em.MCall("b", 0.0, OPUS, {"cache_creation_input_tokens": 1_000_000})
        assert em.model_switches([first, second])[0].cost_usd == pytest.approx(5.0)

    def test_one_hour_write_is_priced_at_the_one_hour_rate(self) -> None:
        """A write that the usage marks as 1-hour costs 2 × the input rate, not 1.25 ×.

        Main-chain writes are one-hour writes, so pricing them at the five-minute rate would understate every R1 figure
        by 37.5 percent.
        """
        usage = {"cache_creation_input_tokens": 1_000_000, "cache_creation": {"ephemeral_1h_input_tokens": 1_000_000}}
        assert em.MCall("a", 0.0, OPUS, usage).write_cost_usd == pytest.approx(8.0)

    def test_a_span_owns_no_exit_to_a_model_it_only_used_inside(self) -> None:
        """The revert after a span counts only when it returns to the model the entry left.

        Scenario: Opus, then the span on Sonnet that moves to Haiku inside, then the next skill on Sonnet. Sonnet is a
        model the span used, not the one it left, so that last rewrite belongs to the next skill, not to this span.
        """
        usage = {"cache_creation_input_tokens": 90_000}
        before, entry, inner, after = (
            em.MCall("a", 0.0, OPUS, usage),
            em.MCall("b", 0.0, SONNET, usage),
            em.MCall("c", 0.0, HAIKU, usage),
            em.MCall("d", 0.0, SONNET, usage),
        )
        found = em.model_switches([before, entry, inner, after], span=[entry, inner])
        assert [(s.index, s.exit) for s in found] == [(1, False), (2, False)]

    def test_each_run_of_the_span_owns_only_its_own_exit(self) -> None:
        """A later run cannot claim a revert to the model an earlier run left, nor an earlier run one to a later model.

        Scenario: Opus, a run on Sonnet, another command on Haiku, a second run on Sonnet, then Opus. The first run left
        Opus and the second left Haiku, so the final Opus rewrite returns to neither run's own entry model.
        """
        usage = {"cache_creation_input_tokens": 90_000}
        first = em.MCall("b", 0.0, SONNET, usage)
        second = em.MCall("d", 0.0, SONNET, usage)
        chain = [em.MCall("a", 0.0, OPUS, usage), first, em.MCall("c", 0.0, HAIKU, usage), second]
        found = em.model_switches([*chain, em.MCall("e", 0.0, OPUS, usage)], span=[first, second])
        assert [(s.index, s.exit) for s in found] == [(1, False), (2, False)]

    def test_a_small_entry_still_lets_the_span_own_its_large_revert(self) -> None:
        """The exit counts when the span's entry changed the model, even if the entry itself wrote little.

        Scenario: Opus, then a span on Sonnet whose first call finds its prefix mostly cached (a small write), then Opus
        again with a full rewrite; that revert is the pin's cost and must not vanish because the entry was cheap.
        """
        opus = em.MCall("a", 0.0, OPUS, {"cache_creation_input_tokens": 100})
        sonnet = em.MCall("b", 0.0, SONNET, {"cache_creation_input_tokens": 1_000})
        back = em.MCall("c", 0.0, OPUS, {"cache_creation_input_tokens": 90_000})
        found = em.model_switches([opus, sonnet, back], span=[sonnet])
        assert [(s.index, s.exit) for s in found] == [(2, True)]

    @pytest.mark.parametrize(
        ("ts", "expected"),
        [
            pytest.param(15.0, True, id="inside"),
            pytest.param(25.0, False, id="after"),
            pytest.param(0.0, True, id="untimed-is-kept"),
        ],
    )
    def test_untimed_call_stays_inside_a_bounded_span(self, ts: float, expected: bool) -> None:
        """A call with no timestamp cannot be placed outside a span, so it is kept rather than silently dropped."""
        assert em._in_spans(ts, [(10.0, 20.0)]) is expected

    def test_call_numbers_are_one_based(self) -> None:
        """The second call of a transcript is reported as call 2, matching the ``--timeline`` rows."""
        first = em.MCall("a", 0.0, OPUS, {})
        second = em.MCall("b", 0.0, SONNET, {"cache_creation_input_tokens": 90_000})
        assert [s.index for s in em.model_switches([first, second])] == [2]


class TestIdleLosses:
    """Covers R7."""

    @pytest.mark.parametrize(
        ("gap", "write", "model", "expected"),
        [
            pytest.param(500, 80_000, OPUS, 1, id="long-gap-large-rewrite"),
            pytest.param(60, 80_000, OPUS, 0, id="short-gap"),
            pytest.param(500, 1_000, OPUS, 0, id="cache-survived"),
            pytest.param(500, 80_000, SONNET, 0, id="model-change-belongs-to-r1"),
        ],
    )
    def test_gap_write_and_model_decide(self, gap: int, write: int, model: str, expected: int) -> None:
        """Only a same-model gap above the idle threshold that is followed by a large write is a cache loss."""
        first = em.MCall("a", 1000.0, OPUS, {})
        second = em.MCall("b", 1000.0 + gap, model, {"cache_creation_input_tokens": write})
        assert len(em.idle_losses([first, second])) == expected

    def test_unknown_timestamp_never_counts(self) -> None:
        """A call without a usable timestamp cannot define a gap, so it is never reported as an idle loss."""
        first = em.MCall("a", 0.0, OPUS, {})
        second = em.MCall("b", 5000.0, OPUS, {"cache_creation_input_tokens": 80_000})
        assert em.idle_losses([first, second]) == []

    def test_growth_that_still_reads_the_prefix_is_not_a_loss(self) -> None:
        """A big write beside a big cache read is new content, not a rewritten prefix.

        Scenario: after a long tool run the next call adds 80K new tokens but reads 200K back from cache; the cache
        survived, so no dollars were lost to expiry.
        """
        first = em.MCall("a", 1000.0, OPUS, {"cache_read_input_tokens": 200_000})
        second = em.MCall(
            "b", 1500.0, OPUS, {"cache_creation_input_tokens": 80_000, "cache_read_input_tokens": 200_000}
        )
        assert em.idle_losses([first, second]) == []

    def test_a_compaction_is_not_a_loss(self) -> None:
        """A call whose prompt shrank well below the previous one is a compaction rebuild, not an idle expiry."""
        first = em.MCall("a", 1000.0, OPUS, {"cache_read_input_tokens": 400_000})
        second = em.MCall("b", 1500.0, OPUS, {"cache_creation_input_tokens": 80_000})
        assert em.idle_losses([first, second]) == []

    def test_expired_prefix_is_a_loss(self) -> None:
        """A big write with almost no cache read after a long gap is the expiry the check exists to find."""
        first = em.MCall("a", 1000.0, OPUS, {"cache_read_input_tokens": 80_000})
        second = em.MCall("b", 1500.0, OPUS, {"cache_creation_input_tokens": 80_000, "cache_read_input_tokens": 100})
        assert [loss.index for loss in em.idle_losses([first, second])] == [2]

    def test_subagent_gap_uses_the_five_minute_limit(self) -> None:
        """A subagent gap needs more than five minutes, and the loss names where it happened."""
        first = em.MCall("a", 1000.0, OPUS, {})
        short = em.MCall("b", 1250.0, OPUS, {"cache_creation_input_tokens": 80_000})
        long = em.MCall("c", 1400.0, OPUS, {"cache_creation_input_tokens": 80_000})
        assert em.idle_losses([first, short], gap_s=em.SUBAGENT_IDLE_S, where="x") == []
        assert [loss.where for loss in em.idle_losses([first, long], gap_s=em.SUBAGENT_IDLE_S, where="x")] == ["x"]


class TestLiteralPrefix:
    """Covers R5."""

    def test_counts_leading_literal_calls_only(self) -> None:
        """The run ends at the first call that is not a pure gh/git Bash call, even if later calls are literal."""
        fetch = em.MCall("a", 0.0, OPUS, {}, tools=["Bash"], cli=["gh"])
        mixed = em.MCall("b", 0.0, OPUS, {}, tools=["Bash"], cli=["gh", "python"])
        assert em.literal_prefix([fetch, fetch, mixed, fetch]) == 2

    def test_a_non_bash_tool_ends_the_run(self) -> None:
        """A parallel Read beside a gh call makes the call non-literal."""
        parallel = em.MCall("a", 0.0, OPUS, {}, tools=["Bash", "Read"], cli=["gh"])
        assert em.literal_prefix([parallel]) == 0


class TestAnalyseSession:
    """Covers R2, R3 and R4 end to end on a synthetic session with one subagent."""

    @pytest.fixture
    def session(self, tmp_path: Path) -> Path:
        """Session: cold start, subagent fetch, model switch, then the first question, then work after the gate."""
        main = _write(
            tmp_path / "proj" / "abc12345.jsonl",
            [
                _row("m1", ts="2030-01-01T00:00:00Z", model=SONNET, cache_creation_input_tokens=100_000),
                _row("m2", ts="2030-01-01T00:10:00Z", model=OPUS, cache_creation_input_tokens=150_000),
                _row("m3", ts="2030-01-01T00:11:00Z", tools=(("AskUserQuestion", {}),), output_tokens=100),
                _row("m4", ts="2030-01-01T00:30:00Z", output_tokens=1_000_000),
            ],
        )
        calls = [_row(f"s{n}", ts="2030-01-01T00:05:00Z", tools=(("Bash", {"command": "gh api x"}),)) for n in range(6)]
        calls.append(_row("s6", ts="2030-01-01T00:20:00Z", tools=(("Read", {}),)))
        for rows in calls:
            rows[0]["message"]["usage"] = {"cache_creation_input_tokens": 1_000}
        _write(tmp_path / "proj" / "abc12345" / "subagents" / "agent-1.jsonl", calls)
        meta = tmp_path / "proj" / "abc12345" / "subagents" / "agent-1.meta.json"
        meta.write_text(json.dumps({"agentType": "foundry:sw-engineer"}), encoding="utf-8")
        return main

    def test_pre_gate_cost_is_exact_and_excludes_later_spend(self, session: Path) -> None:
        """The pre-gate figure is the exact sum up to and including the question, main and subagent apart.

        Main: 100K Sonnet write at $2 × 1.25 = $0.25, 150K Opus write at $4 × 1.25 = $0.75 and 100 Opus output tokens at
        $20 per million = $0.002. The 1M-token output call after the gate would add $20 and the seventh subagent call
        runs after the question; neither may reach the figure. Six subagent writes of 1K tokens cost $0.03.
        """
        metrics = em.analyse_session(session)
        assert metrics is not None
        assert metrics.gate_found
        assert metrics.pre_gate_main_usd == pytest.approx(1.002)
        assert metrics.pre_gate_sub_usd == pytest.approx(0.03)

    def test_without_a_question_the_whole_session_counts(self, tmp_path: Path) -> None:
        """A session that never asks anything is measured whole and says so."""
        path = _write(tmp_path / "p" / "s.jsonl", [_row("m1", ts="2030-01-01T00:00:00Z", output_tokens=1_000_000)])
        metrics = em.analyse_session(path)
        assert metrics is not None
        assert (metrics.gate_found, metrics.pre_gate_main_usd) == (False, pytest.approx(20.0))

    def test_cold_start_is_the_first_call(self, session: Path) -> None:
        """The cold-start figure is the first call's cache write and cost."""
        metrics = em.analyse_session(session)
        assert metrics is not None
        assert metrics.cold_write == 100_000

    def test_switch_is_found(self, session: Path) -> None:
        """The Sonnet → Opus call with a 150K write is reported as one rewrite, as call 2."""
        metrics = em.analyse_session(session)
        assert metrics is not None
        assert [(s.index, s.model) for s in metrics.switches] == [(2, OPUS)]

    def test_subagent_literal_prefix_is_flagged(self, session: Path) -> None:
        """Six leading gh calls in the subagent cross the script-candidate threshold."""
        metrics = em.analyse_session(session)
        assert metrics is not None
        assert [(a.agent_type, a.literal_prefix, a.flags) for a in metrics.agents] == [
            ("foundry:sw-engineer", 6, ["literal-cli-prefix:6"])
        ]

    def test_session_without_usage_gives_none(self, tmp_path: Path) -> None:
        """An empty transcript yields no metrics rather than a zero row."""
        empty = tmp_path / "e.jsonl"
        empty.write_text("", encoding="utf-8")
        assert em.analyse_session(empty) is None

    def test_a_skill_is_measured_from_its_own_command_row(self, tmp_path: Path) -> None:
        """Two skills in one session each get their own span, cold start, gate and switches.

        Scenario: ``/foundry:audit`` asks a question, then ``/oss:resolve`` enters on Sonnet (an entry rewrite, call 1)
        and switches to Opus before its own question (call 2); the audit's span must not take resolve's entry as its
        own exit switch. Charging the whole session to both labels would give resolve the audit's cold start and miss
        resolve's gate; each span must see only its own calls.
        """
        path = _write(
            tmp_path / "p" / "s.jsonl",
            [
                _command_row("2030-01-01T00:00:00Z", "/foundry:audit"),
                _row("a1", ts="2030-01-01T00:00:01Z", cache_creation_input_tokens=60_000),
                _row("a2", ts="2030-01-01T00:00:02Z", tools=(("AskUserQuestion", {}),), output_tokens=10),
                _command_row("2030-01-01T01:00:00Z", "/oss:resolve"),
                _row("r1", ts="2030-01-01T01:00:01Z", model=SONNET, cache_creation_input_tokens=70_000),
                _row("r2", ts="2030-01-01T01:00:02Z", cache_creation_input_tokens=90_000),
                _row("r3", ts="2030-01-01T01:00:03Z", tools=(("AskUserQuestion", {}),), output_tokens=10),
            ],
        )
        audit = em.analyse_session(path, "/foundry:audit")
        resolve = em.analyse_session(path, "/oss:resolve")
        assert audit is not None
        assert resolve is not None
        assert (audit.calls, audit.cold_write, len(audit.switches)) == (2, 60_000, 0)
        assert (resolve.calls, resolve.cold_write, [s.index for s in resolve.switches]) == (3, 70_000, [1, 2])

    def test_builtin_command_does_not_end_the_span(self, tmp_path: Path) -> None:
        """A ``/compact`` typed at a long-idle gate leaves the skill's later calls inside its span.

        Scenario: resolve asks its question, the user compacts, then the run continues. Ending the span at ``/compact``
        would drop everything after the gate: its switches, idle losses and subagents.
        """
        path = _write(
            tmp_path / "p" / "s.jsonl",
            [
                _command_row("2030-01-01T00:00:00Z", "/oss:resolve"),
                _row("r1", ts="2030-01-01T00:00:01Z", tools=(("AskUserQuestion", {}),), output_tokens=10),
                _command_row("2030-01-01T00:30:00Z", "/compact"),
                _row("r2", ts="2030-01-01T00:30:01Z", output_tokens=10),
                _command_row("2030-01-01T01:00:00Z", "/oss:review"),
                _row("v1", ts="2030-01-01T01:00:01Z", output_tokens=10),
            ],
        )
        metrics = em.analyse_session(path, "/oss:resolve")
        assert metrics is not None
        assert metrics.calls == 2

    def test_a_mid_session_pin_counts_both_rewrites(self, tmp_path: Path) -> None:
        """The entry rewrite and the revert rewrite after the span are both attributed to the pinned skill.

        Scenario: an Opus session runs a Sonnet-pinned skill, then the user types another command and the next call is
        Opus again. The entry call is call 1 of the span; the revert is the call just after it, indexed one past the
        last span call. Counting only calls inside the span would show zero switches for the pin that costs two.
        """
        path = _write(
            tmp_path / "p" / "s.jsonl",
            [
                _row("o0", ts="2030-01-01T00:00:00Z", output_tokens=1),
                _command_row("2030-01-01T00:10:00Z", "/oss:resolve"),
                _row("r1", ts="2030-01-01T00:10:01Z", model=SONNET, cache_creation_input_tokens=70_000),
                _row("r2", ts="2030-01-01T00:10:02Z", model=SONNET, tools=(("AskUserQuestion", {}),), output_tokens=1),
                _command_row("2030-01-01T00:20:00Z", "/oss:review"),
                _row("v1", ts="2030-01-01T00:20:01Z", cache_creation_input_tokens=80_000),
            ],
        )
        metrics = em.analyse_session(path, "/oss:resolve")
        assert metrics is not None
        assert [(s.index, s.model) for s in metrics.switches] == [(1, SONNET), (3, OPUS)]

    def test_every_invocation_of_the_command_is_measured(self, tmp_path: Path) -> None:
        """A second run of the same skill in one session is counted, not only the first.

        Scenario: resolve runs, review runs, resolve runs again; the second run switches model. Measuring only the
        first invocation would lose that rewrite.
        """
        path = _write(
            tmp_path / "p" / "s.jsonl",
            [
                _command_row("2030-01-01T00:00:00Z", "/oss:resolve"),
                _row("a1", ts="2030-01-01T00:00:01Z", output_tokens=1),
                _command_row("2030-01-01T01:00:00Z", "/oss:review"),
                _row("b1", ts="2030-01-01T01:00:01Z", output_tokens=1),
                _command_row("2030-01-01T02:00:00Z", "/oss:resolve"),
                _row("a2", ts="2030-01-01T02:00:01Z", model=SONNET, cache_creation_input_tokens=70_000),
            ],
        )
        metrics = em.analyse_session(path, "/oss:resolve")
        assert metrics is not None
        assert (metrics.calls, [s.index for s in metrics.switches]) == (2, [2])

    def test_subagents_are_attributed_to_their_own_span(self, tmp_path: Path) -> None:
        """A subagent that ran inside another skill's span, or outside every span, is not charged to this skill.

        Scenario: two skills in one session each spawn one subagent. Each skill must list only its own agent, and a
        skill must not list an agent with no call inside its span.
        """
        path = _write(
            tmp_path / "p" / "s.jsonl",
            [
                _command_row("2030-01-01T00:00:00Z", "/a:one"),
                _row("a1", ts="2030-01-01T00:00:01Z", output_tokens=1),
                _command_row("2030-01-01T01:00:00Z", "/b:two"),
                _row("b1", ts="2030-01-01T01:00:01Z", output_tokens=1),
            ],
        )
        for name, stamp in (("one", "2030-01-01T00:00:02Z"), ("two", "2030-01-01T01:00:02Z")):
            _write(path.parent / "s" / "subagents" / f"agent-{name}.jsonl", [_row(name, ts=stamp, output_tokens=5)])
        first, second = em.analyse_session(path, "/a:one"), em.analyse_session(path, "/b:two")
        assert first is not None
        assert second is not None
        assert (len(first.agents), len(second.agents)) == (1, 1)
        assert first.agents[0].calls == 1

    def test_untimed_gate_does_not_drop_subagent_spend(self, tmp_path: Path) -> None:
        """A gate call without a timestamp leaves the subagent pre-gate window unbounded instead of zero.

        Scenario: the question row carries no timestamp, so there is no cutoff; a $5 subagent output must still count
        toward the pre-gate figure rather than silently vanish.
        """
        path = _write(
            tmp_path / "p" / "s.jsonl",
            [_row("m1", ts="", tools=(("AskUserQuestion", {}),), output_tokens=10)],
        )
        _write(
            path.parent / "s" / "subagents" / "agent-1.jsonl",
            [_row("x", ts="2030-01-01T00:00:00Z", output_tokens=250_000)],
        )
        metrics = em.analyse_session(path)
        assert metrics is not None
        assert metrics.pre_gate_sub_usd == pytest.approx(5.0)

    def test_untimed_gate_after_timed_calls_keeps_the_subagent_spend(self, tmp_path: Path) -> None:
        """An untimed question after timed calls does not borrow the last timed call as its cutoff.

        Scenario: a timed call, then an untimed question; a subagent call made after the timed call and before the
        question is still pre-gate spend, so the $5 must count.
        """
        path = _write(
            tmp_path / "p" / "s.jsonl",
            [
                _row("m0", ts="2030-01-01T00:00:00Z", output_tokens=10),
                _row("m1", ts="", tools=(("AskUserQuestion", {}),), output_tokens=10),
            ],
        )
        _write(
            path.parent / "s" / "subagents" / "agent-1.jsonl",
            [_row("x", ts="2030-01-01T00:05:00Z", output_tokens=250_000)],
        )
        metrics = em.analyse_session(path)
        assert metrics is not None
        assert metrics.pre_gate_sub_usd == pytest.approx(5.0)

    def test_an_unpinned_neighbour_owns_no_border_rewrite(self, tmp_path: Path) -> None:
        """A skill with no pin of its own is not charged for the rewrite where a pinned neighbour reverts.

        Scenario: Opus session, then a Sonnet-pinned ``/p:pinned``, then ``/x:plain`` whose first call is back on Opus.
        The revert is the pin's cost; charging it to ``/x:plain`` would make the fixed skill fail the rule's check.
        """
        path = _write(
            tmp_path / "p" / "s.jsonl",
            [
                _row("o0", ts="2030-01-01T00:00:00Z", output_tokens=1),
                _command_row("2030-01-01T00:10:00Z", "/p:pinned"),
                _row("p1", ts="2030-01-01T00:10:01Z", model=SONNET, cache_creation_input_tokens=70_000),
                _command_row("2030-01-01T00:20:00Z", "/x:plain"),
                _row("x1", ts="2030-01-01T00:20:01Z", cache_creation_input_tokens=80_000),
                _row("x2", ts="2030-01-01T00:20:02Z", output_tokens=1),
            ],
        )
        pinned = em.analyse_session(path, "/p:pinned")
        plain = em.analyse_session(path, "/x:plain", edges=False)
        assert pinned is not None
        assert plain is not None
        assert [(s.index, s.exit) for s in pinned.switches] == [(1, False), (2, True)]
        assert plain.switches == []

    def test_idle_gap_is_not_paired_across_other_commands(self, tmp_path: Path) -> None:
        """Two runs of one skill, with another command's calls between them, do not make a fake idle gap.

        Scenario: ``/a:x`` call at 0 s, ``/b:y`` calls up to 290 s, ``/a:x`` again at 300 s with a big write. The two
        ``/a:x`` calls are 300 s apart in the span union but the second follows a call 10 s earlier.
        """
        path = _write(
            tmp_path / "p" / "s.jsonl",
            [
                _command_row("2030-01-01T00:00:00Z", "/a:x"),
                _row("a1", ts="2030-01-01T00:00:01Z", output_tokens=1),
                _command_row("2030-01-01T00:00:10Z", "/b:y"),
                _row("b1", ts="2030-01-01T00:00:11Z", output_tokens=1),
                _row("b2", ts="2030-01-01T00:04:50Z", output_tokens=1),
                _command_row("2030-01-01T00:04:55Z", "/a:x"),
                _row("a2", ts="2030-01-01T00:05:01Z", cache_creation_input_tokens=80_000),
            ],
        )
        metrics = em.analyse_session(path, "/a:x")
        assert metrics is not None
        assert metrics.idle_losses == []

    def test_command_never_typed_measures_the_whole_session(self, session: Path) -> None:
        """Asking for a command the session never ran falls back to the whole session instead of dropping it."""
        metrics = em.analyse_session(session, "/never:typed")
        assert metrics is not None
        assert metrics.calls == 4


class TestBaselineLedger:
    """Covers R9."""

    def test_record_then_delta(self, tmp_path: Path) -> None:
        """A second run against a recorded baseline prints before, now and the change.

        Scenario: the first run records pre-gate median $4.00; the same label later measures $1.50, so the table must
        show the -2.50 change that proves the fix.
        """
        ledger = tmp_path / "notes" / "baselines.jsonl"
        em.append_record(ledger, "oss:resolve", {"pre_gate_median_usd": 4.0})
        lines = em.delta_lines(em.last_record(ledger, "oss:resolve"), {"pre_gate_median_usd": 1.5})
        assert any("| pre_gate_median_usd | 4.0 | 1.5 | -2.50 |" in line for line in lines)

    def test_other_label_is_ignored(self, tmp_path: Path) -> None:
        """A record under another label is not a baseline for this one."""
        ledger = tmp_path / "baselines.jsonl"
        em.append_record(ledger, "oss:review", {"pre_gate_median_usd": 9.0})
        assert em.last_record(ledger, "oss:resolve") is None

    def test_first_run_is_the_baseline(self) -> None:
        """With no earlier record the report says so instead of inventing a delta."""
        assert "baseline" in em.delta_lines(None, {})[0]

    def test_baseline_stays_pinned_while_runs_accumulate(self, tmp_path: Path) -> None:
        """The comparison point is the first record, not whichever run came last.

        Scenario: a pre-fix record ($4.00) is followed by two post-fix runs ($1.50, $1.48). Comparing only with the last
        run would show -0.02 and hide that the fix saved $2.50; the section must show both comparisons.
        """
        ledger = tmp_path / "b.jsonl"
        for median, baseline in ((4.0, True), (1.5, False)):
            em.append_record(
                ledger, "/x:y", {"pre_gate_median_usd": median}, {"baseline": baseline, "session_ids": ["aaaa1111"]}
            )
        text = "\n".join(em._delta_section(ledger, "/x:y", {"pre_gate_median_usd": 1.48}, ["aaaa1111", "bbbb2222"]))
        assert "| pre_gate_median_usd | 4.0 | 1.48 | -2.52 |" in text
        assert "| pre_gate_median_usd | 1.5 | 1.48 | -0.02 |" in text
        assert "1 of this run's 2 session(s) were already in that baseline" in text

    def test_other_scope_or_window_is_flagged_in_the_delta(self, tmp_path: Path) -> None:
        """A pinned baseline from another project scope or window earns a warning above its delta table.

        Scenario: the baseline was recorded over all projects for 30 days; this run covers one project for 7 days, so
        a drop in sessions is the population changing, not the skill. A record without a scope reads as unknown.
        """
        ledger = tmp_path / "b.jsonl"
        em.append_record(
            ledger,
            "/x:y",
            {"pre_gate_median_usd": 4.0},
            {"baseline": True, "scope": "all", "since": "30d", "border": True},
        )
        same = "\n".join(em._delta_section(ledger, "/x:y", {"pre_gate_median_usd": 1.0}, [], "all", "30d"))
        other = "\n".join(em._delta_section(ledger, "/x:y", {"pre_gate_median_usd": 1.0}, [], "-proj", "7d"))
        assert "⚠" not in same
        assert "covered scope `all`, window `30d`; this run covers scope `-proj`, window `7d`" in other

    def test_window_only_mismatch_warns_but_equal_windows_do_not(self, tmp_path: Path) -> None:
        """A different window warns even with the same scope, while ``30d`` and ``720h`` are the same window."""
        ledger = tmp_path / "b.jsonl"
        em.append_record(
            ledger,
            "/x:y",
            {"pre_gate_median_usd": 4.0},
            {"baseline": True, "scope": "-p", "since": "30d", "border": True},
        )
        same = "\n".join(em._delta_section(ledger, "/x:y", {"pre_gate_median_usd": 1.0}, [], "-p", "720h"))
        other = "\n".join(em._delta_section(ledger, "/x:y", {"pre_gate_median_usd": 1.0}, [], "-p", "7d"))
        assert "⚠" not in same
        assert "window `30d`; this run covers scope `-p`, window `7d`" in other

    @pytest.mark.parametrize(
        ("recorded", "expected"),
        [
            pytest.param({"border": False}, "counted border rewrites: no; this run: yes", id="other-method"),
            pytest.param({}, "counted border rewrites: not recorded; this run: yes", id="method-not-recorded"),
        ],
    )
    def test_other_border_method_is_flagged_in_the_delta(self, tmp_path: Path, recorded: dict, expected: str) -> None:
        """A record made with another way of counting border rewrites warns, even over the same scope and window.

        Scenario: the baseline came from an audit run that left a de-pinned skill's border rewrites out; this run is a
        hand run that counts them, so a changed switch count would be the method, not the fix.
        """
        ledger = tmp_path / "b.jsonl"
        extra = {"baseline": True, "scope": "-p", "since": "30d", **recorded}
        em.append_record(ledger, "/x:y", {"pre_gate_median_usd": 4.0}, extra)
        text = "\n".join(em._delta_section(ledger, "/x:y", {"pre_gate_median_usd": 1.0}, [], "-p", "30d", True))
        assert expected in text
        assert "The populations differ" not in text

    def test_previous_run_of_another_scope_warns(self, tmp_path: Path) -> None:
        """The previous-run table gets its own warning when that run covered another scope than this one."""
        ledger = tmp_path / "b.jsonl"
        em.append_record(
            ledger, "/x:y", {"pre_gate_median_usd": 4.0}, {"baseline": True, "scope": "-p", "since": "30d"}
        )
        em.append_record(
            ledger, "/x:y", {"pre_gate_median_usd": 3.0}, {"baseline": False, "scope": "-q", "since": "30d"}
        )
        text = "\n".join(em._delta_section(ledger, "/x:y", {"pre_gate_median_usd": 1.0}, [], "-p", "30d"))
        assert "The previous run covered scope `-q`" in text
        assert "The pinned baseline covered" not in text

    def test_default_ledger_is_anchored_at_the_checkout(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The default ledger path follows the main checkout; an explicit path is left alone."""
        monkeypatch.setattr(em, "_repo_root", lambda: tmp_path)
        assert em._ledger_path(em.DEFAULT_BASELINE) == tmp_path / em.DEFAULT_BASELINE
        assert em._ledger_path(str(tmp_path / "x.jsonl")) == tmp_path / "x.jsonl"

    def test_rebaseline_replaces_the_pinned_record(self, tmp_path: Path) -> None:
        """The latest record marked as a baseline wins over older and unmarked ones.

        Scenario: a ledger from before baselines were flagged holds $9.00; a run recorded with ``--rebaseline`` at $4.00
        becomes the comparison point, and a later plain run at $1.50 does not replace it.
        """
        ledger = tmp_path / "b.jsonl"
        em.append_record(ledger, "/x:y", {"pre_gate_median_usd": 9.0})
        em.append_record(ledger, "/x:y", {"pre_gate_median_usd": 4.0}, {"baseline": True})
        em.append_record(ledger, "/x:y", {"pre_gate_median_usd": 1.5}, {"baseline": False})
        pinned = em.pinned_record(ledger, "/x:y")
        assert pinned is not None
        assert pinned["pre_gate_median_usd"] == 4.0

    def test_append_restores_a_missing_final_newline(self, tmp_path: Path) -> None:
        """A ledger edited by hand without a trailing newline does not swallow the next record."""
        ledger = tmp_path / "b.jsonl"
        ledger.write_text('{"label": "/a:b", "sessions": 1}', encoding="utf-8")
        em.append_record(ledger, "/a:b", {"sessions": 2})
        assert [r["sessions"] for r in em.label_records(ledger, "/a:b")] == [1, 2]

    @pytest.mark.parametrize(
        ("label", "expected"),
        [
            pytest.param("oss:resolve", "/oss:resolve", id="bare-name"),
            pytest.param("/oss:resolve", "/oss:resolve", id="slash-form"),
            pytest.param("default", "default", id="default-kept"),
        ],
    )
    def test_labels_share_one_slash_form(self, label: str, expected: str) -> None:
        """A label written with or without the slash lands on the same ledger entry."""
        assert em.normalise_label(label) == expected


class TestScanCommands:
    """Covers turning scanner output into slash commands."""

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            pytest.param("plugins/cc_oss/skills/resolve/SKILL.md", "/oss:resolve", id="cc-plugin-skill"),
            pytest.param(
                "plugins/codemap-py/claude-skills/query-code/SKILL.md", "/codemap-py:query-code", id="claude-skills"
            ),
            pytest.param(".claude/skills/audit/SKILL.md", "/audit", id="project-local-skill"),
            pytest.param("plugins/cc_oss/agents/shepherd.md", None, id="agent-file"),
            pytest.param(
                "~/.claude/plugins/cache/market/oss/0.55.0/skills/resolve/SKILL.md",
                "/oss:resolve",
                id="installed-cache-path-skips-version-dir",
            ),
        ],
    )
    def test_command_for_skill(self, path: str, expected: str | None) -> None:
        """A skill file path maps to the slash command a user types; anything else maps to none.

        The mapping matters because transcripts record the typed command, and the audit scan names only file paths.
        """
        assert em.command_for_skill(path) == expected

    def test_plugin_name_comes_from_the_manifest(self, tmp_path: Path) -> None:
        """A directory whose name differs from the plugin name maps through its manifest.

        Scenario: the source directory ``bridge_cc-codex`` ships the plugin ``bridge``; the typed command is
        ``/bridge:advise``, so the directory-derived name would never match a transcript.
        """
        plugin = tmp_path / "bridge_cc-codex"
        (plugin / ".claude-plugin").mkdir(parents=True)
        (plugin / ".claude-plugin" / "plugin.json").write_text('{"name": "bridge"}', encoding="utf-8")
        assert em.command_for_skill(str(plugin / "claude-skills" / "advise" / "SKILL.md")) == "/bridge:advise"

    def test_windows_manifest_path_remains_absolute(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A Windows drive anchor remains absolute when scanner path parts are reassembled."""
        manifest = tmp_path / ".claude-plugin" / "plugin.json"
        manifest.parent.mkdir()
        manifest.write_text('{"name": "bridge"}', encoding="utf-8")
        paths = []

        def native_path(*parts: str) -> Path:
            paths.append(PureWindowsPath(*parts))
            return tmp_path

        monkeypatch.setattr(em, "Path", native_path)
        parts = PurePosixPath("C:/work/bridge_cc-codex/claude-skills/advise/SKILL.md").parts
        assert em._plugin_name(parts, parts.index("claude-skills"), "fallback") == "bridge"
        assert paths == [PureWindowsPath("C:/work/bridge_cc-codex")]
        assert paths[0].is_absolute()

    def test_only_inline_model_lines_are_used(self) -> None:
        """NO_MODEL lines and repeated skills do not add commands; the order of first sight is kept."""
        scan = (
            "INLINE_MODEL: plugins/cc_oss/skills/review/SKILL.md\n"
            "NO_MODEL: plugins/cc_oss/skills/other/SKILL.md\n"
            "INLINE_MODEL: plugins/cc_oss/skills/review/SKILL.md\n"
            "INLINE_MODEL: plugins/cc_oss/skills/resolve/SKILL.md\n"
        )
        assert em.commands_from_scan(scan) == ["/oss:review", "/oss:resolve"]


class TestMain:
    """Covers the command line."""

    def test_fixed_skill_keeps_its_baseline_comparison(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A skill whose pin was removed is still measured, because the ledger names it.

        Scenario: the baseline holds /oss:resolve at $4.00 pre-gate, the new scan no longer flags that skill (its
        pin is gone) and is empty, and a session that ran the command exists. The report must still hold the skill's
        section with a delta table; without the ledger union the before-and-after comparison could never print.
        """
        _write(
            tmp_path / "proj" / "abc12345.jsonl",
            [
                _command_row("2030-01-01T00:00:00Z", "/oss:resolve"),
                _row("m1", ts="2030-01-01T00:00:01Z", output_tokens=10),
            ],
        )
        ledger = tmp_path / "ledger.jsonl"
        em.append_record(ledger, "/oss:resolve", {"pre_gate_median_usd": 4.0})
        scan = tmp_path / "scan.txt"
        scan.write_text("", encoding="utf-8")
        argv = [
            "--projects-root",
            str(tmp_path),
            "--scan-file",
            str(scan),
            "--baseline",
            str(ledger),
            "--since",
            "36500d",
        ]
        assert em.main(argv) == 0
        out = capsys.readouterr().out
        assert "Label `/oss:resolve`" in out
        assert "| pre_gate_median_usd | 4.0 |" in out

    def test_non_command_labels_are_not_remeasured(self, tmp_path: Path) -> None:
        """A custom label such as ``default`` is not a slash command, so the scan-driven run never selects by it."""
        ledger = tmp_path / "ledger.jsonl"
        em.append_record(ledger, "default", {"sessions": 1})
        assert em.ledger_labels(ledger) == []

    def test_scan_file_measures_each_flagged_skill(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A scan file drives one report section per INLINE_MODEL skill that has matching sessions.

        Scenario: the scan names /oss:resolve and /oss:review; only a session that ran /oss:resolve exists, so the
        report holds one section and the other command is skipped without failing the run.
        """
        _write(
            tmp_path / "proj" / "abc12345.jsonl",
            [
                _command_row("2030-01-01T00:00:00Z", "/oss:resolve"),
                _row("m1", ts="2030-01-01T00:00:01Z", output_tokens=10),
            ],
        )
        scan = tmp_path / "scan.txt"
        scan.write_text(
            "INLINE_MODEL: plugins/cc_oss/skills/resolve/SKILL.md\nINLINE_MODEL: plugins/cc_oss/skills/review/SKILL.md\n",
            encoding="utf-8",
        )
        isolated = str(tmp_path / "empty-ledger.jsonl")
        argv = ["--projects-root", str(tmp_path), "--scan-file", str(scan), "--baseline", isolated, "--since", "36500d"]
        assert em.main(argv) == 0
        assert capsys.readouterr().out.count("## Measured efficiency") == 1

    def test_scan_record_writes_one_pinned_record_per_label(self, tmp_path: Path) -> None:
        """``--scan-file --record`` appends one record per measured skill, with its sessions and pinned flag."""
        for name in ("one", "two"):
            _write(
                tmp_path / "proj" / f"{name}12345.jsonl",
                [
                    _command_row("2030-01-01T00:00:00Z", f"/oss:{name}"),
                    _row("m1", ts="2030-01-01T00:00:01Z", output_tokens=10),
                ],
            )
        scan = tmp_path / "scan.txt"
        scan.write_text(
            "INLINE_MODEL: plugins/cc_oss/skills/one/SKILL.md\nINLINE_MODEL: plugins/cc_oss/skills/two/SKILL.md\n",
            encoding="utf-8",
        )
        ledger = tmp_path / "ledger.jsonl"
        argv = [
            "--projects-root",
            str(tmp_path),
            "--scan-file",
            str(scan),
            "--baseline",
            str(ledger),
            "--since",
            "36500d",
            "--record",
        ]
        assert em.main(argv) == 0
        records = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
        assert [(r["label"], r["baseline"], r["session_ids"]) for r in records] == [
            ("/oss:one", True, ["one12345"]),
            ("/oss:two", True, ["two12345"]),
        ]

    def test_selection_follows_the_command_timestamp_not_file_age(self, tmp_path: Path) -> None:
        """A session whose command row is old is not selected even though its file was just written.

        Scenario: a pre-fix session is resumed today, so its file is fresh while the typed command is from 2020; a
        one-day window must not let it dilute a post-fix comparison.
        """
        _write(
            tmp_path / "proj" / "old12345.jsonl",
            [
                _command_row("2020-01-01T00:00:00Z", "/oss:resolve"),
                _row("m1", ts="2020-01-01T00:00:01Z", output_tokens=10),
            ],
        )
        argv = ["--projects-root", str(tmp_path), "--command", "/oss:resolve", "--since", "1d"]
        assert em.main(argv) == 1

    def test_label_without_slash_shares_the_command_baseline(self, tmp_path: Path) -> None:
        """``--label oss:resolve`` records under the slash label the audit's scan-driven runs read."""
        _write(tmp_path / "proj" / "abc12345.jsonl", [_row("m1", ts="2030-01-01T00:00:00Z", output_tokens=10)])
        ledger = tmp_path / "ledger.jsonl"
        argv = [
            "--projects-root",
            str(tmp_path),
            "--session-id",
            "abc1",
            "--label",
            "oss:resolve",
            "--baseline",
            str(ledger),
            "--record",
        ]
        assert em.main(argv) == 0
        assert em.ledger_labels(ledger) == ["/oss:resolve"]

    def test_report_and_record(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A run on one session prints the report and appends exactly one baseline record."""
        _write(tmp_path / "proj" / "abc12345.jsonl", [_row("m1", ts="2030-01-01T00:00:00Z", output_tokens=10)])
        ledger = tmp_path / "ledger.jsonl"
        argv = ["--projects-root", str(tmp_path), "--session-id", "abc1", "--baseline", str(ledger), "--record"]
        assert em.main(argv) == 0
        assert "## Measured efficiency" in capsys.readouterr().out
        assert len(ledger.read_text(encoding="utf-8").splitlines()) == 1

    def test_no_match_exits_one(self, tmp_path: Path) -> None:
        """An unknown session id exits 1 and writes no ledger."""
        ledger = tmp_path / "ledger.jsonl"
        argv = ["--projects-root", str(tmp_path), "--session-id", "nope", "--baseline", str(ledger), "--record"]
        assert em.main(argv) == 1
        assert not ledger.exists()

    def test_unreadable_transcript_is_skipped_not_fatal(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """One unreadable transcript is skipped with a reason while the others are still measured.

        Scenario: a directory named like a transcript (reading it raises) sits beside a good session that ran the
        command; the report must hold the good session and the run must not abort.
        """
        (tmp_path / "proj" / "bad.jsonl").mkdir(parents=True)
        _write(
            tmp_path / "proj" / "good1234.jsonl",
            [
                _command_row("2030-01-01T00:00:00Z", "/oss:resolve"),
                _row("m1", ts="2030-01-01T00:00:01Z", output_tokens=10),
            ],
        )
        argv = ["--projects-root", str(tmp_path), "--command", "/oss:resolve", "--since", "36500d"]
        assert em.main(argv) == 0
        captured = capsys.readouterr()
        assert "1 session(s)" in captured.out
        assert "skipped bad" in captured.err

    def test_missing_scan_file_exits_four(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """An unreadable scan file is a failed run (exit 4), distinct from "no matching session" (exit 1)."""
        argv = ["--projects-root", str(tmp_path), "--scan-file", str(tmp_path / "absent.txt")]
        assert em.main(argv) == 4
        assert "failed" in capsys.readouterr().err

    def test_default_scope_is_the_current_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Without ``--projects-root`` only the current project's transcripts are read; ``--all-projects`` widens it.

        Scenario: two projects each hold one session that ran the command; a run from the first project must count one
        session, and the widened run two.
        """
        work = tmp_path / "work"
        work.mkdir()
        monkeypatch.chdir(work)
        monkeypatch.setattr(ca, "CLAUDE_PROJECTS_ROOT", str(tmp_path / "projects"))
        monkeypatch.setattr(em, "_repo_root", lambda: work)
        mine = em.project_dir(tmp_path / "projects", work)
        for folder, name in ((mine, "mine1234"), (tmp_path / "projects" / "other", "other123")):
            _write(
                folder / f"{name}.jsonl",
                [
                    _command_row("2030-01-01T00:00:00Z", "/oss:resolve"),
                    _row("m1", ts="2030-01-01T00:00:01Z", output_tokens=10),
                ],
            )
        base = ["--command", "/oss:resolve", "--since", "36500d"]
        assert em.main(base) == 0
        assert "1 session(s)" in capsys.readouterr().out
        assert em.main([*base, "--all-projects"]) == 0
        assert "2 session(s)" in capsys.readouterr().out

    def test_project_dir_name_follows_the_claude_slug(self, tmp_path: Path) -> None:
        """Every character outside letters and digits becomes ``-``, dots and underscores included.

        Real transcript directories are named that way (``…supervision--claude-worktrees-…`` for a ``/.claude/`` path);
        a pattern that only replaced slashes would look in a directory that does not exist.
        """
        name = em.project_dir(tmp_path, tmp_path / "x.y_z").name
        assert name.endswith("-x-y-z")
        assert all(char.isalnum() or char == "-" for char in name)

    def test_worktree_transcripts_belong_to_the_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A session run in one of the project's Claude worktrees counts toward the project, a stranger does not.

        Scenario: the main directory and a ``--claude-worktrees-`` sibling each hold one session; an unrelated project
        holds a third. Worktree sessions are where skills like ``/oss:resolve`` actually run.
        """
        work = tmp_path / "work"
        work.mkdir()
        projects = tmp_path / "projects"
        monkeypatch.setattr(ca, "CLAUDE_PROJECTS_ROOT", str(projects))
        monkeypatch.setattr(em, "_repo_root", lambda: work)
        mine = em.project_dir(projects, work)
        rows = [
            _command_row("2030-01-01T00:00:00Z", "/oss:resolve"),
            _row("m1", ts="2030-01-01T00:00:01Z", output_tokens=10),
        ]
        for folder, name in (
            (mine, "mine1234"),
            (mine.parent / f"{mine.name}--claude-worktrees-wt", "tree1234"),
            (projects / "other", "other123"),
            (mine.parent / f"{mine.name}2", "near1234"),
        ):
            _write(folder / f"{name}.jsonl", rows)
        assert em.main(["--command", "/oss:resolve", "--since", "36500d"]) == 0
        assert "2 session(s)" in capsys.readouterr().out

    def test_missing_project_directory_is_named(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """No transcript directory for the project exits 1 and says so, instead of blaming the time window."""
        monkeypatch.setattr(ca, "CLAUDE_PROJECTS_ROOT", str(tmp_path / "projects"))
        monkeypatch.setattr(em, "_repo_root", lambda: tmp_path)
        assert em.main(["--command", "/oss:resolve", "--since", "36500d"]) == 1
        assert "no transcript directory for this project" in capsys.readouterr().err

    def test_empty_project_scope_points_at_all_projects(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A project with transcripts but no matching session names its scope and the widening flag."""
        monkeypatch.setattr(ca, "CLAUDE_PROJECTS_ROOT", str(tmp_path / "projects"))
        monkeypatch.setattr(em, "_repo_root", lambda: tmp_path)
        _write(
            em.project_dir(tmp_path / "projects", tmp_path) / "s1234567.jsonl", [_row("m", ts="2030-01-01T00:00:00Z")]
        )
        assert em.main(["--command", "/oss:resolve", "--since", "36500d"]) == 1
        assert "--all-projects" in capsys.readouterr().err

    def test_bad_since_is_an_argument_error(self) -> None:
        """An unparsable ``--since`` exits 2 like any other bad argument, not 4."""
        with pytest.raises(SystemExit) as raised:
            em.main(["--command", "/oss:resolve", "--since", "bogus"])
        assert raised.value.code == 2

    def test_later_records_stay_unpinned_and_rebaseline_repins(self, tmp_path: Path) -> None:
        """Only the first record of a label is pinned; ``--rebaseline`` through the CLI moves the pin.

        Scenario: three recorded runs. The second is plain and must not become the comparison point, which would hide
        the saving the baseline exists to show; the third, run with ``--rebaseline``, takes over the pin.
        """
        _write(tmp_path / "proj" / "abc12345.jsonl", [_row("m1", ts="2030-01-01T00:00:00Z", output_tokens=10)])
        ledger = tmp_path / "ledger.jsonl"
        base = [
            "--projects-root",
            str(tmp_path),
            "--session-id",
            "abc1",
            "--label",
            "/x:y",
            "--baseline",
            str(ledger),
            "--record",
        ]
        assert em.main(base) == 0
        assert em.main(base) == 0
        assert [r["baseline"] for r in em._read_records(ledger)] == [True, False]
        assert em.main([*base, "--rebaseline"]) == 0
        records = em._read_records(ledger)
        assert [r["baseline"] for r in records] == [True, False, True]
        assert em.pinned_record(ledger, "/x:y") == records[-1]

    def test_record_stores_its_scope(self, tmp_path: Path) -> None:
        """Every record names the transcripts it covered, so a later run can tell when the populations differ."""
        _write(tmp_path / "proj" / "abc12345.jsonl", [_row("m1", ts="2030-01-01T00:00:00Z", output_tokens=10)])
        ledger = tmp_path / "ledger.jsonl"
        argv = ["--projects-root", str(tmp_path), "--session-id", "abc1", "--baseline", str(ledger), "--record"]
        assert em.main(argv) == 0
        assert em._read_records(ledger)[0]["scope"] == f"root:{tmp_path.resolve()}"

    @pytest.fixture
    def two_projects(self, tmp_path: Path) -> Path:
        """Transcript root with project ``-a`` (one session plus a worktree session), ``-b`` (one) and ``-c`` (none)."""
        rows = [
            _command_row("2030-01-01T00:00:00Z", "/oss:resolve"),
            _row("m1", ts="2030-01-01T00:00:01Z", output_tokens=10),
        ]
        for folder, name in (("-a", "a1234567"), ("-a--claude-worktrees-w", "a2234567"), ("-b", "b1234567")):
            _write(tmp_path / folder / f"{name}.jsonl", rows)
        _write(tmp_path / "-c" / "c1234567.jsonl", [_row("m1", ts="2030-01-01T00:00:01Z", output_tokens=10)])
        return tmp_path

    def test_list_projects_ranks_projects_with_matching_sessions(
        self, two_projects: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The listing names each project whose sessions ran the command, worktrees counted under their project.

        Scenario: ``-a`` has two matching sessions (one in a worktree), ``-b`` one, ``-c`` none; the listing is what the
        audit shows the user before they pick the projects to measure.
        """
        argv = [
            "--projects-root",
            str(two_projects),
            "--command",
            "/oss:resolve",
            "--since",
            "36500d",
            "--list-projects",
        ]
        assert em.main(argv) == 0
        rows = [line.split("\t")[:3] for line in capsys.readouterr().out.splitlines()[1:]]
        assert rows == [["-a", "2", "2030-01-01"], ["-b", "1", "2030-01-01"]]

    def test_list_projects_with_no_match_exits_one(self, tmp_path: Path) -> None:
        """No project with a matching session exits 1 instead of printing an empty table."""
        argv = ["--projects-root", str(tmp_path), "--command", "/oss:resolve", "--since", "36500d", "--list-projects"]
        assert em.main(argv) == 1

    def test_named_projects_are_measured_with_their_worktrees(
        self, two_projects: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``--project`` limits the run to the named projects and their worktrees, and the scope names them.

        Scenario: ``-a`` (plus its worktree) is chosen, ``-b`` is not; the report must count the two ``-a`` sessions.
        """
        ledger = two_projects / "ledger.jsonl"
        argv = [
            *("--projects-root", str(two_projects), "--project=-a", "--command", "/oss:resolve"),
            *("--since", "36500d", "--baseline", str(ledger), "--record"),
        ]
        assert em.main(argv) == 0
        assert "2 session(s)" in capsys.readouterr().out
        assert em._read_records(ledger)[0]["scope"] == "-a"

    def test_projects_file_names_the_projects(self, two_projects: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """``--projects-file`` lists project names one per line, as the audit writes the user's choice."""
        chosen = two_projects / "chosen.txt"
        chosen.write_text("-a\n-b\n", encoding="utf-8")
        argv = [
            *("--projects-root", str(two_projects), "--projects-file", str(chosen)),
            *("--command", "/oss:resolve", "--since", "36500d"),
        ]
        assert em.main(argv) == 0
        assert "3 session(s)" in capsys.readouterr().out

    @pytest.mark.parametrize(
        "name",
        [
            pytest.param("../x", id="parent"),
            pytest.param("a/b", id="separator"),
            pytest.param("..", id="dotdot"),
        ],
    )
    def test_project_name_must_be_a_directory_name(self, name: str, tmp_path: Path) -> None:
        """A project name that is a path is an argument error, so a chosen name cannot reach outside the root."""
        with pytest.raises(SystemExit) as raised:
            em.main(["--projects-root", str(tmp_path), f"--project={name}", "--command", "/oss:resolve"])
        assert raised.value.code == 2

    def test_project_and_all_projects_conflict(self, tmp_path: Path) -> None:
        """Naming projects and asking for all of them is contradictory and rejected with exit 2."""
        with pytest.raises(SystemExit) as raised:
            em.main(["--all-projects", "--project=-a", "--command", "/oss:resolve"])
        assert raised.value.code == 2

    @pytest.mark.parametrize(
        "names",
        [
            pytest.param(["-zz"], id="typo"),
            pytest.param(["-a", "-zz"], id="one-good-one-typo"),
            pytest.param(["-a (Recommended)"], id="label-text-copied"),
        ],
    )
    def test_unknown_project_is_refused(
        self, names: list[str], two_projects: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A name with no transcript directory exits 2 and is named, so it cannot silently change what is measured.

        Scenario: the answer to the project question comes back with a typo, or with the ``(Recommended)`` text of the
        option label; dropping that name would measure another set of projects and record it under the wrong scope.
        """
        argv = ["--projects-root", str(two_projects), "--command", "/oss:resolve", "--since", "36500d"]
        assert em.main([*argv, *(f"--project={name}" for name in names)]) == 2
        assert "unknown project:" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "content",
        [
            pytest.param("", id="empty"),
            pytest.param("\n  \n", id="blank-lines"),
        ],
    )
    def test_projects_file_without_names_is_an_argument_error(self, content: str, two_projects: Path) -> None:
        """A projects file that names nothing exits 2 instead of falling back to the current project."""
        chosen = two_projects / "chosen.txt"
        chosen.write_text(content, encoding="utf-8")
        with pytest.raises(SystemExit) as raised:
            em.main(["--projects-root", str(two_projects), "--projects-file", str(chosen), "--command", "/oss:resolve"])
        assert raised.value.code == 2

    def test_missing_projects_file_is_an_argument_error(self, tmp_path: Path) -> None:
        """A projects file that cannot be read exits 2 like any other bad argument."""
        with pytest.raises(SystemExit) as raised:
            em.main(["--projects-file", str(tmp_path / "absent.txt"), "--command", "/oss:resolve"])
        assert raised.value.code == 2

    def test_several_projects_are_recorded_as_one_scope(self, two_projects: Path) -> None:
        """The scope of a multi-project run names every project, sorted, joined with ``+``."""
        ledger = two_projects / "ledger.jsonl"
        argv = [
            *("--projects-root", str(two_projects), "--project=-b", "--project=-a", "--command", "/oss:resolve"),
            *("--since", "36500d", "--baseline", str(ledger), "--record"),
        ]
        assert em.main(argv) == 0
        assert em._read_records(ledger)[0]["scope"] == "-a+-b"

    def test_project_name_with_a_glob_character_matches_no_other_project(self, tmp_path: Path) -> None:
        """A ``*`` typed under "Other" is a literal name, so it cannot pull in the worktrees of every project."""
        (tmp_path / "-ab--claude-worktrees-x").mkdir()
        assert em.project_family(tmp_path, "-a*") == [tmp_path / "-a*"]

    def test_record_stores_the_border_method(self, tmp_path: Path) -> None:
        """A record says whether border rewrites were counted: for a flagged skill yes, for a ledger-only skill no."""
        _write(
            tmp_path / "proj" / "abc12345.jsonl",
            [
                _command_row("2030-01-01T00:10:00Z", "/p:pinned"),
                _row("p1", ts="2030-01-01T00:10:01Z", output_tokens=1),
                _command_row("2030-01-01T00:20:00Z", "/x:plain"),
                _row("x1", ts="2030-01-01T00:20:01Z", output_tokens=1),
            ],
        )
        ledger = tmp_path / "ledger.jsonl"
        em.append_record(ledger, "/x:plain", {"pre_gate_median_usd": 1.0})
        scan = tmp_path / "scan.txt"
        scan.write_text("INLINE_MODEL: plugins/cc_p/skills/pinned/SKILL.md\n", encoding="utf-8")
        argv = [
            "--projects-root",
            str(tmp_path),
            "--scan-file",
            str(scan),
            "--baseline",
            str(ledger),
            "--since",
            "36500d",
        ]
        assert em.main([*argv, "--record"]) == 0
        recorded = {r["label"]: r["border"] for r in em._read_records(ledger) if "border" in r}
        assert recorded == {"/p:pinned": True, "/x:plain": False}

    def test_project_default_record_stores_the_project_slug(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A run without ``--projects-root`` records the project's directory name as its scope."""
        work = tmp_path / "work"
        work.mkdir()
        projects = tmp_path / "projects"
        monkeypatch.setattr(ca, "CLAUDE_PROJECTS_ROOT", str(projects))
        monkeypatch.setattr(em, "_repo_root", lambda: work)
        mine = em.project_dir(projects, work)
        _write(mine / "abc12345.jsonl", [_row("m1", ts="2030-01-01T00:00:00Z", output_tokens=10)])
        ledger = tmp_path / "ledger.jsonl"
        assert em.main(["--session-id", "abc1", "--baseline", str(ledger), "--record"]) == 0
        assert em._read_records(ledger)[0]["scope"] == mine.name

    def test_scan_run_gives_border_rewrites_only_to_flagged_skills(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """In a scan run a ledger-only skill (its pin removed) owns no border rewrite, a flagged one owns both.

        Scenario: the scan flags ``/p:pinned``; the ledger also holds ``/x:plain``. The session runs the pinned skill
        on Sonnet in an Opus session and then the plain one; only the pinned skill may report switches.
        """
        _write(
            tmp_path / "proj" / "abc12345.jsonl",
            [
                _row("o0", ts="2030-01-01T00:00:00Z", output_tokens=1),
                _command_row("2030-01-01T00:10:00Z", "/p:pinned"),
                _row("p1", ts="2030-01-01T00:10:01Z", model=SONNET, cache_creation_input_tokens=70_000),
                _command_row("2030-01-01T00:20:00Z", "/x:plain"),
                _row("x1", ts="2030-01-01T00:20:01Z", cache_creation_input_tokens=80_000),
                _row("x2", ts="2030-01-01T00:20:02Z", output_tokens=1),
            ],
        )
        ledger = tmp_path / "ledger.jsonl"
        em.append_record(ledger, "/x:plain", {"pre_gate_median_usd": 1.0})
        scan = tmp_path / "scan.txt"
        scan.write_text("INLINE_MODEL: plugins/cc_p/skills/pinned/SKILL.md\n", encoding="utf-8")
        argv = [
            "--projects-root",
            str(tmp_path),
            "--scan-file",
            str(scan),
            "--baseline",
            str(ledger),
            "--since",
            "36500d",
        ]
        assert em.main(argv) == 0
        sections = capsys.readouterr().out.split("## Measured efficiency")[1:]
        counts = {s.split("`")[1]: s.split("; ")[1].split(" ")[0] for s in sections}
        assert counts == {"/p:pinned": "2", "/x:plain": "0"}

    def test_exit_switch_is_labelled_after_the_call(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A revert after the span is listed as "after call N", so it never shares a number with the next run's call
        1."""
        _write(
            tmp_path / "proj" / "abc12345.jsonl",
            [
                _row("o0", ts="2030-01-01T00:00:00Z", output_tokens=1),
                _command_row("2030-01-01T00:10:00Z", "/p:pinned"),
                _row("p1", ts="2030-01-01T00:10:01Z", model=SONNET, cache_creation_input_tokens=70_000),
                _command_row("2030-01-01T00:20:00Z", "/x:plain"),
                _row("x1", ts="2030-01-01T00:20:01Z", cache_creation_input_tokens=80_000),
            ],
        )
        argv = ["--projects-root", str(tmp_path), "--command", "/p:pinned", "--since", "36500d"]
        assert em.main(argv) == 0
        out = capsys.readouterr().out
        assert "after call 1:" in out
        assert "call 1: " in out

    @pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
    def test_repo_root_inside_a_worktree_is_the_main_checkout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """From inside a linked worktree the project is the main checkout, whose transcripts hold the sessions.

        A worktree's own top level has a transcript directory of its own; anchoring on it would hide the project.
        """
        main = tmp_path / "repo"
        main.mkdir()
        git = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-C", str(main)]
        subprocess.run([*git, "init", "-q"], check=True)
        subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "x"], check=True)
        tree = tmp_path / "tree"
        subprocess.run([*git, "worktree", "add", "-q", str(tree), "-b", "wt"], check=True)
        monkeypatch.chdir(tree)
        assert em._repo_root().resolve() == main.resolve()

    def test_selector_required(self) -> None:
        """Without --session-id or --command the parser rejects the call with exit 2."""
        with pytest.raises(SystemExit) as raised:
            em.main([])
        assert raised.value.code == 2
