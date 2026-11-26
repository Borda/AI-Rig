#!/usr/bin/env python
"""Pair a before and an after ``foundry:profile`` run so a savings or cost claim is measured or labelled unmeasured.

``record`` reads one finished profile run (``report.md`` plus ``result.jsonl``), extracts its compute time, idle time,
wall time, analyzer windows, session counts, and any token and cost totals, and appends one ``before`` or
``after`` row for a claim label to the pair ledger (``.notes/profile/pairs.jsonl`` by default, schema
``profile-pair`` version 1). It then prints the paired comparison and, with ``--append-to``, appends it to the
report. ``compare`` prints the comparison for a label. ``check-claims`` scans Markdown (a changelog's ``## Unreleased``
section, a plan) for savings or cost claims and accepts each only when it carries ``(unmeasured)`` or
``(measured: <label>)`` naming a comparable pair that shows a decrease in the claimed metric. A ``--section``
without a matching ``##`` heading rejects that file, since none of its claims were read.

A metric is measured when both values exist and its analyzer counted the same number of sessions on each side.
Equal counts are a necessary workload check. Window sides require distinct reports and two ordered, nonoverlapping
analyzer intervals of equal length. Session sides require two different sessions and a later after record.
Older window rows without analyzer intervals stay readable and cannot substantiate a measured claim.

Each row keeps the working directory's commit as provenance only: a plugin update can change cost while the measured
project's commit stays the same, so equal commits never refuse a pair. Explicit sides are used instead of the
first-record baseline of ``efficiency_measured.py`` because a claim names one specific change: its before must be
chosen deliberately, not inherited from whichever run happened to be recorded first.

Usage:
    python profile_pair.py record --label LABEL --side before|after --report DIR [--ledger PATH] [--append-to FILE]
    python profile_pair.py compare --label LABEL [--ledger PATH]
    python profile_pair.py check-claims FILE [FILE ...] [--section HEADING] [--ledger PATH]

Exit codes:
    0   recorded, compared, or every claim accepted
    1   a claim is rejected or a ``--section`` heading is missing from a file (``check-claims``), or the report lacks
        the clock-time lines (``record``)
    2   argument error
    4   a file cannot be read or written
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from math import isclose, isfinite
from pathlib import Path
from typing import Any

#: Schema family of the pair ledger.
SCHEMA = "profile-pair"
#: Current pair-ledger schema version; a new family starts at 1.
SCHEMA_VERSION = 1
#: Default pair ledger, durable (``.notes/`` is never auto-cleaned), relative to the project root.
DEFAULT_LEDGER = Path(".notes/profile/pairs.jsonl")
#: Allowed claim label characters, mirroring the profile skill's ``--pair-label`` validation.
LABEL_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,80}$")

_SCOPE_RE = re.compile(r"^Scope:\s+window=(\S+), sessions=(\d+), total_wall=(\d+):(\d{2}):(\d{2})\s*$")
_IDLE_RE = re.compile(r"^- AskUserQuestion idle \(excluded\): (\d+):(\d{2}):(\d{2})\s*$")
_COMPUTE_TOTAL_RE = re.compile(r"^- Compute time \(observed\): (\d+):(\d{2}):(\d{2})\s*$")
_WINDOW_COST_RE = re.compile(r"^\*\*Window total: \$([\d,]+\.\d{2})\*\* across all (\d+) session\(s\) above\.$")
_SESSION_COST_RE = re.compile(r"^\| \*\*total\*\* \|.*\| \*\*\$([\d,]+\.\d{2})\*\* \|\s*$")
_TOKEN_TOTAL_RE = re.compile(r"^\*\*Total tokens: (\d+(?:,\d{3})*)\*\*$")
_INTERVAL_RE = re.compile(r"^(Clock|Cost) (window|session) \(UTC epoch seconds\): (\S+) to (\S+)$")
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_INLINE_CODE_RE = re.compile(r"(?<!`)(`+)(?!`).*?(?<!`)\1(?!`)")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")
_ANNOTATION_RE = re.compile(r"\(measured: *([^)]*)(\)|$)|\(unmeasured\)")
_COMPARISON_RE = re.compile(
    r"\b(?:half|halv(?:e[sd]?|ing)|third|quarter|twice|fewer|less|lower|cheaper|faster|quicker|shorter|"
    r"previously|versus|down (?:from|to)|compared (?:with|to))\b|"
    r"\bby \$?\d|\bfrom \$?\d[\d,.]*(?:\s+\w+)?\s+to \$?\d|"
    r"\b\d+(?:\.\d+)?\s*(?:%|×|x\b)|\b\d+/\d+\b",
    re.IGNORECASE,
)
# Unquantified statements of savings still require a label.
_SAVINGS_RE = re.compile(
    r"\b(?:save[sd]?|saving[s]?|cut(?:s|ting)?|reduc(?:e[sd]?|ing|tion)|trim(?:s|med|ming)?|"
    r"lower(?:s|ed|ing)|shrink(?:s|ing)?)\b",
    re.IGNORECASE,
)
_STATISTIC_RE = re.compile(
    r"\b(?:p\d+(?:\.\d+)?|percentiles?|quantiles?|quartiles?|medians?|averages?|mean|"
    r"min(?:imum)?|max(?:imum)?|worst[- ]case|best[- ]case|peak|fastest|slowest|variance|"
    r"standard deviation|distribution|rates?|throughput|per (?:call|request|run|review|session|second|minute|hour)s?)\b"
    r"|\b(?:tokens?|dollars?|cost|latency)\s*/\s*(?:s|sec|hour|call|run|request|session)\b",
    re.IGNORECASE,
)
_TOKEN_RE = re.compile(r"\btokens?\b", re.IGNORECASE)
_COST_RE = re.compile(r"\bcosts?\b|\$\d|\bdollars?\b|\bcheaper\b", re.IGNORECASE)
_SPEND_RE = re.compile(r"\bspend(?:s|ing)?\b", re.IGNORECASE)
_COMPUTE_RE = re.compile(r"\bcompute(?:[- ]time)?\b", re.IGNORECASE)
_WALL_RE = re.compile(r"\b(?:latency|wall[- ]?(?:time|clock)|runtime)\b|(?<!-)\btime\b(?!-)", re.IGNORECASE)
_TIME_HINT_RE = re.compile(
    r"\b(?:seconds|minutes?|hours?|faster|quicker|slower)\b|\b(?:\d+(?:\.\d+)?|one)[ -]+second\b", re.IGNORECASE
)


class Side(str, Enum):
    """Which side of a before/after pair a profile run measures."""

    BEFORE = "before"
    AFTER = "after"


class Metric(str, Enum):
    """Measured quantities a claim can rest on."""

    COMPUTE_S = "compute_s"
    COST_USD = "cost_usd"
    WALL_S = "wall_s"
    TOKENS = "tokens"


_COST_METRICS = (Metric.COST_USD, Metric.TOKENS)
_METRIC_LABELS = {
    Metric.COMPUTE_S: "compute time",
    Metric.COST_USD: "cost",
    Metric.WALL_S: "wall time",
    Metric.TOKENS: "tokens",
}


@dataclass(frozen=True)
class PairStatus:
    """Whether a label's pair is measured for a metric, why not when it is not, and the two sides used."""

    measured: bool
    reason: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None


