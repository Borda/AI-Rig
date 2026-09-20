---
description: Compaction contract — verbatim skill state survival across auto-compact via .temp/state/skill-contract.md
paths:
  - '**'
---

## Compaction Contract

### Verbatim-file principle

Must-keep details (run-dir, task IDs, key decisions, report paths) go in `.temp/state/skill-contract.md` — not relied on in prose summary. PreCompact hook (`task-log.js`) appends contract file **verbatim** to `session-context.md`; post-compaction re-read restores it losslessly.

Prose Compact Instructions remain best-effort. Don't over-promise zero loss for anything not in contract.

### When and where to refresh

Refresh at boundary **after** expanding phase (parallel fan-out, iteration loop, large gather), **before** next phase starts. Not after every step.

Block format and placement rule: see `compaction-contract.md`.

Hook rides auto-compact (85% context threshold) — no manual trigger. Compaction timing best-effort; only verbatim survival of *whatever contract exists* is deterministic.

**Idle gates are the expensive boundary.** An `AskUserQuestion` the user may leave open for hours outlives the prompt-cache TTL; the next turn then rewrites the whole live context at write rate (~12.5× read). Measured across review/resolve sessions: cold starts were 40–44% of all cache-write tokens, single rewrites of 240k–320k tokens. A skill cannot compact itself, so at every gate expected to idle it (1) refreshes the contract so a mid-wait `/compact` is lossless, and (2) prints one hint line **in the reply** (prose — Bash stdout is not reliably shown to the user): `` Long wait? `/compact` now — <state> saved at <path>, resume lossless. `` The resumed skill re-reads its report/state files, never the transcript.

> Manual sibling: `/foundry:session dump` before an explicit `/clear`. Contract = automatic, in-flight skill phase state, `.temp/state/skill-contract.md`; session handover = explicit, plan/decisions/lessons/files table/open loops, `.claude/state/session/`. Different trigger, different payload — never substitutes for the other.

### `keep:` semantics

Skills accepting `[--keep "<items>"]` append user's string verbatim to contract's `preserve:` line at Step 0. Free-form text; skill documents it in `argument-hint`. Optional per-invocation user preserve list — see skill-specific `<compaction>` block for details.

### Clear on completion

Delete `.temp/state/skill-contract.md` at skill completion. Hook appends whatever file holds — skill that forgets to clear leaks stale preserve items into later unrelated compaction. Clearing is mandatory, not optional.

### Stale-contract caution

If skill crashes before clearing, `.temp/state/skill-contract.md` persists. Mitigation: at Step 0, delete any pre-existing contract file before writing first boundary contract.
