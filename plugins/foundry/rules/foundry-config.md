---
description: Foundry-infrastructure rules for editing .claude/ files — plan mode gate, post-edit checklist, XML conventions, worktree policy, cleanup hook, and settings.json allow entries
paths:
  - .claude/**
---

## Before Editing

- **Enter plan mode first** — triggers Opus via `opusplan` for best reasoning on configuration changes. **No exceptions**: typo fixes, single-step edits, and "quick" changes all require plan mode. The global "non-trivial task (3+ steps)" threshold does NOT apply here — any edit to `.claude/` is treated as non-trivial.

## After Any Change

1. **Cross-references** — if a name or capability changes, update every file that mentions it
2. **Auto-memory MEMORY.md** — keep the agents/skills roster in sync with disk (not stored under `.claude/`; path injected at session start — derive with `~/.claude/projects/$(git rev-parse --show-toplevel 2>/dev/null | tr '/' '-' | tr '.' '-')/memory/MEMORY.md`)
3. **`README.md`** — verify agent/skill tables, Status Line, and Config Sync sections
4. **`settings.json` permissions** — IF this change introduces any new `gh`, `bash`, or `WebFetch` call (directly or in a step/workflow you are adding), you MUST add a matching allow rule before marking the task complete. Check: scan the diff for any new CLI invocations before ticking this off.
5. **`</workflow>` tags** — mode sections must sit inside the block; closing tag after the last mode, before `<notes>`
6. **Step numbering** — renumber sequentially after adding/removing steps

## Path Rules (foundry-infra)

- `statusLine` and hook paths in home `settings.json` use `$HOME`: `node $HOME/.claude/hooks/statusline.js`

## Worktrees

- Worktrees land under `.claude/worktrees/<id>/`
- Permissions in `settings.local.json` are snapshotted at worktree-creation time — not updated retroactively
- Alternative for worktree commands: spawn an agent with `isolation: "worktree"` — its CWD is the worktree root automatically

## Agent/Skill File XML Tag Conventions

- **Structural tags** (`<role>`, `<workflow>`, `<notes>`): unescaped — the primary Claude Code-parsed sections
- **Non-structural section tags** (e.g. `\<antipatterns_to_flag>`, `\<toolchain>`, `\<core_principles>`): backslash-escaped — internal organisation that Claude Code does not parse as metadata
- When adding a new section tag: use `\<tag>` for any subsection inside `<role>` or `<workflow>`; leave the three structural tags unescaped

## Distribution

- Source of truth: `plugins/foundry/` (rules, agents, skills, hooks, permissions-guide.md, TEAM_PROTOCOL.md)
- `.claude/` entries are symlinks into the plugin — edit the plugin files, not the symlinks
- Rules distribute to `~/.claude/rules/` via `/foundry:init` (copy) or `/foundry:init link` (symlink)
- `permissions-guide.md` is project-only reference — symlinked from `.claude/`, not copied to `~/.claude/`
- `settings.local.json` is never distributed; `CLAUDE.md` IS distributed via `/foundry:init`

## Log File TTL

| Location             | TTL     | Condition                                                                                         |
| -------------------- | ------- | ------------------------------------------------------------------------------------------------- |
| `~/.claude/logs/`    | forever | hook audit logs (invocations, compactions, timings) — global across all projects; rotate at 10 MB |
| `.claude/logs/`      | forever | skill-specific logs (calibrations, session-archive, audit-errors) — project-scoped; rotate at 10 MB |

## Cleanup Hook (SessionEnd)

The `SessionEnd` hook runs this cleanup automatically:

```bash
# Delete completed skill runs older than 30 days
find .reports/calibrate .reports/resolve .reports/audit .reports/review .reports/analyse .experiments .developments \
    -maxdepth 2 -name "result.jsonl" -mtime +30 2>/dev/null |
xargs dirname | xargs rm -rf

# Delete stale blueprint specs, cache, and temp outputs older than 30 days
find .plans/blueprint .cache .temp -type f -mtime +30 2>/dev/null | xargs rm -f
```

## Settings.json Allow Entries

The deterministic dot-prefixed paths allow precise allow rules (keep in sync with `settings.json` — `/audit` Check 6 detects drift):

```text
"Bash(mkdir -p .cache/*)",
"Bash(mkdir -p .notes/)",
"Bash(mkdir -p .plans/active/)",
"Bash(mkdir -p .plans/blueprint/)",
"Bash(mkdir -p .plans/closed/)",
"Bash(mkdir -p .reports/calibrate/*)",
"Bash(mkdir -p .reports/resolve/*)",
"Bash(mkdir -p .reports/audit/*)",
"Bash(mkdir -p .reports/review/*)",
"Bash(mkdir -p .reports/analyse/*)",
"Bash(mkdir -p .experiments/*)",
"Bash(mkdir -p .developments/*)",
"Bash(mkdir -p .temp/)",
"Bash(find .cache*)",
"Bash(find .notes*)",
"Bash(find .plans*)",
"Bash(find .reports/calibrate*)",
"Bash(find .reports/resolve*)",
"Bash(find .reports/audit*)",
"Bash(find .reports/review*)",
"Bash(find .experiments*)",
"Bash(find .developments*)",
"Bash(find .reports*)",
"Bash(find .temp*)"
```
