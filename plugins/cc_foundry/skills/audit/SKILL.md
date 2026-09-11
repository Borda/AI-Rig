---
name: audit
description: Full-sweep quality audit of .claude/ config — cross-references, permissions, inventory drift, model tiers, docs freshness. Scope tokens select what to audit; --upgrade applies docs-sourced improvements; --adversarial runs foundry:challenger + Codex adversarial review; --efficiency sweeps model tiers, token bloat, spawn patterns, boilerplate duplication, and bin/ extraction candidates (extraction performed separately via /distill executables). Fix level chosen via always-fire follow-up gate after report.
argument-hint: '[<scope>...] [--local] [--upgrade | --adversarial | --efficiency] [--skip-gate] [--keep "<items>"]'
disable-model-invocation: true
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, Agent, WebFetch, Skill, TaskCreate, TaskUpdate, TaskList, AskUserQuestion
effort: high
---

<objective>

Full-sweep audit of `.claude/` config + all `plugins/*/` files: agents, skills, rules, settings.json, hooks. Spawns `foundry:curator` per-file, aggregates system-wide for cross-file issues — infinite loops, inventory drift, missing permissions, interop breaks. Reports findings; fix level chosen from follow-up gate.

</objective>

<inputs>

- **$ARGUMENTS**: optional — parse `--flags` first, then resolve remaining tokens as scope

  **Flags** (order independent, any combination with scope):

  - `--local` — audit source tree (`plugins/*/`) not user setup (`.claude/` + installed cache); plugin-dev workflows where local edits not yet installed; sets `LOCAL_MODE=true`
  - `--upgrade` — fetch latest Claude Code docs, filter new features by genuine value, apply: **config** changes (apply + correctness check), **capability** changes (calibrate before → apply → calibrate after → accept if Δrecall ≥ 0 and ΔF1 ≥ 0). Skip to **Mode: upgrade**. Mutually exclusive with `--adversarial` and `--efficiency` — error if combined with either.
  - `--adversarial` (alias: `--challenge`) — adversarial review of all agents + skills in scope using `foundry:challenger` (Phase A) + Codex adversarial pass (Phase B); surfaces issues beyond standard per-file audit; see **Mode: adversarial**. Mutually exclusive with `--upgrade` only; combinable with `--efficiency`.
  - `--efficiency` — cost and efficiency sweep: model tier validation, token bloat detection, unbounded spawn patterns, cross-file boilerplate duplication, missing model declarations, bin/ extraction candidates (Check 33). Generates prioritized cost-reduction plan with estimated savings. Detection only — run `/distill executables` to act on extraction candidates. Skip to **Mode: efficiency**. Mutually exclusive with `--upgrade` only; combinable with `--adversarial`.
  - `--skip-gate` — suppress follow-up gate (for automation pipelines)
  - `--fast` — widen fan-out from `MAX_BATCHES` to `MAX_BATCHES_FAST`, trading tokens for wall-clock. **Not free**: each extra agent costs ~120,851 tok of fixed overhead regardless of how little work it does (see `<constants>`). Use when latency matters more than cost; omit by default. Combinable with every other flag.

  **Legacy positional tokens** (`fix`, `upgrade`, `adversarial`, `challenge`, `ab`, `apply`, `fast`, `full`) — **hard error**: print migration hint and stop. Example: "`fix medium` removed — run `/audit` and pick fix level from gate, or pass `--upgrade` / `--adversarial` as flags."

  **Scope tokens** (positional, space-separated — resolve each token before Step 2):

  - No scope: full sweep — sources per `--local`: **without `--local`** covers `.claude/agents/`, `.claude/skills/`, `.claude/rules/`, hooks, settings, `~/.claude/plugins/cache/` installed; **with `--local`** covers `plugins/*/agents/`, `plugins/*/skills/` + `.claude/` secondary
  - `agents` — restrict sweep to agent files only
  - `skills` — restrict sweep to skill files only
  - `rules` — restrict sweep to rule files only
  - `communication` — restrict sweep to communication governance files: `rules/communication.md`, `rules/quality-gates.md`, `TEAM_PROTOCOL.md`, `skills/_shared/file-handoff-protocol.md`
  - `setup` — restrict to system-config files: `settings.json`, `permissions-guide.md`, hooks, `MEMORY.md`, `README.md`, plugin integration, post-install user state (Checks 1–11, 30, I1, I2, I3); Step 3: `setup` SKILL.md only (one foundry:curator spawn); Checks I1–I3 read `~/.claude/` not `.claude/`
  - `plugin` — plugin integration only: codex plugin (Check 7), foundry plugin + init validation (Check 8, including 8g); Step 3: `setup` SKILL.md only (one foundry:curator spawn)
  - `plugins` — full audit of all plugins: per-file audit of every `plugins/*/agents/*.md` and `plugins/*/skills/*/SKILL.md` + integration checks (7, 8) per plugin
  - `plugins <name>` — same as `plugins` scoped to one plugin: `plugins/<name>/agents/*.md` + `plugins/<name>/skills/*/SKILL.md` + integration checks; `<name>` must match dir under `plugins/` (e.g. `plugins foundry`, `plugins oss`, `plugins research`)
  - `<plugin-name>` — **tier 2 shorthand**: bare plugin dir name (e.g. `oss`, `foundry`, `research`, `develop`, `codemap`) auto-resolved when token matches dir under `plugins/`; equivalent to `plugins <name>`; no `plugins` prefix needed
  - `<agent-name>` — **tier 3**: name matches `plugins/*/agents/<name>.md` or `.claude/agents/<name>.md`; runs agent checks only (Checks 14a, 14b, 15, 19, 20, 17, 12, 13, 25, 22, 26, 29); one file in Step 3
  - `<skill-name>` — **tier 3**: name matches `plugins/*/skills/<name>/SKILL.md` or `.claude/skills/<name>/SKILL.md`; runs skill checks only (Checks 14a, 14b, 15, 17, 12, 23, 22, 13, 24, 25, 26, 27, 28, 29); one file in Step 3
  - Multiple scope tokens — space-separated, any combo; scope = union of resolved file sets: `agents skills`, `oss research`, `shepherd curator`, `review resolve`; check list = union (de-duplicated)

  **Scope token resolution** (each remaining token after flag-strip, resolved before Step 2): (1) reserved keywords (`agents`, `skills`, `rules`, `communication`, `setup`, `plugin`, `plugins`) → use as-is; (2) matches dir under `plugins/<token>/` → tier 2; (3) matches agent file in `plugins/*/agents/<token>.md` or `.claude/agents/<token>.md` → tier 3 agent; (4) matches skill dir `plugins/*/skills/<token>/` or `.claude/skills/<token>/` → tier 3 skill; (5) no match → error and stop

  **Valid combinations**: scope tokens + flags mix freely: `foundry --local`, `foundry --adversarial`, `agents skills --local`, `oss research --adversarial`, `foundry --efficiency`, `plugins --efficiency`, `foundry --adversarial --efficiency`, `plugins --local --adversarial --efficiency`. `--upgrade` mutually exclusive with `--adversarial` and `--efficiency` — error if combined with either. `--local` compatible with all. When `--adversarial` and `--efficiency` both present: run adversarial Phases A–C then efficiency Phases A–C sequentially; merge findings; single follow-up gate.

