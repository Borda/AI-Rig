#!/usr/bin/env python3
"""Read and check the append-only audit log the auto-allow hooks write.

Purpose:
    Every plugin's Bash auto-allow dispatcher appends one record describing what it decided, and its closer appends one
    describing what it later observed. Nothing coordinates those writers. This command is the reader that turns their
    independent observations back into a picture of each tool call, and the only component permitted to delete a log
    file.

Scope:
    ``verify`` reports integrity, schema conformance and per-call classification for one file or a whole directory.
    ``prune`` removes whole session files older than a cutoff. Both default to ``~/.claude/logs/audit``.

    ``verify`` reads any ``*.jsonl`` it is pointed at. ``prune`` deletes only files named ``s-<key>.jsonl``, this
    writer's own per-session form, whatever path it is given — so a mistyped directory containing other logs loses
    nothing, and naming a single file of any other shape deletes nothing.

What an integrity failure does and does not mean:
    ``record_hash`` detects a record that changed after it was written — a truncated write, a botched edit, a corrupt
    disk. It is NOT tamper-evidence: the same user owns the log, this checker and the hooks that wrote it. Nothing here
    can establish that a record was not forged by whoever could also edit this file.

Reading the classifications honestly:
    Most classifications describe what was OBSERVED, not what was true. ``observed-abstention`` means no allow was seen,
    never that every installed plugin abstained. ``incomplete-evidence`` cannot detect a plugin that never wrote at all,
    and a plugin may legitimately write nothing when neither of its modules reached a decision. An incomplete group is
    indistinguishable from a refusal at the prompt. Authority is usually unknowable, and a local allow never
    established that a call was authorised overall.

Usage:
    python plugins/cc_foundry/bin/verify_blueprint_audit.py verify [PATH] [--json]
    python plugins/cc_foundry/bin/verify_blueprint_audit.py prune [PATH] [--older-than DAYS] [--dry-run]

Outputs:
    A per-file and total report as text, or as one JSON object with ``files``, ``totals`` and ``exit`` when ``--json``
    is given. Each per-file object carries one integer per bucket name plus ``counts``, whose ``parent`` map holds the
    derived lineage per tool call so downstream tooling never parses prose.

Failure:
    Exit 1 only when a record's own hash does not verify. Every other finding — torn framing from concurrent appends,
    a non-conforming record, a missing observation — is a warning and exits 0. Exit 2 for an unusable path or a
    ``--older-than`` below one day.

    Exit 0 therefore means "no record failed its own hash", never "the whole log was read". A file that hit
    ``--max-lines`` stops there and reports ``limit-exceeded``, so its findings cover a prefix. Separately, a
    ``_no-session.jsonl`` at its size cutoff reports ``capped``: that file was read in full, but the writer refused to
    append to it, so the missing records never reached disk. Both still exit 0, so a caller that wants completeness
    must read those two buckets rather than the exit code. The text report states each one whenever it fires.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Iterator
from pathlib import Path

#: Records with no usable session id share this file. Every "one file is one session" rule excludes it.
NO_SESSION_FILE = "_no-session.jsonl"

#: Default log directory, matching the writer's.
DEFAULT_LOG_DIR = Path.home() / ".claude" / "logs" / "audit"

#: Default line ceiling per file. Exceeding it stops that file and is reported, never fatal.
DEFAULT_MAX_LINES = 5_000_000

#: Default soft cutoff for `_no-session.jsonl`, kept equal to the writer's `RIG_AUDIT_NOSESSION_MAX_BYTES`.
DEFAULT_NOSESSION_MAX_BYTES = 64 * 1024 * 1024

#: Default retention for `prune`, in days.
DEFAULT_RETENTION_DAYS = 30

#: Glob for the per-session files this writer creates. `prune` deletes nothing that does not match it.
SESSION_FILE_GLOB = "s-*.jsonl"

#: Regex form of `SESSION_FILE_GLOB`, applied when `prune` is pointed at a single file rather than a directory.
SESSION_FILE_RE = re.compile(r"^s-[0-9a-f]{32}\.jsonl$")

#: Bucket names, in report order. These literals are the contract downstream tooling reads.
BUCKETS = (
    "corrupt-record",
    "truncated",
    "schema-invalid",
    "same-agent-duplicate-after-row",
    "dual-close",
    "no-decision-evidence",
    "incomplete-evidence",
    "incomplete-closure",
    "observed-abstention",
    "multi-allow",
    "repeat-observation",
    "unjoinable",
    "completion-unobserved",
    "in-flight-or-hard-kill",
    "capped",
    "limit-exceeded",
)

#: Fields every conforming record carries. `session_id`, `parent_record_id` and `prev_hash` may be null but must exist.
REQUIRED_FIELDS = (
    "record_id",
    "timestamp",
    "agent_id",
    "agent_version",
    "session_id",
    "action_type",
    "action_detail",
    "outcome",
    "trust_level",
    "parent_record_id",
    "prev_hash",
    "record_phase",
    "record_hash",
)

ACTION_TYPES = frozenset({"tool.bash", "session.start", "session.end"})
RECORD_PHASES = frozenset({"pre_execution", "post_execution"})
LANES = frozenset({"blueprint", "shape", "none"})
DECISIONS = frozenset({"allow", "passthrough", "none"})
#: `plugin` and `unknown` are the only authorities a hook may write; `human` is reserved and never emitted.
TRUST_LEVELS = frozenset({"plugin", "unknown"})
#: `timeout`, `denied` and `escalated` stay reserved: no host signal supports them, so a record with one is not ours.
BEFORE_OUTCOMES = frozenset({"pending"})
AFTER_OUTCOMES = frozenset({"success", "failure"})
CLOSE_EVENTS = frozenset({"PostToolUse", "PostToolUseFailure"})
CLOSE_STATUSES = frozenset({"ok", "error"})


def canonical(record: dict) -> str:
    """Return the canonical serialization of ``record`` with ``record_hash`` omitted.

    Normative and shared with the JavaScript writer: UTF-8, no whitespace, keys sorted, non-ASCII left unescaped.

    Examples:
        >>> canonical({"b": 1, "a": None, "record_hash": "x"})
        '{"a":null,"b":1}'
    """
    body = {key: value for key, value in record.items() if key != "record_hash"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def record_hash(record: dict) -> str | None:
    """Return the SHA-256 hex of a record's canonical bytes.

    A record holding a lone surrogate cannot be UTF-8 encoded. The writer refuses to emit one, so a stored record
    carrying one is damage; it is reported as unhashable rather than allowed to abort the run.

    Examples:
        >>> record_hash({"a": 1})[:8]
        '015abd7f'
        >>> record_hash({"a": "\\ud800"}) is None
        True
    """
    try:
        return hashlib.sha256(canonical(record).encode("utf-8")).hexdigest()
    except UnicodeEncodeError:
        return None


def row_kind(record: dict) -> str | None:
    """Return ``before``, ``after``, ``lifecycle``, or None when the record is none of those.

    Examples:
        >>> row_kind({"action_type": "tool.bash", "record_phase": "pre_execution"})
        'before'
        >>> row_kind({"action_type": "session.end", "record_phase": "post_execution"})
        'lifecycle'
    """
    action, phase = record.get("action_type"), record.get("record_phase")
    if action == "tool.bash":
        return {"pre_execution": "before", "post_execution": "after"}.get(phase)
    if action in ("session.start", "session.end") and phase == "post_execution":
        return "lifecycle"
    return None


def _detail_findings_before(detail: dict, record: dict) -> list[str]:
    """Return schema findings for a before-row's ``action_detail``."""
    findings = []
    if detail.get("decision") not in DECISIONS or detail.get("lane") not in LANES:
        findings.append("before-row needs a valid decision and lane")
    verdicts = detail.get("verdicts")
    if not isinstance(verdicts, list) or len(verdicts) != 2:
        findings.append("before-row needs exactly two verdicts")
    else:
        lanes = [entry.get("lane") if isinstance(entry, dict) else None for entry in verdicts]
        if len(set(lanes)) != 2 or not set(lanes) <= LANES:
            findings.append("verdicts must carry two distinct known lanes")
        if any(not isinstance(entry, dict) or entry.get("decision") not in DECISIONS for entry in verdicts):
            findings.append("each verdict needs a valid decision")
    if detail.get("decision") != "allow" and ("rank" in detail or "src" in detail):
        findings.append("rank and src belong to an allow only")
    if record.get("outcome") not in BEFORE_OUTCOMES:
        findings.append("a before-row's outcome is always pending")
    if {"status", "event", "reason"} & set(detail):
        findings.append("status, event and reason are forbidden on a before-row")
    return findings


