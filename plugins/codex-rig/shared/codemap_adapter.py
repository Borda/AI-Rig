#!/usr/bin/env python3
"""Probe optional codemap-py structural context through its public CLI/JSON surface.

## Purpose

Add one bounded structural-context observation to a Codex Rig workflow without coupling to codemap-py internals or
source paths. The adapter records both interpreter health and category-specific query completeness so downstream
decisions can distinguish healthy context from degraded context.

## Scope

It calls only ``codemap-py doctor --json`` and public query commands; absence or incompatibility is non-fatal and
callers retain local inspection. ``CODEMAP_BIN`` is accepted only when it names an absolute, non-symlink executable, and
every subprocess is bounded by the requested timeout.

## Usage

Run ``python codemap_adapter.py probe`` or use the adapter's ``context`` action with ``--category`` and persist the
result once per workflow. The context form accepts an optional dotted target, ``--query-kind`` (skip, one compact fact,
or standard), repository root, timeout, and ``--out`` path; it always prints the same JSON payload that it writes.

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
no divergence claim.

## Failure

Launcher absence, unsupported output, timeout, or malformed JSON is classified in the probe/context status rather than
blocking the primary workflow. An unknown category still raises ``ValueError`` because it is invalid caller input,
whereas a failed optional query is represented in the JSON outcome and the CLI exits successfully.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


PROTOCOL_VERSION = "codemap-py.integration.v1"
ARTIFACT_SCHEMA_VERSION = 3
STATUS_AVAILABLE = "available"
STATUS_ABSENT = "absent"
STATUS_STALE = "stale"
STATUS_INCOMPATIBLE = "incompatible"
STATUS_DEGRADED = "degraded"
# One composed status, not a seventh independent one: `stale` and `degraded` are the only two
# conditions that can hold at once (every other status short-circuits before any query runs, and
# `stale` is only ever read off a query that parsed successfully). Consumers match this vocabulary
# by exact value, so ranking one condition above the other would silently drop the other's caveat:
# reporting `stale` alone invites the false conclusion that re-indexing restores exhaustiveness,
# and reporting `degraded` alone hides that the evidence describes an older tree.
STATUS_STALE_DEGRADED = "stale+degraded"
STATUS_SKIPPED = "skipped"
_DEFAULT_TIMEOUT = 15.0
_MISSING_TARGET_ERROR = "target required, none supplied"
_EXIT_NOT_INDEXED = 3
_WINDOWS_EXECUTABLE_SUFFIXES = {".bat", ".cmd", ".com", ".exe"}


@dataclass(frozen=True)
class QuerySpec:
    """One planned `codemap-py query` call for a structural-context category."""

    subcommand: str
    requires_target: bool
    extra_args: tuple[str, ...] = ()


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
    # code-review/code-remediate: changed-symbol/diff impact supplied once.
    "review": (QuerySpec("diff-impact", requires_target=False),),
    # audit/release: undocumented public surface + externally-uncalled modules (broken-ref proxy).
    "audit": (
        QuerySpec("undocumented", requires_target=False, extra_args=("--all",)),
        QuerySpec("dead-modules", requires_target=False),
    ),
}


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

_FACT_QUERY_SPECS: dict[str, QuerySpec] = {
    "central": QuerySpec("central", requires_target=False, extra_args=("--top", "5")),
    "callers": QuerySpec("fn-rdeps", requires_target=True, extra_args=("--exclude-tests",)),
    "blast": QuerySpec("fn-blast", requires_target=True),
    "dependencies": QuerySpec("rdeps", requires_target=True),
    "test-impact": QuerySpec("test-impact", requires_target=True),
    "coupling": QuerySpec("coupled", requires_target=False),
}


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
    """Result of one `codemap-py query` call, including completeness metadata."""

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
        }


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

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON shape written once to a run artifact."""
        return {
            "protocol_version": self.protocol_version,
            "artifact_schema_version": self.artifact_schema_version,
            "category": self.category,
            "query_kind": self.query_kind,
            "target": self.target,
            "status": self.status,
            "probe": self.probe.to_dict(),
            "queries": [outcome.to_dict() for outcome in self.queries],
            "index_path_divergence": [record.to_dict() for record in self.index_path_divergence],
        }


def _run_json(argv: list[str], timeout: float) -> tuple[int, dict[str, Any] | None, str | None]:
    """Run one subprocess and parse stdout as JSON; never raise on failure.

    Returns:
        ``(exit_code, parsed_json_or_none, error_message_or_none)``.
    """
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
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


def _resolve_codemap_executable() -> LauncherResolution:
    """Resolve one validated launcher, failing closed for a nonempty invalid `CODEMAP_BIN`."""
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
    executable = shutil.which("codemap-py")
    if executable is None:
        return LauncherResolution(None, STATUS_ABSENT, "codemap-py not found on PATH")
    return LauncherResolution(executable, STATUS_AVAILABLE, "PATH launcher")


def _probe_codemap(resolution: LauncherResolution, timeout: float) -> ProbeResult:
    """Probe one resolved launcher through ``doctor --json``; never re-resolve it."""
    if resolution.launcher is None:
        return ProbeResult(resolution.status, resolution.detail, None, None)
    exit_code, payload, error = _run_json([resolution.launcher, "doctor", "--json"], timeout)
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


def probe_codemap(timeout: float = _DEFAULT_TIMEOUT) -> ProbeResult:
    """Probe `codemap-py` presence and interpreter health via the public CLI only.

    Examples:
        >>> probe_codemap().status in {"available", "absent", "incompatible"}
        True
    """
    return _probe_codemap(_resolve_codemap_executable(), timeout)


