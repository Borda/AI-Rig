---
name: fortify
description: Systematic ablation study runner. After research:run finds improvements, fortify identifies component candidates from git diff + diary, creates isolated git worktrees per ablation (main repo never modified), runs metric+guard in each worktree, ranks component importance, and optionally generates reviewer Q&A calibrated to a target venue.
argument-hint: '[<run-id>|<program.md>] [--venue <CVPR|NeurIPS|ICML|workshop>] [--max-ablations <N>] [--runs <N>] [--skip-run] [--keep "<items>"]'
effort: high
allowed-tools: Read, Write, Bash, Agent, TaskCreate, TaskUpdate, AskUserQuestion
disable-model-invocation: true
---

<objective>

Ablation study runner — after `/research:run` finds improvements, fortify identifies which components contributed, generates ablation variants (remove one component at a time), runs each in **isolated git worktrees** (main repo never modified), ranks component importance, optionally generates reviewer Q&A calibrated to venue.

NOT for: initial optimization loop (use `/research:run`); methodology validation (use `/research:judge`); paper-vs-code consistency (use `/research:verify`); hypothesis generation (use `research:scientist` directly). Fortify runs ablation studies on completed runs only.

</objective>

<constants>

```yaml
MAX_ABLATION_CANDIDATES:  8 (ceiling — scientist produces 3–8; --max-ablations caps further)
METRIC_TIMEOUT_MS:        360000 (6 min — deliberately larger than run's 120s/300s VERIFY_TIMEOUT_SEC, to accommodate slower ablation-variant runs)
GUARD_TIMEOUT_MS:         360000
GIT_OP_TIMEOUT_MS:        15000
METRIC_RUNS_DEFAULT:      3 (metric runs per variant; --runs <N> overrides, 1–10; 1 = no σ, threshold = noise floor alone)
SANITY_DIVERGENCE_PCT:    2.0 (full-variant mean vs best_metric tolerance, widened to 2σ_full; beyond it fortify aborts)
NOISE_FLOOR_PCT:          2.0 (% of |full mean|; an ablation counts as an effect only beyond max(noise floor, 2σΔ))
IMPORTANCE_CLASS_CRITICAL: 50.0 (% of full metric lost)
IMPORTANCE_CLASS_SIGNIFICANT: 10.0
IMPORTANCE_HARMFUL_PCT:   -5.0 (importance below this = Potentially Harmful; [-5.0, -threshold) = NEUTRAL)
FORTIFY_DIR_BASE:         .experiments
STATE_DIR_BASE:           .experiments/state
METRIC_CMD_SOURCE:        state.json .config.metric_cmd (campaign's real metric — no fail-open default; env METRIC_CMD overrides)
GUARD_CMD_SOURCE:         state.json .config.guard_cmd (campaign's real guard — no fail-open default; env GUARD_CMD overrides)
```

</constants>

**Environment overrides** — set these before invoking the skill to override per-variant defaults:

- `METRIC_CMD` — command run inside each variant worktree to measure the ablation metric (override; default sourced from source-run `state.json` `.config.metric_cmd`)
- `GUARD_CMD` — command run inside each variant worktree to detect regressions (override; default sourced from source-run `state.json` `.config.guard_cmd`)
- `STATE_DIR_BASE` — base directory for source-run state lookups (default: `.experiments/state`)
- `FORTIFY_DIR_BASE` — base directory for per-run fortify artifacts (default: `.experiments`)
- `SANITY_DIVERGENCE_PCT` / `NOISE_FLOOR_PCT` — full-variant reproduction tolerance and ablation noise floor, in percent (defaults above)

The constants block defaults above are YAML-only — bash blocks read environment variables (with `${VAR:-default}` fallback) and never source values directly from YAML.

<compaction>

- Key boundaries: end of F2 — ablation candidates identified by scientist; end of F4 — all ablation variant worktrees completed.
- Preserve at F2: FORTIFY_DIR (TMPDIR key), RUN_ID (TMPDIR key), ablation-candidates.jsonl path, best_metric from source run.
- Preserve at F4: FORTIFY_DIR, RUN_ID, results.jsonl and metric-runs.jsonl paths, fortify-runs (TMPDIR key) — ready for F5 importance ranking.
- 4d inner loop is resume-safe too: the run count per variant comes from `metric-runs.jsonl`, never memory, so a compaction mid-variant resumes at the next run.
- F4 loop is resume-safe: 4a-init's resume guard skips variants already terminal (non-timeout) in results.jsonl, so a mid-loop compaction resumes at the first pending variant — never re-runs a completed ablation; a timed-out variant still retries.
- Clear at F1 start (stale prior run) and at start of F8 terminal summary.

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

## CRITICAL: Worktree-based isolation

**Do NOT use `git checkout -b <branch>` for ablations** — dirties main working tree, corrupts concurrent tool calls. Each ablation gets own git worktree under `$FORTIFY_DIR/worktrees/<variant>`, created from `best_commit`. Main working tree NEVER modified. Cleanup: `git worktree remove --force` per variant; `git worktree prune` on interrupt.

## Fortify Mode (Steps F1–F8)

Triggered by `fortify` or `fortify <run-id|program.md>`.

**Task tracking**: create tasks for F1, F2, F3, F4, F5, F6, F7, F8 at start, all in the same response as the first real tool call. Every later `TaskUpdate` rides with the next real tool call — never a bookkeeping-only turn; the one standalone call is a `completed` right before a long report.

## Step F1: Locate source run, parse flags, and validate judge approval

Extract flags: `--venue <VENUE>`, `--max-ablations <N>`, `--runs <N>`, `--skip-run`, `--keep "<items>"`.

**Unsupported flag check**: one block extracts `--keep` / `--venue`, then loads the protocol below to follow. Supported flags for this skill: `--venue`, `--max-ablations`, `--runs`, `--skip-run`, `--keep`.

```bash
# loads: unsupported-flag-protocol.md
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/extract-keep-flag.py" fortify "$ARGUMENTS" --venue-choices CVPR,NeurIPS,ICML,workshop || exit $?  # timeout: 5000 — keep-items + venue sentinels; empty venue → F6 skip rule; exit 2 on an invalid venue
IFS= read -r _RESEARCH_SHARED < "${TMPDIR:-/tmp}/research-shared-${CSID}" 2>/dev/null || _RESEARCH_SHARED=""  # warm read (Check 41)
cat "$_RESEARCH_SHARED/unsupported-flag-protocol.md"
```

**Input resolution** (priority order):

1. Explicit `<run-id>` argument → read `$STATE_DIR_BASE/<run-id>/state.json`
2. Explicit `<program.md>` argument → scan `$STATE_DIR_BASE/*/state.json` for matching `program_file`, pick latest with `status: completed` or `status: goal-achieved`
3. No argument → scan `$STATE_DIR_BASE/`, pick latest with `status: completed` or `status: goal-achieved`
4. None found → stop:
   ```text
   fortify: No completed run found. Run /research:run first.
   ```

> `METRIC_CMD`/`GUARD_CMD` are NOT defaulted — sourced from source-run `state.json` after `$RUN_ID` resolves (block below). No fail-open default: `git diff --stat HEAD` always exits 0, so a defaulted guard would be a no-op that never catches ablation regressions.

