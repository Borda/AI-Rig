---
description: Universal operational rules — no hardcoded paths, Bash timeouts, and directory navigation pattern
paths:
  - '**'
---

## Path Rules

- No hardcoded absolute user paths — use `.claude/`, `~/`, or `git rev-parse --show-toplevel`
- **Artifact dirs** belong at project root, not inside `.claude/` — see `artifact-lifecycle.md`

## Bash Timeouts

Every Bash call must include explicit `timeout` — **3× expected P90 duration**.
Never rely on default 120 s cap; fail fast, let caller retry.

| Operation class | Expected P90 | 3× timeout |
| --- | --- | --- |
| `gh pr view`, `gh pr diff`, `gh issue view` | 2 s | `timeout: 6000` |
| `gh pr checks`, `gh pr list` | 5 s | `timeout: 15000` |
| `gh api --paginate`, `gh release list` | 10 s | `timeout: 30000` |
| Local git commands (`git log`, `git diff`, `git status`) | 1 s | `timeout: 3000` |
| `pip install`, `npm install`, `brew install` | 30 s | `timeout: 90000` |
| Test suite (`pytest`, `uv run pytest`) | 3 min | `timeout: 600000` |
| Build / compile step | 2 min | `timeout: 360000` |
| Simple shell utilities (`wc`, `find`, `grep`, `ls`) | 0.5 s | `timeout: 5000` |

Rules:

- Use 3× fastest plausible time — not worst case
- Timed-out fast op = signal to investigate; frozen session is not
- `timeout: 120000` only for test suites or builds, never network calls

## Directory Navigation Commands

Never combine directory navigation with command in single Bash call — always use **two separate Bash calls**:

```bash
# ✓ correct — two calls; working directory persists between calls
cd /path/to/dir
uv run pytest tests/

# ✗ wrong — all three forms below cause the same failure
cd /path && uv run pytest tests/
cd /path
uv run pytest tests/
cd /path || uv run pytest tests/
```

**Why**: Claude Code's permission matcher checks only **first token** of Bash command.
- Compound using `&&`, `;`, or `||` presents `cd` as first token — matches no allow entry
- Even when `Bash(uv run pytest:*)`, `Bash(python3:*)`, or similar rules in allow list
- Applies to every command, not just worktrees

Working directory persists between Bash calls — two sequential calls equivalent.

## List Range Label Discipline

When editing a file that contains lettered or numbered list range labels (e.g. `**Close-scenario archetypes (A–G):**`):

- After any edit that adds or removes list items, update **all** range labels in the file — not just the section edited
- Non-contiguous letter ranges: use explicit form `A–C, F–G`, not `A–G`, when items are missing
- Scan the entire file after edits to catch stale range labels elsewhere

## Ask Before Acting on Unknown Cause

When a user asks "why" about something that happened (deleted content, unexpected state, missing items) and the cause is unknown:

- **Never act** — do not restore, revert, or modify anything
- State clearly that the cause is unknown and why (e.g. "pre-session change not made by me")
- Ask the user what they want done before taking any action

Restoring without being asked = overstepping. The user asking "why" is a question, not a request to fix.
