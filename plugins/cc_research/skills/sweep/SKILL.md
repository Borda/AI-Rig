---
name: sweep
description: Non-interactive end-to-end pipeline — auto-configure program.md (accept defaults), run judge+refine loop (up to 3 iterations), then run the campaign. Single command from goal to result.
argument-hint: '"<goal>" [--team] [--compute=local|colab|docker] [--colab[=H100|L4|T4|A100]] [--codex] [--researcher] [--architect] [--journal] [--hypothesis <path>] [--skip-validation] [--out <path>] [--keep "<items>"]'
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, Agent, Skill, TaskCreate, TaskUpdate, AskUserQuestion
effort: medium
disable-model-invocation: true
---

<objective>

Non-interactive end-to-end research pipeline: auto-plan → judge gate → run. Single command from goal to result. Accepts goal string, passes all run/colab/team flags.

NOT for: interactive planning (use `/research:plan`); methodology review only (use `/research:judge`); running already-approved plan (use `/research:run`).

</objective>

<compaction>

- Key boundaries: end of S2 — program.md written and confirmed; end of S3 — judge+refinement verdict settled.
- Preserve at S2: program-path (output of plan), GOAL string, OUT path (TMPDIR key).
- Mid-loop refresh: after each S3 fix-apply the contract is rewritten with refine-iter/no-fixes-iter/last-verdict (placed after fixes so the "fixes applied" claim is true) — a mid-loop compaction resumes at the current iteration instead of restarting REFINE_ITER=0.
- Preserve at S3: judge verdict, JUDGE_REPORT path, program-path, GOAL.
- Clear at S1 start (stale prior run) and after S5 pipeline completes.

</compaction>

<workflow>

## Agent Resolution

<!-- Agent resolution: see _RESEARCH_SHARED/agent-resolution.md -->

**Agent resolution**: load and follow the protocol below. Contains: foundry check + fallback table. Foundry not installed → substitute each `foundry:X` with `general-purpose` per table.

```bash
# loads: compaction-contract.md
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/resolve_shared.py" --persist research-shared --emit agent-resolution.md || exit 1  # timeout: 5000 — cold resolve; every later site (including the judge/run steps this skill runs inline) reads the research-shared sentinel
```

Sweep delegates to plan (S2), judge (S3), run (S5) — see each skill's Agent Resolution for fallback handling.

## Steps S1–S5

Triggered by `sweep "goal" [--flags]`. Non-interactive end-to-end: auto-plan → judge gate → run.

**Shared path resolution** (always runs before S1):

