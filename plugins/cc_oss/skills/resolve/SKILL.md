---
name: resolve
description: "OSS maintainer fast-close workflow for GitHub PRs. Three phases: (1) PR intelligence — reads full thread, linked issues, PR body to synthesize contribution motivation and classify every comment into action items; (2) conflict resolution — checks out PR branch (fork-aware via gh pr checkout), merges BASE into it, resolves conflicts semantically using contributor's intent as priority lens; (3) implements each action item via specialist agents (Codex for eligible mechanical items) and commits per the chosen commit mode — one attributed commit per item by default — then pushes back to contributor's fork. Supports three source modes: pr (live GitHub comments only), report (latest /review report findings as action items, no GitHub re-fetch), and pr + report (both sources aggregated and deduplicated in one pass). Also accepts bare comment text for single-comment dispatch. NOT for reply drafting to /oss:analyse findings (use /oss:analyse --reply). NOT for code diff review of PR changes (use /oss:review). NOT for release preparation (use /oss:release). NOT for fixing local bugs unrelated to a PR (use /develop:fix; requires develop plugin). TRIGGER when: PR is ready to close and has open comments, conflicts, or review findings to address; user says 'close this PR', 'resolve comments on PR #N', or 'implement review findings'."
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

- **Preflight side effect**: fetches `origin` to refresh remote refs; never pulls or merges the caller's branch.
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
CHALLENGE_TIMEOUT_S=300  # intel + read-only challenge agents have bounded evidence scopes
AGENT_DEADLINE_S=900     # conflict, specialist, QA/lint agents need editing + validation time; checked at wake-ups, never polled
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

> Read each mode when its load block is reached, follow it in order, then resume here.

<!-- Symbol legend: ⚠ = warning/skipped (non-blocking, proceed with caution) · `! BLOCKED —` = blocked/stop (halt workflow, do not proceed; communication.md form) · ⛔ = the same stop as emitted on stderr by bin/ scripts only -->

<!-- Agent resolution: see _OSS_SHARED/agent-resolution.md -->

## Agent Resolution

`agent-resolution.md` (loaded below) contains: foundry check + fallback table. foundry not installed → use table to substitute each `foundry:X` with `general-purpose`. Agents this skill uses: `foundry:sw-engineer`, `foundry:qa-specialist`, `foundry:linting-expert`, `foundry:doc-scribe`, `foundry:perf-optimizer`, `foundry:solution-architect`, `foundry:challenger`.

<!-- Inline fallback (if agent-resolution.md unreadable): foundry:sw-engineer → general-purpose, foundry:qa-specialist → general-purpose, foundry:linting-expert → general-purpose, foundry:doc-scribe → general-purpose, foundry:perf-optimizer → general-purpose, foundry:solution-architect → general-purpose, foundry:challenger → general-purpose. -->

**Task hygiene** — follow `agent-resolution.md` §Task hygiene (loaded above); keep this workflow's task names and timing below.

<!-- ../../assets/images/resolve-arch.svg diagrams the runs, gates and parallel fan-out. Documentation only, never loaded — update it in the same commit as any change to step order, gate placement, or agent fan-out. -->

## Run structure — text order is not execution order

Agent waits: follow `modes/run-coordination.md` §Agent wait discipline for every spawn.

<!-- loads: modes/run-coordination.md -->

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
_OSS_RESOLVE="$PLUGIN_ROOT/skills/resolve"
python "$PLUGIN_ROOT/bin/render_plugin_root.py" "$_OSS_RESOLVE/modes/run-coordination.md"  # timeout: 5000
```

## State checks — one call

Any ad-hoc look at repository state — branch, HEAD, upstream, ahead/behind, merge-base against the base branch, staged/unstaged/untracked/unmerged files, an in-progress merge, remotes, worktrees — is **one** call to the snapshot below, never a run of separate `git status` / `branch` / `log` / `rev-parse` / `remote` / `worktree list` calls. The guard fences inside steps keep their own git commands: they gate on exact exit codes and are already single calls. Writes (`add`, `commit`, `push`, `merge`) are never replaced.

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r BASE_REF < "${TMPDIR:-/tmp}/resolve-base-ref-${CSID}" 2>/dev/null || BASE_REF=""
python "$PLUGIN_ROOT/bin/git_state_snapshot.py" --base-ref "$BASE_REF"  # timeout: 15000
```

## Step 1: Pre-flight

