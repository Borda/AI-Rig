# Adversarial Mode — foundry:audit

Triggered by `/audit --adversarial` (alias: `--challenge`). Read+executed by `/audit` when `--adversarial` flag present.

## Mode: adversarial (alias: --challenge)

**Trigger**: `/audit [<scope>...] --adversarial`

Adversarial review of all agents + skills in scope. Runs parallel with or after standard per-file audit (Step 3). Surfaces issues curator pass misses: subtle logic flaws, inconsistent claims, NOT-for gaps, scope leakage, cross-file contradictions, security vulnerabilities in bin/ executables.

**Spawn wave cap (overrides "same response"/"parallel with" phrasing below when combined count is high)**: caps are per model tier (`CAP_OPUS`/`CAP_SONNET`, constants block; canonical table in claude-config.md §Parallel Spawn Ceilings), not one shared total — "parallel" between phases means logically independent, not necessarily launched in one burst.

- **Opus pool** (`CAP_OPUS`, default 5): Steps 3-4's curator batches + Phase A's challenger batches + Phase A-prime's curator batches all draw from this one pool, since `foundry:curator` and `foundry:challenger` are both opus-tier. Before launching, sum every opus-tier batch about to be spawned across all currently-due phases; if that sum exceeds `CAP_OPUS`, split into ordered waves of `WAVE_STEP` (default 5) — Step 3/4 first (core audit value), then Phase A, then Phase A-prime. Wait for a wave to return before opening the next.
- **Sonnet pool** (`CAP_SONNET`, default 8): Step 4's docs-freshness web-explorer + Phase D's qa-specialist batches draw from this pool, separately from the opus pool above — the two may run concurrently.
- **Phase B** (Codex bridge call) isn't a Claude `Agent()` spawn and draws from neither pool — it fits any wave with room, governed by Codex-side capacity instead (see codex-rig `specialist-orchestration.md` if `bridge` routes through it).

Below a pool's cap, launch that pool's phases together as written below.

All phase reports live in `<RUN_DIR>/adversarial/`; create that directory before dispatch. `<RUN_DIR>` remains the shared run root for canonical gate inputs. For every Agent spawn below, write `agent-watch-adversarial-<phase>-<wave>.tsv` at the run root in the spawn response, one `<unique-agent-name>\t<output-file-or-dash>\t900` row per agent; use `-` for multi-file batches. Follow SKILL.md Step 3 notification/deadline handling. Phase B is a Skill call and has no watch row.

**Phase A — Challenger sweep** (parallel with Phase B):

Before Phase A, assign a unique `<file-slug>` to every Step 2 inventory path even when Step 3 is skipped: `<plugin>-agent-<agent-name>` for agents, `<plugin>-skill-<skill-directory>` for skills; `.claude/` sources use the `local` prefix. Nested paths within either kind retain their directory components, joined with `-`; if flattened paths collide, append the full logical POSIX path's UTF-8 hex to each colliding slug. Reuse an existing Step 3 mapping when present. Persist the source-path-to-slug mapping and substitute literal slugs in every prompt before dispatch.

For each file in scope (Step 2 inventory; default all agents + skills if no explicit scope), spawn **foundry:challenger** (adversarial challenge of design decisions inside agent/skill files is in challenger scope; only config structure review — verbosity, formatting, cross-ref integrity, step numbering — routes to `foundry:curator`, which Phase A-prime and Step 3 already cover):

> "Adversarially challenge this agent/skill. `--no-codex` — Phase B runs the Codex pass separately. Do NOT accept claims at face value. Find: (1) unstated assumptions failing in edge cases, (2) NOT-for coverage gaps — tasks this agent wrongly accepts because exclusions are incomplete, (3) conflicting instructions producing non-deterministic or contradictory behavior, (4) workflow steps routing to the wrong sub-agent for the stated goal, (5) implicit scope contradicting explicit NOT-for lines. Report every finding with specific evidence from the file."
>
> Write full findings to `<RUN_DIR>/adversarial/challenger-<file-slug>.md` where `<file-slug>` is the plugin-prefixed slug assigned above or retained from SKILL.md Step 3; preserve the source-path-to-slug mapping, including agent/skill disambiguation. Never use bare `challenger-SKILL.md`. Envelope severity mapping from your Blocker/Concern/Nitpick sections: each Blocker → `critical`, each Concern → `high`, each Nitpick → `low`; a Blocker or Concern from the Security Risks dimension → `security` instead; `medium` stays 0. Return ONLY: `{"status":"done","file":"<path>","findings":N,"severity":{"security":N,"critical":N,"high":N,"medium":N,"low":N},"confidence":0.N}`

Use `ADVERSARIAL_BATCH_SIZE` (default 2) for grouping — smaller batches than Step 3 (`BATCH_SIZE_MIN`) to maximise per-file attention depth. Same plugin-aware batching algorithm; substitute `ADVERSARIAL_BATCH_SIZE` for `BATCH_SIZE_MIN`.

**Phase A-prime — Unconstrained curator pass** (parallel with Phase A and Phase B):

