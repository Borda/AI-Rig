#!/usr/bin/env python3
"""Probe optional codemap-py structural context through its public CLI/JSON surface.

## Purpose

Add one bounded structural-context observation to a Codex Rig workflow without coupling to codemap-py internals or
source paths. The adapter records both interpreter health and category-specific query completeness so downstream
decisions can distinguish healthy context from degraded context.

## Scope

It calls only ``codemap-py doctor --json`` and public query commands; absence or incompatibility is non-fatal and
callers retain local inspection. ``CODEMAP_BIN`` is accepted only when it names an absolute, non-symlink executable, and
``--provider-root`` accepts one caller-selected active install root when ``CODEMAP_BIN`` is unset. It never searches
installation caches. Every subprocess is bounded by the requested timeout.

## Usage

Run ``python codemap_adapter.py probe`` or use the adapter's ``context`` action with ``--category`` and persist the
result once per workflow. The context form accepts an optional dotted target, ``--query-kind`` (skip, one compact fact,
or standard), repository root, timeout, ``--provider-root``, ``--diff-file`` for the review batch's change set, and
``--out`` path; it always prints the same JSON payload that it writes. The probe form also accepts ``--provider-root``.
The review batch never runs ``diff-impact`` without a diff file, because diffing a detached review worktree against its
own ``HEAD`` reports zero changed files as a complete answer.

## Used by

The ``assess``, ``implement``, ``audit``, and ``code-review`` skills consume these observations; see the adjacent
``codemap-contract.md`` for the category/query mapping. The module is also exercised by portable helper tests that
verify status reduction and the public CLI contract.

## Outputs

It returns or writes one versioned JSON probe/context payload whose status makes an unavailable optional integration
explicit. Context payloads contain ``protocol_version``, ``artifact_schema_version``, category, query kind, target,
probe details, and one outcome record per mapped query, while ``probe`` emits only the probe record. A ``skip`` context
records status ``skipped`` without resolving or running Codemap.

Each query outcome also records the index path the provider reported for that query, and the payload lists every query
whose path disagreed with the probe's resolver-derived one under ``index_path_divergence``. That disagreement is
reported as evidence and never reconciled or folded into the status; a provider that reports no path yields ``null`` and
no divergence claim. Each query also keeps the provider's answer itself (``answer``, every list and string bounded, with
cut lengths in ``answer_truncated``) and its completeness detail (``completeness_reason``, ``root_mismatch``, ``note``,
and a change set's ``changed_files``). ``status_reasons`` names every gap behind a ``degraded``, ``stale``, or
post-query ``incompatible`` status, including a change set whose changed Python files the index mapped to no module and
a diff whose deleted Python modules the provider never reads. A provider failure keeps the provider's own error text,
and its other structured error fields (such as an ambiguous target's ``candidates``) as the bounded ``answer`` — an
empty object when it printed only its error text, ``null`` when it printed no JSON object at all. Input the provider
itself diagnoses and rejects — exit 2 with its JSON ``error`` object, or a target it reports it could not resolve —
degrades the context instead of marking the provider incompatible; a bare exit 2, such as an argument-parser usage
error, is a provider-side failure. That ``input_rejected`` flag is derived from the recorded exit code and ``answer``
alone, so an artifact reader re-derives it instead of trusting it. Git C-quoted paths in a diff or file listing are
unquoted before use.

## Failure

Launcher absence, unsupported output, timeout, or malformed JSON is classified in the probe/context status rather than
blocking the primary workflow. An unknown category still raises ``ValueError`` because it is invalid caller input,
whereas a failed optional query is represented in the JSON outcome and the CLI exits successfully.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field, fields
from itertools import pairwise
from pathlib import Path
from typing import Any

#: Identifier of the Codemap integration protocol this adapter speaks, recorded in every context artifact.
PROTOCOL_VERSION = "codemap-py.integration.v1"
#: Schema revision of the structural-context artifact this adapter writes. Revision 4 added the diff-file input,
#: per-query provider completeness fields, the provider-diagnosed input-rejection flag, and top-level status reasons;
#: revision 3 artifacts remain readable history.
ARTIFACT_SCHEMA_VERSION = 4
#: Status reported when the Codemap launcher is healthy and its queries returned complete results.
STATUS_AVAILABLE = "available"
#: Status reported when no `codemap-py` launcher could be found.
STATUS_ABSENT = "absent"
#: Status reported when query results describe an older tree than the one on disk.
STATUS_STALE = "stale"
#: Status reported when the Codemap launcher, its health check, or its query output cannot be used.
STATUS_INCOMPATIBLE = "incompatible"
#: Status reported when Codemap ran but one or more queries failed or returned incomplete results.
STATUS_DEGRADED = "degraded"
# One composed status, not a seventh independent one: `stale` and `degraded` are the only two
# conditions that can hold at once (`absent` and `skipped` short-circuit before any query runs,
# `incompatible` either does too or means no query succeeded, and `stale` is only ever read off a
# query that parsed successfully). Consumers match this vocabulary
# by exact value, so ranking one condition above the other would silently drop the other's caveat:
# reporting `stale` alone invites the false conclusion that re-indexing restores exhaustiveness,
# and reporting `degraded` alone hides that the evidence describes an older tree.
#: Composed status for results that are both stale and degraded, so neither caveat is hidden.
STATUS_STALE_DEGRADED = "stale+degraded"
#: Status reported when the caller asked for no Codemap queries, so no subprocess was started.
STATUS_SKIPPED = "skipped"
#: Closed status vocabulary a persisted context artifact may carry; artifact validators check against it.
STATUSES = (
    STATUS_AVAILABLE,
    STATUS_ABSENT,
    STATUS_STALE,
    STATUS_INCOMPATIBLE,
    STATUS_DEGRADED,
    STATUS_STALE_DEGRADED,
    STATUS_SKIPPED,
)
#: Primary-artifact statuses after which a specialist follow-up may still run. `absent` and `incompatible` already
#: proved the provider unusable for this run, and `skipped` recorded a deliberate zero-query decision.
FOLLOW_UP_ELIGIBLE_STATUSES = (STATUS_AVAILABLE, STATUS_STALE, STATUS_DEGRADED, STATUS_STALE_DEGRADED)
#: Fact routes a specialist may request as a targeted follow-up after the workflow's persisted probe.
FOLLOW_UP_QUERY_KINDS = ("callers", "dependencies", "test-impact")
#: Most targeted follow-up queries one specialist may run in one workflow run.
FOLLOW_UP_QUERY_LIMIT = 3
#: Default per-subprocess timeout, in seconds, for Codemap probe and query calls.
_DEFAULT_TIMEOUT = 15.0
#: Error text recorded for a query that needs a target when the caller supplied none.
_MISSING_TARGET_ERROR = "target required, none supplied"
#: Error text recorded for a supplied target no route accepts: an empty side around `::`, or more than one `::`.
_MALFORMED_TARGET_ERROR = "target malformed: expected a module, a module::symbol qname, or a dotted or bare symbol name"
#: Error text recorded for a change-set query that needs a diff file when the caller supplied none.
_MISSING_DIFF_FILE_ERROR = "diff file required, none supplied"
#: Caller-input errors: the provider was never asked, so they degrade the context rather than prove it unusable.
_CALLER_INPUT_ERRORS = (_MISSING_TARGET_ERROR, _MALFORMED_TARGET_ERROR, _MISSING_DIFF_FILE_ERROR)
#: Exit code Codemap uses for caller input it rejects, such as an unreadable diff file. Python's argument parser exits
#: with the same code for a usage error, so only an exit that also carries the provider's JSON `error` object proves
#: the provider works and rejected the input.
_EXIT_BAD_INPUT = 2
#: Exit code Codemap uses for a module absent from the index, and for an index it could not load at all.
_EXIT_NOT_INDEXED = 3
#: Key Codemap adds to every target-resolution failure (missing, ambiguous, or unindexed target). Those failures share
#: exit 1 or 3 with provider-side ones, such as an invalid index or a disabled feature, so only this key — or, from a
#: provider predating it, a not-indexed exit naming the `module` — shows the provider works and refused the target.
_REJECTED_TARGET_KEY = "rejected_target"
#: Provider error fields already kept as the failure reason; every other field of a failure payload is kept as `answer`.
_FAILURE_TEXT_KEYS = frozenset({"error", "detail"})
#: Most items an answer keeps from a list not nested in another list; `answer_truncated` records the original length.
ANSWER_LIST_LIMIT = 20
#: Most items a persisted answer keeps from a list nested inside another list's items.
_ANSWER_NESTED_LIST_LIMIT = 5
#: Longest string a persisted answer keeps; a longer one is cut and its original length recorded.
_ANSWER_TEXT_LIMIT = 300
#: Provider fields never copied into an answer: completeness metadata is reduced separately. A top-level `hint` stays:
#: on a zero-caller method it is the only sign that `called_by: []` means "unresolved statically", not "unused".
_ANSWER_EXCLUDED_KEYS = frozenset({"index"})
#: Most paths one change-set gap reason names; the count in the same reason always covers every path.
_GAP_PATH_LIMIT = 5
#: Change-set gap recorded when the adapter cannot read back the diff file the provider just read.
_UNREADABLE_DIFF_GAP = "diff file unreadable by the adapter: deleted Python modules unchecked"
#: Unified-diff target that marks a whole-file deletion.
_DEV_NULL = "/dev/null"
#: One escape inside a git C-quoted path: three octal digits for a raw byte, or one of git's single-character escapes.
_GIT_QUOTED_ESCAPE = re.compile(r'\\([0-7]{3}|[abtnvfr"\\])')
#: Byte each single-character escape in a git C-quoted path stands for.
_GIT_QUOTE_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}
#: Lowercase file suffixes treated as directly executable launchers on Windows.
_WINDOWS_EXECUTABLE_SUFFIXES = {".bat", ".cmd", ".com", ".exe"}


@dataclass(frozen=True)
class QuerySpec:
    """One planned `codemap-py query` call for a structural-context category."""

    subcommand: str
    requires_target: bool
    extra_args: tuple[str, ...] = ()
    #: Change-set queries read the caller's unified diff instead of diffing the working tree against `HEAD`, which in a
    #: detached review worktree at the reviewed head compares the tree with itself and sees no change.
    requires_diff_file: bool = False


#: Codemap queries planned for each structural-context category (analysis, implementation, review, audit).
CATEGORY_QUERIES: dict[str, tuple[QuerySpec, ...]] = {
    # analysis/research: centrality, symbol/import context, completeness metadata.
    "analysis": (
        QuerySpec("central", requires_target=False),
        QuerySpec("deps", requires_target=True),
    ),
    # implement/investigate/optimize: callers, coupling, test impact before implementation.
    "implementation": (
        QuerySpec("rdeps", requires_target=True),
        QuerySpec("coupled", requires_target=False),
        QuerySpec("test-impact", requires_target=True),
    ),
    # code-review/code-remediate: changed-symbol/diff impact of the caller's diff file, supplied once.
    "review": (QuerySpec("diff-impact", requires_target=False, requires_diff_file=True),),
    # audit/release: undocumented public surface + externally-uncalled modules (broken-ref proxy).
    "audit": (
        QuerySpec("undocumented", requires_target=False, extra_args=("--all",)),
        QuerySpec("dead-modules", requires_target=False),
    ),
}


#: Accepted query kinds for a fact request; `skip` runs nothing and `standard` uses the category plan.
QUERY_KINDS = (
    "skip",
    "central",
    "callers",
    "blast",
    "dependencies",
    "test-impact",
    "coupling",
    "standard",
)

#: Single Codemap query planned for each non-standard query kind.
_FACT_QUERY_SPECS: dict[str, QuerySpec] = {
    "central": QuerySpec("central", requires_target=False, extra_args=("--top", "5")),
    "callers": QuerySpec("fn-rdeps", requires_target=True, extra_args=("--exclude-tests",)),
    "blast": QuerySpec("fn-blast", requires_target=True),
    "dependencies": QuerySpec("rdeps", requires_target=True),
    "test-impact": QuerySpec("test-impact", requires_target=True),
    "coupling": QuerySpec("coupled", requires_target=False),
}
#: Provider subcommand each specialist follow-up route runs, so a reader can check a follow-up ran what it claims.
FOLLOW_UP_SUBCOMMANDS = {kind: _FACT_QUERY_SPECS[kind].subcommand for kind in FOLLOW_UP_QUERY_KINDS}


@dataclass(frozen=True)
class DoctorReport:
    """Parsed ``codemap-py doctor --json`` payload."""

    python: str
    version: str
    implementation: str
    supported: bool
    plugin_root: str
    index_path: str


@dataclass(frozen=True)
class LauncherResolution:
    """One validated Codemap launcher decision reused for one adapter invocation."""

    launcher: str | None
    status: str
    detail: str


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of probing `codemap-py` availability and interpreter health."""

    status: str
    detail: str
    launcher: str | None
    doctor: DoctorReport | None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable view."""
        return {
            "status": self.status,
            "detail": self.detail,
            "launcher": self.launcher,
            "doctor": None if self.doctor is None else vars(self.doctor),
        }


@dataclass(frozen=True)
class QueryOutcome:
    """Result of one `codemap-py query` call: its bounded answer plus completeness metadata."""

    subcommand: str
    target: str | None
    exit_code: int
    stale: bool
    query_complete: bool
    not_covered: tuple[str, ...]
    degraded_count: int
    error: str | None
    #: Index file this one query reported for itself: the path the provider actually opened
    #: (`index.index_path`), or — on a not-indexed exit raised by a failed load — the path it
    #: addressed and could not open. `None` when the provider never reported one, which is the
    #: normal shape for a provider predating the field and for a not-indexed exit raised after a
    #: successful load. Never inferred from the launcher, the root, or the probe.
    index_path: str | None = None
    #: Provider's own explanation for an incomplete answer (`index.completeness_reason`), kept verbatim.
    completeness_reason: str | None = None
    #: Whether the provider reported that the index was built for a different root than the one queried.
    root_mismatch: bool = False
    #: Provider's human-readable caveat for this answer (`index.note`), kept verbatim.
    note: str | None = None
    #: Number of changed Python files a change-set query read from its diff, mapped or not, when reported.
    changed_files: int | None = None
    #: Gap the adapter itself detected that the provider's completeness flags did not report.
    adapter_gap: str | None = None
    #: The provider's answer itself, minus completeness metadata, with every list and string bounded. On a failed query,
    #: the provider's structured error fields besides its `error`/`detail` text — such as `candidates`, `suggestions`,
    #: or `rejected_target` — so a rejected target keeps the alternatives the provider offered. A failure whose JSON
    #: object held only that text keeps `{}`; `None` means the provider printed no JSON object at all.
    answer: dict[str, Any] | None = None
    #: Original length of every answer list or string that was cut, keyed by its path inside `answer`.
    answer_truncated: dict[str, int] = field(default_factory=dict)
    #: Whether the provider itself diagnosed this failure as rejected caller input: exit 2 carrying its JSON `error`
    #: object, or a target it could not resolve (see `_REJECTED_TARGET_KEY`). Always
    #: `provider_rejected_input(exit_code, answer)`, so a reader re-derives it rather than trusting it, and still tells
    #: it from an argument-parser usage exit (also code 2) or a provider-side exit 1 or 3.
    input_rejected: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable view."""
        return {
            "subcommand": self.subcommand,
            "target": self.target,
            "exit_code": self.exit_code,
            "stale": self.stale,
            "query_complete": self.query_complete,
            "not_covered": list(self.not_covered),
            "degraded_count": self.degraded_count,
            "error": self.error,
            "index_path": self.index_path,
            "completeness_reason": self.completeness_reason,
            "root_mismatch": self.root_mismatch,
            "note": self.note,
            "changed_files": self.changed_files,
            "adapter_gap": self.adapter_gap,
            "answer": self.answer,
            "answer_truncated": dict(self.answer_truncated),
            "input_rejected": self.input_rejected,
        }

    @classmethod
    def from_dict(cls, record: Any) -> QueryOutcome:
        """Rebuild one persisted query record so a reader can re-derive the status it implies.

        Raises:
            ValueError: The record is not a mapping, misses a field, or carries a field of the wrong type.

        Examples:
            >>> record = QueryOutcome("central", None, 0, False, True, (), 0, None).to_dict()
            >>> QueryOutcome.from_dict(record).query_complete
            True
        """
        names = [item.name for item in fields(cls)]
        if not isinstance(record, dict) or set(record) != set(names):
            raise ValueError("query record fields mismatch")
        typed = {
            "subcommand": str,
            "exit_code": int,
            "stale": bool,
            "query_complete": bool,
            "not_covered": list,
            "degraded_count": int,
            "root_mismatch": bool,
            "answer_truncated": dict,
            "input_rejected": bool,
        }
        optional_text = ("target", "error", "index_path", "completeness_reason", "note", "adapter_gap")
        if (
            any(type(record[name]) is not kind for name, kind in typed.items())
            or any(record[name] is not None and not isinstance(record[name], str) for name in optional_text)
            or not all(isinstance(item, str) for item in record["not_covered"])
            or (record["changed_files"] is not None and type(record["changed_files"]) is not int)
            or (record["answer"] is not None and not isinstance(record["answer"], dict))
        ):
            raise ValueError("query record field type mismatch")
        return cls(**{**record, "not_covered": tuple(record["not_covered"])})


