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

Calibration data drives improvement loop: systematic gaps: instruction updates; persistent overconfidence: overconfidence checks in the target's `<antipatterns-to-flag>`.

NOT for: static routing overlap analysis (use /foundry:audit); manually reviewing skill output quality (use /develop:review (requires `develop` plugin)).

</objective>

<inputs>

- **$ARGUMENTS**: parse `--flags` first, then resolve remaining tokens as scope targets

  **Flags** (order independent):

  - `--fast` — 3 problems per target (default when neither pace flag passed)
  - `--full` — 10 problems per target; mutually exclusive with `--fast`
  - `--ab-test` — also run `general-purpose` baseline and report delta metrics; requires benchmark (default `--fast` if no pace flag); mutually exclusive with `--apply`
  - `--apply` — prepare proposals for application: with `--fast`/`--full`: benchmark then review the concrete edits; without pace flag: skip benchmark, select the most recent proposal per target; application always requires the Step 6 approval; mutually exclusive with `--ab-test`
  - `--skip-gate` — suppress fan-out and follow-up gates; for programmatic callers; never bypasses Step 6 edit approval
  - `--local` — read rules (`rules` target), mode files and the cross-plugin availability check from source tree (`plugins/*/`) instead of installed plugin cache; agent/skill targets come from source tree in both modes (`resolve_agent_file.py`); for plugin-dev workflows where local edits aren't yet installed; sets `LOCAL_MODE=true` in all pipeline spawns

  **Mutual exclusion validation** (check before any work):

  - `--ab-test` + `--apply` together: hard error: "`--ab-test` and `--apply` are mutually exclusive. Pass one or neither."
  - `--fast` + `--full` together: hard error: "Pass `--fast` or `--full`, not both."
  - `--ab-test` without pace flag: default `--fast` silently (no error)

  **Unsupported flag check** — after all supported flags extracted (`--fast`, `--full`, `--ab-test`, `--apply`, `--skip-gate`, `--local`, `--keep`), scan `$ARGUMENTS` for remaining `--<token>` tokens. Found: invoke `AskUserQuestion` with question text `` ! Unknown flag(s): `--<token>`. Supported: `--fast`, `--full`, `--ab-test`, `--apply`, `--skip-gate`, `--local`, `--keep`. `` — (a) **Abort** (stop, re-invoke with correct flags) · (b) **Continue ignoring** (skip unknown flags, proceed). On Abort: stop.

  **Legacy positional tokens** (`ab`, `apply`, `fast`, `full`) — **hard error**: print migration hint, stop. Example: "`ab` removed — use `--ab-test` flag: `/calibrate curator --ab-test`."

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
- CALIBRATE_LOG: `.notes/logs/calibrations.jsonl` — append-only (Step 4); legacy `.claude/logs/calibrations.jsonl` is read-only, migrated once behind the `.notes/logs/.calibrations-legacy-migrated` marker
- AB_ADVANTAGE_THRESHOLD: 0.10 (delta recall or F1 above this → meaningful advantage; below → marginal, baseline-ahead, inconclusive or no-lift; precedence in `templates/pipeline-prompt.md` Phase 4)
- CHILD_ALLOWANCE_S: 120 (planning allowance per serial child, not an enforced child runtime limit)
- PIPELINE_OVERHEAD_S: 600 (fixed allowance for local generation, validation, aggregation and coordinator re-entry)
- SERIAL_CHILDREN: `6 × PROBE_PROBLEMS + 2 × TOTAL_PROBLEMS × ARMS + 1`; `PROBE_PROBLEMS` = 2 for fast, 4 for full, or 0 with `weak_probe: off`; `TOTAL_PROBLEMS` = N + 1 scope problem + the domain's real-fixture count on full only; `ARMS` = 2 with `--ab-test`, otherwise 1. A fast run initially needs about 13 children (2 probes, 2 probe scorers, 4 targets, 4 scorers, 1 curator); reserve up to 21 including regeneration/re-probing and replacement of a probe that reads forbidden context. Six children per probed problem cover two generations, each with up to two weak probes and one scorer. Lower concurrency never reduces this serial allowance.
- PIPELINE_TIMEOUT_MIN: 52 (`ceil((600 + 120 × 21) / 60)` for fast, one arm, no Codex; arm `3120` seconds in `agent-watch-*.tsv` — no extension: the orchestrator has no clock to grant one)
- PIPELINE_BATCH_SIZE: 4 when one mode category runs alone, 2 while two categories are in flight; the session-wide maximum is 4 pipeline coordinators, each explicitly `model="sonnet"`, with per-tier child reservations defined by the session-wide spawn budget
- ROUTING_ACCURACY_THRESHOLD: 0.90 (below → agent descriptions need improvement) # keep in sync with modes/routing.md
- ROUTING_HARD_THRESHOLD: 0.80 (below → high-overlap pair descriptions need disambiguation)
- SPAWN_GATE_THRESHOLD: 50 (spawn estimate = target-count × N; above this, large-fan-out gate fires before Step 2 even when `--apply` is set — only `--skip-gate` bypasses)

