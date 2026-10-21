---
name: judge
description: Research-supervisor review of program.md — validates experimental methodology (hypothesis clarity, measurement validity, control adequacy, scope, strategy fit), emits APPROVED / NEEDS-REVISION / BLOCKED verdict before expensive run loop.
argument-hint: '[<program.md>] [--skip-validation] [--keep "<items>"]'
effort: medium
allowed-tools: Read, Write, Bash, Glob, Agent, Skill, TaskCreate, TaskUpdate, AskUserQuestion
disable-model-invocation: true
---

<objective>

Research-supervisor review of `program.md` — validates experimental methodology, emits APPROVED / NEEDS-REVISION / BLOCKED verdict before expensive run loop. Read-only apart from its own run dir and report; J4 executes the program's own metric/guard commands, so working-tree drift is checked and reported.

NOT for: running experiments (use `/research:run`); designing hypotheses (use `research:scientist` agent); config quality (`/foundry:audit` (requires `foundry` plugin)). </objective>

<compaction>

- Key boundary: end of J3 — methodology and scientific review agents complete, output files written; before J4 validation and J6 verdict.
- Preserve: RUN_DIR (TMPDIR key), PROGRAM_PATH (TMPDIR key), methodology.md path, scientific-review.md path, SKIP_VALIDATION flag, BRANCH.
- Clear at J1 start (stale prior run) and at end of J6 after terminal summary printed.

</compaction>

<workflow>

<!-- Agent resolution: see _RESEARCH_SHARED/agent-resolution.md -->

## Agent Resolution

**Agent resolution**: load and follow the protocol below. Agent this skill dispatches: `research:scientist` (same plugin — always present while this skill runs, so no fallback row).

```bash
# loads: compaction-contract.md
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/resolve_shared.py" --persist research-shared --emit agent-resolution.md || exit 1  # timeout: 5000 — cold resolve; every later site reads the research-shared sentinel instead of re-running python
```

## Judge Mode (Steps J1–J6)

Triggered by `judge` or `judge <file.md>`.

**Task tracking**: create tasks for J1, J2, J3, J4, J5a, J5b, J6 at start, all in the same response as the first real tool call. Every later `TaskUpdate` rides with the next real tool call — never a bookkeeping-only turn; the one standalone call is a `completed` right before a long report. (J5a = Codex adversarial review; J5b = resolve rating source.)

## Step J1: Locate and parse program.md

**Flag parsing** (first):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
[ -f "${TMPDIR:-/tmp}/sweep-out-path-${CSID}" ] && _INL=--inlined || _INL=""  # sweep S1 sentinel present = inlined in sweep: sweep's live contract must survive
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/parse-skill-flags.py" --flags skip-validation --keep-state judge $_INL "$ARGUMENTS" >/dev/null  # timeout: 5000 — no eval; judge-{skip-validation,clean-args,keep-items}-${CSID} (J3 note block reads skip-validation in a fresh shell), clears a stale contract (not when --inlined)
IFS= read -r SKIP_VALIDATION < "${TMPDIR:-/tmp}/judge-skip-validation-${CSID}" 2>/dev/null || SKIP_VALIDATION=false
IFS= read -r ARGUMENTS < "${TMPDIR:-/tmp}/judge-clean-args-${CSID}" 2>/dev/null || ARGUMENTS=""
echo "SKIP_VALIDATION=${SKIP_VALIDATION:-false}"
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

**Schema gate** — before parsing, count matching `## <Section>` headings against the known schema set (`Goal`, `Metric`, `Guard`, `Config`, `Notes`). Zero matches = located file doesn't parse as program.md (e.g. input resolution located an unrelated document) — stop immediately with the same "nothing found" error shown in step 4 above; do NOT proceed to J2's C1–C13 checklist. Distinct from step 4 (file not locatable at all): this fires when a file IS located but content doesn't match expected schema.

> 1-2 matching headings is a partial match, not a gap here — deferred to J2's C1-C4 criticals (missing Goal / Metric.command / Metric.direction / Guard.command → BLOCKED). A non-program file is reported as a flawed program, by design, not treated as unparsable input.

