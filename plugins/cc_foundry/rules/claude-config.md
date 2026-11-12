---
description: Universal operational rules — no hardcoded paths, Bash timeouts, and directory navigation pattern
paths:
  - '**'
---

> Extracted sections bind before their activity. Resolve with active Foundry’s `bin/resolve_plugin_root.py --plugin-name foundry` helper (registry preferred); set `FOUNDRY_ROOT` to the result in the same Bash call. Never select a different installed version by mtime. For repository maintenance only, use source-tree `plugins/cc_foundry`. Unreadable detail → stop and report.
>
> ```bash
> RULE_FULL="$FOUNDRY_ROOT/rules/_full/claude-config.md"  # timeout: 5000
> cat "$RULE_FULL"  # timeout: 5000
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

## Turn Batching — one response, every independent call

> Before applying, Read `_full/claude-config.md` §Turn Batching — one response, every independent call; binding inline. If it cannot be read, stop and report the missing prerequisite.

Batch consecutive edits to one file and independent calls of every kind in one response. Only a genuine data dependency justifies another response; an edit depending on a preceding result is exempt. Per-fix narration still applies (`quality-gates.md` §Reporting Findings).

## Directory Navigation Commands

> Before applying, Read `_full/claude-config.md` §Directory Navigation Commands; binding inline. If it cannot be read, stop and report the missing prerequisite.

Never combine navigation and a command in one Bash call: always two separate calls; CWD persists. Applies to every command, not only worktrees.

## TMPDIR Sentinel Scoping

> Before applying, Read `_full/claude-config.md` §TMPDIR Sentinel Scoping; binding inline. If it cannot be read, stop and report the missing prerequisite.

## List Range Label Discipline

When editing file with lettered/numbered list range labels (e.g. `**Close-scenario archetypes (A–G):**`):

- After any edit adding/removing list items, update **all** range labels in file — not just edited section
- Non-contiguous letter ranges: use explicit form `A–C, F–G`, not `A–G`, when items missing
- Scan entire file after edits to catch stale range labels elsewhere

## Agent/Skill Spawn Discipline

> Before applying, Read `_full/claude-config.md` §Agent/Skill Spawn Discipline; binding inline. If it cannot be read, stop and report the missing prerequisite.

For work-displacement only, estimate native tool-calls: under ~73 → inline; isolation-motivated dispatch (distinct role/system prompt, adversarial independence, different model tier, worktree) is exempt. Prefer an existing domain specialist once warranted. One agent per independent subtask; batch small related work only below the ~55-tool-call workload bound, otherwise split into parallel groups. Keep each agent near ~55 calls; past ~60 risks a missing envelope. `Skill()` overhead remains unverified, never inferred from the ~120,851 tok / ~12.0 s `Agent()` measurements.

## Parallel Spawn Ceilings — by Model Tier

> Before applying, Read `_full/claude-config.md` §Parallel Spawn Ceilings — by Model Tier; binding inline. If it cannot be read, stop and report the missing prerequisite.

## Lint/Format — Pinned Hooks, Not Bare Tools

<!-- policy-sibling: plugins/cc_foundry/rules/python-code.md (§Lint/Format) -->

Project pins lint/format tools via `.pre-commit-config.yaml` → never invoke them directly (`ruff`, `eslint`, `mdformat`, …; version/config drift vs CI); run `pre-commit run <hook-id> --files <path>`, ad-hoc checks included. Hook missing → add it to the config; no pin → direct tool use fine. Command forms: `python-code.md` §Lint/Format.

## Instruction-File Compression Gate

<!-- policy-sibling: plugins/cc_foundry/rules/markdown.md (§Instruction-File Compression Gate) -->

Applies only to instruction files an LLM host loads (`AGENTS.md`, `CLAUDE.md`, rule, skill, or agent-definition files), never to ordinary docs or code. Compressing or structurally reformatting one = behavior-sensitive: byte-exact pre-change backup first, prove complete semantic preservation, restore the pre-change file on any loss; unresolved difference blocks completion. Five-step gate: `markdown.md` §Instruction-File Compression Gate (auto-loads on first access of any `*.md`) — Read before compressing.

## Ask Before Acting on Unknown Cause

When user asks "why" about something (deleted content, unexpected state, missing items) and cause unknown:

- **Never act** — do not restore, revert, or modify anything
- State clearly cause unknown and why (e.g. "pre-session change not made by me")
- Call `AskUserQuestion` tool directly — only an actual tool invocation counts; bracketed or narrated forms never do (`communication.md` §Interactive Questions)

Restoring without being asked = overstepping. "Why" = question, not request to fix.

## Git Commit & Push — Hard Bans

<!-- policy-sibling: plugins/cc_foundry/rules/git-commit.md -->

Always on, format bullets included: the full commit rule (`git-commit.md`) is injected by `rule-inject.js` after the first commit-creating git call (`add`, `commit`, `push`, `merge`, …) of a session or subagent runs, beside that call's result — after the first message was drafted. Before drafting any commit message, Read `~/.claude/rules/foundry-git-commit.md` and its `_full/git-commit.md` unless already in context.

- Never commit without authority: skill workflow step, explicit same-turn user request, local Git grant, or same-turn `git-approve` answer
- Never `git add -A` / `git add .` (stage by name); never `--no-verify`; never `--no-gpg-sign` unless the user asks
- Subject `type(scope): detail` or `type: detail` ≤50 chars naming the highest-tier change in `git diff HEAD` + `git diff --stat HEAD`; never draft from session memory; omit redundant scope
- Evidence-only body: every clause traces to a `+`/`-` line in `git diff HEAD`; no body line wrap; no GitHub auto-links (`#N`, `@name`); no non-VCS paths (`/tmp/`, `~/.claude/`)
- Force-push (`-f`/`--force`/`--force-with-lease`) forbidden on any branch, always; regular `git push` asks via `AskUserQuestion` every time, skill workflows included
- Detect the default branch dynamically, never hardcode `main`/`master`; prefer `git revert` over `reset --hard`, merge commits over rebase
- Co-author trailer on every commit, after a `---` separator: `Co-authored-by: claude[bot] <209825114+claude[bot]@users.noreply.github.com>` ; add `Co-authored-by: Codex <codex@openai.com>` when Codex shaped the outcome; run the Codex-activity check from `_full/git-commit.md` §Co-authors before drafting — replaces the host's default attribution line