def _detail_findings_after(detail: dict, record: dict) -> list[str]:
    """Return schema findings for an after-row's ``action_detail``."""
    findings = []
    # Tested as strings first: an unhashable value on either side raises out of the `in` test and ends the run, and
    # a record carrying one is exactly the malformed input this function exists to classify.
    status, event = detail.get("status"), detail.get("event")
    if not isinstance(status, str) or not isinstance(event, str):
        findings.append("after-row needs a known status and event")
    elif status not in CLOSE_STATUSES or event not in CLOSE_EVENTS:
        findings.append("after-row needs a known status and event")
    if {"decision", "lane", "verdicts", "rank", "src", "digest", "reason"} & set(detail):
        findings.append("decision fields are forbidden on an after-row")
    if record.get("outcome") not in AFTER_OUTCOMES:
        findings.append("an after-row's outcome is success or failure")
    return findings


def _detail_findings_lifecycle(detail: dict, record: dict) -> list[str]:
    """Return schema findings for a lifecycle row's ``action_detail``."""
    findings = []
    if set(detail) - {"reason"}:
        findings.append("a lifecycle row carries at most a reason")
    if "reason" in detail and record.get("action_type") != "session.end":
        findings.append("reason belongs to session.end only")
    if "tool_use_id" in record:
        findings.append("a lifecycle row carries no tool_use_id")
    if record.get("outcome") != "success":
        findings.append("a lifecycle row's outcome is success")
    return findings