class ProfileReportError(Exception):
    """A profile report lacks the clock-time lines a pair row needs."""


def _seconds(hours: str, minutes: str, seconds: str) -> int:
    """Convert ``H:MM:SS`` parts to seconds.

    Examples:
        >>> _seconds("2450", "51", "40")
        8823100
    """
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds)


def _cost_totals(text: str) -> dict[str, Any]:
    """Read the transcript analyzer's dollar total, token total, and corresponding workload."""
    totals: dict[str, Any] = {"cost_usd": None, "cost_sessions": None, "tokens": None}
    for _, line in _section_lines(text, "Tokens & cost") or ():
        if match := _WINDOW_COST_RE.fullmatch(line):
            totals["cost_usd"] = float(match[1].replace(",", ""))
            totals["cost_sessions"] = int(match[2])
        elif match := _SESSION_COST_RE.fullmatch(line):
            totals["cost_usd"] = float(match[1].replace(",", ""))
            totals["cost_sessions"] = 1
        elif match := _TOKEN_TOTAL_RE.fullmatch(line):
            totals["tokens"] = int(match[1].replace(",", ""))
    return totals


def _report_intervals(text: str) -> dict[str, tuple[float, float]]:
    """Read the bounds the analyzers report for their own observations."""
    intervals = {}
    for line in text.splitlines():
        if match := _INTERVAL_RE.fullmatch(line):
            try:
                start, end = float(match[3]), float(match[4])
            except ValueError:
                continue
            if isfinite(start) and isfinite(end) and start <= end:
                intervals[f"{match[1].lower()}_{match[2]}"] = (start, end)
    return intervals


