---
name: review
description: Request a read-only adversarial review from Claude Code through the sandbox-external bridge.
---

# Ask Claude Code to Review

Call `bridge_review` with required `task`; preserve caller-supplied `model`, `effort`, `timeout_seconds`, `depth`, and `run_id`. If effort is absent, select and pass it: `low` for narrow mechanical or settled factual work; `medium` for bounded implementation, diagnosis, or review; `high` for cross-file, adversarial, architectural, or security judgment; `xhigh` for unusually broad consequential work; `max` only on explicit request. Never replace supplied effort.

The MCP host binds its launch workspace. Return only the compact envelope; retain workspace-relative transcript and `incident` references for detail, never inline peer `details`.

If `status=blocked`, open the JSON file referenced by `incident`; inspect its `fault`. For `output-limit`, report no review verdict. Split by file, module, or symbol; use fresh read-only calls with bounded tool output. Reconcile every scope; disclose unanswered work. Never treat transcript fragments as a completed review.