**One block** initializes the base directories (constants YAML is not auto-exported; environment overrides honored), assigns `$RUN_ID` from input resolution above, sources `metric_cmd`/`guard_cmd` from the campaign — env override first, then source-run `state.json` `.config`, fail closed if neither present — and scans for the judge verdict. Judge skill writes it to `.reports/research/judge-<branch>-<date>.md`; the scan looks for the APPROVED verdict line (exit 1 = BLOCKED):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
STATE_DIR_BASE="${STATE_DIR_BASE:-.experiments/state}"
FORTIFY_DIR_BASE="${FORTIFY_DIR_BASE:-.experiments}"
# probe first: jq missing would empty METRIC_CMD/GUARD_CMD below → misreported as `.config missing`
command -v jq >/dev/null 2>&1 || { echo "! MISSING — jq not on PATH; fortify reads state.json with jq. Install jq, then re-run /research:fortify."; exit 1; }
# --keep-state: no eval; prefix fortify-args so the raw --venue value never overwrites the validated fortify-venue sentinel; --inlined: fortify's first block already cleared
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/parse-skill-flags.py" --flags skip-run --value-flags venue,max-ablations,runs --keep-state fortify-args --inlined "$ARGUMENTS" >/dev/null  # timeout: 5000
IFS= read -r VALUE_RUNS < "${TMPDIR:-/tmp}/fortify-args-runs-${CSID}" 2>/dev/null || VALUE_RUNS=""
IFS= read -r VALUE_MAX_ABLATIONS < "${TMPDIR:-/tmp}/fortify-args-max-ablations-${CSID}" 2>/dev/null || VALUE_MAX_ABLATIONS=""
IFS= read -r CLEAN_ARGS < "${TMPDIR:-/tmp}/fortify-args-clean-args-${CSID}" 2>/dev/null || CLEAN_ARGS=""
FORTIFY_RUNS="${VALUE_RUNS:-3}"  # METRIC_RUNS_DEFAULT
case "$FORTIFY_RUNS" in ''|*[!0-9]*) echo "! BLOCKED — --runs '$FORTIFY_RUNS' is not an integer 1-10"; exit 1;; esac
[ "$FORTIFY_RUNS" -ge 1 ] && [ "$FORTIFY_RUNS" -le 10 ] || { echo "! BLOCKED — --runs $FORTIFY_RUNS outside 1-10"; exit 1; }
echo "$FORTIFY_RUNS" > "${TMPDIR:-/tmp}/fortify-runs-${CSID}"  # for 4d-record + post-loop rank (Check 41)
# degenerate --max-ablations: non-numeric treated same as <=1 (avoids bash arithmetic error on e.g. "abc")
if [ -n "$VALUE_MAX_ABLATIONS" ]; then
  case "$VALUE_MAX_ABLATIONS" in
    *[!0-9]*) echo "⚠ --max-ablations $VALUE_MAX_ABLATIONS leaves no components to ablate beyond the full baseline — importance-ranking table (F7) will be empty. Did you mean a higher value?" ;;
    *) [ "$VALUE_MAX_ABLATIONS" -le 1 ] && echo "⚠ --max-ablations $VALUE_MAX_ABLATIONS leaves no components to ablate beyond the full baseline — importance-ranking table (F7) will be empty. Did you mean a higher value?" ;;
  esac
fi
_ARG1=$(echo "$CLEAN_ARGS" | awk '{print $1}')
if [ -n "$_ARG1" ] && [ "${_ARG1#-}" = "$_ARG1" ] && [ ! -f "$_ARG1" ] && [ -d "$STATE_DIR_BASE/$_ARG1" ]; then
  RUN_ID="$_ARG1"
elif [ -n "$_ARG1" ] && [ -f "$_ARG1" ]; then
  # program.md path  <!-- loads: find_run_id.py -->
  RUN_ID=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/find_run_id.py" "$STATE_DIR_BASE" --match-program "$_ARG1" 2>/dev/null)
else
  RUN_ID=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/find_run_id.py" "$STATE_DIR_BASE" 2>/dev/null)
fi
[ -z "$RUN_ID" ] && { echo "fortify: No completed run found. Run /research:run first."; exit 1; }
echo "$RUN_ID" > "${TMPDIR:-/tmp}/fortify-run-id-${CSID}"  # for later blocks (Check 41)
STATE_JSON="$STATE_DIR_BASE/$RUN_ID/state.json"
METRIC_CMD="${METRIC_CMD:-$(jq -r '.config.metric_cmd // empty' "$STATE_JSON" 2>/dev/null)}"
GUARD_CMD="${GUARD_CMD:-$(jq -r '.config.guard_cmd // empty' "$STATE_JSON" 2>/dev/null)}"
if [ -z "$METRIC_CMD" ] || [ -z "$GUARD_CMD" ]; then
  echo "fortify: BLOCKED — $STATE_JSON .config missing metric_cmd/guard_cmd; ablation needs the campaign's real metric and guard commands."
  echo "Re-run /research:run to regenerate state.json, or set METRIC_CMD/GUARD_CMD in the environment."
  exit 1
fi
# echo not printf — 4d/4e reload via `read -r VAR<f||VAR=""`; no trailing newline → read fails → || wipes value → false BLOCK
echo "$METRIC_CMD" > "${TMPDIR:-/tmp}/fortify-metric-cmd-${CSID}"  # for 4d (Check 41)
echo "$GUARD_CMD" > "${TMPDIR:-/tmp}/fortify-guard-cmd-${CSID}"    # for 4e (Check 41)
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/find_judge_verdict.py" --state-dir-base "${STATE_DIR_BASE:-.experiments/state}" --run-id "$RUN_ID" || exit 1  # timeout: 5000 — writes program-file + judge-verdict sentinels; exit 1 = BLOCKED
```

Verify `JUDGE_VERDICT == "APPROVED"`. The program cross-match above guarantees the verdict was issued for the current experiment — fortify cannot ablate against a different program's verdict. Apply explicit bash gate — prose alone never halts execution:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# REJECTED default = fail closed; gate appends actual verdict itself, message never interpolates it
_GATE_MSG="fortify: BLOCKED — no APPROVED judge verdict found for this program; stopping before implementation (F1–F7).
Ablation studies require an approved baseline. Run: /research:judge <program.md>"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/gate-on-sentinel.py" "${TMPDIR:-/tmp}/fortify-judge-verdict-${CSID}" APPROVED REJECTED "$_GATE_MSG" || exit 1
```

> Note: do NOT infer from `methodology.md` alone — `methodology_rating: sound` is one input to verdict, not verdict itself. Only `## Verdict` line in judge output file is authoritative.

Read from `state.json`: `goal`, `best_metric`, `best_commit`, `config` (including `metric_cmd`, `guard_cmd`, `compute`), `program_file`.

Also read `baseline_commit` — iteration 0 commit from `experiments.jsonl` (first line, `status: "baseline"`, field `"commit"`).

**Pre-compute run directory** (each in separate Bash call):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
FORTIFY_DIR=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/make_run_dir.py" "fortify" ".experiments" 2>/dev/null)  # timeout: 5000
[ -z "$FORTIFY_DIR" ] && { echo "! make_run_dir.py failed — ensure research plugin installed"; exit 1; }
mkdir -p "$FORTIFY_DIR"
echo "$FORTIFY_DIR" > "${TMPDIR:-/tmp}/fortify-dir-${CSID}"  # for F2/F4-loop/F6; make_run_dir non-idempotent (Check 41)
WORKTREE_BASE="$FORTIFY_DIR/worktrees"
mkdir -p "$WORKTREE_BASE"
echo "FORTIFY_DIR=$FORTIFY_DIR"  # prose paths ($FORTIFY_DIR/diff.txt …) substitute this printed value
```

## Step F2: Identify ablation candidates via scientist

Gather two inputs for scientist:

1. **Git diff**: run `git diff <baseline_commit>...<best_commit> --stat` (summary) and full `git diff <baseline_commit>...<best_commit>`. If full diff exceeds ~200 lines, write to `$FORTIFY_DIR/diff.txt` via Write tool; otherwise inline in prompt.
2. **Experiment history**: paths to `experiments.jsonl` and `diary.md` from source run directory.

<!-- policy-sibling: plugins/cc_research/skills/judge/SKILL.md, plugins/cc_research/skills/verify/SKILL.md, plugins/cc_research/skills/plan/SKILL.md, plugins/cc_research/skills/topic/SKILL.md, plugins/cc_research/skills/retro/SKILL.md, plugins/cc_research/skills/topic/modes/plan.md, plugins/cc_research/skills/topic/modes/team.md — Agent-budget spawn-envelope rule restated in each; keep in sync (plugins/CLAUDE.md §Policy Duplication Marker). -->

> **Agent budget** — every spawn prompt must require an envelope even on exhaustion — `partial: true` plus what was finished.

Spawn `research:scientist` via `Agent(subagent_type="research:scientist", prompt="...")` with the post-hoc timeout of the Spawn note below (15-min deadline; `agent-resolution.md` §Agent waits — no polling).

Before building the prompt, substitute all bash variables into a single concrete string — never pass literal `<FORTIFY_DIR>` or `<path>` placeholders to the agent:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
STATE_DIR_BASE="${STATE_DIR_BASE:-.experiments/state}"  # default (Check 41)
IFS= read -r RUN_ID < "${TMPDIR:-/tmp}/fortify-run-id-${CSID}" 2>/dev/null || RUN_ID=""  # reload (Check 41)
IFS= read -r FORTIFY_DIR < "${TMPDIR:-/tmp}/fortify-dir-${CSID}" 2>/dev/null || FORTIFY_DIR=""  # reload (Check 41)
EXPERIMENTS_PATH="$STATE_DIR_BASE/$RUN_ID/experiments.jsonl"
DIARY_PATH="$STATE_DIR_BASE/$RUN_ID/diary.md"
F2_PROMPT="Act as an ML ablation study designer.

Read:
- git diff at ${FORTIFY_DIR}/diff.txt (or inline if small)
- experiments.jsonl at ${EXPERIMENTS_PATH} (filter for entries with status: 'kept')
- diary.md at ${DIARY_PATH} (if exists)

Identify 3-8 distinct logical components changed during this run.
A component = a logically independent change removable on its own.

For each component produce one JSON line to ${FORTIFY_DIR}/ablation-candidates.jsonl:
{
  \"component_id\": <int>,
  \"name\": \"<descriptive name, e.g. 'learning rate warmup'>\",
  \"description\": \"<what it does and why it was introduced>\",
  \"files\": [\"<file:line range>\"],
  \"revert_commits\": [\"<commit SHA>\"],
  \"expected_importance\": \"HIGH|MEDIUM|LOW\"
}

Write your analysis to ${FORTIFY_DIR}/candidates-analysis.md.
Include ## Confidence block.
Return ONLY: {\"status\":\"done\",\"components\":N,\"file\":\"${FORTIFY_DIR}/ablation-candidates.jsonl\",\"confidence\":0.N}"
[ -n "$FORTIFY_DIR" ] || { echo "! BLOCKED — fortify-dir sentinel missing; run the F1 run-dir block first"; exit 1; }
echo "$F2_PROMPT"  # shell variables die with this call — the printed text is what Agent() receives
```