def parse_report(text: str) -> dict[str, Any]:
    """Extract the pair metrics from a profile ``report.md``.

    The clock-time lines are required. The best-effort token and cost section may leave its totals unavailable.

    Raises:
        ProfileReportError: The ``Scope:`` header or the idle line is missing.

    Examples:
        >>> report = "Scope:      window=24h, sessions=2, total_wall=01:00:00\\n"
        >>> report += "- AskUserQuestion idle (excluded): 00:10:00\\n"
        >>> parsed = parse_report(report)
        >>> parsed["compute_s"], parsed["sessions"], parsed["cost_usd"]
        (None, 2, None)
    """
    scope = idle = compute = None
    for line in text.splitlines():
        scope = scope or _SCOPE_RE.match(line)
        idle = idle or _IDLE_RE.match(line)
        compute = compute or _COMPUTE_TOTAL_RE.match(line)
    if scope is None or idle is None:
        raise ProfileReportError("report lacks the Scope header or the AskUserQuestion idle line")
    wall = _seconds(*scope.group(3, 4, 5))
    idle_s = _seconds(*idle.groups())
    return {
        "since": scope.group(1),
        "sessions": int(scope.group(2)),
        "wall_s": wall,
        "idle_s": idle_s,
        "compute_s": _seconds(*compute.groups()) if compute else None,
        **_cost_totals(text),
        **_report_intervals(text),
    }


def _revision() -> str | None:
    """Return the working directory's commit id, or ``None`` outside a Git checkout."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607 - git resolved via PATH on purpose
            capture_output=True,
            encoding="utf-8",
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    revision = result.stdout.strip()
    return revision if result.returncode == 0 and re.fullmatch(r"[0-9a-f]{7,64}", revision) else None


def _utc_now() -> str:
    """Return the current UTC time as a fixed-width ISO stamp with microseconds, so stamps sort as strings."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def build_row(label: str, side: Side, report_dir: Path, revision: str | None) -> dict[str, Any]:
    """Build one pair-ledger row from a finished profile run directory.

    Raises:
        ProfileReportError: The report lacks its clock-time lines.
        OSError: The report or its ``result.jsonl`` cannot be read.
    """
    metrics = parse_report((report_dir / "report.md").read_text(encoding="utf-8"))
    result = json.loads((report_dir / "result.jsonl").read_text(encoding="utf-8").splitlines()[0])
    session_id = str(result.get("session_id") or "")
    scope = {
        "kind": "session" if session_id else "window",
        "since": metrics.pop("since"),
        "session_id": session_id,
        "clock_window": metrics.pop("clock_window", None),
        "cost_window": metrics.pop("cost_window", None),
        "clock_session": metrics.pop("clock_session", None),
        "cost_session": metrics.pop("cost_session", None),
        "observed_compute": metrics["compute_s"] is not None,
    }
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "label": label,
        "side": side.value,
        "recorded_at": _utc_now(),
        "report": report_dir.resolve().as_posix(),
        "revision": revision,
        "scope": scope,
        "metrics": metrics,
    }


def read_rows(ledger: Path) -> list[dict[str, Any]]:
    """Return the ledger's valid rows of the current schema; a missing ledger has none.

    Rows of another schema or version and unparsable lines are ignored rather than guessed at.
    """
    if not ledger.exists():
        return []
    rows = []
    for line in ledger.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if (
            isinstance(row, dict)
            and row.get("schema") == SCHEMA
            and row.get("schema_version") == SCHEMA_VERSION
            and isinstance(row.get("scope"), dict)
            and isinstance(row.get("metrics"), dict)
        ):
            rows.append(row)
    return rows


def append_row(ledger: Path, row: dict[str, Any]) -> None:
    """Append one row to the pair ledger, closing an unterminated final line first.

    Raises:
        OSError: The ledger is a symlink or cannot be written.
    """
    if ledger.is_symlink():
        raise OSError(f"refusing symlinked pair ledger: {ledger}")
    ledger.parent.mkdir(parents=True, exist_ok=True)
    unterminated = ledger.exists() and not ledger.read_bytes().endswith(b"\n") and ledger.stat().st_size > 0
    with ledger.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(("\n" if unterminated else "") + json.dumps(row, sort_keys=True) + "\n")


