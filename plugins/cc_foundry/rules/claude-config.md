---
description: Universal operational rules — no hardcoded paths, Bash timeouts, and directory navigation pattern
paths:
  - '**'
---

> §Bash Timeouts per-operation-class table, §Directory Navigation Commands permission-matcher rationale + worktree notes, §TMPDIR Sentinel Scoping verified-token-facts / wrong-form examples / exemption list / migration-debt note, §Agent/Skill Spawn Discipline worked rationale + threshold derivation have worked detail in `_full/claude-config.md`. Resolve + Read when that section's own trigger applies — not needed for routine work:
>
> ```bash
> RULE_FULL="$(ls -td ~/.claude/plugins/cache/borda-ai-rig/foundry/*/rules/_full/claude-config.md 2>/dev/null | head -1)"; [ -z "$RULE_FULL" ] && RULE_FULL="plugins/cc_foundry/rules/_full/claude-config.md"  # timeout: 5000
> ```

## Path Rules

- No hardcoded absolute user paths — use `.claude/`, `~/`, or `git rev-parse --show-toplevel`
- **Artifact dirs** belong at project root, not inside `.claude/` — see `artifact-lifecycle.md`

## Bash Timeouts

Every Bash call must include explicit `timeout` — **3× expected P90 duration**. Never rely on default 120 s cap; fail fast, let caller retry.

- Use 3× fastest plausible time — not worst case
- Unknown-duration command → estimate P90 conservatively, 3× that, minimum `timeout: 15000`
- Timed-out fast op = signal to investigate; frozen session is not
- `timeout: 120000` only for test suites or builds, never network calls

Per-operation-class lookup table (gh commands, git, installs, test suites, build steps): `_full/claude-config.md`.

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

## TMPDIR Sentinel Scoping

Cross-bash-block state (Bash tool state doesn't persist across separate tool invocations within a skill) commonly persists via `${TMPDIR:-/tmp}/<name>` sentinel files. `/tmp` is machine-global, not project- or session-scoped — a bare name collides if two Claude Code sessions run the same skill concurrently.

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

`read` takes the first line only and exits non-zero on a file without trailing newline — safe here because sentinel writes use `echo` (single line + newline); always append `|| VAR=<default>` for the missing-file case. Multi-line payloads (JSON blobs, report content) are not sentinels — keep `$(cat ...)` there and expect the prompt.

- `export` (not plain assignment) — python/node children invoked in the same block must see `CSID`
- Python scripts: `_CSID = os.environ.get("CSID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "shared"` — never `os.getppid()`; temp base via `os.environ.get("TMPDIR") or tempfile.gettempdir()`, never hardcoded `/tmp`
- Applies to every new sentinel added to any skill, hook, or bin script
- Args-derived suffixes alone (e.g. `${CLEAN_ARGS}`, a PR number) are not sufficient — two sessions in two *different* projects invoking the same skill with the same args still collide; `-${CSID}` is still required alongside them, not instead of them
- Cleanup (`rm -f`) at skill completion must target the exact session-scoped filename, never a glob that could match another session's sentinel of the same base name

Cross-OS notes, exemption list (`mktemp` templates, non-CC-runtime sentinels), and existing-debt/migration-plan pointer: `_full/claude-config.md`.

## List Range Label Discipline

When editing file with lettered/numbered list range labels (e.g. `**Close-scenario archetypes (A–G):**`):

- After any edit adding/removing list items, update **all** range labels in file — not just edited section
- Non-contiguous letter ranges: use explicit form `A–C, F–G`, not `A–G`, when items missing
- Scan entire file after edits to catch stale range labels elsewhere

## Agent/Skill Spawn Discipline

Spawning an `Agent()` costs fixed overhead — measured ~120,851 tok (~73 tool-calls' worth) plus ~12.0 s per spawn, regardless of task size. The gate below applies **only to work-displacement spawns** — a spawn chosen purely to move tool-calls out of the caller's context. It does **not** apply to a spawn chosen for role isolation: a distinct system prompt, adversarial independence, a different model tier, or a worktree. Isolation has value the ~73-call figure does not price in, so isolation-motivated spawns are exempt from the threshold, not exceptions to it.

- **Work-displacement spawn** (no isolation need — same role would do, just fewer tokens in caller context): estimate tool-calls first. Under ~73 → do it inline with native tools (Read/Grep/Glob/Edit/Bash), no spawn.
- **Isolation-motivated spawn** (distinct role/system-prompt, adversarial check, different model tier, or worktree required) → spawn regardless of call count; the isolation is the point, not a byproduct. Named standing examples: `bridge:review`/`foundry:challenger` adversarial passes, `foundry:humanizer`'s haiku-tier prose pass, any role-specialist dispatch mandated by another rule.
- One subagent per independent subtask, not one per trivial step — batch related work-displacement spawns into a single spawn instead of a spawn-per-file or spawn-per-check.
- A specialist exists for the domain (`foundry:sw-engineer`, `foundry:curator`, etc.) → prefer it over `general-purpose` once a spawn is warranted under either bullet above.
- Keep each spawned agent near ~55 tool-calls; past ~60 it risks stalling without returning an envelope, forcing reconstruction from disk — split oversized work across agents rather than spawning more small ones.
- `Skill()` cost is **not** covered by the measured figure above — the regression measured `Agent()`-backed subagent transcripts only, no `Skill()` invocation. Treat `Skill()` overhead as unverified until measured separately; don't assume it shares `Agent()`'s constant, especially across model tiers.
- Worked rationale + threshold derivation: `_full/claude-config.md`.

## Ask Before Acting on Unknown Cause

When user asks "why" about something (deleted content, unexpected state, missing items) and cause unknown:

- **Never act** — do not restore, revert, or modify anything
- State clearly cause unknown and why (e.g. "pre-session change not made by me")
- Call `AskUserQuestion` tool directly — prose questions in brackets (`[AskUserQuestion: ...]`, `[Invoking AskUserQuestion: ...]`) do NOT satisfy this requirement; only actual tool invocation does

Restoring without being asked = overstepping. "Why" = question, not request to fix.
