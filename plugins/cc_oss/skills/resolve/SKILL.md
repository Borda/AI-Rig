---
name: resolve
description: "OSS maintainer fast-close workflow for GitHub PRs. Three phases: (1) PR intelligence — reads full thread, linked issues, PR body to synthesize contribution motivation and classify every comment into action items; (2) conflict resolution — checks out PR branch (fork-aware via gh pr checkout), merges BASE into it, resolves conflicts semantically using contributor's intent as priority lens; (3) implements each action item as separate attributed commit via Codex, pushes back to contributor's fork. Supports three source modes: pr (live GitHub comments only), report (latest /review report findings as action items, no GitHub re-fetch), and pr + report (both sources aggregated and deduplicated in one pass). Also accepts bare comment text for single-comment dispatch. NOT for reply drafting to /oss:analyse findings (use /oss:analyse --reply (requires `oss` plugin)). NOT for code diff review of PR changes (use /oss:review). NOT for release preparation (use /oss:release). NOT for fixing local bugs unrelated to a PR (use /develop:fix; requires develop plugin). TRIGGER when: PR is ready to close and has open comments, conflicts, or review findings to address; user says 'close this PR', 'resolve comments on PR #N', or 'implement review findings'."
argument-hint: <PR number or URL> [report] | report | <review comment text> [--no-challenge] [--agent <name>] [--codemap] [--no-codemap] [--worktree] [--keep "<items>"]
disable-model-invocation: true
model: sonnet
allowed-tools: Read, Edit, Write, Bash, Agent, TaskCreate, TaskUpdate, TaskList, AskUserQuestion, EnterWorktree, ExitWorktree
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
  - Omitted → **review-handoff mode**: auto-detect PR from most recent `.reports/review/*/review-report.md` (oss lineage) or `.reports/codex/review/*/review-notes.md` (codex lineage, detected but not parsed — see Step 0 lineage guard)
  - PR number (e.g. `42` or `#42`) or GitHub PR URL → **pr mode**
  - `report` (bare word) → **report mode**: latest review findings as action items; no GitHub re-fetch
  - `42 report` or `<URL> report` → **pr + report mode**: aggregate live GitHub comments + review report, deduplicated in one pass
  - Bare review comment text → **comment dispatch mode** (jumps to Step 12)
- **`--no-challenge`**: optional — skip challenge gate per item; all selected items treated as `VALID`
- **`--no-codemap`**: optional — disable codemap structural context (on by default when codemap installed + index present)
- **`--codemap`**: optional — strict mode: stop and report if codemap not installed or index missing
- **`--agent <name>`**: optional — use `<name>` agent for implementation instead of Codex; must be an implementation agent; bare name auto-prefixed with `foundry:` if no plugin prefix detected (e.g. `--agent sw-engineer` → `foundry:sw-engineer`; `--agent linting-expert` → `foundry:linting-expert`; `--agent doc-scribe` → `foundry:doc-scribe`); explicit prefix also accepted (`--agent foundry:sw-engineer`); see routing table in `action-item-dispatch.md`. **`--agent` also applies to `INTEL_AGENT` (Step 3b thread intelligence)** — explicit `--agent` overrides label/title routing for the thread-intelligence subagent as well, so a docs-focused PR routed via `--agent foundry:doc-scribe` uses doc-scribe for both classification and implementation.

NOT-for additions (scope guards):

- **NOT for non-Python source PRs** (TypeScript, Go, Rust, Java) unless action items are limited to documentation or CI/CD changes — Step 9's lint-qa gate runs Python-specific tools (`ruff`/`mypy`); non-Python PRs will receive partial or no static-analysis review. For non-Python repos, run `/oss:resolve` in `report` mode with manually-curated findings.
- **NOT for branches with uncommitted local edits** — the `report`-mode no-PR# path operates on the current branch as-is; uncommitted changes will be committed alongside the action items. Stash (`git stash`) or commit local edits before invoking; the workflow does not auto-stash.

</inputs>

