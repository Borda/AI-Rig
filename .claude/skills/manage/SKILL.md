---
name: manage
description: Create, update, or delete agents and skills with full cross-reference propagation. Also manages settings.json permissions atomically with permissions-guide.md via add/remove perm operations.
argument-hint: <create|update|delete> <agent|skill> <name> | add perm <rule> "desc" "use-case" | remove perm <rule>
disable-model-invocation: true
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, Task
---

<objective>

Manage the lifecycle of agents and skills in the `.claude/` directory. Handles creation with rich domain content, atomic updates (renames) with cross-reference propagation, and clean deletion with broken-reference cleanup. Keeps the MEMORY.md inventory in sync with what actually exists on disk.

</objective>

<inputs>

- **$ARGUMENTS**: required, one of:
  - `create agent <name> "description"` — create a new agent with generated domain content
  - `create skill <name> "description"` — create a new skill with workflow scaffold
  - `update agent <old-name> <new-name>` — rename agent file + update all cross-refs
  - `update skill <old-name> <new-name>` — rename skill directory + update all cross-refs
  - `delete agent <name>` — delete agent file + clean broken refs
  - `delete skill <name>` — delete skill directory + clean broken refs
  - `add perm <rule> "description" "use case"` — add a permission to settings.json allow list and permissions-guide.md
  - `remove perm <rule>` — remove a permission from settings.json allow list and permissions-guide.md
- Names must be **kebab-case** (lowercase, hyphens only)
- Descriptions must be quoted when they contain spaces
- Permission rules use the Claude Code format: `WebSearch`, `Bash(cmd:*)`, `WebFetch(domain:example.com)`

**Agent examples:**

- `/manage create agent security-auditor "Security specialist for vulnerability scanning and OWASP compliance"`
- `/manage update agent example-agent example-agent-v2`
- `/manage delete agent old-agent-name`

**Skill examples:**

- `/manage create skill benchmark "Benchmark orchestrator for measuring and comparing performance across commits"`
- `/manage update skill debug trace-logger`
- `/manage delete skill old-skill`

**Permission examples:**

- `/manage add perm "Bash(jq:*)" "Parse and filter JSON" "Extract fields from REST API responses"`
- `/manage remove perm "Bash(jq:*)"`

</inputs>

<constants>

- AGENTS_DIR: `.claude/agents`
- SKILLS_DIR: `.claude/skills`
- USED_COLORS: blue, green, purple, lime, orange, yellow, cyan, red, teal, indigo, magenta, pink
- AVAILABLE_COLORS: coral, gold, olive, navy, salmon, violet, maroon, aqua, brown

</constants>

<workflow>

## Step 1: Parse and validate

Extract operation, type, name, and optional arguments from `$ARGUMENTS`.

**Validation rules:**

- Name must match `^[a-z][a-z0-9-]*$` (kebab-case)
- For `create`: name must NOT already exist on disk
- For `update`/`delete`: name MUST already exist on disk
- For `update`: new-name must NOT already exist on disk
- For `create`: description is required
- For `add perm`: rule must NOT already exist in settings.json allow list; description and use case are required
- For `remove perm`: rule MUST already exist in settings.json allow list

```bash
# Check agent/skill existence
ls .claude/agents/<name>.md 2>/dev/null
ls .claude/skills/<name>/SKILL.md 2>/dev/null

# Check permission existence (for add perm / remove perm)
python3 -c "import json,sys; d=json.load(open('.claude/settings.json')); sys.exit(0 if '<rule>' in d['permissions']['allow'] else 1)" 2>/dev/null
```

If validation fails, report the error and stop.

**For perm operations**: skip Steps 2, 3, 5, 6, 7, 8 — go directly from Step 1 → Step 4 → Step 9.

## Step 2: Overlap review (create only)

Before creating anything, check if existing agents/skills already cover the requested functionality:

1. Read descriptions of all existing agents (`head -3` of each `.md` in agents/) and skills (`head -3` of each `SKILL.md`)
2. Compare the new description against each existing one — look for domain overlap, similar workflows, or redundant scope
3. Present findings to the user:
   - **No overlap**: proceed to Step 3
   - **Partial overlap**: name the overlapping agent/skill, explain what it covers vs what the new one would add, and ask the user whether to proceed, extend the existing one instead, or abort
   - **Strong overlap**: recommend against creation — suggest using or extending the existing agent/skill instead

Skip this step for `update` and `delete` operations.

## Step 3: Inventory current state

Snapshot the current roster for later comparison:

