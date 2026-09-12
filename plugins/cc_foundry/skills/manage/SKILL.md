---
name: manage
description: 'Create, update, or delete agents, skills, and rules; update or delete hooks (hook creation not yet implemented — edit `hooks/<name>.js` directly). Full cross-reference propagation. Trivial edits (typos, small fixes ≤10 words) applied inline without agent; `.md` content-edits delegated to foundry:curator; code file edits (`.js`, `.py`, `.ts`) delegated to foundry:sw-engineer; large cross-ref fan-outs (> 3 files) also delegate. The parent orchestrates MEMORY.md, README, audit, calibration, and the final report. Also manages settings.json permissions atomically with permissions-guide.md. NOT for: validation/quality audit of existing agents/skills (use /foundry:audit); implementing application source code changes outside `.claude/` (use develop:feature or develop:fix — requires `develop` plugin).'
argument-hint: create <agent|skill|rule> <name> "desc" | update <name> [new-name|"change"|spec.md] | delete <name> | add perm <rule> "desc" "use-case" | remove perm <rule>
effort: medium
disable-model-invocation: true
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, Agent, TaskList, TaskCreate, TaskUpdate, AskUserQuestion, Skill
---

> **Note:** `disable-model-invocation: true` — `/manage` user-invoked only, no `Skill()` chaining from orchestrators. When suggesting `/manage` as follow-up, invoking skill must present as user-run command, not auto step.

<objective>

Manage lifecycle of agents, skills, rules, hooks in `.claude/`. Handles creation with rich domain content, atomic renames with cross-ref propagation, content editing (trivial edits inline; `.md` files → foundry:curator; code files `*.js`/`*.py`/`*.ts` → foundry:sw-engineer; rule edits inline), clean deletion with broken-ref cleanup. Keeps MEMORY.md inventory in sync with disk.

</objective>

<inputs>

- **$ARGUMENTS**: required, one of:

  - `create agent <name> "description"` — create new agent with generated domain content
  - `create skill <name> "description"` — create new skill with workflow scaffold
  - `create rule <name> "description"` — create new rule file with frontmatter and sections
  - `update <name> <new-name>` — rename; type auto-detected from disk
  - `update <name> "change description"` — content-edit; trivial → inline, `.md` → foundry:curator, code → foundry:sw-engineer, rule → inline
  - `update <name> <spec-file.md>` — content-edit from spec file; trivial → inline, `.md` → foundry:curator, code → foundry:sw-engineer, rule → inline
  - `delete <name>` — delete; type auto-detected from disk (agents, skills, rules, hooks); asks user if ambiguous
  - `add perm <rule> "description" "use case"` — add permission to settings.json allow list and permissions-guide.md
  - `remove perm <rule>` — remove permission from settings.json allow list and permissions-guide.md

- Names must be **kebab-case** (lowercase, hyphens only)

- Descriptions must be quoted when containing spaces

- Permission rules use Claude Code format: `WebSearch`, `Bash(cmd:*)`, `WebFetch(domain:example.com)`

- `--skip-audit` — optional flag: skip Step 9 `/audit` validation (use inside `audit fix` loop to avoid recursion)

- **Spec-file paths must be quoted** — `update <name> <spec-file.md>` requires the spec path quoted if it contains any whitespace (e.g. `update my-agent "docs/My Spec.md"`); unquoted paths with spaces split into multiple arguments and trigger argument-shape mismatch. Recommended: keep spec filenames free of spaces.

**Update/delete mode** — name looked up across agents, skills, rules automatically:

- One match on disk → proceed with that type
- Multiple matches → `AskUserQuestion`: (a) agent, (b) skill, (c) rule
- No match → report error and stop

**Update second-argument discrimination**:

- Two bare kebab-case args (second arg no spaces, no `.md` extension) → **rename mode**
- One name + quoted string → **content-edit mode** (trivial → inline; `.md`: foundry:curator; code `*.js`/`*.py`/`*.ts`: foundry:sw-engineer; rule: inline)
- One name + path ending in `.md` → **content-edit mode** (trivial → inline; `.md`: foundry:curator; code `*.js`/`*.py`/`*.ts`: foundry:sw-engineer; rule: inline)

**Examples:**

- `/foundry:manage create agent task-planner "Planning specialist for decomposing epics into actionable tasks"`
- `/foundry:manage update my-agent "add a section on error handling patterns"`
- `/foundry:manage update optimize docs/specs/YYYY-MM-DD-<spec-name>.md`
- `/foundry:manage delete old-agent-name`
- `/foundry:manage add perm "Bash(jq:*)" "Parse and filter JSON" "Extract fields from REST API responses"`

</inputs>

<constants>

- AGENTS_DIR: `.claude/agents`
- SKILLS_DIR: `.claude/skills`
- RULES_DIR: `.claude/rules`
- HOOKS_DIR: `.claude/hooks`
- AVAILABLE_COLORS: indigo, lime, magenta, teal, violet

Each Step 4 spawn applies the health monitoring in `_shared/agent-spawn-protocol.md` §8b — rely on the harness completion notification, then read the agent's output file; optional single `health_sentinel.py` probe per turn (no sleep loop). Substitute only its own `<ID>` suffix and output-file glob; do not re-paste the snippet per spawn.

Colors in use are read from the live Grep in Step 3 (authoritative) — no static used-color list to maintain. AVAILABLE_COLORS is the candidate pool for a new agent; pick the first entry not already in the Step-3 set.

</constants>

<workflow>

**Task hygiene**: call `TaskList` first; close orphaned tasks. **Task tracking**: create tasks for each major phase; mark in_progress/completed throughout.

## Step 1: Parse and validate