`_RESEARCH_SHARED` does NOT survive the Agent Resolution block — each Bash call is a fresh shell — so re-resolve it here alongside `_RESEARCH_SKILLS`, and again in every later block that loads a skill file:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _RESEARCH_SHARED < "${TMPDIR:-/tmp}/research-shared-${CSID}" 2>/dev/null || _RESEARCH_SHARED=""  # warm read (Check 41)
_RESEARCH_SKILLS="${_RESEARCH_SHARED%/_shared}"
[ -z "$_RESEARCH_SKILLS" ] && _RESEARCH_SKILLS="${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/skills"
```

**Task tracking** — one task per stage, never a bookkeeping-only turn: measured sweeps spent up to 5 of ~15 turns on task calls alone. The cost was the standalone turns, not the task count, so stage-level progress stays visible.

- Load the deferred task tools (`ToolSearch(query="select:TaskCreate,TaskUpdate", max_results=2)`) in the same response as the Agent Resolution block.
- Create tasks for S1–S5 at start, all in the same response as the first S1 call. Steps the inlined plan, judge and run skills track keep their own task lines.
- Every later `TaskUpdate` rides with the next real tool call. A stop before S5 (abort, blocked, unresolved stop) closes the finished stages and marks the remaining ones `deleted` in the response that prints the stop message — one response, not one per task. The only standalone call is the final `completed` right before the R6 summary.
- **Independent calls share one response — never across a question**: the S1 keep-flag and unsupported-flag-protocol blocks together, after the existing-program.md guard is answered (the `--out` validation runs before the guard, as the guard needs its path); S2 codebase-scan Reads/Greps together.

### Step S1: Parse arguments

**Existing program.md guard** — sweep creates new program.md; one already exists at output path (default: `program.md` at project root, or `--out <path>` if provided) → invoke `AskUserQuestion` immediately — never silently overwrite, never hard-stop without recovery:

- question: "program.md already exists at `<output path>` — how to proceed?"
- (a) label: `Overwrite and re-sweep` — description: overwrite existing program.md, run plan+judge+run pipeline from scratch
- (b) label: `Abort — use existing program` — description: stop sweep; use `/research:run <program.md>` to execute the existing program

On (a): proceed to flag extraction below. On (b): print follow-up hint and stop. Check AFTER extracting `--out` flag so correct output path known before checking. (Single overwrite gate — S2 P-P3 bypassed for sweep since decision already made here.)

Extract `<goal>` — first positional argument (quoted or unquoted string describing optimization target).

Extract flags:

- `--colab[=HW]` — passed to plan (Config.compute) and run; if `=HW` present, extract `colab_hw` <!-- policy-sibling: plugins/cc_research/skills/judge/SKILL.md, plugins/cc_research/skills/run/SKILL.md, plugins/cc_research/skills/run/modes/colab-setup.md, plugins/cc_research/skills/plan/SKILL.md — canonical colab_hw known-set {H100, L4, T4, A100}; this skill's argument-hint restates it, keep in sync (plugins/CLAUDE.md §Policy Duplication Marker). -->
- `--compute=local|colab|docker` — passed through
- `--team` — passed through to run
- `--codex` — passed through to run
- `--researcher` — passed through to run; combine with `--architect` for dual-agent SOTA + architectural hypothesis pipeline; controls run's R0 hypothesis pass only, never sets `agent_strategy`
- `--architect` — passed through to run; enables architectural hypothesis pass via `foundry:solution-architect`; controls R0 only, never sets `agent_strategy`
- `--journal` — passed through to run when present; preserves per-iteration journal entries (requires `--researcher` or `--architect` — enforced by run R2)
- `--hypothesis <path>` — passed through to run when present; preloads hypothesis queue from the given file
- `--skip-validation` — passed to judge step (S3)
- `--out <path>` — optional: write program.md here instead of project root. **`.md` output target determined solely by `--out` (or default project-root `program.md`)** — never infer output path by scanning `<goal>` text for `.md` substrings; goal string is prose describing optimization target, not path argument.
- `--keep "<items>"` — compaction contract keep-items; appended to `preserve:` field at each boundary

**`--out` validation**: if `--out <path>` provided, validate path BEFORE any extraction or file write:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/check_output_within_root.py" --parse-out="$ARGUMENTS" --sentinel sweep-out-path  # timeout: 5000 — traversal + project-root guard; persists for S2/S3 contract writes (Check 41: fresh shell)
```

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/extract-keep-flag.py" sweep "$ARGUMENTS"  # timeout: 5000 — parses --keep, clears a stale contract, persists for S2/S3
```

**Unsupported flag check**: load and follow the protocol below. Supported flags for this skill: `--team`, `--compute`, `--colab`, `--codex`, `--researcher`, `--architect`, `--journal`, `--hypothesis`, `--skip-validation`, `--out`, `--keep`.

```bash
# loads: unsupported-flag-protocol.md
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _RESEARCH_SHARED < "${TMPDIR:-/tmp}/research-shared-${CSID}" 2>/dev/null || _RESEARCH_SHARED=""  # warm read (Check 41)
cat "$_RESEARCH_SHARED/unsupported-flag-protocol.md"
```

If `<goal>` missing or empty, stop:

```text
⚠ sweep requires a goal prompt.
Usage: /research:sweep "goal description" [--flags]
```

If extracted `<goal>` starts with `--`, treat as flag misparse — stop with `! Misparse: goal starts with '--'. Did you forget to quote the goal or omit it? Usage: /research:sweep "goal description" [--flags]`

### Step S2: Non-interactive plan

First, load plan mode step definitions below — only the **P-P1 through P-P3** range (P-P0, P-P4, agent resolution and notes are not used by sweep) — then execute steps **P-P1, P-P2, P-P2b and P-P3** with overrides (P-P0 skipped — `<goal>` always text string):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _RESEARCH_SHARED < "${TMPDIR:-/tmp}/research-shared-${CSID}" 2>/dev/null || _RESEARCH_SHARED=""  # warm read (Check 41)
_RESEARCH_SKILLS="${_RESEARCH_SHARED%/_shared}"
[ -z "$_RESEARCH_SKILLS" ] && _RESEARCH_SKILLS="${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/skills"
sed -n '/^### Step P-P1:/,/^### Step P-P4:/{/^### Step P-P4:/!p;}' "$_RESEARCH_SKILLS/plan/SKILL.md"
```

