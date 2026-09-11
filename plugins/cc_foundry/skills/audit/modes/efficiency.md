# Efficiency Mode — foundry:audit

<!-- file: efficiency.md — consumers: audit/SKILL.md -->

Triggered by `/audit --efficiency`. Read+executed by `/audit` when `--efficiency` flag present.

## Mode: efficiency

**Trigger**: `/audit [<scope>...] --efficiency`

Sweeps agents and skills for cost inefficiency signals. Does NOT run standard per-file quality audit (Steps 3–6) — efficiency-only analysis. Generates prioritized cost-reduction plan with estimated savings. Note: mode produces heuristic estimates only — no live token-cost baseline measured, no post-fix delta computed. Savings figures are directional guidance.

**Scope resolution**: same as standard audit. No scope = all agents + skills across plugins + `.claude/`. Named scope = union of resolved file sets. Fragment files (`*/modes/*`, `*/templates/*`, `*/_shared/*`): skip checks 1–3 and 6 (no model frontmatter); run checks 5 (token bloat) and 7 (bin/ extraction) only.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/audit-state-${CSID}/run-dir" 2>/dev/null || RUN_DIR=""
if [ -z "$RUN_DIR" ] || [ ! -d "$RUN_DIR" ]; then
  RUN_DIR=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/make_run_dir.py" .reports/audit)  # timeout: 5000
  [ -z "$RUN_DIR" ] && { printf "! BREAKING: make_run_dir.py returned empty path\n"; exit 1; }
  mkdir -p "${TMPDIR:-/tmp}/audit-state-${CSID}"
  echo "$RUN_DIR" > "${TMPDIR:-/tmp}/audit-state-${CSID}/run-dir"
