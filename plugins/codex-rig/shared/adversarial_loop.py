#!/usr/bin/env python3
"""Validate deterministic evidence for a bounded adversarial review loop.

## Purpose

Keep the review-and-fix convergence rule executable so a lifecycle owner cannot relabel self-review as independent, lose
an open finding, or continue after a terminal convergence decision. The helper computes scores from recorded findings
rather than trusting a supplied total.

## Scope

Validate one JSON ledger with at most three independent review rounds. It checks record shape and consistency, but does
not establish that a named reviewer, revision, digest, or report really exists; those fields are traceability evidence
for an external owner to inspect.

## Usage

Run ``python shared/adversarial_loop.py --ledger path/to/ledger.json``. Import ``validate_ledger`` for a list of stable
validation errors, or ``summarize_ledger`` for the inferred status, reason, and open-finding scores.

Use ``--require-clean`` when an acceptance gate must reject every valid non-clean outcome.

## Outputs

The read-only CLI prints one JSON object. A valid ledger yields its deterministic summary and exit status zero; an
invalid ledger yields ``status=invalid``, error names, and exit status one. ``--require-clean`` preserves the valid JSON
summary but exits one unless its reason is ``clean``. No input files are modified.

## Failure

The contract blocks clean acceptance without current-snapshot independent coverage, stops structural, repeated-open,
plateau, nonconverging, and capped loops, and rejects malformed records or history that loses a stable finding.

## Used by

Review lifecycle owners and their tests use this small helper before deciding whether to fix, stop, or request a
separately scoped review run. It deliberately contains no resume override, dispatch mechanism, or provenance API.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
MAX_ROUNDS = 3
_DISPOSITIONS = {"open", "fixed-pending-verification", "verified-fixed", "rejected"}
_TIERS = {"security": 20, "critical": 10, "high": 6, "medium": 4, "low": 2, "nit": 1}
_OPEN_DISPOSITIONS = {"open", "fixed-pending-verification"}
_DIGEST = re.compile(r"[0-9a-f]{64}")
_ROOT_KEYS = {"schema_version", "implementation_author", "current_snapshot", "rounds"}
_ROUND_KEYS = {"index", "reviewer", "snapshot", "report_path", "findings"}
_REVIEWER_KEYS = {"identity", "independent"}
_SNAPSHOT_KEYS = {"revision", "diff_digest"}
_FINDING_KEYS = {"signature", "tier", "structural", "disposition", "evidence"}


def _text(value: object) -> bool:
    """Return whether a value is a non-empty text field."""
    return isinstance(value, str) and bool(value.strip())


def _exact_keys(value: object, expected: set[str], name: str, errors: list[str]) -> dict[str, Any] | None:
    """Validate an object has exactly the documented JSON fields."""
    if not isinstance(value, dict):
        errors.append(f"{name}-object-required")
        return None
    missing = expected - value.keys()
    unexpected = value.keys() - expected
    if missing:
        errors.append(f"{name}-missing-fields:{','.join(sorted(str(key) for key in missing))}")
    if unexpected:
        errors.append(f"{name}-unexpected-fields:{','.join(sorted(str(key) for key in unexpected))}")
    return value


def _validate_snapshot(value: object, name: str, errors: list[str]) -> dict[str, Any] | None:
    """Validate one named immutable snapshot coordinate."""
    snapshot = _exact_keys(value, _SNAPSHOT_KEYS, name, errors)
    if snapshot is None:
        return None
    if not _text(snapshot.get("revision")):
        errors.append(f"{name}-revision-required")
    digest = snapshot.get("diff_digest")
    if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
        errors.append(f"{name}-diff-digest-invalid")
    return snapshot


def _validate_finding(value: object, round_index: int, errors: list[str]) -> dict[str, Any] | None:
    """Validate one stable finding record without asserting provenance of its evidence."""
    finding = _exact_keys(value, _FINDING_KEYS, f"round-{round_index}-finding", errors)
    if finding is None:
        return None
    if not _text(finding.get("signature")):
        errors.append(f"round-{round_index}-finding-signature-required")
    tier = finding.get("tier")
    if not isinstance(tier, str) or tier not in _TIERS:
        errors.append(f"round-{round_index}-finding-tier-invalid")
    if not isinstance(finding.get("structural"), bool):
        errors.append(f"round-{round_index}-finding-structural-boolean-required")
    disposition = finding.get("disposition")
    if not isinstance(disposition, str) or disposition not in _DISPOSITIONS:
        errors.append(f"round-{round_index}-finding-disposition-invalid")
    evidence = finding.get("evidence")
    if not isinstance(evidence, list) or any(not _text(item) for item in evidence):
        errors.append(f"round-{round_index}-finding-evidence-list-required")
    elif isinstance(disposition, str) and disposition in {"rejected", "verified-fixed"} and not evidence:
        errors.append(f"round-{round_index}-{disposition}-finding-evidence-required")
    return finding


def _validate_rounds(payload: dict[str, Any], errors: list[str]) -> list[dict[str, Any]]:
    """Validate review-round shape, independence claims, and carried finding history."""
    value = payload.get("rounds")
    if not isinstance(value, list):
        errors.append("rounds-list-required")
        return []
    if len(value) > MAX_ROUNDS:
        errors.append("round-limit-exceeded")
    author = payload.get("implementation_author")
    prior_findings: dict[str, dict[str, Any]] = {}
    rounds: list[dict[str, Any]] = []
    for expected_index, value_round in enumerate(value, start=1):
        round_record = _exact_keys(value_round, _ROUND_KEYS, f"round-{expected_index}", errors)
        if round_record is None:
            continue
        rounds.append(round_record)
        index = round_record.get("index")
        if not isinstance(index, int) or isinstance(index, bool) or index != expected_index:
            errors.append("round-index-must-be-contiguous-integer")
        reviewer = _exact_keys(round_record.get("reviewer"), _REVIEWER_KEYS, f"round-{expected_index}-reviewer", errors)
        if reviewer is not None:
            if not _text(reviewer.get("identity")):
                errors.append(f"round-{expected_index}-reviewer-identity-required")
            if not isinstance(reviewer.get("independent"), bool):
                errors.append(f"round-{expected_index}-reviewer-independent-boolean-required")
            elif reviewer["independent"] and reviewer.get("identity") == author:
                errors.append(f"round-{expected_index}-self-review-forbidden")
        _validate_snapshot(round_record.get("snapshot"), f"round-{expected_index}-snapshot", errors)
        if not _text(round_record.get("report_path")):
            errors.append(f"round-{expected_index}-report-path-required")
        findings_value = round_record.get("findings")
        if not isinstance(findings_value, list):
            errors.append(f"round-{expected_index}-findings-list-required")
            continue
        current_findings: dict[str, dict[str, Any]] = {}
        for value_finding in findings_value:
            finding = _validate_finding(value_finding, expected_index, errors)
            if finding is None or not _text(finding.get("signature")):
                continue
            signature = finding["signature"]
            if signature in current_findings:
                errors.append(f"round-{expected_index}-finding-signature-duplicate:{signature}")
            current_findings[signature] = finding
        for signature, prior in prior_findings.items():
            current = current_findings.get(signature)
            if current is None:
                errors.append(f"round-{expected_index}-finding-dropped:{signature}")
            elif current.get("tier") != prior.get("tier"):
                errors.append(f"round-{expected_index}-finding-tier-changed:{signature}")
            elif current.get("structural") != prior.get("structural"):
                errors.append(f"round-{expected_index}-finding-structural-changed:{signature}")
        prior_findings = current_findings
    return rounds


def validate_ledger(payload: object) -> list[str]:
    """Return deterministic contract errors for one adversarial-review ledger."""
    errors: list[str] = []
    root = _exact_keys(payload, _ROOT_KEYS, "ledger", errors)
    if root is None:
        return errors
    version = root.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool) or version != SCHEMA_VERSION:
        errors.append("unsupported-schema-version")
    if not _text(root.get("implementation_author")):
        errors.append("implementation-author-required")
    _validate_snapshot(root.get("current_snapshot"), "current-snapshot", errors)
    rounds = _validate_rounds(root, errors)
    if not errors:
        summary = _summary_from_valid(root)
        reviewed_rounds = summary["rounds"]
        assert isinstance(reviewed_rounds, list)
        terminal_round = len(reviewed_rounds)
        if summary["reason"] == "independence-unavailable":
            author = root["implementation_author"]
            assert isinstance(author, str)
            terminal_round = next(
                (
                    index
                    for index, round_record in enumerate(rounds, start=1)
                    if not _is_independent_round(round_record, author)
                ),
                0,
            )
        if summary["status"] == "stopped" and len(rounds) > terminal_round:
            errors.append("round-after-stop")
    return errors


def _is_independent_round(round_record: dict[str, Any], author: str) -> bool:
    """Return whether a structurally valid record declares non-self independent coverage."""
    reviewer = round_record.get("reviewer")
    return (
        isinstance(reviewer, dict)
        and reviewer.get("independent") is True
        and _text(reviewer.get("identity"))
        and reviewer.get("identity") != author
    )


def _score(round_record: dict[str, Any]) -> int:
    """Compute the weighted total of open and pending-verification findings."""
    findings = round_record["findings"]
    return sum(_TIERS[finding["tier"]] for finding in findings if finding["disposition"] in _OPEN_DISPOSITIONS)


def _summary_from_valid(payload: dict[str, Any]) -> dict[str, object]:
    """Infer a status from a structurally valid ledger without rerunning validation."""
    rounds = payload["rounds"]
    assert isinstance(rounds, list)
    if not rounds:
        return {"status": "stopped", "reason": "independence-unavailable", "scores": [], "rounds": []}

    author = payload["implementation_author"]
    assert isinstance(author, str)
    scores: list[int] = []
    round_summaries: list[dict[str, object]] = []
    previous_open: set[str] = set()
    for position, round_record in enumerate(rounds, start=1):
        assert isinstance(round_record, dict)
        if not _is_independent_round(round_record, author):
            return {
                "status": "stopped",
                "reason": "independence-unavailable",
                "scores": scores,
                "rounds": round_summaries,
            }
        findings = round_record["findings"]
        assert isinstance(findings, list)
        counts = {tier: 0 for tier in _TIERS}
        for finding in findings:
            if finding["disposition"] in _OPEN_DISPOSITIONS:
                counts[finding["tier"]] += 1
        score = _score(round_record)
        scores.append(score)
        decision = "baseline" if position == 1 else "converging"
        open_signatures = {finding["signature"] for finding in findings if finding["disposition"] in _OPEN_DISPOSITIONS}
        if any(finding["structural"] and finding["disposition"] in _OPEN_DISPOSITIONS for finding in findings):
            decision = "structural-finding"
        elif previous_open & open_signatures:
            decision = "consecutive-open-finding"
        elif score == 0:
            snapshot = round_record["snapshot"]
            if snapshot != payload["current_snapshot"]:
                decision = "stale-review"
            else:
                decision = "clean"
        elif position > 1:
            ratio = score / scores[-2]
            if ratio >= 1:
                decision = "nonconverging"
            elif ratio > 0.5:
                decision = "plateau"
        if position == MAX_ROUNDS and decision == "converging":
            decision = "round-cap"
        round_summaries.append({"index": position, "score": score, "counts": counts, "decision": decision})
        if decision not in {"baseline", "converging"}:
            return {"status": "stopped", "reason": decision, "scores": scores, "rounds": round_summaries}
        previous_open = open_signatures
    return {"status": "active", "reason": round_summaries[-1]["decision"], "scores": scores, "rounds": round_summaries}


def summarize_ledger(payload: object) -> dict[str, object]:
    """Summarize a valid ledger or raise ValueError with its validation errors."""
    errors = validate_ledger(payload)
    if errors:
        raise ValueError(";".join(errors))
    assert isinstance(payload, dict)
    return _summary_from_valid(payload)


def main() -> int:
    """Read one ledger and emit a read-only JSON validation result."""
    parser = argparse.ArgumentParser(description="Validate an adversarial-review convergence ledger.")
    parser.add_argument("--ledger", required=True, type=Path, help="Ledger JSON path.")
    parser.add_argument("--require-clean", action="store_true", help="Exit nonzero unless the valid summary is clean.")
    args = parser.parse_args()
    try:
        with args.ledger.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        print(
            json.dumps({"status": "invalid", "reason": "ledger-read-failed", "errors": [str(error)]}, sort_keys=True),
            file=sys.stderr,
        )
        return 1
    errors = validate_ledger(payload)
    if errors:
        print(
            json.dumps({"status": "invalid", "reason": "invalid-ledger", "errors": errors}, sort_keys=True),
            file=sys.stderr,
        )
        return 1
    summary = summarize_ledger(payload)
    print(json.dumps(summary, sort_keys=True))
    return 0 if not args.require_clean or summary["reason"] == "clean" else 1


if __name__ == "__main__":
    raise SystemExit(main())