Load prints nothing → resolution failed or plan step headings renamed; stop with `! sweep: cannot load plan/SKILL.md P-P1–P-P3 — research plugin path unresolved.` Never improvise P-P1–P-P3.

**Out-path handoff** — P-P0 (skipped) is the only writer of the sentinel P-P2b reads for its overwrite check; without it P-P2b tests `program.md` even when `--out` redirects the write. Write it from the S1 `--out` value first:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _OUT < "${TMPDIR:-/tmp}/sweep-out-path-${CSID}" 2>/dev/null || _OUT=""
echo "$_OUT" > "${TMPDIR:-/tmp}/research-plan-out-arg-${CSID}"  # empty = P-P2b default program.md, same as P-P0
```

**Gates overridden in sweep** — the inlined plan steps ask these questions; sweep is non-interactive and S1 owns the single overwrite decision:

- **P-P1 scope guard**: goal not an optimization goal → stop with `! sweep: goal does not look like an optimization goal (expects "Reduce X" / "Increase Y"). Rephrase and re-run.` — no question. Stop is terminal: print that line plus at most one redirect line naming the better-fit skill or agent, then end the reply — never answer, review, or list findings for the off-domain request inline, not even labelled "for reference" or "not sweep output".
- **P-P2b overwrite and advisor questions**: treat `OUTPUT_EXISTS` as false. An advisor `ok: false` → print `sweep: advisor note → <headline>` with the suggestions path and continue to P-P3; the S3 judge gate checks the written program.

> Include P-P2b analysis in S2 synthesis — P-P2b findings are scoped hypotheses needing consolidation into program.md written by P-P3; skipping P-P2b drops hypothesis context from generated program.

**Mandatory P-P1 codebase scan** — sweep MUST execute P-P1 to derive `metric_cmd` and `guard_cmd` from codebase. Skipping P-P1 produces program.md with placeholder commands failing at run-time. P-P1 cannot complete (no detectable test runner, no benchmark scripts, no metric command candidates): mark program **INCOMPLETE**, do NOT proceed to S3 — print `! sweep: P-P1 codebase scan could not derive metric_cmd/guard_cmd from <project-root>. Program marked INCOMPLETE — manual configuration required. Run /research:plan "<goal>" interactively to configure.` and stop.

- **P-P2 (config presentation)**: Accept all auto-detected defaults without prompting. Print proposed config as informational block prefixed `sweep: auto-config →` — do NOT wait for confirmation.
- `--colab[=HW]` or `--compute=colab` passed → write `compute: colab` (and `colab_hw: <HW>` if provided) into Config block.
- **scope_files**: derive from goal string — extract domain-relevant file patterns (e.g. goal mentioning "neural network" → `["*.py", "models/**", "train*.py"]`; goal mentioning "config" or "YAML" → `["*.yaml", "*.yml", "*.json"]`). Default `["**/*.py"]` only when goal gives no domain signals, and never when it would cover an evaluator script (a file named by the derived `metric_cmd`/`guard_cmd`): scope has no exclusion syntax and run's Phase 3 rejects any iteration touching an evaluator file, so narrow the patterns to directories that omit it (e.g. `src/**/*.py`). **Multiple keyword matches**: merge (union) all matched patterns. Always include derived `scope_files` in `sweep: auto-config →` printout — users can't correct silently wrong scope without seeing it.
- **agent_strategy**: set to value accepted by judge C9 (`auto` / `perf` / `code` / `ml` / `arch`). `--researcher`/`--architect`/`--team` control only run's R0 hypothesis passes and team dispatch — they never force a strategy (a non-ML goal with `--researcher` must not become `ml`; `arch` implementation is not a solution-architect task). Resolve from the goal and derived `metric_cmd` text, first match wins:
  - (1) goal/metric names coupling, cohesion or modularity → `"arch"`.
  - (2) else any other unambiguous run/SKILL.md keyword-heuristics class (`ml` / `perf` / `code`) → that value.
  - (3) else → `"auto"` (run infers from metric_cmd keywords).
  - Never write `"dual-agent: ..."`, `"team"`, `"researcher"`, or `"default"` — those values fail C9.
  - Record flag combination + dual-agent dispatch intent separately in `## Notes` (e.g. `dispatch: dual-agent (researcher primary + architect feasibility filter)`) so orchestration intent preserved without overriding validated `agent_strategy` field.
