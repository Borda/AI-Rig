"""Shared benchmark suite paths and task metadata helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

#: Directory (repo-relative) where every runner writes its results JSONL.
RESULTS_DIR = Path("benchmarks/results")

#: The package lives under benchmarks/_bench_common/, while suites are direct children of
#: benchmarks/. Resolve from the benchmark root rather than this package directory.
TASKS_BENCH_FILE = Path(__file__).resolve().parents[1] / "suites" / "tasks-bench.json"


def gt_is_pending(task: dict) -> bool:
    """Return whether a task's ground truth is an unmaterialized placeholder.

    A ``gt_pending`` task was authored without its target repo present, so it carries a
    placeholder ground truth and stale stage anchors; runners skip it until it is
    materialized via ``generate-tasks-bench.py --update``.

    Args:
        task: A task dict from a suites/*.json file.

    Returns:
        ``True`` when ``task["ground_truth"]["gt_pending"]`` is truthy, else ``False``.

    Examples:
        >>> gt_is_pending({"ground_truth": {"gt_pending": True}})
        True
        >>> gt_is_pending({"ground_truth": {}})
        False
        >>> gt_is_pending({})
        False
    """
    return bool(task.get("ground_truth", {}).get("gt_pending"))


def unwrap_tasks(parsed: Any) -> list:
    """Return the task list from a parsed suite file, accepting both container shapes.

    Suite files are either a bare JSON list of tasks or an object with a ``"tasks"`` key
    (alongside repo metadata). This normalizes both to the list.

    Args:
        parsed: The ``json.load`` result of a suite file.

    Returns:
        The task list (``parsed["tasks"]`` for a dict, else ``parsed`` unchanged).

    Examples:
        >>> unwrap_tasks({"tasks": [1, 2]})
        [1, 2]
        >>> unwrap_tasks([1, 2])
        [1, 2]
    """
    return parsed["tasks"] if isinstance(parsed, dict) else parsed