</inputs>

<constants>

```text
BATCH_SIZE_MIN=5       # minimum files per batch; ensures curator gets sufficient context per spawn
MAX_BATCHES=4          # total batch cap; EFFECTIVE_BATCH = max(BATCH_SIZE_MIN, ceil(total / MAX_BATCHES))
MAX_BATCHES_FAST=10    # only when --fast: trades ~120K tok/agent for wall-clock (see Fan-out cost model)
ADVERSARIAL_BATCH_SIZE=2  # adversarial phases (A, A-prime) use smaller batches for deeper per-file attention
AGENT_CALL_BUDGET=55   # target tool-calls per spawned agent; above ~60 agents stall mid-task without returning an envelope
```

<!-- Fan-out buys WALL-CLOCK, not tokens: ~120K tok fixed cost per agent spawned;
     agents past ~60 calls stall without returning an envelope. Default to the
     fewest batches keeping each agent near AGENT_CALL_BUDGET; --fast only when
     latency outweighs cost. -->

</constants>

<compaction>

- Key boundary 1: after Steps 3+4 fan-out (curator spawns + system-wide checks complete), before Step 5 aggregate.
- Key boundary 2: after Step 5 aggregate (aggregate.md + summary.jsonl written), before Step 7 report.
- Preserve at boundary 1: RUN_DIR, per-batch finding file paths, static-findings.jsonl path.
- Preserve at boundary 2: RUN_DIR, aggregate.md path, summary.jsonl path, finding counts.

</compaction>

<workflow>

**Task hygiene**: load and follow the protocol below.

```bash
# loads: compaction-contract.md
cat "$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/resolve_shared_path.py" foundry skills/_shared 2>/dev/null || echo "plugins/cc_foundry/skills/_shared")/task-hygiene.md"
```

**Orchestration contract**: orchestrator is thin coordinator — issues Glob/Grep for inventory, spawns agents, reads JSON envelopes, aggregates findings. Must NOT read agent/skill/rule file bodies directly. Inline read of non-template file = protocol violation; causes context overflow at scale.

**Task tracking**: TaskCreate for each major phase; mark status live:

- Phase 1: setup + collect (Pre-flight + Steps 1–2) → in_progress on start, completed when file list ready
- Phase 2: per-file audit (Step 3) → in_progress on agent launch, completed when all reports received
- Phase 3: system-wide checks (Step 4) → in_progress on start, completed when all checks done
- **Phases 2 and 3 launch simultaneously** — mark both in_progress same update; independent, must not serialize
- Phase 4: aggregate + fix (Steps 5–10) → in_progress, completed when fixes land; **do NOT mark completed until EITHER: (a) follow-up gate fires (Step 7) AND fixes applied or user chose skip; OR (b) `--skip-gate` active — gate suppressed, complete after Step 5 aggregation; Step 5 aggregation alone does NOT complete Phase 4 in normal mode**
- Phase 5: write final report (Step 11a) → in_progress, completed only once `$RUN_DIR/report.md` exists on disk — assembling the report in context does not complete this phase
- Phase 6: print report header (Step 11b) → `blockedBy` Phase 5; mark completed immediately BEFORE emitting the header block (task-lifecycle.md §TaskUpdate before long output), then print
- On loop retry or scope change → new task; do not reuse completed task

Surface progress at milestones: after system-wide checks ("✓ Checks 1-20 complete, N findings so far — spawning per-file audits"), after agent reports ("Agent reports received — N medium, N low findings"), before each fix batch ("Fixing N medium findings in parallel").

## Pre-flight checks

