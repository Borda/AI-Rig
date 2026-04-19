---
description: Universal operational rules — no hardcoded paths, Bash timeouts, and directory navigation pattern
paths:
  - '**'
---

## Path Rules

- No hardcoded absolute user paths (`/Users/<name>/` or `/home/<name>/`) — use `.claude/`, `~/`, or `git rev-parse --show-toplevel`
- **Artifact dirs** belong at project root, not inside `.claude/` — see `artifact-lifecycle.md`

## Bash Timeouts

Every Bash call in skill or agent workflow must include explicit `timeout` — **3× expected P90 duration**. Never rely on default 120 s cap; fail fast, let caller retry.

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

- When in doubt, use 3× fastest plausible time — not worst case
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

**Why**: Claude Code's permission matcher checks only **first token** of Bash command. Any compound using `&&`, `;`, or `||` presents `cd` as first token — matches no allow entry — even when `Bash(uv run pytest:*)`, `Bash(python3:*)`, or similar rules in allow list. Applies to every command, not just worktrees.

Working directory persists between Bash calls — two sequential calls always equivalent to compound form.