@dataclass(frozen=True)
class IndexPathDivergence:
    """One query whose reported index path disagreed with the probe's resolver-derived path."""

    subcommand: str
    doctor_index_path: str
    query_index_path: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable view."""
        return {
            "subcommand": self.subcommand,
            "doctor_index_path": self.doctor_index_path,
            "query_index_path": self.query_index_path,
        }


@dataclass(frozen=True)
class StructuralContext:
    """Persist-once structural-context evidence for one workflow decision point and route."""

    protocol_version: str
    artifact_schema_version: int
    category: str
    query_kind: str
    target: str | None
    status: str
    probe: ProbeResult
    queries: tuple[QueryOutcome, ...] = field(default_factory=tuple)
    index_path_divergence: tuple[IndexPathDivergence, ...] = field(default_factory=tuple)
    #: Unified diff a change-set query read, exactly as the caller passed it; `None` when none was supplied.
    diff_file: str | None = None
    #: One `<subcommand>: <cause>` line per query gap, so a `degraded` or `stale` status always names its cause.
    status_reasons: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON shape written once to a run artifact."""
        return {
            "protocol_version": self.protocol_version,
            "artifact_schema_version": self.artifact_schema_version,
            "category": self.category,
            "query_kind": self.query_kind,
            "target": self.target,
            "diff_file": self.diff_file,
            "status": self.status,
            "status_reasons": list(self.status_reasons),
            "probe": self.probe.to_dict(),
            "queries": [outcome.to_dict() for outcome in self.queries],
            "index_path_divergence": [record.to_dict() for record in self.index_path_divergence],
        }


