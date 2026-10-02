---
name: resolve
description: "OSS maintainer fast-close workflow for GitHub PRs. Three phases: (1) PR intelligence — reads full thread, linked issues, PR body to synthesize contribution motivation and classify every comment into action items; (2) conflict resolution — checks out PR branch (fork-aware via gh pr checkout), merges BASE into it, resolves conflicts semantically using contributor's intent as priority lens; (3) implements each action item as separate attributed commit via Codex, pushes back to contributor's fork. Supports three source modes: pr (live GitHub comments only), report (latest /review report findings as action items, no GitHub re-fetch), and pr + report (both sources aggregated and deduplicated in one pass). Also accepts bare comment text for single-comment dispatch. NOT for reply drafting to /oss:analyse findings (use /oss:analyse --reply (requires `oss` plugin)). NOT for code diff review of PR changes (use /oss:review). NOT for release preparation (use /oss:release). NOT for fixing local bugs unrelated to a PR (use /develop:fix; requires develop plugin). TRIGGER when: PR is ready to close and has open comments, conflicts, or review findings to address; user says 'close this PR', 'resolve comments on PR #N', or 'implement review findings'."
argument-hint: <PR number or URL> [report] | report | <review comment text> [--no-challenge] [--agent <name>] [--codemap] [--no-codemap] [--worktree] [--keep "<items>"]
disable-model-invocation: true
model: sonnet
allowed-tools: Read, Edit, Write, Bash, Agent, Skill, TaskCreate, TaskUpdate, TaskList, AskUserQuestion, EnterWorktree, ExitWorktree
effort: high
---

<objective>

OSS maintainer fast-close workflow. PR number → three phases fire automatically:

1. **PR intelligence** — synthesize motivation from PR body, linked issues, thread; classify comments into action items
2. **Conflict resolution** — checkout PR branch (fork-aware), merge `BASE_REF`, resolve conflicts with contributor intent as priority lens
3. **Action item implementation** — implement each item as separate commit attributed to review comment, push to contributor's fork

Result: conflict-free PR branch pushed to fork, ready to merge — no GitHub UI.

**Core invariant — transparent, reversible**: every action = visible named git object. Use `git merge` (new commit, two parents), never `git rebase` (rewrites SHA, kills revert/cherry-pick). Each action item = own commit — granular revert always possible.

Bare comment text → skip to Codex dispatch (Step 12).

</objective>

<inputs>

- **$ARGUMENTS**: one of:
  - Omitted → **review-handoff mode**: auto-detect PR from most recent `.reports/review/pr-*/run-*/review-report.md` or the legacy pre-rename `.reports/review/*/review-report.md` (both oss lineage, same schema) or `.reports/codex/review/*/review-notes.md` (codex lineage, detected but not parsed — see Step 0 lineage guard)
  - PR number (e.g. `42` or `#42`) or GitHub PR URL → **pr mode**
  - `report` (bare word) → **report mode**: latest review findings as action items; no GitHub re-fetch
  - `42 report` or `<URL> report` (order-invariant: `report 42` is the same request) → **pr + report mode**: aggregate live GitHub comments + review report, deduplicated in one pass. The `report` word **adds** the report as a second source; it never suppresses the GitHub fetch — bare `report` (no PR number) is the no-GitHub mode
  - Bare review comment text → **comment dispatch mode** (jumps to Step 12)
- **`--no-challenge`**: optional — skip challenge gate per item; all selected items treated as `VALID`
- **`--no-codemap`**: optional — disable codemap structural context (on by default when codemap installed + index present)
- **`--codemap`**: optional — strict mode: stop and report if codemap not installed or index missing
- **`--agent <name>`**: optional — use `<name>` agent for implementation instead of Codex; must be an implementation agent or the `bridge:implement` Skill marker.
  - Bare name auto-prefixed with `foundry:` if no plugin prefix detected (e.g. `--agent sw-engineer` → `foundry:sw-engineer`; `--agent linting-expert` → `foundry:linting-expert`; `--agent doc-scribe` → `foundry:doc-scribe`); explicit prefix also accepted (`--agent foundry:sw-engineer`); see routing table in `action-item-dispatch.md`.
  - **`--agent` also applies to `INTEL_AGENT` (Step 3b thread intelligence)** — explicit real Agent types override label/title routing for classification as well.
  - `bridge:implement` is never passed to `Agent`: classification falls back to label/title routing, eligible medium items use the Skill route, and other items use the change-to-specialist table.

NOT-for additions (scope guards):

- **NOT for non-Python source PRs** (TypeScript, Go, Rust, Java) unless action items are limited to documentation or CI/CD changes — Step 9's lint-qa gate runs Python-specific tools (`ruff`/`mypy`); non-Python PRs get partial or no static-analysis review. For non-Python repos, run `/oss:resolve` in `report` mode with manually-curated findings.
- **NOT for branches with uncommitted local edits** — the `report`-mode no-PR# path operates on the current branch as-is; uncommitted changes get committed alongside the action items. Stash (`git stash`) or commit local edits before invoking — workflow doesn't auto-stash.

</inputs>

<constants>
```text
CHALLENGE_TIMEOUT_S=300  # intel + challenge agent deadline; tightened from CLAUDE.md §6 default 900s
AGENT_DEADLINE_S=900     # conflict, specialist, QA/lint agent deadline — checked at wake-ups, never polled
```
> Bash timeout convention — `# timeout: N` annotations in bash blocks are honored by the Claude Code
>
> Bash tool (sets tool-level timeout). Shell enforcement (`timeout S cmd` prefix) is NOT required for
>
> skills executed exclusively via Claude Code. Shell prefix added only for commands that could hang
>
> in direct-shell execution (git push, gh pr checkout).
</constants>

<compaction>

> loads: compaction-contract.md

- Boundary 0: before the Step 3d item-selection gate — longest idle window of the run; contract makes a mid-wait `/compact` lossless.
- Boundary 1: end of Phase 1 challenge (`action-item-dispatch.md`), before Phase 2 implementation dispatch. Closes the gap where Phase 1's parallel challenge agents and all of Phase 2's worktree-held implementation ran with no refresh, so a compaction anywhere in either phase resumed at Step 3d and re-asked an already-answered gate.
- Boundary 2: end of Step 8 — per-item implementation loop complete, before Step 9 lint gate. Contract overwrites on each iteration (latest state wins).
- Boundary 3: start of Step 11 — before final report write, after push.
- Preserve at boundary 0: PR#, `IMPL_DIR`, `action-items.jsonl`, `pr-intelligence.md`, `pr-vars.sh` paths.
- Preserve at boundary 1: PR#, `IMPL_DIR`, `challenge-log.txt`, `skipped-items.txt`, `item-tasks.tsv` paths, recorded Step 3d push answer.
- Preserve at boundary 2: PR#, implemented/remaining item state, `IMPL_DIR`, `challenge-log.txt`, `item-tasks.tsv` paths, recorded Step 3d push and post-PR answers, push status.
- Preserve at boundary 3: final report path, PR#, `IMPL_DIR`, `challenge-log.txt`, `item-tasks.tsv` paths, recorded push status, `push-unblock.txt` path.
- State that must survive a compaction lives in files under `$IMPL_DIR`, never only in-context: challenge verdicts (`challenge-log.txt`), item→task map (`item-tasks.tsv`), `IMPL_DIR` itself via the `resolve-impl-dir-${CSID}` sentinel written at `mktemp` time.

</compaction>

<workflow>

<!-- Symbol legend: ⚠ = warning/skipped (non-blocking, proceed with caution) · ⛔ = blocked/stop (halt workflow, do not proceed) -->

<!-- Agent resolution: see _OSS_SHARED/agent-resolution.md -->

## Agent Resolution

`agent-resolution.md` (loaded below) contains: foundry check + fallback table. foundry not installed → use table to substitute each `foundry:X` with `general-purpose`. Agents this skill uses: `foundry:sw-engineer`, `foundry:qa-specialist`, `foundry:linting-expert`, `foundry:doc-scribe`, `foundry:perf-optimizer`, `foundry:solution-architect`, `foundry:challenger`.

<!-- Inline fallback (if agent-resolution.md unreadable): foundry:sw-engineer → general-purpose, foundry:qa-specialist → general-purpose, foundry:linting-expert → general-purpose, foundry:doc-scribe → general-purpose, foundry:perf-optimizer → general-purpose, foundry:solution-architect → general-purpose, foundry:challenger → general-purpose. -->

**Task hygiene** — task tools may be deferred; load before first use: `ToolSearch(query="select:TaskList,TaskCreate,TaskUpdate,TaskGet", max_results=4)`. Call `TaskList` first and triage each task it returns: `completed` if work clearly done, `deleted` if orphaned, keep `in_progress` only if genuinely continuing. Never spend a turn on bookkeeping alone — every `TaskCreate`/`TaskUpdate` ships in the same response as the next substantive tool call; one exception, `TaskUpdate(completed)` immediately before a long output block (`rules/task-lifecycle.md`).

<!-- ARCH.md beside this file diagrams the runs, gates and parallel fan-out. Documentation only, never loaded — update it in the same commit as any change to step order, gate placement, or agent fan-out. -->

## Run structure — text order is not execution order

Two blocking user gates cut this workflow into three **runs**; an explicit "don't push" at Step 3d removes the second gate, leaving two. Within a run, work that shares no data dependency is dispatched in the same response turn so it overlaps instead of queueing. Sections below stay in step-number order for reading; the run marker on each heading says when it actually executes.

| Run | Executes | Ends at |
| -- | -- | -- |
| 1 | Step 1 · 2 · 3a · 3b ‖ **Step 4 + Step 5** · 3c · **Steps 6–7a** ‖ gate | Step 3d selection gate |
| 2 | Step 7b join · 3e · 8 · 9 | Step 10 push confirmation (skipped on an explicit Step 3d "don't push") |
| 3 | Step 10 push · 11 · 12 | workflow end |

**Run 2 is unattended on the normal path.** Step 3d collects every decision a later step needs and can predict up front with the same information — item scope, commit mode, grouping strategy and typed labels, dispatch width, the over-20 cap, push intent and the post-PR action — and persists each to a sentinel or `$IMPL_DIR` file. Later steps never re-ask a Step 3d decision. Push authorization is the one decision that cannot move: its required scope (diff stat, commit count, last subject) exists only after implementation, so Step 10 asks it with that scope unless the user explicitly chose "don't push" at Step 3d. Otherwise Run 2 asks only for error recovery (an unresolved item status, a challenge that timed out twice, a lost typed-labels file) or for the group preview the user explicitly chose at Step 3d.

Two overlaps, both free — each rides an idle window the orchestrator already had:

- **Step 4 + Step 5 beside the Step 3b intel agent.** Checkout and the `--no-commit` merge need only `PR_NUMBER`, never any intelligence output, so they run in the same turn that spawns `INTEL_AGENT` instead of waiting for its envelope.
- **Steps 6–7a beside the Step 3d gate.** Conflict resolution does not depend on which items the user selects — Step 4 already mandates it at zero selected items — so the per-file agents are dispatched in the same turn as the selection question and work through the ~15 min of human idle.

One dependency forbids a wider overlap: **Step 6a consumes the contribution motivation that `INTEL_AGENT` synthesizes** (`pr-intelligence.md`: PR body is stated intent, thread is the authoritative record). Steps 6–7 therefore cannot start before that envelope returns, and never fall back to a git-log-only lens.

Degenerate cases, all reducing to the old serial order with no special handling: `report` mode with no PR# skips Steps 4–7 entirely; zero conflicted files means Step 5 commits the merge itself and Steps 6–7 plus the join are no-ops; `--worktree` enters the worktree inside Step 4, so an `INTEL_AGENT` spawned moments earlier keeps writing to the absolute `IMPL_DIR` it was handed.

## Agent wait discipline — no polling, per-agent deadlines

Every spawned agent (intel, conflict, Phase 1 challenge, Phase 2 specialists, Step 9 QA/lint) runs in the background. Waiting on one is **never** a tool call:

- **Never** call `ScheduleWakeup`, `ListAgents`, or a `Monitor` loop to wait for a spawned agent — and no `sleep`, no poll loop, no no-op call, no "waiting" turn. Spawn, end the turn, resume on the completion notification (`rules/task-lifecycle.md` §After spawning).
- **Arm a deadline per agent.** In the same response as each spawn batch, write `$IMPL_DIR/agent-watch-<batch>.tsv` with the Write tool — one row per agent, `<name>\t<deliverable path, or - for an envelope-only agent>\t<deadline seconds>`. The file's write time is the spawn time, so no clock value is ever typed. Batch names and deadlines: `intel` 300 · `conflict` 900 · `challenge` 300 · `challenge-retry` 300 · `impl` 900 (rewrite per wave) · `qa` 900.
- **Check once per wake-up.** On every completion or idle notification, first persist any envelope the notification carried (where a step says to write it to a file), then run the block below once, before acting on any agent output, and act on every row: `done` → consume it · `timed_out` → ⏱ `timed_out` now and take the step's documented fallback · an agent whose notification arrived but whose row is still `pending`/`awaiting-envelope` (idle or finished without its deliverable) → ⏱ `timed_out` now, never wait for it further · rows still open with no notification → end the turn.
- Never ask the user whether to keep waiting, and never leave a stalled agent for the user to notice — a run the user returns to must already show every ⏱.
- A ⏱ only informs. It never answers, skips, or defaults a user question: every gate the step defines still fires on the timed-out path.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" 2>/dev/null || IMPL_DIR=""
[ -n "$IMPL_DIR" ] || { echo "! BLOCKED — IMPL_DIR sentinel missing; cannot check agent deadlines"; exit 1; }
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/agent_watch.py" --state-dir "$IMPL_DIR"  # timeout: 5000
```

## State checks — one call

Any ad-hoc look at repository state — branch, HEAD, upstream, ahead/behind, merge-base against the base branch, staged/unstaged/untracked/unmerged files, an in-progress merge, remotes, worktrees — is **one** call to the snapshot below, never a run of separate `git status` / `branch` / `log` / `rev-parse` / `remote` / `worktree list` calls. The guard fences inside steps keep their own git commands: they gate on exact exit codes and are already single calls. Writes (`add`, `commit`, `push`, `merge`) are never replaced.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r BASE_REF < "${TMPDIR:-/tmp}/resolve-base-ref-${CSID}" 2>/dev/null || BASE_REF=""
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/git_state_snapshot.py" --base-ref "$BASE_REF"  # timeout: 15000
```

## Step 1: Pre-flight

- Capture caller branch first — Step 11 restore needs it even when Step 4 (`gh pr checkout`) skipped or fails mid-checkout. Init here so Step 11 restore path always defined.
- Preflight in `bin/resolve_preflight.py` — checks codex availability, `gh` binary + auth, syncs remote. Caches positive results under `.temp/state/preflight/` (4 h TTL).
- Writes `CODEX_AVAILABLE` and `GH_OK` to `${TMPDIR:-/tmp}/resolve-preflight-*-<CSID>`; status to stderr; exits non-zero only on hard failure (`gh` missing/unauthenticated, `git pull` conflict) — `gh` missing/unauthenticated aborts whole block below, flag parsing never runs.
- Codemap auto-detects here too (auto-on if installed; `--no-codemap` off; `--codemap` strict — stop if missing); merged into this same call since nothing between agent resolution, preflight, and codemap detection depends on a decision made in between.