def schema_findings(record: dict) -> list[str]:
    """Return every way ``record`` fails the record contract; empty means conforming.

    Examples:
        >>> schema_findings({})[:1]
        ['missing required fields']
    """
    if any(field not in record for field in REQUIRED_FIELDS):
        return ["missing required fields"]
    kind = row_kind(record)
    if kind is None:
        return ["unknown action_type or record_phase combination"]
    findings = []
    if record.get("action_type") not in ACTION_TYPES or record.get("record_phase") not in RECORD_PHASES:
        findings.append("unknown action_type or record_phase")
    if record.get("trust_level") not in TRUST_LEVELS:
        findings.append("unknown trust_level")
    if record.get("parent_record_id") is not None or record.get("prev_hash") is not None:
        findings.append("parent_record_id and prev_hash are always null — this log is not chained")
    detail = record.get("action_detail")
    if not isinstance(detail, dict):
        return [*findings, "action_detail must be an object"]
    if kind != "lifecycle" and "tool_use_id" not in record:
        findings.append("a tool.bash row carries a tool_use_id, even when null")
    if "project" not in record:
        findings.append("project is required, even when null")
    checks = {
        "before": _detail_findings_before,
        "after": _detail_findings_after,
        "lifecycle": _detail_findings_lifecycle,
    }
    return [*findings, *checks[kind](detail, record)]