def _run_json(
    argv: list[str], timeout: float, cwd: Path | None = None
) -> tuple[int, dict[str, Any] | None, str | None]:
    """Run one subprocess and parse stdout as JSON; never raise on failure.

    Args:
        argv: Command and arguments, run without a shell.
        timeout: Seconds before the subprocess is abandoned.
        cwd: Working directory; Codemap discovers its index from here, not from ``--root``.

    Returns:
        ``(exit_code, parsed_json_or_none, error_message_or_none)``.
    """
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False, cwd=cwd)  # noqa: S603 - argv list, no shell
    except (OSError, subprocess.SubprocessError) as error:
        return -1, None, str(error)
    try:
        return completed.returncode, json.loads(completed.stdout), None
    except json.JSONDecodeError:
        detail = completed.stderr.strip() or completed.stdout.strip() or "non-JSON output"
        return completed.returncode, None, detail


def _configured_launcher_is_valid(candidate: Path) -> bool:
    """Return whether one explicit launcher meets the current platform's executable contract."""
    if not candidate.is_absolute() or candidate.is_symlink() or not candidate.is_file():
        return False
    if os.name == "nt":
        return candidate.suffix.casefold() in _WINDOWS_EXECUTABLE_SUFFIXES
    return os.access(candidate, os.X_OK)


def _resolve_codemap_executable(provider_root: Path | None = None) -> LauncherResolution:
    """Resolve a caller-selected provider or launcher without searching install caches."""
    configured = os.environ.get("CODEMAP_BIN")
    if configured:
        candidate = Path(configured)
        if _configured_launcher_is_valid(candidate):
            return LauncherResolution(str(candidate), STATUS_AVAILABLE, "configured launcher")
        return LauncherResolution(
            None,
            STATUS_INCOMPATIBLE,
            "CODEMAP_BIN must be an absolute, non-symlink executable file",
        )
    if provider_root is not None:
        if not provider_root.is_absolute() or provider_root.is_symlink() or not provider_root.is_dir():
            return LauncherResolution(
                None, STATUS_INCOMPATIBLE, "provider root must be an absolute non-symlink directory"
            )
        launcher = provider_root / "bin" / ("codemap-py.cmd" if os.name == "nt" else "codemap-py")
        if not _configured_launcher_is_valid(launcher):
            return LauncherResolution(None, STATUS_INCOMPATIBLE, "provider root has no executable codemap-py launcher")
        return LauncherResolution(str(launcher), STATUS_AVAILABLE, "caller-selected provider root")
    executable = shutil.which("codemap-py")
    if executable is None:
        return LauncherResolution(None, STATUS_ABSENT, "codemap-py not found on PATH")
    # A relative PATH entry yields a relative launcher, which would stop resolving once queries run from `--root`.
    return LauncherResolution(str(Path(executable).absolute()), STATUS_AVAILABLE, "PATH launcher")