<constants>
```text
CHALLENGE_TIMEOUT_S=300  # tightened from CLAUDE.md §6 default 900s
CHALLENGE_POLL_S=90      # tightened from CLAUDE.md §6 default 300s
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

- Key boundary: end of Step 8 — per-item implementation loop complete, before Step 9 lint gate. Contract overwrites on each iteration (latest state wins).
- Second boundary: start of Step 11 — before final report write, after push.
- Preserve at boundary 1: PR#, implemented/remaining item state.
- Preserve at boundary 2: final report path, PR#.

</compaction>

<workflow>

<!-- Symbol legend: ⚠ = warning/skipped (non-blocking, proceed with caution) · ⛔ = blocked/stop (halt workflow, do not proceed) -->

<!-- Agent resolution: see _OSS_SHARED/agent-resolution.md -->

## Agent Resolution

```bash
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
```

Contains: foundry check + fallback table. foundry not installed → use table to substitute each `foundry:X` with `general-purpose`. Agents this skill uses: `foundry:sw-engineer`, `foundry:qa-specialist`, `foundry:linting-expert`, `foundry:doc-scribe`, `foundry:perf-optimizer`, `foundry:solution-architect`, `foundry:challenger`.

<!-- Inline fallback (if agent-resolution.md unreadable): foundry:sw-engineer → general-purpose, foundry:qa-specialist → general-purpose, foundry:linting-expert → general-purpose, foundry:doc-scribe → general-purpose, foundry:perf-optimizer → general-purpose, foundry:solution-architect → general-purpose, foundry:challenger → general-purpose. -->

**Task hygiene**: Before creating tasks, call `TaskList`. Per task:

- `completed` if done
- `deleted` if orphaned/irrelevant
- `in_progress` only if genuinely continuing

## Step 1: Pre-flight

Capture caller's branch first — needed for Step 11 restore even when Step 4 (`gh pr checkout`) is skipped or fails mid-checkout. Initialise here so the restore path in Step 11 is always well-defined. Preflight extracted to `bin/resolve_preflight.py` — checks codex availability, `gh` binary + auth, syncs with remote. Caches positive results under `.temp/state/preflight/` (4 h TTL). Writes `CODEX_AVAILABLE` and `GH_OK` to `${TMPDIR:-/tmp}/resolve-preflight-*-<CSID>` files; status messages go to stderr; exits non-zero only on hard failure (`gh` missing/unauthenticated, `git pull` conflict) — `gh` missing/unauthenticated aborts the whole block below, flag parsing never runs.

```bash
# timeout: 45000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
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
```

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# codemap: auto-on if installed; --no-codemap off; --codemap strict (stop if missing)
# loads: detect_codemap.py — consumers: resolve/SKILL.md, review/SKILL.md
_DETECT_CODEMAP="${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/detect_codemap.py"
# codemap flags parsed inside the script, before parse-resolve-args: one argv slot, shlex-tokenised
python "$_DETECT_CODEMAP" --prefix resolve --arguments "$ARGUMENTS" 2>&1  # timeout: 5000
[ $? -ne 0 ] && { echo "! BLOCKED — codemap strict mode requested but codemap not installed or index missing"; exit 1; }
IFS= read -r CODEMAP_ENABLED < "${TMPDIR:-/tmp}/resolve-codemap-enabled-${CSID}" 2>/dev/null || CODEMAP_ENABLED="false"
IFS= read -r CODEMAP_CURRENCY < "${TMPDIR:-/tmp}/resolve-codemap-currency-${CSID}" 2>/dev/null || CODEMAP_CURRENCY="off"
IFS= read -r _OSS_SHARED < "${TMPDIR:-/tmp}/resolve-oss-shared-${CSID}" 2>/dev/null || _OSS_SHARED=""  # reload (Check 41)
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
# oss lineage → .reports/review/; codex lineage → .reports/codex/review/
REVIEW_FILE=$(ls -t .reports/review/*/review-report.md .reports/codex/review/*/review-notes.md 2>/dev/null | head -1)
if [ -z "$REVIEW_FILE" ]; then
    echo "No review output found in .reports/review/ or .reports/codex/review/ — run /review <PR#> first, or provide a PR number"
    exit 1
fi
case "$REVIEW_FILE" in
    .reports/codex/review/*)
        echo "! BLOCKED — newest review is codex-lineage ($REVIEW_FILE); this parser reads oss:review's .reports/review/*/review-report.md section schema only, not codex's flat H1/H2/M1-bullet schema. Falling through would silently miss any blocking findings that review recorded. Provide a PR number explicitly (\`/oss:resolve <PR#>\`), or run /oss:review on this PR to produce a compatible report."
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
# no codemap/keep flags in parse-resolve-args.py — strip before passing (parsed above)  # timeout: 3000
eval "$(python "${CLAUDE_PLUGIN_ROOT}/bin/parse-skill-flags.py" --flags no-codemap,codemap,worktree "$ARGUMENTS")"  # timeout: 5000
ARGUMENTS="$CLEAN_ARGS"
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
```

<!-- branch: unsupported-flags — isolated; ≤1 call; fires only when unknown flags present -->

**Unsupported flag check** — after `eval`, scan remaining `$ARGUMENTS` for any `--<token>` not in `{--no-challenge, --agent, --codemap, --no-codemap, --worktree}`. Found → invoke `AskUserQuestion` — (a) **Abort** (stop, re-invoke with correct flags) · (b) **Continue ignoring** (skip unknown tokens). Supported: `--no-challenge`, `--agent <name>`, `--codemap`, `--no-codemap`, `--worktree`, `--keep "<items>"`.

- `MODE="pr+report"` → strip `report` suffix conceptually (already captured separately); find latest review report via `ls -t .reports/review/*/review-report.md .reports/codex/review/*/review-notes.md 2>/dev/null | head -1`; no report found → warn but continue in pr mode; newest match is codex-lineage (`.reports/codex/review/*/review-notes.md`) → this parser can't read its schema — warn `⚠ newest review is codex-lineage, unsupported by this parser — GitHub comments only, no report findings merged` and continue in pr mode (same non-fatal treatment as "no report found")
- `MODE="report"` → find latest review report via `ls -t .reports/review/*/review-report.md .reports/codex/review/*/review-notes.md 2>/dev/null | head -1`; no report found → stop with: "No review report found in .reports/review/ or .reports/codex/review/ — run /review \<PR#> first, or provide a PR number"; newest match is codex-lineage → stop with the Step 0 lineage-guard message (same wording as the auto-detect block above); extract PR# from header if present; no PR# in header → add branch safety check before Step 8 — `CURRENT=$(git branch --show-current); DEFAULT=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's|refs/remotes/origin/||'); [ -z "$DEFAULT" ] && DEFAULT=$(git remote show origin 2>/dev/null | grep 'HEAD branch' | awk '{print $NF}'); [ -z "$DEFAULT" ] && { printf "! BLOCKED — cannot determine default branch; refusing to proceed\n"; exit 1; }; [ "$CURRENT" = "$DEFAULT" ] && { echo "⛔ On default branch '$CURRENT' — report mode without PR# must not operate on default branch; check out a feature branch first"; exit 1; }`
- `MODE="pr"` → continue Step 2
- `MODE="comment-dispatch"` → branch safety check before Step 12: `export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"; IFS= read -r WT_ENABLED < "${TMPDIR:-/tmp}/oss-resolve-worktree-${CSID}" 2>/dev/null; [ "$WT_ENABLED" = "true" ] || WT_ENABLED=false; CURRENT=$(git branch --show-current); DEFAULT=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's|refs/remotes/origin/||'); [ -z "$DEFAULT" ] && DEFAULT=$(git remote show origin 2>/dev/null | grep 'HEAD branch' | awk '{print $NF}'); [ -z "$DEFAULT" ] && { printf "! BLOCKED — cannot determine default branch; refusing to proceed\n"; exit 1; }; [ "$CURRENT" = "$DEFAULT" ] && { echo "⛔ On default branch '$CURRENT' — comment dispatch must not commit to default branch"; exit 1; }; [ "$WT_ENABLED" = "true" ] && echo "⚠ --worktree has no effect in comment-dispatch mode"` → jump to Step 12

