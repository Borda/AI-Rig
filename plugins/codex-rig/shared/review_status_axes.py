"""Separate a Code Review result's overall status into gate, review and decision axes.

## Purpose

A Code Review ``result.json`` carries one ``status`` (``pass``, ``fail`` or ``timeout``) that used to be raised by a
failed gate, by blocked verification readiness, by missing independent coverage or by a low confidence score, often with
an empty ``checks_failed`` list. A reader could not tell an executed gate failure from an unfinished verification or
from a source verdict. This module records the three axes explicitly in ``metadata.status_axes`` and checks that the
overall status is explained by named, closed-set failed checks.

## Scope

It performs pure checks on values the caller supplies; the closed check identifiers live in this module, and the
packaged ``review-status-axes.json`` holds the same identifiers plus the truth-table fixtures, read only on demand. It
never reads run artifacts, never changes a result and never runs gates.

## Usage

``build_status_axes`` derives the block the shared result writer stores for every current Code Review result.
``check_status_axes`` validates a block together with the result's status, failed checks, verification readiness,
critical finding count and independent-coverage state; the shared writer and both artifact validators call it through
``check_result_status_axes``, which derives the coverage state from the result metadata. Results without the block are
earlier saved runs and keep their original readers.

## Outputs

``build_status_axes`` returns the ``status_axes`` dictionary. ``check_status_axes`` returns nothing on success.

## Failure

``check_status_axes`` raises ``SystemExit`` with a stable ``status-axes-*`` code naming the first violated rule.

## Used by

``write-result.py`` (writer), ``validate-artifacts.py`` (shared validator) and the Code Review skill validator
``skills/code-review/validate_artifacts.py``.
"""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Any

#: Packaged truth-table file holding the closed failed-check identifiers and the contract fixtures.
AXES_TABLE_PATH = Path(__file__).with_name("review-status-axes.json")
#: Version family of the ``metadata.status_axes`` block written into Code Review results.
STATUS_AXES_SCHEMA_VERSION = 1
#: Keys of a ``metadata.status_axes`` block, exactly.
STATUS_AXES_KEYS = frozenset({"schema_version", "gates_status", "review_status", "review_decision"})
#: Allowed gate-axis values, mirroring ``gates.json`` status.
GATES_STATUSES = frozenset({"pass", "fail", "timeout"})
#: Allowed review-axis values; ``assessed`` is the explicit name of a result whose metadata omits ``review_status``.
REVIEW_STATUSES = frozenset({"assessed", "unavailable", "closed"})
#: Allowed verification readiness values of an assessed review; ``None`` for terminal results.
READINESS_VALUES = frozenset({"complete", "blocked", "incomplete"})


#: Failed-check identifiers that name one of the five executed quality gates.
GATE_CHECK_IDS: frozenset[str] = frozenset({"lint", "format", "types", "tests", "review"})
#: Identifier naming open verification readiness.
READINESS_CHECK_ID = "verification-readiness"
#: Identifier naming confirmed critical findings.
CRITICAL_CHECK_ID = "critical-findings"
#: Identifier naming required independent specialist coverage that was not established.
COVERAGE_CHECK_ID = "independent-review-coverage"
#: Identifier naming gates the source guard refused before they executed.
REFUSAL_CHECK_ID = "source-refused"
#: Identifiers that explain a non-passing gate axis: an executed gate failure or a source refusal.
GATE_CAUSE_IDS: frozenset[str] = GATE_CHECK_IDS | {REFUSAL_CHECK_ID}
#: Closed set of failed-check identifiers a current Code Review result may name; mirrored by the packaged table.
CHECK_IDS: frozenset[str] = GATE_CAUSE_IDS | {READINESS_CHECK_ID, CRITICAL_CHECK_ID, COVERAGE_CHECK_ID}


