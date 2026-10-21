---
name: review
description: 'Multi-agent code review of local Python files, directories, or the current git diff covering architecture, tests, performance, docs, lint, security, and API design. Scope: Python source files in local working tree. Python-file-free targets (pure JS/TS/Go/Rust projects) are out of scope. TRIGGER when: user asks to review local Python files, a directory, or the current git diff/working-tree changes, with no GitHub PR number involved; phrases: "review this", "review my changes", "code review this diff", "review src/foo.py". SKIP when: input is a bare GitHub PR/issue number (use `/oss:review <PR#>`, requires oss plugin); user wants the Codex-native tiered `$code-review` workflow (codex-rig plugin, JSON artifact + specialist fan-out) — different toolchain; implementation work (use `/develop:fix` or `/develop:feature`); non-Python-only projects.'
argument-hint: '[python-file|dir] [--no-challenge] [--challenge] [--codemap] [--no-codemap] [--worktree] [--full] [--keep "<items>"]'
allowed-tools: Read, Write, Bash, Grep, Glob, Agent, Skill, TaskList, TaskCreate, TaskUpdate, AskUserQuestion, EnterWorktree, ExitWorktree
disable-model-invocation: true
effort: high
---

<objective>

Comprehensive code review of local files or working-tree diff. Spawns specialized sub-agents in parallel, consolidates findings into structured feedback with severity levels.

NOT for: GitHub PR review (use `/oss:review <PR#>` (requires oss plugin)); GitHub thread analysis or PR reply drafting (use `/oss:analyse <PR#>` (requires oss plugin)); implementation (use `/develop:feature` or `/develop:fix`); `.claude/` config changes (use `/foundry:manage` (requires foundry plugin) or `/foundry:audit` (requires foundry plugin)); non-Python-only projects (zero Python source files — pure JS/TS/Go/Rust) — review toolchain assumes Python/pytest; Python test-only targets where diff contains only test files (no `src/` or top-level `.py` source outside `tests/`) — review is uninformative; polyglot projects with Python source review Python files only.

</objective>

<inputs>

- **$ARGUMENTS**: optional file path or directory to review.
  - Path given: review those files
  - Omitted: review current git diff (`git diff HEAD` — staged + unstaged vs HEAD; with `--worktree`, the last commit `HEAD~1..HEAD`)
  - **Scope**: Python source only. Non-Python file (YAML, JSON, shell script, etc.) → state out of scope, suggest appropriate tool. No findings.
  - `--no-challenge`: skip adversarial review (challenger runs by default)
  - `--challenge`: accepted for compatibility, no effect — challenger runs on every diff size unless `--no-challenge`
  - `--full`: run **every** dimension classification preselected, instead of only the `FANOUT_MAX` most relevant. Never widens preselection — a dimension the FIX/CHORE/impact-tier rules ruled out stays out. **Not free**: each extra agent costs ~120,851 tok fixed overhead however little work it does. Default stays capped; pass this when depth matters more than cost.
  - `--codemap`: strict mode — stop and report if codemap not installed (on by default when installed; use `--no-codemap` to opt out)

**PR#/filename disambiguation gate** (its block runs in §Flag parsing, right after the flag block — BEFORE Worktree isolation and Step 1): valid PR# is positive integer (optional leading `#`) with no extension and no existing file at that path. Filenames that look like numbers (e.g. `42.py`) must NOT trigger PR mode. `dev_parse_args.py` classifies the flag-stripped target, so `42 --full` still counts, and writes the bare number or empty to the `dev-review-pr-number` sentinel; the gate block reads it, checks oss through the install record and prints `OSS_AVAILABLE=` and `PR_NUMBER=`. "Proceed to Step 1" below = continue the workflow: Worktree isolation, then Step 1.

`$OSS_AVAILABLE` is `skip` → proceed to Step 1 normally (path / diff / dir mode).

`$OSS_AVAILABLE` is `true` → call `AskUserQuestion` ONCE with BOTH questions in the same call (a second sequential window costs another human-idle round trip) — Q1: "Looks like you passed a PR/issue number. Did you mean to run `/oss:review $ARGUMENTS` (requires oss plugin) to review that PR?" Options: (a) "Yes — launch `/oss:review $ARGUMENTS`" · (b) "No — review local code". Q2: "If reviewing local code: provide the file path or directory (free text; skip if Q1 = Yes)". On (a): call `Skill(skill="oss:review", args="<PR_NUMBER>")` with the bare number the gate block printed (never the flags passed beside it), ignore Q2, then stop — do not enter Step 1. On (b): persist the Q2 response as the review target (block below), proceed to Step 1 (Q2 empty → ask once more for the path).

Persist block for Q2 (shared by both branches) — substitute `<Q2 path>` with the user's answer; it replaces the PR number in the clean-args sentinel Step 1 re-reads:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
Q2_PATH='<Q2 path>'
case "$Q2_PATH" in *'<'*'>'*|"") echo "! BLOCKED — Q2 path unsubstituted"; exit 1;; esac
echo "$Q2_PATH" > "${TMPDIR:-/tmp}/dev-review-clean-args-${CSID}"
```

`$OSS_AVAILABLE` is `false` → call `AskUserQuestion` ONCE with BOTH questions — Q1: "Looks like you passed a PR/issue number, but oss plugin not installed — `/oss:review` unavailable. Review local code instead?" Options: (a) "Yes — review local code" · (b) "I need oss plugin". Q2: "If reviewing local code: provide the file path or directory (free text; skip if Q1 = b)". On (a): persist the Q2 response with the block above, proceed to Step 1 (Q2 empty → ask once more). On (b): inform user: install with `claude plugin install oss@borda-ai-rig`, then stop — do not enter Step 1.

</inputs>

<constants>

```text
FANOUT_MAX=3            # default: top-N spawn UNITS among classification-preselected dimensions; runtime form is FANOUT_CAP (§Flag parsing) — keep both in sync manually if either changes
                        # cap counts dimension spawn units only; real default lineup = up to 3
                        # dimension units + challenger + bridge co-review + consolidator
                        # (+ up to 3 cross-validation verifiers when criticals exist)
                        # --full runs ALL preselected units instead — no numeric cap
AGENT_CALL_BUDGET=55    # target tool-calls per agent; past ~60 they stall without returning an envelope
CHALLENGE_ENABLED=true  # set to false via --no-challenge
CHALLENGE_FORCED=false  # set to true via --challenge — no-op kept for saved invocations; challenger runs at any diff size
CODEMAP_ENABLED=auto    # on by default if codemap installed + index found; --no-codemap = off; --codemap = strict (stop if not installed)
```

</constants>

<compaction>

- Key boundary: end of Step 3 — parallel review-agent fan-out outputs collected, before Step 5 consolidation.
- Second boundary: end of Step 5 — consolidated report written, before Step 6 follow-up.
- Third boundary: immediately before the Step 6 follow-up gate — the idle window; refresh makes a mid-wait `/compact` lossless.
- Preserve at boundary 1: RUN_DIR, REPORT_DIR, target, CLASSIFICATION, per-agent finding file paths, --keep items, frozen REVIEWED_COMMIT_SHA and REVIEWED_COMMIT_KIND.
- Preserve at boundary 2: final report path.
- Preserve at boundary 3: final report path.

</compaction>

<workflow>

<!-- Shared pattern with oss:review — coordinate on agent spawn logic, file-handoff, consolidation changes -->

<!-- Agent resolution: see _DEV_SHARED/agent-resolution.md (mounted by develop plugin init) -->

## Agent Resolution

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo "" > "${TMPDIR:-/tmp}/dev-agent-watch-dir-${CSID}"  # a prior run's dir must not outlive a failed --init; agent-resolution.md §Agent waits recreates it when empty
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_shared_resolve.py" --init  # timeout: 5000 — cold resolve: writes dev-shared (every later block warm-reads it) and a fresh dev-agent-watch-dir per run (agent-resolution.md §Agent waits)
# loads: compaction-contract.md
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_load_shared.py" agent-resolution.md
```