class FileReport:
    """Buckets and counts for one log file."""

    def __init__(self) -> None:
        #: Why the read of this file stopped short, when it did. Kept off ``as_dict`` because the JSON shape is a
        #: pinned contract; the same stop is visible there as ``limit-exceeded``, and here as a reason to print.
        self.read_error: str | None = None
        self.buckets: Counter = Counter({name: 0 for name in BUCKETS})
        self.counts: dict[str, Counter | dict] = {
            "record_phase": Counter(),
            "outcome": Counter(),
            "trust_level": Counter(),
            "agent_id": Counter(),
            "lane": defaultdict(Counter),
            "parent": {},
        }

    def as_dict(self) -> dict:
        """Return the report as plain JSON-serializable data."""
        return {
            **{name: int(self.buckets[name]) for name in BUCKETS},
            "counts": {
                "record_phase": dict(self.counts["record_phase"]),
                "outcome": dict(self.counts["outcome"]),
                "trust_level": dict(self.counts["trust_level"]),
                "agent_id": dict(self.counts["agent_id"]),
                "lane": {lane: dict(inner) for lane, inner in self.counts["lane"].items()},
                "parent": dict(self.counts["parent"]),
            },
        }

    def merge(self, other: FileReport) -> None:
        """Fold another file's report into this one."""
        self.buckets.update(other.buckets)
        for key in ("record_phase", "outcome", "trust_level", "agent_id"):
            self.counts[key].update(other.counts[key])
        for lane, inner in other.counts["lane"].items():
            self.counts["lane"][lane].update(inner)
        self.counts["parent"].update(other.counts["parent"])


def read_rows(path: Path, max_lines: int, report: FileReport) -> list[tuple[int, dict]]:
    """Return ``(line index, record)`` for every parseable line, counting torn framing and hash failures.

    A line that does not parse is torn framing, which concurrent appends produce and which is never corruption. A line
    that parses but whose hash does not verify is corruption, and is the only finding that fails the run.

    A read that dies partway — a failing disk, a mount that went away, a file whose permissions changed under us — keeps
    whatever it already established rather than discarding it. Corruption found in the prefix is still corruption, and
    dropping the file whole would answer "no corrupt records" about records this function has already read and rejected.
    The stop is recorded as ``limit-exceeded``, which is what it is: this read stopped early, so every finding is a
    prefix and the group-level ones are provisional. ``read_error`` carries the reason to print.
    """
    rows: list[tuple[int, dict]] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        try:
            _read_lines(handle, max_lines, report, rows)
        except OSError as error:
            report.read_error = error.strerror or str(error)
            report.buckets["limit-exceeded"] += 1
    return rows


def _read_lines(handle: Iterator[str], max_lines: int, report: FileReport, rows: list[tuple[int, dict]]) -> None:
    """Classify each line into ``rows`` or a bucket, stopping at ``max_lines``."""
    for index, line in enumerate(handle):
        if index >= max_lines:
            report.buckets["limit-exceeded"] += 1
            break
        text = line.strip()
        if not text:
            continue
        try:
            record = json.loads(text)
        except ValueError:
            report.buckets["truncated"] += 1
            continue
        if not isinstance(record, dict):
            report.buckets["schema-invalid"] += 1
            continue
        # An unhashable record yields None, and a record with no `record_hash` also reads as None — comparing the two
        # directly would let the second pass as verified. The computed side is tested first for that reason.
        computed = record_hash(record)
        if computed is None or record.get("record_hash") != computed:
            report.buckets["corrupt-record"] += 1
            continue
        rows.append((index, record))


def _tally(record: dict, report: FileReport) -> None:
    """Add one conforming record to the descriptive counts."""
    report.counts["record_phase"][str(record.get("record_phase"))] += 1
    report.counts["outcome"][str(record.get("outcome"))] += 1
    report.counts["trust_level"][str(record.get("trust_level"))] += 1
    report.counts["agent_id"][str(record.get("agent_id"))] += 1
    detail = record.get("action_detail") or {}
    for verdict in detail.get("verdicts") or []:
        lane = str(verdict.get("lane"))
        decision = verdict.get("decision")
        # `none` and `module-error` are counted apart from `passthrough`: an absent opinion is not an abstention.
        label = "module-error" if decision == "none" and verdict.get("why") == "module-error" else str(decision)
        report.counts["lane"][lane][label] += 1


