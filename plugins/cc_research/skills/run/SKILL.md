---
name: run
description: Sustained metric-improvement loop with atomic commits, auto-rollback, and experiment logging. Iterates with specialist agents, commits atomically, auto-rolls back on regression. Accepts a program.md file path. Supports --resume, --team, --colab, --codex, --researcher, --architect, --journal, --hypothesis.
argument-hint: <program.md> [clarification] [--resume <program.md>] [--team] [--compute=local|colab|docker] [--colab[=H100|L4|T4|A100]] [--codex] [--researcher] [--architect] [--journal] [--hypothesis <path>] [--scientist] [--codemap] [--no-codemap] [--keep "<items>"]
effort: xhigh
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, Agent, TaskCreate, TaskUpdate, AskUserQuestion
disable-model-invocation: true
---

<objective>

Sustained metric-improvement loop — reads `program.md`, iterates specialist ideation agents, commits atomically, auto-rolls back on regression. For long-running automated improvement campaigns.

NOT for: methodology validation before run (use `/research:judge`); hypothesis generation (use `research:scientist` agent); one-off feature work (use `/develop:feature`).

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
STATE_DIR:                  .experiments/state/<run-id>/  (timestamped dir per run — see .claude/rules/foundry-artifact-lifecycle.md)
SENTINEL_SLUG_FORMULA: |
  eval "$(bash "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/git_slugs.sh")"
  # Sentinel path: ${TMPDIR:-/tmp}/claude-commit-auth-${REPO_SLUG}-${BRANCH_SLUG}  # tmpdir-exempt: user-shell-boundary
  # Bash state is lost between tool calls — re-source git_slugs.sh at each use site; it is the only authorized slug form.
```

<!-- Note: STATE_DIR (.experiments/state/) holds per-iteration artifacts (diary, experiments.jsonl).
     Hypothesis pipeline outputs (hypotheses.jsonl, checkpoint.json, journal.md) go to .experiments/<run-id>/ (RUN_DIR).
     These are two separate directories by design — see protocol.md for layout. -->

<!-- policy-sibling: run/modes/colab-setup.md, plan/SKILL.md, sweep/SKILL.md:4, judge/SKILL.md — COLAB_KNOWN_HW set restated in each; keep in sync (plugins/CLAUDE.md §Policy Duplication Marker). -->

**Agent strategy mapping** (`agent_strategy` in config → ideation agent to spawn):

| `agent_strategy` | Specialist agent | When to use |
| -- | -- | -- |
| `auto` | heuristic | Default — infer from metric_cmd keywords |
| `perf` | `foundry:perf-optimizer` | latency, throughput, memory, GPU utilization |
| `code` | `foundry:sw-engineer` | coverage, complexity, lines, coupling |
| `ml` | `research:scientist` | accuracy, loss, F1, AUC, BLEU |
| `arch` | `foundry:solution-architect` | coupling, cohesion, modularity metrics |

**Auto-inference keyword heuristics** (when `agent_strategy: auto` or omitted; checked against `## Goal` text AND metric command):

**Precedence order** (first match wins; ML keywords beat test-framework keywords). ML-specific compound terms (not bare tokens) required — prevents over-triggering on `eval`/`train`/`val` as common words:

- contains `accuracy`, `loss` (paired with `train_loss`/`val_loss`/`eval_loss`), `f1_score`, `auc_roc`, `auroc`, `train_step`, `val_acc`, `eval_loss`, `epoch`, `gradient`, `tensor`, `overfit`, `generaliz`, `regulariz`, `validation`, `dropout`, `weight_decay`, `lr_schedule`, `cross_val`, `precision`, `recall`, OR explicit `--scientist` flag → `ml` → `research:scientist`
- contains `time`, `latency`, `bench`, `throughput`, `memory` → `perf` → `foundry:perf-optimizer`
- contains `pytest`, `coverage`, `complexity` → `code` → `foundry:sw-engineer`
- no keyword match → `perf` (default fallback) — **WARN**: print `⚠ No keyword match — defaulting to 'perf' strategy. If this is an ML task, set agent_strategy: ml in program.md.` Log resolved agent + reason in state.json `strategy_resolution`.

Bare tokens `eval`, `train`, `val` (without compound suffix) do NOT trigger `ml` routing — too common in non-ML contexts (test eval scripts, training-environment configs, validator command names).

**Stuck escalation sequence** (at STUCK_THRESHOLD consecutive discards):

1. Switch agent type. Rotation by current strategy:

   | Current strategy | Next strategy | Escalation agent |
   | -- | -- | -- |
   | `code` | `ml` | `research:scientist` |
   | `ml` | `perf` | `foundry:perf-optimizer` |
   | `perf` | `code` | `foundry:sw-engineer` |
   | `arch` | `code` | `foundry:sw-engineer` (fallback `foundry:solution-architect` if sw-engineer unavailable) |
   | `auto` | infer from resolved strategy | follow rotation row for whichever concrete strategy `auto` heuristics resolved to at Step R3 (e.g. `auto` → resolved `ml` → next `perf` → `foundry:perf-optimizer`) |