**Context budget**: full audit (12+ agents, 14+ skills, 12 system checks) runs close to context limits. File-based handoff mandatory — every sub-agent writes full output to file, returns only compact JSON envelope. Sub-agent echoing findings to context = compaction before audit completes.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/audit_preflight.py" --arguments "$ARGUMENTS"  # timeout: 20000
```

If `.claude/` missing, abort immediately. Missing `jq` is warning — audit continues with Check 4 skipped.

**State re-derivation across Bash blocks** — Claude Code spawns a fresh shell per Bash() call; variables set in pre-flight are LOST in Steps 2–11. Every Bash block in subsequent steps that uses `LOCAL_MODE` or `AUDIT_TPL` must re-read them from the persisted state files:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
AUDIT_TPL=$(cat "${TMPDIR:-/tmp}/audit-state-${CSID}/audit-tpl" 2>/dev/null || python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/resolve_skill_subdir.py" audit templates $( [ "$LOCAL_MODE" = true ] && echo "--local" ))
```

Place these three lines at the top of every Bash block in Steps 2–11 that references either variable.

**Unsupported flag check** — after extracting supported flags (`--local`, `--upgrade`, `--adversarial`, `--efficiency`, `--skip-gate`, `--keep`), scan `$ARGUMENTS` for remaining `--<token>` tokens. If found: print `` ! Unknown flag(s): `--<token>`. Supported: `--local`, `--upgrade`, `--adversarial`, `--efficiency`, `--skip-gate`, `--keep`. `` then invoke `AskUserQuestion` — (a) **Abort** (stop, re-invoke with correct flags) · (b) **Continue ignoring** (skip unknown flags, proceed). On Abort: stop.

## Step 1: Run pre-commit (if configured)

```bash
preflight_ok()   { local f=".temp/state/preflight/$1.ok"; [ -f "$f" ] && [ $(($(date +%s) - $(cat "$f"))) -lt 14400 ]; }
preflight_pass() { mkdir -p .temp/state/preflight; date +%s >".temp/state/preflight/$1.ok"; }

if (preflight_ok pre-commit || { command -v pre-commit &>/dev/null && preflight_pass pre-commit; }) &&
[ -f .pre-commit-config.yaml ]; then
    timeout 600 pre-commit run --all-files # timeout: 600000
fi
```

Files auto-corrected by pre-commit hooks are clean before structural audit. Note modified files — include in audit scope even if not originally targeted.

If pre-commit not configured, skip silently.

## Step 1b: Layer-1 deterministic static pass

Run the zero-LLM checker driver — the same deterministic checkers pre-commit enforces, aggregated into one reproducible findings file. These results are **authoritative** for their check classes: Steps 3–4 (LLM curator + judgment checks) must NOT re-derive them in prose — treat them as already-verified and spend model tokens only on judgment.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
STATIC_SCOPE=$( [ "$LOCAL_MODE" = true ] && echo plugins || echo .claude )
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/audit_static.py" --scan-dir "$STATIC_SCOPE" \
    --jsonl "${TMPDIR:-/tmp}/audit-state-${CSID}/static-findings.jsonl"  # timeout: 120000
```

Covered deterministically by the driver (map to legacy check IDs — do NOT re-run these as prose): **14a** tag symmetry · **14b** fence symmetry · **14c** README drift · **14d** mode-dispatch integrity · **14e** cross-plugin shared-file drift · **43** bash-variable persistence · **42** spawn-prompt `$VAR` (checks-skills.md) · **32d** orphaned bin/ scripts · **cli-flag-drift** SKILL.md flags vs argparse (`check_cli_flag_drift.py`, checks-index 42) · **R3** bin/computed-path reference integrity. The whole-repo checks (orphaned-bin, routing-links, shared-drift) always scope to `plugins/` regardless of `STATIC_SCOPE`; the driver is most complete in `--local` mode. Step 5 merges `static-findings.jsonl` into the aggregate.

> **Layer-1 recall is benchmarked** — `tests/test_audit_static.py` plants a known defect per scope-aware class and asserts the driver catches every one (100% mechanical recall), so this pass is trusted, not assumed.

## Step 1c: Layer-3 recurrence signal (attention weighting)

Read a compact git-churn signal so audit attention follows *measured* churn — the files and change-classes that keep being re-fixed are where the next defect most likely hides. Report the dominant recurring-fix class, not only point findings.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/audit_churn.py" --limit 300 --path plugins \
    > "${TMPDIR:-/tmp}/audit-state-${CSID}/churn-signal.json" 2>/dev/null  # timeout: 20000
```

Use `churn-signal.json` (`commit_types`, `top_churn`, `recurring_hint`) to: (1) prioritize per-file audits toward the most-churned files first in Step 3 batching; (2) add a **Recurring fix class** line to the Step 7 report naming the dominant theme (e.g. "version-bump churn — candidate for automation"). Zero-LLM, best-effort; skip silently if git is unavailable.

## Step 2: Collect all config files

Enumerate everything in scope with built-in tools. Run all Glob calls in parallel.