```bash
# timeout: 45000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# loads: oss-shared-resolver.md
# loads: review-section-taxonomy.md
# loads: compaction-contract.md
_OSS_SHARED=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/resolve_shared_path.py" oss skills/_shared 2>/dev/null)  # timeout: 5000
_OSS_RESOLVE=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/resolve_shared_path.py" oss skills/resolve 2>/dev/null)  # timeout: 5000
[ -z "$_OSS_RESOLVE" ] && _OSS_RESOLVE="plugins/cc_oss/skills/resolve"
echo "$_OSS_SHARED" > "${TMPDIR:-/tmp}/resolve-oss-shared-${CSID}"  # cross-block (Check 41)
echo "$_OSS_RESOLVE" > "${TMPDIR:-/tmp}/resolve-oss-resolve-${CSID}"
cat "$_OSS_SHARED/agent-resolution.md"  # timeout: 5000

SAVED_BRANCH=$(git branch --show-current 2>/dev/null || echo "")
echo "$SAVED_BRANCH" > "${TMPDIR:-/tmp}/resolve-saved-branch-${CSID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/resolve_preflight.py"
_PREFLIGHT_RC=$?
[ "$_PREFLIGHT_RC" -ne 0 ] && { echo "! BLOCKED — resolve_preflight.py failed (gh missing/unauthenticated or git pull conflict); cannot proceed"; exit 1; }
IFS= read -r CODEX_AVAILABLE < "${TMPDIR:-/tmp}/resolve-preflight-CODEX_AVAILABLE-${CSID}" 2>/dev/null || CODEX_AVAILABLE="false"
IFS= read -r GH_OK < "${TMPDIR:-/tmp}/resolve-preflight-GH_OK-${CSID}" 2>/dev/null || GH_OK="true"
# --worktree/--keep: worktree off HEAD pre-Step4 checkout (worktree-isolation.md §resolve)
# shared flag/--keep parser (C5; also analyse/review SKILL.md)
eval "$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/parse-skill-flags.py" --flags worktree "$ARGUMENTS")"
WT_ENABLED="$FLAG_WORKTREE"
echo "${KEEP_ITEMS:-}" > "${TMPDIR:-/tmp}/resolve-keep-items-${CSID}"  # compaction-contract.md §keep: semantics
echo "$WT_ENABLED" > "${TMPDIR:-/tmp}/oss-resolve-worktree-${CSID}"
# stale contract, crashed prior run (compaction-contract.md §Lifecycle)
rm -f .temp/state/skill-contract.md

# codemap: auto-on if installed; --no-codemap off; --codemap strict (stop if missing)
# loads: detect_codemap.py — consumers: resolve/SKILL.md, review/SKILL.md
_DETECT_CODEMAP="${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/detect_codemap.py"
# codemap flags parsed inside the script, before parse-resolve-args: one argv slot, shlex-tokenised
python "$_DETECT_CODEMAP" --prefix resolve --arguments "$ARGUMENTS" 2>&1  # timeout: 5000
[ $? -ne 0 ] && { echo "! BLOCKED — codemap strict mode requested but codemap not installed or index missing"; exit 1; }
IFS= read -r CODEMAP_ENABLED < "${TMPDIR:-/tmp}/resolve-codemap-enabled-${CSID}" 2>/dev/null || CODEMAP_ENABLED="false"
IFS= read -r CODEMAP_CURRENCY < "${TMPDIR:-/tmp}/resolve-codemap-currency-${CSID}" 2>/dev/null || CODEMAP_CURRENCY="off"
IFS= read -r CODEMAP_FORCE_OFF < "${TMPDIR:-/tmp}/resolve-codemap-forced-off-${CSID}" 2>/dev/null || CODEMAP_FORCE_OFF="false"
[ "$CODEMAP_FORCE_OFF" = "false" ] && cat "$_OSS_SHARED/codemap-gates.md"  # timeout: 5000
```

**Codemap gates** — when `CODEMAP_FORCE_OFF=false`, run (from `codemap-gates.md`, loaded above): **Gate A** if `CODEMAP_ENABLED=false` (missing index → offer to build); **Gate B** if `CODEMAP_ENABLED=true` and `CODEMAP_CURRENCY=stale`. On a build choice, build with the gated `codemap-py index` binary in the foreground, then set `CODEMAP_ENABLED=true` — never model-invoke the `codemap-py:scan-codebase` skill, which is `disable-model-invocation: true` (user-slash-only). Skip both gates when `CODEMAP_FORCE_OFF=true` (`--no-codemap`).

Codex missing: set `CODEX_AVAILABLE=false` — Steps 3–7 work without it. Step 8 degradation:

1. Simple, single-file items → `foundry:sw-engineer`
2. Complex/multi-file → skip with: `⚠ bridge@borda-ai-rig is absent or disabled — skipping item #<id>. Install or enable the bridge and reload plugins.`

### Review-handoff auto-detect (when $ARGUMENTS is empty)

When `$ARGUMENTS` empty:

```bash
# oss lineage → .reports/review/pr-<N>/run-<NNN>/ (current) or .reports/review/<timestamp>/ (pre-rename, still readable); codex lineage → .reports/codex/review/
REVIEW_FILE=$(ls -t .reports/review/*/review-report.md .reports/review/*/*/review-report.md .reports/codex/review/*/review-notes.md 2>/dev/null | head -1)
if [ -z "$REVIEW_FILE" ]; then
    echo "No review output found in .reports/review/ or .reports/codex/review/ — run /review <PR#> first, or provide a PR number"
    exit 1
fi
case "$REVIEW_FILE" in
    .reports/codex/review/*)
        echo "! BLOCKED — newest review is codex-lineage ($REVIEW_FILE); this parser reads oss:review's .reports/review/pr-*/run-*/review-report.md section schema only, not codex's flat H1/H2/M1-bullet schema. Falling through would silently miss any blocking findings that review recorded. Provide a PR number explicitly (\`/oss:resolve <PR#>\`), or run /oss:review on this PR to produce a compatible report."
        exit 1
        ;;
esac
echo "→ Using: $REVIEW_FILE"
```

Read `$REVIEW_FILE`. Extract PR number from header:

- Pattern: `## Code Review: PR #<N>` or `## Code Review: <N>`
- Grep: `grep -oE '(PR #|#)?[0-9]+' "$REVIEW_FILE" | head -1 | grep -oE '[0-9]+'`

PR found → set `$ARGUMENTS = <N>`, proceed PR mode. Print: `→ Resolved PR #<N> from review output.`

No PR number extractable → print: "Review output does not reference a PR — provide a PR number explicitly: `/oss:resolve <PR#>`" and exit 1.

Parse $ARGUMENTS:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
[ -n "$CLAUDE_PLUGIN_ROOT" ] || { echo "Error: CLAUDE_PLUGIN_ROOT is unset — verify oss plugin installation and that skill is invoked via Claude Code plugin system"; exit 1; }  # timeout: 5000
[ -f "${CLAUDE_PLUGIN_ROOT}/bin/parse-resolve-args.py" ] || { echo "Error: parse-resolve-args.py not found — verify oss plugin installation"; exit 1; }  # timeout: 5000
# parse-resolve-args.py anchors on "<PR#> [report]" alone — ANY surviving flag token routes to
# comment-dispatch and drops PR_NUMBER. Every supported flag must be stripped here, not just codemap/keep.
eval "$(python "${CLAUDE_PLUGIN_ROOT}/bin/parse-skill-flags.py" --flags no-codemap,codemap,worktree,no-challenge --value-flags agent "$ARGUMENTS")"  # timeout: 5000
ARGUMENTS="$CLEAN_ARGS"
case "${VALUE_AGENT:-}" in
    '') ;;
    *:*) ;;
    *) VALUE_AGENT="foundry:$VALUE_AGENT" ;;
esac
echo "$FLAG_NO_CHALLENGE" > "${TMPDIR:-/tmp}/resolve-no-challenge-${CSID}"  # read by Step 8 Phase 1
echo "${VALUE_AGENT:-}" > "${TMPDIR:-/tmp}/resolve-agent-override-${CSID}"
echo skip > "${TMPDIR:-/tmp}/resolve-post-pr-action-${CSID}"  # Step 3d push question overwrites; a run that never reaches it must not inherit last run's `open`
# `unset`, not `skip`/`push`: a run that never records a Step 3d push answer must neither push nor inherit last run's `push` — Step 10 asks instead
echo unset > "${TMPDIR:-/tmp}/resolve-push-auth-${CSID}"
echo none > "${TMPDIR:-/tmp}/resolve-push-status-${CSID}"  # Step 10 overwrites with what happened; `none` = Step 10 never ran (no PR#, skip-all)
# same reason: a run that never reaches Step 3d (zero pending with no closed items, bulk skip-all) must not inherit last run's `stage`/`grouped`.
# `unset`, not `each`: Step 8's merge fence aborts on it, so a skipped Step 3d block fails loud instead of landing per-item commits
echo unset > "${TMPDIR:-/tmp}/resolve-commit-mode-${CSID}"
echo domain > "${TMPDIR:-/tmp}/resolve-group-strategy-${CSID}"
# Step 3d overwrites; `auto` (not `unset`) because a skipped Step 3d means no Phase 2 dispatch either — nothing to widen or serialize
echo auto > "${TMPDIR:-/tmp}/resolve-dispatch-mode-${CSID}"
: > "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}"  # empty, not rm (danger filter): report mode skips Step 3b; a stale dir from the previous PR would feed its items into this run
# defence-in-depth: validate VAR=value, no metachars, before sourcing — guards regression/tampered binary
tmpenv=$(mktemp)  # timeout: 3000
trap 'rm -f "$tmpenv"' EXIT INT TERM
python "${CLAUDE_PLUGIN_ROOT}/bin/parse-resolve-args.py" "$ARGUMENTS" >"$tmpenv"  # timeout: 5000
if grep -qvE "^[A-Z_][A-Z0-9_]*=([A-Za-z0-9_./:#@+-]*|'[^']*')$" "$tmpenv"; then
    echo "Error: parse-resolve-args.py emitted unexpected output — refusing to source"
    cat "$tmpenv"
    exit 1
fi
. "$tmpenv"
# sets: PR_NUMBER, PR_URL, MODE, ARGUMENTS ('#' stripped, comment-dispatch only)
echo "${PR_NUMBER:-n/a}" > "${TMPDIR:-/tmp}/resolve-pr-number-${CSID}"  # timeout: 3000
# liveness sentinel — cheap same-session signal for oss:review's Step 7 already-running check;
# never read back by this skill itself, only written here, cleared on the report/reject-gate
# stops right below and at completion (Step 11/12) — see review/SKILL.md. Tolerated for the same
# reason a crashed skill-contract.md is (compaction.md §Stale-contract caution), though this one
# does NOT self-heal the same way: skill-contract.md is unconditionally rm'd at Step 0 of ANY
# later resolve run regardless of PR, but this sentinel is only overwritten when a later run
# reaches THIS line again — a prose-level stop before it (Step 3d skip-all, over-20 stop, an
# internal `! BLOCKED` sentinel-loss guard) leaves it stale for the rest of the session, silently
# suppressing oss:review's gate for that exact PR on every later invocation, not just once.
# Deliberately not chased into every such exit path — accepted, not eliminated. `report` mode's
# value can never numeric-match a review's PR tag by design: a no-PR resolve run must never
# suppress any review's gate.
case "$PR_NUMBER" in ''|n/a|*[!0-9]*) echo "report" > "${TMPDIR:-/tmp}/oss-resolve-active-${CSID}" ;; *) echo "$PR_NUMBER" > "${TMPDIR:-/tmp}/oss-resolve-active-${CSID}" ;; esac  # timeout: 3000
: > "${TMPDIR:-/tmp}/resolve-base-ref-${CSID}"  # Step 4 or report mode must publish this run's base before Step 9
: > "${TMPDIR:-/tmp}/resolve-pr-ref-${CSID}"  # Step 4 or local report mode must publish this run's commit reference
```

<!-- branch: unsupported-flags — isolated; ≤1 call; fires only when unknown flags present -->

**Unsupported flag check** — after `eval`, scan remaining `$ARGUMENTS` for any `--<token>` not in `{--no-challenge, --agent, --codemap, --no-codemap, --worktree}`. Found → invoke `AskUserQuestion` — (a) **Abort** (stop, re-invoke with correct flags) · (b) **Continue ignoring** (skip unknown tokens). Supported: `--no-challenge`, `--agent <name>`, `--codemap`, `--no-codemap`, `--worktree`, `--keep "<items>"`.

- `MODE="pr+report"` or `MODE="report"` → resolve the report source with the **Report source resolution** block below (executable, not prose), then branch on its printed `REPORT_STATUS`.
  - `MODE="report"` additionally: extract PR# from the report header if present; no PR# in header → add branch safety check before Step 8 — `CURRENT=$(git branch --show-current); DEFAULT=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's|refs/remotes/origin/||'); [ -z "$DEFAULT" ] && DEFAULT=$(git remote show origin 2>/dev/null | grep 'HEAD branch' | awk '{print $NF}'); [ -z "$DEFAULT" ] && { printf "! BLOCKED — cannot determine default branch; refusing to proceed\n"; exit 1; }; [ "$CURRENT" = "$DEFAULT" ] && { echo "⛔ On default branch '$CURRENT' — report mode without PR# must not operate on default branch; check out a feature branch first"; exit 1; }`
- `MODE="pr"` → continue Step 2
- `MODE="comment-dispatch"` → branch safety check before Step 12: `export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"; IFS= read -r WT_ENABLED < "${TMPDIR:-/tmp}/oss-resolve-worktree-${CSID}" 2>/dev/null; [ "$WT_ENABLED" = "true" ] || WT_ENABLED=false; CURRENT=$(git branch --show-current); DEFAULT=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's|refs/remotes/origin/||'); [ -z "$DEFAULT" ] && DEFAULT=$(git remote show origin 2>/dev/null | grep 'HEAD branch' | awk '{print $NF}'); [ -z "$DEFAULT" ] && { printf "! BLOCKED — cannot determine default branch; refusing to proceed\n"; exit 1; }; [ "$CURRENT" = "$DEFAULT" ] && { echo "⛔ On default branch '$CURRENT' — comment dispatch must not commit to default branch"; exit 1; }; [ "$WT_ENABLED" = "true" ] && echo "⚠ --worktree has no effect in comment-dispatch mode"` → jump to Step 12

### Reject-gate check (every mode — run the block even without a `PR_NUMBER`)

`oss:review`'s acceptance gate can reject a PR at the premise level — `Gate: REJECT_<GROUND> @<sha>`, one of `GOAL`/`CONDUCT`/`SCOPE`/`LICENSE`/`DUPLICATE`/`REVERTED`/`SPAM`/`PHILOSOPHY` (see `oss:review` SKILL.md Stage 1 for what each means). Premise problem, not fixable by `/oss:resolve` editing code — never start the fix pipeline on a PR still in that state, regardless of which of the 8 grounds fired. Only complete `PASS` or `BLOCK` reports are actionable fix queues. Missing, malformed, or ambiguous decision headers block the workflow; a newer unfinished run cannot replace an earlier decision. No prior report still permits PR-comments-only mode. Report-only mode also validates the selected report; it cannot clear a rejection by bypassing PR-aware intake.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PR_NUMBER < "${TMPDIR:-/tmp}/resolve-pr-number-${CSID}" 2>/dev/null || PR_NUMBER=""
# --path-out: this lookup is PR-scoped; Steps 3a/3c reuse it instead of a second newest-of-any-PR glob
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/find_review_report.py" --pr "$PR_NUMBER" \
    --path-out "${TMPDIR:-/tmp}/resolve-report-file-${CSID}"  # timeout: 6000
```

The gate's `--path-out` sentinel (`${TMPDIR:-/tmp}/resolve-report-file-${CSID}`) is the **PR-scoped** answer to "does a review report for this PR already exist". Steps 3a and 3c reuse it; PR-scoped misses never glob another PR — never re-derive it from the gate's printed line, and never let the printed `Gate: …` verdict be the only thing parsed out of this block. A path-publication failure stops the workflow. With no `PR_NUMBER` the script skips the check and writes an **empty** sentinel — that write is why the block runs in every mode: a bare `/oss:resolve report` after an earlier `/oss:resolve 42 report` in the same session would otherwise inherit PR 42's path.

### Report source resolution (`report` and `pr + report` modes)

Run this block — it is the single lookup for both modes, and the **only** sanctioned way to conclude that no report exists. A prose-only lookup here was silently skipped in a real run, and the orchestrator re-ran a full `oss:review` fan-out while a matching report sat on disk and its path had already been printed by the reject gate one block earlier.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# timeout: 5000
IFS= read -r PR_NUMBER < "${TMPDIR:-/tmp}/resolve-pr-number-${CSID}" 2>/dev/null || PR_NUMBER=""
# re-run the PR-scoped lookup here (idempotent): the sentinel is trustworthy only if the reject gate ran THIS run for THIS PR.
# Its exit 1 = still-rejected PR; never swallow that — a skipped gate block would otherwise resolve a rejected PR silently
if [ -n "$PR_NUMBER" ] && [ "$PR_NUMBER" != "n/a" ]; then
    python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/find_review_report.py" --pr "$PR_NUMBER" --path-out "${TMPDIR:-/tmp}/resolve-report-file-${CSID}" || { echo "REPORT_STATUS=blocked"; rm -f "${TMPDIR:-/tmp}/oss-resolve-active-${CSID}"; exit 1; }  # timeout: 6000
fi
IFS= read -r REPORT_FILE < "${TMPDIR:-/tmp}/resolve-report-file-${CSID}" 2>/dev/null || REPORT_FILE=""
[ -f "$REPORT_FILE" ] || REPORT_FILE=""  # sentinel may outlive its report (TTL sweep, failed --path-out write)
# with a PR# the gate sentinel is authoritative: empty means "none for THIS PR", and the newest
# report of a *different* PR would merge the wrong findings. No PR# → the sentinel carries nothing
# PR-scoped (gate wrote it empty), so glob newest-of-any.
case "$PR_NUMBER" in
    ""|n/a) REPORT_FILE=$(ls -t .reports/review/*/review-report.md .reports/review/*/*/review-report.md .reports/codex/review/*/review-notes.md 2>/dev/null | head -1) ;;
esac
echo "$REPORT_FILE" > "${TMPDIR:-/tmp}/resolve-report-file-${CSID}"
case "$REPORT_FILE" in
    "")                      echo "REPORT_STATUS=missing" ;;
    .reports/codex/review/*) echo "REPORT_STATUS=codex-lineage" ;;
    *)
        if [ -z "$PR_NUMBER" ] || [ "$PR_NUMBER" = "n/a" ]; then
        python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/find_review_report.py" --report "$REPORT_FILE" || { echo "REPORT_STATUS=blocked"; rm -f "${TMPDIR:-/tmp}/oss-resolve-active-${CSID}"; exit 1; }  # timeout: 6000
        fi
        echo "REPORT_STATUS=ok" ;;
esac
echo "REPORT_FILE=$REPORT_FILE"
```

