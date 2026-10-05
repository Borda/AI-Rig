"""Loaders for the frozen codemap-cli task suites."""

from __future__ import annotations

import json

from _bench_common.benchmark_paths import TASKS_BENCH_FILE as OSS_TASKS_FILE
from _bench_common.benchmark_paths import unwrap_tasks

from _bench_query.models import TASKS_FILE, Task


def load_tasks(skill_filter: str | None = None) -> list[Task]:
    """Load benchmark tasks from tasks-code.json, optionally filtered by skill.

    Args:
        skill_filter: When given, return only tasks whose ``skill`` field equals
            this value (e.g. ``"fix"``, ``"feature"``, ``"refactor"``).

    Returns:
        List of :class:`Task` objects in the order they appear in the file.

    Examples:
        >>> tasks = load_tasks()
        >>> all(isinstance(t, Task) for t in tasks)
        True
        >>> fix_tasks = load_tasks(skill_filter="fix")
        >>> all(t.skill == "fix" for t in fix_tasks)
        True
    """
    with TASKS_FILE.open() as f:
        raw = json.load(f)
    tasks = [Task.from_dict(t) for t in raw]
    if skill_filter:
        tasks = [t for t in tasks if t.skill == skill_filter]
    return tasks


def load_oss_tasks(type_filter: str | None = None) -> list[dict]:
    """Load OSS benchmark tasks from tasks-bench.json, optionally filtered by type.

    Args:
        type_filter: When given, return only tasks with ``type == type_filter``.

    Returns:
        List of raw task dicts (structure as defined in tasks-bench.json).
    """
    if not OSS_TASKS_FILE.exists():
        return []
    with OSS_TASKS_FILE.open() as f:
        parsed = json.load(f)
    raw: list[dict] = unwrap_tasks(parsed)
    if type_filter:
        raw = [t for t in raw if t.get("type") == type_filter]
    return raw
