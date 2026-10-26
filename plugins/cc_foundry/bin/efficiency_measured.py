#!/usr/bin/env python
"""efficiency_measured.py — measured cost checks over Claude Code transcripts, for ``/foundry:audit --efficiency``.

The static efficiency scan (``scan_efficiency_signals.py``) can say a skill *might* waste money; this module reads real
transcripts and says what it *did* cost. It reuses the pricing and row validation of ``cost_analyzer.py`` and adds the
timestamp and tool-sequence view that module drops when it deduplicates by message id.

Checks, ids as in the efficiency audit plan. Call numbers in main-chain findings are 1-based positions inside the
analysed span, the same numbering ``--timeline`` prints (a subagent finding counts that subagent's own calls; an exit
switch sits one past the last call it follows; the timeline stops at the first question):

``R1`` model-switch rewrite
    A main-chain call whose model differs from the previous call and writes at least ``REWRITE_MIN_TOKENS`` cache
    tokens: the prompt prefix was re-cached under the new model. Reported with the dollars of that write.
``R2`` cold start
    First call's cache write and cost: the fixed prefix every run pays before doing work.
``R3`` pre-gate cost
    Main plus subagent spend from the span start to the first ``AskUserQuestion``.
``R4`` subagent floor and growth
    Start context, end context, call count and cost per subagent transcript; flags counts above ``SUBAGENT_CALL_CAP``.
``R5`` deterministic prefix
    A subagent whose first ``LITERAL_PREFIX_MIN`` or more calls are nothing but ``gh``/``git`` Bash commands: a fetch a
    script could do without a model. Leading ``VAR=value``, ``env`` and ``cd dir &&`` words are looked through.
``R7`` idle-gap cache loss
    A same-model gap above ``IDLE_GAP_S`` seconds (``SUBAGENT_IDLE_S`` for subagents, whose writes expire after five
    minutes) followed by a call that rewrites at least ``REWRITE_MIN_TOKENS`` cache tokens while reading back under
    ``READ_SHARE`` of the previous prompt: the cache expired. A call that still reads most of the prefix is growth, and
    a call whose prompt shrinks below ``COMPACTION_SHARE`` of the previous one is a compaction; neither is a loss.
``R9`` baseline and delta
    ``--record`` appends the run's summary to a JSONL ledger. The first record of a label, or the latest one written
    with ``--rebaseline``, is its pinned baseline; every run prints the change against that baseline and, when
    different, against the previous run, plus how many of its sessions the baseline already held.

Attribution: with a slash command (``--command`` or a scan-driven label) a session is measured over the spans that
start at each typed invocation of it (at or after ``--since``) and end at the next different command, never from the
session start; built-in commands such as ``/compact`` or ``/model`` do not end a span. A session that never ran the
command is measured whole. Sessions are selected by the timestamp of the command row, not by file modification time. A
model switch on the call just before a span's first call counts as its entry, and the call just after it counts as the
exit when it returns to the model the entry left (a mid-session pin shows both rewrites); a different model there is the
next command's own switch and is measured with that command.

Known limits: a user-typed ``/model`` inside a skill's span shows up as that skill's switch; the built-in command list
is a denylist, so a new built-in ends a span until it is added; a session's later runs of a skill share the first run's
cold start and pre-gate window; a revert after a chain of back-to-back pinned skills belongs to none of them, and when
a skill returns to the model its flagged neighbour entered, both skills count that one rewrite; a session whose file
was last written before the window is not read even when it holds a command typed inside it (a restored copy); a
``--command`` run counts the border rewrites of its skill and a scan run only those of flagged skills, so each record
stores ``border`` and a run that counts the other way gets a warning.

Not measured, or measured more coarsely than the audit plan asked, and stated in every report: ``R6`` tier fit and
``R8`` tool-output bloat need thresholds the first measured runs have not supplied; ``R2`` is the first call's write,
not split into instructions, skill listing and hooks; ``R5`` checks the leading run of literal calls, not the commands
a spawn prompt lists; findings carry dollars but no confidence score or static-finding link; an "open since" date is
not tracked.

Reports carry ids, counts, token numbers and dollars only, never message text — transcripts hold user content and a
report may be shared. Dollars are public list rates, not a billing statement. By default only the current project's
transcripts (and its Claude worktrees) under ``~/.claude/projects`` are read; ``--list-projects`` shows the projects
whose sessions ran the command, ``--project=-NAME`` / ``--projects-file`` pick projects by transcript directory name
(they start with a dash, hence the ``=``), and ``--all-projects`` reads every project.

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/efficiency_measured.py" --session-id ef15e184 --session-id 85d8c3b6 \\
        --label /oss:resolve --record --output report.md
    python "${CLAUDE_PLUGIN_ROOT}/bin/efficiency_measured.py" --command /oss:resolve --since 14d
    python "${CLAUDE_PLUGIN_ROOT}/bin/efficiency_measured.py" --session-id ef15e184 --timeline

``--rebaseline`` with ``--scan-file`` re-pins every label of that run at once; an unreadable transcript is counted once
per label in the skipped total.

Exit codes:
    0   report produced
    1   no matching session with usage
    2   argument error, including an unparsable ``--since``
    4   the run failed (unreadable scan file, ledger or transcript root); stderr names the cause
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import re
import shutil
import statistics
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import pairwise
from pathlib import Path, PurePosixPath

import cost_analyzer as ca

#: Gap between consecutive main-chain calls, in seconds, that counts as the session sitting idle.
IDLE_GAP_S = 120
#: Same, for subagent calls: their cache writes expire after five minutes.
SUBAGENT_IDLE_S = 300
#: Cache-write size, in tokens, below which a call is ordinary growth and not a prefix rewrite.
REWRITE_MIN_TOKENS = 50_000
#: Share of the previous prompt a call may still read from cache before its write counts as growth, not loss.
READ_SHARE = 0.5
#: Share of the previous prompt below which a call is a compaction, not an idle rewrite.
COMPACTION_SHARE = 0.7
#: Subagent call count above which the agent risks stalling without returning its envelope.
SUBAGENT_CALL_CAP = 55
#: Leading literal-CLI calls in a subagent at which the fetch is a script candidate.
LITERAL_PREFIX_MIN = 5
#: Tool whose first use closes the pre-gate window.
GATE_TOOL = "AskUserQuestion"
#: Bash first words that make a call a literal fetch.
LITERAL_CLI = frozenset({"gh", "git"})
#: Built-in slash commands typed in the middle of a skill run; they do not end that skill's span.
BUILTIN_COMMANDS = frozenset(
    "/autocompact /compact /model /context /cost /usage /stats /help /status /config /mcp /permissions /agents /memory /doctor "
    "/fast /effort /theme /vim /ide /export /copy /rewind /todos /tasks /hooks /plugin /plugins /skills /resume".split()
)
#: Words that precede the real command without changing it.
_PREFIX_WORDS = frozenset({"env", "command", "builtin"})
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=\S*")
#: Version directory of an installed plugin cache path (``0.55.0``), between the plugin name and ``skills``.
_VERSION_DIR = re.compile(r"\d+(?:\.\d+)+")
#: Rows kept per ranked list in a report; the consolidator reads the report, so unranked tails would drown it.
TOP_N = 5
#: Ledger written by ``--record``; ``.notes/`` is the manual-TTL artifact root, so history outlives the 30-day sweeps.
DEFAULT_BASELINE = ".notes/efficiency/baselines.jsonl"
#: Errors that a single unreadable or odd transcript may raise; it is skipped, never fatal.
_SESSION_ERRORS = (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError)


@dataclass
class MCall:
    """One assistant API call with the fields the measured checks need.

    Attributes:
        message_id: Transcript message id the content-block rows were merged on.
        ts: Epoch seconds of the first row, or ``0.0`` when the row carried no usable timestamp.
        model: Model id as recorded.
        usage: Usage counters with null values dropped.
        tools: Tool names of every ``tool_use`` block, in order.
        cli: First word of each Bash command, in order — never the command text.
    """

    message_id: str
    ts: float
    model: str
    usage: dict
    tools: list[str] = field(default_factory=list)
    cli: list[str] = field(default_factory=list)

    @property
    def cost_usd(self) -> float:
        """List-price cost of this call."""
        return ca.cost(self.usage, self.model)

    @property
    def cache_write(self) -> int:
        """Cache tokens written by this call."""
        return self.usage.get("cache_creation_input_tokens", 0)

    @property
    def cache_read(self) -> int:
        """Cache tokens read by this call."""
        return self.usage.get("cache_read_input_tokens", 0)

    @property
    def context(self) -> int:
        """Prompt size of this call."""
        return ca.prompt_tokens(self.usage)

    @property
    def write_cost_usd(self) -> float:
        """Dollars of the cache-write part of this call alone, priced with the 5-minute and 1-hour multipliers.

        Examples:
            >>> call = MCall("m", 0.0, "claude-opus-5-5", {"cache_creation_input_tokens": 1_000_000})
            >>> round(call.write_cost_usd, 2)
            5.0
            >>> split = {"ephemeral_1h_input_tokens": 1_000_000}
            >>> hour = {"cache_creation_input_tokens": 1_000_000, "cache_creation": split}
            >>> round(MCall("m", 0.0, "claude-opus-5-5", hour).write_cost_usd, 2)
            8.0
        """
        rate = ca.rate_for(self.model, self.context)
        write_5m, write_1h = ca.cache_write_split(self.usage)
        weighted = write_5m * ca.CACHE_WRITE_5M_MULTIPLIER + write_1h * ca.CACHE_WRITE_1H_MULTIPLIER
        return weighted * rate.input / 1_000_000


@dataclass
class Transcript:
    """Calls of one transcript file in first-appearance order, plus the slash commands the user typed.

    Attributes:
        calls: Merged assistant calls that carry usage.
        commands: Count of each slash command typed by the user.
        command_rows: ``(epoch seconds, command)`` of every typed command, in file order; ``0.0`` when untimed.
    """

    calls: list[MCall]
    commands: Counter
    command_rows: list[tuple[float, str]] = field(default_factory=list)


@dataclass
class Switch:
    """R1 finding: a model change that re-cached the prompt prefix."""

    index: int
    previous: str
    model: str
    cache_write: int
    cost_usd: float
    exit: bool = False


@dataclass
class IdleLoss:
    """R7 finding: an idle gap after which the same model had to rewrite its cache."""

    index: int
    gap_s: float
    cache_write: int
    cost_usd: float
    where: str = "main"


@dataclass
class AgentStats:
    """R4/R5 row for one subagent transcript."""

    agent_type: str
    calls: int
    start_context: int
    end_context: int
    cost_usd: float
    literal_prefix: int

    @property
    def flags(self) -> list[str]:
        """Names of the thresholds this agent crosses."""
        found = []
        if self.calls > SUBAGENT_CALL_CAP:
            found.append(f"calls>{SUBAGENT_CALL_CAP}")
        if self.literal_prefix >= LITERAL_PREFIX_MIN:
            found.append(f"literal-cli-prefix:{self.literal_prefix}")
        return found


@dataclass
class SessionMetrics:
    """All measured results for one session."""

    session: str
    calls: int
    gate_found: bool
    cold_write: int
    cold_cost_usd: float
    pre_gate_main_usd: float
    pre_gate_sub_usd: float
    switches: list[Switch]
    idle_losses: list[IdleLoss]
    agents: list[AgentStats]

    @property
    def pre_gate_usd(self) -> float:
        """Main plus subagent spend before the first question."""
        return self.pre_gate_main_usd + self.pre_gate_sub_usd


def _clean_usage(raw: object) -> dict:
    """Drop null counters, keeping the nested cache-write split clean too."""
    if not isinstance(raw, dict):
        return {}
    usage = {key: value for key, value in raw.items() if value is not None}
    if isinstance(usage.get("cache_creation"), dict):
        usage["cache_creation"] = {key: value for key, value in usage["cache_creation"].items() if value is not None}
    return usage


def _row_ts(row: dict) -> float:
    """Epoch seconds of a row, or ``0.0`` when the timestamp is absent or malformed."""
    raw = row.get("timestamp")
    if not isinstance(raw, str):
        return 0.0
    try:
        return ca.parse_ts(raw)
    except ValueError:
        return 0.0


def _first_word(block: dict) -> str | None:
    """First real word of a Bash block's command, or ``None`` for any other tool.

    Leading ``VAR=value`` assignments, ``env`` and a ``cd dir &&`` prefix are looked through so ``cd repo && gh ...``
    still reads as ``gh``. A block whose ``input`` is not an object reads as ``?``.

    Examples:
        >>> _first_word({"name": "Bash", "input": {"command": "cd repo && GH_PAGER= gh pr view 1"}})
        'gh'
        >>> _first_word({"name": "Bash", "input": "oops"})
        '?'
        >>> _first_word({"name": "Read"}) is None
        True
    """
    if block.get("name") != "Bash":
        return None
    tool_input = block.get("input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    words = command.split() if isinstance(command, str) else []
    if words[:1] == ["cd"] and "&&" in words:
        words = words[words.index("&&") + 1 :]
    while words and (_ASSIGNMENT.fullmatch(words[0]) or words[0] in _PREFIX_WORDS):
        words = words[1:]
    return words[0] if words else "?"


def _tool_blocks(message: dict) -> tuple[list[str], list[str]]:
    """Return ``(tool names, Bash first words)`` of one row's ``tool_use`` blocks."""
    names: list[str] = []
    words: list[str] = []
    for block in message.get("content") or []:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        names.append(str(block.get("name", "?")))
        word = _first_word(block)
        if word is not None:
            words.append(word)
    return names, words


