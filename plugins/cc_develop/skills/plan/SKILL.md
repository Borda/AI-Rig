---
name: plan
description: 'Analysis-only planning — classify and scope a task without writing code; outputs a structured plan to .plans/active/. TRIGGER when: user wants to understand scope and risks before implementation; phrases: "plan this", "scope out X", "what would it take to Y", "analyse before we start". SKIP when: user already knows what to build and wants code immediately (use `/develop:feature` or `/develop:fix` directly); `.claude/` config planning (use `/foundry:manage`).'
argument-hint: <goal> [--no-challenge] [--codemap] [--no-codemap] [--max-depth <N>]
effort: medium
allowed-tools: Read, Write, Bash, Grep, Glob, Agent, TaskList, TaskCreate, TaskUpdate, AskUserQuestion, WebFetch
disable-model-invocation: true
---

<objective>

Analysis-only. Produces structured plan, no code. Use to understand scope, risks, effort before `/develop:feature`, `/develop:fix`, `/develop:refactor`.

NOT for: code/tests (use develop mode); `.claude/` config (use `/foundry:manage` (requires foundry plugin)).

- non-Python-only projects (JS/TS/Go/Rust, no Python source) — downstream develop skills assume pytest; planning analysis is language-agnostic but downstream implementation needs language-native toolchain
- mixed refactor+feature tasks — run /develop:refactor first, then /develop:feature

</objective>

<workflow>

<!-- Agent resolution: see _DEV_SHARED/agent-resolution.md (resolved via dev_shared_resolve.py and explicitly Read in workflow) -->

## Agent Resolution

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo "" > "${TMPDIR:-/tmp}/dev-agent-watch-dir-${CSID}"  # a prior run's dir must not outlive a failed --init; agent-resolution.md §Agent waits recreates it when empty
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_shared_resolve.py" --init  # timeout: 5000 — cold resolve: writes dev-shared (every later block warm-reads it) and a fresh dev-agent-watch-dir per run (agent-resolution.md §Agent waits)
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_load_shared.py" agent-resolution.md
```

Contains: foundry check + fallback table. If foundry not installed: substitute each `foundry:X` with `general-purpose` per table. Agents this skill uses: `foundry:sw-engineer` (analysis and the merged sw-engineer + qa-specialist feasibility pass), `foundry:challenger`.

**Checkpoint**: plan single-pass — `.plans/active/<slug>` file existence = implicit resume signal. No `.developments/` checkpoint needed; interrupted → re-run `/develop:plan` to regenerate (no code changes made).

**Task hygiene** — task tools may be deferred; load before first use: `ToolSearch(query="select:TaskList,TaskCreate,TaskUpdate,TaskGet", max_results=4)`. Call `TaskList` first and triage each task it returns: `completed` if work clearly done, `deleted` if orphaned, keep `in_progress` only if genuinely continuing. Never spend a turn on bookkeeping alone — every `TaskCreate`/`TaskUpdate` ships in the same response as the next substantive tool call; one exception, `TaskUpdate(completed)` immediately before a long output block (`rules/task-lifecycle.md`).

## Flag parsing

Parse flags into shell variables (not prose) so downstream blocks see correct values. Persist to **per-invocation namespaced** temp dir for cross-block access (bash resets between calls). The dir name carries `$$` and the session token `CSID`; the `dev-plan-ns-current-${CSID}` sentinel is what later blocks read to find it, so two concurrent sessions never share a namespace:

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
PLAN_NS="${TMPDIR:-/tmp}/dev-plan-$$-${CSID}"
mkdir -m 700 -p "$PLAN_NS"
echo "$PLAN_NS" > "${TMPDIR:-/tmp}/dev-plan-ns-current-${CSID}"  # downstream blocks read back namespace
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/extract-keep-flag.py" dev-plan "$ARGUMENTS"  # timeout: 5000 — owner-safe clear of a crashed run's stale contract before plan's first idle gate; plan takes no --keep
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_parse_args.py" --skill plan --write-files "$ARGUMENTS"
# written to ${TMPDIR:-/tmp}/dev-plan-<flag>-${CSID} (legacy paths: SKILL_SPECS["plan"])
cp "${TMPDIR:-/tmp}/dev-challenge-enabled-${CSID}"  "$PLAN_NS/challenge-enabled" 2>/dev/null || echo "true"  > "$PLAN_NS/challenge-enabled"
cp "${TMPDIR:-/tmp}/dev-codemap-raw-${CSID}"        "$PLAN_NS/codemap-raw"       2>/dev/null || echo "auto"  > "$PLAN_NS/codemap-raw"
cp "${TMPDIR:-/tmp}/dev-plan-max-depth-${CSID}"     "$PLAN_NS/max-depth"         2>/dev/null || echo "3"     > "$PLAN_NS/max-depth"
```

