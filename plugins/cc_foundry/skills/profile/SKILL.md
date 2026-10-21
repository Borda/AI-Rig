---
name: profile
description: "Session clock-time AND token/cost analyzer. Reads the foundry plugin's timings.jsonl and invocations.jsonl logs (written by task-log.js) for wall-time, plus Claude Code transcripts (~/.claude/projects/**, main-loop + subagent files) for token usage and USD cost, and merges both into one per-session and per-skill report — local-tool vs subagent-spawn vs Skill vs AskUserQuestion idle vs main-loop reasoning residual time, and main-loop vs subagent spend by model tier. Useful for answering \"why did /oss:resolve run 30 minutes?\", \"what did this session cost?\", or \"which skill burns the most tokens?\". Pure log/transcript read — no instrumentation, no skill edits, no LLM calls. TRIGGER when: user asks where wall-clock time OR tokens/cost go during a skill/session, why a skill is slow or expensive, what dominates total runtime or spend, or wants a per-skill rollup over a recent window; phrases: \"where does time go\", \"why so slow\", \"what did this cost\", \"token spend\", \"why so expensive\", \"profile last session\", \"clock breakdown\", \"session timing\", \"which skill burns tokens\". SKIP: per-line Python perf (use foundry:perf-optimizer); known failure or hang (use /foundry:investigate); a real billing statement (prices are public list rates, not effective plan rates)."
argument-hint: '[--since 24h|7d|30d] [--session-id ID] [--top-n N]'
allowed-tools: Read, Write, Bash, TaskList, TaskCreate, TaskUpdate, AskUserQuestion
effort: low
---

<objective>

Bucket session **clock time** from `~/.claude/logs/{timings,invocations}.jsonl` into:

1. **Local tools** — Bash/Read/Edit/Write/Grep/Glob and other main-process tools
2. **Agent / subagent spawns** — Task/Agent calls (sync + background)
3. **Skill** — `tool=Skill` wall durations
4. **AskUserQuestion idle** — human-wait, separate column, excluded from compute total
5. **Main-loop reasoning (residual)** — session wall minus buckets above

...and bucket session **tokens/cost** from Claude Code transcripts (`~/.claude/projects/**`, main-loop + subagent files) into:

6. **Sessions ranked by cost** (window mode) — main $ vs subagent $ vs total $ per session, plus a per-command rollup; or **one session's deep-dive** (`--session-id`) — cost by main/sidechain × model tier, agent roster, top cache-rebuild calls, cold-start share

Outputs a markdown report at `.reports/profile/<UTC-timestamp>/report.md` plus a `.temp/output-profile-...md` copy: per-session clock table, per-skill clock rollup, top-N longest single calls, plus a `## Tokens & cost` section scoped to same window / `--session-id`.

NOT for: per-line Python perf (use `foundry:perf-optimizer`); known failure diagnosis (use `/foundry:investigate`); a real billing statement (prices are public list rates, not effective plan rates).

</objective>

<inputs>

- **`--since DURATION`** (default `24h`) — window: `NNs|NNm|NNh|NNd`
- **`--session-id ID`** — optional; restrict to one session
- **`--top-n N`** (default `5`) — slowest single calls to list

If $ARGUMENTS empty, default window is 24h.

</inputs>

<workflow>

**Task hygiene** — task tools may be deferred; load before first use: `ToolSearch(query="select:TaskList,TaskCreate,TaskUpdate,TaskGet", max_results=4)`. Call `TaskList` first and triage each task it returns: `completed` if work clearly done, `deleted` if orphaned, keep `in_progress` only if genuinely continuing. Never spend a turn on bookkeeping alone — every `TaskCreate`/`TaskUpdate` ships in the same response as the next substantive tool call; one exception, `TaskUpdate(completed)` immediately before a long output block (`rules/task-lifecycle.md`).

**Task tracking**: TaskCreate two tasks up front — 1 "Run analyzers + render report" (Steps 1–3), 2 "Step 4b: Print report header" (Step 4). Mark each `in_progress` before its first tool call; 1 completed once `report.md` exists, 2 completed right after the header and path are printed, before the executive summary.

## Step 1: Parse args + create run dir

The first part of the Step 2 block below does this step (one Bash call with the clock-time analyzer): argument text is parsed in `bin/` and validated against closed patterns before use, then persisted as `KEY=VALUE` lines to `${TMPDIR:-/tmp}/foundry-profile-state-${CSID}`; Steps 2b–3 read it back key by key, never sourcing it (bash state doesn't persist across Bash calls, and `REPORT_DIR` carries a per-shell timestamp that can't be re-derived).

