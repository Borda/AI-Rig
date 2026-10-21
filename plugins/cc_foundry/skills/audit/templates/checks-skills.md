# Skill Checks — Hub

## Scan-root derivation — prepend to every bash block in the group files below

Each fenced block in the group files runs as its own Bash tool call in a **fresh shell**, so no variable set in one block survives into the next (`audit/SKILL.md` §State re-derivation; Check 43 in `checks-skills-sweep.md` flags the same pattern). Each block therefore re-reads `LOCAL_MODE` and re-derives its own scan root.

Roots are **plain directory paths**, never glob patterns held in variables: the tool shell may be `zsh`, which — unlike bash — performs neither word-splitting nor filename generation on an unquoted `$VAR`. A var-held glob such as `for f in $_SKILL_GLOB` iterates once over the literal pattern string and matches nothing. Enumerate with `find` piped into `while IFS= read -r`, which behaves identically under both shells.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
[ "$LOCAL_MODE" = "true" ] && _ROOT="plugins" || _ROOT=".claude"
```

`find "$_ROOT" -path "*skills/*/SKILL.md"` enumerates skills; `find "$_ROOT" -path "*/agents/*.md"` enumerates agents **including nested subdirectories** (a `*/agents/*.md` glob silently misses `agents/<parent>/<file>.md`); `find "$_ROOT" -path "*/rules/*.md"` enumerates rules.

## Check groups

Each group file holds the full spec of its checks. A run loads this hub plus only the groups its scope dispatches (`modes/steps-4-5-7.md` Step 4).

| File | Checks |
| -- | -- |
| `checks-skills-gaps.md` | 22 calibration coverage gap, 40 health monitoring gap |
| `checks-skills-bash.md` | 23 bash misuse / native tool substitution (23a-23d), 30 plugin skill bash operational correctness (30a-30f) |
| `checks-skills-contract.md` | 24 skill sequence compatibility, 31 allowed-tools consistency (31a), 38 AskUserQuestion cap |
| `checks-skills-crossplugin.md` | 27 cross-plugin shared-file reference integrity, 28 cross-plugin agent dispatch fallback (28c) |
| `checks-skills-deadfiles.md` | 32 dead file detection (32a-32g) |
| `checks-skills-duplication.md` | 33 code block duplication (33a, 33b) |
| `checks-skills-sweep.md` | C35 background agent health monitoring, 43 shell variable persistence, 42 unexpanded spawn-prompt variables, 44 TMPDIR sentinel session scoping |