> Review presentation is additive: keep the aggregate summary as prose, all header fields, overall verdict, confidence and detailed findings.
>
> - Ask each actual reviewer for a scoped integer rating and rationale: 1 Approve, 2 Minor changes, 3 Major revision, 4 Insufficient, but a good initiative, 5 Block (source-5-v1); null means no source judgment, never rejection.
> - Add `Reviewers:` after `Agents:` in the report header, using readable role names only and square-bracketed scores: `Role [rating]`, for example `Software engineer [2]`. Omit execution-origin labels and skipped roles.
> - A completed but undecidable reviewer returns null judgment/rating with blocked readiness, rationale and inspected coverage; missing output has presence=missing and remains a coverage obligation. Record these separately in review-validation.json; never invent rejection or approval.
> - Never average role ratings into the final verdict.
> - After rendering the header table, add exactly this line below it, in the same Step 5b delivery block: `Legend: 1 = Approve · 2 = Minor changes · 3 = Major revision · 4 = Insufficient, but a good initiative · 5 = Block.`
> - Findings overview adds `Author` after `ID`, retaining all contributing reviewer roles after deduplication.
> - Read the summary and overview after the header rather than treating the header alone as the complete review.
> - Terminal gate rejections preserve existing behavior and use `Reviewers: Not assessed` when no source reviewer ran.

Contains: foundry check + fallback table. If foundry not installed: substitute each `foundry:X` with `general-purpose` per table. Agents this skill uses: `foundry:sw-engineer`, `foundry:qa-specialist`, `foundry:perf-optimizer`, `foundry:doc-scribe`, `foundry:linting-expert`, `foundry:solution-architect`, `foundry:challenger`.

**Task hygiene** — task tools may be deferred; load before first use: `ToolSearch(query="select:TaskList,TaskCreate,TaskUpdate,TaskGet", max_results=4)`. Call `TaskList` first and triage each task it returns: `completed` if work clearly done, `deleted` if orphaned, keep `in_progress` only if genuinely continuing. Never spend a turn on bookkeeping alone — every `TaskCreate`/`TaskUpdate` ships in the same response as the next substantive tool call; one exception, `TaskUpdate(completed)` immediately before a long output block (`rules/task-lifecycle.md`).

After Step 1 completes (scope and `TARGET` known), create these tasks **before any agent spawns** (in order, all at once):

- **"Step 1: Identify scope"** — mark `in_progress` at Step 1 start; mark `completed` when `TARGET` and `SCOPE` set and Python file check passes
- **"Step 2: Codex co-review"** — create before Step 2 (skip task if Codex unavailable); mark `in_progress` before Codex spawn; mark `completed` when codex seed extracted (or timed out)
- **"Step 3: Spawn review agents"** — mark `in_progress` before agents launch; mark `completed` when all agent output files collected (or health-monitoring cutoff reached)
- **"Step 4: Cross-validate critical findings"** — mark `in_progress` before verifier spawns; `completed` when all verdicts received; **skip task creation when no critical/blocking findings exist after Step 3**
- **"Step 5: Consolidate findings"** — mark `in_progress` before spawning consolidator; `completed` when consolidator returns its JSON envelope (Write to `review-report.md` done) — **do NOT mark completed for the terminal print, that's a separate task below**
- **"Step 5b: Print report header"** — created **blockedBy** "Step 5: Consolidate findings"; mark `in_progress` immediately after the consolidator returns. Read the report and build the header table and question summary now; keep it `in_progress` through the question. Mark `completed` only in the response that delivers the table in the final reply after the answer (or on a no-question path), beside the last tool calls immediately before that output, never merely when queued/intended.
  - Consolidator's JSON envelope is a routing signal for the orchestrator, never a substitute for reading `$REPORT_DIR/review-report.md` and rendering its header.
  - **The follow-up `AskUserQuestion` must not fire until the current report has been read and its header table and plain summary are built** — the dedicated task tracks final delivery separately; issuing the question does not complete it.

## Flag parsing

Strip flags from `$ARGUMENTS` before using as path:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/extract-keep-flag.py" dev-review "$ARGUMENTS"  # timeout: 5000 — parses --keep, clears stale contract
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_parse_args.py" --skill review --write-files "$ARGUMENTS"
# writes ${TMPDIR:-/tmp}/dev-review-{no-challenge,codemap,clean-args,pr-number}-${CSID} + legacy paths
IFS= read -r REVIEW_ARGS < "${TMPDIR:-/tmp}/dev-review-clean-args-${CSID}" 2>/dev/null || REVIEW_ARGS=""
# strip --keep so it doesn't leak into find/git diff target path
REVIEW_ARGS=$(echo "$REVIEW_ARGS" | sed -E 's/ *--keep +"[^"]+"//' | xargs 2>/dev/null || echo "$REVIEW_ARGS")
IFS= read -r CHALLENGE_ENABLED < "${TMPDIR:-/tmp}/dev-review-challenge-enabled-${CSID}" 2>/dev/null || CHALLENGE_ENABLED="true"
IFS= read -r CHALLENGE_FORCED < "${TMPDIR:-/tmp}/dev-review-challenge-forced-${CSID}" 2>/dev/null || CHALLENGE_FORCED="false"  # --challenge: no-op, challenger always runs
# fail-safe impact default — codemap block overrides with review_impact_tier.py verdict; codemap off keeps FULL
echo "FULL · impact unknown: codemap unavailable" > "${TMPDIR:-/tmp}/dev-review-impact-${CSID}"
IFS= read -r CODEMAP_RAW < "${TMPDIR:-/tmp}/dev-review-codemap-enabled-${CSID}" 2>/dev/null || CODEMAP_RAW="auto"
IFS= read -r FANOUT_FULL < "${TMPDIR:-/tmp}/dev-review-fanout-full-${CSID}" 2>/dev/null || FANOUT_FULL="false"
FANOUT_CAP=3  # runtime form of FANOUT_MAX (<constants> block) — keep both in sync manually if either changes
[ "$FANOUT_FULL" = "true" ] && FANOUT_CAP=0  # 0 = no cap: all preselected dimensions
echo "REVIEW_ARGS=$REVIEW_ARGS CHALLENGE_ENABLED=$CHALLENGE_ENABLED FANOUT_CAP=$FANOUT_CAP"  # prose below branches on these — shell state dies with the call
```

PR#/filename disambiguation gate — reads the classified sentinel, never the raw arguments; branch on the printed values per `<inputs>`:

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PR_NUMBER < "${TMPDIR:-/tmp}/dev-review-pr-number-${CSID}" 2>/dev/null || PR_NUMBER=""
if [ -n "$PR_NUMBER" ]; then
    echo "PR number detected — checking oss plugin availability"
    OSS_ROOT=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/get_plugin_install_path.py" borda-ai-rig oss 2>/dev/null) || OSS_ROOT=""  # install record, never cache glob — orphaned version dir ≠ installed
    [ -n "$OSS_ROOT" ] && [ -f "$OSS_ROOT/skills/review/SKILL.md" ] && OSS_AVAILABLE=true || OSS_AVAILABLE=false
else
    OSS_AVAILABLE=skip
fi
echo "OSS_AVAILABLE=$OSS_AVAILABLE PR_NUMBER=$PR_NUMBER"  # <inputs> prose branches on these — shell state dies with the call
```

**Unsupported flag check** — after all supported flags extracted, scan `$ARGUMENTS` for remaining `--<token>` tokens not in the supported list below. Found → invoke `AskUserQuestion` with question text `` ! Unknown flag(s): `--<token>`. Supported: `--no-challenge`, `--challenge`, `--codemap`, `--no-codemap`, `--worktree`, `--full`, `--keep`. `` — (a) **Abort** (stop, re-invoke with correct flags) · (b) **Continue ignoring** (skip unknown flags, proceed). On Abort: stop.

## Worktree isolation

> loads: worktree-isolation.md

