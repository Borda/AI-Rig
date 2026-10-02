---
name: investigate
description: "Systematic diagnosis for unknown failures — local environment, tool setup, CI vs local divergence, hook misbehavior, and runtime anomalies. Gathers signals broadly, ranks hypotheses, uses adversarial review (Codex or foundry:challenger) for ambiguous cases, probes each, and reports root cause with a recommended next action. NOT for known code bugs (/develop:debug (requires `develop` plugin)) or config quality (/foundry:audit). TRIGGER when: unknown failure with no Python traceback — hook not firing, CI passes locally but fails remotely, background agent stalled, behavior inconsistent with config; phrases: \"not working but config looks right\", \"hook not triggering\", \"why isn't X running\". SKIP: Python traceback present (use develop:debug (requires `develop` plugin)); known code bug with repro (use develop:fix (requires `develop` plugin)); pure config quality check (use foundry:audit)."
argument-hint: <symptom, question, or failing command> [--fast] [--keep "<items>"]
allowed-tools: Read, Write, Bash, Grep, Glob, Agent, Skill, TaskList, TaskCreate, TaskUpdate, AskUserQuestion
model: opus
effort: high
---

<objective>

Diagnose unknown failures: broken local setup, environment mismatch, tool misbehavior, hook problems, CI vs local divergence, permission errors, runtime anomalies. Gather signals broadly, eliminate hypotheses systematically, report confirmed root cause + recommended next skill. No fixes — diagnosis only. NOT for: known Python test failures with traceback (use `/develop:debug` (requires `develop` plugin)); `.claude/` config quality sweep (use `/foundry:audit`).

</objective>

<inputs>

- **$ARGUMENTS**: required — symptom, question, or failing command, e.g.:

  - `"hooks not firing on Save"`
  - `"bridge review is unavailable on this machine"`
  - `"/calibrate times out every run"`
  - `"CI fails but passes locally"`
  - `"uv run pytest can't find conftest.py"`

- **`--fast`**: optional flag — skip Step 4 adversarial Codex review; use when speed matters more than thoroughness, or Codex unavailable.

$ARGUMENTS empty or too vague: use AskUserQuestion: "What exactly is failing or behaving unexpectedly? Include the command and any error output you can share."

</inputs>

<compaction>
- Key boundary 1: end of Step 2 (signals.md written to run-dir), before Step 3 rank hypotheses.
- Key boundary 2: end of Step 3 (hypotheses.md written), refreshed again after each Step 5 probe verdict — so a mid-loop compaction does NOT re-rank or re-probe.
- Preserve: INVESTIGATE_RUN, symptom.txt, signals.md, hypotheses.md paths; adversarial-review path (codex/challenger) if Step 4 ran; probe ledger (hypothesis → Confirmed/Ruled-out/Inconclusive).
- Terminal path: end of Step 6 (report + follow-up gate complete).
</compaction>

<workflow>

**Task hygiene** — task tools may be deferred; load before first use: `ToolSearch(query="select:TaskList,TaskCreate,TaskUpdate,TaskGet", max_results=4)`. Call `TaskList` first and triage each task it returns: `completed` if work clearly done, `deleted` if orphaned, keep `in_progress` only if genuinely continuing. Never spend a turn on bookkeeping alone — every `TaskCreate`/`TaskUpdate` ships in the same response as the next substantive tool call; one exception, `TaskUpdate(completed)` immediately before a long output block (`rules/task-lifecycle.md`).

**Task tracking**: TaskCreate tasks for Gather, Hypothesise, Probe, Report in the same response as the Step 1 block; each later `TaskUpdate` rides with that step's first real tool call — zero bookkeeping-only turns (measured: ~7 per run before this rule).

**Turn budget** — the profiled runs were 94% single-call turns, and every turn re-reads the whole live context. Each step below is one bash block plus the Read/Grep/Write calls it needs, all issued together in one response; independent probes go in one response too. Never split a block to look at half its output.

## Step 1: Parse symptom and scope