<!-- Problem-set version 2.1 (schema v2 ground truth with decisive/role/near_misses, Phase 1b deterministic validator incl. token-shaped decisive, Phase 1c weak-solver probe, size floors, fetch store for real problems; 2.0 rows lack the fetch store and the sentence-decisive rejection; ledger rows without problem_set_version predate it and are not comparable) — bump when calibration problem set is refreshed (CODEX_PROBLEM_RATIO, CODEX_SCORER_WEIGHT, threshold defaults). Canonical source: this constants block + the per-mode problem fixtures under modes/*.md. Update version line below whenever any constant or fixture changes so historical calibrations.jsonl entries can be filtered by version. -->

- PROBLEM_SET_VERSION: 2.1
- CODEX_PROBLEM_RATIO: 0.5 (fraction of in-scope problems generated by Codex — agents/skills modes only; fast 2 of 3 is the one rounding exception)
- CODEX_SCORER_WEIGHT: 0.49 (Codex scorer weight; Claude = 0.51 — Claude has last word on disagreements)
- SCORER_AGREEMENT_WARN: 0.70 (scorer agreement below this → flag ambiguous ground truth ⚠)
- CODEX_MODES: ["agents", "skills"] (modes where Codex is active; routing/communication/rules excluded — test Claude-specific internals)
- CODEX_ALLOWANCE_S: `576 × GENERATION_CALLS + 432 × 2 × TOTAL_PROBLEMS × ARMS` when Codex active, otherwise 0; `GENERATION_CALLS` = 1 for fast or 3 for full, plus `PROBE_PROBLEMS` for possible regeneration. Generation uses the 576 s worst case for a 240 s bridge call; each 432 s scoring call includes its one retry; both A/B arms are scored sequentially.
- PIPELINE_TIMEOUT_MIN_DUAL: 139 (`ceil((600 + 120 × 21 + 576 × 3 + 432 × 2 × 4) / 60)` for fast dual-source; arm `8340` seconds, replacing the single-source fast deadline)
- PIPELINE_TIMEOUT_MIN_FULL: `ceil((PIPELINE_OVERHEAD_S + CHILD_ALLOWANCE_S × SERIAL_CHILDREN + CODEX_ALLOWANCE_S) / 60)` when `--full` or `--ab-test`, replacing both fast deadlines; arm that many minutes × 60 seconds. Count the domain's `real_fixtures:` JSON files before arming on full; A/B doubles target runs and Claude/Codex scores, not probes. This budgets serial children even when Haiku probes can overlap. These are conservative planning allowances, not measured completion guarantees; the previously measured 985 s dual full A/B run skipped Codex scoring and does not bound the serial path.
- APPLY_TIMEOUT_S: 600 (Step 6 apply-subagent deadline)

Domain tables per mode: see `modes/agents.md`, `modes/skills.md`, `modes/routing.md`, `modes/communication.md`, `modes/rules.md`.

</constants>

<compaction>
- Key boundary 1: after Step 2 pipeline fan-out (all mode pipelines spawned), before Step 3 collect+synthesize.
- Preserve at boundary 1: TIMESTAMP, run-dir (.reports/calibrate/<TIMESTAMP>/), target list, LOCAL_MODE.
- Terminal paths: end of Step 5 (no-apply path) and end of Step 6 (apply path).
</compaction>

<workflow>

**Task hygiene** — task tools may be deferred; load before first use: `ToolSearch(query="select:TaskList,TaskCreate,TaskUpdate,TaskGet", max_results=4)`. Call `TaskList` first and triage each task it returns: `completed` if work clearly done, `deleted` if orphaned, keep `in_progress` only if genuinely continuing. Never spend a turn on bookkeeping alone — every `TaskCreate`/`TaskUpdate` ships in the same response as the next substantive tool call; one exception, `TaskUpdate(completed)` immediately before a long output block (`rules/task-lifecycle.md`).

**Task tracking**: create tasks at start of execution (Step 1) for each phase that will run:

- "Calibrate agents" — Step 2 (benchmark mode, when target includes agents)
- "Calibrate skills" — Step 2 (benchmark mode, when target includes skills)
- "Calibrate routing" — Step 2 (benchmark mode, when target includes routing)
- "Calibrate communication" — Step 2 (benchmark mode, when target includes communication)
- "Calibrate rules" — Step 2 (benchmark mode, when target includes rules)
- "Analyse and report" — Steps 3–5 (benchmark mode)
- "Apply findings" — Step 6 (apply mode only)

**Task marking discipline**: create ALL category tasks as `pending` at the start (before any pipeline spawns). Mark a task `in_progress` only immediately before spawning its pipeline. Mark it `completed` immediately after collecting its results. Never mark more than the two in-flight categories' tasks `in_progress` simultaneously (Step 2 pairing) — misrepresents execution state. On loop retry or scope change, create new task.

<!-- ../../assets/images/calibrate-arch.svg diagrams the runs, gates and parallel fan-out. Documentation only, never loaded — update it in the same commit as any change to step order, gate placement, or agent fan-out. -->

## Step 1: Parse targets and create run directory

From `$ARGUMENTS`, determine:

- **Strip flags first**: extract `--fast`, `--full`, `--ab-test`, `--apply`, `--skip-gate`, `--local`, `--keep` before scope resolution; validate mutual exclusion (error and stop on conflict). Strip all flags from ARGUMENTS before scope token resolution:
  ```bash
  export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
  python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/parse-skill-flags.py" --flags fast,full,ab-test,apply,skip-gate,local --keep-state calibrate-state --out-file "${TMPDIR:-/tmp}/calibrate-state-${CSID}/keep-items" "$ARGUMENTS" >/dev/null  # timeout: 5000 — no eval; calibrate-state-<flag>-${CSID} sentinels, keep-items into the state dir it makes, clears stale contract
  IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/calibrate-state-local-${CSID}" 2>/dev/null || LOCAL_MODE=false
  IFS= read -r ARGUMENTS < "${TMPDIR:-/tmp}/calibrate-state-clean-args-${CSID}" 2>/dev/null || ARGUMENTS=""
  mkdir -p "${TMPDIR:-/tmp}/calibrate-state-${CSID}"
  echo "$LOCAL_MODE" > "${TMPDIR:-/tmp}/calibrate-state-${CSID}/local-mode"
  ```
- **Target list** — remaining tokens after flag-strip; union of resolved targets:
  - `all` or omitted → all agents + `/audit` + routing + communication + all rules
  - `agents` → all agents (full agent list in `modes/agents.md`)
  - `skills` → `/audit` only (and other non-live-PR skills in `modes/skills.md`; `/oss:review` (requires `oss` plugin) excluded)
  - `routing` → routing accuracy test only
  - `communication` → handover + team protocol compliance only
  - `rules` → rule adherence test (installed `~/.claude/rules/*.md` + project `.claude/rules/*.md`; `--local` → `plugins/*/rules/*.md` + project) only
  - `plugins` → all agents + calibratable skills from all `plugins/*/` directories
  - `<plugin-name>` matching `plugins/<name>/` directory → tier 2: all agents + calibratable skills in that plugin
  - Any other token → tier 3: single agent or skill name; search `plugins/*/agents/<name>.md`, `.claude/agents/<name>.md`, `plugins/*/skills/<name>/SKILL.md`, `.claude/skills/<name>/SKILL.md`; error if no match
  - Multiple tokens → union: e.g. `oss research`, `curator shepherd`; each resolved independently

**Empty resolution guard**: after resolving all scope tokens to target list, list empty (e.g. plugin matched but contains no calibratable agents/skills): stop with:

```text
! No calibratable agents/skills found for scope: <input-scope>
Verify: (a) plugin name spelled correctly, (b) plugin has agents/*.md or calibratable skills (see modes/skills.md domain table)
```

Do not proceed to Step 2 — silent no-op produces no report and confuses callers.

**Run identity**: resolve aliases to the concrete plugin-prefixed target first. Preserve each original domain label in `TARGET_LABEL`; strip its leading `/`, then replace every remaining `/`, `:` and space with `-` for `RUN_SLUG`. Apply this conversion to every mode's directory value before template substitution, deadline arming or apply lookup, including agent names. Communication uses the distinct label and slug `communication`. Reject any two registered domain entries with the same label, even when they resolve to the same target file; reject different domain labels that produce the same slug before spawning. Deduplicate scope aliases selecting the same domain, never distinct domain entries. Preserve the label, target file and slug mapping through compaction.

```bash
TARGET_LABEL='<TARGET_LABEL>'
RUN_SLUG=$(printf '%s' "${TARGET_LABEL#/}" | tr ': /' '---')
case "$RUN_SLUG" in ''|*[!A-Za-z0-9_-]*) echo "! Invalid calibration run slug"; exit 1;; esac
printf 'target-label=%s\nrun-slug=%s\n' "$TARGET_LABEL" "$RUN_SLUG"
```

- **Pace**: `--full`: 10 problems; `--fast`: 3 problems; neither: default `--fast`
- **A/B flag**: `--ab-test`: also spawn `general-purpose` baseline per problem
- **Apply flag**:
  - `--apply` without pace flag: pure apply mode: skip Steps 2–5, go to Step 6
  - `--apply` with `--fast`/`--full`: benchmark + apply: run Steps 2–5 then continue to Step 6 for concrete edit approval

Benchmark will run (every invocation except pure apply mode — `--apply` alone; no pace flag defaults to `--fast`): generate timestamp `YYYY-MM-DDTHH-MM-SSZ` (UTC, e.g. `2026-03-03T13-44-48Z`) explicitly via Bash tool, persist for downstream steps (fresh-shell state loss between Bash() calls):

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

- **Skip entirely** in pure-apply mode (`--apply` without a pace flag) — zero pipelines spawn in this mode (routes straight to Step 6), so no confirmation needed.
- **Mode-category scopes** (`all`, `agents`, `skills`, `plugins`) — target list here is mode categories, not yet expanded to individual agent/skill files (expansion happens inside Step 2's mode files, per mode-file table below). An exact spawn count isn't knowable at this point — these scopes routinely expand to dozens of agent/skill pipelines. Gate **always fires** whenever a benchmark will run (any invocation except pure apply mode, default `--fast` pace included), independent of any count.
- **Tier-2 plugin scopes** (`<plugin-name>`) — one plugin's contents are countable here without entering Step 2, by the same globs the Step 2 dispatch table uses: `SPAWN_ESTIMATE = (files matching plugins/<name>/agents/*.md + files matching plugins/<name>/skills/*/SKILL.md) × (FULL_N if --full else FAST_N)`. The skill term is an upper bound — `modes/skills.md` excludes some — so the estimate over-counts, which fires the gate more often, never less; keep it that way rather than "correcting" it. Gate fires only when `SPAWN_ESTIMATE > SPAWN_GATE_THRESHOLD`.
- **Tier-3 single-target scopes** (`<agent-name>`, `<skill-name>`) — target list is already a concrete file (or small union of files), so count is exact here: `SPAWN_ESTIMATE = <resolved-target-count> × (FULL_N if --full else FAST_N)`. Gate fires only when `SPAWN_ESTIMATE > SPAWN_GATE_THRESHOLD`.

When gated (either branch), fire **even when `--apply` is set together with a pace flag** — `--apply` only skips Step 3 proposal-review gate, not this one.

Call `AskUserQuestion`:

- Mode-category scopes: question: "`<scope>` expands to dozens of agent/skill pipelines × `<N_PROBLEMS>` problems each — potentially 100+ spawns. Proceed?"
- Counted scopes (tier-2 `<plugin-name>`, tier-3 `<agent-name>`/`<skill-name>`): question: "This run resolves to `<N>` targets × `<N_PROBLEMS>` problems ≈ `<SPAWN_ESTIMATE>` pipeline spawns. Proceed?"
- (a) label: `Proceed` — description: run as specified
- (b) label: `Switch to --fast` — description: re-run with `--fast` instead of `--full` (lowers spawn count ~3.3×) — **omit this option when pace is already `--fast`/default**; two-option menu (Proceed / Abort) in that case
- (c) label: `Abort` — description: stop; narrow scope and re-invoke

On Abort: stop immediately — no tasks created, no spawns. On Switch to --fast: replace pace flag with `--fast` (mode-category scopes still always-fire at `--fast`; tier-2 and tier-3 recompute `SPAWN_ESTIMATE`, and a recomputed estimate at or below `SPAWN_GATE_THRESHOLD` proceeds without re-asking), continue to task creation.

Create tasks before proceeding — the Task tracking list above, category tasks only for categories the target includes, all `pending`; do NOT mark any `in_progress` yet:

- Benchmark (`--fast`/`--full`): the category tasks + TaskCreate "Analyse and report"; add "Apply findings" when `--apply` is also set
- Pure apply mode (only `--apply`, no pace flag): "Apply findings" only

## Step 2: Spawn pipeline subagents

> **Mode-file load** — run once per dispatched mode, `<MODE_FILE>` = that mode's file from the dispatch table below. Mode files ship in the plugin cache; `/foundry:setup` does NOT symlink them (only `rules/*.md` and `TEAM_PROTOCOL.md`).
>
> ```bash
> export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
> IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/calibrate-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
> MODE_FILE='<MODE_FILE>'
> case "$MODE_FILE" in *'<'*'>'*|"") echo "! BLOCKED — <MODE_FILE> not substituted (agents.md | skills.md | routing.md | communication.md | rules.md)"; exit 1;; esac
> python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/load_mode.py" calibrate modes "$MODE_FILE" $([ "$LOCAL_MODE" = "true" ] && echo --local)  # timeout: 5000
> ```
>
> **Gate**: block exits non-zero (`! BREAKING` / `! BLOCKED` line) — stop immediately; do not proceed to pipeline spawns. Print: `! calibrate/modes/ directory not found — re-install foundry plugin then retry.`

For each target mode in resolved target list, load corresponding mode file (block above), execute spawn instructions within the session-wide budget below. **At most 2 mode categories in flight at once**, with `$PIPELINE_BATCH_SIZE` = **2** while paired, **4** when alone. Never issue a third category's spawns while two are running, or restore batch size 4 until one has fully returned its results. These limits also cap any larger batch named by a mode file.

**Session-wide spawn budget**: the orchestrator owns one inventory of all in-flight `Agent()` calls, including nested agents and unrelated work in this session. Spawn every pipeline coordinator explicitly with `model="sonnet"`; keep the four-coordinator cap. Per-pipeline allocation per tier is `max(1, floor((tier ceiling - coordinators in that tier) / coordinators))`, capped at haiku 4 / sonnet 1 / opus 1. With four Sonnet coordinators the arithmetic is Haiku `floor(20/4)=5`, capped at 4 (16 children, four spare slots), Sonnet `floor((8-4)/4)=1`, Opus `floor(5/4)=1`. Before dispatch subtract unrelated session work from each ceiling, then reduce the coordinator count until every required reservation fits; `max(1, ...)` never authorizes a slot when capacity is zero. Pass the reserved tier limits to each pipeline (routing and rules retain their stricter `CHILD_BATCH=1`); weak probes may overlap within the Haiku reservation, while Sonnet/Opus children remain serial. No overlapping phases. The constants budget all children serially, including retries: fast starts at about 13 children and reserves up to 21, at 120 s each plus 600 s overhead and any Codex allowance. Do not let pipelines independently claim the full tier ceiling. Release reservations only after coordinator and children finish or are confirmed stopped; a timeout alone does not free slots. Carry this inventory through compaction.

**Execution order for `all`**: agents → skills → routing → communication → rules, run as pairs in that order — (agents + skills), (routing + communication), then rules alone at batch 4. For each pair:

1. **Guard** — call `TaskList` in the same response as the pair's first spawn batch (never a turn of its own); a category task (agents/skills/routing/communication/rules) is `in_progress` but its mode is **not** one of the categories currently in flight: call `TaskUpdate(that_task_id, completed)` before proceeding — corrects a missed completed call from a prior iteration. Never complete the task of a category still running: with two in flight, two category tasks are legitimately `in_progress`.
2. Mark both in-flight modes' tasks `in_progress` (all others stay `pending`) — same response as the spawns
3. Spawn pipelines for both modes with `$PIPELINE_BATCH_SIZE` = 2 (= 4 when a category runs alone — see constants), reduced when the session budget requires it, and in that same response arm the batch deadline (below)
4. End the turn; collect every batch result from both modes via completion notifications + the deadline check before proceeding — never `ScheduleWakeup`, `ListAgents`, `Monitor`, a poll, or a "waiting" line
5. Mark each mode's task `completed` in the response that consumes its last result
6. Only then start the next pair — after the deadline check below has resolved every row of both modes (`done` or ⏱)

**Batch deadline** (`_shared/agent-spawn-protocol.md` §Deadlines): every spawn response Writes `<RUN_DIR>/agent-watch-<mode>-<batch-number>.tsv` (`<RUN_DIR>` = `.reports/calibrate/<TIMESTAMP>`), one row per pipeline: `<target>\t<that target's result.jsonl path from its mode file>\t<3120; 8340 when Codex active; PIPELINE_TIMEOUT_MIN_FULL × 60 when --full or --ab-test (overrides both fast deadlines; count the domain's real_fixtures: files before arming — see constants)>`. On every completion notification run once, before reading any result:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _TIMESTAMP < "${TMPDIR:-/tmp}/calibrate-state-${CSID}/timestamp" 2>/dev/null || _TIMESTAMP=""
[ -n "$_TIMESTAMP" ] || { echo "! BLOCKED — calibrate timestamp sentinel missing; cannot check agent deadlines"; exit 1; }
RUN_DIR=".reports/calibrate/$_TIMESTAMP"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/agent_watch.py" --state-dir "$RUN_DIR"  # timeout: 5000
```

`done` → consume · notification arrived but row not `done` → that target is ⏱ `incomplete` now (Step 3 synthesizes its record with the timeout reason) · rows open without a notification → end the turn. A ⏱ never answers or skips the Step 3 or Step 6 gates.

**Stray nested result**: a pipeline's own subagent (solver, scorer, curator) that finishes while its pipeline is between turns cannot hand its result back ("the agent that spawned you is no longer running") and may report to you instead. It is that pipeline's input, never yours: the pipeline advances on the deliverables in its run dir, not on acknowledgments. Never read the response, score it, relay it, or mark the pipeline's row from it; the row's deadline decides.

| Target mode | Mode file | Task to mark in_progress |
| -- | -- | -- |
| agents | `agents.md` | "Calibrate agents" |
| skills | `skills.md` | "Calibrate skills" |
| routing | `routing.md` | "Calibrate routing" |
| communication | `communication.md` | "Calibrate communication" |
| rules | `rules.md` | "Calibrate rules" |
| plugins or `<plugin-name>` (tier 2) | expand to per-agent + per-skill pipelines: glob `plugins/<name>/agents/*.md` and calibratable `plugins/<name>/skills/*/SKILL.md`; spawn one pipeline per resolved target using appropriate mode file (agents.md for agents, skills.md for calibratable skills); task name "Calibrate <plugin-name>" | "Calibrate <plugin-name>" |
| `<agent-name>` / `<skill-name>` (tier 3) | single-file pipeline: use agents.md or skills.md mode file with `<TARGET>` = resolved name; task name "Calibrate <name>" | "Calibrate <name>" |

For multiple tokens, merge resolved targets into per-mode groups before spawning — one pipeline per unique mode file needed, each carrying full target list.

Before spawning **any** pipeline (target includes `agents`, `skills`, or `all`): check cross-plugin availability. `LOCAL_MODE=true`: check `plugins/` source tree (local edits not yet installed); otherwise check installed plugin cache:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/calibrate-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
if [ "$LOCAL_MODE" = "true" ]; then
    [ -d "plugins/cc_oss" ]      && OSS_AVAILABLE="plugins/cc_oss"           || OSS_AVAILABLE=""
    [ -d "plugins/cc_research" ] && RESEARCH_AVAILABLE="plugins/cc_research" || RESEARCH_AVAILABLE=""
    [ -d "plugins/codemap-py" ]  && CODEMAP_AVAILABLE="plugins/codemap-py"   || CODEMAP_AVAILABLE=""
    [ -d "plugins/cc_develop" ]  && DEVELOP_AVAILABLE="plugins/cc_develop"   || DEVELOP_AVAILABLE=""
else
    for PLUGIN in oss research codemap-py develop; do
        AVAILABLE=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/resolve_plugin_root.py" --plugin-name "$PLUGIN")  # timeout: 5000
        RESOLVE_STATUS=$?
        case "$RESOLVE_STATUS" in
            0) ;;
            1) echo "! $PLUGIN: absent (exit 1)"; AVAILABLE="";;
            2) echo "! $PLUGIN: security or argument failure (exit 2) — skipping"; AVAILABLE="";;
            *) echo "! $PLUGIN: resolver failure (exit $RESOLVE_STATUS) — skipping"; AVAILABLE="";;
        esac
        case "$PLUGIN" in
            oss) OSS_AVAILABLE=$AVAILABLE;;
            research) RESEARCH_AVAILABLE=$AVAILABLE;;
            codemap-py) CODEMAP_AVAILABLE=$AVAILABLE;;
            develop) DEVELOP_AVAILABLE=$AVAILABLE;;
        esac
    done
fi
# shell vars die at block end — print so exclusions below read values, not empty vars
echo "OSS_AVAILABLE=$OSS_AVAILABLE"; echo "RESEARCH_AVAILABLE=$RESEARCH_AVAILABLE"; echo "CODEMAP_AVAILABLE=$CODEMAP_AVAILABLE"; echo "DEVELOP_AVAILABLE=$DEVELOP_AVAILABLE"
```

