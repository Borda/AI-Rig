---
description: Rules for editing files under .claude/ — plan mode, cross-references, sync
paths:
  - .claude/**
---

## Before Editing

- **Enter plan mode first** — triggers Opus via `opusplan` for best reasoning on configuration changes

## After Any Change

1. **Cross-references** — if a name or capability changes, update every file that mentions it
2. **`memory/MEMORY.md`** — keep the agents/skills roster in sync with disk
3. **`README.md`** — verify agent/skill tables, Status Line, and Config Sync sections
4. **`settings.json` permissions** — add a matching allow rule for any new `gh`, `bash`, or `WebFetch` calls
5. **`</workflow>` tags** — mode sections must sit inside the block; closing tag after the last mode, before `<notes>`
6. **Step numbering** — renumber sequentially after adding/removing steps

## Path Rules

- No hardcoded absolute user paths (`/Users/<name>/` or `/home/<name>/`) — use `.claude/`, `~/`, or `git rev-parse --show-toplevel`
- statusLine and hook paths in home `settings.json` use `$HOME`: `node $HOME/.claude/hooks/statusline.js`

## Sync

- Source of truth: project `.claude/`
- Propagate to home `~/.claude/` with `/sync apply`
- `settings.local.json` is never synced; `CLAUDE.md` IS synced