def _probe_codemap(resolution: LauncherResolution, timeout: float, cwd: Path | None = None) -> ProbeResult:
    """Probe one resolved launcher through ``doctor --json``; never re-resolve it."""
    if resolution.launcher is None:
        return ProbeResult(resolution.status, resolution.detail, None, None)
    exit_code, payload, error = _run_json([resolution.launcher, "doctor", "--json"], timeout, cwd)
    if exit_code != 0 or payload is None:
        return ProbeResult(
            status=STATUS_INCOMPATIBLE,
            detail=error or f"doctor exited {exit_code}",
            launcher=None,
            doctor=None,
        )
    if not isinstance(payload, dict):
        return ProbeResult(
            status=STATUS_INCOMPATIBLE,
            detail="doctor payload not a JSON object",
            launcher=None,
            doctor=None,
        )
    try:
        doctor = DoctorReport(
            python=str(payload["python"]),
            version=str(payload["version"]),
            implementation=str(payload["implementation"]),
            supported=bool(payload["supported"]),
            plugin_root=str(payload["plugin_root"]),
            index_path=str(payload["index_path"]),
        )
    except KeyError as missing:
        return ProbeResult(
            status=STATUS_INCOMPATIBLE,
            detail=f"doctor payload missing {missing}",
            launcher=None,
            doctor=None,
        )
    if not doctor.supported:
        detail = f"unsupported interpreter {doctor.implementation} {doctor.version}"
        return ProbeResult(STATUS_INCOMPATIBLE, detail, None, doctor)
    return ProbeResult(STATUS_AVAILABLE, "doctor healthy", resolution.launcher, doctor)


def probe_codemap(timeout: float = _DEFAULT_TIMEOUT, provider_root: Path | None = None) -> ProbeResult:
    """Probe `codemap-py` presence and interpreter health via the public CLI only.

    Examples:
        >>> probe_codemap().status in {"available", "absent", "incompatible"}
        True
    """
    resolution = _resolve_codemap_executable() if provider_root is None else _resolve_codemap_executable(provider_root)
    return _probe_codemap(resolution, timeout)


def _query_argv(
    launcher: str, spec: QuerySpec, target: str | None, root: Path | None, diff_file: Path | None
) -> list[str]:
    """Build one compact query command; the caller has already checked every required input is present."""
    # `--root` is a top-level `query` flag (query.py registers it on the parent parser),
    # so it must precede the subcommand — argparse rejects it once the subcommand is consumed.
    argv = [launcher, "query", "--compact"]
    if root is not None:
        argv += ["--root", str(root)]
    argv.append(spec.subcommand)
    if spec.requires_target and target is not None:
        argv.append(target)
    if spec.requires_diff_file and diff_file is not None:
        argv += ["--diff-file", str(diff_file)]
    argv.extend(spec.extra_args)
    return argv


def _run_one_query(
    launcher: str,
    spec: QuerySpec,
    target: str | None,
    root: Path | None,
    timeout: float,
    diff_file: Path | None = None,
    target_error: str = _MISSING_TARGET_ERROR,
) -> QueryOutcome:
    """Run one planned query from an absolute ``root`` and keep its bounded answer plus completeness metadata.

    ``root`` and ``diff_file`` must already be absolute: the provider runs from ``root`` and would otherwise resolve a
    relative diff file against the wrong directory. ``target_error`` is recorded when a target-requiring query has no
    usable target: none supplied, or one supplied in a shape no route accepts.
    """
    if spec.requires_target and target is None:
        return QueryOutcome(spec.subcommand, target, -1, False, False, (), 0, target_error)
    if spec.requires_diff_file and diff_file is None:
        # Never fall back to the provider's working-tree diff: in a detached review worktree at the reviewed head it
        # compares the tree with itself and reports zero changed files as a complete answer.
        return QueryOutcome(spec.subcommand, target, -1, False, False, (), 0, _MISSING_DIFF_FILE_ERROR)
    # Codemap discovers its index from the working directory, while `--root` only names the tree compared with that
    # index's scan root. Running from `root` makes both refer to the same checkout, so a caller in a nested review
    # worktree does not silently walk up to the parent repository's index.
    exit_code, payload, error = _run_json(_query_argv(launcher, spec, target, root, diff_file), timeout, root)
    if exit_code != 0 or payload is None:
        return _failed_query_outcome(spec.subcommand, target, exit_code, payload, error)
    body = payload if isinstance(payload, dict) else {}
    index = body.get("index", {})
    index = index if isinstance(index, dict) else {}
    changed_files = body.get("changed_files")
    changed_files = changed_files if isinstance(changed_files, int) and not isinstance(changed_files, bool) else None
    answer, answer_truncated = _bounded_answer(body)
    # The provider maps post-images only; the adapter reads the same diff back to name what that leaves unanalysed.
    diff_text = read_diff_text(diff_file) if spec.requires_diff_file and diff_file is not None else ""
    return QueryOutcome(
        subcommand=spec.subcommand,
        target=target,
        exit_code=exit_code,
        stale=bool(index.get("stale", False)),
        query_complete=bool(index.get("query_complete", index.get("exhaustive", False))),
        not_covered=tuple(index.get("not_covered", ())),
        degraded_count=int(index.get("degraded", 0)),
        error=None,
        index_path=_reported_text(index, "index_path"),
        completeness_reason=_reported_text(index, "completeness_reason"),
        root_mismatch=index.get("root_mismatch") is True,
        note=_reported_text(index, "note"),
        changed_files=changed_files,
        adapter_gap=change_set_gap(spec.subcommand, answer, diff_text),
        answer=answer,
        answer_truncated=answer_truncated,
    )


def _failed_query_outcome(
    subcommand: str, target: str | None, exit_code: int, payload: Any, error: str | None
) -> QueryOutcome:
    """Record one failed query in the provider's own words, with its structured detail and the side that caused it.

    Codemap prints a JSON error object on stdout for every failure it diagnosed, so its `error`/`detail` text is kept
    instead of a generic label, and its other fields become the bounded ``answer``. That answer stays an object, empty
    when the provider printed only its error text, because whether a JSON object came back at all is what tells a
    provider-diagnosed exit 2 from an argument-parser usage exit. ``input_rejected`` is then read off the recorded
    answer, never the raw payload, so the record carries every input its own flag depends on.

    Examples:
        >>> ambiguous = {"error": "Symbol 'build' is ambiguous", "candidates": ["a::build", "b::build"],
        ...              "candidate_count": 2, "rejected_target": "build"}
        >>> outcome = _failed_query_outcome("test-impact", "build", 1, ambiguous, None)
        >>> outcome.input_rejected, outcome.answer["candidates"]
        (True, ['a::build', 'b::build'])
        >>> provider_side = _failed_query_outcome("fn-rdeps", "pkg::f", 1, {"error": "Index is v2"}, None)
        >>> provider_side.input_rejected, provider_side.answer
        (False, {})
        >>> _failed_query_outcome("diff-impact", None, 2, None, "usage: codemap-py query ...").answer is None
        True
    """
    message = _reported_text(payload, "error")
    detail = _reported_text(payload, "detail")
    reason = f"{message}: {detail}" if message and detail else message or error or "query failed"
    # A not-indexed exit raised by a failed *load* carries the addressed index path at the payload root; one raised for
    # a missing module after a successful load carries none. Both are recorded exactly as received — an absent key stays
    # `None` rather than being back-filled from the probe, which would manufacture the agreement this field tests. Other
    # exits may carry a `path` naming something else (an unreadable diff file), never an index.
    addressed = _reported_text(payload, "path") if exit_code == _EXIT_NOT_INDEXED else None
    answer: dict[str, Any] | None = None
    answer_truncated: dict[str, int] = {}
    if isinstance(payload, dict):
        answer, answer_truncated = _bounded_answer(
            {key: value for key, value in payload.items() if key not in _FAILURE_TEXT_KEYS}
        )
    return QueryOutcome(
        subcommand,
        target,
        exit_code,
        False,
        False,
        (),
        0,
        reason,
        addressed,
        answer=answer,
        answer_truncated=answer_truncated,
        input_rejected=provider_rejected_input(exit_code, answer),
    )