Extract operation, type, name, optional arguments from `$ARGUMENTS`.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
eval "$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/parse-skill-flags.py" --flags skip-audit "$ARGUMENTS")"  # timeout: 5000
SKIP_AUDIT="$FLAG_SKIP_AUDIT"
ARGUMENTS="$CLEAN_ARGS"
echo "$SKIP_AUDIT" > "${TMPDIR:-/tmp}/manage-skip-audit-${CSID}"  # persist (Check 41)
echo "${TMPDIR:-/tmp}/manage-skip-audit-${CSID}" > "${TMPDIR:-/tmp}/manage-skip-audit-path-${CSID}"
```

**Unsupported flag check** — after all supported flags extracted (`--skip-audit`), scan `$ARGUMENTS` for remaining `--<token>` tokens. If found: print `` ! Unknown flag(s): `--<token>`. Supported: `--skip-audit`. `` then invoke `AskUserQuestion` — (a) **Abort** (stop, re-invoke with correct flags) · (b) **Continue ignoring** (skip unknown flags, proceed). On Abort: stop.

**Validation rules:**

- Name must match `^[a-z][a-z0-9-]*$` (kebab-case)
- For `create`: name must NOT already exist on disk; description required
- For `update`/`delete`: name MUST already exist on disk
- For `update` rename: new-name must NOT already exist on disk
- For `add perm`: rule must NOT already exist in settings.json allow list; description and use case required
- For `remove perm`: rule MUST already exist in settings.json allow list

**Type auto-detection** (for `update` and `delete`): first verify post-install context exists:

```bash
[ -d .claude/agents ] || { printf "! .claude/agents not found — run /foundry:setup first or confirm working directory is project root\n"; exit 1; }  # timeout: 3000
```

Then run all four Glob checks in parallel:

- Agent: pattern `agents/<name>.md`, path `.claude/`
- Skill: pattern `skills/<name>/SKILL.md`, path `.claude/`
- Rule: pattern `rules/<name>.md`, path `.claude/`
- Hook: pattern `hooks/<name>.js`, path `.claude/`

Results:

- One non-empty result → resolved type; proceed
- Multiple non-empty results → `AskUserQuestion`: "Multiple entities named `<name>` found. Which one? (a) agent (b) skill (c) rule (d) hook" — note: (d) hook valid for `update` and `delete` only; `create hook` not yet implemented (use Edit tool on `hooks/<name>.js` directly until create-hook mode added)
- All empty → report "No agent, skill, rule, or hook named `<name>` found" and stop

For `create`, check only relevant type's path.

**Delete confirmation gate** — when `$MODE` is `delete`, immediately after type resolution invoke `AskUserQuestion`: "Delete `<name>` (`<type>`)? This cannot be undone. (a) Confirm · (b) Abort". On Abort: stop. On Confirm: proceed to Step 4.

```bash
jq -e --arg rule '<rule>' '.permissions.allow | index($rule) != null' .claude/settings.json >/dev/null 2>&1  # timeout: 5000
```

**Update second-argument discrimination** — apply after type resolved. Set shell variable `MODE` from the parsed operation; consumed by the delete confirmation gate above, the edit-complexity classifier below, and the per-mode workflow branches in Step 4. Recognised values: `create`, `rename`, `content-edit`, `delete`, `add-perm`, `remove-perm`.

| Argument shape | `MODE` |
| -- | -- |
| `create <type> <name> "..."` | `create` |
| `update <name> <new-name>` (two bare kebab-case args; second has no spaces, no `.md`) | `rename` (validate new-name does NOT already exist) |
| `update <name> "<change>"` (one name + quoted string) | `content-edit` (validate spec non-empty; set `DIRECTIVE` = the quoted string) |
| `update <name> <spec>.md` (one name + path ending in `.md`; **must be quoted if path contains spaces**) | `content-edit` (validate spec file exists on disk and path ends in `.md`; report error if not found; set `DIRECTIVE` = contents of the spec file via Read tool) |
| `delete <name>` | `delete` |
| `add perm <rule> "..." "..."` | `add-perm` |
| `remove perm <rule>` | `remove-perm` |

Assign `MODE` in shell before the edit-complexity classification below so the `[[ "$MODE" == "content-edit" ]]` guard fires correctly:

```bash
# MODE="content-edit"   # or "rename" / "create" / "delete" / "add-perm" / "remove-perm"
```

If validation fails, report error and stop.

**Edit complexity classification** (content-edit mode only):

Classify `$DIRECTIVE` as **trivial** when ALL conditions hold:

| Condition | Required |
| -- | -- |
| Word count ≤ 10 | ✓ |
| Matches pattern: `typo`, `spelling`, `rename X to Y`, `change X to Y`, `replace X with Y`, `fix (a/the)? (typo/bug/error)`, `add missing`, `remove [word]`, `correct` | ✓ |

Both must hold — either failing → **substantive**. Trivial edits: apply inline with Edit tool — no agent spawn.

**Step skip rules**:

- **Perm operations**: skip Steps 2, 3, 5, 6, 7, 8, 9 — go Step 1 → Step 4 → Step 10
- **Hook operations**: skip Steps 2, 3, 6 (no color inventory, no MEMORY.md roster entry, no README table row); in Steps 5 and 7 skip cross-ref propagation (hook filenames not referenced from agent/skill markdown) — go Step 1 → Step 4 → Step 9 → Step 10
- **Content-edit operations**: skip Step 2 (entity already exists); skip Step 3 color inventory (no create); in Steps 5–7 only update cross-refs and README if name or description changed. Step 6 count: only update if name added or removed — content-only edits do not change agent/skill count.
- **Trivial content-edits**: additionally skip Steps 6–7 (no roster/description change possible); proceed Step 1 → Step 4 → Step 8 → Step 10

## Step 2: Overlap review (create only)

Before creating, check if existing agents/skills already cover requested functionality:

1. Read descriptions of all existing agents (use `Read(file_path=..., limit=3)` on each `.md` in agents/) and skills (use `Read(file_path=..., limit=3)` on each `SKILL.md`)
2. Compare new description against each existing — look for domain overlap, similar workflows, redundant scope
3. Present findings:
   - **No overlap**: proceed to Step 3
   - **Partial overlap**: name overlapping agent/skill, explain coverage vs what new one adds, use `AskUserQuestion`: "Extend existing (Recommended)" / "Proceed" / "Abort"
   - **Strong overlap**: recommend against creation — suggest using or extending existing agent/skill

Skip for `update`, `delete`, perm operations.

## Step 3: Inventory current state

Snapshot current roster for later comparison. Steps 2 and 3 are independent reads — issue Glob calls for both in same response.

Use Glob (pattern `agents/*.md`, path `.claude/`) for agents and Glob (pattern `skills/*/`, path `.claude/`) for skills. Use Grep (pattern `^color:`, glob `agents/*.md`, path `.claude/`, output mode `content`) to collect colors in use.

Extract names inline from Glob results — strip `.claude/agents/` prefix and `.md` suffix for agents; strip `.claude/skills/` prefix and trailing `/` for skills; strip `.claude/rules/` prefix and `.md` suffix for rules. Sort alphabetically when building roster string.

## Step 4: Execute operation

### Mode: Create Agent

1. Fetch latest Claude Code agent frontmatter schema:

   - Resolve schema cache path (24h TTL — schema changes rarely; saves one web-explorer spawn per create):
     ```bash
     mkdir -p .cache/manage  # timeout: 3000
     MANAGE_SCHEMA_FILE=".cache/manage/agent-schema.md"
     if [ -n "$(find "$MANAGE_SCHEMA_FILE" -mmin -1440 2>/dev/null)" ]; then MANAGE_SCHEMA_CACHED=true; else MANAGE_SCHEMA_CACHED=false; fi
     echo "Schema file: $MANAGE_SCHEMA_FILE (cached: $MANAGE_SCHEMA_CACHED)"  # timeout: 3000
     ```
   - **`MANAGE_SCHEMA_CACHED=true`** → skip the spawn and health monitoring below; Read `$MANAGE_SCHEMA_FILE` (limit=60) for the field list and continue at the extraction bullet.
   - Spawn **foundry:web-explorer** to fetch `https://code.claude.com/docs/en/sub-agents` with instruction: "Write your full findings (schema fields, new fields, deprecated fields) to `<MANAGE_SCHEMA_FILE>` (substitute resolved path from bash block above) using the Write tool. Return ONLY a compact JSON envelope on your final line — nothing else after it: `{\"status\":\"done\",\"file\":\"<MANAGE_SCHEMA_FILE>\",\"fields\":N,\"new\":N,\"deprecated\":N,\"confidence\":0.N,\"summary\":\"N fields, N new, N deprecated\"}`"

   Health monitoring §8b: `<ID>` = `web-explorer`, glob `agent-schema.md` (poll path `.cache/manage`).

   - Read returned summary; extract: valid frontmatter fields (`name`, `description`, `tools`, `disallowedTools`, `model`, `permissionMode`, `maxTurns`, `effort`, `initialPrompt`, `skills`, `mcpServers`, `hooks`, `memory`, `background`, `isolation`, `color`), current model shorthands, new fields
   - Note new fields worth including. Adjust template to reflect current schema. If new field broadly useful for agent's role (e.g. `maxTurns` for long-running agents), include with sensible default and inline comment.

2. Pick first unused color from AVAILABLE_COLORS pool (compare against Step 3 colors)

3. Choose model based on role complexity:

   - `opusplan` — plan-gated roles (solution-architect, oss:shepherd, foundry:curator)
   - `opus` — complex implementation roles (foundry:sw-engineer, research:scientist, foundry:perf-optimizer)
   - `sonnet` — focused execution roles (research:data-steward (requires `research` plugin), foundry:web-explorer, foundry:doc-scribe, foundry:creator, foundry:qa-specialist, oss:cicd-steward)
   - `haiku` — high-frequency diagnostics ONLY (e.g. linting-expert); NOT for analysis/auditing roles that require substantive reasoning

4. Resolve template path (cascade primary → project-local → cache scan; only the cache scan runs if neither cheaper path exists, since each candidate must satisfy `-d` before being assigned):

```bash
MANAGE_TPL=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/resolve_skill_subdir.py" manage templates) || { printf "! BREAKING: manage templates not found — run /foundry:setup first\n"; exit 1; }  # timeout: 5000
```

5. Spawn **foundry:sw-engineer** subagent to scaffold and write the agent file. `foundry:curator` is the wrong delegate here — its NOT-for explicitly excludes creating or scaffolding agents/skills; curator only reviews and edits existing config. `foundry:sw-engineer` owns scaffolding (treat agent `.md` as a config artifact whose authoring is a software task — frontmatter schema, tool selection, structural completeness).

> Before passing schema file path to sw-engineer: verify file exists on disk using Read tool (limit=1). If schema file path from JSON envelope does not exist, proceed with default frontmatter fields (name, description, model, color) — note omission in Step 10 report.

```markdown
Run `cat "<MANAGE_TPL>/agent-scaffold.md"` via the Bash tool (substitute resolved path from bash block above — do not pass literal `$MANAGE_TPL` to the agent).
Also read the schema file at the path returned in the step 1 JSON to incorporate any new frontmatter fields (skip if schema file not found — use default frontmatter fields: name, description, model, color).
Scaffold `.claude/agents/<name>.md` with:
- Frontmatter: name=<name>, description=<description>, model=<model>, color=<color>; add any broadly-useful new fields from the schema
- Body: rich domain-specific content for the role described by the description, following all content rules and tool selection guidelines in the scaffold template
Write the file using the Write tool.
Return ONLY: {"status":"done","file":".claude/agents/<name>.md","lines":N,"confidence":0.N}
```

Health monitoring §8b: `<ID>` = `sw-engineer-agent`, glob matching this agent's output files.

**CRITICAL — worktree isolation copy**: `foundry:sw-engineer` runs with `isolation: worktree` — scaffolded file lands in a temporary worktree, not the main tree. After agent completes: (1) read the worktree path from the agent result (returned in `worktree` field or as part of the result message); (2) run: `cp <worktree-path>/.claude/agents/<name>.md .claude/agents/<name>.md` (substitute actual paths); (3) proceed with Steps 5–9 on the main-tree copy. Without this step, Steps 5–9 Globs find nothing.

### Mode: Create Skill

1. Fetch latest Claude Code skill frontmatter schema:

   - Resolve skill schema cache path (24h TTL — same rationale as Create Agent step 1):
     ```bash
     mkdir -p .cache/manage  # timeout: 3000
     MANAGE_SKILL_SCHEMA_FILE=".cache/manage/skill-schema.md"
     if [ -n "$(find "$MANAGE_SKILL_SCHEMA_FILE" -mmin -1440 2>/dev/null)" ]; then MANAGE_SKILL_SCHEMA_CACHED=true; else MANAGE_SKILL_SCHEMA_CACHED=false; fi
     echo "Skill schema file: $MANAGE_SKILL_SCHEMA_FILE (cached: $MANAGE_SKILL_SCHEMA_CACHED)"  # timeout: 3000
     ```
   - **`MANAGE_SKILL_SCHEMA_CACHED=true`** → skip the spawn and health monitoring below; Read `$MANAGE_SKILL_SCHEMA_FILE` (limit=60) for the field list and continue at the extraction bullet.
   - Spawn **foundry:web-explorer** to fetch `https://code.claude.com/docs/en/skills` with instruction: "Write your full findings (schema fields, new fields, deprecated fields) to `<MANAGE_SKILL_SCHEMA_FILE>` (substitute resolved path from bash block above) using the Write tool. Return ONLY a compact JSON envelope on your final line — nothing else after it: `{\"status\":\"done\",\"file\":\"<MANAGE_SKILL_SCHEMA_FILE>\",\"fields\":N,\"new\":N,\"deprecated\":N,\"confidence\":0.N,\"summary\":\"N fields, N new, N deprecated\"}`"

   Health monitoring §8b: `<ID>` = `web-explorer-skill`, glob matching this agent's output files.

   - Read returned summary; extract: valid frontmatter fields (`name`, `description`, `argument-hint`,`disable-model-invocation`, `user-invocable`, `allowed-tools`, `model`, `effort`, `shell`, `paths`, `context`, `agent`, `hooks`), new fields
   - Note new fields worth including. Adjust template to reflect current schema. Include `model` or `context: fork` only when skill's purpose clearly benefits.

2. **Re-resolve `MANAGE_TPL` at the start of each skill invocation**; do not assume it is set from a prior step. Most `/foundry:manage create skill ...` invocations enter Create Skill mode directly without going through Create Agent first, so the variable will be unset. Run the resolution block from Create Agent step 4 above (cascade primary → project-local → cache scan with the `-d` guards) before reading any template path.

3. Resolve `$_FOUNDRY_SHARED` before spawning — sub-agents do not inherit shell variables:

```bash
_FOUNDRY_SHARED=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/resolve_shared_path.py" foundry skills/_shared 2>/dev/null || echo "plugins/cc_foundry/skills/_shared")  # timeout: 5000
echo "Shared dir: $_FOUNDRY_SHARED"
```

Spawn **foundry:sw-engineer** subagent to create directory and scaffold the skill file (`foundry:curator` NOT-for excludes scaffolding new agents/skills — see Create Agent rationale above):

```markdown
Run: `mkdir -p .claude/skills/<name>` using the Bash tool.
Run `cat "<MANAGE_TPL>/skill-scaffold.md"` via the Bash tool (substitute resolved path from bash block above — do not pass literal `$MANAGE_TPL` to the agent).
Also read the schema file at the path returned in the step 1 JSON to incorporate any new frontmatter fields.
Run `cat "<_FOUNDRY_SHARED>/bin-authoring-guide.md"` (Bash tool; substitute resolved `$_FOUNDRY_SHARED`, echoed as `"Shared dir: <path>"`) and follow it — before any fenced code block in the new SKILL.md: extraction gate (verdict MEDIUM/HIGH → bin/ script instead), §Prose over Code check, §Script Output Routing for multi-value bin/ scripts.
Scaffold `.claude/skills/<name>/SKILL.md` with:
- Frontmatter: name=<name>, description=<description>; add other fields per schema and scaffold guidance
- Body: rich workflow scaffold derived from the description, following all content rules in the scaffold template
Write using the Write tool.
Return ONLY: {"status":"done","file":".claude/skills/<name>/SKILL.md","lines":N,"confidence":0.N}
```

Health monitoring §8b: `<ID>` = `sw-engineer-skill`, glob matching this agent's output files.

### Mode: Update Agent (rename)

Atomic rename — write new file before deleting old:

1. Read `.claude/agents/<old-name>.md` using the Read tool.

2. Write new file to `.claude/agents/<new-name>.md` using the Write tool (copy content of old file with `name:` line updated to `<new-name>`).

3. Verify new file exists and is valid: `Read(file_path=".claude/agents/<new-name>.md", limit=5)`

```bash
rm .claude/agents/<old-name>.md # timeout: 5000
```

### Mode: Update Skill (rename)

Atomic rename — create new directory before removing old:

1. Create new directory:

   ```bash
   mkdir -p .claude/skills/<new-name>  # timeout: 5000
   ```

2. Read old SKILL.md, update `name:` line in frontmatter, Write to new location.

   > After updating `name:` in frontmatter: also scan the new SKILL.md body for TRIGGER conditions, NOT-for lines, and example invocations that still reference the old skill name — update those inline with Edit tool before proceeding to Step 5.

3. Verify new file exists: `Read(file_path=".claude/skills/<new-name>/SKILL.md", limit=5)`

   ```bash
   rm -r .claude/skills/<old-name>  # timeout: 5000
   ```

### Mode: Delete Agent

```bash
rm .claude/agents/<name>.md # timeout: 5000
```

### Mode: Delete Skill

```bash
rm -r .claude/skills/<name>  # timeout: 5000
```

### Mode: Update Agent/Skill (content-edit)

Before executing type-specific content-edit mode, determine approach:

**File-type → agent routing:**

| File extension | Agent |
| -- | -- |
| `.md` (agents, skills, SKILL.md) | `foundry:curator` |
| `.js`, `.py`, `.ts`, `.sh` (code) | `foundry:sw-engineer` |
| Rule `.md` (under `rules/`) | inline Edit — no agent |

**If `EDIT_TRIVIAL=true`** (classified in Step 1):

1. Read file using Read tool
2. Apply directive directly using Edit tool — no agent spawn
3. Proceed to Step 8; skip Steps 5–7 unless name or description changed in edit

**If `EDIT_TRIVIAL=false`**: proceed to type-specific mode below for full agent-delegated edit.

### Mode: Content-Edit Agent

1. Determine change directive:
   - Quoted description → use as-is
   - Spec file path → Read spec file; use content as directive
2. Resolve `_FS_VAL` (concrete path) before constructing spawn prompt — sub-agents do not inherit shell variables, so the prompt must contain a literal path, not a `$VAR` reference:

```bash
_FS_VAL=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/resolve_shared_path.py" foundry skills/_shared 2>/dev/null || echo "plugins/cc_foundry/skills/_shared")  # timeout: 5000
echo "Shared dir for curator prompt: $_FS_VAL"
```

3. Spawn **foundry:curator** subagent — substitute `<_FS_VAL>` with the path from above when emitting the prompt:

```markdown
Read `.claude/agents/<name>.md`.
Apply this change: <directive>
Rules:
- Preserve frontmatter fields (name, description, tools, model, color) unless the change explicitly targets them
- Preserve XML tags (<role>, <workflow>, <notes>) — targeted edits only; do not rewrite unchanged sections
- If the change modifies the agent's purpose: update the description: frontmatter field
- Fenced code block added → run `cat "<_FS_VAL>/bin-authoring-guide.md"` (Bash tool), apply extraction gate (verdict MEDIUM/HIGH → bin/ script instead), §Prose over Code check, §Script Output Routing for multi-value bin/ scripts (all in guide just loaded)
- After editing: verify XML tag balance, step numbering, cross-ref validity
Write all changes using the Edit tool.
Return ONLY: {"status":"done","file":".claude/agents/<name>.md","edits":N,"description_changed":true|false,"confidence":0.N}
```

Use `description_changed` from returned JSON to decide whether Steps 5–7 need cross-ref propagation.

### Mode: Content-Edit Skill

1. Determine change directive (same as Content-Edit Agent).
2. Resolve `_FS_VAL` (concrete path) before constructing the spawn prompt:

```bash
_FS_VAL=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/resolve_shared_path.py" foundry skills/_shared 2>/dev/null || echo "plugins/cc_foundry/skills/_shared")  # timeout: 5000
echo "Shared dir for curator prompt: $_FS_VAL"
```

3. Spawn **foundry:curator** subagent — substitute `<_FS_VAL>` with the path from above:

```markdown
Read `.claude/skills/<name>/SKILL.md`.
Apply this change: <directive>
Rules:
- Preserve frontmatter fields (name, description, argument-hint, disable-model-invocation, allowed-tools)
- Preserve XML tags (<objective>, <inputs>, <workflow>, <notes>) — targeted edits only; do not rewrite unchanged sections
- If the change modifies the skill's purpose: update the description: frontmatter field
- Fenced code block added → run `cat "<_FS_VAL>/bin-authoring-guide.md"` (Bash tool), apply extraction gate (verdict MEDIUM/HIGH → bin/ script instead), §Prose over Code check, §Script Output Routing for multi-value bin/ scripts (all in guide just loaded)
- After editing: verify XML tag balance, step numbering, workflow gate completeness
Write all changes using the Edit tool.
Return ONLY: {"status":"done","file":".claude/skills/<name>/SKILL.md","edits":N,"description_changed":true|false,"confidence":0.N}
```

Use `description_changed` from returned JSON to decide whether Steps 5–7 need cross-ref propagation.

### Mode: Content-Edit Rule

1. Read `.claude/rules/<name>.md` using the Read tool.
2. Determine change directive (same as Content-Edit Agent).
3. Apply changes directly using the Edit tool:
   - Preserve YAML frontmatter (description, paths) unless change explicitly targets them
   - Rule files are free-form markdown with `##` sections — no XML tags
   - Targeted edits — do not rewrite unchanged sections
   - Adding new section: match heading level and style of existing sections
   - If change modifies rule's scope: also update `description:` and `paths:` frontmatter fields
   - After editing: verify YAML frontmatter valid, no broken internal references

### Mode: Create Rule

No schema fetch needed — rule files simpler than agents/skills (only frontmatter + free-form markdown sections).

**Rule scope guidance**: empty `paths:` = global rule (applies everywhere); populated `paths:` = scoped (e.g., `paths: ["src/**/*.py"]` for Python-only rules). Default to global unless rule is clearly language/directory-specific.