### Reject-gate check (all modes with a known `PR_NUMBER`)

`oss:review`'s acceptance gate can reject a PR at the premise level — `Gate: REJECT_<GROUND> @<sha>`, one of `GOAL`/`CONDUCT`/`SCOPE`/`LICENSE`/`DUPLICATE`/`REVERTED`/`SPAM`/`PHILOSOPHY` (see `oss:review` SKILL.md Stage 1 for what each means). That's a premise problem, not something `/oss:resolve` fixes by editing code — never start the fix pipeline on a PR still in that state, regardless of which of the 8 grounds fired. `Gate: BLOCK` and anything else (`PASS`, or no `Gate:` field at all — pre-gate reports) impose no restriction here; those are ordinary fixable findings, exactly what resolve exists for.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PR_NUMBER < "${TMPDIR:-/tmp}/resolve-pr-number-${CSID}" 2>/dev/null || PR_NUMBER=""
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/find_review_report.py" --pr "$PR_NUMBER"  # timeout: 6000
```

## Step 1b: Create all workflow tasks upfront

After `PR_NUMBER` and `MODE` resolved above, create all major-step tasks now. Store each returned `task_id` for step-level `TaskUpdate` calls. Conditional tasks: include condition in subject brackets; cancel via `TaskUpdate(status="deleted")` at skip point — never leave conditional tasks pending.

```text
TASK_GATHER   = TaskCreate(subject="Step 2: Gather action items — PR #<N>",              activeForm="Gathering action items for PR #<N>")
TASK_SELECT   = TaskCreate(subject="Step 3: Select action items — PR #<N>",               activeForm="Selecting action items")
TASK_CHECKOUT = TaskCreate(subject="Step 4: Checkout PR branch [if pr mode]",             activeForm="Checking out PR branch")
TASK_CONFLICT = TaskCreate(subject="Steps 5–7: Conflict resolution [if pr mode]",         activeForm="Resolving conflicts")
TASK_IMPL     = TaskCreate(subject="Step 8: Implement selected items [if items selected]", activeForm="Implementing action items")
TASK_LINT     = TaskCreate(subject="Step 9: Lint and QA gate",                             activeForm="Running lint and QA")
TASK_CLOSE    = TaskCreate(subject="Steps 10–11: Push and final report [if pr mode]",      activeForm="Pushing to fork and reporting")
```

## Step 2: Gather action items

```text
TaskUpdate(task_id=TASK_GATHER, status="in_progress")
```

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
cat "$_OSS_RESOLVE/modes/pr-intelligence.md"  # timeout: 5000
```

Execute its steps (loaded above).

## Step 3c: Merge report findings (pr + report mode only)

*Skip when in pr mode.*

! NO user input in this step — deterministic merge only; Step 3d handles all user selection.

When mode == **pr + report**:

Find + read latest review report (`ls -t .reports/review/*/review-report.md 2>/dev/null | head -1`). Parse findings same as Step 3a.

**Deduplication**:

- Report finding matches GitHub item at same `file:line` → drop report item; annotate GitHub item with `(also flagged by /review — <owner-agent>)` where `<owner-agent>` is the report item's owner agent from taxonomy; update Author to `@login + <owner-agent>`
- Semantic match (same file, no exact line, similar description) → drop report item; same annotation and Author update
- No match → append report finding as `[report]` item

**Re-prefix GitHub items** in deduplication: `[gh][req]` stays `[gh][req]`; `[suggest]` → `[gh][suggest]`, `[question]` → `[gh][question]` if not already prefixed. GitHub items carry `[gh]` prefix in all modes — no change needed for items already classified with `[gh]` in Step 3b.

### Sources confirmation