Exclusions below key on the printed `NAME=value` lines — empty value after `=` means unavailable; the preceding diagnostic distinguishes absence from resolver refusal or failure, with stderr preserved.

- **`agents` pipeline**: exclude all `oss:*` agents (requires `oss` plugin) if `$OSS_AVAILABLE` empty; exclude `research:*` agents (requires `research` plugin) if `$RESEARCH_AVAILABLE` empty. Log: "oss/research plugin not installed — skipping <agent> calibration"
- **`skills` pipeline**: exclude `/oss:review` (requires `oss` plugin) always (requires live GitHub PR — not calibratable with synthetic input; see `modes/skills.md`); exclude `/codemap-py:*` skills (requires `codemap-py` plugin) if `$CODEMAP_AVAILABLE` empty; exclude `/research:*` skills (requires `research` plugin) if `$RESEARCH_AVAILABLE` empty; exclude `/develop:*` skills (requires `develop` plugin) if `$DEVELOP_AVAILABLE` empty. Log skip message per excluded skill.

Fallback role descriptions for cross-plugin agents (if ever substituted with `general-purpose`): load them, apply matching fallback description (file missing: skip — role descriptions unavailable):

```bash
_FS=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/resolve_shared_path.py" foundry skills/_shared 2>/dev/null || echo "plugins/cc_foundry/skills/_shared")  # timeout: 5000
cat "$_FS/agent-resolution.md"
```

