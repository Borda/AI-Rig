---
name: investigate
description: Systematic diagnosis for unknown failures — local environment, tool setup, CI vs local divergence, hook misbehavior, and runtime anomalies. Gathers signals broadly, ranks hypotheses, uses adversarial Codex review for ambiguous cases, probes each, and reports root cause with a recommended next action. NOT for known code bugs (/develop debug) or config quality (/audit).
argument-hint: <symptom, question, or failing command>
allowed-tools: Read, Bash, Grep, Agent, TaskCreate, TaskUpdate, AskUserQuestion
effort: high
---

<objective>

Diagnose unknown failures: broken local setup, environment mismatch, tool misbehavior, hook problems, CI vs local divergence, permission errors, runtime anomalies. Gather signals broadly, eliminate hypotheses systematically, report confirmed root cause + recommended next skill. No fixes — diagnosis only.

NOT for: known Python test failures with traceback (use `/develop:debug`); `.claude/` config quality sweep (use `/audit`).

</objective>

<inputs>

- **$ARGUMENTS**: required — symptom, question, or failing command, e.g.:
  - `"hooks not firing on Save"`
  - `"codex:codex-rescue agent exits 127 on this machine"`
  - `"/calibrate times out every run"`
  - `"CI fails but passes locally"`
  - `"uv run pytest can't find conftest.py"`

If $ARGUMENTS empty or too vague, use AskUserQuestion: "What exactly is failing or behaving unexpectedly? Include the command and any error output you can share."

</inputs>

<workflow>

**Task hygiene**: Before creating tasks, call `TaskList`. For each found task:

- status `completed` if work clearly done
- status `deleted` if orphaned / no longer relevant
- keep `in_progress` only if genuinely continuing

**Task tracking**: TaskCreate tasks for Gather, Hypothesise, Probe, Report; mark in_progress/completed as you go.

## Step 1: Parse symptom and scope

From $ARGUMENTS extract:

- **What**: specific failure or anomaly
- **Where**: local / CI / both; which tool or command; which skill or hook if applicable
- **When**: started recently (after change) or always broken; intermittent or consistent

## Step 2: Gather signals

Collect evidence in parallel — do NOT form hypotheses yet.

**Tool versions and PATH**:

```bash
which python3 && python3 --version                                                                   # timeout: 5000
which uv 2>/dev/null && uv --version 2>/dev/null || echo "uv: not found"                             # timeout: 5000
node --version 2>/dev/null || echo "node: not found"                                                 # timeout: 5000
claude plugin list 2>/dev/null | grep 'codex@openai-codex' || echo "codex (openai-codex): not found" # timeout: 5000
```

```bash
env | grep -E 'PATH|VIRTUAL_ENV|UV_|CLAUDE|HOME|SHELL|NODE' | sort # timeout: 5000
```

**Recent changes**:

```bash
git log --oneline -10        # timeout: 3000
git diff HEAD~3..HEAD --stat # timeout: 3000
```

**Config state** (when symptom involves Claude Code, hooks, or skills):

Use Read to check `.claude/settings.json` and `~/.claude/settings.json` — look for hook registrations, allow entries relevant to failing command, and `enabledMcpjsonServers`.

**Logs** (when symptom involves skill run, background agent, or hook):

Use Grep with pattern `ERROR|WARN|failed|not found|exit` across `.claude/logs/`, `/tmp/`, or relevant `_<skill>/` run dirs. Read last 50 lines of any relevant log file.

Capture all output before Step 3.

## Step 3: Rank hypotheses

List candidate root causes ranked by probability, drawing only from gathered evidence:

| Rank | Hypothesis | Supporting evidence | Ruling-out test |
| --- | --- | --- | --- |
| 1 | … | … | … |
| 2 | … | … | … |
| 3 | … | … | … |

Common categories:

- **Environment mismatch** — tool version differs; wrong virtualenv active; PATH missing entry
- **Missing dependency** — binary not on PATH; package not installed; module import fails
- **Config / permission error** — settings.json allow entry missing; hook path wrong; settings.local.json override
- **State pollution** — stale lock file, leftover tmp artifact, or cached state conflicts with current run
- **Recent change regression** — git commit or config edit introduced issue (check `git log`)
- **Sync drift** — project `.claude/` and `~/.claude/` diverged; compare manually or run `/audit setup`
- **External service** — network unavailable, API rate-limited, or remote tool unreachable

## Step 4: Auxiliary review (optional)

If `codex` plugin available AND top hypothesis has weak/circumstantial evidence (no direct confirming signal), request adversarial review:

```
Agent(subagent_type="codex:codex-rescue", prompt="Adversarial review of hypothesis quality: [provide symptom, signals, and hypothesis table]. Challenge the top hypothesis, identify blindspots, and surface alternative root causes. Read-only.")
```

Provide Codex: symptom (Step 1), key signals (Step 2), ranked hypothesis table (Step 3). Ask it to: identify blindspots not in table, challenge top hypothesis, surface alternative root causes.

- Add any Codex alternative hypotheses as new rows in Step 3 table
- Re-rank if Codex provides stronger evidence for lower-ranked candidate
- If Codex identifies category not in common list, add it

**Skip when**:

- Top hypothesis already has strong direct evidence (confidence clearly high)
- `codex` plugin not available (`claude plugin list` shows no `codex@openai-codex`)
- User requested speed or `/investigate --fast` specified

## Step 5: Probe top hypotheses

One targeted test per hypothesis — clear confirm/rule-out signal. Run independent probes in parallel.

```bash
# Example probes — adapt to the actual symptom

# Environment mismatch: check active interpreter
python3 -c "import sys; print(sys.executable, sys.version)"

# Missing allow entry: check home settings.json allow list
jq -r '.permissions.allow[]' ~/.claude/settings.json

# Hook path wrong: verify hook file exists
ls -la ~/.claude/hooks/

# Sync drift: compare project vs home settings
diff <(jq -S . .claude/settings.json) <(jq -S . ~/.claude/settings.json) | head -40
```

Per probe: mark **Confirmed**, **Ruled out**, or **Inconclusive**.

Stop when one hypothesis confirmed with clear evidence, or top-3 all ruled out (expand to lower-ranked candidates).

## Step 6: Report findings

```
## Investigation: <symptom>

**Root cause**: <confirmed cause, or "inconclusive — suspects narrowed to X, Y">

**Evidence**:
- <key finding that confirmed the diagnosis>
- <secondary supporting evidence>

**Ruled out**: <hypotheses eliminated and why>

**Recommended next action**: <one of:>
  - `/develop:fix` — code regression confirmed (application code only — NOT for `.claude/` changes)
  - `/manage update <name> "<change directive>"` — `.claude/` agent/skill/rule content needs updating (use this, NOT `/develop:feature or /develop:fix`, for any proposed change to `.claude/`)
  - `/audit fix` — structural/quality issue in `.claude/` config confirmed
  - `/foundry:init` — propagate project `.claude/` to `~/.claude/` (foundry plugin is the distribution path)
  - Manual step: <exact command to run>
  - Further investigation needed: <what additional info would resolve it>
```

End with `## Confidence` block per output standards.

</workflow>

<notes>

- **Diagnosis only** — never apply fixes; hand off with specific recommended action
- **Scope vs `/develop:debug`**: `/develop:debug` needs known test failure, runs TDD fix loop. `/investigate` = "something wrong, don't know what" — cause may not be in application code
- **Scope vs `/audit`**: `/audit` = scheduled quality sweep of `.claude/`. `/investigate` = triggered by live failure; two complement each other (investigate finds config symptom → audit confirms structural issue)
- **Broad first**: always complete Step 2 before hypothesising — premature anchoring = most common investigation failure
- **Parallel probes**: run independent probes in single response to avoid serial latency
- **Inconclusiveness valid**: report what ruled out and what info would close remaining gap — don't fabricate root cause to appear decisive

</notes>