Branch on the printed `REPORT_STATUS` — read it from stdout, never assume it:

- `blocked` → stop the fix pipeline; retain the printed reason (rejected PR, incomplete report, failed path publication, or an invalid `--pr` value). Diagnostic/recovery questions remain available. Repair or rerun the producer before consuming findings; never treat this as `missing` or start remediation from its notes.
- `ok` → print `→ Reusing review report: <REPORT_FILE>`; `report` mode continues at Step 3a, `pr + report` at Step 3c. **Never start a review when a report is already resolved.**
- `codex-lineage` → this parser reads `oss:review`'s section schema only, not codex's flat H1/H2/M1-bullet schema. Treat as `missing` for the gate below, stating the lineage as the reason.
- `missing` with **no `PR_NUMBER`** (bare `report` on the current branch) → nothing to offer: there is no second source and no PR to review. Stop with `No review report found in .reports/review/ or .reports/codex/review/ — run /oss:review <PR#> first, or provide a PR number`.
- `missing` with a known `PR_NUMBER` → **do not spawn anything.** The caller asked for report findings; producing them is a decision, not a fallback. Invoke `AskUserQuestion` (actual tool call — prose question is a violation):

<!-- branch: report-missing — fires only when `report` requested, PR# known, and no readable report resolved -->

```text
"No readable review report for PR #<N>. How should /oss:resolve get the findings you asked for?"
  (a) Continue without report findings — GitHub comments only (pr + report mode only)
  (b) Stop — print the /oss:review <PR#> command to run first, then re-invoke resolve  (Recommended)
  (c) Abort — I'll run the review myself
```

- Offer (a) only in `pr + report` — bare `report` has no second source, so its menu is (b)/(c).
- Selected (a) → print `⚠ no report findings merged — GitHub comments only` and continue in pr mode.
- Selected (b) → print `→ Run /oss:review <PR#>, then re-invoke /oss:resolve <PR#> report`, and stop. **Never invoke `Skill(skill="oss:review", …)` here** — `oss:review` ends its own run in a Step 7a `AskUserQuestion` asking the user what to do next, so there is no structural "return" to resume this block on: the whole nested multi-agent fan-out would run only to leave the resume instruction sitting in context, unenforceable across the exact kind of compaction this fix exists to survive. Print the command and let the user re-invoke `/oss:resolve` themselves — the same pattern `oss:review`'s own Step 7a already uses.
- Selected (c) → stop.

### Create all workflow tasks upfront

After `PR_NUMBER` and `MODE` resolved above, create all major-step tasks now — every `TaskCreate` in **one response**, riding with the next real tool call (the Step 3a or 3b `cat` block). Store each returned `task_id` for step-level `TaskUpdate` calls. Conditional tasks: include condition in subject brackets; cancel via `TaskUpdate(status="deleted")` at skip point — never leave conditional tasks pending. All seven creates ride in that one response:

```text
TASK_GATHER   = TaskCreate(subject="Step 2: Gather action items — PR #<N>",              activeForm="Gathering action items for PR #<N>")
TASK_SELECT   = TaskCreate(subject="Step 3: Select action items — PR #<N>",               activeForm="Selecting action items")
TASK_CHECKOUT = TaskCreate(subject="Step 4: Checkout PR branch [if pr mode]",             activeForm="Checking out PR branch")
TASK_CONFLICT = TaskCreate(subject="Steps 5–7: Conflict resolution [if pr mode]",         activeForm="Resolving conflicts")
TASK_IMPL     = TaskCreate(subject="Step 8: Implement selected items [if items selected]", activeForm="Implementing action items")
TASK_LINT     = TaskCreate(subject="Step 9: Lint and QA gate",                             activeForm="Running lint and QA")
TASK_CLOSE    = TaskCreate(subject="Steps 10–11: Push and final report [if pr mode]",      activeForm="Pushing to fork and reporting")
```

**Zero bookkeeping-only turns.** Step-level progress stays visible — every task above moves `pending` → `in_progress` → `completed` (or `deleted` when skipped) exactly as before — but no response may consist only of `TaskCreate`/`TaskUpdate`/`TaskList` calls, per-item and per-conflict tasks included. Each update rides in the response that carries the named real tool call; the only standalone one is `TASK_CLOSE` → `completed` immediately before the long Step 11 report (`rules/task-lifecycle.md` §TaskUpdate before long output):

| Step | Updates | Rides with |
| -- | -- | -- |
| 2 | `TASK_GATHER` → `in_progress` | the Step 3a/3b `cat` block |
| 4 | `TASK_CHECKOUT` → `in_progress` (or `TASK_CHECKOUT`, `TASK_CONFLICT` → `deleted` when skipped) | the Step 4 branch-safety block |
| 4 end | `TASK_CHECKOUT` → `completed` | the FORK_REMOTE block |
| 5 | `TASK_CONFLICT` → `in_progress` | the `conflict-resolution.md` load |
| 3d | `TASK_GATHER` → `completed`, `TASK_SELECT` → `in_progress` | the boundary-0 contract block, before the selection prompt |
| 7b join | `TASK_SELECT` → `completed`, `TASK_CONFLICT` → `completed` | the join's first tool call |
| 8 | `TASK_IMPL` → `in_progress` (or `deleted` when no items) | the codemap index block, or Step 9's first call |
| 8 end | `TASK_IMPL` → `completed` | Step 9's boundary-2 block, with `TASK_LINT` → `in_progress` |
| 9 end | `TASK_LINT` → `completed` | Step 10's first block, with `TASK_CLOSE` → `in_progress` (or `deleted` when Step 10 is skipped) |
| 11 | `TASK_CLOSE` → `completed` | standalone, right before the report |

Per-item tasks (Step 3e) and per-conflict tasks (Step 5a): create each set in one response, and close each item's task in the response that already carries the next real call.

## Step 2: Gather action items

`TaskUpdate(task_id=TASK_GATHER, status="in_progress")` — rides with the Step 3a/3b `cat` block (§Zero bookkeeping-only turns).

## Step 3a: Report intelligence (report mode only)

<!-- loads: report-intelligence.md -->

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _OSS_RESOLVE < "${TMPDIR:-/tmp}/resolve-oss-resolve-${CSID}" 2>/dev/null || _OSS_RESOLVE=""  # reload (Check 41)
cat "$_OSS_RESOLVE/modes/report-intelligence.md"  # timeout: 5000
```

Execute its steps (loaded above).

## Step 3b: PR intelligence

<!-- loads: pr-intelligence.md -->

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _OSS_RESOLVE < "${TMPDIR:-/tmp}/resolve-oss-resolve-${CSID}" 2>/dev/null || _OSS_RESOLVE=""  # reload (Check 41)
IFS= read -r _OSS_SHARED < "${TMPDIR:-/tmp}/resolve-oss-shared-${CSID}" 2>/dev/null || _OSS_SHARED=""  # reload (Check 41)
echo "_OSS_SHARED=$_OSS_SHARED"  # printed (not just written to a file) — pr-intelligence.md's
                                 # spawn prompt substitutes <_OSS_SHARED> with this literal value
cat "$_OSS_RESOLVE/modes/pr-intelligence.md"  # timeout: 5000
```

Execute its steps (loaded above). Substitute `<_OSS_SHARED>` in the Agent() prompt with the literal value printed above.

**Overlap — do not end the turn on the `INTEL_AGENT` spawn.** `Agent()` runs in the background, and Step 4 and Step 5 need nothing it produces. In the same response that dispatches it, continue straight into **Step 4** (checkout) and **Step 5** (merge `--no-commit`, conflict detection, 5a tasks), then end the turn. Resume here at Step 3c when the envelope arrives, with the merge state and conflict task table already decided. Skip the jump only in `report` mode with no PR#, where there is no branch to check out. This is dispatch-then-work, never the forbidden waiting turn: no `sleep`, no poll loop, no no-op call held open (`task-lifecycle.md` §After spawning).

A `>20 conflicted files` abort inside Step 5 now fires while `INTEL_AGENT` is still running. Stop as that gate says; the envelope lands unread and is discarded with the run.

## Step 3c: Merge report findings (pr + report mode only)

*Skip when in pr mode.*

! NO user input in this step — deterministic merge only; Step 3d handles all user selection.

When mode == **pr + report**:

Use the report already resolved by **Report source resolution** (Step 1) — `IFS= read -r REPORT_FILE < "${TMPDIR:-/tmp}/resolve-report-file-${CSID}"`. Never re-glob here: a second newest-of-any-PR lookup can hand this step another PR's findings. Empty sentinel means that block never ran — run it now and honour its gate. Findings source is the same as Step 3a's **Review findings source**: the review's `findings.jsonl` beside the report, or, for an older report without it, a parsed `$IMPL_DIR/report-findings.jsonl` written once with the Write tool.

**Merge, never rewrite by hand.** GitHub comments are often terse; the review's local finding for the same code carries the detail and the evidence paths. `merge_action_items.py` folds the review findings into Step 3b's `action-items.jsonl` deterministically:

- **Same `file:line` as a pending GitHub item** → same target. The GitHub item keeps its id and inherits `finding_id`, the full finding text (appended under the comment), `source_file` and `verify_file`. Author becomes `@login + <owner-agent>`, Summary gains `(also flagged by /review — <owner-agent>)`. It never inherits `verify_verdict`: the verifier confirmed the review's claim, not the GitHub comment's, so Step 8 Phase 1 still tests the item in full, reading the reviewer's evidence first.
- **Same target at another line** (semantic match — your judgment, listed by `--candidates`) → same annotation and evidence paths.
- **One finding per item.** A second finding on the same line, or a link to an item that is closed or already holds a finding, is not folded; that finding is appended as its own item. The summary counts ignored links as `links_ignored`.
- **No match** → appended as a `[report]` item with the next free id (`G+1`, `G+2`, …). GitHub ids `1..G` never change, so no renumbering, and table IDs, Step 3d `SELECTED_ITEMS`, Step 3e task IDs and Step 8 lookups always name the same item.
- **Unmatched pending GitHub item with a short comment** (not a `[question]`) → marked `thin: true`; Step 8 Phase 1 reassesses it from scratch.
- Re-running the merge is safe: findings already present by `finding_id` are skipped.
- When any GitHub row is annotated, the script replaces the file in one atomic rename instead of appending; ids and rows are never retyped.

Step 1 — choose the findings file and list same-file pairs that did not match exactly:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r REPORT_FILE < "${TMPDIR:-/tmp}/resolve-report-file-${CSID}" 2>/dev/null || REPORT_FILE=""
IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" 2>/dev/null || IMPL_DIR=""
[ -n "$IMPL_DIR" ] && [ -f "$IMPL_DIR/action-items.jsonl" ] || { echo "! BLOCKED — Step 3b action-items.jsonl missing"; exit 1; }
[ -f "$REPORT_FILE" ] || { echo "! BLOCKED — report sentinel empty; run Report source resolution (Step 1) first"; exit 1; }
FINDINGS="$(dirname "$REPORT_FILE")/findings.jsonl"
if [ ! -s "$FINDINGS" ]; then
    FINDINGS="$IMPL_DIR/report-findings.jsonl"
    [ -f "$FINDINGS" ] || { echo "! BLOCKED — no findings.jsonl beside the report; write $FINDINGS from the parsed report first"; exit 1; }
    python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/mint_finding_ids.py" "$FINDINGS" || exit 1  # timeout: 5000
fi
printf '%s\n' "$FINDINGS" > "${TMPDIR:-/tmp}/resolve-findings-file-${CSID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/merge_action_items.py" --items "$IMPL_DIR/action-items.jsonl" --findings "$FINDINGS" --candidates  # timeout: 5000
```

Step 2 — for each printed pair, decide whether the finding and the GitHub item describe the same problem (similar description, not merely the same file). Write the accepted pairs with the Write tool to `$IMPL_DIR/report-links.jsonl`, one `{"finding_id": "<id>", "item_id": <N>}` per line; write an empty file when none match. Then merge:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" 2>/dev/null || IMPL_DIR=""
IFS= read -r FINDINGS < "${TMPDIR:-/tmp}/resolve-findings-file-${CSID}" 2>/dev/null || FINDINGS=""
[ -f "$IMPL_DIR/report-links.jsonl" ] || { echo "! BLOCKED — write report-links.jsonl (empty when no semantic match) before merging"; exit 1; }
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/merge_action_items.py" --items "$IMPL_DIR/action-items.jsonl" --findings "$FINDINGS" --links "$IMPL_DIR/report-links.jsonl"  # timeout: 5000
```

- The printed summary's `deduped_exact + deduped_semantic` is the `<M>` and `appended` is the `<K>` of the merge summary line below.
- A non-zero exit stops the run before the merged table or Step 3d; never let Step 8 consume a half-merged file.
- Keep a zero-item file empty. Render the merged table from `action-items.jsonl` itself, so displayed IDs are stored IDs.

**GitHub prefixes**: Step 3b already writes `[gh][req]` / `[gh][suggest]` / `[gh][question]`, and the merge never changes an item's `type`. Never edit `action-items.jsonl` by hand to add a prefix.

### Sources confirmation