`--worktree` set → run review in an isolated git worktree so no dimension agent can accidentally mutate main sources — **before** Step 1.

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r WORKTREE_ENABLED < "${TMPDIR:-/tmp}/dev-review-worktree-${CSID}" 2>/dev/null; [ "$WORKTREE_ENABLED" = "true" ] || WORKTREE_ENABLED=false
if [ "$WORKTREE_ENABLED" = "true" ]; then python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_load_shared.py" worktree-isolation.md; fi
```

`WORKTREE_ENABLED=true` → follow §Enter (base off HEAD, `EnterWorktree(path=…)`). **Read-only skill** — obey §Deliverable: `$RUN_DIR` (`.temp/`) handoffs stay in the worktree, but `$REPORT_DIR` (the review report) is written to the **main tree** (`$_ORIG_ROOT`, wired at Step 2) so its printed path + follow-up gate stay valid. Reviews committed HEAD state — uncommitted working-tree changes aren't visible, so the default target (no path given) is the last commit (`HEAD~1..HEAD`), matching Agent 0's pre-change ref; pass a path to review other committed files. Else skip — run in main tree (doc not loaded).

```bash
# CODEMAP_RAW → true/false; strict exits on unavailability  # timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# round-trips one sentinel: raw flag in, resolved value out
CODEMAP_ENABLED=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_codemap_gate.py" review) || exit 1
# codemap: integrated-via-shared
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_load_shared.py" codemap-gates.md
```

> loads: codemap-gates.md

Follow Gate A and Gate B.

Use `$REVIEW_ARGS` (not `$ARGUMENTS`) as path for rest of workflow.

## Step 1: Identify scope

Capture the target repository's full Git HEAD once, before source inspection. A file target uses its containing directory; a directory target uses itself; diff mode uses the current directory. This identifies the working-tree base for path/diff reviews; it never claims dirty files equal a clean commit. Keep this frozen value through consolidation and compaction, even if HEAD later moves. Non-Git directories and unborn repositories retain an explicit unavailable reason.

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r REVIEW_ARGS < "${TMPDIR:-/tmp}/dev-review-clean-args-${CSID}" 2>/dev/null || REVIEW_ARGS=""
REVIEW_SOURCE_DIR="."
if [ -d "$REVIEW_ARGS" ]; then REVIEW_SOURCE_DIR="$REVIEW_ARGS"; elif [ -f "$REVIEW_ARGS" ]; then REVIEW_SOURCE_DIR=$(dirname -- "$REVIEW_ARGS"); fi
REVIEWED_COMMIT_SHA=$(git -C "$REVIEW_SOURCE_DIR" rev-parse --verify HEAD 2>/dev/null) || REVIEWED_COMMIT_SHA="unavailable (non-Git directory or unborn HEAD)"
IFS= read -r WORKTREE_ENABLED < "${TMPDIR:-/tmp}/dev-review-worktree-${CSID}" 2>/dev/null || WORKTREE_ENABLED=false
REVIEWED_COMMIT_KIND="working-tree base; reviewed files may include uncommitted changes"
[ "$WORKTREE_ENABLED" = "true" ] && REVIEWED_COMMIT_KIND="committed worktree source"
printf '%s\n' "$REVIEWED_COMMIT_SHA" > "${TMPDIR:-/tmp}/dev-review-commit-sha-${CSID}"
printf '%s\n' "$REVIEWED_COMMIT_KIND" > "${TMPDIR:-/tmp}/dev-review-commit-kind-${CSID}"
printf 'Commit SHA: %s (%s)\n' "$REVIEWED_COMMIT_SHA" "$REVIEWED_COMMIT_KIND"
```

Scope + non-Python impact check + Python filter in ONE pass — a single `git diff` capture feeds TARGET, the report-header warnings, and the early exit (previously eight diff invocations across three blocks):

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r REVIEW_ARGS < "${TMPDIR:-/tmp}/dev-review-clean-args-${CSID}" 2>/dev/null || REVIEW_ARGS=""   # re-derive — bash resets between calls
IFS= read -r WORKTREE_ENABLED < "${TMPDIR:-/tmp}/dev-review-worktree-${CSID}" 2>/dev/null || WORKTREE_ENABLED=false
COMMITTED=""; [ "$WORKTREE_ENABLED" = "true" ] && COMMITTED="--committed"  # fresh worktree: working-tree diff is empty, the change is the last commit
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/resolve_review_target.py" $COMMITTED -- "$REVIEW_ARGS" || exit 1
```

Any `⚠` line printed above: include in report header regardless of whether Python files exist. No Python files → `resolve_review_target.py` prints `! Diff contains non-Python files only.`; a nonexistent path prints `! Review target not found: <path>`; either exits 1 and the `|| exit 1` stops Step 1 there — do not continue.

### Scope pre-check

Before spawning agents, classify diff:

- Count files changed, lines added/removed, new classes/modules introduced
- Classify: **FIX** (corrects wrong behavior), **REFACTOR** (same behavior restructured), **FEATURE** (new public API or capability), **CHORE** (config/deps, no logic), **MIXED** — classify by intent, not file count
- **Complexity smell**: 8+ files changed → note in report header

Persist the classification decision for compaction-safe reuse in later steps and the boundary-1 contract. Closed set, no shell variable — run the ONE block below matching the classification just made:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo "FIX" > "${TMPDIR:-/tmp}/dev-review-classification-${CSID}"
```

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo "REFACTOR" > "${TMPDIR:-/tmp}/dev-review-classification-${CSID}"
```

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo "FEATURE" > "${TMPDIR:-/tmp}/dev-review-classification-${CSID}"
```

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo "CHORE" > "${TMPDIR:-/tmp}/dev-review-classification-${CSID}"
```

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo "MIXED" > "${TMPDIR:-/tmp}/dev-review-classification-${CSID}"
```

Skip optional agents by classification:

- FIX → **only when `IMPACT_TIER=LIGHT`** (see Impact tier below; `FULL` keeps both): skip Agent 3 (perf-optimizer) and Agent 6 (solution-architect), unless diff changes an already-exported public function's signature (added/removed/renamed params, new flags) — then Agent 6 still runs per its own trigger (Agent 1's "API-consistency audit" subsection already checks the surface; Agent 6 adds design-quality/backward-compat judgment)
- REFACTOR → skip Agent 6 (solution-architect), same exception — an already-exported public function's signature change still fires Agent 6
- CHORE (config/deps, no logic) → skip Agent 2 (qa-specialist), Agent 3 (perf-optimizer), Agent 6 (solution-architect); keep Agent 1 (sw-engineer), Agent 4 (doc-scribe), Agent 5 (linting-expert). No logic = no test-gap, perf, or architecture surface — same saving pattern as `oss:review`'s DOCS_TYPING/TESTS_CI pre-classification (cc_oss/skills/review/SKILL.md:182,198).
- FEATURE/MIXED → spawn all agents, plus Agent 0 (blind-solve) when a matching `.plans/active/*.md` exists — see §Agent 0 below
- **Challenger at any diff size** — Agent 7 runs on every classification and every diff size; only `--no-challenge` (`CHALLENGE_ENABLED=false`) disables it. Small diff ≠ low risk: few lines in a private helper on the main path can break the main user story.

**Impact tier** — review depth follows the code path a change sits on, never line or file count. `review_impact_tier.py` (codemap `diff-impact` + `fn-blast`, run in the codemap block below) prints `FULL` or `LIGHT` plus reason:

- `FULL` — any changed symbol, public or private, reaches a public function outside tests/leaf modules (main path), or a changed main-path module has ≥5 importers, or a module-level main-path change. **Unknown impact (codemap off, query failed, unmapped file) = `FULL`.**
- `LIGHT` — every change and every transitive caller stays in tests, leaf modules (`viz`, `plot`, `examples`, `docs`, `scripts`, `notebooks`), or uncalled private code.

The Step 2 codemap-copy block prints `IMPACT_TIER=` and `IMPACT=` — apply the FIX skip above from that value (codemap runs after classification). Report the `IMPACT=` line in the review header as `Impact:`.

### Structural context + review pre-flight (codemap-py — only if `CODEMAP_ENABLED=true`)

**Skip entire section if `CODEMAP_ENABLED=false`** — sets `codemap_available=false` for downstream agent prompts; agents fall back to file reads.

Extended scan for changed modules — runs v5 pre-flight queries per module, persists structured output to `$RUN_DIR/codemap-context.md`, and sets `codemap_available` flag for Step 3 agent spawns:

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
        _DIFF_IMPACT_JSON=$(codemap-py query --timeout 15 diff-impact 2>/dev/null)
        printf '%s\n' "$_DIFF_IMPACT_JSON"
        # review depth from code path, not diff size — overrides Step 0's fail-safe FULL default; stdout to sentinel, not stage
        printf '%s\n' "$_DIFF_IMPACT_JSON" | python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/review_impact_tier.py" > "${TMPDIR:-/tmp}/dev-review-impact-${CSID}" 2>/dev/null  # timeout: 300000 — batch + diff-impact + ≤12 tier fn-blast queries
    } > "$CODEMAP_CONTEXT_STAGE"
