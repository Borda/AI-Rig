**Re: Compress markdown to caveman format**

Write for reader, not commit author.

| Element | Rule |
| --- | --- |
| Feature heading | Bold title, period, then plain-English description — no jargon |
| PR numbers (CHANGELOG) | Full Markdown link — `([#947](https://github.com/owner/repo/pull/947))` |
| PR numbers (PUBLIC-NOTES.md) | Short inline ref — `(#947)` |
| PR ref + fenced code block | Place `(#N)` at end of description text **before** the opening fence — never after the closing fence; trailing refs after fenced blocks are invisible in rendered output |
| Issue refs | Never include `closes #N` / `fixes #N` in CHANGELOG or PUBLIC-NOTES.md |
| Code examples | Real usage showing the new surface; not pseudocode |
| Tables | Use for option/preset comparisons; skip for single-item features |
| Breaking changes | Rare — use sparingly; false alarms scare users more than the change itself |
| Fix items | Say what was broken and under what condition — not just "fixed X" |
| Changed items | Behaviour changes only — old behaviour → new behaviour |
| Deprecated items | Name old API and its replacement; omit removal version if unknown |
| Removed items | State deprecated-since version and migration target |

> **Breaking vs Deprecated**: Normal flow is deprecate → announce removal version → Removed. Breaking Changes = rare case where **public API or user-facing behaviour** breaks **immediately** on upgrade, no prior warning, no fallback — including dependency version incompatibilities affecting users directly. Private API and test changes never Breaking Changes. Old behaviour still works (even with deprecation warning) → belongs in Deprecated, not here. When in doubt, not Breaking Changes.

Bad/good examples:

- Bad: `"refactor: extract UserService from monolith"` → Good: `"User management is now ~40% faster"`
- Bad: `"Fix auth bug"` → Good: `"Fixed login failure for email addresses containing special characters"`

**Contributors rules:**

- List **every** PR author in range — human and bot alike; community acknowledgement essential for growth
- **Bots**: collect all bot handles (accounts ending in `[bot]` or known bots like `dependabot`, `renovate`, `github-actions`) and render as single italic line at bottom of section: `*Automated contributions: @bot1, @bot2*` — never list bots individually
- **NEVER guess or hallucinate real name.** Wrong name in public release notes = serious error. When in doubt, omit name entirely.
- **Name lookup protocol** — run for every human contributor @handle before writing entry:
  1. `gh api /users/<handle> --jq '.name'` — if non-null and non-empty, use as real name (high confidence)
  2. If empty: spawn `foundry:web-explorer` to search `site:linkedin.com "<handle>" developer` — use name only if profile clearly matches (same avatar, repos, or employer). Note LinkedIn URL for inclusion.
  3. If still uncertain: use `@handle` only — no name field
- Format when name confirmed: `* **Full Name** (@handle) ([LinkedIn](url)) – *noun phrase*`
- Format when name not confirmed: `* @handle – *noun phrase*`
- LinkedIn optional — include only if found via lookup; never construct URL by guessing
- New contributors get welcome sentence above list
- Maintainer always listed last with infra / CI / docs scope
- Precede Contributors section with `---` separator

**Last line (required):**

Every release entry must end with:

`**Full changelog**: https://github.com/[org]/[repo]/compare/vPREV...vNEXT`

- Must be final line of entry, after Contributors section — no notes, annotations, separators, or prose may follow
- Never omit, even for patch releases
