#!/usr/bin/env python
"""fill_gaps_scope.py — plan and check an ``/oss:review --fill-gaps`` targeted rerun of one published review run.

A fill-gaps run re-spawns only the reviewer roles a caller names and re-validates only the candidates it names, then
publishes a **new** run that carries every other assessment, candidate, open concern and ``findings.jsonl`` row of
the prior run unchanged. The prior run directory is only read, never written.

``plan`` validates the request before anything is allocated or spawned:

- the report sits at ``.reports/review/pr-<PR>/run-<NNN>/review-report.md`` for the requested PR;
- the prior run carries a readable version-one ``review-validation.json``, ``findings.jsonl`` and ``head-sha.txt``,
  and is not a terminal REJECT;
- every requested role is an ``assessments[].role`` and every requested candidate a ``candidates[].id`` of that
  run; every role maps to a review dimension (Codex roles also need the bridge plugin available);
- the prior ``head-sha.txt`` holds a 7-40 hex commit id, and the current PR head (``headRefOid`` in the review
  snapshot) starts with it;
- the prior run has no gap that concerns no record a rerun could replace: an unreadable or malformed sidecar, an
  unsupported contract, an open concern naming no candidate of the run, or a findings row naming none;
- no carried candidate is bound to a revision other than that head, because carrying it would publish stale evidence;
- the carried projection can pass the publication gate. The projection is what the new run must publish: the prior
  envelope minus the re-run roles' assessments and minus the re-validated candidates with their open concerns and
  findings rows, plus one findings row per ``publish_rows`` id, every other record verbatim. It is checked with the
  strict gate's own record rules (``find_review_report``), varying only what the new consolidator may legitimately
  write: the aggregate readiness; the aggregate outcome, which the consolidator recomputes over the merged
  assessments, so it may be null only when some projected assessment is a completed undecidable one and judged only
  when some projected assessment carries a judgment (a re-run reviewer's stand-in can be either, the prior's own label
  never counts); the re-run roles' own assessments; and an added open concern for an open carried candidate that has
  none. A carried record is never rebuilt, deduplicated or completed. The carried ``findings.jsonl`` rows must also
  pass the producer gate's row rules under any aggregate choice: each row ``find_review_report.finding_row_problem``,
  and no two rows one id (``duplicate_finding_id``). ``oss:review`` refuses to deliver a run publishing a row resolve's
  merge cannot use, and the carry check refuses one that repaired it, so only re-validating the row's candidate
  replaces it. A row without a string ``id`` is such a row, since ``mint_finding_ids.py --carry-from`` mints it afresh.
  The ``publish_rows`` rows the new run adds are not carried and not checked here. When no such choice passes, the
  refusal names the carried records whose addition to the request makes one pass, or says that no addition can (a
  failing row naming no recorded candidate is one).

A named candidate that is already CONFIRMED on that head, passes every record rule and has no open concern entry, but
lacks its ``findings.jsonl`` row (the validator's ``finding-missing`` gap), is not re-validated: its record is carried
verbatim and the new run only publishes the missing row. The plan lists such ids under ``publish_rows``; every other
named candidate is re-validated, so a broken record is always replaced rather than carried.

On success it writes the plan JSON and the prior head string (one line) to the given output files and prints a short
summary including the fixed spawn cost. On refusal it prints ``! BLOCKED — <reason>`` and truncates both output
files, so a stale plan can never reach the run-directory allocation step.

``triage`` answers, for ``oss:resolve``'s gap triage, whether a targeted rerun of a gap-graded report is admissible and
which subjects it must or may name, from the checks ``plan`` itself applies, so triage never offers a request the
planner refuses and never refuses a rerun some request it could offer would plan. It prints one JSON line::

    {"admissible": bool, "reason": str, "required_roles": [...], "required_candidates": [...],
     "roles": [...], "candidates": [...], "pr": str}

Selectable subjects are the names the report's gap assessment points at: roles from ``assessment-*`` gaps and from a
completed undecidable reviewer's ``readiness-mislabel``; candidate ids from ``candidate-*``, ``concern-*`` (except
``concern-unknown-candidate``), ``finding-*`` (a missing, duplicated, unbacked or unmergeable row of a recorded
candidate, which re-validation replaces) and every other ``readiness-mislabel``. Only names the run records qualify,
never ``-`` or a name holding ``,`` or ``"``, and a role only when ``plan`` could re-run it. When the request naming
every selectable subject still refuses, or no gap names one, the carried records that make a request pass (same name
rules) join the required subjects: a gap the grader cannot attribute, such as a stray invalid row or a carried row
resolve's merge cannot use, is still repaired by re-validating the candidate the row belongs to. ``required_*`` are the
subjects every passing request needs (of interchangeable subjects the gap-named one, then the later in gap order) plus
every QA or security role a gap names, whose per-PR security scan is mandatory, so a gap naming that reviewer is never
optional. ``roles``/``candidates`` are the optional rest: the gap-named subjects, or, when nothing is required and no
gap names a subject (the gaps lie outside every record, such as a header disagreement or an aggregate outcome label,
which any rerun rewrites), every recorded subject (same name rules) whose request alone plans. An admissible verdict
always lists at least one subject, and every request naming all required subjects plus any of the optional ones plans
as long as it names one subject at all. The rerun is not admissible when ``plan`` would refuse the prior run (layout,
sidecars, REJECT, head id), when the PR head moved, when a gap names a QA or security role no request can name or
re-run, when even naming every recorded subject refuses, or when no recorded subject can be named in a request that
plans; ``reason`` then says why and always ends by pointing to a full ``/oss:review`` of the PR (``<PR>`` literally
when no PR number is known), and every list is empty. ``pr`` is the PR number triage resolved (``--pr`` digits, else
the report header's ``PR:``), empty when none. An unknown PR head never blocks here: ``plan`` re-checks the real head.

``verify`` runs after the consolidator published the new run. It reads ``fill-gaps.json`` beside the new report and
checks that every carried prior record is still present and equal, and that no re-run role or re-validated candidate
was silently dropped. Additions are allowed: a carried CONFIRMED candidate that lacked its findings row may gain one.
A malformed plan or sidecar is reported as a problem line, never a traceback.

consumers: plugins/cc_oss/skills/review/modes/fill-gaps.md (plan gate, carry check);
plugins/cc_oss/skills/review/SKILL.md (Step 2 allocation reads the plan and head outputs);
plugins/cc_oss/skills/resolve/SKILL.md (gap triage reads the triage verdict)

Usage:
    python fill_gaps_scope.py plan --pr 8 --report=.reports/review/pr-8/run-002/review-report.md \
        --roles="QA specialist" --candidates=V13,V20 --snapshot "$SNAP_DIR/pr-meta.json" \
        --plan-out "$TMPDIR/oss-review-fill-plan-$CSID" --head-out "$TMPDIR/oss-review-fill-head-$CSID"
    python fill_gaps_scope.py verify --report .reports/review/pr-8/run-003/review-report.md
    python fill_gaps_scope.py triage --report=.reports/review/pr-8/run-002/review-report.md --pr=8
    python fill_gaps_scope.py triage --assessment="$GAPS_FILE" --pr="$PR_NUMBER"

Exit codes:
    0 — plan written, every carried record intact, or a triage verdict printed (admissible or not; an unreadable or
        malformed ``--assessment`` file is a not-admissible verdict)
    1 — request refused, a carried record altered or dropped, unusable arguments, or a review report that exists but
        cannot be read (reasons on stdout)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable, Collection, Sequence
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Final, NoReturn

#: Sibling import: bin/ is not a package. Putting it on the path keeps one header parser, one JSONL reader, one set of
#: publication rules and one bridge probe shared with the scripts that own them, whether run as a script or loaded by
#: the test conftest.
_BIN_DIR = str(Path(__file__).resolve().parent)
if _BIN_DIR not in sys.path:
    sys.path.insert(0, _BIN_DIR)

from check_bridge import bridge_status  # noqa: E402
from find_review_report import (  # noqa: E402
    _SOURCE_RATINGS,
    COMMIT_ID_RE,
    COMPLETED_NULL_MISLABEL,
    QA_ROLE_RE,
    Gap,
    _check_assessments,
    _check_candidate,
    _check_candidates,
    _GateRejected,
    _head_moved,
    _parse_header_fields,
    assess_report,
    duplicate_finding_id,
    finding_row_problem,
    supported_outcomes,
)
from mint_finding_ids import read_jsonl  # noqa: E402

#: Report basename every review run publishes.
_REPORT_NAME: Final = "review-report.md"
#: Plan file the Step 2 allocation copies beside the new report; ``verify`` reads it from there.
PLAN_NAME: Final = "fill-gaps.json"
#: Run directory names allocated by ``next_run_dir.py``.
_RUN_DIR_RE: Final = re.compile(r"^run-\d+$")
#: Digit-only PR numbers, guarding the path comparison against injection.
_PR_RE: Final = re.compile(r"^[0-9]+$")
#: Measured fixed overhead of one ``Agent()`` spawn, whatever work it does.
SPAWN_TOKENS: Final = 120_851
#: Skill-backed lane: costs a Codex run, not an ``Agent()`` spawn, so it is listed but not counted.
_BRIDGE_AGENT: Final = "bridge:review"
#: Exceptions an oddly typed record raises inside the strict record checks (an unhashable disposition, a list where a
#: mapping belongs); each marks one failed aggregate choice, never a traceback.
_MALFORMED: Final = (ValueError, KeyError, TypeError, AttributeError, RecursionError)
#: Gap rules that concern no record a rerun could replace, so no role or candidate added to a request repairs them.
#: A findings row naming no candidate of the run (``finding-unbacked`` with an unknown subject) is the fifth such gap.
_UNREPAIRABLE_RULES: Final = frozenset(
    {"sidecar-unreadable", "sidecar-malformed", "contract-unsupported", "concern-unknown-candidate"}
)
#: Aggregate readiness values the new consolidator may choose, tried in this order.
_READINESS_CHOICES: Final = ("incomplete", "blocked", "complete")
#: Stand-in judged aggregate outcome: the record rules tell a null outcome from a judged one, never one judgment from
#: another.
_JUDGED: Final = "NEEDS_WORK"
#: Projection failure when the carried assessments support neither a null nor a judged aggregate outcome.
_NO_OUTCOME: Final = (
    "no carried assessment supports an aggregate outcome (a null one needs a completed undecidable reviewer, "
    "a judged one a reviewer that judged)"
)
#: Text filling a record the new run writes but the plan cannot see yet; it lives only in the in-memory projection.
_STUB: Final = "written by the fill-gaps run"
#: Characters that would break the comma-separated ``--roles``/``--candidates`` value or its double quoting.
_UNSELECTABLE_CHARS: Final = frozenset(',"')
#: A report header's ``PR:`` field, with or without the leading ``#``.
_HEADER_PR_RE: Final = re.compile(r"#?\s*([0-9]+)")
#: PR number a full-review pointer names when triage knows none.
_UNKNOWN_PR: Final = "<PR>"
#: Separator ``finding_row_problem`` puts before the producer's remedy, which names a fix for the producing run, never
#: for a carried row the fill-gaps run must keep verbatim.
_REMEDY_SEPARATOR: Final = " — "


class FillGapsBlocked(Exception):
    """A fill-gaps request the prior run cannot satisfy; the message is the user-facing reason."""


@dataclass(frozen=True)
class Dimension:
    """One review dimension a reviewer role maps to: its spawn unit, agent and output file."""

    #: Spawn name from ``templates/agent-prompts.md``; merged dimensions share one unit.
    unit: str
    #: ``subagent_type`` of the spawn, or the skill for the bridge lane.
    agent: str
    #: Output file basename the spawn writes in the run directory.
    file: str


#: Ordered keyword table; the first pattern matching a role name wins. Order matters: "Performance engineer" must hit
#: perf before the generic engineer pattern, and "QA specialist" must not hit the CI pattern (word-bounded ``ci``).
#: The QA row is the validator's own ``QA_ROLE_RE``, so the role graded as the security reviewer is the one re-run by
#: the QA unit.
_DIMENSIONS: Final[tuple[tuple[re.Pattern[str], Dimension], ...]] = (
    (re.compile(r"codex|bridge", re.IGNORECASE), Dimension("codex", _BRIDGE_AGENT, "foundry--codex.md")),
    (re.compile(r"blind", re.IGNORECASE), Dimension("blind-solve", "general-purpose", "foundry--blind-solve.md")),
    (re.compile(r"issue", re.IGNORECASE), Dimension("review-issue", "foundry:doc-scribe", "issue-<N>.md")),
    (
        re.compile(r"challeng|adversar", re.IGNORECASE),
        Dimension("review-challenge", "foundry:challenger", "foundry--challenger.md"),
    ),
    (QA_ROLE_RE, Dimension("review-qa", "foundry:qa-specialist", "foundry--qa-specialist.md")),
    (
        re.compile(r"perf", re.IGNORECASE),
        Dimension("review-perf-api", "foundry:perf-optimizer", "foundry--perf-optimizer.md"),
    ),
    (
        re.compile(r"architect|\bapi\b", re.IGNORECASE),
        Dimension("review-perf-api", "foundry:perf-optimizer", "foundry--solution-architect.md"),
    ),
    (
        re.compile(r"lint", re.IGNORECASE),
        Dimension("review-docs-lint", "foundry:doc-scribe", "foundry--linting-expert.md"),
    ),
    (
        re.compile(r"\bdoc", re.IGNORECASE),
        Dimension("review-docs-lint", "foundry:doc-scribe", "foundry--doc-scribe.md"),
    ),
    (
        re.compile(r"\bci\b|cicd|pipeline|steward|workflow", re.IGNORECASE),
        Dimension("review-cicd", "oss:cicd-steward", "oss--cicd-steward.md"),
    ),
    (
        re.compile(r"software|engineer|\bsw\b|develop", re.IGNORECASE),
        Dimension("review-arch", "foundry:sw-engineer", "foundry--sw-engineer.md"),
    ),
)


@dataclass(frozen=True)
class PlanRequest:
    """One fill-gaps request as the review skill passes it."""

    pr: str
    report: Path
    roles: list[str]
    candidates: list[str]
    current_head: str


@dataclass(frozen=True)
class TriageVerdict:
    """Whether resolve may offer a targeted rerun of a gap-graded report, and which subjects it must or may name."""

    admissible: bool
    #: Why the rerun is not admissible; empty when it is.
    reason: str = ""
    #: Roles and candidate ids every passing request names; always part of the rerun.
    required_roles: list[str] = field(default_factory=list)
    required_candidates: list[str] = field(default_factory=list)
    #: The remaining selectable roles and candidate ids, the caller's choice.
    roles: list[str] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)
    #: The PR number the verdict is about: ``--pr`` digits, else the report header's ``PR:``; empty when unknown.
    pr: str = ""


class _Kind(str, Enum):
    """Which request list a selectable subject belongs to."""

    ROLE = "role"
    CANDIDATE = "candidate"


@dataclass(frozen=True)
class _Subject:
    """One role or candidate id a fill-gaps request can name."""

    kind: _Kind
    name: str


@dataclass(frozen=True)
class _Prior:
    """One published run as a fill-gaps request sees it: its envelope, rows, recorded head and graded gaps."""

    pr: str
    report: Path
    data: dict
    #: ``findings.jsonl`` rows exactly as read; a malformed row stays in place for the record checks to judge.
    findings: list
    head: str
    gaps: list[Gap]


@dataclass(frozen=True)
class _Scope:
    """What a request does to the prior run: roles re-run, candidates re-validated, findings rows published."""

    roles: tuple[str, ...]
    revalidate: tuple[str, ...]
    publish_rows: tuple[str, ...]


def split_names(raw: str) -> list[str]:
    """Split a comma-separated name list, trimming blanks and dropping repeats in order.

    Args:
        raw: The ``--roles`` or ``--candidates`` value as given.

    Returns:
        The distinct non-empty names, in first-seen order.

    Examples:
        >>> split_names(" Software engineer, QA specialist ,,QA specialist")
        ['Software engineer', 'QA specialist']
        >>> split_names("")
        []
    """
    names: list[str] = []
    for token in raw.split(","):
        name = token.strip()
        if name and name not in names:
            names.append(name)
    return names


def map_role(role: str) -> Dimension:
    """Return the review dimension a reviewer role name belongs to.

    Args:
        role: A role name as recorded in ``review-validation.json``.

    Returns:
        The first dimension whose keyword pattern matches the role.

    Raises:
        FillGapsBlocked: no dimension matches, so no agent could re-assess the role.

    Examples:
        >>> map_role("Performance engineer").file
        'foundry--perf-optimizer.md'
        >>> map_role("QA specialist").unit
        'review-qa'
        >>> map_role("Software engineer").agent
        'foundry:sw-engineer'
    """
    for pattern, dimension in _DIMENSIONS:
        if pattern.search(role):
            return dimension
    raise FillGapsBlocked(f"role {role!r} maps to no review dimension; run a full /oss:review instead")


def _named(record: object, key: str, names: Collection[str]) -> bool:
    """Tell whether ``record`` is a mapping whose ``key`` is one of ``names``; a non-string value never matches.

    Examples:
        >>> _named({"id": "V1"}, "id", ["V1"]), _named({"id": ["V1"]}, "id", {"V1"}), _named("V1", "id", ["V1"])
        (True, False, False)
    """
    value = record.get(key) if isinstance(record, dict) else None
    return isinstance(value, str) and value in names


def _names(records: object, key: str) -> list[str]:
    """List the distinct non-empty string ``key`` values of the mapping records, in first-seen order.

    Examples:
        >>> _names([{"id": "V1"}, {"id": 5}, "V2", {"id": "V1"}, {"id": ""}, {"id": "V3"}], "id")
        ['V1', 'V3']
        >>> _names({"id": "V1"}, "id")
        []
    """
    if not isinstance(records, list):
        return []
    values = (record.get(key) for record in records if isinstance(record, dict))
    return list(dict.fromkeys(value for value in values if isinstance(value, str) and value))


def _prior_run_dir(report: Path, pr: str) -> Path:
    """Return the run directory of ``report`` after checking it is a published run of PR ``pr``.

    Raises:
        FillGapsBlocked: the path is not ``.reports/review/pr-<pr>/run-<NNN>/review-report.md`` or is not a file.
    """
    if not _PR_RE.match(pr):
        raise FillGapsBlocked(f"PR number {pr!r} is not numeric")
    run_dir = report.parent
    layout = (report.name, run_dir.name, run_dir.parent.name, run_dir.parent.parent.name)
    expected = f".reports/review/pr-{pr}/run-<NNN>/{_REPORT_NAME}"
    if (
        layout[0] != _REPORT_NAME
        or not _RUN_DIR_RE.match(layout[1])
        or layout[2] != f"pr-{pr}"
        or layout[3] != "review"
        or run_dir.parent.parent.parent.name != ".reports"
    ):
        raise FillGapsBlocked(f"--fill-gaps {report.as_posix()!r} is not a report under {expected}")
    if not report.is_file():
        raise FillGapsBlocked(f"report {report.as_posix()!r} not found")
    return run_dir


def _load_prior(run_dir: Path) -> tuple[dict, list, str]:
    """Read the prior run's envelope, actionable rows and reviewed head string.

    Raises:
        FillGapsBlocked: a sidecar is missing or malformed, the run is legacy or a terminal REJECT, or no head is known.
    """
    report = run_dir / _REPORT_NAME
    fields = _parse_header_fields(report.read_text(encoding="utf-8", errors="replace").splitlines()) or {}
    try:
        data = json.loads((run_dir / "review-validation.json").read_text(encoding="utf-8"))
        findings = read_jsonl(run_dir / "findings.jsonl")
    except (OSError, ValueError, RecursionError) as exc:
        raise FillGapsBlocked(
            f"prior run has no readable review-validation.json/findings.jsonl ({type(exc).__name__}); "
            "a legacy or broken publication cannot be completed piecemeal"
        ) from exc
    if not isinstance(data, dict) or data.get("contract_version") != 1:
        raise FillGapsBlocked("prior review-validation.json is not contract_version=1")
    if not all(isinstance(data.get(key), list) for key in ("assessments", "candidates", "open_concerns")):
        raise FillGapsBlocked("prior review-validation.json lacks assessments/candidates/open_concerns lists")
    if data.get("source_outcome") == "REJECT" or "REJECT_" in fields.get("Gate", ""):
        raise FillGapsBlocked("prior run is a terminal REJECT; there are no gaps to fill")
    head_file = run_dir / "head-sha.txt"
    head = head_file.read_text(encoding="utf-8").strip() if head_file.is_file() else ""
    if not head:
        raise FillGapsBlocked("prior run has no head-sha.txt, so an unchanged head cannot be proven")
    # Same shape the reject gate accepts; a stray short string would prefix-match almost any PR head.
    if not COMMIT_ID_RE.fullmatch(head):
        raise FillGapsBlocked("prior head-sha.txt is not a 7-40 hex commit id")
    return data, findings, head


def _read_prior(report: Path, pr: str) -> _Prior:
    """Locate, load and grade the prior run of a fill-gaps request.

    Raises:
        FillGapsBlocked: the report is not a published run of PR ``pr``, or ``_load_prior`` refuses it.
    """
    data, findings, head = _load_prior(_prior_run_dir(report, pr))
    return _Prior(pr, report, data, findings, head, assess_report(report).gaps)


def _unknown(requested: list[str], known: list[str], kind: str) -> None:
    """Refuse every requested name the prior run does not record.

    Raises:
        FillGapsBlocked: naming all unknown names at once, plus the known ones to choose from.
    """
    missing = [name for name in requested if name not in known]
    if missing:
        listed = ", ".join(repr(name) for name in missing)
        raise FillGapsBlocked(f"unknown {kind} {listed}; that run records: {', '.join(known) or 'none'}")


def _units(roles: list[str]) -> list[dict]:
    """Group the requested roles into spawn units, merged dimensions sharing one spawn.

    Raises:
        FillGapsBlocked: a role maps to no dimension, or a Codex role needs a bridge plugin that is not available.
    """
    units: dict[str, dict] = {}
    for role in roles:
        dimension = map_role(role)
        if dimension.agent == _BRIDGE_AGENT and (status := bridge_status(Path.home(), Path.cwd())) != "available":
            raise FillGapsBlocked(f"role {role!r} needs bridge@borda-ai-rig, which is {status}")
        unit = units.setdefault(
            dimension.unit, {"unit": dimension.unit, "agent": dimension.agent, "files": [], "roles": []}
        )
        if dimension.file not in unit["files"]:
            unit["files"].append(dimension.file)
        unit["roles"].append(role)
    return list(units.values())


def _settled(records: list[dict], cid: str, data: dict, head: str) -> bool:
    """Tell whether ``cid`` names exactly one CONFIRMED record that passes every record rule on ``head``."""
    matches = [record for record in records if record.get("id") == cid]
    if len(matches) != 1 or matches[0].get("disposition") != "CONFIRMED":
        return False
    try:
        _check_candidate(matches[0], data, head)
    except (_GateRejected, *_MALFORMED):
        return False
    return True


def rows_to_publish(data: dict, findings: list, requested: list[str], head: str) -> list[str]:
    """Pick the named candidates that need only their missing findings row, not a fresh validation.

    A CONFIRMED candidate bound to the reviewed head already carries the parent's proof; what the prior run lost is
    the actionable row. Re-validating it would spend a verifier on settled evidence, so it is carried and its row
    published instead. That holds only for a record the new run may carry verbatim: one that passes every record rule
    and has no open concern entry (which a CONFIRMED candidate may not keep). Any other named candidate, stale or
    non-CONFIRMED included, is re-validated, so its record is replaced instead of carried.

    Args:
        data: The prior run's ``review-validation.json`` envelope.
        findings: The prior run's ``findings.jsonl`` rows.
        requested: Candidate ids named with ``--candidates``.
        head: The prior run's ``head-sha.txt`` string.

    Returns:
        The requested ids, in request order, whose single record is CONFIRMED on ``head``, passes every record rule,
        has no open concern entry and lacks a findings row.

    Examples:
        >>> check = {"method": "m", "result": "r", "revision": "abc", "environment": "e", "limits": []}
        >>> def record(cid, disposition, **extra):
        ...     return {"id": cid, "claim": "c", "revision": "abc", "contract": "k", "trigger": "t",
        ...             "checks": [check], "reason": "r", "disposition": disposition, "counterevidence": [],
        ...             "limits": [], "feasible_checks_complete": True, "missing_capability": "hw", **extra}
        >>> envelope = {"readiness": "blocked", "open_concerns": [{"candidate_id": "V4"}],
        ...             "candidates": [record("V1b", "CONFIRMED"), record("V2", "CONFIRMED"),
        ...                            record("V3", "CONFIRMED", contract=""), record("V4", "CONFIRMED"),
        ...                            record("V13", "UNVERIFIED")]}
        >>> rows_to_publish(envelope, [{"candidate_id": "V2"}], ["V13", "V1b", "V2", "V3", "V4"], "abc")
        ['V1b']
    """
    published = set(_names(findings, "candidate_id"))
    concerned = set(_names(data["open_concerns"], "candidate_id"))
    records = [record for record in data["candidates"] if isinstance(record, dict)]
    return [
        cid for cid in requested if cid not in published and cid not in concerned and _settled(records, cid, data, head)
    ]


def _scope(prior: _Prior, roles: Sequence[str], candidates: Sequence[str]) -> _Scope:
    """Split a request's candidates into re-validated ids and ids that only gain their findings row."""
    publish = rows_to_publish(prior.data, prior.findings, list(candidates), prior.head)
    return _Scope(tuple(roles), tuple(cid for cid in candidates if cid not in publish), tuple(publish))


