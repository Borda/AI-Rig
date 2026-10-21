---
name: analyse
description: |
  Analyze GitHub issues, Pull Requests (PRs), Discussions, and repo vitality for an Open Source Software (OSS) project. For any specific item, casts a wide net — finds and lists all related open and closed issues/PRs/discussions, explicitly flags duplicates. Summarizes long threads, extracts reproduction steps, and generates repo vitality stats. Uses gh Command Line Interface (CLI) for GitHub Application Programming Interface (API) access. Complements oss:shepherd (requires `oss` plugin). NOT for PR readiness assessment or code review (use oss:review).
  TRIGGER when: user provides GitHub issue number (#N), PR number, or github.com URL with issue/PR/discussion path AND asks to analyze, summarize, understand, or triage it; user asks for repo vitality stats or "is this repo healthy".
  SKIP: user already pasted full thread text inline; oss:resolve already active on same PR; user wants code review (use oss:review); user phrasing is "review PR" meaning code quality assessment, not thread triage (route to oss:review).
argument-hint: <N|vitality [<owner>/<repo>|github-url]|ecosystem|path/to/report.md> [--reply] [--quick] [--keep "<items>"]
allowed-tools: Read, Bash, Write, Edit, Agent, Skill, AskUserQuestion, TaskList, TaskCreate, TaskUpdate
model: sonnet
effort: high
---

<objective>

Analyze GitHub threads + repo vitality. Help maintainers triage, respond, decide fast. Output actionable + structured — not just summaries.

NOT for implementing PR action items (use oss:resolve). NOT for **code-quality assessment on a PR** — phrasing like "review PR #N" or "does this PR look good?" routes here via TRIGGER (PR number + "analyze/summarize" verbs) but yields thread analysis, not code review. When request is code quality, route to `oss:review` (requires `oss` plugin) instead. NOT for multi-agent code review (use oss:review). NOT for CI pipeline diagnosis (use oss:cicd-steward (requires `oss` plugin)).

</objective>

<inputs>

- **$ARGUMENTS**: one of:
  - `N` (number, plain `123` or `#123`) — any GitHub thread: issue, PR, or discussion; auto-detects type
  - `vitality [<owner>/<repo> | <github-url>]` — repo vitality overview with 9-axis health scorecard, duplicate detection. Optional repo argument accepts `owner/repo` shorthand or full `https://github.com/owner/repo` URL. Omitted → auto-detected from git upstream. Non-GitHub remotes (GitLab, Bitbucket, etc.) stop with warning.
  - `ecosystem` — downstream consumer impact analysis for library maintainers
  - `--reply` — only valid with `N`; spawns shepherd to draft contributor-facing reply after thread analysis. Silently ignored for `vitality` and `ecosystem`.
  - `--quick` — only meaningful with `vitality`; fast daily-scorecard path skipping Codex independent review (Step 5) and mandatory adversarial rework loop (Step 6), reduces spawns to core 4 (gh-scraper + 3 axis scorers). Full mode (all quality passes) stays default. Silently ignored for `N`, `ecosystem`, report-path modes.
  - `path/to/report.md` — path to existing report file; only valid combined with `--reply`; skips all analysis, spawns shepherd directly using provided file

</inputs>

<constants>

> Agent health monitoring (CLAUDE.md §6) — applies to every spawn (shepherd, reproduction agent, gh-scraper, repo-warden, adversarial reviewers). Spawns are background; end the turn after each and resume on the completion notification. The constant is a per-agent deadline checked at wake-ups by `agent_watch.py` — not a poll cadence, nothing sleeps.

```text
AGENT_DEADLINE_S=1800  # per spawned agent, from its spawn; covers the former 900 s silence cutoff + 300 s extension with margin
```

</constants>

<compaction>

> loads: compaction-contract.md

- Key boundary: end of Step 5 — gather/fetch complete, before Step 6 synthesis gate.
- Preserve: cache-dir (.cache/gh), target # (CLEAN_ARGS), synthesized report path, reply-mode flag.

</compaction>

<workflow>

<!-- Agent resolution: see _OSS_SHARED/agent-resolution.md -->

<!-- ARCH.md beside this file diagrams the runs, gates and parallel fan-out. Documentation only, never loaded — update it in the same commit as any change to step order, gate placement, or agent fan-out. -->

## Agent Resolution

Resolved in Step 1's setup block — the `--reply` hard-fail branch needs `REPLY_MODE` parsed first.

> loads: oss-shared-resolver.md

## Step 1: Flag parsing

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# flags: --reply, --quick (vitality fast-path: skips codex review + adversarial rework; ignored for N/ecosystem), --keep
# shared flag/--keep parser (C5; also resolve/review SKILL.md); --keep-state: no eval, writes analyse-{reply,quick,clean-args,keep-items}-${CSID} (compaction-contract.md §keep: semantics), owner-safe clear of a crashed run's stale contract (§Lifecycle)
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/parse-skill-flags.py" --flags reply,quick --keep-state analyse "$ARGUMENTS" >/dev/null  # timeout: 5000
IFS= read -r REPLY_MODE < "${TMPDIR:-/tmp}/analyse-reply-${CSID}" 2>/dev/null || REPLY_MODE=false
IFS= read -r QUICK_MODE < "${TMPDIR:-/tmp}/analyse-quick-${CSID}" 2>/dev/null || QUICK_MODE=false
IFS= read -r CLEAN_ARGS < "${TMPDIR:-/tmp}/analyse-clean-args-${CSID}" 2>/dev/null || CLEAN_ARGS=""
# loads: compaction-contract.md
# cold-start fallback
_OSS_SHARED=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/resolve_shared_path.py" oss skills/_shared 2>/dev/null)  # timeout: 5000
# empty _OSS_SHARED → resolve_shared_path.py failed (no python/script/oss plugin) — downstream `[ -f ... ]` silently expands, Step7 fails after full analysis
# --reply: hard fail; non-reply: degrade gracefully
if [ -z "$_OSS_SHARED" ]; then
    if [ "$REPLY_MODE" = "true" ]; then
        echo "! BLOCKED — could not resolve _OSS_SHARED (oss plugin missing, python unavailable, or resolve_shared_path.py absent); --reply mode requires it"
        exit 1
    else
        echo "⚠ _OSS_SHARED empty — oss plugin shared dir unresolved; continuing with degraded functionality (--reply will fail in this run)"
    fi
fi
# persist $_OSS_SHARED (Check 41)
# loads: terminal-summaries.md (ships in this plugin's _shared); consumed by modes/thread.md, modes/vitality.md, modes/ecosystem.md
echo "${_OSS_SHARED:-}" > "${TMPDIR:-/tmp}/analyse-oss-shared-${CSID}"
# cold-resolve skill dir once; thread.md/vitality.md warm-read this sentinel, skip re-globbing (Check 41)
_OSS_ANALYSE=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/resolve_shared_path.py" oss skills/analyse 2>/dev/null)  # timeout: 5000
[ -z "$_OSS_ANALYSE" ] && _OSS_ANALYSE="plugins/cc_oss/skills/analyse"
echo "$_OSS_ANALYSE" > "${TMPDIR:-/tmp}/analyse-oss-analyse-${CSID}"
# persist REPLY_MODE/QUICK_MODE/CLEAN_ARGS (Check 41); CLEAN_ARGS stored with leading '#' stripped
CLEAN_ARGS="${CLEAN_ARGS#\#}"
echo "$REPLY_MODE" > "${TMPDIR:-/tmp}/analyse-reply-mode-${CSID}"
echo "$QUICK_MODE" > "${TMPDIR:-/tmp}/analyse-quick-mode-${CSID}"
echo "$CLEAN_ARGS" > "${TMPDIR:-/tmp}/analyse-clean-args-${CSID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/parse_analyse_args.py" --mode classify --args "$CLEAN_ARGS"  # timeout: 5000
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/parse_analyse_args.py" --args "$CLEAN_ARGS"  # timeout: 15000
```

`REPLY_MODE` only meaningful when `$CLEAN_ARGS` is number — silently ignored for `vitality` and `ecosystem`.

`DIRECT_PATH_MODE=true` only valid when `REPLY_MODE=true` — if combined without `--reply`, Step 2 prints plain-text error and stops; execution never reaches Step 5 mode dispatch.

**Unsupported flag check** — after all supported flags extracted, scan `$ARGUMENTS` for any remaining `--<token>` tokens. If found: invoke `AskUserQuestion` with:

- question: "Unknown flag(s): `--<token>`. Supported: `--reply`, `--quick`, `--keep`. How to proceed?"
- (a) Abort — re-invoke with correct flags
- (b) Continue ignoring unknown flags

## Step 2: Reply-mode fast-path (only when `REPLY_MODE=true`)

Skip when `REPLY_MODE=false` and `DIRECT_PATH_MODE=false`.

**Direct report path** (`DIRECT_PATH_MODE=true` — checked first). The error branches below execute as explicit bash `exit 1` blocks — `stop` is not prose advice; the workflow must terminate hard before any downstream step can fire a misleading `Item .md not found on GitHub` error:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# reload vars (Check 41)
IFS= read -r REPLY_MODE < "${TMPDIR:-/tmp}/analyse-reply-mode-${CSID}" 2>/dev/null || REPLY_MODE="false"
IFS= read -r DIRECT_PATH_MODE < "${TMPDIR:-/tmp}/analyse-direct-path-mode-${CSID}" 2>/dev/null || DIRECT_PATH_MODE="false"
IFS= read -r REPORT_FILE < "${TMPDIR:-/tmp}/analyse-report-file-${CSID}" 2>/dev/null || REPORT_FILE=""
# both aborts drop the sentinel — leftover path to a never-written report would make enforce-analyse-header.js deny the Step6a follow-up question
if [ "$DIRECT_PATH_MODE" = "true" ] && [ "$REPLY_MODE" = "false" ]; then
    echo "! Error: report path '$REPORT_FILE' passed without --reply."
    echo "  Re-run as: /oss:analyse $REPORT_FILE --reply"
    echo "  Or use:    /oss:analyse <N> | vitality | ecosystem"
    rm -f "${TMPDIR:-/tmp}/analyse-report-file-${CSID}"
    exit 1
fi
if [ "$DIRECT_PATH_MODE" = "true" ] && [ "$REPLY_MODE" = "true" ] && [ ! -f "$REPORT_FILE" ]; then
    echo "! Error: report not found at $REPORT_FILE"
    rm -f "${TMPDIR:-/tmp}/analyse-report-file-${CSID}"
    exit 1
fi
if [ "$DIRECT_PATH_MODE" = "true" ] && [ "$REPLY_MODE" = "true" ] && [ -f "$REPORT_FILE" ]; then
    echo "[direct] using $REPORT_FILE"
    # Skip to Step 7 — orchestrator branches on DIRECT_PATH_MODE=true && REPLY_MODE=true
fi
```

After the block above: `DIRECT_PATH_MODE=true && REPLY_MODE=true && file exists` → skip to Step 7 (don't run auto-detection fast-path below).

Remaining fast-path logic (TODAY, REPORT_FILE auto-construction, drift check) only runs when `DIRECT_PATH_MODE=false`.

When `REPLY_MODE=true`, check if fresh report already exists before any API calls:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# reload vars (Check 41)
IFS= read -r CLEAN_ARGS < "${TMPDIR:-/tmp}/analyse-clean-args-${CSID}" 2>/dev/null || CLEAN_ARGS=""
IFS= read -r TODAY < "${TMPDIR:-/tmp}/analyse-today-${CSID}" 2>/dev/null || TODAY=$(date +%Y-%m-%d)
# Numeric mode only — vitality/ecosystem set REPORT_FILE in their mode files; DIRECT_PATH_MODE already set above.
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/build_analyse_paths.py" --clean-args "$CLEAN_ARGS" --today "$TODAY"  # timeout: 10000
```

- `FAST_PATH_TENTATIVE=true` → continue to Steps 3–4 for type detection and type-aware drift check. If no new activity confirmed: `FAST_PATH=true` → print `[resume] reusing existing report for #$CLEAN_ARGS` → jump to Step 7.
- `FAST_PATH_TENTATIVE=false` (report missing) → continue to Step 3.

## Step 3: Cache layer (numeric arguments only)

Check local cache before API calls — prevents redundant fetches, avoids GitHub rate limits when re-analysing same item same day.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# reload vars (Check 41)
IFS= read -r CLEAN_ARGS < "${TMPDIR:-/tmp}/analyse-clean-args-${CSID}" 2>/dev/null || CLEAN_ARGS=""
IFS= read -r TODAY < "${TMPDIR:-/tmp}/analyse-today-${CSID}" 2>/dev/null || TODAY=$(date +%Y-%m-%d)
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/build_analyse_paths.py" --mode cache --clean-args "$CLEAN_ARGS" --today "$TODAY"  # timeout: 10000
```

**Cache hit** — if `$CACHE_FILE` exists:

- Read `type`, `item`, `comments` fields from JSON; `TYPE` known
- Skip all primary `gh` item fetches in `modes/thread.md` — **except** PR mode's reviews/inline-comments fetch (`github-review-parsing.md` rule 1): review rounds change on every push, so that fetch stays live even on a cache hit
- Print `[cache] #$CLEAN_ARGS ($TODAY)` as one-line status note
- Still run wide-net searches (dynamic — never cached)
- `FAST_PATH_TENTATIVE=true`: run lightweight drift check now that `TYPE` known, then skip Step 4 type-detection API calls:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# reload drift state (Check 41)
IFS= read -r CLEAN_ARGS < "${TMPDIR:-/tmp}/analyse-clean-args-${CSID}" 2>/dev/null || CLEAN_ARGS=""
IFS= read -r DRIFT < "${TMPDIR:-/tmp}/analyse-drift-${CSID}" 2>/dev/null || DRIFT="false"
IFS= read -r FAST_PATH <  "${TMPDIR:-/tmp}/analyse-fast-path-${CSID}" 2>/dev/null || FAST_PATH="false"
IFS= read -r FAST_PATH_TENTATIVE < "${TMPDIR:-/tmp}/analyse-fast-path-tentative-${CSID}" 2>/dev/null || FAST_PATH_TENTATIVE="false"
IFS= read -r REPORT_MTIME < "${TMPDIR:-/tmp}/analyse-report-mtime-${CSID}" 2>/dev/null || REPORT_MTIME="0"
IFS= read -r CACHE_FILE < "${TMPDIR:-/tmp}/analyse-cache-file-${CSID}" 2>/dev/null || CACHE_FILE=""
TYPE=$(jq -r '.type // empty' "$CACHE_FILE" 2>/dev/null)
# Corrupt cache guard — validate TYPE before use
[ "$TYPE" = "pr" ] || [ "$TYPE" = "issue" ] || [ "$TYPE" = "discussion" ] || { echo "! Error: corrupt cache — invalid type \"$TYPE\" for #$CLEAN_ARGS; delete .cache/gh/ to reset"; exit 1; }
# Cache hit + FAST_PATH_TENTATIVE: lightweight updatedAt call; UPDATED_TS > REPORT_MTIME → DRIFT=true
if [ "$TYPE" = "discussion" ]; then
    UPDATED_AT=$(gh api graphql \
        -f query='query($owner:String!,$repo:String!,$number:Int!){repository(owner:$owner,name:$repo){discussion(number:$number){updatedAt}}}' \
        -F owner='{owner}' -F repo='{repo}' -F number=$CLEAN_ARGS \
        --jq '.data.repository.discussion.updatedAt' 2>/dev/null)  # timeout: 6000
else
    UPDATED_AT=$(gh api "repos/{owner}/{repo}/issues/$CLEAN_ARGS" --jq '.updated_at' 2>/dev/null)  # timeout: 6000
fi
UPDATED_TS=$(date -d "$UPDATED_AT" +%s 2>/dev/null || date -j -f "%Y-%m-%dT%H:%M:%SZ" "$UPDATED_AT" +%s 2>/dev/null)  # timeout: 5000
# Date parse failure → treat as drifted (conservative)
[ -z "$UPDATED_TS" ] && DRIFT=true
[ "$UPDATED_TS" -gt "$REPORT_MTIME" ] && DRIFT=true
[ "$DRIFT" = "false" ] && FAST_PATH=true && echo "[resume] reusing existing report for #$CLEAN_ARGS"
# persist for Step 4/7 and the mode file (Check 41); oss-detect-type is Step 4's TYPE sentinel
printf '%s\n' "$TYPE" > "${TMPDIR:-/tmp}/oss-detect-type-${CSID}"
printf '%s\n' "$DRIFT" > "${TMPDIR:-/tmp}/analyse-drift-${CSID}"
printf '%s\n' "$FAST_PATH" > "${TMPDIR:-/tmp}/analyse-fast-path-${CSID}"
```

`FAST_PATH=true` → skip to Step 7. `DRIFT=true` → continue (full re-analysis from cached data).

**Cache miss** — after fetching in `modes/thread.md`, save the primary fetch's `--json` output (the item) with the Write tool to `${TMPDIR:-/tmp}/analyse-item-${CSID}` and the `--comments` text to `${TMPDIR:-/tmp}/analyse-comments-${CSID}` (the fetch results are tool output, not shell variables), then write:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# reload vars (Check 41)
IFS= read -r CLEAN_ARGS < "${TMPDIR:-/tmp}/analyse-clean-args-${CSID}" 2>/dev/null || CLEAN_ARGS=""
IFS= read -r CACHE_FILE < "${TMPDIR:-/tmp}/analyse-cache-file-${CSID}" 2>/dev/null || CACHE_FILE=""
IFS= read -r TYPE < "${TMPDIR:-/tmp}/oss-detect-type-${CSID}" 2>/dev/null || TYPE=""
ITEM_FILE="${TMPDIR:-/tmp}/analyse-item-${CSID}"; COMMENTS_FILE="${TMPDIR:-/tmp}/analyse-comments-${CSID}"
# temp + mv — a failed jq must not leave a truncated cache that later passes as a hit
[ -s "$ITEM_FILE" ] && [ -f "$COMMENTS_FILE" ] && [ -n "$CACHE_FILE" ] && [ -n "$TYPE" ] && jq -n \
    --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --arg type "$TYPE" \
    --argjson number "$CLEAN_ARGS" \
    --slurpfile item "$ITEM_FILE" \
    --rawfile comments "$COMMENTS_FILE" \
    '{"ts":$ts,"type":$type,"number":$number,"item":$item[0],"comments":$comments}' \
    >"$CACHE_FILE.tmp" && mv "$CACHE_FILE.tmp" "$CACHE_FILE" || echo "⚠ cache write skipped — empty or malformed API response" # timeout: 5000
```

**Stale cache** — file for same number but earlier date ignored. Old files left — small, provide audit history.

> **mtime reliability caveat**: `stat` mtime unreliable after `rsync`/copy, in CI with frozen clocks, or on HFS+ (1-second granularity). If drift check produces unexpected fast-path hits, verify report mtime with `stat "$REPORT_FILE"`. Workaround: delete cached report to force full re-analysis.

Cache applies to: issue/PR/discussion primary fetch and comments. Cache does NOT apply to: `gh issue list`, `gh pr list`, `gh pr checks`, `gh pr diff`, discussion list queries, vitality/ecosystem modes.

## Step 4: Auto-Detection (numeric arguments only)

Issues, PRs, discussions share unified running index — given number is exactly one type. Cache hit: read `TYPE` and `ITEM` from `$CACHE_FILE` — skip `gh` calls below.

Cache miss:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# reload drift state (Check 41)
IFS= read -r DRIFT < "${TMPDIR:-/tmp}/analyse-drift-${CSID}" 2>/dev/null || DRIFT="false"
IFS= read -r FAST_PATH_TENTATIVE < "${TMPDIR:-/tmp}/analyse-fast-path-tentative-${CSID}" 2>/dev/null || FAST_PATH_TENTATIVE="false"
IFS= read -r REPORT_MTIME < "${TMPDIR:-/tmp}/analyse-report-mtime-${CSID}" 2>/dev/null || REPORT_MTIME="0"
IFS= read -r CLEAN_ARGS < "${TMPDIR:-/tmp}/analyse-clean-args-${CSID}" 2>/dev/null || CLEAN_ARGS=""
# One call covers all types; writes TYPE/UPDATED_AT/DRIFT to ${TMPDIR:-/tmp}/oss-detect-<var>-${CSID}
if [ "$FAST_PATH_TENTATIVE" = "true" ]; then
    python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/detect_thread_type.py" --number "$CLEAN_ARGS" --report-mtime "$REPORT_MTIME" 2>/dev/null  # timeout: 15000
else
    python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/detect_thread_type.py" --number "$CLEAN_ARGS" 2>/dev/null  # timeout: 15000
fi
IFS= read -r TYPE < "${TMPDIR:-/tmp}/oss-detect-type-${CSID}" 2>/dev/null || TYPE="unknown"
IFS= read -r DRIFT < "${TMPDIR:-/tmp}/oss-detect-drift-${CSID}" 2>/dev/null || DRIFT="false"
if [ "$FAST_PATH_TENTATIVE" = "true" ] && [ "$TYPE" != "unknown" ] && [ "$DRIFT" = "false" ]; then
    FAST_PATH=true
    echo "[resume] reusing existing report for #$CLEAN_ARGS"
fi
# TYPE=unknown: stop — don't fall through to Step 5
if [ "$TYPE" = "unknown" ]; then
    echo "Item #$CLEAN_ARGS not found on GitHub. Re-run with a different number, or use \`/oss:analyse vitality\` for repo overview."
    exit 1
fi
```

## Step 5: Mode dispatch

Read and execute the mode file from `${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/skills/analyse/modes/`.

| Argument | Mode file |
| -- | -- |
| number (any type) | `modes/thread.md` |
| `vitality` | `modes/vitality.md` |
| `ecosystem` | `modes/ecosystem.md` |

> loads: vitality-report.md (used by modes/vitality.md as REPORT_TPL)

Run the boundary block below after the mode file's gather/fetch finishes, before synthesis — not before dispatch:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# boundary: post-gather/fetch (Step5), pre-synthesis gate (compaction-contract.md §Lifecycle)
IFS= read -r _CLEAN_ARGS < "${TMPDIR:-/tmp}/analyse-clean-args-${CSID}" 2>/dev/null || _CLEAN_ARGS=""
IFS= read -r _REPORT_FILE < "${TMPDIR:-/tmp}/analyse-report-file-${CSID}" 2>/dev/null || _REPORT_FILE="pending"
IFS= read -r _REPLY_MODE < "${TMPDIR:-/tmp}/analyse-reply-mode-${CSID}" 2>/dev/null || _REPLY_MODE="false"
_PRESERVE="target=#${_CLEAN_ARGS}, cache-dir=.cache/gh, report=${_REPORT_FILE}, reply-mode=${_REPLY_MODE}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/write_skill_contract.py" --keep-sentinel analyse-keep-items "oss:analyse" "synthesis (after gather/fetch)" ".cache/gh" "${_PRESERVE}" "reply gate (Step 6) or shepherd reply (Step 7)"  # timeout: 5000
```

## Step 6: Reply gate — STOP CHECK

**Run before Confidence block regardless of `--reply` mode.**

`REPLY_MODE=true`: response incomplete until Step 7 done and reply file written. Proceed to Step 7 — `## Confidence` block goes at end of Step 7 instead.

`REPLY_MODE=false` — do NOT proceed to Step 7. Execute both sub-steps below, then end response.

### 6a — Follow-up gate

**Hook-enforced**: on `Stop`, `hooks/enforce-analyse-header.js` keeps a turn going once per report when its final reply lacks the matching full header table — skipping the follow-up question does not skip delivery. At question time it blocks only this workflow's follow-up until the current report exists and the first question's text names the report path and its `Title`, `Outcome` values as a short plain summary, no table. Option descriptions and previews do not supply the summary. Missing evidence blocks this transition; apply the placement the deny reason names, then retry. Diagnostic/recovery questions remain available; use their own question header, not `oss-analyse`. The existing sentinel lifetime still scopes this workflow guard; it does not prove UI rendering or report correctness.

**Delivery — once**: build the mode's delivery block from the current report (all `---` header fields as a `Field | Value` table, then its compact summary and `→ saved to <path>`). Print it once in the final reply after the answer, after its last tool call, never as Bash/tool stdout. Do not print the header table before the follow-up question or put it in option previews. The full table stays in the report file; the question carries only the plain summary below. `REPLY_MODE=true` (no 6a question): the block opens the final reply after Step 7's last tool call. The `Stop` hook requires the matching full header table there; the question summary does not satisfy final delivery.

> **Output-Routing exemption**: the 6a question carries only a plain summary; quality-gates.md §Report File Format's table-first-in-reply rule and Output Routing step 2a apply to the final reply after the answer and to the `REPLY_MODE=true` path.

Invoke `AskUserQuestion`. In both menus, the question text carries at most 6 plain lines (target 600 characters, hard cap 1,500), no table, pipe layout or code fence. Options carry a label and a one-line description only, no `preview`:

**Thread mode** (`$CLEAN_ARGS` is a number; `TYPE` from `oss-detect-type-${CSID}`):

- header: `oss-analyse`
- question: `Title: <report Title>`; `Outcome: <report Outcome>`; finding counts and blockers where present; `→ <REPORT_FILE>`; `What next?`; "Long wait? `/compact` now — report saved, resume lossless." Use short plain lines, not a table.
- `TYPE=issue` or `discussion`:
  - (a) label: `/develop:fix` — description: diagnose and fix the reported issue (requires `develop` plugin)
  - (b) label: `/develop:feature` — description: implement as new feature (requires `develop` plugin)
  - (c) label: `draft reply` — description: run `/oss:analyse $CLEAN_ARGS --reply` to shepherd a contributor-facing reply
  - (d) label: `skip` — description: no action
- `TYPE=pr` (PR thread — the follow-ups are review and resolve, not new implementation):
  - (a) label: `/oss:review $CLEAN_ARGS` — description: full multi-agent code review of this PR (requires `oss` plugin)
  - (b) label: `/oss:resolve $CLEAN_ARGS` — description: apply the PR's review action items (requires `oss` plugin)
  - (c) label: `draft reply` — description: run `/oss:analyse $CLEAN_ARGS --reply` to shepherd a contributor-facing reply
  - (d) label: `skip` — description: no action

**Vitality / ecosystem mode** (`$CLEAN_ARGS` is `vitality` or `ecosystem`):

- header: `oss-analyse`
- question: `Title: <report Title>`; `Outcome: <report Outcome>`; finding counts and blockers where present; `→ <REPORT_FILE>`; `What next?`; "Long wait? `/compact` now — report saved, resume lossless." Use short plain lines, not a table.
- `<N>` below = the highest-priority thread or PR number named in the report, substituted before asking; the report names none → omit options (a) and (b)
- (a) label: `/oss:analyse <N> --reply` — description: draft reply for that thread
- (b) label: `/oss:review <N>` — description: full code review of that PR (requires `oss` plugin; only when `<N>` is a PR)
- (c) label: `skip` — description: no action

### 6b — Confidence block (REPLY_MODE=false only)

End response with `## Confidence` block per CLAUDE.md output standards.

```bash
rm -f .temp/state/skill-contract.md  # skill complete (compaction-contract.md §Lifecycle)  # timeout: 5000
```

## Step 7: Draft contributor reply (only when --reply, thread mode only)

Report at `$REPORT_FILE` guaranteed to exist — either reused via fast-path (Step 2, `FAST_PATH=true`) or freshly written by Step 5.

One block reloads the vars, loads `shepherd-reply-protocol.md` (apply its invocation pattern and terminal summary format) and creates the watch directory; echo-backs give the spawn its runtime values. The spawn response then writes `<WATCH_DIR>/agent-watch-reply.tsv` with the Write tool (health monitoring below):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# reload vars (Check 41)
IFS= read -r _OSS_SHARED < "${TMPDIR:-/tmp}/analyse-oss-shared-${CSID}" 2>/dev/null || _OSS_SHARED=""
IFS= read -r CLEAN_ARGS < "${TMPDIR:-/tmp}/analyse-clean-args-${CSID}" 2>/dev/null || CLEAN_ARGS=""
IFS= read -r TODAY < "${TMPDIR:-/tmp}/analyse-today-${CSID}" 2>/dev/null || TODAY=$(date +%Y-%m-%d)
cat "$_OSS_SHARED/shepherd-reply-protocol.md"  # timeout: 5000
WATCH_DIR="${TMPDIR:-/tmp}/oss-analyse-watch-${CSID}"
mkdir -p "$WATCH_DIR"  # timeout: 5000
echo "_OSS_SHARED=$_OSS_SHARED CLEAN_ARGS=$CLEAN_ARGS TODAY=$TODAY"
echo "WATCH_DIR=$WATCH_DIR"
```

```text
Agent(
  subagent_type="oss:shepherd",
  name="reply-shepherd",
  description="Draft contributor reply for thread #<CLEAN_ARGS>",
  prompt="Draft contributor reply for thread #<CLEAN_ARGS>: read <_OSS_SHARED>/shepherd-reply-protocol.md and follow its invocation pattern. Report: <REPORT_FILE>. Fetch thread context via `gh issue view <CLEAN_ARGS> --comments` (or GraphQL for discussions) if not already in report. Write full reply draft to .reports/analyse/thread/output-reply-thread-<CLEAN_ARGS>-<TODAY>.md using the Write tool. Return ONLY: {\"status\":\"done\",\"file\":\"<OUTPUT_PATH>\",\"confidence\":0.N}"
)
```

**Replace `<_OSS_SHARED>`, `<REPORT_FILE>`, `<CLEAN_ARGS>`, `<TODAY>` with actual runtime values before spawning — agents receive text, not shell variables.**

Verify output file exists and is non-empty after spawn: `[ -s "<OUTPUT_PATH>" ] || { echo "⚠ shepherd output empty or missing"; }`

If `DRIFT=true`: append `[analysis refreshed — new activity since last report]` to terminal summary.

**Health monitoring** (CLAUDE.md §6) — every spawn in this skill and its modes: agent spawns run in the background — spawn, end the turn, resume on the completion notification (shared rule: `rules/task-lifecycle.md` §After spawning); no filler call, no "waiting" line, no sleep, and never `ScheduleWakeup`, `ListAgents` or a `Monitor` loop. Create the watch directory before the first spawn (block above for the reply; mode files do the same for theirs) and in each spawn response write `<WATCH_DIR>/agent-watch-<batch>.tsv` with the Write tool — one row per agent, `<name><TAB><expected output path, or - for an envelope-only agent><TAB>1800`. At every wake-up run the block below once before reading agent output: `done` → consume; `timed_out`, or a notification that arrived without the deliverable → read `tail -100` of the expected path; if none, use `{"verdict":"timed_out"}`; surface with ⏱ now. Never silently omit, and a ⏱ never skips a user question.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/agent_watch.py" --state-dir "${TMPDIR:-/tmp}/oss-analyse-watch-${CSID}"  # timeout: 5000
```

End response with `## Confidence` block per CLAUDE.md — always **absolute last thing**.

```bash
rm -f .temp/state/skill-contract.md  # skill complete (compaction-contract.md §Lifecycle)  # timeout: 5000
```

</workflow>

<calibration>

Calibratable modes: thread (duplicate detection recall), vitality (repo vitality metrics accuracy), ecosystem (impact analysis accuracy).

Scenarios:

1. Thread — duplicate detection: synthetic issue with identical symptoms to existing closed issue → root cause match ≥0.9; duplicate link surfaced
2. Thread — actionable response quality: feature request with no linked PRs → concrete scope + next step; no vague suggestions
3. Vitality — metric accuracy: repo with known issue/PR/response-time counts → numeric values within ±10% of ground truth; archetype scenario matrix with expected score ranges per repo type: `vitality-calibration.md`

</calibration>

<notes>

- **Thread analysis output schema** (canonical section order, defined in `modes/thread.md`): `## Thread #[number]: [title]`, `### Summary`, `### Thread Verdict`, `### Related Items`, `### Analysis`, `### Suggested Labels`, `### Suggested Response`, `### Priority` (omit for discussions). Use these exact headings — consistent section names enable downstream parsing, diff-based change detection across runs. `## Confidence` is not a report-file heading — it is appended to the chat response per CLAUDE.md output standards (SKILL.md Step 6b/7), always last in the response; omitting it triggers calibration failure (confidence defaults to 0.5, producing large negative bias).
- **Precision guidance**: flag issues, don't solve them; flag blockers, don't design solutions. Reference `/develop:fix` and `/develop:feature` (requires `develop` plugin) for implementation work. Verbose implementation sketches in triage output dilute signal-to-noise ratio. Each flagged item (duplicate, blocker, next step) must carry explicit severity/priority label (high/medium/low) inline — enables downstream triage, satisfies format scoring.
- **Vitality mode repo resolution**: `GH_OWNER` and `GH_REPO` are written to the `analyse-gh-owner`/`analyse-gh-repo` sentinels by Step 1's full-args `parse_analyse_args.py` call (modes/vitality.md Step 1 reloads them) from: (1) explicit URL/owner-repo arg, (2) `gh repo view`, (3) `git remote origin`. vitality.md uses `-R "$GH_OWNER/$GH_REPO"` on all gh commands and literal `$GH_OWNER/$GH_REPO` in all `gh api` paths — never `{owner}/{repo}` template substitution in vitality mode.
- Mode files live in `${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/skills/analyse/modes/` — one file per mode, fully self-contained
- `modes/thread.md` handles all three thread types (issue, PR, discussion) via internal branching
- Always use `gh` CLI — never hardcode repo URLs
- Run `gh auth status` first if commands fail; user may need to authenticate
- For closed items, note resolution so history useful
- Don't post responses without explicit user instruction — draft only
- **Out-of-scope early-exit — hard stop, zero partial review**: when input clearly outside skill's domain (e.g. CI pipeline diagnosis, code review of unrelated source/script content pasted alongside the actual target), print scope note + redirect (e.g. "use oss:cicd-steward (requires `oss` plugin)") and stop immediately — zero bugs, zero code-quality observations, zero line-level findings on the out-of-scope portion, even when it arrives embedded inside otherwise in-scope material (e.g. a script pasted alongside a draft report or rubric-scoring JSON).
  - Flag then stop; flag then analyze = precision cost with no recall benefit. Producing any partial review before the redirect (even framed as "quick notes") is itself the violation — the redirect must be the entire response for that content, not a preamble to it.
  - **Does not apply to content that IS the thread's own subject** — a PR diff (review mode), or an issue's reproduction script that `modes/thread.md`'s Reproduction Check (R1-R4) is built to execute and score — that content stays in scope and gets analyzed normally; the guard targets unrelated material riding along with in-scope input, not the input itself.
- **Inline context**: skill runs in the caller's session, never `context: fork` — a fork runs as a hidden background agent, so its phase tasks are invisible to the caller's `TaskList` and its progress is not shown. `Agent` is declared in `allowed-tools` — do NOT skip the vitality adversarial review (`modes/vitality.md` Steps 5–6) assuming Agent unavailable; those steps are mandatory in full mode.
- **`--reply` drafts only** — shepherd produces draft file; does NOT auto-post to GitHub. User posts manually. Write access to repo not required for `--reply`; required only if user subsequently posts draft via `gh issue comment` or `gh pr comment`.
- Follow-up chains:
  - Issue with confirmed bug → `/develop:fix` to diagnose, reproduce with test, apply targeted fix (requires `develop` plugin)
  - Issue is feature request → `/develop:feature` for TDD-first implementation (requires `develop` plugin)
  - PR with quality concerns → `/oss:review` for comprehensive multi-agent code review (requires `oss` plugin)
  - Draft responses → use `--reply` to auto-draft via shepherd; or invoke shepherd manually

</notes>
