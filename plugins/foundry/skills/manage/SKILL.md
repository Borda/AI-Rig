---
name: manage
description: Create, update, or delete agents, skills, rules, and hooks with full cross-reference propagation. Trivial edits (typos, small fixes ≤10 words) applied inline without agent; `.md` content-edits delegated to foundry:curator; code file edits (`.js`, `.py`, `.ts`) delegated to foundry:sw-engineer; large cross-ref fan-outs (> 3 files) also delegate. The parent orchestrates MEMORY.md, README, audit, calibration, and the final report. Also manages settings.json permissions atomically with permissions-guide.md.
argument-hint: create <agent|skill|rule> <name> "desc" | update <name> [new-name|"change"|spec.md] | delete <name> | add perm <rule> "desc" "use-case" | remove perm <rule>
disable-model-invocation: true
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, Agent, TaskCreate, TaskUpdate, AskUserQuestion
effort: high
---

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
- Descriptions must be quoted when they contain spaces
- Permission rules use Claude Code format: `WebSearch`, `Bash(cmd:*)`, `WebFetch(domain:example.com)`
- `--skip-audit` — optional flag: skip Step 9 `/audit` validation (use inside `audit fix` loop to avoid recursion)

**Update/delete mode** — name looked up across agents, skills, rules automatically:

- One match on disk → proceed with that type
- Multiple matches → `AskUserQuestion`: (a) agent, (b) skill, (c) rule
- No match → report error and stop

**Update second-argument discrimination**:

- Two bare kebab-case args (second arg no spaces, no `.md` extension) → **rename mode**
- One name + quoted string → **content-edit mode** (trivial → inline; `.md`: foundry:curator; code `*.js`/`*.py`/`*.ts`: foundry:sw-engineer; rule: inline)
- One name + path ending in `.md` → **content-edit mode** (trivial → inline; `.md`: foundry:curator; code `*.js`/`*.py`/`*.ts`: foundry:sw-engineer; rule: inline)

**Examples:**

- `/manage create agent task-planner "Planning specialist for decomposing epics into actionable tasks"`
- `/manage create skill benchmark "Benchmark orchestrator for measuring and comparing performance across commits"`
- `/manage create rule torch-patterns "PyTorch coding patterns — compile, AMP, distributed"`
- `/manage update example-agent example-agent-v2`
- `/manage update my-agent "add a section on error handling patterns"`
- `/manage update optimize docs/specs/YYYY-MM-DD-<spec-name>.md`
- `/manage update testing "add a section on snapshot testing with syrupy"`
- `/manage delete old-agent-name`
- `/manage add perm "Bash(jq:*)" "Parse and filter JSON" "Extract fields from REST API responses"`
- `/manage remove perm "Bash(jq:*)"`

</inputs>

<constants>

- AGENTS_DIR: `.claude/agents`
- SKILLS_DIR: `.claude/skills`
- RULES_DIR: `.claude/rules`
- HOOKS_DIR: `.claude/hooks`
- USED_COLORS: blue, cyan, green, orange, pink, purple, yellow
- AVAILABLE_COLORS: indigo, lime, magenta, teal, violet

Maintain colors manually — add new agent colors here when creating agents; this static list is advisory only — live Grep in Step 3 is authoritative check for colors in use.

</constants>

<workflow>

**Task hygiene**: call `TaskList` first; close orphaned tasks. **Task tracking**: create tasks for each major phase; mark in_progress/completed throughout.

## Step 1: Parse and validate

Extract operation, type, name, optional arguments from `$ARGUMENTS`.

```bash
# Parse --skip-audit flag before other argument processing
SKIP_AUDIT=false
[[ "$ARGUMENTS" == *"--skip-audit"* ]] && SKIP_AUDIT=true
ARGUMENTS="${ARGUMENTS//--skip-audit/}"
ARGUMENTS="${ARGUMENTS#"${ARGUMENTS%%[![:space:]]*}"}"
```

**Validation rules:**

