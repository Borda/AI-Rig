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

Use ``--require-clean`` when an acceptance gate must reject every valid non-clean outcome. Add ``--progress`` after each
completed round to print its cumulative severity table on stderr, leaving machine-readable stdout unchanged. Add
``--actions path/to/loop-actions.json`` after parent triage to bind every open finding to a resolution action.

## Outputs

The read-only CLI prints one JSON object. A valid ledger yields its deterministic summary and exit status zero; an
invalid ledger yields ``status=invalid``, error names, and exit status one. ``--require-clean`` preserves the valid JSON
summary but exits one unless its reason is ``clean``. Optional progress cells split old and new open findings, grouping
security with critical for display while retaining their distinct weights; no table is printed before a completed round.
Action validation checks record consistency, not whether a claimed fix or cause is true. No input files are modified.

## Failure

The contract blocks clean acceptance without current-snapshot independent coverage, stops plateau, nonconverging, and
capped loops, and rejects malformed records or history that loses a stable finding. Scope and authority checks for
structural or severe fixes remain with the parent workflow. A repeated fix without root-cause evidence is rejected by
the optional action validation.

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
_ACTION_ROOT_KEYS = {"schema_version", "rounds"}
_ACTION_ROUND_KEYS = {"index", "actions"}
_ACTION_KEYS = {"signature", "decision", "evidence", "owner", "next_action", "root_cause"}
_ROOT_CAUSE_KEYS = {"claim", "evidence", "falsification", "rejected_alternative"}
_ACTION_DECISIONS = {"fix", "escalate", "defer"}


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
        if score == 0:
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
    return {"status": "active", "reason": round_summaries[-1]["decision"], "scores": scores, "rounds": round_summaries}


def summarize_ledger(payload: object) -> dict[str, object]:
    """Summarize a valid ledger or raise ValueError with its validation errors."""
    errors = validate_ledger(payload)
    if errors:
        raise ValueError(";".join(errors))
    assert isinstance(payload, dict)
    return _summary_from_valid(payload)


def validate_actions(ledger: object, payload: object) -> list[str]:
    """Bind parent triage and resolution evidence to each open reviewed finding."""
    errors = validate_ledger(ledger)
    if errors:
        return errors
    assert isinstance(ledger, dict)
    root = _exact_keys(payload, _ACTION_ROOT_KEYS, "actions", errors)
    if root is None:
        return errors
    if root.get("schema_version") != 1 or isinstance(root.get("schema_version"), bool):
        errors.append("actions-schema-version-invalid")
    rounds = root.get("rounds")
    if not isinstance(rounds, list) or len(rounds) != len(ledger["rounds"]):
        errors.append("actions-rounds-mismatch")
        return errors
    prior_open: set[str] = set()
    occurrence_counts: dict[str, int] = {}
    for index, (review, action_value) in enumerate(zip(ledger["rounds"], rounds, strict=True), start=1):
        action_round = _exact_keys(action_value, _ACTION_ROUND_KEYS, f"round-{index}-actions", errors)
        if action_round is None:
            continue
        if action_round.get("index") != index or isinstance(action_round.get("index"), bool):
            errors.append(f"round-{index}-actions-index-invalid")
        items = action_round.get("actions")
        if not isinstance(items, list):
            errors.append(f"round-{index}-actions-list-required")
            continue
        open_findings = {
            finding["signature"]: finding
            for finding in review["findings"]
            if finding["disposition"] in _OPEN_DISPOSITIONS
        }
        observed: set[str] = set()
        for value in items:
            action = _exact_keys(value, _ACTION_KEYS, f"round-{index}-action", errors)
            if action is None:
                continue
            signature = action.get("signature")
            if not _text(signature):
                errors.append(f"round-{index}-action-signature-required")
                continue
            if signature in observed:
                errors.append(f"round-{index}-action-duplicate:{signature}")
            observed.add(signature)
            finding = open_findings.get(signature)
            if finding is None:
                errors.append(f"round-{index}-action-not-open:{signature}")
                continue
            decision = action.get("decision")
            if not isinstance(decision, str) or decision not in _ACTION_DECISIONS:
                errors.append(f"round-{index}-action-decision-invalid:{signature}")
            if finding["tier"] in {"security", "critical", "high"} and decision == "defer":
                errors.append(f"round-{index}-severe-finding-must-fix-or-escalate:{signature}")
            evidence = action.get("evidence")
            if not isinstance(evidence, list) or not evidence or any(not _text(item) for item in evidence):
                errors.append(f"round-{index}-action-evidence-required:{signature}")
            for field in ("owner", "next_action"):
                if not _text(action.get(field)):
                    errors.append(f"round-{index}-action-{field}-required:{signature}")
            root_cause = action.get("root_cause")
            if root_cause is not None:
                cause = _exact_keys(root_cause, _ROOT_CAUSE_KEYS, f"round-{index}-root-cause", errors)
                if cause is not None:
                    for field in _ROOT_CAUSE_KEYS:
                        if not _text(cause.get(field)):
                            errors.append(f"round-{index}-root-cause-{field}-required:{signature}")
            if signature in prior_open and decision == "fix" and root_cause is None:
                errors.append(f"round-{index}-repeat-root-cause-required:{signature}")
            if occurrence_counts.get(signature, 0) >= 2 and decision == "fix":
                errors.append(f"round-{index}-third-occurrence-must-stop:{signature}")
        for signature in open_findings.keys() - observed:
            errors.append(f"round-{index}-action-missing:{signature}")
        prior_open = set(open_findings)
        for signature in open_findings:
            occurrence_counts[signature] = occurrence_counts.get(signature, 0) + 1
    return errors


