"""Locked-query fitness, pooling eligibility, and evaluator identity."""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import sys

from _bench_codex import runtime
from _bench_common.provider_parity_contracts import (
    EvaluationResult,
)

from _bench_codex.structural.config import BENCHMARKS_DIR
from _bench_codex.structural.models import CodexRun


@dataclass(frozen=True)
class LockedQueryFitness:
    """Continuous exact-query similarity with independently visible components."""

    overall: float
    endpoint: float
    target: float
    options: float


def _arm_compliance(arm: str, evidence: runtime.CodexParseResult | CodexRun) -> bool | None:
    """Evaluate the transport-specific required-use contract for one arm.

    Scope of the claim: the ``_skill_`` / ``_direct_`` prefixes are configured by
    construction, not observed — the parser labels a query "skill" only because
    the C home supplied a ``skill_path`` (see ``runtime.parse_codex_jsonl``). Both
    branches prove "a successful canonical compact query ran in a verified home
    for this arm"; the stream cannot tell B and C apart, since both end in the
    same ``$CODEMAP_BIN`` command. Never read C compliance as proof the Skill was
    read — ``skill_delivery_observed`` is the separate observational signal.
    """
    if arm == "B_auto":
        return evidence.codemap_direct_compact_successful_calls > 0
    if arm == "C_strict":
        return evidence.codemap_skill_compact_successful_calls > 0
    if arm == "A_plain":
        return None
    raise ValueError(f"unknown benchmark arm {arm!r}")


def _locked_query_conformance(
    task: Mapping[str, Any], arm: str, run: runtime.CodexParseResult | CodexRun
) -> bool | None:
    """Report whether successful compact queries exactly match the locked contract."""
    if arm == "A_plain":
        return None
    locked = _locked_expected_queries(task)
    if not locked:
        return None
    observed = {
        normalized
        for arguments in run.successful_query_arguments
        if arguments and (normalized := _normalize_locked_query(arguments[0], arguments[1:])) is not None
    }
    if _expected_query_policy(task) == "all_required":
        return all(query in observed for query in locked)
    return any(query in observed for query in locked)


def _locked_query_fitness(
    task: Mapping[str, Any],
    arm: str,
    run: runtime.CodexParseResult | CodexRun,
) -> LockedQueryFitness | None:
    """Score exact-query similarity and expose endpoint, target, and option contributions."""
    if arm == "A_plain":
        return None
    locked = _locked_expected_queries(task)
    if not locked:
        return None
    observed = [
        normalized
        for arguments in run.successful_query_arguments
        if arguments and (normalized := _normalize_locked_query(arguments[0], arguments[1:])) is not None
    ]
    if not observed:
        return LockedQueryFitness(0.0, 0.0, 0.0, 0.0)
    best_matches = [_best_locked_query_match(expected_query, observed) for expected_query in locked]
    if _expected_query_policy(task) == "all_required":
        return _mean_locked_query_fitness(best_matches)
    return max(
        best_matches,
        key=lambda match: (match.overall, match.endpoint, match.target, match.options),
    )


def _best_locked_query_match(
    expected_query: tuple[str, ...],
    observed_queries: list[tuple[str, ...]],
) -> LockedQueryFitness:
    """Return the single observed query most similar to one locked query."""
    matches = [_locked_query_pair_fitness(expected_query, actual_query) for actual_query in observed_queries]
    return max(matches, key=lambda match: (match.overall, match.endpoint, match.target, match.options))


def _mean_locked_query_fitness(matches: list[LockedQueryFitness]) -> LockedQueryFitness:
    """Average required-query fitness without mixing independently matched components."""
    count = len(matches)
    return LockedQueryFitness(
        overall=sum(match.overall for match in matches) / count,
        endpoint=sum(match.endpoint for match in matches) / count,
        target=sum(match.target for match in matches) / count,
        options=sum(match.options for match in matches) / count,
    )


_EXPECTED_QUERY_POLICIES = frozenset({"any_match", "all_required"})


def _expected_query_policy(task: Mapping[str, Any]) -> str:
    """Return the task query-match policy, defaulting legacy tasks to any-match."""
    policy = task.get("expected_query_policy", "any_match")
    if not isinstance(policy, str) or policy not in _EXPECTED_QUERY_POLICIES:
        choices = ", ".join(sorted(_EXPECTED_QUERY_POLICIES))
        raise ValueError(f"expected_query_policy must be one of {choices}")
    return policy


