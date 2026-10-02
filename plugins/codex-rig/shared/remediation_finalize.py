#!/usr/bin/env python3
"""Derive code-remediate bookkeeping artifacts from recorded run state and finalize one result candidate.

## Purpose

Remove hand-copied bookkeeping from remediation runs. Metadata blocks, the workplan document, and final-handoff tables
repeat facts that already exist in machine files; copying them by hand drifts and fails validation. This helper copies
them deterministically so the agent authors only judgement fields.

## Scope

Derived, never authored by hand:

- ``resolution_scope`` selected, deferred, and presentation fields, ``pr_relevance``, and each
  ``final_resolution_table`` item's identity fields from ``selection.json``.
- The ``resolution_workplan`` plan copy, digest, paths, approval fields, execution mode, and group counts from
  ``work-bucket-plan.json`` and ``parallel-approval.json``.
- ``merge_resolution`` path, authorization, conflict flag, and status from ``pr/merge-resolution.json``.
- Final-handoff verification, confidence, artifacts, remediation table, source records, and source coverage from
  ``gates.json`` and the derived metadata.
- Each item's latest outcome fields and each bucket's latest status from the append-only ``resolution-events.jsonl``.
  Growing ledgers (that event file, ``closure-log.md``, and the review run's ``resolution.jsonl``) are only appended,
  never rewritten; rendered tables are rebuilt from them.

Judgement fields stay with the agent: parallel eligibility and approval requirement, item outcomes, closure evidence,
confidence gaps and closures, unresolved summaries, and the handoff outcome, remaining work, next steps, and commit
disposition. The helper never decides a finding, runs gates, changes Git state, or relaxes a validator rule.

## Usage

``metadata --run <run> --metadata <draft.json> --out <derived.json>`` writes merged metadata. ``workplan --run <run>
--metadata <draft.json>`` rewrites the four managed sections of ``resolution-workplan.md``, including each bucket's
latest status, and keeps every other section. ``ledger --run <run> --metadata <draft.json>`` rewrites the resolution
table and source records of ``action-items.md`` from ``selection.json`` identity plus the latest item outcomes.
``append --run <run> --ledger closure-log.md|resolution-events.jsonl`` appends the staged ``<ledger>.rec`` record and
removes it; status events are validated first. ``resolutions --run <run> --review-run <review run> [--sha <commit>]``
appends one outcome per admitted review finding to that review run's ``resolution.jsonl``. ``finalize --run <run>
--metadata <draft.json> --handoff <draft.json> --status <status> --confidence <score> --artifact-path <result path>``
derives metadata and handoff, renders ``final.md``, writes ``result.candidate.json``
through the shared result writer, and runs the shared validator in all-errors mode; ``--promote`` renames a passing
candidate to ``result.json``.

## Used by

The code-remediate skill uses it at work-bucket planning and at result finalization. Code Review uses
``finalize --skill code-review``, which derives only the shared handoff fields (verification, confidence, result
artifact), runs the review-specific validator before the shared one, and leaves completion to
``find-review-report.py``. Remediation finalization tests compare its derived fields with hand-built valid fixtures.

## Outputs

``finalize`` prints one JSON summary: overall status, each step's status, every validator error with its repair hint,
the candidate path, and whether it was promoted. Other actions write their named file and print a short JSON status.

## Failure

Missing or malformed machine files, a plan digest that differs from its approval record, a parent-only workflow-default
plan without an ineligibility reason, a render error, a result-writer rejection, or any validator error exits ``1``
with the failing step named. A failed run never promotes the candidate.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Any

SHARED_DIRECTORY = Path(__file__).resolve().parent
if str(SHARED_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SHARED_DIRECTORY))

import final_handoff  # noqa: E402

IDENTITY_FIELDS = ("input_item_id", "item_name", "item_type", "severity", "selectable", "sources")
APPROVAL_STATUS = {"approve": "approved", "parent-only": "parent-only", "not-required": "not-required"}
LAYOUTS = {2: "grouped", 3: "concise", 4: "concise"}
GATE_IDS = ("lint", "format", "types", "tests", "review")
MANAGED_SECTIONS = ("Work Bucket Plan", "Parallel Approval", "Execution Order", "Ungrouped Items")
LEDGER_SECTIONS = ("Review Item Resolution Table", "Expanded Source Records")
EVENTS_LEDGER = "resolution-events.jsonl"
CLOSURE_LEDGER = "closure-log.md"
APPEND_LEDGERS = (CLOSURE_LEDGER, EVENTS_LEDGER)
REVIEW_RESOLUTION_LEDGER = "resolution.jsonl"
EVENT_SCHEMA_VERSION = 1
RESOLUTION_SCHEMA_VERSION = 1
BUCKET_STATUSES = frozenset({"planned", "in-progress", "fixed", "verified", "deferred", "unresolved"})
ITEM_EVENT_ENUMS = {
    # ``stale`` stays readable in historical results but is never a new disposition.
    "triage_status": frozenset(
        {
            "valid",
            "resolved",
            "duplicate",
            "out-of-scope",
            "already-fixed",
            "already-applied",
            "needs-clarification",
        }
    ),
    "resolution_status": frozenset(
        {
            "implemented",
            "resolved",
            "rejected",
            "not-applicable",
            "duplicate",
            "already-fixed",
            "already-applied",
            "needs-clarification",
            "unresolved",
        }
    ),
    "owner_status": frozenset(
        {"todo", "fixed", "resolved", "deferred", "unresolved", "not-selected", "not-actionable"}
    ),
    "pr_relation": frozenset({"direct-diff", "pr-intent", "adjacent", "unknown", "unrelated"}),
}
ITEM_EVENT_TEXT_FIELDS = ("resolved_how", "evidence")
ITEM_OUTCOME_FIELDS = ("triage_status", "resolution_status", "owner_status", "resolved_how", "evidence")
FIXED_RESOLUTIONS = frozenset({"implemented", "resolved", "already-fixed", "already-applied"})
REJECTED_RESOLUTIONS = frozenset({"rejected", "not-applicable", "duplicate", "stale"})
DEFERRED_OWNERS = frozenset({"deferred", "not-selected"})
COMMIT_PATTERN = re.compile(r"[0-9a-f]{7,64}")


class DeriveError(Exception):
    """Report a machine-state problem that prevents deterministic derivation."""


def _load_module(name: str, filename: str) -> ModuleType:
    """Load one hyphenated sibling helper by file path."""
    spec = importlib.util.spec_from_file_location(name, SHARED_DIRECTORY / filename)
    if spec is None or spec.loader is None:
        raise DeriveError(f"helper-unavailable:{filename}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load(path: Path) -> dict[str, Any]:
    """Load one required JSON object."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DeriveError(f"unreadable-json:{path.name}") from error
    if not isinstance(payload, dict):
        raise DeriveError(f"json-not-object:{path.name}")
    return payload


