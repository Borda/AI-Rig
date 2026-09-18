---
description: Foundry-infrastructure rules for editing .claude/ files — plan mode gate, post-edit checklist, XML conventions, worktree policy, cleanup hook, and settings.json allow entries
paths:
  - .claude/**
---

## Before Editing

- **STOP — don't open, read, or edit any file.** Enter plan mode first (`opusplan`). **No exceptions**: typo fixes, single-step edits, "quick" changes all require plan mode before any action. Global "3+ steps" threshold does NOT apply — any `.claude/` edit = non-trivial.
- **Never edit `.claude/` paths directly.** Run `ls -la .claude/` first to identify symlink target; all `.claude/` entries are symlinks into `plugins/cc_foundry/`; edit only source file under `plugins/cc_foundry/`, never `.claude/` symlink destination.
- **Stop before duplicating**: change would add identical or near-identical content to 3+ files → stop, identify shared abstraction first (e.g. `_shared/` dir, shared rule file); proceed to individual file edits only after single source of truth identified.

## Agent Dispatch for .claude/ Config Edits

Use `foundry:curator`, not `foundry:sw-engineer`, for `.claude/` edits (agents, skills, rules, CLAUDE.md). Exception: hook files (`*.js`) → `foundry:sw-engineer`.

`sw-engineer` = application code. `curator` = Claude Code config files.

## After Any Change

1. **Cross-references** — name or capability changes → update every file mentioning it
2. **Auto-memory MEMORY.md** — keep agents/skills roster in sync with disk (not under `.claude/`; path injected at session start — derive with `~/.claude/projects/$(git rev-parse --show-toplevel 2>/dev/null | tr '/' '-' | tr '.' '-')/memory/MEMORY.md`)
3. **`README.md`** — verify agent/skill tables, Status Line, Config Sync sections
4. **`settings.json` permissions** — new `gh`, `bash`, or `WebFetch` call → add matching allow rule before marking done; scan diff for new CLI invocations
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
- **Non-structural section tags** (e.g. `<antipatterns-to-flag>`, `\<toolchain>`, `<core-principles>`): backslash-escaped — internal org, Claude Code ignores
- New section tag: use `\<tag>` for subsections inside `<role>` or `<workflow>`; leave three structural tags unescaped

## Audit Check Numbering Convention

- **Top-level**: integer (`12`, `29`, `32`)
- **Sub-checks**: lowercase letter suffix — never decimal (`29.5`)
- **First sub-check always starts at `a`** (`29a`, `32a`) — never skip letters
- **Additional sub-checks**: continue from last used letter (`29a`, `29b` → next is `29c`)

## Distribution

- Source of truth: `plugins/cc_foundry/` for foundry-owned files (rules, agents, skills, hooks, CLAUDE.md, TEAM_PROTOCOL.md, permissions-guide.md); other plugins (`plugins/cc_oss/`, `plugins/cc_research/`, etc.) own their own agents/skills — edit in respective `plugins/<name>/` source dirs
- `.claude/` entries = symlinks into plugin — edit plugin files, not symlinks
- Rules distribute to `~/.claude/rules/` via `/foundry:setup`
- `permissions-guide.md` = project-only reference — symlinked from `.claude/`, not copied to `~/.claude/`
- `settings.local.json` never distributed; `CLAUDE.md` NOT distributed (reserved — user owns `~/.claude/CLAUDE.md`); `TEAM_PROTOCOL.md` IS distributed via `/foundry:setup`

## Log File TTL

| Location | TTL | Condition |
| -- | -- | -- |
| `~/.claude/logs/` | forever | hook audit logs (invocations, compactions, timings) — global across all projects; rotate at 10 MB |
| `.notes/logs/` | forever | skill-specific logs (calibrations, audit-errors) — project-scoped; rotate at 10 MB; legacy `.claude/logs/` read-only fallback for historical entries written before relocation |

## Cleanup Hook (SessionEnd)

`SessionEnd` hook runs cleanup automatically:

```bash
# completed runs (have result.jsonl), older than 30 days
find .reports/calibrate .reports/resolve .reports/audit .reports/analyse .experiments .developments \
    -maxdepth 2 -name "result.jsonl" -mtime +30 2>/dev/null |
xargs dirname | xargs rm -rf

# review dirs, two shapes: legacy flat <timestamp>/ (still produced by /develop:review) ages by its own mtime;
# pr-<N>/run-<NNN>/ (oss) ages per run not per PR — active PR's older runs still expire;
# empty pr-<N> parent swept once its last run ages out
find .reports/review -mindepth 1 -maxdepth 1 -type d ! -name 'pr-*' -mtime +30 2>/dev/null | xargs rm -rf 2>/dev/null
find .reports/review -mindepth 2 -maxdepth 2 -type d -name 'run-*' -mtime +30 2>/dev/null | xargs rm -rf 2>/dev/null
find .reports/review -mindepth 1 -maxdepth 1 -type d -name 'pr-*' -empty -delete 2>/dev/null

find .plans/blueprint .cache .temp -type f -mtime +30 2>/dev/null | xargs rm -f

# empty dirs left after file cleanup
find .temp -mindepth 2 -maxdepth 2 -type d -empty -delete 2>/dev/null
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
"Bash(mkdir -p .reports/analyse/*)",
"Bash(mkdir -p .reports/review/*)",
"Bash(mkdir -p .temp/review/*)",
"Bash(mkdir -p .experiments/*)",
"Bash(mkdir -p .developments/*)",
"Bash(mkdir -p .temp/)",
"Bash(find .cache*)",
"Bash(find .notes*)",
"Bash(find .plans*)",
"Bash(find .reports/calibrate*)",
"Bash(find .reports/resolve*)",
"Bash(find .reports/audit*)",
"Bash(find .experiments*)",
"Bash(find .developments*)",
"Bash(find .reports*)",
"Bash(find .temp*)"
```