```bash
# Current agents
ls .claude/agents/*.md | xargs -n1 basename | sed 's/\.md$//' | sort

# Current skills
ls -d .claude/skills/*/ | xargs -n1 basename | sort

# Colors in use
grep '^color:' .claude/agents/*.md
```

## Step 4: Execute operation

Branch into one of six modes:

### Mode: Create Agent

1. Fetch the latest Claude Code agent frontmatter schema to ensure the template is current:

   - Spawn **web-explorer** to fetch `code.claude.com/docs/en/sub-agents` <!-- verified 2026-03-02 -->
   - Confirm valid frontmatter fields: `name`, `description`, `tools`, `disallowedTools`,
     `model`, `permissionMode`, `maxTurns`, `skills`, `mcpServers`, `hooks`, `memory`,
     `background`, `isolation`
   - Verify model shorthand values are still current (`sonnet`, `opus`, `haiku`, `inherit`)
   - Note any new fields worth including in the generated template
     Adjust the template generated in steps 2–4 to reflect the current schema. If a new field is
     broadly useful for the agent's role (e.g. `maxTurns` for long-running agents), include it
     with a sensible default and inline comment.

2. Pick the first unused color from the AVAILABLE_COLORS pool (compare against colors found in Step 3)

3. Choose model based on role complexity:

   - `opusplan` — plan-gated roles (solution-architect, oss-maintainer, self-mentor): long-horizon reasoning + plan mode
   - `opus` — complex implementation roles (sw-engineer, qa-specialist, ai-researcher, perf-optimizer): deep reasoning without plan mode
   - `sonnet` — focused execution roles (linting-expert, data-steward, ci-guardian, web-explorer, doc-scribe): pattern-matching, structured output

4. Write the agent file with real domain content derived from the description:

**Agent template** — write to `AGENTS_DIR/<name>.md`:

```
---
name / description / tools / model / color (frontmatter)
---
<role> — 2-3 sentences establishing expertise from description
\<core_knowledge> — 2 subsections, 3-5 bullets each (domain-specific, not generic)

\</core_knowledge>

<workflow> — 5 numbered steps appropriate to the domain

</workflow>

\<notes> — 1-2 operational notes + cross-refs to related agents

\</notes>

```

**Content rules:** `<role>` and `<workflow>` use normal tags; all other sections use `\<escaped>` tags. Generate real domain content (80-120 lines total).

### Mode: Create Skill

1. Fetch the latest Claude Code skill frontmatter schema to ensure the template is current:

   - Spawn **web-explorer** to fetch `code.claude.com/docs/en/skills` <!-- verified 2026-03-02 -->
   - Confirm valid frontmatter fields: `name`, `description`, `argument-hint`,
     `disable-model-invocation`, `user-invocable`, `allowed-tools`, `model`,
     `context`, `agent`, `hooks`
   - Note any new fields worth including in the generated template
     Adjust the template generated in step 3 to reflect the current schema. Include `model`
     or `context: fork` only when the skill's described purpose clearly benefits from them.

2. Create the skill directory

3. Write the skill file with workflow scaffold:

```bash
mkdir -p .claude/skills/<name>
```

**Skill template** — write to `SKILLS_DIR/<name>/SKILL.md`:

```
---
name / description / argument-hint / disable-model-invocation: true / allowed-tools (frontmatter)
---
<objective> — 2-3 sentences from description
<inputs> — $ARGUMENTS documentation
<workflow> — 3+ numbered steps with bash examples
<notes> — operational caveats
```

**Content rules:** No backslash escaping in skills (all normal XML tags). Generate real steps (40-60 lines total). Default `allowed-tools` to `Read, Bash, Grep, Glob, Task` unless writing files is needed.

### Mode: Update Agent

Atomic update — write new file before deleting old:

```bash
# 1. Read the old file
cat .claude/agents/<old-name>.md

# 2. Write new file with updated name in frontmatter
# (Edit the `name:` line in frontmatter to use new-name)

# 3. Verify new file exists and is valid
head -5 .claude/agents/<new-name>.md

# 4. Delete old file only after new file is confirmed
rm .claude/agents/<old-name>.md
```

### Mode: Update Skill

Atomic update — create new directory before removing old:

```bash
# 1. Create new directory
mkdir -p .claude/skills/<new-name>

# 2. Copy SKILL.md with updated name in frontmatter
# (Read old, edit name: line, write to new location)

# 3. Verify new file exists
head -5 .claude/skills/<new-name>/SKILL.md

# 4. Remove old directory only after new is confirmed
rm -r .claude/skills/<old-name>
```

