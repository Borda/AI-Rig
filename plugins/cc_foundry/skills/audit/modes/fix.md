<!-- file: fix.md — consumers: audit/SKILL.md -->

# Mode: fix (Steps 8–10)

Loaded by `audit/SKILL.md` Step 7 when user picks a fix option (a–c) from follow-up gate or already authorized a concrete scoped fix. Runs fix-dispatch → codex cross-file → re-audit convergence loop inline, then returns to SKILL.md Step 11 (Final report).

State (`$RUN_DIR`, `$AUDIT_TPL`, `LOCAL_MODE`, `summary.jsonl`) re-derived from persisted state files exactly as in SKILL.md Pre-flight — Claude Code spawns fresh shell per `Bash()` call.

## Step 8: Delegate fixes to subagents

For `bridge:implement`, the Step 7 selection gate's recorded user decision naming the selected task set, or a recorded user decision already authorizing that concrete scoped task set, is the approval; workspace is the skill's absolute launch directory and network is `none` unless that decision names otherwise. Cite the decision and selected tasks in each dispatch brief, including the audit-fix orchestrator's brief; reuse it only for tasks in that set. A subagent may dispatch only when its brief carries the orchestrating skill's recorded approval; otherwise return `blocked` and never dispatch. If no approval exists, use AskUserQuestion before dispatch; if AskUserQuestion is unavailable, return `blocked` and never dispatch.

**Selection contract — before any dispatch**: retain `SELECTED_OPTION` (a, b, c, or scoped), `SEVERITY_SET` (a/c: security, critical, high, medium, low; b: security, critical, high), `NON_AUTO_FIXABLE_IDS` (the authoritative exclusions below plus every `auto_fixable:false` row), and `RESOLUTIONS` (finding id → explicit user-approved edit or skip). Collect option-(c) decisions from the section below before dispatch, including the audit-fix sub-agent. Persist these inputs to `<RUN_DIR>/fix-selection.json` and pass the literal values and path to every fix orchestrator. A row is eligible only inside `SEVERITY_SET` and with `auto_fixable:true` outside the exclusion ids, or under (c) with an explicit non-skip resolution for that id; absent/unknown eligibility stays excluded with its reason. Apply the same filter to new re-audit findings; no selection input means no fixes.

For a concrete previously authorized scope, set `SELECTED_OPTION=scoped` and persist `SCOPE_FILTER` in `<RUN_DIR>/fix-selection.json`: explicit repo-relative paths (a directory includes its descendants) and/or severities, intersecting both when supplied; an omitted dimension is unrestricted, but an empty or absent filter authorizes nothing. Set `SEVERITY_SET` to the supplied severities or all five severities above. Eligibility additionally requires `auto_fixable:true` outside `NON_AUTO_FIXABLE_IDS`; this scope never collects option-(c) decisions or applies `RESOLUTIONS`. Pass the literal `SCOPE_FILTER` with the other authority inputs; never round a concrete scope to a/b/c.

Before each pass, classify new findings against the authoritative NON_AUTO_FIXABLE classes below and add matching ids, plus every `auto_fixable:false` id, to `NON_AUTO_FIXABLE_IDS` before selection. Preserve existing `RESOLUTIONS`; a new finding never inherits approval from a different finding or a skipped category.

Use repo-relative source paths as finding keys; retain SKILL.md Step 3's plugin-prefixed `<file-slug>` mapping for every gate, residual and re-audit artifact, never a basename. For non-inventory paths, define the slug as `path-` plus the full repo-relative POSIX path encoded as UTF-8 hex, so settings, hooks and README targets remain distinct without a Step 3 entry; persist and pass this mapping too. Before each Agent spawn in Steps 8–10, Write `<RUN_DIR>/agent-watch-fix-<pass>-<phase>-<wave>.tsv` in the spawn response with one `<unique-agent-name>\t<output-file-or-dash>\t900` row per agent, including both gate actors, Phase 2 mini-agents, fix agents, the audit-fix orchestrator and re-auditors; follow SKILL.md Step 3 deadline handling. Use `-` for envelope-only/multi-output spawns.

> **HARD RULE — No inline fixes**: Orchestrator MUST NOT apply any fix directly via Edit or Write — not even single-line edits. Every fix at every severity level goes through sub-agent. Not optional. Spawning overhead always lower than context cost of 40+ inline Edit calls in a `fix all` run.