- Name must match `^[a-z][a-z0-9-]*$` (kebab-case)
- For `create`: name must NOT already exist on disk; description required
- For `update`/`delete`: name MUST already exist on disk
- For `update` rename: new-name must NOT already exist on disk
- For `add perm`: rule must NOT already exist in settings.json allow list; description and use case required
- For `remove perm`: rule MUST already exist in settings.json allow list

**Type auto-detection** (for `update` and `delete`): run all four Glob checks in parallel:

- Agent: pattern `agents/<name>.md`, path `.claude/`
- Skill: pattern `skills/<name>/SKILL.md`, path `.claude/`
- Rule: pattern `rules/<name>.md`, path `.claude/`
- Hook: pattern `hooks/<name>.js`, path `.claude/`

Results:

- One non-empty result → resolved type; proceed
- Multiple non-empty results → `AskUserQuestion`: "Multiple entities named `<name>` found. Which one? (a) agent (b) skill (c) rule (d) hook"
- All empty → report "No agent, skill, rule, or hook named `<name>` found" and stop

For `create`, check only the relevant type's path.

```bash
# Check permission existence (for add perm / remove perm)
python3 -c "import json,sys; d=json.load(open('.claude/settings.json')); sys.exit(0 if '<rule>' in d['permissions']['allow'] else 1)" 2>/dev/null  # timeout: 15000
```

**Update second-argument discrimination** — apply after type resolved:

- Two bare kebab-case arguments (second arg no spaces, no `.md` extension) → **rename mode**: validate new-name does NOT already exist
- One name + quoted string → **content-edit mode**: validate spec non-empty; no new-name uniqueness check
- One name + path ending in `.md` → **content-edit mode**: validate spec file exists on disk; no new-name uniqueness check

If validation fails, report error and stop.

**Edit complexity classification** (content-edit mode only):

```bash
# Classify directive as trivial vs substantive
EDIT_TRIVIAL=false
if [[ "$MODE" == "content-edit" ]]; then
  WORD_COUNT=$(echo "$DIRECTIVE" | wc -w)
  if [[ "$WORD_COUNT" -le 10 ]]; then
    echo "$DIRECTIVE" | grep -qiE '(typo|spelling|rename .+ to .+|change .+ to .+|replace .+ with .+|fix (a |the )?(typo|bug|error)|add missing|remove (the |a )?[a-z]+|correct)' \
      && EDIT_TRIVIAL=true
  fi
fi
```

Trivial = directive ≤10 words AND matches a simple-change pattern. Trivial edits: apply inline with Edit tool — no agent spawn.

**Step skip rules**:

- **Perm operations**: skip Steps 2, 3, 5, 6, 7, 8, 9 — go Step 1 → Step 4 → Step 10
- **Hook operations**: skip Steps 2, 3, 6 (no color inventory, no MEMORY.md roster entry, no README table row); in Steps 5 and 7 skip cross-ref propagation (hook filenames not referenced from agent/skill markdown) — go Step 1 → Step 4 → Step 9 → Step 10
- **Content-edit operations**: skip Step 2 (entity already exists); skip Step 3 color inventory (no create); in Steps 5–7 only update cross-refs and README if name or description changed
- **Trivial content-edits**: additionally skip Steps 6–7 (no roster/description change possible); proceed Step 1 → Step 4 → Step 8 → Step 10

## Step 2: Overlap review (create only)

Before creating, check if existing agents/skills already cover requested functionality:

1. Read descriptions of all existing agents (use `Read(file_path=..., limit=3)` on each `.md` in agents/) and skills (use `Read(file_path=..., limit=3)` on each `SKILL.md`)
2. Compare new description against each existing — look for domain overlap, similar workflows, redundant scope
3. Present findings:
   - **No overlap**: proceed to Step 3
   - **Partial overlap**: name overlapping agent/skill, explain what it covers vs what new one adds, use `AskUserQuestion`: "Extend existing (Recommended)" / "Proceed" / "Abort"
   - **Strong overlap**: recommend against creation — suggest using or extending existing agent/skill