def _stub_assessment(role: str, outcome: str | None, readiness: str) -> dict:
    """Stand in for a re-run reviewer's record: a completed assessment that fits the aggregate choice.

    Under a null aggregate the re-run reviewer is the completed undecidable one the gate then requires; under a judged
    aggregate it supplies that judgment. A re-run reviewer can return either, so neither stand-in claims more.
    """
    return {
        "role": role,
        "presence": "completed",
        "source_outcome": outcome,
        "source_rating": None if outcome is None else _SOURCE_RATINGS[outcome],
        "readiness": "blocked" if outcome is None else readiness,
        "coverage": _STUB,
        "rationale": _STUB,
        "evidence": [_STUB],
        "limits": [],
    }


def _is_open(record: object) -> bool:
    """Tell whether a candidate record is still open: a mapping whose disposition is null, absent or UNVERIFIED.

    Examples:
        >>> _is_open({"disposition": None}), _is_open({"disposition": "CONFIRMED"}), _is_open(["x"])
        (True, False, False)
        >>> _is_open({"disposition": ["UNVERIFIED"]})
        False
    """
    # Tuple membership compares by equality, so an unhashable disposition cannot raise here.
    return isinstance(record, dict) and record.get("disposition") in (None, "UNVERIFIED")


def _projection(prior: _Prior, scope: _Scope, readiness: str, outcome: str | None) -> tuple[dict, list]:
    """Build the envelope and rows the new run must publish under one aggregate choice.

    Records are excluded by their raw name, never re-derived from a stringified one, so a record whose id or role is
    not a string can never drop out of the projection: it stays carried and the record checks judge it.
    """
    data = prior.data
    assessments = [record for record in data["assessments"] if not _named(record, "role", scope.roles)]
    assessments += [_stub_assessment(role, outcome, readiness) for role in scope.roles]
    candidates = [record for record in data["candidates"] if not _named(record, "id", scope.revalidate)]
    concerns = [record for record in data["open_concerns"] if not _named(record, "candidate_id", scope.revalidate)]
    covered = set(_names(concerns, "candidate_id"))
    open_ids = _names([record for record in candidates if _is_open(record)], "id")
    concerns += [
        {"candidate_id": cid, "owner": _STUB, "next_check": _STUB, "closure": _STUB}
        for cid in open_ids
        if cid not in covered
    ]
    rows = [row for row in prior.findings if not _named(row, "candidate_id", scope.revalidate)]
    rows += [
        {"contract_version": 1, "validation_status": "CONFIRMED", "candidate_id": cid} for cid in scope.publish_rows
    ]
    envelope = {
        "readiness": readiness,
        "source_outcome": outcome,
        "assessments": assessments,
        "candidates": candidates,
        "open_concerns": concerns,
    }
    return envelope, rows