def _latest(rows: list[dict[str, Any]], label: str, side: Side) -> dict[str, Any] | None:
    """Return the last row of one side for a label."""
    matching = [row for row in rows if row.get("label") == label and row.get("side") == side.value]
    return matching[-1] if matching else None


def _window_seconds(spec: object) -> int | None:
    """Return a ``--since`` window (``NNs|NNm|NNh|NNd``) in seconds, or ``None`` when it is not one.

    Examples:
        >>> _window_seconds("24h")
        86400
        >>> _window_seconds("soon") is None
        True
    """
    match = re.fullmatch(r"(\d+)([smhd])", str(spec))
    return int(match.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)] if match else None


def _observation_bounds(bounds: object) -> tuple[float, float] | None:
    """Validate stored bounds, accepting a known instantaneous session observation."""
    if not isinstance(bounds, (tuple, list)) or len(bounds) != 2:
        return None
    start, end = bounds
    if not all(type(point) in (int, float) and isfinite(point) for point in (start, end)):
        return None
    return (start, end) if start <= end else None


def _window_bounds(bounds: object, duration: int) -> tuple[float, float] | None:
    """Validate that an analyzer observed the full requested window."""
    interval = _observation_bounds(bounds)
    if interval is None or not isclose(interval[1] - interval[0], duration, abs_tol=1e-6, rel_tol=0):
        return None
    return interval


def _workload_conflict(before: dict[str, Any], after: dict[str, Any], metric: Metric) -> str | None:
    """Name why two sides do not sum the same workload, or return ``None`` when their session counts agree.

    A total over fewer or more sessions reflects a different amount of work, not the change under test. The session
    count is the one workload property a report records, so equal counts are a necessary condition, not proof that the
    sessions did the same work.

    Examples:
        >>> one, two = {"metrics": {"sessions": 1}}, {"metrics": {"sessions": 2}}
        >>> _workload_conflict(one, one, Metric.COMPUTE_S) is None
        True
        >>> _workload_conflict(one, two, Metric.WALL_S)
        'session counts differ (1 before, 2 after): the two sides do not sum the same workload'
    """
    field = "cost_sessions" if metric in _COST_METRICS else "sessions"
    counts = before["metrics"].get(field), after["metrics"].get(field)
    if not all(type(count) is int and count > 0 for count in counts):
        return "session count missing on the before or after side"
    if counts[0] != counts[1]:
        return (
            f"session counts differ ({counts[0]} before, {counts[1]} after): the two sides do not sum the same workload"
        )
    return None


def _scope_conflict(before: dict[str, Any], after: dict[str, Any], metric: Metric) -> str | None:
    """Require ordered, disjoint observations from the analyzer responsible for the metric."""
    left, right = before["scope"], after["scope"]
    kind = left.get("kind")
    if kind != right.get("kind"):
        return "scope kinds differ (window versus session)"
    if kind == "session":
        return _session_conflict(before, after, metric)
    if kind != "window":
        return "unrecognized profile scope"
    if not before.get("report") or not after.get("report") or before["report"] == after["report"]:
        return "window pairs require two distinct reports"
    duration = _window_seconds(left.get("since"))
    if not duration or duration != _window_seconds(right.get("since")):
        return "window lengths differ"
    field = "cost_window" if metric in _COST_METRICS else "clock_window"
    first, second = _window_bounds(left.get(field), duration), _window_bounds(right.get(field), duration)
    if first is None or second is None:
        return "analyzer window bounds missing or invalid on the before or after side"
    if second[1] <= first[0]:
        return "the after window precedes the before window"
    if second[0] < first[1]:
        return "the windows overlap"
    if metric not in _COST_METRICS and not all(scope.get("observed_compute") is True for scope in (left, right)):
        return "clock report lacks interval-aware timing evidence"
    return None


