#!/usr/bin/env python3
"""Join tool targets to earlier complete module answers as an overlap proxy.

The legacy ``avoidance_count`` and ``rate`` keys count module/time overlaps,
not confirmed misuse, guard failures, or token savings. Source-body, test, and
diff inspection can legitimately overlap an earlier structural answer.
Version, project, runtime, and session must agree. Records without explicit
absolute project coordinates or a verified successful CLI outcome are counted
but excluded from joins. Log destinations never establish project identity.

The module-match rule is ported from ``guard-redundant-scan.py``: split the module
on ``.`` / ``/`` into segments, escape regex metacharacters, rejoin with the ``[./]``
separator class, and require the match not to be flanked by an identifier character.
This matches names, not intent. Batch children count as logical answers rather
than additional CLI invocations. Failed, stale, incomplete, and marked benchmark
answers do not support overlaps.

Usage:
    python join_avoidance.py --logs .cache/codemap/logs
    python join_avoidance.py --cli cli.jsonl --tools tools.jsonl --window-min 10
    python join_avoidance.py --logs .cache/codemap/logs --json

Exit codes:
    0 — success (including "no avoidance events" and "no logs found")
    2 — bad arguments (neither ``--logs`` nor a ``--cli``/``--tools`` pair given)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

DEFAULT_WINDOW_MIN = 10
# 50 MB per shard, matching MAX_INDEX_SIZE in scan-stats.py / smoke_test_index.py (CWE-400: DoS guard).
# Per-file cap only — the aggregate across every globbed shard stays uncapped.
MAX_LOG_SIZE = 50_000_000
_IDENT = "A-Za-z0-9_"
_RUNTIMES = ("claude", "codex", "direct")
_RUNTIME_KEY = "_join_avoidance_runtime"
_UNATTRIBUTED_RUNTIME = "unattributed"


@dataclass(frozen=True)
class CliAnswer:
    """One eligible complete module answer, with its evidence coordinates.

    Attributes:
        session: session id joining the cli and tool layers.
        ts: event time (UTC) parsed from the record's ``ts`` field.
        module: dotted module name the command answered.
    """

    session: str
    ts: datetime
    module: str
    runtime: str | None = None
    version: str | None = None
    project: str | None = None


@dataclass(frozen=True)
class ToolEvent:
    """One Grep/Read/Glob tool call recorded by log-tool-use.py.

    Attributes:
        session: session id joining the cli and tool layers.
        ts: event time (UTC) parsed from the record's ``ts`` field.
        tool: ``"Grep"`` | ``"Read"`` | ``"Glob"``.
        target: the tool's target string (pattern, path, or file_path).
    """

    session: str
    ts: datetime
    tool: str
    target: str
    runtime: str | None = None
    version: str | None = None
    project: str | None = None


@dataclass(frozen=True)
class AvoidanceEvent:
    """A tool target overlapping an earlier complete module answer, intent unknown.

    Attributes:
        session: session containing the overlap.
        module: the module codemap had answered completely.
        tool: the tool with an overlapping target; intent is unknown.
        target: the tool call's target string.
        answer_ts: when codemap answered ``module`` completely.
        tool_ts: when the overlapping tool call happened.
        gap_seconds: seconds between the answer and the tool call.
    """

    session: str
    module: str
    tool: str
    target: str
    answer_ts: datetime
    tool_ts: datetime
    gap_seconds: float
    runtime: str | None = None
    version: str | None = None
    project: str | None = None


@dataclass
class Summary:
    """Aggregate avoidance metrics for a debrief report.

    Attributes:
        window_min: the join window in minutes used to produce these counts.
        total_tool_events: every Grep/Read/Glob event considered.
        total_complete_answers: every ``query_complete: true`` cli answer considered.
        avoidance_events: matching tool targets, including legitimate inspection.
        per_session: session id → avoidance count.
        per_skill: skill name → avoidance count (only sessions attributable to a skill).
    """

    window_min: int
    total_tool_events: int
    total_complete_answers: int
    avoidance_events: list[AvoidanceEvent] = field(default_factory=list)
    per_session: dict[str, int] = field(default_factory=dict)
    per_skill: dict[str, int] = field(default_factory=dict)
    per_runtime: dict[str, dict[str, int | float | dict[str, int]]] = field(default_factory=dict)
    record_counts: dict[str, int] = field(default_factory=dict)

    @property
    def rate(self) -> float:
        """Fraction of eligible tool events overlapping answers, not confirmed avoidable work.

        Examples:
            >>> Summary(window_min=10, total_tool_events=0, total_complete_answers=0).rate
            0.0
            >>> s = Summary(window_min=10, total_tool_events=4, total_complete_answers=1)
            >>> s.avoidance_events = [None]  # one flagged event
            >>> s.rate
            0.25
        """
        if not self.total_tool_events:
            return 0.0
        return len(self.avoidance_events) / self.total_tool_events


def _parse_ts(value: object) -> datetime | None:
    """Parse an ISO-8601 ``...Z`` timestamp into an aware UTC datetime.

    Args:
        value: the record's ``ts`` field (expected ``"YYYY-MM-DDTHH:MM:SSZ"``).

    Returns:
        An aware UTC ``datetime``, or ``None`` when the value is missing/unparsable.

    Examples:
        >>> _parse_ts("2026-07-10T01:25:00Z").hour
        1
        >>> _parse_ts("not-a-date") is None
        True
        >>> _parse_ts(None) is None
        True
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _module_from_cli_result(record: dict) -> str:
    """Extract the queried module name from a cli record.

    Prefer emitted module/qname identity. For legacy records, accept only the
    immediate positional after a known module command; never guess from options.

    Args:
        record: a parsed ``cli.jsonl`` record.

    Returns:
        The dotted module name, or ``""`` when none can be determined.

    Examples:
        >>> _module_from_cli_result({"result": {"module": "pkg.auth"}})
        'pkg.auth'
        >>> _module_from_cli_result({"argv": ["rdeps", "pkg.auth"]})
        'pkg.auth'
        >>> _module_from_cli_result({"argv": ["central", "--top", "5"]})
        ''
    """
    result = record.get("result")
    if isinstance(result, dict):
        module = result.get("module") or result.get("qname")
        if isinstance(module, str) and module:
            return module.split("::", 1)[0]
    argv = record.get("argv")
    if isinstance(argv, list):
        module_commands = {
            "deps",
            "rdeps",
            "fn-rdeps",
            "fn-blast",
            "mock-rdeps",
            "uncovered",
            "coverage",
            "coverage-gap",
            "xrefs",
            "undocumented",
        }
        # Only a leading command is unambiguous without reproducing the engine parser.
        for command, token in zip(argv[:1], argv[1:2]):
            if (
                isinstance(command, str)
                and command in module_commands
                and isinstance(token, str)
                and not token.startswith("-")
            ):
                return token.split("::", 1)[0]
    return ""