Write `.claude/rules/<name>.md` with this structure:

```markdown
---
description: <one-line description from user>
paths:
  - '<glob pattern matching the rule's scope>'
---

## <First Section Title>

[Real domain-specific rules derived from the description — not generic boilerplate. 20-60 lines total.]
```

Content rules:

- Generate real domain content from description (e.g., for "torch-patterns": actual PyTorch patterns, not generic "write clean code")
- Use `##` sections for major topics, bullets for individual rules
- Include code examples only when they carry domain-specific patterns
- Match tone and density of existing rules files (terse, imperative, no padding)

### Mode: Update Rule (rename)

Atomic update — write new file before deleting old:

1. Read `.claude/rules/<old-name>.md` using the Read tool.
2. Rule files have no `name:` frontmatter field — filename IS identifier. Write new file at `.claude/rules/<new-name>.md` with identical content.
3. Verify new file exists: `Read(file_path=".claude/rules/<new-name>.md", limit=5)`

```bash
rm .claude/rules/<old-name>.md  # timeout: 5000
```

### Mode: Delete Rule

```bash
rm .claude/rules/<name>.md # timeout: 5000
```

### Mode: Content-Edit Hook

Hook files are JavaScript — delegate to **foundry:sw-engineer** (not foundry:curator):

1. Determine change directive (same as Content-Edit Agent).
2. Spawn **foundry:sw-engineer** subagent:

```markdown
Read `.claude/hooks/<name>.js`.
Apply the hook authoring standards from the `<hook-authoring>` section in your agent definition — file-header structure, exit code semantics, stdin pattern, and anti-patterns.
Apply this change: <directive>
Rules:
- Preserve the file header block (PURPOSE, HOW IT WORKS, EXIT CODES) unless the change explicitly modifies that logic
- Preserve CommonJS require() style; do not convert to ESM
- stdin must use event-based accumulation (process.stdin.on("data"/"end")); never readFileSync("/dev/stdin")
- All subprocess calls must use execFileSync or spawnSync (args array — no execSync with shell strings)
- All logic must be wrapped in try/catch; catch always exits 0
- After editing: verify exit codes match documented cases, no shell injection surface added
Write all changes using the Edit tool.
Return ONLY: {"status":"done","file":".claude/hooks/<name>.js","edits":N,"confidence":0.N}
```

### Mode: Delete Hook

```bash
rm .claude/hooks/<name>.js  # timeout: 5000
```

After deleting the hook file, also remove its entry from `.claude/settings.json` so Claude Code does not invoke a missing script. Identify the hook's matcher pattern (the entry's `matcher` field, or `command` substring containing the deleted filename) and run jq to strip every block referencing it. Substitute `<name>` with the deleted hook's basename (no `.js` suffix):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# timeout: 5000
HOOK_NAME="<name>"        # e.g. "rtk-rewrite" — no .js suffix
echo "$HOOK_NAME" > "${TMPDIR:-/tmp}/manage-hook-name-${CSID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/remove_hook_from_registry.py" \
    --json-file .claude/settings.json \
    --hook-name "$HOOK_NAME" \
    --path-pattern "\\.claude/hooks/${HOOK_NAME}\\.js"
