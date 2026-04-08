---
name: investigate
description: Systematic diagnosis for unknown failures — local environment, tool setup, CI vs local divergence, hook misbehavior, and runtime anomalies. Gathers signals broadly, ranks hypotheses, probes each, and reports root cause with a recommended next action. NOT for known code bugs (/develop debug) or config quality (/audit).
argument-hint: <symptom, question, or failing command>
allowed-tools: Read, Bash, Grep, Agent, TaskCreate, TaskUpdate, AskUserQuestion
effort: high
---

<objective>

Diagnose unknown failures of any kind: broken local setup, environment mismatch, tool misbehavior, hook problems, CI vs local divergence, permission errors, and runtime anomalies. Gathers signals broadly, eliminates hypotheses systematically, and reports a confirmed root cause with a recommended next skill to engage. Does NOT fix — diagnosis only.

NOT for: known Python test failures with a traceback (use `/develop debug`); `.claude/` config quality sweep (use `/audit`).

</objective>

<inputs>

- **$ARGUMENTS**: required — symptom, question, or failing command, e.g.:
  - `"hooks not firing on Save"`
  - `"codex:codex-rescue agent exits 127 on this machine"`
  - `"/calibrate times out every run"`
  - `"CI fails but passes locally"`
  - `"uv run pytest can't find conftest.py"`

If $ARGUMENTS is empty or too vague, use AskUserQuestion: "What exactly is failing or behaving unexpectedly? Include the command and any error output you can share."

</inputs>

<workflow>

**Task hygiene**: Before creating tasks, call `TaskList`. For each found task:

- status `completed` if the work is clearly done
- status `deleted` if orphaned / no longer relevant
- keep `in_progress` only if genuinely continuing

**Task tracking**: TaskCreate tasks for Gather, Hypothesise, Probe, Report; mark in_progress/completed as you go.

## Step 1: Parse symptom and scope

From $ARGUMENTS extract:

- **What**: the specific failure or anomaly
- **Where**: local / CI / both; which tool or command; which skill or hook if applicable
- **When**: started recently (after a change) or was always broken; intermittent or consistent

## Step 2: Gather signals

Collect evidence in parallel — do NOT form hypotheses yet.

**Tool versions and PATH**:

```bash
which python3 && python3 --version  # timeout: 5000
which uv 2>/dev/null && uv --version 2>/dev/null || echo "uv: not found"  # timeout: 5000
node --version 2>/dev/null || echo "node: not found"  # timeout: 5000
claude plugin list 2>/dev/null | grep 'codex@openai-codex' || echo "codex (openai-codex): not found"  # timeout: 5000
```

```bash
env | grep -E 'PATH|VIRTUAL_ENV|UV_|CLAUDE|HOME|SHELL|NODE' | sort  # timeout: 5000
```

**Recent changes**:

```bash
git log --oneline -10  # timeout: 3000
git diff HEAD~3..HEAD --stat  # timeout: 3000
```

**Config state** (when symptom involves Claude Code, hooks, or skills):

Use Read to check `.claude/settings.json` and `~/.claude/settings.json` — look for hook registrations, allow entries relevant to the failing command, and `enabledMcpjsonServers`.

**Logs** (when symptom involves a skill run, background agent, or hook):

Use Grep with pattern `ERROR|WARN|failed|not found|exit` across `.claude/logs/`, `/tmp/`, or relevant `_<skill>/` run dirs. Read the last 50 lines of any relevant log file.

Capture all output before proceeding to Step 3.

## Step 3: Rank hypotheses

List candidate root causes ranked by probability, drawing only from the evidence gathered:

| Rank | Hypothesis | Supporting evidence | Ruling-out test |
| ---- | ---------- | ------------------- | --------------- |
| 1    | …          | …                   | …               |
| 2    | …          | …                   | …               |
| 3    | …          | …                   | …               |

Common categories to consider:

