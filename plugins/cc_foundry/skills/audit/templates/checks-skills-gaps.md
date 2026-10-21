# Skill Checks — Gaps (22, 40)

> Scan-root convention for every bash block below: `checks-skills.md` §Scan-root derivation.

## Check 22 — Calibration coverage gap

**Step 1 — Read calibrate domain table**: Load calibrate `skills.md` via `cat` (not the Read tool — `Bash(cat:*)` grant is version-proof), extract registered target list under `### Domain table`. Build registered-targets set.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/load_mode.py" calibrate modes skills.md $([ "$LOCAL_MODE" = "true" ] && echo --local) --fallback-source  # timeout: 5000
```

**Step 2 — Scan all skill modes on disk**: Use Glob (`skills/*/SKILL.md`, path `.claude/`) and Glob (`skills/*/modes/*.md`, path `.claude/`) to enumerate every skill and mode file. Extract mode names from `argument-hint:` frontmatter and `## Mode:` / `### Mode:` headings.

**Step 3 — Validate registered targets exist on disk**: For each registered target, verify matching skill/mode file exists. Registered target with no matching file → **medium** (calibrate fails at runtime).

**Step 4 — Identify unregistered calibratable candidates** (model reasoning):

Mode is calibratable when ALL three signals present:

1. **Deterministic structured output**: findings list, completeness checklist, structured table, or machine-readable verdict
2. **Synthetic input feasible**: testable without external services
3. **Ground truth constructable**: known issues injectable and scorable

→ Unregistered mode matching all three: **low** (add to `calibrate/modes/skills.md` domain table)

**Step 5 — Read agents domain table**: Load calibrate `agents.md` via `cat` (not the Read tool — `Bash(cat:*)` grant is version-proof), extract all agent names from `### Domain table`. Build registered-agent-names set.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/load_mode.py" calibrate modes agents.md $([ "$LOCAL_MODE" = "true" ] && echo --local) --fallback-source  # timeout: 5000
```

**Step 6 — Scan all agent files on disk**: (When `LOCAL_MODE=false`, skip `plugins/*/agents/*.md` — scan `.claude/agents/` only.) Use Glob (`plugins/*/agents/*.md`, path project root) for plugin agent files; Glob (`agents/*.md`, path `.claude/`) for directly installed agents. Derive qualified name per file: `plugins/<plugin>/agents/<name>.md` → `<plugin>:<name>`; `.claude/agents/<name>.md` → `<name>`. Build full discovered-agent set.

**Step 7 — Validate registered agents exist on disk**: For each registered agent in domain table, verify it resolves to discovered file. Bare name (e.g. `sw-engineer`) matches `foundry:sw-engineer` when no `.claude/agents/sw-engineer.md` exists — apply model reasoning to resolve bare names against plugin-qualified discoveries. Registered agent with no matching file → **medium** (stale entry causes calibrate to fail at runtime; remove from domain table or correct prefix).

**Step 8 — Identify unregistered agents**: For each discovered agent not in domain table, apply same three-signal calibratability test from Step 4. → Unregistered calibratable agent: **low** (add to `calibrate/modes/agents.md` domain table with appropriate domain string).

## Check 40 — Health monitoring gap

Any SKILL.md that spawns `Agent(...)` **must** implement the CLAUDE.md §6 deadline protocol: per-agent deadline armed in the spawn response, one `agent_watch.py` check per wake-up, immediate ⏱ for a notification without its deliverable — never a waiting tool (`ScheduleWakeup`, `ListAgents`, `Monitor`) or a `find -newer` poll. Every spawn is background; `run_in_background` no longer exists, so keying on it would make this check silently N/A.

Scan all SKILL.md files in scope. For each file spawning `Agent(`, verify that the SAME file references `agent-spawn-protocol` OR `agent_watch`. If not → flag. A file still prescribing `health_sentinel` or a `find ... -newer ... wc -l` probe → flag as the retired liveness poll.

```bash
printf "=== Check 40: Health monitoring gap ===\n"
for f in $(find . -path "*skills/*/SKILL.md" 2>/dev/null | sort); do
    grep -q "Agent(" "$f" 2>/dev/null || continue
    grep -q "agent-spawn-protocol\|agent_watch" "$f" 2>/dev/null || printf "C40-HIGH: agent spawn without deadline protocol: %s\n" "$f"
    grep -q "health_sentinel\|find.*-newer.*wc -l" "$f" 2>/dev/null && printf "C40-MEDIUM: retired liveness poll still prescribed: %s\n" "$f"
done  # timeout: 5000
```

**Severity**: high — agents can silently time out with no user notification; lost work and false-progress indicators result. medium — a retired liveness poll costs a turn per probe and answers nothing the deadline check does not. Fix: reference `agent-spawn-protocol.md` §Deadlines; write `agent-watch-<batch>.tsv` in every spawn response.
