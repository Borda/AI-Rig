"""Scope resolution, delivered-prompt identity, and run cost accounting."""

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Sequence


from _bench_common.claude_transport import MODELS

# Re-exported for call-site/test compatibility (tests reference it via this module's namespace).
from _bench_common.agentic_contracts import (
    AGENTIC_ARMS,
    DEFAULT_REPETITIONS,
    AgenticOracle,  # noqa: F401
    AnswerScore,  # noqa: F401
    materialize_agentic_prompt,
)
from _bench_common.provider_parity_contracts import (
    PARITY_TIMEOUT_SECONDS,
    materialize_task_prompt,
)

# Stage plumbing lives in a private module so this runner stays under the suite's 250 KB maintenance limit.
# Every name it defines is re-exported here, including ones this file no longer calls itself: callers and tests
# reach these through the runner module, so pruning an apparently unused re-export breaks patch.object targets.
from _bench_common.claude_stages import (
    PARITY_MANIFEST_PATH,
    _manifest_sha256,
)

from _bench_claude.agentic.models import BenchmarkRun


def resolve_agentic_scope(
    manifest_path: Path = PARITY_MANIFEST_PATH,
    *,
    task_ids: Sequence[str] | None = None,
    arms: Sequence[str] = AGENTIC_ARMS,
    models: Sequence[str] | None = None,
    repetitions: int = DEFAULT_REPETITIONS,
) -> dict[str, object]:
    """Resolve one deterministic manifest-bound Claude agentic scope."""
    if repetitions < 1:
        raise ValueError("agentic repetitions must be at least 1")
    try:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("provider-parity manifest is unavailable or malformed") from exc
    contract = manifest.get("agentic_execution_contract")
    if not isinstance(contract, Mapping):
        raise ValueError("provider-parity manifest lacks the agentic execution contract")
    locked_task_ids = contract.get("task_ids")
    if not isinstance(locked_task_ids, list) or not all(isinstance(task_id, str) for task_id in locked_task_ids):
        raise ValueError("provider-parity manifest has invalid agentic task IDs")
    selected_task_ids = list(locked_task_ids if task_ids is None else task_ids)
    if not selected_task_ids or len(set(selected_task_ids)) != len(selected_task_ids):
        raise ValueError("agentic scope requires unique manifest-bound task IDs")
    if any(task_id not in locked_task_ids for task_id in selected_task_ids):
        raise ValueError("agentic scope includes a task outside the manifest")
    ordered_task_ids = [task_id for task_id in locked_task_ids if task_id in set(selected_task_ids)]
    selected_arms = list(arms)
    if (
        not selected_arms
        or len(set(selected_arms)) != len(selected_arms)
        or any(arm not in AGENTIC_ARMS for arm in selected_arms)
    ):
        raise ValueError("Claude agentic scope requires unique canonical A/B/C arms")
    selected_models = list(MODELS if models is None else models)
    if (
        not selected_models
        or len(set(selected_models)) != len(selected_models)
        or any(model not in MODELS for model in selected_models)
    ):
        raise ValueError("Claude agentic scope requires unique supported model aliases")
    coordinate_timeout_seconds = contract.get("coordinate_timeout_seconds")
    if coordinate_timeout_seconds != PARITY_TIMEOUT_SECONDS:
        raise ValueError("provider-parity manifest must lock the canonical coordinate timeout")
    total_cells = len(ordered_task_ids) * len(selected_arms) * len(selected_models) * repetitions
    payload: dict[str, object] = {
        "provider": "claude",
        "manifest_sha256": _manifest_sha256(Path(manifest_path)),
        "experiment_revision": manifest.get("experiment_revision"),
        "task_ids": ordered_task_ids,
        "arms": selected_arms,
        "models": selected_models,
        "repetitions": repetitions,
        "coordinate_timeout_seconds": coordinate_timeout_seconds,
        "total_cells": total_cells,
        "nonpoolable": True,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {**payload, "scope_sha256": hashlib.sha256(encoded).hexdigest()}


def _delivered_task_prompt(task: Mapping[str, object], *, canonical: bool) -> str:
    """Return the exact provider-visible prompt for one task.

    Canonical cells add the shared JSON response contract after the materialized task prose. Explicit legacy arms retain
    their historical prose-only prompt.
    """
    if canonical:
        return materialize_agentic_prompt(task)
    return materialize_task_prompt(task)


def _delivered_prompt_hash(task: Mapping[str, object], *, canonical: bool) -> str:
    """Return the SHA-256 identity of the exact prompt delivered to Claude."""
    return hashlib.sha256(_delivered_task_prompt(task, canonical=canonical).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# RESULTS_DIR, MODELS, and MODEL_TIMEOUT come from shared benchmark_paths and claude_transport modules.

# Cost is the fair cross-arm metric because arms differ in how many tokens they burn to reach the
# same answer. We use Anthropic's own per-run total_cost_usd (captured from the stream-json result
# event) — current prices, cache-aware, per model, with no local price table to drift out of date
# (an earlier hand-maintained table silently carried Opus 4.1 prices after the models moved to 5).
# A run with no result event (crash/timeout) keeps cost_usd = 0.0 and its $ column is omitted.


def run_cost_usd(r: "BenchmarkRun") -> float:
    """Return the run's captured USD cost (Anthropic's total_cost_usd), or 0.0 when unavailable.

    The cost comes straight from the stream-json ``result`` event's ``total_cost_usd`` — current
    list prices, cache-aware, per model — so there is no local price table to drift. A run that
    produced no result event (crash/timeout) keeps ``cost_usd = 0.0``, and callers omit the $ column.

    Args:
        r: Completed benchmark run.

    Returns:
        The run's cost in USD, or 0.0 when the result event carried no total_cost_usd.

    Examples:
        >>> from types import SimpleNamespace as N
        >>> run_cost_usd(N(cost_usd=0.42))
        0.42
        >>> run_cost_usd(N(cost_usd=0.0))
        0.0
    """
    return getattr(r, "cost_usd", 0.0) or 0.0