def provider_rejected_input(exit_code: int, answer: Any) -> bool:
    """Return whether a failed query's recorded answer shows the provider refused the caller's input, not itself.

    Public so an artifact reader re-derives a recorded ``input_rejected`` flag instead of trusting it: the flag is what
    keeps a batch with no successful query ``degraded`` rather than ``incompatible``, and so decides whether specialist
    follow-ups may still run. ``answer`` is the recorded one — the provider's JSON error object minus its
    ``error``/``detail`` text, ``{}`` when nothing else was in it, ``None`` when the provider printed no JSON object.

    A bare exit 2 is the argument parser refusing the adapter's own argv (flag skew), so exit 2 counts only when the
    provider printed its own JSON object. Exits 1 and 3 also cover provider-side failures — an invalid or unloadable
    index, a disabled feature — so a target the provider could not resolve counts only when the answer carries the
    provider's target-rejection marker, or, from a provider predating it, a not-indexed exit names the ``module`` it did
    not find. A successful query, and an input the adapter never sent, never counts.

    Examples:
        >>> provider_rejected_input(3, {"module": "pkg.gone", "suggestions": []})
        True
        >>> provider_rejected_input(3, {"path": "/repo/index.json"})
        False
        >>> provider_rejected_input(2, {}), provider_rejected_input(2, None)
        (True, False)
        >>> provider_rejected_input(1, {})
        False
    """
    if not isinstance(answer, dict) or exit_code == 0:
        return False
    if exit_code == _EXIT_BAD_INPUT:
        return True
    return isinstance(answer.get(_REJECTED_TARGET_KEY), str) or (
        exit_code == _EXIT_NOT_INDEXED and isinstance(answer.get("module"), str)
    )


def read_diff_text(path: Path) -> str | None:
    """Read a change-set query's unified diff for gap detection, or return `None` when it cannot be read.

    Public so an artifact reader re-derives a recorded gap from the same file decoded the same way. Undecodable bytes
    are replaced rather than failing, because only the diff's file headers matter here.

    Examples:
        >>> read_diff_text(Path("no-such-diff.patch")) is None
        True
    """
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def change_set_gap(subcommand: str, answer: Any, diff_text: str | None) -> str | None:
    """Return every gap a change-set answer leaves that the provider's own completeness flags do not report.

    The provider maps only post-image ``.py`` paths and still calls the answer complete when:

    - none of the changed Python files it read maps to a module — a mismatched index, or a change that only adds
      modules — so the artifact holds no blast radius at all;
    - the diff deletes a Python module, including a rename's old path, whose importers it therefore never checks;
    - it read no Python file at all because every Python change lacks a text hunk (mode, binary, or empty file).

    Each gap makes the artifact ``degraded`` with its reason rather than ``available``; several join with ``"; "``.
    ``diff_text`` is the diff the query read, ``""`` for a query that reads none, or ``None`` when it was unreadable.

    Examples:
        >>> change_set_gap("diff-impact", {"changed_files": 2, "changed_modules": []}, "")
        'all 2 changed Python files unmapped by the index'
        >>> deletion = "--- a/pkg/util.py\\n+++ /dev/null\\n@@ -1,2 +0,0 @@\\n"
        >>> change_set_gap("diff-impact", {"changed_files": 0, "changed_modules": []}, deletion)
        '1 deleted Python modules not analysed (importers unchecked): pkg/util.py'
        >>> change_set_gap("diff-impact", {"changed_files": 1, "changed_modules": []}, deletion).split("; ")[0]
        'all 1 changed Python files unmapped by the index'
        >>> change_set_gap("diff-impact", {"changed_files": 0, "changed_modules": []}, "") is None
        True
    """
    if subcommand != "diff-impact" or not isinstance(answer, dict):
        return None
    changed = answer.get("changed_files")
    if type(changed) is not int:
        return None
    gaps: list[str] = []
    modules = answer.get("changed_modules")
    if changed > 0 and isinstance(modules, list) and not modules:
        gaps.append(f"all {changed} changed Python files unmapped by the index")
    if diff_text is None:
        gaps.append(_UNREADABLE_DIFF_GAP)
    else:
        unread = _unread_python_files(diff_text)
        if unread.deleted:
            count, sample = len(unread.deleted), _path_sample(unread.deleted)
            gaps.append(f"{count} deleted Python modules not analysed (importers unchecked): {sample}")
        # A content-free change matters only when it explains an answer that read no Python file at all; beside other
        # changes it adds no structure (a new empty `__init__.py`), so it would degrade a complete answer for nothing.
        if changed == 0 and unread.content_free:
            count, sample = len(unread.content_free), _path_sample(unread.content_free)
            gaps.append(f"{count} Python files changed without a text hunk not analysed: {sample}")
    return "; ".join(gaps) or None


@dataclass(frozen=True)
class _UnreadPythonFiles:
    """Python paths a unified diff names that the provider's post-image parser never reads."""

    #: Pre-image paths the change deletes, including a rename's old path, so their importers go unchecked.
    deleted: tuple[str, ...]
    #: Paths changed without a text hunk (mode, binary, or empty-file change), so the provider parsed nothing for them.
    content_free: tuple[str, ...]


def _unread_python_files(diff_text: str) -> _UnreadPythonFiles:
    """Return the case-sensitive ``.py`` paths a unified diff names that the provider never reads.

    The provider reads only ``+++ b/<path>`` post-images: it drops a deleted file (``+++ /dev/null``), never sees an
    empty or binary deletion that has no ``---``/``+++`` pair at all, never sees a rename's old path, and has nothing to
    read for a change without a text hunk. Each ``diff --git`` section is classified on its own; text before the first
    such header (a plain unified diff) contributes only its ``---``/``+++`` deletion pairs.

    Examples:
        >>> diff = (
        ...     "diff --git a/pkg/util.py b/pkg/util.py\\ndeleted file mode 100644\\n"
        ...     "--- a/pkg/util.py\\n+++ /dev/null\\n@@ -1,2 +0,0 @@\\n-def helper():\\n-    return 1\\n"
        ...     "diff --git a/pkg/__init__.py b/pkg/__init__.py\\ndeleted file mode 100644\\n"
        ...     "diff --git a/run.py b/run.py\\nold mode 100644\\nnew mode 100755\\n"
        ...     "diff --git a/old.py b/new.py\\nsimilarity index 100%\\nrename from old.py\\nrename to new.py\\n"
        ... )
        >>> _unread_python_files(diff)
        _UnreadPythonFiles(deleted=('old.py', 'pkg/__init__.py', 'pkg/util.py'), content_free=('run.py',))
    """
    sections: list[list[str]] = [[]]
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            sections.append([])
        sections[-1].append(line)
    deleted: set[str] = set()
    content_free: set[str] = set()
    for section in sections:
        section_deleted, section_content_free = _section_unread_python_files(section)
        deleted |= section_deleted
        content_free |= section_content_free
    return _UnreadPythonFiles(tuple(sorted(deleted)), tuple(sorted(content_free)))