Print Sources block (same format as Step 3a template; Mode=pr + report · PR=#<N> · GitHub=Read — PR body · <N> comments · <N> reviews · <N> inline code comments · Report=Read <path>) right before merge summary and action item table.

Result: single merged `ACTION_ITEMS`. GitHub items first (`[gh][req]`/`[gh][suggest]`), then `[report]` items. Print merge summary before table:

```text
Report merged: <N> findings from /review · <M> deduplicated against GitHub comments · <K> added as [report] items
```

Print merged ACTION_ITEMS as markdown table to terminal immediately after the merge summary (severity descending; same columns as pr-intelligence.md table):

> **Output-Routing exemption (canonical — applies to every ACTION_ITEMS table in this skill, Steps 3b/3c/3d)**: ACTION_ITEMS tables are selection-driving, read-in-context enumerations the user must see before the Step 3d picker. Always print inline to terminal regardless of row count. Global Output Routing (*5+ findings → `.temp/output-*.md`, summary only*) does **not** apply — never divert these tables to a file. This makes explicit what the global rule's own copy-intent override (*read-in-context, acted-on-immediately → terminal only even if long*) already implies.

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

Summary ≤60 chars. Notes = `—` when empty; carries commit SHA for `[done]` rows and classification verdicts — never `file:line`, which the `file`/`line` fields already hold. Print only when merged ACTION_ITEMS has ≥1 row.

`location` is a field, not a column — it stays in `action-items.jsonl` and drives resolve routing, but gets no column here: `[report]` origin is already carried by `Type` and `Author`. Its one non-redundant bit is resolvability, so preserve that the same way every other table in this skill does — **append `· thread (no GH resolve)` to Status for `location: discussion` rows** (same rule as Step 11's table and the Step 3d picker). Never reintroduce a `Loc` column to restate what `Type`, `Author`, and that suffix already say. The merged table is the authoritative set for Step 3d selection — it supersedes the pre-merge table shown in Step 3b.

## Step 3d: User item selection

<!-- branch: main-path — item-selection (always fires in step 3d; ≤6 items = one merged call incl. commit-mode, >6 = two calls) -->

! IMPORTANT — invoke `AskUserQuestion` tool directly. Never write options as plain text.

Gather is complete here (3b/3c done). Mark TASK_GATHER `completed` and TASK_SELECT `in_progress` **before** the selection prompt — otherwise the gather `activeForm` keeps driving the spinner through the user-selection window, falsely implying gather is still running:

```text
TaskUpdate(task_id=TASK_GATHER, status="completed")
TaskUpdate(task_id=TASK_SELECT, status="in_progress")
```

Pending items = ACTION_ITEMS where type ≠ `[done]` and type ≠ `[info]`. Zero pending → set `SELECTED_ITEMS` = all pending IDs, skip to Step 3e.

Sort all pending items by severity descending (most impactful first). Constraint: max 3 items/question, max 4 questions/call. Note: `AskUserQuestion` always appends "Type something" outside the option list — 3 items + Type something = 4 visible per page; keep ≤3 items per group.

**Call layout — pick by pending-item count** (each AskUserQuestion window is pure human idle, median ~15 min — merge whenever the 4-question ceiling allows):

- **≤6 pending items → ONE call**: Q1–Q2 = item checkboxes (≤3 each; one question when ≤3 items), next question = bulk action, LAST question = commit-mode menu (full 4-option text below, verbatim). Bulk action resolving to (d) Skip all → discard the commit-mode answer (nothing will be committed). This satisfies the distinct-menus rule below — the menus stay separate questions; only the round-trips merge.
- **>6 pending items → two calls**: Q1–Q3 = item checkboxes, last question = bulk action; commit-mode menu asked as the follow-up call after the bulk action resolves (flow below).

**Bulk action — hard rule**: single-select, fixed options, always positioned after every item-checkbox question. Never put items in it. Items span ≤3 groups regardless of how many type categories exist.

```text
Bulk-action question — multiSelect: FALSE (single-select only — user picks one bulk action, not a checklist)
"Or choose a bulk action:"
  (a) +All [req] — implement all required items
  (b) +All [suggest] — implement all suggested items
  (c) ALL (req + suggest) — implement all pending items
  (d) Skip all — skip all items, exit
```

**Bulk-action resolution**:

- (a) → `SELECTED_ITEMS` = all `[req]` IDs; skip Call 2 in two-call flow; proceed to commit-mode resolution
- (b) → `SELECTED_ITEMS` = all `[suggest]` IDs; skip Call 2 in two-call flow; proceed to commit-mode resolution
- (c) → `SELECTED_ITEMS` = all pending [req+suggest] IDs; skip Call 2; proceed to commit-mode resolution (do NOT hardcode `COMMIT_MODE` — scope and commit mode are orthogonal; user still chooses granularity)
- (d) → stop; print `→ All items skipped.`; jump to Step 11 (merged flow: discard the commit-mode answer from the same call)
- unanswered / "Type something" → use checked IDs from the item questions; proceed to commit-mode resolution; `COMMIT_MODE = each` (default)

**Item checkbox questions**: each `multiSelect: true`, header "Items to implement:", labels: `<type> #<id>: <summary>` (≤55 chars), description: `<file:line> · @<author>` + for `location: discussion` items append `· thread (no GH resolve)`. Fill in severity order (≤3 items each). If >9 pending items: two calls — print `→ N pending items — selecting in 2 calls` before call 1; Call 2 gets remaining items + the bulk-action question again; "ALL (req + suggest)" in Call 1 → skip Call 2.

**≥20 pending items — context-budget mode**: skip per-item checkboxes; print compressed table (type · id · summary ≤40 chars · file) **inline to terminal** (Output-Routing exemption from Step 3c applies — never divert to `.temp`) then ONE call: bulk-action question + commit-mode question (≤6-item merged layout applies — only 2 questions needed).

<!-- branch: main-path — commit-mode (same call in the ≤6-item merged layout; separate call 2 only in the >6-item flow; skipped only when bulk action = (d) skip) -->

**Commit mode** — in the merged layout this menu is the LAST question of the same call; in the two-call flow ask it immediately after the bulk action resolves to (a), (b), (c), or unanswered (skip only when (d) skip-all). Commit mode is always the user's choice; item scope ((c) = all items) never implies a commit mode:

```text
AskUserQuestion: "Commit mode for selected items:"
  (a) Each item separately — one commit per action item (default)
  (b) By topic group — ask for topic labels; group related items into themed commits
  (c) All at once — single commit after all items
  (d) Stage only — no commits; stay staged on PR branch (⚠ cannot cleanly restore to $SAVED_BRANCH after Step 11; governs Step 8 action-item commits only — the Steps 5–7 merge commit is unconditional and always created)
```

**ESSENTIAL — all 4 options mandatory, never emit fewer than 4** (empirically motivated: LLMs tend to drop (b) By topic group and (d) Stage only — both must appear every time). Distinct menu from the bulk-action question, never merge or pull its options in — this menu sets commit MODE (how to commit), the bulk action sets item SCOPE (which items). Sharing one AskUserQuestion call is fine; sharing one menu never is.

Set `COMMIT_MODE`:

- (a) → `each`
- (b) → `grouped`
- (c) → `all`
- (d) → `stage`
- unanswered → `each` (default)

```text
TaskUpdate(task_id=TASK_SELECT, status="completed")
```

## Step 3e: Create tasks for selected items

> Step 2 gather task already marked `completed` at top of Step 3d.

For each item in `SELECTED_ITEMS`, call `TaskCreate` **once per item** — one task per action item; scoped to selected items only, not all pending (avoids bloat when 20+ items exist but only a subset is selected):

```text
TaskCreate(
  subject="<type> <summary> — PR #<number>",   # <type> = full string with brackets, e.g. "[gh][req] rename param — PR #42"
  description="Author: @<author> | Change: <change> | Severity: <severity> | File: <file:line or '—'> | <full_comment_text>",
  activeForm="Implementing: <summary>"          # <summary> truncated to 80 chars
)
```

Store returned task ID in each `SELECTED_ITEMS` entry as `task_id`; the orchestrator holds this `{item_id: task_id}` map in-context and flips each task live during the Step 8 loop. **Applies to `pr` and `pr+report` modes only** — these are the only modes that run Step 3b (which initialises `IMPL_DIR`) and Step 3e. `report` mode skips both steps and has no per-item tasks.

## Step 4: Checkout PR branch

**Worktree isolation (opt-in `--worktree`)** — run this FIRST, before the `gh` check + checkout below, so the checkout, Phase-2 specialist worktrees, cherry-picks, and push all happen off an isolated worktree and the caller's main tree/branch never change. Skip when `WT_ENABLED != true` or `MODE = report` with no PR#.

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r WT_ENABLED < "${TMPDIR:-/tmp}/oss-resolve-worktree-${CSID}" 2>/dev/null; [ "$WT_ENABLED" = "true" ] || WT_ENABLED=false
IFS= read -r _OSS_SHARED < "${TMPDIR:-/tmp}/resolve-oss-shared-${CSID}" 2>/dev/null || _OSS_SHARED="$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/resolve_shared_path.py" oss skills/_shared 2>/dev/null)"
[ "$WT_ENABLED" = "true" ] && [ -f "$_OSS_SHARED/worktree-isolation.md" ] && cat "$_OSS_SHARED/worktree-isolation.md"  # timeout: 5000
```

`WT_ENABLED=true` → follow §Enter (base off HEAD, `EnterWorktree(path=…)`) + §resolve (do NOT alter checkout/mutex/fingerprint/push — Enter is the only addition; the mutex path is worktree-invariant, Step 11 restore becomes a harmless no-op, and the push still targets the fork). Then continue Step 4 below inside the worktree.

*Skip only when `MODE = report` with no PR# (`$PR_NUMBER` unset — no remote branch to check out). In pr mode, runs unconditionally regardless of `SELECTED_ITEMS` — conflict resolution must happen even when 0 action items selected.*

When skipping:

```text
TaskUpdate(task_id=TASK_CHECKOUT, status="deleted")
TaskUpdate(task_id=TASK_CONFLICT, status="deleted")
```

```text
TaskUpdate(task_id=TASK_CHECKOUT, status="in_progress")
```

**`gh` availability check** — hard prereq; `gh pr checkout` has no fallback path:

```bash
command -v gh >/dev/null 2>&1 || { echo "! BLOCKED — gh CLI required; install: https://cli.github.com"; exit 1; }  # timeout: 3000
```

**Branch-safety pre-check** — must run BEFORE `gh pr checkout` so a wrong-branch commit is impossible (per `git-commit.md` Gate 2). Verify the PR's `headRefName` is not the repo's default branch — `gh pr checkout` of a same-repo PR whose HEAD = default branch would land us on default and any later commit (Step 8) would violate Gate 2:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/resolve_pr_refs.py" --pr "<PR#>"  # timeout: 15000
```

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# fresh shell (Check 41) — reload what resolve_pr_refs.py persisted above
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
    gh pr checkout <PR#> --branch "$PR_HEAD_REF" \
        || { echo "⛔ gh pr checkout failed — aborting (network, branch deleted, auth expired, or local conflicts)"; exit 1; }   # timeout: 15000
fi
```

`gh pr checkout` auto-handles forks — adds contributor's remote, configures tracking. Verify checkout landed on expected branch — if not, abort before Step 8 can commit:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# fresh shell (Check 41) — else gates below dead code
IFS= read -r HEAD_REF < "${TMPDIR:-/tmp}/resolve-head-ref-${CSID}" 2>/dev/null || HEAD_REF=""
IFS= read -r IS_CROSS_REPO < "${TMPDIR:-/tmp}/resolve-is-cross-repo-${CSID}" 2>/dev/null || IS_CROSS_REPO=""
[ -n "$HEAD_REF" ] && [ -n "$IS_CROSS_REPO" ] || { echo "⛔ Step 4 verify: HEAD_REF/IS_CROSS_REPO sentinels missing — checkout state unverifiable, aborting before Step 8 can commit"; exit 1; }
PR_HEAD_REF="$HEAD_REF"
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
IFS= read -r IS_CROSS_REPO < "${TMPDIR:-/tmp}/resolve-is-cross-repo-${CSID}" 2>/dev/null || IS_CROSS_REPO="false"
if [ "$IS_CROSS_REPO" = "true" ]; then
    IFS= read -r FORK_REMOTE < "${TMPDIR:-/tmp}/resolve-head-repo-owner-${CSID}" 2>/dev/null || FORK_REMOTE=""
    [ -n "$FORK_REMOTE" ] || FORK_REMOTE=$(gh pr view "<PR#>" --json headRepositoryOwner --jq .headRepositoryOwner.login) # sentinel-miss fallback only # timeout: 6000
    PR_REF="$PR_URL"
else
    FORK_REMOTE="origin"
    PR_REF="#$PR_NUMBER"
fi
echo "$PR_REF" > "${TMPDIR:-/tmp}/resolve-pr-ref-${CSID}"  # timeout: 3000
echo "$FORK_REMOTE" > "${TMPDIR:-/tmp}/resolve-fork-remote-${CSID}"  # read by Step10 push gate
# soft-verify — layouts vary across gh versions
git remote get-url "$FORK_REMOTE" >/dev/null 2>&1 \
    || echo "⚠ Remote $FORK_REMOTE not registered — Step 10 will add it before push" # timeout: 3000
```

`FORK_REMOTE`: contributor login (e.g. `alice`) for forks, `origin` for same-repo. Push always `git push` — tracking configured by `gh pr checkout`.

`PR_REF`: the token Step 8's commit messages embed for this PR — `#<N>` when the commit lands same-repo (`FORK_REMOTE=origin`), or the full `PR_URL` when it lands in the contributor's fork (bare `#N` there would resolve against the fork's own issues, not this repo's PR — a cross-repo false link). Persisted to `${TMPDIR:-/tmp}/resolve-pr-ref-${CSID}` for Step 8 to read.

```text
TaskUpdate(task_id=TASK_CHECKOUT, status="completed")
```

## Steps 5–7: Conflict detection, context, and resolution

<!-- Steps 5–7 defined in conflict-resolution.md — see that file for sub-step numbering -->

```text
TaskUpdate(task_id=TASK_CONFLICT, status="in_progress")
```

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _OSS_RESOLVE < "${TMPDIR:-/tmp}/resolve-oss-resolve-${CSID}" 2>/dev/null || _OSS_RESOLVE=""  # reload (Check 41)
cat "$_OSS_RESOLVE/modes/conflict-resolution.md"  # timeout: 5000
```

Execute its steps (loaded above).

```text
TaskUpdate(task_id=TASK_CONFLICT, status="completed")
```

## Step 8: Implement action items

*Skip when `SELECTED_ITEMS` is empty — jump to Step 9.*

When skipping:

```text
TaskUpdate(task_id=TASK_IMPL, status="deleted")
```

```text
TaskUpdate(task_id=TASK_IMPL, status="in_progress")
```

**Soft cap: 8 bridge implementation calls per session** — skip this cap when `--agent <name>` selects a non-bridge implementation agent:

```bash
# computed here for cap-threshold branch (full resolve in action-item-dispatch.md)
eval "$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/parse-skill-flags.py" --flags worktree --value-flags agent "$ARGUMENTS")"  # timeout: 5000
_RESOLVE_IMPL_AGENT="${VALUE_AGENT:-bridge:implement}"
echo "$_RESOLVE_IMPL_AGENT"   # item count belongs to the prose gate below; SELECTED_ITEMS only enters the shell in action-item-dispatch.md's prelude, later than this
```

<!-- branch: codex-cap — only when codex agent AND N>8 items; adds 1 call (max 5 if user proceeds; worst case = item-select + commit-mode + codex-cap + push-auth + post-pr) -->

If `_RESOLVE_IMPL_AGENT = bridge:implement` AND `SELECTED_ITEMS` has > 8 items, invoke `AskUserQuestion`: "N items selected — bridge implementation cap is 8 per session. Split into batches?" Options: (a) Apply first 8 now, re-run for remainder · (b) Apply all [req] only (if ≤8) · (c) Proceed anyway (sequential, may be slow). For non-bridge agents, skip this gate.

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

**Review pre-flight cache** — reuse the per-module codemap answers `/review` already computed, so the Step 8 blast-radius scan issues 0 duplicate pre-flight queries when a fresh review artifact exists (contract + artifact shape in `$_DEV_SHARED/codemap-context.md` §Review→resolve pre-flight cache; requires `develop`/`oss` codemap wiring). Locate the latest review run-dir and materialize the per-module cache once, before the per-item loop:

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

`action-item-dispatch.md` (loaded above) — execute its prelude (IMPL_AGENT routing, IMPL_DIR init, blast-radius scan, plus a branch mutex + HEAD fingerprint so a second concurrent resolve aborts and an external mid-flight write is surfaced at merge-back), then run its three-phase dispatch directly in the orchestrator: Phase 1 challenge (parallel by domain, read-only) → Phase 2 implementation (parallel, one isolated `git worktree` per specialist; groups formed by specialist then a file-ownership + import-coupling tiebreak so items that would collide on the same file — or across an import edge — land in one worktree) → Phase 3 merge-back (sequential cherry-pick, whole worktree groups ordered most-central-first so foundational commits land before their dependents, `TaskUpdate` per item as its commit lands). `TaskUpdate` calls stay orchestrator-owned throughout — Phase 1/2 subagents never touch the task list (a subagent cannot drive the parent's task list); only Phase 3, run by the orchestrator itself after each cherry-pick, flips a task to `completed`. This is why tasks flip in item-priority order during Phase 3 even though the work that produced them ran concurrently in Phase 2.

`action-item-dispatch.md` caps a single pass at 20 items and gates >20 behind `AskUserQuestion` (split into ≤20 batches · `[req]` only · proceed with all). On "proceed with all", run the same three-phase dispatch over every item — more specialist groups in Phase 2, slower Phase 3 merge-back at that size, but no separate code path.

```text
TaskUpdate(task_id=TASK_IMPL, status="completed")
```

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# boundary1: post-impl loop, pre-lint gate (compaction-contract.md §Lifecycle)
IFS= read -r _PR_NUMBER < "${TMPDIR:-/tmp}/resolve-pr-number-${CSID}" 2>/dev/null || _PR_NUMBER="n/a"
IFS= read -r _KEEP < "${TMPDIR:-/tmp}/resolve-keep-items-${CSID}" 2>/dev/null || _KEEP=""
_PRESERVE="pr=${_PR_NUMBER}, items-implemented; next: lint/push/report"
[ -n "$_KEEP" ] && _PRESERVE="$_PRESERVE; user-keep: $_KEEP"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/write_skill_contract.py" "oss:resolve" "lint-qa (after implementation loop)" "n/a" "${_PRESERVE}" "lint/QA gate (Step 9) → push (Step 10) → final report (Step 11)"  # timeout: 5000
```

