# Task Hygiene Protocol

Task tools may be deferred — schema absent, direct call fails. Load before first use: `ToolSearch(query="select:TaskList,TaskCreate,TaskUpdate,TaskGet", max_results=4)`.

Before creating tasks, call `TaskList`. For each found task:

- status `completed` if work clearly done
- status `deleted` if orphaned / no longer relevant
- keep `in_progress` only if genuinely continuing
