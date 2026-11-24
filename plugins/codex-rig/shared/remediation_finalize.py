#!/usr/bin/env python3
"""Derive remediation, review, and audit bookkeeping and finalize one result candidate.

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
- A source-checkout link naming the absolute path and branch from the current remediation branch receipt, preferring
  the recovered receipt when present. This is recorded location evidence, not a live Git status check.
- The publication disposition and evidence link for a verified local PR worktree delivery, independent of local success.
- Each item's latest outcome fields and each bucket's latest status from the append-only ``resolution-events.jsonl``.
  Growing ledgers (that event file, ``closure-log.md``, and the review run's ``resolution.jsonl``) are only appended,
  never rewritten; rendered tables are rebuilt from them.
- Final triage and resolution counts from those folded item rows, including zero-count historical statuses.

Judgement fields stay with the agent: parallel eligibility and approval requirement, item outcomes, closure evidence,
confidence gaps and closures, unresolved summaries, and the handoff outcome, remaining work, next steps, and commit
disposition. The helper never decides a finding, runs gates, changes Git state, or relaxes a validator rule.

## Usage

``metadata --run <run> --metadata <draft.json> --out <derived.json>`` writes merged metadata. ``workplan --run <run>
--metadata <draft.json>`` rewrites the four managed sections of ``resolution-workplan.md``, including each bucket's
latest status, and keeps every other section. ``ledger --run <run> --metadata <draft.json>`` rewrites the resolution
table, source records and numeric completeness section of ``action-items.md`` from ``selection.json`` identity plus
the latest item outcomes; authored summaries and intake sections remain unchanged.
``append --run <run> --ledger closure-log.md|resolution-events.jsonl`` appends the staged ``<ledger>.rec`` record and
removes it; status events are validated first. ``resolutions --run <run> --review-run <review run> [--sha <commit>]``
appends one outcome per admitted review finding to that review run's ``resolution.jsonl``. ``finalize --run <run>
--metadata <draft.json> --handoff <draft.json> --status <status> --confidence <score> --artifact-path <result path>``
derives metadata and handoff, refreshes the remediation ledger, renders ``final.md``, writes ``result.candidate.json``
through the shared result writer, and runs the shared validator in all-errors mode; ``--promote`` renames a passing
candidate to ``result.json``. ``publication-resume --run <run>`` reads the local delivery and terminal or consumed
publication evidence so the same run can finish reports after locally rechecking its destination.

``obligations --run <run>`` derives inspectable report source occurrences from retained ``findings-input.txt``.
Retain these as review context; they grant no admission, selection, or automatic action-item authority.
``preflight --run <run> --stage selection [--report]`` validates pending inventory and exact report coverage before
prompting. ``preflight --run <run> --stage plan --metadata <draft.json>`` validates frozen ownership, selection,
dependencies and dispatch evidence before edits; completed parallel runtime evidence remains a final gate.

## Used by

The code-remediate skill uses it at work-bucket planning and at result finalization. Code Review uses
``finalize --skill code-review``, which also synchronizes canonical finding and blocker content in existing notes and
handoff action tables, preserving authored statuses and source bindings. It runs the review-specific validator before
the shared one and leaves completion to
``find-review-report.py``. Audit uses ``finalize --skill audit`` for shared handoff derivation and the audit
validator, preserving authored findings, decisions, source coverage and evidence metadata. Finalization tests compare
its derived fields with hand-built valid fixtures.

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
import shlex
import subprocess
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import ModuleType
from typing import Any
from urllib.parse import unquote

# A source file cannot contain a bytecode cache directory.
sys.pycache_prefix = str(Path(__file__).resolve())

#: Directory containing this helper and its sibling modules, added to `sys.path` for file-path loading.
SHARED_DIRECTORY = Path(__file__).resolve().parent
if str(SHARED_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SHARED_DIRECTORY))

import final_handoff  # noqa: E402
import worktree_publication  # noqa: E402

#: Maximum bytes accepted from the append-only remediation event ledger.
MAX_LEDGER_BYTES = 16 * 1024 * 1024

#: Item fields that identify a review item and are carried over unchanged when metadata is merged.
IDENTITY_FIELDS = ("input_item_id", "item_name", "item_type", "severity", "selectable", "sources")
#: Maps each parallel-approval response to the approval status stored in the workplan.
APPROVAL_STATUS = {"approve": "approved", "parent-only": "parent-only", "not-required": "not-required"}
#: Maps a handoff presentation version to the table layout used when rendering the final handoff.
#: Verification gate identifiers, in the order they are reported in the handoff.
GATE_IDS = ("lint", "format", "types", "tests", "review")
#: Contract version for the separately derived PR worktree publication metadata.
PUBLICATION_CONTRACT_VERSION = 1
#: Run-local artifacts unique to the current publication workflow.
PUBLICATION_CHECKPOINT_FILES = (
    "publication.json",
    "publication-offer.json",
    "publication-attempt.json",
    "publication-executed",
    "publication-execution-result.json",
    "publication-readback.json",
)
PUBLICATION_EVIDENCE_FILES = (
    "worktree-delivery.json",
    worktree_publication.CANDIDATE_VALIDATION_FILE,
    *PUBLICATION_CHECKPOINT_FILES,
)
#: Workplan section headings this helper regenerates; any other section is preserved as written.
MANAGED_SECTIONS = ("Work Bucket Plan", "Parallel Approval", "Execution Order", "Ungrouped Items")
#: Action-items section headings this helper regenerates from the resolution ledger.
LEDGER_SECTIONS = ("Review Item Resolution Table", "Expanded Source Records", "Final Resolution Table Completeness")
#: File name of the append-only log of item and bucket resolution events.
EVENTS_LEDGER = "resolution-events.jsonl"
#: File name of the append-only closure evidence log.
CLOSURE_LEDGER = "closure-log.md"
#: Run ledgers that the `append` action is allowed to write to.
APPEND_LEDGERS = (CLOSURE_LEDGER, EVENTS_LEDGER)
#: File name of the review run's resolution ledger that remediation results are written back to.
REVIEW_RESOLUTION_LEDGER = "resolution.jsonl"
#: Schema revision of each record in the resolution-events ledger.
EVENT_SCHEMA_VERSION = 1
#: Schema revision of each record written to the review resolution ledger.
RESOLUTION_SCHEMA_VERSION = 1
#: Allowed status values for a work-bucket event.
BUCKET_STATUSES = frozenset({"planned", "in-progress", "fixed", "verified", "deferred", "unresolved"})
#: Allowed values for each enumerated field of an item event.
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
#: Free-text fields an item event may carry, which must be non-empty strings when present.
ITEM_EVENT_TEXT_FIELDS = ("resolved_how", "evidence")
#: Fields that together record an item's outcome and must all be set before it can be finalized.
ITEM_OUTCOME_FIELDS = ("triage_status", "resolution_status", "owner_status", "resolved_how", "evidence")
#: Resolution statuses that count an item as fixed.
FIXED_RESOLUTIONS = frozenset({"implemented", "resolved", "already-fixed", "already-applied"})
#: Resolution statuses that count an item as rejected; `stale` stays readable for historical results.
REJECTED_RESOLUTIONS = frozenset({"rejected", "not-applicable", "duplicate", "stale"})
#: Owner statuses that mean an item was deferred or not selected for this run.
DEFERRED_OWNERS = frozenset({"deferred", "not-selected"})
#: Pattern for an abbreviated or full lowercase hex commit hash of 7 to 64 characters.
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


def _write(path: Path, payload: dict[str, Any]) -> bytes:
    """Write deterministic UTF-8 JSON with LF line endings and return the exact bytes supplied by the producer."""
    encoded = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode()
    path.write_bytes(encoded)
    return encoded


def _publication_record(run: Path, name: str) -> tuple[dict[str, Any], str]:
    """Read and hash the same bytes from an ordinary run-local publication record."""
    path = run / name
    if path.is_symlink() or not path.is_file():
        raise DeriveError(f"publication-evidence-missing:{name}")
    try:
        content = path.read_bytes()
        record = json.loads(content.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise DeriveError(f"publication-evidence-invalid:{name}") from error
    if not isinstance(record, dict):
        raise DeriveError(f"publication-evidence-invalid:{name}")
    return record, hashlib.sha256(content).hexdigest()


def _absolute_coordinate(value: object) -> bool:
    """Recognize persisted checkout paths from either supported path family."""
    return isinstance(value, str) and (PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute())


def _full_oid(value: object) -> bool:
    """Check the full hexadecimal spelling of a Git object ID."""
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value) is not None


def _publication_delivery(run: Path) -> dict[str, Any]:
    """Require recorded worktree-only delivery at the selected absolute checkout."""
    if os.path.lexists(run / "recovery-worktree.json"):
        raise DeriveError("publication-ineligible-recovery-worktree")
    locator, _ = _publication_record(run, "remediation-worktree.json")
    delivery, _ = _publication_record(run, "worktree-delivery.json")
    detailed = run / "worktree-delivery.md"
    if (
        type(locator.get("schema_version")) is not int
        or locator["schema_version"] != 1
        or set(delivery) != {"schema_version", "status", "checkout", "branch", "head", "common_dir"}
        or type(delivery["schema_version"]) is not int
        or delivery["schema_version"] != 1
        or delivery["status"] != "worktree-only"
        or not _absolute_coordinate(delivery["checkout"])
        or not _absolute_coordinate(delivery["common_dir"])
        or not _absolute_coordinate(locator.get("invoking_checkout"))
        or locator["invoking_checkout"] == delivery["checkout"]
        or locator.get("worktree") != delivery["checkout"]
        or locator.get("git_common_dir") != delivery["common_dir"]
        or not _full_oid(delivery["head"])
        or not isinstance(delivery["branch"], str)
        or not delivery["branch"].strip()
        or detailed.is_symlink()
        or not detailed.is_file()
    ):
        raise DeriveError("publication-delivery-invalid")
    return delivery


def _publication_state(run: Path, delivery: dict[str, Any], *, resume: bool = False) -> dict[str, Any]:
    """Resolve local publication evidence, allowing report-only closure of an interrupted optional offer.

    The execution marker, written before the helper sends anything, decides what an approved attempt can claim. With no
    entry at the marker path nothing was sent, so the only disposition is ``skipped``; with any entry there, even a
    damaged one, the attempt started and only ``verified``, ``failed`` or ``unverified`` apply.
    """
    evidence = "publication.json"
    attempt_path = run / "publication-attempt.json"
    offer_path = run / "publication-offer.json"
    offer = None
    consumed = attempt_path.exists() or attempt_path.is_symlink()
    started = os.path.lexists(run / "publication-executed")
    result_present = os.path.lexists(run / "publication-execution-result.json")
    if (not consumed and (started or result_present)) or (result_present and not started):
        raise DeriveError("publication-attempt-invalid")
    if consumed or offer_path.exists() or offer_path.is_symlink():
        offer, offer_digest = _publication_record(run, offer_path.name)
        if (
            not worktree_publication.valid_offer_fields(offer)
            or offer["checkout"] != delivery["checkout"]
            or offer["branch"] != delivery["branch"]
            or offer["common_dir"] != delivery["common_dir"]
            or offer["source_oid"] != delivery["head"]
            or not _full_oid(offer["expected_oid"])
            or not isinstance(offer["url"], str)
            or not re.fullmatch(r"https://github\.com/[A-Za-z0-9-]+/[A-Za-z0-9_.-]+\.git", offer["url"])
            or not isinstance(offer["ref"], str)
            or not offer["ref"].startswith("refs/heads/")
            or offer["ref"] == "refs/heads/"
        ):
            raise DeriveError("publication-offer-invalid")
    denied_answer = None
    if (run.name == worktree_publication.REQUEST_DIRECTORY and consumed) or os.path.lexists(
        run / "publication-answer.json"
    ):
        answer, answer_digest = _publication_record(run, "publication-answer.json")
        if (
            offer is None
            or set(answer) != {"offer_digest", "decision_id", "answer"}
            or answer["offer_digest"] != offer_digest
            or answer["decision_id"] != worktree_publication.publication_decision_id(run, offer_digest)
            or answer["answer"] not in ("Approve", "Deny")
            or (consumed and answer["answer"] != "Approve")
        ):
            raise DeriveError("publication-answer-mismatch")
        if answer["answer"] == "Deny":
            denied_answer = answer_digest
    if consumed:
        attempt, attempt_digest = _publication_record(run, attempt_path.name)
        if attempt != {
            "offer_digest": offer_digest,
            "decision_id": worktree_publication.publication_decision_id(run, offer_digest),
            "answer": "Approve",
        }:
            raise DeriveError("publication-attempt-invalid")
    terminal_path = run / evidence
    if not terminal_path.exists() and not terminal_path.is_symlink():
        if not consumed:
            if resume and denied_answer:
                return {
                    "schema_version": 1,
                    "status": "declined",
                    "reason": "The user declined this publication request.",
                    "observed_oid": None,
                    "evidence": "publication-answer.json",
                    "evidence_sha256": denied_answer,
                }
            if resume:
                evidence = "worktree-delivery.json" if offer is None else offer_path.name
                _, digest = _publication_record(run, evidence)
                return {
                    "schema_version": 1,
                    "status": "skipped",
                    "reason": "Local delivery was recorded before the optional publication decision finished.",
                    "observed_oid": None,
                    "evidence": evidence,
                    "evidence_sha256": digest,
                }
            raise DeriveError("publication-disposition-required")
        if not started:
            return {
                "schema_version": 1,
                "status": "skipped",
                "reason": "Approval was consumed, but no push started: nothing was sent.",
                "observed_oid": None,
                "evidence": attempt_path.name,
                "evidence_sha256": attempt_digest,
            }
        readback_digest = _verified_readback_digest(run, offer, offer_digest, offer["source_oid"])
        if readback_digest is not None:
            return {
                "schema_version": 1,
                "status": "verified",
                "reason": "Successful transport and the audited destination ref confirm the delivered commit.",
                "observed_oid": offer["source_oid"],
                "evidence": attempt_path.name,
                "evidence_sha256": attempt_digest,
                "readback_sha256": readback_digest,
            }
        return {
            "schema_version": 1,
            "status": "unverified",
            "reason": "Approval was consumed. Publication was not verified.",
            "observed_oid": None,
            "evidence": attempt_path.name,
            "evidence_sha256": attempt_digest,
        }

    record, digest = _publication_record(run, evidence)
    allowed = {"verified", "failed", "unverified"} if started else {"skipped"} if consumed else {"declined", "skipped"}
    if not consumed and (denied_answer or run.name == worktree_publication.REQUEST_DIRECTORY):
        allowed = {"declined"} if denied_answer else {"skipped"}
    if (
        set(record) != {"schema_version", "status", "reason", "observed_oid"}
        or type(record["schema_version"]) is not int
        or record["schema_version"] != 1
        or not isinstance(record["status"], str)
        or record["status"] not in allowed
        or not isinstance(record["reason"], str)
        or not record["reason"].strip()
        or (record["observed_oid"] is not None and not _full_oid(record["observed_oid"]))
    ):
        raise DeriveError("publication-disposition-invalid")
    binding = {"evidence": evidence, "evidence_sha256": digest, **record}
    if record["status"] == "verified":
        readback_digest = _verified_readback_digest(run, offer, offer_digest, record["observed_oid"])
        if readback_digest is None:
            raise DeriveError("publication-verification-unproven")
        binding["readback_sha256"] = readback_digest
    return binding


def _readback_matches_offer(url: object, offer: dict[str, Any]) -> bool:
    """Match the GitHub REST repository and decode only the escaped ref suffix once."""
    repository = offer["url"].removeprefix("https://github.com/").removesuffix(".git")
    prefix = f"https://api.github.com/repos/{repository}/git/refs/"
    if not isinstance(url, str) or not url.startswith(prefix):
        return False
    suffix = url.removeprefix(prefix)
    if re.search(r"[?#]|%(?![0-9a-fA-F]{2})", suffix):
        return False
    try:
        return unquote(suffix, errors="strict") == offer["ref"].removeprefix("refs/")
    except UnicodeDecodeError:
        return False


def _verified_readback_digest(run: Path, offer: dict[str, Any], digest: str, observed_oid: str | None) -> str | None:
    """Return the audited ref readback digest only when it proves the approved push reached its destination."""
    marker = run / "publication-executed"
    try:
        if marker.is_symlink() or not marker.is_file() or marker.read_bytes() != f"{digest}\n".encode("ascii"):
            return None
        execution, _ = _publication_record(run, "publication-execution-result.json")
        readback, readback_digest = _publication_record(run, "publication-readback.json")
    except (DeriveError, OSError):
        return None
    obj = readback.get("object")
    if (
        execution != {"status": "pushed", "offer_digest": digest}
        or readback.get("ref") != offer["ref"]
        or not _readback_matches_offer(readback.get("url"), offer)
        or not isinstance(obj, dict)
        or obj.get("type") != "commit"
        or obj.get("sha") != observed_oid
        or observed_oid != offer["source_oid"]
    ):
        return None
    return readback_digest


def publication_binding(run: Path) -> dict[str, Any]:
    """Recompute publication disposition from retained worktree delivery and publication receipts."""
    return _publication_state(run, _publication_delivery(run))


def publication_evidence_present(run: Path) -> bool:
    """Identify any retained current publication sidecar, including an invalid filesystem entry."""
    return any(path.exists() or path.is_symlink() for path in (run / name for name in PUBLICATION_EVIDENCE_FILES))


def publication_artifact(publication: dict[str, Any], artifact_path: str) -> dict[str, str]:
    """Produce the machine-derived handoff link for the retained publication evidence."""
    evidence_path = PurePosixPath(artifact_path.replace("\\", "/")).with_name(publication["evidence"])
    return {"label": f"PR publication: {publication['status']}", "path": str(evidence_path)}


def _committed_linked_delivery(run: Path, handoff: dict[str, Any]) -> bool:
    """Locate a new PR commit at the recorded linked checkout before routing to the full publication gate."""
    disposition = handoff.get("commit_disposition")
    if not isinstance(disposition, dict) or disposition.get("status") != "committed":
        return False
    # Delivery evidence by itself is expected before the first question. Every offer, decision, or child
    # checkpoint belongs to an existing publication lifecycle, even when interrupted or unreadable.
    if any(
        os.path.lexists(run / name)
        for name in (*PUBLICATION_CHECKPOINT_FILES, "publication-answer.json", worktree_publication.REQUEST_DIRECTORY)
    ):
        return False
    try:
        delivery = worktree_publication.handoff_destination(run, handoff["artifacts"])
        checkout = Path(delivery["checkout"])
        if not checkout.is_absolute():
            return False
        routing = _load(run / "pr" / "pr-routing.json")
        expected = routing["head_oid"]
        if not _full_oid(expected) or not _full_oid(delivery["head"]) or expected == delivery["head"]:
            return False
        worktree_publication._verify_requested_pr(
            run,
            {
                "expected_oid": expected,
                "branch": delivery["branch"],
                "ref": f"refs/heads/{routing['head_ref']}",
                "url": f"https://github.com/{routing['head_repo']}.git",
            },
        )
        # Only local plumbing, with repository overrides, lazy remote fetching and replace objects disabled.
        env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
        env.update(GIT_NO_LAZY_FETCH="1", GIT_ALLOW_PROTOCOL="", GIT_NO_REPLACE_OBJECTS="1")
        root, own, common, head, ref = worktree_publication._git(
            "git",
            checkout,
            env,
            "rev-parse",
            "--path-format=absolute",
            "--show-toplevel",
            "--absolute-git-dir",
            "--git-common-dir",
            "HEAD",
            "--symbolic-full-name",
            "HEAD",
        ).splitlines()
        if (
            Path(root).resolve(strict=True) != checkout.resolve(strict=True)
            or Path(own).resolve(strict=True) == Path(common).resolve(strict=True)
            or Path(delivery.get("common_dir", common)).resolve(strict=True) != Path(common).resolve(strict=True)
            or head != delivery["head"]
            or ref != f"refs/heads/{delivery['branch']}"
        ):
            return False
        worktree_publication._git("git", checkout, env, "merge-base", "--is-ancestor", expected, head)
    except (DeriveError, KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError):
        # Missing or stale local evidence grants no automatic offer. Successful local report delivery remains valid.
        return False
    return True


def _publication_next_step(run: Path, handoff: dict[str, Any], *, offer_eligible: bool) -> str | None:
    """Derive the workflow's next action while preserving an exact caller contract and authored follow-ups."""
    standard = handoff.get("branch") == "standard"
    row_ids = {row["id"] for table in handoff["tables"] for row in table["rows"]} if standard else set()
    action = final_handoff.pr_publication_action(row_ids)
    if standard:
        if not isinstance(handoff.get("remaining"), list) or not isinstance(handoff.get("next_steps"), list):
            return None  # Preserve the handoff validator's diagnostic.
        # A prior candidate may carry either wording. Recompute only its generated step after a decision or opt-out.
        remaining = [item for item in handoff["remaining"] if not final_handoff.is_pr_publication_action(item, action)]
        if len(remaining) != len(handoff["remaining"]):
            handoff["remaining"] = remaining
            handoff["next_steps"] = [row_id for row_id in handoff["next_steps"] if row_id != action["row_id"]]
    if not offer_eligible or not _committed_linked_delivery(run, handoff):
        return None
    if standard:
        handoff["remaining"].append(action)
        handoff["next_steps"].append(action["row_id"])
        if len(handoff["next_steps"]) > 1 and handoff["action_policy"] == "best-way-forward":
            handoff["action_policy"] = "owned"
    return action["next_action"]


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
    with path.open("rb") as stream:
        content = stream.read(MAX_LEDGER_BYTES + 1)
    if len(content) > MAX_LEDGER_BYTES:
        raise DeriveError("events-ledger-oversized")
    items, buckets = _known_identities(run)
    events = []
    for number, line in enumerate(content.decode("utf-8").splitlines(), 1):
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
    if staged.is_symlink() or staged.resolve() != run.resolve() / f"{ledger}.rec":
        raise DeriveError("append-record-path-invalid")
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
    """Derive scope, identities and complete table counts from the validated frozen inventory.

    Example:
        >>> import tempfile
        >>> run = Path(tempfile.mkdtemp())
        >>> _ = (run / "selection.json").write_text(json.dumps({"schema_version": 1,
        ...     "selected_indexes": [1], "items": [
        ...     {"input_item_id": "R1", "item_name": "n", "item_type": "code", "severity": "high",
        ...      "summary": "Add the guard.", "closure_evidence": "Boundary regression passes.",
        ...      "selectable": True, "sources": [{"kind": "report", "source_id": "review.json#F1",
        ...      "location": "widget.py:1", "body": "Missing guard.", "evidence": "review.json"}]}]}))
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
    _, selectable, counts = final_handoff._validate_selection(inventory)
    selectable_total = len(selectable)
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
    # Every frozen row/source is copied above; original-input coverage remains an independent admission check.
    validator = _load_module("codex_rig_validate_artifacts", "validate-artifacts.py")
    table.update(
        ingested_entries_total=counts["items_total"],
        table_rows_total=counts["items_total"],
        omitted_entries_total=0,
        selectable_rows_total=selectable_total,
        nonselectable_rows_total=counts["items_total"] - selectable_total,
        source_records_total=counts["source_records_total"],
        represented_source_records_total=counts["source_records_total"],
        omitted_source_records_total=0,
        grouped_items_total=counts["grouped_items_total"],
        required_columns=sorted(validator.CODE_REMEDIATE_FINAL_TABLE_REQUIRED_COLUMNS),
    )


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
    table = metadata["final_resolution_table"]
    for field in ("triage_status", "resolution_status"):
        counts = dict.fromkeys(sorted(ITEM_EVENT_ENUMS[field] | {"stale"}), 0)
        for item in table["items"]:
            value = item.get(field)
            status = value.strip().casefold() if isinstance(value, str) else None
            if status not in counts:
                raise DeriveError(f"item-{field}-invalid:{item.get('input_item_id')}")
            counts[status] += 1
        table[f"{field}_counts"] = counts
    derive_workplan(run, metadata)
    derive_merge(run, metadata)
    intake = metadata.get("review_report_intake", {})
    if intake.get("requested_report") and intake.get("admission_status", "completed") != "unavailable":
        intake["obligation_records_version"] = 2
        validator = _load_module("codex_rig_validate_artifacts", "validate-artifacts.py")
        intake["context_sources"] = validator.report_obligation_sources(
            _load(run / "findings-input.txt"), intake.get("admission_evidence", {}).get("producer_result_path")
        )
        intake["review_limitations"] = _load(run / "selection.json").get("review_limitations", [])
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
    derive_selection(run, metadata)
    derive_workplan(run, metadata)
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
    """Rewrite resolution rows, source records and completeness counts from selection identity and events.

    Item outcomes come from the latest appended ``item`` events, falling back to the metadata draft; a missing outcome
    field fails instead of rendering an empty cell. The numeric completeness section is derived from those same rows;
    Authored summaries, existing review-report intake and expanded item records stay unchanged. A missing intake section
    can be derived from a complete current inventory without inventing report evidence or findings.
    """
    derive_metadata(run, metadata)
    resolution_table = metadata["final_resolution_table"]
    items = resolution_table["items"]
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
    intake = metadata.get("review_report_intake")
    grouped = metadata.get("resolution_scope", {}).get("presentation_version") in {2, 3, 4, 5}
    intake_sections = [
        i for i, section in enumerate(kept) if section.splitlines()[0].strip().casefold() == "## review report intake"
    ]
    begin = "<!-- codex-rig:review-intake begin -->"
    end = "<!-- codex-rig:review-intake end -->"
    generated = [i for i in intake_sections if begin in kept[i] or end in kept[i]]
    if len(generated) > 1 or (generated and len(intake_sections) != 1):
        raise DeriveError("review-report-intake-generated-ambiguous")
    if not intake_sections or generated:
        if not isinstance(intake, dict) or not grouped:
            if generated:
                raise DeriveError("review-report-intake-generated-input-missing")
        else:
            report_items = [
                item for item in items if any(source.get("kind") == "report" for source in item.get("sources", []))
            ]
            gate_items = [item for item in report_items if item.get("item_type") in {"review-gate", "confidence-gap"}]
            counts = {
                "report_items_total": len(report_items),
                "review_gate_items_total": len(gate_items),
                "review_gate_items_selectable": sum(item.get("selectable") is True for item in gate_items),
                "report_items_marked_out_of_scope": sum(
                    item.get("triage_status") == "out-of-scope" for item in report_items
                ),
            }
            requested = intake.get("requested_report")
            if (
                type(requested) is not bool
                or (not requested and report_items)
                or any(type(intake.get(key)) is not int or intake[key] != value for key, value in counts.items())
            ):
                raise DeriveError("review-report-intake-inventory-mismatch")
            block = (
                f"{begin}\n"
                f"Requested report: {str(requested).lower()}. Report-origin items: {counts['report_items_total']}. "
                f"Report-origin review-gate and follow-up items: {counts['review_gate_items_total']}. "
                f"Selectable review-gate and follow-up items: {counts['review_gate_items_selectable']}. "
                f"Report-origin items marked out of scope: {counts['report_items_marked_out_of_scope']}.\n{end}"
            )
            if generated:
                index = generated[0]
                section = kept[index]
                if section.count(begin) != 1 or section.count(end) != 1 or section.index(begin) > section.index(end):
                    raise DeriveError("review-report-intake-generated-invalid")
                kept[index] = section[: section.index(begin)] + block + section[section.index(end) + len(end) :]
            else:
                kept.append("## Review Report Intake\n\n" + block)
    table = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |", *rows]
    completeness = [
        f"- {label}: {resolution_table[field]}"
        for label, field in (
            ("Ingested entries", "ingested_entries_total"),
            ("Final table rows", "table_rows_total"),
            ("Omitted entries", "omitted_entries_total"),
            ("Selectable rows", "selectable_rows_total"),
            ("Non-selectable rows", "nonselectable_rows_total"),
            ("Source records", "source_records_total"),
            ("Represented source records", "represented_source_records_total"),
            ("Omitted source records", "omitted_source_records_total"),
            ("Grouped items", "grouped_items_total"),
        )
    ]
    for label, field in (("Triage", "triage_status_counts"), ("Resolution", "resolution_status_counts")):
        counts = ", ".join(f"{status}={count}" for status, count in resolution_table[field].items())
        completeness.append(f"- {label} status counts: {counts}")
    blocks = [
        *([title] if title else []),
        "\n".join(["## Review Item Resolution Table", "", *table, "", *details]),
        "\n".join(["## Expanded Source Records", "", *records]),
        *kept,
        "\n".join(["## Final Resolution Table Completeness", "", *completeness]),
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
        if identity.startswith("/"):
            continue  # JSON pointers identify report obligations, not canonical code findings.
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
    # Only a promoted review is ever admitted; an unpromoted ``result.candidate.json`` must not receive verdicts,
    # because the next review would read them back as prior resolutions for findings no completed review validated.
    promoted = review_run / "result.json"
    if promoted.is_file() and promoted.read_bytes() == admitted:
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
    metadata = _load(result_path).get("metadata", {})
    items = metadata.get("final_resolution_table", {}).get("items") or []
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
    # Reclassified process records must stay rejected when the producer is read again, without becoming work items.
    intake = metadata.get("review_report_intake", {})
    for limitation in intake.get("review_limitations", []) if intake.get("obligation_records_version") == 2 else []:
        finding_id = limitation["finding_id"]
        if finding_id not in known or finding_id in seen:
            raise DeriveError(f"resolution-review-limitation-invalid:{finding_id}")
        seen.add(finding_id)
        line = _json_line(
            {
                "schema_version": RESOLUTION_SCHEMA_VERSION,
                "finding_id": finding_id,
                "item_id": finding_id,
                "verdict": "rejected",
                "sha": None,
                "why": _one_line(limitation["rationale"]),
                "remediation_run": run.name,
            }
        )
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


def _artifacts(run: Path, existing: list[Any], artifact_path: str) -> list[dict[str, str]]:
    """Bind result, ledger, and recorded source location while keeping other labelled artifacts."""
    ledger = str(PurePosixPath(artifact_path.replace("\\", "/")).with_name("action-items.md"))
    kept = [
        artifact
        for artifact in existing
        if isinstance(artifact, dict)
        and artifact.get("label") not in {"Result", "Action items"}
        and not str(artifact.get("label", "")).startswith("PR publication:")
        and not str(artifact.get("label", "")).startswith("Source checkout:")
        and PurePosixPath(str(artifact.get("path", "")).replace("\\", "/")).name not in {"action-items.md"}
        and artifact.get("path") != artifact_path
    ]
    artifacts = [{"label": "Result", "path": artifact_path}, {"label": "Action items", "path": ledger}, *kept]
    # Recovery supersedes the original receipt; a malformed recovery must never fall back to an old destination.
    receipt = run / "remediation-branch-recovered.json"
    if not receipt.exists() and not receipt.is_symlink():
        receipt = run / "remediation-branch.json"
    if receipt.exists() or receipt.is_symlink():
        record = _load(receipt)
        branch, worktree = record.get("branch"), record.get("worktree")
        if (
            record.get("schema_version") != 2
            or record.get("status") != "prepared"
            or not isinstance(branch, str)
            or not branch.strip()
            or not _absolute_coordinate(worktree)
        ):
            raise DeriveError(f"checkout-receipt-invalid:{receipt.name}")
        artifacts.append({"label": f"Source checkout: {worktree} (branch {branch})", "path": worktree})
    return artifacts


def _result_artifact(existing: list[Any], artifact_path: str) -> list[dict[str, str]]:
    """Bind the result path while keeping every other labelled artifact."""
    kept = [
        artifact
        for artifact in existing
        if isinstance(artifact, dict) and artifact.get("label") != "Result" and artifact.get("path") != artifact_path
    ]
    return [{"label": "Result", "path": artifact_path}, *kept]


def _prepare_handoff_draft(
    skill: str, metadata: dict[str, Any], handoff: dict[str, Any], errors: list[dict[str, str]]
) -> None:
    """Bind the invocation's identity and report incomplete assessed-review inputs before touching run evidence."""

    def issue(field: str, hint: str) -> None:
        problem = "invalid" if field in handoff else "missing"
        errors.append({"step": "derive", "code": f"handoff-draft-{problem}:{field}", "hint": hint})

    if handoff.setdefault("skill", skill) != skill:
        issue("skill", f"The draft must match the selected --skill {skill}.")
    version = handoff.setdefault("schema_version", final_handoff.SCHEMA_VERSION)
    if type(version) is not int or version != final_handoff.SCHEMA_VERSION:
        issue("schema_version", f"Use handoff schema {final_handoff.SCHEMA_VERSION} or omit the field.")
    if handoff.get("branch") != "caller-contract":
        handoff.setdefault("caller_contract", None)
    if skill == "code-review":
        status = metadata.get("review_status")
        expected_branch = status if status in ("closed", "unavailable") else "assessed"
        if handoff.get("branch") != expected_branch:
            issue("branch", f"The recorded review status requires the {expected_branch} branch.")
    if skill == "code-review" and handoff.get("branch") == "assessed":
        decision = metadata.get("review_decision")
        # An explicitly undecidable judgment is valid; an absent judgment cannot supply an outcome.
        recommendation = decision.get("recommendation", "") if isinstance(decision, dict) else ""
        if recommendation in (*final_handoff.REVIEW_RECOMMENDATIONS, None):
            expected = {"title": "Review Decision", "summary": f"Recommendation: {recommendation}."}
            if handoff.setdefault("outcome", expected) != expected:
                issue("outcome", f"Use the recorded review decision {json.dumps(expected)} or omit this derived field.")
        elif not isinstance(handoff.get("outcome"), dict):
            issue(
                "outcome", "Record the source judgment in metadata.review_decision and its canonical handoff outcome."
            )
        snapshot = "PR Snapshot" if metadata.get("scope") == "pr" else "Review Snapshot"
        tables = handoff.get("tables")
        if (
            not isinstance(tables, list)
            or any(not isinstance(table, dict) for table in tables)
            or not any(table.get("heading") == snapshot for table in tables)
        ):
            issue("tables", f"Provide the evidence-backed {snapshot} with its authored rows and source bindings.")
        for field, hint in (
            ("source_records", "Bind each table row to its retained evidence using stable source IDs."),
            ("remaining", "List each outstanding item with its owner and next action. Use [] when none remain."),
            ("next_steps", "List the chosen next-step row IDs in order. Use [] when no actions remain."),
        ):
            if not isinstance(handoff.get(field), list):
                issue(field, hint)
    if errors:
        raise DeriveError("handoff-draft-invalid")