class CoverageState(str, Enum):
    """What the result metadata proves about independent specialist coverage.

    ``required-unmet``: the user explicitly required independent review and it was not satisfied. ``not-established``:
    independent coverage was not established, or the parent substituted for a reviewer. ``established``: every triggered
    required role had validated independent coverage and nothing was substituted. ``unknown``: the metadata carries no
    coverage evidence.
    """

    REQUIRED_UNMET = "required-unmet"
    NOT_ESTABLISHED = "not-established"
    ESTABLISHED = "established"
    UNKNOWN = "unknown"


def coverage_state(metadata: dict[str, Any]) -> str:
    """Derive the coverage state from ``independence_required``, ``independence_satisfied`` and ``fanout_substituted``.

    Examples:
        >>> coverage_state({"independence_required": True, "independence_satisfied": False})
        'required-unmet'
        >>> coverage_state({"independence_required": False, "independence_satisfied": False})
        'not-established'
        >>> coverage_state({"independence_satisfied": True, "fanout_substituted": False})
        'established'
        >>> coverage_state({})
        'unknown'
    """
    required = metadata.get("independence_required")
    satisfied = metadata.get("independence_satisfied")
    if required is True and satisfied is not True:
        return CoverageState.REQUIRED_UNMET.value
    if satisfied is False or metadata.get("fanout_substituted") is True:
        return CoverageState.NOT_ESTABLISHED.value
    if satisfied is True:
        return CoverageState.ESTABLISHED.value
    return CoverageState.UNKNOWN.value


def truth_table() -> list[dict[str, Any]]:
    """Return the packaged truth-table rows, read on demand so validation never depends on the data file.

    Raises:
        SystemExit: when the packaged table is missing its version or disagrees with the closed identifiers here.
    """
    payload = json.loads(AXES_TABLE_PATH.read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != STATUS_AXES_SCHEMA_VERSION
        or set(payload.get("check_ids", {})) != CHECK_IDS
        or set(payload.get("gate_check_ids", [])) != GATE_CHECK_IDS
    ):
        raise SystemExit("status-axes-table-invalid")
    rows = payload.get("truth_table")
    if not isinstance(rows, list):
        raise SystemExit("status-axes-table-invalid")
    return rows


def review_status_axis(metadata: dict[str, Any]) -> str:
    """Name the review axis, mapping an omitted ``review_status`` to ``assessed``.

    Examples:
        >>> review_status_axis({"review_decision": {}})
        'assessed'
        >>> review_status_axis({"review_status": "closed"})
        'closed'
    """
    value = metadata.get("review_status")
    return "assessed" if value is None else str(value)


def build_status_axes(gates_status: str, metadata: dict[str, Any]) -> dict[str, Any]:
    """Derive the stored axis block from the gate evidence status and the result metadata.

    Examples:
        >>> build_status_axes("pass", {"review_decision": {"recommendation": "MAJOR_REVISION"}})["review_decision"]
        'MAJOR_REVISION'
        >>> build_status_axes("fail", {"review_status": "unavailable"})["review_decision"] is None
        True
    """
    review = review_status_axis(metadata)
    decision = metadata.get("review_decision")
    recommendation = decision.get("recommendation") if review == "assessed" and isinstance(decision, dict) else None
    return {
        "schema_version": STATUS_AXES_SCHEMA_VERSION,
        "gates_status": gates_status,
        "review_status": review,
        "review_decision": recommendation,
    }


def _check_shape(axes: object) -> dict[str, Any]:
    """Require the exact versioned block with closed axis values."""
    if not isinstance(axes, dict) or set(axes) != STATUS_AXES_KEYS:
        raise SystemExit("status-axes-shape-invalid")
    if axes["schema_version"] != STATUS_AXES_SCHEMA_VERSION:
        raise SystemExit("status-axes-schema-version-invalid")
    if axes["gates_status"] not in GATES_STATUSES:
        raise SystemExit("status-axes-gates-status-invalid")
    if axes["review_status"] not in REVIEW_STATUSES:
        raise SystemExit("status-axes-review-status-invalid")
    decision = axes["review_decision"]
    if decision is not None and (not isinstance(decision, str) or not decision.strip()):
        raise SystemExit("status-axes-review-decision-invalid")
    return axes


