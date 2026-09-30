<!-- policy-sibling: plugins/CLAUDE.md §GitHub Reference Scoping — `#N` and `@name` (canonical) -->

Write for reader, not commit author.

| Element | Rule |
| -- | -- |
| Summary (DRAFT.md) | Elevator pitch in shepherd voice: hook line → 1–5 short win bullets (one per real win, never padded) → one-line upgrade call carrying any breaking/removed items (deprecations only as an optional heads-up clause). Contractions, "you", no hedging. No paragraph over 2 sentences; a long line gets split in two or distilled to its core. No PR refs, no count dumps ("12 bugs, 2 refinements") — name the wins instead. Bold at most one feature/API name per bullet |
| Feature heading | Bold title, period, then plain-English description — no jargon |
| PR numbers (CHANGELOG) | Full Markdown link — `([#947](https://github.com/owner/repo/pull/947))` |
| PR numbers (DRAFT.md) | Short inline ref — `(#947)` (every section except Summary, which carries none) — never `[#947](url)`; strip full links when sourcing from intermediate files |
| PR ref + fenced code block | Place `(#N)` at end of description text **before** opening fence — never after closing fence; trailing refs after fenced blocks invisible in rendered output |
| Issue refs | Never include `closes #N` / `fixes #N` in CHANGELOG or DRAFT.md |
| Bare `@name` scope | `@handle` only for a real GitHub user/org mention (Contributors line). Decorator or annotation name → backticks (`` `@cached` ``); role handle or other non-GitHub word → drop the `@` |
| Bare `#N` scope | `#N` only for a real PR/issue number. Ordinal or step ref → plain number or word (`1st`, `step 7`); code-shaped token (C macro, hex color) → backticks |
| Code examples | Real usage showing new surface; not pseudocode |
| Tables | Use for option/preset comparisons; skip for single-item features |
| Breaking changes | Rare — use sparingly; false alarms scare users more than change itself |
| Fix items | Say what was broken and under what condition — not just "fixed X" |
| Changed items | Behaviour changes only — old behaviour → new behaviour |
| Deprecated items | Name old API and replacement; omit removal version if unknown |
| Removed items | State deprecated-since version and migration target |

> **Breaking vs Deprecated**: Normal flow is deprecate → announce removal version → Removed. Breaking Changes = rare case where **public API or user-facing behaviour** breaks **immediately** on upgrade, no prior warning, no fallback — including dependency version incompatibilities affecting users directly. Private API and test changes never Breaking Changes. Old behaviour still works (even with deprecation warning) → belongs in Deprecated, not here. When in doubt, not Breaking Changes

Bad/good examples:

- Bad: `"refactor: extract UserService from monolith"` → Good: `"User management is now ~40% faster"`
- Bad: `"Fix auth bug"` → Good: `"Fixed login failure for email addresses containing special characters"`

**Contributors rules:**

- List **every** PR author in range — human and bot alike; community acknowledgement essential for growth
- **Bots**: collect all bot handles (accounts ending in `[bot]` or known bots like `dependabot`, `renovate`, `github-actions`), render as single italic line at bottom of section: `*Automated contributions: @bot1, @bot2*` — never list bots individually
- **NEVER guess or hallucinate real name.** Wrong name in public release notes = serious error. When in doubt, omit name entirely.
- **Name lookup protocol** — run for every human contributor @handle before writing entry:
  1. `gh api /users/<handle> --jq '.name'` — if non-null and non-empty, use as real name (high confidence)
  2. LinkedIn already resolved by **Extract contributors** phase (SKILL.md — ordered grounding chain: GitHub Social Accounts API → `.blog` field → personal-page scan → past-releases reuse; never by name at any step) — reuse value carried in `$CONTRIBUTORS_FILE` verbatim, don't re-look-up or re-derive here
  3. If name still uncertain: use `@handle` only — no name field
- Format when name confirmed: `* **Full Name** (@handle) ([LinkedIn](url)) – *noun phrase*`
- Format when name not confirmed: `* @handle – *noun phrase*`
- LinkedIn optional — include only when already resolved by Extract contributors; never construct URL by guessing, never re-look-up here
- New contributors get welcome sentence above list
- Maintainer always listed last with infra / CI / docs scope
- Precede Contributors section with `---` separator

**Last line (required):**

Every release entry must end with:

`**Full changelog**: https://github.com/[org]/[repo]/compare/vPREV...vNEXT`

- Must be final line of entry, after Contributors section — no notes, annotations, separators, or prose may follow
- Never omit, even for patch releases