def _resolution_table(metadata: dict[str, Any], draft: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Generate remediation rows, detail notes, and source records from the resolution items."""
    rows, details, records = [], [], []
    scope = metadata.get("resolution_scope", {})
    selectable_index = 0
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
        if item.get("selectable"):
            selectable_index += 1
        rows[-1]["selected"] = (
            "Closed"
            if not item.get("selectable")
            else "Yes"
            if selectable_index in scope.get("selected_indexes", [])
            else "No"
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
        "layout": "concise",
        "overview_only": True,
    }
    table.setdefault("heading", "Remediation results")
    return table, records


def _restore_authored_notes(path: Path, installed: bytes, prior: bytes | None, identity: os.stat_result) -> None:
    """Undo only this failed attempt's notes installation, preserving a concurrent writer's replacement."""
    try:
        if not os.path.samestat(path.lstat(), identity) or path.read_bytes() != installed:
            return
        if prior is None:
            path.unlink()
        else:
            path.write_bytes(prior)
    except FileNotFoundError:
        pass


def derive_handoff(
    run: Path,
    metadata: dict[str, Any],
    handoff: dict[str, Any],
    score: float,
    artifact_path: str,
    *,
    measured_code_safety: dict[str, Any] | None = None,
    review_notes: Path | None = None,
    notes_rollback: ExitStack | None = None,
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
    handoff["presentation_version"] = 15
    handoff.setdefault("action_policy", "owned")
    if handoff.get("skill") != "code-remediate":
        handoff["artifacts"] = _result_artifact(handoff.get("artifacts") or [], artifact_path)
        if handoff.get("skill") == "code-review" and handoff.get("branch") == "assessed":
            if metadata.get("scope") == "pr":
                handoff["presentation_version"] = 16
            commit = final_handoff.review_commit_sha(run, metadata.get("scope"))
            metadata["reviewed_commit_sha"] = commit
            value = f"{commit} (working-tree base)" if metadata["scope"] in {"working-tree", "path"} else commit
            for table in handoff.get("tables", []):
                if table.get("heading") in {"PR Snapshot", "Review Snapshot"}:
                    rows = table.get("rows")
                    if not isinstance(rows, list) or not all(
                        isinstance(row, dict) and isinstance(row.get("cells"), list) and len(row["cells"]) == 2
                        for row in rows
                    ):
                        raise DeriveError(f"review-snapshot-rows-invalid:{table['heading']}")
                    existing = [row for row in rows if row["cells"][0] == "Commit"]
                    if len(existing) > 1:
                        raise DeriveError("review-commit-row-duplicate")
                    if existing:
                        existing[0]["cells"][1] = value
                    else:
                        source_id = "reviewed-commit"
                        if any(
                            not isinstance(record, dict) or not record.get("id") for record in handoff["source_records"]
                        ):
                            raise DeriveError("source-record-id-missing")
                        if any(record["id"] == source_id for record in handoff["source_records"]):
                            raise DeriveError("review-commit-source-id-conflict")
                        receipt = (
                            "reviewed-commit.txt"
                            if (run / "reviewed-commit.txt").is_file()
                            else "batch-inventory.json"
                            if (run / "batch-inventory.json").is_file()
                            else ("pr-routing.json" if (run / "pr-routing.json").is_file() else "local-checkout.json")
                            if metadata["scope"] == "pr"
                            else "gates.json"
                            if metadata["scope"] == "commit"
                            else "local-source/source-snapshot.json"
                        )
                        rows.insert(
                            len(rows) - 1,
                            {"id": "reviewed-commit", "cells": ["Commit", value], "source_ids": [source_id]},
                        )
                        handoff["source_records"].append({"id": source_id, "evidence": receipt})
                        if "source_coverage" in handoff:
                            coverage = handoff["source_coverage"]
                            for key in ("source_records_total", "represented_source_records_total"):
                                if not isinstance(coverage, dict) or type(coverage.get(key)) is not int:
                                    raise DeriveError(f"source-coverage-count-invalid:{key}")
                                coverage[key] += 1
                if table.get("heading") == "Review Findings and Merge Blocks":
                    table.update(layout="concise", overview_only=True)
            report = run / "review-notes.md"
            if report.is_symlink() or (report.exists() and not report.is_file()):
                raise DeriveError("review-commit-report-invalid")
            authored = report
            if review_notes is not None:
                authored = run / review_notes if review_notes.parent == Path(".") else review_notes
                if authored.parent.resolve() != run.resolve():
                    raise DeriveError("review-notes-input-outside-run")
                authored = run / authored.name
                if authored.is_symlink() or not authored.is_file():
                    raise DeriveError("review-notes-input-invalid")
            elif not report.is_file():
                raise DeriveError("review-notes-missing:review-notes.md")
            try:
                original = authored.read_bytes()
                notes = original.decode("utf-8")
                if authored != report:
                    validator = _load_module(
                        "codex_rig_review_validator", "../skills/code-review/validate_artifacts.py"
                    )
                    validator._require_notes_sections(authored, text=notes)
                source = f"## Reviewed Source\n\nReviewed commit: {value}"
                source_sections = re.findall(
                    r"(?ms)^##[ \t]+Reviewed Source(?:[ \t]+#+)?[ \t]*\r?$(.*?)(?=^#{1,2}[ \t]+|\Z)", notes
                )
                commit_lines = [line for line in notes.splitlines() if line.startswith("Reviewed commit:")]
                if not source_sections and not commit_lines:
                    notes += "\n\n" + source + "\n"
                elif (
                    len(source_sections) != 1
                    or commit_lines != [f"Reviewed commit: {value}"]
                    or commit_lines[0] not in source_sections[0].splitlines()
                ):
                    raise DeriveError("review-commit-report-mismatch")
                if measured_code_safety is not None:
                    field = f"Code safety: {measured_code_safety['assessment']}"
                    if measured_code_safety.get("reason"):
                        field += "\nReason: " + measured_code_safety["reason"]
                    # Preserve the original report bytes, including CRLF, when refreshing the derived field on retry.
                    marker = r"(?m)^## Code Safety\r?\n\r?\n(Code safety: (?:safe|unsafe|unassessed)(?:\r?\nReason: [^\r\n]+)?)\r?$"
                    matches = list(re.finditer(marker, notes))
                    if "## Code Safety" not in notes.splitlines():
                        notes += "\n## Code Safety\n\n" + field + "\n"
                    elif len(matches) == 1 and notes.splitlines().count("## Code Safety") == 1:
                        start, end = matches[0].span(1)
                        notes = notes[:start] + field + notes[end:]
                    else:
                        raise DeriveError("review-code-safety-report-mismatch")
                notes = _derive_review_actions(notes, metadata, handoff)
                # Validate normalized presentation rules and source identities before replacing either retained view.
                final_handoff._validate_handoff(handoff, derive_source_counts=True)
                encoded = notes.encode("utf-8")
                if authored != report:
                    prior = report.read_bytes() if report.exists() else None
                    if prior is not None and prior not in (original, encoded):
                        raise DeriveError("review-notes-input-conflict")
                    if prior is None:
                        with report.open("xb") as output:
                            output.write(encoded)
                    elif prior != encoded:
                        # Replacing the directory entry also preserves a draft hard-linked to the canonical report.
                        with tempfile.TemporaryDirectory(dir=run) as staging:
                            temporary = Path(staging) / report.name
                            temporary.write_bytes(encoded)
                            temporary.chmod(report.stat().st_mode)
                            temporary.replace(report)
                    if notes_rollback is not None and prior != encoded:
                        notes_rollback.callback(_restore_authored_notes, report, encoded, prior, report.lstat())
                elif encoded != original:
                    report.write_bytes(encoded)
            except (OSError, UnicodeError) as error:
                raise DeriveError("review-commit-report-unavailable") from error
        return handoff
    handoff["artifacts"] = _artifacts(run, handoff.get("artifacts") or [], artifact_path)
    if handoff.get("branch") == "caller-contract":
        return handoff
    workplan = metadata.get("resolution_workplan")
    if isinstance(workplan, dict) and workplan.get("production_lifecycle"):
        validator = _load_module("codex_rig_validate_artifacts", "validate-artifacts.py")
        grouped_disposition = validator._expected_grouped_commit_disposition(metadata, run)
        if grouped_disposition is not None:
            handoff["commit_disposition"] = grouped_disposition
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


def _derive_review_actions(notes: str, metadata: dict[str, Any], handoff: dict[str, Any]) -> str:
    """Synchronize canonical action content without inventing status, identity, or source bindings.

    Only an unambiguous existing notes table is rewritten. Historical incomplete records remain with their reader;
    ambiguous identities or contradictory authored statuses need a decision before any presentation is changed.
    """
    records = metadata.get("review_findings", []) + metadata.get("operational_blockers", [])
    heading = "Review Findings and Merge Blocks"
    sections = list(re.finditer(rf"^## {heading}[^\S\r\n]*\r?$\n(?P<body>.*?)(?=^## |\Z)", notes, re.M | re.S))
    if metadata.get("finding_records_version") == 2 and not records:
        if any(len(re.findall(r"(?m)^[ \t]*\|[^\r\n]*\|[ \t]*\r?$", section["body"])) > 2 for section in sections):
            raise DeriveError("review-action-identity-coverage-mismatch")
    if metadata.get("finding_records_version") not in {1, 2} or not records:
        return notes
    if any(not isinstance(record, dict) or "required_change" not in record for record in records):
        return notes
    by_id = {}
    for record in records:
        identity = record.get("id")
        if not isinstance(identity, str) or not identity or identity in by_id:
            raise DeriveError("review-action-record-identity-invalid")
        for field in ("title", "required_change"):
            if not isinstance(record.get(field), str) or not record[field].strip():
                raise DeriveError(f"review-action-record-invalid:{identity}:{field}")
        evidence = record.get("evidence")
        if (
            not isinstance(evidence, list)
            or not evidence
            or not all(isinstance(v, str) and v.strip() for v in evidence)
        ):
            raise DeriveError(f"review-action-record-invalid:{identity}:evidence")
        by_id[identity] = record
    tables = [table for table in handoff.get("tables", []) if table.get("heading") == heading]
    if len(tables) != 1:
        raise DeriveError("review-action-table-required")
    rows = tables[0].get("rows", [])
    identities = []
    for row in rows:
        cells = row.get("cells") if isinstance(row, dict) else None
        if not isinstance(cells, list) or len(cells) != 4 or not all(isinstance(cell, str) for cell in cells):
            raise DeriveError("review-action-row-invalid")
        identities.append(cells[0])
    if len(identities) != len(by_id) or set(identities) != set(by_id):
        raise DeriveError("review-action-identity-coverage-mismatch")
    if len(sections) != 1:
        raise DeriveError("review-action-notes-section-ambiguous")
    section = sections[0]
    blocks = list(re.finditer(r"(?:^[ \t]*\|[^\r\n]*\|[ \t]*(?:\r?\n|$))+", section["body"], re.M))
    if len(blocks) != 1:
        raise DeriveError("review-action-notes-table-ambiguous")
    block = blocks[0]
    parsed = [
        [cell.replace(r"\|", "|").strip() for cell in re.split(r"(?<!\\)\|", line.strip()[1:-1])]
        for line in block[0].splitlines()
    ]
    authored_status = {cells[0]: cells[3] for cells in (row["cells"] for row in rows)}
    if len(parsed) != len(rows) + 2 or len({cells[0] for cells in parsed[2:]}) != len(rows):
        raise DeriveError("review-action-notes-identity-mismatch")
    if any(len(cells) not in {4, 5} or authored_status.get(cells[0]) != cells[-1] for cells in parsed[2:]):
        raise DeriveError("review-action-notes-status-mismatch")
    if handoff.get("presentation_version") in {7, 8, 9, 10, 11, 12, 13, 14, 15, 16}:
        detail_sections = list(re.finditer(r"(?ms)^## Finding Details[^\S\r\n]*\r?\n.*?(?=^## |\Z)", notes))
        if len(detail_sections) > 1:
            raise DeriveError("review-report-details-ambiguous")
        if detail_sections and "<!-- codex-rig:review-details -->" not in detail_sections[0][0]:
            raise DeriveError("review-report-details-unowned")
    notes = _render_review_action_views(
        notes,
        section.start("body") + block.start(),
        section.start("body") + block.end(),
        rows,
        by_id,
        metadata.get("reviewer_assessments") is not None,
        role_only=handoff.get("presentation_version") in {7, 8, 9, 10, 11, 12, 13, 14, 15, 16},
    )
    if handoff.get("presentation_version") in {7, 8, 9, 10, 11, 12, 13, 14, 15, 16}:
        # Keep authored prose byte-exact while owning one deterministic, complete detail section.
        details = final_handoff.render_review_report_details(handoff) + "\n"
        sections = list(re.finditer(r"(?ms)^## Finding Details[^\S\r\n]*\r?\n.*?(?=^## |\Z)", notes))
        if sections:
            start, end = sections[0].span()
        else:
            start = end = notes.index(f"## {heading}")
        notes = notes[:start] + details + notes[end:]
    return notes


def _render_review_action_views(
    notes: str,
    start: int,
    end: int,
    rows: list[dict[str, Any]],
    records: dict[str, Any],
    attributed: bool,
    *,
    role_only: bool = False,
) -> str:
    """Render both action views from checked identities while preserving surrounding notes bytes."""
    columns = ["Finding / area", *(["Author"] if attributed else []), "Required change", "Evidence", "Status"]
    rendered = ["| " + " | ".join(columns) + " |", "| " + " | ".join("---" for _ in columns) + " |"]
    for row in rows:
        record = records[row["cells"][0]]
        row["cells"][1:3] = [record["required_change"], "; ".join(record["evidence"])]
        for field in ("title", "summary", "closure_evidence", "authors"):
            if field in record:
                row[field] = record[field]
            else:
                row.pop(field, None)
        cells = row["cells"].copy()
        if attributed:
            authors = record.get("authors")
            if not isinstance(authors, list) or not authors or not all(isinstance(author, str) for author in authors):
                raise DeriveError(f"review-action-record-invalid:{record['id']}:authors")
            cells.insert(
                1, ", ".join(author.removesuffix(" (parent substitute)") if role_only else author for author in authors)
            )
        rendered.append(
            "| "
            + " | ".join(cell.replace("\r\n", "\n").replace("\n", "<br>").replace("|", r"\|") for cell in cells)
            + " |"
        )
    return notes[:start] + "\n".join(rendered) + "\n" + notes[end:]


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
        code_safety=getattr(arguments, "code_safety", "unassessed"),
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
        if name == "derive" and isinstance(error, final_handoff.HandoffError):
            summary["errors"].append(
                {
                    "step": name,
                    "code": code,
                    "hint": f"Correct {code} in the handoff draft using its retained evidence.",
                }
            )
        summary["next_action"]["action"] = (
            "Repair the listed handoff draft fields from the retained review evidence, then rerun finalization."
            if code == "handoff-draft-invalid"
            else f"Inspect the retained {name} diagnostic and canonical inputs; repair only evidenced bookkeeping drift. "
            "Keep source/provenance failures open, preserve rejected evidence, and continue independently authorized "
            "source-verified work. Do not retry unchanged or invent missing evidence."
        )
        notes_hint = {
            "review-notes-missing": "Write the authored report to review-notes.md or pass its run-local file with --review-notes.",
            "review-notes-input-invalid": "Pass an existing regular authored notes file directly inside this run.",
            "review-notes-input-outside-run": "Place the authored notes directly inside this run and pass that filename.",
            "review-notes-input-conflict": "Reconcile the authored text with the existing review-notes.md, then finalize from that canonical report.",
            "missing-review-note-sections": "Complete the listed report sections from retained evidence, then rerun with the authored notes.",
        }.get(code.partition(":")[0])
        if name == "derive" and notes_hint:
            summary["errors"].append({"step": name, "code": code, "hint": notes_hint})
            summary["next_action"]["action"] = notes_hint
        return False
    summary["steps"].append({"step": name, "status": "pass", **({"detail": detail} if detail else {})})
    return True


def finalize(arguments: argparse.Namespace) -> dict[str, Any]:
    """Keep separately authored review notes recoverable until the result passes both validators."""
    with ExitStack() as notes_rollback:
        summary = _finalize(arguments, notes_rollback)
        if summary["status"] == "pass":
            notes_rollback.pop_all()
        return summary


def _finalize(arguments: argparse.Namespace, notes_rollback: ExitStack) -> dict[str, Any]:
    """Derive, render, write, validate, and optionally promote one remediation result candidate."""
    if arguments.no_publication_offer and (not arguments.promote or arguments.skill != "code-remediate"):
        raise DeriveError("--no-publication-offer requires code-remediate final promotion (--promote)")
    if arguments.review_notes is not None and arguments.skill != "code-review":
        raise DeriveError("--review-notes requires an assessed code-review")
    run = arguments.run.resolve()
    summary: dict[str, Any] = {"status": "pass", "steps": [], "errors": [], "not_run": [], "promoted": False}
    summary["next_action"] = {
        "owner": arguments.skill,
        "action": "Inspect validation results before delivering the result.",
        "resume_condition": "Both artifact validators pass before promotion; review also requires --complete-run.",
    }
    state: dict[str, Any] = {}

    def derive() -> dict[str, str]:
        if arguments.skill == "code-remediate" and not arguments.promote:
            # Only the completed validation below can attest the candidate produced by this attempt.
            try:
                (run / worktree_publication.CANDIDATE_VALIDATION_FILE).unlink(missing_ok=True)
            except OSError as error:
                raise DeriveError("publication-candidate-validation-cannot-refresh") from error
        draft = _load(arguments.metadata)
        handoff_draft = _load(arguments.handoff)
        if arguments.review_notes is not None and handoff_draft.get("branch") != "assessed":
            raise DeriveError("--review-notes requires an assessed code-review")
        _prepare_handoff_draft(arguments.skill, draft, handoff_draft, summary["errors"])
        publication = None
        if arguments.skill == "code-remediate":
            draft.pop("worktree_publication", None)
            draft.pop("worktree_publication_contract_version", None)
            # Fold the final events into both machine and displayed counts before validating either consumer.
            render_ledger(run, draft)
        if arguments.skill == "code-review" and (
            draft.get("scope") != "pr" or draft.get("review_status") in {"closed", "unavailable"}
        ):
            draft.pop("code_safety", None)
        elif arguments.skill == "code-review":
            from review_code_safety import review_record

            try:
                draft["code_safety"] = review_record(run, draft, arguments.code_safety)
            except (OSError, ValueError, TypeError, KeyError) as error:
                raise DeriveError(f"review-code-safety-unavailable:{error}") from error
        state["metadata"] = draft
        handoff = derive_handoff(
            run,
            state["metadata"],
            handoff_draft,
            arguments.confidence,
            arguments.artifact_path,
            measured_code_safety=draft.get("code_safety") if arguments.skill == "code-review" else None,
            review_notes=arguments.review_notes,
            notes_rollback=notes_rollback,
        )
        if arguments.skill == "code-remediate":
            # Eligibility depends on final selected outcomes folded from the ledger, even when hosted CI keeps
            # the overall result failed. The artifact validator still checks the complete result before any offer.
            eligible = worktree_publication.publication_result_eligible(
                run,
                {
                    "status": arguments.status,
                    "checks_failed": [item for item in arguments.extra_checks_failed.split(",") if item.strip()],
                    "findings": {"critical": arguments.critical},
                    "metadata": draft,
                },
            )
            if (
                arguments.promote
                and eligible
                and (
                    arguments.status == "pass" or (handoff.get("commit_disposition") or {}).get("status") == "committed"
                )
                and (os.path.lexists(run / "remediation-worktree.json") or publication_evidence_present(run))
            ):
                if draft.get("mode") != "pr":
                    raise DeriveError("publication-requires-pr-mode")
                publication = publication_binding(run)
            state["publication_action"] = _publication_next_step(
                run,
                handoff,
                offer_eligible=eligible and draft.get("mode") == "pr" and not arguments.no_publication_offer,
            )
        if publication is not None:
            draft["worktree_publication_contract_version"] = PUBLICATION_CONTRACT_VERSION
            draft["worktree_publication"] = publication
            handoff["artifacts"].append(publication_artifact(publication, arguments.artifact_path))
        if arguments.skill == "code-review" and handoff.get("branch") != "assessed":
            final_handoff.validate_handoff(handoff)
        _write(run / "final-handoff.json", handoff)
        state["handoff"] = handoff
        return {"handoff": str(run / "final-handoff.json")}

    def write_result() -> dict[str, str]:
        writer = _load_module("codex_rig_write_result", "write-result.py")
        payload = writer.build_payload(
            _result_arguments(arguments, run, _load(run / "gates.json"), state["metadata"]),
            measured_code_safety=state["metadata"].get("code_safety") if arguments.skill == "code-review" else None,
        )
        encoded = _write(run / "result.candidate.json", payload)
        if arguments.skill == "code-review":
            state["review_result_sha256"] = hashlib.sha256(encoded).hexdigest()
        if (
            not arguments.promote
            and state.get("publication_action")
            and os.path.lexists(run / "remediation-worktree.json")
        ):
            state["publication_candidate_validation"] = {
                "schema_version": 1,
                "candidate_sha256": hashlib.sha256(encoded).hexdigest(),
                "gates_sha256": hashlib.sha256((run / "gates.json").read_bytes()).hexdigest(),
            }
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
        summary, "review-validate", lambda: _review_validate(arguments, run, summary)
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
    summary["next_action"]["action"] = (
        "Inspect every reported check and repair its evidenced cause before revalidation; preserve retry limits."
        if report["status"] != "pass"
        else "Run find-review-report.py --complete-run for this run, then resume its waiting remediation if any."
        if arguments.skill == "code-review" and arguments.promote
        else "Deliver the validated final.md."
        if arguments.promote
        else "Promote the validated candidate before delivery."
    )
    if report["status"] == "pass" and "publication_candidate_validation" in state:
        # Exclusive creation prevents a concurrent writer or symlink from redirecting the attestation.
        try:
            with (run / worktree_publication.CANDIDATE_VALIDATION_FILE).open(
                "x", encoding="utf-8", newline="\n"
            ) as stream:
                json.dump(state["publication_candidate_validation"], stream, indent=2)
                stream.write("\n")
        except OSError:
            summary["status"] = "fail"
            summary["steps"].append(
                {
                    "step": "attest-publication-candidate",
                    "status": "fail",
                    "code": "publication-candidate-validation-unavailable",
                }
            )
            summary["next_action"]["action"] = (
                "Inspect the candidate-validation receipt and checkout slot before retrying validation."
            )
            return summary
    if report["status"] == "pass" and state.get("publication_action"):
        prefix = (
            "Deliver the validated final.md, then enter the post-delivery PR worktree publication gate."
            if arguments.promote
            else "Show the validated local handover, then enter the initial PR worktree publication gate before promotion."
            if os.path.lexists(run / "remediation-worktree.json")
            else "Promote the validated candidate, show the final handover, then enter the PR worktree publication gate."
        )
        summary["next_action"]["action"] = f"{prefix} {state['publication_action']}"
    if report["status"] == "pass" and arguments.promote:
        os.replace(run / "result.candidate.json", run / "result.json")
        summary["promoted"] = True
        if arguments.skill == "code-remediate":
            summary.update(_manual_push_command(run, state["metadata"].get("worktree_publication", {})))
            if "manual_push_command" in summary:
                summary["next_action"]["action"] = (
                    "Deliver the validated local handoff. In Recommendations / next steps, show manual_push_command "
                    "as an optional later push from the retained worktree."
                )
        summary["result"] = str(run / "result.json")
        if arguments.skill == "code-review":
            summary["review_result_sha256"] = state["review_result_sha256"]
    return summary


def _review_validate(arguments: argparse.Namespace, run: Path, summary: dict[str, Any]) -> dict[str, Any]:
    """Retain all review diagnostics and require valid parent-thread provenance before promotion."""
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
    completed = subprocess.run(command, capture_output=True, text=True, check=False)  # noqa: S603 - argv list, no shell
    try:
        report = json.loads(completed.stdout)
    except json.JSONDecodeError:
        lines = (completed.stderr or completed.stdout).strip().splitlines()
        raise DeriveError("review-validator:" + (lines[-1] if lines else str(completed.returncode))) from None
    if completed.returncode != 0 or report.get("status") != "pass":
        validator = _load_module("codex_rig_validate_artifacts", "validate-artifacts.py")
        summary["errors"] = [
            {**error, "hint": validator.validation_hint(error["code"])} for error in report.get("errors", [])
        ]
        summary["not_run"] = report.get("not_run", [])
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


def _manual_push_command(run: Path, publication: dict[str, Any]) -> dict[str, str]:
    """Show the user how to publish a declined, verified offer without reopening its approval."""
    if publication.get("status") != "declined":
        return {}
    try:
        answer, _ = _publication_record(run, "publication-answer.json")
        if answer.get("answer") != "Deny":
            return {}
        offer = worktree_publication.read_offer(run, answer["offer_digest"])
        original = run.parent if run.name == worktree_publication.REQUEST_DIRECTORY else run
        worktree_publication._verify_requested_pr(original, offer)
    except (DeriveError, ValueError, OSError, KeyError, TypeError):
        # A historical disposition alone cannot identify an executable command.
        return {}
    argv = [
        "git",
        "-C",
        offer["checkout"],
        "push",
        "--no-follow-tags",
        "--recurse-submodules=no",
        offer["url"],
        f"{offer['source_oid']}:{offer['ref']}",
    ]
    if os.name == "nt":
        # PowerShell accepts typographic as well as ASCII single quotes as delimiters.
        args = ["'" + re.sub(r"['\u2018\u2019\u201a\u201b]", lambda m: "'" + m[0], arg) + "'" for arg in argv[1:]]
        return {"manual_push_command": "git " + " ".join(args)}
    return {"manual_push_command": shlex.join(argv)}


def _publication_resume_action(arguments: argparse.Namespace) -> dict[str, Any]:
    """Read retained evidence for report-only resume without contacting GitHub or writing run state."""
    if arguments.run.is_symlink():
        raise DeriveError("publication-checkpoint-symlink")
    run = arguments.run.resolve()
    if run.name == worktree_publication.REQUEST_DIRECTORY and not os.path.lexists(run / "publication-offer.json"):
        # Directory creation claims the single follow-up before the offer is written. A crash there sent nothing.
        if not run.is_dir() or any(
            os.path.lexists(run / name)
            for name in (
                "publication-answer.json",
                "publication-attempt.json",
                "publication-executed",
                "publication-execution-result.json",
                "publication-readback.json",
                "result.json",
            )
        ):
            raise DeriveError("publication-offer-missing")
        delivery = None
        if os.path.lexists(run / "publication.json"):
            publication = _publication_state(run, {}, resume=True)
        else:
            original_result, digest = _publication_record(run.parent, "result.json")
            metadata = original_result.get("metadata")
            if (
                not worktree_publication.publication_result_eligible(run.parent, original_result)
                or not isinstance(metadata, dict)
                or metadata.get("mode") != "pr"
            ):
                raise DeriveError("publication-promoted-delivery-required")
            publication = {
                "schema_version": 1,
                "status": "skipped",
                "reason": "The follow-up ended before an offer was recorded.",
                "observed_oid": None,
                "evidence": "../result.json",
                "evidence_sha256": digest,
            }
    else:
        if run.name == worktree_publication.REQUEST_DIRECTORY:
            _, digest = _publication_record(run, "publication-offer.json")
            try:
                offer = worktree_publication.read_offer(run, digest)
                delivery = worktree_publication.requested_delivery(run, offer)
            except (ValueError, OSError) as error:
                raise DeriveError(str(error)) from error
        else:
            delivery = _publication_delivery(run)
        publication = _publication_state(run, delivery, resume=True)
    return {
        "status": "pass",
        "delivery": delivery,
        "evidence": publication["evidence"],
        "publication": publication,
        **_manual_push_command(run, publication),
        "needs_terminal_record": publication["status"] in {"skipped", "verified", "declined"}
        and publication["evidence"] != "publication.json",
    }


def _obligations_action(arguments: argparse.Namespace) -> dict[str, Any]:
    """Expose exact report occurrences for attaching to coherent inventory items without choosing scope."""
    run = arguments.run.resolve()
    validator = _load_module("codex_rig_validate_artifacts", "validate-artifacts.py")
    report = _load(run / "findings-input.txt")
    if report.get("schema_version") != 3 or report.get("metadata", {}).get("review_status", "assessed") not in {
        None,
        "assessed",
    }:
        raise DeriveError("code-remediate-report-input-not-assessed")
    finding_sources = []
    finding_source_gaps = []
    producer = arguments.producer_result_path or report.get("artifact_path")
    metadata = report.get("metadata", {})
    groups = [(field, metadata.get(field, [])) for field in ("review_findings", "operational_blockers")]
    identities = [
        record.get("id")
        for _, records in groups
        if isinstance(records, list)
        for record in records
        if isinstance(record, dict) and isinstance(record.get("id"), str)
    ]
    for field, records in groups:
        if not isinstance(records, list):
            finding_source_gaps.append({"field": field, "reason": "records-invalid"})
            continue
        for index, record in enumerate(records):
            coordinate = {"field": field, "index": index}
            if not isinstance(record, dict):
                finding_source_gaps.append({**coordinate, "reason": "record-invalid"})
                continue
            identity = record.get("id")
            source_id = f"{producer}#{identity}"
            if (
                not isinstance(identity, str)
                or identities.count(identity) != 1
                or not isinstance(producer, str)
                or source_id != source_id.strip()
                or re.search(r"[\r\n\[\]]", source_id)
                or re.fullmatch(r".+?\.json#[^#\s]+", source_id, re.IGNORECASE) is None
            ):
                finding_source_gaps.append({**coordinate, "reason": "identity-invalid-or-ambiguous"})
                continue
            fields = ("title", "summary", "required_change", "closure_evidence")
            required = fields if field == "review_findings" else ("title", "required_change")
            evidence = record.get("evidence")
            values = [record[key] for key in fields if key in record]
            if (
                any(not isinstance(record.get(key), str) or not record[key].strip() for key in required)
                or any(not isinstance(value, str) for value in values)
                or not isinstance(evidence, list)
                or not evidence
                or any(not isinstance(value, str) or not value.strip() for value in evidence)
            ):
                finding_source_gaps.append({**coordinate, "reason": "details-unavailable"})
                continue
            finding_sources.append(
                {
                    "kind": "report",
                    "source_id": source_id,
                    "finding_id": identity,
                    "location": "general",
                    "body": "\n".join([*values, *evidence]),
                    "evidence": producer,
                }
            )
    payload = {
        "schema_version": 1,
        "sources": validator.report_obligation_sources(report, arguments.producer_result_path),
    }
    path = run / "report-obligations.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")
    return {
        "status": "pass",
        "obligations": str(path),
        "occurrences": len(payload["sources"]),
        "finding_sources": finding_sources,
        "finding_source_gaps": finding_source_gaps,
    }


def _preflight_action(arguments: argparse.Namespace) -> dict[str, Any]:
    """Check the next consumer's structural prerequisites without requiring finished remediation."""
    run = arguments.run.resolve()
    validator = _load_module("codex_rig_validate_artifacts", "validate-artifacts.py")
    inventory = _load(run / "selection.json")
    final_handoff._validate_selection(inventory)
    if (run / "pr").exists():
        # PR relevance is required at finalization; catch missing triage before the scope prompt or edits.
        relevance = inventory.get("pr_relevance")
        if not isinstance(relevance, dict):
            raise DeriveError("preflight-pr-relevance-missing")
        if relevance.get("evaluated") is not True:
            raise DeriveError("preflight-pr-relevance-not-evaluated")
        keys = (
            "connected_open_items_total",
            "connected_selectable_items_total",
            "connected_required_followup_total",
            "connected_items_marked_out_of_scope",
        )
        if any(type(relevance.get(key)) is not int or relevance[key] < 0 for key in keys):
            raise DeriveError("preflight-pr-relevance-count-invalid")
        if relevance["connected_items_marked_out_of_scope"] > 0 or (
            relevance["connected_selectable_items_total"] + relevance["connected_required_followup_total"]
            < relevance["connected_open_items_total"]
        ):
            raise DeriveError("preflight-pr-relevance-coverage-invalid")
        action_path = run / "action-items.md"
        action_text = action_path.read_text(encoding="utf-8").lower() if action_path.is_file() else ""
        if "pr relevance summary" not in action_text:
            raise DeriveError("preflight-pr-relevance-summary-missing")
        if relevance["connected_open_items_total"] and not any(
            relation in action_text for relation in ("direct-diff", "pr-intent", "adjacent", "unknown")
        ):
            raise DeriveError("preflight-pr-relevance-relation-missing")
    if (run / "pr").exists():
        merge_metadata: dict[str, Any] = {}
        derive_merge(run, merge_metadata)
        validator._validate_code_remediate_merge_resolution(
            merge_metadata,
            run / "pr",
            _load(run / "pr" / "target-branch.json"),
            _load(run / "pr" / "pr-routing.json").get("head_oid"),
            require_integrated=True,
        )
    if arguments.stage == "selection":
        if arguments.report:
            context_sources = validator.report_obligation_sources(
                _load(run / "findings-input.txt"), arguments.producer_result_path
            )
            validator._validate_code_remediate_report_coverage(
                {
                    "final_resolution_table": {"items": inventory["items"]},
                    "review_report_intake": {
                        "admission_evidence": {"producer_result_path": arguments.producer_result_path},
                        "obligation_records_version": 2,
                        "context_sources": context_sources,
                        "review_limitations": inventory.get("review_limitations", []),
                    },
                },
                run,
                occurrence_contract=True,
            )
    else:
        if arguments.metadata is None:
            raise DeriveError("preflight-plan-metadata-required")
        metadata = _load(arguments.metadata)
        derive_selection(run, metadata)
        derive_workplan(run, metadata)
        validator._validate_code_remediate_workplan(metadata, run, pre_edit=True)
    return {"status": "pass", "stage": arguments.stage}


#: Dispatch table from command-line action name to the function that performs it.
ACTIONS = {
    "obligations": _obligations_action,
    "preflight": _preflight_action,
    "metadata": _metadata_action,
    "workplan": _workplan_action,
    "ledger": _ledger_action,
    "publication-resume": _publication_resume_action,
    "append": lambda arguments: append_record(arguments.run.resolve(), arguments.ledger, arguments.record),
    "resolutions": lambda arguments: append_review_resolutions(
        arguments.run, arguments.review_run, arguments.sha, arguments.item_sha
    ),
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse one helper action and its options."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    actions = parser.add_subparsers(dest="action", required=True)
    obligations = actions.add_parser(
        "obligations",
        help="Derive report context and coverage-ready finding sources.",
        description="Write report context and return finding sources and any source-generation gaps as JSON.",
    )
    preflight = actions.add_parser("preflight", help="Validate selection or frozen planning before source edits.")
    preflight.add_argument("--stage", choices=("selection", "plan"), required=True)
    preflight.add_argument("--report", action="store_true", help="Require retained assessed report coverage.")
    for action in (obligations, preflight):
        action.add_argument(
            "--producer-result-path", help="Admitted producer coordinate when artifact_path is relative."
        )
    preflight.add_argument("--metadata", type=Path, help="Plan-stage metadata draft; outcomes are not required.")
    metadata = actions.add_parser("metadata", help="Merge derived fields into a metadata draft.")
    workplan = actions.add_parser("workplan", help="Rewrite the managed resolution-workplan.md sections.")
    ledger = actions.add_parser("ledger", help="Rewrite the action-items.md resolution table from items and events.")
    final = actions.add_parser("finalize", help="Derive, render, write, validate, and optionally promote a result.")
    publication_resume = actions.add_parser(
        "publication-resume", help="Read the retained local publication checkpoint."
    )
    append = actions.add_parser(
        "append",
        help="Append a staged <ledger>.rec record to a run ledger, then remove it.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "resolution-events.jsonl staging: one JSON object or a JSON array; not JSONL.\n"
            "Unknown fields are rejected. All events validate before any append.\n"
            "schema_version is optional and must be 1 when supplied. IDs must exist in the frozen plan.\n"
            "Item fields: kind=item, id, and one or more of the following:\n"
            + "\n".join(f"  {field}: {'|'.join(sorted(values))}" for field, values in ITEM_EVENT_ENUMS.items())
            + "\n  resolved_how, evidence: nonempty strings (not lists or objects).\n"
            "Bucket fields: kind=bucket, id, status: "
            + "|".join(sorted(BUCKET_STATUSES))
            + "; optional note: nonempty string.\n"
            "Examples (replace R1/B1 with retained item/bucket IDs):\n"
            '{"kind":"item","id":"R1","resolution_status":"resolved","owner_status":"resolved",'
            '"resolved_how":"Independent review completed.","evidence":"independent-review.json"}\n'
            '{"kind":"bucket","id":"B1","status":"verified"}\n'
            "closure-log.md staging: one Markdown closure block, not a status event.\n"
            "Rejection appends nothing and retains the staged file; diagnose against this schema before repair."
        ),
    )
    feedback = actions.add_parser(
        "resolutions", help="Append per-finding outcomes to the admitted review run's resolution.jsonl."
    )
    for action in (metadata, workplan, ledger, final, append, feedback, obligations, preflight, publication_resume):
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
    final.add_argument(
        "--handoff",
        type=Path,
        required=True,
        help="Handoff draft JSON. For assessed reviews, author branch, snapshot and action tables, source_records, "
        "remaining and next_steps. The selected skill, schema, source coverage counts and recorded outcome can be omitted.",
    )
    final.add_argument(
        "--skill",
        choices=("code-remediate", "code-review", "audit"),
        default="code-remediate",
        help="Owning workflow; audit derives shared fields and validates audit evidence; review adds its validator.",
    )
    final.add_argument(
        "--code-safety",
        choices=("safe", "unsafe", "unassessed"),
        default="unassessed",
        help="Code Review parent's explicit assessment, independently checked against the source.",
    )
    final.add_argument("--parent-thread-id", help="code-review: parent thread for review-validator provenance.")
    final.add_argument(
        "--review-notes",
        type=Path,
        metavar="FILE",
        help="Assessed code-review: use a complete authored notes file directly inside --run. A bare filename is "
        "relative to --run. Finalization validates the report headings and handoff before creating review-notes.md.",
    )
    final.add_argument("--codex-home", type=Path, help="code-review: actual Codex home for the review validator.")
    final.add_argument("--status", required=True, choices=("pass", "fail", "timeout"))
    final.add_argument("--confidence", type=float, required=True)
    final.add_argument("--artifact-path", required=True, help="Canonical result path, ending in result.json.")
    final.add_argument("--extra-checks-failed", default="", help="Comma-separated non-gate failed checks.")
    for severity in ("critical", "high", "medium", "low"):
        final.add_argument(f"--{severity}", type=int, default=0)
    final.add_argument("--recommendations", default="", help="JSON list or ||-separated recommendations.")
    final.add_argument("--follow-up", default="", help="JSON list or ||-separated follow-up items.")
    final.add_argument(
        "--no-publication-offer",
        action="store_true",
        help="At final promotion only, finish a report-only continuation or honor an explicit opt-out for the delivered commit.",
    )
    final.add_argument("--promote", action="store_true", help="Rename a passing candidate to result.json.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run one action and print its JSON summary."""
    arguments = parse_args(argv)
    try:
        summary = finalize(arguments) if arguments.action == "finalize" else ACTIONS[arguments.action](arguments)
    except (DeriveError, final_handoff.HandoffError, SystemExit) as error:
        summary = {"status": "fail", "code": str(error)}
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