## Step 9: Lint and QA gate

```text
TaskUpdate(task_id=TASK_LINT, status="in_progress")
```

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _OSS_RESOLVE < "${TMPDIR:-/tmp}/resolve-oss-resolve-${CSID}" 2>/dev/null || _OSS_RESOLVE=""  # reload (Check 41)
cat "$_OSS_RESOLVE/modes/lint-qa-gate.md"  # timeout: 5000
```

Execute its steps (loaded above).

```text
TaskUpdate(task_id=TASK_LINT, status="completed")
```

## Step 10: Push

*Skip when report mode with no PR# (`$FORK_REMOTE`, `$HEAD_REF`, `$BASE_REF` unset — no fork branch; workflow ends at Step 11).*

When skipping:

```text
TaskUpdate(task_id=TASK_CLOSE, status="deleted")
```

```text
TaskUpdate(task_id=TASK_CLOSE, status="in_progress")
```

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# fresh shell (Check 41) — unbound here → auth gate shows empty push scope
IFS= read -r FORK_REMOTE < "${TMPDIR:-/tmp}/resolve-fork-remote-${CSID}" 2>/dev/null || FORK_REMOTE=""
IFS= read -r HEAD_REF < "${TMPDIR:-/tmp}/resolve-head-ref-${CSID}" 2>/dev/null || HEAD_REF=""
IFS= read -r BASE_REF < "${TMPDIR:-/tmp}/resolve-base-ref-${CSID}" 2>/dev/null || BASE_REF=""
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/derive_fork_remote.py" --fork-remote "$FORK_REMOTE" --head-ref "$HEAD_REF" --base-ref "$BASE_REF"  # timeout: 10000
```

