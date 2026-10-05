"""Task loading with provenance, plus the legacy suite loader."""

import hashlib
import json
from pathlib import Path
from typing import Any

# Re-exported for call-site/test compatibility (tests reference it via this module's namespace).
from _bench_common.agentic_contracts import (
    AGENTIC_ARMS,
    AgenticOracle,  # noqa: F401
    AnswerScore,  # noqa: F401
)

# Stage plumbing lives in a private module so this runner stays under the suite's 250 KB maintenance limit.
# Every name it defines is re-exported here, including ones this file no longer calls itself: callers and tests
# reach these through the runner module, so pruning an apparently unused re-export breaks patch.object targets.
from _bench_common.claude_stages import (
    PARITY_MANIFEST_PATH,
)
from _bench_common.provider_parity_contracts import (
    canonical_task_hash,
    fresh_input_tokens,
    load_task_policies,
    load_task_suite,
    semantic_suite_hash,
    token_accounting_inconsistent,
)

from _bench_claude.agentic.models import BenchmarkRun, Task
from _bench_claude.agentic.scope import _delivered_prompt_hash, _delivered_task_prompt


def _canonical_agentic_row(result: BenchmarkRun) -> dict[str, Any]:
    """Normalize one prospective canonical Claude result for shared pass reporting.

    The Claude runner owns native event interpretation, while the shared reporter owns planned-coordinate denominators
    and paired comparisons. Absent terminal usage stays ``None`` here so it cannot look like measured zero consumption.
    """
    if result.parity_arm not in AGENTIC_ARMS:
        raise ValueError("canonical reporting requires a canonical A/B/C result")
    usage_available = result.usage_complete
    cached_input_tokens = result.cache_creation_tokens + result.cache_read_tokens
    return {
        "task_id": result.task_id,
        "repetition": result.repetition,
        "arm": result.parity_arm,
        "success": result.success,
        "answer_contract_valid": result.answer_contract_valid,
        "answer_pooling_eligible": result.answer_pooling_eligible,
        "diagnostic_only": result.answer_diagnostic_only,
        "incomplete": result.incomplete,
        "contaminated": result.contaminated,
        "treatment_adherence": result.treatment_adherence,
        "quality": {
            "correct": result.answer_correct,
            "quality_score": result.answer_quality_score,
            "components": dict(result.answer_components),
            "graded_score": result.answer_graded_score,
            "graded_components": dict(result.answer_graded_components),
        },
        "failure_details": [dict(detail) for detail in result.answer_failure_details],
        "answer_error": result.answer_error,
        "error_type": result.error_type,
        "usage_complete": usage_available,
        "token_accounting_inconsistent": (
            token_accounting_inconsistent(result.input_tokens, cached_input_tokens) if usage_available else None
        ),
        "input_tokens": result.input_tokens if usage_available else None,
        "cached_input_tokens": cached_input_tokens if usage_available else None,
        "fresh_input_tokens": (
            fresh_input_tokens(result.input_tokens, cached_input_tokens) if usage_available else None
        ),
        "output_tokens": result.output_tokens if usage_available else None,
        "elapsed_s": result.elapsed_s if usage_available or result.elapsed_s else None,
    }


def _locked_manifest_tasks(manifest_path: Path) -> tuple[dict[str, dict], list[dict]]:
    """Return manifest task rows and suites after structural validation."""
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"experiment manifest {manifest_path} is not valid JSON: {exc}") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("suites"), list):
        raise ValueError(f"experiment manifest {manifest_path} requires a suites list")

    task_rows: dict[str, dict] = {}
    suites: list[dict] = []
    for suite_index, suite in enumerate(manifest["suites"]):
        if not isinstance(suite, dict) or not isinstance(suite.get("tasks"), list):
            raise ValueError(f"experiment manifest {manifest_path} suite {suite_index} requires a tasks list")
        suites.append(suite)
        for task_index, task_row in enumerate(suite["tasks"]):
            task_id = task_row.get("id") if isinstance(task_row, dict) else None
            if not isinstance(task_id, str) or not task_id:
                raise ValueError(
                    f"experiment manifest {manifest_path} suite {suite_index} task {task_index} requires an id"
                )
            if task_id in task_rows:
                raise ValueError(f"experiment manifest {manifest_path} contains duplicate task id {task_id!r}")
            task_rows[task_id] = task_row
    return task_rows, suites


