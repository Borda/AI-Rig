#!/usr/bin/env python
"""Aggregate run gap ledgers (``gaps.jsonl``) across runs into a headline table counted by subject and gap identity.

A gap recorded by several runs of the same pull request (or, without a pull request, the same head commit) counts
once: the identity of a gap is its repository (compared case-insensitively), subject and ``id``, never the run
directory that recorded it. Each ledger contributes its last record per ``id``; closing events retract a gap and are
counted separately. A gap listed in the repository's known-limits file is shown once per repository and host class in
its own section and is not counted again per pull request. The ``known_limit`` flag alone decides nothing: a flagged
record that no given known-limits file lists is counted as an ordinary gap and named under unverified known-limit
claims. Known-limits files come from ``--known-limits`` and from ``.notes/known-limits.json`` directly under each
directory root. Ledgers that fail validation are named with their first error and left out of every count; they are
never silently dropped.

Runs that stopped before writing their terminal artifact are counted too, by harness, run kind, last checkpoint and
status, so a count built from finished runs alone no longer hides them:

- Codex: a run directory with no ``result.json`` and either a ``run-state.json`` stub or a Code Review
  ``review-entry.json`` allocation receipt. A stub supplies the last checkpoint and takes precedence over
  an allocation receipt, which records the ``allocated`` checkpoint.
- Claude review: a ``.reports/review/pr-<N>/run-<NNN>/`` directory without ``review-report.md``; its checkpoint is the
  furthest stage its files show (``allocated``, ``fan-out``, ``consolidation``, ``publication-refused``).
- Claude resolve: a ``.reports/resolve/<timestamp>/`` directory whose ``resolve-report.md`` is still the stub (first
  line ``<!-- resolve-report-status: incomplete -->``) or is missing; its checkpoint is the stub's ``Step N``.

Each such run has one status, decided in this order: ``purged`` (the directory holds no file at all, so its stage is
lost), ``superseded`` (a later run of the same pull request has its terminal artifact: a higher ``run-<NNN>`` with
``result.json`` or ``review-report.md``, or a later resolve run whose final report names the same PR),
``in-progress`` (its newest file is under 24 hours old, so it may still be running) and ``unfinished`` (none of these:
the run stopped and nothing replaced it). Only ``unfinished`` runs are lost work.

Usage:
    python gap_rollup.py ROOT [ROOT ...] [--known-limits FILE ...] [--format markdown|json] [--output PATH]

``ROOT`` is a ``gaps.jsonl`` file or a directory searched recursively for ``gaps.jsonl`` and run directories
(symlinked directories are not followed during traversal). A checkout root also searches ``codex-rig/reports``
under the common Git directory and linked worktree admin directories. The output is a derived report: write it
beside the ledgers or to any sidecar path, never into a ledger.

Exit codes:
    0   report produced
    1   neither a valid ledger nor an incomplete run found under the roots
    2   argument error, including an output path that names a ledger
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from functools import cache
from pathlib import Path
from typing import Any

import gap_ledger
from _report_io import write_report

#: Ledger file name every run writes.
LEDGER_NAME = "gaps.jsonl"
#: A repository's known-limits file, relative to a directory root, read without ``--known-limits``.
ROOT_LIMITS = Path(".notes") / "known-limits.json"
#: Stub a Codex run keeps until its terminal result is promoted, and that terminal result.
RUN_STATE_NAME = "run-state.json"
RESULT_NAME = "result.json"
REVIEW_ENTRY_NAME = "review-entry.json"
#: Status keys of the run-state stub, by the run kind that writes them.
RUN_STATE_STATUS_KEYS = {"review_status": "review", "remediation_status": "remediate"}
#: Terminal artifacts of Claude ``oss:review`` and ``oss:resolve`` runs.
REVIEW_REPORT_NAME = "review-report.md"
RESOLVE_REPORT_NAME = "resolve-report.md"
#: First line of a resolve report that is still the stub the run writes before its final report.
RESOLVE_STUB_MARKER = "<!-- resolve-report-status: incomplete -->"
#: A run whose newest file is younger than this may still be running.
RECENT_SECONDS = 24 * 60 * 60
#: Bytes of a resolve report read for its marker, heading and step line.
_REPORT_HEAD_BYTES = 4096
#: Directory names never searched for ledgers or run directories.
SKIP_DIRS = frozenset({".git", "node_modules", "__pycache__", ".venv", "venv", "site-packages"})
_RUN_DIR = re.compile(r"^run-(\d+)$")
_PR_DIR = re.compile(r"^pr-\d+$")
_STUB_STEP = re.compile(r"^\*\*Last step reached\*\*: (?P<step>.+)$", re.MULTILINE)
_STEP_TOKEN = re.compile(r"^Step \d+[a-z]?")
_REPORT_PR = re.compile(r"^## Resolve Report — PR #(?P<pr>\d+)\s*$", re.MULTILINE)
#: Review report-directory files, furthest stage first: the checkpoint is the first one present.
_REVIEW_STAGES = (
    ("publication-refused", ("review-report.refused.md",)),
    ("consolidation", ("gap-prepare.txt", "gap-identity.json")),
    ("fan-out", ("lineup.md", "fill-gaps.json")),
    ("allocated", ("head-sha.txt",)),
)


class RunStatus(str, Enum):
    """Why a run without its terminal artifact is, or is not, lost work."""

    UNFINISHED = "unfinished"
    SUPERSEDED = "superseded"
    PURGED = "purged"
    IN_PROGRESS = "in-progress"


@dataclass(frozen=True)
class IncompleteRun:
    """One run directory without its terminal artifact, with where it stopped and its status."""

    run_dir: Path
    harness: str
    kind: str
    checkpoint: str
    status: str


#: Plain-language meaning and recommended action per class, in headline order.
CLASS_ROWS: dict[str, tuple[str, str]] = {
    gap_ledger.GapClass.PROCESS.value: (
        "The workflow loses, mislabels or never writes its own result",
        "fix the workflow",
    ),
    gap_ledger.GapClass.ENVIRONMENT_PROVISIONABLE.value: (
        "A missing package, hook cache or index the workflow can provision",
        "provision a local ephemeral environment; stop recording it",
    ),
    gap_ledger.GapClass.HARDWARE_ABSENT.value: (
        "A hardware class this host does not have",
        "list once in known limits with the exact reason",
    ),
    gap_ledger.GapClass.PROJECT.value: (
        "A repository property such as base-branch CI or hosted checks",
        "route to the maintainer",
    ),
    gap_ledger.GapClass.CONTRIBUTION.value: (
        "A property of the contribution such as fork code policy",
        "route to the contributor or maintainer",
    ),
    gap_ledger.GapClass.HUMAN_DECISION.value: (
        "The user's explicit choice, such as a skipped push",
        "report as the user's decision, not as a process gap",
    ),
}


@dataclass
class GapIdentity:
    """Everything the rollup learned about one gap identity across runs."""

    classes: Counter[str] = field(default_factory=Counter)
    runs: set[str] = field(default_factory=set)
    carried: int = 0


@dataclass
class KnownLimitSeen:
    """A known limit seen in one repository, shown once."""

    hardware_class: str
    reason: str
    runs: set[str] = field(default_factory=set)


@dataclass
class Rollup:
    """Aggregated counts over every valid ledger found."""

    ledgers: int = 0
    records: int = 0
    closed: int = 0
    gaps: dict[tuple[str, str, str], GapIdentity] = field(default_factory=dict)
    known: dict[tuple[str, str, str, str], KnownLimitSeen] = field(default_factory=dict)
    unverified: dict[tuple[str, str, str, str], set[str]] = field(default_factory=dict)
    routes: Counter[tuple[str, str, str, str]] = field(default_factory=Counter)
    route_gaps: dict[tuple[str, str, str, str], set[tuple[str, str, str]]] = field(default_factory=dict)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    incomplete: Counter[tuple[str, str, str, str]] = field(default_factory=Counter)


def _find_named(roots: list[Path], *names: str) -> list[Path]:
    """Find matching files under each resolved root and its managed report directories."""
    wanted = set(names)
    # Keep the file name itself lexical so a symlinked ledger still reaches the ledger reader's refusal.
    found = {root.parent.resolve() / root.name for root in roots if root.name in wanted and root.is_file()}
    directories = {root.resolve() for root in roots if root.is_dir()}
    managed = {path for root in directories for path in _checkout_report_roots(root)}
    for root in directories | managed:
        for current, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = sorted(entry for entry in dirs if entry not in SKIP_DIRS)
            found.update(Path(current) / name for name in wanted.intersection(files))
    return sorted(found)


def _checkout_report_roots(root: Path) -> list[Path]:
    """Find managed report roots in a checkout's common and per-worktree Git directories."""
    marker = root / ".git"
    if marker.is_symlink() or not marker.exists():
        return []
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--show-toplevel", "--git-common-dir", "--absolute-git-dir"],  # noqa: S607
            cwd=root,
            env={key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")},
            capture_output=True,
            check=True,
            timeout=5,
        )
        top, common, own = map(Path, os.fsdecode(result.stdout).splitlines())
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    if top.resolve() != root:
        return []
    admins = common / "worktrees"
    try:
        linked = list(admins.iterdir()) if not admins.is_symlink() else []
    except OSError:
        linked = []
    reports = (admin / "codex-rig" / "reports" for admin in {common, own, *linked} if not admin.is_symlink())
    return [
        path.resolve() for path in reports if not path.parent.is_symlink() and not path.is_symlink() and path.is_dir()
    ]