Skip for `update`, `delete`, perm operations.

## Step 3: Inventory current state

Snapshot current roster for later comparison. Steps 2 and 3 are independent reads — issue Glob calls for both in same response.

Use Glob (pattern `agents/*.md`, path `.claude/`) for agents and Glob (pattern `skills/*/`, path `.claude/`) for skills. Use Grep (pattern `^color:`, glob `agents/*.md`, path `.claude/`, output mode `content`) to collect colors in use.

Extract names inline from Glob results — strip `.claude/agents/` prefix and `.md` suffix for agents; strip `.claude/skills/` prefix and trailing `/` for skills; strip `.claude/rules/` prefix and `.md` suffix for rules. Sort alphabetically when building roster string.

## Step 4: Execute operation

### Mode: Create Agent

1. Fetch latest Claude Code agent frontmatter schema:

   - Spawn **foundry:web-explorer** to fetch `https://code.claude.com/docs/en/sub-agents` with instruction: "Write your full findings (schema fields, new fields, deprecated fields) to `/tmp/manage-schema-$(date +%s).md` using the Write tool. Return ONLY a compact JSON envelope on your final line — nothing else after it: `{\"status\":\"done\",\"file\":\"/tmp/manage-schema-<ts>.md\",\"fields\":N,\"new\":N,\"deprecated\":N,\"confidence\":0.N,\"summary\":\"N fields, N new, N deprecated\"}`" <!-- URL verified -->

     <!--
     Health monitoring (CLAUDE.md §8): create checkpoint after spawn:
     LAUNCH_AT=$(date +%s); touch /tmp/manage-check-web-explorer
     Every 5 min: find /tmp -newer /tmp/manage-check-web-explorer -name "manage-schema-*.md" | wc -l
     Hard cutoff: 15 min of no activity → surface partial results with ⏱
     -->

   - Read returned summary; extract: valid frontmatter fields (`name`, `description`, `tools`, `disallowedTools`, `model`, `permissionMode`, `maxTurns`, `effort`, `initialPrompt`, `skills`, `mcpServers`, `hooks`, `memory`, `background`, `isolation`), current model shorthands, new fields
   - Note new fields worth including. Adjust template to reflect current schema. If new field broadly useful for agent's role (e.g. `maxTurns` for long-running agents), include with sensible default and inline comment.

2. Pick first unused color from AVAILABLE_COLORS pool (compare against Step 3 colors)

3. Choose model based on role complexity:

   - `opusplan` — plan-gated roles (solution-architect, oss:shepherd, foundry:curator)
   - `opus` — complex implementation roles (foundry:sw-engineer, foundry:qa-specialist, research:scientist, foundry:perf-optimizer)
   - `sonnet` — focused execution roles (research:data-steward, foundry:web-explorer, foundry:doc-scribe)
   - `haiku` — high-frequency diagnostics roles (linting-expert, oss:ci-guardian)

4. Spawn **foundry:curator** subagent to generate and write agent file:

```markdown
Read the agent scaffold template at `.claude/skills/manage/templates/agent-scaffold.md`.
Also read the schema file at the path returned in the step 1 JSON to incorporate any new frontmatter fields.
Create `.claude/agents/<name>.md` with:
- Frontmatter: name=<name>, description=<description>, model=<model>, color=<color>; add any broadly-useful new fields from the schema
- Body: rich domain-specific content for the role described by the description, following all content rules and tool selection guidelines in the scaffold template
Write the file using the Write tool.
Return ONLY: {"status":"done","file":".claude/agents/<name>.md","lines":N,"confidence":0.N}
```

<!-- Health monitoring (CLAUDE.md §8): create checkpoint after spawn:
     LAUNCH_AT=$(date +%s); touch /tmp/manage-check-curator-agent
 Every 5 min: find .claude/agents -newer /tmp/manage-check-curator-agent -name "<name>.md" | wc -l
     Hard cutoff: 15 min of no activity → surface partial results with ⏱ -->