def _write(path: Path, payload: dict[str, Any]) -> None:
    """Write deterministic UTF-8 JSON with LF line endings."""
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")


def _append_text(path: Path, text: str) -> None:
    """Append text to a growing ledger without reading or rewriting its existing bytes."""
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(text)


def _json_line(record: dict[str, Any]) -> str:
    """Serialize one ledger record as a single sorted JSON line."""
    return json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"


def _known_identities(run: Path) -> tuple[set[str], set[str]]:
    """Return the item IDs from ``selection.json`` and bucket IDs from ``work-bucket-plan.json`` events may name."""
    items: set[str] = set()
    buckets: set[str] = set()
    if (run / "selection.json").is_file():
        inventory = _load(run / "selection.json").get("items")
        items = {item.get("input_item_id") for item in inventory or [] if isinstance(item, dict)}
    if (run / "work-bucket-plan.json").is_file():
        plan = _load(run / "work-bucket-plan.json").get("work_buckets")
        buckets = {bucket.get("bucket_id") for bucket in plan or [] if isinstance(bucket, dict)}
    return items, buckets


def validate_event(event: object, items: set[str], buckets: set[str]) -> dict[str, Any]:
    """Check one status event against its closed vocabulary and the run's known identities.

    A bucket event carries ``status`` and an optional ``note``; an item event carries any subset of the outcome fields
    the resolution table shows. Unknown keys, blank text, retired statuses, and IDs absent from the frozen selection or
    bucket plan are rejected so a typo cannot silently create a phantom row.

    Example:
        >>> validate_event({"kind": "bucket", "id": "B1", "status": "fixed"}, set(), {"B1"})["schema_version"]
        1
    """
    if not isinstance(event, dict):
        raise DeriveError("event-not-object")
    event = {"schema_version": EVENT_SCHEMA_VERSION, **event}
    kind, identity = event.get("kind"), event.get("id")
    if event["schema_version"] != EVENT_SCHEMA_VERSION or not isinstance(identity, str) or not identity.strip():
        raise DeriveError("event-identity-invalid")
    if kind == "bucket":
        allowed = {"schema_version", "kind", "id", "status", "note"}
        if identity not in buckets:
            raise DeriveError(f"event-unknown-bucket:{identity}")
        if event.get("status") not in BUCKET_STATUSES:
            raise DeriveError(f"event-bucket-status-invalid:{identity}")
    elif kind == "item":
        allowed = {"schema_version", "kind", "id", *ITEM_EVENT_ENUMS, *ITEM_EVENT_TEXT_FIELDS}
        if identity not in items:
            raise DeriveError(f"event-unknown-item:{identity}")
        if not set(event) & {*ITEM_EVENT_ENUMS, *ITEM_EVENT_TEXT_FIELDS}:
            raise DeriveError(f"event-item-empty:{identity}")
        for field, values in ITEM_EVENT_ENUMS.items():
            if field in event and event[field] not in values:
                raise DeriveError(f"event-item-{field}-invalid:{identity}")
    else:
        raise DeriveError("event-kind-invalid")
    if set(event) - allowed:
        raise DeriveError(f"event-field-unknown:{identity}:{','.join(sorted(set(event) - allowed))}")
    for field in ("note", *ITEM_EVENT_TEXT_FIELDS):
        if field in event and (not isinstance(event[field], str) or not event[field].strip()):
            raise DeriveError(f"event-{field}-blank:{identity}")
    return event


def read_events(run: Path) -> list[dict[str, Any]]:
    """Read the run's status-event ledger in append order; an absent ledger has no events."""
    path = run / EVENTS_LEDGER
    if not path.is_file():
        return []
    items, buckets = _known_identities(run)
    events = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            # A hand-edited line must fail with a code, never reach folding as a partial record.
            events.append(validate_event(json.loads(line), items, buckets))
        except (json.JSONDecodeError, DeriveError) as error:
            raise DeriveError(f"events-ledger-invalid:{number}") from error
    return events


def fold_events(events: list[dict[str, Any]]) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """Reduce the append-order ledger to the latest bucket status and latest value of each item field.

    Example:
        >>> fold_events([{"kind": "bucket", "id": "B1", "status": "in-progress"},
        ...              {"kind": "bucket", "id": "B1", "status": "fixed"},
        ...              {"kind": "item", "id": "F1", "owner_status": "todo"},
        ...              {"kind": "item", "id": "F1", "owner_status": "fixed", "evidence": "closure-log.md"}])
        ({'B1': 'fixed'}, {'F1': {'owner_status': 'fixed', 'evidence': 'closure-log.md'}})
    """
    buckets: dict[str, str] = {}
    items: dict[str, dict[str, str]] = {}
    for event in events:
        if event.get("kind") == "bucket":
            buckets[event["id"]] = event["status"]
        elif event.get("kind") == "item":
            fields = items.setdefault(event["id"], {})
            fields.update({key: value for key, value in event.items() if key not in {"schema_version", "kind", "id"}})
    return buckets, items