def _derive_parent(before_rows: list[dict]) -> str | None:
    """Return the record id of the allow with the lowest rank, ties broken by the smallest record id.

    Derived lineage only. Nothing stored says who the parent was, so there is nothing here to forge or to detect.
    """
    allows = [row for row in before_rows if (row.get("action_detail") or {}).get("decision") == "allow"]
    if not allows:
        return None
    ranked = sorted(allows, key=lambda row: ((row["action_detail"].get("rank") or 99), str(row.get("record_id"))))
    return str(ranked[0].get("record_id"))


def _classify_group(rows: list[tuple[int, dict]], context: dict, report: FileReport) -> None:
    """Classify one ``(session_id, tool_use_id)`` group and record its buckets."""
    before = [record for _, record in rows if row_kind(record) == "before"]
    after = [record for _, record in rows if row_kind(record) == "after"]

    writers = {str(record.get("agent_id")) for record in before}
    if before and writers < context["all_writers"]:
        report.buckets["incomplete-evidence"] += 1
    # The mirror of the check above, on the closure side. Without it the corroboration claim — several plugins
    # reporting one completion is agreement — is made but never checked, and a call that four plugins decided but
    # only one closed reads exactly like one all four closed.
    closers = {str(record.get("agent_id")) for record in after}
    if after and closers < context["all_closers"]:
        report.buckets["incomplete-closure"] += 1
    if len(before) > len(writers):
        report.buckets["repeat-observation"] += 1
    if not before:
        report.buckets["no-decision-evidence"] += 1

    allowed = {str(r.get("agent_id")) for r in before if (r.get("action_detail") or {}).get("decision") == "allow"}
    declined = any((r.get("action_detail") or {}).get("decision") == "passthrough" for r in before)
    if len(allowed) > 1:
        report.buckets["multi-allow"] += 1
    if before and not allowed and declined and writers >= context["all_writers"]:
        report.buckets["observed-abstention"] += 1

    seen_closes: Counter = Counter(
        (str(record.get("agent_id")), (record.get("action_detail") or {}).get("event")) for record in after
    )
    report.buckets["same-agent-duplicate-after-row"] += sum(1 for count in seen_closes.values() if count > 1)
    outcomes = {record.get("outcome") for record in after}
    if {"success", "failure"} <= outcomes:
        report.buckets["dual-close"] += 1

    if before and not after:
        last_before = max(index for index, record in rows if row_kind(record) == "before")
        bucket = "completion-unobserved" if context["last_session_end"] > last_before else "in-flight-or-hard-kill"
        report.buckets[bucket] += 1

    parent = _derive_parent(before)
    if parent is not None:
        report.counts["parent"][f"{context['session_id']}|{context['tool_use_id']}"] = parent


def verify_file(path: Path, max_lines: int, nosession_max_bytes: int) -> FileReport:
    """Return the full report for one log file."""
    report = FileReport()
    if path.name == NO_SESSION_FILE and path.stat().st_size >= nosession_max_bytes:
        report.buckets["capped"] += 1

    rows = read_rows(path, max_lines, report)
    conforming: list[tuple[int, dict]] = []
    for index, record in rows:
        if schema_findings(record):
            report.buckets["schema-invalid"] += 1
            continue
        conforming.append((index, record))
        _tally(record, report)

    groups: dict[tuple[str, str], list[tuple[int, dict]]] = defaultdict(list)
    last_session_end = -1
    all_writers: set[str] = set()
    all_closers: set[str] = set()
    for index, record in conforming:
        kind = row_kind(record)
        if kind == "lifecycle":
            if record.get("action_type") == "session.end":
                last_session_end = index
            continue
        session_id, tool_use_id = record.get("session_id"), record.get("tool_use_id")
        # Both must be strings, not merely present: these become a dict key, and a list or dict value here raises
        # TypeError and takes the whole run down. The writer only ever emits a string or null, so anything else is a
        # record this reader did not write and cannot join.
        if not isinstance(session_id, str) or not isinstance(tool_use_id, str):
            report.buckets["unjoinable"] += 1
            continue
        groups[(session_id, tool_use_id)].append((index, record))
        if kind == "before":
            all_writers.add(str(record.get("agent_id")))
        elif kind == "after":
            all_closers.add(str(record.get("agent_id")))

    for (session_id, tool_use_id), rows_in_group in groups.items():
        context = {
            "all_writers": all_writers,
            "all_closers": all_closers,
            "last_session_end": last_session_end,
            "session_id": session_id,
            "tool_use_id": tool_use_id,
        }
        _classify_group(rows_in_group, context, report)
    return report