def _query_complete(record: dict) -> bool:
    """Return whether a cli record reports a complete (exhaustive) answer.

    Checks ``result.index.query_complete`` first, then the legacy
    ``result.index.exhaustive`` alias, then the same two keys at ``result`` top
    level (the compact-diet coverage path emits them under ``index`` but the
    top-level check keeps the join robust to future schema moves).

    Args:
        record: a parsed ``cli.jsonl`` record.

    Returns:
        ``True`` only for an explicit completeness flag without failure/staleness/truncation.

    Examples:
        >>> _query_complete({"result": {"index": {"query_complete": True}}})
        True
        >>> _query_complete({"result": {"index": {"exhaustive": True}}})
        True
        >>> _query_complete({"result": {"index": {"query_complete": False}}})
        False
        >>> _query_complete({"result": {}})
        False
    """
    result = record.get("result")
    if not isinstance(result, dict) or result.get("error") or record.get("exit_code", 0) != 0:
        return False
    if any(
        isinstance(block, dict) and (block.get("stale") or block.get("root_mismatch") or block.get("truncated"))
        for block in (result.get("index"), result)
    ):
        return False
    for block in (result.get("index"), result):
        if isinstance(block, dict):
            for key in ("query_complete", "exhaustive"):
                if key in block:
                    return block[key] is True
    return False


