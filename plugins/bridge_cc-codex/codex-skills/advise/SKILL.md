---
name: advise
description: Ask Claude Code a read-only question from Codex through the sandbox-external bridge.
---

> Before asking, read [User Questions](../../rules/codex-user-questions.md).

# Ask Claude Code for Advice

Call `bridge_advise` with required `task`; preserve caller-supplied `model`, `effort`, `timeout_seconds`, `depth`, and `run_id`. If effort absent, select and pass it: `low` for narrow mechanical or settled factual work; `medium` for bounded implementation, diagnosis, or review; `high` for cross-file, adversarial, architectural, or security judgment; `xhigh` for unusually broad consequential work; `max` only on explicit request. Never replace supplied effort.

MCP host binds its launch workspace. Return compact envelope; retain workspace-relative `transcript_path` and `incident` references, but never copy transcript-only peer `details`. Follow up with fresh call, never session resumption.

If `status=blocked`, open the JSON file referenced by `incident`; inspect its `fault`. For `output-limit`, report incomplete advice. Split by file, symbol, or independent question; use fresh read-only calls with bounded tool output. Reconcile every scope; name unanswered work. Never treat transcript fragments as answers.
