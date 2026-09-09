#!/usr/bin/env python3
"""Write canonical Codex Rig result JSON with shared confidence invariants.

## Purpose

Normalize a workflow's outcome, checks, findings, and confidence recovery into a result candidate that the validator can
reconcile with evidence. The writer is the single place where CLI metadata becomes the canonical result JSON shape
consumed by later validation.

## Scope

It validates supplied CLI metadata and writes JSON only; it does not run gates, inspect source, or promote a candidate
to final output. Gate status and confidence-recovery fields are checked against the supplied ``gates.json`` before the
candidate is created.

## Usage

Invoke the CLI after gates and notes exist, then pass the candidate to ``validate-artifacts.py`` before renaming it to
``result.json``. Provide canonical gate IDs in ``--checks-run`` and ``--checks-failed`` plus metadata JSON containing
confidence gaps, closures, recovery evidence, actions, and remaining limits.

## Used by

Artifact-producing workflow skills and result-schema acceptance tests use this writer. Their downstream validators rely
on the writer's enum values and list normalization rather than reparsing workflow-specific command-line conventions.

## Outputs

It writes a deterministic schema-v2 candidate JSON with enum-validated status, check lists, findings, and metadata
supplied by the completed workflow. The payload includes ``status``, ``checks_run``, ``checks_failed``, finding counts,
final confidence, recommendations/follow-up lists, ``artifact_path``, and digest-bound final-handoff metadata.

## Failure

Invalid enum value, malformed metadata, missing confidence closure or final-handoff binding, or contradictory gate
evidence exits non-zero before a candidate exists. In particular, a pass with failed gates or critical findings, a
timeout without a failed check, and a confidence status inconsistent with its numeric band are rejected at write time.
"""

from __future__ import annotations

import argparse
import json
from enum import Enum
from pathlib import Path
from typing import Any


RESULT_SCHEMA_VERSION = 2
FINAL_HANDOFF_SCHEMA_VERSION = 1
FINAL_HANDOFF_METADATA_FIELDS = {
    "schema_version",
    "handoff_path",
    "handoff_sha256",
    "rendered_path",
    "rendered_sha256",
    "validation_path",
    "branch",
}
FINAL_HANDOFF_BRANCHES = {"standard", "assessed", "unavailable", "closed", "caller-contract"}


class ResultStatus(str, Enum):
    """Overall verdict recorded in the result payload and mirrored by gates.json.

    Subclasses ``str`` (not ``enum.StrEnum``) because ``requires-python`` is ``>=3.10``, so members serialise into
    result JSON as plain strings.
    """

    PASS = "pass"
    FAIL = "fail"
    TIMEOUT = "timeout"


class ClosureStatus(str, Enum):
    """How one declared confidence gap was closed out."""

    CLOSED = "closed"
    UNRESOLVED = "unresolved"
    DEFERRED = "deferred"


class RecoveryStatus(str, Enum):
    """Confidence band the recovery block claims for the run."""

    FAIR = "fair"
    CAUTIOUS_LOW = "cautious-low"
    VERY_QUESTIONABLE = "very-questionable"
    NOT_ACCEPTABLE_FAILED = "not-acceptable-failed"


def parse_items(raw: str) -> list[str]:
    """Parse a JSON array or delimiter-separated list into strings."""
    stripped = raw.strip()
    if not stripped:
        return []
    try:
        loaded = json.loads(stripped)
    except json.JSONDecodeError:
        return [item.strip() for item in stripped.split("||") if item.strip()]
    if isinstance(loaded, list):
        return [str(item).strip() for item in loaded if str(item).strip()]
    return [item.strip() for item in stripped.split("||") if item.strip()]


def parse_metadata(raw: str) -> dict[str, Any]:
    """Parse mandatory metadata JSON into an object."""
    stripped = raw.strip()
    if not stripped:
        return {}
    try:
        loaded = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid-metadata-json:{exc}") from exc
    if not isinstance(loaded, dict):
        raise SystemExit("invalid-metadata-json:not-object")
    return loaded


def require_string_list(payload: dict[str, Any], key: str) -> list[str]:
    """Return a required list of strings from a metadata object."""
    value = payload.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SystemExit(f"invalid-confidence-metadata:{key}")
    return value


