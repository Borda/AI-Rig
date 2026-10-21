<!-- file: create.md — consumers: SKILL.md (Step 4 dispatch) -->

### Mode: Create Agent

1. Fetch latest Claude Code agent frontmatter schema:

   - Resolve schema cache path (24h TTL — schema changes rarely; saves one web-explorer spawn per create):
     ```bash
     mkdir -p .cache/manage  # timeout: 3000
     MANAGE_SCHEMA_FILE=".cache/manage/agent-schema.md"
     if [ -n "$(find "$MANAGE_SCHEMA_FILE" -mmin -1440 2>/dev/null)" ]; then MANAGE_SCHEMA_CACHED=true; else MANAGE_SCHEMA_CACHED=false; fi
     echo "Schema file: $MANAGE_SCHEMA_FILE (cached: $MANAGE_SCHEMA_CACHED)"  # timeout: 3000
     ```
   - **`MANAGE_SCHEMA_CACHED=true`**: skip spawn and deadline below; Read `$MANAGE_SCHEMA_FILE` (limit=60) for field list, continue at extraction bullet.
   - Spawn **foundry:web-explorer** to fetch `https://code.claude.com/docs/en/sub-agents` with instruction: "Write your full findings (schema fields, new fields, deprecated fields) to `<MANAGE_SCHEMA_FILE>` (substitute resolved path from bash block above) using the Write tool. Return ONLY a compact JSON envelope on your final line — nothing else after it: `{\"status\":\"done\",\"file\":\"<MANAGE_SCHEMA_FILE>\",\"fields\":N,\"new\":N,\"deprecated\":N,\"confidence\":0.N,\"summary\":\"N fields, N new, N deprecated\"}`"

   Deadline (§Deadlines, constants): in this spawn response Write `.temp/manage/agent-watch-manage.tsv` = `web-explorer\t<MANAGE_SCHEMA_FILE>\t300`.

   - Read returned summary; extract: valid frontmatter fields (`name`, `description`, `tools`, `disallowedTools`, `model`, `permissionMode`, `maxTurns`, `effort`, `initialPrompt`, `skills`, `mcpServers`, `hooks`, `memory`, `background`, `isolation`, `color`, `autoCompactWindow`, `omitClaudeMd`, `experimental`), current model shorthands, new fields
   - Note new fields worth including. Adjust template to reflect current schema. New field broadly useful for agent's role (e.g. `maxTurns` for long-running agents): include with sensible default and inline comment.

2. Pick first unused color from AVAILABLE_COLORS pool (compare against Step 3 colors); pool exhausted → reuse the least-used color, note it in Step 10

3. Choose model based on role complexity:

   - `opus` — high-stakes design/config and complex implementation roles (foundry:solution-architect, foundry:curator, foundry:sw-engineer, research:scientist, foundry:perf-optimizer)
   - `sonnet` — focused execution roles (research:data-steward (requires `research` plugin), foundry:web-explorer, foundry:doc-scribe, foundry:creator, foundry:qa-specialist, oss:cicd-steward, oss:shepherd)
   - `haiku` — mechanical high-frequency work ONLY, with every judgement-free step scripted (e.g. oss:gh-scraper and oss:repo-warden, whose fetch, assembly and metric extraction run as `bin/` scripts); NOT for analysis/auditing roles that require substantive reasoning
   - Aliases only, never a pinned model ID. `opusplan` as subagent frontmatter runs `sonnet` (subagents never enter plan mode) — use `opus` or `sonnet` explicitly.

4. Resolve template path (cascade primary → project-local → cache scan; only the cache scan runs if neither cheaper path exists, since each candidate must satisfy `-d` before being assigned):

```bash
MANAGE_TPL=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/resolve_skill_subdir.py" manage templates) || { printf "! BREAKING: manage templates not found — run /foundry:setup first\n"; exit 1; }  # timeout: 5000
echo "Template dir: $MANAGE_TPL"
```

5. Spawn **foundry:sw-engineer** subagent to scaffold and write agent file. `foundry:curator` is the wrong delegate here — its NOT-for explicitly excludes creating or scaffolding agents/skills; curator only reviews and edits existing config. `foundry:sw-engineer` owns scaffolding (treat agent `.md` as a config artifact whose authoring is a software task — frontmatter schema, tool selection, structural completeness).