<!-- loads: modes/preflight.md -->

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
_OSS_RESOLVE="$PLUGIN_ROOT/skills/resolve"
python "$PLUGIN_ROOT/bin/render_plugin_root.py" "$_OSS_RESOLVE/modes/preflight.md"  # timeout: 5000
```

## Step 2: Gather action items

`TaskUpdate(task_id=TASK_GATHER, status="in_progress")` — see the **Zero bookkeeping-only turns** table.

## Step 3a: Report intelligence (report mode only)

<!-- loads: report-intelligence.md -->

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _OSS_RESOLVE < "${TMPDIR:-/tmp}/resolve-oss-resolve-${CSID}" 2>/dev/null || _OSS_RESOLVE=""  # reload (Check 43)
python "$PLUGIN_ROOT/bin/render_plugin_root.py" "$_OSS_RESOLVE/modes/report-intelligence.md"  # timeout: 5000
```

Execute its steps (loaded above).

## Step 3b: PR intelligence

<!-- loads: pr-intelligence.md -->

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _OSS_RESOLVE < "${TMPDIR:-/tmp}/resolve-oss-resolve-${CSID}" 2>/dev/null || _OSS_RESOLVE=""  # reload (Check 43)
IFS= read -r _OSS_SHARED < "${TMPDIR:-/tmp}/resolve-oss-shared-${CSID}" 2>/dev/null || _OSS_SHARED=""  # reload (Check 43)
echo "_OSS_SHARED=$_OSS_SHARED"  # printed (not just written to a file) — pr-intelligence.md's
                                 # spawn prompt substitutes <_OSS_SHARED> with this literal value
python "$PLUGIN_ROOT/bin/render_plugin_root.py" "$_OSS_RESOLVE/modes/pr-intelligence.md"  # timeout: 5000
```

Execute its steps (loaded above). Substitute `<_OSS_SHARED>` in the Agent() prompt with the literal value printed above.

**Overlap — do not end the turn on the `INTEL_AGENT` spawn.** `Agent()` runs in the background, and Step 4 and Step 5 need nothing it produces. In the same response that dispatches it, continue straight into **Step 4** (checkout) and **Step 5** (merge `--no-commit`, conflict detection, 5a tasks), then end the turn. Resume here at Step 3c when the envelope arrives, with the merge state and conflict task table already decided. Skip the jump only in `report` mode with no PR#, where there is no branch to check out. This is dispatch-then-work, never the forbidden waiting turn: no `sleep`, no poll loop, no no-op call held open (`task-lifecycle.md` §After spawning).

A `>20 conflicted files` abort inside Step 5 now fires while `INTEL_AGENT` is still running. Stop as that gate says; the envelope lands unread and is discarded with the run.

## Step 3c: Merge report findings (pr + report mode only)

<!-- loads: report-merge.md -->

> **Output-Routing exemption (canonical — applies to every ACTION_ITEMS table in this skill, Steps 3b/3c/3d)**: ACTION_ITEMS tables are selection-driving, read-in-context enumerations. Write every row to `$IMPL_DIR/action-items-table.md` before the picker; Q1 lists pending items as plain lines within 1,500 characters, or the top items, pending counts by type and that path past the budget. Print the full headed table once as the last text of the reply message that issues Step 3d’s AskUserQuestion, never in an option’s `preview`, never Bash/tool stdout. Global Output Routing (*5+ findings → `.temp/output-*.md`, summary only*) does **not** apply — never divert these tables to a `.temp` report or replace the table with a summary. This follows the global rule’s copy-intent override (*read-in-context, acted-on-immediately → user-facing reply even if long*).

Load and execute in `pr + report` mode only; any other mode skips it.

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _OSS_RESOLVE < "${TMPDIR:-/tmp}/resolve-oss-resolve-${CSID}" 2>/dev/null || _OSS_RESOLVE=""  # reload (Check 43)
python "$PLUGIN_ROOT/bin/render_plugin_root.py" "$_OSS_RESOLVE/modes/report-merge.md"  # timeout: 5000
```

Execute its blocks (loaded above).

## Step 3d: User item selection

<!-- loads: item-selection.md -->

For `bridge:implement`, this gate's recorded user decision naming the selected task set is the approval; workspace is the skill's absolute launch directory and network is `none` unless that decision names otherwise. After the answer, use Write to record the user's decision with its conversation reference, selected task ids, absolute workspace and network in `$IMPL_DIR/bridge-approval.md`; never manufacture approval from an internal invocation. Cite the decision and selected tasks in each dispatch brief; reuse it only for tasks in that set. A subagent may dispatch only when its brief carries the orchestrating skill's recorded approval; otherwise return `blocked` and never dispatch. If no approval exists, use AskUserQuestion before dispatch; if AskUserQuestion is unavailable, return `blocked` and never dispatch.

