**Agent template** — write to `AGENTS_DIR/<name>.md`:

```markdown
---
name / description / tools / model / color (frontmatter)
---
<role> — 2-3 sentences establishing expertise from description
<core-knowledge> — 2 subsections, 3-5 bullets each (domain-specific, not generic)

</core-knowledge>

`<workflow>` — 5 numbered steps appropriate to the domain

</workflow>

\<notes> — 1-2 operational notes + cross-refs to related agents

\</notes>

```

**Content rules:** `<role>`, `<workflow>`: normal tags. Others: `\<escaped>` tags. Real domain content, 80-120 lines total.

**Tool selection**: match tools to domain, no padding. By role:

- Analysis/read-only agents (e.g., `foundry:solution-architect`, `foundry:doc-scribe`): start `Read, Grep, Glob`; add `WebFetch`/`WebSearch` only if domain fetches external docs/URLs; add `Write` only if creates output files
- Code execution agents (e.g., `foundry:linting-expert`, `foundry:perf-optimizer`, `oss:cicd-steward`): include `Bash`; add `Write`/`Edit` only if modifies code
- Skills orchestrating subagents (e.g., `review`, `feature`, `audit`): include `Agent` in `allowed-tools`
- Web-research agents (e.g., `foundry:web-explorer`, `research:scientist`): include `WebFetch` and/or `WebSearch`

Drop tools with no purpose for declared domain. Minimal precise list beats maximal.

**LLM-first formatting**: agents read by LLM at inference time. One canonical form per pattern type:

- Unordered lists: `-` only (never `*` or `+`)
- Sequential workflow steps: `1.` `2.` `3.`
- Option/choice lists (AskUserQuestion, mode names, examples): `(a)` `(b)` `(c)` — never `1.` `2.` for choices
- 3+ items × 2+ fixed attributes → table; nested prose only when schema varies per item

**NOT-for clause requirement**: every agent must include a NOT-for clause specifying:

1. Use cases this agent does NOT handle
2. Specific alternative agent handling each excluded case
   - Example: "NOT for fixing vulnerabilities — use develop:fix"
   - Example: "NOT for generating reports — use foundry:doc-scribe"
   - Never: "NOT for X" without naming an alternative
