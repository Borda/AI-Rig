<!-- file: fix.md — consumers: audit/SKILL.md -->

# Mode: fix (Steps 8–10)

Loaded by `audit/SKILL.md` Step 7 only when user picks a fix option (a–c) from follow-up gate. Runs fix-dispatch → codex cross-file → re-audit convergence loop inline, then returns to SKILL.md Step 11 (Final report).

State (`$RUN_DIR`, `$AUDIT_TPL`, `LOCAL_MODE`, `summary.jsonl`) re-derived from persisted state files exactly as in SKILL.md Pre-flight — Claude Code spawns fresh shell per `Bash()` call.

## Step 8: Delegate fixes to subagents

> **HARD RULE — No inline fixes**: Orchestrator MUST NOT apply any fix directly via Edit or Write — not even single-line edits. Every fix at every severity level goes through sub-agent. Not optional. Spawning overhead always lower than context cost of 40+ inline Edit calls in a `fix all` run.

**Fix Action Hierarchy** — before any fix:

1. **Reason** — finding correct? Flagged content genuinely wrong or wrong place? Misidentified → discard, don't act.
2. **Relocate** — correct content, wrong location → move, not remove.
3. **Consolidate** — redundant with nearby content → merge into one clearer location.
4. **Minimize** — too long but valid → compress (tighten wording, remove restatements).
5. **Remove** — only if none above apply. Never remove solely because flagged as verbose.

Apply hierarchy to every fix at all severity levels.

**Dependency classification — before any dispatch**