Gather is complete here (3a, 3b, or 3c done). Report mode also enters this step for nonempty report items, so the same user choice supplies item scope and commit mode. Mark TASK_GATHER `completed` and TASK_SELECT `in_progress` **before** the selection prompt — otherwise the gather `activeForm` keeps driving the spinner through the user-selection window, falsely implying gather is still running. `TaskUpdate(task_id=TASK_GATHER, status="completed")` and `TaskUpdate(task_id=TASK_SELECT, status="in_progress")` both ride as specified in the **Zero bookkeeping-only turns** table.

Load and execute at Step 3d for user item selection.

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _OSS_RESOLVE < "${TMPDIR:-/tmp}/resolve-oss-resolve-${CSID}" 2>/dev/null || _OSS_RESOLVE=""  # reload (Check 43)
python "$PLUGIN_ROOT/bin/render_plugin_root.py" "$_OSS_RESOLVE/modes/item-selection.md"  # timeout: 5000
```

Execute its blocks (loaded above).

## Step 7b join: dispatch and collect conflict resolutions — Run 2 coordination

<!-- loads: modes/dispatch-coordination.md -->

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
_OSS_RESOLVE="$PLUGIN_ROOT/skills/resolve"
python "$PLUGIN_ROOT/bin/render_plugin_root.py" "$_OSS_RESOLVE/modes/dispatch-coordination.md"  # timeout: 5000
```

## Step 4: Checkout PR branch

<!-- loads: modes/checkout.md -->

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
_OSS_RESOLVE="$PLUGIN_ROOT/skills/resolve"
python "$PLUGIN_ROOT/bin/render_plugin_root.py" "$_OSS_RESOLVE/modes/checkout.md"  # timeout: 5000
```

## Steps 5–7: Conflict detection, context, and resolution

<!-- Steps 5–7 defined in conflict-resolution.md — see that file for sub-step numbering -->

`TaskUpdate(task_id=TASK_CONFLICT, status="in_progress")` — see the **Zero bookkeeping-only turns** table; it flips to `completed` at the Step 7b join.

> **Split across three dispatch points, one loaded file.** Step 5 runs in Run 1 immediately after Step 4, beside the intel agent. Steps 6–7a are dispatched in Run 2's fan, beside the Phase 1 early challenge wave (Step 7b join section) — never before the Step 3d picker; 7b is collected at the same join. Load the file once here and execute the parts at their own points — re-`cat` it only if a compaction dropped it from context.

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _OSS_RESOLVE < "${TMPDIR:-/tmp}/resolve-oss-resolve-${CSID}" 2>/dev/null || _OSS_RESOLVE=""  # reload (Check 43)
python "$PLUGIN_ROOT/bin/render_plugin_root.py" "$_OSS_RESOLVE/modes/conflict-resolution.md"  # timeout: 5000
```

Execute its steps (loaded above) at their dispatch points.

## Step 8: Implement action items

