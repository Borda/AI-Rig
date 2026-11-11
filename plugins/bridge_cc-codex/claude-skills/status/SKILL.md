---
name: status
description: Read a detached Codex bridge job's current state.
argument-hint: '[--workspace PATH] JOB_ID'
allowed-tools: Bash
effort: low
---

<!-- Diagram: ../../assets/images/bridge-request-arch.svg (request lifecycle; documentation only, SKILL.md wins) -->

# Read Bridge Job Status

<objective>
Read a detached Codex bridge job's current state and route its next lifecycle action.
</objective>

<workflow>

Require one job identifier. Run `python "${CLAUDE_PLUGIN_ROOT}/bin/bridge_call.py" status --job-id "<job-id>"`; always pass the same `--workspace` value the originating detached call used; a different or omitted value can resolve to another default workspace and report `missing` for a job still running elsewhere. Return JSON status unchanged, then apply the routing below. One call per check, never wrapped in a `sleep`, `ScheduleWakeup`, `ListAgents`, or `Monitor` loop; any state other than `queued`, `running` or `cancel_requested` ends the wait.

`cancel_requested` does not end the wait — the supervisor and any write-capable child are still being torn down; the final state becomes `cancelled`. Terminal states: `finished`, `cancelled`, `failed` (the supervisor itself errored), `stalled` (supervisor gone, or still queued after 120 s), `missing`. For `failed` or `stalled` route the caller to `/bridge:result`; an `implement` job may already have edited files. When the originating call omitted `--workspace`, pass the absolute directory it ran from, never `.` — a later `cd` makes `.` another workspace. A `status=error` object (exit 2) is a refused lifecycle call, such as a malformed job id: report its `error`, never retry unchanged.

On Windows, `stalled` may never appear after supervisor PID reuse; the caller must retain the launch budget and dispatch time (`--timeout-seconds`, default 600 seconds for `implement` when the option was omitted), since the status payload does not carry the budget. Use a deadline of `--timeout-seconds` × 1.2 + 9 seconds termination/drain + 120 seconds startup grace, measured from dispatch. At that deadline, stop waiting and route to `/bridge:result`; report incomplete work if no result exists, and do not infer that the child has stopped or release ownership of paths an implement job may still be editing.

</workflow>
