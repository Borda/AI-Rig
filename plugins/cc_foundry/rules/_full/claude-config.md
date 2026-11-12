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

## TMPDIR Sentinel Scoping

Cross-bash-block state (Bash tool state doesn't persist across separate tool invocations in a skill) persists via `${TMPDIR:-/tmp}/<name>` sentinel files. `/tmp` is machine-global, not project- or session-scoped — a bare name collides if two Claude Code sessions run the same skill concurrently.

**Rule**: every bash block touching sentinels derives the session token once (first line), then every sentinel name takes `-${CSID}` as its **terminal** suffix (after any dynamic part, after any extension):

```bash
# ✓ correct — session-scoped, safe under concurrent sessions
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo "$RUN_DIR" > "${TMPDIR:-/tmp}/oss-review-run-dir-${CSID}"

# ✗ wrong — bare name collides; ✗ wrong — CLAUDE_SESSION_ID doesn't exist, $$ is a new PID per Bash call
```

Wrong-form examples in full + verified token facts (env var name, `$$`/`$PPID` behaviour, subagent inheritance): `_full/claude-config.md`.

**Sentinel READS use `read`, never `$(cat ...)`** — command substitution makes prefix allow-rules fail-closed → "Contains expansion" permission prompt in every subagent:

```bash
# ✓ correct — no substitution, allow rules match, shape lane of allow-dispatch covers compounds
IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/oss-review-run-dir-${CSID}" 2>/dev/null || RUN_DIR=""

# ✗ wrong — $() triggers "Contains expansion" prompt regardless of allow list
RUN_DIR=$(cat "${TMPDIR:-/tmp}/oss-review-run-dir-${CSID}" 2>/dev/null || echo "")
```

`read` takes the first line only, exits non-zero on a file without trailing newline — safe here since sentinel writes use `echo` (single line + newline); always append `|| VAR=<default>` for the missing-file case. Multi-line payloads (JSON blobs, report content) aren't sentinels — keep `$(cat ...)` there, expect the prompt.

- `export` (not plain assignment) — python/node children invoked in the same block must see `CSID`
- Python scripts: `_CSID = os.environ.get("CSID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "shared"` — never `os.getppid()`; temp base via `os.environ.get("TMPDIR") or (tempfile.gettempdir() if os.name == "nt" else "/tmp")` (= shell `${TMPDIR:-/tmp}`)
- Applies to every new sentinel added to any skill, hook, or bin script
- Args-derived suffixes alone (e.g. `${CLEAN_ARGS}`, a PR number) are not sufficient — two sessions in two *different* projects invoking the same skill with the same args still collide; `-${CSID}` is still required alongside them, not instead of them
- Cleanup (`rm -f`) at skill completion must target the exact session-scoped filename, never a glob that could match another session's sentinel of the same base name

Cross-OS notes, exemption list (`mktemp` templates, non-CC-runtime sentinels), and existing-debt/migration-plan pointer: `_full/claude-config.md`.

## Parallel Spawn Ceilings — by Model Tier

<!-- policy-sibling: plugins/cc_foundry/rules/agent-spawn.md (§Parallel Spawn Ceilings) -->

In-flight `Agent()` ceilings per model tier, summed across every step/phase due in one response: `haiku` 20 · `sonnet` 8 · `opus` 5 — separate pools per tier, grown in waves of ≤5; pool = the spawn's effective model (an `Agent(..., model=…)` override counts). Tier roster table + multi-phase capping: `agent-spawn.md` §Parallel Spawn Ceilings — injected on the first `Agent()` call, loaded with skill/agent files.

## Turn Batching — one response, every independent call

A response is one billed turn, and a turn re-reads the entire live context. Measured 2026-09-29 over 14 days: mean **221,527 tok** of cache read per turn, **69.2%** of all spend. Splitting work across turns that could have shared one is therefore the most expensive habit available, and it costs the same whether the extra turn does real work or none.

- **Consecutive edits to the same file go in one response** — several `Edit` calls together, never one call per turn. Measured: 180 runs of 3+ single-edit turns on one file, **513 turns collapsible**. Exception: an edit whose content depends on the previous edit's result.
- **Independent tool calls of any kind batch** — parallel `Read`s, parallel `Grep`s, a `TaskUpdate` beside the next real call. Only a genuine data dependency justifies a second turn.
- **Per-fix narration still applies** (`quality-gates.md` §Reporting Findings) — batching changes the turn count, never what gets stated.

## Directory Navigation Commands

Never combine directory navigation with command in single Bash call — always use **two separate Bash calls**:

```bash
# ✓ correct — two calls; CWD persists between calls
cd /path/to/dir
uv run pytest tests/

# ✗ wrong — all three forms cause same failure
cd /path && uv run pytest tests/
cd /path
uv run pytest tests/
cd /path || uv run pytest tests/
```

Working directory persists between Bash calls — two sequential calls equivalent. Applies to every command, not just worktrees. Why (permission-matcher first-token mechanism) + worktree-specific notes: `_full/claude-config.md`.

## Agent/Skill Spawn Discipline

Spawning `Agent()` costs fixed overhead — measured ~120,851 tok (~73 tool-calls' worth) plus ~12.0 s per spawn, regardless of task size. Gate below applies **only to work-displacement spawns** — a spawn chosen purely to move tool-calls out of the caller's context. **Not** for a spawn chosen for role isolation: distinct system prompt, adversarial independence, different model tier, or worktree. Isolation value isn't priced by the ~73-call figure, so isolation-motivated spawns are exempt from the threshold, not exceptions to it.

- **Work-displacement spawn** (no isolation need — same role would do, just fewer tokens in caller context): estimate tool-calls first. Under ~73 → do it inline with native tools (Read/Grep/Glob/Edit/Bash), no spawn.
- **Isolation-motivated spawn** (distinct role/system-prompt, adversarial check, different model tier, or worktree required) → spawn regardless of call count; the isolation is the point, not a byproduct. Named standing examples: `bridge:review`/`foundry:challenger` adversarial passes, any role-specialist dispatch mandated by another rule.
- One subagent per independent subtask, not one per trivial step — batch related work-displacement spawns into a single spawn instead of a spawn-per-file or spawn-per-check. **Cap the batch**: only while the summed load of everything batched into it stays under the ~55-tool-call upper bound below — past that, split into parallel spawns (grouped under the bound) instead of one oversized batch. Worked stall evidence: `_full/claude-config.md`.
- A specialist exists for the domain (`foundry:sw-engineer`, `foundry:curator`, etc.) → prefer it over `general-purpose` once a spawn is warranted under either bullet above.
- Keep each spawned agent near ~55 tool-calls; past ~60 it risks stalling without returning an envelope, forcing reconstruction from disk — split oversized work across agents, not more small ones.
- `Skill()` cost **not** covered by the measured figure above — regression measured `Agent()`-backed subagent transcripts only, no `Skill()` invocation. Treat `Skill()` overhead as unverified until measured separately; don't assume it shares `Agent()`'s constant, especially across model tiers.
- Worked rationale + threshold derivation: `_full/claude-config.md`.