### Mode: Create Skill

1. Fetch latest Claude Code skill frontmatter schema:

   - Spawn **foundry:web-explorer** to fetch `https://code.claude.com/docs/en/skills` with instruction: "Write your full findings (schema fields, new fields, deprecated fields) to `/tmp/manage-skill-schema-$(date +%s).md` using the Write tool. Return ONLY a compact JSON envelope on your final line — nothing else after it: `{\"status\":\"done\",\"file\":\"/tmp/manage-skill-schema-<ts>.md\",\"fields\":N,\"new\":N,\"deprecated\":N,\"confidence\":0.N,\"summary\":\"N fields, N new, N deprecated\"}`" <!-- URL verified -->

     <!--
     Health monitoring (CLAUDE.md §8): create checkpoint after spawn:
     LAUNCH_AT=$(date +%s); touch /tmp/manage-check-web-explorer-skill
     Every 5 min: find /tmp -newer /tmp/manage-check-web-explorer-skill -name "manage-skill-schema-*.md" | wc -l
     Hard cutoff: 15 min of no activity → surface partial results with ⏱
     -->

   - Read returned summary; extract: valid frontmatter fields (`name`, `description`, `argument-hint`,`disable-model-invocation`, `user-invocable`, `allowed-tools`, `model`, `effort`, `shell`, `paths`, `context`, `agent`, `hooks`), new fields
   - Note new fields worth including. Adjust template to reflect current schema. Include `model` or `context: fork` only when skill's purpose clearly benefits.

2. Spawn **foundry:curator** subagent to create directory and generate skill file:

```markdown
Run: `mkdir -p .claude/skills/<name>` using the Bash tool.
Read the skill scaffold template at `.claude/skills/manage/templates/skill-scaffold.md`.
Also read the schema file at the path returned in the step 1 JSON to incorporate any new frontmatter fields.
Create `.claude/skills/<name>/SKILL.md` with:
- Frontmatter: name=<name>, description=<description>; add other fields per schema and scaffold guidance
- Body: rich workflow scaffold derived from the description, following all content rules in the scaffold template
Write using the Write tool.
Return ONLY: {"status":"done","file":".claude/skills/<name>/SKILL.md","lines":N,"confidence":0.N}
```

<!-- Health monitoring (CLAUDE.md §8): create checkpoint after spawn:
     LAUNCH_AT=$(date +%s); touch /tmp/manage-check-curator-skill
 Every 5 min: find .claude/skills -newer /tmp/manage-check-curator-skill -name "SKILL.md" | wc -l
     Hard cutoff: 15 min of no activity → surface partial results with ⏱ -->

### Mode: Update Agent

Atomic update — write new file before deleting old:

1. Read `.claude/agents/<old-name>.md` using the Read tool.

2. Write new file to `.claude/agents/<new-name>.md` using the Write tool (copy content of old file with `name:` line updated to `<new-name>`).

3. Verify new file exists and is valid: `Read(file_path=".claude/agents/<new-name>.md", limit=5)`

```bash
# 4. Delete old file only after new file is confirmed
rm .claude/agents/<old-name>.md # timeout: 5000
```

### Mode: Update Skill

Atomic update — create new directory before removing old:

1. Create new directory:

```bash
mkdir -p .claude/skills/<new-name>  # timeout: 5000
```

2. Read old SKILL.md, update `name:` line in frontmatter, Write to new location.
3. Verify new file exists: `Read(file_path=".claude/skills/<new-name>/SKILL.md", limit=5)`