Each mode file defines `<TARGET>`, `<DOMAIN>`, any N overrides, extra instructions for pipeline subagent. Pipeline template: `templates/pipeline-prompt.md`, loaded by the mode file. **N override**: `communication` caps at fast=3 / full=5 (not global FULL_N=10) to prevent pipeline context overflow — details in `communication.md`. **`rules` mode** spawns one `general-purpose` subagent per rule file (not standard pipeline template) — direct-spawn approach in `rules.md`.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _TIMESTAMP < "${TMPDIR:-/tmp}/calibrate-state-${CSID}/timestamp" 2>/dev/null || _TIMESTAMP=""
_RUN_DIR=".reports/calibrate/$_TIMESTAMP"
_PRESERVE="run-dir=$_RUN_DIR, timestamp=$_TIMESTAMP"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/write_skill_contract.py" --keep-file "${TMPDIR:-/tmp}/calibrate-state-${CSID}/keep-items" "foundry:calibrate" "collect+synthesize (after pipeline fan-out)" "$_RUN_DIR" "$_PRESERVE" "collect pipeline results → combined report → log (Step 4) → follow-up gate (Step 3) → signals (Step 5)"  # timeout: 5000
```

## Step 3: Collect results and print combined report

**Completion handling** — pipeline spawns run in the background: issue the batch, end turn, resume on completion notifications and the Step 2 batch-deadline check; never a waiting tool (`ScheduleWakeup`, `ListAgents`, `Monitor`), a poll loop, a filler call, or a "waiting" line (`_FOUNDRY_SHARED/agent-spawn-protocol.md`). Each returns: read that target's compact JSON; absent: read its exact expected `result.jsonl` path from the deadline row (`<RUN_SLUG>` uses Step 1's safe conversion, e.g. `oss-analyse-vitality`; rules retain their nested `rules/<RULE_DIR>/` path). Neither present: use verdict `incomplete`, mark target `⏱` in report; never omit a stalled target.

**On timeout or missing output**: no compact JSON and no `result.jsonl` → synthesize one record with `ts`, `target` = `TARGET_LABEL`, `problem_set_version`, `verdict:"incomplete"`, null metrics and `gaps` naming the actual reason (`pipeline timed out` or `pipeline returned no output`) plus `/foundry:calibrate <TARGET> --fast`. Timed-out targets appear in report with ⏱ prefix and null metrics. Before Step 4, Write that record as `result-orchestrator.jsonl` beside the expected `result.jsonl` path from the deadline row, including nested rule paths. A compact result returned without a disk record must also be persisted in this orchestrator-owned file. Never overwrite the pipeline's own output; a late `result.jsonl` remains evidence but cannot replace the report's selected outcome. Keep a timed-out pipeline's reservation until it and its children are confirmed stopped; if no slots remain, mark undispatched targets incomplete with that reason rather than waiting indefinitely.

After all pipeline subagents complete or time out: mark "Analyse and report" in_progress. Parse compact JSON summary from each. (Category tasks — "Calibrate agents", "Calibrate skills", etc. — already marked `completed` inline during Step 2's paired category loop; don't re-mark them here.)

Reconcile the expected target inventory against these exact result paths before reporting: each target has one persisted selected outcome, including every incomplete target. With the Write tool, save those selected paths, one per target in sorted path order, to `.reports/calibrate/<TIMESTAMP>/ledger-sources.txt`. Freeze this selection for the report and Step 4; never discover outcomes again by globbing after reporting. Do not substitute a sibling target's result or silently drop a missing path.

Build combined benchmark report, including the full proposals below, and Write it to `.reports/calibrate/<TIMESTAMP>/benchmark-summary.md` first. Gate path: the report and proposals are printed once, rendered as normal Markdown, as the last reply text before the Follow-up gate call; the question text carries the benchmark headline, target/proposal counts, re-run commands below and `→ .reports/calibrate/<TIMESTAMP>/benchmark-summary.md`. Keep the question text to at most 6 plain lines (target 600 characters, hard cap 1,500), with no table, no pipe layout and no code fence. Options carry labels and one-line action descriptions, no `preview`; place the recommended option second without changing labels or answer mapping. Otherwise deliver the report and proposals once as the opening of the turn's final reply, after the last tool call. Report format:

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

**If target is `routing`**: use the "Report format" section of `routing.md` instead of the table above (not in context: Step 2 Mode-file load block with `MODE_FILE='routing.md'`).

Flag targets where recall < 0.70 or |bias| > 0.15 with ⚠.

After the table, add the full content of each `proposal.md` for targets where `proposed_changes > 0` (same placement).

**Log before any branch**: run the Step 4 log block now, after the report Write and before the Follow-up gate or auto-apply — every benchmark run lands in `.notes/logs/calibrations.jsonl` whichever branch follows (option (a) and `--apply` both jump to Step 6). Step 4 does not run a second time.

`--apply` **not** set: put the two genuine re-run commands below in the gate's question text and repeat them as plain copy-pasteable text in the final reply, then fire **Follow-up gate** (`--skip-gate` passed → no gate; report, proposals and commands open the final reply):

```text
Re-run full depth   /calibrate <targets> --full
Re-run full + A/B   /calibrate <targets> --full --ab-test
```

Call `AskUserQuestion` — do NOT write options as plain text. Map options directly:

- question: "Proposals ready. What next?" (include summary, e.g. "3 targets with proposals, 1 calibrated.")
- (a) label: `Apply proposals now` — description: review edits for application in this session — surface Step 5 signals and mark "Analyse and report" completed, then proceed to Step 6 using the persisted TIMESTAMP, no re-invocation, no benchmark re-run
- (b) label: `skip` — description: review proposal files manually at `.reports/calibrate/<TIMESTAMP>/<RUN_SLUG>/proposal.md`

`--apply` **was** set (benchmark + apply mode): print `→ Preparing proposals for edit approval…`, surface Step 5 signals and mark "Analyse and report" completed, then proceed to Step 6. The flag never authorizes undisplayed generated edits.

Targets with verdict `calibrated` and no proposed changes get single line: `✓ <target> — no instruction changes needed`.

## Step 4: Append JSONL logs

Runs from Step 3, before its Follow-up gate or auto-apply branch (see "Log before any branch"); once per benchmark run. `.notes/logs/calibrations.jsonl` is append-only history: this run's per-target `result.jsonl` files are appended to it by `append_ledger.py --from`, never by reading the log and writing it back. One fixed block, two parts:

- **One-time legacy migration**, guarded by the marker `.notes/logs/.calibrations-legacy-migrated`. Marker absent and the `.notes` log empty or missing → append the legacy `.claude/logs/calibrations.jsonl` once. Marker absent but the `.notes` log already holds lines → an earlier run's read-concat-write already merged legacy history, so only set the marker; copying again would duplicate it. `.claude/logs/calibrations.jsonl` is never written.
- **This run's results** — the selected records in `ledger-sources.txt`, including nested `rules/<RULE_DIR>/` results and synthesized incomplete records, appended in sorted path order after Step 3 reconciliation; an empty result set is reported.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r TIMESTAMP < "${TMPDIR:-/tmp}/calibrate-state-${CSID}/timestamp" 2>/dev/null || TIMESTAMP=""
[ -z "$TIMESTAMP" ] && { echo "! TIMESTAMP state lost — re-invoke /foundry:calibrate"; exit 1; }
mkdir -p .notes/logs  # timeout: 3000
if [ ! -f .notes/logs/.calibrations-legacy-migrated ]; then
    if [ -f .claude/logs/calibrations.jsonl ] && [ ! -s .notes/logs/calibrations.jsonl ]; then
        python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/append_ledger.py" .notes/logs/calibrations.jsonl --from .claude/logs/calibrations.jsonl && touch .notes/logs/.calibrations-legacy-migrated || exit 1  # timeout: 5000
    else
        touch .notes/logs/.calibrations-legacy-migrated
    fi
fi
RESULT_FILES=".reports/calibrate/$TIMESTAMP/ledger-sources.txt"
[ -f "$RESULT_FILES" ] || { echo "! Missing selected calibration results — reconcile Step 3 before logging"; exit 1; }
if [ ! -s "$RESULT_FILES" ]; then
    echo "! no result.jsonl files found for this calibration run"
fi
RESULT_SOURCES=()
while IFS= read -r RESULT || [ -n "$RESULT" ]; do
    [ -f "$RESULT" ] || { echo "! Missing selected calibration result: $RESULT"; exit 1; }
    [ -r "$RESULT" ] || { echo "! Unreadable selected calibration result: $RESULT"; exit 1; }
    RESULT_SOURCES+=("$RESULT")
done < "$RESULT_FILES"
if [ "${#RESULT_SOURCES[@]}" -gt 0 ]; then
    python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/append_ledger.py" .notes/logs/calibrations.jsonl --from "${RESULT_SOURCES[@]}" || exit 1  # timeout: 5000
fi
```

