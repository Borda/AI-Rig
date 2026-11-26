---
name: profile
description: "Session clock-time AND token/cost analyzer. Reads the foundry plugin's timings.jsonl and invocations.jsonl logs (written by task-log.js) for wall-time, plus Claude Code transcripts (~/.claude/projects/**, main-loop + subagent files) for token usage and USD cost, and merges both into one per-session and per-skill report — local-tool vs subagent-spawn vs Skill vs AskUserQuestion idle vs unattributed gaps between recorded calls, and main-loop vs subagent spend by model tier. Useful for answering \"why did /oss:resolve run 30 minutes?\", \"what did this session cost?\", or \"which skill burns the most tokens?\". Pure log/transcript read — no instrumentation, no skill edits, no LLM calls. Also pairs a before and an after run under one label, so a savings or cost claim is measured or labelled unmeasured. TRIGGER when: user asks where wall-clock time OR tokens/cost go during a skill/session, why a skill is slow or expensive, what dominates total runtime or spend, wants a per-skill rollup over a recent window, or wants a before/after measurement of a change; phrases: \"where does time go\", \"why so slow\", \"what did this cost\", \"token spend\", \"why so expensive\", \"profile last session\", \"clock breakdown\", \"session timing\", \"which skill burns tokens\", \"measure the saving\", \"before and after cost\". SKIP: per-line Python perf (use foundry:perf-optimizer); known failure or hang (use /foundry:investigate); a real billing statement (prices are public list rates, not effective plan rates)."
argument-hint: '[--since 24h|7d|30d] [--session-id ID] [--top-n N] [--pair-label LABEL --pair-side before|after]'
allowed-tools: Read, Write, Bash, TaskList, TaskCreate, TaskUpdate, AskUserQuestion
effort: low
---

<objective>

Bucket session **clock time** from `~/.claude/logs/{timings,invocations}.jsonl` into:

1. **Local tools** — Bash/Read/Edit/Write/Grep/Glob and other main-process tools
2. **Agent / subagent spawns** — Task/Agent calls (sync + background)
3. **Skill** — `tool=Skill` wall durations
4. **AskUserQuestion idle** — human-wait, separate column, excluded from compute total
5. **Unattributed time** — gaps between recorded calls, which are excluded from compute time

...and bucket session **tokens/cost** from Claude Code transcripts (`~/.claude/projects/**`, main-loop + subagent files) into:

6. **Sessions ranked by cost** (window mode) — main $ vs subagent $ vs total $ per session, plus a per-command rollup; or **one session's deep-dive** (`--session-id`) — cost by main/sidechain × model tier, agent roster, per-agent rows for the 25 costliest agents and a phase table over all (long, chain-link, challenge-\* retry flags), top cache-rebuild calls, cold-start share

Outputs a markdown report at `.reports/profile/<UTC-timestamp>-<unique-suffix>/report.md` plus a `.temp/output-profile-...md` copy: per-session clock table, per-skill clock rollup, top-N longest single calls, plus a `## Tokens & cost` section scoped to the same window or session ID.

NOT for: per-line Python perf (use `foundry:perf-optimizer`); known failure diagnosis (use `/foundry:investigate`); a real billing statement (prices are public list rates, not effective plan rates).

</objective>

<inputs>

- **`--since DURATION`** (default `24h`) — window: `NNs|NNm|NNh|NNd`
- **`--session-id ID`** — optional; restrict to one session
- **`--top-n N`** (default `5`) — slowest single calls to list
- **`--pair-label LABEL --pair-side before|after`** — optional, always together: record this run as one side of a before/after pair for the claim `LABEL` (`[A-Za-z0-9._:/-]`, at most 80 characters, e.g. `/oss:resolve`) in `.notes/profile/pairs.jsonl` and append a `## Paired measurement` section to the report (Step 2c). Profile the before side before the change lands and the after side after it, with the same `--since` (or each with its own `--session-id`).

If $ARGUMENTS empty, default window is 24h.

</inputs>

<workflow>

