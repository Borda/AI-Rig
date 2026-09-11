---
name: investigate
description: "Systematic diagnosis for unknown failures — local environment, tool setup, CI vs local divergence, hook misbehavior, and runtime anomalies. Gathers signals broadly, ranks hypotheses, uses adversarial review (Codex or foundry:challenger) for ambiguous cases, probes each, and reports root cause with a recommended next action. NOT for known code bugs (/develop:debug (requires `develop` plugin)) or config quality (/foundry:audit). TRIGGER when: unknown failure with no Python traceback — hook not firing, CI passes locally but fails remotely, background agent stalled, behavior inconsistent with config; phrases: \"not working but config looks right\", \"hook not triggering\", \"why isn't X running\". SKIP: Python traceback present (use develop:debug (requires `develop` plugin)); known code bug with repro (use develop:fix (requires `develop` plugin)); pure config quality check (use foundry:audit)."
argument-hint: <symptom, question, or failing command> [--fast] [--keep "<items>"]
allowed-tools: Read, Write, Bash, Grep, Glob, Agent, TaskList, TaskCreate, TaskUpdate, AskUserQuestion
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

- **`--fast`**: optional flag — skip Step 4 adversarial Codex review; use when speed matters more than thoroughness or Codex unavailable.

If $ARGUMENTS empty or too vague, use AskUserQuestion: "What exactly is failing or behaving unexpectedly? Include the command and any error output you can share."

</inputs>

<compaction>
- Key boundary 1: end of Step 2 (signals.md written to run-dir), before Step 3 rank hypotheses.
- Key boundary 2: end of Step 3 (hypotheses.md written), refreshed again after each Step 5 probe verdict — so a mid-loop compaction does NOT re-rank or re-probe.
- Preserve: INVESTIGATE_RUN, symptom.txt, signals.md, hypotheses.md paths; adversarial-review path (codex/challenger) if Step 4 ran; probe ledger (hypothesis → Confirmed/Ruled-out/Inconclusive).
- Terminal path: end of Step 6 (report + follow-up gate complete).
</compaction>

<workflow>

**Task hygiene**: load and follow the protocol below.

```bash
# loads: compaction-contract.md
# audit-skip: resilience-replication
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/load_shared_doc.py" foundry skills/_shared task-hygiene.md  # timeout: 5000
```

**Task tracking**: TaskCreate tasks for Gather, Hypothesise, Probe, Report; mark in_progress/completed as you go.