def _outcome_choices(carried: list, rerun: bool) -> list[str | None]:
    """List the aggregate outcomes the new consolidator may write, null first, from the assessments it will publish.

    The consolidator recomputes the aggregate outcome over the merged assessments, so the choice follows what those
    assessments support, never the prior's own aggregate label: a null outcome needs a completed undecidable reviewer
    and a judged outcome needs a reviewer that judged (``find_review_report.supported_outcomes``, the rule the gap
    grader's residual check uses too). A re-run reviewer's stand-in can be either, so any re-run role allows both. An
    empty list means no outcome is supportable without re-running a role.

    Args:
        carried: The prior's assessment records the request carries verbatim.
        rerun: Whether the request re-runs at least one role.

    Returns:
        ``None`` (null outcome) and/or a judged stand-in, in that order.

    Examples:
        >>> judged = {"presence": "completed", "source_outcome": "NEEDS_WORK"}
        >>> undecided = {"presence": "completed", "source_outcome": None}
        >>> missing = {"presence": "missing", "source_outcome": None}
        >>> _outcome_choices([judged], False), _outcome_choices([undecided, missing], False)
        (['NEEDS_WORK'], [None])
        >>> _outcome_choices([missing], False), _outcome_choices([missing], True)
        ([], [None, 'NEEDS_WORK'])
    """
    return [None, _JUDGED] if rerun else supported_outcomes(carried)