<!-- branch: main-path — push-auth (call 3 of 4 normal / 4 of 5 with codex-cap) -->

**Push authorization gate** — per `git-commit.md` push-safety rule ("Never push without explicit user confirmation"), invoke `AskUserQuestion` before any `git push`. The question must surface:

- Target remote and branch: `$FORK_REMOTE/$HEAD_REF`
- Diff stat: `$PUSH_STAT` (e.g. `3 files changed, 47 insertions(+), 12 deletions(-)`)
- Commit count and last subject: `$PUSH_COUNT commits — last: "$LAST_SUBJECT"`

Options:

- (a) **Push** — proceed with `git push` below (default)
- (b) **Skip push** — stop after Step 9; user pushes manually later

Only proceed to the `git push` below on option (a). On option (b): print `` → Push skipped — run `git push` manually when ready. `` and jump to Step 11.

```bash
git push # timeout: 30000
```

Push rejected → fallback:

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
gh pr view <PR_NUMBER> --json headRefOid,commits --jq '.commits[-3:] | .[].messageHeadline' # timeout: 6000
```

## Step 11: Final report

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# boundary2: pre-final-report write (compaction-contract.md §Lifecycle)
IFS= read -r _PR_NUMBER < "${TMPDIR:-/tmp}/resolve-pr-number-${CSID}" 2>/dev/null || _PR_NUMBER="n/a"
IFS= read -r _KEEP < "${TMPDIR:-/tmp}/resolve-keep-items-${CSID}" 2>/dev/null || _KEEP=""
_PRESERVE="pr=${_PR_NUMBER}, final-report=pending-write"
[ -n "$_KEEP" ] && _PRESERVE="$_PRESERVE; user-keep: $_KEEP"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/write_skill_contract.py" "oss:resolve" "final-report (after push)" "n/a" "${_PRESERVE}" "write final report → post-PR action gate"  # timeout: 5000
IFS= read -r _OSS_RESOLVE < "${TMPDIR:-/tmp}/resolve-oss-resolve-${CSID}" 2>/dev/null || _OSS_RESOLVE=""  # reload (Check 41)
cat "$_OSS_RESOLVE/templates/resolve-report.md"  # timeout: 5000
```

