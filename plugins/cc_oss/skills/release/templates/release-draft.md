# [Release version] — [Release name]

## 📋 Summary

> Summary guidance:
>
> - Write an elevator pitch from one developer to another, readable in ten seconds. Render it from the executive summary and highlights; never paste their paragraphs. Keep paragraphs to at most two sentences; split or distill long lines.
> - Use one plain-language hook line of at most 25 words, one to five real win bullets of at most 20 words each, and one upgrade-call line.
> - Order wins by impact; never pad—a one-fix patch gets one bullet. Bold at most one feature/API name per win.
> - Describe user outcomes without copying Spotlight or Notable sentences. Keep PR references in Spotlights and Notable changes.
> - Put breaking or removed items in the upgrade call, not the win bullets; a deprecation may add an optional heads-up clause there. State who should upgrade and what it costs, such as “Drop-in upgrade, no code changes” or “One rename to handle—see Migration guide.”
> - `--append` cycles re-fold this section and may add one `**New since last draft:**` pointer line after the upgrade call; it names new wins only and is outside the win count.

[Release hook]

- [User-facing win]

[Upgrade guidance]

## ✨ Spotlights / highlights

> Present the top three to five features or fixes, each with a short code example.

[Release highlights]

## 🔄 Migration guide

> A breaking change worked before and now fails or behaves differently, without prior warning or a deprecation shim. Classify an API removed after a prior release deprecated it with a warning and forwarding as ❌ Removed, not ⚠️ Breaking Changes. Every Breaking, Deprecated or Removed symbol carries one before/after snippet, its affected call sites listed under it, in the shape defined in `SKILL.md` “Draft migration guide” (Snippet shape). If none, say “No breaking changes, deprecations or removals in this release.”

[Migration guidance]

> Use the draft migration guide content; do not regenerate it independently.

## 📝 Notable changes

> Group significant changes by area or component; list all supporting PRs and commits.

[Notable changes]

### 🚀 Added

- **Feature Name** — what it does and why it matters. (#PR or commit)

### ⚠️ Breaking Changes

- **[Area]** — what changed and what callers must do to migrate. (#PR or commit)

### 🌱 Changed

- Behaviour change: old behaviour → new behaviour. (#PR or commit)

### 🗑️ Deprecated

- `OLD_NAME` deprecated in favour of `NEW_NAME`. (#PR or commit)

### ❌ Removed

- `OLD_API` removed (deprecated since vX.Y). Migrate to `NEW_API`. (#PR or commit)

### 🔧 Fixed

- Fixed what was broken when condition. (#PR or commit)

## 🏆 Contributors

- **[Contributor name]** ([Optional verified handle or profile links]) — [Contribution summary].

> Contributors follow the canonical format in `SKILL.md` “Extract contributors”.

______________________________________________________________________

**Full changelog**: [Full changelog URL]

*Drafted by [`/oss:release`](https://borda.github.io/AI-Rig/cc_oss/#oss-release), an AI skill in **[AI Rig](https://github.com/Borda/AI-Rig)**. If it saved you time, a ⭐ helps others find it.*

> Keep the closing drafted-by line exactly as written, as the last line after the changelog link; when `printenv AI_RIG_FOOTER`, run bare (no redirect, no `|| true`), prints `off` (any case), delete that line instead (empty output with exit 1 means unset: keep it). Replace every square-bracket placeholder text (not Markdown link labels such as `[LinkedIn](url)` or `[#947](url)`) with verified release content; omit unused optional fields and empty categories. The changelog link has the shape `https://github.com/<owner>/<repo>/compare/<previous tag>...<new tag>`. Writing notes are template instructions, not release-note content. <!-- policy-sibling: plugins/cc_foundry/rules/public-github.md, plugins/cc_foundry/rules/_full/public-github.md, plugins/cc_oss/skills/release/guidelines/writing-rules.md, plugins/cc_oss/skills/release/modes/demo.md, plugins/cc_oss/skills/release/modes/notes-draft.md, plugins/cc_oss/skills/release/modes/release-draft-template.md — AI_RIG_FOOTER opt-out wording restated for each consumer; edit all seven together. -->
