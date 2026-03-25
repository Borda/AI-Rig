---
description: Git commit conventions and safety rules — applies globally
---

## Commit Message Format

- First line: short TLDR subject in imperative mood, ≤50 chars — name up to 3 most significant changes/additions/removals only (prefer user-visible impact as the tie-breaker when significance is comparable)
- Blank line, then bullet list — one bullet per logical change; include extended description of the top changes plus all other notable changes; skip typos, linting, and whitespace-only edits; if all changes are skip-worthy, omit the bullet list entirely and use a subject-only commit
- No line wrapping — each bullet is a single long line

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
- Never force-push to main/master

## Worktree Commands

When running commands inside a git worktree, use **two separate Bash calls** rather than `cd /path && command`:

```bash
# Call 1 — sets CWD
cd /path/to/worktree

# Call 2 — real command; first token matches allowlist
uv run pytest tests/
```

This is required because Claude Code's permission matcher checks only the **first token** of a Bash command. Applies to `uv run`, `python`, `pytest`, `git`, etc.

Alternative: spawn an agent with `isolation: "worktree"` — its CWD is the worktree root, no `cd` prefix needed.

Worktrees land under `.claude/worktrees/<id>/`. Permissions in `settings.local.json` are snapshotted at worktree-creation time — permissions added after creation are not reflected automatically.
