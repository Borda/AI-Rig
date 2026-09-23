---
name: cancel
description: Request cancellation of a running detached Codex bridge job.
argument-hint: JOB_ID
allowed-tools: Bash
---

# Cancel a Bridge Job

Require one job identifier. Run `python "${CLAUDE_PLUGIN_ROOT}/bin/bridge_call.py" cancel --job-id "<job-id>"`; always pass the same `--workspace` value the originating detached call used, since a different or omitted value resolves to another default workspace. Report returned cancellation-request state; do not claim termination complete. On `cancel_requested`, direct caller to poll `/bridge:status` or `/bridge:result` for final state. A `missing` result proves nothing about the job: mismatched workspace, a job that never existed, or one that finished and was pruned — report the possibilities rather than a termination claim; the job store cannot distinguish the last two. Preserve returned incident, transcript, or workspace-delta paths.
