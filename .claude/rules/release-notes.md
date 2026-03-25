---
description: Release notes and CHANGELOG format rules
paths:
  - '**/CHANGELOG.md'
  - '**/PUBLIC-NOTES.md'
---

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
- Code examples in fenced blocks where helpful; tables for option comparisons

## Contributors Section

- End with `---` separator then `## 🏆 Contributors` (h2, NOT h3)
- **NEVER guess or hallucinate a contributor's real name** — wrong names in public release notes are a serious error
- Name lookup: `gh api /users/<handle> --jq '.name'` first; if empty, spawn `web-explorer` to search LinkedIn; if still uncertain, use @handle only
- Format when name confirmed: `* **Full Name** (@handle) ([LinkedIn](url)) – *description*`
- Format when name not confirmed: `* @handle – *description*`
- LinkedIn link only if found via lookup; never construct a URL by guessing
- Description is brief and italicised, focused on what they built/fixed
- Include @Borda at the end with infra/docs/coordination work

## Last Line

`**Full changelog**: https://github.com/[org]/[repo]/compare/vPREV...vNEXT`

Full template + writing-patterns table in `.claude/skills/release/SKILL.md` (Step 3).