**Task hygiene** — task tools may be deferred; load before first use: `ToolSearch(query="select:TaskList,TaskCreate,TaskUpdate,TaskGet", max_results=4)`. Call `TaskList` first and triage each task it returns: `completed` if work clearly done, `deleted` if orphaned, keep `in_progress` only if genuinely continuing. Never spend a turn on bookkeeping alone — every `TaskCreate`/`TaskUpdate` ships in the same response as the next substantive tool call; one exception, `TaskUpdate(completed)` immediately before a long output block (`rules/task-lifecycle.md`).

**Task tracking**: TaskCreate two tasks up front — 1 "Run analyzers + render report" (Steps 1–3), 2 "Step 4: Print report header" (Step 4). Mark each `in_progress` before its first tool call; 1 completed once `report.md` exists, 2 completed right after the header and path are printed, before the executive summary.

## Step 1: Parse args + create run dir

**Unsupported flag check** — before executing Step 2, read quote-aware argument tokens left to right. Supported options are `--since`, `--session-id`, `--top-n`, `--pair-label`, and `--pair-side`. For a supported option, take the value after its first `=` or from the next token and consume the value before checking the next option. A dash-leading label remains a value, including when its spelling matches an option name. Pass each retained option to Step 2 as `--name=VALUE` with the value unchanged. For an unsupported option name, including `--noop`, invoke `AskUserQuestion` with question text `` ! Unknown flag(s): `--<token>`. Supported: `--since`, `--session-id`, `--top-n`, `--pair-label`, `--pair-side`. `` — (a) **Abort** (stop, re-invoke with correct flags) · (b) **Continue ignoring** (remove the unknown option at its parsed boundary and preserve every retained option and value). On Abort: stop.

The first part of the Step 2 block parses and validates the retained options, creates a fresh report directory, and saves both as `KEY=VALUE` lines to `${TMPDIR:-/tmp}/foundry-profile-state-${CSID}`. Steps 2b–3 read these values key by key in subsequent Bash calls.

## Step 2: Run analyzers — clock time, then tokens/cost

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name foundry) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# timeout: 60000
# --flags is mandatory for the helper; profile has no boolean flags. --keep-state: no eval; --inlined: profile keeps no compaction contract, so it clears none
python "$PLUGIN_ROOT/bin/parse-skill-flags.py" --flags noop --value-flags since,session-id,top-n,pair-label,pair-side --keep-state profile --inlined -- "$ARGUMENTS" >/dev/null || exit $?
IFS= read -r SINCE < "${TMPDIR:-/tmp}/profile-since-${CSID}" 2>/dev/null; SINCE="${SINCE:-24h}"
IFS= read -r TOP_N < "${TMPDIR:-/tmp}/profile-top-n-${CSID}" 2>/dev/null; TOP_N="${TOP_N:-5}"
IFS= read -r SESSION_ID < "${TMPDIR:-/tmp}/profile-session-id-${CSID}" 2>/dev/null || SESSION_ID=""
{ IFS= read -r PAIR_LABEL && ! IFS= read -r; } < "${TMPDIR:-/tmp}/profile-pair-label-${CSID}" || { echo "! BLOCKED — expected a single --pair-label value"; exit 1; }
IFS= read -r PAIR_SIDE < "${TMPDIR:-/tmp}/profile-pair-side-${CSID}" 2>/dev/null || PAIR_SIDE=""
[[ $SINCE =~ ^[0-9]+[smhd]$ && $TOP_N =~ ^[0-9]+$ && $SESSION_ID =~ ^[A-Za-z0-9._-]*$ ]] || { echo "! BLOCKED — invalid --since/--top-n/--session-id"; exit 1; }
[[ $PAIR_LABEL =~ ^[A-Za-z0-9._:/-]{0,80}$ ]] || { echo "! BLOCKED — invalid --pair-label"; exit 1; }
case "$PAIR_SIDE:${PAIR_LABEL:+set}" in before:set|after:set|:) ;; *) echo "! BLOCKED — --pair-label LABEL and --pair-side before|after go together"; exit 1;; esac
STAMP="$(date -u +%Y-%m-%dT%H-%M-%SZ)" || exit $?
mkdir -p .reports/profile || exit $?
REPORT_DIR="$(mktemp -d ".reports/profile/$STAMP-XXXXXXXX")" || exit $?
{
  echo "REPORT_DIR=$REPORT_DIR"
  echo "SINCE=$SINCE"
  echo "SESSION_ID=$SESSION_ID"
  echo "TOP_N=$TOP_N"
  echo "PAIR_LABEL=$PAIR_LABEL"
  echo "PAIR_SIDE=$PAIR_SIDE"
} | tee "${TMPDIR:-/tmp}/foundry-profile-state-${CSID}" || exit $?
set --; [ -n "$SESSION_ID" ] && set -- --session-id "$SESSION_ID"
python "$PLUGIN_ROOT/bin/timing_analyzer.py" \
    --since "$SINCE" \
    --top-n "$TOP_N" \
    --output "$REPORT_DIR/report.md" \
    "$@" 2>"$REPORT_DIR/warnings.log"
