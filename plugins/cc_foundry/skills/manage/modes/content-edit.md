<!-- file: content-edit.md — consumers: SKILL.md (Step 4 dispatch) -->

### Mode: Update Agent/Skill (content-edit)

Before executing type-specific content-edit mode, determine approach:

**File-type → agent routing:**

| File extension | Agent |
| -- | -- |
| `.md` (agents, skills, SKILL.md) | `foundry:curator` |
| `.py`; hook `.js` under `hooks/` | `foundry:sw-engineer` |
| `.ts`, non-hook `.js`, `.sh` | no owning agent (sw-engineer NOT-for) — inline Edit, flagged as unowned in Step 10 |
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
- Wrap all logic in try/catch; the catch destination depends on hook class (per `references/sw-engineer/hook-authoring.md` §Implementation pattern): PreToolUse gatekeeper that can emit `deny` → `process.exit(2)` (an erroring gate must block — exiting 0 is a security bypass); allow-only PreToolUse hook, or a logging hook (PostToolUse/SubagentStop) → `process.exit(0)`. Classify from the source, not from intent: any code path that can emit `deny` makes it a gatekeeper.
- After editing: verify exit codes match documented cases, no shell injection surface added
Write all changes using the Edit tool.
Return ONLY: {"status":"done","file":".claude/hooks/<name>.js","edits":N,"confidence":0.N}
```
