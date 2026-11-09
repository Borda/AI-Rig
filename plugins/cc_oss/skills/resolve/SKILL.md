---
name: resolve
description: "OSS maintainer fast-close workflow for GitHub PRs. Three phases: (1) PR intelligence — reads full thread, linked issues, PR body to synthesize contribution motivation and classify every comment into action items; (2) conflict resolution — checks out PR branch (fork-aware via gh pr checkout), merges BASE into it, resolves conflicts semantically using contributor's intent as priority lens; (3) implements each action item via specialist agents (Codex for eligible mechanical items) and commits per the chosen commit mode — one attributed commit per item by default — then pushes back to contributor's fork. Supports three source modes: pr (live GitHub comments only), report (latest /review report findings as action items, no GitHub re-fetch), and pr + report (both sources aggregated and deduplicated in one pass). Also accepts bare comment text for single-comment dispatch. NOT for reply drafting to /oss:analyse findings (use /oss:analyse --reply (requires `oss` plugin)). NOT for code diff review of PR changes (use /oss:review). NOT for release preparation (use /oss:release). NOT for fixing local bugs unrelated to a PR (use /develop:fix; requires develop plugin). TRIGGER when: PR is ready to close and has open comments, conflicts, or review findings to address; user says 'close this PR', 'resolve comments on PR #N', or 'implement review findings'."
argument-hint: <PR number or URL> [report] | report | <review comment text> [--no-challenge] [--agent <name>] [--codemap] [--no-codemap] [--worktree] [--keep "<items>"]
disable-model-invocation: true
allowed-tools: Read, Edit, Write, Bash, Agent, Skill, TaskCreate, TaskUpdate, TaskList, AskUserQuestion, EnterWorktree, ExitWorktree
effort: high
---

<objective>

OSS maintainer fast-close workflow. PR number → three phases fire automatically:

1. **PR intelligence** — synthesize motivation from PR body, linked issues, thread; classify comments into action items
2. **Conflict resolution** — checkout PR branch (fork-aware), merge `BASE_REF`, resolve conflicts with contributor intent as priority lens
3. **Action item implementation** — implement each item (specialists first, Codex for eligible mechanical items), committed per `COMMIT_MODE` — default one commit per item, attributed to its review comment — then push to contributor's fork

Result: conflict-free PR branch pushed to fork, ready to merge — no GitHub UI.

**Core invariant — transparent, reversible**: every action = visible named git object. Use `git merge` (new commit, two parents), never `git rebase` (rewrites SHA, kills revert/cherry-pick). Default commit mode `each`: every action item = own commit — granular revert always possible. The Step 3d modes `grouped`, `all` and `stage` trade that granularity for fewer commits (or none); every one still leaves named git objects, never rewritten history.

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
  - Bare name auto-prefixed with `foundry:` if no plugin prefix detected (e.g. `--agent sw-engineer` → `foundry:sw-engineer`; `--agent linting-expert` → `foundry:linting-expert`; `--agent doc-scribe` → `foundry:doc-scribe`); explicit prefix also accepted (`--agent foundry:sw-engineer`); see routing table in `dispatch-prelude.md`.
  - **`--agent` also applies to `INTEL_AGENT` (Step 3b thread intelligence)** — explicit real Agent types override label/title routing for classification as well.
  - `bridge:implement` is never passed to `Agent`: classification falls back to label/title routing, C1 candidates use the Skill route, and other items use the change-to-specialist table.

NOT-for additions (scope guards):

- **NOT for non-Python source PRs** (TypeScript, Go, Rust, Java) unless action items are limited to documentation or CI/CD changes — Step 9's lint-qa gate runs Python-specific tools (`ruff`/`mypy`); non-Python PRs get partial or no static-analysis review. For non-Python repos, run `/oss:resolve` in `report` mode with manually-curated findings.
- **NOT for branches with uncommitted local edits** — the `report`-mode no-PR# path operates on the current branch as-is; uncommitted changes would be committed alongside the action items, so its branch safety check below stops on tracked modifications (`git status --porcelain -uno`). Stash (`git stash`) or commit local edits before invoking — workflow doesn't auto-stash.

</inputs>

<constants>
```text
CHALLENGE_TIMEOUT_S=300  # intel + challenge agent deadline; tightened from CLAUDE.md §6 default 900s
AGENT_DEADLINE_S=900     # conflict, specialist, QA/lint agent deadline — checked at wake-ups, never polled
```
> Shell `timeout S cmd` prefix only for commands that could hang in direct-shell execution (git push, gh pr checkout); `# timeout: N` covers the rest.
</constants>

<compaction>

> loads: compaction-contract.md

- Boundary 0: before the Step 3d item-selection gate — longest idle window of the run; contract makes a mid-wait `/compact` lossless.
- Boundary 1: end of Phase 1 challenge (opening block of `dispatch-phase2.md`), before Phase 2 implementation dispatch. Closes the gap where Phase 1's parallel challenge agents and all of Phase 2's worktree-held implementation ran with no refresh, so a compaction anywhere in either phase resumed at Step 3d and re-asked an already-answered gate.
- Boundary 2: end of Step 8 — per-item implementation loop complete, before Step 9 lint gate. Contract overwrites on each iteration (latest state wins).
- Boundary 3: start of Step 11 — before final report write, after push.
- Preserve at boundary 0: PR#, `IMPL_DIR`, `action-items.jsonl`, `pr-intelligence.md`, `pr-vars.sh` paths.
- Preserve at boundary 1: PR#, `IMPL_DIR`, `challenge-log.txt`, `skipped-items.txt`, `item-tasks.tsv` paths, recorded Step 3d push answer.
- Preserve at boundary 2: PR#, implemented/remaining item state, `IMPL_DIR`, `challenge-log.txt`, `item-tasks.tsv` paths, recorded Step 3d push and post-PR answers, push status.
- Preserve at boundary 3: final report path, PR#, `IMPL_DIR`, `challenge-log.txt`, `item-tasks.tsv` paths, recorded push status, `push-unblock.txt` path.
- State that must survive a compaction lives in files under `$IMPL_DIR`, never only in-context: challenge verdicts (`challenge-log.txt`), item→task map (`item-tasks.tsv`), `IMPL_DIR` itself via the `resolve-impl-dir-${CSID}` sentinel written at `mktemp` time.

</compaction>

<workflow>

<!-- Symbol legend: ⚠ = warning/skipped (non-blocking, proceed with caution) · `! BLOCKED —` = blocked/stop (halt workflow, do not proceed; communication.md form) · ⛔ = the same stop as emitted on stderr by bin/ scripts only -->

<!-- Agent resolution: see _OSS_SHARED/agent-resolution.md -->

## Agent Resolution

`agent-resolution.md` (loaded below) contains: foundry check + fallback table. foundry not installed → use table to substitute each `foundry:X` with `general-purpose`. Agents this skill uses: `foundry:sw-engineer`, `foundry:qa-specialist`, `foundry:linting-expert`, `foundry:doc-scribe`, `foundry:perf-optimizer`, `foundry:solution-architect`, `foundry:challenger`.

<!-- Inline fallback (if agent-resolution.md unreadable): foundry:sw-engineer → general-purpose, foundry:qa-specialist → general-purpose, foundry:linting-expert → general-purpose, foundry:doc-scribe → general-purpose, foundry:perf-optimizer → general-purpose, foundry:solution-architect → general-purpose, foundry:challenger → general-purpose. -->

**Task hygiene** — task tools may be deferred; load before first use: `ToolSearch(query="select:TaskList,TaskCreate,TaskUpdate,TaskGet", max_results=4)`. Call `TaskList` first and triage each task it returns: `completed` if work clearly done, `deleted` if orphaned, keep `in_progress` only if genuinely continuing. Never spend a turn on bookkeeping alone — every `TaskCreate`/`TaskUpdate` ships in the same response as the next substantive tool call; one exception, `TaskUpdate(completed)` immediately before a long output block (`rules/task-lifecycle.md`).

<!-- ../../assets/images/resolve-arch.svg diagrams the runs, gates and parallel fan-out. Documentation only, never loaded — update it in the same commit as any change to step order, gate placement, or agent fan-out. -->

## Run structure — text order is not execution order

Two blocking user gates cut this workflow into three **runs**; an explicit "don't push" at Step 3d removes the second gate, leaving two. Within a run, work that shares no data dependency is dispatched in the same response turn so it overlaps instead of queueing. Sections below stay in step-number order for reading; the run marker on each heading says when it actually executes.

| Run | Executes | Ends at |
| -- | -- | -- |
| 1 | Step 1 · 2 · 3a · 3b ‖ **Step 4 + Step 5** · 3c · gate | Step 3d selection gate |
| 2 | Step 3e · 6 · **Step 7a ‖ Step 8 Phase 1 early wave** · 7b join · C1 · Phase 1 late wave · rest of 8 · 9 | Step 10 push confirmation (skipped on an explicit Step 3d "don't push") |
| 3 | Step 10 push · 11 · 12 | workflow end |