TIME_EXIT=$?
echo "time_exit=$TIME_EXIT"
[ "$TIME_EXIT" -lt 2 ] || { echo "! BLOCKED — timing_analyzer exit $TIME_EXIT; see $REPORT_DIR/warnings.log"; exit 1; }
[ "$TIME_EXIT" -eq 0 ] || { echo "No sessions in window; stopping."; exit 1; }
```

`set --` array form passes `--session-id` and its value as separate arguments only when set (shell-portable; an unquoted `"--session-id $SESSION_ID"` string is one argument under zsh). Exit code 1: no sessions in window — surface that, stop; nothing in Step 2b can rescue a missing clock report, since `enforce-profile-header.js` keys on `report.md` specifically. Exit code 2 or higher: analyzer error (bad arguments, crash) — surface the `! BLOCKED` line with the tail of `warnings.log`, stop; do not run Step 2b.

## Step 2b: Run analyzer — tokens/cost (best-effort, appended)

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name foundry) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# timeout: 60000
while IFS== read -r K V; do case "$K" in REPORT_DIR) REPORT_DIR=$V;; SINCE) SINCE=$V;; SESSION_ID) SESSION_ID=$V;; TOP_N) TOP_N=$V;; esac; done < "${TMPDIR:-/tmp}/foundry-profile-state-${CSID}"
set --; [ -n "$SESSION_ID" ] && set -- --session-id "$SESSION_ID"
python "$PLUGIN_ROOT/bin/cost_analyzer.py" \
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
STATUS=complete
[ "$COST_EXIT" -lt 2 ] || STATUS=partial
echo '{"status":"'"$STATUS"'","cost_exit":'"$COST_EXIT"',"since":"'"$SINCE"'","session_id":"'"$SESSION_ID"'","top_n":'"$TOP_N"'}' > "$REPORT_DIR/result.jsonl"
```

Separate data source (transcripts under `~/.claude/projects/**`, not `timings.jsonl`), separate script, same `--since`/`--session-id`/`--top-n` scoping. `cost_exit=1` means no transcripts fell in window (or no match for `--session-id`) — not fatal: clock report still ships without a `## Tokens & cost` section. The two analyzers never share a report path write; only `report.md` is append-target, only after `cost_exit=0`, so a cost-side failure can't corrupt or block the clock report.

## Step 2c: Record a paired side (only with `--pair-label`)

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name foundry) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# timeout: 30000
while IFS== read -r K V; do case "$K" in REPORT_DIR) REPORT_DIR=$V;; PAIR_LABEL) PAIR_LABEL=$V;; PAIR_SIDE) PAIR_SIDE=$V;; esac; done < "${TMPDIR:-/tmp}/foundry-profile-state-${CSID}"
[ -n "$PAIR_LABEL" ] || { echo "pair=not-requested"; exit 0; }
python "$PLUGIN_ROOT/bin/profile_pair.py" record --label="$PAIR_LABEL" --side "$PAIR_SIDE" --report "$REPORT_DIR" --append-to "$REPORT_DIR/report.md"
```

Runs after Step 2b, so available token and cost totals are paired too. `profile_pair.py` appends this run's side to `.notes/profile/pairs.jsonl` and writes a `## Paired measurement` section to `report.md` with the before and after values and relative change for each measured metric. The report gives a reason for each unmeasured metric. Each analyzer must count the same number of sessions on both sides. Window pairs require two distinct reports with ordered, nonoverlapping measurement intervals of equal length. Session pairs require distinct IDs and ordered, nonoverlapping observations for each analyzer. Record the before side first and choose `--since` broad enough to cover each session's complete timing span. Cost uses the whole session transcript. A pair failure is reported with the clock report. Skip this step without `--pair-label`.