def log_files(target: Path) -> list[Path]:
    """Return the log files under ``target``, which may be a directory or a single file.

    Read-only callers get every ``*.jsonl`` in a directory, or exactly the file named. Deletion uses ``prunable_files``
    instead, which is deliberately narrower.

    A named target is honoured whenever it reads as a regular file, symlink included: following a link to read it is
    harmless, and refusing one the caller named explicitly would answer "clean" about a file nobody looked at. That is
    the one way this function could lie. ``prunable_files`` refuses links because it deletes, which is not symmetric.

    A directory scan takes regular files that are not links, for three reasons. ``open`` raises on a directory and on a
    dangling symlink, and an uncaught raise from a read exits 1 — the code reserved for ``corrupt-record`` — so one
    stray directory would report the loudest failure the tool has about records that are perfectly intact, while the
    real files beside it went unread. A scan also reads paths the caller never named one by one, so following a link out
    of the directory would read a file nobody asked for. And a link to a sibling in the same directory would be verified
    twice, doubling its counts. The second and third reasons cover a link to a perfectly good log, which does not raise;
    that is why the filter is wider than the first reason alone would justify. Entries skipped that way are named by
    ``run_verify`` rather than passed over in silence.

    One shape no type test can catch: a regular file this process may not open. It passes every test here and raises at
    the read instead, so ``run_verify`` guards the open, and ``read_rows`` guards the reading that follows. Both guards
    catch ``OSError`` and nothing wider — a single pathological line large enough to exhaust memory raises
    ``MemoryError``, which is not an ``OSError`` and is not caught. ``--max-lines`` bounds how many lines are read,
    never how long one may be.
    """
    if target.is_file():
        return [target]
    if not target.is_dir():
        return []
    return sorted(path for path in target.glob("*.jsonl") if path.is_file() and not path.is_symlink())


def unreadable_entries(target: Path) -> list[tuple[Path, str]]:
    """Return the ``*.jsonl`` entries of a directory ``log_files`` will not read, each with the reason.

    Skipping them is right, but a silent skip is its own defect: the caller can then say which files it passed over
    instead of leaving the reader to wonder why the scan covered fewer files than the directory holds.

    The reason has to be the real one. A symlink pointing at a healthy log **is** a regular file, so reporting it as
    "not a regular file" would send its owner looking for damage that is not there; it was skipped because a directory
    scan does not follow links, which is a different statement about a different file.
    """
    if not target.is_dir():
        return []
    skipped = []
    for path in sorted(target.glob("*.jsonl")):
        if path.is_symlink():
            skipped.append((path, "symlink, and a directory scan does not follow links"))
        elif not path.is_file():
            skipped.append((path, "not a regular file"))
    return skipped


def prunable_files(target: Path) -> list[Path]:
    """Return only the per-session files ``prune`` may delete.

    Selection is by the writer's own filename shape, never by extension. ``log_files`` would hand back every ``*.jsonl``
    in a directory, and ``~/.claude/logs`` — one path component above the default target — holds ``timings.jsonl`` and
    ``invocations.jsonl``, two unrelated append-only logs that a mistyped path would otherwise destroy. A single-file
    target is held to the same shape, so naming any other file deletes nothing.

    ``_no-session.jsonl`` matches neither form and is never returned: it is never pruned by age.

    Only regular files qualify. A directory carrying a matching name would reach ``unlink`` and raise; a symlink
    carrying one would be judged on its target's age and then removed, which deletes a link this writer never made.
    Neither is something the writer creates, so both are skipped rather than handled.
    """
    if target.is_file() and not target.is_symlink():
        return [target] if SESSION_FILE_RE.fullmatch(target.name) else []
    if not target.is_dir():
        return []
    return sorted(
        path
        for path in target.glob(SESSION_FILE_GLOB)
        if SESSION_FILE_RE.fullmatch(path.name) and path.is_file() and not path.is_symlink()
    )