def _session_conflict(before: dict[str, Any], after: dict[str, Any], metric: Metric) -> str | None:
    """Require distinct, fully observed sessions in measurement order for the metric."""
    first, second = before["scope"].get("session_id"), after["scope"].get("session_id")
    if not first or not second:
        return "session ID missing on the before or after side"
    if first == second:
        return "both sides profile the same session"
    field = "cost_session" if metric in _COST_METRICS else "clock_session"
    left = _observation_bounds(before["scope"].get(field))
    right = _observation_bounds(after["scope"].get(field))
    if left is None or right is None:
        return "full-session observation bounds missing or invalid on the before or after side"
    if right[1] < left[0]:
        return "the after session precedes the before session"
    if right[0] <= left[1]:
        return "the session observations overlap or share an endpoint"
    return None


def _incomparable(before: dict[str, Any], after: dict[str, Any], metric: Metric) -> str | None:
    """Explain which evidence makes a metric unavailable or its observations incomparable."""
    values = before["metrics"].get(metric.value), after["metrics"].get(metric.value)
    if not all(type(value) in (int, float) and isfinite(value) and value >= 0 for value in values):
        return f"{metric.value} missing or invalid on the before or after side"
    return _scope_conflict(before, after, metric) or _workload_conflict(before, after, metric)


def pair_status(rows: list[dict[str, Any]], label: str, metric: Metric) -> PairStatus:
    """Decide whether a label's latest before and after rows measure a metric.

    Examples:
        >>> pair_status([], "/oss:resolve", Metric.COST_USD).reason
        'no before row recorded for this label'
    """
    before, after = _latest(rows, label, Side.BEFORE), _latest(rows, label, Side.AFTER)
    if before is None:
        return PairStatus(False, "no before row recorded for this label", None, after)
    if after is None:
        return PairStatus(False, "no after row recorded for this label", before, None)
    reason = _incomparable(before, after, metric)
    return PairStatus(reason is None, reason or "measured", before, after)


def _fmt_hms(seconds: int) -> str:
    """Format seconds as ``HH:MM:SS``.

    Examples:
        >>> _fmt_hms(3661)
        '01:01:01'
    """
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def _change(before: float, after: float) -> str:
    """Format the relative change from before to after.

    Examples:
        >>> _change(100, 80)
        '-20.0%'
        >>> _change(0, 5)
        'n/a'
    """
    return f"{(after - before) / before:+.1%}" if before else "n/a"


def _status_text(status: PairStatus) -> str:
    """Render one metric's status as ``measured`` or ``unmeasured — <reason>``."""
    return "measured" if status.measured else f"unmeasured — {status.reason}"


def _format_observation(metric: Metric, value: float) -> str:
    """Format a measured value in the units printed by its analyzer."""
    if metric == Metric.COST_USD:
        return f"${value:,.2f}"
    if metric == Metric.TOKENS:
        return f"{value:,}"
    return _fmt_hms(int(value))


def render_comparison(rows: list[dict[str, Any]], label: str) -> str:
    """Render the paired-measurement Markdown section for a label: one status line per metric, then the table.

    Examples:
        >>> render_comparison([], "/oss:resolve").splitlines()[4]
        '- compute time: unmeasured — no before row recorded for this label'
    """
    statuses = {metric: pair_status(rows, label, metric) for metric in Metric}
    compute = statuses[Metric.COMPUTE_S]
    lines = ["## Paired measurement", "", f"Label: `{label}`", ""]
    lines.extend(f"- {_METRIC_LABELS[metric]}: {_status_text(status)}" for metric, status in statuses.items())
    lines.append("")
    if compute.before is None or compute.after is None:
        return "\n".join(lines)
    before, after = compute.before, compute.after
    lines += ["| metric | before | after | change |", "|---|---:|---:|---:|"]
    for metric, status in statuses.items():
        if not status.measured:
            continue
        first, second = before["metrics"][metric.value], after["metrics"][metric.value]
        cells = _format_observation(metric, first), _format_observation(metric, second)
        lines.append(f"| {_METRIC_LABELS[metric]} | {cells[0]} | {cells[1]} | {_change(first, second)} |")
    for name, key in (("clock sessions", "sessions"), ("cost sessions", "cost_sessions")):
        first, second = before["metrics"].get(key), after["metrics"].get(key)
        lines.append(
            f"| {name} | {first if first is not None else 'n/a'} | {second if second is not None else 'n/a'} | |"
        )
    lines += [
        "",
        f"Before: `{before['report']}` @ {before.get('revision') or 'unknown revision'} · "
        f"After: `{after['report']}` @ {after.get('revision') or 'unknown revision'}",
        "",
    ]
    return "\n".join(lines)