def _section_unread_python_files(lines: list[str]) -> tuple[set[str], set[str]]:
    """Return one diff section's deleted and content-free ``.py`` paths; see :func:`_unread_python_files`."""
    git_section = bool(lines) and lines[0].startswith("diff --git ")
    path = _git_header_path(lines[0]) if git_section else None
    # Extended header lines (`deleted file mode`, `rename from`) exist only between a git header and its first `---` or
    # hunk; outside that window the same words could be unprefixed prose, never a file header.
    in_extended_header = git_section
    deleted: set[str] = set()
    whole_file_deleted = post_image = False
    for previous, line in pairwise(lines):
        if line.startswith(("--- ", "@@")):
            in_extended_header = False
        if in_extended_header and line.startswith("deleted file mode "):
            whole_file_deleted = True
        elif in_extended_header and line.startswith("rename from "):
            deleted.add(unquote_git_path(line.removeprefix("rename from ")))
        elif line.startswith("+++ ") and previous.startswith("--- "):
            if line[4:].split("\t")[0].strip() == _DEV_NULL:
                deleted.add(_diff_side_path(previous[4:]))
            else:
                post_image = True
    content_free: set[str] = set()
    if path is not None and whole_file_deleted:
        deleted.add(path)
    elif path is not None and not (post_image or deleted) and path.endswith(".py"):
        content_free.add(path)
    return {item for item in deleted if item.endswith(".py")}, content_free


def unquote_git_path(token: str) -> str:
    """Return a path git printed with C-style quoting as the plain path, or *token* unchanged when it is not quoted.

    Under git's default ``core.quotePath`` a path holding a non-ASCII byte is printed double-quoted with each such byte
    as an octal escape (``"pkg/mod\\303\\251.py"``) — in ``--name-only`` listings and in every diff header — and a
    ``"``, ``\\`` or control character is quoted under any setting. Read verbatim, that token ends in ``"`` rather
    than ``.py``, so a changed or deleted Python module was silently skipped. The escapes decode to bytes and the bytes
    to UTF-8, the encoding git's path output uses on every platform; the provider unquotes the same way.

    Public so an artifact reader classifies the run's ``files.txt`` with the same decoding.

    Examples:
        >>> unquote_git_path('"pkg/mod\\\\303\\\\251.py"')
        'pkg/modé.py'
        >>> unquote_git_path('"a/x \\\\"q\\\\".py"')
        'a/x "q".py'
        >>> unquote_git_path("pkg/plain name.py")
        'pkg/plain name.py'
    """
    if len(token) < 2 or not (token.startswith('"') and token.endswith('"')):
        return token
    body = token[1:-1]
    decoded = bytearray()
    cursor = 0
    for match in _GIT_QUOTED_ESCAPE.finditer(body):
        decoded += body[cursor : match.start()].encode("utf-8")
        code = match.group(1)
        decoded.append(int(code, 8) if len(code) == 3 else _GIT_QUOTE_ESCAPES[code])
        cursor = match.end()
    decoded += body[cursor:].encode("utf-8")
    return decoded.decode("utf-8", errors="replace")


def _diff_side_path(raw: str) -> str:
    """Return a ``---``/``+++`` header path the way the provider reads it: unquoted, no timestamp, no side prefix.

    Examples:
        >>> _diff_side_path('"a/pkg/mod\\\\303\\\\251.py"')
        'pkg/modé.py'
    """
    path = unquote_git_path(raw.split("\t")[0].strip())
    return path[2:] if path.startswith(("a/", "b/")) else path


def _git_header_path(header: str) -> str | None:
    """Return the one path a ``diff --git`` header names when both sides agree, else ``None`` (rename or copy).

    Both sides of an unrenamed path are quoted alike, so the header still splits in half when git C-quotes them.

    Examples:
        >>> _git_header_path("diff --git a/pkg/x y.py b/pkg/x y.py")
        'pkg/x y.py'
        >>> _git_header_path('diff --git "a/pkg/mod\\\\303\\\\251.py" "b/pkg/mod\\\\303\\\\251.py"')
        'pkg/modé.py'
        >>> _git_header_path("diff --git a/old.py b/new.py") is None
        True
    """
    sides = header.removeprefix("diff --git ")
    half = len(sides) // 2
    if sides[half : half + 1] != " ":
        return None
    old, new = _diff_side_path(sides[:half]), _diff_side_path(sides[half + 1 :])
    return old if old == new else None


def _path_sample(paths: tuple[str, ...]) -> str:
    """Return at most ``_GAP_PATH_LIMIT`` paths for a gap reason, naming how many more were left out.

    Examples:
        >>> _path_sample(tuple(f"m{i}.py" for i in range(7)))
        'm0.py, m1.py, m2.py, m3.py, m4.py, +2 more'
    """
    shown = ", ".join(paths[:_GAP_PATH_LIMIT])
    hidden = len(paths) - _GAP_PATH_LIMIT
    return f"{shown}, +{hidden} more" if hidden > 0 else shown