```bash
# 4. Remove old directory only after new is confirmed
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

### Dispatch: Content-Edit

Before executing a type-specific content-edit mode, determine approach:

**File-type → agent routing:**

| File extension | Agent |
| --- | --- |
| `.md` (agents, skills, SKILL.md) | `foundry:curator` |
| `.js`, `.py`, `.ts`, `.sh` (code) | `foundry:sw-engineer` |
| Rule `.md` (under `rules/`) | inline Edit — no agent |

**If `EDIT_TRIVIAL=true`** (classified in Step 1):
1. Read file using Read tool
2. Apply directive directly using Edit tool — no agent spawn
3. Proceed to Step 8; skip Steps 5–7 unless name or description changed in the edit

**If `EDIT_TRIVIAL=false`**: proceed to type-specific mode below for full agent-delegated edit.

### Mode: Content-Edit Agent

1. Determine change directive:
   - Quoted description → use as-is
   - Spec file path → Read spec file; use content as directive
2. Spawn **foundry:curator** subagent:

```markdown
Read `.claude/agents/<name>.md`.
Apply this change: <directive>
Rules:
- Preserve frontmatter fields (name, description, tools, model, color) unless the change explicitly targets them
- Preserve XML tags (<role>, <core_knowledge>, <workflow>, <notes>) — targeted edits only; do not rewrite unchanged sections
- If the change modifies the agent's purpose: update the description: frontmatter field
- After editing: verify XML tag balance, step numbering, cross-ref validity
Write all changes using the Edit tool.
Return ONLY: {"status":"done","file":".claude/agents/<name>.md","edits":N,"description_changed":true|false,"confidence":0.N}
```

Use `description_changed` from returned JSON to decide whether Steps 5–7 need cross-ref propagation.

### Mode: Content-Edit Skill

1. Determine change directive (same as Content-Edit Agent).
2. Spawn **foundry:curator** subagent:

```markdown
Read `.claude/skills/<name>/SKILL.md`.
Apply this change: <directive>
Rules:
- Preserve frontmatter fields (name, description, argument-hint, disable-model-invocation, allowed-tools)
- Preserve XML tags (<objective>, <inputs>, <workflow>, <notes>) — targeted edits only; do not rewrite unchanged sections
- If the change modifies the skill's purpose: update the description: frontmatter field
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
3. Verify new file exists.
4. Delete old file only after new file confirmed: `rm .claude/rules/<old-name>.md` <!-- timeout: 5000 -->

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
Apply the hook authoring standards from the `\<hook_authoring>` section in your agent definition — file-header structure, exit code semantics, stdin pattern, and anti-patterns.
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
rm .claude/hooks/<name>.js # timeout: 5000
```

> After deleting hook, also remove its entry from `.claude/settings.json` hooks configuration so Claude Code does not try to invoke missing file.

### Mode: Add Permission

Adds rule to both `settings.json` and `permissions-guide.md` atomically.

1. Determine guide category from rule prefix:

   - `WebSearch` → `## Web`
   - `WebFetch(domain:...)` → `## WebFetch — allowed domains`
   - `Bash(gh ...)` → `## GitHub CLI — read-only`
   - `Bash(git log:*)`, `Bash(git show:*)`, `Bash(git diff:*)`, `Bash(git rev-*:*)`, `Bash(git ls-*:*)`, `Bash(git -C:*)`, `Bash(git branch:*)`, `Bash(git tag:*)`, `Bash(git status:*)`, `Bash(git describe:*)`, `Bash(git shortlog:*)` → `## Git — read-only`
   - `Bash(git add:*)`, `Bash(git checkout:*)`, `Bash(git stash:*)`, `Bash(git restore:*)`, `Bash(git clean:*)`, `Bash(git apply:*)` → `## Git — local write`
   - `Bash(pytest:*)`, `Bash(python ...)`, `Bash(ruff:*)`, `Bash(mypy:*)`, `Bash(pip ...)` → `## Python toolchain`
   - `Bash(brew ...)`, `Bash(codex:*)` → `## macOS / ecosystem`
   - All other `Bash(...)` → `## Shell utilities`

2. Update `settings.json` — parse, append, write back:

<!-- Note: python3 is excluded from auto-allow list by design — user will see an approval prompt for this command. -->