def validate_confidence_gap_closures(metadata: dict[str, Any], confidence_gaps: list[str]) -> None:
    """Validate that each active confidence gap has closure evidence or rationale."""
    active_gaps = [gap.strip() for gap in confidence_gaps]
    if any(not gap for gap in active_gaps):
        raise SystemExit("invalid-confidence-gap")
    if len(active_gaps) != len(set(active_gaps)):
        raise SystemExit("duplicate-confidence-gap")
    closures = metadata.get("confidence_gap_closures")
    if not active_gaps:
        if closures not in (None, []):
            raise SystemExit("confidence-gap-closure-undeclared")
        return

    if not isinstance(closures, list):
        raise SystemExit("missing-confidence-gap-closures")

    closed_gaps: set[str] = set()
    for index, closure in enumerate(closures):
        if not isinstance(closure, dict):
            raise SystemExit(f"confidence-gap-closure-not-object:{index}")
        gap = closure.get("gap")
        if not isinstance(gap, str) or not gap.strip():
            raise SystemExit(f"confidence-gap-closure-missing-gap:{index}")
        closure_status = closure.get("status")
        if closure_status not in {s.value for s in ClosureStatus}:
            raise SystemExit(f"confidence-gap-closure-invalid-status:{index}")
        evidence = closure.get("evidence") or closure.get("evidence_path")
        rationale = closure.get("rationale")
        if closure_status == ClosureStatus.CLOSED and not (isinstance(evidence, str) and evidence.strip()):
            raise SystemExit(f"confidence-gap-closure-missing-evidence:{index}")
        unclosed = {ClosureStatus.UNRESOLVED, ClosureStatus.DEFERRED}
        if closure_status in unclosed and not (isinstance(rationale, str) and rationale.strip()):
            raise SystemExit(f"confidence-gap-closure-missing-rationale:{index}")
        normalized_gap = gap.strip()
        if normalized_gap not in active_gaps:
            raise SystemExit(f"confidence-gap-closure-undeclared:{index}")
        if normalized_gap in closed_gaps:
            raise SystemExit(f"confidence-gap-closure-duplicate:{normalized_gap}")
        closed_gaps.add(normalized_gap)

    missing = sorted(set(active_gaps) - closed_gaps)
    if missing:
        raise SystemExit("confidence-gap-closure-missing:" + ",".join(missing))


def validate_confidence_recovery(
    metadata: dict[str, Any], confidence: float, status: ResultStatus, checks_failed: list[str]
) -> None:
    """Validate the shared confidence-band recovery contract."""
    recovery = metadata.get("confidence_recovery")
    if not isinstance(recovery, dict):
        raise SystemExit("missing-confidence-recovery")

    initial = recovery.get("initial_confidence")
    final = recovery.get("final_confidence")
    if not isinstance(initial, int | float) or not 0.0 <= float(initial) <= 1.0:
        raise SystemExit("invalid-initial-confidence")
    if not isinstance(final, int | float) or not 0.0 <= float(final) <= 1.0:
        raise SystemExit("invalid-final-confidence")
    if abs(float(final) - confidence) > 0.001:
        raise SystemExit("confidence-recovery-final-mismatch")

    recovery_status = recovery.get("status")
    if recovery_status not in {s.value for s in RecoveryStatus}:
        raise SystemExit("invalid-confidence-recovery-status")

    evidence = require_string_list(recovery, "evidence")
    recovery_actions = require_string_list(recovery, "recovery_actions")
    remaining_limits = require_string_list(recovery, "remaining_limits")
    if not any(item.strip() for item in evidence):
        raise SystemExit("confidence-recovery-evidence-required")
    if not any(item.strip() for item in recovery_actions):
        raise SystemExit("confidence-recovery-actions-required")

    if confidence <= 0.8:
        if status == ResultStatus.PASS:
            raise SystemExit("pass-confidence-not-acceptable")
        if "confidence-not-acceptable" not in checks_failed:
            raise SystemExit("missing-confidence-not-acceptable-check")
        if recovery_status != RecoveryStatus.NOT_ACCEPTABLE_FAILED:
            raise SystemExit("confidence-status-should-fail")
        if not any(item.strip() for item in remaining_limits):
            raise SystemExit("low-confidence-remaining-limits-required")
        return

    if confidence < 0.85:
        if status == ResultStatus.PASS:
            raise SystemExit("pass-confidence-very-questionable")
        if "confidence-very-questionable" not in checks_failed:
            raise SystemExit("missing-confidence-very-questionable-check")
        if recovery_status != RecoveryStatus.VERY_QUESTIONABLE:
            raise SystemExit("confidence-status-should-be-very-questionable")
        if not any(item.strip() for item in remaining_limits):
            raise SystemExit("very-questionable-remaining-limits-required")
        return

    if confidence < 0.9:
        if recovery_status != RecoveryStatus.CAUTIOUS_LOW:
            raise SystemExit("confidence-status-should-be-cautious-low")
        if not any(item.strip() for item in remaining_limits):
            raise SystemExit("cautious-low-remaining-limits-required")
        return

    if recovery_status != RecoveryStatus.FAIR:
        raise SystemExit("confidence-status-should-be-fair")


