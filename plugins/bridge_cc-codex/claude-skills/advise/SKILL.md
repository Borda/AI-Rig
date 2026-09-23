---
name: advise
description: Ask Codex a read-only question with explicit model, effort, budget, and compact results.
argument-hint: '[--model MODEL] [--effort LEVEL] [--timeout-seconds N] QUESTION'
allowed-tools: Bash
---

# Ask Codex for Advice

Parse `$ARGUMENTS`: required question; optional `model`, `effort`, `timeout-seconds`, `depth`, `run-id`, `workspace`. Reject empty question. Not a structured critique; for adversarial review use `/bridge:review`. Effort omitted: classify complete question, pass level explicitly. Preserve caller-supplied level. Tiers: `minimal` = trivial lookup or one-line mechanical confirmation; `low` = narrow mechanical change or settled fact; `medium` = bounded implementation, diagnosis, or review; `high` = cross-file, adversarial, architectural, or security judgment; `xhigh` = unusually broad and consequential; `max` = explicit caller request only.

Run `python "${CLAUDE_PLUGIN_ROOT}/bin/bridge_call.py" advise --task "<question>"`; pass each option separately. For quoted text you did not author, or question text containing a flag-shaped token (e.g. `--effort`), use scratch file + `--task-file <path>` instead of `--task`; mutually exclusive; delete the scratch file once the call returns. Bridge uses read-only ephemeral Codex run; default soft budget 120 seconds. Never resume advice. Follow up with fresh request containing prior `remaining` items.

Return compact JSON envelope. Preserve workspace-relative `transcript_path` and `incident` references. Never copy bounded transcript or peer `details` into conversation.

If the Bash call fails before producing JSON (missing interpreter, non-zero exit, malformed stdout), stop and report the raw failure; never fabricate a status. If `status=blocked`, open the JSON file referenced by `incident` and inspect its `fault`; `incident` may be `null` — then report blocked with no incident detail. For `output-limit`, report incomplete advice. Split by file, symbol, or independent question; use fresh read-only calls with bounded tool output. If `status=timeout`, report the returned `effort` and the incomplete work; narrow the scope or raise `--timeout-seconds` — never resend at a higher effort against the same budget. If `status=refused`, report its `fault`: a request at inherited depth one or greater is refused (`recursion-depth`); never retry. Reconcile every scope; name unanswered work. Never treat transcript fragments as answers.