## Step 5: Surface improvement signals

For each flagged target (recall < 0.70 or |bias| > 0.15):

- **Recall < 0.70**: `→ Update <target> <antipatterns-to-flag> for: <gaps from result>` <!-- `<antipatterns-to-flag>` (not structural XML) — inline prose reference to agent-file section name -->
- **Bias > 0.15**: `→ Add an overconfidence check to <target> <antipatterns-to-flag> (confidence ~<mean_confidence> vs recall <mean_recall>)`
- **Bias < −0.15**: `→ <target> is conservative; threshold can stay at default`

Proposals shown in Step 3 already surface actionable signals. Follow-up gate fires in Step 3 (unless `--skip-gate`). Mark "Analyse and report" completed. `--apply` was set: proceed to Step 6.

```bash
rm -f .temp/state/skill-contract.md  # clear contract — skill complete (compaction-contract.md §Lifecycle)  # timeout: 5000
```

## Step 6: Apply proposals (apply mode)

Mark "Apply findings" in_progress, then load the apply procedure (run-dir selection, per-target curator dispatch with deadlines, final summary, contract clear) and execute it:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/calibrate-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/load_mode.py" calibrate modes apply.md $([ "$LOCAL_MODE" = "true" ] && echo --local)  # timeout: 5000
```

</workflow>

<notes>

- **Timeout handling**: pipeline budgets (see constants block) prevent nested subagent hangs from cascading. Deadlines are armed in the spawn response and checked by `agent_watch.py` on each notification — no extension, since nothing in the orchestrator can count time to grant one. Timed-out pipelines appear with ⏱ prefix and `verdict:"incomplete"`, with the timeout reason preserved in the persisted result; re-run individually with `/calibrate <target> --fast` after session.
- **Context safety**: each target runs in own pipeline subagent — only compact JSON (~200 bytes) returns to main context per target. Batched spawning (`PIPELINE_BATCH_SIZE`; paired categories cap peak at 4 concurrent pipelines) prevents a resource and token spike; accumulated context across all targets still compact.
- **Scorer delegation**: Phase 3a delegates scoring to per-problem `general-purpose` subagents. Each scorer reads response files from disk, returns ~200 bytes. Phase 3b runs Codex scorers sequentially via Bash (writes per-problem files). Phase 3c merges both into `scores.json`. Pipeline holds only compact JSONs regardless of N or A/B mode — no context budget concern.
- **Nesting depth**: main → pipeline subagent → target/scorer agents (2 levels). Pipeline spawns weak-solver probes and their scorers (Phase 1c), target agents (Phase 2), Claude scorer agents (Phase 3a), Codex scoring Bash calls (Phase 3b) at same depth — no additional nesting. Skill targets run under the fixture contract (no `gh`, no spawns), so a skill that normally fans out does not add a level.
- `general-purpose` is built-in Claude Code agent type (no `.claude/agents/general-purpose.md` needed) — no custom system prompt, all tools available.
- **Quasi-ground-truth limitation**: partially addressed by cross-model generation (Claude + Codex) — two model families produce independent ground truth, reducing same-family blind spots. Adversarial and ceiling-difficulty problems included in every run (see difficulty distribution rules in `templates/pipeline-prompt.md` Phase 1a) to test false-positive discipline, reveal upper-bound limits. Generator intent alone never makes a problem hard: Phase 1c runs every high/extreme problem past a weak solver (haiku, no domain instructions), regenerates or downgrades any it fully solves, and reports `weak_solver_recall_*` so a tier discriminates only when the target beats it by ≥ 0.15. Remaining gap: synthetically generated adversarial problems weaker than expert-authored ones; `generator_recall_delta` surfaces whether one generator's problems are systematically easier or harder. `ceiling_recall` (reported separately from `mean_recall`) is primary signal for upper-bound performance — partial recall (0.4–0.7) on ceiling problems expected, doesn't affect calibration verdict.
- **Dual evaluation and scorer agreement**: Phase 3a (Claude) and Phase 3b (Codex) score each response independently. Phase 3c merges with Claude as 51% tiebreaker. `scorer_agreement` measures fraction of issues where both scorers agreed — low agreement (< SCORER_AGREEMENT_WARN=0.70) flags ambiguous ground truth or scorer blind spots. Severity disputes (scorers disagree >1 tier) excluded from SevAcc aggregate.
- **File-based Codex handoff**: Codex writes all output (problem JSON, score JSON) directly to run dir. Avoids bash stdout corruption when capturing large JSON from shell subprocesses. Pipeline reads from disk, never from stdout capture.
- **Historical comparability**: `result.jsonl` includes `"scoring":"dual|single"` and `"source_mode":"dual|claude-only"`. Analyzing trends in `calibrations.jsonl`: filter by these fields — dual-scored results not directly comparable to single-scored baselines.
- **Calibration bias is key signal**: positive bias (overconfident): add an overconfidence check to the agent's `<antipatterns-to-flag>`. Negative bias (underconfident): confidence conservative, no action needed. Near-zero: confidence trustworthy.
- **No private or sensitive project files**: benchmarks use synthetic inputs by default — arbitrary real files carry no ground truth. One exception: frozen public issue threads whose outcome a maintainer recorded (e.g. closed as a duplicate of a named issue), committed with pseudonymized logins under the directory a domain names as `real_fixtures:`, wrapped as untrusted data, cut before the verdict so the answer cannot leak. They attach on `--full` only and report as `recall_real`; they decide the verdict only for a domain that sets `verdict_tier: real` (and holds at least 5 of them), because generated problems in that domain stayed solvable without the skill.
- **Skill benchmarks** run skill as subagent against synthetic config or code; scored identically to agent benchmarks.
- **Improvement loop**: systematic gaps → `<antipatterns-to-flag>` | consistent low recall: consider model tier upgrade (sonnet tier → opus tier) | large calibration bias: add overconfidence check to `<antipatterns-to-flag>` | re-calibrate after instruction changes to quantify improvement.
- **Report always**: every invocation surfaces report — benchmark runs print new results table; `--apply` without pace flag prints saved report from last run before applying, so user always sees basis for changes before files touched.
- **`--apply` semantics**: `--fast --apply` / `--full --apply` = run a fresh benchmark then review new proposals for Step 6 approval. `--apply` alone = select the newest saved proposal per target without re-running the benchmark, then request the same edit approval. Neither form nor `--skip-gate` authorizes generated edits before review.
- **Stale proposals**: `--apply` uses verbatim text matching (`old_string` = **Current** from proposal). Agent file edited between benchmark run and `--apply`: any change whose **Current** text no longer matches is skipped with warning — no silent clobbering of intermediate edits.
- **`routing` target vs `/audit` Check 20**: `/audit` Check 20 (sub-check 20a, `audit/templates/checks-agents.md`) performs static analysis of description overlap (finds potential confusion zones); `/calibrate routing` tests behavioral impact — generates real routing decisions, measures whether descriptions actually disambiguate. Run in sequence: `/audit` first (fast, structural), then `/calibrate routing` (behavioral, slower). Complementary, not redundant.
- **`routing`, `communication`, `rules` in `all`**: see `all` entry in `<inputs>` for authoritative definition — use explicit targets only when running single mode in isolation.
- Follow-up chains:
  - Recall < 0.70 or borderline: pick "Apply proposals" from gate → `/calibrate <agent>` to verify improvement — stop, escalate to user if recall still < 0.70 after this cycle (max 1 apply cycle per run)
  - Calibration bias > 0.15: add overconfidence check to the target's `<antipatterns-to-flag>`, note in next audit
  - Confidence-format change (per-gap deductions, foundry 0.64.6): benchmark prompt now asks for them — compare calibration bias across that boundary as a re-baseline, not a trend
  - Routing accuracy < 0.90 or hard accuracy < 0.80: update descriptions for confused pairs → `/calibrate routing` to verify improvement
  - Recommended cadence: run before and after any significant agent instruction change; run `/calibrate routing` after any agent description change; run `/calibrate communication` after any protocol or handoff change
- **Internal Quality Loop suppressed during benchmarking**: Phase 2 prompt explicitly tells target agents not to self-review before answering. Ensures calibration measures raw instruction quality — not `(agent + loop)` composite. Loop enabled: inflates recall and confidence by unknown ratio, masks real instruction gaps, makes improvement attribution impossible.
- **Skill-creator complement**: trigger accuracy and A/B description testing not yet implemented — future skill-creator skill from Anthropic would own this domain; run `/calibrate` for quality and recall.
- **A/B interpretation**: every specialized agent adds system-prompt tokens — `general-purpose` subagent matches recall and F1: specialization adds no value. `ab` mode quantifies gap per-target. Verdicts follow first-match-wins precedence in `templates/pipeline-prompt.md` Phase 4; mixed-sign results can be `significant` or `marginal` despite a negative delta on the other metric — describe that trade-off explicitly, not as improvement on both metrics.
  - `significant` (Δ>0.10) confirms agent's domain depth earns cost; `marginal` (0.05–0.10) suggests instruction improvements may help; `baseline-ahead` (ΔRecall or ΔF1 ≤ −0.05, and neither positive delta ≥ 0.05) means vanilla agent beat specialist — inspect instructions first; `inconclusive` (both arms recall ≥ 0.95) means problems hit ceiling and could not separate arms — re-run `--full` before judging; `no-lift` (|Δ| \<0.05 below ceiling) signals agent's current instructions add no measurable lift over vanilla agent.
  - Legacy `none` in older ledger entries covers all of `baseline-ahead`, `inconclusive`, `no-lift` — recompute from `delta_recall`, `delta_f1`, `mean_recall` before comparing across runs.
  - Token cost informational (logged in scores.json) but not part of verdict — prioritize recall/F1 delta as primary signal.
  - Role-specificity caveat: for agents whose domain is well-covered by general training data, `no-lift` or `inconclusive` does NOT mean "retire agent" — specialization shows up in ΔSevAcc, ΔFmt, ΔTokens even when ΔRecall ≈ 0; positive ΔSevAcc/ΔFmt combined with negative ΔTokens still confirms specialist earns cost.
- **AB mode nesting**: Phase 2b spawns `general-purpose` baseline agents inside pipeline subagent. Phase 3 spawns `general-purpose` scorer agents inside same pipeline subagent. All at 2 levels (main → pipeline → agents) — no additional depth.
- **Mode files**: domain tables and mode-specific spawn instructions live in `modes/agents.md`, `modes/skills.md`, `modes/routing.md`, `modes/communication.md`, `modes/rules.md`. Add new target mode by creating new file in `modes/`, adding row to Step 2 dispatch table.

</notes>