Report template (loaded above) — use for section structure.

**Print the final report — including the full Action Items resolution table — inline to terminal.**

> **Output-Routing exemption (canonical)**: the Step 11 final report is a read-in-context, acted-on-immediately resolution summary the user must see to confirm every item and how it resolved. Always print the full Action Items table inline to terminal regardless of row count — this is the whole point of the report. Global Output Routing (*5+ findings → `.temp/output-*.md`, summary only*) does **not** apply; never divert this table to a file in place of showing it. Writing a durable copy to `.reports/resolve/` in addition is fine, but the inline terminal print is mandatory and never replaced by a prose summary.

**Action Items table** — one row per selected item, columns: `#` | `Type` | `Change` | `Status` | `Resolution` | `Commit`:

- `Status`: ✓ implemented · ⊘ skipped · ✗ challenge-rejected
- `Resolution`: `implemented` · `self-resolved` (challenger provided alternative) · `skipped` · `challenge-rejected`
- `Change`: action type — `code` / `test` / `docs` / `config` / `ci` / `style` / `refactor`
- `Commit`: short SHA (7 chars); `—` when `COMMIT_MODE=stage`
- For `location: discussion` rows append `· thread (no GH resolve)` to Status — no GitHub Resolve button exists for PR main-thread comments

Include `### Challenge Log` section in report, columns: `#` | `Finding` | `Evidence` | `Suggestion` | `Resolution`. Every cell must be self-contained — reader gets full context from that row alone, never by cross-referencing another row or recalling earlier conversation:

- `Finding`: one-line gist of the reviewer's comment (from `finding` in `CHALLENGE_LOG`) — what was actually flagged, not just its id
- `Evidence`: verdict + reason on one line, e.g. `VALID — <evidence_why>` or `REJECT — <evidence_why>`. Never print bare `VALID`/`REJECT` with no reason
- `Suggestion`: verdict + reason, same rule, e.g. `VALID — <suggestion_why>` or `REJECT — <suggestion_why>`; `—` for rows with `evidence=REJECT` (suggestion never evaluated)
- `Resolution`: concrete outcome, never a bare label. `detail=pending-impl:<id>` → backfill before printing: look up that id's `Commit` SHA in the Action Items table above and run `git log -1 --format=%s <sha>` for the one-line summary of what was actually changed; render as `as-suggested: <that summary>`. `detail=<alternative text>` (self-resolved rows) → render as `self-resolved: <alternative text>`. `detail=<evidence_why>` (rejected rows) → render as `rejected: <evidence_why>`. If a commit lookup fails, state `as-suggested: (commit summary unavailable, see commit <sha>)` — never fall back to printing the bare word `as-suggested` alone

Omit section when `--no-challenge`.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r SAVED_BRANCH < "${TMPDIR:-/tmp}/resolve-saved-branch-${CSID}" 2>/dev/null || SAVED_BRANCH=""
# stage mode: skip restore, else staged work lost
if [ "$COMMIT_MODE" = "stage" ]; then
    echo "⚠ COMMIT_MODE=stage: changes are staged on $(git branch --show-current) — restore to $SAVED_BRANCH skipped to preserve staged work. Run: git stash && git switch $SAVED_BRANCH && git stash pop (on PR branch) when ready."
elif [ -n "$SAVED_BRANCH" ]; then
    git switch "$SAVED_BRANCH" 2>/dev/null && echo "→ Restored to $SAVED_BRANCH"  # timeout: 5000
