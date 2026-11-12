<!-- file: steps-4-5-7.md — consumers: SKILL.md (Steps 4–5b and Step 7 pointers) -->

## Step 4: System-wide checks

> **Full implementation instructions** are split across scope files in `$AUDIT_TPL/` (resolved in Pre-flight). Load only the file(s) for the active scope at the start of this step through `load_mode.py audit templates` after the foundry guarded resolver prologue; pass each selected literal basename and the persisted local-mode flag, never use raw cat or the Read tool — do not read every file unless running a full sweep. `checks-skills.md` is a hub: its checks live in the skill-check groups listed below the scope table — read the hub plus only the groups the scope dispatches.
>
> | Scope | File(s) to read |
> | -- | -- |
> | `setup` | `checks-setup.md` (Checks 1–11, 39) + `checks-install.md` (I1–I3) + `checks-security.md` (Check 37) |
> | `plugin` | `checks-setup.md` (Checks 7, 8 only) |
> | `plugins` | `checks-setup.md` (7, 8) + `checks-agents.md` + `checks-skills.md` + every skill-check group + `checks-shared.md` (14a, 14b, 15, 17, 12, 13, 25, 26, 29, 41, 45) + checks 32, 32d, 33, 38, 40 + `checks-install.md` (R1–R5 — LOCAL_MODE) + `checks-security.md` (35, 36, 37) |
> | `plugins <name>` | same as `plugins` — scoped to one plugin directory |
> | `agents` | `checks-agents.md` (19, 20, 46) + `checks-shared.md` (run only: 14a, 14b, 15, 17, 12, 13, 25, 26, 29, 41, 45) + `checks-skills.md` + `checks-skills-gaps.md` (22, 40 only) + `checks-security.md` (35, 36) |
> | `skills` | `checks-skills.md` + groups gaps, bash, contract, crossplugin, deadfiles, duplication (22–24, 27, 28, 30, 31, 32, 33, 38, 40) + `checks-shared.md` (run only: 14a, 14b, 15, 17, 12, 13, 25, 26, 29, 41, 45) + `checks-security.md` (35–37) |
> | `rules` | `checks-shared.md` (run only: 18, 12, 13, 29, 41, 45) + `checks-skills.md` + `checks-skills-deadfiles.md` (32c only) |
> | `communication` | `checks-shared.md` (run only: 15, 16, 12, 13, 29, 45) |
> | No scope (full) | every file above, incl. every skill-check group |
>
> **Skill-check groups** (read with hub `checks-skills.md`; check IDs per group: its §Check groups): `checks-skills-gaps.md` · `checks-skills-bash.md` · `checks-skills-contract.md` · `checks-skills-crossplugin.md` · `checks-skills-deadfiles.md` (`rules` runs 32c only) · `checks-skills-duplication.md` · `checks-skills-sweep.md` (loaded by `plugins` and full sweeps only; no scope list dispatches its checks — 42 and 43 are driver-run in Step 1b).

**Delegation for full-sweep runs**: for full-sweep (no scope), spawn dedicated `foundry:curator` per scope group, passing template file path and RUN_DIR: agents-checks (reads `checks-agents.md` + relevant `checks-shared.md`), skills-checks (reads `checks-skills.md` + every skill-check group + relevant `checks-shared.md`), shared-checks (reads `checks-shared.md`), setup-checks (reads `checks-setup.md` + `checks-install.md`), security-checks (reads `checks-security.md` — security findings land in separate Security Findings section of report). Each spawn prompt must explicitly instruct the curator to run `PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name foundry) || exit $?; export PLUGIN_ROOT`, then load each selected template through `load_mode.py audit templates` using its literal basename and the passed local-mode flag; do not pass a raw Read/cat instruction. Each writes findings to `<RUN_DIR>/system-checks-<scope>.md`, returns only JSON envelope. Orchestrator does NOT read template files — passes path to spawned agent only.