**Run 2 is unattended on the normal path.** Step 3d collects every decision a later step needs and can predict up front with the same information — item scope, commit mode, grouping strategy and typed labels, dispatch width, push intent and the post-PR action — and persists each to a sentinel or `$IMPL_DIR` file. Later steps never re-ask a Step 3d decision. Push authorization is the one decision that cannot move: its required scope (diff stat, commit count, last subject) exists only after implementation, so Step 10 asks it with that scope unless the user explicitly chose "don't push" at Step 3d. Otherwise Run 2 asks only for error recovery (an unresolved item status, a challenge that timed out twice, a lost typed-labels file) or for the group preview the user explicitly chose at Step 3d.

Two overlaps, each riding work that shares no data dependency with its partner:

- **Step 4 + Step 5 beside the Step 3b intel agent.** Checkout and the `--no-commit` merge need only `PR_NUMBER`, never any intelligence output, so they run in the same turn that spawns `INTEL_AGENT` instead of waiting for its envelope.
- **Step 7a beside the Step 8 Phase 1 challenge, after the gate.** Conflict resolution does not depend on which items get selected, and the challenge only reads, so both fire in one response once Step 3d has answered. While files conflict, Phase 1 reads every file from the pinned PR head (`git show <PR head oid>:<file>`), never the half-merged disk copy — one consistent snapshot whenever 7b commits (`dispatch-phase1.md`).

**Finding collection is one stream.** Everything up to the Step 3d picker (intel, merge findings, table) runs with no conflict agent in flight, so no agent notification or tool output interleaves with the `AskUserQuestion` call that `enforce-resolve-table.js` checks. Conflict agents once ran beside that call and the required table failed to land often enough to cost the picker a retry; the overlap moved behind the gate, where the user is not waiting on a table.

Two dependencies bound the Run 2 fan. **Step 6a consumes the contribution motivation that `INTEL_AGENT` synthesizes** (`pr-intelligence.md`: PR body is stated intent, thread is the authoritative record) — its envelope is long in by Run 2, and a git-log-only lens is never a fallback. **C1 and Phase 2 need a committed merge and a clean tree**, so they wait for the Step 7b join; items C1 may take (`medium` effort with the bridge available) are held out of the early challenge wave and challenged late only if C1 hands them back.

Degenerate cases, all reducing to the old serial order with no special handling: `report` mode with no PR# skips Steps 4–7 entirely; zero conflicted files means Step 5 commits the merge itself, Step 7a spawns nothing and Phase 1 runs alone; `--no-challenge` or zero selected items leaves Step 7a alone in the fan; `--worktree` enters the worktree inside Step 4, so an `INTEL_AGENT` spawned moments earlier keeps writing to the absolute `IMPL_DIR` it was handed.

## Agent wait discipline — no polling, per-agent deadlines

<!-- policy-sibling: plugins/cc_develop/skills/_shared/agent-resolution.md (§Agent waits — no polling), plugins/cc_research/skills/_shared/agent-resolution.md (§Agent waits — no polling) — same agent_watch.py no-polling wait contract -->

Every spawned agent (intel, conflict, Phase 1 challenge, Phase 2 specialists, Step 9 QA/lint) runs in the background. Waiting on one is **never** a tool call:

- **Never** call `ScheduleWakeup`, `ListAgents`, or a `Monitor` loop to wait for a spawned agent — and no `sleep`, no poll loop, no no-op call, no "waiting" turn. Spawn, end the turn, resume on the completion notification (`rules/task-lifecycle.md` §After spawning).
- **Arm a deadline per agent.** In the same response as each spawn batch, write `$IMPL_DIR/agent-watch-<batch>.tsv` with the Write tool — one row per agent, `<name>\t<deliverable path, or - for an envelope-only agent>\t<deadline seconds>`. The file's write time is the spawn time, so no clock value is ever typed. Batch names and deadlines: `intel` 300 · `conflict` 900 · `challenge` 300 · `challenge-retry` 300 · `impl` 900 (rewrite per wave) · `impl-<group_tag>.link<k>` 900 (one file per chain link k≥2, never a rewrite; the spawn-base block also writes one, deadline 0 and any k, for a recorded link with no watch row — never launched, so it reads `timed_out` at once) · `qa` 900.
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
# shared flag/--keep parser (C5; also analyse/review SKILL.md); --keep-state: no eval, writes resolve-{worktree,clean-args,keep-items}-${CSID} (compaction-contract.md §keep: semantics), owner-safe clear of a crashed run's stale contract (§Lifecycle)
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/parse-skill-flags.py" --flags worktree --keep-state resolve "$ARGUMENTS" >/dev/null  # timeout: 5000
IFS= read -r WT_ENABLED < "${TMPDIR:-/tmp}/resolve-worktree-${CSID}" 2>/dev/null || WT_ENABLED=false
echo "$WT_ENABLED" > "${TMPDIR:-/tmp}/oss-resolve-worktree-${CSID}"

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

**Codemap gates** — when `CODEMAP_FORCE_OFF=false`, run (from `codemap-gates.md`, loaded above): **Gate A** if `CODEMAP_ENABLED=false` (missing index → provider-owned preparation); **Gate B** if `CODEMAP_ENABLED=true` and `CODEMAP_CURRENCY=stale`. Follow the active provider contract: prepare automatically by default, preserve explicit `LAZY_CODEMAP` confirm-first and `SCAN_NO_AUTOBUILD=1` frozen preferences, and set `CODEMAP_ENABLED=true` only after successful foreground preparation; retain required-mode failures and optional file-read fallback — never model-invoke the `codemap-py:scan-codebase` skill, which is `disable-model-invocation: true` (user-slash-only). Skip both gates when `CODEMAP_FORCE_OFF=true` (`--no-codemap`).

Codex missing: set `CODEX_AVAILABLE=false` — Steps 3–7 work without it. Step 8 degradation:

1. Simple, single-file items → `foundry:sw-engineer`
2. Complex/multi-file → skip with: `⚠ bridge@borda-ai-rig is absent or disabled — skipping item #<id>. Install or enable the bridge and reload plugins.`

### Review-handoff auto-detect (when $ARGUMENTS is empty)

<!-- loads: handoff-detect.md -->

