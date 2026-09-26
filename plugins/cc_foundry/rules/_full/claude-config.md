---
description: Universal operational rules — no hardcoded paths, Bash timeouts, and directory navigation pattern
paths:
  - '**'
---

## Bash Timeouts — Per-Operation-Class Table

| Operation class | Expected P90 | 3× timeout |
| -- | -- | -- |
| `gh pr view`, `gh pr diff`, `gh issue view` | 2 s | `timeout: 6000` |
| `gh pr checks`, `gh pr list` | 5 s | `timeout: 15000` |
| `gh api --paginate`, `gh release list` | 10 s | `timeout: 30000` |
| Local git commands (`git log`, `git diff`, `git status`) | 1 s | `timeout: 3000` |
| `pip install`, `npm install`, `brew install` | 30 s | `timeout: 90000` |
| Test suite (`pytest`, `uv run pytest`) | 3 min | `timeout: 600000` |
| Build / compile step | 2 min | `timeout: 360000` |
| Simple shell utilities (`wc`, `find`, `grep`, `ls`) | 0.5 s | `timeout: 5000` |
| Any other command (unknown duration) | estimate P90 conservatively | 3× estimate; min `timeout: 15000` |

## Directory Navigation Commands — Why + Worktrees

**Why**: Claude Code's permission matcher checks only the **first token** of a Bash command.

- Compound via `&&`, `;`, or `||` presents `cd` as first token — matches no allow entry
- True even with `Bash(uv run pytest:*)`, `Bash(python:*)`, or similar rules in allow list
- Applies to every command, not just worktrees

**Worktrees**: same rule inside `isolation: "worktree"` agents (CWD = worktree root — no `cd` prefix needed). Worktree settings snapshot at creation time — permissions added to main project after aren't reflected; unexpected permission prompt in a worktree agent → check if `settings.local.json` changed since worktree creation.

## TMPDIR Sentinel Scoping — Verified Facts, Wrong-Form Examples, Exemptions, Migration Debt

**Wrong-form examples** (stub keeps the correct form only):

```bash
# ✗ wrong — bare name, collides across concurrent sessions/projects
echo "$RUN_DIR" > "${TMPDIR:-/tmp}/oss-review-run-dir"

# ✗ wrong — `CLAUDE_SESSION_ID` does not exist; `$$` is a NEW shell PID every Bash tool call
echo "$RUN_DIR" > "${TMPDIR:-/tmp}/oss-review-run-dir-${CLAUDE_SESSION_ID:-$$}"
```

**Verified token facts** (2026-07-21): env var is `CLAUDE_CODE_SESSION_ID` (not `CLAUDE_SESSION_ID`); `$$` changes per Bash tool call (fresh shell each call) — never use it; `$PPID` = Claude Code process PID, stable across all Bash calls in a session; spawned subagents inherit the SAME `CLAUDE_CODE_SESSION_ID` and `$PPID` as the lead — cross-agent sentinel sharing works.

**Cross-OS** (Linux/macOS/Windows-Git-Bash): `${TMPDIR:-/tmp}`, `$PPID`, `export` all work in Git Bash — `/tmp` mounts to the Windows temp dir.

**Exempt** (no `CSID` suffix, mark line with trailing `# tmpdir-exempt: <reason>`): `mktemp ...XXXXXX` templates (already unique); sentinels produced/consumed by code running outside Claude Code (e.g. git post-commit hooks — no session env there; keep project-slug scoping).

**Migration debt**: existing bare-name sentinels (78 files, 794 occurrences repo-wide as of 2026-07-21) are known debt — migration plan: `.plans/active/todo_protected-locations-rollout.md`; their lack of suffixing isn't precedent for new code.

## Agent/Skill Spawn Discipline — Worked Rationale

