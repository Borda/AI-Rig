# Skill Checks — Repo-Wide Sweeps (C35, 42, 43, 44)

> Scan-root convention for every bash block below: `checks-skills.md` §Scan-root derivation.

## Check C35 — Background agent health monitoring compliance (CLAUDE.md §6)

CLAUDE.md §6 requires every skill spawning agents to implement: (1) a per-agent deadline armed in the spawn response (`agent-watch-<batch>.tsv`), (2) completion-notification handling with one `agent_watch.py` check per wake-up, (3) immediate ⏱ `timed_out` for an agent whose notification arrives without its deliverable — and to use no waiting tool (`ScheduleWakeup`, `ListAgents`, `Monitor`) and no fixed-interval poll. Absence = stalled agents silently drop findings, or the user is left typing "check on the agents".

**Step 1 — Find skills that spawn agents**. Every spawn is a background spawn now: `Agent()` never blocks and there is no `run_in_background` parameter, so matching that dead token would make this check silently N/A everywhere.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
[ "$LOCAL_MODE" = "true" ] && _ROOT="plugins" || _ROOT=".claude"
printf "=== Check C35: Agent health monitoring ===\n"
mkdir -p "${TMPDIR:-/tmp}/audit-state-${CSID}"
find "$_ROOT" -path "*skills/*/SKILL.md" -exec grep -l 'Agent(subagent_type' {} + 2>/dev/null |
  sort > "${TMPDIR:-/tmp}/audit-state-${CSID}/c35-bg-skills"
if [ ! -s "${TMPDIR:-/tmp}/audit-state-${CSID}/c35-bg-skills" ]; then
    printf "✓: No agent spawns found — C35 N/A\n"
else
    cat "${TMPDIR:-/tmp}/audit-state-${CSID}/c35-bg-skills"
fi  # timeout: 5000
```

**Step 2 — For each skill found, verify deadline-protocol elements**:

Step 1's list is re-read from the state file — a variable set in Step 1's block is gone by the time this block runs (fresh shell per Bash call). A waiting tool is flagged even in a skill that references `agent-spawn-protocol.md` — the reference proves intent, not compliance. A line that names a waiting tool only to forbid it (`never`, `no `, `not `) is not a finding.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
while IFS= read -r f; do  # timeout: 5000
    [ -f "$f" ] || continue
    skill=$(basename "$(dirname "$f")")
    # grep -c prints 0 AND exits 1 on no match — || echo 0 double-fires, "0\n0" breaks numeric tests below
    has_wait_tool=$(grep -E 'ScheduleWakeup|ListAgents|Monitor\(|`Monitor`' "$f" 2>/dev/null | grep -Evc '[Nn]ever|[Nn]o |not |forbid|ban') || has_wait_tool=0
    has_interval=$(grep -c 'MONITOR_INTERVAL\|every 5 min\|poll every' "$f" 2>/dev/null) || has_interval=0
    [ "$has_wait_tool" -gt 0 ] && printf "⚠ C35e: %s — waits on agents with ScheduleWakeup/ListAgents/Monitor (CLAUDE.md §6)\n" "$skill"  # wait-check: allow — detector output
    [ "$has_interval" -gt 0 ] && printf "⚠ C35d: %s — prescribes a fixed-interval poll; spawns are background, the turn ends and resumes on notification\n" "$skill"
    if grep -q 'agent-spawn-protocol' "$f" 2>/dev/null; then
        printf "✓ C35: %s — references agent-spawn-protocol.md\n" "$skill"
        continue
    fi
    has_deadline=$(grep -c 'agent-watch\|agent_watch' "$f" 2>/dev/null) || has_deadline=0
    has_notify=$(grep -c 'completion notification\|idle notification' "$f" 2>/dev/null) || has_notify=0
    has_timeout=$(grep -c 'timed.out' "$f" 2>/dev/null) || has_timeout=0
    [ "$has_deadline" -eq 0 ] && printf "⚠ C35a: %s — no per-agent deadline (agent-watch-<batch>.tsv + agent_watch.py) (CLAUDE.md §6)\n" "$skill"
    [ "$has_notify" -eq 0 ]   && printf "⚠ C35b: %s — no completion-notification handling (CLAUDE.md §6)\n" "$skill"
    [ "$has_timeout" -eq 0 ]  && printf "⚠ C35c: %s — no timed_out handling (CLAUDE.md §6)\n" "$skill"
done < "${TMPDIR:-/tmp}/audit-state-${CSID}/c35-bg-skills"
```

Severity: **high** for C35a/b/c/e — stalled agents drop findings with no user-visible signal, or waiting-tool turns burn the whole context per wake-up. **medium** for C35d — a dead interval wastes turns rather than losing findings. Fix: reference `$_FOUNDRY_SHARED/agent-spawn-protocol.md` (preferred) or inline the elements in the skill.

