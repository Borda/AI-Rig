#!/usr/bin/env python3
"""Validate the bounded escalation state for one stalled workstream.

## Purpose

Make progress-stall escalation observable instead of relying on an agent's self-description. The validator prevents a
lifecycle owner from silently repeating work after the defined no-progress or non-closing trigger. It also preserves the
exact evidence needed for a human to choose a next step.

## Scope

This helper validates one ledger for one closure condition. It does not select a model, execute an advisory request,
modify project files, or decide whether the workstream should be accepted. Callers retain those decisions and record
only their observed outcome here. The ledger retains the user's primary goal and distinguishes primary work from
auxiliary setup/report repairs; auxiliary success cannot count as primary material progress. It validates declared
dependencies and state consistency, not the truth of a claimed user goal or the host's adherence to instructions.

Current schema three keeps work cycles append-only. `reasoning-progress.json` is a header holding every field except
`cycles` (goal, workstream, closure condition, outcome, advisory, recovery, handoff), rewritten in place as that state
changes. The sibling `reasoning-cycles.jsonl` holds one cycle object per line in index order and is only appended: a
header named `<prefix>progress.json` pairs with `<prefix>cycles.jsonl`. Historical schema two kept `cycles` inline in
one rewritten file; `--historical` validates such an archive as data under the same rules, while the default CLI rejects
it as an active ledger, as it rejects schema one, so an old shape cannot bypass the current contract.

## Usage

Record each work cycle by writing it as one JSON object to `<run-directory>/reasoning-cycles.jsonl.rec`, then running
`python PLUGIN_ROOT/shared/escalation_ledger.py --ledger <run-directory>/reasoning-progress.json --append`. The staged
cycle must continue the index sequence and satisfy the cycle rules; it is appended as one line and the staged file is
deleted. Run the same command without `--append` after recording each triggered escalation state in the header and
before another work cycle. A zero exit means the bounded state is internally consistent; a non-zero exit means the
caller must stop and repair the record or hand off.

## Outputs

The command prints `escalation-ledger-valid` on success, or `escalation-ledger-historical-valid` for a valid archive
read with `--historical`. On malformed, incomplete, unsafe, or unbounded state it prints
`escalation-ledger-invalid:<reason>` and exits with status 2. Validation never modifies files. `--append` adds exactly
one line to the cycle log and deletes the staged record; a rejected record is kept for inspection and nothing is
appended. A cycle that is appended is an observed fact even when the extended ledger then fails validation: the exit
status still requires escalation or repair of the header before another cycle.

## Failure

Validation rejects two consecutive cycles without material progress or three evidence-backed non-closing cycles that
remain marked as ordinary work. It also rejects advisory records without an observed read-only sandbox, advisor state
changes, a second recovery path, and incomplete human handoffs.

## Used by

The `implement`, `investigate`, and `code-remediate` lifecycle owners, plus `delegation-lead`, use this contract through
the canonical reasoning-progress policy. Regression tests and calibration fixtures exercise the same rules so shipped
advice-routing instructions and executable validation cannot drift apart.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 3
#: Historical single-file schema with every cycle inline; validated only as an archive, never as an active ledger.
HISTORICAL_SCHEMA_VERSION = 2
_OUTCOMES = {"working", "advisory", "recovery", "human_handoff", "closed"}


def _require_text(value: object, field: str) -> str:
    """Return a non-empty string field or raise a stable validation error."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field}-required")
    return value


def _require_mapping(value: object, field: str) -> dict[str, Any]:
    """Return a JSON object field or raise a stable validation error."""
    if not isinstance(value, dict):
        raise ValueError(f"{field}-object-required")
    return value


def cycles_path(ledger_path: Path) -> Path:
    """Return the append-only work-cycle log that belongs to one ledger header.

    Examples:
        >>> cycles_path(Path("run") / "reasoning-progress.json").name
        'reasoning-cycles.jsonl'
        >>> cycles_path(Path("ledger.json")).name
        'ledger-cycles.jsonl'
    """
    name = ledger_path.name
    prefix = name[: -len("progress.json")] if name.endswith("progress.json") else f"{ledger_path.stem}-"
    return ledger_path.with_name(f"{prefix}cycles.jsonl")


def _read_cycles(path: Path) -> list[object]:
    """Read one JSON value per line from a cycle log; an absent log has no recorded cycle."""
    if not path.is_file():
        return []
    cycles: list[object] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            raise ValueError(f"cycles-line-blank:{number}")
        try:
            cycles.append(json.loads(line))
        except json.JSONDecodeError as error:
            raise ValueError(f"cycles-line-invalid-json:{number}") from error
    return cycles