2. Spawn 2 agents parallel, competing strategies; each writes full analysis to `.experiments/state/<run-id>/stuck-escalation-<i>-<agent-type>.md`, returns ONLY compact JSON envelope. Use this spawn prompt verbatim (substitute `<run-id>`, `<i>`, and strategy):

   ```text
   Stuck-escalation handoff — iteration <i> after STUCK_THRESHOLD consecutive discards.
   Read `.experiments/state/<run-id>/state.json` for goal, best_metric, baseline, config.
   Read `.experiments/state/<run-id>/experiments.jsonl` for full iteration history.
   Read `.experiments/state/<run-id>/diary.md` for qualitative context (what was tried, why reverted).
   Read `.experiments/state/<run-id>/context-<i>.md` for current iteration's context block.
   Continue from the last completed iteration (do NOT restart from iteration 0).
   Write your full analysis and proposed change to `.experiments/state/<run-id>/stuck-escalation-<i>-<your-strategy>.md`.
   Write a resume point to `.experiments/state/<run-id>/resume.json`: {iteration: <i>, strategy: "<your-strategy>", proposed_change: "<one-line description>"}.
   Return ONLY: {"strategy":"<your-strategy>","description":"...","files_modified":[...],"confidence":0.N,"file":".experiments/state/<run-id>/stuck-escalation-<i>-<your-strategy>.md"}
   ```

   Consolidation: pick whichever returns delta ≥ 0.1% AND guard pass; if both qualify, pick higher delta.

3. Stop, report progress, surface to user — no blind looping

</constants>

<compaction>

- Key boundary: end of each Phase 8 in R5 iteration loop — JSONL record appended and `state.json` updated. Overwrite each iteration; contract always reflects latest in-progress state. Long metric-improvement loops are the primary auto-compact risk.
- Preserve at each boundary: RUN_ID (TMPDIR key), STATE_DIR path, program.md path, current iteration#, best metric, best-commit SHA, experiments.jsonl path.
- Clear at R1 start (stale prior run) and after R6/R7 campaign completion.

</compaction>

<workflow>

<!-- Agent resolution: see _RESEARCH_SHARED/agent-resolution.md -->

## Agent Resolution

**Agent resolution**: load and follow the protocol below. Contains: foundry check + fallback table. If foundry not installed: use table to substitute each `foundry:X` with `general-purpose`. Agents this skill uses: `foundry:sw-engineer`, `foundry:linting-expert`, `foundry:perf-optimizer`, `foundry:solution-architect`, `research:scientist`.

```bash
# loads: compaction-contract.md
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_RESEARCH_SHARED=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/resolve_shared.py" 2>/dev/null)  # timeout: 5000
[ -z "$_RESEARCH_SHARED" ] && { echo "! Plugin path resolution failed — ensure research plugin installed and CLAUDE_PLUGIN_ROOT set, or invoke from project root."; exit 1; }
echo "$_RESEARCH_SHARED" > "${TMPDIR:-/tmp}/research-shared-${CSID}"  # cold resolve — every later site reads this sentinel instead of re-running python
cat "$_RESEARCH_SHARED/agent-resolution.md"
```