def _check_terminal(axes: dict[str, Any], status: str) -> None:
    """Keep terminal dispositions free of a source decision and an unavailable review failed."""
    if axes["review_status"] == "assessed":
        return
    if axes["review_decision"] is not None:
        raise SystemExit("status-axes-terminal-decision")
    if axes["review_status"] == "unavailable" and status != "fail":
        raise SystemExit("status-axes-unavailable-must-fail")


def _check_coverage(named: set[str], coverage: str, status: str) -> None:
    """Cross-check the independent-coverage failed check against the coverage evidence the metadata carries.

    Naming the check requires coverage evidence in the metadata; a result without any refuses it. A failing review
    whose explicit independence requirement is unmet must name it. A passing review with that requirement unmet is
    refused by the review validator, which names the uncovered roles.

    Two directions stay with the review validator, which sees the reviewer passes, risk tier and execution mode that
    the result metadata does not carry. Naming the check while required roles were satisfied is not refused: a
    multi-role review that ran its independent reviewers without overlap still cannot pass and names this cause. A
    shortfall without an explicit requirement is not forced to be named: the low-risk parent-only route passes with it.
    """
    if coverage not in CoverageState._value2member_map_:
        raise SystemExit("status-axes-coverage-invalid")
    if COVERAGE_CHECK_ID in named and coverage == CoverageState.UNKNOWN.value:
        raise SystemExit("status-axes-coverage-evidence-missing")
    if status != "pass" and coverage == CoverageState.REQUIRED_UNMET.value and COVERAGE_CHECK_ID not in named:
        raise SystemExit("status-axes-coverage-unnamed")


def _check_named_gates(gates_status: str, named: set[str]) -> None:
    """Require a nonpassing gate cause and keep source refusal exclusive with execution failures."""
    if gates_status != "pass" and not named & GATE_CAUSE_IDS:
        raise SystemExit("status-axes-gate-failure-unnamed")
    if gates_status == "pass" and named & GATE_CAUSE_IDS:
        raise SystemExit("status-axes-gate-named-while-passing")
    if REFUSAL_CHECK_ID in named and named & GATE_CHECK_IDS:
        raise SystemExit("status-axes-refusal-with-executed")


def _check_named_review_causes(
    review_status: str, status: str, named: set[str], readiness: str | None, critical: int, coverage: str
) -> None:
    """Reconcile review causes against finding counts, readiness and independent coverage."""
    if critical > 0 and CRITICAL_CHECK_ID not in named:
        raise SystemExit("status-axes-critical-findings-unnamed")
    if critical == 0 and CRITICAL_CHECK_ID in named:
        raise SystemExit("status-axes-critical-findings-absent")
    if review_status != "assessed":
        return
    if readiness not in READINESS_VALUES:
        raise SystemExit("status-axes-readiness-invalid")
    if readiness != "complete" and READINESS_CHECK_ID not in named:
        raise SystemExit("status-axes-readiness-unnamed")
    if readiness == "complete" and READINESS_CHECK_ID in named:
        raise SystemExit("status-axes-readiness-not-open")
    _check_coverage(named, coverage, status)
    if status == "fail" and not named:
        raise SystemExit("status-axes-fail-without-named-check")