def module_matches(module: str, text: str) -> bool:
    """Return True if *text* references *module* on identifier boundaries.

    Ported from ``guard-redundant-scan.py``: split the module on ``.``/``/``,
    escape regex metacharacters per segment, rejoin with the ``[./]`` class so
    both dotted and slashed forms match, and require the match not to be flanked
    by an identifier character. A plain substring test would falsely match
    ``pkg.auth`` inside ``pkg.auth2`` / ``notpkg.auth`` / ``pkg.authx``.

    Args:
        module: dotted (or slashed) module name codemap answered.
        text: the tool target string to test (grep pattern / path / file_path).

    Returns:
        Whether *text* contains a word-boundary reference to *module*.

    Examples:
        >>> module_matches("pkg.auth", "grep -r 'import pkg.auth' src/")
        True
        >>> module_matches("pkg.auth", "src/pkg/auth.py")
        True
        >>> module_matches("pkg.auth", "pkg.auth2")
        False
        >>> module_matches("pkg.auth", "pkg.other")
        False
        >>> module_matches("", "anything")
        False
    """
    if not module or not text:
        return False
    escaped = "[./]".join(re.escape(seg) for seg in re.split(r"[./]", module))
    try:
        pattern = re.compile(rf"(^|[^{_IDENT}]){escaped}([^{_IDENT}]|$)")
    except re.error:
        return False
    return pattern.search(text) is not None


def _project_coordinate(record: dict) -> str | None:
    """Validate a serialized project coordinate without resolving it on the analysis host."""
    value = record.get("project")
    if isinstance(value, str) and (PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()):
        return value
    return None


def parse_cli_records(records: list[dict]) -> list[CliAnswer]:
    """Turn raw cli records into the complete-answer subset used by the join.

    Only records that are complete (``query_complete``/``exhaustive`` truthy),
    carry a resolvable module, and have a parseable timestamp become answers;
    everything else is dropped silently (incomplete answers cannot be avoided).

    Args:
        records: parsed ``cli.jsonl`` records.

    Returns:
        The complete answers, in input order.
    """
    answers: list[CliAnswer] = []
    for record in records:
        if (
            record.get("source") == "bench"
            or not record.get("cmd")
            or type(record.get("exit_code")) is not int
            or record["exit_code"] != 0
            or _project_coordinate(record) is None
        ):
            continue
        result = record.get("result")
        if record.get("cmd") == "batch" and isinstance(result, dict) and isinstance(result.get("batch"), list):
            # Children retain the parent's evidence coordinates, never child-supplied identity.
            children = [
                record | {"cmd": item.get("cmd"), "argv": [], "result": item.get("result")}
                for item in result["batch"]
                if isinstance(item, dict) and item.get("ok") is True and item.get("cmd") != "batch"
            ]
            answers.extend(parse_cli_records(children))
            continue
        if not _query_complete(record):
            continue
        module = _module_from_cli_result(record)
        ts = _parse_ts(record.get("ts"))
        session = record.get("session")
        if module and ts is not None and isinstance(session, str) and session.strip():
            runtime = record.get(_RUNTIME_KEY)
            answers.append(
                CliAnswer(
                    session=session,
                    ts=ts,
                    module=module,
                    runtime=runtime if runtime in _RUNTIMES else None,
                    version=record.get("v"),
                    project=record.get("project"),
                )
            )
    return answers


def parse_tool_records(records: list[dict]) -> list[ToolEvent]:
    """Turn raw tool records into typed events with parsed timestamps.

    Args:
        records: parsed ``tools.jsonl`` records.

    Returns:
        The well-formed tool events (records missing target/ts/session dropped).
    """
    events: list[ToolEvent] = []
    for record in records:
        if record.get("source") == "bench" or _project_coordinate(record) is None:
            continue
        target = record.get("target")
        ts = _parse_ts(record.get("ts"))
        session = record.get("session")
        tool = record.get("tool")
        if (
            isinstance(target, str)
            and target
            and ts is not None
            and isinstance(session, str)
            and session.strip()
            and isinstance(tool, str)
        ):
            runtime = record.get(_RUNTIME_KEY)
            events.append(
                ToolEvent(
                    session=session,
                    ts=ts,
                    tool=tool,
                    target=target,
                    runtime=runtime if runtime in _RUNTIMES else None,
                    version=record.get("v"),
                    project=record.get("project"),
                )
            )
    return events


