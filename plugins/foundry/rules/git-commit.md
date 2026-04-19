---
description: Git commit conventions and safety rules — applies globally
paths:
  - '**'
---

## Commit Message Format

- First line: short TLDR subject in imperative mood, ≤50 chars — name up to 3 most significant changes/additions/removals only (prefer user-visible impact as tie-breaker when significance comparable)

- Blank line, then bullet list — one bullet per logical change; include extended description of top changes plus all other notable changes; skip typos, linting, whitespace-only edits; if all changes skip-worthy, omit bullet list entirely and use subject-only commit. Subject-only commit still include co-author block separated by blank line and ---:

  ```
  Fix typo in config key name

  ---
  Co-authored-by: Claude Code <noreply@anthropic.com>
  ```

- No line wrapping — each bullet single long line

## Gathering Diff Context

Before writing commit message, always run these three commands in parallel:

- `git status` — identify staged new files (`A` prefix) and unstaged changes
- `git diff HEAD` — **not** bare `git diff`; bare `git diff` shows only unstaged changes and misses staged new files entirely; `git diff HEAD` captures both staged and unstaged changes vs HEAD
- `git log --oneline -5` — reference repo's existing commit style

**Truncated diff — mandatory follow-up**: when `git diff HEAD` output large and Bash tool saves to file (showing only 2 KB preview), **read saved file completely before writing commit**. Don't write from preview alone — most significant changes often past truncation point. Also run `git diff --stat HEAD` (always fits in context) for complete file-by-file change map; use stat output to identify which files changed most and whether any missed in preview.

**Ranking rule — diff first, recency last**: rank significance across full diff before writing title. Conversational recency bias must not dominate. Title must reflect most significant change in diff, not most recent one.

**New files are always significant**: any file marked `A` in `git status` must be explicitly mentioned in commit bullet list, regardless of line count. New files represent added capability, not just changed lines.

**Semantic novelty beats diff verbosity**: when ranking significance, new capability/interface/script outranks verbose-but-routine config edit even if config diff has more lines. Ask "what would reviewer need to know first?" — that most significant change.

## Co-authors

Separate co-author block from bullet list with `---`:

```
- last bullet

---
Co-authored-by: Claude Code <noreply@anthropic.com>
```

- Claude: `Co-authored-by: Claude Code <noreply@anthropic.com>`
- Codex (if contributed anything — code, review, diagnosis, analysis, architectural guidance, or "here's what needs fixing and why"): `Co-authored-by: OpenAI Codex <codex@openai.com>`

**Codex intellectual contributions count**: Codex earns trailer whenever it shaped outcome — even if Claude wrote final code. Examples: Codex identified root cause, Codex suggested approach, Codex returned review comment that led to change. Test: "would this commit exist in current form without Codex's input?" — if yes, include trailer.

Co-author trailer added to every commit produced by Claude Code — not conditional on user explicitly mentioning Claude's involvement.

**Skill commit templates — trailers not optional**: when skill or workflow step provides `git commit -m "..."` template (heredoc or one-liner), template is **message body scaffold only**. `---` separator and co-author block must always be appended regardless of whether template shows them:

- **Heredoc** (`cat <<'EOF' ... EOF`): insert `---` block and trailers before closing `EOF`
- **One-liner `-m "string"`**: convert to heredoc — one-liners cannot carry multi-line trailers

Never skip trailers because skill template omits them.

## Branch Safety

- **Never commit to main/master** — check current branch first; if on default branch → warn and stop, ask user to create feature branch
- `develop:fix` Step 0 enforces this with hard abort

## Staging and Hooks

- Never `git add -A` or `git add .` — always stage specific files by name
- Never `--no-verify` — if pre-commit blocks, fix underlying issue
- Never `--no-gpg-sign` unless user explicitly requests it

## Push Safety

- **Never push without explicit user confirmation** — always ask before any `git push`, including branch pushes, PR pushes, and release tags
- Authorization scoped: "commit this" does not authorize "push this"; ask separately for every push
- Applies inside skill workflows too — if skill (e.g. `/resolve`) includes push step, treat as "propose and confirm", not "auto-execute"; stop after committing, report what ready to push, wait for user to say push
- Never push in autonomous bug fixing or as "final step" without being explicitly asked in that message
- Never force-push (`--force`, `--force-with-lease`) to main/master; never force-push without explicit user instruction even on feature branches

## History Safety

- Prefer `git revert` over `git reset --hard` (preserves history)
- Prefer merge commits for conflict resolution over rebase (preserves SHAs)
