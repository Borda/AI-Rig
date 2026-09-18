**Skill template** — write to `SKILLS_DIR/<name>/SKILL.md`:

```markdown
---
name / description / argument-hint / disable-model-invocation (conditional — see Content rules) / allowed-tools (frontmatter)
# TRIGGER/SKIP guidance belongs in description: field; do NOT add when_to_use: (deprecated field)
---
<objective> — 2-3 sentences from description
<inputs> — $ARGUMENTS documentation
`<workflow>` — 3+ numbered steps with bash examples
<notes> — operational caveats
```

**Content rules:** no backslash escaping in skills (all normal XML tags). Start `<workflow>` body with `**Task hygiene**` preamble (call `TaskList`, triage found tasks by status) then `**Task tracking**:` for how `TaskCreate` used. Real steps, 40-60 lines total. Default `allowed-tools` to `Read, Bash, Grep, Glob, TaskCreate, TaskUpdate` unless writing files needed; add `Agent` only if skill spawns subagents. Add `Write`/`Edit` only if skill creates/modifies files; add `WebFetch`/`WebSearch` only if skill fetches external docs. Don't list unused tools — inflates permission surface. Set `disable-model-invocation: true` for any skill with side effects (writes, deletes, external calls, destructive/irreversible operations); omit only for pure read/draft/conversational skills — field blocks model from spontaneously invoking skill on inferred intent. Does **not** block an explicit `Skill()` call an orchestrator makes after an `AskUserQuestion`-confirmed follow-up gate — that's user-confirmed chaining, not auto-chain, stays unaffected regardless of flag. **LLM-first formatting**: skills read primarily by LLM at inference time. One canonical form per pattern type:

- Unordered lists: `-` only (never `*` or `+`)
- Sequential workflow steps: `1.` `2.` `3.`
- Option/choice lists (AskUserQuestion options, mode names): `(a)` `(b)` `(c)` — never `1.` `2.` for choices
- 3+ items × 2+ fixed attributes → table; nested prose only when schema varies per item

**TRIGGER/SKIP required**: every skill body must include:

- TRIGGER section: 2-4 conditions activating this skill; phrase as "TRIGGER when: ..."
- SKIP section: 2-3 conditions skill should NOT be used; phrase as "SKIP when: ..."
