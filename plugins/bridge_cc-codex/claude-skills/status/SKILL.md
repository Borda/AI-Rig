---
name: status
description: Read a detached Codex bridge job's current state.
argument-hint: '[--workspace PATH] JOB_ID'
allowed-tools: Bash
effort: low
---

# Read Bridge Job Status

Require one job identifier. Run `python "${CLAUDE_PLUGIN_ROOT}/bin/bridge_call.py" status --job-id "<job-id>"`; always pass the same `--workspace` value the originating detached call used; a different or omitted value can resolve to another default workspace and report `missing` for a job still running elsewhere. Return JSON status unchanged — passthrough only; leave interpretation to `/bridge:result`. One call per check, never wrapped in a `sleep`, `ScheduleWakeup`, `ListAgents`, or `Monitor` loop; any state other than `queued` or `running` ends the wait.

Exception: `cancel_requested` does not end the wait — the supervisor and any write-capable child are still being torn down; the final state becomes `cancelled`. Terminal states: `finished`, `cancelled`, `failed` (the supervisor itself errored), `stalled` (supervisor gone, or still queued after 120 s), `missing`. For `failed` or `stalled` route the caller to `/bridge:result`; an `implement` job may already have edited files. When the originating call omitted `--workspace`, pass the absolute directory it ran from, never `.` — a later `cd` makes `.` another workspace. A `status=error` object (exit 2) is a refused lifecycle call, such as a malformed job id: report its `error`, never retry unchanged.