Load and execute only when `$ARGUMENTS` is empty; any other run skips it.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _OSS_RESOLVE < "${TMPDIR:-/tmp}/resolve-oss-resolve-${CSID}" 2>/dev/null || _OSS_RESOLVE=""  # reload (Check 41)
cat "$_OSS_RESOLVE/modes/handoff-detect.md"  # timeout: 5000
```

Execute its blocks (loaded above).

Parse $ARGUMENTS:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
[ -n "$CLAUDE_PLUGIN_ROOT" ] || { echo "Error: CLAUDE_PLUGIN_ROOT is unset — verify oss plugin installation and that skill is invoked via Claude Code plugin system"; exit 1; }  # timeout: 5000
[ -f "${CLAUDE_PLUGIN_ROOT}/bin/parse-resolve-args.py" ] || { echo "Error: parse-resolve-args.py not found — verify oss plugin installation"; exit 1; }  # timeout: 5000
# parse-resolve-args.py anchors on "<PR#> [report]" alone — ANY surviving flag token routes to
# comment-dispatch and drops PR_NUMBER. Every supported flag must be stripped here, not just codemap/keep.
# --keep-state also writes resolve-no-challenge-${CSID}, read by Step 8 Phase 1; no stale contract yet (first write: Step 3d), so the second clear is a no-op
python "${CLAUDE_PLUGIN_ROOT}/bin/parse-skill-flags.py" --flags no-codemap,codemap,worktree,no-challenge --value-flags agent --keep-state resolve "$ARGUMENTS" >/dev/null  # timeout: 5000
IFS= read -r ARGUMENTS < "${TMPDIR:-/tmp}/resolve-clean-args-${CSID}" 2>/dev/null || ARGUMENTS=""
IFS= read -r VALUE_AGENT < "${TMPDIR:-/tmp}/resolve-agent-${CSID}" 2>/dev/null || VALUE_AGENT=""
case "${VALUE_AGENT:-}" in
    '') ;;
    *:*) ;;
    *) VALUE_AGENT="foundry:$VALUE_AGENT" ;;
esac
# a leftover --token would route "42 --full" to comment dispatch (bridge:implement then gets that text as a review comment): strip it when the rest is a PR/report request; bare comment text keeps its dashes
_REST=$(printf '%s' "$ARGUMENTS" | sed -E 's/(^|[[:space:]])--[A-Za-z][A-Za-z-]*//g; s/^[[:space:]]+//; s/[[:space:]]+$//')  # timeout: 3000
if [ "$_REST" != "$ARGUMENTS" ] && [ -n "$_REST" ] && ! python "${CLAUDE_PLUGIN_ROOT}/bin/parse-resolve-args.py" "$_REST" | grep -qx 'MODE=comment-dispatch'; then
    echo "UNKNOWN_FLAGS=$(printf '%s\n' "$ARGUMENTS" | tr -s '[:space:]' '\n' | grep -E '^--[A-Za-z][A-Za-z-]*$' | tr '\n' ' ')"
    ARGUMENTS="$_REST"
fi
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
: > "${TMPDIR:-/tmp}/resolve-intake-degraded-${CSID}"  # gap triage Proceed writes the accepted report path; empty = full intake, never inherited
# defence-in-depth: validate VAR=value, no metachars, then assign only the four known keys — guards regression/tampered binary
tmpenv=$(mktemp)  # timeout: 3000
trap 'rm -f "$tmpenv"' EXIT INT TERM
python "${CLAUDE_PLUGIN_ROOT}/bin/parse-resolve-args.py" "$ARGUMENTS" >"$tmpenv"  # timeout: 5000
if grep -qvE "^[A-Z_][A-Z0-9_]*=([A-Za-z0-9_./:#@+-]*|'[^']*')$" "$tmpenv"; then
    echo "Error: parse-resolve-args.py emitted unexpected output — refusing to load"
    cat "$tmpenv"
    exit 1
fi
while IFS='=' read -r _K _V; do case "$_V" in \'*\') _V=${_V#\'}; _V=${_V%\'};; esac; case "$_K" in PR_NUMBER) PR_NUMBER=$_V;; PR_URL) PR_URL=$_V;; MODE) MODE=$_V;; ARGUMENTS) ARGUMENTS=$_V;; esac; done < "$tmpenv"
# sets: PR_NUMBER, PR_URL, MODE, ARGUMENTS ('#' stripped, comment-dispatch only)
# parser keeps a non-/pull/ GitHub URL as MODE=pr, empty PR_NUMBER (never comment dispatch); every PR step keys on PR_NUMBER
if [ "$MODE" = pr ] && [ -z "$PR_NUMBER" ]; then echo "! BLOCKED — not a pull-request URL: $PR_URL — /oss:resolve takes a PR number or a .../pull/N URL"; exit 1; fi
echo "${PR_NUMBER:-n/a}" > "${TMPDIR:-/tmp}/resolve-pr-number-${CSID}"  # timeout: 3000
# liveness sentinel for oss:review's Step 7 already-running check; write-only here, cleared at report/reject-gate stops below + Step 11/12 (review/SKILL.md)
# stale-tolerated like a crashed skill-contract.md, but no Step 0 rm: only overwritten when a later run reaches this line, so a stop before it
# (Step 3d skip-all, `! BLOCKED` sentinel-loss guard) suppresses oss:review's gate for that PR all session — accepted, not chased into every exit
# `report` value never numeric-matches a review's PR tag: a no-PR run must never suppress any review's gate
case "$PR_NUMBER" in ''|n/a|*[!0-9]*) echo "report" > "${TMPDIR:-/tmp}/oss-resolve-active-${CSID}" ;; *) echo "$PR_NUMBER" > "${TMPDIR:-/tmp}/oss-resolve-active-${CSID}" ;; esac  # timeout: 3000
: > "${TMPDIR:-/tmp}/resolve-base-ref-${CSID}"  # Step 4 or report mode must publish this run's base before Step 9
: > "${TMPDIR:-/tmp}/resolve-head-ref-${CSID}"  # empty = no PR branch; Step 9.0 would otherwise re-merge a prior PR's target into local work
: > "${TMPDIR:-/tmp}/resolve-pr-ref-${CSID}"  # Step 4 or local report mode must publish this run's commit reference
echo targeted > "${TMPDIR:-/tmp}/resolve-change-scope-${CSID}"  # Step 8 prelude overwrites; a run that skips Step 8 must not inherit last run's lint-only (it would skip QA)
```

<!-- branch: unsupported-flags — isolated; ≤1 call; fires only when unknown flags present -->

**Unsupported flag check** — the parse block above prints `UNKNOWN_FLAGS=<tokens>` when a PR/report request carried a `--<token>` outside `{--no-challenge, --agent, --codemap, --no-codemap, --worktree}`, and has already removed those tokens from `$ARGUMENTS` (the parser anchors on `<PR#> [report]` alone: a surviving token would reroute the run to comment dispatch and drop `PR_NUMBER`). Printed → invoke `AskUserQuestion` — (a) **Abort** (stop, re-invoke with correct flags) · (b) **Continue ignoring** (the tokens are already stripped; proceed). Bare comment text is never scanned: its dashes are text. Supported: `--no-challenge`, `--agent <name>`, `--codemap`, `--no-codemap`, `--worktree`, `--keep "<items>"`.

- `MODE="pr+report"` or `MODE="report"` → resolve the report source with the **Report source resolution** block below (executable, not prose), then branch on its printed `REPORT_STATUS`.
  - `MODE="report"` additionally: extract PR# from the report header if present; no PR# in header → add branch safety check before Step 8 — `CURRENT=$(git branch --show-current); DEFAULT=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's|refs/remotes/origin/||'); [ -z "$DEFAULT" ] && DEFAULT=$(git remote show origin 2>/dev/null | grep 'HEAD branch' | awk '{print $NF}'); [ -z "$DEFAULT" ] && { printf "! BLOCKED — cannot determine default branch; refusing to proceed\n"; exit 1; }; [ "$CURRENT" = "$DEFAULT" ] && { echo "! BLOCKED — on default branch '$CURRENT': report mode without PR# must not operate on default branch; check out a feature branch first"; exit 1; }; [ -z "$(git status --porcelain -uno)" ] || { echo "! BLOCKED — uncommitted edits on '$CURRENT': report mode without PR# commits on this branch as-is; commit or stash them first"; exit 1; }`
- `MODE="pr"` → continue Step 2; only when the reject gate printed `incomplete-review-report`, run **Report source resolution** first for its gap triage
- `MODE="comment-dispatch"` → branch safety check before Step 12: `export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"; IFS= read -r WT_ENABLED < "${TMPDIR:-/tmp}/oss-resolve-worktree-${CSID}" 2>/dev/null; [ "$WT_ENABLED" = "true" ] || WT_ENABLED=false; CURRENT=$(git branch --show-current); DEFAULT=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's|refs/remotes/origin/||'); [ -z "$DEFAULT" ] && DEFAULT=$(git remote show origin 2>/dev/null | grep 'HEAD branch' | awk '{print $NF}'); [ -z "$DEFAULT" ] && { printf "! BLOCKED — cannot determine default branch; refusing to proceed\n"; exit 1; }; [ "$CURRENT" = "$DEFAULT" ] && { echo "! BLOCKED — on default branch '$CURRENT': comment dispatch must not commit to default branch"; exit 1; }; [ "$WT_ENABLED" = "true" ] && echo "⚠ --worktree has no effect in comment-dispatch mode"` → jump to Step 12

### Reject-gate check (every mode — run the block even without a `PR_NUMBER`)

`oss:review`'s acceptance gate can reject a PR at the premise level — `Gate: REJECT_<GROUND> @<sha>`, one of `GOAL`/`CONDUCT`/`SCOPE`/`LICENSE`/`DUPLICATE`/`REVERTED`/`SPAM`/`PHILOSOPHY` (see `oss:review` SKILL.md Stage 1 for what each means). Premise problem, not fixable by `/oss:resolve` editing code — never start the fix pipeline on a PR still in that state, regardless of which of the 8 grounds fired. Only complete `PASS` or `BLOCK` reports are actionable fix queues, plus an incomplete report the user accepts at **Gap triage** with zero critical gaps. Missing, malformed, or ambiguous decision headers block the workflow; a newer unfinished run cannot replace an earlier decision. No prior report still permits PR-comments-only mode. Report-only mode also validates the selected report; it cannot clear a rejection by bypassing PR-aware intake.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PR_NUMBER < "${TMPDIR:-/tmp}/resolve-pr-number-${CSID}" 2>/dev/null || PR_NUMBER=""
# --path-out: this lookup is PR-scoped; Steps 3a/3c reuse it instead of a second newest-of-any-PR glob
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/find_review_report.py" --pr "$PR_NUMBER" \
    --path-out "${TMPDIR:-/tmp}/resolve-report-file-${CSID}"  # timeout: 6000
```

The gate's `--path-out` sentinel (`${TMPDIR:-/tmp}/resolve-report-file-${CSID}`) is the **PR-scoped** answer to "does a review report for this PR already exist". Steps 3a and 3c reuse it; PR-scoped misses never glob another PR — never re-derive it from the gate's printed line, and never let the printed `Gate: …` verdict be the only thing parsed out of this block. A path-publication failure stops the workflow. An exit 1 that printed `⛔ BLOCKED — incomplete-review-report` is not yet a stop, in any mode: run **Report source resolution** next, which grades the gaps and asks (**Gap triage**). Every other exit 1 — a standing rejection, an invalid `--pr`, a failed path publication — stops here. With no `PR_NUMBER` the script skips the check and writes an **empty** sentinel — that write is why the block runs in every mode: a bare `/oss:resolve report` after an earlier `/oss:resolve 42 report` in the same session would otherwise inherit PR 42's path.

### Report source resolution (`report` and `pr + report` modes; `pr` mode only after an incomplete-report gate)

<!-- loads: report-intake.md -->

Single lookup for both modes, the **only** sanctioned way to conclude that no report exists, and the intake triage for an incomplete report (**Gap triage**, **Intake proceed**). Load and execute it when `MODE` is `report` or `pr + report`, or `pr` after the reject gate printed `incomplete-review-report`; `pr` mode otherwise and `comment-dispatch` skip it.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _OSS_RESOLVE < "${TMPDIR:-/tmp}/resolve-oss-resolve-${CSID}" 2>/dev/null || _OSS_RESOLVE=""  # reload (Check 41)
cat "$_OSS_RESOLVE/modes/report-intake.md"  # timeout: 5000
```

