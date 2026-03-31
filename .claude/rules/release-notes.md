---
description: Release notes and CHANGELOG format rules
paths:
  - '**/CHANGELOG.md'
  - '**/PUBLIC-NOTES.md'
---

## File Scope

- `CHANGELOG.md` — full technical record; all sections apply; PR references required
- `PUBLIC-NOTES.md` — user-facing release announcement; PR references optional; may omit Removed/Deprecated sections if empty; tone should be accessible, not just technical

## Section Order (fixed — never reorder)

`## 🚀 Added` → `## ⚠️ Breaking Changes` → `## 🌱 Changed` → `## 🗑️ Deprecated` → `## ❌ Removed` → `## 🔧 Fixed`

- **Breaking Changes goes right after Added** — never first, never at the very end

## Breaking vs Deprecated

- **Breaking Changes** = existing code stops working **immediately** after upgrade (API removed, incompatible signature, no fallback)
- **Deprecated** = still works this release but scheduled for removal (emits warning, replacement exists)
- When in doubt → Deprecated, not Breaking

## Writing Rules

- Fetch recent releases first (`gh release list --limit 5` + `gh release view`) to match formatting style
- Deprecated and Removed are independent sections (not collapsed into Changed)
- Each item: description + PR number(s) in parentheses, e.g. `(#263, #702)`
- When an item includes a multiline code block: place `(#number)` at the end of the description text **before** the opening fence — never after the closing fence; trailing refs after fenced blocks are invisible in rendered output and get lost
- Code examples in fenced blocks where helpful; tables for option comparisons

## Contributors Section

- End with `---` separator then `## 🏆 Contributors` (h2, NOT h3)
- **NEVER guess or hallucinate a contributor's real name** — wrong names in public release notes are a serious error
- Name lookup order:
  1. `gh api /users/<handle> --jq '.name'` — if non-empty, name is confirmed
  2. If empty → spawn `web-explorer` with query `"<handle> site:linkedin.com"` — use the name only if the profile page unambiguously matches the GitHub handle (same avatar, bio, or repos cross-referenced)
  3. If still uncertain → use @handle-only format; never guess
- LinkedIn link: include only if found in step 2 with high confidence; use the direct profile URL returned by web-explorer
- Format when name confirmed: `* **Full Name** (@handle) ([LinkedIn](url)) – *description*`
- Format when name not confirmed: `* @handle – *description*`
- Description is brief and italicised, focused on what they built/fixed
- Always include `* @Borda – *release coordination*` as the final contributor entry; omit only if Borda made zero commits, reviews, or coordination work for this release (rare) — update this handle if the release coordinator changes

## Last Line (required)

Every release entry must end with:

`**Full changelog**: https://github.com/[org]/[repo]/compare/vPREV...vNEXT`

- This line must be the final line of the entry, after the Contributors section — no notes, annotations, separators, or prose may follow it
- Never omit it, even for patch releases

Full template + writing-patterns table in `.claude/skills/release/SKILL.md`.
