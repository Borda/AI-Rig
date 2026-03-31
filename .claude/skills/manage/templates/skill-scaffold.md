**Skill template** — write to `SKILLS_DIR/<name>/SKILL.md`:

```
---
name / description / argument-hint / disable-model-invocation: true / allowed-tools (frontmatter)
---
<objective> — 2-3 sentences from description
<inputs> — $ARGUMENTS documentation
`<workflow>` — 3+ numbered steps with bash examples
<notes> — operational caveats
```

**Content rules:** No backslash escaping in skills (all normal XML tags). Start the `<workflow>` body with a `**Task hygiene**` preamble (call `TaskList`, triage found tasks by status) followed by `**Task tracking**:` for how TaskCreate is used in this skill. Generate real steps (40-60 lines total). Default `allowed-tools` to `Read, Bash, Grep, Glob, TaskCreate, TaskUpdate` unless writing files is needed; add `Agent` only if the skill spawns subagents. Only add `Write`/`Edit` if the skill creates or modifies files; only add `WebFetch`/`WebSearch` if the skill fetches external docs. Do not list tools the workflow never uses — unused declared tools inflate the permission surface needlessly.