<!-- loads: modes/dispatch-closeout.md -->

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
_OSS_RESOLVE="$PLUGIN_ROOT/skills/resolve"
python "$PLUGIN_ROOT/bin/render_plugin_root.py" "$_OSS_RESOLVE/modes/dispatch-closeout.md"  # timeout: 5000
```

## Step 9: Lint and QA gate

`TaskUpdate(task_id=TASK_LINT, status="in_progress")` — see the **Zero bookkeeping-only turns** table.

Implementation envelopes and commit ledgers must be persisted before this refresh.

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# boundary2: post-impl loop, pre-lint gate (compaction-contract.md §Lifecycle)
IFS= read -r _PR_NUMBER < "${TMPDIR:-/tmp}/resolve-pr-number-${CSID}" 2>/dev/null || _PR_NUMBER="n/a"
IFS= read -r _IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" 2>/dev/null || _IMPL_DIR="n/a"
IFS= read -r _PUSH_AUTH < "${TMPDIR:-/tmp}/resolve-push-auth-${CSID}" 2>/dev/null || _PUSH_AUTH="unset"
IFS= read -r _POST_PR < "${TMPDIR:-/tmp}/resolve-post-pr-action-${CSID}" 2>/dev/null || _POST_PR="skip"
IFS= read -r _PUSH_STATUS < "${TMPDIR:-/tmp}/resolve-push-status-${CSID}" 2>/dev/null || _PUSH_STATUS="none"
_PRESERVE="pr=${_PR_NUMBER}, items-implemented, impl-dir=${_IMPL_DIR}, challenge-log=${_IMPL_DIR}/challenge-log.txt, item-tasks=${_IMPL_DIR}/item-tasks.tsv, push-auth=${_PUSH_AUTH} (Step 3d answer), post-pr=${_POST_PR}, push-status=${_PUSH_STATUS} (file resolve-push-status, Step 10 overwrites); next: lint/push/report"
python "$PLUGIN_ROOT/bin/write_skill_contract.py" --keep-sentinel resolve-keep-items "oss:resolve" "lint-qa (after implementation loop)" "$_IMPL_DIR" "${_PRESERVE}; phase-reports=${_IMPL_DIR}/phase2-envelope-*.json, commits=${_IMPL_DIR}/phase2-commits.jsonl, c1-summaries=${_IMPL_DIR}/c1-item-summary.tsv" "resume: re-cat own agent-resolution.md and modes/lint-qa-gate.md; lint/QA gate (Step 9) → push intent recorded at Step 3d; Step 10 still asks the git-push confirmation (modes/push.md), unless the recorded intent is don't push → final report (Step 11)" -- calls_ahead=30 "drop=implementation summaries"  # timeout: 5000
IFS= read -r _OSS_SHARED < "${TMPDIR:-/tmp}/resolve-oss-shared-${CSID}" 2>/dev/null || _OSS_SHARED=""
python "$PLUGIN_ROOT/bin/compaction_boundary.py" "$_OSS_SHARED"
IFS= read -r _OSS_RESOLVE < "${TMPDIR:-/tmp}/resolve-oss-resolve-${CSID}" 2>/dev/null || _OSS_RESOLVE=""  # reload (Check 43)
python "$PLUGIN_ROOT/bin/render_plugin_root.py" "$_OSS_RESOLVE/modes/lint-qa-gate.md"  # timeout: 5000
```

Follow the Boundary step printed above before executing the lint/QA gate; reports are the implementation envelopes and ledgers in the preserved impl-dir.

Execute its steps (loaded above).

## Step 10: Push

<!-- loads: push.md -->

*Skip when report mode with no PR# (`$FORK_REMOTE`, `$HEAD_REF`, `$BASE_REF` unset — no fork branch; workflow ends at Step 11).*

When skipping: `TaskUpdate(task_id=TASK_CLOSE, status="deleted")`. Otherwise: `TaskUpdate(task_id=TASK_CLOSE, status="in_progress")`. Also `TaskUpdate(task_id=TASK_LINT, status="completed")`. Timing: see the **Zero bookkeeping-only turns** table.

Load and execute at Step 10 unless report mode has no PR number.

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _OSS_RESOLVE < "${TMPDIR:-/tmp}/resolve-oss-resolve-${CSID}" 2>/dev/null || _OSS_RESOLVE=""  # reload (Check 43)
python "$PLUGIN_ROOT/bin/render_plugin_root.py" "$_OSS_RESOLVE/modes/push.md"  # timeout: 5000
```

Execute its blocks (loaded above).

## Step 11: Final report

<!-- loads: final-report.md -->

Load and execute at Step 11 for the final report.

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _OSS_RESOLVE < "${TMPDIR:-/tmp}/resolve-oss-resolve-${CSID}" 2>/dev/null || _OSS_RESOLVE=""  # reload (Check 43)
python "$PLUGIN_ROOT/bin/render_plugin_root.py" "$_OSS_RESOLVE/modes/final-report.md"  # timeout: 5000
```

Execute its blocks (loaded above).

## Step 12: Comment dispatch + Codex review loop

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _OSS_RESOLVE < "${TMPDIR:-/tmp}/resolve-oss-resolve-${CSID}" 2>/dev/null || _OSS_RESOLVE=""  # reload (Check 43)
python "$PLUGIN_ROOT/bin/render_plugin_root.py" "$_OSS_RESOLVE/modes/comment-dispatch.md"  # timeout: 5000
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