def validate_confidence_metadata(
    metadata: dict[str, Any], confidence: float, status: ResultStatus, checks_failed: list[str]
) -> None:
    """Validate all confidence metadata required by the shared result contract."""
    if not metadata:
        raise SystemExit("missing-confidence-metadata")
    confidence_gaps = require_string_list(metadata, "confidence_gaps")
    if confidence < 1.0 and not any(item.strip() for item in confidence_gaps):
        raise SystemExit("confidence-gaps-required")
    validate_confidence_gap_closures(metadata, confidence_gaps)
    validate_confidence_recovery(metadata, confidence, status, checks_failed)


def validate_final_handoff_metadata(metadata: dict[str, Any]) -> None:
    """Require the exact digest binding created by the final-handoff renderer."""
    final_handoff = metadata.get("final_handoff")
    if not isinstance(final_handoff, dict):
        raise SystemExit("missing-final-handoff-metadata")
    missing = sorted(FINAL_HANDOFF_METADATA_FIELDS - set(final_handoff))
    unexpected = sorted(set(final_handoff) - FINAL_HANDOFF_METADATA_FIELDS)
    if missing:
        raise SystemExit("final-handoff-metadata-missing:" + ",".join(missing))
    if unexpected:
        raise SystemExit("final-handoff-metadata-unexpected:" + ",".join(unexpected))
    if final_handoff["schema_version"] != FINAL_HANDOFF_SCHEMA_VERSION:
        raise SystemExit("invalid-final-handoff-schema-version")
    for key in ("handoff_path", "rendered_path", "validation_path"):
        if not isinstance(final_handoff[key], str) or not final_handoff[key].strip():
            raise SystemExit(f"invalid-final-handoff-metadata:{key}")
    for key in ("handoff_sha256", "rendered_sha256"):
        value = final_handoff[key]
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise SystemExit(f"invalid-final-handoff-metadata:{key}")
    if final_handoff["branch"] not in FINAL_HANDOFF_BRANCHES:
        raise SystemExit("invalid-final-handoff-metadata:branch")


def split_csv(raw: str) -> list[str]:
    """Split a comma-separated argument into non-empty trimmed values."""
    return [item.strip() for item in raw.split(",") if item.strip()]


def validate_gate_evidence(args: argparse.Namespace, checks_run: list[str], checks_failed: list[str]) -> None:
    """Reconcile result inputs with mandatory five-gate evidence."""
    try:
        gates = json.loads(args.gates.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"missing-gates-json:{args.gates}") from exc
    if not isinstance(gates, dict) or not isinstance(gates.get("checks"), list):
        raise SystemExit("invalid-gates-json")
    expected_ids = ["lint", "format", "types", "tests", "review"]
    gate_ids = [check.get("id") for check in gates["checks"] if isinstance(check, dict)]
    if gate_ids != expected_ids or checks_run != expected_ids:
        raise SystemExit("result-gate-check-set-mismatch")
    gate_failures = gates.get("checks_failed")
    if not isinstance(gate_failures, list) or not set(gate_failures).issubset(checks_failed):
        raise SystemExit("result-gate-failure-mismatch")
    gate_status = gates.get("status")
    if gate_status not in {s.value for s in ResultStatus}:
        raise SystemExit("invalid-gate-status")
    if gate_status == ResultStatus.FAIL and args.status == ResultStatus.PASS:
        raise SystemExit("pass-with-failed-gates")
    if gate_status == ResultStatus.TIMEOUT and args.status != ResultStatus.TIMEOUT:
        raise SystemExit("result-status-timeout-mismatch")