def _locked_expected_queries(task: Mapping[str, Any]) -> list[tuple[str, ...]]:
    """Normalize every valid expected query declared by one task."""
    expected = task.get("expected_queries")
    if not isinstance(expected, list) or not expected:
        return []
    locked: list[tuple[str, ...]] = []
    for query in expected:
        if not isinstance(query, Mapping) or not isinstance(query.get("cmd"), str):
            continue
        arguments = query.get("args", [])
        if isinstance(arguments, list) and all(isinstance(value, str) for value in arguments):
            normalized = _normalize_locked_query(str(query["cmd"]), arguments)
            if normalized is not None:
                locked.append(normalized)
    return locked


def _locked_query_pair_fitness(
    expected_query: tuple[str, ...],
    actual_query: tuple[str, ...],
) -> LockedQueryFitness:
    """Measure overall and component similarity for one locked/observed pair."""
    expected_endpoint, expected_targets, expected_options = _split_normalized_query(expected_query)
    actual_endpoint, actual_targets, actual_options = _split_normalized_query(actual_query)
    return LockedQueryFitness(
        overall=_token_set_similarity(expected_query, actual_query),
        endpoint=float(expected_endpoint == actual_endpoint),
        target=_token_set_similarity(expected_targets, actual_targets),
        options=_token_set_similarity(expected_options, actual_options),
    )


def _token_set_similarity(expected: tuple[str, ...], actual: tuple[str, ...]) -> float:
    """Return Jaccard similarity, treating two empty query components as equal."""
    expected_tokens = set(expected)
    actual_tokens = set(actual)
    if not expected_tokens and not actual_tokens:
        return 1.0
    return len(expected_tokens & actual_tokens) / len(expected_tokens | actual_tokens)


_LOCKED_QUERY_BOOLEAN_OPTIONS = frozenset({"--broken", "--exclude-tests", "--with-imports"})
_LOCKED_QUERY_VALUE_OPTIONS = frozenset({"--limit", "--top"})


def _split_normalized_query(query: tuple[str, ...]) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """Separate a normalized query into endpoint, positional targets, and option groups."""
    endpoint = query[0]
    targets: list[str] = []
    options: list[str] = []
    index = 1
    while index < len(query):
        token = query[index]
        if token in _LOCKED_QUERY_BOOLEAN_OPTIONS:
            options.append(token)
        elif token in _LOCKED_QUERY_VALUE_OPTIONS:
            options.append(f"{token}={query[index + 1]}")
            index += 1
        else:
            targets.append(token)
        index += 1
    return endpoint, tuple(targets), tuple(options)


def _normalize_locked_query(command: str, arguments: list[str]) -> tuple[str, ...] | None:
    """Canonicalize the locked query grammar without weakening task semantics.

    Positional arguments retain their order.  Only the registered boolean options may move, and ``--limit``/``--top``
    accept one non-negative decimal value. Unknown, duplicate, missing-value, and extra option tokens are rejected.
    """
    if not command or not isinstance(command, str):
        return None
    positionals: list[str] = []
    booleans: set[str] = set()
    values: dict[str, str] = {}
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if not isinstance(argument, str) or not argument:
            return None
        if argument in _LOCKED_QUERY_BOOLEAN_OPTIONS:
            if argument in booleans:
                return None
            booleans.add(argument)
        elif argument in _LOCKED_QUERY_VALUE_OPTIONS:
            if argument in values or index + 1 >= len(arguments):
                return None
            value = arguments[index + 1]
            if not isinstance(value, str) or not re.fullmatch(r"[0-9]+", value):
                return None
            values[argument] = str(int(value))
            index += 1
        elif argument.startswith("-"):
            return None
        else:
            positionals.append(argument)
        index += 1
    normalized = [command, *positionals, *sorted(booleans)]
    for option, value in sorted(values.items()):
        normalized.extend((option, value))
    return tuple(normalized)