def find_ledgers(roots: list[Path]) -> list[Path]:
    """Return every ``gaps.jsonl`` under the roots, sorted, without following symlinked directories.

    Examples:
        >>> find_ledgers([Path("/nonexistent-root")])
        []
    """
    return _find_named(roots, LEDGER_NAME)


def _newest_mtime(run_dir: Path) -> float | None:
    """Return the newest file modification time under a run directory, or ``None`` when it holds no file."""
    newest: float | None = None
    for current, dirs, files in os.walk(run_dir, followlinks=False):
        dirs[:] = [entry for entry in dirs if entry not in SKIP_DIRS]
        for name in files:
            try:
                stamp = (Path(current) / name).lstat().st_mtime
            except OSError:
                continue
            newest = stamp if newest is None else max(newest, stamp)
    return newest


def _later_runs(run_dir: Path) -> list[Path]:
    """Return the sibling ``run-<NNN>`` directories numbered after this one.

    Examples:
        >>> _later_runs(Path("/nonexistent/pr-1/run-002"))
        []
    """
    own = _RUN_DIR.match(run_dir.name)
    if own is None:
        return []
    try:
        siblings = [path for path in run_dir.parent.iterdir() if path.is_dir() and not path.is_symlink()]
    except OSError:
        return []
    return [path for path in siblings if (m := _RUN_DIR.match(path.name)) and int(m.group(1)) > int(own.group(1))]