Downstream blocks recover namespace then read back, e.g. `IFS= read -r PLAN_NS < "${TMPDIR:-/tmp}/dev-plan-ns-current-${CSID}" 2>/dev/null || PLAN_NS=""; IFS= read -r CODEMAP_ENABLED < "$PLAN_NS/codemap-enabled" 2>/dev/null || CODEMAP_ENABLED=false`.

**Unsupported flag check** — after all supported flags extracted, scan `$ARGUMENTS` for remaining `--<token>` tokens not in the supported list below. If found: invoke `AskUserQuestion` with question text `` ! Unknown flag(s): `--<token>`. Supported: `--no-challenge`, `--codemap`, `--no-codemap`, `--max-depth`. `` — (a) **Abort** (stop, re-invoke with correct flags) · (b) **Continue ignoring** (skip unknown flags, proceed). On Abort: stop.

**Codemap auto-detection** — normalize `CODEMAP_RAW` to `true`/`false`; strict mode hard-fails when codemap unavailable:

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# resolves PLAN_NS itself — plan keeps its flags in a run-namespace dir, not TMPDIR sentinels
CODEMAP_ENABLED=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_codemap_gate.py" plan) || exit 1
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_load_shared.py" codemap-gates.md
```

> loads: codemap-gates.md

Follow Gate A and Gate B. Codemap validation handled by auto-detect block above.

## Step 1: Classify and scope

Determine task type and affected surface.

**Codemap target derivation** — goal names explicit target as `module.path` or `module.path::function` → pre-set `TARGET_MODULE`/`TARGET_FN` so `codemap-context.md` runs caller-impact queries (`rdeps`, `fn-rdeps`) instead of only `central` baseline. No explicit target → both empty → only `central` runs (correct: affected surface unknown until agent searches). The same block then loads **structural context** — only when codemap enabled; value re-read from namespace:

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PLAN_NS < "${TMPDIR:-/tmp}/dev-plan-ns-current-${CSID}" 2>/dev/null || PLAN_NS=""
[ -n "$PLAN_NS" ] || { echo "! BLOCKED — plan namespace sentinel missing; re-run Flag parsing"; exit 1; }
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/parse-skill-flags.py" --flags no-challenge,codemap,no-codemap --value-flags max-depth --keep-state dev-plan-args --inlined "$ARGUMENTS" >/dev/null  # timeout: 5000 — no eval; --inlined: Flag parsing's extract-keep-flag.py already cleared
IFS= read -r CLEAN_ARGS < "${TMPDIR:-/tmp}/dev-plan-args-clean-args-${CSID}" 2>/dev/null || CLEAN_ARGS=""  # CLEAN_ARGS only; a dotted flag value would otherwise outrank the goal's module
printf '%s\n' "$CLEAN_ARGS" | tr '\n' ' ' > "$PLAN_NS/clean-args"  # Step 2 slug source; tr keeps a multi-line goal readable by `read -r`
eval "$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/derive_codemap_target.py" "$CLEAN_ARGS")"  # timeout: 5000 — module.path or module.path::fn; both empty when goal names none
export TARGET_MODULE TARGET_FN
echo "$TARGET_MODULE" > "$PLAN_NS/target-module"   # persist — bash resets between calls
echo "$TARGET_FN"     > "$PLAN_NS/target-fn"
IFS= read -r CODEMAP_ENABLED < "$PLAN_NS/codemap-enabled" 2>/dev/null || CODEMAP_ENABLED=false
echo "CODEMAP_ENABLED=$CODEMAP_ENABLED"
if [ "$CODEMAP_ENABLED" = "true" ]; then
    python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_load_shared.py" codemap-context.md
else
    echo "→ codemap off — skipping structural context"
fi
```

Follow the codemap block per the value echoed. Nothing printed beyond the skip line → proceed.

**Effort sizing (codemap-py)** — `CODEMAP_ENABLED=true` → derive blast-radius tier table from reverse dependencies so complexity estimate is structural, not guessed. Degrades silently when codemap-py absent — plan works unchanged, sizing falls back to agent's file-count heuristic. Run Extended scan (per-target `rdeps` when `TARGET_MODULE` known — the goal names the surface; else `--source=diff` when a partial diff exists, e.g. re-planning after abandoned work, since unrelated dirty `.py` files must not outrank an explicit target):