| Sub-check | Pattern | Severity | Auto-fix |
| -- | -- | -- | -- |
| C35a — no per-agent deadline | no `agent-watch-<batch>.tsv` / `agent_watch.py` | high | no |
| C35b — no notification handling | no completion/idle-notification handling | high | no |
| C35c — no timeout handling | no `timed_out` path | high | no |
| C35d — fixed-interval poll | `MONITOR_INTERVAL`, "every 5 min", "poll every" — no clock exists to run it | medium | no |
| C35e — waiting tool | `ScheduleWakeup`, `ListAgents` or `Monitor` prescribed to wait on an agent | high | no |

## Check 43 — Shell variable persistence across Bash calls

Variables assigned in one ```` ```bash ```` block are NOT available in later bash blocks — each Bash tool call runs in a fresh shell. Referencing a variable from a prior block silently expands to empty string, corrupting commands, paths, and conditional guards without any error.

```bash
printf "=== Check 43: Shell variable persistence across Bash calls ===\n"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/check_bash_persistence.py" --scan-dir .  # timeout: 15000
```

**Severity**: critical — silent empty-string expansion; corrupts file paths, conditional branches, spawn prompts. No error surfaced at runtime.

Fix: re-assign the variable at the top of every bash block that needs it, or combine dependent commands into a single bash block. Do NOT rely on variable state persisting between bash tool calls.

| Sub-check | Pattern | Severity | Auto-fix |
| -- | -- | -- | -- |
| 43 — cross-block ref | `$VAR` in block N where VAR assigned only in block M\<N | critical | no — requires combining blocks or re-assigning |

## Check 42 — Unexpanded variables in agent spawn prompts

Variables written as `$VAR` or `${VAR}` inside ```` ```markdown ```` fenced blocks (spawn prompt templates) are passed literally to the spawned agent — the agent receives the dollar-sign string, not the resolved value. The agent can't resolve orchestrator shell variables; `$_FOUNDRY_SHARED/foo.md` becomes the literal path string `$_FOUNDRY_SHARED/foo.md` and the Read tool fails silently.

```bash
printf "=== Check 42: Unexpanded variables in spawn prompt markdown blocks ===\n"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/check_spawn_prompt_vars.py" --scan-dir .  # timeout: 10000
```

**Severity**: critical — spawn prompt contains unresolved path or name; spawned agent reads wrong file or fails silently with no diagnostic.

Fix: resolve the variable in a preceding bash block and substitute the resolved value inline into the spawn prompt string. For paths: use `$(cat /tmp/resolved-path)` or embed the bash-resolved value as a literal string in the prompt text. Do NOT pass `$VAR` directly inside a markdown spawn prompt unless the caller explicitly substitutes it before dispatch.

| Sub-check | Pattern | Severity | Auto-fix |
| -- | -- | -- | -- |
| 42 — unexpanded spawn var | `$VAR` inside ```` ```markdown ```` block | critical | no — requires resolving value before spawn |

## Check 44 — TMPDIR sentinel session scoping

`/tmp` is machine-global. Every `${TMPDIR:-/tmp}/<name>` sentinel must carry the session token — `-${CSID}` (bash), `-{_CSID}`/`_csid()` (python), or the inline `${CLAUDE_CODE_SESSION_ID:-$PPID}` form (used where a preceding `export` line would displace the first-token permission match, e.g. `git`/`cd` blocks) — or a trailing `# tmpdir-exempt: <reason>` marker (valid reasons: `mktemp`, `git-hook-boundary`, `user-shell-boundary`). Bare names collide across concurrent sessions/projects. Canonical pattern: `rules/claude-config.md` §TMPDIR Sentinel Scoping.

```bash
printf "=== Check 44: TMPDIR sentinel session scoping ===\n"
_C44_PAT='TMPDIR:-/tmp}/'  # tmpdir-exempt: lint-self-reference — pattern literal, not a sentinel
grep -rn "$_C44_PAT" . --include='*.md' --include='*.py' --include='*.sh' 2>/dev/null \
  | grep -v 'CSID' | grep -v '_csid' | grep -v 'CLAUDE_CODE_SESSION_ID' | grep -v 'tmpdir-exempt' \
  | grep -v 'rules/claude-config.md' \
  | while IFS= read -r line; do printf "C44-HIGH: unscoped TMPDIR sentinel: %s\n" "$line"; done  # timeout: 10000
```

**Severity**: high — silent cross-session/cross-project state bleed; one session reads another's run-dir, flags, or checkpoints with no error surfaced.

Fix: apply `rules/claude-config.md` §TMPDIR Sentinel Scoping (export `CSID` first line of the block, terminal `-${CSID}` suffix on the sentinel filename); genuinely out-of-session sentinels (git hooks, user-shell-created auth files, mktemp templates) get the `# tmpdir-exempt: <reason>` marker instead.

| Sub-check | Pattern | Severity | Auto-fix |
| -- | -- | -- | -- |
| 44 — unscoped sentinel | `TMPDIR:-/tmp}/` line without session token or `tmpdir-exempt` marker | high | yes — mechanical suffix per claude-config rule |
