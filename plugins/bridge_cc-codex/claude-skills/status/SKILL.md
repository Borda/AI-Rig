---
name: status
description: Read a detached Codex bridge job's current state.
argument-hint: JOB_ID
allowed-tools: Bash
---

# Read Bridge Job Status

Require one job identifier. Run `python "${CLAUDE_PLUGIN_ROOT}/bin/bridge_call.py" status --job-id "<job-id>"`; always pass the same `--workspace` value the originating detached call used; a different or omitted value can resolve to another default workspace and report `missing` for a job still running elsewhere. Return JSON status unchanged — passthrough only; leave interpretation to `/bridge:result`. One call per check, never wrapped in a `sleep`, `ScheduleWakeup`, `ListAgents`, or `Monitor` loop; any state other than `queued` or `running` ends the wait.