def _parse_line(line: str) -> dict | None:
    """Parse one JSONL line into a row object, or ``None`` when it is not one."""
    try:
        row = json.loads(line)
    except (ValueError, RecursionError):
        return None
    return row if isinstance(row, dict) else None


def _command_names(row: dict) -> list[str]:
    """Slash commands a user row types; only a row whose text opens with a ``<command-`` tag counts.

    Examples:
        >>> typed = {"type": "user", "message": {"content": "<command-name>/x:y</command-name>"}}
        >>> _command_names(typed)
        ['/x:y']
        >>> _command_names({"type": "assistant", "message": {"content": "<command-name>/x:y</command-name>"}})
        []
    """
    if row.get("type") != "user":
        return []
    message = row.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        content = "".join(b["text"] for b in content if isinstance(b, dict) and isinstance(b.get("text"), str))
    if not isinstance(content, str) or not content.lstrip().startswith("<command-"):
        return []
    return ca.COMMAND_RE.findall(content)


def _fold_row(row: dict, by_id: dict[str, MCall], order: list[str]) -> None:
    """Merge one content-block row into its message's :class:`MCall`.

    A message is written as several rows, one per content block, so tool names live on rows the usage dedupe of
    ``cost_analyzer`` throws away. A row without a message id gets a numbered stand-in, as ``cost_analyzer`` does.
    """
    message = row.get("message") or {}
    message_id = message.get("id") or f"anon-{len(by_id)}"
    call = by_id.get(message_id)
    if call is None:
        call = by_id[message_id] = MCall(message_id, _row_ts(row), message.get("model") or "unknown", {})
        order.append(message_id)
    usage = _clean_usage(message.get("usage"))
    if usage and not call.usage:
        call.usage = usage
        call.model = message.get("model") or call.model
    names, words = _tool_blocks(message)
    call.tools += names
    call.cli += words