def append_record(run: Path, ledger: str, record: Path | None = None) -> dict[str, Any]:
    """Append one staged record file to a run ledger, then remove the staged file.

    The model writes the record to ``<ledger>.rec`` with its file tool; this action appends those bytes, so earlier
    entries are never reread into a rewrite. ``closure-log.md`` records are Markdown blocks; the file receives its ``##
    Closure Evidence`` heading on first append. ``resolution-events.jsonl`` records are one JSON object or a list of
    objects, each validated before anything is appended.
    """
    if ledger not in APPEND_LEDGERS:
        raise DeriveError(f"append-ledger-unsupported:{ledger}")
    target = run / ledger
    staged = record or run / f"{ledger}.rec"
    try:
        text = staged.read_text(encoding="utf-8")
    except OSError as error:
        raise DeriveError(f"append-record-missing:{staged.name}") from error
    if not text.strip():
        raise DeriveError(f"append-record-empty:{staged.name}")
    if ledger == EVENTS_LEDGER:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as error:
            raise DeriveError("append-record-invalid-json") from error
        items, buckets = _known_identities(run)
        events = [
            validate_event(event, items, buckets) for event in (payload if isinstance(payload, list) else [payload])
        ]
        _append_text(target, "".join(_json_line(event) for event in events))
        appended = len(events)
    else:
        prefix = "" if target.is_file() else "## Closure Evidence\n"
        _append_text(target, f"{prefix}\n{text.strip()}\n")
        appended = 1
    staged.unlink()
    return {"status": "pass", "ledger": str(target), "appended": appended}


def derive_selection(run: Path, metadata: dict[str, Any]) -> None:
    """Copy scope, relevance, and item identity from the frozen selection inventory.

    Example:
        >>> import tempfile
        >>> run = Path(tempfile.mkdtemp())
        >>> _ = (run / "selection.json").write_text(json.dumps({"selected_indexes": [1], "items": [
        ...     {"input_item_id": "R1", "item_name": "n", "item_type": "code", "severity": "high",
        ...      "selectable": True, "sources": []}]}))
        >>> metadata = {}
        >>> derive_selection(run, metadata)
        >>> item = metadata["final_resolution_table"]["items"][0]
        >>> metadata["resolution_scope"]["deferred_indexes"], item["item_name"]
        ([], 'n')
    """
    inventory = _load(run / "selection.json")
    items = inventory.get("items")
    selected = inventory.get("selected_indexes")
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise DeriveError("selection-items-invalid")
    if not isinstance(selected, list):
        raise DeriveError("selection-not-confirmed")
    selectable_total = sum(1 for item in items if item.get("selectable"))
    scope = metadata.setdefault("resolution_scope", {})
    scope["selected_indexes"] = selected
    scope["deferred_indexes"] = [index for index in range(1, selectable_total + 1) if index not in selected]
    scope["presentation_version"] = inventory.get("presentation_version", 2)
    if "pr_relevance" in inventory:
        metadata["pr_relevance"] = inventory["pr_relevance"]
    table = metadata.setdefault("final_resolution_table", {})
    existing = {item.get("input_item_id"): item for item in table.get("items", []) or [] if isinstance(item, dict)}
    table["items"] = [
        {**existing.get(item.get("input_item_id"), {}), **{field: item.get(field) for field in IDENTITY_FIELDS}}
        for item in items
    ]


def _execution_mode(buckets: list[dict[str, Any]]) -> str:
    """Name the plan's execution mode from its bucket modes and owners."""
    if any(bucket.get("execution_mode") == "parallel" for bucket in buckets):
        return "parallel-specialists"
    if any(bucket.get("owner") != "parent" for bucket in buckets):
        return "sequential-specialists"
    return "parent-owned"


def _group_counts(buckets: list[dict[str, Any]], selected: list[int]) -> dict[str, int]:
    """Count groups by owner and verifier and selected items left outside every bucket."""
    covered = {index for bucket in buckets for index in bucket.get("selected_indexes", [])}
    parent_groups = sum(1 for bucket in buckets if bucket.get("owner") == "parent")
    return {
        "groups_total": len(buckets),
        "parent_owned_groups": parent_groups,
        "specialist_owned_groups": len(buckets) - parent_groups,
        "verifier_groups": sum(1 for bucket in buckets if bucket.get("verifier") != "none"),
        "unassigned_selected_items": len(set(selected) - covered),
    }


def derive_workplan(run: Path, metadata: dict[str, Any]) -> None:
    """Copy the frozen bucket plan, its digest, and its dispatch record into workplan metadata."""
    plan_path = run / "work-bucket-plan.json"
    if not plan_path.is_file():
        return
    plan = _load(plan_path)
    approval = _load(run / "parallel-approval.json")
    buckets = plan.get("work_buckets")
    if not isinstance(buckets, list) or not all(isinstance(bucket, dict) for bucket in buckets):
        raise DeriveError("work-bucket-plan-buckets-invalid")
    digest = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    if approval.get("plan_sha256") != digest:
        raise DeriveError("parallel-approval-plan-digest-mismatch")
    response = approval.get("response")
    if response not in APPROVAL_STATUS:
        raise DeriveError("parallel-approval-response-invalid")
    selected = metadata.get("resolution_scope", {}).get("selected_indexes") or []
    workplan = metadata.setdefault("resolution_workplan", {})
    workplan.update(
        {
            "work_buckets": buckets,
            "workplan_path": "resolution-workplan.md",
            "bucket_plan_path": "work-bucket-plan.json",
            "bucket_plan_sha256": digest,
            "parallel_approval_path": "parallel-approval.json",
            "parallel_approval_response": response,
            "parallel_approval_source": approval.get("source"),
            "parallel_prompt_presented": approval.get("prompt_presented"),
            "parallel_approval_status": APPROVAL_STATUS[response],
            "approved_plan_sha256": digest if response == "approve" else None,
            "execution_mode": _execution_mode(buckets),
            "max_items_per_bucket": 5,
            **_group_counts(buckets, selected),
        }
    )