- **Environment mismatch** — tool version differs; wrong virtualenv active; PATH missing entry
- **Missing dependency** — binary not on PATH; package not installed; module import fails
- **Config / permission error** — settings.json allow entry missing; hook path wrong; settings.local.json override
- **State pollution** — stale lock file, leftover tmp artifact, or cached state conflicts with current run
- **Recent change regression** — a git commit or config edit introduced the issue (check `git log`)
- **Sync drift** — project `.claude/` and home `~/.claude/` diverged; use `/sync` to check
- **External service** — network unavailable, API rate-limited, or remote tool unreachable

## Step 4: Auxiliary review (optional)

If the `codex` plugin is available AND the top hypothesis has weak or circumstantial evidence (no direct confirming signal), request an adversarial review of the hypothesis set:

```
Agent(subagent_type="codex:codex-rescue", prompt="Adversarial review of hypothesis quality: [provide symptom, signals, and hypothesis table]. Challenge the top hypothesis, identify blindspots, and surface alternative root causes. Read-only.")
```

Provide Codex with: the symptom (Step 1 output), key signals gathered (Step 2), and the ranked hypothesis table (Step 3). Ask it to: identify any blindspots not in the table, challenge the top hypothesis, and surface alternative root causes.

- Incorporate any alternative hypotheses Codex surfaces as new rows in the Step 3 table
- Re-rank if Codex provides stronger evidence for a lower-ranked candidate
- If Codex identifies a category not in the common list, add it

**Skip this step when**:

- Top hypothesis already has strong direct evidence (confidence clearly high)
- `codex` plugin is not available (`claude plugin list` shows no `codex@openai-codex`)
- User requested speed or `/investigate --fast` was specified

## Step 5: Probe top hypotheses

Design one targeted test per hypothesis that gives a clear confirm/rule-out signal. Run independent probes in parallel.

```bash
# Example probes — adapt to the actual symptom

# Environment mismatch: check active interpreter
python3 -c "import sys; print(sys.executable, sys.version)"

# Missing allow entry: check home settings.json allow list
cat ~/.claude/settings.json | jq -r '.permissions.allow[]'

# Hook path wrong: verify hook file exists
ls -la ~/.claude/hooks/

# Sync drift: compare project vs home settings
diff <(jq -S . .claude/settings.json) <(jq -S . ~/.claude/settings.json) | head -40
```

For each probe result: mark **Confirmed**, **Ruled out**, or **Inconclusive**.

Stop when one hypothesis is confirmed with clear evidence, or all top-3 are ruled out (expand to lower-ranked candidates).

## Step 6: Report findings

```
## Investigation: <symptom>

**Root cause**: <confirmed cause, or "inconclusive — suspects narrowed to X, Y">

**Evidence**:
- <key finding that confirmed the diagnosis>
- <secondary supporting evidence>

**Ruled out**: <hypotheses eliminated and why>

**Recommended next action**: <one of:>
  - `/develop fix` — code regression confirmed (application code only — NOT for `.claude/` changes)
  - `/manage update <name> "<change directive>"` — `.claude/` agent/skill/rule content needs updating (use this, NOT `/develop`, for any proposed change to `.claude/`)
  - `/audit fix` — structural/quality issue in `.claude/` config confirmed
  - `/sync apply` — drift between project and home `.claude/` confirmed
  - Manual step: <exact command to run>
  - Further investigation needed: <what additional info would resolve it>
```

End with a `## Confidence` block per output standards.

</workflow>

<notes>

- **Diagnosis only** — never apply fixes in this skill; hand off cleanly with a specific recommended action
- **Scope vs `/develop debug`**: `/develop debug` requires a known test failure and runs a TDD fix loop. `/investigate` is for "something is wrong, I don't know what" — the cause may not be in application code at all
- **Scope vs `/audit`**: `/audit` is a scheduled quality sweep of `.claude/` config. `/investigate` is triggered by a live failure; the two can complement each other (investigate finds a config symptom → audit confirms the structural issue)
- **Broad first**: always complete Step 2 signal gathering before hypothesising — premature anchoring is the most common investigation failure
- **Parallel probes**: run independent probes in a single response to avoid serial latency
- **Inconclusiveness is a valid outcome**: report what was ruled out and exactly what information would close the remaining gap — don't fabricate a root cause to appear decisive

</notes>
