---
name: review
description: Request a read-only adversarial Codex review with explicit model, effort, budget, and compact findings.
argument-hint: '[--model MODEL] [--effort LEVEL] [--timeout-seconds N] REVIEW_INSTRUCTIONS'
allowed-tools: Bash
---

# Ask Codex to Review

Parse `$ARGUMENTS`: required review instructions; optional `model`, `effort`, `timeout-seconds`, `depth`, `run-id`, `workspace`. Reject empty review instructions. Not a general question; for that use `/bridge:advise`. Effort omitted: classify complete scope, pass level explicitly. Preserve caller-supplied level. Tiers: `minimal` = trivial lookup or one-line mechanical confirmation; `low` = narrow mechanical change or settled fact; `medium` = bounded implementation, diagnosis, or review; `high` = cross-file, adversarial, architectural, or security judgment; `xhigh` = unusually broad and consequential; `max` = explicit caller request only.

Run `python "${CLAUDE_PLUGIN_ROOT}/bin/bridge_call.py" review --task "<instructions>"`; pass each option separately. For quoted text you did not author, use scratch file + `--task-file <path>` instead of `--task`; mutually exclusive. Bridge runs read-only ephemeral Codex execution with adversarial-review prompt; default soft budget 300 seconds. Never resume review.

When `--timeout-seconds` × 1.2 can reach the ~10 min foreground Bash cap (observed high-effort runs: 300–680 s), or the caller has independent work to continue, run the same command as a background Bash call (`run_in_background: true`) — its exit is the completion notification; never poll it with `sleep`, `ScheduleWakeup`, `ListAgents`, or `Monitor`.

Return compact JSON envelope. Keep bounded transcript at bridge-reported workspace-relative artifact path. Never inline peer `details`. Add one usage line copied from the envelope: `codex <model>/<effort> · in <input_tokens> (cached <cached_input_tokens>) · out <output_tokens> · <duration_seconds>s`; omit a counter the envelope lacks; never estimate cost.

If `status=blocked`, open the JSON file referenced by `incident` and inspect its `fault`; `incident` may be `null` — then report blocked with no incident detail. For `output-limit`, report no review verdict. Split by file, module, or symbol; use fresh read-only calls with bounded tool output. If `status=timeout`, report the returned `effort` and the unreviewed scope; narrow the scope or raise `--timeout-seconds` — never resend at a higher effort against the same budget. If `status=refused`, report its `fault`: a request at inherited depth one or greater is refused (`recursion-depth`); never retry — the request already exceeded permitted cross-host chain depth. Reconcile every scope; disclose unanswered work. Never treat transcript fragments as a completed review.