- **P-P3 (write program.md)**: Write to `<--out path>` if provided; else `program.md` at project root.
  - Output path exists: P-P3's own AskUserQuestion overwrite gate is bypassed for sweep — already resolved by the S1 guard (see Step S1) before S2 began. Write program.md directly; do not re-prompt.

Print on completion:

```text
sweep: plan → <output path> ✓
```

### Step S3: Judge + refinement loop

Load judge mode step definitions — `$_RESEARCH_SKILLS` from S2 is gone (fresh shell per Bash call), so re-resolve it here rather than dereferencing it bare; same call writes the boundary-1 compaction contract first — no decision sits between "program written" and "load judge", so they ride one turn. The load starts at the `## Judge Mode` heading and runs to the end of the file (J1–J6 and notes); the frontmatter, objective, compaction and Agent Resolution sections are not used — sweep's own Agent Resolution already wrote the `research-shared` sentinel:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# boundary 1: after S2 plan written (compaction-contract.md)
IFS= read -r _OUT < "${TMPDIR:-/tmp}/sweep-out-path-${CSID}" 2>/dev/null || _OUT="program.md"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/write_skill_contract.py" --keep-sentinel sweep-keep-items "research:sweep" "judge-gate (after S2 plan written)" "n/a" "program-path=${_OUT}" "S3 judge+refinement loop against ${_OUT}" -- calls_ahead=60 "drop=S1 probe output and S2 plan drafting"  # timeout: 5000
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/compaction_boundary.py"  # timeout: 60000 — prints the Boundary step, then the foundry gate decision; follow it
IFS= read -r _RESEARCH_SHARED < "${TMPDIR:-/tmp}/research-shared-${CSID}" 2>/dev/null || _RESEARCH_SHARED=""  # warm read (Check 41)
_RESEARCH_SKILLS="${_RESEARCH_SHARED%/_shared}"
[ -z "$_RESEARCH_SKILLS" ] && _RESEARCH_SKILLS="${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/skills"
sed -n '/^## Judge Mode/,$p' "$_RESEARCH_SKILLS/judge/SKILL.md"
```

Load prints nothing / "No such file or directory" → resolution failed; stop with `! sweep: cannot load judge/SKILL.md — research plugin path unresolved.` Never improvise J1–J6: the judge gate is the only check between an auto-generated program and the S5 campaign.

**Inlined judge under `cat`**: `$ARGUMENTS` is empty in the loaded blocks, and J1's `parse-skill-flags.py --keep-state judge` would clear sweep's compaction contract. Skip J1's **Flag parsing** and **Unsupported flag check** blocks (S1 ran both) and persist the validation flag with the one block matching S1. J3's boundary block self-skips here (`sweep-out-path` sentinel present), so sweep's own contract stays live. `--skip-validation` given:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo true > "${TMPDIR:-/tmp}/judge-skip-validation-${CSID}"
```

