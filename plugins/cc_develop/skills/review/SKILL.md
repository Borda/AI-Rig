---
name: review
description: 'Multi-agent code review of local Python files, directories, or the current git diff covering architecture, tests, performance, docs, lint, security, and API design. Scope: Python source files in local working tree. Python-file-free targets (pure JS/TS/Go/Rust projects) are out of scope. TRIGGER when: user asks to review local Python files, a directory, or the current git diff/working-tree changes, with no GitHub PR number involved; phrases: "review this", "review my changes", "code review this diff", "review src/foo.py". SKIP when: input is a bare GitHub PR/issue number (use `/oss:review <PR#>`, requires oss plugin); user wants the Codex-native tiered `$code-review` workflow (codex-rig plugin, JSON artifact + specialist fan-out) — different toolchain; implementation work (use `/develop:fix` or `/develop:feature`); non-Python-only projects.'
argument-hint: '[python-file|dir] [--no-challenge] [--challenge] [--codemap] [--no-codemap] [--semble] [--worktree] [--full] [--keep "<items>"]'
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, Agent, Skill, TaskList, TaskCreate, TaskUpdate, AskUserQuestion, EnterWorktree, ExitWorktree
disable-model-invocation: true
effort: high
---

<objective>

Comprehensive code review of local files or working-tree diff. Spawns specialized sub-agents in parallel, consolidates findings into structured feedback with severity levels.

NOT for: GitHub PR review (use `/oss:review <PR#>` (requires oss plugin)); GitHub thread analysis or PR reply drafting (use `/oss:analyse <PR#>` (requires oss plugin)); implementation (use `/develop:feature` or `/develop:fix`); `.claude/` config changes (use `/foundry:manage` (requires foundry plugin) or `/foundry:audit` (requires foundry plugin)); non-Python-only projects (zero Python source files — pure JS/TS/Go/Rust) — review toolchain assumes Python/pytest; Python test-only targets where diff contains only test files (no `src/` or top-level `.py` source outside `tests/`) — review will be uninformative; for polyglot projects with Python source, reviews Python files only.

</objective>

<inputs>

- **$ARGUMENTS**: optional file path or directory to review.
  - Path given: review those files
  - Omitted: review current git diff (`git diff HEAD` — staged + unstaged vs HEAD)
  - **Scope**: Python source only. Non-Python file (YAML, JSON, shell script, etc.) → state out of scope, suggest appropriate tool. No findings.
  - `--no-challenge`: skip adversarial review (challenger runs by default)
  - `--challenge`: force challenger (Agent 7) even on small diff that small-diff auto-skip would otherwise skip
  - `--full`: run **every** dimension the classification preselected, instead of only the `FANOUT_MAX` most relevant of them. Never widens the preselection — a dimension the FIX/CHORE/small-diff rules ruled out stays out. **Not free**: each extra agent costs ~120,851 tok of fixed overhead however little work it does. Default stays capped; pass this when depth matters more than cost.
  - `--codemap`: strict mode — stop and report if codemap not installed (on by default when installed; use `--no-codemap` to opt out)
  - `--semble`: enable semble semantic search companion (off by default)

**PR#/filename disambiguation gate** (execute BEFORE Step 1): tighten classification — valid PR# is positive integer with no extension and no existing file at that path. Filenames that look like numbers (e.g. `42.py`) must NOT trigger PR mode.

```bash
TOKEN="$ARGUMENTS"
if [[ "$TOKEN" =~ ^[0-9]+$ ]] && [ ! -e "$TOKEN" ]; then
    echo "PR number detected — checking oss plugin availability"
    [ -f "$(ls -td ~/.claude/plugins/cache/borda-ai-rig/oss/*/skills/review/SKILL.md 2>/dev/null | head -1)" ] && OSS_AVAILABLE=true || OSS_AVAILABLE=false  # timeout: 5000
elif [ -f "$TOKEN" ]; then
    OSS_AVAILABLE=skip
elif [[ "$TOKEN" =~ ^#[0-9]+$ ]] && [ ! -e "$TOKEN" ]; then
    echo "PR number detected — checking oss plugin availability"
    [ -f "$(ls -td ~/.claude/plugins/cache/borda-ai-rig/oss/*/skills/review/SKILL.md 2>/dev/null | head -1)" ] && OSS_AVAILABLE=true || OSS_AVAILABLE=false  # timeout: 5000
else
    OSS_AVAILABLE=skip
fi
```

If `$OSS_AVAILABLE` is `skip`: proceed to Step 1 normally (path / diff / dir mode).

If `$OSS_AVAILABLE` is `true`: call `AskUserQuestion` ONCE with BOTH questions in the same call (a second sequential window costs another human-idle round trip) — Q1: "Looks like you passed a PR/issue number. Did you mean to run `/oss:review $ARGUMENTS` (requires oss plugin) to review that PR?" Options: (a) "Yes — launch `/oss:review $ARGUMENTS`" · (b) "No — review local code". Q2: "If reviewing local code: provide the file path or directory (free text; skip if Q1 = Yes)". On (a): call `Skill(skill="oss:review", args="$ARGUMENTS")`, ignore Q2. On (b): use the Q2 response as `$REVIEW_ARGS` and proceed to Step 1 (Q2 empty → ask once more for the path).

If `$OSS_AVAILABLE` is `false`: call `AskUserQuestion` ONCE with BOTH questions — Q1: "Looks like you passed a PR/issue number, but oss plugin not installed — `/oss:review` unavailable. Review local code instead?" Options: (a) "Yes — review local code" · (b) "I need oss plugin". Q2: "If reviewing local code: provide the file path or directory (free text; skip if Q1 = b)". On (a): use the Q2 response as `$REVIEW_ARGS` and proceed to Step 1 (Q2 empty → ask once more). On (b): inform user: install with `claude plugin install oss@borda-ai-rig`.

</inputs>

<constants>

```text
FANOUT_MAX=3            # default: top-N spawn UNITS among classification-preselected dimensions
                        # cap counts dimension spawn units only; real default lineup = up to 3
                        # dimension units + challenger + bridge co-review + consolidator
                        # (+ up to 3 cross-validation verifiers when criticals exist)
                        # --full runs ALL preselected units instead — no numeric cap
AGENT_CALL_BUDGET=55    # target tool-calls per agent; past ~60 they stall without returning an envelope
CHALLENGE_ENABLED=true  # set to false via --no-challenge
CHALLENGE_FORCED=false  # set to true via --challenge — forces Agent 7 even on small diffs (overrides small-diff auto-skip)
CODEMAP_ENABLED=auto    # on by default if codemap installed + index found; --no-codemap = off; --codemap = strict (stop if not installed)
SEMBLE_ENABLED=false    # set to true via --semble
```

</constants>

<compaction>

- Key boundary: end of Step 3 — parallel review-agent fan-out outputs collected, before Step 5 consolidation.
- Second boundary: end of Step 5 — consolidated report written, before Step 6 follow-up.
- Preserve at boundary 1: RUN_DIR, REPORT_DIR, target, per-agent finding file paths, --keep items.
- Preserve at boundary 2: final report path.

</compaction>

<workflow>