- **Pre-flight git fetch** — Step 1 always runs `git fetch origin` (unconditional) so all remote tracking refs — including `origin/$BASE_REF` — current before Step 5 merges. Step 5 fetches the target again right before merging and fast-forwards the local `$BASE_REF` branch to it when that branch exists (best effort: skipped with a warning when diverged or checked out in another worktree); the merge itself always uses `origin/$BASE_REF`. The target keeps moving after that merge, so `check_base_fresh.py` re-fetches and checks ancestry twice more: at Step 9 entry (drift → re-merge unattended, then QA; cap 2) and in Step 10's scope block (drift → shown in the push confirmation with a re-sync option). Preflight fetches `origin` without pulling or merging the caller's current branch; a failed fetch stops before checkout.
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
- **AskUserQuestion usage**: the normal action-item path, after successful source resolution and without diagnostic or conflict recovery, takes at most 3 calls at Step 3d (10-18 pending: two checkbox pages + the commit-mode/topic-group/dispatch/push-intent follow-up; every other nonempty band takes 2, while zero pending with no closed items and a PR takes 1 push-intent call) plus 1 Step 10 push confirmation unless the push intent was an explicit "don't push" or a Step 3d run approval (`git-push-run` `Approve`, which is itself the push authorization) — plus 1 Step 10 drift question first when `BASE_FRESH=no`. Picking topic-group (d) without typing labels adds one Step 3d call for the topic-label question.
  - Every decision a later step needs and can see at Step 3d is asked there: commit mode, grouping strategy and typed labels, dispatch width, push intent, and the post-PR browser action. Between Step 3d and the Step 10 push confirmation, nothing is asked on this path.
  - Only `DISPATCH_MODE=preview` adds a call after Step 3d — the user elects it there by choosing Custom.
  - Other paths can add questions for unsupported flags, missing reports, too many conflicts, codemap index gates (all before Step 3d), an unresolved item status, a challenge that timed out twice (one batched question per wave), a lost typed-labels file, or a push-intent question left unanswered at Step 3d (Step 10 then also asks the post-PR question); they are outside this normal-path count.
  - The Step 3d push question has two forms. In `pr` and `pr+report` (Step 4 done, remote, head ref and PR head known) it is the `git-push-run` run approval: `Approve` authorizes the one push of the run, Step 10 asks nothing unless the guard refuses it, and the post-PR action stays `skip` (the report's Link line opens the PR). Otherwise (`report` mode) it is the intent menu: push intent and the post-PR browser action in one 4-option menu, Step 10 then asks the scope-bearing confirmation (Q1, plus Q2 only without a recorded intent) and Step 11 reads the stored post-PR action.
- **`--agent <name>`**: bare name auto-prefixed `foundry:`; must be an implementation agent (not curator); omit the bridge trailer when another agent is selected.
- **Thread resolution via GraphQL** — `isResolved` on `PullRequestReviewThread` (GraphQL only); REST doesn't expose it. `bin/fetch_pr_intel.py` queries it and stamps `resolved` on each inline record; GraphQL failure → every `resolved` false and `resolved_threads_ok: false`.
- **Discussion vs inline**: `source: discussion` records (PR conversation comments) = discussion (`location: discussion`; no Resolve button); `source: inline` records (`pulls/<N>/comments`) = inline (`location: inline`; resolvable). `location: discussion` + `[report]` items: implement-only, no GitHub close action. Surface unresolvable rows through the Status suffix `· thread (no GH resolve)`, not a separate column.
- **Commit attribution** — `[gh]`: `[resolve No.<id>] <reviewer> (gh):`; `[report]`: `[resolve No.<id>] /review finding by <agent> (report: <path>):`.
- **Reference scenarios**: Classification: LGTM/emoji → `[info]`; `nit:` → `[gh][suggest]`; resolved thread → keeps `[gh][req]`/`[gh][suggest]` type, `status: resolved`; "must fix" from write-access reviewer → `[gh][req]`. Challenge: present bug → VALID; evidenced later fix → ALREADY_FIXED; affirmative refutation → REJECT; better alternative → suggestion REJECT with alternative. Mode examples: see **$ARGUMENTS** in inputs.
- Follow-up chains:
  - After push → maintainer reviews, clicks Merge; never approve/comment on PR.
  - Unanswered `[question]` → resolve report only; do NOT post to PR.
  - After merge → `Closes #N`/`Fixes #N` in body auto-closes linked issues; absent keywords → surface gap under `### Closing Keywords` note; don't edit PR body.
- **Review contract cutover:** New REVIEW_V1\_\* reports require the version-one reader and UNVERIFIED/ALREADY_FIXED challenge consumer together. Run the existing find_review_report.py intake before report merging; unsupported/missing envelope blocks with upgrade/rerun recovery. Never strip the prefix to force legacy intake. Preserve review-validation.json open concerns and resolver unverified obligations in the final report with ID/owner/next_check/closure; they never become automatic source edits.

</notes>