**Plugin layout resolution** (`--local`/`plugins` scope — multi-plugin source tree, mandatory before the fixed-pattern Globs below): don't assume every plugin uses `skills/`+`agents/` — a plugin can override the convention in its own manifest (e.g. `plugins/codemap-py/.claude-plugin/plugin.json` declares `"skills": "./claude-skills/"`, not `./skills/`; a fixed `*/skills/*/SKILL.md` glob silently never matches it). Resolve each plugin's real dir names first:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
if [ "$LOCAL_MODE" = "true" ]; then
  for pj in plugins/*/.claude-plugin/plugin.json; do
    [ -f "$pj" ] || continue
    plugin_name=$(basename "$(dirname "$(dirname "$pj")")")
    python3 -c "
import json
d = json.load(open('$pj'))
skills = (d.get('skills') or './skills/').strip('./')
agents = (d.get('agents') or './agents/').strip('./')
print(f'PLUGIN_LAYOUT: $plugin_name | skills={skills} | agents={agents}')
" 2>/dev/null || echo "PLUGIN_LAYOUT: $plugin_name | skills=skills | agents=agents"
  done
fi
```

For every `PLUGIN_LAYOUT:` line, use its `skills=`/`agents=` value (not the hardcoded `skills`/`agents` names) when Globbing that plugin below — e.g. `codemap-py` prints `skills=claude-skills`, so Glob `claude-skills/*/SKILL.md` under `plugins/codemap-py/`, not `skills/*/SKILL.md`. Plugins with no manifest override resolve to the same `skills`/`agents` names the fixed patterns below already assume — no behavior change for them. **`codex-skills/`-style dirs for non-Claude-Code runtimes are intentionally excluded** — only the manifest-declared path is authoritative, since a heuristic `*skill*` name match would also sweep in skill dirs meant for a different agent runtime and produce false-positive findings against Claude Code's frontmatter schema.

**Source selection by `LOCAL_MODE`**:

- **`LOCAL_MODE=false` (default — user setup)**: `.claude/` primary; `plugins/` skipped. Installed/active config only.
- **`LOCAL_MODE=true` (--local — project source)**: `plugins/` primary; `.claude/` secondary for rules/hooks/settings only.

**Without `--local` (`LOCAL_MODE=false`)**:

- **Agents**: Glob tool, pattern `agents/*.md`, path `.claude/`
- **Skills**: Glob tool, pattern `skills/*/SKILL.md`, path `.claude/`
- **Rules**: Glob tool, pattern `rules/*.md`, path `.claude/`
- **Communication**: Read tool on `rules/communication.md`, `rules/quality-gates.md`, `TEAM_PROTOCOL.md`, `skills/_shared/file-handoff-protocol.md`
- **Settings**: Read tool on `.claude/settings.json`
- **Hooks**: Glob tool, pattern `hooks/*`, path `.claude/`

**With `--local` (`LOCAL_MODE=true`)**:

- **Agents (source — primary)**: for each `PLUGIN_LAYOUT:` line above, Glob tool pattern `<agents-dir>/*.md`, path `plugins/<plugin_name>/`
- **Skills (source — primary)**: for each `PLUGIN_LAYOUT:` line above, Glob tool pattern `<skills-dir>/*/SKILL.md`, path `plugins/<plugin_name>/`
- **Agents (project-local — secondary)**: Glob tool, pattern `agents/*.md`, path `.claude/`
- **Skills (project-local — secondary)**: Glob tool, pattern `skills/*/SKILL.md`, path `.claude/`
- **Rules / Settings / Hooks**: same as without `--local` (`.claude/`)

Merge into single flat inventory. When `LOCAL_MODE=true` and same logical name in both `plugins/` and `.claude/`, prefer plugin source — skip `.claude/` duplicate. Record full paths — Step 3 cross-reference checks depend on current inventory. If MEMORY.md not updated since last agent/skill added/removed, run live disk scan, not cached roster. Stale inventory = primary cause of false-negative cross-reference findings.

**Coverage reconciliation** (`--local`/`plugins` scope only — mandatory, not optional): a plugin contributing zero files to the inventory is invisible to every downstream check and must never pass as a silent clean sweep — same failure shape as a check that always no-ops: absence of findings misread as absence of problems. The base set for this comparison is **every directory under `plugins/`**, not just the ones that produced a `PLUGIN_LAYOUT:` line above — a plugin with a missing or malformed `plugin.json` contributes no `PLUGIN_LAYOUT:` line either, and comparing against that subset would make it invisible all over again:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
mkdir -p "${TMPDIR:-/tmp}/audit-state-${CSID}"
find plugins -mindepth 1 -maxdepth 1 -type d ! -name ".*" 2>/dev/null | sed 's|plugins/||' | sort > "${TMPDIR:-/tmp}/audit-state-${CSID}/all-plugins"
```