def load_tasks_with_provenance(tasks_path: Path, manifest_path: Path = PARITY_MANIFEST_PATH) -> list[Task]:
    """Load agentic tasks with immutable shared policy and canonical identity.

    Args:
        tasks_path: Raw agentic suite path.
        manifest_path: Locked provider-parity manifest defining task policy.

    Returns:
        Agentic task projections carrying the shared revision, hash, and policy fields.

    Raises:
        ValueError: If a task is missing from the locked policy manifest.
    """
    raw_suite_hash = hashlib.sha256(tasks_path.read_bytes()).hexdigest()
    raw_tasks = load_task_suite(tasks_path)
    manifest_tasks, manifest_suites = _locked_manifest_tasks(manifest_path)
    policies = load_task_policies(manifest_path)
    for raw_task in raw_tasks:
        task_id = raw_task["id"]
        if task_id not in policies:
            raise ValueError(f"no locked task policy for {task_id!r}")
        manifest_task = manifest_tasks.get(task_id)
        if manifest_task is None:
            raise ValueError(f"no locked manifest task for {task_id!r}")
        if canonical_task_hash(raw_task) != manifest_task.get("canonical_task_sha256"):
            raise ValueError(f"task hash mismatch for {task_id!r}")
        if _delivered_prompt_hash(raw_task, canonical=True) != manifest_task.get("prompt_sha256"):
            raise ValueError(f"prompt hash mismatch for {task_id!r}")
    raw_task_ids = [task["id"] for task in raw_tasks]
    matching_suites = [
        suite for suite in manifest_suites if [task.get("id") for task in suite["tasks"]] == raw_task_ids
    ]
    if len(matching_suites) != 1:
        raise ValueError("ordered task IDs do not match exactly one locked manifest suite")
    suite_hash = semantic_suite_hash(raw_tasks)
    loaded: list[Task] = []
    for raw_task in raw_tasks:
        task_id = raw_task["id"]
        actual_task_hash = canonical_task_hash(raw_task)
        policy = policies[task_id]
        loaded.append(
            Task(
                id=task_id,
                type=raw_task["type"],
                prompt=_delivered_task_prompt(raw_task, canonical=True),
                primary_module=raw_task.get("primary_module", ""),
                difficulty=raw_task.get("difficulty", "unknown"),
                skill=raw_task.get("skill", ""),
                symbol=raw_task.get("symbol", ""),
                expected_keywords=raw_task.get("expected_keywords", []),
                requires_reset=raw_task.get("requires_reset", False),
                codebase_module=raw_task.get("codebase_module", ""),
                expected_patch_keywords=raw_task.get("expected_patch_keywords", []),
                expected_files=raw_task.get("expected_files", []),
                test_target=raw_task.get("test_target", ""),
                experiment_revision=policy.experiment_revision,
                task_hash=actual_task_hash,
                prompt_hash=_delivered_prompt_hash(raw_task, canonical=True),
                suite_hash=suite_hash,
                suite_raw_hash=raw_suite_hash,
                oracle_class=policy.oracle_class,
                headline_eligible_v1=policy.headline_eligible_v1,
                scoreable=policy.scoreable,
                answer_task=dict(raw_task),
            )
        )
    return loaded


def load_legacy_tasks(tasks_path: Path) -> list[Task]:
    """Load an unlocked legacy suite without assigning canonical parity provenance."""
    raw_tasks = load_task_suite(tasks_path)
    suite_hash = semantic_suite_hash(raw_tasks)
    suite_raw_hash = hashlib.sha256(tasks_path.read_bytes()).hexdigest()
    return [
        Task(
            id=raw_task["id"],
            type=raw_task["type"],
            prompt=_delivered_task_prompt(raw_task, canonical=False),
            primary_module=raw_task.get("primary_module", ""),
            difficulty=raw_task.get("difficulty", "unknown"),
            skill=raw_task.get("skill", ""),
            symbol=raw_task.get("symbol", ""),
            expected_keywords=raw_task.get("expected_keywords", []),
            requires_reset=raw_task.get("requires_reset", False),
            codebase_module=raw_task.get("codebase_module", ""),
            expected_patch_keywords=raw_task.get("expected_patch_keywords", []),
            expected_files=raw_task.get("expected_files", []),
            test_target=raw_task.get("test_target", ""),
            task_hash=canonical_task_hash(raw_task),
            prompt_hash=_delivered_prompt_hash(raw_task, canonical=False),
            suite_hash=suite_hash,
            suite_raw_hash=suite_raw_hash,
            answer_task=dict(raw_task),
        )
        for raw_task in raw_tasks
    ]
