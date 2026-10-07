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
3. **Prices are public list rates**, hard-coded in ``PRICES``. The
   transcript records tokens only; effective plan rates may differ, so
   dollar figures are proportional truth, not a billing statement. Cache
   writes price at 12.5x cache reads, which is why a mid-run context
   rebuild (e.g. ``/clear``) dominates a session's cost line.

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

#: (input, output, cache_write_5m, cache_read) USD per million tokens — public list rates.
PRICES: dict[str, tuple[float, float, float, float]] = {
    "opus": (15.0, 75.0, 18.75, 1.50),
    "sonnet": (3.0, 15.0, 3.75, 0.30),
    "haiku": (1.0, 5.0, 1.25, 0.10),
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


def tier(model: str) -> str:
    """Map a model id onto its price tier.

    Matches on substring so dated and suffixed ids resolve. An unrecognised
    id prices as the most expensive tier rather than the cheapest — an
    overstated cost prompts investigation, an understated one hides it.

    Examples:
        >>> tier("claude-opus-5[1m]")
        'opus'
        >>> tier("claude-haiku-4-5-20251001")
        'haiku'
        >>> tier("some-unreleased-model")
        'opus'
    """
    for name in PRICES:
        if name in model:
            return name
    return "opus"


def cost(usage: dict, model: str = "opus") -> float:
    """Price one usage object in USD - missing keys count as zero.

    Examples:
        >>> round(cost({"output_tokens": 1_000_000}), 2)
        75.0
        >>> round(cost({"cache_creation_input_tokens": 1}) / cost({"cache_read_input_tokens": 1}), 3)
        12.5
        >>> cost({})
        0.0
    """
    p_in, p_out, p_cw, p_cr = PRICES[tier(model)]
    return (
        usage.get("input_tokens", 0) * p_in
        + usage.get("output_tokens", 0) * p_out
        + usage.get("cache_creation_input_tokens", 0) * p_cw
        + usage.get("cache_read_input_tokens", 0) * p_cr
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


def bucket(calls: list[Call]) -> dict[tuple[str, str], dict[str, int]]:
    """Group deduplicated calls into (main|sidechain, price tier) buckets.

    Examples:
        >>> calls = [Call("m1", "claude-opus-5", {"output_tokens": 100}),
        ...          Call("m2", "claude-haiku-4-5", {"output_tokens": 7}, sidechain=True)]
        >>> got = bucket(calls)
        >>> sorted(got)
        [('main', 'opus'), ('sidechain', 'haiku')]
        >>> bucket([])
        {}
    """
    buckets: dict[tuple[str, str], dict[str, int]] = {}
    for call in calls:
        key = ("sidechain" if call.sidechain else "main", tier(call.model))
        b = buckets.setdefault(key, {"in": 0, "out": 0, "cw": 0, "cr": 0})
        b["in"] += call.usage.get("input_tokens", 0)
        b["out"] += call.output
        b["cw"] += call.cache_write
        b["cr"] += call.cache_read
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
        p_in, p_out, p_cw, p_cr = PRICES[tr]
        line_cost = (b["in"] * p_in + b["out"] * p_out + b["cw"] * p_cw + b["cr"] * p_cr) / 1_000_000
        total += line_cost
        lines.append(
            f"| {side} | {tr} | {b['in']:,} | {b['out']:,} | {b['cw']:,} | {b['cr']:,} | {_fmt_usd(line_cost)} |"
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
