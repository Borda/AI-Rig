<!-- file: steps-4-5-7.md — consumers: SKILL.md (Steps 4–5b and Step 7 pointers) -->

## Step 4: System-wide checks

> **Full implementation instructions** are split across 5 scope files in `$AUDIT_TPL/` (resolved in Pre-flight). Read only the file(s) for the active scope at the start of this step — do not read all 5 files unless running a full sweep.
>
> | Scope | File(s) to read |
> | -- | -- |
> | `setup` | `checks-setup.md` (Checks 1–11, 39) + `checks-install.md` (I1–I3) + `checks-security.md` (Check 37) |
> | `plugin` | `checks-setup.md` (Checks 7, 8 only) |
> | `plugins` | `checks-setup.md` (7, 8) + `checks-agents.md` + `checks-skills.md` + `checks-shared.md` (14a, 14b, 15, 17, 12, 13, 25, 26, 29, 41, 45) + checks 32, 32d, 33, 38, 40 + `checks-install.md` (R1–R5 — LOCAL_MODE) + `checks-security.md` (35, 36, 37) |
> | `plugins <name>` | same as `plugins` — scoped to one plugin directory |
> | `agents` | `checks-agents.md` (19, 20) + `checks-shared.md` (run only: 14a, 14b, 15, 17, 12, 13, 25, 26, 29, 41, 45) + `checks-skills.md` (22, 40 only) + `checks-security.md` (35, 36) |
> | `skills` | `checks-skills.md` (22–24, 27, 28, 30, 31, 32, 33, 38, 40) + `checks-shared.md` (run only: 14a, 14b, 15, 17, 12, 13, 25, 26, 29, 41, 45) + `checks-security.md` (35–37) |
> | `rules` | `checks-shared.md` (run only: 18, 12, 13, 29, 41, 45) + `checks-skills.md` (32c only) |
> | `communication` | `checks-shared.md` (run only: 15, 16, 12, 13, 29, 45) |
> | No scope (full) | all 5 files |

**Delegation for full-sweep runs**: for full-sweep (no scope), spawn dedicated `foundry:curator` per scope group, passing template file path and RUN_DIR: agents-checks (reads `checks-agents.md` + relevant `checks-shared.md`), skills-checks (reads `checks-skills.md` + relevant `checks-shared.md`), shared-checks (reads `checks-shared.md`), setup-checks (reads `checks-setup.md` + `checks-install.md`), security-checks (reads `checks-security.md` — security findings land in separate Security Findings section of report). Each writes findings to `<RUN_DIR>/system-checks-<scope>.md`, returns only JSON envelope. Orchestrator does NOT read template files — passes path to spawned agent only.

Run checks below. Native tools first (Glob, Grep, Read); Bash only for pipeline ops native tools can't do.

**Agent roster consistency policy**: evaluate agent system as capability set, not just files. For every overlap in checks 20 or 17, explicit judgment:

- **keep** when both roles own meaningfully different acceptance criteria
- **sharpen** when both roles justified but descriptions/handoffs too fuzzy
- **merge/prune** when roles differ mostly by tone or examples, not decision surface

Don't leave overlap findings as vague "potential duplication." Audit must say which outcome applies and why.

**Context discipline for Step 4**: write all check findings to `$RUN_DIR/system-checks.md` (Write tool after checks complete), not main context. Keep one-line status per check in context:

- `✓ Check N — <one-line result>` (pass)
- `⚠ Check N — N findings` (issues)

**Scope filter**: when `$SCOPE` is set, run only checks listed for that scope; skip all others silently.

> Checks 42, 43 (and 14a/14b/14c/14d/14e, 32d, cli-flag-drift, R3) run in Step 1b on every invocation regardless of scope — never list them here; SKILL.md:198 forbids prose re-derivation.