Pass the printed `$F2_PROMPT` text (fully expanded) as the `prompt=` argument to `Agent(...)`.

**Spawn note**: the F2 scientist runs in the background — spawn, then end the turn; no filler call, no "waiting" line, no sleep (CLAUDE.md §6). Timeout is handled post-hoc — on the completion notification, check `$FORTIFY_DIR/ablation-candidates.jsonl`; if missing or empty, stop with `"fortify: Scientist timed out. Check $FORTIFY_DIR/ for partial output."` and surface with ⏱.

Read `ablation-candidates.jsonl` after scientist completes. If `--max-ablations <M>` specified and component count + 1 (for full variant) exceeds M: sort by `expected_importance` (HIGH first, then MEDIUM, then LOW), keep top M-1 components plus always include `full` sanity-check variant. **Log dropped components**: list each dropped component by `component_id` and `expected_importance` in the Step F7 report's Skipped Variants section and the Step F8 terminal summary (text written before a tool call can arrive as an empty progress update) so users can verify the scientist's importance estimates before proceeding. Include this list in the F7 report under `## Dropped Variants`.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# boundary 1: F2 done, candidates ready (compaction-contract.md §Lifecycle)
IFS= read -r _RUN_ID < "${TMPDIR:-/tmp}/fortify-run-id-${CSID}" 2>/dev/null || _RUN_ID=""
IFS= read -r _FORTIFY_DIR < "${TMPDIR:-/tmp}/fortify-dir-${CSID}" 2>/dev/null || _FORTIFY_DIR=""
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/write_skill_contract.py" --keep-sentinel fortify-keep-items "research:fortify" "ablation-execution (after F2 candidates identified)" "${_FORTIFY_DIR}" "run-id=${_RUN_ID}, fortify-dir=${_FORTIFY_DIR}, candidates=${_FORTIFY_DIR}/ablation-candidates.jsonl, variants=${_FORTIFY_DIR}/variants.jsonl" "F3 generate variants → F4 run worktrees sequentially" -- calls_ahead=80 "drop=git diff, scientist prompt and candidate analysis"  # timeout: 5000
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/compaction_boundary.py"  # timeout: 60000 — prints the Boundary step, then the foundry gate decision; follow it
```

**`--skip-run` early exit**: if `--skip-run` flag present, print candidate table (component_id, name, description, files, expected_importance) and exit. No ablation execution. Mark tasks F3, F4, F5, F6, F7 `deleted` via TaskUpdate (`skipped` is not a valid task status). Print all three lines (no `.reports/research/fortify-*.md` is written in `--skip-run` mode — only `ablation-candidates.jsonl` lives under `$FORTIFY_DIR`; surface `$FORTIFY_DIR` explicitly so the user can locate the candidate list):

```text
fortify: --skip-run — <N> candidates identified.
Candidates artifact: $FORTIFY_DIR/ablation-candidates.jsonl
Next: /research:fortify without --skip-run to execute ablations
```

Jump to F8 (skip-run variant).

## Step F3: Generate ablation variants

For each component from F2, one ablation variant: `no-<component-name>` (slugified — lowercase, spaces to hyphens). Plus one `full` variant (sanity check — should reproduce `best_metric`), always **line 1**: 4a-init refuses any other first line, and no ablation runs until `full` completed within tolerance.

Write variant configs to `$FORTIFY_DIR/variants.jsonl` via Write tool — one JSON line per variant:

```json
{"variant_name": "full", "component_removed": null, "revert_commits": [], "revert_strategy": "none"}
{"variant_name": "no-<name>", "component_removed": "<name>", "revert_commits": ["<sha1>", "<sha2>"], "revert_strategy": "git-revert"}
```

## Step F4: Run ablation variants via worktrees

Run each variant **sequentially** — parallel worktrees would conflict.

**Before loop — store original working directory and pre-create worktree-paths accumulator:**

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
ORIG_DIR="$(pwd)"  # timeout: 3000
echo "$ORIG_DIR" > "${TMPDIR:-/tmp}/fortify-orig-dir-${CSID}"  # for 4f cleanup cd (Check 41)
IFS= read -r FORTIFY_DIR < "${TMPDIR:-/tmp}/fortify-dir-${CSID}" 2>/dev/null || FORTIFY_DIR=""  # reload (Check 41)
[ -n "$FORTIFY_DIR" ] || { echo "! BLOCKED — fortify-dir sentinel missing; cannot create cleanup accumulator. Aborting."; exit 1; }
# accumulator lives in the run dir, fixed name: a shared-TMPDIR pointer file could be repointed at an arbitrary append target
: > "$FORTIFY_DIR/worktree-paths.txt" || { echo "! BLOCKED — cannot create $FORTIFY_DIR/worktree-paths.txt (cleanup accumulator). Aborting."; exit 1; }
echo 1 > "${TMPDIR:-/tmp}/fortify-variant-idx-${CSID}"  # 1-based cursor — loop's only identity across fences (Check 41)

STATE_DIR_BASE="${STATE_DIR_BASE:-.experiments/state}"  # default (Check 41)
IFS= read -r RUN_ID < "${TMPDIR:-/tmp}/fortify-run-id-${CSID}" 2>/dev/null || RUN_ID=""  # reload (Check 41)
best_commit=$(jq -r '.best_commit // empty' "$STATE_DIR_BASE/$RUN_ID/state.json" 2>/dev/null)
echo "$best_commit" > "${TMPDIR:-/tmp}/fortify-source-best-commit-${CSID}"  # raw value (Check 41)

# F-04: must be concrete SHA — branch tip would advance + pollute history on later revert
if ! best_commit_sha=$(git rev-parse --verify "$best_commit^{commit}" 2>/dev/null); then
    echo "! BLOCKED — best_commit '$best_commit' does not resolve to a commit. Re-run source experiment or correct state.json."
    exit 1
fi
best_commit="$best_commit_sha"  # worktree add always sees a SHA → detached HEAD
echo "$best_commit" > "${TMPDIR:-/tmp}/fortify-best-commit-${CSID}"  # for 4a worktree-add (Check 41)
```

**On interrupt** (user abort or unexpected error mid-loop): `cd "$ORIG_DIR"` first, then `git worktree prune` (`timeout: 15000`) to clean up partially created worktrees before exiting. Interrupt cleanup is manual by necessity — a shell `trap` cannot survive the Bash call that registers it (see the no-trap note below 4a), so the accumulator file is the durable record of what needs removing; re-run the post-loop sweep block below to drain it.