def _owners(rows: Sequence[object]) -> str:
    """Quote the candidate ids some rows name, as the opening of a parenthetical; empty when they name none.

    Examples:
        >>> _owners([{"candidate_id": "V1"}, {"candidate_id": "V1"}, {"candidate_id": 5}, "x"]), _owners(["x"])
        ("candidate 'V1'; ", '')
    """
    named = (row.get("candidate_id") for row in rows if isinstance(row, dict))
    owners = dict.fromkeys(repr(owner) for owner in named if isinstance(owner, str))
    return f"candidate {', '.join(owners)}; " if owners else ""


def _carried_row_problems(prior: _Prior, revalidate: Collection[str]) -> list[str]:
    """Name every carried ``findings.jsonl`` row resolve's merge could not use; empty when the carried rows are usable.

    The new run keeps every row of a candidate it does not re-validate verbatim, and ``oss:review`` holds its fresh
    publication to the producer gate, which applies the merge's rules: each row passes ``finding_row_problem`` and no
    two rows share an id (``duplicate_finding_id``). A carried row breaking one publishes a run the producer gate
    refuses, while repairing it fails the carry check, and no aggregate choice changes that. Re-validating the row's
    candidate replaces the row, so each reason quotes the candidates concerned for ``_headline``. A row without a string
    ``id`` is such a row: ``mint_finding_ids.py --carry-from`` keeps only a prior row's string id and mints any other
    row afresh. Rows the new run adds never take a carried id (the same ``--carry-from`` reserves them), and the rows
    the plan adds for ``publish_rows`` are not prior rows, so neither is checked here. Rows are numbered by their
    position in the prior ``findings.jsonl``.

    Examples:
        >>> row = {"id": "f-1", "section": "s", "severity": "high", "title": "t", "change": "code", "author": "qa"}
        >>> rows = [{**row, "candidate_id": cid} for cid in ("V1", "V2", "V3")]
        >>> prior = _Prior("8", Path("r.md"), {}, [rows[0], {**rows[1], "id": 3}, rows[2]], "abc1234", [])
        >>> for reason in _carried_row_problems(prior, ["V2"]):
        ...     print(reason)
        findings.jsonl findings 1 and 3 share id 'f-1' (candidate 'V1', 'V3'; resolve's merge cannot use the rows)
        >>> _carried_row_problems(prior, ["V1"])
        ["findings.jsonl finding 2 field id is int, not text (candidate 'V2'; resolve's merge cannot use the row)"]
        >>> _carried_row_problems(prior, ["V1", "V2"])
        []
    """
    carried = [(n, row) for n, row in enumerate(prior.findings, 1) if not _named(row, "candidate_id", revalidate)]
    problems = [
        f"findings.jsonl {problem.partition(_REMEDY_SEPARATOR)[0]} ({_owners([row])}resolve's merge cannot use the row)"
        for n, row in carried
        if (problem := finding_row_problem(row, n))
    ]
    repeated = duplicate_finding_id([row for _, row in carried])
    if repeated:
        (first, row), (second, other) = (carried[index - 1] for index in repeated)
        problems.append(
            f"findings.jsonl findings {first} and {second} share id {row['id']!r} ({_owners([row, other])}resolve's "
            "merge cannot use the rows)"
        )
    return problems