def run_status(run_dir: Path, superseded: bool, now: float) -> str:
    """Return the status of a run without its terminal artifact: purged, superseded, in-progress or unfinished.

    Examples:
        >>> run_status(Path("/nonexistent-run"), superseded=True, now=0.0)
        'purged'
    """
    newest = _newest_mtime(run_dir)
    if newest is None:
        return RunStatus.PURGED.value
    if superseded:
        return RunStatus.SUPERSEDED.value
    if now - newest < RECENT_SECONDS:
        return RunStatus.IN_PROGRESS.value
    return RunStatus.UNFINISHED.value


def _is_review_allocation(receipt: Path) -> bool:
    """Recognize the Code Review allocation receipt retained through run promotion."""
    if receipt.is_symlink() or not receipt.is_file():
        return False
    try:
        entry = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return False
    return (
        isinstance(entry, dict)
        and entry.get("schema_version") == 1
        and isinstance(entry.get("allocated_as"), str)
        and bool(entry["allocated_as"])
    )


def _codex_runs(roots: list[Path], now: float) -> list[IncompleteRun]:
    """Return Codex runs with an allocation receipt or stub but without a terminal result."""
    runs = []
    directories = {path.parent for path in _find_named(roots, RUN_STATE_NAME, REVIEW_ENTRY_NAME)}
    for run_dir in sorted(directories):
        if (run_dir / RESULT_NAME).exists():
            continue
        stub = run_dir / RUN_STATE_NAME
        if stub.is_file() or stub.is_symlink():
            kind, checkpoint = run_state_key(stub)
        elif _is_review_allocation(run_dir / REVIEW_ENTRY_NAME):
            kind, checkpoint = "review", "allocated"
        else:
            continue
        superseded = any((later / RESULT_NAME).exists() for later in _later_runs(run_dir))
        runs.append(IncompleteRun(run_dir, "codex", kind, checkpoint, run_status(run_dir, superseded, now)))
    return runs