```bash
# timeout: 15000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PLAN_NS < "${TMPDIR:-/tmp}/dev-plan-ns-current-${CSID}" 2>/dev/null || PLAN_NS=""
[ -n "$PLAN_NS" ] || { echo "! BLOCKED — plan namespace sentinel missing; re-run Flag parsing"; exit 1; }
IFS= read -r CODEMAP_ENABLED < "$PLAN_NS/codemap-enabled" 2>/dev/null || CODEMAP_ENABLED=false
IFS= read -r TARGET_MODULE < "$PLAN_NS/target-module" 2>/dev/null || TARGET_MODULE=""
if [ "$CODEMAP_ENABLED" = "true" ] && command -v codemap-py >/dev/null 2>&1; then
    if [ -n "$TARGET_MODULE" ]; then
        codemap-py query --timeout 5 rdeps "$TARGET_MODULE" --top 10 --exclude-tests > "$PLAN_NS/sizing-rdeps" 2>/dev/null || true
        codemap-py query --timeout 5 coupled --top 10 >> "$PLAN_NS/sizing-rdeps" 2>/dev/null || true
    elif [ -n "$(git diff HEAD --name-only 2>/dev/null | grep '\.py$')" ]; then
        python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/codemap_scan.py" --source=diff > "$PLAN_NS/sizing-rdeps" 2>/dev/null || true
    fi
fi
```

> Interpret `$PLAN_NS/sizing-rdeps` (skip when empty — codemap absent or no target): each `rdeps` block's caller count sets per-module blast tier; `coupled` output lists co-change pairs. Tiers match develop's convention:
>
> - `>= 5` rdeps → **HIGH** blast radius — cross-module reach; nudges complexity toward `large` and adds a Risks entry
> - `1–4` rdeps → **MODERATE** — note affected importers in plan
> - `0` rdeps → **LOW** — self-contained; proceed
>
> Fold highest tier across affected modules into complexity assessment below (HIGH tier or ≥3 affected modules → `large`), and pass tier table + coupled pairs to sw-engineer spawn as `## Structural blast radius` block so scope estimate accounts for downstream callers rather than file count alone.

Arm batch `scope` in the spawn response (`agent-resolution.md` §Agent waits — no polling, per-agent deadlines). Spawn **foundry:sw-engineer** agent with full goal text from `$ARGUMENTS`. Agent should:

- Classify task as `feature`, `fix`, `refactor`, or `debug`
  - `debug`: root cause unknown — symptoms present, cause unclear, investigation needed before fix scoped; classified `debug` → recommend `/develop:debug` first
  - **WARNING**: `/develop:debug` hands off directly to `/develop:fix --diagnosis <path>`, never back here; re-running `/develop:plan` afterwards is a user choice, worth it only when the fix needs its own scoped plan; because that repeat is user-driven and unbounded, cap it via the depth checkpoint below. Caller tracks cycle depth via shared checkpoint file to cap repeated cycles (not a CLI flag — `/develop:debug` has no `--max-depth`). Max depth = `$MAX_DEPTH` (default 3, CLAUDE.md safety break). Before invoking `/develop:debug`, execute depth-checkpoint bash block below:

```bash
# anti-loop guard  # timeout: 3000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PLAN_NS < "${TMPDIR:-/tmp}/dev-plan-ns-current-${CSID}" 2>/dev/null || PLAN_NS=""
[ -n "$PLAN_NS" ] || { echo "! BLOCKED — plan namespace sentinel missing; re-run Flag parsing"; exit 1; }
IFS= read -r MAX_DEPTH < "$PLAN_NS/max-depth" 2>/dev/null || MAX_DEPTH=3
IFS= read -r CLEAN_ARGS < "$PLAN_NS/clean-args" 2>/dev/null || CLEAN_ARGS=""
GOAL_KEY=$(printf '%s' "$CLEAN_ARGS" | cksum | cut -d' ' -f1)  # per-goal cap — an unrelated goal in the same session starts at full depth
DEPTH_FILE="${TMPDIR:-/tmp}/dev-plan-depth-checkpoint-${GOAL_KEY}-${CSID}"
IFS= read -r CURRENT_DEPTH < "$DEPTH_FILE" 2>/dev/null || CURRENT_DEPTH="$MAX_DEPTH"
if [ "$CURRENT_DEPTH" -le 0 ]; then
    echo "! depth limit ($MAX_DEPTH) reached — stopping plan→debug→plan loop"
    # don't invoke /develop:debug — proceed to AskUserQuestion below
else
    NEXT_DEPTH=$(( CURRENT_DEPTH - 1 ))
    echo "$NEXT_DEPTH" > "$DEPTH_FILE"
    echo "→ recommending /develop:debug (depth remaining: $NEXT_DEPTH)"
fi
```

`/develop:debug` is user-invocable only (`disable-model-invocation`): recommend it in the plan output and Final report for the user to run — never call it via `Skill()`.

