---
name: run
description: Sustained metric-improvement loop with atomic commits, auto-rollback, and experiment logging. Iterates with specialist agents, commits atomically, auto-rolls back on regression. Accepts a program.md file path. Supports --resume, --team, --colab, --codex, --researcher, --architect, --journal, --hypothesis.
argument-hint: <program.md> [clarification] [--resume <program.md>] [--team] [--compute=local|colab|docker] [--colab[=H100|L4|T4|A100]] [--codex] [--researcher] [--architect] [--journal] [--hypothesis <path>] [--scientist] [--codemap] [--no-codemap] [--keep "<items>"]
effort: xhigh
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, Agent, TaskCreate, TaskUpdate, AskUserQuestion, Skill
disable-model-invocation: true
---

<objective>

Sustained metric-improvement loop — reads `program.md`, iterates specialist ideation agents, commits atomically, auto-rolls back on regression. For long-running automated improvement campaigns.

NOT for: methodology validation before run (use `/research:judge`); hypothesis generation (use `research:scientist` agent); one-off feature work (use `/develop:feature`); no `program.md` yet / starting from a bare goal (use `/research:sweep`).

Accept a metric-improvement campaign on a `program.md` or text goal, including a dry-run/walkthrough of that campaign (trace only; execute nothing). A text goal must name a measurable improvement target, using the same optimization-goal test as `/research:plan` P-P1; R1's text-goal fallback does not make an unrelated request a campaign. Any other request → print `! UNSUPPORTED — /research:run cannot do this request`, name the redirect, stop. Never perform the out-of-scope task after the redirect — no "direct answer" section, no partial findings.

</objective>

<constants>

Campaign mode only:

```yaml
MAX_ITERATIONS:             50 (hard cap); DEFAULT 20 when max_iterations unset in program.md; program.md may raise up to 50; values above 50 clamped to 50 with a warning
MAX_CODEX_RUNS:             10 (cost ceiling for --codex Phase 2c — disable Codex once exceeded)
STUCK_THRESHOLD:            5 consecutive discards → escalation
GUARD_REWORK_MAX:           2 attempts before revert
VERIFY_TIMEOUT_SEC:         120 (local), 300 (--colab)
COLAB_KNOWN_HW:             H100, L4, T4, A100
SUMMARY_INTERVAL:           10 iterations
DIMINISHING_RETURNS_WINDOW: 5 iterations < 0.5% each → warn user and suggest stopping
STATE_DIR:                  .experiments/state/<run-id>/  (timestamped dir per run — foundry artifact-lifecycle rule, requires foundry plugin)
METRIC_REPEATS_DEFAULT:     1 (metric runs per measurement; ## Config metric_repeats 1–10 overrides; 1 = single sample, keep any measured gain)
MIN_DELTA_PCT:              0.1 (% of |best_metric|; ## Config min_delta_pct overrides — smallest gain kept when metric_repeats > 1, and the tiny-gain-on-large-change discard threshold)
```