**Persist program path** — once the schema gate passes, substitute the resolved program.md path into the block below. It aborts when the path is left unsubstituted or is not a file; J3 and J5a re-hydrate it (fresh shell per call):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
PROGRAM_FILE="<resolved program.md path>"
case "$PROGRAM_FILE" in *'<'*'>'*|"") echo "! BLOCKED — program path not substituted in J1"; exit 1;; esac
[ -f "$PROGRAM_FILE" ] || { echo "! BLOCKED — program file not found: $PROGRAM_FILE"; exit 1; }
echo "$PROGRAM_FILE" > "${TMPDIR:-/tmp}/judge-program-file-${CSID}"  # J3 pre-spawn block reads it back
```

**Parsing** — find `## <Section>` headings in program.md, extract first fenced code block per section, parse as `key: value` lines, warn on unrecognized keys. `--skip-validation` and `colab_hw` judge-specific, extracted independently.

**No placeholder substitution** — `metric_cmd` and `guard_cmd` are validated exactly as written: run executes them literally (R1 resolves no `{field_name}` tokens), so substituting here would approve a command run never executes. A `{field_name}` token naming a `## Config` field is flagged at J2. No `clarification_prompt` in judge.

Extract `<program_title>` from `# Program: <title>` line for reports (fallback `# Campaign: <title>` for legacy files).

## Step J2: Completeness audit

Check C1–C13 plus C2b, C6b and C9b (16 items total). Produce findings list with severity. Each finding has: `id`, `check`, `status` (pass/fail/warn), `severity`, `detail`.

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
| C9b | `## Config` `metric_repeats` (integer 1–10) and `min_delta_pct` (number ≥ 0) valid when present; advisory when `agent_strategy` is literally `perf` or `ml` and `metric_repeats` is absent or 1 | low | Invalid value: run warns and uses the default (1 / 0.1). Advisory detail: `"single-sample keep rule — run keeps any measured gain; a timing or stochastic metric needs metric_repeats ≥ 3 (metric cost × metric_repeats)"`. Repeats are an opt-in cost the user decides, so the advisory is listed under findings, never under Required Changes. |
| C10 | `compute` is valid (`local`/`colab`/`docker`) | low | Invalid defaults to `local` |
| C11 | `colab_hw` valid (if present) | low | `colab_hw` absent OR is one of `H100, L4, T4, A100` — fail detail: `"colab_hw '<value>' is not in known set {H100, L4, T4, A100} — run mode will warn and null this value to the default GPU"`. Note: this check is a minimum-capability floor — new Colab hardware tiers may exist beyond this list; unknown values are flagged for user verification, not blocked. <!-- policy-sibling: plugins/cc_research/skills/run/SKILL.md, plugins/cc_research/skills/run/modes/colab-setup.md, plugins/cc_research/skills/plan/SKILL.md, plugins/cc_research/skills/sweep/SKILL.md — canonical colab_hw known-set {H100, L4, T4, A100}; keep judge's C11 set in sync with these four. --> |
| C12 | `## Notes` section present | low | Notes optional but improve ideation quality |
| C13 | No `scope_files` entry covers an evaluator file — an existing *file* named by the `## Metric`/`## Guard` command (directories such as `tests/` are not evaluators) | high | run Phase 3 flags any iteration touching an evaluator file as `scope-violation`, so overlapping scope lets the agent edit the evaluator and wastes iterations; narrow `scope_files` to omit it. If the command names no resolvable file, `pass` |

**Scope adequacy sub-rule (C6b)** — after C6 passes, assess whether `scope_files` is *sufficient* for stated goal. If goal type implies known bottleneck locations outside declared scope, add `medium` finding:

- Test-speed goal + scope limited to `tests/` only → flag: "conftest.py, fixtures, and test infrastructure outside tests/ are common levers for test runtime; scope may be too narrow"
- Throughput/latency goal + scope limited to single-layer path (e.g., `src/serving/`) → flag: "serving bottlenecks often span middleware, connection pooling, or database layers outside declared scope"
- Any goal where the stated scope excludes a widely-known dependency class → emit medium finding with location `## Config / scope_files`, suggested broader pattern as fix