**Idle-gate contract** — every `AskUserQuestion` gate below that waits on a decision (depth cap, breaking change, Step 4 blockers, Step 5 final) first runs this block, so a `/compact` during the wait resumes losslessly (compaction-contract.md §Lifecycle). It prints `STATE=<path>`: before Step 2 that is the scope file — Write the Step 1 findings (classification, complexity, affected files, `breaking_changes`, `risks`) there with the Write tool before asking; from Step 2 on it is `PLAN_FILE`, already saved. Each such question's text ends with the hint line `` Long wait? `/compact` now — plan state saved at <STATE>, resume lossless. `` (`<STATE>` = the printed path) — in the question text, never as reply prose before the call. Every **Abort** at these gates runs the Step 5 contract-clear block before stopping.

```bash
# compaction boundary — before an idle AskUserQuestion gate
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PLAN_NS < "${TMPDIR:-/tmp}/dev-plan-ns-current-${CSID}" 2>/dev/null || PLAN_NS=""
[ -n "$PLAN_NS" ] || { echo "! BLOCKED — plan namespace sentinel missing; re-run Flag parsing"; exit 1; }
IFS= read -r CLEAN_ARGS < "$PLAN_NS/clean-args" 2>/dev/null || CLEAN_ARGS=""
IFS= read -r PLAN_FILE < "$PLAN_NS/plan-file" 2>/dev/null || PLAN_FILE=""  # empty before Step 2 — namespace is per invocation
SCOPE_FILE=".temp/develop/plan-scope-${CSID}.md"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/write_skill_contract.py" "develop:plan" "idle gate" ".plans/active/" "goal=$CLEAN_ARGS; scope-findings=$SCOPE_FILE; plan-file=${PLAN_FILE:-not written yet}" "resume: re-read plan-file when written, else scope-findings; re-issue the pending AskUserQuestion (depth cap, breaking change, Step 4 blockers or Step 5 Proceed/Revise); Revise edits plan-file in place"  # timeout: 5000
echo "STATE=${PLAN_FILE:-$SCOPE_FILE}"
```

At depth 0: stop, report current plan state, run the Idle-gate contract block, invoke `AskUserQuestion` — "Debug-cycle cap ($MAX_DEPTH) reached for this goal; root cause still unconfirmed. How to proceed?" + the `/compact` hint line · (a) **Write the plan anyway** — continue to Step 2 flagging "root cause unconfirmed" under Risks · (b) **Reset the cap for this goal** — run the reset block below, then recommend `/develop:debug` once more · (c) **Abort**.

