#!/usr/bin/env python
"""Bucket Claude Code session clock time into an auditable Markdown report.

Purpose:
    Attribute recorded session wall time to local tools, agent spawns, skills, user
    prompts, and wall time without observed activity.

Scope:
    Read ``~/.claude/logs/{timings,invocations}.jsonl`` from Foundry's timing hooks,
    normalize duration fields, and render a report without invoking agents or modifying logs.

Usage:
    Run ``python ${CLAUDE_PLUGIN_ROOT}/bin/timing_analyzer.py --since 24h``; use
    ``--output`` and ``--session-id`` to select the report destination and scope.

Outputs:
    Write one Markdown report, including clipped-duration counts in its legend, and
    print its path as ``→ <path>`` (or an ASCII arrow when needed by the terminal).

Failure:
    Invalid duration specifications raise ``ValueError``. An empty session window prints
    a diagnostic to stderr and returns 1; report-writing errors propagate to the caller.

Used by:
    Foundry's ``/foundry:profile`` skill and its session-time efficiency review.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from _report_io import write_report

#: Tool names treated as local work when bucketing time; unknown tools also fall into this bucket.
_LOCAL_TOOLS = frozenset(
    {
        "Bash",
        "Read",
        "Edit",
        "Write",
        "Grep",
        "Glob",
        "TaskCreate",
        "TaskUpdate",
        "TaskList",
        "ToolSearch",
        "NotebookEdit",
        "SendMessage",
        "EnterPlanMode",
        "ExitPlanMode",
    }
)
#: Tool names that spawn a subagent and are bucketed as agent time.
_AGENT_TOOLS = frozenset({"Task", "Agent"})
#: Tool names that invoke a skill and are bucketed as skill time.
_SKILL_TOOLS = frozenset({"Skill"})
#: Tool names that wait on the user and are bucketed as idle time.
_IDLE_TOOLS = frozenset({"AskUserQuestion"})

_BASH_CLIP_MS = 3_600_000  # 1h cap on runaway Bash
_BG_THRESHOLD_MS = 1_000  # below this an Agent row is suspect-background
_BG_MATCH_WINDOW_S = 60.0  # invocations pair must end within ±60s of timings ts
_BUCKET_PRIORITY = ("idle", "local", "agent", "skill")


@dataclass
class SessionStats:
    """Accumulated per-session bucket counters."""

    session_id: str
    first_ts: float = float("inf")
    last_ts: float = 0.0
    local_ms: int = 0
    agent_ms: int = 0
    skill_ms: int = 0
    idle_ms: int = 0
    top_calls: list[tuple[int, str, str, str]] = field(default_factory=list)
    skill_names: list[str] = field(default_factory=list)
    observed_spawn: bool = False
    unresolved_join: bool = False
    observation: tuple[float, float] | None = None

    @property
    def wall_ms(self) -> int:
        """Session wall-clock = last_ts − first_ts, ms."""
        if self.last_ts <= self.first_ts:
            return 0
        return round((self.last_ts - self.first_ts) * 1000)

    @property
    def compute_ms(self) -> int:
        """Return time backed by observed non-idle intervals."""
        return self.local_ms + self.agent_ms + self.skill_ms

    @property
    def unattributed_ms(self) -> int:
        """Return wall time with no tool activity recorded."""
        return max(0, self.wall_ms - self.compute_ms - self.idle_ms)


@dataclass(slots=True)
class TimingAccumulators:
    """The mutable structures one aggregation run folds every timing row into.

    Bundled so row-level helpers take a single accumulator instead of four
    positional collections that are only ever mutated in place and always passed
    on together.

    Examples:
        >>> acc = TimingAccumulators(pairs=[])
        >>> acc.sessions, acc.skill_events, acc.counters
        ({}, [], {'warnings': 0})
    """

    pairs: list[dict]
    sessions: dict[str, SessionStats] = field(default_factory=dict)
    skill_events: list[dict] = field(default_factory=list)
    counters: dict[str, int] = field(default_factory=lambda: {"warnings": 0})
    events: dict[str, list[tuple[int, str, int]]] = field(default_factory=dict)
    session_complete: bool = True


def parse_since(spec: str) -> float:
    """Parse a ``Nu`` duration spec into seconds (``s|m|h|d`` suffix).

    Args:
        spec: e.g. ``"24h"``, ``"7d"``, ``"30m"``, ``"600s"``.

    Returns:
        Total seconds.

    Examples:
        >>> parse_since("1h")
        3600.0
        >>> parse_since("0s")
        0.0
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

    Args:
        s: ISO-8601 string with trailing ``Z``.

    Returns:
        Seconds since epoch (UTC).

    Examples:
        >>> parse_ts("1970-01-01T00:00:00.000Z")
        0.0
        >>> parse_ts("1970-01-01T00:00:01Z")
        1.0
    """
    iso = s.replace("Z", "+00:00")
    return datetime.fromisoformat(iso).timestamp()


def iter_jsonl(path: Path) -> Iterator[dict]:
    """Yield decoded JSON objects from a JSONL file, skipping blanks/errors.

    Args:
        path: Source JSONL path. Missing file yields nothing.

    Yields:
        One ``dict`` per valid line.

    Examples:
        >>> import tempfile
        >>> with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
        ...     _ = fh.write('{"a":1}\\n\\nnot-json\\n{"b":2}\\n')
        ...     name = fh.name
        >>> rows = list(iter_jsonl(Path(name)))
        >>> [r.get("a") for r in rows], [r.get("b") for r in rows]
        ([1, None], [None, 2])
    """
    if not path.exists():
        return
    with path.open() as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def extract_skill_name(args: str | None) -> str | None:
    """Pull ``skill=<name>`` from a ``Skill`` row's ``args`` field.

    Args:
        args: Value of the ``args`` field from a timings.jsonl row.

    Returns:
        Skill name slug, or ``None``.

    Examples:
        >>> extract_skill_name("skill=codex:rescue args=...")
        'codex:rescue'
        >>> extract_skill_name(None) is None
        True
    """
    if not args:
        return None
    m = re.search(r"skill=([\w:.-]+)", args)
    return m.group(1) if m else None


def extract_agent_desc(args: str | None) -> tuple[str | None, str | None]:
    """Pull ``type=<agent>`` and ``desc=<desc>`` from a Task/Agent ``args``.

    Args:
        args: ``args`` field of a timings.jsonl Task/Agent row.

    Returns:
        ``(agent_type, description)``, either may be ``None``.

    Examples:
        >>> extract_agent_desc("type=foundry:curator desc=Curator audit")
        ('foundry:curator', 'Curator audit')
        >>> extract_agent_desc("")
        (None, None)
    """
    if not args:
        return None, None
    a = re.search(r"type=([\w:.-]+)", args)
    d = re.search(r"desc=(.+)$", args)
    return (a.group(1) if a else None, d.group(1).strip() if d else None)


def classify_bucket(tool: str) -> str:
    """Map a tool name to one of ``local|agent|skill|idle``.

    Unknown tools fall back to ``"local"`` so they remain accounted for.

    Args:
        tool: tool name as recorded in timings.jsonl.

    Returns:
        Bucket label.

    Examples:
        >>> classify_bucket("Bash"), classify_bucket("Agent"), classify_bucket("Skill")
        ('local', 'agent', 'skill')
        >>> classify_bucket("AskUserQuestion"), classify_bucket("Mystery")
        ('idle', 'local')
    """
    if tool in _AGENT_TOOLS:
        return "agent"
    if tool in _SKILL_TOOLS:
        return "skill"
    if tool in _IDLE_TOOLS:
        return "idle"
    return "local"


def clip_duration(tool: str, raw_ms: int) -> int:
    """Apply per-tool clip (Bash > 1h is treated as 1h to suppress runaways).

    Args:
        tool: tool name.
        raw_ms: raw duration_ms from the log row.

    Returns:
        Clipped, non-negative ms.

    Examples:
        >>> clip_duration("Bash", 5_000)
        5000
        >>> clip_duration("Bash", 9_999_999_999)
        3600000
        >>> clip_duration("Read", -5)
        0
    """
    if tool == "Bash" and raw_ms > _BASH_CLIP_MS:
        return _BASH_CLIP_MS
    return max(0, raw_ms)


def build_invocation_pairs(invocations_path: Path, *, end: float = float("inf")) -> list[dict]:
    """Pair ``started`` and ``completed`` Task events from invocations.jsonl.

    Pairing is FIFO per ``(project, agent)`` because invocations.jsonl
    carries no ``session_id`` or ``tool_use_id``.

    Args:
        invocations_path: path to ``invocations.jsonl``.
        end: exclusive POSIX-second bound on invocation events.

    Returns:
        List of ``{agent, project, desc, start, end, wall_ms}`` dicts.

    Examples:
        >>> import tempfile, json
        >>> with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
        ...     started = {"event": "started", "tool": "Task", "agent": "x", "desc": "d",
        ...                "ts": "1970-01-01T00:00:00Z", "project": "p"}
        ...     completed = {"event": "completed", "tool": "Task", "agent": "x",
        ...                  "ts": "1970-01-01T00:00:10Z", "project": "p"}
        ...     _ = fh.write(json.dumps(started) + "\\n")
        ...     _ = fh.write(json.dumps(completed) + "\\n")
        ...     name = fh.name
        >>> pairs = build_invocation_pairs(Path(name))
        >>> pairs[0]["wall_ms"]
        10000
    """
    queues: dict[tuple[str, str], list[dict]] = {}
    pairs: list[dict] = []
    for row in iter_jsonl(invocations_path):
        if row.get("tool") != "Task":
            continue
        ev = row.get("event")
        agent = row.get("agent") or ""
        project = row.get("project") or ""
        ts = row.get("ts")
        if not ts or ev not in {"started", "completed"}:
            continue
        key = (project, agent)
        if ev == "completed" and not queues.get(key):
            continue
        stamp = parse_ts(ts)
        if stamp >= end:
            continue
        if ev == "started":
            entry = {
                "agent": agent,
                "project": project,
                "desc": row.get("desc") or "",
                "start": stamp,
                "end": None,
                "wall_ms": None,
            }
            queues.setdefault(key, []).append(entry)
            pairs.append(entry)
        else:
            entry = queues[key].pop(0)
            entry["end"] = stamp
            entry["wall_ms"] = int((entry["end"] - entry["start"]) * 1000)
    return [p for p in pairs if p["wall_ms"] is not None]


def resolve_agent_ms(row: dict, pairs: list[dict]) -> int:
    """Return effective ms for an Agent/Task row, substituting bg pair on hit.

    For ``duration_ms < _BG_THRESHOLD_MS`` (the run_in_background
    false-zero case), look up a matching ``(agent, desc)`` pair in
    ``pairs`` whose end-time is within ``±_BG_MATCH_WINDOW_S`` seconds
    of the timings row's ts.  Returns the original ``duration_ms`` when
    no pair matches.

    Args:
        row: a single timings.jsonl Task/Agent row.
        pairs: output of ``build_invocation_pairs``.

    Returns:
        Effective wall-time ms.

    Examples:
        >>> row = {"ts": "1970-01-01T00:00:10Z", "duration_ms": 50,
        ...        "args": "type=foundry:curator desc=Curator audit"}
        >>> pairs = [{"agent": "foundry:curator", "desc": "Curator audit batch",
        ...           "start": 0.0, "end": 10.0, "wall_ms": 10000}]
        >>> resolve_agent_ms(row, pairs)
        10000
        >>> resolve_agent_ms({"ts": "1970-01-01T00:00:10Z", "duration_ms": 5000, "args": ""}, [])
        5000
    """
    return _resolve_agent_timing(row, pairs)[0]


def _resolve_agent_timing(row: dict, pairs: list[dict]) -> tuple[int, bool, dict | None]:
    """Return duration, unresolved-join status and the matching invocation if available."""
    raw = int(row.get("duration_ms") or 0)
    if raw >= _BG_THRESHOLD_MS:
        return raw, False, None
    agent, desc = extract_agent_desc(row.get("args"))
    if not agent or not desc:
        return raw, True, None
    row_end = parse_ts(row["ts"])
    best = None
    best_dt = _BG_MATCH_WINDOW_S
    for p in pairs:
        if p["agent"] != agent:
            continue
        if desc not in p.get("desc", "") and p.get("desc", "") not in desc:
            continue
        dt = abs((p.get("end") or 0.0) - row_end)
        if dt <= best_dt:
            best_dt = dt
            best = p
    return (raw, True, None) if best is None else (best["wall_ms"], False, best)


def _allocate_wall(stats: SessionStats, events: list[tuple[int, str, int]]) -> None:
    """Assign each observed millisecond to the highest-priority active bucket."""
    active: Counter[str] = Counter()
    totals: Counter[str] = Counter()
    previous = 0
    for point, bucket, change in sorted(events):
        # Idle takes precedence over compute, and tool activity over enclosing Skill calls.
        owner = next((name for name in _BUCKET_PRIORITY if active[name]), None)
        if owner is not None:
            totals[owner] += point - previous
        active[bucket] += change
        previous = point
    stats.idle_ms, stats.local_ms, stats.agent_ms, stats.skill_ms = (totals[name] for name in _BUCKET_PRIORITY)


def _call_bounds(row: dict, ts: float, pairs: list[dict]) -> tuple[float, float, bool]:
    """Return the observed interval and whether the background duration remains unknown."""
    tool = row.get("tool") or "?"
    if tool in _AGENT_TOOLS:
        ms, unresolved, match = _resolve_agent_timing(row, pairs)
        if match is not None:
            return match["start"], match["end"], unresolved
    else:
        ms, unresolved = clip_duration(tool, int(row.get("duration_ms") or 0)), False
    return ts - max(0, ms) / 1000, ts, unresolved


def _accumulate_row(
    row: dict,
    cutoff: float,
    end: float,
    session_filter: str | None,
    top_n: int,
    acc: TimingAccumulators,
) -> None:
    """Clip known intervals and track whether a selected session was fully observed."""
    sid, ts_raw = row.get("session_id"), row.get("ts")
    if not sid or (session_filter and sid != session_filter):
        return
    if not ts_raw:
        acc.session_complete = False
        return
    ts = parse_ts(ts_raw)
    tool = row.get("tool") or "?"
    raw_ms = int(row.get("duration_ms") or 0)
    first, last, unresolved = _call_bounds(row, ts, acc.pairs)
    if min(first, ts - max(0, raw_ms) / 1000) < cutoff or max(last, ts) >= end or unresolved:
        acc.session_complete = False
    start, stop = max(cutoff, first), min(end, last)
    if start >= stop and not (first == last and cutoff <= first < end):
        return
    s = acc.sessions.setdefault(sid, SessionStats(session_id=sid))
    s.observed_spawn |= tool in _AGENT_TOOLS
    s.unresolved_join |= unresolved
    if tool == "Bash" and raw_ms > _BASH_CLIP_MS:
        acc.counters["warnings"] = acc.counters.get("warnings", 0) + 1
    bucket = classify_bucket(tool)
    start_ms, stop_ms = round(start * 1000), round(stop * 1000)
    ms = stop_ms - start_ms
    s.first_ts = min(s.first_ts, start_ms / 1000)
    s.last_ts = max(s.last_ts, stop_ms / 1000)
    if ms:
        acc.events.setdefault(sid, []).extend(((start_ms, bucket, 1), (stop_ms, bucket, -1)))
    if bucket == "skill":
        name = extract_skill_name(row.get("args"))
        if name:
            s.skill_names.append(name)
            acc.skill_events.append({"session": sid, "skill": name, "ms": ms, "ts": ts})
    args_snippet = (row.get("args") or "")[:80]
    s.top_calls.append((ms, tool, args_snippet, ts_raw))
    s.top_calls.sort(key=lambda t: -t[0])
    del s.top_calls[top_n:]


def aggregate_sessions(
    timings_path: Path,
    invocations_path: Path,
    *,
    cutoff: float,
    end: float = float("inf"),
    session_filter: str | None = None,
    top_n: int = 5,
) -> tuple[dict[str, SessionStats], list[dict], int]:
    """Walk both JSONL files and bucket every row.

    Args:
        timings_path: ``timings.jsonl`` path.
        invocations_path: ``invocations.jsonl`` path.
        cutoff: inclusive start of the observation in POSIX seconds.
        end: exclusive end of the observation in POSIX seconds.
        session_filter: only aggregate this session_id (optional).
        top_n: keep this many slowest calls per session.

    Returns:
        ``(sessions, skill_events, bash_clip_warnings)``.

    Examples:
        >>> import tempfile, json
        >>> def _w(rows):
        ...     fh = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        ...     for r in rows: _ = fh.write(json.dumps(r) + "\\n")
        ...     fh.close(); return Path(fh.name)
        >>> t = _w([{"ts":"2030-01-01T00:00:00Z","tool":"Bash","duration_ms":100,
        ...          "session_id":"s1"}])
        >>> i = _w([])
        >>> ss, _, _ = aggregate_sessions(t, i, cutoff=0.0)
        >>> ss["s1"].local_ms
        100
    """
    acc = TimingAccumulators(pairs=build_invocation_pairs(invocations_path))
    for row in iter_jsonl(timings_path):
        _accumulate_row(row, cutoff, end, session_filter, top_n, acc)
    for sid, stats in acc.sessions.items():
        _allocate_wall(stats, acc.events.get(sid, []))
        if session_filter and acc.session_complete:
            stats.observation = stats.first_ts, stats.last_ts
    return acc.sessions, acc.skill_events, acc.counters["warnings"]


def _fmt_hms(ms: int) -> str:
    """Format milliseconds as zero-padded hours, minutes, and seconds.

    Clamp negative values to zero and discard subsecond remainders. Hours may
    exceed two digits for long durations.

    Examples:
        >>> _fmt_hms(0)
        '00:00:00'
        >>> _fmt_hms(3_661_000)
        '01:01:01'
        >>> _fmt_hms(-1)
        '00:00:00'
    """
    s = max(0, ms) // 1000
    h, r = divmod(s, 3600)
    m, sec = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}"


def _fmt_pct(part: int, whole: int) -> str:
    """Format ``part / whole`` as one decimal percent with a zero denominator guard.

    Examples:
        >>> _fmt_pct(1, 4)
        '25.0%'
        >>> _fmt_pct(1, 0)
        '0.0%'
    """
    return f"{(100 * part / whole):.1f}%" if whole else "0.0%"


@dataclass(frozen=True)
class ConfidenceGap:
    """One named limitation of the clock report and its share of the confidence shortfall."""

    deduction: float
    #: Short label for the one-line ``Confidence:`` header field.
    label: str
    #: Full gap text for the ``## Confidence`` block.
    text: str


_AGENT_INTERNAL_GAP = ConfidenceGap(
    0.07,
    "clock log excludes internal subagent timing",
    "The main-process hook cannot observe internal subagent tool calls or split their elapsed time into buckets",
)
_AGENT_JOIN_GAP = ConfidenceGap(
    0.05,
    "background agent join by substring match",
    "Background agent join via (agent, desc) substring match can mispair concurrent spawns of the same type and description",
)
_UNRESOLVED_JOIN_GAP = ConfidenceGap(
    _AGENT_JOIN_GAP.deduction,
    "unresolved background-agent joins",
    "Unresolved background-agent joins can leave activity unattributed. " + _AGENT_JOIN_GAP.text,
)
#: Gap that always applies: the clock-side source logs carry no model field.
_MODEL_GAP = ConfidenceGap(
    0.03,
    "no per-model-tier clock breakdown",
    "`model` field 100% null in source logs → no per-model-tier breakdown",
)


def confidence_gaps(
    agent_ms: int, *, observed_spawn: bool = False, unresolved_join: bool = False
) -> list[ConfidenceGap]:
    """Return the confidence gaps that apply to a report, so the score follows from named gaps only.

    Observed spawns bring in timing gaps even when they contribute zero milliseconds. An unresolved join gives
    a more specific explanation for the join deduction.

    Args:
        agent_ms: Summed agent and subagent spawn wall time across the reported sessions, in milliseconds.
        observed_spawn: At least one in-scope Agent or Task timing row exists.
        unresolved_join: At least one short agent timing lacks a matching completion.

    Returns:
        Applicable gaps, largest deduction first.

    Examples:
        >>> [gap.deduction for gap in confidence_gaps(0)]
        [0.03]
        >>> [gap.deduction for gap in confidence_gaps(1)]
        [0.07, 0.05, 0.03]
    """
    if unresolved_join:
        return [_AGENT_INTERNAL_GAP, _UNRESOLVED_JOIN_GAP, _MODEL_GAP]
    if observed_spawn or agent_ms > 0:
        return [_AGENT_INTERNAL_GAP, _AGENT_JOIN_GAP, _MODEL_GAP]
    return [_MODEL_GAP]


def confidence_score(gaps: list[ConfidenceGap]) -> float:
    """Return the report score: 1.0 minus the deductions of its named gaps.

    Examples:
        >>> confidence_score(confidence_gaps(0))
        0.97
        >>> confidence_score(confidence_gaps(1))
        0.85
    """
    return round(1.0 - sum(gap.deduction for gap in gaps), 2)


def _render_yaml(
    sessions: dict[str, SessionStats],
    total_wall: int,
    since_spec: str,
    output_hint: str,
    gaps: list[ConfidenceGap] | None = None,
) -> str:
    """Render the report's ``---`` metadata header, one ``Key: value`` line per field.

    Every line must be a ``Key: value`` pair and ``Title`` must be present: the
    ``enforce-profile-header.js`` follow-up gate (via ``report-header-table.js``)
    treats a header with any other line, or without ``Title``, as incomplete and
    denies the profile follow-up question for that report.

    Args:
        sessions: Aggregated per-session stats; only their count is reported.
        total_wall: Summed wall time in milliseconds across the sessions.
        since_spec: The ``--since`` window as given, e.g. ``24h``.
        output_hint: Report path shown in the ``Path:`` field.
        gaps: Applicable confidence gaps; ``None`` means a window without agent spawns.

    Returns:
        The header block, opening and closing ``---`` lines included.

    Examples:
        >>> header = _render_yaml({}, 0, "24h", "report.md")
        >>> header.splitlines()[1].split(":", 1)[0]
        'Title'
        >>> all(":" in line for line in header.splitlines()[1:-1])
        True
        >>> [line for line in header.splitlines() if line.startswith("Confidence:")]
        ['Confidence: 0.97 — no per-model-tier clock breakdown']
    """
    today = time.strftime("%Y-%m-%d", time.gmtime())
    gaps = confidence_gaps(0) if gaps is None else gaps
    return (
        "---\n"
        "Title:      foundry:profile — session clock-time breakdown\n"
        f"Date:       {today}\n"
        f"Scope:      window={since_spec}, sessions={len(sessions)}, total_wall={_fmt_hms(total_wall)}\n"
        "Focus:      local-tool / agent-spawn / skill / askuser / unattributed wall-time split\n"
        "Agents:     timing_analyzer.py (no LLM agents — pure log read)\n"
        "Outcome:    INFORMATIONAL — data-only report\n"
        f"Confidence: {confidence_score(gaps):.2f} — {gaps[0].label}\n"
        "Next steps: re-run with --session-id <slowest> for drill-down\n"
        f"Path:       → {output_hint}\n"
        "---\n"
    )


def _render_headline(totals: dict[str, int]) -> list[str]:
    """Render the exclusive shares of observed compute and the excluded durations."""
    compute = totals["compute"]
    return [
        "## Headline split (shares of observed compute)",
        "",
        f"- Local tools: {_fmt_hms(totals['local'])} ({_fmt_pct(totals['local'], compute)})",
        f"- Agent / subagent spawns: {_fmt_hms(totals['agent'])} ({_fmt_pct(totals['agent'], compute)})",
        f"- Skill wall: {_fmt_hms(totals['skill'])} ({_fmt_pct(totals['skill'], compute)})",
        f"- Compute time (observed): {_fmt_hms(compute)}",
        f"- AskUserQuestion idle (excluded): {_fmt_hms(totals['idle'])}",
        f"- Unattributed wall time (excluded): {_fmt_hms(totals['unattributed'])}",
        "",
    ]


def _render_per_session(sessions: dict[str, SessionStats]) -> list[str]:
    """Show wall, compute shares, idle and unobserved gaps per session."""
    lines = [
        "## Per-session breakdown",
        "",
        "| session | wall | local% | agent% | skill% | unattributed | idle | top skill |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for s in sorted(sessions.values(), key=lambda x: -x.wall_ms)[:50]:
        top_skill = max(set(s.skill_names), key=s.skill_names.count) if s.skill_names else "—"
        ct = s.compute_ms
        lines.append(
            f"| `{s.session_id[:8]}` | {_fmt_hms(s.wall_ms)} | "
            f"{_fmt_pct(s.local_ms, ct)} | {_fmt_pct(s.agent_ms, ct)} | "
            f"{_fmt_pct(s.skill_ms, ct)} | {_fmt_hms(s.unattributed_ms)} | "
            f"{_fmt_hms(s.idle_ms)} | {top_skill} |"
        )
    lines.append("")
    return lines


def _render_per_skill(skill_events: list[dict]) -> list[str]:
    if not skill_events:
        return []
    by_skill: dict[str, list[int]] = {}
    for ev in skill_events:
        by_skill.setdefault(ev["skill"], []).append(ev["ms"])
    lines = [
        "## Per-skill rollup",
        "",
        "| skill | runs | total | mean | median | p90 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name in sorted(by_skill, key=lambda k: -sum(by_skill[k])):
        vals = sorted(by_skill[name])
        p90 = vals[int(0.9 * (len(vals) - 1))] if vals else 0
        lines.append(
            f"| `{name}` | {len(vals)} | {_fmt_hms(sum(vals))} | "
            f"{_fmt_hms(int(statistics.mean(vals)))} | "
            f"{_fmt_hms(int(statistics.median(vals)))} | {_fmt_hms(p90)} |"
        )
    lines.append("")
    return lines


def _render_top_n(sessions: dict[str, SessionStats], top_n: int) -> list[str]:
    all_calls: list[tuple[int, str, str, str, str]] = []
    for s in sessions.values():
        for ms, tool, args, ts in s.top_calls:
            all_calls.append((ms, tool, args, ts, s.session_id))
    if not all_calls:
        return []
    all_calls.sort(key=lambda t: -t[0])
    lines = [
        f"## Top-{top_n} longest single calls",
        "",
        "| ts | session | tool | duration | args |",
        "|---|---|---|---:|---|",
    ]
    for ms, tool, args, ts, sid in all_calls[:top_n]:
        safe = args.replace("|", "\\|").replace("`", "'")
        lines.append(f"| {ts} | `{sid[:8]}` | {tool} | {_fmt_hms(ms)} | `{safe}` |")
    lines.append("")
    return lines


def _render_legend(warnings: int) -> list[str]:
    """Describe the clock buckets and their allocation."""
    return [
        "## Legend",
        "",
        "- **Local tools**: Bash/Read/Edit/Write/Grep/Glob/TaskCreate etc — main-process work",
        "- **Agent**: Task/Agent spawn wall time; background agents resolved via invocations.jsonl pair",
        "- **Skill**: tool=Skill row duration (whole skill turn wall)",
        "- **Unattributed**: gaps between recorded intervals, excluded from compute",
        "- **Idle**: AskUserQuestion wall — pure human-wait, excluded from compute total",
        "- **Wall**: span of the observed intervals within each session",
        "- **Overlap**: idle takes priority, then local tools, agent calls and Skill calls",
        f"- **Bash clipped**: {warnings} row(s) clipped at 1h (runaway-shell guard)",
        "",
    ]


def _render_confidence(gaps: list[ConfidenceGap]) -> list[str]:
    """Render the ``## Confidence`` block whose deductions sum to ``1.0 - Score`` (Confidence Block rule).

    Examples:
        >>> _render_confidence(confidence_gaps(0))[1]
        '**Score**: 0.97 — high'
    """
    score = confidence_score(gaps)
    band = "high" if score >= 0.9 else "moderate" if score >= 0.85 else "low ⚠"
    return [
        "## Confidence",
        f"**Score**: {score:.2f} — {band}",
        "**Gaps**:",
        *(f"- (-{gap.deduction:.2f}) {gap.text}" for gap in gaps),
    ]


def render_report(
    sessions: dict[str, SessionStats],
    skill_events: list[dict],
    *,
    since_spec: str,
    top_n: int,
    warnings: int,
    output_hint: str = "report.md",
    window: tuple[float, float] | None = None,
) -> str:
    """Render the full markdown report string.

    Args:
        sessions: per-session bucket counters.
        skill_events: flat list of skill invocation events.
        since_spec: human spec passed for ``--since`` (placed in YAML).
        top_n: how many slowest calls to list at the bottom.
        warnings: Bash-clip warning count for the legend.
        output_hint: path placed in the YAML ``Path:`` line.
        window: inclusive start and exclusive end of the timing-row observation window in POSIX seconds.

    Returns:
        Complete markdown report (string with trailing newline).

    Examples:
        >>> r = render_report({}, [], since_spec="1h", top_n=5, warnings=0)
        >>> "Headline split" in r
        True
    """
    totals = {
        "local": sum(s.local_ms for s in sessions.values()),
        "agent": sum(s.agent_ms for s in sessions.values()),
        "skill": sum(s.skill_ms for s in sessions.values()),
        "idle": sum(s.idle_ms for s in sessions.values()),
        "wall": sum(s.wall_ms for s in sessions.values()),
        "compute": sum(s.compute_ms for s in sessions.values()),
        "unattributed": sum(s.unattributed_ms for s in sessions.values()),
    }
    gaps = confidence_gaps(
        totals["agent"],
        observed_spawn=any(s.observed_spawn for s in sessions.values()),
        unresolved_join=any(s.unresolved_join for s in sessions.values()),
    )
    parts: list[str] = [_render_yaml(sessions, totals["wall"], since_spec, output_hint, gaps), ""]
    if window is not None:
        parts.extend([f"Clock window (UTC epoch seconds): {window[0]!r} to {window[1]!r}", ""])
    elif len(sessions) == 1:
        observation = next(iter(sessions.values())).observation
        if observation is not None:
            parts.extend([f"Clock session (UTC epoch seconds): {observation[0]!r} to {observation[1]!r}", ""])
    parts.extend(_render_headline(totals))
    parts.extend(_render_per_session(sessions))
    parts.extend(_render_per_skill(skill_events))
    parts.extend(_render_top_n(sessions, top_n))
    parts.extend(_render_legend(warnings))
    parts.extend(_render_confidence(gaps))
    return "\n".join(parts) + "\n"


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Session clock-time analyzer.")
    p.add_argument("--timings", default="~/.claude/logs/timings.jsonl")
    p.add_argument("--invocations", default="~/.claude/logs/invocations.jsonl")
    p.add_argument("--since", default="24h", help="Window: NNs|NNm|NNh|NNd")
    p.add_argument("--session-id", default=None)
    p.add_argument("--project", default=None, help="Reserved — filtering not yet supported.")
    p.add_argument("--top-n", type=int, default=5)
    p.add_argument("--output", default=None)
    p.add_argument("--timeout", type=int, default=30, help="Reserved — no subprocess yet.")
    return p


def main(argv: list[str] | None = None) -> int:
    """Analyze timing logs and return the report path or process status.

    Reads logs, writes the report, and prints ``→ <path>`` on stdout.

    Args:
        argv: Argument list (defaults to ``sys.argv[1:]``).

    Returns:
        ``0`` on success, ``1`` when the window contains no sessions.
    """
    args = _build_parser().parse_args(argv)
    timings = Path(args.timings).expanduser()
    invocations = Path(args.invocations).expanduser()
    end = time.time()
    cutoff = end - parse_since(args.since)
    sessions, skill_events, warnings = aggregate_sessions(
        timings,
        invocations,
        cutoff=cutoff,
        end=end,
        session_filter=args.session_id,
        top_n=args.top_n,
    )
    if not sessions:
        sys.stderr.write(f"no sessions found in window --since={args.since}\n")
        return 1
    if args.output:
        out = Path(args.output).expanduser()
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
        out = Path(f".reports/profile/{stamp}/report.md")
    report = render_report(
        sessions,
        skill_events,
        since_spec=args.since,
        top_n=args.top_n,
        warnings=warnings,
        output_hint=str(out),
        window=(cutoff, end) if args.session_id is None else None,
    )
    write_report(out, report)
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        "→".encode(enc)
        print(f"→ {out}")
    except (UnicodeEncodeError, LookupError):
        print(f"-> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
