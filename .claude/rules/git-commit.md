---
description: Git commit conventions and safety rules — applies globally
paths:
  - '**'
---

## Commit Message Format

- First line: short TLDR subject in imperative mood, ≤50 chars — name up to 3 most significant changes/additions/removals only (prefer user-visible impact as the tie-breaker when significance is comparable)
- Blank line, then bullet list — one bullet per logical change; include extended description of the top changes plus all other notable changes; skip typos, linting, and whitespace-only edits; if all changes are skip-worthy, omit the bullet list entirely and use a subject-only commit
- No line wrapping — each bullet is a single long line

## Gathering Diff Context

Before writing a commit message, always run these three commands in parallel:

- `git status` — identify staged new files (`A` prefix) and unstaged changes
- `git diff HEAD` — **not** bare `git diff`; bare `git diff` shows only unstaged changes and misses staged new files entirely; `git diff HEAD` captures both staged and unstaged changes vs HEAD
- `git log --oneline -5` — reference the repo's existing commit style

**Truncated diff — mandatory follow-up**: when `git diff HEAD` output is large and the Bash tool saves it to a file (showing only a 2 KB preview), **read the saved file completely before writing the commit**. Do not write from the preview alone — the most significant changes are often past the truncation point. Also run `git diff --stat HEAD` (always fits in context) to get a complete file-by-file change map; use the stat output to identify which files changed most and whether any were missed in the preview.

**Ranking rule — diff first, recency last**: rank significance across the full diff before writing the title. Conversational recency bias (the last thing worked on in the session) must not dominate. The title must reflect the most significant change in the diff, not the most recent one.

**New files are always significant**: any file marked `A` in `git status` must be explicitly mentioned in the commit bullet list, regardless of line count. New files represent added capability, not just changed lines.

**Semantic novelty beats diff verbosity**: when ranking significance, a new capability, new interface, or new script outranks a verbose-but-routine config edit even if the config diff has more lines. Ask "what would a reviewer need to know first?" — that is the most significant change.

## Co-authors

Separate the co-author block from the bullet list with `---`:

```
- last bullet

---
Co-authored-by: Claude Code <noreply@anthropic.com>
```

- Claude: `Co-authored-by: Claude Code <noreply@anthropic.com>`
- Codex (if it reviewed or implemented any part): `Co-authored-by: OpenAI Codex <codex@openai.com>`

## Branch Safety

- **Never commit to main/master** — check current branch first; if on default branch → warn and stop, ask user to create a feature branch
- `/develop` Step 0 enforces this with a hard abort

## Staging and Hooks

- Never `git add -A` or `git add .` — always stage specific files by name
- Never `--no-verify` — if pre-commit blocks, fix the underlying issue
- Never `--no-gpg-sign` unless user explicitly requests it

## Push Safety

- **Never push without explicit user confirmation** — always ask before any `git push`, including branch pushes, PR pushes, and release tags
- Authorization is scoped: "commit this" does not authorize "push this"; ask separately for every push
- Applies inside skill workflows too — if a skill (e.g. `/resolve`) includes a push step, treat it as "propose and confirm", not "auto-execute"; stop after committing, report what is ready to push, and wait for the user to say push
- Never push in autonomous bug fixing or as a "final step" without being explicitly asked in that message
- Never force-push (`--force`, `--force-with-lease`) to main/master; never force-push without explicit user instruction even on feature branches

## History Safety

- Prefer `git revert` over `git reset --hard` (preserves history)
- Prefer merge commits for conflict resolution over rebase (preserves SHAs)
