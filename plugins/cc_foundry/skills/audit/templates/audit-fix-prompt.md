<!-- file: audit-fix-prompt.md — consumers: audit/modes/fix.md Step 8 -->

<!-- Canonical multi-file fix orchestration. NOT derived from fix-prompt.md (per-file only). Keep both in sync when changing shared audit-fix behavior. -->

<!-- policy-sibling: plugins/cc_foundry/skills/audit/modes/fix.md (Step 8 orchestrator path: same PARALLEL_SAFE_CATEGORIES, coalescing, category-dependency rules, gate fast path and its verbose-bash-block exclusion; gate verdict files differ by design: per-finding there, per-file here) -->

Read `<RUN_DIR>/summary.jsonl` — the findings list (one JSON object per line). Run `cat "$AUDIT_TPL/fix-prompt.md"` via the Bash tool for the per-file fix prompt template.

**Authority inputs**: require the caller's `SELECTED_OPTION` and `SEVERITY_SET`, plus `NON_AUTO_FIXABLE_IDS` and `RESOLUTIONS` from `<RUN_DIR>/fix-selection.json`. The parent collects option-(c) decisions before dispatch; this sub-agent never widens that authority. Option (a) includes all severities but only `auto_fixable:true` rows outside the exclusion ids; (b) additionally limits to security/critical/high; (c) includes auto-fixable rows plus excluded or `auto_fixable:false` rows only when that id has an explicit non-skip user resolution. Missing eligibility is report-only; missing selection inputs stop dispatch. Filter before coalescing, gating or fixing, including newly discovered findings on every pass. Pass each approved resolution verbatim to the owning fix agent.

For `SELECTED_OPTION=scoped`, require `SCOPE_FILTER` from `<RUN_DIR>/fix-selection.json`: explicit repo-relative paths (directories include descendants) and/or severities; intersect both when supplied, with an omitted dimension unrestricted and an empty or absent filter authorizing nothing. Intersect with `SEVERITY_SET` and `auto_fixable:true` outside `NON_AUTO_FIXABLE_IDS`; never collect option-(c) decisions or apply `RESOLUTIONS` on this route. Preserve this exact filter on every pass.

<!-- policy-sibling: plugins/cc_foundry/skills/audit/modes/fix.md §NON_AUTO_FIXABLE (same five exclusion classes) -->

Before each pass, classify new findings against these authoritative NON_AUTO_FIXABLE classes: Check 3 (settings.json missing permission), Check 5 critical/high (permission safety), Check 19 (model tier mismatch), CLAUDE.md contradiction, dead loop in follow-up chains. Add matching ids, plus every `auto_fixable:false` id, to `NON_AUTO_FIXABLE_IDS` before selection. Preserve existing `RESOLUTIONS`; a new finding never inherits approval from a different finding or a skipped category. Apply severity-table.md's closed advisory list; unknown eligibility stays excluded with its reason.

Use repo-relative source paths as file keys and the caller's SKILL.md Step 3 plugin-prefixed `<file-slug>` mapping for artifacts, never basenames. For non-inventory paths use `path-` plus the full repo-relative POSIX path encoded as UTF-8 hex; persist and reuse that mapping. Each pass gets `<RUN_DIR>/fix-pass-<N>/` for its gate and residual files so earlier evidence is retained; the artifact names below are relative to that pass directory. For every Agent spawn (gate challenger and curator, Phase 1/2 fixes, Phase 2 mini-agent, R1, R2, cross-file revalidation), Write `<RUN_DIR>/fix-pass-<N>/agent-watch-fix-<N>-<phase>-<wave>.tsv` in the same response, one `<unique-agent-name>\t<output-file-or-dash>\t900` row per spawn; use `-` for multi-file/envelope-only batches. Follow SKILL.md Step 3 notification/deadline handling using only this pass directory; a missing output is a coverage gap and cannot establish clean status. The parent's run-root watch files belong to the parent alone.