**Repeated measurement** (`metric_repeats` > 1 — opt-in, each iteration's metric cost × `metric_repeats`): R4 and Phase 5 run `metric_cmd` that many times, each run its own Bash/MCP call under its own `VERIFY_TIMEOUT_MS`, each staged as one line of `STATE_DIR/metric-runs.jsonl`; `bin/run_metric_stats.py status` is the cursor and `decide` is Phase 7's verdict — keep only a gain beyond `max(min_delta_pct, 2σΔ)`, the noise model `/research:fortify` ranks ablations with. Recorded metric = mean of the runs; scalar `metric` fields keep their meaning for every reader.

<!-- Note: STATE_DIR (.experiments/state/) holds per-iteration artifacts (diary, experiments.jsonl).
     Hypothesis pipeline outputs (hypotheses.jsonl, checkpoint.jsonl, journal.md) go to .experiments/<run-id>/ (RUN_DIR).
     These are two separate directories by design — see protocol.md for layout. -->

<!-- policy-sibling: plugins/cc_research/skills/run/modes/colab-setup.md, plugins/cc_research/skills/plan/SKILL.md, plugins/cc_research/skills/sweep/SKILL.md, plugins/cc_research/skills/judge/SKILL.md — COLAB_KNOWN_HW set restated in each; keep in sync (plugins/CLAUDE.md §Policy Duplication Marker). -->

**Agent strategy mapping** (`agent_strategy` in config → ideation agent to spawn):

| `agent_strategy` | Specialist agent | When to use |
| -- | -- | -- |
| `auto` | heuristic | Default — infer from metric_cmd keywords |
| `perf` | `foundry:perf-optimizer` | latency, throughput, memory, GPU utilization |
| `code` | `foundry:sw-engineer` | coverage, complexity, lines, coupling |
| `ml` | `research:scientist` | accuracy, loss, F1, AUC, BLEU |
| `arch` | `foundry:solution-architect` proposes, `foundry:sw-engineer` implements (Phase 2) | coupling, cohesion, modularity metrics |

**Auto-inference keyword heuristics** (when `agent_strategy: auto` or omitted; checked against `## Goal` text AND metric command):

**Precedence order** (first match wins; ML keywords beat test-framework keywords). ML-specific compound terms (not bare tokens) required — prevents over-triggering on `eval`/`train`/`val` as common words:

- contains `accuracy`, `loss` (paired with `train_loss`/`val_loss`/`eval_loss`), `f1_score`, `auc_roc`, `auroc`, `train_step`, `val_acc`, `eval_loss`, `epoch`, `gradient`, `tensor`, `overfit`, `generaliz`, `regulariz`, `validation`, `dropout`, `weight_decay`, `lr_schedule`, `cross_val`, `precision`, `recall`, OR explicit `--scientist` flag → `ml` → `research:scientist`
- contains `time`, `latency`, `bench`, `throughput`, `memory` → `perf` → `foundry:perf-optimizer`
- contains `pytest`, `coverage`, `complexity` → `code` → `foundry:sw-engineer`
- no keyword match → `perf` (default fallback) — rationale: perf-optimizer profiles before changing and is the least assumption-laden generic improver; code/ml metrics are reliably keyword-detectable, so an unmatched goal is most often timing-shaped. **WARN**: print `⚠ No keyword match — defaulting to 'perf' strategy. If this is an ML task, set agent_strategy: ml in program.md.` Log resolved agent + reason in state.json `strategy_resolution`.

Bare tokens `eval`, `train`, `val` (without compound suffix) do NOT trigger `ml` routing — too common in non-ML contexts (test eval scripts, training-environment configs, validator command names).

**Stuck escalation sequence** (at STUCK_THRESHOLD consecutive discards):

1. Switch agent type. Rotation by current strategy:

   | Current strategy | Next strategy | Escalation agent |
   | -- | -- | -- |
   | `code` | `ml` | `research:scientist` |
   | `ml` | `perf` | `foundry:perf-optimizer` |
   | `perf` | `code` | `foundry:sw-engineer` |
   | `arch` | `code` | `foundry:sw-engineer` (fallback `general-purpose` if sw-engineer unavailable) |
   | `auto` | infer from resolved strategy | follow rotation row for whichever concrete strategy `auto` heuristics resolved to at Step R3 (e.g. `auto` → resolved `ml` → next `perf` → `foundry:perf-optimizer`) |

2. Spawn 2 agents parallel, competing strategies, **propose only** — both share one working tree, so neither edits project files. Each writes full analysis + one proposed change to `.experiments/state/<run-id>/stuck-escalation-<i>-<strategy>.md`, returns ONLY compact JSON envelope. Use this spawn prompt verbatim (substitute `<run-id>`, `<i>`, and strategy):

   ```text
   <your-strategy> stuck-escalation proposal — iteration <i> after STUCK_THRESHOLD consecutive discards.
   Read `.experiments/state/<run-id>/state.json` for goal, best_metric, baseline, config.
   Read `.experiments/state/<run-id>/experiments.jsonl` for full iteration history.
   Read `.experiments/state/<run-id>/diary.md` for qualitative context (what was tried, why reverted).
   Read `.experiments/state/<run-id>/context-<i>.md` for current iteration's context block.
   Continue from the last completed iteration (do NOT restart from iteration 0).
   Propose only: do NOT edit, create, or delete project files and do NOT run git — the lead applies and measures each proposal.
   Write your full analysis and ONE proposed atomic change (files, exact edits) to `.experiments/state/<run-id>/stuck-escalation-<i>-<your-strategy>.md`.
   Return ONLY: {"strategy":"<your-strategy>","description":"...","files_modified":[],"proposed_changes":"<one-line description>","confidence":0.N,"file":".experiments/state/<run-id>/stuck-escalation-<i>-<your-strategy>.md"}
   ```

   Consolidation: lead applies proposals one at a time, higher `confidence` first — each is its own iteration: Phase 0, then the Phase 2b apply prompt (`modes/compute-docker.md` §Phase 2b, escalation file in place of `ideation-<i>.md`), then Phases 3–8. Phase 7 keeps or reverts each on measured delta + guard. Escalation iterations count toward `max_iterations`.

3. Single stuck outcome, every mode (solo, `--codex`, `--team`): any escalation iteration kept → reset discard count, continue loop on switched strategy. Both discarded → stop, report progress, surface to user — no blind looping. `--codex` effort boost (`modes/codex-copilot.md`) applies inside escalation iterations only, never replaces this stop.

</constants>

<compaction>

- Key boundary: end of each Phase 8 in R5 iteration loop — staged records appended by the flush block and `state.json` updated. Overwrite each iteration; contract always reflects latest in-progress state. Long metric-improvement loops are the primary auto-compact risk.
- Preserve at each boundary: RUN_ID (TMPDIR key), STATE_DIR path, program.md path, current iteration#, best metric, best-commit SHA, experiments.jsonl path.
- Clear at R1 start (stale prior run) and at R6 wrap-up, before optional R7.
- Repeated measurement (`metric_repeats` > 1) is resume-safe between runs: `metric-runs.jsonl` is the cursor, so `run_metric_stats.py status` names the next run after a compaction.

</compaction>

<workflow>

<!-- Agent resolution: see _RESEARCH_SHARED/agent-resolution.md -->

## Agent Resolution

**Agent resolution**: load and follow the protocol below. Contains: foundry check + fallback table. If foundry not installed: use table to substitute each `foundry:X` with `general-purpose`. Agents this skill uses: `foundry:sw-engineer`, `foundry:linting-expert`, `foundry:perf-optimizer`, `foundry:solution-architect`, `research:scientist`.

```bash
# loads: compaction-contract.md
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/resolve_shared.py" --persist research-shared --emit agent-resolution.md || exit 1  # timeout: 5000 — cold resolve; every later site reads the research-shared sentinel instead of re-running python
# installed plugin root; source-tree path only when CLAUDE_PLUGIN_ROOT unset
CLAUDE_SKILL_DIR="${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/skills/run"
echo "$CLAUDE_SKILL_DIR" > "${TMPDIR:-/tmp}/research-run-skill-dir-${CSID}"
```

## Default Mode (Steps R1–R7)

Triggered by `run <goal|file.md>`.

**Task tracking**: create tasks R0–R7 at start, all in the same response as the first real tool call. Every later `TaskUpdate` rides with the next real tool call — never a bookkeeping-only turn; the one standalone call is a `completed` right before a long report. Per-iteration R5 subject updates ride with that iteration's next real call. If none of `--researcher`/`--architect`/`--hypothesis`, `TaskUpdate` R0 status `deleted` (no `skipped` status exists). If `--codex` active, create task `R5b: Codex co-pilot (iter ?/max)` status `pending`.

### Step R0: Hypothesis pre-phase (`--researcher` / `--architect` / `--hypothesis`)

**Executes after R2 passes** — section sits first so task names stay stable, but R0 needs R1's `RUN_DIR` (`.experiments/<run-id>`) and must not spawn oracles before R2's clean-git and `--journal` checks fail fast. Resume Mode never spawns oracles (pipeline step 1 reuses the on-disk queue).

If none of `--researcher`/`--architect`/`--hypothesis`, skip R0 (after R2, go to R3).

`--hypothesis <path>` without oracle flags → pipeline steps 1–4 on that file as the queue, no oracle spawn. Queue file is read-only — never written or rewritten.

**Flag combination note**: every oracle self-annotates feasibility (`feasible`/`blocker`/`codebase_mapping` are part of the oracle schema — no separate annotation spawn). `--researcher` alone, `--architect` alone, both together — all valid; both together adds architectural hypotheses alongside research ones.

Follow `modes/hypothesis-pipeline.md`:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CLAUDE_SKILL_DIR < "${TMPDIR:-/tmp}/research-run-skill-dir-${CSID}" 2>/dev/null || CLAUDE_SKILL_DIR=""
IFS= read -r RUN_ID < "${TMPDIR:-/tmp}/research-run-id-${CSID}" 2>/dev/null || RUN_ID=""
[ -n "$RUN_ID" ] || { echo "! BLOCKED — R0: research run-id sentinel missing (R1 not run)"; exit 1; }
echo "RUN_DIR=.experiments/${RUN_ID}"
cat "$CLAUDE_SKILL_DIR/modes/hypothesis-pipeline.md"  # timeout: 5000
```

**Per-iteration hypothesis selection** (when R0 built `RESEARCH_QUEUE` — any of `--researcher`/`--architect`/`--hypothesis` — inside R5 loop): pop next from `RESEARCH_QUEUE`. Append to Phase 2 prompt: "Focus this iteration on testing this hypothesis: `<hypothesis text>`." Queue exhausted → normal ideation, no focus line.

**Per-iteration journal hook** (inside R5, after Phase 7): if `--journal` active, stage one entry for `<RUN_DIR>/journal.md` after EVERY iteration — regardless of outcome: Write the entry alone to `<RUN_DIR>/journal.md.rec` with the Write tool; Phase 8's flush block appends it (never Read-and-rewrite `journal.md`). Entry format: `protocol.md` (companion file, same skill dir). Journals record kept and reverted iterations so ideation agent learns failed approaches.

> loads: protocol.md

**Per-iteration checkpoint write** (after Phase 7): if this iteration popped a `RESEARCH_QUEUE` entry (any R0 flag), stage one line for `<RUN_DIR>/checkpoint.jsonl` per schema in `protocol.md` (companion file, same skill dir): `{iteration, hypothesis_id, metric_before, metric_after, status: "passed"|"rolled_back"}` — Write that single line to `<RUN_DIR>/checkpoint.jsonl.rec` with the Write tool; Phase 8's flush block appends it. Runs started before the rename keep a legacy `checkpoint.json` (same JSONL lines); `--resume` reads it when `checkpoint.jsonl` is absent, and new lines go to `checkpoint.jsonl`.

### Step R1: Load / build config

**`--resume` flag detection**: if `--resume` in args, extract optional program.md path. Jump to `## Resume Mode`. Rest of R1 and R4 skipped; Resume Mode re-runs R2 and R3 itself, then continues the R5 loop.

**`--hypothesis <path>` parsing**: if `--hypothesis` in args, extract path token following it. Verify file exists: `[ -f "$HYPOTHESIS_PATH" ]`. If not found: print `! --hypothesis <path>: file not found` and stop. If found: set `hypothesis_override = true`. The file is a JSONL hypothesis queue (the format `/research:retro` and `/research:sweep` emit) — R0 builds `RESEARCH_QUEUE` from it via `modes/hypothesis-pipeline.md` steps 1–4 (feasibility backfill, sort, checkpoint skip), no oracle spawn; R5 pops one entry per iteration. Never prepend the whole file to a prompt.

**Auto-detect**: first non-flag arg ends in `.md` → parse as program file. Otherwise → text goal.

**Clarification prompt** (`.md` file only): after extracting `.md` path, inspect next token (before `--` flags):

- Absent or starts with `--` → `clarification_prompt = null`
- Quoted string (starts/ends with `"`) → extract as `clarification_prompt`, strip quotes
- Bare unquoted token (no `--`, no `"`) → accept as `clarification_prompt`; print: `ℹ clarification set to "<token>" (tip: quote multi-word hints — e.g. "/research:run program.md \"focus on sort\" --codex")`

After clarification extraction, remaining non-flag tokens (not starting `--`) are unrecognized. For each, print:

```markdown
⚠ Unrecognized argument "<token>" — ignored.
  Known positional args: <program.md path> [clarification]
  Known flags: --resume <program.md>, --team, --compute=local|colab|docker, --colab[=HW], --codex, --researcher, --architect, --journal, --hypothesis <path>, --scientist, --codemap, --no-codemap, --keep "<items>"
  If you meant to override the algo, edit the ## Config block in your program.md (algo: sort) and update ## Metric to match.
  If you meant to set a clarification hint, pass it as a quoted string: "/research:run program.md \"sort improvements\" --codex"
```

**`--keep` extraction + unsupported flag check** — one block: parse `--keep`, then load and follow the unsupported-flag protocol. Supported flags for this skill: `--resume`, `--team`, `--compute`, `--colab`, `--codex`, `--researcher`, `--architect`, `--journal`, `--hypothesis`, `--scientist`, `--codemap`, `--no-codemap`, `--keep`.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# runs under bash — zsh never populates ${BASH_REMATCH[1]}, so --keep "..." was silently resolving empty
IFS= read -r _RUN_ARGS < "${TMPDIR:-/tmp}/sweep-run-args-${CSID}" 2>/dev/null || _RUN_ARGS="$ARGUMENTS"  # sweep S5 cat-loads this file: $ARGUMENTS is then sweep's goal text, not run's args
[ -f "${TMPDIR:-/tmp}/sweep-run-args-${CSID}" ] && _INL=--inlined || _INL=""  # handoff sentinel present = inlined in sweep: sweep's live contract must survive
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/extract-keep-flag.py" research-run "$_RUN_ARGS" $_INL  # timeout: 5000 — parses --keep, clears a stale contract (not when --inlined), persists for Phase 8
# loads: unsupported-flag-protocol.md
IFS= read -r _RESEARCH_SHARED < "${TMPDIR:-/tmp}/research-shared-${CSID}" 2>/dev/null || _RESEARCH_SHARED=""  # warm read (Check 41)
cat "$_RESEARCH_SHARED/unsupported-flag-protocol.md"
```

**Codemap auto-detection** — structural blast-radius context for modules the experiment edits; on by default when codemap installed + index found. `--no-codemap` opts out; `--codemap` is strict (fail if unavailable).

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# writes true/false to research-run-codemap-enabled-${CSID}; strict mode exits 1 (already printed ! BLOCKED) if unavailable
IFS= read -r _RUN_ARGS < "${TMPDIR:-/tmp}/sweep-run-args-${CSID}" 2>/dev/null || _RUN_ARGS="$ARGUMENTS"  # same sweep handoff as the --keep block
CODEMAP_RAW=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/codemap-flag.py" research-run "$_RUN_ARGS") || exit 1
```

> loads: codemap-gates.md

When `CODEMAP_RAW` ≠ `off`:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _RESEARCH_SHARED < "${TMPDIR:-/tmp}/research-shared-${CSID}" 2>/dev/null || _RESEARCH_SHARED=""  # warm read (Check 41)
cat "$_RESEARCH_SHARED/codemap-gates.md"
```

Follow Gate A and Gate B.

**If argument is a `.md` file** — read and parse with these rules:

1. Find each `## <Section>` heading (case-insensitive).
2. Extract first fenced code block following that heading.
3. Parse block as `key: value` lines; multi-value = indented `  - value` items. Paths with spaces: wrap in double quotes.
4. Missing required fields (`command` under `## Metric`/`## Guard`) → stop with error.
5. `agent_strategy: auto` (or omitted) → apply keyword heuristics from `<constants>` to `## Goal` text and metric command.
6. `target` under `## Metric`: `direction: higher` → stop when metric ≥ target; `direction: lower` → stop when metric ≤ target. If `target` omitted, run until `max_iterations`. `direction` omitted → `higher`, print `⚠ ## Metric has no direction — assuming higher`.
7. Recognized keys: `## Metric` `command`, `direction`, `target`; `## Guard` `command`; `## Config` `max_iterations`, `agent_strategy`, `scope_files`, `compute`, `colab_hw`, `sandbox_network`, `metric_repeats`, `min_delta_pct`. Unrecognized keys/headings → warn once, ignore.
8. `## Notes` and `# Program:` title never parsed — human-only. (`# Campaign:` accepted as alias.)
9. `metric_repeats` (integer 1–10, default `METRIC_REPEATS_DEFAULT`) and `min_delta_pct` (number ≥ 0, default `MIN_DELTA_PCT`): invalid value → print `⚠ metric_repeats '<value>' invalid (1–10) — using 1` / `⚠ min_delta_pct '<value>' invalid (≥ 0) — using 0.1` and use the default. `metric_repeats` > 1 turns on repeated measurement (R4, Phase 5, Phase 7 — see `<constants>`).

**If argument is text** — auto-detect `metric_cmd`/`guard_cmd` from goal string and codebase scan (same as P-P1, non-interactive). `config.json` not read. Infer `direction`: `lower` when the goal or `metric_cmd` says reduce, lower, minimize, decrease or faster, or names latency, loss, time, error or memory; else `higher`. Print `ℹ text goal: metric direction <direction> (inferred — use a program.md ## Metric direction to set it)` — retro's significance test and Phase 7 both depend on it. `scope_files` `[]`, every other `config` key at its default.

**`--colab[=HW]` parsing**: `--colab` (no `=`) → `compute = "colab"`, `colab_hw = null`. `--colab=<value>` → `compute = "colab"`, `colab_hw = <value>` (uppercased). Unknown `<value>` (not in `{H100, L4, T4, A100}`) → print `"⚠ Unknown Colab hardware '<value>' — proceeding with default GPU. Known: H100, L4, T4, A100"`, set `colab_hw = null`. `--compute=colab` (no HW) → `compute = "colab"`, `colab_hw = null`.

`colab_hw` in `## Config` sets hardware preference (`H100`, `L4`, `T4`, `A100`); CLI `--colab=HW` overrides.

**`sandbox_network` persistence** — `## Config` `sandbox_network` (closed set `none` | `bridge`) is read by R2 and Phase 5 through a sentinel, since shell variables die between Bash calls. Run exactly one block matching the parsed value; key absent or text goal → `none`; any other value → print `⚠ sandbox_network '<value>' unknown — using none` and run the `none` block. The `SANDBOX_NETWORK` environment variable still overrides the sentinel.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo none > "${TMPDIR:-/tmp}/research-run-sandbox-network-${CSID}"
```

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo bridge > "${TMPDIR:-/tmp}/research-run-sandbox-network-${CSID}"
```

Generate `run-id` = `$(date -u +%Y-%m-%dT%H-%M-%SZ)`. Assign immediately:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
RUN_ID=$(date -u +%Y-%m-%dT%H-%M-%SZ)
RUN_DIR=".experiments/${RUN_ID}"  # hypothesis pipeline + journal outputs (per <constants> note)
STATE_DIR=".experiments/state/${RUN_ID}"  # per-iteration artifacts (state.json, experiments.jsonl, diary.md)
mkdir -p "$RUN_DIR" "$STATE_DIR"  # timeout: 5000 — both dirs created before any Write to either
[ -f .experiments/.gitignore ] || echo '*' > .experiments/.gitignore  # self-ignore: run artifacts never dirty R2 check 1 or Phase 3 porcelain where project doesn't ignore .experiments/
echo "$RUN_ID" > "${TMPDIR:-/tmp}/research-run-id-${CSID}"  # persist for Phase 8 contract write (Check 41: fresh shell)
```

Note: `STATE_DIR` (`.experiments/state/${RUN_ID}/`) is per-iteration artifact dir — distinct from `RUN_DIR`. Both coexist; see `<constants>` block.

Create run directory:

```text
.experiments/state/<run-id>/
  state.json         ← iteration count, best metric, status
  experiments.jsonl  ← one line per iteration
  diary.md           ← human-readable research diary (hypothesis → outcome → decision)
```

Convert `program_file` to absolute path: `realpath "$PROGRAM_FILE"` — Resume Mode matches on absolute path.

Write initial `state.json` (`program_file` = absolute path to `.md` or `null` for text goal):

```json
{
  "run_id": "<run-id>",
  "goal": "<goal>",
  "config": {
    "metric_cmd": "<## Metric command>",
    "guard_cmd": "<## Guard command>",
    "metric": {"direction": "higher", "target": null},
    "scope_files": [],
    "compute": "local",
    "agent_strategy": "auto",
    "max_iterations": 20,
    "sandbox_network": "none",
    "metric_repeats": 1,
    "min_delta_pct": 0.1
  },
  "program_file": "<absolute path to program.md, or null>",
  "iteration": 0,
  "best_metric": null,
  "best_metric_sigma": null,
  "best_metric_runs": null,
  "best_commit": null,
  "status": "initializing",
  "started_at": "<ISO timestamp>",
  "clarification_prompt": null,
  "colab_hw": null,
  "sandbox_mode": "local",
  "flags": {"team": false, "compute": "local", "codex": false, "researcher": false, "architect": false, "journal": false, "hypothesis": null, "scientist": false}
}
```

`config` holds the parsed program (text goal: the auto-detected values above) — never written as `{}`. Its shape is fixed by its readers, so keep it exactly: `/research:fortify` reads flat `metric_cmd`, `guard_cmd` and `compute`; `/research:retro` reads `metric_cmd` and the nested `metric.direction`. Keys absent from the program take the defaults shown (`max_iterations` clamped to 50; `compute` = the resolved compute route). `best_metric_sigma` and `best_metric_runs` stay `null` unless `metric_repeats` > 1.

`flags` records the parsed CLI flags of this invocation (`compute` = resolved `local`|`colab`|`docker`; `hypothesis` = path or `null`); `--resume` restores them and flags passed with the resume override them. Re-write `flags` whenever R2 or R3 changes a value.

Note: status is `"initializing"` until all R2 precondition checks pass — resume treats `"initializing"` as failed-init, not active run. Update to `"running"` at end of R2 (after all checks pass).

### Step R2: Precondition checks

Run all checks before touching code. Fail fast with clear message. **Order**: checks 1–2 and 5–11, then the sandbox/timeout init and `sandbox_mode` blocks below, then checks 3–4 (they need the compute route), then R0 when active, then R3.

01. **Clean git**: `git status --porcelain` → must be empty. If dirty: print dirty files and stop. Exact command excludes run artifacts: `git status --porcelain -- . ':(exclude).experiments' ':(exclude).temp'`.
02. **Not detached HEAD**: `git rev-parse --abbrev-ref HEAD` → must not be `HEAD`.
03. **Metric command numeric**: run `metric_cmd` once through the Phase 5 compute route (`modes/phase5-metric.md`: docker sandbox, Colab MCP, or local) — same environment as iterations (with `--colab`, the Phase 5 code-identity check runs first); parse stdout for float. If no float: show output and stop. Docker image is `python:3.11-slim` without project deps (`modes/compute-docker.md`) — a metric needing them fails here, not at iteration 1.
04. **Guard passes**: run `guard_cmd` once through the same route; must exit 0. If fails: show output and stop.
05. **`--colab` check**: verify `mcp__colab-mcp__runtime_execute_code` available. If not, print setup instructions (see Colab MCP section) and stop. If `--colab=HW` (`colab_hw` non-null): print: `  Hardware requested: --colab=<colab_hw>. Ensure your Colab notebook running with <colab_hw> GPU.`
06. **`--codex` check**: distinguish the installed-and-enabled bridge target from absence. `claude` CLI not needed — run `python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/check_bridge.py" --status` (`timeout: 5000`; reads plugin registry + settings). `absent` → print `⚠ bridge@borda-ai-rig not installed. Install it from the Borda AI Rig marketplace.` and **stop**. `disabled` → print `⚠ bridge@borda-ai-rig is disabled. Enable it and reload plugins.` and **stop**. `available` → continue.
07. **`compute: docker` check**: run `docker ps` via Bash (`timeout: 5000`). If non-zero: print `⚠ Docker daemon not running. Start Docker Desktop and retry.` and **stop**.
08. **Flag conflict**: if `--colab` and `--compute=docker` both active: print `⚠ --colab and --compute=docker are mutually exclusive. Use one or the other.` and **stop**.
09. **`--colab` + `--codex` compatibility note** (non-blocking): if both flags active, print `ℹ --colab + --codex active: Codex Phase 2c will receive colab_hw context so generated code can target the right GPU (H100/T4 bf16 vs fp16). Phase 5 metric verification runs through Colab MCP as usual.` and continue. Pass `colab_hw` to Codex spawn prompt (Phase 2c — see `modes/codex-copilot.md`).
10. **`--journal` prerequisite**: verify `--researcher`/`--architect` also set. If neither: print `⚠ --journal requires --researcher or --architect — omit --journal or add a hypothesis pipeline flag.` and **stop**.
11. **`jq` on PATH**: run `command -v jq` via Bash (`timeout: 5000`). Empty → print `! MISSING — jq not on PATH; run reads state.json with jq (Phase 0 iteration counter, Phase 8 contract). Install jq and retry.` and **stop** — without it Phase 0 misreports the missing tool as `state.json unreadable`.

**`--codex-delegation` warning** (non-blocking): `codex-delegation.md` ships inside this plugin's own `skills/_shared/`, so R7 needs no other plugin installed. Verify it resolves:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _RESEARCH_SHARED < "${TMPDIR:-/tmp}/research-shared-${CSID}" 2>/dev/null || _RESEARCH_SHARED=""  # warm read (Check 41)
[ -f "$_RESEARCH_SHARED/codex-delegation.md" ] || echo "⚠ codex-delegation.md not found under $_RESEARCH_SHARED — R7 Codex delegation will be skipped; reinstall the research plugin."
```

Set `CODEX_DELEGATION_AVAILABLE=true` if found, `false` otherwise. Continue regardless.

**Initialize sandbox + timeout variables** (after checks 1–2 and 5–11, before checks 3–4 — constants YAML block not auto-exported to bash; assign explicitly with `${VAR:-default}` to honour environment overrides):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _CFG_NET < "${TMPDIR:-/tmp}/research-run-sandbox-network-${CSID}" 2>/dev/null || _CFG_NET=""
SANDBOX_NETWORK="${SANDBOX_NETWORK:-${_CFG_NET:-none}}"  # env override > program.md Config sentinel (R1) > none
# Verify timeout — 120s local, 300s Colab per <constants>; bash overrides via VERIFY_TIMEOUT_SEC env var
if [ "${compute:-local}" = "colab" ]; then
    VERIFY_TIMEOUT_SEC="${VERIFY_TIMEOUT_SEC:-300}"
else
    VERIFY_TIMEOUT_SEC="${VERIFY_TIMEOUT_SEC:-120}"
fi
VERIFY_TIMEOUT_MS=$((VERIFY_TIMEOUT_SEC * 1000))
echo "$VERIFY_TIMEOUT_MS" > "${TMPDIR:-/tmp}/research-run-verify-timeout-ms-${CSID}"  # shell vars die per Bash call; modes/ blocks reload this
echo "SANDBOX_NETWORK=$SANDBOX_NETWORK VERIFY_TIMEOUT_MS=$VERIFY_TIMEOUT_MS"
# Ideation Agent() calls run in background — spawn, end turn, no filler call; on each notification check its output file, mark timed_out (⏱) if empty.
```

**Initialize `sandbox_mode`**:

- `compute: docker` (daemon check passed in step 7) → `sandbox_mode = "docker"`. Print: `sandbox: Docker daemon reachable — sandbox mode active`
- All other cases (`compute: local`, `compute: colab`) → `sandbox_mode = "local"`

**Update state.json status to `"running"`** — write only after ALL checks above pass. Resume treats `"initializing"` as failed-init and skips such runs.

### Step R3: Select ideation agent

Apply `agent_strategy` mapping from `<constants>`. If `auto`, apply keyword heuristics to `## Goal` text and `metric_cmd` (precedence per `<constants>`). Log selected agent to `state.json`.

### Step R4: Establish baseline (iteration 0)

Run `metric_cmd` and `guard_cmd` through the same compute route as Phases 5–6 (`modes/phase5-metric.md`) — a host baseline against sandboxed iterations measures the environment, not the change. With `--colab`, run Phase 5's code-identity check (`modes/phase5-metric.md`) before the baseline `metric_cmd` — a stale runtime checkout would record a baseline for unrelated code; mismatch → `! BLOCKED`, stop. Parse metric value. Create `experiments.jsonl` with the Write tool holding this baseline record as its only line — the file does not exist yet. Every later record goes through Phase 8's `.rec` staging and flush block; this file is never rewritten:

```json
{
  "iteration": 0,
  "commit": "<HEAD sha>",
  "metric": 0.0,
  "delta": 0.0,
  "guard": "pass",
  "status": "baseline",
  "description": "baseline",
  "agent": null,
  "confidence": null,
  "timestamp": "<ISO>",
  "files": []
}
```

Update `state.json`: `best_metric = <baseline>`, `best_commit = <HEAD sha>`.

**`metric_repeats` > 1** — measure the baseline that many times. After each `metric_cmd` run (same route, its own call), Write one line `{"iteration":0,"metric":<float or null>,"exit":<code>}` to `.experiments/state/<run-id>/metric-runs.jsonl.rec` with the Write tool, then run the block below. `RUN_METRIC_NEXT_RUN=<k>` → run again; `RUN_METRIC_FAILED` → stop the run (an unmeasurable baseline cannot anchor Phase 7); `RUN_METRIC_DONE` → the baseline record's `metric` is the printed mean plus `"metric_sigma"` and `"metric_runs"` fields, and `state.json` gets `best_metric` = mean, `best_metric_sigma` = sigma (`null` for `n/a`), `best_metric_runs` = runs.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r RUN_ID < "${TMPDIR:-/tmp}/research-run-id-${CSID}" 2>/dev/null || RUN_ID=""
[ -n "$RUN_ID" ] || { echo "! BLOCKED — R4: research run-id sentinel missing; metric-runs.jsonl.rec left in place"; exit 1; }
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/append_ledger.py" ".experiments/state/$RUN_ID/metric-runs.jsonl"  # timeout: 5000
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/run_metric_stats.py" status ".experiments/state/$RUN_ID" --iteration 0  # timeout: 5000 — repeat count read from state.json config
```

Print: `Baseline: <metric_cmd key> = <value>`.

Write initial diary header to `.experiments/state/<run-id>/diary.md`:

```markdown
# Research Diary — [Goal]

**Run**: [Run ID]
**Started**: [Start timestamp]
**Baseline**: [Metric key] = [Baseline value]

---
```

Then proceed to R5.

### Step R5: Iteration loop

**`--team` mode**: If `--team` active, follow `modes/team.md` and execute Phases A–D in place of standard iteration loop below.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CLAUDE_SKILL_DIR < "${TMPDIR:-/tmp}/research-run-skill-dir-${CSID}" 2>/dev/null || CLAUDE_SKILL_DIR=""
cat "$CLAUDE_SKILL_DIR/modes/team.md"  # timeout: 5000
```

**`--team` + `--hypothesis` combination**: combinable. `--hypothesis` changes only R0's queue source — `modes/hypothesis-pipeline.md` steps 1–4 build `RESEARCH_QUEUE` from the file, no oracle spawn, file stays read-only. Team Phase A still runs its axis agents; Phase B merges every `RESEARCH_QUEUE` entry alongside the axis hypotheses with the same backfill as R0 oracle entries (team.md Phase B step 1).

For each iteration `i` from 1 to `max_iterations`:

**Phase overview** (all phases run per iteration):

| Phase | Name | Trigger / description |
| -- | -- | -- |
| 0 | Print header | Always — print `[→ Iter N/max · starting]`; TaskUpdate R5 subject with current iteration |
| 1 | Build context | Always — build compact context: git log, JSONL history, recent diff |
| 2 | Propose change | Always — spawn specialist agent: read code, research, investigate, generate hypothesis with optional sandbox scripts |
| 2a | Sandbox validate | `compute: docker` only — run agent's exploratory scripts in Docker sandbox (read-only mount) |
| 2b | Apply change | `compute: docker` only — agent applies validated proposal to real codebase via Write/Edit tools only; no Bash on codebase |
| 2c | Codex co-pilot | `--codex` only — required each iteration up to `MAX_CODEX_RUNS`; after cap reached, continue without Codex |
| 3 | Verify files | Always — assert HEAD unchanged, check `git status --porcelain`; skip to Phase 7a if no files changed (no-op) |
| 4 | Commit change | Always — stage modified files and commit before verifying metric |
| 5 | Verify metric | Always — run `metric_cmd` via `compute` mode (local/colab/docker); revert on timeout |
| 6 | Run guard | Always — run `guard_cmd` via `compute` mode; record pass or fail |
| 7 | Evaluate outcome | Always — keep, rework, or revert based on metric + guard result |
| 7a | Write diary | Always — stage one structured entry in `diary.md.rec` recording hypothesis, outcome, decision rationale |
| 8 | Write log | Always — append JSONL record, update `state.json`, print iteration summary, TaskUpdate R5 with result (record staged in `experiments.jsonl.rec`; the flush block appends it with every other staged record) |
| 9 | Progress checks | Always — summary every SUMMARY_INTERVAL, stuck detection, diminishing-returns warn, early-stop check |

**Command execution rules** (apply to ALL phases running external commands):

1. **Use Bash tool `timeout` parameter**: Never shell `timeout` wrapper. Pass `timeout: <ms>` on Bash tool call itself. (Compound commands are already barred globally — see `claude-config.md` §Directory Navigation Commands.)
2. **No inline multi-line Python**: Python logic >3 lines → write to `.experiments/state/<run-id>/scripts/script-<i>.py` via Write tool, execute with `python <path>` or `uv run python <path>`. Two triggers Claude Code always flags: (a) `=([0-9.]+)` inside `-c "..."` (false Zsh substitution); (b) multi-line `-c "..."` with `#`-prefixed comment lines. Writing to file sidesteps both.
3. **No Zsh constructs**: Never use `=()`, `<()`, `>()` in Bash commands — even inside quoted strings; Claude Code scans raw command text.
4. **Local exploratory scripts writing to real files** (scanning config combos, patching JSON, temp overrides): write to `.experiments/state/<run-id>/scripts/`, run locally with `python <path>`. Legitimately modify project files — NOT in Docker sandbox.
5. **Docker sandbox** (when available — see Phase 2a): Phases 4–6 route `metric_cmd`/`guard_cmd` through Docker when `compute: docker`. Phase 2a: read-only hypothesis scripts in sandbox. Scripts writing to project files always run locally.
6. **One change per iteration**: Never batch-loop over config variants/combos in single Bash/Python call. Each variant = one campaign iteration — loop/measure/compare is campaign framework's job, not ideation agent's.

#### Phase 0 — Print header

Print iteration header, update R5 task:

```text
[→ Iter N/max_iterations — best so far: <best_metric> (Δ<best_delta_pct>% vs baseline)]
```

TaskUpdate R5 subject: `R5: Iteration N/max_iterations — running`

Derive the iteration number and start SHA once per iteration — every later block reloads them from these sentinels:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r RUN_ID < "${TMPDIR:-/tmp}/research-run-id-${CSID}" 2>/dev/null || RUN_ID=""
[ -n "$RUN_ID" ] || { echo "! BLOCKED — Phase 0: research run-id sentinel missing; run R1 or --resume first"; exit 1; }
# state.json.iteration = last finished iteration until Phase 8 writes i → +1 holds through Phase 7
DONE_ITER=$(jq -r '.iteration // 0' ".experiments/state/${RUN_ID}/state.json") || { echo "! BLOCKED — Phase 0: state.json unreadable"; exit 1; }  # timeout: 5000
I=$((DONE_ITER + 1))
echo "$I" > "${TMPDIR:-/tmp}/research-run-iter-${CSID}"
git rev-parse HEAD > "${TMPDIR:-/tmp}/research-run-iter-sha-${CSID}"  # Phase 3 asserts the agent left HEAD here
echo "iteration=$I"
```

#### Phase 1 — Build context

Build context for ideation agent, write to file — do NOT accumulate inline in main context:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r RUN_ID < "${TMPDIR:-/tmp}/research-run-id-${CSID}" 2>/dev/null || RUN_ID=""  # reload (Check 41)
IFS= read -r I < "${TMPDIR:-/tmp}/research-run-iter-${CSID}" 2>/dev/null || I=""
[ -n "$RUN_ID" ] && [ -n "$I" ] || { echo "! BLOCKED — Phase 1: run-id or iteration sentinel missing (run the Phase 0 block first)"; exit 1; }
git log --oneline -10 >.experiments/state/${RUN_ID}/context-${I}.md  # timeout: 3000
tail -10 .experiments/state/${RUN_ID}/experiments.jsonl >>.experiments/state/${RUN_ID}/context-${I}.md  # timeout: 5000
# Fresh repos have <5 commits — fall back to full HEAD diff when shallow
if [ "$(git rev-list HEAD --count 2>/dev/null)" -gt 5 ]; then
    git diff --stat HEAD~5 HEAD >>.experiments/state/${RUN_ID}/context-${I}.md  # timeout: 3000
else
    git diff --stat HEAD >>.experiments/state/${RUN_ID}/context-${I}.md  # timeout: 3000
fi
```

**Codemap structural context** (only if `CODEMAP_ENABLED=true` — re-read from `${TMPDIR:-/tmp}/research-run-codemap-enabled-${CSID}`). Cat once, first iteration only — the file is static and stays in context; re-cat only if it is no longer in context (e.g. after a compaction). Re-catting every iteration re-bills ~800 tok × N iterations for identical text:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
CODEMAP_SENTINEL_SLUG=research-run
IFS= read -r CODEMAP_ENABLED < "${TMPDIR:-/tmp}/${CODEMAP_SENTINEL_SLUG}-codemap-enabled-${CSID}" 2>/dev/null || CODEMAP_ENABLED="false"
IFS= read -r _RESEARCH_SHARED < "${TMPDIR:-/tmp}/research-shared-${CSID}" 2>/dev/null || _RESEARCH_SHARED=""  # warm read (Check 41)
if [ "$CODEMAP_ENABLED" = "true" ]; then cat "$_RESEARCH_SHARED/codemap-context.md"; fi
```

Execute its block. Leave `TARGET_MODULE`/`TARGET_FN` empty for the global `central` blast-radius baseline, or set `TARGET_MODULE` to the module the experiment edits (from `## Config`) for importer/coverage queries. Append output to `context-${I}.md` under a `## Structural Context (codemap-py)` heading so the Phase 2 ideation agent sees blast-radius before proposing edits.

Codemap output non-empty: also append the reuse gate from `codemap-context.md` above verbatim and this **codemap-first protocol** directly below it in `context-${I}.md` (own copy — self-contained, no cross-plugin reference), so the Phase 2 spawn prompt's "read `context-<i>.md`" instruction carries it to the ideation agent: (1) **Skill-first** — use the Structural Context above before any Grep/Glob/Read aimed at imports, callers, or test coverage for a symbol already listed there. (2) **Query choice** — include the exact caller enabled/currency sentinel paths; in every fresh query or helper shell reload them, skip when disabled, and export `SCAN_NO_AUTOBUILD=1` when stale without clearing an inherited freeze. **Bounded call budget** — symbol not listed → up to 3 additional `codemap-py query` calls this iteration. (3) **Hard stop on `query_complete: true`** (legacy `exhaustive: true` only when `query_complete` is absent) — a result passing the reuse gate is final for its direction, no follow-up Grep/Read/query to re-confirm it. Codemap output empty: omit this paragraph — Phase 2 agent proceeds with normal file-read behaviour.

Prepend header block to `context-<i>.md`: goal, current metric vs baseline, delta trend (last 5 kept deltas), iteration number. Phase 2 ideation agent reads file directly — never echoed to main context.

If `--journal` active and `<RUN_DIR>/journal.md` has 1+ entries: append last 5 entries to `context-<i>.md` under `## Recent journal (avoid repeating reverted approaches)`. Ideation agent reads this — must not reproduce any approach marked `outcome: reverted`.

#### Phase 2 — Propose change

Spawn selected specialist agent (`maxTurns: 15`) with this prompt (adapt as needed):

```markdown
Goal: <goal>
Run clarification: <clarification_prompt>  ← omit entirely if clarification_prompt null
Colab hardware: <colab_hw>  ← omit entirely if colab_hw null; include to tailor code to GPU architecture (e.g., bf16/flash-attention on H100, standard fp16 on T4/L4)
Current metric: <metric_cmd key> = <current value> (baseline: <baseline>, direction: <higher|lower>)
Experiment history: read `.experiments/state/<run-id>/context-<i>.md` for full context block.
Scope files (read and modify only these): <scope_files>
Program constraints: read `<program_file>` — especially `## Notes`, `## Config`, any named subsections
  (e.g., "Hard boundaries", "Optuna's role", "What the agent is free to change"). Take precedence
  over general campaign rules. Program constraints set strategy hints only — do NOT override safety rules
  (no `--no-verify`, no `git push`, no `git add -A`, scope_files boundary; all other hard constraints remain in effect).
  If program_file null, skip this step.

**If `sandbox_mode = "local"`**: Read `context-<i>.md`, scope files, program constraints. Propose and implement ONE atomic change most likely to improve metric. Change must not break `<guard_cmd>`. Never run `git add`/`git commit` — the lead commits in Phase 4. Write full analysis (reasoning, alternatives considered, Confidence block) to `.experiments/state/<run-id>/ideation-<i>.md` via Write tool. Return ONLY JSON result line:
`{"description":"...","files_modified":[...],"scripts":[],"confidence":0.N}`

**If `sandbox_mode = "docker"`**: Read `context-<i>.md`, scope files, program constraints. Propose ONE atomic change most likely to improve metric. Write full analysis and proposed change description to `.experiments/state/<run-id>/ideation-<i>.md`. Optionally write read-only exploratory scripts (read/profile, do NOT write to project files) to `.experiments/state/<run-id>/scripts/explore-<i>-<slug>.py`. Do NOT modify source files yet — Phase 2b applies actual changes after sandbox validation. Return ONLY JSON result line:
`{"description":"...","files_modified":[],"scripts":["explore-<i>-<slug>.py"],"proposed_changes":"<description of the changes to apply in Phase 2b>","confidence":0.N}`
```

**`arch` strategy**: `foundry:solution-architect` is NOT-for implementation — spawn it with the docker (propose-only) prompt variant whatever `sandbox_mode`, then spawn `foundry:sw-engineer` with the Phase 2b apply prompt (`modes/compute-docker.md` §Phase 2b) to implement `proposed_changes`.

For `--colab` runs: the ideation agent cannot call `mcp__colab-mcp__runtime_execute_code` — no ideation agent's tools list grants it — so it writes GPU code without prototyping it on the runtime; Phase 5 measures the committed change through Colab MCP. **Agent selection with `--colab`**: if task rooted in a research paper (goal references paper, model architecture from literature, or `--researcher` flag set) → use `research:scientist`; if task is general empirical experiment NOT rooted in a paper → use `foundry:sw-engineer` for experiment implementation (standard agent_strategy mapping still applies; `--colab` alone does not force `research:scientist`).

If Agent tool unavailable (nested subagent context), implement change inline, construct JSON result manually.

#### Phase 2a — Sandbox validate (`sandbox_mode = "docker"` only)

> loads: compute-docker.md
>
> Follow `modes/compute-docker.md` — full Phase 2a and 2b logic for docker sandbox. Skip entire file if `sandbox_mode = "local"`. Cat once, first iteration only — static content stays in context; re-cat only if no longer in context (e.g. after a compaction).

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CLAUDE_SKILL_DIR < "${TMPDIR:-/tmp}/research-run-skill-dir-${CSID}" 2>/dev/null || CLAUDE_SKILL_DIR=""
cat "$CLAUDE_SKILL_DIR/modes/compute-docker.md"  # timeout: 5000
```

#### Phase 2b — Apply change (`sandbox_mode = "docker"` only)

Skip if `sandbox_mode = "local"` — handled in compute-docker.md above.

#### Phase 2c — Codex co-pilot (`--codex` only)

Follow `modes/codex-copilot.md` — contains full Phase 2c logic, cost-bounded gate, Codex dispatch prompt, outcome handling, and stuck escalation. Cat once, first `--codex` iteration only — static content stays in context; re-cat only if no longer in context (e.g. after a compaction).

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CLAUDE_SKILL_DIR < "${TMPDIR:-/tmp}/research-run-skill-dir-${CSID}" 2>/dev/null || CLAUDE_SKILL_DIR=""
cat "$CLAUDE_SKILL_DIR/modes/codex-copilot.md"  # timeout: 5000
```

#### Phase 3 — Verify files changed

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r ITER_START_SHA < "${TMPDIR:-/tmp}/research-run-iter-sha-${CSID}" 2>/dev/null || ITER_START_SHA=""
IFS= read -r RUN_ID < "${TMPDIR:-/tmp}/research-run-id-${CSID}" 2>/dev/null || RUN_ID=""
[ -n "$ITER_START_SHA" ] && [ -n "$RUN_ID" ] || { echo "! BLOCKED — Phase 3: iteration start SHA or run-id sentinel missing (run the Phase 0 block first)"; exit 1; }
[ "$(git rev-parse HEAD)" = "$ITER_START_SHA" ] || { echo "⚠ agent committed during Phase 2 — folding its commits back into the tree for the Phase 4 experiment commit"; git reset --soft "$ITER_START_SHA"; }  # timeout: 5000
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/check_iteration_scope.py" --state-json ".experiments/state/${RUN_ID}/state.json"  # timeout: 15000 — porcelain change set vs scope_files + metric/guard script files; exit 3 = violation, 1 = unreadable
```

The JSON line's `changed` comes from `git status --porcelain` (tracked edits and created files — `git diff --stat` misses the latter), run artifacts excluded.

- Exit 0, `changed` empty → no-op: `status: no-op`, skip to Phase 7a — Phase 8 writes the iteration's single record.
- Exit 0, `changed` non-empty → these paths are the change set Phase 4 stages. `checked: false` (text goal, no program file) → print `⚠ scope unchecked — no program file` and proceed.
- Exit 3 → scope violation (path outside `scope_files`, or a file named by `metric_cmd`/`guard_cmd`): `git restore` the tracked `changed` paths, delete only `untracked` paths the agent reported in `files_modified` (never `git clean`; list any other), `status: scope-violation`, skip to Phase 7a.
- Exit 1 → stop the run — never commit an unchecked change set.

#### Phase 4 — Commit change

Stage only the Phase 3 porcelain paths (never `git add -A`; never the agent's `files_modified` alone — it can omit created files). `<N>` is the `iteration=` value the Phase 0 block printed — the revert guard compares the subject against that sentinel and stops with `! BLOCKED` on any other number:

```bash
git add -- <Phase 3 porcelain paths>  # timeout: 3000
git commit -m "experiment(optimize/i<N>): <description>"  # timeout: 90000
```

If pre-commit hooks fail:

- Delegate to `foundry:linting-expert`: provide failing hook output and modified files; ask to fix. Max 2 attempts.
- If still failing after 2 attempts: `git restore --staged` + `git restore` the tracked Phase 3 paths and delete the Phase 3 `untracked` paths the agent reported (never `git clean`) — discard restricted to iteration scope; `status: hook-blocked`, skip to Phase 7a.

Commit fails for any other reason (no hook output — signing, identity, empty index): same cleanup, `status: commit-failed`, skip to Phase 7a. Never run Phases 5–7 on an uncommitted tree — the revert guard only recognises an `experiment(optimize/i<N>):` HEAD.

#### Phase 5 — Verify metric

> loads: phase5-metric.md # also loads: codex-copilot.md, colab-setup.md, compute-docker.md, hypothesis-pipeline.md, report.md, resume.md, team.md
>
> Follow `modes/phase5-metric.md` — metric verification logic for docker, local, and colab sandbox modes. Cat once, first iteration only — static content stays in context; re-cat only if no longer in context (e.g. after a compaction).

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CLAUDE_SKILL_DIR < "${TMPDIR:-/tmp}/research-run-skill-dir-${CSID}" 2>/dev/null || CLAUDE_SKILL_DIR=""
cat "$CLAUDE_SKILL_DIR/modes/phase5-metric.md"  # timeout: 5000
```

#### Phase 6 — Run guard

**If `sandbox_mode = "docker"`**: run `guard_cmd` through the same wrapper call as Phase 5 (`docker_sandbox_run.py --mode verify "$GUARD_CMD"`: same image, mounts, caps, network and quotas `--cpus 2 --memory 2g --pids-limit 512`; a fresh container, not Phase 5's). Check exit code only.

**If `sandbox_mode = "local"`**: run `guard_cmd` directly.

Record pass (exit 0) or fail (non-zero).

#### Phase 7 — Evaluate outcome

Top-to-bottom; **first match wins**.

| Condition | Action |
| -- | -- |
| metric improved AND guard fail | Rework: re-spawn agent with guard failure output. Max `GUARD_REWORK_MAX` (2) attempts. Each attempt folds into the iteration commit — `git add -- <changed paths>` + `git commit --amend --no-edit` (one `experiment(optimize/i<N>)` commit per iteration, so one revert undoes every attempt) — then re-runs Phases 5–6 and re-enters this table: the kept metric is the post-rework one. If still failing after all rework attempts: revert (`git revert HEAD --no-edit`); diary status = `"reverted"`, decision = `"Guard failed after GUARD_REWORK_MAX rework attempts — reverted"`. |
| metric improved AND gain < `min_delta_pct` AND change > 50 lines | Refresh sentinel; discard: `git revert HEAD --no-edit`. (Line count computed via `CHANGE_LINES` — see note below table.) |
| metric improved AND guard pass | Keep commit. Update `state.json`: `best_metric`, `best_commit` (`metric_repeats` > 1: also `best_metric_sigma`, `best_metric_runs` from the decide line). "Improved" = `new_metric > best_metric` when `direction: higher`; `new_metric < best_metric` when `direction: lower`; with `metric_repeats` > 1, only when the decide block below prints `RUN_METRIC_IMPROVED=1`. |
| no improvement | Refresh sentinel; revert: `git revert HEAD --no-edit`. |

**`metric_repeats` > 1 — decide before the table** (and again after every rework's re-measurement). `metric_repeats` = 1 skips this block: one sample against `best_metric`, as above.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r RUN_ID < "${TMPDIR:-/tmp}/research-run-id-${CSID}" 2>/dev/null || RUN_ID=""
IFS= read -r I < "${TMPDIR:-/tmp}/research-run-iter-${CSID}" 2>/dev/null || I=""
[ -n "$RUN_ID" ] && [ -n "$I" ] || { echo "! BLOCKED — Phase 7: run-id or iteration sentinel missing (run the Phase 0 block first)"; exit 1; }
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/run_metric_stats.py" decide ".experiments/state/$RUN_ID" --iteration "$I"  # timeout: 5000 — gain must beat max(min_delta_pct, 2σΔ); best values come from state.json
```

`RUN_METRIC_IMPROVED=1` → "metric improved" holds and the iteration's metric is the printed `mean`; `=0` → "no improvement". A guard rework re-measures the amended commit with Phase 5's repeated runs (a fresh block of `metric_repeats` runs under the same iteration), then re-runs this block.

**Line count computation** (for "gain < `min_delta_pct` AND change > 50 lines" row): run before evaluating the condition:

```bash
DIFF_SUMMARY=$(git diff --stat HEAD~1..HEAD | tail -1)  # timeout: 3000
INSERTIONS=$(echo "$DIFF_SUMMARY" | grep -oE '[0-9]+ insertion' | grep -oE '[0-9]+' || echo 0)
DELETIONS=$(echo "$DIFF_SUMMARY" | grep -oE '[0-9]+ deletion' | grep -oE '[0-9]+' || echo 0)
CHANGE_LINES=$(( INSERTIONS + DELETIONS ))
echo "CHANGE_LINES=$CHANGE_LINES (insertions=$INSERTIONS deletions=$DELETIONS)"
```

`git revert HEAD --no-edit` — never `git reset --hard` (preserves history, not in deny list).

**Double-revert guard** (ADV-H19) — Phase 7 rework→revert can collide with a partial Phase 5 timeout revert performed in the same iteration. Always check before issuing the revert; `modes/phase5-metric.md` calls the same script for its timeout revert.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r I < "${TMPDIR:-/tmp}/research-run-iter-${CSID}" 2>/dev/null || I=""  # written by Phase 0
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/revert_iteration_commit.py" --iteration "$I" --phase 7  # timeout: 15000 — reverts HEAD only if its subject starts with `experiment(optimize/i${I}):` (revert subjects embed the original, so never a substring match); unset $I → ! BLOCKED, exit 1
```

The guard fires on `metric improved AND guard fail` (after `GUARD_REWORK_MAX` attempts exhausted), `no improvement`, and `gain < min_delta_pct AND change > 50 lines` paths — any path that issues a revert after Phase 5 may have already reverted. Rework amends the iteration commit (table above), so one revert covers every attempt. HEAD carrying another iteration's `experiment(optimize/i<M>):` subject is never skipped: the script prints `! BLOCKED`, reverts nothing and exits 1 — fix the subject, then re-run the guard.

#### Phase 7a — Write diary

After Phase 7 decision, stage one entry for `diary.md`: Write the entry alone to `.experiments/state/<run-id>/diary.md.rec` with the Write tool. Phase 8's flush block appends it — never Read-and-rewrite `diary.md`:

```markdown
## Iteration N — <ISO timestamp>

**Hypothesis**: <agent's description from Phase 2 JSON — the proposed change and expected improvement>

**Outcome**: <metric_key> = <value> (Δ<delta>% vs baseline) — <kept|reverted|rework|no-op|hook-blocked|commit-failed|scope-violation|sandbox-failed|timeout>

**Decision**: <one sentence: why the outcome was accepted or rejected — e.g. "Metric improved 1.2% with guard passing" or "Reverted: metric regressed by 0.5%" or "Guard failed after 2 rework attempts">

---
```

For `no-op` iterations (no file changes):

```markdown
## Iteration N — <ISO timestamp>

**Hypothesis**: <description> — no files modified

**Outcome**: no-op

**Decision**: Skipped (no changes made)

---
```

#### Phase 8 — Write log

Stage one JSONL record for `experiments.jsonl` (same schema as baseline record in Step R4, plus `ideation_source`; `metric_repeats` > 1: `metric` is the measured mean, plus `"metric_sigma"` and `"metric_runs"` from the decide line): Write that single line to `.experiments/state/<run-id>/experiments.jsonl.rec` with the Write tool:

```json
{
  "iteration": 1,
  "commit": "<sha of experiment commit or revert>",
  "metric": 0.0,
  "delta": 0.0,
  "guard": "pass|fail",
  "status": "kept|reverted|rework|no-op|hook-blocked|commit-failed|scope-violation|sandbox-failed|timeout|codex-no-op",
  "description": "<agent description>",
  "agent": "<agent type>",
  "confidence": 0.0,
  "timestamp": "<ISO>",
  "files": [],
  "ideation_source": "claude"
}
```

`ideation_source`: two values, no combined one — `"codex"` when Phase 2c (Codex) changed any path in the committed change set (even on top of Claude's edit), `"claude"` otherwise (`modes/codex-copilot.md` §Phase 2c).

Flush every staged record — this iteration's `experiments.jsonl`, `diary.md`, and, when their flags are active, `checkpoint.jsonl` and `journal.md` — in one fixed block. `append_ledger.py` appends each staged `.rec`, supplies a trailing newline the Write tool may have omitted, and deletes the `.rec`; a ledger with nothing staged is a no-op:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _RUN_ID < "${TMPDIR:-/tmp}/research-run-id-${CSID}" 2>/dev/null || _RUN_ID=""
[ -n "$_RUN_ID" ] || { echo "! BLOCKED — research run-id sentinel missing; staged iteration records left in place"; exit 1; }
for _LEDGER in ".experiments/state/$_RUN_ID/experiments.jsonl" ".experiments/state/$_RUN_ID/diary.md" ".experiments/$_RUN_ID/checkpoint.jsonl" ".experiments/$_RUN_ID/journal.md"; do
    python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/append_ledger.py" "$_LEDGER"  # timeout: 5000
done
```

Update `state.json`: `iteration = i`, `status = running`.

Print iteration summary:

```text
[✓ Iter N/max — <kept|reverted|no-op|...> · metric=<value> (Δ<delta>%) · agent=<agent_type>]
```

TaskUpdate R5 subject: `R5: Iter N/max — last: <status>, best: <best_metric>`

```bash
# compaction contract — overwritten each iteration, always latest state (compaction-contract.md §Lifecycle)
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _RUN_ID < "${TMPDIR:-/tmp}/research-run-id-${CSID}" 2>/dev/null || _RUN_ID=""
_STATE_JSON=".experiments/state/${_RUN_ID}/state.json"
_ITER=$(jq -r '.iteration // 0' "$_STATE_JSON" 2>/dev/null || echo "?")
_BEST=$(jq -r '.best_metric // "?"' "$_STATE_JSON" 2>/dev/null || echo "?")
_PROG=$(jq -r '.program_file // ""' "$_STATE_JSON" 2>/dev/null || echo "")
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/write_skill_contract.py" --keep-sentinel research-run-keep-items "research:run" "iteration-loop (after iter ${_ITER})" ".experiments/${_RUN_ID}" "state-json=${_STATE_JSON}, program=${_PROG}, iter=${_ITER}, best-metric=${_BEST}, cat-once-files=_shared/codemap-context.md + modes/compute-docker.md + modes/codex-copilot.md + modes/phase5-metric.md (re-cat after compaction only)" "continue R5 from iter $(( _ITER + 1 )) or proceed to R6 when loop done" -- calls_ahead=30 "drop=iteration diff, metric and guard output, ideation prompts"  # timeout: 5000
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/compaction_boundary.py"  # timeout: 60000 — prints the Boundary step, then the foundry gate decision; follow it
```

#### Phase 9 — Progress checks

- **Summary every SUMMARY_INTERVAL iterations**: print compact table (iteration, metric, delta, status) for last N iterations.
- **Stuck detection**: if last `STUCK_THRESHOLD` entries all have `status: reverted|no-op|hook-blocked|commit-failed|scope-violation|sandbox-failed|timeout`, trigger escalation (see `<constants>`). Log escalation action.
- **Diminishing returns**: if last `DIMINISHING_RETURNS_WINDOW` kept entries each improved < 0.5%, warn and suggest stopping. No auto-stop — user decides.
- **Early stop**: if `target` set, stop when metric crosses it. Mark `state.json` `status: goal-achieved`.
- **Context compaction** (every SUMMARY_INTERVAL): write full iteration summary to `.experiments/state/<run-id>/progress-<i>.md`, discard verbose per-iteration details from working memory. Retain only: current metric, iteration count, JSONL path, `best_commit`. Full history recoverable from `experiments.jsonl` and `ideation-<i>.md`.

### Step R6: Results report

`--team`: Phase D (`modes/team.md`) already wrote the report — skip the report path, write and `modes/report.md` below; run the `state.json` status update and the wrap-up.

Pre-compute the report path before writing — branch is for report paths only; anti-overwrite appends a counter suffix (`-2.md`, `-3.md`, …) when the file exists:

```bash
mkdir -p .reports/research  # timeout: 3000
BRANCH=$(git branch --show-current 2>/dev/null | tr '/' '-' || echo 'main')  # timeout: 3000
OUT=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/resolve-anti-overwrite-path.py" .reports/research "run-$BRANCH-$(date +%Y-%m-%d)")  # timeout: 5000
echo "OUT=$OUT"
```

Write full report to the printed `OUT` via Write tool. Do not print to terminal.

Follow `modes/report.md`:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CLAUDE_SKILL_DIR < "${TMPDIR:-/tmp}/research-run-skill-dir-${CSID}" 2>/dev/null || CLAUDE_SKILL_DIR=""
cat "$CLAUDE_SKILL_DIR/modes/report.md"  # timeout: 5000
```

`state.json`: `status = completed`.

**Wrap-up — always, before R7** (R7 may be skipped; this must not be):

Print next-step suggestions as plain text — do NOT call `AskUserQuestion`: both `/research:retro` and `/research:verify` ship `disable-model-invocation: true`, so neither is dispatchable this turn.

```text
Next: /research:retro <run-id>       — post-run retrospective analysis
Next: /research:verify <paper>       — verify implementation matches paper claims
```

```bash
rm -f .temp/state/skill-contract.md  # clear contract — campaign complete (compaction-contract.md §Lifecycle)  # timeout: 5000
```

### Step R7: Codex delegation (optional)

Skip R7 if `CODEX_DELEGATION_AVAILABLE=false` (warning already printed at R2 — no further action needed).

Inspect applied changes (`git diff <baseline_commit>...<best_commit> --stat`), identify tasks Codex can complete (comments on non-obvious changes, docstrings for modified functions, test coverage).

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _RESEARCH_SHARED < "${TMPDIR:-/tmp}/research-shared-${CSID}" 2>/dev/null || _RESEARCH_SHARED=""  # warm read (Check 41) — bash state lost between Bash() calls
cat "$_RESEARCH_SHARED/codex-delegation.md"  # timeout: 5000
```

Apply criteria loaded above.

## Resume Mode

> loads: resume.md
>
> Follow and execute `modes/resume.md`.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CLAUDE_SKILL_DIR < "${TMPDIR:-/tmp}/research-run-skill-dir-${CSID}" 2>/dev/null || CLAUDE_SKILL_DIR=""
cat "$CLAUDE_SKILL_DIR/modes/resume.md"  # timeout: 5000
```

## Mode: colab

> loads: colab-setup.md
>
> Execute only when `--colab` flag active. Follow and execute `modes/colab-setup.md`.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CLAUDE_SKILL_DIR < "${TMPDIR:-/tmp}/research-run-skill-dir-${CSID}" 2>/dev/null || CLAUDE_SKILL_DIR=""
cat "$CLAUDE_SKILL_DIR/modes/colab-setup.md"  # timeout: 5000
```

</workflow>

<notes>

- **Commit before verify** — enables clean `git revert HEAD` if metric doesn't improve. Never verify before committing.
- **`git revert` over `git reset --hard`** — preserves experiment history, not in deny list.
- **Never `git add -A`** — always stage specific files returned by agent JSON.
- **Never `--no-verify`** — if pre-commit hook blocks, delegate to `foundry:linting-expert` and fix.
- **Guard ≠ Verify** — guard checks regressions (tests, lint); verify checks target metric. Both must pass to keep commit.
- **Single-sample keep rule by default** — `metric_repeats` 1 compares one Phase 5 sample with `best_metric` and keeps any measured gain: right for a deterministic metric (coverage, line counts, fixed-seed eval), wrong for timing, throughput or stochastic training, where noise alone produces "gains" and the `perf` fallback strategy lands. For those set `metric_repeats` ≥ 3: Phase 7 then keeps only gains beyond `max(min_delta_pct, 2σΔ)`, the noise model `/research:fortify` uses, at `metric_repeats`× metric cost. Default stays 1 (fortify's own `--runs` defaults to 3) so deterministic campaigns and the Colab time budget pay nothing extra.
- **metric_cmd exit code ignored** — R2 validates metric_cmd by parsing stdout for a float, not exit code. Piping metric output through grep/awk/tr is acceptable; only final stdout float matters.
- **Guard/metric scripts protected** — ideation agent must not modify files referenced in `guard_cmd`/`metric_cmd`; exclude them from `scope_files`. Enforced in Phase 3 by `check_iteration_scope.py` (needs a program file; text-goal runs stay prompt-only). New test files may be created within `scope_files` for coverage-improvement campaigns.
- **JSONL over TSV** — richer structured fields, `jq`-parseable, no delimiter ambiguity; query with `jq -c 'select(.status == "kept")' experiments.jsonl`.
- **State persistence enables resume** — if loop crashes/times out, `resume` picks up exactly where it stopped.
- **Safety break**: hard cap = 50 iterations (values above 50 in program.md clamped to 50 with a warning); default 20 when max_iterations unset in program.md; skill never exceeds MAX_ITERATIONS.
- **Unbounded cross-skill chain**: `run` → `/research:retro` → `/research:run --hypothesis` / `/research:fortify` → re-run `/research:run` has no campaign-level iteration cap (unlike `sweep`'s `MAX_REFINE = 3` or `run`'s own `MAX_ITERATIONS`). Human-gated each hop — cannot spin autonomously. No counter by design — would need `retro` to write `.experiments/state/`, breaking its read-only invariant.
- **Explicit flags = hard requirements**: all flags (`--colab`, `--compute=docker`, `--codex`, `--researcher`, `--architect`) must be available at R2. If unavailable, stop — never silently degrade.
- R7 Codex delegation needs no other plugin — `codex-delegation.md` ships in this plugin's own `skills/_shared/` and resolves via `bin/resolve_shared.py`. R7 is silently skipped only if that file is missing (broken install).
- **Dry-run / walkthrough requests** (trace steps, execute nothing): report only what the step contract decides. That means R1 parse results and printed warnings, R2 blocking checks with their stop messages, R3 resolved agent and reason, `state.json` fields, and the Phase 7 row matched by any outcome the request supplies. Trace only the steps the request names; never simulate phases or iterations it excludes. Advice outside the contract (measurement tuning, cost caps, environment drift, leftover state) → at most one trailing `Suggestions:` line, never in the findings list.
- **Confidence on traced outcomes** (walkthrough/report contexts only): parse, routing, precondition and Phase 7 outcomes follow deterministically from the step text. Deduct only for inputs the request leaves unknown, such as unrun command output or unread file contents. Exact warning wording that the skill does not fix is `(-0.00)`, not a gap.

</notes>