**Fix Action Hierarchy** — before any fix:

1. **Reason** — finding correct? Flagged content genuinely wrong or wrong place? Misidentified → discard, don't act.
2. **Relocate** — correct content, wrong location → move, not remove.
3. **Consolidate** — redundant with nearby content → merge into one clearer location.
4. **Minimize** — too long but valid → compress (tighten wording, remove restatements).
5. **Remove** — only if none above apply. Never remove solely because flagged as verbose.

Apply hierarchy to every fix at all severity levels.

<!-- policy-sibling: plugins/cc_foundry/skills/audit/templates/audit-fix-prompt.md (audit-fix sub-agent path: same PARALLEL_SAFE_CATEGORIES, coalescing, category-dependency rules, gate fast path and its verbose-bash-block exclusion; gate verdict files differ by design: per-finding here, per-file there) -->

**Dependency classification — before any dispatch**

Classify each finding from `summary.jsonl` before spawning fix agents to avoid stale-read conflicts (agent reads file A, concurrent agent modifies file A, first agent's assumptions wrong).

**Parallel-safe** (ALL four must hold — apply after same-file coalescing in criterion 4):

1. Fix writes only to the file that contains the finding
2. Finding category is in `PARALLEL_SAFE_CATEGORIES`: `{hardcoded-path, missing-confidence-block, typo, heading-hierarchy, duplicate-lines, broken-bash-fence, stale-version-ref, missing-frontmatter-field, verbose-bash-block}` — any category not in this list defaults to sequential
3. No other finding in this batch writes to a file this fix reads from
4. **Coalesce first**: group all same-file findings into a single agent prompt before applying criteria 1–3; the coalesced group is classified as one unit (prevents multiple agents racing on the same file)

Parallel-safe examples: typos, hardcoded `/Users/` paths (replacement `~/`), missing `## Confidence` block (template known), broken bash fence, heading hierarchy jump, duplicated lines within one file, verbose-bash-block (compress multi-line `if/fi` to `&&`/`||` one-liners, join sequential assignments, remove WHAT/HOW comments — see curator.md §Code Block Authoring step 7).

**Sequential (cross-file dependent)**: any finding where the fix must read another file to determine correct replacement, OR where another concurrent fix writes to a file this fix reads from.

Sequential examples: broken cross-reference (must verify target name on current disk), README sync (must read agent/skill source files).

**Two-phase dispatch**:

- **Phase 1 — Parallel basket**: issue ALL parallel-safe fix spawns in a single response. Each agent touches only its own file with self-contained changes.

- **Phase 2 — Sequential basket**: after Phase 1 complete, spawn **foundry:curator** mini-agent to re-read files modified in Phase 1 that are dependency inputs for Phase 2 fixes — orchestrator must NOT inline-read modified files (orchestration contract: see SKILL.md `## Pre-flight checks` orchestration rule). Mini-agent returns updated finding refs (refreshed line numbers, moot findings dropped). Then dispatch Phase 2 fixes via category→dependency table:

  | Category | Reads from | Serialization rule |
  | -- | -- | -- |
  | broken-cross-ref | target agent/skill file | serialize after any fix that renames/moves target |
  | README-sync | agent/skill source files | serialize after any fix that modifies an agent/skill file |
  | orphaned-ref | disk agent/skill inventory | serialize after any fix that adds/removes agents or skills |

  Groups with no dependency on other groups' outputs run in parallel; groups depending on prior output serialize in order.

Narrate phase boundaries: `"Phase 1: N parallel-safe fixes launched"` → `"Phase 1 complete — mini-agent re-reading modified files"` → `"Phase 2: N sequential fixes starting"`.

**Adversarial pre-apply validation gate** — each proposed fix must clear two-agent gate before spawning fix agent:

1. Spawn **foundry:challenger** with finding text, file path, proposed fix — challenge: "Is this finding real? Is fix appropriate? Does it risk removing load-bearing behavioral content (runtime gates, behavioral invariants, execution constraints, `notes` checkpoints)?"
2. Spawn **foundry:curator** same context — validate: "Fix correct per Fix Action Hierarchy? Preserves behavioral integrity? Could silently remove load-bearing content even if appearing redundant or verbose?"
3. Both spawns in parallel per file. Each writes verdict to `<RUN_DIR>/gate-<file-slug>-<finding-id>-<actor>.md` (`actor` = challenger or curator); returns only: `{"verdict":"approved"|"blocked","reason":"<one-line>","file":"<path>"}`
4. **Either** returns `blocked` → skip fix agent; add to `blocked_findings` with reason; surface `⚠ GATE-BLOCKED — needs human review: <reason>`
5. **Both** `approved` → proceed to fix agent

Gate applies to every finding that misses the fast-path criteria below, at any severity; a user-resolved NON_AUTO_FIXABLE edit (option (c)) is gated like any other before its fix agent runs. Default-report-only NON_AUTO_FIXABLE findings (options a–b) never reach a fix agent, so never reach the gate.

**Trivial-finding fast path** (see `agent-spawn-protocol.md` §Delegation cost discipline): batch parallel-safe findings by file adjacency, prefer `bridge:implement` only when `bridge@borda-ai-rig` is available and `git status --porcelain` confirms a clean index and working tree immediately before dispatch. A dirty tree, including earlier audit fixes, or bridge absent/disabled → use normal per-file-type agent with `model: haiku`; never clean or stash to satisfy bridge:implement. Each call must carry the recorded user approval, approved task set, absolute workspace and network, and state exact finding, target paths, current evidence, permitted edits, required result, stop condition, verification command. Reserve full adversarial gate + top-tier agents for findings outside the fast-path criteria (`audit-fix-prompt.md` §Gate fast path): any CRITICAL/HIGH, non-`PARALLEL_SAFE_CATEGORIES` category, `verbose-bash-block`, or cross-file-dependent.

Fix agent by file type:

- **Any `.md` file** (agents, skills, modes, templates, `_shared`, rules, CLAUDE.md, in `.claude/` or `plugins/*/`) → spawn **foundry:curator** — domain expertise in config quality, has `Write`/`Edit` tools
- **Code files** (`.py`, `.js`, `.ts`, etc.) → spawn **foundry:sw-engineer**

**Phase 4 delegation rule**: edits touching >3 files → delegate per file type, same routing as above (`.md` → `foundry:curator`, code → `foundry:sw-engineer`; never `.md` to `foundry:sw-engineer`, whose NOT-for excludes it) — pass findings list + target file paths; returns compact status JSON.

Spawn one agent per affected file, batch all findings per file into single prompt. Issue **all spawns in a single response** for parallelism.

Each subagent prompt: instruct agent to run `cat "$AUDIT_TPL/fix-prompt.md"` via the Bash tool, then fill `<file path>` and findings list.

**Preferred orchestration pattern — audit-fix sub-agent**

<!-- loads: audit-fix-prompt.md -->

After the Step 7 selection or existing scoped authorization: finding count > 10, user picked option (a) "Fix auto-fixable" or (c) "Fix ALL", or `SELECTED_OPTION=scoped` at any count → use audit-fix sub-agent pattern below (handles Steps 8–10 in isolation); otherwise inline batched pattern at end of this step.

**Pre-apply validation gate authority**: sub-agent path → orchestrator pre-apply validation gate **skipped** — sub-agent runs that validation internally, authoritative. Inline batched path (≤10 findings) → orchestrator pre-apply validation gate authoritative, no sub-agent validation gate. Never double-gate. The Step 7 user follow-up gate precedes either path unless scoped dispatch uses already granted authority; scoped dispatch asks no new selection question but retains pre-apply validation.

**Gate failure fallback**: for each fixed file outside `fast_path`, require both approved `<RUN_DIR>/fix-pass-<N>/gate-<file-slug>-<actor>.md` verdicts for its pass; if either is absent, surface `⚠ GATE-SKIPPED — sub-agent did not perform adversarial gate; review fixes manually before merging.` in the Step 11 final report, not only as reply text before further tool calls. Gate files are not required for files recorded under `fast_path` in `fix-summary.md` (mechanical, single-file, no CRITICAL/HIGH — see `audit-fix-prompt.md` §Gate fast path).

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name foundry) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
IFS= read -r AUDIT_TPL < "${TMPDIR:-/tmp}/audit-state-${CSID}/audit-tpl" 2>/dev/null || AUDIT_TPL=$(python "$PLUGIN_ROOT/bin/resolve_skill_subdir.py" audit templates $( [ "$LOCAL_MODE" = true ] && echo "--local" ))
python "$PLUGIN_ROOT/bin/load_mode.py" audit templates audit-fix-prompt.md $( [ "$LOCAL_MODE" = true ] && echo "--local" )
echo "AUDIT_TPL=$AUDIT_TPL"
```

Spawn dedicated **audit-fix** sub-agent with explicit `subagent_type="general-purpose"` (it dispatches gate and fix agents itself, so it needs the Agent tool no specialist has) — use full prompt loaded above, pass the recorded user approval, approved task set, absolute workspace, network, `<RUN_DIR>`, `$AUDIT_TPL`, `SELECTED_OPTION`, `SEVERITY_SET`, `SCOPE_FILTER` when scoped, `NON_AUTO_FIXABLE_IDS`, `RESOLUTIONS`, and the source-path-to-slug mapping as literal context values, after decision collection. Orchestrator reads only compact JSON envelope returned; does NOT read `fix-summary.md` unless `re_audit_clean: false`, `failed > 0`, or `residual_criticals > 0`.

Finding count ≤ 10 and user picked option (b) "Fix SECURITY + CRITICAL + HIGH" → inline batched pattern (one fix-agent per file, all parallel) acceptable; no dedicated sub-agent.

**Findings that bypass fix-agent delegation:**

**NON_AUTO_FIXABLE** (authoritative set — referenced by bypass list and option (d)): Check 3 (settings.json missing permission), Check 5 critical/high (permission safety), Check 19 (model tier mismatch), CLAUDE.md contradiction, dead loop in follow-up chains.

Default (options a–b): report only — no Edit or Write tool calls for NON_AUTO_FIXABLE findings.

- **settings.json permission missing** (Check 3, 5): report only — structural JSON edits risky to delegate
- **CLAUDE.md contradiction**: raise to user — do not auto-fix (CLAUDE.md takes precedence)
- **Dead loop**: flag for user review — human judgment needed on which link to break
- **Model tier mismatch** (Check 19): report only — assignments may be intentional for cost/latency trade-offs; user decides

**"Fix ALL" option (c)**:

1. **Upfront decision collection** — before any fixes run: include every `auto_fixable:false` row and every NON_AUTO_FIXABLE id, grouped by category, including advisory and unknown-eligibility categories. Write every id, source path, exclusion reason and exact proposed resolution to `<RUN_DIR>/fix-decisions.md`. Present all groups through `AskUserQuestion` before dispatch; never silently omit a group or severity. Honor `communication.md` limits of 4 questions per call and 4 calls total: pack groups into decision bundles when needed, referencing every bundled id and its proposed edit in the decision file. Options: (a) Apply listed resolutions · (b) Supply individual resolutions · (c) Skip bundle. A uniform category may share a resolution; non-uniform rows require their own explicitly approved edits. Persist each id's approved edit or skip in `RESOLUTIONS`; unanswered rows remain ineligible. A bundle approval covers only its listed edits, never a blanket permission for future findings. Severity-free advisories such as Check 47 remain outside the fix gate.

2. **Single integrated fix pass** — after all decisions collected, run auto-fixable + explicitly user-resolved excluded rows in one combined loop, same Phase 1 parallel / Phase 2 sequential dispatch. Low findings included. No mid-run checkpoint — all decisions already made upfront.

Apply NON_AUTO_FIXABLE fixes only on explicit user selection per category; never auto-apply.

After inline fix agents complete, collect results and run Steps 9–10. The dedicated audit-fix sub-agent runs those checks and convergence internally; after its terminal summary, proceed directly to Step 11 without incrementing the pass counter or dispatching another loop.

**Low findings** (nits): included under options (a) and (c), subject to the selection contract; option (b) excludes them. In the Step 11 report list every ineligible row of any severity with id, source path, severity and reason (outside selected scope, systemic/advisory exclusion, skipped/unanswered resolution, unknown eligibility or blocked gate); no exclusion silently disappears.

## Step 9: Codex cross-file check

After Step 8 fix agents complete, before foundry:curator re-audit:

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name foundry) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "$PLUGIN_ROOT/bin/check_bridge.py" --status > "${TMPDIR:-/tmp}/audit-state-${CSID}/codex-status" 2>/dev/null || echo "absent" > "${TMPDIR:-/tmp}/audit-state-${CSID}/codex-status"  # timeout: 5000
IFS= read -r CODEX_STATUS < "${TMPDIR:-/tmp}/audit-state-${CSID}/codex-status" || CODEX_STATUS="absent"
[ "$CODEX_STATUS" = "available" ] && printf 'CODEX_AVAILABLE=%s\n' true || printf 'CODEX_AVAILABLE=%s\n' false  # timeout: 5000
[ "$CODEX_STATUS" = "available" ] && python "$PLUGIN_ROOT/bin/load_shared_doc.py" --optional foundry skills/_shared codex-prepass.md  # timeout: 5000
```