Reset block (option b only; rewrites this goal's checkpoint to full depth):

```bash
# timeout: 3000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PLAN_NS < "${TMPDIR:-/tmp}/dev-plan-ns-current-${CSID}" 2>/dev/null || PLAN_NS=""
[ -n "$PLAN_NS" ] || { echo "! BLOCKED — plan namespace sentinel missing; re-run Flag parsing"; exit 1; }
IFS= read -r MAX_DEPTH < "$PLAN_NS/max-depth" 2>/dev/null || MAX_DEPTH=3
IFS= read -r CLEAN_ARGS < "$PLAN_NS/clean-args" 2>/dev/null || CLEAN_ARGS=""
GOAL_KEY=$(printf '%s' "$CLEAN_ARGS" | cksum | cut -d' ' -f1)
echo "$MAX_DEPTH" > "${TMPDIR:-/tmp}/dev-plan-depth-checkpoint-${GOAL_KEY}-${CSID}"
echo "→ depth cap reset to $MAX_DEPTH for this goal"
```

- Identify affected files and modules (search codebase — no guessing)
- Assess complexity: small (1-3 files, self-contained), medium (4-8 files or 1-2 modules), large (cross-module, API changes, or 3+ modules). Effort-sizing block produced tier table → let structural reach override file count: any **HIGH** blast module (≥5 rdeps) or ≥3 affected modules → `large`, regardless of raw file count.
- Return **two separate** structured fields (not merged into flat risks list):
  - `breaking_changes`: list of changes affecting **public API only** — see criteria below; empty list when none
  - `risks`: non-breaking concerns (missing tests, unclear requirements, external dependencies, internal coupling); when effort-sizing tier table flags HIGH/MODERATE modules or coupled pairs, add each as concrete risk (e.g. "changing `<mod>` reaches N downstream callers", "`<a>`/`<b>` co-change coupling")
- Note complexity smells: ambiguous goal, scope creep risk, missing reproduction case, directory-wide refactor without explicit goal

Agent returns findings inline (no file handoff — output short).

**Breaking change gate**: triggers only when `breaking_changes` non-empty — items in `risks` do NOT trigger it. Stop before writing plan. Run the Idle-gate contract block, then call `AskUserQuestion` per breaking change, the first question's text ending with the `/compact` hint line (group only when logically one atomic change). State: what worked before, what breaks, why needed. Options: (a) **Accept breaking change** — proceed with plan as-is · (b) **Revise to non-breaking** — return to Step 1 once with constraint to avoid this breaking change, then re-run this gate on the revised result (second pass offers only (a)/(c)) · (c) **Abort** — stop immediately. Proceed only on explicit (a). Prose question in response body does NOT count — `AskUserQuestion` mandatory per `communication.md`. User selects (c) → stop immediately, do not proceed to Step 2 or beyond.

Breaking change criteria — change is breaking when it affects **public API** (exported from `__init__.py`, documented in README, or stable interface used by external consumers) and any of these apply: removed public API (function, class, method, or module), changed function signatures (parameter names, types, order, or defaults), changed config key names or schema, changed output format (return type, serialization structure, CLI output shape). Internal/private signature changes (functions prefixed `_`, classes not exported) do NOT count as breaking — list under `risks` instead.

## Step 2: Structured plan

Derive filename slug from goal: first 4-5 meaningful words, lowercase, hyphen-separated (e.g. `"improve caching in data loader"` -> `plan_improve-caching-data-loader.md`). If `.plans/active/<slug>` already exists, append counter suffix (`-2`, `-3`, etc.) before writing — never silently overwrite. Run this block once per plan: a Revise (breaking-gate (b), Step 4 blocker fix) edits the existing `PLAN_FILE` in place and does not re-run it — a second run would mint `plan_<slug>-2.md` and leave Step 3's reviewer reading the superseded plan. Store full path as `PLAN_FILE` — used in Steps 3 and Final output.

```bash
# persist — bash resets between calls  # timeout: 3000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PLAN_NS < "${TMPDIR:-/tmp}/dev-plan-ns-current-${CSID}" 2>/dev/null || PLAN_NS=""
[ -n "$PLAN_NS" ] || { echo "! BLOCKED — plan namespace sentinel missing; re-run Flag parsing"; exit 1; }
IFS= read -r CLEAN_ARGS < "$PLAN_NS/clean-args" 2>/dev/null || CLEAN_ARGS=""
mkdir -p ".plans/active"
SLUG=$(printf '%s\n' "$CLEAN_ARGS" | tr '[:upper:]' '[:lower:]' | grep -oE '[a-z0-9]+' | grep -vE '^(a|an|the|in|on|of|to|for|and|or|is|with|at|by|from)$' | head -5 | paste -sd- -)
[ -n "$SLUG" ] || SLUG="unnamed-$(date -u +%Y-%m-%dT%H-%M-%SZ)"  # stopword-only / non-ASCII goal would otherwise yield plan_.md
PLAN_FILE=".plans/active/plan_${SLUG}.md"
N=2
while [ -e "$PLAN_FILE" ]; do
    PLAN_FILE=".plans/active/plan_${SLUG}-${N}.md"
    N=$((N+1))
done
echo "$PLAN_FILE" > "$PLAN_NS/plan-file"
```

> In the saved plan, use one paragraph under `Goal` to state concrete changes and exclusions, explain each affected file, list material risks, and order the suggested implementation steps.

```markdown
# Plan: [goal]

## Brief

*[Generated after agent review — see below]*

---

## Full Plan

**Classification**: feature | fix | refactor
**Complexity**: small | medium | large
**Date**: [YYYY-MM-DD]

### Goal

[goal restatement]

### Affected files

- `path/to/file.py` — reason
- `path/to/other.py` — reason

### Risks

- [risk 1]
- [risk 2]

### Suggested approach

1. [step 1] — verify: [command or observable check proving it done]
2. [step 2] — verify: [check]
3. [step 3] — verify: [check]
...
```

## Step 3: Agent feasibility review

Arm batch `other` in the spawn response (`agent-resolution.md` §Agent waits). Spawn ONE `foundry:sw-engineer` feasibility agent covering both role perspectives in a single pass (merged spawn unit — two role-labelled verdicts, one file read, ~300 bytes JSON; two spawns would each pay ~120,851 tok fixed overhead for the same read):

- **feature / fix / refactor**: one spawn, roles `sw-engineer` (implementation feasibility) + `qa-specialist` (testability, coverage feasibility)
- **debug**: skip feasibility review — no implementation plan to review; proceed directly to Final output with debug recommendation

> `foundry:linting-expert` intentionally excluded — its role is post-implementation static analysis (ruff/mypy), not pre-plan architectural feasibility. Including it produces noise (trivial `ok: true`) or false blockers on linting-config concerns. Surface lint-specific notes (e.g. "target module has no type annotations — mypy will flag everything") in Final output advisory notes instead. The two surviving roles are dimensions of one review, not cross-checks — merging into one spawn preserves both checklists.

Agent receives only plan file path and both role checklists — no conversation history, no unrelated context. Prompt (substitute `<PLAN_FILE>`):

> "Read `<PLAN_FILE>`. Review the plan twice, once from each perspective: (1) as `sw-engineer` — implementation feasibility, architectural risks, blockers; (2) as `qa-specialist` — testability, coverage feasibility, verification blockers. For each role: flag domain-specific concerns, risks, or blockers you see; can that role execute its part autonomously without further user input? Return only a JSON array with exactly two elements, one per role, nothing else: `[{\"a\":\"sw-engineer\",\"ok\":true|false,\"blockers\":[\"...\"],\"q\":[\"...\"],\"concerns\":[\"...\"]},{\"a\":\"qa-specialist\",\"ok\":true|false,\"blockers\":[\"...\"],\"q\":[\"...\"],\"concerns\":[\"...\"]}]`"

**Parse-failure handling**: agent responses may not be valid JSON (especially fallback `general-purpose` agents wrapping JSON in prose). Before processing:

1. The reply is the agent's message in context, never a shell variable — no `echo` pipeline reaches it. Reply is exactly the JSON array → use it. Otherwise Write the reply verbatim with the Write tool to `.temp/develop/<plan-name>-feasibility.txt` (`<plan-name>` = `PLAN_FILE`'s file name without `.md`) and run the recovery block below: with `jq` it prints each balanced JSON object on its own line (one nesting level; breaks on strings containing `{` or `}`) — match each to its role via the `"a":"<ROLE>"` anchor; without `jq`, `extract_json_field.py` recovers only the last balanced object (one role).
2. If extraction succeeds: use extracted objects (one per role)
3. If extraction fails entirely for a role: log `⚠ non-JSON plan response — falling back to prose extraction`; treat that role as `{"a":"<ROLE>","ok":false,"blockers":["agent returned non-JSON response"],"q":[],"concerns":[]}` and enter resolution loop with re-query

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PLAN_NS < "${TMPDIR:-/tmp}/dev-plan-ns-current-${CSID}" 2>/dev/null || PLAN_NS=""
[ -n "$PLAN_NS" ] || { echo "! BLOCKED — plan namespace sentinel missing; re-run Flag parsing"; exit 1; }
IFS= read -r PLAN_FILE < "$PLAN_NS/plan-file" 2>/dev/null || PLAN_FILE=""
[ -n "$PLAN_FILE" ] || { echo "! BLOCKED — plan-file not persisted; re-run Step 2"; exit 1; }
_PLAN_NAME="${PLAN_FILE##*/}"
_REPLY=".temp/develop/${_PLAN_NAME%.md}-feasibility.txt"
[ -s "$_REPLY" ] || { echo "! BLOCKED — feasibility reply not saved at $_REPLY; Write it there first"; exit 1; }
if command -v jq >/dev/null 2>&1; then grep -oE '\{[^{}]*(\{[^{}]*\}[^{}]*)?\}' "$_REPLY" | jq -c '.' 2>/dev/null
else python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/extract_json_field.py" . < "$_REPLY"; fi
```

Verdicts return inline (~300 bytes total — no file handoff). Collect both role results:

- All `ok: true`, empty `blockers`, `q`, `concerns` -> note `✓ agents ready` in final output and proceed
- Any `ok: false`, non-empty `blockers` or `q` -> enter **internal resolution loop** below before surfacing to user
- Non-empty `concerns` with `ok: true` -> surface as advisory notes in final output (not blockers, domain-specific flags user should know before starting)

### Internal resolution loop (max 3 iterations)

Loop body is model-driven (codebase search, agent re-query), so counter lives in a file, not a shell variable — bash resets between calls; an in-prose `ITER` would never be assigned.

Initialize before entering the loop, and again after any Revise that re-enters Step 3 (the 3-pass budget is per plan revision):

```bash
# timeout: 3000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PLAN_NS < "${TMPDIR:-/tmp}/dev-plan-ns-current-${CSID}" 2>/dev/null || PLAN_NS=""
[ -n "$PLAN_NS" ] || { echo "! BLOCKED — plan namespace sentinel missing; re-run Flag parsing"; exit 1; }
echo 0 > "$PLAN_NS/resolution-iter"
```

Then run this block at the top of **every** iteration, before doing any resolution work for that pass:

```bash
# timeout: 3000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PLAN_NS < "${TMPDIR:-/tmp}/dev-plan-ns-current-${CSID}" 2>/dev/null || PLAN_NS=""
[ -n "$PLAN_NS" ] || { echo "! BLOCKED — plan namespace sentinel missing; re-run Flag parsing"; exit 1; }
IFS= read -r ITER < "$PLAN_NS/resolution-iter" 2>/dev/null || ITER=3  # unreadable counter = budget spent, never a fresh 0
if [ "$ITER" -ge 3 ]; then
    echo "! Max feasibility iterations (3) reached — stop the loop and escalate remaining items to user"