fi
```

**Worktree exit** — if `WT_ENABLED=true` and a worktree was entered at Step 4: commits are already pushed to the fork (the deliverable is remote). Follow `worktree-isolation.md` §Exit — `git branch --show-current`, then `ExitWorktree(action="keep")` to return the session to the main tree, and append the `Worktree` block noting the local worktree is disposable (`git worktree remove` when done). The `SAVED_BRANCH` restore above was a no-op — the main tree was never switched. Never auto-merge.

<!-- branch: main-path — post-pr (call 4 of 4 normal / 5 of 5 with codex-cap) -->

```text
TaskUpdate(task_id=TASK_CLOSE, status="completed")
```

Invoke `AskUserQuestion` — options: (a) Open PR in browser (`gh pr view <PR_NUMBER> --web`) · (b) Skip.

```bash
rm -f .temp/state/skill-contract.md  # skill complete (compaction-contract.md §Lifecycle)  # timeout: 5000
```

## Step 12: Comment dispatch + Codex review loop

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _OSS_RESOLVE < "${TMPDIR:-/tmp}/resolve-oss-resolve-${CSID}" 2>/dev/null || _OSS_RESOLVE=""  # reload (Check 41)
cat "$_OSS_RESOLVE/modes/comment-dispatch.md"  # timeout: 5000
```

Execute its steps (loaded above).

```bash
rm -f .temp/state/skill-contract.md  # skill complete (compaction-contract.md §Lifecycle)  # timeout: 5000
```

</workflow>

<calibration>

Non-calibratable — `disable-model-invocation: true` means skill dispatches to sub-agents rather than running model pass directly; calibrate cannot score model output for skill that produces none.

</calibration>

<notes>

- **Pre-flight git fetch** — Step 1 always runs `git fetch origin` (unconditional) so all remote tracking refs — including `origin/$BASE_REF` — current before Step 5 merges. Then pulls current branch if upstream tracking ref exists and remote ahead. `git pull` conflicts → exit with message to resolve manually — prevents `git merge --continue` with no in-progress merge
- **Branch safety** — `gh pr checkout <PR#>` always lands on PR's HEAD, never `main`/`master`. Never push to default branch — if PR branch = default branch, abort, surface.
- **Same-repo branch rule** — for non-fork PRs (`isCrossRepository=false`), local branch name MUST equal `headRefName` at all times. Never create `pr<N>` alias or other branch name substitute. Enforced by `--branch "$PR_HEAD_REF"` at checkout + hard assertion post-checkout. Rationale: `git push HEAD:$HEAD_REF` on `pr<N>` alias creates new remote branch instead of pushing to PR head — silent data-loss class bug.
- **OSS fork support** — `gh pr checkout <PR#>` works same for branches + forks; forks get contributor remote + tracking; plain `git push` targets fork branch automatically.
- **Merge direction** — `origin/BASE_REF` INTO `HEAD_REF` (not reverse); PR branch = source of truth; maintainer still clicks Merge.
- **Contribution motivation before code** — "whose intent wins" lens; PR body + linked issues reveal constraints invisible in diff.
- **`[question]` items** — answer inline in resolve report only; reclassify before implementing; never silently implement unanswered question.
- **Push verification** — confirm via `gh pr view --json commits`; exit 0 from `git push` necessary but not sufficient (branch protection can silently reject).
- **Merge-push sequencing + escape hatch** — not atomic; concurrent push → non-fast-forward rejection; retry push only (don't re-run full merge). `git merge --abort` = undo conflict state; `git push --force-with-lease` on explicit user request only.
- **Impl agent health + effort**: bridge implementation calls use `bridge:implement`; effort is never `low`, minimum `medium`, typo/doc `medium`, multi-file/new-feature `xhigh`, default `high`. `--agent foundry:*` stays foreground only.
- **Two-phase challenge**: evidence = problem exists?; suggestion = fix quality?; evidence reject → skip; suggestion reject → self-resolved via `alternative` field; all in `CHALLENGE_LOG` + Step 11 report.
- **COMMIT_MODE**: `each` (default); `all`; `stage` (⚠ branch restore skipped); `grouped` (falls back to `each` when labels skipped). Set via the commit-mode menu (Step 3d) — last question of the merged call when ≤6 pending items, separate follow-up call when >6 — skipped/discarded only when the bulk action = (d) skip-all. Distinct MENU from the bulk action (item scope vs commit strategy); item scope never implies commit mode; menus may share a call, never options.
- **AskUserQuestion usage**: calls spread across independent branch-paths — no single sequential path exceeds 4-call limit (worst case: codex-cap adds one call when N>8 items and codex available). Compliant with sequential-call limit.
- **`--agent <name>`**: bare name auto-prefixed `foundry:`; must be an implementation agent (not curator); omit the bridge trailer when another agent is selected.
- **Thread resolution via GraphQL** — `isResolved` on `PullRequestReviewThread` (GraphQL only); REST doesn't expose it. `RESOLVED_THREAD_IDS` = root comment `databaseId`; GraphQL failure → `[]`.
- **Discussion vs inline**: `gh pr view --comments` = discussion (`location: discussion`; no Resolve button); `gh api .../pulls/<N>/comments` = inline (`location: inline`; resolvable). `location: discussion` + `[report]` items: implement-only, no GitHub close action. Surface unresolvable rows through the Status suffix `· thread (no GH resolve)`, not a separate column.
- **Commit attribution** — `[gh]`: `[resolve No.<id>] <reviewer> (gh):`; `[report]`: `[resolve No.<id>] /review finding by <agent> (report: <path>):`.
- **Reference scenarios**: Mode: bare PR# → pr; `42 report` → pr+report; `report` → report mode; bare comment → comment dispatch. Classification: LGTM/emoji → `[info]`; `nit:` → `[gh][suggest]`; resolved thread → `[done]`; "must fix" from write-access reviewer → `[gh][req]`. Challenge: present bug → VALID; already addressed → REJECT; better alternative → REJECT with alternative.
- Follow-up chains:
  - After push → maintainer reviews, clicks Merge; never approve/comment on PR.
  - Unanswered `[question]` → resolve report only; do NOT post to PR.
  - After merge → `Closes #N`/`Fixes #N` in body auto-closes linked issues; absent keywords → surface gap under `### Closing Keywords` note; don't edit PR body.

</notes>