```

Verify the entry is gone:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r HOOK_NAME < "${TMPDIR:-/tmp}/manage-hook-name-${CSID}" 2>/dev/null || HOOK_NAME=""
jq --arg hook "$HOOK_NAME" '[.. | objects | select(.command? // "" | test($hook + "\\.js"))] | length' .claude/settings.json  # timeout: 5000
# Expected output: 0
```

**Also update plugin `hooks.json` registry** — if the deleted hook was registered in the foundry plugin's own `hooks.json`, `/foundry:setup` would re-add it to `settings.json` on the next sync, resurrecting a broken reference. Strip the matching entry from the plugin registry too:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# timeout: 5000
IFS= read -r HOOK_NAME < "${TMPDIR:-/tmp}/manage-hook-name-${CSID}" 2>/dev/null || HOOK_NAME=""
PLUGIN_HOOKS_JSON="${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/hooks/hooks.json"
if [ -f "$PLUGIN_HOOKS_JSON" ]; then
    python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/remove_hook_from_registry.py" \
        --json-file "$PLUGIN_HOOKS_JSON" \
        --hook-name "$HOOK_NAME" \
        --path-pattern "${HOOK_NAME}\\.js"
    jq --arg hook "$HOOK_NAME" '[.. | objects | select(.command? // "" | test($hook + "\\.js"))] | length' "$PLUGIN_HOOKS_JSON"  # expected: 0