Distinct from C6 (path existence) — C6b fires even when path exists but is likely insufficient.

**Severity summary**: count findings per severity. Any critical finding = verdict cannot be APPROVED. **Enumeration rule**: check ALL items (C1–C13 plus C2b, C6b and C9b) before stopping — don't short-circuit after first critical issue. program.md can have multiple independent flaws across severity levels; Required Changes section must list all, not just verdict-determining one — except the C9b advisory (see its row).

**Placeholder token check (C2, C4 sub-rule)** — after confirming `command` present in `## Metric` (C2) and `## Guard` (C4), scan each command for `{...}` tokens. A token naming a `## Config` field = `high` finding: run executes commands literally and never substitutes it — inline the value in program.md. Other braces (shell `${VAR}`, brace expansion, awk/jq bodies) are not tokens.

**Goodhart's Law check (C2b)** — after confirming metric `command` present (C2 passes), assess whether command operationalizes stated `## Goal` or measures proxy. If metric could improve while actual goal NOT achieved, add `critical` finding:

- metric measures test pass rate but goal is latency reduction → critical: "metric is a correctness proxy, not a latency measure"
- metric measures lint error count but goal is bug density reduction → critical: "pylint score is a gameable proxy; agent can suppress warnings without improving actual quality"
- metric measures a format/style score but goal is functional improvement → critical: "metric does not operationalize the stated goal"

Goodhart findings are `critical` (not just methodology notes) — broken metric invalidates entire feedback loop, equivalent impact to C2 (missing command).

**Command feasibility**: J2 validates command fields statically (presence, format). Executability deferred to J4. If `$SKIP_VALIDATION` is `true`, J4 skipped, commands unverified — report as "validation skipped — commands unverified."

## Step J3: Methodology review

The J3 pre-spawn block pre-computes the run dir, program path, validation note and complexity gate, then prints the prompt templates — no decision sits between them, so they ride one call. Expand every printed value into concrete paths before constructing prompts — never pass literal `<path_to_program.md>` or `<RUN_DIR>` placeholders to agents; the `${PROGRAM_PATH}` / `${RUN_DIR}` tokens in the templates are substituted from the `PROGRAM_PATH=… RUN_DIR=…` line:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
RUN_DIR=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/make_run_dir.py" "judge" ".experiments" 2>/dev/null)  # timeout: 5000
[ -n "$RUN_DIR" ] || { echo "! BLOCKED — make_run_dir.py returned empty; check research plugin install"; exit 1; }
echo "$RUN_DIR" > "${TMPDIR:-/tmp}/judge-run-dir-${CSID}"  # persist for J3 block (Check 41)
IFS= read -r PROGRAM_FILE < "${TMPDIR:-/tmp}/judge-program-file-${CSID}" 2>/dev/null || PROGRAM_FILE=""  # J1 persist block (fresh shell per call)
[ -n "$PROGRAM_FILE" ] || { echo "! BLOCKED — judge-program-file sentinel missing; run the J1 persist-program-path block first"; exit 1; }
PROGRAM_PATH=$(realpath "$PROGRAM_FILE" 2>/dev/null || echo "$PROGRAM_FILE")
echo "$PROGRAM_PATH" > "${TMPDIR:-/tmp}/judge-program-path-${CSID}"  # persist for J3 gate and J5a gate (Check 41)
echo "PROGRAM_PATH=$PROGRAM_PATH RUN_DIR=$RUN_DIR"  # values must reach the transcript: prose substitutes them into prompts and paths
IFS= read -r SKIP_VALIDATION < "${TMPDIR:-/tmp}/judge-skip-validation-${CSID}" 2>/dev/null || SKIP_VALIDATION="false"
if [ "${SKIP_VALIDATION:-false}" = "true" ]; then
    SKIP_VALIDATION_NOTE="Local validation skipped via --skip-validation — do NOT assess executability of metric_cmd/guard_cmd; note this limitation in your review."