else
    echo $(( ITER + 1 )) > "$PLAN_NS/resolution-iter"
    echo "→ resolution pass $(( ITER + 1 )) of 3"
fi
```

Block printed the `!` line → exit the loop immediately and escalate whatever remains unresolved.

For each blocker or open question:

1. **Attempt autonomous resolution** — search codebase, read relevant files, re-read goal. Fetch primary-source docs for relevant issues (official docs, RFCs, library changelogs, migration guides) via WebFetch — known URLs only; WebFetch fetches a specific URL, doesn't search.
   - **Unknown-URL path**: URL needed to resolve blocker unknown (e.g. "what does library X's new API look like?") → do NOT guess/invent URL. Mark blocker `requires-user-input`, skip WebFetch — escalate to user, note documentation lookup required.
   - **Known URL — mandatory verification gate**: after each WebFetch call, before incorporating content into `<PLAN_FILE>`, three-step verification per quality-gates.md link verification: (a) Fetch returned non-error (HTTP 200), (b) Read returned content, (c) Match content against specific blocker — confirm topic alignment. Any step fails → mark URL non-resolving, don't write content to `<PLAN_FILE>`, escalate to user. Each URL needs its own Fetch+Read+Match pass — no exemption for same-domain or "similar" URLs.
   - Answer determinable from verified source → update `<PLAN_FILE>`, mark resolved.
2. **Re-query raising role** — batch ALL items resolved this iteration for that role into ONE re-query (a spawn per item pays ~120,851 tok each): `{"a":"<ROLE>","resolved":[{"item":"<item>","answer":"<resolution>"}, ...]}`. Agent returns updated `ok`/`blockers` for the role; accepted items drop from blockers list.
3. All resolvable items cleared → re-check: all agents `ok: true` -> `✓ agents ready`.

**Plan file coherence**: after resolution loop exits (regardless of outcome), annotate `<PLAN_FILE>`:

- Each resolved blocker: add `(resolved ✓)` inline
- Each unresolved blocker: add `(unresolved — requires user input)`
- Update Brief (once it exists): note "N of M blockers resolved autonomously; N require user input" Ensures plan file coherent after partial resolution.

**Escalate only what can't be resolved autonomously** — blocker requires user input when: depends on business decision, undocumented external constraint, missing credential/secret, or genuine goal ambiguity with two equally valid interpretations.

For each escalated item:

- **Issue**: one sentence — what blocks or is unclear
- **Alternatives**: 2-3 concrete options with trade-offs
- **Recommendation**: which option and why

Do not escalate: items resolvable from codebase, items that are risks (not blockers), items already addressed in plan.

## Step 4: Challenger gate

**Two states** (plan has no diff yet — no small-diff auto-skip, no `--challenge` flag, unlike fix/feature/refactor/debug): by **default** challenger always reviews plan design; `--no-challenge` (`CHALLENGE_ENABLED=false`) **skips gate entirely**.

```bash
# re-hydrate PLAN_FILE + CHALLENGE_ENABLED — bash resets between calls  # timeout: 3000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PLAN_NS < "${TMPDIR:-/tmp}/dev-plan-ns-current-${CSID}" 2>/dev/null || PLAN_NS=""
[ -n "$PLAN_NS" ] || { echo "! BLOCKED — plan namespace sentinel missing; re-run Flag parsing"; exit 1; }
IFS= read -r PLAN_FILE < "$PLAN_NS/plan-file" 2>/dev/null || PLAN_FILE=""
IFS= read -r CHALLENGE_ENABLED < "$PLAN_NS/challenge-enabled" 2>/dev/null || CHALLENGE_ENABLED=true
[ -f "$PLAN_FILE" ] || { echo "plan: PLAN_FILE not found: $PLAN_FILE" >&2; exit 1; }
if [ "$CHALLENGE_ENABLED" = "false" ]; then
    echo "→ --no-challenge passed — skipping Step 4 challenger gate; go to Step 5"