After inventory is built (model-context comparison — inventory already in context, not re-read from disk): compare every name in `all-plugins` against plugin names actually present in the inventory. For each plugin with zero inventory entries, print and persist in the **same** bash call (re-export `CSID` here too — a later call starts a fresh shell and an unexported `$CSID` silently writes to the wrong sentinel path):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
: > "${TMPDIR:-/tmp}/audit-state-${CSID}/unscanned-plugins"
# for each zero-contribution plugin found above:
printf "⚠ UNSCANNED: %s — no files matched scope globs\n" "<plugin>" | tee -a "${TMPDIR:-/tmp}/audit-state-${CSID}/unscanned-plugins"
```

Step 11 reads this sentinel and surfaces it as a top-level report section — a zero-coverage plugin must never produce a green summary.

**Scope filtering for Step 2** (applies on top of `LOCAL_MODE`):

- `agents` scope — collect agents from active source (`.claude/agents/` or `plugins/*/agents/` per `LOCAL_MODE`); skip skills, rules, hooks
- `skills` scope — collect skills from active source; skip agents, rules, hooks
- `plugins` scope — always reads `plugins/*/agents/*.md` + `plugins/*/skills/*/SKILL.md` regardless of `LOCAL_MODE`; forces `LOCAL_MODE=true`
- `plugins <name>` or `<plugin-name>` (tier 2) scope — collect `plugins/<name>/agents/*.md` + `plugins/<name>/skills/*/SKILL.md` only; forces `LOCAL_MODE=true`; also force `LOCAL_MODE=true` when any scope token matches `plugins` keyword or matches a `plugins/<name>/` directory even without explicit `--local` flag
- `<agent-name>` (tier 3) scope — single matching agent file; `LOCAL_MODE=false`: `.claude/agents/<name>.md`; `LOCAL_MODE=true`: `plugins/*/agents/<name>.md` first, then `.claude/agents/<name>.md`
- `<skill-name>` (tier 3) scope — single matching skill file; same `LOCAL_MODE` resolution as agent above
- Multiple scope tokens — union of all resolved file sets
- `setup`/`plugin` (bare) scope — no agent/skill collection from plugins; see setup/plugin notes below
- Full sweep (no scope) — collect per `LOCAL_MODE` source selection above

**Setup scope**: when `$SCOPE` is `setup`, also collect `${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/skills/setup/SKILL.md` for Step 3 foundry:curator spawn — only per-file spawn in setup scope. Checks I1–I3 (from `checks-install.md`) run in Step 4 against `~/.claude/` to validate post-install user state.

**`plugins <name>` scope**: verify `plugins/<name>/` exists — abort `! BREAKING: plugins/<name>/ not found` if absent. Collect `plugins/<name>/skills/setup/SKILL.md` for Step 3 plus all agents/skills in that plugin. **`plugins` (no name)**: iterate every subdir under `plugins/` — do not filter by literal `agents/`/`skills/` dir presence; PLUGIN_LAYOUT resolution above already covers plugins declaring a non-standard skills/agents path via their own manifest.

## Step 3: Per-file audit via foundry:curator

**Context management** — 12+ agents and 14+ skills: accumulating full foundry:curator responses in context causes overflow before aggregation. Use file-based findings to keep main context lean.

**Hard rule — no pre-reading**: Never call Read on agent/skill file before spawning foundry:curator. Spawned agent does the reading. Orchestrator reads only returned JSON envelope. Pre-reading 41 KB files into main context = defeats delegation + causes context overflow at scale.

**Batching rule**: Always apply the grouping algorithm. Compute `EFFECTIVE_BATCH = max(BATCH_SIZE_MIN, ceil(total_files / MAX_BATCHES))` before grouping — caps total batches at `MAX_BATCHES` while guaranteeing `BATCH_SIZE_MIN` files per batch for adequate curator context. Group files into batches of up to `EFFECTIVE_BATCH`. Never spawn one agent per file. Total files ≤ `EFFECTIVE_BATCH` → one batch containing all files. Use `MAX_BATCHES_FAST` in place of `MAX_BATCHES` **only** when `--fast` was passed.

**Spawn-count gate — apply before spawning anything**: every agent costs ~120,851 tok just to exist, i.e. ~73 tool-calls' worth of work (see `<constants>`). So **spawn the fewest agents that keep each one near `AGENT_CALL_BUDGET`**, not the most the cap allows. Two consequences, both mandatory:

- **Gate on Layer-1 signal.** Step 1b already ran ~10 deterministic checkers at zero LLM cost, and its results are authoritative for their classes. A file with no Layer-1 finding and no judgment-bearing content (no `Agent()` dispatch, no cross-file contract, no model/tool declaration) does **not** need a curator. Audit it by the static pass alone and say so in the report. Spawning a curator to re-confirm a clean mechanical result is the single most common waste in this skill.
- **State the trade in the report.** Record agents spawned and the reason for that count. If `--fast` widened the fan-out, say that it bought wall-clock at roughly 120K tok per extra agent — never present the fast path as free.

**Budget line — include verbatim in every spawn prompt**: `If you approach your budget, stop cleanly and return the envelope with "partial": true and an accurate account of what you finished.` Measured: agents at ≤57 calls returned a clean envelope 5/5; agents at 87–142 calls stalled mid-task 3/5 and returned a progress fragment instead, forcing the orchestrator to reconstruct their state from disk.

**Grouping algorithm**: (1) sort by plugin origin (`plugins/<name>/` prefix); (2) assign each plugin's files to batches, fill to `EFFECTIVE_BATCH` before next — keeps same-plugin files together; (3) remaining files (`.claude/` and mixed) fill open slots. Grouping plugin-first, not strictly ordered — unconnected files assigned randomly to reach `EFFECTIVE_BATCH`.

**Layer-2 — judgment by domain, not by file (plugin scope)**: when the scope is `plugins`, `plugins <name>`, or a tier-2 plugin name, override the batch cap and group **all of a plugin's files into ONE holistic batch** (one `foundry:curator` per plugin), even if that exceeds `EFFECTIVE_BATCH`. Whole-plugin context is what lets the curator catch cross-file breaks that per-file batching structurally misses — tool-grant mismatches (agent frontmatter vs skill dispatch), inter-skill contract splits (a constant clamped differently in two files), dead dispatch paths, and version/description drift within the plugin. The curator prompt for a holistic batch must say: "You have this plugin's ENTIRE file set — review it as one system: check that every `Agent(subagent_type=...)` dispatch targets an agent whose frontmatter grants the needed tools, that shared constants/contracts agree across files, and that no skill references a removed mode/file." The mechanical checks are already done in Step 1b — spend this holistic pass on cross-file judgment only. (Very large plugins may still split, but keep agents+their dispatching skills in the same batch.)

**Scope-restricted runs**: fewer than `EFFECTIVE_BATCH` files → one batch containing ALL files in scope (single foundry:curator spawn). Read only relevant template file(s) for active scope, not all 4.

Set up the run directory once before spawning any agents:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
AUDIT_TPL=$(cat "${TMPDIR:-/tmp}/audit-state-${CSID}/audit-tpl" 2>/dev/null || python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/resolve_skill_subdir.py" audit templates $( [ "$LOCAL_MODE" = true ] && echo "--local" ))

RUN_DIR=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/make_run_dir.py" .reports/audit)  # timeout: 5000
[ -z "$RUN_DIR" ] && { printf "! BREAKING: make_run_dir.py returned empty path — check Python availability and write permission on .reports/\n"; exit 1; }
echo "Run dir: $RUN_DIR"
echo "$RUN_DIR" > "${TMPDIR:-/tmp}/audit-state-${CSID}/run-dir"
cat "$AUDIT_TPL/curator-prompt.md"
```

Spawn **foundry:curator** agents in batches of up to `EFFECTIVE_BATCH` (grouping algorithm above) — or one batch if scope ≤ `EFFECTIVE_BATCH`. Each spawn prompt must:

1. Include the curator-prompt.md content loaded above
2. Include the disk inventory from Step 2 (agent/skill list for cross-reference validation)
3. End with:

> "Write your FULL findings (all severity levels) to `<RUN_DIR>/<file-slug>.md` using the Write tool — where `<file-slug>` is a unique identifier combining plugin prefix and filename (e.g. `foundry-shepherd.md`, `oss-analyse-SKILL.md`, `develop-fix-SKILL.md`) to avoid collisions between cross-plugin files sharing the same basename. End your full findings file with a `## Confidence` block per quality-gates.md format (Score, Gaps, Refinements). Then return to the caller ONLY a compact JSON envelope on your final line — nothing else after it: `{\"status\":\"done\",\"file\":\"<RUN_DIR>/<file-slug>.md\",\"findings\":N,\"severity\":{\"security\":N,\"critical\":N,\"high\":N,\"medium\":N,\"low\":N},\"confidence\":0.N,\"summary\":\"<filename>: N critical, N high, N medium, N low\"}`"

Replace `<RUN_DIR>` with actual path, `<file-slug>` with plugin-prefixed unique slug (e.g. `foundry-shepherd`, `oss-analyse-SKILL`, `develop-fix-SKILL`). Slug chars: `[a-zA-Z0-9-]` only — no slashes, spaces, or dots.

**Critical context discipline**: response body = JSON envelope on final line only. No other text, output summaries, or findings. All content to file.

> Template file = canonical per-file audit criteria. Disk inventory and RUN_DIR path = runtime values injected per spawn.

After spawns complete: short summaries in context; use to identify files with findings. Full content in run directory files.

**Health monitoring** (CLAUDE.md §6): apply the honest protocol in `$_FS/agent-spawn-protocol.md` — these curator batches return on completion; after each returns, read its `$RUN_DIR` output file. On empty/missing output: mark `timed_out`, surface with ⏱ in final report. Never omit timed-out agents.

## Steps 4–5b: System-wide checks, aggregate, low-confidence remediation

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
AUDIT_MODES=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/resolve_skill_subdir.py" audit modes $( [ "$LOCAL_MODE" = true ] && echo "--local" )) || { printf "! BREAKING: audit/modes not found — run /foundry:setup first\n"; exit 1; }  # timeout: 5000
cat "$AUDIT_MODES/steps-4-5-7.md"
```

> loads: modes/steps-4-5-7.md (§Step 4–5b and §Step 7)

Execute §Step 4–5b loaded above — system-wide checks (scope-dispatched), aggregate + classify findings, low-confidence remediation (conditional, skipped when no file scored \<0.80). State on disk in `summary.jsonl`, `$RUN_DIR`, `$AUDIT_TPL`. Returns here to Step 6.

## Step 6: Cross-validate critical findings

```bash
_SHARED=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/resolve_shared_path.py" foundry skills/_shared 2>/dev/null || echo "plugins/cc_foundry/skills/_shared")  # timeout: 5000
SKIP_CROSS_VAL=false
[ -f "$_SHARED/cross-validation-protocol.md" ] || { printf "⚠ WARNING: cross-validation-protocol.md not found at $_SHARED — skipping cross-validation\n"; SKIP_CROSS_VAL=true; }
[ "$SKIP_CROSS_VAL" = false ] && cat "$_SHARED/cross-validation-protocol.md"
```

If `$SKIP_CROSS_VAL` = false: follow the cross-validation protocol loaded above.

**Skill-specific**: the verifier agent is always **foundry:curator**.

## Step 7: Report findings

Execute §Step 7 of `steps-4-5-7.md` (loaded above in Steps 4–5b) — emits report, fires follow-up gate; on fix pick continues to Steps 8–10 (`modes/fix.md`); otherwise skip to Step 11.

## Steps 8–10: Fix dispatch, codex cross-file check, re-audit (mode: fix)

Runs **only** when the user picked a fix option (a–c) from the Step 7 follow-up gate. Resolve the modes dir, load `fix.md`, then execute it inline — it carries Step 8 (delegate fixes to subagents), Step 9 (codex cross-file check), and Step 10 (re-audit + convergence loop), then returns here to Step 11:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/load_mode.py" audit modes fix.md $( [ "$LOCAL_MODE" = true ] && echo "--local" )  # timeout: 5000
```

> loads: modes/fix.md Execute Steps 8–10 loaded above inline (state on disk in `summary.jsonl`, `$RUN_DIR`, `$AUDIT_TPL`). On convergence (clean or 5-pass limit), continue to Step 11 below. If no fix option was picked, skip directly to Step 11.

## Step 11: Final report

<!-- loads: report-template.md -->

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
AUDIT_TPL=$(cat "${TMPDIR:-/tmp}/audit-state-${CSID}/audit-tpl" 2>/dev/null || python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/resolve_skill_subdir.py" audit templates $( [ "$LOCAL_MODE" = true ] && echo "--local" ))
IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/audit-state-${CSID}/run-dir" 2>/dev/null || RUN_DIR=""
cat "$AUDIT_TPL/report-template.md"
```

**Step 11a — assemble and persist** — assemble the complete audit report following the template and instructions loaded above, then **Write it to `$RUN_DIR/report.md`** using the Write tool. Set the template's `Path:` field to that same real path. This Write is mandatory, not optional: the terminal step below reads the header back from this file, and the report is the run's only durable artifact once the session ends. Skipping it leaves `Path:` pointing at a file that was never created.

**Unscanned-plugin surfacing** (`--local`/`plugins` scope only): before assembling, read `${TMPDIR:-/tmp}/audit-state-${CSID}/unscanned-plugins` (sentinel from Step 2 coverage reconciliation). Non-empty → include an `### Unscanned Plugins` section in the report immediately after `### Files Audited`, listing every `⚠ UNSCANNED: <plugin>` line verbatim; this makes zero-coverage plugins visible in the durable artifact, not just the transient terminal output. Empty or missing → omit the section.

**Step 11b — terminal output** — per quality-gates.md universal rule: read the `---` header block from the top of `$RUN_DIR/report.md` (all fields from opening `---` up to and including closing `---`) and render as a two-column Markdown table (`Field | Value`, one row per key, file order) as the FIRST content of the reply — never print the raw `---`-delimited block. Then print `→ $RUN_DIR/report.md`. Then executive summary. Omit the `╔═╗` Re:Anchor box (communication.md exempts quality-gates `---` report headers — the table IS the reply header).

**Completion marker** — on successful completion, write `$RUN_DIR/result.jsonl` with one JSONL line summarising the run (severity totals, scope, pass count). On any abort/error path before completion, leave `result.jsonl` absent — the TTL cleanup hook (artifact-lifecycle.md) intentionally skips run directories without `result.jsonl`, preserving incomplete runs for post-mortem debugging. To force cleanup of a known-bad incomplete run, write `{"status":"incomplete","reason":"<one-line>"}` to `result.jsonl` so TTL can age it out.

```bash
rm -f .temp/state/skill-contract.md  # clear contract — skill complete (compaction-contract.md §Lifecycle)  # timeout: 5000
```

## Mode: upgrade

**Trigger**: `/audit --upgrade`

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
AUDIT_TPL=$(cat "${TMPDIR:-/tmp}/audit-state-${CSID}/audit-tpl" 2>/dev/null || python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/resolve_skill_subdir.py" audit templates $( [ "$LOCAL_MODE" = true ] && echo "--local" ))
cat "$AUDIT_TPL/../modes/upgrade.md"
```

Execute the mode loaded above.

## Mode: adversarial (alias: --challenge)

**Trigger**: `/audit [<scope>...] --adversarial`

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
AUDIT_TPL=$(cat "${TMPDIR:-/tmp}/audit-state-${CSID}/audit-tpl" 2>/dev/null || python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/resolve_skill_subdir.py" audit templates $( [ "$LOCAL_MODE" = true ] && echo "--local" ))
cat "$AUDIT_TPL/../modes/adversarial.md"
```

Execute the mode loaded above.

## Mode: efficiency

**Trigger**: `/audit [<scope>...] --efficiency`

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
AUDIT_TPL=$(cat "${TMPDIR:-/tmp}/audit-state-${CSID}/audit-tpl" 2>/dev/null || python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/resolve_skill_subdir.py" audit templates $( [ "$LOCAL_MODE" = true ] && echo "--local" ))
cat "$AUDIT_TPL/../modes/efficiency.md"
```

Execute the mode loaded above.

## Combined-run output isolation

When `--adversarial` and `--efficiency` both run in the same invocation: each mode writes its finding files to its own subdirectory to prevent overwriting — `$RUN_DIR/adversarial/` for adversarial mode output, `$RUN_DIR/efficiency/` for efficiency mode output. Step 5 consolidator reads both subdirs. Follow-up gate fires once after both complete with merged finding counts.

## Follow-up gate

**Always fires** unless `--skip-gate` passed (programmatic callers). Call `AskUserQuestion` — do NOT write options as plain text first. Map options directly into tool call arguments.

When user picks fix option (a–c): run Steps 8–10 inline via `modes/fix.md` (state on disk in `summary.jsonl`); no recursive `/audit` call.

**HARD RULE — Fixed option labels**: Use exactly the labels below verbatim. Do NOT rewrite, paraphrase, or substitute finding-specific alternatives. Finding context must NOT influence option labels. This rule has been violated repeatedly in past runs; enforce strictly.

**Mandatory options (always present, never omit)**:

- (a) label: `Fix auto-fixable (Recommended)` — auto-fix critical, high, medium, and low findings (skip NON_AUTO_FIXABLE systemic issues)
- (c) label: `Fix ALL` — collect all NON_AUTO_FIXABLE decisions upfront (max 4 `AskUserQuestion` calls), then run one integrated fix pass covering auto-fixable + resolved systemic items + lows; most thorough option

**Conditional options (include when condition met)**:

- (b) label: `Fix SECURITY + CRITICAL + HIGH` — include only when security, critical, or high findings present; omit otherwise to stay within 4-option cap
- (d) label: `Skip` — always include; when `--efficiency` ran and `extract_count > 0` (HIGH or MEDIUM), replace with `` `Run /distill executables (N HIGH, M MEDIUM candidates)` `` instead

**Option slot budget** (`AskUserQuestion` hard cap = 4 options; "Other" always auto-appended as 5th free-text slot):

- Typical: (a) + (b) + (c) + (d) = 4 ✓

- No sec/crit/high findings: (a) + (c) + (d) = 3 ✓ — slot freed; do NOT fill with custom finding-specific option

- Efficiency override active: (a) + (b) + (c) + distill = 4 ✓ — (d) Skip replaced by distill label

- question: "What next?" (include counts, e.g. "2 critical, 4 high, 3 medium, 1 low. What next?")

After completing `--upgrade`, `--adversarial`, or `--efficiency`: also fire this gate. For (d) Skip: remove the mode just run from the "for other modes" list. When `--adversarial --efficiency` combined: fire gate once after both complete with merged finding counts; remove both from (d) hint. Efficiency distill override: selecting distill option invokes `/distill executables` → `foundry:sw-engineer` extraction then `/audit --efficiency` re-run to confirm savings.

</workflow>

<notes>

- **`!` Breaking findings**: when skill or agent completely non-functional (check 7, broken cross-refs, invalid hook events), prefix finding with `!` and state impact + fix in one place — don't bury in table row. Surfaces as **`! BREAKING`** in bash output and as prominent callout in final report. **`! BREAKING` findings require user acknowledgment before audit proceeds past that check**: call `AskUserQuestion` — state what is broken and impact; user must explicitly confirm awareness before continuing. One question per distinct breaking finding; group only when logically one atomic issue. Batch up to 4 of those questions into a single `AskUserQuestion` call (communication.md per-call cap) — N breaking findings become `ceil(N/4)` calls, never N. Batching changes only how many calls carry the questions; every finding still gets its own question and its own acknowledgment. Prose acknowledgment in response body does NOT count — `AskUserQuestion` mandatory.

- **settings.json is hands-off**: missing permissions always reported, never auto-edited — structural JSON edits risk breaking Claude Code config loading

- **Dead loops need human judgment**: cycle in follow-up chains may be intentional (e.g., refactor → review → fix → refactor) — flag and explain, don't auto-remove

- **Convergence loop replaces cycle cap**: fix loop runs until zero fixable findings or 5-pass hard limit — see `modes/fix.md` Step 10 for full protocol

- **Relationship to curator**: `foundry:curator` = single-file reactive audit; `/audit` = system-wide sweep running foundry:curator at scale + cross-file checks

- **Paths must be portable**: `.claude/` for project-relative, `~/` or `$HOME/` for home — never literal `/Users/<name>/` or `/home/<name>/` (anti-examples only); applies to ALL config files including `settings.json`

- **Bash error logging**: if bash block in Pre-flight or Step 4 fails unexpectedly, append JSONL line to `.notes/logs/audit-errors.jsonl` (`{"ts":"<ISO>","check":"<N>","error":"<message>"}`) for post-mortem — never swallow errors silently.

- **Parallel execution rule**: after Step 2, launch Steps 3 and 4 in same response — all foundry:curator spawns AND system-wide bash checks issued together. Do NOT run Step 3 first then Step 4. Aggregation (Step 5) waits for both. Docs-freshness web-explorer (within Step 4) launches in same parallel batch.

- **Token cost**: Step 3 (foundry:curator spawns) most expensive. For quick structural scan needing only cross-reference + inventory validation, Step 4 system-wide checks often sufficient. Run `/audit agents` or `/audit skills` to scope, or skip Step 3 for fast pass when per-file quality trusted.

- **Routing calibration complement**: to test whether skill trigger descriptions fire correctly (trigger accuracy, A/B testing), use `/foundry:calibrate routing`. `/audit` checks structural quality; `/foundry:calibrate routing` validates right skill selected by Claude Code dispatcher.

- Follow-up chains:

  - Audit clean → pick `/foundry:setup` from gate to propagate verified config to `~/.claude/`
  - Audit found structural issues → review flagged files manually before syncing; pick fix level from gate
  - Audit found many low items → pick "Fix all" from gate, or run `/develop:refactor` (requires `develop` plugin) for targeted cleanup
  - After fixing agent instructions (from audit gate) → `/foundry:calibrate <agent>` to verify fix improved recall and confidence calibration
  - Audit Check 20 found description overlap → `/foundry:calibrate routing` to verify behavioral routing impact; update descriptions for confused pairs based on routing report
  - Audit surfaced upgrade proposals → run `/audit --upgrade` separately to apply (not a gate option — use Other slot or run the command directly after gate)
  - `/audit --upgrade` reverted capability change → run `/foundry:calibrate <agent> --full` for deeper signal (N=10 vs N=3 used in upgrade mode)
  - Audit Check 22 found unregistered calibratable mode → update `calibrate/modes/skills.md` domain table and run `/foundry:calibrate skills` to verify new target works
  - Audit Check 22 found stale domain table entry → remove from `calibrate/modes/skills.md`
  - `/audit --efficiency` found model over-provisioning → apply P1+P2 changes from efficiency report → re-run `/audit --efficiency` to confirm savings estimate; run `/foundry:calibrate routing --fast` to verify no routing regression from model changes
  - `/audit --efficiency` found bin/ extraction candidates (HIGH or MEDIUM verdict) → select `Run /distill executables` from follow-up gate to perform extraction; then re-run `/audit --efficiency` to confirm `extract_count == 0`

</notes>