Printed `CODEX_AVAILABLE=true` and the codex-prepass.md text printed above (a `⚠ WARNING` line instead = doc missing → skip, as Otherwise): follow codex-prepass.md instructions above, applied to combined diff of Step 8 fixes. Otherwise: `echo "⚠ codex plugin not available — skipping codex pass"`

Treat findings as additional issues entering Step 10 re-audit scope. Skip if Step 8 touched only 1 file.

## Step 10: Re-audit modified files + confidence check

For every file changed in Step 8, spawn **foundry:curator** to confirm fix resolved finding and no new issues introduced. Write full re-audit findings to `<RUN_DIR>/<file-slug>-reaudit.md`; end the full findings file with a `## Confidence` block per quality-gates.md format (Score, Gaps, Refinements); return ONLY compact JSON envelope: `{"status":"done","file":"<RUN_DIR>/<file-slug>-reaudit.md","findings":N,"severity":{"security":N,"critical":N,"high":N,"medium":N,"low":N},"confidence":0.N,"summary":"<filename>: fix confirmed, N residual findings"}`

Confirm a broken-name fix with the Grep tool: pattern = the broken name from the finding, path = the fixed file.

**Confidence re-run**: parse confidence scores from Step 3 and Step 10 summaries. **Score < 0.80**: Step 5b already ran a three-pass remediation (double-reasoning, docs consultation, Codex adversarial) — if Step 10 re-audit still scores < 0.80 after Step 5b, flag with ⚠, include gap in final report. Recurring low-confidence gaps (same gap, same file, multiple runs) → candidate for foundry:curator `<antipatterns-to-flag>` or agent instructions.

