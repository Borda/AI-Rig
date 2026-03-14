Write for the reader, not the commit author.

| Element          | Rule                                                              |
| ---------------- | ----------------------------------------------------------------- |
| Feature heading  | Bold title, period, then plain-English description — no jargon    |
| PR numbers       | Always at line end: `(#N)` or `(#N, #M)` — never omit             |
| Code examples    | Real usage showing the new surface; not pseudocode                |
| Tables           | Use for option/preset comparisons; skip for single-item features  |
| Breaking changes | State exactly what breaks and the migration path                  |
| Fix items        | Say what was broken and under what condition — not just "fixed X" |
| Changed items    | Behaviour changes only — old behaviour → new behaviour            |
| Deprecated items | Name old API and its replacement; omit removal version if unknown |
| Removed items    | State deprecated-since version and migration target               |

Bad/good examples:

- Bad: `"refactor: extract UserService from monolith"` → Good: `"User management is now ~40% faster"`
- Bad: `"Fix auth bug"` → Good: `"Fixed login failure for email addresses containing special characters"`

**Contributors rules:**

- List every external contributor, even for a one-liner fix
- **NEVER guess or hallucinate a real name.** A wrong name in public release notes is a serious error. When in doubt, omit the name entirely.
- **Name lookup protocol** — run for every contributor @handle before writing their entry:
  1. `gh api /users/<handle> --jq '.name'` — if non-null and non-empty, use as the real name (high confidence)
  2. If empty: spawn `web-explorer` to search `site:linkedin.com "<handle>" developer` — use the name only if the profile clearly matches (same avatar, repos, or employer). Note the LinkedIn URL for inclusion.
  3. If still uncertain: use `@handle` only — no name field at all
- Format when name is confirmed: `* **Full Name** (@handle) ([LinkedIn](url)) – *noun phrase*`
- Format when name is not confirmed: `* @handle – *noun phrase*`
- LinkedIn is optional — include only if found via lookup; never construct a URL by guessing
- New contributors get a welcome sentence above the list
- Maintainer always listed last with infra / CI / docs scope