def _bounded_answer(body: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
    """Return the provider's answer with completeness metadata removed and every list and string bounded.

    Examples:
        >>> answer, cut = _bounded_answer({"imported_by": [f"m{i}" for i in range(25)], "index": {}})
        >>> len(answer["imported_by"]), cut
        (20, {'imported_by': 25})
    """
    truncated: dict[str, int] = {}
    answer = {
        key: _bounded_value(value, key, 0, truncated) for key, value in body.items() if key not in _ANSWER_EXCLUDED_KEYS
    }
    return answer, truncated


def _bounded_value(value: Any, path: str, list_depth: int, truncated: dict[str, int]) -> Any:
    """Bound one answer value, recording the original length of every list or string it cuts."""
    if isinstance(value, str):
        if len(value) > _ANSWER_TEXT_LIMIT:
            truncated[path] = len(value)
            return value[:_ANSWER_TEXT_LIMIT]
        return value
    if isinstance(value, dict):
        return {key: _bounded_value(item, f"{path}.{key}", list_depth, truncated) for key, item in value.items()}
    if isinstance(value, list):
        limit = ANSWER_LIST_LIMIT if list_depth == 0 else _ANSWER_NESTED_LIST_LIMIT
        if len(value) > limit:
            truncated[path] = len(value)
        return [
            _bounded_value(item, f"{path}[{position}]", list_depth + 1, truncated)
            for position, item in enumerate(value[:limit])
        ]
    return value


def _reported_text(block: Any, key: str) -> str | None:
    """Return one provider-reported string field, or `None` when it reported none.

    Tolerates a provider predating the field: a missing key, a non-mapping block, a non-string
    value, and an empty string all reduce to `None`, so an older Codemap yields absence rather
    than a fabricated value or a raised exception.

    Examples:
        >>> _reported_text({"index_path": "/repo/.codemap/index.json"}, "index_path")
        '/repo/.codemap/index.json'
        >>> _reported_text({}, "completeness_reason") is None
        True
    """
    if not isinstance(block, dict):
        return None
    value = block.get(key)
    return value if isinstance(value, str) and value else None


def _index_path_divergences(probe: ProbeResult, queries: tuple[QueryOutcome, ...]) -> tuple[IndexPathDivergence, ...]:
    """Return every query whose reported index path disagreed with the probe's own.

    `doctor` derives its path from Codemap's resolver in a separate process; each query reports
    the path that process actually opened. A disagreement means the two processes resolved
    different indexes — a stale index-directory override, a different git root, or a self-heal
    that rewrote elsewhere — so the probe's path is not provenance for the answers returned.

    The divergence is recorded, never reconciled: the adapter has no basis for electing one path
    as correct, and the two paths are compared verbatim rather than normalized, since normalizing
    would silently absorb exactly the symlink and relative-root differences worth reporting. A
    missing path on either side yields no record — absence is not disagreement.

    Examples:
        >>> probe = ProbeResult("available", "", "/bin/codemap-py", None)
        >>> _index_path_divergences(probe, ())
        ()
    """
    doctor_path = probe.doctor.index_path if probe.doctor is not None else ""
    if not doctor_path:
        return ()
    return tuple(
        IndexPathDivergence(outcome.subcommand, doctor_path, outcome.index_path)
        for outcome in queries
        if outcome.index_path is not None and outcome.index_path != doctor_path
    )


def _has_evidence_gap(queries: tuple[QueryOutcome, ...]) -> bool:
    """Return whether any mapped query failed or returned non-exhaustive completeness metadata.

    Examples:
        >>> clean = QueryOutcome("central", None, 0, False, True, (), 0, None)
        >>> _has_evidence_gap((clean,))
        False
    """
    return any(
        outcome.error is not None
        or not outcome.query_complete
        or bool(outcome.not_covered)
        or outcome.degraded_count > 0
        or outcome.root_mismatch
        or outcome.adapter_gap is not None
        for outcome in queries
    )


def gap_reasons(queries: tuple[QueryOutcome, ...] | list[QueryOutcome]) -> tuple[str, ...]:
    """Return the ``status_reasons`` lines a set of query outcomes implies, in query order.

    Examples:
        >>> gap_reasons([QueryOutcome("central", None, 0, False, True, (), 0, None)])
        ()
    """
    return tuple(reason for outcome in queries for reason in _query_gap_reasons(outcome))


def _query_gap_reasons(outcome: QueryOutcome) -> list[str]:
    """Return one ``<subcommand>: <cause>`` line for every gap or caveat one query carries.

    Provider wording is kept verbatim, so a reader of a ``degraded`` artifact sees the provider's own explanation, such
    as ``root_mismatch``, instead of a bare flag.

    Examples:
        >>> mismatch = QueryOutcome("diff-impact", None, 0, False, False, (), 0, None, None, "root_mismatch", True)
        >>> _query_gap_reasons(mismatch)
        ['diff-impact: incomplete: root_mismatch', 'diff-impact: root_mismatch: index built for another root']
    """
    causes: list[str] = []
    if outcome.error is not None:
        causes.append(outcome.error)
    elif not outcome.query_complete:
        causes.append(f"incomplete: {outcome.completeness_reason or 'provider reported no reason'}")
    if outcome.root_mismatch:
        causes.append("root_mismatch: index built for another root")
    if outcome.adapter_gap is not None:
        causes.append(outcome.adapter_gap)
    if outcome.not_covered:
        causes.append("not covered: " + ", ".join(outcome.not_covered))
    if outcome.degraded_count > 0:
        causes.append(f"{outcome.degraded_count} degraded entries")
    if outcome.stale:
        causes.append("stale: index older than source")
    # Advisory, never a status change: the provider keeps `query_complete` for a zero-caller method, so this line is
    # what stops a reader of the reasons alone from taking `called_by: []` as proof the method is unused.
    hint = outcome.answer.get("hint") if isinstance(outcome.answer, dict) else None
    if isinstance(hint, str) and hint:
        causes.append(f"hint: {hint}")
    return [f"{outcome.subcommand}: {cause}" for cause in causes]


def _reduce_status(probe: ProbeResult, queries: tuple[QueryOutcome, ...]) -> str:
    """Reduce a probe plus its queries to one overall named status, composing coexisting caveats."""
    return reduce_status(probe.status, queries)


def reduce_status(probe_status: str, queries: tuple[QueryOutcome, ...] | list[QueryOutcome]) -> str:
    """Return the one named status a probe status plus its query outcomes implies.

    Public so an artifact reader can re-derive the recorded status from the recorded query outcomes instead of trusting
    it.

    Examples:
        >>> reduce_status("available", [QueryOutcome("rdeps", "m", 0, False, True, (), 0, None, root_mismatch=True)])
        'degraded'
    """
    if probe_status != STATUS_AVAILABLE:
        return probe_status
    if not queries:
        return STATUS_AVAILABLE
    succeeded = [outcome for outcome in queries if outcome.error is None]
    # A batch whose only failures are caller input the provider rejected or never saw stays `degraded`:
    # the provider works, so the run is not evidence that the integration itself is unusable.
    if not succeeded and not any(_is_caller_input_failure(outcome) for outcome in queries):
        return STATUS_INCOMPATIBLE
    stale = any(outcome.stale for outcome in succeeded)
    gapped = _has_evidence_gap(queries)
    if stale and gapped:
        return STATUS_STALE_DEGRADED
    if stale:
        return STATUS_STALE
    return STATUS_DEGRADED if gapped else STATUS_AVAILABLE


def _is_caller_input_failure(outcome: QueryOutcome) -> bool:
    """Return whether a failed query failed on caller input rather than on the provider itself.

    Exit 2 alone does not qualify: the provider's argument parser uses it for a usage error, which means the provider
    rejected the adapter's own command line and cannot answer this run. Only an outcome the adapter recorded as
    ``input_rejected`` — the provider's own diagnosis — or an input the adapter never sent counts.

    Examples:
        >>> usage = QueryOutcome("diff-impact", None, 2, False, False, (), 0, "usage: codemap-py query ...")
        >>> diagnosed = QueryOutcome("rdeps", "m", 3, False, False, (), 0, "module not indexed", input_rejected=True)
        >>> _is_caller_input_failure(usage), _is_caller_input_failure(diagnosed)
        (False, True)
    """
    return outcome.error in _CALLER_INPUT_ERRORS or (
        outcome.input_rejected and outcome.error is not None and outcome.exit_code != 0
    )


def _normalized_fact_target(query_kind: str, target: str | None) -> str | None:
    """Return the one target form a compact fact route sends, or ``None`` when no usable target was supplied.

    Dotted and bare symbol names pass through unchanged: the provider resolves a unique one and lists candidates for an
    ambiguous one, so guessing here would only hide its answer. ``dependencies`` reads a module, so it keeps the module
    portion of a ``module::symbol`` qname. Only a shape no route accepts — an empty side around ``::``, or more than one
    ``::`` — is refused without a query.

    Examples:
        >>> _normalized_fact_target("callers", "pkg.util.helper")
        'pkg.util.helper'
        >>> _normalized_fact_target("dependencies", "pkg.util::helper")
        'pkg.util'
        >>> _normalized_fact_target("blast", "pkg.util::") is None
        True
    """
    if query_kind in {"central", "coupling"} or target is None:
        return None
    normalized = target.strip()
    module, separator, symbol = normalized.partition("::")
    if not normalized or "::" in symbol or (separator and (not module or not symbol)):
        return None
    return module if separator and query_kind == "dependencies" else normalized


def _unusable_target_error(target: str | None) -> str:
    """Return why a required target produced no query: none supplied, or supplied in a shape no route accepts.

    Examples:
        >>> _unusable_target_error("pkg::") == _MALFORMED_TARGET_ERROR
        True
        >>> _unusable_target_error("  ") == _MISSING_TARGET_ERROR
        True
    """
    return _MALFORMED_TARGET_ERROR if target is not None and target.strip() else _MISSING_TARGET_ERROR


def _applicable_standard_specs(specs: tuple[QuerySpec, ...], target: str | None) -> tuple[QuerySpec, ...]:
    """Return the standard batch reduced to the queries a caller's target actually supports.

    `target` is optional for every skill that consumes the standard batch. Running a
    target-requiring query anyway records a bounded error, and that error reduces to
    `degraded` — a status the contract reserves for a gap in the *provider's* evidence.
    A caller that asked no targeted question has no such gap, so the query is dropped
    from the plan instead: `available` stays truthful because the contract scopes it to
    "the queries run". Explicit fact routes keep degrading on a missing target, because
    there the target is the caller's own required input rather than an optional refinement.

    Examples:
        >>> plan = _applicable_standard_specs(CATEGORY_QUERIES["analysis"], None)
        >>> [spec.subcommand for spec in plan]
        ['central']
    """
    if target is not None:
        return specs
    applicable = tuple(spec for spec in specs if not spec.requires_target)
    # A batch of only target-requiring queries keeps its bounded errors: reporting
    # `available` off zero executed queries would claim evidence never received.
    return applicable or specs


def _query_plan(category: str, query_kind: str, target: str | None) -> tuple[tuple[QuerySpec, ...], str | None]:
    """Return the bounded query plan while retaining the legacy category batch for `standard`."""
    specs = CATEGORY_QUERIES.get(category)
    if specs is None:
        known = ", ".join(sorted(CATEGORY_QUERIES))
        raise ValueError(f"unknown category {category!r}; expected one of: {known}")
    if query_kind not in QUERY_KINDS:
        known_kinds = ", ".join(QUERY_KINDS)
        raise ValueError(f"unknown query kind {query_kind!r}; expected one of: {known_kinds}")
    if query_kind == "standard":
        return _applicable_standard_specs(specs, target), target
    if query_kind == "skip":
        return (), target
    return (_FACT_QUERY_SPECS[query_kind],), _normalized_fact_target(query_kind, target)


def gather_structural_context(
    category: str,
    target: str | None = None,
    root: Path | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
    query_kind: str = "standard",
    provider_root: Path | None = None,
    diff_file: Path | None = None,
) -> StructuralContext:
    """Record a skip, one fact query, or a category's legacy standard query batch.

    ``diff_file`` feeds change-set queries (the ``review`` batch's ``diff-impact``); those queries record a bounded
    degraded outcome instead of running when it is missing.

    Examples:
        >>> ctx = gather_structural_context("implementation", target="pkg.mod")
        >>> ctx.protocol_version
        'codemap-py.integration.v1'
    """
    specs, query_target = _query_plan(category, query_kind, target)
    # Codemap runs from `root`, so relative paths are resolved against the caller's working directory first; a relative
    # diff file would otherwise be read from inside `root` and fail as unreadable caller input.
    root = None if root is None else root.absolute()
    diff_file = None if diff_file is None else diff_file.absolute()
    recorded_diff_file = None if diff_file is None else str(diff_file)
    if query_kind == "skip":
        probe = ProbeResult(STATUS_SKIPPED, "query kind skip: no Codemap subprocess requested", None, None)
        return StructuralContext(
            protocol_version=PROTOCOL_VERSION,
            artifact_schema_version=ARTIFACT_SCHEMA_VERSION,
            category=category,
            query_kind=query_kind,
            target=target,
            status=STATUS_SKIPPED,
            probe=probe,
            diff_file=recorded_diff_file,
        )
    resolution = _resolve_codemap_executable() if provider_root is None else _resolve_codemap_executable(provider_root)
    probe = _probe_codemap(resolution, timeout, root)
    queries: tuple[QueryOutcome, ...] = ()
    if probe.status == STATUS_AVAILABLE and resolution.launcher is not None:
        target_error = _unusable_target_error(target)
        queries = tuple(
            _run_one_query(resolution.launcher, spec, query_target, root, timeout, diff_file, target_error)
            for spec in specs
        )
    status = _reduce_status(probe, queries)
    return StructuralContext(
        protocol_version=PROTOCOL_VERSION,
        artifact_schema_version=ARTIFACT_SCHEMA_VERSION,
        category=category,
        query_kind=query_kind,
        target=target,
        status=status,
        probe=probe,
        queries=queries,
        # Deliberately computed after the status and never folded into it: a path disagreement is
        # evidence about which index answered, not a defect in the answers, and the adapter cannot
        # tell which of the two processes resolved correctly. Reducing it into `degraded` would
        # assert a verdict the adapter has no grounds for and would hide the two paths themselves.
        # A provider-reported `root_mismatch` differs: that is the provider's own completeness
        # verdict on the answer, so it is part of the status and its reasons.
        index_path_divergence=_index_path_divergences(probe, queries),
        diff_file=recorded_diff_file,
        status_reasons=gap_reasons(queries),
    )


def _write_output(payload: dict[str, Any], out_path: Path | None) -> None:
    """Print JSON to stdout and, when requested, persist it once to a run artifact."""
    encoded = json.dumps(payload, indent=2, sort_keys=True)
    print(encoded)
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(encoded + "\n", encoding="utf-8")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse the adapter's two-subcommand CLI contract."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)

    probe_parser = sub.add_parser("probe", help="Probe codemap-py availability and interpreter health.")
    probe_parser.add_argument("--timeout", type=float, default=_DEFAULT_TIMEOUT)
    probe_parser.add_argument("--provider-root", type=Path, help="Absolute active installed provider root.")

    context_parser = sub.add_parser("context", help="Gather one category's structural-context evidence.")
    context_parser.add_argument("--category", required=True, choices=sorted(CATEGORY_QUERIES))
    context_parser.add_argument(
        "--query-kind",
        default="standard",
        choices=QUERY_KINDS,
        help="Bound Codemap work: skip, one fact route, or the legacy standard category batch.",
    )
    context_parser.add_argument("--target", default=None, help="Dotted module or module::symbol qname.")
    context_parser.add_argument("--root", type=Path, default=None)
    context_parser.add_argument("--out", type=Path, default=None, help="Also persist JSON to this run-artifact path.")
    context_parser.add_argument("--timeout", type=float, default=_DEFAULT_TIMEOUT)
    context_parser.add_argument("--provider-root", type=Path, help="Absolute active installed provider root.")
    context_parser.add_argument(
        "--diff-file", type=Path, default=None, help="Unified diff the review batch's diff-impact query reads."
    )

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Dispatch `probe`/`context`; always exits 0 — absence/incompatibility is data, not failure."""
    arguments = _parse_args(argv)
    if arguments.mode == "probe":
        _write_output(probe_codemap(timeout=arguments.timeout, provider_root=arguments.provider_root).to_dict(), None)
        return 0
    context = gather_structural_context(
        category=arguments.category,
        target=arguments.target,
        root=arguments.root,
        timeout=arguments.timeout,
        query_kind=arguments.query_kind,
        provider_root=arguments.provider_root,
        diff_file=arguments.diff_file,
    )
    _write_output(context.to_dict(), arguments.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