```bash
python3 -c "  # timeout: 15000
import json
with open('.claude/settings.json') as f:
    d = json.load(f)
d['permissions']['allow'].append('<rule>')
with open('.claude/settings.json', 'w') as f:
    json.dump(d, f, indent=2)
    f.write('\n')
"
```

3. Update `permissions-guide.md` — append new row to end of correct section (before its trailing `---` separator). New row format:

```markdown
| `<rule>` | <description> | <use case> |
```

Use Edit tool to insert row: find last table row in target section and insert after it.

4. Verify both files updated:

```bash
python3 -c "import json; d=json.load(open('.claude/settings.json')); print('OK' if '<rule>' in d['permissions']['allow'] else 'MISSING')"  # timeout: 15000
grep -F '`<rule>`' .claude/permissions-guide.md
```

### Mode: Remove Permission

Removes rule from both `settings.json` and `permissions-guide.md` atomically.

1. Update `settings.json` — parse, filter, write back:

```bash
python3 -c "  # timeout: 15000
import json
with open('.claude/settings.json') as f:
    d = json.load(f)
d['permissions']['allow'] = [p for p in d['permissions']['allow'] if p != '<rule>']
with open('.claude/settings.json', 'w') as f:
    json.dump(d, f, indent=2)
    f.write('\n')
"
```

2. Update `permissions-guide.md` — use Edit tool to remove table row containing `` `<rule>` ``.

3. Verify both files clean:

```bash
python3 -c "import json; d=json.load(open('.claude/settings.json')); print('OK' if '<rule>' not in d['permissions']['allow'] else 'STILL PRESENT')"  # timeout: 15000
grep -cF '`<rule>`' .claude/permissions-guide.md && echo "STILL IN GUIDE" || echo "OK"
```

## Step 5: Propagate cross-references

Search all `.claude/` markdown files for changed name and update references:

Use Grep to find all references:

- Pattern `<name>`, glob `agents/*.md`, path `.claude/`, output mode `content`
- Pattern `<name>`, glob `skills/*/SKILL.md`, path `.claude/`, output mode `content`
- Pattern `<name>`, glob `rules/*.md`, path `.claude/`, output mode `content`
- Pattern `<name>`, file `.claude/CLAUDE.md`, output mode `content`
- Pattern `<name>`, file `README.md`, output mode `content`

**For update (rename):** Count files grep returns. **≤ 3 files**: apply inline with Edit tool. **> 3 files**: spawn **foundry:curator** subagent:

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

## Step 6: Update MEMORY.md roster (auto-memory)