else
    echo "→ challenger gate active for $PLAN_FILE"
fi
```

Block printed the skip line → skip rest of Step 4, go to Step 5. Otherwise spawn `foundry:challenger` to adversarially review written plan before user commits — arm batch `challenge` in the spawn response (`agent-resolution.md` §Agent waits); a timed-out challenger is never read as "no findings" (`AskUserQuestion` per §Agent waits):

> "Read `<PLAN_FILE>`. Challenge plan across all 5 dimensions: Assumptions, Missing Cases, Security Risks, Architectural Concerns, Complexity Creep. Apply mandatory refutation step per your instructions."

Parse result:

- **Blockers found** → STOP. Update `<PLAN_FILE>` with blocker annotations first, then present findings through `AskUserQuestion` — run the Idle-gate contract block right before it (findings summary as question text, `→ <PLAN_FILE>`, ending with the `/compact` hint line): (a) **Revise the plan** — edit `PLAN_FILE` in place for each blocker, re-run Step 3 · (b) **Accept risk and continue** — blockers recorded under Risks · (c) **Abort**. Don't print `/develop` handoff until user resolves each blocker or explicitly accepts risk.
- **Concerns only** → append `### Challenger concerns` to `<PLAN_FILE>` as advisory; continue to Final output.
- **No findings / all refuted** → proceed.