fi
echo "$codemap_available" > "${TMPDIR:-/tmp}/dev-review-codemap-available-${CSID}"
```

Codemap context propagation in Step 3:

- `codemap_available=true` → copy `$CODEMAP_CONTEXT_STAGE` to `$RUN_DIR/codemap-context.md` once `$RUN_DIR` exists (Step 2). Each dimension spawn prompt gets a literal block holding only ITS slice of the batch results (the whole block in every prompt re-bills every query result to consumers that never read it; results for skipped agents injected nowhere): qa unit → `uncovered` + `mock-rdeps` entries; doc unit → `undocumented` + `xrefs --broken` entries; sw-engineer → `rdeps` + `central` + `diff-impact`. Entries whose only consumer was skipped by classification aren't injected anywhere:
  ```text
  ## Structural Context (codemap-py, codemap_available=true)
  <this agent's slice of $RUN_DIR/codemap-context.md>

  Read this section first. The payload contains a `batch` array: each entry has `cmd`, `ok`, an ordinal `index`, and `result`. Each child's `result.index` describes its own scope and completeness; the outer `index` is only a conservative summary. Retain child metadata when slicing results for agents. For successful entries passing the reuse gate, skip only equivalent structural retrieval. A valid empty list is not a failed query; failed or unjoinable children remain explicit gaps. Source and test reads for specific findings remain required.

  > Reuse gate: reuse a supplied answer only for the same project, current index, target, query and flags; skip its duplicate pre-flight call. Require success and direction-complete metadata. For batch children require `ok: true` and inspect `result.index`; `ok: false` is a failure, never an empty answer. Missing metadata, `stale`, root mismatch, degraded or incomplete results need targeted fallback. Use legacy `exhaustive: true` only when `query_complete` is absent. A valid empty list settles that scoped query; truncation does not enumerate all matches. Necessary source-body reads, test-quality checks, dynamic behavior and required independent verification remain allowed.

  **Bounded call budget**: symbol not covered by the batch above → up to 3 additional `codemap-py query` calls this review pass. **Hard stop on `query_complete: true`** (legacy `exhaustive: true` only when `query_complete` is absent): a result passing the reuse gate is final for its direction — no follow-up Grep/Read/query to re-confirm it.

  Per-agent priority (skip redundant reads for symbols the listed query already covers):
  - qa-specialist (Agent 2): `uncovered` + `mock-rdeps` first
  - doc-scribe (Agent 4): `undocumented` + `xrefs --broken` first
  - sw-engineer (Agent 1): `rdeps` first (importers per changed module)
  - challenger (Agent 7): unchanged — always reads source directly
  ```
- `codemap_available=false` → omit block; agents proceed with current file-read behaviour.

> Per-agent consumption guidance kept in sync with `$_DEV_SHARED/codemap-context.md` §Review-pipeline injection — update both on change.

Tier annotation for Agent 1 (sw-engineer) only: label each module's `imported_by` count — **high risk** (>20), **moderate** (5–20), **low** (\<5). Agent 1 uses this to prioritize: high `imported_by` modules warrant deeper scrutiny on API compatibility, error handling, behavioural correctness — downstream callers outside diff otherwise invisible.

## Step 2: Codex co-review

Set up run directory, check Codex availability, and materialize codemap context into the run directory:

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
CODEX_STATUS=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/check_bridge.py" --status 2>/dev/null || echo "absent")  # timeout: 5000
[ "$CODEX_STATUS" = "available" ] && echo "bridge@borda-ai-rig available" || echo "⚠ bridge@borda-ai-rig is ${CODEX_STATUS} — skipping co-review"
IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/dev-review-run-dir-${CSID}" 2>/dev/null || RUN_DIR=""  # round-trip the sentinel agents self-resolve from
[ -n "$RUN_DIR" ] || { echo "! BLOCKED — run-dir sentinel empty; refusing to copy codemap context to a root-relative path"; exit 1; }
IFS= read -r codemap_available < "${TMPDIR:-/tmp}/dev-review-codemap-available-${CSID}" 2>/dev/null || codemap_available="false"
if [ "$codemap_available" = "true" ] && [ -f "${TMPDIR:-/tmp}/dev-review-codemap-context.md-${CSID}" ]; then
    cp "${TMPDIR:-/tmp}/dev-review-codemap-context.md-${CSID}" "$RUN_DIR/codemap-context.md"
fi
# review depth by code path — gates FIX perf+arch skip; missing sentinel = FULL
IFS= read -r IMPACT_LINE < "${TMPDIR:-/tmp}/dev-review-impact-${CSID}" 2>/dev/null || IMPACT_LINE="FULL · impact unknown"
echo "IMPACT_TIER=${IMPACT_LINE%% *}"
echo "IMPACT=$IMPACT_LINE"
```

If Codex available:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/dev-review-run-dir-${CSID}" 2>/dev/null || RUN_DIR=""  # re-derive — bash resets between calls
[ -n "$RUN_DIR" ] || { echo "! BLOCKED — run-dir sentinel empty; refusing to write a root-relative codex-out path"; exit 1; }
CODEX_OUT="$RUN_DIR/codex.md"
echo "$CODEX_OUT" > "${TMPDIR:-/tmp}/dev-review-codex-out-${CSID}"  # Step 6 re-reads — bash state lost
```

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_load_shared.py" codex-prepass.md  # timeout: 5000
```

Use the loaded `codex-prepass.md` for the **skip/run criteria only** (formatting-only skip, availability check) — do NOT use its dispatch line as the call: its args name no output path, so `$RUN_DIR/codex.md` would never be written and Step 6 would silently skip.

Dispatch — substitute `<TARGET>` with its resolved literal value before the call: `Skill(skill="bridge:review", args="Read-only adversarial review of <TARGET>. Look for bugs, missed edge cases, incorrect logic, and inconsistencies with existing code patterns. Report findings with file:line locations; do not apply fixes.")` (requires `bridge@borda-ai-rig`).

Note: `bridge:review` is a foreground, read-only skill with no Write tool — it returns findings inline in its response envelope and never creates a file itself, unlike a background `Agent()` spawn. No health-monitoring/liveness-probe protocol applies to this call: it blocks until it returns. After `Skill(skill="bridge:review", ...)` returns, the orchestrator itself writes `$RUN_DIR/codex.md` from the returned envelope's `.findings`/`.verdict`/`.transcript_path` fields.

After `$RUN_DIR/codex.md` is written (or skipped/blocked), extract compact seed list (≤10 items, `[{"loc":"file:line","note":"..."}]`) to inject into agent prompts in Step 3 as pre-flagged issues to verify or dismiss. Codex skipped, timed out, or found nothing → proceed with empty seed.

**Cap-disclosure**: count total Codex findings before truncating. If ≥10, surface in consolidated report header:

```text
Codex: first 10 items seeded to review agents; full list in $RUN_DIR/codex.md (N total) — review codex.md directly for complete coverage.
```

Pass notice through to consolidator (Step 5) so it appears in final report header, not just terminal scratch.

### Agent 0 — blind-solve (FEATURE/MIXED with a matching plan file only, general-purpose)

Anti-anchoring pre-step: before any agent reads the diff, spawn a standalone agent that derives its own **blueprint-level** solution to the problem the change solves — approach + key data structures + edge cases, not full implementation. Bounded: ~10 tool calls, output ≤1 page. Isolation-motivated spawn, not work displacement — the blind agent must not share the orchestrator's context, so the "under ~73 calls → inline" rule does not apply; it still pays the fixed per-spawn overhead, hence the double gate below.

Problem statement = a matching plan file, nothing else. A local diff has no PR body; the last commit message describes the *previous* change when the target is the working tree, so it is never used. Gather with this block — no diff content is read:

```bash
# timeout: 10000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/dev-review-run-dir-${CSID}" 2>/dev/null || RUN_DIR=""
[ -n "$RUN_DIR" ] || { echo "! BLOCKED — run-dir sentinel empty; refusing to write blind-solve input to a root-relative path"; exit 1; }
IFS= read -r WORKTREE_ENABLED < "${TMPDIR:-/tmp}/dev-review-worktree-${CSID}" 2>/dev/null || WORKTREE_ENABLED=false
# pre-change ref: working-tree diff → HEAD is untouched; --worktree reviews committed HEAD → parent is the pre-change state
if [ "$WORKTREE_ENABLED" = "true" ]; then PRE_REF=HEAD~1; CHANGED=$(git diff --name-only HEAD~1 HEAD 2>/dev/null); else PRE_REF=HEAD; CHANGED=$(git diff --name-only HEAD 2>/dev/null); fi
STEMS=$(printf '%s\n' $CHANGED | sed -E 's#.*/##; s#\.py$##; s#^test_##' | sort -u | grep -v '^__init__$' | head -20)
PLAN=""; for f in .plans/active/*.md; do [ -f "$f" ] || continue; for s in $STEMS; do grep -qi -- "$s" "$f" 2>/dev/null && { PLAN="$f"; break 2; }; done; done
if [ -n "$PLAN" ]; then
  { echo "## Plan: $PLAN"; sed -n '1,120p' "$PLAN"; echo; echo "## Changed files (names only)"; printf '%s\n' $CHANGED; echo; echo "PRE_REF=$PRE_REF"; } > "$RUN_DIR/blind-solve-input.md"
  echo "blind-solve input: $RUN_DIR/blind-solve-input.md plan=$PLAN pre-ref=$PRE_REF"
else
  echo "blind-solve: skipped — no .plans/active/*.md mentions a changed module ($(printf '%s ' $STEMS))"
fi
```

`blind-solve: skipped` → no spawn; note the skip in the report header. Otherwise spawn with the contents of `$RUN_DIR/blind-solve-input.md` verbatim, then: "Your only source for pre-change code is `git show <PRE_REF>:<path>` for files listed above. Never use Read, `cat`, `git diff`, or any working-tree path — the working tree contains the change under review. Sketch your own solution to the stated problem — approach, key data structures/functions, edge cases — in ≤1 page. Do not write full code." Write to `$RUN_DIR/blind-solve.md`, return `{"status":"done","file":"$RUN_DIR/blind-solve.md","confidence":0.N}`.

Skip when classification is FIX/REFACTOR/CHORE (no fresh problem to solve blind) — checked before the block above runs. Launch in the same batch as the Step 3 spawn units — it needs no diff, nothing blocks it. Not a `FANOUT_MAX` unit — never counted against the cap, never ranked out, not part of any merged spawn unit.

## Step 3: Spawn sub-agents in parallel