Execute its blocks (loaded above), then branch on the printed `REPORT_STATUS`.

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
| 4 | `TASK_CHECKOUT` → `in_progress` (or `TASK_CHECKOUT`, `TASK_CONFLICT` → `deleted` when skipped) | the Step 4 checkout block |
| 4 end | `TASK_CHECKOUT` → `completed` | the same Step 4 checkout block |
| 5 | `TASK_CONFLICT` → `in_progress` | the `conflict-resolution.md` load |
| 3d | `TASK_GATHER` → `completed`, `TASK_SELECT` → `in_progress` | the boundary-0 contract block, before the selection prompt |
| Run 2 fan | `TASK_SELECT` → `completed` | the fan's first tool call (Step 3e creates) |
| 7b join | `TASK_CONFLICT` → `completed` | the merge commit call (zero conflicts: the fan's first tool call) |
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

<!-- loads: report-merge.md -->

> **Output-Routing exemption (canonical — applies to every ACTION_ITEMS table in this skill, Steps 3b/3c/3d)**: ACTION_ITEMS tables are selection-driving, read-in-context enumerations. Write every row to `$IMPL_DIR/action-items-table.md` before the picker; Q1 lists pending items as plain lines within 1,500 characters, or the top items, pending counts by type and that path past the budget. Print the full headed table once as the last text of the reply message that issues Step 3d’s AskUserQuestion, never in an option’s `preview`, never Bash/tool stdout. Global Output Routing (*5+ findings → `.temp/output-*.md`, summary only*) does **not** apply — never divert these tables to a `.temp` report or replace the table with a summary. This follows the global rule’s copy-intent override (*read-in-context, acted-on-immediately → user-facing reply even if long*).

Load and execute in `pr + report` mode only; any other mode skips it.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _OSS_RESOLVE < "${TMPDIR:-/tmp}/resolve-oss-resolve-${CSID}" 2>/dev/null || _OSS_RESOLVE=""  # reload (Check 41)
cat "$_OSS_RESOLVE/modes/report-merge.md"  # timeout: 5000
```

Execute its blocks (loaded above).

## Step 3d: User item selection

<!-- loads: item-selection.md -->

Gather is complete here (3a, 3b, or 3c done). Report mode also enters this step for nonempty report items, so the same user choice supplies item scope and commit mode. Mark TASK_GATHER `completed` and TASK_SELECT `in_progress` **before** the selection prompt — otherwise the gather `activeForm` keeps driving the spinner through the user-selection window, falsely implying gather is still running. `TaskUpdate(task_id=TASK_GATHER, status="completed")` and `TaskUpdate(task_id=TASK_SELECT, status="in_progress")` both ride with the boundary-0 contract block below.

Load and execute at Step 3d for user item selection.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _OSS_RESOLVE < "${TMPDIR:-/tmp}/resolve-oss-resolve-${CSID}" 2>/dev/null || _OSS_RESOLVE=""  # reload (Check 41)
cat "$_OSS_RESOLVE/modes/item-selection.md"  # timeout: 5000
```

Execute its blocks (loaded above).

## Step 7b join: dispatch and collect conflict resolutions — opens Run 2

Run 2 opens with a fan, not a wait: conflict resolution (Steps 6–7a) and the Phase 1 early challenge wave start in one response, after the Step 3d answers are in. Order:

1. **First response** — `TaskUpdate(task_id=TASK_SELECT, status="completed")` rides with Step 3e's `TaskCreate` batch and, when files conflict, the Step 6 context call (`conflict-resolution.md`; it needs only refs and the Step 3b motivation). The Step 3d confirm block rides here too. The one standalone-bookkeeping rule stays: no response of task calls alone (§Zero bookkeeping-only turns).
2. **Prep response(s)** — Step 3e's map Write, then Step 8's `cat` load and every block through its prelude, C1-candidate split and verdict re-check, up to the first `Agent()` call. `report` mode skips 3e; zero selected items skips Step 8 and the fan holds Step 7a alone. In the last Prep response — or, with no Prep response (`report` mode and zero selected items), in a response of its own before the Fan response — run the write+gate block below before any fan spawn: the fan outlives the Step 3d contract, and a resume must never re-dispatch 7a while its watch row is open.

Persist the completed intake report in `pr-intelligence.md` and selected scope in `selected-items.txt` before this refresh.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" 2>/dev/null || _IMPL_DIR=""
IFS= read -r _PR_NUMBER < "${TMPDIR:-/tmp}/resolve-pr-number-${CSID}" 2>/dev/null || _PR_NUMBER="n/a"
_PRESERVE="pr=$_PR_NUMBER, impl-dir=$_IMPL_DIR, items=$_IMPL_DIR/action-items.jsonl, selected-items=$_IMPL_DIR/selected-items.txt, item-tasks=$_IMPL_DIR/item-tasks.tsv, conflict-tasks=$_IMPL_DIR/conflict-tasks.tsv, conflict-envelope=$_IMPL_DIR/conflict-envelope.json, watch=$_IMPL_DIR/agent-watch-conflict.tsv + $_IMPL_DIR/agent-watch-challenge.tsv (after fan), Step 3d answered (sentinels hold commit-mode, dispatch-mode, push-auth)"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/write_skill_contract.py" --keep-sentinel resolve-keep-items "oss:resolve" "Run 2 fan (conflict resolve ‖ Phase 1 early wave)" "$_IMPL_DIR" "$_PRESERVE; phase-report=$_IMPL_DIR/pr-intelligence.md" "resume: never re-issue Step 3d; re-cat own agent-resolution.md, modes/conflict-resolution.md, modes/dispatch-prelude.md and modes/dispatch-phase1.md (the last only when resolve-no-challenge is not true; current phase only); neither agent-watch-conflict.tsv nor agent-watch-challenge.tsv exists → fan not spawned: run the Fan response (7a + Phase 1 early wave, arm watches); else run agent_watch.py first; merge may be uncommitted (MERGE_HEAD) — never re-dispatch Step 7a while agent-watch-conflict.tsv has an open row; collect 7b when conflict-envelope.json exists, then C1, late wave, Phase 2" -- calls_ahead=40 "drop=expanded PR intelligence and action-item analysis"  # timeout: 5000
IFS= read -r _OSS_SHARED < "${TMPDIR:-/tmp}/resolve-oss-shared-${CSID}" 2>/dev/null || _OSS_SHARED=""
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/compaction_boundary.py" "$_OSS_SHARED"
```

Follow the Boundary step printed above before any fan spawn; report is the run-dir’s `pr-intelligence.md`, with the preserved selection and task maps. Only an opt-in `stop` decision defers the fan until `continue`; an `advise` decision carries its hint into the fan response's final text.

3. **Fan response** — a `continue` or `advise` decision enters the fan: the Step 7a spawn and the Phase 1 early-wave spawns plus Structural prep, all in this one response, each batch armed in its own `agent-watch-*.tsv` (§Agent wait discipline). After spawning and arming its watches, end the turn.

No conflicted files → no 7a; `--no-challenge` or an empty early set → no early wave.

4. **Each notification** — persist any envelope, run the watch check, then consume what landed: a challenge reply goes through Phase 1's verdict processing at once; the conflict envelope goes to 7b below. Order between the two never matters.
5. **7b** (`### 7b: Verify and complete merge` in `conflict-resolution.md`) — confirm `git diff --name-only --diff-filter=U` is empty, no residual conflict markers remain staged, mark each conflict task `completed` (all in one response), commit the merge; `TaskUpdate(task_id=TASK_CONFLICT, status="completed")` rides with the commit call; with zero conflicts there is no 7b, so the close rides with the fan's first response instead. A conflict agent that returned nothing is `timed_out` — surface it with ⏱, run the stop-path release fence in `dispatch-prelude.md` (the run holds the branch mutex since the prelude), and stop before C1 and Phase 2 rather than implementing on an unmerged tree: jump to Step 11 and state `merge uncommitted — finish with git commit, or git merge --abort`. Challenge results stay on disk. Then re-point the HEAD fingerprint the Step 8 prelude took (when it ran) before the merge commit existed; the block refuses when the commit's first parent is not that fingerprint, since a foreign local commit would otherwise be absorbed:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _PRIOR < "${TMPDIR:-/tmp}/resolve-base-sha-${CSID}" 2>/dev/null || _PRIOR=""
[ -z "$_PRIOR" ] || [ "$(git rev-parse HEAD^1)" = "$_PRIOR" ] || { echo "⚠ merge commit's first parent is not the prelude fingerprint — foreign commit during the fan; fingerprint left as is"; exit 0; }
git rev-parse HEAD > "${TMPDIR:-/tmp}/resolve-base-sha-${CSID}"  # timeout: 3000
```

6. **Join complete** = merge committed and every early-wave chunk, single-item retry, caucus and double-timeout gate settled or ⏱. Only then do C1, the Phase 1 late wave and Phase 2 begin (`dispatch-prelude.md`, `dispatch-phase1.md`, `dispatch-phase2.md`): C1 edits the tree, and retries read it.

Nothing to fan (no conflicted files and an empty early set, or `report` mode with no PR#) → no-op, continue with the prep response.

## Step 3e: Create tasks for selected items

`report` mode skips Step 3e, whether or not the report header names a PR. Step 3a already persisted its action items, and Step 8's report-mode task handling expects no `item-tasks.tsv`. Continue to Step 8 — Steps 4–5 already ran back in Run 1 when a PR# was found (skipped entirely when none was); Steps 6–7a fire in the Run 2 fan (Step 7b join). `pr` and `pr+report` create per-item tasks below.

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

When skipping, both ride with the next real call: `TaskUpdate(task_id=TASK_CHECKOUT, status="deleted")`, `TaskUpdate(task_id=TASK_CONFLICT, status="deleted")`. Otherwise `TaskUpdate(task_id=TASK_CHECKOUT, status="in_progress")` and `TaskUpdate(task_id=TASK_CHECKOUT, status="completed")` both ride with the one Step 4 block below.

**Branch-safety pre-check** — must run BEFORE `gh pr checkout` so a wrong-branch commit is impossible (per `git-commit.md` Gate 2). Verify PR's `headRefName` isn't repo's default branch — `gh pr checkout` of a same-repo PR whose HEAD = default branch would land on default; any later commit (Step 8) would violate Gate 2. One block, in order: the pre-check (`resolve_pr_refs.py`), the `gh` check + checkout + same-branch verify, then the `FORK_REMOTE`/`PR_REF` derivation for Step 10. Every hard `exit 1` ends the whole block, so a failed stage never reaches the next:

```bash
# timeout: 45000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PR_NUMBER < "${TMPDIR:-/tmp}/resolve-pr-number-${CSID}" 2>/dev/null || PR_NUMBER=""
case "$PR_NUMBER" in ''|n/a|*[!0-9]*) echo "! BLOCKED — Step 4 PR number sentinel missing or invalid; refusing checkout"; exit 1 ;; esac
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/resolve_pr_refs.py" --pr "$PR_NUMBER" || exit $?  # timeout: 15000
# gh = hard prereq: gh pr checkout has no fallback path
command -v gh >/dev/null 2>&1 || { echo "! BLOCKED — gh CLI required; install: https://cli.github.com"; exit 1; }
# resolve_pr_refs.py persisted these sentinels; a child process cannot export them
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
        git switch --end-of-options "$PR_HEAD_REF" 2>/dev/null \
            || git switch -c "$PR_HEAD_REF" --end-of-options "$LOCAL_SHA" \
            || { echo "! BLOCKED — Cannot switch to $PR_HEAD_REF — aborting (branch active in another worktree?)"; exit 1; }
    fi