else
    SKIP_VALIDATION_NOTE="Local validation will run after this review (J4)."
fi
echo "SKIP_VALIDATION_NOTE: $SKIP_VALIDATION_NOTE"
SPAWN_ARCHITECT=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/detect-complexity.py" "$PROGRAM_PATH")  # timeout: 5000
case "$SPAWN_ARCHITECT" in true|false) ;; *) SPAWN_ARCHITECT=true;; esac  # fail closed: any other output runs the full review
echo "SPAWN_ARCHITECT=$SPAWN_ARCHITECT"
IFS= read -r _RESEARCH_SHARED < "${TMPDIR:-/tmp}/research-shared-${CSID}" 2>/dev/null || _RESEARCH_SHARED=""  # warm read (Check 41)
cat "$_RESEARCH_SHARED/judge-j3-prompts.md"
```

**Complexity gate** (`SPAWN_ARCHITECT` above) — mirrors P-P2b; skips the architect for narrow single-scope experiments (saves a full opus pass). `SKIP_VALIDATION_NOTE` goes into the prompt.

**Spawn note**: J3 agents run in the background — issue the batch, then end the turn; no filler call, no "waiting" line, no sleep (CLAUDE.md §6). Timeout handled post-hoc — on each completion notification, check that agent's output file; if missing/empty mark it timed out (⏱). See J3 post-call checks below.

Dispatch — scientist dimension always; architect dimension only when the complexity gate fires. When BOTH dimensions are active, J3 is ONE merged spawn covering both (two opus spawns for a single-file review pay 2× ~120,851 tok fixed overhead for a workload far under the §Agent budget breakeven below); when only the scientist dimension is active, it's a single scientist spawn as before. Per-dimension output files and Confidence blocks are unchanged either way.

> Merge is competence-safe: J3_ARCH_PROMPT is a self-contained research-supervisor checklist (7 dimensions, explicit pass/fail criteria) — not a request for a second agent's own domain judgment. Independent Goodhart coverage still comes from J2 C2b, not from a second spawn.

When `SPAWN_ARCHITECT=false`: skip architect spawn; J5b precedence step 0 sets `methodology_rating="sound"` (scientist review still covers scientific rigor); record `architect: skipped (narrow scope)` in J6 summary.

<!-- policy-sibling: plugins/cc_research/skills/fortify/SKILL.md, plugins/cc_research/skills/verify/SKILL.md, plugins/cc_research/skills/plan/SKILL.md, plugins/cc_research/skills/topic/SKILL.md, plugins/cc_research/skills/retro/SKILL.md, plugins/cc_research/skills/topic/modes/plan.md, plugins/cc_research/skills/topic/modes/team.md — Agent-budget spawn-envelope rule restated in each; keep in sync (plugins/CLAUDE.md §Policy Duplication Marker). -->

> **Agent budget** — every spawn prompt must require an envelope even on exhaustion — `partial: true` plus what was finished.

When `SPAWN_ARCHITECT=true`: spawn ONE merged reviewer via `Agent(subagent_type="research:scientist", prompt=<merged prompt>)` (opus) — build the merged prompt exactly per `judge-j3-prompts.md` §Merged spawn — envelope override (both templates with individual `Return ONLY:` lines stripped, joined by `---`, closed with the override's single array-envelope instruction). The single spawn executes BOTH templates in order, writes `${RUN_DIR}/methodology.md` AND `${RUN_DIR}/scientific-review.md`, each with its own Confidence block. Full prompt templates (expand `${PROGRAM_PATH}` and `${RUN_DIR}` before passing):

> `$J3_ARCH_PROMPT` template externalized — follow `judge-j3-prompts.md` § J3_ARCH_PROMPT, printed by the J3 pre-spawn block above (one load supplies both J3 templates).

> **Substitution requirement**: every `${RUN_DIR}` and `${PROGRAM_PATH}` token in template above MUST be replaced with concrete bash-expanded value (e.g. `.experiments/judge-2026-05-13T10-00-00Z`) before string passed to `Agent(...)`. Passing literal `${RUN_DIR}` to agent causes agent to write to directory named `${RUN_DIR}`.

When `SPAWN_ARCHITECT=true` — after the merged Agent() returns, check `$RUN_DIR/methodology.md`: if missing or empty, set `methodology_rating = "timed_out"`, continue to J6; surface with ⏱ in report (J6 reports methodology as unreviewed, not NEEDS-REVISION, when the scientist review completed).

After the spawn returns (merged or scientist-only), check `$RUN_DIR/scientific-review.md`: if missing or empty, set `scientific_rating = "timed_out"`, continue to J6; surface with ⏱ in Scientific Rigor section.

Use `methodology_rating` from returned envelope for verdict computation in J6:

- `sound` → supports APPROVED
- `needs-refinement` → supports NEEDS-REVISION
- `fundamentally-flawed` → supports BLOCKED

When `SPAWN_ARCHITECT=false`: spawn `research:scientist` alone for scientific rigor. Expand `${PROGRAM_PATH}` and `${RUN_DIR}` before passing — construct `$J3_SCI_PROMPT` per the template (same variable substitution pattern). Spawn: `Agent(subagent_type="research:scientist", prompt=$J3_SCI_PROMPT)`. (When `SPAWN_ARCHITECT=true` the scientist dimension already ran inside the merged spawn above — do NOT spawn a second agent.)

> `$J3_SCI_PROMPT` template: `judge-j3-prompts.md` § J3_SCI_PROMPT (already printed by the J3 pre-spawn block).

Use `scientific_rating` as **advisory** in J6 report under **Scientific Rigor** — `needs-refinement` informs but doesn't change the verdict. Two exact-match exceptions: `fundamentally-flawed` elevates verdict to BLOCKED; `timed_out` (review never completed) holds verdict at NEEDS-REVISION.

**Source precedence** for `scientific_rating` and `methodology_rating` (file-parsed authoritative, envelope fallback only when file missing or unparsable): mandatory — resolved in J5b.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# boundary: after J3 agents complete, before J4 validation (compaction-contract.md)
[ -f "${TMPDIR:-/tmp}/sweep-out-path-${CSID}" ] && { echo "judge boundary: skipped — inlined in sweep, whose contract stays live"; exit 0; }  # judge write would replace sweep's contract identity and keep-items
IFS= read -r _RUN_DIR < "${TMPDIR:-/tmp}/judge-run-dir-${CSID}" 2>/dev/null || _RUN_DIR=""
IFS= read -r _PROG_PATH < "${TMPDIR:-/tmp}/judge-program-path-${CSID}" 2>/dev/null || _PROG_PATH=""
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/write_skill_contract.py" --keep-sentinel judge-keep-items "research:judge" "validation-verdict (after J3 review agents complete)" "${_RUN_DIR}" "run-dir=${_RUN_DIR}, program=${_PROG_PATH}, methodology=${_RUN_DIR}/methodology.md, scientific-review=${_RUN_DIR}/scientific-review.md" "J4 local validation → J5 Codex review → J6 verdict and report" -- calls_ahead=25 "drop=J3 review prompts and agent output"  # timeout: 5000
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/compaction_boundary.py"  # timeout: 60000 — prints the Boundary step, then the foundry gate decision; follow it
```