On each child completion notification, re-read the persisted pass count and run the watch check for this sub-agent's children:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/audit-state-${CSID}/run-dir" || exit 1
[ -n "$RUN_DIR" ] || exit 1
IFS= read -r PASS_COUNT < "$RUN_DIR/fix-passes.txt"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/agent_watch.py" --state-dir "$RUN_DIR/fix-pass-$PASS_COUNT"  # timeout: 5000
```

**Dependency classification**: (1) Coalesce all same-file findings into one group first. (2) Classify each coalesced group: parallel-safe = category in PARALLEL_SAFE_CATEGORIES {hardcoded-path, missing-confidence-block, typo, heading-hierarchy, duplicate-lines, broken-bash-fence, stale-version-ref, missing-frontmatter-field, verbose-bash-block} AND fix writes only to its own file AND no other group writes to a file this group reads. All others sequential.

**Model tiering (gate + fix calls)**: tier on whether the task PRODUCES a judgment or merely APPLIES one.

- Use `haiku` or `bridge:implement` for applying an exact supplied replacement or confirming mechanical facts (grep counts, numeric equality, file existence, table-row presence). Use bridge:implement only if the bridge is available and `git status --porcelain` confirms a clean index and working tree immediately before dispatch; a dirty tree (including earlier fixes) uses the per-file `haiku` agent, never cleanup or stash. Every bridge brief must include the exact finding, target path, current evidence, permitted edit, expected result, stop condition, and verification command.
- Use `sonnet` for fixes needing judgment about placement or phrasing.
- Use `opus` for adversarial gates on CRITICAL/HIGH findings, and any task needing empirical probing or cross-file authority determination.

Once a gate returns a verbatim replacement string, every downstream call is transcription; a gate that must derive correct wording from history, runtime behavior, or conflicting sources earns `opus` regardless of the resulting diff size.

**Gate fast path** (mandated by `_shared/agent-spawn-protocol.md` §Delegation cost discipline: the two-agent gate on a mechanical, unambiguous, single-file substitution is "cost without signal"): a file qualifies for the fast path only when ALL of its approved findings are in PARALLEL_SAFE_CATEGORIES AND the coalesced group is single-file by the classification above (writes only its own file, reads no file another group writes) AND no finding on that file is CRITICAL or HIGH AND none is `verbose-bash-block` (parallel-safe, but its compression can drop load-bearing `# timeout: N` and why-comments, so it always takes the full gate).

- Fast-path files skip the pre-apply gate and go straight to their fix agent under the Model tiering rule above; record them under `fast_path` in the completion summary below.
- Every other file — any CRITICAL or HIGH finding, any cross-file dependency, any category outside PARALLEL_SAFE_CATEGORIES — takes the full gate below unchanged (findings where a wrong fix has real cost keep the full gate regardless of how small the diff looks).

**Adversarial pre-apply gate**: for each unique file that didn't qualify for the fast path, spawn **foundry:challenger** AND **foundry:curator** in parallel — challenge/validate each finding batch: "Is each finding real? Is the fix appropriate? Does any fix risk removing load-bearing behavioral or structural/display content (e.g. CommonMark fence nesting, template rendering, visual output structure)?" Each writes its own verdict to `<RUN_DIR>/fix-pass-<N>/gate-<file-slug>-<actor>.md` (`actor` = challenger or curator); return `{"verdict":"approved"|"blocked","reason":"<one-line>","file":"<path>"}`. Either returns `blocked` → mark findings for that file as blocked (add to `blocked_findings` list with reason); skip fix agent. Proceed to fix agent only if both return `approved`. Issue all gate spawns in a single response (parallel).

After gate verdicts received: **Phase 1 — Parallel basket**: issue all parallel-safe approved fix spawns in a single response, with `<RUN_DIR>/fix-pass-<N>/agent-watch-fix-<N>-p1-<wave>.tsv` (one row per fix agent, `-`, `900`) written in that same response; collect results via completion notifications and the pass-local `agent_watch.py` check above — never a waiting tool.

**Phase 2 — Sequential basket**: re-read the files modified in Phase 1 that are dependency inputs for Phase 2 fixes.

- Bounded direct read — narrows the "do NOT inline-read — orchestration contract" rule rather than dropping it: run `wc -l` over the dependency-input set first; at ≤10 files AND ≤2000 lines total, read them yourself with the Read tool, because this prompt already runs inside a disposable fix sub-agent whose context ends with the pipeline, so paying a ~120K-token spawn to avoid ~5–10K tokens of reads is inverted.
- Above either bound, keep the original contract: spawn a foundry:curator mini-agent to do the re-read, and do not inline-read.
- Use category→dependency rules: broken-cross-ref reads target agent/skill file; inventory-drift reads MEMORY.md; README-sync reads agent/skill source files; orphaned-ref reads the disk agent/skill inventory.
- Serialize groups where group A output is group B input; parallel within independent groups.

