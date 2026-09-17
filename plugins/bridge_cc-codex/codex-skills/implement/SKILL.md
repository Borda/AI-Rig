---
name: implement
description: Ask Claude Code to implement one bounded write-capable change through the sandbox-external bridge.
---

> Before asking, read [User Questions](../../rules/codex-user-questions.md).

# Implement with Claude Code

Call `bridge_implement` with required `task`; preserve caller-supplied `model`, `effort`, `timeout_seconds`, `depth`, and `run_id`. If effort is absent, select and pass it: `low` for narrow mechanical or settled factual work; `medium` for bounded implementation, diagnosis, or review; `high` for cross-file, adversarial, architectural, or security judgment; `xhigh` for unusually broad consequential work; `max` only on explicit request. Never replace supplied choices. The MCP host binds its launch workspace and rejects model-controlled workspace, background, and session fields.

Return the compact public envelope. `verdict`, `findings`, `files_touched`, `remaining`, and `blockers` must carry incomplete work and blockers; verbose evidence stays in the workspace-relative `transcript_path`, never inline `details`. Before accepting consequential work, reread reported files and run relevant project checks. Refuse another cross-host dispatch at trusted inherited depth one.

If `status=blocked`, open the JSON file referenced by `incident`; inspect its `fault`. For `output-limit`, report uncertain completion. Inspect the transcript, worktree delta, changed files, and checks; never replay the original write-capable task. Request verified remaining work as a fresh bounded task with file ownership and bounded tool output.