def claim_metrics(line: str) -> list[Metric]:
    """Return the metrics a line claims to save, or an empty list when the line makes no savings or cost claim.

    A claim couples a metric with comparative, quantitative or explicit savings language.
    Unqualified time and speed refer to wall time.

    Examples:
        >>> claim_metrics("- Cuts resolve token spend by 60%.")
        [<Metric.TOKENS: 'tokens'>]
        >>> claim_metrics("- Saves 40% of review tokens.")
        [<Metric.TOKENS: 'tokens'>]
        >>> claim_metrics("- Reviews run faster on large diffs.")
        [<Metric.WALL_S: 'wall_s'>]
        >>> claim_metrics("- Faster cache lookups (measured: cost).")
        [<Metric.WALL_S: 'wall_s'>]
        >>> claim_metrics("- Cuts one-time setup costs.")
        [<Metric.COST_USD: 'cost_usd'>]
        >>> claim_metrics("- Fewer false positives in the guard.")
        []
    """
    line = _ANNOTATION_RE.sub("", line)
    if not (_COMPARISON_RE.search(line) or _SAVINGS_RE.search(line)):
        return []
    return _quantities(line)


def _quantities(line: str) -> list[Metric]:
    """Find quantities mentioned in claim prose."""
    tokens = _TOKEN_RE.search(line)
    metrics = [Metric.TOKENS] if tokens else []
    if _COST_RE.search(line) or (not tokens and _SPEND_RE.search(line)):
        metrics.append(Metric.COST_USD)
    compute = _COMPUTE_RE.search(line)
    if compute:
        metrics.append(Metric.COMPUTE_S)
    if _WALL_RE.search(_COMPUTE_RE.sub("", line)) or (not compute and _TIME_HINT_RE.search(line)):
        metrics.append(Metric.WALL_S)
    return metrics


def _outside_fences(text: str) -> list[tuple[int, str]]:
    """Return numbered Markdown lines outside matching backtick or tilde fences."""
    lines = []
    fence = ""
    for number, line in enumerate(text.splitlines(), start=1):
        match = _FENCE_RE.fullmatch(line)
        if fence:
            if match and match[1].startswith(fence) and not match[2].strip():
                fence = ""
        elif match and (match[1][0] == "~" or "`" not in match[2]):
            fence = match[1]
        else:
            lines.append((number, line))
    return lines


def _section_lines(text: str, section: str | None) -> list[tuple[int, str]] | None:
    """Read lines outside fenced code, selecting an exact level-two heading when requested.

    Return ``None`` when the requested heading does not exist.

    Examples:
        >>> _section_lines("## Unreleased\\n- a\\n## 0.1.0\\n- b\\n", "Unreleased")
        [(2, '- a')]
        >>> _section_lines("## 0.1.0\\n- b\\n", "Unreleased") is None
        True
    """
    selected: list[tuple[int, str]] = []
    inside, found = section is None, section is None
    for number, line in _outside_fences(text):
        if line.startswith("## ") and section is not None:
            inside = line[3:].rstrip() == section
            found = found or inside
            continue
        if inside:
            selected.append((number, line))
    return selected if found else None


def claim_problems(path: Path, rows: list[dict[str, Any]], section: str | None) -> tuple[int, list[str]]:
    """Return the number of claims in a Markdown file and one problem per claim that is neither measured nor labelled.

    A missing ``##`` heading is itself a problem when a section is requested.

    Examples:
        >>> import tempfile
        >>> doc = Path(tempfile.mkdtemp()) / "CHANGELOG.md"
        >>> _ = doc.write_text("## Unreleased\\n\\n- Cuts review cost by half.\\n", encoding="utf-8")
        >>> claim_problems(doc, [], "Unreleased")[1][0].split(": ", 1)[1].startswith("savings or cost claim")
        True
        >>> claim_problems(doc, [], "Pending")[1][0].split(": ", 1)[1]
        "no heading matches '## Pending', so nothing was checked"
    """
    lines = _section_lines(path.read_text(encoding="utf-8"), section)
    if lines is None:
        return 0, [f"{path}: no heading matches {'## ' + str(section)!r}, so nothing was checked"]
    problems: list[str] = []
    claims = 0
    for number, line in lines:
        for sentence in _SENTENCE_RE.split(_INLINE_CODE_RE.sub(" ", line)):
            claims += bool(claim_metrics(sentence))
            problems.extend(f"{path}:{number}: {reason}" for reason in _line_problems(sentence, rows))
    return claims, problems