**`CLAUDE_SKILL_DIR` resolution** — constants block provides default `plugins/cc_research/skills/run` (source-tree path). Resolve to installed path before use:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
CLAUDE_SKILL_DIR=$(ls -td ~/.claude/plugins/cache/borda-ai-rig/research/*/skills/run 2>/dev/null | head -1)
[ -z "$CLAUDE_SKILL_DIR" ] && CLAUDE_SKILL_DIR="$(git rev-parse --show-toplevel 2>/dev/null)/plugins/cc_research/skills/run"
echo "$CLAUDE_SKILL_DIR" > "${TMPDIR:-/tmp}/research-run-skill-dir-${CSID}"
```

## Default Mode (Steps R1–R7)

Triggered by `run <goal|file.md>`.

**Task tracking**: create tasks R0–R7 at start. If no `--researcher`/`--architect`, mark R0 skipped. If `--codex` active, create task `R5b: Codex co-pilot (iter ?/max)` status `pending`.

### Step R0: Hypothesis pre-phase (`--researcher` / `--architect`)

If no `--researcher`/`--architect`, skip to R1.

**Flag combination note**: every oracle self-annotates feasibility (`feasible`/`blocker`/`codebase_mapping` are part of the oracle schema — no separate annotation spawn). `--researcher` alone, `--architect` alone, and both together are all valid; both together adds architectural hypotheses alongside the research ones.

Follow `modes/hypothesis-pipeline.md`:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CLAUDE_SKILL_DIR < "${TMPDIR:-/tmp}/research-run-skill-dir-${CSID}" 2>/dev/null || CLAUDE_SKILL_DIR=""
cat "$CLAUDE_SKILL_DIR/modes/hypothesis-pipeline.md"  # timeout: 5000
```

**Per-iteration hypothesis selection** (when `--researcher`/`--architect` set, inside R5 loop): pop next from `RESEARCH_QUEUE`. Append to Phase 2 prompt: "Focus this iteration on testing this hypothesis: `<hypothesis text>`."

**Per-iteration journal hook** (inside R5, after Phase 7): if `--journal` active, append entry to `<RUN_DIR>/journal.md` after EVERY iteration — regardless of outcome. Entry format: `protocol.md` (companion file, same skill dir). # loads: protocol.md Journals record kept and reverted iterations so ideation agent learns failed approaches.

**Per-iteration checkpoint write** (after Phase 7): if `--researcher`/`--architect` active, append one line to `<RUN_DIR>/checkpoint.json` per schema in `protocol.md` (companion file, same skill dir): `{iteration, hypothesis_id, metric_before, metric_after, status: "passed"|"rolled_back"}`.

### Step R1: Load / build config

**`--resume` flag detection**: if `--resume` in args, extract optional program.md path. Jump to `## Resume Mode`. Rest of R1 and R2–R7 skipped.

**`--hypothesis <path>` parsing**: if `--hypothesis` in args, extract path token following it. Verify file exists: `[ -f "$HYPOTHESIS_PATH" ]`. If not found: print `! --hypothesis <path>: file not found` and stop. If found: set `hypothesis_override = true`. In R5 Phase 2 (Propose change), replace oracle-generated hypothesis with loaded file content — prepend to ideation agent prompt: "Use this pre-specified hypothesis as your starting hypothesis for iteration N: <contents of HYPOTHESIS_PATH>. Validate, refine, and implement it. Do not generate a new hypothesis from scratch."

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

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# runs under bash — zsh never populates ${BASH_REMATCH[1]}, so --keep "..." was silently resolving empty
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/extract-keep-flag.py" research-run "$ARGUMENTS"  # timeout: 5000 — parses --keep, clears a stale contract, persists for Phase 8
```

**Unsupported flag check**: load and follow the protocol below. Supported flags for this skill: `--resume`, `--team`, `--compute`, `--colab`, `--codex`, `--researcher`, `--architect`, `--journal`, `--hypothesis`, `--scientist`, `--codemap`, `--no-codemap`, `--keep`.

```bash
# loads: unsupported-flag-protocol.md
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _RESEARCH_SHARED < "${TMPDIR:-/tmp}/research-shared-${CSID}" 2>/dev/null || _RESEARCH_SHARED=""  # warm read (Check 41)
cat "$_RESEARCH_SHARED/unsupported-flag-protocol.md"
```

**Codemap auto-detection** — structural blast-radius context for modules the experiment edits; on by default when codemap installed + index found. `--no-codemap` opts out; `--codemap` is strict (fail if unavailable).

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# writes true/false to research-run-codemap-enabled-${CSID}; strict mode exits 1 (already printed ! BLOCKED) if unavailable
CODEMAP_RAW=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/codemap-flag.py" research-run "$ARGUMENTS") || exit 1
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
6. `target` under `## Metric`: `direction: higher` → stop when metric ≥ target; `direction: lower` → stop when metric ≤ target. If `target` omitted, run until `max_iterations`.
7. Unrecognized keys/headings → warn once, ignore.
8. `## Notes` and `# Program:` title never parsed — human-only. (`# Campaign:` accepted as alias.)

**If argument is text** — auto-detect `metric_cmd`/`guard_cmd` from goal string and codebase scan (same as P-P1, non-interactive). `config.json` not read.

**`--colab[=HW]` parsing**: `--colab` (no `=`) → `compute = "colab"`, `colab_hw = null`. `--colab=<value>` → `compute = "colab"`, `colab_hw = <value>` (uppercased). Unknown `<value>` (not in `{H100, L4, T4, A100}`) → print `"⚠ Unknown Colab hardware '<value>' — proceeding with default GPU. Known: H100, L4, T4, A100"`, set `colab_hw = null`. `--compute=colab` (no HW) → `compute = "colab"`, `colab_hw = null`.

`colab_hw` in `## Config` sets hardware preference (`H100`, `L4`, `T4`, `A100`); CLI `--colab=HW` overrides.

Generate `run-id` = `$(date -u +%Y-%m-%dT%H-%M-%SZ)`. Assign immediately:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
RUN_ID=$(date -u +%Y-%m-%dT%H-%M-%SZ)
RUN_DIR=".experiments/${RUN_ID}"  # hypothesis pipeline + journal outputs (per <constants> note)
STATE_DIR=".experiments/state/${RUN_ID}"  # per-iteration artifacts (state.json, experiments.jsonl, diary.md)
mkdir -p "$RUN_DIR" "$STATE_DIR"  # timeout: 5000 — both dirs created before any Write to either
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
  "config": {},
  "program_file": "<absolute path to program.md, or null>",
  "iteration": 0,
  "best_metric": null,
  "best_commit": null,
  "status": "initializing",
  "started_at": "<ISO timestamp>",
  "clarification_prompt": null,
  "colab_hw": null,
  "sandbox_mode": "local"
}
```

Note: status is `"initializing"` until all R2 precondition checks pass — resume treats `"initializing"` as failed-init, not active run. Update to `"running"` at end of R2 (after all checks pass).

### Step R2: Precondition checks

Run all checks before touching code. Fail fast with clear message:

01. **Clean git**: `git status --porcelain` → must be empty. If dirty: print dirty files and stop.
02. **Not detached HEAD**: `git rev-parse --abbrev-ref HEAD` → must not be `HEAD`.
03. **Metric command numeric**: run `metric_cmd` once; parse stdout for float. If no float: show output and stop.
04. **Guard passes**: run `guard_cmd` once; must exit 0. If fails: show output and stop.
05. **`--colab` check**: verify `mcp__colab-mcp__runtime_execute_code` available. If not, print setup instructions (see Colab MCP section) and stop. If `--colab=HW` (`colab_hw` non-null): print: `  Hardware requested: --colab=<colab_hw>. Ensure your Colab notebook running with <colab_hw> GPU.`
06. **`--codex` check**: distinguish the installed-and-enabled bridge target from absence. `claude` not on `PATH` → print `⚠ 'claude' CLI not in PATH — bridge availability cannot be verified.` and **stop**. If `claude plugin list` lacks `bridge@borda-ai-rig`, print `⚠ bridge@borda-ai-rig not installed. Install it from the Borda AI Rig marketplace.` and **stop**. If it is disabled, print `⚠ bridge@borda-ai-rig is disabled. Enable it and reload plugins.` and **stop**.
07. **`compute: docker` check**: run `docker ps` via Bash (`timeout: 5000`). If non-zero: print `⚠ Docker daemon not running. Start Docker Desktop and retry.` and **stop**.
08. **Flag conflict**: if `--colab` and `--compute=docker` both active: print `⚠ --colab and --compute=docker are mutually exclusive. Use one or the other.` and **stop**.
09. **`--colab` + `--codex` compatibility note** (non-blocking): if both flags active, print `ℹ --colab + --codex active: Codex Phase 2c will receive colab_hw context so generated code can target the right GPU (H100/T4 bf16 vs fp16). Phase 5 metric verification runs through Colab MCP as usual.` and continue. Pass `colab_hw` to Codex spawn prompt (Phase 2c — see `modes/codex-copilot.md`).
10. **`--journal` prerequisite**: verify `--researcher`/`--architect` also set. If neither: print `⚠ --journal requires --researcher or --architect — omit --journal or add a hypothesis pipeline flag.` and **stop**.

**`--codex-delegation` warning** (non-blocking): `codex-delegation.md` ships inside this plugin's own `skills/_shared/`, so R7 needs no other plugin installed. Verify it resolves:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _RESEARCH_SHARED < "${TMPDIR:-/tmp}/research-shared-${CSID}" 2>/dev/null || _RESEARCH_SHARED=""  # warm read (Check 41)
[ -f "$_RESEARCH_SHARED/codex-delegation.md" ] || echo "⚠ codex-delegation.md not found under $_RESEARCH_SHARED — R7 Codex delegation will be skipped; reinstall the research plugin."
```

Set `CODEX_DELEGATION_AVAILABLE=true` if found, `false` otherwise. Continue regardless.

**Initialize sandbox + timeout variables** (after all checks pass — constants YAML block not auto-exported to bash; assign explicitly with `${VAR:-default}` to honour environment overrides; ADV-L15 / ADV-M20):

```bash
SANDBOX_NETWORK="${SANDBOX_NETWORK:-none}"  # override via program.md Config or environment variable
# Verify timeout — 120s local, 300s Colab per <constants>; bash overrides via VERIFY_TIMEOUT_SEC env var
if [ "${compute:-local}" = "colab" ]; then
    VERIFY_TIMEOUT_SEC="${VERIFY_TIMEOUT_SEC:-300}"
else
    VERIFY_TIMEOUT_SEC="${VERIFY_TIMEOUT_SEC:-120}"
fi
VERIFY_TIMEOUT_MS=$((VERIFY_TIMEOUT_SEC * 1000))
# Ideation Agent() calls run in background — spawn, end turn, no filler call; on each notification check its output file, mark timed_out (⏱) if empty.
```

**Initialize `sandbox_mode`**:

- `compute: docker` (daemon check passed in step 7) → `sandbox_mode = "docker"`. Print: `sandbox: Docker daemon reachable — sandbox mode active`
- All other cases (`compute: local`, `compute: colab`) → `sandbox_mode = "local"`

**Update state.json status to `"running"`** — write only after ALL checks above pass. Resume treats `"initializing"` as failed-init and skips such runs.

### Step R3: Select ideation agent

Apply `agent_strategy` mapping from `<constants>`. If `auto`, apply keyword heuristics to `metric_cmd`. Log selected agent to `state.json`.

### Step R4: Establish baseline (iteration 0)

Run `metric_cmd` and `guard_cmd`. Parse metric value. Append to `experiments.jsonl`:

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

Print: `Baseline: <metric_cmd key> = <value>`.

Write initial diary header to `.experiments/state/<run-id>/diary.md`:

```markdown
# Research Diary — <goal>

**Run**: <run-id>
**Started**: <ISO timestamp>
**Baseline**: <metric_key> = <baseline value>

---
```

Then proceed to R5.

### Step R5: Iteration loop

```bash
# REPO_SLUG / BRANCH_SLUG: source the single authorized slug form (see <constants>)
eval "$(bash "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/git_slugs.sh")"  # timeout: 3000
COMMIT_SENTINEL="${TMPDIR:-/tmp}/claude-commit-auth-${REPO_SLUG}-${BRANCH_SLUG}"  # tmpdir-exempt: user-shell-boundary
touch "$COMMIT_SENTINEL"  # timeout: 3000
# trap doesn't survive across Bash calls — commit-guard.js hook (foundry-owned) handles protection instead
```

> **Dependency — `commit-guard.js` (requires `foundry` plugin)**: the commit-sentinel dance above (touch at R5, re-touch each phase, `rm` at cleanup) is enforced by foundry's `commit-guard.js` `PreToolUse` hook. That hook ships with the `foundry` plugin only — research does not bundle it. **Standalone install (foundry absent): the sentinel touches become inert and `git commit` proceeds unguarded.** The sentinel logic is still safe to run (touch/`rm` on a temp file are harmless no-ops without the hook); it simply provides no protection. If you rely on atomic-commit guarding during `research:run`, install `foundry`.

**Sentinel liveness**: touch `$COMMIT_SENTINEL` after each Phase 8 result write to extend monitoring window — do NOT rely solely on sentinel touched at loop start; slow iterations exceed 15-min TTL. Re-derive slug per SENTINEL_SLUG_FORMULA from `<constants>` (bash state lost between calls).

**`--team` mode**: If `--team` active, follow `modes/team.md` and execute Phases A–D in place of standard iteration loop below.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CLAUDE_SKILL_DIR < "${TMPDIR:-/tmp}/research-run-skill-dir-${CSID}" 2>/dev/null || CLAUDE_SKILL_DIR=""
cat "$CLAUDE_SKILL_DIR/modes/team.md"  # timeout: 5000
```

**`--team` + `--hypothesis` combination**: combinable. Team mode uses provided hypothesis path and skips oracle/hypothesis-generation phase — `hypothesis_override = true` applies inside team.md Phase A same as solo mode.

For each iteration `i` from 1 to `max_iterations`:

**Phase overview** (all phases run per iteration):

| Phase | Name | Trigger / description |
| -- | -- | -- |
| 0 | Print header | Always — print `[→ Iter N/max · starting]`; TaskUpdate R5 subject with current iteration |
| 1 | Build context | Always — build compact context from git log, JSONL history, and recent diff |
| 2 | Propose change | Always — spawn specialist agent to read code, research, investigate, and generate a hypothesis with optional sandbox scripts |
| 2a | Sandbox validate | `compute: docker` only — run agent's exploratory scripts in Docker sandbox (read-only mount) |
| 2b | Apply change | `compute: docker` only — agent applies the (validated) proposal to real codebase using Write/Edit tools only; no Bash on codebase |
| 2c | Codex co-pilot | `--codex` only — required each iteration up to `MAX_CODEX_RUNS`; after cap reached, continue without Codex |
| 3 | Verify files | Always — check `git diff --stat`; skip to Phase 8 if no files changed (no-op) |
| 4 | Commit change | Always — stage modified files and commit before verifying metric |
| 5 | Verify metric | Always — run `metric_cmd` via `compute` mode (local/colab/docker); revert on timeout |
| 6 | Run guard | Always — run `guard_cmd` via `compute` mode; record pass or fail |
| 7 | Evaluate outcome | Always — keep, rework, or revert based on metric + guard result |
| 7a | Write diary | Always — append one structured entry to `diary.md` recording hypothesis, outcome, and decision rationale |
| 8 | Write log | Always — append JSONL record, update `state.json`, print iteration summary, TaskUpdate R5 with result |
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

#### Phase 1 — Build context

Build context for ideation agent, write to file — do NOT accumulate inline in main context:

```bash
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
IFS= read -r _RESEARCH_SHARED < "${TMPDIR:-/tmp}/research-shared-${CSID}" 2>/dev/null || _RESEARCH_SHARED=""  # warm read (Check 41)
cat "$_RESEARCH_SHARED/codemap-context.md"
```

Execute its block. Leave `TARGET_MODULE`/`TARGET_FN` empty for the global `central` blast-radius baseline, or set `TARGET_MODULE` to the module the experiment edits (from `## Config`) for importer/coverage queries. Append output to `context-${I}.md` under a `## Structural Context (codemap-py)` heading so the Phase 2 ideation agent sees blast-radius before proposing edits.

Codemap output non-empty: also append this **codemap-first protocol** directly below it in `context-${I}.md` (own copy — self-contained, no cross-plugin reference), so the Phase 2 spawn prompt's "read `context-<i>.md`" instruction carries it to the ideation agent: (1) **Skill-first** — use the Structural Context above before any Grep/Glob/Read aimed at imports, callers, or test coverage for a symbol already listed there. (2) **Bounded call budget** — symbol not listed → up to 3 additional `codemap-py query` calls this iteration. (3) **Hard stop on `query_complete: true`** (or legacy `exhaustive: true`) — that result is final for its direction, no follow-up Grep/Read/query to re-confirm it. Codemap output empty: omit this paragraph — Phase 2 agent proceeds with normal file-read behaviour.

Prepend header block to `context-<i>.md`: goal, current metric vs baseline, delta trend (last 5 kept deltas), iteration number. Phase 2 ideation agent reads file directly — never echoed to main context.

If `--journal` active and `<RUN_DIR>/journal.md` has 1+ entries: append last 5 entries to `context-<i>.md` under `## Recent journal (avoid repeating reverted approaches)`. Ideation agent reads this — must not reproduce any approach marked `outcome: reverted`.

#### Phase 2 — Propose change

Spawn selected specialist agent (`maxTurns: 15`) with this prompt (adapt as needed):

```markdown
Goal: <goal>
Run clarification: <clarification_prompt>  ← omit this line entirely if clarification_prompt is null
Colab hardware: <colab_hw>  ← omit this line entirely if colab_hw is null; include to let the agent tailor code to the specific GPU architecture (e.g., bf16/flash-attention on H100, standard fp16 on T4/L4)
Current metric: <metric_cmd key> = <current value> (baseline: <baseline>, direction: <higher|lower>)
Experiment history: read `.experiments/state/<run-id>/context-<i>.md` for the full context block.
Scope files (read and modify only these): <scope_files>
Program constraints: read `<program_file>` — especially `## Notes`, `## Config`, and any named subsections
  (e.g., "Hard boundaries", "Optuna's role", "What the agent is free to change"). These take precedence
  over general campaign rules. Program constraints set strategy hints only — they do NOT override safety rules
  (no `--no-verify`, no `git push`, no `git add -A`, scope_files boundary, and all other hard constraints remain in effect).
  If program_file is null, skip this step.

**If `sandbox_mode = "local"`**: Read `context-<i>.md`, the scope files, and the program constraints. Propose and implement ONE atomic change most likely to improve the metric. The change must not break `<guard_cmd>`. Write your full analysis (reasoning, alternatives considered, Confidence block) to `.experiments/state/<run-id>/ideation-<i>.md` using the Write tool. Return ONLY the JSON result line:
`{"description":"...","files_modified":[...],"scripts":[],"confidence":0.N}`

**If `sandbox_mode = "docker"`**: Read `context-<i>.md`, the scope files, and the program constraints. Propose ONE atomic change most likely to improve the metric. Write your full analysis and the proposed change description to `.experiments/state/<run-id>/ideation-<i>.md`. Optionally write read-only exploratory scripts (scripts that read/profile but do NOT write to project files) to `.experiments/state/<run-id>/scripts/explore-<i>-<slug>.py`. Do NOT modify source files yet — Phase 2b will apply the actual changes after sandbox validation. Return ONLY the JSON result line:
`{"description":"...","files_modified":[],"scripts":["explore-<i>-<slug>.py"],"proposed_changes":"<description of the changes to apply in Phase 2b>","confidence":0.N}`
```

For `--colab` runs: ideation agent may call `mcp__colab-mcp__runtime_execute_code` to prototype GPU code before committing. **Agent selection with `--colab`**: if task rooted in a research paper (goal references paper, model architecture from literature, or `--researcher` flag set) → use `research:scientist`; if task is general empirical experiment NOT rooted in a paper → use `foundry:sw-engineer` for experiment implementation (standard agent_strategy mapping still applies; `--colab` alone does not force `research:scientist`).

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

`git diff --stat`. If no files changed (no-op): append to JSONL with `status: no-op`, skip to Phase 8 (log), continue loop.

#### Phase 4 — Commit change

Refresh commit sentinel before staging — R5 loop can exceed the 15-min sentinel TTL set in R5 setup. Slug computation unavoidably re-run (bash state lost between tool calls); path pattern identical to R5 setup block above:

```bash
# refresh sentinel — bash state lost between calls, re-source slug (R5 form)
eval "$(bash "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/git_slugs.sh")"  # timeout: 3000
touch "${TMPDIR:-/tmp}/claude-commit-auth-${REPO_SLUG}-${BRANCH_SLUG}"  # timeout: 3000  # tmpdir-exempt: user-shell-boundary
```

Stage only modified files (never `git add -A`):

```bash
git add <files_modified from agent JSON>  # timeout: 3000
git commit -m "experiment(optimize/i<N>): <description>"  # timeout: 90000
```

If pre-commit hooks fail:

- Delegate to `foundry:linting-expert`: provide failing hook output and modified files; ask to fix. Max 2 attempts.
- If still failing after 2 attempts: `git restore --staged <files_modified>` + `git restore <files_modified>` to clean up (`# <files_modified>` = list of files returned by the iteration agent; restricts discard to iteration scope only), append `status: hook-blocked`, continue loop.

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

**If `sandbox_mode = "docker"`**: run `guard_cmd` in same Docker container as Phase 5 (same flags; no resource limits). Check exit code only.

**If `sandbox_mode = "local"`**: run `guard_cmd` directly.

Record pass (exit 0) or fail (non-zero).

#### Phase 7 — Evaluate outcome

| Condition | Action |
| -- | -- |
| metric improved AND guard pass | Keep commit. Update `state.json`: `best_metric`, `best_commit`. "Improved" = `new_metric > best_metric` when `direction: higher`; `new_metric < best_metric` when `direction: lower`. |
| metric improved AND guard fail | Rework: re-spawn agent with guard failure output. Max `GUARD_REWORK_MAX` (2) attempts. If still failing after all rework attempts: revert (`git revert HEAD --no-edit`); diary status = `"reverted"`, decision = `"Guard failed after GUARD_REWORK_MAX rework attempts — reverted"`. |
| metric improved AND gain < 0.1% AND change > 50 lines | Refresh sentinel; discard: `git revert HEAD --no-edit`. (Line count computed via `CHANGE_LINES` — see note below table.) |
| no improvement | Refresh sentinel; revert: `git revert HEAD --no-edit`. |

**Line count computation** (for "gain < 0.1% AND change > 50 lines" row): run before evaluating the condition:

```bash
DIFF_SUMMARY=$(git diff --stat HEAD~1..HEAD | tail -1)  # timeout: 3000
INSERTIONS=$(echo "$DIFF_SUMMARY" | grep -oE '[0-9]+ insertion' | grep -oE '[0-9]+' || echo 0)
DELETIONS=$(echo "$DIFF_SUMMARY" | grep -oE '[0-9]+ deletion' | grep -oE '[0-9]+' || echo 0)
CHANGE_LINES=$(( INSERTIONS + DELETIONS ))
```

`git revert HEAD --no-edit` — never `git reset --hard` (preserves history, not in deny list).

**Double-revert guard** (ADV-H19) — Phase 7 rework→revert can collide with a partial Phase 5 timeout revert or Phase 6 guard-fail revert performed in the same iteration. Always check before issuing the revert:

```bash
ALREADY_REVERTED=$(git log --oneline -5 2>/dev/null | grep -c "^[0-9a-f]\+ Revert ") || ALREADY_REVERTED=0  # `|| echo 0` appends a second 0: grep -c prints 0 *and* exits 1 on zero matches
if [ "$ALREADY_REVERTED" -gt 0 ]; then
    echo "Phase 7: prior revert detected (Phase 5/6 already reverted this iteration) — skipping double-revert."
else
    git revert HEAD --no-edit  # timeout: 15000
fi
```

The guard fires on `metric improved AND guard fail` (after `GUARD_REWORK_MAX` attempts exhausted), `no improvement`, and `gain < 0.1% AND change > 50 lines` paths — any path that issues a revert after Phase 5 or Phase 6 may have already reverted.

#### Phase 7a — Write diary

After Phase 7 decision, append one entry to `diary.md`:

```markdown
## Iteration N — <ISO timestamp>

**Hypothesis**: <agent's description from Phase 2 JSON — the proposed change and expected improvement>

**Outcome**: <metric_key> = <value> (Δ<delta>% vs baseline) — <kept|reverted|rework|no-op|hook-blocked|timeout>

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

Append one JSONL record to `experiments.jsonl` (same schema as baseline record in Step R4, plus `ideation_source`):

```json
{
  "iteration": 1,
  "commit": "<sha of experiment commit or revert>",
  "metric": 0.0,
  "delta": 0.0,
  "guard": "pass|fail",
  "status": "kept|reverted|rework|no-op|hook-blocked|timeout",
  "description": "<agent description>",
  "agent": "<agent type>",
  "confidence": 0.0,
  "timestamp": "<ISO>",
  "files": [],
  "ideation_source": "claude"
}
```

`ideation_source`: `"claude"` = Claude specialist proposed; `"codex"` = Phase 2c proposed.

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
IFS= read -r _KEEP < "${TMPDIR:-/tmp}/research-run-keep-items-${CSID}" 2>/dev/null || _KEEP=""
_STATE_JSON=".experiments/state/${_RUN_ID}/state.json"
_ITER=$(jq -r '.iteration // 0' "$_STATE_JSON" 2>/dev/null || echo "?")
_BEST=$(jq -r '.best_metric // "?"' "$_STATE_JSON" 2>/dev/null || echo "?")
_PROG=$(jq -r '.program_file // ""' "$_STATE_JSON" 2>/dev/null || echo "")
_KEEP_APPEND=""; [ -n "$_KEEP" ] && _KEEP_APPEND="; user-keep: $_KEEP"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/write_skill_contract.py" "research:run" "iteration-loop (after iter ${_ITER})" ".experiments/${_RUN_ID}" "state-json=${_STATE_JSON}, program=${_PROG}, iter=${_ITER}, best-metric=${_BEST}, cat-once-files=_shared/codemap-context.md + modes/compute-docker.md + modes/codex-copilot.md + modes/phase5-metric.md (re-cat after compaction only)${_KEEP_APPEND}" "continue R5 from iter $(( _ITER + 1 )) or proceed to R6 when loop done"  # timeout: 5000
```

#### Phase 9 — Progress checks

- **Summary every SUMMARY_INTERVAL iterations**: print compact table (iteration, metric, delta, status) for last N iterations.
- **Stuck detection**: if last `STUCK_THRESHOLD` entries all have `status: reverted|no-op|hook-blocked`, trigger escalation (see `<constants>`). Log escalation action.
- **Diminishing returns**: if last `DIMINISHING_RETURNS_WINDOW` kept entries each improved < 0.5%, warn and suggest stopping. No auto-stop — user decides.
- **Early stop**: if `target` set, stop when metric crosses it. Mark `state.json` `status: goal-achieved`.
- **Context compaction** (every SUMMARY_INTERVAL): write full iteration summary to `.experiments/state/<run-id>/progress-<i>.md`, discard verbose per-iteration details from working memory. Retain only: current metric, iteration count, JSONL path, `best_commit`. Full history recoverable from `experiments.jsonl` and `ideation-<i>.md`.

**After campaign loop completes** (outside per-iteration loop):

```bash
# fresh shell — $COMMIT_SENTINEL gone, re-derive path before rm or cleanup is a silent no-op on ""
eval "$(bash "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/git_slugs.sh")"  # timeout: 3000
rm -f "${TMPDIR:-/tmp}/claude-commit-auth-${REPO_SLUG}-${BRANCH_SLUG}"  # timeout: 3000  (best-effort; commit-guard.js owns lifecycle)  # tmpdir-exempt: user-shell-boundary
```

### Step R6: Results report

Pre-compute branch before writing: `BRANCH=$(git branch --show-current 2>/dev/null | tr '/' '-' || echo 'main')` — deliberate second slug form, report paths only; commit sentinels use `git_slugs.sh`/`BRANCH_SLUG` (SENTINEL_SLUG_FORMULA). Not a bypass — retracted audit finding.

```bash
mkdir -p .reports/research  # timeout: 3000
```

Write full report to `.reports/research/run-$BRANCH-$(date +%Y-%m-%d).md` via Write tool. Do not print to terminal. Anti-overwrite: if file exists, append counter suffix (e.g. `-2.md`): `OUT=".reports/research/run-$BRANCH-$(date +%Y-%m-%d).md"; BASE="$OUT"; COUNT=2; while [ -f "$OUT" ]; do OUT="${BASE%.md}-${COUNT}.md"; COUNT=$((COUNT+1)); done`

Follow `modes/report.md`:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CLAUDE_SKILL_DIR < "${TMPDIR:-/tmp}/research-run-skill-dir-${CSID}" 2>/dev/null || CLAUDE_SKILL_DIR=""
cat "$CLAUDE_SKILL_DIR/modes/report.md"  # timeout: 5000
```

`state.json`: `status = completed`.

### Step R7: Codex delegation (optional)

Skip R7 if `CODEX_DELEGATION_AVAILABLE=false` (warning already printed at R2 — no further action needed).

Inspect applied changes (`git diff <baseline_commit>...<best_commit> --stat`), identify tasks Codex can complete (comments on non-obvious changes, docstrings for modified functions, test coverage).

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _RESEARCH_SHARED < "${TMPDIR:-/tmp}/research-shared-${CSID}" 2>/dev/null || _RESEARCH_SHARED=""  # warm read (Check 41) — bash state lost between Bash() calls
cat "$_RESEARCH_SHARED/codex-delegation.md"  # timeout: 5000
```

Apply criteria loaded above.

Print next-step suggestions as plain text — do NOT call `AskUserQuestion`: both `/research:retro` and `/research:verify` ship `disable-model-invocation: true` and `run`'s `allowed-tools` has no `Skill` entry, so neither is dispatchable this turn.

```text
Next: /research:retro <run-id>       — post-run retrospective analysis
Next: /research:verify <paper>       — verify implementation matches paper claims
```

```bash
rm -f .temp/state/skill-contract.md  # clear contract — campaign complete (compaction-contract.md §Lifecycle)  # timeout: 5000
```

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
IFS= read -r CLAUDE_SKILL_DIR < "${TMPDIR:-/tmp}/research-run-skill-dir-${CSID}" 2>/dev/null || CLAUDE_SKILL_DIR="plugins/cc_research/skills/run"
cat "$CLAUDE_SKILL_DIR/modes/colab-setup.md"  # timeout: 5000
```

</workflow>

<notes>

- **Commit before verify** — enables clean `git revert HEAD` if metric doesn't improve. Never verify before committing.
- **`git revert` over `git reset --hard`** — preserves experiment history, not in deny list.
- **Never `git add -A`** — always stage specific files returned by agent JSON.
- **Never `--no-verify`** — if pre-commit hook blocks, delegate to `foundry:linting-expert` and fix.
- **Guard ≠ Verify** — guard checks regressions (tests, lint); verify checks target metric. Both must pass to keep commit.
- **metric_cmd exit code ignored** — R2 validates metric_cmd by parsing stdout for a float, not by checking exit code. Piping metric output through grep/awk/tr is acceptable; only the final stdout float matters.
- **Guard/metric scripts protected** — ideation agent must not modify the files referenced in `guard_cmd` or `metric_cmd`; do not include them in `scope_files`. New test files may be created within `scope_files` for coverage improvement campaigns.
- **JSONL over TSV** — richer structured fields, `jq`-parseable, no delimiter ambiguity; query with `jq -c 'select(.status == "kept")' experiments.jsonl`.
- **State persistence enables resume** — if loop crashes/times out, `resume` picks up exactly where it stopped.
- **Safety break**: hard cap = 50 iterations (values above 50 in program.md clamped to 50 with a warning); default 20 when max_iterations unset in program.md; skill never exceeds MAX_ITERATIONS.
- **Unbounded cross-skill chain**: `run` → `/research:retro` → `/research:run --hypothesis` / `/research:fortify` → re-run `/research:run` has no campaign-level iteration cap (unlike `sweep`'s `MAX_REFINE = 3` or `run`'s own `MAX_ITERATIONS`). Human-gated at each hop, so it cannot spin autonomously. No counter implemented by design — a counter would need `retro` to write to `.experiments/state/`, breaking retro's read-only invariant.
- **Explicit flags = hard requirements**: all flags (`--colab`, `--compute=docker`, `--codex`, `--researcher`, `--architect`) must be available at R2. If unavailable, stop — never silently degrade.
- R7 Codex delegation needs no other plugin — `codex-delegation.md` ships in this plugin's own `skills/_shared/` and resolves via `bin/resolve_shared.py`. R7 is silently skipped only if that file is missing (broken install).

</notes>