Not given:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo false > "${TMPDIR:-/tmp}/judge-skip-validation-${CSID}"
```

Initialize `REFINE_ITER = 0`, `MAX_REFINE = 3`, `NO_FIXES_ITER = 0`.

Repeat up to `MAX_REFINE` times:

1. Increment `REFINE_ITER`. Run judge mode (J1–J6 from the `judge/SKILL.md` loaded above) against program file.

   - Pass `--skip-validation` if user provided it; else include validation (J4).
   - Capture J6 verdict and judge report path (`JUDGE_REPORT`).

2. Print: `` sweep: judge iteration `REFINE_ITER`/`MAX_REFINE` → `VERDICT`  ``

3. **If `APPROVED`** — exit loop, outcome `approved`.

4. **If `BLOCKED`** — exit loop, outcome `blocked`. No fix attempt — BLOCKED = fundamental design flaw requiring human redesign.

5. **If `NEEDS-REVISION`**:

   - `REFINE_ITER < MAX_REFINE`:
     - Read `JUDGE_REPORT`. Extract `### Required Changes` section.

     - `### Required Changes` section absent: increment `NO_FIXES_ITER`. `NO_FIXES_ITER >= 2` (two consecutive judge runs returning NEEDS-REVISION without `### Required Changes` section): exit loop with outcome `judge-report-malformed` and print `! sweep: judge emitted NEEDS-REVISION but report contains no Required Changes section in 2 consecutive iterations — possible judge formatting issue. Inspect <JUDGE_REPORT>.` Invoke `AskUserQuestion` — (a) `proceed to run anyway` · (b) `abort`. On (a): proceed to S5. On (b): print follow-up hint and stop. Otherwise (NO_FIXES_ITER < 2): print `sweep: judge report missing Required Changes section — re-judging without edits (NO_FIXES_ITER=N)` and continue loop (re-judge with unchanged file).

     - Present: reset `NO_FIXES_ITER = 0`. Apply each fix to program file via Edit tool.

       - **Always re-read program file before each sequential Edit call; never assume file content stable between tool calls** — earlier Edits in batch may have shifted line offsets or modified surrounding context; stale `old_string` from judge report may no longer match.
       - Count applied fixes as `N_FIXES`; track failures as `N_FAILS`. Any Edit call fails (old_string not found or not unique): increment `N_FAILS`, continue remaining fixes.
       - After all fixes attempted: `N_FAILS > 0` → print `⚠ N_FAILS edit(s) failed — file may have changed since judge run; re-judging with partial fixes (N_FIXES applied)`. `N_FIXES == 0` AND `N_FAILS > 0` → print `! All edits failed — re-judging without changes (edit conflict; check program file manually)`.
       - Print: `sweep: applied N_FIXES fix(es) to <program path> — re-judging`

     - **Refresh compaction contract now** — fixes for this iteration applied, so contract can truthfully assert them. Substitute literal `REFINE_ITER`, `MAX_REFINE`, `NO_FIXES_ITER`, `VERDICT`, `JUDGE_REPORT` values tracked (fill-in template like judge-report tokens, not verbatim-run block — counters are prose loop state, not shell vars):

       ```bash
       # WHY: no post-fix refresh → compaction here resumes boundary-1 (pre-loop), re-judges from iter 1. Placed after fixes so "applied through iteration N" holds.
       export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
       case "<REFINE_ITER>|<MAX_REFINE>|<NO_FIXES_ITER>|<VERDICT>|<JUDGE_REPORT>" in *'<'*'>'*) echo "! BLOCKED — contract placeholders not substituted"; exit 1;; esac
       IFS= read -r _OUT < "${TMPDIR:-/tmp}/sweep-out-path-${CSID}" 2>/dev/null || _OUT="program.md"
       python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/write_skill_contract.py" --keep-sentinel sweep-keep-items "research:sweep" "judge+refinement loop (S3, iteration <REFINE_ITER>/<MAX_REFINE> — fixes applied)" "n/a" "program-path=${_OUT}, refine-iter=<REFINE_ITER>, no-fixes-iter=<NO_FIXES_ITER>, last-verdict=<VERDICT>, judge-report=<JUDGE_REPORT>" "re-judge ${_OUT} (it carries the fixes applied through iteration <REFINE_ITER>) → continue loop; do NOT reset REFINE_ITER. Exit on APPROVED/BLOCKED or REFINE_ITER==MAX_REFINE." -- calls_ahead=30 "drop=judge prompts and agent output"  # timeout: 5000
       python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/compaction_boundary.py"  # timeout: 60000 — prints the Boundary step, then the foundry gate decision; follow it
       ```

     - Continue next iteration (loop item 1 will re-judge).
   - `REFINE_ITER == MAX_REFINE` — exit loop, outcome `unresolved`.

