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

Judgement fields stay with the agent: parallel eligibility and approval requirement, item outcomes, closure evidence,
confidence gaps and closures, unresolved summaries, and the handoff outcome, remaining work, next steps, and commit
disposition. The helper never decides a finding, runs gates, changes Git state, or relaxes a validator rule.

## Usage

``metadata --run <run> --metadata <draft.json> --out <derived.json>`` writes merged metadata. ``workplan --run <run>
--metadata <draft.json>`` rewrites the four managed sections of ``resolution-workplan.md`` and keeps every other
section. ``finalize --run <run> --metadata <draft.json> --handoff <draft.json> --status <status> --confidence <score>
--artifact-path <result path>`` derives metadata and handoff, renders ``final.md``, writes ``result.candidate.json``
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


def derive_metadata(run: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    """Merge every derivable metadata field into an agent draft, overwriting stale copies."""
    derive_selection(run, metadata)
    derive_workplan(run, metadata)
    derive_merge(run, metadata)
    return metadata


def _workplan_table(buckets: list[dict[str, Any]]) -> list[str]:
    """Render the bucket table with the columns the workplan validator reads."""
    lines = [
        "| Bucket | Selected indexes | Owner | Verifier | Context pack | Owned paths | Mode | Closure |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for bucket in buckets:
        indexes = ", ".join(str(index) for index in bucket.get("selected_indexes", []))
        paths = ", ".join(f"`{path}`" for path in bucket.get("owned_paths", []))
        closure = bucket.get("expected_closure") or "closure-log.md"
        lines.append(
            f"| {bucket.get('bucket_id')} | {indexes} | {bucket.get('owner')} | {bucket.get('verifier')} | "
            f"`{bucket.get('context_pack_path')}` | {paths} | {bucket.get('execution_mode')} | {closure} |"
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


def _unmanaged_sections(path: Path) -> tuple[str, list[str]]:
    """Return the existing title and every section the helper does not own."""
    if not path.is_file():
        return "# Resolution Workplan", []
    title = "# Resolution Workplan"
    sections: list[list[str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("# ") and not sections:
            title = line
        elif line.startswith("## "):
            sections.append([line])
        elif sections:
            sections[-1].append(line)
    kept = [section for section in sections if section[0][3:].strip() not in MANAGED_SECTIONS]
    return title, ["\n".join(section).rstrip() for section in kept]


def render_workplan(run: Path, metadata: dict[str, Any], reason: str | None) -> Path:
    """Rewrite the managed workplan sections from derived metadata and keep agent-owned sections."""
    derive_metadata(run, metadata)
    workplan = metadata.get("resolution_workplan")
    if not isinstance(workplan, dict) or "work_buckets" not in workplan:
        raise DeriveError("work-bucket-plan-missing")
    path = run / "resolution-workplan.md"
    title, kept = _unmanaged_sections(path)
    buckets = workplan["work_buckets"]
    order = [
        f"{position}. {bucket.get('bucket_id')} — {bucket.get('execution_mode')}"
        for position, bucket in enumerate(buckets, 1)
    ]
    blocks = [
        title,
        "\n".join(["## Work Bucket Plan", "", *_workplan_table(buckets)]),
        "\n".join(["## Parallel Approval", "", *_approval_lines(workplan, reason)]),
        "\n".join(["## Execution Order", "", f"Execution mode: {workplan['execution_mode']}.", "", *order]),
        "## Ungrouped Items\n\nnone",
        *kept,
    ]
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8", newline="\n")
    return path


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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse one helper action and its options."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    actions = parser.add_subparsers(dest="action", required=True)
    metadata = actions.add_parser("metadata", help="Merge derived fields into a metadata draft.")
    workplan = actions.add_parser("workplan", help="Rewrite the managed resolution-workplan.md sections.")
    final = actions.add_parser("finalize", help="Derive, render, write, validate, and optionally promote a result.")
    for action in (metadata, workplan, final):
        action.add_argument("--run", type=Path, required=True, help="Remediation run directory.")
        action.add_argument("--metadata", type=Path, required=True, help="Agent-authored metadata draft JSON.")
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
        if arguments.action == "finalize":
            summary = finalize(arguments)
        elif arguments.action == "metadata":
            summary = _metadata_action(arguments)
        else:
            summary = _workplan_action(arguments)
    except DeriveError as error:
        summary = {"status": "fail", "code": str(error)}
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