## Step J4: Local validation

> Skip if `$SKIP_VALIDATION` is `true` (parsed in J1). Print: `→ Validation skipped (--skip-validation passed)` and continue to J5. Add `high` finding: "Executability unverified — metric_cmd and guard_cmd not tested on local machine." This finding persists into J6 — APPROVED not achievable when `--skip-validation` set (high > 0 → NEEDS-REVISION at best).

> Skip also when `compute` in `## Config` is `colab`: the commands target the Colab runtime and cannot run here. Print `→ Validation skipped (compute: colab — commands run only on the Colab runtime)`, continue to J5, add a `medium` finding "Executability unverified — compute: colab; first Colab run is the first test". Medium (not `high`) keeps a remote-compute program APPROVED-able; `compute: docker` is still validated on this machine.

Execute each command once. **Non-blocking** — failures become `critical` findings, not hard stops.

**Literal-command invariant** — `metric_cmd` and `guard_cmd` run exactly as written in program.md, as run will execute them. A `{field_name}` token naming a Config field was already flagged `high` at J2: skip executing that command.

Snapshot the working tree first — the commands are the program's own and may write files:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/judge-run-dir-${CSID}" 2>/dev/null || RUN_DIR=""
[ -n "$RUN_DIR" ] || { echo "! BLOCKED — judge-run-dir sentinel missing; J3 run dir not persisted"; exit 1; }
git status --porcelain > "$RUN_DIR/porcelain-before.txt"  # timeout: 10000
```

Fill both command lines below with the program's command text. Each aborts when the token survives unsubstituted — an empty guard would otherwise exit 0 and pass. Write embedded single quotes as `'\''`:

```bash
case '${metric_cmd}' in '${'*|'') echo "! BLOCKED — metric_cmd not substituted"; exit 1;; esac
${metric_cmd} 2>&1  # timeout: 360000
```

Parse stdout for float. If found, record as `baseline_value`. If not found or non-zero exit: add critical finding: "Metric command failed or produced no numeric output".

```bash
case '${guard_cmd}' in '${'*|'') echo "! BLOCKED — guard_cmd not substituted"; exit 1;; esac
${guard_cmd}  # timeout: 360000
```

If guard exits non-zero: add critical finding: "Guard command exited non-zero (exit <code>): \<first 3 lines of output>".

Drift check — a changed tree means the metric/guard are not read-only; add a `medium` finding naming the diff:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/judge-run-dir-${CSID}" 2>/dev/null || RUN_DIR=""
[ -n "$RUN_DIR" ] || { echo "! BLOCKED — judge-run-dir sentinel missing; J3 run dir not persisted"; exit 1; }
git status --porcelain | diff - "$RUN_DIR/porcelain-before.txt" || echo "⚠ J4 changed the working tree (diff above)"  # timeout: 10000
```

