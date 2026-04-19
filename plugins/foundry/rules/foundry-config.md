---
description: Foundry-infrastructure rules for editing .claude/ files — plan mode gate, post-edit checklist, XML conventions, worktree policy, cleanup hook, and settings.json allow entries
paths:
  - .claude/**
---

## Before Editing

- **Enter plan mode first** — triggers Opus via `opusplan`. **No exceptions**: typo fixes, single-step edits, "quick" changes all need plan mode. Global "3+ steps" threshold NOT apply here — any `.claude/` edit = non-trivial.

## After Any Change

1. **Cross-references** — name or capability changes → update every file mentioning it
2. **Auto-memory MEMORY.md** — keep agents/skills roster in sync with disk (not under `.claude/`; path injected at session start — derive with `~/.claude/projects/$(git rev-parse --show-toplevel 2>/dev/null | tr '/' '-' | tr '.' '-')/memory/MEMORY.md`)
3. **`README.md`** — verify agent/skill tables, Status Line, Config Sync sections
4. **`settings.json` permissions** — new `gh`, `bash`, or `WebFetch` call → add matching allow rule before marking complete. Scan diff for new CLI invocations.
5. **`</workflow>` tags** — mode sections sit inside block; closing tag after last mode, before `<notes>`
6. **Step numbering** — renumber sequentially after adding/removing steps

## Path Rules (foundry-infra)

- `statusLine` and hook paths in home `settings.json` use `$HOME`: `node $HOME/.claude/hooks/statusline.js`

## Worktrees

- Worktrees land under `.claude/worktrees/<id>/`
- Permissions in `settings.local.json` snapshotted at worktree-creation — not updated retroactively
- Alternative: spawn agent with `isolation: "worktree"` — CWD auto-set to worktree root

## Agent/Skill File XML Tag Conventions

- **Structural tags** (`<role>`, `<workflow>`, `<notes>`): unescaped — primary Claude Code-parsed sections
- **Non-structural section tags** (e.g. `\<antipatterns_to_flag>`, `\<toolchain>`, `\<core_principles>`): backslash-escaped — internal org, Claude Code ignores
- New section tag: use `\<tag>` for subsections inside `<role>` or `<workflow>`; leave three structural tags unescaped

## Distribution

- Source of truth: `plugins/foundry/` (rules, agents, skills, hooks, CLAUDE.md, TEAM_PROTOCOL.md, permissions-guide.md)
- `.claude/` entries = symlinks into plugin — edit plugin files, not symlinks
- Rules distribute to `~/.claude/rules/` via `/foundry:init`
- `permissions-guide.md` = project-only reference — symlinked from `.claude/`, not copied to `~/.claude/`
- `settings.local.json` never distributed; `CLAUDE.md` NOT distributed (reserved — user owns `~/.claude/CLAUDE.md`); `TEAM_PROTOCOL.md` IS distributed via `/foundry:init`

## Log File TTL

| Location | TTL | Condition |
| --- | --- | --- |
| `~/.claude/logs/` | forever | hook audit logs (invocations, compactions, timings) — global across all projects; rotate at 10 MB |
| `.claude/logs/` | forever | skill-specific logs (calibrations, session-archive, audit-errors) — project-scoped; rotate at 10 MB |

## Cleanup Hook (SessionEnd)

`SessionEnd` hook runs cleanup automatically:

```bash
# Delete completed skill runs older than 30 days
find .reports/calibrate .reports/resolve .reports/audit .reports/review .reports/analyse .experiments .developments \
    -maxdepth 2 -name "result.jsonl" -mtime +30 2>/dev/null |
xargs dirname | xargs rm -rf

# Delete stale blueprint specs, cache, and temp outputs older than 30 days
find .plans/blueprint .cache .temp -type f -mtime +30 2>/dev/null | xargs rm -f
```

## Settings.json Allow Entries

Dot-prefixed paths enable precise allow rules (keep in sync with `settings.json` — `/foundry:audit` Check 6 detects drift):

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