MEMORY.md is Claude Code's auto-memory file — **not** stored under `.claude/`. Injected into conversation context at session start. Absolute path appears near top of system prompt (e.g. `~/.claude/projects/.../memory/MEMORY.md`). Use that absolute path with Edit tool.

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
- **create skill**: add row to `### Skills` table — columns: `| **name** | \`/name\` | Description |\`
- **update (rename)**: find and replace old name in table row
- **delete**: remove row for deleted name

**`.claude/README.md` (config README) — Rules table only:**

- **create rule**: add row to Rules reference table — columns: `| rule-file | Applies to | What it governs |`
- **update rule (rename)**: replace old name in Rules table row
- **update rule (content-edit)**: update "What it governs" column if rule's description changed
- **delete rule**: remove row for deleted rule

Keep descriptions concise (one line), consistent in tone with surrounding rows. Do not add/remove table columns.

**For content-edit (agent/skill):** Update README only if description changed.

## Step 8: Verify integrity

Confirm no broken references remain:

Use Grep (pattern `[a-z]+:[a-z]+(-[a-z]+)*` to find cross-plugin references, or `See [a-z-]+ agent` for cross-references, glob `{agents/*.md,skills/*/SKILL.md}`, path `.claude/`, output mode `content`). Avoid broad kebab-case patterns — they match code examples and produce false positives.

Use Glob (`agents/*.md`, path `.claude/`) and Glob (`skills/*/`, path `.claude/`) for on-disk inventory; extract names inline. Use Grep to search for changed name and confirm:

- **Update (rename)**: zero hits for old name, appropriate hits for new name
- **Delete**: zero hits for deleted name (or flagged references noted)
- **Create**: new file exists with valid structure
- **Content-edit**: target file has valid structure (XML tag balance for agents/skills; YAML frontmatter for rules)

Add rules to on-disk inventory check: Glob (`rules/*.md`, path `.claude/`), extract names inline.

For **create** and **update (rename)**: verify tool efficiency — cross-check agent/skill's declared tools (`tools:` or `allowed-tools:`) against tool names in workflow body. Declared tool not referenced anywhere → flag as cleanup candidate in Step 10 report (report only — do not block operation).

## Step 9: Audit and calibrate

Run `/audit` to validate created/modified files. **Skip if invoked with `--skip-audit` or if current `manage` operation runs inside an `audit fix` loop** — outer audit covers it.

```text
/audit
```

For targeted check of only affected file, spawn **foundry:curator** directly:

- For `create`: audit new file for structural completeness, cross-ref validity, content quality
- For `update`: audit renamed file, verify no stale references remain
- For `delete`: audit remaining files for broken references to deleted name

Include audit findings in final report. Do not proceed to sync if any `critical` findings remain.

**Calibration** — for agent/skill create or non-trivial content-edit, run `/calibrate <name>` after audit passes — mandatory, not optional:

```text
/calibrate <name>
```

Then run `/calibrate routing fast` to confirm overall routing accuracy unaffected:

```text
/calibrate routing fast
```

Skip calibration for: trivial edits, renames, deletes, rule operations, perm operations.

## Step 10: Summary report

- **Operation**: what was done (create/update/delete + type + name, or add/remove perm + rule)
- **Files Changed**: table of file paths and actions (created/renamed/deleted/cross-ref updated/appended/removed)
- **Cross-References**: count of files updated, broken refs cleaned (n/a for perm operations)
- **Current Roster**: agents (N) and skills (N) with comma-separated names (n/a for perm operations)
- **Audit Result**: audit findings (pass / issues found) (n/a for perm operations)
- **Calibration Result**: recall score and routing accuracy from Step 9 (n/a for trivial edits, renames, deletes, perms)
- **Follow-up**: perm ops → confirm both `settings.json` and `permissions-guide.md` updated; run `/foundry:init` to sync `~/.claude/`

End response with `## Confidence` block per CLAUDE.md output standards.

</workflow>

<notes>

- **Atomic updates**: write-before-delete prevents data loss on interruption; perm ops must update both `settings.json` and `permissions-guide.md`
- **settings.json format**: json.load/json.dump with indent=2 — avoids fragile sed/awk on JSON
- **Write delegation by file type**: `.md` (agents, skills) → `foundry:curator`; code (`.js`, `.py`, `.ts`, `.sh`) → `foundry:sw-engineer`; rule edits and trivial edits (≤10-word simple-change directive) → inline Edit, no agent; cross-ref fan-out >3 files → `foundry:curator`
- **README.md tables**: agent/skill tables in project `README.md`; rules table in `.claude/README.md` — keep row format consistent with existing rows
- **No auto-edit for agent/skill/rule operations**: this skill does not mutate settings.json for non-perm operations
- **Rule files have no `name:` frontmatter** — filename is identifier; renames update file + cross-refs only
- **Color pool**: AVAILABLE_COLORS lists unused colors; if exhausted, reuse with note
- **MEMORY.md inventory**: always regenerated from disk — prevents drift
- Follow-up chains:
  - create or non-trivial update of agent/skill → `/audit` → `/calibrate <name>` (mandatory) → `/calibrate routing fast`
  - trivial update or rename or delete → `/audit` → `/calibrate routing fast` (if description changed)
  - add/remove perm → confirm both files updated; run `/foundry:init`

</notes>
