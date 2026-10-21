#!/usr/bin/env python3
"""Compute every debrief-coding statistic from codemap telemetry so the skill never hand-reads log shards.

A busy repository holds hundreds of shards (one measured tree: 220 shards, 16,367 lines, roughly 2.9M tokens). Reading
them with a file tool truncates large shards and overflows any context window, so a report built that way rests on a
partial dataset with model-estimated numbers. This helper streams every shard and prints one JSON object of finished
aggregates; the skill writes its report from that object only.

Pipeline, in order:

1. Resolve the log root once with the writer's own resolver (git top-level, ``CODEMAP_LOG_DIR`` honoured), so a run
   from a subdirectory or with an override reads the tree the hooks wrote.
2. Copy every ``cli``/``skills``/``tools`` shard into a private scratch tree that preserves runtime topology
   (``claude/``, ``codex/``, ``direct/``, flat legacy), keeping only lines that pass ``--since`` and ``--session``.
   Malformed lines are counted here. Filtering before anonymization matters: pseudonymized ``session`` values can no
   longer match a raw ``--session`` UUID.
3. With ``--anonymize``, run ``anonymize.py`` on that filtered copy into a fresh run-scoped directory
   ``.cache/codemap/export/<UTC stamp>/``. A run-scoped directory keeps earlier exports out of this report. Anonymized
   mode is active only when ``anonymize.py`` exits 0 and wrote at least one ``-anon.jsonl`` file; otherwise the filtered
   raw copy is used and the JSON says so.
4. Aggregate the selected dataset. ``source: "bench"`` records and CLI records with an empty ``cmd`` are excluded and
   counted. Records without ``v`` join no version cohort and are counted separately. Every remaining record is
   grouped by version; the newest version is named ``primary_cohort``, and older cohorts are reported separately,
   never pooled. Query and index timings are split, p95 is ``sorted_ms[int(len(sorted_ms) * 0.95)]``.
5. Run ``join_avoidance.py`` on the same dataset, so the module-overlap proxy honours the same filters.

Usage:
    python debrief_stats.py --arguments="$ARGUMENTS"
    python debrief_stats.py --since 2026-06-15 --session <uuid> --anonymize --output report.md

Exit codes:
    0 — statistics printed as one JSON object on stdout
    1 — no codemap telemetry under the resolved log root
    2 — bad arguments (unknown flag, missing value, malformed ``--since``)
    3 — ``anonymize.py`` or ``join_avoidance.py`` ran past ``HELPER_TIMEOUT_SECONDS``; the helper was killed and the
        scratch copy removed (an interrupted ``anonymize.py`` may leave a partial, already pseudonymized export)
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

#: This ``bin/`` directory, put on ``sys.path`` so the bare sibling imports also resolve when
#: ``pytest --doctest-modules`` imports this file by path (a script run already has its own directory on the path).
_BIN_DIR = str(Path(__file__).resolve().parent)
if _BIN_DIR not in sys.path:
    sys.path.insert(0, _BIN_DIR)

import _runtime_log  # noqa: E402  (needs the sys.path insert above)
from join_avoidance import _RUNTIMES, _parse_ts, _path_runtime, _shard_paths  # noqa: E402

#: Telemetry layers, named as their shard prefixes.
LAYERS = ("cli", "skills", "tools")

#: Label for flat legacy shards, which carry no runtime directory.
UNATTRIBUTED = "unattributed"

#: Version-free records bucket key in the per-version count map.
NO_VERSION = "(none)"

#: Error-prefix length used to group ``result.error`` strings.
ERROR_PREFIX_CHARS = 60

#: Number of error prefixes reported.
TOP_ERRORS = 5

#: Cap on the chronological event list printed for ``--session``.
MAX_SESSION_EVENTS = 500

#: Overlap window handed to ``join_avoidance.py``.
WINDOW_MINUTES = 10

#: Exit status for malformed arguments.
EXIT_BAD_INPUT = 2

#: Exit status when the log root holds no shard.
EXIT_NO_TELEMETRY = 1

#: Exit status when a sibling helper runs past :data:`HELPER_TIMEOUT_SECONDS`.
EXIT_HELPER_TIMEOUT = 3

#: Wall-clock cap for each sibling helper run. ``anonymize.py`` and ``join_avoidance.py`` run one after the other and
#: the skill's Bash call is killed at 120 s, so two 50 s caps stop a hung helper here first, while the ``with`` block in
#: :func:`main` can still delete the raw scratch copy. A Bash kill would skip that cleanup.
HELPER_TIMEOUT_SECONDS = 50

_BIN_DIR = Path(__file__).resolve().parent
_VALUE_FLAGS = ("--since", "--session", "--output")
_BOOL_FLAGS = ("--anonymize",)


class OptionsError(ValueError):
    """The debrief arguments are malformed."""


@dataclass(frozen=True)
class DebriefOptions:
    """Parsed debrief flags.

    Attributes:
        since: Keep records on or after this date.
        session: Keep only this raw session id.
        anonymize: Anonymize the filtered copy before aggregation.
        output: Explicit report path; the default path is derived when absent.
    """

    since: date | None = None
    session: str | None = None
    anonymize: bool = False
    output: str | None = None


def parse_options(tokens: list[str]) -> DebriefOptions:
    """Parse debrief flags from already split tokens.

    Args:
        tokens: Flag tokens, ``--flag value`` or ``--flag=value``.

    Returns:
        The parsed options.

    Raises:
        OptionsError: On an unknown flag, a stray positional, a missing value or a malformed date.

    Examples:
        >>> parse_options(["--since=2026-06-15", "--anonymize"]).since
        datetime.date(2026, 6, 15)
        >>> parse_options(["--session", "abc"]).session
        'abc'
    """
    values: dict[str, str] = {}
    flags: set[str] = set()
    unknown: list[str] = []
    queue = list(tokens)
    while queue:
        token = queue.pop(0)
        name, has_value, inline = token.partition("=")
        if name in _BOOL_FLAGS and not has_value:
            flags.add(name)
        elif name in _VALUE_FLAGS:
            if not has_value and (not queue or queue[0].startswith("--")):
                raise OptionsError(f"{name} requires a value")
            values[name] = inline if has_value else queue.pop(0)
        else:
            unknown.append(token)
    if unknown:
        raise OptionsError(f"Unknown flag(s): {' '.join(unknown)}. Supported: {', '.join(_VALUE_FLAGS + _BOOL_FLAGS)}")
    try:
        since = date.fromisoformat(values["--since"]) if "--since" in values else None
    except ValueError as exc:
        raise OptionsError(f"--since expects YYYY-MM-DD, got {values['--since']!r}") from exc
    return DebriefOptions(
        since=since,
        session=values.get("--session"),
        anonymize="--anonymize" in flags,
        output=values.get("--output"),
    )


def tokens_from_argv(argv: list[str]) -> list[str]:
    """Return the flag tokens, expanding a single ``--arguments=<text>`` with :func:`shlex.split`.

    Args:
        argv: This helper's command line.

    Returns:
        Flag tokens for :func:`parse_options`.

    Raises:
        OptionsError: When the ``--arguments`` text has an unbalanced quote.

    Examples:
        >>> tokens_from_argv(["--arguments=--since 2026-01-02 --anonymize"])
        ['--since', '2026-01-02', '--anonymize']
    """
    if len(argv) == 1 and argv[0].startswith("--arguments="):
        try:
            return shlex.split(argv[0].partition("=")[2])
        except ValueError as exc:
            raise OptionsError(f"cannot split arguments: {exc}") from exc
    return list(argv)


def _record_date(record: dict) -> date | None:
    """Return the UTC date of a record's ``ts``, or ``None`` when it does not parse."""
    parsed = _parse_ts(record.get("ts"))
    return parsed.astimezone(timezone.utc).date() if parsed is not None else None