> Before passing schema file path to sw-engineer: verify file exists on disk using Read tool (limit=1). Schema file path from JSON envelope doesn't exist: proceed with default frontmatter fields (name, description, model, color) — note omission in Step 10 report.

```markdown
Run `cat "<MANAGE_TPL>/agent-scaffold.md"` via the Bash tool (substitute resolved path from bash block above — do not pass literal `$MANAGE_TPL` to the agent).
Also read the schema file at the path returned in the step 1 JSON to incorporate any new frontmatter fields (skip if schema file not found — use default frontmatter fields: name, description, model, color).
Scaffold `.claude/agents/<name>.md` with:
- Frontmatter: name=<name>, description=<description>, model=<model>, color=<color>; add any broadly-useful new fields from the schema
- Body: rich domain-specific content for the role described by the description, following all content rules and tool selection guidelines in the scaffold template
Write the file using the Write tool.
Return ONLY: {"status":"done","file":".claude/agents/<name>.md","lines":N,"confidence":0.N}
```

Deadline (§Deadlines, constants): in this spawn response Write `.temp/manage/agent-watch-manage.tsv` = `sw-engineer-agent\t-\t900` (envelope-only).

Spawn passes no `isolation` (`agents/sw-engineer.md` §Worktree isolation), so the scaffolded file lands in the caller's tree and later steps Glob it in place.

### Mode: Create Skill

1. Fetch latest Claude Code skill frontmatter schema:

   - Resolve skill schema cache path (24h TTL — same rationale as Create Agent step 1):
     ```bash
     mkdir -p .cache/manage  # timeout: 3000
     MANAGE_SKILL_SCHEMA_FILE=".cache/manage/skill-schema.md"
     if [ -n "$(find "$MANAGE_SKILL_SCHEMA_FILE" -mmin -1440 2>/dev/null)" ]; then MANAGE_SKILL_SCHEMA_CACHED=true; else MANAGE_SKILL_SCHEMA_CACHED=false; fi
     echo "Skill schema file: $MANAGE_SKILL_SCHEMA_FILE (cached: $MANAGE_SKILL_SCHEMA_CACHED)"  # timeout: 3000
     ```
   - **`MANAGE_SKILL_SCHEMA_CACHED=true`**: skip spawn and deadline below; Read `$MANAGE_SKILL_SCHEMA_FILE` (limit=60) for field list, continue at extraction bullet.
   - Spawn **foundry:web-explorer** to fetch `https://code.claude.com/docs/en/skills` with instruction: "Write your full findings (schema fields, new fields, deprecated fields) to `<MANAGE_SKILL_SCHEMA_FILE>` (substitute resolved path from bash block above) using the Write tool. Return ONLY a compact JSON envelope on your final line — nothing else after it: `{\"status\":\"done\",\"file\":\"<MANAGE_SKILL_SCHEMA_FILE>\",\"fields\":N,\"new\":N,\"deprecated\":N,\"confidence\":0.N,\"summary\":\"N fields, N new, N deprecated\"}`"

   Deadline (§Deadlines, constants): in this spawn response Write `.temp/manage/agent-watch-manage.tsv` = `web-explorer-skill\t<MANAGE_SKILL_SCHEMA_FILE>\t300`.

   - Read returned summary; extract: valid frontmatter fields (`name`, `description`, `when_to_use`, `argument-hint`, `arguments`, `disable-model-invocation`, `user-invocable`, `allowed-tools`, `disallowed-tools`, `model`, `effort`, `shell`, `paths`, `context`, `agent`, `background`, `hooks`, `metadata`, `license`, `compatibility`), new fields
   - Note new fields worth including. Adjust template to reflect current schema. Include `model` or `context: fork` only when skill's purpose clearly benefits.

2. **Re-resolve `MANAGE_TPL` at the start of each skill invocation**; don't assume it's set from a prior step. Most `/foundry:manage create skill ...` invocations enter Create Skill mode directly without going through Create Agent first, so the variable will be unset. Run resolution block from Create Agent step 4 above (cascade primary → project-local → cache scan with `-d` guards) before reading any template path.

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

Deadline (§Deadlines, constants): in this spawn response Write `.temp/manage/agent-watch-manage.tsv` = `sw-engineer-skill\t-\t900` (envelope-only).

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