## Step 2: Run analyzers — clock time, then tokens/cost

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# timeout: 60000
# --flags is mandatory for the helper; profile has no boolean flags. --keep-state: no eval; --inlined: profile keeps no compaction contract, so it clears none
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/parse-skill-flags.py" --flags noop --value-flags since,session-id,top-n --keep-state profile --inlined "$ARGUMENTS" >/dev/null
IFS= read -r SINCE < "${TMPDIR:-/tmp}/profile-since-${CSID}" 2>/dev/null; SINCE="${SINCE:-24h}"
IFS= read -r TOP_N < "${TMPDIR:-/tmp}/profile-top-n-${CSID}" 2>/dev/null; TOP_N="${TOP_N:-5}"
IFS= read -r SESSION_ID < "${TMPDIR:-/tmp}/profile-session-id-${CSID}" 2>/dev/null || SESSION_ID=""
[[ $SINCE =~ ^[0-9]+[smhd]$ && $TOP_N =~ ^[0-9]+$ && $SESSION_ID =~ ^[A-Za-z0-9._-]*$ ]] || { echo "! BLOCKED — invalid --since/--top-n/--session-id"; exit 1; }
STAMP="$(date -u +%Y-%m-%dT%H-%M-%SZ)"
REPORT_DIR=".reports/profile/$STAMP"
mkdir -p "$REPORT_DIR"
{
  echo "REPORT_DIR=$REPORT_DIR"
  echo "SINCE=$SINCE"
  echo "SESSION_ID=$SESSION_ID"
  echo "TOP_N=$TOP_N"
} | tee "${TMPDIR:-/tmp}/foundry-profile-state-${CSID}"
set --; [ -n "$SESSION_ID" ] && set -- --session-id "$SESSION_ID"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/timing_analyzer.py" \
    --since "$SINCE" \
    --top-n "$TOP_N" \
    --output "$REPORT_DIR/report.md" \
    "$@" 2>"$REPORT_DIR/warnings.log"
TIME_EXIT=$?
echo "time_exit=$TIME_EXIT"
[ "$TIME_EXIT" -lt 2 ] || echo "! BLOCKED — timing_analyzer exit $TIME_EXIT; see $REPORT_DIR/warnings.log"
```

`set --` array form passes `--session-id` and its value as separate arguments only when set (shell-portable; an unquoted `"--session-id $SESSION_ID"` string is one argument under zsh). Exit code 1: no sessions in window — surface that, stop; nothing in Step 2b can rescue a missing clock report, since `enforce-profile-header.js` keys on `report.md` specifically. Exit code 2 or higher: analyzer error (bad arguments, crash) — surface the `! BLOCKED` line with the tail of `warnings.log`, stop; do not run Step 2b.

## Step 2b: Run analyzer — tokens/cost (best-effort, appended)

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# timeout: 60000
while IFS== read -r K V; do case "$K" in REPORT_DIR) REPORT_DIR=$V;; SINCE) SINCE=$V;; SESSION_ID) SESSION_ID=$V;; TOP_N) TOP_N=$V;; esac; done < "${TMPDIR:-/tmp}/foundry-profile-state-${CSID}"
set --; [ -n "$SESSION_ID" ] && set -- --session-id "$SESSION_ID"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/cost_analyzer.py" \
    --since "$SINCE" \
    --top-n "$TOP_N" \
    --output "$REPORT_DIR/cost.md" \
    "$@" 2>>"$REPORT_DIR/warnings.log"
COST_EXIT=$?
if [ "$COST_EXIT" -eq 0 ] && [ -s "$REPORT_DIR/report.md" ]; then
  printf '\n' >> "$REPORT_DIR/report.md"
  cat "$REPORT_DIR/cost.md" >> "$REPORT_DIR/report.md"
fi
echo "cost_exit=$COST_EXIT"
echo '{"status":"complete","since":"'"$SINCE"'","session_id":"'"$SESSION_ID"'","top_n":'"$TOP_N"'}' > "$REPORT_DIR/result.jsonl"
```

Separate data source (transcripts under `~/.claude/projects/**`, not `timings.jsonl`), separate script, same `--since`/`--session-id`/`--top-n` scoping. `cost_exit=1` means no transcripts fell in window (or no match for `--session-id`) — not fatal: clock report still ships without a `## Tokens & cost` section. The two analyzers never share a report path write; only `report.md` is append-target, only after `cost_exit=0`, so a cost-side failure can't corrupt or block the clock report.

## Step 3: Mark run complete

The last line of the Step 2b block writes `$REPORT_DIR/result.jsonl` (`status: complete`, `since`, `session_id`, `top_n`) — it runs only after Step 2's stop branches passed, exactly as this step did.

## Step 4: Emit terminal output