Resolve checklist path first — spawned curator runs in a fresh shell, can't expand orchestrator variables, so resolved value interpolates as a literal string into the prompt:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
IFS= read -r AUDIT_TPL < "${TMPDIR:-/tmp}/audit-state-${CSID}/audit-tpl" 2>/dev/null || AUDIT_TPL=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/resolve_skill_subdir.py" audit templates $( [ "$LOCAL_MODE" = true ] && echo "--local" ))
printf "CURATOR_PROMPT_PATH=%s/curator-prompt.md\n" "$AUDIT_TPL"
```

For each file in scope, spawn **foundry:curator** with no scope constraint — replace `<CURATOR_PROMPT_PATH>` with the literal value printed above:

> "Audit this file. Run `cat "<CURATOR_PROMPT_PATH>"` via the Bash tool, use as baseline checklist — apply all those checks. Then go beyond: report ANY additional issue outside the explicit checklist. Look especially for: execution continuing after a confirmed failure path with no `exit 1`; incomplete specs leaving an agent uncertain at a branch point; undocumented implicit dependencies (env vars, files, network) not declared in inputs; workflow logic self-consistent but silently producing wrong results on a valid non-happy-path input. No scope constraint — senior-engineer judgment applies."
>
> Write full findings to `<RUN_DIR>/adversarial/deep-curator-<file-slug>.md` using same `<file-slug>` convention as Phase A. Return ONLY: `{"status":"done","file":"<path>","findings":N,"severity":{"security":N,"critical":N,"high":N,"medium":N,"low":N},"confidence":0.N}`

Use `ADVERSARIAL_BATCH_SIZE` grouping. Phase C deduplicates Phase A-prime findings against `summary.jsonl` from Steps 3–6 in SAME RUN_DIR only — not against prior runs. Adversarial-only mode (no same-run standard audit): all Phase A-prime findings carried forward.

**Phase B — Codex adversarial pass** (parallel with Phase A):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/check_bridge.py" --status > "${TMPDIR:-/tmp}/audit-state-${CSID}/codex-status" 2>/dev/null || echo "absent" > "${TMPDIR:-/tmp}/audit-state-${CSID}/codex-status"  # timeout: 5000
IFS= read -r CODEX_STATUS < "${TMPDIR:-/tmp}/audit-state-${CSID}/codex-status" || CODEX_STATUS="absent"
[ "$CODEX_STATUS" = "available" ] && printf 'CODEX_AVAILABLE=%s\n' true || printf 'CODEX_AVAILABLE=%s\n' false  # timeout: 5000
```

If the printed status is `CODEX_AVAILABLE=true`, render a brief per inventory batch with literal source paths and use `Skill(skill="bridge:review", args="Inventory review of these files, including unchanged sources: <source paths>. Read every file; do not limit review to the working-tree diff. Find cross-file inconsistencies, circular dispatch chains, ambiguous agent routing, and instructions assuming unavailable tools. Return compact findings with source path, line, severity, category and proposed correction. Read-only: do not write files.")`. Availability false: log the persisted bridge status and skip Phase B. This inventory pass does not use the diff-only codex-prepass.md contract.

The orchestrator uses the Write tool to save the returned envelope's `findings` and status to `<RUN_DIR>/adversarial/codex-adversarial-<batch-slug>.md`; bridge:review is read-only and returns compact findings, not a written findings file. Preserve any bridge artifact link for supporting detail. Non-`complete` status or an unreviewed file is a coverage gap, never a clean pass; Phase C consumes the captured findings and gaps.

For state-changing workflows, include unchanged downstream consumers and the next ordinary user action in supplied context. Compare producer postconditions with consumer preconditions; challenge whether all local checks could pass while the promised result fails. Record concrete counterexamples or unexecuted probe requests, positive cases, coverage gaps for identity, ownership, destination, lifetime, supported resume/retry behavior. Preserve read-only audit authority; no production mutation implied by a probe request.

**Phase D — Security & Vulnerability Review** (parallel with Phases A, A-prime, B):

Scope resolution — map audit scope tokens to plugin dirs, collect all bin/ scripts:

- Default (full sweep): all `plugins/*/bin/*.py`
- Named scope (e.g. `foundry`, `oss`, `codemap`): `plugins/<name>/bin/*.py`
- `--local` mode: same paths from `plugins/` source tree; non-local: resolve each scoped plugin's active root with `python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/resolve_plugin_root.py" --plugin-name <manifest-name>` and scan only that root's `bin/*.py`. Never glob cache versions; failed resolution is unreviewed scope, not zero scripts. Pass the resolved literal bin directory into the review prompt.
- `bin/` executables are Python-only (plugins/CLAUDE.md §Installability); legacy `.sh` shims are portability debt, out of Phase D scope — list any found as unreviewed in the report
- Zero bin/ scripts for scope: skip Phase D, note in report

Per-plugin security sweep — for each plugin in scope with bin/ scripts, spawn **foundry:qa-specialist**:

> "Security and vulnerability review of all bin/ scripts in `<resolved-bin-directory>`. Read-only audit: do NOT create, edit or run tests, and do NOT modify any repository file — your only write is the findings file below. You are a black-box security reviewer — focus on executable surface only, not callers. Review every `.py` file assigned to this batch. Check OWASP Top 10 applicability: (1) injection — `subprocess` calls with `shell=True` or string-concatenated command args; (2) path traversal — unvalidated file paths from argv; (3) insecure deserialization — `pickle.load`, `yaml.load` without `Loader`; (4) hardcoded secrets — API keys, tokens, passwords in source; (5) uncontrolled resource consumption — unbounded loops or file reads without size check. Write full findings per file to `<RUN_DIR>/adversarial/security-<plugin-name>-<batch>.md`. Return ONLY: `{\"status\":\"done\",\"file\":\"<path>\",\"scripts_reviewed\":N,\"findings\":N,\"severity\":{\"security\":N,\"critical\":N,\"high\":N,\"medium\":N,\"low\":N},\"confidence\":0.N}`"

Use `BATCH_SIZE_MIN` (not `ADVERSARIAL_BATCH_SIZE`) for Phase D — bin/ scripts shorter, security checks deterministic; BATCH_SIZE_MIN fine here. Plugin with ≤5 bin/ scripts: one foundry:qa-specialist spawn covers all; larger sets batch by 5.

Phase D runs parallel with Phases A, A-prime, and B — same async launch pattern.

**Phase C — Aggregate and deduplicate**:

With `--efficiency`, defer this Phase C dispatch until efficiency Phase C completes; run efficiency Phases A–C now, then return here. Spawn **foundry:curator** consolidator exactly once to merge Phase A + Phase A-prime + Phase B + Phase D findings and, when combined, efficiency findings. This consolidator performs Step 5's sole writer role for the combined route; do not wait for a separately dispatched Step 5 or fire a gate before it returns. Cross-reference against standard audit `summary.jsonl` (same RUN_DIR). Surface only findings NOT already in standard audit — adversarial adds signal, not noise.

Adversarial-only mode (`--adversarial` flag without preceding standard audit in same RUN_DIR): no `summary.jsonl` exists in this RUN_DIR. Skip dedup entirely — surface all adversarial findings without overlap filtering. Do NOT dedup against prior audit runs in `.reports/audit/`; prior runs may have unresolved findings still needing fixing.

Write deduplicated findings to `<RUN_DIR>/adversarial/adversarial-aggregate.md` and stage `<RUN_DIR>/summary.jsonl.adversarial.rec` (Step 5 schema: repo-relative `file`, `sev`, `id`, `line`, `category`, `auto_fixable`, `one_line`). Phase C never rewrites the ledger. With `--efficiency`, read `<RUN_DIR>/efficiency/efficiency-report.md` or, if absent, the existing run-root `<RUN_DIR>/efficiency-report.md` once; consume its staged `<RUN_DIR>/summary.jsonl.efficiency.rec` if present, otherwise stage its findings there using the same schema. Apply efficiency Phase C's row-selection rules to E8/E9/E10 and plan items: one row per P1–P4 item plus outside-plan findings; exclude P-HOLD and E11 entirely from the ledger and fix gate. E8/E9 set auto_fixable false; E10 merges crossing a model decision point also set it false, while setup-only merges may be true; model-tier and all other systemic/advisory exclusions stay false. Preserve P-HOLD/E11 suggestions in the human report. Deduplicate both mode stages against each other and current same-run rows by source path, line and issue; preserve systemic/advisory exclusions and decisions. A missing efficiency report is a coverage gap, never zero findings. After standard aggregation (if any) and both active modes complete, this consolidator runs `python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/append_ledger.py" "<RUN_DIR>/summary.jsonl"`, consuming both stages, then renders canonical `<RUN_DIR>/aggregate.md` from all current same-run rows, retaining standard findings and ending with a Confidence block before returning its envelope. Zero findings: stage an empty `.rec` so the same helper creates empty canonical `summary.jsonl`, and write a non-empty no-findings `aggregate.md`. Return: `{"status":"done","new_findings":N,"overlapping":N,"severity":{"security":N,"critical":N,"high":N,"medium":N,"low":N}}`

**Report format**:

```markdown
## Adversarial Audit — <date> — <scope>

| File | Challenger | Deep-curator | Codex | Security | New Findings | Top Issue |
|------|-----------|--------------|-------|----------|--------------|-----------|
| agents/curator.md | 3 | 1 | 1 | 0 | 2 | NOT-for gap: accepts task X |
```

Adversarial findings feed into standard fix pipeline (Steps 7–10) when user picks fix level from follow-up gate.

**Adversarial-only runs** (no standard audit in same RUN_DIR): run the run-directory setup block from `SKILL.md` Step 3 (`export CSID` → `make_run_dir.py` → empty-path guard → `echo` → sentinel write) before Phase A, then skip Steps 3–6; run only Phases A–D above; skip Phase C dedup; report all adversarial findings. Standard findings from prior `.reports/audit/` runs NOT consulted for dedup — user may not have fixed them.

**Flag aliases**: `--adversarial` and `--challenge` are identical — either triggers this mode.