else
    # hard-exit on failure — else HEAD_REF set but git stuck on caller branch, Step8 commits land wrong branch
    # --branch required: w/o it gh CLI v2.93+ falls back to pr<N> alias on collision → Step10 push makes unrelated branch (CRITICAL bug pyDeprecate 2026-06-13T08:33Z)
    gh pr checkout "$PR_NUMBER" --branch="$PR_HEAD_REF" \
        || { echo "! BLOCKED — gh pr checkout failed — aborting (network, branch deleted, auth expired, or local conflicts)"; exit 1; }   # timeout: 15000
fi
# verify in the same call — checkout must land on the expected branch, else abort before Step 8 can commit
HEAD_REF="$PR_HEAD_REF"
IFS= read -r IS_CROSS_REPO < "${TMPDIR:-/tmp}/resolve-is-cross-repo-${CSID}" 2>/dev/null || IS_CROSS_REPO=""
[ -n "$HEAD_REF" ] && [ -n "$IS_CROSS_REPO" ] || { echo "! BLOCKED — Step 4 verify: HEAD_REF/IS_CROSS_REPO sentinels missing — checkout state unverifiable, aborting before Step 8 can commit"; exit 1; }
git remote -v | grep '(fetch)' | head -10 # timeout: 3000
git status  # timeout: 3000
CURRENT_BRANCH=$(git branch --show-current 2>/dev/null)  # timeout: 3000
# same-repo: branch must equal PR_HEAD_REF, no alias — gh falls back to pr<N> on collision; assert as hard gate
if [ "$IS_CROSS_REPO" = "false" ] && [ "$CURRENT_BRANCH" != "$PR_HEAD_REF" ]; then
    echo "! BLOCKED — SAME-REPO RULE VIOLATION: on '$CURRENT_BRANCH' but PR headRefName='$PR_HEAD_REF' — branch alias (pr<N>) created instead of using original branch. Aborting to prevent push to wrong branch."
    exit 1