def _find_leaked_answer(event: ToolEvent, answers: list[CliAnswer], window_seconds: float) -> CliAnswer | None:
    """Return the most recent eligible answer whose module overlaps the tool target.

    An answer overlaps when it is in the same evidence cohort, its module matches the tool
    target on identifier boundaries, and it landed within ``window_seconds``
    *before* the tool call. The most recent qualifying answer is returned so the
    reported gap is the tightest (and the guard's own last-answer semantics match).

    Args:
        event: the Grep/Read/Glob tool call under test.
        answers: complete cli answers to join against.
        window_seconds: max seconds an answer may precede the tool call.

    Returns:
        The overlapping :class:`CliAnswer`, or ``None`` when no name/time match exists.
    """
    best: CliAnswer | None = None
    for answer in answers:
        if (answer.session, answer.runtime, answer.version, answer.project) != (
            event.session,
            event.runtime,
            event.version,
            event.project,
        ):
            continue
        gap = (event.ts - answer.ts).total_seconds()
        if gap < 0 or gap > window_seconds:
            continue
        if not module_matches(answer.module, event.target):
            continue
        if best is None or answer.ts > best.ts:
            best = answer
    return best


def find_avoidance_events(
    answers: list[CliAnswer],
    events: list[ToolEvent],
    window_min: int = DEFAULT_WINDOW_MIN,
) -> list[AvoidanceEvent]:
    """Join eligible answers with tool targets, without inferring redundant work.

    Args:
        answers: complete cli answers (from :func:`parse_cli_records`).
        events: tool events (from :func:`parse_tool_records`).
        window_min: how many minutes an answer may precede a tool call and still
            count as overlapping. Defaults to :data:`DEFAULT_WINDOW_MIN`.

    Returns:
        One :class:`AvoidanceEvent` per matching target, in ``events`` order.
    """
    window_seconds = window_min * 60
    flagged: list[AvoidanceEvent] = []
    for event in events:
        answer = _find_leaked_answer(event, answers, window_seconds)
        if answer is None:
            continue
        flagged.append(
            AvoidanceEvent(
                session=event.session,
                module=answer.module,
                tool=event.tool,
                target=event.target,
                answer_ts=answer.ts,
                tool_ts=event.ts,
                gap_seconds=(event.ts - answer.ts).total_seconds(),
                runtime=event.runtime,
                version=event.version,
                project=event.project,
            )
        )
    return flagged


def _runtime_label(runtime: str | None) -> str:
    """Return a stable report label, including a fallback for legacy unscoped rows.

    Examples:
        >>> _runtime_label('codex')
        'codex'
        >>> _runtime_label(None)
        'unattributed'
    """
    return runtime or _UNATTRIBUTED_RUNTIME


def _session_label(runtime: str | None, session: str) -> str:
    """Return an unambiguous report key for one scoped or legacy session.

    Examples:
        >>> _session_label('claude', 'abc')
        'claude:abc'
        >>> _session_label(None, 'abc')
        'abc'
    """
    return session if runtime is None else f"{runtime}:{session}"


def _runtime_metrics(
    answers: list[CliAnswer], events: list[ToolEvent], flagged: list[AvoidanceEvent]
) -> dict[str, dict[str, int | float | dict[str, int]]]:
    """Return totals, rates, and flagged modules grouped by source runtime."""
    metrics: dict[str, dict[str, int | float | dict[str, int]]] = {}
    for answer in answers:
        label = _runtime_label(answer.runtime)
        metric = metrics.setdefault(label, {"total_complete_answers": 0, "total_tool_events": 0, "avoidance_count": 0})
        metric["total_complete_answers"] = int(metric["total_complete_answers"]) + 1
    for event in events:
        label = _runtime_label(event.runtime)
        metric = metrics.setdefault(label, {"total_complete_answers": 0, "total_tool_events": 0, "avoidance_count": 0})
        metric["total_tool_events"] = int(metric["total_tool_events"]) + 1
    for event in flagged:
        label = _runtime_label(event.runtime)
        metric = metrics.setdefault(label, {"total_complete_answers": 0, "total_tool_events": 0, "avoidance_count": 0})
        metric["avoidance_count"] = int(metric["avoidance_count"]) + 1
        modules = metric.setdefault("modules", {})
        assert isinstance(modules, dict)
        modules[event.module] = modules.get(event.module, 0) + 1
    for metric in metrics.values():
        total = int(metric["total_tool_events"])
        metric["rate"] = round(int(metric["avoidance_count"]) / total, 4) if total else 0.0
        metric.setdefault("modules", {})
    return metrics


