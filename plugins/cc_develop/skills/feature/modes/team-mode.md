<!-- file: team-mode.md — consumers: plugins/cc_develop/skills/feature/SKILL.md (## Team Mode Branch, gated on TEAM_MODE=true) -->

# Feature — Team Mode Protocol

Loaded only when `TEAM_MODE=true`. Runs Step 1 inline (teammates need scope context), then spawns teammates for Steps 2-4 in two waves. After synthesis run only the closing blocks named at the end of this file, then exit — do not continue to solo Steps 1-4.

Team-protocol guard, shared by every develop team mode — run first, before Step 1 or any run-dir: printed `! BLOCKED` → stop the skill and relay that line as the final reply; never fall back to solo, never spawn teammates told to read a missing file.

```bash
# timeout: 5000
[ -f ~/.claude/TEAM_PROTOCOL.md ] || { echo "! BLOCKED — --team needs ~/.claude/TEAM_PROTOCOL.md, written by /foundry:setup (requires foundry plugin); run /foundry:setup, or re-run without --team"; exit 1; }
echo "✓ TEAM_PROTOCOL.md present"
```

Run Step 1 scope analysis inline (same as solo Step 1, including its Source Verification subsection) — teammates need orientation context. Then run feature/SKILL.md §Challenger gate against that analysis before any teammate spawn; its two-state decision applies unchanged (`--no-challenge` skips; otherwise always run, `--challenge` is a no-op). Blockers found → STOP; Write the blocker findings (one line each: claim · file:line · evidence) to `$DEV_DIR/blockers.md` first; print them once as the last reply text before invoking `AskUserQuestion`, as normal Markdown. The question text carries the blocker count, headline and `→ $DEV_DIR/blockers.md`, at most 6 plain lines (target 600 characters, hard cap 1,500), no table, pipe layout or code fence. Options carry a label and a one-line description, no `preview` — "Challenger raised N blocker(s) on the implementation approach. How to proceed?" · (a) **Revise scope** — return to Step 1 analysis with the blockers as input · (b) **Accept risk** — proceed to Wave 1 with each blocker documented in the Final Report Follow-up · (c) **Abort**. On Abort: stop. Never proceed to Wave 1 on prose alone. After Step 1, broadcast to teammates: `{feature: <desc>, scope: <modules>, API: <proposed signature>}`.

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_load_shared.py" preflight-helpers.md
```

§Team Spawn Template to get spawn prompt template. Replace `[ROLE_PHRASE]` with feature description, `[FILE_SLUG]` with `feature`. Drop the template's `Your hypothesis: …` line — writer teammates investigate no root cause.

Compute run directory:

```bash
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_setup_worktree_wrap.py" feature  # timeout: 5000
```

**IMPORTANT**: in spawn prompts below, substitute `$_SPAWN_TS` and `$_SPAWN_TEAM_DIR` with actual computed values from bash block above — literal resolved strings, not shell variable references. Bare `$TS`/`$TEAM_DIR` inside a quoted Agent prompt string will NOT expand; spawned agent receives literal dollar-sign text — path mismatches, false health-monitoring timeouts.

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r TS < "${TMPDIR:-/tmp}/dev-feature-team-ts-${CSID}" 2>/dev/null || TS=""        # re-derive — bash resets between calls
[ -n "$TS" ] || { echo "! dev-feature-team-ts missing — cannot build teammate output paths; aborting team mode"; exit 1; }
IFS= read -r TEAM_DIR < "${TMPDIR:-/tmp}/dev-feature-team-dir-${CSID}" 2>/dev/null || TEAM_DIR=""
_SPAWN_TS="$TS"
_SPAWN_TEAM_DIR="$TEAM_DIR"
```

Use `$_SPAWN_TS` (resolved to literal before prompt construction) inside spawn prompt strings — never bare `$TS`.

> **Agent budget** — each teammate costs ~120,851 tok fixed overhead (~73 tool-calls' worth) plus ~12.0 s/call, so work under ~73 calls is cheaper done inline: spawn nothing — work-displacement only; an isolation-motivated spawn (adversarial reviewer, distinct specialist role, model tier, worktree) runs regardless of size. Keep each teammate near ~55 tool-calls; past ~60 they stall without returning an envelope, forcing reconstruction from disk. Every spawn prompt requires an envelope even on exhaustion — `partial: true` plus what was finished.

Spawn teammates in **two serialized waves** — qa-specialist and doc-scribe cannot meaningfully audit/document an implementation that doesn't exist yet; parallel with sw-engineer produces tests against guessed APIs and docs of placeholder structure:

- **Wave 1 — foundry:sw-engineer alone**: spawn Teammate 1 (sw-engineer), end the turn, resume on its completion notification (`Status: complete`).
- **Wave 2 — foundry:qa-specialist + foundry:doc-scribe in parallel**: after Wave 1 returns, spawn Teammates 2 and 3 together. Both receive actual implementation file path from Wave 1's output as input context (resolved via `.temp/develop/$_SPAWN_TS/feature-sw-engineer-$_SPAWN_TS.md`).

<!-- loads: team-spawn-prompts.md -->

Spawn prompts: load full prompt text per teammate via `cat` (not the Read tool — `Bash(cat:*)` grant is version-proof):

```bash
cat "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/skills/feature/templates/team-spawn-prompts.md"  # timeout: 5000
```

Summary below:

- **Teammate 1 — foundry:sw-engineer (model=opus)**: implement feature (Steps 2-3: demo test, TDD loop); edit source only, not `tests/`; write to `.temp/develop/$_SPAWN_TS/feature-sw-engineer-$_SPAWN_TS.md`; return compact JSON. Append to this teammate's spawn prompt: capture the Step 2 demo's failing exit code before implementing, report it in the output file as `"demo_red_exit": N` and in the return envelope as `demo_red_exit:N`; demo already passes pre-implementation → return `status: blocked` instead of proceeding.
- **Teammate 2 — foundry:qa-specialist (model=sonnet)**: add edge-case/regression/security tests; edit `tests/` only, not source; write to `.temp/develop/$_SPAWN_TS/feature-qa-specialist-$_SPAWN_TS.md`; return compact JSON.
- **Teammate 3 — foundry:doc-scribe (model=sonnet)**: prepare docstrings and README only (no CHANGELOG); write to `.temp/develop/$_SPAWN_TS/feature-doc-scribe-$_SPAWN_TS.md`; return compact JSON.

**Note on `model=` assignments**: `model=opus`/`model=sonnet` labels above are advisory hints — effective only when actual foundry agents installed. Falling back to `general-purpose` (foundry absent): prompt-prepend `model=` doesn't reliably override agent-resolution fallback tier; effective model set by `agent-resolution.md`'s fallback table, not spawn prompt. Intentional — sonnet sufficient for qa-specialist/doc-scribe, opus for sw-engineer; on fallback, expect tier degradation noted in Final Report.

**Wave 1 output gate** — verify sw-engineer wrote expected file before launching Wave 2:

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r TS < "${TMPDIR:-/tmp}/dev-feature-team-ts-${CSID}" 2>/dev/null || TS=""
[ -n "$TS" ] || { echo "! dev-feature-team-ts missing — cannot verify Wave 1 output; aborting team mode"; exit 1; }
WAVE1_FILE=".temp/develop/$TS/feature-sw-engineer-$TS.md"
if [ ! -f "$WAVE1_FILE" ]; then
    echo "! Wave 1 output missing: $WAVE1_FILE — sw-engineer did not write expected file"
    echo "! Cannot proceed to Wave 2 without implementation. Aborting."
    exit 1
fi
echo "✓ Wave 1 output verified: $WAVE1_FILE"
grep -q '"demo_red_exit"[[:space:]]*:[[:space:]]*[1-9]' "$WAVE1_FILE" || { echo "! Wave 1 gate: demo_red_exit missing or zero — demo passed pre-implementation, or sw-engineer returned status: blocked. Aborting Wave 2."; exit 1; }
```

**Coordination order**: QA is spawned only in Wave 2, so it never challenges SW's API design before implementation — the Challenger gate above covers the approach. QA receives SW's implementation file path from Wave 1's output so tests stay accurate.

Health monitoring (CLAUDE.md §6): re-derive `$TS` at block start (bash resets between calls — read back from temp file the spawn block persisted):

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r TS < "${TMPDIR:-/tmp}/dev-feature-team-ts-${CSID}" 2>/dev/null || TS=""
[ -n "$TS" ] || { echo "! dev-feature-team-ts missing — cannot locate team run dir; aborting team mode"; exit 1; }
```

Every spawn is background: spawn, end the turn, resume on the harness completion notification — never a fixed-interval poll, never a liveness probe (`find -newer` or similar). Arm batch `team` in each spawn response (deliverables `.temp/develop/$TS/feature-<role>-$TS.md`; `agent-resolution.md` §Agent waits — no polling, per-agent deadlines, 1800 s) and run the §Agent waits check block (`agent_watch.py`) once per wake-up. On timeout (`timed_out` row, or a notification without its deliverable): read `tail -100` of the stalled file; surface with ⏱; never omit timed-out teammates.

**Path verification** — after Wave 2 completes (all three teammates spawned), verify agents got correct paths — check expected output files exist. Re-read `$TS` from temp file (bash resets between calls — spawn block persisted it). Runs only once every teammate spawned; earlier would false-flag "missing" for Wave-2 agents not yet existing:

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r TS < "${TMPDIR:-/tmp}/dev-feature-team-ts-${CSID}" 2>/dev/null || TS=""
[ -n "$TS" ] || { echo "! dev-feature-team-ts missing — cannot verify teammate output paths; aborting team mode"; exit 1; }
for agent in sw-engineer qa-specialist doc-scribe; do
    expected=".temp/develop/$TS/feature-${agent}-$TS.md"
    [ -f "$expected" ] && echo "✓ $agent wrote $expected" || echo "⚠ $agent missing expected output $expected"
done
```

After all teammates complete: read output files from `.temp/develop/$TS/`, synthesize, then run only these solo blocks, in order:

1. CHANGELOG entry — the lead inserts it under `Unreleased` with the Edit tool, as in Step 5 (doc-scribe wrote none).
2. Step 5's `foundry--quality-stack.md` load block and the steps it lists (Branch Safety Guard, Quality Stack, Codex Pre-pass, Progressive Review Loop, Codex Mechanical Delegation) — the Progressive Review Loop is team mode's review; the Step 4 review loop does not run.
3. Step 5's Final gate rerun block (full suite with `$TEST_CMD`) — the quality stack's wide run is its first full-suite run only when `$TEST_CMD` is that same pytest invocation (else run the block once); rerun after any later code change, max 3 gate iterations.

Then the solo `## Final Report` section in full (plan drift check, report template, worktree exit, contract cleanup). Never report the suite green before block 3 passes. Exit after the Final Report — do not continue to solo Steps 1-4 or Step 5's doc-scribe spawn.