fi
echo "Run dir: $RUN_DIR"
```

This RUN_DIR replaces Step 3 setup (skipped in efficiency mode) — unless a prior mode in the same combined invocation (e.g. `--adversarial --efficiency`) already minted one; then it is reused, never clobbered.

**Phase A — Per-file efficiency audit** (parallel foundry:curator spawns, same batching rule as Step 3 (`EFFECTIVE_BATCH`) — efficiency skips Step 3, so recompute the value here):

Spawn **foundry:curator** per file with efficiency-specific prompt:

> Audit `<file>` for cost and efficiency signals only. Do NOT run general quality checks. Check:
>
> 1. **Model tier**: is `model:` declared? If `model: opus` or `model: opusplan`, does task genuinely require reasoning depth — adversarial multi-step analysis, architectural design, complex implementation? Flag if task is primarily: template fill, pattern matching, structured summarization, orchestrator-only dispatch, or single-pass structured write. **Performance-safety sub-check (mandatory before any downgrade recommendation)**: does the agent produce quality-sensitive output? Quality-sensitive signals: public-facing text (contributor replies, blog posts), security analysis (OWASP, exploit reasoning), adversarial reasoning, complex multi-file code design, creative original content. If any signal present, add `performance_risk: medium|high` to the finding and require empirical validation note — do NOT recommend downgrade as P1/P2 without this caveat. A lower-cost model that degrades output quality is not an efficiency gain.
> 2. **Effort level**: is `effort: xhigh` declared? Flag if agent is read-only, single-pass, or executes a fixed decision tree — xhigh planning budget has nowhere to spend. Exception: `xhigh` on sonnet is acceptable only for these agent roles: adversarial reviewer (challenger, qa-specialist), multi-file code designer (sw-engineer, solution-architect), or public-facing content writer (creator, shepherd). All other roles: flag regardless of quality-sensitivity claim.
> 3. **Missing model declaration**: no `model:` → session model inherited; flag with recommended tier (opus/sonnet/haiku based on role complexity). Applies to skill and agent files alike. `disable-model-invocation: true` is **not** an exemption: it blocks only Claude-automatic invocation, subagent preloading, and scheduled dispatch — the skill is still user-invocable via `/skill-name`, still runs a model, and so still inherits the session model when it declares none.
>
> <!-- Item 4 (dead model spec) removed: `disable-model-invocation: true` blocks only Claude-automatic invocation, subagent preloading, and scheduled dispatch — user-typed `/skill-name` still runs and `model:` still governs it. Numbering gap at 4 is intentional; items 5–9 keep their numbers because E8/E9 are named identifiers referenced across this file and the envelope schema. -->
>
> 5. **Token bloat**: identify inline reference blocks >40 lines that load unconditionally but apply only to a subset of invocations (e.g., ML-specific patterns for non-ML projects, hook authoring for non-hook tasks, domain CI blocks always loaded). Flag with estimated line count and suggested gate/extraction. A block "applies only to a subset" if its heading keyword does not appear in the skill's `description:` field or is scoped by a conditional the skill rarely enters. Curator must verify subset-applicability before flagging — do not flag always-on decision tables or check indexes.
> 6. **Tool grant scope**: for agent files: does `tools:` include `*` or tools not used in the agent's workflow prose? For skill files: does `allowed-tools:` include `*` or tools not used in the skill workflow body? Note: skills with `disable-model-invocation: true` have no model execution and tool grants are irrelevant — skip.
> 7. **Bin/ extraction candidates**: scan fenced code blocks of any language (bash, python, sh, perl, etc.) for self-contained patterns appearing 3+ times in this file with only constant differences (variable names, path segments, string literals). Self-contained means: block produces output to stdout, has no shell function definitions, reads no caller shell state beyond `$HOME`, `$ARGUMENTS`, `$RUN_DIR`, `$AUDIT_TPL`. Flag each candidate: block language, purpose, occurrence count, suggested `bin/<script-name>.sh` or `bin/<script-name>.py`. Skip: blocks defining bash functions, blocks mutating shell state used in later blocks. 7b. **Prose compression candidates**: scan inline fenced code blocks and bin/ call-site descriptions. Flag when: (a) inline block — `tokens(block) > tokens(equivalent prose/table/schema)` at identical precision; (b) bin/ call-site — `tokens(call-site description) >= tokens(prose equivalent)`. Exempt: examples, templates, blocks carrying exact executable syntax. Severity: low. Suggested fix: replace with prose/table/schema (Case 1) or delete script and replace call-site with prose (Case 2). Reference: `bin-authoring-guide.md §Prose over Code (Token Compression)`.
>
> <!-- GUARD-RAILS: never flag as E8/E9 — these look verbose but are load-bearing -->
>
> <!-- (a) Structural protocols: convergence limits, iteration caps, loop bounds, health monitoring constants (MONITOR_INTERVAL, HARD_CUTOFF), security gates (adversarial pre-apply validation, AskUserQuestion before destructive ops), NON_AUTO_FIXABLE bypass lists, confidence block requirements, task hygiene protocols, JSON envelope contracts, file-based handoff requirements, batch-size guards, Fix Action Hierarchy multi-step rules, post-fix verification protocols, reversibility checks, <antipatterns-to-flag> curator rules. -->
>
> <!-- (b) High-stakes path reinforcement: 3+ restatements on irreversible operations, destructive edits, security boundaries are expected and exempt — any section whose heading or surrounding context names git push, settings.json mutation, external messages, force operations, or dropping data. -->
>
> <!-- (c) Domain-specific constraints: any instruction not inferable from agent/skill role alone — escalation protocols, output format specs (field names, file naming), cross-plugin coordination rules, permission model constraints. -->
>
> 8. **Instruction complexity (E8)**: are instructions disproportionately complex relative to the operation they govern? Flag when: (a) same behavioral constraint restated 3+ times with different wording in same logical section — exempt: high-stakes path reinforcement (guard-rail (b) above); (b) conditional logic nested >3 levels with no meaningful behavioral difference between branches; (c) >100-word preamble preceding single atomic action with no new constraints added; (d) step containing ≥5 sub-bullets where ≥3 restate same rule — exempt: Fix Action Hierarchy and structural-protocol lists (guard-rail (a) above). Never flag guard-rail taxonomy items above. Severity: medium. Fix: consolidate redundant restatements to one canonical statement; flatten trivially equivalent branches. For each E8 finding in the report file include columns: `check` (E8), `location` (line range), `issue` (one-line), `impact` (estimated lines reduced), `suggested simplification`.
> 9. **Behavioral noise (E9)**: do instructions describe behavior any capable frontier model performs by default, or contradict each other? Flag when: (a) instruction verbatim duplicates documented default Claude Code behavior already enforced by `CLAUDE.md` or `settings.json` — verifiable by grep, not opinion; (b) abstract directive with no observable behavioral constraint ("be thorough", "ensure quality") — identical behavior whether present or absent; (c) two instructions in same file that cannot both be satisfied — contradiction is noise; (d) "IMPORTANT: do X" where X is explicitly stated in an adjacent Bash block or step header in the same file — same-file structural evidence required. Omit (a) and (d) when no file-internal or CLAUDE.md/settings.json evidence exists — do not flag on inference. Never flag guard-rail taxonomy items above, domain-specific constraints, or format contracts. Severity: low. Fix: remove or replace with concrete behavioral constraint. For each E9 finding in the report file include columns: `check` (E9), `location` (line range), `issue` (one-line), `impact` (estimated lines reduced), `suggested simplification`. Write findings to `<RUN_DIR>/efficiency-<file-slug>.md` where `<file-slug>` = `<plugin>-<skill-dir-name>` for skills (e.g. `foundry-audit`, `oss-review`) or `<plugin>-<agent-name>` for agents (e.g. `foundry-curator`). For `.claude/` files prefix with `local` (e.g. `local-audit`). Never use bare `efficiency-SKILL.md` — all skills share that basename.
>
> Return ONLY: `{"status":"done","file":"<path>","issues":N,"severity":{"critical":N,"high":N,"medium":N,"low":N},"top_issue":"<one-line>","cheapest_viable_model":"<model or unchanged>","e8_complexity":N,"e9_noise":N,"confidence":0.N}`

**Phase B — System-wide spawn pattern + duplication scan** (parallel with Phase A):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
[ "$LOCAL_MODE" = "true" ] && _SCAN_DIR="plugins/" || _SCAN_DIR=".claude/"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/scan_efficiency_signals.py" --scan-dir "$_SCAN_DIR"  # timeout: 60000
```