For each file that passed the gate or qualified for the fast path, spawn one fix agent (foundry:curator for .md files, foundry:sw-engineer for .js/.py files) with all approved findings batched into a single prompt — pass `model:` per the Model tiering rule above (parallel-safe/mechanical findings → `haiku`, override the agent's own opus-class default; placement/phrasing judgment → `sonnet`; never spend a fix-spawn on `opus`, reserved for the gate).

After all fix agents complete: **Step R1 — curator re-audit**: spawn foundry:curator re-audit agents in batches of up to 5 changed files per spawn (BATCH_SIZE_MIN-aligned) to confirm fixes held — each spawn re-reads every file in its batch and returns per-file verdicts and remaining/new findings in one envelope: `{"files":[{"file":"<repo-relative path>","held":true|false,"note":"<one-line>","findings":[{"id":"<id>","sev":"<severity>","line":N,"category":"<category>","auto_fixable":true|false,"one_line":"<description>"}]}]}`. Every changed file is still re-audited; only spawn packaging changes.

**Step R2 — challenger residual scan**: for every file where a CRITICAL finding was fixed, spawn foundry:challenger in batches of up to 2 files per spawn (ADVERSARIAL_BATCH_SIZE-aligned — adversarial passes use smaller batches for deeper per-file attention, so keep this batch below R1's) with prompt: "Fresh adversarial read of each of `<file list>`. No context from prior audit — read each file yourself before judging it. Find any CRITICAL issues present NOW. Return ONLY: {"files":\[{"file":"<repo-relative path>","new_criticals":N,"findings":\[{"id":"<slug>","sev":"critical","line":N,"category":"<category>","auto_fixable":true|false,"one_line":"<one-line>"}\]}\]}". Issue all challenger spawns in single response (parallel). Write one result file per scanned file to `<RUN_DIR>/fix-pass-<N>/residual-<file-slug>.md`. Collect total `new_criticals` by summing every per-file entry across all results.

**Convergence — inside this sub-agent**: before each fix pass read `<RUN_DIR>/fix-passes.txt` (missing = 0); at 5 stop without dispatch, otherwise increment and persist it, using the new value as N. Run this block before any gate or fix dispatch; on its limit exit, return the terminal summary instead of dispatching:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/audit-state-${CSID}/run-dir" || exit 1
[ -n "$RUN_DIR" ] || exit 1
IFS= read -r PASS_COUNT < "$RUN_DIR/fix-passes.txt" 2>/dev/null || PASS_COUNT=0
[ "$PASS_COUNT" -lt 5 ] || { echo "⚠ CONVERGENCE LIMIT"; exit 0; }
PASS_COUNT=$((PASS_COUNT + 1)); echo "$PASS_COUNT" > "$RUN_DIR/fix-passes.txt"
mkdir -p "$RUN_DIR/fix-pass-$PASS_COUNT"
```

Run gates, Phase 1/2, the Step 9 Codex cross-file check from fix.md (when more than one file changed), R1 and R2. Before judging clean, delegate the applicable fix.md Step 10 cross-file re-validation checks (1, 2, 14a, 14b, 17, 25, 27 under their stated file-change conditions), writing `<RUN_DIR>/crossfile-revalidation-pass<N>.md`. Merge their findings with R1/R2/Codex results and filter through the unchanged selection contract. Stage net-new rows in `<RUN_DIR>/summary.jsonl.fix-pass-<N>.rec`, then run `python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/append_ledger.py" "<RUN_DIR>/summary.jsonl"`; retain the current unresolved set separately so resolved historical ledger rows are not fixed again.

Set `re_audit_clean: true` only when all changed files were re-audited, all fixes held, residual_criticals = 0, all applicable cross-file checks completed, and no eligible fixable findings remain. On `re_audit_clean: false`, repeat the fix/gate/re-audit pass only for unresolved eligible findings, up to **5 total fix passes** including the initial pass. Compute W_n with severity-table.md weights on the unresolved findings under the same selected scope; it must strictly decrease after the first pass, otherwise stop with `⚠ NOT CONVERGING`. At pass 5 stop with `⚠ CONVERGENCE LIMIT`; unavailable verification or only blocked/excluded findings stops with those gaps, never retries them without new authority. Never suppress findings to declare clean. Return to the parent only after clean or a stated stop; the parent proceeds to Step 11 without rerunning Steps 8–10.

Write a completion summary to `<RUN_DIR>/fix-summary.md`:

- findings_total: N
- fixed: N
- blocked: N (gate-rejected; listed in blocked_findings)
- fast_path: N (files that skipped the pre-apply gate under the fast-path rule; each listed with its file path)
- failed: N
- residual_criticals: N (new criticals from post-fix challenger scan; 0 when no CRITICAL findings were fixed)
- re_audit_clean: true|false (true only under the complete convergence predicate above, including cross-file checks and no eligible unresolved findings)
- passes: N; stop_reason: clean|plateau|limit|blocked|verification-gap; unresolved: [{id, file, reason}, ...]
- blocked_findings: [{id, file, reason}, ...] Return ONLY: {"status":"done","file":"\<RUN_DIR>/fix-summary.md","fixed":N,"blocked":N,"failed":N,"residual_criticals":N,"re_audit_clean":true|false,"confidence":0.N}