Record validation results for J6 report.

**Note**: J4 executes on current machine. For cross-machine workflows, pass `--skip-validation`.

## Step J5a: Codex adversarial review

**Complexity gate** — simple programs skip the Codex pass (J2 completeness, scientist review, and J4 dry-run still apply):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PROGRAM_PATH < "${TMPDIR:-/tmp}/judge-program-path-${CSID}" 2>/dev/null || PROGRAM_PATH=""  # re-hydrate (Check 41: fresh shell)
[ -n "$PROGRAM_PATH" ] || { echo "! BLOCKED — judge-program-path sentinel missing; run the J3 pre-spawn block first"; exit 1; }
J5A_COMPLEX=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/detect-complexity.py" "$PROGRAM_PATH")  # timeout: 5000 — same gate as J3; must stay identical, hence the shared script
case "$J5A_COMPLEX" in true|false) ;; *) J5A_COMPLEX=true;; esac  # fail closed: any other output runs the Codex pass
echo "J5A_COMPLEX=$J5A_COMPLEX"
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
echo "CODEX_STATUS=$CODEX_STATUS"
```

**`CODEX_STATUS=available`**: invoke adversarial review on top 3 critical/high gaps from J2 and J3. Example (replace `<top finding N>` with actual findings):

```text
Skill(skill="bridge:review", args="Read-only adversarial review of run program. Check <top finding 1>, <top finding 2>, and <top finding 3> in program.md; do not apply fixes.")
```

Incorporate Codex findings into overall findings list with `source: "codex"`; a `critical` Codex finding counts as critical in the J6 verdict table.

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

<!-- Confidence score-bands are owned by rules/quality-gates.md (Confidence Block) — do not restate a numeric threshold here; the verdict table keys on severity and *_rating strings only. -->

**Verdict computation** (deterministic — design soundness, not outcome prediction):

Top-to-bottom; **first match wins**. BLOCKED takes precedence — stop at first match.

| Condition | Verdict |
| -- | -- |
| any critical (J2 audit, J4 validation, or J5a Codex) — exact `critical` severity match | BLOCKED |
| `methodology_rating == "fundamentally-flawed"` (exact string match, J3) | BLOCKED |
| `scientific_rating == "fundamentally-flawed"` (exact string match, J3) | BLOCKED |
| `scientific_rating == "timed_out"` (exact match — scientist review did not complete; adversarial review absent → APPROVED not safe) | NEEDS-REVISION |
| 0 critical AND (high > 0 OR `methodology_rating == "needs-refinement"`) | NEEDS-REVISION |
| 0 critical AND 0 high AND `methodology_rating` in {`sound`, `timed_out`, null} AND `scientific_rating != "timed_out"` | APPROVED — when methodology was `timed_out` or null, report it as **unreviewed ⏱** in the verdict line and Methodology section; the scientist review still ran, so the missing methodology pass is disclosed rather than forced into NEEDS-REVISION (a retry would time out the same way) |

**Verdict matching rules**: all `*_rating` comparisons require exact string match. Reject partial/substring matches — e.g., `timed_out_partial` does NOT match `timed_out`; `flawed` does NOT match `fundamentally-flawed`. Use `==` equality only; never `=~`, `startswith`, or pattern matching.

**Goodhart consolidation rule**: Goodhart's Law findings surface via two paths — J2 C2b (static, produces `critical` finding) and J3 agents (dynamic review, produces `methodology_rating` or `scientific_rating`). Before applying verdict table: if J3 architect or scientist explicitly flags Goodhart's Law as issue AND J2 didn't already flag it `critical`, promote to `critical` finding in J2 list (source: "J3-Goodhart"). Ensures both paths produce BLOCKED for Goodhart issues. Architect/scientist prompts already instruct `fundamentally-flawed` for Goodhart — this consolidation handles edge cases where rating falls below `fundamentally-flawed` but Goodhart still mentioned.

**Write full report** (never overwrite — the helper appends a counter suffix when the file exists):

```bash
mkdir -p .reports/research  # timeout: 3000
BRANCH=$(git branch --show-current 2>/dev/null | tr '/' '-' || echo 'main')  # timeout: 3000
OUT=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/resolve-anti-overwrite-path.py" .reports/research "judge-$BRANCH-$(date +%Y-%m-%d)")  # timeout: 5000
echo "OUT=$OUT"
```

**Resolve `Agents:` field** before filling the template below, per `SPAWN_ARCHITECT` (J3 §Complexity gate):

- `SPAWN_ARCHITECT=true` → use `research:scientist (J3, architect+scientist merged dimensions)`
- `SPAWN_ARCHITECT=false` → use `research:scientist (J3); architect: skipped (narrow scope)`

```markdown
---
Title:         Judge — [program_title]
Date:          [YYYY-MM-DD]
Scope:         [path to program.md]
Focus:         experimental protocol validation
Agents:        [resolved per J3 gate — see prose above]
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
**Score**: 0.NN — [high|moderate|low]
**Gaps**:
- (-0.NN) [specific limitation]
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
- Judge run dirs don't write `result.jsonl` — exempt from automated 30-day TTL cleanup (per `.claude/rules/foundry-artifact-lifecycle.md` TTL policy, installed via `/foundry:setup` — requires `foundry` plugin; no `result.jsonl` = cleanup skipped); remove manually (`rm -rf .experiments/judge-*/`) <!-- policy-sibling: plugins/cc_research/skills/fortify/SKILL.md, plugins/cc_research/skills/plan/SKILL.md, plugins/cc_research/skills/retro/SKILL.md, plugins/cc_research/skills/verify/SKILL.md — TTL-exemption note (no result.jsonl → skip 30-day cleanup) restated in each; keep in sync (plugins/CLAUDE.md §Policy Duplication Marker). -->
- **Calibration scope**: J1–J2 sub-steps only — synthetic result file with known verdict (APPROVED/NEEDS-REVISION/BLOCKED) and injected finding counts; score whether judge correctly identifies verdict and extracts counts. Full J3 validation execution loop excluded — needs live git state and executable metric commands. See `/foundry:calibrate` skills mode domain table for path resolution.

</notes>