## Step 5: Final output

Compose brief — compact human-readable plan summary after all agent input incorporated:

> Write the plan summary in one sentence, describing its main approach and outcome. Select the steps table that fits the plan: simple, staged/large, or fix. Omit the advisory notes and co-review corrections tables when they have no rows.

```markdown
[plan summary]

Classification : [feature | fix | refactor | debug]
Complexity     : [small | medium | large]
Affected files : [file count] files across [module count] modules
Key risks      : [key risk or none]
Agent review   : ✓ agents ready ([correction count] corrections incorporated)  |  ⚠ see below

[Steps table — columns `# | Step | Verification` for simple plans, `# | Stage | What changes | Stop condition | Verification` for staged or large plans, `# | Action | Target | Verification` for fixes; Verification = the command or observable check that proves the step done]

Advisory notes from agents (omit table if none):

| Agent | Note |
|-------|------|
| [role] | [concern] |

Co-review corrections applied ([agent count] agents, omit table if none):

| Agent | Location | Change |
|-------|----------|--------|
| [agent] | [file or step] | [correction] |
```

**Write brief into `<PLAN_FILE>`**: replace `*[Generated after agent review — see below]*` placeholder in `## Brief` with composed brief. File now contains both brief and full plan.

**Build terminal brief** (the brief is already saved in `<PLAN_FILE>`):

```text
Plan -> <PLAN_FILE>

[brief content exactly as written to the file]

-> /develop:<classification> <goal> --plan <PLAN_FILE> when ready  [debug: -> /develop:debug <goal> first — it hands off to /develop:fix --diagnosis <path>]
```

If unresolved items escalated, include each after brief and save them in `<PLAN_FILE>`:

```text
⚠ Issue: [one-sentence issue]
  Alternatives: (a) [first option] (b) [second option] (c) [third option]
  Recommendation: [recommended option] — [reason]
```

Before invoking `AskUserQuestion`, print the saved brief and any unresolved items once as the last reply text before the question, as normal Markdown; omit the handoff line until the answer. Run the Idle-gate contract block (Step 1) before the question. The question text carries a short plain summary: classification, counts, headline, `→ <PLAN_FILE>` and the `/compact` hint line last, at most 6 plain lines (target 600 characters, hard cap 1,500), no table, pipe layout or code fence. Options carry a label and a one-line description, no `preview`: (a) Proceed — print handoff line, continue · (b) Revise plan — return to Step 2 with user edits. Don't print handoff line until user selects (a).

Skill complete — after the handoff line on (a), and on **Abort** at any gate — clear the contract; Revise keeps it, the next gate refreshes it:

```bash
rm -f .temp/state/skill-contract.md  # clear contract — skill complete (compaction-contract.md §Lifecycle)  # timeout: 5000
```

**Handoff contract**: plan file at `<PLAN_FILE>` consumable by downstream skills. Pass via `--plan <PLAN_FILE>` when invoking `/develop:feature`, `/develop:fix`, or `/develop:refactor`. `debug` classification: no downstream plan file — invoke `/develop:debug <goal>` directly; it hands off to `/develop:fix --diagnosis <path>` on completion, never back to `/develop:plan` — re-running `/develop:plan` afterwards is a user choice, only when the fix needs its own scoped plan. Skill receives `--plan <path>` → reads plan file at Step 1 and:

- Extracts `Classification`, `Affected files`, `Risks`, `Suggested approach` — skips cold codebase exploration
- Inherits agent feasibility verdicts already applied
- Uses `Suggested approach` as implementation roadmap

No quality stack, no Codex pre-pass, no review loop. Exit after printing summary.

End plan document with:

```markdown
## Confidence
**Score**: 0.NN — [high ≥0.9 | moderate 0.85–0.9 | low <0.85 ⚠]
**Gaps**:
- (-0.NN) [specific limitation or unverified assumption]

**Refinements**: N passes.
- Pass 1: [what was addressed]
```

</workflow>

<notes>

<!-- Reference only — execution-dead at runtime; included for agent behavioral context -->

## Anti-Rationalizations

| Temptation | Reality |
| -- | -- |
| "The plan is obvious — no need for agent feasibility review" | Feasibility review catches domain-specific blockers (missing test infrastructure, incompatible library constraints, API changes) obvious only in hindsight. |
| "I can scope this during implementation — no need to plan first" | Scope discovered mid-implementation inflates PRs, obscures intent. Plan mode exists to prevent exactly this. |

</notes>