> **Safety net**: loop edits modify `<program path>` in place; the S1 overwrite gate (P-P3's own gate is bypassed for sweep, see S2 P-P3 note) already secured user authorization before S2 wrote the file. Recover prior file from git if needed.

Substitute literal `VERDICT` and `JUDGE_REPORT` values tracked by the loop (fill-in template, same convention as the mid-loop block above — counters and verdict are prose loop state, never shell vars; `${_OUT}` stays shell-expanded):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# boundary 2: after S3 judge loop settles verdict (compaction-contract.md)
case "<VERDICT>|<JUDGE_REPORT>" in *'<'*'>'*) echo "! BLOCKED — contract placeholders not substituted"; exit 1;; esac
IFS= read -r _OUT < "${TMPDIR:-/tmp}/sweep-out-path-${CSID}" 2>/dev/null || _OUT="program.md"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/write_skill_contract.py" --keep-sentinel sweep-keep-items "research:sweep" "run-gate (after S3 judge+refinement)" "n/a" "program-path=${_OUT}, judge-verdict=<VERDICT>, judge-report=<JUDGE_REPORT>" "S4 gate on verdict → S5 run program if approved" -- calls_ahead=20 "drop=judge loop prompts, fixes and agent output"  # timeout: 5000
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/compaction_boundary.py"  # timeout: 60000 — prints the Boundary step, then the foundry gate decision; follow it
```

> A `<REFINE_ITER>`, `<MAX_REFINE>`, `<NO_FIXES_ITER>`, `<VERDICT>` or `<JUDGE_REPORT>` placeholder surviving into either contract block aborts it with `! BLOCKED` before anything is written — substitute and re-run the block; a missing contract means the verdict is unsettled, never `approved`.

### Step S4: Gate on loop outcome

| Outcome | Action |
| -- | -- |
| `approved` | Print `sweep: plan approved (REFINE_ITER/MAX_REFINE iteration(s)) ✓` → proceed to S5 |
| `blocked` | Print `sweep: judge → BLOCKED ✗`; show all critical findings from report; print follow-up hint; stop |
| `unresolved` | Print `sweep: judge unresolved after MAX_REFINE iterations ✗`; keep the full remaining Required Changes in `<last report path>`, then print that list once as the last reply text before calling `AskUserQuestion` — do NOT write options as plain text. The question text carries a plain summary (remaining-change count, headline, `→ <last report path>`, "Unresolved — how to proceed?"), at most 6 plain lines (target 600 characters, hard cap 1,500), no table, pipe layout, or code fence. Options have one-line descriptions and no previews; preserve order and keep the recommended option second: (a) label `proceed to run anyway` — run despite the listed changes; (b) label `fix manually then re-run` — stop for manual fixes; (c) label `abort` — stop the sweep. If `a`, proceed to S5; if `b` or `c`, print follow-up hint and stop |
| `judge-report-malformed` | S3 already invoked `AskUserQuestion` with (a) proceed / (b) abort and handled the answer — S4 is a no-op for this outcome (S3 already proceeded to S5 or stopped). |

Follow-up hint (blocked or unresolved):

```text
Fix the issues above in <program path>, then:
  /research:judge <program path>          ← re-validate
  /research:run <program path>            ← run when approved
  /research:sweep "revised goal" [flags]  ← re-sweep from scratch