Classify each finding from `summary.jsonl` before spawning fix agents to avoid stale-read conflicts (agent reads file A, concurrent agent modifies file A, first agent's assumptions wrong).

**Parallel-safe** (ALL four must hold — apply after same-file coalescing in criterion 4):

1. Fix writes only to the file that contains the finding
2. Finding category is in `PARALLEL_SAFE_CATEGORIES`: `{hardcoded-path, missing-confidence-block, typo, heading-hierarchy, duplicate-lines, broken-bash-fence, stale-version-ref, missing-frontmatter-field, verbose-bash-block}` — any category not in this list defaults to sequential
3. No other finding in this batch writes to a file this fix reads from
4. **Coalesce first**: group all same-file findings into a single agent prompt before applying criteria 1–3; the coalesced group is classified as one unit (prevents multiple agents racing on the same file)

Parallel-safe examples: typos, hardcoded `/Users/` paths (replacement `~/`), missing `## Confidence` block (template known), broken bash fence, heading hierarchy jump, duplicated lines within one file, verbose-bash-block (compress multi-line `if/fi` to `&&`/`||` one-liners, join sequential assignments, remove WHAT/HOW comments — see curator.md §Code Block Authoring step 7).

**Sequential (cross-file dependent)**: any finding where the fix must read another file to determine correct replacement, OR where another concurrent fix writes to a file this fix reads from.

Sequential examples: broken cross-reference (must verify target name on current disk), inventory drift (must read MEMORY.md), README sync (must read agent/skill source files).

**Two-phase dispatch**:

- **Phase 1 — Parallel basket**: issue ALL parallel-safe fix spawns in a single response. Each agent touches only its own file with self-contained changes.

- **Phase 2 — Sequential basket**: after Phase 1 complete, spawn **foundry:curator** mini-agent to re-read files modified in Phase 1 that are dependency inputs for Phase 2 fixes — orchestrator must NOT inline-read modified files (orchestration contract: see SKILL.md `## Pre-flight checks` orchestration rule). Mini-agent returns updated finding refs (refreshed line numbers, moot findings dropped). Then dispatch Phase 2 fixes via category→dependency table:

  | Category | Reads from | Serialization rule |
  | -- | -- | -- |
  | broken-cross-ref | target agent/skill file | serialize after any fix that renames/moves target |
  | inventory-drift | MEMORY.md | serialize after any fix that adds/removes agents or skills |
  | README-sync | agent/skill source files | serialize after any fix that modifies an agent/skill file |
  | orphaned-ref | disk agent/skill inventory | serialize after any fix that adds/removes agents or skills |

  Groups with no dependency on other groups' outputs run in parallel; groups depending on prior output serialize in order.

Narrate phase boundaries: `"Phase 1: N parallel-safe fixes launched"` → `"Phase 1 complete — mini-agent re-reading modified files"` → `"Phase 2: N sequential fixes starting"`.

**Adversarial pre-apply validation gate** — each proposed fix must clear two-agent gate before spawning fix agent:

1. Spawn **foundry:challenger** with finding text, file path, proposed fix — challenge: "Is this finding real? Is fix appropriate? Does it risk removing load-bearing behavioral content (runtime gates, behavioral invariants, execution constraints, `notes` checkpoints)?"
2. Spawn **foundry:curator** same context — validate: "Fix correct per Fix Action Hierarchy? Preserves behavioral integrity? Could silently remove load-bearing content even if appearing redundant or verbose?"
3. Both spawns in parallel per file. Each writes verdict to `<RUN_DIR>/gate-<file-basename>-<finding-id>.md`; returns only: `{"verdict":"approved"|"blocked","reason":"<one-line>","file":"<path>"}`
4. **Either** returns `blocked` → skip fix agent; add to `blocked_findings` with reason; surface `⚠ GATE-BLOCKED — needs human review: <reason>`
5. **Both** `approved` → proceed to fix agent

Gate applies at every severity level. Skip only for inline-exception cases (settings.json, CLAUDE.md, dead loops, model tier).

**Trivial-finding fast path** (see `agent-spawn-protocol.md` §Delegation cost discipline): batch parallel-safe findings by file adjacency, prefer `bridge:implement` when `bridge@borda-ai-rig` available. Each call must state exact finding, target paths, current evidence, permitted edits, required result, stop condition, verification command. Bridge absent/disabled → use normal per-file-type agent with `model: haiku`. Reserve full adversarial gate + top-tier agents for CRITICAL/HIGH or cross-file-dependent findings.

Fix agent by file type:

- **`.claude/agents/*.md` and `.claude/skills/*/SKILL.md`** → spawn **foundry:curator** — domain expertise in config quality, has `Write`/`Edit` tools
- **Code files** (`.py`, `.js`, `.ts`, etc.) → spawn **foundry:sw-engineer**

**Phase 4 delegation rule**: edits touching >3 files → delegate to `foundry:sw-engineer` — pass findings list + target file paths; returns compact status JSON.

Spawn one agent per affected file, batch all findings per file into single prompt. Issue **all spawns in a single response** for parallelism.

Each subagent prompt: instruct agent to run `cat "$AUDIT_TPL/fix-prompt.md"` via the Bash tool, then fill `<file path>` and findings list.

**Preferred orchestration pattern — audit-fix sub-agent**

<!-- loads: audit-fix-prompt.md -->

After gate fires (Step 7): finding count > 10 or user picked option (a) "Fix auto-fixable" or (c) "Fix ALL" → use audit-fix sub-agent pattern below (handles Steps 8–10 in isolation); otherwise inline batched pattern at end of this step.

**Gate authority**: sub-agent path → orchestrator Step 7 gate **skipped** — sub-agent runs own gate internally, authoritative. Inline batched path (≤10 findings) → orchestrator Step 7 gate authoritative, no sub-agent gate. Never double-gate.

**Gate failure fallback**: if sub-agent returns `blocked_findings: []` with `fixed > 0` and `failed == 0` but no `gate-<file>.md` files appear in `<RUN_DIR>`, surface: `⚠ GATE-SKIPPED — sub-agent did not perform adversarial gate; review fixes manually before merging.` Missing `gate-<file>.md` expected — not a failure — for any file the sub-agent recorded under `fast_path` in `fix-summary.md` (mechanical, single-file, no CRITICAL/HIGH — see `audit-fix-prompt.md` §Gate fast path). Fire the warning only when a fixed file is absent from BOTH the gate files and `fast_path`.

```bash
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
AUDIT_TPL=$(cat "${TMPDIR:-/tmp}/audit-state-${CSID}/audit-tpl" 2>/dev/null || python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/resolve_skill_subdir.py" audit templates $( [ "$LOCAL_MODE" = true ] && echo "--local" ))
cat "$AUDIT_TPL/audit-fix-prompt.md"
```

Spawn dedicated **audit-fix** sub-agent — use full prompt loaded above, pass `<RUN_DIR>` and `$AUDIT_TPL` as context values substituted into prompt. Orchestrator reads only compact JSON envelope returned; does NOT read `fix-summary.md` unless `re_audit_clean: false`, `failed > 0`, or `residual_criticals > 0`.

Finding count ≤ 10 and user picked option (b) "Fix SECURITY + CRITICAL + HIGH" → inline batched pattern (one fix-agent per file, all parallel) acceptable; no dedicated sub-agent.

**Findings that bypass fix-agent delegation:**

**NON_AUTO_FIXABLE** (authoritative set — referenced by bypass list and option (d)): Check 3 (settings.json missing permission), Check 5 critical/high (permission safety), Check 19 (model tier mismatch), CLAUDE.md contradiction, dead loop in follow-up chains.

Default (options a–b): report only — no Edit or Write tool calls for NON_AUTO_FIXABLE findings.

- **settings.json permission missing** (Check 3, 5): report only — structural JSON edits risky to delegate
- **CLAUDE.md contradiction**: raise to user — do not auto-fix (CLAUDE.md takes precedence)
- **Dead loop**: flag for user review — human judgment needed on which link to break
- **Model tier mismatch** (Check 19): report only — assignments may be intentional for cost/latency trade-offs; user decides

**"Fix ALL" option (c)**:

1. **Upfront decision collection** — before any fixes run: group all NON_AUTO_FIXABLE findings by category (settings.json / model-tier / CLAUDE.md-conflict / dead-loop — max 4 categories). For each category call `AskUserQuestion` (one call per category, honoring `communication.md` 4-question-per-call cap): list up to 4 representative findings; >4 in category → note "and N more follow same pattern". Options: (a) Apply same resolution to all in category · (b) Review each individually · (c) Skip category. Hard cap: max 4 `AskUserQuestion` calls total; overflow findings listed in final report. "Apply same resolution" valid for uniform findings; non-uniform findings force option (b).

2. **Single integrated fix pass** — after all decisions collected, run auto-fixable + user-resolved NON_AUTO_FIXABLE in one combined loop, same Phase 1 parallel / Phase 2 sequential dispatch. Low findings included. No mid-run checkpoint — all decisions already made upfront.

Apply NON_AUTO_FIXABLE fixes only on explicit user selection per category; never auto-apply.

After subagents complete, collect results and proceed to Step 10.

**Low findings** (nits): fix only when `fix all` passed — otherwise collect in final report for optional manual cleanup.

## Step 9: Codex cross-file check

After Step 8 fix agents complete, before foundry:curator re-audit:

```bash
CODEX_STATUS=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/check_bridge.py" --status 2>/dev/null || echo "absent")
[ "$CODEX_STATUS" = "available" ] && CODEX_AVAILABLE=true || CODEX_AVAILABLE=""  # timeout: 5000
_SHARED=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/resolve_shared_path.py" foundry skills/_shared 2>/dev/null || echo "plugins/cc_foundry/skills/_shared")  # timeout: 5000
[ -f "$_SHARED/codex-prepass.md" ] || { printf "⚠ WARNING: codex-prepass.md not found at $_SHARED — skipping codex pre-pass\n"; CODEX_AVAILABLE=""; }
[ -n "$CODEX_AVAILABLE" ] && cat "$_SHARED/codex-prepass.md"
```

`$CODEX_AVAILABLE` non-empty: follow codex-prepass.md instructions above, applied to combined diff of Step 8 fixes. Otherwise: `echo "⚠ codex plugin not available — skipping codex pass"`

Treat findings as additional issues entering Step 10 re-audit scope. Skip if Step 8 touched only 1 file.

## Step 10: Re-audit modified files + confidence check

For every file changed in Step 8, spawn **foundry:curator** to confirm fix resolved finding and no new issues introduced. Write full re-audit findings to `<RUN_DIR>/<file-basename>-reaudit.md`; end the full findings file with a `## Confidence` block per quality-gates.md format (Score, Gaps, Refinements); return ONLY compact JSON envelope: `{"status":"done","file":"<RUN_DIR>/<file-basename>-reaudit.md","findings":N,"severity":{"security":N,"critical":N,"high":N,"medium":N,"low":N},"confidence":0.N,"summary":"<filename>: fix confirmed, N residual findings"}`

```bash
# Replace BROKEN_NAME and FIXED_FILE with the actual values from the finding
grep -n "BROKEN_NAME" FIXED_FILE
```

**Confidence re-run**: parse confidence scores from Step 3 and Step 10 summaries. **Score < 0.80**: Step 5b already ran a three-pass remediation (double-reasoning, docs consultation, Codex adversarial) — if Step 10 re-audit still scores < 0.80 after Step 5b, flag with ⚠, include gap in final report. Recurring low-confidence gaps (same gap, same file, multiple runs) → candidate for foundry:curator `<antipatterns-to-flag>` or agent instructions.

**Convergence loop**: re-audit surfaces new fixable findings within gate-selected severity threshold → loop back to Step 8. Repeat until:

- Zero fixable findings remain → mark fix pass complete, or
- Hard limit: **5 total fix passes** (including initial Step 8) — still not converged → surface all remaining fixable findings with `⚠ CONVERGENCE LIMIT` warning; **do not re-enter Step 8; omit fix options from follow-up gate when convergence limit reached**.

Track pass count via `$RUN_DIR/fix-passes.txt` (persists across bash calls — shell state doesn't). At each Step 10 entry: `IFS= read -r PASS_COUNT < "$RUN_DIR/fix-passes.txt" 2>/dev/null || PASS_COUNT=0; PASS_COUNT=$((PASS_COUNT + 1)); echo "$PASS_COUNT" > "$RUN_DIR/fix-passes.txt"`. If `$PASS_COUNT >= 5`, stop loop immediately — do not re-enter Step 8 regardless of remaining findings. Never suppress findings to clean counter.

Audit-fix sub-agent (when used) must apply this loop internally — instruct it to keep spawning fix agents and re-audit agents until clean or 5-pass limit.

**Cross-file re-validation**: after per-file re-audit, re-run Step 4 checks sensitive to modified files:

- Check 1 (inventory drift) — if any agent or skill file modified
- Check 2 (README vs disk) — if any agent or skill added, renamed, or deleted
- Check 14a (structural tag symmetry) — if any agent or skill file modified
- Check 14b (code fence symmetry) — if any agent or skill file modified
- Check 17 (cross-file content duplication) — if 2+ files modified
- Check 25 (implicit agent references) — if any agent or skill file modified
- Check 27 (cross-plugin shared-file ref integrity) — if any skill file modified

Write findings to `<RUN_DIR>/crossfile-revalidation-pass<N>.md`, N = current pass count. Include new findings in convergence loop input for next Step 8 iteration.

After the convergence loop completes (clean or 5-pass limit), return to `audit/SKILL.md` Step 11 (Final report).