Print Sources block (same format as Step 3a template; Mode=pr + report · PR=#<N> · GitHub=Read — PR body · <N> comments · <N> reviews · <N> inline code comments · <N> recurring findings merged · Report=Read <path>) right before merge summary and action item table.

Result: single merged `ACTION_ITEMS`. Storage order is append order (GitHub items `1..G`, then `[report]` items `G+1..`); the table below displays rows severity descending and always shows each row's stored id in `#`. Print merge summary before table:

```text
Report merged: <N> findings from /review · <M> deduplicated against GitHub comments · <K> added as [report] items
```

**MANDATORY — print merged ACTION_ITEMS as markdown table in an assistant user-facing reply immediately after the merge summary and before Step 3d's AskUserQuestion** (severity descending; same columns as pr-intelligence.md table). Include every row in that reply, not Bash/tool stdout. This table is selection-driving data, not a decorative table — print it in full under every compression mode and every communication style active this session (caveman included), never replace it with a prose count or summary line. A reply that references "the table above" without the table in the same message, or in the message immediately before it, is a defect — regenerate the table before sending.

> **Output-Routing exemption (canonical — applies to every ACTION_ITEMS table in this skill, Steps 3b/3c/3d)**: ACTION_ITEMS tables are selection-driving, read-in-context enumerations user must see before Step 3d picker. Put every row in an assistant user-facing reply regardless of row count, not Bash/tool stdout. Global Output Routing (*5+ findings → `.temp/output-*.md`, summary only*) does **not** apply — never divert these tables to a file. Makes explicit what the global rule's own copy-intent override (*read-in-context, acted-on-immediately → user-facing reply even if long*) already implies.

```markdown
### Action Items — PR #<N> (merged)

| # | Type | Change | Severity | Author | Status | Summary | Notes |
|---|------|--------|----------|--------|--------|---------|-------|
| 1 | [gh][req] | code | 4 | @reviewer | pending | rename param x to count | — |
| 2 | [gh][suggest] | docs | 2 | @reviewer + foundry:doc-scribe | pending | add docstring (also flagged by /review — foundry:doc-scribe) | — |
| 3 | [report][suggest] | docs | 2 | foundry:doc-scribe | pending | add docstring to Foo.bar | — |
```

**Author field rules** — Author = who owns fixing this item:

- `[gh]` items (no dedup): GitHub reviewer's `@login`
- `[gh]` items (dedup collision with report): `@login + <owner-agent>` (e.g. `@reviewer + foundry:doc-scribe`) — both authors preserved
- `[report]` items (no collision): Owner agent from taxonomy (e.g. `foundry:doc-scribe`, `foundry:qa-specialist`) — **never** the skill name `review` or `/review`

Summary ≤60 chars. Notes = `—` when empty; carries commit SHA for `addressed` rows and classification verdicts — never `file:line`, which the `file`/`line` fields already hold. Status = item `status` (`pending`/`resolved`/`addressed`, pr-intelligence.md rule); Type never carries resolution. Print only when merged ACTION_ITEMS has ≥1 row.

`location` is a field, not a column — stays in `action-items.jsonl`, drives resolve routing, gets no column here: `[report]` origin already carried by `Type` and `Author`. One non-redundant bit is resolvability, so preserve it the same way every other table in this skill does — **append `· thread (no GH resolve)` to Status for `location: discussion` rows** (same rule as Step 11's table and the Step 3d picker). Never reintroduce a `Loc` column to restate what `Type`, `Author`, and that suffix already say. Merged table is authoritative set for Step 3d selection — supersedes pre-merge table shown in Step 3b.

## Step 3d: User item selection

<!-- branch: main-path — item-selection (always fires in step 3d; ≤3 items = two calls: items + bulk + commit-mode + dispatch, then push + topic-group-when-grouped follow-up; 4-6 and 7-9 = two calls: checkboxes + bulk, then commit-mode + topic-group + dispatch + push follow-up; 10-18 = three: two checkbox pages + the same follow-up; ≥19 = two: bulk + commit-mode + topic-group + dispatch, then push + over-20-when-needed follow-up; zero pending with closed items = bulk + commit-mode + topic-group + dispatch, then push + over-20-when-needed; zero pending without closed items and with a PR = one push-only call; push question omitted when no PR number exists) -->

! IMPORTANT — invoke `AskUserQuestion` tool directly. Never write options as plain text.

Gather is complete here (3a, 3b, or 3c done). Report mode also enters this step for nonempty report items, so the same user choice supplies item scope, commit mode, and the over-20 cap decision. Mark TASK_GATHER `completed` and TASK_SELECT `in_progress` **before** the selection prompt — otherwise the gather `activeForm` keeps driving the spinner through the user-selection window, falsely implying gather is still running. `TaskUpdate(task_id=TASK_GATHER, status="completed")` and `TaskUpdate(task_id=TASK_SELECT, status="in_progress")` both ride with the boundary-0 contract block below.

Pending items = ACTION_ITEMS where `status` is `pending` or absent and type contains neither `[info]` nor legacy `[done]`. Closed items = `status` `resolved` or `addressed`: they stay in the table but never enter checkboxes, bulk options, or the pending count — the user pulls one in only by typing its id (see "Type something" below). With ≥1 closed item, print `→ N resolved/addressed items not in bulk options — type their ids to include` in the reply right before the picker.

- **Zero pending, closed items present** → follow the dedicated slot-table row below: no item checkboxes; ask the existing bulk menu, commit-mode, topic-group, and dispatch questions. The bulk menu's "Type something" field accepts explicit closed IDs in PR and report modes alike. Print the full table first. Bulk (a)/(b)/(c) selects no closed IDs; (d) stops as usual. Typed IDs follow the ordinary bulk-action resolution, commit-mode, and over-20 rules; then ask the follow-up when a PR number exists or more than 20 IDs were selected; without a PR number omit only push, never the over-20 question. If no IDs were selected, discard commit/group/dispatch answers and continue with the empty selection after the push follow-up.

- **Zero pending, no closed items** → set `SELECTED_ITEMS` empty; when a PR number exists, ask the **Push question** below alone — the Steps 5–7 merge commit still needs push intent. Then continue to Step 3e for `pr`/`pr+report`, or skip Step 3e in `report` mode. This bypass never applies to a list containing resolved/addressed items.

Sort all pending items by severity descending (most impactful first).

**Overlap — dispatch Steps 6–7a before asking.** Conflicted files and their tasks are already known (Step 5 ran in this run), and the `INTEL_AGENT` motivation Step 6a needs has just arrived, so the per-file resolution agents are dispatched **in this same response, before the `AskUserQuestion` call**. They work through the idle window below instead of after it. Their result is collected at the Step 7b join that opens Run 2. Nothing here is wasted whatever the user picks: conflict resolution is mandatory even at zero selected items. No conflicted files → nothing to dispatch; proceed straight to the gate.

Longest idle window of the run sits here (median ~15 min, measured up to 16 h) — long enough for the prompt cache to expire, so the next turn rewrites the whole context at write rate. Persist a resume contract first, then print the hint so the user can `/compact` while waiting (skill can't trigger compaction itself):

```bash
# compaction boundary 0 — before the Step 3d idle gate (compaction-contract.md §Lifecycle)
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" 2>/dev/null || _IMPL_DIR=""
IFS= read -r _PR_NUMBER < "${TMPDIR:-/tmp}/resolve-pr-number-${CSID}" 2>/dev/null || _PR_NUMBER="n/a"
IFS= read -r _KEEP < "${TMPDIR:-/tmp}/resolve-keep-items-${CSID}" 2>/dev/null || _KEEP=""
_PRESERVE="pr=$_PR_NUMBER, impl-dir=$_IMPL_DIR, intel=$_IMPL_DIR/pr-intelligence.md, items=$_IMPL_DIR/action-items.jsonl, vars=$_IMPL_DIR/pr-vars.sh"
[ -n "$_KEEP" ] && _PRESERVE="$_PRESERVE; user-keep: $_KEEP"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/write_skill_contract.py" "oss:resolve" "item selection (Step 3d gate)" "$_IMPL_DIR" "$_PRESERVE" "resume: re-read action-items.jsonl + pr-intelligence.md, re-issue Step 3d AskUserQuestion; Steps 6-7a agents may be in flight — re-check git diff --name-only --diff-filter=U at the Step 7b join before Step 8"  # timeout: 5000
```

Then print this line **in the reply** (prose, not Bash stdout — tool output is not reliably shown to the user): `` Long wait? `/compact` now — state persisted in <IMPL_DIR>, resume lossless. ``

Immediately before the first `AskUserQuestion`, ensure the latest assistant user-facing reply contains every ACTION_ITEMS row the user is selecting from (the full Step 3b or 3c table, or the ≥19 compressed table). If intervening work separated the earlier table reply from the picker, repeat that table in the reply now. Bash/tool stdout and a row count do not satisfy this gate. Steps 6–7a output (conflict fixes, merge commit) lands in this same turn, so print the table **after** it, as the last text before the picker. **Hook-enforced**: `hooks/enforce-resolve-table.js` denies the selection call while any pending `action-items.jsonl` id lacks a table row since the last user turn — on denial, print the table and re-issue the call.

**Cap mechanics — read before building any call**: the tool cap is **4 questions per call**. The `Submit` tab is NOT a question — a 4-question call renders 5 tabs. Never stop at 3 questions believing the cap is reached, and never over-pack a question past 3 items to avoid opening a 4th. Within one question, `AskUserQuestion` appends "Type something" outside the option list, so 3 items + Type something = 4 visible rows; that is the **≤3 items/question** limit, a separate constraint from the 4-question cap.

**Call layout — literal slot template, pick by pending-item count** (each AskUserQuestion window is pure human idle, median ~15 min — merge whenever the 4-question cap allows):

| Pending | Call 1 slots | Follow-up call |
| -- | -- | -- |
| 0, closed items present | Q1 bulk · Q2 commit-mode · Q3 topic-group · Q4 dispatch | Q1 push · Q2 over-20, only when more than 20 IDs are selected |
| 0, no closed items | Q1 push, only when a PR number exists; otherwise no call | None |
| ≤3 | Q1 items · Q2 bulk · Q3 commit-mode · Q4 dispatch | Q1 push · Q2 topic-group, only when commit mode = (b) |
| 4-6 | Q1-Q2 items (≤3 each) · Q3 bulk | Q1 commit-mode · Q2 topic-group · Q3 dispatch · Q4 push |
| 7-9 | Q1-Q3 items (≤3 each) · Q4 bulk | Q1 commit-mode · Q2 topic-group · Q3 dispatch · Q4 push |
| 10-18 | Q1-Q3 items (first 9) · Q4 bulk → Call 2: Q1-Q3 items (remainder, ≤3 each) · Q4 bulk | Q1 commit-mode · Q2 topic-group · Q3 dispatch · Q4 push |
| ≥19 | context-budget mode below — no item checkboxes exist | Q1 push · Q2 over-20, only when more than 20 IDs are selected |

Checkbox mode holds at most 18 items (2 calls × 3 questions × 3 items). Decide the mode from the pending count **before** building Call 1; never widen a question past 3 items and never open a Call 3 to stretch checkbox mode further.

The two explicit zero-pending rows take precedence over the ≤3 row; its item checkboxes apply only with at least one pending item. The dispatch question is asked in **every** run with selectable pending or closed items and always shares a call with the commit-mode question, so the user sets how to commit and how to parallelize together. The ≤3 row spends its last slot on it, and topic-group moves to the follow-up, asked there only when commit mode = (b). The 4–18 pending-item bands ask commit-mode, topic-group, dispatch, and push together in one follow-up call; the topic-group answer is discarded unless commit mode = (b).

For lists with pending or resolved/addressed items, the push question rides a follow-up call; zero pending with no closed items instead uses its push-only first call. Run 2 never stops to ask about pushing. When no PR number exists (`report` mode without a PR# in its header), Step 10 never runs: omit the push question, and the ≤3 follow-up then fires when commit mode = (b) or more than 20 IDs were explicitly selected. The closed-only over-20 follow-up also fires without a PR number.

Bulk action resolving to (d) Skip all → discard the commit-mode, topic-group, **and dispatch** answers from the same call and issue no follow-up call (nothing will be committed, no specialist will be dispatched, and the run jumps to Step 11 without pushing). This satisfies the distinct-menus rule below — menus stay separate questions; only the round-trips merge.

**Bulk action — hard rule**: single-select, fixed options, **present in every selection call without exception** — Call 1 and Call 2 alike, positioned after that call's last item-checkbox question. A selection call without a bulk page is a defect, never a valid compression. Never put items in it. Items span ≤3 groups per call regardless of how many type categories exist.

```text
Bulk-action question — multiSelect: FALSE (single-select only — user picks one bulk action, not a checklist)
"Or choose a bulk action:"
  (a) +All [req] — implement all required items
  (b) +All [suggest] — implement all suggested items
  (c) ALL (req + suggest) — implement all pending items
  (d) Skip all — skip all items, exit
```

**ESSENTIAL — exactly these 4 options, verbatim, never substitute and never add** (empirically motivated: an observed run emitted an invented `Use my checked picks (Recommended)` option and dropped `+All [suggest]`). The checked-picks path needs no option — it is the "unanswered" branch below. Every selection call carries this menu; a call that omits it must be re-issued.

**Bulk-action resolution**:

- (a) → `SELECTED_ITEMS` = all pending `[req]` IDs (closed items excluded); skip Call 2 in two-call flow; proceed to commit-mode resolution
- (b) → `SELECTED_ITEMS` = all pending `[suggest]` IDs (closed items excluded); skip Call 2 in two-call flow; proceed to commit-mode resolution
- (c) → `SELECTED_ITEMS` = all pending [req+suggest] IDs (closed items excluded); skip Call 2; proceed to commit-mode resolution (do NOT hardcode `COMMIT_MODE` — scope and commit mode are orthogonal; user still chooses granularity)
- (d) → stop; print `→ All items skipped.`; jump to Step 11 (merged flow: discard the commit-mode answer from the same call)
- unanswered / "Type something" → use checked IDs from the item questions, plus every item id typed in any "Type something" field — the only way a closed (`resolved`/`addressed`) item is selected; typed ids naming `[info]`, legacy `[done]`, or unknown items are dropped with a one-line note; proceed to commit-mode resolution; `COMMIT_MODE = each` (default)

**Item checkbox questions**: each `multiSelect: true`, header "Items to implement:", labels: `<type> #<id>: <summary>` (≤55 chars), description: `<file:line> · @<author>` + for `location: discussion` items append `· thread (no GH resolve)`. Fill in severity order (≤3 items each — never 4, open another question instead). >9 pending items: two calls — print `→ N pending items — selecting in 2 calls` before Call 1, then build each call from the slot table above:

- **Call 1** = Q1-Q3 item checkboxes (items 1-9) + Q4 bulk action.
- **Call 2** = Q1-Q3 item checkboxes (remaining items, ≤3 each) + Q4 bulk action — the bulk menu repeats here, it is not carried over from Call 1.
- Any bulk answer other than "unanswered" in Call 1 → skip Call 2 entirely (scope already resolved).
- ≥19 pending → context-budget mode below instead, decided before Call 1; never open a Call 3.

**≥19 pending items — context-budget mode**: no per-item checkboxes in this branch. **MANDATORY, in this order — print first, ask second:** (1) print the compressed table (type · id · summary ≤40 chars · file) with every row in an assistant user-facing reply, not Bash/tool stdout, immediately before AskUserQuestion; same non-decorative/no-compression-substitute rule as Step 3c (Output-Routing exemption applies — never divert to `.temp`); (2) then issue ONE call: Q1 bulk action · Q2 commit-mode · Q3 topic-group · Q4 dispatch (all four slots; no item checkboxes exist in this mode); (3) once the bulk answer resolves to anything but (d) Skip all, issue the follow-up call: Q1 push · Q2 over-20, only when more than 20 IDs are selected. Threshold is 19 because checkbox mode tops out at 18 — this branch takes the whole layout, never a partial checkbox pass.

<!-- branch: main-path — commit-mode (same call in the ≤3-item merged layout; follow-up call with topic-group and dispatch for 4–18 pending items; skipped only when bulk action = (d) skip) -->

**Commit mode** — placed per the slot table above: same call in the closed-only branch and for ≤3 pending items, follow-up call (paired with topic-group and dispatch) for 4–18 pending items, one shared call in context-budget mode. In the follow-up flow ask it immediately after the bulk action resolves to (a), (b), (c), or unanswered (skip only when (d) skip-all). Commit mode is always the user's choice; item scope ((c) = all items) never implies a commit mode:

```text
AskUserQuestion: "Commit mode for selected items:"
  (a) Each item separately — one commit per action item (default)
  (b) By topic group — group related items into themed commits (grouping strategy asked next)
  (c) All at once — single commit after all items
  (d) Stage only — no commits; stay staged on PR branch (⚠ cannot cleanly restore to $SAVED_BRANCH after Step 11; governs Step 8 action-item commits only — the Steps 5–7 merge commit is unconditional and always created)
```

**ESSENTIAL — all 4 options mandatory, never emit fewer than 4** (empirically motivated: LLMs tend to drop (b) By topic group and (d) Stage only — both must appear every time). Distinct menu from bulk-action question, never merge or pull its options in — this menu sets commit MODE (how to commit), bulk action sets item SCOPE (which items). Sharing one AskUserQuestion call is fine; sharing one menu never is.

Set `COMMIT_MODE`:

- (a) → `each`
- (b) → `grouped`
- (c) → `all`
- (d) → `stage`
- unanswered → `each` (default)

**Topic-group question** — always present in the SAME call as the commit-mode menu wherever the slot table leaves room (the follow-up call for 4–18 pending items, the closed-only branch, and the ≥19 single call): the commit-mode answer is unknown when that call is built, so the question is asked unconditionally there and its answer discarded silently unless commit mode resolves to (b) — same pattern as the skip-all discard. For `≤3` items the first call is already full, so ask it in the follow-up call beside the push question, and only when commit mode = (b). Options are grouping strategies, not free-text labels: the orchestrator already knows each item's `change` category and `file`, so it proposes concrete groupings and only falls back to typing. Typed labels are collected here rather than at Step 8 — the same decision over the same item list (HEAD's Step 8 label question listed `SELECTED_ITEMS`), and a label question after implementation would park the unattended Run 2.

```text
Topic-group question — multiSelect: FALSE
"If 'By topic group' — how should items group?"
  (a) By change domain — one commit per `change` category (perf, docs, test, ...)
  (b) By file/module — one commit per touched file or package
  (c) By specialist domain — mirrors the Step 8 Phase 2 dispatch groups
  (d) Let me type labels — via "Type something" as `<id>=<topic>` pairs, e.g. `1=style 2=logic 3=tests`
```

Set `GROUP_STRATEGY`: (a) → `domain` · (b) → `file` · (c) → `specialist` · typed `<id>=<topic>` pairs → `labels` · typed `auto` → `domain` (same `change`-field mapping) · unanswered → `domain` (default). `COMMIT_MODE` ≠ `grouped` → discard; `GROUP_STRATEGY` unused.

**(d) picked with no pairs typed, and commit mode = (b)** → the labels are still owed: ask the **Topic-label question** below in the next Step 3d call (alone, or beside any question that call still has room for). It is the same question Step 8 used to ask after implementation, over the same `SELECTED_ITEMS` list — asked now so Run 2 stays unattended:

```text
AskUserQuestion: "Assign a topic label to each implemented item (e.g. style, logic, tests, docs, config).
Items implemented:
  <for each item in SELECTED_ITEMS: "#<id>: <summary>">
Type a topic for each item ID (e.g. '1=style 2=logic 3=tests'), or type 'auto' to infer labels from change field."
```

- Typed `<id>=<topic>` pairs → `labels`. Typed `auto` → `domain` (the same `change`-field mapping).
- Skipped (empty response or blank) → fall back to `each`, exactly as the Step 8 question did: run the `(a)` each commit-mode block instead of `grouped` and print `⚠ no labels typed — committing each item separately`.

`labels` only: write the pairs with the Write tool to `$IMPL_DIR/group-labels.tsv`, one `<id>\t<topic>` row per pair — numeric ids only, topic lowercased with every character outside `[a-z0-9-]` replaced by `-`. The Write tool, not a shell `echo`: the topics are user free text, and Step 8's group commit reads this file instead of asking again.

**Dispatch-granularity question** — placed per the slot table above, asked when pending or resolved/addressed items are available (omitted on the empty-list push-only path; bulk action = (d) skip-all discards the answer, as for topic-group). It sets **wave width and sub-group size only**: specialist routing, the file-ownership tiebreak, and the import-coupling merge are correctness guards, never widened or dropped by any answer. The ≤5-items-per-group split is width, so `(c)` does drop it — deliberately, at the stall risk its own label states.

```text
Dispatch-granularity question — multiSelect: FALSE
"Phase 2 runs specialists in isolated worktrees. How should the work spread?"
  (a) Auto — one worktree per specialist, split at ≤5 items, pool-capped waves (default)
  (b) Sequential — same groups, one worktree at a time
  (c) Per specialist — one worktree per specialist, no ≤5 split (⚠ >~10 items in one worktree can stall)
  (d) Custom — show the computed groups first, then choose from these
```

Set `DISPATCH_MODE`:

- (a) → `auto` · (b) → `sequential` · (c) → `per-specialist` · (d) → `preview` · unanswered → `auto` (default)
- Groups cannot be shown here: Phase 2 forms them from `SURVIVING_ITEMS` after Phase 1's challenge verdicts, so a concrete list does not exist at Step 3d. (d) Custom is the only path to approving real groups and costs one extra gate at the Phase 1 → Phase 2 boundary; the other three answers keep this run gate-free from here to dispatch.
- `preview` is not a width. At that boundary `action-item-dispatch.md` prints the formed groups and re-asks (a)/(b)/(c), then the orchestrator runs the matching block below a second time to record the resolved width.

`(a)` auto:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo auto > "${TMPDIR:-/tmp}/resolve-dispatch-mode-${CSID}"  # timeout: 3000
```

`(b)` sequential:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo sequential > "${TMPDIR:-/tmp}/resolve-dispatch-mode-${CSID}"  # timeout: 3000
```

`(c)` per specialist:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo per-specialist > "${TMPDIR:-/tmp}/resolve-dispatch-mode-${CSID}"  # timeout: 3000
```

`(d)` custom — records `preview`, the internal value the boundary gate keys on:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo preview > "${TMPDIR:-/tmp}/resolve-dispatch-mode-${CSID}"  # timeout: 3000
```

Persist every answer once the menus resolve — Step 8's merge fence passes `--commit-mode` to `merge_specialist_batch.py`, its after-loop grouping reads the strategy (and `group-labels.tsv` for `labels`), Phase 2 reads the dispatch mode, and Steps 10–11 read the push and post-PR answers; none survives a fence boundary or a compaction on its own.

<!-- policy-sibling: plugins/CLAUDE.md §Blueprint Blocks (canonical), plugins/cc_foundry/agents/challenger.md, plugins/cc_oss/skills/resolve/SKILL.md (Step 3d, Step 10), plugins/cc_oss/skills/review/SKILL.md (reject gate) -->

Run **exactly one** commit-mode block — the one matching the user's answer — then, for `grouped` only, exactly one strategy block. Never edit a block's text to a different value: an edited block misses the blueprint manifest, and a block run unedited silently persists the wrong mode (a real run selected grouped, landed 12 per-item commits because the old single block carried `each` as its literal default).

`(a)` each:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo each > "${TMPDIR:-/tmp}/resolve-commit-mode-${CSID}"  # timeout: 3000
```

`(b)` grouped:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo grouped > "${TMPDIR:-/tmp}/resolve-commit-mode-${CSID}"  # timeout: 3000
```

`(c)` all:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo all > "${TMPDIR:-/tmp}/resolve-commit-mode-${CSID}"  # timeout: 3000
```

`(d)` stage:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo stage > "${TMPDIR:-/tmp}/resolve-commit-mode-${CSID}"  # timeout: 3000
```

Strategy — `grouped` only; skip otherwise (Step 0 already wrote the `domain` default):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo domain > "${TMPDIR:-/tmp}/resolve-group-strategy-${CSID}"  # timeout: 3000
```

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo file > "${TMPDIR:-/tmp}/resolve-group-strategy-${CSID}"  # timeout: 3000
```

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo specialist > "${TMPDIR:-/tmp}/resolve-group-strategy-${CSID}"  # timeout: 3000
```

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo labels > "${TMPDIR:-/tmp}/resolve-group-strategy-${CSID}"  # timeout: 3000
```

<!-- branch: main-path — push (follow-up call in every band; one push-only call only at zero pending with no closed items; omitted when no PR number exists) -->

**Push question** — placed per the slot table above; asked whenever a PR number exists. It records **push intent only**, plus the post-PR action. It is never push authorization: commits do not exist yet, so the scope the push-safety rule (`git-commit.md`, "Never push without explicit user confirmation") requires the user to see — diff stat, commit count, last subject — cannot be shown here. Intent "push" means Step 10 asks the scope-bearing push confirmation; an explicit "don't push" is itself an answer, and Step 10 then skips its question silently. Name what is knowable now — target `$FORK_REMOTE/$HEAD_REF` when Step 4 already ran (`pr`, `pr+report`), otherwise `the head branch of PR #<N>` (`report` mode checks out after this gate) — plus the selected item count and commit mode.

```text
Push question — multiSelect: FALSE
"After the lint/QA gate passes, push to <target>? (Step 10 shows the diff stat and asks you to confirm before pushing.)"
  (a) Push (confirm at Step 10), then open the PR in the browser
  (b) Push (confirm at Step 10) (Recommended)
  (c) Don't push — I'll push manually; open the PR in the browser
  (d) Don't push — I'll push manually
```

Unanswered or dismissed → run **no** push block and **no** post-PR block: the push sentinel stays `unset` and post-PR stays at Step 1's `skip`, so Step 10 asks both push and post-PR at push time exactly as before — no answer is never a silent skip. Otherwise run **exactly one** push block and **exactly one** post-PR block — never edit a block's value (same trap as the commit-mode blocks above: an unedited default silently wins):

| Answer | Push block | Post-PR block |
| -- | -- | -- |
| (a) | push | open |
| (b) | push | skip |
| (c) | skip | open |
| (d) | skip | skip |
| unanswered / dismissed | none — stays `unset`, Step 10 asks push + post-PR | none |

Push = push:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo push > "${TMPDIR:-/tmp}/resolve-push-auth-${CSID}"  # read back at Step 10  # timeout: 3000
```

Push = skip:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo skip > "${TMPDIR:-/tmp}/resolve-push-auth-${CSID}"  # read back at Step 10  # timeout: 3000
```

Post-PR = open:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo open > "${TMPDIR:-/tmp}/resolve-post-pr-action-${CSID}"  # read back at the end of Step 11  # timeout: 3000
```

Post-PR = skip:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo skip > "${TMPDIR:-/tmp}/resolve-post-pr-action-${CSID}"  # read back at the end of Step 11  # timeout: 3000
```

**Batch the answer blocks.** The matching commit-mode, strategy, dispatch, push and post-PR blocks each write a different file, so issue all of them in **one response**, never one block per turn. Run the confirm block below in the next response, beside the Step 7b join's first tool call — the echoed line must match the user's answers before Step 3e starts:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _CM < "${TMPDIR:-/tmp}/resolve-commit-mode-${CSID}" 2>/dev/null || _CM="unset"
IFS= read -r _GS < "${TMPDIR:-/tmp}/resolve-group-strategy-${CSID}" 2>/dev/null || _GS="unset"
IFS= read -r _DM < "${TMPDIR:-/tmp}/resolve-dispatch-mode-${CSID}" 2>/dev/null || _DM="unset"
IFS= read -r _PA < "${TMPDIR:-/tmp}/resolve-push-auth-${CSID}" 2>/dev/null || _PA="unset"
IFS= read -r _PP < "${TMPDIR:-/tmp}/resolve-post-pr-action-${CSID}" 2>/dev/null || _PP="unset"
echo "commit-mode=$_CM group-strategy=$_GS dispatch-mode=$_DM push=$_PA post-pr=$_PP"  # timeout: 3000
```

**Over-20 selection gate** — applies to the final selected IDs in every pending-count band and in report mode without a PR number: explicitly typed closed IDs can exceed 20 even when there are no pending items. After bulk resolution and before creating any item tasks, count `SELECTED_ITEMS`; when more than 20 IDs are selected, add this question to the next follow-up `AskUserQuestion` call if a slot is free, otherwise ask it alone in another call. Omit only push when no PR number exists; never omit this decision: "More than 20 items were selected; one resolve pass can handle at most 20. What should run now?" Options: (a) Apply the first 20 selected items in the displayed severity/priority order now, then rerun for the remaining items; (b) Stop and reselect at most 20 items. For (a), set `SELECTED_ITEMS` to exactly those first 20 selected IDs, print the deferred IDs, and tell the user to rerun for the remaining items. For (b) or no answer, stop without creating tasks and discard the push answer from the same call. Never silently trim a bulk choice, run a second batch inside this pass, or pass more than 20 IDs to Step 3e.

## Step 7b join: collect conflict resolutions — opens Run 2

First work of Run 2, before any item task is created. In the same response as the join's first tool call: `TaskUpdate(task_id=TASK_SELECT, status="completed")`; `TaskUpdate(task_id=TASK_CONFLICT, status="completed")` rides with the merge commit call (§Zero bookkeeping-only turns). The Steps 6–7a agents dispatched beside the gate have been running through it; run the §Agent wait discipline check, then collect them (`### 7b: Verify and complete merge` in `conflict-resolution.md`): confirm `git diff --name-only --diff-filter=U` is empty, no residual conflict markers remain staged, mark each conflict task `completed` (all in one response), and commit the merge. A group that returned nothing is `timed_out` — surface it with ⏱ and stop before Step 8 rather than implementing on an unmerged tree.

Nothing dispatched (no conflicted files, or `report` mode with no PR#) → no-op, continue to Step 3e.

## Step 3e: Create tasks for selected items

`report` mode skips Step 3e, whether or not the report header names a PR. Step 3a already persisted its action items, and Step 8's report-mode task handling expects no `item-tasks.tsv`. Continue to Step 8 — Steps 4–7 already ran back in Run 1 when a PR# was found, and are skipped entirely when none was. `pr` and `pr+report` create per-item tasks below.

For each item in `SELECTED_ITEMS`, call `TaskCreate` **once per item** — one task per action item; scoped to selected items only, not all pending (avoids bloat when 20+ items exist but only a subset is selected). Issue every item's `TaskCreate` in **one response**, never one per turn — the same response as the Step 7b join's first tool call, so it is never a bookkeeping-only turn:

```text
TaskCreate(
  subject="<type> <summary> — PR #<number>",   # <type> = full string with brackets, e.g. "[gh][req] rename param — PR #42"
  description="Author: @<author> | Change: <change> | Severity: <severity> | File: <file:line or '—'> | <full_comment_text>",
  activeForm="Implementing: <summary>"          # <summary> truncated to 80 chars
)
```

Store returned task ID in each `SELECTED_ITEMS` entry as `task_id`, **then write the whole map in one Write tool call** to `$IMPL_DIR/item-tasks.tsv` — one `<item_id>\t<task_id>` row per selected item, in the response right after the creates return (beside the Step 8 `cat` load), never one append per item. The file is the map; the Step 8 loop reads task IDs from it (a compaction between here and Step 8 would otherwise orphan every per-item task). Then validate it once:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" 2>/dev/null || IMPL_DIR=""
[ -n "$IMPL_DIR" ] || { echo "! BLOCKED — IMPL_DIR sentinel missing; Step 3b never ran"; exit 1; }
[ -s "$IMPL_DIR/item-tasks.tsv" ] || { echo "! BLOCKED — item-tasks.tsv missing or empty; write it before Step 8"; exit 1; }
# every row: numeric item id, non-empty task id, no unsubstituted <placeholder>; an empty or non-numeric id would be
# parsed by every downstream numeric-only guard as a wrong-but-valid-looking item_id
awk -F'\t' 'NF != 2 || $1 !~ /^[0-9]+$/ || $2 == "" || $0 ~ /[<>]/ || seen[$1]++ { bad = bad " " NR } END { if (bad) { print "! BLOCKED — malformed or duplicate item-tasks.tsv row(s):" bad; exit 1 } print "item-tasks.tsv rows: " NR }' "$IMPL_DIR/item-tasks.tsv"  # timeout: 3000
```

**Applies to `pr` and `pr+report` modes only** — these run Step 3b (which initialises `IMPL_DIR`) and Step 3e. `report` mode skips both steps and has no per-item tasks; Step 3a initialises `IMPL_DIR` instead.

## Step 4: Checkout PR branch

> **Run 1** — entered from Step 3b's overlap directive, in the same turn that spawned `INTEL_AGENT`, not after Step 3e. Needs only `PR_NUMBER`.

**Worktree isolation (opt-in `--worktree`)** — run FIRST, before `gh` check + checkout below, so checkout, Phase-2 specialist worktrees, cherry-picks, and push all happen off an isolated worktree and caller's main tree/branch never change. Skip when `WT_ENABLED != true` or `MODE = report` with no PR#.

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r WT_ENABLED < "${TMPDIR:-/tmp}/oss-resolve-worktree-${CSID}" 2>/dev/null; [ "$WT_ENABLED" = "true" ] || WT_ENABLED=false
IFS= read -r _OSS_SHARED < "${TMPDIR:-/tmp}/resolve-oss-shared-${CSID}" 2>/dev/null || _OSS_SHARED="$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/resolve_shared_path.py" oss skills/_shared 2>/dev/null)"
[ "$WT_ENABLED" = "true" ] && [ -f "$_OSS_SHARED/worktree-isolation.md" ] && cat "$_OSS_SHARED/worktree-isolation.md"  # timeout: 5000
```

`WT_ENABLED=true` → follow §Enter (base off HEAD, `EnterWorktree(path=…)`) + §resolve (do NOT alter checkout/mutex/fingerprint/push — Enter is the only addition; the mutex path is worktree-invariant, Step 11 restore becomes a harmless no-op, and the push still targets the fork). Then continue Step 4 below inside the worktree.

*Skip only when `MODE = report` with no PR# (`$PR_NUMBER` unset — no remote branch to check out). In pr mode, runs unconditionally regardless of `SELECTED_ITEMS` — conflict resolution must happen even when 0 action items selected.*

When skipping, both ride with the next real call: `TaskUpdate(task_id=TASK_CHECKOUT, status="deleted")`, `TaskUpdate(task_id=TASK_CONFLICT, status="deleted")`. Otherwise `TaskUpdate(task_id=TASK_CHECKOUT, status="in_progress")` rides with the branch-safety block below, and `TaskUpdate(task_id=TASK_CHECKOUT, status="completed")` with the FORK_REMOTE block.

**Branch-safety pre-check** — must run BEFORE `gh pr checkout` so a wrong-branch commit is impossible (per `git-commit.md` Gate 2). Verify PR's `headRefName` isn't repo's default branch — `gh pr checkout` of a same-repo PR whose HEAD = default branch would land on default; any later commit (Step 8) would violate Gate 2:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PR_NUMBER < "${TMPDIR:-/tmp}/resolve-pr-number-${CSID}" 2>/dev/null || PR_NUMBER=""
case "$PR_NUMBER" in ''|n/a|*[!0-9]*) echo "⛔ Step 4 PR number sentinel missing or invalid; refusing checkout"; exit 1 ;; esac
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/resolve_pr_refs.py" --pr "$PR_NUMBER"  # timeout: 15000
```

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# gh = hard prereq: gh pr checkout has no fallback path (folded here — its own block cost a call per run)
command -v gh >/dev/null 2>&1 || { echo "! BLOCKED — gh CLI required; install: https://cli.github.com"; exit 1; }
# fresh shell (Check 41) — reload what resolve_pr_refs.py persisted above
IFS= read -r PR_NUMBER < "${TMPDIR:-/tmp}/resolve-pr-number-${CSID}" 2>/dev/null || PR_NUMBER=""
case "$PR_NUMBER" in ''|n/a|*[!0-9]*) echo "⛔ Step 4 PR number sentinel missing or invalid; refusing checkout"; exit 1 ;; esac
IFS= read -r PR_HEAD_REF < "${TMPDIR:-/tmp}/resolve-head-ref-${CSID}" 2>/dev/null || PR_HEAD_REF=""
IFS= read -r PR_HEAD_OID < "${TMPDIR:-/tmp}/resolve-pr-head-oid-${CSID}" 2>/dev/null || PR_HEAD_OID=""
# SHA-first: skip if at PR head — avoids worktree conflict (gh pr checkout aliases pr-N-slug if branch active elsewhere)
IFS= read -r LOCAL_SHA < "${TMPDIR:-/tmp}/resolve-local-sha-${CSID}" 2>/dev/null || LOCAL_SHA=""
if [ -n "$PR_HEAD_OID" ] && [ "$LOCAL_SHA" = "$PR_HEAD_OID" ]; then
    echo "→ Already at PR head ($LOCAL_SHA) — skipping gh pr checkout"
    # SHA match, diff branch (e.g. pr<N> alias) — force-align to PR_HEAD_REF so Step8/10 land correct branch
    CURRENT=$(git branch --show-current 2>/dev/null)
    if [ -n "$PR_HEAD_REF" ] && [ "$CURRENT" != "$PR_HEAD_REF" ]; then
        echo "→ Re-aligning local branch: $CURRENT → $PR_HEAD_REF (same SHA $LOCAL_SHA)"
        git switch "$PR_HEAD_REF" 2>/dev/null \
            || git switch -c "$PR_HEAD_REF" "$LOCAL_SHA" \
            || { echo "⛔ Cannot switch to $PR_HEAD_REF — aborting (branch active in another worktree?)"; exit 1; }
    fi
else
    # hard-exit on failure — else HEAD_REF set but git stuck on caller branch, Step8 commits land wrong branch
    # --branch required: w/o it gh CLI v2.93+ falls back to pr<N> alias on collision → Step10 push makes unrelated branch (CRITICAL bug pyDeprecate 2026-06-13T08:33Z)
    gh pr checkout "$PR_NUMBER" --branch "$PR_HEAD_REF" \
        || { echo "⛔ gh pr checkout failed — aborting (network, branch deleted, auth expired, or local conflicts)"; exit 1; }   # timeout: 15000
fi
# verify in the same call — checkout must land on the expected branch, else abort before Step 8 can commit
HEAD_REF="$PR_HEAD_REF"
IFS= read -r IS_CROSS_REPO < "${TMPDIR:-/tmp}/resolve-is-cross-repo-${CSID}" 2>/dev/null || IS_CROSS_REPO=""
[ -n "$HEAD_REF" ] && [ -n "$IS_CROSS_REPO" ] || { echo "⛔ Step 4 verify: HEAD_REF/IS_CROSS_REPO sentinels missing — checkout state unverifiable, aborting before Step 8 can commit"; exit 1; }
git remote -v | grep '(fetch)' | head -10 # timeout: 3000
git status  # timeout: 3000
CURRENT_BRANCH=$(git branch --show-current 2>/dev/null)  # timeout: 3000
# same-repo: branch must equal PR_HEAD_REF, no alias — gh falls back to pr<N> on collision; assert as hard gate
if [ "$IS_CROSS_REPO" = "false" ] && [ "$CURRENT_BRANCH" != "$PR_HEAD_REF" ]; then
    echo "⛔ SAME-REPO RULE VIOLATION: on '$CURRENT_BRANCH' but PR headRefName='$PR_HEAD_REF' — branch alias (pr<N>) created instead of using original branch. Aborting to prevent push to wrong branch."
    exit 1
fi
[ "$CURRENT_BRANCH" = "$HEAD_REF" ] || { echo "⛔ checkout did not land on $HEAD_REF (current: $CURRENT_BRANCH) — aborting before Step 8 can commit to wrong branch"; exit 1; }  # timeout: 3000
```

Determine `FORK_REMOTE` for push in Step 10:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PR_NUMBER < "${TMPDIR:-/tmp}/resolve-pr-number-${CSID}" 2>/dev/null || PR_NUMBER=""
case "$PR_NUMBER" in ''|n/a|*[!0-9]*) echo "⛔ Step 4 PR number sentinel missing or invalid; refusing PR reference"; exit 1 ;; esac
: > "${TMPDIR:-/tmp}/resolve-pr-ref-${CSID}"
: > "${TMPDIR:-/tmp}/resolve-fork-remote-${CSID}"
IFS= read -r IS_CROSS_REPO < "${TMPDIR:-/tmp}/resolve-is-cross-repo-${CSID}" 2>/dev/null || IS_CROSS_REPO="false"
if [ "$IS_CROSS_REPO" = "true" ]; then
    IFS= read -r FORK_REMOTE < "${TMPDIR:-/tmp}/resolve-head-repo-owner-${CSID}" 2>/dev/null || FORK_REMOTE=""
    [ -n "$FORK_REMOTE" ] || FORK_REMOTE=$(gh pr view "$PR_NUMBER" --json headRepositoryOwner --jq .headRepositoryOwner.login) # sentinel-miss fallback only # timeout: 6000
    PR_URL=$(gh pr view "$PR_NUMBER" --json url --jq .url) || { echo "⛔ Could not resolve fork PR URL"; exit 1; } # timeout: 6000
    [[ "$PR_URL" =~ ^https://[^/]+/[^/]+/[^/]+/pull/${PR_NUMBER}$ ]] || { echo "⛔ Fork PR URL does not match PR number"; exit 1; }
    PR_REF="$PR_URL"
else
    FORK_REMOTE="origin"
    PR_REF="#$PR_NUMBER"
fi
echo "$PR_REF" > "${TMPDIR:-/tmp}/resolve-pr-ref-${CSID}"  # timeout: 3000
echo "$FORK_REMOTE" > "${TMPDIR:-/tmp}/resolve-fork-remote-${CSID}"  # read by Step 3d push question + Step 10 push
# soft-verify — layouts vary across gh versions
git remote get-url "$FORK_REMOTE" >/dev/null 2>&1 \
    || echo "⚠ Remote $FORK_REMOTE not registered — Step 10 will add it before push" # timeout: 3000
```

`gh pr checkout` auto-handles forks — adds contributor's remote, configures tracking. `FORK_REMOTE`: contributor login (e.g. `alice`) for forks, `origin` for same-repo. Push always `git push` — tracking configured by `gh pr checkout`.

`PR_REF`: the token Step 8's commit messages embed for this PR — `#<N>` when the commit lands same-repo (`FORK_REMOTE=origin`), or the full `PR_URL` when it lands in the contributor's fork (bare `#N` there would resolve against the fork's own issues, not this repo's PR — a cross-repo false link). Persisted to `${TMPDIR:-/tmp}/resolve-pr-ref-${CSID}` for Step 8 to read.

## Steps 5–7: Conflict detection, context, and resolution

<!-- Steps 5–7 defined in conflict-resolution.md — see that file for sub-step numbering -->

`TaskUpdate(task_id=TASK_CONFLICT, status="in_progress")` — rides with the load block below; it flips to `completed` at the Step 7b join.

> **Split across two dispatch points, one loaded file.** Step 5 runs in Run 1 immediately after Step 4, beside the intel agent. Steps 6–7a are dispatched in Run 1's gate turn (Step 3d overlap directive); 7b is collected at the Step 7b join that opens Run 2. Load the file once here and execute the parts at their own points — re-`cat` it only if a compaction dropped it from context.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _OSS_RESOLVE < "${TMPDIR:-/tmp}/resolve-oss-resolve-${CSID}" 2>/dev/null || _OSS_RESOLVE=""  # reload (Check 41)
cat "$_OSS_RESOLVE/modes/conflict-resolution.md"  # timeout: 5000
```

Execute its steps (loaded above) at their dispatch points.

## Step 8: Implement action items

*Skip when `SELECTED_ITEMS` is empty — jump to Step 9.*

When skipping, `TaskUpdate(task_id=TASK_IMPL, status="deleted")` rides with Step 9's first call. Otherwise `TaskUpdate(task_id=TASK_IMPL, status="in_progress")` rides with the codemap index block below.

**Codemap index identity (if `CODEMAP_ENABLED=true`)**: resolve the index path the next block reuses. No query runs here — per-item blast radius is action-item-dispatch.md's **Pre-loop blast-radius scan**, which resolves each item's canonical module first and passes it as `rdeps`' positional argument.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CODEMAP_ENABLED < "${TMPDIR:-/tmp}/resolve-codemap-enabled-${CSID}" 2>/dev/null || CODEMAP_ENABLED="false"  # timeout: 3000
if [ "$CODEMAP_ENABLED" = "true" ]; then
    # index dir anchors at git root, not cwd — subdir invocation otherwise misses an index that exists. _PROJ = raw basename; scanner writes it unsanitized, so `tr -cd` would seek a filename it never wrote.
    _ROOT=$(git rev-parse --show-toplevel 2>/dev/null); [ -n "$_ROOT" ] || _ROOT="$PWD"
    _PROJ=$(basename "$_ROOT")
    _IDX="${CODEMAP_INDEX_DIR:-$_ROOT/.cache/codemap}"
fi
```

Blast radius, top callers and coupling pairs reach each implementation agent through action-item-dispatch.md's own `ITEM_CALLERS` context, not from this step.

**Review pre-flight cache** — reuse per-module codemap answers `/review` already computed, so Step 8 blast-radius scan issues 0 duplicate pre-flight queries when a fresh review artifact exists (contract + artifact shape in `$_DEV_SHARED/codemap-context.md` §Review→resolve pre-flight cache; requires `develop`/`oss` codemap wiring). Locate latest review run-dir, materialize per-module cache once, before per-item loop:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
CODEMAP_CACHE_DIR=""
if [ "$CODEMAP_ENABLED" = "true" ]; then
    _IDX_FILE="${_IDX}/${_PROJ}.json"  # both set above; git-root-anchored
    CODEMAP_CACHE_DIR=".temp/resolve/codemap-context"  # resolve-owned; stable across the run
    mkdir -p "$CODEMAP_CACHE_DIR"  # timeout: 3000
    # review's pre-flight blob: .temp/review/<ts>/codemap-context.md
    _REVIEW_CTX=$(ls -t .temp/review/*/codemap-context.md 2>/dev/null | head -1)
    if [ -n "$_REVIEW_CTX" ] && [ -f "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/codemap_cache.py" ]; then
        # .md wraps codemap-py query batch JSON under md headers — extract
        _BATCH_JSON="${TMPDIR:-/tmp}/resolve-review-batch-${CSID}.json"
        sed -n '/^{/,$p' "$_REVIEW_CTX" | head -1 > "$_BATCH_JSON" 2>/dev/null || true
        if [ -s "$_BATCH_JSON" ] && [ -f "$_IDX_FILE" ]; then
            python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/codemap_cache.py" write \
                --batch "$_BATCH_JSON" --index "$_IDX_FILE" --cache-dir "$CODEMAP_CACHE_DIR" 2>/dev/null || true  # timeout: 5000
            echo "→ Review pre-flight cache materialized from $_REVIEW_CTX"
        fi
    fi
fi
echo "${CODEMAP_CACHE_DIR}" > "${TMPDIR:-/tmp}/resolve-codemap-cache-dir-${CSID}"  # timeout: 3000
```

`action-item-dispatch.md`'s per-item blast-radius scan reads this cache first (freshness-gated `codemap_cache.py read`) and only calls `codemap-py query` on a cache miss — see its **Pre-loop blast-radius scan**. Empty `CODEMAP_CACHE_DIR` (no review artifact, or oss helper absent) → every module is a cache miss and the scan queries live, unchanged from prior behaviour.

<!-- Step 8 defined in action-item-dispatch.md -->

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _OSS_RESOLVE < "${TMPDIR:-/tmp}/resolve-oss-resolve-${CSID}" 2>/dev/null || _OSS_RESOLVE=""  # reload (Check 41)
cat "$_OSS_RESOLVE/modes/action-item-dispatch.md"  # timeout: 5000
```

`action-item-dispatch.md` (loaded above) — execute its prelude (IMPL_AGENT routing, IMPL_DIR init, blast-radius scan, plus a branch mutex + HEAD fingerprint so a second concurrent resolve aborts and an external mid-flight write surfaces at merge-back), then run its three-phase dispatch directly in the orchestrator:

1. Phase 1 challenge (parallel by domain, read-only).
2. Phase 2 implementation (parallel, one isolated `git worktree` per specialist; groups formed by specialist then a file-ownership + import-coupling tiebreak so items that would collide on same file — or across an import edge — land in one worktree).
3. Phase 3 merge-back (sequential cherry-pick, whole worktree groups ordered most-central-first so foundational commits land before dependents, `TaskUpdate` per item as its commit lands).

`TaskUpdate` calls stay orchestrator-owned throughout — Phase 1/2 subagents never touch task list (subagent can't drive parent's task list); only Phase 3, run by orchestrator itself after each cherry-pick, flips a task to `completed`. Explains why tasks flip in item-priority order during Phase 3 even though the work producing them ran concurrently in Phase 2.

`action-item-dispatch.md` caps a single pass at 20 items. Step 3d asks before task creation when a bulk or checkbox selection exceeds 20; only the chosen first 20 can enter Step 8, and the rest require a later invocation. Step 8's prelude rejects more than 20 IDs if that earlier gate was missed.

**Straggler gate — before leaving Step 8**: `action-item-dispatch.md`'s per-item close-out (REJECT, skipped, cherry-pick landed — including the C1 medium-effort Codex-direct shortcut, which never enters Phase 1/2/3 at all) should have already terminated every id in `item-tasks.tsv`; this catches whichever one didn't. Never move on to Step 9 over an open child — that hid the original leak. Fails closed on a lost `IMPL_DIR` sentinel: distinct from "no items were selected," which the file's own absence still reports safely.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" 2>/dev/null || IMPL_DIR=""
[ -n "$IMPL_DIR" ] || { echo "! BLOCKED — IMPL_DIR sentinel missing; cannot verify child tasks before leaving Step 8"; exit 1; }
if [ -f "$IMPL_DIR/item-tasks.tsv" ]; then
    _SKIPPED_IDS=$(cut -f1 "$IMPL_DIR/skipped-items.txt" 2>/dev/null)
    # anchored right after id= — resolution= is the field placed there, before any free-text field
    # (action-item-dispatch.md's producer template), so a reviewer's quoted text can never match it
    _REJECTED_IDS=$(grep -iE '^id[[:space:]]*=[[:space:]]*[0-9]+[[:space:]]+resolution[[:space:]]*=[[:space:]]*rejected' "$IMPL_DIR/challenge-log.txt" 2>/dev/null | sed -n 's/^id[[:space:]]*=[[:space:]]*\([0-9]*\).*/\1/p')
    while IFS=$'\t' read -r item_id task_id; do
        case "$item_id" in ''|*[!0-9]*) continue ;; esac
        { printf '%s\n' "$_SKIPPED_IDS" "$_REJECTED_IDS" | grep -qx "$item_id"; } && printf 'closed: item=%s (rejected/skipped)\n' "$item_id" && continue
        [ -n "$task_id" ] || { echo "! skipping — item $item_id has an empty task id in item-tasks.tsv"; continue; }
        printf 'check: item=%s task=%s\n' "$item_id" "$task_id"  # not rejected/skipped — must have landed a commit; verify below, never assume
    done < "$IMPL_DIR/item-tasks.tsv"
else
    echo "n/a — report mode or no items selected"
fi
# timeout: 3000
```

- Bash printed `n/a` → no per-item tasks were created, proceed straight to the flip below.
- `closed:` lines need nothing further.
- A `! skipping` line names a malformed row (empty task id) — investigate `item-tasks.tsv` directly via `TaskList`/`grep` for that item id before proceeding; never guess its status.
- For every `check:` line: call `TaskList`; if that task is already `completed`/`deleted`, done. If it's still open, do NOT default it to `completed` — confirm independently that item's commit is actually on the branch before calling `TaskUpdate(status="completed")`. The confirmation command depends on `COMMIT_MODE` — only `each` carries a per-item attribution token; `grouped` folds several ids into one message; `all`/`stage` carry none at all (`stage` never commits — the diff stays staged, per its own contract). A lost `resolve-base-sha` sentinel degrades the confirmation to an unscoped search across the whole branch, which can false-confirm a never-implemented item against a prior run's commit on the same branch — the block below warns and treats any match with extra suspicion in that case; both reads and the mode dispatch happen inside it, not by hand:

Run once per printed `check:` line, substituting that line's `item_id`:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r COMMIT_MODE < "${TMPDIR:-/tmp}/resolve-commit-mode-${CSID}" 2>/dev/null || COMMIT_MODE="each"
IFS= read -r _BASE_SHA < "${TMPDIR:-/tmp}/resolve-base-sha-${CSID}" 2>/dev/null || _BASE_SHA=""
[ -n "$_BASE_SHA" ] || echo "⚠ resolve-base-sha sentinel missing — confirmation below is unscoped, verify the matched commit's date/author before trusting it"
_ITEM_ID="<item_id from a printed check: line>"
case "$_ITEM_ID" in '<'*'>') echo "! BLOCKED — item_id placeholder not substituted"; exit 1 ;; esac
case "$COMMIT_MODE" in
    each)
        # anchored to the literal attribution token commit_action_item.py --build emits, and
        # range-bound to this run's commits — an unanchored, unscoped grep can match a different
        # item's id as a substring (No.1 inside No.12) or a prior run's commit on the same branch
        git log --oneline -E --grep="\[resolve No\.${_ITEM_ID}\]" ${_BASE_SHA:+"$_BASE_SHA..HEAD"}  # timeout: 5000
        ;;
    grouped)
        # tokenize, exact-match — no boundary regex at all. Two prior attempts at this command were
        # both wrong, in opposite directions: (1) --grep="items .*\b${_ITEM_ID}\b" never matches
        # anything (\b is a GNU extension, does not compile under `git log -E`'s POSIX ERE); (2) a
        # boundary-regex replacement against --oneline output is fail-open — --oneline prints only
        # the commit subject, never the body where the item list lives, so it matches stray digits
        # in the abbreviated sha or unrelated subject text and false-confirms items that never
        # landed. --format=%B reads the full body (where "[resolve group] … items <ids>" actually
        # is), isolates that one line per commit, splits it into whitespace-delimited tokens, and
        # requires an EXACT token match — a group's own "PR #1" text can never satisfy grep -qx
        # against a bare ${_ITEM_ID}, and "30" can never satisfy a check for "3". Verified against a
        # live multi-commit repo (two group commits, ids "30 4" and "7 8"): every real id matched,
        # "1" (present only inside "PR #1", not the items list) did not.
        git log --format=%B --grep="items" ${_BASE_SHA:+"$_BASE_SHA..HEAD"} \
            | grep -E '^\[resolve group\]' | tr ' ' '\n' | grep -qx "${_ITEM_ID}" \
            && echo "MATCH — item ${_ITEM_ID} found in a group commit" \
            || echo "NO MATCH — item ${_ITEM_ID} not found in any group commit"  # timeout: 5000
        ;;
    all|stage)
        echo "no per-item token exists in the commit message for COMMIT_MODE=$COMMIT_MODE — grep cannot confirm this item; use this turn's own memory of Phase 3's PLAN_FILE/cherry-pick output, or: git diff --cached --stat / git show --stat against the item's .file"
        ;;
    *)
        echo "! BLOCKED — COMMIT_MODE is '$COMMIT_MODE', not each/grouped/all/stage"; exit 1
        ;;
esac
```

No confirming commit found → this item was never closed by any exit path; that's the exact defect this gate exists to catch — surface it via `AskUserQuestion` (dispose as `deleted` with a stated reason, or leave open and investigate) rather than guessing either status. Only once every printed id is accounted for, continue to Step 9. Batch every `TaskList`/`TaskUpdate` this gate needs into one response.

`TaskUpdate(task_id=TASK_IMPL, status="completed")` — only once every id is accounted for; rides with Step 9's boundary-2 block.

## Step 9: Lint and QA gate

`TaskUpdate(task_id=TASK_LINT, status="in_progress")` rides with the boundary-2 block below (beside Step 8's `TASK_IMPL` completion); `TASK_LINT` flips to `completed` with Step 10's first block (§Zero bookkeeping-only turns).

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# boundary2: post-impl loop, pre-lint gate (compaction-contract.md §Lifecycle)
IFS= read -r _PR_NUMBER < "${TMPDIR:-/tmp}/resolve-pr-number-${CSID}" 2>/dev/null || _PR_NUMBER="n/a"
IFS= read -r _KEEP < "${TMPDIR:-/tmp}/resolve-keep-items-${CSID}" 2>/dev/null || _KEEP=""
IFS= read -r _IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" 2>/dev/null || _IMPL_DIR="n/a"
IFS= read -r _PUSH_AUTH < "${TMPDIR:-/tmp}/resolve-push-auth-${CSID}" 2>/dev/null || _PUSH_AUTH="unset"
IFS= read -r _POST_PR < "${TMPDIR:-/tmp}/resolve-post-pr-action-${CSID}" 2>/dev/null || _POST_PR="skip"
IFS= read -r _PUSH_STATUS < "${TMPDIR:-/tmp}/resolve-push-status-${CSID}" 2>/dev/null || _PUSH_STATUS="none"
_PRESERVE="pr=${_PR_NUMBER}, items-implemented, impl-dir=${_IMPL_DIR}, challenge-log=${_IMPL_DIR}/challenge-log.txt, item-tasks=${_IMPL_DIR}/item-tasks.tsv, push-auth=${_PUSH_AUTH} (Step 3d answer), post-pr=${_POST_PR}, push-status=${_PUSH_STATUS} (file resolve-push-status, Step 10 overwrites); next: lint/push/report"
[ -n "$_KEEP" ] && _PRESERVE="$_PRESERVE; user-keep: $_KEEP"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/write_skill_contract.py" "oss:resolve" "lint-qa (after implementation loop)" "$_IMPL_DIR" "${_PRESERVE}" "lint/QA gate (Step 9) → push per the recorded Step 3d answer, never re-ask (Step 10) → final report (Step 11)"  # timeout: 5000
IFS= read -r _OSS_RESOLVE < "${TMPDIR:-/tmp}/resolve-oss-resolve-${CSID}" 2>/dev/null || _OSS_RESOLVE=""  # reload (Check 41)
cat "$_OSS_RESOLVE/modes/lint-qa-gate.md"  # timeout: 5000
```

Execute its steps (loaded above).

## Step 10: Push

*Skip when report mode with no PR# (`$FORK_REMOTE`, `$HEAD_REF`, `$BASE_REF` unset — no fork branch; workflow ends at Step 11).*

When skipping, `TaskUpdate(task_id=TASK_CLOSE, status="deleted")` rides with Step 11's first call. Otherwise `TaskUpdate(task_id=TASK_CLOSE, status="in_progress")` rides with the block below, together with `TaskUpdate(task_id=TASK_LINT, status="completed")`.

**Push intent from Step 3d, confirmation here — with the full scope.** One call reads the recorded Step 3d intent back and computes the push scope the confirmation must show:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PUSH_AUTH < "${TMPDIR:-/tmp}/resolve-push-auth-${CSID}" 2>/dev/null || PUSH_AUTH="unset"
echo "PUSH_AUTH=$PUSH_AUTH"
# fresh shell (Check 41) — unbound here → empty push scope
IFS= read -r FORK_REMOTE < "${TMPDIR:-/tmp}/resolve-fork-remote-${CSID}" 2>/dev/null || FORK_REMOTE=""
IFS= read -r HEAD_REF < "${TMPDIR:-/tmp}/resolve-head-ref-${CSID}" 2>/dev/null || HEAD_REF=""
IFS= read -r BASE_REF < "${TMPDIR:-/tmp}/resolve-base-ref-${CSID}" 2>/dev/null || BASE_REF=""
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/derive_fork_remote.py" --fork-remote "$FORK_REMOTE" --head-ref "$HEAD_REF" --base-ref "$BASE_REF"  # timeout: 10000
```

The block exits non-zero (`⛔` — fork remote or head ref unresolved, push scope not computable) → never push and never ask about an unknown scope; record `not-attempted` (**Record the push outcome** below) and continue to Step 11. Otherwise branch on the printed `PUSH_AUTH`:

- `skip` → the user explicitly chose "don't push" at Step 3d: print `` → Push skipped (Step 3d answer) — run `git push` manually when ready. ``, record `skipped-by-user`, and jump to Step 11; the post-PR answer still applies there. No question.
- `push` → ask the **push confirmation** below — Q1 only; the post-PR action was already answered at Step 3d.
- anything else (`unset` — Step 3d's push question went unanswered or was dismissed, or no answer was ever recorded, e.g. a resume in a fresh session) → ask the **push confirmation** with both Q1 and Q2 in one call, as before the intent question existed. A missing answer is never a skip and never authorization.

<!-- branch: main-path — push confirmation (intent push or no recorded intent; skipped only on an explicit Step 3d "don't push" or an uncomputable scope) -->

**Push confirmation — one `AskUserQuestion` call.** Per `git-commit.md` push-safety rule ("Never push without explicit user confirmation") this question precedes any `git push`. Second-longest idle window (measured up to 11 h). Boundary-2 contract already names every file Step 11 needs; print this line in the reply before the call: `` Long wait? `/compact` now — commits landed, challenge log + item map in <IMPL_DIR>, resume lossless. ``

Q1 — push. Must surface:

- Target remote and branch: `$FORK_REMOTE/$HEAD_REF`
- Diff stat: `$PUSH_STAT` (e.g. `3 files changed, 47 insertions(+), 12 deletions(-)`)
- Commit count and last subject: `$PUSH_COUNT commits — last: "$LAST_SUBJECT"`

Options:

- (a) **Push** — proceed with `git push` below (default)
- (b) **Skip push** — stop after Step 9; user pushes manually later

Q2 — `unset` intent only — after the final report: (a) **Open PR in browser** (`gh pr view <PR_NUMBER> --web`) · (b) **Skip**. Run the matching Step 3d post-PR block (`open` or `skip`) — never edit a block's value.

Only proceed to the `git push` below on Q1 option (a). On option (b): print `` → Push skipped — run `git push` manually when ready. ``, record `skipped-by-user`, and jump to Step 11 (the post-PR answer still applies there). Unanswered Q1 is never authorization: treat it as (b).

<!-- policy-sibling: plugins/CLAUDE.md §Blueprint Blocks (canonical), plugins/cc_foundry/agents/challenger.md, plugins/cc_oss/skills/resolve/SKILL.md (Step 3d, Step 10), plugins/cc_oss/skills/review/SKILL.md (reject gate) -->

```bash
git push # timeout: 30000
```

An authorized push still stops on its own failures — never retried in a loop, never forced, never re-asked. The push guard and the absent `git push` allow rule are deliberate user safety controls: never weaken, bypass, or work around either, and never create, touch, or edit a guard's authorization file yourself.

- **Blocked by a push guard** (a hook rejects the call and names a user-created authorization file) → print the guard's instruction verbatim, do not retry, record `blocked-guard`, and write its exact `! touch …` / `git push …` / `! rm -f …` lines — copied character for character from the guard's message, no paraphrase — with the Write tool to `$IMPL_DIR/push-unblock.txt`. Continue to Step 11.
- **Blocked by a denied permission** (the harness permission prompt for `git push` was refused) → do not retry, record `blocked-permission`, and write the exact push command that was denied (`git push`, or the explicit-refspec form below) with the Write tool to `$IMPL_DIR/push-unblock.txt`. Continue to Step 11.
- **Rejected as non-fast-forward** (the PR branch moved on the remote) → no force and no retry. Print `⛔ Push rejected (non-fast-forward) — the PR branch moved; merge the remote branch, then push manually.`, record `rejected-non-ff`, and continue to Step 11.
- **No upstream or wrong tracking** (plain `git push` cannot resolve its destination) → the explicit-refspec fallback below, once; its own outcome is then classified by the bullets above.
- **Push lands** → record `pushed` after the verification block below.

Push failed for lack of upstream tracking → fallback:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r FORK_REMOTE < "${TMPDIR:-/tmp}/resolve-fork-remote-${CSID}" 2>/dev/null || FORK_REMOTE=""
IFS= read -r HEAD_REF < "${TMPDIR:-/tmp}/resolve-head-ref-${CSID}" 2>/dev/null || HEAD_REF=""
# empty refspec → push to wrong ref
[ -n "$FORK_REMOTE" ] && [ -n "$HEAD_REF" ] || { echo "⛔ Step 10 fallback: FORK_REMOTE/HEAD_REF unresolved — refusing explicit-refspec push"; exit 1; }
git push "$FORK_REMOTE" HEAD:"$HEAD_REF" # timeout: 30000
```

Verify push reached GitHub — confirm latest commit headlines match what was committed:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PR_NUMBER < "${TMPDIR:-/tmp}/resolve-pr-number-${CSID}" 2>/dev/null || PR_NUMBER=""
case "$PR_NUMBER" in ''|n/a|*[!0-9]*) echo "⛔ Step 10 PR number sentinel missing or invalid; cannot verify push"; exit 1 ;; esac
gh pr view "$PR_NUMBER" --json headRefOid,commits --jq '.commits[-3:] | .[].messageHeadline' # timeout: 6000
```

**Record the push outcome** — run **exactly one** block, the one matching what happened above; never edit a block's value (same trap as the Step 3d blocks). Step 11 reports this status, so a run that leaves Step 10 without recording one reports `none`.

`pushed`:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo pushed > "${TMPDIR:-/tmp}/resolve-push-status-${CSID}"  # timeout: 3000
```

`blocked-guard`:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo blocked-guard > "${TMPDIR:-/tmp}/resolve-push-status-${CSID}"  # timeout: 3000
```

`blocked-permission`:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo blocked-permission > "${TMPDIR:-/tmp}/resolve-push-status-${CSID}"  # timeout: 3000
```

`rejected-non-ff`:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo rejected-non-ff > "${TMPDIR:-/tmp}/resolve-push-status-${CSID}"  # timeout: 3000
```

`skipped-by-user`:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo skipped-by-user > "${TMPDIR:-/tmp}/resolve-push-status-${CSID}"  # timeout: 3000
```

`not-attempted`:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo not-attempted > "${TMPDIR:-/tmp}/resolve-push-status-${CSID}"  # timeout: 3000
```

## Step 11: Final report

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# boundary3: pre-final-report write (compaction-contract.md §Lifecycle)
IFS= read -r _PR_NUMBER < "${TMPDIR:-/tmp}/resolve-pr-number-${CSID}" 2>/dev/null || _PR_NUMBER="n/a"
IFS= read -r _KEEP < "${TMPDIR:-/tmp}/resolve-keep-items-${CSID}" 2>/dev/null || _KEEP=""
IFS= read -r _IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" 2>/dev/null || _IMPL_DIR="n/a"
IFS= read -r PUSH_STATUS < "${TMPDIR:-/tmp}/resolve-push-status-${CSID}" 2>/dev/null || PUSH_STATUS="none"
_PRESERVE="pr=${_PR_NUMBER}, final-report=pending-write, impl-dir=${_IMPL_DIR}, challenge-log=${_IMPL_DIR}/challenge-log.txt, item-tasks=${_IMPL_DIR}/item-tasks.tsv, push-status=${PUSH_STATUS}, push-unblock=${_IMPL_DIR}/push-unblock.txt"
[ -n "$_KEEP" ] && _PRESERVE="$_PRESERVE; user-keep: $_KEEP"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/write_skill_contract.py" "oss:resolve" "final-report (after push)" "$_IMPL_DIR" "${_PRESERVE}" "write final report → post-PR action gate"  # timeout: 5000
echo "PUSH_STATUS=$PUSH_STATUS"
case "$PUSH_STATUS" in blocked-guard|blocked-permission) echo "PUSH_UNBLOCK=$_IMPL_DIR/push-unblock.txt"; cat "$_IMPL_DIR/push-unblock.txt" 2>/dev/null || echo "⚠ push-unblock.txt missing" ;; esac
IFS= read -r _OSS_RESOLVE < "${TMPDIR:-/tmp}/resolve-oss-resolve-${CSID}" 2>/dev/null || _OSS_RESOLVE=""  # reload (Check 41)
cat "$_OSS_RESOLVE/templates/resolve-report.md"  # timeout: 5000
```

Report template (loaded above) — use for section structure. Its `### Push` section shows the printed `PUSH_STATUS` through the template's status table, never a prose recollection of Step 10.

**Tell the review what happened.** When this run consumed a review report, append one outcome per review-sourced item (`fixed` / `self-resolved` / `rejected` / `skipped` / `pending`, finding title and location, commit hash, reason) to `resolution.jsonl` beside that report. `fixed` and `self-resolved` need an implementation record (Phase 2 commit, Codex-direct record, or a `[resolve No.<id>]` commit after this run's base head); an accepted but unmerged item stays `pending`. The next `/oss:review` of this PR reads the ledger, confirms earlier fixes at the new head and re-checks rejected findings before deciding whether to report them again. Append-only and idempotent per run; the script builds records from this run's own files, never from recollection:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r REPORT_FILE < "${TMPDIR:-/tmp}/resolve-report-file-${CSID}" 2>/dev/null || REPORT_FILE=""
IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" 2>/dev/null || IMPL_DIR=""
IFS= read -r BASE_SHA < "${TMPDIR:-/tmp}/resolve-base-sha-${CSID}" 2>/dev/null || BASE_SHA=""
if [ -n "$REPORT_FILE" ] && [ -f "$REPORT_FILE" ] && [ -f "$IMPL_DIR/action-items.jsonl" ]; then
    python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/append_resolution.py" --impl-dir "$IMPL_DIR" --review-dir "$(dirname "$REPORT_FILE")" --base-sha "$BASE_SHA"  # timeout: 30000
else
    echo "→ no review report consumed — resolution ledger skipped"
fi
```

**Unblock push — last actionable item.** `PUSH_STATUS` is `blocked-guard` or `blocked-permission` → the block above `cat`s the `PUSH_UNBLOCK` file; end the report — after `**Next**`, the Challenge Log, Confidence, and every other section — with a `## Unblock push` section that repeats its lines verbatim in a fenced block. The user returns to the bottom of the report and runs exactly those lines; never paraphrase, reorder, or regenerate them. File missing or empty → print `⚠ push-unblock.txt missing — scroll to Step 10 for the guard's exact lines` in that section instead.

Immediately before printing it — the one standalone bookkeeping call, so a compaction mid-report cannot leave the run `in_progress` (`rules/task-lifecycle.md` §TaskUpdate before long output):

```text
TaskUpdate(task_id=TASK_CLOSE, status="completed")
```

**Print the final report — including the full Action Items resolution table — inline to terminal.**

> **Output-Routing exemption (canonical)**: the Step 11 final report is a read-in-context, acted-on-immediately resolution summary the user must see to confirm every item and how it resolved. Always print the full Action Items table inline to terminal regardless of row count — this is the whole point of the report. Global Output Routing (*5+ findings → `.temp/output-*.md`, summary only*) does **not** apply; never divert this table to a file in place of showing it. Writing a durable copy to `.reports/resolve/` in addition is fine, but the inline terminal print is mandatory and never replaced by a prose summary.

**Action Items table** — one row per selected item, columns: `#` | `Type` | `Change` | `Status` | `Resolution` | `Commit`:

- `Status`: ✓ implemented · ⊘ skipped · ✗ challenge-rejected
- `Resolution`: `implemented` · `self-resolved` (challenger provided alternative) · `skipped` · `challenge-rejected`
- `Change`: action type — `code` / `test` / `docs` / `config` / `ci` / `style` / `refactor`
- `Commit`: short SHA (7 chars); `—` when `COMMIT_MODE=stage`
- For `location: discussion` rows append `· thread (no GH resolve)` to Status — no GitHub Resolve button exists for PR main-thread comments

Include `### Challenge Log` section in report — source of truth is `$IMPL_DIR/challenge-log.txt` (Read it; one `key=value` record per line, written by `action-item-dispatch.md` Phase 1), never a recollection of verdicts from earlier in the conversation. File absent or empty (Step 8 skipped, skip-all, or `--no-challenge`) → omit the section; never treat the failed Read as an error. Columns: `#` | `Finding` | `Evidence` | `Suggestion` | `Resolution`. Every cell must be self-contained — reader gets full context from that row alone, never by cross-referencing another row or recalling earlier conversation:

- `Finding`: one-line gist of the reviewer's comment (from `finding` in `CHALLENGE_LOG`) — what was flagged, not just its id
- `Evidence`: bracketed flag + reason on one line, e.g. `[VALID] — <evidence_why>` or `[REJECT] — <evidence_why>`. Reason never empty, never generic — state in a few words what the verdict was about. Never print a bare `VALID`/`REJECT`, bracketed or not, with no reason
- `Suggestion`: bracketed flag + reason, same rule as `Evidence` above — never a bare verdict, reason always a few words naming what was assessed. `[VALID] — <suggestion_why>` or `[REJECT] — <suggestion_why>`; `—` for rows with `evidence=REJECT` (suggestion never evaluated, machine field unbracketed per `action-item-dispatch.md`'s producer format). A `CHALLENGE_LOG` entry reaching this render with an empty or missing `suggestion_why` is a producer defect, not a render-time gap — `action-item-dispatch.md`'s Phase 1 guards against this at the point the verdict is parsed (its per-item UNCERTAIN fallback). Never paper over a missing reason here with generic filler text
- `Resolution`: concrete outcome, never a bare label. `detail=pending-impl:<id>` → backfill before printing: look up that id's `Commit` SHA in the Action Items table above and run `git log -1 --format=%s <sha>` for the one-line summary of what changed; render as `as-suggested: <that summary>`. `detail=<alternative text>` (self-resolved rows) → render as `self-resolved: <alternative text>`. `detail=<evidence_why>` (rejected rows) → render as `rejected: <evidence_why>`. If a commit lookup fails, state `as-suggested: (commit summary unavailable, see commit <sha>)` — never fall back to printing the bare word `as-suggested` alone

Omit section when `--no-challenge`.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r SAVED_BRANCH < "${TMPDIR:-/tmp}/resolve-saved-branch-${CSID}" 2>/dev/null || SAVED_BRANCH=""
IFS= read -r COMMIT_MODE < "${TMPDIR:-/tmp}/resolve-commit-mode-${CSID}" 2>/dev/null || COMMIT_MODE="each"
IFS= read -r PUSH_STATUS < "${TMPDIR:-/tmp}/resolve-push-status-${CSID}" 2>/dev/null || PUSH_STATUS="none"
# stage mode: skip restore, else staged work lost
if [ "$COMMIT_MODE" = "stage" ]; then
    echo "⚠ COMMIT_MODE=stage: changes are staged on $(git branch --show-current) — restore to $SAVED_BRANCH skipped to preserve staged work. Run: git stash && git switch $SAVED_BRANCH && git stash pop (on PR branch) when ready."
# unpushed work (declined, blocked, rejected or never attempted): stay on the branch holding it, for review or a manual push
elif [ -n "$SAVED_BRANCH" ] && [ "$PUSH_STATUS" != "pushed" ]; then
    echo "→ Staying on $(git branch --show-current) — commits not pushed (PUSH_STATUS=$PUSH_STATUS). Return later with: git switch $SAVED_BRANCH"
elif [ -n "$SAVED_BRANCH" ]; then
    git switch "$SAVED_BRANCH" 2>/dev/null && echo "→ Restored to $SAVED_BRANCH"  # timeout: 5000
fi
```

**Branch after the run** — the block above returns to `SAVED_BRANCH` only when `PUSH_STATUS=pushed`. Any unpushed outcome (push declined, blocked, rejected or never attempted) leaves the session on the PR branch that holds the commits, so the user can inspect them or push by hand; the block prints how to switch back.

**Worktree exit** — if `WT_ENABLED=true` and a worktree was entered at Step 4: when pushed, the commits are on the fork (the deliverable is remote); when not pushed, they exist only in that worktree, so keep it. Follow `worktree-isolation.md` §Exit — `git branch --show-current`, then `ExitWorktree(action="keep")` to return the session to the main tree, and append the `Worktree` block noting the local worktree is disposable (`git worktree remove` when done). The `SAVED_BRANCH` restore above was a no-op — the main tree was never switched. Never auto-merge.

Post-PR action — already answered by the Step 3d push question; no new `AskUserQuestion` here:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r POST_PR_ACTION < "${TMPDIR:-/tmp}/resolve-post-pr-action-${CSID}" 2>/dev/null || POST_PR_ACTION="skip"
IFS= read -r _PR_NUMBER < "${TMPDIR:-/tmp}/resolve-pr-number-${CSID}" 2>/dev/null || _PR_NUMBER="n/a"
# n/a is what Step 1 stores when no PR# was parsed; never hand it to gh
[ "$POST_PR_ACTION" = "open" ] && [ "$_PR_NUMBER" != "n/a" ] && [ -n "$_PR_NUMBER" ] && gh pr view "$_PR_NUMBER" --web  # timeout: 10000
echo "POST_PR_ACTION=$POST_PR_ACTION"
```

The post-PR sentinel needs no cleanup — Step 1 re-initialises it to `skip` on every run, so nothing stale survives into the next invocation. Clear the compaction contract alone (its own fence: the `rm` keeps the block out of the blueprint manifest, and this exact path is allow-listed):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
rm -f .temp/state/skill-contract.md "${TMPDIR:-/tmp}/oss-resolve-active-${CSID}"  # skill complete (compaction-contract.md §Lifecycle)  # timeout: 5000
```

## Step 12: Comment dispatch + Codex review loop

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _OSS_RESOLVE < "${TMPDIR:-/tmp}/resolve-oss-resolve-${CSID}" 2>/dev/null || _OSS_RESOLVE=""  # reload (Check 41)
cat "$_OSS_RESOLVE/modes/comment-dispatch.md"  # timeout: 5000
```

Execute its steps (loaded above).

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
rm -f .temp/state/skill-contract.md "${TMPDIR:-/tmp}/oss-resolve-active-${CSID}"  # skill complete (compaction-contract.md §Lifecycle)  # timeout: 5000
```

</workflow>

<calibration>

Non-calibratable — `disable-model-invocation: true` means skill dispatches to sub-agents rather than running model pass directly; calibrate cannot score model output for skill that produces none.

</calibration>

<notes>

- **Pre-flight git fetch** — Step 1 always runs `git fetch origin` (unconditional) so all remote tracking refs — including `origin/$BASE_REF` — current before Step 5 merges. Step 5 fetches the target again right before merging and fast-forwards the local `$BASE_REF` branch to it when that branch exists (best effort: skipped with a warning when diverged or checked out in another worktree); the merge itself always uses `origin/$BASE_REF`. Then pulls current branch if upstream tracking ref exists and remote ahead. `git pull` conflicts → exit with message to resolve manually — prevents `git merge --continue` with no in-progress merge
- **Branch safety** — `gh pr checkout <PR#>` always lands on PR's HEAD, never `main`/`master`. Never push to default branch — if PR branch = default branch, abort, surface.
- **Same-repo branch rule** — for non-fork PRs (`isCrossRepository=false`), local branch name MUST equal `headRefName` at all times. Never create `pr<N>` alias or other branch name substitute. Enforced by `--branch "$PR_HEAD_REF"` at checkout + hard assertion post-checkout. Rationale: `git push HEAD:$HEAD_REF` on `pr<N>` alias creates new remote branch instead of pushing to PR head — silent data-loss class bug.
- **OSS fork support** — `gh pr checkout <PR#>` works same for branches + forks; forks get contributor remote + tracking; plain `git push` targets fork branch automatically.
- **Merge direction** — `origin/BASE_REF` INTO `HEAD_REF` (not reverse); PR branch = source of truth; maintainer still clicks Merge.
- **Contribution motivation before code** — "whose intent wins" lens; PR body + linked issues reveal constraints invisible in diff.
- **`[question]` items** — answer inline in resolve report only; reclassify before implementing; never silently implement unanswered question.
- **Push verification** — confirm via `gh pr view --json commits`; exit 0 from `git push` necessary but not sufficient (branch protection can silently reject).
- **Merge-push sequencing + escape hatch** — not atomic; concurrent push → non-fast-forward rejection; Step 10 never retries it unattended — the user retries the push only (don't re-run full merge). `git merge --abort` = undo conflict state; `git push --force-with-lease` on explicit user request only.
- **Impl agent health + effort**: C1 medium-effort bridge implementation calls use `bridge:implement` on the default or explicit bridge route, one item from a clean worktree per call; Git-derived changed paths must match the reply before per-item records. Explicit `--agent foundry:*` sends medium items through Phase 1+2 with the selected specialist. Dirty or non-medium bridge items use the change-to-specialist table. Effort is never `low`, minimum `medium`, typo/doc `medium`, multi-file/new-feature `xhigh`, default `high`.
- **Two-phase challenge**: evidence = problem exists?; suggestion = fix quality?; evidence reject → skip; suggestion reject → self-resolved via `alternative` field; all in `CHALLENGE_LOG` + Step 11 report.
- **COMMIT_MODE**: `each` (default); `all`; `stage` (⚠ branch restore skipped); `grouped` (falls back to `each` when labels skipped). Set via the commit-mode menu (Step 3d) — placement per the Step 3d slot table — skipped/discarded only when the bulk action = (d) skip-all. Distinct MENU from the bulk action (item scope vs commit strategy); item scope never implies commit mode; menus may share a call, never options.
- **GROUP_STRATEGY**: `domain` (default) · `file` · `specialist` · `labels`. Set via the topic-group question (Step 3d), asked beside the commit-mode menu. Read only when `COMMIT_MODE=grouped`; `labels` are typed at Step 3d and persisted to `$IMPL_DIR/group-labels.tsv`, so no strategy adds a user round-trip at Step 8.
- **DISPATCH_MODE**: `auto` (default) · `sequential` · `per-specialist` · `preview` (the "Custom" label). Set via the dispatch-granularity question (Step 3d), asked when pending or resolved/addressed items are available, beside the commit-mode or topic-group menu. Read by Phase 2 for sub-group splitting and wave width only — specialist routing, the file-ownership tiebreak and the import-coupling merge never change; `per-specialist` drops the ≤5 split, the one width guard a width answer may touch. Distinct from `GROUP_STRATEGY=specialist`, which is a commit-grouping strategy on its own sentinel. `preview` defers the width to one extra gate at the Phase 1 → Phase 2 boundary, where the formed groups are printed first; that gate resolves it to one of the other three.
- **AskUserQuestion usage**: the normal action-item path, after successful source resolution and without diagnostic or conflict recovery, takes at most 3 calls at Step 3d (10-18 pending: two checkbox pages + the commit-mode/topic-group/dispatch/push-intent follow-up; every other nonempty band takes 2, while zero pending with no closed items and a PR takes 1 push-intent call) plus 1 Step 10 push confirmation unless the push intent was an explicit "don't push". Picking topic-group (d) without typing labels adds one Step 3d call for the topic-label question. Selecting more than 20 IDs through typed closed IDs can add one cap-decision call when the ordinary follow-up has all four slots filled.
  - Every decision a later step needs and can see at Step 3d is asked there: commit mode, grouping strategy and typed labels, dispatch width, the over-20 cap, push intent, and the post-PR browser action. Between Step 3d and the Step 10 push confirmation, nothing is asked on this path.
  - Only `DISPATCH_MODE=preview` adds a call after Step 3d — the user elects it there by choosing Custom.
  - Other paths can add questions for unsupported flags, missing reports, too many conflicts, codemap index gates (all before Step 3d), an unresolved item status, a challenge that timed out twice (one batched question per wave), a lost typed-labels file, or a push-intent question left unanswered at Step 3d (Step 10 then also asks the post-PR question); they are outside this normal-path count.
  - The Step 3d push question carries push intent and the post-PR browser action in one 4-option menu; Step 10 asks the scope-bearing confirmation (Q1, plus Q2 only without a recorded intent) and Step 11 reads the stored post-PR action.
- **`--agent <name>`**: bare name auto-prefixed `foundry:`; must be an implementation agent (not curator); omit the bridge trailer when another agent is selected.
- **Thread resolution via GraphQL** — `isResolved` on `PullRequestReviewThread` (GraphQL only); REST doesn't expose it. `RESOLVED_THREAD_IDS` = root comment `databaseId`; GraphQL failure → `[]`.
- **Discussion vs inline**: `gh pr view --comments` = discussion (`location: discussion`; no Resolve button); `gh api .../pulls/<N>/comments` = inline (`location: inline`; resolvable). `location: discussion` + `[report]` items: implement-only, no GitHub close action. Surface unresolvable rows through the Status suffix `· thread (no GH resolve)`, not a separate column.
- **Commit attribution** — `[gh]`: `[resolve No.<id>] <reviewer> (gh):`; `[report]`: `[resolve No.<id>] /review finding by <agent> (report: <path>):`.
- **Reference scenarios**: Mode: bare PR# → pr; `42 report` → pr+report; `report` → report mode; bare comment → comment dispatch. Classification: LGTM/emoji → `[info]`; `nit:` → `[gh][suggest]`; resolved thread → keeps `[gh][req]`/`[gh][suggest]` type, `status: resolved`; "must fix" from write-access reviewer → `[gh][req]`. Challenge: present bug → VALID; already addressed → REJECT; better alternative → REJECT with alternative.
- Follow-up chains:
  - After push → maintainer reviews, clicks Merge; never approve/comment on PR.
  - Unanswered `[question]` → resolve report only; do NOT post to PR.
  - After merge → `Closes #N`/`Fixes #N` in body auto-closes linked issues; absent keywords → surface gap under `### Closing Keywords` note; don't edit PR body.

</notes>