def load_ledger(path: Path) -> dict[str, Any]:
    """Assemble the ledger object that validation checks from its on-disk files.

    A schema-3 header gains the ``cycles`` list read from its sibling cycle log; a header that also carries inline
    cycles is rejected, because two copies of history could disagree. Any other JSON object, including a historical
    schema-2 file with inline cycles, is returned unchanged for validation to accept or reject.

    Raises:
        OSError: When the header or cycle log cannot be read.
        ValueError: When either file is not valid JSON, the header is not an object, or it duplicates its cycle log.
    """
    payload = json.loads(path.read_bytes())
    if not isinstance(payload, dict):
        raise ValueError("ledger-object-required")
    if payload.get("schema_version") != SCHEMA_VERSION:
        return payload
    if "cycles" in payload:
        raise ValueError("ledger-header-inline-cycles-forbidden")
    return {**payload, "cycles": _read_cycles(cycles_path(path))}


def append_cycle(ledger_path: Path) -> None:
    """Append the staged cycle record to the ledger's cycle log after validating the extended cycle sequence.

    Only the cycle sequence is checked here, so an observed stall can always be recorded; escalation state is checked
    by the following full validation, which then requires the owner to escalate before another cycle.

    Raises:
        ValueError: With a stable reason when the staged record or the extended sequence is invalid.
    """
    log = cycles_path(ledger_path)
    staged = log.with_name(f"{log.name}.rec")
    try:
        record = json.loads(staged.read_bytes())
    except OSError as error:
        raise ValueError(f"append-record-missing:{staged.name}") from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("append-record-invalid-json") from error
    if not isinstance(record, dict):
        raise ValueError("append-record-object-required")
    ledger = load_ledger(ledger_path)
    if ledger.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("append-requires-current-schema")
    _validate_cycles([*ledger["cycles"], record])
    with log.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    staged.unlink()


def _validate_cycles(value: object) -> list[dict[str, Any]]:
    """Validate ordered work-cycle records and return their object form."""
    if not isinstance(value, list) or not value:
        raise ValueError("cycles-required")

    cycles: list[dict[str, Any]] = []
    for expected_index, item in enumerate(value, start=1):
        cycle = _require_mapping(item, "cycle")
        if cycle.get("index") != expected_index:
            raise ValueError("cycle-index-must-be-contiguous")
        for field in ("objective", "operation", "outcome", "next_decision"):
            _require_text(cycle.get(field), f"cycle-{field}")
        if not isinstance(cycle.get("material_progress"), bool):
            raise ValueError("cycle-material-progress-boolean-required")
        evidence = cycle.get("evidence")
        if not isinstance(evidence, list) or any(not isinstance(entry, str) or not entry.strip() for entry in evidence):
            raise ValueError("cycle-evidence-list-required")
        if cycle["material_progress"] and not evidence:
            raise ValueError("material-progress-evidence-required")
        if cycle.get("work_kind") not in {"primary", "auxiliary"}:
            raise ValueError("cycle-work-kind-required")
        if cycle["work_kind"] == "auxiliary":
            _require_text(cycle.get("required_for"), "auxiliary-required-for")
            if cycle["material_progress"]:
                raise ValueError("auxiliary-work-is-not-primary-progress")
        cycles.append(cycle)
    return cycles


def _validate_advisory(value: object) -> None:
    """Validate that the single advisor was observed, read-only, and non-mutating."""
    advisory = _require_mapping(value, "advisory")
    for field in (
        "requested_model",
        "requested_effort",
        "observed_model",
        "observed_effort",
        "observed_sandbox",
        "mode",
        "recommendation",
        "stop_condition",
    ):
        _require_text(advisory.get(field), f"advisory-{field}")
    if advisory["observed_sandbox"] != "read-only":
        raise ValueError("advisory-read-only-sandbox-required")
    if advisory["mode"] != "advice-only":
        raise ValueError("advisory-advice-only-mode-required")
    if advisory.get("state_changes") != []:
        raise ValueError("advisory-state-changes-forbidden")


def _validate_handoff(value: object) -> None:
    """Validate the evidence and proposal required for a human handoff."""
    handoff = _require_mapping(value, "human_handoff")
    _require_text(handoff.get("summary"), "human-handoff-summary")
    _require_text(handoff.get("recommended_next_step"), "human-handoff-recommended-next-step")
    alternatives = handoff.get("alternatives")
    if not isinstance(alternatives, list) or not alternatives:
        raise ValueError("human-handoff-alternatives-required")
    if any(not isinstance(item, str) or not item.strip() for item in alternatives):
        raise ValueError("human-handoff-alternatives-invalid")


