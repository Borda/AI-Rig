<!-- Content is the canonical task-hygiene block for develop plugin skills. Update this file to propagate changes to all skills that Read it. -->

**Task tools may be deferred** — schema absent, direct call fails. Load before first use: `ToolSearch(query="select:TaskList,TaskCreate,TaskUpdate,TaskGet", max_results=4)`.

**Task hygiene**: Before creating tasks, call `TaskList`. Each found task:

- status `completed` if work clearly done
- status `deleted` if orphaned / no longer relevant
- keep `in_progress` only if genuinely continuing

**Task tracking**: immediately after Step 1 (scope known), TaskCreate all steps before any other work. Mark each step `in_progress` when starting, `completed` when done.
