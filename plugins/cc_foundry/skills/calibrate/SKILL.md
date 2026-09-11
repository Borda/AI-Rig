---
name: calibrate
description: Calibration testing for agents and skills. Generates synthetic problems with known outcomes (quasi-ground-truth), runs targets against them, measures recall, precision, confidence calibration — reveals whether self-reported confidence scores track actual quality.
argument-hint: '[<scope>...] [--fast | --full] [--ab-test | --apply] [--skip-gate] [--local] [--keep "<items>"]'
effort: high
disable-model-invocation: true
allowed-tools: Read, Write, Bash, Agent, Glob, TaskCreate, TaskUpdate, TaskList, AskUserQuestion
---

<objective>

Validate agents and skills by measuring outputs against synthetic problems with defined ground truth. Primary signal: **calibration bias** — gap between self-reported confidence and actual recall. Well-calibrated agent reports 0.9 when it finds ~90% of issues. Miscalibrated: reports 0.9, finds 60%.

Calibration data drives improvement loop: systematic gaps → instruction updates; persistent overconfidence → adjusted re-run thresholds in MEMORY.md.

NOT for: static routing overlap analysis (use /foundry:audit); manually reviewing skill output quality (use /develop:review (requires `develop` plugin)).

</objective>

<inputs>

- **$ARGUMENTS**: parse `--flags` first, then resolve remaining tokens as scope targets

  **Flags** (order independent):

  - `--fast` — 3 problems per target (default when neither pace flag passed)
  - `--full` — 10 problems per target; mutually exclusive with `--fast`
  - `--ab-test` — also run `general-purpose` baseline and report delta metrics; requires benchmark (default `--fast` if no pace flag); mutually exclusive with `--apply`
  - `--apply` — apply proposals: with `--fast`/`--full`: run benchmark then immediately apply; without pace flag: skip benchmark, apply proposals from most recent past run; mutually exclusive with `--ab-test`
  - `--skip-gate` — suppress follow-up gate; for programmatic callers
  - `--local` — resolve target agent/skill files from source tree (`plugins/*/`) instead of installed plugin cache; for plugin-dev workflows where local edits aren't yet installed; sets `LOCAL_MODE=true` in all pipeline spawns

  **Mutual exclusion validation** (check before any work):

  - `--ab-test` + `--apply` together → hard error: "`--ab-test` and `--apply` are mutually exclusive. Pass one or neither."
  - `--fast` + `--full` together → hard error: "Pass `--fast` or `--full`, not both."
  - `--ab-test` without pace flag → default `--fast` silently (no error)

  **Unsupported flag check** — after all supported flags extracted (`--fast`, `--full`, `--ab-test`, `--apply`, `--skip-gate`, `--local`, `--keep`), scan `$ARGUMENTS` for remaining `--<token>` tokens. If found: print `` ! Unknown flag(s): `--<token>`. Supported: `--fast`, `--full`, `--ab-test`, `--apply`, `--skip-gate`, `--local`, `--keep`. `` then invoke `AskUserQuestion` — (a) **Abort** (stop, re-invoke with correct flags) · (b) **Continue ignoring** (skip unknown flags, proceed). On Abort: stop.

  **Legacy positional tokens** (`ab`, `apply`, `fast`, `full`) — **hard error**: print migration hint and stop. Example: "`ab` removed — use `--ab-test` flag: `/calibrate curator --ab-test`."

  **Scope tokens** (positional, space-separated — defaults to `all`):

  - `all` — all agents + relevant skills + routing + communication + all rules
  - `agents` — all agents only (full agent list in `modes/agents.md`)
  - `skills` — calibratable skills only (`/audit` and others per `modes/skills.md`; `/oss:review` (requires `oss` plugin) excluded — requires live GitHub PR)
  - `routing` — routing accuracy test: measures how accurately `general-purpose` orchestrator selects correct `subagent_type` for synthetic task prompts (not per-agent quality benchmark; included in `all`)
  - `communication` — handover + team protocol compliance: runs `foundry:curator` against synthetic agent responses and team transcripts with injected protocol violations (missing JSON envelope, missing `summary`, AgentSpeak v2 breaches); included in `all`
  - `rules` — rule adherence test: for each global rule file (no `paths:`) and each path-scoped rule when matching file is in context, generates synthetic tasks that should trigger rule's key directives, measures whether `general-purpose` agent with rule loaded correctly applies them; reports rules that are ignored, misapplied, or redundant; included in `all`
  - `plugins` — all agents + calibratable skills from all `plugins/*/` directories (union of all plugin-namespaced agents and calibratable skills)
  - `<plugin-name>` — **tier 2**: bare plugin directory name (e.g. `oss`, `foundry`, `research`, `develop`) auto-resolved when token matches `plugins/<name>/` directory; calibrates all agents + calibratable skills in that plugin
  - `<agent-name>` — **tier 3**: single agent (e.g., `foundry:sw-engineer`); also accepts bare name (e.g. `sw-engineer`) and resolves via `plugins/*/agents/<name>.md`
  - `/foundry:audit` — single skill (pass any calibratable skill name; `/oss:review` (requires `oss` plugin) accepted but excluded per `modes/skills.md`)
  - Multiple scope tokens — space-separated; calibrates union of resolved targets: `oss research`, `agents skills`, `curator shepherd`; each token resolved through same tier hierarchy as `/audit` scope tokens (reserved keywords first, then plugin-dir lookup, then agent/skill file search)

  Every invocation surfaces report: benchmark runs print new results; `--apply` without pace flag prints saved report from last run before applying.

</inputs>

<constants>