**Loop control**: run 4a-init through 4g-advance once per line of `variants.jsonl`, in file order. The iteration cursor lives in the `fortify-variant-idx-${CSID}` sentinel (initialized to 1 above, advanced at 4g-advance — or by the resume guard on skip) — never in a shell variable, which would die between blocks. Stop the loop when 4a-init reports the cursor is past the last line.

Each Bash call costs a ~12 s round-trip, so adjacent steps sharing a first token with no model decision between them are merged: 4a-init now carries the cursor read, resume guard, and cleanup pre-registration in one block, taking the loop from 13 calls per variant to 11. The remaining splits are load-bearing, not oversight — each is annotated at its own step (4b/4b-assert and the 4f pair keep `cd` out of compound commands; 4c's two calls keep `git` in first-token position; 4d and 4e each hold a `timeout: 360000` and cannot share one call under the 600 s ceiling; 4g-advance must follow 4g's on-disk record). Do not "optimize" them back together. Repeated metric runs add, per run, one 4d call plus a `.rec` Write and the 4d-record block — N runs cannot share one call for the same 600 s reason.

**4a-init. Read the current variant's spec by cursor + resume guard + pre-register cleanup path** (one block — must run before any 4a/4b/4c/4d/4e block, which all read the name it persists):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r FORTIFY_DIR < "${TMPDIR:-/tmp}/fortify-dir-${CSID}" 2>/dev/null || FORTIFY_DIR=""  # reload (Check 41)
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/fortify_next_variant.py" -- "$FORTIFY_DIR"  # timeout: 5000 — cursor + resume guard + cleanup pre-registration
```

`! BLOCKED` or `FORTIFY_LOOP_DONE=1` printed → do NOT run 4a–4g for this iteration; halt the loop and continue at the post-loop step. `! BLOCKED` covers the full-first gate too: line 1 not `full`, or — past line 1 — the `full` record not `completed` (`diverged`, `metric-failed`, absent). `FORTIFY_SKIP_VARIANT=1` printed → the cursor is already advanced: go straight back to 4a-init for the next variant. `FORTIFY_STALE_WORKTREE=1` printed → an interrupted earlier attempt left this variant's worktree directory: run both 4f blocks once, then 4a — otherwise `git worktree add` fails and 4b would `cd` into the stale tree with its half-applied revert.

**4a. Create isolated worktree at best_commit:**

```bash
# inline: `git` stays first token (allow-list match); CSID inlined, same reason (Check 41)
git worktree add "$(cat "${TMPDIR:-/tmp}/fortify-dir-${CLAUDE_CODE_SESSION_ID:-$PPID}" 2>/dev/null)/worktrees/$(cat "${TMPDIR:-/tmp}/fortify-variant-name-${CLAUDE_CODE_SESSION_ID:-$PPID}" 2>/dev/null)" "$(cat "${TMPDIR:-/tmp}/fortify-best-commit-${CLAUDE_CODE_SESSION_ID:-$PPID}" 2>/dev/null)"  # timeout: 15000
```

(Cleanup registration already happened in 4a-init — the path was appended to the accumulator BEFORE creation, so an interrupt right after `worktree add` is still covered.)

**No `trap` here — deliberate.** A `trap ... EXIT` registered in this fence fires when *this block* ends, moments after `git worktree add` created the worktree: it would delete the worktree the loop is about to use, 4b's `cd` would fail, and 4c's `git revert` would then execute in the **main working tree** — the exact outcome the worktree-isolation invariant exists to prevent. Trap disposition is per-process and cannot outlive its own Bash call, so it can neither cover a later interrupt nor survive to the next block. Cleanup is therefore explicit: 4f per variant (happy path), plus the post-loop sweep and `git worktree prune` below (interrupted or skipped variants). The accumulator is pre-created via `mktemp` before the loop; `mktemp` ensures collision-free naming across concurrent invocations.

**4b. Navigate into worktree** (two separate Bash calls — cd first, then command; CWD persists between calls):

```bash
# inline: `cd` stays first token; CSID inlined, same reason (Check 41)
cd "$(cat "${TMPDIR:-/tmp}/fortify-dir-${CLAUDE_CODE_SESSION_ID:-$PPID}" 2>/dev/null)/worktrees/$(cat "${TMPDIR:-/tmp}/fortify-variant-name-${CLAUDE_CODE_SESSION_ID:-$PPID}" 2>/dev/null)"  # timeout: 3000
```

**4b-assert. Confirm the `cd` actually landed inside a worktree** — run before 4c, in its own Bash call so CWD carries over:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r ORIG_DIR < "${TMPDIR:-/tmp}/fortify-orig-dir-${CSID}" 2>/dev/null || ORIG_DIR=""  # reload (Check 41)
IFS= read -r FORTIFY_DIR < "${TMPDIR:-/tmp}/fortify-dir-${CSID}" 2>/dev/null || FORTIFY_DIR=""  # reload (Check 41)
IFS= read -r VARIANT_NAME < "${TMPDIR:-/tmp}/fortify-variant-name-${CSID}" 2>/dev/null || VARIANT_NAME=""  # reload (Check 41)
# FORTIFY_DIR is relative to ORIG_DIR; the assert runs from the worktree, so anchor it first
case "$FORTIFY_DIR" in /*) ;; *) FORTIFY_DIR="$ORIG_DIR/$FORTIFY_DIR";; esac
# exact physical-path match — a substring test passes in any cwd under a `worktrees/` dir, e.g. the main repo inside .claude/worktrees
[ -n "$ORIG_DIR" ] && [ -n "$VARIANT_NAME" ] && [ "$(pwd -P)" = "$(realpath "$FORTIFY_DIR/worktrees/$VARIANT_NAME" 2>/dev/null)" ] || { echo "! BLOCKED — not inside a variant worktree (4b cd failed); refusing to run revert/metric/guard against the main repo"; exit 1; }  # timeout: 3000
```

> **Not merged into 4b — deliberate.** Folding the assertion into the `cd` call would put directory navigation and a command in one Bash call, which `claude-config.md` §Directory Navigation Commands forbids outright: a prefix allow-rule earned by the leading `cd` covers whatever rides behind the `&&`, and here what rides behind it is the gate protecting the main working tree from 4c's `git revert`. The saved ~12 s isn't worth widening that grant.

`! BLOCKED` printed → do NOT run 4c/4d/4e. Nothing was created to clean up beyond the worktree itself: go to 4f, then 4g, then 4g-advance. Without this assertion a failed `cd` leaves CWD at the repo root and 4c's `git revert` commits into the user's checked-out branch.

**4c. Apply revert (skip for `full` variant):**

For `full` variant: no changes — skip both Bash calls below and go to 4d.

For `no-<component>` variant: revert component's commits.

**IMPORTANT — order matters**: revert **newest first** to avoid conflicts. The block orders the SHAs itself from the commit graph, whatever order the scientist wrote `revert_commits` in.

