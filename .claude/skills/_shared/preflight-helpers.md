# Shared Pre-flight Helpers

Skills that need to verify binary or environment prerequisites before running should use this caching pattern instead of bare `which`/`command -v` calls.

## Pattern

```bash
# Paste at the top of any Step 0 / Step 1 block that uses pre-flight checks
preflight_ok()  { local f=".claude/state/preflight/$1.ok"; [ -f "$f" ] && [ $(( $(date +%s) - $(cat "$f") )) -lt 14400 ]; }
preflight_pass(){ mkdir -p .claude/state/preflight; date +%s > ".claude/state/preflight/$1.ok"; }
```

**TTL**: 4 hours (14400 seconds). Binary presence on PATH does not change within a normal session.

**Usage** — replace a bare check with a cached one:

```bash
# Before
which gh || { echo "gh not found"; exit 1; }

# After
preflight_ok gh || which gh || { echo "gh not found"; exit 1; }
preflight_pass gh
```

The cache is per-working-directory (`.claude/state/preflight/` is relative to the project root). If a skill runs in a different repo, it gets its own cache.

## Key Registry

| Key          | Check                                        | Used by            |
| ------------ | -------------------------------------------- | ------------------ |
| `git`        | `git rev-parse --git-dir` — git repo present | `codex`, `audit`   |
| `codex`      | `which codex` — Codex CLI on PATH            | `codex`, `resolve` |
| `gh`         | `which gh` — GitHub CLI on PATH              | `resolve`          |
| `jq`         | `command -v jq` — jq on PATH                 | `audit`            |
| `pre-commit` | `command -v pre-commit` — pre-commit on PATH | `audit`            |

**Update this table** when adding a new cacheable check to any skill.

## What NOT to Cache

Some checks look stable but are not — never cache these:

- `gh auth status` — validates token against GitHub API; token can expire
- `git status --porcelain` — live working-tree state, changes constantly
- `git rev-parse --abbrev-ref HEAD` — branch can change during a session
- Any `metric_cmd` or `guard_cmd` output — depends on current code state
- Network reachability checks of any kind