else
    printf "  (no plugin hooks.json at %s — skipping registry cleanup)\n" "$PLUGIN_HOOKS_JSON"
fi
```

### Mode: Add Permission

Adds rule to both `settings.json` and `permissions-guide.md` atomically.

1. Determine guide category from rule prefix:

   > **Agent budget** — each spawn costs ~120,851 tok of fixed overhead (~73 tool-calls' worth) plus ~12.0 s/call, so work under ~73 calls is cheaper done inline: spawn nothing. Keep each agent near ~55 tool-calls; past ~60 they stall without returning an envelope, forcing reconstruction from disk. Every spawn prompt must require an envelope even on exhaustion — `partial: true` plus what was finished.

   - `WebSearch` → `## Web`
   - `WebFetch(domain:...)` → `## WebFetch — allowed domains`
   - `WebFetch` (bare, no domain) → `## WebFetch — allowed domains`
   - `Agent(subagent_type:*)` → `## Shell utilities` (agent dispatch rules)
   - `mcp__*` → `## Shell utilities` (MCP tool rules)
   - `Bash(gh ...)` → `## GitHub CLI — read-only`
   - `Bash(git log:*)`, `Bash(git show:*)`, `Bash(git diff:*)`, `Bash(git rev-*:*)`, `Bash(git ls-*:*)`, `Bash(git -C:*)`, `Bash(git branch:*)`, `Bash(git tag:*)`, `Bash(git status:*)`, `Bash(git describe:*)`, `Bash(git shortlog:*)` → `## Git — read-only`
   - `Bash(git add:*)`, `Bash(git checkout:*)`, `Bash(git stash:*)`, `Bash(git restore:*)`, `Bash(git clean:*)`, `Bash(git apply:*)` → `## Git — local write`
   - `Bash(pytest:*)`, `Bash(python ...)`, `Bash(ruff:*)`, `Bash(mypy:*)`, `Bash(pip ...)` → `## Python toolchain`
   - `Bash(brew ...)`, `Bash(codex:*)` → `## macOS / ecosystem`
   - All other `Bash(...)` → `## Shell utilities`

2. Update `settings.json` — atomic jq edit via shared helper:

   ```bash
   # timeout: 15000
   python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/jq_write.py" .claude/settings.json '.permissions.allow += [$rule]' --arg rule "<rule>"
   ```

3. Also append to plugin's `permissions-allow.json` so `/foundry:setup` syncs it to `~/.claude/settings.json` on reinstall:

   ```bash
   # timeout: 5000
   PERM_FILE="${CLAUDE_PLUGIN_ROOT}/.claude-plugin/permissions-allow.json"
   if [ -f "$PERM_FILE" ]; then
       python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/jq_write.py" "$PERM_FILE" '. += [$rule] | unique' --arg rule "<rule>"
   fi
   ```

4. Update `permissions-guide.md` — append new row to end of correct section (before its trailing `---` separator). New row format:

   ```markdown
   | `<rule>` | <description> | <use case> |
   ```

   Use Edit tool to insert row: find last table row in target section and insert after it.

5. Verify both files updated:

   ```bash
   # timeout: 5000
   python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/verify_perm.py" "<rule>" .claude/settings.json .claude/permissions-guide.md present
   # Exits 0 if both consistent; prints "settings: OK|MISSING" + "guide: OK|MISSING"
   ```

### Mode: Remove Permission

Removes rule from both `settings.json` and `permissions-guide.md` atomically.

1. Update `settings.json` — atomic jq edit via shared helper:

   ```bash
   # timeout: 15000
   python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/jq_write.py" .claude/settings.json 'del(.permissions.allow[] | select(. == $rule))' --arg rule "<rule>"
   ```

2. Update `permissions-guide.md` — use Edit tool to remove table row containing `` `<rule>` ``.

3. Verify both files clean:

   ```bash
   # timeout: 5000
   python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/verify_perm.py" "<rule>" .claude/settings.json .claude/permissions-guide.md absent
   # Exits 0 if both consistent; prints "settings: OK|STILL_PRESENT" + "guide: OK|STILL_PRESENT"
   ```

## Step 5: Propagate cross-references

Search all `.claude/` markdown files for changed name and update references:

Use Grep to find all references:

- Pattern `<name>`, glob `agents/*.md`, path `.claude/`, output mode `content`
- Pattern `<name>`, glob `skills/*/SKILL.md`, path `.claude/`, output mode `content`
- Pattern `<name>`, glob `rules/*.md`, path `.claude/`, output mode `content`
- Pattern `<name>`, file `.claude/CLAUDE.md`, output mode `content`
- Pattern `<name>`, file `README.md`, output mode `content`

