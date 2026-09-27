"""Task loading with provenance for the structural suite."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


from _bench_common.provider_parity_contracts import (
    TaskPolicy,
    canonical_task_hash,
    load_task_policies,
    load_task_suite,
    prompt_hash,
    semantic_suite_hash,
)

from _bench_codex.structural.config import PARITY_MANIFEST_PATH, _PROVENANCE_KEY
from _bench_codex.structural.manifest import _manifest_revision


def load_tasks_with_provenance(tasks_path: Path, manifest_path: Path = PARITY_MANIFEST_PATH) -> list[dict[str, Any]]:
    """Load raw tasks and fail closed on any locked hash or ordering mismatch."""
    raw_tasks = load_task_suite(tasks_path)
    policies = load_task_policies(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    experiment_revision = _manifest_revision(manifest)
    manifest_rows = {task["id"]: task for suite in manifest.get("suites", []) for task in suite.get("tasks", [])}
    task_ids = [task["id"] for task in raw_tasks]
    matching_suites = [
        suite for suite in manifest.get("suites", []) if [row.get("id") for row in suite.get("tasks", [])] == task_ids
    ]
    if len(matching_suites) != 1:
        raise ValueError("ordered task IDs do not match exactly one locked manifest suite")
    for task_ordinal, task in enumerate(raw_tasks):
        row = manifest_rows.get(task["id"])
        if row is None or task["id"] not in policies:
            raise ValueError(f"no locked task policy for {task['id']!r}")
        if canonical_task_hash(task) != row.get("canonical_task_sha256"):
            raise ValueError(f"task hash mismatch for {task['id']!r}")
        if prompt_hash(task) != row.get("prompt_sha256"):
            raise ValueError(f"prompt hash mismatch for {task['id']!r}")
    suite_hash = semantic_suite_hash(raw_tasks)
    raw_hash = hashlib.sha256(tasks_path.read_bytes()).hexdigest()
    loaded: list[dict[str, Any]] = []
    for task_ordinal, task in enumerate(raw_tasks):
        policy: TaskPolicy = policies[task["id"]]
        if policy.experiment_revision != experiment_revision:
            raise ValueError(f"task policy revision mismatch for {task['id']!r}")
        item = dict(task)
        item[_PROVENANCE_KEY] = {
            "experiment_revision": experiment_revision,
            "task_hash": canonical_task_hash(task),
            "prompt_hash": prompt_hash(task),
            "suite_hash": suite_hash,
            "suite_raw_hash": raw_hash,
            "task_ordinal": task_ordinal,
            "oracle_class": policy.oracle_class,
            "headline_eligible_v1": policy.headline_eligible_v1,
            "scoreable": policy.scoreable,
        }
        loaded.append(item)
    return loaded