```

### Step S5: Run

Load run mode step definitions — re-resolve rather than dereferencing the S2/S3 variable, which died with its shell. The load drops only the frontmatter and objective (everything through the `</objective>` line); constants, Agent Resolution, R0–R7, the mode sections and notes all stay. Execute run's Agent Resolution block first: it writes the `research-run-skill-dir` sentinel that every `modes/` load in the steps below reads:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _RESEARCH_SHARED < "${TMPDIR:-/tmp}/research-shared-${CSID}" 2>/dev/null || _RESEARCH_SHARED=""  # warm read (Check 41)
_RESEARCH_SKILLS="${_RESEARCH_SHARED%/_shared}"
[ -z "$_RESEARCH_SKILLS" ] && _RESEARCH_SKILLS="${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/skills"
sed '1,/^<\/objective>$/d' "$_RESEARCH_SKILLS/run/SKILL.md"
```

Load fails → stop with `! sweep: cannot load run/SKILL.md — research plugin path unresolved.` Never improvise R1–R7: S5 executes and commits against the user's repo.

Run Default Mode (R1–R7 from the `run/SKILL.md` loaded above) passing program file from S2 as the first positional argument, plus all flags.

**Run-args handoff** — run's R1 flag blocks read `$ARGUMENTS`, which here holds sweep's goal text. Write run's argument string (program path + forwarded flags, `--keep "<items>"` verbatim) to the sentinel those blocks read first, before executing R1; run's final blocks never delete it, so the sweep completion block below does:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
cat > "${TMPDIR:-/tmp}/sweep-run-args-${CSID}" <<'EOF'
<program path> <forwarded flags verbatim>
EOF
```

> Forward same flags accepted at Step S1 (`--colab[=HW]`, `--compute`, `--team`, `--codex`, `--researcher`, `--architect`, `--journal`, `--hypothesis <path>`, `--keep "<items>"`).

> **Flag-forwarding invariant**: any of `--journal` / `--hypothesis` set at sweep entry MUST appear in S5 run invocation. Dropping them silently breaks resume continuity and hypothesis queue.

> **`--team` and interactivity**: `--team` passed → sweep semi-interactive — run mode Phase B presents user confirmation gate before Phase C. Gate cannot be bypassed from sweep context; sweep pauses and waits. Expected behavior.

On completion, standard R6 terminal summary printed. Also prepend:

```text
sweep: complete — plan → judge → run pipeline finished
```

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
rm -f .temp/state/skill-contract.md "${TMPDIR:-/tmp}/sweep-run-args-${CSID}"  # clear contract — sweep pipeline complete (compaction-contract.md §Lifecycle); handoff sentinel must not leak into a later direct run  # timeout: 5000
```

</workflow>

<notes>

- **Overwrite gate** (S1): output path exists → resolved once via S1's AskUserQuestion — P-P3's own overwrite gate is bypassed for sweep to avoid double-prompting; no silent `.bak` rename + overwrite. Pipeline exemption applies to non-interactive CI pipelines only, not user-initiated sweeps. Use git to recover prior file if needed.
- **`--journal` and `--hypothesis` forwarded when present**: both flags pass through to S5 verbatim; sweep never strips them. `--journal` requires `--researcher` or `--architect` (validated at run R2). `--hypothesis <path>` preloads hypothesis queue.
- **`--team` and interactivity**: sweep non-interactive except when `--team` active. Team mode Phase B presents user confirmation gate before Phase C — sweep pauses and waits. Expected; sweep cannot bypass Phase B gate. In automated/CI contexts where interaction impossible, avoid `--team` flag or pre-confirm via gate prompt manually; no `--auto` flag to suppress Phase B — by design (Phase B reviews potentially risky parallel agent decisions).
- **`--skip-validation`**: passes through to judge step (S3). Useful for cross-machine workflows where metric/guard commands run only on target machine.
- **Metric direction conventions** (S2 auto-config): `direction: lower` for loss/error/latency metrics (loss, error_rate, mse, mae, latency, time); `direction: higher` for quality metrics (accuracy, f1, precision, recall, auc, throughput). Goal string ambiguous → default `direction: lower`, note assumption in config comment.

</notes>
