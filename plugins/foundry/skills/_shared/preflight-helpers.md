**Re: Compress preflight helpers doc to caveman format**

# Preflight Helpers

TTL binary availability cache for skill pre-flight checks. Results cached 4 hours (14400 sec) per binary under `.claude/state/preflight/`. Skips repeated `command -v` calls within session.

## Functions

```bash
# Returns 0 (true) if binary $1 passed preflight within the last 4 hours
preflight_ok() {
    local f=".claude/state/preflight/$1.ok"
    [ -f "$f" ] && [ $(($(date +%s) - $(cat "$f"))) -lt 14400 ]
}

# Records a passing preflight result for binary $1 at the current timestamp
preflight_pass() {
    mkdir -p .claude/state/preflight
    date +%s >".claude/state/preflight/$1.ok"
}
```

Cache files at `.claude/state/preflight/<binary>.ok` hold Unix timestamp. 4-hour TTL handles staleness — no cleanup needed.

## Usage

Check binary, skip step if unavailable:

```bash
if preflight_ok jq; then
    JQ_AVAILABLE=true
elif command -v jq &>/dev/null; then
    preflight_pass jq
    JQ_AVAILABLE=true
else
    printf "⚠ MISSING: jq not found — skipping check\n"
    JQ_AVAILABLE=false
fi
```

Warning-only (absence non-fatal):

```bash
if ! preflight_ok git && ! command -v git &>/dev/null; then
    printf "⚠ MISSING: git not found\n"
else
    preflight_ok git || preflight_pass git
fi
```

Combined check-and-run (inline pass on first use):

```bash
if (preflight_ok pre-commit || { command -v pre-commit &>/dev/null && preflight_pass pre-commit; }) &&
[ -f .pre-commit-config.yaml ]; then
    pre-commit run --all-files
fi
```

## Key Registry

| Key | Check | Used by |
| --- | --- | --- |
| `git` | `command -v git` — git on PATH | `audit` |
| `codex` | `[ -n "$CLAUDE_PLUGIN_DATA" ] && [[ "$CLAUDE_PLUGIN_DATA" == *codex-openai-codex* ]]` | `resolve`, `calibrate` |
| `gh` | `which gh` — GitHub CLI on PATH | `resolve` |
| `jq` | `command -v jq` — jq on PATH | `audit` |
| `pre-commit` | `command -v pre-commit` — pre-commit on PATH | `audit` |

**Update table** when adding new cacheable check to any skill.

## What NOT to Cache

- `gh auth status` — validates token against GitHub; token can expire
- `git status --porcelain` — live working-tree state, changes constantly
- `git rev-parse --abbrev-ref HEAD` — branch can change mid-session
- Any `metric_cmd` or `guard_cmd` output — depends on current code state
- Network reachability checks
