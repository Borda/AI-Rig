#!/usr/bin/env python3
"""Score behavioral calibration observations against known finding IDs.

## Purpose

Measure recall, precision, route correctness, and confidence calibration without conflating scoring with model
invocation. It turns recorded finding selections and paired live metadata into deterministic metrics that can be
compared across calibration runs.

## Scope

Read recorded case and observation data and produce deterministic summaries without launching a model or altering
source fixtures. The validator checks fixture and live provenance, prompt and task hashes, pairing completeness, and
normalized token-cost evidence before scoring.

## Usage

Call the CLI from a calibration workflow after observations exist, or import its pure metric helpers in focused tests.
Supply the case set, observation JSONL, route policy, task manifest, and output path required by the CLI so the score
is grounded in the same contracts used for collection.

## Used by

The ``run.py`` calibration checks, live-campaign follow-up analysis, and behavioral-calibration regression tests use
this module. Release and routing decisions can use its explicit threshold results, while tests use the small metric
helpers to exercise edge cases such as zero denominators.

## Outputs

Emit rounded aggregate and per-route metrics, including explicit fixture-versus-live observation separation and
threshold failures. The output also records campaign completeness, route evidence, and confidence details so a high
score cannot hide missing or substituted live data.

## Failure

Malformed observations, stale live evidence, missing case IDs, or impossible usage metadata raise a validation error
instead of skewing the score. Hash mismatches, incomplete pairs, unsupported route metadata, and policy violations are
rejected before aggregate metrics are accepted.

Example:
    >>> _round3(2 / 3)
    0.667
    >>> _safe_div(3, 0)
    0.0
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from live_contract import Layout, build_prompt, candidate_findings, prompt_sha256, role_context, task_contract_sha256

LIVE_FIELDS = {
    "cached_input_tokens",
    "campaign_id",
    "check_failure_count",
    "estimated_cost_units",
    "evidence_scope",
    "input_tokens",
    "latency_ms",
    "model",
    "outcome",
    "output_tokens",
    "pair_id",
    "pair_role",
    "pricing_ref",
    "prompt_sha256",
    "reasoning_effort",
    "role",
    "route_id",
    "task_type",
    "task_contract_sha256",
    "tool_failure_count",
}
PRICING_REF = "normalized-token-v1:uncached+0.1*cached+4*output"


def _round3(value: float) -> float:
    """Round metric values to three decimals."""
    return round(value, 3)


def _safe_div(numerator: float, denominator: float) -> float:
    """Divide two numbers, returning 0.0 when the denominator is zero."""
    if denominator == 0:
        return 0.0
    return float(numerator) / float(denominator)


def _raw_metrics(tp: int, fp: int, fn: int) -> dict[str, float]:
    """Compute unrounded recall, precision, and F1 for gate decisions.

    Example:
        >>> _raw_metrics(2999, 0, 1001)["recall"]
        0.74975
    """
    recall = _safe_div(tp, tp + fn)
    precision = _safe_div(tp, tp + fp)
    f1 = _safe_div(2 * precision * recall, precision + recall)
    return {
        "recall": recall,
        "precision": precision,
        "f1": f1,
    }


def _is_fixture_source(source: str) -> bool:
    """Return whether an observation source is a fixture-only self-test.

    Example:
        >>> _is_fixture_source("fixture-selftest")
        True
        >>> _is_fixture_source("live-codex-cli")
        False
    """
    return source.startswith("fixture")


def _is_live_source(source: str) -> bool:
    """Return whether a source claims current paired live execution evidence."""
    return source.startswith("live-")


def _validate_live_metadata(
    record: dict[str, Any],
    route_policy: dict[str, Any],
    route_tasks: dict[str, dict[tuple[str, str, str, str], dict[str, Any]]],
    cases: dict[str, dict[str, Any]],
    root: Path,
    layout: str = "source",
) -> None:
    """Validate model, usage, cost, and pairing evidence for one live row."""
    missing = sorted(LIVE_FIELDS - set(record))
    if missing:
        raise ValueError("live observation missing fields: " + ",".join(missing))
    route_id = record["route_id"]
    routes = route_policy.get("routes", {})
    if not isinstance(route_id, str) or route_id not in routes:
        raise ValueError(f"unknown live route_id: {route_id!r}")
    pair_role = record["pair_role"]
    if pair_role not in {"baseline", "candidate"}:
        raise ValueError(f"invalid live pair_role: {pair_role!r}")
    route = routes[route_id]
    expected_model = route[f"{pair_role}_model"]
    if record["model"] != expected_model or record["reasoning_effort"] != route["effort"]:
        raise ValueError(f"live model/effort mismatch: {route_id}:{pair_role}")
    for key in ("campaign_id", "pair_id", "task_type", "pricing_ref"):
        if not isinstance(record[key], str) or not record[key].strip():
            raise ValueError(f"live observation needs non-empty {key}")
    if record.get("run_id") != record["pair_id"]:
        raise ValueError("live run_id must equal pair_id")
    if record["pricing_ref"] != PRICING_REF:
        raise ValueError(f"unsupported live pricing_ref: {record['pricing_ref']!r}")
    for key in ("prompt_sha256", "task_contract_sha256"):
        digest = record[key]
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError(f"live observation needs a SHA-256 {key}")
        try:
            int(digest, 16)
        except ValueError as exc:
            raise ValueError(f"live observation {key} is not hexadecimal") from exc
    for key in (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "latency_ms",
        "tool_failure_count",
        "check_failure_count",
    ):
        if not isinstance(record[key], int) or record[key] < 0:
            raise ValueError(f"live observation needs non-negative integer {key}")
    if record["cached_input_tokens"] > record["input_tokens"]:
        raise ValueError("live cached_input_tokens exceeds input_tokens")
    if not isinstance(record["estimated_cost_units"], int | float) or record["estimated_cost_units"] < 0:
        raise ValueError("live observation needs non-negative estimated_cost_units")
    expected_cost = _normalized_cost_units(record)
    if abs(float(record["estimated_cost_units"]) - expected_cost) > 0.001:
        raise ValueError("live estimated_cost_units does not match token evidence")
    if record["outcome"] not in {"pass", "fail", "timeout"}:
        raise ValueError(f"invalid live outcome: {record['outcome']!r}")
    if record["evidence_scope"] not in {"classification", "tool-use"}:
        raise ValueError(f"invalid live evidence_scope: {record['evidence_scope']!r}")
    role = record["role"]
    if not isinstance(role, str) or not role:
        raise ValueError("live observation needs non-empty role")
    signature = (record["case_id"], role, record["task_type"], record["evidence_scope"])
    task = route_tasks[route_id].get(signature)
    if task is not None:
        if record["task_contract_sha256"] != task_contract_sha256(task):
            raise ValueError(f"live task contract hash mismatch: {route_id}:{record['case_id']}:{role}")
        case = cases[record["case_id"]]
        expected_prompt = build_prompt(
            case, candidate_findings(record["case_id"], cases), task, role_context(root, role, layout)
        )
        if record["prompt_sha256"] != prompt_sha256(expected_prompt):
            raise ValueError(f"live prompt hash mismatch: {route_id}:{record['case_id']}:{role}")


def _prompt_sha256(prompt: str) -> str:
    """Return the canonical prompt digest used by paired live runs."""
    return hashlib.sha256(prompt.encode()).hexdigest()


def _normalized_cost_units(usage: dict[str, Any]) -> float:
    """Recompute the versioned normalized cost proxy from token evidence.

    Cached input tokens receive a tenth of the uncached-input weight and output tokens receive four times the weight;
    the result is rounded to three decimals. Validation of counter types and relationships happens before this helper
    is called.

    Example:
        >>> _normalized_cost_units({"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 5})
        84.0
    """
    uncached = max(int(usage["input_tokens"]) - int(usage["cached_input_tokens"]), 0)
    return round(uncached + 0.1 * int(usage["cached_input_tokens"]) + 4.0 * int(usage["output_tokens"]), 3)


def _parse_observed_at(value: str) -> datetime:
    """Parse an observation timestamp as timezone-aware UTC.

    Example:
        >>> _parse_observed_at("2026-06-02T06:50:00Z").isoformat()
        '2026-06-02T06:50:00+00:00'
    """
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("observed_at must include a timezone")
    return parsed.astimezone(timezone.utc)


def _read_json(path: Path) -> dict[str, Any]:
    """Read one JSON object from ``path`` and reject top-level arrays or scalars."""
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read non-empty JSON Lines rows, requiring each row to be an object."""
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            record = json.loads(stripped)
            if not isinstance(record, dict):
                raise ValueError(f"expected JSON object at {path}:{line_no}")
            records.append(record)
    return records


def _load_cases(path: Path) -> tuple[dict[str, dict[str, Any]], dict[str, float]]:
    """Load behavioral cases and scoring thresholds while preserving expected finding order.

    Each case is indexed by its unique non-empty ID and retains both the original finding list and a set for metric
    comparisons. Missing thresholds receive permissive defaults so callers can validate the case schema independently
    from gate policy.
    """
    payload = _read_json(path)
    cases_raw = payload.get("cases", [])
    if not isinstance(cases_raw, list):
        raise ValueError("behavioral cases must contain a 'cases' list")

    cases: dict[str, dict[str, Any]] = {}
    for case in cases_raw:
        if not isinstance(case, dict):
            raise ValueError("each behavioral case must be a JSON object")
        case_id = case.get("id")
        expected = case.get("expected_findings")
        target = case.get("target")
        prompt = case.get("prompt")
        if not isinstance(case_id, str) or not case_id:
            raise ValueError("each behavioral case needs a non-empty string id")
        if case_id in cases:
            raise ValueError(f"duplicate behavioral case id: {case_id}")
        if not isinstance(target, str) or not target:
            raise ValueError(f"behavioral case {case_id} needs a non-empty target")
        if not isinstance(prompt, str) or not prompt:
            raise ValueError(f"behavioral case {case_id} needs a non-empty prompt")
        if not isinstance(expected, list) or not all(isinstance(item, str) for item in expected):
            raise ValueError(f"behavioral case {case_id} needs string expected_findings")
        cases[case_id] = {
            "target": target,
            "expected": set(expected),
            "expected_findings": expected,
            "prompt": prompt,
        }

    thresholds_raw = payload.get("thresholds", {})
    if not isinstance(thresholds_raw, dict):
        raise ValueError("thresholds must be a JSON object")
    thresholds = {
        "min_observations": float(thresholds_raw.get("min_observations", 1)),
        "min_recall": float(thresholds_raw.get("min_recall", 0.0)),
        "min_precision": float(thresholds_raw.get("min_precision", 0.0)),
        "max_confidence_mae": float(thresholds_raw.get("max_confidence_mae", 1.0)),
        "max_mean_overconfidence": float(thresholds_raw.get("max_mean_overconfidence", 1.0)),
    }
    return cases, thresholds


def _load_route_policy(path: Path) -> dict[str, Any]:
    """Load and validate the live route acceptance policy."""
    payload = _read_json(path)
    if payload.get("schema_version") != 1:
        raise ValueError("live route policy schema_version must be 1")
    routes = payload.get("routes")
    if not isinstance(routes, dict) or not routes:
        raise ValueError("live route policy needs routes")
    for route_id, route in routes.items():
        if not isinstance(route_id, str) or not isinstance(route, dict):
            raise ValueError("invalid live route policy entry")
        for key in (
            "baseline_model",
            "candidate_model",
            "effort",
            "min_pairs",
            "max_quality_drop",
            "min_quality_gain",
            "min_campaigns",
            "max_cost_ratio",
            "min_tool_use_pairs",
            "pairs_per_campaign",
            "required_evidence_scopes",
            "selection_goal",
        ):
            if key not in route:
                raise ValueError(f"live route policy {route_id} missing {key}")
        if not isinstance(route["min_pairs"], int) or route["min_pairs"] < 1:
            raise ValueError(f"live route policy {route_id} has invalid min_pairs")
        if not isinstance(route["min_campaigns"], int) or route["min_campaigns"] < 2:
            raise ValueError(f"live route policy {route_id} has invalid min_campaigns")
        if not isinstance(route["min_tool_use_pairs"], int) or route["min_tool_use_pairs"] < 1:
            raise ValueError(f"live route policy {route_id} has invalid min_tool_use_pairs")
        if not isinstance(route["pairs_per_campaign"], int) or route["pairs_per_campaign"] < 1:
            raise ValueError(f"live route policy {route_id} has invalid pairs_per_campaign")
        scopes = route["required_evidence_scopes"]
        if (
            not isinstance(scopes, list)
            or not scopes
            or not all(scope in {"classification", "tool-use"} for scope in scopes)
        ):
            raise ValueError(f"live route policy {route_id} has invalid required_evidence_scopes")
    return payload


def _load_route_tasks(
    path: Path, route_policy: dict[str, Any], cases: dict[str, dict[str, Any]]
) -> dict[str, dict[tuple[str, str, str, str], dict[str, Any]]]:
    """Load exact case, role, type, and scope signatures for every live route."""
    payload = _read_json(path)
    if payload.get("schema_version") != 1:
        raise ValueError("live task manifest schema_version must be 1")
    routes = payload.get("routes")
    if not isinstance(routes, dict) or set(routes) != set(route_policy["routes"]):
        raise ValueError("live task manifest route set mismatch")
    route_tasks: dict[str, dict[tuple[str, str, str, str], dict[str, Any]]] = {}
    for route_id, tasks in routes.items():
        if not isinstance(tasks, list) or not tasks:
            raise ValueError(f"live task manifest route needs tasks: {route_id}")
        task_map: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        case_ids: set[str] = set()
        for index, task in enumerate(tasks):
            if not isinstance(task, dict):
                raise ValueError(f"live task manifest entry not object: {route_id}:{index}")
            case_id = task.get("case_id")
            role = task.get("role")
            task_type = task.get("task_type")
            evidence_scope = task.get("evidence_scope", "classification")
            if case_id not in cases:
                raise ValueError(f"live task manifest unknown case: {route_id}:{case_id!r}")
            if not all(isinstance(value, str) and value for value in (role, task_type)):
                raise ValueError(f"live task manifest role/type missing: {route_id}:{index}")
            if evidence_scope not in {"classification", "tool-use"}:
                raise ValueError(f"live task manifest scope invalid: {route_id}:{index}")
            if case_id in case_ids:
                raise ValueError(f"live task manifest duplicate case: {route_id}:{case_id}")
            case_ids.add(case_id)
            task_map[(case_id, role, task_type, evidence_scope)] = task
        if len(task_map) != route_policy["routes"][route_id]["pairs_per_campaign"]:
            raise ValueError(f"live task manifest campaign size mismatch: {route_id}")
        route_tasks[route_id] = task_map
    return route_tasks


def _validate_observation(
    record: dict[str, Any], cases: dict[str, dict[str, Any]]
) -> tuple[str, str, set[str], float, str, str, str | None]:
    """Validate one observation and return normalized fields used by metric aggregation.

    Confidence is constrained to [0.0, 1.0], reported findings are converted to a set, and timestamps are checked for
    timezone information while their original text is retained for output provenance.
    """
    case_id = record.get("case_id")
    if not isinstance(case_id, str) or case_id not in cases:
        raise ValueError(f"unknown behavioral case_id: {case_id!r}")

    target = record.get("target", cases[case_id]["target"])
    if not isinstance(target, str) or target != cases[case_id]["target"]:
        raise ValueError(f"target mismatch for {case_id}: expected {cases[case_id]['target']!r}, got {target!r}")

    reported = record.get("reported_findings")
    if not isinstance(reported, list) or not all(isinstance(item, str) for item in reported):
        raise ValueError(f"observation {case_id} needs string reported_findings")

    confidence = record.get("confidence")
    if not isinstance(confidence, int | float) or not 0.0 <= float(confidence) <= 1.0:
        raise ValueError(f"observation {case_id} needs confidence in [0.0, 1.0]")

    source = record.get("source", "unknown")
    if not isinstance(source, str) or not source:
        raise ValueError(f"observation {case_id} needs a non-empty source")

    run_id = record.get("run_id", "unknown")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError(f"observation {case_id} needs a non-empty run_id")

    observed_at_raw = record.get("observed_at")
    if observed_at_raw is None:
        observed_at = None
    elif isinstance(observed_at_raw, str) and observed_at_raw:
        _parse_observed_at(observed_at_raw)
        observed_at = observed_at_raw
    else:
        raise ValueError(f"observation {case_id} needs observed_at as an ISO timestamp")

    return case_id, target, set(reported), float(confidence), source, run_id, observed_at


def _metrics(tp: int, fp: int, fn: int) -> dict[str, float]:
    """Return recall, precision, and F1 rounded for the published summary."""
    raw = _raw_metrics(tp, fp, fn)
    return {
        "recall": _round3(raw["recall"]),
        "precision": _round3(raw["precision"]),
        "f1": _round3(raw["f1"]),
    }


def _score(
    cases_path: Path,
    observations_path: Path,
    route_policy_path: Path,
    route_tasks_path: Path,
    root: Path,
    require_live_routes: bool = False,
    layout: str = "source",
) -> dict[str, Any]:
    """Score recorded observations after validating case, route, pairing, freshness, and live-evidence contracts.

    The returned payload keeps raw gate metrics alongside rounded presentation values and marks fixture-only evidence
    separately from current live observations. No model invocation or source-fixture mutation occurs here.
    """
    cases, thresholds = _load_cases(cases_path)
    route_policy = _load_route_policy(route_policy_path)
    route_tasks = _load_route_tasks(route_tasks_path, route_policy, cases)
    observations = _read_jsonl(observations_path)
    by_target_counts: dict[str, Counter[str]] = defaultdict(Counter)
    by_target_conf_errors: dict[str, list[float]] = defaultdict(list)
    by_target_overconfidence: dict[str, list[float]] = defaultdict(list)
    by_source_counts: dict[str, Counter[str]] = defaultdict(Counter)
    by_source_conf_errors: dict[str, list[float]] = defaultdict(list)
    by_source_overconfidence: dict[str, list[float]] = defaultdict(list)
    case_results: list[dict[str, Any]] = []
    sources: Counter[str] = Counter()
    run_ids: Counter[str] = Counter()
    observed_timestamps: list[datetime] = []
    missing_observed_at = 0
    observed_case_ids: set[str] = set()
    observed_run_cases: set[tuple[str, str]] = set()
    live_rows: list[dict[str, Any]] = []

    total_counts: Counter[str] = Counter()
    total_conf_errors: list[float] = []
    total_overconfidence: list[float] = []

    for record in observations:
        case_id, target, reported, confidence, source, run_id, observed_at = _validate_observation(record, cases)
        if _is_live_source(source):
            _validate_live_metadata(record, route_policy, route_tasks, cases, root, layout)
        run_case = (run_id, f"{case_id}:{record.get('pair_role', '')}")
        if run_case in observed_run_cases:
            raise ValueError(f"duplicate behavioral observation: run_id={run_id!r}, case_id={case_id!r}")
        observed_run_cases.add(run_case)
        observed_case_ids.add(case_id)
        expected = cases[case_id]["expected"]
        tp = len(expected & reported)
        fp = len(reported - expected)
        fn = len(expected - reported)
        case_metrics_raw = _raw_metrics(tp, fp, fn)
        case_metrics = _metrics(tp, fp, fn)
        case_f1 = case_metrics_raw["f1"]
        confidence_error = abs(confidence - case_f1)
        overconfidence = max(confidence - case_f1, 0.0)

        total_counts.update({"tp": tp, "fp": fp, "fn": fn, "observations": 1})
        by_target_counts[target].update({"tp": tp, "fp": fp, "fn": fn, "observations": 1})
        by_source_counts[source].update({"tp": tp, "fp": fp, "fn": fn, "observations": 1})
        total_conf_errors.append(confidence_error)
        total_overconfidence.append(overconfidence)
        by_target_conf_errors[target].append(confidence_error)
        by_target_overconfidence[target].append(overconfidence)
        by_source_conf_errors[source].append(confidence_error)
        by_source_overconfidence[source].append(overconfidence)
        sources[source] += 1
        run_ids[run_id] += 1
        if observed_at is None:
            missing_observed_at += 1
        else:
            observed_timestamps.append(_parse_observed_at(observed_at))

        case_results.append(
            {
                "case_id": case_id,
                "target": target,
                "source": source,
                "run_id": run_id,
                "observed_at": observed_at,
                "tp": tp,
                "fp": fp,
                "fn": fn,
                "confidence": _round3(confidence),
                "confidence_error": _round3(confidence_error),
                "overconfidence": _round3(overconfidence),
                **case_metrics,
            }
        )
        if _is_live_source(source):
            live_rows.append({**record, "quality_f1": case_f1})

    overall_raw = _summarize_counts_raw(total_counts, total_conf_errors, total_overconfidence)
    overall = _round_summary(overall_raw)
    by_target = {
        target: _round_summary(
            _summarize_counts_raw(counts, by_target_conf_errors[target], by_target_overconfidence[target])
        )
        for target, counts in sorted(by_target_counts.items())
    }
    by_source = {
        source: _round_summary(
            _summarize_counts_raw(counts, by_source_conf_errors[source], by_source_overconfidence[source])
        )
        for source, counts in sorted(by_source_counts.items())
    }
    fixture_observations = sum(count for source, count in sources.items() if _is_fixture_source(source))
    live_observations = sum(count for source, count in sources.items() if _is_live_source(source))
    freshness = _summarize_freshness(observed_timestamps, missing_observed_at, fixture_observations, live_observations)
    live_routes = _summarize_live_routes(live_rows, route_policy, route_tasks)

    missing_case_ids = sorted(set(cases) - observed_case_ids)
    checks_failed = _threshold_failures(overall_raw, thresholds)
    if missing_case_ids:
        checks_failed.append("behavioral-case-coverage")
    if live_routes["status"] == "fail":
        checks_failed.extend(live_routes["checks_failed"])
    elif require_live_routes and live_routes["status"] == "insufficient-evidence":
        checks_failed.append("live-route-insufficient-evidence")
    notes = [
        "The scorer measures supplied observations; live Codex behavior is measured only when observations come from live calibration runs.",
        "fixture-selftest observations validate the scoring contract but are not evidence of current live model quality.",
    ]
    if live_observations == 0:
        notes.append(
            "No live Codex observations were supplied; use source=live-* rows to measure current model behavior."
        )
    if live_routes["status"] == "insufficient-evidence":
        notes.append("Configured model routes remain provisional until every route reaches its paired-run minimum.")
    return {
        "status": "fail" if checks_failed else "pass",
        "checks_failed": checks_failed,
        "thresholds": thresholds,
        "overall": overall,
        "gate_metrics_raw": overall_raw,
        "by_target": by_target,
        "by_source": by_source,
        "case_results": case_results,
        "missing_case_ids": missing_case_ids,
        "observation_sources": dict(sorted(sources.items())),
        "observation_run_ids": dict(sorted(run_ids.items())),
        "observation_freshness": freshness,
        "live_route_acceptance": live_routes,
        "live_routes_required": require_live_routes,
        "notes": notes,
    }


def _summarize_counts_raw(
    counts: Counter[str], confidence_errors: list[float], overconfidence: list[float]
) -> dict[str, Any]:
    """Compute unrounded quality and confidence metrics for one aggregate bucket."""
    metrics = _raw_metrics(counts["tp"], counts["fp"], counts["fn"])
    confidence_mae = _safe_div(sum(confidence_errors), len(confidence_errors))
    mean_overconfidence = _safe_div(sum(overconfidence), len(overconfidence))
    return {
        "observations": counts["observations"],
        "tp": counts["tp"],
        "fp": counts["fp"],
        "fn": counts["fn"],
        **metrics,
        "confidence_mae": confidence_mae,
        "confidence_accuracy": max(0.0, 1.0 - confidence_mae),
        "mean_overconfidence": mean_overconfidence,
    }


def _round_summary(summary: dict[str, Any]) -> dict[str, Any]:
    """Round floating-point fields in an aggregate summary without changing counters."""
    return {key: _round3(value) if isinstance(value, float) else value for key, value in summary.items()}


def _summarize_live_routes(
    live_rows: list[dict[str, Any]],
    route_policy: dict[str, Any],
    route_tasks: dict[str, dict[tuple[str, str, str, str], dict[str, Any]]],
) -> dict[str, Any]:
    """Evaluate paired live observations for quality and cost non-regression."""
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in live_rows:
        grouped[(row["route_id"], row["pair_id"])].append(row)

    route_results: dict[str, Any] = {}
    checks_failed: list[str] = []
    any_insufficient = False
    for route_id, policy in sorted(route_policy["routes"].items()):
        comparisons: list[dict[str, Any]] = []
        malformed_pairs = 0
        for (observed_route, pair_id), rows in sorted(grouped.items()):
            if observed_route != route_id:
                continue
            by_role = {row["pair_role"]: row for row in rows}
            if len(rows) != 2 or set(by_role) != {"baseline", "candidate"}:
                malformed_pairs += 1
                continue
            baseline = by_role["baseline"]
            candidate = by_role["candidate"]
            if (
                baseline["prompt_sha256"] != candidate["prompt_sha256"]
                or baseline["case_id"] != candidate["case_id"]
                or baseline["task_type"] != candidate["task_type"]
                or baseline["evidence_scope"] != candidate["evidence_scope"]
                or baseline["campaign_id"] != candidate["campaign_id"]
                or baseline["role"] != candidate["role"]
            ):
                malformed_pairs += 1
                continue
            quality_ok = (
                baseline["outcome"] != "timeout"
                and baseline["tool_failure_count"] == 0
                and candidate["quality_f1"] + float(policy["max_quality_drop"]) >= baseline["quality_f1"]
                and candidate["outcome"] == "pass"
                and candidate["tool_failure_count"] <= baseline["tool_failure_count"]
                and candidate["check_failure_count"] <= baseline["check_failure_count"]
            )
            baseline_cost = float(baseline["estimated_cost_units"])
            candidate_cost = float(candidate["estimated_cost_units"])
            cost_ratio = candidate_cost / baseline_cost if baseline_cost else 1.0 if candidate_cost == 0 else None
            max_cost_ratio = policy["max_cost_ratio"]
            cost_ok = max_cost_ratio is None or (cost_ratio is not None and cost_ratio <= float(max_cost_ratio))
            comparisons.append(
                {
                    "pair_id": pair_id,
                    "campaign_id": baseline["campaign_id"],
                    "case_id": baseline["case_id"],
                    "task_type": baseline["task_type"],
                    "evidence_scope": baseline["evidence_scope"],
                    "role": baseline["role"],
                    "baseline_f1": _round3(baseline["quality_f1"]),
                    "candidate_f1": _round3(candidate["quality_f1"]),
                    "cost_ratio": _round3(cost_ratio) if cost_ratio is not None else None,
                    "latency_ratio": _round3(
                        _safe_div(candidate["latency_ms"], baseline["latency_ms"]) if baseline["latency_ms"] else 0.0
                    ),
                    "quality_ok": quality_ok,
                    "cost_ok": cost_ok,
                }
            )
        quality_failures = sum(not item["quality_ok"] for item in comparisons)
        cost_failures = sum(not item["cost_ok"] for item in comparisons)
        mean_quality_gain = _safe_div(
            sum(item["candidate_f1"] - item["baseline_f1"] for item in comparisons), len(comparisons)
        )
        gain_ok = mean_quality_gain >= float(policy["min_quality_gain"])
        observed_scopes = sorted({item["evidence_scope"] for item in comparisons})
        missing_scopes = sorted(set(policy["required_evidence_scopes"]) - set(observed_scopes))
        campaign_tasks: dict[str, set[tuple[str, str, str, str]]] = defaultdict(set)
        for item in comparisons:
            campaign_tasks[item["campaign_id"]].add(
                (item["case_id"], item["role"], item["task_type"], item["evidence_scope"])
            )
        observed_campaigns = sorted(campaign_tasks)
        expected_tasks = set(route_tasks[route_id])
        complete_campaigns = sorted(
            campaign_id for campaign_id, signatures in campaign_tasks.items() if signatures == expected_tasks
        )
        substituted_campaigns = sorted(
            campaign_id for campaign_id, signatures in campaign_tasks.items() if signatures - expected_tasks
        )
        tool_use_pairs = sum(item["evidence_scope"] == "tool-use" for item in comparisons)
        enough_pairs = (
            len(comparisons) >= policy["min_pairs"]
            and len(complete_campaigns) >= policy["min_campaigns"]
            and tool_use_pairs >= policy["min_tool_use_pairs"]
            and not missing_scopes
        )
        if malformed_pairs:
            checks_failed.append(f"live-route-malformed-pair:{route_id}")
        if quality_failures:
            checks_failed.append(f"live-route-quality-regression:{route_id}")
        if comparisons and not gain_ok:
            checks_failed.append(f"live-route-quality-gain:{route_id}")
        if cost_failures:
            checks_failed.append(f"live-route-cost-regression:{route_id}")
        if substituted_campaigns:
            checks_failed.append(f"live-route-task-substitution:{route_id}")
        route_failed = bool(
            malformed_pairs
            or quality_failures
            or cost_failures
            or substituted_campaigns
            or (comparisons and not gain_ok)
        )
        if not enough_pairs:
            any_insufficient = True
        route_results[route_id] = {
            "status": "fail" if route_failed else "pass" if enough_pairs else "insufficient-evidence",
            "required_pairs": policy["min_pairs"],
            "valid_pairs": len(comparisons),
            "required_campaigns": policy["min_campaigns"],
            "observed_campaigns": observed_campaigns,
            "complete_campaigns": complete_campaigns,
            "substituted_campaigns": substituted_campaigns,
            "pairs_per_campaign": policy["pairs_per_campaign"],
            "required_tool_use_pairs": policy["min_tool_use_pairs"],
            "tool_use_pairs": tool_use_pairs,
            "required_evidence_scopes": policy["required_evidence_scopes"],
            "observed_evidence_scopes": observed_scopes,
            "missing_evidence_scopes": missing_scopes,
            "malformed_pairs": malformed_pairs,
            "quality_failures": quality_failures,
            "mean_quality_gain": _round3(mean_quality_gain),
            "required_mean_quality_gain": policy["min_quality_gain"],
            "mean_quality_gain_ok": gain_ok,
            "cost_failures": cost_failures,
            "selection_goal": policy["selection_goal"],
            "comparisons": comparisons,
        }
    status = "fail" if checks_failed else "insufficient-evidence" if any_insufficient else "pass"
    return {
        "status": status,
        "checks_failed": checks_failed,
        "cost_metric": PRICING_REF,
        "monetary_cost_evidence": False,
        "evidence_scope": "paired classification and isolated tool-use tasks",
        "routes": route_results,
    }


def _summarize_freshness(
    observed_timestamps: list[datetime],
    missing_observed_at: int,
    fixture_observations: int,
    live_observations: int,
) -> dict[str, Any]:
    """Summarize observation recency and distinguish fixture, live, and timestamp-missing rows."""
    latest = max(observed_timestamps) if observed_timestamps else None
    latest_age_days = None
    if latest is not None:
        latest_age_days = _round3(max(0.0, (datetime.now(timezone.utc) - latest).total_seconds() / 86400.0))
    return {
        "latest_observed_at": latest.isoformat().replace("+00:00", "Z") if latest is not None else None,
        "latest_age_days": latest_age_days,
        "missing_observed_at": missing_observed_at,
        "fixture_observations": fixture_observations,
        "live_observations": live_observations,
    }


def _threshold_failures(overall: dict[str, Any], thresholds: dict[str, float]) -> list[str]:
    """Return gate failures using unrounded metric values.

    Example:
        >>> rounded = _metrics(2999, 0, 1001)
        >>> rounded["recall"]
        0.75
        >>> _threshold_failures(
        ...     {
        ...         "observations": 16,
        ...         "recall": 0.74975,
        ...         "precision": 1.0,
        ...         "confidence_mae": 0.0,
        ...         "mean_overconfidence": 0.0,
        ...     },
        ...     {
        ...         "min_observations": 16.0,
        ...         "min_recall": 0.75,
        ...         "min_precision": 0.75,
        ...         "max_confidence_mae": 0.2,
        ...         "max_mean_overconfidence": 0.15,
        ...     },
        ... )
        ['behavioral-recall']
    """
    failures: list[str] = []
    if overall["observations"] < thresholds["min_observations"]:
        failures.append("behavioral-min-observations")
    if overall["recall"] < thresholds["min_recall"]:
        failures.append("behavioral-recall")
    if overall["precision"] < thresholds["min_precision"]:
        failures.append("behavioral-precision")
    if overall["confidence_mae"] > thresholds["max_confidence_mae"]:
        failures.append("behavioral-confidence-mae")
    if overall["mean_overconfidence"] > thresholds["max_mean_overconfidence"]:
        failures.append("behavioral-overconfidence")
    return failures


def main() -> int:
    """Parse calibration-score arguments, validate inputs, write the score, and return its process status."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", required=True, type=Path, help="Behavioral case-set JSON.")
    parser.add_argument("--observations", required=True, type=Path, help="JSONL observations to score.")
    parser.add_argument("--route-policy", required=True, type=Path, help="Paired route acceptance policy JSON.")
    parser.add_argument("--tasks", required=True, type=Path, help="Live A/B task-contract JSON.")
    parser.add_argument(
        "--layout",
        choices=[layout.value for layout in Layout],
        default=Layout.PLUGIN.value,
        help="Instruction layout used to verify live prompt hashes.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Source project root; plugin mode discovers role cards from the installed package.",
    )
    parser.add_argument(
        "--require-live-routes", action="store_true", help="Return failure unless every configured live route passes."
    )
    parser.add_argument("--out", required=True, type=Path, help="Scored result JSON path.")
    args = parser.parse_args()

    context_root = Path(__file__).resolve().parents[2] if args.layout == "plugin" else args.root.resolve()
    result = _score(
        args.cases,
        args.observations,
        args.route_policy,
        args.tasks,
        context_root,
        args.require_live_routes,
        args.layout,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(args.out)
    return 1 if result["status"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