**Spawn-count gate — apply before spawning anything.** Each agent costs ~120,851 tok fixed overhead regardless of how little work it does (~73 tool-calls' worth), plus ~12.0 s/call. Rules, all mandatory:

Two stages, in order — never collapse them:

1. **Classification preselection** (always): the FIX (at `IMPACT_TIER=LIGHT`) / CHORE skips above decide which dimensions are *relevant at all*. A dimension with no changed file in its territory is out here and never comes back, at any flag.
2. **Unit grouping** (always): fold the survivors into spawn units per §Merged spawn units below — Agents 3+6 share one spawn, Agents 4+5 share one spawn, Agents 1 and 2 stay standalone. A unit exists when at least one of its dimensions survived stage 1; its prompt lists only the surviving dimensions' brief files.
3. **Relevance ranking** (default only): rank the units by evidence — changed files and lines in their dimensions' territory, what the classification implies, what the structural context flagged (a merged unit ranks by its strongest surviving dimension) — and spawn the top `FANOUT_MAX` (3) units. With `--full` (`FANOUT_CAP=0`) skip this stage and spawn every unit of stage 2.

- More work → give each agent more, never add agents.
- **Spawn the fewest that keep each near `AGENT_CALL_BUDGET`** — not the most the cap allows. Small diff → merge surviving units into fewer spawns, **never zero**.
- **Diff size never licenses inline review.** ~73-call inline rule covers work-displacement spawns only; review specialists are role-isolated (own system prompt, independent eyes), so it does not apply here. Small diff ≠ low risk: few-line change on hot path often riskiest. Orchestrator never substitutes itself for a unit that survived stages 1–3.
- **Merge before you split**: two dimensions whose files overlap go to one agent, not two — the fixed pairs below are the floor, not the ceiling.
- Every spawn prompt states the budget, requires an envelope even on exhaustion — `partial: true` plus what was finished.
- Dimensions dropped by the cap are listed in the report; never silently skipped.

### Merged spawn units (shared pattern with oss:review — each spawn costs ~120,851 tok fixed overhead, so paired dimensions share one spawn)

- **Agents 3+6 = ONE `foundry:perf-optimizer` spawn** covering Performance + Architecture/API design — spawn when either dimension survives classification preselection; prompt lists only the surviving dimensions' brief files (e.g. FIX with a public-signature change spawns this unit with only the Agent-6 dimension active). (this merged spawn is explicitly authorized to also cover architecture/SOLID review; its own NOT-for line does not apply to this dispatch)
- **Agents 4+5 = ONE `foundry:doc-scribe` spawn** covering Documentation + Linting — same rule. (this merged spawn is explicitly authorized to also cover linting; its own NOT-for line does not apply to this dispatch)
- Agents 1 (sw-engineer, incl. security augmentation), 2 (qa-specialist), 7 (challenger) stay standalone spawns.
- A merged spawn writes **one file per covered dimension**, each with its OWN full sections + Confidence block — never blended: `perf-optimizer.md` + `solution-architect.md`, or `doc-scribe.md` + `linting-expert.md`. Downstream contracts (consolidator filename list, Step-4 cross-validation "same type as origin", report sections) key on those files, stay unchanged.
- Merged-spawn envelope = JSON array, one element per dimension file, same per-element schema as the standard envelope. Element absent from the array ⇒ that dimension gets the ⏱ marker — never silently omitted.

**File-based handoff** — load the protocol, then resolve the develop:review checklist path (version-agnostic) and the dimension-briefs directory (`$REVIEW_CHECKLIST` rules follow the preamble and pre-expansion rule below):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_load_shared.py" file-handoff-protocol.md  # timeout: 5000
# loads: the brief-*.md files in the loop — each child cats only its own; parent passes the directory path only
_DIMENSION_BRIEFS="${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/skills/review/templates"
for _B in brief-sw-engineer.md brief-qa-specialist.md brief-perf-optimizer.md brief-doc-scribe.md brief-linting-expert.md brief-security-augmentation.md brief-solution-architect.md brief-challenger.md; do
    [ -f "$_DIMENSION_BRIEFS/$_B" ] || { echo "! BLOCKED — dimension brief $_B missing; restore/reinstall this develop version before spawning review agents"; exit 1; }
done
echo "Dimension briefs: $_DIMENSION_BRIEFS"
REVIEW_CHECKLIST="$_DIMENSION_BRIEFS/review-checklist.md"  # develop's own copy — never read from the oss plugin
if [ -f "$REVIEW_CHECKLIST" ]; then
    echo "Checklist: $REVIEW_CHECKLIST"
else
    echo "⚠ review checklist unavailable — review will proceed without severity anchors; restore/reinstall this develop version for full coverage"
    REVIEW_CHECKLIST=""
fi
```

Run directory created in Step 2 (`$RUN_DIR`).

<!-- $REPORT_DIR pre-expanded into $REPORT_DIR_LITERAL — substitute literal vars in Agent spawn prompt strings. Run-dir is the exception: agents self-resolve it (see run-dir preamble below), never hand-substituted. -->

### Run-dir preamble (canonical)

Place this in every agent spawn prompt (Agents 1–7 and the Step 5 consolidator) as the paragraph **after** prompt line 1 — line 1 is the delta task line (dimension + target, ≤12 words, delta first; `agent-spawn.md`), never this shared boilerplate:

> "First run Bash `CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"; IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/dev-review-run-dir-${CSID}" 2>/dev/null || RUN_DIR=""` to obtain the exact run-dir path. Use `$RUN_DIR` verbatim for every file you read or write — never retype the path literally (the leading `.` in `.temp` is easy to drop, scattering output into a stray `temp/` dir)."

Inside agent prompt strings, leave `$RUN_DIR` literal — agent resolves it via preamble. Orchestrator must NOT hand-substitute run-dir path.

### $VAR_LITERAL pre-expansion rule (canonical)

Any OTHER shell variable inserted into an Agent spawn prompt string — `$REPORT_DIR_LITERAL`, `$REVIEW_CHECKLIST`, `$DATE`, `$_REVIEW_TEMPLATE` — must be substituted with its literal resolved value **before** building the Agent call. Bare variable name inside a quoted Agent prompt will NOT expand — spawned agent receives literal dollar-sign text, causing path mismatches. Resolve each to its value first; never pass a bare `$VAR` name. (`$RUN_DIR` is the deliberate exception above — agents self-resolve it.)

Pass the resolved `$REVIEW_CHECKLIST` path (printed by the File-based handoff block above) as a `REVIEW_CHECKLIST=<resolved path>` binding line in Agent 1's spawn prompt and in the consolidator's. **Empty → omit that binding line from Agent 1's prompt entirely** — don't pass empty path.

**Visible-degradation rule** — `$REVIEW_CHECKLIST` empty → print `⚠ REVIEW_CHECKLIST is empty — review scope undefined` at TOP of review output (before Findings), and consolidator prompt (Step 5) **must** insert into report header (YAML `---` block or first line before Findings): "Review checklist not applied (review-checklist.md missing from this develop install) — severity anchors may be inconsistent." Silent degradation hides the gap from reviewers, makes severity drift invisible.

**Finding evidence standard — applies to every agent, every finding** (tiers, source independence and citation tracing per `quality-gates.md` §Evidence Grounding):

- Every finding must cite specific `file:line` from diff as primary evidence — "I know this typically causes issues" without a diff citation isn't a valid finding
- Claims referencing external standards (OWASP, PEP, language spec, CVE) must cite the specific authoritative document — official spec, CVE entry, PEP text; a blog post or Stack Overflow answer referencing the standard is Tier 2 only
- Only Tier 2 available, distinct-origin count < 3, no experiment run → downgrade finding to LOW or drop it; never raise MEDIUM/HIGH/CRITICAL on Tier 2 alone

Launch spawn units simultaneously with Agent tool (security augmentation folded into Agent 1 — not separate spawn; Agent 6 optional; Agents 3+6 and 4+5 launch as merged units per §Merged spawn units; Agent 0 blind-solve joins this batch on FEATURE/MIXED, outside the cap). Every agent prompt opens with its delta task line, carries the [run-dir preamble (canonical)](#run-dir-preamble-canonical) next, and ends with:

> "Write your FULL findings (all sections, Confidence block) to `$RUN_DIR/<agent-name>.md` using the Write tool — where `<agent-name>` is e.g. `sw-engineer`, `qa-specialist`, `perf-optimizer`, `doc-scribe`, `linting-expert`, `solution-architect`. Then return to the caller ONLY a compact JSON envelope on your final line — nothing else after it (a merged unit returns the JSON array of §Merged spawn units instead, one such object per dimension file): `{\"status\":\"done\",\"findings\":N,\"severity\":{\"critical\":0,\"high\":1,\"medium\":2,\"low\":0},\"file\":\"$RUN_DIR/<agent-name>.md\",\"confidence\":0.88}`"

**Codemap-py context preamble (substituted by orchestrator)**: rehydrate `IFS= read -r codemap_available < "${TMPDIR:-/tmp}/dev-review-codemap-available-${CSID}" 2>/dev/null || codemap_available=false`. When `codemap_available=true`, each dimension spawn prompt is prefixed with its `## Structural Context (codemap-py, codemap_available=true)` slice from `$RUN_DIR/codemap-context.md` per the per-agent slicing rules in Step 1. Agents must read that block first and skip redundant Grep/Read on symbols already covered by codemap output. Block absent → fall back to current file-read behaviour. Challenger (Agent 7) unchanged.

**Dimension briefs** — each dimension's full instructions live in its own file under `templates/` (directory printed above as `Dimension briefs: …`): Agent 1 → `brief-sw-engineer.md` (with its API-consistency audit and error-path table), Agent 2 → `brief-qa-specialist.md`, Agent 3 → `brief-perf-optimizer.md`, Agent 4 → `brief-doc-scribe.md`, Agent 5 → `brief-linting-expert.md`, Security augmentation → `brief-security-augmentation.md`, Agent 6 → `brief-solution-architect.md`, Agent 7 → `brief-challenger.md`. Do NOT cat, Read or inline them into orchestrator context or the Agent prompt; the child loads them. After the run-dir preamble each spawn prompt carries one load instruction — a single Bash `cat` of `<DIMENSION_BRIEFS_LITERAL>/<brief file>` for each surviving dimension of that spawn unit (Agents 3+6 and 4+5 list only their survivors; Agent 1 adds the security brief when augmentation is dispatched), followed by "follow ONLY these briefs" — then the literal bindings (`REVIEW_CHECKLIST=<resolved path>` for Agent 1, line omitted when empty; bindings are values, never text to execute), then the findings-write instruction above. A `§Merged spawn units` mention inside a brief only describes how this orchestrator grouped dimensions; the prompt names the briefs to follow. `DIMENSION_BRIEFS_LITERAL` is the resolved path, never a bare `$_DIMENSION_BRIEFS`.

Dispatch triggers stay here — the orchestrator decides before any child reads the file:

- **Security augmentation** (folds into Agent 1, never a separate spawn): target touches authentication, user input handling, dependency updates, or serialization → add the security brief to Agent 1's brief list. Skip for purely internal refactoring.
- **Agent 6** (optional, folds into the Agents 3+6 unit): target touches `__init__.py` exports, adds/modifies Protocols or ABCs, changes module structure, introduces new public classes, or changes the signature of any already-exported public function (added/removed/renamed params, new flags) → include the Agent 6 brief. Skip for purely internal (non-exported) implementation changes.
- **Agent 7** (challenger): skip only if `CHALLENGE_ENABLED=false`; runs at any diff size.

**Challenger severity propagation**: consolidator (Step 5) reads `challenger.md` → map its findings by section before merging — Blockers (`[CRITICAL]`) → `critical` or `high` (promote to `critical` only when independently corroborated, e.g. a Step 4 CONFIRMED verdict or matching evidence from another agent; otherwise keep the required independent verification open, never downgrade uncertainty into actionable high severity), Concerns (`[HIGH]`) → `medium`, Nitpicks (`[LOW]`) → `low`. Never drop challenger candidates from validation; only parent-CONFIRMED claims become findings, and uncertainty stays separately durable.

**Health monitoring**: arm batch `review` in the spawn response — deliverable `$RUN_DIR/<agent-name>.md` per dimension file (a merged unit gets one row per file); Step 4 verifiers arm `verify`, the Step 5 consolidator arms `consolidate` (deliverable `-`); run the check block once per wake-up (`agent-resolution.md` §Agent waits — no polling, per-agent deadlines). Agent calls run in background. Spawn the batch, end the turn, resume on each completion notification — no filler tool calls, no "waiting" turns, no sleep, and never `ScheduleWakeup`, `ListAgents`, or a `Monitor` loop: the notification is the only resume signal (`agent-resolution.md` §Agent waits — no polling). An agent whose completion or idle notification arrives while `$RUN_DIR/<agent-name>.md` is still missing is ⏱ `timed_out` at once — never wait on it further. Agent returns partial results or errors → use Read tool on `$RUN_DIR/<agent-name>.md` for details. Mark agents that returned empty or error with ⏱ in final report. Never silently omit agents that **failed** (returned error/partial) — must appear with ⏱ marker. Agents **not spawned** (skipped due to mode flags, docs-only, CHORE mode) may be absent from RUN_DIR; consolidator "skip missing" applies only to legitimately-not-spawned agents.

## Step 4: Validate all candidates and independently verify critical/blocking concerns

Agent phase reports must be written to the run-dir before this refresh.

```bash
# compaction boundary 1 (compaction-contract.md §Lifecycle) — written before entering Step 4
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _RUN_DIR < "${TMPDIR:-/tmp}/dev-review-run-dir-${CSID}" 2>/dev/null || _RUN_DIR=""
IFS= read -r _REPORT_DIR < "${TMPDIR:-/tmp}/dev-review-report-dir-${CSID}" 2>/dev/null || _REPORT_DIR=""
IFS= read -r _TARGET < "${TMPDIR:-/tmp}/dev-review-clean-args-${CSID}" 2>/dev/null || _TARGET="working-tree diff"
IFS= read -r _CLASS < "${TMPDIR:-/tmp}/dev-review-classification-${CSID}" 2>/dev/null || _CLASS=""
_FINDING_FILES=$(ls "$_RUN_DIR/"*.md 2>/dev/null | tr '\n' ' ' | sed 's/ *$//')
_PRESERVE="run-dir=$_RUN_DIR, report-dir=$_REPORT_DIR, target=$_TARGET, classification=$_CLASS, finding-files=$_FINDING_FILES"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/write_skill_contract.py" --keep-sentinel dev-review-keep-items "develop:review" "consolidation (after parallel review-agent fan-out)" "$_RUN_DIR" "$_PRESERVE" "resume: re-cat own agent-resolution.md, file-handoff-protocol.md and foundry--cross-validation-protocol.md; cross-validate critical findings (Step 4) → consolidate → final report" -- calls_ahead=20 "drop=expanded review findings"  # timeout: 5000
IFS= read -r _DEV_SHARED < "${TMPDIR:-/tmp}/dev-shared-${CSID}" 2>/dev/null || _DEV_SHARED=""
[ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/compaction_boundary.py" "$_DEV_SHARED"
RUN_DIR="$_RUN_DIR"
[ -n "$RUN_DIR" ] || { echo "! BLOCKED — run-dir sentinel empty; refusing to write cross-validation notes to a root-relative path"; exit 1; }
if [ ! -f "$_DEV_SHARED/foundry--cross-validation-protocol.md" ]; then
    echo "⚠ foundry--cross-validation-protocol.md not found at $_DEV_SHARED — Step 4 skipped; required validation is incomplete. It ships in this plugin — reinstall develop@borda-ai-rig."
    echo "## Cross-Validation: SKIPPED" >> "$RUN_DIR/cross-validation.md"
    echo "**Reason**: _DEV_SHARED unavailable — cross-validation protocol not executed." >> "$RUN_DIR/cross-validation.md"
else
    cat "$_DEV_SHARED/foundry--cross-validation-protocol.md"
fi
```

Follow the Boundary step printed above before candidate validation and consolidation; reports are the preserved finding-files in the run-dir.

File present → parent validates all candidates using the printed protocol before consolidation. File absent → record incomplete process coverage, owner and reinstall recovery; no unconditional approval or unvalidated actionable findings. Exhaust allowed static checks. A child read-only limit does not establish that authorized parent runtime checks are infeasible; record the actual authorization/capability boundary and next check when blocked, and feasible undone work as incomplete.

**Skill-specific**: use **same agent type** that raised finding as verifier (e.g. foundry:sw-engineer verifies foundry:sw-engineer's critical finding). **Spawn cap: max 3 verifier agents** — critical/blocking findings > 3 → group into batches of ≤2 findings per verifier (same origin type per batch); note grouped IDs in rationale; each finding still gets its own verdict. Same cap as the shared protocol and oss:review — unbounded verifier fanout costs ~120,851 tok per critical finding.

Before consolidation, persist the parent's all-severity candidate validation and reviewer assessment evidence in `$RUN_DIR/cross-validation.md` with the Write tool. Include structured envelope fields defined by the loaded protocol and retain all dispositions. Pass that literal evidence path to the consolidator. Parent rechecks new/materially strengthened claims and the final envelope before delivery; a model's confirmation label alone is not proof. Non-empty `new_claims` in the consolidator envelope → validate each listed candidate ID against the source before delivery; any still unvalidated stays out of Findings and is listed under the report's open concerns.

## Step 5: Consolidate findings

Resolve the report date — the consolidator's `DATE` binding (the report directory already exists from the Step 2 run-dir block):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r REVIEWED_COMMIT_SHA < "${TMPDIR:-/tmp}/dev-review-commit-sha-${CSID}" 2>/dev/null || REVIEWED_COMMIT_SHA="unavailable (capture missing)"
IFS= read -r REVIEWED_COMMIT_KIND < "${TMPDIR:-/tmp}/dev-review-commit-kind-${CSID}" 2>/dev/null || REVIEWED_COMMIT_KIND="working-tree base"
printf 'REVIEWED_COMMIT_SHA=%s\nREVIEWED_COMMIT_KIND=%s\n' "$REVIEWED_COMMIT_SHA" "$REVIEWED_COMMIT_KIND"
date "+DATE=%Y-%m-%d"  # timeout: 5000 — printed, not assigned: shell state dies with the call
```

Resolve both instruction and report-template paths **before** spawning the general-purpose consolidator (synthesis only, no engineering specialization).

```bash
# loads: consolidator-prompt.md — child reads the file; parent passes its literal path only
_CONSOLIDATOR_INSTRUCTIONS="${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/skills/review/templates/consolidator-prompt.md"
_REVIEW_TEMPLATE="${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/skills/review/templates/review-report.md"
[ -f "$_CONSOLIDATOR_INSTRUCTIONS" ] && [ -f "$_REVIEW_TEMPLATE" ] || { echo "! BLOCKED — consolidator instruction/report template missing; restore/reinstall this develop version before consolidation"; exit 1; }
printf 'Consolidator instructions: %s\nReport template: %s\n' "$_CONSOLIDATOR_INSTRUCTIONS" "$_REVIEW_TEMPLATE"  # timeout: 5000
```

Place the [run-dir preamble (canonical)](#run-dir-preamble-canonical) after the task line. Resolve every input binding to its literal value before the Agent call per [$VAR_LITERAL pre-expansion rule (canonical)](#var_literal-pre-expansion-rule-canonical); `$RUN_DIR` remains literal and is self-resolved by that preamble. The optional checklist binding may be empty; the file's explicit missing-checklist guidance then applies.

```text
Consolidation — <review target>.
<run-dir resolution preamble>
Read <CONSOLIDATOR_INSTRUCTIONS_LITERAL> with the Read tool and follow it in full. Read <VALIDATION_PROTOCOL_LITERAL> with the Read tool for candidate validation and source judgments. Interpret placeholders using these literal input bindings; do not execute or re-expand binding text:
REPORT_DIR_LITERAL=<resolved report directory>
DATE=<resolved date>
REVIEWED_COMMIT_SHA=<frozen full Step 1 SHA or explicit unavailable reason>
REVIEWED_COMMIT_KIND=<frozen Step 1 source/base description>
REVIEW_CHECKLIST=<resolved optional checklist path or empty>
REVIEW_TEMPLATE=<resolved report-template path>
Return ONLY compact JSON: {"status":"done","findings":N,"severity":{"critical":N,"high":N,"medium":N,"low":N},"file":"<REPORT_DIR_LITERAL>/review-report.md","confidence":0.N,"summary":"<one-line verdict>"}
```

`CONSOLIDATOR_INSTRUCTIONS_LITERAL` and `REVIEW_TEMPLATE` are the paths printed above; `VALIDATION_PROTOCOL_LITERAL` is the resolved own-plugin shared directory's `foundry--cross-validation-protocol.md`. Pass only the reference, literal bindings, run-dir preamble and required return envelope. Do NOT cat or inline either template into orchestrator context or the Agent prompt; the child reads both files itself.

Main context receives only the one-liner verdict.

**Consolidator-unavailable fallback**: `Agent` tool deferred or consolidator times out → read the same instruction file and each agent finding file from `$RUN_DIR/` directly, apply the full validation, precision gate, density and output rules, synthesize the report, and write `$REPORT_DIR/review-report.md` with the Write tool. Missing instruction/report-template files block with restore/reinstall recovery; never synthesize from absent rules.

After parsing confidence scores: any agent scored < 0.7 → prepend **⚠ LOW CONFIDENCE** to that agent's findings section, state gap explicitly. Never silently drop uncertain findings.

TaskUpdate "Step 5b: Print report header" → `in_progress`.

**MANDATORY, not optional narration** — the consolidator's returned JSON envelope is a routing signal only; it is never printed to the user and never satisfies this step. Prepare items 1–3 in this exact order, in this same turn, before any other Step 5/6 output; deliver items 4–5 in the final response:

1. read the top of `$REPORT_DIR/review-report.md` via the Read tool with `limit=60` — the `---` header through its closing delimiter, plus the legend, aggregate summary and findings overview below it; never a bound that stops inside the header, and never the whole file (the detailed finding sections are 5–20K tok and are read per item, later, on demand). A report whose overview runs past that bound: re-read with `offset` from the last line delivered, never the whole file.
2. render its fields as a two-column Markdown table (`Field | Value`, one row per key, file order) per quality-gates.md §Report File Format — never the raw `---`-delimited block.
3. complete the delivery block: the reviewer legend immediately below the table, then the aggregate summary as prose and limits, kept short: 2–3 sentences, each critical/high finding on one line, limits as at most 3 bullets. The table is the required core — its `Path` row points at the full report, where the findings overview with its Author column and the detailed review content stay; end with `→ saved to $REPORT_DIR/review-report.md`.
4. Deliver the block once in the final reply after the answer, after its last tool call, never as Bash/tool stdout. Do not print the header table before the follow-up question or put it in option previews. The question carries only the plain summary defined below; the full table stays in the report file until final delivery. On a no-question path, the block still opens the final reply after all Step 6/7/8 tool calls that apply. The `Stop` hook requires the matching full header table there; the question summary does not satisfy final delivery.
5. TaskUpdate "Step 5b: Print report header" → `completed` in the response that delivers the final block, beside the last tool calls immediately before the table text, never at the follow-up question.

Report file already contains the fields — no separate prepend needed.

> **Output-Routing exemption**: the follow-up question carries only a plain summary; quality-gates.md §Report File Format's table-first-in-reply rule and Output Routing step 2a apply to the final reply after the answer, and to no-question paths. The table IS the final reply's header — omit the `╔═╗` Re:Anchor box (communication.md exempts quality-gates `---` report headers). Never emit both a box header and this table.

```bash
# compaction boundary 2 (compaction-contract.md §Lifecycle)
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _RUN_DIR < "${TMPDIR:-/tmp}/dev-review-run-dir-${CSID}" 2>/dev/null || _RUN_DIR=""
IFS= read -r _REPORT_DIR < "${TMPDIR:-/tmp}/dev-review-report-dir-${CSID}" 2>/dev/null || _REPORT_DIR=""
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/write_skill_contract.py" "develop:review" "follow-up (after consolidation)" "$_RUN_DIR" "final-report=$_REPORT_DIR/review-report.md" "Step 6 only with Codex output → follow-up gate: re-read report header, re-issue follow-up AskUserQuestion"  # timeout: 5000
```

## Step 6: Delegate implementation follow-up (optional)

Re-hydrate `CODEX_OUT` from persisted temp file (Bash() state doesn't survive between calls):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CODEX_OUT < "${TMPDIR:-/tmp}/dev-review-codex-out-${CSID}" 2>/dev/null || CODEX_OUT=""  # timeout: 5000
if [ -n "$CODEX_OUT" ] && [ -f "$CODEX_OUT" ]; then echo "CODEX_OUT=$CODEX_OUT"; else echo "→ no Codex output — Step 6 skipped"; fi
```

Block printed the skip line (`$CODEX_OUT` empty or file at that path doesn't exist) → skip Step 6.

After consolidating, identify findings Codex could implement — not style violations (pre-commit handles those), but bounded code or documentation work grounded in the actual implementation. The review only names them; it never edits code. A fix applied inside review would land outside the fix skills' reproduce-test-verify loop and could collide with their later edits to the same file.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_load_shared.py" codex-delegation.md  # timeout: 5000
```

Apply the "good fit" and "don't delegate" criteria defined there (when found) — criteria only; never invoke `bridge:implement` from this step. Qualifying findings → print a `### Codex-eligible findings` section listing each finding's location and a one-line brief, and add them to the report's **Recommended Next Steps** as candidates for a `bridge:implement` call by the fix workflow or the user. None qualify → omit the section.

**Hard gate**: before the follow-up question, the Step 5b header table and plain summary must already be built from the current report file. Not built → go back and do it now (see Step 5b). The question carries the summary; the full delivery block opens the final reply after the answer (or on a no-question path). Keep "Step 5b: Print report header" `in_progress` until that final delivery.

**Hook-enforced**: on `Stop`, `hooks/enforce-review-header.js` keeps a turn going once per report when its final reply lacks the matching full header table — skipping the follow-up question does not skip delivery. At question time it blocks only this workflow's follow-up until the current report exists and the first question's text names the report path and its `Title`, `Outcome` values as a short plain summary, no table. Option descriptions and previews do not supply the summary. Missing evidence blocks this transition; apply the placement the deny reason names, then retry. Diagnostic/recovery questions remain available; use their own question header, not `dev-review`. The existing sentinel lifetime still scopes this workflow guard; it does not prove UI rendering or report correctness.

**Worktree exit** — if `WORKTREE_ENABLED=true`: the report already lives in the main tree (§Deliverable). Follow `worktree-isolation.md` §Exit — capture branch, call `ExitWorktree(action="keep")`, append the `Worktree` block to the report/output. Exit **before** the follow-up gate so the `/develop:fix`/`/develop:refactor` next-step suggestions below point at the main tree. Never auto-merge.

**Suggested next steps** (plain text, not selectable — `/develop:fix` and `/develop:refactor` both carry `disable-model-invocation: true`, so `Skill()` dispatch is impossible for either): blocking issues found → `Run: /develop:fix` to reproduce with a test, apply a targeted fix; structural/quality issues found → `Run: /develop:refactor` for test-first improvements.

Refresh the contract right before the gate — boundary 2 was written before Step 6 pulled more into context — and print the hint so a long wait can be spent compacted. Step 6 skipped (`CODEX_OUT` empty or its file absent) → nothing new entered context and boundary 2 already carries the gate resume line: skip the block below and go straight to the follow-up gate. Otherwise run:

```bash
# compaction boundary 3 — immediately before the follow-up idle gate (compaction-contract.md §Lifecycle)
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _RUN_DIR < "${TMPDIR:-/tmp}/dev-review-run-dir-${CSID}" 2>/dev/null || _RUN_DIR=""
IFS= read -r _REPORT_DIR < "${TMPDIR:-/tmp}/dev-review-report-dir-${CSID}" 2>/dev/null || _REPORT_DIR=""
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/write_skill_contract.py" "develop:review" "follow-up gate" "$_RUN_DIR" "final-report=$_REPORT_DIR/review-report.md" "resume: re-read report header, re-issue follow-up AskUserQuestion"  # timeout: 5000
```

**Follow-up gate (NEVER SKIP)** — Call `AskUserQuestion` tool — do NOT write options as plain text first. Map options directly into tool call arguments. The `/compact` hint rides in the question text, never as reply prose or Bash stdout before the call (text before a tool call can vanish on 5.5-family models):

- header: `dev-review`
- question: at most 6 plain lines (target 600 characters, hard cap 1,500), no table, pipe layout or code fence: `Title: <report Title>`; `Outcome: <report Outcome>`; finding counts and blockers; `→ <REPORT_DIR>/review-report.md`; `What next?`; "Long wait? `/compact` now — report saved, resume lossless." Keep the `/compact` hint in the question text.
- (a) label: `walk through findings` — description: go through each finding interactively (procedure: ≤4 findings per `AskUserQuestion` call, per finding options `note` / `skip` / `stop`; after the walk run the contract-clear block below)
- (b) label: `skip` — description: no action
- every option: label and one-line description only, no `preview`; never put the header table in the question or its options.

**Confidence block** — emitted by consolidator agent in `$REPORT_DIR/review-report.md`, not at skill level (DMI skill: top-level model invocation disabled, so any skill-level instruction would be unreachable).

```bash
rm -f .temp/state/skill-contract.md  # clear contract — skill complete (compaction-contract.md §Lifecycle)  # timeout: 5000
```

</workflow>

<notes>

- Critical issues always surfaced regardless of scope
- Skip sections with no issues — no padding with "looks good". Reviewing isolated code without git context → skip Performance Concerns unless code itself shows performance issues.
- **Signal-to-noise gate**: function or class ≤50 lines with only 1–2 ground-level issues (critical/high) → no more than 2 medium/low findings beyond them. Remainder as `[nit]` in dedicated "Minor Observations" section — not elevated to same tier as high-severity findings.
- **Follow-up chains**:
  - `[blocking]` bugs or regressions → `/develop:fix` to reproduce with test, apply targeted fix
  - Structural or quality issues → `/develop:refactor` for test-first improvements
  - Security findings in auth/input/deps → run `pip-audit` for dependency CVEs; address OWASP issues inline via `/develop:fix`
  - Mechanical issues beyond Step 5 findings → `bridge@borda-ai-rig` available → call its `implement` skill with a brief stating the exact finding, target paths, current evidence, permitted edits, required result, stop condition, verification command.
  - Contributor-facing review of GitHub PR → use `/oss:review <PR#>` (requires oss plugin) instead

</notes>