def summarize(
    answers: list[CliAnswer],
    events: list[ToolEvent],
    window_min: int = DEFAULT_WINDOW_MIN,
    session_skill: dict[tuple[str | None, ...] | str, str] | None = None,
) -> Summary:
    """Compute the full avoidance summary for a debrief report.

    Args:
        answers: complete cli answers.
        events: tool events.
        window_min: join window in minutes.
        session_skill: optional session id → skill name map (from the skills
            shard) enabling per-skill attribution; sessions absent from the map
            are still counted per session but not per skill.

    Returns:
        A populated :class:`Summary`.
    """
    flagged = find_avoidance_events(answers, events, window_min)
    per_session: dict[str, int] = {}
    per_skill: dict[str, int] = {}
    skill_map = session_skill or {}
    for event in flagged:
        session = _session_label(event.runtime, event.session)
        per_session[session] = per_session.get(session, 0) + 1
        skill = skill_map.get((event.project, event.version, event.runtime, event.session)) or skill_map.get(
            event.session
        )
        if skill:
            per_skill[skill] = per_skill.get(skill, 0) + 1
    return Summary(
        window_min=window_min,
        total_tool_events=len(events),
        total_complete_answers=len(answers),
        avoidance_events=flagged,
        per_session=per_session,
        per_skill=per_skill,
        per_runtime=_runtime_metrics(answers, events, flagged),
    )


def _read_jsonl(path: Path) -> list[dict]:
    """Read one JSONL file, skipping blank and malformed lines.

    Args:
        path: JSONL file to read.

    Returns:
        Parsed records; an unreadable, absent, or oversized file yields ``[]``.
    """
    records: list[dict] = []
    try:
        size = path.stat().st_size
        if size > MAX_LOG_SIZE:
            msg = f"join_avoidance: skipping oversized shard: {path} ({size} bytes; max {MAX_LOG_SIZE})"
            print(msg, file=sys.stderr)
            return records
        text = path.read_text()
    except OSError:
        return records
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records


def _path_runtime(log_dir: Path, path: Path) -> str | None:
    """Return the allowlisted runtime named by *path* below *log_dir*, if any."""
    try:
        parts = path.relative_to(log_dir).parts
    except ValueError:
        return None
    return parts[0] if len(parts) > 1 and parts[0] in _RUNTIMES else None


def _collect(paths: list[Path], log_dir: Path | None = None) -> list[dict]:
    """Read paths and attach only their directory-derived runtime scope.

    Any ``_RUNTIME_KEY`` already present in a record on disk is discarded first — runtime scope is trusted only when
    derived from the shard's directory, never from record content.
    """
    records: list[dict] = []
    for path in paths:
        runtime = _path_runtime(log_dir, path) if log_dir is not None else None
        for record in _read_jsonl(path):
            record.pop(_RUNTIME_KEY, None)
            records.append(record if runtime is None else record | {_RUNTIME_KEY: runtime})
    return records


