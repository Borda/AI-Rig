**Agent template** — write to `AGENTS_DIR/<name>.md`:

```markdown
---
name / description / tools / model / color (frontmatter)
---
<role> — 2-3 sentences establishing expertise from description
\<core_knowledge> — 2 subsections, 3-5 bullets each (domain-specific, not generic)

\</core_knowledge>

`<workflow>` — 5 numbered steps appropriate to the domain

</workflow>

\<notes> — 1-2 operational notes + cross-refs to related agents

\</notes>

```

**Content rules:** `<role>` and `<workflow>` use normal tags; all other sections use `\<escaped>` tags. Generate real domain content (80-120 lines total).

**Tool selection**: match tools precisely to domain — no padding. Guidelines by role:

- Analysis/read-only agents (e.g., `foundry:solution-architect`, `foundry:doc-scribe`): start `Read, Grep, Glob`; add `WebFetch`/`WebSearch` only if domain fetches external docs/URLs; add `Write` only if agent creates output files
- Code execution agents (e.g., `foundry:linting-expert`, `foundry:perf-optimizer`, `oss:ci-guardian`): include `Bash`; add `Write`/`Edit` only if agent modifies code
- Skills orchestrating subagents (e.g., `review`, `feature`, `audit`): include `Agent` in `allowed-tools`
- Web-research agents (e.g., `foundry:web-explorer`, `research:scientist`): include `WebFetch` and/or `WebSearch`

Drop any tool serving no purpose for declared domain. Minimal precise list beats maximal one.