def _projection_failures(prior: _Prior, roles: Sequence[str], candidates: Sequence[str]) -> list[str]:
    """Return why the request's projection cannot publish, or ``[]`` when it can.

    Every aggregate choice is tried against the strict record rules; when all fail, their reasons are returned. Once one
    passes, the carried rows must still pass the producer gate's row rules (``_carried_row_problems``), checked after
    the strict rules as the producer gate checks them; no aggregate choice changes a row, so a row problem fails them
    all.
    """
    scope = _scope(prior, roles, candidates)
    carried = [record for record in prior.data["assessments"] if not _named(record, "role", scope.roles)]
    outcomes = _outcome_choices(carried, rerun=bool(scope.roles))
    if not outcomes:
        # No loop iteration would run, and an empty reason list reads as a pass.
        return [_NO_OUTCOME]
    reasons = []
    for readiness in _READINESS_CHOICES:
        for outcome in outcomes:
            envelope, rows = _projection(prior, scope, readiness, outcome)
            try:
                _check_assessments(envelope)
                _check_candidates(envelope, rows, prior.head)
            except _GateRejected as exc:
                reasons.append(str(exc))
            except _MALFORMED as exc:
                reasons.append(f"malformed record ({type(exc).__name__}: {exc})")
            else:
                return _carried_row_problems(prior, scope.revalidate)
    return reasons


def _with(roles: Sequence[str], candidates: Sequence[str], subjects: Sequence[_Subject]) -> tuple[list, list]:
    """Extend a request's role and candidate lists with ``subjects``."""
    added_roles = [subject.name for subject in subjects if subject.kind is _Kind.ROLE]
    added_ids = [subject.name for subject in subjects if subject.kind is _Kind.CANDIDATE]
    return [*roles, *added_roles], [*candidates, *added_ids]


def _required(prior: _Prior, roles: Sequence[str], candidates: Sequence[str], extra: list[_Subject]) -> list[_Subject]:
    """Reduce ``extra`` to an irredundant set whose addition still lets the request's projection pass.

    The request plus every ``extra`` subject must already pass. Each subject is dropped, in order, when the request
    still passes without it. Dropping a subject only ever adds records to the projection, so a subject that every
    passing request needs is never dropped, and any superset of the result passes too. Of interchangeable subjects (any
    one re-run reviewer can supply a judged aggregate), the later ones survive.
    """
    kept = list(extra)
    for subject in extra:
        trial = [other for other in kept if other != subject]
        if not _projection_failures(prior, *_with(roles, candidates, trial)):
            kept = trial
    return kept


def _mentions(reason: str, name: str) -> bool:
    """Tell whether a strict reason names ``name`` as a whole token, quoted or bare.

    Examples:
        >>> _mentions("open concern for 'V20' lacks owner", "V2"), _mentions("row: V1b", "V1b")
        (False, True)
    """
    return re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", reason) is not None


def _headline(reasons: list[str], names: Sequence[str]) -> str:
    """Pick the strict reason to report from the failed aggregate choices.

    The reason most choices share wins: one a single readiness choice trips is a label slip the new consolidator would
    avoid, not the carried records' defect. Ties go to a reason naming a listed record, then to the earliest choice.

    Examples:
        >>> _headline(["a 'V2' bad", "under complete", "a 'V2' bad"], ["V2"])
        "a 'V2' bad"
        >>> _headline(["x", "y 'V9'"], ["V9"])
        "y 'V9'"
    """
    return max(dict.fromkeys(reasons), key=lambda r: (reasons.count(r), any(_mentions(r, n) for n in names)))


def _cannot_pass(reason: str, pr: str) -> str:
    """Word a refusal that no addition to the request can repair."""
    return f"carried records cannot pass the publication gate ({reason}); run a full /oss:review {pr}"


def _unrepairable(prior: _Prior) -> list[str]:
    """Name the prior's gaps no request can repair, because they concern no record a rerun could replace."""
    known = set(_names(prior.data["candidates"], "id"))
    return [
        f"{gap.rule}: {gap.detail}" if gap.subject == "-" else f"{gap.rule} {gap.subject}"
        for gap in prior.gaps
        if gap.rule in _UNREPAIRABLE_RULES or (gap.rule == "finding-unbacked" and gap.subject not in known)
    ]


def _stale(prior: _Prior, roles: Sequence[str], candidates: Sequence[str]) -> list[str]:
    """List the candidates a request would carry although they are bound to a revision other than the prior head."""
    revalidate = _scope(prior, roles, candidates).revalidate
    off_head = [r for r in prior.data["candidates"] if isinstance(r, dict) and r.get("revision") != prior.head]
    return [cid for cid in _names(off_head, "id") if cid not in revalidate]


def _carry_refusal(prior: _Prior, roles: Sequence[str], candidates: Sequence[str]) -> str:
    """Say why a request cannot publish a run that passes the gate, or return an empty string when it can.

    Args:
        prior: The prior run.
        roles: Roles the request re-runs.
        candidates: Candidate ids the request names (re-validated, or only gaining their findings row).

    Returns:
        The user-facing refusal, naming the carried records to add when an addition helps; empty when the carried
        projection passes under some aggregate choice.
    """
    broken = _unrepairable(prior)
    if broken:
        return _cannot_pass(", ".join(broken), prior.pr)
    stale = _stale(prior, roles, candidates)
    if stale:
        return (
            f"carried candidate(s) {', '.join(stale)} bound to a revision other than {prior.head}; "
            "add them to --candidates or run a full /oss:review"
        )
    reasons = _projection_failures(prior, roles, candidates)
    if not reasons:
        return ""
    carried = [_Subject(_Kind.ROLE, r) for r in _names(prior.data["assessments"], "role") if r not in roles]
    carried += [_Subject(_Kind.CANDIDATE, c) for c in _names(prior.data["candidates"], "id") if c not in candidates]
    residue = _projection_failures(prior, *_with(roles, candidates, carried))
    if residue:
        return _cannot_pass(_headline(residue, []), prior.pr)
    names = [subject.name for subject in _required(prior, roles, candidates, carried)]
    return (
        f"carried record(s) {', '.join(names)} fail {_headline(reasons, names)}; "
        f"add them to --roles/--candidates or run a full /oss:review {prior.pr}"
    )