**Deadlines for every spawn in Steps 4–5b**: in the spawn response Write `<RUN_DIR>/agent-watch-<step>-<wave>.tsv`, one `<unique-agent-name>\t<output-file-or-dash>\t<seconds>` row per Agent call. Step 4 group curators and Step 5 consolidator: `900`; docs-freshness: `300`; Step 5b per-slug passes, batch re-run, per-slug mini-consolidators and batch mini-consolidator: `600`. Use the concrete output file, or `-` for multi-output/envelope-only batches. Follow SKILL.md Step 3 notification/deadline handling; Skill bridge calls do not get rows.

Run checks below. Native tools first (Glob, Grep, Read); Bash only for pipeline ops native tools can't do.

**Agent roster consistency policy**: evaluate agent system as capability set, not just files. For every overlap in checks 20 or 17, explicit judgment:

- **keep** when both roles own meaningfully different acceptance criteria
- **sharpen** when both roles justified but descriptions/handoffs too fuzzy
- **merge/prune** when roles differ mostly by tone or examples, not decision surface

Don't leave overlap findings as vague "potential duplication." Audit must state which outcome applies and why.

**Context discipline for Step 4**: write all check findings to `$RUN_DIR/system-checks.md` (Write tool after checks complete), not main context. Keep one-line status per check in context:

- `✓ Check N — <one-line result>` (pass)
- `⚠ Check N — N findings` (issues)

**Scope filter**: when `$SCOPE` is set, run only checks listed for that scope; skip all others silently.

> Checks 42, 43 (and 14a/14b/14c/14d/14e, 32d, cli-flag-drift, R3) run in Step 1b on every invocation regardless of scope — never list them here; SKILL.md §Step 1b: Layer-1 deterministic static pass forbids prose re-derivation.

