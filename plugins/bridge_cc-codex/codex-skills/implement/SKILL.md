---
name: implement
description: Ask Claude Code to implement one bounded write-capable change through the sandbox-external bridge.
---

> Before asking, read [User Questions](../../rules/codex-user-questions.md).

# Implement with Claude Code

Before dispatch, read `bridge_status`. Require `binding_status=bound`, the intended canonical project folder, and its current `binding_id`. If unbound or wrong project, call `bridge_bind_workspace` with empty arguments; the user types an absolute folder and separately confirms the displayed canonical target in native forms. Retain the returned `binding_id` for this call; never provide a workspace path or scope token from model text. Rebinding invalidates every older ID, even for the same folder. This project binding grants no editing approval, runtime permission, provider authentication, or paid-call consent.

Call `bridge_implement` with required `task` and current `binding_id`; preserve caller-supplied `model`, `effort`, `timeout_seconds`, `depth`, and `run_id`. If effort absent, select and pass it: `low` for narrow mechanical or settled factual work; `medium` for bounded implementation, diagnosis, or review; `high` for cross-file, adversarial, architectural, or security judgment; `xhigh` for unusually broad consequential work; `max` only on explicit request. Never replace supplied choices. The server rejects model-controlled workspace, background, and session fields and stale binding identities.

Return compact public envelope. `verdict`, `findings`, `files_touched`, `remaining`, and `blockers` must carry incomplete work and blockers; verbose evidence stays in workspace-relative `transcript_path`, never inline `details`. Before accepting consequential work, reread reported files and run relevant project checks. Refuse another cross-host dispatch at trusted inherited depth one.

If `status=blocked`, open the JSON file referenced by `incident`; inspect its `fault`. For `output-limit`, report uncertain completion. Inspect transcript, worktree delta, changed files, and checks; never replay the original write-capable task. Request verified remaining work as a fresh bounded task with file ownership and bounded tool output.