def _run_one_query(
    launcher: str, spec: QuerySpec, target: str | None, root: Path | None, timeout: float
) -> QueryOutcome:
    """Run one planned query and reduce its response to completeness metadata."""
    # `--root` is a top-level `query` flag (query.py registers it on the parent parser),
    # so it must precede the subcommand — argparse rejects it once the subcommand is consumed.
    argv = [launcher, "query", "--compact"]
    if root is not None:
        argv += ["--root", str(root)]
    argv.append(spec.subcommand)
    if spec.requires_target:
        if target is None:
            return QueryOutcome(spec.subcommand, target, -1, False, False, (), 0, _MISSING_TARGET_ERROR)
        argv.append(target)
    argv.extend(spec.extra_args)
    exit_code, payload, error = _run_json(argv, timeout)
    if exit_code == _EXIT_NOT_INDEXED:
        # A not-indexed exit raised by a failed *load* carries the addressed path at the payload
        # root; one raised for a missing module after a successful load carries no path at all.
        # Both are recorded exactly as received — an absent key stays `None` rather than being
        # back-filled from the probe, which would manufacture the agreement this field exists to test.
        addressed = _reported_index_path(payload, "path")
        return QueryOutcome(spec.subcommand, target, exit_code, False, False, (), 0, "target not indexed", addressed)
    if exit_code != 0 or payload is None:
        return QueryOutcome(spec.subcommand, target, exit_code, False, False, (), 0, error or "query failed")
    index = payload.get("index", {}) if isinstance(payload, dict) else {}
    complete = bool(index.get("query_complete", index.get("exhaustive", False)))
    return QueryOutcome(
        subcommand=spec.subcommand,
        target=target,
        exit_code=exit_code,
        stale=bool(index.get("stale", False)),
        query_complete=complete,
        not_covered=tuple(index.get("not_covered", ())),
        degraded_count=int(index.get("degraded", 0)),
        error=None,
        index_path=_reported_index_path(index, "index_path"),
    )


def _reported_index_path(block: Any, key: str) -> str | None:
    """Return one provider-reported index path, or `None` when it reported none.

    Tolerates a provider predating the field: a missing key, a non-mapping block, a non-string
    value, and an empty string all reduce to `None`, so an older Codemap yields absence rather
    than a fabricated path or a raised exception.

    Examples:
        >>> _reported_index_path({"index_path": "/repo/.codemap/index.json"}, "index_path")
        '/repo/.codemap/index.json'
        >>> _reported_index_path({}, "index_path") is None
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
        for outcome in queries
    )


def _reduce_status(probe: ProbeResult, queries: tuple[QueryOutcome, ...]) -> str:
    """Reduce a probe plus its queries to one overall named status, composing coexisting caveats."""
    if probe.status != STATUS_AVAILABLE:
        return probe.status
    if not queries:
        return STATUS_AVAILABLE
    succeeded = [outcome for outcome in queries if outcome.error is None]
    # A batch that only ever reported a missing target stays `degraded`: the provider answered
    # normally, so the run is not evidence that the integration itself is unusable.
    if not succeeded and not any(outcome.error == _MISSING_TARGET_ERROR for outcome in queries):
        return STATUS_INCOMPATIBLE
    stale = any(outcome.stale for outcome in succeeded)
    gapped = _has_evidence_gap(queries)
    if stale and gapped:
        return STATUS_STALE_DEGRADED
    if stale:
        return STATUS_STALE
    return STATUS_DEGRADED if gapped else STATUS_AVAILABLE


def _normalized_fact_target(query_kind: str, target: str | None) -> str | None:
    """Return the one target form required by a compact fact route, without guessing malformed input."""
    if query_kind in {"central", "coupling"}:
        return None
    if target is None:
        return None
    normalized = target.strip()
    if not normalized or normalized.count("::") > 1:
        return None
    module, separator, symbol = normalized.partition("::")
    if query_kind in {"callers", "blast"}:
        return normalized if separator and module and symbol else None
    if separator and (not module or not symbol):
        return None
    if not separator:
        return normalized
    if query_kind == "dependencies":
        return module
    return normalized


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
) -> StructuralContext:
    """Record a skip, one fact query, or a category's legacy standard query batch.

    Examples:
        >>> ctx = gather_structural_context("implementation", target="pkg.mod")
        >>> ctx.protocol_version
        'codemap-py.integration.v1'
    """
    specs, query_target = _query_plan(category, query_kind, target)
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
        )
    resolution = _resolve_codemap_executable()
    probe = _probe_codemap(resolution, timeout)
    queries: tuple[QueryOutcome, ...] = ()
    if probe.status == STATUS_AVAILABLE and resolution.launcher is not None:
        queries = tuple(_run_one_query(resolution.launcher, spec, query_target, root, timeout) for spec in specs)
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
        index_path_divergence=_index_path_divergences(probe, queries),
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

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Dispatch `probe`/`context`; always exits 0 — absence/incompatibility is data, not failure."""
    arguments = _parse_args(argv)
    if arguments.mode == "probe":
        _write_output(probe_codemap(timeout=arguments.timeout).to_dict(), None)
        return 0
    context = gather_structural_context(
        category=arguments.category,
        target=arguments.target,
        root=arguments.root,
        timeout=arguments.timeout,
        query_kind=arguments.query_kind,
    )
    _write_output(context.to_dict(), arguments.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