**Bash call 1 — extract and order revert_commits via jq + `git rev-list`** (jq-based JSONL filter avoids per-iteration approval prompt):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r FORTIFY_DIR < "${TMPDIR:-/tmp}/fortify-dir-${CSID}" 2>/dev/null || FORTIFY_DIR=""  # reload (Check 41)
IFS= read -r VARIANT_NAME < "${TMPDIR:-/tmp}/fortify-variant-name-${CSID}" 2>/dev/null || VARIANT_NAME=""  # reload (Check 41)
# full has nothing to revert; empty the sorted sentinel so a stray Bash call 2 cannot replay the previous variant's SHAs
case "$VARIANT_NAME" in variant-full) : > "${TMPDIR:-/tmp}/fortify-revert-sorted-${CSID}"; echo "full variant — no revert; skip Bash call 2, run 4d"; exit 0;; esac
REVERT_COMMITS_RAW=$(jq -r --arg vn "$VARIANT_NAME" 'select(.variant_name==$vn) | .revert_commits[]' "$FORTIFY_DIR/variants.jsonl" 2>/dev/null | tr '\n' ' ')  # timeout: 5000
# no shell loop — emit skip token so 4d/4e are genuinely bypassed
[ -z "$REVERT_COMMITS_RAW" ] && { echo "⚠ No revert_commits for $VARIANT_NAME — skipping"; echo '{"variant":"'$VARIANT_NAME'","status":"revert-missing"}' >> "$FORTIFY_DIR/results.jsonl"; echo "FORTIFY_SKIP_VARIANT=1"; exit 0; }
# topo-order newest-first, independent of input order; one unresolvable SHA fails the whole call → empty → skip
REVERT_COMMITS_SORTED=$(git rev-list --topo-order --no-walk $REVERT_COMMITS_RAW 2>/dev/null | tr '\n' ' ')
[ -z "$REVERT_COMMITS_SORTED" ] && { echo "⚠ revert_commits do not resolve for $VARIANT_NAME — skipping"; echo '{"variant":"'$VARIANT_NAME'","status":"revert-missing"}' >> "$FORTIFY_DIR/results.jsonl"; echo "FORTIFY_SKIP_VARIANT=1"; exit 0; }
echo "$REVERT_COMMITS_SORTED" > "${TMPDIR:-/tmp}/fortify-revert-sorted-${CSID}"  # for git-revert (Check 41)
```

**Bash call 2 — apply revert** (separated so first-token allow-list matches `git`):

```bash
# inline, unquoted (multi-SHA word-split); `git` stays first token (Check 41)
git revert $(cat "${TMPDIR:-/tmp}/fortify-revert-sorted-${CLAUDE_CODE_SESSION_ID:-$PPID}" 2>/dev/null) --no-edit  # timeout: 15000
```

`FORTIFY_SKIP_VARIANT=1` printed by the block above → the worktree exists and must still be removed: jump to 4f (cleanup), skipping 4d/4e. The block already wrote this variant's `results.jsonl` line, so 4g has nothing to add — but still run 4g-advance, or the loop re-runs this variant forever.

If revert produces merge conflicts: Write `{"variant":"<name>","status":"revert-conflict",...}` to `$FORTIFY_DIR/results.jsonl.rec` with the Write tool and run 4g's append block, then jump to 4f (cleanup), then 4g-advance.

**4d. Run metric_cmd in worktree:**

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r METRIC_CMD < "${TMPDIR:-/tmp}/fortify-metric-cmd-${CSID}" 2>/dev/null || METRIC_CMD=""  # reload (Check 41)
[ -z "$METRIC_CMD" ] && { echo "fortify: BLOCKED — metric_cmd not initialized in F1"; exit 1; }
bash -c "$METRIC_CMD"; METRIC_EXIT=$?  # timeout: 360000 (state.json .config.metric_cmd) — bash -c parses operators/pipes; bare expansion only word-splits, so `true && false` would exit 0
echo "METRIC_EXIT=$METRIC_EXIT"
exit "$METRIC_EXIT"  # a trailing assignment would make the block exit 0 and hide the status
```

Parse stdout for the numeric metric value. A non-zero exit (the block's status, echoed as `METRIC_EXIT`) or no numeric output — even a printed number after a non-zero exit — is a failed run: record it with `"metric":null`. A 4d call killed by its Bash timeout records no run: write `status: "timeout"` at 4g after 4f (the resume guard retries it).

**4d-record. Stage the run and decide the next step** — each variant's metric runs `fortify-runs` times (`--runs`, default 3) in the same worktree, so one number never decides an ablation. After every 4d call, Write exactly one line to `$FORTIFY_DIR/metric-runs.jsonl.rec` with the Write tool, then run the fixed block below (`append_ledger.py` appends it, `fortify_metric_stats.py` counts this variant's runs on the ledger — the ledger, never memory, is the inner-loop cursor):

