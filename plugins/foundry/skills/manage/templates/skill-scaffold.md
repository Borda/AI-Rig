**Re: Compress skill template instructions into caveman format**

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

**Content rules:** No backslash escaping in skills (all normal XML tags). Start `<workflow>` body with `**Task hygiene**` preamble (call `TaskList`, triage found tasks by status) then `**Task tracking**:` for how `TaskCreate` used. Generate real steps (40-60 lines total). Default `allowed-tools` to `Read, Bash, Grep, Glob, TaskCreate, TaskUpdate` unless writing files needed; add `Agent` only if skill spawns subagents. Add `Write`/`Edit` only if skill creates/modifies files; add `WebFetch`/`WebSearch` only if skill fetches external docs. Don't list unused tools — inflates permission surface.