**Phase B2 — Code block purpose-grouping + extraction feasibility (Check 33, parallel with Phase A+B)**:

Scope: per plugin — compare blocks within same plugin only (cross-plugin overlap also captured here). When `--efficiency` active, **skip Check 17** — Phase B2 subsumes it: DUPLICATE clusters (max-sim ≥ 0.90) are the Check 17 findings at higher resolution.

**Primary signal: functional purpose, not syntactic similarity.** Syntactic line-intersection is blind to conditional-inversion and variable renaming — two blocks implementing same logic written differently have low syntactic overlap but identical purpose. Group by purpose first; use syntactic overlap only as secondary confirmation and DUPLICATE label.

<!-- policy-sibling: plugins/cc_foundry/skills/audit/templates/checks-skills.md (Check 33b Phase 2 — same Gate/Score spec) -->

Spawn **foundry:curator** per plugin with this prompt:

> Enumerate every fenced code block (```` ```bash ````, ```` ```python ````, ```` ```sh ````, etc.) in all `.md` files under `plugins/<name>/` — including `modes/`, `templates/`, and `_shared/` subdirs. Assign each a block ID: `<plugin-abbrev>-<skill-slug>-B<n>` (e.g. `fnd-audit-B3`, `fnd-audit-modes-efficiency-B2`). Record: ID, source file, start line, language, line count, total lines across cluster.
>
> **Step 1 — Purpose statements**: for each block, write a one-sentence purpose statement describing what the block does functionally (not how) — e.g., "resolves `_shared/` path from plugin cache", "detects codex plugin availability", "sets LOCAL_MODE-aware glob vars", "emits boilerplate-duplication counts". Same wording of different goal = different cluster. Different wording of same goal = same cluster.
>
> **Step 2 — Purpose clusters**: group blocks with equivalent purpose into clusters. This is the primary grouping. Singletons omitted. Assign cluster ID `C<n>`.
>
> **Step 2a — Computational equivalence gate (mandatory before finalizing any cluster)**: purpose-statement wording similarity is necessary but not sufficient — two blocks that *sound* alike can compute different things. Before finalizing a cluster (and before proposing any "site B bypasses/duplicates site A" relationship), verify: (a) same output destination *namespace* — `.reports/audit/*` and `.reports/research/*` are different namespaces even when both blocks "write a report file"; (b) same input parameters/env vars consumed; (c) same side-effect semantics (write vs read vs delete). If members target structurally different destinations or inputs despite similar wording, split into separate clusters — do not merge. Any auto-fix proposal that would rename/redirect one site's output to match another's requires this equivalence evidence stated in the cluster row; omit the auto-fix suggestion (report the cluster only) when evidence is inconclusive.
>
> **Step 3 — Syntactic similarity (secondary)**: for each cluster, normalize each member block: strip `#` comment lines → collapse whitespace → replace path segments / slugs / numeric literals with `<STR>` → **replace ALL concrete argument/parameter values** (flag values after `--flag`, option strings, RHS of variable assignments `FOO="val"`) with `<ARG>`; keep structural tokens. Compute `sim(A,B) = 2 × |lines(A_norm) ∩ lines(B_norm)| / (|A| + |B|)`. Record max-sim within cluster. Mark cluster **DUPLICATE** if max-sim ≥ 0.90 (blocks are near-identical, not just same-purpose).
>
> **Table 1 — Purpose clusters**:
>
> ```
> | Cluster | Block IDs | Files | Lang | Lines each | Total lines | Est. tokens/call | Purpose | Max-sim | Duplicate? |
> ```
>
> (Total lines = sum of Lines each across all instances; Est. tokens/call = (lines_per_instance − 1) × ~4 — tokens saved per calling-skill invocation when block extracted to bin/)
>
> **Table 2 — Extraction scoring**: for each cluster, apply gate then score:
>
> - **ParamSlots**: count of distinct `<ARG>` placeholder slots after normalization = how many CLI parameters the extracted script would need.
> - **Tokens**: estimated token count of one block instance.
> - **Never-extract list — checked first, before the gate.** A cluster matching any of these is not a candidate at all; record it in Table 1 with the reason and omit it from Table 2:
>   - Replacement costs more than the block. A one-command block (`rm -f .temp/state/skill-contract.md`) becomes a longer invocation line — extraction is a net token loss.
>   - The block is marked `# audit-skip` on its first line, or prose beside it declares the duplication intentional.
>   - The block's only purpose is echoing values for the model to read, with no computation behind them.
>   - One-off glue under ~10 lines that appears in a single file.
>   - The body carries model-substituted placeholders (`<changed_files>`, `<TARGET>`) rather than shell variables — it is a template, not executable code.
>   - The block is an `Agent(...)` prompt, an output template, or a prompt string assigned to a shell variable.
>   - The block is a propagated copy owned by another plugin's MANIFEST entry — extract at the canonical, never at a copy.
>   - The shape is load-bearing for a permission hook: `IFS= read -r VAR < "${TMPDIR:-/tmp}/…-${CSID}"` sentinel reads pass `sentinel-read-allow.js` by shape, and a `$(python …)` replacement reintroduces the prompt they exist to avoid.
> - **Gate** = `G1:P/F · G2:P/F · G3:P/F` — all must pass or Verdict = HOLD:
>   - G1 (Size OR execution cost): block > 100 tokens (payload cost), **OR** block launches an external interpreter process (subprocess/heredoc/`-c` invocation of python/node/perl/ruby/etc) **and** occurs ≥3× in cluster (execution cost — N forked interpreters is real overhead even when each instance is token-small; a tiny `python -c "..."` one-liner repeated 84× must not gate out on size alone)
>   - G2 (Independence): no branch on prior LLM decision that cannot become explicit arg
>   - G3 (Identity): has computational meaning outside orchestration prose (high env-var coupling = G3 fail)
> - **Score** = sum of applicable positive-dimension weights when gate passes:
>   - Testable (deterministic I/O, writable pytest/shellcheck test) +2
>   - Reuse (same logic in 2+ .md files) +2
>   - Token drain (block > 300 tokens) +2
>   - Process overhead (external interpreter launched ≥3× in cluster — extraction collapses N process forks into 1 script invocation) +2
>   - Lintable (shellcheck/ruff directly applicable) +1
>   - Run frequency (executes >1× per skill invocation) +1
>   - Standalone debuggable (runnable with no SKILL.md context) +1
> - **Verdict**: HOLD (any gate fail) · LOW (0–1) · MEDIUM (2–3) · HIGH (≥4)
>
> ```
> | Cluster | ParamSlots | Tokens | Gate | Score | Verdict | Differs-by | Recommended extraction |
> ```
>
> **Differs-by**: list the concrete `<ARG>` slot values that vary across cluster instances — these become named CLI parameters in the extracted script signature. Recommendation = concrete: e.g. `Extract → bin/find-plugin.sh <plugin-name>; N call sites become $(find-plugin.sh codex)`.
>
> **Severity**: DUPLICATE cluster (max-sim ≥ 0.90) → **high** regardless of gate/score; HIGH verdict → **medium**; MEDIUM verdict → **low**; LOW or HOLD → Table 2 only (not a finding). Python blocks with HIGH verdict → medium + note approval-prompt impact.
>
> Write to `<RUN_DIR>/efficiency-check33-<plugin>.md`. Return ONLY: `{"status":"done","file":"<path>","clusters":N,"findings":N,"severity":{"high":N,"medium":N,"low":N},"confidence":0.N}`

**Phase C — Aggregate, score, and plan** (after A+B+B2 complete):

Spawn **foundry:curator** consolidator to merge all findings:

> Read all per-file reports from `<RUN_DIR>/efficiency-*.md` and all Check 33 reports from `<RUN_DIR>/efficiency-check33-*.md`. Also read Phase B bash output passed as context. Deduplicate findings by `(file, finding_type)` pair — Phase A curator and Phase B bash both scan for the missing-model condition; prefer Phase A curator finding (has file context) over Phase B bash line (no context) when both report same file+condition.
>
> Produce a cost-reduction report with these sections:
>
> 1. **Cheapest Viable Model table** — one row per agent/skill with cost issue: `| file | current model+effort | minimum viable | rationale | estimated saving |`; saving = opus→sonnet: LARGE, opusplan→sonnet: LARGE, xhigh→high: MEDIUM, xhigh→medium: MEDIUM (heuristic tiers — not measured against live run costs)
> 2. **Unbounded Spawn Patterns** — list files with uncapped per-item agent dispatch; recommended cap and batch strategy
> 3. **Token Bloat Hotspots** — top 5 files by redundant inline content; section name, line count, suggested action
> 4. **Boilerplate Duplication + Bin/ Extraction Candidates** — pattern name × occurrence count × total redundant lines × extraction target; for each bin/ candidate from Phase A+B: block purpose, occurrence count, suggested `bin/<script-name>.sh`, estimated line reduction. Merge with Check 33 (Phase B2) similarity clusters: include Table 1 and Table 2 per plugin inline in this section, sorted by feasibility HIGH→LOW
> 5. **Missing Model Declarations** — list files inheriting session model; recommended tier per file
> 6. **Prioritized Improvement Plan** — P1 (critical: correctness/highest cost), P2 (high: model downgrades), P3 (medium: hygiene/dedup), P4 (low: compression); each item: file + exact change + estimated saving + `performance_risk: low|medium|high`. Items with `performance_risk: high` are automatically downgraded to P-HOLD (do not apply without empirical benchmarking). Items with `performance_risk: medium` stay in plan but carry a `⚠ validate first` marker.
> 7. **Estimated Combined Savings** — rough directional reduction for most common workflows if all P1+P2 applied; note: savings are heuristic estimates only — no live cost measurement performed; treat as directional guidance, not engineering targets
> 8. **Instruction Quality Issues** — table of E8/E9 findings aggregated from per-file envelopes (`e8_complexity` + `e9_noise` fields) and detailed findings in `efficiency-*.md` reports: `| file | check | location | issue | impact | suggested simplification |`; sorted by estimated noise reduction (high→low). NON_AUTO_FIXABLE — E8 findings contribute to `medium` count; E9 findings contribute to `low` count; both enter the follow-up gate under option (c) "Fix ALL" as a separate "instruction-quality" AskUserQuestion category. Do not auto-fix instruction-quality findings via options (a) or (b). Omit section entirely when both E8 and E9 totals are zero. Write full report to `<RUN_DIR>/efficiency-report.md`. End report with `## Confidence` block per quality-gates.md format (Score, Gaps, Refinements).
>
> Return ONLY: `{"status":"done","file":"<RUN_DIR>/efficiency-report.md","critical":N,"high":N,"medium":N,"low":N,"total_issues":N,"clusters":N,"extract_count":N,"recommended_count":N,"e8_complexity":N,"e9_noise":N,"top_saving":"<description>","confidence":0.N}`

**Report format** (terminal summary):

```
verdict: EFFICIENCY_ISSUES · critical: N · high: N · medium: N · low: N
code-blocks: clusters: N · HIGH: N · MEDIUM: N
instruction-quality: complexity: N · noise: N
confidence: 0.N
→ <RUN_DIR>/efficiency-report.md

Critical: [list each]
Estimated savings (P1+P2): ~X%
```

Omit `code-blocks:` line when no clusters found (all HOLD verdicts or no Check 33 data available). Omit `instruction-quality:` line when both complexity and noise are zero.

Efficiency findings feed into standard fix pipeline (Steps 7–10). **Step 8 override**: model-tier mismatch findings NOT subject to Step 8's "report-only" bypass — user opted into auto-fix by invoking `--efficiency`. Fix agents will apply model-tier changes.

**Extraction routing**: when Phase C envelope `extract_count > 0` (HIGH or MEDIUM verdict clusters), follow-up gate replaces option (d) with a `/distill executables` choice — user selects from gate; do NOT auto-run. Gate substitution logic is in main SKILL.md follow-up gate section.

**Post-extraction orphan check**: after `/distill executables` completes, run `python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/check_orphaned_bin.py"` — must exit 0. New orphan introduced (bin/ script created without consumer rewire) = HIGH finding; abort extraction phase, require wire-in before commit.

**Flag aliases**: `--efficiency` only (no alias).