def _render_progress(payload: dict[str, Any], completed_rounds: int) -> str:
    """Render validated completed history with separate old and new open totals."""
    if not completed_rounds:
        return ""
    lines = [
        "| Iteration | Critical | High | Medium | Low | Nits | Weighted score |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    seen: set[str] = set()
    for round_record in payload["rounds"][:completed_rounds]:
        counts = {tier: [0, 0] for tier in ("critical", "high", "medium", "low", "nit")}
        weighted = [0, 0]
        for finding in round_record["findings"]:
            if finding["disposition"] not in _OPEN_DISPOSITIONS:
                continue
            bucket = 0 if finding["signature"] in seen else 1
            tier = finding["tier"]
            counts["critical" if tier == "security" else tier][bucket] += 1
            weighted[bucket] += _TIERS[tier]
        cells = [str(round_record["index"]), *(f"{old} + {new}" for old, new in [*counts.values(), weighted])]
        lines.append("| " + " | ".join(cells) + " |")
        # Closed signatures remain history so a later reopened finding is old, not newly discovered.
        seen.update(finding["signature"] for finding in round_record["findings"])
    lines.append("")
    lines.append(
        "Cells: old + new open findings (including pending verification); old = seen in any prior completed round. "
        "Critical includes security; weights: security 20, critical 10, high 6, medium 4, low 2, nit 1. "
        "Weighted score: old weighted + new weighted."
    )
    return "\n".join(lines)


def main() -> int:
    """Emit JSON validation and optionally a cumulative human-readable progress table."""
    parser = argparse.ArgumentParser(description="Validate an adversarial-review convergence ledger.")
    parser.add_argument("--ledger", required=True, type=Path, help="Ledger JSON path.")
    parser.add_argument("--require-clean", action="store_true", help="Exit nonzero unless the valid summary is clean.")
    parser.add_argument("--progress", action="store_true", help="Repeat the cumulative progress table on stderr.")
    parser.add_argument("--actions", type=Path, help="Validate per-finding parent actions against the ledger.")
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
    if not errors and args.actions is not None:
        try:
            with args.actions.open(encoding="utf-8") as handle:
                actions = json.load(handle)
        except (OSError, json.JSONDecodeError) as error:
            errors.append(f"actions-read-failed:{error}")
        else:
            errors.extend(validate_actions(payload, actions))
    if errors:
        print(
            json.dumps({"status": "invalid", "reason": "invalid-ledger", "errors": errors}, sort_keys=True),
            file=sys.stderr,
        )
        return 1
    summary = summarize_ledger(payload)
    if args.progress and summary["rounds"]:
        print(_render_progress(payload, len(summary["rounds"])), file=sys.stderr)
    print(json.dumps(summary, sort_keys=True))
    return 0 if not args.require_clean or summary["reason"] == "clean" else 1


if __name__ == "__main__":
    raise SystemExit(main())