def _line_problems(line: str, rows: list[dict[str, Any]]) -> list[str]:
    """Check every annotation against its preceding claim and catch any unlabelled claim."""
    problems = []
    start = 0
    claim = ""
    for match in _ANNOTATION_RE.finditer(line):
        segment = line[start : match.start()]
        start = match.end()
        if segment.strip(" .,;\t"):
            claim = segment
        if match[1] is not None and not match[2]:
            problems.append("unterminated measured annotation")
        elif match[1] is not None:
            problems.extend(_annotation_problems(match[1], claim, rows))
    if claim_metrics(line[start:]):
        problems.append("savings or cost claim without (measured: <label>) or (unmeasured)")
    return problems


def _annotation_problems(label: str, claim: str, rows: list[dict[str, Any]]) -> list[str]:
    """Require comparable savings in each stated quantity, or evidence for an otherwise bare reference."""
    if not LABEL_RE.fullmatch(label):
        return [f"invalid measured label {label!r}"]
    if _STATISTIC_RE.search(claim):
        return [f"{label}: profile totals cannot measure a statistic or rate; use (unmeasured)"]
    metrics = _quantities(claim)
    if not metrics:
        if any(pair_status(rows, label, metric).measured for metric in Metric):
            return []
        return [f"{label}: no comparable profile observations for this label"]
    problems = []
    for metric in metrics:
        reason = _savings_conflict(pair_status(rows, label, metric), metric)
        if reason:
            problems.append(f"{label} {metric.value}: {reason}")
    return problems


def _savings_conflict(status: PairStatus, metric: Metric) -> str | None:
    """Explain why a measured label fails to show a saving in its stated quantity."""
    if not status.measured or status.before is None or status.after is None:
        return f"unmeasured: {status.reason}"
    if status.after["metrics"][metric.value] >= status.before["metrics"][metric.value]:
        return "the measured after value did not decrease"
    return None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pair before/after profile runs and check savings claims.")
    commands = parser.add_subparsers(dest="command", required=True)
    record = commands.add_parser("record")
    record.add_argument("--label", required=True)
    record.add_argument("--side", required=True, choices=[side.value for side in Side])
    record.add_argument("--report", required=True, type=Path)
    record.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    record.add_argument("--append-to", type=Path, default=None)
    compare = commands.add_parser("compare")
    compare.add_argument("--label", required=True)
    compare.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    check = commands.add_parser("check-claims")
    check.add_argument("files", nargs="+", type=Path)
    check.add_argument("--section", default=None)
    check.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    return parser


def _record(args: argparse.Namespace) -> int:
    """Append one side for a label and print (and optionally append) the comparison."""
    try:
        row = build_row(args.label, Side(args.side), args.report, _revision())
    except ProfileReportError as error:
        print(f"! BLOCKED — {args.report}: {error}")
        return 1
    append_row(args.ledger, row)
    section = render_comparison(read_rows(args.ledger), args.label)
    if args.append_to is not None:
        with args.append_to.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write("\n" + section)
    print(section, end="")
    return 0


def _check(args: argparse.Namespace) -> int:
    """Check every file's claims and print each problem; exit 1 when any claim is rejected."""
    rows = read_rows(args.ledger)
    total, problems = 0, []
    for path in args.files:
        claims, found = claim_problems(path, rows, args.section)
        total += claims
        problems += found
    print("\n".join(problems) if problems else f"OK: {total} savings or cost claim(s) measured or labelled")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point; see the module docstring for exit codes (argparse raises its own exit 2)."""
    args = _build_parser().parse_args(argv)
    if getattr(args, "label", None) == []:
        # Python 3.10 argparse strips the value of `--label=--` and leaves an empty list.
        args.label = "--"
    if getattr(args, "label", None) is not None and not LABEL_RE.fullmatch(args.label):
        print(f"! BLOCKED — label must match {LABEL_RE.pattern}")
        return 2
    try:
        if args.command == "record":
            return _record(args)
        if args.command == "compare":
            print(render_comparison(read_rows(args.ledger), args.label), end="")
            return 0
        return _check(args)
    except (OSError, ValueError, IndexError) as error:
        print(f"! BLOCKED — {type(error).__name__}: {error}")
        return 4


if __name__ == "__main__":
    sys.exit(main())