def build_plan(request: PlanRequest) -> dict:
    """Validate a fill-gaps request against its prior run and return the plan the review skill follows.

    Args:
        request: PR number, prior report path, requested roles/candidates and the current PR head.

    Returns:
        The plan: prior artifact paths, the reviewed head string, re-run roles, re-validated ``candidates``,
        ``publish_rows`` (named CONFIRMED candidates that only need their findings row; carried, not re-validated),
        spawn units, carried record keys and the number of ``Agent()`` spawns (dimension units plus the consolidator).

    Raises:
        FillGapsBlocked: any check in the module docstring fails; the message names the cause.

    Examples:
        >>> try:
        ...     build_plan(PlanRequest("8", Path("x.md"), [], [], "abc1234"))
        ... except FillGapsBlocked as exc:
        ...     print(exc)
        --fill-gaps needs --roles and/or --candidates
    """
    if not request.roles and not request.candidates:
        raise FillGapsBlocked("--fill-gaps needs --roles and/or --candidates")
    prior = _read_prior(request.report, request.pr)
    known_roles, known_ids = _names(prior.data["assessments"], "role"), _names(prior.data["candidates"], "id")
    _unknown(request.roles, known_roles, "role(s)")
    _unknown(request.candidates, known_ids, "candidate(s)")
    if not request.current_head or not request.current_head.startswith(prior.head):
        raise FillGapsBlocked(
            f"PR head moved: run reviewed {prior.head}, PR head is now {request.current_head or 'unknown'}; "
            f"run a full /oss:review {request.pr} instead"
        )
    refusal = _carry_refusal(prior, request.roles, request.candidates)
    if refusal:
        raise FillGapsBlocked(refusal)
    scope = _scope(prior, request.roles, request.candidates)
    units = _units(request.roles)
    run_dir = request.report.parent
    carried_ids = [cid for cid in known_ids if cid not in scope.revalidate]
    return {
        "pr": request.pr,
        "prior_report": request.report.resolve().as_posix(),
        "prior_validation": (run_dir / "review-validation.json").resolve().as_posix(),
        "prior_findings": (run_dir / "findings.jsonl").resolve().as_posix(),
        "head_sha": prior.head,
        "roles": request.roles,
        "candidates": list(scope.revalidate),
        "publish_rows": list(scope.publish_rows),
        "units": units,
        "carried": {
            "assessments": [role for role in known_roles if role not in request.roles],
            "candidates": carried_ids,
            "open_concerns": [
                concern["candidate_id"]
                for concern in prior.data["open_concerns"]
                if _named(concern, "candidate_id", carried_ids)
            ],
            "findings": [str(row.get("id")) for row in prior.findings if _named(row, "candidate_id", carried_ids)],
        },
        "agent_spawns": sum(unit["agent"] != _BRIDGE_AGENT for unit in units) + 1,
    }


def _runnable(role: str) -> bool:
    """Tell whether ``plan`` could re-run a role: it maps to a dimension, and a Codex role finds the bridge."""
    try:
        _units([role])
    except FillGapsBlocked:
        return False
    return True


def _subject_kind(gap: Gap) -> _Kind | None:
    """Tell which request list a graded gap's subject belongs to, or ``None`` when no request can name it.

    Every ``finding-*`` rule names the candidate whose rows are wrong (missing, duplicated, unmergeable, or backing a
    candidate that is not CONFIRMED); re-validating that candidate replaces all of its rows, so it repairs the gap.
    Whether the subject is a recorded candidate at all is checked by the caller, which drops ``-`` and unknown ids.

    Examples:
        >>> from find_review_report import GapSeverity
        >>> rules = ("finding-duplicate", "finding-unbacked", "concern-unknown-candidate", "envelope-header")
        >>> [getattr(_subject_kind(Gap(rule, GapSeverity.CRITICAL, "V2", "")), "value", None) for rule in rules]
        ['candidate', 'candidate', None, None]
    """
    if gap.rule.startswith("assessment-") or (
        gap.rule == "readiness-mislabel" and gap.detail == COMPLETED_NULL_MISLABEL
    ):
        return _Kind.ROLE
    if gap.rule == "concern-unknown-candidate":
        return None
    if gap.rule == "readiness-mislabel" or gap.rule.startswith(("candidate-", "concern-", "finding-")):
        return _Kind.CANDIDATE
    return None


def _nameable(name: str) -> bool:
    """Tell whether a recorded name survives the comma-separated ``--roles``/``--candidates`` value and its quoting.

    Examples:
        >>> _nameable("QA specialist"), _nameable("V1,V2"), _nameable('say "x"')
        (True, False, False)
    """
    return not _UNSELECTABLE_CHARS & set(name)


def _selectable(prior: _Prior) -> list[_Subject]:
    """List the subjects a targeted rerun of ``prior`` may name, roles first, each kind in gap order."""
    known = {
        _Kind.ROLE: set(_names(prior.data["assessments"], "role")),
        _Kind.CANDIDATE: set(_names(prior.data["candidates"], "id")),
    }
    subjects: dict[_Subject, None] = {}
    for gap in prior.gaps:
        kind = _subject_kind(gap)
        name = gap.subject
        if kind is None or name == "-" or name not in known[kind] or not _nameable(name):
            continue
        if kind is _Kind.CANDIDATE or _runnable(name):
            subjects[_Subject(kind, name)] = None
    return sorted(subjects, key=lambda subject: subject.kind is not _Kind.ROLE)


def _carried_subjects(prior: _Prior, exclude: Collection[_Subject]) -> list[_Subject]:
    """List every other recorded subject a request could name: re-runnable roles, then candidate ids.

    Used when the gap-named subjects alone cannot repair the run, for instance a stray invalid row the grader could not
    attribute to its candidate. Names follow the same rules as gap-named ones.
    """
    roles = [
        _Subject(_Kind.ROLE, r) for r in _names(prior.data["assessments"], "role") if _nameable(r) and _runnable(r)
    ]
    ids = [_Subject(_Kind.CANDIDATE, c) for c in _names(prior.data["candidates"], "id") if _nameable(c)]
    return [subject for subject in [*roles, *ids] if subject not in exclude]


def _triage_refusal(prior: _Prior, subjects: Sequence[_Subject]) -> str:
    """Say why the request naming ``subjects`` cannot publish a run that passes the gate, or return ``""`` when it can.

    The same checks as ``_carry_refusal``, but worded for a caller that has already offered every name it can, so the
    only remedy left is a full review; it never asks for an addition.
    """
    roles, candidates = _with([], [], subjects)
    broken = _unrepairable(prior)
    if broken:
        return _cannot_pass(", ".join(broken), prior.pr)
    stale = _stale(prior, roles, candidates)
    if stale:
        return _cannot_pass(f"candidate(s) {', '.join(stale)} bound to a revision other than {prior.head}", prior.pr)
    reasons = _projection_failures(prior, roles, candidates)
    return _cannot_pass(_headline(reasons, []), prior.pr) if reasons else ""


def _rerun_pool(prior: _Prior, subjects: list[_Subject]) -> list[_Subject]:
    """Return the subjects a rerun may draw from: the gap-named ones when they suffice, else every nameable record.

    Carried records come first, so of interchangeable subjects ``_required`` keeps the gap-named one.
    """
    if subjects and not _triage_refusal(prior, subjects):
        return subjects
    return [*_carried_subjects(prior, subjects), *subjects]


def _unselectable_qa(prior: _Prior) -> list[str]:
    """List the QA or security roles a gap names that no request can name in ``--roles`` or ``plan`` could re-run.

    Their per-PR security scan is mandatory, so a gap naming one is never optional; a rerun that cannot include the
    role would publish without it.

    Examples:
        >>> from find_review_report import GapSeverity
        >>> gaps = [Gap("assessment-not-complete", GapSeverity.CRITICAL, role, "") for role in ("QA, security", "QA")]
        >>> data = {"assessments": [{"role": "QA, security"}, {"role": "QA"}]}
        >>> _unselectable_qa(_Prior("8", Path("r.md"), data, [], "abc1234", gaps))
        ['QA, security']
    """
    recorded = set(_names(prior.data["assessments"], "role"))
    named = [gap.subject for gap in prior.gaps if _subject_kind(gap) is _Kind.ROLE and gap.subject in recorded]
    qa_roles = dict.fromkeys(role for role in named if QA_ROLE_RE.search(role))
    return [role for role in qa_roles if not (_nameable(role) and _runnable(role))]


def _plannable_alone(prior: _Prior) -> list[_Subject]:
    """List every recorded subject a request naming it alone would plan: re-runnable roles, then candidate ids.

    Offered when nothing is required and no gap names a subject, so the empty request's projection already passes: the
    gaps lie outside every record (a header disagreement, an aggregate outcome label), and any rerun publishes a fresh
    header and recomputes the aggregate labels. Each offer is checked on its own rather than assumed.
    """
    return [subject for subject in _carried_subjects(prior, ()) if not _triage_refusal(prior, [subject])]