### Mode: Delete Agent

```bash
# Confirm existence before deleting
ls .claude/agents/<name>.md
rm .claude/agents/<name>.md
```

### Mode: Delete Skill

```bash
# Confirm existence before deleting
ls .claude/skills/<name>/SKILL.md
rm -r .claude/skills/<name>
```

### Mode: Add Permission

Adds a rule to both `settings.json` and `permissions-guide.md` atomically.

1. Determine the guide category from the rule prefix:

   - `WebSearch` → `## Web`
   - `WebFetch(domain:...)` → `## WebFetch — allowed domains`
   - `Bash(gh ...)` → `## GitHub CLI — read-only`
   - `Bash(git log:*)`, `Bash(git show:*)`, `Bash(git diff:*)`, `Bash(git rev-*:*)`, `Bash(git ls-*:*)`, `Bash(git -C:*)`, `Bash(git branch:*)`, `Bash(git tag:*)`, `Bash(git status:*)`, `Bash(git describe:*)`, `Bash(git shortlog:*)` → `## Git — read-only`
   - `Bash(git add:*)`, `Bash(git checkout:*)`, `Bash(git stash:*)`, `Bash(git restore:*)`, `Bash(git clean:*)`, `Bash(git apply:*)` → `## Git — local write`
   - `Bash(pytest:*)`, `Bash(python ...)`, `Bash(ruff:*)`, `Bash(mypy:*)`, `Bash(pip ...)` → `## Python toolchain`
   - `Bash(brew ...)`, `Bash(codex:*)` → `## macOS / ecosystem`
   - All other `Bash(...)` → `## Shell utilities`

2. Update `settings.json` — parse, append, write back:

```bash
python3 -c "
import json
with open('.claude/settings.json') as f:
    d = json.load(f)
d['permissions']['allow'].append('<rule>')
with open('.claude/settings.json', 'w') as f:
    json.dump(d, f, indent=2)
    f.write('\n')
"
```

3. Update `permissions-guide.md` — append a new row to the end of the correct section (before its trailing `---` separator). New row format:

```
| `<rule>` | <description> | <use case> |
```

Use the Edit tool to insert the row: find the last table row in the target section and insert after it.

4. Verify both files were updated:

```bash
python3 -c "import json; d=json.load(open('.claude/settings.json')); print('OK' if '<rule>' in d['permissions']['allow'] else 'MISSING')"
grep -F '`<rule>`' .claude/permissions-guide.md
```

### Mode: Remove Permission

Removes a rule from both `settings.json` and `permissions-guide.md` atomically.

1. Update `settings.json` — parse, filter, write back:

```bash
python3 -c "
import json
with open('.claude/settings.json') as f:
    d = json.load(f)
d['permissions']['allow'] = [p for p in d['permissions']['allow'] if p != '<rule>']
with open('.claude/settings.json', 'w') as f:
    json.dump(d, f, indent=2)
    f.write('\n')
"
```

2. Update `permissions-guide.md` — use the Edit tool to remove the table row containing `` `<rule>` ``.

3. Verify both files are clean:

```bash
python3 -c "import json; d=json.load(open('.claude/settings.json')); print('OK' if '<rule>' not in d['permissions']['allow'] else 'STILL PRESENT')"
grep -cF '`<rule>`' .claude/permissions-guide.md && echo "STILL IN GUIDE" || echo "OK"
```

## Step 5: Propagate cross-references

Search all `.claude/` markdown files for the changed name and update references:

```bash
# Find all references to the name across agents, skills, and top-level config
grep -rn "<name>" .claude/agents/*.md
grep -rn "<name>" .claude/skills/*/SKILL.md
grep -n "<name>" .claude/CLAUDE.md
grep -n "<name>" README.md
```

**For update:** Use the Edit tool to replace every occurrence of `<old-name>` with `<new-name>` in each file that references it.

**For delete:** Review each reference. If the deleted name appears in:

- A cross-reference suggestion (e.g., "use X agent") — remove or replace with the closest alternative
- An inventory list — remove the entry
- A workflow spawn directive — flag for manual review

**For create:** No cross-ref propagation needed (new names have no existing references).

## Step 6: Update memory/MEMORY.md

Regenerate the inventory lines from what actually exists on disk:

```bash
# Get current agent list
ls .claude/agents/*.md | xargs -n1 basename | sed 's/\.md$//' | paste -sd', ' -

# Get current skill list
ls -d .claude/skills/*/ | xargs -n1 basename | paste -sd', ' -
```