def render_text(per_file: dict[str, dict], totals: dict, exit_code: int, skipped: int = 0) -> str:
    """Return the human-readable report."""
    lines = []
    for name, data in per_file.items():
        flagged = [f"{bucket}: {data[bucket]}" for bucket in BUCKETS if data[bucket]]
        lines.append(f"{name}: {', '.join(flagged) if flagged else 'clean'}")
        for group, parent in sorted(data["counts"]["parent"].items()):
            lines.append(f"    parent {group} -> {parent}")
    flagged_totals = [f"{bucket}: {totals[bucket]}" for bucket in BUCKETS if totals[bucket]]
    # State the scope of the scan before its verdict. "clean" over zero files and "clean" over nine are the same two
    # words about very different runs, and an empty directory, a mistyped path that happens to exist, or a directory
    # whose every entry was skipped all produce the first one while looking like the second.
    lines.append(f"files read: {len(per_file)}")
    if skipped:
        # The numerator alone is not the scope. A directory where one log of two could not be opened prints
        # `files read: 1` and a clean verdict, and stderr — which a caller redirecting stdout never sees — was the only
        # place saying a file was missed. A quiet all-clear is the failure this whole line exists to prevent.
        lines.append(f"files SKIPPED: {skipped} could not be read at all; see stderr for which and why.")
    if not per_file:
        lines.append("nothing was read: no readable log file under the target, so 'clean' means nothing was checked.")
    lines.append(f"totals: {', '.join(flagged_totals) if flagged_totals else 'clean'}")
    # Two different losses, deliberately not merged. `limit-exceeded` means THIS READ stopped early, so the findings
    # below cover a prefix. `capped` means the WRITER refused to append to the shared stream; the file was read in
    # full, and what is missing never reached disk. Both exit 0, so each has to be stated rather than inferred from a
    # bucket an operator may not read.
    if totals["limit-exceeded"]:
        lines.append(
            "scan INCOMPLETE: a file stopped early — at its line limit, or because the read failed partway, which is "
            "named on stderr. Findings cover only the part that was read, and a call whose completion row lies past "
            "the cut is counted as unobserved. Raise --max-lines if the limit was the cause."
        )
    if totals["capped"]:
        lines.append(
            "writer SATURATED: the shared no-session stream is at its size cutoff, so records were refused rather "
            "than appended. This scan read the whole file; the missing records never reached disk."
        )
    lines.append(
        "corrupt-record fails the run; every other finding is a warning. "
        "Integrity here means the record has not changed since it was written — never that it was not forged."
        if exit_code
        else "no corrupt records."
    )
    return "\n".join(lines)


def run_verify(args: argparse.Namespace) -> int:
    """Verify one path and print the report."""
    target = args.path
    if not target.exists():
        print(f"no such path: {target}", file=sys.stderr)
        return 2
    # `glob` swallows the error when a directory cannot be listed, so both `log_files` and `unreadable_entries` return
    # empty and the run would print a clean report about a directory it never saw inside. Ask once, up front, and fail
    # the way a missing path fails.
    if target.is_dir():
        try:
            next(target.iterdir(), None)
        except OSError as error:
            print(f"cannot list {target}: {error.strerror or error}", file=sys.stderr)
            return 2
    totals = FileReport()
    per_file: dict[str, dict] = {}
    skipped = 0
    for entry, reason in unreadable_entries(target):
        print(f"skipped {entry}: {reason}", file=sys.stderr)
    for path in log_files(target):
        # Guarded the same way the delete loop is, and for the same reason. A file this process cannot open — mode 000,
        # another owner, a mount that went away — passes every type test `log_files` applies, so it arrives here and
        # `open` raises. Uncaught, that exits 1: the code reserved for `corrupt-record`, reported about records nobody
        # read, with the whole report discarded and the healthy files beside it never mentioned. A read that fails
        # after opening is not caught here: `read_rows` keeps what it already found and reports the stop instead.
        try:
            report = verify_file(path, args.max_lines, args.nosession_max_bytes)
        except OSError as error:
            print(f"skipped {path}: {error.strerror or error}", file=sys.stderr)
            skipped += 1
            continue
        if report.read_error:
            print(f"read stopped in {path}: {report.read_error}", file=sys.stderr)
        per_file[path.name] = report.as_dict()
        totals.merge(report)
    summary = totals.as_dict()
    exit_code = 1 if summary["corrupt-record"] else 0
    if args.json:
        print(json.dumps({"files": per_file, "totals": summary, "exit": exit_code}, sort_keys=True))
    else:
        print(render_text(per_file, summary, exit_code, skipped))
    return exit_code