def _shard_paths(log_dir: Path, layer: str) -> list[Path]:
    """Return every per-session shard plus the legacy unsuffixed log for *layer*.

    Args:
        log_dir: the ``.cache/codemap/logs`` directory.
        layer: ``"cli"``, ``"tools"``, or ``"skills"``.

    Returns:
        Sorted matching paths (``<layer>_*.jsonl`` and legacy ``<layer>.jsonl``).
    """
    shards = list(log_dir.glob(f"{layer}_*.jsonl"))
    legacy = log_dir / f"{layer}.jsonl"
    if legacy.exists():
        shards.append(legacy)
    for runtime in _RUNTIMES:
        runtime_dir = log_dir / runtime
        if runtime_dir.is_dir():
            shards.extend(runtime_dir.rglob(f"{layer}_*.jsonl"))
            runtime_legacy = runtime_dir / f"{layer}.jsonl"
            if runtime_legacy.exists():
                shards.append(runtime_legacy)
    return sorted(set(shards))


def _session_skill_map(skill_records: list[dict]) -> dict[tuple[str | None, ...], str]:
    """Map each explicitly attributed cohort to its first recorded skill start.

    Args:
        skill_records: parsed ``skills.jsonl`` records (``session`` + ``skill``).

    Returns:
        session id → skill name; sessions with no skill record are absent.
    """
    mapping: dict[tuple[str | None, ...], str] = {}
    for record in skill_records:
        session = record.get("session")
        skill = record.get("skill")
        runtime = record.get(_RUNTIME_KEY)
        project = _project_coordinate(record)
        version = record.get("v") if isinstance(record.get("v"), str) else None
        scoped_session = (project, version, runtime if runtime in _RUNTIMES else None, session)
        if project and isinstance(session, str) and isinstance(skill, str) and scoped_session not in mapping:
            mapping[scoped_session] = skill
    return mapping


def _resolve_inputs(args: argparse.Namespace) -> tuple[list[dict], list[dict], dict[tuple[str | None, ...] | str, str]]:
    """Resolve CLI arguments into (cli_records, tool_records, session_skill_map).

    Args:
        args: parsed argparse namespace (``logs`` or ``cli``/``tools`` paths).

    Returns:
        The raw cli records, raw tool records, and the session→skill map.
    """
    if args.logs:
        log_dir = Path(args.logs)
        cli = _collect(_shard_paths(log_dir, "cli"), log_dir)
        tools = _collect(_shard_paths(log_dir, "tools"), log_dir)
        skills = _collect(_shard_paths(log_dir, "skills"), log_dir)
        return cli, tools, _session_skill_map(skills)
    cli = _collect([Path(args.cli)]) if args.cli else []
    tools = _collect([Path(args.tools)]) if args.tools else []
    return cli, tools, {}


def render_text(summary: Summary) -> str:
    """Render a human-readable avoidance summary.

    Args:
        summary: the computed :class:`Summary`.

    Returns:
        A multi-line report string (a single terminal ``print``).

    Examples:
        >>> s = Summary(window_min=10, total_tool_events=0, total_complete_answers=0)
        >>> "no tool events" in render_text(s)
        True
    """
    n = len(summary.avoidance_events)
    lines = [
        f"avoidance join (window {summary.window_min} min)",
        "  metric: module overlap proxy v3; not confirmed misuse or measured savings",
        f"  complete answers: {summary.total_complete_answers}",
        f"  tool events:      {summary.total_tool_events}",
        f"  avoidance events: {n}  (rate {summary.rate:.1%})",
    ]
    if not summary.total_tool_events:
        lines.append("  no tool events eligible — nothing to score.")
    if summary.record_counts:
        lines.append("  record counts:")
        lines.extend(f"    {name}: {count}" for name, count in sorted(summary.record_counts.items()))
    if summary.per_session:
        lines.append("  per session:")
        for session, count in sorted(summary.per_session.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"    {session or '<none>'}: {count}")
    if summary.per_skill:
        lines.append("  per skill:")
        for skill, count in sorted(summary.per_skill.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"    {skill}: {count}")
    if summary.per_runtime:
        lines.append("  per runtime:")
        for runtime, metric in sorted(summary.per_runtime.items()):
            lines.append(
                f"    {runtime}: {metric['avoidance_count']}/{metric['total_tool_events']} "
                f"(rate {float(metric['rate']):.1%}; modules {metric['modules']})"
            )
    return "\n".join(lines)