def _rerun_verdict(prior: _Prior) -> TriageVerdict:
    """Decide the required and optional subjects of a targeted rerun of a loaded prior run, or why none plans.

    A gap naming the QA/security reviewer always makes that role required: its per-PR security scan is mandatory, so
    leaving it optional would let a rerun publish without it even where the carried records would pass. For the same
    reason such a role that no request can name or re-run makes the rerun not admissible.
    """
    unselectable = _unselectable_qa(prior)
    if unselectable:
        reason = (
            f"QA/security role(s) {', '.join(repr(role) for role in unselectable)} named by a gap cannot be named in "
            f"--roles or re-run, and the security scan is mandatory; run a full /oss:review {prior.pr}"
        )
        return TriageVerdict(False, reason, pr=prior.pr)
    subjects = _selectable(prior)
    pool = _rerun_pool(prior, subjects)
    refusal = _triage_refusal(prior, pool)
    if refusal:
        return TriageVerdict(False, refusal, pr=prior.pr)
    forced = [subject for subject in subjects if subject.kind is _Kind.ROLE and QA_ROLE_RE.search(subject.name)]
    reduced = _required(prior, *_with([], [], forced), [subject for subject in pool if subject not in forced])
    required = [*forced, *reduced]
    optional = [subject for subject in subjects if subject not in required]
    if not required and not optional:
        optional = _plannable_alone(prior)
    if not required and not optional:
        reason = (
            f"no recorded role or candidate can be named in a targeted rerun that plans; run a full /oss:review "
            f"{prior.pr}"
        )
        return TriageVerdict(False, reason, pr=prior.pr)
    return TriageVerdict(
        True,
        "",
        required_roles=_named_as(required, _Kind.ROLE),
        required_candidates=_named_as(required, _Kind.CANDIDATE),
        roles=_named_as(optional, _Kind.ROLE),
        candidates=_named_as(optional, _Kind.CANDIDATE),
        pr=prior.pr,
    )


def _header_pr(report: Path) -> str:
    """Return the digits of the report header's ``PR:`` field, or an empty string."""
    try:
        fields = _parse_header_fields(report.read_text(encoding="utf-8", errors="replace").splitlines()) or {}
    except OSError:
        return ""
    match = _HEADER_PR_RE.fullmatch(fields.get("PR", ""))
    return match.group(1) if match else ""


def _not_possible(reason: str, pr: str) -> str:
    """Word a triage refusal reached before any request could be weighed, pointing to a full review of the PR.

    Examples:
        >>> _not_possible("prior run has no head-sha.txt", "8")
        'targeted rerun not possible (prior run has no head-sha.txt); run a full /oss:review 8'
    """
    return f"targeted rerun not possible ({reason}); run a full /oss:review {pr or _UNKNOWN_PR}"


def triage(report: Path, pr: str, head_moved: Callable[[str], bool | None]) -> TriageVerdict:
    """Decide whether resolve may offer a targeted rerun of a gap-graded report, and with which subjects.

    Args:
        report: The gap-graded ``review-report.md``.
        pr: The PR number; empty or non-numeric falls back to the report header's ``PR:`` field.
        head_moved: Called with the PR number once the prior run loads; ``True`` when the PR head moved past the
            run's recorded head, ``None`` when unknown (never blocks: ``plan`` re-checks the real head).

    Returns:
        The verdict, carrying the resolved PR number; every subject list is empty when the rerun is not admissible, and
        its reason then ends with a full-review pointer.

    Examples:
        >>> verdict = triage(Path("no-such-dir/review-report.md"), "", lambda _pr: None)
        >>> verdict.reason, verdict.pr
        ('targeted rerun not possible (no PR number is known); run a full /oss:review <PR>', '')
        >>> verdict = triage(Path("no-such-dir/review-report.md"), "8", lambda _pr: None)
        >>> verdict.reason.endswith("; run a full /oss:review 8"), verdict.pr
        (True, '8')
    """
    pr = pr if _PR_RE.match(pr) else _header_pr(report)
    if not pr:
        return TriageVerdict(False, _not_possible("no PR number is known", ""))
    try:
        prior = _read_prior(report, pr)
    except FillGapsBlocked as exc:
        return TriageVerdict(False, _not_possible(str(exc), pr), pr=pr)
    if head_moved(pr):
        reason = f"PR head moved since the run reviewed {prior.head}; run a full /oss:review {pr}"
        return TriageVerdict(False, reason, pr=pr)
    return _rerun_verdict(prior)


def _named_as(subjects: list[_Subject], kind: _Kind) -> list[str]:
    """List the names of the subjects of one kind, in order."""
    return [subject.name for subject in subjects if subject.kind is kind]


def _summary(plan: dict) -> list[str]:
    """Render the plan as the lines the skill prints before any spawn."""
    lines = [f"fill-gaps: completes {plan['prior_report']} @{plan['head_sha']}"]
    lines += [f"re-run: {', '.join(unit['roles'])} -> {unit['unit']} ({unit['agent']})" for unit in plan["units"]]
    lines.append(f"re-validate: {', '.join(plan['candidates']) or 'none'}")
    lines.append(
        f"publish missing findings row (CONFIRMED, no re-validation): {', '.join(plan['publish_rows']) or 'none'}"
    )
    carried = plan["carried"]
    lines.append(
        f"carried: {len(carried['assessments'])} assessment(s), {len(carried['candidates'])} candidate(s), "
        f"{len(carried['open_concerns'])} open concern(s), {len(carried['findings'])} finding row(s)"
    )
    spawns = plan["agent_spawns"]
    bridge = " + bridge:review" if any(unit["agent"] == _BRIDGE_AGENT for unit in plan["units"]) else ""
    lines.append(
        f"cost: {spawns} agent spawn(s) x ~{SPAWN_TOKENS:,} tok = ~{spawns * SPAWN_TOKENS:,} tok fixed overhead"
        f"{bridge} (+ up to 3 verifiers for critical/blocking candidates)"
    )
    return lines


def _current_head(snapshot: Path) -> str:
    """Return the PR head recorded in the review snapshot, or an empty string when unknown."""
    try:
        return str(json.loads(snapshot.read_text(encoding="utf-8")).get("headRefOid") or "")
    except (OSError, ValueError, AttributeError):
        return ""


def _entries(envelope: dict, key: str) -> list:
    """Return an envelope's record list, or an empty list when the field is absent or not a list."""
    value = envelope.get(key)
    return value if isinstance(value, list) else []


def _carried_problems(carried: dict, prior: dict, prior_rows: list, current: dict, current_rows: list) -> list[str]:
    """Name every carried prior record the new run altered or dropped."""
    roles, ids = carried.get("assessments") or [], carried.get("candidates") or []
    problems = [
        f"carried assessment {record['role']!r} altered or dropped"
        for record in _entries(prior, "assessments")
        if _named(record, "role", roles) and record not in _entries(current, "assessments")
    ]
    problems += [
        f"carried candidate {record['id']!r} altered or dropped"
        for record in _entries(prior, "candidates")
        if _named(record, "id", ids) and record not in _entries(current, "candidates")
    ]
    problems += [
        f"carried open concern for {record['candidate_id']!r} altered or dropped"
        for record in _entries(prior, "open_concerns")
        if _named(record, "candidate_id", ids) and record not in _entries(current, "open_concerns")
    ]
    problems += [
        f"carried finding row {record.get('id')!r} altered or dropped"
        for record in prior_rows
        if _named(record, "candidate_id", ids) and record not in current_rows
    ]
    return problems


def _requested_problems(plan: dict, current: dict, current_rows: list) -> list[str]:
    """Name every re-run role, re-validated candidate or row to publish the new run silently dropped."""
    present_roles = set(_names(_entries(current, "assessments"), "role"))
    problems = [
        f"re-run role {role!r} has no assessment; record presence=missing when its agent returned nothing"
        for role in plan.get("roles") or []
        if role not in present_roles
    ]
    row_ids = set(_names(current_rows, "candidate_id"))
    problems += [
        f"finding-missing candidate {cid!r} still has no findings.jsonl row; publish it"
        for cid in plan.get("publish_rows") or []
        if cid not in row_ids
    ]
    present_ids = set(_names(_entries(current, "candidates"), "id"))
    problems += [
        f"re-validated candidate {cid!r} dropped; retain it with its new disposition"
        for cid in plan.get("candidates") or []
        if cid not in present_ids
    ]
    return problems


