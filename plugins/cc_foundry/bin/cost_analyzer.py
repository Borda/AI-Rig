#!/usr/bin/env python
"""cost_analyzer.py — bucket Claude Code session token spend and cost.

Reads Claude Code transcripts (``~/.claude/projects/<slug>/<session-id>.jsonl``
plus that session's ``<session-id>/subagents/agent-*.jsonl`` files) and
produces a markdown report of token usage and USD cost, split by session,
model tier, and main-loop vs subagent. Companion to ``timing_analyzer.py``,
which answers "where did the *clock* go" from a different data source
(``~/.claude/logs/{timings,invocations}.jsonl``); this answers "where did
the *tokens/money* go". Designed for the ``/foundry:profile`` skill, which
runs both and merges them into one report.

Three facts that shaped this module, load-bearing enough to restate here
rather than only in a report footnote:

1. **Dedupe by ``message.id`` or triple-count.** Claude Code writes one
   JSONL row per content block; every row of the same assistant message
   repeats that message's ``usage`` object verbatim. Summing rows instead
   of deduplicating multiplies the answer by the average block count — on
   one real session that read $61.21 where the true figure was $20.56.
2. **Subagent transcripts are real transcripts.** Each ``agent-*.jsonl``
   under a session's ``subagents/`` directory is parseable with the exact
   same row shape as the main-loop file (``message.usage``, ``isSidechain``
   already ``true`` on its own rows). A prior version of these scripts
   read only the flat ``<session-id>.jsonl``, which contains zero
   sidechain rows — every total was a main-loop floor, undercounting a
   fan-out-heavy session by roughly half. Reading the subagent files
   closes that gap.
3. **Prices are public list rates, per model id**, hard-coded in
   ``PRICES`` from the platform.claude.com pricing page (checked
   2026-10-08). The transcript records tokens only; effective plan rates
   may differ, so dollar figures are proportional truth, not a billing
   statement. Generations inside one family differ several-fold (Opus 5.5
   at $4/$20 vs Opus 4.1 at $15/$75), so pricing by family substring
   misprices whole sessions. A model released after the table was written
   prices as its family's newest known model, with a stderr warning —
   never at zero and never dropped. A retired id with no row of its own
   (the older ``claude-3-5-haiku-...`` word order, or a version below its
   family's newest) prices at the maximum known rates instead, never at a
   newer generation's lower ones. Cache writes derive from the input rate
   (5-minute 1.25x, 1-hour 2x) while the cache-read rate varies per model
   (0.1x standard, 0.05x on Opus/Sonnet 5.5, 0.025x on Fable 5.1), so a
   write costs 12.5x to 80x a read — which is why a mid-run context
   rebuild (e.g. ``/clear``) dominates a session's cost line.
4. **Some prices are per request, not per token total.** Usage rows
   carry a ``cache_creation`` split (``ephemeral_5m_input_tokens`` /
   ``ephemeral_1h_input_tokens``) priced per duration — on observed
   transcripts the main loop writes the 1-hour cache and subagents the
   5-minute one, so one flat write rate misprices either side. Haiku 5.5 bills a
   request whose prompt (input + cache read + cache write tokens) exceeds
   100,000 at a higher band. Neither survives summing tokens, so every
   rollup sums per-call cost instead of re-pricing token totals.

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/cost_analyzer.py" \\
        --since 24h --output report.md
    python "${CLAUDE_PLUGIN_ROOT}/bin/cost_analyzer.py" \\
        --session-id 9c1bded7 --output report.md
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

#: 5-minute cache-write price as a multiple of the base input rate — the same for every model.
CACHE_WRITE_5M_MULTIPLIER = 1.25
#: 1-hour cache-write price as a multiple of the base input rate — the same for every model.
CACHE_WRITE_1H_MULTIPLIER = 2.0


class Rate(NamedTuple):
    """USD per million tokens for one model within one prompt-size band.

    Cache-write rates are not stored: they derive from ``input`` through the module multipliers, leaving the cache-read
    rate as the only per-model cache price.
    """

    input: float
    output: float
    cache_read: float


@dataclass(frozen=True)
class ModelPrice:
    """List price for one model, optionally with a higher band for long prompts.

    Attributes:
        base: Rate for every request, or for prompts up to the threshold when ``long_prompt`` is set.
        long_prompt: ``(threshold, rate)`` pair — ``rate`` applies to a request whose prompt tokens exceed
            ``threshold``.
    """

    base: Rate
    long_prompt: tuple[int, Rate] | None = None

    def rate(self, prompt_tokens: int) -> Rate:
        """Pick the rate band for one request of ``prompt_tokens`` prompt tokens.

        Examples:
            >>> PRICES["haiku-5.5"].rate(100_000).input
            0.1
            >>> PRICES["haiku-5.5"].rate(100_001).input
            0.5
            >>> PRICES["opus-5.5"].rate(900_000).input
            4.0
        """
        if self.long_prompt is not None and prompt_tokens > self.long_prompt[0]:
            return self.long_prompt[1]
        return self.base


#: Haiku 5.5 bills a request whose prompt exceeds this many tokens at its higher band.
HAIKU_5_5_LONG_PROMPT_TOKENS = 100_000

#: List price per model, keyed ``<family>-<major>[.<minor>]`` (see :func:`price_key`).
PRICES: dict[str, ModelPrice] = {
    "fable-5.1": ModelPrice(Rate(10.0, 50.0, 0.25)),
    "fable-5": ModelPrice(Rate(10.0, 50.0, 1.00)),
    "opus-5.5": ModelPrice(Rate(4.0, 20.0, 0.20)),
    "opus-5": ModelPrice(Rate(5.0, 25.0, 0.50)),
    "opus-4.8": ModelPrice(Rate(5.0, 25.0, 0.50)),
    "opus-4.7": ModelPrice(Rate(5.0, 25.0, 0.50)),
    "opus-4.6": ModelPrice(Rate(5.0, 25.0, 0.50)),
    "opus-4.5": ModelPrice(Rate(5.0, 25.0, 0.50)),
    # Retired on the Claude API; kept so older transcripts price correctly, and so they set UNKNOWN_RATE's ceiling.
    "opus-4.1": ModelPrice(Rate(15.0, 75.0, 1.50)),
    "opus-4": ModelPrice(Rate(15.0, 75.0, 1.50)),
    "sonnet-5.5": ModelPrice(Rate(2.0, 10.0, 0.10)),
    "sonnet-5": ModelPrice(Rate(2.0, 10.0, 0.20)),
    "sonnet-4.6": ModelPrice(Rate(3.0, 15.0, 0.30)),
    "sonnet-4.5": ModelPrice(Rate(3.0, 15.0, 0.30)),
    "sonnet-4": ModelPrice(Rate(3.0, 15.0, 0.30)),
    "haiku-5.5": ModelPrice(
        Rate(0.10, 0.50, 0.01),
        long_prompt=(HAIKU_5_5_LONG_PROMPT_TOKENS, Rate(0.50, 2.50, 0.05)),
    ),
    "haiku-4.5": ModelPrice(Rate(1.0, 5.0, 0.10)),
}

#: Report tier for a model id matching no known family.
UNKNOWN_TIER = "unknown"

#: Every rate band in ``PRICES``, long-prompt bands included.
_ALL_RATES = [p.base for p in PRICES.values()] + [p.long_prompt[1] for p in PRICES.values() if p.long_prompt]

#: Price for an id of no known family: the per-field maximum across every band, so it overstates rather than hides.
UNKNOWN_RATE = Rate(*(max(column) for column in zip(*_ALL_RATES)))

#: Parses ``claude-<family>-<major>[-<minor>]``; a 1-2 digit minor keeps a date suffix from reading as one.
#: The older ``claude-3-5-haiku-...`` word order never matches — those retired ids price at :data:`UNKNOWN_RATE`.
MODEL_ID_RE = re.compile(r"claude-(?P<family>[a-z]+)-(?P<major>\d+)(?:-(?P<minor>\d{1,2})(?!\d))?")


def _version(key: str) -> tuple[int, ...]:
    """Turn a ``PRICES`` key's version into a sortable tuple.

    Examples:
        >>> _version("opus-4.10") > _version("opus-4.8")
        True
    """
    return tuple(int(part) for part in key.split("-", 1)[1].split("."))


#: Newest priced model per family — what an id of that family with no ``PRICES`` entry yet is priced as.
LATEST_BY_FAMILY: dict[str, str] = {
    family: max((key for key in PRICES if key.split("-")[0] == family), key=_version)
    for family in ("fable", "opus", "sonnet", "haiku")
}

#: Matches a ``<command-name>`` marker in a transcript line, capturing the slash command name.
COMMAND_RE = re.compile(r"<command-name>([^<]+)</command-name>")


def parse_since(spec: str) -> float:
    """Parse a ``Nu`` duration spec into seconds (``s|m|h|d`` suffix).

    Args:
        spec: e.g. ``"24h"``, ``"7d"``, ``"30m"``.

    Returns:
        Total seconds.

    Examples:
        >>> parse_since("1h")
        3600.0
        >>> parse_since("7d")
        604800.0
    """
    m = re.fullmatch(r"(\d+)([smhd])", spec)
    if not m:
        raise ValueError(f"invalid --since: {spec!r}; expected NNu where u in s|m|h|d")
    n, unit = int(m.group(1)), m.group(2)
    mul = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return float(n * mul)


def parse_ts(s: str) -> float:
    """Parse an ISO-8601 ``...Z`` timestamp into a POSIX UTC float.

    Examples:
        >>> parse_ts("1970-01-01T00:00:01Z")
        1.0
    """
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def price_key(model: str) -> str | None:
    """Resolve a transcript model id to its ``PRICES`` key, or ``None`` when no price is known.

    Dated (``-20251001``) and context-suffixed (``[1m]``) ids resolve to their base model.

    Examples:
        >>> price_key("claude-opus-5-5")
        'opus-5.5'
        >>> price_key("claude-opus-5[1m]")
        'opus-5'
        >>> price_key("claude-haiku-4-5-20251001")
        'haiku-4.5'
        >>> price_key("claude-opus-5-20260101")
        'opus-5'
        >>> price_key("<synthetic>") is None
        True
    """
    match = MODEL_ID_RE.match(model)
    if match is None:
        return None
    key = f"{match['family']}-{match['major']}" + (f".{match['minor']}" if match["minor"] else "")
    return key if key in PRICES else None


def price_entry(model: str) -> str | None:
    """Pick the ``PRICES`` key that prices ``model``, falling back to its family's newest model for a newer release.

    A model released after this table was written has no entry of its own; it prices as the newest known model of
    the same family rather than at zero or not at all. Only a ``claude-<family>-<version>`` id whose version is above
    that newest model takes this fallback. A retired id without its own entry — the older ``claude-3-5-haiku-...``
    word order, or a version below the family's newest — would be under-priced at a newer generation's rates (this
    table lists the retired Opus 4.1 at $15/$75 against Opus 5.5 at $4/$20), so it returns ``None`` and prices at
    :data:`UNKNOWN_RATE`. ``None`` also means the id names no known family.

    Examples:
        >>> price_entry("claude-sonnet-4-6")
        'sonnet-4.6'
        >>> price_entry("claude-opus-6")
        'opus-5.5'
        >>> price_entry("claude-3-5-haiku-20241022") is None
        True
        >>> price_entry("claude-opus-4-3") is None
        True
        >>> price_entry("some-unreleased-model") is None
        True
    """
    key = price_key(model)
    if key is not None:
        return key
    match = MODEL_ID_RE.match(model)
    if match is None or match["family"] not in LATEST_BY_FAMILY:
        return None
    newest = LATEST_BY_FAMILY[match["family"]]
    released = (int(match["major"]), int(match["minor"] or 0))
    return newest if released > (*_version(newest), 0)[:2] else None


def tier(model: str) -> str:
    """Map a model id onto its report tier — the model family, or ``unknown``.

    The tier only groups report rows; pricing is per model id (see :func:`cost`). A release newer than its family's
    newest ``PRICES`` entry prices as that model (see :func:`price_entry`); an id of no known family, or a retired id
    without its own entry, is ``unknown`` and prices at :data:`UNKNOWN_RATE`, the most expensive rates — an overstated
    cost prompts investigation, an understated one hides it. Transcripts record API model ids, never Claude Code
    aliases.

    Examples:
        >>> tier("claude-opus-5[1m]")
        'opus'
        >>> tier("claude-haiku-4-5-20251001")
        'haiku'
        >>> tier("claude-opus-6")
        'opus'
        >>> tier("claude-3-opus-20240229")
        'unknown'
        >>> tier("some-unreleased-model")
        'unknown'
    """
    entry = price_entry(model)
    return entry.split("-")[0] if entry else UNKNOWN_TIER


def rate_for(model: str, prompt_tokens: int) -> Rate:
    """Return the rate band that prices one request of ``model`` with ``prompt_tokens`` prompt tokens.

    Examples:
        >>> rate_for("claude-sonnet-5-5", 0)
        Rate(input=2.0, output=10.0, cache_read=0.1)
        >>> rate_for("some-unreleased-model", 0) == UNKNOWN_RATE
        True
    """
    entry = price_entry(model)
    return PRICES[entry].rate(prompt_tokens) if entry else UNKNOWN_RATE


def unpriced_models(calls: list[Call]) -> dict[str, str]:
    """Map each model id that carried tokens but has no ``PRICES`` entry of its own to what it was priced as.

    Zero-usage rows (Claude Code's ``<synthetic>`` messages) cost nothing whatever the rate, so they never warn.

    Examples:
        >>> calls = [Call("m1", "claude-opus-6", {"output_tokens": 5}),
        ...          Call("m2", "claude-opus-5-5", {"output_tokens": 5}),
        ...          Call("m3", "<synthetic>", {"output_tokens": 0})]
        >>> unpriced_models(calls)
        {'claude-opus-6': 'opus-5.5'}
    """
    found: dict[str, str] = {}
    for call in calls:
        if price_key(call.model) is None and prompt_tokens(call.usage) + call.output > 0:
            found[call.model] = price_entry(call.model) or UNKNOWN_TIER
    return found


def _warn_unpriced(calls: list[Call]) -> None:
    """Write one stderr warning per model id priced by fallback rather than by its own list price."""
    for model, entry in sorted(unpriced_models(calls).items()):
        priced_as = f"newest known {entry.split('-')[0]} ({entry})" if entry != UNKNOWN_TIER else "maximum known rates"
        sys.stderr.write(f"warning: no list price for model {model!r}; priced as {priced_as}\n")


def prompt_tokens(usage: dict) -> int:
    """Count one request's prompt tokens — input plus cache read plus cache write, the measure Haiku 5.5 bands on.

    Examples:
        >>> prompt_tokens({"input_tokens": 2, "cache_read_input_tokens": 90_000, "cache_creation_input_tokens": 10_000})
        100002
        >>> prompt_tokens({})
        0
    """
    return (
        usage.get("input_tokens", 0)
        + usage.get("cache_read_input_tokens", 0)
        + usage.get("cache_creation_input_tokens", 0)
    )


def cache_write_split(usage: dict) -> tuple[int, int]:
    """Split cache-write tokens into ``(5-minute, 1-hour)`` counts.

    Without a ``cache_creation`` breakdown every write counts as 5-minute — the cheaper rate, so an old transcript
    without the split never gains cost it may not have had.

    Examples:
        >>> cache_write_split({"cache_creation_input_tokens": 10})
        (10, 0)
        >>> split = {"ephemeral_5m_input_tokens": 3, "ephemeral_1h_input_tokens": 7}
        >>> cache_write_split({"cache_creation_input_tokens": 10, "cache_creation": split})
        (3, 7)
    """
    total = usage.get("cache_creation_input_tokens", 0)
    split = usage.get("cache_creation")
    if not isinstance(split, dict):
        return total, 0
    one_hour = min(split.get("ephemeral_1h_input_tokens", 0), total)
    return total - one_hour, one_hour


def cost(usage: dict, model: str) -> float:
    """Price one usage object of ``model`` in USD — missing keys count as zero.

    Examples:
        >>> cost({"output_tokens": 1_000_000}, "claude-opus-5-5")
        20.0
        >>> cost({"input_tokens": 1_000_000}, "claude-haiku-5-5")
        0.5
        >>> round(cost({"input_tokens": 100_000}, "claude-haiku-5-5"), 6)
        0.01
        >>> split = {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 1_000_000}
        >>> cost({"cache_creation_input_tokens": 1_000_000, "cache_creation": split}, "claude-opus-5-5")
        8.0
        >>> cost({}, "claude-opus-5-5")
        0.0
    """
    rate = rate_for(model, prompt_tokens(usage))
    write_5m, write_1h = cache_write_split(usage)
    return (
        usage.get("input_tokens", 0) * rate.input
        + usage.get("output_tokens", 0) * rate.output
        + write_5m * rate.input * CACHE_WRITE_5M_MULTIPLIER
        + write_1h * rate.input * CACHE_WRITE_1H_MULTIPLIER
        + usage.get("cache_read_input_tokens", 0) * rate.cache_read
    ) / 1_000_000


@dataclass
class Call:
    """One deduplicated assistant API call."""

    message_id: str
    model: str
    usage: dict
    tools: list[str] = field(default_factory=list)
    sidechain: bool = False

    @property
    def cache_read(self) -> int:
        return self.usage.get("cache_read_input_tokens", 0)

    @property
    def cache_write(self) -> int:
        return self.usage.get("cache_creation_input_tokens", 0)

    @property
    def output(self) -> int:
        return self.usage.get("output_tokens", 0)


@dataclass
class AgentSpend:
    """Cost rollup for one subagent transcript file."""

    agent_type: str
    description: str
    calls: int
    cost_usd: float


@dataclass
class Session:
    """A parsed session: main-loop + merged subagent calls, plus metadata."""

    path: Path
    calls: list[Call]
    commands: dict[str, int]
    agent_spends: list[AgentSpend]
    ts_first: float = float("inf")
    ts_last: float = 0.0

    @property
    def project(self) -> str:
        return _project_label(self.path.parent.name)

    @property
    def total_cost(self) -> float:
        return sum(cost(c.usage, c.model) for c in self.calls)

    @property
    def agent_count(self) -> int:
        return sum(a.calls > 0 for a in self.agent_spends) or len(self.agent_spends)

    @property
    def subagent_cost(self) -> float:
        return sum(a.cost_usd for a in self.agent_spends)


def _project_label(dirname: str) -> str:
    """Strip the encoded home-directory prefix from a `~/.claude/projects` dir name.

    Project directory names are the session's cwd with path separators
    replaced by ``-``. Stripping the machine-specific home prefix (derived
    from ``Path.home()``, never hard-coded) leaves a short, portable label.

    Examples:
        >>> import unittest.mock as mock
        >>> with mock.patch("pathlib.Path.home", return_value=Path("/Users/x")):
        ...     _project_label("-Users-x-Workspace-Borda-local")
        'Workspace-Borda-local'
        >>> _project_label("-some-other-slug")
        '-some-other-slug'
    """
    home_slug = str(Path.home()).replace("\\", "-").replace("/", "-")
    if dirname.startswith(home_slug):
        return dirname[len(home_slug) :].lstrip("-") or dirname
    return dirname


def _extract_timestamp(row: dict, ts_first: float, ts_last: float) -> tuple[float, float]:
    """Fold one row's ``timestamp`` into a running (min, max) pair."""
    ts_raw = row.get("timestamp")
    if not ts_raw:
        return ts_first, ts_last
    try:
        ts = parse_ts(ts_raw)
    except ValueError:
        return ts_first, ts_last
    return min(ts_first, ts), max(ts_last, ts)


def _row_to_call(row: dict, existing: dict[str, Call]) -> Call | None:
    """Build a :class:`Call` from one JSONL row, or ``None`` if it carries no new usage."""
    message = row.get("message") or {}
    usage = message.get("usage") or {}
    if not usage:
        return None
    message_id = message.get("id") or f"anon-{len(existing)}"
    if message_id in existing:
        return None
    blocks = [b for b in (message.get("content") or []) if isinstance(b, dict)]
    return Call(
        message_id=message_id,
        model=message.get("model") or "unknown",
        usage=usage,
        tools=[b.get("name", "?") for b in blocks if b.get("type") == "tool_use"],
        sidechain=bool(row.get("isSidechain")),
    )


def _parse_rows(path: Path) -> tuple[dict[str, Call], dict[str, int], float, float]:
    """Read one transcript file, deduplicating usage rows by message id.

    Shared by the main-loop file and every subagent file — both carry the identical row shape. ``isSidechain`` is
    already ``true`` on subagent rows, so no caller-side tagging is needed.
    """
    calls: dict[str, Call] = {}
    commands: dict[str, int] = {}
    ts_first, ts_last = float("inf"), 0.0

    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if "<command-name>" in line:
                for name in COMMAND_RE.findall(line):
                    commands[name] = commands.get(name, 0) + 1
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts_first, ts_last = _extract_timestamp(row, ts_first, ts_last)
            call = _row_to_call(row, calls)
            if call is not None:
                calls[call.message_id] = call
    return calls, commands, ts_first, ts_last


def _load_agent_spend(jf: Path, sub_calls: dict[str, Call]) -> AgentSpend:
    """Build one :class:`AgentSpend` from a subagent transcript's calls + sibling meta.json."""
    meta_path = jf.parent / f"{jf.stem}.meta.json"
    agent_type, desc = "?", ""
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            agent_type = meta.get("agentType") or "?"
            desc = meta.get("description") or ""
        except (json.JSONDecodeError, OSError):
            pass
    spend = sum(cost(c.usage, c.model) for c in sub_calls.values())
    return AgentSpend(agent_type, desc, len(sub_calls), spend)


def load_session(main_path: Path) -> Session:
    """Parse one session: its main-loop file plus every subagent transcript.

    Subagent files live at ``<main_path stem>/subagents/agent-*.jsonl``, sibling to the flat ``<session-id>.jsonl``.
    Each has a matching ``agent-*.meta.json`` carrying ``agentType`` and ``description``, used for the per-agent cost
    rollup — more reliable than scanning the main transcript for ``Agent`` tool_use blocks, which misses agents spawned
    through the ``Workflow`` tool.
    """
    calls_by_id, commands, ts_first, ts_last = _parse_rows(main_path)
    calls = list(calls_by_id.values())

    agent_spends: list[AgentSpend] = []
    subagents_dir = main_path.parent / main_path.stem / "subagents"
    if subagents_dir.is_dir():
        for jf in sorted(subagents_dir.glob("agent-*.jsonl")):
            sub_calls, _, sub_first, sub_last = _parse_rows(jf)
            calls.extend(sub_calls.values())
            ts_first, ts_last = min(ts_first, sub_first), max(ts_last, sub_last)
            agent_spends.append(_load_agent_spend(jf, sub_calls))

    return Session(
        path=main_path,
        calls=calls,
        commands=commands,
        agent_spends=agent_spends,
        ts_first=ts_first,
        ts_last=ts_last,
    )


def discover_sessions(root: Path) -> list[Path]:
    """Return top-level session transcript paths — one per session id.

    Only ``<project>/*.jsonl`` at depth 1 counts as a session; a plain
    ``rglob`` would also match ``<sid>/subagents/agent-*.jsonl``, double
    counting every subagent transcript as if it were its own session.

    Examples:
        >>> discover_sessions(Path("/does/not/exist"))
        []
    """
    root = root.expanduser()
    if root.is_file():
        return [root]
    if not root.is_dir():
        return []
    found: list[Path] = []
    for project_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        found.extend(sorted(project_dir.glob("*.jsonl")))
    return found


def totals(calls: list[Call]) -> tuple[int, int, int]:
    """Sum (cache_read, cache_write, output) over deduplicated calls.

    Examples:
        >>> totals([Call("m1", "opus", {"cache_read_input_tokens": 100, "output_tokens": 5})])
        (100, 0, 5)
        >>> totals([])
        (0, 0, 0)
    """
    return (
        sum(c.cache_read for c in calls),
        sum(c.cache_write for c in calls),
        sum(c.output for c in calls),
    )


def cold_start_share(calls: list[Call]) -> float:
    """Fraction of cache-write tokens spent on cold starts (no cache read).

    A cold start is a full context rebuild: session open, or a mid-run
    ``/clear``. On one measured review two such calls carried 74% of all
    write tokens — the evidence behind "never ``/clear`` mid-run".

    Examples:
        >>> cold = Call("m1", "opus", {"cache_creation_input_tokens": 180_000})
        >>> warm = Call("m2", "opus", {"cache_creation_input_tokens": 20_000, "cache_read_input_tokens": 150_000})
        >>> round(cold_start_share([cold, warm]), 2)
        0.9
        >>> cold_start_share([])
        0.0
    """
    written = sum(c.cache_write for c in calls)
    if not written:
        return 0.0
    return sum(c.cache_write for c in calls if c.cache_read == 0) / written


@dataclass
class Bucket:
    """Token and cost totals for one ``(scope, tier)`` report row.

    ``cost_usd`` is summed per call, never re-priced from the token totals: the Haiku 5.5 prompt-size band and the
    cache-write duration split are per-request facts the totals no longer carry, and one tier spans several models.
    """

    input: int = 0
    output: int = 0
    cache_write: int = 0
    cache_read: int = 0
    cost_usd: float = 0.0

    def add(self, call: Call) -> None:
        """Fold one deduplicated call into the totals."""
        self.input += call.usage.get("input_tokens", 0)
        self.output += call.output
        self.cache_write += call.cache_write
        self.cache_read += call.cache_read
        self.cost_usd += cost(call.usage, call.model)


def bucket(calls: list[Call]) -> dict[tuple[str, str], Bucket]:
    """Group deduplicated calls into ``(main|sidechain, tier)`` buckets.

    Examples:
        >>> calls = [Call("m1", "claude-opus-5", {"output_tokens": 100}),
        ...          Call("m2", "claude-haiku-4-5", {"output_tokens": 7}, sidechain=True)]
        >>> got = bucket(calls)
        >>> sorted(got)
        [('main', 'opus'), ('sidechain', 'haiku')]
        >>> got[("main", "opus")].cost_usd
        0.0025
        >>> bucket([])
        {}
    """
    buckets: dict[tuple[str, str], Bucket] = {}
    for call in calls:
        key = ("sidechain" if call.sidechain else "main", tier(call.model))
        buckets.setdefault(key, Bucket()).add(call)
    return buckets


def aggregate_commands(sessions: list[Session]) -> dict[str, dict[str, float]]:
    """Aggregate runs, agent spawns and cost per slash command.

    A session invoking several commands attributes its whole cost and
    spawn count to *each* of them, so the money column ranks which skill
    to look at first — it is an upper bound, never a total to sum.

    Examples:
        >>> import types
        >>> s1 = Session(Path("s1.jsonl"), [], {"/oss:review": 1, "/oss:resolve": 2}, [])
        >>> s2 = Session(Path("s2.jsonl"), [], {"/oss:resolve": 1}, [])
        >>> got = aggregate_commands([s1, s2])
        >>> sorted(got)
        ['/oss:resolve', '/oss:review']
        >>> got["/oss:resolve"]["runs"]
        3
    """
    out: dict[str, dict[str, float]] = {}
    for session in sessions:
        for name, count in session.commands.items():
            if not name.startswith("/") or ":" not in name:
                continue
            row = out.setdefault(name, {"runs": 0, "agents": 0, "cost": 0.0})
            row["runs"] += count
            row["agents"] += session.agent_count
            row["cost"] += session.total_cost
    return out


def _fmt_usd(v: float) -> str:
    """Format a dollar amount with thousands separators and two decimal places.

    Examples:
        >>> _fmt_usd(1234.5)
        '$1,234.50'
        >>> _fmt_usd(0)
        '$0.00'
    """
    return f"${v:,.2f}"


def _find_session(root: Path, session_id: str) -> Path | None:
    """Locate a session's transcript by id or id-prefix under ``root``."""
    for path in discover_sessions(root):
        if path.stem == session_id or path.stem.startswith(session_id):
            return path
    return None


def render_session_detail(session: Session, *, top_n: int, every: int = 10) -> list[str]:
    """Render the ``--session-id`` deep-dive: cost buckets, cache rebuilds, agent roster."""
    lines = [
        "## Tokens & cost",
        "",
        f"### Session `{session.path.stem}` — {session.project}",
        "",
    ]
    buckets = bucket(session.calls)
    lines += ["| scope | tier | in | out | cache_w | cache_r | cost |", "|---|---|---:|---:|---:|---:|---:|"]
    total = 0.0
    for (side, tr), b in sorted(buckets.items()):
        total += b.cost_usd
        lines.append(
            f"| {side} | {tr} | {b.input:,} | {b.output:,} | {b.cache_write:,} | {b.cache_read:,} "
            f"| {_fmt_usd(b.cost_usd)} |"
        )
    lines += [f"| **total** | | | | | | **{_fmt_usd(total)}** |", ""]

    if session.agent_spends:
        lines += ["### Agent roster", "", "| agent type | files | cost |", "|---|---:|---:|"]
        by_type: dict[str, list[AgentSpend]] = {}
        for a in session.agent_spends:
            by_type.setdefault(a.agent_type, []).append(a)
        for t, spends in sorted(by_type.items(), key=lambda kv: -sum(s.cost_usd for s in kv[1])):
            lines.append(f"| `{t}` | {len(spends)} | {_fmt_usd(sum(s.cost_usd for s in spends))} |")
        lines.append("")

    calls = session.calls
    if calls:
        cr, cw, out = totals(calls)
        lines += [
            f"cache_read {cr:,} tok, cache_write {cw:,} tok, output {out:,} tok "
            f"across {len(calls)} deduplicated calls (main + subagent)",
            "",
            f"### Top {top_n} calls by cache_write (rebuilds)",
            "",
            "| cache_write | cache_read | output | tools |",
            "|---:|---:|---:|---|",
        ]
        ranked = sorted(calls, key=lambda c: -c.cache_write)[:top_n]
        for c in ranked:
            tools = ",".join(c.tools)[:44] or "-"
            lines.append(f"| {c.cache_write:,} | {c.cache_read:,} | {c.output:,} | {tools} |")
        if cw:
            share = cold_start_share(calls)
            lines.append("")
            lines.append(f"Cold starts (cache_read == 0) account for {share:.0%} of all cache-write tokens.")
    lines.append("")
    return lines


def render_window(sessions: list[Session], *, top_n: int, since_spec: str) -> list[str]:
    """Render the window-ranking report: sessions by cost, plus per-command rollup."""
    lines = ["## Tokens & cost", "", f"Window: last {since_spec}, {len(sessions)} session(s) with usage.", ""]
    ranked = sorted(sessions, key=lambda s: -s.total_cost)
    lines += [
        "### Sessions ranked by cost",
        "",
        "| session | project | agents | main $ | subagent $ | total $ |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for s in ranked[:top_n]:
        main_cost = s.total_cost - s.subagent_cost
        lines.append(
            f"| `{s.path.stem[:8]}` | {s.project} | {s.agent_count} | "
            f"{_fmt_usd(main_cost)} | {_fmt_usd(s.subagent_cost)} | {_fmt_usd(s.total_cost)} |"
        )
    grand_total = sum(s.total_cost for s in sessions)
    lines += ["", f"**Window total: {_fmt_usd(grand_total)}** across all {len(sessions)} session(s) above."]

    cmd_totals = aggregate_commands(sessions)
    if cmd_totals:
        lines += [
            "",
            "### Per-command rollup (upper bound — see notes)",
            "",
            "| command | runs | agents | session $ |",
            "|---|---:|---:|---:|",
        ]
        for name, row in sorted(cmd_totals.items(), key=lambda kv: -kv[1]["runs"])[:top_n]:
            lines.append(f"| `{name}` | {int(row['runs'])} | {int(row['agents'])} | {_fmt_usd(row['cost'])} |")

    lines += [
        "",
        "### Notes",
        "",
        "- Prices are public list rates — dollar figures are proportional truth, not a billing statement.",
        "- Per-command `session $` attributes a session's *entire* cost to every command it ran — ranks, never sums.",
        "- Subagent spend is included via each session's `subagents/agent-*.jsonl` transcripts.",
        "",
    ]
    return lines


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Session token/cost analyzer.")
    p.add_argument("--projects-root", default="~/.claude/projects")
    p.add_argument("--since", default="24h", help="Window: NNs|NNm|NNh|NNd")
    p.add_argument("--session-id", default=None, help="Full id or prefix; drills into one session")
    p.add_argument("--top-n", type=int, default=10)
    p.add_argument("--output", default=None)
    return p


def _run_session_mode(root: Path, args: argparse.Namespace) -> list[str] | int:
    """Render the ``--session-id`` deep-dive, or an error code."""
    path = _find_session(root, args.session_id)
    if path is None:
        sys.stderr.write(f"no session matching {args.session_id!r} under {root}\n")
        return 1
    session = load_session(path)
    if not session.calls:
        sys.stderr.write(f"session {path.stem} has no usage rows\n")
        return 1
    _warn_unpriced(session.calls)
    return render_session_detail(session, top_n=args.top_n)


def _collect_window_sessions(root: Path, cutoff: float) -> list[Session]:
    """Load every session under ``root`` whose activity falls at/after ``cutoff``.

    A file-mtime pre-check skips parsing anything provably older than the window before paying for a full transcript
    read.
    """
    sessions: list[Session] = []
    for candidate in discover_sessions(root):
        try:
            if candidate.stat().st_mtime < cutoff:
                continue
        except OSError:
            continue
        session = load_session(candidate)
        if session.calls and session.ts_last >= cutoff:
            sessions.append(session)
    return sessions


def _run_window_mode(root: Path, args: argparse.Namespace) -> list[str] | int:
    """Render the window-ranking report, or an error code."""
    cutoff = time.time() - parse_since(args.since)
    sessions = _collect_window_sessions(root, cutoff)
    if not sessions:
        sys.stderr.write(f"no sessions with usage found in window --since={args.since}\n")
        return 1
    _warn_unpriced([call for session in sessions for call in session.calls])
    return render_window(sessions, top_n=args.top_n, since_spec=args.since)


def _write_report(lines: list[str], output: str | None) -> Path:
    """Write rendered ``lines`` to ``output`` (or a timestamped default) and return the path."""
    report = "\n".join(lines) + "\n"
    if output:
        out = Path(output).expanduser()
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
        out = Path(f".reports/profile/{stamp}/cost.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    """CLI entry.

    Writes a markdown ``## Tokens & cost`` section to ``--output``.
    """
    args = _build_parser().parse_args(argv)
    root = Path(args.projects_root).expanduser()

    result = _run_session_mode(root, args) if args.session_id else _run_window_mode(root, args)
    if isinstance(result, int):
        return result

    out = _write_report(result, args.output)
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        "→".encode(enc)
        print(f"→ {out}")
    except (UnicodeEncodeError, LookupError):
        print(f"-> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