fi
[ "$CURRENT_BRANCH" = "$HEAD_REF" ] || { echo "! BLOCKED — checkout did not land on $HEAD_REF (current: $CURRENT_BRANCH) — aborting before Step 8 can commit to wrong branch"; exit 1; }  # timeout: 3000
# FORK_REMOTE for the Step 10 push; truncate first so a failed derivation leaves no stale value
: > "${TMPDIR:-/tmp}/resolve-pr-ref-${CSID}"
: > "${TMPDIR:-/tmp}/resolve-fork-remote-${CSID}"
if [ "$IS_CROSS_REPO" = "true" ]; then
    IFS= read -r FORK_REMOTE < "${TMPDIR:-/tmp}/resolve-head-repo-owner-${CSID}" 2>/dev/null || FORK_REMOTE=""
    [ -n "$FORK_REMOTE" ] || FORK_REMOTE=$(gh pr view "$PR_NUMBER" --json headRepositoryOwner --jq .headRepositoryOwner.login) # sentinel-miss fallback only # timeout: 6000
    PR_URL=$(gh pr view "$PR_NUMBER" --json url --jq .url) || { echo "! BLOCKED — Could not resolve fork PR URL"; exit 1; } # timeout: 6000
    [[ "$PR_URL" =~ ^https://[^/]+/[^/]+/[^/]+/pull/${PR_NUMBER}$ ]] || { echo "! BLOCKED — Fork PR URL does not match PR number"; exit 1; }
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

> **Split across three dispatch points, one loaded file.** Step 5 runs in Run 1 immediately after Step 4, beside the intel agent. Steps 6–7a are dispatched in Run 2's fan, beside the Phase 1 early challenge wave (Step 7b join section) — never before the Step 3d picker; 7b is collected at the same join. Load the file once here and execute the parts at their own points — re-`cat` it only if a compaction dropped it from context.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _OSS_RESOLVE < "${TMPDIR:-/tmp}/resolve-oss-resolve-${CSID}" 2>/dev/null || _OSS_RESOLVE=""  # reload (Check 41)
cat "$_OSS_RESOLVE/modes/conflict-resolution.md"  # timeout: 5000
```

Execute its steps (loaded above) at their dispatch points.

## Step 8: Implement action items

*Skip when `SELECTED_ITEMS` is empty — jump to Step 9.*

When skipping, `TaskUpdate(task_id=TASK_IMPL, status="deleted")` rides with Step 9's first call. Otherwise `TaskUpdate(task_id=TASK_IMPL, status="in_progress")` rides with the codemap index block below.

Step 8 starts inside the Run 2 fan (Step 7b join): its prelude and the Phase 1 early wave run while the merge is still uncommitted; C1, the Phase 1 late wave and Phase 2 wait for the join.

**Codemap index identity (if `CODEMAP_ENABLED=true`)**: resolve the index path the next block reuses. No query runs here — per-item blast radius is dispatch-prelude.md's **Pre-loop blast-radius scan**, which resolves each item's canonical module first and passes it as `rdeps`' positional argument.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CODEMAP_ENABLED < "${TMPDIR:-/tmp}/resolve-codemap-enabled-${CSID}" 2>/dev/null || CODEMAP_ENABLED="false"  # timeout: 3000
if [ "$CODEMAP_ENABLED" = "true" ]; then
    # index dir anchors at git root, not cwd — subdir invocation otherwise misses an index that exists. _PROJ = raw basename; scanner writes it unsanitized, so `tr -cd` would seek a filename it never wrote.
    _ROOT=$(git rev-parse --show-toplevel 2>/dev/null); [ -n "$_ROOT" ] || _ROOT="$PWD"
    _PROJ=$(basename "$_ROOT")
    _IDX="${CODEMAP_INDEX_DIR:-$_ROOT/.cache/codemap}"
    echo "${_IDX}/${_PROJ}.json" > "${TMPDIR:-/tmp}/resolve-codemap-index-${CSID}"  # timeout: 3000
fi
```

Blast radius, top callers and coupling pairs reach each implementation agent through the prelude's printed per-item `← callers:` lines (blast-radius scan), not from this step.

**Review pre-flight cache** — reuse per-module codemap answers `/review` already computed, so Step 8 blast-radius scan issues 0 duplicate pre-flight queries when a fresh review artifact exists (artifact shape and freshness rule in the `bin/codemap_cache.py` docstring; the source is the `$RUN_DIR/codemap-context.md` that `/oss:review` Step 2 copies from its `modes/codemap-context.md` staging; requires codemap). Locate latest review run-dir, materialize per-module cache once, before per-item loop. The newest context of any PR is taken on purpose: `codemap_cache.py read` freshness-gates every answer against the current index, so a context from another PR degrades to a cache miss, never to wrong data:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CODEMAP_ENABLED < "${TMPDIR:-/tmp}/resolve-codemap-enabled-${CSID}" 2>/dev/null || CODEMAP_ENABLED="false"  # timeout: 3000
IFS= read -r _IDX_FILE < "${TMPDIR:-/tmp}/resolve-codemap-index-${CSID}" 2>/dev/null || _IDX_FILE=""  # git-root-anchored path from the index-identity block
CODEMAP_CACHE_DIR=""
if [ "$CODEMAP_ENABLED" = "true" ]; then
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

`dispatch-prelude.md`'s per-item blast-radius scan reads this cache first (freshness-gated `codemap_cache.py read`) and only calls `codemap-py query` on a cache miss — see its **Pre-loop blast-radius scan**. Empty `CODEMAP_CACHE_DIR` (no review artifact, or oss helper absent) → every module is a cache miss and the scan queries live, unchanged from prior behaviour.

<!-- Step 8 defined in modes/dispatch-prelude.md, dispatch-phase1.md, dispatch-phase2.md, dispatch-phase3.md -->

> loads: dispatch-prelude.md · dispatch-phase1.md · dispatch-phase2.md · dispatch-phase3.md

Read `--no-challenge` from its sentinel, not from the raw argument blob — SKILL.md Step 1 strips every flag token before mode parsing, so `$ARGUMENTS` no longer carries it here. The block loads `dispatch-phase1.md` only without the flag:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _OSS_RESOLVE < "${TMPDIR:-/tmp}/resolve-oss-resolve-${CSID}" 2>/dev/null || _OSS_RESOLVE=""  # reload (Check 41)
[ -f "${TMPDIR:-/tmp}/resolve-no-challenge-${CSID}" ] && IFS= read -r NO_CHALLENGE < "${TMPDIR:-/tmp}/resolve-no-challenge-${CSID}" || NO_CHALLENGE="false"
echo "NO_CHALLENGE=$NO_CHALLENGE"
cat "$_OSS_RESOLVE/modes/dispatch-prelude.md"
[ "$NO_CHALLENGE" = "true" ] || cat "$_OSS_RESOLVE/modes/dispatch-phase1.md"  # timeout: 5000
```

`NO_CHALLENGE=true` → skip Phase 1 entirely; `SURVIVING_ITEMS` = all `SELECTED_ITEMS`, every item treated `VALID`, Challenge Log section omitted from the report.

Execute the prelude (IMPL_AGENT routing, IMPL_DIR init, blast-radius scan, plus a branch mutex + HEAD fingerprint so a second concurrent resolve aborts and an external mid-flight write surfaces at merge-back, C1, shared Challenge-log append block), then run the three-phase dispatch directly in the orchestrator. Load each later phase file only when that phase starts — on resume, re-cat the prelude plus the current phase file only:

1. Phase 1 challenge (parallel by domain, read-only) — `dispatch-phase1.md`, loaded above.

2. Phase 2 implementation (parallel, one isolated `git worktree` per specialist; groups formed by specialist then a file-ownership + import-coupling tiebreak so items that would collide on same file — or across an import edge — land in one worktree). Load once the Step 7b join, C1 and the Phase 1 late wave have finished:

   ```bash
   export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
   IFS= read -r _OSS_RESOLVE < "${TMPDIR:-/tmp}/resolve-oss-resolve-${CSID}" 2>/dev/null || _OSS_RESOLVE=""
   cat "$_OSS_RESOLVE/modes/dispatch-phase2.md"  # timeout: 5000
   ```

3. Phase 3 merge-back (sequential cherry-pick, whole worktree groups ordered most-central-first so foundational commits land before dependents, `TaskUpdate` per item as its commit lands). Load once every Phase 2 group's fences ran or it is ⏱:

   ```bash
   export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
   IFS= read -r _OSS_RESOLVE < "${TMPDIR:-/tmp}/resolve-oss-resolve-${CSID}" 2>/dev/null || _OSS_RESOLVE=""
   cat "$_OSS_RESOLVE/modes/dispatch-phase3.md"  # timeout: 5000
   ```

`TaskUpdate` calls stay orchestrator-owned throughout — Phase 1/2 subagents never touch task list (subagent can't drive parent's task list); only Phase 3, run by orchestrator itself after each cherry-pick, flips a task to `completed`. Explains why tasks flip in item-priority order during Phase 3 even though the work producing them ran concurrently in Phase 2.

Step 8 has no per-pass item cap: every selected item runs in one pass, never a rerun for a remainder. It bounds load per agent instead — ≤12 items per Phase 1 challenge agent (`ceil(n/12)` chunks per domain), ≤5 per Phase 2 implementation spawn (≤8 under `per-specialist`; one file's overflow runs as sequential chained links) — and paces both in pool-capped waves.

**Straggler gate — before leaving Step 8**: the dispatch files' per-item close-out (REJECT, skipped, cherry-pick landed — including the C1 Codex-direct shortcut, which never enters Phase 1/2/3 at all) should have already terminated every id in `item-tasks.tsv`; this catches whichever one didn't. UNVERIFIED tasks are durable pending obligations with owner/next_check/closure, excluded from implementation recovery and carried into Step 9/final Open Concerns. Never move on to Step 9 over any other open child — that hid the original leak. Fails closed on a lost `IMPL_DIR` sentinel: distinct from "no items were selected," which the file's own absence still reports safely. One Bash call, run once, unedited: the gate loop lists each open id in `straggler-check-ids.txt`, then the confirmation loop checks every listed id in the same run.

```bash
# timeout: 8000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" 2>/dev/null || IMPL_DIR=""
[ -n "$IMPL_DIR" ] || { echo "! BLOCKED — IMPL_DIR sentinel missing; cannot verify child tasks before leaving Step 8"; exit 1; }
if [ -f "$IMPL_DIR/item-tasks.tsv" ]; then
    : > "$IMPL_DIR/straggler-check-ids.txt"  # the confirmation loop below reads its ids here, never from a placeholder
    _SKIPPED_IDS=$(cut -f1 "$IMPL_DIR/skipped-items.txt" 2>/dev/null)
    # anchored right after id= — resolution= is the field placed there, before any free-text field
    # (dispatch-prelude.md's producer template), so a reviewer's quoted text can never match it
    _REJECTED_IDS=$(grep -iE '^id[[:space:]]*=[[:space:]]*[0-9]+[[:space:]]+resolution[[:space:]]*=[[:space:]]*rejected' "$IMPL_DIR/challenge-log.txt" 2>/dev/null | sed -n 's/^id[[:space:]]*=[[:space:]]*\([0-9]*\).*/\1/p')
    _ALREADY_FIXED_IDS=$(sed -n 's/^id=\([0-9][0-9]*\) resolution=already-fixed evidence=ALREADY_FIXED .*/\1/p' "$IMPL_DIR/challenge-log.txt")
    _UNVERIFIED_IDS=$(sed -n 's/^id=\([0-9][0-9]*\) resolution=unverified evidence=UNVERIFIED .*/\1/p' "$IMPL_DIR/challenge-log.txt")
    while IFS=$'\t' read -r item_id task_id; do
        case "$item_id" in ''|*[!0-9]*) continue ;; esac
        { printf '%s\n' "$_ALREADY_FIXED_IDS" | grep -qx "$item_id"; } && printf 'closed: item=%s (already-fixed)\n' "$item_id" && continue
        { printf '%s\n' "$_UNVERIFIED_IDS" | grep -qx "$item_id"; } && printf 'pending: item=%s (unverified) — retain owner/next_check/closure; no implementation recovery\n' "$item_id" && continue
        { printf '%s\n' "$_SKIPPED_IDS" "$_REJECTED_IDS" | grep -qx "$item_id"; } && printf 'closed: item=%s (rejected/skipped)\n' "$item_id" && continue
        [ -n "$task_id" ] || { echo "! skipping — item $item_id has an empty task id in item-tasks.tsv"; continue; }
        printf 'check: item=%s task=%s\n' "$item_id" "$task_id"  # not rejected/skipped — must have landed a commit; verify below, never assume
        printf '%s\n' "$item_id" >> "$IMPL_DIR/straggler-check-ids.txt"
    done < "$IMPL_DIR/item-tasks.tsv"
else
    echo "n/a — report mode or no items selected"
    exit 0
fi
# timeout: 3000
IFS= read -r COMMIT_MODE < "${TMPDIR:-/tmp}/resolve-commit-mode-${CSID}" 2>/dev/null || COMMIT_MODE="each"
IFS= read -r _BASE_SHA < "${TMPDIR:-/tmp}/resolve-base-sha-${CSID}" 2>/dev/null || _BASE_SHA=""
[ -s "$IMPL_DIR/straggler-check-ids.txt" ] || { echo "n/a — no check: line above"; exit 0; }
[ -n "$_BASE_SHA" ] || echo "⚠ resolve-base-sha sentinel missing — confirmation below is unscoped, verify the matched commit's date/author before trusting it"
# every check: id from the gate loop's file, never an item_id placeholder: one invariant run confirms them all
case "$COMMIT_MODE" in each|grouped|all|stage) ;; *) echo "! BLOCKED — COMMIT_MODE is '$COMMIT_MODE', not each/grouped/all/stage"; exit 1 ;; esac
while IFS= read -r _ITEM_ID || [ -n "$_ITEM_ID" ]; do
    case "$_ITEM_ID" in ''|*[!0-9]*) continue ;; esac
    case "$COMMIT_MODE" in
        each)
            # anchored to the literal attribution token commit_action_item.py --build emits, and
            # range-bound to this run's commits — an unanchored, unscoped grep can match a different
            # item's id as a substring (No.1 inside No.12) or a prior run's commit on the same branch
            _HITS=$(git log --oneline -E --grep="\[resolve No\.${_ITEM_ID}\]" ${_BASE_SHA:+"$_BASE_SHA..HEAD"})  # timeout: 5000
            [ -n "$_HITS" ] && printf 'MATCH — item %s: %s\n' "$_ITEM_ID" "$_HITS" || echo "NO MATCH — item ${_ITEM_ID} has no [resolve No.${_ITEM_ID}] commit"
            ;;
        grouped)
            # tokenize + exact-match, no boundary regex: `\b` is GNU-only (never compiles under `git log -E` POSIX ERE);
            # --oneline is fail-open (subject only, never the body holding the item list → stray sha/subject digits false-confirm).
            # --format=%B reads the body's "[resolve group] … items <ids>" line; grep -qx exact token: "PR #1" never matches 1, "30" never 3
            # (verified on a live repo: group commits "30 4" and "7 8" — every real id matched, "1" did not)
            git log --format=%B --grep="items" ${_BASE_SHA:+"$_BASE_SHA..HEAD"} \
                | grep -E '^\[resolve group\]' | tr ' ' '\n' | grep -qx "${_ITEM_ID}" \
                && echo "MATCH — item ${_ITEM_ID} found in a group commit" \
                || echo "NO MATCH — item ${_ITEM_ID} not found in any group commit"  # timeout: 5000
            ;;
        all|stage)
            echo "item ${_ITEM_ID}: no per-item token exists in the commit message for COMMIT_MODE=$COMMIT_MODE — grep cannot confirm this item; use this turn's own memory of Phase 3's PLAN_FILE/cherry-pick output, or: git diff --cached --stat / git show --stat against the item's .file"
            ;;
    esac