def validate_ledger(ledger: dict[str, Any], *, historical: bool = False) -> None:
    """Validate bounded retries, counting observed primary attempts independently of progress.

    Args:
        ledger: Assembled ledger object, as returned by ``load_ledger``.
        historical: Accept a schema-2 archive with inline cycles instead of a current schema-3 ledger.
    """
    expected_version = HISTORICAL_SCHEMA_VERSION if historical else SCHEMA_VERSION
    if ledger.get("schema_version") != expected_version:
        raise ValueError("unsupported-schema-version")
    _require_text(ledger.get("primary_goal"), "primary-goal")
    _require_text(ledger.get("workstream_id"), "workstream-id")
    closure_condition = _require_mapping(ledger.get("closure_condition"), "closure-condition")
    _require_text(closure_condition.get("id"), "closure-condition-id")
    closure_status = closure_condition.get("status")
    if closure_status not in {"open", "closed", "replaced"}:
        raise ValueError("closure-condition-status-invalid")
    if closure_status == "replaced":
        replacement = _require_mapping(closure_condition.get("replacement"), "closure-replacement")
        replacement_id = _require_text(replacement.get("id"), "closure-replacement-id")
        if replacement_id == closure_condition["id"]:
            raise ValueError("closure-replacement-must-change-condition")
        if replacement.get("source") not in {"user-direction", "external-state"}:
            raise ValueError("closure-replacement-source-invalid")
        evidence = replacement.get("evidence")
        if (
            not isinstance(evidence, list)
            or not evidence
            or any(not isinstance(item, str) or not item.strip() for item in evidence)
        ):
            raise ValueError("closure-replacement-evidence-required")

    cycles = _validate_cycles(ledger.get("cycles"))
    outcome = ledger.get("outcome")
    if outcome not in _OUTCOMES:
        raise ValueError("outcome-invalid")
    if outcome == "closed" and closure_status != "closed":
        raise ValueError("closed-outcome-requires-closed-condition")
    if closure_status == "closed" and outcome != "closed":
        raise ValueError("closed-condition-requires-closed-outcome")

    no_progress_trigger = len(cycles) >= 2 and all(not cycle["material_progress"] for cycle in cycles[-2:])
    # Auxiliary repair cannot reset earlier attempts; observed failures still count without material progress.
    primary_attempts = sum(cycle["work_kind"] == "primary" and bool(cycle["evidence"]) for cycle in cycles)
    nonclosing_trigger = closure_status == "open" and primary_attempts >= 3
    if (no_progress_trigger or nonclosing_trigger) and outcome == "working":
        raise ValueError("escalation-required-after-stall-trigger")

    advisory = ledger.get("advisory")
    recovery = ledger.get("recovery")
    handoff = ledger.get("human_handoff")
    if outcome in {"advisory", "recovery"}:
        _validate_advisory(advisory)
    elif advisory is not None:
        _validate_advisory(advisory)

    if recovery is not None:
        if outcome not in {"recovery", "human_handoff", "closed"}:
            raise ValueError("recovery-outcome-invalid")
        _validate_advisory(advisory)
        recovery_record = _require_mapping(recovery, "recovery")
        _require_text(recovery_record.get("action"), "recovery-action")
        if not isinstance(recovery_record.get("material_progress"), bool):
            raise ValueError("recovery-material-progress-boolean-required")
        if not isinstance(recovery_record.get("closure_met"), bool):
            raise ValueError("recovery-closure-met-boolean-required")
        if recovery_record["closure_met"] and outcome != "closed":
            raise ValueError("successful-recovery-must-close-workstream")
        if not recovery_record["closure_met"] and outcome != "human_handoff":
            raise ValueError("unsuccessful-recovery-requires-human-handoff")

    if outcome == "human_handoff":
        _validate_handoff(handoff)
    elif handoff is not None:
        raise ValueError("human-handoff-only-valid-for-handoff-outcome")


def main() -> int:
    """Run the escalation-ledger validator as a portable helper CLI."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", required=True, type=Path, help="Escalation ledger header JSON path.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--append", action="store_true", help="First append the staged cycle from <cycle log>.rec.")
    mode.add_argument("--historical", action="store_true", help="Validate a schema-2 archive with inline cycles.")
    args = parser.parse_args()
    try:
        if args.append:
            append_cycle(args.ledger)
        validate_ledger(load_ledger(args.ledger), historical=args.historical)
    except (OSError, ValueError) as error:
        print(f"escalation-ledger-invalid:{error}")
        return 2
    print("escalation-ledger-historical-valid" if args.historical else "escalation-ledger-valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