- FAST_N: 3 problems per target
- FULL_N: 10 problems per target
- RECALL_THRESHOLD: 0.70 (below → agent needs instruction improvement)
- CALIBRATION_BORDERLINE: ±0.10 (|bias| within this → calibrated; between 0.10 and 0.15 → borderline)
- CALIBRATION_WARN: ±0.15 (bias beyond this → confidence decoupled from quality)
- CALIBRATE_LOG: `.notes/logs/calibrations.jsonl` (legacy `.claude/logs/calibrations.jsonl` read-only fallback for historical entries)
- AB_ADVANTAGE_THRESHOLD: 0.10 (delta recall or F1 above this → meaningful advantage; below → marginal or none)
- PHASE_TIMEOUT_MIN: 5 (per-phase budget — if spawned subagents haven't all returned, collect partial results and continue)
- PIPELINE_TIMEOUT_MIN: 10 (hard cutoff — pipeline not notified within 10 min of launch is timed out; extendable if agent explains delay) # tighter than global 15-min cutoff from CLAUDE.md §6 — intentional for calibrate
- PIPELINE_BATCH_SIZE: 5 when one mode category runs alone, 2 while two categories are in flight (max agent/skill pipeline subagents spawned concurrently within one mode — prevents agent count explosion on `all`; batch: spawn ≤ that many, wait for all results, then spawn next batch; halving keeps peak concurrency at 4 ≤ 5 when paired)
- ROUTING_ACCURACY_THRESHOLD: 0.90 (below → agent descriptions need improvement) # keep in sync with modes/routing.md
- ROUTING_HARD_THRESHOLD: 0.80 (below → high-overlap pair descriptions need disambiguation)
- SPAWN_GATE_THRESHOLD: 50 (spawn estimate = target-count × N; above this, large-fan-out gate fires before Step 2 even when `--apply` is set — only `--skip-gate` bypasses)

<!-- Problem-set version 1.0 — bump when calibration problem set is refreshed (CODEX_PROBLEM_RATIO, CODEX_SCORER_WEIGHT, threshold defaults). Canonical source: this constants block + the per-mode problem fixtures under modes/*.md. Update version line below whenever any constant or fixture changes so historical calibrations.jsonl entries can be filtered by version. -->

- PROBLEM_SET_VERSION: 1.0
- CODEX_PROBLEM_RATIO: 0.6 (fraction of in-scope problems generated by Codex — agents/skills modes only)
- CODEX_SCORER_WEIGHT: 0.49 (Codex scorer weight; Claude = 0.51 — Claude has last word on disagreements)
- SCORER_AGREEMENT_WARN: 0.70 (scorer agreement below this → flag ambiguous ground truth ⚠)
- CODEX_MODES: ["agents", "skills"] (modes where Codex is active; routing/communication/rules excluded — test Claude-specific internals)
- PIPELINE_TIMEOUT_MIN_DUAL: 15 (hard cutoff when Codex active — replaces PIPELINE_TIMEOUT_MIN=10 for dual-source runs)

Domain tables per mode: see `modes/agents.md`, `modes/skills.md`, `modes/routing.md`, `modes/communication.md`, `modes/rules.md`.

</constants>

<compaction>
- Key boundary 1: after Step 2 pipeline fan-out (all mode pipelines spawned), before Step 3 collect+synthesize.
- Preserve at boundary 1: TIMESTAMP, run-dir (.reports/calibrate/<TIMESTAMP>/), target list, LOCAL_MODE.
- Terminal paths: end of Step 5 (no-apply path) and end of Step 6 (apply path).
</compaction>

<workflow>

**Task hygiene**: load and follow the protocol below.

```bash
# loads: compaction-contract.md
# audit-skip: resilience-replication — duplicated; plugin cannot self-locate
cat "$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/resolve_shared_path.py" foundry skills/_shared 2>/dev/null || echo "plugins/cc_foundry/skills/_shared")/task-hygiene.md"
```

**Task tracking**: create tasks at start of execution (Step 1) for each phase that will run:

- "Calibrate agents" — Step 2 (benchmark mode, when target includes agents)
- "Calibrate skills" — Step 2 (benchmark mode, when target includes skills)
- "Calibrate routing" — Step 2 (benchmark mode, when target includes routing)
- "Calibrate communication" — Step 2 (benchmark mode, when target includes communication)
- "Calibrate rules" — Step 2 (benchmark mode, when target includes rules)
- "Analyse and report" — Steps 3–5 (benchmark mode)
- "Apply findings" — Step 6 (apply mode only)

**Task marking discipline**: create ALL category tasks as `pending` at the start (before any pipeline spawns). Mark a task `in_progress` only immediately before spawning its pipeline. Mark it `completed` immediately after collecting its results. Never mark more than one category task `in_progress` simultaneously — misrepresents execution state. On loop retry or scope change, create new task.

## Step 1: Parse targets and create run directory

From `$ARGUMENTS`, determine:

- **Strip flags first**: extract `--fast`, `--full`, `--ab-test`, `--apply`, `--skip-gate`, `--local`, `--keep` before scope resolution; validate mutual exclusion (error and stop on conflict). Strip all flags from ARGUMENTS before scope token resolution:
  ```bash
  export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
  KEEP_ITEMS=""
  if [[ "$ARGUMENTS" =~ --keep[[:space:]]\"([^\"]+)\" ]]; then
      KEEP_ITEMS="${BASH_REMATCH[1]}"
  fi
  ARGUMENTS=$(echo "$ARGUMENTS" | sed 's/--keep "[^"]*"//g')
  rm -f .temp/state/skill-contract.md  # clear stale contract (compaction-contract.md §Lifecycle)  # timeout: 5000
  LOCAL_MODE=false; [[ "$ARGUMENTS" == *"--local"* ]] && LOCAL_MODE=true
  ARGUMENTS="${ARGUMENTS//--fast/}"; ARGUMENTS="${ARGUMENTS//--full/}"
  ARGUMENTS="${ARGUMENTS//--ab-test/}"; ARGUMENTS="${ARGUMENTS//--apply/}"
  ARGUMENTS="${ARGUMENTS//--skip-gate/}"; ARGUMENTS="${ARGUMENTS//--local/}"
  ARGUMENTS="${ARGUMENTS#"${ARGUMENTS%%[![:space:]]*}"}"
  mkdir -p "${TMPDIR:-/tmp}/calibrate-state-${CSID}"
  echo "$LOCAL_MODE" > "${TMPDIR:-/tmp}/calibrate-state-${CSID}/local-mode"
  echo "$KEEP_ITEMS" > "${TMPDIR:-/tmp}/calibrate-state-${CSID}/keep-items"
  ```
- **Target list** — remaining tokens after flag-strip; union of resolved targets:
  - `all` or omitted → all agents + `/audit` + routing + communication + all rules
  - `agents` → all agents (full agent list in `modes/agents.md`)
  - `skills` → `/audit` only (and other non-live-PR skills in `modes/skills.md`; `/oss:review` (requires `oss` plugin) excluded)
  - `routing` → routing accuracy test only
  - `communication` → handover + team protocol compliance only
  - `rules` → rule adherence test (all rule files in `.claude/rules/`) only
  - `plugins` → all agents + calibratable skills from all `plugins/*/` directories
  - `<plugin-name>` matching `plugins/<name>/` directory → tier 2: all agents + calibratable skills in that plugin
  - Any other token → tier 3: single agent or skill name; search `plugins/*/agents/<name>.md`, `.claude/agents/<name>.md`, `plugins/*/skills/<name>/SKILL.md`, `.claude/skills/<name>/SKILL.md`; error if no match
  - Multiple tokens → union: e.g. `oss research`, `curator shepherd`; each resolved independently

**Empty resolution guard**: after resolving all scope tokens to target list, if list is empty (e.g. plugin matched but contains no calibratable agents/skills, such as `/calibrate codemap`), stop with:

```text
! No calibratable agents/skills found for scope: <input-scope>
Verify: (a) plugin name spelled correctly, (b) plugin has agents/*.md or calibratable skills (see modes/skills.md domain table)
```

Do not proceed to Step 2 — silent no-op produces no report and confuses callers.

- **Pace**: `--full` → 10 problems; `--fast` → 3 problems; neither → default `--fast`
- **A/B flag**: `--ab-test` → also spawn `general-purpose` baseline per problem
- **Apply flag**:
  - `--apply` without pace flag → pure apply mode: skip Steps 2–5; go to Step 6
  - `--apply` with `--fast`/`--full` → benchmark + auto-apply: run Steps 2–5 then continue to Step 6

If benchmark will run (i.e., `--fast` or `--full` present, with or without `--apply`): generate timestamp `YYYY-MM-DDTHH-MM-SSZ` (UTC, e.g. `2026-03-03T13-44-48Z`) explicitly via the Bash tool and persist for downstream steps (fresh-shell state loss between Bash() calls):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
TIMESTAMP=$(date -u +%Y-%m-%dT%H-%M-%SZ)
echo "Calibration timestamp: $TIMESTAMP"
mkdir -p "${TMPDIR:-/tmp}/calibrate-state-${CSID}"
echo "$TIMESTAMP" > "${TMPDIR:-/tmp}/calibrate-state-${CSID}/timestamp"
```

Every subsequent Bash block in Steps 2–6 that uses `$TIMESTAMP` must re-read it at the top of the block:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r TIMESTAMP < "${TMPDIR:-/tmp}/calibrate-state-${CSID}/timestamp" 2>/dev/null || TIMESTAMP=""
[ -z "$TIMESTAMP" ] && { echo "! TIMESTAMP state lost — re-invoke /foundry:calibrate"; exit 1; }
# never fall back to $(date ...) — generates new timestamp → nonexistent run dir; surface state loss explicitly
```

All run dirs use this timestamp.

**Large fan-out gate** — after target list resolves (and before any task creation or pipeline spawn), when `--skip-gate` not passed:

- **Skip entirely** in pure-apply mode (`--apply` without a pace flag) — zero pipelines spawn in this mode (routes straight to Step 6), so no confirmation is needed.
- **Mode-category scopes** (`all`, `agents`, `skills`, `plugins`, `<plugin-name>` tier 2) — the target list here is mode categories, not yet expanded to individual agent/skill files (expansion happens inside Step 2's mode files, per the mode-file table below). An exact spawn count is not knowable at this point — these scopes routinely expand to dozens of agent/skill pipelines. Gate **always fires** whenever a benchmark pace flag is set (`--fast` or `--full`), independent of any count.
- **Tier-3 single-target scopes** (`<agent-name>`, `<skill-name>`) — the target list is already a concrete file (or small union of files), so the count is exact here: `SPAWN_ESTIMATE = <resolved-target-count> × (FULL_N if --full else FAST_N)`. Gate fires only when `SPAWN_ESTIMATE > SPAWN_GATE_THRESHOLD`.

When gated (either branch), fire **even when `--apply` is set together with a pace flag** — `--apply` only skips the Step 3 proposal-review gate, not this one.

Call `AskUserQuestion`:

- Mode-category scopes: question: "`<scope>` expands to dozens of agent/skill pipelines × `<N_PROBLEMS>` problems each — potentially 100+ spawns. Proceed?"
- Tier-3 scopes: question: "This run resolves to `<N>` targets × `<N_PROBLEMS>` problems ≈ `<SPAWN_ESTIMATE>` pipeline spawns. Proceed?"
- (a) label: `Proceed` — description: run as specified
- (b) label: `Switch to --fast` — description: re-run with `--fast` instead of `--full` (lowers spawn count ~3.3×) — **omit this option when pace is already `--fast`/default**; two-option menu (Proceed / Abort) in that case
- (c) label: `Abort` — description: stop; narrow scope and re-invoke

On Abort: stop immediately — no tasks created, no spawns. On Switch to --fast: replace pace flag with `--fast` (mode-category scopes still always-fire at `--fast`; tier-3 recomputes `SPAWN_ESTIMATE`), continue to task creation.

Create tasks before proceeding:

- Benchmark only (no `--apply`): TaskCreate "Calibrate agents" (if target includes agents), TaskCreate "Calibrate skills" (if target includes skills), TaskCreate "Calibrate routing" (if target includes routing), TaskCreate "Calibrate communication" (if target includes communication), TaskCreate "Calibrate rules" (if target includes rules), TaskCreate "Analyse and report" — all created as `pending`; do NOT mark any `in_progress` yet
- Benchmark + auto-apply (`--fast`/`--full` + `--apply`): TaskCreate "Calibrate agents" (if target includes agents), TaskCreate "Calibrate skills" (if target includes skills), TaskCreate "Calibrate routing" (if target includes routing), TaskCreate "Calibrate communication" (if target includes communication), TaskCreate "Calibrate rules" (if target includes rules), TaskCreate "Analyse and report", TaskCreate "Apply findings" — all created as `pending`; do NOT mark any `in_progress` yet
- Pure apply mode (only `--apply`, no pace flag): TaskCreate "Apply findings" only

## Step 2: Spawn pipeline subagents

> **Pre-flight**: mode files at `<plugin-cache>/foundry/<v>/skills/calibrate/modes/` — resolve via plugin cache scan below.
>
> `/foundry:setup` does NOT symlink these (only `rules/*.md` and `TEAM_PROTOCOL.md`); if not found, re-install foundry plugin.
>
> ```bash
> export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
> IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/calibrate-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
> CALIB_MODES_DIR=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/resolve_skill_subdir.py" calibrate modes $([ "$LOCAL_MODE" = "true" ] && echo --local))  # timeout: 5000
> ```
>
> **Gate**: if the bash block above failed (non-zero exit or `$CALIB_MODES_DIR` empty) — stop immediately; do not proceed to pipeline spawns. Print: `! calibrate/modes/ directory not found — re-install foundry plugin then retry.`

For each target mode in resolved target list, read corresponding mode file and execute spawn instructions. **At most 2 mode categories in flight at once**, and only with `$PIPELINE_BATCH_SIZE` halved to **2** (floor of 5÷2) for as long as two run concurrently. Rationale: the constraint being protected is peak agent count and context, not ordering — two categories at batch 2 peak at 4 concurrent pipelines, below the 5 a single category reaches on its own, so the resource ceiling is unchanged while wall-clock on `all` drops. Everything else stays serial: never issue a third category's spawns while two are running, and never restore batch size 5 until one of the two has fully returned its results.

**Execution order for `all`**: agents → skills → routing → communication → rules, run as pairs in that order — (agents + skills), (routing + communication), then rules alone at batch 5. For each pair:

1. **Guard** — call `TaskList`; if a category task (agents/skills/routing/communication/rules) is `in_progress` but its mode is **not** one of the categories currently in flight, call `TaskUpdate(that_task_id, completed)` before proceeding — corrects a missed completed call from a prior iteration. Never complete the task of a category still running: with two in flight, two category tasks are legitimately `in_progress`.
2. Mark both in-flight modes' tasks `in_progress` (all others stay `pending`)
3. Spawn pipelines for both modes with `$PIPELINE_BATCH_SIZE` = 2 (= 5 when a category runs alone — see constants)
4. Wait for all batch results from both modes before proceeding
5. Mark each mode's task `completed` as its own results arrive
6. Only then start the next pair

| Target mode | Mode file | Task to mark in_progress |
| -- | -- | -- |
| agents | `$CALIB_MODES_DIR/agents.md` | "Calibrate agents" |
| skills | `$CALIB_MODES_DIR/skills.md` | "Calibrate skills" |
| routing | `$CALIB_MODES_DIR/routing.md` | "Calibrate routing" |
| communication | `$CALIB_MODES_DIR/communication.md` | "Calibrate communication" |
| rules | `$CALIB_MODES_DIR/rules.md` | "Calibrate rules" |
| plugins or `<plugin-name>` (tier 2) | expand to per-agent + per-skill pipelines: glob `plugins/<name>/agents/*.md` and calibratable `plugins/<name>/skills/*/SKILL.md`; spawn one pipeline per resolved target using appropriate mode file (agents.md for agents, skills.md for calibratable skills); task name "Calibrate <plugin-name>" | "Calibrate <plugin-name>" |
| `<agent-name>` / `<skill-name>` (tier 3) | single-file pipeline: use agents.md or skills.md mode file with `<TARGET>` = resolved name; task name "Calibrate <name>" | "Calibrate <name>" |

For multiple tokens, merge resolved targets into per-mode groups before spawning — one pipeline per unique mode file needed, each carrying full target list.

Before spawning **any** pipeline (when target includes `agents`, `skills`, or `all`), check cross-plugin availability. When `LOCAL_MODE=true`, check `plugins/` source tree (local edits not yet installed); otherwise check installed plugin cache:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/calibrate-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
if [ "$LOCAL_MODE" = "true" ]; then
    [ -d "plugins/cc_oss" ]      && OSS_AVAILABLE="plugins/cc_oss"           || OSS_AVAILABLE=""
    [ -d "plugins/cc_research" ] && RESEARCH_AVAILABLE="plugins/cc_research" || RESEARCH_AVAILABLE=""
    [ -d "plugins/codemap-py" ]  && CODEMAP_AVAILABLE="plugins/codemap-py"   || CODEMAP_AVAILABLE=""
    [ -d "plugins/cc_develop" ]  && DEVELOP_AVAILABLE="plugins/cc_develop"   || DEVELOP_AVAILABLE=""
else
    OSS_AVAILABLE=$(find ~/.claude/plugins/cache -name "oss" -type d 2>/dev/null | head -1)  # timeout: 5000
    RESEARCH_AVAILABLE=$(find ~/.claude/plugins/cache -name "research" -type d 2>/dev/null | head -1)  # timeout: 5000
    CODEMAP_AVAILABLE=$(find ~/.claude/plugins/cache -name "codemap-py" -type d 2>/dev/null | head -1)  # timeout: 5000
    DEVELOP_AVAILABLE=$(find ~/.claude/plugins/cache -name "develop" -type d 2>/dev/null | head -1)  # timeout: 5000
fi
```

- **`agents` pipeline**: exclude `oss:cicd-steward` and `oss:shepherd` (requires `oss` plugin) if `$OSS_AVAILABLE` empty; exclude `research:data-steward` and `research:scientist` (requires `research` plugin) if `$RESEARCH_AVAILABLE` empty. Log: "oss/research plugin not installed — skipping <agent> calibration"
- **`skills` pipeline**: exclude `/oss:review` (requires `oss` plugin) always (requires live GitHub PR — not calibratable with synthetic input; see `modes/skills.md`); exclude `/codemap-py:*` skills (requires `codemap` plugin) if `$CODEMAP_AVAILABLE` empty; exclude `/research:plan`, `/research:judge`, `/research:verify` (requires `research` plugin) if `$RESEARCH_AVAILABLE` empty; exclude `/develop:review` (requires `develop` plugin) if `$DEVELOP_AVAILABLE` empty. Log skip message per excluded skill.

Fallback role descriptions for cross-plugin agents (if ever substituted with `general-purpose`) — run `cat "$_FS/agent-resolution.md"` (where `$_FS` is resolved via the cache-resolution block at the start of Step 2; if `$_FS` is empty, skip — role descriptions unavailable) and apply the matching fallback description.

Each mode file defines `<TARGET>`, `<DOMAIN>`, any N overrides, and extra instructions for pipeline subagent. Pipeline template lives at `$CALIB_MODES_DIR/../templates/pipeline-prompt.md`. **N override**: `communication` caps at fast=3 / full=5 (not global FULL_N=10) to prevent pipeline context overflow — run `cat "$CALIB_MODES_DIR/communication.md"` for details. **`rules` mode** spawns one `general-purpose` subagent per rule file (not standard pipeline template) — run `cat "$CALIB_MODES_DIR/rules.md"` for direct-spawn approach.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _TIMESTAMP < "${TMPDIR:-/tmp}/calibrate-state-${CSID}/timestamp" 2>/dev/null || _TIMESTAMP=""
IFS= read -r _KEEP < "${TMPDIR:-/tmp}/calibrate-state-${CSID}/keep-items" 2>/dev/null || _KEEP=""
_RUN_DIR=".reports/calibrate/$_TIMESTAMP"
_PRESERVE="run-dir=$_RUN_DIR, timestamp=$_TIMESTAMP"
[ -n "$_KEEP" ] && _PRESERVE="$_PRESERVE; user-keep: $_KEEP"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/write_skill_contract.py" "foundry:calibrate" "collect+synthesize (after pipeline fan-out)" "$_RUN_DIR" "$_PRESERVE" "collect pipeline results → combined report → follow-up gate (Step 3) → log (Step 4) → signals (Step 5)"  # timeout: 5000
```

## Step 3: Collect results and print combined report

**Completion handling** — pipeline spawns are synchronous, so no poll loop is possible (`_FOUNDRY_SHARED/agent-spawn-protocol.md` §Synchronous spawns). When a batch returns, read each target's compact JSON; when absent, read `.reports/calibrate/<TIMESTAMP>/<TARGET>/result.jsonl` (written on every exit path per the pipeline's graceful-exit protocol). Neither present → record `{"verdict":"timed_out"}` and mark the target `⏱` in the report; never omit a stalled target.

**On timeout**: read `tail -100 <output_file>` for partial JSON; if none use: `{"target":"<TARGET>","verdict":"timed_out","mean_recall":null,"gaps":["pipeline timed out — re-run individually with /calibrate <target> fast"]}`. Timed-out targets appear in report with ⏱ prefix and null metrics.

After all pipeline subagents complete or time out: mark "Analyse and report" in_progress. Parse compact JSON summary from each. (Category tasks — "Calibrate agents", "Calibrate skills", etc. — are already marked `completed` inline during Step 2's sequential loop; do not re-mark them here.)

For any pipeline that returned without a compact JSON, use Glob (pattern `*/result.jsonl`, base `.reports/calibrate/<TIMESTAMP>/`) to check whether a result file was written. If `result.jsonl` exists, parse it as the compact JSON for that target. If neither compact JSON nor `result.jsonl` exists, synthesize: `{"target":"<TARGET>","verdict":"incomplete","mean_recall":null,"calibration_bias":null,"gaps":["pipeline returned no output — re-run: /calibrate <TARGET> --fast"]}` and mark that target with ⏱ in the report table.

Print combined benchmark report:

```markdown
## Calibrate — <date> — <MODE>

| Target           | Recall | SevAcc | Fmt  | Confidence | Bias    | F1   | Scope | Verdict    | Top Gap              |
|------------------|--------|--------|------|------------|---------|------|-------|------------|----------------------|
| sw-engineer      | 0.83   | 0.91   | 0.87 | 0.85       | +0.02 ✓ | 0.81 | 0 ✓   | calibrated | async error paths    |
| ...              |        |        |      |            |         |      |       |            |                      |

*Recall: in-scope issues found / total. SevAcc: severity match rate for found issues (±1 tier) — high recall + low SevAcc = issues found but misprioritized. Fmt: fraction of found issues with location + severity + fix (actionability). Bias: confidence − recall (+ = overconfident). Scope: FP on out-of-scope input (0 ✓).*
```

**If AB mode**, add `ΔRecall`, `ΔSevAcc`, `ΔFmt`, `ΔTokens`, and `AB Verdict` columns after F1. ΔTokens = token_ratio − 1.0 (negative = specialist more concise).

```markdown
| Target      | Recall | SevAcc | Fmt  | Bias    | F1   | ΔRecall | ΔSevAcc | ΔFmt  | ΔTokens | Scope | AB Verdict |
|-------------|--------|--------|------|---------|------|---------|---------|-------|---------|-------|------------|
| sw-engineer | 0.83   | 0.91   | 0.87 | +0.02 ✓ | 0.81 | +0.05 ~ | +0.12 ✓ | +0.15 ✓ | −0.18 ✓ | 0 ✓ | marginal ~ |

*ΔRecall/ΔSevAcc/ΔFmt: specialist − general (positive = specialist better). ΔTokens: token_ratio − 1.0 (negative = more focused). AB Verdict covers ΔRecall and ΔF1 only; use ΔSevAcc and ΔFmt as supplementary evidence for agents where ΔRecall ≈ 0.*
```

**If target is `routing`**:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/calibrate-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
CALIB_MODES_DIR=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/resolve_skill_subdir.py" calibrate modes $([ "$LOCAL_MODE" = "true" ] && echo --local))  # timeout: 5000
cat "$CALIB_MODES_DIR/routing.md"
```

Use the "Report format" section loaded above instead of the table above. Mark "Calibrate routing" completed.

Flag targets where recall < 0.70 or |bias| > 0.15 with ⚠.

After table, print full content of each `proposal.md` for targets where `proposed_changes > 0`.

If `--apply` **not** set: after printing proposals, print the two genuine re-run commands as plain copy-pasteable text, then fire **Follow-up gate** (unless `--skip-gate` passed):

```text
Re-run full depth   /calibrate <targets> --full
Re-run full + A/B   /calibrate <targets> --full --ab-test
```

Call `AskUserQuestion` — do NOT write options as plain text. Map options directly:

- question: "Proposals ready. What next?" (include summary, e.g. "3 targets with proposals, 1 calibrated.")
- (a) label: `Apply proposals now` — description: apply in this session — proceed directly to Step 6 using the persisted TIMESTAMP, no re-invocation, no benchmark re-run
- (b) label: `skip` — description: review proposal files manually at `.reports/calibrate/<TIMESTAMP>/<TARGET>/proposal.md`

If `--apply` **was** set (benchmark + auto-apply mode), print `→ Auto-applying proposals now…` and proceed to Step 6.

Targets with verdict `calibrated` and no proposed changes get single line: `✓ <target> — no instruction changes needed`.

## Step 4: Concatenate JSONL logs

Append each target's result line to `.notes/logs/calibrations.jsonl` using native tools (no Bash needed):

1. Use Glob (pattern `*/result.jsonl`, path `.reports/calibrate/<TIMESTAMP>/`) to find all result files
2. Read each result file with Read tool
3. Read `.claude/logs/calibrations.jsonl` (legacy, if exists; use empty string if missing) and `.notes/logs/calibrations.jsonl` (if exists; use empty string if missing); concat both for historical context
4. Append new lines and Write combined content back to `.notes/logs/calibrations.jsonl` only — never write to `.claude/logs/calibrations.jsonl`

## Step 5: Surface improvement signals

For each flagged target (recall < 0.70 or |bias| > 0.15):

- **Recall < 0.70**: `→ Update <target> <antipatterns-to-flag> for: <gaps from result>` <!-- `<antipatterns-to-flag>` (not structural XML) — inline prose reference to agent-file section name -->
- **Bias > 0.15**: `→ Raise effective re-run threshold for <target> in MEMORY.md (default 0.70 → ~<mean_confidence>)`
- **Bias < −0.15**: `→ <target> is conservative; threshold can stay at default`

Proposals shown in Step 3 already surface actionable signals. Follow-up gate fires in Step 3 (unless `--skip-gate`). Mark "Analyse and report" completed. If `--apply` was set: proceed to Step 6.

```bash
rm -f .temp/state/skill-contract.md  # clear contract — skill complete (compaction-contract.md §Lifecycle)  # timeout: 5000
```

## Step 6: Apply proposals (apply mode)

Mark "Apply findings" in_progress.

**Determine run directory**:

- Benchmark + auto-apply mode (`--fast`/`--full` + `--apply`): re-read TIMESTAMP from persisted state (fresh-shell state loss):

  ```bash
  export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
  IFS= read -r TIMESTAMP < "${TMPDIR:-/tmp}/calibrate-state-${CSID}/timestamp" 2>/dev/null || TIMESTAMP=""
  [ -z "$TIMESTAMP" ] && { echo "! TIMESTAMP state lost — falling back to latest run dir"; TIMESTAMP=$(basename "$(find .reports/calibrate -maxdepth 1 -mindepth 1 -type d 2>/dev/null | sort -Vr | head -1)"); }  # safe: uses existing dir from find, not new $(date) timestamp — won't create phantom run dir
  ```

- Pure apply mode (only `--apply`, no pace flag): find most recent run:

```bash
LATEST=$(find .reports/calibrate -maxdepth 1 -mindepth 1 -type d 2>/dev/null | sort -Vr | head -1)
TIMESTAMP=$(basename "$LATEST")
[ -z "$TIMESTAMP" ] && { echo "! No prior calibration run found under .reports/calibrate/ — run /calibrate <targets> --fast first."; exit 1; }
```

For each target in target list, check whether `.reports/calibrate/<TIMESTAMP>/<target>/proposal.md` exists. Collect targets with proposal (`found`) and without (`missing`).

**Partial-match behavior**: `--apply` with mixed found/missing targets continues with found targets — does not halt on missing. For each **missing** target: print warning and skip (do not stop entire run): `⚠ No prior run for <target> — skipping. Re-run with --fast --apply to benchmark+apply, or --fast to benchmark only. (If target was skipped because its plugin was unavailable, install the plugin first, then re-run.)` Continue to next target. Only if ALL targets are missing: stop with `! No proposals found for any requested target` — nothing to apply. `--apply` without pace flag is intentional — see `<inputs>` definition; auto-triggering benchmark would contradict that contract.

**Print run's report before applying**: for each found target, read and print `.reports/calibrate/<TIMESTAMP>/<target>/benchmark-report.md` verbatim so user sees benchmark basis before any file changes.

**Spawn one `foundry:curator` subagent per found target (`.md` files — agents and skills). Issue ALL spawns in single response — no waiting between spawns.**

**Deduplicate by resolved physical path before spawning** — when two targets resolve to the same `<AGENT_FILE>` (e.g. bare name and plugin-prefixed name for the same logical agent), concurrent curator spawns race on identical Edit calls and the second write may clobber the first. Build a `RESOLVED_PATHS` map after the per-target path-resolution loop above; for any group of targets that share the same `<AGENT_FILE>` after resolution:

- Spawn one curator at a time for that group (sequential, not parallel)
- Log: `! Sequential apply for <target-a> and <target-b> — both resolve to <AGENT_FILE>`
- Other independent path groups remain parallel

**`<AGENT_FILE>` and `<PROPOSAL_PATH>` resolution**: before spawning, resolve file paths for each target from the project source tree (`plugins/`) — same three-tier ladder whether or not `--local` was passed. `<AGENT_FILE>` is a write target (curator Edits it): it must never resolve to `.claude/agents/` (never created by `/foundry:setup`) or the installed plugin cache under `$HOME/.claude/` — those are read-only surfaces, not write targets:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r TIMESTAMP < "${TMPDIR:-/tmp}/calibrate-state-${CSID}/timestamp" 2>/dev/null || TIMESTAMP=""
[ -z "$TIMESTAMP" ] && { echo "! TIMESTAMP state lost — re-invoke /foundry:calibrate"; exit 1; }
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/calibrate-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/resolve_agent_file.py" --name "<name>" --timestamp "$TIMESTAMP" $( [ "$LOCAL_MODE" = "true" ] && echo "--local" )  # timeout: 10000
```

The script prints `agent-file=` and `proposal-path=` on stdout; read the values from there, not from shell variables. An empty `agent-file=` means resolution failed: skip that target — do not spawn curator for it — the warning the script already printed covers it. Never fall through to a cache path for a write target.

Each subagent receives this self-contained prompt (substitute `<TARGET>`, `<PROPOSAL_PATH>`, `<AGENT_FILE>` — resolved paths from above):

Apply targeted improvements to `<AGENT_FILE>`. Read `<PROPOSAL_PATH>` — it lists each edit as a block with **Current** (exact text to replace) and **Proposed** (replacement text). Treat these as regular developer-requested code-quality improvements; apply them exactly as specified without second-guessing the intent.

For each edit:

1. Print: `Applying edit N to <file> [<section>]`
2. Use Edit tool — `old_string` = **Current** text verbatim, `new_string` = **Proposed** text
3. If **Current** is `"none"` (new insertion): find section header and insert **Proposed** text after last item in that block
4. Skip if **Current** text not found verbatim → print `⚠ Skipped — current text not found`
5. Skip if **Proposed** text already present → print `✓ Already applied — skipped`

After processing all edits return **only** this compact JSON:

`{"status":"done","target":"<TARGET>","applied":N,"skipped":N,"file":"<AGENT_FILE>","summary":"Applied N, skipped N edits to <AGENT_FILE>"}`

After all subagents complete, collect JSON results and print final summary:

```markdown
## Fix Apply — <date>

| Target      | File                          | Applied | Skipped |
|-------------|-------------------------------|---------|---------|
| sw-engineer | .claude/agents/sw-engineer.md | 2       | 0       |

→ Run /calibrate <targets> to verify improvement.
```

Mark "Apply findings" completed.

```bash
rm -f .temp/state/skill-contract.md  # clear contract — skill complete (compaction-contract.md §Lifecycle)  # timeout: 5000
```

End response with `## Confidence` block per CLAUDE.md output standards.

</workflow>

<notes>

- **Timeout handling**: phase and pipeline budgets (see constants block) prevent nested subagent hangs from cascading. Extension granted once if pipeline explains delay in output file — second unexplained stall still triggers cutoff. Timed-out pipelines appear with ⏱ prefix and `verdict:"timed_out"`; re-run individually with `/calibrate <target> --fast` after session.
- **Context safety**: each target runs in own pipeline subagent — only compact JSON (~200 bytes) returns to main context per target. Sequential spawning prevents concurrent resource and token spike; accumulated context across all targets is still compact.
- **Scorer delegation**: Phase 3a delegates scoring to per-problem `general-purpose` subagents. Each scorer reads response files from disk, returns ~200 bytes. Phase 3b runs Codex scorers sequentially via Bash (writes per-problem files). Phase 3c merges both into `scores.json`. Pipeline holds only compact JSONs regardless of N or A/B mode — no context budget concern.
- **Nesting depth**: main → pipeline subagent → target/scorer agents (2 levels). Pipeline spawns target agents (Phase 2), Claude scorer agents (Phase 3a), Codex scoring Bash calls (Phase 3b) at same depth — no additional nesting.
- `general-purpose` is built-in Claude Code agent type (no `.claude/agents/general-purpose.md` needed) — no custom system prompt, all tools available.
- **Quasi-ground-truth limitation**: partially addressed by cross-model generation (Claude + Codex) — two model families produce independent ground truth, reducing same-family blind spots. Adversarial and ceiling-difficulty problems included in every run (see difficulty distribution rules in `templates/pipeline-prompt.md` Phase 1a) to test false-positive discipline and reveal upper-bound limits. Remaining gap: synthetically generated adversarial problems weaker than expert-authored ones; `generator_recall_delta` surfaces whether one generator's problems are systematically easier or harder. `ceiling_recall` (reported separately from `mean_recall`) is primary signal for upper-bound performance — partial recall (0.4–0.7) on ceiling problems expected and does not affect calibration verdict.
- **Dual evaluation and scorer agreement**: Phase 3a (Claude) and Phase 3b (Codex) score each response independently. Phase 3c merges with Claude as 51% tiebreaker. `scorer_agreement` measures fraction of issues where both scorers agreed — low agreement (< SCORER_AGREEMENT_WARN=0.70) flags ambiguous ground truth or scorer blind spots. Severity disputes (scorers disagree >1 tier) excluded from SevAcc aggregate.
- **File-based Codex handoff**: Codex writes all output (problem JSON, score JSON) directly to run dir. Avoids bash stdout corruption when capturing large JSON from shell subprocesses. Pipeline reads from disk, never from stdout capture.
- **Historical comparability**: `result.jsonl` includes `"scoring":"dual|single"` and `"source_mode":"dual|claude-only"`. When analyzing trends in `calibrations.jsonl`, filter by these fields — dual-scored results not directly comparable to single-scored baselines.
- **Calibration bias is key signal**: positive bias (overconfident) → raise agent's effective re-run threshold in MEMORY.md. Negative bias (underconfident) → confidence conservative, no action needed. Near-zero → confidence trustworthy.
- **Do NOT use real project files**: benchmark only against synthetic inputs — no sensitive data and real files have no ground truth.
- **Skill benchmarks** run skill as subagent against synthetic config or code; scored identically to agent benchmarks.
- **Improvement loop**: systematic gaps → `<antipatterns-to-flag>` | consistent low recall → consider model tier upgrade (sonnet tier → opus tier) | large calibration bias → document adjusted threshold in MEMORY.md | re-calibrate after instruction changes to quantify improvement.
- **Report always**: every invocation surfaces report — benchmark runs print new results table; `--apply` without pace flag prints saved report from last run before applying, so user always sees basis for changes before files touched.
- **`--apply` semantics**: `--fast --apply` / `--full --apply` = run fresh benchmark then auto-apply new proposals. `--apply` alone = apply proposals from most recent past run without re-running benchmark.
- **Stale proposals**: `--apply` uses verbatim text matching (`old_string` = **Current** from proposal). If agent file edited between benchmark run and `--apply`, any change whose **Current** text no longer matches is skipped with warning — no silent clobbering of intermediate edits.
- **`routing` target vs `/audit` Check 12**: `/audit` Check 12 performs static analysis of description overlap (finds potential confusion zones); `/calibrate routing` tests behavioral impact — generates real routing decisions and measures whether descriptions actually disambiguate. Run in sequence: `/audit` first (fast, structural), then `/calibrate routing` (behavioral, slower). Complementary, not redundant.
- **`routing`, `communication`, `rules` in `all`**: see `all` entry in `<inputs>` for authoritative definition — use explicit targets only when running single mode in isolation.
- Follow-up chains:
  - Recall < 0.70 or borderline → pick "Apply proposals" from gate → `/calibrate <agent>` to verify improvement — stop and escalate to user if recall still < 0.70 after this cycle (max 1 apply cycle per run)
  - Calibration bias > 0.15 → add adjusted threshold to MEMORY.md → note in next audit
  - Routing accuracy < 0.90 or hard accuracy < 0.80 → update descriptions for confused pairs → `/calibrate routing` to verify improvement
  - Recommended cadence: run before and after any significant agent instruction change; run `/calibrate routing` after any agent description change; run `/calibrate communication` after any protocol or handoff change
- **Internal Quality Loop suppressed during benchmarking**: Phase 2 prompt explicitly tells target agents not to self-review before answering. Ensures calibration measures raw instruction quality — not `(agent + loop)` composite. Loop enabled → inflates recall and confidence by unknown ratio, masks real instruction gaps, makes improvement attribution impossible.
- **Skill-creator complement**: trigger accuracy and A/B description testing not yet implemented — future skill-creator skill from Anthropic would own this domain; run `/calibrate` for quality and recall.
- **A/B interpretation**: every specialized agent adds system-prompt tokens — if `general-purpose` subagent matches recall and F1, specialization adds no value. `ab` mode quantifies gap per-target. `significant` (Δ>0.10) confirms agent's domain depth earns cost; `marginal` (0.05–0.10) suggests instruction improvements may help; `none` (\<0.05) signals agent's current instructions add no measurable lift over vanilla agent. Token cost informational (logged in scores.json) but not part of verdict — prioritize recall/F1 delta as primary signal. Role-specificity caveat: for agents whose domain is well-covered by general training data, `none` ΔRecall does NOT mean "retire agent" — specialization shows up in ΔSevAcc, ΔFmt, ΔTokens even when ΔRecall ≈ 0; positive ΔSevAcc/ΔFmt combined with negative ΔTokens still confirms specialist earns cost.
- **AB mode nesting**: Phase 2b spawns `general-purpose` baseline agents inside pipeline subagent. Phase 3 spawns `general-purpose` scorer agents inside same pipeline subagent. All at 2 levels (main → pipeline → agents) — no additional depth.
- **Mode files**: domain tables and mode-specific spawn instructions live in `modes/agents.md`, `modes/skills.md`, `modes/routing.md`, `modes/communication.md`, `modes/rules.md`. Add new target mode by creating new file in `modes/` and adding row to Step 2 dispatch table.

</notes>