done < "$IMPL_DIR/straggler-check-ids.txt"
```

- Bash printed `n/a` → no per-item tasks were created, proceed straight to the flip below.
- `closed:` lines need nothing further; ALREADY_FIXED already completed its task from validated historical evidence.
- `pending:` lines remain pending and visible in resolution.jsonl/final Open Concerns; never require an implementation commit, delete the task, or enter straggler implementation recovery for these UNVERIFIED obligations.
- A `! skipping` line names a malformed row (empty task id) — investigate `item-tasks.tsv` directly via `TaskList`/`grep` for that item id before proceeding; never guess its status.
- For every `check:` line: call `TaskList`; if that task is already `completed`/`deleted`, done. If it's still open, do NOT default it to `completed` — confirm independently that item's commit is actually on the branch before calling `TaskUpdate(status="completed")`. The block's confirmation half already printed one `MATCH`/`NO MATCH` line per `check:` id, read from `straggler-check-ids.txt` — use the lines for ids whose task is still open. The confirmation command depends on `COMMIT_MODE` — only `each` carries a per-item attribution token; `grouped` folds several ids into one message; `all`/`stage` carry none at all (`stage` never commits — the diff stays staged, per its own contract). A lost `resolve-base-sha` sentinel degrades the confirmation to an unscoped search across the whole branch, which can false-confirm a never-implemented item against a prior run's commit on the same branch — the block warns and treats any match with extra suspicion in that case; both reads and the mode dispatch happen inside it, not by hand.

No confirming commit found → this item was never closed by any exit path; that's the exact defect this gate exists to catch — surface it via `AskUserQuestion` (dispose as `deleted` with a stated reason, or leave open and investigate) rather than guessing either status. Only once every printed id is accounted for, continue to Step 9. Batch every `TaskList`/`TaskUpdate` this gate needs into one response.

`TaskUpdate(task_id=TASK_IMPL, status="completed")` — only once every id is accounted for; rides with Step 9's boundary-2 block.

## Step 9: Lint and QA gate

`TaskUpdate(task_id=TASK_LINT, status="in_progress")` rides with the boundary-2 block below (beside Step 8's `TASK_IMPL` completion); `TASK_LINT` flips to `completed` with Step 10's first block (§Zero bookkeeping-only turns).

Implementation envelopes and commit ledgers must be persisted before this refresh.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# boundary2: post-impl loop, pre-lint gate (compaction-contract.md §Lifecycle)
IFS= read -r _PR_NUMBER < "${TMPDIR:-/tmp}/resolve-pr-number-${CSID}" 2>/dev/null || _PR_NUMBER="n/a"
IFS= read -r _IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" 2>/dev/null || _IMPL_DIR="n/a"
IFS= read -r _PUSH_AUTH < "${TMPDIR:-/tmp}/resolve-push-auth-${CSID}" 2>/dev/null || _PUSH_AUTH="unset"
IFS= read -r _POST_PR < "${TMPDIR:-/tmp}/resolve-post-pr-action-${CSID}" 2>/dev/null || _POST_PR="skip"
IFS= read -r _PUSH_STATUS < "${TMPDIR:-/tmp}/resolve-push-status-${CSID}" 2>/dev/null || _PUSH_STATUS="none"
_PRESERVE="pr=${_PR_NUMBER}, items-implemented, impl-dir=${_IMPL_DIR}, challenge-log=${_IMPL_DIR}/challenge-log.txt, item-tasks=${_IMPL_DIR}/item-tasks.tsv, push-auth=${_PUSH_AUTH} (Step 3d answer), post-pr=${_POST_PR}, push-status=${_PUSH_STATUS} (file resolve-push-status, Step 10 overwrites); next: lint/push/report"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/write_skill_contract.py" --keep-sentinel resolve-keep-items "oss:resolve" "lint-qa (after implementation loop)" "$_IMPL_DIR" "${_PRESERVE}; phase-reports=${_IMPL_DIR}/phase2-envelope-*.json, commits=${_IMPL_DIR}/phase2-commits.jsonl, c1-summaries=${_IMPL_DIR}/c1-item-summary.tsv" "resume: re-cat own agent-resolution.md and modes/lint-qa-gate.md; lint/QA gate (Step 9) → push per the recorded Step 3d answer, never re-ask (Step 10) → final report (Step 11)" -- calls_ahead=30 "drop=implementation summaries"  # timeout: 5000
IFS= read -r _OSS_SHARED < "${TMPDIR:-/tmp}/resolve-oss-shared-${CSID}" 2>/dev/null || _OSS_SHARED=""
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/compaction_boundary.py" "$_OSS_SHARED"
IFS= read -r _OSS_RESOLVE < "${TMPDIR:-/tmp}/resolve-oss-resolve-${CSID}" 2>/dev/null || _OSS_RESOLVE=""  # reload (Check 41)
cat "$_OSS_RESOLVE/modes/lint-qa-gate.md"  # timeout: 5000
```

Follow the Boundary step printed above before executing the lint/QA gate; reports are the implementation envelopes and ledgers in the preserved impl-dir.

Execute its steps (loaded above).

## Step 10: Push

<!-- loads: push.md -->

*Skip when report mode with no PR# (`$FORK_REMOTE`, `$HEAD_REF`, `$BASE_REF` unset — no fork branch; workflow ends at Step 11).*

When skipping, `TaskUpdate(task_id=TASK_CLOSE, status="deleted")` rides with Step 11's first call. Otherwise `TaskUpdate(task_id=TASK_CLOSE, status="in_progress")` rides with the block below, together with `TaskUpdate(task_id=TASK_LINT, status="completed")`.

Load and execute at Step 10 unless report mode has no PR number.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _OSS_RESOLVE < "${TMPDIR:-/tmp}/resolve-oss-resolve-${CSID}" 2>/dev/null || _OSS_RESOLVE=""  # reload (Check 41)
cat "$_OSS_RESOLVE/modes/push.md"  # timeout: 5000
```

Execute its blocks (loaded above).

## Step 11: Final report

<!-- loads: final-report.md -->

Load and execute at Step 11 for the final report.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _OSS_RESOLVE < "${TMPDIR:-/tmp}/resolve-oss-resolve-${CSID}" 2>/dev/null || _OSS_RESOLVE=""  # reload (Check 41)
cat "$_OSS_RESOLVE/modes/final-report.md"  # timeout: 5000
```

Execute its blocks (loaded above).

## Step 12: Comment dispatch + Codex review loop

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _OSS_RESOLVE < "${TMPDIR:-/tmp}/resolve-oss-resolve-${CSID}" 2>/dev/null || _OSS_RESOLVE=""  # reload (Check 41)
cat "$_OSS_RESOLVE/modes/comment-dispatch.md"  # timeout: 5000
```

Execute its steps (loaded above).

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
rm -f .temp/state/skill-contract.md "${TMPDIR:-/tmp}/oss-resolve-active-${CSID}" "${TMPDIR:-/tmp}/resolve-intake-gaps-${CSID}" "${TMPDIR:-/tmp}/resolve-intake-rerun-${CSID}" "${TMPDIR:-/tmp}/resolve-intake-degraded-${CSID}" "${TMPDIR:-/tmp}/resolve-gate-out-${CSID}"  # skill complete (compaction-contract.md §Lifecycle)  # timeout: 5000
```