Use the Edit tool to update these two lines in MEMORY.md:

- `- Agents: oss-maintainer, sw-engineer, ...` (the roster line, not the path line)
- `- Skills: review, security, ...`

## Step 7: Update README.md

Update the agent or skill table in `README.md`:

- **create agent**: add a new row to the `### Agents` table — columns: `| **name** | Short tagline | Key capabilities |`
- **create skill**: add a new row to the `### Skills` table — columns: `| **name** | \`/name\` | Description |\`
- **update (rename)**: find and replace the old name in the table row with the new name
- **delete**: remove the row for the deleted name

The README tables are self-documenting — keep descriptions concise (one line) and consistent in tone with the surrounding rows. Do not add/remove table columns.

## Step 8: Verify integrity

Confirm no broken references remain:

```bash
# Extract all agent/skill names referenced in .claude/ files
grep -rohE '[a-z]+-[a-z]+(-[a-z]+)*' .claude/agents/*.md \
  .claude/skills/*/SKILL.md | sort -u

# Compare against actual files on disk
ls .claude/agents/*.md | xargs -n1 basename | sed 's/\.md$//'
ls -d .claude/skills/*/ | xargs -n1 basename
```

Use Grep to search for the specific changed name and confirm:

- **Update**: zero hits for old name, appropriate hits for new name
- **Delete**: zero hits for deleted name (or flagged references noted)
- **Create**: new file exists with valid structure

## Step 9: Audit

Run `/audit` to validate the created/modified file(s) and catch any issues introduced by this operation. **Skip this step if the current `manage` operation is itself being executed as part of an `audit fix` run** — the outer audit will cover it.

```
/audit
```

For a targeted check of only the affected file, spawn **self-mentor** directly:

- For `create`: audit the new file for structural completeness, cross-ref validity, and content quality
- For `update`: audit the renamed file and verify no stale references remain
- For `delete`: audit remaining files for broken references to the deleted name

Include the audit findings in the final report. Do not proceed to sync if any `critical` findings remain.

## Step 10: Summary report

Output a structured report containing:

- **Operation**: what was done (create/update/delete + type + name, or add/remove perm + rule)
- **Files Changed**: table of file paths and actions (created/renamed/deleted/cross-ref updated/appended/removed)
- **Cross-References**: count of files updated, broken refs cleaned (n/a for perm operations)
- **Current Roster**: agents (N) and skills (N) with comma-separated names (n/a for perm operations)
- **Audit Result**: audit findings (pass / issues found) (n/a for perm operations)
- **Follow-up**: run `/sync apply` to propagate to `~/.claude/`; for `create` review generated content; for perm operations confirm both `settings.json` and `permissions-guide.md` are updated

</workflow>

<notes>

- **Atomic updates**: always write-before-delete to prevent data loss on interruption
- **Perm operations are dual-file**: `add perm` and `remove perm` MUST update both `settings.json` and `permissions-guide.md` — they must stay in sync. Never update one without the other.
- **settings.json format**: Python json.load/json.dump with indent=2 is the safe editing path — avoids fragile sed/awk surgery on JSON. The output format (2-space indent, no trailing commas) matches the existing file style.
- **No auto-edit for agent/skill operations**: this skill only mentions when new permissions might be needed for create/update/delete — it does not mutate settings.json for those operations
- **README.md tables**: Step 7 updates the agent/skill tables in the project README.md — keep the row format consistent with existing rows
- **Color pool**: the AVAILABLE_COLORS list provides unused colors for new agents; if exhausted, reuse colors with a note
- **Cross-ref grep is broad**: searches bare kebab-case names across all markdown files — catches backtick references, prose mentions, spawn directives, and inventory lists
- **MEMORY.md inventory**: always regenerated from disk (`ls`), never manually calculated — this prevents drift
- Follow-up chains:
  - After any create/update/delete → `/audit` to verify config integrity, then `/sync apply` to propagate
  - After creating a new agent/skill → `/review` to validate generated content quality
  - After updating agent instructions (especially `\<antipatterns_to_flag>` or `\<evaluation_criteria>`) → `/calibrate <agent>` to measure whether recall and confidence calibration improved
  - After `add perm`/`remove perm` → `/sync apply` to propagate updated settings.json and permissions-guide.md to `~/.claude/`
  - Recommended sequence: `/manage <op>` → `/audit` → `/sync apply`

</notes>