## Step 1: Parse symptom and scope

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
KEEP_ITEMS=""
if [[ "$ARGUMENTS" =~ --keep[[:space:]]\"([^\"]+)\" ]]; then
    KEEP_ITEMS="${BASH_REMATCH[1]}"
fi
ARGUMENTS=$(echo "$ARGUMENTS" | sed 's/--keep "[^"]*"//g')
rm -f .temp/state/skill-contract.md ${TMPDIR:-/tmp}/investigate-verdicts-${CSID}  # clear stale contract + probe ledger (compaction-contract.md §Lifecycle)  # timeout: 5000
echo "$KEEP_ITEMS" > "${TMPDIR:-/tmp}/investigate-keep-items-${CSID}"
```

From $ARGUMENTS extract:

- **What**: specific failure or anomaly
- **Where**: local / CI / both; which tool or command; which skill or hook if applicable
- **When**: started recently (after change) or always broken; intermittent or consistent

**Unsupported flag check** — after all supported flags extracted (`--fast`, `--keep`), scan `$ARGUMENTS` for remaining `--<token>` tokens. If found: print `` ! Unknown flag(s): `--<token>`. Supported: `--fast`, `--keep`. `` then invoke `AskUserQuestion` — (a) **Abort** (stop, re-invoke with correct flags) · (b) **Continue ignoring** (skip unknown flags, proceed). On Abort: stop.

## Step 2: Gather signals

**Init run directory unconditionally at start of Step 2** — `$INVESTIGATE_RUN` must be set even when Step 4 skipped (`--fast` path), so Step 6's read of `$INVESTIGATE_RUN/*-review.md` does not expand to `/codex-review.md` or unset reference. Step 4 creates review files only when adversarial review runs; Step 6 must guard reads with `[ -f <path> ]`.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# timeout: 5000
INVESTIGATE_RUN=".temp/investigate/$(date -u +%Y-%m-%dT%H-%M-%SZ)"
mkdir -p "$INVESTIGATE_RUN"
echo "$INVESTIGATE_RUN" > "${TMPDIR:-/tmp}/investigate-run-path-${CSID}"  # persist; re-read in later steps
echo "INVESTIGATE_RUN=$INVESTIGATE_RUN"  # bash vars don't persist; read from stdout
```

Collect evidence in parallel — do NOT form hypotheses yet. **Tool versions and PATH**:

```bash
which python && python --version                                                                   # timeout: 5000
which uv 2>/dev/null && uv --version 2>/dev/null || echo "uv: not found"                             # timeout: 5000
node --version 2>/dev/null || echo "node: not found"                                                 # timeout: 5000
CODEX_STATUS=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/check_bridge.py" --status 2>/dev/null || echo "absent"); echo "bridge@borda-ai-rig: $CODEX_STATUS"  # timeout: 5000
```

```bash
env | grep -E 'PATH|VIRTUAL_ENV|UV_|CLAUDE|HOME|SHELL|NODE' | grep -v -E '(_TOKEN|_KEY|_SECRET|_PASSWORD|_PASS)=' | sort # timeout: 5000
```

**Recent changes**:

```bash
git log --oneline -10        # timeout: 3000
git diff HEAD~3..HEAD --stat # timeout: 3000
```

**Config state** (when symptom involves Claude Code, hooks, or skills):

Use Read to check `.claude/settings.json` — look for hook registrations, allow entries relevant to failing command, and `enabledMcpjsonServers`. For `~/.claude/settings.json` (outside allowed Read paths), use Bash:

```bash
jq . ~/.claude/settings.json  # timeout: 5000
```

**Logs** (when symptom involves skill run, background agent, or hook):

Use Grep with pattern `ERROR|WARN|failed|not found|exit` across `.notes/logs/`, `.claude/logs/` (legacy fallback), `/tmp/`, or relevant `.reports/<skill>/` run dirs. Read last 50 lines of any relevant log file.

Capture all output before Step 3.

After gathering evidence, capture top signals as working notes for Step 4 spawn prompts AND persist them to disk so the values survive the bash-state reset between Steps 2 → 3 → 4:

- `SYMPTOM_DESCRIPTION` — verbatim from `$ARGUMENTS`
- `KEY_SIGNALS` — write 3–5 bullet-point sentences summarizing the most diagnostic signals found above (tool versions, missing binaries, config anomalies, recent changes)

Use the Write tool (NOT a `echo > $INVESTIGATE_RUN/...` heredoc, which loses bash variable state across tool calls) to write the captured values to disk so Step 4 spawn prompts can instruct subagents to Read them rather than relying on inline interpolation:

- `Write(file_path="<INVESTIGATE_RUN>/symptom.txt", content=<SYMPTOM_DESCRIPTION>)` — substitute `<INVESTIGATE_RUN>` with the path printed in the Step 2 bash output above
- `Write(file_path="<INVESTIGATE_RUN>/signals.md", content=<KEY_SIGNALS>)`

Step 4 spawn prompts must instruct the subagent to Read these files (not rely on inline `${SYMPTOM_DESCRIPTION}` interpolation, which the LLM can paraphrase or truncate under context pressure).

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

Capture the ranked hypothesis table as `HYPOTHESIS_TABLE` and persist it to disk before Step 4 — use the Write tool:

- `Write(file_path="<INVESTIGATE_RUN>/hypotheses.md", content=<HYPOTHESIS_TABLE>)`

This avoids LLM-paraphrase risk when inlining a long table into a spawn prompt. Step 4 spawn prompts will instruct the subagent to Read this file.

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

**Skip entirely** when `--fast` passed, or top hypothesis has strong direct evidence. (`foundry:challenger` is always available as part of foundry plugin, so the skip condition simplifies to: skip when `--fast` passed.) Skip → proceed to Step 5.

When `--fast`: mark Step 4 task as `deleted` (not completed — it was skipped).

Otherwise, set up adversarial review. Run dir created in Step 2; re-resolve path string here (bash state does not persist):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# timeout: 5000
IFS= read -r INVESTIGATE_RUN < "${TMPDIR:-/tmp}/investigate-run-path-${CSID}" 2>/dev/null || INVESTIGATE_RUN=""
# fallback if path file absent
[ -z "$INVESTIGATE_RUN" ] && INVESTIGATE_RUN=$(find .temp/investigate -maxdepth 1 -mindepth 1 -type d 2>/dev/null | sort -Vr | head -1)
CODEX_OUT="$INVESTIGATE_RUN/codex-review.md"
echo "INVESTIGATE_RUN=$INVESTIGATE_RUN"
echo "CODEX_OUT=$CODEX_OUT"  # spawn-prompt reads both stdout lines
```

> **Step 2 appends the resolved run path to `${TMPDIR:-/tmp}/investigate-run-path-${CSID}`** so this resolution succeeds — see `echo "$INVESTIGATE_RUN" > "${TMPDIR:-/tmp}/investigate-run-path-${CSID}"` in Step 2.

Re-check Codex availability at point of use (bash vars don't persist across tool calls) and **echo result so next prose decision can read it**:

```bash
# check_bridge.py resolves the installed-and-enabled state in one call and already honors a
# project-local .claude/settings.json opt-out, which an inline registry read does not.
CODEX_STATUS=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/check_bridge.py" --status 2>/dev/null || echo "absent")  # timeout: 5000
[ "$CODEX_STATUS" = "available" ] && CODEX_AVAILABLE=true || CODEX_AVAILABLE=false
[ "$CODEX_AVAILABLE" = "false" ] && printf "  bridge@borda-ai-rig is %s — skipping bridge review\n" "$CODEX_STATUS"
echo "CODEX_AVAILABLE=$CODEX_AVAILABLE"  # bash vars don't persist; branch below MUST read this value from stdout
```

**Read `CODEX_AVAILABLE=…` from bash stdout above** (NOT shell state). Printed `true`: spawn Codex; else spawn `foundry:challenger`. Spawn prompts below instruct subagent to Read persisted symptom/signals/hypotheses files (written in Steps 2 and 3) — more reliable than inlining values, which the LLM can paraphrase under context pressure.

If the bridge is available (requires `bridge@borda-ai-rig` installed and enabled) — substitute concrete path strings for `<INVESTIGATE_RUN>` and `<CODEX_OUT>` before constructing the prompt:

> **Agent budget** — each spawn costs ~120,851 tok of fixed overhead (~73 tool-calls' worth) plus ~12.0 s/call, so work under ~73 calls is cheaper done inline: spawn nothing. Keep each agent near ~55 tool-calls; past ~60 they stall without returning an envelope, forcing reconstruction from disk. Every spawn prompt must require an envelope even on exhaustion — `partial: true` plus what was finished.

```text
Skill(skill="bridge:review", args="Read-only adversarial review of hypothesis quality. Read <INVESTIGATE_RUN>/symptom.txt, <INVESTIGATE_RUN>/signals.md, and <INVESTIGATE_RUN>/hypotheses.md. Challenge the top hypothesis, identify blind spots, and surface alternative root causes. Write full findings to <CODEX_OUT> and return a compact result envelope.")
```

Else (Codex unavailable) — substitute `<INVESTIGATE_RUN>` with the printed run-dir path:

```text
Agent(subagent_type="foundry:challenger", prompt="Adversarial review of hypothesis quality. Read these files for full context: <INVESTIGATE_RUN>/symptom.txt, <INVESTIGATE_RUN>/signals.md, <INVESTIGATE_RUN>/hypotheses.md. Challenge the top hypothesis, identify blindspots, and surface alternative root causes. Read-only analysis only. Write full findings to <INVESTIGATE_RUN>/challenger-review.md using the Write tool. Return ONLY: {\"status\":\"done\",\"file\":\"<path>\",\"findings\":N,\"severity\":{\"critical\":N,\"high\":N,\"medium\":N,\"low\":N},\"confidence\":0.N,\"summary\":\"<one-line>\"}")
```

Before issuing the call: scan constructed prompt string for remaining `<` or `>` characters — if present, substitution incomplete; resolve before spawning.

- Add challenger alternative hypotheses as new rows in Step 3 table
- Re-rank if challenger gives stronger evidence for lower-ranked candidate
- If challenger finds category not in common list, add it

## Step 5: Probe top hypotheses

One targeted test per hypothesis — clear confirm/rule-out signal. Run independent probes in parallel.

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

Per probe: mark **Confirmed**, **Ruled out**, or **Inconclusive**. Append each verdict to the probe ledger and refresh the contract — so a mid-loop compaction does not re-probe an already-decided hypothesis:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# WHY: probe verdicts live only in-context; a post-compact resume without this would re-probe ruled-out hypotheses (loop)
echo "<hypothesis> :: <Confirmed|Ruled-out|Inconclusive>" >> ${TMPDIR:-/tmp}/investigate-verdicts-${CSID}
IFS= read -r _IR < "${TMPDIR:-/tmp}/investigate-run-path-${CSID}" 2>/dev/null || _IR=""
_VERDICTS=$(tail -8 "${TMPDIR:-/tmp}/investigate-verdicts-${CSID}" 2>/dev/null)  # cap keeps contract compact; tail keeps most-recent verdicts
_REVIEW=""; [ -f "$_IR/codex-review.md" ] && _REVIEW="$_IR/codex-review.md"; [ -f "$_IR/challenger-review.md" ] && _REVIEW="$_IR/challenger-review.md"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/write_skill_contract.py" "foundry:investigate" "probe (Step 5)" "$_IR" "hypotheses=$_IR/hypotheses.md${_REVIEW:+, review=$_REVIEW}" "probe remaining pending hypotheses → confirm root cause → report (Step 6). Skip Confirmed/Ruled-out in the probed list below." "probed (do NOT re-probe)" "$_VERDICTS"  # timeout: 5000
```

Stop when one hypothesis confirmed with clear evidence, or top-3 all ruled out (expand to lower-ranked candidates).

## Step 6: Report findings

Re-resolve `$INVESTIGATE_RUN` from persisted path file (`cat "${TMPDIR:-/tmp}/investigate-run-path-${CSID}"`). Guard each read with `[ -f <path> ]`: if `$INVESTIGATE_RUN/codex-review.md` exists, read it; if `$INVESTIGATE_RUN/challenger-review.md` exists, read it. Either or both may be absent (Step 4 skipped via `--fast`, or spawned agent failed silently). Incorporate new hypotheses or blindspots from existing files into Evidence section below; skip read entirely if neither file present — do NOT block on missing review files.

```markdown
## Investigation: <symptom>

**Root cause**: <confirmed cause, or "inconclusive — suspects narrowed to X, Y">

**Evidence**:
- <key finding that confirmed the diagnosis>
- <secondary supporting evidence>

**Ruled out**: <hypotheses eliminated and why>

**Recommended next action**: <one of:>
  - `/develop:fix` — code regression confirmed (application code only — NOT for `.claude/` changes) (requires `develop` plugin — check plugin availability before following this recommendation)
  - `/foundry:manage update <name> "<change directive>"` — `.claude/` agent/skill content needs adding or updating (NOT for structural/quality sweeps — use `/foundry:audit` for that)
  - `/foundry:audit` — structural/quality issue in `.claude/` config confirmed (e.g. broken cross-refs, missing blocks, tag imbalance); NOT for content additions — use `/manage update` for those
  - `/foundry:setup` — propagate project `.claude/` to `~/.claude/` (foundry plugin is the distribution path)
  - Manual step: <exact command to run>
  - Further investigation needed: <what additional info would resolve it>
```

End with a `## Confidence` block:

```markdown
## Confidence
**Score**: 0.N — [high ≥0.9 | moderate 0.85–0.9 | low <0.85 ⚠]
**Gaps**:
- [e.g., root cause unconfirmed — probe was inconclusive; external service logs inaccessible]

**Refinements**: N passes.
- Pass 1: [gap addressed]
```

Invoke `AskUserQuestion` as follow-up gate: (a) Invoke recommended next action (from Recommended next action field above) (b) Run additional investigation with narrowed hypothesis (c) Skip — diagnosis complete

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
rm -f .temp/state/skill-contract.md ${TMPDIR:-/tmp}/investigate-verdicts-${CSID}  # clear contract + probe ledger — skill complete (compaction-contract.md §Lifecycle)  # timeout: 5000
```

</workflow>

<notes>

- **Diagnosis only** — never apply fixes; hand off with specific recommended action
- **Scope vs `/develop:debug`**: `/develop:debug` (requires `develop` plugin) needs known test failure, runs TDD fix loop. `/investigate` = "something wrong, don't know what" — cause may not be in application code
- **Scope vs `/foundry:audit`**: `/foundry:audit` = scheduled quality sweep of `.claude/`. `/investigate` = triggered by live failure; two complement each other (investigate finds config symptom → audit confirms structural issue)
- **Follow-up**: `/develop:fix` (requires `develop` plugin) for implementing resolution once root cause confirmed
- **Broad first**: always complete Step 2 before hypothesising — premature anchoring = most common investigation failure
- **Parallel probes**: run independent probes in single response, avoid serial latency
- **Inconclusiveness valid**: report what ruled out and what info would close remaining gap — don't fabricate root cause to appear decisive
- **Root-cause discipline**: drill to confirmed root cause before handoff — never hand off "likely cause"; if fix applied and symptoms persist, re-invoke investigate with residual symptom to continue the loop; full protocol in `rules/debugging.md`

</notes>
