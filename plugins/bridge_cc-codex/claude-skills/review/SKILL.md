---
name: review
description: Request a read-only adversarial Codex review with explicit model, effort, budget, and compact findings.
argument-hint: '[--model MODEL] [--effort LEVEL] [--timeout-seconds N] REVIEW_INSTRUCTIONS'
allowed-tools: Bash
---

# Ask Codex to Review

Parse `$ARGUMENTS`: optional review instructions; optional `model`, `effort`, `timeout-seconds`, `depth`, `run-id`, `workspace`. Effort omitted: classify complete scope, pass selected level explicitly. Preserve caller-supplied level. Tiers: `low` = narrow mechanical change or settled fact; `medium` = bounded implementation, diagnosis, or review; `high` = cross-file, adversarial, architectural, or security judgment; `xhigh` = unusually broad and consequential; `max` = explicit caller request only.

Run `python "${CLAUDE_PLUGIN_ROOT}/bin/bridge_call.py" review --task "<instructions>"`; pass each supplied option separately. For quoted text you did not author, use scratch file + `--task-file <path>` instead of `--task`; mutually exclusive. Bridge runs read-only ephemeral general Codex execution with explicit adversarial-review prompt; default soft budget 300 seconds. Never resume review.

Return compact JSON envelope. Keep the bounded transcript at the bridge-reported workspace-relative artifact path. Do not inline verbose peer `details`.

If `status=blocked`, open the JSON file referenced by `incident`; inspect its `fault`. For `output-limit`, report no review verdict. Split by file, module, or symbol; use fresh read-only calls with bounded tool output. Reconcile every scope; disclose unanswered work. Never treat transcript fragments as a completed review.
