<!-- file: team-mode.md — consumers: plugins/cc_develop/skills/fix/SKILL.md (## Team Mode Branch, gated on TEAM_MODE=true) -->

# Fix — Team Mode Protocol

Loaded only when `TEAM_MODE=true`. Execute team workflow now — do not proceed to Step 1.

Root cause unclear after initial triage, OR bug spans 3+ modules and user accepted "Proceed anyway" at scope gate: use this path.

**Coordination:**

**Note on `model=` assignments**: `model=opus` in prompts below is an advisory hint — effective only when actual foundry agents installed. Falling back to `general-purpose` (foundry absent): prompt-prepend `model=` doesn't reliably override agent-resolution fallback tier; effective model set by `agent-resolution.md`'s fallback table, not spawn prompt.

1. Lead broadcasts current evidence: `{bug: <description>, traceback: <key lines>}`
2. Spawn **foundry:sw-engineer x 2 (model=opus)** — each investigates a distinct root-cause hypothesis (A, B) independently. `export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"; IFS= read -r _DEV_SHARED < "${TMPDIR:-/tmp}/dev-shared-${CSID}" 2>/dev/null || _DEV_SHARED=""; [ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"; cat "$_DEV_SHARED/preflight-helpers.md"` §Team Spawn Template — replace `[ROLE_PHRASE]` with `[bug description]`, `[FILE_SLUG]` with `fix-hypothesis`. Third independent investigation wanted → re-invoke with a narrower hypothesis spec, don't auto-scale here.
3. Each teammate investigates independently — claims hypothesis; returns full output to file (file-based handoff protocol).
4. Lead facilitates cross-challenge between competing analyses.
5. Lead synthesizes consensus root cause, proceeds with Steps 2-4 (regression test, fix, review loop) alone.

Compute run directory and create health sentinel:

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_setup_worktree_wrap.py" fix
IFS= read -r TS < "${TMPDIR:-/tmp}/dev-fix-team-ts-${CSID}" 2>/dev/null || TS=""
# no EXIT trap here — fires when THIS Bash call's shell exits (immediately), deleting the sentinel setup_worktree.py just created, breaking every later health poll; cleanup in completion block below
```

> **Agent budget** — each teammate costs ~120,851 tok fixed overhead (~73 tool-calls' worth) plus ~12.0 s/call, so work under ~73 calls is cheaper done inline: spawn nothing. Keep each teammate near ~55 tool-calls; past ~60 they stall without returning an envelope, forcing reconstruction from disk. Every spawn prompt requires an envelope even on exhaustion — `partial: true` plus what was finished.

Spawn 2 teammates in parallel using Agent() tool:

**IMPORTANT**: before building each spawn prompt below, resolve all shell variables to literal values — embed resolved literals, not variable references, in prompt strings. `<TS_LITERAL>`, `<_DEV_SHARED_LITERAL>`, `<ARGUMENTS_LITERAL>` below are placeholders — substitute actual computed values before constructing Agent call; spawned agent cannot expand shell variables from its parent context:

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r TS < "${TMPDIR:-/tmp}/dev-fix-team-ts-${CSID}" 2>/dev/null || TS=""                                 # re-derive — bash resets between calls
IFS= read -r _DEV_SHARED < "${TMPDIR:-/tmp}/dev-shared-${CSID}" 2>/dev/null || _DEV_SHARED=""
[ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"
_SPAWN_DEV_SHARED="$_DEV_SHARED"
_SPAWN_TS="$TS"
_SPAWN_ARGS="$ARGUMENTS"
IFS= read -r _SPAWN_RUN_DIR < "${TMPDIR:-/tmp}/dev-fix-run-dir-${CSID}" 2>/dev/null || _SPAWN_RUN_DIR=".temp/develop/$TS"
touch "${TMPDIR:-/tmp}/fix-team-check-$TS"  # re-arm health sentinel right before spawn (tmpdir-exempt: matches setup_worktree.py's _sentinel_dir(); $TS run-timestamp provides uniqueness in place of a CSID suffix)
```

**Teammate 1 — foundry:sw-engineer (model=opus) — hypothesis A**: substitute `$_SPAWN_DEV_SHARED`, `$_SPAWN_TS`, `$_SPAWN_ARGS`, and `$_SPAWN_RUN_DIR` with resolved literals before constructing prompt: "You are a foundry:sw-engineer teammate investigating a bug fix. Read ${HOME}/.claude/TEAM_PROTOCOL.md — use AgentSpeak v2. Read `<_DEV_SHARED_LITERAL>/preflight-helpers.md` §Team Spawn Template. Bug: \<ARGUMENTS_LITERAL>. Evidence: {bug: <description>, traceback: <key lines>}. Your task: investigate hypothesis A — claim one distinct root-cause hypothesis, gather evidence, propose fix approach. Task tracking: do NOT call TaskCreate or TaskUpdate — lead owns all task state. Signal completion: 'Status: complete | blocked — <reason>'. Write full analysis to \<RUN_DIR_LITERAL>/fix-hypothesis-A-\<TS_LITERAL>.md using Write tool. Return ONLY: {"status":"done","file":"<path>","hypothesis":"<one-line>","confidence":0.N}"

**Teammate 2 — foundry:sw-engineer (model=opus) — hypothesis B**: substitute `$_SPAWN_DEV_SHARED`, `$_SPAWN_TS`, `$_SPAWN_ARGS`, and `$_SPAWN_RUN_DIR` with resolved literals before constructing prompt: "You are a foundry:sw-engineer teammate investigating a bug fix. Read ${HOME}/.claude/TEAM_PROTOCOL.md — use AgentSpeak v2. Read `<_DEV_SHARED_LITERAL>/preflight-helpers.md` §Team Spawn Template. Bug: \<ARGUMENTS_LITERAL>. Evidence: {bug: <description>, traceback: <key lines>}. Your task: investigate hypothesis B — claim a DIFFERENT root-cause hypothesis from your teammates, gather evidence, propose fix approach. Task tracking: do NOT call TaskCreate or TaskUpdate — lead owns all task state. Signal completion: 'Status: complete | blocked — <reason>'. Write full analysis to \<RUN_DIR_LITERAL>/fix-hypothesis-B-\<TS_LITERAL>.md using Write tool. Return ONLY: {"status":"done","file":"<path>","hypothesis":"<one-line>","confidence":0.N}"

Health monitoring (CLAUDE.md §6): re-derive `$TS` and `$RUN_DIR` at block start (bash resets between calls — read back from temp files spawn block persisted):

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r TS < "${TMPDIR:-/tmp}/dev-fix-team-ts-${CSID}" 2>/dev/null || TS=""
[ -n "$TS" ] || TS=$(date -u +%Y-%m-%dT%H-%M-%SZ)
IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/dev-fix-run-dir-${CSID}" 2>/dev/null || RUN_DIR=".temp/develop/$TS"
```

Every 5 min: `find $RUN_DIR -newer ${TMPDIR:-/tmp}/fix-team-check-$TS -name "fix-hypothesis-*.md" | wc -l` (sentinel dir resolved by setup_worktree.py's `_sentinel_dir()` — TMPDIR when set, else system temp dir; matches this expression) — new files = alive; zero = stalled. Hard cutoff: 15 min no file activity → timed out. One extension (+5 min) if `tail -20` of output file explains delay; second unexplained stall = hard cutoff. On timeout: read `tail -100` of each `$RUN_DIR/fix-hypothesis-*.md`; surface with ⏱; never omit.

After both teammates complete: read output files from `$RUN_DIR/`, synthesize consensus root cause, facilitate cross-challenge between competing analyses. Lead then proceeds alone with Steps 2-4 (regression test, fix, review loop). Clean up health sentinel (exact filename, never a glob — another session's sentinel may share the base name):

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r TS < "${TMPDIR:-/tmp}/dev-fix-team-ts-${CSID}" 2>/dev/null || TS=""
[ -n "$TS" ] && rm -f "${TMPDIR:-/tmp}/fix-team-check-$TS"
```