</workflow>

<calibration>

Non-calibratable — multi-gate interactive workflow dispatching sub-agents, with no single scorable model output; `disable-model-invocation: true` only keeps the model from auto-invoking it.

</calibration>

<notes>

- **Pre-flight git fetch** — Step 1 always runs `git fetch origin` (unconditional) so all remote tracking refs — including `origin/$BASE_REF` — current before Step 5 merges. Step 5 fetches the target again right before merging and fast-forwards the local `$BASE_REF` branch to it when that branch exists (best effort: skipped with a warning when diverged or checked out in another worktree); the merge itself always uses `origin/$BASE_REF`. The target keeps moving after that merge, so `check_base_fresh.py` re-fetches and checks ancestry twice more: at Step 9 entry (drift → re-merge unattended, then QA; cap 2) and in Step 10's scope block (drift → shown in the push confirmation with a re-sync option). Then pulls current branch if upstream tracking ref exists and remote ahead. `git pull` conflicts → exit with message to resolve manually — prevents `git merge --continue` with no in-progress merge
- **Branch safety** — `gh pr checkout <PR#>` always lands on PR's HEAD, never `main`/`master`. Never push to default branch — if PR branch = default branch, abort, surface.
- **Same-repo branch rule** — for non-fork PRs (`isCrossRepository=false`), local branch name MUST equal `headRefName` at all times. Never create `pr<N>` alias or other branch name substitute. Enforced by `--branch "$PR_HEAD_REF"` at checkout + hard assertion post-checkout. Rationale: `git push HEAD:$HEAD_REF` on `pr<N>` alias creates new remote branch instead of pushing to PR head — silent data-loss class bug.
- **OSS fork support** — `gh pr checkout <PR#>` works same for branches + forks; forks get contributor remote + tracking; plain `git push` targets fork branch automatically.
- **Merge direction** — `origin/BASE_REF` INTO `HEAD_REF` (not reverse); PR branch = source of truth; maintainer still clicks Merge.
- **Contribution motivation before code** — "whose intent wins" lens; PR body + linked issues reveal constraints invisible in diff.
- **`[question]` items** — answer inline in resolve report only; reclassify before implementing; never silently implement unanswered question.
- **Push verification** — confirm via `gh pr view --json commits`; exit 0 from `git push` necessary but not sufficient (branch protection can silently reject).
- **Merge-push sequencing + escape hatch** — not atomic; concurrent push → non-fast-forward rejection; Step 10 never retries it unattended — the user retries the push only (don't re-run full merge). Target-branch races are the other half: a target commit landing between Step 5 and the push leaves the PR behind and conflicting without any push error — caught by the Step 9.0 and Step 10 `check_base_fresh.py` checks, never by the push itself. `git merge --abort` = undo conflict state; `git push --force-with-lease` on explicit user request only.
- **Impl agent health + effort**: C1 bridge implementation calls (C1 candidates: `medium` or `high` effort with a Codex-fit `change` — `code`·`test`·`docs`·`style`·`config`·`ci` — and one file) use `bridge:implement` on the default or explicit bridge route, one item from a clean worktree per call, passing the item's own effort; Git-derived changed paths must match the reply before per-item records. Explicit `--agent foundry:*` sends C1 candidates through Phase 1+2 with the selected specialist. Dirty or non-candidate bridge items use the change-to-specialist table. Effort is never `low`, minimum `medium`, typo/doc `medium`, multi-file/new-feature `xhigh`, default `high`. Effort also picks the Phase 2 tier: a `foundry:sw-engineer` group whose max effort is `high` or `medium` — no `xhigh` item — spawns with `model="sonnet"` (`CAP_SONNET` pool); `medium`-only groups are included on purpose, a deliberate widening of a `high`-only rule, since C1 fall-through work is smaller, never harder; any `xhigh` item keeps its opus frontmatter (`dispatch-phase2.md` §Spawn wave cap).
- **Two-phase challenge**: evidence = problem exists?; suggestion = fix quality?; evidence reject → skip; suggestion reject → self-resolved via `alternative` field; all in `CHALLENGE_LOG` + Step 11 report.
- **COMMIT_MODE**: values, default `each`, `stage` restore caveat, `grouped`→`each` fallback and the distinct-menu rule — Step 3d commit-mode menu. Skipped/discarded only when bulk action = (d) skip-all.
- **GROUP_STRATEGY**: values, default `domain`, typed labels → `$IMPL_DIR/group-labels.tsv` — Step 3d topic-group question. Read only when `COMMIT_MODE=grouped`; no strategy adds a user round-trip at Step 8.
- **DISPATCH_MODE**: values, default `auto`, the 5→8 per-spawn widening and the never-widened correctness guards — Step 3d dispatch-granularity question. Read by Phase 2 for sub-group splitting and wave width only. Distinct from `GROUP_STRATEGY=specialist`, a commit-grouping strategy on its own sentinel. `preview` resolves at the Phase 1 → Phase 2 gate (`dispatch-phase2.md`) to one of the other three.
- **AskUserQuestion usage**: the normal action-item path, after successful source resolution and without diagnostic or conflict recovery, takes at most 3 calls at Step 3d (10-18 pending: two checkbox pages + the commit-mode/topic-group/dispatch/push-intent follow-up; every other nonempty band takes 2, while zero pending with no closed items and a PR takes 1 push-intent call) plus 1 Step 10 push confirmation unless the push intent was an explicit "don't push" — plus 1 Step 10 drift question first when `BASE_FRESH=no`. Picking topic-group (d) without typing labels adds one Step 3d call for the topic-label question.
  - Every decision a later step needs and can see at Step 3d is asked there: commit mode, grouping strategy and typed labels, dispatch width, push intent, and the post-PR browser action. Between Step 3d and the Step 10 push confirmation, nothing is asked on this path.
  - Only `DISPATCH_MODE=preview` adds a call after Step 3d — the user elects it there by choosing Custom.
  - Other paths can add questions for unsupported flags, missing reports, too many conflicts, codemap index gates (all before Step 3d), an unresolved item status, a challenge that timed out twice (one batched question per wave), a lost typed-labels file, or a push-intent question left unanswered at Step 3d (Step 10 then also asks the post-PR question); they are outside this normal-path count.
  - The Step 3d push question carries push intent and the post-PR browser action in one 4-option menu; Step 10 asks the scope-bearing confirmation (Q1, plus Q2 only without a recorded intent) and Step 11 reads the stored post-PR action.
- **`--agent <name>`**: bare name auto-prefixed `foundry:`; must be an implementation agent (not curator); omit the bridge trailer when another agent is selected.
- **Thread resolution via GraphQL** — `isResolved` on `PullRequestReviewThread` (GraphQL only); REST doesn't expose it. `bin/fetch_pr_intel.py` queries it and stamps `resolved` on each inline record; GraphQL failure → every `resolved` false and `resolved_threads_ok: false`.
- **Discussion vs inline**: `source: discussion` records (PR conversation comments) = discussion (`location: discussion`; no Resolve button); `source: inline` records (`pulls/<N>/comments`) = inline (`location: inline`; resolvable). `location: discussion` + `[report]` items: implement-only, no GitHub close action. Surface unresolvable rows through the Status suffix `· thread (no GH resolve)`, not a separate column.
- **Commit attribution** — `[gh]`: `[resolve No.<id>] <reviewer> (gh):`; `[report]`: `[resolve No.<id>] /review finding by <agent> (report: <path>):`.
- **Reference scenarios**: Mode: bare PR# → pr; `42 report` → pr+report; `report` → report mode; bare comment → comment dispatch. Classification: LGTM/emoji → `[info]`; `nit:` → `[gh][suggest]`; resolved thread → keeps `[gh][req]`/`[gh][suggest]` type, `status: resolved`; "must fix" from write-access reviewer → `[gh][req]`. Challenge: present bug → VALID; evidenced later fix → ALREADY_FIXED; affirmative refutation → REJECT; better alternative → suggestion REJECT with alternative.
- Follow-up chains:
  - After push → maintainer reviews, clicks Merge; never approve/comment on PR.
  - Unanswered `[question]` → resolve report only; do NOT post to PR.
  - After merge → `Closes #N`/`Fixes #N` in body auto-closes linked issues; absent keywords → surface gap under `### Closing Keywords` note; don't edit PR body.
- **Review contract cutover:** New REVIEW_V1\_\* reports require the version-one reader and UNVERIFIED/ALREADY_FIXED challenge consumer together. Run the existing find_review_report.py intake before report merging; unsupported/missing envelope blocks with upgrade/rerun recovery. Never strip the prefix to force legacy intake. Preserve review-validation.json open concerns and resolver unverified obligations in the final report with ID/owner/next_check/closure; they never become automatic source edits.

</notes>