- `agents` — Checks 14a, 14b, 15, 16, 19, 20, 17, 12, 13, 25, 22, 26, 29, 35, 36, 40, 41 (files: `.claude/agents/*.md` + `plugins/*/agents/*.md`)
- `skills` — Checks 14a, 14b, 15, 16, 17, 12, 23, 22, 13, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 35, 36, 37, 38, 40, 41 (files: `.claude/skills/*/SKILL.md` + `plugins/*/skills/*/SKILL.md`)
- `rules` — Checks 18, 12, 13, 29, 32c, 41 (32d skipped — no plugin bin/ in rules scope)
- `communication` — Checks 15, 16, 12, 13, 29
- `setup` — Checks 1, 2, 3, 4, 5, 9, 10, 11, 7, 6, 8, 30, 37, 39, I1, I2, I3 (Step 3: one foundry:curator spawn for `setup` SKILL.md only; I1–I3 read `~/.claude/`)
- `plugin` — Checks 7, 8 (Step 3: one foundry:curator spawn for `${CLAUDE_PLUGIN_ROOT}/skills/setup/SKILL.md` only)
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
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name foundry) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
for _F in checks-index.md checks-security.md; do python "$PLUGIN_ROOT/bin/load_mode.py" audit templates "$_F" $( [ "$LOCAL_MODE" = true ] && echo "--local" ); done  # timeout: 5000
```

Full check index (Checks 1–41, I1–I3, R1–R5 with severity, scope, notes) and security checks (35–37) loaded above.

### Claude Code docs freshness (within Step 4)

> Replace `<RUN_DIR>` with the actual run-directory path (sentinel `${TMPDIR:-/tmp}/audit-state-${CSID}/run-dir`) before dispatching — the spawned agent gets a fresh shell and cannot expand orchestrator variables.

```text
Agent(subagent_type="foundry:web-explorer", name="docs-freshness", description="Claude Code docs drift check", prompt="Fetch current Claude Code docs (https://code.claude.com/docs/en/). If that URL returns 404 or redirects, navigate from https://code.claude.com homepage to find the documentation section. If docs are entirely unavailable, return {\"status\":\"unavailable\",\"findings\":0}. Check: hook event names + type field vs documented schema (deprecated decision:/reason: fields); agent frontmatter fields + model values; skill frontmatter fields; new features passing genuine-value filter → Upgrade Proposals table (max 5, classify config or capability). Write full findings to <RUN_DIR>/docs-freshness.md using the Write tool. End your full findings file with a `## Confidence` block per quality-gates.md format (Score, Gaps, Refinements). Return ONLY: {\"status\":\"done\",\"file\":\"<RUN_DIR>/docs-freshness.md\",\"findings\":N,\"deprecated\":N,\"new_features\":N,\"confidence\":0.N,\"summary\":\"N findings, N deprecated, N new features\"}")
```

<!-- URLs fetched live by web-explorer at runtime; graceful degradation: if any 404, instruct navigation from code.claude.com homepage. -->

Severity: deprecated/invalid = **high**; deprecated frontmatter field = **medium**; new feature not used = **Upgrade Proposals** (not LOW).

After checks complete: collect `⚠` lines, write full details to `$RUN_DIR/system-checks.md`, include only summary table in context.

## Step 5: Aggregate and classify findings

**Delegate aggregation** to consolidator agent to avoid flooding main context. Same block refreshes the compaction contract (Step 4 → Step 5 aggregate boundary) and resolves the two absolute paths the consolidator must read — it runs in a fresh shell, can't expand orchestrator variables, so these are interpolated as literal strings into the prompt below:

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name foundry) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _RUN_DIR < "${TMPDIR:-/tmp}/audit-state-${CSID}/run-dir" 2>/dev/null || _RUN_DIR=""
[ -n "$_RUN_DIR" ] || { echo "! BLOCKED — audit run-dir sentinel missing; re-run /foundry:audit"; exit 1; }
_PRESERVE="run-dir=$_RUN_DIR, static-findings=${TMPDIR:-/tmp}/audit-state-${CSID}/static-findings.jsonl, finding-files=$_RUN_DIR/*.md"
python "$PLUGIN_ROOT/bin/write_skill_contract.py" --keep-file "${TMPDIR:-/tmp}/audit-state-${CSID}/keep-items" "foundry:audit" "aggregate (after parallel curator+system-checks fan-out)" "$_RUN_DIR" "$_PRESERVE" "consolidate findings → aggregate.md + summary.jsonl → Step 7 report"  # timeout: 5000
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
IFS= read -r AUDIT_TPL < "${TMPDIR:-/tmp}/audit-state-${CSID}/audit-tpl" 2>/dev/null || AUDIT_TPL=$(python "$PLUGIN_ROOT/bin/resolve_skill_subdir.py" audit templates $( [ "$LOCAL_MODE" = true ] && echo "--local" ))
IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/audit-state-${CSID}/run-dir" 2>/dev/null || RUN_DIR=""
printf "RUN_DIR=%s\nSTATIC_FINDINGS_PATH=%s\nSEVERITY_TABLE_PATH=%s\nPRECOMMIT_VALIDATION_PATH=%s\nPRECOMMIT_STATUS_PATH=%s\n" "$RUN_DIR" "${TMPDIR:-/tmp}/audit-state-${CSID}/static-findings.jsonl" "$(dirname "$AUDIT_TPL")/severity-table.md" "${TMPDIR:-/tmp}/audit-state-${CSID}/precommit-validate.txt" "${TMPDIR:-/tmp}/audit-state-${CSID}/precommit-status.txt"
```

Replace `<RUN_DIR>`, `<STATIC_FINDINGS_PATH>`, `<SEVERITY_TABLE_PATH>`, and `<PRECOMMIT_VALIDATION_PATH>` in the prompt below with the literal values printed above. Spawn **foundry:curator** consolidator:

<!-- policy-sibling: plugins/cc_foundry/skills/audit/modes/fix.md §NON_AUTO_FIXABLE (same five exclusion classes in the consolidator prompt) -->

Append to the consolidator prompt after replacing `<PRECOMMIT_STATUS_PATH>` with its printed literal path: "Read `<PRECOMMIT_STATUS_PATH>` alongside the validation output. Its `validation_status` is the validator exit code, `unavailable`, or `not-configured`; classify nonzero codes as preflight findings, report unavailable validation, and skip not-configured without claiming a validation pass. Missing or unreadable status stays unverified."