def _is_review_run(path: Path) -> bool:
    """Return whether a directory is a Claude review report directory ``.reports/review/pr-<N>/run-<NNN>``.

    Examples:
        >>> _is_review_run(Path("/r/.reports/review/pr-12/run-003"))
        True
        >>> _is_review_run(Path("/r/.reports/codex/code-review/pr-12/run-003"))
        False
    """
    parents = path.parents
    return (
        len(parents) >= 3
        and bool(_RUN_DIR.match(path.name))
        and bool(_PR_DIR.match(parents[0].name))
        and parents[1].name == "review"
        and parents[2].name == ".reports"
    )


def _is_resolve_run(path: Path) -> bool:
    """Return whether a directory is a Claude resolve run directory ``.reports/resolve/<timestamp>``.

    Examples:
        >>> _is_resolve_run(Path("/r/.reports/resolve/2026-11-25T10-00-00Z"))
        True
    """
    return len(path.parents) >= 2 and path.parents[0].name == "resolve" and path.parents[1].name == ".reports"


def _claude_run_dirs(roots: list[Path]) -> tuple[list[Path], list[Path]]:
    """Return Claude review and resolve run directories under the roots, without following symlinked directories."""
    review: set[Path] = set()
    resolve: set[Path] = set()
    for root in {path.resolve() for path in roots if path.is_dir()}:
        for current, dirs, _files in os.walk(root, followlinks=False):
            here = Path(current)
            is_review = _is_review_run(here)
            if is_review or _is_resolve_run(here):
                (review if is_review else resolve).add(here)
                dirs[:] = []
                continue
            dirs[:] = sorted(entry for entry in dirs if entry not in SKIP_DIRS)
    return sorted(review), sorted(resolve)


def review_checkpoint(run_dir: Path) -> str:
    """Return the furthest stage a Claude review report directory's files show.

    Examples:
        >>> review_checkpoint(Path("/nonexistent-run"))
        '-'
    """
    for stage, names in _REVIEW_STAGES:
        if any((run_dir / name).exists() for name in names):
            return stage
    return "-"


def _review_runs(run_dirs: list[Path], now: float) -> list[IncompleteRun]:
    """Return Claude review runs without ``review-report.md``."""
    runs = []
    for run_dir in run_dirs:
        if (run_dir / REVIEW_REPORT_NAME).exists():
            continue
        superseded = any((later / REVIEW_REPORT_NAME).exists() for later in _later_runs(run_dir))
        status = run_status(run_dir, superseded, now)
        runs.append(IncompleteRun(run_dir, "claude", "review", review_checkpoint(run_dir), status))
    return runs