**For update (rename):** Count files grep returns. **≤ 3 files**: apply inline with Edit tool. **> 3 files**: spawn **foundry:curator** subagent. For hook renames: also update hook entry in `.claude/settings.json` `hooks` array if hook filename referenced there by path.

```text
Apply these cross-reference updates (<old-name> → <new-name>):
<list each file path with the required substitution>
Use the Edit tool for each file (replace_all: true where appropriate).
Return ONLY: {"status":"done","files_updated":N}
```

**For delete:** Review each reference. Deleted name in:

- Cross-ref suggestion — remove or replace with closest alternative
- Inventory list — remove entry
- Workflow spawn directive — flag for manual review

**For create:** No cross-ref propagation needed.

**For content-edit:** Run propagation only if entity's `description:` frontmatter changed — propagate new description to any MEMORY.md or README summary lines that quote it. Skip if only internal content changed.

**Severity/priority labeling** — for every cross-reference decision above (rename fix, delete flag, content-edit propagate-or-skip), state a priority: **high** (genuine reference — invocation, routing, or discovery breaks if missed) or **low** (cosmetic/consistency-only, e.g. mention inside a code example or an unrelated similarly-named entity). Carry the priority into the Step 10 report (Files Changed table / Rename Occurrence Validation buckets).

### Rename occurrence validation (rename mode only)

```bash
# loads: rename-validation.md
MANAGE_MODES=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/resolve_skill_subdir.py" manage modes 2>/dev/null || echo "plugins/cc_foundry/skills/manage/modes")  # timeout: 5000
cat "$MANAGE_MODES/rename-validation.md"
```

Execute the mode loaded above.

## Step 6: Update MEMORY.md roster (auto-memory)

MEMORY.md is Claude Code's auto-memory file — **not** stored under `.claude/`. Injected into conversation context at session start. Absolute path appears near top of system prompt (e.g. `~/.claude/projects/.../memory/MEMORY.md`). Use that absolute path with Edit tool. If system prompt parsing fails or path absent, fall back to:

```bash
# Claude Code auto-memory: / and . → - for path slugs
MEMORY_FALLBACK=~/.claude/projects/$(pwd | sed 's|[/.]|-|g' | sed 's/^-//')/memory/MEMORY.md
[ -f "$MEMORY_FALLBACK" ] && echo "Fallback MEMORY.md: $MEMORY_FALLBACK" || echo "MEMORY.md not found at fallback path — skip roster update"  # timeout: 5000
```

Regenerate inventory lines from disk:

Use Glob (`agents/*.md`, path `.claude/`) for agents, Glob (`skills/*/`, path `.claude/`) for skills, Glob (`rules/*.md`, path `.claude/`) for rules. Extract names inline from returned paths (strip path prefix and `.md`/trailing-`/` suffix), join as comma-separated string.

Use Edit tool with **absolute auto-memory path** to update these roster lines in MEMORY.md:

- `- Agents: doc-scribe, foundry:sw-engineer, ...`
- `- Skills: review, research, ...`
- `- Rules (N): artifact-lifecycle, ...` (update count N when rules created or deleted)

**For content-edit:** Skip if only internal content changed; update only if description changed.

## Step 7: Update README.md

**`README.md` (project root):**

- **create agent**: add row to `### Agents` table — columns: `| **name** | Short tagline | Key capabilities |`
- **create skill**: add row to `### Skills` table — columns: `` | **name** | `/name` | Description | ``
- **update (rename)**: find and replace old name in table row
- **delete**: remove row for deleted name

**`.claude/README.md` (config README) — Rules table only:**

- **create rule**: add row to Rules reference table — columns: `| rule-file | Applies to | What it governs |`
- **update rule (rename)**: replace old name in Rules table row
- **update rule (content-edit)**: update "What it governs" column if rule's description changed
- **delete rule**: remove row for deleted rule

Keep descriptions concise (one line), consistent in tone with surrounding rows. Do not add/remove table columns.

**For content-edit (agent/skill):** Update README if description OR model field changed. Model changes update the Model column only; description changes update the description column.

## Step 8: Verify integrity

Confirm no broken references remain:

Use Grep (pattern `[a-z]+:[a-z]+(-[a-z]+)*` to find cross-plugin references, or `See [a-z-]+ agent` for cross-references, glob `{agents/*.md,skills/*/SKILL.md}`, path `.claude/`, output mode `content`). Avoid broad kebab-case patterns — they match code examples and produce false positives.

Use Glob (`agents/*.md`, path `.claude/`) and Glob (`skills/*/`, path `.claude/`) for on-disk inventory; extract names inline. Use Grep to search for changed name and confirm:

- **Update (rename)**: zero unresolved genuine references to old name; documented false positives (accepted by user in Step 5 validation) may remain and must be listed in Step 10
- **Delete**: zero hits for deleted name (or flagged references noted)
- **Create**: new file exists with valid structure
- **Content-edit**: target file has valid structure (XML tag balance for agents/skills; YAML frontmatter for rules)

Add rules to on-disk inventory check: Glob (`rules/*.md`, path `.claude/`), extract names inline.

For **create** and **update (rename)**: verify tool efficiency — cross-check agent/skill's declared tools (`tools:` or `allowed-tools:`) against tool names in workflow body. Declared tool not referenced anywhere → flag as cleanup candidate in Step 10 report (report only — do not block operation).

## Step 9: Audit and calibrate