def run_prune(args: argparse.Namespace) -> int:
    """Remove session log files older than the cutoff and print every removal.

    The only component that deletes anything. It races nothing: it is explicitly invoked, single-process, and no hook
    ever removes a file. ``_no-session.jsonl`` is never pruned by age — a growing one means the host stopped sending a
    session id, which is a regression to investigate rather than a file to rotate.

    Two guards keep a mistyped invocation from destroying something else. Candidates come from ``prunable_files``, so
    only this writer's own ``s-<key>.jsonl`` files are ever eligible whatever the path points at. And an age below one
    day is refused rather than treated as "everything", because ``--older-than 0`` reads as a no-op and is not.
    """
    target = args.path
    if args.older_than < 1:
        print(f"--older-than must be at least 1 day, got {args.older_than}", file=sys.stderr)
        return 2
    if not target.exists():
        print(f"no such path: {target}", file=sys.stderr)
        return 2
    cutoff = time.time() - args.older_than * 86400
    keep = (
        f"s-{hashlib.sha256(args.keep_session.encode('utf-8')).hexdigest()[:32]}.jsonl" if args.keep_session else None
    )
    removed = 0
    for path in prunable_files(target):
        # `lstat` and a guarded unlink, because the loop must survive whatever it meets: a file that vanished between
        # the listing and the stat, one the user cannot delete, a mount that went away. An uncaught raise here would
        # abort the sweep partway, leaving every later file unpruned with no indication which ones were skipped.
        try:
            if path.name == keep or path.lstat().st_mtime >= cutoff:
                continue
            if not args.dry_run:
                path.unlink()
        except OSError as error:
            print(f"skipped {path}: {error.strerror or error}", file=sys.stderr)
            continue
        print(f"{'would remove' if args.dry_run else 'removed'} {path}")
        removed += 1
    print(f"{removed} file(s) {'would be removed' if args.dry_run else 'removed'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Return the command-line parser.

    Examples:
        >>> build_parser().parse_args(["verify"]).json
        False
    """
    parser = argparse.ArgumentParser(description="Verify or prune the auto-allow audit log.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    verify = subparsers.add_parser("verify", help="report integrity, schema conformance and per-call classification")
    verify.add_argument("path", nargs="?", type=Path, default=DEFAULT_LOG_DIR)
    verify.add_argument("--json", action="store_true", help="emit one JSON object instead of text")
    verify.add_argument("--max-lines", type=int, default=DEFAULT_MAX_LINES)
    verify.add_argument("--nosession-max-bytes", type=int, default=DEFAULT_NOSESSION_MAX_BYTES)
    verify.set_defaults(handler=run_verify)

    prune = subparsers.add_parser("prune", help="remove whole session log files older than a cutoff")
    prune.add_argument("path", nargs="?", type=Path, default=DEFAULT_LOG_DIR)
    prune.add_argument(
        "--older-than", type=int, default=DEFAULT_RETENTION_DAYS, help="age in days; minimum 1, below that is rejected"
    )
    prune.add_argument("--keep-session", default=None, help="session id whose file is never removed")
    prune.add_argument("--dry-run", action="store_true", help="list what would be removed and remove nothing")
    prune.set_defaults(handler=run_prune)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run one subcommand and return its exit code.

    Examples:
        >>> isinstance(main.__doc__, str)
        True
    """
    args = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