One block: flag parsing, stale-ledger clear, and the run directory Step 2 needs. Run-dir creation has no dependency on the scoping below, and a second bash block would cost a whole turn — a turn re-reads the entire live context (`claude-config.md` §Turn Batching).

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/extract-keep-flag.py" investigate "$ARGUMENTS"  # timeout: 5000 — parses --keep, clears stale contract
eval "$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/parse-skill-flags.py" --flags fast "$ARGUMENTS")"  # timeout: 5000
ARGUMENTS="$CLEAN_ARGS"
rm -f "${TMPDIR:-/tmp}/investigate-verdicts-${CSID}"  # stale probe ledger  # timeout: 3000
INVESTIGATE_RUN=".temp/investigate/$(date -u +%Y-%m-%dT%H-%M-%SZ)"
mkdir -p "$INVESTIGATE_RUN"
echo "$INVESTIGATE_RUN" > "${TMPDIR:-/tmp}/investigate-run-path-${CSID}"  # persist; re-read in later steps
echo "INVESTIGATE_RUN=$INVESTIGATE_RUN"  # bash vars don't persist; read from stdout
printf '%s\n' "$ARGUMENTS" | tr ' ' '\n' | grep -E '^--' | sed 's/^/UNKNOWN_FLAG=/'  # feeds unsupported-flag check
```

From $ARGUMENTS extract:

- **What**: specific failure or anomaly
- **Where**: local / CI / both; which tool or command; which skill or hook if applicable
- **When**: started recently (after change) or always broken; intermittent or consistent

**Unsupported flag check** — after all supported flags extracted (`--fast`, `--keep`), every `UNKNOWN_FLAG=--<token>` line printed by the Step 1 block is a remaining `--<token>`. Found: print `` ! Unknown flag(s): `--<token>`. Supported: `--fast`, `--keep`. `` then invoke `AskUserQuestion` — (a) **Abort** (stop, re-invoke with correct flags) · (b) **Continue ignoring** (skip unknown flags, proceed). On Abort: stop.

## Step 2: Gather signals

`$INVESTIGATE_RUN` is created in Step 1 and holds for every path, `--fast` included, so Step 6's read of `$INVESTIGATE_RUN/*-review.md` never expands to `/codex-review.md` or an unset reference. Step 4 creates review files only when adversarial review runs; Step 6 must guard reads with `[ -f <path> ]`. Re-read the path from `${TMPDIR:-/tmp}/investigate-run-path-${CSID}` in later steps — bash state does not persist.

Collect evidence in parallel — do NOT form hypotheses yet. **Tool versions, PATH, environment, recent changes** — one block; persists the bridge status so Step 4 reads it instead of re-probing:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
which python && python --version                                                                   # timeout: 5000
which uv 2>/dev/null && uv --version 2>/dev/null || echo "uv: not found"                             # timeout: 5000
node --version 2>/dev/null || echo "node: not found"                                                 # timeout: 5000
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/check_bridge.py" --status > "${TMPDIR:-/tmp}/investigate-codex-${CSID}" 2>/dev/null || echo "absent" > "${TMPDIR:-/tmp}/investigate-codex-${CSID}"  # timeout: 5000
printf 'bridge@borda-ai-rig: '; cat "${TMPDIR:-/tmp}/investigate-codex-${CSID}"
env | grep -E 'PATH|VIRTUAL_ENV|UV_|CLAUDE|HOME|SHELL|NODE' | grep -v -E '(_TOKEN|_KEY|_SECRET|_PASSWORD|_PASS)=' | sort # timeout: 5000
git log --oneline -10        # timeout: 3000
git diff HEAD~3..HEAD --stat # timeout: 3000
```

Issue the **Config state** and **Logs** calls below in the same response as this block — none depends on its output.

**Config state** (when symptom involves Claude Code, hooks, or skills):

Use Read to check `.claude/settings.json` — look for hook registrations, allow entries relevant to failing command, and `enabledMcpjsonServers`. For `~/.claude/settings.json` (outside allowed Read paths), use Bash (same response as the block above):

```bash
jq . ~/.claude/settings.json  # timeout: 5000
```

**Logs** (when symptom involves skill run, background agent, or hook):

Use Grep with pattern `ERROR|WARN|failed|not found|exit` across `.notes/logs/`, `.claude/logs/` (legacy fallback), `/tmp/`, or relevant `.reports/<skill>/` run dirs. Read last 50 lines of any relevant log file.

Capture all output before Step 3.

After gathering evidence, capture top signals as working notes for Step 4 spawn prompts, persist to disk so values survive bash-state reset between Steps 2 → 3 → 4. The two Writes below and the contract block after them go in **one** response:

- `SYMPTOM_DESCRIPTION` — verbatim from `$ARGUMENTS`
- `KEY_SIGNALS` — write 3–5 bullet-point sentences summarizing the most diagnostic signals found above (tool versions, missing binaries, config anomalies, recent changes)

Use Write tool (NOT `echo > $INVESTIGATE_RUN/...` heredoc, which loses bash variable state across tool calls) to write captured values to disk so Step 4 spawn prompts can instruct subagents to Read them rather than relying on inline interpolation:

- `Write(file_path="<INVESTIGATE_RUN>/symptom.txt", content=<SYMPTOM_DESCRIPTION>)` — substitute `<INVESTIGATE_RUN>` with the path printed in the Step 2 bash output above
- `Write(file_path="<INVESTIGATE_RUN>/signals.md", content=<KEY_SIGNALS>)`

Step 4 spawn prompts must instruct subagent to Read these files (not rely on inline `${SYMPTOM_DESCRIPTION}` interpolation, which LLM can paraphrase or truncate under context pressure).

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _INVESTIGATE_RUN < "${TMPDIR:-/tmp}/investigate-run-path-${CSID}" 2>/dev/null || _INVESTIGATE_RUN=""
IFS= read -r _KEEP < "${TMPDIR:-/tmp}/investigate-keep-items-${CSID}" 2>/dev/null || _KEEP=""
_PRESERVE="run-dir=$_INVESTIGATE_RUN, symptom=$_INVESTIGATE_RUN/symptom.txt, signals=$_INVESTIGATE_RUN/signals.md"
[ -n "$_KEEP" ] && _PRESERVE="$_PRESERVE; user-keep: $_KEEP"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/write_skill_contract.py" "foundry:investigate" "hypothesise+probe (after signal gather)" "$_INVESTIGATE_RUN" "$_PRESERVE" "rank hypotheses (Step 3) → adversarial review (Step 4) → probe (Step 5) → report (Step 6)"  # timeout: 5000
```

## Step 3: Rank hypotheses

List candidate root causes ranked by probability, drawing only from gathered evidence:

| Rank | Hypothesis | Supporting evidence | Ruling-out test |
| -- | -- | -- | -- |
| 1 | … | … | … |
| 2 | … | … | … |
| 3 | … | … | … |

Capture ranked hypothesis table as `HYPOTHESIS_TABLE`, persist to disk before Step 4 — use Write tool:

- `Write(file_path="<INVESTIGATE_RUN>/hypotheses.md", content=<HYPOTHESIS_TABLE>)`

Avoids LLM-paraphrase risk when inlining a long table into a spawn prompt. Step 4 spawn prompts instruct subagent to Read this file.

Refresh the compaction contract now that ranking is done — the boundary moves into the Step 4–5 loop so a mid-loop compaction resumes from `hypotheses.md` instead of re-ranking:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# Compaction contract — boundary 2: ranking done, entering adversarial+probe loop (compaction-contract.md §Lifecycle)
IFS= read -r _IR < "${TMPDIR:-/tmp}/investigate-run-path-${CSID}" 2>/dev/null || _IR=""
IFS= read -r _KEEP < "${TMPDIR:-/tmp}/investigate-keep-items-${CSID}" 2>/dev/null || _KEEP=""
_PRESERVE="run-dir=$_IR, symptom=$_IR/symptom.txt, signals=$_IR/signals.md, hypotheses=$_IR/hypotheses.md"
[ -n "$_KEEP" ] && _PRESERVE="$_PRESERVE; user-keep: $_KEEP"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/write_skill_contract.py" "foundry:investigate" "adversarial+probe (after hypotheses ranked)" "$_IR" "$_PRESERVE" "adversarial review (Step 4, unless --fast) → probe hypotheses (Step 5) → report (Step 6). Resume from hypotheses.md — do NOT re-rank."  # timeout: 5000
```

Common categories:

- **Environment mismatch** — tool version differs; wrong virtualenv active; PATH missing entry
- **Missing dependency** — binary not on PATH; package not installed; module import fails
- **Config / permission error** — settings.json allow entry missing; hook path wrong; settings.local.json override
- **State pollution** — stale lock file, leftover tmp artifact, or cached state conflicts with current run
- **Recent change regression** — git commit or config edit introduced issue (check `git log`)
- **Sync drift** — project `.claude/` and `~/.claude/` diverged; compare manually or `/foundry:audit setup`
- **External service** — network unavailable, API rate-limited, or remote tool unreachable

## Step 4: Auxiliary review (optional)

**Skip entirely** when `--fast` passed, or top hypothesis has strong direct evidence. Skip: proceed to Step 5.

When `--fast`: mark Step 4 task `deleted` (not completed — it was skipped).

Otherwise, set up adversarial review in one block — run dir from Step 1's sentinel, bridge status from Step 2's (bash state does not persist):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# timeout: 5000
IFS= read -r INVESTIGATE_RUN < "${TMPDIR:-/tmp}/investigate-run-path-${CSID}" 2>/dev/null || INVESTIGATE_RUN=""
# fallback if path file absent
[ -z "$INVESTIGATE_RUN" ] && INVESTIGATE_RUN=$(find .temp/investigate -maxdepth 1 -mindepth 1 -type d 2>/dev/null | sort -Vr | head -1)
# Step 2 persisted check_bridge.py --status, which already honors a project-local .claude/settings.json opt-out
IFS= read -r CODEX_STATUS < "${TMPDIR:-/tmp}/investigate-codex-${CSID}" 2>/dev/null || CODEX_STATUS="absent"
[ "$CODEX_STATUS" = "available" ] && CODEX_AVAILABLE=true || CODEX_AVAILABLE=false
[ "$CODEX_AVAILABLE" = "false" ] && printf "  bridge@borda-ai-rig is %s — skipping bridge review\n" "$CODEX_STATUS"
echo "INVESTIGATE_RUN=$INVESTIGATE_RUN"
echo "CODEX_OUT=$INVESTIGATE_RUN/codex-review.md"  # spawn-prompt reads these stdout lines
echo "CODEX_AVAILABLE=$CODEX_AVAILABLE"  # branch below MUST read this value from stdout
```

> **Step 1 writes the run path to `${TMPDIR:-/tmp}/investigate-run-path-${CSID}`, Step 2 the bridge status to `${TMPDIR:-/tmp}/investigate-codex-${CSID}`** — both reads above depend on them.

**Read `CODEX_AVAILABLE=…` from bash stdout above** (NOT shell state). Printed `true`: spawn Codex; else spawn `foundry:challenger`. Spawn prompts below instruct subagent to Read persisted symptom/signals/hypotheses files (written in Steps 2 and 3) — more reliable than inlining values, which LLM can paraphrase under context pressure.

Bridge available (requires `bridge@borda-ai-rig` installed and enabled): substitute concrete path strings for `<INVESTIGATE_RUN>` and `<CODEX_OUT>` before constructing prompt:

> **Agent budget** — each spawn costs ~120,851 tok of fixed overhead (~73 tool-calls' worth) plus ~12.0 s/call, so work under ~73 calls is cheaper done inline: spawn nothing — work-displacement only; an isolation-motivated spawn (adversarial reviewer, distinct specialist role, model tier, worktree) runs regardless of size. Keep each agent near ~55 tool-calls; past ~60 they stall without returning an envelope, forcing reconstruction from disk. Every spawn prompt must require an envelope even on exhaustion — `partial: true` plus what was finished.

```text
Skill(skill="bridge:review", args="Read-only adversarial review of hypothesis quality. Read <INVESTIGATE_RUN>/symptom.txt, <INVESTIGATE_RUN>/signals.md, and <INVESTIGATE_RUN>/hypotheses.md. Challenge the top hypothesis, identify blind spots, and surface alternative root causes. Return actionable findings with locations; do not apply fixes.")
```

After the call returns, use the Write tool yourself to persist its findings text to `<CODEX_OUT>` (bridge:review is read-only and does not write files) — Step 6 reads this path guarded by `[ -f <path> ]`.

Else (Codex unavailable): substitute `<INVESTIGATE_RUN>` with printed run-dir path:

```text
Agent(subagent_type="foundry:challenger", prompt="Adversarial review of hypothesis quality. Read these files for full context: <INVESTIGATE_RUN>/symptom.txt, <INVESTIGATE_RUN>/signals.md, <INVESTIGATE_RUN>/hypotheses.md. Challenge the top hypothesis, identify blindspots, and surface alternative root causes. Read-only analysis only. Write full findings to <INVESTIGATE_RUN>/challenger-review.md using the Write tool. Return ONLY: {\"status\":\"done\",\"file\":\"<path>\",\"findings\":N,\"severity\":{\"critical\":N,\"high\":N,\"medium\":N,\"low\":N},\"confidence\":0.N,\"summary\":\"<one-line>\"}")
```

Before issuing call: scan constructed prompt string for remaining `<` or `>` characters — present means substitution incomplete; resolve before spawning.

**Deadline** (`_shared/agent-spawn-protocol.md` §Deadlines): in the same response as the spawn, Write `<INVESTIGATE_RUN>/agent-watch-challenger.tsv` = `challenger\t<INVESTIGATE_RUN>/challenger-review.md\t600`, then end the turn — never `ScheduleWakeup`, `ListAgents` or `Monitor`. On its notification run once:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/investigate-run-path-${CSID}" 2>/dev/null || RUN_DIR=""
[ -n "$RUN_DIR" ] || { echo "! BLOCKED — investigate run-dir sentinel missing; cannot check agent deadline"; exit 1; }
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/agent_watch.py" --state-dir "$RUN_DIR"  # timeout: 5000
```

`done` → fold its findings in below · notification arrived but row not `done` → ⏱ `timed_out` now, note it in the report's Evidence, continue to Step 5 with the unreviewed table — never wait further.

- Add challenger alternative hypotheses as new rows in Step 3 table
- Re-rank if challenger gives stronger evidence for lower-ranked candidate
- Challenger finds category not in common list: add it

## Step 5: Probe top hypotheses

One targeted test per hypothesis — clear confirm/rule-out signal. Run independent probes in parallel: all of them in one response. A probe that runs tests runs only the tests covering the suspect — `codemap-py` test-impact when installed, else the test files named after the suspect module — never the full suite.

```bash
# Example probes — adapt to the actual symptom

# Environment mismatch
python --version  # timeout: 3000

# Missing allow entry
jq -r '.permissions.allow[]' ~/.claude/settings.json

# Hook path wrong
ls -la ~/.claude/hooks/

# Sync drift
diff <(jq -S . .claude/settings.json) <(jq -S . ~/.claude/settings.json) | head -40
```

Per probe: mark **Confirmed**, **Ruled out**, or **Inconclusive**. Append verdicts to probe ledger, refresh contract — so a mid-loop compaction doesn't re-probe an already-decided hypothesis. One block per probe batch: one `echo` line per verdict from that batch, then the refresh once:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# WHY: probe verdicts live only in-context; a post-compact resume without this would re-probe ruled-out hypotheses (loop)
echo "<hypothesis> :: <Confirmed|Ruled-out|Inconclusive>" >> ${TMPDIR:-/tmp}/investigate-verdicts-${CSID}
IFS= read -r _IR < "${TMPDIR:-/tmp}/investigate-run-path-${CSID}" 2>/dev/null || _IR=""
_VERDICTS=$(tail -8 "${TMPDIR:-/tmp}/investigate-verdicts-${CSID}" 2>/dev/null)  # cap keeps contract compact; tail keeps most-recent verdicts
_REVIEW=""; [ -f "$_IR/codex-review.md" ] && _REVIEW="$_IR/codex-review.md"; [ -f "$_IR/challenger-review.md" ] && _REVIEW="$_IR/challenger-review.md"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/write_skill_contract.py" "foundry:investigate" "probe (Step 5)" "$_IR" "hypotheses=$_IR/hypotheses.md${_REVIEW:+, review=$_REVIEW}" "probe remaining pending hypotheses → confirm root cause → report (Step 6). Skip Confirmed/Ruled-out in the probed list below." "probed (do NOT re-probe)" "$_VERDICTS"  # timeout: 5000
```

Stop when one hypothesis confirmed with clear evidence, or top-3 all ruled out (expand to lower-ranked candidates — max 2 expansion rounds, per CLAUDE.md §Safety breaks for loops default cap of 3). Cap hit with nothing confirmed: stop, report inconclusive with the ruled-out list — do not loop through the full candidate table.

## Step 6: Report findings

Re-resolve `$INVESTIGATE_RUN` from persisted path file (`cat "${TMPDIR:-/tmp}/investigate-run-path-${CSID}"`). Guard each read with `[ -f <path> ]`: `$INVESTIGATE_RUN/codex-review.md` exists, read it; `$INVESTIGATE_RUN/challenger-review.md` exists, read it. Either or both may be absent (Step 4 skipped via `--fast`, or spawned agent failed silently). Incorporate new hypotheses or blindspots from existing files into Evidence section below; skip read entirely if neither file present — do NOT block on missing review files.

```markdown
## Investigation: [symptom]

**Root cause**: [cause or inconclusive result]

**Evidence**:
- [supporting finding]
- [additional evidence]

**Ruled out**: [ruled-out hypotheses]

**Recommended next action**: [recommended action]
  - `/develop:fix` — code regression confirmed (application code only — NOT for `.claude/` changes) (requires `develop` plugin — check plugin availability before following this recommendation)
  - `/foundry:manage update <name> "<change directive>"` — `.claude/` agent/skill content needs adding or updating (NOT for structural/quality sweeps — use `/foundry:audit` for that)
  - `/foundry:audit` — structural/quality issue in `.claude/` config confirmed (e.g. broken cross-refs, missing blocks, tag imbalance); NOT for content additions — use `/manage update` for those
  - `/foundry:setup` — propagate project `.claude/` to `~/.claude/` (foundry plugin is the distribution path)
  - Manual step: [command to run]
  - Further investigation needed: [missing information]
```

> Include why each hypothesis was ruled out. When no cause is confirmed, mark the result inconclusive, name the narrowed suspects, and include the ruled-out explanations. Choose the recommended action from the options above.

End with a `## Confidence` block:

```markdown
## Confidence
**Score**: [score] — [high ≥0.9 | moderate 0.85–0.9 | low <0.85 ⚠]
**Gaps**:
- [confidence gaps]

**Refinements**: [pass count] passes.
- Pass 1: [gap addressed]
```

Invoke `AskUserQuestion` as follow-up gate: (a) Invoke recommended next action (from Recommended next action field above) (b) Run additional investigation with narrowed hypothesis (c) Skip — diagnosis complete

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
rm -f .temp/state/skill-contract.md ${TMPDIR:-/tmp}/investigate-verdicts-${CSID} ${TMPDIR:-/tmp}/investigate-codex-${CSID}  # clear contract, probe ledger, bridge status — skill complete (compaction-contract.md §Lifecycle)  # timeout: 5000
```

</workflow>

<notes>

- **Diagnosis only** — never apply fixes; hand off with specific recommended action
- **Scope vs `/develop:debug`**: `/develop:debug` (requires `develop` plugin) needs known test failure, runs TDD fix loop. `/investigate` = "something wrong, don't know what" — cause may not be in application code
- **Scope vs `/foundry:audit`**: `/foundry:audit` = scheduled quality sweep of `.claude/`. `/investigate` = triggered by live failure; two complement each other (investigate finds config symptom, audit confirms structural issue)
- **Follow-up**: `/develop:fix` (requires `develop` plugin) for implementing resolution once root cause confirmed
- **Broad first**: always complete Step 2 before hypothesising — premature anchoring = most common investigation failure
- **Parallel probes**: run independent probes in single response, avoid serial latency
- **Inconclusiveness valid**: report what's ruled out, what info would close remaining gap — don't fabricate root cause to appear decisive
- **Root-cause discipline**: drill to confirmed root cause before handoff — never hand off "likely cause"; fix applied and symptoms persist: re-invoke investigate with residual symptom to continue the loop; full protocol in `rules/debugging.md`

</notes>