> "Read the Step 3 per-file reports and Step 4 `system-checks*.md`/`docs-freshness.md` in `<RUN_DIR>/`. Exclude mode-prefixed reports (`challenger-`, `deep-curator-`, `codex-adversarial-`, `security-`, `adversarial-`, `efficiency-`) from the root glob, plus generated aggregate, re-audit, gate, fix and low-confidence reports; they are not fresh standard findings. For combined modes, consume the mode-specific staged rows and read `<RUN_DIR>/adversarial/adversarial-aggregate.md` and `<RUN_DIR>/efficiency/efficiency-report.md` once as supporting context, never both raw phase reports and their aggregates. Read `<PRECOMMIT_VALIDATION_PATH>` from Step 1, classify nonzero pre-commit configuration validation as a preflight finding, and report unavailable validation without claiming success. Read the deterministic Layer-1 results at `<STATIC_FINDINGS_PATH>` (Step 1b — one JSON object per check; each `\"status\":\"fail\"` object's `lines` array is a set of already-verified mechanical findings, severity per the check's known level: fence/mode-dispatch/agent-waits=high, tag/README-drift/bash-persistence/spawn-vars/shared-drift=medium, orphaned-bin/routing=medium). Run `cat "<SEVERITY_TABLE_PATH>"` via the Bash tool and apply its severity classification and closed report-only advisory list. Antipatterns that indicate severity under-classification are also in that file. Group all findings by severity (security, critical, high, medium, low). Apply the one-finding-per-issue rule: when a single location has multiple distinct problems at different severities, emit one finding entry per problem. Stage new standard findings in `<RUN_DIR>/summary.jsonl.standard.rec` — one compact JSON object per line, one line per finding: `{"file":"<repo-relative path>","sev":"security|critical|high|medium|low","id":"H1","line":<line number or null>,"category":"<category>","auto_fixable":true|false,"one_line":"<finding description>"}`. Zero standard findings: Write an empty `summary.jsonl.standard.rec` so the helper creates the empty ledger. Never use basenames as file keys; preserve the source-path-to-plugin-prefixed-slug mapping from Step 3 for downstream artifact names. Installed sources use their logical plugin-relative inventory coordinates, not cache-version paths. Set auto_fixable false for these NON_AUTO_FIXABLE classes: Check 3 (settings.json missing permission), Check 5 critical/high (permission safety), Check 19 (model tier mismatch), CLAUDE.md contradiction, dead loop in follow-up chains; also set it false for the closed advisory list. Record each exclusion reason in one_line; unknown eligibility stays false with its reason, not a new advisory class. Deduplicate against existing same-run rows and mode staging by source path, line and issue; preserve report-only flags and user decisions, never deduplicate against prior runs. Append staged rows with `python \"${CLAUDE_PLUGIN_ROOT}/bin/append_ledger.py\" \"<RUN_DIR>/summary.jsonl\" --run-root \"<RUN_DIR>\"`. Render canonical `<RUN_DIR>/aggregate.md` from all current rows using the Write tool; include a no-findings note if empty and end with a `## Confidence` block per quality-gates.md format (Score, Gaps, Refinements). This ledger is what the orchestrator will read; aggregate.md is for human review only. Return ONLY a compact JSON envelope on your final line — nothing else after it: `{\"status\":\"done\",\"file\":\"<RUN_DIR>/aggregate.md\",\"findings\":N,\"severity\":{\"security\":N,\"critical\":N,\"high\":N,\"medium\":N,\"low\":N},\"confidence\":0.N,\"summary\":\"N findings total: S security, C critical, H high, M medium, L low\"}`"

Main context receives only that one-liner. Orchestrator MUST NOT read `aggregate.md` in full — 200–600 lines, overflows context on large audits. Use `$RUN_DIR/summary.jsonl` for all dispatch decisions, Steps 7 and 8.

## Step 5b: Low-confidence remediation

Parse confidence scores from each file's `## Confidence` block in `<RUN_DIR>/<slug>.md` output files (use Glob + Read — batch envelopes carry aggregate confidence, not per-file scores; individual file reports are the authoritative source). For each slug where `Score` < **0.80**, run three passes in two waves: A first; B and C in parallel once A's `<slug>-rerun.md` exists, since both read it. Slugs still run in parallel with each other.