Invoke `Skill(skill="foundry:audit", args="--skip-gate")` to validate created/modified files without triggering interactive follow-up gate (requires `foundry` plugin). **Skip if invoked with `--skip-audit` or if current `manage` operation runs inside audit-initiated fix session** — outer audit covers it.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _SKIP_FILE < "${TMPDIR:-/tmp}/manage-skip-audit-path-${CSID}" 2>/dev/null || _SKIP_FILE="${TMPDIR:-/tmp}/manage-skip-audit-${CSID}"
IFS= read -r SKIP_AUDIT < "$_SKIP_FILE" 2>/dev/null || SKIP_AUDIT="false"  # reload (Check 41)
[[ "$SKIP_AUDIT" == "true" ]] && { echo "[--skip-audit] skipping Step 9 audit"; }
```

For targeted check of only affected file, spawn **foundry:curator** directly:

- For `create`: audit new file for structural completeness, cross-ref validity, content quality
- For `update`: audit renamed file, verify no stale references remain
- For `delete`: audit remaining files for broken references to deleted name

Include audit findings in final report. Do not proceed to sync if any `critical` findings remain.

**Calibration** — invoke `Skill(skill="foundry:calibrate", args="<name>")` after audit passes (requires `foundry` plugin). **Mandatory only when the edit changes the routing surface** — an agent/skill `description:` field, or a `TRIGGER`/`SKIP`/`NOT-for` line. For pure body/prose edits that leave the routing surface untouched (workflow steps, examples, `<notes>`, wording polish), calibration is **optional** — suggest it, don't force it. Routing accuracy is unaffected by non-routing edits, so the 10–30 min run rarely pays off; skipping keeps small polishes cheap.

Then, **only when the routing surface changed**, invoke `Skill(skill="foundry:calibrate", args="routing --fast")` to confirm overall routing accuracy unaffected (requires `foundry` plugin). Skip this for pure body/prose edits.

Skip calibration entirely for: trivial edits, renames, deletes, rule operations, perm operations.

## Step 10: Summary report

- **Operation**: what was done (create/update/delete + type + name, or add/remove perm + rule)
- **Files Changed**: table of file paths and actions (created/renamed/deleted/cross-ref updated/appended/removed)
- **Cross-References**: count of files updated, broken refs cleaned (n/a for perm operations)
- **Rename Occurrence Validation** (rename mode only): three buckets — **Fixed** (N genuine references updated, list files) · **False positives** (N skipped, list file + one-line reason each) · **User-resolved** (N ambiguous, list file + user decision); if any genuine references could not be cleanly resolved, flag explicitly as requiring manual review
- **Current Roster**: agents (N) and skills (N) with comma-separated names (n/a for perm operations)
- **Audit Result**: audit findings (pass / issues found) (n/a for perm operations)
- **Calibration Result**: recall score and routing accuracy from Step 9 (n/a for trivial edits, renames, deletes, perms, and pure body/prose edits that left the routing surface untouched — note calibration was suggested-but-skipped in that case)
- **Follow-up**: perm ops → confirm both `settings.json` and `permissions-guide.md` updated; run `/foundry:setup` to sync `~/.claude/`

End response with `## Confidence` block per CLAUDE.md output standards.

**Challenger review gate** — after emitting the summary report and Confidence block, invoke `AskUserQuestion` to offer adversarial review of the just-completed changes. Skip this gate entirely when: `MODE` is `delete`, `add-perm`, or `remove-perm`; OR `EDIT_TRIVIAL=true`; OR `MODE` is `rename` (structural change only, no content to challenge). Gate is mandatory for: `create` (agent, skill, rule), non-trivial `content-edit` (agent, skill, rule).

```text
AskUserQuestion: "Run foundry:challenger to adversarially review the changes just made?"
  (a) Skip — done  [default]
  (b) Challenge — spawn foundry:challenger on modified file(s)
```

On **(b)**: spawn `foundry:challenger` inline (foreground, not background):

```text
Agent(subagent_type="foundry:challenger", prompt="Adversarially review the changes just made to <list modified file paths>. Challenge: correctness of design decisions, completeness, potential regressions, and whether the stated goal was achieved. Read-only. Write full findings to .temp/manage-challenger-<YYYY-MM-DD>.md using the Write tool. Return ONLY: {\"status\":\"done\",\"file\":\".temp/manage-challenger-<YYYY-MM-DD>.md\",\"findings\":N,\"confidence\":0.N}")
```

Print challenger's `findings` count and confidence; note any HIGH findings that warrant a follow-up `/manage update` pass. On **(a)**: print `→ Done.` and stop.

**Cycle guard**: this challenger dispatch is from the orchestrator (manage skill itself), not from a sub-agent — cycle detection in `<notes>` does not apply here. Do NOT spawn challenger from inside foundry:curator or foundry:sw-engineer sub-agents spawned by manage.

</workflow>

<notes>

- **Atomic updates**: write-before-delete prevents data loss on interruption; perm ops must update both `settings.json` and `permissions-guide.md`
- **settings.json format**: jq with atomic tmp-file pattern (`jq ... > .tmp && mv .tmp dest`) — avoids fragile sed/awk on JSON; indent=2 via `jq --indent 2` when formatting required
- **README.md tables**: agent/skill tables in project `README.md`; rules table in `.claude/README.md` — keep row format consistent with existing rows
- **No auto-edit for agent/skill/rule operations**: skill does not mutate settings.json for non-perm operations
- **Color pool**: AVAILABLE_COLORS lists unused colors; if exhausted, reuse with note
- **Inline bash / extraction gate + prose compression**: before writing any fenced bash block directly into a `.md` file (agent, skill, rule) via Edit/Write, apply two checks from `bin-authoring-guide.md`: (1) extraction gate — verdict MEDIUM or HIGH → write a `bin/` script instead; verdict LOW → inline is acceptable; (2) Prose over Code — if `tokens(block) > tokens(equivalent prose/table/schema)` at identical precision → write prose/table instead. Exempt: examples, templates, exact-syntax blocks. Same rules enforced in spawn prompts to foundry:curator and foundry:sw-engineer (see Content-Edit Agent/Skill modes). This note applies to orchestrator-level inline edits.
- **Cycle detection**: sub-tasks spawned by manage (foundry:sw-engineer, foundry:curator, foundry:doc-scribe) must not invoke manage again. Circular dispatch — manage→sw-engineer→curator→manage — causes infinite loops. If a sub-task needs manage capabilities, surface the need back to the orchestrator; never chain manage from inside a manage-spawned sub-agent.
- Follow-up chains:
  - create or non-trivial update of agent/skill → `Skill(skill="foundry:audit", args="--skip-gate")` → `Skill(skill="foundry:calibrate", args="<name>")` (mandatory) → `Skill(skill="foundry:calibrate", args="routing --fast")`
  - trivial update or rename or delete → `Skill(skill="foundry:audit", args="--skip-gate")` → `Skill(skill="foundry:calibrate", args="routing --fast")` (if description changed)
  - add/remove perm → confirm both files updated; run `/foundry:setup`

</notes>