## Step 3: Mark run complete

The last line of the Step 2b block writes `$REPORT_DIR/result.jsonl` (`status: complete` for `cost_exit` 0 or 1, `status: partial` for a cost analyzer error, plus `cost_exit`, `since`, `session_id`, `top_n`) — it runs only after Step 2's stop branches passed, exactly as this step did. Surface a cost analyzer error with `warnings.log` while delivering the clock report.

## Step 4: Emit terminal output

Build the delivery block: read YAML header from `$REPORT_DIR/report.md` (first block between `---` lines), render as two-column Markdown table (`Field | Value`, one row per key, file order) per quality-gates.md §Report File Format — never the raw `---`-delimited block. Then `→ $REPORT_DIR/report.md`. Then read Headline split block plus top 3 sessions from per-session table, and — when a `## Tokens & cost` section is present — window total or single-session total cost line, add both as executive summary. Deliver the full block once in the final reply after the answer, after the last tool call — never before the question, never in an option or Bash/tool stdout. The first question's text carries only the report path, `Title`, `Outcome` and headline split, plus the cost total when present. Keep the question text to at most 6 plain lines (target 600 characters, hard cap 1,500), with no table, no pipe layout and no code fence. Options have no `preview`. Step 5 always asks once a report exists; a turn ending without the question still needs the full header table in the final reply, enforced by `Stop`.

> **Output-Routing exemption**: this report-header follow-up carries a plain summary in the question and the full delivery block in the final reply after the answer. Quality-gates.md §Report File Format's table-first-in-reply rule and Output Routing step 2a do not require a table before the question; the full dump below still goes to `.temp/`.

Also Write the long-output dump per quality-gates rule — never overwrite an existing dump; if the path exists, append the first unused counter suffix (`-2.md`, `-3.md`, …) before calling Write:

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
- **Clock coverage** — the wall split counts each overlapping instant once. Gaps without recorded activity remain unattributed. The timing hook records main-process calls, and the cost analyzer reads each session's subagent transcripts directly for token spend.
- **Background agent join**: rows with `duration_ms < 1s` (a spawn-only row — every `Agent()` returns at dispatch) matched against invocations.jsonl `started→completed` pairs by `(agent, desc)` substring within ±60s window. Concurrent same-type spawns may mispair.
- **Bash clip**: any `duration_ms > 1h` for `tool=Bash` clipped at 3,600,000 ms to avoid runaway-shell pollution; clip count surfaced in legend.
- **Cost window filtering**: `cost_analyzer.py` uses each transcript's own row `timestamp` field for `--since` cutoff (file mtime is only a cheap pre-filter to skip old files before parsing them), so window is as accurate as clock side's. Window mode prices only calls inside the window; a session that began earlier counts its in-window spend, while `--session-id` shows the whole session.
- **Prices are public list rates**: `## Tokens & cost` dollar figures are proportional truth (useful for ranking, comparing), not a billing statement — effective plan rates may differ. Per-command `session $` also attributes a session's whole cost to every command it ran, so that column ranks, never sums.
- **Cost section is best-effort**: `cost_analyzer.py` finds no transcripts in window (or no match for `--session-id`): exits 1, `## Tokens & cost` section simply omitted — clock report still ships.
- **Savings and cost claims need a pair.** Use `(measured: <label>)` when a comparable pair shows a reduction in each quantity named in the claim, or `(unmeasured)` when evidence is unavailable. Wall time, observed compute time, tokens and dollars are checked independently. Use `(unmeasured)` for claims about percentiles or other statistics that paired totals cannot show. `python "$PLUGIN_ROOT/bin/profile_pair.py" check-claims <file> --section Unreleased` requires the exact `## Unreleased` heading.
- **Read-only**: skill never edits source files. No commits, no pushes. With `--pair-label` it appends one row to `.notes/profile/pairs.jsonl`.

</notes>