def _pooling_ineligibility_reasons(run: CodexRun) -> tuple[str, ...]:
    """Return run-level admission failures that forbid canonical pooling.

    Unscoreable diagnostic cells are intentionally absent: they are planned
    exclusions, unlike incomplete, contaminated, malformed-token, and
    required-use-invalid results.
    """
    reasons: list[str] = []
    if not run.success:
        reasons.append("unsuccessful")
    if run.incomplete:
        reasons.append("incomplete")
    if run.extraction_failed:
        reasons.append("extraction_failed")
    if run.contaminated:
        reasons.append("contaminated")
    if run.token_accounting_inconsistent:
        reasons.append("token_accounting_inconsistent")
    if run.targeted:
        reasons.append("targeted")
    if run.diagnostic_only:
        reasons.append("diagnostic_only")
    # Only the strict arm carries a required-use contract. B is an optional-use canary on
    # both providers, so a zero-query B cell is compliant and stays poolable; excluding it
    # here dropped exactly the cells where the model declined to query, which biased the
    # pooled B result toward the runs that happened to use Codemap.
    if run.arm == "C_strict" and run.compliance is not True:
        reasons.append("required_use_missing")
    return tuple(reasons)


def _infrastructure_failure_signature(run: CodexRun) -> str | None:
    """Return a recurrence key only for pre-response runner/provider failures."""
    if run.success or not run.incomplete or run.input_tokens or run.output_tokens or run.output_text.strip():
        return None
    if run.error_type == "authentication_failed":
        return "authentication_failed"
    if run.error_type not in {
        "authentication_state_failed",
        "launch_os_error",
        "missing_terminal",
        "non_zero_exit",
        "response_failed",
        "transport_error",
        "turn_failed",
    }:
        return None
    normalized = run.error.casefold()
    http_class = re.search(r"\b([45])\d\d\b", normalized)
    return f"{run.error_type}:http_{http_class.group(1)}xx" if http_class else run.error_type


@lru_cache(maxsize=1)
def _reference_bench_module() -> Any:
    """Load the Claude reference module once to reuse its exact evaluator registry."""
    module_path = BENCHMARKS_DIR / "run-claude-structural.py"
    spec = importlib.util.spec_from_file_location("_codex_shared_bench_reference", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load shared evaluator")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _diff_impact_stager(repo_path: Path, task: Mapping[str, Any]) -> Any | None:
    """Return the shared Claude stager for one DI task, or ``None``."""
    if task.get("type") != "diff_impact":
        return None
    stage_spec = task.get("stage")
    if not isinstance(stage_spec, list) or not stage_spec:
        return None
    return _reference_bench_module().DiffImpactStager(repo_path, stage_spec)


def _validate_diff_impact_stage(repo_path: Path, task: Mapping[str, Any]) -> None:
    """Validate every DI anchor without mutating the target tree."""
    stager = _diff_impact_stager(repo_path, task)
    if stager is None:
        return
    stager._assert_clean()
    for edit in stager.stage_spec:
        if not isinstance(edit, Mapping) or not isinstance(edit.get("file"), str):
            raise ValueError(f"invalid DI stage edit for {task.get('id', '<unknown>')}")
        path = repo_path / str(edit["file"])
        if not path.is_file():
            raise ValueError(f"DI stage anchor file is unavailable: {edit['file']}")
        text = path.read_text(encoding="utf-8")
        if "append" in edit:
            continue
        if "find" not in edit or "replace" not in edit or str(edit["find"]) not in text:
            raise ValueError(f"DI stage anchor is stale: {edit['file']}")


def _default_evaluator(task: Mapping[str, Any], output_text: str) -> EvaluationResult:
    """Invoke the exact evaluator registry used by the Claude reference adapter."""
    return _reference_bench_module()._SHARED_EVALUATORS.evaluate(task, output_text)


def _evaluator_identity(task: Mapping[str, Any], evaluator: Callable[..., Any]) -> tuple[str, str]:
    """Return the shared evaluator ID/hash, or deterministic fixture provenance."""
    if evaluator is _default_evaluator:
        return _reference_bench_module()._evaluator_provenance(task)
    identifier = getattr(evaluator, "__name__", evaluator.__class__.__name__)
    try:
        source = inspect.getsource(evaluator)
    except (OSError, TypeError):
        source = identifier
    return identifier, hashlib.sha256((identifier + "\n" + source).encode("utf-8")).hexdigest()
