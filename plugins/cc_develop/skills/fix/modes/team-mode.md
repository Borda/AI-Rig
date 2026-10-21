<!-- file: team-mode.md — consumers: plugins/cc_develop/skills/fix/SKILL.md (## Team Mode Branch, gated on TEAM_MODE=true) -->

# Fix — Team Mode Protocol

Loaded only when `TEAM_MODE=true`. Execute team workflow now — do not proceed to Step 1.

Pass `--team` when you already expect an unclear root cause, or a bug spanning 3+ modules. This branch runs **before** triage and the scope gate, so these are your expectations at invocation time, not conditions the skill evaluates.

**Coordination:**

**Note on `model=` assignments**: `model=opus` in prompts below is an advisory hint — effective only when actual foundry agents installed. Falling back to `general-purpose` (foundry absent): prompt-prepend `model=` doesn't reliably override agent-resolution fallback tier; effective model set by `agent-resolution.md`'s fallback table, not spawn prompt.

1. Lead broadcasts current evidence: `{bug: <description>, traceback: <key lines>}`
2. Spawn **foundry:sw-engineer x 2 (model=opus)** — each investigates a distinct root-cause hypothesis (A, B) independently. `export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"; python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_load_shared.py" preflight-helpers.md` §Team Spawn Template — replace `[ROLE_PHRASE]` with `[bug description]`, `[FILE_SLUG]` with `fix-hypothesis`. Third independent investigation wanted → re-invoke with a narrower hypothesis spec, don't auto-scale here.
3. Each teammate investigates independently and read-only — claims hypothesis; returns full output to file (file-based handoff protocol). Both run in parallel in this working tree, so each prompt carries the `Read-only investigation` line: no tracked-file edits, no `git stash`/`checkout`/`reset`/`restore`, probes in a `mktemp -d` copy or under the run directory.
4. Lead cross-challenges the competing analyses inline — no teammate resume, no new spawn: check each hypothesis's claims against the other teammate's evidence file and note which evidence supports, contradicts, or is left unexplained by it.
5. Lead synthesizes consensus root cause, then re-enters the main workflow at §Premise Grounding Gate — premise grounding, inline plan generation, and the ## Challenger gate against the consensus root cause — before Steps 2-4 (regression test, fix, review loop) alone.

Compute run directory — its first line is the team-protocol guard shared by every develop team mode: printed `! BLOCKED` → stop the skill and relay that line as the final reply; never fall back to solo, never spawn teammates told to read a missing file:

```bash
# timeout: 5000
[ -f ~/.claude/TEAM_PROTOCOL.md ] || { echo "! BLOCKED — --team needs ~/.claude/TEAM_PROTOCOL.md, written by /foundry:setup (requires foundry plugin); run /foundry:setup, or re-run without --team"; exit 1; }
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_setup_worktree_wrap.py" fix
IFS= read -r TS < "${TMPDIR:-/tmp}/dev-fix-team-ts-${CSID}" 2>/dev/null || TS=""
```

> **Agent budget** — each teammate costs ~120,851 tok fixed overhead (~73 tool-calls' worth) plus ~12.0 s/call, so work under ~73 calls is cheaper done inline: spawn nothing — work-displacement only; an isolation-motivated spawn (adversarial reviewer, distinct specialist role, model tier, worktree) runs regardless of size. Keep each teammate near ~55 tool-calls; past ~60 they stall without returning an envelope, forcing reconstruction from disk. Every spawn prompt requires an envelope even on exhaustion — `partial: true` plus what was finished.

Spawn 2 teammates in parallel using Agent() tool:

**IMPORTANT**: before building each spawn prompt below, resolve all shell variables to literal values — embed resolved literals, not variable references, in prompt strings. `<TS_LITERAL>`, `<RUN_DIR_LITERAL>`, `<ARGUMENTS_LITERAL>` below are placeholders — substitute actual computed values before constructing Agent call; spawned agent cannot expand shell variables from its parent context:

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r TS < "${TMPDIR:-/tmp}/dev-fix-team-ts-${CSID}" 2>/dev/null || TS=""                                 # re-derive — bash resets between calls
[ -n "$TS" ] || { echo "! dev-fix-team-ts missing — cannot build teammate output paths; aborting team mode"; exit 1; }
_SPAWN_TS="$TS"
_SPAWN_ARGS="$ARGUMENTS"
IFS= read -r _SPAWN_RUN_DIR < "${TMPDIR:-/tmp}/dev-fix-run-dir-${CSID}" 2>/dev/null || _SPAWN_RUN_DIR=".temp/develop/$TS"
```

Each `Agent()` call sets `name` and `description` (slots below) and opens prompt line 1 with the teammate's delta; the shared bug and boilerplate follow line 1.

**Teammate 1 — foundry:sw-engineer (model=opus) — hypothesis A**: substitute `$_SPAWN_TS`, `$_SPAWN_ARGS`, and `$_SPAWN_RUN_DIR` with resolved literals before constructing prompt. Slots: `name="fix-hypothesis-a"` · `description="root-cause hypothesis A"`

"Investigate root-cause hypothesis A.

You are a foundry:sw-engineer teammate investigating a bug fix. Read ~/.claude/TEAM_PROTOCOL.md — use AgentSpeak v2. Bug: \<ARGUMENTS_LITERAL>. Evidence: {bug: <description>, traceback: <key lines>}. Your task: claim one distinct root-cause hypothesis, gather evidence, propose fix approach. Read-only investigation: never edit, create or delete tracked files and never run `git stash`, `git checkout`, `git reset` or `git restore` — sibling investigators share this working tree; run reproduction probes in a `mktemp -d` copy or under the run directory; write only your analysis file. Task tracking: do NOT call TaskCreate or TaskUpdate — lead owns all task state. Signal completion: 'Status: complete | blocked — <reason>'. Write full analysis to \<RUN_DIR_LITERAL>/fix-hypothesis-A-\<TS_LITERAL>.md using Write tool. Return ONLY: {"status":"done","file":"<path>","hypothesis":"<one-line>","confidence":0.N}"

**Teammate 2 — foundry:sw-engineer (model=opus) — hypothesis B**: substitute `$_SPAWN_TS`, `$_SPAWN_ARGS`, and `$_SPAWN_RUN_DIR` with resolved literals before constructing prompt. Slots: `name="fix-hypothesis-b"` · `description="root-cause hypothesis B"`

"Investigate root-cause hypothesis B.

You are a foundry:sw-engineer teammate investigating a bug fix. Read ~/.claude/TEAM_PROTOCOL.md — use AgentSpeak v2. Bug: \<ARGUMENTS_LITERAL>. Evidence: {bug: <description>, traceback: <key lines>}. Your task: claim a DIFFERENT root-cause hypothesis from your teammates, gather evidence, propose fix approach. Read-only investigation: never edit, create or delete tracked files and never run `git stash`, `git checkout`, `git reset` or `git restore` — sibling investigators share this working tree; run reproduction probes in a `mktemp -d` copy or under the run directory; write only your analysis file. Task tracking: do NOT call TaskCreate or TaskUpdate — lead owns all task state. Signal completion: 'Status: complete | blocked — <reason>'. Write full analysis to \<RUN_DIR_LITERAL>/fix-hypothesis-B-\<TS_LITERAL>.md using Write tool. Return ONLY: {"status":"done","file":"<path>","hypothesis":"<one-line>","confidence":0.N}"

Health monitoring (CLAUDE.md §6): re-derive `$TS` and `$RUN_DIR` at block start (bash resets between calls — read back from temp files spawn block persisted):

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r TS < "${TMPDIR:-/tmp}/dev-fix-team-ts-${CSID}" 2>/dev/null || TS=""
[ -n "$TS" ] || { echo "! dev-fix-team-ts missing — cannot locate team run dir; aborting team mode"; exit 1; }
IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/dev-fix-run-dir-${CSID}" 2>/dev/null || RUN_DIR=".temp/develop/$TS"
```

Arm batch `team` in the spawn response (deliverables `$RUN_DIR/fix-hypothesis-A-$TS.md`, `-B-$TS.md`; `agent-resolution.md` §Agent waits — no polling, per-agent deadlines). Spawn both teammates, **end the turn**, resume on each completion notification and run the §Agent waits check block (`agent_watch.py`) once — never a no-op call, a "waiting" line, or a sleep. No liveness probe (`find -newer` or similar) — the armed deadline (1800 s) is the only liveness mechanism; a `timed_out` row, or a notification without its deliverable, means timed out. On timeout: read `tail -100` of each `$RUN_DIR/fix-hypothesis-*.md`; surface with ⏱; never omit.

After both teammates complete: read output files from `$RUN_DIR/`, synthesize consensus root cause, weighing the inline cross-challenge (Coordination step 4) — never a teammate resume or new spawn. Lead then re-enters at §Premise Grounding Gate (premise grounding → plan-inline → ## Challenger gate on the consensus root cause), then proceeds alone with Steps 2-4.