**Fan-out ceiling**: when MORE THAN 8 slugs score \<0.80, do NOT run per-slug passes (4 spawns × N is unbounded) — instead spawn ONE consolidated **foundry:curator** re-run covering all low-confidence slugs (batched prompt listing every slug + its `Gaps:` block, one combined `<RUN_DIR>/lowconf-batch-rerun.md`) plus one Codex pass over same batch when available; run pass B (docs check) only for slugs whose gaps explicitly cite schema/docs uncertainty. Systematically low confidence signals a rubric or curator problem, not N independent file problems — remediate once, not N×4 times.

**Batch consumer**: after the batch re-run exists, use it as the input for the single batch Codex pass, saving its compact findings/status to `<RUN_DIR>/codex-recheck-lowconf-batch.md`; selected per-slug docs checks read their source's section in that batch report. After those passes return or time out, spawn one **batch mini-consolidator** to merge `<RUN_DIR>/lowconf-batch-rerun.md`, `codex-recheck-lowconf-batch.md` and the selected `docs-recheck-<slug>.md` files. It writes `<RUN_DIR>/lowconf-batch-remediation.md` for the `aggregate.md` remediation section and stages net-new Step 5 rows to `<RUN_DIR>/summary.jsonl.lowconf-batch.rec`. Do not spawn per-slug mini-consolidators on the batch route. The shared append call below consumes that staging file after the batch mini-consolidator returns.

**Health monitoring** (CLAUDE.md §6): SKILL.md Step 3 deadline protocol — in each wave's spawn response Write that wave's watch file — `$RUN_DIR/agent-watch-lowconf-a.tsv`, then `$RUN_DIR/agent-watch-lowconf-b.tsv` (one row per spawned pass, its output file, `600`; C is a `Skill()` call, no row); on each notification run the Step 3 `agent_watch.py` block once. Notification without output → mark `timed_out` now, surface with ⏱ in final report; never a waiting tool.

**A — Double-reasoning pass** (curator re-run with gaps called out):

Spawn **foundry:curator** with the prior report and its `Gaps:` block:

> "Re-audit `<original-source-file>` targeting these specific gaps from the prior pass: `<Gaps block content>`. Address each gap explicitly — do not repeat prior findings verbatim; focus on what was uncertain. Write updated findings to `<RUN_DIR>/<slug>-rerun.md`. End your full findings file with a `## Confidence` block per quality-gates.md format (Score, Gaps, Refinements). Return ONLY: `{\"status\":\"done\",\"file\":\"<path>\",\"findings\":N,\"confidence\":0.N,\"summary\":\"...\"}`"

**B — Docs consultation** (verify findings against current Claude Code schema):

Spawn **foundry:web-explorer**:

> "Fetch current Claude Code docs for `[agent|skill|hook]` schema — navigate from `https://code.claude.com/docs/en/` to the `[sub-agents|skills|hooks]` page. Verify that findings about frontmatter fields or documented behavior in `<RUN_DIR>/<slug>-rerun.md` are accurate against current docs. List any corrections. Write to `<RUN_DIR>/docs-recheck-<slug>.md`. End your full findings file with a `## Confidence` block per quality-gates.md format (Score, Gaps, Refinements). Return ONLY: `{\"status\":\"done\",\"file\":\"<path>\",\"corrections\":N,\"confidence\":0.N}`"

**C — Codex adversarial pass** (requires `bridge@borda-ai-rig`):

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name foundry) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "$PLUGIN_ROOT/bin/check_bridge.py" --status > "${TMPDIR:-/tmp}/audit-state-${CSID}/codex-status" 2>/dev/null || echo "absent" > "${TMPDIR:-/tmp}/audit-state-${CSID}/codex-status"  # timeout: 5000
IFS= read -r CODEX_STATUS < "${TMPDIR:-/tmp}/audit-state-${CSID}/codex-status" || CODEX_STATUS="absent"
[ "$CODEX_STATUS" = "available" ] && printf 'CODEX_AVAILABLE=%s\n' true || printf 'CODEX_AVAILABLE=%s\n' false  # timeout: 5000
```

If `CODEX_AVAILABLE=true`, resolve every placeholder in the following review brief, then pass that entire rendered brief to `Skill(skill="bridge:review", args="...")`:

> "Adversarial review of low-confidence findings for `<original-source-file>`. Prior foundry:curator pass scored confidence=<N> — gaps: `<Gaps>`. Challenge each finding: real? severity correct? missed issues? Read source file + prior report `<RUN_DIR>/<slug>-rerun.md`. Return findings ending with a `## Confidence` block per quality-gates.md format (Score, Gaps, Refinements)."

