---
name: judge
description: Research-supervisor review of program.md — validates experimental methodology (hypothesis clarity, measurement validity, control adequacy, scope, strategy fit), emits APPROVED / NEEDS-REVISION / BLOCKED verdict before expensive run loop.
argument-hint: '[<program.md>] [--skip-validation] [--keep "<items>"]'
effort: medium
allowed-tools: Read, Write, Bash, Grep, Glob, Agent, TaskCreate, TaskUpdate, AskUserQuestion
disable-model-invocation: true
---

<objective>

Research-supervisor review of `program.md` — validates experimental methodology, emits APPROVED / NEEDS-REVISION / BLOCKED verdict before expensive run loop. Read-only; never modifies code or state.

NOT for: running experiments (use `/research:run`); designing hypotheses (use `research:scientist` agent); config quality (`/foundry:audit` (requires `foundry` plugin)). </objective>

<compaction>

- Key boundary: end of J3 — methodology and scientific review agents complete, output files written; before J4 validation and J6 verdict.
- Preserve: RUN_DIR (TMPDIR key), PROGRAM_PATH (TMPDIR key), methodology.md path, scientific-review.md path, SKIP_VALIDATION flag, BRANCH.
- Clear at J1 start (stale prior run) and at end of J6 after terminal summary printed.

</compaction>

<workflow>

<!-- Agent resolution: see _RESEARCH_SHARED/agent-resolution.md -->

## Agent Resolution

**Agent resolution**: load and follow the protocol below. Contains foundry check + fallback table. If foundry not installed: use table to substitute each `foundry:X` with `general-purpose`. Agents: `foundry:solution-architect`, `research:scientist`.

```bash
# loads: compaction-contract.md
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_RESEARCH_SHARED=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/resolve_shared.py" 2>/dev/null)  # timeout: 5000
[ -z "$_RESEARCH_SHARED" ] && { echo "! Plugin path resolution failed — ensure research plugin installed and CLAUDE_PLUGIN_ROOT set, or invoke from project root."; exit 1; }
echo "$_RESEARCH_SHARED" > "${TMPDIR:-/tmp}/research-shared-${CSID}"  # cold resolve — every later site reads this sentinel instead of re-running python
cat "$_RESEARCH_SHARED/agent-resolution.md"
```

| Agent | Fallback if absent |
| -- | -- |
| `foundry:solution-architect` | `general-purpose` (methodology review quality reduced — **⚠ general-purpose agent may not emit `methodology_rating` in required format; verdict defaults to NEEDS-REVISION**) |
| `research:scientist` | `general-purpose` (scientific rigor review quality reduced — **⚠ general-purpose agent may not emit `scientific_rating`; verdict defaults to NEEDS-REVISION**) |

## Judge Mode (Steps J1–J6)

Triggered by `judge` or `judge <file.md>`.

**Task tracking**: create tasks for J1, J2, J3, J4, J5a, J5b, J6 at start — before any tool calls. (J5a = Codex adversarial review; J5b = resolve rating source.)

## Step J1: Locate and parse program.md

**Flag parsing** (first):