@dataclass
class StageReport:
    """Counts gathered while copying the filtered shards.

    Attributes:
        shards: Source shard count per layer.
        kept: Kept record count per layer.
        malformed_lines: Lines that were not a JSON object.
        ts_unparsable_excluded: Records dropped by ``--since`` because ``ts`` did not parse.
    """

    shards: dict[str, int] = field(default_factory=lambda: dict.fromkeys(LAYERS, 0))
    kept: dict[str, int] = field(default_factory=lambda: dict.fromkeys(LAYERS, 0))
    malformed_lines: int = 0
    ts_unparsable_excluded: int = 0


def _keep(record: dict, options: DebriefOptions, report: StageReport) -> bool:
    """Apply ``--session`` and ``--since`` to one record."""
    if options.session is not None and record.get("session") != options.session:
        return False
    if options.since is None:
        return True
    record_day = _record_date(record)
    if record_day is None:
        report.ts_unparsable_excluded += 1
        return False
    return record_day >= options.since


def stage_filtered_copy(log_root: Path, dest: Path, options: DebriefOptions) -> StageReport:
    """Copy every kept telemetry line into *dest*, preserving each shard's path below the log root.

    Args:
        log_root: Resolved telemetry root.
        dest: Empty scratch directory.
        options: Active filters.

    Returns:
        Shard, kept-record and malformed-line counts.
    """
    report = StageReport()
    for layer in LAYERS:
        for shard in _shard_paths(log_root, layer):
            report.shards[layer] += 1
            kept_lines = []
            with shard.open(encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    record = _parse_line(line)
                    if record is None:
                        report.malformed_lines += bool(line.strip())
                    elif _keep(record, options, report):
                        kept_lines.append(line.rstrip("\r\n"))
            if kept_lines:
                target = dest / shard.relative_to(log_root)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("\n".join(kept_lines) + "\n", encoding="utf-8", newline="\n")
                report.kept[layer] += len(kept_lines)
    return report


def _parse_line(line: str) -> dict | None:
    """Return the JSON object on one line, or ``None`` for blank, malformed or non-object lines."""
    try:
        parsed = json.loads(line)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _dataset_shards(root: Path, layer: str) -> list[Path]:
    """Return *layer* shards below *root*, including anonymized legacy ``<layer>-anon.jsonl`` names."""
    shards = set(_shard_paths(root, layer))
    shards.update(root.glob(f"{layer}-anon.jsonl"))
    for runtime in _RUNTIMES:
        if (root / runtime).is_dir():
            shards.update((root / runtime).rglob(f"{layer}-anon.jsonl"))
    return sorted(shards)


def load_dataset(root: Path) -> dict[str, list[dict]]:
    """Read every record below *root*, tagging each with its directory-derived runtime.

    Args:
        root: Filtered copy or anonymized export.

    Returns:
        Records per layer; each carries ``_runtime`` (``claude``/``codex``/``direct``/``unattributed``).
    """
    dataset: dict[str, list[dict]] = {layer: [] for layer in LAYERS}
    for layer in LAYERS:
        for shard in _dataset_shards(root, layer):
            runtime = _path_runtime(root, shard) or UNATTRIBUTED
            with shard.open(encoding="utf-8", errors="replace") as handle:
                records = (_parse_line(line) for line in handle)
                dataset[layer].extend(record | {"_runtime": runtime} for record in records if record is not None)
    return dataset


def _version_key(version: str) -> tuple[int, ...]:
    """Sort key for dotted versions; non-numeric parts sort as zero.

    Examples:
        >>> sorted(["0.9.1", "0.10.0"], key=_version_key)
        ['0.9.1', '0.10.0']
    """
    return tuple(int(part) if part.isdigit() else 0 for part in version.split("."))


def _is_scripted(record: dict, layer: str) -> bool:
    """Return whether a record is benchmark load or pre-0.23 test pollution."""
    return record.get("source") == "bench" or (layer == "cli" and not record.get("cmd"))


def _percentiles(values: list[float]) -> dict[str, float | int | None]:
    """Return count, median, p95 and max with the skill's documented p95 formula.

    Examples:
        >>> _percentiles([10, 20, 30])["p95"]
        30
        >>> _percentiles([])["median"] is None
        True
    """
    ordered = sorted(values)
    if not ordered:
        return {"n": 0, "median": None, "p95": None, "max": None}
    middle = len(ordered) // 2
    median = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
    return {"n": len(ordered), "median": median, "p95": ordered[int(len(ordered) * 0.95)], "max": ordered[-1]}


def _outcome(record: dict) -> str:
    """Classify a CLI record as ``success``, ``error`` or ``unverified`` (legacy, no exit code)."""
    result = record.get("result") if isinstance(record.get("result"), dict) else {}
    exit_code = record.get("exit_code")
    if (exit_code is not None and exit_code != 0) or result.get("error"):
        return "error"
    return "success" if exit_code == 0 else "unverified"


def _index_block(record: dict) -> dict:
    """Return ``result.index`` as a dict, or ``{}``."""
    result = record.get("result")
    index = result.get("index") if isinstance(result, dict) else None
    return index if isinstance(index, dict) else {}


def cli_stats(records: list[dict]) -> dict:
    """Aggregate one cohort's CLI records.

    Args:
        records: Organic, versioned CLI records of one version.

    Returns:
        Outcomes, subcommands, split timings, completeness reasons, blind-spot lists, top errors, stale fraction,
        index refresh triggers and per-runtime counts.
    """
    outcomes = Counter(_outcome(record) for record in records)
    index_records = [record for record in records if record.get("cmd") == "index"]
    query_records = [record for record in records if record.get("cmd") != "index"]
    with_index = [_index_block(record) for record in query_records if _index_block(record)]
    blind_spots = Counter(tuple(index.get("not_covered") or []) for index in with_index)
    errors = Counter(
        str(record["result"]["error"])[:ERROR_PREFIX_CHARS]
        for record in records
        if isinstance(record.get("result"), dict) and record["result"].get("error")
    )
    by_runtime: dict[str, Counter] = defaultdict(Counter)
    for record in records:
        by_runtime[record["_runtime"]]["invocations"] += 1
        by_runtime[record["_runtime"]][_outcome(record)] += 1
    refresh_results = [record["result"] for record in index_records if isinstance(record.get("result"), dict)]
    return {
        "invocations": len(records),
        "outcomes": dict(outcomes),
        "subcommands": dict(Counter(str(record.get("cmd")) for record in records).most_common()),
        "timing_ms": {
            "query": _percentiles([r["timing_ms"] for r in query_records if r.get("timing_ms") is not None]),
            "index": _percentiles([r["timing_ms"] for r in index_records if r.get("timing_ms") is not None]),
        },
        "completeness_reasons": dict(Counter(str(i.get("completeness_reason") or "ok") for i in with_index)),
        "not_covered_lists": [{"slugs": list(slugs), "queries": n} for slugs, n in blind_spots.most_common()],
        "top_errors": [{"prefix": prefix, "count": n} for prefix, n in errors.most_common(TOP_ERRORS)],
        "stale": {
            "stale": sum(index.get("stale") is True for index in with_index),
            "queries_with_index": len(with_index),
        },
        "index_refresh": {
            "triggers": dict(Counter(str(result.get("trigger")) for result in refresh_results)),
            "changed_count": dict(Counter(json.dumps(result.get("changed_count")) for result in refresh_results)),
        },
        "by_runtime": {runtime: dict(counts) for runtime, counts in sorted(by_runtime.items())},
    }


def skill_and_session_stats(cli: list[dict], skills: list[dict], tools: list[dict]) -> dict:
    """Aggregate skill starts, sessions, cross-layer chains, tool volume and the timeline of one cohort.

    Args:
        cli: Cohort CLI records.
        skills: Cohort skill records.
        tools: Cohort tool records.

    Returns:
        Skill-start counts, session counts, linked skill-to-CLI chains, index-only sessions, tool counts per runtime,
        and first/last timestamps.
    """
    starts = [record for record in skills if record.get("event") == "start"]
    cli_per_session = Counter(record.get("session") for record in cli if record.get("session"))
    skill_sessions = {record.get("session") for record in starts if record.get("session")}
    linked = skill_sessions & set(cli_per_session)
    index_only = {
        session
        for session in cli_per_session
        if all(record.get("cmd") == "index" for record in cli if record.get("session") == session)
    }
    tool_counts: dict[str, Counter] = defaultdict(Counter)
    for record in tools:
        tool_counts[record["_runtime"]][str(record.get("tool"))] += 1
    stamps = sorted(stamp for stamp in (_parse_ts(r.get("ts")) for r in cli + skills + tools) if stamp is not None)
    return {
        "skill_starts": dict(Counter(str(record.get("skill")) for record in starts).most_common()),
        "sessions": len(set(cli_per_session) | skill_sessions),
        "linked_skill_sessions": len(linked),
        "avg_cli_calls_per_linked_session": (
            round(sum(cli_per_session[s] for s in linked) / len(linked), 2) if linked else None
        ),
        "index_only_sessions": len(index_only),
        "tool_records": {runtime: dict(counts) for runtime, counts in sorted(tool_counts.items())},
        "first_ts": stamps[0].isoformat() if stamps else None,
        "last_ts": stamps[-1].isoformat() if stamps else None,
    }


def cohort_breakdown(dataset: dict[str, list[dict]]) -> dict:
    """Split organic records into version cohorts and aggregate each one separately.

    Args:
        dataset: Records per layer from :func:`load_dataset`.

    Returns:
        Exclusion counts, per-version record counts, the primary (newest) cohort name and per-cohort statistics.
    """
    excluded_scripted = dict.fromkeys(LAYERS, 0)
    unversioned = dict.fromkeys(LAYERS, 0)
    grouped: dict[str, dict[str, list[dict]]] = defaultdict(lambda: {layer: [] for layer in LAYERS})
    for layer, records in dataset.items():
        for record in records:
            version = record.get("v")
            if _is_scripted(record, layer):
                excluded_scripted[layer] += 1
            elif not isinstance(version, str) or not version:
                unversioned[layer] += 1
            else:
                grouped[version][layer].append(record)
    versions = sorted(grouped, key=_version_key, reverse=True)
    return {
        "excluded_scripted": excluded_scripted,
        "unversioned_excluded": unversioned,
        "versions": {version: {layer: len(grouped[version][layer]) for layer in LAYERS} for version in versions},
        "primary_cohort": versions[0] if versions else None,
        "cohorts": {
            version: {
                "cli": cli_stats(grouped[version]["cli"]),
                **skill_and_session_stats(
                    grouped[version]["cli"], grouped[version]["skills"], grouped[version]["tools"]
                ),
            }
            for version in versions
        },
    }


def session_events(dataset: dict[str, list[dict]]) -> dict:
    """Return the chronological event list for a ``--session`` report, capped at :data:`MAX_SESSION_EVENTS`."""
    events = [
        {
            "ts": record.get("ts"),
            "layer": layer,
            "runtime": record["_runtime"],
            "what": record.get("cmd") or record.get("skill") or record.get("tool"),
            "event": record.get("event"),
        }
        for layer, records in dataset.items()
        for record in records
    ]
    events.sort(key=lambda event: str(event["ts"]))
    return {"total": len(events), "truncated": len(events) > MAX_SESSION_EVENTS, "events": events[:MAX_SESSION_EVENTS]}


class HelperTimeoutError(RuntimeError):
    """A sibling helper ran past :data:`HELPER_TIMEOUT_SECONDS`; ``str(exc)`` is the message shown to the user."""


def _run_helper(script: str, args: Iterable[str]) -> subprocess.CompletedProcess[str]:
    """Run a sibling ``bin/`` helper with this interpreter and capture its output.

    Raises:
        HelperTimeoutError: if the helper is still running after :data:`HELPER_TIMEOUT_SECONDS`;
            :func:`subprocess.run` has already killed it.
    """
    try:
        return subprocess.run(  # noqa: S603 — fixed sibling script, argument list, no shell
            [sys.executable, str(_BIN_DIR / script), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=HELPER_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise HelperTimeoutError(f"{script} did not finish within {HELPER_TIMEOUT_SECONDS} s") from exc


def anonymize_copy(staged: Path, export_dir: Path, log_root: Path) -> dict:
    """Anonymize the filtered copy into a fresh run-scoped export directory.

    Args:
        staged: Filtered raw copy.
        export_dir: New run-scoped directory below ``.cache/codemap/export``.
        log_root: Telemetry root holding the stable ``.salt``.

    Returns:
        ``ok`` (exit 0 and at least one ``-anon.jsonl`` written), exit code and the helper's message lines.
    """
    proc = _run_helper(
        "anonymize.py", ["--input", str(staged), "--out-dir", str(export_dir), "--salt", str(log_root / ".salt")]
    )
    written = proc.returncode == 0 and export_dir.is_dir() and any(export_dir.rglob("*-anon.jsonl"))
    return {"ok": written, "rc": proc.returncode, "message": (proc.stdout + proc.stderr).strip()}


def module_overlap(dataset_root: Path) -> dict:
    """Run ``join_avoidance.py`` on the selected dataset and return its JSON, or the failure."""
    proc = _run_helper(
        "join_avoidance.py", ["--logs", str(dataset_root), "--window-min", str(WINDOW_MINUTES), "--json"]
    )
    try:
        return (
            json.loads(proc.stdout) if proc.returncode == 0 else {"error": proc.stderr.strip(), "rc": proc.returncode}
        )
    except ValueError:
        return {"error": "join_avoidance.py printed no JSON", "rc": proc.returncode}


def report_path(project_root: Path, options: DebriefOptions, today: date) -> Path:
    """Return the report path: ``--output`` verbatim, else the first free dated default.

    Args:
        project_root: Canonical project root.
        options: Parsed flags.
        today: Date stamped into the default name.

    Returns:
        The path to write; the default never names an existing file.
    """
    if options.output:
        return Path(options.output)
    base = project_root / ".reports" / "codemap" / f"debrief-{today.isoformat()}"
    candidate, counter = base.with_suffix(".md"), 1
    while candidate.exists():
        counter += 1
        candidate = base.parent / f"{base.name}-{counter}.md"
    return candidate


def build_report(log_root: Path, project_root: Path, options: DebriefOptions, scratch: Path) -> dict:
    """Stage, optionally anonymize, aggregate and join; return the full statistics object.

    Args:
        log_root: Resolved telemetry root.
        project_root: Canonical project root (export and report location).
        options: Parsed flags.
        scratch: Empty private directory for the filtered copy.

    Returns:
        The JSON-serialisable statistics object.
    """
    stage = stage_filtered_copy(log_root, scratch, options)
    now = datetime.now(timezone.utc)
    anonymize = {"requested": options.anonymize, "ok": False}
    dataset_root = scratch
    if options.anonymize:
        export_dir = project_root / ".cache" / "codemap" / "export" / now.strftime("%Y-%m-%dT%H-%M-%SZ")
        anonymize |= anonymize_copy(scratch, export_dir, log_root) | {"export_dir": str(export_dir)}
        dataset_root = export_dir if anonymize["ok"] else scratch
    dataset = load_dataset(dataset_root)
    target = report_path(project_root, options, now.date())
    return {
        "schema": "debrief_stats_v1",
        "log_root": str(log_root),
        "dataset": "anonymized" if anonymize["ok"] else "raw",
        "anonymize": anonymize,
        "filters": {
            "since": options.since.isoformat() if options.since else None,
            "session": ("(filtered before anonymization)" if anonymize["ok"] else options.session)
            if options.session
            else None,
        },
        "report_path": str(target),
        "shards": stage.shards,
        "records": stage.kept,
        "malformed_lines": stage.malformed_lines,
        "ts_unparsable_excluded": stage.ts_unparsable_excluded,
        "session_not_found_in": [layer for layer in LAYERS if options.session and not stage.kept[layer]],
        **cohort_breakdown(dataset),
        "session_events": session_events(dataset) if options.session else None,
        "module_overlap": module_overlap(dataset_root),
    }


def main(argv: list[str] | None = None) -> int:
    """Parse flags, compute the statistics and print them as one JSON object.

    Args:
        argv: Override ``sys.argv[1:]`` (mainly for testing).

    Returns:
        ``0`` on success, ``1`` when no telemetry exists, ``2`` on bad arguments, ``3`` when a sibling helper timed
        out.
    """
    try:
        options = parse_options(tokens_from_argv(sys.argv[1:] if argv is None else argv))
    except OptionsError as exc:
        print(f"! {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT
    log_root = _runtime_log.log_root()
    if not any(_shard_paths(log_root, layer) for layer in LAYERS):
        print(
            f"No codemap telemetry found under {log_root}. Run any /codemap-py:* skill or "
            "`codemap-py query`/`index` command to start collecting logs.",
            file=sys.stderr,
        )
        return EXIT_NO_TELEMETRY
    project_root = _runtime_log.canonical_root()
    with tempfile.TemporaryDirectory(prefix="codemap-debrief-") as scratch:
        try:
            stats = build_report(log_root, project_root, options, Path(scratch))
        except HelperTimeoutError as exc:
            # returning inside the ``with`` still deletes the raw scratch copy on the way out
            print(f"! {exc} — helper killed, debrief stopped; the filtered scratch copy was removed", file=sys.stderr)
            return EXIT_HELPER_TIMEOUT
    Path(stats["report_path"]).parent.mkdir(parents=True, exist_ok=True)
    print(json.dumps(stats, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