Build the delivery block: read YAML header from `$REPORT_DIR/report.md` (first block between `---` lines), render as two-column Markdown table (`Field | Value`, one row per key, file order) per quality-gates.md §Report File Format — never the raw `---`-delimited block. Then `→ $REPORT_DIR/report.md`. Then read Headline split block plus top 3 sessions from per-session table, and — when a `## Tokens & cost` section is present — window total or single-session total cost line, add both as executive summary. Deliver the full block once in the final reply after the answer, after the last tool call — never before the question, never in an option or Bash/tool stdout. The first question's text carries only the report path, `Title`, `Outcome` and headline split, plus the cost total when present. Keep the question text to at most 6 plain lines (target 600 characters, hard cap 1,500), with no table, no pipe layout and no code fence. Options have no `preview`. Step 5 always asks once a report exists; a turn ending without the question still needs the full header table in the final reply, enforced by `Stop`.

> **Output-Routing exemption**: this report-header follow-up carries a plain summary in the question and the full delivery block in the final reply after the answer. Quality-gates.md §Report File Format's table-first-in-reply rule and Output Routing step 2a do not require a table before the question; the full dump below still goes to `.temp/`.

Also Write the long-output dump per quality-gates rule:

```text
Write(file_path=".temp/output-profile-<branch>-<YYYY-MM-DD>.md", content=<full report contents>)
```

Where `<branch>` = `$(git branch --show-current 2>/dev/null | tr '/' '-' || echo 'main')`.

Backed by `hooks/enforce-profile-header.js`: on `Stop` it keeps a turn going once per report when the final reply lacks the matching header table. While Step 1 state is live, Step 5's workflow follow-up requires a saved report and a short plain summary in the first question's text naming `$REPORT_DIR/report.md`, `Title` and `Outcome`; reply text and option fields do not supply that summary. Missing/unreadable delivery evidence blocks that transition; apply the placement the deny reason names before retrying. Diagnostic/recovery questions remain available under their own question header, not `profile`.

## Step 5: Follow-up gate

Invoke `AskUserQuestion` (denied by `enforce-profile-header.js` until Step 2 has written `report.md` — if the analyzer found no sessions, report that and stop instead of asking):

- header: `profile`
- question: "What next?" followed by the saved report's `Title`, `Outcome`, headline split and `→ $REPORT_DIR/report.md` as short plain lines per Step 4; include the cost total when present.
- (a) Drill into slowest session — re-run with `--session-id <id>`
- (b) Re-run with different window (`--since 7d`, `--since 30d`)
- (c) Skip — done
- Every option: label plus a one-line action description, no `preview`.

</workflow>

<notes>

- **Scope vs `/foundry:investigate`**: investigate diagnoses failures; profile measures wall time when things ran fine but slow.
- **Scope vs `foundry:perf-optimizer`**: perf-optimizer profiles Python/ML code (CPU/GPU/IO); profile measures Claude Code session wall time.
- **No model split on clock side**: timings.jsonl `model` field is 100% null in current task-log.js payload — clock report doesn't break down by model tier. Documented in report Confidence Gaps. `## Tokens & cost` section *does* break down by tier, but from a different data source (transcripts, not timings.jsonl) — the two never merge into one table.
- **Subagent internals invisible to clock side**: `timings.jsonl`'s hook fires only in main Claude Code process; subagent internal tool calls do NOT hit it, so reasoning bucket underestimates when subagent spawns dominate. Cost side lacks this gap — reads each session's `subagents/agent-*.jsonl` transcripts directly, so subagent token spend is included, not inferred.
- **Background agent join**: rows with `duration_ms < 1s` (a spawn-only row — every `Agent()` returns at dispatch) matched against invocations.jsonl `started→completed` pairs by `(agent, desc)` substring within ±60s window. Concurrent same-type spawns may mispair.
- **Bash clip**: any `duration_ms > 1h` for `tool=Bash` clipped at 3,600,000 ms to avoid runaway-shell pollution; clip count surfaced in legend.
- **Cost window filtering**: `cost_analyzer.py` uses each transcript's own row `timestamp` field for `--since` cutoff (file mtime is only a cheap pre-filter to skip old files before parsing them), so window is as accurate as clock side's.
- **Prices are public list rates**: `## Tokens & cost` dollar figures are proportional truth (useful for ranking, comparing), not a billing statement — effective plan rates may differ. Per-command `session $` also attributes a session's whole cost to every command it ran, so that column ranks, never sums.
- **Cost section is best-effort**: `cost_analyzer.py` finds no transcripts in window (or no match for `--session-id`): exits 1, `## Tokens & cost` section simply omitted — clock report still ships.
- **Read-only**: skill never edits source files. No commits, no pushes.

</notes>