def derive_merge(run: Path, metadata: dict[str, Any]) -> None:
    """Copy the recorded target-merge outcome and its resolved artifact path."""
    path = run / "pr" / "merge-resolution.json"
    if not path.is_file():
        return
    resolution = _load(path)
    summary = metadata.setdefault("merge_resolution", {})
    summary["artifact_path"] = str(path.resolve())
    for field in ("authorization", "conflicts_detected", "status"):
        summary[field] = resolution.get(field)


def derive_events(run: Path, metadata: dict[str, Any]) -> None:
    """Overlay the latest appended item outcomes onto the resolution items; no ledger leaves items unchanged."""
    _, item_fields = fold_events(read_events(run))
    if not item_fields:
        return
    for item in metadata.get("final_resolution_table", {}).get("items", []) or []:
        fields = item_fields.get(item.get("input_item_id"), {})
        item.update({field: fields[field] for field in ITEM_OUTCOME_FIELDS if field in fields})


def derive_metadata(run: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    """Merge every derivable metadata field into an agent draft, overwriting stale copies."""
    derive_selection(run, metadata)
    derive_events(run, metadata)
    derive_workplan(run, metadata)
    derive_merge(run, metadata)
    return metadata


def _workplan_table(buckets: list[dict[str, Any]], statuses: dict[str, str]) -> list[str]:
    """Render the bucket table with the columns the workplan validator reads and each bucket's latest status."""
    lines = [
        "| Bucket | Selected indexes | Owner | Verifier | Context pack | Owned paths | Mode | Closure | Status |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for bucket in buckets:
        indexes = ", ".join(str(index) for index in bucket.get("selected_indexes", []))
        paths = ", ".join(f"`{path}`" for path in bucket.get("owned_paths", []))
        closure = bucket.get("expected_closure") or "closure-log.md"
        status = statuses.get(bucket.get("bucket_id"), "planned")
        lines.append(
            f"| {bucket.get('bucket_id')} | {indexes} | {bucket.get('owner')} | {bucket.get('verifier')} | "
            f"`{bucket.get('context_pack_path')}` | {paths} | {bucket.get('execution_mode')} | {closure} | {status} |"
        )
    return lines


def _approval_lines(workplan: dict[str, Any], reason: str | None) -> list[str]:
    """Render the dispatch record lines, requiring a reason for a default parent-only plan."""
    lines = [
        f"Approval source: {workplan['parallel_approval_source']}.",
        f"Approval response: {workplan['parallel_approval_response']}.",
        f"Prompt presented: {str(workplan['parallel_prompt_presented']).lower()}.",
        f"Plan SHA-256: {workplan['bucket_plan_sha256']}.",
    ]
    default_fallback = (
        workplan["parallel_approval_source"] == "workflow-default"
        and workplan["parallel_approval_response"] == "parent-only"
    )
    if default_fallback and not (reason and reason.strip()):
        raise DeriveError("ineligibility-reason-required")
    if reason and reason.strip():
        lines.append(f"Ineligibility reason: {reason.strip()}")
    return lines


def _unmanaged_sections(path: Path, managed: tuple[str, ...], title: str | None) -> tuple[str | None, list[str]]:
    """Return the existing title and every level-two section the helper does not own."""
    if not path.is_file():
        return title, []
    sections: list[list[str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("# ") and not sections:
            title = line
        elif line.startswith("## "):
            sections.append([line])
        elif sections:
            sections[-1].append(line)
    kept = [section for section in sections if section[0][3:].strip() not in managed]
    return title, ["\n".join(section).rstrip() for section in kept]


def _recorded_reason(path: Path) -> str | None:
    """Reuse the one ineligibility reason already rendered, so a status refresh need not restate it."""
    if not path.is_file():
        return None
    section = re.search(r"(?ms)^## Parallel Approval[ \t]*\n(.*?)(?=^## |\Z)", path.read_text(encoding="utf-8"))
    reasons = re.findall(r"(?m)^Ineligibility reason:[ \t]*(\S[^\n]*)$", section.group(1) if section else "")
    return reasons[0].strip() if len(reasons) == 1 else None


def render_workplan(run: Path, metadata: dict[str, Any], reason: str | None) -> Path:
    """Rewrite the managed workplan sections from derived metadata and keep agent-owned sections.

    Bucket status comes from the latest ``bucket`` event in ``resolution-events.jsonl``; it lives only in this rendered
    table, never in the digest-bound plan JSON or its metadata copy.
    """
    derive_metadata(run, metadata)
    workplan = metadata.get("resolution_workplan")
    if not isinstance(workplan, dict) or "work_buckets" not in workplan:
        raise DeriveError("work-bucket-plan-missing")
    path = run / "resolution-workplan.md"
    reason = reason if reason and reason.strip() else _recorded_reason(path)
    title, kept = _unmanaged_sections(path, MANAGED_SECTIONS, "# Resolution Workplan")
    buckets = workplan["work_buckets"]
    statuses, _ = fold_events(read_events(run))
    order = [
        f"{position}. {bucket.get('bucket_id')} — {bucket.get('execution_mode')}"
        for position, bucket in enumerate(buckets, 1)
    ]
    blocks = [
        title,
        "\n".join(["## Work Bucket Plan", "", *_workplan_table(buckets, statuses)]),
        "\n".join(["## Parallel Approval", "", *_approval_lines(workplan, reason)]),
        "\n".join(["## Execution Order", "", f"Execution mode: {workplan['execution_mode']}.", "", *order]),
        "## Ungrouped Items\n\nnone",
        *kept,
    ]
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8", newline="\n")
    return path


def _cell(value: object) -> str:
    """Keep one table cell on one line and escape the pipe the table parser splits on."""
    return str(value).replace("\r\n", " ").replace("\n", " ").replace("|", "\\|")


def _one_line(value: object) -> str:
    """Collapse a detail definition to the single line the validator compares."""
    return " ".join(str(value).split())


def _ledger_row(position: int, index: str, item: dict[str, Any], extra: dict[str, Any], relation: str) -> str:
    """Render one resolution-table row; outcome text lives in the O/E definitions below the table."""
    sources = item.get("sources") or []
    cells = [
        index,
        item["input_item_id"],
        item["item_name"],
        item["item_type"],
        " ".join(f"{source.get('kind')} [{source.get('source_id')}]" for source in sources),
        "; ".join(dict.fromkeys(str(source.get("location")) for source in sources)),
        ", ".join(dict.fromkeys(str(source.get("kind")) for source in sources)),
        "; ".join(dict.fromkeys(str(source.get("evidence")) for source in sources)),
        relation,
        item["severity"],
        extra.get("summary") or "-",
        item["triage_status"],
        item["resolution_status"],
        item["owner_status"],
        f"[O{position}]",
        f"[E{position}]",
    ]
    return "| " + " | ".join(_cell(cell) for cell in cells) + " |"


def render_ledger(run: Path, metadata: dict[str, Any]) -> Path:
    """Rewrite the resolution table and source records of ``action-items.md`` from selection identity and events.

    Item outcomes come from the latest appended ``item`` events, falling back to the metadata draft; a missing outcome
    field fails instead of rendering an empty cell. Every other section, including the summary, completeness counts,
    review-report intake, and expanded item records, stays agent-owned and unchanged.
    """
    derive_metadata(run, metadata)
    items = metadata["final_resolution_table"]["items"]
    for item in items:
        missing = [field for field in ITEM_OUTCOME_FIELDS if not isinstance(item.get(field), str) or not item[field]]
        if missing:
            raise DeriveError(f"ledger-item-outcome-missing:{item.get('input_item_id')}:{','.join(missing)}")
    extras = {item.get("input_item_id"): item for item in _load(run / "selection.json").get("items", [])}
    _, item_fields = fold_events(read_events(run))
    header = [
        "Selection index",
        "Input item",
        "Item name",
        "Item type",
        "Sources",
        "Item id or source location",
        "Source category",
        "Fetched evidence path",
        "PR/diff relation",
        "Severity",
        "Summary",
        "Triage status",
        "Resolution",
        "Owner/status",
        "Resolved how",
        "Evidence",
    ]
    rows, details, records = [], [], []
    selectable = 0
    for position, item in enumerate(items, 1):
        selectable += bool(item.get("selectable"))
        index = str(selectable) if item.get("selectable") else "-"
        relation = item_fields.get(item["input_item_id"], {}).get("pr_relation", "-")
        rows.append(_ledger_row(position, index, item, extras.get(item["input_item_id"], {}), relation))
        details.extend(
            (f"[O{position}] {_one_line(item['resolved_how'])}", f"[E{position}] {_one_line(item['evidence'])}")
        )
        records.extend(
            f"- {source.get('kind')} [{source.get('source_id')}] @ {_one_line(source.get('location'))} — "
            f"{_one_line(source.get('body'))} — {_one_line(source.get('evidence'))}"
            for source in item.get("sources") or []
        )
    path = run / "action-items.md"
    title, kept = _unmanaged_sections(path, LEDGER_SECTIONS, None)
    table = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |", *rows]
    blocks = [
        *([title] if title else []),
        "\n".join(["## Review Item Resolution Table", "", *table, "", *details]),
        "\n".join(["## Expanded Source Records", "", *records]),
        *kept,
    ]
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8", newline="\n")
    return path


def _report_finding_ids(item: dict[str, Any]) -> list[str]:
    """List the canonical review finding IDs an item's report sources name, in source order."""
    identities: list[str] = []
    for source in item.get("sources") or []:
        if not isinstance(source, dict) or source.get("kind") != "report":
            continue
        source_id = str(source.get("source_id", ""))
        identity = source.get("finding_id") or (source_id.rpartition("#")[2] if "#" in source_id else "")
        if identity and identity not in identities:
            identities.append(identity)
    return identities


def resolution_verdict(item: dict[str, Any]) -> str:
    """Map one item's recorded outcome to the review feedback verdict.

    Examples:
        >>> resolution_verdict({"resolution_status": "implemented", "owner_status": "fixed"})
        'fixed'
        >>> resolution_verdict({"resolution_status": "unresolved", "owner_status": "not-selected"})
        'deferred'
        >>> resolution_verdict({"resolution_status": "duplicate", "owner_status": "resolved"})
        'rejected'
        >>> resolution_verdict({"resolution_status": "unresolved", "owner_status": "unresolved"})
        'skipped'
    """
    if item.get("owner_status") in DEFERRED_OWNERS:
        return "deferred"
    if item.get("resolution_status") in FIXED_RESOLUTIONS:
        return "fixed"
    if item.get("resolution_status") in REJECTED_RESOLUTIONS:
        return "rejected"
    return "skipped"


def _admitted_review(review_run: Path, run: Path) -> dict[str, Any]:
    """Return the review result whose exact bytes this remediation admitted as ``findings-input.txt``."""
    try:
        admitted = (run / "findings-input.txt").read_bytes()
    except OSError as error:
        raise DeriveError("resolution-findings-input-missing") from error
    for name in ("result.json", "result.candidate.json"):
        candidate = review_run / name
        if candidate.is_file() and candidate.read_bytes() == admitted:
            return json.loads(admitted)
    raise DeriveError("resolution-review-run-mismatch")


def _item_commits(pairs: list[str], default: str | None) -> tuple[dict[str, str], str | None]:
    """Parse ``ITEM=SHA`` commit overrides and validate every commit identifier."""
    commits: dict[str, str] = {}
    for pair in pairs:
        item, separator, commit = pair.partition("=")
        if not separator or not item or COMMIT_PATTERN.fullmatch(commit) is None:
            raise DeriveError(f"resolution-item-sha-invalid:{pair}")
        commits[item] = commit
    if default is not None and COMMIT_PATTERN.fullmatch(default) is None:
        raise DeriveError("resolution-sha-invalid")
    return commits, default


def append_review_resolutions(
    run: Path, review_run: Path, sha: str | None = None, item_shas: list[str] | None = None
) -> dict[str, Any]:
    """Append one feedback record per admitted review finding to that review run's ``resolution.jsonl``.

    Records come from this remediation's promoted ``result.json``: the item that owns each report finding supplies the
    verdict (``fixed``, ``rejected``, ``skipped`` or ``deferred``) and its ``resolved_how`` text. ``sha`` names the
    commit holding the fix, or ``null`` while changes stay unstaged. The target must be the exact review this run
    admitted, so feedback can never land on an unrelated report. An identical record already present is not appended
    twice, which keeps a resumed run from duplicating lines; a later different outcome is appended and supersedes it.
    """
    run, review_run = run.resolve(), review_run.resolve()
    result_path = run / "result.json"
    if not result_path.is_file():
        raise DeriveError("resolution-result-not-promoted")
    review = _admitted_review(review_run, run)
    review_metadata = review.get("metadata") if isinstance(review.get("metadata"), dict) else {}
    known = {
        record.get("id")
        for field in ("review_findings", "operational_blockers")
        for record in review_metadata.get(field) or []
        if isinstance(record, dict)
    }
    commits, default = _item_commits(item_shas or [], sha)
    ledger = review_run / REVIEW_RESOLUTION_LEDGER
    existing = set(ledger.read_text(encoding="utf-8").splitlines()) if ledger.is_file() else set()
    items = _load(result_path).get("metadata", {}).get("final_resolution_table", {}).get("items") or []
    lines, unmatched, seen = [], [], set()
    for item in items:
        for finding_id in _report_finding_ids(item):
            if finding_id not in known:
                unmatched.append(finding_id)
                continue
            if finding_id in seen:
                continue
            seen.add(finding_id)
            record = {
                "schema_version": RESOLUTION_SCHEMA_VERSION,
                "finding_id": finding_id,
                "item_id": item.get("input_item_id"),
                "verdict": resolution_verdict(item),
                "sha": commits.get(item.get("input_item_id"), default),
                "why": _one_line(item.get("resolved_how") or ""),
                "remediation_run": run.name,
            }
            line = _json_line(record)
            if line.rstrip("\n") not in existing:
                lines.append(line)
    if lines:
        _append_text(ledger, "".join(lines))
    return {
        "status": "pass",
        "ledger": str(ledger),
        "appended": len(lines),
        "already_recorded": len(seen) - len(lines),
        "unmatched_finding_ids": sorted(set(unmatched)),
    }


def _confidence_block(metadata: dict[str, Any], score: float) -> dict[str, Any]:
    """Copy confidence gaps, closures, and remaining limits into the handoff shape."""
    closures = {entry.get("gap"): entry for entry in metadata.get("confidence_gap_closures", []) or []}
    gaps = []
    for gap in metadata.get("confidence_gaps", []) or []:
        closure = closures.get(gap, {})
        status = closure.get("status")
        detail_key = "evidence" if status == "closed" else "rationale"
        gaps.append({"gap": gap, "status": status, detail_key: closure.get(detail_key) or closure.get("evidence_path")})
    recovery = metadata.get("confidence_recovery") or {}
    return {
        "score": score,
        "band": final_handoff._expected_band(score),
        "gaps": gaps,
        "limits": recovery.get("remaining_limits"),
    }


def _artifacts(existing: list[Any], artifact_path: str) -> list[dict[str, str]]:
    """Bind the result and its action-item ledger while keeping other labelled artifacts."""
    ledger = str(PurePosixPath(artifact_path.replace("\\", "/")).with_name("action-items.md"))
    kept = [
        artifact
        for artifact in existing
        if isinstance(artifact, dict)
        and artifact.get("label") not in {"Result", "Action items"}
        and PurePosixPath(str(artifact.get("path", "")).replace("\\", "/")).name not in {"action-items.md"}
        and artifact.get("path") != artifact_path
    ]
    return [{"label": "Result", "path": artifact_path}, {"label": "Action items", "path": ledger}, *kept]


def _result_artifact(existing: list[Any], artifact_path: str) -> list[dict[str, str]]:
    """Bind the result path while keeping every other labelled artifact."""
    kept = [
        artifact
        for artifact in existing
        if isinstance(artifact, dict) and artifact.get("label") != "Result" and artifact.get("path") != artifact_path
    ]
    return [{"label": "Result", "path": artifact_path}, *kept]


def _resolution_table(metadata: dict[str, Any], draft: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Generate remediation rows, detail notes, and source records from the resolution items."""
    rows, details, records = [], [], []
    for position, item in enumerate(metadata["final_resolution_table"]["items"], start=1):
        sources = item.get("sources") or []
        source_ids = [f"{source.get('kind')}:{source.get('source_id')}" for source in sources]
        rows.append(
            {
                "id": item.get("input_item_id"),
                "cells": [
                    item.get("input_item_id"),
                    item.get("severity"),
                    item.get("item_name"),
                    "\n".join(f"{source.get('kind')} [{source.get('source_id')}]" for source in sources),
                    f"{item.get('resolution_status')} — [O{position}]",
                    f"[E{position}] — owner/status: {item.get('owner_status')}",
                ],
                "source_ids": source_ids,
            }
        )
        details.extend(
            (
                {"id": f"O{position}", "text": item.get("resolved_how")},
                {"id": f"E{position}", "text": item.get("evidence")},
            )
        )
        records.extend(
            {"id": source_id, "evidence": source.get("evidence")} for source_id, source in zip(source_ids, sources)
        )
    table = {
        **draft,
        "columns": list(final_handoff.STANDARD_COLUMNS["code-remediate"]),
        "rows": rows,
        "details": details,
    }
    table.setdefault("heading", "Remediation results")
    presentation = metadata.get("resolution_scope", {}).get("presentation_version")
    if presentation in LAYOUTS:
        table["layout"] = LAYOUTS[presentation]
    if presentation in {3, 4}:
        table["overview_only"] = True
    return table, records


def derive_handoff(
    run: Path, metadata: dict[str, Any], handoff: dict[str, Any], score: float, artifact_path: str
) -> dict[str, Any]:
    """Fill every handoff field that copies gates, confidence metadata, artifacts, or resolution items."""
    gates = _load(run / "gates.json")
    checks = gates.get("checks")
    if not isinstance(checks, list):
        raise DeriveError("gates-checks-invalid")
    handoff["verification"] = [
        {"check": check.get("id"), "status": check.get("status"), "evidence": check.get("stdout")} for check in checks
    ]
    handoff["confidence"] = _confidence_block(metadata, score)
    if handoff.get("skill") != "code-remediate":
        handoff["artifacts"] = _result_artifact(handoff.get("artifacts") or [], artifact_path)
        return handoff
    handoff["artifacts"] = _artifacts(handoff.get("artifacts") or [], artifact_path)
    if handoff.get("branch") == "caller-contract":
        return handoff
    presentation = metadata.get("resolution_scope", {}).get("presentation_version")
    if presentation in {3, 4}:
        handoff["presentation_version"] = presentation
    draft_tables = handoff.get("tables") or [{}]
    table, records = _resolution_table(metadata, draft_tables[0] if isinstance(draft_tables[0], dict) else {})
    handoff["tables"] = [table]
    handoff["source_records"] = records
    handoff["source_coverage"] = {
        "source_records_total": len(records),
        "represented_source_records_total": len(records),
        "omitted_source_records_total": 0,
    }
    return handoff


def _result_arguments(
    arguments: argparse.Namespace, run: Path, gates: dict[str, Any], metadata: dict[str, Any]
) -> argparse.Namespace:
    """Build the shared result writer's arguments with gate-derived check lists."""
    failed = [
        *gates.get("checks_failed", []),
        *[item for item in arguments.extra_checks_failed.split(",") if item.strip()],
    ]
    return argparse.Namespace(
        out=run / "result.candidate.json",
        gates=run / "gates.json",
        status=arguments.status,
        checks_run=",".join(GATE_IDS),
        checks_failed=",".join(dict.fromkeys(item.strip() for item in failed)),
        critical=arguments.critical,
        high=arguments.high,
        medium=arguments.medium,
        low=arguments.low,
        confidence=arguments.confidence,
        artifact_path=arguments.artifact_path,
        recommendations=arguments.recommendations,
        follow_up=arguments.follow_up,
        metadata=json.dumps(metadata),
    )


def _bind_handoff(run: Path, metadata: dict[str, Any], handoff: dict[str, Any]) -> dict[str, Any]:
    """Render the handoff and record its digest binding in metadata."""
    validation = final_handoff.render_files(
        run / "final-handoff.json", run / "final.md", run / "final-handoff.validation.json"
    )
    binding = {
        "schema_version": 1,
        "handoff_path": "final-handoff.json",
        "handoff_sha256": validation["handoff_sha256"],
        "rendered_path": "final.md",
        "rendered_sha256": validation["rendered_sha256"],
        "validation_path": "final-handoff.validation.json",
        "branch": handoff["branch"],
    }
    metadata["final_handoff"] = binding
    return {**binding, "validation_status": validation["status"]}


def _step(summary: dict[str, Any], name: str, action: Any) -> bool:
    """Run one finalize step, record its outcome, and report whether it passed."""
    try:
        detail = action()
    except (DeriveError, final_handoff.HandoffError, SystemExit) as error:
        code = str(error.code) if isinstance(error, SystemExit) else str(error)
        summary["steps"].append({"step": name, "status": "fail", "code": code})
        summary["status"] = "fail"
        return False
    summary["steps"].append({"step": name, "status": "pass", **({"detail": detail} if detail else {})})
    return True


def finalize(arguments: argparse.Namespace) -> dict[str, Any]:
    """Derive, render, write, validate, and optionally promote one remediation result candidate."""
    run = arguments.run.resolve()
    summary: dict[str, Any] = {"status": "pass", "steps": [], "errors": [], "not_run": [], "promoted": False}
    state: dict[str, Any] = {}

    def derive() -> dict[str, str]:
        draft = _load(arguments.metadata)
        state["metadata"] = derive_metadata(run, draft) if arguments.skill == "code-remediate" else draft
        handoff = derive_handoff(
            run, state["metadata"], _load(arguments.handoff), arguments.confidence, arguments.artifact_path
        )
        _write(run / "final-handoff.json", handoff)
        state["handoff"] = handoff
        return {"handoff": str(run / "final-handoff.json")}

    def write_result() -> dict[str, str]:
        writer = _load_module("codex_rig_write_result", "write-result.py")
        payload = writer.build_payload(_result_arguments(arguments, run, _load(run / "gates.json"), state["metadata"]))
        _write(run / "result.candidate.json", payload)
        return {"candidate": str(run / "result.candidate.json"), "status": payload["status"]}

    steps = (
        ("derive", derive),
        ("render", lambda: _bind_handoff(run, state["metadata"], state["handoff"])),
        ("write-result", write_result),
    )
    if not all(_step(summary, name, action) for name, action in steps):
        return summary
    summary["candidate"] = str(run / "result.candidate.json")
    if arguments.skill == "code-review" and not _step(
        summary, "review-validate", lambda: _review_validate(arguments, run)
    ):
        return summary
    validator = _load_module("codex_rig_validate_artifacts", "validate-artifacts.py")
    report = validator.collect_errors(arguments.skill, run, run / "result.candidate.json")
    summary.update(status=report["status"], errors=report["errors"], not_run=report["not_run"])
    summary["steps"].append(
        {
            "step": "validate",
            "status": report["status"],
            "detail": {"errors": len(report["errors"]), "not_run": len(report["not_run"])},
        }
    )
    if report["status"] == "pass" and arguments.promote:
        os.replace(run / "result.candidate.json", run / "result.json")
        summary["promoted"] = True
        summary["result"] = str(run / "result.json")
    return summary


def _review_validate(arguments: argparse.Namespace, run: Path) -> dict[str, Any]:
    """Run the review-specific validator against the candidate with this run's parent-thread provenance."""
    command = [
        sys.executable,
        str(SHARED_DIRECTORY.parent / "skills" / "code-review" / "validate_artifacts.py"),
        "--out",
        str(run),
        "--result",
        str(run / "result.candidate.json"),
        "--all-errors",
    ]
    if arguments.parent_thread_id:
        command += ["--parent-thread-id", arguments.parent_thread_id]
    if arguments.codex_home:
        command += ["--codex-home", str(arguments.codex_home)]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    try:
        report = json.loads(completed.stdout)
    except json.JSONDecodeError:
        lines = (completed.stderr or completed.stdout).strip().splitlines()
        raise DeriveError("review-validator:" + (lines[-1] if lines else str(completed.returncode))) from None
    if completed.returncode != 0 or report.get("status") != "pass":
        failures = [error["code"] for error in report.get("errors", [])]
        failures += [f"not-run:{entry['step']}" for entry in report.get("not_run", [])]
        raise DeriveError("review-validator:" + ",".join(failures))
    return {"exit_code": completed.returncode, "errors": 0, "not_run": 0}


def _metadata_action(arguments: argparse.Namespace) -> dict[str, Any]:
    """Write merged metadata derived from the run's machine files."""
    _write(arguments.out, derive_metadata(arguments.run.resolve(), _load(arguments.metadata)))
    return {"status": "pass", "metadata": str(arguments.out)}


def _workplan_action(arguments: argparse.Namespace) -> dict[str, Any]:
    """Rewrite the managed workplan sections."""
    path = render_workplan(arguments.run.resolve(), _load(arguments.metadata), arguments.ineligibility_reason)
    return {"status": "pass", "workplan": str(path)}


def _ledger_action(arguments: argparse.Namespace) -> dict[str, Any]:
    """Rewrite the managed resolution-table sections of ``action-items.md``."""
    path = render_ledger(arguments.run.resolve(), _load(arguments.metadata))
    return {"status": "pass", "action_items": str(path)}


ACTIONS = {
    "metadata": _metadata_action,
    "workplan": _workplan_action,
    "ledger": _ledger_action,
    "append": lambda arguments: append_record(arguments.run.resolve(), arguments.ledger, arguments.record),
    "resolutions": lambda arguments: append_review_resolutions(
        arguments.run, arguments.review_run, arguments.sha, arguments.item_sha
    ),
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse one helper action and its options."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    actions = parser.add_subparsers(dest="action", required=True)
    metadata = actions.add_parser("metadata", help="Merge derived fields into a metadata draft.")
    workplan = actions.add_parser("workplan", help="Rewrite the managed resolution-workplan.md sections.")
    ledger = actions.add_parser("ledger", help="Rewrite the action-items.md resolution table from items and events.")
    final = actions.add_parser("finalize", help="Derive, render, write, validate, and optionally promote a result.")
    append = actions.add_parser("append", help="Append a staged <ledger>.rec record to a run ledger, then remove it.")
    feedback = actions.add_parser(
        "resolutions", help="Append per-finding outcomes to the admitted review run's resolution.jsonl."
    )
    for action in (metadata, workplan, ledger, final, append, feedback):
        action.add_argument("--run", type=Path, required=True, help="Remediation run directory.")
    for action in (metadata, workplan, ledger, final):
        action.add_argument("--metadata", type=Path, required=True, help="Agent-authored metadata draft JSON.")
    append.add_argument("--ledger", required=True, choices=APPEND_LEDGERS, help="Run ledger receiving the record.")
    append.add_argument("--record", type=Path, help="Staged record file; default <run>/<ledger>.rec.")
    feedback.add_argument("--review-run", type=Path, required=True, help="Review run whose result was admitted.")
    feedback.add_argument("--sha", help="Commit holding every fix; omit while changes stay unstaged.")
    feedback.add_argument(
        "--item-sha", action="append", default=[], help="ITEM=SHA commit for one item; repeat for per-finding commits."
    )
    metadata.add_argument("--out", type=Path, required=True, help="Path for the merged metadata JSON.")
    workplan.add_argument("--ineligibility-reason", help="Concrete reason a parallel plan is not dispatched.")
    final.add_argument("--handoff", type=Path, required=True, help="Agent-authored final-handoff draft JSON.")
    final.add_argument(
        "--skill",
        choices=("code-remediate", "code-review"),
        default="code-remediate",
        help="Owning workflow; code-review derives only shared handoff fields and also runs its review validator.",
    )
    final.add_argument("--parent-thread-id", help="code-review: parent thread for review-validator provenance.")
    final.add_argument("--codex-home", type=Path, help="code-review: actual Codex home for the review validator.")
    final.add_argument("--status", required=True, choices=("pass", "fail", "timeout"))
    final.add_argument("--confidence", type=float, required=True)
    final.add_argument("--artifact-path", required=True, help="Canonical result path, ending in result.json.")
    final.add_argument("--extra-checks-failed", default="", help="Comma-separated non-gate failed checks.")
    for severity in ("critical", "high", "medium", "low"):
        final.add_argument(f"--{severity}", type=int, default=0)
    final.add_argument("--recommendations", default="", help="JSON list or ||-separated recommendations.")
    final.add_argument("--follow-up", default="", help="JSON list or ||-separated follow-up items.")
    final.add_argument("--promote", action="store_true", help="Rename a passing candidate to result.json.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run one action and print its JSON summary."""
    arguments = parse_args(argv)
    try:
        summary = finalize(arguments) if arguments.action == "finalize" else ACTIONS[arguments.action](arguments)
    except DeriveError as error:
        summary = {"status": "fail", "code": str(error)}
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