- `agents` — Checks 14a, 14b, 15, 16, 19, 20, 17, 12, 13, 25, 22, 26, 29, 35, 36, 40, 41 (files: `.claude/agents/*.md` + `plugins/*/agents/*.md`)
- `skills` — Checks 14a, 14b, 15, 16, 17, 12, 23, 22, 13, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 35, 36, 37, 38, 40, 41 (files: `.claude/skills/*/SKILL.md` + `plugins/*/skills/*/SKILL.md`)
- `rules` — Checks 18, 12, 13, 29, 32c, 41 (32d skipped — no plugin bin/ in rules scope)
- `communication` — Checks 15, 16, 12, 13, 29
- `setup` — Checks 1, 2, 3, 4, 5, 9, 10, 11, 7, 6, 8, 30, 37, 39, I1, I2, I3 (Step 3: one foundry:curator spawn for `setup` SKILL.md only; I1–I3 read `~/.claude/`)
- `plugin` — Checks 7, 8 (Step 3: one foundry:curator spawn for `${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/skills/setup/SKILL.md` only)
- `plugins` — Checks 7, 8, 14, 14b, 15, 16, 19, 20, 17, 12, 13, 25, 22, 26, 23, 24, 27, 28, 29, 30, 31, 32, 32d, 33, 35, 36, 37, 38, 39, 40, 41, R1, R2, R3, R4, R5 (files: all `plugins/*/agents/*.md` + `plugins/*/skills/*/SKILL.md`; Step 3: foundry:curator batches for all plugin agents + skills + each plugin's setup SKILL.md; 32d, R1–R5 always LOCAL_MODE — skip in non-local)
- `plugins <name>` or `<plugin-name>` (tier 2) — same check list as `plugins`, scoped to `plugins/<name>/` only
- `<agent-name>` (tier 3) — Checks 14, 14b, 15, 16, 19, 20, 17, 12, 13, 25, 22, 26, 29, 35, 36, 40, 41 (one file only; no cross-plugin Checks 7/8)
- `<skill-name>` (tier 3) — Checks 14, 14b, 15, 16, 17, 12, 23, 22, 13, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33a, 35, 36, 37, 38, 40, 41 (one file only)
- Multiple scope tokens — union of check lists for all resolved scope types; de-duplicate; run each check once against union file set
- No scope argument — run all checks

### Check summary

<!-- loads: checks-index.md -->

<!-- loads: checks-security.md -->

```bash
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
AUDIT_TPL=$(cat "${TMPDIR:-/tmp}/audit-state-${CSID}/audit-tpl" 2>/dev/null || python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/resolve_skill_subdir.py" audit templates $( [ "$LOCAL_MODE" = true ] && echo "--local" ))
cat "$AUDIT_TPL/checks-index.md" "$AUDIT_TPL/checks-security.md"
```

Full check index (Checks 1–41, I1–I3, R1–R5 with severity, scope, notes) and security checks (35–37) loaded above.

### Claude Code docs freshness (within Step 4)

> Replace `<RUN_DIR>` with the actual run-directory path (sentinel `${TMPDIR:-/tmp}/audit-state-${CSID}/run-dir`) before dispatching — the spawned agent gets a fresh shell and cannot expand orchestrator variables.

```text
Agent(subagent_type="foundry:web-explorer", prompt="Fetch current Claude Code docs (https://code.claude.com/docs/en/). If that URL returns 404 or redirects, navigate from https://code.claude.com homepage to find the documentation section. If docs are entirely unavailable, return {\"status\":\"unavailable\",\"findings\":0}. Check: hook event names + type field vs documented schema (deprecated decision:/reason: fields); agent frontmatter fields + model values; skill frontmatter fields; new features passing genuine-value filter → Upgrade Proposals table (max 5, classify config or capability). Write full findings to <RUN_DIR>/docs-freshness.md using the Write tool. End your full findings file with a `## Confidence` block per quality-gates.md format (Score, Gaps, Refinements). Return ONLY: {\"status\":\"done\",\"file\":\"<RUN_DIR>/docs-freshness.md\",\"findings\":N,\"deprecated\":N,\"new_features\":N,\"confidence\":0.N,\"summary\":\"N findings, N deprecated, N new features\"}")
```

<!-- URLs fetched live by web-explorer at runtime; graceful degradation: if any 404, instruct navigation from code.claude.com homepage. -->

Severity: deprecated/invalid = **high**; deprecated frontmatter field = **medium**; new feature not used = **Upgrade Proposals** (not LOW).

After checks complete: collect `⚠` lines, write full details to `$RUN_DIR/system-checks.md`, include only summary table in context.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _RUN_DIR < "${TMPDIR:-/tmp}/audit-state-${CSID}/run-dir" 2>/dev/null || _RUN_DIR=""
IFS= read -r _KEEP < "${TMPDIR:-/tmp}/audit-state-${CSID}/keep-items" 2>/dev/null || _KEEP=""
_PRESERVE="run-dir=$_RUN_DIR, static-findings=${TMPDIR:-/tmp}/audit-state-${CSID}/static-findings.jsonl, finding-files=$_RUN_DIR/*.md"
[ -n "$_KEEP" ] && _PRESERVE="$_PRESERVE; user-keep: $_KEEP"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/write_skill_contract.py" "foundry:audit" "aggregate (after parallel curator+system-checks fan-out)" "$_RUN_DIR" "$_PRESERVE" "consolidate findings → aggregate.md + summary.jsonl → Step 7 report"  # timeout: 5000
```

## Step 5: Aggregate and classify findings

**Delegate aggregation** to consolidator agent to avoid flooding main context. Resolve the two absolute paths the consolidator must read — it runs in a fresh shell and cannot expand orchestrator variables, so these are interpolated as literal strings into the prompt below:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
AUDIT_TPL=$(cat "${TMPDIR:-/tmp}/audit-state-${CSID}/audit-tpl" 2>/dev/null || python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/resolve_skill_subdir.py" audit templates $( [ "$LOCAL_MODE" = true ] && echo "--local" ))
IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/audit-state-${CSID}/run-dir" 2>/dev/null || RUN_DIR=""
printf "RUN_DIR=%s\nSTATIC_FINDINGS_PATH=%s\nSEVERITY_TABLE_PATH=%s\n" "$RUN_DIR" "${TMPDIR:-/tmp}/audit-state-${CSID}/static-findings.jsonl" "$(dirname "$AUDIT_TPL")/severity-table.md"
```

Replace `<RUN_DIR>`, `<STATIC_FINDINGS_PATH>`, and `<SEVERITY_TABLE_PATH>` in the prompt below with the literal values printed above. Spawn **foundry:curator** consolidator:

> "Read all finding files in `<RUN_DIR>/` (\*.md files from Steps 3–4, including `docs-freshness.md` if present) AND the deterministic Layer-1 results at `<STATIC_FINDINGS_PATH>` (Step 1b — one JSON object per check; each `\"status\":\"fail\"` object's `lines` array is a set of already-verified mechanical findings, severity per the check's known level: fence/mode-dispatch=high, tag/README-drift/bash-persistence/spawn-vars/shared-drift=medium, orphaned-bin/routing=medium). Run `cat "<SEVERITY_TABLE_PATH>"` via the Bash tool and apply its severity classification. Antipatterns that indicate severity under-classification are also in that file. Group all findings by severity (security, critical, high, medium, low). Apply the one-finding-per-issue rule: when a single location has multiple distinct problems at different severities, emit one finding entry per problem. Write the aggregated severity table to `<RUN_DIR>/aggregate.md` using the Write tool. End your aggregate.md file with a `## Confidence` block per quality-gates.md format (Score, Gaps, Refinements). Also write `<RUN_DIR>/summary.jsonl` — one compact JSON object per line, one line per finding: `{"file":"<basename>","sev":"security|critical|high|medium|low","id":"H1","line":"<line number or null>","category":"<category>","one_line":"<finding description>"}`. This file is what the orchestrator will read; aggregate.md is for human review only. Return ONLY a compact JSON envelope on your final line — nothing else after it: `{\"status\":\"done\",\"file\":\"<RUN_DIR>/aggregate.md\",\"findings\":N,\"severity\":{\"security\":N,\"critical\":N,\"high\":N,\"medium\":N,\"low\":N},\"confidence\":0.N,\"summary\":\"N findings total: S security, C critical, H high, M medium, L low\"}`"

Main context receives only that one-liner. Orchestrator MUST NOT read `aggregate.md` in full — 200–600 lines, overflows context on large audits. Use `$RUN_DIR/summary.jsonl` for all dispatch decisions in Steps 7 and 8.

## Step 5b: Low-confidence remediation

Parse confidence scores from each file's `## Confidence` block in `<RUN_DIR>/<slug>.md` output files (use Glob + Read — batch envelopes carry aggregate confidence, not per-file scores; individual file reports are the authoritative source). For each slug where `Score` < **0.80**, run three parallel passes:

**Fan-out ceiling**: when MORE THAN 8 slugs score \<0.80, do NOT run per-slug passes (4 spawns × N is unbounded) — instead spawn ONE consolidated **foundry:curator** re-run covering all low-confidence slugs (batched prompt listing every slug + its `Gaps:` block, one combined `<RUN_DIR>/lowconf-batch-rerun.md`) plus one Codex pass over same batch when available; run pass B (docs check) only for slugs whose gaps explicitly cite schema/docs uncertainty. Systematically low confidence signals a rubric or curator problem, not N independent file problems — remediate once, not N×4 times.

**Health monitoring** (CLAUDE.md §6): apply the honest protocol in `$_FS/agent-spawn-protocol.md` — passes A–C return on completion; read each pass's output file afterwards. On empty/missing output: mark `timed_out`, surface with ⏱ in final report.

**A — Double-reasoning pass** (curator re-run with gaps called out):

Spawn **foundry:curator** with the prior report and its `Gaps:` block:

> "Re-audit `<original-source-file>` targeting these specific gaps from the prior pass: `<Gaps block content>`. Address each gap explicitly — do not repeat prior findings verbatim; focus on what was uncertain. Write updated findings to `<RUN_DIR>/<slug>-rerun.md`. End your full findings file with a `## Confidence` block per quality-gates.md format (Score, Gaps, Refinements). Return ONLY: `{\"status\":\"done\",\"file\":\"<path>\",\"findings\":N,\"confidence\":0.N,\"summary\":\"...\"}`"

**B — Docs consultation** (verify findings against current Claude Code schema):

Spawn **foundry:web-explorer**:

> "Fetch current Claude Code docs for `[agent|skill|hook]` schema — navigate from `https://code.claude.com/docs/en/` to the `[sub-agents|skills|hooks]` page. Verify that findings about frontmatter fields or documented behavior in `<RUN_DIR>/<slug>-rerun.md` are accurate against current docs. List any corrections. Write to `<RUN_DIR>/docs-recheck-<slug>.md`. End your full findings file with a `## Confidence` block per quality-gates.md format (Score, Gaps, Refinements). Return ONLY: `{\"status\":\"done\",\"file\":\"<path>\",\"corrections\":N,\"confidence\":0.N}`"

**C — Codex adversarial pass** (requires `bridge@borda-ai-rig`):

```bash
CODEX_STATUS=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/check_bridge.py" --status 2>/dev/null || echo "absent")
[ "$CODEX_STATUS" = "available" ] && CODEX_AVAILABLE=true || CODEX_AVAILABLE=false  # timeout: 5000
```

If `CODEX_AVAILABLE=true`, resolve every placeholder in the following review brief, then pass that entire rendered brief to `Skill(skill="bridge:review", args="...")`:

> "Adversarial review of low-confidence findings for `<original-source-file>`. Prior foundry:curator pass scored confidence=<N> — gaps: `<Gaps>`. Challenge each finding: real? severity correct? missed issues? Read source file + prior report `<RUN_DIR>/<slug>-rerun.md`. Write to `<RUN_DIR>/codex-recheck-<slug>.md`. End your full findings file with a `## Confidence` block per quality-gates.md format (Score, Gaps, Refinements). Return ONLY: `{\"status\":\"done\",\"file\":\"<path>\",\"findings\":N,\"confidence\":0.N}`"

If `CODEX_AVAILABLE=false`: log `[Step 5b] bridge@borda-ai-rig is ${CODEX_STATUS} — adversarial pass skipped.` Include note in final report `## Confidence` section.

**After all three passes complete** for a slug: spawn **foundry:curator** mini-consolidator to merge `<slug>-rerun.md`, `docs-recheck-<slug>.md`, `codex-recheck-<slug>.md` → append `### Low-Confidence Remediation — <slug>` section to `aggregate.md` with reconciled findings (promoted corrections, confirmed findings, refuted findings). Update `summary.jsonl` with any net-new findings.

**Skip Step 5b entirely** when no Step 3 files scored below 0.80.

Returns to SKILL.md Step 6 (cross-validate critical findings) after Step 5b completes or is skipped.

## Step 7: Report findings

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _RUN_DIR < "${TMPDIR:-/tmp}/audit-state-${CSID}/run-dir" 2>/dev/null || _RUN_DIR=""
IFS= read -r _KEEP < "${TMPDIR:-/tmp}/audit-state-${CSID}/keep-items" 2>/dev/null || _KEEP=""
_PRESERVE="run-dir=$_RUN_DIR, aggregate=$_RUN_DIR/aggregate.md, summary=$_RUN_DIR/summary.jsonl"
[ -n "$_KEEP" ] && _PRESERVE="$_PRESERVE; user-keep: $_KEEP"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/write_skill_contract.py" "foundry:audit" "report (after aggregate complete)" "$_RUN_DIR" "$_PRESERVE" "emit report → follow-up gate → optional fix mode (Steps 8-10) → Step 11"  # timeout: 5000
```

Before emitting, read current `$RUN_DIR/summary.jsonl` (may have been updated by Step 5b with net-new promoted findings) and recompute severity totals. Then emit report (omit Upgrade Proposals if none passed genuine-value filter):

```markdown
## Audit Report

### Findings by Severity
#### Security (N) | Critical (N) | High (N) | Medium (N) | Low (N)
| File | Line | Issue | Category |
|---|---|---|---|
| agents/foo.md | 42 | References `bar-agent` which does not exist on disk | broken cross-ref |

### Summary
- Total: N (S security, C critical, H high, M medium, L low)
- Fix via follow-up gate: (a) Fix auto-fixable (Recommended) · (b) Fix SECURITY + CRITICAL + HIGH · (c) fix ALL incl. systemic

### Upgrade Proposals (N — pick `/audit --upgrade` from gate to apply)
| # | Feature | Type | Rationale |
|---|---------|------|-----------|
```

After report → fire **Follow-up gate**. If user picks fix option (a–c), proceed inline to fix mode (Steps 8–10, loaded from `modes/fix.md`). Otherwise skip to Step 11.

Returns to SKILL.md Steps 8–10 (fix dispatch, gated) / Step 11 (final report).