`bridge:review` runs read-only and writes no audit file: the orchestrator Writes the returned compact envelope's findings and status to `<RUN_DIR>/codex-recheck-<slug>.md` with the Write tool, retaining its artifact link for supporting detail; a non-`complete` bridge status is a coverage gap, so the mini-consolidator sees the unreviewed scope. Never assume the compact reply contains full Markdown or a Confidence block.

If `CODEX_AVAILABLE=false`: log `[Step 5b] bridge@borda-ai-rig is ${CODEX_STATUS} — adversarial pass skipped.` Include note in final report `## Confidence` section.

**After all three passes complete** for a slug on the per-slug route: spawn **foundry:curator** mini-consolidator to merge `<slug>-rerun.md`, `docs-recheck-<slug>.md`, `codex-recheck-<slug>.md`, write `<RUN_DIR>/lowconf-<slug>-remediation.md` with reconciled findings (promoted corrections, confirmed findings, refuted findings) for the `### Low-Confidence Remediation — <slug>` section of `aggregate.md`. Net-new findings (absent from `summary.jsonl` by `file` + `line` + `one_line`) go to `<RUN_DIR>/summary.jsonl.<slug>.rec` — Write tool, one JSON line per finding in the Step 5 schema; the mini-consolidator never rewrites `summary.jsonl` itself. Parallel slugs each stage their own `.rec` and never write shared `aggregate.md`.

**After every slug's mini-consolidator has returned** (or timed out), append all staged findings in one call — `append_ledger.py` sweeps every `summary.jsonl.<slug>.rec` in name order, terminates each line, and deletes the staged files; nothing staged is a no-op:

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name foundry) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _RUN_DIR < "${TMPDIR:-/tmp}/audit-state-${CSID}/run-dir" 2>/dev/null || _RUN_DIR=""
[ -n "$_RUN_DIR" ] || { echo "! BLOCKED — audit run-dir sentinel missing; staged summary.jsonl.*.rec left in place"; exit 1; }
python "$PLUGIN_ROOT/bin/append_ledger.py" "$_RUN_DIR/summary.jsonl" --run-root "$_RUN_DIR"  # timeout: 5000
```

The same append boundary applies after the batch mini-consolidator. Then run this fixed append block once to add the completed `lowconf-*-remediation.md` sections (including `lowconf-batch-remediation.md`) to `aggregate.md`, preserving its existing findings and Confidence block without an orchestrator Read/Write round trip. The source remediation files remain available; record missing/timed-out passes as gaps.

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name foundry) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _RUN_DIR < "${TMPDIR:-/tmp}/audit-state-${CSID}/run-dir" 2>/dev/null || _RUN_DIR=""
[ -n "$_RUN_DIR" ] || { echo "! BLOCKED — audit run-dir sentinel missing; remediation files left in place"; exit 1; }
python "$PLUGIN_ROOT/bin/append_ledger.py" "$_RUN_DIR/aggregate.md" --run-root "$_RUN_DIR" --from "$_RUN_DIR"/lowconf-*-remediation.md  # timeout: 5000
```

**Skip Step 5b entirely** when no Step 3 files scored below 0.80.

Returns to SKILL.md Step 6 (cross-validate critical findings) after Step 5b completes or is skipped.