<!-- Shared pattern with oss:review — coordinate on agent spawn logic, file-handoff, consolidation changes -->

<!-- Agent resolution: see _DEV_SHARED/agent-resolution.md (mounted by develop plugin init) -->

## Agent Resolution

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_DEV_SHARED=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_shared_resolve.py" 2>/dev/null)  # timeout: 5000
[ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"
echo "$_DEV_SHARED" > "${TMPDIR:-/tmp}/dev-shared-${CSID}"  # cold resolve — every later block warm-reads this
# loads: compaction-contract.md
cat "$_DEV_SHARED/agent-resolution.md"
```

Contains: foundry check + fallback table. If foundry not installed: substitute each `foundry:X` with `general-purpose` per table. Agents this skill uses: `foundry:sw-engineer`, `foundry:qa-specialist`, `foundry:perf-optimizer`, `foundry:doc-scribe`, `foundry:linting-expert`, `foundry:solution-architect`, `foundry:challenger`.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEV_SHARED < "${TMPDIR:-/tmp}/dev-shared-${CSID}" 2>/dev/null || _DEV_SHARED=""  # timeout: 5000
[ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"
cat "$_DEV_SHARED/task-hygiene.md"
```

After Step 1 completes (scope and `TARGET` known), create these tasks **before any agent spawns** (in order, all at once):

- **"Step 1: Identify scope"** — mark `in_progress` at Step 1 start; mark `completed` when `TARGET` and `SCOPE` set and Python file check passes
- **"Step 2: Codex co-review"** — create before Step 2 (skip task if Codex unavailable); mark `in_progress` before Codex spawn; mark `completed` when codex seed extracted (or timed out)
- **"Step 3: Spawn review agents"** — mark `in_progress` before agents launch; mark `completed` when all agent output files collected (or health-monitoring cutoff reached)
- **"Step 4: Cross-validate critical findings"** — mark `in_progress` before verifier spawns; mark `completed` when all verdicts received; **skip task creation when no critical/blocking findings exist after Step 3**
- **"Step 5: Consolidate findings"** — mark `in_progress` before spawning consolidator; mark `completed` when consolidator returns its JSON envelope (Write to `review-report.md` done) — **do NOT mark completed for the terminal print, that's a separate task below**
- **"Step 5b: Print report header"** — created **blockedBy** "Step 5: Consolidate findings"; mark `in_progress` immediately after the consolidator's envelope returns; mark `completed` only once the `---` header table has actually appeared in this response's output (not merely queued/intended). The consolidator's JSON envelope is a routing signal for the orchestrator, never a substitute for reading `$REPORT_DIR/review-report.md` and printing its header. **The follow-up gate's `AskUserQuestion` must not fire while this task is `pending`/`in_progress`** — a sibling skill (oss:review, identical consolidator+print architecture) had an incident where the hard-enforced tool call (`AskUserQuestion`) fired correctly while this prose-only print step got silently dropped; the dedicated task exists specifically to make the print step as trackable/enforceable as the tool calls around it.

## Flag parsing

Strip flags from `$ARGUMENTS` before using as path:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/extract-keep-flag.py" dev-review "$ARGUMENTS"  # timeout: 5000 — parses --keep, clears stale contract
```

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_parse_args.py" --skill review --write-files "$ARGUMENTS"
# writes ${TMPDIR:-/tmp}/dev-review-{no-challenge,semble,codemap,clean-args}-${CSID} + legacy paths
IFS= read -r REVIEW_ARGS < "${TMPDIR:-/tmp}/dev-review-clean-args-${CSID}" 2>/dev/null || REVIEW_ARGS="$ARGUMENTS"
# strip --keep so it doesn't leak into find/git diff target path
REVIEW_ARGS=$(echo "$REVIEW_ARGS" | sed -E 's/ *--keep +"[^"]+"//' | xargs 2>/dev/null || echo "$REVIEW_ARGS")
IFS= read -r CHALLENGE_ENABLED < "${TMPDIR:-/tmp}/dev-review-challenge-enabled-${CSID}" 2>/dev/null || CHALLENGE_ENABLED="true"
IFS= read -r CHALLENGE_FORCED < "${TMPDIR:-/tmp}/dev-review-challenge-forced-${CSID}" 2>/dev/null || CHALLENGE_FORCED="false"  # --challenge: force Agent 7 even on small diffs
IFS= read -r SEMBLE_ENABLED < "${TMPDIR:-/tmp}/dev-review-semble-enabled-${CSID}" 2>/dev/null || SEMBLE_ENABLED="false"
IFS= read -r CODEMAP_RAW < "${TMPDIR:-/tmp}/dev-review-codemap-enabled-${CSID}" 2>/dev/null || CODEMAP_RAW="auto"
IFS= read -r FANOUT_FULL < "${TMPDIR:-/tmp}/dev-review-fanout-full-${CSID}" 2>/dev/null || FANOUT_FULL="false"
FANOUT_CAP=3; [ "$FANOUT_FULL" = "true" ] && FANOUT_CAP=0  # 0 = no cap: all preselected dimensions
```

**Unsupported flag check** — after all supported flags extracted, scan `$ARGUMENTS` for remaining `--<token>` tokens not in the supported list below. If found: print `` ! Unknown flag(s): `--<token>`. Supported: `--no-challenge`, `--challenge`, `--codemap`, `--no-codemap`, `--semble`, `--worktree`, `--full`, `--keep`. `` then invoke `AskUserQuestion` — (a) **Abort** (stop, re-invoke with correct flags) · (b) **Continue ignoring** (skip unknown flags, proceed). On Abort: stop.

## Worktree isolation

> loads: worktree-isolation.md

When `--worktree` set, run the review in an isolated git worktree so no dimension agent can accidentally mutate the main sources — **before** Step 1.

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r WORKTREE_ENABLED < "${TMPDIR:-/tmp}/dev-review-worktree-${CSID}" 2>/dev/null; [ "$WORKTREE_ENABLED" = "true" ] || WORKTREE_ENABLED=false
```

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEV_SHARED < "${TMPDIR:-/tmp}/dev-shared-${CSID}" 2>/dev/null || _DEV_SHARED=""  # timeout: 5000
[ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"
cat "$_DEV_SHARED/worktree-isolation.md"
```

`WORKTREE_ENABLED=true` → follow §Enter (base off HEAD, `EnterWorktree(path=…)`). **Read-only skill** — obey §Deliverable: `$RUN_DIR` (`.temp/`) handoffs stay in the worktree, but `$REPORT_DIR` (the review report) is written to the **main tree** (`$_ORIG_ROOT`, wired at Step 2) so its printed path + follow-up gate stay valid. Reviews committed HEAD state — uncommitted working-tree changes are not visible. Else skip — run in main tree.

```bash
# CODEMAP_RAW → true/false; strict exits on unavailability  # timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# round-trips one sentinel: raw flag in, resolved value out
CODEMAP_ENABLED=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_codemap_gate.py" review) || exit 1
# codemap: integrated-via-shared
```

> loads: codemap-gates.md

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEV_SHARED < "${TMPDIR:-/tmp}/dev-shared-${CSID}" 2>/dev/null || _DEV_SHARED=""  # timeout: 5000
[ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"
cat "$_DEV_SHARED/codemap-gates.md"
```

Follow Gate A and Gate B.

If `SEMBLE_ENABLED=true`: verify `mcp__semble__search` in available tools. DMI skill — stop enforced via bash exit when semble not configured:

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r SEMBLE_ENABLED < "${TMPDIR:-/tmp}/dev-review-semble-enabled-${CSID}" 2>/dev/null || SEMBLE_ENABLED="false"
if [ "$SEMBLE_ENABLED" = "true" ]; then
    # bash can't check MCP availability — LLM verifies. If mcp__semble__search absent:
    # echo "! --semble requested but semble MCP server not configured. Configure: claude mcp add semble -s user -- uvx --from 'semble[mcp]' semble"; exit 1
    echo "⚠ --semble enabled: verify mcp__semble__search is available in your tools before proceeding; if absent, abort with error above"
fi
```

Use `$REVIEW_ARGS` (not `$ARGUMENTS`) as path for rest of workflow.

## Step 1: Identify scope

Scope + non-Python impact check + Python filter in ONE pass — a single `git diff` capture feeds TARGET, the report-header warnings, and the early exit (previously eight diff invocations across three blocks):

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r REVIEW_ARGS < "${TMPDIR:-/tmp}/dev-review-clean-args-${CSID}" 2>/dev/null || REVIEW_ARGS="$ARGUMENTS"   # re-derive — bash state lost between Bash() calls
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/resolve_review_target.py" -- "$REVIEW_ARGS"
```

Any `⚠` line printed above: include in report header regardless of whether Python files exist. No Python files → `resolve_review_target.py` prints `! Diff contains non-Python files only.` and Step 1 ends there (DMI skill — prose "stop" not executable).

### Scope pre-check

Before spawning agents, classify diff:

- Count files changed, lines added/removed, new classes/modules introduced
- Classify: **FIX** (corrects wrong behavior), **REFACTOR** (same behavior restructured), **FEATURE** (new public API or capability), **CHORE** (config/deps, no logic), **MIXED** — classify by intent, not file count
- **Complexity smell**: 8+ files changed → note in report header

Skip optional agents by classification:

- FIX → skip Agent 3 (perf-optimizer) and Agent 6 (solution-architect), unless the diff changes an already-exported public function's signature (added/removed/renamed params, new flags) — then Agent 6 still runs per its own trigger (Agent 1's "API-consistency audit" subsection already checks the surface; Agent 6 adds design-quality/backward-compat judgment)
- REFACTOR → skip Agent 6 (solution-architect), same exception — an already-exported public function's signature change still fires Agent 6
- CHORE (config/deps, no logic) → skip Agent 2 (qa-specialist), Agent 3 (perf-optimizer), and Agent 6 (solution-architect); keep Agent 1 (sw-engineer), Agent 4 (doc-scribe), Agent 5 (linting-expert). No logic means no test-gap, perf, or architecture surface — same saving pattern as `oss:review`'s DOCS_TYPING/TESTS_CI pre-classification (cc_oss/skills/review/SKILL.md:182,198).
- FEATURE/MIXED → spawn all agents
- **Small-diff challenger skip** (any classification) — unless `--challenge` was passed (`CHALLENGE_FORCED=true`): diff is single file, \<50 lines changed, and introduces no new public API / exported symbol → also skip Agent 7 (challenger). Multi-file, ≥50 lines, or any new public API → challenger runs. `--no-challenge` (`CHALLENGE_ENABLED=false`) disables Agent 7 entirely regardless.

### Structural context + review pre-flight (codemap-py — only if `CODEMAP_ENABLED=true`)

**Skip entire section if `CODEMAP_ENABLED=false`** — sets `codemap_available=false` for downstream agent prompts; agents fall back to file reads.

Extended scan for changed modules — runs v4 pre-flight queries per module, persists structured output to `$RUN_DIR/codemap-context.md`, and sets `codemap_available` flag for Step 3 agent spawns:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
codemap_available=false
IFS= read -r CODEMAP_ENABLED < "${TMPDIR:-/tmp}/dev-review-codemap-enabled-${CSID}" 2>/dev/null || CODEMAP_ENABLED="false"
if [ "$CODEMAP_ENABLED" = "true" ]; then
    codemap_available=true
    # RUN_DIR not made yet (Step 2) — stage here, copy after mkdir. codemap-py on PATH guaranteed (codemap_resolve.py confirmed)
    CODEMAP_CONTEXT_STAGE="${TMPDIR:-/tmp}/dev-review-codemap-context.md-${CSID}"
    BATCH_REQ="${TMPDIR:-/tmp}/dev-review-codemap-batch.json-${CSID}"
    # derives changed modules from diff, batches central + 5 pre-flight queries/module — one query process, not N×5 spawns
    python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/build_codemap_batch.py" "$BATCH_REQ"  # timeout: 5000
    {
        echo "## Structural Context (codemap-py)"
        echo
        echo "### Batched query results (one shared coverage block)"
        codemap-py query --timeout 20 batch "$BATCH_REQ" 2>/dev/null
        echo
        echo "### Change-set blast radius (diff-impact)"
        # fn-level context batch can't give: fn-rdeps per symbol (line-range, methods), unioned test-impact, risk tiers
        codemap-py query --timeout 15 diff-impact 2>/dev/null
    } > "$CODEMAP_CONTEXT_STAGE"
fi
echo "$codemap_available" > "${TMPDIR:-/tmp}/dev-review-codemap-available-${CSID}"
```

Codemap context propagation in Step 3:

- `codemap_available=true` → copy `$CODEMAP_CONTEXT_STAGE` to `$RUN_DIR/codemap-context.md` once `$RUN_DIR` exists (Step 2). Each dimension spawn prompt gets a literal block holding only ITS slice of the batch results (the whole block in every prompt re-bills every query result to consumers that never read it, and results for skipped agents are injected nowhere): qa unit → `uncovered` + `mock-rdeps` entries; doc unit → `undocumented` + `xrefs --broken` entries; sw-engineer → `rdeps` + `central` + `diff-impact`. Entries whose only consumer was skipped by classification are not injected anywhere:
  ```text
  ## Structural Context (codemap-py, codemap_available=true)
  <this agent's slice of $RUN_DIR/codemap-context.md>

  Read this section first. The results are a single `batch` JSON array: each entry has `cmd` (the query) and `result` (its payload), keyed by `index`; one shared `index` coverage block covers the whole batch. For symbols listed in `uncovered`/`mock-rdeps`/`undocumented`/`xrefs --broken` entries, trust the codemap output; skip redundant Grep/Read on the same data. Fall back to file reads only when a query's `result` is empty for a symbol you need or when verifying a specific finding.

  **Bounded call budget**: symbol not covered by the batch above → up to 3 additional `codemap-py query` calls this review pass. **Hard stop on `query_complete: true`** (or legacy `exhaustive: true`): that result is final for its direction — no follow-up Grep/Read/query to re-confirm it.

  Per-agent priority (skip redundant reads for symbols the listed query already covers):
  - qa-specialist (Agent 2): `uncovered` + `mock-rdeps` first
  - doc-scribe (Agent 4): `undocumented` + `xrefs --broken` first
  - sw-engineer (Agent 1): `rdeps` first (importers per changed module)
  - challenger (Agent 7): unchanged — always reads source directly
  ```
- `codemap_available=false` → omit block; agents proceed with current file-read behaviour.

> Per-agent consumption guidance kept in sync with `$_DEV_SHARED/codemap-context.md` §Review-pipeline injection — update both on change.

Tier annotation for Agent 1 (sw-engineer) only: label each module's `imported_by` count — **high risk** (>20), **moderate** (5–20), **low** (\<5). Agent 1 uses this to prioritize: high `imported_by` modules warrant deeper scrutiny on API compatibility, error handling, behavioural correctness — downstream callers outside diff not otherwise visible.

**Semble companion** (only if `SEMBLE_ENABLED=true`): include in Agent 1 spawn prompt:

> If `mcp__semble__search` is available in your tools and any changed module's codemap result was non-exhaustive (`"exhaustive": false`) or no codemap index found: call `mcp__semble__search` with varied queries and `repo=<git_root>`, `top_k=20`. Stop per module when two consecutive queries return no new importers. Merge with codemap results. If `mcp__semble__search` is NOT available (MCP not activated in this session): use Grep and Glob tools as fallback for code search — search for import references and usages of changed modules using `Grep(pattern="from <module>|import <module>", path=".")`.

## Step 2: Codex co-review

Set up run directory:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
TIMESTAMP=$(date -u +%Y-%m-%dT%H-%M-%SZ)
RUN_DIR=".temp/review/$TIMESTAMP"
mkdir -p "$RUN_DIR"  # timeout: 5000
# persisted for agents — hand-retyping drops leading dot (stray temp/review/ dirs)
echo "$RUN_DIR" > "${TMPDIR:-/tmp}/dev-review-run-dir-${CSID}"
# REPORT_DIR: orig-root sentinel (worktree §Enter) or pwd — stays reachable outside worktree. RUN_DIR stays worktree-local.
IFS= read -r _REPORT_BASE < "${TMPDIR:-/tmp}/dev-review-orig-root-${CSID}" 2>/dev/null || _REPORT_BASE="$(pwd)"
[ -n "$_REPORT_BASE" ] || _REPORT_BASE="$(pwd)"
REPORT_DIR="$_REPORT_BASE/.reports/review/$TIMESTAMP"
mkdir -p "$REPORT_DIR"  # timeout: 5000
echo "$REPORT_DIR" > "${TMPDIR:-/tmp}/dev-review-report-dir-${CSID}"  # persist for contract-write
REPORT_DIR_LITERAL="$REPORT_DIR"
```

Check availability:

```bash
CODEX_STATUS=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/check_bridge.py" --status 2>/dev/null || echo "absent")  # timeout: 5000
[ "$CODEX_STATUS" = "available" ] && echo "bridge@borda-ai-rig available" || echo "⚠ bridge@borda-ai-rig is ${CODEX_STATUS} — skipping co-review"
```

Materialize codemap context into run directory (`$RUN_DIR` now exists):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r codemap_available < "${TMPDIR:-/tmp}/dev-review-codemap-available-${CSID}" 2>/dev/null || codemap_available="false"
if [ "$codemap_available" = "true" ] && [ -f "${TMPDIR:-/tmp}/dev-review-codemap-context.md-${CSID}" ]; then
    cp "${TMPDIR:-/tmp}/dev-review-codemap-context.md-${CSID}" "$RUN_DIR/codemap-context.md"
fi
```

If Codex available:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
CODEX_OUT="$RUN_DIR/codex.md"
echo "$CODEX_OUT" > ${TMPDIR:-/tmp}/dev-review-codex-out-${CSID}  # Step 6 re-reads — bash state lost
```

Read `$_DEV_SHARED/codex-prepass.md` for the **skip/run criteria only** (small-diff skip, availability check) — do NOT use its dispatch line as the spawn prompt: its args name no output path, so Step 2's watch on `$RUN_DIR/codex.md` would idle ~2 min on a file that never appears and Step 6 would silently skip.

Dispatch — substitute `<TARGET>` and `<RUN_DIR>` with resolved literal values before the call (a `Skill()` dispatch gets no run-dir preamble, so an unexpanded `$RUN_DIR` reaches Codex as literal dollar-sign text): `Skill(skill="bridge:review", args="Read-only adversarial review of <TARGET>. Look for bugs, missed edge cases, incorrect logic, and inconsistencies with existing code patterns. Write findings to <RUN_DIR>/codex.md; do not apply fixes.")` (requires `bridge@borda-ai-rig`).

Note: agent spawns run in the background and cannot be timeout-wrapped via Bash `timeout:`. Dispatch, then **end the turn** — the completion notification resumes the workflow; never hold the turn open with a no-op call, a "waiting" line, or a sleep. Health monitoring per CLAUDE.md §6: at most one liveness probe per wake-up (`find "$RUN_DIR" -newer <sentinel>`); no new file activity across wake-ups for ~15 min → treat as timed out and continue with what exists.

After Codex writes `$RUN_DIR/codex.md` (or times out), extract compact seed list (≤10 items, `[{"loc":"file:line","note":"..."}]`) to inject into agent prompts in Step 3 as pre-flagged issues to verify or dismiss. Codex skipped, timed out, or found nothing → proceed with empty seed.

**Cap-disclosure**: count total Codex findings before truncating. If ≥10, surface in consolidated report header:

```text
Codex: first 10 items seeded to review agents; full list in $RUN_DIR/codex.md (N total) — review codex.md directly for complete coverage.
```

Pass notice through to consolidator (Step 5) so it appears in final report header, not just terminal scratch.

## Step 3: Spawn sub-agents in parallel

**Spawn-count gate — apply before spawning anything.** Each agent costs ~120,851 tok of fixed overhead regardless of how little work it does, i.e. ~73 tool-calls' worth, plus ~12.0 s/call. Rules, all mandatory:

Two stages, in order — never collapse them:

1. **Classification preselection** (always): the FIX / CHORE / small-diff-challenger skips above decide which dimensions are *relevant at all*. A dimension with no changed file in its territory is out here and never comes back, at any flag.
2. **Unit grouping** (always): fold the survivors into spawn units per §Merged spawn units below — Agents 3+6 share one spawn, Agents 4+5 share one spawn, Agents 1 and 2 stay standalone. A unit exists when at least one of its dimensions survived stage 1; its prompt carries only the surviving dimensions' instructions.
3. **Relevance ranking** (default only): rank the units by evidence — changed files and lines in their dimensions' territory, what the classification implies, what the structural context flagged (a merged unit ranks by its strongest surviving dimension) — and spawn the top `FANOUT_MAX` (3) units. With `--full` (`FANOUT_CAP=0`) skip this stage and spawn every unit of stage 2.

- More work → give each agent more, never add agents.
- **Spawn the fewest that keep each near `AGENT_CALL_BUDGET`** — not the most the cap allows. Total work under ~73 calls → do it inline and spawn nothing.
- **Merge before you split**: two dimensions whose files overlap go to one agent, not two — the fixed pairs below are the floor, not the ceiling.
- Every spawn prompt states the budget and requires an envelope even on exhaustion — `partial: true` plus what was finished.
- Dimensions dropped by the cap are listed in the report; never silently skipped.

### Merged spawn units (shared pattern with oss:review — each spawn costs ~120,851 tok fixed overhead, so paired dimensions share one spawn)

- **Agents 3+6 = ONE `foundry:perf-optimizer` spawn** covering Performance + Architecture/API design — spawn when either dimension survives classification preselection; the prompt includes only the surviving dimensions' instructions (e.g. FIX with a public-signature change spawns this unit with only the Agent-6 dimension active).
- **Agents 4+5 = ONE `foundry:doc-scribe` spawn** covering Documentation + Linting — same rule.
- Agents 1 (sw-engineer, incl. security augmentation), 2 (qa-specialist), and 7 (challenger) stay standalone spawns.
- A merged spawn writes **one file per covered dimension**, each with its OWN full sections + Confidence block — never blended: `perf-optimizer.md` + `solution-architect.md`, or `doc-scribe.md` + `linting-expert.md`. Downstream contracts (consolidator filename list, Step-4 cross-validation "same type as origin", report sections) key on those files and stay unchanged.
- Merged-spawn envelope = JSON array, one element per dimension file, same per-element schema as the standard envelope. An element absent from the array ⇒ that dimension gets the ⏱ marker — never silently omitted.

**File-based handoff**:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEV_SHARED < "${TMPDIR:-/tmp}/dev-shared-${CSID}" 2>/dev/null || _DEV_SHARED=""   # re-derive — bash state lost between Bash() calls
[ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"
cat "$_DEV_SHARED/file-handoff-protocol.md"
```

Run directory created in Step 2 (`$RUN_DIR`).

<!-- $REPORT_DIR pre-expanded into $REPORT_DIR_LITERAL — substitute literal vars in Agent spawn prompt strings. Run-dir is the exception: agents self-resolve it (see run-dir preamble below), never hand-substituted. -->

### Run-dir preamble (canonical)

Prepend this to every agent spawn prompt (Agents 1–7 and the Step 5 consolidator):

> "First run Bash `CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"; IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/dev-review-run-dir-${CSID}" 2>/dev/null || RUN_DIR=""` to obtain the exact run-dir path. Use `$RUN_DIR` verbatim for every file you read or write — never retype the path literally (the leading `.` in `.temp` is easy to drop, scattering output into a stray `temp/` dir)."

Inside agent prompt strings, leave `$RUN_DIR` literal — agent resolves it via preamble. Orchestrator must NOT hand-substitute run-dir path.

### $VAR_LITERAL pre-expansion rule (canonical)

Any OTHER shell variable inserted into an Agent spawn prompt string — `$REPORT_DIR_LITERAL`, `$REVIEW_CHECKLIST`, `$DATE`, `$_REVIEW_TEMPLATE` — must be substituted with its literal resolved value **before** building the Agent call. Bare variable name inside a quoted Agent prompt will NOT expand — spawned agent receives literal dollar-sign text, causing path mismatches. Resolve each to its value first; never pass bare `$VAR` name. (`$RUN_DIR` is the deliberate exception above — agents self-resolve it.)

Resolve develop:review checklist path (version-agnostic):

```bash
if ! command -v jq >/dev/null 2>&1; then
    echo "⚠ jq not available — oss:review checklist path resolution skipped; Agent 1 will proceed without checklist"
    REVIEW_CHECKLIST=""
fi
```

```bash
if command -v jq >/dev/null 2>&1; then
    OSS_ROOT=$(jq -r 'to_entries[] | select(.key | test("oss@")) | .value.installPath' ${HOME}/.claude/plugins/installed_plugins.json 2>/dev/null | head -1) || OSS_ROOT=""  # timeout: 5000; jq failure → empty, handled below
    if [ -z "$OSS_ROOT" ]; then
        echo "⚠ oss plugin checklist unavailable — review will proceed without severity anchors; install oss plugin for full coverage"
        REVIEW_CHECKLIST=""
    else
        REVIEW_CHECKLIST="${OSS_ROOT}/skills/review/checklist.md"
        if [ ! -f "$REVIEW_CHECKLIST" ]; then
            echo "⚠ oss plugin checklist unavailable — review will proceed without severity anchors; install oss plugin for full coverage"
            REVIEW_CHECKLIST=""
        else
            echo "Checklist: $REVIEW_CHECKLIST"
        fi
    fi
fi
```

Replace `$REVIEW_CHECKLIST` in Agent 1 and consolidator spawn prompts with resolved path. **If empty, omit checklist instruction from those prompts entirely** — do not pass empty path.

> See [$VAR_LITERAL pre-expansion rule (canonical)](#var_literal-pre-expansion-rule-canonical) — `$REVIEW_CHECKLIST` follows it; substitute its resolved value before inserting into any Agent spawn prompt.

**Visible-degradation rule** — `$REVIEW_CHECKLIST` empty → print `⚠ REVIEW_CHECKLIST is empty — review scope undefined` at TOP of review output (before Findings), and consolidator prompt (Step 5) **must** insert into report header (YAML `---` block or first line before Findings): "Review checklist not applied (oss plugin not available) — severity anchors may be inconsistent." Silent degradation hides gap from reviewers, makes severity drift invisible.

**Finding evidence standard — applies to every agent, every finding:**

- Every finding must cite specific `file:line` from diff as primary evidence — "I know this typically causes issues" without a diff citation is not a valid finding
- Training knowledge and memory never sufficient evidence — read actual code in diff
- Claims referencing external standards (OWASP, PEP, language spec, CVE) must cite the specific authoritative document — official spec, CVE entry, PEP text; a blog post or Stack Overflow answer referencing the standard is Tier 2 only
- Tier 2 sources (blog, tutorial, forum, memory) require ≥3 genuinely independent sources OR experimental validation before claim becomes a finding; independent means different authors, different primary research with distinct origins — N posts all citing same original = 1 source
- Citation tracing mandatory: for each Tier 2 source, follow its citations one level; if tracing reveals a Tier 1 source (official doc, CVE, spec) confirming the claim, treat as Tier 1 verified; if multiple Tier 2 sources share one origin, merge into one; count distinct origins only
- When only Tier 2 available, distinct-origin count < 3, and no experiment run: downgrade finding to LOW or drop it; never raise MEDIUM/HIGH/CRITICAL on Tier 2 alone

Launch spawn units simultaneously with Agent tool (security augmentation folded into Agent 1 — not separate spawn; Agent 6 optional; Agents 3+6 and 4+5 launch as merged units per §Merged spawn units). Every agent prompt must begin with the [run-dir preamble (canonical)](#run-dir-preamble-canonical) and end with:

> "Write your FULL findings (all sections, Confidence block) to `$RUN_DIR/<agent-name>.md` using the Write tool — where `<agent-name>` is e.g. `sw-engineer`, `qa-specialist`, `perf-optimizer`, `doc-scribe`, `linting-expert`, `solution-architect`. Then return to the caller ONLY a compact JSON envelope on your final line — nothing else after it: `{\"status\":\"done\",\"findings\":N,\"severity\":{\"critical\":0,\"high\":1,\"medium\":2,\"low\":0},\"file\":\"$RUN_DIR/<agent-name>.md\",\"confidence\":0.88}`"

**Codemap-py context preamble (substituted by orchestrator)**: rehydrate `IFS= read -r codemap_available < "${TMPDIR:-/tmp}/dev-review-codemap-available-${CSID}" 2>/dev/null || codemap_available=false`. When `codemap_available=true`, each dimension spawn prompt is prefixed with its `## Structural Context (codemap-py, codemap_available=true)` slice from `$RUN_DIR/codemap-context.md` per the per-agent slicing rules in Step 1. Agents must read that block first and skip redundant Grep/Read on symbols already covered by codemap output. Block absent → fall back to current file-read behaviour. Challenger (Agent 7) unchanged.

**Agent 1 — foundry:sw-engineer**: Review architecture, SOLID adherence, type safety, error handling, code structure. Check Python anti-patterns (bare `except:`, `import *`, mutable defaults). Flag blocking issues vs suggestions. `codemap_available=true`: read `rdeps` first (importer list per changed module) — skip importer-walk Reads on listed modules; verify only when needed for a specific finding.

**API-consistency audit** (any diff hunk touching public API surface — new/changed function, method, class, constant, param, flag, return shape, or module placement; fires for new kwargs on already-exported functions too, not gated on `__init__.py` churn): for each public symbol added or changed, `Read` the ACTUAL surrounding surface from source — the existing function/class it lives beside, its siblings' signatures, the module it sits in — and validate the change against the established API principles, not in isolation:

- **Params / discriminators**: new boolean/flag that overlaps an existing discriminator/enum param (`kind=`, `mode=`, `type=`, `backend=`, `format=`) → **HIGH**: "adds parallel `<flag>` while `<existing>=` already discriminates — extend the existing enum (`<existing>="<value>"`) instead". Canonical miss: `tensorrt: bool` added when `kind="onnx"` already exists → should be `kind="tensorrt"`. New param inconsistent with sibling ordering/default conventions → **MEDIUM**.
- **Naming**: new function/method/class/constant name that breaks sibling conventions (verb-noun form, casing, prefix/suffix pattern, `get_`/`is_`/`to_` idioms in the same module) → **MEDIUM**; a name that duplicates or shadows an existing public symbol's meaning → **HIGH**.
- **Organization / placement**: symbol added to the wrong module/class, or duplicating capability that already lives elsewhere in the package, or bypassing an established factory/registry/dispatch entry point → **MEDIUM–HIGH** (flag: reuse/extend the existing home instead).
- **Return / type shape**: return type or structure inconsistent with sibling functions doing the same job (one returns a dataclass, the new one a raw tuple) → **MEDIUM**.
- Any API-shape suggestion YOU emit must itself be checked against the read surface — never propose a name, param, or placement without confirming it does not duplicate or contradict an existing one.

**Error path analysis** (new/changed code in diff): For each error-handling path introduced or modified, produce table:

| Location | Exception/Error | Caught? | Action if caught | User-visible? |
| -- | -- | -- | -- | -- |

Flag rules:

- Caught=No + User-visible=Silent → **HIGH** (unhandled error path)
- Caught=Yes + Action=`pass` or bare `except` → **MEDIUM** (swallowed error)
- Cap at 15 rows. New/changed paths only, not entire codebase.

Read review checklist (Read tool → `$REVIEW_CHECKLIST`) — apply CRITICAL/HIGH patterns as severity anchors. Respect suppressions list.

**Agent 2 — foundry:qa-specialist**: Audit test coverage. Identify untested paths, missing edge cases, test quality issues. Check ML-specific issues (non-deterministic tests, missing seed pinning). List top 5 missing tests. `codemap_available=true`: read `uncovered` + `mock-rdeps` sections from codemap context block first — symbols listed in `uncovered` lack any test rdep; symbols listed in `mock-rdeps` are tested via mock (not falsely "untested"). Skip manual grep/Read of `tests/` for symbols codemap already classifies; fall back to file reads only when codemap output empty for a symbol needed or when verifying a specific finding. Explicitly check for missing tests in these patterns (GT-level findings, not afterthoughts):

- Concurrent access to shared state (locks or shared variables present)
- Error paths: calling methods in wrong order (e.g., `log()` before `start()`)
- Resource cleanup on exception (file handles, database connections)
- Boundary conditions for division, empty collections, zero-count inputs
- Type-coercion boundary inputs: functions parsing/converting strings to typed values (`int()`, `float()`, `datetime`) — test near-valid inputs (float strings for int parsers, empty strings, very large values, `None`) — common omissions.

**Consolidation rule**: Each test gap = one finding with concise list of test scenarios, not separate findings per scenario. Format: "Missing tests for `parse_numeric()`: empty string, None, very large integers, float-string for int parser." Keeps test coverage section actionable, prevents exceeding 5 items.

**Agent 3 — foundry:perf-optimizer** (merged spawn with Agent 6 — see §Merged spawn units): Analyze performance issues. Algorithmic complexity, Python loops that should be NumPy/torch ops, repeated computation, unnecessary I/O. ML code: check DataLoader config, mixed precision. Prioritize by impact. Findings to `$RUN_DIR/perf-optimizer.md`.

**Agent 4 — foundry:doc-scribe** (merged spawn with Agent 5 — see §Merged spawn units): Check documentation completeness. Public APIs without docstrings, missing Google style sections, outdated README, CHANGELOG gaps. Verify examples run. `codemap_available=true`: read `undocumented` + `xrefs --broken` sections from codemap context block first — `undocumented` enumerates symbols missing docstrings; `xrefs --broken` enumerates stale Sphinx refs. Skip docstring-scan Reads on listed symbols; fall back to file reads only when codemap output empty for a symbol needed or when verifying a specific finding. Findings to `$RUN_DIR/doc-scribe.md`.

- **Algorithmic accuracy check**: Functions computing mathematical results (moving averages, statistics, transforms, distances) — verify docstring behavioral claims match implementation. Deviation from conventional definition → MEDIUM; docstring must document deviation, not state standard definition. **Deprecation check**: Check deprecated stdlib usage in public API surface only — skip private functions, classes, constants, and modules starting with `_`. E.g., `datetime.utcnow()` deprecated in 3.12, `os.path` vs `pathlib`. Flag deprecated stdlib as MEDIUM with replacement.

**Agent 5 — foundry:linting-expert** (dimension covered by the Agent 4 merged spawn — see §Merged spawn units): Static analysis audit. Check ruff and mypy pass. Type annotation gaps on public APIs, suppressed violations without explanation, missing pre-commit hooks. Flag mismatched target Python version. Findings to `$RUN_DIR/linting-expert.md`.

**Security augmentation (conditional — fold into Agent 1 prompt, not separate spawn)**: Target touches authentication, user input handling, dependency updates, or serialization → add to foundry:sw-engineer prompt (Agent 1): check SQL injection, XSS, insecure deserialization, hardcoded secrets, missing input validation. If dependency files changed: check pip-audit availability first — `if ! command -v pip-audit >/dev/null 2>&1; then echo "⚠ pip-audit not found — dependency vulnerability check skipped"; else <run pip-audit>; fi`. Skip for purely internal refactoring.

**Agent 6 — foundry:solution-architect (optional, for changes touching public API boundaries; dimension covered by the Agent 3 merged spawn — see §Merged spawn units, findings to `$RUN_DIR/solution-architect.md`)**: Target touches `__init__.py` exports, adds/modifies Protocols or ABCs, changes module structure, introduces new public classes, **or changes the signature of any already-exported public function — added/removed/renamed params, new flags** → evaluate API design quality, coupling impact, backward compatibility, and consistency of any added symbol (name, placement, signature, param/flag, return shape) with the existing API surface — naming conventions, module organization, sibling patterns (e.g. a new bool that duplicates an existing `kind=`/`mode=` discriminator, or a helper added where an equivalent already lives → flag, reuse/extend the existing home instead). Skip for purely internal (non-exported) implementation changes.

**Agent 7 — foundry:challenger (skip if `CHALLENGE_ENABLED=false`, or per Small-diff challenger skip in Scope pre-check when `CHALLENGE_FORCED=false`)**: Adversarial review of design decisions in diff. Attacks assumptions, missing edge cases, security risks, architectural concerns, complexity creep with mandatory refutation step. File-handoff: write full findings to `$RUN_DIR/challenger.md`. Return JSON: `{"status":"done","findings":N,"severity":{"critical":0,"high":0,"medium":0,"low":0},"file":"$RUN_DIR/challenger.md","confidence":0.88}`. Severity mapping: blockers → `high`; concerns → `medium`.

**Challenger severity propagation**: when consolidator (Step 5) reads `challenger.md`, map challenger severity labels to review severity labels before merging — CRITICAL → `critical`, HIGH → `high`, MEDIUM → `medium`, LOW → `low`. Do not drop severity; if challenger uses non-standard labels (e.g. "blocker", "concern"), apply mapping: blockers → `high`, concerns → `medium`.

**Health monitoring**: agent calls run in the background. Spawn the batch, end the turn, and resume on each completion notification — no filler tool calls, no "waiting" turns, no sleep. If an agent returns partial results or errors, use Read tool on `$RUN_DIR/<agent-name>.md` for details. Mark agents that returned empty or error with ⏱ in final report. Never silently omit agents that **failed** (returned error/partial) — they must appear with ⏱ marker. Agents that are **not spawned** (skipped due to mode flags, docs-only, CHORE mode) may be absent from RUN_DIR; consolidator "skip missing" applies only to legitimately-not-spawned agents.

```bash
# compaction boundary 1 (compaction-contract.md §Lifecycle)
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _RUN_DIR < "${TMPDIR:-/tmp}/dev-review-run-dir-${CSID}" 2>/dev/null || _RUN_DIR=""
IFS= read -r _REPORT_DIR < "${TMPDIR:-/tmp}/dev-review-report-dir-${CSID}" 2>/dev/null || _REPORT_DIR=""
IFS= read -r _KEEP < "${TMPDIR:-/tmp}/dev-review-keep-items-${CSID}" 2>/dev/null || _KEEP=""
IFS= read -r _TARGET < "${TMPDIR:-/tmp}/dev-review-clean-args-${CSID}" 2>/dev/null || _TARGET="working-tree diff"
_FINDING_FILES=$(ls "$_RUN_DIR/"*.md 2>/dev/null | tr '\n' ' ' | sed 's/ *$//')
_PRESERVE="run-dir=$_RUN_DIR, report-dir=$_REPORT_DIR, target=$_TARGET, finding-files=$_FINDING_FILES"
[ -n "$_KEEP" ] && _PRESERVE="$_PRESERVE; user-keep: $_KEEP"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/write_skill_contract.py" "develop:review" "consolidation (after parallel review-agent fan-out)" "$_RUN_DIR" "$_PRESERVE" "cross-validate critical findings (Step 4) → consolidate → final report"  # timeout: 5000
```

## Step 4: Cross-validate critical/blocking findings

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEV_SHARED < "${TMPDIR:-/tmp}/dev-shared-${CSID}" 2>/dev/null || _DEV_SHARED=""   # re-derive — bash state lost between Bash() calls
[ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"
IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/dev-review-run-dir-${CSID}" 2>/dev/null || RUN_DIR="$RUN_DIR"
if [ ! -f "$_DEV_SHARED/foundry--cross-validation-protocol.md" ]; then
    echo "⚠ foundry--cross-validation-protocol.md not found at $_DEV_SHARED — Step 4 skipped; critical findings are unverified. It ships in this plugin — reinstall develop@borda-ai-rig."
    echo "## Cross-Validation: SKIPPED" >> "$RUN_DIR/cross-validation.md"
    echo "**Reason**: _DEV_SHARED unavailable — cross-validation protocol not executed." >> "$RUN_DIR/cross-validation.md"
else
    cat "$_DEV_SHARED/foundry--cross-validation-protocol.md"
fi
```

If file present: follow cross-validation protocol printed above. File absent → skip Step 4 (warning printed above).

**Skill-specific**: use **same agent type** that raised finding as verifier (e.g., foundry:sw-engineer verifies foundry:sw-engineer's critical finding). **Spawn cap: max 3 verifier agents** — critical/blocking findings > 3 → group into batches of ≤2 findings per verifier (same origin type per batch); note grouped IDs in rationale; each finding still gets its own verdict. Same cap as the shared protocol and oss:review — unbounded verifier fanout costs ~120,851 tok per critical finding.

## Step 5: Consolidate findings

Extract branch and date before constructing output path: `BRANCH=$(git branch --show-current 2>/dev/null | tr '/' '-' || echo 'main')` `DATE=$(date +%Y-%m-%d)`

Spawn consolidator agent (general-purpose — synthesis only, no engineering specialization needed):

```bash
# loads: consolidator-prompt.md
_TPL="${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/skills/review/templates/consolidator-prompt.md"
cat "$_TPL"
```

Use as full consolidator instructions. Prepend the [run-dir preamble (canonical)](#run-dir-preamble-canonical) so consolidator self-resolves `$RUN_DIR`. Summary: read all finding files in `$RUN_DIR/`, apply consolidation rules, write report to `$REPORT_DIR_LITERAL/review-report.md`. Substitute `$REPORT_DIR_LITERAL`, `$DATE`, and `$REVIEW_CHECKLIST` with literal resolved values before inserting into spawn prompt — see [$VAR_LITERAL pre-expansion rule (canonical)](#var_literal-pre-expansion-rule-canonical); leave `$RUN_DIR` literal (agent self-resolves). Return ONLY compact JSON envelope: `{"status":"done","findings":N,"severity":{"critical":N,"high":N,"medium":N,"low":N},"file":"$REPORT_DIR_LITERAL/review-report.md","confidence":0.N,"summary":"<one-line verdict>"}`

Main context receives only one-liner verdict.

**Consolidator-unavailable fallback**: if `Agent` tool deferred or consolidator times out — read each agent finding file from `$RUN_DIR/` directly, apply same precision gate and density rules, synthesize consolidated report, write to `$REPORT_DIR/review-report.md` using Write tool.

Report format — resolve template path first:

```bash
_REVIEW_TEMPLATE=$(ls -td ~/.claude/plugins/cache/borda-ai-rig/develop/*/skills/review/templates 2>/dev/null | head -1); [ -z "$_REVIEW_TEMPLATE" ] && _REVIEW_TEMPLATE="${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/skills/review/templates"
_REVIEW_TEMPLATE="$_REVIEW_TEMPLATE/review-report.md"
echo "$_REVIEW_TEMPLATE"
```

Substitute the resolved literal path into the consolidator spawn prompt ("Read the report template at `<path>` with the Read tool — it defines the output structure"); do NOT cat the template into orchestrator context — that bills the same ~579 tok twice (once here, once inside the prompt).

After parsing confidence scores: any agent scored < 0.7 → prepend **⚠ LOW CONFIDENCE** to that agent's findings section, state gap explicitly. Never silently drop uncertain findings.

TaskUpdate "Step 5b: Print report header" → `in_progress`.

**MANDATORY, not optional narration** — the consolidator's returned JSON envelope is a routing signal only; it is never printed to the user and never satisfies this step. Perform, in this exact order, in this same turn, before any other Step 5/6 text: (1) read `---` header from top of `$REPORT_DIR/review-report.md` (lines 1–15, up to and including closing `---`) via the Read tool; (2) render its fields as a two-column Markdown table (`Field | Value`, one row per key, file order) per quality-gates.md §Report File Format's Universal terminal-print rule — never print the raw `---`-delimited block; (3) append `→ saved to $REPORT_DIR/review-report.md`; (4) TaskUpdate "Step 5b: Print report header" → `completed` — only after the table has actually appeared in this response, never before. Report file already contains the fields — no separate prepend needed. Omit `╔═╗` Re:Anchor box (the table IS the reply header).

```bash
# compaction boundary 2 (compaction-contract.md §Lifecycle)
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _RUN_DIR < "${TMPDIR:-/tmp}/dev-review-run-dir-${CSID}" 2>/dev/null || _RUN_DIR=""
IFS= read -r _REPORT_DIR < "${TMPDIR:-/tmp}/dev-review-report-dir-${CSID}" 2>/dev/null || _REPORT_DIR=""
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/write_skill_contract.py" "develop:review" "follow-up (after consolidation)" "$_RUN_DIR" "final-report=$_REPORT_DIR/review-report.md" "optional Codex delegation → follow-up gate"  # timeout: 5000
```

## Step 6: Delegate implementation follow-up (optional)

Re-hydrate `CODEX_OUT` from persisted temp file (Bash() state does not survive between calls): `IFS= read -r CODEX_OUT < "${TMPDIR:-/tmp}/dev-review-codex-out-${CSID}" 2>/dev/null || CODEX_OUT=""`. Skip Step 6 if `$CODEX_OUT` empty or file at that path does not exist.

After consolidating, identify tasks Codex can implement directly — not style violations (pre-commit handles those), but work requiring meaningful code or documentation grounded in actual implementation.

**Delegate to Codex when you can write accurate, specific brief:**

- Public functions with no docstrings — read implementation first, describe what each does so Codex writes real 6-section docstring, not placeholder
- Missing test coverage for concrete, well-defined behaviour — describe exact scenario to test
- Consistent rename across multiple files — name old and new symbol and why flagged

**Do not delegate — require human judgment:**

- Architectural issues, logic errors, security vulnerabilities, behavioural changes
- Any task where accurate description requires guessing

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEV_SHARED < "${TMPDIR:-/tmp}/dev-shared-${CSID}" 2>/dev/null || _DEV_SHARED=""
[ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"
cat "$_DEV_SHARED/codex-delegation.md"
```

Apply delegation criteria defined there (when found).

Print `### Codex Delegation` section to terminal only when tasks actually delegated — omit entirely if nothing delegated.

**Hard gate**: check "Step 5b: Print report header" task status before anything below. Not `completed` → the header table has not actually been printed yet — go back and do it now (see Step 5), then mark the task `completed`, before calling `AskUserQuestion` below.

**Hook-enforced**: `hooks/enforce-review-header.js` (PreToolUse on `AskUserQuestion`) denies the follow-up gate's call while `$REPORT_DIR/review-report.md` is missing or empty. A denial reading `develop:review report gate` means Step 5 never produced the report — spawn the consolidator, print the header, then re-issue the question. The hook cannot see whether the print happened, only whether the report exists; the task above remains the check for the print itself.

**Worktree exit** — if `WORKTREE_ENABLED=true`: the report already lives in the main tree (§Deliverable). Follow `worktree-isolation.md` §Exit — capture branch, call `ExitWorktree(action="keep")`, append the `Worktree` block to the report/output. Any Step 6 Codex edits stay on the worktree branch for you to merge. Exit **before** the follow-up gate so the `/develop:fix`/`/develop:refactor` next-step suggestions below point at the main tree. Never auto-merge.

**Suggested next steps** (plain text, not selectable — `/develop:fix` and `/develop:refactor` both carry `disable-model-invocation: true`, so `Skill()` dispatch is impossible for either): blocking issues found → `Run: /develop:fix` to reproduce with a test and apply a targeted fix; structural/quality issues found → `Run: /develop:refactor` for test-first improvements.

**Follow-up gate (NEVER SKIP)** — Call `AskUserQuestion` tool — do NOT write options as plain text first. Map options directly into tool call arguments:

- question: "What next?"
- (a) label: `walk through findings` — description: go through each finding interactively
- (b) label: `skip` — description: no action

**Confidence block** — emitted by consolidator agent in `$REPORT_DIR/review-report.md`, not at skill level (DMI skill: top-level model invocation disabled, so any skill-level instruction would be unreachable).

```bash
rm -f .temp/state/skill-contract.md  # clear contract — skill complete (compaction-contract.md §Lifecycle)  # timeout: 5000
```

</workflow>

<notes>

- Critical issues always surfaced regardless of scope
- Skip sections with no issues — no padding with "looks good". Reviewing isolated code without git context → skip Performance Concerns unless code itself shows performance issues.
- **Signal-to-noise gate**: Function or class ≤50 lines with only 1–2 ground-level issues (critical/high) → no more than 2 medium/low findings beyond them. Remainder as `[nit]` in dedicated "Minor Observations" section — not elevated to same tier as high-severity findings.
- **Follow-up chains**:
  - `[blocking]` bugs or regressions → `/develop:fix` to reproduce with test and apply targeted fix
  - Structural or quality issues → `/develop:refactor` for test-first improvements
  - Security findings in auth/input/deps → run `pip-audit` for dependency CVEs; address OWASP issues inline via `/develop:fix`
  - Mechanical issues beyond Step 5 findings → when `bridge@borda-ai-rig` is available, call its `implement` skill with a brief that states the exact finding, target paths, current evidence, permitted edits, required result, stop condition, and verification command.
  - Contributor-facing review of GitHub PR → use `/oss:review <PR#>` (requires oss plugin) instead
- **Parallel agent cleanup**: after all spawn units complete, review `TaskList` — delete any tasks created by sub-agents (not by lead orchestrator). Sub-agent task creation unintended, can leave zombie tasks.

</notes>