def parse_transcript(path: Path) -> Transcript:
    """Read one transcript file into merged calls; calls without usage are dropped.

    Examples:
        >>> from tempfile import TemporaryDirectory
        >>> with TemporaryDirectory() as directory:
        ...     empty = Path(directory) / "s.jsonl"
        ...     _ = empty.write_text("")
        ...     parse_transcript(empty).calls
        []
    """
    by_id: dict[str, MCall] = {}
    order: list[str] = []
    commands: Counter = Counter()
    command_rows: list[tuple[float, str]] = []
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            row = _parse_line(line)
            if row is None:
                continue
            for name in _command_names(row):
                commands[name] += 1
                command_rows.append((_row_ts(row), name))
            if ca.row_shape_error(row) is None:
                _fold_row(row, by_id, order)
    return Transcript([by_id[key] for key in order if by_id[key].usage], commands, command_rows)


def _runs(calls: list[MCall], position: dict[int, int]) -> list[tuple[int, int]]:
    """Return the first and last index of each stretch of consecutive ``calls`` that lie in ``position``.

    Examples:
        >>> calls = [MCall(name, 0.0, "m", {}) for name in "abcde"]
        >>> _runs(calls, {id(calls[1]): 1, id(calls[2]): 2, id(calls[4]): 3})
        [(1, 2), (4, 4)]
    """
    runs: list[tuple[int, int]] = []
    for index, call in enumerate(calls):
        if id(call) not in position:
            continue
        if runs and runs[-1][1] == index - 1:
            runs[-1] = (runs[-1][0], index)
        else:
            runs.append((index, index))
    return runs


def model_switches(
    calls: list[MCall],
    min_write: int = REWRITE_MIN_TOKENS,
    span: list[MCall] | None = None,
    edges: bool = True,
) -> list[Switch]:
    """R1: calls that changed model and re-cached at least ``min_write`` tokens.

    ``calls`` is the whole main chain and ``span`` the part being measured (default: all of it). A switch counts when
    its call is in the span. With ``edges`` the entry switch (index 1) counts too, and so does the call just after the
    span (flagged ``exit``, indexed one past the call before it) when it returns to a model the entry left: that is the
    pin reverting. Without ``edges`` only switches between two calls of the span count, because a skill with no pin of
    its own does not own the rewrite at its border; that rewrite belongs to the neighbouring skill that set the model.

    Examples:
        >>> opus = MCall("a", 0.0, "claude-opus-5-5", {"cache_creation_input_tokens": 10})
        >>> sonnet = MCall("b", 0.0, "claude-sonnet-5-5", {"cache_creation_input_tokens": 90_000})
        >>> [s.index for s in model_switches([opus, sonnet])]
        [2]
        >>> [s.index for s in model_switches([opus, sonnet], span=[sonnet])]
        [1]
        >>> [s.index for s in model_switches([opus, sonnet], span=[sonnet], edges=False)]
        []
        >>> back = MCall("c", 0.0, "claude-opus-5-5", {"cache_creation_input_tokens": 90_000})
        >>> [(s.index, s.exit) for s in model_switches([opus, sonnet, back], span=[sonnet])]
        [(1, False), (2, True)]
    """
    position = {id(call): number for number, call in enumerate(calls if span is None else span, start=1)}
    rewrites = [
        (previous, call)
        for previous, call in pairwise(calls)
        if call.model != previous.model and call.cache_write >= min_write
    ]
    inside = [
        (position[id(call)], previous, call, False)
        for previous, call in rewrites
        if id(call) in position and (edges or id(previous) in position)
    ]
    after = [
        (position[id(calls[last])] + 1, calls[last], calls[last + 1], True)
        for first, last in (_runs(calls, position) if edges else [])
        if last + 1 < len(calls)
        and first > 0
        and calls[first - 1].model != calls[first].model
        and calls[last + 1].model == calls[first - 1].model
        and calls[last + 1].model != calls[last].model
        and calls[last + 1].cache_write >= min_write
    ]
    return [
        Switch(index, previous.model, call.model, call.cache_write, call.write_cost_usd, leaves)
        for index, previous, call, leaves in sorted(inside + after, key=lambda row: row[0])
    ]


def idle_losses(
    calls: list[MCall], min_write: int = REWRITE_MIN_TOKENS, gap_s: float = IDLE_GAP_S, where: str = "main"
) -> list[IdleLoss]:
    """R7: same-model idle gaps followed by a large cache rewrite that read little of the old prefix back.

    Examples:
        >>> first = MCall("a", 1000.0, "claude-opus-5-5", {})
        >>> second = MCall("b", 1500.0, "claude-opus-5-5", {"cache_creation_input_tokens": 80_000})
        >>> [round(loss.gap_s) for loss in idle_losses([first, second])]
        [500]
        >>> read = {"cache_creation_input_tokens": 80_000, "cache_read_input_tokens": 200_000}
        >>> grown = MCall("c", 1500.0, "claude-opus-5-5", read)
        >>> big = MCall("a", 1000.0, "claude-opus-5-5", {"cache_read_input_tokens": 200_000})
        >>> idle_losses([big, grown])
        []
    """
    found = []
    for index, (previous, call) in enumerate(pairwise(calls), start=2):
        if not (previous.ts and call.ts) or call.model != previous.model:
            continue
        gap = call.ts - previous.ts
        if gap <= gap_s or call.cache_write < min_write:
            continue
        if previous.context and (
            call.cache_read >= previous.context * READ_SHARE or call.context < previous.context * COMPACTION_SHARE
        ):
            continue
        found.append(IdleLoss(index, gap, call.cache_write, call.write_cost_usd, where))
    return found


def gate_index(calls: list[MCall]) -> int | None:
    """Index of the first call that asks the user a question, or ``None`` when none does."""
    return next((index for index, call in enumerate(calls) if GATE_TOOL in call.tools), None)