def check_status_axes(
    axes: object,
    *,
    status: str,
    checks_failed: list[str],
    readiness: str | None,
    critical: int,
    coverage: str = CoverageState.UNKNOWN.value,
) -> None:
    """Validate an axis block against the overall status and its named failed checks.

    The overall ``status`` is ``timeout`` exactly when the gates timed out, ``pass`` only with no failed check, and
    ``fail`` otherwise; a failing assessed review names every cause from the closed set. Confidence never appears in
    that set, so a confidence score alone never fails a review. ``coverage`` is a ``CoverageState`` value; its
    default ``unknown`` refuses a named independent-coverage check, since nothing then shows the shortfall.

    Examples:
        >>> axes = build_status_axes("pass", {"review_decision": {"recommendation": "APPROVE"}})
        >>> check_status_axes(axes, status="pass", checks_failed=[], readiness="complete", critical=0)
        >>> check_status_axes(axes, status="fail", checks_failed=[], readiness="complete", critical=0)
        Traceback (most recent call last):
        SystemExit: status-axes-fail-without-named-check
        >>> check_status_axes(axes, status="fail", checks_failed=["independent-review-coverage"],
        ...                   readiness="complete", critical=0)
        Traceback (most recent call last):
        SystemExit: status-axes-coverage-evidence-missing
    """
    block = _check_shape(axes)
    if status not in GATES_STATUSES:
        raise SystemExit("status-axes-status-invalid")
    named = set(checks_failed)
    unknown = sorted(named - CHECK_IDS)
    if unknown:
        raise SystemExit("status-axes-unknown-check:" + ",".join(unknown))
    if (status == "timeout") != (block["gates_status"] == "timeout"):
        raise SystemExit("status-axes-timeout-mismatch")
    if status == "pass" and block["gates_status"] != "pass":
        raise SystemExit("status-axes-pass-with-failed-gates")
    if status == "pass" and checks_failed:
        raise SystemExit("status-axes-pass-with-failed-checks")
    _check_terminal(block, status)
    _check_named_gates(block["gates_status"], named)
    _check_named_review_causes(block["review_status"], status, named, readiness, critical, coverage)


def _check_gate_receipt(failed: list[str], receipt: dict[str, Any]) -> None:
    """Reconcile each gate cause against the corresponding list in the receipt."""
    executed = receipt.get("checks_failed")
    refused = receipt.get("checks_refused", [])
    if not isinstance(executed, list) or not all(isinstance(item, str) and item in GATE_CHECK_IDS for item in executed):
        raise SystemExit("status-axes-gate-receipt-invalid")
    if not isinstance(refused, list) or not all(isinstance(item, str) and item in GATE_CHECK_IDS for item in refused):
        raise SystemExit("status-axes-gate-receipt-invalid")
    if set(failed) & GATE_CHECK_IDS != set(executed):
        raise SystemExit("status-axes-gate-failures-mismatch")
    if (REFUSAL_CHECK_ID in failed) != bool(refused):
        raise SystemExit("status-axes-source-refusal-mismatch")


def check_result_status_axes(result: dict[str, Any], gate_evidence: object) -> None:
    """Validate a result's axes against its fields and the full gate receipt when supplied.

    Artifact validators supply the receipt to verify exact gate causes. The result writer can
    supply the status it already extracted for the axis consistency checks.

    Raises:
        SystemExit: when the block is missing or disagrees with the gate evidence, the recorded review status or the
            recorded recommendation, or when ``check_status_axes`` rejects it.
    """
    metadata = result.get("metadata")
    if not isinstance(metadata, dict):
        raise SystemExit("status-axes-metadata-invalid")
    axes = _check_shape(metadata.get("status_axes"))
    gates_status = gate_evidence.get("status") if isinstance(gate_evidence, dict) else gate_evidence
    if axes != build_status_axes(str(gates_status), metadata):
        raise SystemExit("status-axes-evidence-mismatch")
    validation = metadata.get("review_validation")
    readiness = validation.get("verification_readiness") if isinstance(validation, dict) else None
    findings = result.get("findings")
    critical = findings.get("critical", 0) if isinstance(findings, dict) else 0
    failed = result.get("checks_failed")
    if not isinstance(failed, list) or not all(isinstance(item, str) for item in failed):
        raise SystemExit("status-axes-checks-failed-invalid")
    check_status_axes(
        axes,
        status=str(result.get("status")),
        checks_failed=failed,
        readiness=readiness,
        critical=critical if isinstance(critical, int) else 0,
        coverage=coverage_state(metadata),
    )
    if isinstance(gate_evidence, dict):
        _check_gate_receipt(failed, gate_evidence)
