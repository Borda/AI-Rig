**Agent template** — write to `AGENTS_DIR/<name>.md`:

```
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

**Tool selection**: match tools precisely to the domain — do not pad the list. Guidelines by role type:

- Analysis / read-only agents (e.g., `solution-architect`, `doc-scribe`): start with `Read, Grep, Glob`; add `WebFetch`/`WebSearch` only if the domain involves fetching external docs or URLs; add `Write` only if the agent creates output files
- Code execution agents (e.g., `linting-expert`, `perf-optimizer`, `ci-guardian`): include `Bash`; add `Write`/`Edit` only if the agent modifies code
- Skills that orchestrate agent subagents (e.g., `review`, `feature`, `audit`): include `Agent` in `allowed-tools`
- Web-research agents (e.g., `web-explorer`, `ai-researcher`): include `WebFetch` and/or `WebSearch`

Remove any tool that serves no purpose for the declared domain. A minimal, precise list is safer and clearer than a maximal one.
