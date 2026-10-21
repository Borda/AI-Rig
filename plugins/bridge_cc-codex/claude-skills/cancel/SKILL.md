---
name: cancel
description: Request cancellation of a running detached Codex bridge job.
argument-hint: '[--workspace PATH] JOB_ID'
allowed-tools: Bash
effort: low
---

# Cancel a Bridge Job

Require one job identifier. Run `python "${CLAUDE_PLUGIN_ROOT}/bin/bridge_call.py" cancel --job-id "<job-id>"`; always pass the same `--workspace` value the originating detached call used, since a different or omitted value resolves to another default workspace. Report returned cancellation-request state; do not claim termination complete. On `cancel_requested`, direct caller to check `/bridge:status` or `/bridge:result` once when there is a reason to, never in a loop, for final state; the write-capable child may still be running until then. Any other returned state (`finished`, `cancelled`, `failed`, `stalled`) means nothing was left to cancel: report it and route to `/bridge:result`. When the originating call omitted `--workspace`, pass the absolute directory it ran from, never `.`. A `missing` result proves nothing about the job: mismatched workspace, a job that never existed, or one that finished and was pruned — report the possibilities rather than a termination claim; the job store cannot distinguish the last two. Preserve returned incident, transcript, or workspace-delta paths.
