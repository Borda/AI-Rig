---
name: advise
description: Ask Codex a read-only question with explicit model, effort, budget, and compact results.
argument-hint: '[--model MODEL] [--effort LEVEL] [--timeout-seconds N] QUESTION'
allowed-tools: Bash
---

# Ask Codex for Advice

Parse `$ARGUMENTS`: required question; optional `model`, `effort`, `timeout-seconds`, `depth`, `run-id`, `workspace`. Reject empty question. Effort omitted: classify complete question, pass selected level explicitly. Preserve caller-supplied level. Tiers: `low` = narrow mechanical change or settled fact; `medium` = bounded implementation, diagnosis, or review; `high` = cross-file, adversarial, architectural, or security judgment; `xhigh` = unusually broad and consequential; `max` = explicit caller request only.

Run `python "${CLAUDE_PLUGIN_ROOT}/bin/bridge_call.py" advise --task "<question>"`; pass each supplied option separately. For quoted text you did not author, use scratch file + `--task-file <path>` instead of `--task`; mutually exclusive. Bridge uses read-only ephemeral Codex run; default soft budget 120 seconds. Never resume advice. Follow up with fresh request containing prior `remaining` items.

Return compact JSON envelope. Preserve workspace-relative `transcript_path` and `incident` references. Do not copy the bounded transcript or verbose peer `details` into conversation.

If `status=blocked`, open the JSON file referenced by `incident`; inspect its `fault`. For `output-limit`, report incomplete advice. Split by file, symbol, or independent question; use fresh read-only calls with bounded tool output. Reconcile every scope; name unanswered work. Never treat transcript fragments as answers.
