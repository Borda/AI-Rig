---
name: implement
description: Ask Codex for one bounded write-capable change with compact results.
argument-hint: '[--model MODEL] [--effort LEVEL] [--timeout-seconds N] [--background] [--session-id UUID] TASK'
allowed-tools: Bash
---

# Implement with Codex

Implement one bounded change in selected workspace. Parse `$ARGUMENTS`: required task; optional `model`, `effort`, `timeout-seconds`, `background`, `session-id`, `depth`, `run-id`, `workspace`. Reject empty task. Session ID only follows up same task in same workspace. Effort omitted: classify complete task, pass selected level explicitly. Preserve caller-supplied level. Tiers: `low` = narrow mechanical change or settled fact; `medium` = bounded implementation, diagnosis, or review; `high` = cross-file, adversarial, architectural, or security judgment; `xhigh` = unusually broad and consequential; `max` = explicit caller request only.

Run `python "${CLAUDE_PLUGIN_ROOT}/bin/bridge_call.py" implement --task "<task>"`; pass each supplied option separately. Never interpolate task shell syntax. If brief contains text you did not author (review comment, issue body, reviewer finding), use scratch file + `--task-file <path>` instead of `--task`; mutually exclusive. Default soft budget: 600 seconds; bridge enforces documented hard cutoff. Call is write-capable under peer host's normal permission mode. Never auto-retry after timeout.

Return compact public envelope. `verdict`, `findings`, `files_touched`, `remaining`, and `blockers` must expose incomplete work and blockers; verbose evidence stays only in workspace-relative transcript referenced by `transcript_path`. Do not copy transcript-only `details` into conversation. If detached, return job identifier; direct caller to `/bridge:status`, `/bridge:result`, or `/bridge:cancel`. During detached run, do not edit task-named paths. After completion, re-read every `files_touched` path before further edits.

If `status=blocked`, open the JSON file referenced by `incident`; inspect its `fault`. For `output-limit`, report uncertain completion. Inspect the transcript, worktree delta, changed files, and checks; never replay the original write-capable task. Request verified remaining work as a fresh bounded task with file ownership and bounded tool output.