def render_json(summary: Summary) -> str:
    """Render the avoidance summary as a single-line JSON object.

    Args:
        summary: the computed :class:`Summary`.

    Returns:
        A JSON string with totals, rate, per-session/per-skill counts, and events.
    """
    payload = {
        "metric": "module_overlap_proxy_v3",
        "confirmed_misuse": None,
        "interpretation": "Module/time overlaps, including legitimate source inspection; not guard failures or token savings.",
        "window_min": summary.window_min,
        "total_tool_events": summary.total_tool_events,
        "total_complete_answers": summary.total_complete_answers,
        "avoidance_count": len(summary.avoidance_events),
        "rate": round(summary.rate, 4),
        "per_session": summary.per_session,
        "per_skill": summary.per_skill,
        "per_runtime": summary.per_runtime,
        "record_counts": summary.record_counts,
        "events": [
            {
                "session": e.session,
                "module": e.module,
                "tool": e.tool,
                "target": e.target,
                "answer_ts": e.answer_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "tool_ts": e.tool_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "gap_seconds": round(e.gap_seconds, 1),
                "runtime": e.runtime,
                "version": e.version,
                "project": e.project,
            }
            for e in summary.avoidance_events
        ],
    }
    return json.dumps(payload, separators=(",", ":"))


def _build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser for the avoidance-join CLI."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--logs", help="Log dir holding cli_*/tools_*/skills_* shards (default resolution)")
    parser.add_argument("--cli", help="Explicit cli JSONL file (overrides --logs for the cli layer)")
    parser.add_argument("--tools", help="Explicit tools JSONL file (overrides --logs for the tools layer)")
    parser.add_argument(
        "--window-min",
        type=int,
        default=DEFAULT_WINDOW_MIN,
        help=f"Minutes an answer may precede a re-deriving tool call (default {DEFAULT_WINDOW_MIN})",
    )
    parser.add_argument("--json", action="store_true", help="Emit a single-line JSON object instead of text")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for the avoidance-join CLI.

    Args:
        argv: override ``sys.argv[1:]`` (mainly for testing).

    Returns:
        ``0`` on success, ``2`` when neither ``--logs`` nor a ``--cli``/``--tools``
        input was given.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not args.logs and not (args.cli or args.tools):
        print("join_avoidance: give --logs DIR or --cli FILE and/or --tools FILE", file=sys.stderr)
        return 2

    cli_records, tool_records, session_skill = _resolve_inputs(args)
    answer_groups = [parse_cli_records([record]) for record in cli_records]
    answers = [answer for group in answer_groups for answer in group]
    events = parse_tool_records(tool_records)
    summary = summarize(answers, events, window_min=args.window_min, session_skill=session_skill)
    summary.record_counts = {
        "cli_records": len(cli_records),
        "eligible_cli_invocations": sum(bool(group) for group in answer_groups),
        "logical_answers": len(answers),
        "excluded_or_unjoinable_cli_records": sum(not group for group in answer_groups),
        "tool_records": len(tool_records),
        "eligible_tool_events": len(events),
        "excluded_or_unjoinable_tool_records": len(tool_records) - len(events),
        "unverified_cli_outcomes": sum(type(record.get("exit_code")) is not int for record in cli_records),
        "missing_project_cli_records": sum(_project_coordinate(record) is None for record in cli_records),
        "missing_project_tool_records": sum(_project_coordinate(record) is None for record in tool_records),
    }
    # Parent invocations and logical children are different denominators. Retain every
    # failed or malformed child even when a successful sibling makes its parent eligible.
    batch_children = failed_children = eligible_children = 0
    for record, group in zip(cli_records, answer_groups):
        result = record.get("result")
        if record.get("cmd") != "batch" or not isinstance(result, dict) or not isinstance(result.get("batch"), list):
            continue
        children = result["batch"]
        batch_children += len(children)
        failed_children += sum(isinstance(child, dict) and child.get("ok") is False for child in children)
        eligible_children += len(group)
    summary.record_counts.update(
        batch_children=batch_children,
        eligible_batch_children=eligible_children,
        failed_batch_children=failed_children,
        unjoinable_batch_children=batch_children - eligible_children - failed_children,
    )
    print(render_json(summary) if args.json else render_text(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