```bash
SKIP_VALIDATION=false
[[ "$ARGUMENTS" == *"--skip-validation"* ]] && SKIP_VALIDATION=true
ARGUMENTS="${ARGUMENTS/--skip-validation/}"
ARGUMENTS="${ARGUMENTS#"${ARGUMENTS%%[![:space:]]*}"}"  # trim leading whitespace
```

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/extract-keep-flag.py" judge "$ARGUMENTS"  # timeout: 5000 — parses --keep, clears a stale contract, persists for J3
```

**Unsupported flag check**: load and follow the protocol below. Supported flags for this skill: `--skip-validation`, `--keep`.

```bash
# loads: unsupported-flag-protocol.md
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _RESEARCH_SHARED < "${TMPDIR:-/tmp}/research-shared-${CSID}" 2>/dev/null || _RESEARCH_SHARED=""  # warm read (Check 41)
cat "$_RESEARCH_SHARED/unsupported-flag-protocol.md"
```

**Input resolution** (priority order):

1. Explicit argument: `/research:judge path/to/plan.md`
2. Auto-detect: `program.md` at project root
3. Latest state: scan `.experiments/state/*/state.json` for most recent with `status: running` and non-null `program_file` field
4. If nothing found: stop with error:
   ```text
   No program.md found. Run /research:plan <goal> first, or provide a path: /research:judge <path.md>
   ```

**Schema gate** — before parsing, count matching `## <Section>` headings against the known schema set (`Goal`, `Metric`, `Guard`, `Config`, `Notes`). Zero matches = the located file does not parse as a program.md (e.g. input resolution located an unrelated document) — stop immediately with the same "nothing found" error shown in step 4 above; do NOT proceed to J2's C1–C12 checklist. Distinct trigger from step 4 (file not locatable at all): this fires when a file IS located but its content doesn't match the expected schema.

**Parsing** — find `## <Section>` headings in program.md, extract first fenced code block per section, parse as `key: value` lines, warn on unrecognized keys. `--skip-validation` and `colab_hw` judge-specific, extracted independently.

**Placeholder substitution** — after parsing, apply same substitution as R1: resolve all `{field_name}` tokens in `metric_cmd` and `guard_cmd` using `## Config` fields, fallback to declared default. No `clarification_prompt` in judge — skip clarification-override step.

Extract `<program_title>` from `# Program: <title>` line for reports (fallback `# Campaign: <title>` for legacy files).

## Step J2: Completeness audit

Check C1–C12 plus C2b and C6b (14 items total). Produce findings list with severity. Each finding has: `id`, `check`, `status` (pass/fail/warn), `severity`, `detail`.

| ID | Check | Severity if failing | Description |
| -- | -- | -- | -- |
| C1 | `## Goal` present and non-empty | critical | Campaign cannot run without a goal |
| C2 | `## Metric` has `command` field | critical | No metric = no feedback loop |
| C3 | `## Metric` has `direction` field (higher/lower) | critical | Cannot decide keep/revert without direction |
| C4 | `## Guard` has `command` field | critical | Without guard, regressions go undetected. Note: a command field containing only `echo 0`, `true`, or `exit 0` is equivalent to no guard (always exits 0 regardless of test state) — flag as critical with detail "guard command is a no-op; add real regression detection". |
| C5 | `scope_files` present in `## Config` | high | Without scope, ideation agent modifies arbitrary files |
| C6 | Each `scope_files` path exists on disk (glob match) | high | Non-matching patterns = ideation agent has nothing to work with. If filesystem unavailable, flag `warn` unless path name signals non-existence (e.g., `nonexistent`, `placeholder`, `todo`, `legacy_v1`, `deprecated`, `old`, `removed`). |
| C7 | `target` set in `## Metric` | medium | Without target, campaign runs to max_iterations — may waste compute |
| C8 | `max_iterations` in bounds (1–50) | medium | Missing defaults to 20 (acceptable); >50 violates SKILL.md constants. Additionally: if value is within bounds but >20 AND combined with risk factors (C4 fails / guard empty, OR C6 fails / scope non-existent), add a separate `low` finding: "max_iterations=N is elevated; with no functioning guard/scope, runaway iterations amplify risk — consider reducing to ≤15 until guard/scope is fixed" |
| C9 | `agent_strategy` is valid (`auto`/`perf`/`code`/`ml`/`arch`) | medium | Invalid value silently falls back to `auto` |
| C10 | `compute` is valid (`local`/`colab`/`docker`) | low | Invalid defaults to `local` |
| C11 | `colab_hw` valid (if present) | low | `colab_hw` absent OR is one of `H100, L4, T4, A100` — fail detail: `"colab_hw '<value>' is not in known set {H100, L4, T4, A100} — run mode will warn and null this value to the default GPU"`. Note: this check is a minimum-capability floor — new Colab hardware tiers may exist beyond this list; unknown values are flagged for user verification, not blocked. <!-- policy-sibling: plugins/cc_research/skills/run/SKILL.md, plugins/cc_research/skills/run/modes/colab-setup.md, plugins/cc_research/skills/plan/SKILL.md, plugins/cc_research/skills/sweep/SKILL.md — canonical colab_hw known-set {H100, L4, T4, A100}; keep judge's C11 set in sync with these four. --> |
| C12 | `## Notes` section present | low | Notes optional but improve ideation quality |

**Scope adequacy sub-rule (C6b)** — after C6 passes, assess whether `scope_files` is *sufficient* for stated goal. If goal type implies known bottleneck locations outside declared scope, add `medium` finding:

- Test-speed goal + scope limited to `tests/` only → flag: "conftest.py, fixtures, and test infrastructure outside tests/ are common levers for test runtime; scope may be too narrow"
- Throughput/latency goal + scope limited to single-layer path (e.g., `src/serving/`) → flag: "serving bottlenecks often span middleware, connection pooling, or database layers outside declared scope"
- Any goal where the stated scope excludes a widely-known dependency class → emit medium finding with location `## Config / scope_files`, suggested broader pattern as fix

Distinct from C6 (path existence) — C6b fires even when path exists but is likely insufficient.

**Severity summary**: count findings per severity. Any critical finding = verdict cannot be APPROVED. **Enumeration rule**: check ALL items (C1–C12 plus C2b and C6b) before stopping — don't short-circuit after first critical issue. program.md can have multiple independent flaws across severity levels; Required Changes section must list all, not just verdict-determining one.

**Placeholder token check (C2, C4 sub-rule)** — after confirming `command` present in `## Metric` (C2) and `## Guard` (C4), scan each command for `{...}` tokens. Verify each token's field name exists in `## Config`. Token with no matching field = unresolvable — add `high` finding. Don't flag `{field_name}` tokens as malformed; valid when resolvable.

**Goodhart's Law check (C2b)** — after confirming metric `command` present (C2 passes), assess whether command operationalizes stated `## Goal` or measures proxy. If metric could improve while actual goal NOT achieved, add `critical` finding:

- metric measures test pass rate but goal is latency reduction → critical: "metric is a correctness proxy, not a latency measure"
- metric measures lint error count but goal is bug density reduction → critical: "pylint score is a gameable proxy; agent can suppress warnings without improving actual quality"
- metric measures a format/style score but goal is functional improvement → critical: "metric does not operationalize the stated goal"

Goodhart findings are `critical` (not just methodology notes) — broken metric invalidates entire feedback loop, equivalent impact to C2 (missing command).

**Command feasibility**: J2 validates command fields statically (presence, format). Executability deferred to J4. If `$SKIP_VALIDATION` is `true`, J4 skipped, commands unverified — report as "validation skipped — commands unverified."

## Step J3: Methodology review

Pre-compute run dir before spawning:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
RUN_DIR=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/make_run_dir.py" "judge" ".experiments" 2>/dev/null)  # timeout: 5000
echo "$RUN_DIR" > "${TMPDIR:-/tmp}/judge-run-dir-${CSID}"  # persist for J3 block (Check 41)
```

**Synchronous spawn note**: J3 agents spawned synchronously (not `run_in_background=true`), so CLAUDE.md §6 sentinel polling unreachable mid-call. Timeout handled post-hoc — after each Agent() returns, check output file; if missing/empty mark agent timed out (⏱). See J3 post-call checks below.

Dispatch — scientist dimension always; architect dimension only when the complexity gate fires. When BOTH dimensions are active, J3 is ONE merged spawn covering both (two opus spawns for a single-file review pay 2× ~120,851 tok of fixed overhead for a workload far under the breakeven above); when only the scientist dimension is active, it is a single scientist spawn as before. Per-dimension output files and Confidence blocks are unchanged either way.

Before constructing J3 prompts, expand all bash variables into concrete paths — never pass literal `<path_to_program.md>` or `<RUN_DIR>` placeholders to agents:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
PROGRAM_PATH=$(realpath "$PROGRAM_FILE" 2>/dev/null || echo "$PROGRAM_FILE")
echo "$PROGRAM_PATH" > "${TMPDIR:-/tmp}/judge-program-path-${CSID}"  # persist for J3 complexity-gate block (Check 41)
# Reload RUN_DIR (Check 41: fresh shell per call — persisted in J2 block)
IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/judge-run-dir-${CSID}" 2>/dev/null || RUN_DIR=""
```

Compute `SKIP_VALIDATION_NOTE` before constructing the prompt:

```bash
if [ "${SKIP_VALIDATION:-false}" = "true" ]; then
    SKIP_VALIDATION_NOTE="Local validation skipped via --skip-validation — do NOT assess executability of metric_cmd/guard_cmd; note this limitation in your review."
else
    SKIP_VALIDATION_NOTE="Local validation will run after this review (J4)."
fi
```

**Complexity gate** — mirrors P-P2b; skip architect for narrow single-scope experiments (saves full opus pass):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PROGRAM_PATH < "${TMPDIR:-/tmp}/judge-program-path-${CSID}" 2>/dev/null || PROGRAM_PATH=""  # re-hydrate (Check 41: fresh shell — persisted in J3 pre-spawn block)
SPAWN_ARCHITECT=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/detect-complexity.py" "$PROGRAM_PATH")  # timeout: 5000
```

When `SPAWN_ARCHITECT=false`: skip architect spawn; J5b precedence step 0 sets `methodology_rating="sound"` (scientist review still covers scientific rigor); record `architect: skipped (narrow scope)` in J6 summary.

> **Agent budget** — each spawn costs ~120,851 tok of fixed overhead (~73 tool-calls' worth) plus ~12.0 s/call, so work under ~73 calls is cheaper done inline: spawn nothing. Keep each agent near ~55 tool-calls; past ~60 they stall without returning an envelope, forcing reconstruction from disk. Every spawn prompt must require an envelope even on exhaustion — `partial: true` plus what was finished.

When `SPAWN_ARCHITECT=true`: spawn ONE merged reviewer via `Agent(subagent_type="research:scientist", prompt=<merged prompt>)` (opus) — build the merged prompt exactly per `judge-j3-prompts.md` §Merged spawn — envelope override (both templates with their individual `Return ONLY:` lines stripped, joined by `---`, closed with the override's single array-envelope instruction). The single spawn executes BOTH templates in order and writes `${RUN_DIR}/methodology.md` AND `${RUN_DIR}/scientific-review.md`, each with its own Confidence block. Full prompt templates (expand `${PROGRAM_PATH}` and `${RUN_DIR}` before passing):

> `$J3_ARCH_PROMPT` template externalized — load and follow protocol below § J3_ARCH_PROMPT (one load supplies both J3 templates).

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _RESEARCH_SHARED < "${TMPDIR:-/tmp}/research-shared-${CSID}" 2>/dev/null || _RESEARCH_SHARED=""  # warm read (Check 41)
cat "$_RESEARCH_SHARED/judge-j3-prompts.md"
```

> **Substitution requirement**: every `${RUN_DIR}` and `${PROGRAM_PATH}` token in template above MUST be replaced with concrete bash-expanded value (e.g. `.experiments/judge-2026-05-13T10-00-00Z`) before string passed to `Agent(...)`. Passing literal `${RUN_DIR}` to agent causes agent to write to directory named `${RUN_DIR}`. Applies equally to any historical `<RUN_DIR>` angle-bracket notation in older copies — both forms are text-substitution placeholders, not bash interpolation the Agent runtime expands.

When `SPAWN_ARCHITECT=true` — after the merged Agent() returns, check `$RUN_DIR/methodology.md`: if missing or empty, set `methodology_rating = "timed_out"`, continue to J6; surface with ⏱ in report.

After the spawn returns (merged or scientist-only), check `$RUN_DIR/scientific-review.md`: if missing or empty, set `scientific_rating = "timed_out"`, continue to J6; surface with ⏱ in Scientific Rigor section.

Use `methodology_rating` from returned envelope for verdict computation in J6:

- `sound` → supports APPROVED
- `needs-refinement` → supports NEEDS-REVISION
- `fundamentally-flawed` → supports BLOCKED

When `SPAWN_ARCHITECT=false`: spawn `research:scientist` alone for scientific rigor. Expand `${PROGRAM_PATH}` and `${RUN_DIR}` before passing — construct `$J3_SCI_PROMPT` per the template (same variable substitution pattern). Spawn: `Agent(subagent_type="research:scientist", prompt=$J3_SCI_PROMPT)`. (When `SPAWN_ARCHITECT=true` the scientist dimension already ran inside the merged spawn above — do NOT spawn a second agent.)

> `$J3_SCI_PROMPT` template: `judge-j3-prompts.md` § J3_SCI_PROMPT (already loaded by J3_ARCH `cat` above).

Use `scientific_rating` as **advisory** in J6 report under **Scientific Rigor** — informs but doesn't override verdict. Exception: `scientific_rating == "fundamentally-flawed"` (exact match) elevates verdict to BLOCKED.

**Source precedence for `scientific_rating`** (mandatory when both present):

1. **File-parsed value** from `$RUN_DIR/scientific-review.md` (read after agent completes) — authoritative
2. **Health-monitor / envelope value** from agent's returned JSON — advisory only

File-parsed value takes priority over health monitor value; use file-parsed value when both present. Same precedence applies to `methodology_rating` parsed from `$RUN_DIR/methodology.md` vs envelope value. Use envelope value only when file missing or unparsable (e.g., timeout with no output).

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# boundary: after J3 agents complete, before J4 validation (compaction-contract.md)
IFS= read -r _RUN_DIR < "${TMPDIR:-/tmp}/judge-run-dir-${CSID}" 2>/dev/null || _RUN_DIR=""
IFS= read -r _PROG_PATH < "${TMPDIR:-/tmp}/judge-program-path-${CSID}" 2>/dev/null || _PROG_PATH=""
IFS= read -r _KEEP < "${TMPDIR:-/tmp}/judge-keep-items-${CSID}" 2>/dev/null || _KEEP=""
_KEEP_APPEND=""; [ -n "$_KEEP" ] && _KEEP_APPEND="; user-keep: $_KEEP"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/write_skill_contract.py" "research:judge" "validation-verdict (after J3 review agents complete)" "${_RUN_DIR}" "run-dir=${_RUN_DIR}, program=${_PROG_PATH}, methodology=${_RUN_DIR}/methodology.md, scientific-review=${_RUN_DIR}/scientific-review.md${_KEEP_APPEND}" "J4 local validation → J5 Codex review → J6 verdict and report"  # timeout: 5000
```

## Step J4: Local validation

> Skip if `$SKIP_VALIDATION` is `true` (parsed in J1). Print: `→ Validation skipped (--skip-validation passed)` and continue to J5. Add `high` finding: "Executability unverified — metric_cmd and guard_cmd not tested on local machine." This finding persists into J6 — APPROVED not achievable when `--skip-validation` set (high > 0 → NEEDS-REVISION at best).

Execute each command once. **Non-blocking** — failures become `critical` findings, not hard stops.

**Substitution invariant** — `metric_cmd` and `guard_cmd` fully resolved in J1. No `{...}` tokens should remain. If any `{field_name}` token still present, add `critical` finding: "Unresolved placeholder `{field_name}` in `<metric_cmd|guard_cmd>` — substitution failed in J1" and skip execution.

```bash
# Substitute ${metric_cmd} with the resolved command from J1 before execution
${metric_cmd} 2>&1  # timeout: 360000
```

Parse stdout for float. If found, record as `baseline_value`. If not found or non-zero exit: add critical finding: "Metric command failed or produced no numeric output".

```bash
# Substitute ${guard_cmd} with the resolved command from J1 before execution
${guard_cmd}  # timeout: 360000
```

If guard exits non-zero: add critical finding: "Guard command exited non-zero (exit <code>): \<first 3 lines of output>".

Record validation results for J6 report.

**Note**: J4 executes on current machine. For cross-machine workflows, pass `--skip-validation`.

## Step J5a: Codex adversarial review

**Complexity gate** — simple programs skip the Codex pass (J2 completeness, scientist review, and J4 dry-run still apply):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PROGRAM_PATH < "${TMPDIR:-/tmp}/judge-program-path-${CSID}" 2>/dev/null || PROGRAM_PATH=""  # re-hydrate (Check 41: fresh shell)
J5A_COMPLEX=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/detect-complexity.py" "$PROGRAM_PATH")  # timeout: 5000 — same gate as J3; must stay identical, hence the shared script
```

`J5A_COMPLEX=false` → print `note: simple program (single scope file, single-phase strategy) — Codex adversarial pass skipped by complexity gate`, record `codex: skipped (complexity gate)` in J6 summary, continue to J5b. `J5A_COMPLEX=true` → proceed below.

Check Codex availability. Distinguish two failure modes — CLI missing vs plugin missing:

```bash
if ! command -v claude >/dev/null 2>&1; then
    CODEX_STATUS="cli-missing"
elif [ "$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/check_bridge.py" --status 2>/dev/null || echo absent)" = "available" ]; then
    CODEX_STATUS="available"
else
    CODEX_STATUS="plugin-missing"
fi
```

**`CODEX_STATUS=available`**: invoke adversarial review on top 3 critical/high gaps from J2 and J3. Example (replace `<top finding N>` with actual findings):

```text
Skill(skill="bridge:review", args="Read-only adversarial review of run program. Check <top finding 1>, <top finding 2>, and <top finding 3> in program.md; do not apply fixes.")
```

Incorporate Codex findings into overall findings list with `source: "codex"`.

**`CODEX_STATUS=plugin-missing`**: print one line and continue:

```text
note: bridge@borda-ai-rig not installed or disabled — skipping adversarial review (Claude-only judge)
```

**`CODEX_STATUS=cli-missing`**: print diagnostic and continue (distinguish from plugin-absent so user with Codex installed but `claude` CLI not in PATH isn't silently denied review):

```text
note: `claude` CLI not in PATH — Codex availability cannot be verified; skipping adversarial review. To enable: ensure `claude` binary is on PATH and Codex plugin installed.
```

## Step J5b: Resolve rating source

Apply rating source precedence before J6 verdict computation — fixes ambiguity when envelope and file-parsed ratings disagree. For `methodology_rating`:

0. If `SPAWN_ARCHITECT=false` (complexity gate didn't fire): set `methodology_rating="sound"` directly — no file or envelope; log `→ methodology_rating: sound (architect gate skipped — narrow scope)`. Skip steps 1–2 for methodology_rating.
1. If `$RUN_DIR/methodology.md` present AND parsable, use file-parsed value — authoritative.
2. Else use envelope value from agent's returned JSON — fallback.
3. Log source used: print `→ methodology_rating source: file | envelope`.

For `scientific_rating`:

1. If `$RUN_DIR/scientific-review.md` present AND parsable, use file-parsed value — authoritative.
2. Else use envelope value from the agent's returned JSON — fallback.
3. Log source used: print `→ scientific_rating source: file | envelope`.

Resolved rating values feed directly into J6 verdict table — no further source disambiguation in J6.

## Step J6: Verdict and report

<!-- Confidence score-bands are owned by rules/quality-gates.md (Confidence Block) — do not restate the numeric threshold here. The verdict table below keys on severity (critical/high) and *_rating strings only; it does not consume a numeric confidence threshold. -->

**Verdict computation** (deterministic — design soundness, not outcome prediction):

Top-to-bottom; **first match wins**. BLOCKED takes precedence — stop at first match.

| Condition | Verdict |
| -- | -- |
| any critical (J2) — exact `critical` severity match | BLOCKED |
| `methodology_rating == "fundamentally-flawed"` (exact string match, J3) | BLOCKED |
| `scientific_rating == "fundamentally-flawed"` (exact string match, J3) | BLOCKED |
| J3 agent timed out (`methodology_rating == "timed_out"` — exact match — or null; note: `timed_out` does **not** trigger BLOCKED — it falls to NEEDS-REVISION) | NEEDS-REVISION |
| `scientific_rating == "timed_out"` (exact match — scientist review did not complete; adversarial review absent → APPROVED not safe) | NEEDS-REVISION |
| 0 critical AND (high > 0 OR `methodology_rating == "needs-refinement"`) | NEEDS-REVISION |
| 0 critical AND 0 high AND `methodology_rating == "sound"` AND `scientific_rating != "timed_out"` | APPROVED |

**Verdict matching rules**: all `*_rating` comparisons require exact string match. Reject partial/substring matches — e.g., `timed_out_partial` does NOT match `timed_out`; `flawed` does NOT match `fundamentally-flawed`. Use `==` equality only; never `=~`, `startswith`, or pattern matching.

**Goodhart consolidation rule**: Goodhart's Law findings surface via two paths — J2 C2b (static, produces `critical` finding) and J3 agents (dynamic review, produces `methodology_rating` or `scientific_rating`). Before applying verdict table: if J3 architect or scientist explicitly flags Goodhart's Law as issue AND J2 didn't already flag it `critical`, promote to `critical` finding in J2 list (source: "J3-Goodhart"). Ensures both paths produce BLOCKED for Goodhart issues. Architect and scientist prompts already instruct `fundamentally-flawed` for Goodhart — this consolidation handles edge cases where rating falls below `fundamentally-flawed` but Goodhart still mentioned.

**Pre-compute**:

```bash
BRANCH=$(git branch --show-current 2>/dev/null | tr '/' '-' || echo 'main')  # timeout: 3000
```

**Write full report** (never overwrite — use counter loop):

```bash
mkdir -p .reports/research  # timeout: 3000
BRANCH=$(git branch --show-current 2>/dev/null | tr '/' '-' || echo 'main')  # timeout: 3000  # re-derive: separate bash block
BASE=".reports/research/judge-$BRANCH-$(date +%Y-%m-%d).md"
OUT="$BASE"; COUNT=2
while [ -f "$OUT" ]; do OUT="${BASE%.md}-${COUNT}.md"; ((COUNT++)); done
```

```markdown
---
Title:         Judge — [program_title]
Date:          [YYYY-MM-DD]
Scope:         [path to program.md]
Focus:         experimental protocol validation
Agents:        foundry:solution-architect (J3), research:scientist (J3)  ← include "foundry:solution-architect (J3), " only when SPAWN_ARCHITECT=true (J3 §Complexity gate); when false, use "research:scientist (J3); architect: skipped (narrow scope)" instead
Outcome:       APPROVED | NEEDS-REVISION | BLOCKED
Methodology:   sound | needs-refinement | fundamentally-flawed
Findings:      [N] critical · [N] high · [N] medium · [N] low
Protocol gaps: [N]
Confidence:    [score] — [key gaps]
Next steps:    /research:run <path>  [APPROVED] | fix protocol, re-run /research:judge  [otherwise]
Path:          → .reports/research/judge-<branch>-<date>.md
---

## Judge Report: <program_title>

**Program**: <path to program.md>
**Date**: <date>
**Verdict**: APPROVED | NEEDS-REVISION | BLOCKED

### Completeness Audit
| ID | Check | Status | Severity | Detail |
|----|-------|--------|----------|--------|

### Methodology Review
**Rating**: sound | needs-refinement | fundamentally-flawed | timed-out
Read full review: <RUN_DIR>/methodology.md

- Hypothesis clarity: <one-line finding>
- Measurement validity: <one-line finding>
- Control adequacy: <one-line finding>
- Experimental scope: <one-line finding>
- Protocol consistency: <one-line finding>
- Stopping criteria: <one-line finding>
- Reproducibility: <one-line finding>

**Protocol gaps** (specific improvements to program.md):
1. <gap>
2. <gap>

### Scientific Rigor (advisory)
**Rating**: sound | needs-refinement | fundamentally-flawed | timed-out
Read full review: `<RUN_DIR>/scientific-review.md`

- Hypothesis falsifiability: <one-line finding>
- Goodhart's Law risk: <one-line finding>
- Missing baselines: <one-line finding>
- Reproducibility risks: <one-line finding>

### Dry-Run Results
| Command | Status | Output |
|---------|--------|--------|
| metric_cmd | pass/fail | <baseline value or first error line> |
| guard_cmd | pass/fail | exit 0 or exit N: <first error line> |

(Skipped — `--skip-validation`) [if applicable]

### Codex Review
<findings from Codex adversarial review, annotated with source: "codex">
(Skipped — codex plugin not installed) [if unavailable]

### Required Changes
<ordered list of specific fixes for each non-pass finding, critical first; include exact edits to program.md>

### Supervisor Decision
[APPROVED] Experimental protocol is sound. Proceed: `/research:run <path>`
[NEEDS-REVISION] Refine the protocol (see Required Changes above), then re-submit: `/research:judge <path>`
[BLOCKED] Fundamental design flaw — the experiment as designed cannot produce valid results. Fix items 1-N before proceeding.

## Confidence
**Score**: 0.N — [high|moderate|low]
**Gaps**:
- [specific limitation]
```

**Terminal summary** (compact):

```text
---
Judge — <program_title>
Verdict:      APPROVED | NEEDS-REVISION | BLOCKED
Methodology:  sound | needs-refinement | fundamentally-flawed
Scientific:   sound | needs-refinement | fundamentally-flawed | timed-out  (advisory)
Findings:     <N> critical · <N> high · <N> medium · <N> low
Protocol gaps: <N>
Validation:   metric=<value> guard=pass|fail  (or "skipped — --skip-validation")
Codex:        reviewed | skipped
→ saved to .reports/research/judge-<branch>-<date>.md
---
Next: /research:run <path>                         [APPROVED]
Next: fix protocol, re-run /research:judge <path>      [NEEDS-REVISION or BLOCKED]
```

```bash
rm -f .temp/state/skill-contract.md  # clear contract — judge verdict complete (compaction-contract.md §Lifecycle)  # timeout: 5000
```

</workflow>

<notes>

- Judge read-only — never modifies code, commits, or writes to `.experiments/state/`
- `.experiments/judge-<timestamp>/` stores methodology agent's full output
- Validation executes on current machine — use `--skip-validation` for cross-machine workflows
- Verdict deterministic (finding counts + methodology_rating); not inferred from prose
- Re-run judge after editing `program.md` to confirm fixes
- Judge run dirs don't write `result.jsonl` — exempt from automated 30-day TTL cleanup (per `.claude/rules/foundry-artifact-lifecycle.md` TTL policy — no `result.jsonl` = cleanup skipped); remove manually (`rm -rf .experiments/judge-*/`)
- **Calibration scope**: J1–J2 sub-steps only — synthetic result file with known verdict (APPROVED/NEEDS-REVISION/BLOCKED) and injected finding counts; score whether judge correctly identifies verdict and extracts counts. Full J3 validation execution loop excluded — needs live git state and executable metric commands. See `/foundry:calibrate` skills mode domain table for path resolution.

</notes>