def verify_carry(report: Path) -> list[str]:
    """Check a published fill-gaps run kept every carried prior record and every requested name.

    Never raises on the plan's or the sidecars' content: an unreadable file, a non-object envelope or an oddly typed
    record becomes one problem line.

    Args:
        report: The new run's ``review-report.md``; ``fill-gaps.json`` must sit beside it.

    Returns:
        One problem string per violation; empty when the carry-over is intact.

    Examples:
        >>> verify_carry(Path("no-such-run/review-report.md"))
        ['fill-gaps.json beside the report unreadable (FileNotFoundError); not a fill-gaps run']
    """
    try:
        plan = json.loads(report.with_name(PLAN_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as exc:
        return [f"{PLAN_NAME} beside the report unreadable ({type(exc).__name__}); not a fill-gaps run"]
    try:
        prior = json.loads(Path(plan["prior_validation"]).read_text(encoding="utf-8"))
        prior_rows = read_jsonl(Path(plan["prior_findings"]))
        current = json.loads(report.with_name("review-validation.json").read_text(encoding="utf-8"))
        current_rows = read_jsonl(report.with_name("findings.jsonl"))
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
        return [f"prior or new sidecar unreadable ({type(exc).__name__}: {exc})"]
    shapes = {"prior review-validation.json": prior, "new review-validation.json": current, PLAN_NAME: plan}
    not_objects = [name for name, value in shapes.items() if not isinstance(value, dict)]
    if not_objects or not isinstance(plan.get("carried"), dict):
        return [f"{name} is not a JSON object" for name in not_objects or [f"{PLAN_NAME} carried"]]
    try:
        return _carried_problems(plan["carried"], prior, prior_rows, current, current_rows) + _requested_problems(
            plan, current, current_rows
        )
    except _MALFORMED as exc:
        return [f"malformed plan or sidecar record ({type(exc).__name__}: {exc})"]


def _write_outputs(plan_out: Path, head_out: Path, plan: dict | None) -> None:
    """Write the plan and head files, or truncate both when the request was refused."""
    plan_out.write_text(json.dumps(plan, indent=2) + "\n" if plan else "", encoding="utf-8", newline="\n")
    head_out.write_text(plan["head_sha"] + "\n" if plan else "", encoding="utf-8", newline="\n")


def _run_plan(args: argparse.Namespace) -> int:
    """Validate the request, publish the plan, and print its summary or the refusal."""
    request = PlanRequest(
        pr=args.pr,
        report=Path(args.report),
        roles=split_names(args.roles),
        candidates=split_names(args.candidates),
        current_head=_current_head(Path(args.snapshot)),
    )
    try:
        plan = build_plan(request)
    except FillGapsBlocked as exc:
        _write_outputs(Path(args.plan_out), Path(args.head_out), None)
        print(f"! BLOCKED — {exc}")
        return 1
    _write_outputs(Path(args.plan_out), Path(args.head_out), plan)
    print("\n".join(_summary(plan)))
    return 0


def _run_verify(args: argparse.Namespace) -> int:
    """Print every carry-over violation of a published fill-gaps run."""
    problems = verify_carry(Path(args.report))
    for problem in problems:
        print(f"[carry] {problem}")
    if not problems:
        print("[carry] intact")
    return 1 if problems else 0


def _assessment_input(assessment: Path) -> tuple[Path, bool | None]:
    """Read the report path and ``head_moved`` from a saved ``find_review_report.py --assess`` JSON line.

    Raises:
        OSError: the file cannot be read.
        ValueError: it holds no such line (not JSON, no ``report`` path, or a ``head_moved`` that is not a bool/null).
    """
    payload = json.loads(assessment.read_text(encoding="utf-8"))
    path = payload.get("report") if isinstance(payload, dict) else None
    moved = payload.get("head_moved") if isinstance(payload, dict) else None
    if not isinstance(path, str) or not path or not (moved is None or isinstance(moved, bool)):
        raise ValueError("no report path or head_moved of a find_review_report.py --assess line")
    return Path(path), moved


def _run_triage(args: argparse.Namespace) -> int:
    """Print the targeted-rerun verdict as one JSON line; a blocked line only when the report itself is unreadable.

    ``--assessment`` reuses the saved ``--assess`` line, ``head_moved`` included, so no second ``gh`` call is made. An
    unreadable or malformed assessment file is a verdict (not admissible), never a failure of the command: resolve reads
    an empty or non-admissible verdict the same way.
    """
    if not args.assessment:
        report = Path(args.report)
        return _print_triage(report, args.pr, lambda pr: _head_moved(report, pr, args.timeout))
    try:
        report, moved = _assessment_input(Path(args.assessment))
    except (OSError, ValueError, RecursionError) as exc:
        pr = args.pr.strip() if _PR_RE.match(args.pr.strip()) else ""
        cause = f"gap assessment {args.assessment} unreadable or malformed ({type(exc).__name__}: {exc})"
        print(json.dumps(asdict(TriageVerdict(False, _not_possible(cause, pr), pr=pr)), ensure_ascii=False))
        return 0
    return _print_triage(report, args.pr, lambda _pr: moved)


def _print_triage(report: Path, pr: str, head_moved: Callable[[str], bool | None]) -> int:
    """Triage one report and print the verdict line, or a blocked line with exit 1 when the report cannot be read."""
    try:
        verdict = triage(report, pr.strip(), head_moved)
    except (OSError, RecursionError) as exc:
        print(f"! BLOCKED — review report unreadable ({type(exc).__name__}: {exc})")
        return 1
    print(json.dumps(asdict(verdict), ensure_ascii=False))
    return 0


class _Parser(argparse.ArgumentParser):
    """Argument parser that reports a usage error the way this script reports every refusal."""

    def error(self, message: str) -> NoReturn:
        """Report a usage error as a blocked line on stdout and exit 1, instead of argparse's stderr and exit 2."""
        print(f"! BLOCKED — {self.prog}: {message}")
        raise SystemExit(1)


def _build_parser() -> argparse.ArgumentParser:
    """Declare the ``plan``, ``verify`` and ``triage`` subcommands."""
    parser = _Parser(
        prog="fill_gaps_scope.py",
        description="Plan and check an /oss:review --fill-gaps targeted rerun of one published review run.",
    )
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    plan = commands.add_parser("plan", help="validate the request and write the plan before any spawn")
    plan.add_argument("--pr", required=True, help="PR number the review run belongs to.")
    plan.add_argument("--report", required=True, help="Prior run's review-report.md.")
    plan.add_argument("--roles", default="", help="Comma-separated assessment roles to re-run.")
    plan.add_argument("--candidates", default="", help="Comma-separated candidate ids to re-validate.")
    plan.add_argument("--snapshot", required=True, help="Review snapshot pr-meta.json carrying headRefOid.")
    plan.add_argument("--plan-out", required=True, help="Plan JSON destination (truncated on refusal).")
    plan.add_argument("--head-out", required=True, help="Prior head string destination (truncated on refusal).")
    plan.set_defaults(handler=_run_plan)
    verify = commands.add_parser("verify", help="check a published fill-gaps run kept every carried record")
    verify.add_argument("--report", required=True, help="New run's review-report.md with fill-gaps.json beside it.")
    verify.set_defaults(handler=_run_verify)
    triage_command = commands.add_parser("triage", help="print whether resolve may offer a targeted rerun, as JSON")
    source = triage_command.add_mutually_exclusive_group(required=True)
    source.add_argument("--report", help="Gap-graded review-report.md; the PR head is checked with gh.")
    source.add_argument(
        "--assessment",
        help="File holding the find_review_report.py --assess JSON line; its report and head_moved are reused.",
    )
    triage_command.add_argument("--pr", default="", help="PR number; empty or n/a falls back to the header PR field.")
    triage_command.add_argument("--timeout", type=int, default=6, help="Max gh wait in seconds (--report only).")
    triage_command.set_defaults(handler=_run_triage)
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Argument list override for testing. Defaults to ``sys.argv[1:]``.

    Returns:
        Exit code — 0 on a written plan, an intact carry-over or a printed triage verdict, 1 otherwise.
    """
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]
    args = _build_parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