**Convergence loop**: first run the cross-file re-validation below and combine its findings with per-file and Codex results; then apply the selection contract. Re-audit surfaces new eligible fixable findings within gate-selected severity threshold → loop back to Step 8. Repeat until:

- Zero fixable findings remain → mark fix pass complete, or
- Plateau: weighted score W_n per quality-gates.md §Adversarial Convergence Loop does not strictly decrease versus the prior pass — stop, surface remaining findings with `⚠ NOT CONVERGING`; do not re-enter Step 8 (same follow-up-gate rule as the limit below), or
- Hard limit: **5 total fix passes** (including initial Step 8) — still not converged → surface all remaining fixable findings with `⚠ CONVERGENCE LIMIT` warning; **do not re-enter Step 8; omit fix options from follow-up gate when convergence limit reached**.

Track pass count via `$RUN_DIR/fix-passes.txt` (persists across bash calls — shell state doesn't). At each Step 10 entry: `IFS= read -r PASS_COUNT < "$RUN_DIR/fix-passes.txt" 2>/dev/null || PASS_COUNT=0; PASS_COUNT=$((PASS_COUNT + 1)); echo "$PASS_COUNT" > "$RUN_DIR/fix-passes.txt"`. If `$PASS_COUNT >= 5`, stop loop immediately — do not re-enter Step 8 regardless of remaining findings. Never suppress findings to clean counter.

Audit-fix sub-agent (when used) must apply this loop internally — instruct it to keep spawning fix agents and re-audit agents until clean or 5-pass limit.

**Cross-file re-validation**: after per-file re-audit, re-run Step 4 checks sensitive to modified files:

- Check 1 (inventory discovery) — if any agent or skill file modified
- Check 2 (README vs disk) — if any agent or skill added, renamed, or deleted
- Check 14a (structural tag symmetry) — if any agent or skill file modified
- Check 14b (code fence symmetry) — if any agent or skill file modified
- Check 17 (cross-file content duplication) — if 2+ files modified
- Check 25 (implicit agent references) — if any agent or skill file modified
- Check 27 (cross-plugin shared-file ref integrity) — if any skill file modified

Write findings to `<RUN_DIR>/crossfile-revalidation-pass<N>.md`, N = current pass count. Include new findings in convergence loop input for next Step 8 iteration.

After the convergence loop completes (clean or 5-pass limit), return to `audit/SKILL.md` Step 11 (Final report).