def validate_result_invariants(args: argparse.Namespace, checks_run: list[str], checks_failed: list[str]) -> None:
    """Reject contradictory or ambiguous result status inputs."""
    if not checks_run:
        raise SystemExit("checks-run-required")
    if len(checks_run) != len(set(checks_run)):
        raise SystemExit("duplicate-checks-run")
    if len(checks_failed) != len(set(checks_failed)):
        raise SystemExit("duplicate-checks-failed")
    if args.status == ResultStatus.PASS and checks_failed:
        raise SystemExit("pass-with-failed-checks")
    if args.status == ResultStatus.PASS and args.critical > 0:
        raise SystemExit("pass-with-critical-findings")
    if args.status == ResultStatus.TIMEOUT and not checks_failed:
        raise SystemExit("timeout-without-failed-check")


def validate_artifact_path(out_path: Path, artifact_path: str) -> None:
    """Require schema-v2 output to name its run's final result before promotion."""
    declared = Path(artifact_path)
    run_root = out_path.parent.resolve()
    expected = (out_path.parent / "result.json").resolve()
    if not expected.is_relative_to(run_root):
        raise SystemExit("result-artifact-path-mismatch")
    # Anchor on the run directory, never the caller's. The declared path was written by an earlier
    # process whose directory is not recorded with it, so probing this one made the same artifact
    # acceptable from one place and rejected from another. Ancestors cover runs written before the
    # output-relative convention; the equality check below still admits only this run's result.
    run_directory = out_path.parent
    candidates = (
        [declared]
        if declared.is_absolute()
        else [run_directory / declared, *(ancestor / declared for ancestor in run_directory.resolve().parents)]
    )
    if not any(candidate.resolve() == expected for candidate in candidates):
        raise SystemExit("result-artifact-path-mismatch")


def build_payload(args: argparse.Namespace) -> dict[str, Any]:
    """Build and validate the canonical result payload from parsed arguments."""
    checks_run = split_csv(args.checks_run)
    checks_failed = split_csv(args.checks_failed)
    validate_result_invariants(args, checks_run, checks_failed)
    validate_artifact_path(args.out, args.artifact_path)
    validate_gate_evidence(args, checks_run, checks_failed)
    metadata = parse_metadata(args.metadata)
    status = ResultStatus(args.status)
    validate_confidence_metadata(metadata, args.confidence, status, checks_failed)
    validate_final_handoff_metadata(metadata)

    payload = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "status": status.value,
        "checks_run": checks_run,
        "checks_failed": checks_failed,
        "findings": {
            "critical": args.critical,
            "high": args.high,
            "medium": args.medium,
            "low": args.low,
        },
        "confidence": args.confidence,
        "artifact_path": args.artifact_path,
        "metadata": metadata,
    }
    if metadata.get("review_status") not in {"unavailable", "closed"}:
        payload["recommendations"] = parse_items(args.recommendations)
        payload["follow_up"] = parse_items(args.follow_up)
    return payload


def parse_args() -> argparse.Namespace:
    """Parse command line arguments for result writing."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path, help="Candidate or final result JSON to write.")
    parser.add_argument("--gates", required=True, type=Path, help="Canonical gates.json evidence path.")
    parser.add_argument(
        "--status", required=True, choices=[s.value for s in ResultStatus], help="Overall result status."
    )
    parser.add_argument("--checks-run", required=True, help="Comma-separated canonical gate IDs.")
    parser.add_argument("--checks-failed", default="", help="Comma-separated failed check IDs.")
    parser.add_argument("--critical", type=int, default=0, help="Critical finding count.")
    parser.add_argument("--high", type=int, default=0, help="High finding count.")
    parser.add_argument("--medium", type=int, default=0, help="Medium finding count.")
    parser.add_argument("--low", type=int, default=0, help="Low finding count.")
    parser.add_argument("--confidence", required=True, type=float, help="Final confidence in [0, 1].")
    parser.add_argument("--artifact-path", required=True, help="Canonical result path recorded in the payload.")
    parser.add_argument("--recommendations", default="", help="JSON list or ||-separated recommendations.")
    parser.add_argument("--follow-up", default="", help="JSON list or ||-separated follow-up items.")
    parser.add_argument("--metadata", default="", help="JSON object containing required confidence metadata.")
    return parser.parse_args()


def main() -> int:
    """Write the result JSON and print the output path."""
    args = parse_args()
    if not 0.0 <= args.confidence <= 1.0:
        raise SystemExit("invalid-confidence")
    for field in ("critical", "high", "medium", "low"):
        if getattr(args, field) < 0:
            raise SystemExit(f"invalid-finding-count:{field}")

    payload = build_payload(args)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