## Step 7: Report findings

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name foundry) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _RUN_DIR < "${TMPDIR:-/tmp}/audit-state-${CSID}/run-dir" 2>/dev/null || _RUN_DIR=""
[ -n "$_RUN_DIR" ] || { echo "! BLOCKED — audit run-dir sentinel missing; re-run /foundry:audit"; exit 1; }
_PRESERVE="run-dir=$_RUN_DIR, aggregate=$_RUN_DIR/aggregate.md, summary=$_RUN_DIR/summary.jsonl"
python "$PLUGIN_ROOT/bin/write_skill_contract.py" --keep-file "${TMPDIR:-/tmp}/audit-state-${CSID}/keep-items" "foundry:audit" "report (after aggregate complete)" "$_RUN_DIR" "$_PRESERVE" "emit report → follow-up gate → optional fix mode (Steps 8-10) → Step 11"  # timeout: 5000
```

Before emitting, read current `$RUN_DIR/summary.jsonl` (Step 5b may have appended net-new promoted findings), recompute severity totals. Build the full report (omit Upgrade Proposals if none passed genuine-value filter), Write it to `$RUN_DIR/audit-report.md`, and deliver it once in the final reply after the answer, after the last tool call, below Step 11b's header table — never before the question, never in an option or Bash/tool stdout. The first question's text carries only the `Audit Report` headline, exact `Total: N`, severity counts and `→ $RUN_DIR/summary.jsonl`, plus `→ $RUN_DIR/audit-report.md`. Keep the question text to at most 6 plain lines (target 600 characters, hard cap 1,500), with no table, no pipe layout and no code fence. Options have no `preview`. `--skip-gate` (no question): the full findings report still goes into the turn's final reply after its last tool call, below Step 11b's header table — the `Stop` hook enforces delivery.

<!-- policy-sibling: plugins/cc_foundry/skills/audit/SKILL.md (Follow-up gate: same question-text cap, no preview, full findings report once in the final reply) -->

> **Output-Routing exemption**: this report-header follow-up carries a plain summary in the question and the full findings report in the final reply after the answer. Output Routing (5+ findings → `.temp/output-*.md`, summary only) does not shorten that final findings report, and the table-first-in-reply rule does not require a copy before the question; durable copies remain `$RUN_DIR/audit-report.md`, `$RUN_DIR/aggregate.md` and Step 11's report.

```markdown
## Audit Report

### Findings by Severity
#### Security (N) | Critical (N) | High (N) | Medium (N) | Low (N)
| File | Line | Issue | Category |
|---|---|---|---|
| agents/foo.md | 42 | References `bar-agent` which does not exist on disk | broken cross-ref |

### Summary
- Total: N (S security, C critical, H high, M medium, L low)
- Fix options when a question is required: (b) Fix SECURITY + CRITICAL + HIGH · (a) Fix auto-fixable (Recommended) · (c) Fix ALL · (d) Skip; omit (b) when no top-tier findings exist, per SKILL.md

### Upgrade Proposals (N — run `/foundry:audit --upgrade` separately to apply)
| # | Feature | Type | Rationale |
|---|---------|------|-----------|
```

After saving the report, apply SKILL.md §Follow-up gate: `Total: 0` continues directly to Step 11; `--skip-gate` returns the report without fix dispatch; an explicitly authorized fix scope continues inline to Steps 8–10 without another scope question, preserving owning gates for NON_AUTO_FIXABLE decisions. Otherwise fire **Follow-up gate**, with question header `audit`. The hook requires `aggregate.md`, valid `summary.jsonl`, and a short plain summary in the first question's text naming `$RUN_DIR/summary.jsonl`, the `Audit Report` headline and exact `Total: N` (SKILL.md §Follow-up gate). The full current findings report, including every finding's `one_line`, is printed in the final reply after the answer; `Stop` still requires it there. A zero-finding summary is valid when its aggregate exists. Missing/unreadable delivery blocks only this fix transition; diagnostic/recovery questions use a different header and remain available. Do not require Step 11's final report before Step 7. If user picks fix option (a–c), proceed inline to fix mode (Steps 8–10, loaded from `modes/fix.md`). Otherwise skip to Step 11.

Returns to SKILL.md Steps 8–10 (fix dispatch, gated) / Step 11 (final report).