**Measured cost**: linear regression over 10 subagent transcript runs found `Agent()` spawns carry ~120,851 tok fixed overhead (role scaffold, tool schemas, boilerplate context) — equivalent to ~73 native tool-calls at ~1,647 marginal tok/call — plus ~12.0 s wall-clock per spawn, regardless of task size. Corroborated independently across 116 subagent transcripts: observed fixed-cost floor 114,855 tok, within 5% of the regression estimate. `Skill()` wasn't part of the measured population — treat its overhead as unverified, not as sharing this constant, especially across model tiers (e.g. haiku-tier skill vs opus-tier agent).

**Decision table** — the threshold below governs work-displacement spawns only; isolation-motivated spawns (distinct role/system-prompt, adversarial independence, different model tier, worktree) are exempt regardless of call count — see stub §Agent/Skill Spawn Discipline:

| Situation | Action |
| -- | -- |
| Task needs < ~73 tool-calls, single domain, no isolation requirement | Do inline — no spawn |
| Task needs ≥ ~73 tool-calls, and no isolation requirement | Spawn one specialist matching the domain |
| Isolation requirement (distinct role/prompt, adversarial check, model tier, worktree) — any call count | Spawn — the threshold doesn't apply |
| N independent files/subtasks, each individually small, no isolation need, summed load < ~55–60 calls | Batch into one spawn covering all N, not N spawns |
| N independent files/subtasks, each individually small, but summed load ≥ ~55–60 calls | Split into parallel spawns, each kept under the bound — one batched spawn here is the same stall risk as any oversized single agent |
| N independent subtasks each individually large (≥ ~73 calls) | N parallel spawns — one per subtask, still not per-step |
| Read-only lookup ("where is X defined", "what does Y do") | Native Grep/Glob/Read — never spawn a lookup agent |

**Anti-patterns observed:**

- Spawning `general-purpose` for a single-file edit reachable with Edit — pure overhead, no specialist benefit, no isolation need.
- Spawning once per file in a batch of small, similar work-displacement edits instead of one spawn covering the batch — unless the batch's summed load approaches the ~55–60-call bound below, in which case one spawn covering all of it is the anti-pattern: split into parallel spawns instead, each kept under the bound.
- Spawning a subagent to answer a question the orchestrator could answer by reading one file already in context.
- Re-spawning near-identical agents across turns for the same investigation instead of resuming via `SendMessage` to the still-running agent.
- Applying the work-displacement threshold to an isolation-motivated spawn (e.g. skipping a `foundry:challenger` post-fix review that cleared the stakes gate in `debugging.md`, because it's "only 12 tool calls") — isolation value isn't priced by call count; don't gate it on call count. Converse isn't an exemption: a spawn below that stakes gate shouldn't fire at all, however many calls it would displace.

**Upper bound**: keep each spawned agent's workload near ~55 tool-calls. Measured stall behavior: ≤57 calls returned a clean envelope 5/5 runs; 87–142 calls stalled 3/5 runs before emitting a final envelope; the 58–86 range untested — "~60" is an interpolated working bound across that gap, not a measured cliff. Past it, agents risk stalling before emitting their final envelope, forcing the orchestrator to reconstruct state from disk artifacts instead of the returned summary — split oversized work across multiple agents, don't spawn more small ones. This bound is why the "batch N small subtasks into one spawn" row above carries a split condition: batching without checking the sum reproduces this exact stall risk in a different guise — a single agent given N small tasks stalls on the same measured curve as a single agent given one big one.

**Open question — not yet measured**: `CLAUDE.md` §7 Context Cost Discipline notes cache-read cost scales with live context size × turn count. A parallel spawn (when the file-handoff protocol is followed) returns only a compact envelope, keeping the orchestrator's context flat; inline/batched work grows that context permanently, and the growth is paid again on every later turn in the session, not just the current one. This could mean parallel wins even below raw token parity with sequential/batched execution once enough later turns are counted — but no break-even turn-count has been measured, so this is a hypothesis, not a rule. Before hardening it into policy, measure it via `bin/cost_analyzer.py` or `/foundry:profile` on real sessions with a batch-vs-parallel split.