def _report_head(run_dir: Path) -> str | None:
    """Return the first bytes of a resolve run's report as text, or ``None`` when it is missing or unreadable."""
    path = run_dir / RESOLVE_REPORT_NAME
    if not path.is_file() or path.is_symlink():
        return None
    try:
        with path.open("rb") as handle:
            return handle.read(_REPORT_HEAD_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return None


def resolve_checkpoint(head: str | None) -> str:
    """Return a resolve stub's ``Step N`` checkpoint, ``no report`` without a report file.

    Examples:
        >>> resolve_checkpoint("<!-- x -->\\n\\n**Last step reached**: Step 10 — push step reached\\n")
        'Step 10'
        >>> resolve_checkpoint(None)
        'no report'
    """
    if head is None:
        return "no report"
    step = _STUB_STEP.search(head)
    if step is None:
        return "-"
    token = _STEP_TOKEN.match(step.group("step"))
    return token.group(0) if token else step.group("step")[:60]


def _report_pr(head: str | None) -> str | None:
    """Return the PR number a resolve report or stub heading names, or ``None`` for a local run."""
    match = _REPORT_PR.search(head or "")
    return match.group("pr") if match else None


def _resolve_runs(run_dirs: list[Path], now: float) -> list[IncompleteRun]:
    """Return Claude resolve runs whose report is still the stub or is missing."""
    runs = []
    latest_final = cache(_latest_resolve_reports)
    for run_dir in run_dirs:
        head = _report_head(run_dir)
        if head is not None and head.splitlines()[:1] != [RESOLVE_STUB_MARKER]:
            continue
        pr = _report_pr(head)
        superseded = pr is not None and latest_final(run_dir.parent).get(pr, "") > run_dir.name
        status = run_status(run_dir, superseded, now)
        runs.append(IncompleteRun(run_dir, "claude", "resolve", resolve_checkpoint(head), status))
    return runs


def _latest_resolve_reports(directory: Path) -> dict[str, str]:
    """Read the latest completed resolve report directory for each PR among siblings on disk."""
    latest = {}
    try:
        siblings = sorted(directory.iterdir())
    except OSError:
        return latest
    for sibling in siblings:
        if sibling.is_symlink() or not sibling.is_dir():
            continue
        head = _report_head(sibling)
        if head is not None and head.splitlines()[:1] != [RESOLVE_STUB_MARKER] and (pr := _report_pr(head)) is not None:
            latest[pr] = sibling.name
    return latest


def find_incomplete_runs(roots: list[Path], now: float | None = None) -> list[IncompleteRun]:
    """Return Codex and Claude runs under the roots that have no terminal artifact.

    Args:
        roots: Files or directories searched recursively.
        now: Clock for the in-progress window, in seconds since the epoch; defaults to the current time.

    Examples:
        >>> find_incomplete_runs([Path("/nonexistent-root")])
        []
    """
    clock = time.time() if now is None else now
    review, resolve = _claude_run_dirs(roots)
    return _codex_runs(roots, clock) + _review_runs(review, clock) + _resolve_runs(resolve, clock)


def run_state_key(path: Path) -> tuple[str, str]:
    """Return ``(run kind, last checkpoint)`` of an incomplete run's stub; an unreadable stub is ``unreadable``.

    Examples:
        >>> run_state_key(Path("/nonexistent/run-state.json"))
        ('unreadable', '-')
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return "unreadable", "-"
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        return "unreadable", "-"
    kind = next((label for key, label in RUN_STATE_STATUS_KEYS.items() if key in data), "unknown")
    return kind, str(data.get("checkpoint") or "-")


def subject_key(subject: dict[str, Any]) -> str:
    """Return the subject part of a gap identity: the pull request when there is one, else the head commit.

    Examples:
        >>> subject_key({"repo": "o/r", "pr": 12, "head": "abcdef1"})
        'pr-12'
        >>> subject_key({"repo": "o/r", "pr": None, "head": "abcdef1"})
        'head-abcdef1'
    """
    return f"pr-{subject['pr']}" if subject.get("pr") else f"head-{subject['head']}"


def root_limit_files(roots: list[Path]) -> list[Path]:
    """Return the ``.notes/known-limits.json`` file directly under each directory root, when present.

    Examples:
        >>> root_limit_files([Path("/nonexistent-root")])
        []
    """
    return sorted({root / ROOT_LIMITS for root in roots if root.is_dir() and (root / ROOT_LIMITS).is_file()})


def _limits_by_repo(files: list[Path], skipped: list[tuple[str, str]]) -> dict[str, list[gap_ledger.KnownLimit]]:
    """Load known-limits files keyed by case-folded repository, naming any file that cannot be used."""
    limits: dict[str, list[gap_ledger.KnownLimit]] = {}
    for path in files:
        try:
            repo, entries = gap_ledger.read_known_limits(path)
        except (gap_ledger.LedgerError, gap_ledger.LedgerInvalidError) as error:
            skipped.append((str(path), str(error)))
            continue
        limits.setdefault(gap_ledger.repo_key(repo) or repo, []).extend(entries)
    return limits


def _host_key(repo: str, record: dict[str, Any]) -> tuple[str, str, str, str]:
    """Return the repository, gap id and observing host class of a hardware record."""
    host = record["host_class"]
    return repo, record["id"], host["os"], host["architecture"]


def _add_ledger(rollup: Rollup, ledger: gap_ledger.Ledger, limits: dict[str, list[gap_ledger.KnownLimit]]) -> None:
    """Fold one valid ledger into the rollup, last record per gap id.

    Only a registry match makes a record a known limit. A record flagged ``known_limit`` without one is counted as an
    ordinary gap and also named as an unverified claim.
    """
    rollup.ledgers += 1
    rollup.records += len(ledger.records)
    run = ledger.records[0]["run"]
    run_key = str(ledger.path)
    route = (run["harness"], run["plugin"], run["plugin_version"], run["route"])
    rollup.routes[route] += 1
    for record in gap_ledger.latest_by_id(ledger.records).values():
        if record.get("status") == gap_ledger.Status.CLOSED.value:
            rollup.closed += 1
            continue
        repo = gap_ledger.repo_key(record["subject"]["repo"]) or record["subject"]["repo"]
        listed = gap_ledger.matching_limit(record, limits.get(repo, ()))
        if listed is not None:
            seen = rollup.known.setdefault(
                _host_key(repo, record), KnownLimitSeen(record.get("hardware_class", ""), listed.reason)
            )
            seen.runs.add(run_key)
            continue
        if record.get("known_limit") is True:
            rollup.unverified.setdefault(_host_key(repo, record), set()).add(run_key)
        identity = (repo, subject_key(record["subject"]), record["id"])
        gap = rollup.gaps.setdefault(identity, GapIdentity())
        gap.classes[record["class"]] += 1
        gap.runs.add(run_key)
        gap.carried += 1 if record.get("carried_from") else 0
        rollup.route_gaps.setdefault(route, set()).add(identity)


def build_rollup(
    ledger_paths: list[Path], limit_files: list[Path], incomplete: list[IncompleteRun] | None = None
) -> Rollup:
    """Validate and aggregate ledgers; invalid ledgers are named in ``skipped`` and left out of every count.

    Args:
        ledger_paths: ``gaps.jsonl`` files to aggregate.
        limit_files: Per-repository known-limits files.
        incomplete: Runs without a terminal artifact, counted by harness, kind, checkpoint and status.

    Examples:
        >>> build_rollup([], []).ledgers
        0
    """
    rollup = Rollup()
    rollup.incomplete.update((run.harness, run.kind, run.checkpoint, run.status) for run in incomplete or [])
    limits = _limits_by_repo(limit_files, rollup.skipped)
    for path in ledger_paths:
        try:
            ledger = gap_ledger.load_ledger(path)
        except gap_ledger.LedgerError as error:
            rollup.skipped.append((str(path), str(error)))
            continue
        if ledger.errors:
            rollup.skipped.append((str(path), f"{ledger.errors[0]} ({len(ledger.errors)} error(s))"))
            continue
        if ledger.records:
            _add_ledger(rollup, ledger, limits)
    return rollup


def dominant_class(gap: GapIdentity) -> str:
    """Return the class most runs recorded for a gap identity, ties broken by class name.

    Examples:
        >>> dominant_class(GapIdentity(classes=Counter({"process": 1, "project": 2})))
        'project'
    """
    return sorted(gap.classes.items(), key=lambda item: (-item[1], item[0]))[0][0]


def class_rows(rollup: Rollup) -> list[dict[str, Any]]:
    """Return one headline row per class: distinct gaps, subjects, recording runs, carried entries and share.

    Examples:
        >>> [row["class"] for row in class_rows(Rollup())][:2]
        ['process', 'environment-provisionable']
    """
    total = len(rollup.gaps)
    rows = []
    for name, (meaning, action) in CLASS_ROWS.items():
        members = {key: gap for key, gap in rollup.gaps.items() if dominant_class(gap) == name}
        rows.append(
            {
                "class": name,
                "meaning": meaning,
                "action": action,
                "distinct_gaps": len(members),
                "subjects": len({(repo, subject) for repo, subject, _ in members}),
                "runs": len({run for gap in members.values() for run in gap.runs}),
                "carried": sum(gap.carried for gap in members.values()),
                "share": (len(members) / total) if total else 0.0,
            }
        )
    return rows


def to_json(rollup: Rollup) -> dict[str, Any]:
    """Return the rollup as a JSON-serializable document."""
    return {
        "ledgers": rollup.ledgers,
        "records": rollup.records,
        "closed_in_run": rollup.closed,
        "distinct_gaps": len(rollup.gaps),
        "classes": class_rows(rollup),
        "class_conflicts": [
            {"repo": repo, "subject": subject, "id": gap_id, "classes": dict(gap.classes)}
            for (repo, subject, gap_id), gap in sorted(rollup.gaps.items())
            if len(gap.classes) > 1
        ],
        "known_limits": [
            {
                "repo": repo,
                "id": gap_id,
                "hardware_class": seen.hardware_class,
                "host_class": {"os": system, "architecture": architecture},
                "reason": seen.reason,
                "runs": len(seen.runs),
            }
            for (repo, gap_id, system, architecture), seen in sorted(rollup.known.items())
        ],
        "unverified_known_limits": [
            {
                "repo": repo,
                "id": gap_id,
                "host_class": {"os": system, "architecture": architecture},
                "runs": len(runs),
            }
            for (repo, gap_id, system, architecture), runs in sorted(rollup.unverified.items())
        ],
        "routes": [
            {
                "harness": harness,
                "plugin": plugin,
                "plugin_version": version,
                "route": route,
                "runs": runs,
                "distinct_gaps": len(rollup.route_gaps.get((harness, plugin, version, route), set())),
            }
            for (harness, plugin, version, route), runs in sorted(rollup.routes.items())
        ],
        "incomplete_runs": [
            {"harness": harness, "kind": kind, "checkpoint": checkpoint, "status": status, "runs": runs}
            for (harness, kind, checkpoint, status), runs in sorted(rollup.incomplete.items())
        ],
        "incomplete_by_status": {
            status.value: sum(runs for key, runs in rollup.incomplete.items() if key[3] == status.value)
            for status in RunStatus
        },
        "skipped": [{"path": path, "error": error} for path, error in rollup.skipped],
    }


def _cell(text: object) -> str:
    """Escape a Markdown table cell."""
    return str(text).replace("|", "\\|").replace("\n", " ")


def to_markdown(rollup: Rollup) -> str:
    """Render the headline table, known limits, route and version identity, conflicts and skipped ledgers.

    Examples:
        >>> to_markdown(Rollup()).splitlines()[0]
        '# Gap rollup'
    """
    data = to_json(rollup)
    lines = [
        "# Gap rollup",
        "",
        f"Ledgers read: {data['ledgers']} · records: {data['records']} · distinct gaps: {data['distinct_gaps']}"
        f" · closed in-run: {data['closed_in_run']} · skipped files: {len(data['skipped'])}",
        "",
        "Counted by repository, subject (pull request, else head) and gap id, never by run directory.",
        "",
        "| Class | What it is | Distinct gaps | Subjects | Runs recording it | Carried | Share | Action |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ]
    lines += [
        f"| {row['class']} | {_cell(row['meaning'])} | {row['distinct_gaps']} | {row['subjects']} | {row['runs']} "
        f"| {row['carried']} | {row['share']:.0%} | {_cell(row['action'])} |"
        for row in data["classes"]
    ]
    lines += ["", "## Known limits by repository and host class", ""]
    lines += ["| Repository | Limit | Hardware class | Host class | Reason | Runs |", "|---|---|---|---|---|---:|"]
    lines += [
        f"| {item['repo']} | {item['id']} | {item['hardware_class']} | "
        f"{item['host_class']['os']} / {item['host_class']['architecture']} | {_cell(item['reason'])} | {item['runs']} |"
        for item in data["known_limits"]
    ] or ["| — | — | — | — | — | 0 |"]
    if data["unverified_known_limits"]:
        lines += ["", "## Unverified known-limit claims (counted as gaps)", ""]
        lines += [
            "Flagged `known_limit` with no matching entry in a given known-limits file, so each stays an ordinary gap.",
            "",
            "| Repository | Limit | Host class | Runs |",
            "|---|---|---|---:|",
        ]
        lines += [
            f"| {item['repo']} | {item['id']} | {item['host_class']['os']} / {item['host_class']['architecture']} "
            f"| {item['runs']} |"
            for item in data["unverified_known_limits"]
        ]
    lines += ["", "## Runs by harness, plugin version and route", ""]
    lines += ["| Harness | Plugin | Version | Route | Runs | Distinct gaps |", "|---|---|---|---|---:|---:|"]
    lines += [
        f"| {item['harness']} | {item['plugin']} | {item['plugin_version']} | {item['route']} | {item['runs']} "
        f"| {item['distinct_gaps']} |"
        for item in data["routes"]
    ] or ["| — | — | — | — | 0 | 0 |"]
    if data["class_conflicts"]:
        lines += ["", "## Class conflicts (counted under the class most runs recorded)", ""]
        lines += [
            f"- {item['repo']} {item['subject']} `{item['id']}`: {json.dumps(item['classes'], sort_keys=True)}"
            for item in data["class_conflicts"]
        ]
    if data["incomplete_runs"]:
        by_status = " · ".join(f"{status}: {runs}" for status, runs in data["incomplete_by_status"].items())
        lines += ["", "## Runs without a terminal artifact", ""]
        lines += [
            f"{by_status}. Only `unfinished` runs are lost work: `superseded` runs were rerun to a finished later run "
            "of the same PR, `purged` directories hold no file, `in-progress` runs wrote within 24 hours.",
            "",
        ]
        lines += ["| Harness | Run kind | Last checkpoint | Status | Runs |", "|---|---|---|---|---:|"]
        lines += [
            f"| {item['harness']} | {item['kind']} | {_cell(item['checkpoint'])} | {item['status']} | {item['runs']} |"
            for item in data["incomplete_runs"]
        ]
    if data["skipped"]:
        lines += ["", "## Skipped files (not counted)", ""]
        lines += [f"- `{item['path']}`: {_cell(item['error'])}" for item in data["skipped"]]
    return "\n".join(lines) + "\n"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Aggregate run gap ledgers across runs.")
    parser.add_argument("roots", nargs="+", type=Path, help="gaps.jsonl files or directories to search")
    parser.add_argument("--known-limits", action="append", type=Path, default=[], help="known-limits.json file")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--output", type=Path, default=None, help="sidecar report path (never a ledger)")
    return parser


def _is_sidecar_output(path: Path) -> bool:
    """Admit an output only when its resolved name and link count rule out a ledger alias."""
    if path.is_symlink():
        return False
    try:
        name = path.resolve().name.casefold()
        if name == LEDGER_NAME or name.endswith(".rec"):
            return False
        return path.lstat().st_nlink == 1
    except FileNotFoundError:
        return True
    except (OSError, RuntimeError):
        return False


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point; see the module docstring for exit codes (argparse raises its own exit 2)."""
    args = _build_parser().parse_args(argv)
    if args.output is not None and not _is_sidecar_output(args.output):
        print(f"refusing a linked or ledger output path: {args.output}", file=sys.stderr)
        return 2
    limit_files = list(dict.fromkeys([*args.known_limits, *root_limit_files(args.roots)]))
    rollup = build_rollup(find_ledgers(args.roots), limit_files, find_incomplete_runs(args.roots))
    text = json.dumps(to_json(rollup), indent=2) + "\n" if args.format == "json" else to_markdown(rollup)
    if args.output is None:
        sys.stdout.write(text)
    else:
        try:
            write_report(args.output, text)
        except OSError as error:
            print(f"cannot write rollup: {error}", file=sys.stderr)
            return 2
        print(f"wrote {args.output}")
    for path, error in rollup.skipped:
        print(f"skipped {path}: {error}", file=sys.stderr)
    return 0 if rollup.ledgers or rollup.incomplete else 1


if __name__ == "__main__":
    sys.exit(main())