def literal_prefix(calls: list[MCall]) -> int:
    """R5: length of the leading run of calls that only run literal ``gh``/``git`` commands.

    Examples:
        >>> fetch = MCall("a", 0.0, "m", {}, tools=["Bash"], cli=["gh"])
        >>> think = MCall("b", 0.0, "m", {}, tools=["Read"])
        >>> literal_prefix([fetch, fetch, think, fetch])
        2
    """
    run = 0
    for call in calls:
        literal = bool(call.cli) and set(call.tools) == {"Bash"} and set(call.cli) <= LITERAL_CLI
        if not literal:
            break
        run += 1
    return run


def _agent_type(path: Path) -> str:
    """Subagent type from the sibling ``meta.json``, or ``?``."""
    try:
        meta = json.loads((path.parent / f"{path.stem}.meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return "?"
    value = meta.get("agentType") if isinstance(meta, dict) else None
    return value if isinstance(value, str) and value else "?"


def _agent_files(session_path: Path) -> list[Path]:
    """Subagent transcript files that belong to one session transcript."""
    directory = session_path.parent / session_path.stem / "subagents"
    return sorted(directory.glob("agent-*.jsonl")) if directory.is_dir() else []


Spans = list[tuple[float, float]]
#: The whole session: no command selected, or the command was never typed in the period.
WHOLE_SESSION: Spans = [(0.0, math.inf)]


def _in_spans(ts: float, spans: Spans) -> bool:
    """Whether a timestamp lies in any ``[start, end)`` span; an untimed call is kept."""
    return not ts or any(start <= ts < end for start, end in spans)


def command_spans(rows: list[tuple[float, str]], command: str | None, since_ts: float = 0.0) -> Spans:
    """Spans of every typed ``command`` at or after ``since_ts``, each up to the next different non-built-in command.

    Without a command, or when the command was never typed in that period, the span is the whole session. Invocations
    with no other command between them share one end, so their spans overlap and cover the same calls once.

    Examples:
        >>> rows = [(10.0, "/a"), (15.0, "/compact"), (20.0, "/b"), (30.0, "/a")]
        >>> command_spans(rows, "/a")
        [(10.0, 20.0), (30.0, inf)]
        >>> command_spans(rows, "/a", since_ts=25.0)
        [(30.0, inf)]
        >>> command_spans(rows, None)
        [(0.0, inf)]
    """
    starts = [ts for ts, name in rows if command and name == command and ts and ts >= since_ts]
    if not starts:
        return WHOLE_SESSION
    return [
        (
            start,
            min(
                (ts for ts, name in rows if name != command and name not in BUILTIN_COMMANDS and ts > start),
                default=math.inf,
            ),
        )
        for start in starts
    ]


def agent_stats(path: Path, spans: Spans = WHOLE_SESSION) -> tuple[AgentStats, list[MCall]]:
    """R4/R5 row for one subagent transcript, plus its calls inside ``spans`` for the pre-gate window."""
    calls = [call for call in parse_transcript(path).calls if _in_spans(call.ts, spans)]
    stats = AgentStats(
        agent_type=_agent_type(path),
        calls=len(calls),
        start_context=calls[0].context if calls else 0,
        end_context=calls[-1].context if calls else 0,
        cost_usd=sum(call.cost_usd for call in calls),
        literal_prefix=literal_prefix(calls),
    )
    return stats, calls


def _span_calls(transcript: Transcript, command: str | None, since_ts: float) -> tuple[list[MCall], Spans]:
    """Main-chain calls inside the command's spans, with the spans."""
    spans = command_spans(transcript.command_rows, command, since_ts)
    return [call for call in transcript.calls if _in_spans(call.ts, spans)], spans


def analyse_session(
    path: Path, command: str | None = None, since_ts: float = 0.0, edges: bool = True
) -> SessionMetrics | None:
    """Run every measured check on one session; ``None`` when it holds no usage in the span.

    With ``command`` the session is measured over that command's spans (see ``command_spans``); otherwise whole.
    ``edges`` says whether the command owns the model rewrites at the border of its spans (see ``model_switches``). A
    session's later runs of the command join the first in the main-chain checks; cold start and the pre-gate window
    describe the first run, and idle gaps are paired only between calls that are neighbours in the transcript.
    """
    transcript = parse_transcript(path)
    calls, spans = _span_calls(transcript, command, since_ts)
    if not calls:
        return None
    gate = gate_index(calls)
    window = calls if gate is None else calls[: gate + 1]
    cutoff = max((call.ts for call in window), default=0.0) if window[-1].ts else 0.0
    bounded = spans != WHOLE_SESSION
    agents: list[AgentStats] = []
    adjacent = {(id(a), id(b)) for a, b in pairwise(transcript.calls)}
    losses = [loss for loss in idle_losses(calls) if (id(calls[loss.index - 2]), id(calls[loss.index - 1])) in adjacent]
    sub_before_gate = 0.0
    for agent_path in _agent_files(path):
        stats, agent_calls = agent_stats(agent_path, spans)
        if bounded and not agent_calls:
            continue
        agents.append(stats)
        sub_before_gate += sum(
            call.cost_usd for call in agent_calls if gate is None or not cutoff or not call.ts or call.ts <= cutoff
        )
        losses += idle_losses(agent_calls, gap_s=SUBAGENT_IDLE_S, where=stats.agent_type)
    return SessionMetrics(
        session=path.stem,
        calls=len(calls),
        gate_found=gate is not None,
        cold_write=calls[0].cache_write,
        cold_cost_usd=calls[0].cost_usd,
        pre_gate_main_usd=sum(call.cost_usd for call in window),
        pre_gate_sub_usd=sub_before_gate,
        switches=model_switches(transcript.calls, span=calls, edges=edges),
        idle_losses=losses,
        agents=agents,
    )


def timeline(path: Path, command: str | None = None, since_ts: float = 0.0) -> list[str]:
    """Per-call table up to and including the first question: time, model, cache split, cost, running total, tools."""
    calls, _ = _span_calls(parse_transcript(path), command, since_ts)
    gate = gate_index(calls)
    shown = calls if gate is None else calls[: gate + 1]
    lines = [
        f"### Timeline `{path.stem}`",
        "",
        "| # | time (UTC) | model | cache_w | cache_r | out | $ | running $ | tools |",
        "|--:|---|---|--:|--:|--:|--:|--:|---|",
    ]
    running = 0.0
    for number, call in enumerate(shown, start=1):
        running += call.cost_usd
        clock = datetime.fromtimestamp(call.ts, timezone.utc).strftime("%H:%M:%S") if call.ts else "?"
        tools = ", ".join(call.tools) or "—"
        lines.append(
            f"| {number} | {clock} | {call.model.removeprefix('claude-')} | {call.cache_write} | {call.cache_read} "
            f"| {call.usage.get('output_tokens', 0)} | {call.cost_usd:.2f} | {running:.2f} | {tools} |"
        )
    return [*lines, ""]


def summarise(metrics: list[SessionMetrics]) -> dict:
    """Numbers stored in the baseline ledger and compared on the next run."""
    return {
        "sessions": len(metrics),
        "pre_gate_median_usd": round(statistics.median(m.pre_gate_usd for m in metrics), 2),
        "cold_start_median_usd": round(statistics.median(m.cold_cost_usd for m in metrics), 2),
        "switch_count": sum(len(m.switches) for m in metrics),
        "switch_usd": round(sum(s.cost_usd for m in metrics for s in m.switches), 2),
        "idle_loss_usd": round(sum(i.cost_usd for m in metrics for i in m.idle_losses), 2),
        "flagged_agents": sum(bool(a.flags) for m in metrics for a in m.agents),
    }


def _read_records(ledger: Path) -> list[dict]:
    """Every readable record of a ledger, oldest first; a missing ledger or an unreadable line adds nothing."""
    try:
        lines = ledger.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records = []
    for line in lines:
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def label_records(ledger: Path, label: str) -> list[dict]:
    """Ledger records of one label, oldest first."""
    return [record for record in _read_records(ledger) if record.get("label") == label]


def last_record(ledger: Path, label: str) -> dict | None:
    """Most recent ledger record with this label, or ``None`` when the ledger or label is absent."""
    records = label_records(ledger, label)
    return records[-1] if records else None


def pinned_record(ledger: Path, label: str) -> dict | None:
    """Return the label's baseline: the latest record marked ``baseline``, else its oldest record, else ``None``."""
    records = label_records(ledger, label)
    flagged = [record for record in records if record.get("baseline")]
    return flagged[-1] if flagged else (records[0] if records else None)


def ledger_labels(ledger: Path) -> list[str]:
    """Slash-command labels in the ledger, in first-seen order, so a fixed skill keeps being compared.

    A skill whose pin was removed no longer appears in the scan, so scan-driven selection alone would stop measuring
    exactly the skill whose before-and-after delta the baseline exists to show.

    Examples:
        >>> from tempfile import TemporaryDirectory
        >>> with TemporaryDirectory() as directory:
        ...     book = Path(directory) / "b.jsonl"
        ...     _ = book.write_text('{"label": "/oss:resolve"}\\n{"label": "default"}\\n{"label": "/oss:resolve"}\\n')
        ...     ledger_labels(book)
        ['/oss:resolve']
    """
    labels = (record.get("label") for record in _read_records(ledger))
    return list(dict.fromkeys(label for label in labels if isinstance(label, str) and label.startswith("/")))


def normalise_label(label: str) -> str:
    """Slash form of a skill label, so ``oss:resolve`` and ``/oss:resolve`` share one baseline; ``default`` stays.

    Examples:
        >>> normalise_label("oss:resolve"), normalise_label("/oss:resolve"), normalise_label("default")
        ('/oss:resolve', '/oss:resolve', 'default')
    """
    return label if label == "default" or label.startswith("/") else f"/{label}"


def append_record(ledger: Path, label: str, summary: dict, extra: dict | None = None) -> None:
    """Append one record to the baseline ledger, creating its directory and restoring a missing final newline."""
    ledger.parent.mkdir(parents=True, exist_ok=True)
    try:
        with ledger.open("rb") as existing:
            existing.seek(-1, 2)
            joiner = "" if existing.read(1) == b"\n" else "\n"
    except OSError:
        joiner = ""
    record = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "label": label,
        **summary,
        **(extra or {}),
    }
    with ledger.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(joiner + json.dumps(record, sort_keys=True) + "\n")


def delta_lines(previous: dict | None, summary: dict, kind: str = "record") -> list[str]:
    """R9 table of the change against one earlier record; ``kind`` names it in the heading."""
    if previous is None:
        return ["No earlier record with this label — this run is the baseline.", ""]
    lines = [
        f"Against the {kind} of {previous.get('ts', '?')}:",
        "",
        "| metric | before | now | change |",
        "|---|--:|--:|--:|",
    ]
    for key, now in summary.items():
        before = previous.get(key)
        if isinstance(before, (int, float)) and not isinstance(before, bool):
            lines.append(f"| {key} | {before} | {now} | {now - before:+.2f} |")
    return [*lines, ""]


def _seconds(window: object) -> object:
    """Window length in seconds, so ``30d`` and ``720h`` match; text that does not parse stays as it is.

    Examples:
        >>> _seconds("30d") == _seconds("720h"), _seconds("not recorded")
        (True, 'not recorded')
    """
    try:
        return ca.parse_since(str(window))
    except ValueError:
        return window


def _window_warning(record: dict, kind: str, scope: str, since: str, border: bool = True) -> list[str]:
    """Warning lines when an earlier record covered other transcripts or counted switches differently than this run.

    ``border`` says whether this run counted the model rewrites at the border of a skill's spans.

    Examples:
        >>> _window_warning({"scope": "all", "since": "30d", "border": True}, "pinned baseline", "all", "30d")
        []
        >>> _window_warning({"since": "30d", "border": True}, "pinned baseline", "all", "30d")[0][:21]
        '⚠ The pinned baseline'
        >>> old = {"scope": "all", "since": "30d"}
        >>> "border rewrites: not recorded" in _window_warning(old, "run", "all", "30d")[0]
        True
    """
    lines = []
    before = (record.get("scope", "not recorded"), record.get("since", "not recorded"))
    if before[0] != scope or _seconds(before[1]) != _seconds(since):
        lines += [
            f"⚠ The {kind} covered scope `{before[0]}`, window `{before[1]}`; this run covers scope `{scope}`, window "
            f"`{since}`. The populations differ, so read the change with care.",
            "",
        ]
    method = record.get("border")
    if method != border:
        shown = "not recorded" if method is None else ("yes" if method else "no")
        lines += [
            f"⚠ The {kind} counted border rewrites: {shown}; this run: {'yes' if border else 'no'}. The methods "
            "differ, so switch counts are not comparable; after a pin is removed, start --since at the fix.",
            "",
        ]
    return lines


def _delta_section(
    ledger: Path,
    label: str,
    summary: dict,
    session_ids: list[str],
    scope: str = "all",
    since: str = "14d",
    border: bool = True,
) -> list[str]:
    """Change against the pinned baseline and, when it differs, against the previous run, with their session overlap."""
    pinned, last = pinned_record(ledger, label), last_record(ledger, label)
    if pinned is None:
        return delta_lines(None, summary)
    lines = [
        *_window_warning(pinned, "pinned baseline", scope, since, border),
        *delta_lines(pinned, summary, "pinned baseline"),
    ]
    held = pinned.get("session_ids")
    if isinstance(held, list) and held:
        shared = len(set(held) & set(session_ids))
        lines += [f"{shared} of this run's {len(session_ids)} session(s) were already in that baseline.", ""]
    if last is not None and last != pinned:
        lines += [
            *_window_warning(last, "previous run", scope, since, border),
            *delta_lines(last, summary, "previous run"),
        ]
    return lines


def _capped(lines: list[str], top_n: int, empty: str = "None found.") -> list[str]:
    """First ``top_n`` lines of an already ranked list, a count of the rest, and a closing blank line.

    Examples:
        >>> _capped(["a", "b", "c"], 2)
        ['a', 'b', '(+1 more, ranked below the cut)', '']
        >>> _capped([], 2)
        ['None found.', '']
    """
    if not lines:
        return [empty, ""]
    more = [f"(+{len(lines) - top_n} more, ranked below the cut)"] if len(lines) > top_n else []
    return [*lines[:top_n], *more, ""]


def _session_rows(metrics: list[SessionMetrics], top_n: int) -> list[str]:
    """R2/R3 table of the ``top_n`` sessions with the highest pre-gate cost."""
    lines = [
        "| session | calls | gate | cold-start tokens | cold-start $ | pre-gate main $ | pre-gate sub $ | pre-gate $ |",
        "|---|--:|---|--:|--:|--:|--:|--:|",
    ]
    ranked = sorted(metrics, key=lambda m: m.pre_gate_usd, reverse=True)
    for m in ranked[:top_n]:
        gate = "yes" if m.gate_found else "none (whole span)"
        lines.append(
            f"| `{m.session[:8]}` | {m.calls} | {gate} | {m.cold_write} | {m.cold_cost_usd:.2f} "
            f"| {m.pre_gate_main_usd:.2f} | {m.pre_gate_sub_usd:.2f} | {m.pre_gate_usd:.2f} |"
        )
    return [*lines, "" if len(ranked) <= top_n else f"(+{len(ranked) - top_n} more sessions in the median above)", ""]


def _finding_rows(metrics: list[SessionMetrics], top_n: int) -> list[str]:
    """R1, R7 and R4/R5 sections, each ranked by dollars and capped at ``top_n`` rows."""
    switches = sorted(((m.session[:8], s) for m in metrics for s in m.switches), key=lambda r: -r[1].cost_usd)
    losses = sorted(((m.session[:8], i) for m in metrics for i in m.idle_losses), key=lambda r: -r[1].cost_usd)
    flagged = sorted(((m.session[:8], a) for m in metrics for a in m.agents if a.flags), key=lambda r: -r[1].cost_usd)
    return [
        "### R1 model-switch rewrites",
        "",
        *_capped(
            [
                f"- `{sid}` {'after call ' + str(s.index - 1) if s.exit else 'call ' + str(s.index)}: {s.previous} → {s.model}, {s.cache_write} tokens, ${s.cost_usd:.2f}"
                for sid, s in switches
            ],
            top_n,
        ),
        "### R7 idle-gap cache losses",
        "",
        *_capped(
            [
                f"- `{sid}` call {i.index} ({i.where}): idle {i.gap_s:.0f}s, {i.cache_write} tokens, ${i.cost_usd:.2f}"
                for sid, i in losses
            ],
            top_n,
        ),
        "### R4/R5 flagged subagents",
        "",
        *_capped(
            [
                f"- `{sid}` {a.agent_type}: {a.calls} calls, context {a.start_context} → {a.end_context}, "
                f"${a.cost_usd:.2f} ⚠ {', '.join(a.flags)}"
                for sid, a in flagged
            ],
            top_n,
        ),
    ]


def render(
    metrics: list[SessionMetrics],
    label: str,
    delta: list[str],
    timelines: list[str],
    top_n: int = TOP_N,
    skipped: int = 0,
) -> list[str]:
    """Full markdown report; session tables and finding lists are capped at ``top_n`` rows."""
    summary = summarise(metrics)
    note = f" · {skipped} unreadable session(s) skipped" if skipped else ""
    return [
        "## Measured efficiency",
        "",
        f"Label `{label}` · {summary['sessions']} session(s){note} · list prices, not a billing statement.",
        "",
        "### R2/R3 cold start and pre-gate cost",
        "",
        f"Median pre-gate ${summary['pre_gate_median_usd']:.2f}, median cold start ${summary['cold_start_median_usd']:.2f}; "
        f"{summary['switch_count']} model-switch rewrite(s) costing ${summary['switch_usd']:.2f} in total; "
        f"{summary['flagged_agents']} flagged subagent(s).",
        "",
        *_session_rows(metrics, top_n),
        *_finding_rows(metrics, top_n),
        "### Not measured",
        "",
        "- R6 tier fit and R8 tool-output bloat: thresholds not yet set from measured runs.",
        "- R2 is the first call's cache write, not split into instructions, skill listing and hooks.",
        "- R5 checks the leading run of literal calls, not the commands a spawn prompt lists.",
        "- Findings carry dollars but no confidence score, static-finding link or open-since date.",
        "- A session's later runs of one skill join the first in R1 and R7; cold start and pre-gate cover the first run.",
        "- A `/model` typed inside a skill's run counts as that skill's switch.",
        "- The revert after back-to-back pinned skills belongs to none of them.",
        "",
        "### R9 baseline and delta",
        "",
        *delta,
        *timelines,
    ]


def _plugin_name(parts: tuple[str, ...], index: int, fallback: str) -> str:
    """Plugin name from the manifest above the skills directory, else the directory-derived ``fallback``."""
    manifest = Path(PurePosixPath(*parts[:index]).as_posix()) / ".claude-plugin" / "plugin.json"
    try:
        name = json.loads(manifest.read_text(encoding="utf-8")).get("name")
    except (OSError, ValueError, AttributeError):
        return fallback
    return name if isinstance(name, str) and name else fallback


def command_for_skill(path: str) -> str | None:
    """Slash command that runs the skill at ``path``, or ``None`` when the path holds no skill directory.

    The plugin part is the manifest name when the plugin directory is readable, else the directory above ``skills/`` or
    ``claude-skills/`` without its ``cc_`` prefix; a project-local ``.claude/skills/<name>`` runs as plain ``/<name>``.

    Examples:
        >>> command_for_skill("plugins/cc_oss/skills/resolve/SKILL.md")
        '/oss:resolve'
        >>> command_for_skill("plugins/codemap-py/claude-skills/query-code/SKILL.md")
        '/codemap-py:query-code'
        >>> command_for_skill(".claude/skills/audit/SKILL.md")
        '/audit'
        >>> command_for_skill("~/.claude/plugins/cache/market/oss/0.55.0/skills/resolve/SKILL.md")
        '/oss:resolve'
        >>> command_for_skill("plugins/cc_oss/agents/shepherd.md") is None
        True
    """
    parts = PurePosixPath(Path(path).as_posix()).parts
    for index, part in enumerate(parts):
        if part in ("skills", "claude-skills") and 0 < index < len(parts) - 2:
            owner = parts[index - 2] if index > 1 and _VERSION_DIR.fullmatch(parts[index - 1]) else parts[index - 1]
            plugin = owner.removeprefix("cc_")
            if plugin == ".claude":
                return f"/{parts[index + 1]}"
            return f"/{_plugin_name(parts, index, plugin)}:{parts[index + 1]}"
    return None


def commands_from_scan(text: str) -> list[str]:
    """Slash commands of every skill the efficiency scan flags ``INLINE_MODEL``, in first-seen order.

    Examples:
        >>> scan = "INLINE_MODEL: plugins/cc_oss/skills/resolve/SKILL.md\\nNO_MODEL: plugins/cc_oss/agents/x.md\\n"
        >>> commands_from_scan(scan)
        ['/oss:resolve']
    """
    found: dict[str, None] = {}
    for line in text.splitlines():
        if line.startswith("INLINE_MODEL:"):
            command = command_for_skill(line.split(":", 1)[1].strip())
            if command:
                found[command] = None
    return list(found)


def project_dir(root: Path, cwd: Path) -> Path:
    """Transcript directory of the project at ``cwd``: every non-alphanumeric character of its path becomes ``-``.

    Examples:
        >>> project_dir(Path.cwd(), Path.cwd()).parent == Path.cwd()
        True
    """
    return root / re.sub(r"[^A-Za-z0-9]", "-", str(cwd.resolve()))


#: Separator between a project's slug and the slug of one of its Claude worktrees.
WORKTREE_MARK = "--claude-worktrees-"


def project_family(root: Path, name: str) -> list[Path]:
    """Return the transcript directory named ``name`` plus those of its Claude worktrees (``<name>--claude-
    worktrees-*``).

    Examples:
        >>> project_family(Path("/does/not/exist"), "-a-b")[0].name
        '-a-b'
    """
    return [root / name, *sorted(root.glob(f"{glob.escape(name)}{WORKTREE_MARK}*"))]


def project_dirs(root: Path, cwd: Path) -> list[Path]:
    """Return the project's transcript directory plus those of its Claude worktrees."""
    return project_family(root, project_dir(root, cwd).name)


def _project_names(names: list[str], projects_file: str | None) -> list[str]:
    """Project directory names from ``--project`` and the lines of ``--projects-file``; a name with a separator is
    refused.

    Examples:
        >>> _project_names(["-a-b", "-a-b"], None)
        ['-a-b']
        >>> _project_names(["../x"], None)
        Traceback (most recent call last):
        ...
        ValueError: project name must be a directory name, not a path: '../x'
    """
    lines = Path(projects_file).read_text(encoding="utf-8").splitlines() if projects_file else []
    if projects_file and not any(line.strip() for line in lines):
        raise ValueError(f"projects file names no project: {projects_file}")
    found = dict.fromkeys(name.strip() for name in [*names, *lines] if name.strip())
    for name in found:
        if Path(name).name != name or name in {".", ".."} or "\\" in name:
            raise ValueError(f"project name must be a directory name, not a path: {name!r}")
    return list(found)


def _unknown_projects(root: Path, names: list[str]) -> list[str]:
    """Return the named projects that have neither a transcript directory nor one for a Claude worktree.

    Examples:
        >>> _unknown_projects(Path("/does/not/exist"), ["-a"])
        ['-a']
    """
    return [name for name in names if not any(path.is_dir() for path in project_family(root, name))]


def list_projects(root: Path, commands: list[str], cutoff: float) -> list[tuple[str, int, float]]:
    """List projects with sessions that typed one of ``commands`` since ``cutoff``: name, session count, last time.

    A project's Claude worktrees count under the project itself. Unreadable transcripts are skipped with a reason.
    """
    found: dict[str, list[float]] = {}
    for path in ca.discover_sessions(root):
        try:
            age = path.stat().st_mtime
            if age < cutoff:
                continue
            times = [
                ts or age
                for ts, name in parse_transcript(path).command_rows
                if name in commands and (ts or age) >= cutoff
            ]
        except _SESSION_ERRORS as exc:
            sys.stderr.write(f"skipped {path.stem[:8]}: {type(exc).__name__}\n")
            continue
        if times:
            found.setdefault(path.parent.name.split(WORKTREE_MARK)[0], []).append(max(times))
    return sorted(((name, len(times), max(times)) for name, times in found.items()), key=lambda row: (-row[1], row[0]))


def project_list_lines(rows: list[tuple[str, int, float]], current: str) -> list[str]:
    """Tab-separated listing: project, sessions, last active day (UTC), and ``yes`` for the project at hand.

    Examples:
        >>> project_list_lines([("-a", 3, 0.0)], "-a")
        ['project\\tsessions\\tlast_active\\tcurrent', '-a\\t3\\t1970-01-01\\tyes']
    """
    lines = ["project\tsessions\tlast_active\tcurrent"]
    for name, count, last in rows:
        day = datetime.fromtimestamp(last, timezone.utc).strftime("%Y-%m-%d")
        lines.append(f"{name}\t{count}\t{day}\t{'yes' if name == current else 'no'}")
    return lines


def _repo_root() -> Path:
    """Return the main checkout of the current directory, else the current directory.

    A session starts at the top of the main checkout, and a Claude worktree has its own transcript directory, so inside
    a worktree the shared repository directory (the ``.git`` of the main checkout) names the project, not the worktree's
    own top level. A worktree made by hand has a transcript directory of its own that the project's family does not
    include; name it with ``--project`` to measure it.
    """
    git = shutil.which("git")
    if git is None:
        return Path.cwd()
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, git resolved by shutil.which
            [git, "rev-parse", "--show-toplevel", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return Path.cwd()
    lines = done.stdout.splitlines()
    if done.returncode != 0 or len(lines) < 2 or not lines[0].strip():
        return Path.cwd()
    shared = (Path.cwd() / lines[1].strip()).resolve()
    return shared.parent if shared.name == ".git" else Path(lines[0].strip())


def _ran_since(path: Path, command: str, cutoff: float) -> bool:
    """Whether the user typed ``command`` in ``path`` at or after ``cutoff``; an untimed row falls back to file age."""
    rows = parse_transcript(path).command_rows
    return any(name == command and (ts >= cutoff if ts else path.stat().st_mtime >= cutoff) for ts, name in rows)


def _select_sessions(
    session_ids: list[str], command: str | None, cutoff: float, root: Path, projects: list[Path] | None = None
) -> tuple[list[Path], set[str]]:
    """Resolve the sessions to analyse from id prefixes and/or a slash command typed since ``cutoff``.

    Returns the session paths and the stems of transcripts skipped as unreadable. ``projects`` limits the search to
    those transcript directories; otherwise every project under ``root`` is searched.
    """
    found: list[Path] = []
    skipped: set[str] = set()
    if projects is None:
        available = ca.discover_sessions(root)
    else:
        available = sorted(path for directory in projects for path in directory.glob("*.jsonl"))
    for prefix in session_ids:
        found += [path for path in available if path.stem.startswith(prefix)][:1]
    for path in available if command else []:
        try:
            if path.stat().st_mtime >= cutoff and path not in found and _ran_since(path, command, cutoff):
                found.append(path)
        except _SESSION_ERRORS as exc:
            skipped.add(path.stem)
            sys.stderr.write(f"skipped {path.stem[:8]}: {type(exc).__name__}\n")
    return found, skipped


def _report(
    paths: list[Path],
    label: str,
    command: str | None,
    since_ts: float,
    args: argparse.Namespace,
    skipped: int = 0,
    scope: str = "all",
    edges: bool = True,
) -> list[str] | None:
    """Analyse ``paths`` under one baseline label; ``None`` when no session holds usage.

    ``scope`` names the transcripts searched (project directory names, ``root:<path>`` or ``all``) and is stored with
    the record. ``edges`` is False for a label the scan does not flag, which owns no border rewrite; it is stored as
    ``border`` so a later run that counts switches the other way is warned about.
    """
    results = []
    for path in paths:
        try:
            results.append((path, analyse_session(path, command, since_ts, edges)))
        except _SESSION_ERRORS as exc:
            skipped += 1
            sys.stderr.write(f"skipped {path.stem[:8]}: {type(exc).__name__}\n")
    metrics = [m for _, m in results if m is not None]
    if not metrics:
        return None
    ledger = _ledger_path(args.baseline)
    summary = summarise(metrics)
    session_ids = [m.session[:8] for m in metrics]
    delta = _delta_section(ledger, label, summary, session_ids, scope, args.since, edges)
    timelines = [
        line for path, m in results if m is not None and args.timeline for line in timeline(path, command, since_ts)
    ]
    if args.record:
        extra = {
            "baseline": args.rebaseline or pinned_record(ledger, label) is None,
            "since": args.since,
            "scope": scope,
            "border": edges,
            "session_ids": session_ids,
        }
        append_record(ledger, label, summary, extra)
    return render(metrics, label, delta, timelines, args.top_n, skipped)


def _build_parser() -> argparse.ArgumentParser:
    """Command-line interface."""
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--projects-root", default=None, help="Transcript root; default ~/.claude/projects")
    p.add_argument(
        "--all-projects", action="store_true", help="Search every project under the default root, not only this one"
    )
    p.add_argument(
        "--project",
        action="append",
        default=[],
        help="Transcript directory of a project to search, with its worktrees; repeatable. Names start with a dash, so write --project=-NAME; see --list-projects",
    )
    p.add_argument("--projects-file", default=None, help="File with one --project name per line")
    p.add_argument(
        "--list-projects",
        action="store_true",
        help="With --scan-file or --command: print the projects whose sessions ran the command(s) in the window, then exit",
    )
    p.add_argument("--session-id", action="append", default=[], help="Session id or prefix; repeatable")
    p.add_argument("--command", default=None, help="Select sessions that ran this slash command, e.g. /oss:resolve")
    p.add_argument("--since", default="14d", help="Window for --command: NNs|NNm|NNh|NNd")
    p.add_argument(
        "--scan-file",
        default=None,
        help="Output of scan_efficiency_signals.py; measures every INLINE_MODEL skill it names and every slash-command "
        "label already in the baseline ledger, one section each",
    )
    p.add_argument("--label", default=None, help="Baseline label; defaults to --command, else 'default'")
    p.add_argument("--baseline", default=DEFAULT_BASELINE, help="Baseline ledger path")
    p.add_argument("--record", action="store_true", help="Append this run to the baseline ledger")
    p.add_argument(
        "--rebaseline",
        action="store_true",
        help="With --record: make this record the pinned baseline, e.g. just before a fix or after a method change",
    )
    p.add_argument("--timeline", action="store_true", help="Add the per-call timeline of each session")
    p.add_argument("--top-n", type=int, default=TOP_N, help="Rows kept per ranked list")
    p.add_argument("--output", default=None, help="Write the report here instead of stdout")
    return p


def _listed_commands(args: argparse.Namespace) -> tuple[list[str], list[str]]:
    """Return the slash commands to look for and the part the scan flags as pinned.

    A scan run looks for the flagged skills plus the ledger's labels (a skill whose pin was removed); only the flagged
    ones own model rewrites at the border of their spans. A ``--command`` run owns its own.
    """
    if args.scan_file:
        scan = Path(args.scan_file).read_text(encoding="utf-8", errors="replace")
        flagged = commands_from_scan(scan)
        return [*flagged, *ledger_labels(_ledger_path(args.baseline))], flagged
    return ([args.command] if args.command else []), []


def _ledger_path(baseline: str) -> Path:
    """Ledger path; the default one is anchored at the main checkout so a subdirectory or worktree shares one
    history."""
    path = Path(baseline).expanduser()
    return path if path.is_absolute() or baseline != DEFAULT_BASELINE else _repo_root() / path


def _print_projects(root: Path, commands: list[str], cutoff: float) -> int:
    """Print the project listing; exit 1 when no project has a matching session."""
    rows = list_projects(root, commands, cutoff)
    if not rows:
        sys.stderr.write("no project has a session that ran a flagged skill in the window\n")
        return 1
    current = project_dir(root, _repo_root()).name.split(WORKTREE_MARK)[0]
    sys.stdout.write("\n".join(project_list_lines(rows, current)) + "\n")
    return 0


def _run(args: argparse.Namespace) -> int:
    """Select the sessions, build the sections and print or write the report."""
    root = Path(args.projects_root or ca.CLAUDE_PROJECTS_ROOT).expanduser()
    named = _project_names(args.project, args.projects_file)
    if unknown := _unknown_projects(root, named):
        sys.stderr.write(
            f"unknown project: {', '.join(unknown)} has no transcript directory under {root}; "
            "use the names --list-projects prints, without any added text\n"
        )
        return 2
    if named:
        projects = [path for name in named for path in project_family(root, name)]
        scope = "+".join(sorted(named))
    elif args.projects_root or args.all_projects:
        projects, scope = None, f"root:{root.resolve()}" if args.projects_root else "all"
    else:
        projects = project_dirs(root, _repo_root())
        scope = projects[0].name
    cutoff = time.time() - ca.parse_since(args.since)
    sections: list[list[str]] = []
    skipped: set[str] = set()
    all_commands, flagged = _listed_commands(args)
    commands = dict.fromkeys(all_commands)
    if args.list_projects:
        return _print_projects(root, list(commands), cutoff)
    if args.scan_file:
        jobs = [(command, command, *_select_sessions([], command, cutoff, root, projects)) for command in commands]
        since_ts = cutoff
    else:
        command = args.command
        label = normalise_label(args.label or command or "default")
        paths, skip = _select_sessions(args.session_id, command, cutoff, root, projects)
        jobs = [(label, command, paths, skip)]
        since_ts = cutoff if command and not args.session_id else 0.0
    for label, command, paths, skip in jobs:
        skipped |= skip
        edges = not args.scan_file or command in flagged
        if (section := _report(paths, label, command, since_ts, args, len(skip), scope, edges)) is not None:
            sections.append(section)
    if not sections:
        if projects is not None and not named and not projects[0].is_dir():
            sys.stderr.write(f"no transcript directory for this project: {projects[0].name} not found under {root}\n")
        else:
            where = "all projects" if projects is None else f"the transcripts of {scope} (with its worktrees)"
            sys.stderr.write(
                f"no matching session with usage in {where} ({len(skipped)} unreadable skipped); "
                "--all-projects searches every project\n"
            )
        return 1
    report = "\n".join(line for section in sections for line in section) + "\n"
    if args.output:
        Path(args.output).expanduser().write_text(report, encoding="utf-8", errors="replace")
    else:
        sys.stdout.write(report)
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry: analyse the selected sessions and print or write the report."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not (args.session_id or args.command or args.scan_file):
        parser.error("give --session-id, --command or --scan-file")
    if args.list_projects and not (args.scan_file or args.command):
        parser.error("--list-projects needs --scan-file or --command")
    if (args.project or args.projects_file) and args.all_projects:
        parser.error("--project and --projects-file cannot be combined with --all-projects")
    try:
        ca.parse_since(args.since)
        _project_names(args.project, args.projects_file)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    try:
        return _run(args)
    except _SESSION_ERRORS as exc:
        sys.stderr.write(f"efficiency_measured failed: {type(exc).__name__}: {exc}\n")
        return 4


if __name__ == "__main__":
    sys.exit(main())