```json
{"variant":"<variant-name sentinel value>","metric":0.0,"exit":0}
```

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r ORIG_DIR < "${TMPDIR:-/tmp}/fortify-orig-dir-${CSID}" 2>/dev/null || ORIG_DIR=""  # reload (Check 41)
IFS= read -r _FDIR < "${TMPDIR:-/tmp}/fortify-dir-${CSID}" 2>/dev/null || _FDIR=""
IFS= read -r VARIANT_NAME < "${TMPDIR:-/tmp}/fortify-variant-name-${CSID}" 2>/dev/null || VARIANT_NAME=""
IFS= read -r FORTIFY_RUNS < "${TMPDIR:-/tmp}/fortify-runs-${CSID}" 2>/dev/null || FORTIFY_RUNS=3
IFS= read -r _RUN_ID < "${TMPDIR:-/tmp}/fortify-run-id-${CSID}" 2>/dev/null || _RUN_ID=""
[ -n "$ORIG_DIR" ] && [ -n "$_FDIR" ] && [ -n "$VARIANT_NAME" ] || { echo "! fortify: BLOCKED — orig-dir/fortify-dir/variant-name sentinel missing; metric-runs.jsonl.rec left in place"; exit 1; }
# CWD is the variant worktree (4b); relative run-dir and state paths resolve from ORIG_DIR
case "$_FDIR" in /*) ;; *) _FDIR="$ORIG_DIR/$_FDIR";; esac
_STATE_BASE="${STATE_DIR_BASE:-.experiments/state}"; case "$_STATE_BASE" in /*) ;; *) _STATE_BASE="$ORIG_DIR/$_STATE_BASE";; esac
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/append_ledger.py" "$_FDIR/metric-runs.jsonl"  # timeout: 5000
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/fortify_metric_stats.py" status "$_FDIR" --variant "$VARIANT_NAME" --runs "$FORTIFY_RUNS" --state-json "$_STATE_BASE/$_RUN_ID/state.json" --divergence-pct "${SANITY_DIVERGENCE_PCT:-2.0}"  # timeout: 5000 — full variant: divergence gate vs best_metric
```

Branch on the printed marker:

| Marker | Next |
| -- | -- |
| `FORTIFY_METRIC_NEXT_RUN=<k>` | run 4d again (same worktree) |
| `FORTIFY_METRIC_DONE=1 … mean=… sigma=…` | 4e; carry `mean`/`sigma` into the 4g record |
| `FORTIFY_METRIC_FAILED=1` | `status: "metric-failed"`; 4f, 4g, 4g-advance |
| `FORTIFY_FULL_DIVERGED=1` + `! BLOCKED` | full only: `status: "diverged"`; skip 4e; 4f, 4g, then halt the loop → post-loop step |

**4e. Run guard_cmd in worktree:**

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r GUARD_CMD < "${TMPDIR:-/tmp}/fortify-guard-cmd-${CSID}" 2>/dev/null || GUARD_CMD=""  # reload (Check 41)
[ -z "$GUARD_CMD" ] && { echo "fortify: BLOCKED — guard_cmd not initialized in F1"; exit 1; }
bash -c "$GUARD_CMD"; GUARD_EXIT=$?  # timeout: 360000 (state.json .config.guard_cmd) — bash -c parses operators/pipes; bare expansion would falsely pass e.g. `true && false`
echo "GUARD_EXIT=$GUARD_EXIT"
exit "$GUARD_EXIT"
```

> **`bash -c`, not `eval "$CMD"`** — 4d/4e run inside the per-variant worktree that 4b `cd`'d into (cwd persists across Bash calls here); `bash -c` forks a child that inherits that cwd, keeping the 4b-assert guard intact, whereas a `metric_cmd`/`guard_cmd` containing `cd` under `eval` could silently relocate work outside the worktree. Assumes a POSIX `bash` binary is available wherever these blocks run — consistent with this file's existing POSIX-shell idioms (`IFS= read -r`).

Record guard result: `"pass"` (exit 0) or `"fail"` (non-zero).

**4f. Cleanup worktree (INVARIANT — must execute even if 4c/4d/4e fail):**

```bash
# inline: `cd` stays first token; CSID inlined, same reason (Check 41)
# grep -m1 . fails on empty/missing sentinel → bogus path → cd errors; bare `cd ""` succeeds and stays inside the worktree 4f removes
cd "$(grep -m1 . "${TMPDIR:-/tmp}/fortify-orig-dir-${CLAUDE_CODE_SESSION_ID:-$PPID}" 2>/dev/null || echo /fortify-orig-dir-missing)"  # timeout: 3000
```

`cd` errors → do NOT run the remove call below (CWD still the variant worktree): print `! BLOCKED — fortify-orig-dir sentinel missing or empty; worktree left in place for manual cleanup` and continue at 4g.

```bash
# inline: `git` stays first token; CSID inlined, same reason (Check 41)
git worktree remove --force "$(cat "${TMPDIR:-/tmp}/fortify-dir-${CLAUDE_CODE_SESSION_ID:-$PPID}" 2>/dev/null)/worktrees/$(cat "${TMPDIR:-/tmp}/fortify-variant-name-${CLAUDE_CODE_SESSION_ID:-$PPID}" 2>/dev/null)"  # timeout: 15000
```

> **These two stay separate, and the cursor advance stays out of both — deliberate.** Chaining `git worktree remove --force` behind the `cd` puts navigation and a command in one call (`claude-config.md` §Directory Navigation Commands) and lets a `cd` allow-rule carry a force-remove. Advancing the cursor here would move it *before* 4g writes the result: an interrupt in that window leaves a variant with an advanced cursor and no `results.jsonl` line, so the resume guard has nothing to match on and 4a-init skips the ablation entirely — a silently dropped variant, which is worse than the re-run that the un-advanced cursor produces today.

**4g. Record result** — stage one JSON line for `$FORTIFY_DIR/results.jsonl`: Write it alone to `$FORTIFY_DIR/results.jsonl.rec` with the Write tool, then run the fixed append block below. The line carries model-decided values (metric, guard, status), so an `echo`/`printf` with those values inline would differ every variant and never match a blueprint digest; staging the values in `.rec` keeps the bash text fixed, and `append_ledger.py` (allowed via `python`) appends it without rewriting earlier lines:

```json
{"variant":"<name>","component_removed":"<name or null>","metric":0.0,"sigma":0.0,"runs":3,"guard":"pass|fail","status":"completed|revert-conflict|metric-failed|diverged|timeout","timestamp":"<ISO>"}
```

`metric`/`sigma` = the `mean`/`sigma` printed by 4d-record's `FORTIFY_METRIC_DONE` line (`null` when not done; `sigma` `null` for one run). The per-run values stay in `metric-runs.jsonl`, which the post-loop `rank` step reads.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _FDIR < "${TMPDIR:-/tmp}/fortify-dir-${CSID}" 2>/dev/null || _FDIR=""
[ -n "$_FDIR" ] || { echo "! fortify: BLOCKED — FORTIFY_DIR sentinel missing; results.jsonl.rec left in place"; exit 1; }
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/append_ledger.py" "$_FDIR/results.jsonl"  # timeout: 5000
```

Deltas are not part of this record — the post-loop step writes them to the `results-deltas.jsonl` sidecar once the `full` baseline is known.

**4g-advance. Move the cursor to the next variant** — runs only after 4g's append block put the line in `results.jsonl`, so cursor and record advance together:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _VIDX < "${TMPDIR:-/tmp}/fortify-variant-idx-${CSID}" 2>/dev/null || _VIDX=1
echo "$((_VIDX + 1))" > "${TMPDIR:-/tmp}/fortify-variant-idx-${CSID}"
```

Then return to 4a-init.

**Full-first gate** — `full` is `variants.jsonl` line 1, so its 4g record exists before any ablation ran. A worktree holds tracked files only: gitignored inputs (data, venv, `.env`) are absent, and absolute paths in `metric_cmd` measure the main tree instead. The `full` mean over its N runs must reproduce `best_metric` within `max(SANITY_DIVERGENCE_PCT, 2σ_full)` — the 2σ term (not σ/√N: `best_metric` is one draw) keeps a legitimately noisy metric from aborting. 4d-record decides it in code: `FORTIFY_FULL_DIVERGED=1` → record `diverged`. Any full status but `completed` → print `⚠ fortify: full variant <status> — worktree does not reproduce the source run`, halt the loop at once and continue at the post-loop step (its guard reports `full-variant-failed`); no ablation compute is spent on an unreproduced baseline. 4a-init enforces the same gate if the loop is resumed past line 1.

After all variants processed (4a-init printed `FORTIFY_LOOP_DONE=1`) — sweep any worktree the per-variant 4f missed, then prune:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r FORTIFY_DIR < "${TMPDIR:-/tmp}/fortify-dir-${CSID}" 2>/dev/null || FORTIFY_DIR=""
# replaces removed 4a-trap — same accumulator, read once worktrees are finished
WORKTREE_PATHS_FILE="$FORTIFY_DIR/worktree-paths.txt"
if [ -n "$FORTIFY_DIR" ] && [ -f "$WORKTREE_PATHS_FILE" ]; then
    while IFS= read -r _wt; do
        [ -n "$_wt" ] && [ -d "$_wt" ] && git worktree remove --force "$_wt" 2>/dev/null
    done < "$WORKTREE_PATHS_FILE"
    rm -f "$WORKTREE_PATHS_FILE"
fi
rm -f "${TMPDIR:-/tmp}/fortify-variant-idx-${CSID}"
```

```bash
git worktree prune  # timeout: 15000
```

`prune` only drops registrations whose directory is already gone. The inverse — directory present, never removed because the accumulator entry was never written (interrupt between `worktree add` and the append) — is invisible to it and to `git worktree list`. Sweep the variant root for those, then write the boundary-2 compaction contract in the same call — no state-changing decision sits between them, so they ride one turn.

> `--managed-prefix 'variant-'` matches every fortify variant dir (all named `variant-<slug>`) under `--root`, fortify's own variant directory. Never widen it to a shared root; `'*'` is rejected. `--include-orphans` keeps the unregistered-dir sweep this step exists for — without it those dirs are only reported. Variants holding uncommitted work are reported, not deleted; symlinks are never followed. `--apply` is unattended here for parity with the `git worktree remove --force` above (same trees, same run) — but print the output verbatim whenever it removed anything, so the sweep is never silent.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _FDIR < "${TMPDIR:-/tmp}/fortify-dir-${CSID}" 2>/dev/null || _FDIR=""
[ -n "$_FDIR" ] && python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/heal_git_artifacts.py" worktrees --root "$_FDIR/worktrees" --managed-prefix 'variant-' --include-orphans --min-age-days 1 --apply  # timeout: 30000
# boundary 2: F4 all variants complete (compaction-contract.md §Lifecycle)
IFS= read -r _RUN_ID < "${TMPDIR:-/tmp}/fortify-run-id-${CSID}" 2>/dev/null || _RUN_ID=""
IFS= read -r _FORTIFY_DIR < "${TMPDIR:-/tmp}/fortify-dir-${CSID}" 2>/dev/null || _FORTIFY_DIR=""
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/write_skill_contract.py" --keep-sentinel fortify-keep-items "research:fortify" "post-ablation (after F4 worktrees complete)" "${_FORTIFY_DIR}" "run-id=${_RUN_ID}, fortify-dir=${_FORTIFY_DIR}, results=${_FORTIFY_DIR}/results.jsonl, metric-runs=${_FORTIFY_DIR}/metric-runs.jsonl, deltas=${_FORTIFY_DIR}/results-deltas.jsonl (post-loop)" "F5 rank importance → F6 reviewer Q&A → F7 report" -- calls_ahead=12 "drop=per-variant worktree, metric and guard output"  # timeout: 5000
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/compaction_boundary.py"  # timeout: 60000 — prints the Boundary step, then the foundry gate decision; follow it
```

**Post-loop delta computation**:

**Guard — full variant must exist and be completed** (run FIRST, before the `results.jsonl` read below — a guard at F5 fires too late, values are already persisted by then). Check `results.jsonl` for a row with `variant == "full"`. If that row is absent (jq returns empty) OR present with `status != "completed"` (e.g. `metric-failed`, or `diverged` — the full mean did not reproduce `best_metric`; 4c never runs a revert for `full`, so `revert-missing` cannot occur):

```text
! fortify: full variant ended '<status>' (or: full variant row missing) — cannot compute ablation deltas without a baseline metric. Re-run the source campaign or investigate the full-variant failure before fortifying. For 'diverged': the worktree lacks gitignored inputs (data, venv, .env) or metric_cmd uses absolute paths into the main tree.
```

Mark F5, F6, F7 `deleted` via TaskUpdate (mirrors the `--skip-run` precedent at F2/F8); jump directly to the `full-variant-failed` terminal block in F8, carrying the error text and `$FORTIFY_DIR` path. Do NOT write an F7 report in this case — mirrors `--skip-run`'s behavior exactly, the per-variant record still lives in `$FORTIFY_DIR`.

If the guard passes, compute every delta in code — never by hand. The block reads the per-run values from `metric-runs.jsonl` (first N per variant), the statuses from `results.jsonl` and `direction` from the program file's `## Metric` block (default `higher`), and writes the one-shot sidecar `$FORTIFY_DIR/results-deltas.jsonl`, one line per completed `no-<component>` variant: `variant`, `runs`, `metric` (mean), `sigma`, `full_metric`, `full_sigma`, `delta_from_full`, `delta_pct` (signed, `ablated − full`; 0 when `full_metric == 0`), `importance_pct`, `sigma_delta_pct`, `threshold_pct`, `class`. `results.jsonl` is never rewritten, so every per-variant record stays exactly as 4g appended it. A `! BLOCKED` from `rank` means the same as the guard above.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _FDIR < "${TMPDIR:-/tmp}/fortify-dir-${CSID}" 2>/dev/null || _FDIR=""
IFS= read -r FORTIFY_RUNS < "${TMPDIR:-/tmp}/fortify-runs-${CSID}" 2>/dev/null || FORTIFY_RUNS=3
IFS= read -r PROGRAM_FILE < "${TMPDIR:-/tmp}/fortify-program-file-${CSID}" 2>/dev/null || PROGRAM_FILE=""
[ -n "$_FDIR" ] || { echo "! fortify: BLOCKED — FORTIFY_DIR sentinel missing; cannot rank ablations"; exit 1; }
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/fortify_metric_stats.py" rank "$_FDIR" --runs "$FORTIFY_RUNS" --program-file "$PROGRAM_FILE" --noise-floor-pct "${NOISE_FLOOR_PCT:-2.0}"  # timeout: 5000
```

## Step F5: Rank component importance

For each `no-<component>` variant with `status: "completed"`, take `importance_pct`, `threshold_pct` and `class` from `results-deltas.jsonl` — `fortify_metric_stats.py rank` applied these rules over the means of N runs:

- `direction` from the `## Metric` block in `program_file` (`higher` or `lower`); absent → `higher`.

- **Signed delta** (positive = removal hurt metric → component helpful), on the run means:

  ```python
  signed_delta = (full_mean - ablated_mean) * (1 if direction == "higher" else -1)
  importance = signed_delta / abs(full_mean) * 100 if full_mean != 0 else 0
  sigma_delta = sqrt(
      full_sigma**2 / full_runs + ablated_sigma**2 / ablated_runs
  )  # SE of the difference; sigma = sample σ (n-1), 0 for one run
  threshold = max(NOISE_FLOOR_PCT, 2 * sigma_delta / abs(full_mean) * 100)
  ```

- **BORDERLINE**: `|importance| <= threshold` — the effect is within run-to-run noise (its ±2σΔ interval spans zero). Never ranked as an effect; see Borderline below.

- Importance class (helpful components — `importance > threshold`):

  - **CRITICAL**: importance > 50%
  - **SIGNIFICANT**: importance 10–50%
  - **MARGINAL**: importance < 10%

- **NEUTRAL**: `-5% <= importance < -threshold` — removal moved the metric beyond noise but within tolerance in the improving direction. Ranked after the helpful components, marked as such (empty when the threshold reaches 5%).

**Sign convention check** — after computing signed_delta for all variants, verify:

- For `direction == 'higher'`: all helpful components should have `signed_delta >= 0` (ablated metric < full)

- For `direction == 'lower'`: all helpful components should have `signed_delta >= 0` (ablated metric > full; removing component worsened metric)

- Any component where `signed_delta` has unexpected sign: flag explicitly in report as "sign anomaly — verify ablation ran correctly"

- **Potentially Harmful** class: `importance < -5%` (compare `importance`, not raw `signed_delta`, which is in metric units) — removing component IMPROVED metric. Surface in dedicated `Potentially Harmful Components` report section; not ranked in main table.

**Borderline components** (class `BORDERLINE` — `importance ± 2σΔ` spans zero, or the effect sits under the noise floor). Do NOT classify as MARGINAL. Instead:

- Add to a "Borderline Components" subsection in the F7 report, with `importance_pct` and `threshold_pct`
- Note: "within noise (|importance| ≤ threshold) — component may be neutral or harmful; more runs (`--runs`) required before including in model"
- Do not rank these in the main importance table; surface separately

**Coupling check** — before sorting, scan the ablation candidates (from `ablation-candidates.jsonl`) for notes on architectural dependencies. For any pair where one component explicitly requires the other (noted in `description` field or `candidates-analysis.md`):

- Mark both components in the ranking with `[COUPLED]` suffix
- Add a note: "Independent ablation unreliable — recommend joint ablation of [A + B]"
- Add to the "Skipped Variants" section: `joint-[A]-[B] — not run (joint ablation recommended)`
- Surface as a `! WARNING` in the F7 report before the ranking table

Sort by importance descending (helpful and NEUTRAL components; Potentially Harmful ones stay in their own section). Write to `$FORTIFY_DIR/importance-ranking.json` via Write tool — JSON array with fields: `rank`, `component`, `full_metric`, `ablated_metric`, `signed_delta_pct`, `importance_pct`, `threshold_pct`, `class` (`CRITICAL`/`SIGNIFICANT`/`MARGINAL`/`NEUTRAL`/`HARMFUL`).

**Sanity check**: the full-first gate already aborted on divergence, so reaching F5 means the `full` mean reproduced `best_metric`. Report 4d-record's `full-first gate: PASS — full mean X vs best_metric Y (gap ≤ tolerance)` line in the F7 Sanity Check section.

## Step F6: Reviewer Q&A (optional — `--venue` only)

Skip entirely if no `--venue` flag. Supported venues: `CVPR`, `NeurIPS`, `ICML`, `workshop`.

Spawn `research:scientist` via `Agent(subagent_type="research:scientist", prompt="...")`. **Spawn note**: runs in the background — spawn, then end the turn; no filler call, no "waiting" line, no sleep (CLAUDE.md §6). On the completion notification, check `$FORTIFY_DIR/reviewer-qa.md`; if missing or empty, treat as timed out and surface with ⏱.

Before building the prompt, substitute all bash variables into a single concrete string — never pass literal `<FORTIFY_DIR>`, `<path>`, or `<venue>` placeholders to the agent:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r VENUE < "${TMPDIR:-/tmp}/fortify-venue-${CSID}" 2>/dev/null || VENUE=""  # reload F1 --venue parse (Check 41)
[ -z "$VENUE" ] && { echo "fortify: F6 skipped — no --venue flag"; exit 0; }  # no default venue: an unset flag must skip, not silently review at workshop bar
IFS= read -r PROGRAM_FILE < "${TMPDIR:-/tmp}/fortify-program-file-${CSID}" 2>/dev/null || PROGRAM_FILE=""  # reload (Check 41)
IFS= read -r FORTIFY_DIR < "${TMPDIR:-/tmp}/fortify-dir-${CSID}" 2>/dev/null || FORTIFY_DIR=""  # reload (Check 41)
[ -z "$PROGRAM_FILE" ] && { echo "! fortify: BLOCKED — PROGRAM_FILE not set; re-invoke from F1"; exit 1; }  # absolute path resolved in F1
F6_PROMPT="Act as a peer reviewer for ${VENUE}.

Read:
- ablation results at ${FORTIFY_DIR}/results.jsonl
- per-variant deltas from the full baseline at ${FORTIFY_DIR}/results-deltas.jsonl
- importance ranking at ${FORTIFY_DIR}/importance-ranking.json
- original program.md at ${PROGRAM_FILE}

Generate:
1. 5-7 likely reviewer questions calibrated to ${VENUE} standards
   (CVPR/NeurIPS/ICML: expect thorough ablations, statistical significance, compute budget justification; workshop: lighter bar)
2. For each question: a data-backed answer referencing specific ablation results
3. A supplementary material draft section with the ablation table (LaTeX-ready)

Write to ${FORTIFY_DIR}/reviewer-qa.md.
Include ## Confidence block.
Return ONLY: {\"status\":\"done\",\"questions\":N,\"file\":\"${FORTIFY_DIR}/reviewer-qa.md\",\"confidence\":0.N}"
[ -n "$FORTIFY_DIR" ] || { echo "! BLOCKED — fortify-dir sentinel missing; run the F1 run-dir block first"; exit 1; }
echo "$F6_PROMPT"  # shell variables die with this call — the printed text is what Agent() receives
```

Pass the printed `$F6_PROMPT` text (fully expanded) as the `prompt=` argument to `Agent(...)`.

**Health monitoring**: same as F2 (15-min deadline, checked on the completion notification — no polling). On timeout: note `"Reviewer Q&A: timed out"` in report, continue to F7.

## Step F7: Write fortify report

Pre-compute the report path (anti-overwrite appends a counter suffix — `-2.md`, `-3.md`, … — when the file exists):

```bash
mkdir -p .reports/research  # timeout: 3000
BRANCH=$(git branch --show-current 2>/dev/null | tr '/' '-' || echo 'main')  # timeout: 3000
OUT=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/resolve-anti-overwrite-path.py" .reports/research "fortify-$BRANCH-$(date +%Y-%m-%d)")  # timeout: 5000
echo "OUT=$OUT"
```

Write full report to the printed `OUT` via Write tool.

```markdown
---
Title:       Fortify — [goal]
Date:        [YYYY-MM-DD]
Scope:       [run-id] / [N] components identified / [N] runs per variant
Focus:       ablation study / component importance ranking
Agents:      research:scientist (F2, F6)
Outcome:     [N] critical · [N] significant · [N] marginal · [N] borderline components
Top:         [component-name] (importance: X.X% · CRITICAL|SIGNIFICANT|MARGINAL)
Confidence:  [score] — [key gaps]
Next steps:  simplify by removing marginal components, re-run /research:run
Path:        → .reports/research/fortify-<branch>-<date>.md
---

## Fortify Report: <goal>

**Source run**: <run-id>
**Date**: <date>
**Baseline commit**: <best_commit>
**Components identified**: <N>
**Ablations run**: <N completed> of <N+1 planned>

### Sanity Check (full variant)
Full metric: <mean> ± <σ> over <N> runs (expected from run: <best_metric>) — PASS (gap <g>% ≤ tolerance <t>%)

### Component Importance Ranking

| Rank | Component | Full Metric | Ablated Metric | Signed Δ | Importance | Class |
|------|-----------|-------------|----------------|----------|------------|-------|
| 1    | ...       | ...         | ...            | +X.X%    | X.X%       | CRITICAL |

### Potentially Harmful or Borderline Components

Components that either:
- Improved the metric when removed (`importance < -5%`) — **Potentially Harmful**
- Sit within noise (`|importance| ≤ max(noise floor, 2σΔ)`) — **Borderline** (insufficient evidence of contribution)

| Component | Full Metric | Ablated Metric | Signed Δ | Status |
|-----------|-------------|----------------|----------|--------|
| ...       | ...         | ...            | -X.X%    | Potentially Harmful |
| ...       | ...         | mean ± σ       | ±X.X% ≤ thr | Borderline |

(Omit section entirely if no harmful or borderline components found.)

### Ablation Matrix

Metric (mean ± σ over N runs), Guard, Status from `results.jsonl`; Delta from Full = `delta_pct` from `results-deltas.jsonl` (`n/a` for variants absent there).

| Variant       | Metric | Guard | Status           | Delta from Full |
|---------------|--------|-------|------------------|-----------------|
| full          | ...    | pass  | completed        | baseline        |
| no-component1 | ...    | pass  | completed        | -X.X%           |
| no-component2 | ...    | n/a   | revert-conflict  | n/a             |

### Skipped Variants
<list any revert-conflict, metric-failed, or timeout variants with reason>

### Reviewer Q&A
<section from F6 if --venue was specified; otherwise omit this section entirely>

Full artifacts: <FORTIFY_DIR>/

## Confidence
**Score**: 0.NN — [high|moderate|low]
**Gaps**:
- (-0.NN) [specific limitation]
```

## Step F8: Terminal summary

```bash
rm -f .temp/state/skill-contract.md  # contract done (compaction-contract.md §Lifecycle)  # timeout: 5000
```

Print compact terminal summary:

```text
---
Fortify — <goal>
Source run:   <run-id>
Sanity:       full=<mean> ± <σ> (expected <best_metric>) — PASS
Components:   <N> identified · <N> ablations completed · <N> runs each
Top:          <component-name> (importance: X.X% · CRITICAL|SIGNIFICANT|MARGINAL)
Marginal:     <N> components < 10% each
Borderline:   <N> components within noise (more --runs needed)
Venue Q&A:    generated for <venue> | n/a
-> saved to .reports/research/fortify-<branch>-<date>.md
-> ablation artifacts: <FORTIFY_DIR>/
---
Next: simplify model by removing marginal components, re-run /research:run
```

If `--skip-run` used (early exit at F2): replace ablation lines with:

```text
---
Fortify — <goal> (--skip-run)
Source run:   <run-id>
Components:   <N> candidates identified — ablations not executed
-> candidates: <FORTIFY_DIR>/ablation-candidates.jsonl
-> analysis:   <FORTIFY_DIR>/candidates-analysis.md
---
Next: run /research:fortify without --skip-run to execute ablations
```

If `full` variant failed post-loop (outcome `full-variant-failed`, F5/F6/F7 skipped): replace ablation lines with:

```text
---
Fortify — <goal> (full-variant-failed)
Source run:   <run-id>
Error:        <full-variant error text — missing row or non-completed status>
-> ablation artifacts: <FORTIFY_DIR>/
---
Next: investigate the full-variant failure (check <FORTIFY_DIR>/results.jsonl), then re-run /research:fortify
```

</workflow>

<calibration>

Calibratable: F1–F3 sub-steps only — synthetic ablation plan with known component importance order; score whether fortify correctly ranks components and identifies reviewer questions. Full F4–F6 execution loop (worktree creation, metric runs, real guard scripts) excluded — requires live git state and metric commands.

See the domain table entry for `/research:fortify` for the full ground-truth checklist: `/foundry:calibrate` (requires `foundry` plugin), `modes/skills.md`.

</calibration>

<notes>

- **Worktree invariant** — cleanup (`git worktree remove --force`) must run even if metric/guard fails. No stale worktrees. Final `git worktree prune` catches missed cleanup.
- **Main repo never modified** — all ablation work in worktrees. Main working tree stays clean.
- **Sequential execution** — variants run one at a time. Parallel worktrees would require separate detached HEADs and complicate cleanup.
- **Repeated runs cost compute** — each variant's metric runs N times (`--runs`, default 3, 1–10), so a study costs about `(components + 1) × N` metric runs plus one guard run per variant. `--runs 1` restores single-run cost, but σ is unavailable and every classification rests on the noise floor alone.
- **Worktrees hold tracked files only** — gitignored inputs (data, venv, `.env`) are absent and absolute paths in `metric_cmd` measure the main tree; the full-first gate aborts the study when either makes `full` miss `best_metric`.
- **Bash tool `timeout` parameter** — never shell `timeout` wrapper. Pass `timeout: <ms>` on Bash tool call.
- **Judge prerequisite** — fortify refuses without APPROVED judge verdict. Prevents ablation on unapproved methodologies.
- **`--skip-run` for planning** — generates candidate list without running ablations. Useful for reviewing what would be ablated before committing compute.
- **`--skip-run` scope**: flag skips ablation *execution* only — source run (`research:run`) must already be complete. Does not affect source run.
- **Fortify run directories** don't write `result.jsonl` — exempt from 30-day TTL cleanup (per `.claude/rules/foundry-artifact-lifecycle.md`, installed via `/foundry:setup` — requires `foundry` plugin: no `result.jsonl` = cleanup skipped); remove manually when done (`rm -rf .experiments/fortify-*/`) <!-- policy-sibling: plugins/cc_research/skills/judge/SKILL.md, plugins/cc_research/skills/plan/SKILL.md, plugins/cc_research/skills/retro/SKILL.md, plugins/cc_research/skills/verify/SKILL.md — TTL-exemption note (no result.jsonl → skip 30-day cleanup) restated in each; keep in sync (plugins/CLAUDE.md §Policy Duplication Marker). -->
- **Compute mode**: local execution only. `--compute` and `--colab` passthrough not implemented — contributions welcome. Until then, fortify runs `metric_cmd`/`guard_cmd` directly in each worktree on local machine.
- **Revert conflicts expected** — when commits interleave (component A's commit touches same lines as B's), revert may conflict. Recorded as `revert-conflict`, not treated as error.

</notes>
